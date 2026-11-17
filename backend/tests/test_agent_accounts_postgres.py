"""Agent accounts through the real routers against real PostgreSQL, as non-admins.

An account is its owner's alone: another user gets the unknown id's 404 on
every route, an administrator reaches every account (``scope=all`` on the
lists); the bindings an account names are checked against its owner; the
approval queue posts or drops a draft once; the public push endpoint acts
only on an entry naming a recorded subscription and echoing its clientState.
Nothing reaches a provider: the sync, the release and the send are stubbed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.deps import get_current_user
from app.api.v1 import agent_accounts as api
from app.channels import base as channel_base
from app.core.database import get_db
from app.core.security import encrypt_value
from app.models.agent import Agent
from app.models.agent_account import AgentAccount, AgentAccountReply, AgentAccountSubscription
from app.models.audit_log import AuditLog
from app.models.integration import Integration
from app.models.user import User
from app.services.agent_accounts import management
from app.services.agent_accounts.events import AccountIdentity
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


#: The accounts a route handed to a background sync, and those it released.
_scheduled: list[str] = []
_released: list[tuple[str, str | None]] = []
#: Who each integration is signed in as, by integration id (unknown: the provider did not say).
_signed_in: dict[str, AccountIdentity] = {}


@pytest.fixture(autouse=True)
def _no_provider(monkeypatch):
    """Nothing in these tests talks to Microsoft Graph."""
    _scheduled.clear()
    _released.clear()
    _signed_in.clear()

    def _schedule(account_id, *, poll=False):
        _scheduled.append(account_id)

    async def _identity(db, provider, *, owner_id, integration_id):
        return _signed_in.get(str(integration_id))

    async def _release(db, account):
        _released.append((str(account.id), str(account.integration_id)))
        await db.execute(
            AgentAccountSubscription.__table__.delete().where(AgentAccountSubscription.account_id == account.id)
        )

    monkeypatch.setattr(management, "schedule_sync", _schedule)
    monkeypatch.setattr(management, "release_account", _release)
    monkeypatch.setattr(management, "read_identity", _identity)


class Caller:
    """The user the routers authenticate, switched per request."""

    def __init__(self) -> None:
        self.user: User | None = None


@asynccontextmanager
async def _client(session_factory: async_sessionmaker, caller: Caller) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(api.notification_router, prefix="/api/v1/agent-accounts")
    app.include_router(api.router, prefix="/api/v1/agent-accounts")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: caller.user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _user(db, name: str, role: str = "user") -> User:
    user = User(email=f"{name}-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name=name, role=role)
    db.add(user)
    await db.flush()
    return user


async def _integration(db, owner: User, provider: str = "microsoft") -> Integration:
    integration = Integration(
        user_id=owner.id,
        provider=provider,
        auth_type="oauth",
        name=f"{owner.full_name}'s {provider}",
        status="connected",
        access_token_enc=encrypt_value("token"),
    )
    db.add(integration)
    await db.flush()
    return integration


async def _agent(db, owner: User) -> Agent:
    agent = Agent(name=f"{owner.full_name} agent", slug=f"a-{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db.add(agent)
    await db.flush()
    return agent


async def _world(db):
    owner = await _user(db, "Owner")
    other = await _user(db, "Other")
    admin = await _user(db, "Admin", role="admin")
    world = {
        "owner": owner,
        "other": other,
        "admin": admin,
        "integration": await _integration(db, owner),
        "slack": await _integration(db, owner, provider="slack"),
        "other_integration": await _integration(db, other),
        "agent": await _agent(db, owner),
        "other_agent": await _agent(db, other),
    }
    await db.commit()
    return world


def _create_body(world, **overrides) -> dict:
    body = {
        "name": "Teams delegate",
        "provider": "microsoft_teams",
        "mode": "on_behalf",
        "integration_id": str(world["integration"].id),
        "agent_id": str(world["agent"].id),
    }
    body.update(overrides)
    return body


async def test_an_account_is_its_owners_alone_and_admins_reach_it_with_scope_all():
    async with _scratch("agent_accounts_scope") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = world["owner"]
            created = await client.post("/api/v1/agent-accounts", json=_create_body(world))
            assert created.status_code == 201, created.text
            account = created.json()
            # The preset of the mode: approval, label, grace period, DMs + mentions.
            assert account["response_mode"] == "approval" and account["ai_label_enabled"] is True
            assert [r["id"] for r in account["rules"]] == ["direct", "mentions"]
            # Answered at once: the provider is reached by a background sync.
            assert account["status"] == "setup" and _scheduled == [account["id"]]
            path = f"/api/v1/agent-accounts/{account['id']}"

            caller.user = world["other"]
            assert (await client.get(path)).status_code == 404
            assert (await client.put(path, json={"name": "mine now"})).status_code == 404
            assert (await client.delete(path)).status_code == 404
            assert (await client.get("/api/v1/agent-accounts")).json() == []
            assert (await client.get("/api/v1/agent-accounts?scope=all")).json() == []

            caller.user = world["admin"]
            assert (await client.get("/api/v1/agent-accounts")).json() == []
            listed = (await client.get("/api/v1/agent-accounts?scope=all")).json()
            assert [a["id"] for a in listed] == [account["id"]]
            assert (await client.get(path)).status_code == 200

            caller.user = world["owner"]
            assert (await client.delete(path)).status_code == 204
            assert (await client.get(path)).status_code == 404
        async with scratch.sessions() as db:
            actions = (await db.execute(select(AuditLog.action).where(AuditLog.entity_type == "agent_account"))).scalars().all()
            assert sorted(actions) == ["create", "delete"]


async def test_the_account_serves_what_its_last_sync_found():
    """The card reads the report: why a conversation is not heard, which ones, and since when."""
    report = {
        "checked_at": "2026-10-10T20:00:00+00:00",
        "wanted": 14,
        "heard": 2,
        "problems": [
            {
                "kind": "refused", "count": 12, "permission": "ChannelMessage.Read.All", "holder": None,
                "targets": [{"covers": "channel", "name": "Ops > General"}],
                "details": ["Microsoft Graph answered 403: ..."],
            }
        ],
    }
    async with _scratch("agent_accounts_report") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
            account, _reply = await _account_with_reply(db, world)
            account.sync_report = report
            await db.commit()
            path = f"/api/v1/agent-accounts/{account.id}"
        caller = Caller()
        caller.user = world["owner"]
        async with _client(scratch.sessions, caller) as client:
            served = (await client.get(path)).json()
            # A report stored before the notes reads with no notes.
            expected = {**report, "problems": [{**problem, "notes": []} for problem in report["problems"]]}
            assert served["sync_report"] == expected and served["last_error"] is None
            async with scratch.sessions() as db:
                stored = await db.get(AgentAccount, account.id)
                stored.sync_report = {"problems": "not a list"}
                await db.commit()
            # A report that no longer reads is the report of no sync, never a 500.
            assert (await client.get(path)).json()["sync_report"] is None


async def test_the_bindings_are_checked_against_the_owner():
    async with _scratch("agent_accounts_bindings") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = world["owner"]
            for overrides in (
                {"integration_id": str(world["other_integration"].id)},
                {"integration_id": str(world["slack"].id)},
                {"agent_id": str(world["other_agent"].id)},
                {"provider": "carrier_pigeon"},
            ):
                response = await client.post("/api/v1/agent-accounts", json=_create_body(world, **overrides))
                assert response.status_code == 400, overrides
            created = (await client.post("/api/v1/agent-accounts", json=_create_body(world))).json()
            path = f"/api/v1/agent-accounts/{created['id']}"
            # An administrator editing someone's account binds what the OWNER may use.
            caller.user = world["admin"]
            refused = await client.put(path, json={"agent_id": str(world["other_agent"].id)})
            assert refused.status_code == 400


async def test_rules_and_schedule_are_normalized_by_the_engine():
    async with _scratch("agent_accounts_rules") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = world["owner"]
            created = (await client.post("/api/v1/agent-accounts", json=_create_body(world, mode="ai_user"))).json()
            assert created["response_mode"] == "auto" and created["ai_label_enabled"] is False
            path = f"/api/v1/agent-accounts/{created['id']}"
            bad = await client.put(path, json={"rules": [{"trigger": "sometimes"}]})
            assert bad.status_code == 400 and "trigger" in bad.json()["detail"]
            good = await client.put(
                path,
                json={
                    "rules": [{"id": "watch", "conversation_types": ["channel"], "trigger": "any", "reply": "decide"}],
                    "active_hours": {"timezone": "Europe/Paris", "windows": [{"days": [1, 2], "start": "09:00", "end": "18:00"}]},
                },
            )
            assert good.status_code == 200, good.text
            saved = good.json()
            assert saved["rules"][0]["reply"] == "decide" and saved["rules"][0]["continue_threads"] is True
            assert saved["active_hours"]["mode"] == "inside"
            cleared = (await client.put(path, json={"clear_active_hours": True})).json()
            assert cleared["active_hours"] is None


async def test_the_simulator_decides_without_reading_or_writing_anything():
    async with _scratch("agent_accounts_simulate") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = world["other"]
            response = await client.post(
                "/api/v1/agent-accounts/simulate",
                json={"mode": "on_behalf", "event": {"conversation_type": "group", "text": "hi"}},
            )
            assert response.status_code == 200
            assert response.json()["decision"]["reason"] == "not_addressed"
            mentioned = await client.post(
                "/api/v1/agent-accounts/simulate",
                json={"mode": "on_behalf", "event": {"conversation_type": "group", "mentions_account": True}},
            )
            assert mentioned.json()["decision"]["respond"] is True


async def test_the_simulator_reads_the_groups_of_the_xtm_one_user_behind_an_email():
    from app.models.group import Group, GroupMember

    async with _scratch("agent_accounts_simulate_groups") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
            board = Group(name=f"Executive Management {uuid.uuid4().hex[:4]}")
            db.add(board)
            await db.flush()
            db.add(GroupMember(group_id=board.id, user_id=world["owner"].id))
            await db.commit()
            board_id, owner_email = str(board.id), world["owner"].email
        rules = [
            {"id": "direct", "conversation_types": ["direct"], "trigger": "any", "reply": "decide",
             "groups": [{"id": board_id, "name": "Executive Management"}]},
            {"id": "channel", "conversation_types": ["group", "channel"], "trigger": "any",
             "conversations": [{"id": "19:exec@thread.tacv2", "name": "Exec", "type": "channel", "team_id": "t"}]},
        ]
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = world["owner"]

            async def _decide(email: str) -> dict:
                response = await client.post(
                    "/api/v1/agent-accounts/simulate",
                    json={
                        "mode": "ai_user",
                        "settings": {"rules": rules},
                        "event": {"conversation_type": "direct", "conversation_id": "19:exec@thread.tacv2",
                                  "sender_email": email, "text": "Can you have a look at this?"},
                    },
                )
                assert response.status_code == 200, response.text
                return response.json()

            member = await _decide(owner_email.upper())
            assert member["decision"]["respond"] is True and member["decision"]["rule_id"] == "direct"
            # Who the sender was found to be, in the rules' own words.
            assert member["sender"] == {
                "known": True, "emails": [owner_email.upper()],
                "groups": [{"id": board_id, "name": "Executive Management"}],
            }
            stranger = await _decide("stranger@example.com")
            assert stranger["decision"]["reason"] == "no_matching_rule"
            assert stranger["sender"] == {"known": False, "emails": ["stranger@example.com"], "groups": []}
            assert stranger["decision"]["sender"] == stranger["sender"]


async def _account_with_reply(db, world, status: str = "pending") -> tuple[AgentAccount, AgentAccountReply]:
    # An integration of its own: two enabled accounts never share one.
    integration = await _integration(db, world["owner"])
    account = AgentAccount(
        user_id=world["owner"].id,
        name="Delegate",
        provider="microsoft_teams",
        integration_id=integration.id,
        agent_id=world["agent"].id,
        rules=[],
    )
    db.add(account)
    await db.flush()
    reply = AgentAccountReply(
        account_id=account.id,
        user_id=world["owner"].id,
        status=status,
        conversation_type="direct",
        conversation_id="chat-1",
        thread_id="chat-1",
        message_id="m1",
        sender_name="Alice",
        inbound_text="Can you send the report?",
        draft_text="Sure, sending it now.",
        ai_label=True,
        route={"kind": "chat", "chat_id": "chat-1"},
    )
    db.add(reply)
    await db.commit()
    return account, reply


async def test_a_pending_reply_is_approved_or_rejected_once_by_its_owner(monkeypatch):
    sent: list[str | None] = []

    async def _send(db, account, reply, *, text, decided_by):
        sent.append(text)
        reply.status = "sent"
        reply.sent_text = text or reply.draft_text
        reply.decided_by_user_id = decided_by
        return reply

    monkeypatch.setattr(api, "send_pending", _send)
    async with _scratch("agent_accounts_replies") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
            _account, first = await _account_with_reply(db, world)
            _account2, second = await _account_with_reply(db, world)
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = world["other"]
            assert (await client.post(f"/api/v1/agent-accounts/replies/{first.id}/approve", json={})).status_code == 404
            assert (await client.get("/api/v1/agent-accounts/replies")).json() == []

            caller.user = world["owner"]
            pending = (await client.get("/api/v1/agent-accounts/replies?status=pending")).json()
            assert {r["id"] for r in pending} == {str(first.id), str(second.id)}
            approved = await client.post(
                f"/api/v1/agent-accounts/replies/{first.id}/approve", json={"text": "Sending it in 5 minutes."}
            )
            assert approved.status_code == 200 and approved.json()["status"] == "sent"
            assert sent == ["Sending it in 5 minutes."]
            again = await client.post(f"/api/v1/agent-accounts/replies/{first.id}/approve", json={})
            assert again.status_code == 409
            rejected = await client.post(f"/api/v1/agent-accounts/replies/{second.id}/reject")
            assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
            assert (await client.post(f"/api/v1/agent-accounts/replies/{second.id}/reject")).status_code == 409


async def test_the_push_endpoint_acts_only_on_a_recorded_subscription_echoing_its_secret(monkeypatch):
    spawned: list[tuple[str, str]] = []

    async def _handle(account_id, provider_key, entry):
        return None

    def _spawn(coro, *, name=None):
        spawned.append(name)
        coro.close()

    handshakes: list[str] = []

    async def _note(provider_key):
        handshakes.append(provider_key)

    monkeypatch.setattr(api, "handle_notification", _handle)
    monkeypatch.setattr(channel_base, "spawn_channel_task", _spawn)
    monkeypatch.setattr(api.state, "note_handshake", _note)
    async with _scratch("agent_accounts_push") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
            account, _reply = await _account_with_reply(db, world)
            db.add(
                AgentAccountSubscription(
                    account_id=account.id,
                    external_id="sub-1",
                    resource="/users/x/chats/getAllMessages",
                    client_state_enc=encrypt_value("the-secret"),
                    expires_at=datetime.now(UTC) + timedelta(days=2),
                )
            )
            await db.commit()
        caller = Caller()
        url = "/api/v1/agent-accounts/notifications/microsoft_teams"
        async with _client(scratch.sessions, caller) as client:
            handshake = await client.post(f"{url}?validationToken=hello")
            assert handshake.status_code == 200 and handshake.text == "hello"
            # Remembered: a subscription refused while no handshake came means Microsoft could not call back.
            assert handshakes == ["microsoft_teams"]

            def _body(sub_id: str, state: str) -> bytes:
                return json.dumps(
                    {"value": [{"subscriptionId": sub_id, "clientState": state, "changeType": "created",
                                "resource": "chats('19:a')/messages('1')"}]}
                ).encode()

            for sub_id, secret in (("sub-1", "wrong"), ("unknown", "the-secret")):
                response = await client.post(url, content=_body(sub_id, secret))
                assert response.status_code == 202
            assert spawned == []
            accepted = await client.post(url, content=_body("sub-1", "the-secret"))
            assert accepted.status_code == 202 and spawned == ["agent-account-notification"]
            assert (await client.post(url, content=b"not json")).status_code == 400
            assert (await client.post("/api/v1/agent-accounts/notifications/nope", content=b"{}")).status_code == 404


async def test_a_new_sign_in_releases_the_previous_ones_subscriptions_and_syncs_again():
    async with _scratch("agent_accounts_resign") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
            service = await _integration(db, world["owner"])
            await db.commit()
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = world["owner"]
            created = (await client.post("/api/v1/agent-accounts", json=_create_body(world))).json()
            path = f"/api/v1/agent-accounts/{created['id']}"
            async with scratch.sessions() as db:
                stored = await db.get(AgentAccount, uuid.UUID(created["id"]))
                stored.account_external_id = "owner-graph-id"
                await db.commit()
            moved = (await client.put(path, json={"integration_id": str(service.id)})).json()
            # Released while the account still held the previous sign-in.
            assert _released == [(created["id"], str(world["integration"].id))]
            assert moved["integration_id"] == str(service.id) and moved["account_external_id"] is None
            assert moved["status"] == "setup" and _scheduled == [created["id"], created["id"]]
            paused = (await client.put(path, json={"enabled": False})).json()
            assert paused["status"] == "paused" and len(_scheduled) == 2


async def test_the_identity_picker_names_the_account_and_who_already_operates_it(monkeypatch):
    async def _identify(self, session):
        return api.AccountIdentity(external_id="ea-graph-id", display_name="Executive Assistant", email="ea@example.com")

    async def _signed_in(db, *, integration_id, owner_id, provider, account_external_id=None):
        return api.ProviderSession(token="t")

    monkeypatch.setattr(api, "open_integration_session", _signed_in)
    monkeypatch.setattr(api.get_provider("microsoft_teams").__class__, "identify", _identify)
    async with _scratch("agent_accounts_identity") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
        caller = Caller()
        url = f"/api/v1/agent-accounts/pickers/microsoft_teams/identity?integration_id={world['integration'].id}"
        async with _client(scratch.sessions, caller) as client:
            caller.user = world["owner"]
            free = (await client.get(url)).json()
            assert free["display_name"] == "Executive Assistant" and free["email"] == "ea@example.com"
            assert free["operated_by_account_id"] is None and free["operated_by_other_user"] is False

            created = (await client.post("/api/v1/agent-accounts", json=_create_body(world, name="EA"))).json()
            async with scratch.sessions() as db:
                stored = await db.get(AgentAccount, uuid.UUID(created["id"]))
                stored.account_external_id = "ea-graph-id"
                await db.commit()
            taken = (await client.get(url)).json()
            assert taken["operated_by_account_id"] == created["id"] and taken["operated_by_account_name"] == "EA"
            # The account being edited is not its own competitor.
            editing = (await client.get(f"{url}&account_id={created['id']}")).json()
            assert editing["operated_by_account_id"] is None

            # Another user signed in as the same account learns it is taken, not by whom.
            async with scratch.sessions() as db:
                shared = await _integration(db, world["other"])
                await db.commit()
            caller.user = world["other"]
            other_url = f"/api/v1/agent-accounts/pickers/microsoft_teams/identity?integration_id={shared.id}"
            theirs = (await client.get(other_url)).json()
            assert theirs["operated_by_other_user"] is True and theirs["operated_by_account_name"] is None


async def test_one_enabled_agent_account_per_integration_and_per_platform_account():
    async with _scratch("agent_accounts_unique") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
            second_sign_in = await _integration(db, world["owner"])
            await db.commit()
        executive_assistant = AccountIdentity("ea-graph-id", "Executive Assistant", "ea@example.com")
        _signed_in[str(world["integration"].id)] = executive_assistant
        _signed_in[str(second_sign_in.id)] = executive_assistant
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = world["owner"]
            first = await client.post("/api/v1/agent-accounts", json=_create_body(world, name="EA"))
            assert first.status_code == 201 and first.json()["account_external_id"] == "ea-graph-id"

            # The same integration again.
            again = await client.post("/api/v1/agent-accounts", json=_create_body(world, name="EA (2)"))
            assert again.status_code == 409 and "'EA'" in again.json()["detail"]
            # Another integration signed in as the same account.
            other = await client.post(
                "/api/v1/agent-accounts",
                json=_create_body(world, name="EA (3)", integration_id=str(second_sign_in.id)),
            )
            assert other.status_code == 409 and "'EA'" in other.json()["detail"]

            # Saved switched off it is allowed, and stays off.
            paused = await client.post(
                "/api/v1/agent-accounts",
                json=_create_body(world, name="EA (4)", integration_id=str(second_sign_in.id), enabled=False),
            )
            assert paused.status_code == 201 and paused.json()["status"] == "paused"
            enabling = await client.put(f"/api/v1/agent-accounts/{paused.json()['id']}", json={"enabled": True})
            assert enabling.status_code == 409

            # Another user signed in as the same account learns it is taken, not by whom.
            async with scratch.sessions() as db:
                theirs = await _integration(db, world["other"])
                other_agent = world["other_agent"]
                await db.commit()
            _signed_in[str(theirs.id)] = executive_assistant
            caller.user = world["other"]
            refused = await client.post(
                "/api/v1/agent-accounts",
                json=_create_body(
                    world, name="Mine", integration_id=str(theirs.id), agent_id=str(other_agent.id)
                ),
            )
            assert refused.status_code == 409
            assert "Another user's agent account" in refused.json()["detail"] and "EA" not in refused.json()["detail"]

        async with scratch.sessions() as db:
            enabled = (
                await db.execute(select(AgentAccount.name).where(AgentAccount.enabled.is_(True)))
            ).scalars().all()
            assert enabled == ["EA"]


async def test_one_sign_in_runs_a_teams_account_and_an_outlook_mailbox_never_two_of_one_provider():
    """A Teams account and a mailbox hear different conversations."""
    async with _scratch("agent_accounts_mailbox") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
        _signed_in[str(world["integration"].id)] = AccountIdentity("sam-graph-id", "Sam", "sam@example.com")
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = world["owner"]
            teams = await client.post("/api/v1/agent-accounts", json=_create_body(world, name="Teams twin"))
            assert teams.status_code == 201
            mailbox = await client.post(
                "/api/v1/agent-accounts",
                json=_create_body(world, name="Mailbox", provider="microsoft_outlook", mode="ai_user"),
            )
            assert mailbox.status_code == 201, mailbox.text
            served = mailbox.json()
            assert served["provider_label"] == "Microsoft Outlook"
            assert [r["name"] for r in served["rules"]] == [
                "Emails to the account alone", "Emails where the account is in To",
            ]
            assert served["mailbox_actions"]["category_needs_owner"] == "Needs your attention"

            again = await client.post(
                "/api/v1/agent-accounts",
                json=_create_body(world, name="Mailbox (2)", provider="microsoft_outlook"),
            )
            assert again.status_code == 409 and "'Mailbox'" in again.json()["detail"]

            changed = await client.put(
                f"/api/v1/agent-accounts/{served['id']}",
                json={"mailbox_actions": {"mark_read_no_reply": True, "category_replied": ""}},
            )
            assert changed.status_code == 200
            assert changed.json()["mailbox_actions"]["mark_read_no_reply"] is True
            assert changed.json()["mailbox_actions"]["category_replied"] == ""
            refused = await client.put(
                f"/api/v1/agent-accounts/{served['id']}", json={"mailbox_actions": {"delete_it": True}}
            )
            assert refused.status_code == 400 and "delete_it" in refused.json()["detail"]

            catalog = (await client.get("/api/v1/agent-accounts/catalog")).json()
            outlook = next(p for p in catalog["providers"] if p["key"] == "microsoft_outlook")
            assert outlook["kind"] == "mailbox" and outlook["capabilities"]["triage"] is True
            assert outlook["presets"]["on_behalf"]["rules"][0]["reply"] == "decide"
            assert catalog["mailbox_defaults"]["flag_needs_owner"] is True


async def test_a_mailbox_starts_with_bounds_on_its_turns_and_its_reply_recipients_its_owner_can_change():
    """A mailbox's turn and reply-recipient bounds, as the account's non-admin owner (rule 19)."""
    async with _scratch("agent_accounts_limits") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = world["owner"]
            teams = await client.post("/api/v1/agent-accounts", json=_create_body(world, enabled=False))
            assert teams.status_code == 201, teams.text
            assert (teams.json()["max_turns_per_hour"], teams.json()["max_reply_recipients"]) == (0, 10)
            mailbox = await client.post(
                "/api/v1/agent-accounts",
                json=_create_body(world, name="Mailbox", provider="microsoft_outlook", mode="ai_user", enabled=False),
            )
            assert mailbox.status_code == 201, mailbox.text
            served = mailbox.json()
            assert (served["max_turns_per_hour"], served["max_reply_recipients"]) == (60, 10)

            changed = await client.put(
                f"/api/v1/agent-accounts/{served['id']}", json={"max_turns_per_hour": 5, "max_reply_recipients": 3}
            )
            assert changed.status_code == 200
            assert (changed.json()["max_turns_per_hour"], changed.json()["max_reply_recipients"]) == (5, 3)
            for body in (
                {"max_turns_per_hour": -1},
                {"max_turns_per_hour": 1001},
                {"max_reply_recipients": -1},
                {"max_reply_recipients": 1001},
                {"max_turns_per_hour": None, "max_reply_recipients": "many"},
            ):
                refused = await client.put(f"/api/v1/agent-accounts/{served['id']}", json=body)
                assert refused.status_code == 422, body
            # Another user reaches neither the account nor its limits.
            caller.user = world["other"]
            hidden = await client.put(f"/api/v1/agent-accounts/{served['id']}", json={"max_turns_per_hour": 0})
            assert hidden.status_code == 404
        async with scratch.sessions() as db:
            stored = await db.get(AgentAccount, uuid.UUID(served["id"]))
        assert (stored.max_turns_per_hour, stored.max_reply_recipients) == (5, 3)


async def test_a_mailbox_refuses_a_rule_its_provider_cannot_serve_on_every_route():
    """A channel or a named conversation on a mailbox would be stored and never match."""
    channel_rule = [{"id": "watch", "conversation_types": ["channel"], "trigger": "any"}]
    named_rule = [{"id": "named", "conversation_types": ["direct"], "conversations": [{"id": "thread-1"}]}]
    async with _scratch("agent_accounts_mailbox_rules") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
        _signed_in[str(world["integration"].id)] = AccountIdentity("sam-graph-id", "Sam", "sam@example.com")
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = world["owner"]
            mailbox_body = _create_body(world, name="Mailbox", provider="microsoft_outlook", mode="ai_user")
            refused_create = await client.post("/api/v1/agent-accounts", json={**mailbox_body, "rules": channel_rule})
            assert refused_create.status_code == 400
            assert refused_create.json()["detail"] == "Rule 1: this platform has no channel conversations."
            assert (await client.get("/api/v1/agent-accounts")).json() == []

            created = await client.post("/api/v1/agent-accounts", json=mailbox_body)
            assert created.status_code == 201, created.text
            path = f"/api/v1/agent-accounts/{created.json()['id']}"
            preset_rules = created.json()["rules"]
            for rules, detail in (
                (channel_rule, "Rule 1: this platform has no channel conversations."),
                (named_rule, "Rule 1: on this platform a rule cannot name specific conversations."),
            ):
                refused = await client.put(path, json={"rules": rules})
                assert refused.status_code == 400 and refused.json()["detail"] == detail
            assert (await client.get(path)).json()["rules"] == preset_rules

            any_type = await client.put(path, json={"rules": [{"id": "all", "conversation_types": []}]})
            assert any_type.status_code == 200, any_type.text
            assert any_type.json()["rules"][0]["conversation_types"] == ["direct", "group"]

            simulated = await client.post(
                "/api/v1/agent-accounts/simulate",
                json={"mode": "ai_user", "provider": "microsoft_outlook", "settings": {"rules": channel_rule}},
            )
            assert simulated.status_code == 400
            assert simulated.json()["detail"] == "Rule 1: this platform has no channel conversations."
            # The same rule on a chat provider stays valid.
            chat = await client.post(
                "/api/v1/agent-accounts/simulate",
                json={"mode": "ai_user", "provider": "microsoft_teams", "settings": {"rules": channel_rule}},
            )
            assert chat.status_code == 200, chat.text


async def test_the_simulator_refuses_a_provider_the_platform_does_not_have():
    async with _scratch("agent_accounts_simulate_provider") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = world["owner"]
            unknown = await client.post(
                "/api/v1/agent-accounts/simulate", json={"mode": "on_behalf", "provider": "fax_machine"}
            )
            assert unknown.status_code == 422
            (error,) = unknown.json()["detail"]
            assert error["loc"] == ["body", "provider"]
            assert "Unknown provider." in error["msg"]
            default = await client.post("/api/v1/agent-accounts/simulate", json={"mode": "on_behalf", "provider": ""})
            assert default.status_code == 200, default.text


async def test_the_migration_pauses_every_enabled_duplicate_but_the_oldest():
    import importlib.util
    import pathlib

    from sqlalchemy import text

    path = pathlib.Path(__file__).resolve().parents[1] / "alembic/versions/a5450uniq01_agent_account_one_per_platform_account.py"
    spec = importlib.util.spec_from_file_location("a5450uniq01", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with _scratch("agent_accounts_dedup") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
            # The state before the rule: the indexes did not exist.
            await db.execute(text("DROP INDEX uq_agent_accounts_operated"))
            await db.execute(text("DROP INDEX uq_agent_accounts_provider_integration"))
            now = datetime.now(UTC)
            names = []
            for index, (integration, external_id) in enumerate(
                [
                    (world["integration"], "ea"),
                    (world["integration"], None),  # never synced, same integration
                    (await _integration(db, world["owner"]), "ea"),  # another sign-in, same account
                    (await _integration(db, world["owner"]), "sam"),
                ]
            ):
                name = f"account-{index}"
                names.append(name)
                db.add(
                    AgentAccount(
                        user_id=world["owner"].id, name=name, provider="microsoft_teams",
                        integration_id=integration.id, account_external_id=external_id,
                        created_at=now - timedelta(hours=10 - index), rules=[],
                    )
                )
            await db.commit()
            paused = await db.run_sync(lambda session: migration.pause_duplicates(session.connection()))
            await db.commit()
            rows = {
                row.name: row
                for row in (await db.execute(select(AgentAccount))).scalars().all()
            }
        assert paused == 2
        assert rows["account-0"].enabled and rows["account-3"].enabled
        for name in ("account-1", "account-2"):
            assert not rows[name].enabled and rows[name].status == "error"
            assert rows[name].last_error == migration.PAUSED_REASON


async def test_a_mailbox_signs_with_a_signature_integration_its_owner_can_see():
    """As the account's non-admin owner (rule 19)."""
    async with _scratch("agent_accounts_signature") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
            letsignit = Integration(
                user_id=world["owner"].id, provider="letsignit", auth_type="api_key", name="Letsignit",
                status="connected", api_key_enc=encrypt_value("lsk"),
            )
            others = Integration(
                user_id=world["other"].id, provider="letsignit", auth_type="api_key", name="Theirs",
                status="connected", api_key_enc=encrypt_value("lsk2"),
            )
            db.add_all([letsignit, others])
            await db.commit()
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = world["owner"]
            catalog = (await client.get("/api/v1/agent-accounts/catalog")).json()
            assert "letsignit" in catalog["signature_providers"]
            outlook = next(p for p in catalog["providers"] if p["key"] == "microsoft_outlook")
            teams_info = next(p for p in catalog["providers"] if p["key"] == "microsoft_teams")
            assert outlook["capabilities"]["signature"] is True and teams_info["capabilities"]["signature"] is False

            created = await client.post(
                "/api/v1/agent-accounts",
                json=_create_body(
                    world, name="Mailbox", provider="microsoft_outlook", mode="ai_user",
                    signature_source="integration", signature_integration_id=str(letsignit.id),
                ),
            )
            assert created.status_code == 201, created.text
            served = created.json()
            assert served["signature_source"] == "integration"
            assert served["signature_integration_id"] == str(letsignit.id)
            assert served["signature_integration_name"] == "Letsignit"
            path = f"/api/v1/agent-accounts/{served['id']}"

            refused = {
                "not visible": {"signature_integration_id": str(others.id)},
                "not a signature integration": {"signature_integration_id": str(world["slack"].id)},
                "none picked": {"signature_source": "integration", "signature_integration_id": ""},
            }
            for why, body in refused.items():
                response = await client.put(path, json=body)
                assert response.status_code == 400, why
            assert "can see" in (await client.put(path, json=refused["not visible"])).json()["detail"]
            assert "does not hold email signatures" in (
                await client.put(path, json=refused["not a signature integration"])
            ).json()["detail"]

            unchanged = (await client.put(path, json={"name": "Mailbox"})).json()
            assert unchanged["signature_source"] == "integration" and unchanged["signature_integration_name"] == "Letsignit"

            server = (await client.put(path, json={"signature_source": "none"})).json()
            assert server["signature_source"] == "none" and server["signature_integration_id"] is None

            teams = (await client.post("/api/v1/agent-accounts", json=_create_body(world, name="Teams"))).json()
            assert teams["signature_source"] == "own"
            chat = await client.put(f"/api/v1/agent-accounts/{teams['id']}", json={"signature_source": "none"})
            assert chat.status_code == 400 and "carry no signature" in chat.json()["detail"]
