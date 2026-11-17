"""Extraction in scoring: the ``quality_with_extraction`` seam shared by fresh runs and
re-scores, the worked agents end to end, the persisted extracted prediction, and how a
failed extraction is disclosed.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from app.services.evaluation.aggregation import reduce_grouped_metrics
from app.services.evaluation.extraction import (
    EXTRACTED_PREDICTION_KEY,
    EXTRACTION_FAILED_KEY,
    ExtractedPrediction,
    ExtractionFailure,
    count_extraction_failures,
    failure_metric_blob,
    item_count,
    record_extracted_prediction,
)
from app.services.evaluation.extraction.pipeline import quality_with_extraction
from app.services.evaluation.metric_package import build_quality_config, is_performance_key, is_quality_key
from app.services.evaluation.metrics import extract_metrics
from tests.fixtures.extraction import adverse, traces

# ── The scoring seam ──
# Both engine call sites go through ``quality_with_extraction``, so parity proven here
# holds for both.


HARVESTER_CHAIN: dict[str, Any] = {
    "source": {"kind": "agent_output"},
    "operations": [
        {"op": "parse_json"},
        {"op": "object_values"},
        {"op": "flatten"},
        {"op": "collect_keys", "path": ["predictions"]},
        {"op": "dedupe_sort"},
    ],
    "output_kind": "label_set",
}

TOOL_CHAIN: dict[str, Any] = {
    "source": {"kind": "tool_output", "tool_name": traces.TOOL_NAME},
    "operations": [{"op": "parse_json"}],
    "output_kind": "object",
}

_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "definition": {"type": "object", "properties": {"name": {"type": "string"}}},
}

REFERENCE_LABELS = '["T1003.001","T1053.005","T1059.001","T1105","T1574.002"]'


def _classification_config(references: list[str]):
    return build_quality_config(
        family="CLASSIFICATION",
        evaluation=None,
        dataset_refs=references,
    )


def _score(
    *,
    extractor: dict[str, Any] | None,
    agent_output: str,
    execution_details: Any = None,
    reference: str = REFERENCE_LABELS,
    family: str = "CLASSIFICATION",
    output_schema: dict[str, Any] | None = None,
) -> dict[str, dict]:
    return quality_with_extraction(
        extractor=extractor,
        family=family,
        reference=reference,
        agent_output=agent_output,
        execution_details=execution_details or {"traces": []},
        output_schema=output_schema,
        config=_classification_config([reference]),
        agent_id="agent-1",
    ).metric_results


# --- No extractor scores the raw output --------------------------------------


def test_no_extractor_scores_the_raw_output_through_the_unchanged_path() -> None:
    """With no extractor the seam is a no-op: the raw output is scored as before.

    Compared, serialised, against ``run_quality`` followed by ``to_persistable``, so
    a changed shape is caught and not merely a changed score.
    """
    from app.services.evaluation.metric_package import run_quality, to_persistable

    reference = REFERENCE_LABELS
    through_the_seam = _score(extractor=None, agent_output=reference)
    without_the_seam = to_persistable(
        run_quality(
            family="CLASSIFICATION",
            reference=reference,
            prediction=reference,
            output_schema=None,
            config=_classification_config([reference]),
        )
    )

    assert json.dumps(through_the_seam, sort_keys=True) == json.dumps(
        without_the_seam, sort_keys=True
    )
    assert EXTRACTION_FAILED_KEY not in through_the_seam
    assert through_the_seam["classification.micro_prf"]["status"] == "scored"
    assert through_the_seam["classification.micro_prf"]["value"] == 100.0


# --- Preview, fresh run and re-score agree ------------------------------------


@pytest.mark.parametrize(
    ("operations", "stored"),
    [
        pytest.param(HARVESTER_CHAIN["operations"], adverse.harvester_merged(), id="bare-answer"),
        # ``find_json`` meets the catalog's parity condition too.
        pytest.param(
            [{"op": "find_json"}, *HARVESTER_CHAIN["operations"]],
            f"Here are the predictions:\n```json\n{adverse.harvester_merged()}\n```",
            id="fenced-answer-found",
        ),
    ],
)
def test_preview_run_and_rescore_agree(operations: list[dict[str, Any]], stored: str) -> None:
    """Preview, fresh run and re-score share one implementation, so agree byte for byte.

    The fresh path passes its in-memory trace list; the re-score path passes the
    same list as it was persisted.
    """
    from app.services.evaluation.extraction import apply_extractor

    chain = {**HARVESTER_CHAIN, "operations": operations}
    preview = apply_extractor(chain, stored)
    fresh = _score(extractor=chain, agent_output=stored)
    rescore = _score(
        extractor=chain,
        agent_output=stored,
        execution_details={"traces": [], "metric_results": {"performance.x": {}}},
    )

    assert preview.prediction == REFERENCE_LABELS
    assert json.dumps(fresh, sort_keys=True) == json.dumps(rescore, sort_keys=True)
    assert fresh["classification.micro_prf"]["value"] == 100.0


def _tool_score(trace_list: list[dict[str, Any]], payload: str) -> dict[str, dict]:
    """Score a tool-source extractor against one trace list, with a real schema.

    ``_score`` hard-codes ``output_schema=None``, which makes every
    ``json.field_accuracy`` ``unavailable`` whatever the traces hold, so a parity
    assertion built on it would pass vacuously.
    """
    return quality_with_extraction(
        extractor=TOOL_CHAIN,
        family="STRUCTURED_JSON",
        reference=payload,
        agent_output="I have created the artefact as requested.",
        execution_details={"traces": trace_list},
        output_schema={
            "type": "json_schema",
            "definition": {"type": "object", "properties": {"name": {"type": "string"}}},
        },
        config=build_quality_config(
            family="STRUCTURED_JSON", evaluation=None, dataset_refs=[payload]
        ),
        agent_id="agent-1",
    ).metric_results


def test_tool_source_parity_survives_the_judge_trace() -> None:
    """The re-score leg reads a SUPERSET of what the fresh leg read, and still agrees.

    The engine scores a fresh entry against ``agent_local_traces`` but persists
    ``[*agent_local_traces, judge_trace]``. They agree because ``last_successful_call``
    skips any entry whose ``type`` is not ``"tool"``, and the judge entry is
    ``judge``/``judge_skipped``. The companion below shows this is not vacuous.
    """
    payload = '{"name": "Q1 review"}'
    fresh_traces = traces.one_successful_call(payload)
    # The shape ``_build_judge_trace`` writes; keys copied from that writer.
    persisted_traces = [
        *fresh_traces,
        {
            "type": "judge",
            "description": "LLM judge evaluation",
            "duration_ms": 640,
            "outcome": "success",
            "model": "test-judge-model",
        },
    ]

    fresh = _tool_score(fresh_traces, payload)
    rescore = _tool_score(persisted_traces, payload)

    assert fresh["json.field_accuracy"]["status"] == "scored"
    assert fresh["json.field_accuracy"]["value"] == 100.0
    assert json.dumps(fresh, sort_keys=True) == json.dumps(rescore, sort_keys=True)


def test_a_tool_typed_extra_trace_does_move_the_score() -> None:
    """The tripwire for the test above: proves its equality is not vacuous.

    An appended element that passes the ``type == "tool"`` filter becomes the last
    successful call and is scored instead.
    """
    payload = '{"name": "Q1 review"}'
    fresh_traces = traces.one_successful_call(payload)
    tool_typed = [*fresh_traces, traces.tool_trace(tool_output='{"name": "WRONG"}')]

    assert _tool_score(fresh_traces, payload)["json.field_accuracy"]["value"] == 100.0
    assert _tool_score(tool_typed, payload)["json.field_accuracy"]["value"] != 100.0


# --- The reference is never transformed ---------------------------------------


@pytest.mark.parametrize(
    ("extractor", "agent_output"),
    [
        (None, REFERENCE_LABELS),
        (HARVESTER_CHAIN, adverse.harvester_merged()),
        (HARVESTER_CHAIN, "not json at all"),
        (TOOL_CHAIN, "{}"),
    ],
    ids=["no-extractor", "extracted", "payload-unparseable", "family-incompatible"],
)
def test_reference_is_never_transformed(
    extractor: dict[str, Any] | None,
    agent_output: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The family scores the reference as given: a transformed one would corrupt every
    extracted score."""
    seen: list[object] = []
    import app.services.evaluation.extraction.pipeline as pipeline

    original = pipeline.run_quality

    def _spy(**kwargs):
        seen.append(kwargs["reference"])
        return original(**kwargs)

    monkeypatch.setattr(pipeline, "run_quality", _spy)

    reference = REFERENCE_LABELS
    _score(extractor=extractor, agent_output=agent_output, reference=reference)

    assert all(seen_reference is reference for seen_reference in seen)


