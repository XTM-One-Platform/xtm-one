"""Content straight into the sandbox workspace, never through the model's context.

``to_workspace_path`` on the tools that return external content (declared by
``ToolDefinition.workspace_ingest``), ``workspace_path`` on the tools that
take a file of the run (``ToolDefinition.workspace_files``), the ``sandbox_*``
file tools, ``materialize_repository`` against a fixture repository archive
with the checkout record it leaves and ``create_github_commit(from_checkout)``
over it, and the server-side package installs against a fixture index. Synthetic fixtures
only: an in-memory workspace store, an in-memory archive, an httpx mock
transport for the index.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import uuid
import zipfile
from types import SimpleNamespace

import httpx
import pytest

import app.services.work_item_files as wi_files
from app.llm.base import FileRef
from app.services import code_interpreter
from app.services.builtin_tools.media import build_code_interpreter_tool
from app.services.sandbox_workspace import local
from app.services.sandbox_workspace import session as session_module
from app.services.sandbox_workspace.local import BlobCache
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope
from app.tools.base import (
    WORKSPACE_FILE_ARGUMENT,
    WORKSPACE_PATH_ARGUMENT,
    CallableToolExecutor,
    CompositeToolExecutor,
    FileContext,
    ToolCall,
    ToolDefinition,
    ToolResult,
    offering_tools,
)

MB = 1024 * 1024
#: A fixture credential that must never land on disk.
TOKEN = "ghs_FIXTURESECRET0123456789"


@pytest.fixture
def store(monkeypatch, tmp_path) -> MemoryWorkspaceStore:
    from app.core.config import settings

    monkeypatch.setattr(settings, "custom_tool_sandbox", "subprocess")
    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    monkeypatch.setattr(settings, "code_interpreter_workspace_enabled", True)
    monkeypatch.setattr(settings, "code_interpreter_package_allowlist", "")
    monkeypatch.setattr(local, "_cache", BlobCache(root=str(tmp_path / "cache"), max_bytes=64 * MB))
    memory = MemoryWorkspaceStore()
    monkeypatch.setattr(session_module, "default_store", lambda: memory)
    return memory


def _composite(conversation: uuid.UUID, user: uuid.UUID) -> tuple[CompositeToolExecutor, object]:
    composite = CompositeToolExecutor()
    composite.set_file_context(FileContext())
    definitions, executor = build_code_interpreter_tool(user_id=str(user), conversation_id=str(conversation))
    composite.add_tools(definitions, executor)
    return composite, executor


def _text_tool(text: str, calls: list) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    executor = CallableToolExecutor()

    async def fetch_text(url: str) -> str:
        calls.append(url)
        return text

    executor.register("fetch_text", fetch_text)
    return [
        ToolDefinition(
            name="fetch_text",
            description="Fetch text.",
            input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
            workspace_ingest=True,
        )
    ], executor


async def _workspace_bytes(store: MemoryWorkspaceStore, conversation: uuid.UUID, path: str) -> bytes:
    (record,) = [r for r in store.records.values() if r.conversation_id == conversation]
    entry = store.files[record.id][path]
    return await store.read_blob(record.id, entry.sha256)


# -- The schema ---------------------------------------------------------------


async def test_an_ingest_tool_is_offered_to_workspace_path_only_with_a_workspace(store):
    composite, _ = _composite(uuid.uuid4(), uuid.uuid4())
    composite.add_tools(*_text_tool("x", []))
    plain = CompositeToolExecutor()
    plain.add_tools(*_text_tool("x", []))
    with_ws = {d.name: d for d in composite.all_tools}
    assert WORKSPACE_PATH_ARGUMENT in with_ws["fetch_text"].input_schema["properties"]
    assert WORKSPACE_PATH_ARGUMENT not in plain.all_tools[0].input_schema["properties"]
    # Only declared tools gain it.
    assert WORKSPACE_PATH_ARGUMENT not in with_ws["sandbox_list_files"].input_schema["properties"]
    assert {"sandbox_put_file", "sandbox_get_file", "sandbox_list_files", "sandbox_remove_files", "materialize_repository"} <= set(with_ws)


def test_the_code_interpreter_description_teaches_the_workspace():
    definitions, _ = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    ci = definitions[0]
    text = ci.description
    assert "Files under workspace/ (the WORKSPACE_DIR" in text and "DO persist" in text
    assert "session=true" in text and "to_workspace_path" in text and "materialize_repository" in text
    assert "not persisted between calls" in text.lower()
    props = ci.input_schema["properties"]
    assert {"code", "session", "reset"} <= set(props)
    assert "packages" not in props, "installs are off without an allow-list"
    # Without a conversation or a run: today's description, today's schema.
    (bare,), _ = build_code_interpreter_tool()
    assert "workspace/" not in bare.description and set(bare.input_schema["properties"]) == {"code"}


async def test_the_description_carries_the_manifest_read_before_the_run(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    seed = WorkspaceSession(WorkspaceScope(user_id=user, conversation_id=conversation), store=store)
    await seed.put_bytes("notes/plan.md", b"# plan")
    composite, _ = _composite(conversation, user)
    await composite.prepare_for_run()
    ci = next(d for d in composite.all_tools if d.name == "code_interpreter")
    described = ci.describe_for_surface(())
    assert "The workspace now holds: 1 file(s)" in described and "workspace/notes/plan.md" in described


# -- to_workspace_path --------------------------------------------------------


async def test_a_text_result_is_written_to_the_workspace_instead_of_returned(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    calls: list = []
    composite.add_tools(*_text_tool("print('repository file')\n", calls))
    result = await composite.execute_tool(
        ToolCall(id="c1", name="fetch_text", arguments={"url": "u", WORKSPACE_PATH_ARGUMENT: "workspace/src/a.py"})
    )
    assert calls == ["u"]
    assert not result.is_error, result.content
    assert "print('repository file')" not in result.content
    digest = hashlib.sha256(b"print('repository file')\n").hexdigest()
    assert f"workspace/src/a.py (25 B, sha256 {digest})" in result.content
    assert await _workspace_bytes(store, conversation, "src/a.py") == b"print('repository file')\n"


async def test_a_downloaded_file_goes_to_the_workspace_not_to_a_conversation_file(store):
    from app.integrations.file_delivery import deliver_file

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    executor = CallableToolExecutor()

    async def _never(*_a):
        raise AssertionError("bound for the workspace: not stored as a conversation file")

    async def download_thing(name: str) -> ToolResult:
        return await deliver_file(data=b"\x00binary", filename=name, content_type="application/octet-stream", source="Fixture", persist_file_fn=_never)

    executor.register("download_thing", download_thing)
    composite.add_tools(
        [ToolDefinition(name="download_thing", description="d", input_schema={"type": "object", "properties": {}}, workspace_ingest=True)],
        executor,
    )
    result = await composite.execute_tool(
        ToolCall(id="c1", name="download_thing", arguments={"name": "blob.bin", WORKSPACE_PATH_ARGUMENT: "data/blob.bin"})
    )
    assert not result.is_error, result.content
    assert await _workspace_bytes(store, conversation, "data/blob.bin") == b"\x00binary"
    assert "Also kept as conversation file" not in result.content
    assert not result.files


async def test_read_file_content_writes_the_file_itself(store):
    from app.services.builtin_tools.media import build_generate_file_tool

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    definitions, executor = build_generate_file_tool(None, str(user), str(conversation))
    composite.add_tools(definitions, executor)
    ref = FileRef.from_bytes(data=b"a,b\n1,2\n", filename="data.csv", content_type="text/csv")
    composite.file_context.add(ref)
    composite.set_file_context(composite.file_context)
    result = await composite.execute_tool(
        ToolCall(id="c1", name="read_file_content", arguments={"file_id": ref.file_id, WORKSPACE_PATH_ARGUMENT: "in/data.csv"})
    )
    assert not result.is_error, result.content
    assert await _workspace_bytes(store, conversation, "in/data.csv") == b"a,b\n1,2\n"


async def test_get_github_file_writes_the_raw_bytes_not_the_rendering(store, monkeypatch):
    from app.integrations.providers.github import contents

    raw = b"# README\n\nNo rendering, no links.\n"

    async def _branch(client, headers, repo, owner, name, branch):
        return branch or "main", None

    async def _request(client, method, url, provider, action, **kwargs):
        assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
        meta = {"type": "file", "size": len(raw), "encoding": "base64", "content": base64.b64encode(raw).decode(), "html_url": "h"}
        return SimpleNamespace(status_code=200, json=lambda: meta), None

    monkeypatch.setattr(contents, "_resolve_branch", _branch)
    monkeypatch.setattr(contents._common, "safe_request", _request)
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    gh = CallableToolExecutor()
    definitions = contents.register({"Authorization": f"Bearer {TOKEN}"}, gh, None, None)
    composite.add_tools(definitions, gh)
    assert next(d for d in definitions if d.name == "get_github_file").workspace_ingest
    result = await composite.execute_tool(
        ToolCall(id="c1", name="get_github_file", arguments={"repo": "acme/repo", "path": "README.md", WORKSPACE_PATH_ARGUMENT: "repo/README.md"})
    )
    assert not result.is_error, result.content
    assert await _workspace_bytes(store, conversation, "repo/README.md") == raw
    # Written instead of returned: no content, and no file on the result (a
    # file on a tool result is read into the model's context as its text).
    assert "No rendering, no links." not in result.content
    assert not result.files
    assert "workspace/repo/README.md" in result.content and "sha256" in result.content


async def test_a_path_outside_the_workspace_is_refused_before_the_tool_runs(store):
    composite, _ = _composite(uuid.uuid4(), uuid.uuid4())
    calls: list = []
    composite.add_tools(*_text_tool("x", calls))
    result = await composite.execute_tool(
        ToolCall(id="c1", name="fetch_text", arguments={"url": "u", WORKSPACE_PATH_ARGUMENT: "../../etc/cron.d/x"})
    )
    assert result.is_error and "Nothing was run" in result.content
    assert calls == []


async def test_a_failed_call_writes_nothing(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    executor = CallableToolExecutor()

    async def broken(url: str) -> ToolResult:
        return ToolResult(tool_call_id="", content="Error: upstream 500", is_error=True)

    executor.register("broken", broken)
    composite.add_tools(
        [ToolDefinition(name="broken", description="b", input_schema={"type": "object", "properties": {}}, workspace_ingest=True)],
        executor,
    )
    result = await composite.execute_tool(ToolCall(id="c1", name="broken", arguments={"url": "u", WORKSPACE_PATH_ARGUMENT: "a.txt"}))
    assert result.is_error and result.content == "Error: upstream 500"
    assert store.records == {} or all(not files for files in store.files.values())


async def test_a_write_past_the_quota_returns_neither_the_content_nor_a_file(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    executor.session._limits = Limits(quota_bytes=10, max_files=10)
    composite.add_tools(*_text_tool("y" * 100, []))
    result = await composite.execute_tool(ToolCall(id="c1", name="fetch_text", arguments={"url": "u", WORKSPACE_PATH_ARGUMENT: "big.txt"}))
    assert result.is_error and "Workspace quota exceeded" in result.content
    assert "y" * 100 not in result.content


# -- The sandbox_* tools ------------------------------------------------------


async def test_put_list_get_and_remove(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    ref = FileRef.from_bytes(data=b"hello", filename="hello.txt", content_type="text/plain")
    composite.file_context.add(ref)
    put = await composite.execute_tool(ToolCall(id="1", name="sandbox_put_file", arguments={"file_id": ref.file_id, "path": "in/hello.txt"}))
    assert "Wrote hello.txt to workspace/in/hello.txt (5 B" in put.content
    listed = await composite.execute_tool(ToolCall(id="2", name="sandbox_list_files", arguments={}))
    assert "- workspace/in/hello.txt (5 B" in listed.content
    got = await composite.execute_tool(ToolCall(id="3", name="sandbox_get_file", arguments={"path": "in/hello.txt"}))
    assert "[[FILE:" in got.content and "hello" not in got.content.split("(", 1)[1]
    # A text file is registered on the turn but not attached to the result: an
    # attached text file is read back to the model whole by every provider.
    assert got.files is None
    handed = [f for f in composite.file_context.list_files() if f.filename == "hello.txt" and f.file_id != ref.file_id]
    assert handed and handed[0].inline_data == b"hello"
    removed = await composite.execute_tool(ToolCall(id="4", name="sandbox_remove_files", arguments={"paths": ["in"]}))
    assert "removed 1: workspace/in/hello.txt" in removed.content
    again = await composite.execute_tool(ToolCall(id="5", name="sandbox_list_files", arguments={}))
    assert again.content.startswith("The workspace is empty")


async def test_list_files_narrows_to_a_depth_and_a_glob(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    for name in ("src/a.test.ts", "src/b.test.ts", "src/main.ts", "src/deep/c.test.ts"):
        await executor.session.put_bytes(name, b"x")
    root = await composite.execute_tool(
        ToolCall(id="1", name="sandbox_list_files", arguments={"path": "src", "depth": 1, "glob": "*.test.ts"})
    )
    assert not root.is_error, root.content
    assert root.content.splitlines()[0].startswith("2 file(s)") and "matching depth 1, glob *.test.ts" in root.content
    assert "workspace/src/a.test.ts" in root.content and "workspace/src/b.test.ts" in root.content
    assert "deep/c.test.ts" not in root.content and "main.ts" not in root.content
    nested = await composite.execute_tool(
        ToolCall(id="2", name="sandbox_list_files", arguments={"path": "src", "glob": "deep/*.ts"})
    )
    assert "workspace/src/deep/c.test.ts" in nested.content and "a.test.ts" not in nested.content
    none = await composite.execute_tool(
        ToolCall(id="3", name="sandbox_list_files", arguments={"path": "src", "glob": "*.py"})
    )
    assert none.content == "No file under workspace/src matches glob *.py."
    bad = await composite.execute_tool(ToolCall(id="4", name="sandbox_list_files", arguments={"depth": "two"}))
    assert bad.is_error and "depth is a whole number" in bad.content


async def test_put_takes_a_stored_file_of_the_conversation_the_context_missed(store, monkeypatch):
    """A file this conversation stored (a kept video frame) that the turn's context does not hold is put
    through its row, then joins the context as a fetched file (code_interpreter mounts it in attachments/)."""
    from app.services.builtin_tools import _shared

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    ref = FileRef.from_bytes(data=b"PNGFRAME", filename="launch - 1.00s.png", content_type="image/png")
    asked: list[dict] = []

    async def scoped(file_id, *, conversation_id, assignment_run_id):
        asked.append({"file_id": file_id, "conversation_id": conversation_id})
        # The real lookup reads the id however it is spelled (bare, file-<id>, the [[FILE:<id>]] marker).
        return ref if ref.file_id in file_id and conversation_id == str(conversation) else None

    monkeypatch.setattr(_shared, "load_scoped_file_ref", scoped)
    put = await composite.execute_tool(
        ToolCall(
            id="1", name="sandbox_put_file", arguments={"file_id": f"[[FILE:{ref.file_id}]]", "path": "frames/f1.png"}
        )
    )
    assert "to workspace/frames/f1.png (8 B" in put.content, put.content
    assert asked == [{"file_id": f"[[FILE:{ref.file_id}]]", "conversation_id": str(conversation)}]
    assert composite.file_context.get(ref.file_id) is not None and composite.file_context.is_fetched(ref.file_id)
    # A second put resolves from the context: no second row read.
    again = await composite.execute_tool(
        ToolCall(id="2", name="sandbox_put_file", arguments={"file_id": ref.file_id, "path": "frames/f2.png"})
    )
    assert "workspace/frames/f2.png" in again.content and len(asked) == 1


@pytest.mark.parametrize(
    ("row", "said"),
    [
        (None, "is not a file of this conversation: pass"),
        ({"conversation_id": "c-other", "assignment_run_id": None}, "(it is a file of conversation c-other)"),
        ({"conversation_id": None, "assignment_run_id": "r-1"}, "(it is a file of assignment run r-1)"),
        ({"conversation_id": None, "assignment_run_id": None}, "(it is unregistered: bound to no conversation or run)"),
    ],
    ids=["stranger-or-absent", "own-other-conversation", "own-run", "own-unregistered"],
)
async def test_put_refuses_another_conversations_file_naming_its_owner_scope(store, monkeypatch, row, said):
    """Never another conversation's file; the refusal says whose it is only for a file the caller owns."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.services.builtin_tools import _shared

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)

    async def scoped(_file_id, **_scope):
        return None

    class _Rows:
        def first(self):
            return SimpleNamespace(**row) if row is not None else None

    class _Db:
        async def execute(self, _statement):
            return _Rows()

    @asynccontextmanager
    async def own_session():
        yield _Db()

    async def not_owned(_file_id, _user_id):
        # The owned lookup admits nothing here (a template asset, a row gone): only the refusal is tested.
        return None, None

    async def no_asset(_file_id, _user_id):
        return None

    from app.services import template_containers

    monkeypatch.setattr(_shared, "load_scoped_file_ref", scoped)
    monkeypatch.setattr(_shared, "_own_session", own_session)
    monkeypatch.setattr(wi_files, "load_owned_file_ref", not_owned)
    monkeypatch.setattr(template_containers, "load_visible_asset", no_asset)
    file_id = str(uuid.uuid4())
    put = await composite.execute_tool(
        ToolCall(id="1", name="sandbox_put_file", arguments={"file_id": file_id, "path": "x.png"})
    )
    assert put.is_error and f"file {file_id} is not a file of this conversation" in put.content
    assert said in put.content
    assert composite.file_context.get(file_id) is None


async def test_put_takes_a_file_another_run_of_the_same_user_produced(store, monkeypatch):
    """A stored file the acting user owns in another conversation or run is put, by its id or an 8+ hex
    prefix, and joins the context as a fetched file."""
    from app.services.builtin_tools import _shared

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    ref = FileRef.from_bytes(data=b"BRIEF", filename="brief.md", content_type="text/markdown")
    asked: list[tuple[str, str]] = []

    async def scoped(_file_id, **_scope):
        return None

    async def owned(file_id, user_id):
        asked.append((file_id, user_id))
        return (ref, None) if user_id == str(user) and ref.file_id.startswith(file_id) else (None, None)

    monkeypatch.setattr(_shared, "load_scoped_file_ref", scoped)
    monkeypatch.setattr(wi_files, "load_owned_file_ref", owned)
    put = await composite.execute_tool(
        ToolCall(id="1", name="sandbox_put_file", arguments={"file_id": ref.file_id[:8], "path": "in/brief.md"})
    )
    assert "to workspace/in/brief.md (5 B" in put.content, put.content
    assert asked == [(ref.file_id[:8], str(user))]
    assert composite.file_context.is_fetched(ref.file_id)


async def test_put_takes_a_template_asset_the_caller_can_see(store, monkeypatch):
    """A template asset (a brand logo from get_template) is put by its id when its container is visible to the
    acting user (#1318); the lookup is asked with the bare id whatever spelling the model used."""
    from types import SimpleNamespace

    from app.services import template_containers
    from app.services.builtin_tools import _shared

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    asset_id = uuid.uuid4()
    asked: list[tuple[str, str]] = []

    async def scoped(_file_id, **_scope):
        return None

    async def not_owned(_file_id, _user_id):
        return None, None

    async def visible(file_id, user_id):
        asked.append((file_id, user_id))
        return SimpleNamespace(id=asset_id) if file_id == str(asset_id) and user_id == str(user) else None

    ref = FileRef.from_bytes(data=b"<svg/>", filename="logo.svg", content_type="image/svg+xml")
    monkeypatch.setattr(_shared, "load_scoped_file_ref", scoped)
    monkeypatch.setattr(wi_files, "load_owned_file_ref", not_owned)
    monkeypatch.setattr(template_containers, "load_visible_asset", visible)
    monkeypatch.setattr(FileRef, "from_attachment", staticmethod(lambda _row: ref))
    put = await composite.execute_tool(
        ToolCall(
            id="1", name="sandbox_put_file", arguments={"file_id": f"[[FILE:{asset_id}]]", "path": "brand/logo.svg"}
        )
    )
    assert "to workspace/brand/logo.svg (6 B" in put.content, put.content
    assert asked == [(str(asset_id), str(user))]


