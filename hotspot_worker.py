from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, Optional

from hotspot_guard import HotspotResult, ensure_once


ResultCallback = Callable[[HotspotResult], None]


class HotspotGuardWorker:
    """Run the one-shot WinRT hotspot guard on a dedicated MTA thread."""

    def __init__(
        self,
        logger: logging.Logger,
        interval: int = 10,
        result_callback: ResultCallback | None = None,
        ensure_function=ensure_once,
    ):
        self.logger = logger
        self.interval = max(5, int(interval))
        self._result_callback = result_callback or (lambda result: None)
        self._ensure_function = ensure_function
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        self._enabled = False
        self._thread: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="MobileHotspotGuard",
            daemon=True,
        )
        self._thread.start()

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
        self._wake_event.set()

    def check_now(self) -> None:
        self._wake_event.set()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _wait(self, seconds: float) -> None:
        self._wake_event.wait(max(0.0, seconds))
        self._wake_event.clear()

    def _run(self) -> None:
        # Consume a possible enable signal issued before the new thread began;
        # the enabled flag itself is authoritative for the first cycle.
        self._wake_event.clear()
        apartment_initialized = False
        try:
            from winrt.runtime import ApartmentType, init_apartment

            init_apartment(ApartmentType.MULTI_THREADED)
            apartment_initialized = True
        except Exception as exc:
            self.logger.warning("初始化 Windows Runtime MTA 失败，将继续尝试: %s", exc)

        failures = 0
        try:
            while not self._stop_event.is_set():
                if not self.enabled:
                    self._wait(3600)
                    continue

                try:
                    result = asyncio.run(self._ensure_function())
                except Exception as exc:
                    result = HotspotResult(
                        status="error",
                        message=f"Hotspot worker failed: {type(exc).__name__}: {exc}",
                    )
                self._result_callback(result)

                if result.status in {"on", "started", "transition"}:
                    failures = 0
                    delay = 3 if result.status == "transition" else self.interval
                    if result.status == "started":
                        self.logger.info("Windows 移动热点已自动恢复")
                else:
                    failures += 1
                    delay = min(self.interval * (2 ** min(failures - 1, 3)), 60)
                    self.logger.error(
                        "热点守护检查失败: status=%s message=%s；%ss 后重试",
                        result.status,
                        result.message,
                        delay,
                    )
                if self.enabled:
                    self._wake_event.clear()
                self._wait(delay)
        finally:
            if apartment_initialized:
                try:
                    from winrt.runtime import uninit_apartment

                    uninit_apartment()
                except Exception:
                    pass