# --- Raw material is read, never written --------------------------------------


def test_the_traces_are_read_but_never_written() -> None:
    """A tool-source extractor reads the stored traces and leaves them as they were.

    Checked on the success and the failure path, both of which resolve the source
    from the traces before anything else can go wrong.
    """
    payload = '{"name": "Q1 review"}'
    stored = traces.one_successful_call(payload)
    unparseable = traces.one_successful_call("not json")
    before = json.dumps([stored, unparseable], sort_keys=True)

    scored = _tool_score(stored, payload)
    failed = _tool_score(unparseable, payload)

    assert scored["json.field_accuracy"]["value"] == 100.0
    assert failed[EXTRACTION_FAILED_KEY]["error"]["reason"] == "payload_unparseable"
    assert json.dumps([stored, unparseable], sort_keys=True) == before


# --- A later incompatible family fails, never scores --------------------------


def test_family_changed_after_authoring_is_a_typed_failure() -> None:
    """An extractor valid at save time but incompatible at run time is a typed failure."""
    results = _score(
        extractor=HARVESTER_CHAIN,
        agent_output=adverse.harvester_merged(),
        family="STRUCTURED_JSON",
    )
    blob = results.pop(EXTRACTION_FAILED_KEY)
    assert blob["status"] == "invalid"
    assert blob["error"]["reason"] == "family_incompatible"
    assert "not supported in this version" in blob["error"]["message"]
    # A configuration failure leaves the entry out with its reason.
    assert {key: (outcome["status"], outcome["detail"]["reason"]) for key, outcome in results.items()} == {
        "json.field_accuracy": ("unavailable", "extraction_failed"),
        "json.schema_validity": ("unavailable", "extraction_failed"),
    }


