"""The one-rating-per-rater rule for ``avg_feedback_score``.

The PostgreSQL behaviour is proven in ``test_feedback_scores_postgres.py``;
these pin what no local database can: the ClickHouse spelling, the lock key the
two writers share, and that every reader goes through the shared rule.
"""

from __future__ import annotations

import inspect
import logging
import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.api.v1 import chat
from app.models.llm_trace import LLM_TRACE_SOURCES
from app.services import telemetry, trace_storage
from app.services.tracing import feedback_scores, read_store
from app.services.tracing.feedback_scores import (
    FEEDBACK_PER_TRACE_SQL_CLICKHOUSE,
    FEEDBACK_PER_TRACE_SQL_POSTGRES,
    FEEDBACK_PER_TRACE_SQL_POSTGRES_ONE_DAY,
    feedback_per_trace_subquery,
    new_score_id,
    score_lock_key,
)
from app.services.tracing.local_sink import write_score_row


def test_clickhouse_takes_the_latest_row_as_a_tuple_so_a_null_retraction_is_not_skipped():
    # argMax skips NULL arguments: argMax(value, ...) would hand back the
    # rating a NULL retraction replaced.
    assert "argMax(tuple(value" in FEEDBACK_PER_TRACE_SQL_CLICKHOUSE
    assert not re.search(r"argMax\(value\b", FEEDBACK_PER_TRACE_SQL_CLICKHOUSE)
    assert "GROUP BY trace_id, ifNull(toString(user_id), toString(id))" in FEEDBACK_PER_TRACE_SQL_CLICKHOUSE


def test_a_legacy_retraction_also_needs_the_missing_metadata():
    """The comment is free text a rater can type; the metadata is not."""
    assert "latest_metadata IN ('', '{}', 'null')" in FEEDBACK_PER_TRACE_SQL_CLICKHOUSE
    assert "OR metadata_type <> 'null'" in FEEDBACK_PER_TRACE_SQL_POSTGRES
    assert "jsonb_typeof" in str(feedback_per_trace_subquery().compile(dialect=postgresql.dialect()))


def test_clickhouse_also_needs_the_version_4_id_every_old_writer_minted():
    """ClickHouse stores an admin score's empty metadata like none at all: only
    the id version tells a new admin score from a legacy retraction there."""
    assert "substring(toString(latest_id), 15, 1) = '4'" in FEEDBACK_PER_TRACE_SQL_CLICKHOUSE
    assert "tupleElement(latest, 4) AS latest_id" in FEEDBACK_PER_TRACE_SQL_CLICKHOUSE


@pytest.mark.parametrize("stdlib", [True, False])
def test_an_admin_score_id_is_version_7(monkeypatch, stdlib):
    """Python 3.14 has ``uuid.uuid7``; 3.13 gets the same layout built by hand."""
    if not stdlib:
        monkeypatch.delattr(uuid, "uuid7", raising=False)
    elif not hasattr(uuid, "uuid7"):
        pytest.skip("uuid.uuid7 needs Python 3.14")
    ids = [new_score_id() for _ in range(200)]
    assert {score_id.version for score_id in ids} == {7}
    assert {score_id.variant for score_id in ids} == {uuid.RFC_4122}
    assert {str(score_id)[14] for score_id in ids} == {"7"}  # the 15th character ClickHouse reads
    assert len(set(ids)) == len(ids)


def test_the_admin_writers_mint_version_7_ids():
    for writer in (
        read_store.PostgresTraceReadStore.create_trace_score,
        read_store.ClickHouseTraceReadStore.create_trace_score,
    ):
        source = inspect.getsource(writer)
        assert "new_score_id()" in source, writer.__qualname__
        assert "uuid.uuid4()" not in source, writer.__qualname__


def test_the_day_scans_rank_only_that_days_scores():
    """Once per day scan, the rule would otherwise rank every feedback row there is."""
    scope = feedback_scores._ONE_DAY_OF_TRACES
    for bound in ("started_at >= :day_start", "started_at < :day_end", "source = ANY(:sources)"):
        assert bound in scope
    # The same rule otherwise.
    assert scope in FEEDBACK_PER_TRACE_SQL_POSTGRES_ONE_DAY
    assert FEEDBACK_PER_TRACE_SQL_POSTGRES_ONE_DAY.replace(scope, "") == FEEDBACK_PER_TRACE_SQL_POSTGRES
    for builder in (trace_storage._day_rollup_rows, trace_storage.refresh_daily_feedback):
        source = inspect.getsource(builder)
        assert "{FEEDBACK_PER_TRACE_SQL_POSTGRES_ONE_DAY}" in source, builder.__name__
        assert "{FEEDBACK_PER_TRACE_SQL_POSTGRES}" not in source, builder.__name__


