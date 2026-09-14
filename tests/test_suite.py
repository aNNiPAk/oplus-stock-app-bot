import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import oplus_suite as suite
import consume_suite as consumer


class SuiteTests(unittest.TestCase):
    def donor(self):
        return suite.multi.Donor("ota1", "OP 15", "EU", "CPH", "16", "ota", "20260901", 0, "signed-secret", "OnePlus")

    def test_partition_is_extracted_once_for_multiple_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.apk").touch()
            (root / "b.apk").touch()
            config = {"package_partitions": {"a": ["my_stock"], "b": ["my_stock"]}}
            def metadata(apk):
                return f"package: name='{apk.stem}' versionCode='1' versionName='1'"
            class Candidate:
                def __init__(self, package):
                    self.package = package
                    self.classification = "stable"
                    self.version_name = "1"
                    self.version_code = 1
            with patch.object(suite, "load_cache", return_value=None), \
                 patch.object(suite, "save_cache") as save, \
                 patch.object(suite, "extract_once", return_value=root) as extract, \
                 patch.object(suite.multi, "aapt_badging", side_effect=metadata) as badging, \
                 patch.object(suite, "make_candidate", side_effect=lambda d, p, r, a, b: Candidate(a.stem)), \
                 patch.object(suite.multi.transport, "cleanup_partition"):
                values, errors = suite.scan_donor(self.donor(), {"a", "b"}, config)
            self.assertEqual(len(values), 2)
            self.assertEqual(errors, [])
            self.assertEqual(extract.call_count, 1)
            self.assertEqual(badging.call_count, 2)
            save.assert_called_once()

    def test_failed_download_is_not_cached_as_absent_or_deep_searched(self):
        config = {"package_partitions": {"a": ["my_stock"]}, "partitions": ["my_stock", "system"]}
        with patch.object(suite, "load_cache", return_value=None), \
             patch.object(suite, "save_cache") as save, \
             patch.object(suite, "extract_once", side_effect=RuntimeError("403")) as extract, \
             patch.object(suite.multi.transport, "cleanup_partition"):
            values, errors = suite.scan_donor(self.donor(), {"a"}, config)
        self.assertEqual(values, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(extract.call_count, 1)
        save.assert_not_called()

    def test_ota_cache_key_ignores_signed_url_but_changes_with_build(self):
        donor = self.donor()
        key = suite.donor_key(donor, {})
        donor.source_url = "fresh-secret"
        self.assertEqual(key, suite.donor_key(donor, {}))
        donor.build_timestamp = "20260902"
        self.assertNotEqual(key, suite.donor_key(donor, {}))

    def test_cache_hit_does_not_extract(self):
        with patch.object(suite, "load_cache", return_value=[]), patch.object(suite, "extract_once") as extract:
            self.assertEqual(suite.scan_donor(self.donor(), {"a"}, {}), ([], []))
        extract.assert_not_called()

    def test_bundle_hash_ignores_runner_paths_but_detects_apk_change(self):
        reports = {"a": {"selection": {"stable": {"version_code": 2, "apk_sha256": "abc", "apk_path": "/runner/one"}, "experimental": None}}}
        before = suite.bundle_fingerprint(reports)
        reports["a"]["selection"]["stable"]["apk_path"] = "/runner/two"
        self.assertEqual(before, suite.bundle_fingerprint(reports))
        reports["a"]["selection"]["stable"]["apk_sha256"] = "changed"
        self.assertNotEqual(before, suite.bundle_fingerprint(reports))

    def test_selection_uses_apk_code_and_keeps_experimental_separate(self):
        donor = self.donor()
        def candidate(code, kind):
            return suite.multi.Candidate("a", "1", code, donor, "my_stock", "a.apk", "a.apk", "abc", "cert", classification=kind)
        values = suite.selections([candidate(2, "stable"), candidate(4, "experimental"), candidate(10, "rejected")],
                                  [{"package": "a", "repository": "oplus-a"}], "owner")
        self.assertEqual(values["a"]["selection"]["stable"]["version_code"], 2)
        self.assertEqual(values["a"]["selection"]["experimental"]["version_code"], 4)
        self.assertNotIn("source_url", values["a"]["selection"]["stable"]["donor"])

    def test_consumer_rejects_wrong_repository_and_failed_manifest(self):
        manifest = {"status": "ok", "apps": {"a": {"status": "ok", "repository": "owner/a", "package": "a", "selection": {}}}}
        with self.assertRaises(RuntimeError):
            consumer.channel_report(manifest, "a", "owner/b")
        manifest["status"] = "failed"
        with self.assertRaises(RuntimeError):
            consumer.channel_report(manifest, "a", "owner/a")

    def test_tampered_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.apk").write_bytes(b"tampered")
            (root / "candidates.json").write_text(json.dumps([{"apk_path": "a.apk", "apk_sha256": "wrong"}]))
            self.assertIsNone(suite.load_cache(root))
