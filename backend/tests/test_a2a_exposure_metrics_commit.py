"""The exposure counter counts durable changes, not flushed intentions.

Every exposure change is decided inside a flush listener, before COMMIT, and a
Prometheus counter is monotonic: an increment for a change that a failed flush,
a rollback or a caller that never commits then discards can never be taken
back. So the hooks queue the increment and emit it on the commit that makes
the change durable.

A real ``Session`` against in-memory SQLite, because what is under test is the
transaction lifecycle - commit, rollback, SAVEPOINT release and rollback, a
flush or a COMMIT that fails, close - and a double cannot say when SQLAlchemy
fires which event. Each test counts under a label value of its own, so the
process-wide registry never leaks one test's increments into another.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import ForeignKey, String, create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.a2a import hooks


class _Base(DeclarativeBase):
    pass


class _Row(_Base):
    """A unique column, so a flush can be made to fail on demand."""

    __tablename__ = "pr3117_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)


class _Parent(_Base):
    __tablename__ = "pr3117_parents"
    id: Mapped[int] = mapped_column(primary_key=True)


class _Child(_Base):
    """A DEFERRED foreign key: the flush passes and the COMMIT is refused."""

    __tablename__ = "pr3117_children"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(
        ForeignKey("pr3117_parents.id", deferrable=True, initially="DEFERRED")
    )


def _count(reason: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "a2a_exposure_changes_total", {"action": "expose", "reason": reason}
        )
        or 0.0
    )


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
    hooks.register_a2a_hooks()
    try:
        with Session(engine) as s:
            yield s
    finally:
        engine.dispose()


@pytest.fixture
def session():
    with _sqlite_session() as s:
        yield s


@pytest.fixture
def reason() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def test_a_committed_change_is_counted_once_at_commit(session, reason):
    session.connection()
    hooks._queue_exposure_metric(session, "expose", reason)
    assert _count(reason) == 0, "counted before the transaction committed"
    session.commit()
    assert _count(reason) == 1
    session.commit()
    assert _count(reason) == 1, "a later commit counted it again"


def test_a_rolled_back_change_is_never_counted(session, reason):
    session.connection()
    hooks._queue_exposure_metric(session, "expose", reason)
    session.rollback()
    session.connection()
    session.commit()
    assert _count(reason) == 0


def test_a_transaction_closed_without_commit_is_never_counted(session, reason):
    """``close()`` fires neither ``after_commit`` nor ``after_soft_rollback``,
    and ``session.info`` survives it - a reused session must not report the
    discarded change at its next commit."""
    session.connection()
    hooks._queue_exposure_metric(session, "expose", reason)
    session.close()
    session.connection()
    session.commit()
    assert _count(reason) == 0


def test_a_savepoint_rollback_discards_only_what_it_decided(session, reason):
    session.connection()
    hooks._queue_exposure_metric(session, "expose", reason)
    nested = session.begin_nested()
    hooks._queue_exposure_metric(session, "expose", reason)
    nested.rollback()
    session.commit()
    assert _count(reason) == 1


def test_a_savepoint_release_is_not_the_durable_commit(session, reason):
    session.connection()
    nested = session.begin_nested()
    hooks._queue_exposure_metric(session, "expose", reason)
    nested.commit()
    assert _count(reason) == 0, "counted at the SAVEPOINT release"
    session.rollback()
    session.connection()
    session.commit()
    assert _count(reason) == 0, "the outer rollback discarded it"


def test_a_released_savepoint_is_counted_at_the_outer_commit(session, reason):
    session.connection()
    nested = session.begin_nested()
    hooks._queue_exposure_metric(session, "expose", reason)
    nested.commit()
    session.commit()
    assert _count(reason) == 1


def test_a_flush_that_fails_inside_a_savepoint_block_is_never_counted(
    session, reason
):
    """The ``with session.begin_nested():`` idiom that absorbs an
    IntegrityError. The failing flush rolls the SAVEPOINT back, but the only
    ``after_soft_rollback`` it fires names the flush's own subtransaction, and
    the block then CLOSES the savepoint instead of rolling it back - so the
    decision made inside it must be dropped when the savepoint ends, while the
    one made before it still counts."""
    session.connection()
    hooks._queue_exposure_metric(session, "expose", reason)
    with pytest.raises(IntegrityError), session.begin_nested():
        hooks._queue_exposure_metric(session, "expose", reason)
        session.add_all([_Row(name="clash"), _Row(name="clash")])
        session.flush()
    session.commit()
    assert _count(reason) == 1, "the failed SAVEPOINT's change was counted"


def test_a_commit_that_fails_is_never_counted(session, reason):
    """The flush succeeds and the COMMIT itself is refused (a deferred
    constraint): ``after_commit`` never fires, and the rollback that follows
    drops the queue, including for the next commit on the same session."""
    session.connection()
    hooks._queue_exposure_metric(session, "expose", reason)
    session.add(_Child(parent_id=404))
    session.flush()
    with pytest.raises(IntegrityError):
        session.commit()
    assert _count(reason) == 0, "counted although the COMMIT failed"
    session.rollback()
    session.connection()
    session.commit()
    assert _count(reason) == 0


def test_nested_savepoints_hand_their_changes_up_one_level_at_a_time(
    session, reason
):
    session.connection()
    outer = session.begin_nested()
    inner = session.begin_nested()
    hooks._queue_exposure_metric(session, "expose", reason)
    inner.commit()
    outer.commit()
    assert _count(reason) == 0, "counted at a SAVEPOINT release"
    session.commit()
    assert _count(reason) == 1


def test_a_released_savepoint_goes_with_its_rolled_back_parent(session, reason):
    session.connection()
    hooks._queue_exposure_metric(session, "expose", reason)
    outer = session.begin_nested()
    inner = session.begin_nested()
    hooks._queue_exposure_metric(session, "expose", reason)
    inner.commit()
    outer.rollback()
    session.commit()
    assert _count(reason) == 1, "only the change decided before the savepoints"


def test_sessions_never_share_a_queue(session, reason):
    """The queue lives on each session, never in module state: one session
    rolling back cannot drop another's change, nor lend it one."""
    with _sqlite_session() as other:
        session.connection()
        other.connection()
        hooks._queue_exposure_metric(session, "expose", reason)
        hooks._queue_exposure_metric(other, "expose", reason)
        other.rollback()
        session.commit()
        assert _count(reason) == 1
        other.connection()
        other.commit()
        assert _count(reason) == 1


