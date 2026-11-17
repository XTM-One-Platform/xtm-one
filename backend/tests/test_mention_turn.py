"""A web chat turn several @mentioned agents answer, on the real
``run_chat_connection`` with the fan-out fakes: one agentic action per agent
that answers, the trace, the files and the context reading
each reply carries, on the REST send too.

A message folded into a turn already running, on the web chat and
REST sends: the fold carries the agents it mentions, they answer it after
the turn's own agents under the same metering, a refusal asks none of them,
and a fold stranded at the turn's end is answered by the next turn.

Under the relay a turn several agents answer is ONE turn: one lock,
one ``turn_id`` on every agent's frames, each reply a segment of it, the
last reply its end."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.agent_chat as agent_chat_module
import app.services.sub_agent_files as sub_agent_files_module
from app.api.v1 import chat as chat_module
from app.api.v1 import chat_web_stream
from app.llm.base import FileRef
from app.services import chat_turn_guard, chat_turn_stream, mention_dispatch
from app.services.agent_chat import QUOTA_EXCEEDED_MESSAGE
from app.services.chat_turn_stream import TURN_FRAME_TYPES, public_turn_id, turn_channel
from app.services.context_usage import CONTEXT_USAGE_META_KEYS
from app.services.integration_credentials import INTEGRATION_NOT_VISIBLE_REASON
from app.services.mention_dispatch import MentionTarget, parse_mentioned_agent_ids
from app.tools.agent_loop import steering_text
from tests.test_channel_mentions import patch_chat_gate, run_through_gate
from tests.test_chat_turn_fanout import (  # noqa: F401
    _patch_general_chat,
    _wait_until,
    fake_rl,
    fanout_redis,
    ws_env,
)
from tests.test_chat_web_steering import _FakeConnection, _FakeSession, _run_connection, _send, _wait_for_frame

B = str(uuid.uuid4())
C = str(uuid.uuid4())


def _file(file_id: str) -> FileRef:
    return FileRef(file_id=file_id, filename=f"{file_id}.txt", content_type="text/plain", size=1, s3_object_name=file_id)


@pytest.fixture
def mention_env(monkeypatch, ws_env):  # noqa: F811
    state = SimpleNamespace(trace=None, stored=[], mentioned_calls=[], loaded=[])

    async def _targets(*_a, **_kw):
        return [MentionTarget(agent_id=B, name="Beta", agent=SimpleNamespace(id=uuid.UUID(B), name="Beta"))]

    async def _own_session(agent, run):
        return await run(object(), agent)

    async def _langfuse_ids(_conv_id):
        return {"llm_trace_id": state.trace} if state.trace else {}

    original_store = chat_module.store_message

    async def _store(db, conv_id, role, content, **kw):
        state.stored.append((role, content, kw))
        message = await original_store(db, conv_id, role, content, **kw)
        # The row keeps its metadata, as ``store_message`` does.
        message.metadata_json = kw.get("metadata_json")
        return message

    async def _load_rows(ids, **_kw):
        state.loaded.append(sorted(ids))
        return [SimpleNamespace(id=i) for i in ids]

    monkeypatch.setattr(chat_module, "resolve_mention_targets", _targets)
    monkeypatch.setattr(chat_module, "run_on_own_session", _own_session)
    monkeypatch.setattr(chat_module, "load_latest_langfuse_ids", _langfuse_ids)
    monkeypatch.setattr(chat_module, "store_message", _store)
    monkeypatch.setattr(sub_agent_files_module, "load_attachment_rows", _load_rows)
    monkeypatch.setattr(sub_agent_files_module, "bind_attachment_rows", AsyncMock(side_effect=lambda _db, rows, **_kw: rows))
    monkeypatch.setattr(
        chat_module,
        "attachment_meta_from_file_attachment",
        lambda fa: {"file_id": str(fa.id), "filename": "f.txt", "type": "file", "size": 1},
    )
    state.env = ws_env
    return state


def _mentioned_run(state, *, text="beta says", trace=None, file_id=None):
    async def _run(_db, _uid, agent, _history, _content, **kwargs):
        state.mentioned_calls.append(kwargs)
        if file_id:
            kwargs["file_context"].add(_file(file_id))
        state.trace = trace
        return (text, None)

    return _run


async def _turn(state):
    return await _send(state.env, {"content": "@beta what do you think?", "mentioned_agent_ids": [B]})


@pytest.mark.parametrize("reason", ["quota_exceeded", "quota_unavailable"])
async def test_a_quota_refusal_of_the_conversation_run_asks_no_mentioned_agent(monkeypatch, mention_env, reason):
    async def _general(_db, _uid, _history, _content, **kwargs):
        kwargs["quota_out"].update({"actions_consumed": 0, "reason": reason})
        return ("You have reached your agentic execution quota.", None)

    _patch_general_chat(monkeypatch, _general)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _mentioned_run(mention_env))
    conn = await _turn(mention_env)

    assert mention_env.mentioned_calls == []
    done = [f for f in conn.sent if f.get("type") == "done"]
    assert [f["content"] for f in done] == ["You have reached your agentic execution quota."]
    assert "answered_by" not in done[0]


async def test_a_mentioned_agent_is_charged_its_own_action_and_keeps_its_own_trace_and_files(
    monkeypatch, mention_env
):
    gate = patch_chat_gate(monkeypatch)
    ga_trace, beta_trace = str(uuid.uuid4()), str(uuid.uuid4())

    async def _general(db, uid, _history, _content, **kwargs):
        await run_through_gate(db, uid, None, **kwargs)
        kwargs["file_context"].add(_file("ga-file"))
        mention_env.trace = ga_trace
        return ("assistant says", None)

    beta_run = _mentioned_run(mention_env, trace=beta_trace, file_id="beta-file")

    async def _beta(db, uid, agent, history, content, **kwargs):
        await run_through_gate(db, uid, agent.id, **kwargs)
        # A consult mid-run is a nested handover: it rides Beta's action.
        await run_through_gate(db, uid, uuid.uuid4(), meter_quota=False)
        return await beta_run(db, uid, agent, history, content, **kwargs)

    _patch_general_chat(monkeypatch, _general)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _beta)
    conn = await _turn(mention_env)

    # One agentic action (and one execution) per agent that answered.
    assert gate.charged == [None, B]
    assert gate.executions == 2
    [beta_call] = mention_env.mentioned_calls
    assert "failure_refund_out" not in beta_call
    # Each reply is stored with the trace of the run that wrote it ...
    replies = [(content, kw.get("llm_trace_id")) for role, content, kw in mention_env.stored if role == "assistant"]
    assert [(c, str(t) if t else None) for c, t in replies] == [("assistant says", ga_trace), ("beta says", beta_trace)]
    # ... and shows the files its own run wrote.
    assert mention_env.loaded == [["ga-file"], ["beta-file"]]
    done = [f for f in conn.sent if f.get("type") == "done"]
    assert [f["content"] for f in done] == ["assistant says", "beta says"]
    assert done[0]["intermediate"] is True and done[0]["attachments"][0]["file_id"] == "ga-file"
    assert done[1]["answered_by"]["agent_name"] == "Beta"
    final_meta = json.loads(mention_env.stored[-1][2]["metadata_json"])
    assert [a["file_id"] for a in final_meta["attachments"]] == ["beta-file"]


_GA_GAP = {"provider": "github", "reason": INTEGRATION_NOT_VISIBLE_REASON}
_BETA_GAP = {"provider": "jira", "reason": INTEGRATION_NOT_VISIBLE_REASON}


def _gap_runs(monkeypatch, state):
    """The assistant and Beta each meet an integration they cannot use."""

    async def _general(_db, _uid, _history, _content, **kwargs):
        kwargs["unavailable_integrations_out"].append(dict(_GA_GAP))
        return ("assistant says", None)

    beta_run = _mentioned_run(state)

    async def _beta(db, uid, agent, history, content, **kwargs):
        kwargs["unavailable_integrations_out"].append(dict(_BETA_GAP))
        return await beta_run(db, uid, agent, history, content, **kwargs)

    _patch_general_chat(monkeypatch, _general)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _beta)


def _stored_gaps(state) -> list:
    return [
        json.loads(kw.get("metadata_json") or "{}").get("unavailable_integrations")
        for role, _content, kw in state.stored
        if role == "assistant"
    ]


async def test_each_web_reply_names_the_integrations_its_own_agent_could_not_use(monkeypatch, mention_env):
    _gap_runs(monkeypatch, mention_env)
    conn = await _turn(mention_env)

    assert _stored_gaps(mention_env) == [[_GA_GAP], [_BETA_GAP]]
    done = [f for f in conn.sent if f.get("type") == "done"]
    assert [f.get("unavailable_integrations") for f in done] == [[_GA_GAP], [_BETA_GAP]]


async def test_each_rest_reply_names_the_integrations_its_own_agent_could_not_use(monkeypatch, mention_env):
    _gap_runs(monkeypatch, mention_env)
    response = await _rest_send(mention_env.env, "@beta what do you think?", mentioned_agent_ids=[B])

    assert _stored_gaps(mention_env) == [[_GA_GAP], [_BETA_GAP]]
    assert [g.model_dump(exclude_none=True) for g in response.assistant_message.unavailable_integrations] == [_GA_GAP]
    [beta] = response.mention_messages
    assert beta.answered_by["agent_name"] == "Beta"
    assert [g.model_dump(exclude_none=True) for g in beta.unavailable_integrations] == [_BETA_GAP]


# How full each run's context was: the composer's gauge reads
# the reply on screen, so a reply must carry the reading of the run that wrote
# it - never the conversation agent's.
_GA_READING = {
    "context_tokens": 84_000,
    "context_window": 200_000,
    "context_breakdown": {"conversation": 80_000, "system": 4_000},
    "context_level": "approaching",
}
_BETA_READING = {"context_tokens": 1_200, "context_window": 128_000, "context_level": "normal"}


def _reading_runs(monkeypatch, state, *, beta_reads: bool = True):
    """The assistant's run and Beta's each publish their loop's iteration tick."""

    async def _general(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": "thinking", "iteration": 1, **_GA_READING})
        return ("assistant says", None)

    beta_run = _mentioned_run(state)

    async def _beta(db, uid, agent, history, content, **kwargs):
        if beta_reads:
            await kwargs["on_event"]({"status": "thinking", "iteration": 1, **_BETA_READING})
        return await beta_run(db, uid, agent, history, content, **kwargs)

    _patch_general_chat(monkeypatch, _general)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _beta)


