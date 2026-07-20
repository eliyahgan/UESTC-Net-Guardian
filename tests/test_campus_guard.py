import logging
import threading
import time
import unittest

from campus_guard import CampusGuard
from uestc_srun_autoconnect import AuthResult


class FakeSettings:
    wait_time = 15
    max_retry_wait = 300


class FakeSession:
    def close(self):
        pass


class FakeClient:
    def __init__(self, online=False, result=None):
        self.settings = FakeSettings()
        self.session = FakeSession()
        self._online = online
        self._result = result or AuthResult(True, "ok")
        self.authenticate_calls = 0

    def is_online(self):
        return self._online

    def authenticate(self):
        self.authenticate_calls += 1
        return self._result


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


if __name__ == "__main__":
    unittest.main()
