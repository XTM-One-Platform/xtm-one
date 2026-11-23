"""Cases open only from a signal or a split (#1529): no other code path builds a ``CtemCase``.

A CTEM case enters the loop two ways: ``ctem_case_loop.open_from_signal``
(a signal the scope gate kept, recorded by a person, an agent, a listener or a
source reader) and ``ctem_cases_store.create_manual_case`` (a person or
``create_ctem_case`` splits issues out). A source recompute records signals; it
never writes a case itself. This tripwire reads the backend source.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

#: The only functions allowed to construct a ``CtemCase`` row.
ALLOWED = {
    ("services/ctem_case_loop.py", "open_from_signal"),
    ("services/ctem_cases_store.py", "create_manual_case"),
}


def _constructions() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "CtemCase":
                    found.add((path.relative_to(APP).as_posix(), func.name))
    return found


def test_only_open_from_signal_and_the_split_create_a_case() -> None:
    assert _constructions() == ALLOWED


def test_a_source_read_never_writes_the_source_read_reason() -> None:
    """The pre-signal recompute announced its cases with reason "source read"; nothing writes it any more."""
    offenders = [
        path.relative_to(APP).as_posix()
        for path in APP.rglob("*.py")
        if 'reason="source read"' in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
