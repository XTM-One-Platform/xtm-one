"""The qualitative CTEM scores against the organization context (#11464)."""

from __future__ import annotations

from app.services.ctem_context_scoring import (
    CONTEXT_FIELDS,
    STAGE_CONTEXT_FIELDS,
    context_state,
    qualitative_stage_score,
    stage_coverage,
    tailored_kev_score,
    with_context_scores,
)


def _payload() -> dict:
    return {
        "posture_score": 70,
        "weights": {"scoping": 0.5, "prioritization": 0.5},
        "headline": {"kev_tailored_count": 2},
        "stages": [
            {"key": "scoping", "score": 100},
            {"key": "prioritization", "score": 80},
            {"key": "validation", "score": None},
        ],
    }


def test_every_stage_reads_context_fields_of_the_contract():
    from app.services.organization_context import CONTRACT_FIELDS

    paths = {path for path, _label in CONTRACT_FIELDS}
    assert set(STAGE_CONTEXT_FIELDS) == {"scoping", "discovery", "prioritization", "validation", "remediation"}
    assert set(CONTEXT_FIELDS) <= paths


def test_coverage_is_the_share_of_the_stage_fields_filled():
    assert stage_coverage("scoping", set()) == 1.0
    assert stage_coverage("scoping", {"crown_jewels", "software"}) == 0.5
    assert stage_coverage("scoping", set(STAGE_CONTEXT_FIELDS["scoping"])) == 0.0


def test_full_context_keeps_the_evidence_and_none_gives_no_score():
    assert qualitative_stage_score("scoping", 100, set()) == 100
    assert qualitative_stage_score("scoping", 100, set(STAGE_CONTEXT_FIELDS["scoping"])) is None
    assert qualitative_stage_score("scoping", None, set()) is None


def test_half_the_context_keeps_seventy_percent():
    # 0.4 + 0.6 * 0.5 = 0.7
    assert qualitative_stage_score("scoping", 100, {"crown_jewels", "software"}) == 70


def test_known_exploited_in_the_stack_pulls_prioritization_down():
    assert tailored_kev_score(0) == 100
    assert tailored_kev_score(2) == 60
    assert tailored_kev_score(9) == 0
    assert tailored_kev_score(None) is None
    assert tailored_kev_score(True) is None
    # (80 + 60) / 2 = 70, full context.
    assert qualitative_stage_score("prioritization", 80, set(), 2) == 70
    # Without a tailored count the evidence is the quantitative score.
    assert qualitative_stage_score("prioritization", 80, set(), None) == 80


def test_context_state_names_the_empty_fields():
    assert context_state(False, set())["state"] == "missing"
    assert context_state(True, set()) == {"state": "complete", "missing_fields": []}
    partial = context_state(True, {"software", "currency"})
    assert partial == {"state": "partial", "missing_fields": ["software"]}


def test_payload_gets_both_scores_and_keeps_the_quantitative_ones():
    out = with_context_scores(_payload(), has_profile=True, missing_fields=[])
    assert out["posture_score"] == 70
    stages = {s["key"]: s for s in out["stages"]}
    assert stages["scoping"]["score"] == 100
    assert stages["scoping"]["qualitative_score"] == 100
    assert stages["prioritization"]["qualitative_score"] == 70
    assert stages["validation"]["qualitative_score"] is None
    assert stages["validation"]["qualitative_band"] == "unknown"
    # Weighted 0.5 / 0.5 over the two stages that have one.
    assert out["qualitative_score"] == 85
    assert out["qualitative_band"] == "good"
    assert out["context"] == {"state": "complete", "missing_fields": []}


def test_no_profile_gives_no_qualitative_score_anywhere():
    out = with_context_scores(_payload(), has_profile=False, missing_fields=[])
    assert out["qualitative_score"] is None
    assert all(s["qualitative_score"] is None for s in out["stages"])
    assert out["context"]["state"] == "missing"
    assert out["stages"][0]["context_missing"] == list(STAGE_CONTEXT_FIELDS["scoping"])


def test_the_input_payload_is_not_changed():
    payload = _payload()
    with_context_scores(payload, has_profile=True, missing_fields=["software"])
    assert "qualitative_score" not in payload
    assert "qualitative_score" not in payload["stages"][0]
