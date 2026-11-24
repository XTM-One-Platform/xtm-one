"""An admin's agent tools reach everything, like the REST routes, unless
``AGENT_TOOLS_STRICT_ADMIN_ACCESS`` keeps them to the admin's three tiers.

The call sites are pinned by the tripwire in ``test_manual_trigger_gate.py``
and through the real tools on PostgreSQL in
``test_assignment_visibility_postgres.py``; this file covers the setting, the
helper, the write loaders' order (three-tier first, then the bypass, one
WARNING), each suite's tools in both modes and the Security Audit finding.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.api.v1 import helpers, security_audit
from app.api.v1.helpers import agent_tool_admin_bypass
from app.core.config import Settings, settings
from app.services.builtin_tools import agent_builder


def test_strict_admin_access_is_off_by_default():
    assert Settings.model_fields["agent_tools_strict_admin_access"].default is False


@pytest.mark.parametrize(
    ("is_admin", "strict", "expected"),
    [(True, False, True), (True, True, False), (False, False, False)],
    ids=["admin-default", "admin-strict", "member"],
)
def test_only_an_admin_takes_the_bypass_and_only_by_default(monkeypatch, is_admin, strict, expected):
    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", strict)
    assert agent_tool_admin_bypass(SimpleNamespace(is_admin=is_admin)) is expected
    assert agent_tool_admin_bypass(None) is False


def _warnings(caplog):
    return [r for r in caplog.records if "AGENT_TOOLS_STRICT_ADMIN_ACCESS" in r.getMessage()]


@pytest.mark.asyncio
async def test_the_assignment_write_loader_looks_three_tier_first(monkeypatch, caplog):
    """Three-tier first; an admin whose tiers miss gets the REST bypass by
    default (one WARNING per process) and nothing more in strict mode."""
    admin = SimpleNamespace(id=uuid.uuid4(), is_admin=True)
    row = SimpleNamespace(id=uuid.uuid4(), agent_id=uuid.uuid4())
    agent = SimpleNamespace(id=row.agent_id)
    lookups = []

    async def _find(_db, _uid, _aid, _groups=None, *, is_admin=False, lock=False):
        lookups.append((is_admin, lock))
        return row if is_admin else None

    monkeypatch.setattr(helpers, "find_user_assignment", _find)
    monkeypatch.setattr(helpers, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_builder, "_load_caller_user_for_builder", AsyncMock(return_value=admin))
    monkeypatch.setattr(agent_builder, "_admin_write_bypass_reported", [])
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: agent)))

    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", False)
    # A write the gate refuses (a built-in agent) is no bypassed write.
    monkeypatch.setattr(agent_builder, "writable_assignment_error", lambda *_a, **_k: "Cannot modify built-ins.")
    with caplog.at_level("WARNING"):
        refused = await agent_builder._load_writable_assignment_for_builder(db, admin.id, row.id)
    assert refused == (None, None, "Cannot modify built-ins.")
    assert _warnings(caplog) == []

    monkeypatch.setattr(agent_builder, "writable_assignment_error", lambda *_a, **_k: None)
    lookups.clear()
    with caplog.at_level("WARNING"):
        first = await agent_builder._load_writable_assignment_for_builder(db, admin.id, row.id)
        second = await agent_builder._load_writable_assignment_for_builder(db, admin.id, row.id)
    assert first == second == (row, agent, None)
    assert lookups[:2] == [(False, True), (True, True)]
    assert len(_warnings(caplog)) == 1

    lookups.clear()
    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", True)
    found, parent, error = await agent_builder._load_writable_assignment_for_builder(db, admin.id, row.id)
    assert (found, parent) == (None, None)
    assert error == f"Assignment `{row.id}` not found."
    assert lookups == [(False, True)]


class _VisibilityDb:
    """Answers an entity SELECT with the row only when it carries no
    visibility predicate (the admin bypass)."""

    def __init__(self, row, table: str = "agents"):
        self.row = row
        self.table = table
        self.bypasses: list[bool] = []

    async def execute(self, statement, *_args, **_kwargs):
        where = str(statement.whereclause.compile(dialect=postgresql.dialect())).lower()
        bypass = f"{self.table}.user_id" not in where
        self.bypasses.append(bypass)
        return SimpleNamespace(scalar_one_or_none=lambda: self.row if bypass else None)


@pytest.mark.asyncio
async def test_the_agent_an_assignment_is_added_to_is_looked_up_three_tier_first(monkeypatch, caplog):
    admin = SimpleNamespace(id=uuid.uuid4(), is_admin=True)
    agent = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(helpers, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_builder, "_load_caller_user_for_builder", AsyncMock(return_value=admin))
    monkeypatch.setattr(agent_builder, "writable_agent_error", lambda *_a, **_k: None)
    monkeypatch.setattr(agent_builder, "_admin_write_bypass_reported", [])
    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", False)

    db = _VisibilityDb(agent)
    with caplog.at_level("WARNING"):
        loaded = await agent_builder._load_writable_agent_for_builder(
            db, admin.id, agent.id, action="add an assignment to", assignment_write=True
        )
    assert loaded == (agent, None)
    assert db.bypasses == [False, True]
    assert len(_warnings(caplog)) == 1

    # Any other agent write keeps the one admin lookup it always had.
    db = _VisibilityDb(agent)
    assert await agent_builder._load_writable_agent_for_builder(db, admin.id, agent.id) == (agent, None)
    assert db.bypasses == [True]

    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", True)
    db = _VisibilityDb(agent)
    loaded = await agent_builder._load_writable_agent_for_builder(
        db, admin.id, agent.id, action="add an assignment to", assignment_write=True
    )
    assert loaded == (None, f"Agent `{agent.id}` not found.")
    assert db.bypasses == [False]

    # ...and so does any other agent write: update_agent, delete_agent.
    db = _VisibilityDb(agent)
    loaded = await agent_builder._load_writable_agent_for_builder(db, admin.id, agent.id)
    assert loaded == (None, f"Agent `{agent.id}` not found.")
    assert db.bypasses == [False]


class _ToolIdsDb:
    """Answers the one tool-list read: the enabled agents' built-in tools."""

    def __init__(self, agents: list):
        self._agents = agents
        self.queries = 0

    async def execute(self, *_args, **_kwargs):
        self.queries += 1
        return SimpleNamespace(scalars=lambda: iter(self._agents))


