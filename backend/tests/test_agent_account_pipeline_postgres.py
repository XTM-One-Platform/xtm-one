"""The agent-account pipeline end to end: a fake provider, a stubbed agent turn, real PostgreSQL.

From a message to what happens: posted with or without the label, drafted for
approval with a notice to the owner, only recorded (shadow), left silent when
the agent decides so, never posted when the turn stopped at a gated tool,
parked for a grace period and dropped when the human answered meanwhile.
"""

from __future__ import annotations

import json
import uuid
from collections import OrderedDict
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.security import encrypt_value
from app.models.activity import Activity
from app.models.agent import Agent
from app.models.agent_account import AgentAccount, AgentAccountReply
from app.models.integration import Integration
from app.models.user import User
from app.services import agent_chat, agentic_gate, email_signatures
from app.services.agent_accounts import activity, pipeline, state, turns
from app.services.agent_accounts import providers as provider_registry
from app.services.agent_accounts.events import AccountEvent, ContextMessage
from app.services.agent_accounts.providers import (
    AccountProvider,
    AccountSignature,
    MessageMark,
    ProviderCapabilities,
    ProviderError,
    ProviderSession,
)
from app.services.agent_accounts.rules import normalize_rules, preset
from app.services.email_signatures import EmailSignature, SignatureUnavailableError
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


class FakeProvider(AccountProvider):
    key = "fake"
    label = "Fake Chat"
    integration_provider = "microsoft"
    platform = "teams"

    def __init__(self) -> None:
        self.sent: list[tuple[dict, str]] = []
        self.written_since: list[str] = []
        self.fail_send = False

    async def identify(self, session):  # pragma: no cover - not reached
        raise NotImplementedError

    async def fetch_context(self, session, event, limit):
        return [
            ContextMessage("c1", "Alice", "Morning!", datetime(2026, 10, 6, 8, 0, tzinfo=UTC)),
            ContextMessage("c2", "Sam", "Morning Alice", datetime(2026, 10, 6, 8, 1, tzinfo=UTC), from_account=True),
        ]

    async def account_messages_since(self, session, event):
        return list(self.written_since)

    async def send_reply(self, session, route, text):
        if self.fail_send:
            raise ProviderError("Fake Chat refused the message.")
        self.sent.append((route, text))
        return f"sent-{len(self.sent)}"


class MemoryState:
    """The pipeline's Redis state, in memory."""

    def __init__(self, monkeypatch) -> None:
        self.decisions: list[str] = []
        #: Who each decided sender was found to be (``Decision.sender``).
        self.senders: list[dict | None] = []
        #: The reply each decision is linked to in the activity log.
        self.reply_ids: list[uuid.UUID | None] = []
        #: The agent's note on each triaged message.
        self.notes: list[str | None] = []
        self.sent: set[str] = set()
        self.owner_active: list[tuple[str, int]] = []
        self.replied: list[str] = []
        self.parked: list[str] = []
        self.sending: set[str] = set()
        #: A post whose id gets recorded while an echo waits for it.
        self.recorded_during_wait: str | None = None
        #: ``(provider, conversation, message)`` of every agent account's posts.
        self.agent_posts: set[tuple[str, str, str]] = set()
        #: A message another agent account records as its post while this turn runs.
        self.agent_post_during_turn: tuple[str, str, str] | None = None
        #: The account of every agent turn taken this hour.
        self.turns: list[str] = []
        #: The engine reads no turn yet, as when messages arrive together.
        self.turns_unseen = False

        async def account_turns(account_id):
            return 0 if self.turns_unseen else self.turns.count(account_id)

        async def take_turn(account_id, limit):
            if limit > 0 and self.turns.count(account_id) >= limit:
                return False
            self.turns.append(account_id)
            return True

        async def first_delivery(account_id, conversation_id, message_id):
            return True

        async def conversation_state(account_id, event):
            return False, False, 0

        async def record_decision(account_id, event, decision, outcome, *, reply_id=None, note=None):
            self.decisions.append(outcome)
            self.senders.append(decision.sender)
            self.reply_ids.append(reply_id)
            self.notes.append(note)

        async def mark_sent(account_id, conversation_id, message_id):
            self.sent.add(message_id)

        async def was_sent_by_agent(account_id, conversation_id, message_id):
            return message_id in self.sent

        async def mark_owner_active(account_id, conversation_id, minutes):
            self.owner_active.append((conversation_id, minutes))

        async def mark_replied(account_id, *, conversation_id, thread_id, message_id):
            self.replied.append(thread_id)

        async def park(account_id, event, due):
            self.parked.append(event.message_id)
            return f"member-{event.message_id}"

        async def mark_sending(account_id, conversation_id, sending):
            (self.sending.add if sending else self.sending.discard)(conversation_id)

        async def is_sending(account_id, conversation_id):
            if self.recorded_during_wait:
                self.sent.add(self.recorded_during_wait)
            return conversation_id in self.sending

        async def mark_agent_post(provider, conversation_id, message_id):
            self.agent_posts.add((provider, conversation_id, message_id))

        async def is_agent_post(provider, conversation_id, message_id):
            return (provider, conversation_id, message_id) in self.agent_posts

        for fn in (
            account_turns,
            take_turn,
            mark_agent_post,
            is_agent_post,
            first_delivery,
            conversation_state,
            mark_sent,
            was_sent_by_agent,
            mark_owner_active,
            mark_replied,
            park,
            mark_sending,
            is_sending,
        ):
            monkeypatch.setattr(state, fn.__name__, fn)
        monkeypatch.setattr(activity, "record_decision", record_decision)
        monkeypatch.setattr(pipeline, "_schedule_after_grace", lambda *a, **k: None)

        async def no_turn_lock(account_id, conversation_id, holder):
            return None

        # One turn per conversation has its own tests
        # (test_agent_account_turns.py): here each message is its own turn.
        monkeypatch.setattr(turns, "take", no_turn_lock)


