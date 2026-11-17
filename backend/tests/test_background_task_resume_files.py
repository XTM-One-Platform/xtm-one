"""A resumed background task reads again the files its lost attempt produced.

The platform re-attaches the files a replayed call named on an assignment
run's resume. A background task resumes on another path
(``sub_agent._execute_sub_agent_task``): its stopping worker hands it back
(graceful SIGTERM, a rollout) or the engine loses its lease, and the next
attempt replays the checkpointed rounds. The checkpoint keeps each result's
text, never its files, and the new attempt's ``FileContext`` was rebuilt from
the parent's files only: a contact sheet the lost attempt wrote answered
"no file attached". The resumed attempt now registers, with the same ids, the
files the lost attempts recorded and every file a replayed result names, read
as the task's user only.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import worker_shutdown
from app.llm.base import ChatMessage, FileRef
from app.services import background_checkpoint, sub_agent, sub_agent_files
from app.services.background_checkpoint import TaskReplay
from app.services.sub_agent_files import (
    RecordingFileContext,
    resumed_file_refs,
)
from app.services.turn_checkpoint import ResumedWork
from app.tools.base import FileContext, ToolCall


@pytest.fixture(autouse=True)
def _no_stop_request(monkeypatch):
    monkeypatch.setattr(worker_shutdown, "_request", None)


def _round(call_id: str, name: str, result: str) -> list[ChatMessage]:
    return [
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id=call_id, name=name, arguments={})]),
        ChatMessage(role="tool", content=result, tool_call_id=call_id, name=name),
    ]


def _row(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        user_id=None,
        assignment_run_id=None,
        template_container_id=None,
        source="video_studio_take",
        filename="take - contact sheet.png",
        content_type="image/png",
        size=1024,
        s3_object_name="video-studio/u/sheet.png",
        description=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _rows_of(rows, calls: list):
    """``load_attachment_rows`` over stub *rows*: by id, in the order asked, scoped to *user_id*."""

    async def _load(file_ids, *, db=None, conversation_id=None, user_id=None):
        calls.append({"ids": list(file_ids), "user_id": user_id})
        by_id = {str(row.id): row for row in rows if user_id is None or str(row.user_id) == str(user_id)}
        return [by_id[i] for i in file_ids if i in by_id]

    return _load


async def test_the_carried_and_the_replayed_files_come_back_with_their_ids(monkeypatch):
    user_id = uuid.uuid4()
    recorded = _row(user_id=user_id, filename="report.md", source="generate_file", content_type="text/markdown")
    sheet = _row(user_id=user_id)
    calls: list = []
    monkeypatch.setattr(sub_agent_files, "load_attachment_rows", _rows_of([recorded, sheet], calls))
    history = _round("a", "save_video_frames", f"Contact sheet: [[FILE:{sheet.id}]]")

    refs = await resumed_file_refs([str(recorded.id)], history, user_id=user_id)

    assert [ref.file_id for ref in refs] == [str(recorded.id), str(sheet.id)]
    assert calls == [{"ids": [str(recorded.id), str(sheet.id)], "user_id": user_id}], "one user-scoped lookup"


async def test_another_user_s_file_or_a_template_asset_is_never_attached(monkeypatch):
    user_id = uuid.uuid4()
    foreign = _row(user_id=uuid.uuid4())
    asset = _row(user_id=user_id, template_container_id=uuid.uuid4())
    monkeypatch.setattr(sub_agent_files, "load_attachment_rows", _rows_of([foreign, asset], []))
    history = _round("a", "web_fetch", f"The page quotes {foreign.id} and {asset.id}")

    assert await resumed_file_refs([], history, user_id=user_id) == []


async def test_nothing_to_look_up_means_no_query_and_a_failed_lookup_costs_only_the_files(monkeypatch):
    calls: list = []
    monkeypatch.setattr(sub_agent_files, "load_attachment_rows", _rows_of([], calls))
    assert await resumed_file_refs([], _round("a", "web_fetch", "no ids here"), user_id=uuid.uuid4()) == []
    assert calls == []

    async def _broken(*args, **kwargs):
        raise RuntimeError("database away")

    monkeypatch.setattr(sub_agent_files, "load_attachment_rows", _broken)
    history = _round("a", "save_video_frames", f"Sheet {uuid.uuid4()}")
    assert await resumed_file_refs([], history, user_id=uuid.uuid4()) == []


def test_the_resumed_files_are_readable_and_inherited_never_counted_as_this_attempt_s_output():
    parent_file = FileRef.from_attachment(_row(filename="brief.pdf", content_type="application/pdf"))
    sheet = FileRef.from_attachment(_row())
    parent = FileContext.from_file_refs([parent_file])

    context = RecordingFileContext.from_parent(
        parent, task_id=str(uuid.uuid4()), carried_ids=[sheet.file_id], resumed=[sheet, parent_file]
    )

    assert context.get(sheet.file_id) is not None, "read_file_content finds the sheet by the id it had"
    assert context.get(parent_file.file_id) is not None
    assert len(context.list_files()) == 2, "a file both handed and resumed is registered once"
    assert sheet.file_id in context.inherited_ids
    assert context.record() == [sheet.file_id], "the carried record names it once, as before the resume"


async def test_a_task_handed_back_by_its_stopping_worker_reads_its_contact_sheet_again(monkeypatch):
    """The graceful SIGTERM leg: hand-back recorded, next attempt replays, the sheet is in its context."""
    from tests.test_background_edition_gate import (
        ENTERPRISE,
        _patch_task_collaborators,
        _pin,
        _task_row,
        _TaskSession,
    )

    _pin(monkeypatch, **ENTERPRISE)
    monkeypatch.setattr("app.services.quota.quota_store_out", AsyncMock(return_value=False))
    monkeypatch.setattr(sub_agent, "credit_lost_attempts", AsyncMock())
    task = _task_row()
    sheet = _row(user_id=task.user_id)
    task.status = "running"
    task.metadata_json = background_checkpoint.handed_back({}, attempt=1, signal="SIGTERM")
    target = SimpleNamespace(id=uuid.uuid4(), name="Video reviewer", handover_agent_ids=[])
    monkeypatch.setattr("app.core.database.async_session", lambda: _TaskSession([task, target, task]))
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(sub_agent_files, "load_attachment_rows", _rows_of([sheet], []))
    collaborators = _patch_task_collaborators(monkeypatch)
    rounds = [
        *_round("a", "save_video_frames", f"Contact sheet of 3 frames: [[FILE:{sheet.id}]]"),
        *_round("b", "wait", "Waited 60 s."),
    ]
    replay = TaskReplay(
        resumed=ResumedWork(messages=rounds, found=True),
        history=[*rounds, ChatMessage(role="assistant", content="[Resumed: ...]")],
        turn_id="attempt-1",
    )
    monkeypatch.setattr(background_checkpoint, "load_task_replay", AsyncMock(return_value=replay))
    monkeypatch.setattr(background_checkpoint, "discard_task_checkpoint", AsyncMock())

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(target.id),
        task_description="Review the video frames",
        timeout_seconds=600,
        attempt=2,
    )

    assert outcome.succeeded
    turn = collaborators.run_agent_chat.await_args
    assert turn.args[3] == replay.history
    file_context = turn.kwargs["file_context"]
    assert file_context.get(str(sheet.id)) is not None, "the resumed turn reads the sheet by its id"
    assert str(sheet.id) in file_context.inherited_ids, "read again, never counted as this attempt's output"
