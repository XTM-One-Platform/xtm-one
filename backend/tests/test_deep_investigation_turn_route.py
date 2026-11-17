"""Routing a chat turn to the Deep Investigation orchestrator.

The turn itself — metering, the quota gate, a message for every failure, and
the marker that keeps a failed run's partial record reachable — lives in
``run_deep_investigation_turn`` and is covered in
``test_deep_investigation_chat.py``. What is covered HERE is the one thing that
cannot move into the service: the *routing decision* in ``run_chat_connection``.

The review of #3316 named that gap, and rightly — the reported defect was a
stale ``mode`` surviving into a later message, which only a test that drives the
send can catch.

Driven through ``run_chat_connection`` with the steering suite's send connection
(same precedent as ``test_chat_turn_singleflight.py``).
"""

from __future__ import annotations

import asyncio

import pytest

from app.api.v1 import chat as chat_module

# The fixtures must be imported under their own names for pytest to resolve
# them; the noqa covers the fixture-as-parameter shadowing.
from tests.test_chat_web_steering import (  # noqa: F401
    _FakeConnection,
    _run_connection,
    _send,
    _steer,
    _wait_for_frame,
    fake_rl,
    ws_env,
)

SUBJECT = "Who is behind the RUBY-DELTA infrastructure?"


@pytest.fixture(autouse=True)
def _deep_investigation_enabled(monkeypatch):
    """The routing under test exists only behind ``DEEP_INVESTIGATION``."""
    monkeypatch.setattr(chat_module, "is_feature_enabled", lambda flag: flag == "DEEP_INVESTIGATION")


def _assistant_text(ws_env) -> str:  # noqa: F811
    """The assistant message the turn persisted, as the user would read it."""
    texts = [content for role, content in ws_env.stored if role == "assistant"]
    assert texts, "the turn must persist an assistant message"
    return texts[-1]


def _patch_turn(monkeypatch, stub):
    monkeypatch.setattr(chat_module, "run_deep_investigation_turn", stub)


def _patch_general_chat(monkeypatch, stub):
    import app.services.general_chat as general_chat_module

    monkeypatch.setattr(general_chat_module, "run_general_chat", stub)


async def test_the_investigation_mode_reaches_the_orchestrator(
    monkeypatch, ws_env  # noqa: F811
):
    """``mode: deep_investigation`` routes to the investigation turn, with the
    message text as the subject, and the agent loop never runs."""
    seen: dict[str, object] = {}

    async def _run_turn(_db, *, subject, **_kw):
        seen["subject"] = subject
        return "Here is what the sources say."

    async def _run_general(*_a, **_kw):
        seen["general_chat_ran"] = True
        return ("general answer", None)

    _patch_turn(monkeypatch, _run_turn)
    _patch_general_chat(monkeypatch, _run_general)

    await _send(ws_env, {"content": SUBJECT, "mode": "deep_investigation"})

    assert seen.get("subject") == SUBJECT
    assert "general_chat_ran" not in seen, (
        "the investigation turn must bypass the agent loop, not run both"
    )
    assert _assistant_text(ws_env) == "Here is what the sources say."


async def test_a_message_sent_during_an_investigation_is_answered_after_it(
    monkeypatch, ws_env  # noqa: F811
):
    """A message steered in while the investigation runs is stored; with no
    model to steer, it must then get an ordinary follow-up answer on the same
    stream, never be acknowledged and dropped."""
    steer = "and check the domain registrar too"
    answered: list[str] = []

    async def _run_turn(_db, **_kw):
        deadline = asyncio.get_event_loop().time() + 2
        while ("user", steer) not in ws_env.stored:
            assert asyncio.get_event_loop().time() < deadline
            await asyncio.sleep(0.01)
        return "Here is what the sources say."

    async def _run_general(_db, _uid, _history, content, **_kw):
        answered.append(content)
        return ("follow-up answer", None)

    _patch_turn(monkeypatch, _run_turn)
    _patch_general_chat(monkeypatch, _run_general)

    conn = _FakeConnection(
        str(ws_env.conv.id), {"content": SUBJECT, "mode": "deep_investigation"}, ws_env.timeline
    )
    task = asyncio.create_task(_run_connection(conn, ws_env.user_id))
    assert await _wait_for_frame(conn, "turn_started"), "the investigation never started"
    answer = await _steer(ws_env, steer)
    await asyncio.wait_for(task, 5)

    assert answer["status"] == "steered"
    assert answered == [steer]
    done = [f["content"] for f in conn.sent if f.get("type") == "done"]
    assert done == ["Here is what the sources say.", "follow-up answer"]


