"""A file a tool fetched to read is not handed to the user unless the reply names it.

An investigation reads pages, documents and downloads: a ``web_fetch``
response, an integration download, a fetched image, a knowledge-base document
copy. Every one of them is kept as a file of the conversation so the agent can
reuse it, and every one of them lands in the turn's ``FileContext`` - the one
source every reply attaches from. Attaching every file new to the turn put each
of those working files on the reply as a chip nobody asked for. A fetched file
now rides a reply only when the reply's text names it; a generated or uploaded
file is attached as before.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.llm.base import FileRef
from app.tools.base import (
    FileContext,
    deliver_named_files,
    files_for_reply,
    register_fetched_file,
    register_produced_file,
)


def _ref(name: str = "page.html", source: str = "agent_working") -> FileRef:
    return FileRef(
        file_id=str(uuid.uuid4()),
        filename=name,
        content_type="text/html",
        size=120,
        s3_object_name=f"generated/{name}",
        source=source,
    )


# -- the mark ------------------------------------------------------------------


def test_a_fetched_file_is_marked_and_a_generated_one_is_not():
    ctx = FileContext()
    fetched, generated = _ref("page.html"), _ref("report.pdf", source="agent_generated")

    ctx.add_fetched(fetched)
    ctx.add(generated)

    assert ctx.get(fetched.file_id) is fetched
    assert ctx.is_fetched(fetched.file_id)
    assert ctx.is_fetched(f"file-{fetched.file_id}"), "the marker spelling reads the same mark"
    assert not ctx.is_fetched(generated.file_id)


def test_a_file_the_turn_already_held_is_never_marked():
    """An upload or a generated file re-read through a fetch keeps what it was:
    the user asked for it or the agent made it."""
    upload = _ref("brief.pdf", source="chat")
    ctx = FileContext.from_file_refs([upload])

    ctx.add_fetched(upload)

    assert not ctx.is_fetched(upload.file_id)


def test_the_mark_survives_a_plain_re_add():
    """A later tool updating the ref (a retype, a description) does not turn a
    fetched page into a deliverable."""
    page = _ref()
    ctx = FileContext()
    ctx.add_fetched(page)

    ctx.add(page)

    assert ctx.is_fetched(page.file_id)


def test_register_fetched_file_tolerates_no_context_and_a_plain_one():
    page = _ref()
    register_fetched_file(None, page)  # nothing to register in

    added: list[FileRef] = []
    plain = SimpleNamespace(add=added.append)
    register_fetched_file(plain, page)
    assert added == [page], "a context without the mark still receives the file"


def test_a_write_handed_a_fetched_row_makes_it_a_deliverable():
    """The conversation-scoped dedup hands a write the row a fetch already
    stored when the bytes are the same (a prompt saved as ``.txt`` whose text
    an earlier page held). The write is what the user asked for: it is
    delivered without the reply having to name it."""
    page = _ref("prompt.txt")
    ctx = FileContext()
    ctx.add_fetched(page)

    ctx.add_produced(page)

    assert not ctx.is_fetched(page.file_id)
    assert files_for_reply(ctx, [page.file_id], "Here is the prompt.") == [page.file_id]


def test_register_produced_file_tolerates_no_context_and_a_plain_one():
    report = _ref("report.md", "agent_download")
    register_produced_file(None, report)

    added: list[FileRef] = []
    register_produced_file(SimpleNamespace(add=added.append), report)
    assert added == [report]


@pytest.mark.asyncio
async def test_the_incremental_record_writes_a_fetched_row_a_write_was_handed():
    """A delegated task's recorder skips fetched files; once a write makes one
    a deliverable it is written like any file the task produced."""
    from app.services.sub_agent_files import RecordingFileContext

    writes: list[list[str]] = []

    async def _write(_task_id, ids, **_kwargs):
        writes.append(list(ids))
        return True

    page = _ref("prompt.txt")
    ctx = RecordingFileContext(task_id=str(uuid.uuid4()))
    with patch("app.services.sub_agent_files.write_task_result_file_ids", new=_write):
        ctx.add_fetched(page)
        await ctx.drain()
        assert writes == []
        ctx.add_produced(page)
        await ctx.drain()

    assert writes == [[page.file_id]]


# -- the filter ----------------------------------------------------------------


def test_files_for_reply_keeps_produced_files_and_drops_unnamed_fetched_ones():
    generated, named, unnamed = _ref("chart.png", "agent_generated"), _ref("a.pdf"), _ref("b.html")
    ctx = FileContext()
    ctx.add(generated)
    ctx.add_fetched(named)
    ctx.add_fetched(unnamed)
    ids = [generated.file_id, named.file_id, unnamed.file_id]

    reply = f"Here is the source I used: [[FILE:{named.file_id}]]"
    assert files_for_reply(ctx, ids, reply) == [generated.file_id, named.file_id]
    assert files_for_reply(ctx, ids, None) == [generated.file_id]
    assert files_for_reply(ctx, ids, "") == [generated.file_id]


@pytest.mark.parametrize(
    "spelling",
    [
        "[[FILE:{id}]]",
        "[[FILE:file-{id}]]",
        "[download](/api/v1/chat/files/{id}/download)",
        "![chart](file:{id})",
        "[[FILE:{upper}]]",
    ],
)
def test_files_for_reply_reads_every_spelling_of_a_named_file(spelling):
    page = _ref()
    ctx = FileContext()
    ctx.add_fetched(page)

    reply = "See " + spelling.format(id=page.file_id, upper=page.file_id.upper())

    assert files_for_reply(ctx, [page.file_id], reply) == [page.file_id]


def test_files_for_reply_without_a_marking_context_keeps_every_file():
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    assert files_for_reply(None, ids, None) == ids
    assert files_for_reply(SimpleNamespace(list_files=list), ids, None) == ids


# -- the producers ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_tool_artifact_marks_a_fetched_file_and_only_then(monkeypatch):
    from app.services.builtin_tools._shared import persist_tool_artifact

    def _row():
        return SimpleNamespace(
            id=uuid.uuid4(), filename="logo.png", content_type="image/png", size=3,
            s3_object_name="generated/logo.png", source="agent_download",
        )

    rows = [_row(), _row()]
    monkeypatch.setattr(
        "app.services.builtin_tools._shared._persist_generated_file",
        AsyncMock(side_effect=rows),
    )
    ctx = FileContext()
    common = {
        "user_id": str(uuid.uuid4()),
        "filename": "logo.png",
        "raw_bytes": b"png",
        "content_type": "image/png",
        "conversation_id": str(uuid.uuid4()),
        "assignment_run_id": None,
        "file_context": ctx,
    }

    fetched = await persist_tool_artifact(**common, fetched=True)
    generated = await persist_tool_artifact(**common)

    assert ctx.is_fetched(fetched.file_id)
    assert not ctx.is_fetched(generated.file_id)
    assert {ref.file_id for ref in ctx.list_files()} == {fetched.file_id, generated.file_id}


@pytest.mark.asyncio
async def test_persist_tool_artifact_handed_a_fetched_row_delivers_it(monkeypatch):
    from app.services.builtin_tools._shared import persist_tool_artifact

    row = SimpleNamespace(
        id=uuid.uuid4(), filename="chart.png", content_type="image/png", size=3,
        s3_object_name="generated/chart.png", source="agent_working",
    )
    monkeypatch.setattr(
        "app.services.builtin_tools._shared._persist_generated_file", AsyncMock(return_value=row)
    )
    ctx = FileContext()
    common = {
        "user_id": str(uuid.uuid4()), "filename": "chart.png", "raw_bytes": b"png",
        "content_type": "image/png", "conversation_id": str(uuid.uuid4()),
        "assignment_run_id": None, "file_context": ctx,
    }

    fetched = await persist_tool_artifact(**common, fetched=True)
    assert ctx.is_fetched(fetched.file_id)
    produced = await persist_tool_artifact(**common)

    assert produced.file_id == fetched.file_id
    assert not ctx.is_fetched(produced.file_id)


@pytest.mark.asyncio
async def test_generate_file_handed_a_fetched_row_delivers_it(monkeypatch):
    """The DGSI turn shape: the agent read a page holding the text, then saved
    the same text with ``generate_file``. The dedup hands the write the page's
    row; the file the user asked for is still on the reply, under its name."""
    from app.services.builtin_tools import build_generate_file_tool
    from app.tools.base import ToolCall

    page = _ref("prompt.txt")
    row = SimpleNamespace(
        id=uuid.UUID(page.file_id), filename="prompt.txt", content_type="text/plain",
        size=10, s3_object_name="chat/c/prompt.txt", source="agent_working",
    )
    persisted: list[dict] = []

    async def _persist(**kwargs):
        persisted.append(kwargs)
        return row

    monkeypatch.setattr("app.services.builtin_tools.media._persist_generated_file", _persist)
    _defs, executor = build_generate_file_tool(
        db=SimpleNamespace(), user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4())
    )
    ctx = FileContext()
    ctx.add_fetched(page)
    executor.file_context = ctx

    result = await executor.execute_tool(
        ToolCall(id="c1", name="generate_file", arguments={"filename": "prompt.txt", "content": "the prompt"})
    )

    assert result.is_error is False
    assert persisted[0]["match_filename"] is True, "a named write only reuses a row of that name"
    assert not ctx.is_fetched(page.file_id)
    assert files_for_reply(ctx, [page.file_id], "Here is the prompt.") == [page.file_id]


# -- the attach sites ------------------------------------------------------------


def test_produced_file_ids_hands_a_fetched_file_over_only_when_the_answer_names_it():
    from app.services.sub_agent_files import produced_file_ids

    csv, named, unnamed = _ref("q1.csv", "agent_generated"), _ref("a.pdf"), _ref("b.html")
    ctx = FileContext()
    ctx.add(csv)
    ctx.add_fetched(named)
    ctx.add_fetched(unnamed)

    assert produced_file_ids(ctx, set()) == [csv.file_id]
    answer = f"Built from [[FILE:{named.file_id}]]."
    assert produced_file_ids(ctx, set(), answer) == [csv.file_id, named.file_id]


@pytest.mark.asyncio
async def test_the_incremental_record_never_writes_for_a_fetched_file():
    """The copy a dead-lettered task keeps has no answer to read a name from,
    so a fetched file is never in it; a produced one is written as it lands."""
    from app.services.sub_agent_files import RecordingFileContext

    writes: list[list[str]] = []

    async def _write(_task_id, ids, **_kwargs):
        writes.append(list(ids))
        return True

    page, csv = _ref(), _ref("q1.csv", "agent_generated")
    ctx = RecordingFileContext(task_id=str(uuid.uuid4()))
    with patch("app.services.sub_agent_files.write_task_result_file_ids", new=_write):
        ctx.add_fetched(page)
        await ctx.drain()
        assert writes == [], "a fetched page alone writes nothing"
        ctx.add(csv)
        await ctx.drain()

    assert writes == [[csv.file_id]]
    assert ctx.get(page.file_id) is page, "the task still reads what it fetched"


@pytest.mark.asyncio
async def test_record_result_files_reads_the_delivered_answer():
    from app.services import sub_agent
    from app.services.sub_agent_files import RESULT_FILE_IDS_KEY

    csv, named, unnamed = _ref("q1.csv", "agent_generated"), _ref("a.pdf"), _ref("b.html")
    ctx = FileContext()
    ctx.add(csv)
    ctx.add_fetched(named)
    ctx.add_fetched(unnamed)
    task = SimpleNamespace(metadata_json={})

    recorded = await sub_agent._record_result_files(
        task, ctx, set(), answer=f"See [[FILE:{named.file_id}]]"
    )

    assert recorded == [csv.file_id, named.file_id]
    assert task.metadata_json[RESULT_FILE_IDS_KEY] == recorded

    failed = SimpleNamespace(metadata_json={})
    assert await sub_agent._record_result_files(failed, ctx, set()) == [csv.file_id]


@pytest.mark.asyncio
async def test_platform_chat_attaches_a_fetched_file_only_when_the_reply_names_it(monkeypatch):
    from app.api.v1 import platform as platform_api

    generated, page = _ref("report.pdf", "agent_generated"), _ref()
    ctx = FileContext()
    ctx.add(generated)
    ctx.add_fetched(page)

    async def _load(ids, **_kwargs):
        return [
            SimpleNamespace(
                id=uuid.UUID(file_id), filename="f", content_type="application/pdf",
                size=1, source="agent",
            )
            for file_id in ids
        ]

    monkeypatch.setattr("app.services.sub_agent_files.load_attachment_rows", _load)

    rows, _meta = await platform_api._collect_generated_attachments(
        object(), ctx, set(), conversation_id=uuid.uuid4(), reply_text="Done."
    )
    assert [str(r.id) for r in rows] == [generated.file_id]

    rows, _meta = await platform_api._collect_generated_attachments(
        object(), ctx, set(), conversation_id=uuid.uuid4(),
        reply_text=f"Source: [[FILE:{page.file_id}]]",
    )
    assert {str(r.id) for r in rows} == {generated.file_id, page.file_id}


# -- the public widget -------------------------------------------------------------


def test_deliver_named_files_clears_only_the_named_marks():
    named, unnamed = _ref("a.pdf"), _ref("b.html")
    ctx = FileContext()
    ctx.add_fetched(named)
    ctx.add_fetched(unnamed)

    deliver_named_files(ctx, f"Built from [[FILE:{named.file_id}]].")

    assert not ctx.is_fetched(named.file_id)
    assert ctx.is_fetched(unnamed.file_id)
    deliver_named_files(None, "no context")
    deliver_named_files(SimpleNamespace(), "a context without marks")


def _widget_turn(monkeypatch, answer: str, ctx: FileContext, *, withheld: bool = False):
    import app.tools.agent_loop as agent_loop_mod
    from app.api.v1.public_chat import _run_public_agent_chat
    from app.services import prompt_guards
    from tests.test_public_chat_hardening import _FakeDB, _make_public_agent, _patch_chat_pipeline

    _patch_chat_pipeline(monkeypatch)

    async def _loop(**_kwargs):
        return SimpleNamespace(content=answer, usage=None, model="test-model")

    monkeypatch.setattr(agent_loop_mod, "run_agent_loop", _loop)
    if withheld:
        async def _withhold(_text, **_kwargs):
            return prompt_guards.MODEL_OUTPUT_WITHHELD_MESSAGE

        monkeypatch.setattr(prompt_guards, "guard_model_output", _withhold)
    return _run_public_agent_chat(_FakeDB({}), _make_public_agent(), [], "hello", file_context=ctx)


@pytest.mark.asyncio
async def test_the_widget_hands_over_a_fetched_file_its_answer_names(monkeypatch):
    """The widget strips the markers from the prose (it shows files as chips),
    so which fetched file the answer hands over is read before they go."""
    named, unnamed = _ref("leak.txt"), _ref("page.html")
    ctx = FileContext()
    ctx.add_fetched(named)
    ctx.add_fetched(unnamed)

    text = await _widget_turn(monkeypatch, f"Here is the file: [[FILE:{named.file_id}]]", ctx)

    assert named.file_id not in text
    assert files_for_reply(ctx, [named.file_id, unnamed.file_id], text) == [named.file_id]


@pytest.mark.asyncio
async def test_a_withheld_widget_answer_hands_over_no_fetched_file(monkeypatch):
    named = _ref("leak.txt")
    ctx = FileContext()
    ctx.add_fetched(named)

    text = await _widget_turn(monkeypatch, f"See [[FILE:{named.file_id}]]", ctx, withheld=True)

    assert files_for_reply(ctx, [named.file_id], text) == []


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _SavepointDB:
    def begin_nested(self):
        return _Savepoint()


@pytest.mark.asyncio
async def test_the_widget_links_an_answers_files_to_it(monkeypatch):
    """Linked, a file is that answer's: a later write of the same bytes gets a
    row of its own, which the later answer can show."""
    import json

    from app.api.v1 import public_chat

    shown, taken = str(uuid.uuid4()), str(uuid.uuid4())
    files = [{"file_id": shown, "filename": "a.txt"}, {"file_id": taken, "filename": "b.txt"}]
    calls = []

    async def _bind(_db, rows, *, conversation_id, message_id):
        calls.append(([str(r.id) for r in rows], conversation_id, message_id))
        return [r for r in rows if str(r.id) == shown]

    monkeypatch.setattr("app.services.sub_agent_files.bind_attachment_rows", _bind)
    message = SimpleNamespace(id=uuid.uuid4(), metadata_json=json.dumps({public_chat.WIDGET_FILES_KEY: files}))
    conv = uuid.uuid4()

    out = await public_chat._bind_widget_answer_files(_SavepointDB(), message, conv, files)

    assert calls == [([shown, taken], conv, message.id)]
    assert out == [files[0]], "a file another message shows is not this answer's chip"
    assert json.loads(message.metadata_json) == {public_chat.WIDGET_FILES_KEY: [files[0]]}


@pytest.mark.asyncio
async def test_a_failed_widget_link_keeps_the_answer_and_its_chips(monkeypatch):
    from app.api.v1 import public_chat

    files = [{"file_id": str(uuid.uuid4()), "filename": "a.txt"}]

    async def _fail(*_args, **_kwargs):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr("app.services.sub_agent_files.bind_attachment_rows", _fail)
    message = SimpleNamespace(id=uuid.uuid4(), metadata_json="kept")

    out = await public_chat._bind_widget_answer_files(_SavepointDB(), message, uuid.uuid4(), files)

    assert out == files
    assert message.metadata_json == "kept"


def test_every_deliverable_producer_registers_its_file_as_produced():
    """A write the dedup hands a fetched row must still be delivered."""
    from app.services.builtin_tools import _shared, media

    assert inspect.getsource(media).count("register_produced_file(") >= 2, "generate_file and edits"
    assert inspect.getsource(media).count("match_filename=True") >= 2
    assert "register_produced_file(" in inspect.getsource(_shared.persist_tool_artifact)


def test_every_reply_attach_site_goes_through_files_for_reply():
    """Each surface that turns the turn's FileContext into a reply's
    attachments filters it through the one rule - a site that does not puts
    every page the agent read back on the reply."""
    from app.api.v1 import chat, platform, public_chat
    from app.channels import router
    from app.services import parent_wake, sub_agent_files

    for module in (chat, platform, public_chat, router, parent_wake, sub_agent_files):
        assert "files_for_reply(" in inspect.getsource(module), module.__name__


def test_every_read_only_producer_registers_its_file_as_fetched():
    """The tools that bring a file in to READ mark it; a tool that makes a
    deliverable (``generate_file``, a chart, an export) does not."""
    from app.integrations import file_content
    from app.services import integration_tools
    from app.services.builtin_tools import image_assets, kb, web

    assert "register_fetched_file(" in inspect.getsource(integration_tools)
    assert "register_fetched_file(" in inspect.getsource(file_content)
    assert inspect.getsource(web).count("register_fetched_file(") >= 2, "fresh fetch and cache hit"
    assert "fetched=True" in inspect.getsource(image_assets)
    assert "fetched=True" in inspect.getsource(kb)
