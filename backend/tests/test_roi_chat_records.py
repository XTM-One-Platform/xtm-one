"""Recorder of chat deliverables for ROI (#10544, slice S2): ``services/roi_chat_records.py``.

The statements are compiled against the PostgreSQL dialect and read as SQL;
the real round trip is ``tests/test_roi_chat_records_postgres.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.services import roi_chat_records as rec


class _Session:
    def __init__(self, fail: bool = False):
        self.statements: list = []
        self.fail = fail

    @asynccontextmanager
    async def begin_nested(self):
        yield self

    async def execute(self, statement, *a, **k):
        if self.fail:
            raise RuntimeError("boom")
        self.statements.append(statement)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))


_AT = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


def test_a_deliverable_saved_in_a_conversation_is_upserted_on_its_own_id():
    session = _Session()
    deck = uuid.uuid4()
    ok = asyncio.run(
        rec.record_deliverable(
            session,
            kind="presentation",
            deliverable_id=deck,
            conversation_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            title="All Hands",
            size=30,
            at=_AT,
        )
    )
    assert ok
    (statement,) = session.statements
    sql = _sql(statement)
    assert "INSERT INTO roi_chat_records" in sql
    assert "ON CONFLICT (record_key) DO UPDATE" in sql
    # The agent is the conversation's, read in the same statement.
    assert "FROM conversations" in sql
    params = statement.compile(dialect=postgresql.dialect()).params
    assert params["record_key"] == f"presentation:{deck}"
    assert params["size"] == 30.0


def test_no_conversation_no_record():
    """A deliverable written by an assignment run is automation, counted elsewhere."""
    session = _Session()
    ok = asyncio.run(
        rec.record_deliverable(
            session, kind="report", deliverable_id=uuid.uuid4(), conversation_id=None, user_id=None, title="t", size=3
        )
    )
    assert not ok and session.statements == []


def test_an_unknown_kind_or_signal_writes_nothing():
    session = _Session()
    assert not asyncio.run(
        rec.record_deliverable(
            session, kind="email", deliverable_id="x", conversation_id=uuid.uuid4(), user_id=None, title="", size=1
        )
    )
    assert not asyncio.run(rec.record_signal(session, kind="presentation", deliverable_id="x", signal="viewed"))
    assert session.statements == []


def test_a_signal_is_appended_once_and_stamps_the_first():
    session = _Session()
    assert asyncio.run(rec.record_signal(session, kind="video", deliverable_id="v1", signal="public_link", at=_AT))
    sql = _sql(session.statements[0])
    assert sql.startswith("UPDATE roi_chat_records")
    assert "@>" in sql, "an already-seen signal is not appended twice"
    assert "coalesce(roi_chat_records.first_signal_at" in sql


def test_the_first_deletion_wins():
    session = _Session()
    assert asyncio.run(rec.record_deleted(session, kind="file", deliverable_id="f1", at=_AT))
    assert "roi_chat_records.deleted_at IS NULL" in _sql(session.statements[0])


def test_bookkeeping_never_fails_the_observed_write():
    session = _Session(fail=True)
    ok = asyncio.run(
        rec.record_deliverable(
            session,
            kind="presentation",
            deliverable_id="d",
            conversation_id=uuid.uuid4(),
            user_id=None,
            title="",
            size=1,
        )
    )
    assert ok is False


def test_sizes_stay_on_the_low_side():
    assert rec.report_pages(0) == 0.0
    assert rec.report_pages(3) == 1.0
    assert rec.report_pages(12) == 2.0
    assert rec.video_minutes(90_000) == 1.5
    assert rec.video_minutes(None) == 0.0


def test_the_writers_call_the_recorder():
    """Presentations, reports and videos record on save; public links record a use."""
    import inspect

    from app.services import presentation_public_links, presentations, reports
    from app.services.video_studio import projects, public_links

    assert "record_deliverable(" in inspect.getsource(presentations.save_presentation)
    assert "record_deliverable(" in inspect.getsource(reports.save_report)
    assert "record_deliverable(" in inspect.getsource(projects)
    assert 'signal="public_link"' in inspect.getsource(presentation_public_links.enable_public_link)
    assert 'signal="public_link"' in inspect.getsource(public_links.enable_public_link)


def test_exports_record_a_use_and_deletions_are_stamped():
    """Slice S2b: UI and agent exports record ``exported``; deck and video deletions stamp the row."""
    import inspect

    from app.api.v1 import presentations as presentations_api
    from app.api.v1 import reports as reports_api
    from app.services import presentations
    from app.services.builtin_tools import presentation as presentation_tools
    from app.services.builtin_tools import report as report_tools
    from app.services.video_studio import deletion

    for module in (presentations_api, reports_api, presentation_tools, report_tools):
        source = inspect.getsource(module)
        assert "record_signal_detached(" in source and 'signal="exported"' in source, module.__name__
    assert "record_deleted(" in inspect.getsource(presentations.remove_presentation)
    assert "record_deleted(" in inspect.getsource(deletion.delete_video)


def test_a_detached_signal_with_an_unknown_kind_or_signal_opens_no_session():
    assert not asyncio.run(rec.record_signal_detached(kind="email", deliverable_id="x", signal="exported"))
    assert not asyncio.run(rec.record_signal_detached(kind="report", deliverable_id="x", signal="viewed"))
    assert not asyncio.run(rec.record_signal_detached(kind="report", deliverable_id=None, signal="exported"))


def test_a_detached_signal_never_fails_the_export(monkeypatch):
    import app.core.database as database

    def _broken():
        raise RuntimeError("no pool")

    monkeypatch.setattr(database, "async_session", _broken)
    assert asyncio.run(rec.record_signal_detached(kind="report", deliverable_id="r1", signal="exported")) is False


# ── Slice S2c: shared, presented, attached; generated document files ─────────


class _Result:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _DetachedSession(_Session):
    """A session of its own: ``async with``, ``execute`` answering a rowcount, ``commit``."""

    def __init__(self, moved: int = 0):
        super().__init__()
        self.moved = moved
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        return _Result(self.moved if len(self.statements) == 1 else 1)

    async def commit(self):
        self.committed = True


def _detached(monkeypatch, session):
    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: session)
    return session


_PDF = "application/pdf"
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_document_pages_stay_on_the_low_side():
    assert rec.document_pages(None) == 1.0, "bytes handed in rendered count one page"
    assert rec.document_pages(500) == 1.0
    assert rec.document_pages(5_999) == 1.0
    assert rec.document_pages(9_000) == 3.0


def test_a_generated_document_is_recorded_once_on_its_file_id(monkeypatch):
    session = _detached(monkeypatch, _DetachedSession(moved=0))
    fid = uuid.uuid4()
    ok = asyncio.run(
        rec.record_document_file(
            file_id=fid,
            conversation_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            filename="brief.pdf",
            content_type=_PDF,
            source_chars=9_000,
        )
    )
    assert ok and session.committed
    move, insert = session.statements
    assert _sql(move).startswith("UPDATE roi_chat_records"), "a revision of the same name moves first"
    assert "roi_chat_records.title" in _sql(move)
    assert "INSERT INTO roi_chat_records" in _sql(insert)
    params = insert.compile(dialect=postgresql.dialect()).params
    assert params["record_key"] == f"file:{fid}"
    assert params["size"] == 3.0


def test_a_re_render_under_the_same_name_moves_the_record_instead_of_counting_twice(monkeypatch):
    session = _detached(monkeypatch, _DetachedSession(moved=1))
    fid = uuid.uuid4()
    ok = asyncio.run(
        rec.record_document_file(
            file_id=fid,
            conversation_id=uuid.uuid4(),
            user_id=None,
            filename="brief.docx",
            content_type=_DOCX,
            source_chars=100,
        )
    )
    assert ok
    (move,) = session.statements
    assert move.compile(dialect=postgresql.dialect()).params["record_key"] == f"file:{fid}"


def test_a_file_that_is_no_document_or_has_no_conversation_opens_no_session(monkeypatch):
    def _never():
        raise AssertionError("no session for a file that is not a deliverable")

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", _never)
    common = {"user_id": None, "filename": "x", "source_chars": 10}
    assert not asyncio.run(
        rec.record_document_file(file_id=uuid.uuid4(), conversation_id=uuid.uuid4(), content_type="text/csv", **common)
    )
    assert not asyncio.run(
        rec.record_document_file(file_id=uuid.uuid4(), conversation_id=None, content_type=_PDF, **common)
    )
    assert not asyncio.run(
        rec.record_document_file(file_id="not-a-uuid", conversation_id=uuid.uuid4(), content_type=_PDF, **common)
    )
    assert asyncio.run(rec.record_attached([(str(uuid.uuid4()), "image/png"), ("file-abc", _PDF)])) == 0


def test_a_document_sent_or_uploaded_records_attached_once(monkeypatch):
    seen: list[tuple[str, str, str]] = []

    async def _signal(*, kind, deliverable_id, signal):
        seen.append((kind, deliverable_id, signal))
        return True

    monkeypatch.setattr(rec, "record_signal_detached", _signal)
    fid = str(uuid.uuid4())
    marked = asyncio.run(rec.record_attached([(fid, _PDF), (f"file-{fid}", "application/pdf; charset=binary")]))
    assert marked == 1
    assert seen == [("file", fid, "attached")]


def test_sharing_records_a_use_only_when_someone_gains_access():
    from app.models.presentation import Presentation
    from app.models.video_studio import VideoProject

    deck = Presentation(id=uuid.uuid4())
    video = VideoProject(id=uuid.uuid4())
    session = _Session()
    assert not asyncio.run(rec.record_sharing(session, deck, {"added_users": [], "removed_users": ["u"]}))
    assert not asyncio.run(rec.record_sharing(session, object(), {"added_users": ["u"]}))
    assert session.statements == []
    assert asyncio.run(rec.record_sharing(session, deck, {"added_users": ["u"]}))
    assert asyncio.run(rec.record_sharing(session, video, {"added_groups": ["company"]}))
    keys = [s.compile(dialect=postgresql.dialect()).params["record_key_1"] for s in session.statements]
    assert keys == [f"presentation:{deck.id}", f"video:{video.id}"]


def test_the_s2c_writers_call_the_recorder():
    """Grant writes record ``shared``, poll control ``presented``, generate_file the document, outbound files ``attached``."""
    import inspect

    from app.api.v1 import presentations as presentations_api
    from app.services import presentation_sharing
    from app.services.builtin_tools import media
    from app.tools.base import FileContext

    assert "record_sharing(" in inspect.getsource(presentation_sharing.apply_sharing)
    assert "SIGNAL_PRESENTED" in inspect.getsource(presentations_api.control_presentation_poll)
    assert "record_document_file(" in inspect.getsource(media.build_generate_file_tool)
    assert "record_attached(" in inspect.getsource(FileContext.resolve_upload)
    assert "resolve_upload(fid, outbound=False)" in inspect.getsource(media._inline_embedded_file_refs)
