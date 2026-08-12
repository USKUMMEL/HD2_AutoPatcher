import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from hd2_patch_fixer import settings  # noqa: E402


class SettingsTests(unittest.TestCase):
    def test_preferences_are_saved_and_restored_from_appdata_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "HD2 Patch Fixer" / "settings.json"
            with patch.object(settings, "preferences_path", return_value=path):
                settings.save_preferences({"gameDataFolder": "E:/Helldivers 2/data"})
                self.assertEqual(
                    settings.load_preferences(),
                    {"gameDataFolder": "E:/Helldivers 2/data"},
                )


if __name__ == "__main__":
    unittest.main()
