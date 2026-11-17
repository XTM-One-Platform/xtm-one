import json
import uuid

import pytest

from app.services.builtin_tools import build_fimi_metrics_tool
from app.tools.base import ToolCall


def _make_bundle(*objects):
    return {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": list(objects),
    }


def _report(name="Report", created="2024-01-05T00:00:00Z", stix_id=None, confidence=80, sources=None):
    obj = {
        "type": "report",
        "spec_version": "2.1",
        "id": stix_id or f"report--{uuid.uuid4()}",
        "name": name,
        "confidence": confidence,
        "created": created,
        "modified": created,
        "published": created,
        "report_types": ["threat-report"],
        "object_refs": [],
    }
    if sources:
        obj["external_references"] = [{"source_name": s} for s in sources]
    return obj


def _campaign(name="attack", stix_id=None, confidence=80):
    return {
        "type": "campaign",
        "spec_version": "2.1",
        "id": stix_id or f"campaign--{uuid.uuid4()}",
        "name": name,
        "confidence": confidence,
        "created": "2024-01-01T00:00:00Z",
        "modified": "2024-01-01T00:00:00Z",
    }


async def _run(metric, bundle, comparison=None, **kwargs):
    _, executor = build_fimi_metrics_tool()
    args = {"metric": metric, "bundle": bundle, **kwargs}
    if comparison is not None:
        args["comparison_bundle"] = comparison
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="compute_fimi_derived_metrics",
            arguments=args,
        )
    )
    payload = json.loads(result.content)
    # ``is_error`` must agree with the payload's own error marker: a
    # canonical ``{"error": true, …}`` body is a FAILED call, and the flag is
    # what the trace span, the ``tool_done`` event and the loop's per-tool
    # error counters read.
    assert result.is_error is (payload.get("error") is True), result.content
    return payload


# ── Narrative Propagation ────────────────────────────────────────────────


class TestNarrativePropagation:
    @pytest.mark.asyncio
    async def test_basic_propagation(self):
        reports = [
            _report(name=f"R{i}", created=f"2024-01-{i + 1:02d}T00:00:00Z")
            for i in range(5)
        ]
        bundle = _make_bundle(*reports)

        payload = await _run("narrative_propagation", bundle, time_window_days=7)

        assert payload["metric"] == "narrative_propagation"
        assert payload["intermediate_values"]["narrative_count"] == 5
        assert isinstance(payload["score"], (int, float))

    @pytest.mark.asyncio
    async def test_no_narrative_objects(self):
        obj = {"type": "indicator", "spec_version": "2.1", "id": f"indicator--{uuid.uuid4()}", "name": "test", "created": "2024-01-01T00:00:00Z", "modified": "2024-01-01T00:00:00Z"}
        bundle = _make_bundle(obj)

        payload = await _run("narrative_propagation", bundle)

        assert "error" in payload

    @pytest.mark.asyncio
    async def test_empty_bundle(self):
        bundle = _make_bundle()

        payload = await _run("narrative_propagation", bundle)

        assert "error" in payload


# ── Source Divergence ────────────────────────────────────────────────────


class TestSourceDivergence:
    @pytest.mark.asyncio
    async def test_two_sources_overlapping(self):
        r1 = _report(name="Threat A", sources=["Source1", "Source2"])
        r2 = _report(name="Threat B", sources=["Source1"])
        r3 = _report(name="Threat C", sources=["Source2"])
        bundle = _make_bundle(r1, r2, r3)

        payload = await _run("source_divergence", bundle)

        assert payload["metric"] == "source_divergence"
        assert len(payload["pairwise_comparisons"]) >= 1
        assert 0 <= payload["score"] <= 1

    @pytest.mark.asyncio
    async def test_single_source(self):
        r1 = _report(sources=["OnlySource"])
        bundle = _make_bundle(r1)

        payload = await _run("source_divergence", bundle)

        assert "error" in payload


# ── Confidence Drift ─────────────────────────────────────────────────────


