"""Continuing one conversation across the web, Slack and Teams: the rules.

No database: the predicates the conversation list and the send paths share,
the turn lock a channel message takes (``ChannelTurn``, against the in-memory
Redis of the turn-guard suite), the Slack and Teams openers of a
direct-message thread, and the web send refusals. The flows against
PostgreSQL are in ``test_channel_continuation_postgres.py``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from app.services import channel_continuation, chat_turn_guard
from app.services.channel_continuation import (
    ChannelContinuationError,
    ChannelTurn,
    continuable_on_web,
    is_private_channel_thread,
    web_continuation_refusal,
    web_listable_clause,
    web_send_refusal,
)
from tests.test_chat_turn_singleflight import _relay_the_other_turn, fake_rl, ws_env  # noqa: F401 - fixtures
from tests.test_chat_web_steering import _FakeRedisKV, _FakeSession, _send

CONV = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def fake_redis(monkeypatch):
    import app.core.redis as redis_module

    fake = _FakeRedisKV()

    async def _client():
        return fake

    monkeypatch.setattr(redis_module, "redis_client", _client)
    return fake


@pytest.fixture(autouse=True)
def _every_hold_given_back():
    """No test leaves a channel hold renewing (``_HOLD_KEEPERS``)."""
    yield
    left = dict(channel_continuation._HOLD_KEEPERS)
    channel_continuation._HOLD_KEEPERS.clear()
    assert not left, f"channel holds left renewing: {sorted(left)}"


def _conv(channel, thread=None, *, user_id=uuid.uuid4()):
    return SimpleNamespace(id=uuid.uuid4(), channel=channel, channel_thread_id=thread, user_id=user_id)


# ── Which conversations the web continues ─────────────────────────────────


@pytest.mark.parametrize(
    ("channel", "thread", "private"),
    [
        ("slack", "D0123:1700000000.000100", True),
        ("slack", "D0123", True),
        ("slack", "C0123:1700000000.000100", False),
        ("slack", "G0123:1700000000.000100", False),
        ("teams", "a:1personal", True),
        ("teams", "19:abc@thread.v2", False),
        ("teams", "19:abc@thread.tacv2;messageid=1", False),
        ("whatsapp", "33612345678", False),
        ("web", None, False),
        ("slack", None, False),
    ],
)
def test_a_direct_message_thread_is_told_by_its_id(channel, thread, private):
    assert is_private_channel_thread(channel, thread) is private


def test_the_list_clause_is_the_web_plus_the_direct_message_threads():
    rendered = (
        str(web_listable_clause().compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        .lower()
        .replace("%%", "%")  # the driver's pyformat escape
    )
    assert "conversations.channel = 'web'" in rendered
    assert "conversations.channel = 'slack' and conversations.channel_thread_id like 'd%'" in rendered
    assert "conversations.channel = 'teams' and conversations.channel_thread_id like 'a:%'" in rendered


def test_what_the_web_continues_and_what_it_refuses():
    assert continuable_on_web(_conv("web")) and web_continuation_refusal(_conv("web")) is None
    # A row never flushed has no channel yet: the column default, web.
    assert continuable_on_web(_conv(None)) and web_continuation_refusal(_conv(None)) is None
    dm = _conv("slack", "D0123:1.0")
    assert continuable_on_web(dm) and web_continuation_refusal(dm) is None
    group = _conv("teams", "19:abc@thread.v2")
    assert not continuable_on_web(group)
    assert web_continuation_refusal(group) == (
        "This conversation is a Microsoft Teams thread shared with other people. "
        "Continue it in Microsoft Teams."
    )
    anonymous = _conv("slack", "D0123:1.0", user_id=None)
    assert not continuable_on_web(anonymous)
    # Another channel keeps what it had: its owner may send in it, it is not listed.
    whatsapp = _conv("whatsapp", "33612345678")
    assert not continuable_on_web(whatsapp) and web_continuation_refusal(whatsapp) is None


# ── The turn lock across channels ─────────────────────────────────────────


async def test_a_channel_turn_token_names_its_channel(fake_redis):
    token = await chat_turn_guard.acquire_turn(CONV, channel="slack")
    assert token.startswith("channel:slack:")
    assert await chat_turn_guard.channel_turn_holder(CONV) == "slack"
    assert await web_send_refusal(_conv_with_id(CONV, "web")) == (
        "A reply to a message sent from Slack is being written in this "
        "conversation. Send yours again once it arrives."
    )
    await chat_turn_guard.release_turn(CONV, token)
    web_token = await chat_turn_guard.acquire_turn(CONV)
    assert chat_turn_guard.channel_turn_token_channel(web_token) is None
    # A web turn is folded into, as before: no refusal.
    assert await chat_turn_guard.channel_turn_holder(CONV) is None
    assert await web_send_refusal(_conv_with_id(CONV, "web")) is None


def _conv_with_id(conversation_id, channel, thread=None):
    return SimpleNamespace(id=conversation_id, channel=channel, channel_thread_id=thread, user_id=uuid.uuid4())


async def test_an_unreachable_guard_refuses_no_web_send(monkeypatch):
    async def _down():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(chat_turn_guard, "_redis", _down)
    assert await web_send_refusal(_conv_with_id(CONV, "web")) is None


class _Publisher:
    def __init__(self, conversation_id, turn_token, *, origin=None):
        self.frames: list[dict] = []
        self.closed_with = "open"

    def start(self, ack: dict[str, object] | None = None) -> None:
        self.frames.append({**(ack or {}), "type": "thinking"})

    def offer(self, frame):
        self.frames.append(frame)

    async def on_event(self, event):
        self.frames.append({"type": "status", **event})

    async def aclose(self, *, terminal=None):
        self.closed_with = terminal


@pytest.fixture
def publisher(monkeypatch):
    from app.services import chat_turn_stream

    made: list[_Publisher] = []

    def _make(*args, **kwargs):
        made.append(_Publisher(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(chat_turn_stream, "TurnPublisher", _make)
    return made


async def test_a_channel_turn_holds_the_lock_shows_on_the_web_and_gives_it_back(fake_redis, publisher):
    turn = ChannelTurn("teams")
    assert await turn.acquire(CONV)
    assert turn.held and await chat_turn_guard.channel_turn_holder(CONV) == "teams"
    assert publisher == [], "nothing shows on the web before the turn starts"
    turn.start()
    turn.start()  # once
    await turn.on_event({"status": "tool_start", "tool": "search"})
    turn.finish(message_id="m-1", content="the answer")
    turn.finish(message_id="m-2", content="ignored")
    await turn.release()
    await turn.release()  # once

    (pub,) = publisher
    assert pub.frames == [
        {"type": "thinking"},
        {"type": "status", "status": "tool_start", "tool": "search"},
        {
            "type": "done",
            "conversation_id": CONV,
            "channel": "teams",
            "content": "the answer",
            "message_id": "m-1",
        },
    ]
    assert pub.closed_with is not None  # the interrupted terminal, offered only without a done
    assert not await chat_turn_guard.is_turn_active(CONV)


async def test_a_suppressed_channel_turn_ends_quietly_for_the_web(fake_redis, publisher):
    turn = ChannelTurn("slack")
    assert await turn.acquire(CONV)
    turn.start()
    turn.finish(message_id=None, content="", content_format="slack_mrkdwn")
    await turn.release()
    assert publisher[0].frames[-1] == {"type": "done", "conversation_id": CONV, "channel": "slack"}


async def test_a_reply_written_in_slacks_mrkdwn_says_so_on_its_done_frame(fake_redis, publisher):
    turn = ChannelTurn("slack")
    assert await turn.acquire(CONV)
    turn.start()
    turn.finish(message_id="m-1", content="*contained*", content_format="slack_mrkdwn")
    await turn.release()
    assert publisher[0].frames[-1] == {
        "type": "done",
        "conversation_id": CONV,
        "channel": "slack",
        "content": "*contained*",
        "message_id": "m-1",
        "content_format": "slack_mrkdwn",
    }


async def test_a_turn_refused_before_it_starts_shows_nothing_on_the_web(fake_redis, publisher):
    # A queued message that no longer reaches its conversation gives the turn
    # back before start().
    turn = ChannelTurn("slack")
    assert await turn.acquire(CONV)
    turn.show_user_message(object())
    turn.finish(message_id=None, content="")
    await turn.release()
    assert publisher == []
    assert not await chat_turn_guard.is_turn_active(CONV)


async def test_the_channel_message_shows_on_the_web_before_the_reply(fake_redis, publisher, monkeypatch):
    from app.services import chat_turn_stream

    shown: list[tuple] = []
    monkeypatch.setattr(
        chat_turn_stream,
        "publish_live",
        lambda cid, frame, *, origin: shown.append((cid, frame, origin)),
    )
    monkeypatch.setattr(
        chat_turn_stream,
        "_user_message_serializer",
        lambda message: {"id": str(message.id), "content": message.content, "channel": "teams"},
    )
    turn = ChannelTurn("teams")
    assert await turn.acquire(CONV)
    turn.start()
    message = SimpleNamespace(id=uuid.uuid4(), content="hello from Teams")
    turn.show_user_message(message)
    turn.show_user_message(None)
    await turn.release()
    ((cid, frame, origin),) = shown
    assert cid == CONV
    assert frame == {
        "type": "user_message",
        "message": {"id": str(message.id), "content": "hello from Teams", "channel": "teams"},
    }
    assert origin.startswith("channel-"), "the fan-out does not echo it back to its own turn"


def test_the_chat_api_serializes_the_relayed_user_message():
    """A web turn and a channel turn relay a stored message through the
    service; the API registered its own serializer, so the frame carries what
    a refetch returns."""
    from datetime import UTC, datetime

    from app.api.v1 import chat as chat_api
    from app.services import chat_turn_stream

    stored = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        role="user",
        content="hello",
        channel="slack",
        channel_message_id="1700000000.000100",
        sequence=3,
        created_at=datetime.now(UTC),
        metadata_json=None,
        llm_trace_id=None,
    )
    assert chat_turn_stream._user_message_serializer is not None
    payload = chat_turn_stream._user_message_serializer(stored)
    assert payload == chat_api._build_message_response(stored).model_dump(mode="json")
    assert payload["channel"] == "slack" and payload["content"] == "hello"


@pytest.mark.parametrize(
    ("meta", "content_format"),
    [
        ({"content_format": "slack_mrkdwn"}, "slack_mrkdwn"),
        (None, None),
        ({"content_format": "html"}, None),
        ({"content_format": ["slack_mrkdwn"]}, None),
    ],
)
def test_a_stored_reply_serves_the_markup_it_is_written_in(meta, content_format):
    """Only a reply marked as Slack's mrkdwn is converted by the web; one
    stored unmarked, or with a value nobody wrote, is the Markdown it renders."""
    import json
    from datetime import UTC, datetime

    from app.api.v1 import chat as chat_api

    stored = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        role="assistant",
        content="*contained*",
        channel="slack",
        channel_message_id=None,
        sequence=4,
        created_at=datetime.now(UTC),
        metadata_json=json.dumps(meta) if meta else None,
        llm_trace_id=None,
    )
    assert chat_api._build_message_response(stored).content_format == content_format


async def test_a_channel_message_never_waits_for_the_running_turn(fake_redis, publisher):
    web = await chat_turn_guard.acquire_turn(CONV)
    turn = ChannelTurn("slack")
    assert not await turn.acquire(CONV)
    assert not turn.held
    await turn.release()
    assert fake_redis.kv[chat_turn_guard.processing_key(CONV)] == web
    assert publisher == []


async def test_a_guard_that_cannot_be_reached_runs_the_turn_unguarded(monkeypatch, publisher):
    async def _down(*_a, **_k):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(chat_turn_guard, "acquire_turn", _down)
    turn = ChannelTurn("slack")
    assert await turn.acquire(CONV)
    assert not turn.held
    await turn.release()


async def test_the_web_stop_reaches_a_channel_turn(fake_redis, publisher, monkeypatch):
    monkeypatch.setattr(chat_turn_guard, "BRIDGE_POLL_SECONDS", 0.01)
    turn = ChannelTurn("slack")
    assert await turn.acquire(CONV)
    turn.start()
    assert await chat_turn_guard.request_turn_cancel(CONV)
    await asyncio.wait_for(turn.cancel_event.wait(), timeout=2)
    await turn.release()


async def test_a_message_folded_into_a_channel_turn_goes_to_the_next_turn(fake_redis, publisher):
    turn = ChannelTurn("slack")
    assert await turn.acquire(CONV)
    # A fold that bypassed the channel hold still reaches the next turn.
    _token, folded = await chat_turn_guard.acquire_or_fold(CONV, "the task finished")
    assert folded
    await turn.release()
    assert [p.text for p in await chat_turn_guard.drain_steering_inbox(CONV)] == ["the task finished"]


# ── Holding channel turns off while a web send or a wake joins ────────────


async def test_a_hold_keeps_a_channel_message_off_the_turn_until_it_is_released(fake_redis, publisher):
    hold, refusal = await channel_continuation.hold_off_channel_turns(CONV)
    assert hold and refusal is None
    # The web send stores its message and takes the turn under the hold.
    web = await chat_turn_guard.acquire_turn(CONV)
    assert web
    await chat_turn_guard.release_turn(CONV, web)

    turn = ChannelTurn("slack")
    assert not await turn.acquire(CONV), "the hold keeps the free lock from a channel"
    await channel_continuation.release_channel_hold(CONV, hold)
    assert await turn.acquire(CONV)
    await turn.release()


async def test_a_hold_is_renewed_while_its_caller_works(fake_redis, monkeypatch):
    # Slow writes between the hold and the turn: the hold outlives its TTL.
    # The hold lapses on a clock only the test moves, and only after each
    # renewal: a keeper a loaded runner wakes late cannot lose it.
    clock = [1000.0]
    monkeypatch.setattr(fake_redis, "now", lambda: clock[0])
    monkeypatch.setattr(chat_turn_guard, "TURN_ENTRY_RESERVATION_SECONDS", 0.06)
    renewed = asyncio.Event()
    real_eval = fake_redis.eval

    async def _eval(script, numkeys, *keys_and_args):
        result = await real_eval(script, numkeys, *keys_and_args)
        if script == chat_turn_guard._RENEW_TURN_ENTRY_LUA and result:
            renewed.set()
        return result

    monkeypatch.setattr(fake_redis, "eval", _eval)
    hold, _refusal = await channel_continuation.hold_off_channel_turns(CONV)
    assert hold
    for _ in range(5):  # 200 ms in all: past three TTLs of 60 ms
        renewed.clear()
        await asyncio.wait_for(renewed.wait(), timeout=10)
        clock[0] += 0.04
    assert not await ChannelTurn("teams").acquire(CONV), "still held off"
    await channel_continuation.release_channel_hold(CONV, hold)
    assert hold not in channel_continuation._HOLD_KEEPERS
    turn = ChannelTurn("teams")
    assert await turn.acquire(CONV)
    await turn.release()


async def test_a_hold_its_caller_never_released_lapses_on_its_own(fake_redis, monkeypatch):
    monkeypatch.setattr(chat_turn_guard, "TURN_ENTRY_RESERVATION_SECONDS", 0.03)
    monkeypatch.setattr(chat_turn_guard, "TURN_ENTRY_RESERVATION_MAX_SECONDS", 0.05)
    hold, _refusal = await channel_continuation.hold_off_channel_turns(CONV)
    assert hold
    await asyncio.sleep(0.2)  # renewed up to the cap, then left to lapse
    assert hold not in channel_continuation._HOLD_KEEPERS
    turn = ChannelTurn("teams")
    assert await turn.acquire(CONV)
    await turn.release()


async def test_releasing_a_hold_starts_a_message_it_kept_off(fake_redis, drains):
    hold, _refusal = await channel_continuation.hold_off_channel_turns(CONV)
    notice = await channel_continuation.take_turn_or_queue(ChannelTurn("slack"), CONV, _dm("meanwhile"))
    assert notice == channel_continuation.QUEUED_NOTICE
    drains.clear()
    # The web send failed on its way: it took no turn, so no release drains.
    await channel_continuation.release_channel_hold(CONV, hold)
    assert drains == [CONV]


async def test_a_running_channel_turn_refuses_the_hold(fake_redis, publisher, monkeypatch):
    monkeypatch.setattr(channel_continuation, "_HOLD_POLL_SECONDS", 0.01)
    turn = ChannelTurn("slack")
    assert await turn.acquire(CONV)
    hold, refusal = await channel_continuation.hold_off_channel_turns(CONV)
    assert hold is None
    assert refusal == (
        "A reply to a message sent from Slack is being written in this "
        "conversation. Send yours again once it arrives."
    )
    assert fake_redis._live_members(chat_turn_guard.turn_entries_key(CONV)) == {}
    await turn.release()


async def test_a_wake_waits_out_the_channel_turn_then_holds(fake_redis, publisher, monkeypatch):
    monkeypatch.setattr(channel_continuation, "_HOLD_POLL_SECONDS", 0.01)
    turn = ChannelTurn("teams")
    assert await turn.acquire(CONV)
    released: list[bool] = []

    async def _before_wait():
        released.append(True)

    async def _turn_ends():
        await asyncio.sleep(0.05)
        await turn.release()

    ender = asyncio.create_task(_turn_ends())
    hold, refusal = await channel_continuation.hold_off_channel_turns(
        CONV, wait_seconds=5, before_wait=_before_wait
    )
    await ender
    assert hold and refusal is None
    assert released == [True]
    await channel_continuation.release_channel_hold(CONV, hold)
    assert fake_redis._live_members(chat_turn_guard.turn_entries_key(CONV)) == {}


async def test_a_hold_whose_caller_failed_before_releasing_it_is_given_back_at_once(fake_redis, drains):
    keepers: list[asyncio.Task] = []

    async def _send_that_fails():
        hold, _refusal = await channel_continuation.hold_off_channel_turns(CONV)
        keepers.append(channel_continuation._HOLD_KEEPERS[hold])
        raise RuntimeError("the commit failed")  # before release_channel_hold

    with pytest.raises(RuntimeError):
        await asyncio.create_task(_send_that_fails())
    await asyncio.wait_for(keepers[0], 2)  # not after TURN_ENTRY_RESERVATION_MAX_SECONDS
    assert fake_redis._live_members(chat_turn_guard.turn_entries_key(CONV)) == {}
    turn = ChannelTurn("slack")
    assert await turn.acquire(CONV), "channel turns are let in again"
    await turn.release()


async def test_an_unreachable_guard_holds_nothing_and_refuses_nothing(monkeypatch):
    async def _down():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(chat_turn_guard, "_redis", _down)
    assert await channel_continuation.hold_off_channel_turns(CONV) == (None, None)
    await channel_continuation.release_channel_hold(CONV, "a-hold")  # never raises


# ── Whether a bot answers direct messages at all ──────────────────────────


@pytest.mark.parametrize(("channel", "refusal"), [("slack", "bot"), ("teams", None)])
async def test_a_bot_that_keeps_direct_messages_out_is_read_from_the_router_gates(monkeypatch, channel, refusal):
    from app.channels import router
    from app.channels.base import ChannelType

    bot = SimpleNamespace(
        only_mention=True, allow_private=False, allowed_receive_channels=[{"id": "C0SUPPORT"}]
    )
    monkeypatch.setattr(router, "_get_channel_bot", AsyncMock(return_value=bot))
    monkeypatch.setattr(router, "_any_agent_bypasses_whitelist", AsyncMock(return_value=False))
    monkeypatch.setattr(
        "app.services.general_assistant_link.general_assistant_stand_in", AsyncMock(return_value=None)
    )
    # A Slack DM is checked against the whitelist; a Teams personal chat never
    # is (the router lets it through), and only_mention never gates a DM.
    assert (
        await router.direct_message_refusal(
            None, ChannelType(channel), agent_id=None, user=SimpleNamespace(id=uuid.uuid4())
        )
        == refusal
    )


# ── Messages queued behind a running turn ─────────────────────────────────

THREAD = "D0Q1:1700000000.000100"
QUEUE = chat_turn_guard.channel_queue_key(CONV)
CLAIMED = chat_turn_guard.channel_queue_claim_key(CONV)


def _dm(text, *, thread=THREAD, message_id=None, files=()):
    from app.channels.base import ChannelType, InboundMessage

    return InboundMessage(
        channel=ChannelType.SLACK,
        text=text,
        user_email="ada@example.test",
        channel_user_id="U0ADA",  # the sender's member id, as the adapter sends it
        channel_thread_id=thread,
        channel_message_id=message_id or uuid.uuid4().hex,
        file_attachments=list(files),
        raw={"token": "raw payload"},
        metadata={"channel_type": "im", "is_threaded_reply": True},
    )


@pytest.fixture
def drains(monkeypatch):
    """The drains the queue asks for, recorded instead of spawned."""
    asked: list[str] = []
    monkeypatch.setattr(
        channel_continuation, "schedule_queue_drain", lambda cid: asked.append(str(cid))
    )
    return asked


@pytest.fixture
def ran(monkeypatch):
    """The queued messages handed to their adapter, with the turn taken for them."""
    handed: list[tuple] = []

    async def _run(inbound):
        handed.append((inbound, channel_continuation._QUEUED_TURN.get()))
        await asyncio.sleep(0.01)  # a turn takes time, with the lock held

    monkeypatch.setattr(channel_continuation, "_run_queued_message", _run)
    return handed


async def test_a_message_that_finds_the_turn_taken_is_queued_with_its_files_stored(fake_redis, drains):
    from app.channels.base import InboundFileAttachment

    web = await chat_turn_guard.acquire_turn(CONV)
    attachment = InboundFileAttachment(
        url="https://smba.trafficmanager.net/download?token=short-lived-secret",
        filename="report.pdf",
        auth_headers={"Authorization": "Bearer xoxb-secret"},
    )
    stored_files = AsyncMock(return_value=["0f7e2a10-0000-4000-8000-000000000001"])
    turn = ChannelTurn("slack")
    notice = await channel_continuation.take_turn_or_queue(
        turn, CONV, _dm("and the figures?", files=[attachment]), store_files=stored_files
    )

    assert notice == channel_continuation.QUEUED_NOTICE
    assert not turn.held
    # Stored now, while the platform URL works; the queue keeps the id only.
    stored_files.assert_awaited_once()
    (stored,) = fake_redis.lists[QUEUE]
    assert "secret" not in stored and "raw payload" not in stored and "trafficmanager" not in stored
    item = json.loads(stored)
    assert item["text"] == "and the figures?"
    assert item["file_ids"] == ["0f7e2a10-0000-4000-8000-000000000001"]
    assert CONV in fake_redis.sets[chat_turn_guard.CHANNEL_QUEUE_INDEX_KEY]
    assert drains == [CONV], "a drain is asked for in case the turn ended meanwhile"
    assert fake_redis.kv[chat_turn_guard.processing_key(CONV)] == web


async def test_a_message_queued_where_no_file_is_stored_keeps_no_url_either(fake_redis, drains):
    # The router passes no store_files when the deployment is not licensed:
    # the message is refused when it runs.
    from app.channels.base import InboundFileAttachment

    await chat_turn_guard.acquire_turn(CONV)
    attachment = InboundFileAttachment(url="https://files.example.test/f?token=secret", filename="f.txt")
    notice = await channel_continuation.take_turn_or_queue(
        ChannelTurn("teams"), CONV, _dm("hello", files=[attachment])
    )
    assert notice == channel_continuation.QUEUED_NOTICE
    (stored,) = fake_redis.lists[QUEUE]
    assert "secret" not in stored and json.loads(stored)["file_ids"] == []


async def test_a_free_turn_is_taken_unless_messages_wait_ahead(fake_redis, drains):
    turn = ChannelTurn("slack")
    assert await channel_continuation.take_turn_or_queue(turn, CONV, _dm("first")) is None
    assert turn.held
    await turn.release()

    await chat_turn_guard.enqueue_channel_message(CONV, channel_continuation._queue_item(_dm("older")))
    later = ChannelTurn("slack")
    assert (
        await channel_continuation.take_turn_or_queue(later, CONV, _dm("newer"))
        == channel_continuation.QUEUED_NOTICE
    ), "arrival order: a message never jumps the queue, even with no turn running"
    assert not later.held
    assert [json.loads(i)["text"] for i in fake_redis.lists[QUEUE]] == ["older", "newer"]


async def test_the_queue_is_capped_and_says_so(fake_redis, drains):
    await chat_turn_guard.acquire_turn(CONV)
    for i in range(chat_turn_guard.CHANNEL_QUEUE_MAX_MESSAGES):
        notice = await channel_continuation.take_turn_or_queue(ChannelTurn("slack"), CONV, _dm(f"m{i}"))
        assert notice == channel_continuation.QUEUED_NOTICE
    from app.channels.base import InboundFileAttachment

    stored_files = AsyncMock(return_value=["unused"])
    one_more = _dm("one more", files=[InboundFileAttachment(url="https://x.test/f", filename="f.txt")])
    notice = await channel_continuation.take_turn_or_queue(
        ChannelTurn("slack"), CONV, one_more, store_files=stored_files
    )
    assert notice == channel_continuation.QUEUE_FULL_NOTICE
    stored_files.assert_not_awaited()  # a message that is not queued stores nothing
    assert len(fake_redis.lists[QUEUE]) == chat_turn_guard.CHANNEL_QUEUE_MAX_MESSAGES


@pytest.mark.parametrize("enqueue", ["full", "unreachable"])
async def test_a_message_refused_once_its_files_were_stored_leaves_none_behind(
    fake_redis, drains, monkeypatch, enqueue
):
    from app.channels.base import InboundFileAttachment

    await chat_turn_guard.acquire_turn(CONV)
    if enqueue == "full":  # the queue filled up between the check and the queueing
        refusal = AsyncMock(return_value=None)
    else:
        refusal = AsyncMock(side_effect=ConnectionError("Redis down"))
    monkeypatch.setattr(chat_turn_guard, "enqueue_channel_message", refusal)
    discard = AsyncMock(return_value=1)
    monkeypatch.setattr(channel_continuation, "discard_queued_files", discard)
    stored = ["0f7e2a10-0000-4000-8000-000000000002"]
    notice = await channel_continuation.take_turn_or_queue(
        ChannelTurn("slack"),
        CONV,
        _dm("see attached", files=[InboundFileAttachment(url="https://x.test/f", filename="f.txt")]),
        store_files=AsyncMock(return_value=stored),
    )

    assert notice == (
        channel_continuation.QUEUE_FULL_NOTICE if enqueue == "full" else channel_continuation.TURN_BUSY_NOTICE
    )
    discard.assert_awaited_once_with(stored)
    assert drains == []


async def test_a_redelivered_message_is_received_once(fake_redis):
    message = _dm("hello", message_id="1700000000.000200")
    assert await channel_continuation.claim_channel_message(message)
    assert not await channel_continuation.claim_channel_message(message), "a Slack retry or a Teams redelivery"
    assert await channel_continuation.claim_channel_message(_dm("hello", message_id="1700000000.000300"))
    # The same id in another thread is another message.
    assert await channel_continuation.claim_channel_message(
        _dm("hello", thread="D0Q1:1700000000.000999", message_id="1700000000.000200")
    )


@pytest.mark.parametrize("ending", ["release", "release_and_drain", "error", "cancelled"])
async def test_every_end_of_a_turn_starts_the_queue(fake_redis, drains, publisher, ending):
    from app.channels.router import _holds_channel_turn

    await chat_turn_guard.enqueue_channel_message(CONV, channel_continuation._queue_item(_dm("waiting")))
    if ending in ("release", "release_and_drain"):
        token = await chat_turn_guard.acquire_turn(CONV)
        if ending == "release":
            await chat_turn_guard.release_turn(CONV, token)
        else:  # the web turn's teardown, a session that expired included
            await chat_turn_guard.release_turn_and_drain(CONV, token)
    elif ending == "error":

        @_holds_channel_turn
        async def _fails(inbound, conversation_id=None, *, turn):
            assert await turn.acquire(CONV)
            turn.start()
            raise RuntimeError("the model call failed")

        with pytest.raises(RuntimeError):
            await _fails(_dm("now"))
    else:
        started = asyncio.Event()

        @_holds_channel_turn
        async def _hangs(inbound, conversation_id=None, *, turn):
            assert await turn.acquire(CONV)
            turn.start()
            started.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(_hangs(_dm("now")))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert drains == [CONV]
    assert not await chat_turn_guard.is_turn_active(CONV)


async def test_queued_messages_run_in_arrival_order_one_turn_per_thread(fake_redis, drains, ran):
    web = await chat_turn_guard.acquire_turn(CONV)
    other_thread = "D0Q1:1700000000.000999"
    for text, thread in (("first", THREAD), ("second", THREAD), ("elsewhere", other_thread), ("last", THREAD)):
        notice = await channel_continuation.take_turn_or_queue(ChannelTurn("slack"), CONV, _dm(text, thread=thread))
        assert notice == channel_continuation.QUEUED_NOTICE
    drains.clear()

    await chat_turn_guard.release_turn(CONV, web)  # the web turn ends
    while drains:  # run each asked-for drain as its spawned task would
        drains.pop(0)
        await channel_continuation.drain_channel_queue(CONV)

    # Consecutive messages of a thread are answered together, as the web folds
    # a message sent mid-turn into the turn that answers it.
    assert [inbound.text for inbound, _ in ran] == ["first\n\nsecond", "elsewhere", "last"]
    assert all(q.conversation_id == CONV and q.token for _, q in ran)
    assert QUEUE not in fake_redis.lists or not fake_redis.lists[QUEUE]
    assert not await chat_turn_guard.is_turn_active(CONV)


async def test_two_drains_never_run_the_same_message(fake_redis, drains, ran):
    for text, thread in (("a", THREAD), ("b", "D0Q1:1700000000.000999")):
        await chat_turn_guard.enqueue_channel_message(
            CONV, channel_continuation._queue_item(_dm(text, thread=thread))
        )
    results = await asyncio.gather(
        channel_continuation.drain_channel_queue(CONV),
        channel_continuation.drain_channel_queue(CONV),
    )
    assert sorted(results) == [False, True], "only the drain holding the turn pops"
    assert [inbound.text for inbound, _ in ran] == ["a"]
    assert [json.loads(i)["text"] for i in fake_redis.lists[QUEUE]] == ["b"]
    assert drains == [CONV], "the release of that turn asks for the next one"


async def test_the_router_takes_over_the_turn_the_drain_took(fake_redis, drains, monkeypatch):
    from app.channels.base import OutboundMessage
    from app.channels.router import _holds_channel_turn

    seen: list[tuple] = []

    @_holds_channel_turn
    async def _handler(inbound, conversation_id=None, *, turn):
        held_by = fake_redis.kv.get(chat_turn_guard.processing_key(CONV))
        seen.append((turn.held, turn.queued, turn.conversation_id, held_by == turn.token, inbound.text))
        return OutboundMessage(text="ok")

    async def _run(inbound):
        await _handler(inbound)

    monkeypatch.setattr(channel_continuation, "_run_queued_message", _run)
    await chat_turn_guard.enqueue_channel_message(CONV, "not a queued message")
    await chat_turn_guard.enqueue_channel_message(CONV, channel_continuation._queue_item(_dm("queued")))

    assert await channel_continuation.drain_channel_queue(CONV)
    assert seen == [(True, True, CONV, True, "queued")], "an unreadable entry is dropped, not run"
    assert not await chat_turn_guard.is_turn_active(CONV)


@pytest.mark.parametrize("reaches_router", [True, False], ids=["answered", "never_reached"])
async def test_a_drain_ends_by_deleting_the_files_its_turn_left_unattached(
    fake_redis, drains, monkeypatch, reaches_router
):
    from app.channels.base import OutboundMessage
    from app.channels.router import _holds_channel_turn

    events: list[tuple] = []

    async def _discard(file_ids):
        events.append(("discard", list(file_ids), await chat_turn_guard.is_turn_active(CONV)))
        return len(file_ids)

    monkeypatch.setattr(channel_continuation, "discard_queued_files", _discard)

    @_holds_channel_turn
    async def _handler(inbound, conversation_id=None, *, turn):
        events.append(("turn", list(turn.stored_file_ids)))
        return OutboundMessage(text="ok")

    async def _run(inbound):
        if reaches_router:  # otherwise refused before the router, e.g. no Slack bot any more
            await _handler(inbound)

    monkeypatch.setattr(channel_continuation, "_run_queued_message", _run)
    for text, file_ids in (("first", ["f-1"]), ("second", ["f-2", "f-3"])):
        await chat_turn_guard.enqueue_channel_message(
            CONV, channel_continuation._queue_item(_dm(text), file_ids)
        )

    assert await channel_continuation.drain_channel_queue(CONV)
    files = ["f-1", "f-2", "f-3"]
    answered = [("turn", files)] if reaches_router else []
    # Once the turn is over and the conversation free: what it attached stays.
    assert events == [*answered, ("discard", files, False)]


async def test_a_queue_left_by_a_dead_turn_is_restarted_by_the_sweep(fake_redis, drains):
    await chat_turn_guard.enqueue_channel_message(CONV, channel_continuation._queue_item(_dm("waiting")))
    fake_redis.kv[chat_turn_guard.processing_key(CONV)] = "token-of-a-process-that-died"
    assert await channel_continuation.sweep_channel_queues() == 0 and drains == []

    del fake_redis.kv[chat_turn_guard.processing_key(CONV)]  # its TTL ran out, nothing released
    assert await channel_continuation.sweep_channel_queues() == 1 and drains == [CONV]

    fake_redis.lists[QUEUE].clear()
    assert await channel_continuation.sweep_channel_queues() == 0
    assert CONV not in fake_redis.sets[chat_turn_guard.CHANNEL_QUEUE_INDEX_KEY]


async def _die_holding_a_claim(fake_redis, text="waiting", file_ids=("f-1",)) -> None:
    """A drain claims the next message, then its process dies: the lock's TTL runs out."""
    await chat_turn_guard.enqueue_channel_message(CONV, channel_continuation._queue_item(_dm(text), list(file_ids)))
    token = await chat_turn_guard.acquire_turn(CONV, channel="slack")
    assert await chat_turn_guard.claim_queued_channel_message(CONV, token)
    del fake_redis.kv[chat_turn_guard.processing_key(CONV)]