@pytest.fixture
def platform(monkeypatch):
    state = SimpleNamespace(xtm_mcp_enabled=False, chat_enabled=False)

    async def _peek(_db):
        return state

    async def _chat_enabled(_db):
        return state.chat_enabled

    monkeypatch.setattr("app.api.v1.platform_settings_helpers.peek_platform_settings", _peek)
    monkeypatch.setattr("app.api.v1.platform.is_chat_enabled", _chat_enabled)
    return state


@pytest.mark.asyncio
async def test_the_security_audit_reports_the_bypass_where_an_admin_s_agent_can_use_it(monkeypatch, platform):
    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", False)
    findings: list[security_audit.AuditFinding] = []
    db = _ToolIdsDb(
        [
            ["agent_builder"],
            ["work_items"],
            ["flow_builder"],
            ["inspect_agent_traces"],
            ["inspect_my_traces"],
            ["web_fetch"],
            None,
        ]
    )
    await security_audit._check_agent_tool_admin_access(db, findings)
    [finding] = findings
    assert (finding.id, finding.category, finding.severity) == ("access.admin_reach_private_assignments", "access", "warning")
    assert finding.control == "access.agent_tools_strict_admin_access"
    assert (
        "4 agent(s) carrying the agent builder, the flow builder, the fleets, the work items or "
        "the agent trace inspection" in finding.description
    )
    assert "AGENT_TOOLS_STRICT_ADMIN_ACCESS=true" in (finding.recommendation or "")

    findings.clear()
    platform.xtm_mcp_enabled = True
    platform.chat_enabled = True
    await security_audit._check_agent_tool_admin_access(_ToolIdsDb([]), findings)
    [finding] = findings
    assert "the General Assistant, the XTM One MCP server" in finding.description