class TestConfidenceDrift:
    @pytest.mark.asyncio
    async def test_dual_bundle_confidence_change(self):
        cid = f"campaign--{uuid.uuid4()}"
        obj_a = _campaign(stix_id=cid, confidence=80)
        obj_b = _campaign(stix_id=cid, confidence=20)

        bundle_a = _make_bundle(obj_a)
        bundle_b = _make_bundle(obj_b)

        payload = await _run("confidence_drift", bundle_a, bundle_b)

        assert payload["metric"] == "confidence_drift"
        assert payload["drift_count"] >= 1
        drift = payload["drifts"][0]
        assert drift["old_confidence"] == 80
        assert drift["new_confidence"] == 20
        assert drift["anomaly"] is True  # |delta| = 60 > 30

    @pytest.mark.asyncio
    async def test_dual_bundle_no_change(self):
        cid = f"campaign--{uuid.uuid4()}"
        obj = _campaign(stix_id=cid, confidence=80)
        bundle = _make_bundle(obj)

        payload = await _run("confidence_drift", bundle, bundle)

        assert payload["drift_count"] == 0

    @pytest.mark.asyncio
    async def test_single_bundle_outlier_detection(self):
        objects = [
            _campaign(name=f"C{i}", confidence=80) for i in range(10)
        ]
        objects.append(_campaign(name="outlier", confidence=5))
        bundle = _make_bundle(*objects)

        payload = await _run("confidence_drift", bundle)

        assert payload["metric"] == "confidence_drift"
        assert payload["flagged_count"] >= 1

    @pytest.mark.asyncio
    async def test_single_bundle_missing_confidence(self):
        obj = _campaign()
        del obj["confidence"]
        bundle = _make_bundle(obj)

        payload = await _run("confidence_drift", bundle)

        assert payload["flagged_count"] >= 1
        assert payload["flagged"][0]["flag"] == "missing"


# ── Similarity Score ─────────────────────────────────────────────────────


class TestSimilarityScore:
    @pytest.mark.asyncio
    async def test_identical_bundles(self):
        obj = _campaign()
        bundle = _make_bundle(obj)

        payload = await _run("similarity_score", bundle, bundle)

        assert payload["scores"]["id_jaccard"] == 1.0
        assert payload["scores"]["type_cosine"] == 1.0
        assert payload["scores"]["combined"] == 1.0

    @pytest.mark.asyncio
    async def test_completely_different_bundles(self):
        bundle_a = _make_bundle(_campaign(name="A"))
        bundle_b = _make_bundle(_report(name="B"))

        payload = await _run("similarity_score", bundle_a, bundle_b)

        assert payload["scores"]["id_jaccard"] == 0.0
        # Type cosine should also be 0 (campaign vs report)
        assert payload["scores"]["type_cosine"] == 0.0

    @pytest.mark.asyncio
    async def test_requires_two_bundles(self):
        bundle = _make_bundle(_campaign())

        payload = await _run("similarity_score", bundle)

        assert payload.get("error") is True


# ── Error handling ───────────────────────────────────────────────────────


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_unknown_metric(self):
        bundle = _make_bundle(_campaign())
        payload = await _run("nonexistent", bundle)
        assert payload.get("error") is True

    @pytest.mark.asyncio
    async def test_no_metric(self):
        _, executor = build_fimi_metrics_tool()
        result = await executor.execute_tool(
            ToolCall(
                id=str(uuid.uuid4()),
                name="compute_fimi_derived_metrics",
                arguments={"bundle": _make_bundle()},
            )
        )
        payload = json.loads(result.content)
        assert payload.get("error") is True

    @pytest.mark.asyncio
    async def test_no_bundle(self):
        _, executor = build_fimi_metrics_tool()
        result = await executor.execute_tool(
            ToolCall(
                id=str(uuid.uuid4()),
                name="compute_fimi_derived_metrics",
                arguments={"metric": "confidence_drift"},
            )
        )
        payload = json.loads(result.content)
        assert payload.get("error") is True

    @pytest.mark.asyncio
    async def test_json_string_input(self):
        bundle = _make_bundle(
            _campaign(name="C1", confidence=80),
            _campaign(name="C2", confidence=20),
        )
        _, executor = build_fimi_metrics_tool()
        result = await executor.execute_tool(
            ToolCall(
                id=str(uuid.uuid4()),
                name="compute_fimi_derived_metrics",
                arguments={
                    "metric": "confidence_drift",
                    "bundle_json": json.dumps(bundle),
                },
            )
        )
        assert not result.is_error
        payload = json.loads(result.content)
        assert payload["metric"] == "confidence_drift"
