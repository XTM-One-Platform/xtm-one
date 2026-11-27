"""#2689: a resumed turn keeps a large cut result reachable instead of "not kept".

A result over ``MAX_REPLAYED_RESULT_CHARS`` was kept as a file only by the
write a stop or a hand-back makes. A worker that died before that write
landed left the replay with its first ``REPLAY_HEAD_CHARS`` characters and
"the rest was not kept", with no next step: the resumed run reported the data
as unavailable. Now a turn with an acting user keeps the whole text in its
checkpoint from its first write, the resume turns it into a file, and a result
that still has no file names the call that returned it and how to read the rest.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from app.core import worker_shutdown
from app.llm.base import ChatMessage
from app.services import background_checkpoint, turn_checkpoint
from app.services.background_checkpoint import TaskCheckpoint, load_task_replay
from app.services.turn_checkpoint import ResumedWork, resume_note
from app.tools.base import ToolCall

TASK = "5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a"
USER = "11111111-2222-4333-8444-555555555555"
CONV = "99999999-8888-4777-8666-555555555555"
RESULT = "".join(f"row {i:05d}: value within the bound\n" for i in range(1_000))


def _round(call_id: str, result: str, arguments: dict | None = None) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call_id, name="list_github_prs", arguments=arguments or {"repo": "a/b"})],
        ),
        ChatMessage(role="tool", content=result, tool_call_id=call_id, name="list_github_prs"),
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


@pytest.fixture
def kept(monkeypatch):
    files: list[dict] = []

    async def _persist(**kwargs):
        files.append(kwargs)
        return SimpleNamespace(id=uuid.UUID(int=30_000 + len(files)))

    monkeypatch.setattr("app.services.builtin_tools._shared._persist_generated_file", _persist)
    return files


async def _settle(checkpoint) -> None:
    for _ in range(5):
        await asyncio.sleep(0)
    if checkpoint._task is not None:
        await checkpoint._task


async def test_a_worker_dead_before_its_stop_write_still_resumes_with_a_file(redis, kept):
    """No stop, no hand-back: the periodic write alone lets the resume keep the whole result as a file."""
    assert len(RESULT) > 12_000
    checkpoint = TaskCheckpoint(uuid.UUID(TASK), attempt=1, user_id=USER, files_conversation_id=CONV)
    checkpoint.offer(_round("a", RESULT))
    await _settle(checkpoint)
    # The worker dies here: no stop listener ran, no flushing close.
    assert kept == [], "a turn that ends normally creates no file"
    stored = json.loads(redis.store[background_checkpoint.checkpoint_key(TASK)])
    (tool_entry,) = [e for e in stored["entries"] if e["role"] == "tool"]
    assert tool_entry["whole"] == RESULT and tool_entry["spill"]["file_id"] is None

    replay = await load_task_replay(TASK, conversation_id=CONV, user_id=USER)
    (file,) = kept
    assert file["raw_bytes"].decode("utf-8") == RESULT
    assert file["conversation_id"] == CONV and file["source"] == turn_checkpoint.SPILL_SOURCE
    file_id = str(uuid.UUID(int=30_001))
    (tool,) = [m for m in replay.history if m.role == "tool"]
    assert "not kept" not in tool.content
    assert f'read_file_content(file_id="{file_id}")' in tool.content
    assert len(tool.content) < turn_checkpoint.REPLAY_HEAD_CHARS + 1_000, "the replay stays bounded"
    note = replay.history[-1].content
    assert f"conversation file {file_id}" in note and "not kept" not in note
    # The resumed attempt's own checkpoint keeps the file, not the whole text a second time.
    (resumed_entry,) = [e for e in replay.resumed.entries if e.get("role") == "tool"]
    assert "whole" not in resumed_entry and resumed_entry["spill"]["file_id"] == file_id


async def test_the_resumed_attempt_keeps_no_second_file(redis, kept):
    first = TaskCheckpoint(uuid.UUID(TASK), attempt=1, user_id=USER, files_conversation_id=CONV)
    first.offer(_round("a", RESULT))
    await _settle(first)
    replay = await load_task_replay(TASK, conversation_id=CONV, user_id=USER)
    second = TaskCheckpoint(
        uuid.UUID(TASK), attempt=2, resumed=replay.resumed, user_id=USER, files_conversation_id=CONV
    )
    second.offer(_round("b", "small"))
    await second.aclose(flush=True)
    assert len(kept) == 1
    stored = json.loads(redis.store[background_checkpoint.checkpoint_key(TASK)])
    (tool_entry, _) = [e for e in stored["entries"] if e["role"] == "tool"]
    assert tool_entry["spill"]["file_id"] == str(uuid.UUID(int=30_001)) and "whole" not in tool_entry


async def test_without_a_user_the_cut_names_the_call_to_make_again(redis, kept):
    checkpoint = TaskCheckpoint(uuid.UUID(TASK), attempt=1)
    checkpoint.offer(_round("a", RESULT, {"repo": "a/b", "count": 30}))
    await checkpoint.aclose(flush=True)
    assert kept == []
    stored = json.loads(redis.store[background_checkpoint.checkpoint_key(TASK)])
    assert all("whole" not in e for e in stored["entries"]), "no user: nothing kept to turn into a file"
    replay = await load_task_replay(TASK)
    (tool,) = [m for m in replay.history if m.role == "tool"]
    assert 'call list_github_prs({"repo": "a/b", "count": 30}) again' in tool.content
    assert "narrower page" in tool.content
    note = replay.history[-1].content
    assert "kept in no file" in note and "list_github_prs(" in note
    assert "call the tool again" in note
    assert "-> not kept" not in note


async def test_a_failed_file_write_keeps_the_whole_text_for_the_next_resume(redis, monkeypatch):
    async def _fails(**kwargs):
        raise RuntimeError("storage down")

    monkeypatch.setattr("app.services.builtin_tools._shared._persist_generated_file", _fails)
    checkpoint = TaskCheckpoint(uuid.UUID(TASK), attempt=1, user_id=USER, files_conversation_id=CONV)
    checkpoint.offer(_round("a", RESULT))
    await _settle(checkpoint)
    replay = await load_task_replay(TASK, conversation_id=CONV, user_id=USER)
    (tool,) = [m for m in replay.history if m.role == "tool"]
    assert "call list_github_prs(" in tool.content, "no file: the way back is the call"
    (entry,) = [e for e in replay.resumed.entries if e.get("role") == "tool"]
    assert entry["whole"] == RESULT, "the next resume tries again"


def test_the_whole_texts_kept_stay_bounded():
    head = {"role": "tool", "content": "h" * 100, "tool_call_id": "a", "whole": "x" * 300}
    newer = {"role": "tool", "content": "h" * 100, "tool_call_id": "b", "whole": "y" * 300}
    out = turn_checkpoint._wholes_within([head, newer], limit=600)
    assert "whole" not in out[0] and out[1]["whole"] == "y" * 300, "the newest whole text is kept first"
    assert turn_checkpoint._wholes_within([head], limit=100_000) == [head]


def test_resume_note_separates_kept_results_from_the_ones_to_read_again():
    call = ToolCall(id="c1", name="get_github_file", arguments={"path": "x"})
    resumed = ResumedWork(
        found=True,
        messages=[ChatMessage(role="assistant", content="", tool_calls=[call])],
        spilled=[
            {"call_id": "c1", "name": "get_github_file", "chars": 40_000, "file_id": "f-1"},
            {
                "call_id": "c2",
                "name": "get_github_file",
                "chars": 30_000,
                "file_id": None,
                "call": 'get_github_file({"path": "y"})',
            },
        ],
    )
    note = resume_note(resumed)
    assert note is not None
    assert "1 result(s) are replayed cut and kept whole as conversation files (read_file_content)" in note.content
    assert 'get_github_file({"path": "y"}) (30,000 characters)' in note.content
    assert "not kept" not in note.content
