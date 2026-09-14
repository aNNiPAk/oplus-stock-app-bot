#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
WORK = ROOT / ".work"
IMAGES = WORK / "images"
FILESYSTEMS = WORK / "filesystems"
STAGED = WORK / "staged"
REPORT_PATH = WORK / "report.json"

CATALOG_API = "https://roms.danielspringer.at/api/ota.php"

OPLUS_HEADERS = {
    "User-Agent": "Dalvik/2.1.0 (Linux; Android 16)",
    "userId": "oplus-ota|16002018",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
}

PACKAGE_RE = re.compile(
    r"^package:\s+name='(?P<package>[^']+)'.*?"
    r"versionCode='(?P<version_code>[^']+)'.*?"
    r"versionName='(?P<version_name>[^']*)'"
)

CERT_RE = re.compile(r"^Signer #1 certificate SHA-256 digest:\s*(.+)$", re.MULTILINE)


@dataclass
class AppCandidate:
    package: str
    version_name: str
    version_code: int
    certificate_sha256: str
    apk_sha256: str
    partition: str
    firmware_path: str
    staged_apk: str


def log(message: str) -> None:
    print(message, flush=True)


def run(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    log("+ " + " ".join(str(x) for x in args))
    return subprocess.run(
        args,
        check=check,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def require_commands() -> None:
    required = [
        "payload-dumper",
        "aapt2",
        "apksigner",
        "file",
        "fsck.erofs",
        "debugfs",
        "simg2img",
        "gh",
    ]
    missing = [name for name in required if not command_exists(name)]
    if missing:
        raise RuntimeError("Missing required commands: " + ", ".join(missing))


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not data.get("device", {}).get("model"):
        raise ValueError("config.json: device.model is required")

    apps = [x for x in data.get("apps", []) if x.get("enabled", True)]
    if not apps:
        raise ValueError("config.json: at least one enabled app is required")

    seen_packages: set[str] = set()
    seen_repositories: set[str] = set()
    for app in apps:
        package = app.get("package")
        repository = app.get("repository")
        if not package or not repository:
            raise ValueError("Every app needs package and repository")
        if package in seen_packages:
            raise ValueError(f"Duplicate package in config: {package}")
        if repository in seen_repositories:
            raise ValueError(f"Two apps target the same repository: {repository}")
        seen_packages.add(package)
        seen_repositories.add(repository)

    target = os.environ.get("APP_REPOSITORY", "").strip()
    if target:
        apps = [app for app in apps if app["repository"] == target]
        if not apps:
            raise ValueError(f"Unknown APP_REPOSITORY: {target}")
        data["device"] = {**data["device"], **apps[0].get("device", {})}
    data["apps"] = apps
    return data


def http_json(url: str, timeout: int = 30) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "OPlusStockAppBot/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def collect_catalog_entries(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return

        if not isinstance(value, dict):
            return

        if value.get("source_url") and (value.get("model") or value.get("ota_version")):
            found.append(value)
            return

        for child in value.values():
            walk(child)

    walk(node)

    unique: dict[str, dict[str, Any]] = {}
    for entry in found:
        key = str(entry.get("id") or entry.get("source_url"))
        unique[key] = entry
    return list(unique.values())


def numeric_version_key(value: Any) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(value or "")))


def catalog_sort_key(entry: dict[str, Any]) -> tuple[Any, ...]:
    try:
        published = int(entry.get("published") or 0)
    except (TypeError, ValueError):
        published = 0

    return (
        published,
        numeric_version_key(entry.get("build_timestamp")),
        numeric_version_key(entry.get("ota_version")),
        numeric_version_key(entry.get("version")),
    )


def resolve_catalog_release(model: str, region: str | None) -> dict[str, Any]:
    params = {"model": model, "latest": "1"}
    if region:
        params["region"] = region

    url = CATALOG_API + "?" + urllib.parse.urlencode(params)
    log(f"Querying OTA catalog: {url}")
    payload = http_json(url)
    entries = collect_catalog_entries(payload)

    exact_model = [
        x for x in entries if str(x.get("model", "")).casefold() == model.casefold()
    ]
    entries = exact_model

    if region:
        exact_region = [
            x for x in entries if str(x.get("region", "")).casefold() == region.casefold()
        ]
        entries = exact_region

    if not entries:
        raise RuntimeError(
            f"No OTA catalog entry found for model={model!r}, region={region!r}"
        )

    selected = max(entries, key=catalog_sort_key)
    if not selected.get("source_url"):
        raise RuntimeError("Selected OTA catalog entry has no source_url")

    log(
        "Selected OTA: "
        f"model={selected.get('model')}, "
        f"region={selected.get('region')}, "
        f"version={selected.get('version')}, "
        f"ota_version={selected.get('ota_version')}"
    )
    return selected


