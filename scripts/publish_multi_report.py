#!/usr/bin/env python3
"""Publish the verified selection from a successful dry-run in the same job."""
import json
import os
import re
import sys
from pathlib import Path

import oplus_multi_donor as multi


def verified_candidate(data, package, stable_locale, fallback_locale):
    data = dict(data)
    donor = multi.Donor(source_url="", **data.pop("donor"))
    candidate = multi.Candidate(donor=donor, **data)
    apk = Path(candidate.apk_path).resolve()
    if not apk.is_relative_to(multi.STAGED.resolve()) or not apk.is_file():
        raise RuntimeError("Selected APK is outside the current job staging directory")
    if multi.sha256_file(apk) != candidate.apk_sha256:
        raise RuntimeError("Selected APK hash changed after dry-run")
    if multi.certificate_sha256(apk) != candidate.certificate_sha256:
        raise RuntimeError("Selected APK certificate changed after dry-run")
    badging = multi.aapt_badging(apk)
    metadata = multi.parse_package_line(badging)
    if metadata != (package, candidate.version_name, candidate.version_code):
        raise RuntimeError("Selected APK package or version does not match the dry-run")
    classification, _ = multi.classify_candidate(multi.parse_locales(badging), stable_locale, fallback_locale)
    if classification != candidate.classification or classification == "rejected":
        raise RuntimeError("Selected APK language classification does not match the dry-run")
    if package == "com.android.mms":
        manifest = multi.run(["aapt2", "dump", "xmltree", "--file", "AndroidManifest.xml", str(apk)], capture=True).stdout or ""
        if not re.search(r"com\.(oplus|coloros)\.", manifest):
            raise RuntimeError("Messages APK lacks OPlus/ColorOS vendor evidence")
    return candidate


def publish_report(path):
    report = json.loads(path.read_text(encoding="utf-8"))
    try:
        repo = os.environ["GITHUB_REPOSITORY"]
        if report.get("status") != "ok" or report.get("repository") != repo:
            raise RuntimeError("Publication requires a successful dry-run for this repository")
        apps = multi.load_json(multi.ROOT / "config.json")["apps"]
        package = report["package"]
        if not any(app["package"] == package and repo.split("/", 1)[1] == app["repository"] for app in apps):
            raise RuntimeError("Package does not belong to this release channel")
        candidates = {}
        for channel, data in report["selection"].items():
            if data:
                candidate = verified_candidate(data, package, report["stable_locale"], report["fallback_locale"])
                if candidate.classification != channel:
                    raise RuntimeError("Candidate is in the wrong release channel")
                candidates[channel] = candidate
        stable_code, experimental_code = multi.current_release_codes(repo)
        releases = []
        for channel in ("stable", "experimental"):
            candidate = candidates.get(channel)
            if candidate is None:
                continue
            threshold = stable_code if channel == "stable" else max(stable_code, experimental_code)
            if candidate.version_code <= threshold:
                releases.append({"status": "up-to-date", "channel": channel})
                continue
            releases.append(multi.publish_candidate(candidate, repo, prerelease=channel == "experimental", dry_run=False))
            if channel == "stable":
                stable_code = candidate.version_code
        report["releases"] = releases
        report["status"] = "ok"
        report["publication_completed"] = True
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    except Exception as exc:
        report["status"] = "failed"
        report["publication_error"] = str(exc)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        multi.log("PUBLICATION ERROR: " + str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(publish_report(Path(sys.argv[1])))
