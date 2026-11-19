"""query_loki_logs names the upstream cause of a server error.

A 502/503/504 from the Grafana datasource proxy used to read "temporarily
unavailable" whatever the cause, so a heavy query hitting the gateway
timeout could not be told from an outage. These tests pin the answer:
status, elapsed time, the upstream's words and a verdict.
"""

from __future__ import annotations

import httpx
import pytest

from app.api.v1.integration_catalog import build_dummy_token
from app.integrations.providers.grafana import _loki_server_error, build_tools
from app.tools.base import CallableToolExecutor


def _query_loki(monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]):
    calls = {"n": 0}

    async def fake_request(self, method, url, **kwargs):
        calls["n"] += 1
        return responses[min(calls["n"], len(responses)) - 1]

    async def fake_sleep(_secs):
        return None

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    monkeypatch.setattr("app.integrations.http.asyncio.sleep", fake_sleep)
    executor = CallableToolExecutor()
    build_tools(token=build_dummy_token("grafana"), executor=executor)
    return executor._handlers["query_loki_logs"], calls


@pytest.mark.asyncio
async def test_gateway_timeout_names_the_cause_and_the_elapsed_time(monkeypatch):
    fn, calls = _query_loki(
        monkeypatch,
        [httpx.Response(504, text="<html><body><h1>504 Gateway Time-out</h1></body></html>")],
    )

    result = await fn(datasource_id=5, expr='{job="x"} |~ "err.*"')

    assert calls["n"] == 4  # the transient retries still run first
    assert "HTTP 504 after" in result
    assert " ms (retries included)." in result
    assert "Upstream said: 504 Gateway Time-out" in result
    assert "<html>" not in result
    assert "Verdict: gateway timeout" in result
    assert "narrow the time range" in result


@pytest.mark.asyncio
async def test_502_with_a_timeout_body_is_a_gateway_timeout(monkeypatch):
    fn, _ = _query_loki(
        monkeypatch,
        [httpx.Response(502, json={"message": "context deadline exceeded"})],
    )

    result = await fn(datasource_id=5, expr='{job="x"}')

    assert "HTTP 502 after" in result
    assert "deadline exceeded" in result
    assert "Verdict: gateway timeout" in result


@pytest.mark.asyncio
async def test_502_without_a_timeout_reads_as_unavailable(monkeypatch):
    fn, _ = _query_loki(monkeypatch, [httpx.Response(502, text="Bad Gateway")])

    result = await fn(datasource_id=5, expr='{job="x"}')

    assert "Upstream said: Bad Gateway" in result
    assert "Verdict: upstream unavailable" in result
    assert "check_datasource_health" in result


@pytest.mark.asyncio
async def test_transient_502_then_success_answers_the_logs(monkeypatch):
    body = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{"stream": {"job": "x"}, "values": [["1700000000000000000", "hello"]]}],
        },
    }
    fn, calls = _query_loki(monkeypatch, [httpx.Response(502, text="Bad Gateway"), httpx.Response(200, json=body)])

    result = await fn(datasource_id=5, expr='{job="x"}')

    assert calls["n"] == 2
    assert "hello" in result
    assert "Verdict" not in result


def test_client_errors_pass_through_unchanged():
    resp = httpx.Response(400, json={"message": "parse error"})
    assert _loki_server_error("Error: bad", resp, 12) == "Error: bad"
    assert _loki_server_error("Error: net", None, 12) == "Error: net"


def test_upstream_detail_is_bounded():
    resp = httpx.Response(503, text="x" * 5000)
    result = _loki_server_error("Error: down", resp, 3)
    detail = result.split("Upstream said: ", 1)[1].split("\n", 1)[0]
    assert len(detail) == 300
