"""Enforcing a Scope(s) selection is a choice made per selection.

``integration_element_enforcement``, stored next to ``integration_element_bindings``
on an agent and on an assignment, lists the element types whose selection is a
hard boundary on the integration tools: GitHub repositories, Slack channels,
Microsoft Teams channels and HubSpot CRM object types. Every other
selection - every existing one included - is the prompt guidance it was before.
No deployment setting decides it.

What an enforced scope does to the tools is covered by the provider suites
(``test_github_repo_scope.py`` and siblings) and by ``test_element_scope.py``;
these tests pin which scope is enforced, where the choice is stored and every
path that carries it.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.api.v1 import security_audit
from app.api.v1.auth import AuthConfigResponse
from app.api.v1.schemas.agent import CreateAgentRequest, UpdateAgentRequest
from app.api.v1.schemas.assignment import CreateAssignmentRequest, UpdateAssignmentRequest
from app.core.config import Settings
from app.integrations.providers.github.scope import GITHUB_REPOSITORY_SCOPE_KEY as REPO
from app.integrations.providers.hubspot_scope import HUBSPOT_OBJECT_TYPE_SCOPE_KEY as HUBSPOT
from app.integrations.providers.microsoft.teams_scope import TEAMS_CHANNEL_SCOPE_KEY as TEAMS
from app.integrations.providers.slack_scope import SLACK_CHANNEL_SCOPE_KEY as SLACK
from app.models.agent import Agent
from app.models.agent_copy import (
    AGENT_BUNDLE_PORTABLE_FIELDS,
    AGENT_COPY_FIELDS,
    ASSIGNMENT_COPY_FIELDS,
)
from app.models.assignment import Assignment
from app.services.element_scope import (
    RunScope,
    ScopeEnforcementError,
    agent_run_scope,
    assignment_run_scope,
    enforceable_scope_keys,
    guidance_only_scope_keys,
    normalize_scope_enforcement,
    parse_scope_enforcement,
    queued_task_scope,
)
from app.services.integration_tools import BindingBasis, ToolBindings

_BACKEND = Path(__file__).resolve().parents[1]
_ROOT = _BACKEND.parent


def _source(relative: str) -> str:
    return (_BACKEND / relative).read_text(encoding="utf-8")


def _repo(full_name: str, name: str | None = None) -> dict:
    return {
        "id": full_name,
        "name": name or full_name.split("/")[-1],
        "metadata": {"full_name": full_name},
    }


API, WEB = _repo("acme/api"), _repo("acme/web")
SUPPORT = {"id": "C0SUPPORT", "name": "support"}
GENERAL = {"id": "19:x@thread.tacv2", "name": "General"}
DEAL = {"id": "deal", "name": "Deals"}
ORG = "github:organization"


# ---------------------------------------------------------------------------
# No deployment setting: the choice is stored with the selection
# ---------------------------------------------------------------------------


def test_no_deployment_setting_decides_it():
    assert "integration_scope_enforcement" not in Settings.model_fields
    assert "integration_scope_enforcement" not in AuthConfigResponse.model_fields
    for path in (".env.sample", "docs/docs/deployment/configuration.md"):
        assert "INTEGRATION_SCOPE_ENFORCEMENT" not in (_ROOT / path).read_text(encoding="utf-8")
    assert "INTEGRATION_SCOPE_ENFORCEMENT" not in _source("app/services/element_scope.py")


@pytest.mark.parametrize("model", [Agent, Assignment])
def test_the_choice_is_a_nullable_column_next_to_the_selection(model):
    column = model.__table__.c.integration_element_enforcement
    assert column.nullable
    assert column.default is None or column.default.arg is None
    assert "integration_element_bindings" in model.__table__.c


def test_one_lock_safe_migration_adds_it_to_both_tables():
    versions = _BACKEND / "alembic/versions"
    adding = [
        path.read_text(encoding="utf-8")
        for path in versions.glob("*.py")
        if "integration_element_enforcement" in path.read_text(encoding="utf-8")
    ]
    assert len(adding) == 1
    [migration] = adding
    assert '"agents"' in migration and '"assignments"' in migration
    assert "ADD COLUMN IF NOT EXISTS" in migration
    assert "terminate_lock_holders(" in migration and "set_lock_timeout(" in migration


# ---------------------------------------------------------------------------
# Which types may be enforced, and the stored form
# ---------------------------------------------------------------------------


def test_only_the_four_types_a_provider_holds_to_a_selection_can_be_enforced():
    assert enforceable_scope_keys() == {REPO, SLACK, TEAMS, HUBSPOT}
    assert parse_scope_enforcement(None) is None
    assert parse_scope_enforcement([]) == []
    assert parse_scope_enforcement([SLACK, REPO, REPO]) == sorted([REPO, SLACK])
    for bad in (REPO, [7], [ORG], [f" {REPO}"]):
        with pytest.raises(ScopeEnforcementError):
            parse_scope_enforcement(bad)
    with pytest.raises(ScopeEnforcementError) as refused:
        parse_scope_enforcement([ORG])
    assert ORG in str(refused.value)
    assert "GitHub repositories" in str(refused.value)


@pytest.mark.parametrize(
    ("schema", "required"),
    [
        (CreateAgentRequest, {"name": "Triage"}),
        (UpdateAgentRequest, {}),
        (
            CreateAssignmentRequest,
            {"agent_id": str(uuid.uuid4()), "name": "Nightly", "trigger_type": "manual", "prompt": "Go"},
        ),
        (UpdateAssignmentRequest, {}),
    ],
)
def test_the_rest_schemas_accept_the_four_types_only(schema, required):
    accepted = schema(**required, integration_element_enforcement=[SLACK, REPO])
    assert accepted.integration_element_enforcement == [REPO, SLACK]
    assert schema(**required).integration_element_enforcement is None
    with pytest.raises(ValidationError):
        schema(**required, integration_element_enforcement=[ORG])


def test_the_stored_choice_only_names_selected_types_that_can_be_enforced():
    bindings = {REPO: [API], SLACK: [], ORG: [{"id": "acme"}]}
    assert normalize_scope_enforcement([SLACK, REPO, ORG, 3], bindings) == [REPO]
    # A type with no selection, or an emptied one, keeps no flag.
    assert normalize_scope_enforcement([SLACK], bindings) is None
    assert normalize_scope_enforcement([REPO], None) is None
    assert normalize_scope_enforcement(None, bindings) is None
    assert normalize_scope_enforcement(REPO, bindings) is None


# ---------------------------------------------------------------------------
# A run's scope: what the prompt says, what the tools are held to
# ---------------------------------------------------------------------------


def test_an_agent_selection_is_guidance_unless_its_type_is_enforced():
    bindings = {REPO: [API], SLACK: [SUPPORT]}
    assert agent_run_scope(bindings, None) == RunScope(prompt=bindings, enforced={})
    assert agent_run_scope(bindings, [REPO]) == RunScope(prompt=bindings, enforced={REPO: [API]})
    # A flag on a type the agent does not select, or no provider enforces, does nothing.
    assert agent_run_scope({REPO: [API]}, [SLACK]).enforced == {}
    assert agent_run_scope({ORG: [{"id": "acme"}]}, [ORG]).enforced == {}


def test_a_delegated_run_is_narrowed_only_by_an_enforced_scope_it_inherited():
    # The delegating run hands over what it enforces (``RunScope.enforced``).
    handed = {"element_scope": {REPO: [API]}}
    assert agent_run_scope(None, None, handed).enforced == {REPO: [API]}
    # A target selecting the same type is narrowed whatever its own flag: the
    # stricter wins.
    assert agent_run_scope({REPO: [WEB]}, None, handed).enforced == {REPO: []}
    assert agent_run_scope({REPO: [API, WEB]}, None, handed).enforced == {REPO: [API]}
    # A type the delegating run did not enforce stays the target's own:
    # guidance, or the target's own boundary.
    slack = {SLACK: [SUPPORT]}
    guided = agent_run_scope(slack, None, handed)
    assert guided.enforced == {REPO: [API]}
    assert guided.prompt == {**slack, REPO: [API]}
    assert agent_run_scope(slack, [SLACK], handed).enforced == {REPO: [API], **slack}
    # A delegating run that enforces nothing narrows nothing.
    assert agent_run_scope({REPO: [WEB]}, None, {"element_scope": {}}) == RunScope(
        prompt={REPO: [WEB]}, enforced={}
    )
    # A type no provider enforces never narrows a delegated run, whoever wrote it.
    written_before = {"element_scope": {ORG: [{"id": "acme"}]}}
    assert agent_run_scope(None, None, written_before) == RunScope(prompt={}, enforced={})


def test_a_queued_task_reads_its_boundary_only_from_a_marked_payload():
    """A background task queued by an older build - or by an older replica during a
    rolling deploy - holds the delegating run's WHOLE selection under the same
    key: only the marker says the key is a boundary."""
    marked = {"element_scope": {REPO: [API]}, "element_scope_enforced": True}
    assert queued_task_scope(marked) == {REPO: [API]}
    assert queued_task_scope({"element_scope": {REPO: [API]}}) is None
    assert queued_task_scope({**marked, "element_scope_enforced": "true"}) is None
    assert queued_task_scope({**marked, "element_scope": None}) is None
    assert queued_task_scope({**marked, "element_scope": "junk"}) is None
    assert queued_task_scope(None) is None


def test_an_assignment_without_enforcement_merges_its_selection_as_before():
    renamed = _repo("acme/web", name="web (renamed)")
    # The assignment's own entry of a kept element, as the baseline built it.
    assert assignment_run_scope({REPO: [API, WEB]}, None, {REPO: [renamed]}, None) == RunScope(
        prompt={REPO: [renamed]}, enforced={}
    )
    # One side empty: the other one as stored.
    stored = {REPO: [API], SLACK: []}
    assert assignment_run_scope(stored, None, {}, None).prompt is stored
    assert assignment_run_scope({}, None, stored, None).prompt is stored
    assert assignment_run_scope(None, None, None, None) == RunScope(prompt={}, enforced={})
    # A type only one side selects stays; an empty intersection stays empty.
    slack = {SLACK: [SUPPORT]}
    assert assignment_run_scope({REPO: [API]}, None, slack, None).prompt == {REPO: [API], **slack}
    assert assignment_run_scope({REPO: [WEB]}, None, {REPO: [API]}, None).prompt == {REPO: []}


def test_a_malformed_element_id_matches_nothing_instead_of_aborting_the_run():
    odd = {"id": ["acme/api"], "name": "api"}
    assert assignment_run_scope({REPO: [API, odd]}, None, {REPO: [odd, API]}, None).prompt == {REPO: [API]}
    assert assignment_run_scope({REPO: [API]}, None, {REPO: [{"id": {"x": 1}}]}, [REPO]).enforced == {REPO: []}


def test_the_stricter_of_the_agent_and_the_assignment_wins():
    agent, assignment = {REPO: [API, WEB]}, {REPO: [WEB]}
    for agent_flag, assignment_flag in (([REPO], None), (None, [REPO]), ([REPO], [REPO])):
        scope = assignment_run_scope(agent, agent_flag, assignment, assignment_flag)
        assert scope == RunScope(prompt={REPO: [WEB]}, enforced={REPO: [WEB]})
    # The agent's boundary holds for an assignment that selects nothing of that type.
    assert assignment_run_scope(agent, [REPO], {}, None).enforced == agent
    # Enforcing elements the agent does not select allows nothing.
    assert assignment_run_scope({REPO: [API]}, None, {REPO: [WEB]}, [REPO]).enforced == {REPO: []}
    # Only the enforced type is a boundary; the other one stays guidance.
    slack = {SLACK: [SUPPORT]}
    scope = assignment_run_scope({**agent, **slack}, [REPO], assignment, None)
    assert scope == RunScope(prompt={REPO: [WEB], **slack}, enforced={REPO: [WEB]})


def test_an_enforced_type_keeps_the_agent_entry():
    """An assignment reusing an allowed id with other metadata must not widen
    the boundary: the provider reads the metadata."""
    forged = {"id": "acme/api", "name": "api", "metadata": {"full_name": "evil/x"}}
    scope = assignment_run_scope({REPO: [API]}, [REPO], {REPO: [forged]}, None)
    assert scope == RunScope(prompt={REPO: [API]}, enforced={REPO: [API]})


def test_guidance_only_selections_are_the_selected_types_nobody_enforces():
    bindings = {REPO: [API], SLACK: [SUPPORT], ORG: [{"id": "acme"}], TEAMS: []}
    assert guidance_only_scope_keys(bindings, None) == [REPO, SLACK]
    assert guidance_only_scope_keys(bindings, [REPO]) == [SLACK]
    assert guidance_only_scope_keys(bindings, [REPO], enforced_elsewhere=frozenset({SLACK})) == []


# ---------------------------------------------------------------------------
# The tool build: a provider receives the enforced scope, and only that
# ---------------------------------------------------------------------------


class _IntegrationDb:
    def __init__(self, row):
        self._row = row

    async def execute(self, *_args, **_kwargs):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [self._row]))


PROVIDERS = [
    ("github", "repo_scope", REPO, {REPO: [API]}),
    ("slack", "channel_scope", SLACK, {SLACK: [SUPPORT]}),
    ("microsoft", "teams_channel_scope", TEAMS, {TEAMS: [GENERAL]}),
    ("hubspot", "object_type_scope", HUBSPOT, {HUBSPOT: [DEAL]}),
]


async def _builder_kwargs(monkeypatch, provider: str, scope: dict) -> dict:
    import app.services.integration_tools as integration_tools

    integration_id = str(uuid.uuid4())
    row = SimpleNamespace(id=uuid.UUID(integration_id), name="Work", description=None, auth_type="api_key")
    monkeypatch.setattr(integration_tools, "_parallel_refresh_expiring_tokens", AsyncMock(return_value=({}, set())))
    monkeypatch.setattr(integration_tools, "_get_token", AsyncMock(return_value="token"))
    seen: list[dict] = []

    def _record(_token, _executor, **kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setitem(integration_tools.PROVIDER_TOOLS, provider, _record)
    await integration_tools.build_integration_tools(
        _IntegrationDb(row),
        ToolBindings({provider: [integration_id]}, basis=BindingBasis.CALLER_CHECKED, reason="test"),
        context={"source": "test", "element_scope": scope},
    )
    [kwargs] = seen
    return kwargs


@pytest.mark.parametrize(("provider", "kwarg", "key", "bindings"), PROVIDERS)
async def test_a_guidance_only_selection_leaves_the_toolbox_complete(monkeypatch, provider, kwarg, key, bindings):
    kwargs = await _builder_kwargs(monkeypatch, provider, agent_run_scope(bindings, None).enforced)
    assert kwarg not in kwargs


@pytest.mark.parametrize(("provider", "kwarg", "key", "bindings"), PROVIDERS)
async def test_an_enforced_selection_bounds_the_provider(monkeypatch, provider, kwarg, key, bindings):
    kwargs = await _builder_kwargs(monkeypatch, provider, agent_run_scope(bindings, [key]).enforced)
    assert kwargs[kwarg] is not None


def test_the_tool_build_enforces_the_scope_it_is_handed():
    tools = _source("app/services/integration_tools.py")
    assert tools.count('context.get("element_scope")') == 1
    assert "tool_element_scope" not in tools


# ---------------------------------------------------------------------------
# Entry points: chat, assignments, the public widget, evaluations
# ---------------------------------------------------------------------------


def test_a_chat_run_reads_the_agent_choice_and_hands_over_what_it_enforces():
    chat = _source("app/services/agent_chat.py")
    assert (
        "_run_scope = agent_run_scope(\n"
        '        getattr(agent, "integration_element_bindings", None),\n'
        '        getattr(agent, "integration_element_enforcement", None),\n'
        "        handover_context,\n"
        "    )\n"
        "    _element_scope = _run_scope.enforced\n"
    ) in chat
    assert "format_element_bindings_prompt(_run_scope.prompt)" in chat
    assert "scope_enforcement_enabled" not in chat


def test_an_assignment_run_reads_both_choices_and_hands_over_what_it_enforces():
    engine = _source("app/services/assignment_engine.py")
    assert (
        "_run_scope = assignment_run_scope(\n"
        "                agent.integration_element_bindings,\n"
        "                agent.integration_element_enforcement,\n"
        "                assignment.integration_element_bindings,\n"
        "                assignment.integration_element_enforcement,\n"
        "            )\n"
        "            element_bindings = _run_scope.prompt\n"
        "            enforced_scope = _run_scope.enforced\n"
    ) in engine
    assert engine.count('"element_scope": enforced_scope,') == 3
    assert "element_scope=enforced_scope," in engine
    assert '"element_scope": element_bindings' not in engine


async def test_the_public_widget_bounds_only_the_enforced_types(monkeypatch):
    from app.api.v1.public_chat import _run_public_agent_chat
    from tests.test_element_scope import _recording_builder
    from tests.test_public_chat_hardening import _FakeDB, _make_public_agent, _patch_chat_pipeline

    _patch_chat_pipeline(monkeypatch, integration_tools=[])
    seen: list[dict] = []
    import app.services.integration_tools as integration_tools_mod

    monkeypatch.setattr(integration_tools_mod, "build_integration_tools", _recording_builder(seen))
    agent = _make_public_agent(
        integration_bindings={"github": [str(uuid.uuid4())]},
        integration_element_bindings={REPO: [API], SLACK: [SUPPORT]},
        integration_element_enforcement=[REPO],
        channel_config={"web_embed": {"allow_tools": True}},
    )
    await _run_public_agent_chat(_FakeDB({}), agent, [], "hello")
    assert seen[0]["element_scope"] == {REPO: [API]}


def test_an_evaluation_run_is_bounded_like_a_chat_run():
    evaluation = _source("app/services/evaluation_engine.py")
    assert '"element_scope": agent_run_scope(' in evaluation
    assert 'getattr(agent, "integration_element_enforcement", None)' in evaluation


# ---------------------------------------------------------------------------
# REST: create / update agents and assignments
# ---------------------------------------------------------------------------


async def test_an_agent_update_stores_the_choice_for_its_selected_types(monkeypatch):
    from tests.test_agent_update_partial_payload import _invoke_update

    agent = Agent(id=uuid.uuid4(), name="Triage", enabled=True, is_builtin=False, is_company_managed=False)
    await _invoke_update(
        monkeypatch,
        UpdateAgentRequest(
            integration_element_bindings={REPO: [API], SLACK: [SUPPORT]},
            integration_element_enforcement=[REPO, HUBSPOT],
        ),
        agent=agent,
    )
    # HubSpot has no selection, so it keeps no flag.
    assert agent.integration_element_enforcement == [REPO]

    # Dropping the repository selection drops its flag.
    await _invoke_update(monkeypatch, UpdateAgentRequest(integration_element_bindings={SLACK: [SUPPORT]}), agent=agent)
    assert agent.integration_element_enforcement is None

    # An explicit null clears the choice; a selection alone never sets one.
    await _invoke_update(
        monkeypatch,
        UpdateAgentRequest(integration_element_bindings={SLACK: [SUPPORT]}, integration_element_enforcement=[SLACK]),
        agent=agent,
    )
    assert agent.integration_element_enforcement == [SLACK]
    await _invoke_update(monkeypatch, UpdateAgentRequest(integration_element_enforcement=None), agent=agent)
    assert agent.integration_element_enforcement is None


def test_removing_an_integration_drops_the_choice_with_the_selection():
    agent = Agent(
        integration_bindings={"slack": [str(uuid.uuid4())]},
        integration_element_bindings={REPO: [API], SLACK: [SUPPORT]},
        integration_element_enforcement=[REPO, SLACK],
    )
    agent.prune_orphaned_integration_scopes()
    assert agent.integration_element_bindings == {SLACK: [SUPPORT]}
    assert agent.integration_element_enforcement == [SLACK]


def test_a_builtin_agent_admin_may_change_the_choice_with_its_integrations():
    from app.api.v1.agents import _BUILTIN_INTEGRATION_FIELDS, _is_integration_binding_change

    assert "integration_element_enforcement" in _BUILTIN_INTEGRATION_FIELDS
    agent = SimpleNamespace(integration_element_enforcement=None, integration_element_bindings={REPO: [API]})
    assert _is_integration_binding_change(agent, UpdateAgentRequest(integration_element_enforcement=[REPO]))
    # Turning the last switch off sends an explicit null: a change, not a no-op.
    agent.integration_element_enforcement = [REPO]
    cleared = UpdateAgentRequest(integration_element_bindings={REPO: [API]}, integration_element_enforcement=None)
    assert _is_integration_binding_change(agent, cleared)
    # ... and a null over nothing stored stays the ordinary no-op refusal.
    agent.integration_element_enforcement = None
    assert not _is_integration_binding_change(agent, cleared)


async def test_a_builtin_agent_admin_may_clear_its_last_scope_selection(monkeypatch):
    """Removing the last Scope(s) entry sends explicit nulls - both fields when
    the selection was guidance, the default - and resetting a tool scope sends
    one too: the carve-out admits each clear, and a null over nothing stored
    still gets the ordinary refusal."""
    from fastapi import HTTPException

    from app.api.v1 import agents as agents_api
    from tests.test_agent_update_partial_payload import _invoke_update

    monkeypatch.setattr(agents_api.settings, "platform_mode", "xtm_one")
    agent = Agent(
        id=uuid.uuid4(),
        name="Built-in",
        enabled=True,
        is_builtin=True,
        is_company_managed=True,
        integration_bindings={"github": [str(uuid.uuid4())]},
        integration_element_bindings={REPO: [API]},
        integration_allowed_tools={"github": ["list_github_issues"]},
    )
    await _invoke_update(
        monkeypatch,
        UpdateAgentRequest(integration_element_bindings=None, integration_element_enforcement=None),
        agent=agent,
    )
    assert agent.integration_element_bindings is None
    await _invoke_update(monkeypatch, UpdateAgentRequest(integration_allowed_tools=None), agent=agent)
    assert agent.integration_allowed_tools is None
    for nothing_stored in ("integration_element_bindings", "integration_allowed_tools"):
        with pytest.raises(HTTPException) as refused:
            await _invoke_update(monkeypatch, UpdateAgentRequest(**{nothing_stored: None}), agent=agent)
        assert refused.value.status_code == 403
    # A null ``integration_bindings`` changes nothing in ``update_agent``, so
    # it is no change for the carve-out either.
    with pytest.raises(HTTPException):
        await _invoke_update(monkeypatch, UpdateAgentRequest(integration_bindings=None), agent=agent)
    assert agent.integration_bindings


async def test_an_assignment_update_stores_the_choice_for_its_selected_types(monkeypatch):
    from tests.test_assignment_max_iterations import _invoke_update_assignment

    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Nightly",
        max_iterations=None,
        trigger_type="manual",
        trigger_config={},
        is_company_managed=False,
        enabled=True,
        delay=None,
        ai_label=False,
        human_in_the_loop=False,
        integration_element_bindings=None,
        integration_element_enforcement=None,
        run_as_user_id=None,
    )
    await _invoke_update_assignment(
        monkeypatch,
        assignment,
        UpdateAssignmentRequest(
            integration_element_bindings={SLACK: [SUPPORT]},
            integration_element_enforcement=[SLACK, TEAMS],
        ),
    )
    assert assignment.integration_element_enforcement == [SLACK]
    await _invoke_update_assignment(monkeypatch, assignment, UpdateAssignmentRequest(integration_element_bindings=None))
    assert assignment.integration_element_enforcement is None


def test_the_rest_create_paths_store_the_normalized_choice():
    agents = _source("app/api/v1/agents.py")
    assignments = _source("app/api/v1/assignments.py")
    for source in (agents, assignments):
        assert (
            "integration_element_enforcement=normalize_scope_enforcement(\n"
            "            body.integration_element_enforcement, body.integration_element_bindings\n"
            "        ),"
        ) in source
    assert "integration_element_enforcement=a.integration_element_enforcement," in agents
    assert "integration_element_enforcement=w.integration_element_enforcement," in assignments


# ---------------------------------------------------------------------------
# Copies: duplicate, export / import, AgentSchema, the agent builder
# ---------------------------------------------------------------------------


def test_every_copy_manifest_carries_the_choice_with_the_selection():
    assert "integration_element_enforcement" in AGENT_COPY_FIELDS
    assert "integration_element_enforcement" in AGENT_BUNDLE_PORTABLE_FIELDS
    assert "integration_element_enforcement" in ASSIGNMENT_COPY_FIELDS


async def test_a_duplicated_agent_and_its_assignments_keep_their_boundaries(monkeypatch):
    from app.api.v1 import agents as agents_api
    from tests.test_agent_duplicate_keeps_integrations import (
        _assignment,
        _AssignmentDB,
        _opencti_agent,
        _stub_duplicate_dependencies,
        _user,
    )

    source = _opencti_agent(
        integration_element_bindings={REPO: [API]},
        integration_element_enforcement=[REPO],
    )
    src_wf = _assignment(
        source.id,
        integration_element_bindings={SLACK: [SUPPORT]},
        integration_element_enforcement=[SLACK],
    )
    _stub_duplicate_dependencies(monkeypatch, source)
    db = _AssignmentDB([src_wf])
    await agents_api.duplicate_agent(
        str(source.id),
        SimpleNamespace(client=None, headers={}, url=None),
        user=_user(),
        db=db,
        _agentic_gate=None,
    )
    [copy] = [obj for obj in db.added if isinstance(obj, Agent)]
    [copy_wf] = [obj for obj in db.added if isinstance(obj, Assignment)]
    assert copy.integration_element_enforcement == [REPO]
    assert copy_wf.integration_element_enforcement == [SLACK]


async def test_an_export_import_round_trip_keeps_the_choice(monkeypatch):
    from app.api.v1 import agent_import as agent_import_api
    from app.services import agent_bundle_projection
    from tests.test_agent_duplicate_keeps_integrations import _FakeDB, _opencti_agent, _user

    source = _opencti_agent(
        integration_element_bindings={REPO: [API], SLACK: [SUPPORT]},
        integration_element_enforcement=[REPO],
    )
    bundle = await agent_bundle_projection.build_agent_export_bundle(_FakeDB(), source)
    assert bundle["agent"]["integration_element_enforcement"] == [REPO]

    async def _unique_slug(_db, _name):
        return "imported"

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_import_api, "generate_unique_agent_slug", _unique_slug)
    monkeypatch.setattr(agent_import_api, "seed_initial_agent_versions", _noop)
    monkeypatch.setattr(agent_import_api, "log_audit", _noop)
    imported = await agent_import_api.create_agent_from_bundle(_FakeDB(), user=_user(), bundle=bundle)
    assert imported.agent.integration_element_enforcement == [REPO]

    # A bundle is untrusted: a flag without its selection, or on a type no
    # provider enforces, is not stored.
    bundle["agent"]["integration_element_enforcement"] = [SLACK, TEAMS, ORG, 7]
    imported = await agent_import_api.create_agent_from_bundle(_FakeDB(), user=_user(), bundle=bundle)
    assert imported.agent.integration_element_enforcement == [SLACK]


def test_an_agentschema_round_trip_keeps_the_choice():
    from app.services.agentschema import build_agentschema_export, prepare_agent_import
    from tests.test_agentschema_mapping import _bundle

    bundle = _bundle(integration_element_bindings={REPO: [API]}, integration_element_enforcement=[REPO])
    text, _document = build_agentschema_export(bundle)
    prepared = prepare_agent_import(text.encode(), filename="agent.agent.yaml")
    assert prepared.bundle["agent"]["integration_element_enforcement"] == [REPO]


async def test_the_agent_builder_sets_and_clears_the_choice(monkeypatch):
    import app.core.database as database
    from app.services.builtin_tools import build_agent_builder_tool
    from tests.test_agent_builder_crud_tools import _call, _session_factory, _stub_writable_agent

    agent = Agent(id=uuid.uuid4(), name="Bound")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    ok = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        integration_element_bindings='{"github:repository": [{"id": "acme/api", "name": "api"}]}',
        integration_element_enforcement="github:repository, slack:slack_channel",
    )
    assert not ok.is_error
    assert agent.integration_element_enforcement == [REPO]

    refused = await _call(
        executor, "update_agent", agent_id=str(agent.id), integration_element_enforcement="github:organization"
    )
    assert "github:organization" in refused.content
    assert agent.integration_element_enforcement == [REPO]

    cleared = await _call(executor, "update_agent", agent_id=str(agent.id), integration_element_enforcement="clear")
    assert not cleared.is_error
    assert agent.integration_element_enforcement is None


def test_the_agent_builder_advertises_the_choice():
    from app.services.builtin_tools import build_agent_builder_tool

    definitions, _executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    [update_agent] = [d for d in definitions if d.name == "update_agent"]
    properties = update_agent.input_schema["properties"]
    assert "integration_element_enforcement" in properties
    assert "github:repository" in properties["integration_element_enforcement"]["description"]
    assert "Integration element enforcement:" in _source("app/services/builtin_tools/agent_builder.py")


def test_the_catalog_clone_and_the_public_download_carry_the_choice_with_the_selection():
    from app.models.agent_copy import (
        AGENT_CATALOG_CLONE_FIELDS,
        ASSIGNMENT_CATALOG_CLONE_FIELDS,
        PUBLIC_DOWNLOAD_AGENT_FIELDS,
        PUBLIC_DOWNLOAD_ASSIGNMENT_FIELDS,
        build_agent_catalog_clone_kwargs,
    )

    for fields in (
        AGENT_CATALOG_CLONE_FIELDS,
        ASSIGNMENT_CATALOG_CLONE_FIELDS,
        PUBLIC_DOWNLOAD_AGENT_FIELDS,
        PUBLIC_DOWNLOAD_ASSIGNMENT_FIELDS,
    ):
        assert "integration_element_bindings" in fields
        assert "integration_element_enforcement" in fields
    source = Agent(integration_element_bindings={REPO: [API]}, integration_element_enforcement=[REPO])
    assert build_agent_catalog_clone_kwargs(source)["integration_element_enforcement"] == [REPO]


def test_an_exported_assignment_keeps_its_choice_and_the_import_cleans_it():
    bundle = _source("app/services/agent_bundle_projection.py")
    importer = _source("app/api/v1/agent_import.py")
    assert '"integration_element_enforcement": wf.integration_element_enforcement,' in bundle
    assert 'wf_data.get("integration_element_enforcement"),' in importer


# ---------------------------------------------------------------------------
# The Security Audit: an informational list, never a score change
# ---------------------------------------------------------------------------


class _AuditDb:
    def __init__(self, agents: list, assignments: list):
        self._results = [agents, assignments]

    async def execute(self, *_args, **_kwargs):
        rows = self._results.pop(0)
        return SimpleNamespace(all=lambda: rows)


def _agent_row(name, bindings, enforcement=None):
    return SimpleNamespace(name=name, integration_element_bindings=bindings, integration_element_enforcement=enforcement)


def _assignment_row(name, bindings, enforcement, agent_name, agent_bindings, agent_enforcement):
    return SimpleNamespace(
        name=name,
        integration_element_bindings=bindings,
        integration_element_enforcement=enforcement,
        agent_name=agent_name,
        agent_bindings=agent_bindings,
        agent_enforcement=agent_enforcement,
    )


async def test_the_security_audit_lists_the_guidance_only_selections():
    triage = {REPO: [API], SLACK: [SUPPORT]}
    agents = [
        _agent_row("Triage", triage, [REPO]),
        _agent_row("Scout", {ORG: [{"id": "acme"}]}),
        _agent_row("Guard", {HUBSPOT: [DEAL]}, [HUBSPOT]),
    ]
    assignments = [
        # Its agent enforces repositories: the stricter wins, nothing to list.
        _assignment_row("Nightly", {REPO: [API]}, None, "Triage", triage, [REPO]),
        _assignment_row("Digest", {TEAMS: [GENERAL]}, None, "Triage", triage, [REPO]),
    ]
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_integration_scopes(_AuditDb(agents, assignments), findings)
    [finding] = findings
    assert (finding.id, finding.category, finding.severity) == (
        "integrations.integration_scopes_guidance_only",
        "integrations",
        "info",
    )
    assert "1 agent(s)" in finding.title and "1 assignment(s)" in finding.title
    assert "agent 'Triage' (Slack channels)" in finding.description
    assert "assignment 'Digest' of agent 'Triage' (Microsoft Teams channels)" in finding.description
    for enforced_or_unenforceable in ("Scout", "Guard", "Nightly"):
        assert enforced_or_unenforceable not in finding.description
    assert "INTEGRATION_SCOPE_ENFORCEMENT" not in finding.model_dump_json()


async def test_the_security_audit_says_nothing_when_every_selection_is_enforced():
    findings: list[security_audit.AuditFinding] = []
    agents = [_agent_row("Guard", {HUBSPOT: [DEAL]}, [HUBSPOT]), _agent_row("Plain", None)]
    await security_audit._check_integration_scopes(_AuditDb(agents, []), findings)
    assert findings == []


async def test_the_scope_finding_never_changes_the_score(monkeypatch):
    import inspect

    async def _no_finding(*_args, **_kwargs):
        return None

    for name, check in list(vars(security_audit).items()):
        if name.startswith("_check_") and name != "_check_integration_scopes":
            stub = _no_finding if inspect.iscoroutinefunction(check) else (lambda *_a, **_k: None)
            monkeypatch.setattr(security_audit, name, stub)
    db = _AuditDb([_agent_row("Triage", {REPO: [API]})], [])
    report = await security_audit.run_security_audit(admin=object(), db=db)  # type: ignore[arg-type]
    assert report.overall_score == "healthy"
    assert report.summary["info"] == 1
