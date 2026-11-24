"""Tests for the agent-builder run history tools and admin-wide listing.

Covers the additions for platform-wide observability from the builder tool
surface:

* ``list_runs`` / ``get_run`` — assignment run (execution) history with the
  same visibility contract as the REST runs API (own + company-managed runs,
  admin may open/list any run),
* ``list_agents`` pagination (``limit`` / ``offset``) and the admin-only
  ``scope='all'`` platform-wide listing,
* graceful refusals: anonymous callers, non-admin ``scope='all'`` requests,
  invalid pagination / status / scope arguments.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.api.v1.helpers as api_helpers
import app.core.database as database
import app.services.builtin_tools as builtin_tools_module
from app.tools.base import ToolCall

build_agent_builder_tool = builtin_tools_module.build_agent_builder_tool


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value

    def all(self):
        return list(self._value or [])

    def fetchall(self):
        return list(self._value or [])

    def scalars(self):
        return _Scalars(self._value)


class _Scalars:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value

    def all(self):
        if self._value is None:
            return []
        if isinstance(self._value, (list, tuple)):
            return list(self._value)
        return [self._value]


class _FakeSession:
    """Pops pre-baked results from ``execute_results`` (None once empty)."""

    def __init__(self, created, *, execute_results=None):
        self._created = created
        self._created.append(self)
        self._execute_results = list(execute_results or [])
        self.executed = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        self.closed = True
        return False

    async def execute(self, _stmt):
        self.executed += 1
        value = (
            self._execute_results.pop(0) if self._execute_results else None
        )
        if isinstance(value, BaseException):
            raise value
        return _Result(value)


def _session_factory(created, **kwargs):
    def _make():
        return _FakeSession(created, **kwargs)

    return _make


def _stub_caller(monkeypatch, *, is_admin):
    fake = SimpleNamespace(
        id=uuid.uuid4(), role="admin" if is_admin else "user", is_admin=is_admin
    )

    async def _fake(_db, _uid):
        return fake

    monkeypatch.setattr(
        builtin_tools_module, "_load_caller_user_for_builder", _fake
    )
    return fake


def _stub_group_ids(monkeypatch):
    async def _fake(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _fake)


def _forbid_group_ids(monkeypatch):
    """Fail the test if the group-membership query is executed."""

    async def _fail(_db, _uid):
        raise AssertionError(
            "get_user_group_ids must not be called on this path"
        )

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _fail)


def _forbid_caller_load(monkeypatch):
    """Fail the test if the caller ``User`` row is loaded."""

    async def _fail(_db, _uid):
        raise AssertionError(
            "_load_caller_user_for_builder must not be called on this path"
        )

    monkeypatch.setattr(
        builtin_tools_module, "_load_caller_user_for_builder", _fail
    )


def _record_user_visible_filter(monkeypatch):
    """Wrap the real ``user_visible_filter`` and record its ``is_admin`` arg."""
    recorded = {}
    real = api_helpers.user_visible_filter

    def _recorder(model, user_id, group_ids=None, *, is_admin=False):
        recorded["is_admin"] = is_admin
        return real(model, user_id, group_ids, is_admin=is_admin)

    monkeypatch.setattr(api_helpers, "user_visible_filter", _recorder)
    return recorded


def _record_run_visible_filter(monkeypatch):
    """Wrap the real ``run_visible_filter`` and record its invocations."""
    calls = []
    real = api_helpers.run_visible_filter

    def _recorder(user_id):
        calls.append(user_id)
        return real(user_id)

    monkeypatch.setattr(api_helpers, "run_visible_filter", _recorder)
    return calls


def _fake_agent(name, *, user_id=None, enabled=True, company=False, **extra):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        user_id=user_id,
        enabled=enabled,
        is_company_managed=company,
        description=f"{name} description",
        **extra,
    )


def _fake_run(
    *,
    agent_id=None,
    assignment_id=None,
    status="completed",
    error=None,
    result_summary="All done.",
    stop_reason=None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id or uuid.uuid4(),
        assignment_id=assignment_id,
        user_id=uuid.uuid4(),
        is_company_managed=False,
        parent_run_id=None,
        trigger_context={"type": "manual"},
        status=status,
        result_summary=result_summary,
        error=error,
        stop_reason=stop_reason,
        retry_count=0,
        actions_taken=[],
        human_input_question=None,
        human_input_response=None,
        total_input_tokens=1200,
        total_output_tokens=340,
        created_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        started_at=datetime(2026, 7, 1, 10, 0, 5, tzinfo=UTC),
        completed_at=datetime(2026, 7, 1, 10, 1, 5, tzinfo=UTC),
    )


async def _call(executor, tool_name, /, **arguments):
    return await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=tool_name, arguments=arguments)
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_run_tools_registered_and_defined():
    definitions, executor = build_agent_builder_tool(
        SimpleNamespace(), str(uuid.uuid4())
    )
    defined = {d.name for d in definitions}
    registered = set(executor.tool_names)
    for name in ("list_runs", "get_run"):
        assert name in defined, f"missing ToolDefinition for {name}"
        assert name in registered, f"missing executor registration for {name}"


def test_list_agents_schema_exposes_scope_and_pagination():
    definitions, _ = build_agent_builder_tool(
        SimpleNamespace(), str(uuid.uuid4())
    )
    schema = next(d for d in definitions if d.name == "list_agents").input_schema
    props = schema["properties"]
    assert {"search", "scope", "limit", "offset", "detail"} <= set(props)
    # scope / detail must stay opt-in: nothing may be required.
    assert schema["required"] == []


def test_list_runs_schema_exposes_filters_and_pagination():
    definitions, _ = build_agent_builder_tool(
        SimpleNamespace(), str(uuid.uuid4())
    )
    schema = next(d for d in definitions if d.name == "list_runs").input_schema
    props = schema["properties"]
    assert {
        "agent_id",
        "assignment_id",
        "status",
        "search",
        "scope",
        "limit",
        "offset",
    } <= set(props)
    assert schema["required"] == []


def test_list_runs_status_description_matches_engine_vocabulary():
    """The schema's status description is built from the engine's canonical
    vocabulary — it must list every accepted status (including the reserved
    'skipped' / 'cancelled') so the description can never drift from the
    validation."""
    from app.services.assignment_engine import (
        ACTIVE_RUN_STATUSES,
        QUEUED_RUN_STATUS,
        TERMINAL_RUN_STATUSES,
    )

    definitions, _ = build_agent_builder_tool(
        SimpleNamespace(), str(uuid.uuid4())
    )
    schema = next(d for d in definitions if d.name == "list_runs").input_schema
    description = schema["properties"]["status"]["description"]
    for status in (*ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS, *TERMINAL_RUN_STATUSES):
        assert f"'{status}'" in description


# ---------------------------------------------------------------------------
# list_agents — scope + pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_agents_scope_all_refused_for_non_admin(monkeypatch):
    created = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _stub_caller(monkeypatch, is_admin=False)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_agents", scope="all")

    assert not result.is_error
    assert "Only admins can list ALL agents" in result.content


@pytest.mark.asyncio
async def test_list_agents_scope_all_refused_for_anonymous(monkeypatch):
    created = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), "anonymous")
    result = await _call(executor, "list_agents", scope="all")

    assert not result.is_error
    assert "Only admins can list ALL agents" in result.content


@pytest.mark.asyncio
async def test_list_agents_invalid_scope_rejected(monkeypatch):
    created = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_agents", scope="everything")

    assert not result.is_error
    assert "Invalid scope" in result.content
    # Rejected before any DB session is opened.
    assert created == []


@pytest.mark.asyncio
async def test_list_agents_invalid_limit_rejected(monkeypatch):
    created = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_agents", limit="lots")

    assert not result.is_error
    assert "limit must be an integer" in result.content
    assert created == []


@pytest.mark.asyncio
async def test_list_agents_scope_all_lists_platform_wide(monkeypatch):
    owner_id = uuid.uuid4()
    agent_a = _fake_agent("Alpha", user_id=owner_id)
    agent_b = _fake_agent("Beta", company=True)

    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[
                2,  # total count
                [agent_a, agent_b],  # page
                [],  # assignment counts
            ],
        ),
    )
    _stub_caller(monkeypatch, is_admin=True)
    # The is_admin bypass ignores group ids — the membership query must be
    # skipped entirely on the admin-wide path.
    _forbid_group_ids(monkeypatch)
    recorded = _record_user_visible_filter(monkeypatch)

    async def _fake_owner_names(_db, _entities):
        return {str(owner_id): "Alice Admin"}

    monkeypatch.setattr(api_helpers, "resolve_owner_names", _fake_owner_names)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_agents", scope="all")

    assert not result.is_error
    assert "Found 2 agent(s)" in result.content
    assert "all agents (admin)" in result.content
    # Admin-wide scope must flow through the shared is_admin bypass —
    # never a hand-rolled unfiltered query.
    assert recorded["is_admin"] is True
    # Owners are surfaced so "agents I don't own" are identifiable.
    assert "owner: Alice Admin" in result.content
    assert "[company-managed]" in result.content


@pytest.mark.asyncio
async def test_list_agents_default_scope_keeps_three_tier_filter(monkeypatch):
    agent = _fake_agent("Mine")
    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[1, [agent], []],
        ),
    )
    _stub_group_ids(monkeypatch)
    recorded = _record_user_visible_filter(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_agents")

    assert not result.is_error
    assert "Found 1 agent(s)" in result.content
    assert recorded["is_admin"] is False
    assert "owner:" not in result.content


@pytest.mark.asyncio
async def test_list_agents_paginates_and_reports_remaining(monkeypatch):
    agent = _fake_agent("Page One")
    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[3, [agent], []],
        ),
    )
    _stub_group_ids(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_agents", limit=1)

    assert not result.is_error
    assert "Found 3 agent(s)" in result.content
    assert "showing 1-1" in result.content
    assert "2 more agent(s)" in result.content
    assert "offset=1" in result.content


@pytest.mark.asyncio
async def test_list_agents_accepts_integral_float_page_args(monkeypatch):
    """JSON numbers may arrive as floats (``1.0``) and numeric strings may be
    float-shaped (``"1.0"``) — integral values must be accepted in both
    forms; fractional ones are still rejected."""
    agent = _fake_agent("Float Page")
    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[1, [agent], []],
        ),
    )
    _stub_group_ids(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_agents", limit=1.0, offset=0.0)

    assert not result.is_error
    assert "Found 1 agent(s)" in result.content

    as_string = await _call(executor, "list_agents", limit="1.0", offset="0")
    assert not as_string.is_error
    assert "Found 1 agent(s)" in as_string.content

    fractional = await _call(executor, "list_agents", limit=1.5)
    assert "limit must be an integer" in fractional.content

    fractional_str = await _call(executor, "list_agents", limit="1.5")
    assert "limit must be an integer" in fractional_str.content


@pytest.mark.asyncio
async def test_list_agents_invalid_detail_rejected(monkeypatch):
    created = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_agents", detail="everything")

    assert not result.is_error
    assert "Invalid detail" in result.content
    # Rejected before any DB session is opened.
    assert created == []


@pytest.mark.asyncio
async def test_list_agents_full_detail_batches_resource_resolution(monkeypatch):
    """``detail='full'`` must surface owner / created / bindings / KBs /
    exposure per agent with page-level batched lookups (2 extra queries for
    the whole page) — never one ``get_agent``-style round-trip per agent."""
    owner_id = uuid.uuid4()
    kb_a, kb_b = uuid.uuid4(), uuid.uuid4()
    integ_a, integ_b = uuid.uuid4(), uuid.uuid4()
    agent_a = _fake_agent(
        "Alpha",
        user_id=owner_id,
        created_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        integration_bindings={"microsoft": [str(integ_a)]},
        knowledge_base_ids=[str(kb_a)],
        web_enabled=True,
        available_channel_types=None,
        is_public_catalog=False,
    )
    agent_b = _fake_agent(
        "Beta",
        user_id=owner_id,
        created_at=datetime(2026, 6, 2, 9, 0, tzinfo=UTC),
        integration_bindings={"hibob": [str(integ_b)]},
        knowledge_base_ids=[str(kb_b)],
        web_enabled=False,
        available_channel_types=["slack"],
        is_public_catalog=True,
    )

    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[
                2,  # total count
                [agent_a, agent_b],  # page
                [],  # assignment counts
                [(kb_a, "Docs KB"), (kb_b, "HR KB")],  # KB names (batched)
                [
                    (integ_a, "[Sam] Microsoft"),
                    (integ_b, "[Filigran] HiBob (SENSITIVE)"),
                ],  # integration names (batched)
            ],
        ),
    )
    _stub_group_ids(monkeypatch)

    async def _fake_owner_names(_db, _entities):
        return {str(owner_id): "Alice Owner"}

    monkeypatch.setattr(api_helpers, "resolve_owner_names", _fake_owner_names)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_agents", detail="full")

    assert not result.is_error
    assert "full detail" in result.content
    # Owner surfaced even outside the admin scope — reconciliation keys
    # rows on (name, owner).
    assert "owner: Alice Owner" in result.content
    assert "Created: 2026-06-01" in result.content
    # Generic provider display name + bound instance name.
    assert "Microsoft [`microsoft`]: [Sam] Microsoft" in result.content
    assert "HiBob [`hibob`]: [Filigran] HiBob (SENSITIVE)" in result.content
    assert "Docs KB" in result.content and "HR KB" in result.content
    assert "web widget: Yes; channels: All channels" in result.content
    assert (
        "web widget: No; channels: slack; public catalog: Yes"
        in result.content
    )
    # Exactly 5 queries for the whole page: count, page, assignment counts,
    # KB names, integration names — no per-agent lookups.
    assert created[0].executed == 5


@pytest.mark.asyncio
async def test_list_agents_full_detail_hides_invisible_resource_names(
    monkeypatch,
):
    """A visible agent row may reference resources the caller cannot see —
    full detail must show only the bare id, never resolve the name (same
    gate as ``get_agent``)."""
    hidden_integ = uuid.uuid4()
    hidden_kb = uuid.uuid4()
    agent = _fake_agent(
        "Opaque",
        user_id=uuid.uuid4(),
        integration_bindings={"notion": [str(hidden_integ)]},
        knowledge_base_ids=[str(hidden_kb)],
        web_enabled=False,
        available_channel_types=None,
    )

    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[
                1,  # total count
                [agent],  # page
                [],  # assignment counts
                [],  # KB names — none visible
                [],  # integration names — none visible
            ],
        ),
    )
    _stub_group_ids(monkeypatch)

    async def _fake_owner_names(_db, _entities):
        return {}

    monkeypatch.setattr(api_helpers, "resolve_owner_names", _fake_owner_names)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_agents", detail="full")

    assert not result.is_error
    # Generic provider name comes from the static catalog (no leak), but the
    # per-instance binding falls back to the bare id.
    assert f"Notion [`notion`]: `{hidden_integ}`" in result.content
    assert f"`{hidden_kb}`" in result.content


@pytest.mark.asyncio
async def test_list_agents_summary_skips_detail_queries(monkeypatch):
    """The default summary mode must not pay for the detail lookups."""
    agent = _fake_agent("Mine")
    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[1, [agent], []]),
    )
    _stub_group_ids(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_agents")

    assert not result.is_error
    assert "Integrations:" not in result.content
    # count + page + assignment counts only.
    assert created[0].executed == 3


@pytest.mark.asyncio
async def test_get_agent_admin_fallback_reads_invisible_agent(monkeypatch):
    """An admin must be able to inspect an agent outside their three-tier
    visibility (REST ``GET /agents/{id}`` parity) — otherwise an agent found
    via ``list_agents(scope='all')`` could not be opened."""
    _stub_group_ids(monkeypatch)
    _stub_caller(monkeypatch, is_admin=True)

    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Private Agent",
        slug="private-agent",
        description="desc",
        persona="You are private.",
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
    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[
                None,  # visibility-filtered agent SELECT (miss)
                agent,  # admin-bypass agent SELECT
                0,  # assignment count
            ],
        ),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_agent", agent_id=str(agent.id))

    assert not result.is_error
    assert "Private Agent" in result.content


@pytest.mark.asyncio
async def test_get_agent_non_admin_still_hidden_on_visibility_miss(monkeypatch):
    _stub_group_ids(monkeypatch)
    _stub_caller(monkeypatch, is_admin=False)

    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[None]),
    )

    agent_id = str(uuid.uuid4())
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_agent", agent_id=agent_id)

    assert not result.is_error
    assert "not found or you don't have access" in result.content


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_runs_refused_for_anonymous(monkeypatch):
    created = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), "anonymous")
    result = await _call(executor, "list_runs")

    assert not result.is_error
    assert "not available for anonymous users" in result.content
    assert created == []


@pytest.mark.asyncio
async def test_list_runs_scope_all_refused_for_non_admin(monkeypatch):
    created = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _stub_caller(monkeypatch, is_admin=False)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_runs", scope="all")

    assert not result.is_error
    assert "Only admins can list ALL runs" in result.content


@pytest.mark.asyncio
async def test_list_runs_invalid_status_rejected(monkeypatch):
    created = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_runs", status="exploded")

    assert not result.is_error
    assert "Invalid status" in result.content
    assert created == []


@pytest.mark.asyncio
async def test_list_runs_accepts_reserved_statuses(monkeypatch):
    """The status vocabulary is derived from the engine's canonical
    ``ACTIVE_RUN_STATUSES`` / ``TERMINAL_RUN_STATUSES`` — the reserved
    'skipped' / 'cancelled' states must be accepted, not rejected."""
    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[0]),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_runs", status="cancelled")

    assert not result.is_error
    assert result.content == "No runs found."


@pytest.mark.asyncio
async def test_list_runs_accepts_queued_status(monkeypatch):
    """A queued run (a delivery waiting for a busy assignment) is neither
    active nor terminal: the filter must accept 'queued' instead of refusing
    it, so queued runs are listable rather than a silent zero."""
    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[0]),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_runs", status="queued")

    assert not result.is_error
    assert "Invalid status" not in result.content
    assert result.content == "No runs found."


@pytest.mark.asyncio
async def test_list_runs_invalid_agent_id_rejected(monkeypatch):
    created = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_runs", agent_id="not-a-uuid")

    assert not result.is_error
    assert "agent_id must be a valid UUID" in result.content
    assert created == []


@pytest.mark.asyncio
async def test_list_runs_default_scope_applies_run_visibility(monkeypatch):
    user_id = str(uuid.uuid4())
    agent_id = uuid.uuid4()
    wf_id = uuid.uuid4()
    run = _fake_run(agent_id=agent_id, assignment_id=wf_id)

    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[
                1,  # total count
                [run],  # page
                [(agent_id, "Mail Desk")],  # agent names
                [(wf_id, "Daily digest")],  # assignment names
            ],
        ),
    )
    calls = _record_run_visible_filter(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), user_id)
    result = await _call(executor, "list_runs")

    assert not result.is_error
    # The shared run-visibility predicate (own + company-managed) must be
    # applied for the default scope.
    assert calls == [uuid.UUID(user_id)]
    assert "Found 1 run(s)" in result.content
    assert "Mail Desk" in result.content
    assert "Daily digest" in result.content
    assert str(run.id) in result.content
    assert "Status: completed" in result.content
    assert "Summary: All done." in result.content
    assert "get_run" in result.content


@pytest.mark.asyncio
async def test_list_runs_scope_all_admin_bypasses_run_visibility(monkeypatch):
    agent_id = uuid.uuid4()
    run = _fake_run(agent_id=agent_id, status="failed", error="boom " * 100)

    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[
                1,  # total count
                [run],  # page
                [(agent_id, "Someone else's agent")],  # agent names
            ],
        ),
    )
    _stub_caller(monkeypatch, is_admin=True)
    calls = _record_run_visible_filter(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_runs", scope="all")

    assert not result.is_error
    # Admin scope='all' must NOT apply the own+company-managed predicate.
    assert calls == []
    assert "all runs (admin)" in result.content
    assert "Someone else's agent" in result.content
    # Failed runs surface a bounded error preview.
    assert "Error: boom" in result.content
    assert "…" in result.content


@pytest.mark.asyncio
async def test_list_runs_paginates_and_reports_remaining(monkeypatch):
    agent_id = uuid.uuid4()
    run = _fake_run(agent_id=agent_id)

    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[
                5,  # total count
                [run],  # page
                [(agent_id, "Mail Desk")],  # agent names
            ],
        ),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_runs", limit=1, offset=2)

    assert not result.is_error
    assert "Found 5 run(s)" in result.content
    assert "showing 3-3" in result.content
    assert "2 more run(s)" in result.content
    assert "offset=3" in result.content


# ---------------------------------------------------------------------------
# get_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_invalid_uuid_rejected(monkeypatch):
    created = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_run", run_id="nope")

    assert not result.is_error
    assert "run_id must be a valid UUID" in result.content
    assert created == []


@pytest.mark.asyncio
async def test_get_run_refused_for_anonymous(monkeypatch):
    created = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), "anonymous")
    result = await _call(executor, "get_run", run_id=str(uuid.uuid4()))

    assert not result.is_error
    assert "not available for anonymous users" in result.content
    assert created == []


@pytest.mark.asyncio
async def test_get_run_not_found_hides_existence(monkeypatch):
    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[None]),
    )
    _stub_caller(monkeypatch, is_admin=False)
    calls = _record_run_visible_filter(monkeypatch)

    run_id = str(uuid.uuid4())
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_run", run_id=run_id)

    assert not result.is_error
    assert f"Run `{run_id}` not found." == result.content
    # Non-admin reads must be scoped by the shared run-visibility predicate
    # so an invisible run is indistinguishable from a missing one.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_get_run_full_detail_for_owner(monkeypatch):
    agent_id = uuid.uuid4()
    wf_id = uuid.uuid4()
    run = _fake_run(agent_id=agent_id, assignment_id=wf_id)
    run.result_summary = "The quarterly digest was sent to 4 recipients."

    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[
                run,  # visibility-scoped run SELECT (hit)
                "Mail Desk",  # agent name
                "Daily digest",  # assignment name
            ],
        ),
    )
    # The common visible-run path must not pay the caller-row query — the
    # admin fallback is lazy (only on a visibility miss).
    _forbid_caller_load(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_run", run_id=str(run.id))

    assert not result.is_error
    assert f"# Run `{run.id}`" in result.content
    assert "Mail Desk" in result.content
    assert "Daily digest" in result.content
    assert "Status: completed" in result.content
    assert "Trigger: manual" in result.content
    assert "Duration: 60s" in result.content
    assert "Tokens: 1200 in / 340 out" in result.content
    # The full result is surfaced verbatim (never truncated here).
    assert "The quarterly digest was sent to 4 recipients." in result.content
    # A run that ended on its own terms carries no early-stop line.
    assert "Stopped before finishing" not in result.content


@pytest.mark.asyncio
async def test_get_run_says_a_completed_run_stopped_on_a_limit(monkeypatch):
    """A completed run that ended on its iteration cap reads as partial,
    not as the finished task its ``completed`` status suggests."""
    run = _fake_run(stop_reason="max_iterations")
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory([], execute_results=[run, "Mail Desk"]),
    )
    _forbid_caller_load(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_run", run_id=str(run.id))

    assert not result.is_error
    lines = result.content.splitlines()
    assert "Status: completed" in lines
    assert "Stopped before finishing: max_iterations (partial result)" in lines


@pytest.mark.asyncio
async def test_get_run_names_a_timeout_stop_and_the_run_budget(monkeypatch):
    """A failed run its timeout stopped says so, with the budget it had and
    its queue wait read off its execution (#1695)."""
    wf_id = uuid.uuid4()
    run = _fake_run(assignment_id=wf_id, status="failed", stop_reason="timeout")
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            [],
            execute_results=[
                run,
                "Mail Desk",
                "Daily digest",
                {"start_budget_seconds": 3120.0, "queue_wait_seconds": 42.7},
            ],
        ),
    )
    _forbid_caller_load(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_run", run_id=str(run.id))

    assert not result.is_error
    lines = result.content.splitlines()
    assert "Status: failed" in lines
    assert "Stopped by: timeout (the run's wall-clock bound fired)" in lines
    assert (
        "Budget: 3120s from its start (effective timeout, floor applied, plus grace); "
        "waited 42s for a worker (not charged)"
    ) in lines
    assert "partial result" not in result.content


@pytest.mark.asyncio
async def test_get_run_admin_falls_back_on_visibility_miss(monkeypatch):
    """An admin may open ANY run (REST run-detail contract). The lookup is
    lazy: the visibility-scoped SELECT runs first, and only a miss triggers
    the caller-row load + unscoped admin retry."""
    run = _fake_run()

    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[
                None,  # visibility-scoped run SELECT (miss)
                run,  # admin-bypass run SELECT
                "Agent X",  # agent name
            ],
        ),
    )
    _stub_caller(monkeypatch, is_admin=True)
    calls = _record_run_visible_filter(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_run", run_id=str(run.id))

    assert not result.is_error
    # Exactly one scoped attempt — the admin retry is unscoped.
    assert len(calls) == 1
    assert f"# Run `{run.id}`" in result.content


# ---------------------------------------------------------------------------
# Deep run search
# ---------------------------------------------------------------------------


def test_list_runs_search_description_documents_deep_fields():
    """The search now covers run content (trigger payload, actions, error)
    — the schema must say so, and must point at get_run for conversation-log
    search, so the model stops burning iterations on search variants."""
    definitions, _ = build_agent_builder_tool(
        SimpleNamespace(), str(uuid.uuid4())
    )
    schema = next(d for d in definitions if d.name == "list_runs").input_schema
    description = schema["properties"]["search"]["description"]
    assert "trigger payload" in description
    assert "actions" in description
    assert "get_run" in description


def test_get_run_schema_exposes_search():
    definitions, _ = build_agent_builder_tool(
        SimpleNamespace(), str(uuid.uuid4())
    )
    schema = next(d for d in definitions if d.name == "get_run").input_schema
    assert "search" in schema["properties"]
    # search must stay opt-in.
    assert schema["required"] == ["run_id"]


@pytest.mark.asyncio
async def test_list_runs_search_miss_returns_guidance(monkeypatch):
    """A search miss must return actionable guidance (what was searched,
    what to do next) instead of a bare 'No runs found.' that sends the
    model into a loop of search-term variants."""
    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[[]]),  # empty page
    )
    _record_run_visible_filter(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_runs", search="harfanglab")

    assert not result.is_error
    assert "No runs matched search `harfanglab`" in result.content
    assert "trigger payload" in result.content
    assert "get_run" in result.content
    assert "never received" in result.content
    # Search mode must NOT pay for an exact COUNT(*) — the deep-search
    # ILIKE predicates over the JSON casts would turn it into a second
    # full scan per call. One page query only.
    assert created[0].executed == 1


class _QueryCanceledError(Exception):
    """Stands in for asyncpg's QueryCanceledError (SQLSTATE 57014)."""

    sqlstate = "57014"


@pytest.mark.asyncio
async def test_list_runs_search_statement_timeout_returns_guidance(monkeypatch):
    """A search cancelled by the database statement timeout answers how to
    narrow it (the filters not set yet, get_run search) instead of raising."""
    from sqlalchemy.exc import DBAPIError

    timeout = DBAPIError("SELECT ...", {}, _QueryCanceledError("canceling statement due to statement timeout"))
    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[timeout]),
    )
    _record_run_visible_filter(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_runs", search="rare-term", status="failed")

    assert not result.is_error
    assert "The search for `rare-term` timed out" in result.content
    assert "Do not retry the same call" in result.content
    assert "add agent_id, assignment_id" in result.content
    assert "status" not in result.content.split("Narrow it")[1].split(")")[0]
    assert "get_run" in result.content


@pytest.mark.asyncio
async def test_list_runs_search_other_database_error_still_raises(monkeypatch):
    """Only a statement timeout becomes guidance; any other error raises."""
    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[RuntimeError("connection lost")]),
    )
    _record_run_visible_filter(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_runs", search="term")

    assert result.is_error
    assert "timed out" not in result.content


@pytest.mark.asyncio
async def test_list_runs_whitespace_search_is_not_search_mode(monkeypatch):
    """A whitespace-only search term must be normalized away — otherwise it
    enters search mode and ILIKE-matches every row (an expensive scan with
    misleading 'matches'). It must behave exactly like no search: exact
    COUNT(*) path, plain 'No runs found.' on empty."""
    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[0]),  # count = 0
    )
    _record_run_visible_filter(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_runs", search="   ")

    assert not result.is_error
    assert result.content == "No runs found."


@pytest.mark.asyncio
async def test_list_runs_search_reports_has_more_without_count(monkeypatch):
    """Search mode fetches ``limit + 1`` rows instead of running COUNT(*):
    the header shows ``N+`` and the footer offers the next offset."""
    agent_id = uuid.uuid4()
    run_a = _fake_run(agent_id=agent_id)
    run_b = _fake_run(agent_id=agent_id)

    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[
                [run_a, run_b],  # page (limit+1 rows -> has more)
                [(agent_id, "Mail Desk")],  # agent names
            ],
        ),
    )
    _record_run_visible_filter(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_runs", search="digest", limit=1)

    assert not result.is_error
    assert "Found 1+ run(s) matching `digest`" in result.content
    assert "showing 1-1" in result.content
    # Only the extra fetched row signals more pages — no exact remainder.
    assert "More matching run(s) exist" in result.content
    assert "offset=1" in result.content
    # The overflow row must not be rendered.
    assert str(run_a.id) in result.content
    assert str(run_b.id) not in result.content


@pytest.mark.asyncio
async def test_list_runs_without_search_keeps_plain_no_runs_message(
    monkeypatch,
):
    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[0]),  # count = 0
    )
    _record_run_visible_filter(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_runs")

    assert not result.is_error
    assert result.content == "No runs found."


@pytest.mark.asyncio
async def test_get_run_search_returns_excerpts_from_full_content(monkeypatch):
    """``get_run`` with ``search`` greps the run's FULL content — including
    the trigger payload and conversation log that the detail view never
    dumps — and returns bounded excerpts with context."""
    run = _fake_run()
    run.trigger_payload = {
        "action": "opened",
        "pull_request": {"title": "feat(integrations): add HarfangLab EDR"},
        "number": 2390,
    }
    run.messages = [
        {"role": "user", "content": "Label the new PR."},
        {
            "role": "assistant",
            "content": "Skipping the HarfangLab PR because it already has labels.",
        },
    ]

    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[run]),
    )
    _forbid_caller_load(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "get_run", run_id=str(run.id), search="harfanglab"
    )

    assert not result.is_error
    assert f"in run `{run.id}`" in result.content
    assert "trigger_payload" in result.content
    assert "HarfangLab EDR" in result.content
    assert "messages[1] (assistant)" in result.content
    assert "already has labels" in result.content


