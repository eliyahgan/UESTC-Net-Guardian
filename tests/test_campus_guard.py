from collections import deque
import io
import logging
import queue
import threading
import time
import unittest

from campus_guard import CampusGuard
from uestc_srun_autoconnect import AuthResult, RedactingFilter


class FakeSettings:
    wait_time = 15
    max_retry_wait = 300


class ShortMaxRetrySettings:
    wait_time = 15
    max_retry_wait = 60


class FakeSession:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


class FakeClient:
    def __init__(self, online=False, result=None):
        self.settings = FakeSettings()
        self.session = FakeSession()
        self._online = online
        self._result = result or AuthResult(True, "ok")
        self.online_calls = 0
        self.authenticate_calls = 0
        self.online_called = threading.Event()
        self.authenticate_called = threading.Event()

    def is_online(self):
        self.online_calls += 1
        self.online_called.set()
        return self._online

    def authenticate(self):
        self.authenticate_calls += 1
        self.authenticate_called.set()
        return self._result


class SequenceClient(FakeClient):
    def __init__(self, online_values, result=None):
        super().__init__(online=False, result=result)
        self._online_values = deque(online_values)

    def is_online(self):
        self.online_calls += 1
        self.online_called.set()
        if self._online_values:
            self._online = self._online_values.popleft()
        return self._online


class BlockingFailureClient(FakeClient):
    def __init__(self, started, release, result=None):
        super().__init__(
            online=False,
            result=result or AuthResult(False, "temporary portal failure"),
        )
        self._started = started
        self._release = release

    def authenticate(self):
        self.authenticate_calls += 1
        self.authenticate_called.set()
        self._started.set()
        if not self._release.wait(3):
            raise TimeoutError("test did not release the blocked authentication")
        return self._result


class BlockingFatalProbeClient(FakeClient):
    def __init__(self, probe_started, release_probe):
        super().__init__(
            online=False,
            result=AuthResult(False, "E2553: bad credentials", fatal=True),
        )
        self._probe_started = probe_started
        self._release_probe = release_probe

    def is_online(self):
        self.online_calls += 1
        self.online_called.set()
        if self.online_calls == 1:
            return False
        self._probe_started.set()
        if not self._release_probe.wait(3):
            raise TimeoutError("test did not release the fatal-state probe")
        return True