def _reading(source: dict) -> dict:
    return {k: source[k] for k in CONTEXT_USAGE_META_KEYS if source.get(k) is not None}


def _stored_readings(state) -> list[dict]:
    return [
        _reading(json.loads(kw.get("metadata_json") or "{}"))
        for role, _content, kw in state.stored
        if role == "assistant"
    ]


def _offered_frames(monkeypatch) -> list[dict]:
    """Every frame the REST turn publishes to the conversation's streams."""
    offered: list[dict] = []
    real_offer = chat_turn_stream.TurnPublisher.offer

    def _offer(self, frame):
        offered.append(frame)
        return real_offer(self, frame)

    monkeypatch.setattr(chat_turn_stream.TurnPublisher, "offer", _offer)
    return offered


async def test_each_web_reply_stores_the_context_its_own_run_ran_in(monkeypatch, mention_env):
    _reading_runs(monkeypatch, mention_env)
    conn = await _turn(mention_env)

    assert _stored_readings(mention_env) == [_GA_READING, _BETA_READING]
    done = [f for f in conn.sent if f.get("type") == "done"]
    assert [_reading(f) for f in done] == [_GA_READING, _BETA_READING]
    # The live gauge reads the ticks themselves, the loop's level included.
    ticks = [f for f in conn.sent if f.get("status") == "thinking" and "context_tokens" in f]
    assert [_reading(f) for f in ticks] == [_GA_READING, _BETA_READING]