@pytest.fixture
def stored(monkeypatch):
    """What the database says of a claim a dead process left: ``stored.answer`` (``None``: never stored)."""
    state = SimpleNamespace(answer=None, asked=[])

    async def _stored_turn(conversation_id, items):
        state.asked.append([item["text"] for item in items])
        return state.answer

    monkeypatch.setattr(channel_continuation, "_stored_turn", _stored_turn)
    return state


@pytest.fixture
def told(monkeypatch):
    """The queued messages whose senders were told they were dropped."""
    dropped: list[list[str]] = []

    async def _tell(items):
        dropped.append([item["text"] for item in items])

    monkeypatch.setattr(channel_continuation, "_tell_dropped", _tell)
    return dropped


async def test_a_message_a_dead_process_claimed_is_answered_by_the_next_drain(fake_redis, drains, ran, stored):
    await _die_holding_a_claim(fake_redis)
    assert not fake_redis.lists.get(QUEUE) and len(fake_redis.lists[CLAIMED]) == 1

    # Nothing waits, something is owed: the sweep keeps the conversation and drains it.
    assert await channel_continuation.sweep_channel_queues() == 1 and drains == [CONV]
    assert CONV in fake_redis.sets[chat_turn_guard.CHANNEL_QUEUE_INDEX_KEY]
    # A turn that ends meanwhile hands over to it too.
    drains.clear()
    web = await chat_turn_guard.acquire_turn(CONV)
    await chat_turn_guard.release_turn(CONV, web)
    assert drains == [CONV]

    assert await channel_continuation.drain_channel_queue(CONV)
    ((inbound, queued),) = ran
    assert inbound.text == "waiting" and queued.file_ids == ["f-1"]
    assert stored.asked == [["waiting"]], "asked whether the dead turn stored it first"
    assert not fake_redis.lists.get(QUEUE) and not fake_redis.lists.get(CLAIMED)
    assert not await chat_turn_guard.is_turn_active(CONV)


