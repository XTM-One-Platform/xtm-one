"""Tests for agent-generated file tooling and attachment serialization."""

from __future__ import annotations

import json
import pathlib
import re
import uuid
from types import SimpleNamespace

import pytest

from app.services.builtin_tools import build_generate_file_tool, load_custom_tools
from app.services.chat_messages import parse_attachments
from app.tools.base import FileContext, ToolCall


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalarResult(self._rows)


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.added = []

    async def execute(self, _query):
        return _FakeExecuteResult(self._rows)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


class _FakeComposite:
    def __init__(self):
        self.defs = []
        self.executor = None

    def add_tools(self, definitions, executor):
        self.defs.extend(definitions)
        self.executor = executor


class _FakeOwnSession:
    """Stand-in for the short-lived session ``_persist_generated_file`` opens.

    The helper never writes on the caller's session — it opens a
    dedicated session via ``_shared._own_session()`` and commits.  Tests patch
    that seam with this fake so no real database is needed.
    """

    def __init__(self, sink: list):
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def add(self, obj):
        self._sink.append(obj)

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_generate_file_tool_returns_file_id_and_registers_file(monkeypatch):
    created = SimpleNamespace(
        id=uuid.uuid4(),
        filename="bundle.json",
        content_type="application/json",
        size=15,
        s3_object_name="chat/conv/bundle.json",
    )

    async def _fake_persist(**_kwargs):
        return created

    monkeypatch.setattr("app.services.builtin_tools._persist_generated_file", _fake_persist)

    defs, executor = build_generate_file_tool(
        db=_FakeDB([]),
        user_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )
    assert defs and defs[0].name == "generate_file"

    ctx = FileContext()
    executor.file_context = ctx
    result = await executor.execute_tool(
        ToolCall(
            id="call-1",
            name="generate_file",
            arguments={"filename": "bundle.json", "content": '{"ok":true}'},
        )
    )

    assert result.is_error is False
    assert result.content == f"[[FILE:{created.id}]]"
    assert result.files and result.files[0].file_id == str(created.id)
    assert ctx.get(str(created.id)) is not None


@pytest.mark.asyncio
async def test_load_custom_tools_captures_output_files(monkeypatch):
    tool_id = uuid.uuid4()
    fake_tool = SimpleNamespace(
        id=tool_id,
        name="csv_tool",
        js_function="return 'ok';",
        input_schema={"type": "object", "properties": {}},
        enabled=True,
        language="javascript",
        timeout=10,
        description="CSV tool",
    )
    db = _FakeDB([fake_tool])
    composite = _FakeComposite()

    async def _fake_exec(*_args, **_kwargs):
        return json.dumps(
            {
                "result": "done",
                "$output_files": [
                    {
                        "filename": "export.csv",
                        "content": "a,b\n1,2",
                        "content_type": "text/csv",
                    }
                ],
            }
        )

    created = SimpleNamespace(
        id=uuid.uuid4(),
        filename="export.csv",
        content_type="text/csv",
        size=7,
        s3_object_name="chat/conv/export.csv",
    )

    async def _fake_persist(**_kwargs):
        return created

    monkeypatch.setattr("app.services.builtin_tools.execute_tool_auto", _fake_exec, raising=False)
    monkeypatch.setattr("app.services.custom_tool_executor.execute_tool_auto", _fake_exec)
    monkeypatch.setattr("app.services.builtin_tools._persist_generated_file", _fake_persist)

    await load_custom_tools(
        db,
        [tool_id],
        composite,
        user_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )
    assert composite.executor is not None

    ctx = FileContext()
    composite.executor.file_context = ctx
    result = await composite.executor.execute_tool(
        ToolCall(id="call-2", name="csv_tool", arguments={})
    )

    assert result.is_error is False
    assert result.content == "done"
    assert result.files and result.files[0].filename == "export.csv"
    assert ctx.get(str(created.id)) is not None


# ──────────────────────────────────────────────────────────────────────────
# Delegation / handover run file linkage
#
# Delegation / handover runs (sub-agent tasks, consult, transfer) run through
# ``run_agent_chat``, which used to build ``generate_file`` + custom tools
# without an ``assignment_run_id``. Files produced there were persisted with
# both ``conversation_id`` and ``assignment_run_id`` NULL (orphaned for
# sub-agent / consult, which have no conversation), so they never surfaced on
# the Delegation Run detail "Generated Files" card. The fix threads
# ``parent_run_id`` into both tool builders as ``assignment_run_id``. These
# pin (1) that each builder stamps the run id on the persisted file, and
# (2) that ``run_agent_chat`` actually wires ``parent_run_id`` into both.
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_file_tool_stamps_assignment_run_id(monkeypatch):
    """When ``build_generate_file_tool`` is built with an ``assignment_run_id``
    (a delegation / handover run, where ``conversation_id`` may be None), the
    persisted FileAttachment must carry that run id so it surfaces on the run
    detail page's attachments query (``FileAttachment.assignment_run_id ==
    run.id``) instead of being orphaned."""
    captured: dict[str, object] = {}
    created = SimpleNamespace(
        id=uuid.uuid4(),
        filename="report.csv",
        content_type="text/csv",
        size=7,
        s3_object_name="assignments/run/report.csv",
    )

    async def _fake_persist(**kwargs):
        captured.update(kwargs)
        return created

    monkeypatch.setattr(
        "app.services.builtin_tools._persist_generated_file", _fake_persist
    )

    run_id = str(uuid.uuid4())
    _defs, executor = build_generate_file_tool(
        db=_FakeDB([]),
        user_id=str(uuid.uuid4()),
        conversation_id=None,  # sub-agent / consult: no conversation
        assignment_run_id=run_id,
    )
    executor.file_context = FileContext()
    result = await executor.execute_tool(
        ToolCall(
            id="call-run-1",
            name="generate_file",
            arguments={"filename": "report.csv", "content": "a,b\n1,2"},
        )
    )

    assert result.is_error is False
    assert captured["assignment_run_id"] == run_id, (
        "generate_file must persist the delegation run id so the file is "
        "downloadable from the run detail page"
    )
    assert captured["conversation_id"] is None


@pytest.mark.asyncio
async def test_load_custom_tools_stamps_assignment_run_id_on_output_files(monkeypatch):
    """Custom-tool ``$output_files`` produced during a delegation / handover run
    must likewise be persisted with the run's ``assignment_run_id`` so they are
    downloadable from the run detail page."""
    tool_id = uuid.uuid4()
    fake_tool = SimpleNamespace(
        id=tool_id,
        name="csv_tool",
        js_function="return 'ok';",
        input_schema={"type": "object", "properties": {}},
        enabled=True,
        language="javascript",
        timeout=10,
        description="CSV tool",
    )
    db = _FakeDB([fake_tool])
    composite = _FakeComposite()

    async def _fake_exec(*_args, **_kwargs):
        return json.dumps(
            {
                "result": "done",
                "$output_files": [
                    {
                        "filename": "export.csv",
                        "content": "a,b\n1,2",
                        "content_type": "text/csv",
                    }
                ],
            }
        )

    captured: dict[str, object] = {}

    async def _fake_persist(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename="export.csv",
            content_type="text/csv",
            size=7,
            s3_object_name="assignments/run/export.csv",
        )

    monkeypatch.setattr(
        "app.services.builtin_tools.execute_tool_auto", _fake_exec, raising=False
    )
    monkeypatch.setattr("app.services.custom_tool_executor.execute_tool_auto", _fake_exec)
    monkeypatch.setattr(
        "app.services.builtin_tools._persist_generated_file", _fake_persist
    )

    run_id = str(uuid.uuid4())
    await load_custom_tools(
        db,
        [tool_id],
        composite,
        user_id=str(uuid.uuid4()),
        conversation_id=None,  # sub-agent / consult: no conversation
        assignment_run_id=run_id,
    )
    assert composite.executor is not None

    composite.executor.file_context = FileContext()
    result = await composite.executor.execute_tool(
        ToolCall(id="call-run-2", name="csv_tool", arguments={})
    )

    assert result.is_error is False
    assert captured["assignment_run_id"] == run_id, (
        "custom-tool $output_files must persist the delegation run id so the "
        "file is downloadable from the run detail page"
    )


def test_run_agent_chat_links_generated_files_to_parent_run():
    """Guard for the actual fix: ``run_agent_chat`` MUST thread ``parent_run_id``
    into BOTH ``build_generate_file_tool`` and the custom-tool loader as the
    ``assignment_run_id`` keyword. A regression that drops either wiring
    re-orphans files generated during a delegation / handover run (they would
    persist with both ``conversation_id`` and ``assignment_run_id`` NULL and
    never reach the run detail attachments).

    Custom tools are loaded through ``load_custom_and_mcp_tools``
    (parallel split-session loading, P3.5); the facade
    forwards ``assignment_run_id`` verbatim to ``load_custom_tools``, so the
    keyword contract is asserted on the facade call.  The legacy direct
    ``load_custom_tools`` call is also accepted so a future revert of the
    parallel loader cannot silently drop the wiring.

    Uses an AST check (not a raw substring match) so it is robust to
    line-wrapping, spacing, and unrelated local renames - it asserts the actual
    ``assignment_run_id=parent_run_id`` call keyword regardless of formatting.
    """
    import ast
    import inspect
    import textwrap

    from app.services import agent_chat

    tree = ast.parse(textwrap.dedent(inspect.getsource(agent_chat.run_agent_chat)))

    custom_loader_names = {"load_custom_tools", "load_custom_and_mcp_tools"}
    targets = {"build_generate_file_tool", *custom_loader_names}
    wired: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in targets:
            continue
        for kw in node.keywords:
            if (
                kw.arg == "assignment_run_id"
                and isinstance(kw.value, ast.Name)
                and kw.value.id == "parent_run_id"
            ):
                wired.add(name)

    assert "build_generate_file_tool" in wired and (
        wired & custom_loader_names
    ), (
        "run_agent_chat must call build_generate_file_tool AND the custom-tool "
        "loader (load_custom_and_mcp_tools or load_custom_tools) with "
        f"assignment_run_id=parent_run_id; wired={sorted(wired)}"
    )


def test_parse_attachments_uses_message_scoped_download_url():
    msg_meta = json.dumps(
        {
            "attachments": [
                {"filename": "bundle.json", "type": "json", "size": 12, "file_id": "f-1"}
            ]
        }
    )
    parsed = parse_attachments(
        msg_meta,
        conversation_id="conv-1",
        message_id="msg-1",
    )
    assert parsed is not None
    assert parsed[0]["download_url"] == (
        "/api/v1/chat/conversations/conv-1/messages/msg-1/files/f-1/download"
    )


# ──────────────────────────────────────────────────────────────────────────
# Fixes:
#
#  * ``attachment_meta_from_file_attachment`` extension fallback used to
#    default to ``"txt"`` whenever the filename had no dot — meaning a
#    file persisted via ``generate_file`` without an extension (a PDF
#    via ``content_type='application/pdf'`` with the filename "report",
#    for example) would render with a ``TXT`` label in the chip. The
#    new fallback uses the ``content_type`` subtype so the chip never
#    mislabels a real PDF / JSON / CSV.
#  * ``_persist_generated_file`` now uses the *canonical* string form
#    of the parsed UUID for the S3 prefix (``str(conv_uuid)``) so two
#    callers passing semantically-equal identifiers end up with the
#    same object-name prefix — uppercase / mixed-case / whitespace
#    used to all generate distinct prefixes.
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename,content_type,expected_type",
    [
        # Filename has an extension — use it (lowercased).
        ("report.PDF", "application/pdf", "pdf"),
        ("export.csv", "text/csv", "csv"),
        ("bundle.json", "application/json", "json"),
        # Filename has NO extension — derive from content_type subtype.
        ("report", "application/pdf", "pdf"),
        ("export", "text/csv", "csv"),
        ("rules", "application/yaml", "yaml"),
        ("data", "application/x-ndjson", "x-ndjson"),
        # ``application/vnd.ms-excel`` → drop the ``vnd.`` prefix so the
        # chip surfaces a usable label rather than the full vendor path.
        ("workbook", "application/vnd.ms-excel", "ms-excel"),
        # Multi-dotted OOXML vendor subtype → take the LAST dotted segment
        # (``…spreadsheetml.sheet`` → ``sheet``) rather than the unwieldy
        # full subtype. Pins the behaviour documented in the helper's
        # vendor-subtype comment for extension-less ``.xlsx`` payloads.
        (
            "spreadsheet",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "sheet",
        ),
        # ``image/svg+xml`` → drop the ``+xml`` suffix.
        ("logo", "image/svg+xml", "svg"),
        # ``application/octet-stream`` is the DB default (the column is
        # NOT NULL and seeded with this value), so an "I have no idea"
        # file lands here as ``octet-stream`` rather than ``txt``. The
        # ``txt`` last-ditch fallback only fires when the content_type
        # is genuinely malformed (e.g. empty subtype after the ``/``) —
        # see the next parametrised case below.
        ("anonymous", "application/octet-stream", "octet-stream"),
        # Malformed content-type (no subtype) → last-ditch ``txt``.
        ("malformed", "application/", "txt"),
        # No content-type at all (truly missing) → last-ditch ``txt``.
        ("nada", "/", "txt"),
    ],
)
def test_attachment_meta_extension_fallback(filename, content_type, expected_type):
    """Pins the new extension-fallback logic. A regression that brings
    back the bare ``"txt"`` default for extension-less filenames would
    silently mislabel every PDF / JSON / CSV the ``generate_file`` tool
    produces without an explicit ``.ext`` suffix.
    """
    from app.services.chat_messages import attachment_meta_from_file_attachment

    fa = SimpleNamespace(
        id=uuid.uuid4(),
        filename=filename,
        # Pass the raw content_type through — the helper itself defaults
        # to ``application/octet-stream`` only when ``content_type`` is
        # falsy, so passing the literal here ensures the parametrised
        # malformed cases actually exercise the malformed branch.
        content_type=content_type,
        size=10,
        source="agent_download",
    )
    meta = attachment_meta_from_file_attachment(fa)
    assert meta["type"] == expected_type, (
        f"filename={filename!r} content_type={content_type!r} → expected "
        f"type={expected_type!r}, got {meta['type']!r}"
    )


