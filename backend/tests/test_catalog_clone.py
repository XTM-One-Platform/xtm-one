"""Installing a Filigran AI Catalog agent into another account.

``POST /public/catalog/agents/{id}/add`` used to build the clone from a
hand-written list of 16 fields, so a published agent installed WITHOUT its tool
deny list, its tool scopes, its approval gate or its assignments' self-trigger
loop guard - it did more in the installer's account than it did in the
publisher's. The route now builds from ``AGENT_CATALOG_CLONE_FIELDS`` /
``ASSIGNMENT_CATALOG_CLONE_FIELDS`` and re-keys every reference onto what the
installer ends up with.

The route is driven end to end through a session double that answers each
query by the model it selects, so what is asserted is the rows the route would
insert.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.v1 import public_catalog
from app.core.config import settings
from app.core.utils import slugify
from app.models.agent import Agent
from app.models.agent_copy import REDACTED_MCP_SERVER_ID
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.custom_tool import CustomTool
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.prompt import Prompt
from app.models.variable import Variable
from app.services import agent_bundle_projection


class _Result:
    def __init__(self, rows, scalars=None):
        self._rows = list(rows)
        self._scalars = list(scalars) if scalars is not None else self._rows

    def scalar_one_or_none(self):
        return self._scalars[0] if self._scalars else None

    def scalars(self):
        return _Result(self._scalars)

    def all(self):
        return list(self._rows)


class _CatalogSession:
    """Answers ``select(Model)`` by model; lookups by the UUIDs bound in the
    statement (``IN`` lists included). A ``select(Model.col)`` answers rows of
    that column. Visibility predicates are NOT evaluated here - every row is
    visible - which is why ``test_catalog_clone_visibility_postgres.py`` proves
    them against a real database. Records what the route adds."""

    def __init__(self, source, assignments, rows):
        self._source = source
        self._assignments = assignments
        self._rows = rows
        self.added: list[object] = []
        self.committed = False
        # What each statement read, in order: the entity it selects, or the SQL
        # of a ``text()`` statement (the provider config advisory lock).
        self.executed: list[str] = []

    async def execute(self, stmt):
        descriptions = getattr(stmt, "column_descriptions", None)
        if descriptions is None:
            self.executed.append(str(stmt))
            return _Result([])
        description = descriptions[0]
        entity = description.get("entity")
        self.executed.append(getattr(entity, "__name__", repr(entity)))
        if entity is Agent:
            # A column select is the slug-uniqueness probe: no other agent
            # holds the slug.
            return _Result([self._source] if description.get("expr") is Agent else [])
        if entity is Assignment:
            return _Result(self._assignments)
        if not isinstance(entity, type):
            return _Result([])
        wanted: set[str] = set()
        for value in stmt.compile().params.values():
            values = value if isinstance(value, (list, tuple)) else [value]
            wanted.update(str(v) for v in values if isinstance(v, uuid.UUID))
        rows = [
            row for row in self._rows if isinstance(row, entity) and str(row.id) in wanted
        ]
        if description.get("expr") is entity:
            return _Result(rows)
        column = description["name"]
        return _Result(
            [(getattr(row, column),) for row in rows],
            scalars=[getattr(row, column) for row in rows],
        )

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        self.committed = True

    def of(self, model):
        return [obj for obj in self.added if isinstance(obj, model)]


async def _noop(*_a, **_kw):
    return None


def _ids():
    return str(uuid.uuid4())


def _installer(*, is_admin: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        full_name="Installer",
        email="i@example.com",
        is_admin=is_admin,
        role="admin" if is_admin else "user",
    )


def _published_agent(**values) -> Agent:
    return Agent(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Published agent",
        is_company_managed=True,
        is_public_catalog=True,
        persona="You triage issues.",
        **values,
    )


async def _install(monkeypatch, source, assignments=(), rows=(), *, user=None, body=None):
    monkeypatch.setattr(public_catalog, "log_audit", _noop)
    monkeypatch.setattr(public_catalog, "reconcile_agent_skill_package_bindings", _noop)
    monkeypatch.setattr(public_catalog, "_seed_initial_versions", _noop)
    session = _CatalogSession(source, list(assignments), list(rows))
    await public_catalog.add_catalog_item_to_account(
        entity_type="agents",
        entity_id=str(source.id),
        body=body,
        user=user or _installer(),
        db=session,
        _ai_gate=None,
    )
    [clone] = session.of(Agent)
    return clone, {a.name: a for a in session.of(Assignment)}, session


async def test_a_catalog_install_keeps_every_safety_field_and_none_of_the_publishers_reach(
    monkeypatch,
):
    monkeypatch.setattr(public_catalog, "log_audit", _noop)
    monkeypatch.setattr(public_catalog, "reconcile_agent_skill_package_bindings", _noop)
    monkeypatch.setattr(public_catalog, "_seed_initial_versions", _noop)

    publisher_id = uuid.uuid4()
    installer = _installer()

    shared_integration = Integration(
        id=uuid.uuid4(), provider="opencti", name="Shared OpenCTI", is_company_managed=True
    )
    private_integration = Integration(
        id=uuid.uuid4(), provider="github", name="Publisher GitHub", is_company_managed=False
    )
    installer_integration = Integration(
        id=uuid.uuid4(),
        user_id=installer.id,
        provider="github",
        name="Installer GitHub",
        is_company_managed=False,
    )
    installer_github = str(installer_integration.id)
    shared_kb = KnowledgeBase(id=uuid.uuid4(), name="Shared KB", is_company_managed=True)
    private_kb = KnowledgeBase(id=uuid.uuid4(), name="Private KB", is_company_managed=False)
    private_server = MCPServer(
        id=uuid.uuid4(), name="Publisher MCP", transport="http", url="https://mcp.example.com",
        is_company_managed=False,
    )
    shared_skill = AgentSkill(id=uuid.uuid4(), name="Shared skill", is_company_managed=True)
    publisher_bot = _ids()
    peer_agent = _ids()

    source = Agent(
        id=uuid.uuid4(),
        user_id=publisher_id,
        name="Published agent",
        is_company_managed=True,
        is_public_catalog=True,
        persona="You triage issues.",
        integration_template_bindings=["opencti"],
        integration_bindings={
            "opencti": [str(shared_integration.id)],
            "github": [str(private_integration.id)],
        },
        integration_allowed_tools={"github": ["get_issue"]},
        tool_deny_list=["github:delete_repo"],
        human_in_the_loop=True,
        human_in_the_loop_prompt="Ask before closing anything.",
        knowledge_base_ids=[str(shared_kb.id), str(private_kb.id)],
        knowledge_base_permissions={str(shared_kb.id): "write", str(private_kb.id): "write"},
        mcp_server_ids=[str(private_server.id)],
        mcp_allowed_tools={str(private_server.id): ["read_only_tool"]},
        skill_ids=[str(shared_skill.id)],
        skill_load_modes={str(shared_skill.id): "always"},
        channel_config={"slack": {"bot_id": publisher_bot}},
        available_channel_types=["web", "slack"],
        handover_agent_ids=[peer_agent],
        handover_types={peer_agent: "consult"},
        trace_supervision_target_agent_ids=[peer_agent],
        work_item_owner_agent_ids=[peer_agent],
        shared_user_ids=[_ids()],
        web_enabled=True,
        web_token="publisher-live-token",
    )
    outside_assignment = uuid.uuid4()
    second = Assignment(
        id=uuid.uuid4(),
        agent_id=source.id,
        user_id=publisher_id,
        name="Follow-up",
        trigger_type="manual",
        trigger_config={},
        prompt="Follow up.",
        next_assignment_id=outside_assignment,
        enabled=True,
    )
    first = Assignment(
        id=uuid.uuid4(),
        agent_id=source.id,
        user_id=publisher_id,
        name="Triage",
        trigger_type="manual",
        trigger_config={},
        prompt="Triage the issue.",
        activation_prompt="Only issues labelled bug.",
        ignore_self_triggered_events=True,
        human_in_the_loop=True,
        dry_run=True,
        run_as_user_id=publisher_id,
        run_as_all_users=True,
        integration_bindings={"github": [str(private_integration.id)]},
        channel_config={"slack": {"bot_id": publisher_bot}},
        tool_sources=[str(private_server.id), "named-server", _ids()],
        read_knowledge_base_ids=[str(private_kb.id)],
        write_knowledge_base_ids=[str(private_kb.id)],
        next_assignment_id=second.id,
        enabled=True,
    )
    session = _CatalogSession(
        source,
        [first, second],
        [
            shared_integration,
            private_integration,
            installer_integration,
            shared_kb,
            private_kb,
            private_server,
            shared_skill,
        ],
    )

    response = await public_catalog.add_catalog_item_to_account(
        entity_type="agents",
        entity_id=str(source.id),
        body=public_catalog.AgentSetupConfig(integration_bindings={"github": installer_github}),
        user=installer,
        db=session,
        _ai_gate=None,
    )

    assert session.committed
    [clone] = session.of(Agent)
    assert response.id == str(clone.id)
    [new_kb] = session.of(KnowledgeBase)
    [new_server] = session.of(MCPServer)

    # Ownership: the installer's, privately.
    assert clone.user_id == installer.id
    assert clone.is_company_managed is False
    # Every narrowing / safety field travels.
    assert clone.tool_deny_list == ["github:delete_repo"]
    assert clone.integration_allowed_tools == {"github": ["get_issue"]}
    assert clone.human_in_the_loop is True
    assert clone.human_in_the_loop_prompt == "Ask before closing anything."
    assert clone.integration_template_bindings == ["opencti"]
    # Integrations: shared ones kept, the installer's own choice wins, the
    # publisher's private integration never travels.
    assert clone.integration_bindings == {
        "opencti": [str(shared_integration.id)],
        "github": [installer_github],
    }
    # Re-keyed references.
    assert clone.knowledge_base_ids == [str(shared_kb.id), str(new_kb.id)]
    assert clone.knowledge_base_permissions == {str(new_kb.id): "write"}
    assert clone.mcp_server_ids == [str(new_server.id)]
    assert clone.mcp_allowed_tools == {str(new_server.id): ["read_only_tool"]}
    assert clone.skill_load_modes == {str(shared_skill.id): "always"}
    # None of the publisher's reach.
    assert clone.channel_config is None
    assert clone.available_channel_types == ["web"]
    assert clone.handover_agent_ids is None
    assert clone.handover_types is None
    assert clone.trace_supervision_target_agent_ids is None
    assert clone.work_item_owner_agent_ids is None
    assert clone.shared_user_ids is None
    assert clone.web_enabled is None and clone.web_token is None

    clones = {a.name: a for a in session.of(Assignment)}
    triage, follow_up = clones["Triage"], clones["Follow-up"]
    assert triage.agent_id == clone.id and triage.user_id == installer.id
    assert triage.enabled is False and triage.paused_reason == "created_disabled"
    # Safety fields of the assignment travel.
    assert triage.activation_prompt == "Only issues labelled bug."
    assert triage.ignore_self_triggered_events is True
    assert triage.human_in_the_loop is True
    assert triage.dry_run is True
    # Execution identity and bots of the publisher do not.
    assert triage.run_as_user_id is None and triage.run_as_all_users is None
    assert triage.channel_config is None
    # The assignment's integration subset is kept as a subset, re-bound onto
    # the installer's own GitHub integration; an assignment that inherited
    # keeps inheriting.
    assert triage.integration_bindings == {"github": [installer_github]}
    assert follow_up.integration_bindings is None
    # Re-keyed: the MCP id and the KB refinements follow the clone's rows; a
    # name passes through, and a server id the clone never bound becomes the
    # placeholder that names no server - dropped, a scope left empty would
    # widen to every server of the agent.
    assert triage.tool_sources == [str(new_server.id), "named-server", REDACTED_MCP_SERVER_ID]
    assert triage.read_knowledge_base_ids == [str(new_kb.id)]
    assert triage.write_knowledge_base_ids == [str(new_kb.id)]
    # Chains follow the cloned counterpart; a target outside the set is dropped.
    assert triage.next_assignment_id == follow_up.id
    assert follow_up.next_assignment_id is None


def test_assignment_integration_bindings_keep_their_tri_state():
    """NULL inherits every binding of the agent, {} means none, a subset is a
    subset: dropping the override would widen the assignment to every
    integration of the cloned agent."""
    rekey = public_catalog._rekey_assignment_integration_bindings
    shared, installer = _ids(), _ids()
    final = {"opencti": [shared], "github": [installer]}
    chosen = {"github": [installer]}

    assert rekey(None, final, chosen) is None
    assert rekey({}, final, chosen) == {}
    # A company-managed id the clone still binds is kept as is.
    assert rekey({"opencti": [shared]}, final, chosen) == {"opencti": [shared]}
    # A provider reached through the publisher's own integration is re-bound
    # onto the installer's own choice; the single-id shape is preserved.
    assert rekey({"github": _ids()}, final, chosen) == {"github": installer}
    assert rekey({"github": [_ids()]}, final, chosen) == {"github": [installer]}
    # A provider the clone does not bind is dropped - and when nothing
    # survives the subset stays an explicit "none", never "inherit all".
    assert rekey({"slack": [_ids()]}, final, chosen) == {}
    assert rekey({"slack": [_ids()], "github": [_ids()]}, final, chosen) == {
        "github": [installer]
    }
    # A malformed value fails closed.
    assert rekey(["not", "a", "map"], final, chosen) == {}


def test_an_assignment_subset_never_widens_onto_an_integration_nobody_picked():
    """The source assignment is limited to integration A for a provider; A does
    not travel and the clone binds company-managed B for that provider - but
    the installer skipped the picker, so B was not chosen for this assignment:
    the provider is dropped (fail-closed), never substituted. A provider the
    source selected nothing for stays unselected, whatever the clone binds."""
    rekey = public_catalog._rekey_assignment_integration_bindings
    auto_bound = _ids()
    final = {"opencti": [auto_bound]}

    assert rekey({"opencti": [_ids()]}, final, {}) == {}
    assert rekey({"opencti": _ids()}, final, {}) == {}
    assert rekey({"opencti": []}, final, {}) == {}
    assert rekey({"opencti": []}, final, {"opencti": [auto_bound]}) == {}
    assert rekey({"opencti": [None]}, final, {}) == {}
    # Another provider that survives keeps the subset (still narrower).
    kept = _ids()
    assert rekey(
        {"opencti": [_ids()], "github": [kept]}, {**final, "github": [kept]}, {}
    ) == {"github": [kept]}


async def test_a_narrowed_server_skill_or_prompt_is_left_out_of_an_install(monkeypatch):
    """A company-managed MCP server, skill or prompt narrowed to an audience
    the installer is not in (``bindable_ids`` leaves it out) is neither bound
    nor copied: a private copy would hand over content that is not the
    installer's to read. A private prompt is still copied."""
    owner = uuid.uuid4()
    audience_variable = Variable(
        id=uuid.uuid4(), user_id=owner, name="AUDIENCE_HOST", value="h", is_secret=False
    )
    narrowed = {"is_company_managed": True, "access_restricted": True}
    server = MCPServer(
        id=uuid.uuid4(), name="Audience MCP", transport="http",
        url="https://audience.invalid", **narrowed,
    )
    skill = AgentSkill(
        id=uuid.uuid4(), name="Audience skill",
        content=f"Call {{{{var_id.{audience_variable.id}}}}}", **narrowed,
    )
    prompt = Prompt(id=uuid.uuid4(), name="Audience prompt", content="audience tone", **narrowed)
    private_prompt = Prompt(
        id=uuid.uuid4(), name="Private prompt", content="house tone", is_company_managed=False
    )
    source = _published_agent(
        mcp_server_ids=[str(server.id)],
        mcp_allowed_tools={str(server.id): ["search"]},
        skill_ids=[str(skill.id)],
        skill_load_modes={str(skill.id): "always"},
        prompt_ids=[str(prompt.id), str(private_prompt.id)],
    )
    scoped = Assignment(
        id=uuid.uuid4(),
        agent_id=source.id,
        name="Scoped",
        trigger_type="manual",
        trigger_config={},
        prompt="p",
        tool_sources=[str(server.id)],
    )

    async def _nothing_bindable(*_args, **_kwargs):
        return set()

    monkeypatch.setattr(agent_bundle_projection, "bindable_ids", _nothing_bindable)
    clone, clones, session = await _install(
        monkeypatch,
        source,
        [scoped],
        [server, skill, prompt, private_prompt, audience_variable],
    )

    assert session.of(MCPServer) == [] and session.of(AgentSkill) == []
    assert not clone.mcp_server_ids and clone.mcp_allowed_tools is None
    assert not clone.skill_ids and clone.skill_load_modes is None
    [copied] = session.of(Prompt)
    assert copied.name == "Private prompt"
    assert clone.prompt_ids == [str(copied.id)]
    # The variable only the left-out skill names is not copied either.
    assert session.of(Variable) == []
    # The scope that named the left-out server stays narrow.
    assert clones["Scoped"].tool_sources == [REDACTED_MCP_SERVER_ID]