class AgentTurn:
    """``run_agent_chat`` stood in for: records what it was given, answers what it is told."""

    def __init__(self, monkeypatch) -> None:
        self.answer = "Happy to help."
        self.calls: list[dict[str, Any]] = []
        self.approvals: list = []
        self.error: str | None = None

        async def run(db, user_id, agent, history, user_message, **kwargs):
            self.calls.append({"user_id": user_id, "history": history, "user_message": user_message, **kwargs})
            if self.approvals:
                kwargs["approvals_out"].extend(self.approvals)
            if self.error:
                kwargs["error_out"]["error_code"] = self.error
            return self.answer, None

        monkeypatch.setattr(agent_chat, "run_agent_chat", run)


@pytest.fixture
def harness(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(provider_registry, "_PROVIDERS", {"fake": provider})

    async def _session(db, account, provider_):
        return ProviderSession(token="t", account_external_id="me")

    async def _allowed(db):
        return None

    monkeypatch.setattr(pipeline, "open_session", _session)
    monkeypatch.setattr(agentic_gate, "agentic_refusal", _allowed)
    return provider, MemoryState(monkeypatch), AgentTurn(monkeypatch)


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


async def _account(db, mode: str, *, provider: str = "fake", kind: str = "chat", **fields) -> AgentAccount:
    owner = User(email=f"o-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name="Sam")
    db.add(owner)
    await db.flush()
    integration = Integration(
        user_id=owner.id, provider="microsoft", auth_type="oauth", name="ms", status="connected",
        access_token_enc=encrypt_value("t"),
    )
    agent = Agent(name="Helper", slug=f"h-{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db.add_all([integration, agent])
    await db.flush()
    base = preset(mode, kind)
    values = {
        "response_mode": base["response_mode"],
        "ai_label_enabled": base["ai_label_enabled"],
        "grace_period_seconds": 0,
        "owner_takeover_minutes": base["owner_takeover_minutes"],
        "max_replies_per_hour": base["max_replies_per_hour"],
        "max_turns_per_hour": base["max_turns_per_hour"],
        "max_reply_recipients": base["max_reply_recipients"],
        "rules": base["rules"],
    }
    values.update(fields)
    account = AgentAccount(
        user_id=owner.id, name="Sam's Teams", provider=provider, integration_id=integration.id, agent_id=agent.id,
        mode=mode, account_display_name="Sam Example", account_external_id="me", **values,
    )
    db.add(account)
    await db.commit()
    return account


def _event(**overrides) -> AccountEvent:
    values = {
        "provider": "fake",
        "conversation_type": "direct",
        "conversation_id": "chat-1",
        "thread_id": "chat-1",
        "message_id": f"m-{uuid.uuid4().hex[:6]}",
        "sender_id": "alice-id",
        "sender_name": "Alice",
        "text": "Can you share the deck?",
        "created_at": datetime(2026, 10, 6, 9, 0, tzinfo=UTC),
        "route": {"kind": "chat", "chat_id": "chat-1"},
    }
    values.update(overrides)
    return AccountEvent(**values)


async def _replies(scratch, account) -> list[AgentAccountReply]:
    async with scratch.sessions() as db:
        return list(
            (await db.execute(select(AgentAccountReply).where(AgentAccountReply.account_id == account.id))).scalars()
        )


async def test_an_ai_user_answers_a_direct_message_at_once_in_context(harness):
    provider, memory, turn = harness
    async with _scratch("acct_pipe_ai_user") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user")
        await pipeline.process_event(str(account.id), _event())
        assert provider.sent == [({"kind": "chat", "chat_id": "chat-1"}, "Happy to help.")]
        call = turn.calls[0]
        assert call["user_id"] == str(account.user_id) and call["allow_handover"] is False
        assert [m.role for m in call["history"]] == ["user", "assistant"]
        assert call["user_message"] == "Alice: Can you share the deck?"
        assert call["context"]["response_policy"] == "always"
        assert "AI colleague" in call["context"]["account_brief"]
        (reply,) = await _replies(scratch, account)
        assert reply.status == "sent" and reply.sent_message_id == "sent-1"
        assert memory.decisions == ["sent"] and memory.replied == ["chat-1"] and "sent-1" in memory.sent


async def test_the_files_and_images_of_the_message_reach_the_turn_as_its_files(harness, monkeypatch):
    """A pasted image and a shared file are stored as the owner's files and handed to the turn."""
    from app.models.file_attachment import FileAttachment
    from app.services import file_storage as storage_module
    from app.services.agent_accounts.events import AccountAttachment

    provider, _memory, turn = harness
    bodies = {
        "https://graph.example/image": (b"\x89PNG\r\n\x1a\n", "image/png"),
        "https://graph.example/file": (b"%PDF-1.7", "application/octet-stream"),
    }
    caps: list[int] = []

    async def download_attachment(session, attachment, max_bytes):
        caps.append(max_bytes)
        if attachment.url not in bodies:
            raise ProviderError("Fake Chat refused the download.")
        return bodies[attachment.url]

    uploaded: list[tuple[str, str, str]] = []

    async def upload(*, data, filename, content_type, prefix):
        uploaded.append((filename, content_type, prefix))
        return f"{prefix}/{uuid.uuid4().hex}-{filename}"

    monkeypatch.setattr(provider, "download_attachment", download_attachment, raising=False)
    monkeypatch.setattr(storage_module.file_storage, "upload", upload)
    attachments = (
        AccountAttachment(name="teams-image-abc", url="https://graph.example/image"),
        AccountAttachment(name="plan.pdf", url="https://graph.example/file", content_type="application/pdf"),
        AccountAttachment(name="gone.docx", url="https://graph.example/gone"),
    )
    async with _scratch("acct_pipe_files") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user")
        await pipeline.process_event(str(account.id), _event(attachments=attachments))
        files = turn.calls[0]["files"]
        assert [(f.filename, f.content_type) for f in files] == [
            ("teams-image-abc.png", "image/png"),
            ("plan.pdf", "application/pdf"),
        ]
        assert caps and all(cap == storage_module.MAX_UPLOAD_SIZE_BYTES for cap in caps)
        assert all(prefix == f"accounts/{account.id}" for _, _, prefix in uploaded)
        async with scratch.sessions() as db:
            rows = list((await db.execute(select(FileAttachment))).scalars())
        assert {str(r.id) for r in rows} == {f.file_id for f in files}
        assert {(r.user_id, r.source) for r in rows} == {(account.user_id, "account:fake")}
        # The answer goes out as usual.
        assert provider.sent == [({"kind": "chat", "chat_id": "chat-1"}, "Happy to help.")]


async def test_a_message_without_files_hands_the_turn_none(harness):
    _provider, _memory, turn = harness
    async with _scratch("acct_pipe_no_files") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user")
        await pipeline.process_event(str(account.id), _event())
        assert turn.calls[0]["files"] is None


async def test_a_delegate_posting_on_its_own_adds_the_label(harness):
    provider, _memory, _turn = harness
    async with _scratch("acct_pipe_label") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "on_behalf", response_mode="auto")
        await pipeline.process_event(str(account.id), _event())
        ((_, text),) = provider.sent
        assert text == "Happy to help.\n\n_Sent by an AI agent on behalf of Sam Example_"


async def test_a_reply_posted_on_its_own_tells_the_owner_unless_they_opted_out(harness):
    """The owner follows what is said in their name without opening Teams."""
    provider, memory, _turn = harness
    async with _scratch("acct_pipe_notify_sent") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "on_behalf", response_mode="auto")
        await pipeline.process_event(str(account.id), _event(conversation_name="Q4 planning"))
        (reply,) = await _replies(scratch, account)
        async with scratch.sessions() as db:
            notice = (await db.execute(select(Activity).where(Activity.user_id == account.user_id))).scalar_one()
        assert notice.extra_data == {
            "notification_source": "agent_account",
            "notification_kind": "sent",
            "account_id": str(account.id),
            "reply_id": str(reply.id),
        }
        assert notice.title == "Sam's Teams replied in Q4 planning"
        assert notice.summary.startswith("To Alice: Happy to help.")
        # The log links the message to the reply it produced.
        assert memory.decisions == ["sent"] and memory.reply_ids == [reply.id]

        async with scratch.sessions() as db:
            stored = await db.get(AgentAccount, account.id)
            stored.notify_on_send = False
            await db.commit()
        await pipeline.process_event(str(account.id), _event())
        assert len(provider.sent) == 2
        async with scratch.sessions() as db:
            assert len((await db.execute(select(Activity).where(Activity.user_id == account.user_id))).all()) == 1


async def test_a_delegate_drafts_for_approval_and_tells_its_owner(harness):
    provider, memory, _turn = harness
    async with _scratch("acct_pipe_approval") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "on_behalf")
        await pipeline.process_event(str(account.id), _event())
        assert provider.sent == []
        (reply,) = await _replies(scratch, account)
        assert reply.status == "pending" and reply.draft_text == "Happy to help." and reply.ai_label is True
        async with scratch.sessions() as db:
            notice = (await db.execute(select(Activity).where(Activity.user_id == account.user_id))).scalar_one()
        assert notice.extra_data["notification_source"] == "agent_account"
        assert notice.extra_data["notification_kind"] == "pending"
        assert notice.extra_data["reply_id"] == str(reply.id)
        assert memory.decisions == ["pending"] and memory.reply_ids == [reply.id]

        # Approved as edited: posted with the label, once.
        async with scratch.sessions() as db:
            stored = await db.get(AgentAccountReply, reply.id)
            owner_account = await db.get(AgentAccount, account.id)
            await pipeline.send_pending(db, owner_account, stored, text="Here it is.", decided_by=account.user_id)
            await db.commit()
        assert provider.sent[-1][1].startswith("Here it is.\n\n_Sent by an AI agent")
        (reply,) = await _replies(scratch, account)
        assert reply.status == "sent" and reply.decided_by_user_id == account.user_id


