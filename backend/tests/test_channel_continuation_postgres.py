"""One conversation continued across the web, Slack and Teams, on PostgreSQL.

What is under test is SQL as much as flow: the conversation list's channel
clause, the owner-scoped binding lookup and the binding upsert. The channel
router runs for real against the scratch database (``handle_message``), with
the model, the platform calls and Redis stubbed:

* a Slack or Teams direct-message thread of an identified user is listed on
  the web and continued there; a group thread, an anonymous conversation and
  another user's are not;
* a web conversation bound to a Slack thread is continued from it - same
  record, its history, agent and session tools - and only by its owner: a
  sender who resolves to someone else, or to nobody, never reaches it;
* one turn at a time across channels: a channel message waits for (then
  refuses) a turn already running, a web send refuses while a channel turn
  runs, both before anything is stored;
* a tool call a web turn paused on keeps the conversation waiting on the web;
* the files of a queued message are its turn's, and deleted when it never
  runs; the upload purge takes only those, never a file a turn claimed or a
  message shows.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import chat as chat_api
from app.api.v1 import chat_continuation as continuation_api
from app.channels import router as channel_router
from app.channels.base import ChannelType, InboundFileAttachment, InboundMessage
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.chat import Conversation, Message
from app.models.conversation_channel_binding import ConversationChannelBinding
from app.models.conversation_share import ConversationShare
from app.models.file_attachment import FileAttachment
from app.models.general_assistant_config import GeneralAssistantConfig
from app.models.user import User
from app.services import channel_continuation, chat_turn_guard, chat_turn_stream, widget_uploads
from app.services.channel_notices import notice
from tests.agentic_gate_stub import stub_agentic_gate
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_chat_web_steering import _FakeRedisKV

pytestmark = pytest.mark.integration

_REAL_RESOLVE_CHANNEL_FILES = channel_router._resolve_channel_files

SLACK_DM = "D0CONT1:1700000000.000100"
SLACK_GROUP = "C0GROUP:1700000000.000200"
TEAMS_PERSONAL = "a:1personal-chat"
TEAMS_GROUP = "19:group@thread.v2"


def _user(name: str) -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
        role="user",
    )


def _agent(owner: uuid.UUID, *, slack: bool = True) -> Agent:
    return Agent(
        name=f"agent-{uuid.uuid4().hex[:6]}",
        slug=f"agent-{uuid.uuid4().hex[:8]}",
        enabled=True,
        user_id=owner,
        is_builtin=False,
        is_company_managed=False,
        integration_bindings={},
        channel_config=(
            {"slack": {"receive_enabled": True, "publish_enabled": True}} if slack else None
        ),
        mcp_server_ids=[],
        custom_tool_ids=[],
        knowledge_base_ids=[],
        builtin_tool_ids=[],
        handover_agent_ids=[],
        output_format="markdown",
        memory_short_term_enabled=False,
        memory_medium_term_enabled=False,
        memory_persistent_enabled=False,
        memory_user_enabled=False,
    )


async def _seed(sessions) -> SimpleNamespace:
    """Two users, an agent of the owner's that answers on Slack, and one
    conversation per kind of thread."""
    async with sessions() as session:
        owner, other = _user("owner"), _user("other")
        session.add_all([owner, other])
        await session.flush()
        agent = _agent(owner.id)
        quiet_agent = _agent(owner.id, slack=False)
        session.add_all([agent, quiet_agent])
        await session.flush()

        def conv(user_id, channel, thread, **kw):
            return Conversation(
                user_id=user_id,
                channel=channel,
                channel_thread_id=thread,
                title=kw.pop("title", f"{channel} {thread}"),
                **kw,
            )

        rows = {
            "web": conv(
                owner.id,
                "web",
                None,
                agent_id=agent.id,
                title="Quarterly review",
                tool_overrides_json=json.dumps(
                    {"builtin_tool_ids": ["web_search"], "agent_id": str(agent.id)}
                ),
            ),
            "web_quiet": conv(owner.id, "web", None, agent_id=quiet_agent.id, title="Quiet"),
            "general": conv(owner.id, "web", None, title="Ask anything"),
            "slack_dm": conv(owner.id, "slack", "D0ORIGIN:1700000000.000300", agent_id=agent.id),
            "slack_group": conv(owner.id, "slack", SLACK_GROUP),
            "teams_personal": conv(owner.id, "teams", TEAMS_PERSONAL),
            "teams_group": conv(owner.id, "teams", TEAMS_GROUP),
            "anonymous": conv(None, "slack", "D0ANON:1700000000.000400"),
            "others": conv(other.id, "slack", "D0OTHER:1700000000.000500"),
        }
        session.add_all(rows.values())
        await session.flush()
        for name, text in (("web", "What changed in Q3?"), ("slack_dm", "Draft the summary")):
            session.add(
                Message(conversation_id=rows[name].id, role="user", content=text, channel=rows[name].channel, sequence=1)
            )
            session.add(
                Message(conversation_id=rows[name].id, role="assistant", content=f"re: {text}", channel=rows[name].channel, sequence=2)
            )
        await session.commit()
        return SimpleNamespace(
            owner=owner.id,
            owner_email=owner.email,
            other=other.id,
            other_email=other.email,
            agent=agent.id,
            conv={name: row.id for name, row in rows.items()},
        )


@pytest.fixture
def fake_redis(monkeypatch):
    import app.core.redis as redis_module

    fake = _FakeRedisKV()

    async def _client():
        return fake

    monkeypatch.setattr(redis_module, "redis_client", _client)
    return fake


class _Publisher:
    """Stands in for the turn fan-out: records what the web viewers were sent."""

    instances: list[_Publisher] = []

    def __init__(self, conversation_id, turn_token, *, origin=None):
        self.conversation_id = conversation_id
        self.turn_token = turn_token
        self.frames: list[dict] = []
        self.closed = False
        _Publisher.instances.append(self)

    def start(self, ack: dict[str, object] | None = None) -> None:
        self.frames.append({**(ack or {}), "type": "thinking"})

    def offer(self, frame):
        self.frames.append(frame)

    async def on_event(self, event):
        self.frames.append({"type": "status", **event})

    async def aclose(self, *, terminal=None):
        self.closed = True


def _stub_channel_turn(monkeypatch, *, run=None):
    """``handle_message`` with the model, the platform calls and the logs stubbed."""
    from app.channels import router
    from app.services import chat_turn_stream

    _Publisher.instances = []
    stub_agentic_gate(monkeypatch)
    bot = SimpleNamespace(
        id=uuid.uuid4(),
        only_mention=False,
        allow_anonymous=False,
        allow_private=True,
        allowed_receive_channels=None,
        is_company_managed=True,
        requires_approval=True,
    )
    monkeypatch.setattr(router, "_get_channel_bot", AsyncMock(return_value=bot))
    monkeypatch.setattr(router, "_bot_allows_receive", lambda _b, _i: True)
    monkeypatch.setattr(router, "_is_thread_active", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "_is_thread_muted", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "_mark_thread_active", AsyncMock())
    monkeypatch.setattr(router, "_log_channel_activity", AsyncMock())
    monkeypatch.setattr(router, "_resolve_channel_files", AsyncMock(return_value=[]))
    monkeypatch.setattr(router, "_load_channel_backfill", AsyncMock(return_value=None))
    monkeypatch.setattr(router, "_load_llm_trace_id", AsyncMock(return_value=None))

    async def _no_compaction(_db, history, **_kw):
        return history

    monkeypatch.setattr(router, "_compact_history", _no_compaction)
    monkeypatch.setattr(chat_turn_stream, "TurnPublisher", _Publisher)

    async def _answer(_db, _user_id, agent, history, text, **kwargs):
        return (f"{agent.name} answers {text}", None)

    run = AsyncMock(side_effect=run or _answer)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", run)
    general = AsyncMock(side_effect=AssertionError("the General Assistant must not answer"))
    monkeypatch.setattr("app.services.general_chat.run_general_chat", general)
    resolve = AsyncMock(side_effect=AssertionError("a continued thread keeps its agent"))
    return SimpleNamespace(router=router, run=run, resolve=resolve)


def _slack_dm(text: str, *, thread: str = SLACK_DM, email: str | None, threaded: bool = True) -> InboundMessage:
    ts = thread.split(":", 1)[1]
    return InboundMessage(
        channel=ChannelType.SLACK,
        text=text,
        user_email=email,
        channel_user_id="U0CONT1",  # the sender's member id, as the adapter sends it
        channel_thread_id=thread,
        channel_message_id=f"{ts}9",
        metadata={"channel_type": "im", "bot_was_mentioned": False, "is_threaded_reply": threaded},
    )


async def _bind(sessions, w, name: str = "web", thread: str = SLACK_DM, channel: str = "slack") -> None:
    async with sessions() as session:
        await channel_continuation.bind_thread(
            session,
            conversation_id=w.conv[name],
            user_id=w.owner,
            channel=channel,
            channel_thread_id=thread,
        )
        await session.commit()


async def _audit(session, conv_id) -> list[AuditLog]:
    return list(
        (
            await session.execute(
                select(AuditLog).where(AuditLog.entity_id == conv_id).order_by(AuditLog.created_at)
            )
        ).scalars()
    )


async def _messages(sessions, conv_id) -> list[tuple[str, str, str]]:
    async with sessions() as session:
        rows = (
            await session.execute(
                select(Message.role, Message.content, Message.channel)
                .where(Message.conversation_id == conv_id)
                .order_by(Message.sequence)
            )
        ).all()
    return [tuple(r) for r in rows]


# ── Continue on the web ─────────────────────────────────────────────────────


async def _list(session, user_id, scope="own"):
    return await chat_api.list_conversations(
        user=await session.get(User, user_id),
        db=session,
        limit=50,
        offset=0,
        archived=False,
        workspace_id=None,
        scope=scope,
        presentation_id=None,
        video_project_id=None,
        custom_dashboard_id=None,
        fleet_id=None,
    )


@pytest.mark.asyncio
async def test_the_web_lists_direct_message_threads_and_never_group_anonymous_or_others():
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            listed = {row.id for row in await _list(session, w.owner)}
            others = {row.id for row in await _list(session, w.other)}

    c = {name: str(cid) for name, cid in w.conv.items()}
    assert listed == {c["web"], c["web_quiet"], c["general"], c["slack_dm"], c["teams_personal"]}
    assert c["slack_group"] not in listed and c["teams_group"] not in listed
    assert c["anonymous"] not in listed and c["anonymous"] not in others
    assert others == {c["others"]}


@pytest.mark.asyncio
async def test_a_shared_direct_message_thread_is_listed_and_a_shared_group_thread_is_not():
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            for name in ("slack_dm", "slack_group"):
                session.add(
                    ConversationShare(
                        conversation_id=w.conv[name], shared_with_user_id=w.other, created_by=w.owner
                    )
                )
            await session.commit()
            shared = {row.id for row in await _list(session, w.other, scope="shared")}

    assert shared == {str(w.conv["slack_dm"])}


@pytest.mark.asyncio
async def test_the_detail_says_whether_the_web_continues_it_and_where_it_is_continued():
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        await _bind(db.sessions, w)
        async with db.sessions() as session:
            owner = await session.get(User, w.owner)
            web = await chat_api.get_conversation(str(w.conv["web"]), limit=50, user=owner, db=session)
            dm = await chat_api.get_conversation(str(w.conv["slack_dm"]), limit=50, user=owner, db=session)
            group = await chat_api.get_conversation(str(w.conv["slack_group"]), limit=50, user=owner, db=session)

    assert web.continued_in == ["slack"] and web.web_continuable is True
    assert [m.content for m in dm.messages] == ["Draft the summary", "re: Draft the summary"]
    assert dm.web_continuable is True
    assert group.web_continuable is False


@pytest.mark.asyncio
async def test_the_rest_send_refuses_a_group_thread_before_storing_anything(monkeypatch, fake_redis):
    from app.api.v1.chat import SendMessageRequest

    monkeypatch.setattr(chat_api, "enforce_rate_limit", AsyncMock())
    stored = AsyncMock(side_effect=AssertionError("the message was stored"))
    monkeypatch.setattr(chat_api, "store_message", stored)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            with pytest.raises(HTTPException) as exc:
                await chat_api.send_message(
                    str(w.conv["slack_group"]),
                    SendMessageRequest(content="hello"),
                    user=await session.get(User, w.owner),
                    db=session,
                    _chat_gate=None,
                )

    assert exc.value.status_code == 409
    assert "Slack thread shared with other people" in exc.value.detail
    stored.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "refusal"),
    [
        ("slack_group", "This conversation is a Slack thread shared with other people."),
        ("teams_group", "This conversation is a Microsoft Teams thread shared with other people."),
        ("slack_dm", None),
    ],
    ids=["slack_group", "teams_group", "slack_dm"],
)
async def test_an_upload_into_a_group_thread_is_refused_as_a_send_is(monkeypatch, fake_redis, name, refusal):
    import io

    from fastapi import UploadFile

    from app.models.file_attachment import FileAttachment
    from app.services.file_storage import file_storage

    stored_objects: list[str] = []

    async def _upload(*, data, filename, content_type, prefix):
        stored_objects.append(f"{prefix}/{filename}")
        return stored_objects[-1]

    monkeypatch.setattr(file_storage, "upload", _upload)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            owner = await session.get(User, w.owner)
            assert owner.role == "user"
            call = chat_api.upload_file_to_conversation(
                str(w.conv[name]),
                file=UploadFile(file=io.BytesIO(b"the incident timeline"), filename="timeline.txt"),
                create_message=False,
                user=owner,
                db=session,
                _ai_gate=None,
            )
            if refusal:
                with pytest.raises(HTTPException) as exc:
                    await call
            else:
                uploaded = await call
        async with db.sessions() as session:
            attached = (
                await session.execute(
                    select(func.count())
                    .select_from(FileAttachment)
                    .where(FileAttachment.conversation_id == w.conv[name])
                )
            ).scalar_one()

    if refusal:
        assert exc.value.status_code == 409 and exc.value.detail.startswith(refusal)
        assert stored_objects == [] and attached == 0, "nothing is stored"
    else:
        assert uploaded["status"] == "ok" and attached == 1


@pytest.mark.asyncio
async def test_the_rest_send_refuses_while_a_channel_turn_runs(monkeypatch, fake_redis):
    from app.api.v1.chat import SendMessageRequest

    monkeypatch.setattr(chat_api, "enforce_rate_limit", AsyncMock())
    stored = AsyncMock(side_effect=AssertionError("the message was stored"))
    monkeypatch.setattr(chat_api, "store_message", stored)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        conv_id = str(w.conv["slack_dm"])
        channel_token = await chat_turn_guard.acquire_turn(conv_id, channel="slack")
        async with db.sessions() as session:
            with pytest.raises(HTTPException) as exc:
                await chat_api.send_message(
                    conv_id,
                    SendMessageRequest(content="and the figures?"),
                    user=await session.get(User, w.owner),
                    db=session,
                    _chat_gate=None,
                )

    assert exc.value.status_code == 409
    assert "sent from Slack is being written" in exc.value.detail
    stored.assert_not_awaited()
    assert fake_redis.kv[chat_turn_guard.processing_key(conv_id)] == channel_token


# ── Continue in a channel: through the router ───────────────────────────────


@pytest.mark.asyncio
async def test_a_bound_slack_thread_continues_the_web_conversation_for_its_owner(monkeypatch, fake_redis):
    stub = _stub_channel_turn(monkeypatch)
    held: list[str | None] = []

    async def _answer(_db, _user_id, agent, history, text, **kwargs):
        held.append(chat_turn_guard.channel_turn_token_channel(
            fake_redis.kv.get(chat_turn_guard.processing_key(kwargs["conversation_id"]))
        ))
        return (f"{agent.name} answers {text}", None)

    stub.run.side_effect = _answer
    monkeypatch.setattr(stub.router, "_resolve_channel_agent", stub.resolve)
    relayed: list[dict] = []
    monkeypatch.setattr(
        chat_turn_stream, "publish_live", lambda _cid, frame, *, origin=None: relayed.append(frame)
    )
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        await _bind(db.sessions, w)
        out = await stub.router.handle_message(_slack_dm("and the figures?", email=w.owner_email))
        stored = await _messages(db.sessions, w.conv["web"])
        async with db.sessions() as session:
            conv = await session.get(Conversation, w.conv["web"])
            agent = await session.get(Agent, w.agent)

    assert not out.suppress and out.text == f"{agent.name} answers and the figures?"
    assert out.metadata["conversation_id"] == str(w.conv["web"])
    # Same record: the web history is the model's history, the turn is appended to it.
    _db, _uid, used_agent, history, text = stub.run.await_args.args
    assert used_agent.id == w.agent and text == "and the figures?"
    assert [m.content for m in history] == ["What changed in Q3?", "re: What changed in Q3?"]
    kwargs = stub.run.await_args.kwargs
    assert kwargs["conversation_id"] == str(w.conv["web"])
    assert kwargs["session_builtin_tool_ids"] == ["web_search"]
    assert kwargs["context"]["platform"] == "slack"  # the channel's output format
    stub.resolve.assert_not_awaited()
    assert stored[2:] == [
        ("user", "and the figures?", "slack"),
        ("assistant", f"{agent.name} answers and the figures?", "slack"),
    ]
    assert conv.agent_id == w.agent and conv.channel == "web"
    # One turn at a time: the conversation's lock was held, as a Slack turn, then given back.
    assert held == ["slack"]
    assert chat_turn_guard.processing_key(str(w.conv["web"])) not in fake_redis.kv
    (publisher,) = _Publisher.instances
    done = publisher.frames[-1]
    assert done["type"] == "done" and done["message_id"] and done["channel"] == "slack"
    assert publisher.closed
    # The web sees the Slack message before the reply that answers it.
    (shown,) = [f["message"] for f in relayed if f["type"] == "user_message"]
    assert (shown["content"], shown["channel"]) == ("and the figures?", "slack")


async def test_a_resumed_turn_keeps_each_replayed_tool_call_with_its_result(monkeypatch, fake_redis):
    """A shutdown-cut channel turn resumes its checkpointed tool work after
    compaction, as the web does: a compaction that kept only the tail of the
    replay sent a tool result without its call, which every provider 400s."""
    from app.llm.base import ChatMessage
    from app.services import turn_checkpoint
    from app.tools.base import ToolCall

    stub = _stub_channel_turn(monkeypatch)
    monkeypatch.setattr(stub.router, "_resolve_channel_agent", stub.resolve)
    replay = [
        ChatMessage(role="assistant", tool_calls=[ToolCall(id="toolu_1", name="web_search")]),
        ChatMessage(role="tool", content="found", tool_call_id="toolu_1", name="web_search"),
    ]
    monkeypatch.setattr(turn_checkpoint, "load_replay", AsyncMock(return_value=(None, replay)))

    async def _keep_last(_db, history, **_kw):
        return history[-1:]  # a compaction keeps the most recent messages

    monkeypatch.setattr(stub.router, "_compact_history", _keep_last)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        await _bind(db.sessions, w)
        await stub.router.handle_message(_slack_dm("and the figures?", email=w.owner_email))

    history = stub.run.await_args.args[3]
    assert history[-2:] == replay


@pytest.mark.asyncio
async def test_a_bound_thread_never_reaches_the_conversation_for_another_sender(monkeypatch, fake_redis):
    stub = _stub_channel_turn(monkeypatch)
    monkeypatch.setattr(stub.router, "_resolve_channel_agent", AsyncMock(return_value=None))
    general = AsyncMock(return_value=("the General Assistant answers", None))
    monkeypatch.setattr("app.services.general_chat.run_general_chat", general)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        await _bind(db.sessions, w)
        before = await _messages(db.sessions, w.conv["web"])
        # A sender nobody resolves, then someone else, in the bound thread.
        anonymous = await stub.router.handle_message(_slack_dm("show me", email=None))
        other = await stub.router.handle_message(_slack_dm("show me", email=w.other_email))
        after = await _messages(db.sessions, w.conv["web"])

    assert after == before
    assert anonymous.text.startswith("I couldn't identify your account")
    # The other user gets a conversation of their own, never the owner's.
    assert other.metadata["conversation_id"] != str(w.conv["web"])
    assert general.await_args.args[1] == str(w.other)
    stub.run.assert_not_awaited()


def _run_queue_inline(monkeypatch, router) -> tuple[list[str], list]:
    """The drains asked for, recorded, and queued messages answered through *router*."""
    asked: list[str] = []
    delivered: list = []
    monkeypatch.setattr(channel_continuation, "schedule_queue_drain", lambda cid: asked.append(str(cid)))

    async def _deliver(inbound):
        delivered.append(await router.handle_message(inbound))

    monkeypatch.setattr(channel_continuation, "_run_queued_message", _deliver)
    return asked, delivered


async def _drain_all(asked: list[str]) -> None:
    while asked:  # each asked-for drain, as its spawned task would run it
        await channel_continuation.drain_channel_queue(asked.pop(0))


def _deleted_objects(monkeypatch) -> list[str]:
    """The object store's deletes, recorded."""
    from app.services.file_storage import file_storage

    deleted: list[str] = []

    async def _delete(key):
        deleted.append(key)

    monkeypatch.setattr(file_storage, "delete", _delete)
    return deleted


