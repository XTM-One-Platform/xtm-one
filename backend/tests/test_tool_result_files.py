"""Files in tool results: MCP blocks, provider inlining, persistence wiring.

* An MCP tool result that carries a file gives the agent the file: stored with
  the run scope, its file_id in the text, the file on the result - embedded
  resources (text and blob) included, which used to be dropped, and a
  ``resource_link`` shows its URI.
* A document on a tool result is read as its text by every model provider: a
  PDF on Anthropic / Gemini used to reach the model as a one-line stub.
* Every provider whose download tools store files gets the persistence
  callback, and every channel bot too.
"""

from __future__ import annotations

import base64
import importlib
import inspect
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm.base import FileRef
from app.llm.file_resolver import prepare_tool_result_attachments
from app.services import integration_tools
from app.tools import mcp_client
from app.tools.base import ToolCall, ToolDefinition

_PDF = b"%PDF-1.4 fake"


def _text_pdf(text: str) -> bytes:
    """A valid one-page PDF drawing *text*, hex-encoded so the raw bytes never hold it in plain."""
    content = b"BT /F1 12 Tf 72 720 Td <%s> Tj ET" % text.encode("latin-1").hex().encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    pdf += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n" % (len(objects) + 1, xref)
    return bytes(pdf + b"%%EOF\n")


class _Session:
    def __init__(self, blocks, contents=None):
        self._blocks = blocks
        self._contents = contents or []
        self.reads: list[tuple[str, object]] = []

    async def call_tool(self, _name, _arguments):
        return SimpleNamespace(content=self._blocks)

    async def read_resource(self, uri, read_timeout_seconds=None):
        self.reads.append((uri, read_timeout_seconds))
        if isinstance(self._contents, Exception):
            raise self._contents
        return SimpleNamespace(contents=self._contents)


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
            source="mcp",
        )

    async def _noop(_key):
        return None

    monkeypatch.setattr("app.services.builtin_tools._shared.persist_tool_artifact", _fake_persist)
    monkeypatch.setattr(mcp_client, "_reset_mcp_auth_failures", _noop)
    return calls


async def _run(blocks):
    conn = mcp_client.MCPConnection(
        mcp_client.MCPServerRuntime(name="docs", transport="stdio", command="fake", server_id="s1"),
        artifact_scope=mcp_client.MCPArtifactScope(user_id="u1", conversation_id="c1"),
    )
    conn._session = _Session(blocks)
    conn._connected = True
    return await conn.execute_tool(ToolCall(id="t1", name="fetch_doc", arguments={}))


@pytest.mark.asyncio
async def test_an_mcp_image_names_its_file_id(persisted):
    result = await _run([SimpleNamespace(text=None, data=base64.b64encode(b"\x89PNG").decode(), mimeType="image/png")])
    (ref,) = result.files
    assert f"file_id={ref.file_id}" in result.content
    assert f"[[FILE:{ref.file_id}]]" in result.content


@pytest.mark.asyncio
async def test_an_mcp_blob_resource_is_stored_with_its_uri(persisted):
    resource = SimpleNamespace(
        uri="file:///reports/q3.pdf", mimeType="application/pdf", blob=base64.b64encode(_PDF).decode(), text=None
    )
    result = await _run([SimpleNamespace(type="resource", resource=resource)])
    (call,) = persisted
    assert call["raw_bytes"] == _PDF and call["filename"] == "q3.pdf"
    assert call["conversation_id"] == "c1"
    (ref,) = result.files
    assert "file:///reports/q3.pdf" in result.content
    assert f"file_id={ref.file_id}" in result.content


@pytest.mark.asyncio
async def test_an_mcp_text_resource_is_stored_and_redacted(persisted):
    secret = "postgresql://svc:hunter2@db.internal:5432/app"
    resource = SimpleNamespace(uri="memo://notes/today", mimeType="text/markdown", text=f"# Notes\ntoken {secret}")
    result = await _run([SimpleNamespace(type="resource", resource=resource)])
    (call,) = persisted
    assert call["raw_bytes"].startswith(b"# Notes")
    assert secret.encode() not in call["raw_bytes"]
    assert result.files and "memo://notes/today" in result.content


@pytest.mark.asyncio
async def test_an_mcp_resource_link_shows_its_uri(persisted):
    link = SimpleNamespace(
        type="resource_link", uri="https://docs.example.com/a.pdf", name="a.pdf", mimeType="application/pdf", size=10
    )
    result = await _run([SimpleNamespace(text="found one"), link])
    assert "https://docs.example.com/a.pdf" in result.content
    assert "found one" in result.content
    assert "from MCP server 'docs'" in result.content
    assert 'read_mcp_resource(uri, server="s1")' in result.content, "the tool that reads it, and the server's id"
    assert persisted == []


