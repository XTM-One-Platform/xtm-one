"""The live About page checklist (redesign S1 of the fleet About page): rows only, no run, no model."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services import fleet_about
from app.services.fleet_about import evaluate_prerequisites


async def test_each_check_is_evaluated_once_in_order_and_manual_is_never_evaluated(monkeypatch):
    calls: list[str] = []

    async def connected(_db, _user, _groups, provider):
        calls.append(provider)
        return provider == "opencti"

    async def tracked(_db):
        return False

    async def current_version(_db):
        return SimpleNamespace(
            organization_id=None,
            profile={"sector": "Finance", "assets_exposure": [{"name": "laptop-1", "category": "host"}]},
        )

    monkeypatch.setattr(fleet_about, "_integration_connected", connected)
    monkeypatch.setattr(fleet_about, "_tracked_monitored", tracked)
    monkeypatch.setattr("app.services.organization_context.current_version", current_version)

    items = [
        {"check": "integration:opencti", "label": "OpenCTI", "fix_route": "/integrations"},
        {"check": "integration:openaev", "label": "OpenAEV", "fix_route": "/integrations"},
        {"check": "integration:opencti", "label": "OpenCTI again", "fix_route": "/integrations"},
        {"check": "profile:sector", "label": "Sector", "fix_route": "/settings/organization/profile"},
        # #1844: met by an asset of category ai_target only; a host asset does not count.
        {"check": "profile:ai_targets", "label": "AI targets", "fix_route": "/ctem/inventory"},
        {"check": "profile:tracked_organizations", "label": "Tracked", "fix_route": ""},
        {"check": "manual", "label": "Switch it on", "fix_route": ""},
    ]
    user = SimpleNamespace(id=uuid.uuid4())
    states = await evaluate_prerequisites(None, user, items, group_ids=[])
    assert [s.met for s in states] == [True, False, True, True, False, False, None]
    assert [s.label for s in states][2] == "OpenCTI again"
    assert calls == ["opencti", "openaev"]


async def test_ai_targets_is_met_by_an_ai_target_asset_record_the_stored_version_does_not_list(monkeypatch):
    """#1844 UX B1: once the asset records are imported, the check reads them, not the version's empty list."""
    from app.services import organization_asset_records as records

    organization_id = uuid.uuid4()

    async def current_version(_db):
        return SimpleNamespace(organization_id=organization_id, profile={"sector": "Finance", "assets_exposure": []})

    async def imported(_db, org):
        return org == organization_id

    async def profile_lists(_db, org, *, cap=5000):
        assert org == organization_id
        return [{"name": "Support chatbot model", "category": "ai_target"}], [], False

    monkeypatch.setattr("app.services.organization_context.current_version", current_version)
    monkeypatch.setattr(records, "is_imported", imported)
    monkeypatch.setattr(records, "profile_lists", profile_lists)
    items = [{"check": "profile:ai_targets", "label": "AI targets", "fix_route": "/ctem/inventory"}]
    states = await evaluate_prerequisites(None, SimpleNamespace(id=uuid.uuid4()), items, group_ids=[])
    assert [s.met for s in states] == [True]


async def test_the_automations_check_reads_the_fleet_s_automations_live(monkeypatch):
    """#10094: "Switch on the automations" is met once one is on; not evaluated without a fleet."""
    enabled = {"n": 0}

    async def counts(_db, _fleet):
        return SimpleNamespace(enabled=enabled["n"])

    monkeypatch.setattr("app.services.fleet_views.fleet_automation_counts", counts)
    items = [{"check": "automations:enabled", "label": "Switch on", "fix_route": ""}]
    user = SimpleNamespace(id=uuid.uuid4())
    fleet = SimpleNamespace(id=uuid.uuid4())
    assert [s.met for s in await evaluate_prerequisites(None, user, items, group_ids=[], fleet=fleet)] == [False]
    enabled["n"] = 2
    assert [s.met for s in await evaluate_prerequisites(None, user, items, group_ids=[], fleet=fleet)] == [True]
    assert [s.met for s in await evaluate_prerequisites(None, user, items, group_ids=[])] == [None]


async def test_no_prerequisite_reads_nothing():
    assert await evaluate_prerequisites(None, SimpleNamespace(id=uuid.uuid4()), []) == []
