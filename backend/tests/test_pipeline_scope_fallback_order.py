"""The pipeline loader's scope fallback reads its candidates in a fixed order.

``_load_pipeline_tool_standalone`` hands the candidate integrations of a
provider to ``resolve_integration_by_scope``, whose tie-break keeps the
caller's own private row first and otherwise takes the first candidate. With
an unordered query, "first" was whatever order the database returned; the
query now reads them oldest first, ``id`` breaking a timestamp tie - the
order the template-binding and assignment paths already use.

``test_pipeline_scope_fallback_order_postgres.py`` proves the same pick on a
real database; these tests pin the query and the pick without one.
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from app.services import agent_chat

ACTING = uuid.uuid4()
ADMIN = uuid.uuid4()
REF = {"source": "integration", "provider": "opencti", "tool_name": "search"}


def _integ(*, user_id: uuid.UUID, is_company_managed: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="OpenCTI",
        provider="opencti",
        user_id=user_id,
        is_company_managed=is_company_managed,
        auth_type="api_key",
        scope_config=None,
    )


class _Session:
    """Records the executed statement and returns *rows* in the given order."""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.rows))


async def _pick(rows: list[SimpleNamespace]) -> tuple[_Session, AsyncMock]:
    session = _Session(rows)
    build = AsyncMock(return_value=([SimpleNamespace(name="search")], object()))
    agent = SimpleNamespace(name="Pipeline", id=uuid.uuid4())
    with patch("app.services.integration_tools.build_integration_tools", build):
        loaded = await agent_chat._load_pipeline_tool_standalone(
            session, REF, str(ACTING), agent, None
        )
    assert loaded is not None
    return session, build


def _picked_id(build: AsyncMock) -> str:
    bindings = build.await_args.args[1]
    return bindings["opencti"][0]


@pytest.mark.asyncio
async def test_fallback_query_orders_oldest_first_with_an_id_tie_break():
    session, _ = await _pick([_integ(user_id=ADMIN, is_company_managed=True)])

    # The candidate filter reads the acting user's groups first; the
    # candidate query is the one statement that selects integrations.
    (sql,) = [
        compiled
        for compiled in (
            str(stmt.compile(dialect=postgresql.dialect())) for stmt in session.statements
        )
        if "FROM integrations" in compiled
    ]
    assert sql.rstrip().endswith("ORDER BY integrations.created_at, integrations.id")


@pytest.mark.asyncio
async def test_single_candidate_is_picked_as_before():
    """The common case: one eligible integration, picked whatever its owner."""
    only = _integ(user_id=ADMIN, is_company_managed=True)

    _, build = await _pick([only])
    assert _picked_id(build) == str(only.id)


@pytest.mark.asyncio
async def test_first_candidate_wins_among_company_wide_rows():
    older = _integ(user_id=ADMIN, is_company_managed=True)
    newer = _integ(user_id=ADMIN, is_company_managed=True)

    _, build = await _pick([older, newer])
    assert _picked_id(build) == str(older.id)


def test_every_template_candidate_query_breaks_a_timestamp_tie_on_id():
    """Chat and evaluation template bindings (their shared resolver),
    assignments and the pipeline fallback read candidates in one order, so one
    agent resolves one instance on every path - down to two rows created at
    the same instant."""
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app" / "services"
    models = {"Integration", "_IntegModel"}
    queries: list[str] = []
    for name in ("agent_chat.py", "assignment_engine.py", "integration_tools.py"):
        tree = ast.parse((app_dir / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "order_by"
            ):
                continue
            cols = [
                (arg.value.id, arg.attr)
                for arg in node.args
                if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name)
            ]
            if not any(model in models and col == "created_at" for model, col in cols):
                continue
            queries.append(f"{name}:{node.lineno}")
            assert cols[-2:] == [(cols[-1][0], "created_at"), (cols[-1][0], "id")], (
                f"{name}:{node.lineno} orders integrations by created_at without the id tie-break"
            )
    assert len(queries) == 3, queries


@pytest.mark.asyncio
async def test_callers_own_row_still_wins_over_an_older_company_row():
    company = _integ(user_id=ADMIN, is_company_managed=True)
    own = _integ(user_id=ACTING, is_company_managed=False)

    _, build = await _pick([company, own])
    assert _picked_id(build) == str(own.id)