async def test_each_rest_reply_stores_the_context_its_own_run_ran_in(monkeypatch, mention_env):
    _reading_runs(monkeypatch, mention_env)
    offered = _offered_frames(monkeypatch)
    response = await _rest_send(mention_env.env, "@beta what do you think?", mentioned_agent_ids=[B])

    assert _stored_readings(mention_env) == [_GA_READING, _BETA_READING]
    assert _reading(response.assistant_message.model_dump()) == _GA_READING
    [beta] = response.mention_messages
    assert _reading(beta.model_dump()) == _BETA_READING
    done = [f for f in offered if f.get("type") == "done"]
    assert [_reading(f) for f in done] == [_GA_READING, _BETA_READING]


@pytest.mark.parametrize("path", ["web", "rest"])
async def test_a_reply_whose_run_published_no_reading_carries_none(monkeypatch, mention_env, path):
    """Beta's run never reached the model: its reply stores no reading rather
    than the one the assistant's run left behind."""
    _reading_runs(monkeypatch, mention_env, beta_reads=False)
    if path == "web":
        await _turn(mention_env)
    else:
        response = await _rest_send(mention_env.env, "@beta what do you think?", mentioned_agent_ids=[B])
        [beta] = response.mention_messages
        assert _reading(beta.model_dump()) == {}

    assert _stored_readings(mention_env) == [_GA_READING, {}]


async def test_a_mentioned_agent_the_quota_refuses_says_so_and_nobody_after_it_is_asked(monkeypatch, mention_env):
    """One action left: the assistant is charged, Beta's charge is refused with
    the chat gate's own sentence, and Gamma is not asked."""
    gate = patch_chat_gate(monkeypatch, allowance=1)
    asked: list[str] = []

    async def _targets(*_a, **_kw):
        return [
            MentionTarget(agent_id=B, name="Beta", agent=SimpleNamespace(id=uuid.UUID(B), name="Beta")),
            MentionTarget(agent_id=C, name="Gamma", agent=SimpleNamespace(id=uuid.UUID(C), name="Gamma")),
        ]

    async def _general(db, uid, _history, _content, **kwargs):
        await run_through_gate(db, uid, None, **kwargs)
        return ("assistant says", None)

    async def _mentioned(db, uid, agent, _history, _content, **kwargs):
        asked.append(agent.name)
        refusal = await run_through_gate(db, uid, agent.id, **kwargs)
        return (refusal or f"{agent.name.lower()} says", None)

    monkeypatch.setattr(chat_module, "resolve_mention_targets", _targets)
    _patch_general_chat(monkeypatch, _general)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _mentioned)
    conn = await _turn(mention_env)

    assert asked == ["Beta"]
    assert gate.charged == [None]
    done = [f for f in conn.sent if f.get("type") == "done"]
    assert [f["content"] for f in done] == ["assistant says", QUOTA_EXCEEDED_MESSAGE]
    assert done[1]["answered_by"]["agent_name"] == "Beta"