async def test_a_private_mcp_server_copy_never_carries_the_publishers_credentials(
    monkeypatch,
):
    """The installer's copy is theirs to read back (its export is raw), so a
    credential in the arguments or the URL is masked; the rest of the
    definition is copied as is."""
    token = "xoxb-" + "1" * 40
    server = MCPServer(
        id=uuid.uuid4(), name="Slack MCP", transport="stdio", command="npx",
        args=["-y", "slack-mcp", "--token", token, "--channel", "general"],
        url="https://relay.example.com/sse?api_key=publisher-key&region=eu",
        is_company_managed=False,
    )
    source = _published_agent(mcp_server_ids=[str(server.id)])

    clone, _, session = await _install(monkeypatch, source, rows=[server])

    [copy] = session.of(MCPServer)
    assert copy.args == ["-y", "slack-mcp", "--token", "***", "--channel", "general"]
    assert copy.url == "https://relay.example.com/sse?api_key=***&region=eu"
    assert token not in json.dumps(copy.args)
    assert clone.mcp_server_ids == [str(copy.id)]


# --- Review follow-up: ceilings, scopes, triggers, the wizard's payload -------


@pytest.mark.parametrize(("is_admin", "expected"), [(False, 50), (True, 5000)])
async def test_a_catalog_install_lowers_the_run_budget_to_the_installers_ceiling(
    monkeypatch, is_admin, expected
):
    """The publisher typed the budget, not the installer: like duplicate and
    import, the copy carries it at the installer's ceiling. An admin
    could have typed it anyway."""
    monkeypatch.setattr(settings, "agent_loop_max_tool_calls", 50)
    source = _published_agent(budget_config={"max_tool_calls": 5000})

    clone, _, _ = await _install(monkeypatch, source, user=_installer(is_admin=is_admin))

    assert clone.budget_config["max_tool_calls"] == expected


