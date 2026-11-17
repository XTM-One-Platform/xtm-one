"""Single-flight chat turn guard.

A message sent to a conversation while a previous turn is still running
server-side (page reload mid-turn, second tab, another replica, REST
client) must NEVER spawn a second concurrent agent loop.  Production hit
exactly that: two interleaved slow-turn heartbeats on one conversation,
double token burn, interleaved history writes.

Pins:

* ``chat_turn_guard`` lock semantics — acquire / conflict / token-guarded
  release / refresh that re-creates but never clobbers a different owner;
* the fold handshake — an entry pushed while the marker is visible is
  guaranteed consumed; an entry pushed after the turn released is taken
  back (``LREM``) so the caller runs its own turn; a consumed entry is
  never double-run;
* the send connection folds a new message into the in-flight turn
  (``folded_into_active_turn`` frame, no second loop) when another turn
  holds the lock, relays that turn to its end, and releases the lock on
  normal turn completion;
* the cross-connection bridge feeds folded texts into the running loop's
  steering queue;
* ``drain_steering_queue`` / ``steering_text`` handle rich
  ``SteeringMessage`` items (attachment steering);
* a partner's answer whose work the user stopped is refused at
  the fold, in one script with the Stop record, and kept out by every
  drain that would deliver it: the bridge and the leftover pass;
* acquisition purges stale inbox entries — an entry a previous
  turn never consumed is already a persisted user message in the history
  the new turn loads, so re-injecting it as steering would deliver it
  twice, ordered after the new user message.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import chat as chat_module
from app.core import config, rate_limit
from app.services import chat_turn_guard, chat_turn_stream
from app.tools.agent_loop import (
    SteeringMessage,
    drain_steering_queue,
    steering_text,
)
from tests.test_chat_web_steering import (
    _FakeConnection,
    _FakeRedisKV,
    _FakeRedisRL,
    _FakeSession,
    _run_connection,
    _send,
    _wait_for_frame,
)

CONV = "11111111-2222-3333-4444-555555555555"


# ── Guard unit tests ──────────────────────────────────────────────────────


@pytest.fixture
def fake_redis(monkeypatch):
    import app.core.redis as redis_module

    fake = _FakeRedisKV()

    async def _client():
        return fake

    monkeypatch.setattr(redis_module, "redis_client", _client)
    return fake


async def test_acquire_is_single_flight(fake_redis):
    token = await chat_turn_guard.acquire_turn(CONV)
    assert token is not None
    assert await chat_turn_guard.acquire_turn(CONV) is None
    await chat_turn_guard.release_turn(CONV, token)
    assert await chat_turn_guard.acquire_turn(CONV) is not None


async def test_release_is_token_guarded(fake_redis):
    token = await chat_turn_guard.acquire_turn(CONV)
    # A stale token (previous turn) must not release the current holder.
    await chat_turn_guard.release_turn(CONV, "not-the-token")
    assert await chat_turn_guard.is_turn_active(CONV)
    await chat_turn_guard.release_turn(CONV, token)
    assert not await chat_turn_guard.is_turn_active(CONV)


async def test_refresh_recreates_but_never_clobbers(fake_redis):
    token = await chat_turn_guard.acquire_turn(CONV)
    # Simulate TTL expiry (key vanished) — refresh re-creates it.
    fake_redis.kv.clear()
    await chat_turn_guard.refresh_turn(CONV, token)
    assert await chat_turn_guard.is_turn_active(CONV)
    # Another turn legitimately took over — our refresh must not clobber.
    fake_redis.kv.clear()
    other = await chat_turn_guard.acquire_turn(CONV)
    await chat_turn_guard.refresh_turn(CONV, token)
    assert fake_redis.kv[chat_turn_guard.processing_key(CONV)] == other


async def test_acquire_or_fold_acquires_free_lock(fake_redis):
    token, folded = await chat_turn_guard.acquire_or_fold(CONV, "hello")
    assert token is not None
    assert folded is False
    assert await chat_turn_guard.drain_steering_inbox(CONV) == []
    assert await chat_turn_guard.is_turn_active(CONV)


async def test_acquire_or_fold_folds_into_running_turn(fake_redis):
    assert await chat_turn_guard.acquire_turn(CONV) is not None
    token, folded = await chat_turn_guard.acquire_or_fold(
        CONV, "steer", file_ids=["f-1"]
    )
    assert token is None
    assert folded is True
    payloads = await chat_turn_guard.drain_steering_inbox(CONV)
    assert [(p.text, p.file_ids) for p in payloads] == [("steer", ["f-1"])]


async def test_a_partner_answer_keeps_its_mark_through_the_inbox(fake_redis):
    """A wake turn the user stopped holds back a partner's answer and
    still answers everything else folded into it, so a fold says which it is
    - through a fold, a requeue, and an entry that never carried the mark."""
    import json

    assert await chat_turn_guard.acquire_turn(CONV) is not None
    await chat_turn_guard.acquire_or_fold(CONV, "partner answer", partner_wake=True)
    await chat_turn_guard.acquire_or_fold(CONV, "user message")
    payloads = await chat_turn_guard.drain_steering_inbox(CONV)
    assert [(p.text, p.partner_wake) for p in payloads] == [
        ("partner answer", True),
        ("user message", False),
    ]

    await chat_turn_guard.requeue_stranded(CONV, payloads)
    requeued = await chat_turn_guard.drain_steering_inbox(CONV)
    assert [p.partner_wake for p in requeued] == [True, False]

    # Written only when set: an entry a replica wrote before the mark existed
    # reads as a message, which is answered as it always was.
    assert "partner_wake" not in json.loads(chat_turn_guard._encode_entry("t"))
    old_entry = json.dumps({"id": "x", "text": "old", "file_ids": []})
    assert chat_turn_guard._decode_entries([old_entry])[0].partner_wake is False


async def test_a_partner_answer_carries_when_its_work_was_sent(fake_redis):
    """The drains compare a Stop with the moment the work a partner's
    answer delivers was sent, so the fold carries it - through a fold and a
    requeue, in the Stop record's unit, and only when set."""
    import json
    from datetime import UTC, datetime

    sent = datetime(2026, 9, 28, 8, 30, 12, 345678, tzinfo=UTC)
    assert await chat_turn_guard.acquire_turn(CONV) is not None
    await chat_turn_guard.acquire_or_fold(
        CONV, "partner answer", partner_wake=True, delegated_at=sent
    )
    await chat_turn_guard.acquire_or_fold(CONV, "user message")
    payloads = await chat_turn_guard.drain_steering_inbox(CONV)
    assert [(p.text, p.delegated_at) for p in payloads] == [
        ("partner answer", sent),
        ("user message", None),
    ]

    await chat_turn_guard.requeue_stranded(CONV, payloads)
    requeued = await chat_turn_guard.drain_steering_inbox(CONV)
    assert [p.delegated_at for p in requeued] == [sent, None]

    entry = json.loads(
        chat_turn_guard._encode_entry("t", partner_wake=True, delegated_at=sent)
    )
    assert entry["delegated_at"] == int(sent.timestamp() * 1_000_000)
    # An entry a replica wrote before the field existed has no submission time
    # and reads as before; so does a value nothing here writes.
    assert "delegated_at" not in json.loads(
        chat_turn_guard._encode_entry("t", partner_wake=True)
    )
    for junk in (None, "soon", True, 1.5, {"at": 1}, 10**30):
        raw = json.dumps(
            {"id": "x", "text": "old", "partner_wake": True, "delegated_at": junk}
        )
        (decoded,) = chat_turn_guard._decode_entries([raw])
        assert (decoded.text, decoded.delegated_at) == ("old", None)


