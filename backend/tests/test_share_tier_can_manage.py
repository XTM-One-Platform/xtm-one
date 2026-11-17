"""``can_manage`` on shareable entity responses.

A ``read_only`` grantee - through a group or a direct grant - saw an enabled
Edit button on a shared integration, and on every other shareable entity: the
UI counted any share as a manage right. Every save then dead-ended in a 403.

List and detail responses now carry the requesting user's manage right,
computed by ``helpers.can_manage`` - the rule every write route enforces - so
the UI gates its edit surfaces on the answer the API will give instead of
re-deriving share tiers in the browser.

The routers run for real over an in-memory session that serves the seeded rows:
what is under test is the flag, not the visibility filter.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api import deps
from app.api.deps import (
    require_agentic_ai,
    require_agentic_ai_licensed,
    require_ai_enabled,
)
from app.api.v1 import (
    agent_chats,
    agent_skills,
    agents,
    custom_tools,
    flows,
    integration_catalog,
    integrations,
    knowledge_bases,
    mcp_servers,
    prompts,
    template_containers,
)
from app.core.database import get_db
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.agent_skill_package_origin import AgentSkillPackageOrigin
from app.models.chat import Conversation
from app.models.custom_tool import CustomTool
from app.models.flow import Flow
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.prompt import Prompt
from app.models.template_container import TemplateContainer

NOW = datetime.now(UTC)
GROUP_READ_ONLY = str(uuid.uuid4())
GROUP_CONFIG = str(uuid.uuid4())
GROUP_CHATS = str(uuid.uuid4())


def _viewer(*, role: str = "user", groups: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        role=role,
        is_admin=role == "admin",
        full_name="Viewer",
        email="viewer@example.com",
        group_ids=list(groups),
    )


OWNER = _viewer()
ADMIN = _viewer(role="admin")
READ_ONLY_GROUP_MEMBER = _viewer(groups=(GROUP_READ_ONLY,))
CONFIG_GROUP_MEMBER = _viewer(groups=(GROUP_CONFIG,))
CHATS_GROUP_MEMBER = _viewer(groups=(GROUP_CHATS,))
READ_ONLY_USER_GRANTEE = _viewer()
CONFIG_USER_GRANTEE = _viewer()
VIEWERS = (
    OWNER,
    ADMIN,
    READ_ONLY_GROUP_MEMBER,
    CONFIG_GROUP_MEMBER,
    CHATS_GROUP_MEMBER,
    READ_ONLY_USER_GRANTEE,
    CONFIG_USER_GRANTEE,
)


def _sharing(*, company_managed: bool) -> dict[str, Any]:
    """One entity reaching every viewer: three group grants, two direct grants."""
    return {
        "id": uuid.uuid4(),
        "user_id": OWNER.id,
        "is_company_managed": company_managed,
        # On a company-managed entity the grants are an access restriction:
        # they narrow who SEES it and never confer a manage right.
        "access_restricted": company_managed,
        "shared_group_ids": [GROUP_READ_ONLY, GROUP_CONFIG, GROUP_CHATS],
        "shared_group_permissions": {
            GROUP_READ_ONLY: "read_only",
            GROUP_CONFIG: "config",
            GROUP_CHATS: "config_with_chats",
        },
        "shared_user_ids": [
            str(READ_ONLY_USER_GRANTEE.id),
            str(CONFIG_USER_GRANTEE.id),
        ],
        "shared_user_permissions": {
            str(READ_ONLY_USER_GRANTEE.id): "read_only",
            str(CONFIG_USER_GRANTEE.id): "config_with_chats",
        },
        "tags": [],
        "created_at": NOW,
        "updated_at": NOW,
    }


# ---------------------------------------------------------------------------
# In-memory session
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def __iter__(self):
        return iter(list(self._rows))

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalar(self) -> Any:
        return self._rows[0] if self._rows else None

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def one(self) -> SimpleNamespace:
        # The aggregate rows (document counts, retry window, chat stats) of an
        # empty table.
        return SimpleNamespace(
            ready=0,
            pending=0,
            error=0,
            next_retry=None,
            retryable_count=0,
            total_conversations=0,
            total_messages=0,
            positive_feedback=0,
            negative_feedback=0,
        )


class _Session:
    """Serves the seeded rows of whatever model a statement selects.

    A by-id lookup is narrowed to that id; the caller's group memberships are
    answered from the viewer; anything else (owner names, counts, assets, a
    column of a seeded model) is an empty table.
    """

    def __init__(self, rows: list[Any], viewers: tuple[SimpleNamespace, ...]):
        self._rows = rows
        self._groups = {str(v.id): v.group_ids for v in viewers}

    async def execute(self, stmt: Any) -> _Result:
        compiled = stmt.compile(dialect=postgresql.dialect())
        params = compiled.params
        if "FROM group_members" in str(compiled):
            user_id = next(
                str(value) for key, value in params.items() if key.startswith("user_id")
            )
            return _Result(list(self._groups.get(user_id, [])))
        description = stmt.column_descriptions[0]
        entity = description.get("entity")
        if entity is AgentSkillPackageOrigin:
            # The package-origin links, read one column at a time.
            links = [row for row in self._rows if type(row) is entity]
            return _Result([getattr(row, description["name"]) for row in links])
        if entity is None or description.get("expr") is not entity:
            return _Result([])
        rows = [row for row in self._rows if type(row) is entity]
        ids = {str(value) for key, value in params.items() if key.split("_")[0] == "id"}
        if ids:
            rows = [row for row in rows if str(row.id) in ids]
        extra = len(stmt.column_descriptions) - 1
        if extra:
            # A computed column selected with the model (the flow list's owner
            # pause) reads as NULL, like any other column here.
            rows = [(row, *([None] * extra)) for row in rows]
        return _Result(rows)

    async def scalar(self, stmt: Any) -> Any:
        return (await self.execute(stmt)).scalar()

    def add(self, obj: Any) -> None:
        # What an INSERT would fill in: scalar column defaults, the key and
        # the timestamps.
        for column in obj.__table__.columns:
            default = column.default
            if (
                default is not None
                and default.is_scalar
                and getattr(obj, column.key, None) is None
            ):
                setattr(obj, column.key, default.arg)
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        for column in ("created_at", "updated_at"):
            if hasattr(obj, column) and getattr(obj, column) is None:
                setattr(obj, column, NOW)
        self._rows.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def refresh(self, _obj: Any, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def rollback(self) -> None:
        return None


# ---------------------------------------------------------------------------
# The surfaces
# ---------------------------------------------------------------------------


def _integration(**fields: Any) -> Integration:
    return Integration(
        provider="github",
        auth_type="api_key",
        name="GitHub",
        status="connected",
        is_builtin=False,
        oauth_share_acknowledged=False,
        **fields,
    )


def _mcp_server(**fields: Any) -> MCPServer:
    return MCPServer(
        name="Tools",
        transport="sse",
        url="https://mcp.example.com/sse",
        auth_mode="headers",
        oauth_status="disconnected",
        enabled=True,
        hide_from_chat=False,
        is_public_catalog=False,
        **fields,
    )


def _custom_tool(**fields: Any) -> CustomTool:
    return CustomTool(
        name="Tool",
        description="",
        js_function="return 1;",
        language="javascript",
        timeout=300,
        enabled=True,
        hide_from_chat=False,
        is_public_catalog=False,
        **fields,
    )


def _prompt(**fields: Any) -> Prompt:
    return Prompt(
        name="Prompt",
        description="",
        content="Hello",
        hide_from_chat=False,
        is_public_catalog=False,
        **fields,
    )


def _skill(**fields: Any) -> AgentSkill:
    return AgentSkill(
        name="Skill",
        content="---\nname: skill\ndescription: A skill.\n---\nDo the thing.\n",
        enabled=True,
        hide_from_chat=False,
        is_public_catalog=False,
        refresh_failure_count=0,
        auth_failure_count=0,
        **fields,
    )


def _template(**fields: Any) -> TemplateContainer:
    return TemplateContainer(
        name="Deck",
        kind="presentation",
        theme_config={},
        aspect_ratio="16:9",
        layouts=[],
        extra_metadata={},
        lookup_count=0,
        **fields,
    )


def _flow(**fields: Any) -> Flow:
    return Flow(
        name="Flow",
        description="",
        status="draft",
        version_number=1,
        graph={"nodes": [], "edges": []},
        **fields,
    )


def _knowledge_base(**fields: Any) -> KnowledgeBase:
    return KnowledgeBase(
        name="KB",
        embedding_model="text-embedding-3-large",
        is_system_managed=False,
        is_builtin=False,
        sync_sources_customized=False,
        is_read_only=False,
        document_count=0,
        chunk_count=0,
        query_count=0,
        **fields,
    )


def _agent(**fields: Any) -> Agent:
    return Agent(
        name="Agent",
        icon="Bot",
        output_format="markdown",
        enabled=True,
        is_builtin=False,
        is_public_catalog=False,
        skip_llm=False,
        memory_short_term_enabled=True,
        memory_medium_term_enabled=True,
        memory_persistent_enabled=True,
        memory_user_enabled=True,
        human_in_the_loop=False,
        disable_chat=False,
        web_enabled=False,
        **fields,
    )


@dataclass(frozen=True)
class _Surface:
    name: str
    module: ModuleType
    prefix: str
    make: Callable[..., Any]


SURFACES = (
    _Surface("integration", integrations, "/api/v1/integrations", _integration),
    _Surface("mcp_server", mcp_servers, "/api/v1/mcp-servers", _mcp_server),
    _Surface("custom_tool", custom_tools, "/api/v1/custom-tools", _custom_tool),
    _Surface("prompt", prompts, "/api/v1/prompts", _prompt),
    _Surface("skill", agent_skills, "/api/v1/skills", _skill),
    _Surface(
        "template_container",
        template_containers,
        "/api/v1/template-containers",
        _template,
    ),
    _Surface("flow", flows, "/api/v1/flows", _flow),
    _Surface(
        "knowledge_base", knowledge_bases, "/api/v1/knowledge-bases", _knowledge_base
    ),
    _Surface("agent", agents, "/api/v1/agents", _agent),
)


def _client(
    surface: _Surface,
    viewer: SimpleNamespace,
    entity: Any | None,
    *,
    extra: tuple[Any, ...] = (),
) -> TestClient:
    async def _get_db():
        rows = [entity] if entity is not None else []
        yield _Session([*rows, *extra], VIEWERS)

    app = FastAPI()
    app.include_router(surface.module.router, prefix=surface.prefix)
    app.dependency_overrides[deps.get_current_user] = lambda: viewer
    app.dependency_overrides[get_db] = _get_db
    for gate in (require_ai_enabled, require_agentic_ai, require_agentic_ai_licensed):
        app.dependency_overrides[gate] = lambda: None
    return TestClient(app)


def _flags(surface: _Surface, viewer: SimpleNamespace, entity: Any) -> tuple[Any, Any]:
    """``can_manage`` as the list and the detail route serve it to *viewer*."""
    with _client(surface, viewer, entity) as client:
        listing = client.get(surface.prefix)
        assert listing.status_code == 200, listing.text
        (row,) = [item for item in listing.json() if item["id"] == str(entity.id)]
        detail = client.get(f"{surface.prefix}/{entity.id}")
        assert detail.status_code == 200, detail.text
    return row["can_manage"], detail.json()["can_manage"]


_PERSONAL_EXPECTATIONS = [
    pytest.param(OWNER, True, id="owner"),
    pytest.param(ADMIN, True, id="admin"),
    pytest.param(READ_ONLY_GROUP_MEMBER, False, id="read_only_group"),
    pytest.param(CONFIG_GROUP_MEMBER, True, id="config_group"),
    pytest.param(CHATS_GROUP_MEMBER, True, id="config_with_chats_group"),
    pytest.param(READ_ONLY_USER_GRANTEE, False, id="read_only_user"),
    pytest.param(CONFIG_USER_GRANTEE, True, id="config_with_chats_user"),
]

_COMPANY_MANAGED_EXPECTATIONS = [
    pytest.param(ADMIN, True, id="admin"),
    pytest.param(OWNER, False, id="non_admin_owner"),
    pytest.param(CONFIG_GROUP_MEMBER, False, id="config_group"),
    pytest.param(CONFIG_USER_GRANTEE, False, id="config_with_chats_user"),
    pytest.param(READ_ONLY_GROUP_MEMBER, False, id="read_only_group"),
]


@pytest.mark.parametrize("viewer, expected", _PERSONAL_EXPECTATIONS)
@pytest.mark.parametrize("surface", SURFACES, ids=lambda s: s.name)
def test_a_shared_entity_tells_each_viewer_whether_they_may_edit_it(
    surface: _Surface, viewer: SimpleNamespace, expected: bool
):
    entity = surface.make(**_sharing(company_managed=False))

    assert _flags(surface, viewer, entity) == (expected, expected)


@pytest.mark.parametrize("viewer, expected", _COMPANY_MANAGED_EXPECTATIONS)
@pytest.mark.parametrize("surface", SURFACES, ids=lambda s: s.name)
def test_a_company_managed_entity_is_manageable_by_admins_only(
    surface: _Surface, viewer: SimpleNamespace, expected: bool
):
    entity = surface.make(**_sharing(company_managed=True))

    assert _flags(surface, viewer, entity) == (expected, expected)


# ---------------------------------------------------------------------------
# The flag and the write gate give the same answer
# ---------------------------------------------------------------------------


async def _no_audit(*_args: Any, **_kwargs: Any) -> None:
    return None


def _assert_update_route_agrees(
    monkeypatch, surface: _Surface, viewer: SimpleNamespace, entity: Any
) -> None:
    if hasattr(surface.module, "log_audit"):
        monkeypatch.setattr(surface.module, "log_audit", _no_audit)
    flag, _ = _flags(surface, viewer, entity)

    with _client(surface, viewer, entity) as client:
        response = client.put(f"{surface.prefix}/{entity.id}", json={"name": "Renamed"})

    if flag:
        assert response.status_code == 200, response.text
        assert response.json()["can_manage"] is True
    else:
        assert response.status_code == 403, response.text


@pytest.mark.parametrize("viewer, expected", _PERSONAL_EXPECTATIONS)
@pytest.mark.parametrize("surface", SURFACES, ids=lambda s: s.name)
def test_the_flag_is_the_answer_the_update_route_gives(
    monkeypatch, surface: _Surface, viewer: SimpleNamespace, expected: bool
):
    """What the UI is told it may edit is exactly what a save is allowed to do.

    A ``read_only`` grantee is refused (the backend stays the authority) and a ``config`` grantee's save lands - and the
    response it gets back still says it may manage the entity. Every
    shareable type, through its real update route.
    """
    entity = surface.make(**_sharing(company_managed=False))
    assert _flags(surface, viewer, entity)[0] is expected

    _assert_update_route_agrees(monkeypatch, surface, viewer, entity)


@pytest.mark.parametrize("viewer, expected", _COMPANY_MANAGED_EXPECTATIONS)
@pytest.mark.parametrize("surface", SURFACES, ids=lambda s: s.name)
def test_the_flag_is_the_answer_the_update_route_gives_on_company_managed(
    monkeypatch, surface: _Surface, viewer: SimpleNamespace, expected: bool
):
    entity = surface.make(**_sharing(company_managed=True))
    assert _flags(surface, viewer, entity)[0] is expected

    _assert_update_route_agrees(monkeypatch, surface, viewer, entity)


@pytest.mark.parametrize(
    "surface, body",
    [
        pytest.param(
            SURFACES[1],
            {"name": "New", "transport": "sse", "url": "https://mcp.example.com/sse"},
            id="mcp_server",
        ),
        pytest.param(
            SURFACES[2],
            {"name": "New", "js_function": "return 1;"},
            id="custom_tool",
        ),
    ],
)
def test_a_created_entity_says_its_creator_may_manage_it(
    monkeypatch, surface: _Surface, body: dict[str, Any]
):
    """The create response lands in the UI cache too: it must not read ``false``."""
    monkeypatch.setattr(surface.module, "log_audit", _no_audit)

    with _client(surface, OWNER, None) as client:
        response = client.post(surface.prefix, json=body)

    assert response.status_code == 201, response.text
    assert response.json()["can_manage"] is True


# ---------------------------------------------------------------------------
# Agent chats: one rule for the tab and for the route
# ---------------------------------------------------------------------------

_AGENTS = next(s for s in SURFACES if s.name == "agent")
_AGENT_CHATS = _Surface("agent_chats", agent_chats, "/api/v1/agents", _agent)


def _chats_flags(viewer: SimpleNamespace, agent: Agent) -> tuple[Any, Any, int]:
    """``can_view_chats`` on the list and detail, and the chats route's status."""
    with _client(_AGENTS, viewer, agent) as client:
        (row,) = [a for a in client.get(_AGENTS.prefix).json() if a["id"] == str(agent.id)]
        detail = client.get(f"{_AGENTS.prefix}/{agent.id}").json()
    with _client(_AGENT_CHATS, viewer, agent) as client:
        # An admitted reader goes on to the conversation lookup - a 404 here,
        # the session holds none; a refused one stops at the 403.
        chat = client.get(f"{_AGENT_CHATS.prefix}/{agent.id}/chats/{uuid.uuid4()}")
    return row["can_view_chats"], detail["can_view_chats"], chat.status_code


