"""State that must hold across platform processes.

A platform pod runs ``WEB_WORKERS`` uvicorn processes (2 by default), each
running the whole lifespan, more replicas multiply them, and the SAQ worker is
one more process. Two pieces of state assumed a single process:

* the Discord gateway: every process opens its own session and Discord
  delivers each message to every session, so a message was answered once per
  process - now the first process to claim the message id answers it;
* the branding snapshot: an admin save refreshed only the process that served
  it, and the SAQ worker never loaded it - now every process re-reads it on a
  short loop, and a change counts as applied only once its renders (OpenAPI
  title, index.html) succeeded.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels import discord as discord_channel
from app.services import branding
from tests.ast_helpers import call_lines, function_node

APP_DIR = Path(__file__).resolve().parents[1] / "app"


class _FakeRedis:
    def __init__(self) -> None:
        self.keys: dict[str, tuple[str, int | None]] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
        if nx and key in self.keys:
            return None
        self.keys[key] = (value, ex)
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr("app.core.redis.redis_client", AsyncMock(return_value=redis))
    return redis


# -- Discord gateway -----------------------------------------------------------


async def test_only_the_first_process_claims_a_discord_message(fake_redis):
    assert await discord_channel._claim_discord_message("1234") is True
    # The same message through another process's gateway session.
    assert await discord_channel._claim_discord_message("1234") is False
    # Another message is its own claim.
    assert await discord_channel._claim_discord_message("5678") is True
    _value, ttl = fake_redis.keys["discord:dedup:1234"]
    assert ttl == discord_channel._DEDUP_TTL


async def test_a_redis_outage_answers_the_message(monkeypatch):
    # Fail open like Slack: a duplicate answer beats a message nobody answers.
    monkeypatch.setattr("app.core.redis.redis_client", AsyncMock(side_effect=ConnectionError("down")))
    assert await discord_channel._claim_discord_message("1234") is True


def test_the_gateway_claims_a_message_before_dispatching_it():
    source = inspect.getsource(discord_channel._run_discord_gateway)
    claim = source.index("_claim_discord_message(str(message.id))")
    assert claim < source.index("spawn_channel_task(")


# -- Branding ------------------------------------------------------------------


@pytest.fixture
def restore_branding():
    saved = branding._cached_branding
    yield
    branding._cached_branding = saved


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_index_html_is_current_only_once_rendered_with_the_cached_branding(tmp_path, monkeypatch, restore_branding):
    (tmp_path / "index.html").write_text("<html><head><title>x</title></head><body></body></html>", encoding="utf-8")
    monkeypatch.setattr(branding, "_index_html_frontend_dir", tmp_path)
    monkeypatch.setattr(branding, "_index_html_branding", None)
    monkeypatch.setattr(branding, "_index_html_content", branding._index_html_content)
    assert branding.index_html_is_current() is False
    branding.refresh_configured_branded_index_html()
    assert branding.index_html_is_current() is True
    # A new snapshot the page was not rendered with.
    branding._cached_branding = dataclasses.replace(branding._cached_branding, name="Renamed")
    assert branding.index_html_is_current() is False
    # A render that fails keeps the old page, and says so.
    monkeypatch.setattr(branding, "refresh_branded_index_html", lambda _dir: "")
    branding.refresh_configured_branded_index_html()
    assert branding.index_html_is_current() is False


def test_without_a_built_frontend_the_index_is_current(tmp_path, monkeypatch):
    monkeypatch.setattr(branding, "_index_html_frontend_dir", tmp_path)
    assert branding.index_html_is_current() is True


async def test_a_read_overtaken_by_a_newer_one_does_not_undo_it(monkeypatch, restore_branding):
    before = dataclasses.replace(branding.get_cached_branding(), name="Before the admin save")
    after = dataclasses.replace(before, name="After the admin save")
    reads = iter([before, after])
    slow_read_returns = asyncio.Event()

    async def get_branding(_db):
        stored = next(reads)
        if stored is before:
            await slow_read_returns.wait()
        return stored

    monkeypatch.setattr(branding, "get_branding", get_branding)
    # The refresh loop's read started before the admin committed...
    loop_read = asyncio.create_task(branding.refresh_branding_cache(MagicMock()))
    await asyncio.sleep(0)
    # ...the admin save's own read, after its commit, lands first...
    assert await branding.refresh_branding_cache(MagicMock()) == after
    # ...and the older read landing last keeps the newer snapshot.
    slow_read_returns.set()
    assert await loop_read == after
    assert branding.get_cached_branding() == after


async def _run_loop(monkeypatch, snapshots, *, index_current, openapi_errors=(), web=True):
    """Run the refresh loop over successive database snapshots, then stop it.

    ``web``: the web process's loop (main.py), which re-renders the OpenAPI
    title and index.html; otherwise the SAQ worker's, which only follows the
    database.
    """
    from app import main

    @asynccontextmanager
    async def session():
        yield MagicMock()

    monkeypatch.setattr("app.core.database.async_session", session)
    monkeypatch.setattr(branding, "BRANDING_REFRESH_INTERVAL_SECONDS", 0)
    reads = iter(snapshots)

    async def refresh(_db):
        try:
            answer = next(reads)
        except StopIteration:
            raise asyncio.CancelledError from None
        if isinstance(answer, BaseException):
            raise answer
        branding._cached_branding = answer
        return answer

    monkeypatch.setattr(branding, "refresh_branding_cache", refresh)
    monkeypatch.setattr(branding, "index_html_is_current", index_current)
    errors = list(openapi_errors)

    def openapi():
        if errors:
            raise errors.pop(0)

    renders = MagicMock(side_effect=openapi)
    index = MagicMock()
    monkeypatch.setattr(main, "_refresh_openapi_branding", renders)
    monkeypatch.setattr(main, "_refresh_index_html", index)
    with pytest.raises(asyncio.CancelledError):
        await (main._branding_refresh_loop() if web else branding.keep_branding_current())
    return renders, index


async def test_the_refresh_loop_re_renders_only_on_a_change(monkeypatch, restore_branding):
    first = branding.get_cached_branding()
    renamed = dataclasses.replace(first, name="Renamed by an admin elsewhere")
    renders, index = await _run_loop(
        monkeypatch,
        [first, renamed, renamed, RuntimeError("db down"), renamed],
        index_current=lambda: True,
    )
    # One change, one render; unchanged cycles and a failed read render
    # nothing, and the failure does not stop the loop.
    assert renders.call_count == 1
    assert index.call_count == 1


async def test_a_failed_render_is_retried_on_the_next_cycle(monkeypatch, restore_branding):
    first = branding.get_cached_branding()
    renamed = dataclasses.replace(first, name="Renamed by an admin elsewhere")
    renders, _index = await _run_loop(
        monkeypatch,
        [renamed, renamed, renamed],
        index_current=lambda: True,
        openapi_errors=[RuntimeError("render failed")],
    )
    # Failed on the first cycle, applied on the second, nothing on the third.
    assert renders.call_count == 2


async def test_an_index_html_kept_stale_is_retried(monkeypatch, restore_branding):
    first = branding.get_cached_branding()
    renamed = dataclasses.replace(first, name="Renamed by an admin elsewhere")
    states = iter([False, True, True])
    _renders, index = await _run_loop(
        monkeypatch,
        [renamed, renamed, renamed],
        index_current=lambda: next(states, True),
    )
    # The first render left the page stale; the second made it current.
    assert index.call_count == 2


async def test_a_failing_refresh_is_logged_once_per_streak(monkeypatch, restore_branding):
    first = branding.get_cached_branding()
    down = RuntimeError("db down")
    records = _Records()
    branding_logger = logging.getLogger(branding.__name__)
    previous_level = branding_logger.level
    monkeypatch.setattr(branding_logger, "disabled", False)
    branding_logger.setLevel(logging.INFO)
    branding_logger.addHandler(records)
    try:
        await _run_loop(monkeypatch, [down, down, down, first, down], index_current=lambda: True)
    finally:
        branding_logger.removeHandler(records)
        branding_logger.setLevel(previous_level)
    messages = [(r.levelno, r.getMessage()) for r in records.records]
    assert [level for level, _ in messages].count(logging.WARNING) == 2
    assert [text for _, text in messages].count("Branding refresh recovered") == 1


async def test_without_a_render_the_loop_only_follows_the_database(monkeypatch, restore_branding):
    first = branding.get_cached_branding()
    renamed = dataclasses.replace(first, name="Renamed by an admin elsewhere")
    index_current = MagicMock(return_value=True)
    renders, index = await _run_loop(monkeypatch, [renamed], index_current=index_current, web=False)
    assert branding.get_cached_branding() == renamed
    # The worker serves no page: it renders nothing and asks nothing about one.
    assert renders.call_count == index.call_count == index_current.call_count == 0


async def test_the_worker_loads_the_branding_and_keeps_it_current(monkeypatch, restore_branding):
    from app import worker

    stored = dataclasses.replace(branding.get_cached_branding(), name="Acme Security")

    @asynccontextmanager
    async def session():
        yield MagicMock()

    async def read(_db):
        branding._cached_branding = stored
        return stored

    kept_current = asyncio.Event()

    async def keep_current(render=None):
        kept_current.set()
        await asyncio.Event().wait()

    async def idle(draining=None):
        await asyncio.Event().wait()

    monkeypatch.setattr("app.core.database.async_session", session)
    monkeypatch.setattr(branding, "refresh_branding_cache", read)
    monkeypatch.setattr(branding, "keep_branding_current", keep_current)
    monkeypatch.setattr("app.services.langfuse_tracing._seed_otel_exporter_limits_from_env", lambda: None)
    monkeypatch.setattr("app.core.executor.configure_default_executor", lambda _name: None)
    monkeypatch.setattr("app.core.executor.shutdown_default_executor", lambda: None)
    monkeypatch.setattr("app.core.parse_offload.shutdown_offload_executors", lambda: None)
    monkeypatch.setattr("app.core.loop_watchdog.start_loop_watchdog", lambda _name: None)
    monkeypatch.setattr("app.core.loop_watchdog.stop_loop_watchdog", lambda _name: None)
    monkeypatch.setattr("app.core.schema_readiness.wait_for_schema_head", AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.licence_verdict.accept_licensed_verdicts", lambda: None)
    monkeypatch.setattr("app.services.orchestration_reconciler.reconciler_loop", idle)
    monkeypatch.setattr("app.services.orchestration_reconciler.stop_reconciler", lambda: None)
    monkeypatch.setattr("app.services.custom_tool_executor.warn_sandbox_mode_at_startup", AsyncMock())
    monkeypatch.setattr("app.services.tracing.dispatcher.shutdown_tracing_dispatcher", AsyncMock())
    monkeypatch.setattr(worker, "close_queue", AsyncMock())
    monkeypatch.setattr(worker, "_document_processing_semaphore", None)

    await worker.startup({})
    task = worker._branding_task
    try:
        # Loaded before the reconciler starts runs that put the name in prompts.
        assert branding.get_cached_branding().name == "Acme Security"
        await asyncio.wait_for(kept_current.wait(), 1)
        assert task is not None and not task.done()
    finally:
        await worker.shutdown({})
    assert task.cancelled()
    assert worker._branding_task is None


def test_the_worker_loads_the_branding_before_it_starts_runs():
    startup = function_node(APP_DIR / "worker.py", "startup")
    assert min(call_lines(startup, "_prime_branding")) < min(call_lines(startup, "reconciler_loop"))
