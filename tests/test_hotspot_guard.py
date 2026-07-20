import unittest

from hotspot_guard import (
    PyWinRTBackend,
    TetheringStartResult,
    ensure_once,
)


class FakeBackend:
    def __init__(
        self,
        *,
        state="on",
        profile=object(),
        supported=True,
        capability_name="ENABLED",
        start_result=None,
        client_count=1,
        max_client_count=8,
    ):
        self.state = state
        self.profile = profile
        self.supported = supported
        self.capability_name = capability_name
        self.start_result = start_result or TetheringStartResult(True, "SUCCESS")
        self.client_count = client_count
        self.max_client_count = max_client_count
        self.calls = []

    async def disable_no_connections_timeout(self):
        self.calls.append("disable_timeout")

    def get_internet_connection_profile(self):
        self.calls.append("get_profile")
        return self.profile

    def get_tethering_capability(self, profile):
        self.calls.append("get_capability")
        return self.supported, self.capability_name

    def create_tethering_manager(self, profile):
        self.calls.append("create_manager")
        return self

    def get_operational_state(self, manager):
        self.calls.append("get_state")
        return self.state

    def get_client_counts(self, manager):
        self.calls.append("get_counts")
        return self.client_count, self.max_client_count

    async def start_tethering(self, manager):
        self.calls.append("start")
        self.state = "on"
        self.client_count = 0
        return self.start_result


class HotspotGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_reports_clients_without_starting(self):
        backend = FakeBackend(state="on", client_count=2, max_client_count=8)

        result = await ensure_once(backend)

        self.assertEqual(result.status, "on")
        self.assertEqual(result.client_count, 2)
        self.assertEqual(result.max_client_count, 8)
        self.assertNotIn("start", backend.calls)
        self.assertEqual(backend.calls[0], "disable_timeout")

    async def test_off_starts_and_preserves_structured_counts(self):
        backend = FakeBackend(state="off", client_count=0)

        result = await ensure_once(backend)

        self.assertEqual(result.status, "started")
        self.assertEqual(result.client_count, 0)
        self.assertEqual(backend.calls.count("start"), 1)

    async def test_transition_never_starts(self):
        backend = FakeBackend(state="transition", client_count=0)

        result = await ensure_once(backend)

        self.assertEqual(result.status, "transition")
        self.assertNotIn("start", backend.calls)

    async def test_missing_profile_is_reported_before_capability_check(self):
        backend = FakeBackend(profile=None)

        result = await ensure_once(backend)

        self.assertEqual(result.status, "no_profile")
        self.assertNotIn("get_capability", backend.calls)
        self.assertNotIn("start", backend.calls)

    async def test_unsupported_capability_is_reported(self):
        backend = FakeBackend(
            supported=False,
            capability_name="DISABLED_BY_SYSTEM_CAPABILITY",
        )

        result = await ensure_once(backend)

        self.assertEqual(result.status, "unsupported")
        self.assertIn("DISABLED_BY_SYSTEM_CAPABILITY", result.message)
        self.assertNotIn("create_manager", backend.calls)

    async def test_failed_start_returns_operation_status_and_message(self):
        backend = FakeBackend(
            state="off",
            start_result=TetheringStartResult(
                False,
                "WIFI_DEVICE_OFF",
                "The Wi-Fi radio is disabled.",
            ),
        )

        result = await ensure_once(backend)

        self.assertEqual(result.status, "failed")
        self.assertIn("WIFI_DEVICE_OFF", result.message)
        self.assertIn("Wi-Fi radio", result.message)

    async def test_unknown_state_is_an_error_and_never_starts(self):
        backend = FakeBackend(state="unknown")

        result = await ensure_once(backend)

        self.assertEqual(result.status, "error")
        self.assertNotIn("start", backend.calls)

    async def test_backend_exception_becomes_structured_error(self):
        backend = FakeBackend()

        async def fail():
            raise RuntimeError("test failure")

        backend.disable_no_connections_timeout = fail

        result = await ensure_once(backend)

        self.assertEqual(result.status, "error")
        self.assertIn("RuntimeError", result.message)
        self.assertIn("test failure", result.message)

    async def test_client_counter_failure_does_not_hide_healthy_state(self):
        backend = FakeBackend(state="on")

        def fail_counts(manager):
            raise RuntimeError("counter unavailable")

        backend.get_client_counts = fail_counts

        result = await ensure_once(backend)

        self.assertEqual(result.status, "on")
        self.assertIsNone(result.client_count)
        self.assertIsNone(result.max_client_count)

    async def test_pywinrt_start_uses_no_session_configuration(self):
        class Success:
            pass

        class OperationStatus:
            SUCCESS = Success()

        class OperationResult:
            status = OperationStatus.SUCCESS
            additional_error_message = ""

        class Manager:
            def __init__(self):
                self.arguments = None

            async def start_tethering_async(self, *args):
                self.arguments = args
                return OperationResult()

        backend = PyWinRTBackend.__new__(PyWinRTBackend)
        backend._TetheringOperationStatus = OperationStatus
        manager = Manager()

        result = await backend.start_tethering(manager)

        self.assertTrue(result.success)
        self.assertEqual(manager.arguments, ())


if __name__ == "__main__":
    unittest.main()