@pytest.mark.parametrize(
    "viewer, expected",
    [
        pytest.param(OWNER, True, id="owner"),
        pytest.param(ADMIN, True, id="admin"),
        pytest.param(CHATS_GROUP_MEMBER, True, id="config_with_chats_group"),
        # A direct grant opens the chats like the same tier through a group.
        pytest.param(CONFIG_USER_GRANTEE, True, id="config_with_chats_user"),
        pytest.param(CONFIG_GROUP_MEMBER, False, id="config_group"),
        pytest.param(READ_ONLY_GROUP_MEMBER, False, id="read_only_group"),
        pytest.param(READ_ONLY_USER_GRANTEE, False, id="read_only_user"),
    ],
)
def test_an_agent_says_who_may_read_its_chats_and_the_chats_route_agrees(
    viewer: SimpleNamespace, expected: bool
):
    agent = _agent(**_sharing(company_managed=False))

    assert _chats_flags(viewer, agent) == (expected, expected, 404 if expected else 403)


@pytest.mark.parametrize(
    "viewer, expected",
    [
        pytest.param(ADMIN, True, id="admin"),
        pytest.param(OWNER, True, id="owner"),
        pytest.param(CHATS_GROUP_MEMBER, False, id="config_with_chats_group"),
        pytest.param(CONFIG_USER_GRANTEE, False, id="config_with_chats_user"),
    ],
)
def test_a_company_managed_agent_keeps_its_chats_to_admins_and_its_owner(
    viewer: SimpleNamespace, expected: bool
):
    agent = _agent(**_sharing(company_managed=True))

    assert _chats_flags(viewer, agent) == (expected, expected, 404 if expected else 403)