def _store_files_for_real(monkeypatch, router) -> SimpleNamespace:
    """The router's own file storing, with the platform download and the object store stubbed."""
    from app.services.file_storage import file_storage

    files = SimpleNamespace(
        download=AsyncMock(return_value=(b"the incident timeline", "text/plain")),
        uploaded=[],
        deleted=_deleted_objects(monkeypatch),
    )

    async def _upload(*, filename, **_kw):
        key = f"channel/slack/{uuid.uuid4().hex}-{filename}"
        files.uploaded.append(key)
        return key

    monkeypatch.setattr(router, "_resolve_channel_files", _REAL_RESOLVE_CHANNEL_FILES)
    monkeypatch.setattr(router, "_download_channel_file", files.download)
    monkeypatch.setattr(file_storage, "upload", _upload)
    return files


def _with_timeline(message: InboundMessage) -> InboundMessage:
    message.file_attachments = [
        InboundFileAttachment(
            url="https://files.example.test/download?token=short-lived-secret",
            filename="timeline.txt",
            content_type="text/plain",
        )
    ]
    return message


async def _files_of(sessions, user_id) -> list[FileAttachment]:
    async with sessions() as session:
        return list(
            (await session.execute(select(FileAttachment).where(FileAttachment.user_id == user_id))).scalars()
        )


