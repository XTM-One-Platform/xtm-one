import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.api.v1 import agents as agents_api
from app.api.v1.schemas.agent import UpdateAgentRequest
from app.models.agent import Agent
from app.models.general_assistant_config import MAX_ITERATIONS_CEILING


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        if isinstance(self._value, list):
            return self._value
        return []


class _IterableResult:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def scalar(self):
        return None

    def scalar_one_or_none(self):
        return None


class _FakeDB:
    def __init__(self):
        self._calls = 0
        self.commits = 0

    async def commit(self):
        # ``update_agent`` must commit before it responds: the ``get_db``
        # teardown commit runs after the response is sent, so a caller reading
        # the agent back on its next request could race the transaction.
        self.commits += 1

    async def refresh(self, _obj):
        pass

    async def execute(self, stmt):
        if getattr(stmt, "_for_update_arg", None) is not None or "pg_advisory" in str(stmt):
            # The provider config lock and existence check: the
            # config the payload pins exists.
            return _ScalarResult(uuid.uuid4())
        self._calls += 1
        if self._calls == 1:
            # workflow count query
            return _ScalarResult(0)
        if self._calls == 2:
            # chat count query
            return _ScalarResult(0)
        if self._calls == 3:
            # evaluation id query
            return _IterableResult([])
        return _ScalarResult(None)


class _NoProviderConfigDB(_FakeDB):
    """The provider config lock is granted, but no id names a config."""

    async def execute(self, stmt):
        if getattr(stmt, "_for_update_arg", None) is not None and stmt.whereclause is not None:
            return _ScalarResult(None)
        return await super().execute(stmt)


class _DummyAgent:
    def __init__(self):
        self.id = uuid.uuid4()
        self.user_id = None
        self.template_slug = None
        self.is_builtin = False
        self.is_company_managed = False
        self.is_public_catalog = False
        self.catalog_include_personal_tools = False
        self.catalog_download_includes_personal_tools = False
        self.shared_group_ids = None
        self.shared_group_permissions = None
        self.shared_user_ids = None
        self.shared_user_permissions = None
        self.access_restricted = False
        self.customer_notes = None
        self.customer_fields = None
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)

        self.name = "Original agent"
        self.slug = "original-agent"
        self.description = "desc"
        self.icon = "Bot"
        self.persona = "persona"
        self.output_format = "markdown"

        # Legacy/bad schema can exist in DB; updating another field must not fail.
        self.output_schema = {"type": "not-a-valid-json-schema-type"}

        self.enabled = True
        self.tags = []
        self.integration_bindings = None
        self.integration_lending = None
        self.integration_template_bindings = None
        self.integration_element_bindings = None
        self.integration_element_enforcement = None
        self.integration_allowed_tools = None
        self.mcp_server_ids = None
        self.custom_tool_ids = None
        self.builtin_tool_ids = []
        self.runner_ids = None
        self.runner_labels = None
        self.mcp_allowed_tools = None
        self.tool_deny_list = None
        self.integration_read_only = None
        self.browser_profile_ids = None
        self.browser_profile_read_only = None
        self.prompt_ids = None
        self.skill_ids = None
        self.skill_load_modes = None
        self.knowledge_base_ids = None
        self.knowledge_base_permissions = None
        self.handover_agent_ids = None
        self.handover_types = None
        self.work_item_owner_agent_ids = None
        self.trace_supervision_target_agent_ids = None
        self.handover_conditions = None
        self.allow_self_fanout = True
        self.accept_caller_output_format = False
        self.config = None
        self.llm_provider = None
        self.llm_provider_id = None
        self.llm_model = None
        self.llm_model_source = None
        self.temperature = None
        self.reasoning_effort = None
        self.max_iterations = None
        self.budget_config = None
        self.guardrails = None
        self.prepare_tool_ref = None
        self.finalize_tool_ref = None
        self.skip_llm = False
        self.available_channel_types = None
        self.channel_config = None
        self.memory_short_term_enabled = True
        self.memory_medium_term_enabled = True
        self.memory_persistent_enabled = True
        self.memory_user_enabled = True
        self.human_in_the_loop = False
        self.human_in_the_loop_prompt = None
        self.approve_tool_calls = None
        self.disable_chat = False
        self.web_enabled = False
        self.web_suggested_prompts = None
        self.web_uploads_enabled = False
        self.web_token = None

    def prune_orphaned_integration_scopes(self):
        return None

    def prune_orphaned_skill_load_modes(self):
        return None

    def prune_orphaned_knowledge_base_permissions(self):
        return None


