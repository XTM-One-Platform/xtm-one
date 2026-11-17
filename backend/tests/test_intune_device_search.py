"""Intune device search goes through ``$filter``, never ``$search``.

Graph answers ``$search`` on ``/deviceManagement/managedDevices`` with HTTP 400
("The query parameter '$search' is not supported"), so ``intune_list_devices``
turns ``search`` into ``startswith(deviceName,'...')``, or
``userPrincipalName eq '...'`` when the value is an email, and joins it with
the other criteria using ``and``, the caller's own ``filter`` in parentheses.

The tool runs for real against a fake Graph served through
``httpx.MockTransport``: ``safe_request`` and ``AutoRefreshClient`` are the
production code paths. The fake refuses ``$search`` and any clause it does not
know the way Graph does, evaluates ``startswith`` and ``eq``
case-insensitively, as Graph does, and applies OData precedence: ``and``
binds tighter than ``or``, parentheses group.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.integrations.providers import intune
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

DEVICES_PATH = "/v1.0/deviceManagement/managedDevices"

DEVICES: list[dict[str, Any]] = [
    {
        "id": "d-0065",
        "deviceName": "LAPT-FILI-0065",
        "userPrincipalName": "jane.smith@example.com",
        "operatingSystem": "Windows",
        "complianceState": "compliant",
        "managementAgent": "mdm",
    },
    {
        "id": "d-0071",
        "deviceName": "LAPT-FILI-0071",
        "userPrincipalName": "jane.smith@example.com",
        "operatingSystem": "macOS",
        "complianceState": "noncompliant",
        "managementAgent": "mdm",
    },
    {
        "id": "d-0003",
        "deviceName": "DESK-FILI-0003",
        "userPrincipalName": "jane.doe@filigran.io",
        "operatingSystem": "Windows",
        "complianceState": "noncompliant",
        "managementAgent": "mdm",
    },
    {
        "id": "d-0004",
        "deviceName": "DESK-FILI-0004",
        "userPrincipalName": "jane.doe@filigran.io",
        "operatingSystem": "Windows",
        "complianceState": "noncompliant",
        "managementAgent": "eas",
    },
    {
        "id": "d-0009",
        "deviceName": "O'BRIEN-PC",
        "userPrincipalName": "sean.o'brien@filigran.io",
        "operatingSystem": "Windows",
        "complianceState": "compliant",
        "managementAgent": "mdm",
    },
]

Predicate = Callable[[dict[str, Any]], bool]

_TOKEN = re.compile(
    r"\s*(?:"
    r"startswith\((?P<prefix_of>\w+),'(?P<prefix>(?:[^']|'')*)'\)"
    r"|(?P<equal_to>\w+) eq '(?P<value>(?:[^']|'')*)'"
    r"|(?P<keyword>(?:and|or)\b|\(|\))"
    r")"
)


def _literal(raw: str) -> str:
    return raw.replace("''", "'").lower()


def _startswith(prop: str, prefix: str) -> Predicate:
    return lambda device: str(device.get(prop, "")).lower().startswith(prefix)


def _equals(prop: str, value: str) -> Predicate:
    return lambda device: str(device.get(prop, "")).lower() == value


def _tokens(expression: str) -> list[str | Predicate]:
    tokens: list[str | Predicate] = []
    position, end = 0, len(expression.rstrip())
    while position < end:
        match = _TOKEN.match(expression, position)
        if match is None:
            raise ValueError(f"unknown clause at {expression[position:]!r}")
        position = match.end()
        if match["keyword"]:
            tokens.append(match["keyword"])
        elif match["prefix_of"]:
            tokens.append(_startswith(match["prefix_of"], _literal(match["prefix"])))
        else:
            tokens.append(_equals(match["equal_to"], _literal(match["value"])))
    return tokens


class _FilterParser:
    """``or`` of ``and`` groups of comparisons or parenthesised expressions."""

    def __init__(self, expression: str) -> None:
        self.tokens = _tokens(expression)
        self.position = 0

    def parse(self) -> Predicate:
        predicate = self._disjunction()
        if self.position != len(self.tokens):
            raise ValueError("trailing tokens")
        return predicate

    def _take(self, keyword: str) -> bool:
        if self.position < len(self.tokens) and self.tokens[self.position] == keyword:
            self.position += 1
            return True
        return False

    def _disjunction(self) -> Predicate:
        terms = [self._conjunction()]
        while self._take("or"):
            terms.append(self._conjunction())
        return lambda device: any(term(device) for term in terms)

    def _conjunction(self) -> Predicate:
        factors = [self._factor()]
        while self._take("and"):
            factors.append(self._factor())
        return lambda device: all(factor(device) for factor in factors)

    def _factor(self) -> Predicate:
        if self._take("("):
            inner = self._disjunction()
            if not self._take(")"):
                raise ValueError("unbalanced parenthesis")
            return inner
        token = self.tokens[self.position] if self.position < len(self.tokens) else None
        if not callable(token):
            raise ValueError("expected a comparison")
        self.position += 1
        return token


def _graph_error(message: str) -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "error": {
                "code": "BadRequest",
                "message": message,
                "innerError": {"request-id": "5bacff79-0000-0000-0000-000000000000"},
            }
        },
    )


class FakeGraph:
    """``GET managedDevices`` with Graph's ``$search`` refusal and a small ``$filter`` evaluator."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.failure: httpx.Response | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path == DEVICES_PATH, request.url
        if self.failure is not None:
            return self.failure
        params = request.url.params
        if "$search" in params:
            return _graph_error("The query parameter '$search' is not supported.")
        devices = DEVICES
        if "$filter" in params:
            try:
                predicate = _FilterParser(params["$filter"]).parse()
            except ValueError:
                return _graph_error("Invalid filter clause")
            devices = [d for d in devices if predicate(d)]
        return httpx.Response(200, json={"value": devices[: int(params.get("$top", "1000"))]})

    @property
    def last_filter(self) -> str | None:
        return self.requests[-1].url.params.get("$filter")


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> FakeGraph:
    fake = FakeGraph()
    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return fake