async def _queued_file(sessions, user_id, *, created_at: datetime, source: str | None = None, **kw) -> uuid.UUID:
    async with sessions() as session:
        row = FileAttachment(
            user_id=user_id,
            filename="timeline.txt",
            content_type="text/plain",
            size=21,
            s3_object_name=f"channel/slack/{uuid.uuid4().hex}-timeline.txt",
            source=source or channel_continuation.queued_file_source("slack"),
            created_at=created_at,
            **kw,
        )
        session.add(row)
        await session.commit()
        return row.id


@pytest.mark.asyncio
async def test_a_message_sent_during_a_web_turn_is_queued_then_answered_with_what_that_turn_left(
    monkeypatch, fake_redis
):
    stub = _stub_channel_turn(monkeypatch)
    monkeypatch.setattr(stub.router, "_resolve_channel_agent", stub.resolve)
    asked, delivered = _run_queue_inline(monkeypatch, stub.router)
    relayed: list[dict] = []
    monkeypatch.setattr(
        chat_turn_stream, "publish_live", lambda _cid, frame, *, origin=None: relayed.append(frame)
    )
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        await _bind(db.sessions, w)
        async with db.sessions() as session:
            handed_to = _agent(w.owner)
            session.add(handed_to)
            await session.commit()
            handed_to_id, handed_to_name = handed_to.id, handed_to.name
        conv_id = str(w.conv["web"])
        web_token = await chat_turn_guard.acquire_turn(conv_id)
        before = await _messages(db.sessions, w.conv["web"])

        ack = await stub.router.handle_message(_slack_dm("are you there?", email=w.owner_email))
        queued = await _messages(db.sessions, w.conv["web"])
        assert ack.text == channel_continuation.QUEUED_NOTICE and not ack.suppress
        assert queued == before, "nothing is stored while the message waits"
        stub.run.assert_not_awaited()
        assert fake_redis.kv[chat_turn_guard.processing_key(conv_id)] == web_token
        await _drain_all(asked)  # asked for at queueing: finds the turn taken
        assert delivered == []

        # The web turn hands the conversation to another agent, saves another
        # tool selection, and ends.
        async with db.sessions() as session:
            conv = await session.get(Conversation, w.conv["web"])
            conv.agent_id = handed_to_id
            conv.tool_overrides_json = json.dumps(
                {"builtin_tool_ids": ["read_url"], "agent_id": str(handed_to_id)}
            )
            await session.commit()
        await chat_turn_guard.release_turn(conv_id, web_token)
        assert asked == [conv_id]
        await _drain_all(asked)
        after = await _messages(db.sessions, w.conv["web"])

    (reply,) = delivered
    assert reply.text == f"{handed_to_name} answers are you there?"
    assert stub.run.await_args.args[2].id == handed_to_id
    assert stub.run.await_args.kwargs["session_builtin_tool_ids"] == ["read_url"]
    assert after[len(before):] == [
        ("user", "are you there?", "slack"),
        ("assistant", f"{handed_to_name} answers are you there?", "slack"),
    ]
    # Shown live on the web like any channel turn.
    (publisher,) = _Publisher.instances
    assert publisher.frames[-1]["type"] == "done" and publisher.frames[-1]["channel"] == "slack"
    (shown,) = [f["message"] for f in relayed if f["type"] == "user_message"]
    assert shown["channel"] == "slack"
    assert chat_turn_guard.processing_key(conv_id) not in fake_redis.kv