async def test_the_investigation_message_carries_no_previous_turns_trace(
    monkeypatch, ws_env  # noqa: F811
):
    """An investigation makes no traced model call, so the latest trace ids of
    the conversation belong to an earlier turn and must not be stamped on it."""
    stale = "0b9e3c7a-1111-4222-8333-944455556666"

    async def _latest(_conv_id):
        return {"llm_trace_id": stale}

    monkeypatch.setattr(chat_module, "load_latest_langfuse_ids", _latest)
    stored_kwargs: list[dict] = []
    original_store = chat_module.store_message

    async def _store(db, conv_id, role, content, **kw):
        if role == "assistant":
            stored_kwargs.append(kw)
        return await original_store(db, conv_id, role, content, **kw)

    monkeypatch.setattr(chat_module, "store_message", _store)

    async def _run_turn(_db, **_kw):
        return "Here is what the sources say."

    _patch_turn(monkeypatch, _run_turn)

    await _send(ws_env, {"content": SUBJECT, "mode": "deep_investigation"})

    assert stored_kwargs, "the turn must persist an assistant message"
    assert stale not in repr(stored_kwargs)


@pytest.mark.parametrize("mode", [None, "deep_research", "", "DEEP_INVESTIGATION"])
async def test_any_other_mode_takes_the_ordinary_turn(
    monkeypatch, ws_env, mode  # noqa: F811
):
    """An unrecognised mode degrades to the normal turn: it never refuses the
    message, and it never routes to the orchestrator by accident."""
    ran: list[str] = []

    async def _run_turn(*_a, **_kw):
        ran.append("investigation")
        return ""

    async def _run_general(*_a, **_kw):
        ran.append("general")
        return ("general answer", None)

    _patch_turn(monkeypatch, _run_turn)
    _patch_general_chat(monkeypatch, _run_general)

    frame = {"content": "hello"}
    if mode is not None:
        frame["mode"] = mode
    conn = await _send(ws_env, frame)

    assert ran == ["general"], f"mode={mode!r} must take the ordinary path"
    assert not any(f.get("type") == "error" for f in conn.sent)


async def test_with_the_flag_off_the_mode_takes_the_ordinary_turn(
    monkeypatch, ws_env  # noqa: F811
):
    """``DEEP_INVESTIGATION`` off: the mode is an unknown one, so the
    message is answered by the normal turn and the orchestrator never runs."""
    monkeypatch.setattr(chat_module, "is_feature_enabled", lambda _flag: False)
    ran: list[str] = []

    async def _run_turn(*_a, **_kw):
        ran.append("investigation")
        return ""

    async def _run_general(*_a, **_kw):
        ran.append("general")
        return ("general answer", None)

    _patch_turn(monkeypatch, _run_turn)
    _patch_general_chat(monkeypatch, _run_general)

    conn = await _send(ws_env, {"content": SUBJECT, "mode": "deep_investigation"})

    assert ran == ["general"]
    assert not any(f.get("type") == "error" for f in conn.sent)


# ── A Stop belongs to the turn it interrupted ───────────────────────────────


