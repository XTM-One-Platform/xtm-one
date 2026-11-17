"""A binary MCP tool result is persisted with the run's scope.

It used to be written as a bare ``FileAttachment`` with no user, conversation
or assignment run: the next chat turn's ``FileContext`` (rebuilt by
``conversation_id``) never saw it, an assignment run never listed it, and a
HITL resume (``load_run_generated_files``, by ``assignment_run_id``) could not
restore it.
"""

from __future__ import annotations

import base64
import sys
import uuid
from types import SimpleNamespace

import pytest

from app.llm.base import FileRef
from app.tools import mcp_client
from app.tools.base import ToolCall


def _async_runtime(build):
    """``build_mcp_runtime`` is a coroutine (it reads vault references)."""

    async def _build(server):
        return build(server)

    return _build


_PNG = b"\x89PNG\r\n\x1a\nfake"


class _BlobSession:
    async def call_tool(self, _name, _arguments):
        return SimpleNamespace(
            content=[
                SimpleNamespace(text="here is the chart"),
                SimpleNamespace(
                    text=None,
                    data=base64.b64encode(_PNG).decode(),
                    mimeType="image/png",
                ),
            ]
        )


def _connection(scope):
    conn = mcp_client.MCPConnection(
        mcp_client.MCPServerRuntime(
            name="charts", transport="stdio", command="fake", server_id="srv-1"
        ),
        artifact_scope=scope,
    )
    conn._session = _BlobSession()
    conn._connected = True
    return conn


@pytest.fixture
def persisted(monkeypatch):
    calls: list[dict] = []

    async def _fake_persist(**kwargs):
        calls.append(kwargs)
        return FileRef(
            file_id=str(uuid.uuid4()),
            filename=kwargs["filename"],
            content_type=kwargs["content_type"],
            size=len(kwargs["raw_bytes"]),
            s3_object_name="mcp/fake",
            source=kwargs["source"],
        )

    async def _noop(_key):
        return None

    monkeypatch.setattr(
        "app.services.builtin_tools._shared.persist_tool_artifact", _fake_persist
    )
    monkeypatch.setattr(mcp_client, "_reset_mcp_auth_failures", _noop)
    return calls


@pytest.mark.asyncio
async def test_binary_result_is_persisted_with_the_run_scope(persisted):
    conv_id, user_id, run_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    conn = _connection(
        mcp_client.MCPArtifactScope(
            user_id=user_id, conversation_id=conv_id, assignment_run_id=run_id
        )
    )

    result = await conn.execute_tool(ToolCall(id="c1", name="render_chart", arguments={}))

    assert not result.is_error
    (call,) = persisted
    assert call["user_id"] == user_id
    assert call["conversation_id"] == conv_id
    assert call["assignment_run_id"] == run_id
    assert call["source"] == "mcp"
    assert call["raw_bytes"] == _PNG
    assert call["content_type"] == "image/png"
    # The ref rides on the result, so the loop registers it in the FileContext.
    assert result.files and result.files[0].source == "mcp"
    assert result.files[0].is_agent_generated
    assert "[Binary content:" in result.content


@pytest.mark.asyncio
async def test_no_scope_still_persists_unscoped_instead_of_failing(persisted):
    conn = _connection(None)

    result = await conn.execute_tool(ToolCall(id="c1", name="render_chart", arguments={}))

    assert not result.is_error
    (call,) = persisted
    assert call["user_id"] == ""
    assert call["conversation_id"] is None
    assert call["assignment_run_id"] is None


