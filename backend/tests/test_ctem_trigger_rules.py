# SPDX-License-Identifier: Apache-2.0
"""CTEM-08: "a vulnerability on software we run", read before any model call."""

from __future__ import annotations

from typing import Any

from app.services import ctem_signal_listeners as listeners
from app.services import ctem_trigger_rules as rules
from app.services.ctem_scope_gate import runs_in_profile
from app.services.organization_context import OrganizationProfile


def _profile() -> OrganizationProfile:
    return OrganizationProfile.model_validate(
        {
            "tech_stack": [
                {"product": "nginx", "vendor": "f5"},
                {"product": "PostgreSQL", "cpe": "cpe:2.3:a:postgresql:postgresql:16.2:*:*:*:*:*:*:*"},
            ],
            "assets_exposure": [{"name": "edge-01.example.com", "category": "host"}],
        }
    )


def _has(source: str, *, nested: bool = True) -> dict:
    data = {
        "id": "relationship--1",
        "type": "relationship",
        "relationship_type": "has",
        "source_ref": "software--1",
        "target_ref": "vulnerability--1",
    }
    if nested:
        data["extensions"] = {
            "extension-definition--ea279b3e-5c71-4632-ac08-831c66a786ba": {
                "source_value": source,
                "source_type": "Software",
                "target_type": "Vulnerability",
            }
        }
    else:
        data["source_value"] = source
    return {"type": "create", "data": data}


def test_rule_is_declared_by_the_trigger_config_only() -> None:
    assert rules.rule_of({"provider": "opencti", "runs_in_profile": "source"}) == "source"
    assert rules.rule_of({"provider": "opencti"}) == ""
    assert rules.rule_of({"runs_in_profile": "target"}) == ""
    assert rules.rule_of(None) == ""


def test_source_name_reads_the_opencti_extension_or_the_top_level() -> None:
    assert rules.source_name(_has("nginx 1.27.1")) == "nginx 1.27.1"
    assert rules.source_name(_has("nginx", nested=False)) == "nginx"
    assert rules.source_name({"data": {"type": "vulnerability"}}) == ""
    assert rules.source_name("x") == ""


def test_software_in_the_stack_starts_the_run() -> None:
    assert rules.drop_reason(_profile(), _has("nginx 1.27.1")) is None
    assert rules.drop_reason(_profile(), _has("cpe:2.3:a:f5:nginx:1.27.1:*:*:*:*:*:*:*")) is None
    assert rules.drop_reason(_profile(), _has("cpe:2.3:a:postgresql:postgresql:16.2:*:*:*:*:*:*:*")) is None


def test_an_asset_of_the_profile_starts_the_run() -> None:
    assert rules.drop_reason(_profile(), _has("EDGE-01.example.com.")) is None


def test_software_outside_the_stack_is_dropped_with_its_name() -> None:
    reason = rules.drop_reason(_profile(), _has("Apache Tomcat 10.1"))
    assert reason is not None and "Apache Tomcat 10.1" in reason and reason.startswith("runs_in_profile:")


def test_no_source_or_no_profile_is_dropped() -> None:
    assert "no relationship source" in (rules.drop_reason(_profile(), {"data": {"type": "relationship"}}) or "")
    assert "not saved" in (rules.drop_reason(None, _has("nginx")) or "")


def test_a_name_that_only_contains_a_product_as_a_substring_does_not_match() -> None:
    assert runs_in_profile(_profile(), "nginxplus-fork") == []
    assert runs_in_profile(_profile(), "nginx") == ["nginx"]


def test_the_signal_of_a_has_relationship_carries_its_source_as_software() -> None:
    row, fields = listeners.match("opencti", "create", _has("nginx 1.27.1"))
    assert row.trigger_path == "new_vulnerability"
    assert fields["ref"] == "vulnerability--1"
    assert fields["evidence"]["software"] == ["nginx 1.27.1"]


# --- PIR score thresholds (part 3) -------------------------------------------------

_EXT = "extension-definition--ea279b3e-5c71-4632-ac08-831c66a786ba"


def _flagged(kind: str, pirs: list[tuple[str, int]], *, source: str = "pir_playbook") -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": f"{kind}--1",
        "type": kind,
        "name": "CVE-2026-123633",
        "extensions": {_EXT: {"pir_information": [{"pir_id": p, "pir_score": s} for p, s in pirs]}},
    }
    return {"type": "update", "xtm_source": source, "data": data}


def test_pir_rule_is_declared_by_the_trigger_config_only() -> None:
    assert rules.pir_rule_of({"pir_min_score": "policy"}) is True
    assert rules.pir_rule_of({"pir_min_score": 70}) is False
    assert rules.pir_rule_of({}) is False
    assert rules.pir_rule_of(None) is False


def test_pir_kind_picks_the_threshold_by_entity_type_on_playbook_deliveries_only() -> None:
    assert rules.pir_kind(_flagged("vulnerability", [("p1", 80)])) == "vulnerability"
    assert rules.pir_kind(_flagged("intrusion-set", [("p1", 80)])) == "threat"
    assert rules.pir_kind(_flagged("campaign", [("p1", 80)])) == "threat"
    assert rules.pir_kind(_flagged("report", [("p1", 80)])) == ""
    assert rules.pir_kind(_flagged("vulnerability", [("p1", 80)], source="other")) == ""