@pytest.mark.parametrize(
    "company_managed, viewer, expected",
    [
        pytest.param(False, CHATS_GROUP_MEMBER, True, id="config_with_chats_group"),
        pytest.param(False, CONFIG_USER_GRANTEE, True, id="config_with_chats_user"),
        pytest.param(False, CONFIG_GROUP_MEMBER, False, id="config_group"),
        pytest.param(False, READ_ONLY_GROUP_MEMBER, False, id="read_only_group"),
        pytest.param(True, OWNER, True, id="company_managed-owner"),
        pytest.param(
            True, CHATS_GROUP_MEMBER, False, id="company_managed-config_with_chats_group"
        ),
    ],
)
def test_the_conversation_list_follows_the_same_rule(
    company_managed: bool, viewer: SimpleNamespace, expected: bool
):
    """The list route shows the conversations to exactly the same readers.

    Anyone else who can see the agent gets the stats and an empty list.
    """
    agent = _agent(**_sharing(company_managed=company_managed))
    conversation = Conversation(
        id=uuid.uuid4(),
        agent_id=agent.id,
        channel="web",
        title="Hello",
        is_archived=False,
        created_at=NOW,
        updated_at=NOW,
    )

    with _client(_AGENT_CHATS, viewer, agent, extra=(conversation,)) as client:
        chats = client.get(f"{_AGENT_CHATS.prefix}/{agent.id}/chats")

    assert chats.status_code == 200, chats.text
    assert [item["id"] for item in chats.json()["items"]] == (
        [str(conversation.id)] if expected else []
    )


