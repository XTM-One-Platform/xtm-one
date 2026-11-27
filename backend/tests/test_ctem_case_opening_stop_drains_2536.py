"""Turning CTEM case opening off cancels the drains of the vertical this process runs (#2536)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services import ctem_case_opening as case_opening


async def test_stop_drains_cancels_only_the_paused_vertical(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()

    async def slow_drain(vertical: str, **_: Any) -> dict[str, int]:
        started.set()
        await asyncio.sleep(60)
        return {}

    monkeypatch.setattr(case_opening, "drain", slow_drain)
    cyber = case_opening.start_drain("cyber")
    other = case_opening.start_drain("cloud")
    await started.wait()
    assert case_opening.stop_drains("cyber") == 1
    with pytest.raises(asyncio.CancelledError):
        await cyber
    assert not other.done()
    other.cancel()
    with pytest.raises(asyncio.CancelledError):
        await other
    assert case_opening.stop_drains("cyber") == 0


async def test_turning_it_off_stops_the_running_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    stopped: list[str] = []

    def stop(vertical: str) -> int:
        stopped.append(vertical)
        return 1

    monkeypatch.setattr(case_opening, "stop_drains", stop)

    async def no_audit(*_a: Any, **_k: Any) -> None:
        return None

    row = SimpleNamespace(ctem_case_opening=None)

    async def settings(_db: Any) -> Any:
        return row

    from app.api.v1 import platform_settings_helpers
    from app.services import audit

    monkeypatch.setattr(platform_settings_helpers, "get_platform_settings", settings)
    monkeypatch.setattr(audit, "log_audit", no_audit)
    monkeypatch.setattr(case_opening, "flag_modified", lambda *_a: None)
    db = cast(AsyncSession, object())
    actor = cast(User, SimpleNamespace(id="admin"))
    assert await case_opening.set_opening(db, vertical="cyber", on=False, actor=actor) is True
    assert stopped == ["cyber"] and row.ctem_case_opening == {"cyber": False}
    assert await case_opening.set_opening(db, vertical="cyber", on=True, actor=actor) is True
    assert stopped == ["cyber"]
