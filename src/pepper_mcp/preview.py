"""Optional HTTP side channel for humans and non-MCP consumers.

* ``/``                 control dashboard (camera, status, toggles, gestures, events), see
                         :mod:`pepper_mcp.dashboard`
* ``/api/state``        JSON snapshot the dashboard polls
* ``/api/action/<name>`` POST JSON: run a dashboard action. Allowed from this machine; other
                         devices need ``--preview-token`` (``X-Pepper-Token`` header)
* ``/camera/<cam>.mjpg`` live MJPEG stream (top/bottom/depth)
* ``/camera/<cam>.jpg``  single frame
* ``/audio.wav``         live 16-bit PCM WAV stream from the microphone
* ``/events``            server-sent events (every EventBus event as JSON)
* ``/status``            JSON status; ``/assets/<file>`` bundled assets (tablet GIFs)

Only the standard library is used so it never gets in the way of the MCP server.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import queue
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from pepper_mcp import dashboard
from pepper_mcp.robot.audio_out import ASSETS_DIR
from pepper_mcp.robot.connection import RobotError

MAX_BODY = 16 * 1024

log = logging.getLogger(__name__)

def _ip_or_localhost(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def control_denied(client_ip: str, headers: Any, token: str, json_body: bool = True,
                   action: str | None = None) -> str | None:
    """Why a dashboard control request must be refused, or ``None`` when it may run.

    The preview server listens on the LAN so the tablet and phones can watch, but driving the
    robot is limited to this machine unless a token is configured. Requiring a JSON body and a
    same-origin ``Origin`` stops other web pages open in the browser from posting actions
    (cross-origin JSON needs a CORS preflight, which this server never grants); requiring the
    ``Host`` to be an IP or ``localhost`` stops DNS rebinding (a hostile domain re-resolved to
    127.0.0.1 would otherwise count as same-origin). ``stop`` only ever makes the robot safer,
    so any device that passes those checks may send it.
    """
    host = urlparse("//" + (headers.get("Host") or "")).hostname or ""
    if not _ip_or_localhost(host):
        return "Host must be an IP address or localhost"
    if json_body and not (headers.get("Content-Type") or "").startswith("application/json"):
        return "Content-Type must be application/json"
    origin = headers.get("Origin")
    if origin and urlparse(origin).netloc != headers.get("Host"):
        return "cross-origin request refused"
    try:
        local = ipaddress.ip_address(client_ip).is_loopback
    except ValueError:
        local = False
    if local or action == "stop":
        return None
    if not token:
        return "controls are only allowed from the server machine; start pepper-mcp with --preview-token to allow other devices"
    if not hmac.compare_digest((headers.get("X-Pepper-Token") or "").encode(), token.encode()):
        return "missing or wrong dashboard token"
    return None


def _lan_ip(target_ip: str | None) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target_ip or "8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


class PreviewServer:
    def __init__(self, rt: Any, host: str = "0.0.0.0", port: int = 8765):
        self.rt = rt
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url: str | None = None
        self._sse_clients: list[queue.Queue] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        rt = self.rt
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # Bounds how long a slow client can hold a thread while sending a POST body. Streaming
            # responses are unaffected: the timeout only applies to reads.
            timeout = 10

            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = self.path.split("?")[0]
                try:
                    if path == "/":
                        self._send(200, dashboard.html(), "text/html; charset=utf-8")
                    elif path == "/api/state":
                        out = dashboard.state(rt)
                        out["control"] = server._denied(self, json_body=False) is None
                        self._send(200, json.dumps(out, default=str).encode(), "application/json")
                    elif path == "/status":
                        self._send(200, json.dumps(rt.status(), default=str).encode(), "application/json")
                    elif path == "/events":
                        server._sse(self)
                    elif path == "/audio.wav":
                        server._audio(self)
                    elif path.startswith("/camera/") and path.endswith(".mjpg"):
                        server._mjpeg(self, path[len("/camera/"):-5])
                    elif path.startswith("/camera/") and path.endswith(".jpg"):
                        if rt.cameras is None:
                            self._send(503, b"camera disabled", "text/plain")
                            return
                        data, _ = rt.cameras.snapshot(path[len("/camera/"):-4], 640)
                        self._send(200, data, "image/jpeg")
                    elif path.startswith("/assets/"):
                        name = os.path.basename(path[len("/assets/"):])
                        fpath = os.path.join(ASSETS_DIR, name)
                        if not os.path.isfile(fpath):
                            self._send(404, b"not found", "text/plain")
                            return
                        ctype = {".gif": "image/gif", ".html": "text/html", ".mp3": "audio/mpeg",
                                 ".png": "image/png", ".jpg": "image/jpeg"}.get(os.path.splitext(name)[1], "application/octet-stream")
                        with open(fpath, "rb") as fh:
                            self._send(200, fh.read(), ctype)
                    else:
                        self._send(404, b"not found", "text/plain")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as exc:
                    log.debug("preview request %s failed: %s", path, exc)
                    try:
                        self._send(500, str(exc).encode(), "text/plain")
                    except Exception:
                        pass

            def do_POST(self) -> None:
                path = self.path.split("?")[0]
                if not path.startswith("/api/action/"):
                    self._send(404, b"not found", "text/plain")
                    return
                denied = server._denied(self, json_body=True)
                if denied:
                    self._send(403, json.dumps({"error": denied}).encode(), "application/json")
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if not 0 <= length <= MAX_BODY:
                        raise dashboard.ActionError("invalid request body length")
                    args = json.loads(self.rfile.read(length) or b"{}") if length else {}
                    result = dashboard.run_action(rt, path[len("/api/action/"):], args)
                    self._send(200, json.dumps({"ok": True, "result": result}, default=str).encode(),
                               "application/json")
                except (dashboard.ActionError, json.JSONDecodeError, ValueError) as exc:
                    self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                except RobotError as exc:
                    self._send(409, json.dumps({"error": str(exc)}).encode(), "application/json")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as exc:
                    log.warning("dashboard action %s failed: %s", path, exc)
                    self._send(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(),
                               "application/json")

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="preview-http", daemon=True)
        self._thread.start()
        ip = _lan_ip(getattr(self.rt.conn, "ip", None) if not self.rt.settings.fake else None)
        self.base_url = f"http://{ip}:{self.port}"
        self.rt.bus.add_sync_hook(self._fanout)
        log.info("preview server at %s", self.base_url)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def _denied(self, handler: BaseHTTPRequestHandler, json_body: bool) -> str | None:
        action = handler.path.split("?")[0].removeprefix("/api/action/") if json_body else None
        return control_denied(handler.client_address[0], handler.headers, self.rt.settings.preview_token,
                              json_body, action)

    # ------------------------------------------------------------- streams
    def _fanout(self, event: Any) -> None:
        with self._lock:
            clients = list(self._sse_clients)
        for q in clients:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def _sse(self, handler: BaseHTTPRequestHandler) -> None:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._sse_clients.append(q)
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        try:
            for e in self.rt.bus.recent(0, None, 30):
                handler.wfile.write(f"data: {json.dumps(e.to_dict(), default=str)}\n\n".encode())
            handler.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=15)
                    handler.wfile.write(f"data: {json.dumps(event.to_dict(), default=str)}\n\n".encode())
                except queue.Empty:
                    handler.wfile.write(b": keepalive\n\n")
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self._lock:
                if q in self._sse_clients:
                    self._sse_clients.remove(q)

    def _mjpeg(self, handler: BaseHTTPRequestHandler, camera: str) -> None:
        if self.rt.cameras is None:
            handler.send_response(503)
            handler.end_headers()
            return
        stream = self.rt.cameras.get(camera)
        handler.send_response(200)
        handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        last_seq = -1
        try:
            while True:
                frame = stream.wait_for_frame(2.0)
                if frame is None or frame.seq == last_seq:
                    time.sleep(0.02)
                    continue
                last_seq = frame.seq
                data, _ = stream.jpeg(640)
                handler.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                                    str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                handler.wfile.flush()
                time.sleep(max(0.0, 1.0 / stream.fps - 0.01))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _audio(self, handler: BaseHTTPRequestHandler) -> None:
        mic = self.rt.mic
        if mic is None:
            handler.send_response(503)
            handler.end_headers()
            return
        q: queue.Queue = queue.Queue(maxsize=100)
        remove = mic.add_consumer(lambda samples, rate, ts: q.put_nowait(samples) if not q.full() else None)
        handler.send_response(200)
        handler.send_header("Content-Type", "audio/wav")
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        # streaming WAV header with "infinite" length
        header = b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " + struct.pack(
            "<IHHIIHH", 16, 1, 1, mic.rate, mic.rate * 2, 2, 16) + b"data" + struct.pack("<I", 0xFFFFFFFF)
        try:
            handler.wfile.write(header)
            while True:
                try:
                    samples = q.get(timeout=5)
                except queue.Empty:
                    continue
                handler.wfile.write(samples.astype("<i2").tobytes())
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            remove()