async def test_a_recorded_stop_refuses_the_fold_of_work_sent_before_it(fake_redis):
    """The check and the push are one script, so no Stop recorded in
    between can let the answer of stopped work into the running turn. Work
    sent at the Stop's instant is stopped work, as for the cascade."""
    from datetime import UTC, datetime, timedelta

    stopped = datetime.now(UTC)
    assert await chat_turn_guard.acquire_turn(CONV) is not None
    await chat_turn_guard.record_stop(CONV, stopped)

    for sent in (stopped, stopped - timedelta(seconds=1)):
        with pytest.raises(chat_turn_guard.FoldRefusedError):
            await chat_turn_guard.fold_into_inflight_turn(
                CONV, "stopped work", partner_wake=True, delegated_at=sent
            )
    assert await chat_turn_guard.drain_steering_inbox(CONV) == []

    assert await chat_turn_guard.fold_into_inflight_turn(
        CONV,
        "work sent after the stop",
        partner_wake=True,
        delegated_at=stopped + timedelta(microseconds=1),
    )
    # Neither a message the user sent nor an answer with no submission time is
    # arbitrated: the Stop never silences them.
    assert await chat_turn_guard.fold_into_inflight_turn(CONV, "user message")
    assert await chat_turn_guard.fold_into_inflight_turn(
        CONV, "older replica's answer", partner_wake=True
    )
    assert [p.text for p in await chat_turn_guard.drain_steering_inbox(CONV)] == [
        "work sent after the stop",
        "user message",
        "older replica's answer",
    ]


async def test_a_refused_fold_leaves_the_running_turn_its_lock(fake_redis):
    """The refusal leaves ``acquire_or_fold`` before any lock is taken or any
    entry pushed: the turn the user stopped keeps its own marker to release."""
    from datetime import UTC, datetime, timedelta

    holder = await chat_turn_guard.acquire_turn(CONV)
    assert holder is not None
    stopped = datetime.now(UTC)
    await chat_turn_guard.record_stop(CONV, stopped)

    with pytest.raises(chat_turn_guard.FoldRefusedError):
        await chat_turn_guard.acquire_or_fold(
            CONV,
            "partner answer",
            partner_wake=True,
            delegated_at=stopped - timedelta(minutes=1),
        )
    assert fake_redis.kv[chat_turn_guard.processing_key(CONV)] == holder
    assert await chat_turn_guard.drain_steering_inbox(CONV) == []


async def test_a_withdrawn_stop_refuses_nothing_and_leaves_a_later_one(fake_redis):
    """A Stop that reached no turn withdraws its record; a later Stop recorded
    meanwhile is not that Stop's to withdraw."""
    from datetime import UTC, datetime, timedelta

    stopped = datetime.now(UTC)
    await chat_turn_guard.record_stop(CONV, stopped)
    await chat_turn_guard.forget_stop(CONV, stopped)
    assert await chat_turn_guard.recorded_stop(CONV) is None
    assert await chat_turn_guard.acquire_turn(CONV) is not None
    assert await chat_turn_guard.fold_into_inflight_turn(
        CONV, "partner answer", partner_wake=True, delegated_at=stopped - timedelta(seconds=1)
    )

    later = stopped + timedelta(seconds=1)
    await chat_turn_guard.record_stop(CONV, stopped)
    await chat_turn_guard.record_stop(CONV, later)
    await chat_turn_guard.forget_stop(CONV, stopped)
    assert await chat_turn_guard.recorded_stop(CONV) == later


async def test_acquire_or_fold_retries_acquire_after_handoff_race(
    fake_redis, monkeypatch
):
    """A failed fold means the in-flight turn finished mid-hand-off — the
    next round must retry the acquire and run its own turn (no requeue,
    nothing left in the inbox)."""
    real_acquire = chat_turn_guard.acquire_turn
    calls = {"n": 0}

    async def _fail_first_acquire(cid):
        calls["n"] += 1
        if calls["n"] == 1:
            # Lock appears held; by fold time the turn has exited, so the
            # fold recovers its entry and reports False.
            return None
        return await real_acquire(cid)

    monkeypatch.setattr(chat_turn_guard, "acquire_turn", _fail_first_acquire)
    token, folded = await chat_turn_guard.acquire_or_fold(CONV, "retry me")
    assert token is not None
    assert folded is False
    assert calls["n"] == 2
    assert await chat_turn_guard.drain_steering_inbox(CONV) == []


async def test_acquire_or_fold_requeues_after_pathological_alternation(
    fake_redis, monkeypatch
):
    """When every round loses both races (a turn starts AND finishes
    inside each hand-off window), the message is requeued for the next
    turn's bridge instead of spawning a concurrent loop - with the agents
    it mentions."""

    async def _never_acquire(_cid):
        return None

    async def _never_fold(
        _cid,
        _text,
        file_ids=None,
        context_file_ids=None,
        partner_wake=False,
        delegated_at=None,
        **_kw,
    ):
        return False

    monkeypatch.setattr(chat_turn_guard, "acquire_turn", _never_acquire)
    monkeypatch.setattr(
        chat_turn_guard, "fold_into_inflight_turn", _never_fold
    )
    token, folded = await chat_turn_guard.acquire_or_fold(
        CONV,
        "stranded",
        file_ids=["f-2"],
        context_file_ids=["c-2"],
        mentioned_agent_ids=["agent-b"],
    )
    assert token is None
    assert folded is False
    payloads = await chat_turn_guard.drain_steering_inbox(CONV)
    assert [
        (p.text, p.file_ids, p.context_file_ids, p.mentioned_agent_ids, p.mentions_only)
        for p in payloads
    ] == [("stranded", ["f-2"], ["c-2"], ["agent-b"], False)]


async def test_fold_carries_context_file_ids_apart_from_file_ids(fake_redis):
    """The two channels of a fold never merge: ``file_ids`` ride ON
    the steering message (inlined), ``context_file_ids`` are only registered
    in the running loop's FileContext. A wake folding a task's nine CSVs
    must not become 720k characters of steering text."""
    assert await chat_turn_guard.acquire_turn(CONV) is not None
    token, folded = await chat_turn_guard.acquire_or_fold(
        CONV, "specialist finished", context_file_ids=["c-1", "c-2"]
    )
    assert token is None
    assert folded is True
    payloads = await chat_turn_guard.drain_steering_inbox(CONV)
    assert [(p.text, p.file_ids, p.context_file_ids) for p in payloads] == [
        ("specialist finished", [], ["c-1", "c-2"])
    ]


