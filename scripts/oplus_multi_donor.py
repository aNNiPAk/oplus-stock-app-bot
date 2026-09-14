#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oplus_release_bot as transport
_transport_resolver = transport.resolve_download_url

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "donors.json"

WORK = Path(os.environ.get("RUNNER_TEMP", ROOT / ".work")) / "oplus-multidonor"
IMAGES = WORK / "images"
FILESYSTEMS = WORK / "filesystems"
STAGED = WORK / "staged"
REPORT_PATH = Path(os.environ.get("REPORT_PATH", ROOT / ".work" / "report.json"))

DOWNLOADCHECK_HEADER_PROFILES = [
    {
        "User-Agent": "Dalvik/2.1.0 (Linux; Android 16)",
        "userId": "oplus-ota|16002018",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    },
    {
        "User-Agent": "Dalvik/2.1.0 (Linux; Android 16)",
        "userid": "oplus-ota|",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    },
]

PACKAGE_RE = re.compile(
    r"^package:\s+name='(?P<package>[^']+)'.*?"
    r"versionCode='(?P<version_code>\d+)'.*?"
    r"versionName='(?P<version_name>[^']*)'"
)
CERT_RE = re.compile(
    r"^Signer #1 certificate SHA-256 digest:\s*(.+)$",
    re.MULTILINE,
)
LOCALE_RE = re.compile(r"'([^']+)'")
OVERLAY_TARGET_RE = re.compile(r"targetPackage[^\n]*=\"([^\"]+)\"")


@dataclass
class Donor:
    id: str
    device: str
    region: str
    model: str
    version: str
    ota_version: str
    build_timestamp: str
    published: int
    source_url: str
    vendor: str


@dataclass
class Candidate:
    package: str
    version_name: str
    version_code: int
    donor: Donor
    partition: str
    firmware_path: str
    apk_path: str
    apk_sha256: str
    certificate_sha256: str
    locales: list[str] = field(default_factory=list)
    overlay_locales: list[str] = field(default_factory=list)
    overlays: list[str] = field(default_factory=list)
    uses_libraries: list[str] = field(default_factory=list)
    native_libraries: list[str] = field(default_factory=list)
    classification: str = "unknown"
    reason: str = ""


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
    missing = [x for x in required if shutil.which(x) is None]
    if missing:
        raise RuntimeError("Missing required tools: " + ", ".join(missing))


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def http_json(url: str, timeout: int = 45) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "OPlusStockAppBot/2.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def collect_catalog_entries(node: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        if value.get("source_url") and (value.get("device") or value.get("model")):
            result.append(value)
            return
        for child in value.values():
            walk(child)

    walk(node)

    unique: dict[str, dict[str, Any]] = {}
    for item in result:
        key = str(item.get("id") or item.get("source_url"))
        unique[key] = item
    return list(unique.values())


def canonical_device(device: str) -> str:
    return re.sub(r"^OP\s+", "OnePlus ", device.strip(), flags=re.IGNORECASE)


def vendor_of(device: str) -> str:
    low = canonical_device(device).casefold()
    for prefix, vendor in (("oppo", "OPPO"), ("oneplus", "OnePlus"), ("realme", "Realme")):
        if low.startswith(prefix):
            return vendor
    return "Other"


def build_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (transport.numeric_version_key(item.get("build_timestamp")),
            transport.catalog_sort_key(item))


def major_matches(version: str, major_os: str) -> bool:
    return bool(
        re.search(
            rf"(?:^|_){re.escape(major_os)}(?:\.|_|$)",
            version,
            flags=re.IGNORECASE,
        )
    )