# ---------------------------------------------------------------------------
# Integrations: who may authorize, and which providers are registration-only
# ---------------------------------------------------------------------------

_INTEGRATIONS = next(s for s in SURFACES if s.name == "integration")


def _authorize_outcome(
    monkeypatch, viewer: SimpleNamespace, integration: Integration
) -> tuple[Any, Any, int]:
    """``can_authorize`` on the list and detail, and ``start_oauth``'s status."""
    import app.integrations.oauth as oauth_mod

    async def _authorize_url(_integration: Any, _user_id: str, **_kwargs: Any) -> str:
        return "https://provider.example.com/authorize"

    monkeypatch.setattr(oauth_mod, "build_authorize_url_for_integration", _authorize_url)
    with _client(_INTEGRATIONS, viewer, integration) as client:
        (row,) = [
            item
            for item in client.get(_INTEGRATIONS.prefix).json()
            if item["id"] == str(integration.id)
        ]
        detail = client.get(f"{_INTEGRATIONS.prefix}/{integration.id}").json()
        started = client.get(f"{_INTEGRATIONS.prefix}/{integration.id}/authorize")
    return row["can_authorize"], detail["can_authorize"], started.status_code


@pytest.mark.parametrize(
    "company_managed, viewer, expected",
    [
        # Personal: the OAuth callback stores the token on the owner's row only.
        pytest.param(False, OWNER, True, id="owner"),
        pytest.param(False, ADMIN, False, id="admin_on_personal"),
        pytest.param(False, CONFIG_GROUP_MEMBER, False, id="config_group"),
        pytest.param(False, CONFIG_USER_GRANTEE, False, id="config_with_chats_user"),
        pytest.param(False, READ_ONLY_GROUP_MEMBER, False, id="read_only_group"),
        # Company-managed: admins only (start_oauth), whoever owns the row.
        pytest.param(True, ADMIN, True, id="company_managed-admin"),
        pytest.param(True, OWNER, False, id="company_managed-non_admin_owner"),
        pytest.param(True, CONFIG_GROUP_MEMBER, False, id="company_managed-config_group"),
    ],
)
def test_can_authorize_is_the_answer_the_oauth_flow_gives(
    monkeypatch, company_managed: bool, viewer: SimpleNamespace, expected: bool
):
    """An Authorize button is only offered where the authorization can land.

    ``start_oauth`` now refuses up front whoever the callback would refuse at
    the end, after the provider's consent screen, with a 404.
    """
    integration = _integration(
        **_sharing(company_managed=company_managed), client_id_enc="client"
    )

    assert _authorize_outcome(monkeypatch, viewer, integration) == (
        expected,
        expected,
        200 if expected else 403,
    )