def _response_payload(
    a,
    wf_count,
    chat_count,
    *,
    can_manage,
    can_view_chats,
    owner_name=None,
    owner_email=None,
):
    return {
        "id": str(a.id),
        "name": a.name,
        "wf_count": wf_count,
        "chat_count": chat_count,
        "can_manage": can_manage,
        "can_view_chats": can_view_chats,
        "owner_name": owner_name,
        "owner_email": owner_email,
    }


def _setup_update_agent_mocks(monkeypatch, agent):
    async def _groups(_db, _user_id):
        return []

    async def _get_user_agent(_db, _user_id, _agent_id, _group_ids, **_kwargs):
        return agent

    async def _log_audit(*_args, **_kwargs):
        return None

    async def _record_versions(*_args, **_kwargs):
        return None, {}

    async def _validate_personal_bindings(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agents_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(agents_api, "get_user_agent", _get_user_agent)
    monkeypatch.setattr(
        agents_api,
        "require_admin_for_company_managed",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(agents_api, "log_audit", _log_audit)
    monkeypatch.setattr(agents_api, "record_agent_version_changes", _record_versions)
    monkeypatch.setattr(agents_api, "_to_response", _response_payload)
    monkeypatch.setattr(agents_api, "validate_personal_agent_bindings", _validate_personal_bindings)


async def _invoke_update(
    monkeypatch,
    body: UpdateAgentRequest,
    agent: _DummyAgent | None = None,
    db: _FakeDB | None = None,
):
    fake_db = db or _FakeDB()
    # ``update_agent`` now enforces the full write gate (assert_writable_agent →
    # can_manage). Use an admin caller so the gate passes without needing an
    # owner (the _DummyAgent is ownerless, which keeps the owner-email lookup
    # branch skipped) — these tests exercise field-update mechanics, not the
    # permission boundary (which has its own dedicated tests).
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    current_agent = agent or _DummyAgent()
    _setup_update_agent_mocks(monkeypatch, current_agent)

    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/v1/agents/x",
            "headers": [],
        }
    )

    response = await agents_api.update_agent(
        str(current_agent.id),
        body,
        request,
        user,
        fake_db,
    )
    # The response must describe COMMITTED state. Relying on the ``get_db``
    # teardown commit (which runs after the response is sent) let a caller's
    # immediate read-back race the transaction — the A2A E2E seed binds an
    # integration and reads the card URL straight after, and saw ``exposed:
    # false`` whenever the GET won.
    assert fake_db.commits == 1, "update_agent must commit before responding"
    return current_agent, response


@pytest.mark.asyncio
async def test_update_agent_name_does_not_validate_existing_legacy_output_schema(monkeypatch):
    """Regression test:
    updating unrelated fields (name/description) must succeed even if
    agent.output_schema stored in DB is legacy/invalid.
    """

    agent = _DummyAgent()
    body = UpdateAgentRequest(name="Renamed agent")
    updated_agent, response = await _invoke_update(monkeypatch, body, agent=agent)

    assert response["name"] == "Renamed agent"
    # Critical regression assertion: old/legacy schema is untouched and did not block update.
    assert updated_agent.output_schema == {"type": "not-a-valid-json-schema-type"}
    assert updated_agent.description == "desc"


