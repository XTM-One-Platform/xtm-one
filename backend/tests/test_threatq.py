"""Tests for the ThreatQuotient (ThreatQ) integration provider.

Covers tool introspection, compound-token parsing (the password is the final
greedy segment and may itself contain ``||``), object-type normalization
(aliases + invalid types), request shapes (pagination clamps, name -> id
resolution on create, the SOLR-style advanced search body), and the
consolidated ``manage_threatq_object`` action routing. API calls route through
the shared ``safe_request`` helper and the token mint is monkeypatched, so no
network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import threatq as tq
from app.integrations.providers.threatq import (
    _normalize_object_type,
    build_tools,
    threatq_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://threatq.example.com||client-id-123||analyst@example.com||s3cret"

_EXPECTED_TOOL_COUNT = 47


class _StubResponse:
    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that records calls and returns queued payloads."""

    def __init__(self, payloads: list[Any]):
        self._payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        payload = self._payloads.pop(0) if self._payloads else {"data": []}
        return _StubResponse(payload), None


async def _fake_token(*_args: Any) -> str:
    return "bearer-token"


def _build(monkeypatch, payloads: list[Any]) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(payloads)
    monkeypatch.setattr(tq, "safe_request", recorder)
    monkeypatch.setattr(tq, "_get_access_token", _fake_token)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + token parsing
# ---------------------------------------------------------------------------


def test_build_tools_returns_expected_tools():
    executor = CallableToolExecutor()
    tools = build_tools(token=_TOKEN, executor=executor)
    names = {t.name for t in tools}
    assert len(tools) == _EXPECTED_TOOL_COUNT
    assert "whoami_threatq" in names
    assert "search_threatq_indicators" in names
    assert "set_threatq_indicator_status" in names
    assert "link_threatq_objects" in names
    assert "manage_threatq_object" in names
    # Reads sort before writes before destructive.
    by_name = {t.name: t for t in tools}
    assert by_name["list_threatq_indicators"].priority == 0
    assert by_name["create_threatq_indicator"].priority == 1
    assert by_name["manage_threatq_object"].priority == 2


def test_build_tools_empty_without_base_url():
    executor = CallableToolExecutor()
    assert build_tools(token="", executor=executor) == []
    assert build_tools(token="||cid||a@b.com||pw", executor=executor) == []


@pytest.mark.asyncio
async def test_partial_credentials_surface_call_time_error(monkeypatch):
    """Missing client_id/email/password still builds tools; calls return an
    actionable error without attempting a token mint."""
    minted = False

    async def _fail_mint(*_args: Any):
        nonlocal minted
        minted = True
        return None

    monkeypatch.setattr(tq, "_get_access_token", _fail_mint)
    executor = CallableToolExecutor()
    tools = build_tools(token="https://tq.example.com||cid||", executor=executor)
    assert len(tools) == _EXPECTED_TOOL_COUNT
    result = await executor.execute_tool(
        ToolCall(id="c1", name="whoami_threatq", arguments={})
    )
    assert "credentials are not configured" in str(result.content)
    assert minted is False


@pytest.mark.asyncio
async def test_build_tools_password_may_contain_delimiter(monkeypatch):
    """The password is the final greedy segment — ``||`` inside it must survive."""
    minted: dict[str, Any] = {}

    async def _capture(base_url, client_id, email, password):
        minted.update(
            base_url=base_url, client_id=client_id, email=email, password=password
        )
        return None  # short-circuits the tool with the auth error

    monkeypatch.setattr(tq, "_get_access_token", _capture)
    executor = CallableToolExecutor()
    build_tools(
        token="https://tq.example.com||cid||a@b.com||pa||ss||word",
        executor=executor,
    )
    result = await executor.execute_tool(
        ToolCall(id="c1", name="whoami_threatq", arguments={})
    )
    assert minted["password"] == "pa||ss||word"
    assert "Failed to authenticate" in str(result.content)