async def test_put_refuses_an_ambiguous_prefix_with_its_candidates(store, monkeypatch):
    from app.services.builtin_tools import _shared

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)

    async def scoped(_file_id, **_scope):
        return None

    async def owned(file_id, _user_id):
        return (
            None,
            f"Error: `{file_id}` is the start of the id of 2 files of yours you can see: `a` (x.md); `b` (y.md).",
        )

    monkeypatch.setattr(_shared, "load_scoped_file_ref", scoped)
    monkeypatch.setattr(wi_files, "load_owned_file_ref", owned)
    put = await composite.execute_tool(
        ToolCall(id="1", name="sandbox_put_file", arguments={"file_id": "abcdef12", "path": "x.md"})
    )
    assert put.is_error and "is the start of the id of 2 files of yours" in put.content
    assert "`a` (x.md); `b` (y.md)" in put.content


# -- workspace_path on a file-taking tool ---------------------------------------


async def test_get_file_still_attaches_an_image_so_the_model_sees_it(store):
    composite, executor = _composite(uuid.uuid4(), uuid.uuid4())
    await executor.session.put_bytes("shots/a.png", b"\x89PNG\r\n\x1a\nfake")
    got = await composite.execute_tool(ToolCall(id="1", name="sandbox_get_file", arguments={"path": "shots/a.png"}))
    assert got.files and got.files[0].filename == "a.png"


async def test_render_html_to_image_renders_a_workspace_file_in_one_call(store, monkeypatch):
    """workspace_path on render_html_to_image: read on the server, no HTML in the result, the source saved once."""
    from types import SimpleNamespace

    from app.services.builtin_tools import html_render

    persisted: list[dict] = []

    async def _fake_persist(**kwargs):
        persisted.append(kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kwargs["filename"],
            content_type=kwargs["content_type"],
            size=len(kwargs["raw_bytes"]),
            s3_object_name=f"chat/conv/{kwargs['filename']}",
            source=kwargs["source"],
        )

    rendered: list[str] = []

    async def _fake_render(html, *, width, height, scale, font_css):
        rendered.append(html)
        return b"\x89PNG\r\n\x1a\n", {"out": [], "ovf": [], "overlap": [], "leaves": 1}

    async def _summary(_png):
        return None

    monkeypatch.setattr("app.services.builtin_tools._shared._persist_generated_file", _fake_persist)
    monkeypatch.setattr(html_render, "render_html", _fake_render)
    monkeypatch.setattr(html_render, "font_face_css", lambda: "")
    monkeypatch.setattr("app.services.image_summary.summarize_image", _summary)
    monkeypatch.setattr("app.services.image_summary.format_visual_summary", lambda _s: "Visual summary: -")

    composite, executor = _composite(uuid.uuid4(), uuid.uuid4())
    defs, render_executor = html_render.build_render_html_tool(db=object(), user_id=str(uuid.uuid4()), conversation_id="c")
    composite.add_tools(defs, render_executor)
    offered = composite.get_tool_definition("render_html_to_image")
    assert offered is not None and offered.workspace_files == ("source_file_id",)
    assert WORKSPACE_FILE_ARGUMENT in {d.name: d for d in composite.all_tools}["render_html_to_image"].input_schema["properties"]

    mockup = "<div class='mockup'>" + "Fleet About tab " * 200 + "</div>"
    await executor.session.put_bytes("work/mockup-about.html", mockup.encode())
    before = {f.file_id for f in composite.file_context.list_files()}
    result = await composite.execute_tool(
        ToolCall(id="r1", name="render_html_to_image", arguments={WORKSPACE_FILE_ARGUMENT: "workspace/work/mockup-about.html"})
    )
    assert result.is_error is False, result.content
    assert rendered and "Fleet About tab" in rendered[0]
    assert "Fleet About tab" not in result.content, "the HTML never enters the context"
    assert [p["filename"] for p in persisted] == ["mockup-about.html", "mockup-about.png"]
    assert persisted[0]["raw_bytes"] == mockup.encode(), "the sibling source is saved for source_file_id"
    assert "sandbox_edit_file" in result.content and "same workspace_path" in result.content
    assert [f.filename for f in result.files] == ["mockup-about.png"]
    # The parked workspace ref is forgotten once the tool returns.
    after = {f.file_id: f for f in composite.file_context.list_files()}
    assert not [f for fid, f in after.items() if fid not in before and f.source == "sandbox_workspace"]


async def test_render_html_to_image_refuses_a_missing_workspace_file(store):
    from app.services.builtin_tools import html_render

    composite, _executor = _composite(uuid.uuid4(), uuid.uuid4())
    composite.add_tools(*html_render.build_render_html_tool(db=object(), user_id=str(uuid.uuid4()), conversation_id="c"))
    result = await composite.execute_tool(
        ToolCall(id="r1", name="render_html_to_image", arguments={WORKSPACE_FILE_ARGUMENT: "work/nope.html"})
    )
    assert result.is_error and "is not in the workspace" in result.content and "Nothing was run" in result.content


async def test_render_html_to_image_writes_the_png_to_the_workspace_and_keeps_its_answer(store, monkeypatch):
    """png_workspace_path: the PNG lands in the workspace for code, the probe findings and file_id stay in the answer."""
    from types import SimpleNamespace

    from app.services.builtin_tools import html_render

    async def _fake_persist(**kwargs):
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kwargs["filename"],
            content_type=kwargs["content_type"],
            size=len(kwargs["raw_bytes"]),
            s3_object_name=f"chat/conv/{kwargs['filename']}",
            source=kwargs["source"],
        )

    png = b"\x89PNG\r\n\x1a\n-mockup-pixels"

    in_flight: list[int] = []

    async def _fake_render(html, *, width, height, scale, font_css):
        # A reader of the same tool round waits for the PNG: the render counts as a workspace write.
        in_flight.append(executor.session.writes_in_flight)
        return png, {"out": [], "ovf": [], "overlap": [], "leaves": 1}

    async def _summary(_png):
        return None

    monkeypatch.setattr("app.services.builtin_tools._shared._persist_generated_file", _fake_persist)
    monkeypatch.setattr(html_render, "render_html", _fake_render)
    monkeypatch.setattr(html_render, "font_face_css", lambda: "")
    monkeypatch.setattr("app.services.image_summary.summarize_image", _summary)
    monkeypatch.setattr("app.services.image_summary.format_visual_summary", lambda _s: "Visual summary: -")

    composite, executor = _composite(uuid.uuid4(), uuid.uuid4())
    composite.add_tools(
        *html_render.build_render_html_tool(db=object(), user_id=str(uuid.uuid4()), conversation_id="c")
    )
    await executor.session.put_bytes("work/card.html", b"<div>card</div>")
    result = await composite.execute_tool(
        ToolCall(
            id="r1",
            name="render_html_to_image",
            arguments={WORKSPACE_FILE_ARGUMENT: "work/card.html", "png_workspace_path": "work/shots/card.png"},
        )
    )
    assert result.is_error is False, result.content
    assert (await executor.session.get_bytes("work/shots/card.png"))[1] == png
    assert "PNG also written to the workspace:" in result.content and "work/shots/card.png" in result.content
    assert "Probe: clean" in result.content and "as file_id" in result.content, "the answer is unchanged"
    assert [f.filename for f in result.files] == ["card.png"]
    assert in_flight == [1] and executor.session.writes_in_flight == 0

    refused = await composite.execute_tool(
        ToolCall(
            id="r2",
            name="render_html_to_image",
            arguments={WORKSPACE_FILE_ARGUMENT: "work/card.html", "png_workspace_path": "../outside.png"},
        )
    )
    assert refused.is_error is False, "the render stands when the workspace refuses the path"
    assert "PNG not written to the workspace:" in refused.content and "Probe: clean" in refused.content


async def test_render_html_to_image_pages_renders_each_workspace_page_without_pixels(store, monkeypatch):
    """pages: every workspace page is rendered and probed in one call; file ids and verdicts, no PNG attached."""
    from types import SimpleNamespace

    from app.services.builtin_tools import html_render

    persisted: list[str] = []

    async def _fake_persist(**kwargs):
        persisted.append(kwargs["filename"])
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kwargs["filename"],
            content_type=kwargs["content_type"],
            size=len(kwargs["raw_bytes"]),
            s3_object_name=f"chat/conv/{kwargs['filename']}",
            source=kwargs["source"],
        )

    rendered: list[tuple[str, int, int]] = []

    async def _fake_render(html, *, width, height, scale, font_css):
        rendered.append((html, width, height))
        if "boom" in html:
            raise RuntimeError("layout crashed")
        overflow = [{"el": "h1.title", "scroll": [480, 40], "box": [440, 40]}] if "wide" in html else []
        return b"\x89PNG-" + html.encode(), {"out": [], "ovf": overflow, "overlap": [], "leaves": 1}

    monkeypatch.setattr("app.services.builtin_tools._shared._persist_generated_file", _fake_persist)
    monkeypatch.setattr(html_render, "render_html", _fake_render)
    monkeypatch.setattr(html_render, "font_face_css", lambda: "")

    composite, executor = _composite(uuid.uuid4(), uuid.uuid4())
    composite.add_tools(
        *html_render.build_render_html_tool(db=object(), user_id=str(uuid.uuid4()), conversation_id="c")
    )
    await executor.session.put_bytes("work/mocks/home.html", b"<div>home</div>")
    await executor.session.put_bytes("work/mocks/list.html", b"<div>wide</div>")
    await executor.session.put_bytes("work/mocks/bad.html", b"<div>boom</div>")
    result = await composite.execute_tool(
        ToolCall(
            id="p1",
            name="render_html_to_image",
            arguments={
                "pages": [
                    "work/mocks/home.html",
                    "work/mocks/list.html",
                    "work/mocks/nope.html",
                    "work/mocks/bad.html",
                ],
                "width": 1440,
                "height": 900,
            },
        )
    )
    assert result.is_error is False, result.content
    assert not result.files, "no pixels are attached in batch mode"
    assert persisted == ["home.png", "list.png"], "only the PNGs are stored, no HTML sibling"
    assert [(w, h) for _html, w, h in rendered] == [(1440, 900)] * 3
    assert "Rendered 2 of 4 workspace page(s) at 1440x900 px" in result.content
    home, wide = result.content.split("\n\n")[1:3]
    assert home.startswith("1. work/mocks/home.html: file_id ") and "Probe: clean" in home
    assert wide.startswith("2. work/mocks/list.html: file_id ") and "Probe: 1 finding(s)" in wide
    assert "OVF h1.title: content clipped (480x40 in 440x40)" in wide
    assert "\n\n3. work/mocks/nope.html: not rendered - " in result.content
    assert result.content.endswith("\n\n4. work/mocks/bad.html: not rendered - layout crashed")


async def test_render_html_to_image_pages_is_refused_when_it_cannot_run(store, monkeypatch):
    from app.services.builtin_tools import html_render

    async def _never(*_args, **_kwargs):
        raise AssertionError("nothing renders")

    monkeypatch.setattr(html_render, "render_html", _never)
    composite, _executor = _composite(uuid.uuid4(), uuid.uuid4())
    composite.add_tools(
        *html_render.build_render_html_tool(db=object(), user_id=str(uuid.uuid4()), conversation_id="c")
    )

    async def _call(**arguments):
        return await composite.execute_tool(ToolCall(id="p", name="render_html_to_image", arguments=arguments))

    mixed = await _call(pages=["work/a.html"], html="<div>x</div>")
    assert mixed.is_error and "leave html, source_file_id and png_workspace_path out" in mixed.content
    empty = await _call(pages=[])
    assert empty.is_error and "list of .html file paths" in empty.content
    too_many = await _call(pages=[f"work/p{n}.html" for n in range(html_render.MAX_BATCH_PAGES + 1)])
    assert too_many.is_error and f"at most {html_render.MAX_BATCH_PAGES}" in too_many.content
    none_rendered = await _call(pages=["work/nope.html"])
    assert none_rendered.is_error and "Rendered 0 of 1" in none_rendered.content


async def test_render_html_to_image_pages_without_a_workspace_is_refused(monkeypatch):
    from app.services.builtin_tools import html_render

    _defs, executor = html_render.build_render_html_tool(db=object(), user_id=str(uuid.uuid4()), conversation_id="c")
    result = await executor.execute_tool(
        ToolCall(id="p", name="render_html_to_image", arguments={"pages": ["work/a.html"]})
    )
    assert result.is_error and "this run has none" in result.content


async def test_workspace_write_file_is_counted_while_it_commits(store, monkeypatch):
    """WorkspaceIngestor.write_file is an in-flight write: a reader of the same round waits for it."""
    from app.services.sandbox_workspace.ingest import WorkspaceIngestor

    _composite_tool, executor = _composite(uuid.uuid4(), uuid.uuid4())
    session = executor.session
    seen: list[int] = []
    put_bytes = session.put_bytes

    async def _counting_put(*args, **kwargs):
        seen.append(session.writes_in_flight)
        return await put_bytes(*args, **kwargs)

    monkeypatch.setattr(session, "put_bytes", _counting_put)
    line = await WorkspaceIngestor(session).write_file("work/a.png", b"png", tool="render_html_to_image")
    assert "work/a.png" in line
    assert seen == [1] and session.writes_in_flight == 0


async def test_render_html_to_image_png_workspace_path_without_a_workspace_is_said():
    from app.services.builtin_tools import html_render

    assert await html_render._png_to_workspace("work/x.png", b"png") == (
        "PNG not written to the workspace: this run has no sandbox workspace.\n"
    )
    assert await html_render._png_to_workspace("", b"png") == ""
    assert await html_render._png_to_workspace(None, b"png") == ""


def _upload_tool(seen: list) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """A file-taking tool of some integration: ``file_id`` resolved through the file context, as the pipe does."""
    executor = CallableToolExecutor()

    async def upload_evidence(file_id: str, title: str = "", file_context: FileContext | None = None) -> str:
        resolved = await file_context.resolve_upload(file_id) if file_context is not None else None
        seen.append((file_id, resolved))
        return f"uploaded {resolved[1] if resolved else '?'}"

    executor.register("upload_evidence", upload_evidence)
    return [
        ToolDefinition(
            name="upload_evidence",
            description="upload",
            input_schema={
                "type": "object",
                "properties": {"file_id": {"type": "string"}, "title": {"type": "string"}},
                "required": ["file_id"],
            },
            source="integration:fixture",
            workspace_files=("file_id",),
        )
    ], executor


async def test_a_file_taking_tool_is_offered_workspace_path_only_with_a_workspace(store):
    composite, _ = _composite(uuid.uuid4(), uuid.uuid4())
    composite.add_tools(*_upload_tool([]))
    plain = CompositeToolExecutor()
    plain.add_tools(*_upload_tool([]))
    with_ws = {d.name: d for d in composite.all_tools}
    assert WORKSPACE_FILE_ARGUMENT in with_ws["upload_evidence"].input_schema["properties"]
    assert WORKSPACE_FILE_ARGUMENT not in plain.all_tools[0].input_schema["properties"]
    # The declaring definition is left as it was.
    assert WORKSPACE_FILE_ARGUMENT not in plain.get_tool_definition("upload_evidence").input_schema["properties"]