@pytest.mark.parametrize("answered", [True, False], ids=["answered", "stored_without_reply"])
async def test_a_claim_whose_turn_stored_it_before_its_process_died_is_never_run_twice(
    fake_redis, drains, ran, stored, told, discarded, answered
):
    await _die_holding_a_claim(fake_redis)
    stored.answer = answered

    assert not await channel_continuation.drain_channel_queue(CONV)
    assert ran == [], "its user message is stored once"
    # A reply was stored: answered. None: the sender is told to send it again.
    assert told == ([] if answered else [["waiting"]])
    assert discarded == [["f-1"]], "what the turn did not attach goes"
    assert not fake_redis.lists.get(QUEUE) and not fake_redis.lists.get(CLAIMED)
    assert not await chat_turn_guard.is_turn_active(CONV)


async def test_a_message_never_jumps_ahead_of_one_a_dead_process_claimed(fake_redis, drains, ran, stored):
    await _die_holding_a_claim(fake_redis, "older")
    later = ChannelTurn("slack")
    assert (
        await channel_continuation.take_turn_or_queue(later, CONV, _dm("newer"))
        == channel_continuation.QUEUED_NOTICE
    )
    assert not later.held and drains == [CONV]

    assert await channel_continuation.drain_channel_queue(CONV)
    assert [inbound.text for inbound, _ in ran] == ["older\n\nnewer"]