@pytest.mark.asyncio
async def test_update_agent_full_payload_updates_general_and_tooling_fields(monkeypatch):
    """Before/after parity test:
    a full update payload still updates all previously editable fields.
    """

    agent = _DummyAgent()
    provider_id = uuid.uuid4()

    body = UpdateAgentRequest(
        name="Updated agent",
        description="Updated description",
        icon="Shield",
        slug="updated-agent",
        persona="Updated persona",
        output_format="json",
        output_schema={"type": "object", "properties": {"status": {"type": "string"}}},
        tags=["ops", "prod"],
        llm_model="gpt-5-mini",
        llm_provider_id=str(provider_id),
        temperature=0.4,
        reasoning_effort="medium",
        max_iterations=8,
        disable_chat=True,
        human_in_the_loop=True,
        human_in_the_loop_prompt="Always request approval",
        integration_bindings={"github": [str(uuid.uuid4())]},
        integration_template_bindings=["github"],
        integration_allowed_tools={"github": ["search_github_issues"]},
        mcp_server_ids=[str(uuid.uuid4())],
        mcp_allowed_tools={"mcp-1": ["tool_a"]},
    )

    updated_agent, response = await _invoke_update(monkeypatch, body, agent=agent)

    assert response["name"] == "Updated agent"
    assert updated_agent.name == "Updated agent"
    assert updated_agent.description == "Updated description"
    assert updated_agent.icon == "Shield"
    assert updated_agent.slug == "updated-agent"
    assert updated_agent.persona == "Updated persona"
    assert updated_agent.output_format == "json"
    assert updated_agent.output_schema == {
        "type": "object",
        "properties": {"status": {"type": "string"}},
    }
    assert updated_agent.tags == ["ops", "prod"]
    assert updated_agent.llm_model == "gpt-5-mini"
    assert updated_agent.llm_provider_id == provider_id
    assert updated_agent.temperature == 0.4
    assert updated_agent.reasoning_effort == "medium"
    assert updated_agent.max_iterations == 8
    assert updated_agent.disable_chat is True
    assert updated_agent.human_in_the_loop is True
    assert updated_agent.human_in_the_loop_prompt == "Always request approval"
    assert updated_agent.integration_bindings is not None
    assert "github" in updated_agent.integration_bindings
    assert updated_agent.integration_template_bindings == ["github"]
    assert updated_agent.integration_allowed_tools == {"github": ["search_github_issues"]}
    assert updated_agent.mcp_server_ids is not None
    assert len(updated_agent.mcp_server_ids) == 1
    assert updated_agent.mcp_allowed_tools == {"mcp-1": ["tool_a"]}


@pytest.mark.asyncio
async def test_update_agent_explicit_null_resets_nullable_fields(monkeypatch):
    """Partial/individual updates:
    explicit nulls must clear fields that support reset-to-default semantics.
    """

    agent = _DummyAgent()
    agent.llm_model = "gpt-5"
    agent.output_schema = {"type": "object"}
    agent.integration_allowed_tools = {"github": ["search_issues"]}
    agent.mcp_allowed_tools = {"mcp-1": ["tool_a"]}
    agent.temperature = 0.7
    agent.reasoning_effort = "high"
    agent.max_iterations = 15
    agent.human_in_the_loop = True
    agent.human_in_the_loop_prompt = "Require manager approval"

    body = UpdateAgentRequest(
        llm_model=None,
        output_schema=None,
        integration_allowed_tools=None,
        mcp_allowed_tools=None,
        temperature=None,
        reasoning_effort=None,
        max_iterations=None,
        human_in_the_loop=False,
        human_in_the_loop_prompt=None,
    )

    updated_agent, _ = await _invoke_update(monkeypatch, body, agent=agent)

    assert updated_agent.llm_model is None
    assert updated_agent.output_schema is None
    assert updated_agent.integration_allowed_tools is None
    assert updated_agent.mcp_allowed_tools is None
    assert updated_agent.temperature is None
    assert updated_agent.reasoning_effort is None
    assert updated_agent.max_iterations is None
    assert updated_agent.human_in_the_loop is False
    assert updated_agent.human_in_the_loop_prompt is None