#: The suite each tool module that takes the admin bypass is carried by; the
#: evaluation tools ride the agent builder, the XTM One MCP server is counted
#: on its own.
_BYPASS_MODULE_SUITES = {
    "services/builtin_tools/agent_builder.py": "agent_builder",
    "services/builtin_tools/evaluations.py": "agent_builder",
    "services/builtin_tools/flow_builder.py": "flow_builder",
    # The fleet tools see fleets and agents as the REST routes do.
    "services/builtin_tools/fleets.py": "fleets",
    "services/builtin_tools/traces.py": "inspect_agent_traces",
    "services/builtin_tools/work_items.py": "work_items",
    # The workspace tools are registered by the agent builder.
    "services/builtin_tools/workspaces.py": "agent_builder",
    # The agent-account tools take it inside the agent builder only; an
    # agent configuring the accounts it operates never does.
    "services/builtin_tools/agent_accounts.py": "agent_builder",
    # The intent-binding tools: registered by the agent builder only.
    "services/builtin_tools/intent_bindings.py": "agent_builder",
    # The wait primitive is offered by the loop to every run, carried by no
    # suite: it reads the status and summary of a run whose id the run already holds.
    "services/builtin_tools/wait.py": None,
    # Platform Health orchestrator's tools (#8261): a person's cycles and requests, as on REST.
    "services/builtin_tools/health_orchestrator.py": "health_orchestrator",
    # The ROI tools (#10006): reports and records, as on REST.
    "services/builtin_tools/roi.py": "roi",
    # The CTEM signal tools (#826): granted to built-in CTEM agents by slug, carried
    # by no suite; an admin's agent lists the organization's signals, as on REST.
    "services/builtin_tools/ctem_signals.py": None,
    "api/v1/mcp_xtm.py": None,
}


def test_the_audit_counts_every_suite_whose_tools_take_the_bypass():
    """A suite whose tools start taking the admin bypass must be counted by the
    Security Audit, or the finding stays silent where the bypass exists."""
    import ast

    app = Path(__file__).resolve().parents[1] / "app"
    modules = [*sorted((app / "services" / "builtin_tools").rglob("*.py")), app / "api" / "v1" / "mcp_xtm.py"]
    calling = {
        path.relative_to(app).as_posix()
        for path in modules
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "agent_tool_admin_bypass"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    }
    assert calling == set(_BYPASS_MODULE_SUITES)
    suites = {suite for suite in _BYPASS_MODULE_SUITES.values() if suite is not None}
    assert suites == set(security_audit._ADMIN_BYPASS_SUITES)


@pytest.mark.asyncio
async def test_the_general_assistant_counts_whenever_chat_is_on(monkeypatch, platform):
    """The chat composer offers the agent builder to every General Assistant
    conversation and the session's tool list replaces the configured ones
    (``GeneralAssistantConfig.builtin_tool_ids``, an admin's
    ``User.ga_builtin_tool_ids``), so no configured list keeps it out."""
    from app.models.general_assistant_config import GENERAL_ASSISTANT_BUILTIN_TOOL_IDS

    assert "agent_builder" in GENERAL_ASSISTANT_BUILTIN_TOOL_IDS
    general_chat = (Path(__file__).resolve().parents[1] / "app" / "services" / "general_chat.py").read_text(
        encoding="utf-8"
    )
    assert "ga_builtin_tool_ids = session_builtin_tool_ids" in general_chat

    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", False)
    platform.chat_enabled = True
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_agent_tool_admin_access(_ToolIdsDb([["web_fetch"]]), findings)
    [finding] = findings
    assert "the General Assistant" in finding.description


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_the_security_audit_is_silent_without_the_tools(monkeypatch, platform, strict):
    """Silent only where no admin's agent can take the bypass: no agent carries
    the tools, the XTM One MCP server is off and chat is off (no General
    Assistant turn runs at all)."""
    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", strict)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_agent_tool_admin_access(_ToolIdsDb([["web_fetch"]]), findings)
    assert findings == []


