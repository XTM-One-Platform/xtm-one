"""Legacy Threat response holders retired once Signals sources replace them (#2583 point 4)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import opencti_trigger_provisioning as prov
from tests.test_opencti_trigger_provisioning import FakeOpenCTI

NOW = datetime(2026, 11, 27, 11, 0, tzinfo=UTC)


def _fake_with_objects() -> FakeOpenCTI:
    gql = FakeOpenCTI()
    gql.triggers["t-old"] = {"name": "XTM One - CTEM loop - New relevant threats - Trigger", "notifiers": []}
    gql.triggers["t-older"] = {"name": "XTM One - Threat response - New intrusion sets", "notifiers": []}
    gql.triggers["t-keep"] = {"name": "XTM One - Signals - OpenCTI live listener - Trigger", "notifiers": []}
    gql.playbooks["p-old"] = {"name": "XTM One - CTEM loop - Threat activity flagged in a PIR - Playbook"}
    gql.notifiers["n-old"] = {"name": "XTM One - CTEM loop - New relevant threats - Notifier"}
    gql.notifiers["n-keep"] = {"name": "XTM One - Signals - OpenCTI live listener - Notifier"}
    return gql


def test_legacy_names_are_the_threat_response_holders_only() -> None:
    assert prov.is_legacy_name("XTM One - Threat response - New intrusion sets")
    assert prov.is_legacy_name("XTM One - CTEM loop - New relevant threats")
    assert prov.is_legacy_name("XTM One - CTEM loop - Threat activity flagged in a PIR - Playbook")
    assert not prov.is_legacy_name("XTM One - Signals - OpenCTI live listener - Trigger")
    assert not prov.is_legacy_name("XTM One - CTEM loop - New KEV or EPSS jump - Trigger")
    assert not prov.is_legacy_name(None)


@pytest.mark.asyncio
async def test_dry_run_lists_then_removal_deletes_only_the_legacy_objects() -> None:
    gql = _fake_with_objects()
    found = await prov.legacy_objects(gql)
    assert {o["id"] for o in found} == {"t-old", "t-older", "p-old", "n-old"}
    # Triggers and playbooks come before the notifiers they post to.
    assert [o["root"] for o in found][-1] == "notifiers"
    assert "t-keep" in gql.triggers and "n-keep" in gql.notifiers
    errors = await prov.remove_legacy_objects(gql, found)
    assert errors == []
    assert set(gql.triggers) == {"t-keep"} and set(gql.notifiers) == {"n-keep"} and not gql.playbooks
    # Already gone: tolerated.
    assert await prov.remove_legacy_objects(gql, found) == []


@pytest.mark.asyncio
async def test_a_retired_holder_removes_its_objects_and_keeps_a_tombstone(monkeypatch: pytest.MonkeyPatch) -> None:
    gql = FakeOpenCTI()
    gql.triggers["t1"] = {"name": "XTM One - CTEM loop - New relevant threats - Trigger", "notifiers": ["n1"]}
    gql.notifiers["n1"] = {"name": "XTM One - CTEM loop - New relevant threats - Notifier"}

    async def connection(_db: object, _assignment: object, _agent: object) -> tuple[FakeOpenCTI, str, str, str]:
        return gql, "i1", "https://x", "s3cret"

    monkeypatch.setattr(prov, "_connection", connection)
    assignment = SimpleNamespace(
        id="a1",
        name="New relevant threat",
        enabled=True,
        trigger_config={
            "provider": "opencti",
            "provision": {"fleet": "CTEM loop", "purpose": "New relevant threats", "entity_types": ["Campaign"]},
            prov.STATE_KEY: {"status": "provisioned", "trigger_id": "t1", "notifier_id": "n1", "integration_id": "i1"},
        },
    )
    state = await prov.provision_one(None, assignment, SimpleNamespace(), now=NOW, retired=True)
    assert not gql.triggers and not gql.notifiers
    assert state == {"status": prov.RETIRED, "integration_id": "i1", "checked_at": NOW.isoformat()}
    assert assignment.trigger_config[prov.STATE_KEY]["status"] == prov.RETIRED
    # The provision block stays declared: the holder comes back if its source goes off.
    assert assignment.trigger_config["provision"]["purpose"] == "New relevant threats"


def test_the_retired_holders_are_the_two_threat_response_intakes() -> None:
    from app.agents.builtin_agents import CTEM_F1_PIR_INTAKE, CTEM_F1_THREAT_INTAKE
    from app.services.ctem_platform_sources import HOLDER_ASSIGNMENTS, RETIRED_HOLDERS

    assert set(RETIRED_HOLDERS) == {
        ("ctem-threat-response-orchestrator", CTEM_F1_THREAT_INTAKE),
        ("ctem-threat-response-orchestrator", CTEM_F1_PIR_INTAKE),
    }
    assert set(RETIRED_HOLDERS) <= HOLDER_ASSIGNMENTS
