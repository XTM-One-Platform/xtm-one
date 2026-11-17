"""Tests for the Google SecOps (Chronicle) integration provider.

Covers region/base-URL resolution (including the per-endpoint API version),
the service-account token flow (mocked), UDM search param/format, the v1beta
case collection and its write actions, alert feedback updates, UDM event
extraction from alerts, the streaming alerts view, entity lookup, rule create +
deployment toggle, reference-list create + merge-on-add, retrohunt creation,
connectivity validation, and tool introspection. Both the token exchange
(``_request_access_token``) and the shared ``safe_request`` are monkeypatched so
no network is touched.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.providers import google_secops as gs
from app.integrations.providers.google_secops import (
    build_tools,
    resolve_base_url,
    validate_connection,
)
from app.tools.base import CallableToolExecutor, ToolCall

_SA = '{"client_email": "svc@proj.iam.gserviceaccount.com", "private_key": "KEY"}'
_TOKEN = f"us||my-project||cust-123||{_SA}"


class _StubResponse:
    """Stand-in for ``httpx.Response``.

    A router may return a raw ``str`` to exercise the streaming path (which
    reads ``.text``); anything else is served through ``.json()``.
    """

    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        return self._payload

    @property
    def text(self) -> str:
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload)


class _Recorder:
    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        return _StubResponse(self._router(method, url)), None


async def _fake_token(_sa_json):
    return "access-token-xyz", 3600


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(gs, "safe_request", recorder)
    monkeypatch.setattr(gs, "_request_access_token", _fake_token)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Pure helpers + introspection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expected", "observed", "prefix", "matches"),
    [
        # The platform may answer a score as a number or as a string.
        (5, 5, "", True),
        (5, "5", "", True),
        (5, "5.0", "", True),
        (5, 5.0, "", True),
        (5, 80, "", False),
        (5, "low", "", False),
        # Enum values travel with or without their family prefix.
        ("PRIORITY_LOW", "PRIORITY_LOW", "PRIORITY_", True),
        ("REVIEWED", "STATUS_REVIEWED", "STATUS_", True),
        ("REVIEWED", "STATUS_CLOSED", "STATUS_", False),
        ("PRIORITY_LOW", "LOW", "PRIORITY_", True),
        ("PRIORITY_LOW", "PRIORITY_HIGH", "PRIORITY_", False),
        # The collision a generic "strip the last segment" rule would create:
        # these two are opposite verdicts and must never compare equal.
        ("VERDICT_TRUE_POSITIVE", "VERDICT_FALSE_POSITIVE", "VERDICT_", False),
        ("TRUE_POSITIVE", "FALSE_POSITIVE", "VERDICT_", False),
        # Free text is compared as written, modulo surrounding whitespace.
        ("Confirmed by sandbox", " Confirmed by sandbox ", "", True),
        ("Confirmed by sandbox", "Something else", "", False),
        (None, "", "", True),
    ],
)
def test_feedback_matches(expected, observed, prefix, matches):
    assert gs._feedback_matches(expected, observed, prefix) is matches


def test_resolve_base_url():
    assert resolve_base_url("us") == "https://us-chronicle.googleapis.com/v1alpha"
    assert resolve_base_url("EUROPE") == "https://europe-chronicle.googleapis.com/v1alpha"
    assert (
        resolve_base_url("https://us-chronicle.googleapis.com/v1alpha")
        == "https://us-chronicle.googleapis.com/v1alpha"
    )
    assert resolve_base_url("") == "https://us-chronicle.googleapis.com/v1alpha"


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    names = {t.name for t in tools}
    assert "run_secops_udm_search" in names
    assert "translate_secops_udm_query" in names
    assert "validate_secops_udm_query" in names
    assert "list_secops_rules" in names
    assert "create_secops_reference_list" in names
    assert len(tools) >= 15


def test_resolve_location_normalizes_region():
    assert gs.resolve_location("us") == "us"
    assert gs.resolve_location("EUROPE") == "europe"
    assert gs.resolve_location("europe-west2") == "europe-west2"
    assert gs.resolve_location("") == "us"
    # A full base URL → location derived from the {location}-chronicle host label.
    assert (
        gs.resolve_location("https://europe-chronicle.googleapis.com/v1alpha")
        == "europe"
    )


@pytest.mark.asyncio
async def test_instance_path_uses_normalized_lowercase_location(monkeypatch):
    """An uppercase region must not leak a wrong-case location into the resource
    path: the base URL is lowercased, so the instance path must be too."""
    recorder = _Recorder(lambda m, u: {"events": []})
    monkeypatch.setattr(gs, "safe_request", recorder)
    monkeypatch.setattr(gs, "_request_access_token", _fake_token)
    executor = CallableToolExecutor()
    build_tools(token=f"EUROPE||my-project||cust-123||{_SA}", executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="loc", name="run_secops_udm_search", arguments={"query": "x"})
    )
    assert not result.is_error
    assert "/locations/europe/instances/" in recorder.calls[0]["url"]
    assert "EUROPE" not in recorder.calls[0]["url"]


# ---------------------------------------------------------------------------
# Hunting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_udm_search_builds_params_and_formats(monkeypatch):
    def router(method, url):
        return {
            "events": [
                {
                    "udm": {
                        "metadata": {
                            "eventTimestamp": "2024-05-01T10:00:00Z",
                            "eventType": "PROCESS_LAUNCH",
                        },
                        "principal": {"hostname": "WIN-01"},
                        "target": {"hostname": "evil.example.com"},
                    }
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="run_secops_udm_search",
            arguments={"query": 'metadata.event_type = "PROCESS_LAUNCH"', "hours": 12},
        )
    )
    assert not result.is_error
    assert "PROCESS_LAUNCH" in result.content
    assert "WIN-01" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith(
        "/projects/my-project/locations/us/instances/cust-123:udmSearch"
    )
    assert call["params"]["query"] == 'metadata.event_type = "PROCESS_LAUNCH"'
    assert "timeRange.startTime" in call["params"]
    assert "timeRange.endTime" in call["params"]


@pytest.mark.asyncio
async def test_udm_search_requires_query(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c2", name="run_secops_udm_search", arguments={"query": " "})
    )
    assert "query string is required" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Hunting — IoC matches + UDM query authoring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_iocs_uses_timestamp_range_and_mandiant_attributes(monkeypatch):
    """legacySearchEnterpriseWideIoCs binds the window to timestampRange and
    requires maxMatchesToReturn + addMandiantAttributes — never pageSize or a
    nested timeRange (both are rejected as unknown query parameters)."""
    def router(method, url):
        return {
            "matches": [
                {
                    "id": "evil.example.com",
                    "categories": ["Malware"],
                    "feedName": "Mandiant",
                    "firstSeenTimestamp": "2024-05-01T10:00:00Z",
                    "lastSeenTimestamp": "2024-05-02T10:00:00Z",
                }
            ],
            "moreDataAvailable": True,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="ioc", name="list_secops_iocs", arguments={"count": 10})
    )
    assert not result.is_error
    assert "evil.example.com" in result.content
    assert "Mandiant" in result.content
    # Truncation flag surfaced so the agent narrows the window.
    assert "Truncated" in result.content
    params = recorder.calls[0]["params"]
    assert params["maxMatchesToReturn"] == 10
    assert "addMandiantAttributes" in params
    assert "timestampRange.startTime" in params
    assert "timestampRange.endTime" in params
    assert "pageSize" not in params
    assert "timeRange.startTime" not in params


@pytest.mark.asyncio
async def test_translate_udm_query_posts_text(monkeypatch):
    def router(method, url):
        return {
            "query": 'metadata.event_type = "PROCESS_LAUNCH"',
            "timeRange": {
                "startTime": "2024-05-01T00:00:00Z",
                "endTime": "2024-05-02T00:00:00Z",
            },
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="tr",
            name="translate_secops_udm_query",
            arguments={"text": "process launches in the last day"},
        )
    )
    assert not result.is_error
    assert "PROCESS_LAUNCH" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith(":translateUdmQuery")
    assert call["json"] == {"text": "process launches in the last day"}


@pytest.mark.asyncio
async def test_validate_udm_query_reports_errors(monkeypatch):
    def router(method, url):
        return {
            "errorText": "unexpected token",
            "errorType": "UNCLOSED_BRACKET_TYPE",
            "errorPosition": {"startLine": 1, "startColumn": 12},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="vq",
            name="validate_secops_udm_query",
            arguments={"query": "metadata.event_type = ("},
        )
    )
    assert not result.is_error
    assert "Invalid UDM query" in result.content
    assert "unexpected token" in result.content
    call = recorder.calls[-1]
    assert call["url"].endswith(":validateQuery")
    assert call["params"]["rawQuery"] == "metadata.event_type = ("


@pytest.mark.asyncio
async def test_validate_udm_query_accepts_valid_query(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="vq2",
            name="validate_secops_udm_query",
            arguments={"query": 'metadata.event_type = "PROCESS_LAUNCH"'},
        )
    )
    assert not result.is_error
    assert "Valid" in result.content


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_rule_alerts_parses_rule_metadata_and_flags_truncation(monkeypatch):
    """legacySearchRulesAlerts groups alerts per rule under ruleMetadata + a
    list of detections; the formatter must read the rule name/id from there
    (not non-existent top-level keys), use maxNumAlertsToReturn (not pageSize),
    and surface the tooManyAlerts truncation flag."""
    def router(method, url):
        return {
            "ruleAlerts": [
                {
                    "ruleMetadata": {
                        "ruleId": "ru_1",
                        "properties": {
                            "name": "Suspicious PowerShell",
                            "liveRuleStatus": "ENABLED",
                        },
                    },
                    "alerts": [
                        {"detectionTimestamp": "2024-05-01T10:00:00Z"},
                        {"detectionTimestamp": "2024-05-01T11:00:00Z"},
                    ],
                }
            ],
            "tooManyAlerts": True,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="ra", name="list_secops_rule_alerts", arguments={"count": 5})
    )
    assert not result.is_error
    assert "Suspicious PowerShell" in result.content
    assert "ru_1" in result.content
    assert "Alerts: 2" in result.content
    assert "2024-05-01T10:00:00" in result.content
    assert "Truncated" in result.content
    params = recorder.calls[0]["params"]
    assert params["maxNumAlertsToReturn"] == 5
    assert "pageSize" not in params


@pytest.mark.asyncio
async def test_list_rule_alerts_allows_more_than_100_up_to_1000(monkeypatch):
    """legacySearchRulesAlerts has no pagination but allows far more than 100,
    and the output is grouped one line per rule - so the tool lifts the old 100
    cap (which silently blocked the "increase count" truncation hint) and bounds
    maxNumAlertsToReturn at 1000 to keep the agent context bounded."""
    executor, recorder = _build(monkeypatch, lambda m, u: {"ruleAlerts": []})
    # A count above the old 100 cap is now honored verbatim...
    await executor.execute_tool(
        ToolCall(id="ra500", name="list_secops_rule_alerts", arguments={"count": 500})
    )
    assert recorder.calls[-1]["params"]["maxNumAlertsToReturn"] == 500
    # ...and the 1000 ceiling still bounds an unreasonably large request.
    await executor.execute_tool(
        ToolCall(
            id="ra5000", name="list_secops_rule_alerts", arguments={"count": 5000}
        )
    )
    assert recorder.calls[-1]["params"]["maxNumAlertsToReturn"] == 1000


@pytest.mark.asyncio
async def test_get_alert_unwraps_collection(monkeypatch):
    """legacyGetAlert returns {"alert": Collection}; the rule details live in
    the Collection's detection[] (SecurityResult) list, not at the top level."""
    def router(method, url):
        return {
            "alert": {
                "id": "det_123",
                "type": "RULE_DETECTION",
                "detectionTime": "2024-05-01T10:00:00Z",
                "timeWindow": {
                    "startTime": "2024-05-01T09:00:00Z",
                    "endTime": "2024-05-01T10:00:00Z",
                },
                "detection": [
                    {
                        "ruleName": "Suspicious PowerShell",
                        "ruleId": "ru_1",
                        "severity": "HIGH",
                        "alertState": "ALERTING",
                        "description": "Encoded command observed",
                    }
                ],
            }
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="ga", name="get_secops_alert", arguments={"alert_id": "det_123"})
    )
    assert not result.is_error
    assert "Suspicious PowerShell" in result.content
    assert "HIGH" in result.content
    assert "ALERTING" in result.content
    assert "Encoded command observed" in result.content
    call = recorder.calls[-1]
    assert call["params"]["alertId"] == "det_123"
    assert call["params"]["includeDetections"] is True


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_rule_body(monkeypatch):
    def router(method, url):
        return {"name": "projects/p/locations/us/instances/c/rules/ru_1", "revisionId": "v1"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="create_secops_rule",
            arguments={"rule_text": "rule x { condition: $e }"},
        )
    )
    assert not result.is_error
    assert "ru_1" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/rules")
    assert call["json"] == {"text": "rule x { condition: $e }"}


