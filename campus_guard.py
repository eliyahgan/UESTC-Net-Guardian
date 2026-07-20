from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from typing import Callable, Optional

from uestc_srun_autoconnect import Settings, SrunClient


@dataclass(frozen=True)
class CampusStatus:
    code: str
    message: str = ""


StatusCallback = Callable[[CampusStatus], None]


class CampusGuard:
    """Interruptible background supervisor around the existing Srun client."""

    def __init__(
        self,
        logger: logging.Logger,
        status_callback: StatusCallback | None = None,
        settings_factory: Callable[[], Settings] = Settings.from_environment,
        client_factory: Callable[[Settings, logging.Logger], SrunClient] = SrunClient,
    ):
        self.logger = logger
        self._status_callback = status_callback or (lambda status: None)
        self._settings_factory = settings_factory
        self._client_factory = client_factory
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        self._enabled = False
        self._fatal_halted = False
        self._thread: Optional[threading.Thread] = None
        self._client: Optional[SrunClient] = None

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="CampusNetworkGuard",
            daemon=True,
        )
        self._thread.start()

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            changed = self._enabled != bool(enabled)
            self._enabled = bool(enabled)
            if changed:
                self._fatal_halted = False
        self._wake_event.set()
        if not enabled:
            self._publish("disabled")

    def check_now(self) -> None:
        with self._lock:
            self._fatal_halted = False
        self._wake_event.set()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._dispose_client()

    def _publish(self, code: str, message: str = "") -> None:
        self._status_callback(CampusStatus(code, message[:300]))

    def _wait(self, seconds: float) -> None:
        self._wake_event.wait(max(0.0, seconds))
        self._wake_event.clear()

    def _dispose_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.session.close()
            except Exception:
                pass

    def _ensure_client(self) -> SrunClient:
        if self._client is None:
            settings = self._settings_factory()
            self._client = self._client_factory(settings, self.logger)
        return self._client

    def ensure_once(self) -> tuple[str, int]:
        """Run one check/authentication cycle and return status plus next delay."""
        client = self._ensure_client()
        self._publish("checking")
        if client.is_online():
            self._publish("online")
            return "online", client.settings.wait_time

        self._publish("authenticating")
        result = client.authenticate()
        if result.success:
            self._publish("online", "自动认证成功")
            self.logger.info("校园网自动认证成功")
            return "online", client.settings.wait_time
        if result.fatal:
            self._publish("fatal", result.message)
            self.logger.critical(
                "校园网认证被门户拒绝，已暂停校园网守护以避免账号锁定: %s",
                result.message,
            )
            return "fatal", max(result.retry_after, client.settings.max_retry_wait)

        self._publish("error", result.message)
        return "error", max(result.retry_after, client.settings.wait_time)

    def _run(self) -> None:
        # ``set_enabled(True)`` can race with thread creation.  If the thread
        # has not reached its disabled wait yet, that wake signal would
        # otherwise remain set and cause an unintended second immediate cycle.
        self._wake_event.clear()
        failures = 0
        self._publish("disabled")
        while not self._stop_event.is_set():
            if not self.enabled:
                self._dispose_client()
                self._wait(3600)
                continue

            with self._lock:
                fatal_halted = self._fatal_halted
            if fatal_halted:
                self._wait(3600)
                continue

            try:
                code, requested_delay = self.ensure_once()
                if code == "online":
                    failures = 0
                    delay = requested_delay
                elif code == "fatal":
                    with self._lock:
                        self._fatal_halted = True
                    delay = requested_delay
                else:
                    failures += 1
                    settings = self._client.settings if self._client else self._settings_factory()
                    delay = min(
                        max(requested_delay, settings.wait_time * (2 ** min(failures - 1, 8))),
                        settings.max_retry_wait,
                    )
                    self.logger.error("校园网守护检查失败，%ss 后重试", delay)
            except ValueError as exc:
                self._publish("config_error", str(exc))
                self.logger.error("校园网凭据未配置: %s", exc)
                self._dispose_client()
                delay = 60
            except Exception as exc:
                failures += 1
                self._publish("error", str(exc))
                delay = min(15 * (2 ** min(failures - 1, 4)), 300)
                self.logger.error("校园网守护异常已隔离: %s；%ss 后重试", exc, delay)
                self._dispose_client()
            # A wake used to enable the worker may have arrived before the
            # first cycle started.  Do not let that stale signal turn into a
            # duplicate immediate authentication attempt.
            if self.enabled:
                self._wake_event.clear()
            self._wait(delay)

        self._dispose_client()
        self._publish("stopped")
