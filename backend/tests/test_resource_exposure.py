"""Who can use a resource a person lends to others - the pure parts.

The audience algebra, what each resource is described as holding, what each
carrier is read as holding, and the pin that an entity's audience is exactly
the people ``user_visible_filter`` admits. The database-backed behaviour (every
path found, the before / after comparison of a preview) is in
``test_resource_exposure_postgres.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.v1.helpers import user_visible_filter
from app.models.integration import Integration
from app.models.mcp_server import MCPServer
from app.services import resource_exposure as ex
from tests.integration_credential_fakes import evaluate

ALICE = uuid.uuid4()
BOB = uuid.uuid4()
CAROL = uuid.uuid4()
GROUP = uuid.uuid4()


# ---------------------------------------------------------------------------
# Audience algebra
# ---------------------------------------------------------------------------


def test_beyond_keeps_only_the_people_the_other_audience_misses():
    wide = ex.Audience(user_ids={ALICE, BOB, CAROL}, named_user_ids={ALICE, BOB})
    narrow = ex.Audience(user_ids={ALICE})
    diff = wide.beyond(narrow)
    assert diff.user_ids == {BOB, CAROL}
    assert diff.named_user_ids == {BOB}


def test_everyone_beyond_a_few_excludes_them():
    diff = ex.Audience(everyone=True).beyond(ex.Audience(user_ids={ALICE}))
    assert diff.everyone and diff.excluded_user_ids == {ALICE}


def test_a_few_beyond_everyone_is_nobody_unless_excluded():
    everyone_but_bob = ex.Audience(everyone=True, excluded_user_ids={BOB})
    assert ex.Audience(user_ids={ALICE}).beyond(everyone_but_bob).is_empty
    assert ex.Audience(user_ids={ALICE, BOB}).beyond(everyone_but_bob).user_ids == {BOB}


@pytest.mark.parametrize("flag", ["public_link", "mcp_key_holders", "a2a_partners", "webhook_callers"])
def test_the_audiences_no_account_names_always_count(flag):
    reached = ex.Audience(**{flag: True}).beyond(ex.Audience(everyone=True))
    assert getattr(reached, flag) and not reached.is_empty
    assert ex.Audience(**{flag: True}).beyond(ex.Audience(**{flag: True})).is_empty


def test_anonymous_channels_are_compared_by_channel():
    diff = ex.Audience(anonymous_channels={"slack", "teams"}).beyond(ex.Audience(anonymous_channels={"slack"}))
    assert diff.anonymous_channels == {"teams"}


def test_without_removes_the_actor_and_the_holder_from_everyone_too():
    audience = ex.Audience(everyone=True).without([ALICE, None])
    assert audience.everyone and ALICE in audience.excluded_user_ids
    assert ex.Audience(user_ids={ALICE}).without([ALICE]).is_empty


def test_merge_is_a_union_that_keeps_everyone():
    merged = ex.Audience(user_ids={ALICE}).merge(ex.Audience(everyone=True, excluded_user_ids={ALICE, BOB}))
    assert merged.everyone and merged.excluded_user_ids == {BOB}


# ---------------------------------------------------------------------------
# What a resource holds
# ---------------------------------------------------------------------------


def _row(**fields: Any) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "user_id": ALICE,
        "is_company_managed": False,
        "shared_group_ids": None,
        "shared_user_ids": None,
        "access_restricted": False,
    }
    base.update(fields)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    ("kind", "fields", "expected", "account_shared"),
    [
        ("integration", {"auth_type": "oauth", "access_token_enc": "t"}, ex.HOLDS_OAUTH_ACCOUNT, False),
        (
            "integration",
            {"auth_type": "oauth", "access_token_enc": "t", "oauth_share_acknowledged": True},
            ex.HOLDS_OAUTH_ACCOUNT,
            True,
        ),
        ("integration", {"auth_type": "oauth"}, ex.HOLDS_NONE, True),
        ("integration", {"auth_type": "api_key", "api_key_enc": "k"}, ex.HOLDS_API_KEY, True),
        ("integration", {"auth_type": "bot_token", "api_key_enc": "k"}, ex.HOLDS_BOT_TOKEN, True),
        ("integration", {"auth_type": "platform_managed"}, ex.HOLDS_PLATFORM, True),
        ("mcp_server", {"auth_mode": "oauth", "access_token_enc": "t"}, ex.HOLDS_MCP_OAUTH, True),
        ("mcp_server", {"auth_mode": "headers", "headers_enc": "h"}, ex.HOLDS_MCP_HEADERS, True),
        ("mcp_server", {"env_enc": "e"}, ex.HOLDS_MCP_ENV, True),
        ("mcp_server", {}, ex.HOLDS_NONE, True),
        ("custom_tool", {}, ex.HOLDS_CODE, True),
        ("channel_bot", {"api_key_enc": "k"}, ex.HOLDS_BOT_TOKEN, True),
        ("variable", {"is_secret": True}, ex.HOLDS_VALUE, True),
        ("variable", {"is_secret": False}, ex.HOLDS_PLAIN_VALUE, True),
    ],
)
def test_describe_secret_names_what_is_stored(kind, fields, expected, account_shared):
    secret = ex.describe_secret(kind, _row(**fields))
    assert secret["kind"] == expected
    assert secret["account_shared"] is account_shared


def test_the_holder_of_a_token_kept_by_an_owner_transfer_is_its_connector():
    row = _row(auth_type="oauth", access_token_enc="t", user_id=BOB, oauth_token_owner_id=ALICE)
    assert ex.describe_secret("integration", row)["holder_id"] == str(ALICE)


def test_a_channel_bot_is_always_the_company_s():
    assert ex.describe_secret("channel_bot", _row(api_key_enc="k"))["is_company"] is True


def test_only_a_credential_minted_per_user_or_none_lends_nothing():
    assert not ex.lends_something(ex.describe_secret("integration", _row(auth_type="platform_managed")))
    assert not ex.lends_something(ex.describe_secret("integration", _row(auth_type="oauth")))
    assert ex.lends_something(ex.describe_secret("custom_tool", _row()))
    assert ex.lends_something(ex.describe_secret("variable", _row(is_secret=False)))


# ---------------------------------------------------------------------------
# What a carrier holds
# ---------------------------------------------------------------------------

I1, I2, M1, T1, B1, V1, V2 = (str(uuid.uuid4()) for _ in range(7))


def _ids(held, kind):
    return {k: sorted(v) for k, v in held.get(kind, {}).items()}


def test_an_agent_holds_every_kind_of_resource():
    agent = SimpleNamespace(
        integration_bindings={"github": [I1]},
        prepare_tool_ref={"source": "integration", "integration_id": I2},
        finalize_tool_ref={"source": "custom_tool", "tool_id": T1},
        mcp_server_ids=[M1],
        custom_tool_ids=[T1],
        channel_config={"slack": {"channel_bot_id": B1, "receive_enabled": True}},
        persona=f"Use {{{{var_id.{V1}}}}}",
        guardrails=None,
        handover_prompt=None,
        human_in_the_loop_prompt=f"{{{{var_id.{V2}}}}}",
    )
    held = ex.held_resources(ex.PATH_AGENT, agent)
    assert _ids(held, "integration") == {I1: ["binding"], I2: ["pipeline"]}
    assert _ids(held, "mcp_server") == {M1: ["binding"]}
    assert _ids(held, "custom_tool") == {T1: ["binding", "pipeline"]}
    assert _ids(held, "channel_bot") == {B1: ["channel"]}
    assert _ids(held, "variable") == {V1: ["reference"], V2: ["reference"]}


def test_an_assignment_holds_only_its_own_overrides():
    assignment = SimpleNamespace(
        integration_bindings={"jira": [I1]},
        trigger_config={"integration_id": I2},
        tool_sources=[M1],
        channel_config={"teams": {"channel_bot_id": B1}},
        prompt=f"{{{{var_id.{V1}}}}}",
        activation_prompt=None,
        ai_label_prompt=None,
        human_in_the_loop_prompt=None,
    )
    held = ex.held_resources(ex.PATH_ASSIGNMENT, assignment)
    assert _ids(held, "integration") == {I1: ["assignment_override"], I2: ["trigger"]}
    assert _ids(held, "mcp_server") == {M1: ["assignment_override"]}
    assert _ids(held, "channel_bot") == {B1: ["assignment_override"]}
    assert _ids(held, "variable") == {V1: ["reference"]}
    empty = SimpleNamespace(integration_bindings=None, trigger_config={}, tool_sources=None)
    assert ex.held_resources(ex.PATH_ASSIGNMENT, empty) == {}


def test_a_flow_holds_its_nodes_resources():
    graph = {
        "nodes": [
            {"id": "a", "kind": "integration", "config": {"integration_id": I1}},
            {"id": "b", "kind": "capability.integration", "config": {"integration_ids": [I2]}},
            {"id": "c", "kind": "action.script", "config": {"custom_tool_id": T1}},
            {"id": "d", "kind": "capability.tool", "config": {"tool_ids": ["web_search", T1]}},
            {"id": "e", "kind": "action.send_message", "config": {"message": f"{{{{var_id.{V1}}}}}"}},
        ]
    }
    held = ex.held_resources(ex.PATH_FLOW, SimpleNamespace(graph=graph))
    assert set(held["integration"]) == {I1, I2}
    assert set(held["custom_tool"]) == {T1}
    assert set(held["variable"]) == {V1}


def test_knowledge_bases_skills_prompts_custom_tools_and_the_assistants():
    kb = ex.held_resources(ex.PATH_KNOWLEDGE_BASE, SimpleNamespace(), sync_source_integration_ids=[I1])
    assert _ids(kb, "integration") == {I1: ["sync_source"]}
    skill = ex.held_resources(
        ex.PATH_SKILL, SimpleNamespace(source_integration_id=uuid.UUID(I2), content=f"{{{{var_id.{V1}}}}}")
    )
    assert _ids(skill, "integration") == {I2: ["skill_source"]}
    assert set(skill["variable"]) == {V1}
    prompt = ex.held_resources(ex.PATH_PROMPT, SimpleNamespace(content=f"{{{{var_id.{V2}}}}}"))
    assert set(prompt["variable"]) == {V2}
    tool = ex.held_resources(ex.PATH_CUSTOM_TOOL, SimpleNamespace(js_function=f"'{{{{var_id.{V1}}}}}'", description=""))
    assert set(tool["variable"]) == {V1}
    ga = ex.held_resources(
        ex.PATH_GENERAL_ASSISTANT,
        SimpleNamespace(
            integration_bindings={"slack": [I1]},
            mcp_server_ids=[M1],
            custom_tool_ids=[T1],
            channel_config={"slack": {"channel_bot_id": B1}},
            system_prompt=None,
        ),
    )
    assert set(ga) == {"integration", "mcp_server", "custom_tool", "channel_bot"}
    mine = ex.held_resources(
        ex.PATH_PERSONAL_ASSISTANT,
        SimpleNamespace(ga_integration_bindings={"slack": [I1]}, ga_mcp_server_ids=[M1], ga_custom_tool_ids=None),
    )
    assert set(mine) == {"integration", "mcp_server"}


def test_what_an_agent_attaches_and_resolves_per_user_is_held_with_its_reason():
    agent = SimpleNamespace(integration_bindings={"github": [I1]})
    held = ex.held_resources(
        ex.PATH_AGENT,
        agent,
        attached={"integration": {I1: {"attached_skill"}}, "variable": {V1: {"attached_prompt"}}},
    )
    assert _ids(held, "integration") == {I1: ["attached_skill", "binding"]}
    assert _ids(held, "variable") == {V1: ["attached_prompt"]}
    assignment = SimpleNamespace(integration_bindings=None, trigger_config={})
    held = ex.held_resources(ex.PATH_ASSIGNMENT, assignment, per_user_integration_ids=[I2])
    assert _ids(held, "integration") == {I2: ["per_user_template"]}


def test_an_assignment_resolves_per_user_bindings_as_whom_it_runs_as():
    agent = SimpleNamespace(
        user_id=ALICE,
        is_company_managed=False,
        integration_template_bindings=["github", "jira"],
        integration_bindings={},
    )
    plain = SimpleNamespace(run_as_user_id=BOB, run_as_all_users=False, integration_bindings=None)
    assert ex._per_user_request(plain, agent) == (ALICE, {"github", "jira"})
    overridden = SimpleNamespace(run_as_user_id=None, run_as_all_users=False, integration_bindings={"github": [I1]})
    assert ex._per_user_request(overridden, agent) == (ALICE, {"jira"})
    added = SimpleNamespace(user_id=CAROL, run_as_user_id=None, run_as_all_users=False, integration_bindings=None)
    assert ex._per_user_request(added, agent) == (CAROL, {"github", "jira"})
    company = SimpleNamespace(**{**vars(agent), "is_company_managed": True})
    assert ex._per_user_request(plain, company) == (BOB, {"github", "jira"})
    fan_out = SimpleNamespace(run_as_user_id=None, run_as_all_users=True, integration_bindings=None)
    assert ex._per_user_request(fan_out, company) is None


def test_malformed_ids_are_skipped_and_spellings_are_canonical():
    agent = SimpleNamespace(mcp_server_ids=["not-a-uuid", M1.upper(), None, 3])
    assert set(ex.held_resources(ex.PATH_AGENT, agent)["mcp_server"]) == {M1}


def test_every_scanned_column_exists_on_its_model():
    for kind, carriers in ex._SCANNED.items():
        assert kind in ex.RESOURCE_KINDS
        for path, model, columns in carriers:
            assert path in ex.CARRIER_PATHS
            for column in columns:
                assert hasattr(model, column), (kind, model.__name__, column)


def test_every_proposable_field_is_copied():
    for entity_type, fields in ex._PROPOSABLE.items():
        assert entity_type in ex.ENTITY_TYPES
        assert set(fields) - ex._CHANGE_ONLY_FIELDS <= set(ex._COPIED_FIELDS), entity_type


# ---------------------------------------------------------------------------
# The audience is the inverse of the visibility filter
# ---------------------------------------------------------------------------


class _Resolver(ex.AudienceResolver):
    def __init__(self, members: dict[uuid.UUID, set[uuid.UUID]]):
        super().__init__(db=None)  # type: ignore[arg-type]
        self._members = members


@pytest.mark.parametrize(
    "sharing",
    [
        {},
        {"shared_user_ids": [str(BOB)]},
        {"shared_group_ids": [str(GROUP)]},
        {"is_company_managed": True},
        {"is_company_managed": True, "access_restricted": True},
        {"is_company_managed": True, "access_restricted": True, "shared_group_ids": [str(GROUP)]},
    ],
)
@pytest.mark.parametrize("model", [Integration, MCPServer])
async def test_entity_audience_admits_exactly_whom_user_visible_filter_admits(sharing, model):
    row = SimpleNamespace(
        **{
            "id": uuid.uuid4(),
            "user_id": ALICE,
            "is_company_managed": False,
            "access_restricted": False,
            "shared_group_ids": None,
            "shared_user_ids": None,
            **sharing,
        }
    )
    resolver = _Resolver({GROUP: {CAROL}})
    audience = await resolver.entity_audience(row)
    membership = {ALICE: [], BOB: [], CAROL: [str(GROUP)]}
    for person, groups in membership.items():
        admitted = evaluate(user_visible_filter(model, person, groups), row) is True
        reached = (audience.everyone and person not in audience.excluded_user_ids) or person in audience.user_ids
        assert reached is admitted, (sharing, person)


async def test_an_internal_entity_reaches_nobody():
    assert (await _Resolver({}).entity_audience(SimpleNamespace(is_internal=True, user_id=ALICE))).is_empty


# ---------------------------------------------------------------------------
# The proposed copy and the routes
# ---------------------------------------------------------------------------


def test_a_proposed_copy_applies_only_what_the_entity_type_accepts():
    stored = SimpleNamespace(id=uuid.uuid4(), user_id=ALICE, name="Agent", web_enabled=False, is_company_managed=False)
    copy = ex.proposed_copy(
        stored,
        {"web_enabled": True, "graph": {"nodes": []}, "new_owner_id": str(BOB)},
        entity_type="agent",
    )
    assert copy.web_enabled is True and copy.graph is None and copy.user_id == BOB
    assert stored.web_enabled is False and stored.user_id == ALICE


def test_a_new_entity_belongs_to_its_creator_and_says_what_it_will_hold():
    parent = SimpleNamespace(id=uuid.uuid4())
    assignment = ex.proposed_copy(None, {"tool_sources": [M1]}, entity_type="assignment", actor_id=ALICE, parent=parent)
    assert assignment.user_id == ALICE and assignment.agent_id == parent.id
    server = ex.proposed_copy(
        None, {"holds_secret": True, "shared_group_ids": [str(GROUP)]}, entity_type="mcp_server", actor_id=ALICE
    )
    assert ex.describe_secret("mcp_server", server)["kind"] == ex.HOLDS_MCP_HEADERS


def test_every_resource_has_its_exposure_and_summary_routes():
    from app.main import application

    paths = [getattr(r, "path", "") for r in application.routes]
    for segment in ("integrations", "mcp-servers", "custom-tools", "channel-bots", "variables"):
        summary = f"/api/v1/exposure/{segment}/summary"
        detail = f"/api/v1/exposure/{segment}/{{resource_id}}"
        assert summary in paths and detail in paths
        assert paths.index(summary) < paths.index(detail), segment
    assert "/api/v1/exposure/preview" in paths
    # The integration pages' own routes are untouched.
    assert "/api/v1/integrations/{integration_id}/exposure" in paths


def test_the_preview_only_reads_what_a_change_may_set():
    from app.api.v1.schemas.exposure import ExposurePreviewRequest

    body = ExposurePreviewRequest(entity_type="agent", entity_id=str(uuid.uuid4()), change={"web_enabled": True})
    assert body.change == {"web_enabled": True}
    with pytest.raises(ValueError):
        ExposurePreviewRequest(entity_type="not_a_thing")


class _FixedCarrier(ex.AudienceResolver):
    """Every carrier reaches *audience*: what is under test is what of it is lent."""

    def __init__(self, audience: ex.Audience):
        super().__init__(db=None)  # type: ignore[arg-type]
        self._audience = audience

    async def carrier_audience(self, kind, entity, parent=None):
        return self._audience


@pytest.mark.parametrize(
    ("mode", "path", "carrier_owner", "public_link", "expected"),
    [
        # Shared with its account: a carrier lends it to its whole audience...
        pytest.param("shared", ex.PATH_FLOW, BOB, False, "all", id="shared_flow"),
        # ...but an agent's chats and runs use their own for whoever cannot
        # see it: it lends it to its owner's own runs...
        pytest.param("shared", ex.PATH_AGENT, BOB, False, "owner", id="shared_agent_chats"),
        # ...and to the callers no account names, who act as its owner.
        pytest.param("shared", ex.PATH_AGENT, BOB, True, "owner_and_public_link", id="shared_agent_public_link"),
        # Shared without it: an agent's chats act with each person's own...
        pytest.param("per_user", ex.PATH_AGENT, ALICE, False, "nobody", id="per_user_agent_chats"),
        # ...its public link acts as its owner, the holder...
        pytest.param("per_user", ex.PATH_AGENT, ALICE, True, "public_link", id="per_user_agent_public_link"),
        # ...and someone else's agent never acts with the holder's account.
        pytest.param("per_user", ex.PATH_AGENT, BOB, True, "nobody", id="per_user_someone_else_s_agent"),
        pytest.param("per_user", ex.PATH_FLOW, ALICE, False, "all", id="per_user_holder_s_flow"),
        pytest.param("per_user", ex.PATH_FLOW, BOB, False, "nobody", id="per_user_someone_else_s_flow"),
    ],
)
async def test_an_integration_shared_without_its_account_lends_it_only_as_its_holder(
    mode, path, carrier_owner, public_link, expected
):
    integration = _row(
        auth_type="oauth",
        access_token_enc="t",
        oauth_share_acknowledged=mode == "shared",
        credential_mode=mode,
        oauth_token_owner_id=None,
        shared_group_ids=[str(GROUP)],
    )
    secret = ex.describe_secret(ex.RESOURCE_INTEGRATION, integration)
    carrier = ex.Audience(user_ids={ALICE, BOB, CAROL}, group_ids={GROUP}, public_link=public_link)
    ref = ex.Reference(path, _row(user_id=carrier_owner), {ex.REASON_BINDING}, None)

    lent = await ex.lent_audience(_FixedCarrier(carrier), ex.RESOURCE_INTEGRATION, integration, ref, secret)

    if expected == "all":
        assert lent == carrier
    elif expected.startswith("owner"):
        assert lent == ex.Audience(user_ids={BOB}, public_link=expected == "owner_and_public_link")
    elif expected == "public_link":
        assert lent.public_link and not lent.user_ids and not lent.group_ids
    else:
        assert lent.is_empty


@pytest.mark.parametrize(
    ("kind", "reasons", "credential_mode", "chat_uses_own"),
    [
        # Bound: a chat turn drops it for whoever cannot see it.
        pytest.param(ex.RESOURCE_INTEGRATION, {ex.REASON_BINDING}, "shared", True, id="bound"),
        # A pipeline pin is checked like a binding.
        pytest.param(ex.RESOURCE_INTEGRATION, {ex.REASON_PIPELINE}, "shared", True, id="pipeline"),
        pytest.param(
            ex.RESOURCE_INTEGRATION, {ex.REASON_BINDING, ex.REASON_PIPELINE}, "shared", True, id="bound_and_pipeline"
        ),
        # What it attaches is read on the attachment's own terms.
        pytest.param(
            ex.RESOURCE_INTEGRATION, {ex.REASON_ATTACHED_KNOWLEDGE_BASE}, "shared", False, id="attached_knowledge_base"
        ),
        # Per user, every chat already acts with each person's own.
        pytest.param(ex.RESOURCE_INTEGRATION, {ex.REASON_BINDING}, "per_user", False, id="per_user"),
        # Its chats run what else it carries for everyone.
        pytest.param(ex.RESOURCE_MCP_SERVER, {ex.REASON_BINDING}, "shared", False, id="mcp_server"),
    ],
)
async def test_an_agent_lends_what_its_chats_and_runs_drop_only_to_the_callers_no_account_names(
    kind, reasons, credential_mode, chat_uses_own
):
    resource = _row(
        auth_type="api_key", auth_mode="headers", api_key_enc="k", headers_enc="h", credential_mode=credential_mode
    )
    secret = ex.describe_secret(kind, resource)
    carrier = ex.Audience(user_ids={ALICE, BOB, CAROL}, group_ids={GROUP}, webhook_callers=True)
    ref = ex.Reference(ex.PATH_AGENT, _row(user_id=ALICE), set(reasons), None)

    lent = await ex.lent_audience(_FixedCarrier(carrier), kind, resource, ref, secret)

    if chat_uses_own:
        assert lent == ex.Audience(user_ids={ALICE}, webhook_callers=True)
        assert ex.path_notes(kind, resource, ref, True) == [ex.NOTE_CHAT_USES_OWN]
    elif credential_mode == "per_user":
        assert lent == ex.Audience(webhook_callers=True)
        assert ex.NOTE_CHAT_USES_OWN not in ex.path_notes(kind, resource, ref, True)
    else:
        assert lent == carrier
        assert ex.NOTE_CHAT_USES_OWN not in ex.path_notes(kind, resource, ref, True)


@pytest.mark.parametrize(
    ("reasons", "credential_mode", "lent_to"),
    [
        # Its own binding: its runs use their own for whoever cannot see it,
        # its webhook callers act as the person it runs as.
        pytest.param({ex.REASON_OVERRIDE}, "shared", "webhook_callers", id="override"),
        # A trigger reads its integration for every run.
        pytest.param({ex.REASON_TRIGGER}, "shared", "carrier", id="trigger"),
        pytest.param({ex.REASON_OVERRIDE, ex.REASON_TRIGGER}, "shared", "carrier", id="override_and_trigger"),
    ],
)
async def test_an_assignment_lends_what_its_runs_drop_only_to_its_webhook_callers(reasons, credential_mode, lent_to):
    resource = _row(auth_type="api_key", api_key_enc="k", credential_mode=credential_mode)
    secret = ex.describe_secret(ex.RESOURCE_INTEGRATION, resource)
    carrier = ex.Audience(user_ids={ALICE, BOB, CAROL}, group_ids={GROUP}, webhook_callers=True)
    ref = ex.Reference(ex.PATH_ASSIGNMENT, _row(user_id=ALICE), set(reasons), _row(user_id=ALICE))

    lent = await ex.lent_audience(_FixedCarrier(carrier), ex.RESOURCE_INTEGRATION, resource, ref, secret)

    assert lent == (ex.Audience(webhook_callers=True) if lent_to == "webhook_callers" else carrier)


# ---------------------------------------------------------------------------
# Orphaned bindings: the agent's owner can no longer see the integration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("owner", "company", "reasons", "seeing", "expected"),
    [
        pytest.param(ALICE, False, {ex.REASON_BINDING}, {ALICE}, False, id="owner_sees_it"),
        pytest.param(ALICE, False, {ex.REASON_BINDING}, set(), True, id="owner_lost_access"),
        pytest.param(ALICE, True, {ex.REASON_BINDING}, set(), False, id="company_agent"),
        pytest.param(None, False, {ex.REASON_BINDING}, set(), False, id="no_owner"),
        pytest.param(ALICE, False, {ex.REASON_PIPELINE}, set(), False, id="pipeline_only"),
    ],
)
async def test_an_agent_is_orphaned_only_when_its_owner_no_longer_sees_what_it_binds(
    monkeypatch, owner, company, reasons, seeing, expected
):
    integration = _row()
    agent = _row(user_id=owner, is_company_managed=company)
    ref = ex.Reference(ex.PATH_AGENT, agent, set(reasons), None)
    asked: list[set] = []

    async def _seeing(_db, _iid, user_ids):
        asked.append(set(user_ids))
        return set(seeing)

    monkeypatch.setattr(ex, "users_seeing_integration", _seeing)

    orphaned = await ex.orphaned_agent_ids(None, ex.RESOURCE_INTEGRATION, integration, [ref])

    assert orphaned == ({str(agent.id)} if expected else set())


async def test_no_other_resource_kind_has_orphaned_agents():
    ref = ex.Reference(ex.PATH_AGENT, _row(), {ex.REASON_BINDING}, None)
    assert await ex.orphaned_agent_ids(None, ex.RESOURCE_MCP_SERVER, _row(), [ref]) == set()


def test_an_orphaned_agent_says_so_and_no_longer_says_its_chats_use_their_own():
    integration = _row(auth_type="api_key", api_key_enc="k", credential_mode="shared")
    ref = ex.Reference(ex.PATH_AGENT, _row(), {ex.REASON_BINDING}, None)

    assert ex.path_notes(ex.RESOURCE_INTEGRATION, integration, ref, False, orphaned=True) == [ex.NOTE_OWNER_LOST_ACCESS]
    assert ex.NOTE_OWNER_LOST_ACCESS not in ex.path_notes(ex.RESOURCE_INTEGRATION, integration, ref, True)