@pytest.fixture
def discarded(monkeypatch):
    """The file ids :func:`discard_queued_files` was asked to delete."""
    asked: list[list[str]] = []

    async def _discard(file_ids):
        asked.append(list(file_ids))
        return len(file_ids)

    monkeypatch.setattr(channel_continuation, "discard_queued_files", _discard)
    return asked


@pytest.mark.parametrize("failure", ["adapter_raised", "undeliverable", "process_died"])
async def test_a_queued_message_is_started_twice_at_most_then_dropped_and_its_sender_told(
    fake_redis, drains, discarded, stored, told, monkeypatch, failure
):
    started: list[str] = []

    async def _run(inbound):
        started.append(inbound.text)
        if failure == "adapter_raised":  # before the router took it: no answer was stored
            raise RuntimeError("Slack is unreachable")
        if failure == "undeliverable":
            raise channel_continuation.QueuedMessageUndeliverableError("no Slack bot is connected")
        del fake_redis.kv[chat_turn_guard.processing_key(CONV)]  # the lock lapsed under a dying process
        raise asyncio.CancelledError

    monkeypatch.setattr(channel_continuation, "_run_queued_message", _run)
    await chat_turn_guard.enqueue_channel_message(CONV, channel_continuation._queue_item(_dm("waiting"), ["f-1"]))

    async def _drain():
        if failure != "process_died":
            return await channel_continuation.drain_channel_queue(CONV)
        with pytest.raises(asyncio.CancelledError):
            await channel_continuation.drain_channel_queue(CONV)
        return True

    assert await _drain()
    assert started == ["waiting"] and discarded == [] and told == []
    if failure != "process_died":
        # Put back at once, and started again by the release of its turn.
        assert drains == [CONV]
        assert json.loads(fake_redis.lists[QUEUE][0])["attempts"] == 1
    else:
        assert len(fake_redis.lists[CLAIMED]) == 1  # left claimed for the next holder

    assert await _drain()
    assert started == ["waiting", "waiting"]
    if failure == "process_died":
        # The next holder finds it claimed a second time: dropped, never started a third.
        assert not await channel_continuation.drain_channel_queue(CONV)
    assert started == ["waiting", "waiting"]
    assert told == [["waiting"]], "the sender is told it was not answered"
    assert discarded == [["f-1"]]
    assert not fake_redis.lists.get(QUEUE) and not fake_redis.lists.get(CLAIMED)
    assert not await chat_turn_guard.is_turn_active(CONV)