async def test_shadow_mode_records_and_sends_nothing(harness):
    provider, _memory, _turn = harness
    async with _scratch("acct_pipe_shadow") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user", response_mode="shadow")
        await pipeline.process_event(str(account.id), _event())
        assert provider.sent == []
        assert [r.status for r in await _replies(scratch, account)] == ["shadow"]


async def test_the_agent_deciding_to_stay_silent_posts_nothing(harness):
    provider, memory, turn = harness
    turn.answer = "[NO_RESPONSE]"
    rules = normalize_rules([{"conversation_types": ["channel"], "trigger": "any", "reply": "decide"}])
    async with _scratch("acct_pipe_silent") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user", rules=rules)
        await pipeline.process_event(str(account.id), _event(conversation_type="channel", conversation_name="#ops"))
        assert turn.calls[0]["context"]["response_policy"] == "decide"
        assert provider.sent == [] and await _replies(scratch, account) == []
        assert memory.decisions == ["silent"]


async def test_a_turn_stopped_at_a_gated_tool_or_refused_posts_nothing(harness):
    provider, _memory, turn = harness
    async with _scratch("acct_pipe_gated") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user")
        turn.approvals = [{"tool": "send_mail"}]
        turn.answer = "This action needs approval."
        await pipeline.process_event(str(account.id), _event())
        turn.approvals = []
        turn.error = "quota_exceeded"
        await pipeline.process_event(str(account.id), _event())
        assert provider.sent == []
        assert [r.status for r in await _replies(scratch, account)] == ["failed", "failed"]


async def test_a_refused_post_is_recorded_as_failed(harness):
    provider, _memory, _turn = harness
    provider.fail_send = True
    async with _scratch("acct_pipe_send_fail") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user")
        await pipeline.process_event(str(account.id), _event())
        (reply,) = await _replies(scratch, account)
        assert reply.status == "failed" and "refused" in reply.error
        async with scratch.sessions() as db:
            assert "refused" in (await db.get(AgentAccount, account.id)).last_error


