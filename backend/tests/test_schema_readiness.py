"""The worker waits for the platform's migration before it processes jobs.

A deploy starts the platform and the worker together and only the platform
migrates: the worker used to run the new models against the old schema, and
a new ``assignments`` column failed every assignment run of the rollout
with ``UndefinedColumnError``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
import uuid

import pytest

from app.core import schema_readiness
from app.core._pre_squash_revisions import PRE_SQUASH_FINAL_HEAD
from app.core.schema_readiness import (
    CodeRevisions,
    load_code_revisions,
    schema_verdict,
    wait_for_schema_head,
)

CODE = CodeRevisions(heads=frozenset({"c3"}), known=frozenset({"c1", "c2", "c3"}))


def test_a_database_at_head_is_ready():
    assert schema_verdict(frozenset({"c3"}), CODE).ready


def test_a_database_behind_the_code_is_not_ready():
    verdict = schema_verdict(frozenset({"c2"}), CODE)
    assert not verdict.ready
    assert "c2" in verdict.reason and "c3" in verdict.reason


@pytest.mark.parametrize("current", [None, frozenset()])
def test_a_database_without_a_revision_is_not_ready(current):
    assert not schema_verdict(current, CODE).ready


def test_a_database_ahead_of_the_code_is_ready():
    """An older worker during a rollout or after a rollback: additive migrations
    keep the older models valid, and waiting would stop it for good."""
    assert schema_verdict(frozenset({"c4-from-a-newer-release"}), CODE).ready


def test_the_last_pre_squash_head_is_behind_not_ahead():
    """The platform re-stamps it to the baseline and upgrades it."""
    assert not schema_verdict(frozenset({PRE_SQUASH_FINAL_HEAD}), CODE).ready


def test_the_shipped_scripts_have_one_head_a_database_there_is_ready_at():
    code = load_code_revisions()
    assert len(code.heads) == 1
    assert code.heads <= code.known
    assert schema_verdict(code.heads, code).ready


@pytest.fixture
def code_revisions(monkeypatch):
    monkeypatch.setattr(schema_readiness, "load_code_revisions", lambda: CODE)


@pytest.fixture
def clock(monkeypatch):
    """The monotonic clock ``schema_readiness`` reads, moved only by the test.

    Only the module's own ``time`` is replaced: the event loop keeps the real
    clock. The escalation tests compared a 10 ms threshold with the real clock
    and the first wait was already past it under xdist load.
    """
    now = {"value": 0.0}
    monkeypatch.setattr(schema_readiness, "time", types.SimpleNamespace(monotonic=lambda: now["value"]))
    return now


def _reader(*answers, clock=None):
    calls = []

    async def read():
        if clock is not None:
            clock["value"] += 1.0
        answer = answers[min(len(calls), len(answers) - 1)]
        calls.append(answer)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return read, calls


@pytest.mark.asyncio
@pytest.mark.usefixtures("code_revisions")
async def test_the_wait_ends_once_the_platform_has_migrated(caplog):
    read, calls = _reader(frozenset({"c1"}), frozenset({"c2"}), frozenset({"c3"}))
    with caplog.at_level(logging.INFO, logger=schema_readiness.__name__):
        ready = await wait_for_schema_head(read_revisions=read, poll_seconds=0)
    assert ready
    assert len(calls) == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "one warning per minute, not one per poll"
    assert "Database schema ready" in caplog.records[-1].getMessage()


@pytest.mark.asyncio
@pytest.mark.usefixtures("code_revisions")
async def test_a_wait_past_the_delay_with_no_platform_migrating_turns_into_an_error(caplog, clock):
    """Nothing moves until a platform of this release migrates.

    Each read takes one second of the test's clock: the first wait is at 1 s,
    before the 1.5 s delay, the second at 2 s, past it.
    """
    read, _ = _reader(frozenset({"c1"}), frozenset({"c1"}), frozenset({"c3"}), clock=clock)

    async def nobody_migrating() -> bool:
        return False

    with caplog.at_level(logging.INFO, logger=schema_readiness.__name__):
        assert await wait_for_schema_head(
            read_revisions=read,
            poll_seconds=0,
            log_every_seconds=0,
            escalate_after_seconds=1.5,
            migration_in_progress=nobody_migrating,
        )
    waits = [r for r in caplog.records if "Waiting for the platform" in r.getMessage()]
    assert len(waits) == 2
    assert waits[0].levelno == logging.WARNING
    assert waits[-1].levelno == logging.ERROR
    assert "no platform process is migrating it" in waits[-1].getMessage()


@pytest.mark.asyncio
@pytest.mark.usefixtures("code_revisions")
async def test_a_long_migration_in_progress_stays_a_warning(caplog, clock):
    """The platform reports its own stuck startup; the worker only says it is migrating."""
    read, _ = _reader(frozenset({"c1"}), frozenset({"c1"}), frozenset({"c3"}), clock=clock)

    async def platform_migrating() -> bool:
        return True

    with caplog.at_level(logging.INFO, logger=schema_readiness.__name__):
        assert await wait_for_schema_head(
            read_revisions=read,
            poll_seconds=0,
            log_every_seconds=0,
            escalate_after_seconds=1.5,
            migration_in_progress=platform_migrating,
        )
    waits = [r for r in caplog.records if "Waiting for the platform" in r.getMessage()]
    assert len(waits) == 2
    assert all(r.levelno == logging.WARNING for r in waits)
    assert "a platform process is migrating it" in waits[-1].getMessage()


@pytest.mark.asyncio
@pytest.mark.usefixtures("code_revisions")
async def test_a_database_at_head_starts_the_worker_without_a_word(caplog):
    read, calls = _reader(frozenset({"c3"}))
    with caplog.at_level(logging.INFO, logger=schema_readiness.__name__):
        assert await wait_for_schema_head(read_revisions=read, poll_seconds=0)
    assert len(calls) == 1
    assert not caplog.records


@pytest.mark.asyncio
@pytest.mark.usefixtures("code_revisions")
async def test_an_unreadable_database_is_waited_for():
    read, calls = _reader(OSError("connection refused"), frozenset({"c3"}))
    assert await wait_for_schema_head(read_revisions=read, poll_seconds=0)
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("code_revisions")
async def test_a_stop_request_ends_the_wait():
    stop = asyncio.Event()
    read, calls = _reader(frozenset({"c1"}))

    async def stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    stopper = asyncio.create_task(stop_soon())
    ready = await asyncio.wait_for(
        wait_for_schema_head(stop=stop, read_revisions=read, poll_seconds=30),
        timeout=5,
    )
    await stopper
    assert ready is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unreadable_migration_scripts_never_hold_the_worker(monkeypatch):
    def broken():
        raise RuntimeError("no alembic directory")

    monkeypatch.setattr(schema_readiness, "load_code_revisions", broken)
    read, calls = _reader(frozenset({"c1"}))
    assert await wait_for_schema_head(read_revisions=read, poll_seconds=0)
    assert not calls


ADMIN_ID = uuid.uuid4()


@pytest.fixture
def worker_startup(monkeypatch):
    """``app.worker.startup`` with every side effect recorded, none run."""
    from app import worker as worker_module
    from app.core import executor as executor_module
    from app.llm import identity
    from app.services import orchestration_reconciler, security_options

    monkeypatch.setitem(sys.modules, "app.models", types.ModuleType("app.models"))
    monkeypatch.setattr(executor_module, "configure_default_executor", lambda _prefix: None)
    monkeypatch.setattr(worker_module, "_reconciler_task", None)
    monkeypatch.setattr(worker_module, "_security_options_refresh_task", None)
    monkeypatch.setattr(worker_module, "_service_identity_task", None)
    events: list[str] = []
    ready = {"value": True}

    async def gate(*, stop=None):
        events.append(f"gate stop={'event' if isinstance(stop, asyncio.Event) else stop}")
        return ready["value"]

    async def refresh():
        events.append("security options read")

    async def loop():
        events.append("security options loop")

    async def reconciler(draining=None):
        events.append("reconciler")

    monkeypatch.setattr(schema_readiness, "wait_for_schema_head", gate)
    monkeypatch.setattr(security_options, "refresh_security_options_cache", refresh)
    monkeypatch.setattr(security_options, "run_security_options_refresh_loop", loop)

    async def admin_lookup(_db):
        events.append("users read")
        return ADMIN_ID

    monkeypatch.setattr(orchestration_reconciler, "reconciler_loop", reconciler)
    async def identity_loop():
        events.append("service identity loop")

    monkeypatch.setattr(identity, "_service_identity", None)
    monkeypatch.setattr(identity, "service_account_id", admin_lookup)
    monkeypatch.setattr(identity, "keep_service_identity_current", identity_loop)
    return worker_module, events, ready


async def _drain(worker_module):
    for task in (
        worker_module._reconciler_task,
        worker_module._security_options_refresh_task,
        worker_module._service_identity_task,
    ):
        if task is not None:
            await task


@pytest.mark.asyncio
async def test_the_worker_reads_nothing_before_the_schema_is_ready(worker_startup):
    worker_module, events, _ = worker_startup
    worker = types.SimpleNamespace(job_task_contexts={}, event=asyncio.Event())
    await worker_module.startup({"worker": worker})
    await _drain(worker_module)
    assert events[0] == "gate stop=event", "the gate listens to the SAQ stop event"
    assert "security options read" in events and "reconciler" in events


@pytest.mark.asyncio
async def test_a_stop_during_the_wait_starts_nothing(worker_startup):
    worker_module, events, ready = worker_startup
    ready["value"] = False
    worker = types.SimpleNamespace(job_task_contexts={}, event=asyncio.Event())
    with pytest.raises(asyncio.CancelledError):
        await worker_module.startup({"worker": worker})
    assert events == ["gate stop=event"]
    assert worker_module._reconciler_task is None


@pytest.mark.asyncio
async def test_the_worker_loads_the_llm_service_identity_once_the_schema_is_ready(worker_startup):
    """Only the platform's bootstrap set it: every gateway call a job made with
    nobody bound failed before it was sent. It reads the users table."""
    from app.llm import identity

    worker_module, events, _ = worker_startup
    worker = types.SimpleNamespace(job_task_contexts={}, event=asyncio.Event())
    await worker_module.startup({"worker": worker})
    await _drain(worker_module)
    assert events.index("gate stop=event") < events.index("users read")
    assert identity.resolve_llm_caller().user_id == ADMIN_ID
    assert "service identity loop" in events
