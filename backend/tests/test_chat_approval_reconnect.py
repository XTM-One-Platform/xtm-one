"""A paused turn must survive the socket it was announced on.

``approval_required`` is sent once, over one socket, and the proposals lived
nowhere else. A reload — or a closed tab, or a dropped connection — therefore
lost the prompt while ``await_approval_decision`` went on waiting, deliberately,
forever. Because the turn keeps refreshing the conversation's processing marker
the whole time, every later message in that conversation queued behind a
decision nobody could give any more: the agent simply stopped answering, with
nothing on screen to say why. Only a backend restart cleared it.

So the pause is now recorded in Redis beside the turn marker, and the
conversation-detail response replays it to a client that reloaded through one.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.services import chat_turn_guard as guard


class _FakeRedis:
    """Enough of the client for the pending-approval record: get/set/delete
    plus the ``expire`` that keeps it alive alongside the turn marker."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.ttl: dict[str, int] = {}

    async def set(self, key, value, nx=False, xx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        if ex is not None:
            self.ttl[key] = ex
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, key):
        self.ttl.pop(key, None)
        # Lists too: the real client deletes a key whatever type it holds, and
        # the inbox this guard clears is a list.
        removed = self.kv.pop(key, None) is not None
        removed = self.lists.pop(key, None) is not None or removed
        return 1 if removed else 0

    async def expire(self, key, ttl):
        if key not in self.kv:
            return 0
        self.ttl[key] = ttl
        return 1

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def lpop(self, key):
        lst = self.lists.get(key)
        return lst.pop(0) if lst else None

    async def exists(self, key):
        return 1 if key in self.kv else 0

    async def eval(self, _script, _numkeys, *_args):
        return 1


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()

    async def _redis():
        return r

    monkeypatch.setattr(guard, "_redis", _redis)
    return r


CONV = "3ff74c57-8872-4399-8451-2abf4f4406d9"
PROPOSALS = [
    {"tool_call_id": "call_1", "tool_name": "Additioner", "arguments": {"a": 1}},
]


@pytest.mark.asyncio
async def test_a_pause_is_readable_after_the_socket_is_gone(fake_redis):
    """The whole point: a client that never saw the frame can still be told."""
    await guard.set_pending_approvals(CONV, PROPOSALS)

    assert await guard.get_pending_approvals(CONV) == PROPOSALS


@pytest.mark.asyncio
async def test_no_pause_reads_as_none(fake_redis):
    """A running turn that is not paused must not grow a phantom prompt."""
    assert await guard.get_pending_approvals(CONV) is None


@pytest.mark.asyncio
async def test_clearing_ends_the_replay(fake_redis):
    """Once decided, the pause must not be replayed to the next client that
    loads the conversation — it would ask for a decision already given."""
    await guard.set_pending_approvals(CONV, PROPOSALS)
    await guard.clear_pending_approvals(CONV)

    assert await guard.get_pending_approvals(CONV) is None


@pytest.mark.asyncio
async def test_a_corrupt_record_is_not_a_prompt(fake_redis):
    """Garbage in the key must not raise into the conversation-detail
    response: failing to restore a prompt is recoverable, failing to load the
    conversation at all is not."""
    fake_redis.kv[guard.pending_approvals_key(CONV)] = "{not json"

    assert await guard.get_pending_approvals(CONV) is None


@pytest.mark.asyncio
async def test_a_non_list_record_is_not_a_prompt(fake_redis):
    """Valid JSON of the wrong shape reaches the UI as proposals to render."""
    fake_redis.kv[guard.pending_approvals_key(CONV)] = json.dumps({"nope": True})

    assert await guard.get_pending_approvals(CONV) is None


@pytest.mark.asyncio
async def test_the_pause_outlives_the_marker_ttl(fake_redis):
    """A reviewer is a person: the pause can outlast several marker TTLs, so
    the record rides the same heartbeat rather than expiring under a turn that
    is still waiting for it."""
    await guard.set_pending_approvals(CONV, PROPOSALS)
    key = guard.pending_approvals_key(CONV)
    fake_redis.ttl[key] = 5

    await guard.refresh_turn(CONV, "token")

    assert fake_redis.ttl[key] == guard.TURN_MARKER_TTL_SECONDS