@pytest.mark.asyncio
async def test_strict_mode_is_reported_as_a_pass_while_an_admin_s_agent_could_reach_further(monkeypatch, platform):
    # No silent pass: the enforced control says so where it matters.
    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", True)
    platform.chat_enabled = True
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_agent_tool_admin_access(_ToolIdsDb([["agent_builder"]]), findings)
    [finding] = findings
    assert (finding.id, finding.category, finding.severity) == ("access.admin_reach_strict", "access", "pass")
    assert (
        "1 agent(s) carrying the agent builder, the flow builder, the fleets, the work items or "
        "the agent trace inspection, the General Assistant" in finding.description
    )
    assert "reaches only the objects" in finding.description
    assert finding.control == "access.agent_tools_strict_admin_access"


# ---------------------------------------------------------------------------
# Every agent tool, not only the assignment ones
# ---------------------------------------------------------------------------


class _Rows:
    """One query's answer, in whichever shape the tool reads it."""

    def __init__(self, value=None):
        self.value = value

    def scalar(self):
        return self.value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        rows = self.value if isinstance(self.value, list) else [] if self.value is None else [self.value]
        return SimpleNamespace(all=lambda: list(rows), first=lambda: self.value)

    def all(self):
        return list(self.value or [])

    def fetchall(self):
        return list(self.value or [])


