"""Platform chat (OCTI/OAEV embedded "Ask Ariane") agent-generated files.

The *native* chat surfaces (``api/v1/chat.py``) surface agent-generated file
attachments, and so does ``api/v1/platform.py`` — the endpoint the embedded
chatbot in OpenCTI / OpenAEV proxies to.  These tests pin the platform-chat
surfacing contract:

  * ``_platform_attachments_from_metadata`` parses the stored attachment
    blocks defensively and never adds an absolute download URL.
  * ``_collect_generated_attachments`` diffs the post-run ``FileContext``
    against the pre-run file ids to isolate newly generated files and maps
    them to the chip metadata shape (without a download URL — the proxy
    builds it).
  * ``_link_generated_attachments`` binds ``conversation_id`` and
    ``message_id`` through the shared atomic bind.
  * The non-streaming endpoint surfaces ``attachments`` on the response
    and persists them in the assistant message metadata, both narrowed to
    the rows the bind reported (``_narrow_attachment_meta`` keeps every
    other metadata key).

The download URL is intentionally absent from every payload: OpenCTI and
OpenAEV reach XTM One through a backend proxy that mints the XTM One JWT
server-side, so the chatbot resolves downloads against its own
``apiBaseUrl`` and the end user never authenticates to XTM One.
"""

import json
import uuid
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.api.v1 import platform as platform_api


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Savepoint:
    """``AsyncSession.begin_nested()`` stand-in recording how it exited."""

    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        self._db.savepoints += 1
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        if exc_type is not None:
            self._db.rolled_back += 1
        return False


def _fake_attachment_row(filename="report.pdf", content_type="application/pdf", source="agent"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        filename=filename,
        content_type=content_type,
        size=1234,
        source=source,
    )


def test_platform_attachments_from_metadata_parses_list():
    meta = (
        '{"attachments": [{"filename": "a.pdf", "type": "pdf", "size": 1, '
        '"file_id": "abc", "content_type": "application/pdf", '
        '"file_tag": "download_file"}]}'
    )
    out = platform_api._platform_attachments_from_metadata(meta)
    assert out is not None
    assert out[0]["file_id"] == "abc"
    # Never leak an absolute XTM One download URL — the proxy builds it.
    assert "download_url" not in out[0]


@pytest.mark.parametrize(
    "raw",
    [None, "", "not json", "{}", '{"attachments": []}', '{"attachments": "x"}'],
)
def test_platform_attachments_from_metadata_handles_missing_or_malformed(raw):
    assert platform_api._platform_attachments_from_metadata(raw) is None


@pytest.mark.asyncio
async def test_collect_generated_attachments_diffs_context():
    generated = _fake_attachment_row()
    generated_id = str(generated.id)

    initial_ref = SimpleNamespace(file_id="initial-1")
    generated_ref = SimpleNamespace(file_id=generated_id)

    file_context = SimpleNamespace(
        list_files=lambda: [initial_ref, generated_ref]
    )

    class _DB:
        def __init__(self):
            self.execute_calls = 0

        async def execute(self, _stmt):
            self.execute_calls += 1
            return _ScalarResult([generated])

    db = _DB()
    rows, meta = await platform_api._collect_generated_attachments(
        db, file_context, {"initial-1"}, conversation_id=uuid.uuid4()
    )

    assert db.execute_calls == 1
    assert [r.id for r in rows] == [generated.id]
    assert len(meta) == 1
    assert meta[0]["file_id"] == generated_id
    assert meta[0]["type"] == "pdf"
    assert meta[0]["file_tag"] == "download_file"
    assert "download_url" not in meta[0]


@pytest.mark.asyncio
async def test_collect_generated_attachments_no_new_files_skips_query():
    """When the context has no files beyond the initial set, no DB query runs."""
    ref = SimpleNamespace(file_id="initial-1")
    file_context = SimpleNamespace(list_files=lambda: [ref])

    class _DB:
        async def execute(self, _stmt):  # pragma: no cover - must not run
            raise AssertionError("execute() must not be called when no new files")

    rows, meta = await platform_api._collect_generated_attachments(
        _DB(), file_context, {"initial-1"}, conversation_id=uuid.uuid4()
    )
    assert rows == []
    assert meta == []