# --- A failure the answer caused scores 0; one of configuration leaves the entry out ---


def _scored_zero(results: dict[str, dict]) -> set[str]:
    """The family keys scored 0 with the extraction's reason."""
    return {
        key
        for key, outcome in results.items()
        if key != EXTRACTION_FAILED_KEY
        and outcome["status"] == "scored"
        and outcome["value"] == 0.0
        and outcome["detail"]["reason"] == "prediction_not_scorable"
        and outcome["detail"]["message"].startswith("Extraction failed")
    }


def test_a_failed_extraction_on_the_answer_scores_the_family_0() -> None:
    """What the agent produced and cannot be scored is a wrong answer, not a missing one:
    every family metric scores 0 with the reason, so the entry stays in the mean."""
    results = _score(extractor=HARVESTER_CHAIN, agent_output="prose, not json")
    assert results[EXTRACTION_FAILED_KEY]["error"]["reason"] == "payload_unparseable"
    assert _scored_zero(results) == set(results) - {EXTRACTION_FAILED_KEY} != set()


def test_an_answer_over_an_extraction_limit_scores_0() -> None:
    """Every limit measures the agent's output, far above an expected answer's size: the
    answer is at fault."""
    from app.services.evaluation.extraction.limits import current_limits

    oversized = "x" * (current_limits().max_source_bytes + 1)
    results = _score(extractor=HARVESTER_CHAIN, agent_output=oversized)
    assert results[EXTRACTION_FAILED_KEY]["error"]["reason"] == "limit_exceeded"
    assert _scored_zero(results) == set(results) - {EXTRACTION_FAILED_KEY} != set()


@pytest.mark.parametrize(
    ("output_schema", "scored_zero"),
    [
        (_ANSWER_SCHEMA, {"json.field_accuracy", "json.schema_validity"}),
        # With no answer shape the metrics are left out for every entry, so for this one too.
        (None, set()),
    ],
    ids=["schema", "no_schema"],
)
def test_a_missing_tool_call_is_source_not_present(output_schema, scored_zero) -> None:
    """Reported against the agent: the metrics the entry scores score 0."""
    results = _score(
        extractor=TOOL_CHAIN,
        agent_output="prose answer",
        execution_details={"traces": traces.no_call_at_all()},
        reference='{"name": "Q1 review"}',
        family="STRUCTURED_JSON",
        output_schema=output_schema,
    )
    error = results.pop(EXTRACTION_FAILED_KEY)["error"]
    assert error["reason"] == "source_not_present"
    assert error["agent_id"] == "agent-1"
    assert _scored_zero(results) == scored_zero
    assert {key for key, outcome in results.items() if outcome["status"] == "unavailable"} == (
        set(results) - scored_zero
    )


