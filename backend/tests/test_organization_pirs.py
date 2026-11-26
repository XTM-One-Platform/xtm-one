# SPDX-License-Identifier: Apache-2.0
"""#2435: the Intelligence policy, OpenCTI PIRs stored in XTM One with a minimum score each."""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models.organization_pir import OrganizationPir
from app.services import ctem_trigger_rules as rules
from app.services import opencti_trigger_provisioning as prov
from app.services import organization_pirs as pirs
from app.services.organization_context import OrganizationProfile

NOW = datetime(2026, 11, 26, 1, 0, tzinfo=UTC)
OCTI = uuid.uuid4()


def _row(pir_id: str, *, enabled: bool = True, min_score: int = 75, integration_id=OCTI, **kw) -> OrganizationPir:
    return OrganizationPir(
        id=uuid.uuid4(),
        integration_id=integration_id,
        pir_id=pir_id,
        name=kw.get("name", f"PIR {pir_id}"),
        question=kw.get("question", ""),
        owner=kw.get("owner", ""),
        review_cadence=kw.get("review_cadence", ""),
        primary_assets=kw.get("primary_assets", []),
        min_score=min_score,
        enabled=enabled,
    )


# --- Acceptance 1: PIRs listed with no sync configured -------------------------------------------


def test_a_read_fills_the_table_with_new_pirs_disabled():
    nodes = [
        {
            "id": "pir-1",
            "name": "Ransomware on energy",
            "pir_type": "THREAT_LANDSCAPE",
            "pir_criteria": [{"weight": 2}],
        },
        {"id": "pir-2", "name": "", "description": "Retail", "pir_criteria": [{"weight": 1}, {"weight": 3}]},
    ]
    created = pirs.apply_read([], OCTI, nodes, NOW)
    assert [(r.pir_id, r.name, r.enabled, r.min_score) for r in created] == [
        ("pir-1", "Ransomware on energy", False, pirs.DEFAULT_MIN_SCORE),
        ("pir-2", "pir-2", False, pirs.DEFAULT_MIN_SCORE),
    ]
    assert created[0].criteria_summary == "1 criterion, weight 2"
    assert created[1].criteria_summary == "2 criteria, weights 1-3"
    assert all(r.integration_id == OCTI and r.last_seen_at == NOW for r in created)


def test_a_pir_gone_from_opencti_is_marked_gone_never_removed():
    kept, gone = _row("pir-1"), _row("pir-2")
    created = pirs.apply_read([kept, gone], OCTI, [{"id": "pir-1", "name": "Renamed"}], NOW)
    assert created == []
    assert kept.name == "Renamed" and kept.gone_at is None
    assert gone.gone_at == NOW and gone.enabled is True


def test_a_carried_pir_without_source_is_adopted_by_the_first_read():
    carried = _row("pir-1", integration_id=None, min_score=80, owner="SOC")
    created = pirs.apply_read([carried], OCTI, [{"id": "pir-1", "name": "Ransomware"}], NOW)
    assert created == [] and carried.integration_id == OCTI
    assert (carried.min_score, carried.owner, carried.enabled) == (80, "SOC", True)


def test_another_opencti_never_marks_these_pirs_gone():
    other = uuid.uuid4()
    row = _row("pir-1")
    pirs.apply_read([row], other, [], NOW)
    assert row.gone_at is None


# --- Acceptance 2: per-PIR minimum 80 vs 40 filters the playbook per PIR -------------------------


def _spec(thresholds: dict[str, int]) -> dict:
    spec = prov.read_provision_spec(
        {
            "provider": "opencti",
            "provision": {"organization_context": ["pir_ids"], "mode": "pir_playbook", "fleet": "F", "purpose": "P"},
        }
    )
    profile = OrganizationProfile.model_validate({"pir_ids": sorted(thresholds), "pir_min_scores": thresholds})
    derived, missing = prov.derive_from_organization_context(spec, profile)
    assert missing == []
    return derived