def test_decode_tolerates_an_older_entry_and_a_malformed_channel():
    """An entry written before the channel existed decodes with an empty
    ``context_file_ids``; a non-list / non-string shape is dropped, never
    raised - the decoder's promise is that one bad entry cannot poison the
    drain."""
    import json

    old_entry = json.dumps({"id": "x", "text": "old", "file_ids": ["f-1"]})
    bad_entry = json.dumps(
        {"id": "y", "text": "bad", "file_ids": "f-1", "context_file_ids": {"c": 1}}
    )
    mixed_entry = json.dumps(
        {"id": "z", "text": "mixed", "context_file_ids": ["c-1", 7, None, "c-2"]}
    )
    payloads = chat_turn_guard._decode_entries([old_entry, bad_entry, mixed_entry])
    assert [(p.text, p.file_ids, p.context_file_ids) for p in payloads] == [
        ("old", ["f-1"], []),
        ("bad", [], []),
        ("mixed", [], ["c-1", "c-2"]),
    ]
    # The encoder omits the key when there is nothing to carry, so a replica
    # still on the old shape reads exactly what it always read.
    assert "context_file_ids" not in json.loads(chat_turn_guard._encode_entry("t"))
    assert json.loads(
        chat_turn_guard._encode_entry("t", None, ["c-1"])
    )["context_file_ids"] == ["c-1"]


async def test_acquisition_purges_stale_inbox_entry_from_crashed_turn(
    fake_redis,
):
    """The bug: a fold consumed by NOBODY (its turn crashed with the
    marker still set) must be discarded when the next turn acquires the
    lock — the text is already a persisted user message in the history
    that turn loads, and re-injecting it as steering delivered it twice,
    ordered after the new user message."""
    crashed_token = await chat_turn_guard.acquire_turn(CONV)
    assert crashed_token is not None
    # A fold lands while the (about-to-crash) turn appears to run.
    assert await chat_turn_guard.fold_into_inflight_turn(CONV, "old message")
    # The process dies; the marker TTL (300s) lapses but the inbox entry
    # (TTL 600s) survives — kv holds the marker, lists holds the inbox.
    fake_redis.kv.clear()

    token, folded = await chat_turn_guard.acquire_or_fold(CONV, "new message")
    assert token is not None
    assert folded is False
    # The stale entry was purged at acquisition: the bridge has nothing
    # to deliver to the new turn.
    assert await chat_turn_guard.drain_steering_inbox(CONV) == []


async def test_acquisition_purges_requeued_entries(fake_redis):
    """An entry pushed by ``requeue_stranded`` with no turn running is
    discarded by the next acquisition — it is persisted, so it
    shapes that turn through history, not through a duplicate steering
    injection."""
    await chat_turn_guard.requeue_stranded(
        CONV,
        [chat_turn_guard.SteeringPayload(text="stranded", file_ids=["f-1"])],
    )
    token, folded = await chat_turn_guard.acquire_or_fold(CONV, "next turn")
    assert token is not None
    assert folded is False
    assert await chat_turn_guard.drain_steering_inbox(CONV) == []


async def test_fold_after_acquisition_survives_the_purge(fake_redis):
    """No regression: the purge runs ONCE at acquisition, so a fold
    landing while the acquired turn RUNS stays in the inbox for the
    turn's bridge to deliver mid-run."""
    token, folded = await chat_turn_guard.acquire_or_fold(CONV, "the turn")
    assert token is not None and folded is False
    # A message from another connection folds into the running turn.
    _, other_folded = await chat_turn_guard.acquire_or_fold(CONV, "steer it")
    assert other_folded is True
    payloads = await chat_turn_guard.drain_steering_inbox(CONV)
    assert [p.text for p in payloads] == ["steer it"]


async def test_purge_failure_never_strands_the_acquired_lock(
    fake_redis, monkeypatch
):
    """The purge is best-effort: the lock is already held when it runs,
    so an inbox read failure must not raise past ``acquire_or_fold`` —
    the caller would never receive the token and the lock would strand
    for a full TTL."""

    async def _boom(_cid):
        raise RuntimeError("redis blip")

    monkeypatch.setattr(chat_turn_guard, "drain_steering_inbox", _boom)
    token, folded = await chat_turn_guard.acquire_or_fold(CONV, "hello")
    assert token is not None
    assert folded is False
    assert await chat_turn_guard.is_turn_active(CONV)


async def test_cancellation_during_purge_releases_the_acquired_lock(
    fake_redis, monkeypatch
):
    """A cancellation delivered while the acquisition purge awaits Redis
    (client disconnect, worker shutdown) is a ``BaseException``: it
    escapes the purge's best-effort ``except Exception`` and the caller
    never receives the token.  ``acquire_or_fold`` must release the
    just-acquired lock before propagating it — otherwise the lock
    strands for a full TTL and every send in that window folds into a
    turn that no longer exists."""

    async def _cancelled(_cid):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        chat_turn_guard, "drain_steering_inbox", _cancelled
    )
    with pytest.raises(asyncio.CancelledError):
        await chat_turn_guard.acquire_or_fold(CONV, "hello")
    assert not await chat_turn_guard.is_turn_active(CONV)


async def test_fold_consumed_when_marker_present(fake_redis):
    await chat_turn_guard.acquire_turn(CONV)
    assert await chat_turn_guard.fold_into_inflight_turn(CONV, "fix the filter")
    payloads = await chat_turn_guard.drain_steering_inbox(CONV)
    assert [p.text for p in payloads] == ["fix the filter"]


async def test_a_fold_names_the_turn_that_accepted_it(fake_redis):
    """The fold's own marker check names the turn that takes the message:
    read again afterwards, the lock may already be released, or
    held by the next turn, while the accepting turn answers the message."""
    token = await chat_turn_guard.acquire_turn(CONV)
    accepted: list[str] = []
    token_out, folded = await chat_turn_guard.acquire_or_fold(CONV, "and also this", accepted_by=accepted)
    assert (token_out, folded) == (None, True)
    assert accepted == [token]
    # No turn to name: the fold takes its entry back and the caller runs the turn.
    assert await chat_turn_guard.release_turn(CONV, token)
    none_named: list[str] = []
    assert not await chat_turn_guard.fold_into_inflight_turn(CONV, "hello", accepted_by=none_named)
    assert none_named == []


async def test_fold_recovers_when_turn_ended(fake_redis):
    # No lock holder: the fold must take its entry back and report False
    # so the caller runs its own turn — nothing may linger in the inbox.
    assert not await chat_turn_guard.fold_into_inflight_turn(CONV, "hello")
    assert await chat_turn_guard.drain_steering_inbox(CONV) == []


async def test_fold_trusts_consumer_when_entry_was_drained(
    fake_redis, monkeypatch
):
    """Marker gone AND the entry already consumed (the exiting turn's
    release-drain won the race) → the fold reports True (the exiting turn
    answers it in a follow-up pass) and never double-runs. That turn is gone,
    so no turn is named for it."""
    real_get = fake_redis.get

    async def _drain_then_get(key):
        # Simulate the exiting turn draining the inbox in the window
        # between our push and our marker check.
        await chat_turn_guard.drain_steering_inbox(CONV)
        return await real_get(key)

    monkeypatch.setattr(fake_redis, "get", _drain_then_get)
    accepted: list[str] = []
    assert await chat_turn_guard.fold_into_inflight_turn(CONV, "late fold", accepted_by=accepted)
    assert accepted == []


async def test_release_and_drain_returns_stranded_payloads(fake_redis):
    token = await chat_turn_guard.acquire_turn(CONV)
    await chat_turn_guard.fold_into_inflight_turn(
        CONV, "with a screenshot", file_ids=["f-1", "f-2"]
    )
    stranded = await chat_turn_guard.release_turn_and_drain(CONV, token)
    assert len(stranded) == 1
    assert stranded[0].text == "with a screenshot"
    assert stranded[0].file_ids == ["f-1", "f-2"]
    assert stranded[0].context_file_ids == []
    assert not await chat_turn_guard.is_turn_active(CONV)


