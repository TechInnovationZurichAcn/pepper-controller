"""Camera streams (ALVideoDevice) with a lazily encoded JPEG cache.

One grabber thread per subscribed camera keeps the newest frame. JPEG encoding happens
on demand and is cached per frame, so the MCP snapshot tool, the resource and the MJPEG
preview share one encode. Frames are RGB from NAOqi (``kRGBColorSpace = 11``); the
depth camera (index 2) is requested as 16-bit depth (``kDepthColorSpace = 17``) and
rendered as a grey image for the LLM.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from pepper_mcp.robot.connection import RobotConnection, RobotError

log = logging.getLogger(__name__)

CAMERAS = {"top": 0, "bottom": 1, "depth": 2}
RESOLUTIONS = {"qqvga": 0, "qvga": 1, "vga": 2, "4vga": 3}
RES_DIMS = {0: (160, 120), 1: (320, 240), 2: (640, 480), 3: (1280, 960)}
COLOR_RGB = 11
COLOR_DEPTH = 17


@dataclass
class Frame:
    camera: str
    width: int
    height: int
    data: bytes
    colorspace: int
    ts: float
    seq: int

    @property
    def age_ms(self) -> int:
        return int((time.time() - self.ts) * 1000)


class CameraStream:
    def __init__(self, conn: RobotConnection, camera: str, resolution: str = "qvga", fps: int = 10,
                 jpeg_quality: int = 80):
        if camera not in CAMERAS:
            raise RobotError(f"unknown camera {camera!r}; choose from {sorted(CAMERAS)}")
        self.conn = conn
        self.camera = camera
        self.cam_id = CAMERAS[camera]
        self.resolution = RESOLUTIONS.get(resolution, 1)
        self.fps = max(1, min(30, int(fps)))
        self.jpeg_quality = jpeg_quality
        self.colorspace = COLOR_DEPTH if camera == "depth" else COLOR_RGB
        self._handle: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._frame: Frame | None = None
        self._jpeg_cache: tuple[int, int, int, bytes] | None = None  # (seq, max_width, quality, bytes)
        self._seq = 0
        self._frame_times: list[float] = []
        self.error: str | None = None
        self._cond = threading.Condition()

    @property
    def video(self) -> Any:
        return self.conn.service("ALVideoDevice")

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._subscribe()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"camera-{self.camera}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._unsubscribe()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _subscribe(self) -> None:
        name = f"pepper_mcp_{self.camera}"
        video = self.video
        # subscribeCamera hands out "<name>_<n>" handles and refuses new ones once a name has
        # 8, so handles leaked by killed runs must be found and released by their suffixed name.
        try:
            stale = [h for h in video.getSubscribers() if h == name or h.startswith(f"{name}_")]
        except Exception:
            stale = [name]
        for handle in stale:
            try:
                video.unsubscribe(handle)
            except Exception as exc:
                log.debug("could not release camera handle %s: %s", handle, exc)
        self._handle = video.subscribeCamera(name, self.cam_id, self.resolution, self.colorspace, self.fps)
        if not self._handle:
            raise RobotError(f"subscribeCamera failed for {self.camera}")
        self.error = None

    def _unsubscribe(self) -> None:
        if self._handle:
            try:
                self.video.unsubscribe(self._handle)
            except Exception:
                pass
            self._handle = None

    def set_fps(self, fps: int) -> None:
        self.fps = max(1, min(30, int(fps)))
        if self._handle:
            try:
                self.video.setFrameRate(self._handle, self.fps)
            except Exception as exc:
                log.debug("setFrameRate: %s", exc)

    # ------------------------------------------------------------- grabbing
    def _loop(self) -> None:
        period = 1.0 / self.fps
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                img = self.video.getImageRemote(self._handle)
            except Exception as exc:
                self.error = str(exc)
                self._stop.wait(0.5)
                continue
            if img is not None:
                try:
                    width, height, data = int(img[0]), int(img[1]), img[6]
                    raw = _to_bytes(data)
                    self._seq += 1
                    frame = Frame(self.camera, width, height, raw, self.colorspace, time.time(), self._seq)
                    with self._lock:
                        self._frame = frame
                        self._frame_times.append(frame.ts)
                        self._frame_times = self._frame_times[-30:]
                    with self._cond:
                        self._cond.notify_all()
                except Exception as exc:
                    self.error = f"bad frame: {exc}"
                finally:
                    try:
                        self.video.releaseImage(self._handle)
                    except Exception:
                        pass
            elapsed = time.monotonic() - t0
            self._stop.wait(max(0.0, period - elapsed))

    def latest(self) -> Frame | None:
        with self._lock:
            return self._frame

    def wait_for_frame(self, timeout_s: float = 2.0) -> Frame | None:
        frame = self.latest()
        if frame is not None:
            return frame
        with self._cond:
            self._cond.wait(timeout_s)
        return self.latest()

    def achieved_fps(self) -> float:
        with self._lock:
            times = list(self._frame_times)
        if len(times) < 2:
            return 0.0
        span = times[-1] - times[0]
        return round((len(times) - 1) / span, 1) if span > 0 else 0.0

    # ------------------------------------------------------------- encoding
    def jpeg(self, max_width: int | None = None, quality: int | None = None,
             timeout_s: float = 2.0) -> tuple[bytes, Frame]:
        frame = self.wait_for_frame(timeout_s)
        if frame is None:
            raise RobotError(f"no frame from {self.camera} camera yet ({self.error or 'waiting'})")
        quality = quality or self.jpeg_quality
        max_width = max_width or frame.width
        with self._lock:
            cached = self._jpeg_cache
        if cached and cached[0] == frame.seq and cached[1] == max_width and cached[2] == quality:
            return cached[3], frame
        data = encode_jpeg(frame, max_width, quality)
        with self._lock:
            self._jpeg_cache = (frame.seq, max_width, quality, data)
        return data, frame

    def status(self) -> dict[str, Any]:
        frame = self.latest()
        return {"camera": self.camera, "running": self.running, "requested_fps": self.fps,
                "achieved_fps": self.achieved_fps(), "resolution": RES_DIMS.get(self.resolution),
                "last_frame_age_ms": frame.age_ms if frame else None, "frames": self._seq,
                "error": self.error}


def _to_bytes(data: Any) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        return data.encode("latin-1")
    return bytes(data)


def encode_jpeg(frame: Frame, max_width: int, quality: int) -> bytes:
    from PIL import Image

    if frame.colorspace == COLOR_DEPTH:
        import numpy as np

        depth = np.frombuffer(frame.data, dtype="<u2")
        if depth.size != frame.width * frame.height:
            raise RobotError("depth frame size mismatch")
        depth = depth.reshape(frame.height, frame.width).astype("float32")
        # 0..4000 mm -> 255..0 (near = bright); 0 (no data) -> black
        img8 = np.where(depth > 0, np.clip(255 - depth / 4000.0 * 255, 0, 255), 0).astype("uint8")
        image = Image.fromarray(img8, "L")
    else:
        expected = frame.width * frame.height * 3
        if len(frame.data) != expected:
            raise RobotError(f"frame buffer {len(frame.data)} != {frame.width}x{frame.height}x3")
        image = Image.frombuffer("RGB", (frame.width, frame.height), frame.data, "raw", "RGB", 0, 1)
    if max_width and image.width > max_width:
        ratio = max_width / image.width
        image = image.resize((max_width, max(1, int(image.height * ratio))))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=int(max(30, min(95, quality))))
    return buf.getvalue()


class Cameras:
    """Manages the set of camera streams."""

    def __init__(self, conn: RobotConnection, resolution: str = "qvga", fps: int = 10, jpeg_quality: int = 80):
        self.conn = conn
        self.resolution = resolution
        self.fps = fps
        self.jpeg_quality = jpeg_quality
        self._streams: dict[str, CameraStream] = {}
        self._lock = threading.Lock()

    def get(self, camera: str = "top", start: bool = True) -> CameraStream:
        with self._lock:
            stream = self._streams.get(camera)
            if stream is None:
                res = "qvga" if camera == "depth" else self.resolution
                stream = CameraStream(self.conn, camera, res, self.fps, self.jpeg_quality)
                self._streams[camera] = stream
        if start and not stream.running:
            stream.start()
        return stream

    def set_stream(self, camera: str, enabled: bool, fps: int | None = None,
                   resolution: str | None = None) -> dict[str, Any]:
        stream = self.get(camera, start=False)
        if resolution and RESOLUTIONS.get(resolution, stream.resolution) != stream.resolution:
            stream.stop()
            stream.resolution = RESOLUTIONS[resolution]
        if fps:
            stream.set_fps(fps)
        if enabled and not stream.running:
            stream.start()
        elif not enabled and stream.running:
            stream.stop()
        return stream.status()

    def snapshot(self, camera: str = "top", max_width: int = 640, quality: int | None = None) -> tuple[bytes, Frame]:
        return self.get(camera).jpeg(max_width, quality)

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.status() for s in self._streams.values()]

    def stop(self) -> None:
        with self._lock:
            streams = list(self._streams.values())
        for s in streams:
            s.stop()