async def test_a_workspace_path_hands_the_bytes_to_the_tool_without_a_conversation_file(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    seen: list = []
    composite.add_tools(*_upload_tool(seen))
    await executor.session.put_bytes("out/report.csv", b"a,b\n1,2\n")
    before = {f.file_id for f in composite.file_context.list_files()}
    result = await composite.execute_tool(
        ToolCall(id="u1", name="upload_evidence", arguments={WORKSPACE_FILE_ARGUMENT: "workspace/out/report.csv", "title": "t"})
    )
    assert not result.is_error, result.content
    assert result.content == "uploaded report.csv"
    [(file_id, resolved)] = seen
    assert resolved[0] == b"a,b\n1,2\n" and resolved[1] == "report.csv" and resolved[2] == "text/csv"
    # The reference lived for the call only: no conversation file, no download marker.
    assert {f.file_id for f in composite.file_context.list_files()} == before
    assert composite.file_context.get(file_id) is None
    assert "[[FILE:" not in result.content


async def test_a_workspace_path_outside_the_workspace_or_missing_is_refused_before_the_tool_runs(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    seen: list = []
    composite.add_tools(*_upload_tool(seen))
    outside = await composite.execute_tool(
        ToolCall(id="u1", name="upload_evidence", arguments={WORKSPACE_FILE_ARGUMENT: "../etc/passwd"})
    )
    assert outside.is_error and "no empty or '..' segment in a workspace path" in outside.content
    missing = await composite.execute_tool(
        ToolCall(id="u2", name="upload_evidence", arguments={WORKSPACE_FILE_ARGUMENT: "nowhere.bin"})
    )
    assert missing.is_error and "is not in the workspace; list it with sandbox_list_files" in missing.content
    both = await composite.execute_tool(
        ToolCall(id="u3", name="upload_evidence", arguments={"file_id": "x", WORKSPACE_FILE_ARGUMENT: "nowhere.bin"})
    )
    assert both.is_error and "either file_id or workspace_path" in both.content
    assert seen == [], "nothing was run"


async def test_a_workspace_path_of_another_users_workspace_is_never_readable(store):
    owner, other = uuid.uuid4(), uuid.uuid4()
    conversation = uuid.uuid4()
    seed = WorkspaceSession(WorkspaceScope(user_id=owner, conversation_id=conversation), store=store)
    await seed.put_bytes("secret.txt", b"mine")
    composite, _ = _composite(conversation, other)
    seen: list = []
    composite.add_tools(*_upload_tool(seen))
    result = await composite.execute_tool(
        ToolCall(id="u1", name="upload_evidence", arguments={WORKSPACE_FILE_ARGUMENT: "secret.txt"})
    )
    assert result.is_error and seen == []
    assert b"mine" not in result.content.encode()
    assert str(conversation) not in result.content, "another person's workspace is never named"


async def test_a_path_only_another_workspace_of_the_person_holds_is_refused_with_where_it_is(store):
    user, writer, reader = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    seed = WorkspaceSession(WorkspaceScope(user_id=user, conversation_id=writer), store=store)
    await seed.put_bytes("work/fold/taxonomy.md", b"# families")
    composite, _ = _composite(reader, user)
    for tool in ("sandbox_read_file", "sandbox_get_file"):
        result = await composite.execute_tool(ToolCall(id=tool, name=tool, arguments={"path": "work/fold/taxonomy.md"}))
        assert result.is_error
        assert f"the workspace of conversation {writer}" in result.content
        assert "not shared between conversations or runs" in result.content
        assert "work item attachment" in result.content
        assert "# families" not in result.content


async def test_a_path_no_workspace_holds_keeps_the_plain_refusal(store):
    composite, _ = _composite(uuid.uuid4(), uuid.uuid4())
    result = await composite.execute_tool(
        ToolCall(id="r", name="sandbox_read_file", arguments={"path": "work/nowhere.md"})
    )
    assert result.is_error
    assert "workspace/work/nowhere.md is not in the workspace; list it with sandbox_list_files." in result.content
    assert "not shared" not in result.content


async def test_a_file_stored_in_a_workspace_is_read_by_a_later_turn_of_the_same_workspace(store):
    user, conversation = uuid.uuid4(), uuid.uuid4()
    seed = WorkspaceSession(WorkspaceScope(user_id=user, conversation_id=conversation), store=store)
    await seed.put_bytes("work/fold/taxonomy.md", b"# families\n")
    composite, _ = _composite(conversation, user)
    result = await composite.execute_tool(
        ToolCall(id="r", name="sandbox_read_file", arguments={"path": "work/fold/taxonomy.md"})
    )
    assert not result.is_error and "# families" in result.content


async def test_a_failed_lookup_of_the_other_workspaces_keeps_the_plain_refusal(store, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(store, "holders", broken)
    composite, _ = _composite(uuid.uuid4(), uuid.uuid4())
    result = await composite.execute_tool(ToolCall(id="r", name="sandbox_read_file", arguments={"path": "a.md"}))
    assert result.is_error and "workspace/a.md is not in the workspace" in result.content


async def test_create_github_commit_takes_files_workspace_path_in_place_of_file_id(store, monkeypatch):
    """The GitHub commit tool reads the parked reference through ``read_pipe_file`` like any file_id."""
    from app.integrations.providers.github import contents

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    await executor.session.put_bytes("work/src/a.py", b"a = 2\n")
    piped: list = []

    async def fake_read_pipe_file(file_context, file_id, *, max_bytes, target):
        resolved = await file_context.resolve_upload(file_id)
        piped.append(resolved)
        return "Error: stop here (fixture)"

    monkeypatch.setattr(contents, "read_pipe_file", fake_read_pipe_file)
    github = CallableToolExecutor()
    definitions = contents.register({"Authorization": "token x"}, github, lambda *a, **k: None)
    composite.add_tools(definitions, github)
    commit = composite.get_tool_definition("create_github_commit")
    offered = {d.name: d for d in composite.all_tools}["create_github_commit"]
    assert commit.workspace_files == ("files[].file_id",)
    assert WORKSPACE_FILE_ARGUMENT in offered.input_schema["properties"]["files"]["items"]["properties"]
    assert "from_checkout" in offered.input_schema["properties"]
    result = await composite.execute_tool(
        ToolCall(
            id="c1",
            name="create_github_commit",
            arguments={"repo": "acme/repo", "message": "feat: x", "files": [{"path": "src/a.py", WORKSPACE_FILE_ARGUMENT: "work/src/a.py"}]},
        )
    )
    assert "stop here (fixture)" in result.content
    assert piped == [(b"a = 2\n", "a.py", "text/x-python")]


# -- reads and edits over the workspace ---------------------------------------


async def _put(composite, path: str, data: bytes) -> None:
    ref = FileRef.from_bytes(data=data, filename=path.rsplit("/", 1)[-1], content_type=None)
    composite.file_context.add(ref)
    result = await composite.execute_tool(ToolCall(id=f"put-{path}", name="sandbox_put_file", arguments={"file_id": ref.file_id, "path": path}))
    assert not result.is_error, result.content


async def test_a_read_without_a_window_answers_its_first_part_and_the_line_to_continue(store):
    from app.services.sandbox_workspace import reading

    composite, _ = _composite(uuid.uuid4(), uuid.uuid4())
    # 300 lines of about 120 characters: 36 KB, over the default window's character bound.
    body = "".join(f"line {i:03d} " + "x" * 110 + "\n" for i in range(1, 301))
    await _put(composite, "src/spec.md", body.encode())

    read = await composite.execute_tool(ToolCall(id="r", name="sandbox_read_file", arguments={"path": "src/spec.md"}))
    assert not read.is_error, read.content
    assert len(read.content) < reading.DEFAULT_WINDOW_CHARS + 1_000
    header = read.content.splitlines()[0]
    last = int(header.split("lines 1-", 1)[1].split(" ", 1)[0])
    assert last < reading.DEFAULT_WINDOW_LINES
    assert "300 lines in total" in read.content
    assert f"continue with offset_line={last + 1}" in read.content

    # An explicit window answers what was asked, up to the result cap.
    asked = await composite.execute_tool(
        ToolCall(id="a", name="sandbox_read_file", arguments={"path": "src/spec.md", "max_lines": 300})
    )
    assert not asked.is_error, asked.content
    assert asked.content.startswith("workspace/src/spec.md: lines 1-300 of 300")
    assert "window cut" not in asked.content


async def test_read_grep_and_outline_never_echo_the_whole_file(store):
    composite, _ = _composite(uuid.uuid4(), uuid.uuid4())
    body = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(100))
    await _put(composite, "src/m.py", body.encode())
    await _put(composite, "src/blob.bin", b"\x00\x01binary")
    read = await composite.execute_tool(ToolCall(id="r", name="sandbox_read_file", arguments={"path": "src/m.py", "offset_line": 4, "max_lines": 2}))
    assert not read.is_error, read.content
    assert read.content.startswith("workspace/src/m.py: lines 4-5 of 300")
    assert "4 | def f1():" in read.content and "def f2" not in read.content
    assert "continue with offset_line=6" in read.content
    binary = await composite.execute_tool(ToolCall(id="b", name="sandbox_read_file", arguments={"path": "src/blob.bin"}))
    assert binary.is_error and "is binary" in binary.content
    missing = await composite.execute_tool(ToolCall(id="m", name="sandbox_read_file", arguments={"path": "src/nope.py"}))
    assert missing.is_error and "is not in the workspace" in missing.content
    grep = await composite.execute_tool(
        ToolCall(id="g", name="sandbox_grep", arguments={"pattern": r"return 9\d", "path_prefix": "src", "max_matches": 5, "include_globs": ["*.py"]})
    )
    assert not grep.is_error, grep.content
    assert grep.content.startswith("5 match(es) for 'return 9\\\\d' under workspace/src/ in 1 file(s), 1 searched:")
    assert "workspace/src/m.py:272:     return 90" in grep.content
    assert "[5 more match(es) not shown" in grep.content
    assert "def f0" not in grep.content
    outline = await composite.execute_tool(ToolCall(id="o", name="sandbox_outline", arguments={"path": "src/m.py"}))
    assert not outline.is_error, outline.content
    assert outline.content.startswith("workspace/src/m.py (300 lines): 100 definition(s)") and "def f99" in outline.content
    assert "return" not in outline.content
    bad = await composite.execute_tool(ToolCall(id="p", name="sandbox_grep", arguments={"pattern": "("}))
    assert bad.is_error and "does not compile" in bad.content
    symbol = await composite.execute_tool(ToolCall(id="s", name="sandbox_read_symbol", arguments={"path": "src/m.py", "symbol": "f42"}))
    assert not symbol.is_error, symbol.content
    assert symbol.content.startswith("def f42 (lines 127-128):\nworkspace/src/m.py: lines 127-128 of 300")
    assert "return 42" in symbol.content and "def f43" not in symbol.content and "def f41" not in symbol.content
    absent = await composite.execute_tool(ToolCall(id="n", name="sandbox_read_symbol", arguments={"path": "src/m.py", "symbol": "f4x"}))
    assert absent.is_error and "no definition named 'f4x'" in absent.content
    near = await composite.execute_tool(ToolCall(id="q", name="sandbox_read_symbol", arguments={"path": "src/m.py", "symbol": "f9"}))
    assert not near.is_error and "return 9" in near.content and "def f99" not in near.content


async def test_grep_searches_a_job_log_past_the_text_limit_and_skips_images_unread(store, monkeypatch):
    """A 5 MB log is searched; a .png is never read; an oversized file is skipped, the search goes on."""
    from app.services.sandbox_workspace import reading

    composite, _ = _composite(uuid.uuid4(), uuid.uuid4())
    log = ("ok step\n" * 700_000 + "E   AssertionError: boom\n").encode()
    assert len(log) > reading.MAX_TEXT_FILE_BYTES
    await _put(composite, "logs/job.log", log)
    await _put(composite, "logs/shot.png", b"E   AssertionError: in a picture\n")
    await _put(composite, "logs/z.txt", b"E   AssertionError: after\n")
    read: list[str] = []
    original = store.read_blob

    async def counting(workspace_id, sha256):
        read.append(sha256)
        return await original(workspace_id, sha256)

    monkeypatch.setattr(store, "read_blob", counting)
    grep = await composite.execute_tool(ToolCall(id="g", name="sandbox_grep", arguments={"pattern": "AssertionError", "path_prefix": "logs"}))
    assert not grep.is_error, grep.content
    assert "workspace/logs/job.log:700001: E   AssertionError: boom" in grep.content
    assert "workspace/logs/z.txt:1:" in grep.content and "shot.png" not in grep.content
    assert "[1 binary or oversized file(s) skipped]" in grep.content
    assert len(read) == 2
    monkeypatch.setattr(reading, "MAX_GREP_FILE_BYTES", 1024)
    small = await composite.execute_tool(ToolCall(id="h", name="sandbox_grep", arguments={"pattern": "AssertionError", "path_prefix": "logs"}))
    assert "workspace/logs/z.txt:1:" in small.content and "job.log" not in small.content
    assert "[2 binary or oversized file(s) skipped]" in small.content and "stopped" not in small.content


async def test_grep_past_its_byte_budget_names_what_it_did_not_reach(store, monkeypatch):
    from app.services.sandbox_workspace import reading

    composite, _ = _composite(uuid.uuid4(), uuid.uuid4())
    for i in range(6):
        await _put(composite, f"src/a/f{i}.py", f"x = {i}  # needle\n".encode() + b"#" * 1000 + b"\n")
    await _put(composite, "src/b/g.py", b"needle = 1\n")
    monkeypatch.setattr(reading, "MAX_GREP_BYTES", 2560)  # two 1,017-byte files fit, the third does not
    monkeypatch.setattr(reading, "GREP_READ_FILES", 2)
    grep = await composite.execute_tool(ToolCall(id="g", name="sandbox_grep", arguments={"pattern": "needle", "path_prefix": "src"}))
    assert not grep.is_error, grep.content
    lines = [line for line in grep.content.splitlines() if ": x = " in line]
    assert lines == [f"workspace/src/a/f{i}.py:1: x = {i}  # needle" for i in range(2)]
    assert (
        "[search stopped early: 2.5 KB read; 5 file(s) not searched, in workspace/src/a/ (4), workspace/src/b/ (1); "
        "the first is workspace/src/a/f2.py. Narrow path_prefix or include_globs to search the rest]"
    ) in grep.content


async def test_grep_past_its_time_budget_names_what_it_did_not_reach(store, monkeypatch):
    """The workspace search stops at the call's time budget, not at a file count (#758), and names the rest."""
    from app.services.sandbox_workspace import commands, reading

    composite, _ = _composite(uuid.uuid4(), uuid.uuid4())
    for i in range(3):
        await _put(composite, f"src/f{i}.py", b"needle = 1\n")
    assert reading.MAX_GREP_FILES >= 20_000  # a whole checkout is not cut by a file count
    monkeypatch.setattr(commands, "wait_budget", lambda *a, **k: 0.0)
    grep = await composite.execute_tool(
        ToolCall(id="g", name="sandbox_grep", arguments={"pattern": "needle", "path_prefix": "src"})
    )
    assert not grep.is_error, grep.content
    assert "search stopped early: the call's time budget ran out; 3 file(s) not searched" in grep.content
    assert "the first is workspace/src/f0.py" in grep.content


def test_plan_grep_batches_by_count_and_bytes_and_keeps_path_order():
    from app.services.sandbox_workspace import reading

    out = reading.GrepResult()
    sized = [(f"f{i}.py", 10) for i in range(10)] + [("big.log", reading.GREP_READ_BYTES + 1), ("img.PNG", 5), ("z.py", 1)]
    batches = reading.plan_grep(sized, out)
    assert batches == [[f"f{i}.py" for i in range(8)], ["f8.py", "f9.py"], ["big.log"], ["z.py"]]
    assert out.files_skipped == 1 and not out.stopped and not out.left


async def test_grep_and_read_in_the_same_round_as_an_edit_see_the_edited_file(store, monkeypatch):
    """The calls of one response run together: a search next to an edit reads what the edit commits."""
    import asyncio

    composite, _ = _composite(uuid.uuid4(), uuid.uuid4())
    await _put(composite, "src/big.json", b'{"k": "old value"}\n' + b" " * 200_000 + b"\n")
    original = store.commit

    async def slow_commit(*args, **kwargs):
        await asyncio.sleep(0.2)  # an object store round trip: the edit is still in flight when the search starts
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "commit", slow_commit)
    edit, grep, read = await asyncio.gather(
        composite.execute_tool(
            ToolCall(id="e", name="sandbox_edit_file", arguments={"path": "src/big.json", "edits": [{"find": "old value", "replace": "new value"}]})
        ),
        composite.execute_tool(ToolCall(id="g", name="sandbox_grep", arguments={"pattern": "value", "path_prefix": "src"})),
        composite.execute_tool(ToolCall(id="r", name="sandbox_read_file", arguments={"path": "src/big.json", "max_lines": 1})),
    )
    assert not edit.is_error, edit.content
    assert not grep.is_error, grep.content
    assert 'workspace/src/big.json:1: {"k": "new value"}' in grep.content and "old value" not in grep.content
    assert not read.is_error and "new value" in read.content and "Object not found" not in read.content


async def test_grep_whose_manifest_names_a_dropped_blob_reads_the_current_one(store):
    """A manifest read before a commit (a resumed turn, another worker) names a blob the commit dropped: re-resolved."""
    composite, executor = _composite(uuid.uuid4(), uuid.uuid4())
    session = executor.session
    await _put(composite, "src/a.py", b"needle = 1\n")
    stale = await session.entries(refresh=True)
    await session.put_bytes("src/a.py", b"needle = 2\n")  # the first blob is orphaned and dropped
    real = session.entries
    calls = 0

    async def entries_stale_once(*, refresh: bool = False):
        nonlocal calls
        calls += 1
        return dict(stale) if calls == 1 else await real(refresh=refresh)

    session.entries = entries_stale_once
    grep = await composite.execute_tool(ToolCall(id="g", name="sandbox_grep", arguments={"pattern": "needle", "path_prefix": "src"}))
    assert not grep.is_error, grep.content
    assert "workspace/src/a.py:1: needle = 2" in grep.content and "Object not found" not in grep.content
    entry, data = await session.read_entry(stale["src/a.py"])
    assert data == b"needle = 2\n" and entry.sha256 != stale["src/a.py"].sha256


async def test_grep_names_a_file_whose_content_stays_missing_and_searches_the_others(store):
    from app.services.sandbox_workspace.store import blob_key

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "src/a.py", b"needle = 'a'\n")
    await _put(composite, "src/b.py", b"needle = 'b'\n")
    (record,) = [r for r in store.records.values() if r.conversation_id == conversation]
    store.blobs.objects.pop(blob_key(record.id, store.files[record.id]["src/b.py"].sha256))
    grep = await composite.execute_tool(ToolCall(id="g", name="sandbox_grep", arguments={"pattern": "needle", "path_prefix": "src"}))
    assert not grep.is_error, grep.content
    assert "workspace/src/a.py:1: needle = 'a'" in grep.content
    assert "[1 file(s) not searched, their content missing from storage: workspace/src/b.py;" in grep.content
    assert "sandbox-workspaces/" not in grep.content
    read = await composite.execute_tool(ToolCall(id="r", name="sandbox_read_file", arguments={"path": "src/b.py"}))
    assert read.is_error and "workspace/src/b.py could not be read" in read.content and "sandbox-workspaces/" not in read.content


async def test_edit_file_applies_every_edit_or_none_and_returns_the_diff(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "src/a.py", b"a = 1\nb = 2\nc = a + b\n")
    refused = await composite.execute_tool(
        ToolCall(
            id="e1",
            name="sandbox_edit_file",
            arguments={"path": "src/a.py", "edits": [{"find": "a = 1", "replace": "a = 10"}, {"find": "zzz", "replace": ""}]},
        )
    )
    assert refused.is_error and "edit 2: 'find' text not found" in refused.content
    assert await _workspace_bytes(store, conversation, "src/a.py") == b"a = 1\nb = 2\nc = a + b\n"
    edited = await composite.execute_tool(
        ToolCall(
            id="e2",
            name="sandbox_edit_file",
            arguments={"path": "src/a.py", "edits": [{"find": "a = 1", "replace": "a = 10"}, {"find": "a + b", "replace": "a * b"}]},
        )
    )
    assert not edited.is_error, edited.content
    assert edited.content.startswith("Edited workspace/src/a.py: 2 edit(s), 2 replacement(s), +2 -2 lines")
    assert "--- a/src/a.py\n+++ b/src/a.py\n" in edited.content and "-a = 1\n+a = 10\n" in edited.content
    assert await _workspace_bytes(store, conversation, "src/a.py") == b"a = 10\nb = 2\nc = a * b\n"
    ambiguous = await composite.execute_tool(
        ToolCall(id="e3", name="sandbox_edit_file", arguments={"path": "src/a.py", "edits": [{"find": "= ", "replace": "="}]})
    )
    assert ambiguous.is_error and "occurs 3 times" in ambiguous.content
    everywhere = await composite.execute_tool(
        ToolCall(id="e4", name="sandbox_edit_file", arguments={"path": "src/a.py", "edits": [{"find": "= ", "replace": "=", "replace_all": True}]})
    )
    assert not everywhere.is_error and "3 replacement(s)" in everywhere.content
    assert await _workspace_bytes(store, conversation, "src/a.py") == b"a =10\nb =2\nc =a * b\n"
    same = await composite.execute_tool(
        ToolCall(id="e5", name="sandbox_edit_file", arguments={"path": "src/a.py", "edits": [{"find": "b =2", "replace": "b =2"}]})
    )
    assert not same.is_error and "unchanged" in same.content and "Nothing was written" in same.content


async def test_edit_file_takes_a_replacement_from_a_workspace_file(store: MemoryWorkspaceStore) -> None:
    """replace_from: a section drafted in work/ lands in a source file without its text in the call."""
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "docs/guide.md", b"# Guide\n\n## End\n")
    await _put(composite, "work/section.md", b"## Drafted\n\nBody.\n\n")
    inserted = await composite.execute_tool(
        ToolCall(
            id="f1",
            name="sandbox_edit_file",
            arguments={
                "path": "docs/guide.md",
                "edits": [
                    {
                        "insert_after_line": 2,
                        "expect_first_blank": True,
                        "replace": "",
                        "replace_from": "work/section.md",
                    }
                ],
            },
        )
    )
    assert not inserted.is_error, inserted.content
    assert await _workspace_bytes(store, conversation, "docs/guide.md") == b"# Guide\n\n## Drafted\n\nBody.\n\n## End\n"
    both = await composite.execute_tool(
        ToolCall(
            id="f2",
            name="sandbox_edit_file",
            arguments={
                "path": "docs/guide.md",
                "edits": [{"find": "## End", "replace": "x", "replace_from": "work/section.md"}],
            },
        )
    )
    assert both.is_error and "either 'replace' or 'replace_from'" in both.content
    absent = await composite.execute_tool(
        ToolCall(
            id="f3",
            name="sandbox_edit_file",
            arguments={
                "path": "docs/guide.md",
                "edits": [{"find": "## End", "replace": "", "replace_from": "work/absent.md"}],
            },
        )
    )
    assert absent.is_error and "work/absent.md" in absent.content
    assert await _workspace_bytes(store, conversation, "docs/guide.md") == b"# Guide\n\n## Drafted\n\nBody.\n\n## End\n"