@pytest.mark.asyncio
async def test_toggle_rule_deployment(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="toggle_secops_rule",
            arguments={"rule_id": "ru_1", "enable": False},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "patch"
    assert call["url"].endswith("/rules/ru_1/deployment")
    assert call["params"]["updateMask"] == "enabled"
    assert call["json"] == {"enabled": False}


@pytest.mark.asyncio
async def test_list_rules_reads_top_level_author_and_severity(monkeypatch):
    """rules.list BASIC view exposes displayName/author/severity at the top
    level; the formatter must not look for author under the FULL-only
    metadata map."""
    def router(method, url):
        return {
            "rules": [
                {
                    "name": "projects/p/locations/us/instances/c/rules/ru_1",
                    "displayName": "Suspicious PowerShell",
                    "author": "soc-team",
                    "type": "MULTI_EVENT",
                    "severity": {"displayName": "HIGH"},
                }
            ]
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="lr", name="list_secops_rules", arguments={})
    )
    assert not result.is_error
    assert "Suspicious PowerShell" in result.content
    assert "soc-team" in result.content
    assert "HIGH" in result.content


@pytest.mark.asyncio
async def test_list_rule_detections_uses_top_level_start_end(monkeypatch):
    """legacySearchDetections takes top-level startTime/endTime query params
    (not a nested timeRange); pageSize is valid for this endpoint."""
    def router(method, url):
        return {"detections": []}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="det",
            name="list_secops_rule_detections",
            arguments={"rule_id": "ru_1", "count": 10},
        )
    )
    assert not result.is_error
    params = recorder.calls[0]["params"]
    assert params["ruleId"] == "ru_1"
    assert params["pageSize"] == 10
    assert "startTime" in params
    assert "endTime" in params
    assert "timeRange.startTime" not in params
    # listBasis must pin the [startTime, endTime) window to the detection
    # timestamp; a regression that drops it would silently apply the window to
    # the wrong basis (e.g. creation time) and return the wrong detections.
    assert params["listBasis"] == "DETECTION_TIME"


