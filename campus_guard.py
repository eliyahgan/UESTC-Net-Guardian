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

    ROUTINE_RETRY_CAP = 60

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
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._publish_lock = threading.RLock()
        self._wake_generation = 0
        self._reset_failures_requested = False
        self._recreate_client_requested = False
        self._enabled = False
        self._fatal_halted = False
        self._fatal_message = ""
        self._last_result_message = ""
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
        enabled = bool(enabled)
        with self._condition:
            changed = self._enabled != enabled
            self._enabled = enabled
            if changed:
                self._fatal_halted = False
                self._fatal_message = ""
                self._reset_failures_requested = True
                self._recreate_client_requested = True
                self._wake_generation += 1
                self._condition.notify_all()
        if not enabled:
            self._publish("disabled")

    def check_now(self) -> None:
        with self._condition:
            self._fatal_halted = False
            self._fatal_message = ""
            self._reset_failures_requested = True
            self._recreate_client_requested = True
            self._wake_generation += 1
            self._condition.notify_all()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop_event.set()
        with self._condition:
            self._wake_generation += 1
            self._condition.notify_all()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._dispose_client()

    def _publish(self, code: str, message: str = "") -> None:
        with self._publish_lock:
            self._status_callback(CampusStatus(code, message[:300]))

    def _publish_if_current(
        self,
        expected_generation: Optional[int],
        code: str,
        message: str = "",
    ) -> bool:
        if expected_generation is None:
            self._publish(code, message)
            return True
        # Serialize callbacks, but never run external UI code while holding
        # the supervisor's condition lock.  The lock ordering guarantees that
        # a concurrent disable either invalidates this result first or emits
        # its final "disabled" status after this callback.
        with self._publish_lock:
            with self._condition:
                if (
                    self._stop_event.is_set()
                    or not self._enabled
                    or self._wake_generation != expected_generation
                ):
                    return False
            self._status_callback(CampusStatus(code, message[:300]))
            return True

    def _wait(self, seconds: float, observed_generation: int) -> None:
        """Wait unless a newer control request arrived.

        A monotonically increasing generation avoids the Event.clear race
        where an "立即检查" click during an in-flight request could be lost.
        """
        with self._condition:
            self._condition.wait_for(
                lambda: (
                    self._stop_event.is_set()
                    or self._wake_generation != observed_generation
                ),
                timeout=max(0.0, seconds),
            )

    def _consume_control_requests(self) -> tuple[bool, bool, bool, bool, int]:
        with self._condition:
            enabled = self._enabled
            fatal_halted = self._fatal_halted
            reset_failures = self._reset_failures_requested
            recreate_client = self._recreate_client_requested
            self._reset_failures_requested = False
            self._recreate_client_requested = False
            generation = self._wake_generation
        return enabled, fatal_halted, reset_failures, recreate_client, generation

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

    def _cycle_is_current(self, expected_generation: Optional[int]) -> bool:
        if expected_generation is None:
            return True
        with self._condition:
            return (
                not self._stop_event.is_set()
                and self._enabled
                and self._wake_generation == expected_generation
            )

    def ensure_once(self, expected_generation: Optional[int] = None) -> tuple[str, int]:
        """Run one check/authentication cycle and return status plus next delay."""
        client = self._ensure_client()
        if not self._publish_if_current(expected_generation, "checking"):
            return "superseded", 0
        online = client.is_online()
        if online:
            self._last_result_message = ""
            if not self._publish_if_current(expected_generation, "online"):
                return "superseded", 0
            return "online", client.settings.wait_time

        if not self._publish_if_current(expected_generation, "authenticating"):
            return "superseded", 0
        result = client.authenticate()
        if not self._cycle_is_current(expected_generation):
            return "superseded", 0
        if result.success:
            self._last_result_message = ""
            if not self._publish_if_current(
                expected_generation,
                "online",
                "自动认证成功",
            ):
                return "superseded", 0
            self.logger.info("校园网自动认证成功")
            return "online", client.settings.wait_time
        self._last_result_message = result.message
        if result.fatal:
            if not self._publish_if_current(
                expected_generation,
                "fatal",
                result.message,
            ):
                return "superseded", 0
            self.logger.critical(
                "校园网认证被门户拒绝，已暂停校园网守护以避免账号锁定: %s",
                result.message,
            )
            return "fatal", result.retry_after or client.settings.max_retry_wait

        if not self._publish_if_current(
            expected_generation,
            "error",
            result.message,
        ):
            return "superseded", 0
        self.logger.error("校园网自动认证失败: %s", result.message)
        return "error", result.retry_after

    def _probe_while_fatal(self, expected_generation: int) -> tuple[bool, int]:
        """Probe online state without submitting credentials while halted."""
        client = self._ensure_client()
        delay = client.settings.wait_time
        if not client.is_online():
            return False, delay

        with self._publish_lock:
            with self._condition:
                if (
                    self._stop_event.is_set()
                    or not self._enabled
                    or self._wake_generation != expected_generation
                ):
                    return False, 0
                self._fatal_halted = False
                self._fatal_message = ""
            self._status_callback(CampusStatus("online", "已检测到手动登录"))
        self._last_result_message = ""
        self.logger.info("检测到校园网已由其他方式登录，校园网守护已恢复")
        return True, delay

    def _run(self) -> None:
        failures = 0
        self._publish("disabled")
        while not self._stop_event.is_set():
            (
                enabled,
                fatal_halted,
                reset_failures,
                recreate_client,
                generation,
            ) = self._consume_control_requests()
            if reset_failures:
                failures = 0
            if recreate_client:
                self._dispose_client()

            if not enabled:
                self._dispose_client()
                self._wait(3600, generation)
                continue

            if fatal_halted:
                try:
                    recovered, delay = self._probe_while_fatal(generation)
                    if recovered:
                        failures = 0
                except Exception as exc:
                    delay = self._client.settings.wait_time if self._client else 15
                    self.logger.warning(
                        "认证暂停期间的在线状态检查失败，将继续只读探测: %s",
                        exc,
                    )
                    self._dispose_client()
                self._wait(delay, generation)
                continue

            try:
                code, requested_delay = self.ensure_once(generation)
                if code == "superseded":
                    delay = 0
                elif code == "online":
                    failures = 0
                    delay = requested_delay
                elif code == "fatal":
                    with self._condition:
                        # A newer check/toggle request must win over the fatal
                        # result from a request that was already in flight.
                        if self._wake_generation == generation:
                            self._fatal_halted = True
                            self._fatal_message = self._last_result_message
                    # Keep polling the read-only online-state endpoint so a
                    # manual login recovers the tray without another attempt.
                    delay = self._client.settings.wait_time if self._client else 15
                else:
                    failures += 1
                    settings = (
                        self._client.settings if self._client else self._settings_factory()
                    )
                    if requested_delay > 0:
                        # Portal-mandated cooling-off periods (for example the
                        # five-minute E2533 lockout) are safety bounds, not
                        # ordinary backoff, and must not be shortened.
                        delay = requested_delay
                    else:
                        delay = min(
                            settings.wait_time * (2 ** min(failures - 1, 8)),
                            settings.max_retry_wait,
                            self.ROUTINE_RETRY_CAP,
                        )
                    self.logger.error(
                        "校园网守护检查失败: %s；%ss 后重试",
                        self._last_result_message or "门户未提供原因",
                        delay,
                    )
                    # Network transitions can invalidate cookies and the
                    # discovered portal context.  Start the next attempt with
                    # a fresh session and freshly loaded settings.
                    self._dispose_client()
            except ValueError as exc:
                if self._publish_if_current(generation, "config_error", str(exc)):
                    self.logger.error("校园网凭据未配置: %s", exc)
                self._dispose_client()
                delay = 60
            except Exception as exc:
                failures += 1
                current = self._publish_if_current(generation, "error", str(exc))
                settings = self._client.settings if self._client else None
                wait_time = settings.wait_time if settings else 15
                max_retry_wait = settings.max_retry_wait if settings else 300
                delay = min(
                    wait_time * (2 ** min(failures - 1, 8)),
                    max_retry_wait,
                    self.ROUTINE_RETRY_CAP,
                )
                if current:
                    self.logger.error(
                        "校园网守护异常已隔离: %s；%ss 后重试",
                        exc,
                        delay,
                    )
                else:
                    delay = 0
                self._dispose_client()
            self._wait(delay, generation)

        self._dispose_client()
        self._publish("stopped")
