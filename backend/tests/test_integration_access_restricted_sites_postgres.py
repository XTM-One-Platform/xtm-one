"""Every place that picks a company-managed integration for a user honours ``access_restricted``.

A company-managed integration narrowed with ``access_restricted`` is visible to
its user / group grantees and admins only. ``integration_visible_filter``
composes ``user_visible_filter`` for a user known by id, and each fixed site is
run here by PostgreSQL against the same rows: the KB sync credential fallback
(company-managed, Drata), KB sync source create / update, the CTEM dashboard
and what the agent builder lists and names to a caller with no account. For
every site, a user outside the restriction is refused while grantees, admins,
unrestricted company-managed rows and own rows resolve. An integration a sync
source is already bound to is used as bound, whoever runs the sync,
and a source whose user cannot see it is reported instead. The catalog clone
is covered by ``test_catalog_clone_visibility_postgres``.
"""

from __future__ import annotations

import logging
import os
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.core.database as database
from app.api.v1 import helpers, knowledge_bases, security_audit
from app.api.v1.schemas.knowledge_base import CreateSyncSourceRequest, UpdateSyncSourceRequest
from app.core.security import encrypt_value
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.integration_user_credential import IntegrationUserCredential
from app.models.intent import Intent, IntentBinding
from app.models.knowledge_base import KBSyncSource, KnowledgeBase
from app.models.platform_settings import PlatformSettings
from app.models.relay import RelayGroup
from app.models.template_container import TemplateContainer
from app.models.user import User
from app.models.workspace import Workspace
from app.services import ctem_metrics_collector, kb_sync, kb_sync_scheduler
from app.services import trigger_integration_access as access
from app.services.builtin_tools.agent_builder import build_agent_builder_tool
from app.tools.base import ToolCall

pytestmark = pytest.mark.integration

_OUTSIDER = uuid.uuid4()
_MEMBER = uuid.uuid4()
_GRANTEE = uuid.uuid4()
_ADMIN = uuid.uuid4()
_CREATOR = uuid.uuid4()
_GROUP = uuid.uuid4()
_ACTORS = {"outsider": _OUTSIDER, "member": _MEMBER, "grantee": _GRANTEE, "admin": _ADMIN}

_COMPANY = {"auth_type": "api_key", "is_company_managed": True, "user_id": _CREATOR}
_RESTRICTED = {**_COMPANY, "access_restricted": True}
_TO_GROUP = {**_RESTRICTED, "shared_group_ids": [str(_GROUP)]}
_TO_GRANTEE = {**_RESTRICTED, "shared_user_ids": [str(_GRANTEE)]}

#: (integration columns, acting user, resolves) - one row per case
_CASES = [
    pytest.param(_TO_GROUP, "outsider", False, id="restricted_no_grant"),
    pytest.param(_TO_GROUP, "member", True, id="restricted_group_grant"),
    pytest.param(_TO_GRANTEE, "grantee", True, id="restricted_user_grant"),
    pytest.param(_RESTRICTED, "admin", True, id="restricted_admin"),
    pytest.param(_COMPANY, "outsider", True, id="unrestricted_company"),
]
_OWN = pytest.param({"auth_type": "api_key", "user_id": _OUTSIDER}, "outsider", True, id="own")
#: Every case, whatever the restriction decides: a binding is used as bound.
_BOUND_CASES = [pytest.param(*case.values[:2], id=case.id) for case in [*_CASES, _OWN]]


def _database_url() -> str:
    # CRLF-safe: a .env dragged through Windows leaves \r on the value.
    return os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot"
    ).strip()