def discover_donors(
    config: dict[str, Any],
    major_os: str,
    max_donors: int,
) -> list[Donor]:
    payload = http_json(config["catalog_url"])
    entries = collect_catalog_entries(payload)
    patterns = [
        re.compile(x, re.IGNORECASE)
        for x in config.get("device_allow_patterns", [])
    ]
    allowed_regions = {str(x).upper() for x in config.get("regions", [])}

    filtered: list[dict[str, Any]] = []
    for entry in entries:
        device = canonical_device(str(entry.get("device") or ""))
        region = str(entry.get("region") or "").upper()
        version = str(entry.get("version") or "")
        if patterns and not any(p.search(device) for p in patterns):
            continue
        if allowed_regions and region not in allowed_regions:
            continue
        if major_os and not major_matches(version, major_os):
            continue
        filtered.append(entry)

    filtered.sort(key=build_sort_key, reverse=True)

    quotas = {
        str(k): int(v)
        for k, v in config.get("vendor_quotas", {}).items()
    }
    if max_donors < 1:
        raise ValueError("max_donors must be positive")
    selected: list[dict[str, Any]] = []
    used_classes: set[tuple[str, str]] = set()
    counts = {vendor: 0 for vendor in quotas}
    # Round-robin preserves vendor diversity even for a reduced scan.
    pools = {vendor: [x for x in filtered if vendor_of(str(x.get("device") or "")) == vendor]
             for vendor in quotas}
    while len(selected) < max_donors:
        progress = False
        for vendor, pool in pools.items():
            if counts[vendor] >= quotas[vendor]:
                continue
            while pool:
                item = pool.pop(0)
                key = (canonical_device(str(item.get("device") or "")).casefold(),
                       "CN" if str(item.get("region") or "").upper() == "CN" else "GLOBAL")
                if key in used_classes:
                    continue
                used_classes.add(key)
                selected.append(item)
                counts[vendor] += 1
                progress = True
                break
            if len(selected) >= max_donors:
                break
        if not progress:
            break

    donors: list[Donor] = []
    for item in selected:
        try:
            published = int(item.get("published") or 0)
        except (ValueError, TypeError):
            published = 0
        donors.append(
            Donor(
                id=str(item.get("id") or ""),
                device=str(item.get("device") or ""),
                region=str(item.get("region") or ""),
                model=str(item.get("model") or ""),
                version=str(item.get("version") or ""),
                ota_version=str(item.get("ota_version") or ""),
                build_timestamp=str(item.get("build_timestamp") or ""),
                published=published,
                source_url=str(item["source_url"]),
                vendor=vendor_of(str(item.get("device") or "")),
            )
        )
    return donors


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_download_url_fallback(source_url: str) -> str:
    if "downloadCheck" not in source_url:
        return source_url

    opener = urllib.request.build_opener(NoRedirect())
    errors: list[str] = []

    for headers in DOWNLOADCHECK_HEADER_PROFILES:
        for method in ("HEAD", "GET"):
            request_headers = dict(headers)
            if method == "GET":
                request_headers["Range"] = "bytes=0-0"
            req = urllib.request.Request(
                source_url,
                headers=request_headers,
                method=method,
            )
            try:
                with opener.open(req, timeout=45) as response:
                    location = response.headers.get("Location")
                    if location:
                        return urllib.parse.urljoin(source_url, location)
                    final_url = response.geturl()
                    if final_url and final_url != source_url:
                        return final_url
            except urllib.error.HTTPError as exc:
                if 300 <= exc.code < 400:
                    location = exc.headers.get("Location")
                    if location:
                        return urllib.parse.urljoin(source_url, location)
                errors.append(f"{method} {exc.code}")
            except Exception as exc:
                errors.append(f"{method} {type(exc).__name__}: {exc}")

    raise RuntimeError(
        "Could not resolve OPlus downloadCheck URL: " + "; ".join(errors[-6:])
    )


def resolve_download_url(source_url: str) -> str:
    try:
        return _transport_resolver(source_url, attempts=2)
    except Exception:
        return resolve_download_url_fallback(source_url)


def file_description(path: Path) -> str:
    result = run(["file", "-b", str(path)], capture=True)
    return (result.stdout or "").strip()


def extract_partition(donor: Donor, partition: str) -> Path | None:
    key = safe_part(donor.id or donor.model or donor.device)
    transport.IMAGES = IMAGES / key
    transport.FILESYSTEMS = FILESYSTEMS / key
    transport.resolve_download_url = resolve_download_url
    return transport.extract_partition(donor.source_url, partition)


