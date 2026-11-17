"""Tripwire of the ROI ledger (issue #12077, design ``dev-docs/roi-outcomes.md`` 12.7 and 12.13).

Every ROI figure comes from ``roi_ledger.figures``. The old entry points
(``roi_report.build_report``, ``load_report`` and
``roi_chat_series.load_series``; ``load_summary`` is gone since slice L7a, the
list column reading ``roi_ledger.figures_for_subjects``) still feed the report keys the Value page and
the agent and fleet views read until slice L6 redraws them on ``figures``.
Their callers outside ``roi_ledger`` are listed here: a NEW caller fails this
test, and the list only shrinks (a caller that moved onto ``figures`` must be
dropped from it, the test says which). It is empty once L6 lands.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

OLD_ENTRY_POINTS = {
    ("roi_report", "build_report"),
    ("roi_report", "load_report"),
    ("roi_chat_series", "load_series"),
}

#: (file under app/, enclosing function, module, entry point): the callers left, each with the slice that moves it.
ALLOWED = {
    # L6: the Value page, agent and fleet views read the report keys next to ``figures``.
    ("api/v1/roi.py", "get_roi_report", "roi_report", "load_report"),
    # L6: the chat lines keep their own read (L4b put the ledger's ``figures`` of the chat source
    # next to them); the lines' own totals go when the chat card reads ``figures``.
    ("api/v1/roi.py", "get_roi_chat_series", "roi_chat_series", "load_series"),
    # L6: the get_roi_report tool answers ``figures`` next to the report keys.
    ("services/builtin_tools/roi.py", "_action", "roi_report", "load_report"),
    # L6: the chat series tool, as GET /roi/chat (``figures`` next to the lines since L4b).
    ("services/builtin_tools/roi.py", "_action", "roi_chat_series", "load_series"),
}


def _visit(node: ast.AST, rel: str, stack: list[str], found: set[tuple[str, str, str, str]]) -> None:
    is_def = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    if is_def:
        stack.append(node.name)  # type: ignore[attr-defined]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        owner = node.func.value
        module = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", None)
        if (module, node.func.attr) in OLD_ENTRY_POINTS:
            found.add((rel, stack[-1] if stack else "<module>", str(module), node.func.attr))
    for child in ast.iter_child_nodes(node):
        _visit(child, rel, stack, found)
    if is_def:
        stack.pop()


def _callers() -> set[tuple[str, str, str, str]]:
    found: set[tuple[str, str, str, str]] = set()
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if rel in ("services/roi_ledger.py", "services/roi_report.py", "services/roi_chat_series.py"):
            continue
        source = path.read_text(encoding="utf-8")
        if not any(name in source for _, name in OLD_ENTRY_POINTS):
            continue
        _visit(ast.parse(source), rel, [], found)
    return found


def test_no_new_caller_of_the_old_roi_entry_points() -> None:
    extra = _callers() - ALLOWED
    assert not extra, f"read roi_ledger.figures instead of the old ROI entry points (design 12.7): {sorted(extra)}"


def test_the_allow_list_only_shrinks() -> None:
    gone = ALLOWED - _callers()
    assert not gone, f"these callers moved onto roi_ledger.figures: drop them from ALLOWED: {sorted(gone)}"