async def test_a_message_without_mentions_is_charged_once_as_on_main(monkeypatch, mention_env):
    gate = patch_chat_gate(monkeypatch)
    calls: list[dict] = []

    async def _general(db, uid, _history, _content, **kwargs):
        calls.append(kwargs)
        await run_through_gate(db, uid, None, **kwargs)
        return ("assistant says", None)

    monkeypatch.setattr(chat_module, "resolve_mention_targets", mention_dispatch.resolve_mention_targets)
    _patch_general_chat(monkeypatch, _general)
    conn = await _send(mention_env.env, {"content": "what do you think?"})

    assert gate.charged == [None] and gate.executions == 1
    [call] = calls
    assert call["quota_out"] is None
    assert "meter_quota" not in call and "failure_refund_out" not in call
    assert "allowed_handover_types" not in call
    done = [f for f in conn.sent if f.get("type") == "done"]
    assert [f["content"] for f in done] == ["assistant says"]
    # Nothing of a multi-agent turn shows: no mention status, no attribution,
    # no intermediate segment, one reply stored.
    assert not [f for f in conn.sent if f.get("status") == "mention_answering"]
    assert not {"answered_by", "mention_failed", "intermediate"} & done[0].keys()
    _assert_one_plain_reply(mention_env)


def _assert_one_plain_reply(state) -> None:
    replies = [
        (content, json.loads(kw.get("metadata_json") or "{}"))
        for role, content, kw in state.stored
        if role == "assistant"
    ]
    assert [content for content, _ in replies] == ["assistant says"]
    assert not {"answered_by", "mention_failed"} & replies[0][1].keys()


async def test_a_rest_message_without_mentions_is_answered_as_on_main(monkeypatch, mention_env):
    gate = patch_chat_gate(monkeypatch)
    calls: list[dict] = []

    async def _general(db, uid, _history, _content, **kwargs):
        calls.append(kwargs)
        await run_through_gate(db, uid, None, **kwargs)
        return ("assistant says", None)

    monkeypatch.setattr(chat_module, "resolve_mention_targets", mention_dispatch.resolve_mention_targets)
    _patch_general_chat(monkeypatch, _general)
    response = await _rest_send(mention_env.env, "what do you think?")

    assert gate.charged == [None] and gate.executions == 1
    [call] = calls
    assert call["quota_out"] is None and "allowed_handover_types" not in call
    assert response.assistant_message.content == "assistant says"
    assert response.assistant_message.answered_by is None
    assert response.mention_messages is None
    _assert_one_plain_reply(mention_env)


# ---------------------------------------------------------------------------
# A message folded into a turn already running
# ---------------------------------------------------------------------------

FOLDED = "@beta your view?"


def _beta_target() -> MentionTarget:
    return MentionTarget(agent_id=B, name="Beta", agent=SimpleNamespace(id=uuid.UUID(B), name="Beta"))


@pytest.fixture
def known_agents(monkeypatch):
    """Resolve the agents a message names among Beta alone, dropping the one answering."""

    async def _targets(_db, *, user_id, raw_ids, answering_agent_id, language="en"):
        return [_beta_target() for i in parse_mentioned_agent_ids(raw_ids) if i == B and i != answering_agent_id]

    monkeypatch.setattr(chat_module, "resolve_mention_targets", _targets)
    monkeypatch.setattr(mention_dispatch, "resolve_mention_targets", _targets)


def _beta_run(state, *, text="beta says"):
    async def _run(db, uid, agent, history, content, **kwargs):
        state.mentioned_calls.append({"content": content, "history": [m.content for m in history], **kwargs})
        refusal = await run_through_gate(db, uid, agent.id, **kwargs)
        return (refusal or text, None)

    return _run


async def _fold_and_wait(conv_id: str, steering_queue) -> str:
    """What another send does while the turn runs, then what the turn's bridge hands its loop."""
    assert await chat_turn_guard.fold_into_inflight_turn(conv_id, FOLDED, mentioned_agent_ids=[B])
    return steering_text(await asyncio.wait_for(steering_queue.get(), timeout=2))


