"""#2583 point 3: a PIR score filter on a Vulnerability or a Report can never match."""

from __future__ import annotations

import json

import pytest

from app.agents.opencti_pir_desk import PIR_DESK_ASSIGNMENTS
from app.services import ctem_platform_sources as sources
from app.services import opencti_trigger_provisioning as prov


def test_vulnerability_and_report_are_refused_with_the_reason() -> None:
    reason = prov.pir_source_refusal(["Report", "Intrusion-Set", "Vulnerability"])
    assert reason is not None
    assert "['Report', 'Vulnerability']" in reason
    assert "source of a relationship" in reason


def test_threat_types_can_be_pir_sources() -> None:
    assert prov.pir_source_refusal(["Intrusion-Set", "Campaign", "Malware"]) is None
    assert prov.pir_source_refusal([]) is None
    assert prov.pir_source_refusal(None) is None


def _provision(**raw: object) -> dict[str, object] | None:
    return prov.read_provision_spec({"provider": "opencti", "provision": raw})


@pytest.mark.parametrize("mode", [None, "pir_playbook"])
def test_validation_refuses_a_pir_score_filter_on_a_vulnerability(mode: str | None) -> None:
    raw: dict[str, object] = {"entity_types": ["Vulnerability"], "organization_context": ["pir_ids"]}
    if mode:
        raw["mode"] = mode
    with pytest.raises(prov.ProvisionSpecError, match="cannot be filtered on a PIR score"):
        _provision(**raw)


def test_a_linked_pir_playbook_takes_vulnerabilities_and_reports() -> None:
    spec = _provision(
        mode="pir_playbook",
        pir_relation="linked",
        entity_types=["Report", "Vulnerability"],
        organization_context=["pir_ids"],
    )
    assert spec is not None and spec["pir_relation"] == "linked"


def test_linked_is_refused_outside_a_pir_playbook_and_unknown_values_too() -> None:
    with pytest.raises(prov.ProvisionSpecError, match="pir_relation linked"):
        _provision(pir_relation="linked", entity_types=["Vulnerability"], organization_context=["pir_ids"])
    with pytest.raises(prov.ProvisionSpecError, match="pir_relation: use one of"):
        _provision(mode="pir_playbook", pir_relation="near", organization_context=["pir_ids"])


def test_a_linked_playbook_listens_to_new_relationships_without_a_score_filter() -> None:
    spec = {
        "pir_ids": ["p1"],
        "pir_min_score": 1,
        "pir_relation": "linked",
        "entity_types": ["Vulnerability"],
    }
    entry, _ = prov.pir_playbook_nodes(spec, "notifier", "member")
    assert entry["create_rel"] is True and entry["create"] is False and entry["update"] is False
    filters = json.loads(entry["filters"])
    assert all(f["key"] != ["pir_score"] for f in filters["filters"])
    assert {"key": ["entity_type"], "values": ["Vulnerability"], "operator": "eq", "mode": "or"} in filters["filters"]


def test_the_new_vulnerability_playbook_is_linked_and_the_threat_one_flagged() -> None:
    vuln = sources._opencti_pir_spec("new_vulnerability")
    threat = sources._opencti_pir_spec("relevant_threat")
    assert vuln["pir_relation"] == "linked" and vuln["entity_types"] == ["Vulnerability"]
    assert threat["pir_relation"] == "flagged" and "Vulnerability" not in threat["entity_types"]


def test_every_seeded_opencti_pir_spec_is_valid() -> None:
    """The seeded PIR desk triggers read reports and vulnerabilities through a linked relationship
    or, for a report, through containment (a live trigger scoped to the flagged entities)."""
    for assignment in PIR_DESK_ASSIGNMENTS:
        config = assignment.get("trigger_config") or {}
        spec = prov.read_provision_spec(config)
        if spec and spec["entity_types"] and set(spec["entity_types"]) & set(prov.NOT_PIR_SOURCE_TYPES):
            linked = spec["mode"] == prov.PIR_PLAYBOOK_MODE and spec["pir_relation"] == prov.PIR_LINKED
            contained = spec["mode"] == prov.TRIGGER_MODE and spec["pir_relation"] == prov.PIR_CONTAINED
            assert linked or contained, assignment["name"]