def resolve_download_url(source_url: str, attempts: int = 3) -> str:
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            headers = dict(OPLUS_HEADERS)
            headers["Range"] = "bytes=0-0"
            req = urllib.request.Request(source_url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=45) as response:
                final_url = response.geturl()
                sample = response.read(256)
                content_type = response.headers.get("Content-Type", "")

            if (
                final_url == source_url
                and "downloadCheck" in source_url
                and (
                    b'"errMsg"' in sample
                    or b'"responseCode"' in sample
                    or "application/json" in content_type.lower()
                )
            ):
                raise RuntimeError(
                    "OPlus downloadCheck returned an error response instead of a CDN redirect"
                )

            log(f"Resolved OTA URL: {urllib.parse.urlsplit(final_url).netloc}")
            return final_url
        except Exception as exc:
            last_error = exc
            log(f"Resolve attempt {attempt}/{attempts} failed: {exc}")
            if attempt < attempts:
                time.sleep(attempt * 3)

    raise RuntimeError(f"Could not resolve OTA download URL: {last_error}")


def extract_partition(source_url: str, partition: str) -> Path | None:
    partition_dir = IMAGES / partition
    shutil.rmtree(partition_dir, ignore_errors=True)
    partition_dir.mkdir(parents=True, exist_ok=True)
    image = partition_dir / f"{partition}.img"

    for attempt in range(1, 3):
        direct_url = resolve_download_url(source_url)
        try:
            run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "dump_payload.py"),
                    direct_url,
                    "-p",
                    partition,
                    "-o",
                    str(partition_dir),
                    "-j",
                    "2",
                ]
            )
        except subprocess.CalledProcessError as exc:
            log(
                f"Partition {partition}: payload extraction attempt "
                f"{attempt}/2 failed with exit code {exc.returncode}"
            )
            if attempt < 2:
                shutil.rmtree(partition_dir, ignore_errors=True)
                partition_dir.mkdir(parents=True, exist_ok=True)
                continue
            return None

        if image.exists() and image.stat().st_size > 0:
            return image

        log(f"Partition {partition}: payload-dumper produced no image")
        return None

    return None


def filesystem_type(image: Path) -> str:
    result = run(["file", "-b", str(image)], capture=True)
    return (result.stdout or "").strip()


def unpack_filesystem(image: Path, partition: str) -> Path | None:
    target = FILESYSTEMS / partition
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    raw = image
    description = filesystem_type(image)
    log(f"{partition}: {description}")

    if "Android sparse image" in description:
        raw = image.with_suffix(".raw.img")
        if raw.exists():
            raw.unlink()
        run(["simg2img", str(image), str(raw)])
        description = filesystem_type(raw)
        log(f"{partition} after simg2img: {description}")

    if "EROFS" in description.upper():
        run(["fsck.erofs", f"--extract={target}", str(raw)])
        return target

    if re.search(r"\bext[234]\b", description, re.IGNORECASE):
        result = run(
            ["debugfs", "-R", f"rdump / {target}", str(raw)],
            check=False,
            capture=True,
        )
        if result.returncode == 0:
            return target

    # Read-only fallback: some versions of `file` describe ext4 in a way that
    # doesn't match the text above, while debugfs can still read it correctly.
    result = run(
        ["debugfs", "-R", f"rdump / {target}", str(raw)],
        check=False,
        capture=True,
    )
    if result.returncode == 0:
        return target

    log(f"Unsupported filesystem for {partition}: {description}")
    shutil.rmtree(target, ignore_errors=True)
    return None


def parse_apk(apk: Path) -> tuple[str, str, int]:
    result = run(["aapt2", "dump", "badging", str(apk)], capture=True)
    first_line = (result.stdout or "").splitlines()[0] if result.stdout else ""
    match = PACKAGE_RE.search(first_line)
    if not match:
        raise RuntimeError(f"Could not parse APK metadata: {apk}")

    package = match.group("package")
    version_name = match.group("version_name") or match.group("version_code")
    raw_version_code = match.group("version_code")

    # `versionCode` is numeric for installable Android packages.
    version_code_match = re.match(r"^\d+", raw_version_code)
    if not version_code_match:
        raise RuntimeError(f"Non-numeric versionCode in {apk}: {raw_version_code}")

    return package, version_name, int(version_code_match.group(0))


