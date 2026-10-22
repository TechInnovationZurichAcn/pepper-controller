"""Connection to NAOqi through libqi-python.

Why a ``qi.Application`` and not a bare ``qi.Session``: the robot has to call back into
this process (audio buffers via ``processRemote``, ALMemory signals). That only works
when our session *listens* on a TCP endpoint, which ``--qi-listen-url`` provides.

The class also hides the two qi conventions the rest of the code relies on:

* ``service.method(*args, _async=True)`` returns a ``qi.Future`` – see :meth:`call_async`
  and :meth:`await_future` (bridges a qi future into asyncio).
* ``session.registerService(name, obj)`` exposes a Python object to the robot.

``qi`` is imported lazily so that the fake robot (tests, ``--fake``) runs on Python
versions that have no libqi wheels.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


class RobotError(RuntimeError):
    """Raised for robot-side failures that should be reported to the LLM as tool errors."""


class ServiceUnavailable(RobotError):
    """The NAOqi service does not exist on this firmware (feature disabled)."""


class RobotConnection:
    """Owns the qi application/session and a cache of service proxies."""

    def __init__(self, ip: str, port: int = 9559, listen_url: str = "tcp://0.0.0.0:0"):
        self.ip = ip
        self.port = port
        self.listen_url = listen_url
        self.url = f"tcp://{ip}:{port}"
        self._app = None
        self._session = None
        self._services: dict[str, Any] = {}
        self._missing: set[str] = set()
        self._lock = threading.RLock()
        self._registered: dict[str, Any] = {}
        self._on_disconnect: list[Callable[[], None]] = []
        self.connected_at: float | None = None

    # ------------------------------------------------------------------ lifecycle
    def connect(self, timeout_s: float = 10.0) -> None:
        import qi

        args = ["pepper-mcp", f"--qi-url={self.url}"]
        if self.listen_url:
            args.append(f"--qi-listen-url={self.listen_url}")
        deadline = time.monotonic() + timeout_s
        last_exc: Exception | None = None
        while True:
            try:
                self._app = qi.Application(args)
                self._app.start()
                self._session = self._app.session
                break
            except Exception as exc:  # qi raises RuntimeError on connection failure
                last_exc = exc
                self._app = None
                if time.monotonic() >= deadline:
                    raise RobotError(
                        f"cannot reach NAOqi at {self.url}: {exc}. Same Wi-Fi? Right IP? "
                        "Port 9559 blocked by client isolation?"
                    ) from exc
                time.sleep(1.0)
        self.connected_at = time.time()
        try:
            self._session.disconnected.connect(self._handle_disconnected)
        except Exception as exc:  # pragma: no cover - depends on qi build
            log.debug("could not hook disconnected signal: %s", exc)
        log.info("connected to NAOqi at %s (listening on %s)", self.url, self.endpoints())
        if last_exc:
            log.info("connected after retry (%s)", last_exc)

    def close(self) -> None:
        with self._lock:
            for name in list(self._registered):
                try:
                    self._session.unregisterService(self._registered.pop(name))
                except Exception:
                    pass
            self._services.clear()
            if self._app is not None:
                try:
                    self._app.stop()
                except Exception:
                    pass
            self._app = None
            self._session = None

    def _handle_disconnected(self, *_: Any) -> None:
        log.warning("NAOqi session disconnected")
        with self._lock:
            self._services.clear()
        for cb in list(self._on_disconnect):
            try:
                cb()
            except Exception as exc:
                log.debug("disconnect callback failed: %s", exc)

    def on_disconnect(self, callback: Callable[[], None]) -> None:
        self._on_disconnect.append(callback)

    @property
    def session(self):
        if self._session is None:
            raise RobotError("not connected to the robot")
        return self._session

    def is_connected(self) -> bool:
        try:
            return bool(self._session is not None and self._session.isConnected())
        except Exception:
            return False

    def endpoints(self) -> list[str]:
        try:
            return list(self.session.endpoints())
        except Exception:
            return []

    # ------------------------------------------------------------------ services
    def service(self, name: str) -> Any:
        """Return a cached proxy for NAOqi service ``name`` or raise ServiceUnavailable."""
        with self._lock:
            proxy = self._services.get(name)
            if proxy is not None:
                return proxy
            if name in self._missing:
                raise ServiceUnavailable(f"{name} is not available on this robot")
            try:
                proxy = self.session.service(name)
            except Exception as exc:
                self._missing.add(name)
                raise ServiceUnavailable(f"{name} is not available on this robot: {exc}") from exc
            self._services[name] = proxy
            return proxy

    def has_service(self, name: str) -> bool:
        try:
            self.service(name)
            return True
        except ServiceUnavailable:
            return False

    def try_service(self, name: str) -> Any | None:
        try:
            return self.service(name)
        except ServiceUnavailable:
            return None

    def service_names(self) -> list[str]:
        try:
            infos = self.session.services()
        except Exception as exc:
            raise RobotError(f"cannot list services: {exc}") from exc
        names = []
        for info in infos:
            if isinstance(info, dict):
                names.append(info.get("name"))
            else:
                try:
                    names.append(info[1])
                except Exception:
                    names.append(str(info))
        return sorted(n for n in names if n)

    def register_service(self, name: str, obj: Any) -> int:
        """Expose ``obj`` to the robot (needed for ALAudioDevice callbacks)."""
        with self._lock:
            if name in self._registered:
                try:
                    self.session.unregisterService(self._registered[name])
                except Exception:
                    pass
            service_id = self.session.registerService(name, obj)
            self._registered[name] = service_id
            return service_id

    def unregister_service(self, name: str) -> None:
        with self._lock:
            service_id = self._registered.pop(name, None)
            if service_id is not None:
                try:
                    self.session.unregisterService(service_id)
                except Exception:
                    pass

    # ------------------------------------------------------------------ futures
    @staticmethod
    def call_async(proxy: Any, method: str, *args: Any) -> Any:
        """``proxy.method(*args, _async=True)`` → qi.Future (falls back to a thread)."""
        fn = getattr(proxy, method)
        try:
            return fn(*args, _async=True)
        except TypeError:
            # Fake services or odd builds: emulate a future with a thread.
            return _ThreadFuture(fn, args)

    @staticmethod
    def future_result(future: Any, timeout_s: float | None = None) -> Any:
        """Block on a qi future (or our thread future) and return its value."""
        if timeout_s is not None:
            # qi.Future.wait returns a FutureState (not a bool); rely on isFinished instead.
            future.wait(int(timeout_s * 1000))
            if not future.isFinished():
                raise TimeoutError("robot call timed out")
        if future.hasError():
            raise RobotError(str(future.error()))
        return future.value()

    @staticmethod
    async def await_future(future: Any, timeout_s: float | None = None) -> Any:
        """Bridge a qi future into asyncio without blocking the event loop."""
        loop = asyncio.get_running_loop()
        done: asyncio.Future = loop.create_future()

        def _cb(fut: Any) -> None:
            def _set() -> None:
                if done.done():
                    return
                try:
                    if fut.hasError():
                        done.set_exception(RobotError(str(fut.error())))
                    else:
                        done.set_result(fut.value())
                except Exception as exc:  # pragma: no cover
                    done.set_exception(exc)

            loop.call_soon_threadsafe(_set)

        future.addCallback(_cb)
        if timeout_s is None:
            return await done
        return await asyncio.wait_for(done, timeout_s)


class _ThreadFuture:
    """Minimal qi.Future look-alike backed by a thread (used by the fake robot)."""

    def __init__(self, fn: Callable[..., Any], args: tuple[Any, ...]):
        self._value: Any = None
        self._error: str | None = None
        self._done = threading.Event()
        self._callbacks: list[Callable[[Any], None]] = []
        self._cancelled = False
        threading.Thread(target=self._run, args=(fn, args), daemon=True).start()

    def _run(self, fn: Callable[..., Any], args: tuple[Any, ...]) -> None:
        try:
            self._value = fn(*args)
        except Exception as exc:
            self._error = str(exc)
        self._done.set()
        for cb in self._callbacks:
            try:
                cb(self)
            except Exception:
                pass

    # qi.Future API subset
    def wait(self, timeout_ms: int | None = None) -> bool:
        return self._done.wait(None if timeout_ms is None else timeout_ms / 1000.0)

    def isFinished(self) -> bool:
        return self._done.is_set()

    def hasError(self) -> bool:
        self._done.wait()
        return self._error is not None

    def error(self) -> str:
        return self._error or ""

    def value(self) -> Any:
        self._done.wait()
        if self._error is not None:
            raise RobotError(self._error)
        return self._value

    def cancel(self) -> None:
        self._cancelled = True

    def addCallback(self, cb: Callable[[Any], None]) -> None:
        if self._done.is_set():
            cb(self)
        else:
            self._callbacks.append(cb)
