#!/usr/bin/env python3
"""Refresh the pinned copy of OpenCRQ's tRPC procedure manifest.

The OpenCRQ provider calls OpenCRQ by procedure name, and
``tests/test_opencrq_trpc_contract.py`` fails when it calls one missing from
``tests/data/opencrq_trpc_procedures.json``. That file is OpenCRQ's
``back/trpc-procedures.json``, generated from its router, reduced to what the
test reads (each procedure's ``path`` and ``type``, not its input schema) and
taken at the last commit that changed it, so the pin moves only when the
manifest does. Refreshing it is the deliberate step that surfaces OpenCRQ's
renames and removals: the diff names them, and the test names the calls they break.

Usage::

    python backend/scripts/refresh_opencrq_trpc_procedures.py              # main
    python backend/scripts/refresh_opencrq_trpc_procedures.py --ref v1.2.0
    python backend/scripts/refresh_opencrq_trpc_procedures.py --check      # exit 1 if stale
    python backend/scripts/refresh_opencrq_trpc_procedures.py --manifest trpc-procedures.json --commit <sha>

The OpenCRQ repository is not public: the GitHub CLI (``gh``), signed in to an
account that can read it, does the fetching. Where there is no ``gh`` (an agent
sandbox), fetch ``back/trpc-procedures.json`` at the last commit that changed it
with any authorised client and pass it with ``--manifest`` and that ``--commit``:
nothing is fetched then. Stdlib only otherwise, so it runs on whatever python3 a
bare checkout has.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

REPO = "OpenCRQ-Platform/opencrq"
MANIFEST_PATH = "back/trpc-procedures.json"
PINNED = Path(__file__).resolve().parents[1] / "tests/data/opencrq_trpc_procedures.json"
# The layout tests/test_opencrq_trpc_contract.py reads.
MANIFEST_FORMAT = 1


def _gh_api(endpoint: str, *, raw: bool = False) -> str:
    accept = "application/vnd.github.raw+json" if raw else "application/vnd.github+json"
    try:
        return subprocess.run(
            ["gh", "api", "-H", f"Accept: {accept}", endpoint],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except FileNotFoundError:
        sys.exit("The GitHub CLI (gh) is required: https://cli.github.com")
    except subprocess.CalledProcessError as error:
        sys.exit(
            f"gh api {endpoint} failed ({error.returncode}): {error.stderr.strip()}\n"
            f"Check the ref, and that `gh auth status` shows an account that can read {REPO}."
        )


def _pinned_commit() -> str | None:
    if not PINNED.exists():
        return None
    return json.loads(PINNED.read_text(encoding="utf-8")).get("pinnedFrom", {}).get("commit")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ref", default="main", help="OpenCRQ branch, tag or commit to pin (default: main).")
    parser.add_argument("--check", action="store_true", help="Report without writing (exit 1 if stale).")
    parser.add_argument(
        "--manifest", type=Path, help=f"A copy of {MANIFEST_PATH} already fetched: no gh call (needs --commit)."
    )
    parser.add_argument("--commit", help="With --manifest: the OpenCRQ commit the copy was read at (full SHA).")
    args = parser.parse_args(argv)

    if args.manifest is not None:
        if not args.commit:
            parser.error("--manifest needs --commit, the OpenCRQ commit the copy was read at.")
        commit = args.commit
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    else:
        history = json.loads(
            _gh_api(f"repos/{REPO}/commits?path={MANIFEST_PATH}&sha={urllib.parse.quote(args.ref, safe='')}&per_page=1")
        )
        if not history:
            sys.exit(f"No commit on {args.ref} touches {MANIFEST_PATH}.")
        commit = history[0]["sha"]
        manifest = json.loads(_gh_api(f"repos/{REPO}/contents/{MANIFEST_PATH}?ref={commit}", raw=True))
    if manifest.get("formatVersion") != MANIFEST_FORMAT:
        sys.exit(
            f"{MANIFEST_PATH} at {commit[:12]} is format {manifest.get('formatVersion')}, this script reads "
            f"format {MANIFEST_FORMAT}: update it and tests/test_opencrq_trpc_contract.py together."
        )
    pinned = {
        "pinnedFrom": {"repository": REPO, "path": MANIFEST_PATH, "commit": commit},
        "formatVersion": MANIFEST_FORMAT,
        "procedures": [{"path": item["path"], "type": item["type"]} for item in manifest["procedures"]],
    }
    content = json.dumps(pinned, indent=2) + "\n"

    current = PINNED.read_text(encoding="utf-8") if PINNED.exists() else None
    if current == content:
        print(f"Up to date with {REPO}@{commit[:12]}.")
        return 0
    if args.check:
        print(f"Stale: pinned at {_pinned_commit()}, the manifest last changed on {args.ref} at {commit}.")
        return 1

    PINNED.write_text(content, encoding="utf-8")
    print(f"Pinned {len(manifest['procedures'])} procedures from {REPO}@{commit[:12]} into {PINNED.name}.")
    print("Run: uv run pytest tests/test_opencrq_trpc_contract.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
