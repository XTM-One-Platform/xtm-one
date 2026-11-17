"""Files the public widget's MCP tools produce belong to the widget conversation.

The widget used to build an unscoped ``MCPManager()`` and run the loop with no
``FileContext``: a binary MCP result became an orphan row only admins could
read, and the visitor never saw it. These tests pin the wiring; who may
download the row is pinned on PostgreSQL in
``test_public_chat_mcp_files_postgres.py``.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import public_chat
from app.api.v1.public_chat import WIDGET_FILE_NOT_FOUND, WIDGET_FILES_KEY, PublicMessageRequest
from app.tools.base import FileContext
from app.tools.mcp_client import MCPArtifactScope
from tests.test_public_chat_hardening import _FakeDB, _make_public_agent, _patch_chat_pipeline

_FILE = {"file_id": str(uuid.uuid4()), "filename": "chart.png", "content_type": "image/png", "size": 11}


def _mcp_server():
    return SimpleNamespace(
        id=uuid.uuid4(), enabled=True, name="charts", transport="http", command=None, args=[],
        env_enc=None, url="http://localhost:9999", headers_enc=None, auth_mode="headers",
        access_token_enc=None, is_company_managed=False,
    )


async def test_the_widget_scopes_mcp_downloads_to_its_conversation(monkeypatch):
    recorder = _patch_chat_pipeline(monkeypatch)
    agent = _make_public_agent(mcp_server_ids=[str(uuid.uuid4())], channel_config={"web_embed": {"allow_tools": True}})
    session_id = str(uuid.uuid4())
    ctx = FileContext()

    await public_chat._run_public_agent_chat(
        _FakeDB({"MCPServer": [_mcp_server()]}), agent, [], "draw it", session_id=session_id, file_context=ctx
    )

    (manager,) = recorder["mcp_managers"]
    # The conversation and nobody's user: only its session reads the file.
    assert manager.artifact_scope == MCPArtifactScope(conversation_id=session_id)
    # The loop registers the turn's tool files where the endpoint reads them.
    assert recorder["loop_kwargs"]["file_context"] is ctx


class _FakeSession:
    def __init__(self, conv):
        self._conv = conv

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self._conv)

    async def commit(self):
        return None


class _FakeRedis:
    async def set(self, *_args, **_kwargs):
        # The session's turn is free: the send takes it.
        return True

    async def delete(self, *_args):
        return None

    async def exists(self, *_args):
        return 0


@pytest.fixture
def send_path(monkeypatch):
    """Stub everything around a widget turn whose tool produced one file."""
    conv = SimpleNamespace(id=uuid.uuid4(), title="Web chat with A", channel_thread_id=None)
    agent = SimpleNamespace(id=uuid.uuid4(), llm_provider=None, llm_model=None)
    stored: list[dict] = []

    @contextlib.asynccontextmanager
    async def _session():
        yield _FakeSession(conv)

    async def _noop(*_args, **_kwargs):
        return None

    async def _true(*_args, **_kwargs):
        return True

    async def _agent(*_args, **_kwargs):
        return agent

    async def _store(_db, _conv_id, role, content, channel="web", metadata_json=None, **_kw):
        stored.append({"role": role, "content": content, "metadata_json": metadata_json})

    async def _history(*_args, **_kwargs):
        return []

    async def _redis():
        return _FakeRedis()

    async def _run(*_args, file_context=None, **_kwargs):
        file_context.add(SimpleNamespace(file_id=_FILE["file_id"]))
        return "Here is your chart."

    async def _turn_files(conversation_id, file_context, known=frozenset(), _reply_text=None):
        assert conversation_id == conv.id
        return [_FILE] if file_context.get(_FILE["file_id"]) and _FILE["file_id"] not in known else []

    async def _session_files(_conversation_id, _file_context):
        return set()

    monkeypatch.setattr(public_chat, "async_session", _session)
    monkeypatch.setattr(public_chat, "_load_session_files", _session_files)
    monkeypatch.setattr(public_chat, "enforce_rate_limit", _noop)
    monkeypatch.setattr(public_chat, "_resolve_agent", _agent)
    monkeypatch.setattr(public_chat, "_run_public_agent_chat", _run)
    monkeypatch.setattr(public_chat, "_widget_turn_files", _turn_files)
    monkeypatch.setattr("app.api.v1.platform.is_chat_enabled", _true)
    monkeypatch.setattr("app.channels.router.store_message", _store)
    monkeypatch.setattr("app.channels.router.get_conversation_history", _history)
    monkeypatch.setattr("app.core.redis.redis_client", _redis)
    return SimpleNamespace(conv=conv, stored=stored)


async def test_the_answer_carries_and_stores_its_files(send_path):
    response = await public_chat.send_public_message(
        "tok", str(send_path.conv.id), PublicMessageRequest(content="draw it"), request=SimpleNamespace()
    )

    assert json.loads(response.body)["files"] == [_FILE]
    assistant = send_path.stored[-1]
    assert assistant["role"] == "assistant"
    assert json.loads(assistant["metadata_json"]) == {WIDGET_FILES_KEY: [_FILE]}


async def test_the_streamed_answer_carries_and_stores_its_files(send_path):
    response = await public_chat.send_public_message_stream(
        "tok", str(send_path.conv.id), PublicMessageRequest(content="draw it"), request=SimpleNamespace()
    )
    chunks = [c.decode() if isinstance(c, bytes | bytearray) else c async for c in response.body_iterator]
    events = [json.loads(line.removeprefix("data: ")) for line in "".join(chunks).split("\n\n") if line.strip()]

    assert events[-1] == {"type": "done", "content": "Here is your chart.", "files": [_FILE]}
    assert json.loads(send_path.stored[-1]["metadata_json"]) == {WIDGET_FILES_KEY: [_FILE]}


def test_the_transcript_restores_the_files_of_an_answer():
    with_files = SimpleNamespace(role="assistant", content="Here.", metadata_json=json.dumps({WIDGET_FILES_KEY: [_FILE]}))
    assert public_chat._public_message(with_files) == {"role": "assistant", "content": "Here.", "files": [_FILE]}

    # The platform's own ``attachments`` block is not a widget file, and a
    # malformed metadata never breaks the transcript.
    for metadata_json in (json.dumps({"attachments": [_FILE]}), "not json", None):
        message = SimpleNamespace(role="assistant", content="Here.", metadata_json=metadata_json)
        assert public_chat._public_message(message) == {"role": "assistant", "content": "Here."}


@pytest.mark.parametrize(
    ("session_id", "file_id"),
    [("not-a-uuid", str(uuid.uuid4())), (str(uuid.uuid4()), "not-a-uuid"), ("", "")],
    ids=["malformed-session", "malformed-file", "empty"],
)
async def test_a_malformed_id_gets_the_unknown_file_answer(monkeypatch, session_id, file_id):
    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(public_chat, "enforce_rate_limit", _noop)
    with pytest.raises(HTTPException) as refused:
        await public_chat.download_session_file("tok", session_id, file_id, request=SimpleNamespace())
    assert (refused.value.status_code, refused.value.detail) == (404, WIDGET_FILE_NOT_FOUND)


class _RowSession:
    """``async_session()`` stand-in whose one query answers *row*."""

    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self._row)


@pytest.mark.parametrize(
    ("filename", "content_type", "inline"),
    [("chart.png", "image/png", True), ("page.html", "text/html", False), ("mark.svg", "image/svg+xml", False)],
)
async def test_the_session_download_answers_the_file_contract(monkeypatch, filename, content_type, inline):
    """What the visitor's browser receives: no cache may keep it (the capability
    rides in the URL), a model-authored document is a download - never a page on
    the platform origin - and the embedding site may read it."""
    row = SimpleNamespace(
        id=uuid.uuid4(), filename=filename, content_type=content_type, s3_object_name=f"chat/c/{filename}", source="mcp"
    )

    async def _noop(*_args, **_kwargs):
        return None

    async def _agent(*_args, **_kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    async def _download(name):
        assert name == row.s3_object_name
        return b"widget bytes"

    monkeypatch.setattr(public_chat, "enforce_rate_limit", _noop)
    monkeypatch.setattr(public_chat, "_resolve_agent", _agent)
    monkeypatch.setattr(public_chat, "async_session", lambda: _RowSession(row))
    monkeypatch.setattr("app.services.file_storage.file_storage.download", _download)

    response = await public_chat.download_session_file(
        "tok", str(uuid.uuid4()), str(row.id), request=SimpleNamespace()
    )

    assert response.status_code == 200
    assert response.body == b"widget bytes"
    assert response.headers["cache-control"] == "private, no-store"
    assert "etag" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"
    if inline:
        assert response.headers["content-disposition"] == "inline"
    else:
        assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["access-control-allow-origin"] == "*"


def test_the_widget_links_a_file_to_its_session_route():
    """Tripwire: the chip the widget draws points at ``download_session_file``
    and is built as DOM, never through the answer's markdown."""
    source = public_chat.WIDGET_JS_PATH.read_text(encoding="utf-8")
    route = next(r.path for r in public_chat.router.routes if getattr(r, "endpoint", None) is public_chat.download_session_file)
    assert route == "/chat/{web_token}/sessions/{session_id}/files/{file_id}"
    start = source.index("function appendFileChips")
    chips = source[start : source.index("async function sendMessage", start)]
    assert 'sessionRoute() + "/files/" + f.file_id' in chips
    assert "textContent" in chips and "innerHTML" not in chips
    # The session route the chips share with the images an answer shows.
    session_route = source[source.index("function sessionRoute") : start]
    assert 'API + "/chat/" + encodeURIComponent(TOKEN) + "/sessions/" + encodeURIComponent(sessionId)' in session_route