def test_starting_oauth_requests_the_catalog_scopes_without_storing_them(monkeypatch):
    """Only the callback records what was authorized.

    Storing the catalog scopes at the start made an abandoned, denied or
    failed consent look like a completed re-authorization: the scope drift
    the page reports went away while the token still lacked the new scopes.
    """
    import app.integrations.oauth as oauth_mod

    requested: list[str | None] = []

    async def _authorize_url(
        _integration: Any,
        _user_id: str,
        *,
        scopes: str | None = None,
        implicit: bool = False,
        own_account: bool = False,
    ) -> str:
        requested.append(scopes)
        return "https://provider.example.com/authorize"

    monkeypatch.setattr(oauth_mod, "build_authorize_url_for_integration", _authorize_url)
    integration = _integration(**_sharing(company_managed=False), client_id_enc="client")
    integration.provider = "google"
    integration.auth_type = "oauth"
    integration.scopes = "openid email"

    with _client(_INTEGRATIONS, OWNER, integration) as client:
        started = client.get(f"{_INTEGRATIONS.prefix}/{integration.id}/authorize")

    assert started.status_code == 200, started.text
    assert requested == [integration_catalog.PROVIDER_CATALOG["google"]["scopes"]]
    assert integration.scopes == "openid email"


@pytest.mark.parametrize("provider", sorted(integration_catalog.REGISTRATION_ONLY_PROVIDERS))
def test_every_registration_only_provider_is_served_as_such_and_never_created(
    monkeypatch, provider: str
):
    """One list: the flag the page reads and the create refusal cannot drift."""
    monkeypatch.setattr(integration_catalog.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(integrations, "log_audit", _no_audit)

    async def _platform_settings(_db: Any) -> SimpleNamespace:
        return SimpleNamespace(disabled_integration_providers=[])

    import app.api.v1.platform_settings_helpers as platform_settings_helpers

    monkeypatch.setattr(
        platform_settings_helpers, "get_platform_settings", _platform_settings
    )
    integration = _integration(**_sharing(company_managed=False))
    integration.provider = provider

    with _client(_INTEGRATIONS, OWNER, integration) as client:
        detail = client.get(f"{_INTEGRATIONS.prefix}/{integration.id}").json()
        created = client.post(
            _INTEGRATIONS.prefix,
            json={"provider": provider, "name": "Manual", "auth_type": "api_key"},
        )

    assert detail["is_registration_only"] is True
    assert created.status_code == 400, created.text
    assert "platform registration" in created.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(integration_catalog.REGISTRATION_ONLY_PROVIDERS))
async def test_the_agent_builder_refuses_every_registration_only_provider_too(
    monkeypatch, provider: str
):
    """The agent builder creates integrations without the REST route, so it
    must read the same list, or an agent could create what the page locks."""
    from app.services.builtin_tools import build_agent_builder_tool
    from app.tools.base import ToolCall

    monkeypatch.setattr(integration_catalog.settings, "platform_mode", "xtm_one")

    def _no_session() -> None:
        raise AssertionError("refused before any database session")

    monkeypatch.setattr("app.core.database.async_session", _no_session)
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await executor.execute_tool(
        ToolCall(
            id="call-1",
            name="create_integration",
            arguments={"provider": provider, "name": "Manual"},
        )
    )

    assert "platform registration" in result.content, result.content


def test_opencrq_is_registration_only_like_opencti_and_openaev():
    assert {"opencti", "openaev", "opencrq"} <= integration_catalog.REGISTRATION_ONLY_PROVIDERS


@pytest.mark.parametrize("mode, expected", [("xtm_one", True), ("copilot", False)])
def test_registration_only_follows_the_platform_mode(
    monkeypatch, mode: str, expected: bool
):
    monkeypatch.setattr(integration_catalog.settings, "platform_mode", mode)
    integration = _integration(**_sharing(company_managed=False))
    integration.provider = "opencrq"
    plain = _integration(**_sharing(company_managed=False))

    with _client(_INTEGRATIONS, OWNER, integration) as client:
        flagged = client.get(f"{_INTEGRATIONS.prefix}/{integration.id}").json()
    with _client(_INTEGRATIONS, OWNER, plain) as client:
        github = client.get(f"{_INTEGRATIONS.prefix}/{plain.id}").json()

    assert flagged["is_registration_only"] is expected
    assert github["is_registration_only"] is False


