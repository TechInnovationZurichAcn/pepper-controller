"""Optional HTTP side channel for humans and non-MCP consumers.

* ``/``                 small live dashboard (camera + events + transcript)
* ``/camera/<cam>.mjpg`` live MJPEG stream (top/bottom/depth)
* ``/camera/<cam>.jpg``  single frame
* ``/audio.wav``         live 16-bit PCM WAV stream from the microphone
* ``/events``            server-sent events (every EventBus event as JSON)
* ``/status``            JSON status; ``/assets/<file>`` bundled assets (tablet GIFs)

Only the standard library is used so it never gets in the way of the MCP server.
"""

from __future__ import annotations

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

from pepper_mcp.robot.audio_out import ASSETS_DIR

log = logging.getLogger(__name__)

DASHBOARD = """<!doctype html><html><head><meta charset="utf-8"><title>Pepper live</title>
<style>body{font-family:system-ui,sans-serif;background:#0b1020;color:#eaf2ff;margin:0;display:grid;
grid-template-columns:1fr 1fr;gap:12px;padding:12px}img{width:100%;border-radius:8px;background:#000}
pre{background:#131a33;padding:8px;border-radius:8px;height:70vh;overflow:auto;font-size:12px}
h2{margin:4px 0;font-size:14px;color:#9fb3ff}</style></head><body>
<div><h2>top camera</h2><img src="/camera/top.mjpg"><h2>status</h2><pre id="s"></pre></div>
<div><h2>events</h2><pre id="e"></pre></div>
<script>
const e=document.getElementById('e');const es=new EventSource('/events');
es.onmessage=m=>{const d=JSON.parse(m.data);e.textContent=(new Date(d.ts*1000).toLocaleTimeString()+'  '+d.type+'  '+
JSON.stringify(d.data)+'\\n')+e.textContent;if(e.textContent.length>40000)e.textContent=e.textContent.slice(0,30000)};
async function st(){try{const r=await fetch('/status');document.getElementById('s').textContent=JSON.stringify(await r.json(),null,1)}catch(x){}}
st();setInterval(st,3000);
</script></body></html>"""


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
                        self._send(200, DASHBOARD.encode(), "text/html; charset=utf-8")
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