@pytest.mark.asyncio
async def test_a_queued_message_whose_thread_was_released_is_never_answered_elsewhere(monkeypatch, fake_redis):
    stub = _stub_channel_turn(monkeypatch)
    monkeypatch.setattr(stub.router, "_resolve_channel_agent", AsyncMock(return_value=None))
    asked, delivered = _run_queue_inline(monkeypatch, stub.router)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        await _bind(db.sessions, w)
        conv_id = str(w.conv["web"])
        web_token = await chat_turn_guard.acquire_turn(conv_id)
        ack = await stub.router.handle_message(_slack_dm("and the figures?", email=w.owner_email))
        assert ack.text == channel_continuation.QUEUED_NOTICE
        before = await _messages(db.sessions, w.conv["web"])
        # The thread stops continuing the conversation before the message runs.
        async with db.sessions() as session:
            assert await channel_continuation.unbind_conversation(session, w.conv["web"], "slack")
            await session.commit()
        await chat_turn_guard.release_turn(conv_id, web_token)
        await _drain_all(asked)
        after = await _messages(db.sessions, w.conv["web"])
        async with db.sessions() as session:
            created = (
                await session.execute(
                    select(func.count())
                    .select_from(Conversation)
                    .where(Conversation.channel_thread_id == SLACK_DM)
                )
            ).scalar_one()

    (reply,) = delivered
    assert reply.text == notice("channel_queued_elsewhere")
    assert after == before and created == 0, "answered in no conversation"
    stub.run.assert_not_awaited()
    assert chat_turn_guard.processing_key(conv_id) not in fake_redis.kv


@pytest.mark.asyncio
async def test_a_queued_message_keeps_its_files_without_their_urls(monkeypatch, fake_redis):
    stub = _stub_channel_turn(monkeypatch)
    monkeypatch.setattr(stub.router, "_resolve_channel_agent", stub.resolve)
    files = _store_files_for_real(monkeypatch, stub.router)
    asked, delivered = _run_queue_inline(monkeypatch, stub.router)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        await _bind(db.sessions, w)
        conv_id = str(w.conv["web"])
        web_token = await chat_turn_guard.acquire_turn(conv_id)
        ack = await stub.router.handle_message(_with_timeline(_slack_dm("see the timeline", email=w.owner_email)))
        assert ack.text == channel_continuation.QUEUED_NOTICE
        (queued,) = fake_redis.lists[chat_turn_guard.channel_queue_key(conv_id)]
        (waiting,) = await _files_of(db.sessions, w.owner)
        await chat_turn_guard.release_turn(conv_id, web_token)
        await _drain_all(asked)
        (kept,) = await _files_of(db.sessions, w.owner)
        async with db.sessions() as session:
            shown_on = (
                await session.execute(select(Message.role, Message.content).where(Message.id == kept.message_id))
            ).one()

    # Downloaded once, while the URL worked; the queue keeps the file's id only.
    assert files.download.await_count == 1
    assert "short-lived-secret" not in queued and "files.example.test" not in queued
    assert json.loads(queued)["file_ids"] == [str(waiting.id)]
    assert waiting.source == channel_continuation.queued_file_source("slack") and waiting.message_id is None
    (reply,) = delivered
    assert not reply.suppress
    (sent,) = stub.run.await_args.kwargs["files"]
    assert str(sent.file_id) == str(waiting.id) and sent.filename == "timeline.txt"
    # Claimed by its turn and shown on the message it stored: a channel upload
    # like any other, which the end of the drain keeps.
    assert kept.id == waiting.id and kept.source == "channel:slack" and kept.conversation_id == w.conv["web"]
    assert tuple(shown_on) == ("user", "see the timeline")
    assert files.deleted == []


@pytest.mark.asyncio
async def test_a_redelivered_message_is_queued_once(monkeypatch, fake_redis):
    stub = _stub_channel_turn(monkeypatch)
    files = _store_files_for_real(monkeypatch, stub.router)
    _run_queue_inline(monkeypatch, stub.router)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        await _bind(db.sessions, w)
        conv_id = str(w.conv["web"])
        await chat_turn_guard.acquire_turn(conv_id)
        first = await stub.router.handle_message(_with_timeline(_slack_dm("are you there?", email=w.owner_email)))
        again = await stub.router.handle_message(_with_timeline(_slack_dm("are you there?", email=w.owner_email)))
        stored = await _files_of(db.sessions, w.owner)

    assert first.text == channel_continuation.QUEUED_NOTICE
    assert again.suppress, "a Slack retry of the same message is not acknowledged twice"
    assert len(fake_redis.lists[chat_turn_guard.channel_queue_key(conv_id)]) == 1
    # Its files are stored once, for the message queued: the retry stores none.
    assert files.download.await_count == 1 and len(stored) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ending",
    [
        "queue_full",
        "queue_unreachable",
        "thread_released",
        "conversation_deleted",
        "never_reached",
        "undeliverable",
    ],
)
async def test_the_files_of_a_queued_message_that_never_runs_are_deleted(monkeypatch, fake_redis, ending):
    stub = _stub_channel_turn(monkeypatch)
    monkeypatch.setattr(stub.router, "_resolve_channel_agent", AsyncMock(return_value=None))
    files = _store_files_for_real(monkeypatch, stub.router)
    asked, delivered = _run_queue_inline(monkeypatch, stub.router)
    told = AsyncMock()
    monkeypatch.setattr(channel_continuation, "_tell_dropped", told)
    if ending == "queue_full":  # filled up between the check and the queueing
        monkeypatch.setattr(chat_turn_guard, "enqueue_channel_message", AsyncMock(return_value=None))
    elif ending == "queue_unreachable":
        monkeypatch.setattr(
            chat_turn_guard, "enqueue_channel_message", AsyncMock(side_effect=ConnectionError("Redis down"))
        )
    elif ending == "never_reached":  # the adapter returned without the router (a failure it caught)
        monkeypatch.setattr(channel_continuation, "_run_queued_message", AsyncMock())
    elif ending == "undeliverable":  # no Slack bot any more: started twice, then dropped
        monkeypatch.setattr(
            channel_continuation,
            "_run_queued_message",
            AsyncMock(side_effect=channel_continuation.QueuedMessageUndeliverableError("no Slack bot")),
        )
    queued = ending in ("thread_released", "conversation_deleted", "never_reached", "undeliverable")
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        await _bind(db.sessions, w)
        conv_id = str(w.conv["web"])
        web_token = await chat_turn_guard.acquire_turn(conv_id)
        ack = await stub.router.handle_message(_with_timeline(_slack_dm("see the timeline", email=w.owner_email)))
        if queued:
            assert ack.text == channel_continuation.QUEUED_NOTICE
            (waiting,) = await _files_of(db.sessions, w.owner)
            assert waiting.source == channel_continuation.queued_file_source("slack")
            assert files.deleted == []
            async with db.sessions() as session:
                if ending == "thread_released":
                    assert await channel_continuation.unbind_conversation(session, w.conv["web"], "slack")
                elif ending == "conversation_deleted":
                    await session.execute(delete(Conversation).where(Conversation.id == w.conv["web"]))
                await session.commit()
            await chat_turn_guard.release_turn(conv_id, web_token)
            await _drain_all(asked)
        left = await _files_of(db.sessions, w.owner)

    if ending == "queue_full":
        assert ack.text == channel_continuation.QUEUE_FULL_NOTICE
    elif ending == "queue_unreachable":
        assert ack.text == channel_continuation.TURN_BUSY_NOTICE
    elif ending == "never_reached":
        assert delivered == []
    elif ending == "undeliverable":
        assert delivered == []
        assert channel_continuation._run_queued_message.await_count == 2
        ((dropped,), _kw) = told.await_args
        assert [item["text"] for item in dropped] == ["see the timeline"]
    else:
        (reply,) = delivered
        assert reply.text == notice("channel_queued_elsewhere")
    if ending != "undeliverable":
        told.assert_not_awaited()
    stub.run.assert_not_awaited()
    # Stored once, then the row and the object both gone.
    assert len(files.uploaded) == 1
    assert left == [] and files.deleted == files.uploaded


@pytest.mark.asyncio
async def test_the_upload_purge_deletes_only_the_files_of_queued_messages_that_never_ran(monkeypatch):
    deleted = _deleted_objects(monkeypatch)
    now = datetime.now(UTC)
    expired = now - channel_continuation.QUEUED_FILE_RETENTION - timedelta(minutes=5)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            message = Message(conversation_id=w.conv["web"], role="user", content="see attached", channel="slack", sequence=3)
            session.add(message)
            await session.commit()
        ids = {
            # The queue entry expired, or was lost, with the ids it held.
            "never_ran": await _queued_file(db.sessions, w.owner, created_at=expired),
            "teams_never_ran": await _queued_file(
                db.sessions, w.owner, created_at=expired, source=channel_continuation.queued_file_source("teams")
            ),
            "still_waiting": await _queued_file(db.sessions, w.owner, created_at=now - timedelta(minutes=30)),
            "claimed": await _queued_file(db.sessions, w.owner, created_at=expired, source="channel:slack"),
            "on_a_message": await _queued_file(
                db.sessions, w.owner, created_at=expired, conversation_id=w.conv["web"], message_id=message.id
            ),
            "chat_upload": await _queued_file(db.sessions, w.owner, created_at=expired, source="chat"),
        }
        async with db.sessions() as session:
            keys = dict(
                (await session.execute(select(FileAttachment.id, FileAttachment.s3_object_name))).tuples().all()
            )
        assert await widget_uploads.purge_expired_uploads(now=now) == 2
        async with db.sessions() as session:
            left = set((await session.execute(select(FileAttachment.id))).scalars())

    assert left == {ids[name] for name in ("still_waiting", "claimed", "on_a_message", "chat_upload")}
    assert sorted(deleted) == sorted(keys[ids[name]] for name in ("never_ran", "teams_never_ran"))