async def test_a_grace_period_parks_the_message_and_the_humans_answer_wins(harness):
    provider, memory, turn = harness
    async with _scratch("acct_pipe_grace") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user", grace_period_seconds=60)
        event = _event()
        await pipeline.process_event(str(account.id), event)
        assert memory.parked == [event.message_id] and memory.decisions == ["waiting"] and turn.calls == []

        provider.written_since = ["typed-by-sam"]
        await pipeline.process_event(str(account.id), event, after_grace=True)
        assert memory.decisions[-1] == "owner_replied" and turn.calls == [] and provider.sent == []

        # The agent's own earlier post is not the human answering.
        memory.sent.add("typed-by-sam")
        await pipeline.process_event(str(account.id), event, after_grace=True)
        assert provider.sent and memory.decisions[-1] == "sent"


async def test_a_turn_resumed_after_a_cut_on_the_agents_own_account_posts_its_reply_once(harness):
    """A rollout cut the turn; the next process runs it again and answers (issue #10779).

    Another post of the agent's own account after the message (a tool send, a
    notification) is not a person answering: the resumed turn posts its real
    reply, once, and nothing else.
    """
    provider, memory, turn = harness
    async with _scratch("acct_pipe_cut_resume") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user")
        event = _event()
        provider.written_since = ["posted-by-another-path"]
        await pipeline.process_event(str(account.id), event, after_grace=True)
        assert len(provider.sent) == 1 and len(turn.calls) == 1
        assert memory.decisions[-1] == "sent" and "owner_replied" not in memory.decisions


async def test_a_resumed_turn_on_behalf_of_the_owner_still_leaves_the_owners_answer(harness):
    provider, memory, turn = harness
    async with _scratch("acct_pipe_cut_resume_owner") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "on_behalf", response_mode="auto")
        provider.written_since = ["typed-by-sam"]
        await pipeline.process_event(str(account.id), _event(), after_grace=True)
        assert memory.decisions[-1] == "owner_replied" and turn.calls == [] and provider.sent == []


async def test_the_human_writing_starts_the_takeover_and_nothing_else(harness):
    provider, memory, turn = harness
    async with _scratch("acct_pipe_takeover") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "on_behalf")
        await pipeline.process_event(str(account.id), _event(from_account=True, sender_id="me"))
        assert memory.owner_active == [("chat-1", account.owner_takeover_minutes)]
        assert turn.calls == [] and provider.sent == [] and memory.decisions == []


async def test_the_echo_of_a_post_in_flight_is_not_the_human_taking_over(harness, monkeypatch):
    _provider, memory, _turn = harness
    monkeypatch.setattr(pipeline, "ECHO_WAIT_SECONDS", 0)
    async with _scratch("acct_pipe_echo") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "on_behalf")
        memory.sending.add("chat-1")
        memory.recorded_during_wait = "echo-1"
        await pipeline.process_event(str(account.id), _event(from_account=True, message_id="echo-1"))
        assert memory.owner_active == []
        # What the human really typed while a post was in flight is still them.
        memory.recorded_during_wait = None
        await pipeline.process_event(str(account.id), _event(from_account=True, message_id="typed"))
        assert memory.owner_active == [("chat-1", account.owner_takeover_minutes)]


async def test_nothing_runs_while_agentic_ai_is_unavailable(harness, monkeypatch):
    provider, memory, turn = harness

    async def _refused(db):
        return "ai_disabled"

    monkeypatch.setattr(agentic_gate, "agentic_refusal", _refused)
    async with _scratch("acct_pipe_gate") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user")
        await pipeline.process_event(str(account.id), _event())
        assert memory.decisions == ["ai_unavailable"] and turn.calls == [] and provider.sent == []


async def test_the_turn_acts_as_the_account_and_talks_to_its_sender(harness):
    _provider, memory, turn = harness
    async with _scratch("acct_pipe_identity") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user")
        await pipeline.process_event(str(account.id), _event(sender_email="alice@example.com"))
        call = turn.calls[0]
        # The account's integration answers for its provider, whatever the agent binds.
        assert call["pinned_integrations"] == {"microsoft": str(account.integration_id)}
        assert call["identity_from_platform"] is True
        assert call["context"]["contact_name"] == "Alice" and call["context"]["sender_email"] == "alice@example.com"
        # Its post is recognisable to every other agent account.
        assert ("fake", "chat-1", "sent-1") in memory.agent_posts


async def test_a_message_another_agent_account_posted_is_never_answered(harness):
    provider, memory, turn = harness
    async with _scratch("acct_pipe_agent_author") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user")
        event = _event()
        memory.agent_posts.add(("fake", "chat-1", event.message_id))
        await pipeline.process_event(str(account.id), event)
        assert memory.decisions == ["agent_author"] and turn.calls == [] and provider.sent == []


async def test_a_post_recorded_by_the_other_account_during_the_turn_is_not_answered(harness, monkeypatch):
    provider, memory, turn = harness
    event = _event()
    answer = agent_chat.run_agent_chat

    async def _turn(*args, **kwargs):
        # The other account's send returned while this turn ran.
        memory.agent_posts.add(("fake", "chat-1", event.message_id))
        return await answer(*args, **kwargs)

    monkeypatch.setattr(agent_chat, "run_agent_chat", _turn)
    async with _scratch("acct_pipe_agent_late") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user")
        await pipeline.process_event(str(account.id), event)
        assert len(turn.calls) == 1 and provider.sent == []
        assert memory.decisions == ["agent_author"] and await _replies(scratch, account) == []


