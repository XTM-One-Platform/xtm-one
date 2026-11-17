"""Every platform file path gives the agent the link, the download, the text and the file id.

A sweep found, on the platform's own paths, a stored file the agent
could not reach one of those ways:

* a result carrying a stored file (an MCP binary result, a custom tool's
  ``$output_files``) that names neither its id nor a marker: no file to hand on
  in the same turn, no link in a channel reply;
* ``store_knowledge`` taking text only: a conversation file could not be piped
  into a knowledge base;
* a knowledge-base document read that was text only: no file id, no chip;
* a channel attachment bound to no conversation and no message: no chip, gone
  after the first turn, unlinkable for an anonymous sender; and a file a channel
  turn produced that the reply did not name: no link at all;
* the public widget: an integration download bound to nothing (an orphan no
  session route serves) and a raw ``[[FILE:id]]`` marker in the answer.

The PostgreSQL half of the channel binding is ``test_channel_files_postgres.py``.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.channels import router as channel_router
from app.channels.base import ChannelType, InboundMessage
from app.llm.base import FileRef
from app.services.builtin_tools import (
    build_kb_document_read_tools,
    build_kb_store_tool,
    load_custom_tools,
)
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    FileContext,
    ToolCall,
    ToolDefinition,
    ToolResult,
)


def _stored_ref(
    name: str = "export.csv",
    content_type: str = "text/csv",
    data: bytes = b"region,total\nnorth,12\nsouth,30\n",
    source: str | None = "agent_generated",
) -> FileRef:
    return FileRef(
        file_id=str(uuid.uuid4()),
        filename=name,
        content_type=content_type,
        size=len(data),
        s3_object_name=f"chat/conv/{name}",
        _data=data,
        source=source,
    )


def _composite(result: ToolResult | None = None, *, name: str = "produce") -> CompositeToolExecutor:
    executor = CallableToolExecutor()

    async def _tool(**_kwargs):
        return result

    executor.register(name, _tool)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [ToolDefinition(name=name, description="d", input_schema={"type": "object", "properties": {}}, source="t")],
        executor,
    )
    return composite


# ---------------------------------------------------------------------------
# A result names the stored files it carries
# ---------------------------------------------------------------------------


async def test_a_result_carrying_an_unnamed_stored_file_names_its_id_and_marker():
    ref = _stored_ref()
    composite = _composite(ToolResult(tool_call_id="", content="[Binary content: export.csv (text/csv, 34 bytes)]", files=[ref]))

    out = await composite.execute_tool(ToolCall(id="c1", name="produce", arguments={}))

    assert out.content.startswith("[Binary content: export.csv")
    assert f"file_id {ref.file_id}" in out.content
    assert f"[[FILE:{ref.file_id}]]" in out.content
    assert out.files == [ref]


async def test_a_result_that_already_names_its_file_is_left_as_it_is():
    ref = _stored_ref()
    content = f"Saved. [[FILE:{ref.file_id}]]"
    composite = _composite(ToolResult(tool_call_id="", content=content, files=[ref]))

    out = await composite.execute_tool(ToolCall(id="c1", name="produce", arguments={}))

    assert out.content == content


async def test_the_file_the_call_named_is_not_named_again():
    ref = _stored_ref(name="shot.png", content_type="image/png")
    composite = _composite(ToolResult(tool_call_id="", content="File: shot.png - the image is attached.", files=[ref]))

    out = await composite.execute_tool(ToolCall(id="c1", name="produce", arguments={"file_id": ref.file_id}))

    assert out.content == "File: shot.png - the image is attached."


async def test_an_in_memory_file_and_an_error_are_left_as_they_are():
    in_memory = FileRef.from_bytes(b"x", "x.txt", "text/plain")
    composite = _composite(ToolResult(tool_call_id="", content="ok", files=[in_memory]))
    assert (await composite.execute_tool(ToolCall(id="c1", name="produce", arguments={}))).content == "ok"

    stored = _stored_ref()
    failing = _composite(ToolResult(tool_call_id="", content="Error: upstream failed", is_error=True, files=[stored]))
    assert (await failing.execute_tool(ToolCall(id="c2", name="produce", arguments={}))).content == "Error: upstream failed"


async def test_every_stored_file_of_a_result_is_named():
    refs = [_stored_ref(name=f"part-{n}.csv") for n in range(14)]
    composite = _composite(ToolResult(tool_call_id="", content="done", files=refs))

    out = await composite.execute_tool(ToolCall(id="c1", name="produce", arguments={}))

    assert all(f"[[FILE:{ref.file_id}]]" in out.content for ref in refs)


async def test_a_custom_tool_output_file_is_named_where_the_model_reads_it(monkeypatch):
    tool_id = uuid.uuid4()
    custom = SimpleNamespace(
        id=tool_id,
        name="csv_tool",
        js_function="return 'ok';",
        input_schema={"type": "object", "properties": {}},
        enabled=True,
        language="javascript",
        timeout=10,
        description="CSV tool",
    )
    created = SimpleNamespace(
        id=uuid.uuid4(),
        filename="export.csv",
        content_type="text/csv",
        size=7,
        s3_object_name="chat/conv/export.csv",
    )

    async def _fake_exec(*_args, **_kwargs):
        return json.dumps(
            {"result": "done", "$output_files": [{"filename": "export.csv", "content": "a,b\n1,2", "content_type": "text/csv"}]}
        )

    async def _fake_persist(**_kwargs):
        return created

    class _DB:
        async def execute(self, _query):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [custom]))

    monkeypatch.setattr("app.services.builtin_tools.execute_tool_auto", _fake_exec, raising=False)
    monkeypatch.setattr("app.services.custom_tool_executor.execute_tool_auto", _fake_exec)
    monkeypatch.setattr("app.services.builtin_tools._persist_generated_file", _fake_persist)

    composite = CompositeToolExecutor()
    await load_custom_tools(_DB(), [tool_id], composite, user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    ctx = FileContext()
    composite.set_file_context(ctx)

    out = await composite.execute_tool(ToolCall(id="c1", name="csv_tool", arguments={}))

    assert out.content.startswith("done")
    assert f"[[FILE:{created.id}]]" in out.content
    assert ctx.get(str(created.id)) is not None


# ---------------------------------------------------------------------------
# store_knowledge takes a conversation file
# ---------------------------------------------------------------------------


class _KBSession:
    def __init__(self, kb) -> None:
        self.kb = kb
        self.added: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _stmt):
        return SimpleNamespace(
            scalar_one_or_none=lambda: self.kb,
            scalars=lambda: SimpleNamespace(all=lambda: []),
        )

    async def delete(self, _obj):
        return None

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self) -> None:
        self.committed = True


def _writable_kb():
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Docs",
        is_read_only=False,
        is_builtin=False,
        is_company_managed=False,
        user_id=uuid.uuid4(),
        chunk_size=400,
        chunk_overlap=40,
        document_count=0,
        chunk_count=0,
    )


async def _store(arguments: dict, file_context: FileContext | None):
    kb = _writable_kb()
    session = _KBSession(kb)
    enqueue = AsyncMock(return_value=True)
    _, executor = build_kb_store_tool(None, "user-1", [(str(kb.id), kb.name)])
    executor.file_context = file_context
    with (
        patch("app.core.database.async_session", new=lambda: session),
        patch("app.api.v1.helpers.is_package_origin_knowledge_base", new=AsyncMock(return_value=False)),
        patch("app.services.kb_helpers.enqueue_document_processing", new=enqueue),
    ):
        out = await executor.execute_tool(ToolCall(id="k1", name="store_knowledge", arguments=arguments))
    return out.content, session, enqueue


async def test_store_knowledge_takes_a_conversation_file():
    ref = _stored_ref(name="policy.md", content_type="text/markdown", data=b"# Policy\n\nRotate the keys every 90 days.\n")
    ctx = FileContext.from_file_refs([ref])

    content, session, enqueue = await _store({"title": "", "file_id": f"[[FILE:{ref.file_id}]]"}, ctx)

    (doc,) = session.added
    assert doc.filename == "policy.md"
    assert "Rotate the keys every 90 days." in doc.content
    assert doc.status == "pending"
    enqueue.assert_awaited_once_with(str(doc.id))
    assert f"file_id {ref.file_id}" in content and str(doc.id) in content


async def test_store_knowledge_says_which_files_it_can_take():
    ref = _stored_ref()
    ctx = FileContext.from_file_refs([ref])

    unknown, session, _ = await _store({"title": "t", "file_id": str(uuid.uuid4())}, ctx)
    assert unknown.startswith("Error: no file") and ref.file_id in unknown
    assert session.added == []

    for content in ("some text", ""):
        both, _, _ = await _store({"title": "t", "content": content, "file_id": ref.file_id}, ctx)
        assert both == "Error: pass either 'content' or 'file_id', not both."

    neither, _, _ = await _store({"title": "t"}, ctx)
    assert neither.startswith("Error: provide the document text")


def test_store_knowledge_declares_the_file_id_and_needs_only_a_title():
    (definition,), _ = build_kb_store_tool(None, "user-1", [(str(uuid.uuid4()), "Docs")])
    assert "file_id" in definition.input_schema["properties"]
    assert definition.input_schema["required"] == ["title"]


# ---------------------------------------------------------------------------
# A knowledge-base document read is also a file of the conversation
# ---------------------------------------------------------------------------


class _DocSession:
    def __init__(self, doc, kb) -> None:
        self.doc = doc
        self.kb = kb

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt):
        rows = [self.kb] if "knowledge_documents" not in str(stmt) else [self.doc]
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: list(rows)),
            scalar_one_or_none=lambda: rows[0],
        )


def _document(content: str = "Rotate the keys every 90 days.", filename: str = "Key policy.pdf"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        filename=filename,
        source_type="upload",
        source_url=None,
        status="ready",
        chunk_count=1,
        error_message=None,
        content=content,
        content_hash="h1",
        created_at=None,
        updated_at=None,
    )


async def test_a_document_read_is_also_a_file_of_the_conversation(monkeypatch):
    doc = _document()
    kb = SimpleNamespace(id=doc.knowledge_base_id, name="Policies")
    stored: list[dict] = []

    async def _persist(**kwargs):
        stored.append(kwargs)
        ref = _stored_ref(name=kwargs["filename"], content_type=kwargs["content_type"], data=kwargs["raw_bytes"], source=kwargs["source"])
        kwargs["file_context"].add(ref)
        return ref

    monkeypatch.setattr("app.core.database.async_session", lambda: _DocSession(doc, kb))
    monkeypatch.setattr("app.services.builtin_tools.kb.persist_tool_artifact", _persist)
    conversation_id = str(uuid.uuid4())
    _, executor = build_kb_document_read_tools(None, str(uuid.uuid4()), [(str(kb.id), kb.name)], conversation_id=conversation_id)
    ctx = FileContext()
    executor.file_context = ctx

    first = await executor.execute_tool(ToolCall(id="d1", name="get_knowledge_document", arguments={"document_id": str(doc.id)}))
    second = await executor.execute_tool(ToolCall(id="d2", name="get_knowledge_document", arguments={"document_id": str(doc.id)}))

    (copy,) = ctx.list_files()
    assert copy.filename == "Key policy.pdf.txt"
    assert stored[0]["raw_bytes"] == doc.content.encode()
    assert stored[0]["conversation_id"] == conversation_id
    assert stored[0]["source"] == "agent_working"
    for out in (first, second):
        assert f"[[FILE:{copy.file_id}]]" in out.content
        assert doc.content in out.content
    assert len(stored) == 1


async def test_a_document_read_without_any_scope_stores_no_copy(monkeypatch):
    doc = _document()
    kb = SimpleNamespace(id=doc.knowledge_base_id, name="Policies")
    persist = AsyncMock()
    monkeypatch.setattr("app.core.database.async_session", lambda: _DocSession(doc, kb))
    monkeypatch.setattr("app.services.builtin_tools.kb.persist_tool_artifact", persist)
    _, executor = build_kb_document_read_tools(None, "anonymous", [(str(kb.id), kb.name)])

    out = await executor.execute_tool(ToolCall(id="d1", name="get_knowledge_document", arguments={"document_id": str(doc.id)}))

    assert doc.content in out.content
    persist.assert_not_awaited()


# ---------------------------------------------------------------------------
# Channels: the message's files and the turn's files are shown and linked
# ---------------------------------------------------------------------------


def test_a_channel_reply_links_the_download_files_it_does_not_name():
    produced = _stored_ref(name="report.pdf", content_type="application/pdf", source="mcp")
    named = _stored_ref(name="chart.png", content_type="image/png")
    working = _stored_ref(name="figure.html", content_type="text/html", source="agent_working")
    in_memory = FileRef.from_bytes(b"x", "x.txt", "text/plain")
    text = f"Here is the chart: [[FILE:{named.file_id}]]"

    out = channel_router._with_produced_file_markers(text, [produced, named, working, in_memory])

    assert out == f"{text}\n\n[[FILE:{produced.file_id}]]"
    assert channel_router._with_produced_file_markers(text, [named, working]) == text
    # A bare id is not a link; the platform URL of an image tag is one.
    bare = f"Stored as file_id {produced.file_id}."
    assert channel_router._with_produced_file_markers(bare, [produced]).endswith(f"[[FILE:{produced.file_id}]]")
    image = f"![chart](/api/v1/chat/files/{produced.file_id}/download)"
    assert channel_router._with_produced_file_markers(image, [produced]) == image
    # A plain link to the same URL stays relative in a channel: the marker is added.
    link = f"[download](/api/v1/chat/files/{produced.file_id}/download)"
    assert channel_router._with_produced_file_markers(link, [produced]).endswith(f"[[FILE:{produced.file_id}]]")


async def test_a_capture_not_shown_to_the_user_is_a_working_file(monkeypatch):
    from app.services.builtin_tools.web import build_browser_tool

    sources: list[str] = []

    async def _persist(**kwargs):
        sources.append(kwargs["source"])
        return _stored_ref(name=kwargs["filename"], content_type="image/png", data=kwargs["raw_bytes"], source=kwargs["source"])

    async def _shot(_key, full_page=False, selector=None):
        return b"\x89PNG not really"

    monkeypatch.setattr("app.services.builtin_tools.web.persist_tool_artifact", _persist)
    monkeypatch.setattr("app.services.browser_tool.browser_screenshot", _shot)
    _, executor = build_browser_tool(
        "agent:conv", db=object(), user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4())
    )

    for show in (True, False):
        await executor.execute_tool(ToolCall(id="s", name="browser", arguments={"action": "screenshot", "show_to_user": show}))

    assert sources == ["agent", "agent_working"]


def _inbound(**overrides) -> InboundMessage:
    fields = dict(
        channel=ChannelType.SLACK,
        channel_user_id="U1",
        channel_thread_id="T1",
        text="see the attached file",
    )
    fields.update(overrides)
    return InboundMessage(**fields)


async def test_the_channel_message_and_the_reply_show_their_files(monkeypatch):
    conv = SimpleNamespace(id=uuid.uuid4(), title="Slack thread", agent_id=None)
    stored: list[SimpleNamespace] = []
    attached: list[tuple[str, list]] = []

    async def _store_message(_db, conversation_id, role, content, **_kwargs):
        message = SimpleNamespace(id=uuid.uuid4(), role=role, content=content, conversation_id=conversation_id, channel="slack")
        stored.append(message)
        return message

    async def _attach(_db, conversation, message, refs):
        assert conversation is conv
        attached.append((message.role, list(refs)))
        return []

    class _DB:
        async def commit(self):
            return None

    monkeypatch.setattr(channel_router, "store_message", _store_message)
    monkeypatch.setattr(channel_router, "_attach_files_to_message", _attach)
    monkeypatch.setattr(channel_router, "_get_thread_key", lambda _inbound: None)
    upload = _stored_ref(name="invoice.pdf", content_type="application/pdf", source="channel:slack")
    produced = _stored_ref(name="summary.csv")

    await channel_router._finalize_message(
        _DB(),
        _inbound(),
        user=None,
        conv=conv,
        target_agent_id=None,
        is_anonymous=True,
        is_external_channel=True,
        suppress=False,
        assistant_text="Done.",
        user_files=[upload],
        turn_files=[produced],
    )

    assert [m.role for m in stored] == ["user", "assistant"]
    assert attached == [("user", [upload]), ("assistant", [produced])]


async def test_attaching_goes_through_the_shared_admission_and_bind(monkeypatch):
    conv = SimpleNamespace(id=uuid.uuid4())
    message = SimpleNamespace(id=uuid.uuid4(), channel="teams")
    ref = _stored_ref(source="channel:teams")
    rows = [SimpleNamespace(id=uuid.UUID(ref.file_id))]
    load = AsyncMock(return_value=rows)
    attach = AsyncMock(return_value=rows)
    monkeypatch.setattr("app.services.sub_agent_files.load_attachment_rows", load)
    monkeypatch.setattr("app.services.sub_agent_files.attach_rows_to_message", attach)

    bound = await channel_router._attach_files_to_message(object(), conv, message, [ref])

    assert bound == rows
    assert load.await_args.args == ([ref.file_id],)
    assert load.await_args.kwargs["conversation_id"] == conv.id
    assert attach.await_args.args[1:] == (message, rows)
    assert attach.await_args.kwargs == {"conversation_id": conv.id}


# ---------------------------------------------------------------------------
# The public widget
# ---------------------------------------------------------------------------


def test_the_widget_answer_keeps_no_raw_file_marker():
    from app.channels.base import strip_file_markers

    file_id = str(uuid.uuid4())
    text = f"Here is the export. [[FILE:{file_id}]]\n\nAnd the deck: [[PRESENTATION:{uuid.uuid4()}]]"

    out = strip_file_markers(text)

    assert "[[FILE:" not in out
    assert "[[PRESENTATION:" in out
    assert strip_file_markers("no marker here") == "no marker here"


def test_the_widget_stream_never_shows_a_marker_split_across_chunks():
    from app.channels.base import FileMarkerStreamFilter

    file_id = str(uuid.uuid4())
    text = f"Here is the export [[FILE:{file_id}]] and a list [1] of [links]."
    for size in (1, 3, 7, 11, len(text)):
        stream = FileMarkerStreamFilter()
        shown = "".join(stream.feed(text[i : i + size]) for i in range(0, len(text), size)) + stream.flush()
        assert shown == "Here is the export  and a list [1] of [links].", size
    unfinished = FileMarkerStreamFilter()
    assert unfinished.feed("cut [[FILE:ab") == "cut "
    assert unfinished.flush() == "[[FILE:ab"


async def test_the_widget_binds_integration_downloads_to_its_conversation(monkeypatch):
    from app.api.v1 import public_chat
    from tests.test_public_chat_hardening import _FakeDB, _make_public_agent, _patch_chat_pipeline

    _patch_chat_pipeline(monkeypatch)
    contexts: list[dict] = []
    file_id = str(uuid.uuid4())

    async def _integration_tools(_db, _bindings, context=None):
        contexts.append(dict(context or {}))
        return [], CallableToolExecutor()

    async def _loop(**_kwargs):
        return SimpleNamespace(content=f"Here is the ticket export. [[FILE:{file_id}]]", usage=None, model="m")

    monkeypatch.setattr("app.services.integration_tools.build_integration_tools", _integration_tools)
    monkeypatch.setattr("app.tools.agent_loop.run_agent_loop", _loop)
    agent = _make_public_agent(
        integration_bindings={"jira": [str(uuid.uuid4())]},
        channel_config={"web_embed": {"allow_tools": True}},
    )
    session_id = str(uuid.uuid4())

    answer = await public_chat._run_public_agent_chat(_FakeDB(), agent, [], "export it", session_id=session_id, file_context=FileContext())

    (context,) = contexts
    assert context["conversation_id"] == session_id
    assert "user_id" not in context
    assert answer == "Here is the ticket export."