def test_the_postgres_subquery_ranks_per_rater():
    sql = str(feedback_per_trace_subquery().compile(dialect=postgresql.dialect()))
    assert "row_number() OVER (PARTITION BY llm_scores.trace_id, coalesce(llm_scores.user_id, llm_scores.id)" in sql
    assert "IS DISTINCT FROM" in sql


def test_every_writer_of_a_raters_score_takes_the_same_lock_and_clock():
    """The latest action wins only if every writer orders its action the same way."""
    assert score_lock_key("t", "u", "user_feedback") == "llm_score:t:u:user_feedback"
    for writer in (
        read_store.PostgresTraceReadStore.create_trace_score,
        read_store.ClickHouseTraceReadStore.create_trace_score,
        chat._begin_local_feedback_action,
    ):
        assert "begin_score_action(" in inspect.getsource(writer), writer.__qualname__
    assert "lock_rater_score(" in inspect.getsource(write_score_row)
    # The one-time rewrite of the old chat scores stores them the way the sink does.
    rewrite = inspect.getsource(trace_storage.rewrite_legacy_chat_scores)
    assert "lock_rater_score(" in rewrite
    assert "write_score_row(" in rewrite
    assert "rater_score_payload(" in rewrite
    # And the chat endpoints build the rater's score with the same function.
    assert "rater_score_payload(" in inspect.getsource(chat._local_feedback_score)
    assert feedback_scores.rater_score_payload is chat.rater_score_payload
    # Through the one chat write, behind the chat endpoints and the annotations router.
    for write in (chat.apply_message_feedback, chat.retract_message_feedback):
        source = inspect.getsource(write)
        assert "await _begin_local_feedback_action(" in source, write.__name__
        assert "await _local_feedback_score(" in source, write.__name__


_ANOTHER_PROCESS = "another-process"


class _Redis:
    """Just enough Redis for the marker and the claim of the one full pass."""

    def __init__(self, marked: bool, fail: bool = False, claimed: bool = False) -> None:
        self.fail = fail
        self.store: dict[str, str] = {}
        if marked:
            self.store[trace_storage.FEEDBACK_RULE_REFRESH_MARKER] = "1"
        if claimed:
            self.store[trace_storage.FEEDBACK_RULE_REFRESH_CLAIM] = _ANOTHER_PROCESS

    async def exists(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return int(key in self.store)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def eval(self, _script, _numkeys, key, token):
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


def _patch_redis(monkeypatch, fake: _Redis) -> None:
    async def _client():
        return fake

    monkeypatch.setattr("app.core.redis.redis_client", _client)


def _patch_rewrite(monkeypatch, *, done: bool = True) -> list[object]:
    """The one-time rewrite of the old chat scores; records who ran it."""
    calls: list[object] = []

    async def _rewrite(db):
        calls.append(db)
        return done

    monkeypatch.setattr(trace_storage, "rewrite_legacy_chat_scores", _rewrite)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marked", "fail", "claimed", "rewritten", "expected_days", "full"),
    [
        (False, False, False, True, 30, True),  # first run after the fix: every retained day
        (True, False, False, True, 7, False),  # afterwards: the last week only
        (False, True, False, True, 7, False),  # marker unreadable: the cheap pass, retried later
        (False, False, True, True, 7, False),  # another process runs the full pass: the cheap one here
        # Old chat scores still to rewrite: the full pass waits for the next run.
        (False, False, False, False, 7, False),
    ],
)
async def test_the_rollup_refresh_is_full_once_then_recent(
    monkeypatch, marked, fail, claimed, rewritten, expected_days, full
):
    fake = _Redis(marked, fail, claimed)
    _patch_redis(monkeypatch, fake)
    rewrites = _patch_rewrite(monkeypatch, done=rewritten)
    refreshed: list[date] = []

    async def _refresh(_db, day):
        refreshed.append(day)
        return 1

    monkeypatch.setattr(trace_storage, "refresh_daily_feedback", _refresh)
    latest = date(2026, 9, 26)
    db = object()
    changed = await trace_storage.refresh_rollup_feedback(db, earliest=latest - timedelta(days=29), latest=latest)
    assert changed == expected_days
    assert refreshed[0] == latest and len(refreshed) == expected_days
    assert (trace_storage.FEEDBACK_RULE_REFRESH_MARKER in fake.store) is (marked or full)
    # Only the owner of the full pass rewrites, and before it re-reads a day.
    assert rewrites == ([] if marked or fail or claimed else [db])
    # The claim is released by its owner and never by anyone else.
    assert fake.store.get(trace_storage.FEEDBACK_RULE_REFRESH_CLAIM) == (_ANOTHER_PROCESS if claimed else None)