async def test_a_rules_access_holds_the_turn_to_a_subset_of_the_agents_capabilities(harness):
    _provider, _memory, turn = harness
    rules = normalize_rules(
        [
            {
                "id": "general", "conversation_types": ["channel"], "trigger": "mention",
                "access": {
                    "knowledge_base_ids": ["kb-public", "kb-the-agent-never-had"],
                    "integrations": ["opencti", "jira"],
                    "builtin_tool_ids": [],
                    "memory": "off",
                },
            },
            {"id": "direct", "conversation_types": ["direct"], "trigger": "any"},
        ]
    )
    async with _scratch("acct_pipe_access") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user", rules=rules)
            agent = await db.get(Agent, account.agent_id)
            agent.knowledge_base_ids = ["kb-board", "kb-public"]
            agent.mcp_server_ids = ["mcp-1"]
            agent.builtin_tool_ids = ["web_search"]
            agent.integration_template_bindings = ["microsoft", "opencti"]
            await db.commit()
        await pipeline.process_event(
            str(account.id), _event(conversation_type="channel", conversation_name="#general", mentions_account=True)
        )
        public = turn.calls[0]
        assert public["session_knowledge_base_ids"] == ["kb-public"]
        assert public["allowed_integration_providers"] == {"opencti"}
        assert public["session_builtin_tool_ids"] == []
        assert "session_mcp_server_ids" not in public
        assert public["memory_overrides"] == {"short_term": False, "medium_term": False, "persistent": False, "user": False}

        # A rule without access: the agent's whole set, as before.
        await pipeline.process_event(str(account.id), _event())
        direct = turn.calls[1]
        assert not any(key.startswith("session_") for key in direct)
        assert "allowed_integration_providers" not in direct and "memory_overrides" not in direct


async def test_a_group_rule_answers_its_members_and_leaves_everyone_else(harness):
    from app.models.group import Group, GroupMember

    _provider, memory, turn = harness
    async with _scratch("acct_pipe_groups") as scratch:
        async with scratch.sessions() as db:
            board = Group(name=f"Board {uuid.uuid4().hex[:6]}")
            member = User(email="Alice.Board@example.com", hashed_password="x", full_name="Alice")
            former = User(email="eve@example.com", hashed_password="x", full_name="Eve", is_active=False)
            db.add_all([board, member, former])
            await db.flush()
            db.add_all([GroupMember(group_id=board.id, user_id=member.id), GroupMember(group_id=board.id, user_id=former.id)])
            rules = normalize_rules(
                [{"id": "board", "conversation_types": ["direct"], "trigger": "any",
                  "groups": [{"id": str(board.id), "name": board.name}]}]
            )
            in_board = [{"id": str(board.id), "name": board.name}]
            await db.commit()
            account = await _account(db, "ai_user", rules=rules)
        await pipeline.process_event(str(account.id), _event(sender_email="alice.board@EXAMPLE.com"))
        assert len(turn.calls) == 1
        # The sign-in name finds the member when the mail address is another one.
        await pipeline.process_event(
            str(account.id),
            _event(message_id="m-alias", sender_email="alice@mail.example", sender_emails=("Alice.Board@example.com",)),
        )
        assert len(turn.calls) == 2
        await pipeline.process_event(str(account.id), _event(sender_email="bob@example.com"))
        # A deactivated account no longer carries its groups.
        await pipeline.process_event(str(account.id), _event(sender_email="eve@example.com"))
        await pipeline.process_event(str(account.id), _event(sender_email=None))
        assert len(turn.calls) == 2
        assert memory.decisions == ["sent", "sent", "no_matching_rule", "no_matching_rule", "no_matching_rule"]
        # The log says who the sender was found to be, in the rules' own words.
        assert memory.senders == [
            {"known": True, "emails": ["alice.board@EXAMPLE.com"], "groups": in_board},
            {"known": True, "emails": ["alice@mail.example", "Alice.Board@example.com"], "groups": in_board},
            {"known": False, "emails": ["bob@example.com"], "groups": []},
            {"known": False, "emails": ["eve@example.com"], "groups": []},
            {"known": False, "emails": [], "groups": []},
        ]


async def test_a_rule_without_groups_reads_no_directory(harness):
    _provider, memory, _turn = harness
    async with _scratch("acct_pipe_no_groups") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user")
        await pipeline.process_event(str(account.id), _event(sender_email="alice@example.com"))
        assert memory.decisions == ["sent"] and memory.senders == [None]


# -- mailboxes: every answering turn triages its email ------------------------------


class FakeMailbox(FakeProvider):
    key = "fake_mail"
    label = "Fake Mail"
    platform = "email"
    kind = "mailbox"
    capabilities = ProviderCapabilities(channel=False, conversations=False, triage=True)

    def __init__(self) -> None:
        super().__init__()
        self.marks: list[tuple[dict, MessageMark]] = []

    async def mark_message(self, session, route, mark):
        self.marks.append((dict(route), mark))


MAIL_ROUTE = {"kind": "mail", "message_id": "mail-1", "reply_all": False}


@pytest.fixture
def mailbox(harness, monkeypatch):
    _chat, memory, turn = harness
    provider = FakeMailbox()
    monkeypatch.setattr(provider_registry, "_PROVIDERS", {"fake_mail": provider})
    return provider, memory, turn


def _email(**overrides) -> AccountEvent:
    return _event(
        provider="fake_mail", conversation_id="conv-1", thread_id="conv-1", route=MAIL_ROUTE,
        conversation_name="Q4 budget", mentions_account=True, **overrides,
    )


def _triage(decision: str, *, reply: str = "", note: str = "") -> str:
    return json.dumps({"decision": decision, "reply": reply, "note": note})


async def _mailbox_account(db, mode: str = "ai_user", **fields) -> AgentAccount:
    return await _account(db, mode, provider="fake_mail", kind="mailbox", **fields)


