"""An agent account's activity log and analytics, on real PostgreSQL.

Every message the account heard is one row - a grace period updates it - linked
to the reply it produced, whose status is what the log shows; the log is
filtered and paged on the server, the analytics are computed in SQL over a
period, rows past the retention are purged, and only the account's owner (or
an administrator) reads any of it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.deps import get_current_user
from app.api.v1 import agent_accounts as api
from app.core.database import get_db
from app.core.security import encrypt_value
from app.models.agent import Agent
from app.models.agent_account import AgentAccount, AgentAccountDecision, AgentAccountReply
from app.models.annotation import Annotation
from app.models.integration import Integration
from app.models.user import User
from app.services.agent_accounts import activity, management
from app.services.agent_accounts.events import AccountEvent
from app.services.agent_accounts.rules import Decision
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


@pytest.fixture
def sessions(monkeypatch):
    """Points the log's own sessions at the scratch database of the test."""
    holder = {}

    def _use(scratch):
        monkeypatch.setattr(activity, "async_session", scratch.sessions)
        holder["scratch"] = scratch
        return scratch

    return _use


async def _owner_and_account(db, name: str = "Sam") -> tuple[User, AgentAccount]:
    owner = User(email=f"{name.lower()}-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name=name)
    db.add(owner)
    await db.flush()
    integration = Integration(
        user_id=owner.id, provider="microsoft", auth_type="oauth", name="ms", status="connected",
        access_token_enc=encrypt_value("t"),
    )
    agent = Agent(name="Helper", slug=f"h-{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db.add_all([integration, agent])
    await db.flush()
    account = AgentAccount(
        user_id=owner.id, name="My digital twin", provider="microsoft_teams", integration_id=integration.id,
        agent_id=agent.id, mode="on_behalf", rules=[], account_external_id=f"me-{uuid.uuid4().hex[:6]}",
    )
    db.add(account)
    await db.commit()
    return owner, account


def _event(message_id: str, **overrides) -> AccountEvent:
    values = {
        "provider": "microsoft_teams",
        "conversation_type": "direct",
        "conversation_id": "chat-alice",
        "thread_id": "chat-alice",
        "message_id": message_id,
        "sender_id": "alice-id",
        "sender_name": "Alice",
        "text": "Can you share the board deck?",
        "created_at": datetime.now(UTC) - timedelta(minutes=5),
        "mentions_account": False,
    }
    values.update(overrides)
    return AccountEvent(**values)


_ANSWER = Decision(True, "respond", rule_id="direct", rule_name="Direct messages", addressed=True)
_NOT_ADDRESSED = Decision(False, "not_addressed", rule_id="mentions", rule_name="Mentions")
_APP = Decision(False, "application_author")


async def _reply(db, account: AgentAccount, status: str, **fields) -> AgentAccountReply:
    reply = AgentAccountReply(
        account_id=account.id, user_id=account.user_id, status=status, conversation_type="direct",
        conversation_id="chat-alice", thread_id="chat-alice", message_id=f"m-{uuid.uuid4().hex[:6]}",
        sender_name="Alice", inbound_text="Can you share the board deck?", draft_text="Here it is.", **fields,
    )
    db.add(reply)
    await db.commit()
    return reply


async def _rows(scratch, account) -> list[AgentAccountDecision]:
    async with scratch.sessions() as db:
        return list(
            (
                await db.execute(select(AgentAccountDecision).where(AgentAccountDecision.account_id == account.id))
            ).scalars()
        )


async def test_a_message_is_one_row_its_grace_period_updates(sessions):
    async with _scratch("acct_activity_upsert") as scratch:
        sessions(scratch)
        async with scratch.sessions() as db:
            _owner, account = await _owner_and_account(db)
            reply = await _reply(db, account, "sent")
        await activity.record_decision(str(account.id), _event("m1"), _ANSWER, "waiting")
        (row,) = await _rows(scratch, account)
        assert (row.outcome, row.waited, row.reply_id) == ("waiting", True, None)
        await activity.record_decision(str(account.id), _event("m1"), _ANSWER, "sent", reply_id=reply.id)
        (row,) = await _rows(scratch, account)
        assert (row.outcome, row.waited, row.reply_id, row.rule_name) == ("sent", True, reply.id, "Direct messages")
        assert row.addressed and row.respond and row.preview == "Can you share the board deck?"
        # The same message id in another conversation is another message.
        await activity.record_decision(str(account.id), _event("m1", conversation_id="chat-bob"), _APP, "application_author")
        assert len(await _rows(scratch, account)) == 2


async def test_an_email_handed_to_the_owner_is_its_own_group_with_the_agents_note(sessions):
    """A mailbox's triage: the note is kept, searched, and survives a later outcome without one."""
    async with _scratch("acct_activity_flagged") as scratch:
        sessions(scratch)
        async with scratch.sessions() as db:
            _owner, account = await _owner_and_account(db)
        account_id = str(account.id)
        await activity.record_decision(account_id, _event("m1"), _ANSWER, "waiting")
        await activity.record_decision(account_id, _event("m1"), _ANSWER, "flagged", note="A pricing decision.")
        await activity.record_decision(account_id, _event("m2"), _ANSWER, "silent", note="Just an FYI.")
        async with scratch.sessions() as db:
            handed, total = await activity.list_decisions(
                db, account.id, activity.DecisionFilters(group="needs_owner"), limit=10, offset=0
            )
            searched, _ = await activity.list_decisions(
                db, account.id, activity.DecisionFilters(text="pricing"), limit=10, offset=0
            )
            stats = await activity.activity_stats(db, account.id, period="24h", tz="UTC")
        assert total == 1 and handed[0]["group"] == "needs_owner" and handed[0]["decision"].note == "A pricing decision."
        assert [entry["decision"].message_id for entry in searched] == ["m1"]
        assert stats["groups"]["needs_owner"] == 1 and stats["groups"]["silent"] == 1
        await activity.record_decision(account_id, _event("m1"), _ANSWER, "flagged")
        (row,) = [r for r in await _rows(scratch, account) if r.message_id == "m1"]
        assert row.note == "A pricing decision."


async def test_a_mail_another_pipeline_took_is_handled_elsewhere_with_its_holder(sessions):
    """``already_handled`` is its own group, filtered and counted, the holder kept as data (no note)."""
    async with _scratch("acct_activity_handled") as scratch:
        sessions(scratch)
        async with scratch.sessions() as db:
            _owner, account = await _owner_and_account(db)
        account_id = str(account.id)
        holder = "agent_account:f28538b0-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
        await activity.record_decision(account_id, _event("m1"), _ANSWER, "already_handled", holder=holder)
        await activity.record_decision(account_id, _event("m2"), _APP, "application_author")
        async with scratch.sessions() as db:
            handled, total = await activity.list_decisions(
                db, account.id, activity.DecisionFilters(group="already_handled"), limit=10, offset=0
            )
            left, _ = await activity.list_decisions(
                db, account.id, activity.DecisionFilters(group="left"), limit=10, offset=0
            )
            stats = await activity.activity_stats(db, account.id, period="24h", tz="UTC")
        assert total == 1 and handled[0]["group"] == "already_handled"
        assert handled[0]["decision"].holder == holder and handled[0]["decision"].note is None
        assert [entry["decision"].message_id for entry in left] == ["m2"]
        assert stats["groups"]["already_handled"] == 1 and stats["groups"]["left"] == 1
        assert "already_handled" not in stats["left_reasons"]


async def test_recording_never_raises(monkeypatch):
    def _broken():
        raise RuntimeError("no database")

    monkeypatch.setattr(activity, "async_session", _broken)
    await activity.record_decision(str(uuid.uuid4()), _event("m1"), _ANSWER, "sent")


async def test_the_log_shows_what_a_reply_became_filtered_and_paged(sessions):
    async with _scratch("acct_activity_log") as scratch:
        sessions(scratch)
        async with scratch.sessions() as db:
            _owner, account = await _owner_and_account(db)
            drafted = await _reply(db, account, "pending")
        account_id = str(account.id)
        await activity.record_decision(account_id, _event("m1"), _ANSWER, "pending", reply_id=drafted.id)
        await activity.record_decision(
            account_id, _event("m2", conversation_type="channel", conversation_id="19:ops", conversation_name="Ops > Alerts",
                              sender_id="app", sender_name="Workflows", text="[FIRING:1] Disk full"),
            _APP, "application_author",
        )
        await activity.record_decision(account_id, _event("m3", text="lunch?"), _NOT_ADDRESSED, "not_addressed")
        await activity.record_decision(account_id, _event("m4"), _ANSWER, "silent")

        async with scratch.sessions() as db:
            items, total = await activity.list_decisions(db, account.id, activity.DecisionFilters(), limit=2, offset=0)
            assert total == 4 and [i["decision"].message_id for i in items] == ["m4", "m3"]
            items, _ = await activity.list_decisions(db, account.id, activity.DecisionFilters(), limit=2, offset=2)
            assert [i["decision"].message_id for i in items] == ["m2", "m1"]
            pending = items[1]
            assert (pending["outcome"], pending["group"], pending["reply_text"]) == ("pending", "awaiting_approval", "Here it is.")

            # Approved since: the log says sent, without a second write.
            await db.execute(update(AgentAccountReply).where(AgentAccountReply.id == drafted.id).values(status="sent"))
            await db.commit()
            (answered,), total = await activity.list_decisions(
                db, account.id, activity.DecisionFilters(group="answered"), limit=10, offset=0
            )
            assert total == 1 and answered["decision"].message_id == "m1" and answered["outcome"] == "sent"

            async def ids(**filters) -> list[str]:
                found, _ = await activity.list_decisions(
                    db, account.id, activity.DecisionFilters(**filters), limit=10, offset=0
                )
                return [i["decision"].message_id for i in found]

            assert await ids(group="left") == ["m3", "m2"]
            assert await ids(group="silent") == ["m4"]
            assert await ids(conversation_type="channel") == ["m2"]
            assert await ids(conversation_id="19:ops") == ["m2"]
            assert await ids(sender="app") == ["m2"] and await ids(sender="Workflows") == ["m2"]
            assert await ids(text="firing") == ["m2"] and await ids(text="ops >") == ["m2"]
            # A wildcard typed by the owner is a character, not a pattern.
            assert await ids(text="%") == []
            # Several groups or kinds of conversation at once: any of them (the Decisions table's filter pills).
            assert await ids(groups=("left", "silent")) == ["m4", "m3", "m2"]
            assert await ids(conversation_types=("channel", "direct")) == ["m4", "m3", "m2", "m1"]

            async def ordered(sort: str, direction: str) -> list[str]:
                found, _ = await activity.list_decisions(
                    db, account.id, activity.DecisionFilters(), limit=10, offset=0, sort=sort, direction=direction
                )
                return [i["decision"].message_id for i in found]

            # Oldest first, and by sender name with the newest first among equal names.
            assert await ordered("date", "asc") == ["m1", "m2", "m3", "m4"]
            by_sender = await ordered("sender", "asc")
            assert by_sender[-1] == "m2" and by_sender[:3] == ["m4", "m3", "m1"]
            assert (await ordered("sender", "desc"))[0] == "m2"


async def test_the_analytics_count_what_the_account_did_and_how_fast(sessions):
    async with _scratch("acct_activity_stats") as scratch:
        sessions(scratch)
        async with scratch.sessions() as db:
            _owner, account = await _owner_and_account(db)
            now = datetime.now(UTC)
            sent = await _reply(db, account, "sent", sent_text="Here it is.", created_at=now - timedelta(minutes=4))
            approved = await _reply(
                db, account, "sent", created_at=now - timedelta(minutes=30), decided_at=now - timedelta(minutes=20)
            )
            await _reply(db, account, "rejected")
            db.add_all(
                [
                    Annotation(target_type="agent_account_reply", target_id=sent.id, agent_id=account.agent_id,
                               user_id=account.user_id, rating="positive"),
                    Annotation(target_type="agent_account_reply", target_id=approved.id, agent_id=account.agent_id,
                               user_id=account.user_id, rating="negative"),
                ]
            )
            await db.commit()
        account_id = str(account.id)
        # Written 5 minutes ago, answered 4 minutes ago: one minute to answer.
        await activity.record_decision(account_id, _event("m1", created_at=now - timedelta(minutes=5)), _ANSWER, "sent", reply_id=sent.id)
        await activity.record_decision(account_id, _event("m2"), _ANSWER, "pending", reply_id=approved.id)
        await activity.record_decision(account_id, _event("m3"), _ANSWER, "silent")
        for index in range(4):
            await activity.record_decision(
                account_id,
                _event(f"app-{index}", conversation_type="channel", conversation_id="19:ops",
                       conversation_name="Ops > Alerts", sender_id="app", sender_name="Workflows"),
                _APP, "application_author",
            )
        # Outside the 24 hours, inside the 7 days; the owner's own message does
        # not name the chat, though "Zoe" sorts after "Alice".
        await activity.record_decision(account_id, _event("old"), _NOT_ADDRESSED, "not_addressed")
        await activity.record_decision(
            account_id, _event("own", sender_id="me", sender_name="Zoe Owner"), Decision(False, "own_message"), "own_message"
        )
        async with scratch.sessions() as db:
            await db.execute(
                update(AgentAccountDecision)
                .where(AgentAccountDecision.message_id.in_(["old", "own"]))
                .values(created_at=now - timedelta(days=3))
            )
            await db.commit()

            day = await activity.activity_stats(db, account.id, period="24h", tz="Europe/Paris")
            week = await activity.activity_stats(db, account.id, period="7d", tz="Europe/Paris")
            unknown_zone = await activity.activity_stats(db, account.id, period="24h", tz="Not/AZone")

    assert (day["heard"], day["for_agent"]) == (7, 3)
    assert day["groups"]["answered"] == 2 and day["groups"]["silent"] == 1 and day["groups"]["left"] == 4
    assert day["left_reasons"] == {"application_author": 4}
    assert day["by_conversation_type"]["channel"]["left"] == 4 and day["by_conversation_type"]["direct"]["answered"] == 2
    assert day["bucket"] == "hour" and len(day["timeline"]) in (24, 25)
    assert sum(sum(b["counts"].values()) for b in day["timeline"]) == 7
    (ops, alice) = day["conversations"]
    assert (ops["conversation_name"], ops["heard"], ops["answered"]) == ("Ops > Alerts", 4, 0)
    assert (alice["conversation_id"], alice["heard"], alice["for_agent"], alice["answered"]) == ("chat-alice", 3, 3, 2)
    assert (alice["conversation_name"], alice["with_name"]) == (None, "Alice")
    assert [(s["sender"], s["sender_name"], s["heard"]) for s in day["senders"]] == [
        ("app", "Workflows", 4), ("alice-id", "Alice", 3),
    ]
    (rule,) = day["rules"]
    assert (rule["rule_id"], rule["matched"], rule["answered"], rule["silent"]) == ("direct", 3, 2, 1)
    assert day["replies"] == {"sent": 2, "rejected": 1}
    assert day["median_answer_seconds"] == pytest.approx(60, abs=2)
    assert day["median_approval_seconds"] == pytest.approx(600, abs=2)
    assert day["ratings"] == {"positive": 1, "negative": 1}

    assert week["heard"] == 9 and week["bucket"] == "day" and len(week["timeline"]) in (7, 8)
    assert week["left_reasons"] == {"application_author": 4, "not_addressed": 1, "own_message": 1}
    alice_week = next(c for c in week["conversations"] if c["conversation_id"] == "chat-alice")
    assert (alice_week["heard"], alice_week["with_name"]) == (5, "Alice")
    assert unknown_zone["timezone"] == "UTC"


async def test_rows_past_the_retention_are_purged(sessions):
    async with _scratch("acct_activity_purge") as scratch:
        sessions(scratch)
        async with scratch.sessions() as db:
            _owner, account = await _owner_and_account(db)
        await activity.record_decision(str(account.id), _event("old"), _APP, "application_author")
        await activity.record_decision(str(account.id), _event("new"), _APP, "application_author")
        async with scratch.sessions() as db:
            await db.execute(
                update(AgentAccountDecision)
                .where(AgentAccountDecision.message_id == "old")
                .values(created_at=datetime.now(UTC) - timedelta(days=activity.RETENTION_DAYS + 1))
            )
            await db.commit()
            assert await activity.purge_old_decisions(db) == 1
            await db.commit()
        assert [row.message_id for row in await _rows(scratch, account)] == ["new"]


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


class Caller:
    def __init__(self) -> None:
        self.user: User | None = None


@asynccontextmanager
async def _client(session_factory, caller: Caller) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1/agent-accounts")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: caller.user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_the_owner_reads_the_log_the_replies_and_the_analytics_and_sets_the_notification(sessions, monkeypatch):
    monkeypatch.setattr(management, "schedule_sync", lambda *a, **k: None)
    async with _scratch("acct_activity_api") as scratch:
        sessions(scratch)
        async with scratch.sessions() as db:
            owner, account = await _owner_and_account(db)
            other, _theirs = await _owner_and_account(db, "Other")
            sent = await _reply(db, account, "sent", sent_text="Here it is.", conversation_name="Zoe")
            await _reply(db, account, "pending", conversation_name="alice")
            await _reply(db, account, "failed", conversation_name="Bob")
        await activity.record_decision(str(account.id), _event("m1"), _ANSWER, "sent", reply_id=sent.id)
        await activity.record_decision(str(account.id), _event("m2"), _APP, "application_author")
        base = f"/api/v1/agent-accounts/{account.id}"
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = owner
            page = (await client.get(f"{base}/decisions?limit=1")).json()
            assert page["total"] == 2 and len(page["items"]) == 1
            (latest,) = page["items"]
            assert (latest["outcome"], latest["group"], latest["sender_name"]) == ("application_author", "left", "Alice")
            answered = (await client.get(f"{base}/decisions?group=answered")).json()["items"]
            assert answered[0]["reply_text"] == "Here it is." and answered[0]["reply_status"] == "sent"

            replies = (await client.get(f"{base}/replies?status=pending")).json()
            assert replies["total"] == 1 and replies["items"][0]["status"] == "pending"
            assert (await client.get(f"{base}/replies?q=here%20it")).json()["total"] == 3
            # Several statuses keep any of them; the conversation sort ignores case.
            several = (await client.get(f"{base}/replies?status=pending&status=failed")).json()
            assert several["total"] == 2 and {item["status"] for item in several["items"]} == {"pending", "failed"}
            by_name = (await client.get(f"{base}/replies?sort=conversation&direction=asc")).json()["items"]
            assert [item["conversation_name"] for item in by_name] == ["alice", "Bob", "Zoe"]
            by_name = (await client.get(f"{base}/replies?sort=conversation&direction=desc")).json()["items"]
            assert [item["conversation_name"] for item in by_name] == ["Zoe", "Bob", "alice"]
            oldest = (await client.get(f"{base}/replies?direction=asc")).json()["items"]
            newest = (await client.get(f"{base}/replies")).json()["items"]
            assert [item["id"] for item in oldest] == [item["id"] for item in reversed(newest)]
            assert (await client.get(f"{base}/replies?sort=sender")).status_code == 422

            stats = (await client.get(f"{base}/activity?period=24h&tz=Europe/Paris")).json()
            assert stats["heard"] == 2 and stats["groups"]["answered"] == 1 and stats["timezone"] == "Europe/Paris"
            assert (await client.get(f"{base}/activity?period=1y")).status_code == 422

            # On by default; the owner switches it off.
            assert (await client.get(base)).json()["notify_on_send"] is True
            assert (await client.put(base, json={"notify_on_send": False})).json()["notify_on_send"] is False

            caller.user = other
            for path in ("decisions", "replies", "activity"):
                assert (await client.get(f"{base}/{path}")).status_code == 404