@pytest.mark.asyncio
async def test_update_agent_clearing_the_provider_never_checks_the_id_sent_with_it(monkeypatch):
    """Clearing llm_provider clears llm_provider_id, so an id sent in the
    same save is never stored and is not refused for naming no config."""
    agent = _DummyAgent()
    agent.llm_provider, agent.llm_provider_id = "openai", uuid.uuid4()

    updated_agent, _ = await _invoke_update(
        monkeypatch,
        UpdateAgentRequest(llm_provider="", llm_provider_id=str(uuid.uuid4())),
        agent=agent,
        db=_NoProviderConfigDB(),
    )

    assert (updated_agent.llm_provider, updated_agent.llm_provider_id) == (None, None)


# ---------------------------------------------------------------------------
# Serializer round-trip — knowledge_base_permissions must be surfaced.
#
# Regression: ``_to_response`` constructs ``AgentResponse`` with explicit
# kwargs (``from_attributes`` is NOT used), so a field it forgets to pass
# silently defaults to ``None`` in the API response even though the DB value
# is persisted. ``knowledge_base_permissions`` was omitted, so the write
# toggle round-tripped to ``null`` on every GET — the UI toggle re-opened
# OFF and the read-only "Write" badge never rendered.
# ---------------------------------------------------------------------------


def test_to_response_round_trips_knowledge_base_permissions():
    """The real serializer must surface per-KB write grants."""
    kb_id = str(uuid.uuid4())
    agent = _DummyAgent()
    agent.knowledge_base_ids = [kb_id]
    agent.knowledge_base_permissions = {kb_id: "write"}

    response = agents_api._to_response(agent, can_manage=True, can_view_chats=True)

    assert response.knowledge_base_ids == [kb_id]
    assert response.knowledge_base_permissions == {kb_id: "write"}


def test_to_response_normalizes_empty_kb_permissions_to_none():
    """An empty grant map serialises to ``None`` (all bound KBs read-only)."""
    agent = _DummyAgent()
    agent.knowledge_base_ids = [str(uuid.uuid4())]
    agent.knowledge_base_permissions = {}

    response = agents_api._to_response(agent, can_manage=True, can_view_chats=True)

    assert response.knowledge_base_permissions is None


def test_to_response_round_trips_the_pinned_provider_and_hitl():
    """The provider pin, the HITL pair and the deny list come back as stored.

    The response said ``null`` for all four, so the model picker read a pinned
    agent back as "provider not pinned" after every save.
    """
    provider_id = uuid.uuid4()
    agent = _DummyAgent()
    agent.llm_provider, agent.llm_provider_id = "anthropic", provider_id
    agent.llm_model = "claude-opus-5-5"
    agent.human_in_the_loop = True
    agent.human_in_the_loop_prompt = "Ask before sending"
    agent.tool_deny_list = ["send_email"]

    response = agents_api._to_response(agent, can_manage=True, can_view_chats=True)

    assert response.llm_provider_id == str(provider_id)
    assert response.llm_model == "claude-opus-5-5"
    assert response.human_in_the_loop is True
    assert response.human_in_the_loop_prompt == "Ask before sending"
    assert response.tool_deny_list == ["send_email"]


def test_to_response_passes_every_stored_field_the_response_declares():
    """A response field the ``Agent`` row stores is passed by ``_to_response``.

    ``AgentResponse`` is built from explicit kwargs, so a forgotten one is
    served as its default whatever the row holds.
    """
    import ast
    import inspect
    import textwrap

    from app.api.v1.schemas.agent import AgentResponse

    tree = ast.parse(textwrap.dedent(inspect.getsource(agents_api._to_response)))
    passed = {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "AgentResponse"
        for keyword in node.keywords
        if keyword.arg
    }
    stored = set(Agent.__mapper__.columns.keys()) & set(AgentResponse.model_fields)

    assert sorted(stored - passed) == []