def test_attachment_meta_carries_what_tells_two_same_name_files_apart():
    """A card shows when its file was written and a prefix of its digest, so
    two files under one name can be told apart; an older row without them
    serves the block it always did."""
    from datetime import UTC, datetime

    from app.api.v1.chat import MessageAttachment
    from app.services.chat_messages import attachment_meta_from_file_attachment

    written = datetime(2026, 10, 1, 9, 42, 7, tzinfo=UTC)
    fa = SimpleNamespace(
        id=uuid.uuid4(),
        filename="run-prompt.txt",
        content_type="text/plain",
        size=18_571,
        source="agent_download",
        created_at=written,
        sha256="ab" * 32,
    )
    meta = attachment_meta_from_file_attachment(fa)
    assert meta["created_at"] == written.isoformat()
    assert meta["sha256"] == "ab" * 32
    served = MessageAttachment.model_validate(meta)
    assert served.created_at == written.isoformat()
    assert served.sha256 == "ab" * 32

    legacy = attachment_meta_from_file_attachment(
        SimpleNamespace(id=uuid.uuid4(), filename="a.txt", content_type="text/plain", size=1, source="chat")
    )
    assert "created_at" not in legacy and "sha256" not in legacy
    assert MessageAttachment.model_validate(legacy).created_at is None


# The ONE table the frontend's ``fileTypeLabel`` is checked against too:
# the bubble a send shows until the refetch must carry the label the
# persisted row will, or the file changes shape when the turn ends.
_ATTACHMENT_TYPE_CASES = json.loads(
    (
        pathlib.Path(__file__).resolve().parents[2]
        / "frontend"
        / "tests_e2e"
        / "fixtures"
        / "attachment-type-cases.json"
    ).read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("case", _ATTACHMENT_TYPE_CASES, ids=[case["name"] for case in _ATTACHMENT_TYPE_CASES])
def test_attachment_type_label(case):
    """Pins ``_attachment_type_label`` (user-upload metadata sites):
    extension first, then the MIME subtype with structured-syntax
    suffixes stripped and vendor paths shortened, then ``file``."""
    from app.api.v1.chat import _attachment_type_label

    assert _attachment_type_label(case["filename"], case["content_type"]) == case["expected"]


def test_frontend_upload_limit_is_the_upload_endpoints():
    """The composer refuses a file over the upload limit before sending it -
    the limit it checks must be the one the upload endpoint
    enforces, or a file between the two is refused for nothing or uploaded to
    be refused."""
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES

    constants = (
        pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "constants.ts"
    ).read_text(encoding="utf-8")
    match = re.search(r"export const MAX_CHAT_UPLOAD_SIZE_MB = (\d+);", constants)
    assert match, "MAX_CHAT_UPLOAD_SIZE_MB is no longer a plain number in constants.ts"
    assert "export const MAX_CHAT_UPLOAD_SIZE_BYTES = MAX_CHAT_UPLOAD_SIZE_MB * 1024 * 1024;" in constants
    assert int(match.group(1)) * 1024 * 1024 == MAX_UPLOAD_SIZE_BYTES
    # The composer is the chat panel's, which takes the limit for each file.
    panel = (
        pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "chat-panel" / "XtmChatPanel.tsx"
    ).read_text(encoding="utf-8")
    assert "maxFileSize={MAX_CHAT_UPLOAD_SIZE_BYTES}" in panel


@pytest.mark.asyncio
async def test_persist_generated_file_canonicalises_s3_prefix(monkeypatch):
    """The S3 prefix used to be built from the *raw* caller-provided
    ``conversation_id`` string, so two callers passing semantically-equal
    identifiers (uppercase, leading whitespace, ``urn:uuid:`` prefix)
    ended up with distinct prefixes and the bucket listing fell out of
    grep range. The fix passes the parsed UUID through ``str(...)`` so
    every prefix is the canonical lowercase form.
    """
    from app.services import builtin_tools
    from app.services import file_storage as fs_module

    captured_prefix: dict[str, str] = {}

    async def _fake_upload(*, data, filename, content_type, prefix):
        captured_prefix["value"] = prefix
        return f"{prefix}/abc123"

    # Patch the ``upload`` method on the singleton (instance attribute,
    # NOT class — the latter is read-only on the bound method descriptor)
    # so the closure-scoped import of ``file_storage`` inside
    # ``_persist_generated_file`` resolves to our fake.
    monkeypatch.setattr(
        fs_module.file_storage, "upload", _fake_upload, raising=False
    )

    # Uppercase UUID with mixed casing — ``safe_uuid`` parses it,
    # then ``str(uuid)`` normalises it to lowercase. ``uuid.UUID``
    # does NOT accept leading whitespace, so we test the casing-only
    # canonicalisation here (the whitespace case would return None
    # from ``safe_uuid`` and fall through to the ``generated`` prefix).
    raw_conv_id = "6FA459EA-EE8A-3CA4-894E-DB77E160355E"
    canonical = "6fa459ea-ee8a-3ca4-894e-db77e160355e"

    from app.services.builtin_tools import _shared

    added: list = []
    monkeypatch.setattr(_shared, "_own_session", lambda: _FakeOwnSession(added))

    await builtin_tools._persist_generated_file(
        db=None,
        user_id=str(uuid.uuid4()),
        filename="report.csv",
        raw_bytes=b"a,b\n1,2",
        content_type="text/csv",
        conversation_id=raw_conv_id,
        assignment_run_id=None,
    )

    assert captured_prefix["value"] == f"chat/{canonical}", (
        f"S3 prefix must use canonical UUID; got {captured_prefix['value']!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_content_type,expected",
    [
        ("Image/PNG", "image/png"),
        ("image/png; charset=binary", "image/png"),
        ("  Text/Plain; charset=utf-8  ", "text/plain"),
        ("application/pdf", "application/pdf"),
    ],
)
async def test_persist_generated_file_normalizes_content_type(
    monkeypatch, raw_content_type, expected
):
    """``_persist_generated_file`` is the single persistence point for agent
    artifacts (``build_generate_file_tool`` + custom-tool ``$output_files``).
    A caller (notably ``$output_files``, which forwards the tool-provided MIME
    verbatim) can hand it mixed-case or parameterized values. Normalising to a
    lowercase base MIME type here keeps the stored value consistent for the
    downstream case/parameter-sensitive checks (``startswith("image/")`` for
    inline-vs-download, chip label/icon, and the ``file_tag`` mapping). Pins
    both the value sent to S3 and the value persisted on the row.
    """
    from app.services import builtin_tools
    from app.services import file_storage as fs_module

    captured: dict[str, str] = {}

    async def _fake_upload(*, data, filename, content_type, prefix):
        captured["content_type"] = content_type
        return f"{prefix}/abc123"

    monkeypatch.setattr(
        fs_module.file_storage, "upload", _fake_upload, raising=False
    )

    from app.services.builtin_tools import _shared

    added: list = []
    monkeypatch.setattr(_shared, "_own_session", lambda: _FakeOwnSession(added))

    result = await builtin_tools._persist_generated_file(
        db=None,
        user_id=str(uuid.uuid4()),
        filename="artifact.bin",
        raw_bytes=b"data",
        content_type=raw_content_type,
        conversation_id=None,
        assignment_run_id=None,
    )

    assert captured["content_type"] == expected
    assert result.content_type == expected
    # Persistence must go through the dedicated short-lived session.
    assert added and added[0] is result


# ──────────────────────────────────────────────────────────────────────────
# PDF rendering via Playwright
#
# The ``generate_file`` tool used to fall through to a UTF-8 byte-encode
# whenever the caller passed text content — so an agent asked for a PDF
# would only get raw HTML / Markdown bytes saved with ``.pdf`` suffix.
# The new ``application/pdf`` short-circuit invokes Playwright to render
# the supplied HTML / Markdown to a real PDF. These tests pin both the
# happy path (Playwright is invoked with the wrapped HTML) and the
# error path (Playwright failure surfaces as a clean ToolResult error
# instead of a crash).
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_file_renders_pdf_via_playwright(monkeypatch):
    """When ``content_type='application/pdf'`` and ``encoding`` is not
    base64, the tool MUST route the text content through
    ``_render_text_to_pdf`` (Playwright) instead of just UTF-8-encoding
    it. Pin the call shape so a regression that reverts to byte-encoding
    HTML as ``.pdf`` trips immediately.
    """
    from app.services import builtin_tools

    pdf_calls: list[dict[str, object]] = []

    async def _fake_render(content: str, *, title: str | None = None) -> bytes:
        pdf_calls.append({"content": content, "title": title})
        return b"%PDF-1.4\n%fake-pdf-bytes\n%%EOF"

    monkeypatch.setattr(builtin_tools, "_render_text_to_pdf", _fake_render)

    created = SimpleNamespace(
        id=uuid.uuid4(),
        filename="report.pdf",
        content_type="application/pdf",
        size=32,
        s3_object_name="chat/conv/report.pdf",
    )

    async def _fake_persist(**_kwargs):
        # The PDF rendering path MUST have replaced the raw HTML content
        # with the rendered PDF bytes before reaching _persist.
        assert _kwargs["raw_bytes"].startswith(b"%PDF-"), (
            "raw_bytes must be the rendered PDF, not the source HTML — got "
            f"{_kwargs['raw_bytes'][:32]!r}"
        )
        assert _kwargs["content_type"] == "application/pdf"
        return created

    monkeypatch.setattr(builtin_tools, "_persist_generated_file", _fake_persist)

    defs, executor = builtin_tools.build_generate_file_tool(
        db=_FakeDB([]),
        user_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )
    assert defs

    ctx = FileContext()
    executor.file_context = ctx
    result = await executor.execute_tool(
        ToolCall(
            id="call-pdf-1",
            name="generate_file",
            arguments={
                "filename": "report.pdf",
                "content": "# OpenCTI report\n\nTop threats: …",
                "content_type": "application/pdf",
                "file_tag": "download_file",
            },
        )
    )

    assert result.is_error is False
    assert pdf_calls, "Playwright renderer must be invoked for PDF output"
    assert "OpenCTI report" in str(pdf_calls[0]["content"]), (
        f"Renderer must receive the original Markdown/HTML content; got: "
        f"{pdf_calls[0]['content']!r}"
    )
    # Title is the filename stem so the rendered HTML <title> isn't empty.
    assert pdf_calls[0]["title"] == "report", pdf_calls[0]["title"]


@pytest.mark.asyncio
async def test_generate_file_html_stored_verbatim(monkeypatch):
    """Generating a ``.html`` file MUST store the agent's HTML exactly as
    authored — agents already produce excellent standalone HTML, so the
    content must NOT be routed through the PDF/Markdown renderer (which is
    reserved for ``application/pdf``). Pin this so the Markdown-rendering
    PDF enhancement can never start mangling HTML files.
    """
    from app.services import builtin_tools, pdf_render

    async def _fail_render(*_args, **_kwargs):
        raise AssertionError(
            "_render_text_to_pdf must NOT be called for HTML file generation"
        )

    def _fail_markdown(*_args, **_kwargs):
        raise AssertionError(
            "render_markdown_to_html must NOT be called for HTML file generation"
        )

    monkeypatch.setattr(builtin_tools, "_render_text_to_pdf", _fail_render)
    monkeypatch.setattr(pdf_render, "render_markdown_to_html", _fail_markdown)

    html_content = "<!DOCTYPE html><html><body><h1>Report</h1><p>Body</p></body></html>"
    persisted: dict[str, object] = {}

    async def _fake_persist(**_kwargs):
        persisted.update(_kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=_kwargs["filename"],
            content_type=_kwargs["content_type"],
            size=len(_kwargs["raw_bytes"]),
            s3_object_name=f"chat/conv/{_kwargs['filename']}",
        )

    monkeypatch.setattr(builtin_tools, "_persist_generated_file", _fake_persist)

    defs, executor = builtin_tools.build_generate_file_tool(
        db=_FakeDB([]),
        user_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )
    assert defs

    ctx = FileContext()
    executor.file_context = ctx
    result = await executor.execute_tool(
        ToolCall(
            id="call-html-1",
            name="generate_file",
            arguments={
                "filename": "page.html",
                "content": html_content,
                "content_type": "text/html",
                "file_tag": "download_file",
            },
        )
    )

    assert result.is_error is False
    # The persisted bytes must be the raw HTML, byte-for-byte.
    assert persisted["raw_bytes"] == html_content.encode("utf-8"), (
        f"HTML must be stored verbatim; got {persisted['raw_bytes'][:64]!r}"
    )
    assert persisted["content_type"] == "text/html"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_content_type",
    [
        "Application/PDF",
        "application/pdf; charset=utf-8",
        "  APPLICATION/PDF  ",
        "application/pdf;charset=UTF-8",
    ],
)
async def test_generate_file_normalizes_pdf_content_type(monkeypatch, raw_content_type):
    """A non-canonical PDF content type — mixed case or carrying a
    ``; charset=…`` parameter — MUST still route through the Playwright
    renderer and be stored as the normalized base ``application/pdf``. The
    raw ``== "application/pdf"`` check only ``.strip()``'d the value, so
    ``Application/PDF`` / ``application/pdf; charset=utf-8`` silently bypassed
    the renderer and persisted UTF-8-encoded Markdown bytes under a ``.pdf``
    name. Pin the normalization so that regression trips immediately.
    """
    from app.services import builtin_tools

    pdf_calls: list[str] = []

    async def _fake_render(content: str, *, title: str | None = None) -> bytes:
        pdf_calls.append(content)
        return b"%PDF-1.4\n%fake\n%%EOF"

    monkeypatch.setattr(builtin_tools, "_render_text_to_pdf", _fake_render)

    persisted: dict[str, object] = {}

    async def _fake_persist(**_kwargs):
        persisted.update(_kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=_kwargs["filename"],
            content_type=_kwargs["content_type"],
            size=len(_kwargs["raw_bytes"]),
            s3_object_name="chat/conv/report.pdf",
        )

    monkeypatch.setattr(builtin_tools, "_persist_generated_file", _fake_persist)

    _defs, executor = builtin_tools.build_generate_file_tool(
        db=_FakeDB([]),
        user_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )
    executor.file_context = FileContext()
    result = await executor.execute_tool(
        ToolCall(
            id="call-pdf-norm",
            name="generate_file",
            arguments={
                "filename": "report.pdf",
                "content": "# Report\n\nbody",
                "content_type": raw_content_type,
                "file_tag": "download_file",
            },
        )
    )

    assert result.is_error is False
    assert pdf_calls, (
        f"content_type={raw_content_type!r} must route to the PDF renderer, "
        "not be byte-encoded as text"
    )
    assert persisted["raw_bytes"].startswith(b"%PDF-")
    assert persisted["content_type"] == "application/pdf", (
        "stored content_type must be the normalized base MIME; got "
        f"{persisted['content_type']!r}"
    )


