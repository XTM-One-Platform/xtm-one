"""Conversation-scoped dedup of tool-produced files (trace 722375f1, 2026-09-24).

``_persist_generated_file`` is the single persistence path for every file a
tool downloads or generates. Persisting bytes whose SHA-256 already exists as a
tool-produced row in the SAME conversation must return that row instead of
creating another row and another S3 object: the incident turn persisted 84
byte-identical PNGs that are now 3. User uploads are excluded, another
conversation is another file, and a failing lookup never blocks persistence.
"""

from __future__ import annotations

import uuid

import pytest

from app.services import builtin_tools
from app.services import file_storage as fs_module
from app.services.builtin_tools import _shared
from app.services.builtin_tools._shared import (
    TOOL_FILE_DEDUP_SOURCE_PREFIX,
    file_sha256,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 40


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDedupSession:
    """In-memory stand-in for the short-lived persistence session.

    ``execute`` answers the dedup ``select`` from the rows added so far, using
    the statement's own bound parameters so the predicate the production
    query carries (conversation, digest, exact source, no assignment run, no
    message, the name when asked) is what is tested — not a re-implementation
    of it.
    """

    def __init__(self, store: list, *, fail_lookup: bool = False):
        self._store = store
        self._fail_lookup = fail_lookup
        self.lookups = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def add(self, obj):
        self._store.append(obj)

    async def commit(self):
        return None

    async def execute(self, stmt):
        self.lookups += 1
        if self._fail_lookup:
            raise RuntimeError("database unreachable")
        compiled = stmt.compile()
        params = compiled.params
        conv = params.get("conversation_id_1")
        digest = params.get("sha256_1")
        source = params.get("source_1")
        filename = params.get("filename_1")
        # ``IS NULL`` binds no parameter; assert the predicates are in the
        # statement instead of guessing at them.
        assert "assignment_run_id IS NULL" in str(compiled)
        assert "message_id IS NULL" in str(compiled)
        matches = [
            r
            for r in self._store
            if r.conversation_id == conv
            and r.sha256 == digest
            and r.source == source
            and getattr(r, "assignment_run_id", None) is None
            and getattr(r, "message_id", None) is None
            and (filename is None or r.filename == filename)
        ]
        return _FakeResult(matches[:1])


@pytest.fixture
def persistence(monkeypatch):
    """Patch S3 + the session seam; return (store, uploads, sessions)."""
    store: list = []
    uploads: list[str] = []
    sessions: list[_FakeDedupSession] = []

    async def _fake_upload(*, data, filename, content_type, prefix):
        uploads.append(filename)
        return f"{prefix}/{uuid.uuid4().hex}-{filename}"

    monkeypatch.setattr(fs_module.file_storage, "upload", _fake_upload, raising=False)

    def _session():
        s = _FakeDedupSession(store)
        sessions.append(s)
        return s

    monkeypatch.setattr(_shared, "_own_session", _session)
    return store, uploads, sessions


async def _persist(
    conversation_id,
    *,
    data=_PNG,
    filename="diagram.png",
    source="agent_download",
    assignment_run_id=None,
    match_filename=False,
):
    return await builtin_tools._persist_generated_file(
        db=None,
        user_id=str(uuid.uuid4()),
        filename=filename,
        raw_bytes=data,
        content_type="image/png",
        conversation_id=conversation_id,
        assignment_run_id=assignment_run_id,
        source=source,
        match_filename=match_filename,
    )


@pytest.mark.asyncio
async def test_same_bytes_twice_in_one_conversation_yield_one_file(persistence):
    store, uploads, _sessions = persistence
    conv = str(uuid.uuid4())

    first = await _persist(conv, filename="platform-overview-v1.png")
    # The incident retry: same bytes, cosmetic rename.
    second = await _persist(conv, filename="platform-overview-v1b.png")

    assert second is first
    assert second.id == first.id
    assert len(store) == 1, "no second row"
    assert uploads == ["platform-overview-v1.png"], "no second S3 object"
    assert first.sha256 == file_sha256(_PNG)


@pytest.mark.asyncio
async def test_same_bytes_in_another_conversation_yield_a_new_file(persistence):
    store, uploads, _sessions = persistence

    first = await _persist(str(uuid.uuid4()))
    second = await _persist(str(uuid.uuid4()))

    assert second is not first
    assert second.id != first.id
    assert len(store) == 2
    assert len(uploads) == 2


@pytest.mark.asyncio
async def test_different_bytes_in_one_conversation_are_different_files(persistence):
    store, _uploads, _sessions = persistence
    conv = str(uuid.uuid4())

    first = await _persist(conv, data=_PNG)
    second = await _persist(conv, data=_PNG + b"!")

    assert second.id != first.id
    assert len(store) == 2


@pytest.mark.asyncio
async def test_user_uploads_are_never_reused_and_never_matched(persistence):
    """A user upload with the same bytes in the same conversation is not the
    tool's file: it is not matched, and a write under a user-upload source
    does not look for duplicates at all."""
    store, _uploads, sessions = persistence
    conv = str(uuid.uuid4())

    upload = await _persist(conv, source="chat")
    assert upload.sha256 == file_sha256(_PNG)
    assert sum(s.lookups for s in sessions) == 0, "a user upload never looks up"
    assert not "chat".startswith(TOOL_FILE_DEDUP_SOURCE_PREFIX)

    tool_file = await _persist(conv, source="agent_download")

    assert tool_file.id != upload.id, "the tool's file is not the user's upload"
    assert len(store) == 2


@pytest.mark.asyncio
async def test_same_bytes_under_another_agent_source_are_another_file(persistence):
    """``source`` decides how the file is shown (``agent_working`` is the
    compact working-file chip, a download is a download card), so a working
    file is never answered with the download row that happens to hold the
    same bytes - and vice versa. Both still dedup against their own kind."""
    store, uploads, _sessions = persistence
    conv = str(uuid.uuid4())

    download = await _persist(conv, source="agent_download")
    working = await _persist(conv, source="agent_working")
    assert working.id != download.id
    assert len(store) == 2 and len(uploads) == 2

    assert (await _persist(conv, source="agent_working")) is working
    assert (await _persist(conv, source="agent_download")) is download
    assert len(store) == 2, "each source converges on its own row"


@pytest.mark.asyncio
async def test_a_row_bound_to_an_assignment_run_is_never_reused(persistence):
    """A row that is a run's output is re-pointed by the assignment engine to
    whichever run's FileContext carries it, so handing it to another producer
    would re-home it. Only a run-unbound row qualifies."""
    store, _uploads, _sessions = persistence
    conv = str(uuid.uuid4())

    bound = await _persist(conv, assignment_run_id=str(uuid.uuid4()))
    assert bound.assignment_run_id is not None

    again = await _persist(conv)

    assert again.id != bound.id
    assert again.assignment_run_id is None
    assert len(store) == 2
    # The unbound row is what every later duplicate converges on.
    assert (await _persist(conv)) is again


@pytest.mark.asyncio
async def test_a_row_an_earlier_answer_shows_is_never_reused(persistence):
    """A row is shown by one message at most: once an answer carries it, the
    same bytes written in a later turn are a row of their own, which that
    turn's answer can carry (the embedded chat and the widget show a reply's
    files from that reply alone, so a reused row showed nothing)."""
    store, uploads, _sessions = persistence
    conv = str(uuid.uuid4())

    shown = await _persist(conv, filename="report.md")
    shown.message_id = uuid.uuid4()

    again = await _persist(conv, filename="report.md")

    assert again.id != shown.id
    assert getattr(again, "message_id", None) is None
    assert len(store) == 2 and len(uploads) == 2
    assert (await _persist(conv, filename="report.md")) is again


@pytest.mark.asyncio
async def test_a_named_write_reuses_only_a_row_of_that_name(persistence):
    """``generate_file`` names the file the user gets: the same bytes under
    another name are another file, the same name is still one."""
    store, uploads, _sessions = persistence
    conv = str(uuid.uuid4())

    fetched = await _persist(conv, filename="export-123.csv")
    named = await _persist(conv, filename="leak-analysis.csv", match_filename=True)

    assert named.id != fetched.id
    assert named.filename == "leak-analysis.csv"
    assert uploads == ["export-123.csv", "leak-analysis.csv"]
    assert (await _persist(conv, filename="leak-analysis.csv", match_filename=True)) is named
    assert len(store) == 2


@pytest.mark.asyncio
async def test_no_conversation_scope_persists_without_a_lookup(persistence):
    store, _uploads, sessions = persistence

    first = await _persist(None)
    second = await _persist(None)

    assert second.id != first.id
    assert len(store) == 2
    assert sum(s.lookups for s in sessions) == 0


@pytest.mark.asyncio
async def test_lookup_failure_fails_open(monkeypatch):
    """Dedup is a saving, never a reason not to persist: a lookup error is
    logged and the file is written as before."""
    store: list = []
    uploads: list[str] = []

    async def _fake_upload(*, data, filename, content_type, prefix):
        uploads.append(filename)
        return f"{prefix}/{filename}"

    monkeypatch.setattr(fs_module.file_storage, "upload", _fake_upload, raising=False)
    monkeypatch.setattr(
        _shared, "_own_session", lambda: _FakeDedupSession(store, fail_lookup=True)
    )

    row = await _persist(str(uuid.uuid4()))

    assert row in store
    assert uploads == ["diagram.png"]
