"""#2583 comment point 5: path 11 is fed by the OpenCTI live listener source.

The source provisions a trigger of its own for reports and incidents naming a tracked
organization (scoped to their identities, the vulnerability intake untouched); the receiver
resolves the organizations a delivery names from that trigger's state, and the inbox drain
records one path 11 signal per organization.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from app.services import ctem_platform_sources as sources
from app.services import ctem_signal_inbox as inbox
from app.services import ctem_signal_sources as catalog
from app.services import opencti_trigger_provisioning as prov
from app.services.organization_context import SignalSourcesSettings

from .test_ctem_platform_sources import INTEGRATION, NOW, RUN_AS, FakeDb
from .test_opencti_trigger_provisioning import FakeOpenCTI

TRACKED = {"id": "org-1", "name": "Acme Hosting", "identity_id": "identity-internal", "sectors": []}


class OpenCTIWithIds(FakeOpenCTI):
    async def __call__(self, query: str, variables: dict[str, Any] | None = None) -> tuple[dict[str, Any], str | None]:
        if "XtmStandardId" in query:
            return {"stixCoreObject": {"id": (variables or {})["id"], "standard_id": "identity--acme"}}, None
        found: tuple[dict[str, Any], str | None] = await super().__call__(query, variables)
        return found


def _excluded(excluded: dict[str, list[str]]) -> dict[str, set[tuple[str, ...]]]:
    profile = type("P", (), {"signal_sources": SignalSourcesSettings(excluded=excluded, included={})})()
    return catalog.parse_excluded(profile)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    octi = OpenCTIWithIds()
    state: dict[str, Any] = {"excluded": _excluded({}), "refs": [TRACKED]}

    async def connection(_db: Any, integration_id: Any, _user_id: Any) -> tuple[Any, str, str]:
        return octi, str(integration_id), "s3cret"

    async def run_as_ok(_db: Any, _provider: str, _user_id: Any) -> None:
        return None

    async def excluded_sources(_db: Any, _org: Any) -> dict[str, set[tuple[str, ...]]]:
        current: dict[str, set[tuple[str, ...]]] = state["excluded"]
        return current

    async def with_tracked(_db: Any, spec: dict[str, Any]) -> dict[str, Any]:
        return {**spec, "organization_refs": list(state["refs"])}

    monkeypatch.setattr(prov, "integration_connection", connection)
    monkeypatch.setattr(prov, "with_tracked_organizations", with_tracked)
    monkeypatch.setattr(sources, "_run_as_reason", run_as_ok)
    monkeypatch.setattr(catalog, "excluded_sources", excluded_sources)
    return {"octi": octi, "state": state}


def _third_party_triggers(octi: FakeOpenCTI) -> list[dict[str, Any]]:
    return [t for t in octi.triggers.values() if "Third-party" in t["name"]]


async def test_the_listener_source_provisions_a_scoped_third_party_trigger(world: dict[str, Any]) -> None:
    octi = world["octi"]
    row = await sources.enable(
        FakeDb("opencti"),
        provider="opencti",
        kind="listener",
        integration_id=INTEGRATION,
        run_as_user_id=RUN_AS,
        now=NOW,
    )
    assert sources.status_of(row) == ("connected", None)
    [trigger] = _third_party_triggers(octi)
    filters = json.loads(trigger["filters"])
    assert {"key": ["entity_type"], "values": ["Incident", "Report"], "operator": "eq", "mode": "or"} in filters[
        "filters"
    ]
    scoped = [f for g in filters["filterGroups"] for f in g["filters"] if f["key"] == ["objects"]]
    assert scoped and "identity-internal" in scoped[0]["values"], "only reports naming a tracked organization"
    main = [t for t in octi.triggers.values() if t is not trigger]
    assert main and all("Report" not in json.dumps(t["filters"]) for t in main), "vulnerability intake not widened"
    sub = row.state["paths"][sources.THIRD_PARTY_PATH]
    assert sub["organizations"][0]["identity_ids"] == ["identity--acme", "identity-internal"]
    held = [(o["kind"], o["id"]) for o in sources.provisioned_objects(row.state)]
    assert ("trigger", sub["trigger_id"]) in held


async def test_path_11_off_or_nothing_tracked_removes_the_third_party_trigger(world: dict[str, Any]) -> None:
    octi, state = world["octi"], world["state"]
    db = FakeDb("opencti")
    row = await sources.enable(
        db, provider="opencti", kind="listener", integration_id=INTEGRATION, run_as_user_id=RUN_AS, now=NOW
    )
    assert _third_party_triggers(octi)
    state["excluded"] = _excluded({"third_party_event": ["opencti:listener"]})
    row.state = await sources._reconcile(db, row, NOW + timedelta(hours=1))
    assert not _third_party_triggers(octi) and sources.THIRD_PARTY_PATH not in (row.state.get("paths") or {})
    assert sources.status_of(row) == ("connected", None), "the other paths still deliver"
    state["excluded"], state["refs"] = _excluded({}), []
    row.state = await sources._reconcile(db, row, NOW + timedelta(hours=2))
    assert not _third_party_triggers(octi), "no tracked organization: nothing to scope to"


async def test_every_other_path_off_keeps_path_11_and_reads_connected(world: dict[str, Any]) -> None:
    octi, state = world["octi"], world["state"]
    db = FakeDb("opencti")
    row = await sources.enable(
        db, provider="opencti", kind="listener", integration_id=INTEGRATION, run_as_user_id=RUN_AS, now=NOW
    )
    state["excluded"] = _excluded(
        {
            "new_vulnerability": ["opencti:listener"],
            "relevant_threat": ["opencti:listener"],
            "credential_leak": ["opencti:listener"],
        }
    )
    row.state = await sources._reconcile(db, row, NOW + timedelta(hours=1))
    assert [t["name"] for t in octi.triggers.values()] == [t["name"] for t in _third_party_triggers(octi)]
    assert sources.status_of(row) == ("connected", None)


def test_the_receiver_names_the_tracked_organizations_of_a_delivery() -> None:
    state = {
        "paths": {
            "third_party_event": {
                "organizations": [{"id": "org-1", "identity_ids": ["identity--acme"], "sector_ids": []}]
            }
        }
    }
    report = {"type": "update", "data": {"type": "report", "object_refs": ["identity--acme", "malware--x"]}}
    assert inbox.tracked_named(state, report) == ["org-1"]
    assert inbox.tracked_named(state, {"data": {"type": "report", "object_refs": ["malware--x"]}}) == []
    assert inbox.tracked_named({}, report) == [], "a source without the path 11 trigger names nobody"


async def test_the_drain_records_path_11_for_the_named_organizations(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ctem_analyst_mark, ctem_signal_listeners

    seen: dict[str, Any] = {}

    async def note_event(*_a: Any, **_k: Any) -> bool:
        return False

    async def note_tracked(payload: Any, ids: list[str], *, verified: bool, session_factory: Any = None) -> int:
        seen.update(ids=ids, verified=verified, factory=session_factory)
        return len(ids)

    async def mark(*_a: Any, **_k: Any) -> list[str]:
        return []

    monkeypatch.setattr(ctem_signal_listeners, "note_event", note_event)
    monkeypatch.setattr(ctem_signal_listeners, "note_tracked_organizations", note_tracked)
    monkeypatch.setattr(ctem_analyst_mark, "note_event", mark)
    payload = {"data": {"type": "report"}, inbox.TRACKED_KEY: ["org-1"]}
    assert await inbox.record("opencti", str(INTEGRATION), "update", payload, verified=True) is True
    assert seen["ids"] == ["org-1"] and seen["verified"] is True
    assert seen["factory"] is not None, "recorded on the intake engine, never the web pool"