# ---------------------------------------------------------------------------
# Retrohunts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_retrohunt_body(monkeypatch):
    # The create call returns a long-running Operation (no retrohunt state yet),
    # NOT a retrohunt resource - so the mocked response is shaped like an LRO
    # (an ``operations/...`` name, ``done``/``metadata``, and crucially no
    # ``state``).
    def router(method, url):
        return {
            "name": "projects/p/locations/us/instances/c/operations/op_1",
            "metadata": {"@type": "type.googleapis.com/...RetrohuntMetadata"},
            "done": False,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="create_secops_retrohunt",
            arguments={"rule_id": "ru_1", "hours": 48},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["url"].endswith("/rules/ru_1/retrohunts")
    assert "processInterval" in call["json"]
    assert "startTime" in call["json"]["processInterval"]
    assert "endTime" in call["json"]["processInterval"]
    # Output reflects the LRO operation id and defers state to the list tool; it
    # must NOT invent a retrohunt state from the create response (which has none).
    assert "op_1" in result.content
    assert "list_secops_retrohunts" in result.content
    assert "RUNNING" not in result.content


# ---------------------------------------------------------------------------
# Reference lists (IOC enforcement)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_reference_list_body(monkeypatch):
    def router(method, url):
        return {"name": "projects/p/locations/us/instances/c/referenceLists/bad_ips"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="create_secops_reference_list",
            arguments={
                "name": "bad_ips",
                "entries": "1.2.3.4, 5.6.7.8",
                "syntax_type": "cidr",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["params"]["referenceListId"] == "bad_ips"
    assert call["json"]["entries"] == [{"value": "1.2.3.4"}, {"value": "5.6.7.8"}]
    assert call["json"]["syntaxType"] == "REFERENCE_LIST_SYNTAX_TYPE_CIDR"


@pytest.mark.asyncio
async def test_add_reference_list_entries_merges(monkeypatch):
    def router(method, url):
        if method == "get":
            return {
                "name": "projects/p/locations/us/instances/c/referenceLists/bad_ips",
                "entries": [{"value": "1.2.3.4"}],
            }
        return {}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="add_secops_reference_list_entries",
            arguments={"name": "bad_ips", "entries": "1.2.3.4, 9.9.9.9"},
        )
    )
    assert not result.is_error
    patch_call = next(c for c in recorder.calls if c["method"] == "patch")
    values = [e["value"] for e in patch_call["json"]["entries"]]
    # Existing 1.2.3.4 kept (deduped), 9.9.9.9 appended.
    assert values == ["1.2.3.4", "9.9.9.9"]


# ---------------------------------------------------------------------------
# Connectivity validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_connection_ok(monkeypatch):
    monkeypatch.setattr(gs, "_request_access_token", _fake_token)
    ok, message = await validate_connection("us", "proj", "cust", _SA)
    assert ok is True
    assert "connected" in message.lower()


@pytest.mark.asyncio
async def test_validate_connection_failure(monkeypatch):
    async def _no_token(_sa):
        return None, 0

    monkeypatch.setattr(gs, "_request_access_token", _no_token)
    ok, message = await validate_connection("us", "proj", "cust", _SA)
    assert ok is False
    assert "authentication failed" in message.lower()


@pytest.mark.asyncio
async def test_validate_connection_missing_fields():
    ok, message = await validate_connection("", "", "", "")
    assert ok is False
    assert "missing" in message.lower()


# ---------------------------------------------------------------------------
# Per-endpoint API versioning
# ---------------------------------------------------------------------------


def test_resolve_base_url_is_version_aware():
    assert resolve_base_url("us", "v1beta") == "https://us-chronicle.googleapis.com/v1beta"
    assert resolve_base_url("us", "v1") == "https://us-chronicle.googleapis.com/v1"
    # A full base URL keeps its host but has its version segment replaced, so
    # the same configured URL serves both the v1alpha and v1beta endpoints.
    assert (
        resolve_base_url("https://us-chronicle.googleapis.com/v1alpha", "v1beta")
        == "https://us-chronicle.googleapis.com/v1beta"
    )
    assert (
        resolve_base_url("https://custom.example.com", "v1beta")
        == "https://custom.example.com/v1beta"
    )


def test_resolve_base_url_preserves_the_case_of_a_full_url():
    """A region short name is lowercased; a full base URL is not.

    A custom base URL can point at a proxy with case-sensitive path segments,
    so only the region short names are normalized.
    """
    assert (
        resolve_base_url("https://Proxy.Example.com/Chronicle-API/v1alpha", "v1beta")
        == "https://Proxy.Example.com/Chronicle-API/v1beta"
    )
    assert (
        resolve_base_url("HTTPS://Proxy.Example.com/Path", "v1alpha")
        == "HTTPS://Proxy.Example.com/Path/v1alpha"
    )


def test_case_priority_and_alert_priority_are_different_enums():
    """A case takes CasePriority (bare); alert feedback takes Priority (PRIORITY_*).

    Sending PRIORITY_HIGH to a case is rejected by the API, so each side
    normalizes to its own vocabulary and tolerates the other spelling.
    """
    assert gs._resolve_case_priority("high") == "HIGH"
    assert gs._resolve_case_priority("PRIORITY_HIGH") == "HIGH"
    assert gs._resolve_case_priority("info") == "INFORMATIVE"
    assert gs._resolve_case_priority("PRIORITY_INFO") == "INFORMATIVE"
    assert gs._resolve_case_priority("nope") is None
    assert "PRIORITY_HIGH" not in gs._CASE_PRIORITIES

    assert gs._resolve_enum("high", gs._ALERT_PRIORITIES, "PRIORITY_") == "PRIORITY_HIGH"
    assert gs._resolve_enum("PRIORITY_HIGH", gs._ALERT_PRIORITIES, "PRIORITY_") == "PRIORITY_HIGH"


def test_dt_renders_epoch_millisecond_timestamps():
    """Case.createTime/updateTime are int64 milliseconds, not RFC 3339."""
    assert gs._dt("1714557600000") == "2024-05-01T10:00:00Z"
    assert gs._dt("2024-05-01T10:00:00Z") == "2024-05-01T10:00:00"
    assert gs._dt(None) == ""
    assert gs._dt("") == ""


def test_collect_udm_events_accepts_camel_and_snake_case():
    """The Collection reference renders proto snake_case; the wire is camelCase."""
    event = {"metadata": {"eventType": "PROCESS_LAUNCH"}}
    camel = {"collectionElements": [{"references": [{"event": event}]}]}
    snake = {"collection_elements": [{"references": [{"event": event}]}]}
    assert gs._collect_udm_events(camel) == [event]
    assert gs._collect_udm_events(snake) == [event]


def test_every_tool_definition_is_registered():
    executor = CallableToolExecutor()
    tools = build_tools(token=_TOKEN, executor=executor)
    assert len(tools) == 32
    registered = set(executor._handlers)
    assert {t.name for t in tools} == registered


def test_resource_id_accepts_bare_id_uuid_or_resource_name():
    """Agents copy whichever identifier shape they last saw."""
    assert gs._resource_id("12345") == "12345"
    assert (
        gs._resource_id("projects/p/locations/us/instances/c/cases/12345") == "12345"
    )
    uuid = "3f1a2b4c-5d6e-7f80-9a1b-2c3d4e5f6071"
    assert gs._resource_id(f"projects/p/locations/us/instances/c/cases/{uuid}") == uuid
    assert gs._resource_id("") == ""


# ---------------------------------------------------------------------------
# Cases — v1beta collection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_cases_uses_documented_query_params(monkeypatch):
    """Cases are pinned to v1beta (they exist on v1/v1beta/v1alpha alike)."""

    def router(method, url):
        return {
            "totalSize": 1,
            "cases": [
                {
                    "name": "projects/p/locations/us/instances/c/cases/9001",
                    "displayName": "Emotet on WIN-01",
                    "priority": "HIGH",
                    "stage": "Triage",
                    "alertCount": 3,
                    # Case.createTime is an int64 of MILLISECONDS, not RFC 3339.
                    "createTime": "1714557600000",
                    "environment": "Corporate",
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="lc",
            name="list_secops_cases",
            arguments={"filter_query": "priority='HIGH'", "order_by": "priority desc"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert "/v1beta/" in call["url"]
    assert call["url"].endswith("/instances/cust-123/cases")
    assert call["params"]["filter"] == "priority='HIGH'"
    assert call["params"]["orderBy"] == "priority desc"
    assert call["params"]["expand"] == "tags,products"
    assert "case_id: 9001" in result.content
    # The epoch-ms timestamp is rendered, not printed raw.
    assert "1714557600000" not in result.content
    assert "2024-05-01T10:00:00Z" in result.content


@pytest.mark.asyncio
async def test_list_cases_appends_filter_grammar_on_error(monkeypatch):
    recorder = _Recorder(lambda m, u: {})

    async def _failing(_client, method, url, _provider, action, **kwargs):
        recorder.calls.append({"method": method, "url": url, **kwargs})
        return None, "Error: Google SecOps rejected the request (400)."

    monkeypatch.setattr(gs, "safe_request", _failing)
    monkeypatch.setattr(gs, "_request_access_token", _fake_token)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)

    result = await executor.execute_tool(
        ToolCall(id="lcf", name="list_secops_cases", arguments={"filter_query": "bogus"})
    )
    assert "Case filter syntax" in result.content


@pytest.mark.asyncio
async def test_get_case_accepts_a_full_resource_name(monkeypatch):
    def router(method, url):
        return {
            "name": "projects/p/locations/us/instances/c/cases/9001",
            "displayName": "Emotet on WIN-01",
            "priority": "HIGH",
            "environment": "Corporate",
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="gc",
            name="get_secops_case",
            arguments={"case_id": "projects/p/locations/us/instances/c/cases/9001"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert "/v1beta/" in call["url"]
    assert call["url"].endswith("/cases/9001")
    assert "case_id: 9001" in result.content
    assert "Environment: Corporate" in result.content


@pytest.mark.asyncio
async def test_update_case_builds_update_mask_and_normalizes_priority(monkeypatch):
    def router(method, url):
        return {
            "name": "projects/p/cases/9001",
            "priority": "CRITICAL",
            "stage": "Investigation",
            "status": "OPENED",
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="uc",
            name="update_secops_case",
            # A case takes CasePriority (bare names), NOT the alert-side
            # PRIORITY_* enum — and the prefixed spelling is accepted and
            # stripped so an agent guessing either way still succeeds.
            arguments={"case_id": "9001", "priority": "PRIORITY_critical", "stage": "Investigation"},
        )
    )
    assert not result.is_error
    assert [c["method"] for c in recorder.calls] == ["patch", "get"]
    call = recorder.calls[0]
    assert call["method"] == "patch"
    assert "/v1beta/" in call["url"]
    assert call["json"] == {"priority": "CRITICAL", "stage": "Investigation"}
    assert call["params"]["updateMask"] == "priority,stage"


@pytest.mark.asyncio
async def test_update_case_rejects_unknown_priority_without_calling_the_api(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="ucb",
            name="update_secops_case",
            arguments={"case_id": "9001", "priority": "URGENT"},
        )
    )
    assert "CRITICAL" in result.content
    assert "PRIORITY_CRITICAL" not in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_case_requires_at_least_one_field(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="uce", name="update_secops_case", arguments={"case_id": "9001"})
    )
    assert "nothing to update" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_bulk_case_operations_send_numeric_ids(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})

    await executor.execute_tool(
        ToolCall(
            id="bp",
            name="bulk_change_secops_case_priority",
            arguments={"case_ids": "9001, 9002", "priority": "high"},
        )
    )
    call = recorder.calls[-1]
    assert call["url"].endswith("/cases:executeBulkChangePriority")
    assert "/v1beta/" in call["url"]
    assert call["json"] == {"casesIds": [9001, 9002], "priority": "HIGH"}

    await executor.execute_tool(
        ToolCall(
            id="ba",
            name="bulk_assign_secops_cases",
            arguments={"case_ids": "9001", "username": "@SecurityTeam"},
        )
    )
    assert recorder.calls[-1]["json"] == {
        "casesIds": [9001],
        "userName": "@SecurityTeam",
    }

    await executor.execute_tool(
        ToolCall(
            id="bc",
            name="close_secops_cases",
            arguments={
                "case_ids": "9001",
                "close_reason": "not_malicious",
                "close_comment": "Benign admin activity",
            },
        )
    )
    assert recorder.calls[-1]["json"] == {
        "casesIds": [9001],
        "closeReason": "NOT_MALICIOUS",
        "closeComment": "Benign admin activity",
    }

    await executor.execute_tool(
        ToolCall(
            id="br",
            name="reopen_secops_cases",
            arguments={"case_ids": "9001", "reopen_comment": "New evidence"},
        )
    )
    assert recorder.calls[-1]["json"] == {
        "casesIds": [9001],
        "reopenComment": "New evidence",
    }


# ---------------------------------------------------------------------------
# Alert feedback — the blocked playbook use case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_alert_posts_feedback_and_normalizes_enums(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="ua",
            name="update_secops_alert",
            arguments={
                "alert_id": "det_123",
                "severity": 80,
                "priority": "high",
                "status": "reviewed",
                "verdict": "true_positive",
                "comment": "Confirmed by sandbox detonation",
            },
        )
    )
    assert not result.is_error
    call = _post_call(recorder)
    assert "/v1alpha/" in call["url"]
    assert call["json"] == {
        "alertId": "det_123",
        "feedback": {
            "severity": 80,
            "priority": "PRIORITY_HIGH",
            "status": "REVIEWED",
            "verdict": "TRUE_POSITIVE",
            "comment": "Confirmed by sandbox detonation",
        },
    }