async def test_a_send_folded_into_a_running_turn_carries_the_agents_it_mentions(
    monkeypatch, mention_env, known_agents
):
    """The fold carries the agents the message mentions and names the
    turn that took it: that turn answers the message, and its agents."""
    conv_id = str(mention_env.env.conv.id)
    running = await chat_turn_guard.acquire_turn(conv_id)
    assert running is not None
    ran: list[str] = []

    async def _general(_db, _uid, _history, content, **_kw):
        ran.append(content)
        return ("never", None)

    _patch_general_chat(monkeypatch, _general)
    # The send then relays the turn it folded into until it ends: this one never does.
    monkeypatch.setattr(chat_module, "FOLD_RELAY_PROBE_SECONDS", 0.02)
    conn = _FakeConnection(conv_id, {"content": FOLDED, "mentioned_agent_ids": [B]}, mention_env.env.timeline)
    task = asyncio.create_task(_run_connection(conn, mention_env.env.user_id))
    frame = await _wait_for_frame(conn, "folded_into_active_turn")
    conn.detach()
    await asyncio.wait_for(task, 5)

    assert ran == []
    assert frame is not None
    assert "mentions_not_asked" not in frame
    assert frame["turn_id"] == public_turn_id(running)
    [payload] = await chat_turn_guard.drain_steering_inbox(conv_id)
    assert (payload.text, payload.mentioned_agent_ids) == (FOLDED, [B])


async def test_a_folded_message_is_answered_by_its_agents_after_the_turn_each_charged_once(
    monkeypatch, mention_env, known_agents
):
    """The agent already answering reads the folded message as steering; the
    agent it mentions answers it once that answer is done, with the message's
    own text, reading the turn's reply, on an action of its own."""
    gate = patch_chat_gate(monkeypatch)
    conv_id = str(mention_env.env.conv.id)
    steered: list[str] = []

    async def _general(db, uid, _history, _content, **kwargs):
        await run_through_gate(db, uid, None, **kwargs)
        steered.append(await _fold_and_wait(conv_id, kwargs["steering_queue"]))
        return ("assistant says", None)

    _patch_general_chat(monkeypatch, _general)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _beta_run(mention_env))
    conn = await _send(mention_env.env, {"content": "what do you think?"})

    assert steered == [FOLDED]
    [beta_call] = mention_env.mentioned_calls
    assert beta_call["content"] == FOLDED
    assert beta_call["history"] == ["what do you think?", "[Reply from Agent]\n\nassistant says"]
    assert gate.charged == [None, B] and gate.executions == 2
    done = [f for f in conn.sent if f.get("type") == "done"]
    assert [f["content"] for f in done] == ["assistant says", "beta says"]
    assert done[0]["intermediate"] is True
    assert done[1]["answered_by"]["agent_name"] == "Beta"
    assert await chat_turn_guard.drain_steering_inbox(conv_id) == []


async def test_a_quota_refusal_asks_no_agent_a_folded_message_mentions(monkeypatch, mention_env, known_agents):
    """A fold its turn never consumed is purged by the next acquisition, which
    owes its agents; that turn's refused charge asks none of them, and they
    are not handed back either."""
    gate = patch_chat_gate(monkeypatch, allowance=0)
    conv_id = str(mention_env.env.conv.id)
    await chat_turn_guard.requeue_stranded(
        conv_id, [chat_turn_guard.SteeringPayload(text=FOLDED, mentioned_agent_ids=[B])]
    )
    primary_quota: list = []

    async def _general(db, uid, _history, _content, **kwargs):
        primary_quota.append(kwargs.get("quota_out"))
        refusal = await run_through_gate(db, uid, None, **kwargs)
        return (refusal or "assistant says", None)

    _patch_general_chat(monkeypatch, _general)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _beta_run(mention_env))
    conn = await _send(mention_env.env, {"content": "what do you think?"})

    assert mention_env.mentioned_calls == []
    assert primary_quota[0]["reason"] == "quota_exceeded"
    assert gate.charged == []
    done = [f for f in conn.sent if f.get("type") == "done"]
    assert [f["content"] for f in done] == [QUOTA_EXCEEDED_MESSAGE]
    assert await chat_turn_guard.drain_steering_inbox(conv_id) == []


async def test_a_fold_stranded_at_the_end_of_a_turn_is_answered_by_the_next_one(
    monkeypatch, mention_env, known_agents
):
    """A fold landing after the turn's last drain is stranded at its release:
    the message shapes the next turn through its history, and the agent it
    mentions is handed back to the inbox and answers it after that turn -
    without the text being injected a second time."""
    gate = patch_chat_gate(monkeypatch)
    conv_id = str(mention_env.env.conv.id)
    real_release = chat_turn_guard.release_turn_and_drain
    racing = [True]

    async def _release_racing_a_fold(cid, token):
        if racing:
            racing.clear()
            assert await chat_turn_guard.fold_into_inflight_turn(cid, FOLDED, mentioned_agent_ids=[B])
        return await real_release(cid, token)

    monkeypatch.setattr(chat_turn_guard, "release_turn_and_drain", _release_racing_a_fold)
    answers = iter(["first answer", "second answer"])
    steered: list[list[str]] = []

    async def _general(db, uid, _history, _content, **kwargs):
        await run_through_gate(db, uid, None, **kwargs)
        await asyncio.sleep(0.1)
        queue = kwargs["steering_queue"]
        steered.append([steering_text(queue.get_nowait()) for _ in range(queue.qsize())])
        return (next(answers), None)

    _patch_general_chat(monkeypatch, _general)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _beta_run(mention_env))
    await _send(mention_env.env, {"content": "first"})
    [handed_back] = mention_env.env.redis.lists[chat_turn_guard.steering_inbox_key(conv_id)]
    assert json.loads(handed_back)["mentions_only"] is True
    assert mention_env.mentioned_calls == []

    second = await _send(mention_env.env, {"content": "second"}, timeline=[])

    assert steered == [[], []]
    [beta_call] = mention_env.mentioned_calls
    assert beta_call["content"] == FOLDED
    assert gate.charged == [None, None, B]
    done = [f for f in second.sent if f.get("type") == "done"]
    assert [f["content"] for f in done] == ["second answer", "beta says"]
    assert await chat_turn_guard.drain_steering_inbox(conv_id) == []