# -- read_mcp_resource: an on-demand, bounded resources/read ---------


def _link(uri: str = "https://docs.example.com/a.pdf", **fields) -> SimpleNamespace:
    return SimpleNamespace(type="resource_link", uri=uri, name="a.pdf", mimeType="application/pdf", **fields)


async def _linked(name: str, blocks: list, contents=None, *, server_id: str | None = None):
    """A connection of server *name* whose tool returned *blocks*, and the session its reads reach."""
    conn = mcp_client.MCPConnection(
        mcp_client.MCPServerRuntime(name=name, transport="stdio", command="fake", server_id=server_id or name),
        artifact_scope=mcp_client.MCPArtifactScope(user_id="u1", conversation_id="c1"),
    )
    session = _Session(blocks, contents)
    conn._session = session
    conn._connected = True
    await conn.execute_tool(ToolCall(id="t1", name="list_docs", arguments={}))
    return conn, session


def _manager(*connections) -> mcp_client.MCPManager:
    manager = mcp_client.MCPManager()
    manager._connections.extend(connections)
    return manager


def _read(**arguments) -> ToolCall:
    return ToolCall(id="r1", name=mcp_client.READ_RESOURCE_TOOL_NAME, arguments=arguments)


@pytest.mark.asyncio
async def test_a_linked_resource_is_read_on_demand_and_stored_as_a_file_of_the_run(persisted):
    blob = SimpleNamespace(uri="https://docs.example.com/a.pdf", mimeType=None, blob=base64.b64encode(_PDF).decode(), text=None)
    conn, session = await _linked("docs", [_link(size=len(_PDF))], [blob])

    result = await _manager(conn).execute_tool(_read(uri="https://docs.example.com/a.pdf"))

    assert not result.is_error
    assert session.reads == [("https://docs.example.com/a.pdf", mcp_client._RESOURCE_READ_TIMEOUT)]
    (call,) = persisted
    assert call["raw_bytes"] == _PDF and call["filename"] == "a.pdf"
    assert call["content_type"] == "application/pdf", "the link's type when the content declares none"
    assert (call["user_id"], call["conversation_id"], call["source"]) == ("u1", "c1", "mcp")
    (ref,) = result.files
    assert f"file_id={ref.file_id}" in result.content and f"[[FILE:{ref.file_id}]]" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("uri", ["file:///etc/passwd", "https://docs.example.com/b.pdf"])
async def test_a_uri_no_tool_of_the_run_linked_is_never_read(persisted, uri):
    conn, session = await _linked("docs", [_link()], [])

    result = await _manager(conn).execute_tool(_read(uri=uri))

    assert result.is_error and "reads only a resource link an MCP tool returned" in result.content
    assert session.reads == [] and persisted == []


@pytest.mark.asyncio
async def test_a_server_tool_of_the_same_name_never_receives_the_platform_read(persisted):
    text = SimpleNamespace(uri="https://docs.example.com/a.pdf", mimeType="text/plain", text="linked")
    conn, session = await _linked("docs", [_link()], [text])
    conn._tools = [ToolDefinition(name=mcp_client.READ_RESOURCE_TOOL_NAME, description="server's own")]

    result = await _manager(conn).execute_tool(_read(uri="https://docs.example.com/a.pdf"))

    assert not result.is_error and len(session.reads) == 1, "the call reads the link, it is not sent as a tool call"


@pytest.mark.asyncio
async def test_a_link_declared_over_the_file_limit_is_not_read(persisted):
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES

    conn, session = await _linked("docs", [_link(size=MAX_UPLOAD_SIZE_BYTES + 1)], [])

    result = await _manager(conn).execute_tool(_read(uri="https://docs.example.com/a.pdf"))

    assert result.is_error and "over the" in result.content
    assert session.reads == [] and persisted == []


@pytest.mark.asyncio
async def test_contents_past_the_file_limit_are_not_stored(persisted, monkeypatch):
    from app.services import file_storage

    monkeypatch.setattr(file_storage, "MAX_UPLOAD_SIZE_BYTES", 40)
    first = SimpleNamespace(uri="memo://notes/one.md", mimeType="text/markdown", text="# One\n" + "x" * 20)
    second = SimpleNamespace(uri="memo://notes/two.md", mimeType="text/markdown", text="# Two\n" + "y" * 30)
    conn, _session = await _linked("notes", [_link("memo://notes/")], [first, second])

    result = await _manager(conn).execute_tool(_read(uri="memo://notes/"))

    (call,) = persisted
    assert call["filename"] == "one.md"
    assert "[Resource: memo://notes/two.md - not stored: over the" in result.content
    assert len(result.files) == 1