def _post_call(recorder) -> dict[str, Any]:
    """The legacyUpdateAlert POST, whichever calls follow it."""
    posts = [
        c
        for c in recorder.calls
        if c["method"] == "post" and c["url"].endswith("/legacy:legacyUpdateAlert")
    ]
    assert posts, f"no update call recorded: {[c['url'] for c in recorder.calls]}"
    return posts[-1]


def _readback_router(feedback: dict[str, Any], *, case_name: str = "", soar: Any = None):
    """Router answering the write, the confirmation read, and the SOAR view."""

    def _route(_method: str, url: str) -> Any:
        if url.endswith("/legacy:legacyUpdateAlert"):
            return {}
        if url.endswith("/legacy:legacyGetAlert"):
            alert: dict[str, Any] = {"id": "det_123", "feedbackSummary": feedback}
            if case_name:
                alert["caseName"] = case_name
            return {"alert": alert}
        if url.endswith("/caseAlerts"):
            return {"caseAlerts": soar or []}
        return {}

    return _route


@pytest.mark.asyncio
async def test_update_alert_confirms_the_write_against_the_platform(monkeypatch):
    """A 200 on the write is not evidence; the alert read-back is."""
    executor, recorder = _build(
        monkeypatch,
        _readback_router({"priority": "PRIORITY_LOW", "status": "REVIEWED"}),
    )
    result = await executor.execute_tool(
        ToolCall(
            id="uc",
            name="update_secops_alert",
            arguments={"alert_id": "det_123", "priority": "low", "status": "reviewed"},
        )
    )
    assert not result.is_error
    assert "confirmed on the platform" in result.content
    assert "priority=PRIORITY_LOW" in result.content
    # The confirmation is an independent GET, not a read of the write's own echo.
    assert any(
        c["method"] == "get" and c["url"].endswith("/legacy:legacyGetAlert")
        for c in recorder.calls
    )