def test_to_response_uses_real_agent_model_default_permissions():
    """A freshly built Agent model with no grants serialises to ``None``."""
    now = datetime.now(UTC)
    agent = Agent(
        id=uuid.uuid4(),
        user_id=None,
        name="Reader",
        icon="Bot",
        output_format="markdown",
        enabled=True,
        is_builtin=False,
        is_company_managed=False,
        is_public_catalog=False,
        skip_llm=False,
        memory_short_term_enabled=True,
        memory_medium_term_enabled=True,
        memory_persistent_enabled=True,
        memory_user_enabled=True,
        human_in_the_loop=False,
        disable_chat=False,
        web_enabled=False,
        knowledge_base_ids=[],
        knowledge_base_permissions=None,
        created_at=now,
        updated_at=now,
    )

    response = agents_api._to_response(agent, can_manage=True, can_view_chats=True)

    assert response.knowledge_base_permissions is None


@pytest.mark.asyncio
async def test_update_agent_owner_transfer_cascades_to_assignments(monkeypatch):
    """An admin owner transfer reassigns the agent AND cascades the new owner
    onto every assignment of that agent, preserving the "assignment owner
    mirrors agent owner" invariant."""
    from sqlalchemy.sql.dml import Update

    import app.services.audit as audit_mod
    from app.models.assignment import Assignment  # noqa: F401 — table name assert

    target = SimpleNamespace(
        id=uuid.uuid4(),
        is_admin=False,
        is_active=True,
        email="new@example.com",
        full_name="New Owner",
    )

    class _CascadeResult:
        def scalar(self):
            return 0

        def scalar_one_or_none(self):
            return None

        def one_or_none(self):
            return None

        def scalars(self):
            return self

        def all(self):
            return []

        def __iter__(self):
            return iter([])

    class _CascadeDB:
        def __init__(self):
            self.executed: list = []

        async def execute(self, stmt):
            self.executed.append(stmt)
            return _CascadeResult()

        async def get(self, _model, _key):
            return target

        def add(self, _obj):
            return None

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def refresh(self, _obj):
            return None

    async def _noop_audit(*_args, **_kwargs):
        return None

    # ``apply_owner_transfer`` lazily imports ``log_audit`` from the audit
    # service (not the agents module), so stub it there to keep the no-op DB.
    monkeypatch.setattr(audit_mod, "log_audit", _noop_audit)

    agent = _DummyAgent()
    agent.user_id = uuid.uuid4()  # some prior owner
    _setup_update_agent_mocks(monkeypatch, agent)

    admin = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    db = _CascadeDB()
    request = Request(
        {"type": "http", "method": "PUT", "path": "/api/v1/agents/x", "headers": []}
    )

    await agents_api.update_agent(
        str(agent.id),
        UpdateAgentRequest(user_id=str(target.id)),
        request,
        admin,
        db,
    )

    # The agent itself is reassigned ...
    assert agent.user_id == target.id
    # ... and exactly one UPDATE against the assignments table cascaded the new
    # owner, scoped to this agent.
    assignment_updates = [
        s
        for s in db.executed
        if isinstance(s, Update) and s.table.name == "assignments"
    ]
    assert len(assignment_updates) == 1
    params = set(assignment_updates[0].compile().params.values())
    assert target.id in params  # new owner stamped onto assignments
    assert agent.id in params  # scoped to this agent


@pytest.mark.asyncio
async def test_update_agent_clamps_explicit_over_ceiling_max_iterations(monkeypatch):
    """An explicit max_iterations above the ceiling is clamped to it on save."""
    agent = _DummyAgent()
    agent.max_iterations = 8
    _, _ = await _invoke_update(
        monkeypatch, UpdateAgentRequest(max_iterations=5000), agent=agent
    )
    assert agent.max_iterations == MAX_ITERATIONS_CEILING