async def test_the_router_forgets_the_claim_before_it_releases_the_turn(fake_redis, drains, monkeypatch):
    from app.channels.base import OutboundMessage
    from app.channels.router import _holds_channel_turn

    answered: list[tuple[str, int]] = []

    @_holds_channel_turn
    async def _handler(inbound, conversation_id=None, *, turn):
        answered.append((inbound.text, len(await chat_turn_guard.claimed_channel_messages(CONV))))
        return OutboundMessage(text="ok")

    async def _run(inbound):
        await _handler(inbound)

    monkeypatch.setattr(channel_continuation, "_run_queued_message", _run)
    for text, thread in (("first", THREAD), ("next", "D0Q1:1700000000.000999")):
        await chat_turn_guard.enqueue_channel_message(
            CONV, channel_continuation._queue_item(_dm(text, thread=thread))
        )

    assert await channel_continuation.drain_channel_queue(CONV)
    assert answered == [("first", 1)] and drains == [CONV]
    assert not fake_redis.lists.get(CLAIMED)
    # The drain that release asked for answers the next message, never the first again.
    assert await channel_continuation.drain_channel_queue(CONV)
    assert answered == [("first", 1), ("next", 1)]


async def test_a_message_queued_longer_than_the_queue_lives_is_dropped_and_its_sender_told(
    fake_redis, drains, ran, discarded, told
):
    from datetime import UTC, datetime, timedelta

    stale = json.loads(channel_continuation._queue_item(_dm("too late"), ["f-old"]))
    stale["queued_at"] = (
        datetime.now(UTC) - timedelta(seconds=chat_turn_guard.CHANNEL_QUEUE_TTL_SECONDS + 60)
    ).isoformat()
    await chat_turn_guard.enqueue_channel_message(CONV, json.dumps(stale))
    await chat_turn_guard.enqueue_channel_message(CONV, channel_continuation._queue_item(_dm("fresh"), ["f-new"]))

    assert await channel_continuation.drain_channel_queue(CONV)
    assert [inbound.text for inbound, _ in ran] == ["fresh"]
    assert told == [["too late"]]
    assert discarded == [["f-old"], ["f-new"]]