@pytest.mark.asyncio
async def test_update_alert_reports_a_write_the_platform_did_not_take(monkeypatch):
    """The reported defect: the call succeeds, the platform keeps the old value.

    This must not read as a success — an agent that believes it writes an audit
    note claiming a change the analyst will never find.
    """
    executor, _ = _build(
        monkeypatch, _readback_router({"priority": "PRIORITY_HIGH"})
    )
    result = await executor.execute_tool(
        ToolCall(
            id="un",
            name="update_secops_alert",
            arguments={"alert_id": "det_123", "priority": "low"},
        )
    )
    assert result.is_error
    assert result.content.startswith("Error:")
    assert "sent 'PRIORITY_LOW'" in result.content
    assert "PRIORITY_HIGH" in result.content


@pytest.mark.asyncio
async def test_update_alert_accepts_a_prefixed_status_echo(monkeypatch):
    """``STATUS_REVIEWED`` answers a written ``REVIEWED`` — not a refusal."""
    executor, _ = _build(monkeypatch, _readback_router({"status": "STATUS_REVIEWED"}))
    result = await executor.execute_tool(
        ToolCall(
            id="ustatus",
            name="update_secops_alert",
            arguments={"alert_id": "det_123", "status": "reviewed"},
        )
    )
    assert not result.is_error
    assert "confirmed on the platform" in result.content


@pytest.mark.asyncio
async def test_update_alert_confirms_free_text_and_scores_that_echo_back(monkeypatch):
    executor, _ = _build(
        monkeypatch,
        _readback_router({"comment": "Sandbox detonation", "riskScore": 5}),
    )
    result = await executor.execute_tool(
        ToolCall(
            id="ufree",
            name="update_secops_alert",
            arguments={
                "alert_id": "det_123",
                "comment": "Sandbox detonation",
                "risk_score": 5,
            },
        )
    )
    assert not result.is_error
    assert "confirmed on the platform" in result.content


@pytest.mark.asyncio
async def test_update_alert_does_not_call_a_normalized_comment_a_refusal(monkeypatch):
    """Free text and scores are observed, never contradicted.

    An ``Error:`` here would feed the per-tool error counters and the
    consecutive-error brake, so a playbook writing a comment on every alert
    would brake itself out of the tool the day SecOps starts trimming or
    timestamping what it stores.
    """
    executor, _ = _build(
        monkeypatch,
        _readback_router(
            {"comment": "[auto] Sandbox detonation ", "riskScore": 6}
        ),
    )
    result = await executor.execute_tool(
        ToolCall(
            id="unorm",
            name="update_secops_alert",
            arguments={
                "alert_id": "det_123",
                "comment": "Sandbox detonation",
                "risk_score": 5,
            },
        )
    )
    assert not result.is_error
    assert not result.content.startswith("Error:")
    assert "Reported differently" in result.content
    assert "not evidence the write was refused" in result.content


@pytest.mark.asyncio
async def test_update_alert_separates_a_refusal_from_a_confirmed_field(monkeypatch):
    """One field refused, one confirmed: the error leads, the confirmation stays."""
    executor, _ = _build(
        monkeypatch,
        _readback_router(
            {"priority": "PRIORITY_HIGH", "verdict": "VERDICT_TRUE_POSITIVE"}
        ),
    )
    result = await executor.execute_tool(
        ToolCall(
            id="umixed",
            name="update_secops_alert",
            arguments={
                "alert_id": "det_123",
                "priority": "low",
                "verdict": "true_positive",
            },
        )
    )
    assert result.is_error
    assert result.content.startswith("Error:")
    assert "changed again between the write and this read" in result.content
    # Sent as the resolved enum; the platform echoed it prefixed, and the
    # prefix-aware comparison still confirmed it.
    assert "Confirmed: verdict=TRUE_POSITIVE." in result.content


@pytest.mark.asyncio
async def test_update_alert_says_the_soar_row_was_out_of_reach(monkeypatch):
    """A noisy case must not look like an alert with no SOAR row.

    ``caseAlerts.list`` cannot be filtered on ``siemAlertId``, so the row is
    found by scanning; returning "" when the scan does not reach it would go
    quiet on exactly the cases where SIEM and SOAR disagree most.
    """
    crowd = [{"siemAlertId": f"other-{i}", "priority": "PriorityLow"} for i in range(200)]
    executor, _ = _build(
        monkeypatch,
        _readback_router(
            {"priority": "PRIORITY_LOW"},
            case_name="projects/p/locations/us/instances/c/cases/9001",
            soar=crowd,
        ),
    )
    result = await executor.execute_tool(
        ToolCall(
            id="ucrowd",
            name="update_secops_alert",
            arguments={"alert_id": "det_123", "priority": "low"},
        )
    )
    assert "not found in the first 200 case alerts" in result.content
    assert "list_secops_case_alerts" in result.content


@pytest.mark.asyncio
async def test_update_case_does_not_report_state_it_did_not_get(monkeypatch):
    """A failed read-back confirms nothing - and must not print N/A as state."""
    executor, _ = _build(monkeypatch, lambda _m, _u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="ucase",
            name="update_secops_case",
            arguments={"case_id": "9001", "priority": "high"},
        )
    )
    # Unconfirmed is not a failure: the write was accepted and may have landed.
    assert not result.is_error
    assert "N/A" not in result.content
    assert "persistence is NOT confirmed" in result.content
    assert "get_secops_case(case_id='9001')" in result.content


def _case_router(read_back: dict, patch_answer: dict | None = None):
    """PATCH answers *patch_answer*; the independent GET answers *read_back*."""

    def router(method, url):
        if method == "patch":
            return patch_answer or {}
        return {"name": "projects/p/cases/9001", **read_back}

    return router


async def _update_case(monkeypatch, read_back, patch_answer=None, **arguments):
    executor, recorder = _build(monkeypatch, _case_router(read_back, patch_answer))
    result = await executor.execute_tool(
        ToolCall(
            id="ucase2",
            name="update_secops_case",
            arguments={"case_id": "9001", **arguments},
        )
    )
    return result, recorder


@pytest.mark.asyncio
async def test_update_case_confirms_fields_from_an_independent_read(monkeypatch):
    # The platform spells the case priority ``PriorityHigh`` for a written HIGH.
    result, recorder = await _update_case(
        monkeypatch,
        {"priority": "PriorityHigh", "stage": "Triage", "status": "Opened"},
        priority="high",
        stage="triage",
    )
    assert not result.is_error
    assert [c["method"] for c in recorder.calls] == ["patch", "get"]
    assert recorder.calls[1]["url"].endswith("/cases/9001")
    assert "Case 9001 updated and confirmed on the platform (priority=HIGH, stage=triage)." in result.content
    assert "Now: priority=PriorityHigh, stage=Triage, status=Opened." in result.content


