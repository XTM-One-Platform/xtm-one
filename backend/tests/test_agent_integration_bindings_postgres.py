"""End to end: an evaluation never runs an agent with an integration its
acting user may not use, the rule a chat turn follows.

The rows live in a throwaway schema on one connection: the guard's
visibility filter and the lending check run in PostgreSQL. The tool build
itself is captured, so no provider is reached.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.integration_user_credential import IntegrationUserCredential
from app.models.relay import RelayGroup
from app.models.user import User
from app.services import agent_chat, evaluation_engine, integration_tools, scheduled_work_item_runner
from app.tools.base import CallableToolExecutor, ToolDefinition

pytestmark = pytest.mark.integration

_OWNER = uuid.uuid4()
#: An admin who can see the agent, not its owner's personal integrations.
_COLLEAGUE = uuid.uuid4()
_GROUP = uuid.uuid4()
_PERSONAL = uuid.uuid4()  # the owner's GitHub, neither shared nor lent
_LENT = uuid.uuid4()  # the owner's Jira, lent through the agent


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot").strip()


@pytest.fixture
async def session():
    schema = f"agent_bindings_{uuid.uuid4().hex[:10]}"
    engine = create_async_engine(_database_url())
    try:
        async with engine.connect() as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(text(f'SET search_path TO "{schema}"'))
            for table in (
                User.__table__,
                Group.__table__,
                GroupMember.__table__,
                RelayGroup.__table__,
                Integration.__table__,
                IntegrationUserCredential.__table__,
            ):
                await conn.run_sync(lambda c, t=table: t.create(c))
            db = AsyncSession(bind=conn)
            for user_id, role in ((_OWNER, "user"), (_COLLEAGUE, "admin")):
                db.add(
                    User(
                        id=user_id,
                        email=f"{user_id}@example.com",
                        full_name="u",
                        hashed_password="x",
                        role=role,
                    )
                )
            db.add(Group(id=_GROUP, name=f"agents-{_GROUP}"))
            await db.flush()
            db.add(GroupMember(group_id=_GROUP, user_id=_COLLEAGUE))
            for integration_id, provider in ((_PERSONAL, "github"), (_LENT, "jira")):
                db.add(
                    Integration(
                        id=integration_id,
                        provider=provider,
                        name=f"owner {provider}",
                        status="connected",
                        auth_type="api_key",
                        access_token_enc="enc",
                        user_id=_OWNER,
                    )
                )
            await db.flush()
            try:
                yield db
            finally:
                await db.close()
                await conn.rollback()
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                await conn.commit()
    finally:
        await engine.dispose()


def _shared_agent() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Shared agent",
        user_id=_OWNER,
        is_company_managed=False,
        shared_group_ids=[str(_GROUP)],
        integration_bindings={"github": [str(_PERSONAL)], "jira": [str(_LENT)]},
        integration_template_bindings=[],
        integration_lending={"integration_ids": [str(_LENT)]},
        integration_allowed_tools={},
    )


@pytest.fixture
def built(monkeypatch) -> dict:
    """The bindings the evaluation's tools are built from."""
    captured: dict = {}

    async def build(_db, bindings, *, context=None, channel_bot_mode=False):
        captured["bindings"] = dict(bindings)
        definitions = [ToolDefinition(name=f"{p}_search", description="", source=f"integration:{p}") for p in bindings]
        return definitions, CallableToolExecutor()

    monkeypatch.setattr(integration_tools, "build_integration_tools", build)
    return captured


async def test_a_colleague_s_evaluation_never_gets_the_owner_s_personal_integration(session, built):
    evaluation = SimpleNamespace(id=uuid.uuid4(), user_id=_COLLEAGUE)

    bundle = await evaluation_engine._build_evaluation_integration_tools(session, _shared_agent(), evaluation)

    # The lent integration stays bound, as in chat; the personal one is withheld.
    assert built["bindings"] == {"jira": str(_LENT)}
    assert bundle is not None
    assert bundle.acting_user_id == str(_COLLEAGUE)


async def test_the_colleague_s_turn_says_what_it_withheld(session):
    resolved = await integration_tools.resolve_agent_integration_bindings(session, _shared_agent(), str(_COLLEAGUE))

    assert resolved.bindings == {"jira": str(_LENT)}
    assert [entry.get("provider") for entry in resolved.withheld] == ["github"]
    # An integration the person cannot see is named by its provider only.
    assert "owner github" not in repr(resolved.withheld)