@pytest.mark.asyncio
async def test_generate_file_pdf_render_failure_returns_friendly_error(monkeypatch):
    """If Playwright is missing / fails / times out, the tool MUST
    surface a clean ``ToolResult(is_error=True)`` so the agent can
    fall back to plain HTML/Markdown — not crash the whole tool call
    with a raw ImportError / RuntimeError that leaks the missing
    Chromium path / stack trace into the chat transcript.
    """
    from app.services import builtin_tools

    async def _fake_render_raises(_content: str, **_kwargs) -> bytes:
        raise RuntimeError("playwright._impl._errors.Error: Executable doesn't exist")

    monkeypatch.setattr(builtin_tools, "_render_text_to_pdf", _fake_render_raises)

    # ``_persist_generated_file`` must NEVER be called on the error
    # path — we don't want an empty / zero-byte FileAttachment row
    # written to the DB and S3 when the render failed.
    persist_called = {"v": False}

    async def _fake_persist(**_kwargs):
        persist_called["v"] = True
        return SimpleNamespace(id=uuid.uuid4(), filename="x", content_type="x", size=0)

    monkeypatch.setattr(builtin_tools, "_persist_generated_file", _fake_persist)

    defs, executor = builtin_tools.build_generate_file_tool(
        db=_FakeDB([]),
        user_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )
    assert defs

    executor.file_context = FileContext()
    result = await executor.execute_tool(
        ToolCall(
            id="call-pdf-err",
            name="generate_file",
            arguments={
                "filename": "report.pdf",
                "content": "<html><body>hi</body></html>",
                "content_type": "application/pdf",
            },
        )
    )

    assert result.is_error is True
    assert "PDF rendering failed" in result.content
    assert persist_called["v"] is False, (
        "Failed PDF render must NOT persist a zero-byte FileAttachment row"
    )


@pytest.mark.asyncio
async def test_generate_file_pdf_base64_path_bypasses_renderer(monkeypatch):
    """When the caller explicitly supplies pre-rendered PDF bytes via
    ``encoding='base64'``, the renderer MUST NOT run — those bytes
    are the source of truth and the LLM has paid the token cost to
    emit them. Re-rendering them as HTML through Playwright would
    produce a "PDF of base64 text" which is exactly what nobody
    wants.
    """
    import base64 as _b64

    from app.services import builtin_tools

    render_called = {"v": False}

    async def _fake_render(_content: str, **_kwargs) -> bytes:
        render_called["v"] = True
        return b"should-not-be-called"

    monkeypatch.setattr(builtin_tools, "_render_text_to_pdf", _fake_render)

    pdf_bytes = b"%PDF-1.4\n%real-pdf-bytes-from-llm\n%%EOF"

    async def _fake_persist(**_kwargs):
        # The base64 path must pass the DECODED bytes straight through.
        assert _kwargs["raw_bytes"] == pdf_bytes
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=_kwargs["filename"],
            content_type=_kwargs["content_type"],
            size=len(pdf_bytes),
            s3_object_name="chat/conv/report.pdf",
        )

    monkeypatch.setattr(builtin_tools, "_persist_generated_file", _fake_persist)

    _defs, executor = builtin_tools.build_generate_file_tool(
        db=_FakeDB([]),
        user_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )

    executor.file_context = FileContext()
    result = await executor.execute_tool(
        ToolCall(
            id="call-pdf-b64",
            name="generate_file",
            arguments={
                "filename": "report.pdf",
                "content": _b64.b64encode(pdf_bytes).decode("ascii"),
                "content_type": "application/pdf",
                "encoding": "base64",
            },
        )
    )

    assert result.is_error is False
    assert render_called["v"] is False, (
        "base64-encoded PDF input MUST bypass the Playwright renderer"
    )


@pytest.mark.parametrize(
    "html_input,should_wrap",
    [
        ("<!DOCTYPE html><html><body>x</body></html>", False),
        ("<html><body>x</body></html>", False),
        ("<body>raw</body>", False),
        ("<article>section</article>", False),
        ("Plain text content", True),
        ("# Markdown header\n\nParagraph.", True),
        ("", True),  # empty defaults to wrapping
    ],
)
def test_wrap_content_as_html_detects_existing_html(html_input, should_wrap):
    """``looks_like_html`` decides how the *body* is built, not whether the
    outer printable shell is added.

    ``wrap_content_as_html`` ALWAYS injects the outer ``<!DOCTYPE html>`` shell
    (charset, ``<title>``, ``PDF_DEFAULT_STYLESHEET``) so every rendered PDF is
    UTF-8 and styled. The sniff only controls the body:

    * already-HTML input (``should_wrap`` False) is embedded **verbatim** — not
      Markdown-rendered, not ``<pre>``-escaped — even though that nests a full
      ``<html>`` document inside the shell. This nesting is intentional and
      pinned: Chromium tolerates the redundant structural tags (see the
      ``wrap_content_as_html`` review thread), and wrapping-always guarantees
      the charset/title/print CSS that bare agent-authored documents omit.
    * plain text / Markdown (``should_wrap`` True) is run through the
      Markdown→HTML renderer so the PDF is a formatted document rather than a
      wall of literal ``#`` / ``|`` characters.
    """
    from app.services.pdf_render import looks_like_html, wrap_content_as_html

    detected = looks_like_html(html_input)
    assert detected is (not should_wrap)

    wrapped = wrap_content_as_html(html_input, title="t")
    # The wrapper always carries our charset declaration so the renderer
    # picks up UTF-8 regardless of the source.
    assert "charset=\"utf-8\"" in wrapped
    assert "<title>t</title>" in wrapped
    if not should_wrap:
        # Real HTML is embedded verbatim — never Markdown-rendered, never
        # escaped into a <pre> block.
        assert html_input in wrapped
    # Markdown / plain text is rendered to HTML. The ``<pre>`` plaintext
    # block is now only the last-resort fallback when Markdown rendering
    # raises, so it must not appear for these well-formed inputs.
    assert "<pre class=\"rendered-plaintext\">" not in wrapped


def test_wrap_content_renders_markdown_to_html():
    """Markdown content is rendered to real HTML elements (headings,
    tables, lists) so the generated PDF is a formatted document instead
    of a wall of literal '#' and '|' characters."""
    from app.services.pdf_render import wrap_content_as_html

    md = (
        "# Report\n\n"
        "| Field | Value |\n"
        "|-------|-------|\n"
        "| Author | ACME |\n\n"
        "- one\n"
        "- two\n"
    )
    wrapped = wrap_content_as_html(md, title="t")

    assert "<h1>Report</h1>" in wrapped
    assert "<table>" in wrapped
    assert "<th>Field</th>" in wrapped
    assert "<li>one</li>" in wrapped
    # The raw Markdown table/heading control characters must NOT survive as
    # visible text — that was the "ugly snapshot of Markdown" bug.
    assert "| Field | Value |" not in wrapped
    assert "# Report" not in wrapped


# ──────────────────────────────────────────────────────────────────────────
# MIME_TYPES coverage — agent artifact extensions
#
# The CTI / CTEM agents commonly produce YARA rules, Sigma rules, STIX
# bundles, YAML configs, NDJSON exports, etc. The legacy ``MIME_TYPES``
# dict only knew about office-doc-style extensions; the missing types
# silently fell through to ``application/octet-stream`` which forced
# the browser into a "save as binary" mode for files that are really
# just text. These tests pin the new coverage.
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename,expected_mime",
    [
        # Security / threat intel
        ("rules.yar", "text/plain"),
        ("detection.yara", "text/plain"),
        ("attack.sigma", "application/yaml"),
        ("bundle.stix", "application/json"),
        ("alerts.snort", "text/plain"),
        # YAML / TOML / NDJSON
        ("config.yaml", "application/yaml"),
        ("config.yml", "application/yaml"),
        ("settings.toml", "application/toml"),
        ("events.ndjson", "application/x-ndjson"),
        ("events.jsonl", "application/x-ndjson"),
        # Code / scripts
        ("analyzer.py", "text/x-python"),
        ("query.sql", "application/sql"),
        # Archives
        ("export.tar.gz", "application/gzip"),
        ("export.zip", "application/zip"),
    ],
)
def test_guess_content_type_covers_agent_artifacts(filename, expected_mime):
    """Every extension the agent-generated file tool is likely to emit
    must map to a meaningful Content-Type — without this, the browser
    saves the file as ``application/octet-stream`` and forces a
    download-then-rename UX even for plain-text rule files that could
    be viewed inline."""
    from app.services.file_storage import guess_content_type

    assert guess_content_type(filename) == expected_mime, (
        f"{filename!r} → expected {expected_mime!r}, "
        f"got {guess_content_type(filename)!r}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Download-file authorization tests
#
# The legacy ``/api/v1/chat/files/{file_id}/download`` endpoint used to
# treat ``file_att.user_id IS NULL`` as "downloadable by any authenticated
# user". That made every agent-generated file inside a company-managed
# assignment run readable by everyone with a valid session, even though
# the run itself is gated to admins / run owners. The endpoint now layers
# conversation-ownership and ``require_run_access`` checks on top of the
# NULL-user fast-path so legitimate scope owners keep working but the
# "NULL means everyone" hole is closed. These tests pin both directions
# of the contract.
# ──────────────────────────────────────────────────────────────────────────


class _DownloadStubResult:
    """Result wrapper for ``download_file``'s ``select(FileAttachment)``
    and follow-up scope-check queries — returns whatever stub was queued.

    ``scalars().all()`` serves the list-shaped probes the contract ends
    with (the requester's groups, the readable decks naming the file):
    an exhausted queue reads as "no rows", so a request nothing
    admits still falls through to the 403.
    """

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return [] if self._value is None else [self._value]


class _DownloadStubDB:
    """Pop-style fake session: each ``execute`` consumes the next stub.

    Mirrors the call order ``download_file`` issues:

    1. ``select(FileAttachment).where(id == ...)``
    2. (NULL-user path) ``select(Conversation.id).where(...)``
    3. (NULL-user path) ``select(AssignmentRun).where(...)``
    4. (still refused) the group lookup, then the readable-deck probe —
       both empty once the queue is exhausted.
    """

    def __init__(self, results):
        self._queue = list(results)

    async def execute(self, _statement):
        if not self._queue:
            return _DownloadStubResult(None)
        return _DownloadStubResult(self._queue.pop(0))

    async def commit(self) -> None:
        """The route commits the file's last use (retention, #396) before answering."""


@pytest.mark.asyncio
async def test_download_file_owner_succeeds(monkeypatch):
    """Sanity check: the owning user can still download their own file."""
    from app.api.v1 import chat as chat_api

    user_uuid = uuid.uuid4()
    file_att = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_uuid,
        conversation_id=None,
        assignment_run_id=None,
        filename="hello.txt",
        content_type="text/plain",
        size=5,
        s3_object_name="key/hello.txt",
    )
    db = _DownloadStubDB([file_att])
    user = SimpleNamespace(id=user_uuid, role="user")

    from app.services import file_storage as file_storage_module

    async def _fake_download(_key):
        return b"hello"

    monkeypatch.setattr(file_storage_module.file_storage, "download", _fake_download)

    response = await chat_api.download_file(str(file_att.id), user=user, db=db)
    assert response.body == b"hello"