@pytest.mark.asyncio
async def test_update_case_accepted_but_unchanged_is_an_error(monkeypatch):
    """The PATCH echoes the request; the case itself still says LOW."""
    result, _ = await _update_case(
        monkeypatch,
        {"priority": "LOW", "stage": "Triage"},
        patch_answer={"priority": "HIGH"},
        priority="high",
    )
    assert result.is_error
    assert result.content.startswith("Error: case 9001 did not take the update as requested")
    assert "priority: sent 'HIGH', platform now reports 'LOW'" in result.content
    assert "updated and confirmed" not in result.content


@pytest.mark.asyncio
async def test_update_case_contradicted_stage_is_an_error(monkeypatch):
    result, _ = await _update_case(
        monkeypatch, {"priority": "HIGH", "stage": "Triage"}, stage="Investigation"
    )
    assert result.is_error
    assert "stage: sent 'Investigation', platform now reports 'Triage'" in result.content


@pytest.mark.asyncio
async def test_update_case_checks_assignee_and_display_name(monkeypatch):
    # A normalized assignee is an observation, never a refusal.
    result, _ = await _update_case(
        monkeypatch,
        {"priority": "HIGH", "assignee": "@Tier1", "displayName": "Phishing wave"},
        priority="high",
        assignee="tier1",
        display_name="phishing wave",
    )
    assert not result.is_error
    assert "confirmed on the platform (priority=HIGH, displayName=phishing wave)" in result.content
    assert "Reported differently: assignee: sent 'tier1', platform reports '@Tier1'" in result.content


@pytest.mark.asyncio
async def test_update_case_names_fields_the_read_back_does_not_report(monkeypatch):
    result, _ = await _update_case(
        monkeypatch, {"priority": "HIGH"}, priority="high", assignee="tier1"
    )
    assert not result.is_error
    assert "Not verifiable: assignee=tier1" in result.content


@pytest.mark.asyncio
async def test_update_case_with_nothing_observable_confirms_nothing(monkeypatch):
    result, _ = await _update_case(monkeypatch, {}, assignee="tier1")
    assert not result.is_error
    assert "nothing in the read-back confirms it" in result.content
    # Each field is still classified, even when it is the only one.
    assert "Not verifiable: assignee=tier1" in result.content


@pytest.mark.parametrize(
    ("observed", "expected"),
    [("PriorityHigh", "HIGH"), ("PRIORITY_HIGH", "HIGH"), ("high", "HIGH"), ("PriorityInfo", "INFORMATIVE")],
)
def test_observed_case_priority_spellings(observed, expected):
    assert gs._observed_case_priority(observed) == expected


@pytest.mark.asyncio
async def test_update_alert_says_so_when_it_cannot_confirm(monkeypatch):
    """A failed read-back is reported as unconfirmed, not as an error or a success."""
    # Every call answers an empty document: the write is accepted and the
    # read-back carries no alert to confirm it with.
    executor, _ = _build(monkeypatch, lambda _m, _u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="uu",
            name="update_secops_alert",
            arguments={"alert_id": "det_123", "priority": "low"},
        )
    )
    assert not result.is_error
    assert "NOT confirmed" in result.content
    assert "get_secops_alert" in result.content


@pytest.mark.asyncio
async def test_update_alert_separates_the_severity_score_from_its_label(monkeypatch):
    """severity is a score the summary never echoes; severityDisplay is the label.

    Comparing the two would manufacture a contradiction, so the score is
    reported as unverifiable and the observed label is printed next to it.
    """
    executor, _ = _build(
        monkeypatch, _readback_router({"severityDisplay": "HIGH"})
    )
    result = await executor.execute_tool(
        ToolCall(
            id="us",
            name="update_secops_alert",
            arguments={"alert_id": "det_123", "severity": 5},
        )
    )
    assert not result.is_error
    # Nothing was confirmed and nothing was contradicted — and the headline must
    # not claim confirmation on the strength of a 200.
    assert "nothing in the read-back confirms it" in result.content
    assert "confirmed on the platform" not in result.content
    assert "severityDisplay='HIGH'" in result.content
    assert "Pass severity_display" in result.content


@pytest.mark.asyncio
async def test_update_alert_sends_severity_display_only_when_asked(monkeypatch):
    """Opt-in field: an existing call's payload must be byte-identical to before."""
    executor, recorder = _build(monkeypatch, _readback_router({}))
    await executor.execute_tool(
        ToolCall(
            id="ud1",
            name="update_secops_alert",
            arguments={"alert_id": "det_123", "severity": 5},
        )
    )
    assert _post_call(recorder)["json"]["feedback"] == {"severity": 5}

    await executor.execute_tool(
        ToolCall(
            id="ud2",
            name="update_secops_alert",
            arguments={"alert_id": "det_123", "severity_display": "LOW"},
        )
    )
    assert _post_call(recorder)["json"]["feedback"] == {"severityDisplay": "LOW"}


@pytest.mark.asyncio
async def test_update_alert_treats_a_blank_severity_display_as_unset(monkeypatch):
    """A whitespace-only label must not be sent as an empty string.

    Sending it would either clear the label an analyst reads or fail
    validation — and it would turn a no-op call into a write.
    """
    executor, recorder = _build(monkeypatch, _readback_router({}))
    result = await executor.execute_tool(
        ToolCall(
            id="ublank",
            name="update_secops_alert",
            arguments={"alert_id": "det_123", "severity_display": "   "},
        )
    )
    assert "nothing to update" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_alert_reports_the_soar_row_the_analyst_looks_at(monkeypatch):
    """SIEM feedback and the SOAR case-alert row are different objects."""
    executor, _ = _build(
        monkeypatch,
        _readback_router(
            {"priority": "PRIORITY_LOW"},
            case_name="projects/p/locations/us/instances/c/cases/9001",
            soar=[
                {"siemAlertId": "other", "priority": "PriorityLow"},
                {"siemAlertId": "det_123", "priority": "PriorityHigh", "status": "Opened"},
            ],
        ),
    )
    result = await executor.execute_tool(
        ToolCall(
            id="usoar",
            name="update_secops_alert",
            arguments={"alert_id": "det_123", "priority": "low"},
        )
    )
    assert "SOAR view (case 9001): priority=PriorityHigh | status=Opened" in result.content


@pytest.mark.asyncio
async def test_update_alert_validates_score_range_and_empty_update(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})

    out_of_range = await executor.execute_tool(
        ToolCall(
            id="uar",
            name="update_secops_alert",
            arguments={"alert_id": "det_123", "severity": 150},
        )
    )
    assert "between 0 and 100" in out_of_range.content

    empty = await executor.execute_tool(
        ToolCall(id="uae", name="update_secops_alert", arguments={"alert_id": "det_123"})
    )
    assert "nothing to update" in empty.content
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# UDM events on an alert
# ---------------------------------------------------------------------------


