"""The LinkedIn notification endpoint against real PostgreSQL.

``test_linkedin_notification_scope.py`` answers the queries itself: it proves
the decisions, not the SQL behind them - the ``IN`` over the assignments'
candidate integrations and ``user_visible_filter``'s ``?|`` over
``shared_group_ids`` that decides whether an agent's owner still sees an
integration they do not own. This posts signed deliveries through the real
route (ASGI, on the test's own event loop) over real rows. Every owner is an
ordinary user: the route has no admin bypass.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import integration_notifications as notifications
from app.core.security import encrypt_value
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.oauth_default import OAuthDefault
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

URL = "/api/v1/integrations/notifications/linkedin"
SECRET = "linkedin-admin-app-secret-4385-pg"
ADMIN_CLIENT_ID = "linkedin-admin-app-client-id-4385-pg"
OWN_CLIENT_ID = "users-own-linkedin-app-client-id-pg"
BATCH = json.dumps(
    {
        "type": "ORGANIZATION_SOCIAL_ACTION_NOTIFICATIONS",
        "notifications": [
            {
                "action": "COMMENT",
                "notificationId": 4385,
                "organizationalEntity": "urn:li:organization:1",
                "decoratedGeneratedActivity": {"comment": {"text": "hi", "owner": "urn:li:person:1"}},
            }
        ],
    }
).encode()


@dataclass
class World:
    users: dict[str, User]
    assignments: dict[str, Assignment]


def _integration(owner: User | None, *, client_id=ADMIN_CLIENT_ID, acknowledged=False, groups=None):
    return Integration(
        user_id=owner.id if owner else None,
        is_company_managed=owner is None,
        provider="linkedin",
        auth_type="oauth",
        name=f"LinkedIn {uuid.uuid4().hex[:6]}",
        client_id_enc=encrypt_value(client_id),
        client_secret_enc=encrypt_value("secret"),
        # What the create path records: true only when it copied the admin's app.
        uses_admin_oauth_credentials=client_id == ADMIN_CLIENT_ID,
        oauth_share_acknowledged=acknowledged,
        shared_group_ids=[str(g.id) for g in groups] if groups else None,
        status="connected",
    )


async def _seed(sessions: async_sessionmaker) -> World:
    """One agent per case, each with a catch-all LinkedIn assignment.

    Alice's agents: ``admin_app`` (her integration on the admin app), ``own_app``
    (hers, on her own app), ``shared_seen`` (Bob's, token shared with
    acknowledgement with a group she is in), ``shared_unseen`` (the same, with a
    group she is not in), ``unacknowledged`` (Bob's, shared with her group, token
    not acknowledged), ``private`` (Bob's, acknowledged but shared with nobody:
    a binding left from a withdrawn share), ``unbound`` (no LinkedIn
    integration) and ``pinned`` (bound to her own app, pinned to the admin-app
    one). ``company``: a company-managed agent with no owner, bound to a
    company-managed integration on the admin app whose token is acknowledged.
    """
    async with sessions() as db:
        users = {
            name: User(
                email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                full_name=name,
                role="user",
            )
            for name in ("alice", "bob")
        }
        groups = {name: Group(name=f"{name}-{uuid.uuid4().hex[:8]}") for name in ("team", "other")}
        db.add_all([*users.values(), *groups.values()])
        await db.flush()
        db.add(GroupMember(group_id=groups["team"].id, user_id=users["alice"].id))
        db.add(
            OAuthDefault(
                provider="linkedin",
                client_id_enc=encrypt_value(ADMIN_CLIENT_ID),
                client_secret_enc=encrypt_value(SECRET),
            )
        )
        alice, bob = users["alice"], users["bob"]
        integrations = {
            "admin_app": _integration(alice),
            "own_app": _integration(alice, client_id=OWN_CLIENT_ID),
            "shared_seen": _integration(bob, acknowledged=True, groups=[groups["team"]]),
            "shared_unseen": _integration(bob, acknowledged=True, groups=[groups["other"]]),
            "unacknowledged": _integration(bob, groups=[groups["team"]]),
            "private": _integration(bob, acknowledged=True),
            "company": _integration(None, acknowledged=True),
        }
        db.add_all(integrations.values())
        await db.flush()

        bindings = {key: [str(integration.id)] for key, integration in integrations.items()}
        bindings["unbound"] = []
        bindings["pinned"] = [str(integrations["own_app"].id)]
        agents = {
            key: Agent(
                name=f"Agent {key}",
                slug=f"{key.replace('_', '-')}-{uuid.uuid4().hex[:8]}",
                persona="p",
                user_id=None if key == "company" else alice.id,
                is_company_managed=key == "company",
                integration_bindings={"linkedin": ids} if ids else {},
            )
            for key, ids in bindings.items()
        }
        db.add_all(agents.values())
        await db.flush()
        assignments = {}
        for key, agent in agents.items():
            config = {"provider": "linkedin", "event": "linkedin_social_action"}
            if key == "pinned":
                config["integration_id"] = str(integrations["admin_app"].id)
            assignments[key] = Assignment(
                agent_id=agent.id,
                user_id=agent.user_id,
                is_company_managed=agent.is_company_managed,
                name=f"On LinkedIn {key}",
                trigger_type="integration_event",
                trigger_config=config,
                prompt="Answer it.",
                enabled=True,
            )
        db.add_all(assignments.values())
        await db.commit()
    return World(users=users, assignments=assignments)


async def _deliver(*, signed: bool = True) -> tuple[httpx.Response, set[str]]:
    """POST ``BATCH`` through the route; return the response and the triggered assignment ids."""
    trigger = AsyncMock(side_effect=lambda **kw: f"run-{kw['assignment'].id}")
    app = FastAPI()
    app.include_router(notifications.notification_router, prefix="/api/v1/integrations")
    headers = {"Content-Type": "application/json"}
    if signed:
        headers["X-LI-Signature"] = "hmacsha256=" + hmac.new(SECRET.encode(), BATCH, hashlib.sha256).hexdigest()
    with patch.object(notifications, "trigger_assignment", new=trigger):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://copilot.example"
        ) as client:
            response = await client.post(URL, content=BATCH, headers=headers)
    return response, {str(call.kwargs["assignment"].id) for call in trigger.await_args_list}


def _keys(world: World, assignment_ids) -> set[str]:
    wanted = {str(i) for i in assignment_ids}
    return {key for key, a in world.assignments.items() if str(a.id) in wanted}


def _scratch():
    return scratch_database(
        "linkedin_notification_scope",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    )


@pytest.mark.asyncio
async def test_a_delivery_reaches_the_admin_app_integrations_the_owner_may_use_and_see():
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        response, triggered = await _deliver()

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "triggered": 4}
    assert _keys(world, triggered) == {"admin_app", "shared_seen", "pinned", "company"}


@pytest.mark.asyncio
async def test_taken_out_of_the_group_the_owner_stops_hearing_at_once():
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        async with scratch.sessions() as db:
            await db.execute(delete(GroupMember).where(GroupMember.user_id == world.users["alice"].id))
            await db.commit()
        _response, triggered = await _deliver()

    assert _keys(world, triggered) == {"admin_app", "pinned", "company"}


@pytest.mark.asyncio
async def test_without_an_oauth_default_nobody_is_reached():
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        async with scratch.sessions() as db:
            await db.execute(delete(OAuthDefault))
            await db.commit()
        response, triggered = await _deliver(signed=False)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "triggered": 0}
    assert _keys(world, triggered) == set()

@pytest.fixture(autouse=True)
def _strict_admin_app_scoping(monkeypatch):
    """The narrowing these tests pin is what INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION turns on."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "integration_notifications_require_verification", True)
