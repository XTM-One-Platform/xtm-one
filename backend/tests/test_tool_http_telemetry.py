"""Integration HTTP latency, retries and 5xx on the tool span and the run rollup."""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations import http as integration_http
from app.integrations.http import TRANSIENT_RETRY_STATUSES, safe_request
from app.services.langfuse_tracing import (
    record_tool_http_call,
    reset_current_trace_ctx,
    set_current_trace_ctx,
)
from app.services.tracing.run_rollup import MAX_PROVIDERS, rollup_from_spans, rollup_percentiles


def _tool_ctx() -> dict:
    return {
        "local_span_type": "TOOL",
        "local_metadata": {"observation_kind": "tool", "tool_name": "get_github_file"},
    }


def _client(statuses: list[int | Exception]) -> httpx.AsyncClient:
    answers = iter(statuses)

    def handler(_request: httpx.Request) -> httpx.Response:
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json={"ok": answer < 400})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _call(ctx: dict | None, statuses: list[int | Exception], **kwargs):
    token = set_current_trace_ctx(ctx)
    try:
        with (
            patch.object(integration_http.asyncio, "sleep", AsyncMock()),
            patch.object(integration_http, "_note_integration_rate_limit", AsyncMock()),
        ):
            async with _client(statuses) as client:
                return await safe_request(client, "get", "https://api.example.test/x", "GitHub", "read", **kwargs)
    finally:
        reset_current_trace_ctx(token)


@pytest.mark.asyncio
async def test_a_retried_503_then_200_is_one_call_one_retry_one_5xx():
    ctx = _tool_ctx()
    resp, err = await _call(ctx, [503, 200], retry_statuses=TRANSIENT_RETRY_STATUSES)
    assert err is None and resp is not None
    entry = ctx["local_metadata"]["http"]["GitHub"]
    assert entry["calls"] == 1
    assert entry["retries"] == 1
    assert entry["5xx"] == 1
    assert entry["429"] == 0
    assert entry["status"] == 200
    assert entry["ms"] >= 0.0
    # The same figures ride on the export at close.
    assert ctx["added_metadata"]["http"] is ctx["local_metadata"]["http"]


@pytest.mark.asyncio
async def test_a_final_5xx_and_a_network_error_are_counted():
    ctx = _tool_ctx()
    _resp, err = await _call(ctx, [500])
    assert err
    _resp, err = await _call(ctx, [httpx.ConnectError("down")] * 3)
    assert err
    entry = ctx["local_metadata"]["http"]["GitHub"]
    assert entry["calls"] == 2
    assert entry["5xx"] == 1
    assert entry["network_errors"] == 1
    assert entry["status"] is None


@pytest.mark.asyncio
async def test_a_429_retry_is_counted():
    ctx = _tool_ctx()
    resp, _err = await _call(ctx, [429, 200])
    assert resp is not None
    entry = ctx["local_metadata"]["http"]["GitHub"]
    assert entry["429"] == 1 and entry["retries"] == 1


@pytest.mark.asyncio
async def test_outside_a_tool_call_nothing_is_recorded_and_the_result_is_unchanged():
    resp, err = await _call(None, [200])
    assert err is None and resp is not None and resp.status_code == 200
    generation = {"local_span_type": "GENERATION", "local_metadata": {}}
    await _call(generation, [200])
    assert "http" not in generation["local_metadata"]


def test_a_telemetry_failure_never_reaches_the_request():
    with patch("app.services.langfuse_tracing.record_tool_http_call", side_effect=RuntimeError("boom")):
        integration_http._record_tool_http_call("GitHub", ms=1.0, status=200, retries=0, count_5xx=0, count_429=0)


def test_the_providers_of_one_span_are_bounded():
    ctx = _tool_ctx()
    token = set_current_trace_ctx(ctx)
    try:
        for i in range(10):
            record_tool_http_call(f"p{i}", ms=1.0, status=200, retries=0, count_5xx=0, count_429=0)
    finally:
        reset_current_trace_ctx(token)
    assert len(ctx["local_metadata"]["http"]) == 4


def _tool_span(http: dict | None) -> dict:
    metadata: dict = {"tool_name": "t"}
    if http is not None:
        metadata["http"] = http
    return {"id": uuid.uuid4(), "type": "TOOL", "name": "tool:t", "duration_ms": 50.0, "metadata": metadata}


def test_the_rollup_sums_http_by_provider_and_bounds_the_table():
    figures = {"calls": 2, "ms": 120.0, "retries": 1, "5xx": 1, "429": 0, "network_errors": 0, "status": 200}
    spans = [_tool_span({"GitHub": figures}), _tool_span({"GitHub": figures, "Jira": figures}), _tool_span(None)]
    spans += [_tool_span({f"p{i}": {"calls": 1}}) for i in range(MAX_PROVIDERS + 3)]
    rollup = rollup_from_spans(spans, wall_ms=10_000.0)
    github = rollup["providers"]["GitHub"]
    assert github == {"tool_calls": 2, "ms": 240.0, "calls": 4, "retries": 2, "5xx": 2, "429": 0, "network_errors": 0}
    assert len(rollup["providers"]) == MAX_PROVIDERS
    assert rollup["http_5xx"] == 3
    assert rollup["http_retries"] == 3
    summary = rollup_percentiles([rollup])
    assert summary["http_5xx"]["n"] == 1


def test_a_run_without_integration_calls_has_an_empty_table():
    rollup = rollup_from_spans([_tool_span(None)], wall_ms=10.0)
    assert rollup["providers"] == {}
    assert rollup["http_5xx"] == 0


def test_recording_a_call_costs_microseconds():
    """The program's gate: far under 1 % of an HTTP call (tens to hundreds of ms)."""
    ctx = _tool_ctx()
    token = set_current_trace_ctx(ctx)
    try:
        started = time.perf_counter()
        for _ in range(2000):
            record_tool_http_call("GitHub", ms=80.0, status=200, retries=0, count_5xx=0, count_429=0)
        per_call_ms = (time.perf_counter() - started) * 1000.0 / 2000
    finally:
        reset_current_trace_ctx(token)
    # 1 % of a 10 ms call (a fast upstream) is 0.1 ms.
    assert per_call_ms < 0.1, per_call_ms