_ALERT_WITH_EVENTS = {
    "alert": {
        "id": "det_123",
        "type": "RULE_DETECTION",
        "detectionTime": "2024-05-01T10:00:00Z",
        "caseName": "projects/p/locations/us/instances/c/cases/9001",
        "feedbackSummary": {"status": "NEW", "priority": "PRIORITY_HIGH"},
        "detection": [{"ruleName": "Suspicious PowerShell", "ruleId": "ru_1"}],
        "collectionElements": [
            {
                "references": [
                    {
                        "event": {
                            "metadata": {
                                "eventTimestamp": "2024-05-01T09:59:00Z",
                                "eventType": "PROCESS_LAUNCH",
                                "productName": "Windows Defender",
                            },
                            "principal": {
                                "hostname": "WIN-01",
                                "ip": ["10.0.0.5"],
                                "platform": "WINDOWS",
                                "platformVersion": "10.0.19045",
                                "user": {"userid": "jdoe"},
                                "process": {"commandLine": "powershell -enc ZQBj"},
                            },
                            "target": {
                                "hostname": "evil.example.com",
                                "ip": ["203.0.113.10"],
                                "file": {"sha256": "a" * 64},
                            },
                            "securityResult": [
                                {"summary": "Encoded command", "severity": "HIGH", "action": ["BLOCK"]}
                            ],
                        }
                    }
                ]
            }
        ],
    }
}


@pytest.mark.asyncio
async def test_get_alert_surfaces_udm_events_and_observables(monkeypatch):
    """The events carry the IP / hostname / OS an analyst pivots on."""
    executor, _ = _build(monkeypatch, lambda m, u: _ALERT_WITH_EVENTS)
    result = await executor.execute_tool(
        ToolCall(id="gae", name="get_secops_alert", arguments={"alert_id": "det_123"})
    )
    assert not result.is_error
    assert "PROCESS_LAUNCH" in result.content
    assert "WIN-01" in result.content
    assert "10.0.0.5" in result.content
    assert "WINDOWS 10.0.19045" in result.content
    assert "jdoe" in result.content
    assert "Observables:" in result.content
    assert "203.0.113.10" in result.content
    assert "a" * 64 in result.content
    # The case is surfaced so the agent can walk on to its other alerts.
    assert "Case: 9001" in result.content


@pytest.mark.asyncio
async def test_get_alert_says_so_when_no_events_are_attached(monkeypatch):
    executor, _ = _build(
        monkeypatch,
        lambda m, u: {"alert": {"id": "det_9", "detection": [{"ruleName": "R"}]}},
    )
    result = await executor.execute_tool(
        ToolCall(id="gan", name="get_secops_alert", arguments={"alert_id": "det_9"})
    )
    assert "No UDM events are attached" in result.content


# ---------------------------------------------------------------------------
# Alerts view — streaming body + case → alerts → events
# ---------------------------------------------------------------------------


def test_parse_stream_payload_merges_concatenated_chunks():
    """legacyFetchAlertsView streams several JSON objects, not one document."""
    body = (
        '{"progress": 0.5, "alerts": {"alerts": [{"id": "a1"}]}}\n'
        '{"progress": 1, "complete": true, "alerts": {"alerts": [{"id": "a2"}]}}'
    )
    merged = gs._parse_stream_payload(body)
    assert merged["complete"] is True
    assert merged["alerts"]["alerts"] == [{"id": "a2"}]

    # A top-level array of snapshots merges the same way.
    array_body = '[{"progress": 0.5}, {"complete": true, "filteredAlertsCount": 2}]'
    assert gs._parse_stream_payload(array_body) == {
        "progress": 0.5,
        "complete": True,
        "filteredAlertsCount": 2,
    }

    assert gs._parse_stream_payload("") == {}
    assert gs._parse_stream_payload("not json at all") == {}


def _alerts_view_body(*alerts: dict, complete: bool = True) -> str:
    return json.dumps({"complete": complete, "alerts": {"alerts": list(alerts)}})


_CASE_ALERTS_PAGE = {
    "caseAlerts": [
        {
            "name": "projects/p/locations/us/instances/c/cases/9001/caseAlerts/77",
            "identifier": "SOAR-ALERT-77",
            "caseId": 9001,
            "displayName": "Suspicious PowerShell",
            "priority": "HIGH",
            "status": "OPENED",
            "product": "Windows Defender",
            "eventCount": 4,
            "siemAlertId": "det_123",
            "startTime": "2024-05-01T10:00:00Z",
        },
        {
            "identifier": "SOAR-ONLY-78",
            "caseId": 9001,
            "displayName": "Manual alert",
            "priority": "LOW",
            "status": "OPENED",
        },
    ]
}


@pytest.mark.asyncio
async def test_list_case_alerts_uses_the_case_alerts_collection(monkeypatch):
    """caseAlerts.list is the first-class 'alerts in a case' endpoint."""
    executor, recorder = _build(monkeypatch, lambda m, u: _CASE_ALERTS_PAGE)
    result = await executor.execute_tool(
        ToolCall(
            id="lca",
            name="list_secops_case_alerts",
            arguments={"case_id": "9001", "filter_query": "priority='HIGH'"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert "/v1beta/" in call["url"]
    assert call["url"].endswith("/cases/9001/caseAlerts")
    assert call["params"]["filter"] == "priority='HIGH'"
    # The three ids are named apart, which is the confusion the customer hit.
    assert "siem_alert_id: det_123" in result.content
    assert "soar identifier: SOAR-ALERT-77" in result.content
    assert "case_id: 9001" in result.content
    # A SOAR-only alert has no SIEM id and says so rather than showing a blank.
    assert "(none — SOAR-only alert)" in result.content


@pytest.mark.asyncio
async def test_case_alerts_render_the_involved_entity_graph(monkeypatch):
    """involvedRelations is the only entity source a SOAR-only alert has."""
    page = {
        "caseAlerts": [
            {
                "identifier": "SOAR-ONLY-78",
                "displayName": "Manual alert",
                "involvedRelations": [
                    {
                        "from": {"identifier": "WIN-01", "type": "HOSTNAME"},
                        "to": {"identifier": "203.0.113.10", "type": "ADDRESS"},
                    },
                    # A repeated endpoint must not be listed twice.
                    {"from": {"identifier": "WIN-01", "type": "HOSTNAME"}},
                ],
            }
        ]
    }
    executor, recorder = _build(monkeypatch, lambda m, u: page)
    result = await executor.execute_tool(
        ToolCall(id="rel", name="list_secops_case_alerts", arguments={"case_id": "9001"})
    )
    assert recorder.calls[-1]["params"]["expand"] == "involvedRelations"
    assert "Entities: WIN-01 [HOSTNAME], 203.0.113.10 [ADDRESS]" in result.content


@pytest.mark.asyncio
async def test_case_comments_round_trip(monkeypatch):
    """post_case_comment parity: write findings back, and read prior notes."""
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: (
            {"userOwnerFullName": "XTM One"}
            if m == "post"
            else {
                "caseComments": [
                    {
                        "createTime": "1714557600000",
                        "user": "jdoe",
                        "comment": "Confirmed Emotet, host isolated.",
                    },
                    {"comment": "removed", "isDeleted": True},
                ]
            }
        ),
    )

    posted = await executor.execute_tool(
        ToolCall(
            id="cc",
            name="add_secops_case_comment",
            arguments={"case_id": "9001", "comment": "Sandbox detonation confirms."},
        )
    )
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert "/v1beta/" in call["url"]
    assert call["url"].endswith("/cases/9001/caseComments")
    assert call["json"] == {"comment": "Sandbox detonation confirms."}
    assert "Comment added to case 9001" in posted.content

    listed = await executor.execute_tool(
        ToolCall(id="lcc", name="list_secops_case_comments", arguments={"case_id": "9001"})
    )
    assert "Confirmed Emotet" in listed.content
    assert "2024-05-01T10:00:00Z" in listed.content
    # Soft-deleted comments are filtered out.
    assert "removed" not in listed.content


@pytest.mark.asyncio
async def test_add_case_comment_requires_a_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="cce",
            name="add_secops_case_comment",
            arguments={"case_id": "9001", "comment": "  "},
        )
    )
    assert "comment body is required" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_list_case_events_walks_case_to_siem_alert_to_events(monkeypatch):
    """case -> caseAlerts -> siemAlertId -> legacyGetAlert is the documented path."""

    def router(method, url):
        if url.endswith("/caseAlerts"):
            return _CASE_ALERTS_PAGE
        return _ALERT_WITH_EVENTS

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="lce", name="list_secops_case_events", arguments={"case_id": "9001"}
        )
    )
    assert not result.is_error
    # Only the SIEM-backed alert is fetched; the SOAR-only one is skipped.
    fetches = [c for c in recorder.calls if "legacyGetAlert" in c["url"]]
    assert len(fetches) == 1
    assert fetches[0]["params"]["alertId"] == "det_123"
    assert "WIN-01" in result.content
    assert "Observables:" in result.content


