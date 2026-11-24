#!/usr/bin/env python3
"""Regenerate the reviewed snapshot of platform tools that skip approval.

The platform half of the HITL whitelist is derived from the read/write tool
classifier, not hand-written, which keeps it accurate as providers gain tools
but costs the reviewability the curated list has. ``tests/data/
hitl_platform_read_whitelist.txt`` buys that back: it is the derived set,
checked in, so widening it always shows up as diff lines a reviewer can read.

Run this after adding or renaming a platform tool, then **read the diff**. Every
added line is a tool that will run with no human in the loop on every
deployment, including scheduled runs nobody is watching. A tool that should not
be there belongs in ``HAND_GATED`` with its reason, not silently omitted here.

Usage, from ``backend/``::

    python -m scripts.dump_platform_read_whitelist          # write the snapshot
    python -m scripts.dump_platform_read_whitelist --check   # CI-style, no write
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

SNAPSHOT = _BACKEND / "tests" / "data" / "hitl_platform_read_whitelist.txt"
INTEGRATION_SNAPSHOT = (
    _BACKEND / "tests" / "data" / "hitl_integration_read_whitelist.txt"
)

_HEADER = """\
# Platform tools that skip human approval. GENERATED, REVIEWED AS DATA.
#
# One line per exempt tool, `provider:tool_name`. Regenerate with:
#   python -m scripts.dump_platform_read_whitelist
# and read the diff before committing it: every added line is a tool that
# will run unattended on every deployment, including scheduled runs nobody
# is watching. Tools held back by hand live in HAND_GATED, not here.
# No count in this header: a count line changes with every added tool and
# made every two tool PRs conflict on it; the diff of the body is the count.
"""


def render() -> str:
    from app.services.hitl_whitelist_platform import platform_read_tools

    by_provider = platform_read_tools()
    body = [
        f"{provider}:{name}"
        for provider in sorted(by_provider)
        for name in sorted(by_provider[provider])
    ]
    return _HEADER + "\n" + "\n".join(body) + "\n"


_INTEGRATION_HEADER = """\
# Integration tools that skip human approval. GENERATED, REVIEWED AS DATA.
#
# The sibling of the platform snapshot, for the ~100 integration providers this
# layer auto-exempts. It had no golden file at all, so widening THIS set — by
# far the larger one — showed up in no diff a reviewer could read.
#
# One line per exempt tool, `provider:tool_name`. Regenerate with:
#   python -m scripts.dump_platform_read_whitelist
# Only providers a deployment has actually connected contribute at runtime;
# this lists what each WOULD contribute, which is what review needs to see.
# No count in this header: it made every two tool PRs conflict on one line.
"""


def render_integrations() -> str:
    from app.services.hitl_whitelist_derived import provider_read_tools

    by_provider = provider_read_tools()
    body = [
        f"{provider}:{name}"
        for provider in sorted(by_provider)
        for name in sorted(by_provider[provider])
    ]
    return _INTEGRATION_HEADER + "\n" + "\n".join(body) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the snapshot is stale, without writing it",
    )
    args = parser.parse_args()

    pairs = ((SNAPSHOT, render()), (INTEGRATION_SNAPSHOT, render_integrations()))

    if args.check:
        stale = False
        for path, rendered in pairs:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != rendered:
                print(
                    f"{path} is stale — run "
                    "`python -m scripts.dump_platform_read_whitelist` and review "
                    "the diff.",
                    file=sys.stderr,
                )
                stale = True
            else:
                print(f"{path} is up to date.")
        return 1 if stale else 0

    for path, rendered in pairs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
