#!/usr/bin/env python
"""Run one generator of ``backend/scripts/`` in the checkout and keep what it writes (#11483).

The ``backend-generate`` check of ``.xtm-one/checks.toml`` calls it:
``args: ["refresh_lucide_icons", "--check"]`` runs
``python -m scripts.refresh_lucide_icons --check`` from ``backend/``. The
files the generator writes come back to the workspace like any check's, so a
generated file is regenerated, never rewritten through code_interpreter.

Only a module that is a file of ``backend/scripts/`` is run: no dotted path,
no package outside it, no Python option. Exit 2 on a refused name, else the
generator's own exit code.
"""

from __future__ import annotations

import re
import runpy
import sys
from pathlib import Path

#: A generator name: one module of ``backend/scripts/``, without ``.py``.
_NAME = re.compile(r"[a-z][a-z0-9_]*")
SCRIPTS = Path(__file__).resolve().parent


def generators() -> list[str]:
    """The modules of ``backend/scripts/`` the check may run (this runner excluded)."""
    return sorted(
        p.stem for p in SCRIPTS.glob("*.py") if _NAME.fullmatch(p.stem) and p.stem not in ("run_generator", "__init__")
    )


def resolve(name: str) -> str | None:
    """The module path ``scripts.<name>`` when *name* is a generator, else None."""
    return f"scripts.{name}" if _NAME.fullmatch(name or "") and name in generators() else None


def main(argv: list[str]) -> int:
    if not argv:
        print("Name a generator: " + ", ".join(generators()), file=sys.stderr)
        return 2
    module = resolve(argv[0])
    if module is None:
        print(f"{argv[0]!r} is not a module of backend/scripts/: " + ", ".join(generators()), file=sys.stderr)
        return 2
    sys.argv = [module, *argv[1:]]
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
