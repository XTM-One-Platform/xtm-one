"""A resumed turn names the recording an interrupted ``record_browser_demo`` created (#10073).

``video_studio.resume_landed`` reads ``video_takes``: a recording of the same
user and conversation, created within the same-take window, with the call's
name, is named in the resume note with its id and status. ``same_take``
answers a queued or recording take with the same submitted script whatever
its name, and ``record_browser_demo`` is replay-safe, so a repeat after a
resume never queues a second take.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.llm.base import ChatMessage
from app.services.turn_checkpoint import ResumedWork, in_flight_calls, in_flight_writes, resume_note
from app.services.video_studio import resume_landed
from app.tools.base import ToolCall

_NOW = datetime(2026, 11, 8, 13, 0, tzinfo=UTC)


def _in_flight(**arguments: object) -> list[dict]:
    return in_flight_calls(
        [
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="r", name="record_browser_demo", arguments=dict(arguments))],
            )
        ]
    )


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self.execute = AsyncMock(return_value=_Result(rows))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _take(name: str, status: str = "running") -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), name=name, status=status, created_at=_NOW)


async def test_the_resume_note_names_the_recording_the_cut_call_created():
    take = _take("Fleet map demo")
    calls = _in_flight(name="Fleet map demo", from_recording_id="8a3f2b1c", fps=30)

    judged = await resume_landed.judge_for_resume(
        calls, uuid.uuid4(), uuid.uuid4(), now=_NOW, session_factory=lambda: _Session([take])
    )

    assert judged == 1
    assert calls[0]["landed"] is True and str(take.id) in calls[0]["outcome"]
    note = resume_note(ResumedWork(found=True, in_flight=calls))
    assert note is not None
    assert f"it created recording {take.id} (running" in note.content
    assert "do not record it again" in note.content


async def test_no_recording_with_its_name_says_it_created_none():
    calls = _in_flight(name="Fleet map demo", steps=[{"action": "goto", "url": "/"}])

    judged = await resume_landed.judge_for_resume(
        calls, None, uuid.uuid4(), now=_NOW, session_factory=lambda: _Session([_take("Another take")])
    )

    assert judged == 1
    assert calls[0]["landed"] is False and "created no recording" in calls[0]["outcome"]


async def test_a_call_without_a_name_matches_the_default_or_the_rerecorded_name():
    assert resume_landed.name_matches({}, "Demo recording") is True
    assert resume_landed.name_matches({"from_recording_id": "8a3f2b1c"}, "Fleet map (re-recorded)") is True
    assert resume_landed.name_matches({"from_recording_id": "8a3f2b1c"}, "Fleet map") is False
    # A name the replay cut cannot be told.
    assert resume_landed.name_matches({"name": "x... [cut for the replay: 9 more characters]"}, "x") is None


async def test_nothing_is_read_without_a_user_or_a_recording_call():
    factory = AsyncMock()
    assert await resume_landed.judge_for_resume(_in_flight(name="a"), None, None, session_factory=factory) == 0
    other = in_flight_calls(
        [ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="b", name="render_video", arguments={})])]
    )
    assert await resume_landed.judge_for_resume(other, None, uuid.uuid4(), session_factory=factory) == 0
    factory.assert_not_called()


async def test_a_read_failure_judges_nothing():
    calls = _in_flight(name="a")

    def _broken():
        raise RuntimeError("database gone")

    assert await resume_landed.judge_for_resume(calls, None, uuid.uuid4(), session_factory=_broken) == 0
    assert "landed" not in calls[0]


async def test_same_take_answers_a_running_take_asked_again_under_another_name():
    from app.services.builtin_tools.video_studio import same_take

    submitted = {"steps": [{"action": "goto", "url": "/"}], "params": {"fps": 30}}
    running = SimpleNamespace(id=uuid.uuid4(), name="Take 1", submitted_script=submitted, status="running")
    session = SimpleNamespace(execute=AsyncMock(return_value=_Result([running])))
    found = await same_take(session, user_id=uuid.uuid4(), conversation_id=None, name="Take 2", submitted=submitted)
    assert found is running

    # A finished take is answered under its own name only: a new take after it stays possible.
    ready = SimpleNamespace(id=uuid.uuid4(), name="Take 1", submitted_script=submitted, status="ready")
    session.execute = AsyncMock(return_value=_Result([ready]))
    assert (
        await same_take(session, user_id=uuid.uuid4(), conversation_id=None, name="Take 2", submitted=submitted) is None
    )


def test_record_browser_demo_is_replay_safe_so_its_repeat_is_not_refused():
    from app.services.builtin_tools.video_studio import build_video_studio_tools

    definitions, _ = build_video_studio_tools(user_id="")
    record = next(d for d in definitions if d.name == "record_browser_demo")
    assert record.replay_safe is True
    calls = _in_flight(name="a", fps=30)
    messages = [ChatMessage(role="assistant", content="", in_flight=calls)]
    assert in_flight_writes(messages, replay_safe={"record_browser_demo"}) == {}