class _Session:
    """A session answering the queued results in order, then empty ones."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.statements: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        return _Rows(self.answers.pop(0) if self.answers else None)

    def add(self, _row):
        return None

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _where(statement) -> str:
    clause = statement.whereclause
    return "" if clause is None else str(clause.compile(dialect=postgresql.dialect())).lower()


def _admin():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin", email="admin@example.com")


@pytest.fixture
def builder(monkeypatch):
    """Runs an agent-builder tool as an admin on a scripted session."""
    from app.core import database
    from app.tools.base import ToolCall

    admin = _admin()
    monkeypatch.setattr(agent_builder, "_load_caller_user_for_builder", AsyncMock(return_value=admin))
    monkeypatch.setattr(helpers, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(helpers, "resolve_owner_names", AsyncMock(return_value={}))
    _defs, executor = agent_builder.build_agent_builder_tool(SimpleNamespace(), str(admin.id))

    async def run(tool: str, answers=(), **arguments):
        session = _Session(answers)
        monkeypatch.setattr(database, "async_session", lambda: session)
        result = await executor.execute_tool(ToolCall(id="1", name=tool, arguments=arguments))
        return result.content, session

    return run


def _strict(monkeypatch, strict: bool) -> None:
    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", strict)


def _colleague_agent():
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Colleague's agent",
        slug="colleagues-agent",
        description="",
        persona="",
        enabled=True,
        tags=[],
        knowledge_base_ids=[],
        mcp_server_ids=[],
        custom_tool_ids=[],
        integration_bindings={},
        prompt_ids=[],
        skill_ids=[],
        handover_agent_ids=[],
        handover_types={},
        is_company_managed=False,
        user_id=uuid.uuid4(),
    )


def _colleague_run():
    from datetime import UTC, datetime

    started = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        assignment_id=None,
        user_id=uuid.uuid4(),
        is_company_managed=False,
        parent_run_id=None,
        trigger_context={"type": "manual"},
        status="completed",
        result_summary="Done.",
        stop_reason=None,
        error=None,
        retry_count=0,
        actions_taken=[],
        human_input_question=None,
        human_input_response=None,
        total_input_tokens=1,
        total_output_tokens=1,
        created_at=started,
        started_at=started,
        completed_at=started,
    )


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
async def test_an_admin_s_agent_lists_and_opens_every_agent_and_run_unless_strict(monkeypatch, builder, strict):
    """``list_agents(scope="all")``, ``get_agent``, ``list_runs(scope="all")``
    and ``get_run`` reach a colleague's private rows for an admin's agent, as
    the REST detail routes do, and only the admin's three tiers in strict mode:
    a refusal for the platform-wide listings, "not found" for the rows."""
    _strict(monkeypatch, strict)
    agent, run = _colleague_agent(), _colleague_run()

    listed, session = await builder("list_agents", [2, [agent], []], scope="all")
    assert ("AGENT_TOOLS_STRICT_ADMIN_ACCESS is set" in listed) is strict
    assert ("all agents (admin)" in listed) is not strict
    assert len(session.statements) == (0 if strict else 3)

    opened, session = await builder("get_agent", [None, agent, 0], agent_id=str(agent.id))
    assert ("not found or you don't have access" in opened) is strict
    assert (agent.name in opened) is not strict
    assert len(session.statements) >= (1 if strict else 2)
    if strict:
        assert len(session.statements) == 1

    runs, session = await builder("list_runs", [1, [run], [(run.agent_id, "Agent")]], scope="all")
    assert ("AGENT_TOOLS_STRICT_ADMIN_ACCESS is set" in runs) is strict
    assert ("all runs (admin)" in runs) is not strict
    assert len(session.statements) == (0 if strict else 3)

    detail, session = await builder("get_run", [None, run, "Agent"], run_id=str(run.id))
    assert (detail == f"Run `{run.id}` not found.") is strict
    assert (f"# Run `{run.id}`" in detail) is not strict
    if strict:
        assert len(session.statements) == 1


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
async def test_an_admin_s_agent_binds_what_the_admin_s_tiers_show_in_strict_mode(monkeypatch, builder, strict):
    """The personal-binding check of ``create_agent`` / ``update_agent`` is a
    visibility check: an admin's agent binds a colleague's private knowledge
    base by default, as REST does, and only its three tiers in strict mode."""
    from fastapi import HTTPException

    from app.services import agent_bindings

    _strict(monkeypatch, strict)
    seen: list[bool] = []

    async def _check(*_args, is_admin=False, **_kwargs):
        seen.append(is_admin)
        raise HTTPException(status_code=400, detail="checked")

    monkeypatch.setattr(agent_bindings, "validate_personal_agent_bindings", _check)
    content, _session = await builder("create_agent", name="Bound", knowledge_base_ids=str(uuid.uuid4()))
    assert content == "checked"
    assert seen == [not strict]


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
async def test_the_entity_write_loader_takes_the_bypass_through_the_helper(monkeypatch, strict):
    """``update_prompt`` / ``delete_skill`` / ... load through
    ``_load_manageable_entity_for_builder``: a colleague's private row for an
    admin's agent by default, "not found" in strict mode."""
    from app.models.prompt import Prompt

    _strict(monkeypatch, strict)
    admin = _admin()
    prompt = SimpleNamespace(id=uuid.uuid4(), is_company_managed=False, user_id=uuid.uuid4())
    monkeypatch.setattr(agent_builder, "_load_caller_user_for_builder", AsyncMock(return_value=admin))
    monkeypatch.setattr(helpers, "get_user_group_ids", AsyncMock(return_value=[]))
    db = _VisibilityDb(prompt, table="prompts")
    entity, error = await agent_builder._load_manageable_entity_for_builder(
        db, Prompt, admin.id, prompt.id, entity_label="prompt"
    )
    assert db.bypasses == [not strict]
    assert (entity is None) is strict
    assert (error is not None and "not found" in error) is strict


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
async def test_the_flow_builder_takes_the_bypass_through_the_helper(monkeypatch, strict):
    """``list_flows`` and ``get_flow_run`` see a colleague's private flow and
    run for an admin's agent by default, and the node catalog offers what the
    admin's role reaches; all three keep to the three tiers in strict mode."""
    from app.services import flow_access
    from app.services.builtin_tools.flow_builder import build_flow_builder_tool
    from app.tools.base import ToolCall

    _strict(monkeypatch, strict)
    admin = _admin()
    monkeypatch.setattr(helpers, "get_user_group_ids", AsyncMock(return_value=[]))

    async def _call(db, tool, **arguments):
        _defs, executor = build_flow_builder_tool(db, str(admin.id))
        return (await executor.execute_tool(ToolCall(id="1", name=tool, arguments=arguments))).content

    db = _Session([admin, []])
    await _call(db, "list_flows")
    assert ("flows.user_id" in _where(db.statements[1])) is strict

    run = SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), is_company_managed=False, status="completed", error=None
    )
    content = await _call(_Session([admin, run, []]), "get_flow_run", run_id=str(run.id))
    assert (content == f"Run `{run.id}` not found.") is strict
    # A run of a flow shared with the admin's group is in the three tiers
    # (the REST flow-run visibility, every tier of it).
    group = str(uuid.uuid4())
    monkeypatch.setattr(helpers, "get_user_group_ids", AsyncMock(return_value=[group]))
    shared = SimpleNamespace(**{**vars(run), "id": uuid.uuid4(), "shared_group_ids": [group]})
    content = await _call(_Session([admin, shared, []]), "get_flow_run", run_id=str(shared.id))
    assert "status: **completed**" in content and "not found" not in content, content

    seen: list[bool] = []

    async def _sources(_db, _user, *, is_admin=None):
        seen.append(is_admin)
        return {}

    monkeypatch.setattr(flow_access, "catalog_sources", _sources)
    await _call(_Session([admin]), "get_node_catalog")
    assert seen == [not strict]