async def test_an_assignment_mcp_scope_the_clone_cannot_map_stays_narrow(monkeypatch):
    """``tool_sources`` empty means "every server of the agent" at run time:
    a scope naming only a server the clone does not bind must not collapse to
    that - it becomes the placeholder, which names no server."""
    source = _published_agent()
    scoped = Assignment(
        id=uuid.uuid4(),
        agent_id=source.id,
        name="Scoped",
        trigger_type="manual",
        trigger_config={},
        prompt="p",
        tool_sources=[_ids()],
    )

    _, clones, _ = await _install(monkeypatch, source, [scoped])

    assert clones["Scoped"].tool_sources == [REDACTED_MCP_SERVER_ID]
    assert uuid.UUID(REDACTED_MCP_SERVER_ID)


async def test_a_cloned_trigger_never_polls_or_drains_the_publishers_rows(monkeypatch):
    """``trigger_config.integration_id`` names the integration the poller reads
    (a non-OAuth one is read whoever the owner is); ``owner_agent_id`` names the
    work-item queue an inbox drains. Neither may point at the publisher's rows
    in the installer's account; an integration the clone binds is kept."""
    shared = Integration(
        id=uuid.uuid4(), provider="opencti", name="Shared OpenCTI", is_company_managed=True
    )
    private = Integration(
        id=uuid.uuid4(), provider="github", name="Publisher GitHub", is_company_managed=False
    )
    source = _published_agent(
        integration_bindings={"opencti": [str(shared.id)], "github": [str(private.id)]}
    )
    polls_private = Assignment(
        id=uuid.uuid4(),
        agent_id=source.id,
        name="Polls private",
        trigger_type="integration_event",
        trigger_config={
            "provider": "github",
            "event": "new_issue",
            "integration_id": str(private.id),
            "filter": {"scope": {"github_repository": ["org/repo"]}},
        },
        prompt="p",
    )
    polls_shared = Assignment(
        id=uuid.uuid4(),
        agent_id=source.id,
        name="Polls shared",
        trigger_type="integration_event",
        trigger_config={"provider": "opencti", "event": "x", "integration_id": str(shared.id)},
        prompt="p",
    )
    inbox = Assignment(
        id=uuid.uuid4(),
        agent_id=source.id,
        name="Inbox",
        trigger_type="work_item",
        trigger_config={"owner_agent_id": str(source.id), "correlation_prefix": "case-"},
        prompt="p",
    )

    _, clones, _ = await _install(
        monkeypatch, source, [polls_private, polls_shared, inbox], [shared, private]
    )

    private_trigger = clones["Polls private"].trigger_config
    assert "integration_id" not in private_trigger
    assert private_trigger["provider"] == "github"
    assert private_trigger["filter"] == {"scope": {"github_repository": ["org/repo"]}}
    assert clones["Polls shared"].trigger_config["integration_id"] == str(shared.id)
    # Absent, the inbox drains its own agent's queue - the clone's.
    assert clones["Inbox"].trigger_config == {"correlation_prefix": "case-"}


