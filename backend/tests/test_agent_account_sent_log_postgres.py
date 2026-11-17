"""Every message sent through an agent account is in its activity log, on real PostgreSQL.

A rule reply stays one ``reply`` entry linked to the message it answers; a
message the agent sends on its own is one ``proactive`` entry with its text,
recipients and origin; a failed send is a ``failed`` entry; ``kind`` keeps one
of the two in the log and in the Messages sent list.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.security import encrypt_value
from app.models.agent import Agent
from app.models.agent_account import AgentAccount, AgentAccountDecision, AgentAccountReply
from app.models.integration import Integration
from app.models.user import User
from app.services.agent_accounts import activity, sent_log
from app.services.agent_accounts.events import AccountEvent
from app.services.agent_accounts.rules import Decision
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


@pytest.fixture
def sessions(monkeypatch):
    def _use(scratch):
        monkeypatch.setattr(activity, "async_session", scratch.sessions)
        monkeypatch.setattr(sent_log, "async_session", scratch.sessions)
        return scratch

    return _use


async def _account(db) -> AgentAccount:
    owner = User(email=f"sam-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name="Sam")
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
    return account


async def _rule_reply(db, account: AgentAccount) -> None:
    reply = AgentAccountReply(
        account_id=account.id, user_id=account.user_id, status="sent", conversation_type="direct",
        conversation_id="chat-alice", thread_id="chat-alice", message_id="m-in", sender_name="Alice",
        inbound_text="Can you share the board deck?", draft_text="Here it is.", sent_text="Here it is.",
    )
    db.add(reply)
    await db.commit()
    event = AccountEvent(
        provider="microsoft_teams", conversation_type="direct", conversation_id="chat-alice", thread_id="chat-alice",
        message_id="m-in", sender_id="alice-id", sender_name="Alice", text="Can you share the board deck?",
        created_at=datetime.now(UTC) - timedelta(minutes=5), mentions_account=False,
    )
    answer = Decision(True, "respond", rule_id="direct", rule_name="Direct messages", addressed=True)
    await activity.record_decision(str(account.id), event, answer, "sent", reply_id=reply.id)


async def test_a_reply_and_a_proactive_send_are_two_kinds_of_one_log(sessions):
    async with _scratch("acct_sent_log_kinds") as scratch:
        sessions(scratch)
        async with scratch.sessions() as db:
            account = await _account(db)
            await _rule_reply(db, account)
        written = await sent_log.record_proactive_send(
            str(account.integration_id),
            provider="microsoft_teams",
            tool="send_microsoft_chat_message",
            conversation_id="chat-alice",
            text="The decision is taken: we go.",
            message_id="m-out",
            recipients=["bob@example.com"],
            origin={"source": "chat", "conversation_id": "conv-1", "agent_id": str(account.agent_id)},
        )
        assert written == 1
        async with scratch.sessions() as db:
            both, total = await activity.list_decisions(db, account.id, activity.DecisionFilters(), limit=10, offset=0)
            proactive, _ = await activity.list_decisions(
                db, account.id, activity.DecisionFilters(kind="proactive"), limit=10, offset=0
            )
            replies, _ = await activity.list_decisions(
                db, account.id, activity.DecisionFilters(kind="reply"), limit=10, offset=0
            )
        assert total == 2 and {entry["kind"] for entry in both} == {"reply", "proactive"}
        (reply_entry,) = replies
        assert reply_entry["decision"].rule_name == "Direct messages" and reply_entry["origin"] is None
        assert reply_entry["reply_text"] == "Here it is."
        (sent,) = proactive
        assert sent["outcome"] == "sent" and sent["group"] == "answered"
        assert sent["reply_text"] == "The decision is taken: we go."
        # The conversation's kind and name come from what the log already knows of it.
        assert sent["decision"].conversation_type == "direct" and sent["decision"].rule_id is None
        assert sent["origin"]["tool"] == "send_microsoft_chat_message"
        assert sent["origin"]["source"] == "chat" and sent["origin"]["recipients"] == ["bob@example.com"]


async def test_a_failed_send_is_a_failed_entry(sessions):
    async with _scratch("acct_sent_log_failed") as scratch:
        sessions(scratch)
        async with scratch.sessions() as db:
            account = await _account(db)
        await sent_log.record_proactive_send(
            str(account.integration_id),
            provider="microsoft_teams",
            tool="send_microsoft_chat_message",
            conversation_id="no-such-chat",
            text="Hello",
            error="404 Not Found",
        )
        async with scratch.sessions() as db:
            (reply,) = (
                await db.execute(select(AgentAccountReply).where(AgentAccountReply.account_id == account.id))
            ).scalars()
            (row,) = (
                await db.execute(select(AgentAccountDecision).where(AgentAccountDecision.account_id == account.id))
            ).scalars()
            failed, _ = await activity.list_decisions(
                db, account.id, activity.DecisionFilters(group="failed"), limit=10, offset=0
            )
        assert (reply.kind, reply.status, reply.sent_text, reply.error) == ("proactive", "failed", None, "404 Not Found")
        assert (row.kind, row.outcome, row.reply_id) == ("proactive", "failed", reply.id)
        assert [entry["decision"].id for entry in failed] == [row.id]


async def test_a_disabled_account_on_the_same_integration_never_reads_the_send(sessions):
    async with _scratch("acct_sent_log_disabled") as scratch:
        sessions(scratch)
        async with scratch.sessions() as db:
            account = await _account(db)
            disabled = AgentAccount(
                user_id=account.user_id, name="Old twin", provider="microsoft_teams",
                integration_id=account.integration_id, agent_id=account.agent_id, mode="on_behalf", rules=[],
                account_external_id=f"old-{uuid.uuid4().hex[:6]}", enabled=False,
            )
            db.add(disabled)
            await db.commit()
        written = await sent_log.record_proactive_send(
            str(account.integration_id), provider="microsoft_teams", tool="send_microsoft_chat_message",
            conversation_id="chat-1", text="Hi",
        )
        assert written == 1
        async with scratch.sessions() as db:
            owners = (await db.execute(select(AgentAccountDecision.account_id))).scalars().all()
        assert owners == [account.id]


async def test_only_the_accounts_of_the_integration_and_provider_log_the_send(sessions):
    async with _scratch("acct_sent_log_scope") as scratch:
        sessions(scratch)
        async with scratch.sessions() as db:
            account = await _account(db)
        # An Outlook send through the same integration is not a Teams account's message.
        assert (
            await sent_log.record_proactive_send(
                str(account.integration_id), provider="microsoft_outlook", tool="send_email",
                conversation_id="mail-1", text="Hi",
            )
            == 0
        )
        assert (
            await sent_log.record_proactive_send(
                str(uuid.uuid4()), provider="microsoft_teams", tool="send_microsoft_chat_message",
                conversation_id="chat-1", text="Hi",
            )
            == 0
        )
        async with scratch.sessions() as db:
            rows = (
                await db.execute(select(AgentAccountDecision).where(AgentAccountDecision.account_id == account.id))
            ).all()
        assert rows == []