def _agent_graph(*agent_ids: str) -> dict:
    return {
        "nodes": [
            {"id": f"n{i}", "kind": "agent", "config": {"agent_id": agent_id}}
            for i, agent_id in enumerate(agent_ids)
        ],
        "edges": [],
    }


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
async def test_a_flow_an_admin_s_agent_saves_or_runs_keeps_to_the_three_tiers_in_strict_mode(monkeypatch, strict):
    """Narrowing the node catalog is not enough: an id typed into the graph
    reaches past it. In strict mode every id a dynamic source fills must be one
    the admin's three tiers offer - on a save for the ids it adds, on a run for
    all of them; a member and the default mode are not checked."""
    from app.services import flow_access
    from app.services.flow_references import check_three_tier_references

    _strict(monkeypatch, strict)
    visible, hidden = str(uuid.uuid4()), str(uuid.uuid4())
    offered: list[bool] = []

    async def _sources(_db, _user, *, is_admin=None):
        offered.append(is_admin)
        return {"agents": [{"value": visible, "label": "Mine"}]}

    monkeypatch.setattr(flow_access, "catalog_sources", _sources)
    admin = _admin()

    issues = await check_three_tier_references(object(), admin, graph=_agent_graph(visible, hidden))
    assert [(i["code"], i["node_id"], i["field"]) for i in issues] == (
        [("reference.outside_tiers", "n1", "agent_id")] if strict else []
    )
    assert offered == ([False] if strict else [])
    # A save re-checks only what it adds: an id the flow already held stays.
    assert await check_three_tier_references(
        object(), admin, graph=_agent_graph(visible, hidden), previous_graph=_agent_graph(hidden)
    ) == []
    member = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    assert await check_three_tier_references(object(), member, graph=_agent_graph(hidden)) == []