async def test_acquisition_hands_a_purged_wakes_files_to_the_new_turn(fake_redis):
    """A wake that lost every hand-off race was requeued; no turn was running,
    so the next acquisition purges the entry (its text is in history).
    Its ``context_file_ids`` are the one thing history does not carry: they
    are handed to the acquiring caller, which registers them in the turn's
    FileContext. A caller that passes no list still acquires."""
    await chat_turn_guard.requeue_stranded(
        CONV,
        [
            chat_turn_guard.SteeringPayload(
                text="wake one", context_file_ids=["c-1", "c-2"]
            ),
            chat_turn_guard.SteeringPayload(
                text="wake two", context_file_ids=["c-2", "c-3"], file_ids=["f-1"]
            ),
        ],
    )
    purged: list[str] = []
    token, folded = await chat_turn_guard.acquire_or_fold(
        CONV, "next turn", purged_context_file_ids=purged
    )
    assert token is not None and folded is False
    assert purged == ["c-1", "c-2", "c-3"]  # deduplicated, order kept
    assert await chat_turn_guard.drain_steering_inbox(CONV) == []
    await chat_turn_guard.release_turn(CONV, token)

    await chat_turn_guard.requeue_stranded(
        CONV, [chat_turn_guard.SteeringPayload(text="again", context_file_ids=["c-9"])]
    )
    token, _ = await chat_turn_guard.acquire_or_fold(CONV, "no list passed")
    assert token is not None
    assert await chat_turn_guard.drain_steering_inbox(CONV) == []


async def test_a_fold_carries_the_agents_its_message_mentions(fake_redis):
    """A message folded into a running turn is steering for the agent
    answering; the other agents it mentions ride the entry, for the
    turn that drains it to answer."""
    assert await chat_turn_guard.acquire_turn(CONV) is not None
    token, folded = await chat_turn_guard.acquire_or_fold(
        CONV, "@beta your view?", mentioned_agent_ids=["b-1", "c-1"]
    )
    assert token is None and folded is True
    [payload] = await chat_turn_guard.drain_steering_inbox(CONV)
    assert (payload.text, payload.mentioned_agent_ids, payload.mentions_only) == (
        "@beta your view?",
        ["b-1", "c-1"],
        False,
    )


def test_decode_reads_an_entry_either_side_of_a_rolling_deploy():
    """The field is written only when set, so a replica that predates it reads
    the shape it knows; an entry without it decodes with no mentions, and a
    malformed one costs the mentions, never the drain."""
    import json

    old_entry = json.dumps({"id": "x", "text": "old", "file_ids": []})
    bad_entry = json.dumps(
        {"id": "y", "text": "bad", "mentioned_agent_ids": "b-1", "mentions_only": True}
    )
    new_entry = chat_turn_guard._encode_entry(
        "new", mentioned_agent_ids=["b-1"], mentions_only=True
    )
    payloads = chat_turn_guard._decode_entries([old_entry, bad_entry, new_entry])
    assert [(p.text, p.mentioned_agent_ids, p.mentions_only) for p in payloads] == [
        ("old", [], False),
        ("bad", [], False),
        ("new", ["b-1"], True),
    ]
    plain = json.loads(chat_turn_guard._encode_entry("t", mentions_only=True))
    assert "mentioned_agent_ids" not in plain and "mentions_only" not in plain


def test_a_replica_that_predates_mentions_skips_a_hand_back_instead_of_answering_it_again():
    """An older replica reads ``text`` and skips an entry without it. A
    mentions-only hand-back, whose text is in the thread already, carries
    none, so a replica of the previous build draining it mid-deploy drops it
    instead of injecting or following up the message again; a fold it can
    steer keeps its text, as before."""
    import json

    hand_back = chat_turn_guard._encode_entry("owed", mentioned_agent_ids=["b-1"], mentions_only=True)
    fold = chat_turn_guard._encode_entry("steer", mentioned_agent_ids=["b-1"])
    assert "text" not in json.loads(hand_back)
    assert json.loads(fold)["text"] == "steer"
    # The previous build's reading: the same entry without the hand-back flag.
    unflagged = {k: v for k, v in json.loads(hand_back).items() if k != "mentions_only"}
    assert chat_turn_guard._decode_entries([json.dumps(unflagged)]) == []
    # This build reads both back.
    decoded = chat_turn_guard._decode_entries([hand_back, fold])
    assert [(p.text, p.mentions_only) for p in decoded] == [("owed", True), ("steer", False)]


async def test_acquisition_hands_the_agents_a_purged_fold_mentions_to_the_new_turn(
    fake_redis,
):
    """A fold nobody consumed is purged at the next acquisition: its
    text is in the history that turn loads, its mentions are not - they are
    handed to the acquiring caller, which answers them."""
    await chat_turn_guard.requeue_stranded(
        CONV,
        [
            chat_turn_guard.SteeringPayload(text="no mention"),
            chat_turn_guard.SteeringPayload(
                text="@beta your view?", mentioned_agent_ids=["b-1"]
            ),
        ],
    )
    purged: list = []
    token, folded = await chat_turn_guard.acquire_or_fold(
        CONV, "next turn", purged_mentions=purged
    )
    assert token is not None and folded is False
    assert [(p.text, p.mentioned_agent_ids) for p in purged] == [
        ("@beta your view?", ["b-1"])
    ]
    assert await chat_turn_guard.drain_steering_inbox(CONV) == []


async def test_owed_mentions_are_handed_back_as_mentions_only(fake_redis):
    """A turn that ends before asking a fold's agents hands the fold back
    ``mentions_only``: the next consumer answers the agents and never injects
    the text a second time. A fold naming none is left out."""
    handed = await chat_turn_guard.requeue_owed_mentions(
        CONV,
        [
            chat_turn_guard.SteeringPayload(text="plain steering"),
            chat_turn_guard.SteeringPayload(
                text="@beta your view?", file_ids=["f-1"], mentioned_agent_ids=["b-1"]
            ),
        ],
    )
    assert handed == 1
    [payload] = await chat_turn_guard.drain_steering_inbox(CONV)
    assert (payload.text, payload.file_ids, payload.mentioned_agent_ids, payload.mentions_only) == (
        "@beta your view?",
        ["f-1"],
        ["b-1"],
        True,
    )
    assert await chat_turn_guard.requeue_owed_mentions(CONV, []) == 0


async def test_requeue_stranded_round_trips_context_file_ids(fake_redis):
    """A stranded wake keeps its task's files through the requeue:
    the follow-up turn that picks it up registers them, or it would read a
    text saying the files are attached while holding none."""
    await chat_turn_guard.requeue_stranded(
        CONV,
        [
            chat_turn_guard.SteeringPayload(
                text="stranded wake", context_file_ids=["c-1"]
            )
        ],
    )
    payloads = await chat_turn_guard.drain_steering_inbox(CONV)
    assert [(p.text, p.file_ids, p.context_file_ids) for p in payloads] == [
        ("stranded wake", [], ["c-1"])
    ]


