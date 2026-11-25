"""manage_openaev_expectation action=traces always sends both parameters (#2371).

``GET /api/inject-expectations-traces`` takes ``injectExpectationId`` AND
``sourceId`` as required parameters: a call without ``sourceId`` is a bare
Spring 400. Without an explicit ``source_id`` the tool reads the collector
sources of the expectation row and calls once per source.
"""

from typing import Any

import pytest

from app.integrations.providers.openaev import build_tools
from app.integrations.providers.openaev import expectations as expectations_mod
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"

_TRACES = "/api/inject-expectations-traces"
_TARGET_RESULTS = "/api/atomic-testings/i1/target_results/a1/types/AGENT"

_TRACE = {
    "inject_expectation_trace_alert_name": "Suspicious LSASS access",
    "inject_expectation_trace_date": "2026-11-25T10:00:00Z",
    "inject_expectation_trace_alert_link": "https://edr.example/alerts/42",
}


class _Resp:
    def __init__(self, data: Any) -> None:
        self._data = data

    def json(self) -> Any:
        return self._data


class _RoutedApi:
    """Stub for ``_api``: answers per path, records every call with its params."""

    def __init__(self, routes: dict[str, Any], errors: dict[str, str] | None = None) -> None:
        self.routes = routes
        self.errors = errors or {}
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        base_url: str,
        headers: dict[str, str],
        method: str,
        path: str,
        action: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
        **kw: Any,
    ) -> tuple[_Resp | None, str | None]:
        self.calls.append({"method": method, "path": path, "params": params})
        if path in self.errors:
            return None, self.errors[path]
        data = self.routes.get(path, [])
        if callable(data):
            data = data(params or {})
        return _Resp(data), None

    def traces_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["path"] == _TRACES]


def _row(results: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "inject_expectation_id": "x1",
        "inject_expectation_type": "DETECTION",
        "inject_expectation_status": "SUCCESS",
        "inject_expectation_agent": "a1",
        "inject_expectation_results": results,
    }


async def _traces(monkeypatch: pytest.MonkeyPatch, fake: _RoutedApi, **args: str) -> Any:
    monkeypatch.setattr(expectations_mod, "_api", fake)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    return await executor.execute_tool(
        ToolCall(
            id="call-traces",
            name="manage_openaev_expectation",
            arguments={"action": "traces", "expectation_id": "x1", **args},
        )
    )


_LOCATOR = {"inject_id": "i1", "target_id": "a1", "target_type": "agent"}


async def test_traces_without_source_id_sends_both_parameters_per_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RoutedApi(
        {
            _TARGET_RESULTS: [
                _row(
                    [
                        {
                            "sourceId": "c-edr",
                            "sourceType": "collector",
                            "sourceName": "Defender",
                            "result": "Detected",
                        },
                        {
                            "sourceId": "ui-1",
                            "sourceType": "player-manual-validation",
                            "sourceName": "Analyst",
                            "result": "Detected",
                        },
                    ]
                )
            ],
            _TRACES: lambda p: [_TRACE] if p.get("sourceId") == "c-edr" else [],
        }
    )
    result = await _traces(monkeypatch, fake, **_LOCATOR)
    assert not result.is_error, result.content
    calls = fake.traces_calls()
    assert [c["params"] for c in calls] == [{"injectExpectationId": "x1", "sourceId": "c-edr"}]
    assert "Suspicious LSASS access" in result.content
    assert "Defender" in result.content


async def test_traces_merges_and_labels_several_collector_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _RoutedApi(
        {
            _TARGET_RESULTS: [
                _row(
                    [
                        {"sourceId": "c-edr", "sourceType": "collector", "sourceName": "Defender"},
                        {"sourceId": "c-siem", "sourceType": "collector", "sourceName": "Splunk"},
                    ]
                )
            ],
            _TRACES: lambda p: [_TRACE],
        }
    )
    result = await _traces(monkeypatch, fake, **_LOCATOR)
    assert not result.is_error, result.content
    assert {c["params"]["sourceId"] for c in fake.traces_calls()} == {"c-edr", "c-siem"}
    assert "Alert evidence (2)" in result.content
    assert "Source Defender (c-edr)" in result.content
    assert "Source Splunk (c-siem)" in result.content


async def test_traces_on_a_row_without_collector_source_makes_no_traces_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RoutedApi(
        {
            _TARGET_RESULTS: [
                _row([{"sourceId": "ui-1", "sourceType": "player-manual-validation", "sourceName": "Analyst"}])
            ]
        }
    )
    result = await _traces(monkeypatch, fake, **_LOCATOR)
    assert not result.is_error, result.content
    assert fake.traces_calls() == []
    assert "No collector scored expectation x1" in result.content


async def test_traces_without_locator_probes_the_platform_bound_collectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RoutedApi(
        {
            "/api/collectors": [
                {"collector_id": "c-edr", "collector_name": "Defender", "collector_security_platform": "sp1"},
                {"collector_id": "c-internal", "collector_name": "Built-in"},
            ],
            _TRACES: [],
        }
    )
    result = await _traces(monkeypatch, fake)
    assert not result.is_error, result.content
    assert [c["params"] for c in fake.traces_calls()] == [{"injectExpectationId": "x1", "sourceId": "c-edr"}]
    assert "Not Detected row normally has none" in result.content


async def test_traces_400_names_the_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _RoutedApi(
        {},
        errors={_TRACES: "Error: OpenAEV rejected the request to get expectation traces — bad request. Bad Request"},
    )
    result = await _traces(monkeypatch, fake, source_id="c-edr")
    assert not result.is_error, result.content
    out = result.content
    assert "injectExpectationId" in out and "sourceId" in out
    assert "sent: x1" in out and "sent: c-edr" in out


async def test_traces_with_explicit_source_id_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _RoutedApi({_TRACES: [_TRACE]})
    result = await _traces(monkeypatch, fake, source_id="c-edr", **_LOCATOR)
    assert not result.is_error, result.content
    # One call, straight to the traces endpoint: no row read, no collector probe.
    assert [(c["path"], c["params"]) for c in fake.calls] == [
        (_TRACES, {"injectExpectationId": "x1", "sourceId": "c-edr"})
    ]
    assert "Suspicious LSASS access" in result.content