async def test_a_stop_does_not_survive_into_the_next_investigation(
    monkeypatch, ws_env  # noqa: F811
):
    """Two investigation turns in a row; a cancel consumed during the first
    must not pre-cancel the second.

    The consequence if it were ever wrong is
    silent and expensive: the second run would come back `cancelled` with zero
    sources queried, having been metered and quota-charged, for a turn the
    analyst never cancelled. Nothing about that looks like a bug from outside,
    which is exactly why it needs an assertion rather than an argument.
    """
    events: list[object] = []

    async def _run_turn(_db, *, cancel_event=None, **_kw):
        events.append(cancel_event)
        assert cancel_event is not None
        # The first turn is the one that gets stopped.
        if len(events) == 1:
            cancel_event.set()
        assert not cancel_event.is_set() or len(events) == 1, (
            "a Stop from the previous turn must not be set on this one"
        )
        return "done"

    _patch_turn(monkeypatch, _run_turn)

    await _send(ws_env, {"content": SUBJECT, "mode": "deep_investigation"})
    await _send(ws_env, {"content": "another subject entirely", "mode": "deep_investigation"})

    assert len(events) == 2, "both turns must have run"
    first, second = events
    assert first is not second, (
        "each turn must get its own cancel event — a shared one carries a Stop "
        "into every later investigation of the conversation"
    )
    assert first.is_set() is True
    assert second.is_set() is False, (
        "the second run would otherwise be cancelled before querying a single "
        "source, after being metered and charged"
    )


# ── Confirming nothing is not the same as confirming nothing in particular ──


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # Absent: no selection was made, so the server recommends.
        ({}, None),
        # Explicitly empty: an answer, and it is "none of these".
        ({"source_pack_slugs": []}, []),
        ({"source_pack_slugs": ["web-general"]}, ["web-general"]),
        # Stripped, and blanks dropped (the request model refuses non-strings).
        ({"source_pack_slugs": [" web-general ", ""]}, ["web-general"]),
        ({"source_pack_slugs": None}, None),
    ],
)
async def test_what_the_client_confirmed_reaches_the_turn_unchanged(
    monkeypatch, ws_env, payload, expected  # noqa: F811
):
    """`[]` collapsing to `None` meant an explicit "none of these" was widened
    to every recommended pack — the opposite of the request, on the feature
    whose premise is that a run never queries what nobody confirmed."""
    seen: list = []

    async def _run_turn(_db, *, source_pack_slugs=None, **_kw):
        seen.append(source_pack_slugs)
        return "done"

    _patch_turn(monkeypatch, _run_turn)
    await _send(ws_env, {"content": SUBJECT, "mode": "deep_investigation", **payload})

    assert seen == [expected]


async def test_a_confirmed_selection_is_bounded(monkeypatch, ws_env):  # noqa: F811
    """The picker offers a handful."""
    seen: list = []

    async def _run_turn(_db, *, source_pack_slugs=None, **_kw):
        seen.append(source_pack_slugs)
        return "done"

    _patch_turn(monkeypatch, _run_turn)
    await _send(
        ws_env,
        {
            "content": SUBJECT,
            "mode": "deep_investigation",
            "source_pack_slugs": [f"pack-{i}" for i in range(200)],
        },
    )

    assert len(seen[0]) == chat_module.MAX_CONFIRMED_PACKS


@pytest.mark.asyncio
async def test_repeated_slugs_are_confirmed_once(monkeypatch, ws_env):  # noqa: F811
    """The run persists these slugs as its record of what the user ticked, and
    nobody ticks the same pack twice. De-duplicated before the cap, too, so a
    repeat cannot push a distinct slug out of the selection."""
    seen: list = []

    async def _run_turn(db, *, source_pack_slugs=None, **_kw):
        seen.append(source_pack_slugs)
        return "done"

    _patch_turn(monkeypatch, _run_turn)
    await _send(
        ws_env,
        {
            "content": SUBJECT,
            "mode": "deep_investigation",
            "source_pack_slugs": ["web-general", " web-general ", "other"],
        },
    )

    assert seen[0] == ["web-general", "other"], (
        "a repeated slug was confirmed twice in the audit record"
    )
