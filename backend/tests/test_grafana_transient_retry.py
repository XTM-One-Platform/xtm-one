"""Regression tests for the Grafana transient-error retry.

Grafana deployments commonly sit behind a reverse proxy / ingress whose
gateway tier intermittently answers 502/503 for a few seconds. Before the
fix, every such blip surfaced as a hard tool failure to the calling agent
("Grafana server error (502|503) ... temporarily unavailable"). These tests
pin the contract:

- Every Grafana READ / QUERY tool routes through ``_grafana_request`` and
  therefore passes ``retry_statuses=TRANSIENT_RETRY_STATUSES`` (502/503/504)
  to ``safe_request``.
- The two MUTATION tools (create_alert_silence, create_grafana_annotation)
  call ``safe_request`` directly with NO transient retry, because retrying a
  write could apply it twice when the first attempt succeeded behind a
  failing gateway.
- End to end, a tool call seeing a 502 then a 200 succeeds transparently.
"""

from __future__ import annotations

from contextlib import suppress

import pytest

from app.api.v1.integration_catalog import build_dummy_token
from app.integrations.http import TRANSIENT_RETRY_STATUSES
from app.integrations.providers.grafana import build_tools
from app.tools.base import CallableToolExecutor

# (tool name, kwargs required to reach the HTTP call) for the read/query
# tools reported failing, plus the remaining read surface.
_READ_TOOLS = [
    ("get_grafana_org", {}),
    ("get_grafana_user", {}),
    ("list_grafana_dashboards", {}),
    ("read_grafana_dashboard", {"uid": "abc"}),
    ("list_dashboard_panel_queries", {"uid": "abc"}),
    ("search_grafana", {}),
    ("list_grafana_folders", {}),
    ("list_grafana_data_sources", {}),
    ("read_grafana_data_source", {"datasource_id": 5}),
    ("check_datasource_health", {"datasource_uid": "abc"}),
    ("run_grafana_query", {"expr": "up", "datasource_uid": "abc"}),
    ("discover_loki_datasource", {}),
    ("query_loki_logs", {"datasource_id": 5, "expr": '{job="x"}'}),
    ("get_loki_labels", {"datasource_id": 5}),
    ("get_loki_label_values", {"datasource_id": 5, "label": "job"}),
    ("get_loki_series", {"datasource_id": 5}),
    ("get_loki_log_stats", {"datasource_id": 5, "query": '{job="x"}'}),
    ("discover_prometheus_datasource", {}),
    ("get_prometheus_metric_names", {"datasource_id": 5}),
    ("get_prometheus_label_values", {"datasource_id": 5, "label": "job"}),
    ("list_grafana_alerts", {}),
    ("read_grafana_alert", {"alert_uid": "abc"}),
    ("get_grafana_alertmanager_config", {}),
    ("list_firing_alerts", {}),
    ("list_alert_silences", {}),
    ("list_grafana_annotations", {}),
    ("get_grafana_health", {}),
]

# Mutations must NOT retry transient statuses (duplicate-write risk).
_MUTATION_TOOLS = [
    (
        "create_alert_silence",
        {"matchers": "alertname=HighMemory", "comment": "test silence"},
    ),
    ("create_grafana_annotation", {"text": "test annotation"}),
]


class _FakeResponse:
    """Minimal httpx.Response stand-in for the mocked safe_request."""

    def __init__(self, payload=None, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload if self._payload is not None else {}


def _build_executor(monkeypatch: pytest.MonkeyPatch, fake_safe_request):
    monkeypatch.setattr(
        "app.integrations.providers.grafana.safe_request", fake_safe_request
    )
    executor = CallableToolExecutor()
    build_tools(token=build_dummy_token("grafana"), executor=executor)
    return executor


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "kwargs"), _READ_TOOLS)
async def test_read_tools_opt_into_transient_retry(monkeypatch, tool_name, kwargs):
    """Every read/query tool must pass the transient retry statuses so an
    intermittent gateway 502/503 is retried instead of failing the call."""
    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, **kw):
        # Record the first (and decisive) HTTP call's retry opt-in.
        captured.setdefault("retry_statuses", kw.get("retry_statuses"))
        return _FakeResponse({}), None

    executor = _build_executor(monkeypatch, fake_safe_request)
    fn = executor._handlers[tool_name]

    # The stub payload ({}) doesn't match every tool's expected response
    # shape — result formatting may fail, but the retry opt-in is captured
    # at request time, which is all this contract test asserts.
    with suppress(Exception):
        await fn(**kwargs)

    assert captured["retry_statuses"] == TRANSIENT_RETRY_STATUSES


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "kwargs"), _MUTATION_TOOLS)
async def test_mutation_tools_do_not_retry_transient_statuses(
    monkeypatch, tool_name, kwargs
):
    """Mutations must not opt into the transient retry — a duplicate silence
    or annotation could be created if the first attempt actually succeeded
    behind a failing proxy."""
    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, **kw):
        captured["retry_statuses"] = kw.get("retry_statuses")
        return _FakeResponse({"silenceID": "abc", "id": 1}), None

    executor = _build_executor(monkeypatch, fake_safe_request)
    fn = executor._handlers[tool_name]

    await fn(**kwargs)

    assert captured["retry_statuses"] is None


@pytest.mark.asyncio
async def test_read_tool_recovers_from_transient_502(monkeypatch):
    """End to end through the real safe_request: a 502 followed by a 200 must
    yield a successful tool result, not the 'temporarily unavailable'
    error."""
    import httpx

    calls = {"n": 0}

    async def fake_request(self, method, url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(
            200, json={"commit": "abc123", "version": "10.4.2", "created": "now"}
        )

    async def fake_sleep(_secs):
        return None

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    monkeypatch.setattr("app.integrations.http.asyncio.sleep", fake_sleep)

    executor = CallableToolExecutor()
    build_tools(token=build_dummy_token("grafana"), executor=executor)
    fn = executor._handlers["get_grafana_health"]

    result = await fn()

    assert calls["n"] == 2
    assert "temporarily unavailable" not in result
    assert "10.4.2" in result