@pytest.mark.asyncio
async def test_download_file_rejects_null_user_without_any_scope_binding():
    """Regression: a file with ``user_id IS NULL`` and no
    ``conversation_id`` / ``assignment_run_id`` binding (orphan from a
    cascading FK SET NULL after the conversation / run was deleted)
    must NOT be downloadable. The previous shape granted access to any
    authenticated user — this assertion locks that hole down for good.
    """
    from fastapi import HTTPException

    from app.api.v1 import chat as chat_api

    file_att = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        conversation_id=None,
        assignment_run_id=None,
        filename="orphan.txt",
        content_type="text/plain",
        size=4,
        s3_object_name="key/orphan.txt",
    )
    db = _DownloadStubDB([file_att])
    user = SimpleNamespace(id=uuid.uuid4(), role="user")

    with pytest.raises(HTTPException) as exc_info:
        await chat_api.download_file(str(file_att.id), user=user, db=db)
    # Refused like an unknown id: never a 403 that confirms it exists.
    assert (exc_info.value.status_code, exc_info.value.detail) == (404, "File not found")


@pytest.mark.asyncio
async def test_download_file_grants_null_user_conversation_scope(monkeypatch):
    """When ``user_id`` is NULL but the file is bound to a conversation
    owned by the requester, the download succeeds. This covers the
    anonymous-chat path where the user_id can't be set but the
    conversation binding is."""
    from app.api.v1 import chat as chat_api

    user_uuid = uuid.uuid4()
    conv_uuid = uuid.uuid4()
    file_att = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        conversation_id=conv_uuid,
        assignment_run_id=None,
        filename="anon.txt",
        content_type="text/plain",
        size=4,
        s3_object_name="key/anon.txt",
    )
    # Second execute returns the conversation *row*: the scope
    # check is ``conversation_access(...)``, which needs ``user_id`` and
    # ``link_access`` to tell an owner from a shared viewer from a stranger —
    # it can no longer be satisfied by a bare truthy id.
    conv_row = SimpleNamespace(
        id=conv_uuid, user_id=user_uuid, link_access="private"
    )
    db = _DownloadStubDB([file_att, conv_row])
    user = SimpleNamespace(id=user_uuid, role="user")

    from app.services import file_storage as file_storage_module

    async def _fake_download(_key):
        return b"anon"

    monkeypatch.setattr(file_storage_module.file_storage, "download", _fake_download)

    response = await chat_api.download_file(str(file_att.id), user=user, db=db)
    assert response.body == b"anon"


@pytest.mark.asyncio
async def test_download_file_grants_null_user_assignment_run_owner_scope(
    monkeypatch,
):
    """When ``user_id`` is NULL but the file is bound to an assignment
    run the requester owns, the download succeeds via
    ``require_run_access``. Covers assignment-engine ``generate_file``
    outputs and custom-tool ``$output_files`` for runs that legitimately
    can't carry a ``user_id``."""
    from app.api.v1 import chat as chat_api

    user_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    run = SimpleNamespace(id=run_uuid, user_id=user_uuid, is_company_managed=False)
    file_att = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        conversation_id=None,
        assignment_run_id=run_uuid,
        filename="run.csv",
        content_type="text/csv",
        size=8,
        s3_object_name="key/run.csv",
    )
    # Order: FileAttachment row, then AssignmentRun row (no conversation
    # in between because file_att.conversation_id is None).
    db = _DownloadStubDB([file_att, run])
    user = SimpleNamespace(id=user_uuid, role="user")

    from app.services import file_storage as file_storage_module

    async def _fake_download(_key):
        return b"csv-bytes"

    monkeypatch.setattr(file_storage_module.file_storage, "download", _fake_download)

    response = await chat_api.download_file(str(file_att.id), user=user, db=db)
    assert response.body == b"csv-bytes"


@pytest.mark.asyncio
async def test_download_file_rejects_null_user_run_when_run_is_company_managed_and_not_admin():
    """Counterpart: a company-managed assignment run (``run.user_id IS NULL``)
    must NOT let a non-admin requester download its files via the
    NULL-user fast-path. ``require_run_access`` raises 403, the file
    download falls through to its own 403, and a non-admin user can't
    enumerate / exfiltrate run outputs they're not entitled to see."""
    from fastapi import HTTPException

    from app.api.v1 import chat as chat_api

    run_uuid = uuid.uuid4()
    run = SimpleNamespace(
        id=run_uuid, user_id=None, is_company_managed=True
    )  # company-managed
    file_att = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        conversation_id=None,
        assignment_run_id=run_uuid,
        filename="cm-run.csv",
        content_type="text/csv",
        size=8,
        s3_object_name="key/cm-run.csv",
    )
    db = _DownloadStubDB([file_att, run])
    user = SimpleNamespace(id=uuid.uuid4(), role="user")  # non-admin

    with pytest.raises(HTTPException) as exc_info:
        await chat_api.download_file(str(file_att.id), user=user, db=db)
    # Refused like an unknown id: never a 403 that confirms it exists.
    assert (exc_info.value.status_code, exc_info.value.detail) == (404, "File not found")


# ──────────────────────────────────────────────────────────────────────────
# get_run_detail attachment visibility
#
# ``get_run_detail`` lets a non-admin view a company-managed run
# (``run.user_id IS NULL``). Its ``attachments[]`` block hands out
# filename / content_type / size metadata plus
# ``/chat/files/{id}/download`` URLs — but those downloads are gated to
# admins / run owners by ``_authorize_file_access`` ->
# ``require_run_access``. Surfacing the metadata to a non-admin viewing a
# company-managed run therefore leaks metadata AND returns links that
# always 403. The endpoint must only surface attachments to viewers who
# can actually download them (admin OR run owner). These tests pin both
# directions of that contract.
# ──────────────────────────────────────────────────────────────────────────


class _RunDetailResult:
    """Flexible result stub supporting every accessor ``get_run_detail``
    uses: ``scalar_one_or_none`` (run / agent / assignment rows),
    ``scalar_one`` (recursive descendant count), and ``scalars().all()``
    (the attachment rows)."""

    def __init__(self, *, scalar=None, count=None, rows=None):
        self._scalar = scalar
        self._count = count
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._count

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


class _RunDetailDB:
    """Pop-style fake session mirroring ``get_run_detail``'s query order:
    run, agent, assignment (when ``assignment_id`` set), descendant count,
    and finally — only when the viewer is authorized — the attachments."""

    def __init__(self, results):
        self._queue = list(results)

    async def execute(self, _statement, *_args, **_kwargs):
        # ``get_run_detail`` issues the recursive descendant-count query as
        # ``execute(sa_text(...), {"rid": ...})`` — accept the extra params
        # positional/keyword argument so the stub matches every call shape.
        if not self._queue:
            return _RunDetailResult()
        return self._queue.pop(0)


def _make_run_detail_fixtures(
    *, run_user_id, attachment_source="agent_download", is_company_managed=None
):
    """Build the run / agent / assignment / attachment stubs shared by the
    ``get_run_detail`` visibility tests below.

    ``attachment_source`` mirrors the ``FileAttachment.source`` provenance
    the ``generate_file`` tool persists (``agent_working`` for scratch
    artifacts, ``agent_download`` / ``agent_generated`` for deliverables) so
    the ``file_tag`` mapping can be exercised. ``is_company_managed`` defaults
    to ``run_user_id is None`` but can be overridden to model the always-set
    invariant where a company-managed run carries a real run-as owner."""
    import datetime

    run_uuid = uuid.uuid4()
    agent_uuid = uuid.uuid4()
    assignment_uuid = uuid.uuid4()
    run = SimpleNamespace(
        id=run_uuid,
        assignment_id=assignment_uuid,
        agent_id=agent_uuid,
        user_id=run_user_id,
        # A run with no owner is company-managed under the always-set-owner
        # model; a run with an owner is personal — unless explicitly
        # overridden (company-managed run carrying a run-as owner).
        is_company_managed=(
            (run_user_id is None) if is_company_managed is None else is_company_managed
        ),
        status="completed",
        messages=None,
        notification_sent=False,
        parent_run_id=None,
        trigger_payload=None,
        trigger_context=None,
        actions_taken=None,
        result_summary=None,
        error=None,
        pending_approvals=None,
        # Read by ``_run_to_response`` for the derived ``approval_answered``.
        # Present on every real row; the stub has to carry it too.
        approval_decisions=None,
        failure_cause=None,
        failure_determination=None,
        stop_reason=None,
        human_input_question=None,
        human_input_response=None,
        human_input_history=None,
        langfuse_trace_id=None,
        llm_trace_id=None,
        total_input_tokens=None,
        total_output_tokens=None,
        started_at=None,
        completed_at=None,
        created_at=datetime.datetime(2026, 5, 29, tzinfo=datetime.UTC),
    )
    agent = SimpleNamespace(name="Agent", icon="Bot")
    assignment = SimpleNamespace(
        name="Assignment", notify_on_complete=False, show_in_digest=False
    )
    attachment = SimpleNamespace(
        id=uuid.uuid4(),
        filename="cm-run.csv",
        content_type="text/csv",
        size=8,
        source=attachment_source,
    )
    return run, agent, assignment, attachment


@pytest.mark.asyncio
async def test_get_run_detail_hides_attachments_from_non_admin_on_company_managed_run():
    """A non-admin viewing a company-managed run (``user_id IS NULL``)
    must NOT receive attachment metadata — every download URL would 403,
    so surfacing it both leaks metadata and hands out broken links."""
    from app.api.v1 import assignments as assignments_api

    run, agent, assignment, _attachment = _make_run_detail_fixtures(run_user_id=None)
    # No attachment result is queued: the gate must short-circuit before
    # the attachment query is ever issued for an unauthorized viewer.
    db = _RunDetailDB(
        [
            _RunDetailResult(scalar=run),
            _RunDetailResult(scalar=agent),
            _RunDetailResult(scalar=assignment),
            _RunDetailResult(count=0),
        ]
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)

    resp = await assignments_api.get_run_detail(str(run.id), user=user, db=db)
    assert resp.attachments is None


@pytest.mark.asyncio
async def test_get_run_detail_surfaces_attachments_to_admin_on_company_managed_run():
    """An admin viewing the same company-managed run can download the
    files, so the metadata is surfaced."""
    from app.api.v1 import assignments as assignments_api

    run, agent, assignment, attachment = _make_run_detail_fixtures(run_user_id=None)
    db = _RunDetailDB(
        [
            _RunDetailResult(scalar=run),
            _RunDetailResult(scalar=agent),
            _RunDetailResult(scalar=assignment),
            _RunDetailResult(count=0),
            _RunDetailResult(rows=[attachment]),
        ]
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True)

    resp = await assignments_api.get_run_detail(str(run.id), user=user, db=db)
    assert resp.attachments is not None
    assert len(resp.attachments) == 1
    assert resp.attachments[0].filename == "cm-run.csv"
    assert resp.attachments[0].download_url.endswith(
        f"/chat/files/{attachment.id}/download"
    )
    # ``agent_download`` provenance → ``download_file`` render intent.
    assert resp.attachments[0].file_tag == "download_file"


@pytest.mark.asyncio
async def test_get_run_detail_maps_working_file_tag_from_source():
    """A run attachment persisted with ``source="agent_working"`` (the
    ``generate_file`` default for scratch artifacts) must surface
    ``file_tag="working_file"`` so the run-detail UI renders a compact chip
    instead of a prominent download card — mirroring
    ``attachment_meta_from_file_attachment`` in ``chat.py``."""
    from app.api.v1 import assignments as assignments_api

    run, agent, assignment, attachment = _make_run_detail_fixtures(
        run_user_id=None, attachment_source="agent_working"
    )
    db = _RunDetailDB(
        [
            _RunDetailResult(scalar=run),
            _RunDetailResult(scalar=agent),
            _RunDetailResult(scalar=assignment),
            _RunDetailResult(count=0),
            _RunDetailResult(rows=[attachment]),
        ]
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True)

    resp = await assignments_api.get_run_detail(str(run.id), user=user, db=db)
    assert resp.attachments is not None
    assert resp.attachments[0].file_tag == "working_file"


@pytest.mark.asyncio
async def test_get_run_detail_surfaces_attachments_to_run_owner():
    """A non-admin viewing their OWN personal run can download the files
    (``require_run_access`` permits the owner), so the metadata is
    surfaced."""
    from app.api.v1 import assignments as assignments_api

    owner_uuid = uuid.uuid4()
    run, agent, assignment, attachment = _make_run_detail_fixtures(
        run_user_id=owner_uuid
    )
    db = _RunDetailDB(
        [
            _RunDetailResult(scalar=run),
            _RunDetailResult(scalar=agent),
            _RunDetailResult(scalar=assignment),
            _RunDetailResult(count=0),
            _RunDetailResult(rows=[attachment]),
        ]
    )
    user = SimpleNamespace(id=owner_uuid, is_admin=False)

    resp = await assignments_api.get_run_detail(str(run.id), user=user, db=db)
    assert resp.attachments is not None
    assert len(resp.attachments) == 1