def test_delivered_pir_ids_are_read_from_the_extension() -> None:
    assert rules.delivered_pir_ids(_flagged("vulnerability", [("p2", 10), ("p1", 90)])) == ["p1", "p2"]
    assert rules.delivered_pir_ids({"xtm_source": "pir_playbook"}) == []


def test_a_listener_keeps_only_its_own_entity_types() -> None:
    threat_cfg = {"pir_min_score": "policy", "provision": {"entity_types": ["Intrusion-Set", "Malware"]}}
    assert rules.type_drop_reason(threat_cfg, _flagged("intrusion-set", [("p1", 80)])) is None
    assert "not one of" in str(rules.type_drop_reason(threat_cfg, _flagged("vulnerability", [("p1", 80)])))
    assert rules.type_drop_reason({"pir_min_score": "policy"}, _flagged("vulnerability", [("p1", 80)])) is None


def test_thresholds_come_from_the_policy_with_defaults_70_and_75() -> None:
    from app.services.organization_context import PrioritizationPolicy

    assert rules.threshold_for(None, "vulnerability") == 70
    assert rules.threshold_for(None, "threat") == 75
    policy = PrioritizationPolicy(pir_min_score_vulnerability=50, pir_min_score_threat=90)
    assert rules.threshold_for(policy, "vulnerability") == 50
    assert rules.threshold_for(policy, "threat") == 90


def test_policy_thresholds_are_bounded_0_to_100() -> None:
    import pytest
    from pydantic import ValidationError

    from app.services.organization_context import PrioritizationPolicy

    with pytest.raises(ValidationError):
        PrioritizationPolicy(pir_min_score_vulnerability=101)
    with pytest.raises(ValidationError):
        PrioritizationPolicy(pir_min_score_threat=-1)


def test_pir_drop_reason_at_above_and_below_the_threshold() -> None:
    assert rules.pir_drop_reason("vulnerability", 70, {"p1": 70}) is None
    assert rules.pir_drop_reason("vulnerability", 70, {"p1": 50, "p2": 80}) is None
    reason = rules.pir_drop_reason("vulnerability", 70, {"p1": 50})
    assert reason is not None and "50" in reason and "70" in reason
    assert rules.pir_drop_reason("threat", 75, {"p1": 74}) is not None


def test_pir_drop_reason_never_widens_on_an_unreadable_score() -> None:
    assert "could not be read" in str(rules.pir_drop_reason("vulnerability", 70, {"p1": None}))
    assert "names no PIR" in str(rules.pir_drop_reason("vulnerability", 70, {}))
    assert rules.pir_drop_reason("", 70, {}) is None


def test_live_scores_are_read_per_pir_and_not_from_the_delivery() -> None:
    import asyncio

    calls: list[dict[str, Any]] = []

    async def gql(query: str, variables: dict[str, Any] | None = None) -> tuple[dict[str, Any], str | None]:
        args = variables or {}
        calls.append(args)
        if args["pirId"] == "p-err":
            return {}, "boom"
        return {"stixDomainObject": {"id": args["id"], "pirInformation": {"pir_score": 80}}}, None

    scores = asyncio.run(rules.live_pir_scores(gql, "vulnerability--1", ["p1", "p-err"]))
    assert scores == {"p1": 80, "p-err": None}
    assert [c["pirId"] for c in calls] == ["p1", "p-err"]


def test_pir_filter_reason_is_a_no_op_without_the_rule_or_on_other_deliveries() -> None:
    import asyncio

    payload = _flagged("vulnerability", [("p1", 10)])
    assert asyncio.run(rules.pir_filter_reason(None, {}, payload)) is None
    assert asyncio.run(rules.pir_filter_reason(None, {"pir_min_score": "policy"}, _has("nginx"))) is None


def test_pir_filter_reason_drops_playbook_deliveries_of_other_entity_types() -> None:
    import asyncio

    report = _flagged("report", [("p1", 90)])
    typed = {"pir_min_score": "policy", "provision": {"entity_types": ["Vulnerability"]}}
    reason = asyncio.run(rules.pir_filter_reason(None, typed, report))
    assert reason is not None and "not one of" in reason
    untyped = {"pir_min_score": "policy"}
    reason = asyncio.run(rules.pir_filter_reason(None, untyped, report))
    assert reason is not None and "no PIR score threshold" in reason
    threat_cfg = {"pir_min_score": "policy", "provision": {"entity_types": ["Intrusion-Set"]}}
    reason = asyncio.run(rules.pir_filter_reason(None, threat_cfg, _flagged("vulnerability", [("p1", 90)])))
    assert reason is not None and "not one of" in reason


def test_the_per_assignment_receiver_loads_the_agent_for_the_pir_score_rule() -> None:
    # The live PIR score read on a registered (platform-managed) OpenCTI mints
    # its token for the agent's owner: without the agent every delivery drops.
    from app.api.v1 import webhooks

    assert webhooks._filter_reads_as_agent({"provider": "opencti", "pir_min_score": "policy"})
    assert not webhooks._filter_reads_as_agent({"provider": "opencti"})
    assert not webhooks._filter_reads_as_agent({"provider": "opencti", "pir_min_score": 1})