@pytest.mark.asyncio
async def test_a_full_pass_that_failed_a_day_is_left_for_the_next_run(monkeypatch):
    fake = _Redis(marked=False)
    _patch_redis(monkeypatch, fake)
    _patch_rewrite(monkeypatch)

    async def _refresh(_db, day):
        if day == date(2026, 9, 20):
            raise RuntimeError("canceling statement due to statement timeout")
        return 0

    class _DB:
        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(trace_storage, "refresh_daily_feedback", _refresh)
    latest = date(2026, 9, 26)
    await trace_storage.refresh_rollup_feedback(_DB(), earliest=latest - timedelta(days=29), latest=latest)

    # No marker, and the claim is released: the next run takes the full pass again.
    assert fake.store == {}


class _RewriteDB:
    """The rewrite's statements: the scores to rewrite, then one transaction each."""

    def __init__(self, pairs) -> None:
        self.pairs = pairs
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        return SimpleNamespace(all=lambda: list(self.pairs))

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _ClickHouseSink:
    flushed: list[dict] = []
    closed = 0

    def __init__(self) -> None:
        self.tracing_errors = 0

    async def write_score(self, score: dict) -> None:
        type(self).flushed.append(score)

    async def close(self) -> None:
        type(self).closed += 1


class _DownClickHouseSink(_ClickHouseSink):
    """What ``ClickHouseSink`` does when the insert fails: log, count, carry on."""

    async def write_score(self, score: dict) -> None:
        self.tracing_errors += 1


def _postgres_store(monkeypatch) -> None:
    monkeypatch.setattr("app.services.tracing.store.get_trace_store_provider", lambda: "postgres")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["postgres", "clickhouse"])
async def test_the_rewrite_goes_past_a_failed_score_and_copies_the_rest_to_clickhouse(monkeypatch, provider):
    from datetime import UTC, datetime

    last_action = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    pairs = [(uuid.uuid4(), uuid.uuid4(), last_action) for _ in range(3)]
    db = _RewriteDB(pairs)

    async def _lock(*_args):
        return None

    async def _payload(_db, *, trace_id, user_id, created_at):
        if trace_id == pairs[1][0]:
            raise RuntimeError("deadlock detected")
        return {"trace_id": str(trace_id), "user_id": str(user_id), "value": 1.0, "created_at": created_at}

    async def _write(_db, payload, _trace_id):
        return True

    monkeypatch.setattr(trace_storage, "lock_rater_score", _lock)
    monkeypatch.setattr(trace_storage, "rater_score_payload", _payload)
    monkeypatch.setattr(trace_storage, "write_score_row", _write)
    monkeypatch.setattr("app.services.tracing.store.get_trace_store_provider", lambda: provider)
    monkeypatch.setattr("app.services.tracing.clickhouse_sink.ClickHouseSink", _ClickHouseSink)
    _ClickHouseSink.flushed, _ClickHouseSink.closed = [], 0

    # A score that failed is left for the next run: not done yet.
    assert await trace_storage.rewrite_legacy_chat_scores(db) is False
    assert (db.commits, db.rollbacks) == (2, 2)  # the read, then the failed score
    if provider == "clickhouse":
        # The rows ClickHouse reads get the same scores, timed after the last old action.
        assert [score["trace_id"] for score in _ClickHouseSink.flushed] == [str(pairs[0][0]), str(pairs[2][0])]
        assert {score["created_at"] for score in _ClickHouseSink.flushed} == {last_action + timedelta(milliseconds=1)}
        assert _ClickHouseSink.closed == 1
    else:
        assert _ClickHouseSink.flushed == []


