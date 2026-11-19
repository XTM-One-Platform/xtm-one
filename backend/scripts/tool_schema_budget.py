#!/usr/bin/env python3
"""Print the built-in tool schema budget: size, ceiling, headroom and delta per group.

Read-only: it measures what each built-in group's function schemas cost on the
wire (``app.services.builtin_tool_budget``) against the ceilings the size test
``tests/test_builtin_tool_groups.py`` asserts, with no test run. Save a
baseline on the base checkout first, then read the delta after the change.

Usage, from ``backend/``::

    python -m scripts.tool_schema_budget --save-baseline   # on the base checkout
    python -m scripts.tool_schema_budget                    # after the change
    python -m scripts.tool_schema_budget --top 10           # list more grown functions
    python -m scripts.tool_schema_budget --group coordination   # where a group's weight is

A group over its ceiling, and every ``--group``, is broken down after the table:
its functions by size with their largest arguments, then the arguments whose
schema is the same text in several of its functions.

Exit 1 when a group or the total is over its ceiling, 2 on an unknown
``--group``, 0 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def main(argv: list[str] | None = None) -> int:
    from app.services.builtin_tool_budget import (
        DEFAULT_BASELINE,
        GROUP_SCHEMA_CHARS_CEILING,
        compute_budget,
        format_breakdown,
        format_budget,
        load_baseline,
        measure_definitions,
        measure_schema_sizes,
        save_baseline,
    )

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="write the current sizes as the baseline the next readouts measure their delta against",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=_BACKEND / DEFAULT_BASELINE,
        help="baseline file (default: backend/.xtm/tool-schema-baseline.json)",
    )
    parser.add_argument("--top", type=int, default=5, help="grown functions to list (default 5)")
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        metavar="NAME",
        help="break this group down by function and argument (repeatable); a group over its ceiling always is",
    )
    args = parser.parse_args(argv)

    catalog = measure_definitions()
    unknown = [name for name in args.group if name not in catalog]
    if unknown:
        print(f"Unknown group: {', '.join(unknown)}. Groups: {', '.join(sorted(catalog))}", file=sys.stderr)
        return 2
    sizes = measure_schema_sizes(catalog)
    if args.save_baseline:
        save_baseline(sizes, args.baseline)
        print(f"Baseline saved to {args.baseline}")
        budget = compute_budget(sizes)
    else:
        budget = compute_budget(sizes, load_baseline(args.baseline))
    print(format_budget(budget, top=args.top))
    over = [name for name in budget.over if name in catalog]
    for group_id in dict.fromkeys([*args.group, *over]):
        print()
        print(
            format_breakdown(
                group_id,
                catalog[group_id],
                ceiling=GROUP_SCHEMA_CHARS_CEILING.get(group_id),
                top=None if group_id in args.group else 5,
            )
        )
    return 1 if budget.over else 0


if __name__ == "__main__":
    raise SystemExit(main())
