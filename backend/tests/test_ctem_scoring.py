"""Unit tests for the pure CTEM scoring module (no I/O)."""

from app.services.ctem_scoring import (
    STAGE_KEYS,
    band,
    compute_posture,
    pct,
    score_stage,
)


def test_pct_basic_and_edge_cases():
    assert pct(1, 4) == 25.0
    assert pct(0, 0) is None  # no denominator
    assert pct(10, 5) == 100.0  # clamped to 100
    assert pct(0, 10) == 0.0


def test_band_thresholds():
    assert band(None) == "unknown"
    assert band(10) == "critical"
    assert band(50) == "at_risk"
    assert band(70) == "fair"
    assert band(80) == "good"
    assert band(95) == "strong"
    assert band(100) == "strong"


def test_score_stage_blind_spot_when_no_product_present():
    # Validation needs OpenCTI or OpenAEV; with neither present (and no facts)
    # it is a blind spot.
    res = score_stage("validation", {}, present_products=set())
    assert res["covered"] is False
    assert res["score"] is None
    assert "openaev" in res["missing_products"]
    assert res["confidence"] == "none"


def test_score_stage_scoping_attack_surface_legacy_fallback():
    # Legacy fallback path: with neither breadth signal present (e.g. an older
    # fact snapshot), raw attack-surface domain coverage still drives the score.
    res = score_stage("scoping", {"attack_surface_pct": 80.0}, {"openaev"})
    assert res["score"] == 80
    assert res["covered"] is True
    assert res["confidence"] == "high"


def test_score_stage_scoping_blends_breadth_and_completeness():
    # Both products contribute breadth: OpenAEV inventory breadth
    # (scope_breadth_pct) + OpenCTI intel-scope completeness are averaged.
    res = score_stage(
        "scoping",
        {"scope_breadth_pct": 75.0, "scope_completeness_pct": 100.0},
        {"opencti", "openaev"},
    )
    assert res["score"] == 88  # round((75 + 100) / 2)
    assert res["covered"] is True
    assert res["confidence"] == "high"


def test_score_stage_scoping_sparse_openaev_does_not_tank_score():
    # Regression for the OpenAEV "present is worse than absent" paradox: a
    # connected-but-sparse OpenAEV (rich inventory in scope but no BAS content
    # mapped to any attack domain yet -> attack_surface_pct == 0) must NOT crater
    # Scoping to 0 while the OpenCTI intel scope is fully populated. Previously
    # attack_surface_pct (0) overrode scope completeness (100), so Scoping scored
    # 0/Critical WITH OpenAEV connected but 100/Strong without it.
    facts = {
        "attack_surface_pct": 0.0,  # 0 of 7 attack domains have BAS content
        "scope_breadth_pct": 75.0,  # 3 of 4 inventory dimensions in scope
        "scope_completeness_pct": 100.0,  # OpenCTI intel scope fully populated
    }
    res = score_stage("scoping", facts, {"opencti", "openaev"})
    assert res["score"] == 88
    assert res["score"] > 0
    assert res["band"] in ("good", "strong")


def test_score_stage_scoping_opencti_scope_completeness():
    # OpenCTI-only: scope completeness drives the score and OpenCTI is now a
    # co-primary, so the stage is covered (filled) - OpenAEV stays an enrichment.
    res = score_stage("scoping", {"scope_completeness_pct": 75.0}, {"opencti"})
    assert res["score"] == 75
    assert res["covered"] is True
    assert res["confidence"] == "high"
    assert "openaev" in res["missing_products"]


def test_score_validation_blends_components():
    facts = {
        "prevention_success_pct": 80.0,
        "detection_success_pct": 60.0,
        "exercised_coverage_pct": 50.0,
    }
    res = score_stage("validation", facts, {"openaev"})
    # weighted: (80*0.4 + 60*0.4 + 50*0.2) / (0.4+0.4+0.2) = 66
    assert res["score"] == 66
    assert res["covered"] is True


def test_score_validation_opencti_detection_readiness_fallback():
    # No OpenAEV efficacy, but OpenCTI detection-rule coverage carries the stage
    # so an OpenCTI-only dashboard still scores Validation.
    res = score_stage("validation", {"detection_rule_coverage_pct": 55.0}, {"opencti"})
    assert res["score"] == 55
    assert res["covered"] is True
    assert "openaev" in res["missing_products"]


def test_score_validation_blind_without_any_signal():
    # OpenCTI present but no detection content and no OpenAEV: stays a blind spot
    # inviting OpenAEV BAS rather than showing a misleading 0.
    res = score_stage("validation", {}, {"opencti"})
    assert res["score"] is None
    assert res["covered"] is False
    assert "openaev" in res["missing_products"]