def unpack_image(image: Path, donor: Donor, partition: str) -> Path | None:
    transport.FILESYSTEMS = FILESYSTEMS / safe_part(donor.id or donor.model or donor.device)
    return transport.unpack_filesystem(image, partition)


def aapt_badging(apk: Path) -> str:
    result = run(
        ["aapt2", "dump", "badging", str(apk)],
        check=False,
        capture=True,
    )
    return result.stdout or ""


def parse_package_line(badging: str) -> tuple[str, str, int] | None:
    first = next(
        (line for line in badging.splitlines() if line.startswith("package:")),
        "",
    )
    match = PACKAGE_RE.search(first)
    if not match:
        return None
    return (
        match.group("package"),
        match.group("version_name") or match.group("version_code"),
        int(match.group("version_code")),
    )


def parse_locales(badging: str) -> list[str]:
    line = next(
        (line for line in badging.splitlines() if line.startswith("locales:")),
        "",
    )
    locales = sorted({normalize_locale(x) for x in LOCALE_RE.findall(line)})
    return [x for x in locales if x]


def parse_uses_libraries(badging: str) -> list[str]:
    values: set[str] = set()
    for line in badging.splitlines():
        if line.startswith("uses-library:") or line.startswith("uses-library-not-required:"):
            values.update(LOCALE_RE.findall(line))
    return sorted(values)


def normalize_locale(value: str) -> str:
    if value.startswith("b+"):
        value = value[2:].replace("+", "-")
    value = value.replace("_", "-").strip().lower()
    return "" if not value.strip("-") else value


def has_locale(locales: Iterable[str], wanted: str) -> bool:
    wanted = normalize_locale(wanted)
    for locale in locales:
        locale = normalize_locale(locale)
        if locale == wanted or locale.startswith(wanted + "-"):
            return True
    return False


def certificate_sha256(apk: Path) -> str:
    # Verification remains mandatory; certificate equality is not a filter.
    return transport.apk_certificate(apk)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def native_libraries(apk: Path) -> list[str]:
    result: set[str] = set()
    try:
        with zipfile.ZipFile(apk) as archive:
            for name in archive.namelist():
                if re.match(r"^lib/[^/]+/[^/]+\.so$", name):
                    result.add(name)
    except zipfile.BadZipFile:
        pass
    return sorted(result)


def find_package(root: Path, package_name: str) -> tuple[Path, str] | None:
    for apk in root.rglob("*.apk"):
        badging = aapt_badging(apk)
        parsed = parse_package_line(badging)
        if parsed and parsed[0] == package_name:
            return apk, badging
    return None


