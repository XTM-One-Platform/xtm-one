"""Tests for get_grafana_alertmanager_config on the unified-alerting API.

The tool used to call /api/v1/grafana/api/v1/status, which is not a Grafana
endpoint, so it answered "not found" on every instance. The configuration
lives at /api/alertmanager/{datasource}/config/api/v1/alerts, "grafana"
being the built-in Grafana-managed Alertmanager and any other value the UID
of an external Alertmanager datasource.
"""

from __future__ import annotations

import pytest

from app.api.v1.integration_catalog import build_dummy_token
from app.integrations.providers.grafana import build_tools
from app.tools.base import CallableToolExecutor

_CONFIG = {
    "template_files": {},
    "alertmanager_config": {
        "route": {
            "receiver": "default-email",
            "group_by": ["grafana_folder", "alertname"],
            "routes": [
                {
                    "receiver": "oncall-webhook",
                    "object_matchers": [["severity", "=", "critical"]],
                },
                {"receiver": "team-chat"},
            ],
        },
        "receivers": [
            {"name": "default-email"},
            {"name": "oncall-webhook"},
            {"name": "team-chat"},
        ],
    },
}


class _FakeResponse:
    def __init__(self, payload=None, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _executor(monkeypatch: pytest.MonkeyPatch, fake):
    monkeypatch.setattr("app.integrations.providers.grafana.safe_request", fake)
    executor = CallableToolExecutor()
    build_tools(token=build_dummy_token("grafana"), executor=executor)
    return executor._handlers["get_grafana_alertmanager_config"]


def _ok(captured: dict, payload=None):
    async def fake(client, method, url, provider, action, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _FakeResponse(_CONFIG if payload is None else payload), None

    return fake


def _fail(err: str, status_code: int | None):
    async def fake(client, method, url, provider, action, **kwargs):
        if status_code is None:
            return None, err
        return _FakeResponse(status_code=status_code), err

    return fake


@pytest.mark.asyncio
async def test_default_targets_builtin_grafana_alertmanager(monkeypatch):
    captured: dict = {}
    fn = _executor(monkeypatch, _ok(captured))

    result = await fn()

    assert captured["method"] == "get"
    assert captured["url"].endswith("/api/alertmanager/grafana/config/api/v1/alerts")
    assert "/api/v1/grafana/api/v1/status" not in captured["url"]
    assert captured["kwargs"]["expected_statuses"] == set()
    assert "Alertmanager Config (grafana):" in result
    assert "Receivers (3): default-email, oncall-webhook, team-chat" in result
    assert "Root route: default-email" in result
    assert "Group by: grafana_folder, alertname" in result
    assert "Top-level routes (2):" in result
    assert "receiver=oncall-webhook matchers=[severity = critical]" in result
    assert "receiver=team-chat" in result


@pytest.mark.asyncio
async def test_external_datasource_uid_targets_its_alertmanager(monkeypatch):
    captured: dict = {}
    fn = _executor(monkeypatch, _ok(captured))

    result = await fn(datasource="ext-am_01")

    assert captured["url"].endswith("/api/alertmanager/ext-am_01/config/api/v1/alerts")
    assert "Alertmanager Config (ext-am_01):" in result


@pytest.mark.asyncio
async def test_blank_datasource_falls_back_to_builtin(monkeypatch):
    captured: dict = {}
    fn = _executor(monkeypatch, _ok(captured))

    await fn(datasource="  ")

    assert captured["url"].endswith("/api/alertmanager/grafana/config/api/v1/alerts")


@pytest.mark.asyncio
async def test_config_without_wrapper_is_parsed(monkeypatch):
    """An external Alertmanager may answer the bare config object."""
    captured: dict = {}
    fn = _executor(monkeypatch, _ok(captured, payload=_CONFIG["alertmanager_config"]))

    result = await fn(datasource="ext")

    assert "Receivers (3):" in result
    assert "Root route: default-email" in result


@pytest.mark.asyncio
async def test_invalid_datasource_is_refused_without_a_request(monkeypatch):
    captured: dict = {}
    fn = _executor(monkeypatch, _ok(captured))

    result = await fn(datasource="../admin")

    assert "not a valid Alertmanager datasource UID" in result
    assert "url" not in captured


@pytest.mark.asyncio
async def test_unknown_external_datasource_names_path_and_status(monkeypatch):
    err = "Error: Grafana resource not found while trying to get alertmanager config."
    fn = _executor(monkeypatch, _fail(err, 404))

    result = await fn(datasource="missing-am")

    assert result.startswith(err)
    assert "HTTP 404 on GET /api/alertmanager/missing-am/config/api/v1/alerts." in result
    assert "list_grafana_data_sources" in result
    assert "Retrying this tool will not help" not in result


@pytest.mark.asyncio
async def test_builtin_404_names_path_status_and_guidance(monkeypatch):
    err = "Error: Grafana resource not found while trying to get alertmanager config."
    fn = _executor(monkeypatch, _fail(err, 404))

    result = await fn()

    assert "HTTP 404 on GET /api/alertmanager/grafana/config/api/v1/alerts." in result
    assert "Retrying this tool will not help" in result


@pytest.mark.asyncio
async def test_permission_refusal_reads_apart_from_not_found(monkeypatch):
    err = "Error: Grafana access denied while trying to get alertmanager config."
    fn = _executor(monkeypatch, _fail(err, 403))

    result = await fn()

    assert "HTTP 403 on GET /api/alertmanager/grafana/config/api/v1/alerts." in result
    assert "Retrying this tool will not help" not in result


@pytest.mark.asyncio
async def test_network_error_passes_through(monkeypatch):
    err = "Error: could not connect to Grafana - host not found or unreachable."
    fn = _executor(monkeypatch, _fail(err, None))

    assert await fn() == err