@pytest.mark.asyncio
async def test_refresh_does_not_invent_a_pause(fake_redis):
    """A turn that never paused must not gain a pending-approval key from its
    own heartbeat."""
    await guard.refresh_turn(CONV, "token")

    assert guard.pending_approvals_key(CONV) not in fake_redis.kv


# --- The answer must reach the turn from a socket that never asked ---------


@pytest.mark.asyncio
async def test_a_decision_needs_a_turn_to_answer(fake_redis):
    """``push_approval_decision`` refuses when nothing is waiting, which is what
    lets the socket tell the reviewer their click went nowhere instead of
    leaving them staring at a prompt that merely vanished."""
    assert await guard.push_approval_decision(CONV, {"call_1": {"decision": "approve"}}) is False


@pytest.mark.asyncio
async def test_a_decision_reaches_a_waiting_turn(fake_redis):
    """The inbox is the whole point of routing through Redis: the socket
    carrying the verdict and the turn awaiting it need not be the same one."""
    fake_redis.kv[guard.processing_key(CONV)] = "token"

    assert await guard.push_approval_decision(CONV, {"call_1": {"decision": "approve"}}) is True
    assert await guard.await_approval_decision(CONV) == {"call_1": {"decision": "approve"}}


def test_a_reloaded_page_answers_too():
    """Regression guard for the half that made the reload fix look broken.

    A reviewer who reloaded answers from a page that never saw the pause: the
    approve route hands the verdict to the waiting turn through Redis, never
    through the stream that announced it.
    """
    import inspect

    from app.api.v1 import chat_web_stream

    source = inspect.getsource(chat_web_stream.approve_web_chat_tools)
    assert "apply_approval_decisions(" in source
    assert "live_connection" not in source


# --- A turn that holds no marker is still a waiting turn -------------------


@pytest.mark.asyncio
async def test_a_recorded_pause_is_enough_to_accept_a_decision(fake_redis):
    """The embedded-assistant path runs a turn without acquiring the chat turn
    marker, so requiring the marker refused every decision it sent while its
    turn went on blocking — the approve endpoint answered 409 for a pause that
    was demonstrably live."""
    await guard.set_pending_approvals(CONV, PROPOSALS)

    assert await guard.push_approval_decision(CONV, {"call_1": {"decision": "approve"}}) is True


@pytest.mark.asyncio
async def test_a_new_pause_starts_with_an_empty_inbox(fake_redis):
    """A decision that outlived its turn must not answer the next prompt.

    It survives under the inbox TTL, and the next pause pops it immediately:
    its ids match nothing, every proposal defaults to a refusal, and the batch
    is declined on behalf of a reviewer who never saw it.
    """
    fake_redis.kv[guard.processing_key(CONV)] = "token"
    await guard.push_approval_decision(CONV, {"stale_call": {"decision": "approve"}})

    await guard.set_pending_approvals(CONV, PROPOSALS)

    assert fake_redis.lists.get(guard.approval_inbox_key(CONV), []) == []


@pytest.mark.asyncio
async def test_clearing_a_pause_clears_its_inbox(fake_redis):
    """Nothing may be left behind for the next pause to trip over."""
    fake_redis.kv[guard.processing_key(CONV)] = "token"
    await guard.set_pending_approvals(CONV, PROPOSALS)
    await guard.push_approval_decision(CONV, {"call_1": {"decision": "approve"}})

    await guard.clear_pending_approvals(CONV)

    assert fake_redis.lists.get(guard.approval_inbox_key(CONV), []) == []


@pytest.mark.asyncio
async def test_waiting_keeps_the_pause_from_expiring(fake_redis):
    """The wait is the liveness signal, so it must hold the record open.

    The chat transports refresh it from their turn marker; the embedded
    assistant holds no marker, so a TTL alone gave the reviewer a hidden
    five-minute deadline after which their decision was answered with 409.
    """
    fake_redis.kv[guard.processing_key(CONV)] = "token"
    await guard.set_pending_approvals(CONV, PROPOSALS)
    key = guard.pending_approvals_key(CONV)
    fake_redis.ttl[key] = 3  # nearly gone

    # One pass of the poll loop: a decision is already waiting, so it returns.
    await guard.push_approval_decision(CONV, {"call_1": {"decision": "approve"}})
    await guard.await_approval_decision(CONV, poll_seconds=0)

    assert fake_redis.ttl[key] == guard.TURN_MARKER_TTL_SECONDS


# --- Recovering a prompt the reload threw away -----------------------------