async def test_a_fold_handed_back_to_a_running_turn_is_answered_without_its_text_again(
    monkeypatch, mention_env, known_agents
):
    """Another turn handed a fold back while this one runs: its text is in the
    thread already, so the bridge only records its agents, who answer it."""
    patch_chat_gate(monkeypatch)
    conv_id = str(mention_env.env.conv.id)
    injected: list[str] = []

    async def _general(db, uid, _history, _content, **kwargs):
        await run_through_gate(db, uid, None, **kwargs)
        await chat_turn_guard.requeue_owed_mentions(
            conv_id, [chat_turn_guard.SteeringPayload(text=FOLDED, mentioned_agent_ids=[B])]
        )
        await asyncio.sleep(0.1)
        queue = kwargs["steering_queue"]
        injected.extend(steering_text(queue.get_nowait()) for _ in range(queue.qsize()))
        return ("assistant says", None)

    _patch_general_chat(monkeypatch, _general)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _beta_run(mention_env))
    conn = await _send(mention_env.env, {"content": "what do you think?"})

    assert injected == []
    assert [c["content"] for c in mention_env.mentioned_calls] == [FOLDED]
    done = [f for f in conn.sent if f.get("type") == "done"]
    assert [f["content"] for f in done] == ["assistant says", "beta says"]


def _rest_send(env, content: str, **fields):
    return chat_module.send_message(
        conversation_id=str(env.conv.id),
        body=chat_module.SendMessageRequest(content=content, **fields),
        user=SimpleNamespace(id=env.user_id),
        db=_FakeSession(env.conv, env.timeline),
    )


async def test_a_rest_send_folded_into_a_running_turn_carries_the_agents_it_mentions(
    monkeypatch, mention_env, known_agents
):
    conv_id = str(mention_env.env.conv.id)
    running = await chat_turn_guard.acquire_turn(conv_id)
    assert running is not None

    response = await _rest_send(mention_env.env, FOLDED, mentioned_agent_ids=[B])

    assert response.status_code == 202
    body = json.loads(response.body)
    assert body["status"] == "folded_into_active_turn"
    assert "mentions_not_asked" not in body
    assert body["turn_id"] == public_turn_id(running)
    [payload] = await chat_turn_guard.drain_steering_inbox(conv_id)
    assert (payload.text, payload.mentioned_agent_ids) == (FOLDED, [B])


async def test_a_rest_turn_answers_a_folded_message_with_its_agents(monkeypatch, mention_env, known_agents):
    """The REST turn's bridge records the fold; its agent answers it after the
    turn's own, charged its own action, and the reply is in
    ``mention_messages``."""
    gate = patch_chat_gate(monkeypatch)
    conv_id = str(mention_env.env.conv.id)
    steered: list[str] = []

    async def _general(db, uid, _history, _content, **kwargs):
        await run_through_gate(db, uid, None, **kwargs)
        steered.append(await _fold_and_wait(conv_id, kwargs["steering_queue"]))
        return ("assistant says", None)

    _patch_general_chat(monkeypatch, _general)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _beta_run(mention_env))

    response = await _rest_send(mention_env.env, "what do you think?")

    assert steered == [FOLDED]
    [beta_call] = mention_env.mentioned_calls
    assert beta_call["content"] == FOLDED
    assert beta_call["history"] == ["what do you think?", "[Reply from Agent]\n\nassistant says"]
    assert gate.charged == [None, B]
    assert response.assistant_message.content == "assistant says"
    assert [m.content for m in response.mention_messages] == ["beta says"]
    beta_meta = json.loads(mention_env.stored[-1][2]["metadata_json"])
    assert beta_meta["answered_by"]["agent_name"] == "Beta"
    assert await chat_turn_guard.drain_steering_inbox(conv_id) == []


# ---------------------------------------------------------------------------
# The relay: the agents of one message answer in ONE relayed turn
# ---------------------------------------------------------------------------


