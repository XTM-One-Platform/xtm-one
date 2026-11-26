"""The seeded CTEM loop names its OpenCTI objects after the fleet and its PIR playbook keeps its types (#2121)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.builtin_agents import CTEM_F1_ASSIGNMENTS
from app.agents.builtin_fleets import CTEM_LOOP_FLEET
from app.services import opencti_trigger_provisioning as prov


def _provisioned() -> list[dict[str, Any]]:
    return [a["trigger_config"]["provision"] for a in CTEM_F1_ASSIGNMENTS if "provision" in a.get("trigger_config", {})]


def test_the_loop_provisions_opencti_objects_under_its_own_name() -> None:
    provisions = _provisioned()
    assert provisions
    assert {p["fleet"] for p in provisions} == {CTEM_LOOP_FLEET["name"]}


def _spec(**provision: Any) -> dict[str, Any]:
    config = {
        "provider": "opencti",
        "provision": {"fleet": "CTEM loop", "purpose": "PIR flags", "organization_context": ["pir_ids"], **provision},
    }
    spec = prov.read_provision_spec(config)
    assert spec is not None
    return dict(spec, pir_ids=["pir-a"])


def _filters(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return json.loads(entry["filters"])["filters"]


def test_a_pir_playbook_keeps_only_its_declared_entity_types() -> None:
    entry, _ = prov.pir_playbook_nodes(
        _spec(mode="pir_playbook", entity_types=["Intrusion-Set", "Malware"]), "notifier", "member"
    )
    assert {"key": ["entity_type"], "values": ["Intrusion-Set", "Malware"], "operator": "eq", "mode": "or"} in (
        _filters(entry)
    )


def test_a_pir_playbook_without_types_keeps_every_flagged_entity() -> None:
    entry, _ = prov.pir_playbook_nodes(_spec(mode="pir_playbook"), "notifier", "member")
    assert [f["key"] for f in _filters(entry)] == [["pir_score"]]


class _Db:
    def __init__(self, row: Any) -> None:
        self.row = row

    async def execute(self, _stmt: Any) -> Any:
        row = self.row
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: row))

    def add(self, _row: Any) -> None:  # pragma: no cover - an existing check is never re-added
        raise AssertionError("no new check expected")


@pytest.mark.asyncio
async def test_an_existing_health_check_follows_a_renamed_fleet() -> None:
    from app.services.fleets import ensure_fleet_health_check, fleet_health_check_gate, fleet_health_check_prompt

    fleet = SimpleNamespace(id=uuid.uuid4(), name="Threat-informed exposure response", health_check=None)
    check = SimpleNamespace(
        activation_check=fleet_health_check_gate(fleet.id), prompt=fleet_health_check_prompt(fleet), ai_label=False
    )
    fleet.name = "CTEM loop"
    agent = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), is_company_managed=True)
    assert not await ensure_fleet_health_check(_Db(check), fleet, agent)  # type: ignore[arg-type]
    assert "'CTEM loop'" in check.prompt and "Threat-informed" not in check.prompt