def _registered_openaev(monkeypatch) -> Integration:
    import json

    from app.core.security import encrypt_value

    monkeypatch.setattr(integration_catalog.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(integrations, "log_audit", _no_audit)
    integration = _integration(**_sharing(company_managed=True))
    integration.provider = "openaev"
    integration.auth_type = "platform_managed"
    integration.extra_config_enc = encrypt_value(json.dumps({"base_url": "https://aev.test"}))
    return integration


def test_an_admin_sets_the_tenant_of_a_registered_integration(monkeypatch):
    """The OpenAEV tenant is the deployment's choice: editable, the rest kept."""
    from app.core.security import decrypt_json

    integration = _registered_openaev(monkeypatch)
    with _client(_INTEGRATIONS, ADMIN, integration) as client:
        saved = client.put(
            f"{_INTEGRATIONS.prefix}/{integration.id}",
            json={"extra_config": {"tenant_id": " t-b "}},
        )
    assert saved.status_code == 200, saved.text
    assert decrypt_json(integration.extra_config_enc) == {
        "base_url": "https://aev.test",
        "tenant_id": "t-b",
    }

    with _client(_INTEGRATIONS, ADMIN, integration) as client:
        cleared = client.put(
            f"{_INTEGRATIONS.prefix}/{integration.id}",
            json={"extra_config": {"base_url": "https://aev.test", "tenant_id": ""}},
        )
    assert cleared.status_code == 200, cleared.text
    assert decrypt_json(integration.extra_config_enc) == {"base_url": "https://aev.test"}


def test_the_registration_keys_of_a_registered_integration_stay_locked(monkeypatch):
    from app.core.security import decrypt_json

    integration = _registered_openaev(monkeypatch)
    with _client(_INTEGRATIONS, ADMIN, integration) as client:
        refused = client.put(
            f"{_INTEGRATIONS.prefix}/{integration.id}",
            json={"extra_config": {"base_url": "https://attacker.test", "tenant_id": "t-b"}},
        )
    assert refused.status_code == 400, refused.text
    assert "platform registration" in refused.json()["detail"]
    assert decrypt_json(integration.extra_config_enc) == {"base_url": "https://aev.test"}


class _BuilderSession:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> _BuilderSession:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


async def _builder_update(monkeypatch, integration: Integration, extra: dict[str, Any]) -> tuple[str, int]:
    """The agent builder's ``update_integration`` as an admin: its answer and its commits."""
    import json

    import app.core.database as database
    import app.services.builtin_tools as builtin_tools_module
    from app.tools.base import ToolCall

    async def _manageable(*_args: Any, **_kwargs: Any) -> tuple[Integration, None]:
        return integration, None

    async def _caller(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return ADMIN

    session = _BuilderSession()
    monkeypatch.setattr(builtin_tools_module, "_load_manageable_entity_for_builder", _manageable)
    monkeypatch.setattr(builtin_tools_module, "_load_caller_user_for_builder", _caller)
    monkeypatch.setattr(database, "async_session", lambda: session)
    _, executor = builtin_tools_module.build_agent_builder_tool(SimpleNamespace(), str(ADMIN.id))
    result = await executor.execute_tool(
        ToolCall(
            id="call-1",
            name="update_integration",
            arguments={"integration_id": str(integration.id), "extra_config": json.dumps(extra)},
        )
    )
    assert not result.is_error, result.content
    return result.content, session.commits


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(integration_catalog.REGISTRATION_ONLY_PROVIDERS))
async def test_the_agent_builder_keeps_the_registration_keys_locked_too(monkeypatch, provider: str):
    """The update twin of the create refusal: an admin's agent cannot
    rewrite from chat the base URL the page and the API lock."""
    from app.core.security import decrypt_json

    integration = _registered_openaev(monkeypatch)
    integration.provider = provider
    content, commits = await _builder_update(
        monkeypatch, integration, {"base_url": "https://attacker.test"}
    )

    assert "'base_url' is set by the platform registration" in content, content
    assert commits == 0
    assert decrypt_json(integration.extra_config_enc) == {"base_url": "https://aev.test"}


@pytest.mark.asyncio
async def test_the_agent_builder_sets_the_tenant_and_keeps_the_registration(monkeypatch):
    """Only the catalog's keys change, merged into the stored config, not replacing it."""
    from app.core.security import decrypt_json

    integration = _registered_openaev(monkeypatch)
    content, commits = await _builder_update(monkeypatch, integration, {"tenant_id": " t-b "})

    assert commits == 1, content
    assert decrypt_json(integration.extra_config_enc) == {
        "base_url": "https://aev.test",
        "tenant_id": "t-b",
    }


@pytest.mark.asyncio
async def test_the_agent_builder_refuses_a_non_text_tenant(monkeypatch):
    from app.core.security import decrypt_json

    integration = _registered_openaev(monkeypatch)
    content, commits = await _builder_update(monkeypatch, integration, {"tenant_id": 1})

    assert "'tenant_id' must be a string." in content, content
    assert commits == 0
    assert decrypt_json(integration.extra_config_enc) == {"base_url": "https://aev.test"}


@pytest.mark.asyncio
async def test_a_registration_edit_is_diffed_as_the_update_stores_it(monkeypatch):
    """The re-point check reads the config the update stores: a form that
    sends the unchanged tenant alone changes nothing, a new tenant does."""
    import json

    from app.api.v1.schemas.integration import UpdateIntegrationRequest
    from app.core.security import encrypt_value

    integration = _registered_openaev(monkeypatch)
    integration.extra_config_enc = encrypt_value(
        json.dumps({"base_url": "https://aev.test", "tenant_id": "t-a"})
    )

    unchanged = UpdateIntegrationRequest(extra_config={"tenant_id": "t-a"})
    changed = UpdateIntegrationRequest(extra_config={"tenant_id": "t-b"})
    assert await integrations._update_repoints_integration(None, integration, unchanged) is False
    assert await integrations._update_repoints_integration(None, integration, changed) is True


def _put_extra(viewer, integration, extra):
    with _client(_INTEGRATIONS, viewer, integration) as client:
        return client.put(f"{_INTEGRATIONS.prefix}/{integration.id}", json={"extra_config": extra})


def test_a_non_admin_cannot_set_the_tenant_of_a_registered_integration(monkeypatch):
    """A registered integration is company-managed: only admins edit it, tenant included."""
    from app.core.security import decrypt_json

    integration = _registered_openaev(monkeypatch)
    refused = _put_extra(OWNER, integration, {"tenant_id": "t-b"})
    assert refused.status_code == 403, refused.text
    assert decrypt_json(integration.extra_config_enc) == {"base_url": "https://aev.test"}


@pytest.mark.parametrize("value", [1, ["t-b"], {"a": 1}, True], ids=["number", "list", "object", "bool"])
def test_an_editable_field_must_be_text(monkeypatch, value):
    from app.core.security import decrypt_json

    integration = _registered_openaev(monkeypatch)
    refused = _put_extra(ADMIN, integration, {"tenant_id": value})
    assert refused.status_code == 400, refused.text
    assert "must be a string" in refused.json()["detail"]
    assert decrypt_json(integration.extra_config_enc) == {"base_url": "https://aev.test"}


def test_an_editable_field_has_a_length_limit(monkeypatch):
    integration = _registered_openaev(monkeypatch)
    refused = _put_extra(ADMIN, integration, {"tenant_id": "x" * 201})
    assert refused.status_code == 400, refused.text
    assert "too long" in refused.json()["detail"]


def test_an_unreadable_stored_config_is_not_overwritten(monkeypatch):
    """Saving the tenant must not replace a config nobody can read with the tenant alone."""
    integration = _registered_openaev(monkeypatch)
    integration.extra_config_enc = "not-a-fernet-token"
    refused = _put_extra(ADMIN, integration, {"tenant_id": "t-b"})
    assert refused.status_code == 409, refused.text
    assert integration.extra_config_enc == "not-a-fernet-token"


def test_the_catalog_serves_the_fields_editable_after_registration():
    assert integration_catalog.registration_editable_extra_fields("openaev") == {
        "tenant_id",
        "webhook_secret",
    }
    assert integration_catalog.registration_editable_extra_fields("opencti") == {
        "webhook_secret",
        "live_writes",
    }
    assert integration_catalog.registration_editable_extra_fields("opencrq") == frozenset()


def test_an_admin_sets_the_webhook_secret_of_a_registered_integration(monkeypatch):
    """Without it the OpenAEV events of a CTEM fleet can never be provisioned."""
    from app.core.security import decrypt_json

    integration = _registered_openaev(monkeypatch)
    saved = _put_extra(ADMIN, integration, {"webhook_secret": " s3cret "})
    assert saved.status_code == 200, saved.text
    assert decrypt_json(integration.extra_config_enc) == {
        "base_url": "https://aev.test",
        "webhook_secret": "s3cret",
    }

    # The response mask sent back keeps the stored secret (a tenant-only edit).
    kept = _put_extra(
        ADMIN, integration, {"tenant_id": "t-b", "webhook_secret": integrations.EXTRA_CONFIG_MASK}
    )
    assert kept.status_code == 200, kept.text
    assert decrypt_json(integration.extra_config_enc) == {
        "base_url": "https://aev.test",
        "tenant_id": "t-b",
        "webhook_secret": "s3cret",
    }

    # An empty value removes it.
    cleared = _put_extra(ADMIN, integration, {"webhook_secret": ""})
    assert cleared.status_code == 200, cleared.text
    assert "webhook_secret" not in decrypt_json(integration.extra_config_enc)


def _registered_opencti(monkeypatch) -> Integration:
    import json

    from app.core.security import encrypt_value

    integration = _registered_openaev(monkeypatch)
    integration.provider = "opencti"
    integration.extra_config_enc = encrypt_value(json.dumps({"base_url": "https://octi.test"}))
    return integration


@pytest.mark.asyncio
async def test_an_admin_turns_live_writes_on_for_a_registered_opencti(monkeypatch):
    """Live writes is the integration's setting: settable where the platform registered it."""
    from app.core.security import decrypt_json
    from app.integrations.providers.opencti import build_tools
    from app.services.integration_tools import _get_extra_config_value
    from app.tools.base import CallableToolExecutor

    integration = _registered_opencti(monkeypatch)
    saved = _put_extra(ADMIN, integration, {"live_writes": "on"})
    assert saved.status_code == 200, saved.text
    assert decrypt_json(integration.extra_config_enc) == {
        "base_url": "https://octi.test",
        "live_writes": "on",
    }

    # The value a run reads is the one saved, and it registers the live-write tools.
    class _Db:
        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalar_one_or_none=lambda: integration)

    read = await _get_extra_config_value(_Db(), str(integration.id), "live_writes")
    assert read == "on"
    names = {d.name for d in build_tools("https://octi.test||t", CallableToolExecutor(), live_writes=read == "on")}
    assert {"apply_opencti_draft", "create_opencti_label"} <= names

    # Off keeps today's behaviour: no live-write tool.
    off = _put_extra(ADMIN, integration, {"live_writes": "off"})
    assert off.status_code == 200, off.text
    read = await _get_extra_config_value(_Db(), str(integration.id), "live_writes")
    assert read == "off"
    names = {d.name for d in build_tools("https://octi.test||t", CallableToolExecutor(), live_writes=read == "on")}
    assert not {"apply_opencti_draft", "create_opencti_label"} & names