def test_the_embedded_surface_can_be_asked_what_it_is_waiting_on():
    """``approval_required`` is one event on a stream, so a reload loses it —
    including the `tool_call_id`s a decision has to name. Without somewhere to
    ask, the turn waits for an answer nobody can give and the conversation stops
    replying with nothing on screen to explain it."""
    from app.api.v1.platform import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert (
        "/chat/conversations/{conversation_id}/pending-approvals" in paths
    ), "a reloaded widget needs somewhere to rediscover a pause"


@pytest.mark.asyncio
async def test_a_recovered_prompt_is_the_one_that_was_sent(fake_redis):
    """Whatever the reload lost, the record still holds — same proposals, same
    ids, so the client can render and answer it as if nothing happened."""
    await guard.set_pending_approvals(CONV, PROPOSALS)

    assert await guard.get_pending_approvals(CONV) == PROPOSALS


# --- Parked for a reload, not for ever -------------------------------------


@pytest.mark.asyncio
async def test_a_reviewer_who_is_thinking_is_never_on_a_clock(fake_redis):
    """The cap counts only while nobody is connected. A client that is there —
    or a caller that cannot tell — waits as long as the person takes."""
    fake_redis.kv[guard.processing_key(CONV)] = "token"
    await guard.set_pending_approvals(CONV, PROPOSALS)

    async def _answer_later():
        await asyncio.sleep(0.05)
        await guard.push_approval_decision(CONV, {"call_1": {"decision": "approve"}})

    asyncio.ensure_future(_answer_later())
    decision = await guard.await_approval_decision(
        CONV,
        poll_seconds=0.01,
        client_present=lambda: True,
        abandon_after=0.02,  # would have fired many times over
    )

    assert decision == {"call_1": {"decision": "approve"}}


@pytest.mark.asyncio
async def test_an_unreachable_reviewer_eventually_frees_the_turn(fake_redis):
    """A stream that dropped and never came back leaves nobody able to answer,
    so the turn stops waiting for them — and takes the pause with it, rather
    than leaving a record advertising a prompt no turn is behind."""
    fake_redis.kv[guard.processing_key(CONV)] = "token"
    await guard.set_pending_approvals(CONV, PROPOSALS)

    with pytest.raises(guard.ApprovalAbandonedError):
        await guard.await_approval_decision(
            CONV,
            poll_seconds=0.01,
            client_present=lambda: False,
            abandon_after=0.03,
        )

    assert await guard.get_pending_approvals(CONV) is None


@pytest.mark.asyncio
async def test_coming_back_resets_the_count(fake_redis):
    """A reload is a disconnect. It must not push the turn toward giving up."""
    fake_redis.kv[guard.processing_key(CONV)] = "token"
    await guard.set_pending_approvals(CONV, PROPOSALS)

    here = {"value": False}

    async def _reconnect_then_answer():
        await asyncio.sleep(0.03)
        here["value"] = True
        await asyncio.sleep(0.03)
        await guard.push_approval_decision(CONV, {"call_1": {"decision": "approve"}})

    asyncio.ensure_future(_reconnect_then_answer())
    decision = await guard.await_approval_decision(
        CONV,
        poll_seconds=0.01,
        client_present=lambda: here["value"],
        abandon_after=0.05,
    )

    assert decision == {"call_1": {"decision": "approve"}}


@pytest.mark.asyncio
async def test_a_reload_is_not_absence(fake_redis):
    """The flaw the integrator found: `_client_gone` belongs to the request that
    carried the prompt. A reload is a *new* request, so that flag stays true for
    ever while the person is back and looking at the recovered prompt — and the
    abandonment clock ran against them.

    Presence therefore has to be read from the conversation, not from whichever
    request happens to be waiting on it.
    """
    fake_redis.kv[guard.processing_key(CONV)] = "token"
    await guard.set_pending_approvals(CONV, PROPOSALS)
    await guard.mark_client_seen(CONV)  # the reloaded client asked for the prompt

    async def _answer_after_thinking():
        await asyncio.sleep(0.05)
        await guard.push_approval_decision(CONV, {"call_1": {"decision": "approve"}})

    asyncio.ensure_future(_answer_after_thinking())
    decision = await guard.await_approval_decision(
        CONV,
        poll_seconds=0.01,
        # The dead stream's flag, which can never turn back.
        client_present=lambda: False,
        abandon_after=0.02,
    )

    assert decision == {"call_1": {"decision": "approve"}}


