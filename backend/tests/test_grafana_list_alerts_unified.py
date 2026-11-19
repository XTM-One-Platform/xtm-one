"""``list_grafana_alerts`` reads unified alerting and falls back on a 404.

Before the fix the tool called the ruler API, whose answer is an object keyed
by folder, and parsed it as ``data.groups``: every Grafana 10+ instance got
"No alert rules found." while hundreds of alert instances were firing. These
tests pin the unified Prometheus-compatible endpoint, the state filter, the
firing instance totals and the legacy ``/api/alerts`` fallback.
"""

from __future__ import annotations

import pytest

from app.api.v1.integration_catalog import build_dummy_token
from app.integrations.providers.grafana import (
    _LEGACY_ALERTS_PATH,
    _UNIFIED_RULES_PATH,
    build_tools,
)
from app.tools.base import CallableToolExecutor


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


_UNIFIED_PAYLOAD = {
    "status": "success",
    "data": {
        "groups": [
            {
                "name": "infra",
                "file": "Platform",
                "rules": [
                    {
                        "type": "alerting",
                        "name": "HighCpu",
                        "uid": "rule-a",
                        "state": "firing",
                        "health": "ok",
                        "totals": {"alerting": 3, "normal": 1},
                        "alerts": [],
                    },
                    {
                        "type": "alerting",
                        "name": "DiskFull",
                        "uid": "rule-b",
                        "state": "pending",
                        "health": "ok",
                        "alerts": [{"state": "Pending"}],
                    },
                    {"type": "recording", "name": "job:up:sum"},
                ],
            },
            {
                "name": "apps",
                "file": "Services",
                "rules": [
                    {
                        "type": "alerting",
                        "name": "ErrorRate",
                        "uid": "rule-c",
                        "state": "firing",
                        "health": "ok",
                        "alerts": [
                            {"state": "Alerting"},
                            {"state": "Alerting"},
                            {"state": "Normal"},
                        ],
                    },
                    {
                        "type": "alerting",
                        "name": "Latency",
                        "uid": "rule-d",
                        "state": "inactive",
                        "health": "ok",
                    },
                ],
            },
        ]
    },
}


def _executor(monkeypatch, routes: dict[str, _FakeResponse], calls: list[str]):
    async def fake_safe_request(client, method, url, provider, action, **kw):
        path = next((p for p in routes if url.endswith(p)), None)
        calls.append(path or url)
        resp = routes.get(path) if path else None
        if resp is None:
            return None, f"unexpected url {url}"
        if resp.status_code >= 400:
            return (resp if kw.get("expected_statuses") is not None else None), (
                f"Grafana error {resp.status_code}"
            )
        return resp, None

    monkeypatch.setattr(
        "app.integrations.providers.grafana.safe_request", fake_safe_request
    )
    executor = CallableToolExecutor()
    build_tools(token=build_dummy_token("grafana"), executor=executor)
    return executor._handlers["list_grafana_alerts"]


@pytest.mark.asyncio
async def test_unified_rules_are_listed_with_firing_totals(monkeypatch):
    calls: list[str] = []
    fn = _executor(
        monkeypatch, {_UNIFIED_RULES_PATH: _FakeResponse(_UNIFIED_PAYLOAD)}, calls
    )

    out = await fn()

    assert calls == [_UNIFIED_RULES_PATH]
    assert out.startswith("Found 4 alert rules:")
    assert "HighCpu | UID: rule-a | State: firing" in out
    assert "Firing instances: 3" in out
    assert "ErrorRate | UID: rule-c | State: firing" in out
    assert "job:up:sum" not in out  # recording rules are not alert rules
    assert "Firing alert instances across these rules: 5." in out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected", "absent"),
    [
        ("firing", ["HighCpu", "ErrorRate"], ["DiskFull", "Latency"]),
        ("pending", ["DiskFull"], ["HighCpu", "ErrorRate", "Latency"]),
        ("normal", ["Latency"], ["HighCpu", "DiskFull", "ErrorRate"]),
        ("alerting", ["HighCpu", "ErrorRate"], ["DiskFull", "Latency"]),
    ],
)
async def test_state_filter_is_honoured(monkeypatch, state, expected, absent):
    fn = _executor(
        monkeypatch, {_UNIFIED_RULES_PATH: _FakeResponse(_UNIFIED_PAYLOAD)}, []
    )

    out = await fn(state=state)

    for name in expected:
        assert name in out
    for name in absent:
        assert name not in out


@pytest.mark.asyncio
async def test_unknown_state_is_refused_without_a_request(monkeypatch):
    calls: list[str] = []
    fn = _executor(
        monkeypatch, {_UNIFIED_RULES_PATH: _FakeResponse(_UNIFIED_PAYLOAD)}, calls
    )

    out = await fn(state="burning")

    assert "Unknown alert state 'burning'" in out
    assert calls == []


@pytest.mark.asyncio
async def test_limit_caps_the_listing_and_says_so(monkeypatch):
    fn = _executor(
        monkeypatch, {_UNIFIED_RULES_PATH: _FakeResponse(_UNIFIED_PAYLOAD)}, []
    )

    out = await fn(limit=1)

    assert out.startswith("Found 4 alert rules, showing 1:")
    assert "Showing 1 of 4 rules" in out


@pytest.mark.asyncio
async def test_legacy_endpoint_is_used_only_on_a_404(monkeypatch):
    calls: list[str] = []
    legacy = [
        {"id": 7, "name": "OldCpu", "state": "alerting", "dashboardUid": "d1"},
        {"id": 8, "name": "OldDisk", "state": "ok", "dashboardUid": "d2"},
    ]
    fn = _executor(
        monkeypatch,
        {
            _UNIFIED_RULES_PATH: _FakeResponse({"message": "not found"}, 404),
            _LEGACY_ALERTS_PATH: _FakeResponse(legacy),
        },
        calls,
    )

    out = await fn(state="firing")

    assert calls == [_UNIFIED_RULES_PATH, _LEGACY_ALERTS_PATH]
    assert "OldCpu | UID: 7 | State: alerting" in out
    assert "OldDisk" not in out


@pytest.mark.asyncio
async def test_other_errors_do_not_fall_back(monkeypatch):
    calls: list[str] = []
    fn = _executor(
        monkeypatch,
        {
            _UNIFIED_RULES_PATH: _FakeResponse({"message": "forbidden"}, 403),
            _LEGACY_ALERTS_PATH: _FakeResponse([]),
        },
        calls,
    )

    out = await fn()

    assert calls == [_UNIFIED_RULES_PATH]
    assert "403" in out


@pytest.mark.asyncio
async def test_empty_unified_answer_says_no_rules(monkeypatch):
    fn = _executor(
        monkeypatch,
        {_UNIFIED_RULES_PATH: _FakeResponse({"data": {"groups": []}})},
        [],
    )

    assert await fn() == "No alert rules found."
    assert await fn(state="firing") == "No firing alert rules found."