async def test_json_keys_changes_every_catalog_in_one_commit_or_none(store):
    """sandbox_json_keys: one key set, renamed and removed in two catalogs; a bad file refuses the whole call."""
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "i18n/en.json", b'{\n  "Save": "Save",\n  "Old": "Old"\n}\n')
    await _put(composite, "i18n/fr.json", b'{\n  "Save": "Enregistrer",\n  "Old": "Ancien"\n}\n')
    done = await composite.execute_tool(
        ToolCall(
            id="j1",
            name="sandbox_json_keys",
            arguments={
                "paths": ["i18n/en.json", "i18n/fr.json"],
                "rename": {"Save": "Save changes"},
                "set": {"i18n/en.json": {"New": "New"}, "i18n/fr.json": {"New": "Nouveau"}},
                "remove": ["Old"],
                "get": ["New"],
            },
        )
    )
    assert not done.is_error, done.content
    assert done.content.startswith("Wrote 2 of 2 file(s) in one commit")
    assert '"New": "Nouveau"' in done.content
    assert await _workspace_bytes(store, conversation, "i18n/fr.json") == b'{\n  "Save changes": "Enregistrer",\n  "New": "Nouveau"\n}\n'
    await _put(composite, "i18n/bad.json", b"[1]\n")
    refused = await composite.execute_tool(
        ToolCall(id="j2", name="sandbox_json_keys", arguments={"paths": ["i18n/en.json", "i18n/bad.json"], "set": {"*": {"K": "V"}}})
    )
    assert refused.is_error and "not a JSON object" in refused.content and "Nothing was written" in refused.content
    assert b'"K"' not in await _workspace_bytes(store, conversation, "i18n/en.json")


async def test_json_keys_reads_the_set_from_a_workspace_file(store):
    """set_from: the set object is read from a workspace JSON file; set and set_from together are refused."""
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "i18n/en.json", b'{\n  "Save": "Save"\n}\n')
    await _put(composite, "i18n/fr.json", b'{\n  "Save": "Enregistrer"\n}\n')
    await _put(composite, "work/set.json", b'{"i18n/en.json": {"New": "New"}, "i18n/fr.json": {"New": "Nouveau"}}')
    done = await composite.execute_tool(
        ToolCall(
            id="s1",
            name="sandbox_json_keys",
            arguments={"paths": ["i18n/en.json", "i18n/fr.json"], "set_from": "work/set.json"},
        )
    )
    assert not done.is_error, done.content
    assert done.content.startswith("Wrote 2 of 2 file(s) in one commit")
    assert (
        await _workspace_bytes(store, conversation, "i18n/fr.json")
        == b'{\n  "Save": "Enregistrer",\n  "New": "Nouveau"\n}\n'
    )
    both = await composite.execute_tool(
        ToolCall(
            id="s2",
            name="sandbox_json_keys",
            arguments={"paths": ["i18n/en.json"], "set": {"*": {"K": "V"}}, "set_from": "work/set.json"},
        )
    )
    assert both.is_error and "either 'set' or 'set_from'" in both.content
    await _put(composite, "work/list.json", b"[1]")
    wrong = await composite.execute_tool(
        ToolCall(id="s3", name="sandbox_json_keys", arguments={"paths": ["i18n/en.json"], "set_from": "work/list.json"})
    )
    assert wrong.is_error and "must hold a JSON object" in wrong.content
    assert b'"K"' not in await _workspace_bytes(store, conversation, "i18n/en.json")


async def test_edit_file_changes_a_crlf_file_and_keeps_its_endings(store):
    """A file with Windows line endings: the find copied from sandbox_read_file (LF) matches, CRLF is kept."""
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "tests/t.py", b"def test_a():\r\n    assert 1 == 1\r\n")
    edited = await composite.execute_tool(
        ToolCall(
            id="c1",
            name="sandbox_edit_file",
            arguments={"path": "tests/t.py", "edits": [{"find": "def test_a():\n    assert 1 == 1", "replace": "def test_a():\n    assert 2 == 2"}]},
        )
    )
    assert not edited.is_error, edited.content
    assert "1 replacement(s), +1 -1 lines" in edited.content
    assert await _workspace_bytes(store, conversation, "tests/t.py") == b"def test_a():\r\n    assert 2 == 2\r\n"


async def test_edit_file_replaces_a_line_range_checked_by_its_anchors(store):
    """A long block replaced by line numbers: a stale number is refused, nothing written."""
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "src/b.py", b"def keep():\n    pass\n\n\ndef old():\n    a = 1\n    return a\n\n\ndef tail():\n    pass\n")
    stale = await _call(
        composite,
        "sandbox_edit_file",
        path="src/b.py",
        edits=[{"start_line": 4, "end_line": 7, "expect_first": "def old", "expect_last": "return a", "replace": ""}],
    )
    assert stale.is_error and "line 4" in stale.content and "stale" in stale.content
    deleted = await _call(
        composite,
        "sandbox_edit_file",
        path="src/b.py",
        edits=[{"start_line": 5, "end_line": 9, "expect_first": "def old", "expect_last": "", "replace": ""}],
    )
    assert deleted.is_error and "expect_last" in deleted.content
    replaced = await _call(
        composite,
        "sandbox_edit_file",
        path="src/b.py",
        edits=[
            {"start_line": 5, "end_line": 7, "expect_first": "def old", "expect_last": "return a", "replace": "def new():\n    return 2"},
            {"find": "def tail", "replace": "def end"},
        ],
    )
    assert not replaced.is_error, replaced.content
    assert "2 edit(s), 2 replacement(s)" in replaced.content and "-def old():\n" in replaced.content
    assert await _workspace_bytes(store, conversation, "src/b.py") == (
        b"def keep():\n    pass\n\n\ndef new():\n    return 2\n\n\ndef end():\n    pass\n"
    )


async def test_diff_files_diffs_two_files_and_merges_three(store):
    """Diff -u of two workspace files, diff3 -m of three, the merge written on request."""
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    base = b"a = 1\nb = 2\nc = 3\n"
    await _put(composite, "m/base.py", base)
    await _put(composite, "m/pr.py", b"a = 10\nb = 2\nc = 3\n")
    await _put(composite, "m/main.py", b"a = 1\nb = 2\nc = 30\n")
    await _put(composite, "m/clash.py", b"a = 11\nb = 2\nc = 3\n")
    await _put(composite, "m/blob.bin", b"\x00\x01binary")
    two = await _call(composite, "sandbox_diff_files", path="m/pr.py", other="m/main.py")
    assert not two.is_error, two.content
    assert two.content.startswith("workspace/m/pr.py -> workspace/m/main.py: +2 -2 lines.")
    assert "--- workspace/m/pr.py\n+++ workspace/m/main.py\n" in two.content and "-a = 10\n+a = 1\n" in two.content
    clean = await _call(
        composite, "sandbox_diff_files", path="m/pr.py", other="m/main.py", base="m/base.py", write_merge_to="m/merged.py"
    )
    assert not clean.is_error, clean.content
    assert "merged cleanly, no conflict. Written to workspace/m/merged.py" in clean.content
    assert await _workspace_bytes(store, conversation, "m/merged.py") == b"a = 10\nb = 2\nc = 30\n"
    clash = await _call(composite, "sandbox_diff_files", path="m/pr.py", other="m/clash.py", base="m/base.py")
    assert not clash.is_error, clash.content
    assert "1 conflict(s), markers at line(s) 1 of the merged text." in clash.content
    assert "+<<<<<<< workspace/m/pr.py\n" in clash.content and "+>>>>>>> workspace/m/clash.py\n" in clash.content
    assert "Written" not in clash.content
    assert await _workspace_bytes(store, conversation, "m/pr.py") == b"a = 10\nb = 2\nc = 3\n"
    # Written with a conflict: the hunks to settle, numbered, never the merged-against-ours diff.
    held = await _call(
        composite,
        "sandbox_diff_files",
        path="m/pr.py",
        other="m/clash.py",
        base="m/base.py",
        write_merge_to="m/held.py",
    )
    assert not held.is_error, held.content
    assert "Written to workspace/m/held.py" in held.content and "@@ lines 1-" in held.content
    assert "     1 | <<<<<<< workspace/m/pr.py\n" in held.content and "--- workspace/m/pr.py" not in held.content
    assert "Call again without write_merge_to" in clean.content and "+++ merged" not in clean.content
    no_base = await _call(composite, "sandbox_diff_files", path="m/pr.py", other="m/main.py", write_merge_to="m/x.py")
    assert no_base.is_error and "write_merge_to needs base" in no_base.content
    missing = await _call(composite, "sandbox_diff_files", path="m/pr.py", other="m/nope.py")
    assert missing.is_error and "is not in the workspace" in missing.content
    binary = await _call(composite, "sandbox_diff_files", path="m/pr.py", other="m/blob.bin")
    assert binary.is_error and "is binary" in binary.content
    empty = await _call(composite, "sandbox_diff_files", path="m/pr.py", other=" ")
    assert empty.is_error and "name two workspace files" in empty.content


def test_the_read_tools_are_offered_with_the_workspace_only():
    with_ws, _ = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    names = {d.name for d in with_ws}
    assert {"sandbox_read_file", "sandbox_grep", "sandbox_outline", "sandbox_read_symbol", "sandbox_edit_file", "sandbox_diff_files"} <= names
    diff_files = next(d for d in with_ws if d.name == "sandbox_diff_files")
    assert diff_files.input_schema["required"] == ["path", "other"]
    symbol = next(d for d in with_ws if d.name == "sandbox_read_symbol")
    assert symbol.input_schema["required"] == ["path", "symbol"]
    edit = next(d for d in with_ws if d.name == "sandbox_edit_file")
    assert edit.input_schema["required"] == ["path", "edits"]
    items = edit.input_schema["properties"]["edits"]["items"]
    assert items["required"] == ["replace"]
    assert {
        "find",
        "start_line",
        "end_line",
        "expect_first",
        "expect_last",
        "expect_first_blank",
        "expect_last_blank",
        "insert_after_line",
    } <= set(items["properties"])
    without, _ = build_code_interpreter_tool(user_id=None, conversation_id=None)
    assert {d.name for d in without} == {"code_interpreter"}


async def test_write_file_creates_a_new_file_stored_before_the_answer(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    text = "def test_one():\n    assert 1 + 1 == 2\n"
    written = await _call(composite, "sandbox_write_file", path="repo/tests/test_new.py", content=text)
    assert not written.is_error, written.content
    digest = hashlib.sha256(text.encode()).hexdigest()
    assert written.content.startswith(f"Created workspace/repo/tests/test_new.py: 2 line(s), {len(text)} B, sha256 {digest}.")
    # Stored (manifest committed) before the answer: the next tool finds it.
    assert await _workspace_bytes(store, conversation, "repo/tests/test_new.py") == text.encode()
    read = await _call(composite, "sandbox_read_file", path="repo/tests/test_new.py")
    assert not read.is_error and "2 |     assert 1 + 1 == 2" in read.content


async def test_write_file_refuses_an_existing_file_unless_overwrite(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "src/a.py", b"a = 1\n")
    refused = await _call(composite, "sandbox_write_file", path="src/a.py", content="a = 2\n")
    assert refused.is_error and "already exists" in refused.content and "overwrite=true" in refused.content
    assert await _workspace_bytes(store, conversation, "src/a.py") == b"a = 1\n"
    replaced = await _call(composite, "sandbox_write_file", path="src/a.py", content="a = 2\n", overwrite=True)
    assert not replaced.is_error and replaced.content.startswith("Replaced workspace/src/a.py: 1 line(s)")
    assert await _workspace_bytes(store, conversation, "src/a.py") == b"a = 2\n"
    on_dir = await _call(composite, "sandbox_write_file", path="src", content="x", overwrite=True)
    assert on_dir.is_error and "is a directory of the workspace" in on_dir.content
    under_file = await _call(composite, "sandbox_write_file", path="src/a.py/b.py", content="x")
    assert under_file.is_error and "is a file, so it cannot hold" in under_file.content


async def test_write_file_stays_inside_the_workspace_and_honours_create_dirs(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    for bad in ("../escape.py", "src/../../escape.py", "/etc/passwd", "src\\a.py"):
        refused = await _call(composite, "sandbox_write_file", path=bad, content="x")
        assert refused.is_error, bad
    no_dirs = await _call(composite, "sandbox_write_file", path="new/dir/a.py", content="x", create_dirs=False)
    assert no_dirs.is_error and "create_dirs is false" in no_dirs.content
    await _put(composite, "old/keep.py", b"k\n")
    in_existing = await _call(composite, "sandbox_write_file", path="old/b.py", content="b\n", create_dirs=False)
    assert not in_existing.is_error, in_existing.content
    surrogate = await _call(composite, "sandbox_write_file", path="s.txt", content="bad \ud800")
    assert surrogate.is_error and "not valid UTF-8" in surrogate.content


def test_the_write_tool_is_offered_with_the_workspace():
    with_ws, _ = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    write = next(d for d in with_ws if d.name == "sandbox_write_file")
    assert write.input_schema["required"] == ["path", "content"]
    assert set(write.input_schema["properties"]) == {"path", "content", "create_dirs", "overwrite"}


# -- GitHub reads: windows, workspace-bound diffs and logs ---------------------


def _github_file_tool(raw: bytes, monkeypatch):
    from app.integrations.providers.github import contents

    async def _branch(client, headers, repo, owner, name, branch):
        return branch or "main", None

    async def _request(client, method, url, provider, action, **kwargs):
        meta = {"type": "file", "size": len(raw), "encoding": "base64", "content": base64.b64encode(raw).decode(), "html_url": "h"}
        return SimpleNamespace(status_code=200, json=lambda: meta), None

    monkeypatch.setattr(contents, "_resolve_branch", _branch)
    monkeypatch.setattr(contents._common, "safe_request", _request)
    gh = CallableToolExecutor()
    definitions = contents.register({"Authorization": f"Bearer {TOKEN}"}, gh, None, None)
    return definitions, gh


def _window_text(answer: str) -> str:
    """The characters of the file a ``get_github_file`` answer shows: its header and its tail left out."""
    body = answer.split("\n\n", 1)[1]
    return body.split("\n\n[", 1)[0]


async def test_get_github_file_returns_a_window_and_what_remains(monkeypatch):
    from app.integrations.providers.github import contents

    text = ("x" * 100 + "\n") * 10
    definitions, gh = _github_file_tool(text.encode(), monkeypatch)
    props = next(d for d in definitions if d.name == "get_github_file").input_schema["properties"]
    assert {"offset", "max_chars"} <= set(props)
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, gh)
    first = await composite.execute_tool(ToolCall(id="1", name="get_github_file", arguments={"repo": "acme/repo", "path": "big.txt", "max_chars": 250}))
    assert first.content.startswith("File: big.txt (size: 1,010 chars, branch: main) - chars 0-250")
    assert _window_text(first.content) == text[:250]
    assert "[760 more chars: continue with offset=250" in first.content and "to_workspace_path" in first.content
    last = await composite.execute_tool(ToolCall(id="2", name="get_github_file", arguments={"repo": "acme/repo", "path": "big.txt", "offset": 1000}))
    assert _window_text(last.content) == text[1000:] and "more chars" not in last.content
    whole = await composite.execute_tool(ToolCall(id="3", name="get_github_file", arguments={"repo": "acme/repo", "path": "big.txt"}))
    assert _window_text(whole.content) == text and " - chars " not in whole.content
    assert contents._clamp_window(None, 10**9) == (0, contents._MAX_FILE_CHARS)


async def test_pr_diff_and_job_logs_land_in_the_workspace_uncut(store, monkeypatch):
    from app.integrations.providers.github import actions, pulls

    diff = "diff --git a/x b/x\n" + "+line\n" * 50_000
    log = "##[group]Run pytest\n##[endgroup]\n" + "ok\n" * 30_000 + "=== FAILURES ===\nFAILED t.py::t\n##[error]Process completed with exit code 1.\n"

    async def _request(client, method, url, provider, action, **kwargs):
        return SimpleNamespace(status_code=200, text=diff if "/pulls/" in url else log, json=lambda: {}), None

    monkeypatch.setattr(pulls._common, "safe_request", _request)
    monkeypatch.setattr(actions._common, "safe_request", _request)
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    gh = CallableToolExecutor()
    definitions = [*pulls.register({}, gh, None), *actions.register({}, gh, None)]
    composite.add_tools(definitions, gh)
    by_name = {d.name: d for d in definitions}
    assert by_name["get_github_pr_diff"].workspace_ingest and by_name["get_github_job_logs"].workspace_ingest
    written = await composite.execute_tool(
        ToolCall(id="d", name="get_github_pr_diff", arguments={"repo": "acme/repo", "number": 7, WORKSPACE_PATH_ARGUMENT: "pr/7.diff"})
    )
    assert not written.is_error, written.content
    assert "+line" not in written.content
    assert (await _workspace_bytes(store, conversation, "pr/7.diff")).decode() == diff
    logged = await composite.execute_tool(
        ToolCall(id="l", name="get_github_job_logs", arguments={"repo": "acme/repo", "job_id": 9, WORKSPACE_PATH_ARGUMENT: "ci/9.log"})
    )
    assert not logged.is_error, logged.content
    assert (await _workspace_bytes(store, conversation, "ci/9.log")).decode() == log
    cut = await composite.execute_tool(ToolCall(id="c", name="get_github_pr_diff", arguments={"repo": "acme/repo", "number": 7}))
    assert "truncated" in cut.content and "to_workspace_path" in cut.content and len(cut.content) < 81_000
    summary = await composite.execute_tool(
        ToolCall(id="s", name="get_github_job_logs", arguments={"repo": "acme/repo", "job_id": 9, "failures_only": True})
    )
    assert summary.content.startswith("Logs for job 9 in acme/repo: ") and "'Run pytest'" in summary.content
    assert "FAILED t.py::t" in summary.content and "exit code 1" in summary.content
    assert summary.content.count("ok\n") == 0 and len(summary.content) < 1000
    # The default answers the failures first, then the log's last lines; tail=true keeps the raw tail.
    default = await composite.execute_tool(ToolCall(id="f", name="get_github_job_logs", arguments={"repo": "acme/repo", "job_id": 9}))
    assert default.content.startswith("Logs for job 9 in acme/repo: ") and "FAILED t.py::t" in default.content
    assert "--- the log's last" in default.content and len(default.content) < 2_000
    tail = await composite.execute_tool(ToolCall(id="t", name="get_github_job_logs", arguments={"repo": "acme/repo", "job_id": 9, "tail": True}))
    assert tail.content.startswith("Logs for job 9 in acme/repo:\n\n... [truncated to last 40,000 chars of ") and "to_workspace_path" in tail.content


async def test_query_github_graphql_lands_in_the_workspace_uncut(store, monkeypatch):
    from app.integrations.providers.github import _common, graphql

    nodes = [{"number": n, "title": "t" * 300} for n in range(400)]

    async def _gql(client, headers, query, variables=None, **kwargs):
        return {"search": {"nodes": nodes}}, None

    monkeypatch.setattr(_common, "gh_graphql", _gql)
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    gh = CallableToolExecutor()
    definitions = graphql.register({}, gh, None)
    composite.add_tools(definitions, gh)
    by_name = {d.name: d for d in definitions}
    assert by_name["query_github_graphql"].workspace_ingest
    assert not by_name["github_graphql_query"].workspace_ingest  # a write tool keeps its answer
    document = "query { search(query: \"repo:a/b\", type: ISSUE, first: 100) { nodes { ... on Issue { number title } } } }"
    written = await composite.execute_tool(
        ToolCall(id="g", name="query_github_graphql", arguments={"query": document, WORKSPACE_PATH_ARGUMENT: "gql/search.json"})
    )
    assert not written.is_error, written.content
    assert "tttt" not in written.content
    stored = json.loads((await _workspace_bytes(store, conversation, "gql/search.json")).decode())
    assert len(stored["search"]["nodes"]) == 400
    cut = await composite.execute_tool(ToolCall(id="c", name="query_github_graphql", arguments={"query": document}))
    assert "truncated at" in cut.content and "to_workspace_path" in cut.content


def _frozen(name: str) -> zipfile.ZipInfo:
    # A fixed mod-time: writestr("<name>", ...) stamps time.localtime(), so two builds
    # straddling a 2-second DOS tick differ byte for byte (issue #9151).
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.external_attr = 0o600 << 16
    return info


def _artifact_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(_frozen("index.html"), "<html>report</html>")
        archive.writestr(_frozen("data/trace-1.zip"), b"PK\x03\x04trace")
        archive.writestr(_frozen("../escape.txt"), "no")
        link = _frozen("link")
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "/etc/passwd")
    return buffer.getvalue()