def apk_certificate(apk: Path) -> str:
    result = run(
        ["apksigner", "verify", "--print-certs", str(apk)],
        capture=True,
    )
    match = CERT_RE.search(result.stdout or "")
    if not match:
        raise RuntimeError(f"Could not read signing certificate: {apk}; apksigner output: {result.stdout}; stderr: {result.stderr}")
    return match.group(1).strip().lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_filename_part(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._+-]+", "_", value).strip("._")
    return cleaned or "unknown"


def scan_partition(
    root: Path,
    partition: str,
    tracked: dict[str, dict[str, Any]],
    candidates: dict[str, AppCandidate],
    problems: list[str],
    inventory: list[dict[str, Any]] | None = None,
) -> None:
    handled_dirs: set[Path] = set()

    for apk in root.rglob("*.apk"):
        try:
            package, version_name, version_code = parse_apk(apk)
        except Exception:
            continue

        if os.environ.get("INVENTORY", "false").lower() == "true":
            log(f"INVENTORY {package} | {version_name} | {version_code} | {partition}/{apk.relative_to(root).as_posix()}")
            if inventory is not None:
                inventory.append({"package": package, "version_name": version_name,
                                  "version_code": version_code, "partition": partition,
                                  "firmware_path": apk.relative_to(root).as_posix()})
            continue

        if package not in tracked:
            continue

        if tracked[package].get("required_vendor") == "oplus":
            manifest = run(["aapt2", "dump", "xmltree", "--file", "AndroidManifest.xml", str(apk)], capture=True).stdout or ""
            evidence = [line.strip() for line in manifest.splitlines() if re.search(r"com\.(oplus|coloros)\.", line)]
            if not evidence:
                raise RuntimeError(f"{package}: no OPlus vendor evidence in APK manifest")
            for line in evidence[:8]:
                log("OPlus manifest evidence: " + line)

        app_dir = apk.parent
        if app_dir in handled_dirs:
            continue
        handled_dirs.add(app_dir)

        sibling_apks = sorted(x for x in app_dir.glob("*.apk") if x.is_file())
        if len(sibling_apks) != 1:
            message = (
                f"{package}: split APK directory is not published automatically: "
                + ", ".join(x.name for x in sibling_apks)
            )
            log("WARNING: " + message)
            problems.append(message)
            continue

        cert = apk_certificate(apk)
        apk_hash = sha256_file(apk)
        version_part = safe_filename_part(version_name)
        staged_name = f"{package}_{version_part}_{version_code}_{apk_hash[:12]}.apk"
        staged_path = STAGED / staged_name
        shutil.copy2(apk, staged_path)

        rel = apk.relative_to(root).as_posix()
        candidate = AppCandidate(
            package=package,
            version_name=version_name,
            version_code=version_code,
            certificate_sha256=cert,
            apk_sha256=apk_hash,
            partition=partition,
            firmware_path=rel,
            staged_apk=str(staged_path),
        )

        previous = candidates.get(package)
        if previous is None or candidate.version_code > previous.version_code:
            candidates[package] = candidate
            log(
                f"Found {package}: {version_name} ({version_code}) "
                f"in {partition}/{rel}"
            )
        elif (
            previous.version_code == candidate.version_code
            and previous.apk_sha256 != candidate.apk_sha256
        ):
            message = (
                f"{package}: same versionCode {version_code} has different APK "
                f"hashes in {previous.partition} and {partition}; keeping first candidate"
            )
            log("WARNING: " + message)
            problems.append(message)


def gh_repo(owner: str, repository: str) -> str:
    if "/" in repository:
        return repository
    return f"{owner}/{repository}"


