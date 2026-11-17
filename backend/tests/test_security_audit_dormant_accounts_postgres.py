"""The automations of accounts nobody signs in to, read on real PostgreSQL.

``_check_dormant_account_automations`` reports the active accounts that own an
automation deactivating them would pause (an enabled assignment
on an enabled agent or an active flow, not company-managed) when no completed
sign-in was recorded for them in ``DORMANT_ACCOUNT_DAYS`` days. The rule is SQL
(an anti-join on ``audit_logs``), so it is exercised against the real schema.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import security_audit
from app.models.activity import Activity
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.audit_log import AuditLog
from app.models.flow import FLOW_STATUS_ACTIVE, FLOW_STATUS_DRAFT, Flow
from app.models.user import User
from app.services import security_audit_notifications as notifications
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)
LONG_AGO = NOW - timedelta(days=90)


def _scratch(prefix: str):
    return scratch_database(
        prefix,
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    )


def _user(db: AsyncSession, name: str, *, created: datetime = LONG_AGO, active: bool = True) -> User:
    user = User(
        email=f"{name}@example.com",
        hashed_password="x",
        full_name=name.capitalize(),
        is_active=active,
        created_at=created,
    )
    db.add(user)
    return user


def _agent(db: AsyncSession, owner: User, *, enabled: bool = True, company: bool = False) -> Agent:
    agent = Agent(
        name=f"{owner.full_name}'s agent",
        slug=f"a-{uuid.uuid4().hex[:8]}",
        user_id=owner.id,
        enabled=enabled,
        is_company_managed=company,
    )
    db.add(agent)
    return agent


def _assignment(
    db: AsyncSession, agent: Agent, owner: User, *, enabled: bool = True, company: bool = False
) -> Assignment:
    assignment = Assignment(
        agent_id=agent.id,
        user_id=owner.id,
        name=f"Digest {uuid.uuid4().hex[:4]}",
        trigger_type="schedule",
        trigger_config={"cron": "0 8 * * *"},
        prompt="Summarise the day.",
        enabled=enabled,
        is_company_managed=company,
    )
    db.add(assignment)
    return assignment


def _sign_in(db: AsyncSession, user: User, when: datetime, *, entity_type: str = "user", action: str = "login") -> None:
    db.add(
        AuditLog(
            user_id=user.id,
            user_email=user.email,
            action=action,
            entity_type=entity_type,
            entity_id=user.id,
            summary=f"Logged in ({user.email})",
            created_at=when,
        )
    )


async def _findings(db: AsyncSession) -> dict[str | None, security_audit.AuditFinding]:
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_dormant_account_automations(db, findings)
    return {finding.id: finding for finding in findings}


async def test_accounts_nobody_signed_in_to_are_listed_with_what_they_own() -> None:
    async with _scratch("security_audit_dormant") as scratch, scratch.sessions() as db:
        # Left 45 days ago: two assignments and a flow still run.
        alice = _user(db, "alice")
        # Signed in three days ago.
        bob = _user(db, "bob")
        # Only the password step of a sign-in awaiting its second factor.
        carol = _user(db, "carol")
        # Deactivated: the deactivation already pauses what the account owns.
        dave = _user(db, "dave", active=False)
        # A new account, not yet 30 days old.
        erin = _user(db, "erin", created=NOW - timedelta(days=5))
        # Owns only what runs anyway or does not run at all.
        frank = _user(db, "frank")
        await db.flush()

        alice_agent = _agent(db, alice)
        bob_agent = _agent(db, bob)
        carol_agent = _agent(db, carol)
        dave_agent = _agent(db, dave)
        erin_agent = _agent(db, erin)
        frank_agent = _agent(db, frank)
        frank_disabled_agent = _agent(db, frank, enabled=False)
        await db.flush()

        _assignment(db, alice_agent, alice)
        _assignment(db, alice_agent, alice)
        _assignment(db, alice_agent, alice, enabled=False)
        db.add(Flow(name="Alice's live flow", user_id=alice.id, status=FLOW_STATUS_ACTIVE, graph={}))
        db.add(Flow(name="Alice's draft", user_id=alice.id, status=FLOW_STATUS_DRAFT, graph={}))
        _assignment(db, bob_agent, bob)
        _assignment(db, carol_agent, carol)
        _assignment(db, dave_agent, dave)
        _assignment(db, erin_agent, erin)
        _assignment(db, frank_agent, frank, company=True)
        _assignment(db, frank_disabled_agent, frank)
        db.add(
            Flow(
                name="Frank's company flow",
                user_id=frank.id,
                status=FLOW_STATUS_ACTIVE,
                graph={},
                is_company_managed=True,
            )
        )

        _sign_in(db, alice, NOW - timedelta(days=60))
        _sign_in(db, alice, NOW - timedelta(days=45))
        # Declining an XTM One Desktop request is a refusal, not a sign-in.
        _sign_in(db, alice, NOW - timedelta(days=1), action="login_declined")
        _sign_in(db, bob, NOW - timedelta(days=3))
        _sign_in(db, carol, NOW - timedelta(days=2), entity_type="mfa")
        await db.commit()

        findings = await _findings(db)

    assert set(findings) == {"authentication.dormant_account_automations"}
    finding = findings["authentication.dormant_account_automations"]
    assert finding.severity == "warning"
    assert finding.category == "authentication"
    assert finding.title == "4 active automation(s) run as 2 account(s) nobody signed in to for 30 days"
    carol_line = (
        f"Carol (carol@example.com): no sign-in recorded since the account was created on "
        f"{LONG_AGO.date().isoformat()}, 1 assignment(s)"
    )
    alice_line = (
        f"Alice (alice@example.com): last sign-in {(NOW - timedelta(days=45)).date().isoformat()}, "
        "2 assignment(s) and 1 flow(s)"
    )
    # The longest without a sign-in first.
    assert f"Accounts: {carol_line}; {alice_line}." in finding.description
    for listed_elsewhere in ("bob@", "dave@", "erin@", "frank@"):
        assert listed_elsewhere not in finding.description
    assert "Administration > Users and groups" in finding.recommendation


async def test_owners_who_signed_in_within_the_window_pass() -> None:
    async with _scratch("security_audit_dormant_pass") as scratch, scratch.sessions() as db:
        bob = _user(db, "bob")
        erin = _user(db, "erin", created=NOW - timedelta(days=5))
        await db.flush()
        bob_agent = _agent(db, bob)
        await db.flush()
        _assignment(db, bob_agent, bob)
        db.add(Flow(name="Erin's flow", user_id=erin.id, status=FLOW_STATUS_ACTIVE, graph={}))
        _sign_in(db, bob, NOW - timedelta(days=29))
        await db.commit()

        findings = await _findings(db)

    assert set(findings) == {"authentication.automation_owners_signed_in"}
    finding = findings["authentication.automation_owners_signed_in"]
    assert finding.severity == "pass"
    assert finding.description.startswith("2 account(s) own assignments or flows")


async def test_nothing_is_reported_without_an_automation_to_pause() -> None:
    async with _scratch("security_audit_dormant_none") as scratch, scratch.sessions() as db:
        frank = _user(db, "frank")
        dave = _user(db, "dave", active=False)
        await db.flush()
        frank_agent = _agent(db, frank, company=True)
        dave_agent = _agent(db, dave)
        await db.flush()
        _assignment(db, frank_agent, frank, company=True)
        _assignment(db, dave_agent, dave)
        await db.commit()

        findings = await _findings(db)

    assert findings == {}


class _Redis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)


async def test_the_hourly_run_notifies_every_active_administrator(monkeypatch) -> None:
    """The whole audit on a session of the scheduler's, then the bell's rows."""
    async with _scratch("security_audit_dormant_notify") as scratch:
        async with scratch.sessions() as db:
            admin = _user(db, "admin", created=NOW)
            admin.role = "admin"
            other_admin = _user(db, "root", created=NOW)
            other_admin.role = "admin"
            retired_admin = _user(db, "former", created=NOW, active=False)
            retired_admin.role = "admin"
            alice = _user(db, "alice")
            await db.flush()
            alice_agent = _agent(db, alice)
            await db.flush()
            _assignment(db, alice_agent, alice)
            _sign_in(db, alice, NOW - timedelta(days=45))
            await db.commit()
            admin_ids = {admin.id, other_admin.id}

        monkeypatch.setattr(notifications, "async_session", scratch.sessions)
        redis = _Redis()
        notified = await notifications.notify_new_findings(redis)

        async with scratch.sessions() as db:
            rows = (await db.execute(select(Activity).where(Activity.activity_type == "notification"))).scalars().all()
        # A second run: the finding was reported.
        assert await notifications.notify_new_findings(redis) == 0

    assert notified >= 1
    assert {row.user_id for row in rows} == admin_ids
    for row in rows:
        assert row.agent_name == "Security audit"
        assert row.extra_data["notification_source"] == "security_audit"
        assert "authentication.dormant_account_automations" in row.extra_data["finding_ids"]
        assert row.extra_data["audit_path"].startswith("/settings/security?finding=")
        # The titles only: who the finding lists stays on the admin page.
        assert "alice@example.com" not in (row.summary or "") + row.title
