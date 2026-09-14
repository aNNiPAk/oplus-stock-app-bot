import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("bot", Path(__file__).resolve().parents[1] / "scripts/oplus_release_bot.py")
bot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bot
spec.loader.exec_module(bot)


class BotTests(unittest.TestCase):
    def test_unknown_channel_is_rejected(self):
        with patch.dict(bot.os.environ, {"APP_REPOSITORY": "oplus-unknown"}):
            with self.assertRaisesRegex(ValueError, "Unknown APP_REPOSITORY"):
                bot.load_config()

    def test_selected_channel_excludes_other_apps(self):
        config = {"device": {"model": "RMX5131"}, "apps": [
            {"package": "com.oplus.calendar", "repository": "oplus-calendar"},
            {"package": "com.coloros.note", "repository": "oplus-notes"},
        ]}
        with patch.object(bot.json, "load", return_value=config), patch.dict(bot.os.environ, {"APP_REPOSITORY": "oplus-calendar"}):
            selected = bot.load_config()
            self.assertEqual([app["package"] for app in selected["apps"]], ["com.oplus.calendar"])

    def test_wrong_model_is_rejected(self):
        with patch.object(bot, "http_json", return_value=[{"model": "RMX9999", "source_url": "https://example.org/ota"}]):
            with self.assertRaisesRegex(RuntimeError, "No OTA catalog entry"):
                bot.resolve_catalog_release("RMX5131", None)

    def test_wrong_region_is_rejected(self):
        with patch.object(bot, "http_json", return_value=[{"model": "RMX5131", "region": "IN", "source_url": "https://example.org/ota"}]):
            with self.assertRaisesRegex(RuntimeError, "No OTA catalog entry"):
                bot.resolve_catalog_release("RMX5131", "EU")

    def test_release_access_failure_is_not_first_release(self):
        failure = subprocess.CompletedProcess([], 1, "", "HTTP 403: Resource not accessible by integration")
        with patch.object(bot, "run", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "could not read latest release"):
                bot.get_latest_release_apk("aNNiPAk/oplus-calendar", Path("unused"))

    def test_no_releases_is_allowed(self):
        failure = subprocess.CompletedProcess([], 1, "", "release not found")
        empty = subprocess.CompletedProcess([], 0, "[]", "")
        with patch.object(bot, "run", side_effect=[failure, empty]):
            self.assertIsNone(bot.get_latest_release_apk("aNNiPAk/oplus-calendar", Path("unused")))

    def test_missing_latest_with_existing_releases_is_not_first_release(self):
        failure = subprocess.CompletedProcess([], 1, "", "release not found")
        releases = subprocess.CompletedProcess([], 0, '[{"tag_name":"v1"}]', "")
        with patch.object(bot, "run", side_effect=[failure, releases]):
            with self.assertRaisesRegex(RuntimeError, "could not read latest release"):
                bot.get_latest_release_apk("aNNiPAk/oplus-calendar", Path("unused"))

    def test_missing_apk_fails_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(bot, WORK=root, IMAGES=root / "images", FILESYSTEMS=root / "filesystems", STAGED=root / "staged", REPORT_PATH=root / "report.json"), patch.object(bot, "require_commands"), patch.object(bot, "extract_partition", return_value=None), patch.dict(bot.os.environ, {"TARGET_OWNER": "aNNiPAk", "DRY_RUN": "true", "OTA_URL_OVERRIDE": "https://example.org/ota"}):
                self.assertEqual(bot.main(), 1)
                report = bot.json.loads((root / "report.json").read_text())
                self.assertEqual(report["status"], "partial-failure")
                self.assertTrue(any("not found" in problem for problem in report["problems"]))


if __name__ == "__main__":
    unittest.main()