@pytest.mark.asyncio
async def test_a_client_that_never_shows_itself_still_frees_the_turn(fake_redis):
    """The mark lapses with its TTL, so silence is still eventually silence."""
    fake_redis.kv[guard.processing_key(CONV)] = "token"
    await guard.set_pending_approvals(CONV, PROPOSALS)

    with pytest.raises(guard.ApprovalAbandonedError):
        await guard.await_approval_decision(
            CONV,
            poll_seconds=0.01,
            client_present=lambda: False,
            abandon_after=0.03,
        )


def test_the_recovery_read_reports_whether_the_turn_is_still_going():
    """A decision on a recovered prompt resumes a turn whose stream the reload
    closed, so the reply lands in the conversation with nothing arriving live.
    Without a completion signal every consumer polls the transcript blind and
    guesses when to give up — so the read reports turn state, from the marker
    the steering path already refreshes every second."""
    import inspect

    from app.api.v1 import platform

    source = inspect.getsource(platform.get_platform_chat_pending_approvals)
    assert '"turn"' in source
    assert "_steer_active_key" in source, (
        "reuse the marker that is already maintained rather than inventing one"
    )


# --- The guard is only a guard where the caller supplies presence ----------


def test_every_approval_wait_can_tell_whether_anyone_is_there():
    """``await_approval_decision`` reads a missing ``client_present`` as "someone
    is there", so a caller that omits it disables the abandonment guard
    entirely — silently, because the wait looks identical either way.

    The browser socket omitted it. A closed tab then held the turn (and its
    processing marker) until the loop's own timeout, which is
    ``max(600, iterations * 120)`` seconds and so routinely *longer* than the
    30-minute guard written for exactly this case.

    A sweep rather than a list of known call sites: the next caller has to
    answer the question too.
    """
    import ast
    import pathlib

    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # computer_use.py has an unrelated, deadline-bounded namesake.
        if not any(
            isinstance(n, ast.ImportFrom)
            and n.module == "app.services.chat_turn_guard"
            and any(a.name == "await_approval_decision" for a in n.names)
            for n in ast.walk(tree)
        ):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "await_approval_decision":
                continue
            if not any(kw.arg == "client_present" for kw in node.keywords):
                offenders.append(f"{path.relative_to(app_dir)}:{node.lineno}")

    assert not offenders, (
        "these approval waits cannot tell a thinking reviewer from an absent "
        f"one, so the abandonment guard never fires: {offenders}"
    )


def test_both_chat_services_end_the_turn_when_an_approval_is_abandoned():
    """Enabling the guard on the socket makes ``ApprovalAbandonedError``
    reachable there for the first time. Unhandled, it would replace a wedged
    turn with a generic failure — so both services answer it the way they
    answer a timeout.
    """
    import inspect

    from app.services.agent_chat import run_agent_chat
    from app.services.general_chat import run_general_chat

    for entry in (run_agent_chat, run_general_chat):
        assert "except ApprovalAbandonedError" in inspect.getsource(entry), (
            f"{entry.__name__} lets the abandonment guard raise past it"
        )


# --- The seen mark must not double the abandonment window ------------------


def test_the_seen_mark_is_far_shorter_than_the_abandonment_window():
    """The mark suppresses the count while it lives, so a window-length TTL made
    the two add up: a reload at T=0 held the mark to T=1800, the count then
    needed another 1800, and the turn held the conversation for ~60 minutes
    against a documented 30.
    """
    assert guard.CLIENT_SEEN_TTL_SECONDS * 4 < guard.ABANDONED_APPROVAL_SECONDS


@pytest.mark.asyncio
async def test_the_default_mark_uses_the_short_ttl(fake_redis, monkeypatch):
    """Pins the wiring, not just the constant: the default argument is what the
    two production callers rely on."""
    seen = {}

    class _R:
        async def set(self, key, val, ex=None):
            seen["key"], seen["ex"] = key, ex

    monkeypatch.setattr(guard, "_redis", lambda: _fake_async(_R()))
    await guard.mark_client_seen(CONV)

    assert seen["ex"] == guard.CLIENT_SEEN_TTL_SECONDS
    assert seen["ex"] < guard.ABANDONED_APPROVAL_SECONDS


async def _fake_async(value):
    return value
