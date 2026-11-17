"""A transfer on the NON-streaming platform chat path is recorded.

The streaming path records every ``transfer_start`` hop and persists it as the
answer's ``transfer_chain``; the session restore walks those chains
(``attribute_message_agents``) to name who wrote each answer. The
non-streaming path (``stream: false``) rewrote ``conv.agent_id`` but stored no
chain, so every answer after the handover was restored under the agent that
had handed the conversation away.

Both paths persist only the hops that took the conversation: a hop whose
target failed is recorded when it starts, but the turn falls back to the agent
before it (``_completed_transfer_chain``).
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.api.v1 import platform as platform_api

TARGET_ID = str(uuid.uuid4())
_UNSET = object()


def _row():
    return SimpleNamespace(
        id=uuid.uuid4(), filename="iocs.csv", content_type="text/csv", size=12, source="agent"
    )


class _SteerRedis:
    """The streaming path's steering bridge, with nothing ever queued."""

    async def delete(self, *_args):
        return 0

    async def set(self, *_args, **_kwargs):
        return True

    async def lpop(self, *_args):
        return None


async def _done_frame(response) -> dict:
    frames = []
    async for chunk in response.body_iterator:
        text = chunk.decode() if isinstance(chunk, bytes | bytearray) else chunk
        frames += [
            json.loads(block.removeprefix("data: "))
            for block in text.split("\n\n")
            if block.startswith("data: ")
        ]
    (done,) = [frame for frame in frames if frame.get("type") == "done"]
    return done


async def _send(
    monkeypatch,
    *,
    bound_rows=None,
    emit_transfer=True,
    rows=None,
    hops=None,
    landed_on=_UNSET,
    stream=False,
):
    """Drive the route; return (response or ``done`` frame, stored message, conversation).

    *hops* are the ``transfer_start`` events the run emits, *landed_on* the agent
    id ``run_agent_chat`` returns (``None``: the conversation never moved).
    """
    if hops is None:
        hops = [(TARGET_ID, "Beta")] if emit_transfer else []
    if landed_on is _UNSET:
        landed_on = TARGET_ID if emit_transfer else None
    stored: dict = {}
    conversations: list = []

    async def _resolve(_db, _slug):
        return SimpleNamespace(id=uuid.uuid4(), name="Alpha")

    async def _store_message(_db, _conv_id, role, content, channel, metadata_json=None):
        msg = SimpleNamespace(id=uuid.uuid4(), metadata_json=metadata_json, role=role)
        stored["message"] = msg
        return msg

    async def _history(_db, _conv_id):
        return []

    async def _quota(_db, _user_id):
        return None

    async def _run_agent_chat(_db, _uid, _agent, _history, _content, conversation_id, **kwargs):
        on_event = kwargs.get("on_event")
        for agent_id, agent_name in hops:
            assert on_event is not None, "the platform path must listen for handovers"
            await on_event(
                {"status": "transfer_start", "agent_id": agent_id, "agent_name": agent_name}
            )
        return ("answered by Beta", landed_on)

    collected = rows if rows is not None else []

    async def _collect(_db, _ctx, _initial, **_kwargs):
        meta = [{"file_id": str(r.id), "filename": r.filename} for r in collected]
        return (collected, meta)

    async def _link(_db, attachment_rows, *, conversation_id, message_id):
        return attachment_rows if bound_rows is None else bound_rows

    monkeypatch.setattr(platform_api, "_resolve_platform_agent", _resolve)
    monkeypatch.setattr("app.channels.router.store_message", _store_message)
    monkeypatch.setattr("app.channels.router.get_conversation_history", _history)
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _quota)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _run_agent_chat)
    monkeypatch.setattr(platform_api, "_collect_generated_attachments", _collect)
    monkeypatch.setattr(platform_api, "_link_generated_attachments", _link)

    class _DB:
        def add(self, obj):
            conversations.append(obj)

        async def flush(self):
            for obj in conversations:
                if getattr(obj, "id", None) is None:
                    obj.id = uuid.uuid4()

        async def commit(self):
            return None

    async def _steer_redis():
        return _SteerRedis()

    monkeypatch.setattr("app.core.redis.redis_client", _steer_redis)

    request = Request(
        {"type": "http", "method": "POST", "path": "/api/v1/platform/chat/messages", "headers": [], "query_string": b""}
    )
    response = await platform_api.send_platform_chat_message(
        platform_api.PlatformChatRequest(content="hand me over", agent_slug="alpha", stream=stream),
        request=request,
        db=_DB(),  # type: ignore[arg-type]
        user=SimpleNamespace(id=uuid.uuid4(), email="u@example.com"),  # type: ignore[arg-type]
    )
    if stream:
        response = await _done_frame(response)
    return response, stored["message"], conversations


@pytest.mark.asyncio
async def test_non_streaming_transfer_persists_the_chain(monkeypatch):
    response, message, conversations = await _send(monkeypatch)

    meta = json.loads(message.metadata_json)
    assert meta["transfer_chain"] == [{"agent_id": TARGET_ID, "agent_name": "Beta"}]
    # Same response keys as the streaming ``done`` payload.
    assert response["transfer_agent_id"] == TARGET_ID
    assert response["transfer_agent_name"] == "Beta"
    assert response["transfer_chain"] == meta["transfer_chain"]
    # The conversation now belongs to the target, as before.
    (conv,) = [c for c in conversations if hasattr(c, "agent_id")]
    assert str(conv.agent_id) == TARGET_ID