def get_latest_release_apk(repo: str, temp_dir: Path) -> Path | None:
    view = run(
        ["gh", "release", "view", "--repo", repo, "--json", "tagName"],
        check=False,
        capture=True,
    )
    if view.returncode != 0:
        error = (view.stderr or "").strip()
        listing = run(
            ["gh", "api", f"repos/{repo}/releases?per_page=1"],
            check=False,
            capture=True,
        )
        if listing.returncode == 0:
            releases = json.loads(listing.stdout or "null")
            if releases == []:
                return None
        raise RuntimeError(f"{repo}: could not read latest release: {error}")

    shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    download = run(
        [
            "gh",
            "release",
            "download",
            "--repo",
            repo,
            "--pattern",
            "*.apk",
            "--dir",
            str(temp_dir),
        ],
        check=False,
        capture=True,
    )
    if download.returncode != 0:
        raise RuntimeError(
            f"{repo}: latest release exists but its APK could not be downloaded: "
            f"{(download.stderr or '').strip()}"
        )

    apks = sorted(temp_dir.glob("*.apk"))
    if len(apks) != 1:
        raise RuntimeError(
            f"{repo}: latest release must contain exactly one APK, found {len(apks)}"
        )
    return apks[0]


def make_release_notes(
    candidate: AppCandidate,
    catalog: dict[str, Any],
) -> str:
    source_model = catalog.get("model") or "unknown"
    source_region = catalog.get("region") or "unknown"
    source_version = catalog.get("version") or "unknown"
    ota_version = catalog.get("ota_version") or "unknown"
    catalog_id = catalog.get("id") or "manual-url"

    return f"""## {candidate.package}

- Version: `{candidate.version_name}`
- Version code: `{candidate.version_code}`
- Package: `{candidate.package}`
- Signing certificate SHA-256: `{candidate.certificate_sha256}`
- APK SHA-256: `{candidate.apk_sha256}`
- Source model: `{source_model}`
- Source region: `{source_region}`
- Firmware: `{source_version}`
- OTA version: `{ota_version}`
- OTA catalog entry: `{catalog_id}`
- Partition: `{candidate.partition}`
- Firmware path: `{candidate.firmware_path}`

The APK is published unchanged from the stock firmware image. It is not resigned or patched.
"""