async def test_the_owner_s_own_evaluation_keeps_every_binding(session, built):
    evaluation = SimpleNamespace(id=uuid.uuid4(), user_id=_OWNER)

    await evaluation_engine._build_evaluation_integration_tools(session, _shared_agent(), evaluation)

    assert built["bindings"] == {"github": str(_PERSONAL), "jira": str(_LENT)}


async def test_the_colleague_s_own_integration_of_the_provider_replaces_it(session, built):
    own = Integration(
        id=uuid.uuid4(),
        provider="github",
        name="colleague github",
        status="connected",
        auth_type="api_key",
        access_token_enc="enc",
        user_id=_COLLEAGUE,
    )
    session.add(own)
    await session.flush()
    evaluation = SimpleNamespace(id=uuid.uuid4(), user_id=_COLLEAGUE)

    await evaluation_engine._build_evaluation_integration_tools(session, _shared_agent(), evaluation)

    assert built["bindings"] == {"jira": str(_LENT), "github": str(own.id)}


async def test_a_turn_refuses_a_bundle_holding_what_its_resolution_withheld(session):
    resolved = await integration_tools.resolve_agent_integration_bindings(session, _shared_agent(), str(_COLLEAGUE))
    owner_built = evaluation_engine.IntegrationToolsBundle(
        [], "executor", bindings={"github": str(_PERSONAL), "jira": str(_LENT)}, acting_user_id=str(_COLLEAGUE)
    )

    refusal = integration_tools.prebuilt_tools_refusal(
        owner_built, agent=_shared_agent(), acting_user_id=str(_COLLEAGUE), effective_bindings=resolved.bindings
    )

    assert refusal is not None


# --- The pipeline tool ref's pinned integration ---------------------------------


def _github(name: str, *, user_id=None, company: bool = False) -> Integration:
    return Integration(
        id=uuid.uuid4(),
        provider="github",
        name=name,
        status="connected",
        auth_type="api_key",
        access_token_enc="enc",
        user_id=user_id,
        is_company_managed=company,
    )


def _ref(provider: str, integration_id: uuid.UUID) -> dict:
    return {
        "source": "integration",
        "provider": provider,
        "integration_id": str(integration_id),
        "tool_name": f"{provider}_search",
    }


def _unshared_agent() -> SimpleNamespace:
    """The same agent shared with nobody: the colleague reaches it as an admin only."""
    agent = _shared_agent()
    agent.shared_group_ids = []
    return agent


async def test_an_admin_the_agent_is_not_shared_with_never_runs_the_owner_s_pinned_integration(session, built):
    loaded = await agent_chat._load_pipeline_tool_standalone(
        session, _ref("github", _PERSONAL), str(_COLLEAGUE), _unshared_agent()
    )

    # Withheld (#683), and the colleague has no GitHub of their own to fall back on.
    assert loaded is None
    assert "bindings" not in built


@pytest.mark.parametrize(
    ("provider", "pinned", "acting"),
    [("jira", _LENT, _COLLEAGUE), ("github", _PERSONAL, _COLLEAGUE), ("github", _PERSONAL, _OWNER)],
    ids=["lent-to-the-colleague", "bound-and-shared-with-the-colleague", "the-owner-s-own-run"],
)
async def test_a_pinned_integration_the_person_may_use_stays(session, built, provider, pinned, acting):
    await agent_chat._load_pipeline_tool_standalone(session, _ref(provider, pinned), str(acting), _shared_agent())

    assert built["bindings"] == {provider: [str(pinned)]}


# --- Scheduled work items ----------------------------------------------------------


async def test_a_member_s_scheduled_work_item_keeps_what_the_agent_lends(session, built):
    agent = _shared_agent()
    agent.channel_config = None
    agent.custom_tool_ids = []
    agent.tool_deny_list = []

    await scheduled_work_item_runner.resolve_exact_toolset(session, agent, str(_COLLEAGUE))

    assert built["bindings"] == {"jira": str(_LENT)}


# --- Assignment runs ------------------------------------------------------------