async def test_a_mailbox_turn_answers_in_the_triage_structure_and_files_the_answered_email(mailbox):
    provider, memory, turn = mailbox
    turn.answer = _triage("reply", reply="Here are the figures.", note="A plain request.")
    async with _scratch("acct_pipe_mail_reply") as scratch:
        async with scratch.sessions() as db:
            account = await _mailbox_account(db)
        await pipeline.process_event(str(account.id), _email())
        call = turn.calls[0]
        assert call["context"]["response_policy"] == "schema" and call["context"]["message_id"]
        assert call["session_output_schema"]["properties"]["decision"]["enum"] == ["reply", "needs_owner", "no_reply"]
        assert provider.sent == [(MAIL_ROUTE, "Here are the figures.")]
        assert provider.marks == [(MAIL_ROUTE, MessageMark(read=True, category="Answered by AI"))]
        assert memory.decisions == ["sent"] and memory.notes == ["A plain request."]
        (reply,) = await _replies(scratch, account)
        assert reply.status == "sent" and reply.draft_text == "Here are the figures."


async def test_an_email_the_agent_cannot_answer_is_flagged_and_its_owner_told_why(mailbox):
    provider, memory, turn = mailbox
    turn.answer = _triage("needs_owner", reply="ignored", note="It asks for a discount only you can grant.")
    async with _scratch("acct_pipe_mail_owner") as scratch:
        async with scratch.sessions() as db:
            account = await _mailbox_account(db)
        await pipeline.process_event(str(account.id), _email())
        assert provider.sent == [] and await _replies(scratch, account) == []
        assert provider.marks == [(MAIL_ROUTE, MessageMark(flag=True, category="Needs your attention"))]
        assert memory.decisions == ["flagged"] and memory.notes == ["It asks for a discount only you can grant."]
        async with scratch.sessions() as db:
            notice = (await db.execute(select(Activity).where(Activity.user_id == account.user_id))).scalar_one()
        assert notice.extra_data == {
            "notification_source": "agent_account", "notification_kind": "flagged", "account_id": str(account.id),
        }
        assert notice.title == "Sam's Teams: Alice needs you"
        assert notice.summary == "Q4 budget - It asks for a discount only you can grant."


async def test_an_email_needing_no_reply_is_filed_as_the_owner_chose(mailbox):
    provider, memory, turn = mailbox
    turn.answer = _triage("no_reply", note="A newsletter-style update.")
    async with _scratch("acct_pipe_mail_no_reply") as scratch:
        async with scratch.sessions() as db:
            account = await _mailbox_account(
                db, mailbox_actions={"mark_read_no_reply": True, "category_no_reply": "", "notify_needs_owner": False}
            )
        await pipeline.process_event(str(account.id), _email())
        assert provider.sent == [] and provider.marks == [(MAIL_ROUTE, MessageMark(read=True))]
        assert memory.decisions == ["silent"] and memory.notes == ["A newsletter-style update."]
        async with scratch.sessions() as db:
            assert (await db.execute(select(Activity).where(Activity.user_id == account.user_id))).first() is None


async def test_a_rule_that_always_replies_still_lets_the_agent_hand_an_email_over(mailbox):
    provider, memory, turn = mailbox
    turn.answer = _triage("no_reply", note="Nothing I can add.")
    rules = normalize_rules([{"conversation_types": ["direct"], "trigger": "any", "reply": "always"}])
    async with _scratch("acct_pipe_mail_always") as scratch:
        async with scratch.sessions() as db:
            account = await _mailbox_account(db, rules=rules)
        await pipeline.process_event(str(account.id), _email())
        assert turn.calls[0]["session_output_schema"]["properties"]["decision"]["enum"] == ["reply", "needs_owner"]
        assert memory.decisions == ["flagged"] and provider.marks[0][1].flag


async def test_shadow_mode_flags_a_handed_over_email_and_touches_nothing_else(mailbox):
    provider, memory, turn = mailbox
    turn.answer = _triage("needs_owner", note="Legal question.")
    async with _scratch("acct_pipe_mail_shadow") as scratch:
        async with scratch.sessions() as db:
            account = await _mailbox_account(db, response_mode="shadow")
        await pipeline.process_event(str(account.id), _email())
        # The flag alone: no category, nothing read, nothing sent, nobody notified.
        assert provider.sent == [] and provider.marks == [(MAIL_ROUTE, MessageMark(flag=True))]
        assert memory.decisions == ["flagged"]
        async with scratch.sessions() as db:
            assert (await db.execute(select(Activity).where(Activity.user_id == account.user_id))).first() is None


@pytest.mark.parametrize("actions", [None, {"flag_needs_owner": False}])
async def test_shadow_mode_never_flags_an_email_left_alone_or_one_the_owner_wants_unflagged(mailbox, actions):
    provider, memory, turn = mailbox
    turn.answer = _triage("no_reply" if actions is None else "needs_owner", note="An FYI.")
    async with _scratch("acct_pipe_mail_shadow_quiet") as scratch:
        async with scratch.sessions() as db:
            account = await _mailbox_account(db, response_mode="shadow", mailbox_actions=actions)
        await pipeline.process_event(str(account.id), _email())
        assert provider.sent == [] and provider.marks == []
        assert memory.decisions == ["silent" if actions is None else "flagged"]


async def test_a_flag_the_mailbox_refuses_is_recorded_as_failed_with_the_error(mailbox, monkeypatch):
    provider, memory, turn = mailbox
    turn.answer = _triage("needs_owner", note="Legal question.")

    async def refuse(session, route, mark):
        raise ProviderError("Graph answered 403")

    monkeypatch.setattr(provider, "mark_message", refuse)
    async with _scratch("acct_pipe_mail_flag_failed") as scratch:
        async with scratch.sessions() as db:
            account = await _mailbox_account(db, response_mode="shadow")
        await pipeline.process_event(str(account.id), _email())
        assert memory.decisions == ["failed"] and memory.notes == ["Legal question."]
        async with scratch.sessions() as db:
            stored = await db.get(AgentAccount, account.id)
        assert stored.last_error == "Could not file the message in the mailbox: Graph answered 403"