def test_threatq_api_base():
    assert threatq_api_base("https://tq.example.com/") == "https://tq.example.com/api"


# ---------------------------------------------------------------------------
# Object-type normalization
# ---------------------------------------------------------------------------


def test_normalize_object_type_accepts_aliases():
    assert _normalize_object_type("files") == "attachments"
    assert _normalize_object_type("Indicator") == "indicators"
    assert _normalize_object_type("TTPs") == "ttp"
    assert _normalize_object_type("attack pattern") == "attack_pattern"
    assert _normalize_object_type("malware") == "malware"
    assert _normalize_object_type("nonsense") is None


@pytest.mark.asyncio
async def test_invalid_object_type_returns_error(monkeypatch):
    executor, recorder = _build(monkeypatch, [])
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_threatq_objects",
            arguments={"object_type": "bogus"},
        )
    )
    assert "unknown ThreatQ object type" in str(result.content)
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Request shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_indicators_clamps_limit_and_sends_bearer(monkeypatch):
    executor, recorder = _build(monkeypatch, [{"data": [], "total": 0}])
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_threatq_indicators",
            arguments={"limit": 10_000_000},
        )
    )
    call = recorder.calls[0]
    assert call["url"] == "https://threatq.example.com/api/indicators"
    assert call["params"]["limit"] == 200  # clamped
    assert call["headers"]["Authorization"] == "Bearer bearer-token"


@pytest.mark.asyncio
async def test_search_indicators_builds_solr_style_body(monkeypatch):
    executor, recorder = _build(monkeypatch, [{"data": [], "total": 0}])
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="search_threatq_indicators",
            arguments={
                "value_contains": "evil.test",
                "indicator_type": "FQDN",
                "status": "Active",
                "min_score": 6,
            },
        )
    )
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/indicators/query")
    body = call["json"]
    assert body["criteria"] == {"value": {"+contains": "evil.test"}}
    assert {"type_name": "FQDN"} in body["filters"]["+and"]
    assert {"status_name": "Active"} in body["filters"]["+and"]
    assert {"score": {"+gte": 6}} in body["filters"]["+and"]


@pytest.mark.asyncio
async def test_search_indicators_requires_a_filter(monkeypatch):
    executor, recorder = _build(monkeypatch, [])
    result = await executor.execute_tool(
        ToolCall(id="c1", name="search_threatq_indicators", arguments={})
    )
    assert "at least one filter" in str(result.content)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_create_indicator_resolves_type_and_status_names(monkeypatch):
    payloads = [
        {"data": [{"id": 11, "name": "IP Address", "class": "network"}]},
        {"data": [{"id": 2, "name": "Active"}]},
        {"data": [{"id": 7908, "existing": "N"}], "total": 1},
    ]
    executor, recorder = _build(monkeypatch, payloads)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="create_threatq_indicator",
            arguments={
                "value": "1.2.3.4",
                "indicator_type": "ip address",
                "status": "active",
                "source": "XTM One",
                "tlp": "green",
            },
        )
    )
    assert "Created indicator #7908" in str(result.content)
    create_call = recorder.calls[2]
    assert create_call["method"] == "post"
    assert create_call["url"].endswith("/api/indicators")
    item = create_call["json"][0]
    assert item["type_id"] == 11
    assert item["status_id"] == 2
    assert item["class"] == "network"
    assert item["sources"] == [{"name": "XTM One", "tlp": {"name": "GREEN"}}]


@pytest.mark.asyncio
async def test_create_indicator_unknown_type_lists_options(monkeypatch):
    executor, recorder = _build(
        monkeypatch, [{"data": [{"id": 11, "name": "IP Address"}]}]
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="create_threatq_indicator",
            arguments={"value": "x", "indicator_type": "Nope"},
        )
    )
    content = str(result.content)
    assert "unknown ThreatQ indicator type 'Nope'" in content
    assert "IP Address" in content
    assert len(recorder.calls) == 1  # only the resolution call, no create


