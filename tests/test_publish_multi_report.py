import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("publisher", Path(__file__).resolve().parents[1] / "scripts/publish_multi_report.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PublishReportTests(unittest.TestCase):
    def candidate(self, code, classification="stable"):
        donor = publisher.multi.Donor("1", "OP 15", "EU", "CPH2747", "16", "ota", "20260901", 0, "", "OnePlus")
        return publisher.multi.Candidate("com.oplus.calendar", "16", code, donor, "my_product", "Calendar.apk", "Calendar.apk", "a" * 64, "b" * 64, classification=classification)

    def test_wrong_repository_report_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps({"status": "ok", "repository": "other/repo"}))
            with patch.dict(publisher.os.environ, {"GITHUB_REPOSITORY": "aNNiPAk/oplus-calendar"}), patch.object(publisher, "verified_candidate") as verify:
                self.assertEqual(publisher.publish_report(path), 1)
                verify.assert_not_called()

    def test_changed_apk_is_rejected_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "Calendar.apk"
            apk.write_bytes(b"modified APK")
            candidate = self.candidate(30)
            candidate.apk_path = str(apk)
            with patch.object(publisher.multi, "STAGED", Path(directory)), patch.object(publisher.multi, "certificate_sha256") as cert:
                with self.assertRaisesRegex(RuntimeError, "hash changed"):
                    publisher.verified_candidate(publisher.multi.public_candidate(candidate), candidate.package, "ru", "en")
                cert.assert_not_called()

    def test_stale_dry_run_does_not_publish_older_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            report = {"status": "ok", "repository": "aNNiPAk/oplus-calendar", "package": "com.oplus.calendar",
                      "stable_locale": "ru", "fallback_locale": "en", "selection": {"stable": {"code": 150}, "experimental": {"code": 250}}}
            path.write_text(json.dumps(report))
            with patch.dict(publisher.os.environ, {"GITHUB_REPOSITORY": report["repository"]}), patch.object(publisher, "verified_candidate", side_effect=[self.candidate(150), self.candidate(250, "experimental")]), patch.object(publisher.multi, "current_release_codes", return_value=(200, 300)), patch.object(publisher.multi, "publish_candidate") as publish:
                self.assertEqual(publisher.publish_report(path), 0)
                publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