def test_live_writes_takes_only_a_catalog_choice(monkeypatch):
    from app.core.security import decrypt_json

    integration = _registered_opencti(monkeypatch)
    refused = _put_extra(ADMIN, integration, {"live_writes": "yes"})
    assert refused.status_code == 400, refused.text
    assert "must be one of" in refused.json()["detail"]
    assert decrypt_json(integration.extra_config_enc) == {"base_url": "https://octi.test"}


def test_a_non_admin_cannot_turn_live_writes_on(monkeypatch):
    from app.core.security import decrypt_json

    integration = _registered_opencti(monkeypatch)
    refused = _put_extra(OWNER, integration, {"live_writes": "on"})
    assert refused.status_code == 403, refused.text
    assert decrypt_json(integration.extra_config_enc) == {"base_url": "https://octi.test"}


def test_the_integration_page_reads_the_served_registration_only_flag():
    """The page must not keep its own provider list next to the backend's."""
    from pathlib import Path

    page = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "pages"
        / "IntegrationDetail.tsx"
    ).read_text(encoding="utf-8")
    assert "is_registration_only" in page
    for provider in integration_catalog.REGISTRATION_ONLY_PROVIDERS:
        assert f'"{provider}"' not in page, provider


# ---------------------------------------------------------------------------
# Package-installed custom tools and knowledge bases
# ---------------------------------------------------------------------------