async def test_release_and_drain_gated_on_ownership(fake_redis):
    """A stale token (TTL lapsed, another turn acquired) must NOT drain
    the inbox — the pending folds belong to the NEW holder's loop."""
    stale_token = await chat_turn_guard.acquire_turn(CONV)
    # TTL lapse + takeover by another turn.
    fake_redis.kv.clear()
    new_token = await chat_turn_guard.acquire_turn(CONV)
    assert new_token is not None
    # A fold destined for the new holder's running loop.
    assert await chat_turn_guard.fold_into_inflight_turn(CONV, "for the new turn")
    # The stale turn exits: no drain, no lock release.
    stranded = await chat_turn_guard.release_turn_and_drain(CONV, stale_token)
    assert stranded == []
    assert await chat_turn_guard.is_turn_active(CONV)
    payloads = await chat_turn_guard.drain_steering_inbox(CONV)
    assert [p.text for p in payloads] == ["for the new turn"]


async def test_release_turn_completes_when_caller_cancelled(
    fake_redis, monkeypatch
):
    """Release runs from teardown paths that may themselves be cancelled
    (client disconnect during the ``finally``).  An interrupted release
    would strand the lock for a full TTL — every send in that window
    would fold into a turn that no longer exists.  The release must run
    to completion even when the awaiting task is cancelled mid-await."""
    token = await chat_turn_guard.acquire_turn(CONV)
    started = asyncio.Event()
    gate = asyncio.Event()
    real_eval = fake_redis.eval

    async def _slow_eval(script, numkeys, *args):
        started.set()
        await gate.wait()
        return await real_eval(script, numkeys, *args)

    monkeypatch.setattr(fake_redis, "eval", _slow_eval)

    task = asyncio.create_task(chat_turn_guard.release_turn(CONV, token))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The shielded release still completed: the lock is gone.
    assert not await chat_turn_guard.is_turn_active(CONV)


async def test_release_and_drain_completes_when_caller_cancelled(
    fake_redis, monkeypatch
):
    """Same cancellation contract for the turn-exit handshake: the
    release AND the racing-fold drain both complete even when the
    teardown task is cancelled, so no fold is left with a freed lock."""
    token = await chat_turn_guard.acquire_turn(CONV)
    await chat_turn_guard.fold_into_inflight_turn(CONV, "racing fold")
    started = asyncio.Event()
    gate = asyncio.Event()
    real_eval = fake_redis.eval

    async def _slow_eval(script, numkeys, *args):
        started.set()
        await gate.wait()
        return await real_eval(script, numkeys, *args)

    monkeypatch.setattr(fake_redis, "eval", _slow_eval)

    task = asyncio.create_task(
        chat_turn_guard.release_turn_and_drain(CONV, token)
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await chat_turn_guard.is_turn_active(CONV)
    # The drain ran too: nothing lingers in the inbox.
    assert await chat_turn_guard.drain_steering_inbox(CONV) == []


async def test_requeue_stranded_round_trips_file_ids(fake_redis):
    await chat_turn_guard.requeue_stranded(
        CONV,
        [chat_turn_guard.SteeringPayload(text="again", file_ids=["f-9"])],
    )
    payloads = await chat_turn_guard.drain_steering_inbox(CONV)
    assert [(p.text, p.file_ids) for p in payloads] == [("again", ["f-9"])]


# ── Steering queue rich items ─────────────────────────────────────────────


def test_drain_steering_queue_handles_rich_items():
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait("plain text")
    q.put_nowait(SteeringMessage(text="with file", files=["ref"]))
    q.put_nowait(SteeringMessage(text="   ", files=None))  # blank → dropped
    items = drain_steering_queue(q)
    assert [steering_text(i) for i in items] == ["plain text", "with file"]
    assert isinstance(items[1], SteeringMessage)
    assert items[1].files == ["ref"]


# ── Send connection integration ───────────────────────────────────────────


@pytest.fixture
def fake_rl(monkeypatch):
    fake = _FakeRedisRL()

    async def _client():
        return fake

    monkeypatch.setattr(rate_limit, "redis_client", _client)
    monkeypatch.setattr(config.settings, "rate_limit_fail_mode", "open")
    return fake


@pytest.fixture
def ws_env(monkeypatch, fake_rl):
    """Same environment shape as ``test_chat_web_steering.ws_env``."""
    import app.api.v1.platform as platform_module
    import app.core.database as database_module
    import app.core.redis as redis_module
    import app.llm.router as llm_router_module

    user_id = uuid.uuid4()
    conv = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        agent_id=None,
        title="single-flight test",
        transfer_source_agent_id=None,
        tool_overrides_json=None,
        # Presentation Studio binding: the turn resolves it every
        # time; ``None`` is an ordinary, unbound chat.
        presentation_id=None,
        video_project_id=None,
        custom_dashboard_id=None,
        fleet_id=None,
    )
    timeline: list[tuple] = []
    stored: list[tuple[str, str]] = []
    fake_kv = _FakeRedisKV()
    env = SimpleNamespace(
        user_id=user_id,
        conv=conv,
        timeline=timeline,
        stored=stored,
        fake_kv=fake_kv,
    )

    async def _chat_enabled(_db):
        return True

    monkeypatch.setattr(platform_module, "is_chat_enabled", _chat_enabled)

    # The account-state gate a turn runs again is covered by
    # tests/test_deactivated_account_auth.py.
    async def _account_ok(_db, _uid, _issued_at, **_kw):
        return None

    monkeypatch.setattr(chat_module, "resolve_account_rejection", _account_ok)

    async def _session_holds(session):
        return session

    monkeypatch.setattr(chat_module, "recheck_session", _session_holds)
    monkeypatch.setattr(
        database_module, "async_session", lambda: _FakeSession(conv, timeline)
    )

    async def _redis_client():
        return fake_kv

    monkeypatch.setattr(redis_module, "redis_client", _redis_client)
    monkeypatch.setattr(chat_module, "_spawn_cancel_cascade", lambda _cid, *_a: None)
    # Fast bridge polling so remote-fold tests don't sleep for seconds.
    monkeypatch.setattr(chat_turn_guard, "BRIDGE_POLL_SECONDS", 0.02)

    async def _store_message(_db, _conv_id, role, content, **_kw):
        timeline.append(("persist", role, content))
        stored.append((role, content))
        return SimpleNamespace(id=uuid.uuid4(), metadata_json=None)

    monkeypatch.setattr(chat_module, "store_message", _store_message)

    async def _history(_db, _conv_id):
        return []

    monkeypatch.setattr(chat_module, "get_conversation_history", _history)

    async def _langfuse_ids(_conv_id):
        return {}

    monkeypatch.setattr(chat_module, "load_latest_langfuse_ids", _langfuse_ids)

    async def _no_llm(_db, _uid):
        raise RuntimeError("no LLM in tests")

    monkeypatch.setattr(llm_router_module, "get_llm_for_user", _no_llm)
    return env


def _patch_general_chat(monkeypatch, stub):
    import app.services.general_chat as general_chat_module

    monkeypatch.setattr(general_chat_module, "run_general_chat", stub)


def _relay_the_other_turn(monkeypatch, *, ended: bool = False) -> asyncio.Event:
    """Stand in for the conversation's relay: once the returned event is set,
    a folded send is relayed the ``done`` of the turn it folded into."""
    other_turn_ended = asyncio.Event()
    if ended:
        other_turn_ended.set()

    async def _relay(_conv_id, *, origin, send, **_kw):
        await other_turn_ended.wait()
        if await send({"type": "done", "content": "the other turn's answer", "relayed": True}):
            await asyncio.Event().wait()

    monkeypatch.setattr(chat_turn_stream, "relay_turn_frames", _relay)
    return other_turn_ended