def publish_candidate(
    app: dict[str, Any],
    candidate: AppCandidate,
    owner: str,
    catalog: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    repo = gh_repo(owner, app["repository"])
    result: dict[str, Any] = {
        "package": candidate.package,
        "repository": repo,
        "new_version_name": candidate.version_name,
        "new_version_code": candidate.version_code,
        "certificate_sha256": candidate.certificate_sha256,
    }

    repo_check = run(
        ["gh", "repo", "view", repo, "--json", "nameWithOwner"],
        check=False,
        capture=True,
    )
    if repo_check.returncode != 0:
        raise RuntimeError(
            f"Target repository {repo} does not exist or GH_TOKEN cannot access it"
        )

    required_cert = app.get("required_certificate_sha256")
    if required_cert:
        required_cert = str(required_cert).strip().lower()
        if candidate.certificate_sha256 != required_cert:
            raise RuntimeError(
                f"{candidate.package}: certificate does not match "
                "required_certificate_sha256 from config.json"
            )

    previous_dir = WORK / "previous" / safe_filename_part(candidate.package)
    previous_apk = get_latest_release_apk(repo, previous_dir)

    if previous_apk:
        old_package, old_version_name, old_version_code = parse_apk(previous_apk)
        old_cert = apk_certificate(previous_apk)

        result["old_version_name"] = old_version_name
        result["old_version_code"] = old_version_code
        result["old_certificate_sha256"] = old_cert

        if old_package != candidate.package:
            raise RuntimeError(
                f"{repo}: latest release contains package {old_package}, "
                f"expected {candidate.package}"
            )

        if old_cert != candidate.certificate_sha256:
            raise RuntimeError(
                f"{candidate.package}: signing certificate changed; refusing to "
                f"publish incompatible update to {repo}"
            )

        if candidate.version_code <= old_version_code:
            result["status"] = "up-to-date"
            log(
                f"{candidate.package}: repository already has versionCode "
                f"{old_version_code}; nothing to publish"
            )
            return result

    version_tag = safe_filename_part(candidate.version_name)
    tag = f"v{version_tag}-{candidate.version_code}"
    title = f"{candidate.version_name} ({candidate.version_code})"
    staged_apk = Path(candidate.staged_apk)
    notes = WORK / f"notes-{safe_filename_part(candidate.package)}.md"
    notes.write_text(make_release_notes(candidate, catalog), encoding="utf-8")

    existing = run(
        ["gh", "release", "view", tag, "--repo", repo],
        check=False,
        capture=True,
    )
    if existing.returncode == 0:
        result["status"] = "already-released"
        result["tag"] = tag
        return result

    if dry_run:
        result["status"] = "would-publish"
        result["tag"] = tag
        log(f"DRY RUN: would publish {candidate.package} as {repo}@{tag}")
        return result

    run(
        [
            "gh",
            "release",
            "create",
            tag,
            str(staged_apk),
            "--repo",
            repo,
            "--title",
            title,
            "--notes-file",
            str(notes),
            "--latest",
        ]
    )

    result["status"] = "published"
    result["tag"] = tag
    log(f"Published {candidate.package} -> {repo}@{tag}")
    return result


def cleanup_partition(partition: str) -> None:
    shutil.rmtree(IMAGES / partition, ignore_errors=True)
    shutil.rmtree(FILESYSTEMS / partition, ignore_errors=True)


def write_report(report: dict[str, Any]) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    for directory in (WORK, IMAGES, FILESYSTEMS, STAGED):
        directory.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "status": "running",
        "catalog": None,
        "candidates": {},
        "releases": [],
        "problems": [],
        "inventory": [],
    }

    try:
        require_commands()
        config = load_config()
        device = config["device"]
        model = str(device["model"])
        region = device.get("catalog_region")
        if os.environ.get("INVENTORY", "false").lower() == "true":
            model = os.environ.get("INVENTORY_MODEL", "").strip() or model
            region = os.environ.get("INVENTORY_REGION", "").strip() or region
        partitions = list(device.get("partitions") or [])
        if os.environ.get("INVENTORY", "false").lower() == "true" and os.environ.get("INVENTORY_PARTITIONS"):
            partitions = os.environ["INVENTORY_PARTITIONS"].split(",")
        if not partitions:
            raise ValueError("config.json: device.partitions must not be empty")

        override = os.environ.get("OTA_URL_OVERRIDE", "").strip()
        if override:
            catalog: dict[str, Any] = {
                "id": "manual-url",
                "model": model,
                "region": region,
                "version": "manual OTA URL",
                "ota_version": "manual OTA URL",
                "source_url": override,
            }
            log("Using OTA_URL_OVERRIDE instead of the catalog")
        else:
            catalog = resolve_catalog_release(model, region)

        report["catalog"] = {
            key: catalog.get(key)
            for key in (
                "id",
                "device",
                "region",
                "model",
                "version",
                "ota_version",
                "build_timestamp",
                "security_patch",
                "size",
                "published",
            )
        }

        source_url = str(catalog["source_url"])
        tracked = {app["package"]: app for app in config["apps"]}
        candidates: dict[str, AppCandidate] = {}
        problems: list[str] = report["problems"]

        for partition in partitions:
            if os.environ.get("INVENTORY", "false").lower() != "true" and len(candidates) == len(tracked):
                log("All tracked packages were found; remaining partitions are skipped")
                break

            log(f"\n=== Partition: {partition} ===")
            image = extract_partition(source_url, partition)
            if image is None:
                cleanup_partition(partition)
                continue

            fs_root = unpack_filesystem(image, partition)
            if fs_root is not None:
                scan_partition(
                    fs_root,
                    partition,
                    tracked,
                    candidates,
                    problems,
                    report["inventory"],
                )

            cleanup_partition(partition)

        report["candidates"] = {
            package: asdict(candidate)
            for package, candidate in candidates.items()
        }

        if os.environ.get("INVENTORY", "false").lower() == "true":
            report["status"] = "inventory-complete"
            write_report(report)
            return 0

        missing = sorted(set(tracked) - set(candidates))
        for package in missing:
            message = f"{package}: not found in configured partitions"
            log("WARNING: " + message)
            problems.append(message)

        owner = os.environ.get("TARGET_OWNER", "").strip()
        if not owner:
            raise RuntimeError("TARGET_OWNER is empty")

        dry_run = os.environ.get("DRY_RUN", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        publication_errors: list[str] = []
        for package, candidate in candidates.items():
            try:
                outcome = publish_candidate(
                    tracked[package],
                    candidate,
                    owner,
                    catalog,
                    dry_run,
                )
                report["releases"].append(outcome)
            except Exception as exc:
                message = f"{package}: {exc}"
                publication_errors.append(message)
                problems.append(message)
                log("ERROR: " + message)

        if publication_errors or missing:
            report["status"] = "partial-failure"
            write_report(report)
            return 1

        report["status"] = "ok"
        write_report(report)
        return 0

    except Exception as exc:
        report["status"] = "failed"
        report["fatal_error"] = str(exc)
        write_report(report)
        log("FATAL: " + str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