@pytest.mark.asyncio
async def test_set_indicator_score_clamps_to_valid_range(monkeypatch):
    executor, recorder = _build(monkeypatch, [{"data": {}}])
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="set_threatq_indicator_score",
            arguments={"indicator_id": 5, "score": 99},
        )
    )
    call = recorder.calls[0]
    # Singular /indicator/ prefix — like /indicator/types and
    # /indicator/statuses (the plural form is not a valid scores route).
    assert call["url"].endswith("/api/indicator/5/scores")
    assert call["json"] == {"manual_score": 10}


@pytest.mark.asyncio
async def test_create_event_defaults_happened_at_to_now(monkeypatch):
    """ThreatQ hashes events on title + happened_at, so a timestamp is always sent."""
    payloads = [
        {"data": [{"id": 3, "name": "Sighting"}]},
        {"data": [{"id": 42}], "total": 1},
    ]
    executor, recorder = _build(monkeypatch, payloads)
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="create_threatq_event",
            arguments={"title": "Suspicious login", "event_type": "Sighting"},
        )
    )
    item = recorder.calls[1]["json"][0]
    assert item["type_id"] == 3
    # Defaulted to now — "YYYY-MM-DD HH:MM:SS".
    assert len(item["happened_at"]) == 19
    assert item["happened_at"][4] == "-" and item["happened_at"][10] == " "


@pytest.mark.asyncio
async def test_resolve_named_id_requests_full_list(monkeypatch):
    """Name resolution lists with an explicit high limit (no page truncation)."""
    executor, recorder = _build(
        monkeypatch, [{"data": [{"id": 11, "name": "IP Address"}]}]
    )
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="create_threatq_indicator",
            arguments={"value": "x", "indicator_type": "Nope"},
        )
    )
    assert recorder.calls[0]["params"] == {"limit": 500}


@pytest.mark.asyncio
async def test_link_objects_normalizes_collections(monkeypatch):
    executor, recorder = _build(monkeypatch, [{"data": {}}])
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="link_threatq_objects",
            arguments={
                "object_type": "indicator",
                "object_id": 54,
                "related_type": "adversary",
                "related_id": 9,
            },
        )
    )
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/indicators/54/adversaries")
    assert call["json"] == {"id": 9}


@pytest.mark.asyncio
async def test_update_object_uses_collection_label_field(monkeypatch):
    """Adversaries/events are keyed by name/title, not value."""
    executor, recorder = _build(monkeypatch, [{"data": {}}, {"data": {}}])
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="update_threatq_object",
            arguments={"object_type": "event", "object_id": 4, "value": "New title"},
        )
    )
    await executor.execute_tool(
        ToolCall(
            id="c2",
            name="update_threatq_object",
            arguments={"object_type": "malware", "object_id": 8, "value": "Emotet"},
        )
    )
    assert recorder.calls[0]["json"] == {"title": "New title"}
    assert recorder.calls[1]["json"] == {"value": "Emotet"}


# ---------------------------------------------------------------------------
# Consolidated management tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_object_delete(monkeypatch):
    executor, recorder = _build(monkeypatch, [{"data": {}}])
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="manage_threatq_object",
            arguments={
                "action": "delete_object",
                "object_type": "ttp",
                "object_id": 3,
            },
        )
    )
    assert recorder.calls[0]["method"] == "delete"
    assert recorder.calls[0]["url"].endswith("/api/ttp/3")
    assert "permanently deleted" in str(result.content)


@pytest.mark.asyncio
async def test_manage_object_unknown_action(monkeypatch):
    executor, recorder = _build(monkeypatch, [])
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="manage_threatq_object",
            arguments={
                "action": "explode",
                "object_type": "indicators",
                "object_id": 1,
            },
        )
    )
    assert "unknown action" in str(result.content)
    assert recorder.calls == []