@pytest.mark.asyncio
async def test_update_agent_clamps_legacy_out_of_range_on_unrelated_update(monkeypatch):
    """A legacy out-of-range value (set before the server-side clamp existed) is
    pulled back under the ceiling even when this update doesn't touch
    max_iterations — so editing any other field enforces the ceiling."""
    agent = _DummyAgent()
    agent.max_iterations = 2000  # legacy value, never clamped server-side
    _, _ = await _invoke_update(
        monkeypatch, UpdateAgentRequest(name="Renamed"), agent=agent
    )
    assert agent.max_iterations == MAX_ITERATIONS_CEILING


@pytest.mark.asyncio
async def test_update_agent_leaves_in_range_max_iterations_untouched(monkeypatch):
    """An in-range value is not rewritten by the defensive clamp on an unrelated
    update (no spurious change), and an unset value stays unset (platform default)."""
    in_range = _DummyAgent()
    in_range.max_iterations = 50
    _, _ = await _invoke_update(
        monkeypatch, UpdateAgentRequest(name="Renamed"), agent=in_range
    )
    assert in_range.max_iterations == 50

    unset = _DummyAgent()  # max_iterations is None (platform default)
    _, _ = await _invoke_update(
        monkeypatch, UpdateAgentRequest(name="Renamed"), agent=unset
    )
    assert unset.max_iterations is None


# ---------------------------------------------------------------------------
# The platform routing model as an agent's model source.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_agent_onto_the_routing_model_drops_its_own_model(monkeypatch):
    """An agent on the routing model keeps no model of its own: a stale pin
    would come back the day the source is cleared."""
    agent = _DummyAgent()
    agent.llm_provider, agent.llm_provider_id, agent.llm_model = "openai", uuid.uuid4(), "gpt-5"

    updated_agent, _ = await _invoke_update(
        monkeypatch, UpdateAgentRequest(llm_model_source="routing"), agent=agent
    )

    assert updated_agent.llm_model_source == "routing"
    assert (updated_agent.llm_provider, updated_agent.llm_provider_id, updated_agent.llm_model) == (
        None,
        None,
        None,
    )
    # The response carrying it is pinned by
    # test_to_response_passes_every_stored_field_the_response_declares.


@pytest.mark.asyncio
async def test_update_agent_picking_a_model_leaves_the_routing_model(monkeypatch):
    """A model chosen for a routing agent is never silently ignored behind the
    routing source."""

    async def _selectable(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.llm.router.validate_selectable_chat_model", _selectable)
    agent = _DummyAgent()
    agent.llm_model_source = "routing"

    updated_agent, _ = await _invoke_update(
        monkeypatch, UpdateAgentRequest(llm_model="gpt-5"), agent=agent
    )

    assert updated_agent.llm_model_source is None
    assert updated_agent.llm_model == "gpt-5"


@pytest.mark.asyncio
async def test_update_agent_without_model_fields_keeps_the_routing_model(monkeypatch):
    agent = _DummyAgent()
    agent.llm_model_source = "routing"

    updated_agent, _ = await _invoke_update(
        monkeypatch, UpdateAgentRequest(name="Renamed"), agent=agent
    )

    assert updated_agent.llm_model_source == "routing"


@pytest.mark.asyncio
async def test_update_agent_explicit_null_source_returns_to_the_agent_model(monkeypatch):
    agent = _DummyAgent()
    agent.llm_model_source = "routing"

    updated_agent, _ = await _invoke_update(
        monkeypatch, UpdateAgentRequest(llm_model_source=None), agent=agent
    )

    assert updated_agent.llm_model_source is None
    assert updated_agent.llm_model is None


@pytest.mark.parametrize("value", ["", "ROUTING", "agent", "routing ", "x" * 200, 1, {}])
def test_update_agent_refuses_an_unknown_model_source(value):
    """An unknown source is a 422 (the body model), never a 500 at the column."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        UpdateAgentRequest(llm_model_source=value)