async def test_send_during_inflight_turn_folds_instead_of_second_loop(
    monkeypatch, ws_env
):
    """The production bug: a message arriving while ANOTHER turn
    holds the conversation's lock (reload mid-turn, second tab, other
    replica) must be folded into the running turn — no second agent loop,
    a ``folded_into_active_turn`` frame, and the text lands in the
    steering inbox for the running turn to drain."""
    conv_id = str(ws_env.conv.id)
    # Another turn (other connection / process) holds the lock.
    other_token = await chat_turn_guard.acquire_turn(conv_id)
    assert other_token is not None

    run_calls: list[str] = []

    async def _run(_db, _uid, _history, content, **_kw):
        run_calls.append(content)
        return ("should never run", None)

    _patch_general_chat(monkeypatch, _run)
    other_turn_ended = _relay_the_other_turn(monkeypatch)
    conn = _FakeConnection(conv_id, {"content": "do the changes NOW"}, ws_env.timeline)
    task = asyncio.create_task(_run_connection(conn, ws_env.user_id))
    assert await _wait_for_frame(conn, "folded_into_active_turn") is not None

    assert run_calls == [], "a second concurrent agent loop must never start"
    # The message is persisted (part of the thread) AND queued for the
    # running turn.
    assert ("user", "do the changes NOW") in ws_env.stored
    payloads = await chat_turn_guard.drain_steering_inbox(conv_id)
    assert [p.text for p in payloads] == ["do the changes NOW"]
    # The other turn still owns the lock, and the folded send follows it.
    assert await chat_turn_guard.is_turn_active(conv_id)
    assert not task.done()

    await chat_turn_guard.release_turn(conv_id, other_token)
    other_turn_ended.set()
    await asyncio.wait_for(task, 5)
    assert conn.sent[-1] == {
        "type": "done",
        "content": "the other turn's answer",
        "relayed": True,
        "turn_id": chat_turn_stream.public_turn_id(other_token),
    }


async def test_turn_acquires_and_releases_the_lock(monkeypatch, ws_env):
    """A normal turn takes the lock while running and releases it on
    completion, so the next send starts a regular turn."""
    conv_id = str(ws_env.conv.id)
    held_during_run: list[bool] = []

    async def _run(_db, _uid, _history, _content, **_kw):
        held_during_run.append(await chat_turn_guard.is_turn_active(conv_id))
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn = await _send(ws_env, {"content": "build the report"})

    assert held_during_run == [True]
    assert not await chat_turn_guard.is_turn_active(conv_id)
    assert any(f.get("type") == "done" for f in conn.sent)


def _own_frames(conn) -> list[dict]:
    """What the connection's own send produced: no relayed frame."""
    return [f for f in conn.sent if not f.get("relayed")]


async def test_a_send_that_takes_the_lock_says_so_before_its_turn(monkeypatch, ws_env):
    """#4403: the plain ``thinking`` ack leaves before ``acquire_or_fold``
    decides, so a client still following a relayed turn whose terminal frame
    it missed cannot tell a new turn from a fold by it. The send that took the
    lock says so right after the ack, before any frame of its turn."""

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": "tool_start", "tools": ["search"]})
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn = await _send(ws_env, {"content": "build the report"})

    kinds = [f["type"] for f in _own_frames(conn)]
    assert kinds[:2] == ["thinking", "turn_started"]
    assert kinds.count("turn_started") == 1
    assert kinds[-1] == "done"


async def test_a_folded_send_is_never_announced_as_a_turn_of_its_own(monkeypatch, ws_env):
    """The #4403 fold cases keep their state: a send folded into the turn
    another connection runs (a reloaded page, a second tab) gets its ack and
    ``folded_into_active_turn``, never ``turn_started``."""
    conv_id = str(ws_env.conv.id)
    assert await chat_turn_guard.acquire_turn(conv_id) is not None

    async def _run(_db, _uid, _history, _content, **_kw):  # pragma: no cover
        raise AssertionError("a folded send never runs a loop")

    _patch_general_chat(monkeypatch, _run)
    _relay_the_other_turn(monkeypatch, ended=True)
    conn = await asyncio.wait_for(_send(ws_env, {"content": "do the changes NOW"}), 5)

    assert [f["type"] for f in _own_frames(conn)] == ["thinking", "folded_into_active_turn"]


async def test_remote_fold_reaches_running_loop_via_bridge(
    monkeypatch, ws_env
):
    """A fold pushed into the Redis inbox while the turn runs (another
    connection or the steer route) is bridged into the in-process steering
    queue."""
    conv_id = str(ws_env.conv.id)
    received: list[str] = []

    async def _run(_db, _uid, _history, _content, **kwargs):
        # Simulate a fold from another connection while this loop runs.
        assert await chat_turn_guard.fold_into_inflight_turn(
            conv_id, "steer from another tab"
        )
        queue = kwargs["steering_queue"]
        item = await asyncio.wait_for(queue.get(), timeout=2)
        received.append(steering_text(item))
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    await _send(ws_env, {"content": "build the report"})

    assert received == ["steer from another tab"]


