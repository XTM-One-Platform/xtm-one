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

Exit 1 when a group or the total is over its ceiling, 0 otherwise.
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
        compute_budget,
        format_budget,
        load_baseline,
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
    args = parser.parse_args(argv)

    sizes = measure_schema_sizes()
    if args.save_baseline:
        save_baseline(sizes, args.baseline)
        print(f"Baseline saved to {args.baseline}")
        budget = compute_budget(sizes)
    else:
        budget = compute_budget(sizes, load_baseline(args.baseline))
    print(format_budget(budget, top=args.top))
    return 1 if budget.over else 0


if __name__ == "__main__":
    raise SystemExit(main())
