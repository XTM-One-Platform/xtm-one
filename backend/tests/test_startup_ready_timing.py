from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

import app.main as main_module


async def _noop_async(*args, **kwargs) -> None:
    return None


class _DummyDBSession:
    async def commit(self) -> None:
        return None


class _DummySessionContext:
    async def __aenter__(self) -> _DummyDBSession:
        return _DummyDBSession()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def _dummy_async_session() -> _DummySessionContext:
    return _DummySessionContext()


class _DummyFileStorage:
    @property
    def client(self) -> object:
        return object()


@pytest.mark.asyncio
async def test_startup_marks_ready_before_optional_warmups(monkeypatch):
    """Core startup should open the gate before optional warmups run."""
    # Core startup dependencies
    monkeypatch.setattr("app.core.migrations.run_migrations", _noop_async)

    @asynccontextmanager
    async def _bootstrap_lock():
        yield

    monkeypatch.setattr("app.core.migrations.bootstrap_lock", _bootstrap_lock)
    monkeypatch.setattr(main_module, "_bootstrap_platform_data", _noop_async)
    monkeypatch.setattr("app.core.ann_index_builder.build_ann_indexes", _noop_async)
    monkeypatch.setattr("app.services.tracing.dispatcher.init_tracing_dispatcher", _noop_async)
    monkeypatch.setattr("app.services.file_storage.file_storage", _DummyFileStorage())
    monkeypatch.setattr(
        "app.services.custom_tool_executor.warn_sandbox_mode_at_startup", _noop_async
    )
    monkeypatch.setattr(main_module, "_autonomous_loop", _noop_async)

    # Optional post-ready warmups
    observed: dict[str, bool] = {}

    async def _start_discord_gateway() -> None:
        observed["discord"] = main_module._startup_done.is_set()

    async def _init_telemetry(_db) -> None:
        observed["telemetry"] = main_module._startup_done.is_set()

    monkeypatch.setattr("app.channels.discord.start_discord_gateway", _start_discord_gateway)
    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings", _noop_async
    )
    monkeypatch.setattr("app.services.branding.refresh_branding_cache", _noop_async)
    monkeypatch.setattr("app.services.telemetry.init_telemetry", _init_telemetry)
    monkeypatch.setattr("app.core.database.async_session", _dummy_async_session)
    monkeypatch.setattr(main_module, "_refresh_openapi_branding", lambda: None)
    monkeypatch.setattr(main_module, "_refresh_index_html", lambda: None)

    original_event = main_module._startup_done
    main_module._startup_done = main_module.asyncio.Event()
    try:
        await main_module._do_all_startup_work()
    finally:
        main_module._startup_done = original_event

    assert observed == {"discord": True, "telemetry": True}
