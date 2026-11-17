"""The files a replayed call produced stay readable after a run resume.

A run resumed on a new worker replays the completed rounds of the attempt it
lost. The checkpoint keeps each result's text, never the ``files`` it
carried, and ``load_run_generated_files`` only finds the rows stamped with the
run: a frame, a contact sheet or a render's captions file a tool stored for
the run's user without that stamp answered "no file attached" on
``read_file_content``. The resume now re-attaches every file a replayed result
names that the run itself or its acting user owns, with the same id.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

from app.llm.base import ChatMessage, FileRef
from app.models.file_attachment import FileAttachment
from app.services import assignment_engine, assignment_run_resume
from app.tools.base import FileContext, ToolCall


def _round(call_id: str, name: str, result: str) -> list[ChatMessage]:
    return [
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id=call_id, name=name, arguments={})]),
        ChatMessage(role="tool", content=result, tool_call_id=call_id, name=name),
    ]


def _file(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        user_id=None,
        assignment_run_id=None,
        template_container_id=None,
        source="video_studio_take",
        filename="take-contact-sheet.png",
        content_type="image/png",
        size=1024,
        s3_object_name="video-studio/u/sheet.png",
        description=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FilesDB:
    """Answers a ``FileAttachment`` lookup by applying the statement's own filter to stub rows.

    The statement is compiled with its literal values, so the scope it states
    (ids, no template asset, the run or the acting user) is the one tested.
    """

    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        assert statement.column_descriptions[0]["entity"] is FileAttachment
        params = statement.compile().params
        ids = {str(v) for value in params.values() if isinstance(value, list | tuple) for v in value}
        scalars = {str(v) for v in params.values() if not isinstance(v, list | tuple)}
        kept = [
            row
            for row in self.rows
            if str(row.id) in ids
            and row.template_container_id is None
            and (str(row.assignment_run_id) in scalars or str(row.user_id) in scalars)
        ]
        return _Result(kept)


def test_ids_are_read_from_the_replayed_results_only_in_order_each_once():
    sheet, captions, argument = (str(uuid.uuid4()) for _ in range(3))
    history = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="a", name="save_video_frames", arguments={"file_id": argument})],
        ),
        ChatMessage(
            role="tool",
            content=f"Contact sheet [[FILE:{sheet}]] and frame file-{sheet.upper()}",
            tool_call_id="a",
            name="save_video_frames",
        ),
        *_round("b", "get_video_jobs", f'{{"captions_file_id": "{captions}"}}'),
        ChatMessage(role="user", content=f"resume note naming {argument}"),
    ]

    assert assignment_run_resume.replayed_file_ids(history) == [sheet, captions]


@pytest.mark.asyncio
async def test_a_file_a_replayed_call_produced_for_the_user_is_attached_again_with_its_id():
    """Acceptance 1 and 3: a contact sheet stored without the run's stamp, same id after the resume."""
    user_id = uuid.uuid4()
    run = SimpleNamespace(id=uuid.uuid4(), user_id=user_id)
    sheet = _file(user_id=user_id)
    db = _FilesDB([sheet])
    history = _round("a", "save_video_frames", f"Contact sheet: [[FILE:{sheet.id}]]")

    rows = await assignment_run_resume.replayed_files(db, run, history, exclude_ids=set(), user_id=user_id)

    assert rows == [sheet]
    context = FileContext()
    for row in rows:
        context.add(FileRef.from_attachment(row))
    assert context.get(str(sheet.id)) is not None, "read_file_content finds the sheet by the id it had before"


@pytest.mark.asyncio
async def test_a_render_s_captions_file_is_attached_again():
    """Acceptance 2: the ``captions_file_id`` of a finished render named in a replayed result."""
    user_id = uuid.uuid4()
    run = SimpleNamespace(id=uuid.uuid4(), user_id=None)
    captions = _file(user_id=user_id, filename="scene.vtt", content_type="text/vtt", source="video_studio_render")
    db = _FilesDB([captions])
    history = _round("r", "get_video_jobs", f'{{"status": "done", "captions_file_id": "{captions.id}"}}')

    rows = await assignment_run_resume.replayed_files(db, run, history, exclude_ids=set(), user_id=user_id)

    assert rows == [captions], "a company run acts as its agent's owner: that user's captions file"


@pytest.mark.asyncio
async def test_another_user_s_file_a_result_quotes_is_never_attached():
    user_id = uuid.uuid4()
    run = SimpleNamespace(id=uuid.uuid4(), user_id=user_id)
    foreign = _file(user_id=uuid.uuid4())
    asset = _file(user_id=user_id, template_container_id=uuid.uuid4())
    history = _round("a", "web_fetch", f"Page mentions {foreign.id} and {asset.id}")

    rows = await assignment_run_resume.replayed_files(
        _FilesDB([foreign, asset]), run, history, exclude_ids=set(), user_id=user_id
    )

    assert rows == []


@pytest.mark.asyncio
async def test_a_purged_file_stays_unreadable_and_a_failed_lookup_costs_only_the_files():
    user_id = uuid.uuid4()
    run = SimpleNamespace(id=uuid.uuid4(), user_id=user_id)
    history = _round("a", "save_video_frames", f"Sheet {uuid.uuid4()}")

    assert await assignment_run_resume.replayed_files(_FilesDB([]), run, history, exclude_ids=set(), user_id=user_id) == []

    class _Broken:
        async def execute(self, statement):
            raise RuntimeError("database away")

    assert await assignment_run_resume.replayed_files(_Broken(), run, history, exclude_ids=set(), user_id=user_id) == []


@pytest.mark.asyncio
async def test_no_id_in_the_replay_or_only_trigger_files_means_no_query():
    run = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    trigger = str(uuid.uuid4())
    db = _FilesDB([])

    assert await assignment_run_resume.replayed_files(db, run, _round("a", "web_fetch", "no ids"), exclude_ids=set()) == []
    assert (
        await assignment_run_resume.replayed_files(
            db, run, _round("b", "read_file_content", f"read {trigger}"), exclude_ids={trigger}
        )
        == []
    )
    assert db.statements == []


def test_the_resume_registers_the_replayed_files_as_the_acting_user():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    resume = source.index("if _run_replay is not None:")
    replayed = source.index("assignment_run_resume.replayed_files(", resume)
    assert replayed - resume < 1200, "the replayed files are registered with the replay"
    assert "user_id=effective_user_id," in source[replayed : replayed + 300]