@pytest.mark.asyncio
async def test_two_servers_that_linked_the_same_uri_need_the_server_named(persisted):
    text = SimpleNamespace(uri="https://docs.example.com/a.pdf", mimeType="text/plain", text="shared")
    docs, docs_session = await _linked("docs", [_link()], [text])
    wiki, wiki_session = await _linked("wiki", [_link()], [text])
    manager = _manager(docs, wiki)

    ambiguous = await manager.execute_tool(_read(uri="https://docs.example.com/a.pdf"))
    named = await manager.execute_tool(_read(uri="https://docs.example.com/a.pdf", server="wiki"))

    assert ambiguous.is_error and '"docs" (docs), "wiki" (wiki)' in ambiguous.content
    assert not named.is_error
    assert docs_session.reads == [] and len(wiki_session.reads) == 1


@pytest.mark.asyncio
async def test_two_servers_of_the_same_name_are_told_apart_by_the_id_on_the_link_line(persisted):
    text = SimpleNamespace(uri="https://docs.example.com/a.pdf", mimeType="text/plain", text="shared")
    first, first_session = await _linked("Docs", [_link()], [text], server_id="id-1")
    second, second_session = await _linked("Docs", [_link()], [text], server_id="id-2")
    manager = _manager(first, second)

    by_name = await manager.execute_tool(_read(uri="https://docs.example.com/a.pdf", server="Docs"))
    by_id = await manager.execute_tool(_read(uri="https://docs.example.com/a.pdf", server="id-2"))

    assert by_name.is_error and '"id-1" (Docs), "id-2" (Docs)' in by_name.content
    assert not by_id.is_error
    assert first_session.reads == [] and len(second_session.reads) == 1


def test_a_text_over_the_limit_is_not_encoded_to_be_measured():
    long_text = SimpleNamespace(blob=None, text="\u00e9" * 50)

    assert mcp_client._resource_contents_size(long_text, 40) == 50, "over the limit by its length alone"
    assert mcp_client._resource_contents_size(long_text, 100) == 100, "measured in UTF-8 bytes within it"


@pytest.mark.asyncio
async def test_a_failed_read_is_an_error_result_naming_the_server(persisted):
    conn, _session = await _linked("docs", [_link()], TimeoutError("Timed out while waiting for response"))

    result = await _manager(conn).execute_tool(_read(uri="https://docs.example.com/a.pdf"))

    assert result.is_error and "MCP server 'docs' could not read" in result.content
    assert persisted == []


@pytest.mark.asyncio
async def test_the_session_read_carries_the_uri_and_its_timeout():
    sent: list = []

    async def _send_request(request, result_type, request_read_timeout_seconds=None):
        sent.append((request, result_type, request_read_timeout_seconds))
        return "result"

    fake = SimpleNamespace(send_request=_send_request)
    timeout = mcp_client._RESOURCE_READ_TIMEOUT

    assert await mcp_client.ClientSession.read_resource(fake, "file:///reports/q3.pdf", timeout) == "result"
    ((request, result_type, sent_timeout),) = sent
    assert request.root.method == "resources/read" and str(request.root.params.uri) == "file:///reports/q3.pdf"
    assert result_type is mcp_client.mcp_types.ReadResourceResult and sent_timeout == timeout


def test_the_reader_is_offered_only_with_mcp_connections():
    manager = mcp_client.MCPManager()
    assert manager.resource_tools() == []
    manager._connections.append(object())
    (definition,) = manager.resource_tools()
    assert definition.name == "read_mcp_resource" and definition.input_schema["required"] == ["uri"]


@pytest.mark.asyncio
async def test_a_pdf_on_a_tool_result_is_read_as_text_by_every_provider():
    pdf = FileRef.from_bytes(_text_pdf("Quarterly findings: three critical issues."), "findings.pdf", "application/pdf")
    for provider in ("anthropic", "gemini", "openai", "bedrock"):
        images, texts = await prepare_tool_result_attachments([pdf], provider, source="t")
        assert images == []
        assert any("three critical issues" in t for t in texts), provider


def test_every_provider_that_stores_downloads_gets_the_persistence_callback():
    """A builder that reads ``persist_file_fn`` is useless unless it is injected."""

    def reads_persist(builder) -> bool:
        module = importlib.import_module(builder.__module__)
        root = Path(inspect.getfile(module))
        files = root.parent.rglob("*.py") if root.name == "__init__.py" else [root]
        return any("persist_file_fn" in f.read_text(encoding="utf-8") for f in files)

    for key, builder in integration_tools.PROVIDER_TOOLS.items():
        if reads_persist(builder):
            assert key in integration_tools._PERSIST_FILE_PROVIDERS, key
    for key, builder in integration_tools.CHANNEL_BOT_TOOLS.items():
        if reads_persist(builder):
            assert key in integration_tools._PERSIST_FILE_CHANNEL_BOTS, key
