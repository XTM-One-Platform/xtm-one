"""The admin app's provider-level webhook against real PostgreSQL.

``test_provider_webhook_app_scope.py`` scripts the session: it proves the
decisions, not the SQL behind them - the JSONB ``?`` that selects the bound
agents, the ``IN`` over their candidate integrations, and
``user_visible_filter``'s ``?|`` over ``shared_group_ids`` that decides whether
an agent owner can still see an integration they do not own. This drives
``_load_provider_routing`` and ``receive_provider_webhook`` over real rows.
Every owner is an ordinary user: the route has no admin bypass.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import webhooks
from app.core.security import encrypt_value
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.oauth_default import OAuthDefault
from app.models.user import User
from app.services import github_token_installations
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

ADMIN_CLIENT_ID = "admin-app-client-id-4299-pg"
OWN_CLIENT_ID = "users-own-app-client-id-pg"


@pytest.fixture(autouse=True)
def _clean_cache():
    webhooks._invalidate_provider_routing_cache()
    yield
    webhooks._invalidate_provider_routing_cache()


@dataclass
class World:
    users: dict[str, User]
    groups: dict[str, Group]
    assignments: dict[str, Assignment]
    integrations: dict[str, Integration]


def _integration(owner: User, *, client_id=ADMIN_CLIENT_ID, acknowledged=False, groups=None, extra=None):
    return Integration(
        user_id=owner.id,
        provider="github",
        auth_type="oauth",
        name=f"GitHub {uuid.uuid4().hex[:6]}",
        client_id_enc=encrypt_value(client_id),
        # Connected: the token a strict-mode delivery reads its installations with.
        access_token_enc=encrypt_value(f"gho-{uuid.uuid4().hex}"),
        # What the create path records: True when it copied the admin's OAuth defaults.
        uses_admin_oauth_credentials=client_id == ADMIN_CLIENT_ID,
        client_secret_enc=encrypt_value("secret"),
        oauth_share_acknowledged=acknowledged,
        shared_group_ids=[str(g.id) for g in groups] if groups else None,
        extra_config_enc=encrypt_value(json.dumps(extra)) if extra else None,
        status="connected",
    )


async def _seed(sessions: async_sessionmaker) -> World:
    """Alice's agents, one per GitHub integration, each with an ``issues`` assignment.

    ``admin_app``: hers, on the admin app. ``own_app``: hers, on her own app.
    ``shared_seen``: Bob's, token shared with acknowledgement with a group Alice
    is in. ``shared_unseen``: the same, with a group she is not in.
    ``unacknowledged``: Bob's, shared with her group, token not acknowledged.
    ``installation``: hers, on the admin app, recording GitHub App installation 111.
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
                provider="github",
                client_id_enc=encrypt_value(ADMIN_CLIENT_ID),
                client_secret_enc=encrypt_value("admin-secret"),
            )
        )
        alice, bob = users["alice"], users["bob"]
        integrations = {
            "admin_app": _integration(alice),
            "own_app": _integration(alice, client_id=OWN_CLIENT_ID),
            "shared_seen": _integration(bob, acknowledged=True, groups=[groups["team"]]),
            "shared_unseen": _integration(bob, acknowledged=True, groups=[groups["other"]]),
            "unacknowledged": _integration(bob, groups=[groups["team"]]),
            "installation": _integration(alice, extra={"installations": [{"id": 111}]}),
        }
        db.add_all(integrations.values())
        await db.flush()
        agents = {
            key: Agent(
                name=f"Agent {key}",
                slug=f"{key.replace('_', '-')}-{uuid.uuid4().hex[:8]}",
                persona="p",
                user_id=alice.id,
                integration_bindings={"github": [str(integration.id)]},
            )
            for key, integration in integrations.items()
        }
        db.add_all(agents.values())
        await db.flush()
        assignments = {
            key: Assignment(
                agent_id=agent.id,
                user_id=agent.user_id,
                is_company_managed=False,
                name=f"On issue {key}",
                trigger_type="integration_event",
                trigger_config={"provider": "github", "event": "issues"},
                prompt="Triage it.",
                enabled=True,
            )
            for key, agent in agents.items()
        }
        db.add_all(assignments.values())
        await db.commit()
    return World(users=users, groups=groups, assignments=assignments, integrations=integrations)


def _keys(world: World, assignment_ids) -> set[str]:
    wanted = {str(i) for i in assignment_ids}
    return {key for key, a in world.assignments.items() if str(a.id) in wanted}


def _request(payload: dict):
    body = json.dumps(payload).encode()

    async def _body() -> bytes:
        return body

    return SimpleNamespace(
        headers={"X-GitHub-Event": "issues"},
        body=_body,
        url="https://copilot.example/api/hooks/providers/github",
        method="POST",
        query_params={},
        client=SimpleNamespace(host="203.0.113.7"),
    )