def _assignment(**overrides) -> SimpleNamespace:
    values = {
        "id": uuid.uuid4(),
        "name": "Nightly",
        "integration_bindings": None,
        "integration_element_bindings": None,
        "integration_element_enforcement": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _run_bindings(session, acting, assignment=None, agent=None):
    from app.services import assignment_engine

    return await assignment_engine._resolve_run_integration_bindings(
        session, agent=agent or _shared_agent(), assignment=assignment or _assignment(), effective_user_id=acting
    )


async def test_a_colleague_s_assignment_run_never_gets_the_owner_s_personal_integration(session):
    resolved = await _run_bindings(session, _COLLEAGUE)

    # The lent integration stays bound, as in chat; the personal one is withheld, and said.
    assert resolved.bindings == {"jira": str(_LENT)}
    assert [entry.get("provider") for entry in resolved.withheld] == ["github"]
    assert resolved.tool_bindings.basis is integration_tools.BindingBasis.RESOLVED


@pytest.mark.parametrize("acting", [_OWNER, None], ids=["the-owner-s-run", "a-run-with-no-person"])
async def test_the_owner_s_assignment_run_keeps_every_binding(session, acting):
    resolved = await _run_bindings(session, acting)

    assert resolved.bindings == {"github": str(_PERSONAL), "jira": str(_LENT)}
    assert resolved.withheld == []


async def test_a_platform_health_run_with_no_user_keeps_no_binding(session):
    """It acts as no one, never as its agent's owner: the tasks it delegates do too."""
    from app.services.health.identity import HEALTH_ROLES

    agent = _shared_agent()
    agent.health_role = sorted(HEALTH_ROLES)[0]
    agent.is_company_managed = True
    # Not even a company-wide integration for its per-user bindings.
    agent.integration_template_bindings = ["github"]
    session.add(_github("company github", user_id=_OWNER, company=True))
    await session.flush()

    resolved = await _run_bindings(session, None, agent=agent)

    assert resolved.bindings == {}
    assert sorted(entry["provider"] for entry in resolved.withheld) == ["github", "jira"]
    assert {entry["reason"] for entry in resolved.withheld} == {"no_acting_person"}


async def test_a_colleague_s_assignment_run_uses_their_own_integration_of_the_provider(session):
    own = Integration(
        id=uuid.uuid4(),
        provider="github",
        name="colleague github",
        status="connected",
        auth_type="api_key",
        access_token_enc="enc",
        user_id=_COLLEAGUE,
    )
    session.add(own)
    await session.flush()

    resolved = await _run_bindings(session, _COLLEAGUE)

    assert resolved.bindings == {"jira": str(_LENT), "github": str(own.id)}
    assert resolved.withheld == []


async def test_a_withheld_provider_resolves_among_the_colleague_s_scoped_integrations_as_in_chat(session, monkeypatch):
    """Two of the colleague's GitHubs, scoped by the registered platform's URL:
    the one it names stands in, as in chat."""
    from app.models.platform_registration import PlatformRegistration

    await session.run_sync(lambda s: PlatformRegistration.__table__.create(s.connection()))
    session.add(
        PlatformRegistration(
            platform_identifier="github",
            platform_url="https://b.example",
            platform_title="b",
            platform_version="1",
            status="active",
        )
    )
    scoped = {
        url: _github(f"colleague github {url}", user_id=_COLLEAGUE)
        for url in ("https://a.example", "https://b.example")
    }
    urls = {}
    for url, integration in scoped.items():
        integration.scope_config = {"match_key": "url", "resolve_from_platform": "platform_url"}
        integration.extra_config_enc = "enc"
        urls[integration.id] = url
        session.add(integration)
    await session.flush()

    async def reveal(integration):
        return {"url": urls[integration.id]}

    monkeypatch.setattr(integration_tools, "_reveal_extra", reveal)

    resolved = await _run_bindings(session, _COLLEAGUE)

    assert resolved.bindings["github"] == str(scoped["https://b.example"].id)
    assert resolved.withheld == []


@pytest.mark.parametrize(
    ("acting", "kept"),
    [(_COLLEAGUE, False), (_OWNER, True)],
    ids=["a-colleague-s-run", "the-owner-s-run"],
)
async def test_an_assignment_s_own_binding_follows_the_same_rule(session, acting, kept):
    assignment = _assignment(integration_bindings={"github": [str(_PERSONAL)]})

    resolved = await _run_bindings(session, acting, assignment)

    assert resolved.bindings == ({"github": str(_PERSONAL)} if kept else {})
    assert resolved.origins == ({"github": "assignment_binding_override"} if kept else {})


@pytest.mark.parametrize(
    ("acting", "bound"),
    [(_COLLEAGUE, {"jira": str(_LENT)}), (_OWNER, {"github": str(_PERSONAL), "jira": str(_LENT)})],
    ids=["a-colleague-s-run", "the-owner-s-run"],
)
async def test_the_activation_check_builds_what_the_run_would(session, built, acting, bound):
    from app.services import assignment_engine

    agent = _shared_agent()
    for field in (
        "builtin_tool_ids",
        "builtin_allowed_tools",
        "integration_element_bindings",
        "integration_element_enforcement",
        "integration_signatures",
        "integration_read_only",
        "integration_live_writes",
    ):
        setattr(agent, field, None)
    agent.is_company_managed = False
    run = SimpleNamespace(id=uuid.uuid4(), user_id=acting, is_company_managed=False)

    await assignment_engine._activation_check_executor(
        session, tool="github_search", run=run, agent=agent, assignment=_assignment(), effective_user_id=acting
    )

    assert built["bindings"] == bound


# --- The flow Integration node ----------------------------------------------------


@pytest.mark.parametrize(("runner", "used"), [(_COLLEAGUE, False), (_OWNER, True)], ids=["colleague", "owner"])
async def test_a_flow_integration_node_runs_only_what_the_run_s_user_may_use(session, built, runner, used):
    from app.services.flow_runner import FlowWalker

    walker = SimpleNamespace(db=session, user=SimpleNamespace(id=runner), _renderer=None)

    outcome = await FlowWalker._exec_integration(
        walker, {"integration_id": str(_PERSONAL), "operation": "github_search", "parameters": {}}
    )

    if used:
        assert built["bindings"] == {"github": str(_PERSONAL)}
    else:
        assert "bindings" not in built
        # Chat's reason, not a generic refusal.
        assert outcome.error == "The run's user may not use the configured integration: the acting user cannot see it."


async def test_a_flow_integration_node_falls_back_to_the_run_s_user_s_own_integration(session, built):
    from app.services.flow_runner import FlowWalker

    own = _github("colleague github", user_id=_COLLEAGUE)
    session.add(own)
    await session.flush()
    walker = SimpleNamespace(db=session, user=SimpleNamespace(id=_COLLEAGUE), _renderer=None)

    await FlowWalker._exec_integration(
        walker, {"integration_id": str(_PERSONAL), "operation": "github_search", "parameters": {}}
    )

    assert built["bindings"] == {"github": str(own.id)}


# --- The turn's prompt and a run with no person -----------------------------------------


async def test_a_withheld_integration_is_never_listed_in_the_colleague_s_prompt(session):
    agent = _shared_agent()
    resolved = await integration_tools.resolve_agent_integration_bindings(session, agent, str(_COLLEAGUE))

    assert agent_chat.listed_integration_bindings(agent, resolved.bindings) == {"jira": str(_LENT)}


@pytest.mark.parametrize("acting", ["", "anonymous", "mcp-global", None])
async def test_a_run_with_no_person_keeps_no_binding_unless_declared_the_owner_s(session, acting):
    agent = _shared_agent()

    closed = await integration_tools.resolve_agent_integration_bindings(session, agent, acting)
    declared = await integration_tools.resolve_agent_integration_bindings(session, agent, acting, owner_run=True)

    assert closed.bindings == {}
    assert sorted(entry["provider"] for entry in closed.withheld) == ["github", "jira"]
    assert declared.bindings == {"github": str(_PERSONAL), "jira": str(_LENT)}


async def test_a_pipeline_pin_of_a_run_with_no_person_falls_back_to_nothing(session, built):
    """Not even to a company-wide integration: its direct bindings keep none."""
    session.add(_github("company github", user_id=_OWNER, company=True))
    await session.flush()

    loaded = await agent_chat._load_pipeline_tool_standalone(
        session, _ref("github", _PERSONAL), "anonymous", _shared_agent(), owner_run=False
    )

    assert loaded is None
    assert "bindings" not in built


# --- A pipeline pin copied from another agent -----------------------------------


@pytest.mark.parametrize("owner_run", [False, True], ids=["the-copy-s-owner", "its-public-callers"])
async def test_a_copied_pipeline_pin_never_runs_the_original_owner_s_integration(session, built, owner_run):
    # The colleague's copy of the shared agent: theirs, its pin still names
    # the original owner's personal GitHub.
    copy = _shared_agent()
    copy.user_id = _COLLEAGUE
    copy.integration_lending = {}

    loaded = await agent_chat._load_pipeline_tool_standalone(
        session,
        _ref("github", _PERSONAL),
        "anonymous" if owner_run else str(_COLLEAGUE),
        copy,
        owner_run=owner_run,
    )

    assert loaded is None
    assert "bindings" not in built
