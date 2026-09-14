#!/usr/bin/env python3
"""One partition pass for all stock apps; publish an immutable handoff bundle."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import oplus_multi_donor as multi
from publish_multi_report import verified_candidate

CACHE = multi.ROOT / ".work" / "donor-cache"
REPORT = multi.ROOT / ".work" / "suite-report.json"
BUNDLE = multi.ROOT / ".work" / "bundle"


def donor_key(donor, config):
    identity = {"donor": multi.public_donor(donor), "config": config, "schema": 1}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def load_cache(directory):
    metadata = directory / "candidates.json"
    if not metadata.is_file():
        return None
    result = []
    try:
        for data in json.loads(metadata.read_text()):
            data = dict(data)
            name = Path(data["apk_path"]).name
            apk = directory / name
            if not apk.is_file() or multi.sha256_file(apk) != data["apk_sha256"]:
                return None
            staged = multi.STAGED / name
            shutil.copy2(apk, staged)
            data["apk_path"] = str(staged)
            candidate = verified_candidate(data, data["package"], "ru", "en")
            result.append(candidate)
        return result
    except Exception as exc:
        multi.log(f"Cache rejected: {exc}")
        return None


def save_cache(directory, candidates):
    temporary = directory.with_name(directory.name + ".tmp")
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True)
    data = []
    for candidate in candidates:
        value = multi.public_candidate(candidate)
        name = Path(candidate.apk_path).name
        shutil.copy2(candidate.apk_path, temporary / name)
        value["apk_path"] = name
        data.append(value)
    (temporary / "candidates.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    shutil.rmtree(directory, ignore_errors=True)
    temporary.rename(directory)


def make_candidate(donor, partition, root, apk, badging):
    package, version, code = multi.parse_package_line(badging)
    if len(list(apk.parent.glob("*.apk"))) != 1:
        raise RuntimeError(f"{package}: split APK requires manual review")
    if package == "com.android.mms":
        manifest = multi.run(["aapt2", "dump", "xmltree", "--file", "AndroidManifest.xml", str(apk)], capture=True).stdout or ""
        if not re.search(r"com\.(oplus|coloros)\.", manifest):
            raise RuntimeError("Messages APK lacks OPlus/ColorOS vendor evidence")
    locales = multi.parse_locales(badging)
    classification, reason = multi.classify_candidate(locales, "ru", "en")
    # Language-rejected packages are still recorded, but never published.
    cert = multi.certificate_sha256(apk)
    digest = multi.sha256_file(apk)
    staged = multi.STAGED / f"{package}_{multi.safe_part(version)}_{code}_{digest[:12]}.apk"
    shutil.copy2(apk, staged)
    return multi.Candidate(package, version, code, donor, partition,
        apk.relative_to(root).as_posix(), str(staged), digest, cert,
        locales=locales, uses_libraries=multi.parse_uses_libraries(badging),
        native_libraries=multi.native_libraries(apk), classification=classification, reason=reason)


def extract_once(donor, partition):
    key = multi.safe_part(donor.id or donor.model or donor.device)
    multi.transport.IMAGES = multi.IMAGES / key
    multi.transport.FILESYSTEMS = multi.FILESYSTEMS / key
    directory = multi.transport.IMAGES / partition
    directory.mkdir(parents=True, exist_ok=True)
    direct = multi.resolve_download_url(donor.source_url)
    env = dict(os.environ, OPLUS_OTA_SOURCE=donor.source_url)
    # No restart from zero on a failed download; the adapter refreshes expired
    # URLs at the current range. A failed partition is not "package absent".
    multi.log(f"Extracting {donor.device}/{donor.region}: {partition}")
    try:
        subprocess.run([sys.executable, str(multi.ROOT / "scripts" / "dump_payload.py"),
            direct, "-p", partition, "-o", str(directory), "-j", "1"],
            check=True, timeout=600, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError("OTA partition extraction exceeded 600 seconds") from None
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"OTA partition extraction failed with exit code {exc.returncode}") from None
    image = directory / f"{partition}.img"
    if not image.is_file() or image.stat().st_size == 0:
        return None
    return multi.transport.unpack_filesystem(image, partition)


def scan_donor(donor, packages, config, deep_scan=False):
    directory = CACHE / donor_key(donor, dict(config, deep_scan=deep_scan))
    cached = load_cache(directory)
    if cached is not None:
        multi.log(f"OTA cache hit: {donor.device}/{donor.region}")
        return cached, []
    partitions = list(dict.fromkeys(p for package in sorted(packages)
        for p in config.get("package_partitions", {}).get(package, ["my_product", "my_stock"])))
    if deep_scan:
        partitions = list(dict.fromkeys(partitions + config.get("partitions", [])))
    result, errors, found = [], [], set()
    for partition in partitions:
        if found == set(packages):
            break
        try:
            root = extract_once(donor, partition)
            if root is None:
                continue
            # Parse each APK once; collect overlay diagnostics from this
            # already extracted partition without extra downloads.
            overlay_index = {}
            for apk in root.rglob("*.apk"):
                badging = multi.aapt_badging(apk)
                if "overlay" in apk.as_posix().lower():
                    target = multi.overlay_target(apk)
                    if target in packages:
                        names, locales = overlay_index.setdefault(target, ([], set()))
                        names.append(f"{partition}/{apk.relative_to(root).as_posix()}")
                        locales.update(multi.parse_locales(badging))
                parsed = multi.parse_package_line(badging)
                if not parsed or parsed[0] not in packages or parsed[0] in found:
                    continue
                try:
                    candidate = make_candidate(donor, partition, root, apk, badging)
                    result.append(candidate)
                    found.add(candidate.package)
                    multi.log(f"Candidate {candidate.package}: {candidate.version_name} ({candidate.version_code}), {candidate.classification}")
                except Exception as exc:
                    errors.append(f"{partition}/{apk.name}: {exc}")
            for candidate in result:
                diagnostics = overlay_index.get(candidate.package)
                if diagnostics:
                    names, locales = diagnostics
                    candidate.overlays = sorted(set(candidate.overlays + names))
                    candidate.overlay_locales = sorted(set(candidate.overlay_locales) | locales)
        except Exception as exc:
            errors.append(f"{partition}: {type(exc).__name__}: {exc}")
            multi.log(f"Partition unavailable: {partition}: {type(exc).__name__}")
        finally:
            multi.transport.cleanup_partition(partition)
    # Persist only complete reads; transient failures are retried next run.
    if not errors:
        save_cache(directory, [c for c in result if c.classification != "rejected"])
    return result, errors


def selections(candidates, apps, owner):
    reports = {}
    for app in apps:
        package = app["package"]
        pool = [c for c in candidates if c.package == package]
        stable = max((c for c in pool if c.classification == "stable"), key=multi.candidate_key, default=None)
        experimental = max((c for c in pool if c.classification == "experimental"), key=multi.candidate_key, default=None)
        if stable and experimental and experimental.version_code <= stable.version_code:
            experimental = None
        reports[package] = {"status": "ok" if stable or experimental else "missing",
            "package": package, "repository": f"{owner}/{app['repository']}",
            "stable_locale": "ru", "fallback_locale": "en",
            "selection": {"stable": multi.public_candidate(stable) if stable else None,
                          "experimental": multi.public_candidate(experimental) if experimental else None},
            "releases": []}
    return reports


def bundle_fingerprint(reports):
    values = {package: {channel: (data["version_code"], data["apk_sha256"]) if data else None
              for channel, data in report["selection"].items()} for package, report in reports.items()}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:24]


def publish_bundle(report):
    repo = os.environ["GITHUB_REPOSITORY"]
    tag = "suite-" + bundle_fingerprint(report["apps"])
    exists = multi.release_exists(repo, tag)
    if exists:
        state = json.loads(multi.run(["gh", "release", "view", tag, "--repo", repo, "--json", "isDraft"], capture=True).stdout)
        if not state["isDraft"]:
            multi.log(f"Suite bundle unchanged: {tag}")
            return tag
    shutil.rmtree(BUNDLE, ignore_errors=True)
    BUNDLE.mkdir(parents=True)
    assets = {}
    for package, app in report["apps"].items():
        for data in app["selection"].values():
            if not data:
                continue
            candidate = verified_candidate(data, package, "ru", "en")
            name = Path(candidate.apk_path).name
            target = BUNDLE / name
            shutil.copy2(candidate.apk_path, target)
            data["apk_path"] = name
            assets[name] = target
    manifest = BUNDLE / "suite-manifest.json"
    manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    # Draft keeps incomplete uploads invisible to consumers.
    files = [str(manifest)] + [str(p) for p in assets.values()]
    if exists:
        multi.run(["gh", "release", "upload", tag] + files + ["--repo", repo, "--clobber"])
    else:
        multi.run(["gh", "release", "create", tag] + files +
            ["--repo", repo, "--draft", "--prerelease", "--title", tag,
             "--notes", "Verified stock APK handoff bundle. Channel repositories publish their own releases."])
    multi.run(["gh", "release", "edit", tag, "--repo", repo, "--draft=false", "--prerelease", "--latest=false"])
    return tag


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-donors", type=int, default=6)
    parser.add_argument("--deep-scan", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_donors <= 6:
        raise ValueError("max-donors must be 1..6")
    config = multi.load_json(multi.DEFAULT_CONFIG)
    apps = [a for a in multi.load_json(multi.ROOT / "config.json")["apps"] if a.get("enabled", True)]
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "aNNiPAk")
    multi.STAGED.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    report = {"status": "running", "apps": {}, "errors": [], "donors": [],
              "overlay_policy": "No extra partition extraction for overlays", "deep_scan": args.deep_scan}
    candidates = []
    try:
        multi.require_commands()
        donors = multi.discover_donors(config, "16", args.max_donors)
        if not donors:
            raise RuntimeError("No donor OTAs found")
        report["donors"] = [multi.public_donor(d) for d in donors]
        for donor in donors:
            started = time.monotonic()
            values, errors = scan_donor(donor, {a["package"] for a in apps}, config, args.deep_scan)
            candidates.extend(values)
            report["errors"].extend(f"{donor.device}/{donor.region}: {error}" for error in errors)
            multi.log(f"Donor completed: {donor.device}/{donor.region}, {time.monotonic() - started:.1f}s")
        report["apps"] = selections(candidates, apps, owner)
        missing = [p for p, a in report["apps"].items() if a["status"] != "ok"]
        if missing:
            raise RuntimeError("No publishable APK for: " + ", ".join(missing))
        report["status"] = "ok"
        if not args.dry_run:
            report["bundle_tag"] = publish_bundle(report)
        return 0
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        multi.log(f"SUITE ERROR: {exc}")
        return 1
    finally:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
