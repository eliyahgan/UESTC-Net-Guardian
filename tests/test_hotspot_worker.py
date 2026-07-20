import asyncio
import logging
import threading
import unittest

from hotspot_guard import HotspotResult
from hotspot_worker import HotspotGuardWorker


class HotspotWorkerTests(unittest.TestCase):
    def test_enabled_worker_runs_check_on_background_thread(self):
        completed = threading.Event()
        results = []

        async def fake_ensure():
            await asyncio.sleep(0)
            return HotspotResult("on", client_count=1, max_client_count=8)

        def callback(result):
            results.append(result)
            completed.set()

        worker = HotspotGuardWorker(
            logging.getLogger("hotspot-worker-test"),
            interval=30,
            result_callback=callback,
            ensure_function=fake_ensure,
        )
        worker.start()
        worker.set_enabled(True)
        try:
            self.assertTrue(completed.wait(3))
            self.assertEqual(results[0].status, "on")
            self.assertEqual(results[0].client_count, 1)
            # Enabling immediately after start must not leave a stale wake
            # signal that causes a second check in the same instant.
            threading.Event().wait(0.2)
            self.assertEqual(len(results), 1)
        finally:
            worker.stop()


if __name__ == "__main__":
    unittest.main()
