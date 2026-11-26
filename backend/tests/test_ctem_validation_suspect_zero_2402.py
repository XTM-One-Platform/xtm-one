"""The CTEM Validation Analyst never reads a suspect 0% detection as a finding (#2402).

A uniform "Not Detected" with a live EDR / SIEM collector is far more often a
collector that did not match the security platform's alerts than a total
detection failure. The seeded analyst reads the collector traces before scoring
it, says the detection result is not assessable when the traces show unmatched
alerts, keeps a real miss reported as not detected, and writes counts that
match the items it lists.
"""

from __future__ import annotations

from jsonschema import ValidationError, validate

from app.agents.builtin_agents import (
    _STAGE_AGENTS,
    _STAGE_ASSESSMENT_SCHEMA,
    _STAGE_ASSIGNMENTS,
    _STAGE_DEFS,
    _stage_persona,
    _stage_prompt,
)

_VALIDATION = next(d for d in _STAGE_DEFS if d["key"] == "validation")


def _validation_texts() -> list[str]:
    agent = next(a for a in _STAGE_AGENTS if a["slug"] == _VALIDATION["slug"])
    prompts = [a["prompt"] for a in _STAGE_ASSIGNMENTS if a["agent_slug"] == _VALIDATION["slug"]]
    assert len(prompts) == 2, "the weekly and the on-demand assignments"
    return [agent["persona"], *prompts]


def test_the_validation_analyst_reads_the_collector_traces_before_scoring_a_zero():
    for text in _validation_texts():
        assert "manage_openaev_expectation action=traces" in text
        assert "at most two" in text
        assert "not assessable" in text
        assert "score_read to unknown" in text
        assert "collector's matching" in text
        assert "`caveats`" in text


def test_a_suspect_zero_is_never_scored_critical_nor_answered_with_new_detections():
    persona = _stage_persona(_VALIDATION)
    assert "never critical on that zero" in persona
    assert "not new detections" in persona


def test_a_true_miss_stays_reported_as_not_detected():
    persona = _stage_persona(_VALIDATION)
    assert "the miss is real: report those techniques as not detected" in persona


def test_every_stage_writes_counts_that_match_the_items_listed():
    for d in _STAGE_DEFS:
        assert "equals the number of items you list" in _stage_persona(d), d["key"]
        assert "equals the number of items listed" in _stage_prompt(d), d["key"]


def test_the_suspect_zero_rule_stays_on_the_validation_stage():
    for d in _STAGE_DEFS:
        has_rule = "action=traces" in _stage_persona(d) + _stage_prompt(d)
        assert has_rule == (d["key"] == "validation"), d["key"]


def test_the_traces_action_the_rule_names_exists_on_the_expectation_tool():
    import inspect

    from app.integrations.providers.openaev import expectations

    assert '"traces"' in inspect.getsource(expectations)
    assert "manage_openaev_expectation" in inspect.getsource(expectations)


_SUSPECT_ZERO_ASSESSMENT = {
    "stage": "validation",
    "vertical": "cyber",
    "headline": "Prevention held on 5 of 8 techniques; the 0% detection is not assessable",
    "assessment": (
        "Detection result not assessable: the collector did not match the security "
        "platform's alerts on the 8 detection expectations read."
    ),
    "score_read": "unknown",
    "key_metrics": [{"label": "Prevention", "value": "5/8"}],
    "recommended_action": "Check the EDR collector's matching against the EDR's own alerts.",
    "caveats": ["Detection 0/8 is suspect: the traces show EDR alerts the collector did not match."],
    "coverage": {"opencti": True, "openaev": True},
}


def test_the_schema_carries_an_optional_caveats_list():
    validate(_SUSPECT_ZERO_ASSESSMENT, _STAGE_ASSESSMENT_SCHEMA)
    assert "caveats" not in _STAGE_ASSESSMENT_SCHEMA["required"]
    try:
        validate({**_SUSPECT_ZERO_ASSESSMENT, "caveats": "one string"}, _STAGE_ASSESSMENT_SCHEMA)
    except ValidationError:
        pass
    else:
        raise AssertionError("caveats is a list of strings")


def test_an_existing_assessment_without_caveats_still_validates():
    existing = {k: v for k, v in _SUSPECT_ZERO_ASSESSMENT.items() if k != "caveats"}
    validate({**existing, "score_read": "critical"}, _STAGE_ASSESSMENT_SCHEMA)