async def _list_devices(**arguments: Any) -> ToolResult:
    executor = CallableToolExecutor()
    intune.build_tools("tok", executor)
    return await executor.execute_tool(ToolCall(id="c1", name="intune_list_devices", arguments=arguments))


def _device_names(result: ToolResult) -> set[str]:
    return {d["deviceName"] for d in DEVICES if f"• {d['deviceName']} (ID:" in result.content}


@pytest.mark.asyncio
async def test_search_by_device_name_returns_the_device(graph: FakeGraph) -> None:
    result = await _list_devices(search="LAPT-FILI-0065")

    assert not result.is_error
    assert _device_names(result) == {"LAPT-FILI-0065"}
    request = graph.requests[-1]
    assert request.url.params["$filter"] == "startswith(deviceName,'LAPT-FILI-0065')"
    assert "$search" not in request.url.params
    assert "$orderby" not in request.url.params
    assert "ConsistencyLevel" not in request.headers


@pytest.mark.asyncio
async def test_search_without_a_match_is_an_empty_list_not_an_error(graph: FakeGraph) -> None:
    result = await _list_devices(search="dexpc")

    assert not result.is_error
    assert result.content == "No devices found."
    assert graph.last_filter == "startswith(deviceName,'dexpc')"


@pytest.mark.asyncio
async def test_search_by_email_matches_the_owner(graph: FakeGraph) -> None:
    result = await _list_devices(search="jane.smith@example.com")

    assert not result.is_error
    assert _device_names(result) == {"LAPT-FILI-0065", "LAPT-FILI-0071"}
    assert graph.last_filter == "userPrincipalName eq 'jane.smith@example.com'"


@pytest.mark.asyncio
async def test_search_keeps_the_value_as_given(graph: FakeGraph) -> None:
    result = await _list_devices(search="  lapt-fili ")

    assert graph.last_filter == "startswith(deviceName,'lapt-fili')"
    assert _device_names(result) == {"LAPT-FILI-0065", "LAPT-FILI-0071"}


@pytest.mark.asyncio
async def test_filter_and_compliance_state_combine(graph: FakeGraph) -> None:
    result = await _list_devices(filter="managementAgent eq 'mdm'", compliance_state="noncompliant")

    assert not result.is_error
    assert graph.last_filter == "(managementAgent eq 'mdm') and complianceState eq 'noncompliant'"
    assert _device_names(result) == {"LAPT-FILI-0071", "DESK-FILI-0003"}


@pytest.mark.asyncio
async def test_a_filter_alone_is_sent_as_given(graph: FakeGraph) -> None:
    result = await _list_devices(filter="  managementAgent eq 'eas' ")

    assert graph.last_filter == "managementAgent eq 'eas'"
    assert _device_names(result) == {"DESK-FILI-0004"}


@pytest.mark.asyncio
async def test_an_or_filter_cannot_escape_the_other_criteria(graph: FakeGraph) -> None:
    by_search = await _list_devices(
        filter="operatingSystem eq 'Windows' or operatingSystem eq 'macOS'",
        search="LAPT",
    )
    assert graph.last_filter == (
        "(operatingSystem eq 'Windows' or operatingSystem eq 'macOS') and startswith(deviceName,'LAPT')"
    )
    assert _device_names(by_search) == {"LAPT-FILI-0065", "LAPT-FILI-0071"}

    by_owner = await _list_devices(
        filter="complianceState eq 'compliant' or complianceState eq 'noncompliant'",
        user_principal_name="jane.doe@filigran.io",
    )
    assert graph.last_filter == (
        "(complianceState eq 'compliant' or complianceState eq 'noncompliant') "
        "and userPrincipalName eq 'jane.doe@filigran.io'"
    )
    assert _device_names(by_owner) == {"DESK-FILI-0003", "DESK-FILI-0004"}


