"""A file a long result names past the replayed head stays readable after a resume.

The replay of an interrupted turn keeps the first ``REPLAY_HEAD_CHARS`` of a
long result. A resumed run attaches again the files its replayed results name
(``assignment_run_resume.replayed_file_ids``, then ``replayed_files``, which
looks them up among the run's rows and its acting user's only). A report the
result named near its end was cut away with the text: the resumed turn's
``read_file_content`` on it answered "no file ... is attached to this
conversation". The cut note now names the ids of the part not shown again.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.llm.base import ChatMessage
from app.services import assignment_run_resume, background_checkpoint, turn_checkpoint
from app.services.background_checkpoint import TaskCheckpoint, load_task_replay
from app.tools.base import ToolCall

TASK = "7e1a2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5c"
USER = "11111111-2222-4333-8444-555555555555"
CONV = "99999999-8888-4777-8666-555555555555"
REPORT = "a0b1c2d3-e4f5-4a6b-8c7d-9e0f1a2b3c4d"
HEAD_FILE = "0f0e0d0c-0b0a-4908-8706-050403020100"


def _long_result() -> str:
    body = "".join(f"check {i:05d}: ok, measured within the bound\n" for i in range(1_000))
    return (
        f"Contact sheet stored: file_id {HEAD_FILE}\n"
        + body
        + f"\nFull quality report: file_id {REPORT} - read_file_content opens it.\n"
    )


def _round(call_id: str, result: str) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call_id, name="get_video_jobs", arguments={"job": 1})],
        ),
        ChatMessage(role="tool", content=result, tool_call_id=call_id, name="get_video_jobs"),
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


@pytest.fixture
def kept(monkeypatch):
    files: list[dict] = []

    async def _persist(**kwargs):
        files.append(kwargs)
        return SimpleNamespace(id=uuid.UUID(int=20_000 + len(files)))

    monkeypatch.setattr("app.services.builtin_tools._shared._persist_generated_file", _persist)
    return files


def test_cut_named_ids_names_only_what_the_head_lost():
    content = _long_result()
    assert REPORT not in content[: turn_checkpoint.REPLAY_HEAD_CHARS]
    assert turn_checkpoint.cut_named_ids(content) == [REPORT]


def test_cut_named_ids_keeps_the_last_ones_and_skips_the_excluded():
    head = "x" * turn_checkpoint.REPLAY_HEAD_CHARS
    ids = [str(uuid.UUID(int=i + 1)) for i in range(turn_checkpoint.MAX_CUT_NAMED_IDS + 5)]
    content = head + " ".join(ids) + f" {ids[0].upper()}"
    named = turn_checkpoint.cut_named_ids(content, exclude=[ids[-1]])
    assert len(named) == turn_checkpoint.MAX_CUT_NAMED_IDS
    assert ids[-1] not in named, "the spill file is named by the note already"
    assert named[-1] == ids[0], "an id named again late counts at its last place"
    assert REPORT not in named


def test_a_short_result_is_not_touched():
    assert turn_checkpoint.cut_named_ids("short " + REPORT) == []


@pytest.mark.parametrize("user", [USER, None])
async def test_a_report_named_past_the_head_is_attached_again_on_resume(redis, kept, user):
    checkpoint = TaskCheckpoint(uuid.UUID(TASK), attempt=1, user_id=user, files_conversation_id=CONV)
    checkpoint.offer(_round("a", _long_result()))
    await checkpoint.aclose(flush=True)

    replay = await load_task_replay(TASK)
    (tool,) = [m for m in replay.history if m.role == "tool"]
    assert len(tool.content) < turn_checkpoint.REPLAY_HEAD_CHARS + 1_000, "the replay stays bounded"
    assert REPORT in tool.content, "the cut note names the report again"
    ids = assignment_run_resume.replayed_file_ids(replay.history)
    assert REPORT in ids and HEAD_FILE in ids
    if user is not None:
        spill_id = str(uuid.UUID(int=20_001))
        assert spill_id in ids
        assert tool.content.count(spill_id) == 2, "the spill file is not named a third time"


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


async def test_the_lookup_still_scopes_the_named_report_to_the_run_and_its_user(redis, kept):
    """The ids the note adds go through the same lookup: the run's rows or its acting user's, no template asset."""
    checkpoint = TaskCheckpoint(uuid.UUID(TASK), attempt=1, user_id=USER, files_conversation_id=CONV)
    checkpoint.offer(_round("a", _long_result()))
    await checkpoint.aclose(flush=True)
    replay = await load_task_replay(TASK)

    statements: list[object] = []

    class _Db:
        async def execute(self, statement):
            statements.append(statement)
            return _Rows([SimpleNamespace(id=uuid.UUID(REPORT))])

    run = SimpleNamespace(id=uuid.uuid4())
    rows = await assignment_run_resume.replayed_files(
        _Db(), run, replay.history, exclude_ids=set(), user_id=uuid.UUID(USER)
    )
    assert [str(r.id) for r in rows] == [REPORT]
    (statement,) = statements
    compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
    assert "file_attachments.user_id" in compiled and "file_attachments.assignment_run_id" in compiled
    assert "template_container_id IS NULL" in compiled
    ids = statement.compile().params
    assert any(uuid.UUID(REPORT) in (v if isinstance(v, list | tuple) else [v]) for v in ids.values())
