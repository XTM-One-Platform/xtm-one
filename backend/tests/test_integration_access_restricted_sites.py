"""Picking a company-managed integration is a decision about ``access_restricted``.

Every SQL predicate selecting company-managed integrations, per-user template
resolution included, must say, within the two lines above it, whether it
honours the restriction or why it legitimately bypasses it
(``# access_restricted: ...``): a new selection that forgets the flag fails
here instead of shipping. The
predicates are found on the syntax tree, whatever the name the model is imported
or ``aliased`` under and however the expression is wrapped. The behaviour of
each honouring site is run by PostgreSQL in
``test_integration_access_restricted_sites_postgres``.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.api.v1 import helpers
from tests import app_sources

_APP = Path(__file__).resolve().parents[1] / "app"
_MARKER = "# access_restricted:"
_INTEGRATION_MODULE = "app.models.integration"


def _integration_names(tree: ast.AST) -> set[str]:
    """Names bound to the ``Integration`` model in *tree*: imports and ``aliased()``."""
    names: set[str] = set()
    for node in app_sources.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (_INTEGRATION_MODULE, "app.models"):
            names |= {a.asname or a.name for a in node.names if a.name == "Integration"}
    for node in app_sources.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", getattr(node.value.func, "attr", None)) == "aliased"
            and node.value.args
            and isinstance(node.value.args[0], ast.Name)
            and node.value.args[0].id in names
        ):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def _selects_company_managed(node: ast.AST, names: set[str]) -> bool:
    """``<Integration>.is_company_managed`` compared to true, in any SQL spelling."""

    def _column(expr: ast.AST) -> bool:
        if not (isinstance(expr, ast.Attribute) and expr.attr == "is_company_managed"):
            return False
        model = expr.value
        return (isinstance(model, ast.Name) and model.id in names) or (
            isinstance(model, ast.Attribute) and model.attr == "Integration"
        )

    def _const(expr: ast.AST, value: bool) -> bool:
        return isinstance(expr, ast.Constant) and expr.value is value

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
        method, arg = node.func.attr, node.args[0]
        if _column(node.func.value):
            return (method == "is_" and _const(arg, True)) or (
                method in ("isnot", "is_not") and _const(arg, False)
            )
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and _column(node.left):
        op, right = node.ops[0], node.comparators[0]
        return (isinstance(op, ast.Eq) and _const(right, True)) or (
            isinstance(op, ast.NotEq) and _const(right, False)
        )
    return False


def _undecided_in(source: str, tree: ast.Module | None = None) -> list[int]:
    tree = ast.parse(source) if tree is None else tree
    names = _integration_names(tree)
    lines = source.splitlines()
    return sorted(
        node.lineno
        for node in app_sources.walk(tree)
        if _selects_company_managed(node, names)
        and not any(_MARKER in line for line in lines[max(0, node.lineno - 3) : node.lineno - 1])
    )


def test_the_tripwire_reads_code_not_names_or_formatting():
    source = "\n".join(
        [
            '"""Integration.is_company_managed.is_(True) in prose is ignored."""',
            "from app.models.integration import Integration as Integ",
            "from app.models.agent import Agent",
            "Row = aliased(Integ)",
            "q1 = select(Integ).where(Integ.is_company_managed.is_(True))",
            "q2 = select(Row).where(",
            "    Row.is_company_managed",
            "    .is_(True)",
            ")",
            "q3 = select(Integ).where(Integ.is_company_managed == True)  # noqa: E712",
            "q4 = select(Agent).where(Agent.is_company_managed.is_(True))",
            "q6 = select(models.Integration).where(models.Integration.is_company_managed.is_(True))",
            "# access_restricted: bypass - decided.",
            "q5 = select(Integ).where(Integ.is_company_managed.isnot(False))",
        ]
    )

    assert _undecided_in(source) == [5, 7, 10, 12]


def test_every_company_managed_integration_selection_decides_on_access_restricted():
    undecided = [
        f"{path.relative_to(_APP)}:{lineno}"
        for path in app_sources.python_files(_APP)
        for lineno in _undecided_in(app_sources.source(path), app_sources.tree(path))
    ]

    assert undecided == [], (
        "Say within the two lines above whether this selection honours "
        "access_restricted (compose helpers.integration_visible_filter) or why "
        f"it may bypass it: {_MARKER} ..."
    )


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize("user_id", [None, "", "anonymous", "not-a-uuid"])
async def test_without_a_user_only_unrestricted_company_rows_are_visible(user_id):
    db = SimpleNamespace(execute=AsyncMock())

    clause = await helpers.integration_visible_filter(db, user_id)

    assert _sql(clause) == _sql(helpers.company_wide_visible_filter(helpers.Integration))
    db.execute.assert_not_awaited()


async def test_without_a_user_the_tier_is_the_one_caller_visible_filter_decides():
    # Work the server established keeps every company-managed row with no
    # account; the anonymous branch follows that one rule.
    db = SimpleNamespace(execute=AsyncMock())

    with helpers.keeping_company_managed_tier(helpers.MCP_GLOBAL_KEY):
        clause = await helpers.integration_visible_filter(db, None)
        expected = helpers.caller_visible_filter(helpers.Integration, None)

    assert _sql(clause) == _sql(expected) == _sql(helpers.Integration.is_company_managed.is_(True))
    db.execute.assert_not_awaited()


@pytest.mark.parametrize("spelling", [str, lambda uid: uid], ids=["text", "uuid"])
@pytest.mark.parametrize(("role", "is_admin"), [("admin", True), ("user", False), (None, False)])
async def test_a_known_user_gets_the_three_tier_filter_with_their_groups_and_role(
    monkeypatch, role, is_admin, spelling
):
    user_id = uuid.uuid4()
    seen: dict = {}

    def visible(model_cls, uid, group_ids, *, is_admin=False):
        seen.update(model=model_cls, uid=uid, groups=group_ids, admin=is_admin)
        return "clause"

    monkeypatch.setattr(helpers, "user_visible_filter", visible)
    monkeypatch.setattr(helpers, "get_user_group_ids", AsyncMock(return_value=["g1"]))
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: role))
    )

    assert await helpers.integration_visible_filter(db, spelling(user_id)) == "clause"
    assert seen == {"model": helpers.Integration, "uid": user_id, "groups": ["g1"], "admin": is_admin}


@pytest.mark.parametrize("integration_id", [None, "", "not-a-uuid"])
async def test_a_value_that_is_not_an_integration_id_is_never_visible(integration_id):
    db = SimpleNamespace(execute=AsyncMock())

    assert await helpers.integration_visible_to_user(db, integration_id, uuid.uuid4()) is False
    db.execute.assert_not_awaited()