@pytest.fixture
async def session():
    schema = f"integ_restricted_{uuid.uuid4().hex[:10]}"
    engine = create_async_engine(_database_url())
    try:
        async with engine.connect() as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    tables = (
        User.__table__, Group.__table__, GroupMember.__table__, RelayGroup.__table__, Integration.__table__,
        # A knowledge base may belong to a workspace, whose visibility it then follows.
        Workspace.__table__, KnowledgeBase.__table__, KBSyncSource.__table__, Agent.__table__, Assignment.__table__,
        IntegrationUserCredential.__table__,
        # The agent builder reads the organization context default (Settings > Organization)
        # from platform_settings, which references a template container.
        TemplateContainer.__table__, PlatformSettings.__table__,
        # get_agent and list_agents name the agent's intent bindings.
        Intent.__table__,
        IntentBinding.__table__,
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(text(f'SET search_path TO "{schema}"'))
            for table in tables:
                await conn.run_sync(lambda c, t=table: t.create(c))
            db = AsyncSession(bind=conn, expire_on_commit=False)
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


async def _add(session, provider: str, columns: dict) -> Integration:
    row = Integration(
        id=uuid.uuid4(), provider=provider, name=f"{provider}-{uuid.uuid4().hex[:6]}",
        status="connected", api_key_enc=encrypt_value(f"key-{provider}"), **columns,
    )
    session.add(row)
    await session.flush()
    return row


# -- the shared filter ----------------------------------------------------


@pytest.mark.parametrize(
    ("actor", "expected"),
    [
        ("outsider", {"unrestricted", "outsider_own"}),
        ("member", {"unrestricted", "to_group"}),
        ("grantee", {"unrestricted", "to_grantee"}),
        ("admin", {"unrestricted", "to_group", "to_grantee", "outsider_own"}),
        (None, {"unrestricted"}),
    ],
)
async def test_the_filter_sees_a_restricted_row_through_a_grant_only(session, actor, expected):
    for name, columns in {
        "to_group": _TO_GROUP, "to_grantee": _TO_GRANTEE, "unrestricted": _COMPANY,
        "outsider_own": {"auth_type": "api_key", "user_id": _OUTSIDER},
    }.items():
        session.add(Integration(id=uuid.uuid4(), provider="x", name=name, status="connected", **columns))
    await session.flush()

    rows = await session.execute(
        select(Integration.name).where(
            await helpers.integration_visible_filter(session, _ACTORS.get(actor))
        )
    )

    assert set(rows.scalars().all()) == expected


# -- KB sync credential resolution ----------------------------------------


@pytest.mark.parametrize(("columns", "actor", "resolves"), [*_CASES, _OWN])
async def test_kb_sync_falls_back_only_to_a_company_integration_its_owner_may_see(
    session, columns, actor, resolves
):
    await _add(session, "notion", columns)

    if resolves:
        token = await kb_sync._get_integration_token(session, _ACTORS[actor], "notion")
        assert token == "key-notion"
    else:
        with pytest.raises(ValueError, match="No connected notion integration"):
            await kb_sync._get_integration_token(session, _ACTORS[actor], "notion")


@pytest.mark.parametrize(("columns", "actor"), _BOUND_CASES)
async def test_kb_sync_uses_a_bound_integration_as_bound(session, columns, actor):
    row = await _add(session, "github", columns)

    token = await kb_sync._get_integration_token(
        session, _ACTORS[actor], "github", integration_id=str(row.id)
    )

    assert token == "key-github"


@pytest.mark.parametrize(("columns", "actor", "resolves"), _CASES)
async def test_kb_sync_drata_falls_back_to_a_restricted_row_for_its_grantees_only(
    session, columns, actor, resolves
):
    await _add(session, "drata", columns)

    fallback = await kb_sync._get_drata_integration(session, _ACTORS[actor])

    assert (fallback is not None) is resolves


@pytest.mark.parametrize(("columns", "actor"), _BOUND_CASES)
async def test_kb_sync_drata_uses_a_bound_integration_as_bound(session, columns, actor):
    row = await _add(session, "drata", columns)

    bound = await kb_sync._get_drata_integration(session, _ACTORS[actor], str(row.id))

    assert bound is not None and bound.id == row.id


# -- a bound source its user cannot see is reported -------------------------


async def _bound_source(session, base, integration, *, user_id, enabled=True) -> KBSyncSource:
    source = KBSyncSource(
        id=uuid.uuid4(), knowledge_base_id=base.id, user_id=user_id, source_type="github",
        config={}, integration_id=integration.id, enabled=enabled,
    )
    session.add(source)
    await session.flush()
    return source


@pytest.mark.parametrize(("columns", "actor", "resolves"), [*_CASES, _OWN])
async def test_a_sync_names_a_bound_integration_its_user_cannot_see(
    session, caplog, columns, actor, resolves
):
    row = await _add(session, "github", columns)

    with caplog.at_level(logging.WARNING, logger=access.__name__):
        await kb_sync_scheduler._report_unseen_bound_integration(
            session, str(uuid.uuid4()), str(row.id), _ACTORS[actor]
        )

    assert bool([r for r in caplog.records if r.name == access.__name__]) is not resolves


async def test_a_check_that_fails_leaves_the_sync_session_usable(session, monkeypatch):
    row = await _add(session, "github", _TO_GROUP)

    async def _broken(_self, db, *_args):
        await db.execute(text("SELECT * FROM no_such_table"))

    monkeypatch.setattr(access.TriggerIntegrationVisibility, "hides", _broken)

    await kb_sync_scheduler._report_unseen_bound_integration(
        session, str(uuid.uuid4()), str(row.id), _OUTSIDER
    )

    # The check ran in a savepoint: the sync's own transaction carries on.
    assert (await session.execute(select(Integration.id).where(Integration.id == row.id))).scalar_one() == row.id


async def test_the_audit_lists_the_sources_whose_user_cannot_see_their_integration(session, kb):
    restricted = await _add(session, "github", _TO_GROUP)
    unseen = await _bound_source(session, kb, restricted, user_id=_OUTSIDER)
    await _bound_source(session, kb, restricted, user_id=_MEMBER)
    await _bound_source(session, kb, restricted, user_id=_ADMIN)
    await _bound_source(session, kb, restricted, user_id=_OUTSIDER, enabled=False)
    # No configuring user: the knowledge base's owner, whom the sync runs as.
    await _bound_source(session, kb, restricted, user_id=None)
    outsider_kb = KnowledgeBase(id=uuid.uuid4(), user_id=_OUTSIDER, name="outsider-kb")
    session.add(outsider_kb)
    await session.flush()
    via_owner = await _bound_source(session, outsider_kb, restricted, user_id=None)

    report = await access.sync_source_integration_visibility(session)

    assert report.checked == 5
    assert {source_id for source_id, _name in report.unseen} == {unseen.id, via_owner.id}
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_kb_sync_source_integrations(session, findings)
    [finding] = findings
    assert (finding.id, finding.severity) == ("access.kb_sync_source_integration_not_visible", "warning")
    assert str(unseen.id) in finding.description and str(via_owner.id) in finding.description


async def test_the_audit_passes_when_every_bound_source_syncs_as_a_user_who_can_see_it(session, kb):
    restricted = await _add(session, "github", _TO_GROUP)
    await _bound_source(session, kb, restricted, user_id=_MEMBER)
    await _bound_source(session, kb, await _add(session, "notion", _COMPANY), user_id=_OUTSIDER)

    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_kb_sync_source_integrations(session, findings)

    assert [(f.id, f.severity) for f in findings] == [("access.kb_sync_source_integrations_visible", "pass")]


# -- KB sync source create / update ----------------------------------------


@pytest.fixture
async def kb(session, monkeypatch):
    base = KnowledgeBase(id=uuid.uuid4(), user_id=_CREATOR, name="kb")
    session.add(base)
    await session.flush()
    monkeypatch.setattr(knowledge_bases, "get_user_knowledge_base", AsyncMock(return_value=base))
    for guard in (
        "assert_knowledge_base_not_package_origin",
        "_assert_sync_sources_licensed",
        "_assert_kb_manageable",
    ):
        monkeypatch.setattr(knowledge_bases, guard, AsyncMock(return_value=None))
    return base


@pytest.mark.parametrize("kb_owner", [False, True], ids=["co_manager", "kb_owner"])
@pytest.mark.parametrize(("columns", "actor", "resolves"), [*_CASES, _OWN])
async def test_a_sync_source_binds_a_company_integration_only_for_its_grantees(
    session, kb, columns, actor, resolves, kb_owner
):
    row = await _add(session, "github", columns)
    user = await session.get(User, _ACTORS[actor])
    if kb_owner:
        kb.user_id = user.id
        await session.flush()
    body = CreateSyncSourceRequest(source_type="github", config={}, integration_id=str(row.id))

    if resolves:
        created = await knowledge_bases.create_sync_source(
            str(kb.id), body, user=user, db=session, _ai_gate=None
        )
        assert str(created.integration_id) == str(row.id)
    else:
        with pytest.raises(HTTPException) as refused:
            await knowledge_bases.create_sync_source(
                str(kb.id), body, user=user, db=session, _ai_gate=None
            )
        assert refused.value.status_code == 403


@pytest.mark.parametrize(("columns", "actor", "resolves"), [*_CASES, _OWN])
async def test_a_sync_source_is_rebound_to_a_company_integration_only_for_its_grantees(
    session, kb, columns, actor, resolves
):
    row = await _add(session, "github", columns)
    user = await session.get(User, _ACTORS[actor])
    source = KBSyncSource(
        id=uuid.uuid4(), knowledge_base_id=kb.id, user_id=user.id, source_type="github", config={}
    )
    session.add(source)
    await session.flush()
    body = UpdateSyncSourceRequest(integration_id=str(row.id))

    if resolves:
        await knowledge_bases.update_sync_source(
            str(kb.id), str(source.id), body, user=user, db=session, _ai_gate=None
        )
        assert source.integration_id == row.id
    else:
        with pytest.raises(HTTPException) as refused:
            await knowledge_bases.update_sync_source(
                str(kb.id), str(source.id), body, user=user, db=session, _ai_gate=None
            )
        assert refused.value.status_code == 403


# -- CTEM dashboard ---------------------------------------------------------


@pytest.mark.parametrize(("columns", "actor", "resolves"), _CASES)
async def test_ctem_reads_a_restricted_platform_only_for_its_grantees(
    session, monkeypatch, columns, actor, resolves
):
    row = await _add(session, "opencti", columns)
    minted: list[str] = []

    async def _token(_db, integration_id, _provider, *, user_id=None):
        minted.append(integration_id)
        return "https://opencti.example||jwt"

    monkeypatch.setattr(ctem_metrics_collector, "_get_token", _token)
    monkeypatch.setattr(ctem_metrics_collector, "release_db_connection_before_loop", AsyncMock())

    creds = await ctem_metrics_collector._user_creds(session, str(_ACTORS[actor]), "opencti")

    assert (creds is not None) is resolves
    assert minted == ([str(row.id)] if resolves else [])


# -- agent builder, anonymous caller ------------------------------------------


class _SessionContext:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture
def run_anonymously(session, monkeypatch):
    """Run one agent builder tool as a caller with no account."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(session))
    _, executor = build_agent_builder_tool(session, "anonymous")

    async def run(name: str, **arguments) -> str:
        result = await executor.execute_tool(ToolCall(id=name, name=name, arguments=arguments))
        return result.content

    return run


async def test_the_agent_builder_never_lists_a_restricted_integration_to_an_anonymous_caller(
    session, run_anonymously
):
    restricted = await _add(session, "slack", _TO_GROUP)
    unrestricted = await _add(session, "jira", _COMPANY)

    listed = await run_anonymously("list_integrations")
    resources = await run_anonymously("list_available_resources", resource_type="integrations")
    detail = await run_anonymously("get_integration", integration_id=str(restricted.id))
    visible_detail = await run_anonymously("get_integration", integration_id=str(unrestricted.id))

    for output in (listed, resources):
        assert str(unrestricted.id) in output
        assert str(restricted.id) not in output
    assert "not found" in detail
    assert str(unrestricted.id) in visible_detail


async def test_the_agent_builder_never_names_a_restricted_integration_to_an_anonymous_caller(
    session, run_anonymously
):
    restricted = await _add(session, "slack", _TO_GROUP)
    unrestricted = await _add(session, "jira", _COMPANY)
    agent = Agent(
        name="Company helper", user_id=_CREATOR, is_company_managed=True,
        integration_bindings={"slack": [str(restricted.id)], "jira": [str(unrestricted.id)]},
    )
    session.add(agent)
    await session.flush()

    detail = await run_anonymously("get_agent", agent_id=str(agent.id))
    listing = await run_anonymously("list_agents", detail="full")

    for output in (detail, listing):
        assert unrestricted.name in output
        assert str(restricted.id) in output
        assert restricted.name not in output
