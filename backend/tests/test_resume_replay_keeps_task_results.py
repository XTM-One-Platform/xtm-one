"""A resumed background task or assignment run reads every replayed tool result in full.

Before, a task's or a run's checkpoint had no acting user: a hand-back replayed
a result over ``MAX_REPLAYED_RESULT_CHARS`` as its first ``REPLAY_HEAD_CHARS``
characters, "the rest was not kept", and the resumed turn could only run the
tool again. The flushing close now keeps it whole as a file of that user, in
the scope the resumed attempt reads files from, and the replay names it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from app.core import worker_shutdown
from app.llm.base import ChatMessage
from app.services import assignment_run_resume, background_checkpoint, turn_checkpoint
from app.services.background_checkpoint import TaskCheckpoint, load_task_replay
from app.tools.base import ToolCall

TASK = "7e1a2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5b"
USER = "11111111-2222-4333-8444-555555555555"
CONV = "99999999-8888-4777-8666-555555555555"
RESULT = "".join(f"frame-{i:05d} crop file {uuid.UUID(int=i)}\n" for i in range(800))


def _round(call_id: str, result: str) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call_id, name="render_video_scene", arguments={"scene": 1})],
        ),
        ChatMessage(role="tool", content=result, tool_call_id=call_id, name="render_video_scene"),
    ]


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(background_checkpoint, "_redis", _client)
    return fake


@pytest.fixture(autouse=True)
def _no_stop_request(monkeypatch):
    monkeypatch.setattr(worker_shutdown, "_request", None)
    monkeypatch.setattr(worker_shutdown, "_stop_listeners", {})


async def _settle(checkpoint) -> None:
    """Let the stop listeners run and the writer they started land."""
    for _ in range(5):
        await asyncio.sleep(0)
    task = checkpoint._task
    if task is not None:
        await task


@pytest.fixture
def kept(monkeypatch):
    files: list[dict] = []

    async def _persist(**kwargs):
        files.append(kwargs)
        return SimpleNamespace(id=uuid.UUID(int=10_000 + len(files)))

    monkeypatch.setattr("app.services.builtin_tools._shared._persist_generated_file", _persist)
    return files


async def test_a_40k_result_is_readable_in_full_after_a_hand_back(redis, kept):
    assert len(RESULT) > 40_000
    checkpoint = TaskCheckpoint(uuid.UUID(TASK), attempt=1, user_id=USER, files_conversation_id=CONV)
    checkpoint.offer(_round("a", RESULT))
    await checkpoint.aclose(flush=True)

    (file,) = kept
    assert file["raw_bytes"].decode("utf-8") == RESULT, "the whole result is kept"
    assert file["user_id"] == USER and file["conversation_id"] == CONV
    assert file["source"] == turn_checkpoint.SPILL_SOURCE
    file_id = str(uuid.UUID(int=10_001))

    replay = await load_task_replay(TASK)
    (tool,) = [m for m in replay.history if m.role == "tool"]
    assert len(tool.content) < turn_checkpoint.REPLAY_HEAD_CHARS + 1_000, "the first-run budget is unchanged"
    assert "the rest was not kept" not in tool.content
    assert f'read_file_content(file_id="{file_id}")' in tool.content
    assert replay.resumed.spilled and replay.resumed.spilled[0]["file_id"] == file_id
    note = replay.history[-1].content
    assert f"conversation file {file_id}" in note and f"{len(RESULT):,} characters" in note


async def test_the_resumed_attempts_flush_keeps_no_second_file(redis, kept):
    first = TaskCheckpoint(uuid.UUID(TASK), attempt=1, user_id=USER, files_conversation_id=CONV)
    first.offer(_round("a", RESULT))
    await first.aclose(flush=True)
    replay = await load_task_replay(TASK)

    second = TaskCheckpoint(
        uuid.UUID(TASK), attempt=2, resumed=replay.resumed, user_id=USER, files_conversation_id=CONV
    )
    second.offer([*replay.resumed.messages, *_round("b", "small")])
    await second.aclose(flush=True)
    assert len(kept) == 1
    stored = json.loads(redis.store[background_checkpoint.checkpoint_key(TASK)])
    assert stored["entries"][1]["spill"]["file_id"] == str(uuid.UUID(int=10_001))


async def test_without_a_user_the_rest_is_still_not_kept(redis, kept):
    checkpoint = TaskCheckpoint(uuid.UUID(TASK), attempt=1)
    checkpoint.offer(_round("a", RESULT))
    await checkpoint.aclose(flush=True)
    assert kept == []
    replay = await load_task_replay(TASK)
    (tool,) = [m for m in replay.history if m.role == "tool"]
    assert "the rest was not kept" in tool.content


async def test_a_run_keeps_its_result_stamped_with_the_run_and_its_secrets_as_markers(redis, kept, monkeypatch):
    monkeypatch.setattr(
        "app.core.redaction.redact_known_secrets",
        lambda data: data.replace("s3cr3t", "[[SECRET_VARIABLE:k]]") if isinstance(data, str) else data,
    )
    run_id = uuid.uuid4()
    checkpoint = assignment_run_resume.checkpoint_for(run_id, replay=None, attempt=1, user_id=USER)
    checkpoint.offer(_round("a", "s3cr3t " + RESULT))
    await checkpoint.aclose(flush=True)
    (file,) = kept
    assert file["assignment_run_id"] == str(run_id) and file["conversation_id"] is None
    assert file["user_id"] == USER
    text = file["raw_bytes"].decode("utf-8")
    assert "s3cr3t" not in text and text.endswith(RESULT)


@pytest.mark.parametrize("kind", ["assignment_run", "background_task"])
async def test_a_stop_keeps_the_result_before_a_take_back_with_no_hand_back(redis, kept, kind):
    """A turn still in a call or a wait when its worker goes is taken back, never flushed: the stop keeps the file."""
    run_id = uuid.uuid4()
    if kind == "assignment_run":
        checkpoint = assignment_run_resume.checkpoint_for(run_id, replay=None, attempt=1, user_id=USER)
        key = assignment_run_resume.checkpoint_id(run_id)
    else:
        checkpoint = TaskCheckpoint(uuid.UUID(TASK), attempt=1, user_id=USER, files_conversation_id=CONV)
        key = TASK
    checkpoint.offer(_round("a", RESULT))
    await _settle(checkpoint)
    assert kept == [], "no stop: a periodic write keeps no file"

    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")
    await _settle(checkpoint)
    # The worker dies here: no aclose, no hand-back. The next attempt reads the checkpoint.
    (file,) = kept
    assert file["raw_bytes"].decode("utf-8").endswith(RESULT)
    file_id = str(uuid.UUID(int=10_001))

    replay = await load_task_replay(key)
    (tool,) = [m for m in replay.history if m.role == "tool"]
    assert "the rest was not kept" not in tool.content
    assert f'read_file_content(file_id="{file_id}")' in tool.content
    assert replay.replayed_calls == 1

    # The second attempt, also taken back after a stop, keeps no second file.
    if kind == "assignment_run":
        second = assignment_run_resume.checkpoint_for(run_id, replay=replay, attempt=2, user_id=USER)
    else:
        second = TaskCheckpoint(
            uuid.UUID(TASK), attempt=2, resumed=replay.resumed, user_id=USER, files_conversation_id=CONV
        )
    # As the agent loop does: only the messages this attempt appended, never the replayed ones it inherited.
    second.offer(_round("b", "small"))
    await _settle(second)
    assert len(kept) == 1
    again = await load_task_replay(key)
    (first_tool, _) = [m for m in again.history if m.role == "tool"]
    assert f'read_file_content(file_id="{file_id}")' in first_tool.content


async def test_a_stop_after_the_close_keeps_nothing(redis, kept):
    checkpoint = TaskCheckpoint(uuid.UUID(TASK), attempt=1, user_id=USER, files_conversation_id=CONV)
    checkpoint.offer(_round("a", RESULT))
    await checkpoint.aclose(flush=False)
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")
    await _settle(checkpoint)
    assert kept == []
    assert worker_shutdown._stop_listeners == {}