def test_a_source_this_version_cannot_read_leaves_the_entry_out() -> None:
    """The extractor is at fault, not the answer: the entry is left out, never 0."""
    extractor = {**TOOL_CHAIN, "source": {"kind": "a_retired_source"}}
    results = _score(extractor=extractor, agent_output='{"a": 1}', family="STRUCTURED_JSON")
    assert results.pop(EXTRACTION_FAILED_KEY)["error"]["reason"] == "type_mismatch"
    assert {key: (outcome["status"], outcome["detail"]["reason"]) for key, outcome in results.items()} == {
        "json.field_accuracy": ("unavailable", "extraction_failed"),
        "json.schema_validity": ("unavailable", "extraction_failed"),
    }


def test_the_failure_record_carries_only_bounded_metadata() -> None:
    """No fragment of the material reaches the failure record, in any field."""
    sentinel = "SECRET-CANARY-8f2a"
    results = _score(extractor=HARVESTER_CHAIN, agent_output=f"{sentinel} prose")
    assert sentinel not in json.dumps(results)
    assert set(results[EXTRACTION_FAILED_KEY]["error"]) == {
        "reason",
        "message",
        "agent_id",
        "operation_index",
        "operation",
        "path",
        "expected_kind",
        "actual_kind",
    }


# ── End to end for the worked agents ──
# These drive the real seam with the real fixtures and the real family configuration.
# No agent or integration is modified: the extractors read the shapes those agents
# already produce.


# --- The worked extractors, exactly as the contracts document them -----------

HARVESTER = {
    "source": {"kind": "agent_output"},
    "operations": [
        {"op": "parse_json"},
        {"op": "object_values"},
        {"op": "flatten"},
        {"op": "collect_keys", "path": ["predictions"]},
        {"op": "dedupe_sort"},
    ],
    "output_kind": "label_set",
}

DASHBOARD = {
    "source": {"kind": "tool_output", "tool_name": "build_dashboard"},
    "operations": [{"op": "parse_json"}],
    "output_kind": "object",
}

SCENARIO = {
    "source": {"kind": "tool_output", "tool_name": "create_scenario"},
    "operations": [{"op": "parse_json"}],
    "output_kind": "object",
}


def _classification(refs: list[str]):
    return build_quality_config(
        family="CLASSIFICATION", evaluation=None, dataset_refs=refs
    )


def _structured(refs: list[str]):
    return build_quality_config(
        family="STRUCTURED_JSON", evaluation=None, dataset_refs=refs
    )


def _schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "definition": {"type": "object", "properties": properties},
    }


# --- cti-ttp-harvester-filigran-ai ---------------------------------------------


def test_harvester_scores_above_zero_on_a_partial_match() -> None:
    """A partial match earns partial credit, where raw-JSON scoring gives none."""
    partial = json.dumps(
        {
            "other-report.pdf": [
                {
                    "text": "a paraphrase of the evidence",
                    "predictions": {"T1059.001": 0.11, "T1105": 0.42},
                }
            ]
        }
    )
    results = quality_with_extraction(
        extractor=HARVESTER,
        family="CLASSIFICATION",
        reference=REFERENCE_LABELS,
        agent_output=partial,
        execution_details={"traces": []},
        output_schema=None,
        config=_classification([REFERENCE_LABELS]),
    ).metric_results
    value = results["classification.micro_prf"]["value"]
    assert value is not None and value > 0.0


def test_harvester_empty_case_is_scored_not_failed() -> None:
    """An empty extraction is a prediction, not a failure.

    The dataset carries a vocabulary from its other entries: a dataset whose only
    reference is ``[]`` has none, which is refused as a configuration error.
    """
    results = quality_with_extraction(
        extractor=HARVESTER,
        family="CLASSIFICATION",
        reference="[]",
        agent_output=adverse.harvester_empty(),
        execution_details={"traces": []},
        output_schema=None,
        config=_classification([REFERENCE_LABELS, "[]"]),
    ).metric_results
    assert EXTRACTION_FAILED_KEY not in results


# --- The two markdown agents whose answer is in a tool call ------------------


def _tool_case(extractor: dict, tool_name: str, payload: str) -> dict:
    reference = payload
    return quality_with_extraction(
        extractor=extractor,
        family="STRUCTURED_JSON",
        reference=reference,
        # The answer itself is prose — the deliverable is only in the tool call.
        agent_output="I have created the artefact as requested.",
        execution_details={
            "traces": [traces.tool_trace(tool_name=tool_name, tool_output=payload)]
        },
        output_schema=_schema({"name": {"type": "string"}}),
        config=_structured([reference]),
    ).metric_results