@pytest.mark.asyncio
async def test_get_run_detail_hides_attachments_from_run_as_user_on_company_managed_run():
    """A non-admin run-as / triggering user (``run.user_id == viewer``) on a
    COMPANY-MANAGED run must NOT see attachment metadata. Downloads are
    admin-gated for company-managed runs (``require_run_access``), so the
    metadata gate must mirror that and stay admin-only — keying on
    ``user_id == viewer`` alone would leak filenames/sizes and hand out
    always-403 links to the non-admin run-as user (the always-set-owner
    invariant now stamps that id on company-managed runs)."""
    from app.api.v1 import assignments as assignments_api

    viewer_id = uuid.uuid4()
    # Company-managed run that carries a run-as owner = the (non-admin) viewer.
    run, agent, assignment, _attachment = _make_run_detail_fixtures(
        run_user_id=viewer_id
    )
    run.is_company_managed = True
    # No attachment result queued: the gate must short-circuit before querying.
    db = _RunDetailDB(
        [
            _RunDetailResult(scalar=run),
            _RunDetailResult(scalar=agent),
            _RunDetailResult(scalar=assignment),
            _RunDetailResult(count=0),
        ]
    )
    user = SimpleNamespace(id=viewer_id, is_admin=False)

    resp = await assignments_api.get_run_detail(str(run.id), user=user, db=db)
    assert resp.attachments is None


# ──────────────────────────────────────────────────────────────────────────
# $output_files skip-warning diagnostics
#
# The four ``Skipping $output_files entry …`` warnings must all include
# the entry index AND the filename so a custom-tool author can correlate
# the log line to the rejected entry in the outgoing array. The two
# warnings that previously omitted ``idx`` and ``filename`` (malformed-
# payload and oversize-payload) were inconsistent with the surrounding
# missing-filename / non-string-content warnings; this regression test
# pins all four into a single grep-able diagnostic surface.
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output_files,expected_marker,expected_filename",
    [
        pytest.param(
            [
                {"filename": "ok.csv", "content": "a,b\n1,2"},
                {
                    "filename": "bad.bin",
                    "content": "this-is-not-valid-base64!!!",
                    "encoding": "base64",
                },
            ],
            "invalid base64 payload",
            "bad.bin",
            id="malformed-base64",
        ),
        pytest.param(
            [
                {"filename": "ok.csv", "content": "a,b\n1,2"},
                {
                    # 10-byte payload trips the oversize branch once we
                    # monkeypatch ``MAX_UPLOAD_SIZE_BYTES`` down to 5
                    # below — keeps the test memory-friendly while
                    # still exercising the actual
                    # ``len(data) > MAX_UPLOAD_SIZE_BYTES`` predicate.
                    "filename": "huge.bin",
                    "content": "abcdefghij",
                },
            ],
            "payload exceeds",
            "huge.bin",
            id="oversize",
        ),
    ],
)
async def test_output_files_skip_warnings_include_idx_and_filename(
    monkeypatch,
    caplog,
    output_files,
    expected_marker,
    expected_filename,
):
    """Both skip paths (invalid-base64 / oversize) must log the entry
    index AND the filename so a tool author can correlate the warning
    to the offending entry in the outgoing ``$output_files`` array.
    """
    import logging as _logging

    from app.services import builtin_tools

    tool_id = uuid.uuid4()
    fake_tool = SimpleNamespace(
        id=tool_id,
        name="multi_output_tool",
        js_function="return 'ok';",
        input_schema={"type": "object", "properties": {}},
        enabled=True,
        language="javascript",
        timeout=10,
        description="multi-output tool",
    )
    db = _FakeDB([fake_tool])
    composite = _FakeComposite()

    async def _fake_exec(*_args, **_kwargs):
        return json.dumps({"result": "done", "$output_files": output_files})

    # ``MAX_UPLOAD_SIZE_BYTES`` is imported inside the function body
    # from ``app.services.file_storage``; patch the source module so
    # every late-binding ``from app.services.file_storage import ...``
    # site (including the one inside ``load_custom_tools``) picks up
    # the smaller cap.
    from app.services import file_storage as _file_storage

    monkeypatch.setattr(_file_storage, "MAX_UPLOAD_SIZE_BYTES", 5)

    async def _fake_persist(**_kwargs):
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=_kwargs.get("filename", "f.bin"),
            content_type=_kwargs.get("content_type") or "application/octet-stream",
            size=len(_kwargs.get("raw_bytes", b"") or b""),
            s3_object_name="key/x",
        )

    monkeypatch.setattr(
        "app.services.builtin_tools.execute_tool_auto", _fake_exec, raising=False
    )
    monkeypatch.setattr(
        "app.services.custom_tool_executor.execute_tool_auto", _fake_exec
    )
    monkeypatch.setattr(
        "app.services.builtin_tools._persist_generated_file", _fake_persist
    )

    await load_custom_tools(
        db,
        [tool_id],
        composite,
        user_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )
    assert composite.executor is not None
    composite.executor.file_context = FileContext()

    with caplog.at_level(_logging.WARNING, logger=builtin_tools.logger.name):
        await composite.executor.execute_tool(
            ToolCall(id="call-multi", name="multi_output_tool", arguments={})
        )

    warning_records = [
        record for record in caplog.records
        if record.levelno == _logging.WARNING
        and "Skipping $output_files entry" in record.message
    ]
    assert warning_records, (
        f"expected a 'Skipping $output_files entry' WARNING containing "
        f"{expected_marker!r}; got: {[r.message for r in caplog.records]}"
    )
    # At least one of the skip warnings must surface the offending
    # filename AND the entry index (``1`` because ``"ok.csv"`` is entry
    # ``0`` and the offender is always the second item in the test
    # fixtures above).
    matched = [
        r for r in warning_records
        if expected_marker in r.message
        and expected_filename in r.message
        and "entry 1" in r.message
    ]
    assert matched, (
        f"WARNING must include entry index AND filename {expected_filename!r}; "
        f"got: {[r.message for r in warning_records]}"
    )


# ──────────────────────────────────────────────────────────────────────────
# generate_file built-in is always available (no feature flag).
#
# The ``generate_file`` surface is part of the platform's core
# agentic toolkit, in lockstep with ``generate_image`` /
# ``code_interpreter``: agent / GA / assignment loops include it
# whenever the entity opts in via ``builtin_tool_ids``. No
# environment variable gates it on or off — this regression test
# pins that contract so a future refactor that re-introduces a
# feature flag (and the corresponding silent-disable failure mode)
# trips immediately.
# ──────────────────────────────────────────────────────────────────────────


def test_generate_file_has_no_feature_flag_gate():
    """``Settings`` and ``PlatformConfigResponse`` MUST NOT carry an
    ``agent_file_generation`` field. The tool is always available when
    selected via ``builtin_tool_ids``; a feature flag would silently
    suppress every agent-generated file the day it flips off, which
    is exactly the failure mode the GA / Profile / Tooling pickers
    can't surface to operators."""
    from app.api.v1.platform import PlatformConfigResponse
    from app.core.config import Settings

    assert "agent_file_generation" not in Settings.model_fields, (
        "Settings must not declare ``agent_file_generation`` — the tool "
        "is always available; gating it behind a flag was removed."
    )
    assert "agent_file_generation" not in PlatformConfigResponse.model_fields, (
        "PlatformConfigResponse must not expose ``agent_file_generation`` — "
        "the frontend should always render the generate_file picker entry."
    )


def test_ctem_assistant_wires_generate_file_by_default():
    """The XTM One CTEM Assistant (the platform's primary entry point)
    MUST ship with ``generate_file`` in its ``builtin_tool_ids`` so
    fresh XTM One deployments can produce downloadable artifacts —
    STIX bundles, threat reports, IOC lists, detection rules — out
    of the box without an admin opt-in. A regression that drops
    ``generate_file`` from the CTEM Assistant seed would silently
    disable the agent's file-output surface on every restart (seed
    sync overwrites ``builtin_tool_ids``)."""
    from app.agents.builtin_agents import BUILTIN_AGENTS

    by_slug = {a["slug"]: a for a in BUILTIN_AGENTS}
    ctem = by_slug.get("ctem-assistant")
    assert ctem is not None, "ctem-assistant must exist in BUILTIN_AGENTS"
    assert "generate_file" in ctem["builtin_tool_ids"], (
        "ctem-assistant must wire ``generate_file`` by default; got "
        f"{ctem['builtin_tool_ids']!r}"
    )

    xtm_one = by_slug.get("xtm-one-assistant")
    assert xtm_one is not None, "xtm-one-assistant must exist in BUILTIN_AGENTS"
    assert "generate_file" in xtm_one["builtin_tool_ids"], (
        "xtm-one-assistant must wire ``generate_file`` by default; got "
        f"{xtm_one['builtin_tool_ids']!r}"
    )


def test_general_assistant_seeds_generate_file_by_default():
    """Every ``GeneralAssistantConfig`` creation site MUST seed
    ``builtin_tool_ids`` from the shared ``DEFAULT_GA_BUILTIN_TOOL_IDS``
    constant — which carries ``generate_file`` — so a brand-new Copilot
    deployment ships with the file-output surface ON. A regression that
    drops the seed would force every operator to manually opt in,
    defeating the "agent-generated files are a core platform capability"
    contract. The same constant also switches the presentation
    tools on, so a construction site with a hand-written literal list is
    a drift risk and is refused here as well."""
    import inspect

    from app.api.v1 import settings as settings_module
    from app.models.general_assistant_config import DEFAULT_GA_BUILTIN_TOOL_IDS

    assert "generate_file" in DEFAULT_GA_BUILTIN_TOOL_IDS
    assert "generate_presentation" in DEFAULT_GA_BUILTIN_TOOL_IDS
    assert "template_search" in DEFAULT_GA_BUILTIN_TOOL_IDS

    source = inspect.getsource(settings_module)
    # Look for the two ``GeneralAssistantConfig(...)`` constructions
    # and confirm each seeds from the shared constant.
    ga_constructions = source.count("GeneralAssistantConfig(")
    ga_with_seed = source.count("builtin_tool_ids=list(DEFAULT_GA_BUILTIN_TOOL_IDS)")
    assert ga_constructions == 2, (
        f"Expected exactly 2 GeneralAssistantConfig constructions in "
        f"api/v1/settings.py (first-provider-bootstrap + GA-config "
        f"first-write); got {ga_constructions}. If new construction "
        f"sites are added they must seed ``builtin_tool_ids``."
    )
    assert ga_with_seed == ga_constructions, (
        f"All {ga_constructions} GeneralAssistantConfig constructions "
        f"must seed ``builtin_tool_ids=list(DEFAULT_GA_BUILTIN_TOOL_IDS)``; "
        f"only {ga_with_seed} do."
    )
    assert 'builtin_tool_ids=["generate_file"]' not in source

    # The env-driven boot seed (DEFAULT_GENERAL_ASSISTANT_MODEL) constructs
    # the row too, in main.py, and must not fall behind the API sites.
    main_source = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"
    ).read_text(encoding="utf-8")
    assert "_GAConfig(" in main_source
    assert "builtin_tool_ids=list(_GA_DEFAULT_TOOLS)" in main_source
    assert 'builtin_tool_ids=["generate_file"]' not in main_source


# ──────────────────────────────────────────────────────────────────────────
# general_chat caller-provided file_context
#
# ``run_general_chat`` accepts an optional caller-provided
# ``FileContext`` so the REST/WS chat layer can detect newly-generated
# files added during agentic-loop execution. A previous shape
# unconditionally reassigned the parameter from
# ``load_conversation_files(...)``, shadowing the caller's reference
# — every new file added during the run went into a fresh local
# context the caller never saw, and assistant messages on the General
# Assistant path rendered without their freshly-generated attachments.
# The fix is a single ``if file_context is None:`` guard mirroring the
# canonical pattern in ``agent_chat.run_agent_chat``. This source-
# level test pins the guard so a future refactor that re-collapses
# it trips immediately.
# ──────────────────────────────────────────────────────────────────────────