@pytest.mark.asyncio
async def test_a_score_clickhouse_did_not_take_is_left_for_the_next_run(monkeypatch):
    """The ClickHouse sink logs a failed insert and carries on, and a pair whose
    PostgreSQL rows are rewritten is never selected again: that pair's rewrite
    is rolled back, or ClickHouse would keep the misread for good. ClickHouse is
    likely down, so the rest of the batch waits too."""
    from datetime import UTC, datetime

    last_action = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    pairs = [(uuid.uuid4(), uuid.uuid4(), last_action) for _ in range(3)]
    db = _RewriteDB(pairs)
    writes: list[dict] = []

    async def _nothing(*_args, **_kwargs):
        return None

    async def _payload(_db, *, trace_id, user_id, created_at):
        return {"trace_id": str(trace_id), "user_id": str(user_id), "value": 1.0, "created_at": created_at}

    async def _write(_db, payload, _trace_id):
        writes.append(payload)
        return True

    monkeypatch.setattr(trace_storage, "lock_rater_score", _nothing)
    monkeypatch.setattr(trace_storage, "rater_score_payload", _payload)
    monkeypatch.setattr(trace_storage, "write_score_row", _write)
    monkeypatch.setattr("app.services.tracing.store.get_trace_store_provider", lambda: "clickhouse")
    monkeypatch.setattr("app.services.tracing.clickhouse_sink.ClickHouseSink", _DownClickHouseSink)
    _DownClickHouseSink.closed = 0

    assert await trace_storage.rewrite_legacy_chat_scores(db) is False
    # The read, then the first pair's rewrite: rolled back, nothing committed.
    assert (db.commits, db.rollbacks) == (0, 2)
    assert [payload["trace_id"] for payload in writes] == [str(pairs[0][0])]
    assert _DownClickHouseSink.closed == 1


@pytest.mark.asyncio
async def test_the_rewrite_is_done_only_once_nothing_is_left(monkeypatch):
    from datetime import UTC, datetime

    async def _nothing(*_args, **_kwargs):
        return None

    async def _payload(_db, *, trace_id, user_id, created_at):
        return {"trace_id": str(trace_id), "user_id": str(user_id), "value": 0.0, "created_at": created_at}

    async def _written(*_args):
        return True

    monkeypatch.setattr(trace_storage, "lock_rater_score", _nothing)
    monkeypatch.setattr(trace_storage, "rater_score_payload", _payload)
    monkeypatch.setattr(trace_storage, "write_score_row", _written)
    _postgres_store(monkeypatch)
    at = datetime(2026, 9, 1, tzinfo=UTC)
    three = [(uuid.uuid4(), uuid.uuid4(), at) for _ in range(3)]
    # The read asks for one more than the batch, to know whether any is left.
    assert await trace_storage.rewrite_legacy_chat_scores(_RewriteDB(three), limit=2) is False
    assert await trace_storage.rewrite_legacy_chat_scores(_RewriteDB(three[:2]), limit=2) is True
    assert await trace_storage.rewrite_legacy_chat_scores(_RewriteDB([]), limit=2) is True


@pytest.mark.asyncio
async def test_a_retraction_found_by_the_rewrite_is_not_written(monkeypatch):
    """The rater's last rating went since the read: a retraction sent its own
    score, a deleted conversation sent none - neither is the rewrite's to write."""
    from datetime import UTC, datetime

    async def _nothing(*_args, **_kwargs):
        return None

    async def _retraction(_db, *, trace_id, user_id, created_at):
        return {"trace_id": str(trace_id), "user_id": str(user_id), "value": None, "retract": True}

    writes: list[dict] = []

    async def _write(_db, payload, _trace_id):
        writes.append(payload)
        return True

    monkeypatch.setattr(trace_storage, "lock_rater_score", _nothing)
    monkeypatch.setattr(trace_storage, "rater_score_payload", _retraction)
    monkeypatch.setattr(trace_storage, "write_score_row", _write)
    _postgres_store(monkeypatch)
    pair = (uuid.uuid4(), uuid.uuid4(), datetime(2026, 9, 1, tzinfo=UTC))
    assert await trace_storage.rewrite_legacy_chat_scores(_RewriteDB([pair])) is True
    assert writes == []


