#!/usr/bin/env python3
"""Refresh the vendored Microsoft AgentSchema JSON Schema files.

The schemas under ``app/services/agentschema/schemas/`` are a verbatim, pinned
copy of ``microsoft/AgentSchema`` ``schemas/v1.0/*.yaml`` (MIT — see the
directory's ``NOTICE.md``). Validation must never depend on the network at
request time, and the upstream spec is still published as ``v1.0.0-beta.*``, so
refreshing is a deliberate, reviewable commit rather than something that
happens implicitly.

Usage::

    python backend/scripts/refresh_agentschema_vendor.py            # refresh + report
    python backend/scripts/refresh_agentschema_vendor.py --check    # report only

After a refresh that changes files:

1. update ``AGENTSCHEMA_UPSTREAM_COMMIT`` in
   ``app/services/agentschema/spec.py`` and the pinned commit in
   ``NOTICE.md`` to the SHA this script printed;
2. run ``pytest tests/test_agentschema_spec.py`` — it fails when the pin and
   the notice disagree, and when a previously valid export stops validating.

Deliberately dependency-light (stdlib only): it must run in a bare checkout,
not just inside the backend virtualenv.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

REPO = "microsoft/AgentSchema"
UPSTREAM_PATH = "schemas/v1.0"
VENDOR_DIR = Path(__file__).resolve().parents[1] / "app/services/agentschema/schemas"
_TIMEOUT_SECONDS = 30


def _get(url: str) -> bytes:
    request = urllib.request.Request(  # noqa: S310 - fixed https GitHub API host
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": "xtm-one"}
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report differences without writing anything (exit 1 if stale).",
    )
    args = parser.parse_args()

    head = json.loads(
        _get(f"https://api.github.com/repos/{REPO}/commits?path={UPSTREAM_PATH}&per_page=1")
    )[0]
    sha = head["sha"]
    date = head["commit"]["committer"]["date"]
    print(f"upstream {REPO}@{sha} ({date}) {UPSTREAM_PATH}")

    listing = json.loads(
        _get(f"https://api.github.com/repos/{REPO}/contents/{UPSTREAM_PATH}?ref={sha}")
    )
    remote = {
        entry["name"]: entry["download_url"]
        for entry in listing
        if entry["type"] == "file" and entry["name"].endswith(".yaml")
    }

    local = {path.name for path in VENDOR_DIR.glob("*.yaml")}
    added = sorted(set(remote) - local)
    removed = sorted(local - set(remote))
    changed: list[str] = []

    for name, url in sorted(remote.items()):
        content = _get(url)
        target = VENDOR_DIR / name
        if target.exists() and target.read_bytes() == content:
            continue
        if name not in added:
            changed.append(name)
        if not args.check:
            target.write_bytes(content)

    if not args.check:
        for name in removed:
            (VENDOR_DIR / name).unlink()

    for label, names in (("added", added), ("changed", changed), ("removed", removed)):
        if names:
            print(f"{label}: {', '.join(names)}")

    if not (added or changed or removed):
        print("vendored schemas already match upstream")
        return 0
    if args.check:
        print("vendored schemas are STALE (run without --check to refresh)")
        return 1
    print(
        "\nvendored schemas refreshed. Now update AGENTSCHEMA_UPSTREAM_COMMIT in "
        f"app/services/agentschema/spec.py and NOTICE.md to {sha}, then run "
        "pytest tests/test_agentschema_spec.py"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