_CUSTOM_TOOLS = next(s for s in SURFACES if s.name == "custom_tool")
_KNOWLEDGE_BASES = next(s for s in SURFACES if s.name == "knowledge_base")


def _origin_link(surface: _Surface, entity: Any) -> AgentSkillPackageOrigin:
    """The link a skill package import writes for the resource it materialized."""
    if surface is _CUSTOM_TOOLS:
        return AgentSkillPackageOrigin(
            id=uuid.uuid4(),
            skill_id=uuid.uuid4(),
            resource_kind="script",
            resource_path="scripts/tool.js",
            custom_tool_id=entity.id,
        )
    return AgentSkillPackageOrigin(
        id=uuid.uuid4(),
        skill_id=uuid.uuid4(),
        resource_kind="reference",
        resource_path="references/doc.md",
        knowledge_base_id=entity.id,
        knowledge_document_id=uuid.uuid4(),
    )


def _origin_flags(
    surface: _Surface, viewer: SimpleNamespace, entity: Any, links: tuple[Any, ...]
) -> tuple[Any, Any]:
    with _client(surface, viewer, entity, extra=links) as client:
        listing = client.get(surface.prefix)
        assert listing.status_code == 200, listing.text
        (row,) = [item for item in listing.json() if item["id"] == str(entity.id)]
        detail = client.get(f"{surface.prefix}/{entity.id}")
        assert detail.status_code == 200, detail.text
    return row["is_package_origin"], detail.json()["is_package_origin"]


_ORIGIN_SURFACES = [
    pytest.param(_CUSTOM_TOOLS, id="custom_tool"),
    pytest.param(_KNOWLEDGE_BASES, id="knowledge_base"),
]


@pytest.mark.parametrize("from_package", [True, False], ids=["package", "plain"])
@pytest.mark.parametrize("surface", _ORIGIN_SURFACES)
def test_list_and_detail_say_whether_a_skill_package_installed_it(
    surface: _Surface, from_package: bool
):
    entity = surface.make(**_sharing(company_managed=False))
    links = (_origin_link(surface, entity),) if from_package else ()

    assert _origin_flags(surface, OWNER, entity, links) == (from_package, from_package)


@pytest.mark.parametrize("viewer", [OWNER, ADMIN], ids=["owner", "admin"])
@pytest.mark.parametrize("surface", _ORIGIN_SURFACES)
def test_a_package_installed_entity_is_refused_by_its_write_routes(
    monkeypatch, surface: _Surface, viewer: SimpleNamespace
):
    """The flag is the write routes' own answer: update and delete refuse it.

    Owners and admins alike - the lock is the package's, not the viewer's, so
    the manage right they hold (``can_manage`` stays true) does not open it.
    """
    monkeypatch.setattr(surface.module, "log_audit", _no_audit)
    entity = surface.make(**_sharing(company_managed=False))
    links = (_origin_link(surface, entity),)
    assert _origin_flags(surface, viewer, entity, links) == (True, True)

    with _client(surface, viewer, entity, extra=links) as client:
        detail = client.get(f"{surface.prefix}/{entity.id}").json()
        update = client.put(f"{surface.prefix}/{entity.id}", json={"name": "Renamed"})
        delete = client.delete(f"{surface.prefix}/{entity.id}")

    assert detail["can_manage"] is True
    assert update.status_code == 403, update.text
    assert "skill packages" in update.json()["detail"]
    assert delete.status_code == 403, delete.text
    assert "skill packages" in delete.json()["detail"]