def test_score_remediation_blends_closure_and_automation():
    # Remediation credits response-automation coverage alongside case closure, so
    # a strong playbook program is not scored 0/Critical just because the case
    # backlog still has a low closure rate.
    res = score_stage(
        "remediation",
        {"case_closure_pct": 0.0, "playbook_automation_pct": 100.0},
        {"opencti"},
    )
    assert res["score"] == 50  # (0 + 100) / 2
    assert res["band"] == "at_risk"
    assert res["covered"] is True


def test_score_remediation_closure_only_without_automation():
    # No playbook signal (no EE / AUTOMATION capability) -> case closure alone
    # drives the score, unchanged from before.
    res = score_stage("remediation", {"case_closure_pct": 60.0}, {"opencti"})
    assert res["score"] == 60
    assert res["covered"] is True


def test_score_remediation_blends_prevention_when_connected():
    # When OpenAEV is connected, prevention efficacy (attacks actually blocked) is
    # a remediation signal too, averaged with closure + automation.
    res = score_stage(
        "remediation",
        {"case_closure_pct": 90.0, "playbook_automation_pct": 60.0, "prevention_success_pct": 30.0},
        {"opencti", "openaev"},
    )
    assert res["score"] == 60  # (90 + 60 + 30) / 3


def test_score_remediation_empty_queue_does_not_inflate():
    # An empty case queue emits NO closure rate (None), so the score rests on the
    # remaining signals instead of a vacuous 100% - Remediation is not "strong"
    # just because there are no cases.
    res = score_stage(
        "remediation",
        {"playbook_automation_pct": 100.0, "prevention_success_pct": 0.0},
        {"opencti", "openaev"},
    )
    assert res["score"] == 50  # (100 + 0) / 2; closure absent
    assert res["score"] < 90  # not "strong"


def test_compute_posture_renormalizes_over_covered_stages():
    stage_facts = {
        "discovery": {"detection_coverage_pct": 60.0},
        "prioritization": {"noncritical_share_pct": 90.0},
    }
    out = compute_posture(stage_facts, {"opencti"})
    # Weighted mean over the two scored stages only (not divided by all five):
    # (60*0.25 + 90*0.25) / (0.25 + 0.25) = 75
    assert out["posture_score"] == 75
    assert out["posture_band"] == "good"
    assert out["covered_stages"] == 2
    assert out["total_stages"] == 5
    val = next(s for s in out["stages"] if s["key"] == "validation")
    assert val["score"] is None
    assert val["covered"] is False


def test_compute_posture_excludes_unscored_stages_from_composite():
    # A covered stage can still have a null score - e.g. a closure rate with 0
    # cases, where ``case_incidents=0`` is real data to show but yields no numeric
    # score. Such a stage is ``covered`` (renders its facts) yet not ``scored``,
    # so it must NOT move the composite, which aggregates only scored stages.
    # Stages with no facts at all stay blind spots (covered=False). In the live
    # flow facts only ever exist for connected products (build_posture_for_user
    # adds facts only for present providers), so a score never appears for
    # something the user cannot see.
    stage_facts = {
        "discovery": {"detection_coverage_pct": 60.0},
        "prioritization": {"noncritical_share_pct": 90.0},
        "validation": {"case_incidents": 0},
    }
    out = compute_posture(stage_facts, {"opencti", "openaev"})
    validation = next(s for s in out["stages"] if s["key"] == "validation")
    assert validation["covered"] is True  # real fact to show
    assert validation["scored"] is False  # but no numeric score
    assert validation["score"] is None
    # Composite = the two scored stages only; the covered-but-unscored validation
    # stage does not move it: (60*0.25 + 90*0.25) / (0.25 + 0.25) = 75.
    assert out["posture_score"] == 75
    # Stages with no facts at all remain blind spots.
    scoping = next(s for s in out["stages"] if s["key"] == "scoping")
    assert scoping["covered"] is False


def test_compute_posture_empty():
    out = compute_posture({}, set())
    assert out["posture_score"] is None
    assert out["posture_band"] == "unknown"
    assert out["covered_stages"] == 0
    assert len(out["stages"]) == len(STAGE_KEYS)


def test_compute_posture_full_coverage_both_products():
    stage_facts = {
        "scoping": {"attack_surface_pct": 90.0},
        "discovery": {"detection_coverage_pct": 70.0},
        "prioritization": {"noncritical_share_pct": 95.0},
        "validation": {
            "prevention_success_pct": 80.0,
            "detection_success_pct": 70.0,
        },
        "remediation": {"case_closure_pct": 60.0},
    }
    out = compute_posture(stage_facts, {"opencti", "openaev"})
    assert out["covered_stages"] == 5
    assert out["posture_score"] is not None
    for stage in out["stages"]:
        assert stage["score"] is not None
        assert stage["missing_products"] == []
