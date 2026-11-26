"""The ``user_admin`` tools (#250) against real PostgreSQL: the one cascade, read back.

Seeded as ``test_user_deactivation_postgres`` seeds the cascade: an account
owning a private agent, a shared agent, a company-managed agent, a key, a
timer and a private OAuth integration. The tools run as an administrator, with
the session factory pointed at the scratch database.

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.api_key import UserApiKey
from app.models.assignment import Assignment
from app.models.audit_log import AuditLog
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.platform_timer import TIMER_CANCELLED, PlatformTimer
from app.models.user import User
from app.services.assignment_pause import OWNER_DEACTIVATED_REASON
from app.services.builtin_tools import build_user_admin_tools
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_user_deactivation_postgres import _PLATFORM_ADMIN, _seed, _user

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _quiet_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "admin_email", _PLATFORM_ADMIN)
    monkeypatch.setattr("app.services.notify.notify_web_user", AsyncMock())
    monkeypatch.setattr("app.services.quota.invalidate_membership_quota_cache", AsyncMock())


def _point_sessions_at(monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
    from app.core import database

    @asynccontextmanager
    async def _session() -> AsyncIterator[Any]:
        async with db.sessions() as session:
            yield session

    monkeypatch.setattr(database, "async_session", _session)


def _tools(admin_id: uuid.UUID, *, admin: bool = True) -> Any:
    _, executor = build_user_admin_tools(
        user_id=str(admin_id), caller_is_admin=admin, agent_name="Assistant", run_id="run-1"
    )

    async def call(name: str, **args: Any) -> str:
        handler = executor.handler(name)
        assert handler is not None
        answer = await handler(**args)
        assert isinstance(answer, str)
        return answer

    return call


async def test_deactivate_runs_the_cascade_then_reactivate_restores_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async with (
        scratch_database("useradmin", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        seed = await _seed(session)
        users = seed["users"]
        _point_sessions_at(monkeypatch, db)
        call = _tools(users["older_admin"].id)
        departing = (await session.get(User, users["departing"].id)).email

        # The default is a dry run: the preview, nothing written.
        answer = json.loads(await call("deactivate_platform_users", users=[departing], reason="left the company"))
        assert answer["dry_run"] is True
        assert answer["users"][0]["counts"]["api_keys_revoked"] == 1
        session.expire_all()
        assert (await session.get(User, users["departing"].id)).is_active is True

        answer = json.loads(
            await call("deactivate_platform_users", users=[departing], reason="left the company", dry_run=False)
        )
        [result] = answer["results"]
        assert result["status"] == "done" and result["review_count"] >= 1
        session.expire_all()
        assert (await session.get(User, users["departing"].id)).is_active is False
        assert (await session.get(Agent, seed["private"])).enabled is False
        as_departed = await session.get(Assignment, seed["shared_as_departed"])
        assert as_departed.enabled is False and as_departed.paused_reason == OWNER_DEACTIVATED_REASON
        assert (await session.get(PlatformTimer, seed["timer"])).state == TIMER_CANCELLED
        assert (await session.get(UserApiKey, seed["key"])).revoked_at is not None
        assert (await session.get(Integration, seed["oauth"])).status != "connected"
        assert (await session.get(Agent, seed["shared"])).user_id == users["platform_admin"].id

        audit = (
            (await session.execute(select(AuditLog).where(AuditLog.entity_id == str(users["departing"].id))))
            .scalars()
            .all()
        )
        mine = [row for row in audit if (row.details or {}).get("tool_group") == "user_admin"]
        assert mine and mine[0].details["run_id"] == "run-1" and mine[0].details["reason"] == "left the company"

        # Idempotent: a second call finds the account already off.
        again = json.loads(
            await call("deactivate_platform_users", users=[departing], reason="left the company", dry_run=False)
        )
        assert again["results"][0]["status"] == "already_inactive"

        review = json.loads(await call("list_deactivation_review", user=departing))
        assert review["items"]
        item = review["items"][0]
        resolved = json.loads(
            await call(
                "resolve_deactivation_review_item",
                deactivation_id=item["deactivation_id"],
                key=item["key"],
                resolution="keep",
            )
        )
        assert resolved["item"]["resolution"] == "keep"

        restored = json.loads(await call("reactivate_platform_user", user=departing))
        assert restored["status"] == "done"
        session.expire_all()
        assert (await session.get(User, users["departing"].id)).is_active is True
        assert (await session.get(Agent, seed["private"])).enabled is True
        assert (await session.get(Assignment, seed["shared_as_departed"])).enabled is True

        listed = json.loads(await call("list_platform_users", status="active", search="departing"))
        assert [row["email"] for row in listed["users"]] == [departing]
        assert listed["users"][0]["owned"]["agents"] >= 1


async def test_refusals_self_last_admin_protected_and_non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    async with (
        scratch_database("useradminref", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        admin = _user("solo", role="admin")
        guarded = _user("breakglass")
        guarded.deactivation_protected = True
        plain = _user("plain")
        session.add_all([admin, guarded, plain])
        await session.commit()
        _point_sessions_at(monkeypatch, db)
        call = _tools(admin.id)

        answer = json.loads(
            await call(
                "deactivate_platform_users",
                users=[str(admin.id), guarded.email, "nobody@test.local"],
                reason="test",
                dry_run=False,
            )
        )
        reasons = {row["user"]: row.get("reason") for row in answer["results"]}
        assert reasons[admin.email] == "Cannot deactivate yourself"
        assert "protected" in reasons[guarded.email]
        assert reasons["nobody@test.local"] == "No such user"

        # The last active administrator: refused whoever asks (an actor that
        # is not an active administrator here, as an API token's owner may be).
        from types import SimpleNamespace

        from app.services.builtin_tools.user_admin import deactivation_refusal

        solo = await session.get(User, admin.id)
        assert await deactivation_refusal(session, solo, SimpleNamespace(id=uuid.uuid4())) == (
            "The last active administrator cannot be deactivated"
        )
        other = _user("other", role="admin")
        session.add(other)
        await session.commit()
        assert await deactivation_refusal(session, solo, SimpleNamespace(id=other.id)) is None

        # A user who is not an administrator is refused, whatever the turn claims.
        assert (await _tools(plain.id)("list_platform_users")).startswith("Refused")


async def test_batch_goes_on_after_a_failed_account_rolled_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rollback expires every instance: every later account and the answer still read."""
    from app.services import user_deactivation

    async with (
        scratch_database("useradminrb", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        admin = _user("rbadmin", role="admin")
        failing = _user("failing")
        leaving = _user("leaving")
        last = _user("last")
        session.add_all([admin, failing, leaving, last])
        await session.commit()
        _point_sessions_at(monkeypatch, db)
        real = user_deactivation.deactivate_user
        failing_id, leaving_id, last_id = failing.id, leaving.id, last.id

        async def _deactivate(db: Any, user: Any, **kwargs: Any) -> Any:
            if user.id == failing_id:
                raise RuntimeError("cascade failed")
            return await real(db, user, **kwargs)

        monkeypatch.setattr(user_deactivation, "deactivate_user", _deactivate)
        answer = json.loads(
            await _tools(admin.id)(
                "deactivate_platform_users",
                users=[failing.email, leaving.email, last.email],
                reason="test",
                dry_run=False,
            )
        )
        statuses = {row["user"]: row["status"] for row in answer["results"]}
        assert statuses == {failing.email: "error", leaving.email: "done", last.email: "done"}
        session.expire_all()
        assert (await session.get(User, failing_id)).is_active is True
        assert (await session.get(User, leaving_id)).is_active is False
        assert (await session.get(User, last_id)).is_active is False


async def test_batch_answers_not_reached_past_its_time_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2182: a batch longer than the budget answers what it did and what it left, never times out."""
    from app.services import user_deactivation
    from app.services.builtin_tools import user_admin

    async with (
        scratch_database("useradminbud", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        admin = _user("budadmin", role="admin")
        accounts = [_user(f"bud{index}") for index in range(10)]
        session.add_all([admin, *accounts])
        await session.commit()
        _point_sessions_at(monkeypatch, db)
        now = [0.0]
        monkeypatch.setattr(user_admin, "_clock", lambda: now[0])
        real = user_deactivation.deactivate_user

        async def _slow(db: Any, user: Any, **kwargs: Any) -> Any:
            now[0] += 60.0  # each cascade costs a minute
            return await real(db, user, **kwargs)

        monkeypatch.setattr(user_deactivation, "deactivate_user", _slow)
        emails = [account.email for account in accounts]
        ids = [account.id for account in accounts]
        answer = json.loads(
            await _tools(admin.id)("deactivate_platform_users", users=emails, reason="test", dry_run=False)
        )
        statuses = [row["status"] for row in answer["results"]]
        assert statuses == ["done"] * 4 + ["not_reached"] * 6
        assert answer["not_reached"] == emails[4:]
        assert "call again" in answer["next_step"]
        session.expire_all()
        assert [(await session.get(User, user_id)).is_active for user_id in ids] == [False] * 4 + [True] * 6


async def test_group_members_and_a_scim_group_stays_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async with (
        scratch_database("useradmingrp", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        admin = _user("admin", role="admin")
        member = _user("member")
        session.add_all([admin, member])
        await session.flush()
        local = Group(name="local-team", description="", created_by=admin.id)
        scim = Group(name="entra-team", description="", created_by=admin.id, scim_external_id="ext-1")
        session.add_all([local, scim])
        await session.commit()
        _point_sessions_at(monkeypatch, db)
        call = _tools(admin.id)

        added = json.loads(await call("add_group_members", group_id=str(local.id), users=[member.email]))
        assert added["results"] == [{"user": member.email, "status": "added"}]
        group = json.loads(await call("get_platform_group", group_id=str(local.id)))
        assert [m["email"] for m in group["members"]] == [member.email]
        groups = json.loads(await call("list_platform_groups", search="team"))
        assert {g["name"]: g["scim_managed"] for g in groups["groups"]} == {"entra-team": True, "local-team": False}
        removed = json.loads(await call("remove_group_members", group_id=str(local.id), users=[member.email]))
        assert removed["results"] == [{"user": member.email, "status": "removed"}]
        refused = await call("add_group_members", group_id=str(scim.id), users=[member.email])
        assert refused.startswith("Refused: the group entra-team is managed by the identity provider")
        rows = (await session.execute(select(GroupMember).where(GroupMember.group_id == scim.id))).scalars().all()
        assert rows == []
