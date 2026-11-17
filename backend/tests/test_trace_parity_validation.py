from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

_MODULE_PATH = Path(__file__).resolve().parents[1] / "app/services/tracing/parity_validation.py"
_SPEC = importlib.util.spec_from_file_location("trace_parity_validation", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

TraceMetricCounts = _MODULE.TraceMetricCounts
TraceStoreParityValidator = _MODULE.TraceStoreParityValidator
payload_fingerprint = _MODULE.payload_fingerprint
select_deterministic_trace_sample = _MODULE.select_deterministic_trace_sample


class _FakeTraceStore:
    def __init__(
        self,
        *,
        counts: TraceMetricCounts,
        trace_ids: list[str],
        payloads: dict[str, dict],
    ) -> None:
        self._counts = counts
        self._trace_ids = trace_ids
        self._payloads = payloads

    async def fetch_counts(self, *, start_at: datetime, end_at: datetime) -> TraceMetricCounts:
        _ = (start_at, end_at)
        return self._counts

    async def fetch_trace_ids(self, *, start_at: datetime, end_at: datetime) -> list[str]:
        _ = (start_at, end_at)
        return list(self._trace_ids)

    async def fetch_trace_payloads(self, *, trace_ids: list[str]) -> dict[str, dict]:
        return {trace_id: self._payloads[trace_id] for trace_id in trace_ids if trace_id in self._payloads}


def test_trace_store_parity_validator_returns_match_with_equal_counts_and_payloads():
    now = datetime.now(UTC)
    trace_ids = [str(uuid4()), str(uuid4())]
    payloads = {
        trace_ids[0]: {"trace_id": trace_ids[0], "span_count": 2, "score_count": 1},
        trace_ids[1]: {"trace_id": trace_ids[1], "span_count": 1, "score_count": 0},
    }
    counts = TraceMetricCounts(total_traces=2, total_spans=3, total_scores=1)
    validator = TraceStoreParityValidator(
        left_store=_FakeTraceStore(counts=counts, trace_ids=trace_ids, payloads=payloads),
        right_store=_FakeTraceStore(counts=counts, trace_ids=list(reversed(trace_ids)), payloads=payloads),
    )

    report = asyncio.run(
        validator.validate(
            start_at=now - timedelta(days=1),
            end_at=now,
            sample_size=2,
            sample_seed="stable-seed",
        )
    )

    assert report.metrics.is_match is True
    assert report.sampled_payloads is not None
    assert report.sampled_payloads.is_match is True
    assert report.is_match is True


def test_trace_store_parity_validator_flags_missing_and_mismatched_payloads():
    now = datetime.now(UTC)
    trace_ids = [str(uuid4()), str(uuid4()), str(uuid4())]
    left_counts = TraceMetricCounts(total_traces=3, total_spans=8, total_scores=3)
    right_counts = TraceMetricCounts(total_traces=3, total_spans=7, total_scores=3)
    left_payloads = {
        trace_ids[0]: {"trace_id": trace_ids[0], "value": 1},
        trace_ids[1]: {"trace_id": trace_ids[1], "value": 2},
    }
    right_payloads = {
        trace_ids[0]: {"trace_id": trace_ids[0], "value": 9},
        trace_ids[2]: {"trace_id": trace_ids[2], "value": 3},
    }
    validator = TraceStoreParityValidator(
        left_store=_FakeTraceStore(counts=left_counts, trace_ids=trace_ids, payloads=left_payloads),
        right_store=_FakeTraceStore(counts=right_counts, trace_ids=trace_ids, payloads=right_payloads),
    )

    report = asyncio.run(
        validator.validate(
            start_at=now - timedelta(days=1),
            end_at=now,
            sample_size=3,
            sample_seed="stable-seed",
        )
    )

    assert report.metrics.spans_match is False
    assert report.sampled_payloads is not None
    assert report.sampled_payloads.is_match is False
    assert report.sampled_payloads.missing_in_left == [trace_ids[2]]
    assert report.sampled_payloads.missing_in_right == [trace_ids[1]]
    assert [mismatch.trace_id for mismatch in report.sampled_payloads.mismatches] == [trace_ids[0]]
    assert report.is_match is False


def test_select_deterministic_trace_sample_is_order_independent():
    ids = [str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())]
    sample_a = select_deterministic_trace_sample(ids, sample_size=2, seed="same-seed")
    sample_b = select_deterministic_trace_sample(list(reversed(ids)), sample_size=2, seed="same-seed")
    assert sample_a == sample_b


def test_payload_fingerprint_handles_typed_values():
    trace_id = uuid4()
    now = datetime.now(UTC)
    payload = {
        "trace_id": trace_id,
        "created_at": now,
        "cost": Decimal("1.25"),
    }
    assert payload_fingerprint(payload)