@pytest.mark.asyncio
async def test_list_case_events_reports_soar_only_cases(monkeypatch):
    """No siemAlertId means no UDM events — say why instead of 'none found'."""
    page = {"caseAlerts": [{"identifier": "SOAR-ONLY-78", "displayName": "Manual"}]}
    executor, _ = _build(monkeypatch, lambda m, u: page)
    result = await executor.execute_tool(
        ToolCall(id="lce0", name="list_secops_case_events", arguments={"case_id": "9001"})
    )
    assert "siemAlertId" in result.content
    assert "SOAR-only alerts" in result.content


@pytest.mark.asyncio
async def test_list_case_events_narrows_by_either_alert_id(monkeypatch):
    def router(method, url):
        if url.endswith("/caseAlerts"):
            return _CASE_ALERTS_PAGE
        return _ALERT_WITH_EVENTS

    executor, _ = _build(monkeypatch, router)
    # The SOAR identifier resolves just as well as the SIEM alert id.
    by_soar = await executor.execute_tool(
        ToolCall(
            id="n1",
            name="list_secops_case_events",
            arguments={"case_id": "9001", "alert_id": "SOAR-ALERT-77"},
        )
    )
    assert "WIN-01" in by_soar.content

    miss = await executor.execute_tool(
        ToolCall(
            id="n2",
            name="list_secops_case_events",
            arguments={"case_id": "9001", "alert_id": "nope"},
        )
    )
    assert "was not found in case 9001" in miss.content


@pytest.mark.asyncio
async def test_alerts_view_surfaces_an_invalid_snapshot_query(monkeypatch):
    """A bad snapshot query returns HTTP 200 with validSnapshotQuery=false."""
    body = json.dumps(
        {
            "complete": True,
            "validSnapshotQuery": False,
            "queryValidationErrors": [{"message": "unknown field 'bogus'"}],
            "alerts": {"alerts": []},
        }
    )
    executor, _ = _build(monkeypatch, lambda m, u: body)
    result = await executor.execute_tool(
        ToolCall(
            id="bad", name="list_secops_alerts", arguments={"snapshot_query": "bogus = 1"}
        )
    )
    assert "rejected by Google SecOps" in result.content
    assert "unknown field 'bogus'" in result.content
    assert "snapshot query syntax" in result.content.lower()


@pytest.mark.asyncio
async def test_alerts_view_polls_until_complete_and_flags_a_partial_answer(monkeypatch):
    monkeypatch.setattr(gs, "_ALERT_VIEW_POLL_INTERVAL", 0)
    monkeypatch.setattr(gs, "_ALERT_VIEW_MAX_POLLS", 3)

    executor, recorder = _build(
        monkeypatch,
        lambda m, u: _alerts_view_body({"id": "a1"}, complete=False),
    )
    result = await executor.execute_tool(
        ToolCall(id="lav", name="list_secops_alerts", arguments={})
    )
    assert len(recorder.calls) == 3
    assert "[Partial" in result.content

    executor, recorder = _build(
        monkeypatch, lambda m, u: _alerts_view_body({"id": "a1"}, complete=True)
    )
    done = await executor.execute_tool(
        ToolCall(id="lav2", name="list_secops_alerts", arguments={})
    )
    assert len(recorder.calls) == 1
    assert "[Partial" not in done.content
    assert (
        recorder.calls[0]["params"]["snapshotQuery"]
        == 'feedback_summary.status != "CLOSED"'
    )


# ---------------------------------------------------------------------------
# Entity lookup — the free-text search path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected_query", "expected_type"),
    [
        ("10.0.0.5", 'ip = "10.0.0.5"', "ASSET"),
        ("2001:db8::1", 'ip = "2001:db8::1"', "ASSET"),
        ("a" * 64, f'hash = "{"a" * 64}"', "FILE"),
        ("jdoe@example.com", 'email = "jdoe@example.com"', "USER"),
        # A MAC is all-hex-and-colons like an IPv6 address, so it must be
        # matched as a MAC rather than swallowed by IP detection.
        ("00:1A:2B:3C:4D:5E", 'mac = "00:1A:2B:3C:4D:5E"', "ASSET"),
        ("evil.example.com", 'domain = "evil.example.com"', "DOMAIN_NAME"),
        ("WIN-01", 'hostname = "WIN-01"', "ASSET"),
    ],
)
def test_detect_entity_query(value, expected_query, expected_type):
    assert gs._detect_entity_query(value) == (expected_query, expected_type)


@pytest.mark.asyncio
async def test_lookup_entity_resolves_a_bare_hostname(monkeypatch):
    """A bare hostname is not valid UDM syntax — this is the free-text path."""

    def router(method, url):
        if url.endswith(":summarizeEntitiesFromQuery"):
            return {
                "entitySummaries": [
                    {
                        "entity": [
                            {
                                "name": "projects/p/locations/us/instances/c/entities/ent_1",
                                "metadata": {"entityType": "ASSET"},
                                "metric": {
                                    "firstSeen": "2024-04-01T00:00:00Z",
                                    "lastSeen": "2024-05-01T00:00:00Z",
                                },
                                "entity": {
                                    "asset": {
                                        "hostname": "WIN-01",
                                        "ip": ["10.0.0.5"],
                                        "platformSoftware": {
                                            "platform": "WINDOWS",
                                            "platformVersion": "10.0.19045",
                                        },
                                    }
                                },
                            }
                        ]
                    }
                ]
            }
        return {
            "alertCounts": [{"rule": "Suspicious PowerShell", "count": 3}],
            "timeline": {"buckets": [{"eventCount": 120, "alertCount": 3}]},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="le", name="lookup_secops_entity", arguments={"value": "WIN-01"}
        )
    )
    assert not result.is_error
    assert recorder.calls[0]["params"]["query"] == 'hostname = "WIN-01"'
    assert recorder.calls[1]["params"]["entityId"] == "ent_1"
    assert "WIN-01 [ASSET]" in result.content
    assert "IP: 10.0.0.5" in result.content
    assert "OS: WINDOWS 10.0.19045" in result.content
    assert "Suspicious PowerShell — 3" in result.content
    assert "120 events" in result.content


@pytest.mark.asyncio
async def test_lookup_entity_reports_a_miss_with_the_query_it_used(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: {"entitySummaries": []})
    result = await executor.execute_tool(
        ToolCall(id="lem", name="lookup_secops_entity", arguments={"value": "WIN-99"})
    )
    assert "No entity found for 'WIN-99'" in result.content
    assert 'hostname = "WIN-99"' in result.content
