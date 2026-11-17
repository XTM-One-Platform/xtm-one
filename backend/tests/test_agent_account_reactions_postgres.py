"""Reactions on an agent account's answers as ratings: a fake provider, real PostgreSQL.

A reaction is read back from the platform, never taken from a notification;
it becomes an ``annotations`` row of a platform rater (a keyed digest, no
user), follows every change (switched, removed, both signs) and goes to the
agent that wrote the answer. Nothing is read for a change that is not to an
answer, nor once the setting is off or the window closed. The ratings reach
the agent's feedback through the real routers, without ever showing the rater.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.deps import get_current_user
from app.api.v1 import agent_accounts as accounts_api
from app.api.v1 import annotations as annotations_api
from app.core.database import get_db
from app.core.security import encrypt_value
from app.models.agent import Agent
from app.models.agent_account import AgentAccount, AgentAccountReply, AgentAccountSubscription
from app.models.annotation import Annotation
from app.models.integration import Integration
from app.models.user import User
from app.services import persona_improvement
from app.services.agent_accounts import listening, management, reactions, state
from app.services.agent_accounts import providers as provider_registry
from app.services.agent_accounts.providers import (
    AccountProvider,
    CreatedSubscription,
    DesiredSubscription,
    InboundNotification,
    ProviderCapabilities,
    ProviderError,
    ProviderSession,
    UpdatedMessage,
)
from app.services.reaction_feedback import PlatformReaction, rater_ref
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

UP = "\U0001f44d"
DOWN = "\U0001f44e"
HEART = "❤️"


class ReactingProvider(AccountProvider):
    key = "fake"
    label = "Fake Chat"
    integration_provider = "microsoft"
    platform = "teams"
    capabilities = ProviderCapabilities(reactions=True)

    def __init__(self) -> None:
        self.reactions: list[PlatformReaction] | None = []
        self.reads: list[tuple[dict, str]] = []
        self.pushed = False
        self.created: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        #: The change types a subscription is refused with, and how.
        self.refuse: dict[str, ProviderError] = {}

    async def identify(self, session):  # pragma: no cover - not reached
        raise NotImplementedError

    async def fetch_context(self, session, event, limit):  # pragma: no cover - not reached
        return []

    async def account_messages_since(self, session, event):  # pragma: no cover - not reached
        return []

    async def send_reply(self, session, route, text):  # pragma: no cover - not reached
        raise NotImplementedError

    def reactions_pushed(self, route, subscriptions):
        return self.pushed

    async def read_reactions(self, session, route, message_id):
        self.reads.append((route, message_id))
        return None if self.reactions is None else list(self.reactions)

    async def create_subscription(self, session, resource, *, notification_url, lifecycle_url, client_state,
                                  change_type="created"):
        self.created.append((resource, change_type))
        if change_type in self.refuse:
            raise self.refuse[change_type]
        return CreatedSubscription(f"sub-{len(self.created)}", datetime.now(UTC) + timedelta(days=2))

    def updated_message(self, notification):
        if notification.payload.get("changeType") != "updated":
            return None
        return UpdatedMessage(notification.payload["conversation"], notification.payload["message"])

    async def delete_subscription(self, session, external_id):
        self.deleted.append(external_id)
        return True


class MemoryChecks:
    """The reaction state of ``state.py``, in memory."""

    def __init__(self, monkeypatch) -> None:
        self.scheduled: dict[str, float] = {}
        self.refused: dict[tuple[str, str], str] = {}

        async def claim_reaction_refresh(account_id, conversation_id, message_id):
            return True

        async def release_reaction_refresh(account_id, conversation_id, message_id):
            return None

        async def schedule_reaction_check(reply_id, due):
            self.scheduled[reply_id] = due

        async def schedule_reaction_checks(due_by_reply):
            self.scheduled.update(due_by_reply)

        async def note_reactions_refused(account_id, resource, detail):
            self.refused[(account_id, resource)] = detail

        async def reactions_refused(account_id, resource):
            return self.refused.get((account_id, resource))

        async def due_reaction_checks(limit):
            due = [reply_id for reply_id, at in self.scheduled.items() if at <= time.time()][:limit]
            for reply_id in due:
                del self.scheduled[reply_id]
            return due

        for fn in (
            claim_reaction_refresh, release_reaction_refresh, schedule_reaction_check, schedule_reaction_checks,
            due_reaction_checks, note_reactions_refused, reactions_refused,
        ):
            monkeypatch.setattr(state, fn.__name__, fn)
        monkeypatch.setattr(reactions, "COALESCE_WAIT_SECONDS", 0)


@pytest.fixture
def harness(monkeypatch):
    provider = ReactingProvider()
    monkeypatch.setattr(provider_registry, "_PROVIDERS", {"fake": provider})

    async def _session(db, account, provider_):
        return ProviderSession(token="t", account_external_id=account.account_external_id)

    monkeypatch.setattr(reactions, "open_session", _session)
    return provider, MemoryChecks(monkeypatch)


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


async def _user(db, name: str, role: str = "user") -> User:
    user = User(email=f"{name}-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name=name, role=role)
    db.add(user)
    await db.flush()
    return user


async def _account(db, mode: str = "ai_user", owner: User | None = None, **fields) -> AgentAccount:
    owner = owner or await _user(db, "Sam")
    integration = Integration(
        user_id=owner.id, provider="microsoft", auth_type="oauth", name="ms", status="connected",
        access_token_enc=encrypt_value("t"),
    )
    agent = Agent(name="Helper", slug=f"h-{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db.add_all([integration, agent])
    await db.flush()
    fields.setdefault("account_external_id", "me")
    account = AgentAccount(
        user_id=owner.id, name="Sam's Teams", provider="fake", integration_id=integration.id, agent_id=agent.id,
        mode=mode, **fields,
    )
    db.add(account)
    await db.commit()
    return account


async def _reply(db, account: AgentAccount, *, message_id: str = "m1", status: str = "sent",
                 age: timedelta = timedelta(minutes=1), route: dict | None = None) -> AgentAccountReply:
    reply = AgentAccountReply(
        account_id=account.id, user_id=account.user_id, agent_id=account.agent_id, status=status,
        conversation_type="direct", conversation_id="chat-1", thread_id="chat-1", message_id="in-1",
        inbound_text="Can you share the deck?", draft_text="Here it is.", sent_text="Here it is.",
        sent_message_id=message_id if status == "sent" else None,
        route=route or {"kind": "chat", "chat_id": "chat-1"},
        created_at=datetime.now(UTC) - age,
    )
    db.add(reply)
    await db.commit()
    return reply


async def _ratings(scratch, reply: AgentAccountReply) -> list[tuple[Any, ...]]:
    async with scratch.sessions() as db:
        rows = (
            await db.execute(
                select(Annotation).where(
                    Annotation.target_type == "agent_account_reply", Annotation.target_id == reply.id
                )
            )
        ).scalars().all()
        return sorted((r.rater_ref, r.user_id, r.rating, r.emoji, r.source, r.agent_id) for r in rows)


def _ref(user: str) -> str:
    return rater_ref("fake", user)


async def test_reactions_on_an_answer_become_ratings_and_follow_every_change(harness):
    provider, _checks = harness
    async with _scratch("reactions_follow") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db)
            reply = await _reply(db, account)
        updated = UpdatedMessage("chat-1", "m1")
        provider.reactions = [
            PlatformReaction("alice", UP),
            PlatformReaction("bob", DOWN),
            PlatformReaction("me", UP),  # the AI user itself
            PlatformReaction("carol", HEART),  # not mapped
        ]
        await reactions.on_updated_message(str(account.id), updated)
        agent = account.agent_id
        assert await _ratings(scratch, reply) == sorted([
            (_ref("alice"), None, "positive", UP, "teams_agent_account", agent),
            (_ref("bob"), None, "negative", DOWN, "teams_agent_account", agent),
        ])
        assert provider.reads == [({"kind": "chat", "chat_id": "chat-1"}, "m1")]

        # Alice removes hers, Bob now holds both signs, Carol switches to a thumbs down.
        provider.reactions = [
            PlatformReaction("bob", DOWN),
            PlatformReaction("bob", UP),
            PlatformReaction("carol", DOWN),
        ]
        await reactions.on_updated_message(str(account.id), updated)
        assert await _ratings(scratch, reply) == [
            (_ref("carol"), None, "negative", DOWN, "teams_agent_account", agent),
        ]

        # The same state read again changes nothing; every reaction gone retracts everything.
        await reactions.on_updated_message(str(account.id), updated)
        assert len(await _ratings(scratch, reply)) == 1
        provider.reactions = []
        await reactions.on_updated_message(str(account.id), updated)
        assert await _ratings(scratch, reply) == []


async def test_a_delegates_owner_rates_like_anyone_and_the_setting_decides_what_counts(harness):
    provider, _checks = harness
    async with _scratch("reactions_delegate") as scratch:
        async with scratch.sessions() as db:
            account = await _account(
                db, "on_behalf", reaction_feedback={"enabled": True, "positive": [HEART], "negative": ["\U0001f621"]}
            )
            reply = await _reply(db, account)
        provider.reactions = [PlatformReaction("me", HEART), PlatformReaction("alice", UP)]
        await reactions.on_updated_message(str(account.id), UpdatedMessage("chat-1", "m1"))
        assert await _ratings(scratch, reply) == [
            (_ref("me"), None, "positive", "❤", "teams_agent_account", account.agent_id),
        ]


async def test_nothing_is_read_for_a_change_that_rates_nothing(harness):
    provider, _checks = harness
    async with _scratch("reactions_nothing") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db)
            off = await _account(
                db,
                account_external_id="me-too",
                reaction_feedback={"enabled": False, "positive": [UP], "negative": [DOWN]},
            )
            await _reply(db, account, message_id="m1")
            await _reply(db, account, message_id="old", age=timedelta(days=15))
            pending = await _reply(db, account, status="pending")
            await _reply(db, off, message_id="m1")
        provider.reactions = [PlatformReaction("alice", UP)]
        # Somebody else's message, an answer of another conversation, a reply never posted.
        await reactions.on_updated_message(str(account.id), UpdatedMessage("chat-1", "someone-elses"))
        await reactions.on_updated_message(str(account.id), UpdatedMessage("chat-2", "m1"))
        await reactions.on_updated_message(str(account.id), UpdatedMessage("chat-1", str(pending.id)))
        # The setting off; an answer past the window.
        await reactions.on_updated_message(str(off.id), UpdatedMessage("chat-1", "m1"))
        await reactions.on_updated_message(str(account.id), UpdatedMessage("chat-1", "old"))
        assert provider.reads == []
        async with scratch.sessions() as db:
            assert (await db.execute(select(Annotation))).scalars().all() == []


async def test_the_rating_goes_to_the_agent_that_wrote_the_answer(harness):
    provider, _checks = harness
    async with _scratch("reactions_agent") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db)
            reply = await _reply(db, account)
            writer = account.agent_id
            successor = Agent(name="Successor", slug=f"s-{uuid.uuid4().hex[:8]}", user_id=account.user_id)
            db.add(successor)
            await db.flush()
            stored = await db.get(AgentAccount, account.id)
            stored.agent_id = successor.id
            await db.commit()
        provider.reactions = [PlatformReaction("alice", UP)]
        await reactions.on_updated_message(str(account.id), UpdatedMessage("chat-1", "m1"))
        (rating,) = await _ratings(scratch, reply)
        assert rating[-1] == writer


async def test_an_answer_is_checked_until_its_window_closes_slowly_while_push_hears_it(harness):
    provider, checks = harness
    async with _scratch("reactions_checks") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db)
            reply = await _reply(db, account, age=timedelta(minutes=30))
            old = await _reply(db, account, message_id="old", age=timedelta(days=15))
        provider.reactions = [PlatformReaction("alice", UP)]
        checks.scheduled[str(reply.id)] = time.time() - 1
        assert await reactions.run_due_checks() == 1
        assert len(await _ratings(scratch, reply)) == 1
        # Rescheduled five minutes on (the first hour's pace).
        assert 290 < checks.scheduled[str(reply.id)] - time.time() <= 300

        # Heard through push: still read, every six hours - push may stop
        # hearing it (a subscription removed, or recreated for mentions only).
        provider.pushed = True
        provider.reactions = [PlatformReaction("alice", UP), PlatformReaction("bob", DOWN)]
        checks.scheduled[str(reply.id)] = time.time() - 1
        assert await reactions.run_due_checks() == 1
        assert len(provider.reads) == 2 and len(await _ratings(scratch, reply)) == 2
        assert 6 * 3600 - 10 < checks.scheduled[str(reply.id)] - time.time() <= 6 * 3600

        # Push lost: back to the pace of the answer's age.
        provider.pushed = False
        checks.scheduled[str(reply.id)] = time.time() - 1
        assert await reactions.run_due_checks() == 1
        assert 290 < checks.scheduled[str(reply.id)] - time.time() <= 300

        # Past the window: dropped, never read.
        checks.scheduled = {str(old.id): time.time() - 1}
        assert await reactions.run_due_checks() == 0
        assert len(provider.reads) == 3 and checks.scheduled == {}


async def test_a_subscription_whose_change_types_changed_is_recreated(harness):
    provider, _checks = harness
    async with _scratch("reactions_resubscribe") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db)
            db.add(
                AgentAccountSubscription(
                    account_id=account.id, external_id="old-sub", resource="/users/me/chats/getAllMessages",
                    client_state_enc=encrypt_value("s"), expires_at=datetime.now(UTC) + timedelta(days=2),
                )
            )
            await db.commit()
        async with scratch.sessions() as db:
            stored = await db.get(AgentAccount, account.id)
            wanted = [DesiredSubscription("/users/me/chats/getAllMessages", "created,updated")]
            active, errors = await listening._sync_subscriptions(
                db, stored, provider, ProviderSession(token="t"), wanted
            )
            await db.commit()
        assert errors == [] and active == {"/users/me/chats/getAllMessages"}
        assert provider.deleted == ["old-sub"]
        assert provider.created == [("/users/me/chats/getAllMessages", "created,updated")]
        async with scratch.sessions() as db:
            subs = (await db.execute(select(AgentAccountSubscription))).scalars().all()
            assert [(s.external_id, s.change_type) for s in subs] == [("sub-1", "created,updated")]


async def test_a_subscription_refused_with_its_changes_still_hears_the_new_messages(harness):
    """The reaction variant refused, the subscription falls back to ``created`` and says so."""
    provider, checks = harness
    resource = "/users/me/chats/getAllMessages"
    async with _scratch("reactions_fallback") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db)
            db.add(
                AgentAccountSubscription(
                    account_id=account.id, external_id="old-sub", resource=resource, change_type="created",
                    client_state_enc=encrypt_value("s"), expires_at=datetime.now(UTC) + timedelta(days=2),
                )
            )
            await db.commit()
        provider.refuse["created,updated"] = ProviderError("Graph refused the change type.", kind="refused")
        wanted = [DesiredSubscription(resource, "created,updated", covers="chats", fallback_change_type="created")]

        async def _sync() -> tuple[set[str], list]:
            async with scratch.sessions() as db:
                stored = await db.get(AgentAccount, account.id)
                result = await listening._sync_subscriptions(db, stored, provider, ProviderSession(token="t"), wanted)
                await db.commit()
                return result

        active, problems = await _sync()
        assert active == {resource}
        assert [(p.kind, p.detail, p.covers) for p in problems] == [
            ("reactions_refused", "Graph refused the change type.", "chats"),
        ]
        assert provider.deleted == ["old-sub"]
        assert provider.created == [(resource, "created,updated"), (resource, "created")]
        assert checks.refused == {(str(account.id), resource): "Graph refused the change type."}
        async with scratch.sessions() as db:
            subs = (await db.execute(select(AgentAccountSubscription))).scalars().all()
            assert [(s.external_id, s.change_type) for s in subs] == [("sub-2", "created")]

        # The next sync keeps the fallback rather than deleting it to be refused again.
        active, problems = await _sync()
        assert active == {resource}
        assert [(p.kind, p.detail) for p in problems] == [("reactions_refused", "Graph refused the change type.")]
        assert provider.deleted == ["old-sub"] and len(provider.created) == 2

        # Refused for the new messages too: an ordinary refusal, nothing recorded as a fallback.
        checks.refused.clear()
        provider.refuse["created"] = ProviderError("Forbidden.", kind="refused")
        async with scratch.sessions() as db:
            await db.execute(delete(AgentAccountSubscription))
            await db.commit()
        active, problems = await _sync()
        assert active == set()
        assert [p.kind for p in problems] == ["refused"]
        assert checks.refused == {}


async def test_a_throttled_subscription_is_not_retried_without_its_changes(harness):
    provider, checks = harness
    resource = "/users/me/chats/getAllMessages"
    async with _scratch("reactions_fallback_throttled") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db)
        provider.refuse["created,updated"] = ProviderError("Slow down.", kind="throttled")
        wanted = [DesiredSubscription(resource, "created,updated", covers="chats", fallback_change_type="created")]
        async with scratch.sessions() as db:
            stored = await db.get(AgentAccount, account.id)
            active, problems = await listening._sync_subscriptions(
                db, stored, provider, ProviderSession(token="t"), wanted
            )
        assert active == set() and [p.kind for p in problems] == ["throttled"]
        assert provider.created == [(resource, "created,updated")] and checks.refused == {}


def _entry(sub: str, secret: str, change_type: str | None, *, conversation: str = "chat-1", message: str = "m1",
           lifecycle: str | None = None) -> InboundNotification:
    return InboundNotification(
        sub, secret, lifecycle, {"changeType": change_type, "conversation": conversation, "message": message}
    )


async def test_only_the_changes_to_an_answer_are_accepted(harness, monkeypatch):
    """The noise of a ``created,updated`` subscription is dropped by the endpoint, in its one session."""
    provider, _checks = harness
    async with _scratch("reactions_accept") as scratch:
        monkeypatch.setattr(listening, "async_session", scratch.sessions)
        async with scratch.sessions() as db:
            account = await _account(db)
            other = await _account(db, account_external_id="someone")
            await _reply(db, account, message_id="m1")
            await _reply(db, account, message_id="m2", status="pending")
            await _reply(db, other, message_id="m3")
            for owner, sub in ((account, "sub-a"), (other, "sub-b")):
                db.add(
                    AgentAccountSubscription(
                        account_id=owner.id, external_id=sub, resource=f"/{sub}", change_type="created,updated",
                        client_state_enc=encrypt_value("secret"), expires_at=datetime.now(UTC) + timedelta(days=2),
                    )
                )
            await db.commit()
        answer = _entry("sub-a", "secret", "updated")
        new_message = _entry("sub-a", "secret", "created", message="m9")
        lifecycle = _entry("sub-a", "secret", "updated", message="m9", lifecycle="subscriptionRemoved")
        entries = [
            answer,
            _entry("sub-a", "secret", "updated", message="m9"),  # someone else's message
            _entry("sub-a", "secret", "updated", conversation="chat-2"),  # the answer's id, another conversation
            _entry("sub-a", "secret", "updated", message="m2"),  # a reply never posted
            _entry("sub-a", "secret", "updated", message="m3"),  # another account's answer
            _entry("sub-a", "wrong", "updated"),  # an entry that does not echo the clientState
            new_message,
            lifecycle,
        ]
        accepted = await listening.accept_notifications(provider, entries)
        assert accepted == [(str(account.id), answer), (str(account.id), new_message), (str(account.id), lifecycle)]
        # The other account's answer is accepted through its own subscription.
        theirs = _entry("sub-b", "secret", "updated", message="m3")
        assert await listening.accept_notifications(provider, [theirs]) == [(str(other.id), theirs)]


# ---------------------------------------------------------------------------
# Through the routers
# ---------------------------------------------------------------------------


class Caller:
    def __init__(self) -> None:
        self.user: User | None = None


@asynccontextmanager
async def _client(session_factory, caller: Caller) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(accounts_api.router, prefix="/api/v1/agent-accounts")
    app.include_router(annotations_api.router, prefix="/api/v1/annotations")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: caller.user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
def no_sync(monkeypatch):
    monkeypatch.setattr(management, "schedule_sync", lambda account_id, *, poll=False: None)


@pytest.mark.parametrize(
    "setting",
    [
        pytest.param({"positive": [UP], "negative": [UP]}, id="both-signs"),
        pytest.param({"positive": ["yes"]}, id="not-an-emoji"),
        pytest.param({"positive": None, "enabled": "sometimes"}, id="not-a-bool"),
        pytest.param({"negative": [DOWN] * 3 + [chr(0x1F600 + i) for i in range(10)]}, id="too-many"),
    ],
)
async def test_an_invalid_reaction_setting_is_a_422(harness, no_sync, setting):
    async with _scratch("reactions_setting_invalid") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db)
            owner = await db.get(User, account.user_id)
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = owner
            response = await client.put(f"/api/v1/agent-accounts/{account.id}", json={"reaction_feedback": setting})
            assert response.status_code == 422


async def test_the_reaction_setting_is_stored_normalized_and_read_with_its_defaults(harness, no_sync):
    async with _scratch("reactions_setting") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db)
            owner = await db.get(User, account.user_id)
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = owner
            path = f"/api/v1/agent-accounts/{account.id}"
            assert (await client.get(path)).json()["reaction_feedback"] == {
                "enabled": True, "positive": [UP], "negative": [DOWN],
            }
            body = {"reaction_feedback": {"enabled": True, "positive": [UP + "\U0001f3fd", HEART], "negative": [DOWN]}}
            response = await client.put(path, json=body)
            assert response.status_code == 200, response.text
            assert response.json()["reaction_feedback"] == {
                "enabled": True, "positive": [UP, "❤"], "negative": [DOWN],
            }
            # Another field saved later leaves the setting as it was.
            await client.put(path, json={"name": "Renamed"})
            assert (await client.get(path)).json()["reaction_feedback"]["positive"] == [UP, "❤"]


async def test_reaction_ratings_reach_the_agents_feedback_without_the_rater(harness):
    provider, _checks = harness
    async with _scratch("reactions_feedback_api") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db)
            owner = await db.get(User, account.user_id)
            other = await _user(db, "Other")
            admin = await _user(db, "Admin", role="admin")
            await db.commit()
            reply = await _reply(db, account)
        provider.reactions = [PlatformReaction("alice", UP), PlatformReaction("bob", UP), PlatformReaction("eve", DOWN)]
        await reactions.on_updated_message(str(account.id), UpdatedMessage("chat-1", "m1"))
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = owner
            replies = (await client.get("/api/v1/agent-accounts/replies")).json()
            assert [(r["rating_positive"], r["rating_negative"]) for r in replies] == [(2, 1)]

            listed = (await client.get("/api/v1/annotations", params={"agent_id": str(account.agent_id)})).json()
            assert listed["total"] == 3
            assert sorted((i["target_type"], i["rating"], i["emoji"], i["source"]) for i in listed["items"]) == sorted([
                ("agent_account_reply", "positive", UP, "teams_agent_account"),
                ("agent_account_reply", "positive", UP, "teams_agent_account"),
                ("agent_account_reply", "negative", DOWN, "teams_agent_account"),
            ])
            assert {(i["user_name"], i["user_email"], i["can_open"]) for i in listed["items"]} == {(None, None, False)}
            assert "rater_ref" not in listed["items"][0]
            refs = {_ref("alice"), _ref("bob"), _ref("eve")}
            assert not any(ref in str(listed) for ref in refs)

            summary = (await client.get("/api/v1/annotations/summary", params={"agent_id": str(account.agent_id)})).json()
            by_type = {t["target_type"]: (t["positive"], t["negative"]) for t in summary["by_target_type"]}
            assert by_type["agent_account_reply"] == (2, 1) and summary["total"] == 3

            # The owner may rate the answer too, as themselves: a web rating beside the reactions.
            target = f"/api/v1/annotations/agent_account_reply/{reply.id}"
            rated = await client.post(target, json={"rating": "negative"})
            assert rated.status_code == 200, rated.text
            stats = (await client.get(f"{target}/stats")).json()
            assert (stats["positive"], stats["negative"]) == (2, 2)

            caller.user = other
            assert (await client.get(target)).status_code == 404
            assert (await client.post(target, json={"rating": "positive"})).status_code == 404
            assert (await client.get(f"{target}/stats")).status_code == 404
            assert (await client.get("/api/v1/agent-accounts/replies")).json() == []

            caller.user = admin
            assert (await client.get(f"{target}/stats")).status_code == 200
        async with scratch.sessions() as db:
            web = (
                await db.execute(select(Annotation).where(Annotation.user_id == owner.id))
            ).scalar_one()
            assert (web.source, web.rater_ref, web.emoji) == ("web", None, None)

            # Persona Improvement reads the users' ratings and the reactions apart.
            agent = await db.get(Agent, account.agent_id)
            evidence = await persona_improvement.gather_evidence(
                db, agent, SimpleNamespace(id=uuid.uuid4(), score=None), "score"
            )
            assert evidence["ratings"]["agent_account_reply"] == {"positive": 0, "negative": 1}
            assert evidence["reaction_ratings"] == {"agent_account_reply": {"positive": 2, "negative": 1}}


async def test_switching_reactions_off_keeps_the_ratings_and_on_again_reconciles_them(harness, no_sync):
    """Off drops the checks and keeps what was rated; on again, or another mapping, reads every
    answer still in its window at once, under the setting."""
    provider, checks = harness
    async with _scratch("reactions_resume") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db)
            owner = await db.get(User, account.user_id)
            reply = await _reply(db, account)
            approved = await _reply(db, account, message_id="m2", age=timedelta(days=15))
            old = await _reply(db, account, message_id="old", age=timedelta(days=15))
            await _reply(db, account, status="pending")
            stored = await db.get(AgentAccountReply, approved.id)
            stored.decided_at = datetime.now(UTC) - timedelta(days=13)  # posted when approved
            await db.commit()
        provider.reactions = [PlatformReaction("alice", UP), PlatformReaction("bob", HEART)]
        await reactions.on_updated_message(str(account.id), UpdatedMessage("chat-1", "m1"))
        assert [r[2:4] for r in await _ratings(scratch, reply)] == [("positive", UP)]

        caller = Caller()
        path = f"/api/v1/agent-accounts/{account.id}"
        async with _client(scratch.sessions, caller) as client:
            caller.user = owner
            off = {"enabled": False, "positive": [UP], "negative": [DOWN]}
            assert (await client.put(path, json={"reaction_feedback": off})).status_code == 200
            assert checks.scheduled == {}
            # Off: a due check drops the answer, nothing read, the rating kept.
            checks.scheduled[str(reply.id)] = time.time() - 1
            assert await reactions.run_due_checks() == 0
            assert checks.scheduled == {} and len(provider.reads) == 1
            assert [r[2:4] for r in await _ratings(scratch, reply)] == [("positive", UP)]

            # On again, a heart now the positive emoji: every answer in its window is due now.
            on = {"enabled": True, "positive": [HEART], "negative": [DOWN]}
            before = time.time()
            assert (await client.put(path, json={"reaction_feedback": on})).status_code == 200
            assert set(checks.scheduled) == {str(reply.id), str(approved.id)}
            assert all(before <= due <= time.time() for due in checks.scheduled.values())

            # The same setting saved again resumes nothing.
            checks.scheduled.clear()
            assert (await client.put(path, json={"reaction_feedback": on, "name": "Renamed"})).status_code == 200
            assert checks.scheduled == {}

            # The account switched off, then on: resumed too.
            assert (await client.put(path, json={"enabled": False})).status_code == 200
            assert checks.scheduled == {}
            assert (await client.put(path, json={"enabled": True})).status_code == 200
            assert set(checks.scheduled) == {str(reply.id), str(approved.id)}
            assert str(old.id) not in checks.scheduled

        checks.scheduled = {str(reply.id): time.time() - 1}
        assert await reactions.run_due_checks() == 1
        # Alice's thumbs up no longer counts; Bob's heart does.
        assert [r[:4] for r in await _ratings(scratch, reply)] == [(_ref("bob"), None, "positive", "❤")]