class StopAfterWaitsGuard(CampusGuard):
    """Run the supervisor synchronously without real sleeps."""

    def __init__(self, *args, stop_after=1, stop_when=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.waits = []
        self._stop_after = stop_after
        self._stop_when = stop_when

    def _wait(self, seconds, *args, **kwargs):
        self.waits.append(seconds)
        should_stop = self._stop_when and self._stop_when()
        if should_stop or len(self.waits) >= self._stop_after:
            self._stop_event.set()


class RecordingWaitGuard(CampusGuard):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.waits = queue.Queue()

    def _wait(self, seconds, *args, **kwargs):
        self.waits.put(seconds)
        return super()._wait(seconds, *args, **kwargs)


class BackoffControlGuard(CampusGuard):
    """Let two failures run, then expose the second wait to check_now()."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.waits = []
        self.second_waiting = threading.Event()
        self.third_wait_recorded = threading.Event()

    def _wait(self, seconds, *args, **kwargs):
        self.waits.append(seconds)
        wait_number = len(self.waits)
        if wait_number == 1:
            return
        if wait_number == 2:
            self.second_waiting.set()
            return super()._wait(seconds, *args, **kwargs)
        self.third_wait_recorded.set()
        self._stop_event.set()


class FirstWaitImmediateGuard(CampusGuard):
    """Skip only the first delay so a fatal read-only probe starts immediately."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.waits = queue.Queue()
        self._wait_count = 0

    def _wait(self, seconds, *args, **kwargs):
        self._wait_count += 1
        self.waits.put(seconds)
        if self._wait_count == 1:
            return
        return super()._wait(seconds, *args, **kwargs)


class CampusGuardTests(unittest.TestCase):
    def make_guard(self, client, statuses):
        return CampusGuard(
            logging.getLogger("campus-guard-test"),
            statuses.append,
            settings_factory=lambda: FakeSettings(),
            client_factory=lambda settings, logger: client,
        )

    def test_online_cycle_does_not_authenticate(self):
        statuses = []
        client = FakeClient(online=True)
        guard = self.make_guard(client, statuses)

        code, delay = guard.ensure_once()

        self.assertEqual(code, "online")
        self.assertEqual(delay, 15)
        self.assertEqual(client.authenticate_calls, 0)
        self.assertEqual(statuses[-1].code, "online")

    def test_status_callback_can_read_enabled_without_deadlock(self):
        statuses = []
        enabled_values = []
        outcome = []
        holder = {}

        def status_callback(status):
            statuses.append(status)
            enabled_values.append(holder["guard"].enabled)

        client = FakeClient(online=True)
        guard = CampusGuard(
            logging.getLogger("campus-callback-lock-test"),
            status_callback,
            settings_factory=lambda: FakeSettings(),
            client_factory=lambda settings, logger: client,
        )
        holder["guard"] = guard
        guard.set_enabled(True)
        with guard._lock:
            generation = guard._wake_generation

        def run_cycle():
            try:
                outcome.append(guard.ensure_once(generation))
            except BaseException as exc:  # Preserve worker failures for the assertion.
                outcome.append(exc)

        worker = threading.Thread(target=run_cycle, daemon=True)
        worker.start()
        worker.join(1)

        self.assertFalse(
            worker.is_alive(),
            "status callback deadlocked while reading guard.enabled",
        )
        self.assertEqual(outcome, [("online", 15)])
        self.assertEqual([status.code for status in statuses], ["checking", "online"])
        self.assertEqual(enabled_values, [True, True])

    def test_offline_cycle_authenticates(self):
        statuses = []
        client = FakeClient(online=False, result=AuthResult(True, "ok"))
        guard = self.make_guard(client, statuses)

        code, delay = guard.ensure_once()

        self.assertEqual(code, "online")
        self.assertEqual(client.authenticate_calls, 1)
        self.assertEqual(statuses[-1].code, "online")

    def test_fatal_result_is_exposed_to_supervisor(self):
        statuses = []
        client = FakeClient(
            online=False,
            result=AuthResult(False, "bad credentials", fatal=True),
        )
        guard = self.make_guard(client, statuses)

        code, delay = guard.ensure_once()

        self.assertEqual(code, "fatal")
        self.assertEqual(delay, 300)
        self.assertEqual(statuses[-1].code, "fatal")

    def test_enable_race_does_not_duplicate_first_cycle(self):
        statuses = []
        client = FakeClient(online=True)
        guard = self.make_guard(client, statuses)
        guard.start()
        guard.set_enabled(True)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and client.authenticate_calls == 0 and not any(
                status.code == "online" for status in statuses
            ):
                threading.Event().wait(0.01)
            threading.Event().wait(0.2)
            # is_online is called once per cycle; the wait interval is 15 s.
            self.assertEqual(client.authenticate_calls, 0)
            self.assertEqual(sum(status.code == "online" for status in statuses), 1)
        finally:
            guard.stop()

    def test_explicit_retry_delay_is_not_exponentially_increased(self):
        statuses = []
        clients = [
            FakeClient(
                online=False,
                result=AuthResult(False, "E2532: too frequent", retry_after=10),
            )
            for _ in range(2)
        ]
        factory_calls = []

        def client_factory(settings, logger):
            client = clients[min(len(factory_calls), len(clients) - 1)]
            factory_calls.append(client)
            return client

        guard = StopAfterWaitsGuard(
            logging.getLogger("campus-explicit-retry-test"),
            statuses.append,
            settings_factory=lambda: FakeSettings(),
            client_factory=client_factory,
            stop_after=2,
        )
        guard.set_enabled(True)

        guard._run()

        self.assertEqual(guard.waits, [10, 10])

    def test_portal_cooldown_is_not_shortened_by_local_retry_cap(self):
        statuses = []
        client = FakeClient(
            online=False,
            result=AuthResult(False, "E2533: wait five minutes", retry_after=300),
        )
        client.settings = ShortMaxRetrySettings()
        guard = StopAfterWaitsGuard(
            logging.getLogger("campus-portal-cooldown-test"),
            statuses.append,
            settings_factory=lambda: ShortMaxRetrySettings(),
            client_factory=lambda settings, logger: client,
            stop_after=1,
        )
        guard.set_enabled(True)

        guard._run()

        self.assertEqual(guard.waits, [300])

    def test_check_now_during_authentication_is_not_lost(self):
        statuses = []
        first_started = threading.Event()
        release_first = threading.Event()
        first = BlockingFailureClient(first_started, release_first)
        second = FakeClient(
            online=False,
            result=AuthResult(False, "second temporary failure"),
        )
        clients = deque((first, second))

        def client_factory(settings, logger):
            return clients.popleft() if clients else second

        guard = RecordingWaitGuard(
            logging.getLogger("campus-wake-generation-test"),
            statuses.append,
            settings_factory=lambda: FakeSettings(),
            client_factory=client_factory,
        )
        guard.set_enabled(True)
        guard.start()
        try:
            self.assertTrue(first_started.wait(3), "first authentication never started")
            guard.check_now()
            release_first.set()
            self.assertTrue(
                second.authenticate_called.wait(3),
                "check_now issued during authentication was lost",
            )
            self.assertGreaterEqual(first.session.close_calls, 1)
        finally:
            release_first.set()
            guard.stop()

    def test_check_now_wins_over_stale_fatal_authentication_result(self):
        statuses = []
        online_status = threading.Event()
        first_started = threading.Event()
        release_first = threading.Event()
        first = BlockingFailureClient(
            first_started,
            release_first,
            result=AuthResult(False, "E2553: stale fatal result", fatal=True),
        )
        second = FakeClient(
            online=False,
            result=AuthResult(True, "fresh authentication succeeded"),
        )
        clients = deque((first, second))

        def record_status(status):
            statuses.append(status)
            if status.code == "online":
                online_status.set()

        def client_factory(settings, logger):
            return clients.popleft() if clients else second

        guard = RecordingWaitGuard(
            logging.getLogger("campus-stale-fatal-generation-test"),
            record_status,
            settings_factory=lambda: FakeSettings(),
            client_factory=client_factory,
        )
        guard.set_enabled(True)
        guard.start()
        try:
            self.assertTrue(first_started.wait(3), "fatal authentication never started")
            guard.check_now()
            release_first.set()
            self.assertTrue(
                second.authenticate_called.wait(3),
                "stale fatal result incorrectly paused the newer check request",
            )
            self.assertTrue(online_status.wait(3), "newer authentication did not finish")
            self.assertEqual(second.authenticate_calls, 1)
            self.assertGreaterEqual(first.session.close_calls, 1)
            with guard._lock:
                self.assertFalse(guard._fatal_halted)
        finally:
            release_first.set()
            guard.stop()

    def test_disable_wins_over_stale_inflight_authentication_result(self):
        statuses = []
        first_started = threading.Event()
        release_first = threading.Event()
        client = BlockingFailureClient(
            first_started,
            release_first,
            result=AuthResult(False, "E2553: stale fatal result", fatal=True),
        )
        guard = RecordingWaitGuard(
            logging.getLogger("campus-disable-generation-test"),
            statuses.append,
            settings_factory=lambda: FakeSettings(),
            client_factory=lambda settings, logger: client,
        )
        guard.set_enabled(True)
        guard.start()
        try:
            self.assertTrue(first_started.wait(3), "fatal authentication never started")
            guard.set_enabled(False)
            release_first.set()

            observed_waits = []
            while 3600 not in observed_waits:
                observed_waits.append(guard.waits.get(timeout=3))

            self.assertEqual(statuses[-1].code, "disabled")
            with guard._lock:
                self.assertFalse(guard._fatal_halted)
        finally:
            release_first.set()
            guard.stop()

    def test_disable_wins_over_stale_fatal_online_probe(self):
        statuses = []
        probe_started = threading.Event()
        release_probe = threading.Event()
        client = BlockingFatalProbeClient(probe_started, release_probe)
        guard = FirstWaitImmediateGuard(
            logging.getLogger("campus-disable-fatal-probe-generation-test"),
            statuses.append,
            settings_factory=lambda: FakeSettings(),
            client_factory=lambda settings, logger: client,
        )
        guard.set_enabled(True)
        guard.start()
        try:
            self.assertTrue(probe_started.wait(3), "fatal-state online probe never started")
            guard.set_enabled(False)
            disabled_index = len(statuses) - 1
            self.assertEqual(statuses[disabled_index].code, "disabled")
            release_probe.set()

            observed_waits = []
            while 3600 not in observed_waits:
                observed_waits.append(guard.waits.get(timeout=3))

            self.assertEqual(statuses[-1].code, "disabled")
            self.assertNotIn(
                "online",
                [status.code for status in statuses[disabled_index + 1:]],
            )
            with guard._lock:
                self.assertFalse(guard._fatal_halted)
        finally:
            release_probe.set()
            guard.stop()

    def test_check_now_resets_ordinary_failure_backoff(self):
        statuses = []

        def client_factory(settings, logger):
            return FakeClient(
                online=False,
                result=AuthResult(False, "ordinary retryable failure"),
            )

        guard = BackoffControlGuard(
            logging.getLogger("campus-reset-backoff-test"),
            statuses.append,
            settings_factory=lambda: FakeSettings(),
            client_factory=client_factory,
        )
        guard.set_enabled(True)
        guard.start()
        try:
            self.assertTrue(guard.second_waiting.wait(3), "second retry was not reached")
            guard.check_now()
            self.assertTrue(
                guard.third_wait_recorded.wait(3),
                "check_now did not wake the retry wait",
            )
            self.assertEqual(guard.waits[:3], [15, 30, 15])
        finally:
            guard.stop()

    def test_retryable_failure_rebuilds_client_before_next_cycle(self):
        statuses = []
        first = FakeClient(
            online=False,
            result=AuthResult(False, "stale portal session"),
        )
        second = FakeClient(online=True)
        clients = deque((first, second))
        factory_calls = []

        def client_factory(settings, logger):
            client = clients.popleft() if clients else second
            factory_calls.append(client)
            return client

        guard = StopAfterWaitsGuard(
            logging.getLogger("campus-rebuild-client-test"),
            statuses.append,
            settings_factory=lambda: FakeSettings(),
            client_factory=client_factory,
            stop_after=2,
        )
        guard.set_enabled(True)

        guard._run()

        self.assertEqual(factory_calls[:2], [first, second])
        self.assertGreaterEqual(first.session.close_calls, 1)
        self.assertIn("online", [status.code for status in statuses])

    def test_fatal_state_only_probes_until_manual_login_is_detected(self):
        statuses = []
        client = SequenceClient(
            (False, False, True),
            result=AuthResult(False, "E2553: bad credentials", fatal=True),
        )
        guard = StopAfterWaitsGuard(
            logging.getLogger("campus-fatal-read-only-test"),
            statuses.append,
            settings_factory=lambda: FakeSettings(),
            client_factory=lambda settings, logger: client,
            stop_after=5,
            stop_when=lambda: any(status.code == "online" for status in statuses),
        )
        guard.set_enabled(True)

        guard._run()

        self.assertEqual(client.authenticate_calls, 1)
        self.assertGreaterEqual(client.online_calls, 3)
        self.assertIn("online", [status.code for status in statuses])
        with guard._lock:
            self.assertFalse(guard._fatal_halted)

    def test_retry_log_contains_redacted_portal_reason(self):
        username = "private-student-id"
        password = "private-test-password"
        portal_reason = (
            f"E2532 portal rejected username={username} password={password}"
        )
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(RedactingFilter((username, password)))
        logger = logging.Logger("campus-redacted-reason-test")
        logger.addHandler(handler)
        logger.propagate = False

        client = FakeClient(
            online=False,
            result=AuthResult(False, portal_reason, retry_after=10),
        )
        guard = StopAfterWaitsGuard(
            logger,
            settings_factory=lambda: FakeSettings(),
            client_factory=lambda settings, candidate_logger: client,
            stop_after=1,
        )
        guard.set_enabled(True)

        guard._run()

        log_output = stream.getvalue()
        self.assertIn("E2532", log_output)
        self.assertIn("portal rejected", log_output)
        self.assertNotIn(username, log_output)
        self.assertNotIn(password, log_output)


if __name__ == "__main__":
    unittest.main()