@pytest.mark.asyncio
async def test_collect_generated_attachments_skips_non_uuid_ids():
    """A non-UUID file_id (e.g. an integration's in-memory external id) must be
    skipped, not crash the whole conversion and drop valid attachments."""
    generated = _fake_attachment_row()
    generated_id = str(generated.id)
    valid_ref = SimpleNamespace(file_id=generated_id)
    external_ref = SimpleNamespace(file_id="ext://not-a-uuid")
    file_context = SimpleNamespace(list_files=lambda: [valid_ref, external_ref])

    class _DB:
        def __init__(self):
            self.execute_calls = 0

        async def execute(self, _stmt):
            self.execute_calls += 1
            return _ScalarResult([generated])

    db = _DB()
    rows, meta = await platform_api._collect_generated_attachments(
        db, file_context, set(), conversation_id=uuid.uuid4()
    )

    # The valid UUID still resolves; the non-UUID id is silently dropped.
    assert db.execute_calls == 1
    assert [r.id for r in rows] == [generated.id]
    assert meta[0]["file_id"] == generated_id


@pytest.mark.asyncio
async def test_collect_generated_attachments_all_non_uuid_skips_query():
    """When every new file_id is non-UUID, no DB query runs and nothing is
    surfaced (instead of raising)."""
    external_ref = SimpleNamespace(file_id="ext://x")
    file_context = SimpleNamespace(list_files=lambda: [external_ref])

    class _DB:
        async def execute(self, _stmt):  # pragma: no cover - must not run
            raise AssertionError("execute() must not run when no UUID ids remain")

    rows, meta = await platform_api._collect_generated_attachments(
        _DB(), file_context, set(), conversation_id=uuid.uuid4()
    )
    assert rows == []
    assert meta == []


@pytest.mark.asyncio
async def test_link_generated_attachments_noop_without_rows():
    class _DB:
        async def execute(self, _stmt):  # pragma: no cover - must not run
            raise AssertionError("execute() must not be called for empty rows")

    assert (
        await platform_api._link_generated_attachments(
            _DB(), [], conversation_id=uuid.uuid4(), message_id=uuid.uuid4()
        )
        == []
    )


@pytest.mark.asyncio
async def test_link_generated_attachments_binds_conversation_and_message():
    """The bind sets BOTH columns: an MCP download is born without a
    conversation, and ``load_conversation_files`` finds a file by
    ``conversation_id`` on the next turn. The UPDATE repeats the admission
    predicate, and only the rows it matched are reported bound."""
    won, lost = _fake_attachment_row(), _fake_attachment_row()
    conv_id, msg_id = uuid.uuid4(), uuid.uuid4()

    class _DB:
        def __init__(self):
            self.statements = []
            self.savepoints = 0
            self.rolled_back = 0

        def begin_nested(self):
            return _Savepoint(self)

        async def execute(self, stmt):
            self.statements.append(stmt)
            return _ScalarResult([won.id])

    db = _DB()
    bound = await platform_api._link_generated_attachments(
        db, [won, lost], conversation_id=conv_id, message_id=msg_id
    )
    assert bound == [won]
    assert db.savepoints == 1
    (stmt,) = db.statements
    sql = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "UPDATE file_attachments" in sql
    assert "conversation_id" in sql and "message_id" in sql
    assert "message_id IS NULL" in sql
    values = {col.key for col in stmt._values}
    assert values == {"conversation_id", "message_id"}


@pytest.mark.asyncio
async def test_link_generated_attachments_failure_binds_nothing():
    """A failed bind is rolled back to its SAVEPOINT, so the reply's own commit
    still goes through (the outer transaction is never left rollback-only)."""

    class _DB:
        def __init__(self):
            self.savepoints = 0
            self.rolled_back = 0

        def begin_nested(self):
            return _Savepoint(self)

        async def execute(self, _stmt):
            raise RuntimeError("db down")

    db = _DB()
    assert (
        await platform_api._link_generated_attachments(
            db, [_fake_attachment_row()], conversation_id=uuid.uuid4(), message_id=uuid.uuid4()
        )
        == []
    )
    assert db.savepoints == 1 and db.rolled_back == 1