def test_two_minimums_provision_one_score_filter_each():
    spec = _spec({"pir-a": 80, "pir-b": 40})
    filters = prov.pir_score_filters(spec)
    assert [(f["values"][0]["values"], f["values"][1]["values"]) for f in filters] == [
        (["40"], ["pir-b"]),
        (["80"], ["pir-a"]),
    ]
    entry, _notify = prov.pir_playbook_nodes(spec, "notifier", "member", "Member", {})
    playbook_filters = json.loads(entry["filters"])
    nested = playbook_filters["filterGroups"][0]["filters"]
    assert [(f["values"][0]["values"][0], f["values"][1]["values"]) for f in nested] == [
        ("40", ["pir-b"]),
        ("80", ["pir-a"]),
    ]
    assert sorted(c["value"] for c in entry["inPirFilters"]) == ["pir-a", "pir-b"]


def test_a_flag_under_its_own_minimum_records_no_signal():
    own = {"pir-a": 80, "pir-b": 40}
    assert rules.pir_drop_reason("threat", 75, {"pir-a": 79}, own) is not None
    assert rules.pir_drop_reason("threat", 75, {"pir-b": 41}, own) is None
    assert rules.pir_floor_met([{"pir_id": "pir-a", "score_at_delivery": 79}], 75, own) is False
    assert rules.pir_floor_met([{"pir_id": "pir-b", "score_at_delivery": 45}], 75, own) is True


def test_one_minimum_keeps_the_single_filter_as_before():
    spec = _spec({"pir-a": 70, "pir-b": 70})
    assert len(prov.pir_score_filters(spec)) == 1


# --- Acceptance 3: a disabled PIR leaves the playbook at the next pass ---------------------------


def test_a_disabled_pir_leaves_the_profile_pir_ids():
    profile = OrganizationProfile.model_validate({"pir_ids": ["pir-a", "pir-b"]})
    rows = [_row("pir-a", min_score=80), _row("pir-b", enabled=False, min_score=40)]
    overlaid = pirs.overlay(profile, rows)
    assert overlaid.pir_ids == ["pir-a"]
    assert overlaid.pir_min_scores == {"pir-a": 80}
    spec = prov.read_provision_spec(
        {
            "provider": "opencti",
            "provision": {"organization_context": ["pir_ids"], "mode": "pir_playbook", "fleet": "F", "purpose": "P"},
        }
    )
    derived, _ = prov.derive_from_organization_context(spec, overlaid)
    assert derived["pir_ids"] == ["pir-a"]


def test_no_stored_pir_answers_the_profile_as_saved():
    profile = OrganizationProfile.model_validate({"pir_ids": ["pir-a"]})
    assert pirs.overlay(profile, []) is profile


def test_policy_edit_validates_and_writes():
    row = _row("pir-a")
    pirs.apply_edit(row, {"min_score": 80, "enabled": False, "review_cadence": "monthly", "owner": "SOC"}, NOW)
    assert (row.min_score, row.enabled, row.review_cadence, row.owner) == (80, False, "monthly", "SOC")
    for bad in ({"min_score": 101}, {"min_score": True}, {"review_cadence": "daily"}, {"enabled": "yes"}):
        with pytest.raises(pirs.PirPolicyError):
            pirs.apply_edit(row, bad, NOW)


# --- Acceptance 4: a 34-PIR profile migrates enabled with details and thresholds -----------------


def _migration():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "i2435pirpolicy01_add_organization_pirs.py"
    spec = importlib.util.spec_from_file_location("i2435pirpolicy01", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_a_34_pir_profile_migrates_with_details_and_thresholds():
    ids = [f"pir-{n:02d}" for n in range(34)]
    profile = {
        "pir_ids": ids,
        "pir_names": {"pir-00": "Ransomware on energy"},
        "pir_details": {
            "pir-00": {
                "question": "Who targets us?",
                "owner": "SOC",
                "review_cadence": "monthly",
                "primary_assets": ["Payments"],
            }
        },
        "prioritization_policy": {"pir_min_score_threat": 60},
    }
    rows = _migration().carried_rows(profile)
    assert [r["pir_id"] for r in rows] == ids
    assert all(r["min_score"] == 60 for r in rows)
    assert rows[0] == {
        "pir_id": "pir-00",
        "name": "Ransomware on energy",
        "question": "Who targets us?",
        "owner": "SOC",
        "review_cadence": "monthly",
        "primary_assets": ["Payments"],
        "min_score": 60,
    }


def test_a_profile_without_policy_migrates_at_the_former_default():
    rows = _migration().carried_rows({"pir_ids": ["a", "a", ""]})
    assert [(r["pir_id"], r["min_score"]) for r in rows] == [("a", 75)]
