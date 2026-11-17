"""The web chat's events stream on the real turn relay (#3712 fan-out fakes).

A tab follows its own turn on its POST stream: the events stream it opened
with the same client id never echoes that turn's live frames. Another tab or
device sees the user message and the turn as they happen, and a tab that
attaches mid-turn is resumed into it.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from app.api.v1 import chat as chat_module
from app.services.chat_turn_stream import AGENTIC_ACTION_STATUS, NON_TURN_FRAME_TYPES, turn_channel
from app.services.chat_web_transport import DetachedTurnStreamResponse, HeadlessTurnConnection
from tests.test_chat_turn_fanout import (  # noqa: F401 - fixtures
    _events,
    _patch_general_chat,
    _stop,
    _wait_until,
    fake_rl,
    fanout_redis,
    ws_env,
)


def _post(env, content: str, *, client_id: str) -> tuple[asyncio.Task, list[dict]]:
    conversation_id = str(env.conv.id)
    frames: list[dict] = []
    connection = HeadlessTurnConnection(conversation_id, {"content": content})
    auth = chat_module.ChatConnectionAuth(
        user_id=env.user_id, session=SimpleNamespace(), origin=client_id
    )

    async def _run(conn):
        await chat_module.run_chat_connection(conn, conversation_id, auth)

    async def _client_stays():
        await asyncio.Event().wait()

    async def _written(message):
        body = message.get("body") or b""
        frames.extend(
            json.loads(chunk[6:]) for chunk in body.decode().split("\n\n") if chunk.startswith("data: ")
        )

    task = asyncio.create_task(DetachedTurnStreamResponse(connection, _run)({"type": "http"}, _client_stays, _written))
    return task, frames


async def test_the_events_stream_never_echoes_the_tabs_own_turn_and_shows_it_to_the_others(monkeypatch, ws_env):  # noqa: F811
    conv_id = str(ws_env.conv.id)
    gate = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": "tool_start", "tools": ["search"]})
        await kwargs["on_token"]("Hello")
        await gate.wait()
        return ("Hello", None)

    _patch_general_chat(monkeypatch, _run)
    own_events, own = await _events(ws_env, client_id="tab-1")
    other_events, other = await _events(ws_env, client_id="phone")
    await _wait_until(lambda: len(ws_env.redis.subscribers.get(turn_channel(conv_id)) or ()) >= 1)
    await asyncio.sleep(0.05)

    post, posted = _post(ws_env, "build the report", client_id="tab-1")
    try:
        await _wait_until(lambda: any(f["type"] == "stream" for f in other), timeout=4.0)
        gate.set()
        await asyncio.wait_for(post, 5)
        await _wait_until(lambda: any(f["type"] == "done" for f in other), timeout=4.0)

        assert posted[0]["type"] == "thinking"
        assert posted[-1]["type"] == "done" and not posted[-1].get("relayed")
        assert other[0]["type"] == "user_message"
        assert other[0]["message"]["content"] == "build the report"
        done = next(f for f in other if f["type"] == "done")
        assert done["content"] == "Hello" and done["relayed"] is True
        assert own == []
    finally:
        gate.set()
        await _stop(own_events)
        await _stop(other_events)


async def test_a_tab_that_attaches_mid_turn_is_resumed_into_it(monkeypatch, ws_env):  # noqa: F811
    conv_id = str(ws_env.conv.id)
    gate = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": "streaming"})
        await kwargs["on_token"]("Partial ")
        await gate.wait()
        await kwargs["on_token"]("answer")
        return ("Partial answer", None)

    _patch_general_chat(monkeypatch, _run)
    post, _posted = _post(ws_env, "build the report", client_id="tab-1")
    await _wait_until(
        lambda: any(f["type"] == "stream" for f in ws_env.redis.frames(turn_channel(conv_id))), timeout=4.0
    )
    await asyncio.sleep(0.03)

    reloaded_events, reloaded = await _events(ws_env, client_id="tab-1-reloaded")
    try:
        await _wait_until(lambda: any(f["type"] == "stream" for f in reloaded), timeout=4.0)
        assert reloaded[0]["type"] == "thinking" and reloaded[0]["resumed"] is True
        gate.set()
        await asyncio.wait_for(post, 5)
        await _wait_until(lambda: any(f["type"] == "done" for f in reloaded), timeout=4.0)
        assert next(f for f in reloaded if f["type"] == "done")["content"] == "Partial answer"
    finally:
        gate.set()
        await _stop(reloaded_events)


async def test_a_tab_whose_events_stream_reattaches_mid_turn_is_not_handed_its_own_turn(monkeypatch, ws_env):  # noqa: F811
    """Its send stream carries that turn; a reconnect (a network blip,
    the stream's lifetime) must not replay it a second time. Another tab
    attaching at the same moment is resumed into it, and the tab's next
    turn from elsewhere still reaches it."""
    conv_id = str(ws_env.conv.id)
    gate = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": "streaming"})
        await kwargs["on_token"]("Partial ")
        await gate.wait()
        await kwargs["on_token"]("answer")
        return ("Partial answer", None)

    _patch_general_chat(monkeypatch, _run)
    post, posted = _post(ws_env, "build the report", client_id="tab-1")
    await _wait_until(
        lambda: any(f["type"] == "stream" for f in ws_env.redis.frames(turn_channel(conv_id))), timeout=4.0
    )
    await asyncio.sleep(0.03)

    own_events, own = await _events(ws_env, client_id="tab-1")
    other_events, other = await _events(ws_env, client_id="phone")
    try:
        await _wait_until(lambda: any(f["type"] == "stream" for f in other), timeout=4.0)
        gate.set()
        await asyncio.wait_for(post, 5)
        await _wait_until(lambda: any(f["type"] == "done" for f in other), timeout=4.0)
        assert other[0]["type"] == "thinking" and other[0]["resumed"] is True
        assert any(f["type"] == "done" and not f.get("relayed") for f in posted)
        assert own == []

        gate.clear()
        gate.set()
        phone_post, _phone_posted = _post(ws_env, "and from my phone", client_id="phone")
        await asyncio.wait_for(phone_post, 5)
        await _wait_until(lambda: any(f["type"] == "done" for f in own), timeout=4.0)
        assert own[0]["type"] == "user_message"
        assert next(f for f in own if f["type"] == "done")["relayed"] is True
    finally:
        gate.set()
        await _stop(own_events)
        await _stop(other_events)


def _unnamed(frames: list[dict]) -> list[dict]:
    """The frames of a turn that do not name it (``user_message`` and
    ``conversation_title`` belong to no turn)."""
    return [f for f in frames if f["type"] not in NON_TURN_FRAME_TYPES and not f.get("turn_id")]


async def test_every_turn_frame_names_its_turn_on_the_send_stream_and_on_a_resume(monkeypatch, ws_env):  # noqa: F811
    """The send's own stream, the acknowledgement written before the lock
    included, and a tab resumed mid-turn then fed live: every frame of the
    turn carries the ``turn_id`` its ``turn_started`` names."""
    conv_id = str(ws_env.conv.id)
    gate = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        on_event = kwargs["on_event"]
        await on_event({"status": AGENTIC_ACTION_STATUS, "actions": 1, "period": "2026-10"})
        await on_event({"status": "tool_start", "tools": ["search"]})
        await on_event({"status": "thinking_text", "content": "Looking it up"})
        await on_event({"status": "tool_done", "tool": "search"})
        await on_event({"status": "segment_complete", "content": "Found it."})
        await on_event({"status": "streaming"})
        await kwargs["on_token"]("Partial ")
        await gate.wait()
        await on_event({"status": "tool_start", "tools": ["read"]})
        await kwargs["on_token"]("answer")
        return ("Partial answer", None)

    _patch_general_chat(monkeypatch, _run)
    post, posted = _post(ws_env, "build the report", client_id="tab-1")
    await _wait_until(
        lambda: any(f["type"] == "stream" for f in ws_env.redis.frames(turn_channel(conv_id))), timeout=4.0
    )
    await asyncio.sleep(0.03)

    resumed_events, resumed = await _events(ws_env, client_id="phone")
    try:
        await _wait_until(lambda: any(f["type"] == "stream" for f in resumed), timeout=4.0)
        gate.set()
        await asyncio.wait_for(post, 5)
        await _wait_until(lambda: any(f["type"] == "done" for f in resumed), timeout=4.0)
    finally:
        gate.set()
        await _stop(resumed_events)

    turn_id = next(f["turn_id"] for f in posted if f["type"] == "turn_started")
    assert {"thinking", "agentic_action", "status", "stream", "done"} <= {f["type"] for f in posted}
    assert posted[0]["type"] == "thinking"
    assert any(f["type"] == "done" and f.get("intermediate") for f in posted)
    assert not _unnamed(posted)
    assert {f["turn_id"] for f in posted if f.get("turn_id")} == {turn_id}

    assert resumed[0]["type"] == "thinking" and resumed[0]["resumed"] is True
    assert {"agentic_action", "status", "stream", "done"} <= {f["type"] for f in resumed}
    assert not _unnamed(resumed)
    assert {f["turn_id"] for f in resumed if f.get("turn_id")} == {turn_id}