def overlay_target(apk: Path) -> str | None:
    result = run(
        [
            "aapt2",
            "dump",
            "xmltree",
            str(apk),
            "--file",
            "AndroidManifest.xml",
        ],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return None
    match = OVERLAY_TARGET_RE.search(result.stdout or "")
    return match.group(1) if match else None


def scan_overlays(
    root: Path,
    package_name: str,
) -> tuple[list[str], list[str]]:
    names: list[str] = []
    locales: set[str] = set()

    # Most RRO packages live under paths/names containing "overlay".
    # Restricting this pass avoids parsing every APK in a huge partition.
    possible = [
        apk
        for apk in root.rglob("*.apk")
        if "overlay" in apk.as_posix().casefold()
    ]
    for apk in possible:
        if overlay_target(apk) != package_name:
            continue
        badging = aapt_badging(apk)
        names.append(apk.relative_to(root).as_posix())
        locales.update(parse_locales(badging))
    return sorted(names), sorted(locales)


def safe_part(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z._+-]+", "_", value).strip("._")
    return value or "unknown"


def classify_candidate(
    locales: list[str],
    stable_locale: str,
    fallback_locale: str,
) -> tuple[str, str]:
    if has_locale(locales, stable_locale):
        return "stable", f"base APK contains {stable_locale}"
    if has_locale(locales, fallback_locale):
        return "experimental", (
            f"base APK lacks {stable_locale}, but contains {fallback_locale}"
        )
    if locales:
        return "rejected", (
            f"base APK contains neither {stable_locale} nor {fallback_locale}"
        )
    return "experimental", (
        "AAPT2 reported no explicit locales; default resources are language-unknown"
    )


def inspect_donor(
    donor: Donor, package_name: str, config: dict[str, Any],
    stable_locale: str, fallback_locale: str,
) -> Candidate | None:
    log(f"=== {donor.vendor}: {donor.device} [{donor.region}] {donor.version} ===")
    visited: set[str] = set()
    overlays: list[str] = []
    overlay_locales: set[str] = set()
    candidate = None
    overlay_partitions = config.get("overlay_partitions", [])
    preferred = config.get("package_partitions", {}).get(package_name, [])
    partitions = list(dict.fromkeys(preferred + config.get("partitions", [])))

    def inspect_overlays(root: Path, partition: str) -> None:
        names, locales = scan_overlays(root, package_name)
        overlays.extend(f"{partition}/{name}" for name in names)
        overlay_locales.update(locales)

    for partition in partitions:
        visited.add(partition)
        try:
            image = extract_partition(donor, partition)
            if image is None:
                continue
            root = unpack_image(image, donor, partition)
            if root is None:
                continue
            match = find_package(root, package_name)
            if partition in overlay_partitions or match:
                inspect_overlays(root, partition)
            if match is None:
                continue
            apk, badging = match
            if len(list(apk.parent.glob("*.apk"))) != 1:
                raise RuntimeError(f"{package_name}: split APK is not published automatically")
            if package_name == "com.android.mms":
                manifest = run(["aapt2", "dump", "xmltree", "--file", "AndroidManifest.xml", str(apk)],
                               capture=True).stdout or ""
                if not re.search(r"com\.(oplus|coloros)\.", manifest):
                    raise RuntimeError("Messages APK has no OPlus/ColorOS vendor evidence")
                log("OPlus Messages manifest verified")
            cert = certificate_sha256(apk)
            apk_hash = sha256_file(apk)
            package, version_name, version_code = parse_package_line(badging)
            locales = parse_locales(badging)
            classification, reason = classify_candidate(locales, stable_locale, fallback_locale)
            staged_dir = STAGED / safe_part(donor.id or donor.model or donor.device)
            staged_dir.mkdir(parents=True, exist_ok=True)
            staged = staged_dir / f"{package}_{safe_part(version_name)}_{version_code}_{apk_hash[:12]}.apk"
            shutil.copy2(apk, staged)
            candidate = Candidate(
                package=package, version_name=version_name, version_code=version_code,
                donor=donor, partition=partition, firmware_path=apk.relative_to(root).as_posix(),
                apk_path=str(staged), apk_sha256=apk_hash, certificate_sha256=cert,
                locales=locales, uses_libraries=parse_uses_libraries(badging),
                native_libraries=native_libraries(apk), classification=classification, reason=reason,
            )
            log(f"Candidate {package}: {version_name} ({version_code}), {classification}, locales={locales}")
            break
        finally:
            transport.cleanup_partition(partition)

    if candidate is None:
        log(f"NOTICE: {package_name} not found in {donor.device}/{donor.region}")
        return None
    if candidate.classification != "stable":
        for partition in overlay_partitions:
            if partition in visited:
                continue
            try:
                image = extract_partition(donor, partition)
                if image is not None:
                    root = unpack_image(image, donor, partition)
                    if root is not None:
                        inspect_overlays(root, partition)
            finally:
                transport.cleanup_partition(partition)
    candidate.overlays = sorted(set(overlays))
    candidate.overlay_locales = sorted(overlay_locales)
    return candidate


def cleanup_donor(donor: Donor) -> None:
    key = safe_part(donor.id or donor.model or donor.device)
    shutil.rmtree(IMAGES / key, ignore_errors=True)
    shutil.rmtree(FILESYSTEMS / key, ignore_errors=True)


def candidate_key(candidate: Candidate) -> tuple[Any, ...]:
    return (
        candidate.version_code,
        candidate.donor.build_timestamp,
        candidate.donor.published,
    )


def current_release_codes(repo: str) -> tuple[int, int]:
    result = run(["gh", "api", f"repos/{repo}/releases?per_page=100", "--paginate", "--slurp"],
                 capture=True)
    pages = json.loads(result.stdout or "null")
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise RuntimeError("Invalid GitHub release listing")
    stable = experimental = 0
    for release in (release for page in pages for release in page):
        if release.get("draft"):
            continue
        match = re.search(r"-(\d+)(?:-exp)?$", str(release.get("tag_name") or ""))
        if match:
            code = int(match.group(1))
            if release.get("prerelease"):
                experimental = max(experimental, code)
            else:
                stable = max(stable, code)
    return stable, experimental


def release_exists(repo: str, tag: str) -> bool:
    result = run(
        ["gh", "release", "view", tag, "--repo", repo],
        check=False,
        capture=True,
    )
    return result.returncode == 0


def release_notes(candidate: Candidate) -> str:
    d = candidate.donor
    locale_text = ", ".join(candidate.locales) or "none reported"
    overlay_locale_text = ", ".join(candidate.overlay_locales) or "none"
    overlay_text = "\n".join(f"  - `{x}`" for x in candidate.overlays) or "  - none found"
    uses_text = ", ".join(candidate.uses_libraries) or "none declared"
    native_count = len(candidate.native_libraries)

    return f"""## {candidate.package}

- Version: `{candidate.version_name}`
- Version code: `{candidate.version_code}`
- Channel classification: **{candidate.classification}**
- Classification reason: {candidate.reason}
- Base APK locales: `{locale_text}`
- Associated overlay locales (diagnostic only): `{overlay_locale_text}`
- Signing certificate SHA-256: `{candidate.certificate_sha256}`
- APK SHA-256: `{candidate.apk_sha256}`
- Declared uses-libraries: `{uses_text}`
- Native libraries: `{native_count}`

### Donor firmware

- Vendor: `{d.vendor}`
- Device: `{d.device}`
- Model: `{d.model}`
- Region: `{d.region}`
- Firmware: `{d.version}`
- OTA version: `{d.ota_version}`
- OTA catalog ID: `{d.id}`
- Build timestamp: `{d.build_timestamp}`
- Partition: `{candidate.partition}`
- Firmware path: `{candidate.firmware_path}`

### Matching runtime resource overlays

{overlay_text}

The APK asset is copied unchanged from the stock firmware image. It is not patched or re-signed.

The signing certificate is recorded for diagnostics only and is **not** used as a compatibility filter.
"""


def publish_candidate(
    candidate: Candidate,
    repo: str,
    *,
    prerelease: bool,
    dry_run: bool,
) -> dict[str, Any]:
    suffix = "-exp" if prerelease else ""
    tag = (
        f"v{safe_part(candidate.version_name)}-"
        f"{candidate.version_code}{suffix}"
    )
    title_suffix = " · experimental" if prerelease else ""
    title = (
        f"{candidate.version_name} ({candidate.version_code})"
        f"{title_suffix}"
    )
    notes_path = WORK / f"notes-{candidate.classification}.md"
    notes_path.write_text(release_notes(candidate), encoding="utf-8")

    outcome = {
        "tag": tag,
        "prerelease": prerelease,
        "candidate": public_candidate(candidate),
    }

    if release_exists(repo, tag):
        outcome["status"] = "already-exists"
        return outcome

    if dry_run:
        outcome["status"] = "would-publish"
        return outcome

    args = [
        "gh",
        "release",
        "create",
        tag,
        candidate.apk_path,
        "--repo",
        repo,
        "--title",
        title,
        "--notes-file",
        str(notes_path),
    ]
    if prerelease:
        args.extend(["--prerelease", "--latest=false"])
    else:
        args.append("--latest")

    run(args)
    outcome["status"] = "published"
    return outcome


def public_donor(donor: Donor) -> dict[str, Any]:
    data = asdict(donor)
    data.pop("source_url", None)
    return data


def public_candidate(candidate: Candidate) -> dict[str, Any]:
    data = asdict(candidate)
    data["donor"] = public_donor(candidate.donor)
    return data


def write_report(report: dict[str, Any]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True)
    parser.add_argument("--stable-locale", default="ru")
    parser.add_argument("--fallback-locale", default="en")
    parser.add_argument("--major-os", default="16")
    parser.add_argument("--max-donors", type=int, default=6)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report: dict[str, Any] = {
        "status": "running",
        "package": args.package,
        "repository": args.repo,
        "stable_locale": args.stable_locale,
        "fallback_locale": args.fallback_locale,
        "major_os": args.major_os,
        "donors": [],
        "candidates": [],
        "selection": {},
        "releases": [],
        "errors": [],
    }

    shutil.rmtree(WORK, ignore_errors=True)
    for path in (WORK, IMAGES, FILESYSTEMS, STAGED):
        path.mkdir(parents=True, exist_ok=True)

    try:
        if not 1 <= args.max_donors <= 12:
            raise ValueError("max_donors must be between 1 and 12")
        require_commands()
        config = load_json(args.config)
        donors = discover_donors(
            config,
            args.major_os,
            max(1, args.max_donors),
        )
        if not donors:
            raise RuntimeError("No matching OPlus donor OTAs found")

        report["donors"] = [public_donor(x) for x in donors]

        candidates: list[Candidate] = []
        for donor in donors:
            try:
                candidate = inspect_donor(
                    donor,
                    args.package,
                    config,
                    args.stable_locale,
                    args.fallback_locale,
                )
                if candidate is not None:
                    candidates.append(candidate)
            except Exception as exc:
                message = (
                    f"{donor.device}/{donor.region}: "
                    f"{type(exc).__name__}: {exc}"
                )
                report["errors"].append(message)
                log("WARNING: " + message)
                cleanup_donor(donor)

        report["candidates"] = [public_candidate(x) for x in candidates]
        if not candidates:
            raise RuntimeError(
                f"{args.package} was not extracted from any donor"
            )

        stable_candidates = [
            x for x in candidates if x.classification == "stable"
        ]
        experimental_candidates = [
            x for x in candidates if x.classification == "experimental"
        ]

        best_stable = (
            max(stable_candidates, key=candidate_key)
            if stable_candidates
            else None
        )
        best_experimental = (
            max(experimental_candidates, key=candidate_key)
            if experimental_candidates
            else None
        )

        # Do not create an experimental release when it isn't newer than the
        # selected stable APK.
        if (
            best_stable is not None
            and best_experimental is not None
            and best_experimental.version_code <= best_stable.version_code
        ):
            best_experimental = None

        report["selection"] = {
            "stable": public_candidate(best_stable) if best_stable else None,
            "experimental": (
                public_candidate(best_experimental)
                if best_experimental
                else None
            ),
        }

        if not args.repo:
            raise RuntimeError("--repo or GITHUB_REPOSITORY is required")

        current_stable, current_experimental = current_release_codes(args.repo)
        if best_experimental and best_experimental.version_code <= current_stable:
            best_experimental = None
            report["selection"]["experimental"] = None
        if not stable_candidates and not experimental_candidates:
            raise RuntimeError("No candidate has an acceptable base APK language")


        if (
            best_stable is not None
            and best_stable.version_code > current_stable
        ):
            report["releases"].append(
                publish_candidate(
                    best_stable,
                    args.repo,
                    prerelease=False,
                    dry_run=args.dry_run,
                )
            )
        elif best_stable is not None:
            report["releases"].append(
                {
                    "status": "stable-up-to-date",
                    "current_version_code": current_stable,
                    "candidate_version_code": best_stable.version_code,
                }
            )

        if (
            best_experimental is not None
            and best_experimental.version_code > current_experimental
        ):
            report["releases"].append(
                publish_candidate(
                    best_experimental,
                    args.repo,
                    prerelease=True,
                    dry_run=args.dry_run,
                )
            )
        elif best_experimental is not None:
            report["releases"].append(
                {
                    "status": "experimental-up-to-date",
                    "current_version_code": current_experimental,
                    "candidate_version_code": best_experimental.version_code,
                }
            )

        report["status"] = "ok"
        write_report(report)
        return 0

    except Exception as exc:
        report["status"] = "failed"
        report["fatal_error"] = f"{type(exc).__name__}: {exc}"
        write_report(report)
        log("FATAL: " + report["fatal_error"])
        return 1


if __name__ == "__main__":
    sys.exit(main())
