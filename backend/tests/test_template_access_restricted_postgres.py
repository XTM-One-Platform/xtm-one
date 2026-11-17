"""Per-user template resolution reaches a restricted integration only for its grantees.

``template_candidate_filter`` combines the three-tier visibility filter (JSONB
user / group grants; the admin role only for a company-managed automation)
with the per-user eligibility rule, so it is run here by PostgreSQL: ``users``, ``groups``, ``group_members`` and
``integrations`` are created in a throwaway schema on one connection and
dropped afterwards. The pipeline fallback and the evaluation path are run end
to end against the same rows; agent chat and assignment runs build their
candidates through the same filter (``test_template_access_restricted``).
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.v1.helpers import company_managed_run_scoped, mark_company_managed_run
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.integration_user_credential import IntegrationUserCredential
from app.models.relay import RelayGroup
from app.models.user import User
from app.services import agent_chat, evaluation_engine, integration_tools

pytestmark = pytest.mark.integration

_OUTSIDER = uuid.uuid4()
_MEMBER = uuid.uuid4()
_GRANTEE = uuid.uuid4()
_ADMIN = uuid.uuid4()
_CREATOR = uuid.uuid4()
_GROUP = uuid.uuid4()

_COMPANY = {"auth_type": "api_key", "is_company_managed": True, "user_id": _CREATOR}
_RESTRICTED = {**_COMPANY, "access_restricted": True}

#: integration name -> columns (provider "slack", connected)
_ROWS: dict[str, dict] = {
    "restricted_to_group": {**_RESTRICTED, "shared_group_ids": [str(_GROUP)]},
    "restricted_to_grantee": {**_RESTRICTED, "shared_user_ids": [str(_GRANTEE)]},
    "restricted_group_unshared_oauth": {
        **_RESTRICTED, "auth_type": "oauth", "shared_group_ids": [str(_GROUP)],
    },
    "unrestricted_company": dict(_COMPANY),
    "outsider_own": {"auth_type": "api_key", "user_id": _OUTSIDER},
    "creator_personal": {"auth_type": "api_key", "user_id": _CREATOR},
    "platform_managed_personal": {"auth_type": "platform_managed", "user_id": _CREATOR},
}

_EVERYONE = {"unrestricted_company", "platform_managed_personal"}

#: acting user -> the integrations per-user resolution may pick for them. An
#: admin is held to the rule like anyone; only a company-managed
#: automation acting as an admin keeps every company row (``*_automation``).
_EXPECTED: dict[str | None, set[str]] = {
    "outsider": _EVERYONE | {"outsider_own"},
    "member": _EVERYONE | {"restricted_to_group"},
    "grantee": _EVERYONE | {"restricted_to_grantee"},
    "admin": set(_EVERYONE),
    "admin_automation": _EVERYONE | {"restricted_to_group", "restricted_to_grantee"},
    "member_automation": _EVERYONE | {"restricted_to_group"},
    None: set(_EVERYONE),
}

_ACTORS = {"outsider": _OUTSIDER, "member": _MEMBER, "grantee": _GRANTEE, "admin": _ADMIN}


def _actor(actor: str | None) -> tuple[uuid.UUID | None, bool]:
    """(acting user, company automation) for an ``_EXPECTED`` / case key."""
    if actor is None:
        return None, False
    return _ACTORS[actor.removesuffix("_automation")], actor.endswith("_automation")


def _database_url() -> str:
    # CRLF-safe: a .env dragged through Windows leaves \r on the value.
    return os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot"
    ).strip()


@pytest.fixture
async def session():
    schema = f"template_restricted_{uuid.uuid4().hex[:10]}"
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
            # The template candidates read the people's own accounts.
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
            for user_id in (_OUTSIDER, _MEMBER, _GRANTEE, _ADMIN, _CREATOR):
                db.add(
                    User(
                        id=user_id, email=f"{user_id}@example.com", full_name="u",
                        hashed_password="x", role="admin" if user_id == _ADMIN else "user",
                    )
                )
            db.add(Group(id=_GROUP, name=f"restricted-{_GROUP}"))
            await db.flush()
            db.add(GroupMember(group_id=_GROUP, user_id=_MEMBER))
            for name, columns in _ROWS.items():
                db.add(
                    Integration(
                        id=uuid.uuid4(), provider="slack", name=name, status="connected",
                        access_token_enc="enc", **columns,
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


@pytest.mark.parametrize("actor", list(_EXPECTED))
async def test_template_candidates_follow_the_restricted_integration_s_grants(session, actor):
    acting, company_automation = _actor(actor)
    rows = await session.execute(
        select(Integration.name).where(
            await integration_tools.template_candidate_filter(
                session, acting, company_automation=company_automation
            )
        )
    )
    assert set(rows.scalars().all()) == _EXPECTED[actor]


async def test_an_ownerless_company_managed_run_keeps_every_company_integration(session):
    """The run of a company-managed assignment with no user keeps main's tier in
    every read, its template bindings included; an OAuth row still
    needs its token shared."""

    @company_managed_run_scoped
    async def _run() -> set[str]:
        assert mark_company_managed_run(
            SimpleNamespace(is_company_managed=True), SimpleNamespace(user_id=None)
        )
        rows = await session.execute(
            select(Integration.name).where(
                await integration_tools.template_candidate_filter(
                    session, None, company_automation=True
                )
            )
        )
        return set(rows.scalars().all())

    assert await _run() == _EVERYONE | {"restricted_to_group", "restricted_to_grantee"}


async def _add_jira(session, **columns) -> Integration:
    row = Integration(
        id=uuid.uuid4(), provider="jira", name="jira", status="connected",
        access_token_enc="enc", **columns,
    )
    session.add(row)
    await session.flush()
    return row


def _capture_build(monkeypatch) -> dict:
    captured: dict = {}

    async def _build(_db, bindings, *, context=None, channel_bot_mode=False):
        del channel_bot_mode
        captured["bindings"] = {k: v for k, v in dict(bindings).items()}
        captured["context"] = context or {}
        return [SimpleNamespace(name="jira_search", source="integration:jira")], "executor"

    monkeypatch.setattr(integration_tools, "build_integration_tools", _build)
    return captured


#: (jira row, acting user, resolves) - one row per case so the pick is the row
_PATH_CASES = [
    pytest.param({**_RESTRICTED, "shared_group_ids": [str(_GROUP)]}, "outsider", False,
                 id="restricted_no_grant"),
    pytest.param({**_RESTRICTED, "shared_group_ids": [str(_GROUP)]}, "member", True,
                 id="restricted_group_grant"),
    pytest.param({**_RESTRICTED, "shared_user_ids": [str(_GRANTEE)]}, "grantee", True,
                 id="restricted_user_grant"),
    pytest.param(dict(_RESTRICTED), "admin", False, id="restricted_admin"),
    pytest.param(dict(_RESTRICTED), "admin_automation", True,
                 id="restricted_admin_company_automation"),
    pytest.param({**_RESTRICTED, "shared_group_ids": [str(_GROUP)]}, "outsider_automation",
                 False, id="restricted_no_grant_company_automation"),
    pytest.param(dict(_COMPANY), "outsider", True, id="unrestricted_company"),
    pytest.param({"auth_type": "api_key", "user_id": _OUTSIDER}, "outsider", True,
                 id="own_integration"),
]


@pytest.mark.parametrize(("columns", "actor", "resolves"), _PATH_CASES)
async def test_the_pipeline_fallback_honours_the_restriction(
    session, monkeypatch, columns, actor, resolves
):
    row = await _add_jira(session, **columns)
    captured = _capture_build(monkeypatch)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Pipeline agent", user_id=_CREATOR)
    ref = {"source": "integration", "provider": "jira", "tool_name": "jira_search"}
    acting, company_automation = _actor(actor)

    loaded = await agent_chat._load_pipeline_tool_standalone(
        session, ref, str(acting), agent, company_automation=company_automation
    )

    assert (loaded is not None) is resolves
    if resolves:
        assert captured["bindings"] == {"jira": [str(row.id)]}


async def test_the_acting_user_s_own_integration_still_wins(session, monkeypatch):
    await _add_jira(session, **{**_RESTRICTED, "shared_group_ids": [str(_GROUP)]})
    own = await _add_jira(session, auth_type="api_key", user_id=_MEMBER)
    captured = _capture_build(monkeypatch)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Pipeline agent", user_id=_CREATOR)
    ref = {"source": "integration", "provider": "jira", "tool_name": "jira_search"}

    loaded = await agent_chat._load_pipeline_tool_standalone(session, ref, str(_MEMBER), agent)

    assert loaded is not None
    assert captured["bindings"] == {"jira": [str(own.id)]}


@pytest.mark.parametrize(("columns", "actor", "resolves"), _PATH_CASES)
async def test_the_evaluation_path_honours_the_restriction(
    session, monkeypatch, columns, actor, resolves
):
    row = await _add_jira(session, **columns)
    captured = _capture_build(monkeypatch)
    acting, company_automation = _actor(actor)
    # An evaluation is non-interactive: it is a company automation exactly
    # when the evaluated agent is company-managed.
    agent = SimpleNamespace(
        id=uuid.uuid4(), name="Eval agent", user_id=acting, integration_bindings={},
        integration_template_bindings=["jira"], integration_allowed_tools={},
        is_company_managed=company_automation,
    )

    bundle = await evaluation_engine._build_evaluation_integration_tools(session, agent)

    assert (bundle is not None) is resolves
    if resolves:
        assert captured["bindings"] == {"jira": str(row.id)}