def _artifact_api(sent: list, *, expired: bool = False, size: int = 1024):
    zipped = _artifact_zip()

    async def _request(client, method, url, provider, action, **kwargs):
        sent.append(url)
        if url.endswith("/zip"):
            return SimpleNamespace(status_code=200, content=zipped, json=lambda: {}), None
        meta = {"id": 42, "name": "playwright-report", "size_in_bytes": size, "expired": expired}
        return SimpleNamespace(status_code=200, json=lambda: meta), None

    return _request


async def test_a_workflow_artifact_is_unzipped_into_the_workspace_with_its_tree(store, monkeypatch):
    from app.integrations.providers.github import actions

    sent: list = []
    monkeypatch.setattr(actions._common, "safe_request", _artifact_api(sent))
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    gh = CallableToolExecutor()
    definitions = actions.register({}, gh, None)
    composite.add_tools(definitions, gh)
    assert next(d for d in definitions if d.name == "download_github_workflow_artifact").workspace_ingest
    result = await composite.execute_tool(
        ToolCall(
            id="a",
            name="download_github_workflow_artifact",
            arguments={"repo": "acme/repo", "artifact_id": 42, WORKSPACE_PATH_ARGUMENT: "ci/report"},
        )
    )
    assert not result.is_error, result.content
    assert sent == [
        "https://api.github.com/repos/acme/repo/actions/artifacts/42",
        "https://api.github.com/repos/acme/repo/actions/artifacts/42/zip",
    ]
    assert await _workspace_bytes(store, conversation, "ci/report/index.html") == b"<html>report</html>"
    assert await _workspace_bytes(store, conversation, "ci/report/data/trace-1.zip") == b"PK\x03\x04trace"
    (record,) = [r for r in store.records.values() if r.conversation_id == conversation]
    # Neither the escaping name nor the link is written anywhere.
    assert sorted(store.files[record.id]) == ["ci/report/data/trace-1.zip", "ci/report/index.html"]
    assert "workspace/ci/report/data/trace-1.zip" in result.content and "<html>" not in result.content
    assert not result.files


async def test_a_workflow_artifact_zip_is_kept_whole_with_unzip_false(store, monkeypatch):
    from app.integrations.providers.github import actions

    monkeypatch.setattr(actions._common, "safe_request", _artifact_api([]))
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    gh = CallableToolExecutor()
    composite.add_tools(actions.register({}, gh, None), gh)
    zipped = await composite.execute_tool(
        ToolCall(
            id="z",
            name="download_github_workflow_artifact",
            arguments={"repo": "acme/repo", "artifact_id": 42, "unzip": False, WORKSPACE_PATH_ARGUMENT: "ci/report.zip"},
        )
    )
    assert not zipped.is_error, zipped.content
    assert await _workspace_bytes(store, conversation, "ci/report.zip") == _artifact_zip()


async def test_an_expired_or_oversized_artifact_is_refused_before_its_download(monkeypatch):
    from app.integrations.providers.github import actions

    for kwargs, cue in (({"expired": True}, "has expired"), ({"size": 200 * MB}, "download limit")):
        sent: list = []
        monkeypatch.setattr(actions._common, "safe_request", _artifact_api(sent, **kwargs))
        gh = CallableToolExecutor()
        actions.register({}, gh, None)
        result = await gh.execute_tool(
            ToolCall(id="x", name="download_github_workflow_artifact", arguments={"repo": "acme/repo", "artifact_id": 42})
        )
        assert result.is_error and cue in result.content
        assert not any(url.endswith("/zip") for url in sent)


def test_artifact_members_bound_the_unzip(monkeypatch):
    from app.integrations.providers.github import actions

    files, skipped, error = actions._artifact_members(_artifact_zip())
    assert error is None and [p for p, _ in files] == ["index.html", "data/trace-1.zip"]
    assert len(skipped) == 2
    monkeypatch.setattr(actions, "_MAX_ARTIFACT_MEMBERS", 1)
    assert "unzip=false" in (actions._artifact_members(_artifact_zip())[2] or "")
    monkeypatch.setattr(actions, "_MAX_ARTIFACT_MEMBERS", 10)
    monkeypatch.setattr(actions, "_MAX_ARTIFACT_UNZIPPED_BYTES", 5)
    assert "unzips to" in (actions._artifact_members(_artifact_zip())[2] or "")
    assert "not a valid zip" in (actions._artifact_members(b"not a zip")[2] or "")


# -- materialize_repository ---------------------------------------------------


