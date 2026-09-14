import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("multi", Path(__file__).resolve().parents[1] / "scripts/oplus_multi_donor.py")
multi = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = multi
spec.loader.exec_module(multi)


class MultiDonorTests(unittest.TestCase):
    def config(self):
        return multi.load_json(multi.DEFAULT_CONFIG)

    def entry(self, device, region="EU", build="20260901", code="16.0.1"):
        return {"id": device + region + build, "device": device, "region": region,
                "model": device, "build_timestamp": build, "version": device + "_" + code,
                "source_url": "https://example.org/ota.zip"}

    def test_real_catalog_aliases_keep_three_vendors(self):
        entries = [self.entry("OP 15", "CN"), self.entry("OPPO FIND X9", "CN"),
                   self.entry("Realme GT8 PRO", "CN"), self.entry("OP 15", "EU"),
                   self.entry("OPPO FIND X9", "EU"), self.entry("Realme GT8 PRO", "EU")]
        with patch.object(multi, "http_json", return_value=entries):
            donors = multi.discover_donors(self.config(), "16", 6)
        self.assertEqual(len(donors), 6)
        self.assertEqual({x.vendor for x in donors}, {"OPPO", "OnePlus", "Realme"})

    def test_global_duplicates_do_not_fill_donor_pool(self):
        entries = [self.entry("OP 15", r, str(100 + n)) for n, r in enumerate(["EU", "IN", "GLO", "CN"])]
        with patch.object(multi, "http_json", return_value=entries):
            donors = multi.discover_donors(self.config(), "16", 6)
        self.assertEqual(len(donors), 2)
        self.assertIn("CN", [x.region for x in donors])

    def test_freshest_branch_and_vendor_diversity(self):
        entries = [self.entry("OPPO FIND X9", build="9"), self.entry("OPPO FIND X9 PRO", build="10"),
                   self.entry("OP 15", build="11"), self.entry("Realme GT8 PRO", build="12"),
                   self.entry("OP 15T", build="13", code="15.0.9")]
        with patch.object(multi, "http_json", return_value=entries):
            donors = multi.discover_donors(self.config(), "16", 3)
        self.assertEqual([d.vendor for d in donors], ["OPPO", "OnePlus", "Realme"])
        self.assertEqual(donors[0].device, "OPPO FIND X9 PRO")

    def test_base_language_classification(self):
        for locales, expected in [(["ru-RU", "zh-CN"], "stable"), (["en-US"], "experimental"),
                                  ([], "experimental"), (["zh-CN"], "rejected")]:
            self.assertEqual(multi.classify_candidate(locales, "ru", "en")[0], expected)
        self.assertEqual(multi.parse_locales("locales: '--_--'"), [])
        self.assertTrue(multi.has_locale(multi.parse_locales("locales: 'b+ru+Cyrl+RU'"), "ru"))

    def test_release_listing_failure_is_not_zero_versions(self):
        with patch.object(multi, "run", side_effect=subprocess.CalledProcessError(1, ["gh"])):
            with self.assertRaises(subprocess.CalledProcessError):
                multi.current_release_codes("aNNiPAk/oplus-calendar")

    def test_release_listing_reads_all_pages_and_ignores_drafts(self):
        pages = [[{"tag_name": "v16-12", "prerelease": True}],
                 [{"tag_name": "v16-30", "draft": True}, {"tag_name": "v16-20"}]]
        for page in pages:
            for release in page:
                release["assets"] = [{"name": "a.apk", "size": 1, "state": "uploaded"}]
        with patch.object(multi, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(pages), "")):
            self.assertEqual(multi.current_release_codes("repo"), (20, 12))

    def candidate(self, code, classification="stable"):
        donor = multi.Donor("1", "OP 15", "EU", "CPH2747", "16", "ota", "20260901", 0, "private-url", "OnePlus")
        return multi.Candidate("com.oplus.calendar", "16.1", code, donor, "my_product", "Calendar.apk",
                               "Calendar.apk", "a" * 64, "b" * 64, classification=classification)

    def test_apk_version_beats_firmware_age_and_certificate_is_informational(self):
        older_fw_new_apk = self.candidate(30)
        newer_fw_old_apk = self.candidate(20)
        newer_fw_old_apk.donor.build_timestamp = "20260910"
        newer_fw_old_apk.certificate_sha256 = "c" * 64
        self.assertIs(max([older_fw_new_apk, newer_fw_old_apk], key=multi.candidate_key), older_fw_new_apk)
        self.assertNotIn("source_url", multi.public_candidate(older_fw_new_apk)["donor"])

    def test_experimental_publish_does_not_replace_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(multi, "WORK", Path(directory)), patch.object(multi, "release_exists", return_value=False), patch.object(multi, "run") as run:
                multi.publish_candidate(self.candidate(30, "experimental"), "repo", prerelease=True, dry_run=False)
                args = run.call_args.args[0]
                self.assertIn("--prerelease", args)
                self.assertIn("--latest=false", args)

    def test_empty_or_pending_release_is_not_current_version(self):
        pages = [[{"tag_name": "v1-100", "assets": []},
                  {"tag_name": "v1-90", "assets": [{"name": "a.apk", "size": 5, "state": "new"}]},
                  {"tag_name": "v1-20", "assets": [{"name": "a.apk", "size": 5, "state": "uploaded"}]}]]
        with patch.object(multi, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(pages), "")):
            self.assertEqual(multi.current_release_codes("repo"), (20, 0))

    def test_failed_draft_upload_is_not_published_and_can_be_retried(self):
        state = subprocess.CompletedProcess([], 0, json.dumps({"isDraft": True, "assets": []}), "")
        failure = subprocess.CalledProcessError(1, ["gh", "release", "upload"])
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(multi, "WORK", Path(directory)), \
                 patch.object(multi, "release_exists", return_value=True), \
                 patch.object(multi, "run", side_effect=[state, failure]) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    multi.publish_candidate(self.candidate(30), "repo", prerelease=False, dry_run=False)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args.args[0][2], "upload")

    def test_existing_draft_is_resumed_and_published(self):
        state = subprocess.CompletedProcess([], 0, json.dumps({"isDraft": True, "assets": []}), "")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(multi, "WORK", Path(directory)), \
                 patch.object(multi, "release_exists", return_value=True), \
                 patch.object(multi, "run", side_effect=[state, None, None]) as run:
                result = multi.publish_candidate(self.candidate(30), "repo", prerelease=False, dry_run=False)
                self.assertEqual(result["status"], "published")
                self.assertIn("--draft=false", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