def test_platform_chat_goes_through_the_shared_admission_and_bind():
    """Same contract as the native chat paths: no by-id load plus an
    unconditional ``message_id`` UPDATE that could re-home a file another
    message shows, or leave an MCP download without its conversation."""
    import inspect

    source = inspect.getsource(platform_api)
    assert "load_attachment_rows(" in source
    assert "bind_attachment_rows(" in source
    assert ".values(message_id=" not in source


@pytest.mark.asyncio
async def test_send_platform_chat_message_surfaces_attachments_non_streaming(monkeypatch):
    """Non-streaming platform chat returns ``attachments`` and persists them."""
    generated = _fake_attachment_row(filename="iocs.csv", content_type="text/csv")
    generated_id = str(generated.id)

    async def _fake_resolve_platform_agent(_db, _agent_slug):
        return SimpleNamespace(id=uuid.uuid4())

    store_calls = []

    async def _fake_store_message(
        _db, _conversation_id, role, content, channel, metadata_json=None
    ):
        store_calls.append({"role": role, "metadata_json": metadata_json})
        return SimpleNamespace(id=uuid.uuid4())

    async def _fake_get_history(_db, _conversation_id):
        return []

    async def _fake_check_quota(_db, _user_id):
        return None

    async def _fake_run_agent_chat(
        _db, _user_id_str, _agent, _history, _content, conversation_id, **kwargs
    ):
        # Simulate the agent generating a file: register it in the context.
        ctx = kwargs.get("file_context")
        ctx.add(SimpleNamespace(file_id=generated_id))
        return (f"Here is your export [[FILE:{generated_id}]]", None)

    monkeypatch.setattr(
        platform_api, "_resolve_platform_agent", _fake_resolve_platform_agent
    )
    monkeypatch.setattr("app.channels.router.store_message", _fake_store_message)
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history", _fake_get_history
    )
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _fake_check_quota)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _fake_run_agent_chat)

    async def _fake_collect(_db, _ctx, _initial, **_kwargs):
        return ([generated], [
            {
                "filename": "iocs.csv",
                "type": "csv",
                "size": 1234,
                "file_id": generated_id,
                "content_type": "text/csv",
                "file_tag": "download_file",
            }
        ])

    linked = {}

    async def _fake_link(_db, rows, *, conversation_id, message_id):
        linked["rows"] = rows
        linked["conversation_id"] = conversation_id
        linked["message_id"] = message_id
        return rows

    monkeypatch.setattr(platform_api, "_collect_generated_attachments", _fake_collect)
    monkeypatch.setattr(platform_api, "_link_generated_attachments", _fake_link)

    class _DB:
        def __init__(self):
            self.added = []
            self.commit_calls = 0

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            if self.added and getattr(self.added[-1], "id", None) is None:
                self.added[-1].id = uuid.uuid4()

        async def commit(self):
            self.commit_calls += 1

    db = _DB()
    user = SimpleNamespace(id=uuid.uuid4(), email="user@example.com")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/platform/chat/messages",
            "headers": [],
            "query_string": b"",
        }
    )

    response = await platform_api.send_platform_chat_message(
        platform_api.PlatformChatRequest(
            content="export the iocs", agent_slug="agent", stream=False
        ),
        request=request,
        db=db,  # type: ignore[arg-type]
        user=user,  # type: ignore[arg-type]
    )

    assert response["attachments"][0]["file_id"] == generated_id
    assert "download_url" not in response["attachments"][0]
    # Assistant message persisted with attachment metadata + linkage.
    assistant_meta = store_calls[-1]["metadata_json"]
    assert assistant_meta is not None
    assert generated_id in assistant_meta
    assert linked["rows"] == [generated]
    assert linked["conversation_id"] is not None


