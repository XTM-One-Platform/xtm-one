"""Nothing the platform seeds or creates itself carries the AI label (#1735).

The AI label is a prompt (``assignment_helpers.DEFAULT_AI_LABEL_PROMPT``) the
model obeys on everything it writes: a seeded desk with it on titles its work
items and notes "[AI-generated] ...". The ``Assignment.ai_label`` column
defaults to true (the default of an assignment a person creates), so every
seed path must say ``False`` itself; these tests fail on a new seed or
auto-create path that forgets it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS
from app.agents.templates import BUILTIN_TEMPLATES

_APP = pathlib.Path(__file__).resolve().parents[1] / "app"

#: The modules that build ``Assignment`` rows with no person behind them.
_AUTO_CREATE_MODULES = (
    "agents/seed.py",
    "services/fleets.py",
    "services/health/fleet_seed.py",
)


def test_no_builtin_assignment_seeds_the_ai_label():
    labelled = [(a["agent_slug"], a["name"]) for a in BUILTIN_ASSIGNMENTS if a.get("ai_label", False) is not False]
    assert not labelled, f"seeded with the AI label: {labelled}"


def test_no_builtin_template_default_assignment_seeds_the_ai_label():
    labelled = [
        (t["slug"], a.get("name"))
        for t in BUILTIN_TEMPLATES
        for a in t.get("default_assignments") or []
        if a.get("ai_label", False) is not False
    ]
    assert not labelled, f"template default assignments with the AI label: {labelled}"


def _assignment_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Assignment"
    ]


def _is_false(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return node.value is False
    # ``data.get("ai_label", False)``: the seed's own default is off.
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and len(node.args) == 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value is False
    )


@pytest.mark.parametrize("module", _AUTO_CREATE_MODULES)
def test_every_auto_created_assignment_says_ai_label_false(module: str):
    """Explicit ``ai_label=False`` on every ``Assignment(...)``: the column default is never relied on."""
    tree = ast.parse((_APP / module).read_text())
    calls = _assignment_calls(tree)
    assert calls, f"{module} builds no Assignment any more: update _AUTO_CREATE_MODULES"
    for call in calls:
        value = next((kw.value for kw in call.keywords if kw.arg == "ai_label"), None)
        assert value is not None and _is_false(value), f"{module}:{call.lineno} Assignment(...) without ai_label=False"


@pytest.mark.parametrize(
    "module",
    ("agents/seed.py", "services/fleets.py", "services/health/fleet_seed.py"),
)
def test_update_branches_switch_the_label_off(module: str):
    """A row seeded before #1735 is switched off by the next boot too, not only by the migration."""
    tree = ast.parse((_APP / module).read_text())
    writes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "ai_label" for t in node.targets)
    ]
    assert writes, f"{module} never rewrites ai_label on an existing row"
    assert all(_is_false(node.value) for node in writes), module