class _RecordingDB:
    def __init__(self) -> None:
        self.statements: list[tuple[str, dict | None]] = []
        self.commits = 0

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        return SimpleNamespace(rowcount=2)

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_the_refresh_writes_under_the_rollup_lock_on_the_source_index():
    """Every writer of ``llm_daily_stats`` serializes on one lock (a
    refresh interleaved with the rebuild of a stale day would race its delete and
    insert), and every day scan is a range on ``ix_llm_traces_source_started_at``."""
    db = _RecordingDB()

    assert await trace_storage.refresh_daily_feedback(db, date(2026, 9, 1)) == 2

    (lock, _), (update, params) = db.statements
    assert lock == trace_storage.DAILY_STATS_LOCK_SQL
    assert update.lstrip().startswith("UPDATE llm_daily_stats d")
    assert "t.source = ANY(:sources)" in update
    assert params["sources"] == list(LLM_TRACE_SOURCES)
    assert db.commits == 1


class _ScoreWriteSession:
    """A score write's session, as the rollup record sees it: the flush of the
    score, the savepoint, the trace's start, the retention when the window
    alone does not decide, and the record."""

    def __init__(self, started_at, *, fail: bool = False, retention_days: int | None = 90) -> None:
        self.started_at = started_at
        self.fail = fail
        self.retention_days = retention_days
        self.retention_reads = 0
        self.flushes = 0
        self.savepoints: list[str] = []
        self.records: list[dict] = []

    async def flush(self) -> None:
        self.flushes += 1

    def begin_nested(self):
        savepoints = self.savepoints

        class _Savepoint:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, _exc, _tb):
                savepoints.append("rolled back" if exc_type else "released")
                return False

        return _Savepoint()

    async def scalar(self, statement):
        if self.fail:
            raise ConnectionRefusedError("database unreachable")
        if "trace_retention_days" in str(statement):
            self.retention_reads += 1
            return self.retention_days
        return self.started_at

    async def execute(self, statement, params=None):
        self.records.append({"sql": str(statement), **(params or {})})


_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
# Before 01:00 UTC the last complete day is the day before yesterday.
_EARLY = datetime(2026, 9, 28, 0, 30, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("days_before_latest", "recorded"),
    [
        (10, True),  # a day the recent refresh does not read
        (trace_storage.FEEDBACK_REFRESH_RECENT_DAYS - 1, True),  # its oldest day, which it leaves at 01:00 UTC
        (trace_storage.FEEDBACK_REFRESH_RECENT_DAYS - 2, False),  # read by the next run whatever the hour
        (0, False),
    ],
)
async def test_a_feedback_change_records_the_day_the_recent_refresh_will_not_read(days_before_latest, recorded):
    """A rating given, changed or retracted on an older trace left its day's
    rollup stale for good: its day is recorded, with the score, for the
    backfill to rebuild."""
    day = trace_storage.latest_complete_day(_NOW) - timedelta(days=days_before_latest)
    db = _ScoreWriteSession(datetime.combine(day, time(23, 30), tzinfo=UTC))

    assert await trace_storage.record_feedback_day_stale(db, uuid.uuid4(), now=_NOW) == (day if recorded else None)

    assert [record["day"] for record in db.records] == ([day] if recorded else [])
    # The score's own writes are flushed before the savepoint, which holds the record alone.
    assert db.flushes == 1
    assert db.savepoints == ["released"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("now", "retention_days", "day", "recorded", "reads_retention"),
    [
        # 90 days: the recent window alone decides.
        (_NOW, 90, date(2026, 9, 21), True, False),  # the window's oldest day
        (_NOW, 90, date(2026, 9, 22), False, True),
        (_EARLY, 90, date(2026, 9, 21), False, True),  # still read once the window moves at 01:00
        # 7 days: the refresh never reads the oldest retained day, which Sep 22
        # becomes at midnight, before the window's own oldest day moves.
        (_NOW, 7, date(2026, 9, 22), True, True),
        (_NOW, 7, date(2026, 9, 23), False, True),
        (_EARLY, 7, date(2026, 9, 22), True, True),
        # 1 day: the refresh reads nothing, so the one complete day kept is recorded.
        (_NOW, 1, date(2026, 9, 27), True, True),
        (_NOW, 1, date(2026, 9, 28), False, False),  # today: not built yet
        # No settings row: the backfill's 90 days.
        (_NOW, None, date(2026, 9, 22), False, True),
    ],
)
async def test_a_retention_shorter_than_the_window_records_the_days_it_leaves_unread(
    now, retention_days, day, recorded, reads_retention
):
    """The recent refresh never reads the oldest retained day (the purge may be
    deleting it): under a retention shorter than its window, the days it leaves
    unread are recorded like older ones. The retention is read only when the
    window alone does not decide."""
    db = _ScoreWriteSession(datetime.combine(day, time(12, 0), tzinfo=UTC), retention_days=retention_days)

    assert await trace_storage.record_feedback_day_stale(db, uuid.uuid4(), now=now) == (day if recorded else None)
    assert db.retention_reads == int(reads_retention)


