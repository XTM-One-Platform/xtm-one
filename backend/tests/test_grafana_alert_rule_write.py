"""Grafana-managed alert rules are created, updated and deleted through the provisioning API.

Each write goes to ``/api/v1/provisioning/alert-rules`` with ``X-Disable-Provenance``
(the rule stays editable in the Grafana UI), without transient retry, and a create or
update reads the rule back by its uid.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.api.v1.integration_catalog import build_dummy_token
from app.integrations.providers.grafana import _PROVISIONING_RULES_PATH, build_tools
from app.tools.base import CallableToolExecutor

_RULE = {
    "title": "HighErrorRate",
    "folderUID": "platform",
    "ruleGroup": "api",
    "condition": "C",
    "for": "5m",
    "data": [{"refId": "A", "datasourceUid": "loki-prod"}],
    "labels": {"severity": "critical"},
}


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = b"" if payload is None else json.dumps(payload).encode()

    def json(self) -> Any:
        return self._payload


def _tools(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, _FakeResponse], calls: list[dict[str, Any]]
) -> dict[str, Callable[..., Awaitable[str]]]:
    async def fake_safe_request(
        _client: Any, method: str, url: str, *_args: Any, **kwargs: Any
    ) -> tuple[_FakeResponse, str | None]:
        calls.append({"method": method, "url": url, **kwargs})
        response = answers[method]
        if response.status_code >= 400:
            return response, f"Grafana error: HTTP {response.status_code}"
        return response, None

    monkeypatch.setattr("app.integrations.providers.grafana.safe_request", fake_safe_request)
    executor = CallableToolExecutor()
    build_tools(token=build_dummy_token("grafana"), executor=executor)
    return executor._handlers  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_create_posts_the_rule_and_reads_it_back(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    answers = {
        "post": _FakeResponse({**_RULE, "uid": "new-uid"}, 201),
        "get": _FakeResponse({**_RULE, "uid": "new-uid"}),
    }
    handlers = _tools(monkeypatch, answers, calls)

    out = await handlers["create_grafana_alert_rule"](rule_json=json.dumps(_RULE))

    assert [c["method"] for c in calls] == ["post", "get"]
    assert calls[0]["url"].endswith(_PROVISIONING_RULES_PATH)
    assert calls[0]["json"]["title"] == "HighErrorRate"
    assert calls[0]["headers"]["X-Disable-Provenance"] == "true"
    assert "retry_statuses" not in calls[0]
    assert calls[1]["url"].endswith(f"{_PROVISIONING_RULES_PATH}/new-uid")
    assert out.startswith("Created alert rule new-uid.")
    assert "Alert: HighErrorRate (UID: new-uid)" in out


@pytest.mark.asyncio
async def test_create_refuses_a_rule_without_its_required_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    handlers = _tools(monkeypatch, {}, calls)

    out = await handlers["create_grafana_alert_rule"](rule_json=json.dumps({"title": "x"}))

    assert "missing: folderUID, ruleGroup, condition, data" in out
    assert calls == []


@pytest.mark.asyncio
async def test_invalid_json_is_refused_without_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    handlers = _tools(monkeypatch, {}, calls)

    out = await handlers["create_grafana_alert_rule"](rule_json="{not json")

    assert "not valid JSON" in out
    assert calls == []


@pytest.mark.asyncio
async def test_update_puts_to_the_encoded_uid_and_reads_back(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    answers = {"put": _FakeResponse({**_RULE, "uid": "a/b"}), "get": _FakeResponse({**_RULE, "uid": "a/b"})}
    handlers = _tools(monkeypatch, answers, calls)

    out = await handlers["update_grafana_alert_rule"](alert_uid="a/b", rule_json=json.dumps(_RULE))

    assert calls[0]["method"] == "put"
    assert calls[0]["url"].endswith(f"{_PROVISIONING_RULES_PATH}/a%2Fb")
    assert calls[0]["json"]["uid"] == "a/b"
    assert out.startswith("Updated alert rule a/b.")


@pytest.mark.asyncio
async def test_delete_sends_a_delete_to_the_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    handlers = _tools(monkeypatch, {"delete": _FakeResponse(None, 204)}, calls)

    out = await handlers["delete_grafana_alert_rule"](alert_uid="old-uid")

    assert calls[0]["method"] == "delete"
    assert calls[0]["url"].endswith(f"{_PROVISIONING_RULES_PATH}/old-uid")
    assert out == "Deleted alert rule old-uid."


@pytest.mark.asyncio
async def test_empty_uid_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    handlers = _tools(monkeypatch, {}, calls)

    assert "alert_uid is required" in await handlers["delete_grafana_alert_rule"](alert_uid=" ")
    assert "alert_uid is required" in await handlers["update_grafana_alert_rule"](alert_uid="", rule_json="{}")
    assert calls == []


@pytest.mark.asyncio
async def test_a_write_error_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    handlers = _tools(monkeypatch, {"post": _FakeResponse({"message": "forbidden"}, 403)}, calls)

    out = await handlers["create_grafana_alert_rule"](rule_json=json.dumps(_RULE))

    assert "403" in out
    assert len(calls) == 1