async def test_the_setup_body_accepts_the_list_the_install_wizard_sends(monkeypatch):
    """The wizard posts ``{provider: [integration id]}``; the body used to be
    typed ``{provider: id}``, so every install that picked an integration got a
    422. A bare id keeps working."""
    installer = _installer()
    own = Integration(
        id=uuid.uuid4(), user_id=installer.id, provider="github", name="Mine",
        is_company_managed=False,
    )
    for bindings in ({"github": [str(own.id)]}, {"github": str(own.id)}):
        body = public_catalog.AgentSetupConfig(integration_bindings=bindings)
        clone, _, _ = await _install(
            monkeypatch, _published_agent(), rows=[own], user=installer, body=body
        )
        assert clone.integration_bindings == {"github": [str(own.id)]}


def test_the_setup_body_still_refuses_what_is_not_an_id_list():
    with pytest.raises(ValidationError):
        public_catalog.AgentSetupConfig(integration_bindings={"github": {"id": "x"}})


async def test_a_setup_body_naming_a_malformed_integration_id_is_refused(monkeypatch):
    from fastapi import HTTPException

    body = public_catalog.AgentSetupConfig(integration_bindings={"github": ["not-a-uuid"]})
    with pytest.raises(HTTPException) as exc:
        await _install(monkeypatch, _published_agent(), body=body)
    assert exc.value.status_code == 400