def test_narrow_attachment_meta_rewrites_only_the_attachments():
    """The narrowing replaces the ``attachments`` block and keeps every other key
    of the message metadata (a ``transfer_chain``, the turn's tool trace)."""
    kept, lost = _fake_attachment_row("kept.pdf"), _fake_attachment_row("lost.pdf")
    meta = {
        "transfer_chain": [{"agent_id": "a1"}],
        "attachments": [{"file_id": str(kept.id)}, {"file_id": str(lost.id)}],
    }

    block = platform_api._narrow_attachment_meta(meta, [kept])
    assert [a["file_id"] for a in block] == [str(kept.id)]
    assert meta["attachments"] == block
    assert meta["transfer_chain"] == [{"agent_id": "a1"}]

    assert platform_api._narrow_attachment_meta(meta, []) is None
    assert meta == {"transfer_chain": [{"agent_id": "a1"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("bound_count", [1, 0])
async def test_send_platform_chat_message_non_streaming_narrows_to_the_bound_rows(
    monkeypatch, bound_count
):
    """A file another message bound meanwhile is neither on the stored message
    nor in the response: both carry exactly what the bind reported."""
    from app.services.chat_messages import attachment_meta_from_file_attachment

    rows = [_fake_attachment_row("a.csv", "text/csv"), _fake_attachment_row("b.csv", "text/csv")]
    bound = rows[:bound_count]
    stored = SimpleNamespace(id=uuid.uuid4(), metadata_json=None)

    async def _fake_resolve_platform_agent(_db, _agent_slug):
        return SimpleNamespace(id=uuid.uuid4())

    async def _fake_store_message(_db, _conversation_id, role, content, channel, metadata_json=None):
        if role != "assistant":
            return SimpleNamespace(id=uuid.uuid4())
        stored.metadata_json = metadata_json
        return stored

    async def _fake_get_history(_db, _conversation_id):
        return []

    async def _fake_check_quota(_db, _user_id):
        return None

    async def _fake_run_agent_chat(_db, _user_id_str, _agent, _history, _content, conversation_id, **_kwargs):
        return ("Here are the exports", None)

    async def _fake_collect(_db, _ctx, _initial, **_kwargs):
        return rows, [attachment_meta_from_file_attachment(fa) for fa in rows]

    async def _fake_link(_db, _rows, *, conversation_id, message_id):
        return bound

    monkeypatch.setattr(platform_api, "_resolve_platform_agent", _fake_resolve_platform_agent)
    monkeypatch.setattr("app.channels.router.store_message", _fake_store_message)
    monkeypatch.setattr("app.channels.router.get_conversation_history", _fake_get_history)
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _fake_check_quota)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _fake_run_agent_chat)
    monkeypatch.setattr(platform_api, "_collect_generated_attachments", _fake_collect)
    monkeypatch.setattr(platform_api, "_link_generated_attachments", _fake_link)

    class _DB:
        def __init__(self):
            self.added = []

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            if self.added and getattr(self.added[-1], "id", None) is None:
                self.added[-1].id = uuid.uuid4()

        async def commit(self):
            return None

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/platform/chat/messages",
            "headers": [],
            "query_string": b"",
        }
    )
    response = await platform_api.send_platform_chat_message(
        platform_api.PlatformChatRequest(content="export the iocs", agent_slug="agent", stream=False),
        request=request,
        db=_DB(),  # type: ignore[arg-type]
        user=SimpleNamespace(id=uuid.uuid4(), email="user@example.com"),  # type: ignore[arg-type]
    )

    bound_ids = [str(fa.id) for fa in bound]
    if bound:
        assert [a["file_id"] for a in response["attachments"]] == bound_ids
        assert [a["file_id"] for a in json.loads(stored.metadata_json)["attachments"]] == bound_ids
    else:
        assert "attachments" not in response
        assert stored.metadata_json is None


