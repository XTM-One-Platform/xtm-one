"""Regression tests for the Grafana Alertmanager tool paths.

Grafana's Alertmanager-compatible API is namespaced by datasource: the
Grafana-managed Alertmanager lives at /api/alertmanager/grafana/api/v2/...
The legacy path without the "grafana" segment (/api/alertmanager/api/v2/...)
404s on modern Grafana instances, which made list_firing_alerts (and the
silence tools) fail permanently with "Grafana resource not found".

These tests pin the corrected request paths and the actionable 404 guidance
that tells agents to stop retrying when the instance does not use
Grafana-managed alerting. The guidance must key on the actual HTTP 404
status code — not on error-text substrings — so non-404 errors whose detail
happens to contain "not found" (e.g. Grafana's HTTP 400 "silence not found")
pass through unchanged.
"""

from __future__ import annotations

import pytest

from app.api.v1.integration_catalog import build_dummy_token
from app.integrations.providers.grafana import build_tools
from app.tools.base import CallableToolExecutor

# (tool name, kwargs required to reach the HTTP call) for the three
# alertmanager-backed tools sharing _ALERTMANAGER_BASE and the 404 hint.
_ALERTMANAGER_TOOLS = [
    ("list_firing_alerts", {}),
    ("list_alert_silences", {}),
    (
        "create_alert_silence",
        {"matchers": "alertname=HighMemory", "comment": "test silence"},
    ),
]


class _FakeResponse:
    """Minimal httpx.Response stand-in for the mocked safe_request."""

    def __init__(self, payload=None, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _capture_safe_request(captured: dict, payload=None):
    """Return a safe_request replacement that records the outgoing URL."""

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["method"] = method
        captured["url"] = url
        return _FakeResponse([] if payload is None else payload), None

    return fake_safe_request


def _failing_safe_request(err: str, status_code: int | None):
    """Return a safe_request replacement that fails with *err*.

    status_code None simulates a network error (no response object);
    otherwise the failed response is returned alongside the error string,
    matching the ``expected_statuses`` opt-in contract of safe_request.
    """

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        if status_code is None:
            return None, err
        return _FakeResponse(status_code=status_code), err

    return fake_safe_request


def _build_executor(monkeypatch: pytest.MonkeyPatch, fake_safe_request):
    monkeypatch.setattr(
        "app.integrations.providers.grafana.safe_request", fake_safe_request
    )
    executor = CallableToolExecutor()
    build_tools(token=build_dummy_token("grafana"), executor=executor)
    return executor


@pytest.mark.asyncio
async def test_list_firing_alerts_uses_grafana_managed_alertmanager_path(monkeypatch):
    """list_firing_alerts must call /api/alertmanager/grafana/api/v2/alerts —
    the datasource-namespaced Grafana-managed Alertmanager path — not the
    legacy /api/alertmanager/api/v2/alerts that 404s."""
    captured: dict = {}
    executor = _build_executor(monkeypatch, _capture_safe_request(captured))
    fn = executor._handlers["list_firing_alerts"]

    await fn()

    assert captured["url"].endswith("/api/alertmanager/grafana/api/v2/alerts")
    assert "/api/alertmanager/api/v2/" not in captured["url"]


@pytest.mark.asyncio
async def test_list_alert_silences_uses_grafana_managed_alertmanager_path(monkeypatch):
    captured: dict = {}
    executor = _build_executor(monkeypatch, _capture_safe_request(captured))
    fn = executor._handlers["list_alert_silences"]

    await fn()

    assert captured["url"].endswith("/api/alertmanager/grafana/api/v2/silences")
    assert "/api/alertmanager/api/v2/" not in captured["url"]


@pytest.mark.asyncio
async def test_create_alert_silence_uses_grafana_managed_alertmanager_path(monkeypatch):
    captured: dict = {}
    executor = _build_executor(
        monkeypatch, _capture_safe_request(captured, payload={"silenceID": "abc-123"})
    )
    fn = executor._handlers["create_alert_silence"]

    result = await fn(matchers="alertname=HighMemory", comment="test silence")

    assert captured["method"] == "post"
    assert captured["url"].endswith("/api/alertmanager/grafana/api/v2/silences")
    assert "Silence created" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "kwargs"), _ALERTMANAGER_TOOLS)
async def test_alertmanager_404_returns_actionable_guidance(
    monkeypatch, tool_name, kwargs
):
    """A 404 on the Grafana-managed Alertmanager path is permanent for the
    instance — every alertmanager tool must tell the agent to stop retrying
    and what to check instead of returning a bare 'Not found'."""
    err = (
        "Error: Grafana resource not found while trying to reach the "
        "alertmanager. Detail: Not found"
    )
    executor = _build_executor(monkeypatch, _failing_safe_request(err, 404))
    fn = executor._handlers[tool_name]

    result = await fn(**kwargs)

    assert "Grafana-managed" in result
    assert "Retrying this tool will not help" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "kwargs"), _ALERTMANAGER_TOOLS)
async def test_alertmanager_non_404_error_passes_through(
    monkeypatch, tool_name, kwargs
):
    """Non-404 errors (e.g. auth failures) must pass through unchanged —
    the not-found guidance only applies to missing alertmanager routes."""
    auth_error = (
        "Error: Grafana authentication failed - the token may be expired "
        "or revoked."
    )
    executor = _build_executor(monkeypatch, _failing_safe_request(auth_error, 401))
    fn = executor._handlers[tool_name]

    result = await fn(**kwargs)

    assert result == auth_error


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "kwargs"), _ALERTMANAGER_TOOLS)
async def test_alertmanager_400_with_not_found_detail_passes_through(
    monkeypatch, tool_name, kwargs
):
    """A non-404 error whose detail contains 'not found' must NOT trigger the
    permanent-404 guidance. Grafana answers a silence POST carrying a stale
    ``id`` with HTTP 400 "silence not found" — the hint must branch on the
    HTTP status code, not on error-text substrings."""
    bad_request = (
        "Error: Grafana rejected the request to create alert silence - "
        "bad request. silence not found"
    )
    executor = _build_executor(monkeypatch, _failing_safe_request(bad_request, 400))
    fn = executor._handlers[tool_name]

    result = await fn(**kwargs)

    assert result == bad_request
    assert "Retrying this tool will not help" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "kwargs"), _ALERTMANAGER_TOOLS)
async def test_alertmanager_network_error_passes_through(monkeypatch, tool_name, kwargs):
    """Network errors have no HTTP response — they must pass through unchanged
    even when their text mentions the host is not found."""
    network_error = (
        "Error: could not connect to Grafana - host not found or unreachable."
    )
    executor = _build_executor(monkeypatch, _failing_safe_request(network_error, None))
    fn = executor._handlers[tool_name]

    result = await fn(**kwargs)

    assert result == network_error