async def test_a_drain_that_fails_after_taking_the_turn_gives_it_back(fake_redis, drains, ran, monkeypatch):
    await chat_turn_guard.enqueue_channel_message(CONV, channel_continuation._queue_item(_dm("waiting")))
    monkeypatch.setattr(
        chat_turn_guard, "claim_queued_channel_message", AsyncMock(side_effect=ConnectionError("Redis blip"))
    )

    assert not await channel_continuation.drain_channel_queue(CONV)
    assert not await chat_turn_guard.is_turn_active(CONV), "not held until its TTL runs out"
    assert drains == [], "not retried in a loop: the sweep starts it again"
    assert await channel_continuation.sweep_channel_queues() == 1
    assert ran == []


async def test_files_stored_for_a_queued_message_whose_rows_are_not_written_leave_no_object(monkeypatch):
    from app.channels import router
    from app.channels.base import InboundFileAttachment
    from app.services.file_storage import file_storage

    deleted: list[str] = []

    async def _delete(key):
        deleted.append(key)

    async def _upload(*, filename, **_kw):
        return f"channel/slack/{filename}"

    monkeypatch.setattr(file_storage, "delete", _delete)
    monkeypatch.setattr(file_storage, "upload", _upload)
    monkeypatch.setattr(router, "_download_channel_file", AsyncMock(return_value=(b"data", "text/plain")))
    message = _dm(
        "see attached",
        files=[
            InboundFileAttachment(url="https://x.test/a", filename="a.txt", content_type="text/plain"),
            InboundFileAttachment(url="https://x.test/b", filename="b.txt", content_type="text/plain"),
        ],
    )
    user = SimpleNamespace(id=uuid.uuid4())

    # The row of the second file cannot be written: its object goes, the first stays.
    flushes = iter([None, ConnectionError("database gone")])

    async def _flush():
        if (outcome := next(flushes)) is not None:
            raise outcome

    db = SimpleNamespace(add=lambda _row: None, flush=_flush, commit=AsyncMock(), rollback=AsyncMock())
    assert len(await router._store_queued_files(db, message, user)) == 1
    assert deleted == ["channel/slack/b.txt"]

    # The commit fails: no row names either object any more.
    deleted.clear()
    db = SimpleNamespace(
        add=lambda _row: None,
        flush=AsyncMock(),
        commit=AsyncMock(side_effect=ConnectionError("database gone")),
        rollback=AsyncMock(),
    )
    with pytest.raises(ConnectionError):
        await router._store_queued_files(db, message, user)
    assert deleted == ["channel/slack/a.txt", "channel/slack/b.txt"]
    db.rollback.assert_awaited_once()


async def test_a_dropped_message_is_told_on_its_thread_in_its_senders_language(monkeypatch):
    from app.channels import slack
    from app.services.channel_notices import notice

    posted: list[tuple[str | None, str]] = []

    async def _post(inbound, text):
        posted.append((inbound.channel_thread_id, text))
        return True

    monkeypatch.setattr(slack, "post_queued_notice", _post)
    monkeypatch.setattr(channel_continuation, "_sender", AsyncMock(return_value=SimpleNamespace(language="fr-fr")))
    other = "D0Q1:1700000000.000999"
    items = [json.loads(channel_continuation._queue_item(_dm(text, thread=thread))) for text, thread in (
        ("first", THREAD), ("second", THREAD), ("elsewhere", other),
    )]

    await channel_continuation._tell_dropped(items)
    # One notice per thread, in the sender's own language.
    assert posted == [(THREAD, notice("channel_queued_dropped", "fr")), (other, notice("channel_queued_dropped", "fr"))]

    # A sender that cannot be looked up still hears it, in the platform default.
    posted.clear()
    monkeypatch.setattr(channel_continuation, "_sender", AsyncMock(side_effect=ConnectionError("database gone")))
    monkeypatch.setattr("app.services.channel_notices.platform_language", lambda: None)
    await channel_continuation._tell_dropped(items[:1])
    assert posted == [(THREAD, notice("channel_queued_dropped", "en"))]

    # A bot that cannot post is logged, never raised.
    monkeypatch.setattr(slack, "post_queued_notice", AsyncMock(side_effect=ConnectionError("Slack is down")))
    await channel_continuation._tell_dropped(items[:1])


async def test_an_adapter_that_cannot_answer_a_queued_message_says_so(monkeypatch):
    from app.channels import slack, teams

    monkeypatch.setattr(slack, "_get_slack_bot_creds", AsyncMock(return_value=None))
    with pytest.raises(channel_continuation.QueuedMessageUndeliverableError):
        await slack.run_queued_message(_dm("waiting"))
    assert not await slack.post_queued_notice(_dm("waiting"), "dropped")

    personal = _dm("waiting", thread="a:1personal-chat")
    with pytest.raises(channel_continuation.QueuedMessageUndeliverableError):
        await teams.run_queued_message(personal)  # no Bot Framework service URL
    personal.metadata["service_url"] = "https://smba.trafficmanager.net/emea/"
    monkeypatch.setattr(teams, "_get_teams_creds", AsyncMock(return_value=None))
    with pytest.raises(channel_continuation.QueuedMessageUndeliverableError):
        await teams.run_queued_message(personal)
    assert not await teams.post_queued_notice(personal, "dropped")