@pytest.mark.asyncio
async def test_a_turn_claiming_a_file_the_purge_holds_runs_without_it(monkeypatch):
    from app.services.file_storage import file_storage

    entered, proceed = asyncio.Event(), asyncio.Event()
    deleted: list[str] = []

    async def _slow_delete(key):
        entered.set()
        await proceed.wait()
        deleted.append(key)

    monkeypatch.setattr(file_storage, "delete", _slow_delete)
    now = datetime.now(UTC)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        file_id = await _queued_file(
            db.sessions, w.owner, created_at=now - channel_continuation.QUEUED_FILE_RETENTION - timedelta(minutes=1)
        )

        async def _claim():
            async with db.sessions() as session:
                rows = await channel_continuation.claim_queued_files(
                    session, [str(file_id)], channel="slack", user_id=w.owner
                )
                await session.commit()
                return rows

        purge = asyncio.create_task(widget_uploads.purge_expired_uploads(now=now))
        await asyncio.wait_for(entered.wait(), 10)  # the purge holds the row, deleting its object
        claim = asyncio.create_task(_claim())
        await asyncio.sleep(0.5)
        assert not claim.done(), "the claim waits for the row the purge holds"
        proceed.set()
        assert await asyncio.wait_for(purge, 10) == 1
        claimed = await asyncio.wait_for(claim, 10)
        async with db.sessions() as session:
            gone = await session.get(FileAttachment, file_id) is None

    assert claimed == [] and gone and len(deleted) == 1


@pytest.mark.asyncio
async def test_the_purge_never_takes_a_file_a_turn_claimed_and_attached(monkeypatch):
    from app.services.sub_agent_files import bind_attachment_rows

    deleted = _deleted_objects(monkeypatch)
    now = datetime.now(UTC)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        file_id = await _queued_file(
            db.sessions, w.owner, created_at=now - channel_continuation.QUEUED_FILE_RETENTION - timedelta(minutes=1)
        )
        async with db.sessions() as turn:
            (claimed,) = await channel_continuation.claim_queued_files(
                turn, [str(file_id)], channel="slack", user_id=w.owner
            )
            # Claimed, not yet committed: the purge skips the row the turn holds.
            assert await widget_uploads.purge_expired_uploads(now=now) == 0
            message = await channel_router.store_message(
                turn, w.conv["web"], role="user", content="see the timeline", channel="slack"
            )
            assert await bind_attachment_rows(turn, [claimed], conversation_id=w.conv["web"], message_id=message.id)
            await turn.commit()
        assert await widget_uploads.purge_expired_uploads(now=now + timedelta(days=30)) == 0
        assert await channel_continuation.discard_queued_files([str(file_id)]) == 0
        async with db.sessions() as session:
            kept = await session.get(FileAttachment, file_id)

    assert (kept.source, kept.message_id, kept.conversation_id) == ("channel:slack", message.id, w.conv["web"])
    assert deleted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["answers", "unreachable"])
async def test_a_file_its_turn_claimed_and_left_unattached_is_deleted_or_left_to_the_purge(monkeypatch, store):
    from app.services.file_storage import file_storage

    deleted = _deleted_objects(monkeypatch)
    recording_delete = file_storage.delete
    if store == "unreachable":

        async def _down(_key):
            raise ConnectionError("object store unreachable")

        monkeypatch.setattr(file_storage, "delete", _down)
    now = datetime.now(UTC)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        file_id = await _queued_file(db.sessions, w.owner, created_at=now)
        async with db.sessions() as turn:
            (claimed,) = await channel_continuation.claim_queued_files(
                turn, [str(file_id)], channel="slack", user_id=w.owner
            )
            key = claimed.s3_object_name
            await turn.commit()  # the turn ended without attaching it
        removed = await channel_continuation.discard_queued_files([str(file_id)])
        async with db.sessions() as session:
            left = await session.get(FileAttachment, file_id)
        purged = 0
        if store == "unreachable":
            monkeypatch.setattr(file_storage, "delete", recording_delete)  # the store answers again
            later = now + channel_continuation.QUEUED_FILE_RETENTION + timedelta(minutes=1)
            purged = await widget_uploads.purge_expired_uploads(now=later)
            async with db.sessions() as session:
                assert await session.get(FileAttachment, file_id) is None

    if store == "answers":
        assert removed == 1 and left is None
    else:
        # The object may still exist: its row stays, marked again, for the purge.
        assert removed == 0 and left.source == channel_continuation.queued_file_source("slack")
        assert purged == 1
    assert deleted == [key]


@pytest.mark.asyncio
async def test_a_direct_message_on_another_channel_runs_as_it_always_did(monkeypatch, fake_redis):
    """Telegram, WhatsApp, Discord and Google Chat are not continued across
    surfaces: a direct message there takes no turn lock, so a burst of
    messages is answered as it comes, never told to send again later."""
    stub = _stub_channel_turn(monkeypatch)
    general = AsyncMock(return_value=("the General Assistant answers", None))
    monkeypatch.setattr("app.services.general_chat.run_general_chat", general)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            telegram = Conversation(
                user_id=w.owner, channel="telegram", channel_thread_id="424242", title="Telegram chat"
            )
            session.add(telegram)
            await session.commit()
            telegram_id = str(telegram.id)
        # The previous message of the chat is still being answered.
        running = await chat_turn_guard.acquire_turn(telegram_id)
        out = await stub.router.handle_message(
            InboundMessage(
                channel=ChannelType.TELEGRAM,
                text="and one more thing",
                user_email=w.owner_email,
                channel_user_id="424242",
                channel_thread_id="424242",
                metadata={"chat_type": "private"},
            )
        )
        stored = await _messages(db.sessions, telegram.id)

    assert out.text == "the General Assistant answers"
    assert out.metadata["conversation_id"] == telegram_id
    assert stored[-2:] == [
        ("user", "and one more thing", "telegram"),
        ("assistant", "the General Assistant answers", "telegram"),
    ]
    assert fake_redis.kv[chat_turn_guard.processing_key(telegram_id)] == running
    assert _Publisher.instances == []


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True], ids=["direct", "queued"])
async def test_a_tool_call_left_waiting_for_approval_is_retired_as_a_web_send_retires_it(
    monkeypatch, fake_redis, queued
):
    from app.services.chat_approval_resume import PENDING_KEY, load_pending

    stub = _stub_channel_turn(monkeypatch)
    monkeypatch.setattr(stub.router, "_resolve_channel_agent", stub.resolve)
    asked, delivered = _run_queue_inline(monkeypatch, stub.router)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        await _bind(db.sessions, w)
        conv_id = str(w.conv["web"])
        if queued:  # sent while the turn that proposed the call still ran
            web_token = await chat_turn_guard.acquire_turn(conv_id)
            ack = await stub.router.handle_message(_slack_dm("go ahead", email=w.owner_email))
            assert ack.text == channel_continuation.QUEUED_NOTICE
        # That turn ended at the gate: the call waits for a decision on the web.
        async with db.sessions() as session:
            session.add(
                Message(
                    conversation_id=w.conv["web"],
                    role="assistant",
                    content="I need your approval.",
                    channel="web",
                    sequence=3,
                    metadata_json=json.dumps(
                        {PENDING_KEY: [{"tool_call_id": "call-1", "tool_name": "delete", "arguments": {}}]}
                    ),
                )
            )
            await session.commit()
        if queued:
            await chat_turn_guard.release_turn(conv_id, web_token)
            await _drain_all(asked)
            (out,) = delivered
        else:
            out = await stub.router.handle_message(_slack_dm("go ahead", email=w.owner_email))
        async with db.sessions() as session:
            still_pending = await load_pending(session, w.conv["web"])

    # As a web message that takes the turn: the call is retired (FR-002c4)
    # and the message answered, the reply saying what was dropped.
    assert still_pending is None
    assert out.text.startswith(notice("channel_approval_retired"))
    assert out.text.endswith("answers go ahead")
    stub.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_slack_direct_message_thread_keeps_its_web_turns_in_its_history(monkeypatch, fake_redis):
    """A DM thread reply loads the stored history (the web turns included),
    not only the Slack thread's own copy."""
    stub = _stub_channel_turn(monkeypatch)
    monkeypatch.setattr(stub.router, "_resolve_channel_agent", AsyncMock(return_value=None))
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            session.add(
                Message(conversation_id=w.conv["slack_dm"], role="user", content="written on the web", channel="web", sequence=3)
            )
            await session.commit()
        stub.router._load_channel_backfill.return_value = "--- Prior Slack thread messages ---"
        await stub.router.handle_message(
            _slack_dm("continue", thread="D0ORIGIN:1700000000.000300", email=w.owner_email)
        )

    history = stub.run.await_args.args[3]
    assert [m.content for m in history][-1] == "written on the web"