def test_the_flow_builder_checks_the_references_it_saves_and_runs():
    import inspect

    from app.services.builtin_tools import flow_builder

    source = inspect.getsource(flow_builder.build_flow_builder_tool)
    for function in ("_integration_refusals", "_run_flow"):
        body = source.split(f"async def {function}(", 1)[1].split("\n    async def ", 1)[0]
        assert "check_three_tier_references(" in body, function


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
async def test_the_trace_tools_take_the_bypass_through_the_helper(monkeypatch, strict):
    """The cross-agent trace listing and a colleague agent's trace payload are
    an admin's agent's by default; in strict mode the listing is refused and the
    trace store reads the admin as a member (its own visibility applies)."""
    from app.services.builtin_tools import traces as traces_module
    from app.tools.base import ToolCall

    _strict(monkeypatch, strict)
    admin = _admin()
    fetched: list[dict] = []

    async def _fetch(_db, **kwargs):
        fetched.append(kwargs)
        return "{}"

    monkeypatch.setattr(traces_module, "async_session", lambda: _Session())
    monkeypatch.setattr(traces_module, "_load_user_context", AsyncMock(return_value=(admin, [])))
    monkeypatch.setattr(traces_module, "_fetch_traces", _fetch)
    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(admin.id), agent_id=str(uuid.uuid4()), allow_cross_agent=True
    )

    listing = await executor.execute_tool(ToolCall(id="1", name="inspect_agent_traces", arguments={}))
    assert ("AGENT_TOOLS_STRICT_ADMIN_ACCESS is set" in listing.content) is strict
    if strict:
        assert fetched == []
    else:
        [kwargs] = fetched
        assert kwargs["agent_ids"] is None and kwargs["user"].is_admin is True

    readers: list = []
    trace = SimpleNamespace(id=uuid.uuid4(), agent_id=uuid.uuid4(), input=None, output="out", metadata_json=None)

    class _Store:
        async def get_trace_detail(self, _db, *, user, **_kwargs):
            readers.append(user)
            return trace, None, None, None

    monkeypatch.setattr(traces_module, "build_trace_read_store", _Store)
    # The colleague's agent still exists: its trace is never read as a deleted agent's.
    monkeypatch.setattr(traces_module, "async_session", lambda: _Session([[trace.agent_id]]))
    payload = await executor.execute_tool(
        ToolCall(id="2", name=traces_module.TRACE_PAYLOAD_TOOL_NAME, arguments={"trace_id": str(trace.id)})
    )
    assert ("not allowed to read payloads" in payload.content) is strict
    assert [reader.is_admin for reader in readers] == [not strict]


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
@pytest.mark.usefixtures("evaluation_enabled")
async def test_the_evaluation_tools_take_the_bypass_through_the_helper(monkeypatch, strict):
    """The admin-only evaluation tools read every evaluation by default and the
    admin's three tiers in strict mode, where ``trigger_evaluation`` refuses an
    evaluation outside them - or one running an agent outside them - before the
    REST route, which bypasses by role."""
    from app.api.v1 import evaluations as evaluations_api
    from app.services.builtin_tools import evaluations as evaluations_module
    from app.tools.base import ToolCall

    _strict(monkeypatch, strict)
    admin = _admin()
    monkeypatch.setattr(evaluations_module, "_load_user", AsyncMock(return_value=(admin, [])))
    triggered: list[str] = []

    async def _trigger(evaluation_id, _body, *, user, db):
        triggered.append(evaluation_id)
        return []

    monkeypatch.setattr(evaluations_api, "trigger_evaluation", _trigger)
    _defs, executor = evaluations_module.build_evaluation_tools(db=object(), user_id=str(admin.id))

    session = _Session([[]])
    monkeypatch.setattr(evaluations_module, "async_session", lambda: session)
    await executor.execute_tool(ToolCall(id="1", name="list_evaluations", arguments={}))
    assert ("evaluations.user_id" in _where(session.statements[0])) is strict

    async def _trigger_on(answers):
        monkeypatch.setattr(evaluations_module, "async_session", lambda: _Session(answers))
        triggered.clear()
        evaluation_id = str(uuid.uuid4())
        result = await executor.execute_tool(
            ToolCall(id="2", name="trigger_evaluation", arguments={"evaluation_id": evaluation_id})
        )
        return result.content, triggered == [evaluation_id]

    evaluation, agent_id = SimpleNamespace(id=uuid.uuid4()), uuid.uuid4()
    # The evaluation outside the tiers; inside them, but running an agent
    # outside them (0 of its 1 agent visible); inside them with its agent.
    content, ran = await _trigger_on([None])
    assert (content == "Error: evaluation not found.", ran) == (strict, not strict)
    # Its subject links (#8257): one agent.
    links = [SimpleNamespace(agent_id=agent_id, flow_id=None)]
    content, ran = await _trigger_on([evaluation, links, 0])
    assert ("runs agents outside your own" in content, ran) == (strict, not strict)
    _content, ran = await _trigger_on([evaluation, links, 1])
    assert ran
    # A flow subject outside the tiers is named as a flow.
    flow_links = [SimpleNamespace(agent_id=None, flow_id=uuid.uuid4())]
    content, ran = await _trigger_on([evaluation, flow_links, 0])
    assert ("runs flows outside your own" in content, ran) == (strict, not strict)

    runs = [
        SimpleNamespace(id=uuid.uuid4(), evaluation_id=evaluation.id, agent_id=agent_id, flow_id=None, status="completed")
        for _ in range(2)
    ]
    session = _Session([runs, None])
    monkeypatch.setattr(evaluations_module, "async_session", lambda: session)
    compared = await executor.execute_tool(
        ToolCall(
            id="3",
            name="compare_evaluation_runs",
            arguments={"baseline_run_id": str(runs[0].id), "candidate_run_id": str(runs[1].id)},
        )
    )
    assert "not allowed to read this evaluation" in compared.content
    assert ("evaluations.user_id" in _where(session.statements[1])) is strict


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
@pytest.mark.usefixtures("evaluation_enabled")
async def test_an_evaluation_the_tiers_show_hides_the_agents_they_do_not_in_strict_mode(monkeypatch, strict):
    """An evaluation inside the admin's tiers may run a colleague's private
    agent: in strict mode its agent list and runs leave that agent out, and a
    run of it is refused, as the trigger pre-check refuses to start it."""
    from datetime import UTC, datetime

    from app.services.builtin_tools import evaluations as evaluations_module
    from app.tools.base import ToolCall

    _strict(monkeypatch, strict)
    admin = _admin()
    monkeypatch.setattr(evaluations_module, "_load_user", AsyncMock(return_value=(admin, [])))
    _defs, executor = evaluations_module.build_evaluation_tools(db=object(), user_id=str(admin.id))
    now = datetime(2026, 9, 1, tzinfo=UTC)
    evaluation = SimpleNamespace(
        id=uuid.uuid4(), name="Eval", description="", enabled=True, execution_mode="sequential",
        run_mode="all", use_agent_default_model=True, model_ids=[], disabled_model_ids=[],
        quality_source="judge", timeout_seconds=60, last_triggered_at=None, created_at=now, updated_at=now, apply_prompt_guards=False, require_tool_approval=False,
    )

    async def _call(tool, answers, **arguments):
        session = _Session(answers)
        monkeypatch.setattr(evaluations_module, "async_session", lambda: session)
        result = await executor.execute_tool(ToolCall(id="1", name=tool, arguments=arguments))
        return result.content, session

    _content, session = await _call("get_evaluation", [evaluation, []], evaluation_id=str(evaluation.id))
    assert ("agents.user_id" in _where(session.statements[1])) is strict
    _content, session = await _call("list_evaluation_runs", [evaluation, []], evaluation_id=str(evaluation.id))
    assert ("agents.user_id" in _where(session.statements[1])) is strict

    run = SimpleNamespace(id=uuid.uuid4(), evaluation_id=evaluation.id, agent_id=uuid.uuid4(), flow_id=None)
    content, _session = await _call("get_evaluation_run_values", [run, evaluation, None, []], run_id=str(run.id))
    assert ("not allowed to read this evaluation run" in content) is strict


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
async def test_the_xtm_mcp_server_resolves_knowledge_bases_and_skills_through_the_helper(monkeypatch, strict):
    from app.api.v1 import mcp_xtm

    _strict(monkeypatch, strict)
    admin = _admin()
    db = _Session([[], []])
    await mcp_xtm._resolve_knowledge_bases(db, admin, [])
    await mcp_xtm._resolve_skill_ids(db, admin, [])
    assert ("knowledge_bases.user_id" in _where(db.statements[0])) is strict
    assert ("agent_skills.user_id" in _where(db.statements[1])) is strict