async def test_stale_inbox_entry_never_reaches_the_next_turn(
    monkeypatch, ws_env
):
    """The production incident: a previous turn crashed after a fold
    was persisted + pushed; its marker (TTL 300s) expired but the inbox
    entry (TTL 600s) survived.  The next turn on the conversation must
    NOT have that entry bridged into its steering queue at iteration 1 —
    the text is already in the history it loads."""
    conv_id = str(ws_env.conv.id)
    # The unconsumed leftovers of the crashed turn: inbox entry present,
    # no marker.
    await chat_turn_guard.requeue_stranded(
        conv_id, [chat_turn_guard.SteeringPayload(text="old message")]
    )

    leaked: list[str] = []

    async def _run(_db, _uid, _history, content, **kwargs):
        queue = kwargs["steering_queue"]
        # Give the bridge several poll ticks (0.02s each in ws_env) to
        # (wrongly) deliver the stale entry before we inspect the queue.
        await asyncio.sleep(0.1)
        while not queue.empty():
            leaked.append(steering_text(queue.get_nowait()))
        return ("fresh answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn = await _send(ws_env, {"content": "new question"})

    assert leaked == [], "a stale inbox entry must never be re-injected"
    assert await chat_turn_guard.drain_steering_inbox(conv_id) == []
    done_frames = [f for f in conn.sent if f.get("type") == "done"]
    assert [f["content"] for f in done_frames] == ["fresh answer"]
    assert not await chat_turn_guard.is_turn_active(conv_id)


async def test_remote_fold_during_final_stream_triggers_followup_pass(
    monkeypatch, ws_env
):
    """A fold that lands too late for mid-run injection (during the final
    answer stream) is drained by the leftover check and answered in an
    immediate follow-up pass — never silently dropped."""
    conv_id = str(ws_env.conv.id)
    run_calls: list[str] = []

    async def _run(_db, _uid, _history, content, **_kw):
        run_calls.append(content)
        if len(run_calls) == 1:
            # Stop the bridge from picking the fold up mid-run: it lands
            # right as the loop returns.
            import app.api.v1.chat as _chat

            await chat_turn_guard.fold_into_inflight_turn(
                conv_id, "late correction"
            )
            assert _chat is chat_module
            return ("segment one", None)
        return ("follow-up answer", None)

    _patch_general_chat(monkeypatch, _run)
    # Near-zero bridge polling would consume the fold mid-run; slow it
    # down so the leftover drain path is what picks it up.
    monkeypatch.setattr(chat_turn_guard, "BRIDGE_POLL_SECONDS", 30.0)
    conn = await _send(ws_env, {"content": "build the report"})

    assert run_calls == ["build the report", "late correction"]
    done_frames = [f for f in conn.sent if f.get("type") == "done"]
    assert [f["content"] for f in done_frames] == [
        "segment one",
        "follow-up answer",
    ]
    assert not await chat_turn_guard.is_turn_active(conv_id)


def _stopped_work_sent_at():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - timedelta(minutes=1)


async def test_a_stopped_partner_answer_left_in_the_inbox_gets_no_follow_up(
    monkeypatch, ws_env
):
    """#4577: a partner's answer folded before a Stop sent from another replica,
    which this turn reads only at its bridge's next poll. The leftover drain
    finds it in the inbox and starts no follow-up pass for it."""
    from datetime import UTC, datetime

    conv_id = str(ws_env.conv.id)
    run_calls: list[str] = []

    async def _run(_db, _uid, _history, content, **_kw):
        run_calls.append(content)
        if len(run_calls) == 1:
            assert await chat_turn_guard.fold_into_inflight_turn(
                conv_id,
                "[Platform] partner answer",
                partner_wake=True,
                delegated_at=_stopped_work_sent_at(),
            )
            await chat_turn_guard.record_stop(conv_id, datetime.now(UTC))
            return ("segment one", None)
        return ("follow-up answer", None)

    _patch_general_chat(monkeypatch, _run)
    monkeypatch.setattr(chat_turn_guard, "BRIDGE_POLL_SECONDS", 30.0)
    conn = await _send(ws_env, {"content": "build the report"})

    assert run_calls == ["build the report"], "the stopped work was answered"
    done_frames = [f for f in conn.sent if f.get("type") == "done"]
    assert [f["content"] for f in done_frames] == ["segment one"]
    assert not await chat_turn_guard.is_turn_active(conv_id)


async def test_a_message_folded_with_a_stopped_partner_answer_is_still_answered(
    monkeypatch, ws_env
):
    """The Stop holds back the partner's answer and nothing else: the user's own
    message left in the same inbox gets its follow-up pass, whose history does
    not carry the answer of the work the user stopped."""
    from datetime import UTC, datetime

    conv_id = str(ws_env.conv.id)
    run_calls: list[tuple[str, list[str]]] = []

    async def _run(_db, _uid, history, content, **_kw):
        run_calls.append((content, [m.content for m in history]))
        if len(run_calls) == 1:
            assert await chat_turn_guard.fold_into_inflight_turn(
                conv_id,
                "[Platform] partner answer",
                partner_wake=True,
                delegated_at=_stopped_work_sent_at(),
            )
            await chat_turn_guard.record_stop(conv_id, datetime.now(UTC))
            await chat_turn_guard.fold_into_inflight_turn(conv_id, "late correction")
            return ("segment one", None)
        return ("follow-up answer", None)

    _patch_general_chat(monkeypatch, _run)
    monkeypatch.setattr(chat_turn_guard, "BRIDGE_POLL_SECONDS", 30.0)
    await _send(ws_env, {"content": "build the report"})

    assert [content for content, _ in run_calls] == [
        "build the report",
        "late correction",
    ]
    assert "[Platform] partner answer" not in run_calls[1][1]


async def test_a_partner_answer_queued_before_a_stop_gets_no_follow_up(
    monkeypatch, ws_env
):
    """#4577: the bridge had already queued the partner's answer when a Stop sent
    from another replica was recorded, and the loop returned without injecting
    it. The leftover pass re-checks it rather than answering it before the
    bridge's next poll cancels the turn."""
    from datetime import UTC, datetime

    conv_id = str(ws_env.conv.id)
    run_calls: list[str] = []

    async def _run(_db, _uid, _history, content, **kwargs):
        run_calls.append(content)
        if len(run_calls) == 1:
            assert await chat_turn_guard.fold_into_inflight_turn(
                conv_id,
                "[Platform] partner answer",
                partner_wake=True,
                delegated_at=_stopped_work_sent_at(),
            )
            queue = kwargs["steering_queue"]
            # Bridged into the loop's queue and put back: the loop was
            # streaming its final answer and never injected it.
            queue.put_nowait(await asyncio.wait_for(queue.get(), timeout=2))
            await chat_turn_guard.record_stop(conv_id, datetime.now(UTC))
            return ("segment one", None)
        return ("follow-up answer", None)

    _patch_general_chat(monkeypatch, _run)
    await _send(ws_env, {"content": "build the report"})

    assert run_calls == ["build the report"], "the stopped work was answered"
    assert not await chat_turn_guard.is_turn_active(conv_id)


async def test_the_bridge_does_not_inject_a_stopped_partner_answer(
    monkeypatch, ws_env
):
    """Folded before the Stop and drained after it, the partner's answer
    is not injected into the running loop; the user's message folded behind it
    in the same inbox is."""
    from datetime import UTC, datetime

    conv_id = str(ws_env.conv.id)
    received: list[str] = []

    async def _run(_db, _uid, _history, _content, **kwargs):
        assert await chat_turn_guard.fold_into_inflight_turn(
            conv_id,
            "[Platform] partner answer",
            partner_wake=True,
            delegated_at=_stopped_work_sent_at(),
        )
        await chat_turn_guard.record_stop(conv_id, datetime.now(UTC))
        assert await chat_turn_guard.fold_into_inflight_turn(conv_id, "user message")
        # FIFO: the bridge drains both in one pass, so the first item it queues
        # would be the partner's answer had it been let through.
        item = await asyncio.wait_for(kwargs["steering_queue"].get(), timeout=2)
        received.append(steering_text(item))
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    await _send(ws_env, {"content": "build the report"})

    assert received == ["user message"]


async def test_the_turns_queue_holds_back_a_stopped_answer_as_the_loop_takes_it(
    monkeypatch, ws_env
):
    """#4577: the bridge queued the partner's answer before a Stop sent from
    another replica was recorded; the loop takes it at its next iteration
    boundary through the queue's admission, which holds it back."""
    from datetime import UTC, datetime

    from app.tools.agent_loop import SteeringQueue

    conv_id = str(ws_env.conv.id)
    admitted: list = []

    async def _run(_db, _uid, _history, _content, **kwargs):
        queue = kwargs["steering_queue"]
        assert isinstance(queue, SteeringQueue)
        assert await chat_turn_guard.fold_into_inflight_turn(
            conv_id,
            "[Platform] partner answer",
            partner_wake=True,
            delegated_at=_stopped_work_sent_at(),
        )
        item = await asyncio.wait_for(queue.get(), timeout=2)
        await chat_turn_guard.record_stop(conv_id, datetime.now(UTC))
        admitted.extend(await queue.admit([item]))
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    await _send(ws_env, {"content": "build the report"})

    assert admitted == [], "the stopped work reached the loop"


async def test_the_leftover_check_keeps_the_replay_arithmetic(monkeypatch):
    """Whatever the queue held back - at an iteration boundary or with the
    leftovers - leaves the steered texts too, whose tail the leftovers are, so
    a follow-up still replays exactly what the loop injected. Matched by
    identity: an equal text the user sent is kept."""
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock

    from app.services import parent_wake
    from app.services.parent_wake import PartnerAnswerQueue

    stopped = datetime.now(UTC)
    monkeypatch.setattr(parent_wake, "_latest_stop", AsyncMock(return_value=stopped))

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr("app.core.database.async_session", _Session)

    def _partner(text, delegated_at):
        return chat_turn_guard.SteeringPayload(
            text=text, partner_wake=True, delegated_at=delegated_at
        )

    queue = PartnerAnswerQueue(CONV)
    injected = "injected earlier"
    at_boundary = "[Platform] answer held back at the boundary"
    stopped_answer = "[Platform] answer"
    fresh_answer = "[Platform] fresh answer"
    user_twin = "".join(["[Platform] ", "answer"])
    assert user_twin == stopped_answer and user_twin is not stopped_answer
    steered = [injected, at_boundary, stopped_answer, user_twin, fresh_answer]

    # The loop's iteration boundary: the user's steer goes in, the answer of
    # the stopped work does not.
    queue.put_nowait(injected)
    queue.put_fold(at_boundary, _partner(at_boundary, stopped - timedelta(minutes=1)))
    assert await queue.admit(drain_steering_queue(queue)) == [injected]

    # What is still queued when the loop returns.
    queue.put_fold(
        stopped_answer, _partner(stopped_answer, stopped - timedelta(seconds=1))
    )
    queue.put_nowait(user_twin)
    queue.put_fold(fresh_answer, _partner(fresh_answer, stopped + timedelta(seconds=1)))
    leftover = drain_steering_queue(queue)

    kept = await chat_module._admit_leftover_steers(queue, leftover, steered)

    assert kept == [user_twin, fresh_answer]
    assert kept[0] is user_twin
    assert steered == [injected, user_twin, fresh_answer]
    assert steered[1] is user_twin
    assert steered[: len(steered) - len(kept)] == [injected]
    # A queue that admits nothing again leaves both lists alone.
    plain = ["steer"]
    assert await chat_module._admit_leftover_steers(asyncio.Queue(), plain, plain) is plain


# ── Cross-process turn cancel ─────────────────────────────────────


async def test_request_turn_cancel_is_a_noop_when_idle(fake_redis):
    """No in-flight turn -> nothing to cancel, no request stored (a
    request left behind here could kill the NEXT turn)."""
    assert await chat_turn_guard.request_turn_cancel(CONV) is False
    assert chat_turn_guard.cancel_request_key(CONV) not in fake_redis.kv


async def test_request_and_consume_turn_cancel_roundtrip(fake_redis):
    """A request bound to the in-flight turn's token is consumed exactly
    once by that turn."""
    token = await chat_turn_guard.acquire_turn(CONV)
    assert await chat_turn_guard.request_turn_cancel(CONV) is True
    assert await chat_turn_guard.consume_turn_cancel(CONV, token) is True
    # Consumed: a second check must not re-cancel.
    assert await chat_turn_guard.consume_turn_cancel(CONV, token) is False


async def test_stale_cancel_request_never_kills_a_future_turn(fake_redis):
    """The request is token-bound: one addressed to a turn that already
    released must be discarded, not honoured by the next turn on the
    same conversation."""
    old_token = await chat_turn_guard.acquire_turn(CONV)
    assert await chat_turn_guard.request_turn_cancel(CONV) is True
    await chat_turn_guard.release_turn(CONV, old_token)
    new_token = await chat_turn_guard.acquire_turn(CONV)
    assert await chat_turn_guard.consume_turn_cancel(CONV, new_token) is False
    # The stale request is gone either way.
    assert chat_turn_guard.cancel_request_key(CONV) not in fake_redis.kv


async def test_remote_cancel_reaches_running_loop_via_bridge(
    monkeypatch, ws_env
):
    """The #3259 bug: a Stop that reaches a replica other than the one running
    the turn must cancel the detached in-flight turn.  The request travels
    through Redis and the turn's steering bridge sets the loop's
    ``cancel_event``."""
    conv_id = str(ws_env.conv.id)
    cancelled_in_run: list[bool] = []

    async def _run(_db, _uid, _history, _content, **kwargs):
        # Simulate the Stop arriving from another replica while this loop
        # runs: bind a cancel request to this turn's marker token.
        assert await chat_turn_guard.request_turn_cancel(conv_id) is True
        cancel_event = kwargs["cancel_event"]
        await asyncio.wait_for(cancel_event.wait(), timeout=5)
        cancelled_in_run.append(True)
        return ("stopped midway", None)

    _patch_general_chat(monkeypatch, _run)
    monkeypatch.setattr(chat_turn_guard, "BRIDGE_POLL_SECONDS", 0.01)
    await _send(ws_env, {"content": "build the report"})

    assert cancelled_in_run == [True]
    # The turn unwound normally: lock released, no stale cancel request.
    assert not await chat_turn_guard.is_turn_active(conv_id)


async def test_stop_without_local_turn_requests_remote_cancel(
    monkeypatch, ws_env
):
    """A Stop on a replica with NO local turn (the post-reload Stop click)
    must bind a cross-process cancel request to the holder's token — before
    #3259 it only acked ``cancelled`` and the detached turn kept running."""
    from app.api.v1 import chat_web_stream

    conv_id = str(ws_env.conv.id)
    # Another turn (pre-reload stream / other process) holds the lock.
    other_token = await chat_turn_guard.acquire_turn(conv_id)
    assert other_token is not None

    # The cascade spawns DB work irrelevant here — neutralize it.
    monkeypatch.setattr(
        chat_module, "_spawn_cancel_cascade", lambda _cid, *_a: None
    )

    answer = await chat_web_stream.cancel_web_chat_turn(
        chat_web_stream.WebChatCancelRequest(conversation_id=conv_id),
        SimpleNamespace(headers={}),
        db=_FakeSession(ws_env.conv, []),
        user=SimpleNamespace(id=ws_env.user_id),
    )

    assert answer == {"status": "cancelled", "turn": "remote"}
    # The request exists and is addressed to the detached turn's token.
    assert await chat_turn_guard.consume_turn_cancel(
        conv_id, other_token
    ) is True


# ── Cross-process wait skip ("Continue now") ────────────────────────


async def test_request_wait_skip_is_a_noop_when_idle(fake_redis):
    """No in-flight turn -> no pause to end, no request stored."""
    assert await chat_turn_guard.request_wait_skip(CONV) is False
    assert chat_turn_guard.wait_skip_request_key(CONV) not in fake_redis.kv


async def test_request_and_consume_wait_skip_roundtrip(fake_redis):
    """A skip bound to the in-flight turn's token is consumed once, and it is
    a different request from a cancel: consuming it leaves no cancel behind."""
    token = await chat_turn_guard.acquire_turn(CONV)
    assert await chat_turn_guard.request_wait_skip(CONV) is True
    assert await chat_turn_guard.consume_turn_cancel(CONV, token) is False
    assert await chat_turn_guard.consume_wait_skip(CONV, token) is True
    assert await chat_turn_guard.consume_wait_skip(CONV, token) is False


async def test_stale_wait_skip_never_skips_a_future_turn(fake_redis):
    """Token-bound: a skip addressed to a turn that released is dropped."""
    old_token = await chat_turn_guard.acquire_turn(CONV)
    assert await chat_turn_guard.request_wait_skip(CONV) is True
    await chat_turn_guard.release_turn(CONV, old_token)
    new_token = await chat_turn_guard.acquire_turn(CONV)
    assert await chat_turn_guard.consume_wait_skip(CONV, new_token) is False
    assert chat_turn_guard.wait_skip_request_key(CONV) not in fake_redis.kv