@pytest.mark.asyncio
async def test_new_in_a_bound_thread_releases_it_and_keeps_the_conversation(monkeypatch, fake_redis):
    stub = _stub_channel_turn(monkeypatch)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        await _bind(db.sessions, w)
        out = await stub.router.handle_message(_slack_dm("/new", email=w.owner_email))
        async with db.sessions() as session:
            bindings = (await session.execute(select(func.count()).select_from(ConversationChannelBinding))).scalar()
            conv = await session.get(Conversation, w.conv["web"])
            audit = await _audit(session, w.conv["web"])

    assert out.text.startswith("Conversation reset!")
    assert bindings == 0
    assert conv.is_archived is False
    assert [a.action for a in audit] == ["unbind"]
    assert audit[0].details["channel"] == "slack" and "/new" in audit[0].summary


# ── Continue in a channel: the endpoints ────────────────────────────────────


def _bots(monkeypatch, *providers: str) -> None:
    async def _bot(provider):
        return SimpleNamespace(id=uuid.uuid4()) if provider in providers else None

    monkeypatch.setattr(continuation_api, "get_channel_bot", _bot)


@pytest.mark.asyncio
async def test_continuing_in_slack_binds_the_thread_the_bot_opened(monkeypatch, fake_redis):
    _bots(monkeypatch, "slack_bot", "teams_bot")
    opened = AsyncMock(return_value="D0NEW:1700000001.000100")
    monkeypatch.setattr("app.channels.slack.open_slack_dm_thread", opened)
    monkeypatch.setattr(
        "app.channels.teams.get_personal_conversation_reference", AsyncMock(return_value=None)
    )
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            owner = await session.get(User, w.owner)
            before = await continuation_api.get_channel_bindings(str(w.conv["web"]), user=owner, db=session)
            created = await continuation_api.create_channel_binding(
                str(w.conv["web"]),
                continuation_api.ChannelBindingCreate(channel="slack"),
                user=owner,
                db=session,
                _chat_gate=None,
            )
        async with db.sessions() as session:
            owner = await session.get(User, w.owner)
            after = await continuation_api.get_channel_bindings(str(w.conv["web"]), user=owner, db=session)
            found = await channel_continuation.find_bound_conversation(
                session, "slack", "D0NEW:1700000001.000100", user_id=w.owner
            )
            audit = (
                await session.execute(select(AuditLog.action).where(AuditLog.entity_id == w.conv["web"]))
            ).scalars().all()

    assert {c.channel: c.available for c in before.channels} == {"slack": True, "teams": False}
    # The conversation's agent answers on Slack only.
    assert {c.channel: c.reason for c in before.channels}["teams"] == "agent_unavailable"
    assert created.channel == "slack"
    email, text = opened.await_args.args
    assert email == w.owner_email and "Quarterly review" in text and "Reply in this thread" in text
    assert [b.channel for b in after.bindings] == ["slack"]
    assert found is not None and found.id == w.conv["web"]
    assert "bind" in audit


@pytest.mark.asyncio
async def test_a_thread_that_keeps_changing_while_it_is_bound_is_a_409(monkeypatch, fake_redis):
    _bots(monkeypatch, "slack_bot")
    monkeypatch.setattr(
        "app.channels.slack.open_slack_dm_thread", AsyncMock(return_value="D0NEW:1700000001.000100")
    )
    monkeypatch.setattr(
        "app.channels.teams.get_personal_conversation_reference", AsyncMock(return_value=None)
    )
    refusal = channel_continuation.ChannelContinuationError(
        channel_continuation.REASON_DELIVERY_FAILED, "The Slack thread kept changing while it was linked."
    )
    monkeypatch.setattr(continuation_api, "bind_thread", AsyncMock(side_effect=refusal))
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            owner = await session.get(User, w.owner)
            with pytest.raises(HTTPException) as exc:
                await continuation_api.create_channel_binding(
                    str(w.conv["web"]),
                    continuation_api.ChannelBindingCreate(channel="slack"),
                    user=owner,
                    db=session,
                    _chat_gate=None,
                )

    assert (exc.value.status_code, exc.value.detail) == (409, refusal.detail), "never a 500"


@pytest.mark.asyncio
@pytest.mark.parametrize("replied", [True, False], ids=["replied", "died_before_replying"])
async def test_a_queued_message_a_dying_process_stored_is_never_answered_twice(monkeypatch, fake_redis, replied):
    stub = _stub_channel_turn(monkeypatch)
    asked, delivered = _run_queue_inline(monkeypatch, stub.router)
    told = AsyncMock()
    monkeypatch.setattr(channel_continuation, "_tell_dropped", told)
    message = _slack_dm("are you there?", email=None)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        await _bind(db.sessions, w)
        conv_id = str(w.conv["web"])
        message.user_email = w.owner_email
        # A drain took the message and its turn stored it; then the process died.
        await chat_turn_guard.enqueue_channel_message(conv_id, channel_continuation._queue_item(message))
        token = await chat_turn_guard.acquire_turn(conv_id, channel="slack")
        assert await chat_turn_guard.claim_queued_channel_message(conv_id, token)
        async with db.sessions() as session:
            stored = [
                Message(
                    conversation_id=w.conv["web"],
                    role="user",
                    content=message.text,
                    channel="slack",
                    sequence=3,
                    channel_message_id=message.channel_message_id,
                )
            ]
            if replied:
                stored.append(
                    Message(conversation_id=w.conv["web"], role="assistant", content="yes", channel="slack", sequence=4)
                )
            session.add_all(stored)
            await session.commit()
        del fake_redis.kv[chat_turn_guard.processing_key(conv_id)]  # its lock's TTL ran out

        assert await channel_continuation.sweep_channel_queues() == 1
        await _drain_all(asked)
        rows = await _messages(db.sessions, w.conv["web"])

    assert delivered == [] and stub.run.await_count == 0, "not started again"
    assert [content for _role, content, _channel in rows].count("are you there?") == 1
    # Replied: answered. Not: its sender is told to send it again.
    assert told.await_count == (0 if replied else 1)
    assert not fake_redis.lists.get(chat_turn_guard.channel_queue_claim_key(conv_id))


@pytest.mark.asyncio
async def test_whether_a_dead_turn_stored_its_queued_message_is_read_off_the_conversation():
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        conv = w.conv["web"]
        async with db.sessions() as session:
            session.add_all(
                [
                    Message(conversation_id=conv, role="user", content="answered", channel="slack", sequence=3, channel_message_id="1700000000.0003"),
                    Message(conversation_id=conv, role="assistant", content="re: answered", channel="slack", sequence=4),
                    Message(conversation_id=conv, role="user", content="cut short", channel="slack", sequence=5, channel_message_id="1700000000.0005"),
                ]
            )
            await session.commit()

        def _items(*ids):
            return [{"text": "x", "channel_message_id": mid} for mid in ids]

        answered = await channel_continuation._stored_turn(str(conv), _items("1700000000.0001", "1700000000.0003"))
        cut_short = await channel_continuation._stored_turn(str(conv), _items("1700000000.0005"))
        never = await channel_continuation._stored_turn(str(conv), _items("1700000000.0009"))
        elsewhere = await channel_continuation._stored_turn(str(w.conv["web_quiet"]), _items("1700000000.0003"))

    assert (answered, cut_short, never, elsewhere) == (True, False, None, None)


