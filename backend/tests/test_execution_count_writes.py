"""``Assignment.execution_count`` is never written from a value read in Python.

Runs, run deletions and the agent deletion's run batches all move the count.
A write computed from the value a session loaded (``n = n + 1``, ``n -= 1``)
overwrites whatever another transaction wrote since, and a run's session holds
its assignment for minutes (``expire_on_commit=False``). Every relative change
is SQL arithmetic on the column; only the purges set it outright, to 0.
"""

from __future__ import annotations

import ast

from tests.app_sources import modules, walk


def _is_count(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "execution_count"


def _python_read_modify_writes() -> list[str]:
    found = []
    for path, module in modules():
        for node in walk(module):
            augmented = isinstance(node, ast.AugAssign) and _is_count(node.target)
            from_itself = (
                isinstance(node, ast.Assign)
                and any(_is_count(t) for t in node.targets)
                and any(_is_count(n) for n in ast.walk(node.value))
            )
            if augmented or from_itself:
                found.append(f"{path}:{node.lineno}")
    return found


def test_execution_count_moves_only_by_sql_arithmetic():
    assert _python_read_modify_writes() == [], (
        "execution_count is changed from a value read in Python; update it with SQL "
        "arithmetic on the column instead (update(Assignment).values(execution_count="
        "Assignment.execution_count + 1)), so a concurrent run or deletion is not overwritten"
    )
