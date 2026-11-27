"""Automatic resume of the turns a platform stop cuts, beyond web and channels.

Slice 3: a parent wake, a platform API stream turn and an agent account turn
(e-mail, chat) cut by the graceful drain are handed to the next process; the
same turn cut by anything else (a client leaving, an OOM kill, the turn's own
error) is not, and an agent account message is handed on at most
``MAX_AUTO_RESUMES`` times.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import platform as platform_api
from app.core import worker_shutdown
from app.core.shutdown_finalizers import BACKGROUND_TURN_CANCEL_MESSAGE
from app.services import cut_turn_resume, parent_wake
from app.services.agent_accounts import pipeline, state
from app.services.cut_turn_resume import MAX_AUTO_RESUMES, CutTurn

CONV = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = "22222222-2222-2222-2222-222222222222"
MSG = uuid.UUID("33333333-3333-3333-3333-333333333333")

GRACEFUL = asyncio.CancelledError(BACKGROUND_TURN_CANCEL_MESSAGE)
PLAIN = asyncio.CancelledError()


@pytest.fixture
def stopping(monkeypatch):
    """A platform stop in progress (SIGTERM of a rollout)."""
    monkeypatch.setattr(worker_shutdown, "_request", None)
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")


@pytest.fixture
def queued(monkeypatch):
    calls: list[dict] = []

    async def _enqueue(conversation_id, **kw):
        calls.append({"conversation_id": conversation_id, **kw})
        return True

    monkeypatch.setattr(cut_turn_resume, "enqueue_cut_turn", _enqueue)
    return calls


# ── Parent wakes ──────────────────────────────────────────────────────────


async def test_a_cut_wake_is_queued_with_its_stored_message(stopping, queued):
    await parent_wake._queue_cut_wake(CONV, user_id=USER, message_id=str(MSG))

    [call] = queued
    assert call["conversation_id"] == CONV
    assert call["user_message_id"] == str(MSG)
    assert call["entry"] == parent_wake.WAKE_ENTRY
    assert call["cause"] == worker_shutdown.STOP


@pytest.mark.parametrize(
    ("exc", "message_id", "queued_expected"),
    [(GRACEFUL, str(MSG), True), (PLAIN, str(MSG), False), (GRACEFUL, None, False), (RuntimeError("x"), str(MSG), False)],
    ids=["platform-stop", "other-cancel", "resumed-turn", "error"],
)
async def test_a_wake_is_handed_on_only_when_the_platform_cut_its_stored_message(
    stopping, queued, exc, message_id, queued_expected
):
    assert (
        await parent_wake._hand_on_cut_wake(exc, CONV, user_id=USER, message_id=message_id) is queued_expected
    )
    assert len(queued) == int(queued_expected)


async def test_queueing_a_cut_wake_never_raises(stopping, monkeypatch):
    async def _boom(*_a, **_kw):
        raise RuntimeError("redis down")

    monkeypatch.setattr(cut_turn_resume, "enqueue_cut_turn", _boom)

    await parent_wake._queue_cut_wake(CONV, user_id=USER, message_id=str(MSG))


async def test_a_resumed_wake_restores_the_platform_text_a_person_message_does_not(monkeypatch):
    from contextlib import asynccontextmanager

    import app.core.database as database
    from app.services import turn_checkpoint

    runs: list[dict] = []

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    def _session_answers():
        conv = SimpleNamespace(
            id=CONV, user_id=uuid.UUID(USER), agent_id=None, channel="web",
            channel_thread_id=None, tool_overrides_json=None,
        )
        msg = SimpleNamespace(id=MSG, role="user", content="the task finished", sequence=3)
        return [conv, msg, None]

    @asynccontextmanager
    async def _session():
        answers = _session_answers()

        class _S:
            async def execute(self, _stmt):
                return _Result(answers.pop(0))

            async def commit(self):
                return None

        yield _S()

    async def _not_stopped(*_a, **_kw):
        return False

    async def _load_replay(db, conversation_id, *, user_id=None, now=None, code_interpreter=False):
        return turn_checkpoint.ResumedWork(found=False), []

    async def _run(db, **kw):
        runs.append(kw)
        return False

    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(parent_wake, "_stopped_since", _not_stopped)
    monkeypatch.setattr(turn_checkpoint, "load_replay", _load_replay)
    monkeypatch.setattr(parent_wake, "_run_parent_wake_turn", _run)

    for entry in (parent_wake.WAKE_ENTRY, "web"):
        cut = CutTurn(str(CONV), USER, str(MSG), None, "stop", 0, "2026-11-01T01:00:00+00:00", entry)
        assert await parent_wake.resume_cut_turn(cut) is True

    assert [r["wake_from_platform"] for r in runs] == [True, False]
    assert all(r["persist_user"] is False for r in runs)


# ── Platform API stream turns ─────────────────────────────────────────────


async def test_a_platform_api_turn_cut_by_the_drain_is_queued(stopping, queued):
    assert await platform_api.queue_cut_platform_turn(GRACEFUL, CONV, user_id=USER, user_message_id=MSG) is True

    [call] = queued
    assert call["user_message_id"] == MSG
    assert call["entry"] == platform_api.PLATFORM_CUT_ENTRY
    assert call["cause"] == worker_shutdown.STOP


@pytest.mark.parametrize(
    ("exc", "message_id"),
    [(PLAIN, MSG), (GeneratorExit(), MSG), (GRACEFUL, None)],
    ids=["client-left", "generator-closed", "no-stored-message"],
)
async def test_a_platform_api_turn_not_cut_by_the_platform_is_not_queued(stopping, queued, exc, message_id):
    assert (
        await platform_api.queue_cut_platform_turn(exc, CONV, user_id=USER, user_message_id=message_id) is False
    )
    assert queued == []


@pytest.mark.parametrize(
    ("cut", "answer", "silent"),
    [
        (True, None, True),
        (True, "  ", True),
        (True, "The answer", False),
        (False, None, False),
        (False, "", False),
    ],
    ids=["cut-no-answer", "cut-blank-answer", "cut-answered", "failed", "failed-blank"],
)
def test_only_a_cut_unanswered_platform_turn_stores_no_error_reply(cut, answer, silent):
    # A cut turn's resume answers it: the error fallback would read as a
    # first, false answer. A turn that failed for its own reason keeps it.
    assert platform_api.platform_cut_unanswered(cut=cut, answer=answer) is silent


async def test_a_cut_platform_api_turn_that_answered_drops_its_resume(monkeypatch):
    forgotten: list[tuple] = []

    async def _forget(conversation_id, *, user_message_id):
        forgotten.append((conversation_id, user_message_id))
        return True

    monkeypatch.setattr(cut_turn_resume, "forget_cut_turn", _forget)

    await platform_api.forget_cut_platform_turn(CONV, user_message_id=MSG)

    assert forgotten == [(CONV, MSG)]


class _Checkpoint:
    def __init__(self, may_hold_row: bool = True) -> None:
        self.closes: list[bool] = []
        self.may_hold_row = may_hold_row

    async def aclose(self, *, flush: bool) -> None:
        self.closes.append(flush)


@pytest.fixture
def finalizers(monkeypatch):
    import app.core.shutdown_finalizers as shutdown_finalizers

    started: list = []

    def _start(coro, *, name):
        started.append(name)
        return asyncio.ensure_future(coro)

    monkeypatch.setattr(shutdown_finalizers, "start_shutdown_finalizer", _start)
    return started


@pytest.mark.parametrize("cut_queued", [True, False], ids=["queued", "queue-refused"])
async def test_a_cut_unanswered_platform_turn_flushes_its_tool_rounds_for_the_resume(finalizers, cut_queued):
    checkpoint = _Checkpoint()

    discard = await platform_api.settle_platform_checkpoint(
        checkpoint, CONV, cut=True, cut_queued=cut_queued, answered=False, user_message_id=MSG
    )
    await asyncio.sleep(0)

    assert discard is False, "the row stays for the resume (automatic or one-click) to replay"
    assert checkpoint.closes == [True]
    assert finalizers == [f"turn-checkpoint:{CONV}"]


async def test_an_uncut_unanswered_platform_turn_keeps_today_s_checkpoint_close(finalizers):
    checkpoint = _Checkpoint()

    await platform_api.settle_platform_checkpoint(
        checkpoint, CONV, cut=False, cut_queued=False, answered=False, user_message_id=MSG
    )

    assert checkpoint.closes == [False]
    assert finalizers == []


async def test_a_platform_turn_that_answered_deletes_its_checkpoint_and_its_resume(monkeypatch, finalizers):
    forgotten: list = []

    async def _forget(conversation_id, *, user_message_id):
        forgotten.append(user_message_id)
        return True

    monkeypatch.setattr(cut_turn_resume, "forget_cut_turn", _forget)
    for cut_queued in (True, False):
        checkpoint = _Checkpoint()
        assert await platform_api.settle_platform_checkpoint(
            checkpoint, CONV, cut=cut_queued, cut_queued=cut_queued, answered=True, user_message_id=MSG
        ) is True
        assert checkpoint.closes == [False]

    assert forgotten == [MSG], "only the queued cut drops a resume"
    assert finalizers == []


class _ScriptRedis:
    """GET and the compare-and-delete script, in memory."""

    def __init__(self, records: dict) -> None:
        self.kv = dict(records)
        self.pending = set(records)

    async def get(self, key):
        return self.kv.get(key)

    async def eval(self, _script, _numkeys, key, _pending_key, expected, member):
        if self.kv.get(key) != expected:
            return 0
        del self.kv[key]
        self.pending.discard(member)
        return 1


def _record(message_id: str) -> str:
    return CutTurn(str(CONV), USER, message_id, None, "stop", 0, "2026-11-01T01:00:00+00:00", "platform_api").to_json()


async def test_forgetting_a_cut_turn_leaves_another_message_s_resume(monkeypatch):
    key = cut_turn_resume.record_key(CONV)
    fake = _ScriptRedis({key: _record("other-message")})

    async def _redis():
        return fake

    monkeypatch.setattr(cut_turn_resume, "_redis", _redis)

    assert await cut_turn_resume.forget_cut_turn(CONV, user_message_id=MSG) is False
    assert key in fake.kv, "a concurrent stream's cut keeps its resume"

    fake.kv[key] = _record(str(MSG))
    assert await cut_turn_resume.forget_cut_turn(CONV, user_message_id=MSG) is True
    assert key not in fake.kv


# ── Agent account (e-mail, chat) turns ────────────────────────────────────


def _event():
    return SimpleNamespace(conversation_id="thread-1", message_id="mail-1")


@pytest.fixture
def account_env(monkeypatch):
    env = SimpleNamespace(parked=[], claims=0)

    async def _park(account_id, event, due):
        env.parked.append((account_id, event, due))
        return "member"

    async def _claim(channel, thread_id, message_id):
        env.claims += 1
        env.claim_key = (channel, thread_id, message_id)
        return env.claims <= MAX_AUTO_RESUMES

    import app.services.chat_turn_guard as guard

    monkeypatch.setattr(state, "park", _park)
    monkeypatch.setattr(guard, "claim_channel_resume", _claim)
    return env


async def test_an_account_turn_cut_by_the_platform_is_parked_due_now(stopping, account_env):
    before = time.time()

    assert await pipeline._park_cut_turn("acc-1", _event(), GRACEFUL) is True

    [(account_id, _ev, due)] = account_env.parked
    assert account_id == "acc-1"
    assert before <= due <= time.time()
    assert account_env.claim_key == (f"{pipeline.ACCOUNT_RESUME_CHANNEL}:acc-1", "thread-1", "mail-1")


async def test_an_account_message_is_handed_on_at_most_the_cap(stopping, account_env):
    results = [await pipeline._park_cut_turn("acc-1", _event(), GRACEFUL) for _ in range(MAX_AUTO_RESUMES + 1)]

    assert results == [True] * MAX_AUTO_RESUMES + [False]
    assert len(account_env.parked) == MAX_AUTO_RESUMES


async def test_an_account_turn_cancelled_otherwise_is_not_parked(stopping, account_env):
    assert await pipeline._park_cut_turn("acc-1", _event(), PLAIN) is False
    assert account_env.parked == []


async def test_an_account_turn_cut_by_the_drain_with_no_recorded_cause_is_parked(monkeypatch, account_env):
    """Only a stopping server's drain raises it: a platform stop, as the web turn reads it (#10779)."""
    monkeypatch.setattr(worker_shutdown, "_request", None)

    assert await pipeline._park_cut_turn("acc-1", _event(), GRACEFUL) is True
    assert len(account_env.parked) == 1