@pytest.mark.asyncio
async def test_a_day_left_unrecorded_is_read_by_every_refresh_of_the_next_day():
    """Whatever the hour and the retention, a day the record leaves alone is one
    the recent refresh of the backfill reads in any run of the next 24 hours:
    its window moves a day a day at most."""
    start = datetime(2026, 9, 28, tzinfo=UTC)
    for minutes in range(0, 24 * 60, 30):
        now = start + timedelta(minutes=minutes)
        for retention_days in (*range(1, 11), 90):
            oldest_kept = now.date() - timedelta(days=retention_days)
            day = trace_storage.latest_complete_day(now)
            while day >= oldest_kept:
                db = _ScoreWriteSession(datetime.combine(day, time(12, 0), tzinfo=UTC), retention_days=retention_days)
                if await trace_storage.record_feedback_day_stale(db, uuid.uuid4(), now=now) is None:
                    for hours in range(1, 25):
                        then = now + timedelta(hours=hours)
                        floor = trace_storage.feedback_refresh_floor(
                            earliest=trace_storage.feedback_refresh_earliest(retention_days, then),
                            latest=trace_storage.latest_complete_day(then),
                        )
                        assert floor <= day, (now, retention_days, day, then)
                day -= timedelta(days=1)


@pytest.mark.asyncio
async def test_a_trace_that_is_gone_records_no_rollup_day():
    db = _ScoreWriteSession(None)

    assert await trace_storage.record_feedback_day_stale(db, uuid.uuid4(), now=_NOW) is None
    assert db.records == []


@pytest.mark.asyncio
async def test_a_rollup_record_that_fails_never_fails_the_rating_write(caplog):
    """Best effort: the record's savepoint is rolled back and the failure logged;
    the score it was written with stands."""
    db = _ScoreWriteSession(None, fail=True)

    with caplog.at_level(logging.WARNING, logger=trace_storage.logger.name):
        assert await trace_storage.record_feedback_day_stale(db, uuid.uuid4()) is None

    assert db.savepoints == ["rolled back"]
    assert "Could not record the rollup of trace" in caplog.text


def test_a_feedback_record_takes_no_rollup_lock_and_a_rebuild_clears_only_the_mark_it_read():
    """A score write never waits on a rollup being built, so a mark can land
    while a rebuild runs: the rebuild then keeps it for the next run."""
    record = trace_storage._RECORD_STALE_DAY_SQL
    assert "pg_advisory" not in record
    # Past the mark it replaces, whatever the clock reads: a rebuild that read
    # that mark must never find its value on a new one.
    assert "GREATEST(EXCLUDED.marked_at, recorded.marked_at + INTERVAL '1 microsecond')" in record
    assert "pg_advisory" not in inspect.getsource(trace_storage.record_feedback_day_stale)
    assert trace_storage._CLEAR_STALE_DAY_SQL.endswith("WHERE date = :day AND marked_at = :marked_at")
    rebuild = inspect.getsource(trace_storage.rebuild_daily_stats)
    assert "DELETE FROM llm_daily_stats_stale_days" not in rebuild
    assert rebuild.count("text(_CLEAR_STALE_DAY_SQL), record") == 3


def test_no_reader_averages_every_score_row():
    """A reader averaging ``llm_scores`` directly would bring the bug back."""
    for module in (read_store, trace_storage, telemetry):
        source = inspect.getsource(module)
        assert not re.search(r"avg\(\s*value\s*\)", source, re.IGNORECASE), module.__name__
        assert not re.search(r"func\.avg\(\s*LLMScore(Model)?\.value", source), module.__name__
    assert inspect.getsource(read_store).count("{FEEDBACK_PER_TRACE_SQL_CLICKHOUSE}") == 3