def test_general_chat_does_not_shadow_caller_provided_file_context():
    """The ``load_conversation_files`` call in ``run_general_chat``
    MUST be guarded by ``if file_context is None:`` so a caller-
    provided ``FileContext`` is never replaced by a fresh one (which
    would silently break the REST/WS layer's post-call file-detection
    walk).
    """
    import inspect
    import re

    from app.services import general_chat

    source = inspect.getsource(general_chat.run_general_chat)

    assignments = list(
        re.finditer(
            r"^(?P<indent>[ \t]*)file_context\s*=\s*await\s+load_conversation_files\(",
            source,
            flags=re.MULTILINE,
        )
    )
    assert assignments, (
        "run_general_chat must call load_conversation_files for "
        "conversation-history hydration when the caller doesn't pass one"
    )

    for assignment in assignments:
        prefix = source[: assignment.start()]
        prior_lines = [
            line for line in prefix.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert prior_lines, (
            "no statements before load_conversation_files? source dump:\n"
            + source[: assignment.start() + 200]
        )
        guard_line = prior_lines[-1]
        assert "if file_context is None" in guard_line, (
            f"load_conversation_files in run_general_chat must be gated "
            f"by ``if file_context is None:``; preceding statement was instead:\n"
            f"  {guard_line}"
        )


# ──────────────────────────────────────────────────────────────────────────
# $output_files encoding validation
#
# The custom-tool ``$output_files`` loop in ``load_custom_tools``
# previously treated any ``encoding`` value other than the exact
# literal ``"base64"`` as UTF-8 text. A custom-tool author who typed
# ``"Base64"`` / ``"BASE64"`` / ``"hex"`` would silently get the
# base64 text round-tripped as UTF-8 bytes — completely corrupting
# the persisted file. ``build_generate_file_tool`` (the agent-facing
# sibling tool) already validates the encoding set explicitly and
# returns an error on unknown encodings. The fix mirrors that
# allowed set in ``$output_files`` and skips (with a uniform
# diagnostic WARNING) any entry whose encoding is outside the set.
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_encoding",
    [
        "Base64",  # common typo — base64 with a capital B
        "BASE64",  # capital
        "hex",     # caller mistakenly thinks hex is supported
        "binary",  # caller mistakenly thinks binary is supported
        "latin-1",  # any other encoding
    ],
)
async def test_output_files_skips_entries_with_unsupported_encoding(
    monkeypatch, caplog, bad_encoding
):
    """Mirrors ``build_generate_file_tool``'s accepted-encoding set.

    A typo'd or otherwise unknown ``encoding`` value MUST cause the
    entry to be skipped with a uniform WARNING, NOT fall through to
    the UTF-8 branch — silently round-tripping base64 text as raw
    UTF-8 bytes corrupts the persisted file and the caller has no
    way to notice short of downloading and inspecting it.
    """
    import logging as _logging

    from app.services import builtin_tools

    tool_id = uuid.uuid4()
    fake_tool = SimpleNamespace(
        id=tool_id,
        name="bad_encoding_tool",
        js_function="return 'ok';",
        input_schema={"type": "object", "properties": {}},
        enabled=True,
        language="javascript",
        timeout=10,
        description="bad-encoding tool",
    )
    db = _FakeDB([fake_tool])
    composite = _FakeComposite()

    # First entry is valid (proves the loop reaches entry-1 before
    # bailing); second entry trips the unsupported-encoding guard.
    output_files = [
        {"filename": "ok.csv", "content": "a,b\n1,2"},
        {
            "filename": "bad.bin",
            "content": "aGVsbG8=",  # would be valid base64 text
            "encoding": bad_encoding,
        },
    ]

    async def _fake_exec(*_args, **_kwargs):
        return json.dumps({"result": "done", "$output_files": output_files})

    async def _fake_persist(**_kwargs):
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=_kwargs.get("filename", "f.bin"),
            content_type=_kwargs.get("content_type") or "application/octet-stream",
            size=len(_kwargs.get("raw_bytes", b"") or b""),
            s3_object_name="key/x",
        )

    monkeypatch.setattr(
        "app.services.builtin_tools.execute_tool_auto", _fake_exec, raising=False
    )
    monkeypatch.setattr(
        "app.services.custom_tool_executor.execute_tool_auto", _fake_exec
    )
    monkeypatch.setattr(
        "app.services.builtin_tools._persist_generated_file", _fake_persist
    )

    await load_custom_tools(
        db,
        [tool_id],
        composite,
        user_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )
    assert composite.executor is not None
    composite.executor.file_context = FileContext()

    with caplog.at_level(_logging.WARNING, logger=builtin_tools.logger.name):
        await composite.executor.execute_tool(
            ToolCall(id="call-bad-enc", name="bad_encoding_tool", arguments={})
        )

    # The skip WARNING must include all of: the entry index, the
    # filename, the literal ``unsupported encoding`` phrase, and the
    # repr of the bad encoding value (so a tool author grepping for
    # the typo immediately finds the rejected entry).
    matched = [
        record for record in caplog.records
        if record.levelno == _logging.WARNING
        and "Skipping $output_files entry 1" in record.message
        and "bad.bin" in record.message
        and "unsupported encoding" in record.message
        and repr(bad_encoding) in record.message
    ]
    assert matched, (
        f"expected a 'unsupported encoding' WARNING for encoding="
        f"{bad_encoding!r}; got: {[r.message for r in caplog.records]}"
    )


@pytest.mark.parametrize(
    "accepted_encoding",
    [None, "", "utf-8", "text", "base64"],
)
def test_output_files_accepted_encoding_set_matches_generate_file(
    accepted_encoding,
):
    """Source-level guard: the set of ``encoding`` values accepted by
    ``$output_files`` in ``load_custom_tools`` MUST match the set
    accepted by ``build_generate_file_tool._generate_file`` — a drift
    silently re-introduces the ``"Base64"``-typo corruption
    bug. Pins the contract from the test side rather than parsing
    both source bodies (which would be brittle); the test asserts
    each accepted value lands in the right branch (b64 decoded or
    UTF-8 encoded) via the agent-facing tool, AND the matching
    ``$output_files`` loop in ``load_custom_tools`` is gated on the
    same tuple via a source-text check.
    """
    import inspect

    from app.services import builtin_tools

    # 1. The agent-facing tool's accepted-encoding tuple must include
    # this value.
    agent_src = inspect.getsource(builtin_tools.build_generate_file_tool)
    assert "(None, \"\", \"utf-8\", \"text\")" in agent_src, (
        "build_generate_file_tool's accepted-encoding tuple shape "
        "drifted away from the canonical "
        "(None, \"\", \"utf-8\", \"text\") form — update this test "
        "AND the $output_files matching tuple in load_custom_tools "
        "together"
    )

    # 2. The custom-tool ``$output_files`` loop in ``load_custom_tools``
    # MUST gate unsupported encodings via a matching membership check
    # whose tuple includes ALL of (None, "", "utf-8", "text", "base64").
    # We assert the literal token shape rather than parsing the AST
    # because the tuple is inline inside the loop.
    custom_src = inspect.getsource(builtin_tools.load_custom_tools)
    expected = "(None, \"\", \"utf-8\", \"text\", \"base64\")"
    assert expected in custom_src, (
        f"$output_files loop in load_custom_tools must gate encodings via "
        f"the literal tuple {expected!r} (a typo'd "
        f"encoding like \"Base64\" must NOT "
        f"fall through to the utf-8 branch)"
    )
    # Sanity: the accepted-encoding parameter under test is one of the
    # values listed in that tuple (catches a future test refactor that
    # adds a parameter value without extending the tuple).
    assert accepted_encoding in (None, "", "utf-8", "text", "base64")


def test_run_custom_return_annotation_includes_tool_result():
    """``_run_custom`` returns either a ``str`` (legacy fast path) or
    a ``ToolResult`` (when ``$output_files`` is present). The return
    annotation must mention both — a stale ``-> str`` annotation
    misleads readers and trips strict type checkers downstream.

    The annotation lives inside the closure body of
    ``load_custom_tools``, so we pin it via the function source
    rather than ``__annotations__``.
    """
    import inspect

    from app.services import builtin_tools

    source = inspect.getsource(builtin_tools.load_custom_tools)
    # The closure signature is multi-line; match the closing paren +
    # arrow on its own line.
    assert ") -> str | ToolResult:" in source, (
        "_run_custom must be annotated as ``-> str | ToolResult`` so "
        "readers and type checkers see both return shapes; "
        "current source does not "
        "contain the expected annotation"
    )


# ──────────────────────────────────────────────────────────────────────────
# download_message_file — bytes-streaming response contract
#
# The endpoint used to 307-redirect to ``file_storage.presigned_url(...)``
# but that breaks in any deployment whose S3 endpoint is private (Docker
# ``minio:9000``, internal VPC, etc.) — the redirect target is
# unresolvable from the browser. ``download_file`` already streams the
# bytes through the backend; ``download_message_file`` must take the
# same shape so external clients can fetch message-scoped files
# regardless of where the object store lives.
# ──────────────────────────────────────────────────────────────────────────


class _MsgDownloadStubDB:
    """Pop-style fake session mirroring ``download_message_file``'s call order.

    1. ``select(Conversation).where(id==... AND user_id==...)``
    2. ``select(Message).where(id==... AND conversation_id==...)``
    3. ``select(FileAttachment).where(id==... AND message_id==... AND conversation_id==...)``
    """

    def __init__(self, results):
        self._queue = list(results)

    async def execute(self, _statement):
        if not self._queue:
            return _DownloadStubResult(None)
        return _DownloadStubResult(self._queue.pop(0))

    async def commit(self) -> None:
        """The route commits the file's last use (retention, #396) before answering."""


@pytest.mark.asyncio
async def test_download_message_file_streams_bytes_not_redirect(monkeypatch):
    """Pin the new bytes-streaming contract: ``download_message_file``
    must call ``file_storage.download(...)`` and return the bytes
    inline (``Response(content=...)``) rather than 307-redirecting to
    ``file_storage.presigned_url(...)``. A redirect would break
    deployments whose S3 endpoint is private (Docker ``minio:9000``).

    A regression that re-introduces the redirect shape would either
    fail the ``response.body == b"..."`` assertion (RedirectResponse
    has no ``body``) or fail the import-shape assertion (the endpoint
    must NOT depend on ``RedirectResponse`` anymore).
    """
    from app.api.v1 import chat as chat_api

    user_uuid = uuid.uuid4()
    conv_uuid = uuid.uuid4()
    msg_uuid = uuid.uuid4()
    file_uuid = uuid.uuid4()

    conv = SimpleNamespace(id=conv_uuid, user_id=user_uuid)
    msg = SimpleNamespace(id=msg_uuid, conversation_id=conv_uuid)
    file_att = SimpleNamespace(
        id=file_uuid,
        user_id=user_uuid,
        conversation_id=conv_uuid,
        message_id=msg_uuid,
        filename="export.csv",
        content_type="text/csv",
        size=12,
        s3_object_name="chat/conv/export.csv",
    )

    db = _MsgDownloadStubDB([conv, msg, file_att])
    user = SimpleNamespace(id=user_uuid, role="user")

    presigned_called = {"v": False}

    def _fail_if_called(*_a, **_kw):
        presigned_called["v"] = True
        raise AssertionError(
            "download_message_file must NOT call file_storage.presigned_url; "
            "it must stream the bytes via file_storage.download (internal "
            "S3 endpoints break the "
            "redirect path for browser clients)"
        )

    async def _fake_download(_key):
        return b"type,value\nip,1.2.3.4"

    from app.services import file_storage as file_storage_module

    monkeypatch.setattr(
        file_storage_module.file_storage, "download", _fake_download
    )
    monkeypatch.setattr(
        file_storage_module.file_storage, "presigned_url", _fail_if_called
    )

    response = await chat_api.download_message_file(
        conversation_id=str(conv_uuid),
        message_id=str(msg_uuid),
        file_id=str(file_uuid),
        user=user,
        db=db,
    )

    assert response.body == b"type,value\nip,1.2.3.4"
    assert response.media_type == "text/csv"
    # The new ``build_content_disposition`` shape emits both an
    # ASCII-quoted ``filename="…"`` (RFC 6266 fallback) AND a
    # percent-encoded ``filename*=UTF-8''…`` (RFC 5987) so both
    # legacy and modern clients render the right name. ``export.csv``
    # is pure ASCII, so the two forms render identically.
    assert response.headers["content-disposition"] == (
        'attachment; filename="export.csv"; filename*=UTF-8\'\'export.csv'
    )
    assert presigned_called["v"] is False, (
        "presigned_url MUST NOT be called by download_message_file"
    )


def test_download_message_file_does_not_import_redirect_response():
    """Belt-and-braces structural check: ``chat.py`` must not import
    ``RedirectResponse`` anymore. A future refactor that re-introduces
    the redirect shape (or just leaves a stale import) trips here.
    """
    import inspect

    from app.api.v1 import chat as chat_api

    source = inspect.getsource(chat_api)
    assert "RedirectResponse" not in source, (
        "RedirectResponse should not appear in app/api/v1/chat.py — "
        "download_message_file streams bytes through the backend "
        "instead of redirecting to a presigned S3 URL; a regression that "
        "re-imports RedirectResponse "
        "trips this assertion"
    )


# ---------------------------------------------------------------------------
# Observability for user-message file linkage +
# `_run_custom` return-normalisation contract
# ---------------------------------------------------------------------------