@pytest.mark.parametrize(
    ("extractor", "tool_name", "payload"),
    [
        (DASHBOARD, "build_dashboard", adverse.dashboard_payload()),
        (SCENARIO, "create_scenario", adverse.scenario_payload()),
    ],
    ids=["dashboard-creator", "scenario-creator"],
)
def test_a_markdown_agent_scores_its_tool_payload(
    extractor: dict[str, Any], tool_name: str, payload: str
) -> None:
    results = _tool_case(extractor, tool_name, payload)
    assert EXTRACTION_FAILED_KEY not in results
    assert results["json.field_accuracy"]["status"] == "scored"
    assert results["json.field_accuracy"]["value"] == 100.0


# ── The extracted prediction persisted beside its score ──
# With an extractor configured the score comes from the extracted value, so the entry
# must show that value: otherwise an author cannot tell a bad answer from a chain that
# selected the wrong thing. A re-score must CLEAR a value its own chain did not produce.


CHAIN: dict[str, Any] = {
    "source": {"kind": "agent_output"},
    "operations": [
        {"op": "parse_json"},
        {"op": "select_path", "path": ["predictions"]},
        {"op": "object_keys"},
        {"op": "dedupe_sort"},
    ],
    "output_kind": "label_set",
}
REFERENCE = '["T1003.001","T1059.001"]'
OUTPUT = json.dumps({"predictions": {"T1059.001": 0.9, "T1003.001": 0.7}})


def _record_score(*, extractor: dict[str, Any] | None, agent_output: str = OUTPUT):
    return quality_with_extraction(
        extractor=extractor,
        family="CLASSIFICATION",
        reference=REFERENCE,
        agent_output=agent_output,
        execution_details={"traces": []},
        output_schema=None,
        config=build_quality_config(
            family="CLASSIFICATION", evaluation=None, dataset_refs=[REFERENCE]
        ),
        agent_id="agent-1",
    )


def test_the_seam_returns_the_value_it_scored() -> None:
    scored = _record_score(extractor=CHAIN)
    assert scored.extracted is not None
    # The prediction, not the raw output: that difference is the whole feature.
    assert scored.extracted.prediction != OUTPUT
    assert json.loads(scored.extracted.prediction) == ["T1003.001", "T1059.001"]
    assert scored.extracted.output_kind == "label_set"
    assert scored.extracted.as_detail()["item_count"] == 2


def test_no_extractor_records_nothing() -> None:
    """No empty panel, no placeholder: the raw output IS what scored."""
    scored = _record_score(extractor=None, agent_output=REFERENCE)
    assert scored.extracted is None
    assert record_extracted_prediction({}, scored.extracted) == {}


def test_a_failed_extraction_records_nothing() -> None:
    """The failure notice is the disclosure; there is no value to show."""
    scored = _record_score(extractor=CHAIN, agent_output="not json at all")
    assert scored.extracted is None
    assert "extraction.failed" in scored.metric_results


def test_a_rescore_clears_a_value_its_chain_did_not_produce() -> None:
    """Drop or break the chain, and the previous value must not sit beside a fresh
    score as though it had produced it.
    """
    details: dict[str, Any] = {"metric_results": {}}
    record_extracted_prediction(details, _record_score(extractor=CHAIN).extracted)
    assert EXTRACTED_PREDICTION_KEY in details

    record_extracted_prediction(details, _record_score(extractor=None).extracted)
    assert EXTRACTED_PREDICTION_KEY not in details
    # Nothing else was disturbed on the way out.
    assert details == {"metric_results": {}}


def test_item_count_is_one_definition() -> None:
    """The count an author previews and the count stored beside the score agree."""
    assert item_count('["a","b","c"]') == 3
    assert item_count('{"a":1}') == 1
    # Not every prediction is countable, and that is reported rather than guessed.
    assert item_count("42") is None
    assert item_count("not json") is None


def test_the_record_never_travels_as_a_metric() -> None:
    """It is entry-scoped payload, so it must not reach the metric blob."""
    scored = _record_score(extractor=CHAIN)
    blob = json.dumps(scored.metric_results)
    assert scored.extracted is not None
    assert scored.extracted.prediction not in blob
    assert EXTRACTED_PREDICTION_KEY not in scored.metric_results