@pytest.mark.asyncio
async def test_continuing_in_teams_needs_the_personal_chat_and_binds_it(monkeypatch, fake_redis):
    """A General Assistant conversation answers in every direct message; Teams
    still needs the personal chat the user opened with the bot."""
    _bots(monkeypatch, "teams_bot")
    reference = AsyncMock(return_value=None)
    monkeypatch.setattr("app.channels.teams.get_personal_conversation_reference", reference)
    posted = AsyncMock(return_value=TEAMS_PERSONAL)
    monkeypatch.setattr("app.channels.teams.post_to_teams_personal_chat", posted)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        # The personal chat continued another conversation until now.
        await _bind(db.sessions, w, "web", thread=TEAMS_PERSONAL, channel="teams")
        async with db.sessions() as session:
            owner = await session.get(User, w.owner)
            unknown = await continuation_api.get_channel_bindings(str(w.conv["general"]), user=owner, db=session)
            reference.return_value = {"service_url": "https://smba.trafficmanager.net/emea/", "conversation_id": TEAMS_PERSONAL}
            known = await continuation_api.get_channel_bindings(str(w.conv["general"]), user=owner, db=session)
            await continuation_api.create_channel_binding(
                str(w.conv["general"]),
                continuation_api.ChannelBindingCreate(channel="teams"),
                user=owner,
                db=session,
                _chat_gate=None,
            )
        async with db.sessions() as session:
            found = await channel_continuation.find_bound_conversation(
                session, "teams", TEAMS_PERSONAL, user_id=w.owner
            )
            released = await _audit(session, w.conv["web"])
            bound = await _audit(session, w.conv["general"])

    teams = {c.channel: c for c in unknown.channels}["teams"]
    assert teams.available is False and teams.reason == "teams_chat_unknown"
    assert {c.channel: c for c in known.channels}["teams"].available is True
    assert {c.channel: c for c in known.channels}["slack"].reason == "not_configured"
    assert "until you send /new" in posted.await_args.args[1]
    assert found is not None and found.id == w.conv["general"]
    # The conversation the chat no longer continues is told apart in the audit.
    assert [a.action for a in released] == ["unbind"]
    assert released[0].details["channel"] == "teams"
    assert [a.action for a in bound] == ["bind"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "bots", "reason"),
    [
        ("slack_group", ("slack_bot",), "not_continuable"),
        ("web", (), "not_configured"),
        ("web_quiet", ("slack_bot",), "agent_unavailable"),
    ],
    ids=["group_thread", "no_bot", "agent_not_on_slack"],
)
async def test_a_conversation_is_not_continued_where_it_cannot_be(monkeypatch, fake_redis, name, bots, reason):
    _bots(monkeypatch, *bots)
    opened = AsyncMock(side_effect=AssertionError("nothing is posted"))
    monkeypatch.setattr("app.channels.slack.open_slack_dm_thread", opened)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            owner = await session.get(User, w.owner)
            state = await continuation_api.get_channel_bindings(str(w.conv[name]), user=owner, db=session)
            with pytest.raises(HTTPException) as exc:
                await continuation_api.create_channel_binding(
                    str(w.conv[name]),
                    continuation_api.ChannelBindingCreate(channel="slack"),
                    user=owner,
                    db=session,
                    _chat_gate=None,
                )

    slack = next(c for c in state.channels if c.channel == "slack")
    assert slack.available is False and slack.reason == reason
    assert exc.value.status_code == 409 and exc.value.detail == slack.detail
    opened.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_teams_personal_chat_is_not_continued_in_itself(monkeypatch, fake_redis):
    """The conversation IS the user's personal chat with the Teams bot: a
    binding would point the chat at itself, and /new there would archive it."""
    _bots(monkeypatch, "slack_bot", "teams_bot")
    monkeypatch.setattr(
        "app.channels.teams.get_personal_conversation_reference",
        AsyncMock(return_value={"service_url": "https://smba.trafficmanager.net/emea/", "conversation_id": TEAMS_PERSONAL}),
    )
    posted = AsyncMock(side_effect=AssertionError("nothing is posted"))
    monkeypatch.setattr("app.channels.teams.post_to_teams_personal_chat", posted)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            owner = await session.get(User, w.owner)
            state = await continuation_api.get_channel_bindings(
                str(w.conv["teams_personal"]), user=owner, db=session
            )
            with pytest.raises(HTTPException) as exc:
                await continuation_api.create_channel_binding(
                    str(w.conv["teams_personal"]),
                    continuation_api.ChannelBindingCreate(channel="teams"),
                    user=owner,
                    db=session,
                    _chat_gate=None,
                )
            # Another conversation is offered that chat as before.
            other = await continuation_api.get_channel_bindings(str(w.conv["general"]), user=owner, db=session)

    teams = next(c for c in state.channels if c.channel == "teams")
    assert (teams.available, teams.reason) == (False, "started_there")
    assert exc.value.status_code == 409 and exc.value.detail == teams.detail
    posted.assert_not_awaited()
    assert next(c for c in other.channels if c.channel == "teams").available


_WHITELIST = [{"id": "C0SUPPORT", "name": "support"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bot_settings", "agent_slack", "reason"),
    [
        ({}, {}, None),
        ({"allowed_receive_channels": _WHITELIST}, {}, None),
        ({"allowed_receive_channels": _WHITELIST, "allow_private": False}, {}, "direct_messages_off"),
        (
            {"allowed_receive_channels": _WHITELIST, "allow_private": False},
            {"bypass_whitelist": True},
            None,
        ),
        ({"only_mention": True}, {}, None),
        ({}, {"receive_channels": _WHITELIST, "allow_private": False}, "agent_unavailable"),
    ],
    ids=[
        "default_bot",
        "whitelist_with_private",
        "whitelist_without_private",
        "agent_bypasses_whitelist",
        "only_mention",
        "agent_keeps_private_out",
    ],
)
async def test_the_option_is_offered_exactly_where_the_router_answers_the_thread(
    monkeypatch, fake_redis, bot_settings, agent_slack, reason
):
    """The availability and the router read one predicate (``direct_message_refusal``):
    for every bot and agent setting, the option is offered if and only if a
    message in the continued thread is answered."""
    from app.channels import router as router_module

    real_receive_gate = router_module._bot_allows_receive
    stub = _stub_channel_turn(monkeypatch)
    monkeypatch.setattr(stub.router, "_bot_allows_receive", real_receive_gate)
    monkeypatch.setattr(stub.router, "_resolve_channel_agent", stub.resolve)
    bot = SimpleNamespace(
        **{
            "id": uuid.uuid4(),
            "only_mention": False,
            "allow_anonymous": False,
            "allow_private": True,
            "allowed_receive_channels": None,
            "is_company_managed": True,
            "requires_approval": True,
            **bot_settings,
        }
    )
    monkeypatch.setattr(stub.router, "_get_channel_bot", AsyncMock(return_value=bot))
    _bots(monkeypatch, "slack_bot")
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            agent = await session.get(Agent, w.agent)
            agent.channel_config = {
                "slack": {"receive_enabled": True, "publish_enabled": True, **agent_slack}
            }
            await session.commit()
        await _bind(db.sessions, w)
        async with db.sessions() as session:
            owner = await session.get(User, w.owner)
            state = await continuation_api.get_channel_bindings(str(w.conv["web"]), user=owner, db=session)
        await stub.router.handle_message(_slack_dm("still there?", email=w.owner_email))

    slack = next(c for c in state.channels if c.channel == "slack")
    assert slack.reason == reason
    assert slack.available is (reason is None)
    answered = stub.run.await_count == 1
    assert answered is slack.available, "offered exactly where the router answers"


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation", ["web", "general"], ids=["its_agent", "general_assistant"])
async def test_a_direct_message_let_in_for_another_agent_is_neither_offered_nor_answered_here(
    monkeypatch, fake_redis, conversation
):
    """Off the bot's whitelist, a message is let in for the agents that bypass it only:
    not for the conversation's own agent without ``bypass_whitelist``, nor the
    General Assistant."""
    from app.channels import router as router_module

    real_receive_gate = router_module._bot_allows_receive
    stub = _stub_channel_turn(monkeypatch)
    monkeypatch.setattr(stub.router, "_bot_allows_receive", real_receive_gate)
    monkeypatch.setattr(stub.router, "_resolve_channel_agent", stub.resolve)
    general = AsyncMock(return_value=("The General Assistant answers", None))
    monkeypatch.setattr("app.services.general_chat.run_general_chat", general)
    bot = SimpleNamespace(
        id=uuid.uuid4(),
        only_mention=False,
        allow_anonymous=False,
        allow_private=False,
        allowed_receive_channels=_WHITELIST,
        is_company_managed=True,
        requires_approval=True,
    )
    monkeypatch.setattr(stub.router, "_get_channel_bot", AsyncMock(return_value=bot))
    _bots(monkeypatch, "slack_bot")
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            bypassing = _agent(w.owner)
            bypassing.channel_config = {
                "slack": {"receive_enabled": True, "publish_enabled": True, "bypass_whitelist": True}
            }
            session.add(bypassing)
            await session.commit()
        await _bind(db.sessions, w, conversation)
        async with db.sessions() as session:
            owner = await session.get(User, w.owner)
            state = await continuation_api.get_channel_bindings(
                str(w.conv[conversation]), user=owner, db=session
            )
        reply = await stub.router.handle_message(_slack_dm("still there?", email=w.owner_email))

    slack = next(c for c in state.channels if c.channel == "slack")
    assert (slack.available, slack.reason) == (False, channel_continuation.REASON_DIRECT_MESSAGES_OFF)
    stub.run.assert_not_awaited()
    general.assert_not_awaited()
    if conversation == "general":
        assert reply.suppress, "silent, as the whitelist is"
    else:
        assert reply.text == "This agent is not available on the slack channel."


@pytest.mark.asyncio
async def test_only_the_owner_continues_a_conversation_in_a_channel(monkeypatch, fake_redis):
    _bots(monkeypatch, "slack_bot")
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            other = await session.get(User, w.other)
            for call in (
                continuation_api.get_channel_bindings(str(w.conv["web"]), user=other, db=session),
                continuation_api.create_channel_binding(
                    str(w.conv["web"]),
                    continuation_api.ChannelBindingCreate(channel="slack"),
                    user=other,
                    db=session,
                    _chat_gate=None,
                ),
                continuation_api.delete_channel_binding(str(w.conv["anonymous"]), "slack", user=other, db=session),
            ):
                with pytest.raises(HTTPException) as exc:
                    await call
                assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_stopping_a_continuation_releases_its_thread(monkeypatch, fake_redis):
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        await _bind(db.sessions, w)
        async with db.sessions() as session:
            owner = await session.get(User, w.owner)
            await continuation_api.delete_channel_binding(str(w.conv["web"]), "slack", user=owner, db=session)
        async with db.sessions() as session:
            found = await channel_continuation.find_bound_conversation(
                session, "slack", SLACK_DM, user_id=w.owner
            )

    assert found is None


@pytest.mark.asyncio
async def test_a_teams_personal_chat_follows_the_conversation_continued_last():
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        await _bind(db.sessions, w, "web", thread=TEAMS_PERSONAL, channel="teams")
        async with db.sessions() as session:
            again = await channel_continuation.bind_thread(
                session, conversation_id=w.conv["web"], user_id=w.owner, channel="teams", channel_thread_id=TEAMS_PERSONAL
            )
            moved = await channel_continuation.bind_thread(
                session, conversation_id=w.conv["slack_dm"], user_id=w.owner, channel="teams", channel_thread_id=TEAMS_PERSONAL
            )
            await session.commit()
        async with db.sessions() as session:
            found = await channel_continuation.find_bound_conversation(
                session, "teams", TEAMS_PERSONAL, user_id=w.owner
            )
            count = (await session.execute(select(func.count()).select_from(ConversationChannelBinding))).scalar()
            # An archived conversation is never continued.
            (await session.get(Conversation, w.conv["slack_dm"])).is_archived = True
            await session.commit()
            archived = await channel_continuation.find_bound_conversation(
                session, "teams", TEAMS_PERSONAL, user_id=w.owner
            )

    assert found is not None and found.id == w.conv["slack_dm"]
    assert count == 1
    assert archived is None
    # Binding the thread to the conversation it already continues moves nothing.
    assert again.previous_conversation_id is None
    assert moved.previous_conversation_id == w.conv["web"]


# ── The General Assistant's linked agent, on every surface ─────────


async def _link_the_general_assistant(sessions, w, **overrides) -> uuid.UUID:
    """A company-managed agent, linked to the General Assistant; *overrides* change it."""
    async with sessions() as session:
        linked = _agent(w.owner)
        linked.name = "CTEM Assistant"
        linked.user_id = None
        linked.is_company_managed = True
        for name, value in overrides.items():
            setattr(linked, name, value)
        session.add(linked)
        await session.flush()
        session.add(GeneralAssistantConfig(linked_agent_id=linked.id))
        await session.commit()
        return linked.id


def _stub_web_turn(monkeypatch) -> SimpleNamespace:
    """A REST send with the model stubbed: who answered is what it records."""
    monkeypatch.setattr(chat_api, "enforce_rate_limit", AsyncMock())
    monkeypatch.setattr(chat_turn_stream, "TurnPublisher", _Publisher)

    async def _no_llm(_db, _user_id):
        raise RuntimeError("no LLM in tests")

    monkeypatch.setattr("app.llm.router.get_llm_for_user", _no_llm)
    answered: list[str] = []

    async def _agent_run(_db, _user_id, agent, _history, _text, **_kwargs):
        answered.append(agent.name)
        return (f"{agent.name} answers", None)

    async def _general_run(_db, _user_id, _history, _text, **_kwargs):
        answered.append("General Assistant")
        return ("The General Assistant answers", None)

    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _agent_run)
    monkeypatch.setattr("app.services.general_chat.run_general_chat", _general_run)
    return SimpleNamespace(answered=answered)


