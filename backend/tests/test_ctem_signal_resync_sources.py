# SPDX-License-Identifier: Apache-2.0

"""#2745: the purge dialog's per-source resync, each route or pass ticked or not, windows in hours or days.

- the default plan is today's resync (every route, the same windows);
- an unticked route is never queried, an unticked pass never runs;
- a window of 1 hour reads only what changed in the last hour;
- the days-only ``resync_windows`` payload keeps working unchanged;
- bad entries are refused before any job.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_signal_resync as resync

NOW = datetime(2026, 11, 26, 17, 0, tzinfo=UTC)


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _one_source(monkeypatch: pytest.MonkeyPatch, gql: Any) -> None:
    async def _sources(db: Any) -> list[Any]:
        return [SimpleNamespace(integration_id="i1", run_as_user_id="u1")]

    async def _conn(db: Any, integration_id: str, user_id: Any) -> Any:
        return gql, integration_id, "secret"

    async def _off(sessions: Any) -> set[str]:
        return set()

    monkeypatch.setattr(resync, "_opencti_sources", _sources)
    monkeypatch.setattr(resync, "_excluded_listener_paths", _off)
    monkeypatch.setattr("app.services.opencti_trigger_provisioning.integration_connection", _conn)


async def _note(*a: Any, **k: Any) -> bool:
    return True


# ── the plan ───────────────────────────────────────────────────────────


def test_the_default_plan_is_todays_resync() -> None:
    planned = resync.plan(None, {})
    assert planned["skip"] == []
    assert planned["windows"] == resync.default_windows()
    assert planned["spans"]["vulnerability"] == 7 * 24 and planned["spans"]["malware"] == 30 * 24
    assert planned["spans"]["aikido"] is None


def test_a_window_in_hours_and_an_unticked_source() -> None:
    planned = resync.plan(
        {"malware": 10},
        {
            "vulnerability": {"enabled": True, "window": 1, "unit": "hours"},
            "intrusion_set": {"enabled": False},
            "attack_surface": {"enabled": False},
            "aikido": {"window": 2, "unit": "days"},
            "github": {"window": None},
        },
    )
    assert planned["spans"]["vulnerability"] == 1
    assert "vulnerability" not in planned["windows"]  # not a whole number of days
    assert "intrusion_set" not in planned["spans"] and "intrusion_set" not in planned["windows"]
    assert planned["skip"] == ["intrusion_set", "attack_surface"]
    assert planned["spans"]["malware"] == 240 and planned["windows"]["malware"] == 10
    assert planned["spans"]["aikido"] == 48 and planned["spans"]["github"] is None


@pytest.mark.parametrize(
    "sources",
    [
        {"vulnerability": {"window": 0, "unit": "hours"}},
        {"vulnerability": {"window": 365 * 24 + 1, "unit": "hours"}},
        {"vulnerability": {"window": 3, "unit": "weeks"}},
        {"vulnerability": {"window": None}},  # a world route always has a window
        {"vulnerability": {"enabled": "no"}},
        {"vulnerability": {"window": 2.5}},
        {"vulnerability": {"extra": 1}},
        {"vulnerability": 3},
        {"attack_surface": {"window": 3}},  # a pass reads the current state
        {"gitlab": {"enabled": False}},  # no listing: nothing to tick
        {"nope": {"enabled": False}},
    ],
)
def test_a_bad_source_entry_is_refused(sources: dict[str, Any]) -> None:
    with pytest.raises(resync.WindowError):
        resync.plan(None, sources)


def test_the_route_reports_its_window_in_days_or_hours() -> None:
    assert resync._route("vulnerability", {"vulnerability": 168})["window_days"] == 7
    one_hour = resync._route("vulnerability", {"vulnerability": 1})
    assert one_hour["window_days"] is None and one_hour["window_hours"] == 1


# ── the routes ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_hour_reads_only_the_last_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[dict[str, Any]] = []

    async def _gql(query: str, variables: dict[str, Any]) -> tuple[dict[str, Any], None]:
        asked.append(variables)
        return {"stixCoreObjects": {"edges": [], "pageInfo": {"hasNextPage": False}}}, None

    _one_source(monkeypatch, _gql)
    spans = resync.plan(None, {"vulnerability": {"window": 1, "unit": "hours"}})["spans"]
    await resync.resync_world(lambda: _Session(), windows=spans, now=NOW, note=_note)
    vulns = next(v for v in asked if v["types"] == ["Vulnerability"])
    assert vulns["filters"]["filters"][0]["values"] == ["2026-11-26T16:00:00+00:00"]


@pytest.mark.asyncio
async def test_an_unticked_world_route_is_never_queried(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    async def _gql(query: str, variables: dict[str, Any]) -> tuple[dict[str, Any], None]:
        asked.append(variables["types"][0])
        return {"stixCoreObjects": {"edges": [], "pageInfo": {"hasNextPage": False}}}, None

    _one_source(monkeypatch, _gql)
    spans = resync.plan(None, {"vulnerability": {"enabled": False}})["spans"]
    routes = await resync.resync_world(lambda: _Session(), windows=spans, now=NOW, note=_note)
    assert "Vulnerability" not in asked and "Intrusion-Set" in asked
    assert routes["vulnerability"]["skipped"] == resync.NOT_SELECTED and routes["vulnerability"]["read"] == 0


@pytest.mark.asyncio
async def test_unticked_passes_do_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ctem_attack_surface, ctem_evidence_health, ctem_inventory_staleness

    ran: list[str] = []
    for name, module in (
        ("surface", ctem_attack_surface),
        ("evidence", ctem_evidence_health),
        ("inventory", ctem_inventory_staleness),
    ):

        async def _release(name: str = name) -> None:
            ran.append(f"release {name}")

        async def _pass(db: Any, name: str = name) -> Any:
            ran.append(f"pass {name}")
            return {"recorded": 1}

        monkeypatch.setattr(module, "release_pass_slot", _release)
        monkeypatch.setattr(module, "scheduled_pass", _pass)

    out = await resync.resync_passes(lambda: _Session(), skip={"attack_surface", "evidence_health"})
    assert ran == ["release inventory", "pass inventory"]
    assert out["attack_surface"]["skipped"] == resync.NOT_SELECTED
    assert out["evidence_health"]["skipped"] == resync.NOT_SELECTED


def _stub_steps(monkeypatch: pytest.MonkeyPatch, called: list[str], seen: dict[str, Any]) -> None:
    def _step(key: str) -> Any:
        async def _run(sessions: Any, *, windows: Any, **kw: Any) -> dict[str, Any]:
            called.append(key)
            seen[key] = windows
            return {**resync._route(key, windows), "read": 1, "recorded": 1}

        return _run

    async def _world(sessions: Any, *, windows: Any) -> dict[str, Any]:
        called.append("world")
        seen["world"] = windows
        return {"vulnerability": {**resync._route("vulnerability", windows), "recorded": 1}}

    async def _passes(sessions: Any, *, skip: Any = ()) -> dict[str, Any]:
        seen["passes_skip"] = set(skip)
        return {}

    async def _by_path(sessions: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(resync, "resync_world", _world)
    for key in ("pir", "third_party", "aikido", "github", "openaev_findings"):
        monkeypatch.setattr(resync, f"resync_{key}", _step(key))
    monkeypatch.setattr(resync, "resync_passes", _passes)
    monkeypatch.setattr(resync, "by_trigger_path", _by_path)


async def _progress(key: str, n: int) -> None:
    return None


@pytest.mark.asyncio
async def test_unticked_sources_are_not_read_and_the_others_are(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    seen: dict[str, Any] = {}
    _stub_steps(monkeypatch, called, seen)
    planned = resync.plan(
        None, {"aikido": {"enabled": False}, "pir": {"enabled": False}, "attack_surface": {"enabled": False}}
    )
    out = await resync.resync(
        sessions=None,
        actor_id="admin-1",  # type: ignore[arg-type]
        progress=_progress,
        windows=planned["windows"],
        spans=planned["spans"],
        skip=planned["skip"],
    )
    assert called == ["world", "third_party", "github", "openaev_findings"]
    assert out["routes"]["aikido"]["skipped"] == resync.NOT_SELECTED
    assert out["routes"]["pir"]["skipped"] == resync.NOT_SELECTED
    assert seen["passes_skip"] == {"aikido", "pir", "attack_surface"}
    assert out["skip"] == ["aikido", "attack_surface", "pir"]


@pytest.mark.asyncio
async def test_the_days_only_payload_still_reads_every_route(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    seen: dict[str, Any] = {}
    _stub_steps(monkeypatch, called, seen)
    out = await resync.resync(
        sessions=None,
        actor_id="admin-1",  # type: ignore[arg-type]
        progress=_progress,
        windows={"vulnerability": 3},
    )
    assert called == ["world", "pir", "third_party", "aikido", "github", "openaev_findings"]
    assert seen["world"]["vulnerability"] == 72 and seen["world"]["malware"] == 720
    assert out["windows"]["vulnerability"] == 3 and out["skip"] == []
    assert seen["passes_skip"] == set()


@pytest.mark.asyncio
async def test_every_world_route_unticked_skips_the_opencti_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    seen: dict[str, Any] = {}
    _stub_steps(monkeypatch, called, seen)
    world = {w.key: {"enabled": False} for w in resync.WINDOWS if w.entity_type}
    planned = resync.plan(None, world)
    out = await resync.resync(
        sessions=None,
        actor_id="admin-1",  # type: ignore[arg-type]
        progress=_progress,
        windows=planned["windows"],
        spans=planned["spans"],
        skip=planned["skip"],
    )
    assert "world" not in called
    assert all(out["routes"][k]["skipped"] == resync.NOT_SELECTED for k in world)
