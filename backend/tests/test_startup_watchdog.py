"""A startup that never completes says so, and says what holds it."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections.abc import Iterator

import pytest

from app import main as main_module
from app.core import startup_watchdog
from app.core.config import settings
from app.core.migrations import alembic_server_settings
from app.core.startup_watchdog import set_startup_step, watch_startup


@pytest.fixture(autouse=True)
def _restore_step():
    before = startup_watchdog.current_startup_step()
    yield
    set_startup_step(before)


# Short enough for the suite, long enough that "nothing more is reported"
# waits several of them.
_INTERVAL = 0.05


class _Reports:
    """A ``describe`` that counts its calls, awaitable on a count - no wall clock."""

    def __init__(self, text: str = "pid 42 (xtm-one-migrations) active, blocked by [7]") -> None:
        self.text = text
        self.calls = 0
        self._waiters: list[tuple[int, asyncio.Event]] = []

    async def __call__(self) -> str:
        self.calls += 1
        for count, reached in self._waiters:
            if self.calls >= count:
                reached.set()
        return self.text

    async def reached(self, count: int) -> None:
        reached = asyncio.Event()
        if self.calls >= count:
            return
        self._waiters.append((count, reached))
        await asyncio.wait_for(reached.wait(), 5)


async def _describe() -> str:
    return "pid 42 (xtm-one-migrations) active, blocked by [7]"


async def test_a_stuck_startup_logs_the_step_and_the_sessions_at_error(caplog):
    settled = asyncio.Event()
    reports = _Reports()
    set_startup_step("applying the database migrations")
    with caplog.at_level(logging.ERROR, logger=startup_watchdog.__name__):
        task = asyncio.create_task(watch_startup(settled, interval=_INTERVAL, describe=reports))
        await reports.reached(2)
        settled.set()
        await asyncio.wait_for(task, 1)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) >= 2, "the report repeats while startup stays stuck"
    message = errors[0].getMessage()
    assert "step in progress: applying the database migrations" in message
    assert "blocked by [7]" in message


async def test_a_startup_that_completes_in_time_logs_nothing(caplog):
    settled = asyncio.Event()
    settled.set()
    with caplog.at_level(logging.DEBUG, logger=startup_watchdog.__name__):
        await asyncio.wait_for(watch_startup(settled, interval=_INTERVAL, describe=_describe), 1)
    assert not caplog.records


async def test_zero_disables_the_watchdog(caplog):
    with caplog.at_level(logging.DEBUG, logger=startup_watchdog.__name__):
        await asyncio.wait_for(watch_startup(asyncio.Event(), interval=0, describe=_describe), 1)
    assert not caplog.records


async def test_an_unreadable_database_still_reports_the_step(caplog):
    tried = asyncio.Event()

    async def unreadable() -> str:
        tried.set()
        raise ConnectionRefusedError("connection refused")

    settled = asyncio.Event()
    set_startup_step("waiting for the migration lock")
    with caplog.at_level(logging.ERROR, logger=startup_watchdog.__name__):
        task = asyncio.create_task(watch_startup(settled, interval=_INTERVAL, describe=unreadable))
        await asyncio.wait_for(tried.wait(), 5)
        await asyncio.sleep(0)
        settled.set()
        await asyncio.wait_for(task, 1)

    message = caplog.records[0].getMessage()
    assert "waiting for the migration lock" in message
    assert "Could not read the database sessions" in message


def test_zero_leaves_the_database_s_own_timeouts_in_force(monkeypatch):
    monkeypatch.setattr(settings, "migration_lock_timeout_seconds", 0)
    monkeypatch.setattr(settings, "migration_statement_timeout_seconds", 0)
    bounds = alembic_server_settings()
    assert "lock_timeout" not in bounds
    assert "statement_timeout" not in bounds


@pytest.mark.parametrize(
    "name", ["migration_lock_timeout_seconds", "migration_statement_timeout_seconds", "startup_watchdog_seconds"]
)
def test_a_negative_bound_is_refused_naming_the_setting(name):
    with pytest.raises(ValueError, match=name.upper()):
        type(settings)(**{name: -1})


# The wiring in ``main._run_startup_tasks``.


@contextlib.contextmanager
def _fresh_settled_event() -> Iterator[None]:
    original = main_module._startup_settled
    main_module._startup_settled = asyncio.Event()
    try:
        yield
    finally:
        main_module._startup_settled = original


def _watch_every(monkeypatch, describe) -> None:
    monkeypatch.setattr(
        startup_watchdog, "watch_startup", functools.partial(watch_startup, interval=_INTERVAL, describe=describe)
    )


async def _the_storage_probe_that_never_answers() -> None:
    await asyncio.sleep(60)


async def test_a_stuck_startup_is_reported_with_where_it_awaits_until_it_ends(monkeypatch, caplog):
    async def stuck() -> None:
        set_startup_step("starting the core services")
        await _the_storage_probe_that_never_answers()

    reports = _Reports()
    monkeypatch.setattr(main_module, "_do_all_startup_work", stuck)
    _watch_every(monkeypatch, reports)
    with _fresh_settled_event(), caplog.at_level(logging.ERROR, logger=startup_watchdog.__name__):
        task = asyncio.create_task(main_module._run_startup_tasks())
        await reports.reached(1)
        message = caplog.records[0].getMessage()
        assert "starting the core services" in message
        assert "The startup awaits" in message
        assert "_the_storage_probe_that_never_answers" in message
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        reported = len(caplog.records)
        await asyncio.sleep(_INTERVAL * 4)
    assert len(caplog.records) == reported, "nothing is reported once startup has ended"


@pytest.mark.parametrize("outcome", ["completes", "fails"])
async def test_a_report_in_flight_when_startup_settles_is_dropped(monkeypatch, caplog, outcome):
    describing = asyncio.Event()
    release = asyncio.Event()

    async def slow_describe() -> str:
        describing.set()
        await release.wait()
        return "late"

    async def settles_while_the_watchdog_reads() -> None:
        await describing.wait()
        if outcome == "fails":
            raise RuntimeError("migration failed")

    monkeypatch.setattr(main_module, "_do_all_startup_work", settles_while_the_watchdog_reads)
    monkeypatch.setattr(main_module, "_startup_error", None)
    _watch_every(monkeypatch, slow_describe)
    with _fresh_settled_event(), caplog.at_level(logging.ERROR, logger=startup_watchdog.__name__):
        await asyncio.wait_for(main_module._run_startup_tasks(), 5)
        release.set()
        await asyncio.sleep(_INTERVAL * 4)
    watchdog_reports = [r for r in caplog.records if r.name == startup_watchdog.__name__]
    assert not watchdog_reports, "a settled startup is never reported as not completed"