async def test_a_catalog_install_gets_a_slug_and_an_initial_version(monkeypatch):
    """Every other creation path gives the agent a unique slug and seeds the
    "Initial version" of its persona and tracked config. Without a slug
    the MCP agent endpoint, which names each tool after it, never lists the
    installed agent; without the seed its Versioning tab has no baseline."""
    seeded: list[tuple[object, object]] = []

    async def _record(_db, *, agent, author_id):
        seeded.append((agent, author_id))

    monkeypatch.setattr(public_catalog, "log_audit", _noop)
    monkeypatch.setattr(public_catalog, "reconcile_agent_skill_package_bindings", _noop)
    monkeypatch.setattr(public_catalog, "_seed_initial_versions", _record)
    installer = _installer()
    source = _published_agent()
    session = _CatalogSession(source, [], [])

    await public_catalog.add_catalog_item_to_account(
        entity_type="agents",
        entity_id=str(source.id),
        body=None,
        user=installer,
        db=session,
        _ai_gate=None,
    )

    [clone] = session.of(Agent)
    assert clone.slug == slugify(clone.name) == "published-agent-copy"
    assert seeded == [(clone, installer.id)]


async def test_a_copied_prompt_or_skill_names_the_cloned_variables(monkeypatch):
    """A private prompt or skill is copied into the installer's account, and
    both are resolved against ``{{var_id.<uuid>}}`` at run time: the copy must
    name the variables the clone creates, like the agent's own text. A prompt
    bound as is stays shared - its references are not the clone's to rewrite,
    so they create nothing."""
    source = _published_agent()
    in_prompt, in_skill, in_shared = (
        Variable(id=uuid.uuid4(), user_id=source.user_id, name=name, value="v", is_secret=False)
        for name in ("IN_PROMPT", "IN_SKILL", "IN_SHARED")
    )
    private_prompt = Prompt(
        id=uuid.uuid4(),
        user_id=source.user_id,
        name="Tone",
        content=f"Region {{{{var_id.{in_prompt.id}}}}}",
        is_company_managed=False,
    )
    private_skill = AgentSkill(
        id=uuid.uuid4(),
        user_id=source.user_id,
        name="Triage",
        content=f"Use {{{{var_id.{in_skill.id}}}}}",
        is_company_managed=False,
    )
    shared_prompt = Prompt(
        id=uuid.uuid4(),
        name="House style",
        content=f"Sign {{{{var_id.{in_shared.id}}}}}",
        is_company_managed=True,
    )
    source.prompt_ids = [str(shared_prompt.id), str(private_prompt.id)]
    source.skill_ids = [str(private_skill.id)]

    clone, _, session = await _install(
        monkeypatch,
        source,
        rows=[in_prompt, in_skill, in_shared, private_prompt, private_skill, shared_prompt],
    )

    created = {v.name: str(v.id) for v in session.of(Variable)}
    assert set(created) == {"IN_PROMPT", "IN_SKILL"}
    [prompt_copy] = session.of(Prompt)
    [skill_copy] = session.of(AgentSkill)
    assert prompt_copy.content == f"Region {{{{var_id.{created['IN_PROMPT']}}}}}"
    assert skill_copy.content == f"Use {{{{var_id.{created['IN_SKILL']}}}}}"
    assert clone.prompt_ids == [str(shared_prompt.id), str(prompt_copy.id)]
    assert clone.skill_ids == [str(skill_copy.id)]