async def _send_on_the_web(session, w, name: str):
    from app.api.v1.chat import SendMessageRequest

    owner = await session.get(User, w.owner)
    assert owner.role == "user", "a non-admin: admins short-circuit visibility"
    return await chat_api.send_message(
        str(w.conv[name]), SendMessageRequest(content="and the figures?"), user=owner, db=session, _chat_gate=None
    )


@pytest.mark.asyncio
async def test_a_teams_conversation_the_general_assistant_owns_is_its_linked_agents_on_the_web(
    monkeypatch, fake_redis
):
    """Answered on the web by the agent standing in for the General Assistant,
    as in Teams and in its wakes, and the web is told who answers it; a web
    conversation created before the link keeps the General Assistant, a bound
    one its agent."""
    stub = _stub_web_turn(monkeypatch)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        linked = await _link_the_general_assistant(db.sessions, w)
        async with db.sessions() as session:
            owner = await session.get(User, w.owner)
            detail = {
                name: await chat_api.get_conversation(str(w.conv[name]), limit=50, user=owner, db=session)
                for name in ("teams_personal", "general", "slack_dm")
            }
            listed = {row.id: row for row in await _list(session, w.owner)}
        async with db.sessions() as session:
            response = await _send_on_the_web(session, w, "teams_personal")
        async with db.sessions() as session:
            general = await _send_on_the_web(session, w, "general")
        async with db.sessions() as session:
            conv = await session.get(Conversation, w.conv["teams_personal"])

    assert detail["teams_personal"].agent_id is None
    assert detail["teams_personal"].answering_agent_id == str(linked)
    assert detail["general"].answering_agent_id is None
    assert detail["slack_dm"].answering_agent_id is None and detail["slack_dm"].agent_id == str(w.agent)
    assert listed[str(w.conv["teams_personal"])].answering_agent_id == str(linked)
    assert listed[str(w.conv["general"])].answering_agent_id is None
    assert stub.answered == ["CTEM Assistant", "General Assistant"]
    assert response.assistant_message.content == "CTEM Assistant answers"
    assert general.assistant_message.content == "The General Assistant answers"
    assert conv.agent_id is None, "unbound: the next turn follows the link again"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"enabled": False},
        {"disable_chat": True},
        {"access_restricted": True},
        {"is_company_managed": False},
    ],
    ids=["disabled", "chat-off", "restricted-and-shared-with-the-owner", "private-to-the-owner"],
)
async def test_a_linked_agent_not_every_user_may_chat_with_leaves_the_turn_to_the_general_assistant(
    monkeypatch, fake_redis, overrides
):
    """The General Assistant serves every user: a link only some may chat with
    is not followed, on the web as in Teams - even for an owner the agent is
    shared with, or who owns it."""
    stub = _stub_web_turn(monkeypatch)
    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        if overrides.get("access_restricted"):
            overrides["shared_user_ids"] = [str(w.owner)]
        if overrides.get("is_company_managed") is False:
            overrides["user_id"] = w.owner
        await _link_the_general_assistant(db.sessions, w, **overrides)
        async with db.sessions() as session:
            owner = await session.get(User, w.owner)
            detail = await chat_api.get_conversation(str(w.conv["teams_personal"]), limit=50, user=owner, db=session)
        async with db.sessions() as session:
            await _send_on_the_web(session, w, "teams_personal")
        async with db.sessions() as session:
            route = await channel_router._route_turn(
                session,
                InboundMessage(
                    channel=ChannelType.TEAMS,
                    text="and now?",
                    user_email=w.owner_email,
                    channel_user_id="29:owner",
                    channel_thread_id=TEAMS_PERSONAL,
                    metadata={"conversation_type": "personal"},
                ),
                target_agent_id=None,
                channel_bot=None,
                is_profile_override=False,
                is_anonymous=False,
                is_dm=True,
                user=await session.get(User, w.owner),
                conversation_channel="teams",
            )

    assert detail.answering_agent_id is None
    assert stub.answered == ["General Assistant"]
    assert route.general_assistant is True and route.agent is None


@pytest.mark.asyncio
async def test_the_linked_agent_is_asked_for_the_turns_user_through_the_three_tiers(monkeypatch):
    """The visibility check runs for the user it is asked for, as a non-admin."""
    from app.services import general_assistant_link

    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        linked = await _link_the_general_assistant(db.sessions, w)
        async with db.sessions() as session:
            agent = await session.get(Agent, linked)
            for_owner = await general_assistant_link.may_chat_with(session, agent, w.owner)
            for_other = await general_assistant_link.may_chat_with(session, agent, w.other)
            private = await session.get(Agent, w.agent)
            private_for_other = await general_assistant_link.may_chat_with(session, private, w.other)

    assert for_owner is True and for_other is True, "company-managed: every user sees it"
    assert private_for_other is False, "another user's private agent is not theirs to chat with"


@pytest.mark.asyncio
async def test_a_shared_list_names_who_answers_each_owners_conversation_as_the_turn_would():
    """The list reads the link once and asks no owner on their own: a link that
    qualifies reaches every user, which the per-turn check confirms for the owner."""
    from app.services import general_assistant_link

    async with scratch_database("chcont", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        linked = await _link_the_general_assistant(db.sessions, w)
        async with db.sessions() as session:
            session.add(
                ConversationShare(
                    conversation_id=w.conv["teams_personal"], shared_with_user_id=w.other, created_by=w.owner
                )
            )
            await session.commit()
            shared = {row.id: row for row in await _list(session, w.other, scope="shared")}
            turn = await general_assistant_link.turn_stand_in(
                session, agent_id=None, conversation_channel="teams", user_id=w.owner
            )

    assert shared[str(w.conv["teams_personal"])].answering_agent_id == str(linked)
    assert turn is not None and turn.id == linked