@pytest.mark.asyncio
async def test_no_transfer_stores_no_chain(monkeypatch):
    response, message, _ = await _send(monkeypatch, emit_transfer=False)
    assert message.metadata_json is None
    assert "transfer_chain" not in response
    assert "transfer_agent_name" not in response


@pytest.mark.asyncio
async def test_narrowing_the_attachments_keeps_the_chain(monkeypatch):
    """A file another message kept is dropped from the chips; the handover
    record written next to them must survive that rewrite."""
    kept, lost = _row(), _row()
    _response, message, _ = await _send(monkeypatch, rows=[kept, lost], bound_rows=[kept])

    meta = json.loads(message.metadata_json)
    assert [a["file_id"] for a in meta["attachments"]] == [str(kept.id)]
    assert meta["transfer_chain"] == [{"agent_id": TARGET_ID, "agent_name": "Beta"}]

    _response, message, _ = await _send(monkeypatch, rows=[lost], bound_rows=[])
    meta = json.loads(message.metadata_json)
    assert "attachments" not in meta
    assert meta["transfer_chain"] == [{"agent_id": TARGET_ID, "agent_name": "Beta"}]


@pytest.mark.asyncio
async def test_the_restore_names_the_target_for_a_non_streaming_handover(monkeypatch):
    """End to end on the persisted shape: the chain the non-streaming path
    stores is exactly what the restore walk reads."""
    _response, message, _ = await _send(monkeypatch)
    meta = json.loads(message.metadata_json)
    assert "transfer_chain" in platform_api.RESTORED_ASSISTANT_META_KEYS

    restored = [
        {"role": "assistant", "content": "before"},
        {"role": "user", "content": "hand me over"},
        {"role": "assistant", "content": "answered by Beta", "transfer_chain": meta["transfer_chain"]},
        {"role": "assistant", "content": "later"},
    ]
    platform_api.attribute_message_agents(restored, "Alpha")
    assert [m.get("agent_name") for m in restored] == ["Alpha", None, "Beta", "Beta"]


def test_the_completed_chain_ends_where_the_conversation_landed():
    beta = {"agent_id": "b", "agent_name": "Beta"}
    gamma = {"agent_id": "g", "agent_name": "Gamma"}
    assert platform_api._completed_transfer_chain([beta, gamma], "g") == [beta, gamma]
    assert platform_api._completed_transfer_chain([beta, gamma], "b") == [beta]
    assert platform_api._completed_transfer_chain([beta], None) == []
    assert platform_api._completed_transfer_chain([], "b") == []
    # An id no recorded hop names: the chain is kept as recorded.
    assert platform_api._completed_transfer_chain([beta], "x") == [beta]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_a_failed_first_hop_records_no_handover(monkeypatch, stream):
    """Beta's run failed: ``run_agent_chat`` answered with Alpha's reply and no
    transfer id, and the conversation stayed with Alpha. The hop was recorded
    when it started; persisting it would name Beta the author of this answer
    and of every later one on restore."""
    response, message, conversations = await _send(
        monkeypatch, hops=[(TARGET_ID, "Beta")], landed_on=None, stream=stream
    )

    assert message.metadata_json is None
    for key in ("transfer_chain", "transfer_agent_id", "transfer_agent_name"):
        assert key not in response
    (conv,) = [c for c in conversations if hasattr(c, "agent_id")]
    assert str(conv.agent_id) != TARGET_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_a_failed_nested_hop_is_cut_from_the_chain(monkeypatch, stream):
    """Beta took the conversation, then its own handover to Gamma failed and
    Beta answered: the chain and the name both end at Beta."""
    gamma_id = str(uuid.uuid4())
    response, message, conversations = await _send(
        monkeypatch, hops=[(TARGET_ID, "Beta"), (gamma_id, "Gamma")], landed_on=TARGET_ID, stream=stream
    )

    beta_only = [{"agent_id": TARGET_ID, "agent_name": "Beta"}]
    assert json.loads(message.metadata_json)["transfer_chain"] == beta_only
    assert response["transfer_chain"] == beta_only
    assert (response["transfer_agent_id"], response["transfer_agent_name"]) == (TARGET_ID, "Beta")
    (conv,) = [c for c in conversations if hasattr(c, "agent_id")]
    assert str(conv.agent_id) == TARGET_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_a_completed_chain_is_persisted_whole(monkeypatch, stream):
    gamma_id = str(uuid.uuid4())
    response, message, conversations = await _send(
        monkeypatch, hops=[(TARGET_ID, "Beta"), (gamma_id, "Gamma")], landed_on=gamma_id, stream=stream
    )

    chain = [
        {"agent_id": TARGET_ID, "agent_name": "Beta"},
        {"agent_id": gamma_id, "agent_name": "Gamma"},
    ]
    assert json.loads(message.metadata_json)["transfer_chain"] == chain
    assert response["transfer_chain"] == chain
    assert (response["transfer_agent_id"], response["transfer_agent_name"]) == (gamma_id, "Gamma")
    (conv,) = [c for c in conversations if hasattr(c, "agent_id")]
    assert str(conv.agent_id) == gamma_id


def test_both_paths_read_a_handover_through_one_helper():
    import inspect

    source = inspect.getsource(platform_api.send_platform_chat_message)
    assert source.count("_record_transfer_start(") >= 2
    assert source.count("_completed_transfer_chain(") >= 2
