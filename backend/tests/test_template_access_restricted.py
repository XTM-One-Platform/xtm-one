"""Per-user template resolution honours ``access_restricted``.

A company-managed integration narrowed with ``access_restricted`` is usable by
its grantees only (``helpers.user_visible_filter``) - an admin included,
except in a company-managed automation. Per-user
(template) resolution used to treat every company-managed row as eligible, so
a template binding reached a restricted integration for anyone. Every
resolution path now builds its candidates with ``template_candidate_filter``,
which loads the acting user's groups and role for the shared visibility rule.
The rule itself is run by PostgreSQL in
``test_template_access_restricted_postgres``.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import true
from sqlalchemy.dialects import postgresql

import app.api.v1.helpers as helpers
from app.services import agent_chat, evaluation_engine, integration_tools

_APP = Path(__file__).resolve().parents[1] / "app"

#: module -> number of resolution queries it builds through the filter
_RESOLUTION_PATHS = {
    "services/agent_chat.py": 1,  # the pipeline fallback
    "services/assignment_engine.py": 1,
    # resolve_agent_integration_bindings: agent chat and evaluations
    "services/integration_tools.py": 1,
    "services/slack_events.py": 1,  # which runs a Slack event reaches through a template
}


def test_no_path_builds_template_candidates_without_the_acting_user_s_grants():
    direct = []
    for path in _APP.rglob("*.py"):
        if path.name == "integration_tools.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "template_candidate_clause(" in line and not line.lstrip().startswith("#"):
                direct.append(f"{path.relative_to(_APP)}:{number}")
    assert direct == []


@pytest.mark.parametrize(("module", "count"), _RESOLUTION_PATHS.items())
def test_every_resolution_path_uses_the_filter(module, count):
    source = "".join((_APP / module).read_text(encoding="utf-8").split())
    assert source.count("awaittemplate_candidate_filter(db,") == count


def _capture_clause(monkeypatch) -> dict:
    seen: dict = {}

    def clause(acting_user_uuid, user_group_ids=None, *, is_admin=False):
        seen.update(acting=acting_user_uuid, groups=user_group_ids, admin=is_admin)
        return true()

    monkeypatch.setattr(integration_tools, "template_candidate_clause", clause)
    return seen


@pytest.mark.parametrize(("role", "is_admin"), [("admin", True), ("user", False), (None, False)])
async def test_a_company_automation_loads_the_acting_user_s_groups_and_role(
    monkeypatch, role, is_admin
):
    acting = uuid.uuid4()
    seen = _capture_clause(monkeypatch)
    groups = AsyncMock(return_value=["group-1"])
    monkeypatch.setattr(helpers, "get_user_group_ids", groups)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: role))
    )

    await integration_tools.template_candidate_filter(db, acting, company_automation=True)

    assert seen == {"acting": acting, "groups": ["group-1"], "admin": is_admin}
    assert groups.await_args.args[1] == acting


async def test_any_other_turn_is_checked_without_reading_the_role(monkeypatch):
    acting = uuid.uuid4()
    seen = _capture_clause(monkeypatch)
    groups = AsyncMock(return_value=["group-1"])
    monkeypatch.setattr(helpers, "get_user_group_ids", groups)
    db = SimpleNamespace(execute=AsyncMock())

    await integration_tools.template_candidate_filter(db, acting)

    assert seen == {"acting": acting, "groups": ["group-1"], "admin": False}
    db.execute.assert_not_awaited()


#: module -> how each resolution path says whether its run is a company automation
_COMPANY_AUTOMATION_FLAGS = {
    # Five resolution paths, and the pipeline loader's wrapper forwarding the
    # flag to the loader it keeps behind the turn's interceptor (#8255).
    "services/agent_chat.py": ("company_automation=company_automation", 6),
    "services/assignment_engine.py": ("company_automation=bool(agent.is_company_managed)", 1),
    # The bundle it builds, and the turn it runs the agent in.
    "services/evaluation_engine.py": (
        'company_automation=bool(getattr(agent, "is_company_managed", False))',
        2,
    ),
}


@pytest.mark.parametrize(("module", "flag"), _COMPANY_AUTOMATION_FLAGS.items())
def test_every_resolution_path_says_whether_it_is_a_company_automation(module, flag):
    needle, count = flag
    source = "".join((_APP / module).read_text(encoding="utf-8").split())
    assert source.count("".join(needle.split())) == count


def _run_agent_chat_keywords(path: Path) -> list[set[str]]:
    """The keyword names of every ``run_agent_chat(...)`` call in *path*."""
    source = path.read_text(encoding="utf-8")
    if "run_agent_chat(" not in source:
        return []
    calls = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "run_agent_chat":
            calls.append({keyword.arg for keyword in node.keywords if keyword.arg})
    return calls


def test_only_an_evaluation_runs_an_agent_chat_turn_as_a_company_automation():
    """Chat surfaces and the agents a run hands work to are checked like any user.

    The flag keeps an evaluation's turn in step with the tools
    ``_build_evaluation_integration_tools`` resolved for it. Handed to an agent
    chat turn anywhere else, it would let the per-user fallback resolve again,
    for an admin, a restricted integration the chat gate had just dropped.
    """
    passing = sorted(
        path.relative_to(_APP).as_posix()
        for path in _APP.rglob("*.py")
        if any("company_automation" in call for call in _run_agent_chat_keywords(path))
    )
    assert passing == ["services/evaluation_engine.py"]


@pytest.mark.parametrize("company_managed", [True, False])
async def test_an_evaluation_turn_resolves_like_the_bundle_built_for_it(
    monkeypatch, company_managed
):
    chat = AsyncMock(return_value=("answer", None))
    monkeypatch.setattr(agent_chat, "run_agent_chat", chat)
    agent = SimpleNamespace(id=uuid.uuid4(), is_company_managed=company_managed)

    await evaluation_engine._execute_agent_full_loop(object(), agent, "model", "prompt")

    assert chat.await_args.kwargs["company_automation"] is company_managed


async def test_the_filter_without_an_acting_user_queries_nothing(monkeypatch):
    seen = _capture_clause(monkeypatch)
    db = SimpleNamespace(execute=AsyncMock())

    await integration_tools.template_candidate_filter(db, None)

    assert seen == {"acting": None, "groups": None, "admin": False}
    db.execute.assert_not_awaited()


@pytest.mark.parametrize(
    ("user_id", "acting"),
    [
        pytest.param("11111111-1111-4111-8111-111111111111", "user", id="identified_user"),
        pytest.param("anonymous", None, id="anonymous_turn"),
    ],
)
async def test_the_pipeline_fallback_resolves_candidates_through_the_filter(
    monkeypatch, user_id, acting
):
    candidates = AsyncMock(return_value=true())
    monkeypatch.setattr(integration_tools, "template_candidate_filter", candidates)
    empty = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    db = SimpleNamespace(execute=AsyncMock(return_value=empty))
    agent = SimpleNamespace(id=uuid.uuid4(), name="Pipeline agent", user_id=uuid.uuid4())
    ref = {"source": "integration", "provider": "slack", "tool_name": "slack_post"}

    assert await agent_chat._load_pipeline_tool_standalone(db, ref, user_id, agent) is None

    expected = uuid.UUID(user_id) if acting == "user" else None
    assert candidates.await_args.args[1] == expected


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect())).lower()


async def _in_ownerless_company_run(read):
    """*read* inside the run of a company-managed assignment with no user."""

    @helpers.company_managed_run_scoped
    async def _execute():
        assert helpers.mark_company_managed_run(
            SimpleNamespace(is_company_managed=True), SimpleNamespace(user_id=None)
        )
        return await read()

    return await _execute()


def test_with_no_account_a_restricted_company_integration_is_not_a_candidate():
    assert "access_restricted" in _sql(integration_tools.template_candidate_clause(None))


async def test_an_ownerless_company_managed_run_keeps_every_company_integration():
    """The run of a company-managed assignment with no user keeps main's tier in
    every read: its template bindings resolve like its runners,
    templates and handover agents, restricted rows included."""

    async def read():
        db = SimpleNamespace(execute=AsyncMock())
        return _sql(
            await integration_tools.template_candidate_filter(
                db, None, company_automation=True
            )
        )

    sql = await _in_ownerless_company_run(read)

    assert "access_restricted" not in sql
    assert "integrations.is_company_managed is true" in sql