async def _deliver(installation_id: int) -> set[str]:
    """The assignment ids a GitHub App delivery of *installation_id* triggers."""
    trigger = AsyncMock(side_effect=lambda **kw: f"run-{kw['assignment'].id}")
    with (
        patch.object(
            webhooks, "_claim_webhook_dedup_slot", AsyncMock(return_value=(False, None, None))
        ),
        patch.object(webhooks, "_finalize_dedup", AsyncMock(side_effect=lambda _k, r: r)),
        patch.object(webhooks, "_release_webhook_dedup_slot", AsyncMock()),
        patch.object(webhooks, "trigger_assignment", new=trigger),
    ):
        await webhooks.receive_provider_webhook(
            "github", _request({"action": "opened", "installation": {"id": installation_id}})
        )
    return {str(call.kwargs["assignment"].id) for call in trigger.await_args_list}


def _scratch():
    return scratch_database(
        "provider_webhook_scope",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
    )


@pytest.mark.asyncio
async def test_the_snapshot_holds_the_admin_app_integrations_the_owner_may_use_and_see():
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        routing = await webhooks._load_provider_routing("github")

    # Every matcher is kept whatever the mode (the option can be switched at any
    # time); the strict rule reaches only those it gives an admin-app integration.
    assert len(routing.matchers) == len(world.assignments)
    reached = {m.assignment_id: m for m in routing.matchers if m.integrations}
    assert _keys(world, reached) == {"admin_app", "shared_seen", "installation"}
    installation = reached[str(world.assignments["installation"].id)]
    assert [recorded for _id, recorded in installation.integrations] == [frozenset({"111"})]


@pytest.mark.asyncio
async def test_a_delivery_reaches_them_and_the_installation_narrows_it():
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        other_installation = _keys(world, await _deliver(222))
        recorded_installation = _keys(world, await _deliver(111))

    assert other_installation == {"admin_app", "shared_seen"}
    assert recorded_installation == {"admin_app", "shared_seen", "installation"}


@pytest.mark.asyncio
async def test_taken_out_of_the_group_the_owner_stops_hearing_at_once():
    """The cached snapshot still holds Bob's shared integration; the match
    path reads Alice's memberships fresh and no longer hands her its events."""
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        routing = await webhooks._get_provider_routing("github")
        assert "shared_seen" in _keys(world, (m.assignment_id for m in routing.matchers))
        async with scratch.sessions() as db:
            await db.execute(
                delete(GroupMember).where(GroupMember.user_id == world.users["alice"].id)
            )
            await db.commit()
        triggered = _keys(world, await _deliver(222))
        # The delivery was matched against that same snapshot, not a reload.
        assert webhooks._provider_routing_cache["github"][1] is routing

    assert triggered == {"admin_app"}


class _AnswerStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def mget(self, keys):
        return [self.values.get(key) for key in keys]

    async def set(self, key, value, *, ex=None):
        self.values[key] = value
        return True


@pytest.mark.asyncio
async def test_a_token_narrows_the_integrations_recording_no_installation(monkeypatch):
    """The answer cached for each stored token decides, and a new token is no answer."""
    store = _AnswerStore()

    async def _store():
        return store

    monkeypatch.setattr(github_token_installations, "redis_store", _store)
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        async with scratch.sessions() as db:
            rows = (await db.execute(select(*webhooks._token_row_columns()))).all()
        candidates = {str(row[0]): github_token_installations.token_candidate(*row) for row in rows}
        for key, sees in (("admin_app", ["111"]), ("shared_seen", ["222"])):
            candidate = candidates[str(world.integrations[key].id)]
            store.values[f"github:token_installations:{candidate.integration_id}"] = json.dumps(
                {"fp": candidate.fingerprint, "at": time.time(), "ids": sees, "complete": True}
            )
        narrowed = _keys(world, await _deliver(222))
        async with scratch.sessions() as db:
            admin_app = await db.get(Integration, world.integrations["admin_app"].id)
            admin_app.access_token_enc = encrypt_value("gho-reconnected")
            await db.commit()
        after_reconnect = _keys(world, await _deliver(222))
        await github_token_installations.settle()

    assert narrowed == {"shared_seen"}
    # No answer yet for the new token: reached as before while it is looked up.
    assert after_reconnect == {"admin_app", "shared_seen"}


@pytest.mark.asyncio
async def test_without_an_oauth_default_nobody_is_reached():
    async with _scratch() as scratch:
        await _seed(scratch.sessions)
        async with scratch.sessions() as db:
            await db.execute(delete(OAuthDefault))
            await db.commit()
        routing = await webhooks._load_provider_routing("github")

    assert routing.has_agents is True
    assert [m for m in routing.matchers if m.integrations] == []
    assert webhooks._match_provider_assignments(routing, "github", "issues.opened") == []

@pytest.fixture(autouse=True)
def _strict_admin_app_scoping(monkeypatch):
    """The narrowing these tests pin is what INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION turns on."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "integration_notifications_require_verification", True)
