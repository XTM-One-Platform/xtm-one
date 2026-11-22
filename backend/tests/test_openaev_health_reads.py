"""The OpenAEV module health reads of the module checker (#101 S15b-2c,
platform-health.md 13.5): registered, read-only by name and by method, allow-listed,
and answering what the module holds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.integrations.providers.openaev import build_tools, health_reads
from app.services.health import module_analyst
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"
HEALTH_NAMES = ("get_openaev_platform_health", "list_openaev_expectations_due")


class _Resp:
    def __init__(self, body: Any) -> None:
        self._body = body

    def json(self) -> Any:
        return self._body


class _FakeApi:
    """Answers by path; records every (method, path) the reads send."""

    def __init__(self, answers: dict[str, tuple[Any, str | None]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self, base_url: str, headers: Any, method: str, path: str, action: str, **kwargs: Any
    ) -> tuple[_Resp | None, str | None]:
        self.calls.append((method, path))
        body, err = self.answers.get(path, (None, "404 not found"))
        return (None, err) if err else (_Resp(body), None)


async def _call(monkeypatch: pytest.MonkeyPatch, fake: _FakeApi, name: str) -> str:
    monkeypatch.setattr(health_reads, "_api", fake)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="c", name=name, arguments={}))
    assert not result.is_error, result.content
    return result.content


def test_the_module_health_reads_are_registered_read_only_and_allow_listed() -> None:
    executor = CallableToolExecutor()
    defs = {t.name: t for t in build_tools(TOKEN, executor)}
    for name in HEALTH_NAMES:
        assert name in defs and name in executor._handlers, name
        assert infer_tool_type(name) == "read", name
        assert module_analyst.refuse_module_tool("openaev", name) is None, name
        assert defs[name].input_schema["properties"] == {}, name
        assert len(defs[name].description) <= 200, name


async def test_every_request_of_the_reads_is_a_get(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeApi(
        {"/api/settings": ({}, None), "/api/tenant-settings": ({}, None), "/api/injects/expectations": ([], None)}
    )
    for name in HEALTH_NAMES:
        await _call(monkeypatch, fake, name)
    assert fake.calls and all(method == "get" for method, _ in fake.calls)


async def test_platform_health_answers_version_licence_services_and_links(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeApi(
        {
            "/api/settings": (
                {
                    "platform_version": "3.261116.1",
                    "platform_license": {
                        "license_type": "standard",
                        "license_is_enterprise": True,
                        "license_is_validated": True,
                        "license_is_expired": False,
                        "license_expiration_date": "2027-01-31T00:00:00Z",
                    },
                    "xtm_composer_version": "1.2.0",
                    "xtm_hub_enable": True,
                    "xtm_hub_reachable": False,
                    "smtp_service_available": "true",
                },
                None,
            ),
            "/api/tenant-settings": (
                {"xtm_opencti_enable": True, "xtm_opencti_url": "https://cti.example", "xtm_opencrq_enable": False},
                None,
            ),
        }
    )
    out = await _call(monkeypatch, fake, "get_openaev_platform_health")
    assert "Version: 3.261116.1" in out
    assert "enterprise true, validated true, expired false, expires 2027-01-31T00:00:00Z" in out
    assert "XTM Hub reachable: false" in out
    assert "IMAP available: unknown" in out
    assert "OpenCTI link: enabled true, URL set true, last call result not exposed by OpenAEV" in out
    assert "OpenCRQ link: enabled false, URL set false" in out


async def test_platform_health_keeps_going_past_a_refused_read(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeApi(
        {"/api/settings": (None, "403 forbidden"), "/api/tenant-settings": ({"xtm_opencti_enable": False}, None)}
    )
    out = await _call(monkeypatch, fake, "get_openaev_platform_health")
    assert "Platform settings: 403 forbidden" in out
    assert "OpenCTI link: enabled false" in out


async def test_expectations_due_count_by_type_overdue_and_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    rows = [
        {
            "inject_expectation_type": "DETECTION",
            "inject_expectation_created_at": (now - timedelta(hours=10)).isoformat(),
            "inject_expiration_time": 3600,
        },
        {
            "inject_expectation_type": "PREVENTION",
            "inject_expectation_created_at": (now - timedelta(minutes=5)).isoformat(),
            "inject_expiration_time": 3600,
        },
        {"inject_expectation_type": "MANUAL"},
    ]
    fake = _FakeApi({"/api/injects/expectations": (rows, None)})
    out = await _call(monkeypatch, fake, "list_openaev_expectations_due")
    assert "3 (DETECTION 1, MANUAL 1, PREVENTION 1)" in out
    assert "1 past their expiration time" in out
    assert "oldest created " + (now - timedelta(hours=10)).isoformat()[:16] in out


async def test_expectations_due_none_and_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    out = await _call(monkeypatch, _FakeApi({"/api/injects/expectations": ([], None)}), "list_openaev_expectations_due")
    assert out == "OpenAEV expectations due: none waiting for a result."
    out = await _call(monkeypatch, _FakeApi({}), "list_openaev_expectations_due")
    assert out == "OpenAEV expectations due: 404 not found"
