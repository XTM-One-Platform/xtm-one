"""A run's enforced Scope(s) reaches every tool build and every handover.

``services/element_scope.py`` is what an enforced Scope(s) MEANS for a run: an
assignment refines its agent's selection, a delegated run narrows the scope of
the run that delegated to it, and an empty narrowing allows nothing. These
tests pin that meaning, then follow the scope through every path that builds
GitHub tools for an agent: background tasks (spawn tool -> task payload ->
worker -> the target's run), consult and transfer, the pipeline stages, public
chat. Which selections are enforced at all is chosen per selection
(``test_integration_scope_enforcement.py``).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.integrations.providers.github.scope import (
    GITHUB_REPOSITORY_SCOPE_KEY as REPO,
)
from app.integrations.providers.github.scope import repo_scope_from_bindings
from app.services.element_scope import (
    agent_run_scope,
    assignment_element_scope,
    narrow_element_scope,
    selected_elements,
)
from app.tools.base import CallableToolExecutor, ToolDefinition


def _repo(full_name: str) -> dict:
    return {"id": full_name, "name": full_name.split("/")[-1], "metadata": {"full_name": full_name}}


API, WEB = _repo("acme/api"), _repo("acme/web")
SCOPE = {REPO: [API]}


# ---------------------------------------------------------------------------
# What a scope means
# ---------------------------------------------------------------------------


def test_a_stored_selection_ignores_unselected_types():
    assert selected_elements(None) == {}
    assert selected_elements({REPO: [], "slack:slack_channel": "junk"}) == {}
    assert selected_elements({REPO: [API]}) == {REPO: [API]}


def test_an_assignment_refines_its_agent():
    assert assignment_element_scope({REPO: [API, WEB]}, {REPO: [WEB]}) == {REPO: [WEB]}
    assert assignment_element_scope({REPO: [API]}, {}) == {REPO: [API]}
    assert assignment_element_scope({}, {REPO: [WEB]}) == {REPO: [WEB]}
    # Refining to a repository the agent no longer offers allows nothing.
    assert assignment_element_scope({REPO: [API]}, {REPO: [WEB]}) == {REPO: []}


def test_a_delegated_run_only_narrows_the_scope_it_inherited():
    # An unscoped target inherits the parent's scope.
    assert agent_run_scope(None, None, {"element_scope": SCOPE}).enforced == SCOPE
    # A target scoped elsewhere is left with nothing - not with its own.
    narrowed = agent_run_scope({REPO: [WEB]}, None, {"element_scope": SCOPE}).enforced
    assert narrowed == {REPO: []}
    assert repo_scope_from_bindings(narrowed) == frozenset()
    # "Nothing" stays nothing down the chain, whatever the target selects.
    assert agent_run_scope({REPO: [API]}, [REPO], {"element_scope": {REPO: []}}).enforced == {REPO: []}
    # A type the parent does not scope is the target's own boundary, when the
    # target enforces it.
    slack = {"slack:slack_channel": [{"id": "C1"}]}
    assert agent_run_scope(slack, ["slack:slack_channel"], {"element_scope": SCOPE}).enforced == {**SCOPE, **slack}
    # No handover: the agent's own enforced selection.
    assert agent_run_scope({REPO: [API]}, [REPO]).enforced == SCOPE
    assert agent_run_scope({REPO: [API]}, [REPO], {"visited_agents": []}).enforced == SCOPE


def test_narrowing_never_widens_whatever_it_is_handed():
    assert narrow_element_scope(SCOPE, {REPO: [API, WEB]}) == SCOPE
    assert narrow_element_scope("junk", SCOPE) == SCOPE
    assert narrow_element_scope(SCOPE, {REPO: ["junk", {"no": "id"}]}) == {REPO: []}


def test_narrowing_keeps_the_outer_entry_not_the_inner_metadata():
    """An inner entry reusing an allowed id with another ``full_name`` must
    not become the effective repository: the provider reads the metadata."""
    forged = {"id": "acme/api", "name": "api", "metadata": {"full_name": "evil/x"}}
    for narrowed in (
        narrow_element_scope(SCOPE, {REPO: [forged]}),
        assignment_element_scope({REPO: [API]}, {REPO: [forged]}),
        agent_run_scope({REPO: [forged]}, None, {"element_scope": SCOPE}).enforced,
    ):
        assert narrowed == SCOPE
        assert repo_scope_from_bindings(narrowed) == frozenset({"acme/api"})
    # An unhashable id matches nothing instead of raising.
    assert narrow_element_scope(SCOPE, {REPO: [{"id": ["acme/api"]}]}) == {REPO: []}


# ---------------------------------------------------------------------------
# Background tasks: spawn tool -> task payload -> worker -> target run
# ---------------------------------------------------------------------------


class _FakeSessionCM:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


def _own_db() -> MagicMock:
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock()
    return db


async def test_the_spawn_tool_hands_the_run_scope_to_every_task():
    from app.services.builtin_tools import build_sub_agent_tools

    target = SimpleNamespace(id=uuid.uuid4(), name="Worker", slug="worker")
    _defs, executor = build_sub_agent_tools(
        MagicMock(),
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        [target],
        element_scope=SCOPE,
    )
    create = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), status="pending"))
    with (
        patch("app.services.sub_agent.count_active_sub_agent_executions", new=AsyncMock(return_value=0)),
        patch("app.services.sub_agent.create_sub_agent_task", new=create),
    ):
        spawn = executor.handler("spawn_background_task")
        result = await spawn(agent_id=str(target.id), task="triage the backlog")
    assert "spawned successfully" in result
    assert create.await_args.kwargs["element_scope"] == SCOPE


async def test_a_task_payload_carries_the_scope():
    from app.services.sub_agent import create_sub_agent_task

    create_execution = AsyncMock()
    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(_own_db())),
        patch("app.services.orchestration_engine.create_execution", new=create_execution),
        patch("app.services.orchestration_engine.notify_orchestration_wake", new=AsyncMock()),
        patch("app.services.delegation.log_delegation", new=AsyncMock()),
    ):
        await create_sub_agent_task(
            MagicMock(),
            user_id=str(uuid.uuid4()),
            parent_agent_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description="work",
            element_scope=SCOPE,
        )
    payload = create_execution.await_args.kwargs["payload"]
    assert payload["element_scope"] == SCOPE
    # The marker is what tells a worker the scope is a boundary.
    assert payload["element_scope_enforced"] is True


async def _adapter_forwards(payload_scope: dict) -> dict | None:
    from app.services.orchestration_adapters.sub_agent import SubAgentAdapter
    from app.services.sub_agent import SubAgentTaskOutcome

    execution = MagicMock()
    execution.payload = {
        "task_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "target_agent_id": str(uuid.uuid4()),
        "task_description": "work",
        **payload_scope,
    }
    guard = MagicMock()
    guard.scalar_one_or_none = MagicMock(return_value=None)
    ctx = SimpleNamespace(
        db=MagicMock(execute=AsyncMock(return_value=guard), commit=AsyncMock()),
        cancel_event=None,
    )
    execute = AsyncMock(return_value=SubAgentTaskOutcome(status="completed"))
    with patch("app.services.sub_agent._execute_sub_agent_task", new=execute):
        await SubAgentAdapter().execute(ctx, execution)
    return execute.await_args.kwargs["element_scope"]


async def test_the_worker_adapter_forwards_the_scope():
    assert await _adapter_forwards({"element_scope": SCOPE, "element_scope_enforced": True}) == SCOPE


async def test_a_task_queued_before_the_choice_existed_inherits_no_boundary():
    """An older payload carried the delegating run's WHOLE
    selection under the same key, guidance included: read as a boundary, it
    would narrow a delegated run nobody chose to narrow."""
    assert await _adapter_forwards({"element_scope": SCOPE}) is None
    assert await _adapter_forwards({"element_scope": SCOPE, "element_scope_enforced": "yes"}) is None
    assert await _adapter_forwards({}) is None


def _exec_db(task: MagicMock, agent: MagicMock) -> MagicMock:
    def _result(obj):
        res = MagicMock()
        res.scalar_one_or_none = MagicMock(return_value=obj)
        return res

    groups = MagicMock()
    groups.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(task), groups, _result(agent), _result(task)])
    db.commit = AsyncMock()
    return db


async def test_the_target_run_receives_the_scope_it_must_narrow(monkeypatch):
    from app.services.sub_agent import _execute_sub_agent_task
    from tests.agentic_gate_stub import stub_agentic_gate

    stub_agentic_gate(monkeypatch)
    task = MagicMock()
    task.conversation_id = None
    agent = MagicMock()
    agent.handover_agent_ids = []
    agent.disable_chat = True
    agent.max_iterations = 25
    run_agent_chat = AsyncMock(return_value=("done", None))
    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(_exec_db(task, agent))),
        patch("app.services.agent_chat.run_agent_chat", new=run_agent_chat),
        patch("app.services.sub_agent._update_task_delegation", new=AsyncMock()),
    ):
        outcome = await _execute_sub_agent_task(
            task_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description="work",
            element_scope=SCOPE,
        )
    assert outcome.status == "completed"
    assert run_agent_chat.await_args.kwargs["handover_context"]["element_scope"] == SCOPE


async def test_a_wait_budget_continuation_keeps_the_scope():
    from app.services.sub_agent import _spawn_wait_budget_continuation

    create = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    with patch("app.services.sub_agent.create_sub_agent_task", new=create):
        await _spawn_wait_budget_continuation(
            user_id=str(uuid.uuid4()),
            parent_agent_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            original_task="work",
            pending_ids=[str(uuid.uuid4())],
            conversation_id=None,
            parent_run_id=None,
            visited_agents=[],
            timeout_seconds=None,
            max_iterations=None,
            continuation_of=str(uuid.uuid4()),
            continuation_depth=1,
            scope_headers=None,
            file_context=None,
            db=MagicMock(),
            element_scope=SCOPE,
        )
    assert create.await_args.kwargs["element_scope"] == SCOPE


# ---------------------------------------------------------------------------
# Chat paths that build GitHub tools outside the main tool build
# ---------------------------------------------------------------------------


def _recording_builder(seen: list[dict]):
    async def build_integration_tools(db, bindings, context=None, **kwargs):
        seen.append(dict(context or {}))
        executor = CallableToolExecutor()
        executor.register("get_github_file", AsyncMock(return_value="ok"))
        definition = ToolDefinition(
            name="get_github_file",
            description="x",
            input_schema={"type": "object", "properties": {"repo": {"type": "string"}}},
            source="test",
        )
        return [definition], executor

    return build_integration_tools


async def test_a_pipeline_stage_tool_is_built_under_the_run_scope():
    """PREPARE / FINALIZE feed the user's message into their tool's first
    argument: a GitHub stage would otherwise take ``repo`` from the user."""
    from app.services.agent_chat import _load_pipeline_tool_standalone

    integration = SimpleNamespace(id=uuid.uuid4(), name="GitHub", provider="github")
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=integration)
    db = MagicMock(execute=AsyncMock(return_value=result))
    seen: list[dict] = []
    owner = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), name="Triage", user_id=owner)
    with patch("app.services.integration_tools.build_integration_tools", new=_recording_builder(seen)):
        loaded = await _load_pipeline_tool_standalone(
            db,
            {"source": "integration", "provider": "github", "tool_name": "get_github_file", "integration_id": str(integration.id)},
            str(owner),
            agent,
            None,
            element_scope=SCOPE,
        )
    assert loaded is not None
    assert seen[0]["element_scope"] == SCOPE


async def test_the_public_widget_is_built_under_the_agent_scope(monkeypatch):
    from app.api.v1.public_chat import _run_public_agent_chat
    from tests.test_public_chat_hardening import (
        _FakeDB,
        _make_public_agent,
        _patch_chat_pipeline,
    )

    _patch_chat_pipeline(monkeypatch, integration_tools=[])
    seen: list[dict] = []
    import app.services.integration_tools as integration_tools_mod

    monkeypatch.setattr(integration_tools_mod, "build_integration_tools", _recording_builder(seen))
    agent = _make_public_agent(
        integration_bindings={"github": [str(uuid.uuid4())]},
        integration_element_bindings={REPO: [API]},
        integration_element_enforcement=[REPO],
        channel_config={"web_embed": {"allow_tools": True}},
    )
    await _run_public_agent_chat(_FakeDB({}), agent, [], "hello")
    assert seen[0]["element_scope"] == SCOPE


# ---------------------------------------------------------------------------
# Consult / transfer / spawn inside chat and assignment runs (source pins:
# the two call graphs are too large to drive end to end in a unit test)
# ---------------------------------------------------------------------------

_BACKEND = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (_BACKEND / relative).read_text(encoding="utf-8")


def test_a_chat_run_builds_and_hands_over_under_its_effective_scope():
    chat = _source("app/services/agent_chat.py")
    assert (
        "_run_scope = agent_run_scope(\n"
        '        getattr(agent, "integration_element_bindings", None),\n'
        '        getattr(agent, "integration_element_enforcement", None),\n'
        "        handover_context,\n"
        "    )\n"
        "    _element_scope = _run_scope.enforced"
    ) in chat
    # The three pipeline loads and the prompt hint.
    assert chat.count("scope_headers, element_scope=_element_scope") == 3
    assert "format_element_bindings_prompt(_run_scope.prompt)" in chat
    # The main tool build and the consult and transfer contexts...
    assert chat.count('"element_scope": _element_scope,') == 3
    # ... and every background task the spawn tool creates.
    assert "element_scope=_element_scope,\n                )" in chat
    assert "agent.integration_element_bindings or {}" not in chat


def test_an_assignment_run_hands_over_under_its_merged_scope():
    engine = _source("app/services/assignment_engine.py")
    # Tool build, consult and transfer contexts; the spawn tool.
    assert engine.count('"element_scope": enforced_scope,') == 3
    assert "element_scope=enforced_scope," in engine


def test_the_scope_lines_read_their_labels_and_identifiers_from_the_catalog():
    """No vendor in the generic prompt builder: the provider's catalog entry names the
    element type and the metadata that identifies an element to its tools."""
    from app.services.builtin_tools.prompting import format_element_bindings_prompt

    prompt = format_element_bindings_prompt(
        {
            "microsoft:teams_channel": [
                {
                    "id": "t:c",
                    "name": "General",
                    "group": "Ops",
                    "metadata": {"team_id": "t", "channel_id": "c", "membership_type": "standard"},
                }
            ],
            "github:repository": [
                {"id": "acme/api", "name": "api", "metadata": {"full_name": "acme/api", "language": "Go"}}
            ],
            "unknown_provider:thing": [{"id": "x1", "name": "X one"}],
        }
    )

    assert "- Teams Channel (Microsoft): General (group: Ops) [team_id=t, channel_id=c]" in prompt
    assert "Repository (GitHub): api [full_name=acme/api]" in prompt
    assert "language" not in prompt and "membership_type" not in prompt
    assert "- thing (unknown_provider): X one [id=x1]" in prompt
