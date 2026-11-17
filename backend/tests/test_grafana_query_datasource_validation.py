"""Regression tests for run_grafana_query datasource validation.

Verifies that run_grafana_query returns a clear, actionable error message
when neither datasource_uid nor a valid datasource_id is supplied, instead
of silently forwarding an incomplete query to Grafana's /api/ds/query and
receiving an HTTP 400 "Query does not contain a valid data source identifier".
"""

from __future__ import annotations

import pytest

from app.api.v1.integration_catalog import build_dummy_token
from app.integrations.providers.grafana import build_tools
from app.tools.base import CallableToolExecutor


def _get_query_tool(executor: CallableToolExecutor):
    """Return the registered run_grafana_query callable."""
    fn = executor._handlers.get("run_grafana_query")
    assert fn is not None, "run_grafana_query must be registered with the executor"
    return fn


def _forbid_safe_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch safe_request so any HTTP attempt fails the test.

    The regression being guarded is specifically about sending invalid
    payloads to Grafana's /api/ds/query — the client-side guard must
    return an error WITHOUT any network call.
    """

    async def forbidden(*args, **kwargs):
        raise AssertionError(
            "safe_request must not be called when no valid datasource "
            "identifier is supplied — the client-side guard must return "
            "an error before any HTTP request"
        )

    monkeypatch.setattr("app.integrations.providers.grafana.safe_request", forbidden)


@pytest.mark.asyncio
async def test_run_grafana_query_missing_both_identifiers_returns_error(monkeypatch):
    """Calling run_grafana_query without any datasource identifier must return
    an error string that directs the agent to call list_grafana_data_sources first,
    without sending any request to Grafana."""
    _forbid_safe_request(monkeypatch)
    executor = CallableToolExecutor()
    build_tools(token=build_dummy_token("grafana"), executor=executor)
    fn = _get_query_tool(executor)

    result = await fn(expr='up{job="prometheus"}')

    assert "Error" in result
    assert "datasource" in result.lower()
    assert "list_grafana_data_sources" in result


@pytest.mark.asyncio
async def test_run_grafana_query_zero_datasource_id_returns_error(monkeypatch):
    """datasource_id=0 must be treated as missing (not a valid identifier)."""
    _forbid_safe_request(monkeypatch)
    executor = CallableToolExecutor()
    build_tools(token=build_dummy_token("grafana"), executor=executor)
    fn = _get_query_tool(executor)

    result = await fn(expr='up{job="prometheus"}', datasource_id=0)

    assert "Error" in result
    assert "list_grafana_data_sources" in result


@pytest.mark.asyncio
async def test_run_grafana_query_negative_datasource_id_returns_error(monkeypatch):
    """Negative datasource_id values must also be rejected client-side."""
    _forbid_safe_request(monkeypatch)
    executor = CallableToolExecutor()
    build_tools(token=build_dummy_token("grafana"), executor=executor)
    fn = _get_query_tool(executor)

    result = await fn(expr='up{job="prometheus"}', datasource_id=-1)

    assert "Error" in result
    assert "list_grafana_data_sources" in result


@pytest.mark.asyncio
async def test_run_grafana_query_empty_uid_returns_error(monkeypatch):
    """An empty string datasource_uid must be treated as missing."""
    _forbid_safe_request(monkeypatch)
    executor = CallableToolExecutor()
    build_tools(token=build_dummy_token("grafana"), executor=executor)
    fn = _get_query_tool(executor)

    result = await fn(expr='up{job="prometheus"}', datasource_uid="")

    assert "Error" in result
    assert "list_grafana_data_sources" in result


class _FakeResponse:
    """Minimal httpx.Response stand-in for the mocked safe_request."""

    @staticmethod
    def json() -> dict:
        return {"results": {}}


def _capture_safe_request(captured: dict):
    """Return a safe_request replacement that records the outgoing JSON body."""

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _FakeResponse(), None

    return fake_safe_request


@pytest.mark.asyncio
async def test_run_grafana_query_uid_sent_as_datasource_uid(monkeypatch):
    """A datasource_uid must be sent as query.datasource.uid — the field
    Grafana's /api/ds/query parser resolves first."""
    captured: dict = {}
    monkeypatch.setattr(
        "app.integrations.providers.grafana.safe_request",
        _capture_safe_request(captured),
    )
    executor = CallableToolExecutor()
    build_tools(token=build_dummy_token("grafana"), executor=executor)
    fn = _get_query_tool(executor)

    await fn(expr='up{job="prometheus"}', datasource_uid="P8E80F9AEF21F6940")

    query = captured["json"]["queries"][0]
    assert query["datasource"] == {"uid": "P8E80F9AEF21F6940"}
    assert "datasourceId" not in query


@pytest.mark.asyncio
async def test_run_grafana_query_numeric_id_sent_as_top_level_datasource_id(
    monkeypatch,
):
    """A numeric datasource_id must be sent as the legacy TOP-LEVEL
    query.datasourceId integer field. Grafana's query parser never reads
    query.datasource.id, so nesting the numeric ID under "datasource" would
    reproduce the exact HTTP 400 this validation exists to prevent."""
    captured: dict = {}
    monkeypatch.setattr(
        "app.integrations.providers.grafana.safe_request",
        _capture_safe_request(captured),
    )
    executor = CallableToolExecutor()
    build_tools(token=build_dummy_token("grafana"), executor=executor)
    fn = _get_query_tool(executor)

    await fn(expr='up{job="prometheus"}', datasource_id=42)

    query = captured["json"]["queries"][0]
    assert query["datasourceId"] == 42
    assert "datasource" not in query


def test_run_grafana_query_schema_has_no_default_for_datasource_id():
    """The ToolDefinition schema must NOT carry any default for datasource_id
    so that LLMs cannot silently omit the field and receive an apparently-valid call."""
    executor = CallableToolExecutor()
    tools = build_tools(token=build_dummy_token("grafana"), executor=executor)
    tool = next(t for t in tools if t.name == "run_grafana_query")

    props = tool.input_schema.get("properties", {})
    assert "datasource_id" in props, "datasource_id must remain a declared property of the run_grafana_query schema"
    datasource_id_prop = props["datasource_id"]
    assert "default" not in datasource_id_prop, (
        "datasource_id must not carry a schema default — this causes LLMs to silently omit a real identifier"
    )


def test_run_grafana_query_schema_has_no_default_for_datasource_uid():
    """The ToolDefinition schema must NOT carry any default for datasource_uid
    so that LLMs cannot silently omit the field."""
    executor = CallableToolExecutor()
    tools = build_tools(token=build_dummy_token("grafana"), executor=executor)
    tool = next(t for t in tools if t.name == "run_grafana_query")

    props = tool.input_schema.get("properties", {})
    assert "datasource_uid" in props, "datasource_uid must remain a declared property of the run_grafana_query schema"
    datasource_uid_prop = props["datasource_uid"]
    assert "default" not in datasource_uid_prop, (
        "datasource_uid must not carry a schema default — this causes LLMs to silently omit a real identifier"
    )


def test_run_grafana_query_description_mentions_list_datasources():
    """The run_grafana_query ToolDefinition description must direct agents to call
    list_grafana_data_sources when they don't know the datasource identifier."""
    executor = CallableToolExecutor()
    tools = build_tools(token=build_dummy_token("grafana"), executor=executor)
    tool = next(t for t in tools if t.name == "run_grafana_query")

    assert "list_grafana_data_sources" in tool.description, (
        "run_grafana_query description must reference list_grafana_data_sources so agents know to call it first"
    )
