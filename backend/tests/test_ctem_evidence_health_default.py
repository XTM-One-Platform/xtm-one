# SPDX-License-Identifier: Apache-2.0

"""The evidence health pass on by default on the single connected OpenCRQ integration (#1344).

``resolve_pass`` is the one rule the signal sources badge (``bound_sources``)
and the tick (``scheduled_pass``) read, so paths 9 and 12 never read Connected
when the pass would skip, and never read Not connected while OpenCRQ is connected.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_evidence_health as health
from app.services import ctem_signal_sources as sources
from app.services import organization_context as oc

ONE = str(uuid.uuid4())
TWO = str(uuid.uuid4())


def _settings(**kw: Any) -> oc.EvidenceHealthPass:
    return oc.parse_profile({"evidence_health": kw} if kw else {}).evidence_health


def test_the_switch_is_unset_by_default() -> None:
    assert _settings().enabled is None


@pytest.mark.parametrize(
    ("settings", "connected", "expected"),
    [
        ({}, [], (health.PASS_NONE, "")),
        ({}, [ONE], (health.PASS_ON, ONE)),
        # The legacy stored default (off, none declared) reads as unset.
        ({"enabled": False}, [ONE], (health.PASS_ON, ONE)),
        ({}, [ONE, TWO], (health.PASS_OFF, "")),
        ({"enabled": False, "integration": ONE}, [ONE], (health.PASS_OFF, "")),
        # #1937: an explicit off from the path sources panel names the integration the pass ran on,
        # so it holds with one connected and none declared before; on again runs on that one.
        ({"enabled": False, "integration": ONE}, [ONE, TWO], (health.PASS_OFF, "")),
        ({"enabled": True, "integration": ONE}, [ONE], (health.PASS_ON, ONE)),
        ({"enabled": True, "integration": ONE}, [ONE, TWO], (health.PASS_ON, ONE)),
        ({"integration": ONE}, [ONE], (health.PASS_ON, ONE)),
        # A declared integration no longer connected is never swapped for another one.
        ({"enabled": True, "integration": TWO}, [ONE], (health.PASS_OFF, "")),
    ],
)
def test_resolve_pass(settings: dict[str, Any], connected: list[str], expected: tuple[str, str]) -> None:
    assert health.resolve_pass(_settings(**settings), connected) == expected


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> list[Any]:
        return self._rows


class _Db:
    """Answers the connected integrations query, then the assignments query (no OpenCTI here)."""

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.rows = rows

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self.rows)


def _patch(monkeypatch: pytest.MonkeyPatch, profile: dict[str, Any], connected: list[str]) -> None:
    from app.api.v1 import helpers
    from app.services import organizations

    async def _visible(db: Any, user_id: Any) -> Any:
        from sqlalchemy import true

        return true()

    async def _primary(db: Any) -> Any:
        return SimpleNamespace(id=uuid.uuid4())

    async def _version(db: Any, organization_id: Any) -> Any:
        return SimpleNamespace(profile=profile)

    async def _ids(db: Any) -> list[str]:
        return list(connected)

    monkeypatch.setattr(helpers, "integration_visible_filter", _visible)
    monkeypatch.setattr(organizations, "get_primary", _primary)
    monkeypatch.setattr(oc, "current_version", _version)
    monkeypatch.setattr(health, "connected_integration_ids", _ids)


async def _bound(
    monkeypatch: pytest.MonkeyPatch, profile: dict[str, Any], connected: list[str]
) -> sources.BoundSources:
    _patch(monkeypatch, profile, connected)
    return await sources.bound_sources(_Db([(i, "opencrq") for i in connected]), uuid.uuid4())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_opencrq_connected_and_the_pass_unset_reads_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    bound = await _bound(monkeypatch, {}, [ONE])
    assert ("opencrq", "pass") in bound and ("opencrq", "pass") not in bound.off
    assert sources.path_status(sources.path_sources("control_decay", bound)) == "connected"
    assert sources.path_status(sources.path_sources("blind_spot", bound)) == "connected"


@pytest.mark.asyncio
async def test_opencrq_connected_and_the_pass_turned_off_reads_off(monkeypatch: pytest.MonkeyPatch) -> None:
    bound = await _bound(monkeypatch, {"evidence_health": {"enabled": False, "integration": ONE}}, [ONE])
    assert ("opencrq", "pass") not in bound and ("opencrq", "pass") in bound.off
    assert sources.path_status(sources.path_sources("control_decay", bound)) == "off"


@pytest.mark.asyncio
async def test_several_opencrq_integrations_and_none_declared_read_off(monkeypatch: pytest.MonkeyPatch) -> None:
    bound = await _bound(monkeypatch, {}, [ONE, TWO])
    assert sources.path_status(sources.path_sources("blind_spot", bound)) == "off"


@pytest.mark.asyncio
async def test_no_opencrq_integration_reads_not_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    bound = await _bound(monkeypatch, {}, [])
    assert sources.path_status(sources.path_sources("control_decay", bound)) == "not_connected"


@pytest.mark.asyncio
async def test_a_connected_attack_surface_provider_with_the_pass_off_reads_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {}, [])
    bound = await sources.bound_sources(_Db([(ONE, "shodan")]), uuid.uuid4())  # type: ignore[arg-type]
    assert ("shodan", "pass") in bound.off and ("shodan", "pass") not in bound
    assert ("gti", "pass") not in bound.off
    assert sources.path_status(sources.path_sources("surface_drift", bound)) == "off"


def _reader_platform(monkeypatch: pytest.MonkeyPatch, platform: dict[str, set[str]]) -> None:
    """The caller sees no integration; the platform holds *platform* (an administrator's)."""

    async def _platform(db: Any, providers: tuple[str, ...]) -> dict[str, set[str]]:
        return {p: ids for p, ids in platform.items() if p in providers}

    monkeypatch.setattr(sources, "platform_connected_ids", _platform)


@pytest.mark.asyncio
async def test_a_reader_reads_off_on_an_attack_surface_pass_an_administrator_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, {}, [])
    _reader_platform(monkeypatch, {"shodan": {ONE}})
    bound = await sources.bound_sources(_Db([]), uuid.uuid4())  # type: ignore[arg-type]
    assert ("shodan", "listener") not in bound
    assert ("shodan", "pass") in bound.off
    assert sources.path_status(sources.path_sources("surface_drift", bound)) == "off"