def test_the_detail_shape_mirrors_the_preview_response() -> None:
    detail = ExtractedPrediction(prediction='["a"]', output_kind="label_set").as_detail()
    assert set(detail) == {"prediction", "output_kind", "item_count"}


# ── Disclosure of a failed extraction ──
# The failure key is absent from the metric package's registry, so it is inert to
# selection and aggregation. It survives persistence and the API, and contributes nothing
# to any aggregate.


FAILURE = ExtractionFailure(
    reason="source_not_present",
    message="no successful call to 'extract_ttp' was recorded for this entry",
)


def _details(*, failed: bool) -> dict[str, Any]:
    metric_results: dict[str, Any] = {"performance.latency": {"status": "scored", "value": 12.0}}
    if failed:
        metric_results.update(failure_metric_blob(FAILURE, agent_id=str(uuid.uuid4())))
    else:
        metric_results["classification.samples_f1"] = {"status": "scored", "value": 80.0}
    return {"traces": [], "metric_results": metric_results}


# --- a failed entry contributes nothing --------------------------------------


def test_the_failure_key_is_not_a_package_metric() -> None:
    """Unregistered by design: never selectable, never aggregated."""
    assert not is_quality_key(EXTRACTION_FAILED_KEY)
    assert not is_performance_key(EXTRACTION_FAILED_KEY)


def test_a_failed_entry_writes_no_metric_row() -> None:
    """``extract_metrics`` indexes only ``scored`` outcomes, so ``invalid`` is dropped.

    A second, independent guard: even a registered key would leave no row to aggregate.
    """
    indexed = extract_metrics(_details(failed=True))
    assert EXTRACTION_FAILED_KEY not in indexed
    assert "performance.latency" in indexed


def test_a_failed_entry_does_not_drag_an_aggregate_toward_zero() -> None:
    """A failure contributes nothing, as distinct from contributing a zero.

    Through the real reducer: three scored entries plus two failed ones average to
    the three scored ones alone, not to what 0.0 per failure would give.
    """
    scored_entries = [_details(failed=False) for _ in range(3)]
    mixed = scored_entries + [_details(failed=True) for _ in range(2)]

    def _grouped(entries: list[dict[str, Any]]) -> dict[str, list[float]]:
        grouped: dict[str, list[float]] = {}
        for entry in entries:
            for key, value in extract_metrics(entry).items():
                grouped.setdefault(key, []).append(value)
        return grouped

    scored_only = reduce_grouped_metrics(_grouped(scored_entries))
    with_failures = reduce_grouped_metrics(_grouped(mixed))

    key = "classification.samples_f1"
    assert scored_only[key].value == 80.0
    assert with_failures[key].value == scored_only[key].value
    # The failures did not become contributing entries.
    assert with_failures[key].entry_count == scored_only[key].entry_count == 3
    # The failure key itself is never aggregated.
    assert EXTRACTION_FAILED_KEY not in with_failures
    # A "failure scores 0" reading, for contrast.
    naive = reduce_grouped_metrics({key: [80.0, 80.0, 80.0, 0.0, 0.0]})
    assert naive[key].value != with_failures[key].value


def test_the_blob_mirrors_the_packages_invalid_contract() -> None:
    blob = failure_metric_blob(FAILURE)[EXTRACTION_FAILED_KEY]
    assert blob["status"] == "invalid"
    assert blob["value"] == 0
    assert blob["error"]["reason"] == "source_not_present"


# --- the failure count --------------------------------------------------------


@pytest.mark.parametrize(
    ("failed", "entries"),
    [(0, 4), (3, 7), (5, 5)],
    ids=["none-failed", "n-of-m-failed", "all-failed"],
)
def test_failure_count_is_the_number_of_failed_entries(failed: int, entries: int) -> None:
    rows = [_details(failed=i < failed) for i in range(entries)]
    assert sum(count_extraction_failures(d) for d in rows) == failed


def test_failure_count_tolerates_missing_or_malformed_details() -> None:
    for details in (None, {}, {"metric_results": None}, "not a dict", []):
        assert count_extraction_failures(details) == 0


# --- the record survives to the API ------------------------------------------


def test_the_detail_response_declares_the_count_and_the_list_does_not() -> None:
    """The count is on the detail response, where results are already loaded."""
    from app.api.v1.schemas.evaluation import (
        EvaluationRunDetailResponse,
        EvaluationRunResponse,
    )

    assert "extraction_failure_count" in EvaluationRunDetailResponse.model_fields
    assert "extraction_failure_count" not in EvaluationRunResponse.model_fields
