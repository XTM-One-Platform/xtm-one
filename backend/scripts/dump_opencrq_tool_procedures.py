#!/usr/bin/env python3
"""Regenerate the maps from each OpenCRQ tool to what it sends: procedures and input keys.

``app/integrations/providers/opencrq/tool_procedures.json`` is read off the
provider's source (``tests/opencrq_trpc_resolution.py``), not written by hand:
the runtime reads the committed file, and ``tests/test_opencrq_tool_procedures.py``
fails when it no longer matches the code. It names every tool and, for each, the
procedures its handler can send, so that a tool can be told apart from its
procedures without parsing the provider at runtime.

``tool_input_keys.json`` beside it lists, per tool, the input keys its handler sends: XTM One's
own keys, which say what an instance's schema would drop, without a copy of any schema.

Run this after adding, renaming or rewiring an OpenCRQ tool, or changing the keys it sends, then
read the diff: a procedure added to a tool is one more an OpenCRQ instance must serve for that
tool to stay available.

Usage, from ``backend/``::

    python -m scripts.dump_opencrq_tool_procedures          # write the map
    python -m scripts.dump_opencrq_tool_procedures --check   # CI-style, no write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_PROVIDER = _BACKEND / "app" / "integrations" / "providers" / "opencrq"
SNAPSHOT = _PROVIDER / "tool_procedures.json"
KEYS_SNAPSHOT = _PROVIDER / "tool_input_keys.json"


def render_keys() -> str:
    from tests.opencrq_trpc_resolution import tool_input_keys

    return json.dumps(tool_input_keys(), indent=2) + "\n"


def render() -> str:
    from tests.opencrq_trpc_resolution import tool_procedures

    tools = tool_procedures()
    unresolved = sorted(tool for tool, procedures in tools.items() if procedures is None)
    if unresolved:
        raise SystemExit(
            "These tools send a procedure that is not a literal or a name bound only to literals, "
            f"so no map can name it: {', '.join(unresolved)}."
        )
    return json.dumps(tools, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the map is stale, without writing it",
    )
    args = parser.parse_args()

    snapshots = {SNAPSHOT: render(), KEYS_SNAPSHOT: render_keys()}
    if args.check:
        stale = [
            path
            for path, rendered in snapshots.items()
            if not path.exists() or path.read_text(encoding="utf-8") != rendered
        ]
        for path in stale:
            print(
                f"{path} is stale - run `python -m scripts.dump_opencrq_tool_procedures` and review the diff.",
                file=sys.stderr,
            )
        if stale:
            return 1
        for path in snapshots:
            print(f"{path} is up to date.")
        return 0

    for path, rendered in snapshots.items():
        path.write_text(rendered, encoding="utf-8")
        print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