def test_send_message_user_attachment_linkage_logs_on_failure():
    """The assistant-side linkage fix replaced
    ``contextlib.suppress(Exception)`` around the ``UPDATE
    file_attachments SET message_id=assistant_msg.id`` with
    ``try / except: logger.exception(...)`` so an orphan row surfaces in
    audit tooling. The same contract extends to the symmetric
    USER-side path (REST ``send_message`` + WS chat handler). A silent
    suppress on the user side would let the response carry a
    message-scoped download URL that 404s while the file itself remains
    reachable via the legacy ``/chat/files/{id}/download`` URL — exactly
    the same observability hole as the assistant-side path had before
    its fix. Pin the contract by source-level inspection so a regression
    that puts ``contextlib.suppress`` back around the user-side
    ``update(FileAttachment).values(message_id=user_msg.id)`` blocks
    trips here loudly.
    """
    import inspect

    from app.api.v1 import chat as chat_api

    source = inspect.getsource(chat_api)
    # The user-side REST + WS UPDATE blocks must NOT be wrapped in
    # ``contextlib.suppress`` — both should be ``try / except``.
    assert "contextlib.suppress" not in source or all(
        marker not in source
        for marker in (
            "contextlib.suppress(Exception):\n                await db.execute(\n                    update(FileAttachment)\n                    .where(\n                        FileAttachment.id == uuid.UUID(fid),\n                        FileAttachment.user_id == user.id,",
            "contextlib.suppress(Exception):\n                            fid_uuid = uuid.UUID(fid)\n                            await db.execute(\n                                update(FileAttachment)\n                                .where(\n                                    FileAttachment.id == fid_uuid,\n                                    FileAttachment.user_id == user_id,",
        )
    ), (
        "REST send_message and WS chat handler must NOT wrap the "
        "user-message file-linkage UPDATE in contextlib.suppress — the "
        "assistant-side and user-side fixes both "
        "require try / except: logger.exception so failed linkage "
        "surfaces in audit tooling."
    )
    # And both sites must include a logger.exception call mentioning
    # the user-uploaded file linkage failure, so a regression that
    # restores the silent suppress trips on the missing log call too.
    assert (
        source.count("Failed to link user-uploaded file") >= 2
    ), (
        "Both the REST send_message and WS chat handler must log at "
        "ERROR with 'Failed to link user-uploaded file ...' when the "
        "linkage UPDATE raises."
    )


@pytest.mark.asyncio
async def test_run_custom_normalises_no_output_files_result_to_string(monkeypatch):
    """``_run_custom`` is annotated ``-> str | ToolResult``. The
    ``$output_files`` branch already normalises ``parsed_output["result"]``
    via ``json.dumps(result_payload, default=str)`` when it isn't a
    string, but the no-``$output_files`` branch used to return
    ``parsed_output.get("result", raw_output)`` directly — meaning a
    custom tool that emitted ``{"result": {"counts": {...}}}`` would
    return a ``dict`` to the executor, violating the annotation and
    relying on ``CallableToolExecutor``'s implicit JSON serialisation.

    Pin the new contract: when ``$output_files`` is absent, a non-string
    ``result`` is JSON-serialised by ``_run_custom`` itself.
    """
    tool_id = uuid.uuid4()
    fake_tool = SimpleNamespace(
        id=tool_id,
        name="dict_result_tool",
        js_function="return 'ok';",
        input_schema={"type": "object", "properties": {}},
        enabled=True,
        language="javascript",
        timeout=10,
        description="returns a dict-shaped result with no $output_files",
    )
    db = _FakeDB([fake_tool])
    composite = _FakeComposite()

    async def _fake_exec(*_args, **_kwargs):
        # Dict-shaped ``result`` with NO ``$output_files`` key — pre-fix,
        # ``_run_custom`` would return the raw dict, violating its
        # ``-> str | ToolResult`` annotation.
        return json.dumps({"result": {"counts": {"a": 1, "b": 2}}})

    monkeypatch.setattr(
        "app.services.builtin_tools.execute_tool_auto", _fake_exec, raising=False
    )
    monkeypatch.setattr(
        "app.services.custom_tool_executor.execute_tool_auto", _fake_exec
    )

    await load_custom_tools(db, [tool_id], composite)
    assert composite.executor is not None

    composite.executor.file_context = FileContext()
    result = await composite.executor.execute_tool(
        ToolCall(id="call-norm", name="dict_result_tool", arguments={})
    )

    assert result.is_error is False
    assert isinstance(result.content, str), (
        f"_run_custom must return a string for no-$output_files dict "
        f"results (got {type(result.content).__name__}: {result.content!r})"
    )
    # JSON-decodable back to the original dict — pins the
    # ``json.dumps(result_payload, default=str)`` normalisation
    # shape, not just any random stringification.
    decoded = json.loads(result.content)
    assert decoded == {"counts": {"a": 1, "b": 2}}, (
        f"Normalised content must round-trip via json.dumps; got {result.content!r}"
    )


def test_run_custom_source_pins_no_output_files_normalisation():
    """Source-level guard: the no-``$output_files`` branch in
    ``load_custom_tools::_run_custom`` must NOT return
    ``parsed_output.get("result", raw_output)`` directly — that returns
    arbitrary JSON-decoded types (dict / list / scalar) and violates
    the function's ``-> str | ToolResult`` annotation. A regression to
    the bare-``.get`` shape trips this test.
    """
    import inspect

    from app.services import builtin_tools as bt

    source = inspect.getsource(bt.load_custom_tools)
    # The bare return must be gone.
    assert "return parsed_output.get(\"result\", raw_output)" not in source, (
        "_run_custom must normalise the no-$output_files ``result`` "
        "payload via json.dumps; a "
        "regression to ``return parsed_output.get('result', raw_output)`` "
        "trips this assertion."
    )
    # And the normalisation block must be present.
    assert (
        "json.dumps(result_payload, default=str)" in source
    ), (
        "_run_custom must call json.dumps(result_payload, default=str) "
        "for the no-$output_files non-string result path so the "
        "``-> str | ToolResult`` annotation is honoured."
    )


# ---------------------------------------------------------------------------
# Content-Disposition security
# ---------------------------------------------------------------------------
#
# Filenames travel through the codebase from user uploads (KB documents,
# chat attachments) and from AI-generated output (built-in
# ``generate_file`` and custom-tool ``$output_files``), all of which are
# attacker-influenceable. Pin that the chat download endpoints use
# ``build_content_disposition`` instead of raw f-string interpolation.

def test_build_content_disposition_strips_crlf_injection_attempt():
    """``\\r\\n`` in a filename MUST be stripped before reaching the
    Content-Disposition header — otherwise an attacker-controlled
    filename could smuggle a second header
    (``\\r\\nSet-Cookie: …``) into the response.
    """
    from app.core.utils import build_content_disposition

    malicious = 'evil.csv\r\nSet-Cookie: stolen=1\r\n'
    header = build_content_disposition(malicious)
    assert "\r" not in header, "CR must be stripped (header injection vector)"
    assert "\n" not in header, "LF must be stripped (header injection vector)"
    assert "Set-Cookie" in header, (
        "The literal ``Set-Cookie`` text is allowed to remain in the "
        "filename — what matters is that the CR/LF separator is gone "
        "so the receiver can't parse a second header."
    )


def test_build_content_disposition_escapes_quotes_and_backslashes():
    """Filenames containing ``"`` or ``\\`` MUST be escaped so the
    quoted-form parses correctly per RFC 6266 / RFC 7230. A raw
    ``"`` would otherwise close the filename param early and let the
    rest of the name leak as a separate (potentially attacker-named)
    parameter.
    """
    from app.core.utils import build_content_disposition

    header = build_content_disposition('weird ".name')
    # Quoted form: the ASCII fallback must escape the literal ``"``.
    assert 'filename="weird \\".name"' in header, header
    # And the RFC 5987 form percent-encodes the same characters.
    assert "filename*=UTF-8''" in header


def test_build_content_disposition_emits_rfc5987_for_unicode():
    """Non-ASCII filenames must round-trip via the RFC 5987
    ``filename*=UTF-8''…`` form. The ASCII fallback substitutes
    ``?`` for the unsupported chars (legacy clients only see the
    fallback) but the modern form preserves the original bytes via
    percent-encoding.
    """
    from app.core.utils import build_content_disposition

    header = build_content_disposition("rapport-éàü.csv")
    # ASCII fallback: non-ASCII chars become ``?``.
    assert 'filename="rapport-???.csv"' in header
    # RFC 5987: percent-encoded UTF-8 of the same name.
    assert "filename*=UTF-8''rapport-%C3%A9%C3%A0%C3%BC.csv" in header


def test_chat_download_endpoints_use_build_content_disposition_helper():
    """Source-level guard: ``download_file`` and
    ``download_message_file`` in ``api/v1/chat.py`` MUST go through
    ``build_content_disposition`` (the shared CRLF-safe helper) instead
    of f-string interpolation of the raw filename. A regression that
    re-introduces ``f'attachment; filename="{file_att.filename}"'``
    trips this assertion immediately. The call lives in the one header
    helper the file routes share (``app.services.file_responses``).
    """
    from pathlib import Path

    app_dir = Path(__file__).parent.parent / "app"
    src = (app_dir / "api" / "v1" / "chat.py").read_text(encoding="utf-8")
    helper_src = (app_dir / "services" / "file_responses.py").read_text(encoding="utf-8")
    for source in (src, helper_src):
        assert (
            'f\'attachment; filename="{file_att.filename}"\'' not in source
        ), (
            "File routes must NOT interpolate raw filenames into "
            "Content-Disposition — use ``build_content_disposition`` from "
            "``app.core.utils`` (CRLF-safe + RFC 5987 UTF-8 form)."
        )
    assert "build_content_disposition(file_att.filename)" in helper_src, (
        "The file routes' header helper must call "
        "``build_content_disposition(file_att.filename)`` for their "
        "``Content-Disposition`` header."
    )


# ──────────────────────────────────────────────────────────────────────────
# Missing-S3-key graceful handling
#
# A generated file referenced by an old chat message can disappear from the
# object store (deleted, expired by a lifecycle policy, or never persisted).
# Production logs showed the raw ``minio.error.S3Error`` (``NoSuchKey``)
# bubbling up through ``file_storage.download`` and surfacing as a generic
# ``500 File download failed`` at the chat API. The storage layer now raises
# a decoupled ``FileNotFoundInStorageError`` for missing keys, and the chat
# download endpoints translate that into a friendly ``404`` so callers never
# see the raw storage error. These tests pin both halves of that contract.
# ──────────────────────────────────────────────────────────────────────────


def _make_s3_error(code: str):
    """Construct a ``minio.error.S3Error`` with the given error code."""
    from minio.error import S3Error

    return S3Error(
        None,
        code,
        "The specified key does not exist.",
        "/xtmone/generated/u/gone.png",
        "request-id",
        "host-id",
        bucket_name="xtmone",
        object_name="generated/u/gone.png",
    )


def test_is_missing_object_error_detects_missing_key_codes():
    """``_is_missing_object_error`` must recognise the S3 "gone" codes
    case-insensitively and reject unrelated failures (so a transient
    ``AccessDenied`` / ``InternalError`` is never silently downgraded to
    a 404)."""
    from app.services.file_storage import _is_missing_object_error

    assert _is_missing_object_error(_make_s3_error("NoSuchKey")) is True
    assert _is_missing_object_error(_make_s3_error("nosuchkey")) is True
    assert _is_missing_object_error(_make_s3_error("NoSuchBucket")) is True
    assert _is_missing_object_error(_make_s3_error("AccessDenied")) is False
    assert _is_missing_object_error(_make_s3_error("InternalError")) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("s3_code", ["NoSuchKey", "NoSuchBucket"])
async def test_file_storage_download_maps_missing_key_to_domain_error(
    monkeypatch, s3_code
):
    """``file_storage.download`` must translate an S3 ``NoSuchKey`` *or*
    ``NoSuchBucket`` into a decoupled ``FileNotFoundInStorageError`` carrying
    the object name — callers should never need to import ``minio.error`` to
    detect a missing file. Both codes live in ``_MISSING_OBJECT_S3_CODES``, so
    exercising both keeps ``download()`` in lockstep with
    ``_is_missing_object_error()``."""
    from app.services import file_storage as fs_module

    class _FakeClient:
        def get_object(self, _bucket, _name):
            raise _make_s3_error(s3_code)

    monkeypatch.setattr(fs_module.file_storage, "_client", _FakeClient())

    with pytest.raises(fs_module.FileNotFoundInStorageError) as exc_info:
        await fs_module.file_storage.download("generated/u/gone.png")
    assert exc_info.value.object_name == "generated/u/gone.png"
    # The exception chain must be suppressed (raised ``from None``) so a
    # traceback can never re-leak the underlying ``S3Error`` internals
    # (bucket name, request/host ids) — the warning log already captures the
    # object name and code.
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_file_storage_download_reraises_non_missing_s3_errors(monkeypatch):
    """Non-"gone" S3 failures (e.g. ``AccessDenied``) must keep propagating
    as the raw ``S3Error`` so they still map to a ``500`` — only genuinely
    missing keys are downgraded to the friendly 404 path."""
    from minio.error import S3Error

    from app.services import file_storage as fs_module

    class _FakeClient:
        def get_object(self, _bucket, _name):
            raise _make_s3_error("AccessDenied")

    monkeypatch.setattr(fs_module.file_storage, "_client", _FakeClient())

    with pytest.raises(S3Error):
        await fs_module.file_storage.download("generated/u/denied.png")


@pytest.mark.asyncio
async def test_download_file_returns_friendly_404_when_storage_key_missing(
    monkeypatch,
):
    """``GET /files/{id}/download`` must return a friendly ``404`` (not a
    ``500``) when the underlying object is gone from storage."""
    from fastapi import HTTPException

    from app.api.v1 import chat as chat_api
    from app.services.file_storage import FileNotFoundInStorageError

    user_uuid = uuid.uuid4()
    file_att = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_uuid,
        conversation_id=None,
        assignment_run_id=None,
        filename="gone.png",
        content_type="image/png",
        size=5,
        s3_object_name="generated/u/gone.png",
    )
    db = _DownloadStubDB([file_att])
    user = SimpleNamespace(id=user_uuid, role="user")

    from app.services import file_storage as file_storage_module

    async def _fake_download(key):
        raise FileNotFoundInStorageError(key)

    monkeypatch.setattr(file_storage_module.file_storage, "download", _fake_download)

    with pytest.raises(HTTPException) as exc_info:
        await chat_api.download_file(str(file_att.id), user=user, db=db)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == chat_api.FILE_UNAVAILABLE_DETAIL


