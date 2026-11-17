"""``read_grafana_alert`` reads a rule by uid through the provisioning API.

Before the fix the tool called ``/api/v1/rule/grafana/{uid}``, which Grafana
does not serve: every uid ``list_grafana_alerts`` returned answered "Grafana
resource not found". The uids the unified rules listing carries are served by
``/api/v1/provisioning/alert-rules/{uid}`` (Grafana 9.1+), a flat rule object.
"""

from __future__ import annotations

import pytest

from app.api.v1.integration_catalog import build_dummy_token
from app.integrations.providers.grafana import _PROVISIONING_RULES_PATH, build_tools
from app.tools.base import CallableToolExecutor


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


_RULE = {
    "uid": "dewqvsrrojgu8b-p",
    "title": "HighErrorRate",
    "folderUID": "platform",
    "ruleGroup": "api",
    "condition": "C",
    "for": "5m",
    "noDataState": "NoData",
    "execErrState": "Error",
    "data": [
        {"refId": "A", "datasourceUid": "loki-prod"},
        {"refId": "C", "datasourceUid": "__expr__"},
    ],
    "labels": {"severity": "critical"},
    "annotations": {"summary": "5xx over 2%"},
}


def _read_alert(monkeypatch, response, urls: list[str]):
    async def fake_safe_request(_client, _method, url, *_args, **_kwargs):
        urls.append(url)
        if response.status_code >= 400:
            return response, f"Grafana error: HTTP {response.status_code}"
        return response, None

    monkeypatch.setattr("app.integrations.providers.grafana.safe_request", fake_safe_request)
    executor = CallableToolExecutor()
    build_tools(token=build_dummy_token("grafana"), executor=executor)
    return executor._handlers["read_grafana_alert"]


@pytest.mark.asyncio
async def test_rule_is_read_from_the_provisioning_api(monkeypatch):
    urls: list[str] = []
    fn = _read_alert(monkeypatch, _FakeResponse(_RULE), urls)

    out = await fn(alert_uid="dewqvsrrojgu8b-p")

    assert len(urls) == 1
    assert urls[0].endswith(f"{_PROVISIONING_RULES_PATH}/dewqvsrrojgu8b-p")
    assert "/api/v1/rule/grafana/" not in urls[0]
    assert out.startswith("Alert: HighErrorRate (UID: dewqvsrrojgu8b-p)")
    for line in (
        "Folder UID: platform",
        "Rule Group: api",
        "Condition: C",
        "Pending period (for): 5m",
        "A: datasource loki-prod",
        "severity: critical",
        "summary: 5xx over 2%",
    ):
        assert line in out


@pytest.mark.asyncio
async def test_uid_is_path_encoded(monkeypatch):
    urls: list[str] = []
    fn = _read_alert(monkeypatch, _FakeResponse(_RULE), urls)

    await fn(alert_uid="a/b c")

    assert urls[0].endswith(f"{_PROVISIONING_RULES_PATH}/a%2Fb%20c")


@pytest.mark.asyncio
async def test_empty_uid_is_refused_without_a_request(monkeypatch):
    urls: list[str] = []
    fn = _read_alert(monkeypatch, _FakeResponse(_RULE), urls)

    out = await fn(alert_uid="  ")

    assert "alert_uid is required" in out
    assert urls == []


@pytest.mark.asyncio
async def test_error_answer_is_returned(monkeypatch):
    urls: list[str] = []
    fn = _read_alert(monkeypatch, _FakeResponse({"message": "not found"}, 404), urls)

    out = await fn(alert_uid="missing")

    assert "404" in out