def _fixture_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(f"acme-repo-0a1b2c3/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        add("README.md", b"# repo\n")
        add("src/app.py", b"import lib.util\n")
        add("src/lib/util.py", b"VALUE = 1\n")
        add("docs/guide.md", b"guide\n")
        add(".git/config", f'[remote "origin"]\n\turl = https://x-access-token:{TOKEN}@github.com/acme/repo\n'.encode())
        link = tarfile.TarInfo("acme-repo-0a1b2c3/src/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    return buffer.getvalue()


def _companion_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        for name, data in (
            (".github/workflows/ci.yml", b"jobs:\n  t:\n    steps:\n      - run: uv run ruff check .\n        working-directory: backend\n"),
            ("backend/pyproject.toml", b"[tool.ruff]\n"),
            ("backend/uv.lock", b"version = 1\n"),
            ("backend/app/core/x.py", b"X = 1\n"),
            ("backend/app/services/y.py", b"Y = 1\n"),
            ("frontend/package.json", b"{}\n"),
        ):
            info = tarfile.TarInfo(f"acme-repo-0a1b2c3/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _declared_archive() -> bytes:
    """A repository whose checks.toml runs its unit tests from backend/."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        for name, data in (
            (".xtm-one/checks.toml", b'[check.unit]\nargv = ["python", "-m", "pytest"]\ncwd = "backend"\n'),
            ("backend/app/core/x.py", b"X = 1\n"),
            ("backend/app/services/y.py", b"Y = 1\n"),
            ("frontend/src/z.ts", b"export {}\n"),
        ):
            info = tarfile.TarInfo(f"acme-repo-0a1b2c3/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _archive_tool(seen: list, archive=_fixture_archive) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    executor = CallableToolExecutor()

    async def download_github_repository_archive(repo: str, ref: str = "") -> ToolResult:
        from app.tools.base import workspace_ingest_requested

        seen.append((repo, ref, workspace_ingest_requested()))
        data = archive()
        return ToolResult(tool_call_id="", content="archive", files=[FileRef.from_bytes(data=data, filename="a.tar.gz", content_type="application/gzip")])

    executor.register("download_github_repository_archive", download_github_repository_archive)
    return [
        ToolDefinition(
            name="download_github_repository_archive",
            description="archive",
            input_schema={"type": "object", "properties": {"repo": {"type": "string"}, "ref": {"type": "string"}}},
            source="integration:github",
            source_label="GitHub",
            workspace_ingest=True,
            repository_archive=True,
        )
    ], executor


async def test_materialize_repository_checks_out_the_selected_tree_and_no_credential(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    seen: list = []
    composite.add_tools(*_archive_tool(seen))
    with offering_tools(composite.all_tools):
        result = await composite.execute_tool(
            ToolCall(
                id="m1",
                name="materialize_repository",
                arguments={"provider": "github", "repository": "acme/repo", "ref": "v1", "sub_paths": ["src"], "path": "repo"},
            )
        )
    assert not result.is_error, result.content
    assert seen == [("acme/repo", "v1", True)], "fetched in memory, through the integration's own tool"
    (record,) = store.records.values()
    paths = sorted(store.files[record.id])
    assert paths == ["repo/.xtm-checkout.json", "repo/src/app.py", "repo/src/lib/util.py"]
    assert "escape (not a regular file)" in result.content
    for blob in store.blobs.objects.values():
        assert TOKEN.encode() not in blob
    assert "The tree only: no .git directory and no credential is written" in result.content
    assert TOKEN not in result.content
    # The sandbox reads the checkout; nothing of .git is on its disk either.
    run = await code_interpreter.execute_python(
        "import os\nprint(sorted(os.listdir('workspace/repo')))\nprint(os.path.exists('workspace/repo/.git'))",
        timeout=60,
        workspace=composite.workspace_sink.session,
    )
    assert run["stdout"].split() == ["['.xtm-checkout.json',", "'src']", "False"]


async def test_materialize_repository_passes_sub_paths_to_an_archive_tool_that_narrows(store):
    """#414: a tool offering ``paths`` gets the sub_paths, and the files it left out are named, never silent."""
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    seen: list = []
    executor = CallableToolExecutor()

    async def download_github_repository_archive(repo: str, ref: str = "", paths: list | None = None) -> ToolResult:
        seen.append((repo, ref, paths))
        return ToolResult(
            tool_call_id="",
            content="archive",
            files=[FileRef.from_bytes(data=_fixture_archive(), filename="a.tar.gz", content_type="application/gzip")],
            structured={"left_out": ["public/video.mp4"]},
        )

    executor.register("download_github_repository_archive", download_github_repository_archive)
    composite.add_tools(
        [
            ToolDefinition(
                name="download_github_repository_archive",
                description="archive",
                input_schema={
                    "type": "object",
                    "properties": {"repo": {"type": "string"}, "ref": {"type": "string"}, "paths": {"type": "array"}},
                },
                source="integration:github",
                source_label="GitHub",
                workspace_ingest=True,
                repository_archive=True,
            )
        ],
        executor,
    )
    with offering_tools(composite.all_tools):
        result = await composite.execute_tool(
            ToolCall(
                id="m1",
                name="materialize_repository",
                arguments={
                    "provider": "github",
                    "repository": "acme/repo",
                    "ref": "v1",
                    "sub_paths": ["src"],
                    "path": "repo",
                },
            )
        )
    assert not result.is_error, result.content
    assert seen == [("acme/repo", "v1", ["src"])]
    assert "public/video.mp4 (outside the sub_paths of an archive over its limit)" in result.content


async def test_materialize_repository_writes_a_checkout_record_next_to_the_tree(store):
    from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutRecord

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    composite.add_tools(*_archive_tool([]))
    with offering_tools(composite.all_tools):
        result = await composite.execute_tool(
            ToolCall(id="m1", name="materialize_repository", arguments={"provider": "github", "repository": "acme/repo", "ref": "v1", "sub_paths": ["src"], "path": "repo"})
        )
    assert not result.is_error, result.content
    assert f"Checkout record: workspace/repo/{RECORD_NAME} (commit 0a1b2c3)" in result.content
    assert 'create_github_commit(from_checkout="repo")' in result.content
    record = CheckoutRecord.from_json(await _workspace_bytes(store, conversation, f"repo/{RECORD_NAME}"), path="repo")
    assert (record.provider, record.repository, record.ref, record.commit) == ("github", "acme/repo", "v1", "0a1b2c3")
    assert record.sub_paths == ("src",)
    assert sorted(record.files) == ["src/app.py", "src/lib/util.py"]
    assert record.files["src/lib/util.py"].sha256 == hashlib.sha256(b"VALUE = 1\n").hexdigest()
    listed = await composite.execute_tool(ToolCall(id="l", name="sandbox_list_files", arguments={"path": "repo"}))
    assert f"workspace/repo/{RECORD_NAME}" in listed.content
    assert TOKEN not in (await _workspace_bytes(store, conversation, f"repo/{RECORD_NAME}")).decode()


async def test_materialize_repository_replaces_what_it_checked_out_before(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    composite.add_tools(*_archive_tool([]))
    await executor.session.put_bytes("repo/src/stale.py", b"old")
    await executor.session.put_bytes("keep.txt", b"mine")
    with offering_tools(composite.all_tools):
        await composite.execute_tool(
            ToolCall(id="m1", name="materialize_repository", arguments={"provider": "GitHub", "repository": "acme/repo", "path": "repo"})
        )
    (record,) = store.records.values()
    paths = sorted(store.files[record.id])
    assert "repo/src/stale.py" not in paths and "keep.txt" in paths
    assert "repo/docs/guide.md" in paths and "repo/README.md" in paths


async def test_a_sub_paths_checkout_keeps_the_manifests_and_workflows_its_checks_need(store):
    from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutRecord

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    composite.add_tools(*_archive_tool([], _companion_archive))
    arguments = {"provider": "github", "repository": "acme/repo", "sub_paths": ["backend/app/core"], "path": "repo"}
    with offering_tools(composite.all_tools):
        result = await composite.execute_tool(ToolCall(id="m1", name="materialize_repository", arguments=arguments))
    assert not result.is_error, result.content
    assert "with 3 manifest, lock and workflow file(s) from outside them" in result.content
    (workspace,) = store.records.values()
    assert sorted(store.files[workspace.id]) == [
        "repo/.github/workflows/ci.yml",
        f"repo/{RECORD_NAME}",
        "repo/backend/app/core/x.py",
        "repo/backend/pyproject.toml",
        "repo/backend/uv.lock",
    ], "a sibling directory and another package's manifest stay out"
    record = CheckoutRecord.from_json(await _workspace_bytes(store, conversation, f"repo/{RECORD_NAME}"), path="repo")
    assert record.sub_paths == ("backend/app/core",)
    assert "backend/pyproject.toml" in record.files, "a companion is tracked: its edit is a change to commit"
    from app.services.sandbox_workspace import commands

    found = await commands.checkout_checks(executor.session, "repo", images={"python"})
    assert found.origin == "detected" and "backend:python-lint" in found.specs, found.listing("repo")
    # The next narrow checkout at the same path replaces the companions with its own.
    # (no checks.toml in this archive: nothing is widened)
    assert "widened" not in result.content
    await executor.session.put_bytes("repo/.github/workflows/stale.yml", b"jobs: {}\n")
    with offering_tools(composite.all_tools):
        await composite.execute_tool(ToolCall(id="m2", name="materialize_repository", arguments=arguments))
    assert "repo/.github/workflows/stale.yml" not in store.files[workspace.id]


async def test_run_command_without_a_check_answers_the_listing_as_a_result(store, monkeypatch):
    """The listing the description promises is an answer, not an error (an error is recorded as a friction)."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "custom_tool_sandbox", "opensandbox")
    composite, executor = _composite(uuid.uuid4(), uuid.uuid4())
    with offering_tools(composite.all_tools):
        empty = await composite.execute_tool(ToolCall(id="r0", name="sandbox_run_command", arguments={"checkout": "co"}))
    assert empty.is_error and "declares no check" in empty.content and "workspace/co/" in empty.content
    toml = b'[check.unit]\nargv = ["pytest"]\ncwd = "backend"\nimage = "python-dev"\n[service.api]\nargv = ["serve"]\nport = 8000\n'
    await executor.session.put_bytes("co/.xtm-one/checks.toml", toml)
    with offering_tools(composite.all_tools):
        listed = await composite.execute_tool(ToolCall(id="r1", name="sandbox_run_command", arguments={"checkout": "co"}))
        unknown = await composite.execute_tool(
            ToolCall(id="r2", name="sandbox_run_command", arguments={"checkout": "co", "check": "does-not-exist"})
        )
    assert not listed.is_error, listed.content
    assert listed.content.startswith("Declared in workspace/co/.xtm-one/checks.toml:"), listed.content
    assert "- unit: pytest in backend/ [image python-dev]" in listed.content and "- api: port 8000" in listed.content
    assert unknown.is_error and "'does-not-exist' is not a check of workspace/co/" in unknown.content
    assert "- unit: pytest" in unknown.content


async def test_a_sub_path_inside_a_declared_check_directory_is_widened_to_it(store):
    """Backend-unit runs from backend/ and imports app.core; backend/app/services alone fails at collection."""
    from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutRecord

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _executor = _composite(conversation, user)
    composite.add_tools(*_archive_tool([], _declared_archive))
    arguments = {"provider": "github", "repository": "acme/repo", "sub_paths": ["backend/app/services"], "path": "repo"}
    with offering_tools(composite.all_tools):
        result = await composite.execute_tool(ToolCall(id="m1", name="materialize_repository", arguments=arguments))
    assert not result.is_error, result.content
    assert "(under backend)" in result.content
    assert "backend/app/services widened to backend/ (the directory unit run from)" in result.content
    (workspace,) = store.records.values()
    assert sorted(store.files[workspace.id]) == [
        f"repo/{RECORD_NAME}",
        "repo/.xtm-one/checks.toml",
        "repo/backend/app/core/x.py",
        "repo/backend/app/services/y.py",
    ], "the check's directory is written whole, a sibling directory stays out"
    record = CheckoutRecord.from_json(await _workspace_bytes(store, conversation, f"repo/{RECORD_NAME}"), path="repo")
    assert record.sub_paths == ("backend",), "the record keeps what was written, so commits and rebases cover it"


async def test_add_paths_grows_a_checkout_at_its_commit_and_keeps_its_edits(store):
    """A narrow checkout grows by a path; the edits stay, the new files are tracked from their base."""
    from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutRecord

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    seen: list = []
    composite.add_tools(*_archive_tool(seen, _companion_archive))
    first = {"provider": "github", "repository": "acme/repo", "ref": "main", "sub_paths": ["backend/app/core"], "path": "repo"}
    with offering_tools(composite.all_tools):
        await composite.execute_tool(ToolCall(id="m1", name="materialize_repository", arguments=first))
    await executor.session.put_bytes("repo/backend/app/core/x.py", b"X = 2\n")
    await executor.session.put_bytes("repo/backend/app/services/y.py", b"Y = 'mine'\n")
    grow = {"provider": "github", "repository": "acme/repo", "path": "repo", "add_paths": ["backend/app/services", "frontend"]}
    with offering_tools(composite.all_tools):
        result = await composite.execute_tool(ToolCall(id="m2", name="materialize_repository", arguments=grow))
    assert not result.is_error, result.content
    assert seen[-1] == ("acme/repo", "0a1b2c3", True), "fetched at the commit the record names, not the ref's head"
    assert "Grew the checkout at workspace/repo/ by backend/app/services, frontend" in result.content
    assert "1 file(s) edited in the workspace were kept as they are" in result.content
    assert "Nothing was removed" in result.content
    assert await _workspace_bytes(store, conversation, "repo/backend/app/core/x.py") == b"X = 2\n"
    assert await _workspace_bytes(store, conversation, "repo/backend/app/services/y.py") == b"Y = 'mine'\n"
    assert await _workspace_bytes(store, conversation, "repo/frontend/package.json") == b"{}\n"
    record = CheckoutRecord.from_json(await _workspace_bytes(store, conversation, f"repo/{RECORD_NAME}"), path="repo")
    assert record.sub_paths == ("backend/app/core", "backend/app/services", "frontend")
    assert record.commit == "0a1b2c3" and record.ref == "main"
    assert record.files["backend/app/services/y.py"].sha256 == hashlib.sha256(b"Y = 1\n").hexdigest(), "the base at the commit"
    status = await composite.execute_tool(ToolCall(id="s", name="sandbox_checkout_status", arguments={"checkout": "repo"}))
    assert "backend/app/core/x.py" in status.content and "backend/app/services/y.py" in status.content
    assert "frontend/package.json" not in status.content, "a file written at its base is no change"


async def test_add_paths_replaces_the_stale_files_an_earlier_checkout_left_and_keeps_edits(store):
    """A wider checkout left frontend/ at an older commit; a grow by frontend/ writes it at the record's commit (#10998)."""
    from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutRecord

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    composite.add_tools(*_archive_tool([], _companion_archive))
    whole = {"provider": "github", "repository": "acme/repo", "path": "repo"}
    narrow = {**whole, "sub_paths": ["backend/app/core"]}
    with offering_tools(composite.all_tools):
        await composite.execute_tool(ToolCall(id="m1", name="materialize_repository", arguments=whole))
    # The older commit's frontend, and a stale backend file, before the narrow checkout records its baseline.
    await executor.session.put_bytes("repo/frontend/package.json", b'{"old": true}\n')
    await executor.session.put_bytes("repo/backend/app/services/y.py", b"Y = 0\n")
    with offering_tools(composite.all_tools):
        await composite.execute_tool(ToolCall(id="m2", name="materialize_repository", arguments=narrow))
    # An edit made after the record was written stays an edit.
    await executor.session.put_bytes("repo/backend/app/services/y.py", b"Y = 'mine'\n")
    grow = {**whole, "add_paths": ["backend/app/services", "frontend"]}
    with offering_tools(composite.all_tools):
        result = await composite.execute_tool(ToolCall(id="m3", name="materialize_repository", arguments=grow))
        status = await composite.execute_tool(ToolCall(id="s", name="sandbox_checkout_status", arguments={"checkout": "repo"}))
    assert not result.is_error, result.content
    assert "1 stale file(s) an earlier checkout left there" in result.content
    assert "1 file(s) edited in the workspace were kept as they are (each shows as modified): backend/app/services/y.py" in result.content
    assert await _workspace_bytes(store, conversation, "repo/frontend/package.json") == b"{}\n", "written at the commit"
    assert await _workspace_bytes(store, conversation, "repo/backend/app/services/y.py") == b"Y = 'mine'\n"
    record = CheckoutRecord.from_json(await _workspace_bytes(store, conversation, f"repo/{RECORD_NAME}"), path="repo")
    assert "frontend/package.json" not in (record.outside_baseline or {})
    assert "frontend/package.json" not in status.content, "a stale file is no change, so from_checkout commits nothing for it"
    assert "backend/app/services/y.py" in status.content


def test_stale_on_grow_reads_the_baseline_then_the_record_time():
    import dataclasses
    from types import SimpleNamespace

    from app.services.sandbox_workspace.checkout import build_record, stale_on_grow

    record = build_record(
        provider="github", repository="a/b", ref="main", commit="abc1234", path="repo", sub_paths=["src/a"],
        files=[("src/a/x.py", b"1")], outside={"src/b/y.py": "old"},
    )
    assert stale_on_grow(record, "src/b/y.py", SimpleNamespace(sha256="old", mtime=5.0))
    assert not stale_on_grow(record, "src/b/y.py", SimpleNamespace(sha256="edited", mtime=5.0))
    assert not stale_on_grow(record, "src/b/z.py", SimpleNamespace(sha256="new", mtime=1.0), since=9.0)
    legacy = dataclasses.replace(record, outside_baseline=None)
    assert stale_on_grow(legacy, "src/b/y.py", SimpleNamespace(sha256="any", mtime=1.0), since=9.0)
    assert not stale_on_grow(legacy, "src/b/y.py", SimpleNamespace(sha256="any", mtime=10.0), since=9.0)
    assert not stale_on_grow(legacy, "src/b/y.py", SimpleNamespace(sha256="any", mtime=1.0))


async def test_add_paths_already_covered_writes_nothing(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _executor = _composite(conversation, user)
    seen: list = []
    composite.add_tools(*_archive_tool(seen, _companion_archive))
    with offering_tools(composite.all_tools):
        await composite.execute_tool(
            ToolCall(id="m1", name="materialize_repository", arguments={"provider": "github", "repository": "acme/repo", "sub_paths": ["backend"], "path": "repo"})
        )
        result = await composite.execute_tool(
            ToolCall(id="m2", name="materialize_repository", arguments={"provider": "github", "repository": "acme/repo", "path": "repo", "add_paths": ["backend/app"]})
        )
    assert not result.is_error and "nothing was fetched or written" in result.content
    assert len(seen) == 1


async def test_add_paths_refusals(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _executor = _composite(conversation, user)
    composite.add_tools(*_archive_tool([], _companion_archive))
    grow = {"provider": "github", "repository": "acme/repo", "path": "repo", "add_paths": ["frontend"]}
    with offering_tools(composite.all_tools):
        missing = await composite.execute_tool(ToolCall(id="g0", name="materialize_repository", arguments=grow))
        mixed = await composite.execute_tool(
            ToolCall(id="g1", name="materialize_repository", arguments={**grow, "sub_paths": ["backend"]})
        )
        await composite.execute_tool(
            ToolCall(id="m1", name="materialize_repository", arguments={"provider": "github", "repository": "acme/repo", "path": "repo"})
        )
        whole = await composite.execute_tool(ToolCall(id="g2", name="materialize_repository", arguments=grow))
        other = await composite.execute_tool(
            ToolCall(id="g3", name="materialize_repository", arguments={**grow, "repository": "acme/other"})
        )
    assert missing.is_error and "add_paths grows an existing checkout" in missing.content
    assert mixed.is_error and "pass neither sub_paths nor ref" in mixed.content
    assert whole.is_error and "checkout of the whole tree" in whole.content
    assert other.is_error and "is a checkout of acme/repo, not acme/other" in other.content


def test_grown_record_unions_sub_paths_and_keeps_recorded_hashes():
    from app.services.sandbox_workspace.checkout import build_record, grown

    record = build_record(
        provider="github", repository="a/b", ref="main", commit="abc1234", path="repo", sub_paths=["src/a"], files=[("src/a/x.py", b"1")]
    )
    moved = grown(record, sub_paths=["src/b", "src/a/inner"], files=[("src/a/x.py", b"2"), ("src/b/y.py", b"3")])
    assert moved.sub_paths == ("src/a", "src/b")
    assert moved.files["src/a/x.py"] == record.files["src/a/x.py"]
    assert moved.files["src/b/y.py"].sha256 == hashlib.sha256(b"3").hexdigest()
    assert (moved.commit, moved.ref, moved.path) == ("abc1234", "main", "repo")


async def test_materialize_repository_needs_a_connected_provider(store):
    composite, _ = _composite(uuid.uuid4(), uuid.uuid4())
    with offering_tools(composite.all_tools):
        result = await composite.execute_tool(
            ToolCall(id="m1", name="materialize_repository", arguments={"provider": "gitlab", "repository": "acme/repo"})
        )
    assert result.is_error and "no 'gitlab' repository integration is connected" in result.content


async def test_create_github_commit_from_checkout_commits_exactly_what_the_sandbox_changed(store, monkeypatch):
    from app.integrations.providers.github import contents

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    composite.add_tools(*_archive_tool([]))
    with offering_tools(composite.all_tools):
        await composite.execute_tool(
            ToolCall(id="m1", name="materialize_repository", arguments={"provider": "github", "repository": "acme/repo", "ref": "v1", "sub_paths": ["src"], "path": "repo"})
        )
    parsed: list = []

    def fake_parse(files, delete_paths):
        parsed.append((files, delete_paths))
        return [], [], "Error: stop here (fixture)"

    monkeypatch.setattr(contents, "_parse_commit_changes", fake_parse)
    github = CallableToolExecutor()
    composite.add_tools(contents.register({"Authorization": "token x"}, github, lambda *a, **k: None), github)
    # Unchanged: nothing to commit, the parser is never reached.
    unchanged = await composite.execute_tool(
        ToolCall(id="c0", name="create_github_commit", arguments={"repo": "acme/repo", "message": "feat: x", "from_checkout": "repo"})
    )
    assert not unchanged.is_error, unchanged.content
    assert unchanged.content.startswith("Nothing to commit: repo is unchanged since its checkout of acme/repo@v1 (commit 0a1b2c3)")
    assert parsed == []
    # Two files edited, one added, one removed, one stray file outside the recorded sub_paths.
    await executor.session.put_bytes("repo/src/app.py", b"import lib.util\nVALUE = 2\n")
    await executor.session.put_bytes("repo/src/lib/new.py", b"NEW = 1\n")
    await executor.session.remove(["repo/src/lib/util.py"])
    await executor.session.put_bytes("repo/README.md", b"# stray\n")
    # The stray file would be left behind: refused whole, nothing parsed (#237); paths=[the sub_paths] leaves it out.
    partial = await composite.execute_tool(
        ToolCall(
            id="c1a",
            name="create_github_commit",
            arguments={"repo": "acme/repo", "message": "feat: x", "from_checkout": "repo"},
        )
    )
    assert "nothing was committed" in partial.content and "README.md" in partial.content, partial.content
    assert parsed == []
    changed = await composite.execute_tool(
        ToolCall(
            id="c1",
            name="create_github_commit",
            arguments={"repo": "acme/repo", "message": "feat: x", "from_checkout": "repo", "paths": ["src"]},
        )
    )
    assert "stop here (fixture)" in changed.content
    [(files, deletes)] = parsed
    assert [(f["path"], f["content"].data) for f in files] == [
        ("src/app.py", b"import lib.util\nVALUE = 2\n"),
        ("src/lib/new.py", b"NEW = 1\n"),
    ]
    assert deletes == ["src/lib/util.py"]
    # 'paths' narrows it; another repository or a directory without a record is refused.
    parsed.clear()
    await composite.execute_tool(
        ToolCall(id="c2", name="create_github_commit", arguments={"repo": "acme/repo", "message": "feat: x", "from_checkout": "repo", "paths": ["src/lib"]})
    )
    [(files, deletes)] = parsed
    assert [f["path"] for f in files] == ["src/lib/new.py"] and deletes == ["src/lib/util.py"]
    other = await composite.execute_tool(
        ToolCall(id="c3", name="create_github_commit", arguments={"repo": "acme/other", "message": "feat: x", "from_checkout": "repo"})
    )
    assert "is a checkout of acme/repo, not of acme/other" in other.content
    no_record = await composite.execute_tool(
        ToolCall(id="c4", name="create_github_commit", arguments={"repo": "acme/repo", "message": "feat: x", "from_checkout": "work"})
    )
    assert "holds no .xtm-checkout.json" in no_record.content
    both = await composite.execute_tool(
        ToolCall(id="c5", name="create_github_commit", arguments={"repo": "acme/repo", "message": "feat: x", "from_checkout": "repo", "files": [{"path": "a", "content": "b"}]})
    )
    assert "either from_checkout or files/delete_paths" in both.content


# -- sandbox_checkout_status / sandbox_checkout_diff -------------------


_ORIGINALS = {"README.md": b"# repo\n", "src/app.py": b"import lib.util\n", "src/lib/util.py": b"VALUE = 1\n", "docs/guide.md": b"guide\n"}


def _file_tool(seen: list, originals: dict[str, bytes]) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    executor = CallableToolExecutor()

    async def download_github_file(repo: str, path: str, branch: str = "") -> ToolResult:
        seen.append((repo, path, branch))
        if path not in originals:
            return ToolResult(tool_call_id="", content=f"Error: {path} not found", is_error=True)
        ref = FileRef.from_bytes(data=originals[path], filename=path.rsplit("/", 1)[-1], content_type=None)
        return ToolResult(tool_call_id="", content="file", files=[ref])

    executor.register("download_github_file", download_github_file)
    return [
        ToolDefinition(
            name="download_github_file",
            description="file",
            input_schema={"type": "object", "properties": {"repo": {"type": "string"}, "path": {"type": "string"}, "branch": {"type": "string"}}},
            source="integration:github",
            source_label="GitHub",
            workspace_ingest=True,
            repository_file=True,
        )
    ], executor


async def _checked_out(store, *, originals: dict[str, bytes] | None = None, with_file_tool: bool = True):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    composite.add_tools(*_archive_tool([]))
    seen: list = []
    if with_file_tool:
        composite.add_tools(*_file_tool(seen, originals or _ORIGINALS))
    with offering_tools(composite.all_tools):
        done = await composite.execute_tool(
            ToolCall(id="m1", name="materialize_repository", arguments={"provider": "github", "repository": "acme/repo", "ref": "v1", "sub_paths": ["src"], "path": "repo"})
        )
    assert not done.is_error, done.content
    return composite, executor, seen


async def _call(composite, name: str, **arguments):
    with offering_tools(composite.all_tools):
        return await composite.execute_tool(ToolCall(id=f"{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments))


async def test_checkout_status_lists_what_changed_since_the_checkout(store):
    composite, executor, _ = await _checked_out(store)
    clean = await _call(composite, "sandbox_checkout_status", checkout="repo")
    assert not clean.is_error, clean.content
    assert clean.content == "workspace/repo/ is clean: nothing changed since its checkout of acme/repo@v1 (commit 0a1b2c3)."
    await executor.session.put_bytes("repo/src/app.py", b"import lib.util\nVALUE = 2\n")
    await executor.session.put_bytes("repo/src/lib/new.py", b"NEW = 1\n")
    await executor.session.remove(["repo/src/lib/util.py"])
    status = await _call(composite, "sandbox_checkout_status", checkout="repo")
    assert status.content.splitlines() == [
        "workspace/repo/: checkout of acme/repo@v1 (commit 0a1b2c3); 3 file(s) changed: 1 modified, 1 added, 1 removed. "
        'create_github_commit(from_checkout="repo") commits exactly these.',
        "M src/app.py",
        "A src/lib/new.py",
        "D src/lib/util.py",
    ]
    narrowed = await _call(composite, "sandbox_checkout_status", checkout="repo", paths=["src/lib"])
    assert narrowed.content.splitlines()[1:] == ["A src/lib/new.py", "D src/lib/util.py"]
    no_record = await _call(composite, "sandbox_checkout_status", checkout="work")
    assert no_record.is_error and "holds no .xtm-checkout.json" in no_record.content


async def test_checkout_status_names_the_pre_push_guards_before_the_commit(store):
    """The guard a change touches and the sandbox never saw pass is named by the status, not after the push (#434)."""
    composite, executor, _ = await _checked_out(store)
    checks = b'[check.src-types]\nargv = ["true"]\ncwd = "src"\npre_push = ["*.py"]\n'
    await executor.session.put_bytes("repo/.xtm-one/checks.toml", checks)
    await executor.session.put_bytes("repo/src/app.py", b"import lib.util\nVALUE = 2\n")
    status = await _call(composite, "sandbox_checkout_status", checkout="repo")
    assert not status.is_error, status.content
    assert "M src/app.py" in status.content
    assert "Pre-push guard" in status.content and "src-types never ran on this checkout (src/app.py)" in status.content
    narrowed = await _call(composite, "sandbox_checkout_status", checkout="repo", paths=["src/lib"])
    assert "Pre-push guard" not in narrowed.content, "nothing changed under the paths asked"


async def test_advance_checkout_moves_the_record_and_status_is_clean_after_the_commit(store):
    from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutRecord

    composite, executor, _ = await _checked_out(store)
    await executor.session.put_bytes("repo/src/app.py", b"import lib.util\nVALUE = 2\n")
    await executor.session.put_bytes("repo/src/lib/new.py", b"NEW = 1\n")
    await executor.session.remove(["repo/src/lib/util.py"])
    sink = composite.workspace_sink
    record, changes = await sink.checkout_changes("repo", None)
    assert changes.count == 3
    said = await sink.advance_checkout(
        record,
        commit="9f8e7d6c5b4a",
        ref="feat/x",
        written={"src/app.py": b"import lib.util\nVALUE = 2\n", "src/lib/new.py": b"NEW = 1\n"},
        removed=["src/lib/util.py"],
    )
    assert said == (
        f"Checkout record workspace/repo/{RECORD_NAME} moved to acme/repo@feat/x (commit 9f8e7d6c5b4a): "
        "what this commit carried is no longer a change."
    )
    status = await _call(composite, "sandbox_checkout_status", checkout="repo")
    assert status.content == "workspace/repo/ is clean: nothing changed since its checkout of acme/repo@feat/x (commit 9f8e7d6c5b4a)."
    _entry, data = await executor.session.get_bytes(f"repo/{RECORD_NAME}")
    moved = CheckoutRecord.from_json(data, path="repo")
    assert (moved.commit, moved.ref) == ("9f8e7d6c5b4a", "feat/x")
    assert sorted(moved.files) == ["src/app.py", "src/lib/new.py"]
    # The next edit is the only change.
    await executor.session.put_bytes("repo/src/lib/new.py", b"NEW = 2\n")
    again = await _call(composite, "sandbox_checkout_status", checkout="repo")
    assert again.content.splitlines()[1:] == ["M src/lib/new.py"]


async def test_advance_checkout_says_a_record_it_could_not_write_and_never_raises(store, monkeypatch):
    composite, executor, _ = await _checked_out(store)
    sink = composite.workspace_sink
    record, _changes = await sink.checkout_changes("repo", None)

    async def broken(*args, **kwargs):
        raise RuntimeError("storage down")

    monkeypatch.setattr(executor.session, "put_bytes", broken)
    said = await sink.advance_checkout(record, commit="9f8e7d6c", ref="feat/x", written={}, removed=[])
    assert "was not moved to the new commit (storage down)" in said
    assert await sink.advance_checkout(object(), commit="9f8e7d6c", ref="feat/x", written={}, removed=[]) == ""


async def test_checkout_diff_diffs_each_change_against_the_file_at_the_recorded_commit(store):
    composite, executor, seen = await _checked_out(store)
    await executor.session.put_bytes("repo/src/app.py", b"import lib.util\nVALUE = 2\n")
    await executor.session.put_bytes("repo/src/lib/new.py", b"NEW = 1\n")
    await executor.session.put_bytes("repo/src/logo.png", b"\x89PNG\x00\x01")
    await executor.session.remove(["repo/src/lib/util.py"])
    diff = await _call(composite, "sandbox_checkout_diff", checkout="repo")
    assert not diff.is_error, diff.content
    text = diff.content
    assert text.startswith("workspace/repo/: checkout of acme/repo@v1 (commit 0a1b2c3); 4 file(s) changed, 4 diffed")
    assert "--- a/src/app.py\n+++ b/src/app.py\n" in text and "+VALUE = 2\n" in text
    assert "--- /dev/null\n+++ b/src/lib/new.py\n" in text and "+NEW = 1\n" in text
    assert "--- a/src/lib/util.py\n+++ /dev/null\n" in text and "-VALUE = 1\n" in text
    assert "Binary file src/logo.png differs (0 -> 6 bytes)." in text
    # Only the modified and removed files' bases are read, at the recorded commit, through the integration.
    assert sorted(seen) == [("acme/repo", "src/app.py", "0a1b2c3"), ("acme/repo", "src/lib/util.py", "0a1b2c3")]
    seen.clear()
    narrowed = await _call(composite, "sandbox_checkout_diff", checkout="repo", paths=["src/app.py"], context_lines=0)
    assert "src/lib/new.py" not in narrowed.content and "(0 context line(s))" in narrowed.content
    assert seen == [("acme/repo", "src/app.py", "0a1b2c3")]


async def test_checkout_diff_refuses_a_base_that_does_not_match_the_record(store):
    composite, executor, _ = await _checked_out(store, originals={**_ORIGINALS, "src/app.py": b"moved on\n"})
    await executor.session.put_bytes("repo/src/app.py", b"import lib.util\nVALUE = 2\n")
    diff = await _call(composite, "sandbox_checkout_diff", checkout="repo")
    assert not diff.is_error, diff.content
    assert "0 diffed" in diff.content
    assert "Not diffed: src/app.py (the file read at 0a1b2c3 does not match the checkout record's sha256" in diff.content
    assert "moved on" not in diff.content


async def test_checkout_diff_without_a_file_tool_says_so_and_a_clean_checkout_is_clean(store):
    composite, executor, _ = await _checked_out(store, with_file_tool=False)
    clean = await _call(composite, "sandbox_checkout_diff", checkout="repo")
    assert clean.content.startswith("workspace/repo/ is clean")
    await executor.session.put_bytes("repo/src/app.py", b"changed\n")
    await executor.session.put_bytes("repo/src/added.py", b"A = 1\n")
    diff = await _call(composite, "sandbox_checkout_diff", checkout="repo")
    assert "+A = 1\n" in diff.content, "an added file needs no base"
    assert "Not diffed: src/app.py (base not read: no 'github' integration that reads a repository file is connected)" in diff.content


# -- sandbox_checkout_rebase -------------------------------------------


def _tar(commit: str, files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(f"acme-repo-{commit}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


_V1 = {"README.md": b"# repo\n", "src/app.py": b"a\nb\nc\nd\ne\n", "src/lib/util.py": b"VALUE = 1\n", "src/old.py": b"OLD\n"}
_MAIN = {"README.md": b"# repo 2\n", "src/app.py": b"a\nb\nc\nd\nE\n", "src/lib/util.py": b"VALUE = 2\n", "src/new.py": b"NEW\n"}


def _refs_archive_tool(trees: dict[str, tuple[str, dict[str, bytes]]], seen: list) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    executor = CallableToolExecutor()

    async def download_github_repository_archive(repo: str, ref: str = "") -> ToolResult:
        seen.append((repo, ref))
        commit, files = trees[ref]
        return ToolResult(tool_call_id="", content="archive", files=[FileRef.from_bytes(data=_tar(commit, files), filename="a.tar.gz", content_type="application/gzip")])

    executor.register("download_github_repository_archive", download_github_repository_archive)
    return [
        ToolDefinition(
            name="download_github_repository_archive",
            description="archive",
            input_schema={"type": "object", "properties": {"repo": {"type": "string"}, "ref": {"type": "string"}}},
            source="integration:github",
            source_label="GitHub",
            workspace_ingest=True,
            repository_archive=True,
        )
    ], executor


async def _rebasable(store):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    archives: list = []
    reads: list = []
    composite.add_tools(*_refs_archive_tool({"v1": ("0a1b2c3", _V1), "main": ("9f9f9f9", _MAIN)}, archives))
    composite.add_tools(*_file_tool(reads, _V1))
    done = await _call(composite, "materialize_repository", provider="github", repository="acme/repo", ref="v1", sub_paths=["src"], path="repo")
    assert not done.is_error, done.content
    return composite, executor, archives, reads


async def test_checkout_rebase_merges_the_ref_under_the_edits_and_status_shows_only_them(store):
    from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutRecord

    composite, executor, archives, reads = await _rebasable(store)
    await executor.session.put_bytes("repo/src/app.py", b"A\nb\nc\nd\ne\n")
    await executor.session.put_bytes("repo/src/mine.py", b"MINE\n")
    result = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main")
    assert not result.is_error, result.content
    text = result.content
    assert text.startswith("workspace/repo/: rebased from acme/repo@v1 (commit 0a1b2c3) onto acme/repo@main (commit 9f9f9f9).")
    assert "Updated from the ref (2): src/lib/util.py, src/new.py." in text
    assert "Removed by the ref (1): src/old.py." in text
    assert "Merged cleanly (1): src/app.py." in text
    assert "In conflict" not in text
    assert archives[-1] == ("acme/repo", "main")
    # Only the file changed on both sides has its base read, at the recorded commit.
    assert reads == [("acme/repo", "src/app.py", "0a1b2c3")]
    assert (await executor.session.get_bytes("repo/src/app.py"))[1] == b"A\nb\nc\nd\nE\n"
    assert (await executor.session.get_bytes("repo/src/new.py"))[1] == b"NEW\n"
    with pytest.raises(FileNotFoundError):
        await executor.session.get_bytes("repo/src/old.py")
    record = CheckoutRecord.from_json((await executor.session.get_bytes(f"repo/{RECORD_NAME}"))[1], path="repo")
    assert (record.ref, record.commit, record.sub_paths) == ("main", "9f9f9f9", ("src",))
    status = await _call(composite, "sandbox_checkout_status", checkout="repo")
    assert status.content.splitlines()[1:] == ["M src/app.py", "A src/mine.py"]


async def test_checkout_rebase_writes_markers_for_an_overlapping_edit_and_reports_it(store):
    composite, executor, _archives, _reads = await _rebasable(store)
    await executor.session.put_bytes("repo/src/app.py", b"a\nb\nc\nd\nX\n")
    await executor.session.put_bytes("repo/src/old.py", b"OLD edited\n")
    result = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main")
    assert not result.is_error, result.content
    assert "In conflict (2): src/app.py (1 hunk(s) between conflict markers); src/old.py (changed in the workspace, removed on the ref" in result.content
    merged = (await executor.session.get_bytes("repo/src/app.py"))[1].decode()
    assert merged == "a\nb\nc\nd\n<<<<<<< workspace\nX\n=======\nE\n>>>>>>> ref\n"
    assert (await executor.session.get_bytes("repo/src/old.py"))[1] == b"OLD edited\n"


async def test_checkout_rebase_on_conflict_writes_the_ref_line_then_the_workspace_line(store):
    composite, executor, _archives, _reads = await _rebasable(store)
    await executor.session.put_bytes("repo/src/app.py", b"a\nb\nc\nd\ne\nmine\n")
    result = await _call(
        composite, "sandbox_checkout_rebase", checkout="repo", ref="main", on_conflict="ref-then-workspace"
    )
    assert not result.is_error, result.content
    assert "In conflict" not in result.content and "Merged cleanly (1): src/app.py." in result.content
    assert (await executor.session.get_bytes("repo/src/app.py"))[1] == b"a\nb\nc\nd\nE\nmine\n"

    refused = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main", on_conflict="theirs")
    assert refused.is_error and "on_conflict must be one of: markers, ref-then-workspace" in refused.content


async def test_checkout_rebase_refuses_while_markers_are_unsettled_and_nests_none(store):
    composite, executor, archives, _reads = await _rebasable(store)
    await executor.session.put_bytes("repo/src/app.py", b"a\nb\nc\nd\nX\n")
    first = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main")
    assert not first.is_error, first.content
    marked = (await executor.session.get_bytes("repo/src/app.py"))[1]
    fetched = len(archives)

    again = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main")
    assert again.is_error and "1 file(s) still hold conflict markers (src/app.py)" in again.content
    assert "Nothing was written" in again.content and len(archives) == fetched
    assert (await executor.session.get_bytes("repo/src/app.py"))[1] == marked


async def test_checkout_rebase_in_background_answers_a_run_id_then_the_same_answer_once(store):
    from app.services.sandbox_workspace import jobs

    composite, executor, archives, _reads = await _rebasable(store)
    await executor.session.put_bytes("repo/src/app.py", b"a\nb\nc\nd\nX\n")
    started = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main", background=True)
    assert not started.is_error, started.content
    run_id = started.content.split("run_id ", 1)[1].split(".", 1)[0]
    assert run_id.startswith("rebase-")
    polled = await _call(composite, "sandbox_checkout_rebase", checkout="repo", run_id=run_id)
    assert not polled.is_error, polled.content
    assert polled.content.startswith("workspace/repo/: rebased from acme/repo@v1")
    assert "src/app.py (1 hunk(s) between conflict markers)" in polled.content
    merged = (await executor.session.get_bytes("repo/src/app.py"))[1].decode()
    assert merged.count("<<<<<<< ") == 1
    # A resumed turn repeating the start finds the ended job through the probe: its answer, no second rebase.
    landed = await jobs.rebase_landed(executor.session, {"checkout": "repo", "ref": "main", "background": True})
    assert isinstance(landed, str) and run_id in landed and "rebased from acme/repo@v1" in landed
    fetched = len(archives)
    again = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main", background=True)
    polled_again = await _call(
        composite, "sandbox_checkout_rebase", checkout="repo", run_id=again.content.split("run_id ", 1)[1].split(".", 1)[0]
    )
    # The markers guard refuses before any download: no nested markers.
    assert polled_again.is_error and "still hold conflict markers" in polled_again.content
    assert len(archives) == fetched
    assert (await executor.session.get_bytes("repo/src/app.py"))[1].decode() == merged


async def test_checkout_rebase_repeat_while_its_job_runs_answers_that_job(store, monkeypatch):
    import asyncio

    from app.services.sandbox_workspace import jobs

    composite, executor, _archives, _reads = await _rebasable(store)
    gate = asyncio.Event()
    real = jobs.rebase_running
    tasks: list[asyncio.Task] = []

    async def slow(workspace, key, run_id, label, work):
        async def held():
            await gate.wait()
            return await work()

        task = await real(workspace, key, run_id, label, held)
        tasks.append(task)
        return task

    monkeypatch.setattr(jobs, "rebase_running", slow)
    monkeypatch.setattr(jobs, "POLL_WAIT", 0.05)
    started = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main", background=True)
    run_id = started.content.split("run_id ", 1)[1].split(".", 1)[0]
    repeat = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main", background=True)
    assert not repeat.is_error and f"Rebase {run_id}" in repeat.content and "still running" in repeat.content
    sync_repeat = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main")
    assert f"Rebase {run_id}" in sync_repeat.content
    assert await jobs.rebase_landed(executor.session, {"checkout": "repo", "ref": "main"}) is False
    assert len(tasks) == 1, "the repeats answered the running job, no second rebase"
    gate.set()
    # Wait for the job itself, not a 0.05 s poll: under xdist load the rebase outlives a short poll.
    await tasks[0]
    done = await _call(composite, "sandbox_checkout_rebase", checkout="repo", run_id=run_id)
    assert done.content.startswith("workspace/repo/: rebased from acme/repo@v1"), done.content


async def test_checkout_rebase_job_lost_with_its_worker_is_reported_and_unknown_ids_refused(store):
    import time

    from app.services.sandbox_workspace import jobs

    composite, executor, _archives, _reads = await _rebasable(store)
    key = jobs.rebase_key("repo", "main", "markers")
    stale = time.time() - jobs.STALE_AFTER - 5
    await jobs._put_job(
        executor.session,
        key,
        {"tool": jobs.REBASE, "run_id": "rebase-gone", "label": "repo/ onto main", "status": "running", "at": stale, "heartbeat": stale},
    )
    lost = await _call(composite, "sandbox_checkout_rebase", checkout="repo", run_id="rebase-gone")
    assert lost.is_error and "stopped with its worker" in lost.content
    unknown = await _call(composite, "sandbox_checkout_rebase", checkout="repo", run_id="rebase-nope")
    assert unknown.is_error and "no background rebase rebase-nope" in unknown.content
    # A stale job does not hold a new start back.
    fresh = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main")
    assert not fresh.is_error and fresh.content.startswith("workspace/repo/: rebased from acme/repo@v1")


async def test_checkout_rebase_needs_a_ref_and_a_record(store):
    composite, _executor, _archives, _reads = await _rebasable(store)
    no_ref = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref=" ")
    assert no_ref.is_error and "name the ref" in no_ref.content
    no_record = await _call(composite, "sandbox_checkout_rebase", checkout="work", ref="main")
    assert no_record.is_error and "holds no .xtm-checkout.json" in no_record.content


async def test_checkout_rebase_follows_a_scope_the_ref_widens(store):
    """A ref whose checks.toml runs a check from backend/ widens a backend/app/services checkout on rebase."""
    from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutRecord

    narrow = {"backend/app/services/y.py": b"Y = 1\n", "backend/app/core/x.py": b"X = 1\n"}
    declared = {
        **narrow,
        ".xtm-one/checks.toml": b'[check.unit]\nargv = ["python", "-m", "pytest"]\ncwd = "backend"\n',
        "frontend/src/z.ts": b"export {}\n",
    }
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    composite.add_tools(*_refs_archive_tool({"v1": ("0a1b2c3", narrow), "main": ("9f9f9f9", declared)}, []))
    done = await _call(composite, "materialize_repository", provider="github", repository="acme/repo", ref="v1", sub_paths=["backend/app/services"], path="repo")
    assert not done.is_error, done.content
    assert "widened" not in done.content, "v1 declares no check: nothing widens at checkout"
    await executor.session.put_bytes("repo/backend/app/services/y.py", b"Y = 2\n")
    result = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main")
    assert not result.is_error, result.content
    assert "backend/app/services widened to backend/ (the directory unit run from at the ref)." in result.content
    assert "backend/app/core/x.py" in result.content, "the file newly in scope is taken from the ref"
    assert (await executor.session.get_bytes("repo/backend/app/core/x.py"))[1] == b"X = 1\n"
    record = CheckoutRecord.from_json((await executor.session.get_bytes(f"repo/{RECORD_NAME}"))[1], path="repo")
    assert (record.ref, record.sub_paths) == ("main", ("backend",)), "the record keeps the widened scope"
    assert "backend/app/core/x.py" in record.files
    status = await _call(composite, "sandbox_checkout_status", checkout="repo")
    assert status.content.splitlines()[1:] == ["M backend/app/services/y.py"]


async def test_a_failed_rebase_write_removes_nothing_and_keeps_the_record(store, monkeypatch):
    """The writes and the moved record go first, so a failed write leaves the removals undone."""
    from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutRecord
    from app.services.sandbox_workspace.session import WorkspaceUnavailableError

    composite, executor, _archives, _reads = await _rebasable(store)

    async def broken(*args, **kwargs):
        raise WorkspaceUnavailableError("storage down")

    monkeypatch.setattr(executor.session, "put_tree", broken)
    result = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main")
    assert result.is_error and "was not rebased: storage down" in result.content
    assert (await executor.session.get_bytes("repo/src/old.py"))[1] == b"OLD\n", "the file main removes is still there"
    record = CheckoutRecord.from_json((await executor.session.get_bytes(f"repo/{RECORD_NAME}"))[1], path="repo")
    assert (record.ref, record.commit) == ("v1", "0a1b2c3")


def _compare_tool(
    merge_base: str, seen: list, status: str = "diverged"
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    executor = CallableToolExecutor()

    async def compare_github_commits(repo: str, base: str, head: str) -> ToolResult:
        seen.append((repo, base, head))
        return ToolResult(tool_call_id="", content="compare", structured={"merge_base": merge_base, "status": status})

    executor.register("compare_github_commits", compare_github_commits)
    return [
        ToolDefinition(
            name="compare_github_commits",
            description="compare",
            input_schema={"type": "object", "properties": {"repo": {"type": "string"}, "base": {"type": "string"}, "head": {"type": "string"}}},
            source="integration:github",
            source_label="GitHub",
            repository_compare=True,
        )
    ], executor


async def test_checkout_rebase_replayed_after_it_landed_writes_nothing_twice(store):
    """A rebase whose answer was lost (worker gone) and is replayed finds the checkout on the ref: no second merge."""
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    archives: list = []
    compares: list = []
    composite.add_tools(*_refs_archive_tool({"main": ("9f9f9f9", _MAIN)}, archives))
    composite.add_tools(*_file_tool([], _MAIN))
    composite.add_tools(*_compare_tool("9f9f9f9", compares, status="identical"))
    done = await _call(
        composite,
        "materialize_repository",
        provider="github",
        repository="acme/repo",
        ref="main",
        sub_paths=["src"],
        path="repo",
    )
    assert not done.is_error, done.content
    await executor.session.put_bytes("repo/src/app.py", b"a\nb\nc\nd\nX\n")
    fetched = len(archives)
    again = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main")
    assert not again.is_error, again.content
    assert "already sits on acme/repo@main" in again.content and "nothing was written" in again.content
    assert len(archives) == fetched, "the ref's tree is not downloaded again"
    assert (await executor.session.get_bytes("repo/src/app.py"))[1] == b"a\nb\nc\nd\nX\n"


async def test_checkout_rebase_of_a_branch_keeps_the_directory_it_adds(store):
    """A checkout on a branch adding a directory, rebased onto main, keeps every added file.

    The record holds the branch's files; without the merge base as the base,
    each added file read as "deleted by the ref" and was removed.
    """
    from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutRecord

    added = {f"src/bloodhound/{name}.py": f"{name.upper()} = 1\n".encode() for name in ("a", "b", "c")}
    feat = {**_V1, "src/app.py": b"A\nb\nc\nd\ne\n", **added}
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    archives: list = []
    reads: list = []
    compares: list = []
    trees = {"feat": ("fe47fe4", feat), "main": ("9f9f9f9", _MAIN), "0a1b2c3d": ("0a1b2c3d", _V1)}
    composite.add_tools(*_refs_archive_tool(trees, archives))
    composite.add_tools(*_file_tool(reads, _V1))
    composite.add_tools(*_compare_tool("0a1b2c3d", compares))
    done = await _call(composite, "materialize_repository", provider="github", repository="acme/repo", ref="feat", sub_paths=["src"], path="repo")
    assert not done.is_error, done.content
    result = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main")
    assert not result.is_error, result.content
    text = result.content
    assert compares == [("acme/repo", "main", "fe47fe4")]
    assert archives[-1] == ("acme/repo", "0a1b2c3d"), "the tree at the merge base is the base"
    assert "Your added files kept (3): src/bloodhound/a.py, src/bloodhound/b.py, src/bloodhound/c.py." in text
    assert "Removed by the ref (1): src/old.py." in text
    assert "Merged cleanly (1): src/app.py." in text
    assert "Merged from the merge base 0a1b2c3d of fe47fe4 and main." in text
    assert "In conflict" not in text
    assert reads == [], "the base bytes come from the tree at the merge base"
    for path, data in added.items():
        assert (await executor.session.get_bytes(f"repo/{path}"))[1] == data
    assert (await executor.session.get_bytes("repo/src/app.py"))[1] == b"A\nb\nc\nd\nE\n"
    assert (await executor.session.get_bytes("repo/src/lib/util.py"))[1] == b"VALUE = 2\n"
    record = CheckoutRecord.from_json((await executor.session.get_bytes(f"repo/{RECORD_NAME}"))[1], path="repo")
    assert (record.ref, record.commit) == ("main", "9f9f9f9")
    status = await _call(composite, "sandbox_checkout_status", checkout="repo")
    assert status.content.splitlines()[1:] == [
        "M src/app.py",
        "A src/bloodhound/a.py",
        "A src/bloodhound/b.py",
        "A src/bloodhound/c.py",
    ]


async def test_checkout_rebase_behind_the_ref_keeps_the_record_as_its_base(store):
    """When the merge base is the recorded commit, no second tree is read and the plan is unchanged."""
    composite, executor, archives, _reads = await _rebasable(store)
    compares: list = []
    composite.add_tools(*_compare_tool("0a1b2c3", compares))
    await executor.session.put_bytes("repo/src/mine.py", b"MINE\n")
    result = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main")
    assert not result.is_error, result.content
    assert compares == [("acme/repo", "main", "0a1b2c3")]
    assert archives[-1] == ("acme/repo", "main") and ("acme/repo", "0a1b2c3") not in archives
    assert "Your added files kept (1): src/mine.py." in result.content
    assert "merge base" not in result.content


async def test_checkout_status_names_an_edit_outside_the_recorded_sub_paths(store):
    """A file of an earlier, wider tree edited after a narrow checkout is named, never silently skipped (#10144)."""
    from app.services.sandbox_workspace.ingest import WorkspaceIngestor

    composite, executor, _archives, _reads = await _rebasable(store)
    await executor.session.put_bytes("repo/README.md", b"# edited outside src\n")
    edited = await _call(composite, "sandbox_edit_file", path="repo/src/app.py", edits=[{"find": "a\n", "replace": "A\n"}])
    assert not edited.is_error, edited.content
    written = await _call(composite, "sandbox_write_file", path="repo/src/mine.py", content="MINE\n")
    assert not written.is_error, written.content
    status = await _call(composite, "sandbox_checkout_status", checkout="repo")
    assert status.content.splitlines()[1:] == ["M src/app.py", "A src/mine.py"]
    assert "1 file(s) changed since the checkout lie outside its record and sub_paths" in status.content
    assert "(README.md)" in status.content and 'add_paths=[...]' in status.content
    assert "together with the checkout's sub_paths" in status.content
    # After a rebase (and the same rebase replayed), both edits still show and the stray file is still named:
    # the out-of-scope baseline is carried by the rewritten record, never the record's write time.
    for _ in range(2):
        result = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main")
        assert not result.is_error, result.content
        status = await _call(composite, "sandbox_checkout_status", checkout="repo")
        assert status.content.splitlines()[1:3] == ["M src/app.py", "A src/mine.py"], status.content
        assert "(README.md)" in status.content, status.content
    # The non-empty diff names the outside edit too.
    diff = await _call(composite, "sandbox_checkout_diff", checkout="repo", paths=[])
    assert "(README.md)" in diff.content.splitlines()[0], diff.content
    # Generated output and a nested checkout outside the scope are never named.
    await executor.session.put_bytes("repo/node_modules/x/index.js", b"x\n")
    await executor.session.put_bytes("repo/.pytest_cache/v/cache", b"x\n")
    await executor.session.put_bytes("repo/.xtm/runs/r1/out.txt", b"x\n")
    await executor.session.put_bytes("repo/nested/.xtm-checkout.json", b"{}\n")
    await executor.session.put_bytes("repo/nested/file.py", b"x\n")
    # An edit outside the scope after the rebase is named again, in status and in the from_checkout change set.
    await executor.session.put_bytes("repo/README.md", b"# edited after the rebase\n")
    _record, changes = await WorkspaceIngestor(executor.session).checkout_changes("repo", None)
    assert (changes.modified, changes.added, changes.untracked) == (("src/app.py",), ("src/mine.py",), ("README.md",))
    _record, named = await WorkspaceIngestor(executor.session).checkout_changes("repo", ["README.md"])
    assert named.added == ("README.md",) and named.untracked == ()


async def test_checkout_rebase_of_a_narrow_branch_brings_in_its_files_outside_the_sub_paths(store):
    """A sub_paths checkout of a branch that also changes backend files keeps those changes through a rebase (#11853).

    The trees were read at the sub_paths only: the branch's backend hunks were never merged, the record moved
    to the ref, and add_paths then wrote the ref's versions over them.
    """
    from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutRecord

    base = {**_V1, "backend/x.py": b"a\nb\nc\n", "backend/y.py": b"Y\n"}
    feat = {**base, "src/app.py": b"A\nb\nc\nd\ne\n", "backend/x.py": b"A\nb\nc\n", "backend/new.py": b"NEW\n"}
    main = {**_MAIN, "backend/x.py": b"a\nb\nC\n", "backend/y.py": b"Y2\n"}
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    archives: list = []
    reads: list = []
    trees = {
        "feat": ("fe47fe4", feat),
        "main": ("9f9f9f9", main),
        "9f9f9f9": ("9f9f9f9", main),
        "0a1b2c3d": ("0a1b2c3d", base),
    }
    composite.add_tools(*_refs_archive_tool(trees, archives))
    composite.add_tools(*_file_tool(reads, feat))
    compare = CallableToolExecutor()
    changed = [
        {"filename": "src/app.py", "status": "modified"},
        {"filename": "backend/x.py", "status": "modified"},
        {"filename": "backend/new.py", "status": "added"},
    ]

    async def compare_github_commits(repo: str, base: str, head: str) -> ToolResult:
        return ToolResult(
            tool_call_id="",
            content="compare",
            structured={"merge_base": "0a1b2c3d", "status": "diverged", "files": changed, "files_capped": False},
        )

    compare.register("compare_github_commits", compare_github_commits)
    composite.add_tools(
        [
            ToolDefinition(
                name="compare_github_commits",
                description="compare",
                input_schema={
                    "type": "object",
                    "properties": {"repo": {"type": "string"}, "base": {"type": "string"}, "head": {"type": "string"}},
                },
                source="integration:github",
                source_label="GitHub",
                repository_compare=True,
            )
        ],
        compare,
    )
    done = await _call(
        composite,
        "materialize_repository",
        provider="github",
        repository="acme/repo",
        ref="feat",
        sub_paths=["src"],
        path="repo",
    )
    assert not done.is_error, done.content
    result = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main")
    assert not result.is_error, result.content
    assert (
        "Brought in from the branch, outside the checkout's sub_paths (2): backend/new.py, backend/x.py."
        in result.content
    )
    assert sorted(p for _repo, p, _ref in reads) == ["backend/new.py", "backend/x.py"], "read at the recorded commit"
    assert {ref for _repo, _p, ref in reads} == {"fe47fe4"}
    # The branch's hunk and main's are merged; the branch's added file is kept; main's backend/y.py stays out.
    assert (await executor.session.get_bytes("repo/backend/x.py"))[1] == b"A\nb\nC\n"
    assert (await executor.session.get_bytes("repo/backend/new.py"))[1] == b"NEW\n"
    status = await _call(composite, "sandbox_checkout_status", checkout="repo")
    assert status.content.splitlines()[1:4] == ["A backend/new.py", "M backend/x.py", "M src/app.py"], status.content
    # add_paths afterwards takes main's files the branch did not change and never overwrites the branch's.
    grown = await _call(
        composite,
        "materialize_repository",
        provider="github",
        repository="acme/repo",
        path="repo",
        add_paths=["backend"],
    )
    assert not grown.is_error, grown.content
    assert (await executor.session.get_bytes("repo/backend/x.py"))[1] == b"A\nb\nC\n"
    assert (await executor.session.get_bytes("repo/backend/y.py"))[1] == b"Y2\n"
    record = CheckoutRecord.from_json((await executor.session.get_bytes(f"repo/{RECORD_NAME}"))[1], path="repo")
    assert (record.ref, record.commit) == ("main", "9f9f9f9")
    status = await _call(composite, "sandbox_checkout_status", checkout="repo")
    assert status.content.splitlines()[1:4] == ["A backend/new.py", "M backend/x.py", "M src/app.py"], status.content


async def test_checkout_rebase_refuses_by_name_a_branch_file_outside_it_that_cannot_be_read(store):
    """A file the branch changes outside the checkout that the provider cannot read refuses the rebase, nothing written."""
    from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutRecord

    feat = {**_V1, "src/app.py": b"A\nb\nc\nd\ne\n"}
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, executor = _composite(conversation, user)
    archives: list = []
    trees = {"feat": ("fe47fe4", feat), "main": ("9f9f9f9", _MAIN), "0a1b2c3d": ("0a1b2c3d", _V1)}
    composite.add_tools(*_refs_archive_tool(trees, archives))
    composite.add_tools(*_file_tool([], feat))
    compare = CallableToolExecutor()

    async def compare_github_commits(repo: str, base: str, head: str) -> ToolResult:
        files = [
            {"filename": "src/app.py", "status": "modified"},
            {"filename": "backend/gone.py", "status": "modified"},
        ]
        return ToolResult(
            tool_call_id="",
            content="compare",
            structured={"merge_base": "0a1b2c3d", "status": "diverged", "files": files},
        )

    compare.register("compare_github_commits", compare_github_commits)
    composite.add_tools(
        [
            ToolDefinition(
                name="compare_github_commits",
                description="compare",
                input_schema={
                    "type": "object",
                    "properties": {"repo": {"type": "string"}, "base": {"type": "string"}, "head": {"type": "string"}},
                },
                source="integration:github",
                source_label="GitHub",
                repository_compare=True,
            )
        ],
        compare,
    )
    done = await _call(
        composite,
        "materialize_repository",
        provider="github",
        repository="acme/repo",
        ref="feat",
        sub_paths=["src"],
        path="repo",
    )
    assert not done.is_error, done.content
    fetched = len(archives)
    result = await _call(composite, "sandbox_checkout_rebase", checkout="repo", ref="main")
    assert result.is_error
    assert (
        "changes 1 file(s) outside the checkout's sub_paths" in result.content and "backend/gone.py" in result.content
    )
    assert "add_paths" in result.content and "Nothing was written." in result.content
    assert len(archives) == fetched, "refused before any tree is downloaded"
    record = CheckoutRecord.from_json((await executor.session.get_bytes(f"repo/{RECORD_NAME}"))[1], path="repo")
    assert (record.ref, record.commit) == ("feat", "fe47fe4")


def test_branch_changes_reads_renames_and_removals():
    from app.services.sandbox_workspace import rebase

    changes = rebase.branch_changes(
        {
            "files": [
                {"filename": "a/new.py", "status": "renamed", "previous_filename": "a/old.py"},
                {"filename": "b/x.py", "status": "removed"},
                {"filename": "c/y.py", "status": "added"},
            ],
            "files_capped": True,
        }
    )
    assert changes == rebase.BranchChanges(changed=("a/new.py", "c/y.py"), removed=("a/old.py", "b/x.py"), capped=True)
    assert rebase.branch_changes({"merge_base": "x"}) is None, "a provider that lists no files changes nothing"


def test_the_checkout_tools_are_offered_with_the_workspace_and_the_diff_is_gated():
    from app.services.hitl_whitelist_default import DELIBERATELY_EXCLUDED, SHIPPED_DEFAULT_WHITELIST

    with_ws, _ = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    by_name = {d.name: d for d in with_ws}
    assert by_name["sandbox_checkout_status"].input_schema["required"] == ["checkout"]
    assert set(by_name["sandbox_checkout_diff"].input_schema["properties"]) == {"checkout", "paths", "context_lines"}
    assert "sandbox_checkout_diff" in DELIBERATELY_EXCLUDED
    assert "sandbox_checkout_diff" not in SHIPPED_DEFAULT_WHITELIST
    assert by_name["sandbox_checkout_rebase"].input_schema["required"] == ["checkout"]
    assert {"background", "run_id"} <= set(by_name["sandbox_checkout_rebase"].input_schema["properties"])
    assert by_name["sandbox_checkout_rebase"].landed_probe is not None
    # A resumed turn's repeat of an edit already in the file is answered, reworded included (#12097).
    for name in ("sandbox_edit_file", "sandbox_replace_in_files"):
        assert by_name[name].landed_probe is not None and by_name[name].landed_key is not None, name
    assert by_name["sandbox_replace_in_files"].read_only_call is not None
    assert "sandbox_checkout_rebase" in DELIBERATELY_EXCLUDED
    assert "sandbox_checkout_rebase" not in SHIPPED_DEFAULT_WHITELIST


def test_github_declares_its_repository_file_tool():
    from app.integrations.providers.github import contents

    definitions = contents.register({"Authorization": "token x"}, CallableToolExecutor(), lambda *a, **k: None)
    assert [d.name for d in definitions if d.repository_file] == ["download_github_file"]
    assert [d.name for d in definitions if d.repository_archive] == ["download_github_repository_archive"]


def test_github_declares_its_repository_compare_tool():
    from app.integrations.providers.github import commits

    definitions = commits.register({"Authorization": "token x"}, CallableToolExecutor(), lambda *a, **k: None)
    assert [d.name for d in definitions if d.repository_compare] == ["compare_github_commits"]


# -- Packages -----------------------------------------------------------------


def _wheel(name: str = "tinypkg", version: str = "1.0.0", requires: tuple[str, ...] = ()) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        wheel.writestr(_frozen(f"{name}/__init__.py"), "VALUE = 42\n")
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + "".join(
            f"Requires-Dist: {req}\n" for req in requires
        )
        wheel.writestr(_frozen(f"{name}-{version}.dist-info/METADATA"), metadata)
    return buffer.getvalue()


def _index(wheels: dict[str, bytes], *, tamper: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for name, data in wheels.items():
            filename = f"{name}-1.0.0-py3-none-any.whl"
            if url == f"https://index.test/simple/{name}/":
                sha = hashlib.sha256(data + (b"x" if tamper else b"")).hexdigest()
                body = {"files": [{"filename": filename, "url": f"https://files.test/{filename}", "hashes": {"sha256": sha}}]}
                return httpx.Response(200, json=body, headers={"content-type": "application/vnd.pypi.simple.v1+json"})
            if url == f"https://files.test/{filename}":
                return httpx.Response(200, content=data)
        return httpx.Response(404)

    return lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


async def test_an_allowed_package_is_installed_into_the_workspace_and_imports_next_call(store, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "code_interpreter_package_allowlist", "tinypkg")
    monkeypatch.setattr(settings, "code_interpreter_package_index_url", "https://index.test/simple")
    monkeypatch.setattr("app.core.egress.egress_client", _index({"tinypkg": _wheel(requires=("otherdep>=1",))}))
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    ci = next(d for d in composite.all_tools if d.name == "code_interpreter")
    assert "packages" in ci.input_schema["properties"]
    first = await composite.execute_tool(
        ToolCall(id="1", name="code_interpreter", arguments={"code": "print('ok')", "packages": ["tinypkg==1.0.0"]})
    )
    assert "[Packages: installed into workspace/.venv: tinypkg==1.0.0" in first.content
    assert "dependencies not on the allow-list, not installed" in first.content and "otherdep" in first.content
    second = await composite.execute_tool(ToolCall(id="2", name="code_interpreter", arguments={"code": "import tinypkg\nprint(tinypkg.VALUE)"}))
    assert "Output:\n42" in second.content
    third = await composite.execute_tool(ToolCall(id="3", name="code_interpreter", arguments={"code": "print(1)", "packages": ["tinypkg"]}))
    assert "already installed: tinypkg==1.0.0" in third.content


async def test_a_package_off_the_list_or_with_a_wrong_hash_is_not_installed(store, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "code_interpreter_package_allowlist", "tinypkg")
    monkeypatch.setattr(settings, "code_interpreter_package_index_url", "https://index.test/simple")
    monkeypatch.setattr("app.core.egress.egress_client", _index({"tinypkg": _wheel()}, tamper=True))
    composite, _ = _composite(uuid.uuid4(), uuid.uuid4())
    result = await composite.execute_tool(
        ToolCall(id="1", name="code_interpreter", arguments={"code": "print(1)", "packages": ["requests", "tinypkg"]})
    )
    assert "requests (not on the package allow-list)" in result.content
    assert "sha256 does not match" in result.content
    assert "installed into" not in result.content


async def test_installs_are_off_without_an_allow_list(store):
    from app.services.sandbox_workspace.packages import install

    workspace = WorkspaceSession(WorkspaceScope(user_id=uuid.uuid4(), conversation_id=uuid.uuid4()), store=store)
    report = await install(workspace, ["tinypkg"], pure_only=False)
    assert report.disabled and "installs are disabled" in report.format()
    assert json.dumps(report.installed) == "[]"


# -- Uploads of one commit ----------------------------------------------------


class _SlowBlobs:
    """An object store whose puts take a tick each and count how many are in flight."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.in_flight = 0
        self.peak = 0
        self.fail_on = fail_on

    async def put(self, key: str, data: bytes) -> None:
        import asyncio

        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0)
            if self.fail_on and key.endswith(self.fail_on):
                raise OSError("object store down")
            self.objects[key] = bytes(data)
        finally:
            self.in_flight -= 1

    async def put_path(self, key: str, path: str) -> None:
        with open(path, "rb") as fh:
            await self.put(key, fh.read())

    async def get(self, key: str) -> bytes:
        return self.objects[key]

    async def delete(self, keys) -> int:
        return sum(1 for key in keys if self.objects.pop(key, None) is not None)

    async def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.objects if key.startswith(prefix))


async def test_a_commit_uploads_its_contents_concurrently_and_once_each():
    blobs = _SlowBlobs()
    memory = MemoryWorkspaceStore(blobs=blobs)
    workspace = WorkspaceSession(WorkspaceScope(user_id=uuid.uuid4(), conversation_id=uuid.uuid4()), store=memory)
    files = [(f"src/f{i}.py", f"print({i})\n".encode()) for i in range(40)] + [("src/dup.py", b"print(0)\n")]
    plan = await workspace.put_tree(files, under="repo")
    assert len(plan.diff.added) == 41
    # 40 distinct contents, each stored once, up to UPLOAD_CONCURRENCY at a time.
    assert len(blobs.objects) == 40
    assert 1 < blobs.peak <= session_module.UPLOAD_CONCURRENCY
    assert plan.size_bytes == sum(len(data) for _, data in files)


async def test_an_upload_that_fails_leaves_nothing_of_the_commit_in_the_store():
    blobs = _SlowBlobs(fail_on=hashlib.sha256(b"print(3)\n").hexdigest())
    memory = MemoryWorkspaceStore(blobs=blobs)
    workspace = WorkspaceSession(WorkspaceScope(user_id=uuid.uuid4(), conversation_id=uuid.uuid4()), store=memory)
    files = [(f"src/f{i}.py", f"print({i})\n".encode()) for i in range(8)]
    with pytest.raises(BaseException) as raised:
        await workspace.put_tree(files, under="repo")
    assert "object store down" in str(raised.value) or any(
        "object store down" in str(exc) for exc in getattr(raised.value, "exceptions", ())
    )
    assert blobs.objects == {}
    assert await workspace.entries(refresh=True) == {}


async def test_an_openapi_read_writes_its_whole_text_answer_to_the_workspace(store, monkeypatch):
    """A 500 KB text answer of a declarative GET lands whole; without the path it is cut head and tail."""
    from app.services.declarative_http_executor import make_declarative_handler, spec_is_read

    lines = [f"[task] line {i:06d} of the log" for i in range(17_000)]
    lines.append("ERROR dbt: 2 of 41 models failed: mart_okr, mart_pipeline")
    log = "\n".join(lines) + "\n"
    assert len(log) > 500_000
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200, text=log))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    spec = {"method": "GET", "path": "/dags/{dag}/log", "path_params": ["dag"], "max_response_bytes": 2 * MB}
    assert spec_is_read(spec) and not spec_is_read({"method": "POST", "path": "/x"})
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    executor = CallableToolExecutor()
    executor.register(
        "api_get_log",
        make_declarative_handler(
            spec=spec, tool_name="api_get_log", token="tk", extra_config={"base_url": "https://api.example.test"}, provider_label="Demo"
        ),
    )
    schema = {"type": "object", "properties": {"dag": {"type": "string"}}}
    composite.add_tools([ToolDefinition(name="api_get_log", description="d", input_schema=schema, workspace_ingest=True)], executor)

    result = await composite.execute_tool(
        ToolCall(id="c1", name="api_get_log", arguments={"dag": "okr", WORKSPACE_PATH_ARGUMENT: "logs/okr.log"})
    )
    assert not result.is_error, result.content
    assert "ERROR dbt" not in result.content
    written = await _workspace_bytes(store, conversation, "logs/okr.log")
    assert written == log.encode()
    assert b"mart_okr" in written[-1024:]

    cut = await composite.execute_tool(ToolCall(id="c2", name="api_get_log", arguments={"dag": "okr"}))
    assert len(cut.content) < 21_000
    assert cut.content.startswith("[task] line 000000")
    assert "mart_okr, mart_pipeline" in cut.content
    assert "Call again with to_workspace_path" in cut.content
