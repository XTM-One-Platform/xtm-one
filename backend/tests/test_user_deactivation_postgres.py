"""The user-deactivation cascade against real PostgreSQL (#122).

One account owns a private agent with a scheduled assignment, a shared agent
whose assignments run as the account and as nobody in particular, a
company-managed agent, a personal API key, an armed timer, a private OAuth
integration another person's agent binds, and a shared workspace. Deactivating
it is checked object by object; then a reactivation restores the snapshot, and
a second deactivation is a no-op.

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.api_key import UserApiKey
from app.models.assignment import Assignment
from app.models.audit_log import AuditLog
from app.models.integration import Integration
from app.models.platform_timer import TIMER_ARMED, TIMER_CANCELLED, PlatformTimer
from app.models.user import User
from app.models.user_deactivation import UserDeactivation
from app.models.workspace import Workspace
from app.services import user_deactivation
from app.services.assignment_pause import OWNER_DEACTIVATED_REASON
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_PLATFORM_ADMIN = "platform-admin@test.local"


@pytest.fixture(autouse=True)
def _quiet_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "admin_email", _PLATFORM_ADMIN)
    # The bell's SSE push and the A2A hooks' Redis writes are not under test.
    monkeypatch.setattr("app.services.notify.notify_web_user", AsyncMock())


def _user(name: str, *, role: str = "user", email: str | None = None, created: datetime | None = None) -> User:
    return User(
        email=email or f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
        role=role,
        created_at=created or datetime.now(UTC),
    )


def _agent(owner: uuid.UUID, **kw: Any) -> Agent:
    return Agent(
        name=kw.pop("name", f"agent-{uuid.uuid4().hex[:6]}"),
        slug=f"agent-{uuid.uuid4().hex[:8]}",
        persona="You are an agent.",
        enabled=True,
        user_id=owner,
        is_builtin=False,
        builtin_tool_ids=[],
        handover_agent_ids=[],
        output_format="markdown",
        **kw,
    )


def _assignment(agent: Agent, owner: uuid.UUID, **kw: Any) -> Assignment:
    return Assignment(
        agent_id=agent.id,
        user_id=owner,
        is_company_managed=agent.is_company_managed,
        name=kw.pop("name", f"assignment-{uuid.uuid4().hex[:6]}"),
        trigger_type=kw.pop("trigger_type", "schedule"),
        trigger_config=kw.pop("trigger_config", {"cron": "0 9 * * *"}),
        prompt="Do the thing.",
        enabled=True,
        dry_run=False,
        timeout_seconds=300,
        retry_on_failure=False,
        max_retries=0,
        notify_on_complete=False,
        execution_count=0,
        **kw,
    )


async def _seed(session: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    users = {
        "departing": _user("departing"),
        "platform_admin": _user("admin", role="admin", email=_PLATFORM_ADMIN, created=now - timedelta(days=30)),
        "older_admin": _user("older", role="admin", created=now - timedelta(days=60)),
        "colleague": _user("colleague"),
    }
    session.add_all(users.values())
    await session.flush()
    d = users["departing"].id

    private = _agent(d, name="private")
    shared = _agent(d, name="shared", shared_user_ids=[str(users["colleague"].id)])
    company = _agent(d, name="company", is_company_managed=True)
    session.add_all([private, shared, company])
    await session.flush()
    private_job = _assignment(private, d, name="private-job")
    shared_as_departed = _assignment(shared, d, name="runs-as-departed", run_as_user_id=d)
    shared_plain = _assignment(shared, d, name="plain")
    company_job = _assignment(company, d, name="company-job")
    session.add_all([private_job, shared_as_departed, shared_plain, company_job])

    oauth = Integration(
        provider="microsoft",
        name="departing-mailbox",
        auth_type="oauth",
        status="connected",
        user_id=d,
        is_company_managed=False,
        access_token_enc="enc-access",
        refresh_token_enc="enc-refresh",
    )
    session.add(oauth)
    await session.flush()
    borrower = _agent(users["colleague"].id, name="borrower", integration_bindings={"microsoft": [str(oauth.id)]})
    session.add(borrower)

    key = UserApiKey(user_id=d, name="laptop", key_prefix="fcp-test", key_hash="h", is_active=True)
    session.add(key)
    timer = PlatformTimer(
        user_id=d, agent_id=private.id, correlation_key="t:1", due_at=now + timedelta(hours=1), payload={}
    )
    session.add(timer)
    workspace = Workspace(
        name="team",
        slug="team",
        user_id=d,
        is_company_managed=False,
        shared_user_ids=[str(users["colleague"].id)],
    )
    session.add(workspace)
    await session.commit()
    # Plain ids: a test expires the session, and an expired ORM attribute read
    # outside an await would try a synchronous load.
    return {
        "users": {name: SimpleNamespace(id=user.id) for name, user in users.items()},
        "private": private.id,
        "shared": shared.id,
        "company": company.id,
        "private_job": private_job.id,
        "shared_as_departed": shared_as_departed.id,
        "shared_plain": shared_plain.id,
        "company_job": company_job.id,
        "oauth": oauth.id,
        "borrower": borrower.id,
        "key": key.id,
        "timer": timer.id,
        "workspace": workspace.id,
    }


async def test_deactivation_cascade_then_reactivation_round_trip() -> None:
    async with (
        scratch_database("userdeact", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        seed = await _seed(session)
        users = seed["users"]
        departing = await session.get(User, users["departing"].id)
        admin = await session.get(User, users["older_admin"].id)

        outcome = await user_deactivation.deactivate_user(
            session, departing, source="admin", actor=admin, actor_label="Older"
        )
        await session.commit()
        session.expire_all()

        # Account.
        departing = await session.get(User, users["departing"].id)
        assert departing.is_active is False
        assert departing.deactivation_source == "admin"
        assert departing.deactivated_at is not None and departing.deprovisioned_at is None

        # The successor is the ADMIN_EMAIL account, though another admin is older.
        assert outcome.successor_id == users["platform_admin"].id

        # Credentials.
        key = await session.get(UserApiKey, seed["key"])
        assert key.revoked_at is not None and key.is_active is False
        timer = await session.get(PlatformTimer, seed["timer"])
        assert timer.state == TIMER_CANCELLED

        # Private agent: disabled, its assignment keeps its own switch.
        private = await session.get(Agent, seed["private"])
        assert private.enabled is False and private.user_id == users["departing"].id
        assert (await session.get(Assignment, seed["private_job"])).enabled is True

        # Shared and company-managed agents: moved to the platform admin.
        for key_name in ("shared", "company"):
            agent = await session.get(Agent, seed[key_name])
            assert agent.user_id == users["platform_admin"].id and agent.enabled is True
        as_departed = await session.get(Assignment, seed["shared_as_departed"])
        assert as_departed.enabled is False and as_departed.paused_reason == OWNER_DEACTIVATED_REASON
        plain = await session.get(Assignment, seed["shared_plain"])
        assert plain.enabled is True and plain.user_id == users["platform_admin"].id

        # Shared workspace moved; private OAuth integration disconnected.
        assert (await session.get(Workspace, seed["workspace"])).user_id == users["platform_admin"].id
        oauth = await session.get(Integration, seed["oauth"])
        assert oauth.access_token_enc is None and oauth.refresh_token_enc is None
        assert oauth.status == "disconnected"

        # The review list names each object, the borrowing agent included.
        actions = {(i["kind"], i["id"], i["action"]) for i in outcome.review_items}
        assert ("agent", str(seed["shared"]), "transferred") in actions
        assert ("agent", str(seed["company"]), "transferred") in actions
        assert ("assignment", str(seed["shared_as_departed"]), "paused") in actions
        assert ("workspace", str(seed["workspace"]), "transferred") in actions
        assert ("integration", str(seed["oauth"]), "disconnected") in actions
        assert ("agent", str(seed["borrower"]), "bound_by_other_agent") in actions

        # One episode and one audit row.
        episodes = (
            (await session.execute(select(UserDeactivation).where(UserDeactivation.user_id == users["departing"].id)))
            .scalars()
            .all()
        )
        assert len(episodes) == 1 and episodes[0].reactivated_at is None
        audits = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == str(users["departing"].id), AuditLog.action == "deactivate"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 1

        # A second deactivation is a no-op answering the open episode.
        again = await user_deactivation.deactivate_user(session, departing, source="scim", actor_label="SCIM (entra)")
        episode_id = episodes[0].id
        assert again.already is True and again.episode_id == episode_id
        await session.commit()
        assert (
            len(
                (
                    await session.execute(
                        select(UserDeactivation).where(UserDeactivation.user_id == users["departing"].id)
                    )
                )
                .scalars()
                .all()
            )
            == 1
        )

        # Reactivation restores the snapshot, not the revoked key.
        departing = await session.get(User, users["departing"].id)
        admin = await session.get(User, users["older_admin"].id)
        back = await user_deactivation.reactivate_user(session, departing, source="admin", actor=admin)
        await session.commit()
        session.expire_all()
        assert back.already is False
        departing = await session.get(User, users["departing"].id)
        assert departing.is_active is True and departing.deactivated_at is None
        assert (await session.get(Agent, seed["private"])).enabled is True
        for key_name in ("shared", "company"):
            assert (await session.get(Agent, seed[key_name])).user_id == users["departing"].id
        as_departed = await session.get(Assignment, seed["shared_as_departed"])
        assert as_departed.enabled is True and as_departed.paused_reason is None
        assert (await session.get(Assignment, seed["shared_plain"])).user_id == users["departing"].id
        assert (await session.get(Workspace, seed["workspace"])).user_id == users["departing"].id
        assert (await session.get(UserApiKey, seed["key"])).revoked_at is not None
        assert (await session.get(PlatformTimer, seed["timer"])).state == TIMER_CANCELLED
        episode = await session.get(UserDeactivation, episode_id)
        assert episode.reactivated_at is not None


async def test_reviewed_object_is_not_taken_back_and_deprovision_is_soft() -> None:
    async with (
        scratch_database("userdeact2", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        seed = await _seed(session)
        users = seed["users"]
        departing = await session.get(User, users["departing"].id)
        outcome = await user_deactivation.deactivate_user(
            session, departing, source="scim", actor_label="SCIM (entra)", deprovision=True
        )
        await session.commit()
        departing = await session.get(User, users["departing"].id)
        assert departing.deprovisioned_at is not None and departing.deactivation_source == "scim"
        # Nothing is purged.
        assert await session.get(Agent, seed["private"]) is not None

        episode = await session.get(UserDeactivation, outcome.episode_id)
        admin = await session.get(User, users["platform_admin"].id)
        key = next(i["key"] for i in episode.review_items if i["id"] == str(seed["shared"]))
        await user_deactivation.resolve_review_item(session, episode, key, resolution="keep", admin=admin)
        await session.commit()

        await user_deactivation.reactivate_user(session, departing, source="scim", actor_label="SCIM (entra)")
        await session.commit()
        session.expire_all()
        # Kept with the admin on review: not taken back.
        assert (await session.get(Agent, seed["shared"])).user_id == users["platform_admin"].id
        # Not reviewed: taken back.
        assert (await session.get(Agent, seed["company"])).user_id == users["departing"].id


async def test_no_successor_keeps_objects_and_lists_them() -> None:
    async with (
        scratch_database("userdeact3", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        lone = _user("lone")
        session.add(lone)
        await session.flush()
        agent = _agent(lone.id, name="shared-alone", is_company_managed=True)
        session.add(agent)
        await session.commit()
        outcome = await user_deactivation.deactivate_user(session, lone, source="system", actor_label="system")
        await session.commit()
        assert outcome.successor_id is None
        assert (await session.get(Agent, agent.id)).user_id == lone.id
        assert [i["action"] for i in outcome.review_items] == ["kept_no_successor"]


async def test_platform_admin_and_self_deactivation_are_refused() -> None:
    async with (
        scratch_database("userdeact4", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        platform_admin = _user("admin", role="admin", email=_PLATFORM_ADMIN)
        other = _user("other", role="admin")
        session.add_all([platform_admin, other])
        await session.commit()
        with pytest.raises(user_deactivation.DeactivationError):
            await user_deactivation.deactivate_user(session, platform_admin, source="scim")
        with pytest.raises(user_deactivation.DeactivationError):
            await user_deactivation.deactivate_user(session, other, source="admin", actor=other)
        assert TIMER_ARMED  # the state constants stay importable from the model
