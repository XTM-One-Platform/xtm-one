"""#2583 point 7 and the OpenAEV note (Sam, 27 Nov 2026): a provisioned listener follows the paths that are on."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.services import ctem_platform_sources as sources
from app.services import ctem_signal_sources as catalog
from app.services.organization_context import SignalSourcesSettings

from .test_ctem_platform_sources import INTEGRATION, NOW, RUN_AS, FakeDb, openaev  # noqa: F401


def _excluded(
    excluded: dict[str, list[str]], included: dict[str, list[str]] | None = None
) -> dict[str, set[tuple[str, ...]]]:
    profile = type("P", (), {"signal_sources": SignalSourcesSettings(excluded=excluded, included=included or {})})()
    return catalog.parse_excluded(profile)


def test_every_source_on_listens_to_every_type_and_event() -> None:
    assert sources.opencti_listener_types(None) == list(sources.LIVE_LISTENER_ENTITY_TYPES)
    everything_on = _excluded(
        {}, {"surface_drift": ["openaev:listener:asset_created"], "new_exposure": ["openaev:listener:finding_created"]}
    )
    assert "asset_created" in sources.openaev_events(everything_on)


def test_new_vulnerability_off_drops_vulnerability_from_the_opencti_trigger() -> None:
    excluded = _excluded({"new_vulnerability": ["opencti:listener"]})
    types = sources.opencti_listener_types(excluded)
    assert "Vulnerability" not in types and "has" not in types
    assert "Intrusion-Set" in types and "User-Account" in types


def test_every_opencti_path_off_provisions_nothing() -> None:
    excluded = _excluded(
        {
            "new_vulnerability": ["opencti:listener"],
            "relevant_threat": ["opencti:listener"],
            "credential_leak": ["opencti:listener"],
        }
    )
    assert sources.opencti_listener_types(excluded) == []


def test_openaev_events_follow_the_paths_that_are_on() -> None:
    # Default: the default-off sources (asset_created, finding_created) are off, so are their events.
    default = sources.openaev_events(_excluded({}))
    assert "asset_created" not in default and "finding_created" not in default
    assert set(sources.OPENAEV_CASE_EVENTS) <= set(default)
    assert {"asset_stale", "scenario_score_degraded", "critical_asset_reached"} <= set(default)
    off = _excluded(
        {
            "stale_asset": ["openaev:listener"],
            "proven_exploitable": ["openaev:listener"],
            "mitigation_regression": ["openaev:listener"],
            "new_exposure": ["openaev:listener"],
        }
    )
    events = sources.openaev_events(off)
    assert events == sorted(sources.OPENAEV_CASE_EVENTS), "the case events stay while any case can move"


async def test_a_path_turned_off_removes_its_openaev_trigger_one_reconcile_later(
    openaev: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"excluded": _excluded({})}

    async def excluded_sources(_db: Any, _org: Any) -> dict[str, set[tuple[str, ...]]]:
        return state["excluded"]

    monkeypatch.setattr(catalog, "excluded_sources", excluded_sources)
    db = FakeDb()
    row = await sources.enable(
        db, provider="openaev", kind="listener", integration_id=INTEGRATION, run_as_user_id=RUN_AS, now=NOW
    )
    assert "asset_stale" in row.state["trigger_ids"]
    state["excluded"] = _excluded({"stale_asset": ["openaev:listener"]})
    row.state = await sources._reconcile(db, row, NOW + timedelta(hours=1))
    assert "asset_stale" not in row.state["trigger_ids"]
    assert set(row.state["trigger_ids"].values()) == set(openaev.triggers)
    state["excluded"] = _excluded({})
    row.state = await sources._reconcile(db, row, NOW + timedelta(hours=2))
    assert "asset_stale" in row.state["trigger_ids"]


async def test_every_opencti_path_off_removes_the_trigger_and_reads_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import opencti_trigger_provisioning as prov

    from .test_opencti_trigger_provisioning import FakeOpenCTI

    octi = FakeOpenCTI()
    state = {"excluded": _excluded({})}

    async def connection(_db: Any, integration_id: Any, _user_id: Any) -> tuple[Any, str, str]:
        return octi, str(integration_id), "s3cret"

    async def run_as_ok(_db: Any, _provider: str, _user_id: Any) -> None:
        return None

    async def excluded_sources(_db: Any, _org: Any) -> dict[str, set[tuple[str, ...]]]:
        return state["excluded"]

    monkeypatch.setattr(prov, "integration_connection", connection)
    monkeypatch.setattr(sources, "_run_as_reason", run_as_ok)
    monkeypatch.setattr(catalog, "excluded_sources", excluded_sources)
    db = FakeDb("opencti")
    row = await sources.enable(
        db, provider="opencti", kind="listener", integration_id=INTEGRATION, run_as_user_id=RUN_AS, now=NOW
    )
    assert octi.triggers
    state["excluded"] = _excluded(
        {
            "new_vulnerability": ["opencti:listener"],
            "relevant_threat": ["opencti:listener"],
            "credential_leak": ["opencti:listener"],
        }
    )
    row.state = await sources._reconcile(db, row, NOW + timedelta(hours=1))
    assert not octi.triggers and not octi.notifiers, "OpenCTI stops delivering"
    assert sources.status_of(row)[0] == "off", "every path off is a choice, never an error"
    state["excluded"] = _excluded({})
    row.state = await sources._reconcile(db, row, NOW + timedelta(hours=2))
    assert sources.status_of(row) == ("connected", None) and octi.triggers
