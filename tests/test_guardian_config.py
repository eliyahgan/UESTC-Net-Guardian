import json
from pathlib import Path
import tempfile
import unittest

from guardian_config import GuardianSettings, SettingsStore


class GuardianSettingsTests(unittest.TestCase):
    def test_missing_file_uses_both_guards_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")

            settings = store.load()

            self.assertTrue(settings.campus_enabled)
            self.assertTrue(settings.hotspot_enabled)
            self.assertEqual(settings.hotspot_check_interval, 10)

    def test_save_round_trip_and_atomic_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path)
            expected = GuardianSettings(False, True, 17)

            store.save(expected)

            self.assertEqual(store.load(), expected)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["hotspot_check_interval"], 17)

    def test_invalid_values_are_safely_normalized(self):
        settings = GuardianSettings.from_mapping(
            {"campus_enabled": 0, "hotspot_enabled": 1, "hotspot_check_interval": -99}
        )

        self.assertFalse(settings.campus_enabled)
        self.assertTrue(settings.hotspot_enabled)
        self.assertEqual(settings.hotspot_check_interval, 5)


if __name__ == "__main__":
    unittest.main()
