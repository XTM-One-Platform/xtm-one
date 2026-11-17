"""A deletion cancels its documents' queued jobs once it commits, never before.

``cancel_document_jobs_after_commit`` registers the documents a transaction
deletes; the cancellation starts when the root transaction commits and is
dropped with any transaction that ends without committing. A real ``Session``
against in-memory SQLite, because what is under test is the transaction
lifecycle - commit, rollback, SAVEPOINT release and rollback, a COMMIT that
fails, close - and a double cannot say when SQLAlchemy fires which event. The
callers against a real PostgreSQL are in
``test_document_job_cancel_after_commit_postgres.py``.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import ForeignKey, String, create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app import worker as worker_module
from app.services import kb_helpers


class _Base(DeclarativeBase):
    pass


class _Row(_Base):
    __tablename__ = "pr4842_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)


class _Parent(_Base):
    __tablename__ = "pr4842_parents"
    id: Mapped[int] = mapped_column(primary_key=True)


class _Child(_Base):
    """A DEFERRED foreign key: the flush passes and the COMMIT is refused."""

    __tablename__ = "pr4842_children"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("pr4842_parents.id", deferrable=True, initially="DEFERRED"))


@contextmanager
def _sqlite_session() -> Iterator[Session]:
    engine = create_engine("sqlite://")

    # pysqlite's own transaction handling breaks SAVEPOINT; the documented
    # SQLAlchemy recipe hands transaction control back to the engine.
    @event.listens_for(engine, "connect")
    def _connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")

    _Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


@pytest.fixture
def cancelled(monkeypatch) -> list[set[str]]:
    """Every cancellation a commit started, with the document ids it was given."""
    calls: list[set[str]] = []

    async def _record(doc_ids) -> int:
        calls.append(set(doc_ids))
        return 0

    monkeypatch.setattr(kb_helpers, "cancel_document_processing_jobs", _record)
    return calls


def _in_flight_here() -> list[asyncio.Task]:
    loop = asyncio.get_running_loop()
    return [task for task in kb_helpers._cancellations_in_flight if task.get_loop() is loop]


async def _settled() -> None:
    await asyncio.gather(*_in_flight_here())


def _write(session: Session) -> None:
    session.add(_Row(name=uuid.uuid4().hex))
    session.flush()


@pytest.mark.asyncio
async def test_a_commit_cancels_exactly_the_documents_its_transaction_deleted(cancelled):
    with _sqlite_session() as session:
        _write(session)
        kb_helpers.cancel_document_jobs_after_commit(session, ["a", "b"])
        kb_helpers.cancel_document_jobs_after_commit(session, [uuid.UUID(int=1), "b"])
        await _settled()
        assert cancelled == [], "nothing may be cancelled before the commit"

        session.commit()
        await _settled()

    assert cancelled == [{"a", "b", str(uuid.UUID(int=1))}]


@pytest.mark.asyncio
async def test_a_rollback_cancels_nothing(cancelled):
    with _sqlite_session() as session:
        _write(session)
        kb_helpers.cancel_document_jobs_after_commit(session, ["a"])
        session.rollback()
        session.commit()
        await _settled()

    assert cancelled == []


@pytest.mark.asyncio
async def test_a_failed_commit_cancels_nothing_and_the_next_commit_only_what_it_deleted(cancelled):
    with _sqlite_session() as session:
        session.add(_Child(parent_id=4842))
        session.flush()
        kb_helpers.cancel_document_jobs_after_commit(session, ["kept"])
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        await _settled()
        assert cancelled == []

        _write(session)
        kb_helpers.cancel_document_jobs_after_commit(session, ["deleted"])
        session.commit()
        await _settled()

    assert cancelled == [{"deleted"}]


@pytest.mark.asyncio
async def test_a_session_closed_without_committing_cancels_nothing(cancelled):
    with _sqlite_session() as session:
        _write(session)
        kb_helpers.cancel_document_jobs_after_commit(session, ["a"])
        session.close()

        _write(session)
        session.commit()
        await _settled()

    assert cancelled == []


@pytest.mark.asyncio
async def test_a_rolled_back_savepoint_drops_its_documents_and_keeps_the_enclosing_ones(cancelled):
    with _sqlite_session() as session:
        _write(session)
        kb_helpers.cancel_document_jobs_after_commit(session, ["outer"])
        savepoint = session.begin_nested()
        _write(session)
        kb_helpers.cancel_document_jobs_after_commit(session, ["inner"])
        savepoint.rollback()
        session.commit()
        await _settled()

    assert cancelled == [{"outer"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("outer", ["commit", "rollback"])
async def test_a_released_savepoint_hands_its_documents_to_the_enclosing_transaction(cancelled, outer):
    with _sqlite_session() as session:
        _write(session)
        with session.begin_nested():
            _write(session)
            kb_helpers.cancel_document_jobs_after_commit(session, ["inner"])
        await _settled()
        assert cancelled == [], "a SAVEPOINT release is not the durable commit"

        getattr(session, outer)()
        await _settled()

    assert cancelled == ([{"inner"}] if outer == "commit" else [])


@pytest.mark.asyncio
async def test_a_released_savepoint_inside_a_rolled_back_one_cancels_nothing(cancelled):
    with _sqlite_session() as session:
        _write(session)
        outer = session.begin_nested()
        with session.begin_nested():
            _write(session)
            kb_helpers.cancel_document_jobs_after_commit(session, ["inner"])
        outer.rollback()
        session.commit()
        await _settled()

    assert cancelled == []


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["commit", "rollback"])
async def test_ids_registered_before_any_statement_belong_to_the_transaction_that_deletes_them(cancelled, end):
    with _sqlite_session() as session:
        kb_helpers.cancel_document_jobs_after_commit(session, ["a"])
        _write(session)
        getattr(session, end)()
        await _settled()

    assert cancelled == ([{"a"}] if end == "commit" else [])


@pytest.mark.asyncio
async def test_no_document_registers_nothing(cancelled):
    with _sqlite_session() as session:
        kb_helpers.cancel_document_jobs_after_commit(session, [])
        kb_helpers.cancel_document_jobs_after_commit(session, [None, ""])
        assert kb_helpers._JOBS_TO_CANCEL_ON_COMMIT not in session.info
        _write(session)
        session.commit()
        await _settled()

    assert cancelled == []


@pytest.mark.asyncio
async def test_the_commit_never_waits_on_a_queue_that_does_not_answer(monkeypatch, caplog):
    """The cancellation runs beside the commit, bounded like every enqueue."""

    async def _never_answers(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_module, "queue", SimpleNamespace(jobs=_never_answers, abort=_never_answers))
    monkeypatch.setattr(kb_helpers, "_ENQUEUE_TIMEOUT_SECONDS", 0.05)
    caplog.set_level(logging.WARNING, logger=kb_helpers.logger.name)

    with _sqlite_session() as session:
        _write(session)
        kb_helpers.cancel_document_jobs_after_commit(session, ["a"])
        session.commit()
        [cancellation] = _in_flight_here()
        assert not cancellation.done()

    assert await asyncio.wait_for(cancellation, 5) == 0
    assert any("Failed to cancel queued document jobs for 1" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_deferral_never_fails_a_deletion(cancelled, caplog):
    caplog.set_level(logging.WARNING, logger=kb_helpers.logger.name)

    kb_helpers.cancel_document_jobs_after_commit(object(), ["a"])

    assert any("Could not defer the cancellation" in r.getMessage() for r in caplog.records)
    assert cancelled == []


# The deletions of knowledge documents that cancel their queued jobs, by module
# and function: each registers them through the after-commit helper.
_DELETIONS = {
    "app/api/v1/knowledge_bases.py": {"delete_knowledge_base", "delete_document", "force_resync_source"},
    "app/services/agent_deletion.py": {"_delete_system_knowledge_bases"},
    "app/api/v1/agent_skills.py": {"_cleanup_package_origin_entities"},
    "app/services/builtin_tools/agent_builder.py": {"_delete_knowledge_base", "_delete_kb_document"},
    "app/services/kb_sync.py": {"_delete_stale_documents"},
}

_BACKEND = Path(__file__).resolve().parents[1]


def _called_names(node: ast.AST) -> set[str]:
    return {
        getattr(call.func, "id", None) or getattr(call.func, "attr", None)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
    }


def test_every_deletion_cancels_document_jobs_through_the_after_commit_helper():
    """A direct call cancels before, or regardless of, the commit: only the helper may make it."""
    direct = []
    for path in sorted((_BACKEND / "app").rglob("*.py")):
        relative = path.relative_to(_BACKEND).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if "cancel_document_processing_jobs" not in _called_names(function):
                continue
            if (relative, function.name) != ("app/services/kb_helpers.py", "_cancel_committed_document_jobs"):
                direct.append(f"{relative}:{function.name}")
        if relative in _DELETIONS:
            callers = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and "cancel_document_jobs_after_commit" in _called_names(node)
            }
            missing = _DELETIONS[relative] - callers
            assert not missing, f"{relative}: {sorted(missing)} no longer cancel through the helper"

    # Nested functions are walked twice (as themselves and inside their parent).
    assert sorted(set(direct)) == [], f"direct cancellation outside the after-commit helper: {direct}"