@pytest.mark.asyncio
async def test_a_test_tool_call_scopes_its_binary_result_to_the_tester(monkeypatch):
    """The MCP server page's "Test tool" runs a real call: a binary result is
    the tester's own file, never an unscoped row only an admin could read."""
    from app.api.v1 import mcp_servers
    from app.api.v1.schemas.mcp_server import MCPToolTestRequest

    managers: list = []

    class _Manager:
        def __init__(self, artifact_scope=None):
            self.artifact_scope = artifact_scope
            managers.append(self)

        async def connect_server(self, _runtime, force=False):
            return []

        async def execute_tool(self, _call):
            return SimpleNamespace(is_error=False, content="[Binary content: chart.png]")

        async def disconnect_all(self):
            return None

    async def _server(*_args, **_kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(mcp_servers, "get_manageable_entity", _server)
    monkeypatch.setattr(mcp_servers, "build_mcp_runtime", _async_runtime(lambda _srv: object()))
    monkeypatch.setattr(mcp_client, "MCPManager", _Manager)
    tester = SimpleNamespace(id=uuid.uuid4())

    result = await mcp_servers.test_mcp_tool(
        str(uuid.uuid4()), MCPToolTestRequest(tool_name="render_chart"), user=tester, db=object()
    )

    assert result["status"] == "ok"
    (manager,) = managers
    assert manager.artifact_scope == mcp_client.MCPArtifactScope(user_id=str(tester.id))


def test_the_manager_hands_its_scope_to_every_connection():
    scope = mcp_client.MCPArtifactScope(user_id="u", conversation_id="c")
    manager = mcp_client.MCPManager(artifact_scope=scope)
    manager.register_cached_server(
        mcp_client.MCPServerRuntime(name="charts", transport="stdio", command="fake"),
        [{"name": "render_chart", "description": "", "input_schema": {"type": "object"}}],
    )
    assert manager._connections[0].artifact_scope is scope


@pytest.mark.asyncio
async def test_load_custom_and_mcp_tools_scopes_mcp_like_the_run(monkeypatch):
    """The chat / GA loader passes the same user / conversation / run it gives
    the custom tools, i.e. the scope ``generate_file`` persists with."""
    from app.services.builtin_tools import loaders

    seen: dict = {}

    async def _fake_load_mcp(_db, _ids, _composite, allowed_tools=None, artifact_scope=None):
        seen["scope"] = artifact_scope
        return None

    monkeypatch.setattr(loaders, "load_mcp_tools", _fake_load_mcp)
    await loaders.load_custom_and_mcp_tools(
        object(),
        custom_tool_ids=[],
        mcp_ids=["m1"],
        composite=object(),
        user_id="u1",
        conversation_id="c1",
        assignment_run_id="r1",
    )
    assert seen["scope"] == mcp_client.MCPArtifactScope(
        user_id="u1", conversation_id="c1", assignment_run_id="r1"
    )


@pytest.mark.asyncio
async def test_the_custom_only_path_never_imports_the_mcp_client(monkeypatch):
    """A turn with custom tools and no MCP server stays on the legacy fast path:
    ``app.tools.mcp_client`` (the MCP SDK transports) is never imported for it."""
    from app.services.builtin_tools import loaders

    loaded: list = []

    async def _fake_load_custom(_db, ids, _composite, **_kwargs):
        loaded.append(ids)

    monkeypatch.setattr(loaders, "load_custom_tools", _fake_load_custom)
    # ``None`` in ``sys.modules`` makes any import of the module raise.
    monkeypatch.setitem(sys.modules, "app.tools.mcp_client", None)

    manager = await loaders.load_custom_and_mcp_tools(
        object(),
        custom_tool_ids=["t1"],
        mcp_ids=[],
        composite=object(),
        user_id="u1",
        conversation_id="c1",
    )
    assert manager is None
    assert loaded == [["t1"]]


@pytest.mark.asyncio
async def test_an_mcp_download_is_readable_and_editable_on_the_next_turn(monkeypatch):
    """The regression boundary end to end, with only storage and the table faked:
    the MCP result is persisted with the conversation, the next turn's
    ``load_conversation_files`` finds it, ``read_file_content`` resolves it by
    id and by ``[[FILE:<id>]]`` marker, and ``edit_file_content`` revises it."""
    from app.services import agent_chat
    from app.services.builtin_tools import build_generate_file_tool

    csv = b"indicator,score\nevil.example,90\n"
    conv_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
    table: list = []
    storage: dict[str, bytes] = {}

    async def _fake_persist_generated_file(**kwargs):
        row = SimpleNamespace(
            id=uuid.uuid4(),
            filename=kwargs["filename"],
            content_type=kwargs["content_type"],
            size=len(kwargs["raw_bytes"]),
            s3_object_name=f"chat/{kwargs['conversation_id']}/{kwargs['filename']}",
            source=kwargs["source"],
            conversation_id=kwargs["conversation_id"],
            assignment_run_id=kwargs["assignment_run_id"],
            user_id=kwargs["user_id"],
            description=None,
        )
        storage[row.s3_object_name] = kwargs["raw_bytes"]
        table.append(row)
        return row

    async def _download(name):
        return storage[name]

    async def _noop(_key):
        return None

    monkeypatch.setattr(
        "app.services.builtin_tools._shared._persist_generated_file",
        _fake_persist_generated_file,
    )
    monkeypatch.setattr(
        "app.services.builtin_tools._persist_generated_file", _fake_persist_generated_file
    )
    monkeypatch.setattr("app.services.file_storage.file_storage.download", _download)
    monkeypatch.setattr(mcp_client, "_reset_mcp_auth_failures", _noop)

    class _CsvSession:
        async def call_tool(self, _name, _arguments):
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        text=None, data=base64.b64encode(csv).decode(), mimeType="text/csv"
                    )
                ]
            )

    conn = _connection(
        mcp_client.MCPArtifactScope(user_id=user_id, conversation_id=conv_id)
    )
    conn._session = _CsvSession()
    result = await conn.execute_tool(ToolCall(id="c1", name="export_iocs", arguments={}))
    assert not result.is_error
    (row,) = table
    assert row.conversation_id == conv_id and row.source == "mcp"

    class _ConvDB:
        async def execute(self, _stmt):
            rows = [r for r in table if r.conversation_id == conv_id]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    ctx = await agent_chat.load_conversation_files(_ConvDB(), conv_id)
    assert [ref.file_id for ref in ctx.list_files()] == [str(row.id)]

    _defs, executor = build_generate_file_tool(
        db=None, user_id=user_id, conversation_id=conv_id
    )
    executor.file_context = ctx
    for spelling in (str(row.id), f"[[FILE:{row.id}]]"):
        read = await executor.execute_tool(
            ToolCall(id="r", name="read_file_content", arguments={"file_id": spelling})
        )
        assert read.is_error is False, read.content
        assert "evil.example,90" in read.content

    edit = await executor.execute_tool(
        ToolCall(
            id="e",
            name="edit_file_content",
            arguments={
                "file_id": str(row.id),
                "edits": [{"find": "evil.example,90", "replace": "evil.example,95"}],
            },
        )
    )
    assert edit.is_error is False, edit.content
    revision = table[-1]
    assert revision is not row
    assert storage[revision.s3_object_name] == b"indicator,score\nevil.example,95\n"


def test_the_assignment_engine_scopes_mcp_to_the_run():
    """Tripwire: the engine hands ``load_mcp_tools`` a scope carrying the run id,
    which is what ``load_run_generated_files`` restores on a HITL resume - built
    only when the run has MCP servers, so a run without any never imports the
    MCP SDK."""
    import inspect

    from app.services import assignment_engine

    source = inspect.getsource(assignment_engine)
    start = source.index("mcp_ids = assignment.tool_sources")
    call_start = source.index("mcp_manager = await load_mcp_tools(", start)
    setup = source[start:call_start]
    call = source[call_start : source.index("\n            )\n", call_start)]
    assert setup.index("if mcp_ids:") < setup.index("from app.tools.mcp_client import")
    assert "user_id=_run_file_owner_id" in setup
    assert "assignment_run_id=str(run.id)" in setup
    assert "artifact_scope=mcp_scope" in call
