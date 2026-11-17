"""One turn per chat conversation: ordering, coalescing window, merge.

Two Teams messages of one sender, a few seconds apart, used to start two runs
that both answered. The rules are pure (``services/chat_turn_coalescing.py``);
the engine paths are exercised with the delivery queue's session double.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services import assignment_dispatch
from app.services import assignment_engine as ae
from app.services import chat_turn_coalescing as coalescing
from app.services.event_poller import teams_chat_payload, teams_reply_payload
from tests.test_assignment_delivery_queue import (
    _ai_enabled,  # noqa: F401 - the autouse fixture of the delivery-queue suite
    _build_agent,
    _build_assignment,
    _FakeSession,
    _patch_engine,
    _patch_slots,
)

CHAT_CONTEXT = {"type": "integration_event", "provider": "microsoft", "event": "new_teams_chat_message"}


def _message(message_id: str, text: str, *, sender: str = "Sam", chat: str = "19:chat@thread.v2") -> dict:
    return teams_chat_payload(
        {
            "id": message_id,
            "body": {"content": text},
            "from": {"user": {"displayName": sender, "id": f"id-{sender}"}},
            "createdDateTime": "2026-11-02T21:46:54Z",
        },
        chat_id=chat,
    )


KEY = "teams-chat:19:chat@thread.v2"


# ── Pure rules ───────────────────────────────────────────────────────────────


def test_the_teams_payload_builders_name_the_conversation():
    assert _message("m1", "a")[assignment_dispatch.CONVERSATION_KEY_FIELD] == KEY
    reply = teams_reply_payload(
        {"id": "r1", "body": {"content": "x"}}, team_id="T", channel_id="C", channel_name="general", parent_id="P"
    )
    assert reply[assignment_dispatch.CONVERSATION_KEY_FIELD] == "teams-thread:T:C:P"
    orphan = teams_reply_payload({"id": "r2"}, team_id="T", channel_id="C", channel_name="general", parent_id="")
    assert assignment_dispatch.CONVERSATION_KEY_FIELD not in orphan


@pytest.mark.parametrize(
    ("context", "payload", "expected"),
    [
        (CHAT_CONTEXT, {"conversation_key": "k"}, True),
        (CHAT_CONTEXT, {"chat_id": "c"}, False),
        ({**CHAT_CONTEXT, "type": "manual"}, {"conversation_key": "k"}, False),
        (CHAT_CONTEXT, None, False),
        (None, {"conversation_key": "k"}, False),
    ],
)
def test_only_an_integration_event_naming_its_conversation_is_a_conversation_event(context, payload, expected):
    assert coalescing.is_conversation_event(context, payload) is expected
    assert coalescing.forces_queue(context, payload) is expected


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, 6),
        ({"coalesce_window_seconds": 0}, 0),
        ({"coalesce_window_seconds": 3}, 3),
        ({"coalesce_window_seconds": 600}, coalescing.MAX_WINDOW_SECONDS),
        ({"coalesce_window_seconds": -4}, 0),
        ({"coalesce_window_seconds": "x"}, 6),
        ({"coalesce_window_seconds": True}, 6),
        (None, 6),
    ],
)
def test_the_window_is_the_assignments_own_else_the_default_and_bounded(config, expected):
    assert coalescing.window_seconds(config, 6) == expected


def test_two_messages_of_one_sender_merge_into_one_turn_in_order():
    first = _message("m1", "But did you relay the emoji????")
    second = _message("m2", "Inplace update")

    merged = coalescing.merged_payload(first, second)

    assert merged["message_id"] == "m1"
    assert merged["message_ids"] == ["m1", "m2"]
    assert [m["text"] for m in merged["messages"]] == ["But did you relay the emoji????", "Inplace update"]
    assert merged["text"] == "But did you relay the emoji????\n\nInplace update"
    assert merged["coalesced_count"] == 2
    assert merged["coalesced_note"].startswith("2 messages merged")
    assert "once" in merged["coalesced_note"]


def test_a_third_message_extends_the_merged_turn():
    turn = coalescing.merged_payload(_message("m1", "a"), _message("m2", "b"))
    turn = coalescing.merged_payload(turn, _message("m3", "c"))
    assert turn["message_ids"] == ["m1", "m2", "m3"]
    assert turn["coalesced_count"] == 3


def test_another_sender_a_repeat_or_a_full_turn_never_merges():
    waiting = _message("m1", "a")
    assert coalescing.can_merge(waiting, _message("m2", "b"))
    assert not coalescing.can_merge(waiting, _message("m2", "b", sender="Someone else"))
    assert not coalescing.can_merge(waiting, _message("m1", "a"))
    assert coalescing.carries(waiting, _message("m1", "a"))
    assert not coalescing.can_merge({**waiting, "from_name": ""}, {**_message("m2", "b"), "from_name": ""})
    full = {**waiting, "messages": [{"message_id": f"x{i}"} for i in range(coalescing.MAX_MERGED_MESSAGES)]}
    assert not coalescing.can_merge(full, _message("m2", "b"))


def test_a_window_about_to_end_is_left_alone():
    now = datetime(2026, 11, 2, 21, 47, tzinfo=UTC)
    assert coalescing.still_open(now + timedelta(seconds=4), now)
    assert not coalescing.still_open(now + timedelta(seconds=0.5), now)
    assert not coalescing.still_open(now - timedelta(seconds=1), now)
    assert not coalescing.still_open(None, now)


# ── Engine ───────────────────────────────────────────────────────────────────


def _chat_assignment(**config):
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    assignment.trigger_type = "integration_event"
    assignment.trigger_config = {"provider": "microsoft", "event": "new_teams_chat_message", **config}
    return agent, assignment


@pytest.fixture
def _no_event_dedup(monkeypatch):
    monkeypatch.setattr(ae.settings, "assignment_event_dedup_window_seconds", 0)
    monkeypatch.setattr(ae.settings, "chat_event_coalesce_window_seconds", 6)


async def _deliver(monkeypatch, assignment, agent, payload, *, active=0, keys=(), queue=False, merged=None):
    created = _patch_engine(monkeypatch)
    _patch_slots(monkeypatch, active=active, keys=keys)
    merges: list[dict] = []

    async def _merge(_db, _assignment, ordering_key, incoming):
        merges.append({"key": ordering_key, "payload": incoming})
        return merged

    async def _count(_db, _assignment_id):
        return 0

    monkeypatch.setattr(ae, "_merge_into_waiting_turn", _merge)
    monkeypatch.setattr(ae, "count_queued_runs", _count)
    session = _FakeSession()
    result = await ae.trigger_or_queue_assignment(
        assignment, agent, payload, db=session, trigger_context=dict(CHAT_CONTEXT), queue=queue
    )
    return result, session, created, merges


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_event_dedup")
async def test_the_first_message_of_a_chat_starts_after_the_coalescing_window(monkeypatch):
    agent, assignment = _chat_assignment()
    before = datetime.now(UTC)

    result, session, created, merges = await _deliver(monkeypatch, assignment, agent, _message("m1", "a"))

    assert result.status == "ok"
    run = session.added[0]
    assert run.status == "delayed"
    assert run.trigger_context[assignment_dispatch.ORDERING_KEY_CONTEXT] == KEY
    assert run.trigger_context[coalescing.WINDOW_CONTEXT] == 6
    assert created[0]["run_at"] - before >= timedelta(seconds=6)
    assert merges == [{"key": KEY, "payload": _message("m1", "a")}]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_event_dedup")
async def test_a_window_of_zero_starts_the_run_at_once(monkeypatch):
    agent, assignment = _chat_assignment(coalesce_window_seconds=0)

    result, session, _created, _merges = await _deliver(monkeypatch, assignment, agent, _message("m1", "a"))

    assert result.status == "ok"
    assert session.added[0].status == "pending"
    assert coalescing.WINDOW_CONTEXT not in session.added[0].trigger_context


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_event_dedup")
async def test_a_second_message_three_seconds_later_joins_the_waiting_turn(monkeypatch):
    """Two messages 3 s apart in one chat: one run, so one reply."""
    agent, assignment = _chat_assignment()
    waiting = str(uuid.uuid4())

    result, session, created, merges = await _deliver(
        monkeypatch, assignment, agent, _message("m2", "b"), active=1, keys=(KEY,), merged=waiting
    )

    assert result.status == "coalesced"
    assert result.run_id == waiting
    assert session.added == [] and created == []
    assert session.commits == 1
    assert merges[0]["key"] == KEY


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_event_dedup")
async def test_a_message_arriving_mid_run_queues_behind_it_even_with_the_event_queue_off(monkeypatch):
    """Never dropped and never run beside the run of its chat: queued, started once it ends."""
    agent, assignment = _chat_assignment(max_concurrent_runs=5)
    monkeypatch.setattr(ae.settings, "integration_event_queue_enabled", False)

    result, session, created, _merges = await _deliver(
        monkeypatch, assignment, agent, _message("m3", "c"), active=1, keys=(KEY,), queue=False
    )

    assert result.status == "queued"
    run = session.added[0]
    assert run.status == ae.QUEUED_RUN_STATUS
    assert run.trigger_context[assignment_dispatch.ORDERING_KEY_CONTEXT] == KEY
    assert run.trigger_context[ae.QUEUE_ORIGIN_KEY] == ae.INTEGRATION_EVENT_QUEUE_ORIGIN
    assert created == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_event_dedup")
async def test_another_chat_starts_beside_a_busy_one(monkeypatch):
    agent, assignment = _chat_assignment(max_concurrent_runs=5)

    result, session, created, _merges = await _deliver(
        monkeypatch, assignment, agent, _message("m4", "d", chat="19:other@thread.v2"), active=1, keys=(KEY,)
    )

    assert result.status == "ok"
    assert session.added[0].trigger_context[assignment_dispatch.ORDERING_KEY_CONTEXT] == "teams-chat:19:other@thread.v2"
    assert len(created) == 1


@pytest.mark.asyncio
async def test_the_merge_query_locks_the_newest_waiting_run_of_the_conversation(monkeypatch):
    """The SQL of the merge: same assignment and key, queued or delayed, newest, FOR UPDATE."""
    agent, assignment = _chat_assignment()
    waiting = ae.AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=assignment.id,
        agent_id=agent.id,
        status=ae.QUEUED_RUN_STATUS,
        trigger_payload=_message("m1", "a"),
    )
    session = _FakeSession(results=[waiting])

    merged = await ae._merge_into_waiting_turn(session, assignment, KEY, _message("m2", "b"))

    assert merged == str(waiting.id)
    assert waiting.trigger_payload["message_ids"] == ["m1", "m2"]
    sql = str(session.statements[0])
    assert "FOR UPDATE" in sql and "ORDER BY" in sql


@pytest.mark.asyncio
async def test_a_delayed_run_whose_window_is_closing_is_not_merged_into(monkeypatch):
    agent, assignment = _chat_assignment()
    waiting = ae.AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=assignment.id,
        agent_id=agent.id,
        status="delayed",
        trigger_payload=_message("m1", "a"),
    )
    closing = datetime.now(UTC) + timedelta(milliseconds=200)
    session = _FakeSession(results=[waiting, closing])

    assert await ae._merge_into_waiting_turn(session, assignment, KEY, _message("m2", "b")) is None
    assert "messages" not in waiting.trigger_payload


@pytest.mark.asyncio
async def test_a_message_already_in_the_turn_is_answered_by_that_run(monkeypatch):
    agent, assignment = _chat_assignment()
    waiting = ae.AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=assignment.id,
        agent_id=agent.id,
        status="delayed",
        trigger_payload=_message("m1", "a"),
    )
    session = _FakeSession(results=[waiting])

    assert await ae._merge_into_waiting_turn(session, assignment, KEY, _message("m1", "a")) == str(waiting.id)
    assert "messages" not in waiting.trigger_payload