def _record_turn_tokens(monkeypatch) -> list[str]:
    """The lock tokens the turns of the test acquire, in order."""
    tokens: list[str] = []
    real_acquire = chat_turn_guard.acquire_turn

    async def _acquire(conversation_id):
        token = await real_acquire(conversation_id)
        if token is not None:
            tokens.append(token)
        return token

    monkeypatch.setattr(chat_turn_guard, "acquire_turn", _acquire)
    return tokens


async def _viewer(monkeypatch, state) -> tuple[list[dict], asyncio.Task]:
    """Another tab of the conversation's owner on its events stream, subscribed before the turn."""
    conv_id = str(state.env.conv.id)

    async def _released(_db):
        return None

    monkeypatch.setattr(chat_web_stream, "release_db_connection_before_loop", _released)
    response = await chat_web_stream.stream_web_chat_events(
        conv_id,
        SimpleNamespace(headers={}),
        client_id="viewer",
        db=SimpleNamespace(execute=_FakeSession(state.env.conv, []).execute),
        user=SimpleNamespace(id=state.env.user_id, web_session=SimpleNamespace()),
    )
    frames: list[dict] = []

    async def _read():
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            if text.startswith("data: "):
                frames.append(json.loads(text[6:]))

    task = asyncio.create_task(_read())
    await _wait_until(lambda: state.env.redis.subscribers.get(turn_channel(conv_id)))
    await asyncio.sleep(0.02)
    return frames, task


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _turn_frames(frames: list[dict]) -> list[dict]:
    return [f for f in frames if f.get("type") in TURN_FRAME_TYPES]


def _turn_ended(frames: list[dict]) -> bool:
    return any(chat_turn_stream.is_terminal_frame(f) for f in _turn_frames(frames))


async def _assert_one_relayed_turn(
    state, viewer: list[dict], token: str, done_ids: list[str], *, replies_stored_at_end: bool = False
) -> None:
    """The viewer saw the agents' replies as the segments of ONE turn: the
    conversation agent's an intermediate ``done``, Beta's announced by
    ``mention_answering`` and ending the turn - and the tombstone keeps that
    end for a stream that resyncs after it. A web chat turn commits each
    reply before the next agent starts; a REST turn stores and publishes its
    replies once every agent answered (``replies_stored_at_end``)."""
    relayed = _turn_frames(viewer)
    assert relayed and {f["turn_id"] for f in relayed} == {public_turn_id(token)}
    assert all(f["relayed"] is True for f in relayed)
    done = [f for f in relayed if f["type"] == "done"]
    assert [(f["content"], f.get("intermediate")) for f in done] == [
        ("assistant says", True),
        ("beta says", None),
    ]
    assert [f["message_id"] for f in done] == done_ids
    assert "answered_by" not in done[0]
    assert done[1]["answered_by"]["agent_name"] == "Beta"
    [answering] = [f for f in relayed if f.get("status") == "mention_answering"]
    assert answering["agent_name"] == "Beta" and (answering["position"], answering["total"]) == (1, 1)
    if replies_stored_at_end:
        assert relayed.index(answering) < relayed.index(done[0]) < relayed.index(done[1])
    else:
        assert relayed.index(done[0]) < relayed.index(answering) < relayed.index(done[1])
    assert not [f for f in relayed if f["type"] == "error"], "a turn several agents answer is never lost"
    end = await chat_turn_stream.probe_turn_end(str(state.env.conv.id), token)
    assert end is not None and end.over and end.ended
    assert end.terminal["content"] == "beta says" and "intermediate" not in end.terminal


async def test_a_web_turn_several_agents_answer_reaches_a_viewer_as_one_relayed_turn(monkeypatch, mention_env):
    tokens = _record_turn_tokens(monkeypatch)

    async def _general(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_token"]("assistant says")
        return ("assistant says", None)

    _patch_general_chat(monkeypatch, _general)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _mentioned_run(mention_env))
    viewer, events = await _viewer(monkeypatch, mention_env)
    try:
        owner = await _send(
            mention_env.env, {"content": "@beta what do you think?", "mentioned_agent_ids": [B]}, timeline=[]
        )
        await _wait_until(lambda: _turn_ended(viewer), timeout=4.0)
    finally:
        await _stop(events)

    [token] = tokens
    own = _turn_frames(owner.sent)
    # Every frame, the acknowledgement and the mentioned agent's included,
    # names the one turn.
    assert own[0]["type"] == "thinking"
    assert {f.get("turn_id") for f in own} == {public_turn_id(token)}
    own_done = [f["message_id"] for f in own if f["type"] == "done"]
    await _assert_one_relayed_turn(mention_env, viewer, token, own_done)