async def test_a_mailbox_turn_that_is_not_a_triage_decision_sends_nothing(mailbox):
    provider, memory, turn = mailbox
    turn.answer = "Sure, I will look into it."
    async with _scratch("acct_pipe_mail_prose") as scratch:
        async with scratch.sessions() as db:
            account = await _mailbox_account(db)
        await pipeline.process_event(str(account.id), _email())
        assert provider.sent == [] and provider.marks == []
        assert memory.decisions == ["failed"]
        (reply,) = await _replies(scratch, account)
        assert reply.status == "failed" and "reply, hand it over or leave it" in reply.error


async def test_an_approved_email_reply_files_the_email_once_it_is_sent(mailbox):
    provider, memory, turn = mailbox
    turn.answer = _triage("reply", reply="Draft answer.", note="Routine.")
    async with _scratch("acct_pipe_mail_approval") as scratch:
        async with scratch.sessions() as db:
            account = await _mailbox_account(db, "on_behalf", grace_period_seconds=0)
        await pipeline.process_event(str(account.id), _email())
        assert provider.sent == [] and provider.marks == []
        (reply,) = await _replies(scratch, account)
        assert reply.status == "pending" and memory.notes == ["Routine."]
        async with scratch.sessions() as db:
            stored = await db.get(AgentAccountReply, reply.id)
            owner_account = await db.get(AgentAccount, account.id)
            await pipeline.send_pending(db, owner_account, stored, text=None, decided_by=account.user_id)
            await db.commit()
        assert provider.sent[0][1].startswith("Draft answer.\n\n_Sent by an AI agent")
        assert provider.marks == [(MAIL_ROUTE, MessageMark(read=True, category="Answered by AI"))]


async def test_an_email_another_agent_account_sent_is_never_answered(mailbox):
    provider, memory, turn = mailbox
    async with _scratch("acct_pipe_mail_agent") as scratch:
        async with scratch.sessions() as db:
            account = await _mailbox_account(db)
        await pipeline.process_event(str(account.id), _email(from_agent_account=True))
        assert turn.calls == [] and provider.sent == [] and memory.decisions == ["agent_author"]


# -- what one inbound email can make the account send and spend ---------------------


def _new_email(index: int) -> AccountEvent:
    """An email starting a conversation of its own, as each new email does."""
    return replace(_email(), conversation_id=f"conv-{index}", thread_id=f"conv-{index}")


def _too_many(recipients: int, limit: int) -> str:
    return (
        f"A reply would go to {recipients} other recipients, more than the {limit} "
        "the account answers on its own: nothing was sent."
    )


@pytest.mark.parametrize("mode", ["ai_user", "on_behalf"])
async def test_a_reply_all_to_more_people_than_the_account_allows_is_handed_to_its_owner(mailbox, mode):
    """The sender chooses who a reply-all reaches: fifty addresses in Cc never get an answer from the account."""
    provider, memory, turn = mailbox
    turn.answer = _triage("reply", reply="Here are the figures.", note="A plain request.")
    async with _scratch(f"acct_pipe_mail_fanout_{mode}") as scratch:
        async with scratch.sessions() as db:
            account = await _mailbox_account(db, mode, grace_period_seconds=0)
        await pipeline.process_event(str(account.id), _email(conversation_type="group", reply_recipients=50))
        assert len(turn.calls) == 1
        assert provider.sent == [] and await _replies(scratch, account) == []
        assert provider.marks == [(MAIL_ROUTE, MessageMark(flag=True, category="Needs your attention"))]
        assert memory.decisions == ["flagged"] and memory.notes == [_too_many(50, 10)]
        async with scratch.sessions() as db:
            notice = (await db.execute(select(Activity).where(Activity.user_id == account.user_id))).scalar_one()
        assert notice.extra_data["notification_kind"] == "flagged"
        assert notice.summary == f"Q4 budget - {_too_many(50, 10)}"


@pytest.mark.parametrize(("recipients", "limit"), [(10, 10), (50, 0)])
async def test_a_reply_all_within_the_limit_or_with_no_limit_is_sent(mailbox, recipients, limit):
    provider, memory, turn = mailbox
    turn.answer = _triage("reply", reply="Here are the figures.", note="A plain request.")
    async with _scratch(f"acct_pipe_mail_fanout_ok_{recipients}_{limit}") as scratch:
        async with scratch.sessions() as db:
            account = await _mailbox_account(db, max_reply_recipients=limit)
        await pipeline.process_event(str(account.id), _email(conversation_type="group", reply_recipients=recipients))
        assert provider.sent == [(MAIL_ROUTE, "Here are the figures.")]
        assert memory.decisions == ["sent"]


async def test_an_email_the_agent_leaves_is_left_whatever_its_recipients(mailbox):
    provider, memory, turn = mailbox
    turn.answer = _triage("no_reply", note="A newsletter-style update.")
    async with _scratch("acct_pipe_mail_fanout_silent") as scratch:
        async with scratch.sessions() as db:
            account = await _mailbox_account(db)
        await pipeline.process_event(str(account.id), _email(conversation_type="group", reply_recipients=50))
        assert provider.sent == [] and memory.decisions == ["silent"]
        assert memory.notes == ["A newsletter-style update."]


async def test_a_reply_past_the_recipient_limit_on_a_platform_that_does_not_triage_fails_unsent(harness):
    provider, memory, _turn = harness
    async with _scratch("acct_pipe_fanout_chat") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user")
        await pipeline.process_event(str(account.id), _event(reply_recipients=11))
        assert provider.sent == [] and memory.decisions == ["failed"]
        (reply,) = await _replies(scratch, account)
        assert reply.status == "failed" and reply.error == _too_many(11, 10)


async def test_one_sender_writing_a_hundred_new_emails_in_an_hour_runs_the_accounts_hourly_turns_only(mailbox):
    """Each new email is a new conversation: the account-wide limit bounds the turns and what they notify."""
    provider, memory, turn = mailbox
    turn.answer = _triage("needs_owner", note="It asks for a discount only you can grant.")
    async with _scratch("acct_pipe_mail_flood") as scratch:
        async with scratch.sessions() as db:
            account = await _mailbox_account(db)
        assert account.max_turns_per_hour == 60
        for i in range(100):
            await pipeline.process_event(str(account.id), _new_email(i))
        assert len(turn.calls) == 60
        assert memory.decisions == ["flagged"] * 60 + ["account_rate_limited"] * 40
        assert len(provider.marks) == 60
        async with scratch.sessions() as db:
            notices = (await db.execute(select(Activity).where(Activity.user_id == account.user_id))).all()
        assert len(notices) == 60


