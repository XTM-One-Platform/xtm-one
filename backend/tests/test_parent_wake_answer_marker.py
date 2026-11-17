"""A wake turn's reply names the work that woke it (#11796).

A background task (or an A2A partner's task, or a Video Studio job) that ends
after the turn that started it wakes the conversation: the platform stores a
hidden prompt and runs one turn whose reply nobody asked for in the chat.
Before this, the reply carried no trace of the wake and read as a second
answer to the last user message. Now the reply is stored with
``metadata.wake`` (the source, the task id, the agent), the turn's ``thinking``
ack carries the same note live, and the message API serves it as ``wake``.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.services import parent_wake
from app.services.parent_wake import (
    PARENT_WAKE_SOURCE,
    PARTNER_WAKE_LABEL,
    WAKE_KIND_JOB,
    WAKE_KIND_PARTNER,
    WAKE_KIND_TASK,
    WAKE_META_KEY,
    wake_note,
    wake_start_frame,
)


def test_the_wake_note_names_the_source_the_task_and_its_agent() -> None:
    assert wake_note(task_id="t-1", agent_name="Researcher") == {
        "source": PARENT_WAKE_SOURCE,
        "task_id": "t-1",
        "agent_name": "Researcher",
        "kind": WAKE_KIND_TASK,
    }
    assert wake_note(task_id="t-1", agent_name="")["agent_name"] is None
    assert wake_note(task_id="t-1", agent_name="P", kind=WAKE_KIND_PARTNER)["kind"] == WAKE_KIND_PARTNER


def test_the_wake_opens_on_the_turns_thinking_ack() -> None:
    note = wake_note(task_id="t-1", agent_name="Researcher")
    assert wake_start_frame(note) == {"type": "thinking", WAKE_META_KEY: note}


@pytest.mark.asyncio
async def test_a_wake_turns_reply_carries_the_source_and_the_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    run_turn = AsyncMock(return_value=False)
    monkeypatch.setattr(parent_wake, "_run_parent_wake_turn", run_turn)
    conv = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        channel="web",
        channel_thread_id=None,
    )
    task_id = str(uuid.uuid4())

    await parent_wake._run_turn_for(
        cast(Any, object()),
        cast(Any, conv),
        user_id=str(conv.user_id),
        wake_text="[Platform] Background task finished.",
        task_id=task_id,
        agent_name="Researcher",
    )

    assert run_turn.await_args is not None
    kwargs = run_turn.await_args.kwargs
    note = {
        "source": PARENT_WAKE_SOURCE,
        "task_id": task_id,
        "agent_name": "Researcher",
        "kind": WAKE_KIND_TASK,
    }
    assert kwargs["reply_meta_extra"] == {WAKE_META_KEY: note}
    assert kwargs["start_frame"] == {"type": "thinking", WAKE_META_KEY: note}


def _conversation_row() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        channel="web",
        channel_thread_id=None,
    )


class _Session:
    """A session double whose one query answers the conversation row."""

    def __init__(self, conv: SimpleNamespace) -> None:
        self._conv = conv

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, _query: object) -> SimpleNamespace:
        return SimpleNamespace(scalar_one_or_none=lambda: self._conv)


@pytest.mark.asyncio
async def test_an_a2a_partners_answer_is_stored_as_the_partners_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The partner wake's reply carries kind ``partner``: the chat says the
    partner answered, never that a background task finished."""
    run_turn = AsyncMock(return_value=False)
    monkeypatch.setattr(parent_wake, "_run_parent_wake_turn", run_turn)
    conv = _conversation_row()
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(conv))
    spawned: list[Any] = []
    monkeypatch.setattr(parent_wake, "_spawn_wake", lambda coro, label: spawned.append(coro))

    parent_wake.schedule_partner_wake(
        conversation_id=str(conv.id),
        user_id=str(conv.user_id),
        partner_name="Partner Intake",
        result_text="Received.",
    )
    assert len(spawned) == 1
    await spawned[0]

    assert run_turn.await_args is not None
    kwargs = run_turn.await_args.kwargs
    note = {
        "source": PARENT_WAKE_SOURCE,
        "task_id": PARTNER_WAKE_LABEL,
        "agent_name": "Partner Intake",
        "kind": WAKE_KIND_PARTNER,
    }
    assert kwargs["reply_meta_extra"] == {WAKE_META_KEY: note}
    assert kwargs["start_frame"] == {"type": "thinking", WAKE_META_KEY: note}


@pytest.mark.asyncio
async def test_a_jobs_outcome_is_stored_as_a_job(monkeypatch: pytest.MonkeyPatch) -> None:
    run_turn = AsyncMock(return_value=False)
    monkeypatch.setattr(parent_wake, "_run_parent_wake_turn", run_turn)
    conv = _conversation_row()
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(conv))

    await parent_wake.wake_conversation(
        conversation_id=str(conv.id),
        user_id=str(conv.user_id),
        wake_text="[Platform] The render finished.",
        label="video-job",
    )

    assert run_turn.await_args is not None
    assert run_turn.await_args.kwargs["reply_meta_extra"][WAKE_META_KEY]["kind"] == WAKE_KIND_JOB


def test_the_message_api_serves_the_wake_note_and_ignores_a_foreign_one() -> None:
    from app.api.v1.chat import _wake_payload

    served = _wake_payload(
        {WAKE_META_KEY: {"source": PARENT_WAKE_SOURCE, "task_id": "t-1", "agent_name": "Researcher"}}
    )
    assert served is not None
    assert served.model_dump() == {
        "source": PARENT_WAKE_SOURCE,
        "task_id": "t-1",
        "agent_name": "Researcher",
        "kind": None,
    }
    assert _wake_payload({}) is None
    assert _wake_payload({WAKE_META_KEY: {"source": "other"}}) is None
    assert _wake_payload({WAKE_META_KEY: "text"}) is None
    # Stored as JSON and read back, the note keeps its shape.
    stored = json.loads(json.dumps({WAKE_META_KEY: wake_note(task_id="t", agent_name=None)}))
    read_back = _wake_payload(stored)
    assert read_back is not None
    assert read_back.agent_name is None
    assert read_back.kind == WAKE_KIND_TASK
    partner = _wake_payload({WAKE_META_KEY: wake_note(task_id="a", agent_name="P", kind=WAKE_KIND_PARTNER)})
    assert partner is not None
    assert partner.kind == WAKE_KIND_PARTNER
