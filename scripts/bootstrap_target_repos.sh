#!/usr/bin/env bash
set -euo pipefail

OWNER="${TARGET_OWNER:-$(gh api user --jq .login)}"
VISIBILITY="${1:-private}"

if [[ "$VISIBILITY" != "private" && "$VISIBILITY" != "public" ]]; then
  echo "Usage: scripts/bootstrap_target_repos.sh [private|public]" >&2
  exit 2
fi

python - "$OWNER" "$VISIBILITY" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

owner = sys.argv[1]
visibility = sys.argv[2]
config = json.loads(Path("config.json").read_text(encoding="utf-8"))

for app in config["apps"]:
    if not app.get("enabled", True):
        continue

    slug = app["repository"]
    repo = slug if "/" in slug else f"{owner}/{slug}"

    check = subprocess.run(
        ["gh", "repo", "view", repo, "--json", "nameWithOwner"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check.returncode == 0:
        print(f"Exists: {repo}")
        continue

    cmd = [
        "gh", "repo", "create", repo,
        f"--{visibility}",
        "--add-readme",
        "--description", f"Unmodified stock releases for {app['package']}",
    ]
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)
PY