@pytest.mark.asyncio
async def test_a_reader_reads_connected_on_an_attack_surface_pass_that_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {"attack_surface": {"enabled": True, "integration": ONE}}, [])
    _reader_platform(monkeypatch, {"shodan": {ONE}})
    bound = await sources.bound_sources(_Db([]), uuid.uuid4())  # type: ignore[arg-type]
    assert sources.path_status(sources.path_sources("surface_drift", bound)) == "connected"


@pytest.mark.asyncio
async def test_a_reader_reads_the_evidence_health_pass_of_the_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {}, [ONE])
    _reader_platform(monkeypatch, {"opencrq": {ONE}})
    bound = await sources.bound_sources(_Db([]), uuid.uuid4())  # type: ignore[arg-type]
    assert sources.path_status(sources.path_sources("control_decay", bound)) == "connected"


@pytest.mark.asyncio
async def test_the_tick_runs_on_the_single_connected_integration_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ctem_case_loop

    _patch(monkeypatch, {}, [ONE])
    seen: dict[str, Any] = {}

    async def _due(hours: int) -> bool:
        return True

    async def _reader(db: Any, profile: Any) -> Any:
        seen["integration"] = profile.evidence_health.integration
        return object()

    async def _ok(db: Any, **kw: Any) -> dict[str, Any]:
        return {"pending": ctem_case_loop.PendingEvents(), "counts": {"new": 0}}

    async def _publish(pending: Any) -> None:
        return None

    class _TickDb:
        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(health, "pass_due", _due)
    monkeypatch.setattr(health, "reader_for", _reader)
    monkeypatch.setattr(health, "run_pass", _ok)
    monkeypatch.setattr(ctem_case_loop, "publish_pending", _publish)
    assert await health.scheduled_pass(_TickDb()) == {"new": 0}  # type: ignore[arg-type]
    assert seen["integration"] == ONE


@pytest.mark.asyncio
async def test_the_tick_stays_off_with_several_integrations_and_none_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {}, [ONE, TWO])
    assert await health.scheduled_pass(object()) is None  # type: ignore[arg-type]
