#!/usr/bin/env python3
"""Download one channel's verified selection from the public suite bundle."""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import oplus_multi_donor as multi
from publish_multi_report import publish_report, verified_candidate

CENTRAL = "aNNiPAk/oplus-stock-app-bot"


def fetch_json(url):
    for attempt in range(3):
        try:
            return multi.http_json(url)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            multi.log(f"Metadata HTTP {exc.code}; retry {attempt + 1}/2")
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            multi.log(f"Metadata network timeout; retry {attempt + 1}/2")
        time.sleep(2 ** (attempt + 1))


def latest_bundle():
    releases = fetch_json(f"https://api.github.com/repos/{CENTRAL}/releases?per_page=100")
    bundles = [r for r in releases if not r["draft"] and r["prerelease"] and r["tag_name"].startswith("suite-")]
    if not bundles:
        raise RuntimeError("No completed suite bundle exists yet; run the central suite workflow first")
    return max(bundles, key=lambda r: r["published_at"])


def channel_report(manifest, package, repository):
    app = manifest.get("apps", {}).get(package)
    if manifest.get("status") != "ok" or not app or app.get("status") != "ok":
        raise RuntimeError("Bundle has no successful selection for this package")
    if app["repository"] != repository or app["package"] != package:
        raise RuntimeError("Bundle package does not belong to this repository")
    return json.loads(json.dumps(app))


def check_update(package):
    release = latest_bundle()
    metadata = next((a for a in release["assets"] if a["name"] == "suite-manifest.json" and a["state"] == "uploaded"), None)
    if not metadata:
        raise RuntimeError("Suite bundle has no manifest")
    report = channel_report(fetch_json(metadata["browser_download_url"]), package, os.environ["GITHUB_REPOSITORY"])
    stable, experimental = multi.current_release_codes(report["repository"])
    changed = any(data and data["version_code"] > (stable if channel == "stable" else max(stable, experimental))
                  for channel, data in report["selection"].items())
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as stream:
            stream.write(f"changed={str(changed).lower()}\n")
    if not changed:
        report["selection"] = {"stable": None, "experimental": None}
        report["bundle_tag"] = release["tag_name"]
        report["releases"] = [{"status": "up-to-date"}]
        multi.REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        multi.REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    multi.log(f"Channel needs new APK: {changed}")
    return 0


def consume(package, dry_run=False):
    release = latest_bundle()
    assets = {a["name"]: a for a in release["assets"] if a["state"] == "uploaded"}
    metadata = assets.get("suite-manifest.json")
    if not metadata:
        raise RuntimeError("Suite bundle has no manifest")
    manifest = fetch_json(metadata["browser_download_url"])
    report = channel_report(manifest, package, os.environ["GITHUB_REPOSITORY"])
    multi.STAGED.mkdir(parents=True, exist_ok=True)
    stable, experimental = multi.current_release_codes(report["repository"])
    for channel in ("stable", "experimental"):
        data = report["selection"].get(channel)
        if not data:
            continue
        threshold = stable if channel == "stable" else max(stable, experimental)
        if data["version_code"] <= threshold:
            report["selection"][channel] = None
            multi.log(f"{channel}: already up to date")
            continue
        name = data["apk_path"]
        if Path(name).name != name or name not in assets:
            raise RuntimeError("Invalid or missing bundle APK asset")
        apk = multi.STAGED / name
        with urllib.request.urlopen(assets[name]["browser_download_url"], timeout=120) as response, apk.open("wb") as output:
            shutil.copyfileobj(response, output)
        data["apk_path"] = str(apk)
        verified_candidate(data, package, "ru", "en")
    report["bundle_tag"] = release["tag_name"]
    multi.REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    multi.REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if dry_run:
        multi.log("Channel dry-run verified; no OTA extraction or publication")
        return 0
    return publish_report(multi.REPORT_PATH)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        sys.exit(check_update(args.package) if args.check_only else consume(args.package, args.dry_run))
    except Exception as exc:
        multi.log(f"CHANNEL ERROR: {exc}")
        sys.exit(1)