@pytest.mark.asyncio
async def test_send_platform_chat_message_seeds_full_conversation_file_context(
    monkeypatch,
):
    """The FileContext handed to ``run_agent_chat`` must come from
    ``load_conversation_files`` (every persisted conversation file + this
    turn's inline refs), not just the current turn.

    ``run_agent_chat`` treats a non-None ``file_context`` as authoritative and
    skips its own ``load_conversation_files`` call, so seeding from the current
    turn alone would hide previously uploaded / generated files from the agent
    on later turns — diverging from native chat. This pins the parity fix.
    """
    from app.tools.base import FileContext

    captured: dict = {}

    async def _fake_load_conversation_files(_db, conversation_id, current_turn_files):
        captured["conversation_id"] = conversation_id
        captured["current_turn_files"] = current_turn_files
        ctx = FileContext()
        # A file uploaded on an earlier turn (persisted FileAttachment row).
        ctx.add(SimpleNamespace(file_id="persisted-1"))
        return ctx

    async def _fake_resolve_platform_agent(_db, _agent_slug):
        return SimpleNamespace(id=uuid.uuid4())

    async def _fake_store_message(
        _db, _conversation_id, role, content, channel, metadata_json=None
    ):
        return SimpleNamespace(id=uuid.uuid4())

    async def _fake_get_history(_db, _conversation_id):
        return []

    async def _fake_check_quota(_db, _user_id):
        return None

    async def _fake_run_agent_chat(
        _db, _user_id_str, _agent, _history, _content, conversation_id, **kwargs
    ):
        captured["run_file_context"] = kwargs.get("file_context")
        return ("ok", None)

    async def _fake_collect(_db, _ctx, _initial, **_kwargs):
        return ([], [])

    monkeypatch.setattr(
        platform_api, "_resolve_platform_agent", _fake_resolve_platform_agent
    )
    monkeypatch.setattr("app.channels.router.store_message", _fake_store_message)
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history", _fake_get_history
    )
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _fake_check_quota)
    monkeypatch.setattr(
        "app.services.agent_chat.load_conversation_files", _fake_load_conversation_files
    )
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _fake_run_agent_chat)
    monkeypatch.setattr(platform_api, "_collect_generated_attachments", _fake_collect)

    class _DB:
        def __init__(self):
            self.added = []

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            if self.added and getattr(self.added[-1], "id", None) is None:
                self.added[-1].id = uuid.uuid4()

        async def commit(self):
            return None

    db = _DB()
    user = SimpleNamespace(id=uuid.uuid4(), email="user@example.com")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/platform/chat/messages",
            "headers": [],
            "query_string": b"",
        }
    )

    await platform_api.send_platform_chat_message(
        platform_api.PlatformChatRequest(
            content="anything new about that file?", agent_slug="agent", stream=False
        ),
        request=request,
        db=db,  # type: ignore[arg-type]
        user=user,  # type: ignore[arg-type]
    )

    # The full-conversation loader was consulted (not a current-turn-only seed).
    assert "conversation_id" in captured
    # The context that reached run_agent_chat carries the persisted file, so
    # multi-turn file access is preserved.
    ctx = captured["run_file_context"]
    assert ctx is not None
    assert any(
        getattr(f, "file_id", None) == "persisted-1" for f in ctx.list_files()
    )


@pytest.mark.asyncio
async def test_create_session_restore_is_bounded(monkeypatch):
    """Session restore must bound the message query (latest N) instead of
    selecting every message in the conversation (unbounded regression)."""
    # ``agent_id`` / ``original_agent_id`` are read to seed per-message agent
    # attribution (``original_agent_id`` is write-once, so it survives
    # handovers). None here: this conversation has no transfer history.
    conv = SimpleNamespace(
        id=uuid.uuid4(), agent_id=None, original_agent_id=None, workspace_id=None, channel="platform"
    )

    async def _fake_resolve_platform_agent(_db, _agent_slug):
        return SimpleNamespace(id=uuid.uuid4(), name="Agent")

    monkeypatch.setattr(
        platform_api, "_resolve_platform_agent", _fake_resolve_platform_agent
    )

    class _Result:
        def __init__(self, scalar=None, rows=None):
            self._scalar = scalar
            self._rows = rows or []

        def scalar_one_or_none(self):
            return self._scalar

        def scalars(self):
            return SimpleNamespace(all=lambda: self._rows)

    class _DB:
        def __init__(self):
            self.sql: list[str] = []

        async def execute(self, statement):
            self.sql.append(str(statement))
            if len(self.sql) == 1:
                return _Result(scalar=conv)  # conversation lookup
            return _Result(rows=[])  # bounded message query

    db = _DB()
    user = SimpleNamespace(id=uuid.uuid4(), email="u@example.com")

    resp = await platform_api.create_platform_chat_session(
        platform_api.PlatformChatSessionRequest(
            agent_slug="agent", conversation_id=str(conv.id)
        ),
        db=db,  # type: ignore[arg-type]
        user=user,  # type: ignore[arg-type]
    )

    assert resp.conversation_id == str(conv.id)
    # The message-restore query is bounded (LIMIT present), matching
    # get_conversation_history's latest-N semantics.
    assert len(db.sql) >= 2
    assert "limit" in db.sql[1].lower()