@pytest.mark.asyncio
async def test_every_criterion_joins_with_and(graph: FakeGraph) -> None:
    result = await _list_devices(
        filter="managementAgent eq 'mdm'",
        compliance_state="noncompliant",
        operating_system=" Windows ",
        search="DESK",
    )

    assert graph.last_filter == (
        "(managementAgent eq 'mdm') and complianceState eq 'noncompliant' "
        "and operatingSystem eq 'Windows' and startswith(deviceName,'DESK')"
    )
    assert _device_names(result) == {"DESK-FILI-0003"}


@pytest.mark.asyncio
async def test_top_stays_within_graph_page_bounds(graph: FakeGraph) -> None:
    await _list_devices(top=0)
    assert graph.requests[-1].url.params["$top"] == "1"

    await _list_devices(top=500)
    assert graph.requests[-1].url.params["$top"] == "100"


@pytest.mark.asyncio
async def test_device_name_is_an_exact_match(graph: FakeGraph) -> None:
    result = await _list_devices(device_name="DESK-FILI-0003")

    assert graph.last_filter == "deviceName eq 'DESK-FILI-0003'"
    assert _device_names(result) == {"DESK-FILI-0003"}


@pytest.mark.asyncio
async def test_user_principal_name_is_an_exact_match(graph: FakeGraph) -> None:
    result = await _list_devices(user_principal_name="jane.doe@filigran.io", compliance_state="noncompliant")

    assert graph.last_filter == ("complianceState eq 'noncompliant' and userPrincipalName eq 'jane.doe@filigran.io'")
    assert _device_names(result) == {"DESK-FILI-0003", "DESK-FILI-0004"}


@pytest.mark.asyncio
async def test_single_quotes_are_escaped(graph: FakeGraph) -> None:
    by_name = await _list_devices(search="O'Brien")
    assert graph.last_filter == "startswith(deviceName,'O''Brien')"
    assert _device_names(by_name) == {"O'BRIEN-PC"}

    by_owner = await _list_devices(search="sean.o'brien@filigran.io")
    assert graph.last_filter == "userPrincipalName eq 'sean.o''brien@filigran.io'"
    assert _device_names(by_owner) == {"O'BRIEN-PC"}

    exact = await _list_devices(device_name="O'BRIEN-PC", user_principal_name="sean.o'brien@filigran.io")
    assert graph.last_filter == ("deviceName eq 'O''BRIEN-PC' and userPrincipalName eq 'sean.o''brien@filigran.io'")
    assert _device_names(exact) == {"O'BRIEN-PC"}


@pytest.mark.asyncio
async def test_a_refused_filter_returns_graph_message_and_the_filterable_properties(
    graph: FakeGraph,
) -> None:
    result = await _list_devices(filter="contains(deviceName,'FILI')", compliance_state="compliant")

    assert result.is_error
    assert "$filter=(contains(deviceName,'FILI')) and complianceState eq 'compliant'" in result.content
    assert "(HTTP 400): Invalid filter clause\n" in result.content
    assert result.content.endswith(
        "Hint: managedDevices can be filtered on deviceName, userPrincipalName, serialNumber, "
        "operatingSystem, complianceState, managementAgent, azureADDeviceId, lastSyncDateTime."
    )


@pytest.mark.asyncio
async def test_other_failures_keep_the_shared_wording(graph: FakeGraph) -> None:
    graph.failure = httpx.Response(
        403,
        json={"error": {"code": "Forbidden", "message": "Application is not authorized to perform this operation."}},
    )
    result = await _list_devices(search="LAPT")

    assert result.is_error
    assert result.content.startswith("Error: Microsoft Intune access denied while trying to list managed devices")
    assert "Hint:" not in result.content


@pytest.mark.asyncio
async def test_no_call_sends_the_advanced_query_header(graph: FakeGraph) -> None:
    executor = CallableToolExecutor()
    intune.build_tools("tok", executor)
    for arguments in ({"search": "LAPT"}, {}):
        await executor.execute_tool(ToolCall(id="c1", name="intune_list_devices", arguments=arguments))

    assert len(graph.requests) == 2
    assert all("ConsistencyLevel" not in request.headers for request in graph.requests)
    assert "$filter" not in graph.requests[-1].url.params


def test_graph_error_message_falls_back_to_the_body() -> None:
    html = httpx.Response(400, text="<html>Bad Request</html>")
    assert intune._graph_error_message(html) == "<html>Bad Request</html>"

    envelope = httpx.Response(400, json={"error": {"code": "BadRequest", "message": "Nope"}})
    assert intune._graph_error_message(envelope) == "Nope"

    long = httpx.Response(400, json={"error": {"code": "BadRequest", "message": "x" * 2000}})
    assert intune._graph_error_message(long) == "x" * 500