async def test_a_dropped_notice_is_posted_in_the_slack_thread_of_the_message(monkeypatch):
    from app.channels import slack

    monkeypatch.setattr(slack, "_get_slack_bot_creds", AsyncMock(return_value=("xoxb-1", "secret")))
    sent: list = []

    async def _send(self, message):
        sent.append(message)
        return "1700000003.000100"

    monkeypatch.setattr(slack.SlackChannel, "send_message", _send)
    assert await slack.post_queued_notice(_dm("waiting"), "Send it again.")
    ((message,),) = [sent]
    assert (message.text, message.channel_user_id, message.metadata) == (
        "Send it again.",
        "D0Q1",
        {"thread_ts": "1700000000.000100"},
    )


# ── Opening the direct-message thread ─────────────────────────────────────


def _mock_httpx(monkeypatch, module, handler):
    real = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", _client)


async def test_slack_posts_in_the_users_direct_messages_and_returns_its_thread(monkeypatch):
    from app.channels import slack

    monkeypatch.setattr(slack, "_get_slack_bot_creds", AsyncMock(return_value=("xoxb-1", "secret")))
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users.lookupByEmail"):
            calls.append(("lookup", dict(request.url.params)))
            return httpx.Response(200, json={"ok": True, "user": {"id": "U42"}})
        calls.append(("post", json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "channel": "D42", "ts": "1700000002.000100"})

    _mock_httpx(monkeypatch, slack, handler)
    thread = await slack.open_slack_dm_thread("ada@example.test", "**Review** continues here")

    assert thread == "D42:1700000002.000100"
    assert calls[0] == ("lookup", {"email": "ada@example.test"})
    assert calls[1][1]["channel"] == "U42" and calls[1][1]["text"] == "*Review* continues here"


@pytest.mark.parametrize(
    ("lookup", "post", "reason"),
    [
        ({"ok": False, "error": "users_not_found"}, None, "user_not_found"),
        # Only an unknown email is "not found": a refused lookup is Slack's failure.
        ({"ok": False, "error": "ratelimited"}, None, "delivery_failed"),
        ({"ok": False, "error": "missing_scope"}, None, "delivery_failed"),
        ({"ok": True, "user": {"id": "U42"}}, {"ok": False, "error": "not_allowed"}, "delivery_failed"),
    ],
    ids=["users_not_found", "ratelimited", "missing_scope", "post_refused"],
)
async def test_slack_says_why_it_could_not_open_the_thread(monkeypatch, lookup, post, reason):
    from app.channels import slack

    monkeypatch.setattr(slack, "_get_slack_bot_creds", AsyncMock(return_value=("xoxb-1", "secret")))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users.lookupByEmail"):
            return httpx.Response(200, json=lookup)
        return httpx.Response(200, json=post)

    _mock_httpx(monkeypatch, slack, handler)
    with pytest.raises(ChannelContinuationError) as exc:
        await slack.open_slack_dm_thread("ada@example.test", "hi")
    assert exc.value.reason == reason


@pytest.mark.parametrize(
    "outage",
    [httpx.ConnectTimeout("timed out"), httpx.Response(503, text="<html>Service Unavailable</html>")],
    ids=["timeout", "not-json"],
)
async def test_slack_out_of_reach_is_a_reason_not_a_server_error(monkeypatch, outage):
    from app.channels import slack

    monkeypatch.setattr(slack, "_get_slack_bot_creds", AsyncMock(return_value=("xoxb-1", "secret")))

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(outage, Exception):
            raise outage
        return outage

    _mock_httpx(monkeypatch, slack, handler)
    with pytest.raises(ChannelContinuationError) as exc:
        await slack.open_slack_dm_thread("ada@example.test", "hi")
    assert exc.value.reason == "delivery_failed"
    assert "could not be reached" in exc.value.detail


async def test_slack_without_a_bot_opens_nothing(monkeypatch):
    from app.channels import slack

    monkeypatch.setattr(slack, "_get_slack_bot_creds", AsyncMock(return_value=None))
    with pytest.raises(ChannelContinuationError) as exc:
        await slack.open_slack_dm_thread("ada@example.test", "hi")
    assert exc.value.reason == "not_configured"


SERVICE_URL = "https://smba.trafficmanager.net/emea/"


async def test_the_teams_personal_chat_is_the_recorded_one_or_a_personal_last_reference(monkeypatch, fake_redis):
    from app.channels import teams

    email = "Ada@Example.test"
    assert await teams.get_personal_conversation_reference(email) is None
    # The last conversation the user wrote from: a group chat is not their personal chat.
    await teams._store_conversation_reference(email, SERVICE_URL, "19:group@thread.v2")
    assert await teams.get_personal_conversation_reference(email) is None
    await teams._store_conversation_reference(email, SERVICE_URL, "a:1legacy")
    assert (await teams.get_personal_conversation_reference(email))["conversation_id"] == "a:1legacy"
    await teams._store_conversation_reference(email, SERVICE_URL, "a:1recorded", personal=True)
    await teams._store_conversation_reference(email, SERVICE_URL, "19:group@thread.v2")
    assert (await teams.get_personal_conversation_reference(email))["conversation_id"] == "a:1recorded"
    # A reference naming a host that is not Bot Framework is never used.
    await teams._store_conversation_reference(email, "https://evil.example/", "a:1x", personal=True)
    await teams._store_conversation_reference(email, "https://evil.example/", "a:1y")
    assert await teams.get_personal_conversation_reference(email) is None


async def test_teams_posts_in_the_personal_chat_and_returns_it(monkeypatch):
    from app.channels import teams

    monkeypatch.setattr(
        teams,
        "get_personal_conversation_reference",
        AsyncMock(return_value={"service_url": SERVICE_URL, "conversation_id": "a:1personal"}),
    )
    monkeypatch.setattr(teams, "_get_teams_creds", AsyncMock(return_value=("bf-token", "app-id")))
    sent: list = []

    async def _send(self, message):
        sent.append((self.conversation_id, self.conversation_type, message.text))
        return "activity-1"

    monkeypatch.setattr(teams.TeamsChannel, "send_message", _send)
    assert await teams.post_to_teams_personal_chat("ada@example.test", "continues here") == "a:1personal"
    assert sent == [("a:1personal", "personal", "continues here")]

    async def _refused(self, message):
        return None

    monkeypatch.setattr(teams.TeamsChannel, "send_message", _refused)
    with pytest.raises(ChannelContinuationError) as exc:
        await teams.post_to_teams_personal_chat("ada@example.test", "continues here")
    assert exc.value.reason == "delivery_failed"


@pytest.mark.parametrize(("conversation_type", "personal"), [("personal", True), ("groupChat", False)])
async def test_a_teams_message_records_the_personal_chat_it_came_from(monkeypatch, conversation_type, personal):
    from app.channels import teams
    from app.channels.base import ChannelType, InboundMessage, OutboundMessage

    stored = AsyncMock()
    monkeypatch.setattr(teams, "_store_conversation_reference", stored)
    monkeypatch.setattr(teams, "_store_service_url", AsyncMock())
    monkeypatch.setattr(teams, "_get_teams_creds", AsyncMock(return_value=("bf-token", "app-id")))
    monkeypatch.setattr(teams, "_get_teams_graph_creds", AsyncMock(return_value=None))
    monkeypatch.setattr(teams, "_fetch_graph_inbound_message", AsyncMock(return_value=None))
    monkeypatch.setattr(teams, "handle_message", AsyncMock(return_value=OutboundMessage(text="", suppress=True)))
    monkeypatch.setattr(teams.TeamsChannel, "send_typing_indicator", AsyncMock())
    conversation = "a:1personal" if personal else "19:chat@thread.v2"
    inbound = InboundMessage(
        channel=ChannelType.TEAMS,
        text="hello",
        user_email="ada@example.test",
        channel_user_id="29:1abc",
        channel_thread_id=conversation,
        metadata={"conversation_type": conversation_type, "tenant_id": "tenant-1"},
    )
    await teams._process_teams_message(inbound, SERVICE_URL, conversation)

    calls = [(c.args, c.kwargs) for c in stored.await_args_list]
    assert ((("ada@example.test", SERVICE_URL, conversation), {}) in calls)
    assert ((("ada@example.test", SERVICE_URL, conversation), {"personal": True}) in calls) is personal