@pytest.mark.parametrize("acted", [False, True], ids=["before-acting", "after-acting"])
async def test_an_account_turn_is_parked_only_if_cut_before_it_acted(stopping, account_env, monkeypatch, acted):
    async def _turn(db, account, event, decision, *, after_grace, progress, absorbed=None, trace_out=None):
        # Cut anywhere in the turn: opening the session, the agent, the triage.
        progress.acted = acted
        raise asyncio.CancelledError(BACKGROUND_TURN_CANCEL_MESSAGE)

    monkeypatch.setattr(pipeline, "_answer_turn", _turn)

    with pytest.raises(asyncio.CancelledError):
        await pipeline._answer(None, SimpleNamespace(id="acc-1"), _event(), None, after_grace=False)

    assert len(account_env.parked) == (0 if acted else 1)


async def test_an_account_turn_cut_while_handing_too_many_recipients_to_the_owner_is_not_parked(
    stopping, account_env, monkeypatch
):
    """The real turn: a reply-all over the account's limit is handed to the owner, and the stop cuts the filing."""
    import app.services.agent_chat as agent_chat

    provider = SimpleNamespace(
        capabilities=SimpleNamespace(triage=True, verified_sender=False),
        label="Email",
        platform="email",
        integration_provider="microsoft_365",
    )

    async def _fetch_context(session, event, limit):
        return []

    provider.fetch_context = _fetch_context

    async def _db_get(model, ident):
        return SimpleNamespace(id="agent-1", enabled=True)

    async def _open_session(db, account, provider):
        return SimpleNamespace()

    async def _take_turn(account_id, limit):
        return True

    async def _no_files(*_a, **_kw):
        return []

    async def _agent_reply(*_a, **_kw):
        return '{"decision": "reply", "reply": "Thanks, noted."}', None

    handed: list = []

    async def _triaged(db, session, provider, account, agent, event, decision, triaged):
        handed.append(triaged)
        # The platform stop lands while the message is flagged, filed and notified.
        raise asyncio.CancelledError(BACKGROUND_TURN_CANCEL_MESSAGE)

    monkeypatch.setattr(pipeline, "get_provider", lambda _name: provider)
    monkeypatch.setattr(pipeline, "open_session", _open_session)
    monkeypatch.setattr(state, "take_turn", _take_turn)
    monkeypatch.setattr(pipeline, "_message_files", _no_files)
    monkeypatch.setattr(pipeline, "build_turn", lambda *_a: ([], "Hello all"))
    monkeypatch.setattr(pipeline, "account_brief", lambda **_kw: "")
    monkeypatch.setattr(pipeline, "message_context", lambda *_a, **_kw: {})
    monkeypatch.setattr(agent_chat, "run_agent_chat", _agent_reply)
    monkeypatch.setattr(pipeline, "_triaged", _triaged)

    account = SimpleNamespace(
        id="acc-1",
        user_id="user-1",
        agent_id="agent-1",
        provider="email",
        integration_id="integration-1",
        max_turns_per_hour=0,
        context_messages=5,
        account_display_name="Support",
        mode="assistant",
        ai_label_text=None,
        max_reply_recipients=2,
    )
    event = SimpleNamespace(
        conversation_id="thread-1",
        message_id="mail-1",
        conversation_type="email",
        conversation_name="Hello all",
        reply_recipients=3,
    )
    decision = SimpleNamespace(must_reply=False, instructions="", rule_name="Everyone", ai_label=False, access=None)

    with pytest.raises(asyncio.CancelledError):
        await pipeline._answer(SimpleNamespace(get=_db_get), account, event, decision, after_grace=False)

    assert [(t.decision, t.note) for t in handed] == [("needs_owner", pipeline.too_many_recipients(account, event))]
    assert account_env.parked == []
    assert account_env.claims == 0


async def test_parking_a_cut_account_turn_never_raises(stopping, account_env, monkeypatch):
    async def _boom(*_a, **_kw):
        raise RuntimeError("redis down")

    monkeypatch.setattr(state, "park", _boom)

    assert await pipeline._park_cut_turn("acc-1", _event(), GRACEFUL) is False