async def test_a_catalog_custom_tool_install_and_download_keep_its_language(monkeypatch):
    """A Python tool installed or downloaded without ``language`` runs as
    JavaScript; the download also lacked the ``timeout`` the owner's own export
    and the custom-tool import both carry."""
    monkeypatch.setattr(public_catalog, "log_audit", _noop)
    tool = CustomTool(
        id=uuid.uuid4(),
        name="Parse",
        description="Parses",
        js_function="def run(): return 1",
        language="python",
        timeout=42,
        is_company_managed=True,
        is_public_catalog=True,
    )
    session = _CatalogSession(None, [], [tool])

    await public_catalog.add_catalog_item_to_account(
        entity_type="custom-tools",
        entity_id=str(tool.id),
        body=None,
        user=_installer(),
        db=session,
        _ai_gate=None,
    )
    [clone] = session.of(CustomTool)
    assert clone.language == "python"
    assert clone.timeout == 42

    response = await public_catalog.download_catalog_item(
        entity_type="custom-tools", entity_id=str(tool.id), user=_installer(), db=session
    )
    exported = json.loads(response.body)["custom_tool"]
    assert exported["language"] == "python"
    assert exported["timeout"] == 42


async def test_a_catalog_install_reads_its_source_under_the_provider_lock(monkeypatch):
    """The copy carries the source's model choice, ``llm_provider_id``
    included, so it is read under the shared provider config lock: the advisory
    lock, then the config rows, before the source agent."""
    from app.models.llm_config import LLMProviderConfig

    config = LLMProviderConfig(id=uuid.uuid4(), provider="openai")
    source = _published_agent(
        llm_provider="openai", llm_provider_id=config.id, llm_model="gpt-4o"
    )

    clone, _, session = await _install(monkeypatch, source, rows=[config])

    assert "pg_advisory_xact_lock_shared(" in session.executed[0]
    assert session.executed[1] == "LLMProviderConfig"
    assert session.executed.index("Agent") > 1
    assert (clone.llm_provider, clone.llm_provider_id, clone.llm_model) == (
        "openai",
        config.id,
        "gpt-4o",
    )


async def test_a_catalog_install_drops_a_pin_to_a_config_that_is_gone(monkeypatch):
    """A pin to a config that no longer exists is not copied; the copy
    keeps the provider name and model, which the router resolves by owner."""
    source = _published_agent(
        llm_provider="openai", llm_provider_id=uuid.uuid4(), llm_model="gpt-4o"
    )

    clone, _, _ = await _install(monkeypatch, source)

    assert clone.llm_provider_id is None
    assert (clone.llm_provider, clone.llm_model) == ("openai", "gpt-4o")