@pytest.mark.parametrize("failing", ["token", "post"])
async def test_teams_out_of_reach_is_a_reason_not_a_server_error(monkeypatch, failing):
    from app.channels import teams

    monkeypatch.setattr(
        teams,
        "get_personal_conversation_reference",
        AsyncMock(return_value={"service_url": SERVICE_URL, "conversation_id": "a:1personal"}),
    )
    timeout = httpx.ConnectTimeout("timed out")
    monkeypatch.setattr(
        teams,
        "_get_teams_creds",
        AsyncMock(side_effect=timeout) if failing == "token" else AsyncMock(return_value=("bf", "app")),
    )
    monkeypatch.setattr(teams.TeamsChannel, "send_message", AsyncMock(side_effect=timeout))
    with pytest.raises(ChannelContinuationError) as exc:
        await teams.post_to_teams_personal_chat("ada@example.test", "continues here")
    assert exc.value.reason == "delivery_failed"
    assert "could not be reached" in exc.value.detail


async def test_teams_needs_the_personal_chat_first(monkeypatch):
    from app.channels import teams

    monkeypatch.setattr(teams, "get_personal_conversation_reference", AsyncMock(return_value=None))
    with pytest.raises(ChannelContinuationError) as exc:
        await teams.post_to_teams_personal_chat("ada@example.test", "hi")
    assert exc.value.reason == "teams_chat_unknown"


# ── The web send refusals ─────────────────────────────────────────────────


async def test_the_web_send_refuses_a_group_thread_before_storing_anything(monkeypatch, ws_env):  # noqa: F811
    ws_env.conv.channel = "slack"
    ws_env.conv.channel_thread_id = "C0123:1700000000.000100"
    conn = await asyncio.wait_for(_send(ws_env, {"content": "hello"}), 5)

    assert (conn.refusal.status_code, conn.refusal.detail) == (
        409,
        "This conversation is a Slack thread shared with other people. Continue it in Slack.",
    )
    assert conn.sent == []
    assert ws_env.stored == []


async def test_the_web_send_refuses_while_a_channel_turn_runs(monkeypatch, ws_env):  # noqa: F811
    conv_id = str(ws_env.conv.id)
    token = await chat_turn_guard.acquire_turn(conv_id, channel="teams")
    conn = await asyncio.wait_for(_send(ws_env, {"content": "and now?"}), 5)

    assert (conn.refusal.status_code, conn.refusal.detail) == (
        409,
        "A reply to a message sent from Microsoft Teams is being written in this "
        "conversation. Send yours again once it arrives.",
    )
    assert conn.sent == []
    assert ws_env.stored == []
    assert ws_env.fake_kv.kv[chat_turn_guard.processing_key(conv_id)] == token


async def test_a_web_send_holds_channel_turns_off_until_it_folds(monkeypatch, ws_env):  # noqa: F811
    from app.api.v1 import chat as chat_module

    conv_id = str(ws_env.conv.id)
    web_turn = await chat_turn_guard.acquire_turn(conv_id)
    entries = chat_turn_guard.turn_entries_key(conv_id)
    held_while_storing: list[bool] = []
    store = chat_module.store_message

    async def _store(*args, **kwargs):
        held_while_storing.append(bool(ws_env.fake_kv._live_members(entries)))
        return await store(*args, **kwargs)

    monkeypatch.setattr(chat_module, "store_message", _store)
    _relay_the_other_turn(monkeypatch, ended=True)
    conn = await asyncio.wait_for(_send(ws_env, {"content": "and this"}), 5)

    assert held_while_storing[:1] == [True], "no channel turn takes the lock between store and fold"
    assert any(f.get("type") == "folded_into_active_turn" for f in conn.sent)
    assert ws_env.fake_kv._live_members(entries) == {}, "the hold is given back once folded"
    assert ws_env.fake_kv.kv[chat_turn_guard.processing_key(conv_id)] == web_turn


class _AgentAwareSession(_FakeSession):
    """The harness session, answering what the turn reads of its agent."""

    def __init__(self, conv, timeline, agent):
        super().__init__(conv, timeline)
        self._agent = agent

    async def execute(self, stmt):
        from app.models.agent import Agent

        columns = getattr(stmt, "column_descriptions", None) or []
        if not columns or columns[0].get("entity") is not Agent:
            return await super().execute(stmt)
        agent = self._agent
        values = [agent if c["name"] == "Agent" else getattr(agent, c["name"]) for c in columns]

        class _R:
            def scalar_one_or_none(self):
                return values[0]

            def one_or_none(self):
                return tuple(values)

        return _R()


@pytest.mark.parametrize("linked", [True, False], ids=["linked-agent", "no-link"])
async def test_a_web_turn_of_a_slack_conversation_is_its_linked_agents_as_in_slack(
    monkeypatch, ws_env, linked  # noqa: F811
):
    """The web answers an unbound Slack conversation with the agent standing in
    for the General Assistant, asked through the one rule for this conversation
    and its owner; without one, the General Assistant answers it."""
    import app.core.database as database_module
    from app.api.v1 import chat as chat_module

    stand_in = SimpleNamespace(
        id=uuid.uuid4(),
        name="CTEM Assistant",
        enabled=True,
        disable_chat=False,
        skip_llm=False,
        is_company_managed=True,
        memory_persistent_enabled=False,
    )
    ws_env.conv.channel = "slack"
    ws_env.conv.channel_thread_id = "D0123:1700000000.000100"
    monkeypatch.setattr(
        database_module, "async_session", lambda: _AgentAwareSession(ws_env.conv, ws_env.timeline, stand_in)
    )
    asked: list[tuple] = []

    async def _turn_stand_in(_db, *, agent_id, conversation_channel, user_id):
        asked.append((agent_id, conversation_channel, user_id))
        return stand_in if linked else None

    monkeypatch.setattr(chat_module, "turn_stand_in", _turn_stand_in)
    ran: list[str] = []

    async def _agent_run(_db, _uid, agent, _history, _content, **_kw):
        ran.append(agent.name)
        return ("the linked agent answers", None)

    async def _general_run(_db, _uid, _history, _content, **_kw):
        ran.append("General Assistant")
        return ("the General Assistant answers", None)

    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _agent_run)
    monkeypatch.setattr("app.services.general_chat.run_general_chat", _general_run)
    conn = await asyncio.wait_for(_send(ws_env, {"content": "and the figures?"}), 5)

    assert asked == [(None, "slack", ws_env.user_id)]
    assert ran == (["CTEM Assistant"] if linked else ["General Assistant"])
    done = [f for f in conn.sent if f.get("type") == "done"]
    assert [f.get("content") for f in done] == [
        "the linked agent answers" if linked else "the General Assistant answers"
    ]
    assert ws_env.conv.agent_id is None, "the conversation stays unbound: the next turn follows the link again"


# ── Starting a continuation ───────────────────────────────────────────────


async def test_starting_a_continuation_is_rate_limited_per_user_before_anything_runs(monkeypatch):
    from fastapi import HTTPException

    from app.api.v1 import chat_continuation as continuation_api

    limited: list[tuple] = []

    async def _over_the_limit(_request, name, *, limit, window_seconds, bucket):
        limited.append((name, limit, window_seconds, bucket))
        raise HTTPException(status_code=429, detail="Too many requests.")

    monkeypatch.setattr(continuation_api, "enforce_rate_limit", _over_the_limit)
    opened = AsyncMock()
    monkeypatch.setattr("app.channels.slack.open_slack_dm_thread", opened)
    user = SimpleNamespace(id=uuid.uuid4(), email="ada@example.test")
    db = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("looked the conversation up")))

    with pytest.raises(HTTPException) as exc:
        await continuation_api.create_channel_binding(
            CONV,
            continuation_api.ChannelBindingCreate(channel="slack"),
            user=user,
            db=db,
            _chat_gate=None,
        )

    assert exc.value.status_code == 429
    assert limited == [
        ("chat-channel-binding", continuation_api.CHANNEL_BINDINGS_PER_MINUTE, 60, str(user.id))
    ]
    opened.assert_not_awaited()