@pytest.mark.asyncio
async def test_get_run_search_miss_says_what_was_searched(monkeypatch):
    run = _fake_run(result_summary="Nothing relevant here.")
    run.trigger_payload = {"number": 42}
    run.messages = []

    created = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[run]),
    )
    _forbid_caller_load(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "get_run", run_id=str(run.id), search="harfanglab"
    )

    assert not result.is_error
    assert "No matches for `harfanglab`" in result.content
    assert "conversation" in result.content


def test_cross_run_search_payload_expression_excludes_webhook_meta():
    """The ``list_runs`` trigger-payload predicate must subtract
    ``_webhook_meta`` before matching, aligning cross-run search with the
    in-run "safe to surface" contract (internal routing metadata can
    neither produce false positives nor be probed via search)."""
    from sqlalchemy.dialects import postgresql

    from app.services.builtin_tools.agent_builder import (
        _searchable_trigger_payload,
    )

    sql = str(
        _searchable_trigger_payload().compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "_webhook_meta" in sql
    assert "JSONB" in sql
    assert "trigger_payload" in sql
    # The subtraction must happen BEFORE the final text cast that ILIKE
    # matches against.
    assert sql.index("-") < sql.rindex("AS TEXT")


def test_cross_run_search_payload_expression_skips_nul_poisoned_rows():
    """A legacy row storing a ``\\u0000`` or lone surrogate escape must yield
    NULL for the trigger-payload predicate — excluded from that one predicate
    but still matchable via agent/assignment name, result summary, error, and
    actions — instead of failing the WHOLE query: the ``json -> jsonb`` parse
    refuses both. The exact test is
    ``core/json_sql.json_unparseable``; its agreement with PostgreSQL's own
    cast is ``test_unparseable_json_rows_postgres.py``."""
    from sqlalchemy.dialects import postgresql

    from app.services.builtin_tools.agent_builder import (
        _searchable_trigger_payload,
    )

    sql = str(
        _searchable_trigger_payload().compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    # The probe gates the jsonb parse: it runs on the raw json text BEFORE
    # anything attempts the escape-resolving jsonb cast.
    assert sql.startswith("CASE WHEN")
    assert "CAST(assignment_runs.trigger_payload AS TEXT)" in sql
    assert "regexp_replace" in sql and "0000" in sql
    assert sql.index("regexp_replace") < sql.index("AS JSONB")
    # Poisoned rows yield NULL — never a fallback to the unparsed payload
    # text, which would leak _webhook_meta back into the search surface: the
    # only other branch is the parsed payload less _webhook_meta.
    assert "THEN NULL ELSE" in sql
    assert sql.count("AS JSONB") == 1
    assert sql.index("THEN NULL ELSE") < sql.index("AS JSONB")


def test_search_run_content_quarantines_untrusted_trigger_excerpts():
    """Excerpts from webhook / integration-event trigger sections carry
    attacker-influenceable external data — they must be wrapped in an
    untrusted-content quarantine block (mirroring
    ``_build_trigger_information``), while trusted sections such as the
    result summary stay unwrapped."""
    from app.services.builtin_tools.agent_builder import _search_run_content

    run = _fake_run(result_summary="Processed the needle event fully.")
    run.trigger_context = {"type": "webhook"}
    run.trigger_payload = {"action": "needle", "ignore me": "please"}
    run.messages = []

    report = _search_run_content(run, "needle")

    assert "<<UNTRUSTED-" in report
    assert "webhook trigger (trigger_payload)" in report
    # The trusted result-summary section is NOT quarantined: its excerpt
    # appears before the first quarantine marker.
    summary_pos = report.index("Processed the needle event fully.")
    assert summary_pos < report.index("<<UNTRUSTED-")

    # Manual runs (user-authored payloads) stay unwrapped.
    manual_run = _fake_run()
    manual_run.trigger_payload = {"action": "needle"}
    manual_run.messages = []
    manual_report = _search_run_content(manual_run, "needle")
    assert "<<UNTRUSTED-" not in manual_report


def test_search_run_content_strips_internal_webhook_metadata():
    """In-run search must honour the same "safe to surface" contract as
    ``_build_trigger_information``: internal routing metadata
    (``trigger_payload._webhook_meta``) and the raw request
    ``headers``/``body`` mirrors in ``trigger_context`` are never scanned
    nor excerpted."""
    from app.services.builtin_tools.agent_builder import _search_run_content

    run = _fake_run()
    run.trigger_payload = {
        "action": "opened",
        "_webhook_meta": {"headers": {"x-github-token": "sekret-needle"}},
    }
    run.trigger_context = {
        "type": "webhook",
        "headers": {"authorization": "Bearer sekret-needle"},
        "body": "raw sekret-needle body",
    }
    run.messages = []

    report = _search_run_content(run, "sekret-needle")

    # No excerpt may surface the stripped values — the only echo of the
    # needle is the miss message quoting the search term itself.
    assert report.startswith("No matches for `sekret-needle`")
    assert "Found" not in report
    assert "x-github-token" not in report
    assert "Bearer" not in report

    # The sanitised remainder is still searchable.
    report_open = _search_run_content(run, "opened")
    assert "trigger_payload" in report_open
    report_type = _search_run_content(run, "webhook")
    assert "trigger_context" in report_type


def test_search_run_content_caps_total_matches():
    """The excerpt report must stay bounded even when the needle appears
    hundreds of times across a long conversation log."""
    from app.services.builtin_tools.agent_builder import (
        _RUN_SEARCH_MAX_TOTAL,
        _search_run_content,
    )

    run = _fake_run()
    run.trigger_payload = None
    run.trigger_context = None
    run.messages = [
        {"role": "assistant", "content": "needle " * 50} for _ in range(50)
    ]

    report = _search_run_content(run, "needle")

    assert f"Found {_RUN_SEARCH_MAX_TOTAL} match(es)" in report
    assert "Stopped after" in report
