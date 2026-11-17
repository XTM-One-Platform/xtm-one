"""Shared AST source-inspection helpers for shutdown-ordering pins.

Several regression tests pin call sites / call ordering inside ``app`` source
files without importing them (importing ``app.main`` builds the whole FastAPI
app).  These helpers were originally duplicated between
``test_langfuse_shutdown_flush.py`` and ``test_worker_shutdown_order.py`` —
keep them here so future source-inspection tests reuse them.
"""

from __future__ import annotations

import ast
from pathlib import Path


def function_node(path: Path, func_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Return the AST node of ``func_name`` in ``path`` (sync or async)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == func_name
        ):
            return node
    raise AssertionError(f"function {func_name!r} not found in {path}")


def call_lines(node: ast.AST, callee: str) -> list[int]:
    """Line numbers of every ``callee(...)`` call inside ``node``."""
    lines: list[int] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name == callee:
                lines.append(sub.lineno)
    return lines