def test_a_failing_counter_never_fails_the_commit(
    session, reason, monkeypatch, caplog
):
    """``after_commit`` runs once the COMMIT is durable: raising there would
    report a failure for data that did commit. The problem is logged, never
    swallowed silently."""

    def _broken(_action, _reason):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(hooks.a2a_metrics, "record_exposure_change", _broken)
    session.connection()
    hooks._queue_exposure_metric(session, "expose", reason)
    session.add(_Row(name="kept"))
    with caplog.at_level(logging.WARNING, logger=hooks.logger.name):
        session.commit()
    assert session.scalars(select(_Row.name)).all() == ["kept"]
    assert "registry unavailable" in caplog.text


def test_expose_treats_an_unset_enabled_as_enabled(monkeypatch):
    """``enabled`` is a Python-side default applied at INSERT, so it reads
    ``None`` until the agent is flushed. The truthiness guard refused every
    agent in that state; only an explicit ``False`` refuses now."""
    from app.models.agent import Agent

    added: list = []
    queued: list = []

    class _Session:
        def add(self, obj):
            added.append(obj)

    monkeypatch.setattr(hooks, "_run_as_or_none", lambda _agent: uuid.uuid4())
    monkeypatch.setattr(
        hooks, "_queue_exposure_metric", lambda _s, action, reason: queued.append(action)
    )
    agent = Agent(id=uuid.uuid4(), name="a", user_id=uuid.uuid4())
    agent.enabled = None

    hooks._expose(_Session(), agent)
    assert any(type(o).__name__ == "A2AExposure" for o in added)
    assert queued == ["expose"]

    added.clear()
    agent.enabled = False
    hooks._expose(_Session(), agent)
    assert added == []