@pytest.mark.asyncio
async def test_download_file_still_maps_other_failures_to_500(monkeypatch):
    """A non-missing download failure must still surface as the existing
    ``500 File download failed`` — the friendly-404 path must not swallow
    genuine errors."""
    from fastapi import HTTPException

    from app.api.v1 import chat as chat_api

    user_uuid = uuid.uuid4()
    file_att = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_uuid,
        conversation_id=None,
        assignment_run_id=None,
        filename="boom.png",
        content_type="image/png",
        size=5,
        s3_object_name="generated/u/boom.png",
    )
    db = _DownloadStubDB([file_att])
    user = SimpleNamespace(id=user_uuid, role="user")

    from app.services import file_storage as file_storage_module

    async def _fake_download(_key):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(file_storage_module.file_storage, "download", _fake_download)

    with pytest.raises(HTTPException) as exc_info:
        await chat_api.download_file(str(file_att.id), user=user, db=db)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "File download failed"


@pytest.mark.asyncio
async def test_download_message_file_returns_friendly_404_when_storage_key_missing(
    monkeypatch,
):
    """The message-scoped download endpoint must mirror ``download_file``'s
    friendly ``404`` for a missing object key."""
    from fastapi import HTTPException

    from app.api.v1 import chat as chat_api
    from app.services.file_storage import FileNotFoundInStorageError

    user_uuid = uuid.uuid4()
    conv_uuid = uuid.uuid4()
    msg_uuid = uuid.uuid4()
    file_uuid = uuid.uuid4()

    conv = SimpleNamespace(id=conv_uuid, user_id=user_uuid)
    msg = SimpleNamespace(id=msg_uuid, conversation_id=conv_uuid)
    file_att = SimpleNamespace(
        id=file_uuid,
        user_id=user_uuid,
        conversation_id=conv_uuid,
        message_id=msg_uuid,
        filename="missing.csv",
        content_type="text/csv",
        size=12,
        s3_object_name="chat/conv/missing.csv",
    )

    db = _MsgDownloadStubDB([conv, msg, file_att])
    user = SimpleNamespace(id=user_uuid, role="user")

    from app.services import file_storage as file_storage_module

    async def _fake_download(key):
        raise FileNotFoundInStorageError(key)

    monkeypatch.setattr(file_storage_module.file_storage, "download", _fake_download)

    with pytest.raises(HTTPException) as exc_info:
        await chat_api.download_message_file(
            conversation_id=str(conv_uuid),
            message_id=str(msg_uuid),
            file_id=str(file_uuid),
            user=user,
            db=db,
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == chat_api.FILE_UNAVAILABLE_DETAIL


@pytest.mark.asyncio
async def test_get_file_public_returns_friendly_404_when_storage_key_missing(
    monkeypatch,
):
    """The public-token endpoint (`get_file_public`) is the third download path
    that maps a missing object key to the friendly ``404`` — pin its runtime
    behaviour too, not just the source-level guard."""
    from fastapi import HTTPException

    import app.core.security as security_module
    from app.api.v1 import chat as chat_api
    from app.services.file_storage import FileNotFoundInStorageError

    file_uuid = uuid.uuid4()
    file_att = SimpleNamespace(
        id=file_uuid,
        user_id=uuid.uuid4(),
        conversation_id=None,
        assignment_run_id=None,
        filename="gone.png",
        content_type="image/png",
        size=5,
        s3_object_name="generated/u/gone.png",
    )
    db = _DownloadStubDB([file_att])

    # get_file_public imports verify_image_token from app.core.security at call
    # time, so patch it on the source module; bypass the signed-token gate.
    monkeypatch.setattr(
        security_module, "verify_image_token", lambda *_a, **_k: True
    )

    from app.services import file_storage as file_storage_module

    async def _fake_download(key):
        raise FileNotFoundInStorageError(key)

    monkeypatch.setattr(file_storage_module.file_storage, "download", _fake_download)

    with pytest.raises(HTTPException) as exc_info:
        await chat_api.get_file_public(
            file_id=str(file_uuid), token="tok", expires=9999999999, db=db
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == chat_api.FILE_UNAVAILABLE_DETAIL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content_type", "public_inline", "session_inline"),
    [
        ("figure.png", "image/png", True, True),
        ("photo.jpg", "image/jpeg; charset=binary", True, True),
        ("founders-card.html", "text/html", False, False),
        ("mark.svg", "image/svg+xml", False, False),
        ("notes.txt", "text/plain", True, False),
        ("blob.bin", None, False, False),
    ],
)
async def test_the_public_file_route_never_renders_a_stored_document(
    monkeypatch, filename, content_type, public_inline, session_inline
):
    """The tokened public route is minted for ANY ``[[FILE:id]]`` a channel
    reply carries and needs no session, so a model-authored ``text/html``
    (``render_html_to_image``'s source) or an SVG served inline there
    is a page on the platform origin for whoever holds the link. It opens
    raster images and what a browser shows or plays itself (PDF, plain text,
    JSON, Markdown, CSV, audio, video...); the session route opens raster images
    only; everything else is a download; every answer is ``nosniff``."""
    import app.core.security as security_module
    from app.api.v1 import chat as chat_api

    file_uuid = uuid.uuid4()
    user_uuid = uuid.uuid4()
    file_att = SimpleNamespace(
        id=file_uuid,
        user_id=user_uuid,
        conversation_id=None,
        assignment_run_id=None,
        message_id=None,
        filename=filename,
        content_type=content_type,
        size=5,
        s3_object_name=f"generated/u/{filename}",
    )
    monkeypatch.setattr(security_module, "verify_image_token", lambda *_a, **_k: True)

    from app.services import file_storage as file_storage_module

    async def _fake_download(_key):
        return b"<script>alert(1)</script>"

    monkeypatch.setattr(file_storage_module.file_storage, "download", _fake_download)

    public = await chat_api.get_file_public(
        file_id=str(file_uuid), token="tok", expires=9999999999, db=_DownloadStubDB([file_att])
    )
    user = SimpleNamespace(id=user_uuid, role="user")
    private = await chat_api.download_file(file_id=str(file_uuid), user=user, db=_DownloadStubDB([file_att]))

    for response, inline in ((public, public_inline), (private, session_inline)):
        assert response.headers["x-content-type-options"] == "nosniff"
        disposition = response.headers["content-disposition"]
        if inline:
            assert disposition == "inline"
            assert "html" not in response.headers["content-type"]
        else:
            assert disposition.startswith("attachment;"), disposition
            assert filename in disposition
        # Never ``public``: the signed link's token is its only
        # authorisation, and no shared cache may keep a private file.
        assert response.headers["cache-control"] == "private, max-age=86400"
        assert response.headers["etag"]


def test_every_file_byte_route_answers_through_the_one_header_helper():
    """Source-level guard: the FileAttachment byte routes (public token,
    session download, message-scoped download, template asset download) build
    their headers through ``file_response_headers`` - a route that hand-rolls
    ``inline`` for ``startswith("image/")`` re-opens the SVG / HTML door."""
    from pathlib import Path

    api_dir = Path(__file__).parent.parent / "app" / "api" / "v1"
    src = (api_dir / "chat.py").read_text(encoding="utf-8")
    assert src.count("headers=_file_response_headers(file_att") == 3
    assert 'startswith("image/")' not in src
    templates_src = (api_dir / "template_containers.py").read_text(encoding="utf-8")
    assert templates_src.count("headers=file_response_headers(asset") == 1

    from app.api.v1 import chat as chat_api

    assert "image/svg+xml" not in chat_api._INLINE_FILE_CONTENT_TYPES
    assert "text/html" not in chat_api._INLINE_FILE_CONTENT_TYPES
    assert all(t.startswith("image/") for t in chat_api._INLINE_FILE_CONTENT_TYPES)


def test_chat_download_endpoints_decoupled_from_minio_error():
    """Source-level guard: ``chat.py`` must detect missing files via the
    storage-layer ``FileNotFoundInStorageError`` rather than importing
    ``minio.error.S3Error`` and sniffing error codes. This keeps the API
    decoupled from the object-store backend and ensures all download paths
    share one missing-key contract."""
    from pathlib import Path

    src = (
        Path(__file__).parent.parent / "app" / "api" / "v1" / "chat.py"
    ).read_text(encoding="utf-8")
    assert "FileNotFoundInStorageError" in src, (
        "chat.py must catch ``FileNotFoundInStorageError`` from the storage "
        "layer to map missing keys to a friendly 404."
    )
    assert "from minio" not in src and "import minio" not in src, (
        "chat.py must NOT import minio directly — missing-key detection "
        "belongs in ``file_storage`` (FileNotFoundInStorageError)."
    )
    # All three download paths share the same user-facing detail constant.
    assert src.count("FILE_UNAVAILABLE_DETAIL") >= 4, (
        "download_file, download_message_file and get_file_public must all "
        "return FILE_UNAVAILABLE_DETAIL (1 definition + 3 usages)."
    )


# ---------------------------------------------------------------------------
# Custom tool unresolved {{var_id.UUID}} placeholder detection
# ---------------------------------------------------------------------------

_EMPTY_SCHEMA = {"type": "object", "properties": {}}


@pytest.mark.asyncio
async def test_custom_tool_unresolved_placeholder_fails_fast_before_execution(monkeypatch):
    """When a custom tool's function code still contains an unresolved
    ``{{var_id.UUID}}`` placeholder after variable substitution (e.g. the
    referenced variable doesn't exist), calling the tool must return an
    immediate error string rather than attempting execution with the literal
    placeholder as a credential (which would cause a 401 / 300 s timeout).
    """
    var_uuid = "12345678-1234-1234-1234-123456789012"
    placeholder = "{{var_id." + var_uuid + "}}"
    # The function code still contains the placeholder (twice, to exercise
    # dedup) — substitution produced no match (variable doesn't exist for
    # this user).
    tool_id = uuid.uuid4()
    fake_tool = SimpleNamespace(
        id=tool_id,
        name="renovate_tool",
        js_function=(
            "const token = '" + placeholder + "'; "
            "const retry = '" + placeholder + "'; /* unresolved */"
        ),
        input_schema=_EMPTY_SCHEMA,
        enabled=True,
        language="javascript",
        timeout=10,
        description="Renovate PR tool",
    )
    db = _FakeDB([fake_tool])
    composite = _FakeComposite()

    # Track whether execute_tool_auto was called (it must NOT be).
    executed: list[bool] = []

    async def _should_not_be_called(*_args, **_kwargs):
        executed.append(True)
        return "should not reach here"

    monkeypatch.setattr(
        "app.services.custom_tool_executor.execute_tool_auto", _should_not_be_called
    )

    # No variables provided — the placeholder is left unresolved.
    await load_custom_tools(db, [tool_id], composite, variables={})
    assert composite.executor is not None

    composite.executor.file_context = FileContext()
    result = await composite.executor.execute_tool(
        ToolCall(id="call-unresolved-1", name="renovate_tool", arguments={})
    )

    # Must return an error string mentioning the placeholder — not execute the code.
    content = result.content if hasattr(result, "content") else str(result)
    assert "unresolved" in content.lower(), f"Expected 'unresolved' in error, got: {content!r}"
    assert "var_id" in content or "variable" in content.lower(), (
        f"Expected placeholder info in error, got: {content!r}"
    )
    # The same placeholder referenced twice must be reported once (deduped).
    assert "1 unresolved" in content, f"Expected deduped count of 1, got: {content!r}"
    assert executed == [], "execute_tool_auto must NOT be called when placeholders remain unresolved"


@pytest.mark.asyncio
async def test_custom_tool_resolved_placeholder_executes_normally(monkeypatch):
    """When all ``{{var_id.UUID}}`` placeholders are resolved by variable
    substitution, the tool must execute without any placeholder-guard error."""
    resolved_token = "ghp_real_token_here"
    tool_id = uuid.uuid4()
    fake_tool = SimpleNamespace(
        id=tool_id,
        name="resolved_tool",
        # Simulate the tool after substitution: placeholder already resolved.
        js_function="const token = '" + resolved_token + "'; return token;",
        input_schema=_EMPTY_SCHEMA,
        enabled=True,
        language="javascript",
        timeout=10,
        description="Resolved tool",
    )
    db = _FakeDB([fake_tool])
    composite = _FakeComposite()

    async def _fake_exec(*_args, **_kwargs):
        return resolved_token

    monkeypatch.setattr(
        "app.services.custom_tool_executor.execute_tool_auto", _fake_exec
    )

    await load_custom_tools(db, [tool_id], composite)
    assert composite.executor is not None

    composite.executor.file_context = FileContext()
    result = await composite.executor.execute_tool(
        ToolCall(id="call-resolved-1", name="resolved_tool", arguments={})
    )

    content = result.content if hasattr(result, "content") else str(result)
    assert "unresolved" not in content.lower(), (
        f"Resolved tool must not trigger placeholder guard; got: {content!r}"
    )
