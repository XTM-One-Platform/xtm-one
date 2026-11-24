"""Defender XDR custom detection rule tools (Graph beta security/rules/detectionRules).

The Graph calls are recorded through a stub of ``safe_request``: each test checks
the request the tool sends and the answer it gives, including the 403 that names
the CustomDetection permission to grant.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import defender as df
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor, ToolCall

_RULES = "https://graph.microsoft.com/beta/security/rules/detectionRules"
_NEW_TOOLS = (
    "list_defender_detection_rules",
    "get_defender_detection_rule",
    "create_defender_detection_rule",
    "update_defender_detection_rule",
    "delete_defender_detection_rule",
)
_QUERY = (
    'DeviceProcessEvents | where InitiatingProcessFileName =~ "node.exe" and FileName in~ '
    '("cmd.exe","powershell.exe") | project Timestamp, DeviceId, ReportId'
)
_STORED_RULE: dict[str, Any] = {
    "id": "72",
    "displayName": "Node spawns a shell",
    "isEnabled": True,
    "createdBy": "analyst@example.com",
    "createdDateTime": "2026-11-10T08:00:00Z",
    "queryCondition": {"queryText": _QUERY},
    "schedule": {"period": "24H"},
    "lastRunDetails": {"lastRunDateTime": "2026-11-10T09:00:00Z", "status": "completed"},
    "detectionAction": {
        "alertTemplate": {
            "title": "Test rule",
            "description": "Node.js spawning a shell",
            "severity": "low",
            "category": "Execution",
            "mitreTechniques": ["T1059"],
            "recommendedActions": None,
            "entityMappings": {"hosts": [{"deviceIdColumn": "DeviceId"}]},
        },
        "responseActions": [],
        "organizationalScope": None,
    },
}


class _Response:
    def __init__(self, status: int, payload: dict[str, Any] | None = None):
        self.status_code = status
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _Graph:
    """A stub of ``safe_request`` answering from a queue, recording each call."""

    def __init__(self, *answers: tuple[int, dict[str, Any] | None]):
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        status, payload = self.answers.pop(0) if self.answers else (200, {})
        resp = _Response(status, payload)
        if status >= 400:
            err = f"Error: {provider} HTTP {status} while trying to {action}."
            return (resp if kwargs.get("expected_statuses") is not None else None), err
        return resp, None


async def _token(*_a, **_k) -> str:
    return "graph-token"


def _executor(monkeypatch, graph: _Graph) -> CallableToolExecutor:
    monkeypatch.setattr(df, "safe_request", graph)
    monkeypatch.setattr(df, "_get_token", _token)
    executor = CallableToolExecutor()
    df.build_tools(token="tenant||client||secret", executor=executor)
    return executor


async def _call(executor: CallableToolExecutor, name: str, **arguments: Any) -> str:
    result = await executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments))
    return result.content if isinstance(result.content, str) else str(result.content)


def test_the_five_tools_are_declared_with_read_and_write_access():
    executor = CallableToolExecutor()
    names = {t.name for t in df.build_tools(token="tenant||client||secret", executor=executor)}
    assert set(_NEW_TOOLS) <= names
    assert infer_tool_type("list_defender_detection_rules") == "read"
    assert infer_tool_type("get_defender_detection_rule") == "read"
    for name in ("create_defender_detection_rule", "update_defender_detection_rule", "delete_defender_detection_rule"):
        assert infer_tool_type(name) == "write"


def test_the_existing_tools_are_kept():
    executor = CallableToolExecutor()
    names = [t.name for t in df.build_tools(token="tenant||client||secret", executor=executor)]
    assert len(names) == len(set(names))
    # 49 before #863, plus the three Live Response tools (run script, library, result).
    assert len([n for n in names if n not in _NEW_TOOLS]) == 52
    assert {"create_defender_indicator", "run_defender_hunting_query"} <= set(names)


@pytest.mark.asyncio
async def test_create_sends_the_rule_and_returns_its_id(monkeypatch):
    graph = _Graph((201, {"id": "72"}))
    executor = _executor(monkeypatch, graph)

    answer = await _call(
        executor,
        "create_defender_detection_rule",
        display_name="Node spawns a shell",
        query=_QUERY,
        alert_title="Test rule",
        period="24H",
        severity="Low",
        category="Execution",
        mitre_techniques=["T1059"],
        impacted_assets=[{"type": "device", "identifier": "DeviceId"}],
    )

    assert "id: 72" in answer
    call = graph.calls[0]
    assert call["method"] == "post" and call["url"] == _RULES
    body = call["json"]
    assert body["displayName"] == "Node spawns a shell"
    assert body["isEnabled"] is True
    assert body["queryCondition"] == {"queryText": _QUERY}
    assert body["schedule"] == {"period": "24H"}
    template = body["detectionAction"]["alertTemplate"]
    assert template["title"] == "Test rule" and template["severity"] == "low"
    assert template["mitreTechniques"] == ["T1059"]
    # Graph removed alertTemplate.impactedAssets on 2026-10-01: the mapping
    # travels as entityMappings, else the service drops it silently.
    assert "impactedAssets" not in template
    assert template["entityMappings"] == {"hosts": [{"deviceIdColumn": "DeviceId"}]}
    assert body["detectionAction"]["responseActions"] == []


@pytest.mark.asyncio
async def test_create_maps_each_asset_kind_and_field(monkeypatch):
    graph = _Graph((201, {"id": "73"}))
    executor = _executor(monkeypatch, graph)

    await _call(
        executor,
        "create_defender_detection_rule",
        display_name="x",
        query=_QUERY,
        alert_title="x",
        impacted_assets=[
            {"type": "device", "identifier": "DeviceName", "field": "nameColumn"},
            {"type": "user", "identifier": "AccountObjectId"},
            {"type": "mailbox", "identifier": "RecipientEmailAddress"},
        ],
    )

    template = graph.calls[0]["json"]["detectionAction"]["alertTemplate"]
    assert template["entityMappings"] == {
        "hosts": [{"nameColumn": "DeviceName"}],
        "accounts": [{"aadUserIdColumn": "AccountObjectId"}],
        "mailboxes": [{"primaryAddressColumn": "RecipientEmailAddress"}],
    }


@pytest.mark.asyncio
async def test_create_without_assets_sends_no_mapping_and_refuses_a_wrong_field(monkeypatch):
    graph = _Graph((201, {"id": "74"}))
    executor = _executor(monkeypatch, graph)

    await _call(executor, "create_defender_detection_rule", display_name="x", query=_QUERY, alert_title="x")
    refused = await _call(
        executor,
        "create_defender_detection_rule",
        display_name="x",
        query=_QUERY,
        alert_title="x",
        impacted_assets=[{"type": "mailbox", "identifier": "X", "field": "deviceIdColumn"}],
    )

    assert "entityMappings" not in graph.calls[0]["json"]["detectionAction"]["alertTemplate"]
    assert "primaryAddressColumn" in refused and len(graph.calls) == 1


@pytest.mark.asyncio
async def test_get_shows_the_entity_mappings_and_legacy_assets(monkeypatch):
    legacy = {
        **_STORED_RULE,
        "detectionAction": {
            "alertTemplate": {
                "title": "Old",
                "impactedAssets": [
                    {"@odata.type": "#microsoft.graph.security.impactedDeviceAsset", "identifier": "deviceId"}
                ],
            }
        },
    }
    graph = _Graph((200, _STORED_RULE), (200, legacy))
    executor = _executor(monkeypatch, graph)

    got = await _call(executor, "get_defender_detection_rule", rule_id="72")
    old = await _call(executor, "get_defender_detection_rule", rule_id="71")

    assert "Impacted assets: hosts(deviceIdColumn=DeviceId)" in got
    assert "Impacted assets: impactedDeviceAsset=deviceId" in old


@pytest.mark.asyncio
async def test_update_sends_entity_mappings_and_drops_the_removed_property(monkeypatch):
    legacy_template = {
        **_STORED_RULE["detectionAction"]["alertTemplate"],
        "impactedAssets": [{"@odata.type": "#microsoft.graph.security.impactedDeviceAsset", "identifier": "x"}],
    }
    stored = {**_STORED_RULE, "detectionAction": {"alertTemplate": legacy_template, "responseActions": []}}
    graph = _Graph((200, stored), (200, {}))
    executor = _executor(monkeypatch, graph)

    await _call(
        executor,
        "update_defender_detection_rule",
        rule_id="72",
        impacted_assets=[{"type": "user", "identifier": "AccountUpn", "field": "upnColumn"}],
    )

    template = graph.calls[1]["json"]["detectionAction"]["alertTemplate"]
    assert template["entityMappings"] == {"accounts": [{"upnColumn": "AccountUpn"}]}
    assert "impactedAssets" not in template


@pytest.mark.asyncio
async def test_create_refuses_an_unknown_period_before_any_call(monkeypatch):
    graph = _Graph()
    executor = _executor(monkeypatch, graph)
    answer = await _call(
        executor, "create_defender_detection_rule", display_name="x", query=_QUERY, alert_title="x", period="6H"
    )
    assert "period must be one of" in answer
    assert graph.calls == []


@pytest.mark.asyncio
async def test_get_and_list_show_the_stored_fields(monkeypatch):
    graph = _Graph((200, _STORED_RULE), (200, {"value": [_STORED_RULE]}))
    executor = _executor(monkeypatch, graph)

    got = await _call(executor, "get_defender_detection_rule", rule_id="72")
    listed = await _call(executor, "list_defender_detection_rules", odata_filter="isEnabled eq true")

    assert graph.calls[0]["url"] == f"{_RULES}/72"
    for text in ("Node spawns a shell", "id: 72", "Period: 24H", "Severity: low", "T1059", "DeviceId", _QUERY):
        assert text in got
    assert graph.calls[1]["params"] == {"$top": 50, "$filter": "isEnabled eq true"}
    assert "Node spawns a shell (id: 72)" in listed


@pytest.mark.asyncio
async def test_update_patches_only_the_given_alert_fields(monkeypatch):
    graph = _Graph((200, _STORED_RULE), (200, {}))
    executor = _executor(monkeypatch, graph)

    answer = await _call(
        executor, "update_defender_detection_rule", rule_id="72", alert_title="Renamed", severity="medium"
    )

    assert "updated" in answer and "severity" in answer and "title" in answer
    read, patch = graph.calls
    assert read["method"] == "get" and patch["method"] == "patch"
    assert patch["url"] == f"{_RULES}/72"
    assert set(patch["json"]) == {"detectionAction"}
    template = patch["json"]["detectionAction"]["alertTemplate"]
    assert template["title"] == "Renamed" and template["severity"] == "medium"
    # The fields not given are sent back as stored.
    assert template["category"] == "Execution" and template["mitreTechniques"] == ["T1059"]


@pytest.mark.asyncio
async def test_update_of_a_top_level_field_reads_nothing(monkeypatch):
    graph = _Graph((200, {}))
    executor = _executor(monkeypatch, graph)
    await _call(executor, "update_defender_detection_rule", rule_id="72", enabled=False)
    assert len(graph.calls) == 1
    assert graph.calls[0]["json"] == {"isEnabled": False}


@pytest.mark.asyncio
async def test_delete_then_get_says_not_found(monkeypatch):
    graph = _Graph((204, {}), (404, {}))
    executor = _executor(monkeypatch, graph)

    deleted = await _call(executor, "delete_defender_detection_rule", rule_id="72")
    got = await _call(executor, "get_defender_detection_rule", rule_id="72")

    assert deleted == "Custom detection rule 72 deleted."
    assert graph.calls[0]["method"] == "delete"
    assert "404" in got


@pytest.mark.asyncio
async def test_a_403_names_the_permission_to_grant(monkeypatch):
    graph = _Graph((403, {}), (403, {}))
    executor = _executor(monkeypatch, graph)

    write = await _call(
        executor, "create_defender_detection_rule", display_name="x", query=_QUERY, alert_title="x"
    )
    read = await _call(executor, "list_defender_detection_rules")

    assert "CustomDetection.ReadWrite.All" in write
    assert "CustomDetection.Read.All" in read
    assert "admin consent" in write


@pytest.mark.asyncio
async def test_update_replaces_the_mitre_techniques_whole(monkeypatch):
    graph = _Graph((200, _STORED_RULE), (200, {}), (200, _STORED_RULE), (200, {}))
    executor = _executor(monkeypatch, graph)

    await _call(executor, "update_defender_detection_rule", rule_id="72", mitre_techniques=["T1105", "T1027"])
    await _call(executor, "update_defender_detection_rule", rule_id="72", mitre_techniques=[])

    assert graph.calls[1]["json"]["detectionAction"]["alertTemplate"]["mitreTechniques"] == ["T1105", "T1027"]
    assert graph.calls[3]["json"]["detectionAction"]["alertTemplate"]["mitreTechniques"] == []


class _TimedOutGraph(_Graph):
    """The write answers a timeout; the reads answer from the queue."""

    async def __call__(self, _client, method, url, provider, action, **kwargs):
        if method in ("post", "patch"):
            self.calls.append({"method": method, "url": url, "action": action, **kwargs})
            return (
                None,
                f"Error: {provider} request timed out while trying to {action}. The service may be slow or overloaded.",
            )
        return await super().__call__(_client, method, url, provider, action, **kwargs)


@pytest.mark.asyncio
async def test_a_create_that_times_out_finds_the_rule_instead_of_a_duplicate(monkeypatch):
    graph = _TimedOutGraph((200, {"value": [_STORED_RULE]}))
    executor = _executor(monkeypatch, graph)

    answer = await _call(
        executor, "create_defender_detection_rule", display_name="Node spawns a shell", query=_QUERY, alert_title="x"
    )

    assert "id: 72" in answer and "do not create it again" in answer
    assert graph.calls[1]["params"] == {"$filter": "displayName eq 'Node spawns a shell'"}


@pytest.mark.asyncio
async def test_a_create_that_times_out_and_left_nothing_says_a_retry_is_safe(monkeypatch):
    graph = _TimedOutGraph((200, {"value": []}))
    executor = _executor(monkeypatch, graph)
    answer = await _call(executor, "create_defender_detection_rule", display_name="x", query=_QUERY, alert_title="x")
    assert "timed out" in answer and "safe to create it again" in answer


@pytest.mark.asyncio
async def test_an_incident_update_that_times_out_is_read_back(monkeypatch):
    graph = _TimedOutGraph((200, {"id": "9", "status": "resolved"}), (200, {"id": "9", "status": "active"}))
    executor = _executor(monkeypatch, graph)

    applied = await _call(executor, "update_defender_incident", incident_id="9", status="resolved")
    not_applied = await _call(executor, "update_defender_incident", incident_id="9", status="resolved")

    assert "updated" in applied and "do not retry" in applied
    assert "does not show status" in not_applied and "safe to retry" in not_applied


@pytest.mark.asyncio
async def test_a_comment_that_times_out_is_not_posted_twice(monkeypatch):
    stored = {"id": "9", "comments": [{"comment": "Closed as benign"}]}
    graph = _TimedOutGraph((200, stored), (200, {"id": "9", "comments": []}), (500, {}))
    executor = _executor(monkeypatch, graph)

    there = await _call(executor, "add_defender_incident_comment", incident_id="9", comment="Closed as benign")
    absent = await _call(executor, "add_defender_alert_comment", alert_id="a1", comment="Closed as benign")
    unknown = await _call(executor, "add_defender_incident_comment", incident_id="9", comment="Closed as benign")

    assert "do not post it again" in there
    assert "safe to post it again" in absent
    assert "Outcome unknown" in unknown


def test_the_catalog_lists_the_permission():
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    scopes = [p["scope"] for p in PROVIDER_CATALOG["defender"]["setup_guide"]["permissions"]]
    assert "CustomDetection.ReadWrite.All (Graph)" in scopes