async def test_a_rest_turn_several_agents_answer_reaches_a_viewer_as_one_relayed_turn(monkeypatch, mention_env):
    """The REST turn publishes its replies once they are all stored: the
    viewer gets them as the same segments, under the REST turn's
    ``turn_id``, after the mentioned agent's progress."""
    tokens = _record_turn_tokens(monkeypatch)

    async def _general(_db, _uid, _history, _content, **_kwargs):
        return ("assistant says", None)

    _patch_general_chat(monkeypatch, _general)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _mentioned_run(mention_env))
    viewer, events = await _viewer(monkeypatch, mention_env)
    try:
        response = await _rest_send(mention_env.env, "@beta what do you think?", mentioned_agent_ids=[B])
        await _wait_until(lambda: _turn_ended(viewer), timeout=4.0)
    finally:
        await _stop(events)

    [token] = tokens
    [beta] = response.mention_messages
    await _assert_one_relayed_turn(
        mention_env,
        viewer,
        token,
        [str(response.assistant_message.id), str(beta.id)],
        replies_stored_at_end=True,
    )


# ---------------------------------------------------------------------------
# The turn's line-up: the conversation's agent, bound or the General
# Assistant, and each mentioned agent but the last read who answers after
# them, on the web chat and REST sends alike
# ---------------------------------------------------------------------------


def _gamma_target() -> MentionTarget:
    return MentionTarget(agent_id=C, name="Gamma", agent=SimpleNamespace(id=uuid.UUID(C), name="Gamma"))


def _bind_to_alpha(monkeypatch, state) -> None:
    """The conversation is bound to the agent Alpha (the fake session finds it)."""
    conv = state.env.conv
    conv.agent_id = uuid.uuid4()
    conv.name, conv.enabled, conv.disable_chat = "Alpha", True, False

    async def _alpha_target(_db, agent_id):
        return MentionTarget(agent_id=str(agent_id), name="Alpha")

    monkeypatch.setattr(mention_dispatch, "conversation_agent_target", _alpha_target)


def _lineup_runs(monkeypatch, state) -> list[tuple[str, list[str]]]:
    """Every agent run of the turn, in order: who ran and the agents it was told answer after it."""
    runs: list[tuple[str, list[str]]] = []

    def _after(kwargs) -> list[str]:
        return [t.name for t in kwargs["agents_after"]]

    async def _general(_db, _uid, _history, _content, **kwargs):
        runs.append(("General Assistant", _after(kwargs)))
        return ("assistant says", None)

    async def _agent(_db, _uid, agent, _history, _content, **kwargs):
        runs.append((agent.name, _after(kwargs)))
        return (f"{agent.name.lower()} says", None)

    _patch_general_chat(monkeypatch, _general)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _agent)
    return runs


async def _answers(state, surface: str, content: str, mentioned: list[str]):
    if surface == "web":
        payload = {"content": content, **({"mentioned_agent_ids": mentioned} if mentioned else {})}
        conn = await _send(state.env, payload)
        return [f["content"] for f in conn.sent if f.get("type") == "done"]
    response = await _rest_send(state.env, content, **({"mentioned_agent_ids": mentioned} if mentioned else {}))
    return [response.assistant_message.content, *(m.content for m in response.mention_messages or [])]


@pytest.mark.parametrize("surface", ["web", "rest"])
@pytest.mark.parametrize("bound", [True, False], ids=["agent", "general-assistant"])
async def test_the_conversation_agent_reads_that_the_mentioned_agent_answers_after_it(
    monkeypatch, mention_env, surface, bound
):
    if bound:
        _bind_to_alpha(monkeypatch, mention_env)
    runs = _lineup_runs(monkeypatch, mention_env)

    answers = await _answers(mention_env, surface, "@beta what do you think?", [B])

    first = "Alpha" if bound else "General Assistant"
    assert runs == [(first, ["Beta"]), ("Beta", [])]
    assert answers == ["alpha says" if bound else "assistant says", "beta says"]


@pytest.mark.parametrize("surface", ["web", "rest"])
async def test_each_agent_but_the_last_reads_the_agents_after_it(monkeypatch, mention_env, surface):
    async def _targets(*_a, **_kw):
        return [_beta_target(), _gamma_target()]

    monkeypatch.setattr(chat_module, "resolve_mention_targets", _targets)
    runs = _lineup_runs(monkeypatch, mention_env)

    await _answers(mention_env, surface, "@beta @gamma what do you think?", [B, C])

    assert runs == [("General Assistant", ["Beta", "Gamma"]), ("Beta", ["Gamma"]), ("Gamma", [])]


@pytest.mark.parametrize("surface", ["web", "rest"])
async def test_a_message_naming_no_other_agent_carries_no_line_up(monkeypatch, mention_env, surface):
    monkeypatch.setattr(chat_module, "resolve_mention_targets", mention_dispatch.resolve_mention_targets)
    runs = _lineup_runs(monkeypatch, mention_env)

    await _answers(mention_env, surface, "what do you think?", [])

    assert runs == [("General Assistant", [])]