async def test_emails_arriving_together_never_run_more_turns_than_the_account_allows(mailbox):
    """The engine reads the hour before the turns are taken: taking one is what bounds them."""
    provider, memory, turn = mailbox
    memory.turns_unseen = True
    turn.answer = _triage("reply", reply="Here are the figures.", note="A plain request.")
    async with _scratch("acct_pipe_mail_burst") as scratch:
        async with scratch.sessions() as db:
            account = await _mailbox_account(db, max_turns_per_hour=2)
        for i in range(3):
            await pipeline.process_event(str(account.id), _new_email(i))
        assert len(turn.calls) == 2 and len(provider.sent) == 2
        assert memory.decisions == ["sent", "sent", "account_rate_limited"]
        assert len(await _replies(scratch, account)) == 2


async def test_a_chat_account_keeps_no_account_wide_limit(harness):
    provider, memory, _turn = harness
    async with _scratch("acct_pipe_chat_no_turn_limit") as scratch:
        async with scratch.sessions() as db:
            account = await _account(db, "ai_user")
        assert account.max_turns_per_hour == 0
        for i in range(5):
            await pipeline.process_event(str(account.id), _event(conversation_id=f"chat-{i}", thread_id=f"chat-{i}"))
        assert memory.decisions == ["sent"] * 5 and len(provider.sent) == 5


# -- signatures --------------------------------------------------------


class FakeSignedMailbox(FakeMailbox):
    """A mailbox whose replies are signed; it records the signature each reply was given."""

    capabilities = ProviderCapabilities(channel=False, conversations=False, triage=True, signature=True)

    def __init__(self) -> None:
        super().__init__()
        self.signatures: list[AccountSignature | None] = []

    async def send_reply(self, session, route, text):
        self.signatures.append(session.signature)
        return await super().send_reply(session, route, text)


@pytest.fixture
def signed(harness, monkeypatch):
    _chat, _memory, turn = harness
    provider = FakeSignedMailbox()
    monkeypatch.setattr(provider_registry, "_PROVIDERS", {"fake_mail": provider})
    service: dict[str, Any] = {"answer": EmailSignature(html="<p>New</p>", reply_html="<p>Reply banner</p>"), "asked": []}

    async def _read(credential: str, address: str):
        service["asked"].append((credential, address))
        if isinstance(service["answer"], BaseException):
            raise service["answer"]
        return service["answer"]

    monkeypatch.setattr(email_signatures, "_READERS", {"fake_signatures": _read})
    monkeypatch.setattr(email_signatures, "_cache", OrderedDict())
    turn.answer = _triage("reply", reply="Here are the figures.")
    return provider, service


async def _signed_account(db, source: str, *, with_integration: bool = True) -> AgentAccount:
    account = await _mailbox_account(db, account_email="sam@example.com", signature_source=source)
    if source == "integration" and with_integration:
        integration = Integration(
            user_id=account.user_id, provider="fake_signatures", auth_type="api_key", name="Signatures",
            status="connected", api_key_enc=encrypt_value("sig-key"),
        )
        db.add(integration)
        await db.flush()
        account.signature_integration_id = integration.id
        await db.commit()
    return account


async def _last_error(scratch, account) -> str | None:
    async with scratch.sessions() as db:
        return (await db.get(AgentAccount, account.id)).last_error


async def test_a_reply_carries_the_signature_read_from_the_owners_signature_integration(signed):
    provider, service = signed
    async with _scratch("acct_pipe_sig_live") as scratch:
        async with scratch.sessions() as db:
            account = await _signed_account(db, "integration")
        await pipeline.process_event(str(account.id), _email())
        assert provider.signatures == [AccountSignature("given", "<p>Reply banner</p>")]
        assert service["asked"] == [("sig-key", "sam@example.com")]
        assert await _last_error(scratch, account) is None


@pytest.mark.parametrize(
    ("answer", "words"),
    [
        (SignatureUnavailableError("The service refused the key."), "could not be read, so the reply carried the account's own signature: The service refused the key."),
        (None, "holds no signature for sam@example.com, so the reply carried the account's own signature."),
    ],
)
async def test_a_signature_integration_that_gives_nothing_falls_back_and_says_why(signed, answer, words):
    provider, service = signed
    service["answer"] = answer
    async with _scratch("acct_pipe_sig_fallback") as scratch:
        async with scratch.sessions() as db:
            account = await _signed_account(db, "integration")
        await pipeline.process_event(str(account.id), _email())
        assert provider.signatures == [AccountSignature("own")] and len(provider.sent) == 1
        assert words in (await _last_error(scratch, account) or "")


async def test_a_deleted_signature_integration_falls_back_on_the_accounts_own(signed):
    provider, service = signed
    async with _scratch("acct_pipe_sig_gone") as scratch:
        async with scratch.sessions() as db:
            account = await _signed_account(db, "integration", with_integration=False)
        await pipeline.process_event(str(account.id), _email())
        assert provider.signatures == [AccountSignature("own")] and service["asked"] == []
        assert "The signature integration is gone." in (await _last_error(scratch, account) or "")


@pytest.mark.parametrize(("source", "expected"), [("none", AccountSignature("none")), ("own", AccountSignature("own"))])
async def test_a_reply_signed_by_the_mail_server_or_the_account_reads_no_integration(signed, source, expected):
    provider, service = signed
    async with _scratch(f"acct_pipe_sig_{source}") as scratch:
        async with scratch.sessions() as db:
            account = await _signed_account(db, source)
        await pipeline.process_event(str(account.id), _email())
        assert provider.signatures == [expected] and service["asked"] == []
