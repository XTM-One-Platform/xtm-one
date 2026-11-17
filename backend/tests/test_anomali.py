"""Tests for the Anomali ThreatStream integration provider.

Covers tool introspection (incl. the 2-part catalog introspection dummy), the
``apikey user:key`` auth-header helper and base-URL normalisation, the
intelligence search (filter forwarding + pagination hint from ``meta.next``),
observable detail, import-with-approval (form payload + session ID surfacing),
import session approval, the threat model manage tool (action guard, tagging,
associations, delete), the investigation manage tool (element type mapping),
and the whitelist/rule manage tools. Every API call routes through the shared
``safe_request`` helper, which is monkeypatched here so no network is touched.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.providers import anomali as an
from app.integrations.providers.anomali import (
    anomali_api_base,
    build_anomali_headers,
    build_tools,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://api.threatstream.com||analyst@example.com||s3cret-key"


class _StubResponse:
    """Minimal httpx.Response stand-in exposing ``.json()``."""

    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, (list, dict)):
            return self._payload
        raise ValueError("response body is not JSON")


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls."""

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        return _StubResponse(self._router(method, url)), None


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(an, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Sample ThreatStream payloads
# ---------------------------------------------------------------------------

_INTEL_PAGE = {
    "meta": {
        "limit": 2,
        "next": "/api/v2/intelligence/?limit=2&offset=2",
        "offset": 0,
        "total_count": 49906,
    },
    "objects": [
        {
            "id": 50460807643,
            "value": "5.29.211.60",
            "itype": "tor_ip",
            "type": "ip",
            "confidence": 100,
            "status": "active",
            "meta": {"severity": "low"},
            "source": "TOR Exit Nodes",
            "tags": [{"name": "tor"}],
            "modified_ts": "2026-04-11T13:37:28.423Z",
        },
        {
            "id": 50460807644,
            "value": "evil.example.com",
            "itype": "c2_domain",
            "type": "domain",
            "confidence": 85,
            "status": "active",
            "meta": {"severity": "high"},
            "source": "Analyst",
            "tags": None,
            "modified_ts": "2026-04-12T09:00:00.000Z",
        },
    ],
}

_IMPORT_OK = {"success": True, "import_session_id": 12345, "job_id": "abc-123"}


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(
        token="https://dummy.example.com||__dummy__||__dummy__", executor=executor
    )
    names = {t.name for t in tools}
    assert "search_anomali_intelligence" in names
    assert "get_anomali_observable" in names
    assert "import_anomali_observables" in names
    assert "approve_anomali_import_session" in names
    assert "list_anomali_threat_models" in names
    assert "manage_anomali_threat_model" in names
    assert "create_anomali_investigation" in names
    assert "manage_anomali_rule" in names
    assert "manage_anomali_whitelist" in names
    assert "manage_anomali_sandbox" in names
    assert "delete_anomali_observable" in names
    assert len(tools) >= 27


def test_build_tools_works_with_two_part_introspection_dummy():
    # The catalog introspects compound providers with a 2-part url dummy
    # (``https://dummy.example.com||__dummy__``); the builder must still return
    # the full tool list so the catalog shows every ThreatStream tool.
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    assert len(tools) >= 27


def test_build_tools_empty_without_username():
    executor = CallableToolExecutor()
    # base URL only, no username/api key -> no tools
    assert build_tools(token="https://api.threatstream.com||", executor=executor) == []


def test_headers_and_base_url_helpers():
    headers = build_anomali_headers("analyst@example.com", "s3cret")
    assert headers["Authorization"] == "apikey analyst@example.com:s3cret"
    assert headers["Accept"] == "application/json"
    # Whitespace pasted around the credentials is stripped: a trailing newline
    # would otherwise produce an Authorization header that always fails.
    padded = build_anomali_headers("  analyst@example.com\n", " s3cret\n")
    assert padded["Authorization"] == "apikey analyst@example.com:s3cret"
    # base URL default + trailing-slash normalisation + whitespace stripping
    assert anomali_api_base("") == "https://api.threatstream.com"
    assert anomali_api_base("   \n") == "https://api.threatstream.com"
    assert anomali_api_base("https://ts.example.com/") == "https://ts.example.com"
    assert anomali_api_base("  https://ts.example.com/ \n") == "https://ts.example.com"
    # A trailing /api, /api/v1, or /api/v2 segment (how the ThreatStream docs
    # often present the base) is trimmed so endpoint paths never double up
    # into /api/api/v2/...; matching is case-insensitive.
    assert anomali_api_base("https://ts.example.com/api") == "https://ts.example.com"
    assert anomali_api_base("https://ts.example.com/api/") == "https://ts.example.com"
    assert anomali_api_base("https://ts.example.com/api/v1") == "https://ts.example.com"
    assert anomali_api_base("https://ts.example.com/api/v2/") == "https://ts.example.com"
    assert anomali_api_base("https://ts.example.com/API") == "https://ts.example.com"
    # A pasted bare /api (pathological but possible) falls back to the default
    assert anomali_api_base("/api") == "https://api.threatstream.com"


# ---------------------------------------------------------------------------
# Intelligence search (filters + pagination)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_intelligence_forwards_filters_and_paginates(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _INTEL_PAGE)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="search_anomali_intelligence",
            arguments={
                "itype": "tor_ip",
                "status": "active",
                "confidence_min": 80,
                "tags": "tor",
                "limit": 2,
            },
        )
    )
    assert not result.is_error
    assert "5.29.211.60" in result.content
    assert "evil.example.com" in result.content
    assert "tor_ip" in result.content
    # meta.next present -> pagination hint with the next offset
    assert 'offset="2"' in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/api/v2/intelligence/")
    assert call["params"]["itype"] == "tor_ip"
    assert call["params"]["status"] == "active"
    assert call["params"]["confidence__gte"] == 80
    assert call["params"]["tags.name"] == "tor"
    assert call["headers"]["Authorization"] == (
        "apikey analyst@example.com:s3cret-key"
    )


@pytest.mark.asyncio
async def test_get_observable_detail(monkeypatch):
    obj = dict(_INTEL_PAGE["objects"][1])
    obj["threat_type"] = "c2"
    obj["description"] = "Known C2 infrastructure"
    executor, recorder = _build(monkeypatch, lambda m, u: obj)
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="get_anomali_observable",
            arguments={"observable_id": "50460807644"},
        )
    )
    assert not result.is_error
    assert "evil.example.com" in result.content
    assert "c2" in result.content
    assert "Known C2 infrastructure" in result.content
    assert recorder.calls[0]["url"].endswith("/api/v2/intelligence/50460807644/")


@pytest.mark.asyncio
async def test_update_observable_severity_is_top_level(monkeypatch):
    # The single-observable PATCH takes ``severity`` as a top-level key (the
    # Splunk SOAR reference connector's shape); nesting it under ``meta``
    # would clobber the observable's other meta fields.
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c2b",
            name="update_anomali_observable",
            arguments={
                "observable_id": "50460807644",
                "severity": "High",
                "status": "falsepos",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["method"] == "patch"
    assert call["url"].endswith("/api/v2/intelligence/50460807644/")
    assert call["json"] == {"status": "falsepos", "severity": "high"}


# ---------------------------------------------------------------------------
# Import with approval + session approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_observables_with_approval(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _IMPORT_OK)
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="import_anomali_observables",
            arguments={
                "datatext": "1.2.3.4\nevil.example.com",
                "confidence": 70,
                "severity": "high",
                "tags": "campaign-x,apt",
                "tags_tlp": "red",
            },
        )
    )
    assert not result.is_error
    assert "12345" in result.content  # import session id surfaced
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/v2/intelligence/import/")
    # Import parameters ride as form data, not JSON
    assert call["data"]["datatext"] == "1.2.3.4\nevil.example.com"
    assert call["data"]["confidence"] == 70
    assert call["data"]["severity"] == "high"
    tags = json.loads(call["data"]["tags"])
    assert {"name": "campaign-x", "tlp": "red"} in tags


@pytest.mark.asyncio
async def test_import_observables_rejects_bad_severity(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _IMPORT_OK)
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="import_anomali_observables",
            arguments={"datatext": "1.2.3.4", "severity": "catastrophic"},
        )
    )
    assert "severity must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_import_observables_rejects_bad_default_state(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _IMPORT_OK)
    result = await executor.execute_tool(
        ToolCall(
            id="c4b",
            name="import_anomali_observables",
            arguments={"datatext": "1.2.3.4", "default_state": "quarantined"},
        )
    )
    assert "default_state must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_approve_import_session(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="approve_anomali_import_session",
            arguments={"import_id": "12345"},
        )
    )
    assert not result.is_error
    assert "approved" in result.content.lower()
    call = recorder.calls[0]
    assert call["method"] == "patch"
    assert call["url"].endswith("/api/v1/importsession/12345/approve_all/")


# ---------------------------------------------------------------------------
# Threat model manage tool (guards + actions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_threat_model_rejects_invalid_model(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="manage_anomali_threat_model",
            arguments={"action": "delete", "model": "unicorn", "entity_id": "1"},
        )
    )
    assert "model must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_threat_model_rejects_invalid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="manage_anomali_threat_model",
            arguments={"action": "nuke", "model": "actor", "entity_id": "1"},
        )
    )
    assert "action must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_threat_model_associate(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"success": True, "ids": [7, 8]}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="manage_anomali_threat_model",
            arguments={
                "action": "associate",
                "model": "actor",
                "entity_id": "42",
                "associated_model": "campaign",
                "associated_ids": "7, 8",
            },
        )
    )
    assert not result.is_error
    assert "2 campaign entities" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/v1/actor/42/campaign/bulk_add/")
    assert call["json"] == {"ids": [7, 8]}


@pytest.mark.asyncio
async def test_manage_threat_model_associate_intelligence_says_observables(
    monkeypatch,
):
    # ``intelligence`` is the API name for observables; the user-facing
    # message must say "observable(s)", not "intelligence entities".
    executor, recorder = _build(monkeypatch, lambda m, u: {"success": True})
    result = await executor.execute_tool(
        ToolCall(
            id="c8b",
            name="manage_anomali_threat_model",
            arguments={
                "action": "associate",
                "model": "incident",
                "entity_id": "42",
                "associated_model": "intelligence",
                "associated_ids": "101,102",
            },
        )
    )
    assert not result.is_error
    assert "2 observables associated" in result.content
    assert "intelligence entities" not in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/api/v1/incident/42/intelligence/bulk_add/")
    assert call["json"] == {"ids": [101, 102]}


@pytest.mark.asyncio
async def test_manage_threat_model_comment_requires_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="manage_anomali_threat_model",
            arguments={"action": "comment", "model": "actor", "entity_id": "42"},
        )
    )
    assert "comment_body is required" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_threat_model_comment_sends_raw_text_body(monkeypatch):
    # The comment endpoint consumes the raw comment text as the request body
    # (the Splunk SOAR reference connector's shape) and works on any threat
    # model entity type, not just threat bulletins.
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c9b",
            name="manage_anomali_threat_model",
            arguments={
                "action": "comment",
                "model": "actor",
                "entity_id": "42",
                "comment_body": "Seen in incident IR-77",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/v1/actor/42/comment/")
    assert call["content"] == b"Seen in incident IR-77"
    assert "json" not in call


@pytest.mark.asyncio
async def test_manage_threat_model_delete(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="manage_anomali_threat_model",
            arguments={"action": "delete", "model": "incident", "entity_id": "99"},
        )
    )
    assert not result.is_error
    assert "deleted" in result.content.lower()
    call = recorder.calls[0]
    assert call["method"] == "delete"
    assert call["url"].endswith("/api/v1/incident/99/")


# ---------------------------------------------------------------------------
# Investigations (element type mapping)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_investigation_add_elements_maps_observable_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"objects": [{}]})
    result = await executor.execute_tool(
        ToolCall(
            id="c11",
            name="manage_anomali_investigation",
            arguments={
                "action": "add_elements",
                "investigation_id": "5",
                "element_model": "observable",
                "element_ids": "101,102",
            },
        )
    )
    assert not result.is_error
    assert "2 element(s)" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/api/v1/investigationelement/")
    assert call["params"] == {"investigation_id": "5"}
    # Observables use the internal ``intelligence2`` r_type
    assert call["json"]["objects"] == [
        {"r_type": "intelligence2", "r_id": 101},
        {"r_type": "intelligence2", "r_id": 102},
    ]


@pytest.mark.asyncio
async def test_create_investigation_payload(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"id": 77, "name": "Case X"}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c12",
            name="create_anomali_investigation",
            arguments={
                "name": "Case X",
                "priority": "high",
                "status": "in-progress",
                "assignee_id": 3,
            },
        )
    )
    assert not result.is_error
    assert "#77" in result.content
    call = recorder.calls[0]
    assert call["json"]["priority"] == "high"
    assert call["json"]["status"] == "in-progress"
    assert call["json"]["assignee_id"] == 3
    assert call["json"]["assignee_type"] == "user"


# ---------------------------------------------------------------------------
# Rules + whitelist manage tools (guards + payloads)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_rule_create_requires_name_and_keywords(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"id": 1})
    result = await executor.execute_tool(
        ToolCall(id="c13", name="manage_anomali_rule", arguments={"action": "create"})
    )
    assert "name and keywords are required" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_rule_create(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"id": 55})
    result = await executor.execute_tool(
        ToolCall(
            id="c14",
            name="manage_anomali_rule",
            arguments={
                "action": "create",
                "name": "APT keywords",
                "keywords": "aptx, evilcorp",
                "match_threat_bulletins": True,
            },
        )
    )
    assert not result.is_error
    assert "#55" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/v1/rule/")
    assert call["json"]["keywords"] == ["aptx", "evilcorp"]
    assert call["json"]["match_observables"] is True
    assert call["json"]["match_tips"] is True


@pytest.mark.asyncio
async def test_manage_rule_update_omits_unset_match_flags(monkeypatch):
    # Tri-state semantics: an update that only changes keywords must NOT
    # silently reset the rule's match_* settings back to the create defaults.
    executor, recorder = _build(monkeypatch, lambda m, u: {"id": 55})
    result = await executor.execute_tool(
        ToolCall(
            id="c14b",
            name="manage_anomali_rule",
            arguments={
                "action": "update",
                "rule_id": "55",
                "keywords": "newkeyword",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["method"] == "patch"
    assert call["url"].endswith("/api/v1/rule/55/")
    assert call["json"]["keywords"] == ["newkeyword"]
    for flag in (
        "match_observables",
        "match_tips",
        "match_signatures",
        "match_vulnerabilities",
    ):
        assert flag not in call["json"]

    explicit = await executor.execute_tool(
        ToolCall(
            id="c14c",
            name="manage_anomali_rule",
            arguments={
                "action": "update",
                "rule_id": "55",
                "match_signatures": True,
            },
        )
    )
    assert not explicit.is_error
    assert recorder.calls[1]["json"] == {"match_signatures": True}


@pytest.mark.asyncio
async def test_manage_whitelist_add(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c15",
            name="manage_anomali_whitelist",
            arguments={
                "action": "add",
                "value_type": "ip",
                "values": "10.0.0.1, 10.0.0.2",
            },
        )
    )
    assert not result.is_error
    assert "2 whitelist" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/api/v1/orgwhitelist/bulk/")
    assert call["json"]["whitelist"] == [
        {"value_type": "ip", "value": "10.0.0.1"},
        {"value_type": "ip", "value": "10.0.0.2"},
    ]


@pytest.mark.asyncio
async def test_manage_whitelist_rejects_invalid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c16",
            name="manage_anomali_whitelist",
            arguments={"action": "purge"},
        )
    )
    assert "action must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_whitelist_rejects_invalid_value_type(monkeypatch):
    # The docstring/schema advertise a fixed value_type set; an unknown type
    # must be refused locally instead of being forwarded to the API.
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c16b",
            name="manage_anomali_whitelist",
            arguments={"action": "add", "value_type": "hostname", "values": "x.io"},
        )
    )
    assert "value_type must be one of" in result.content
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Observable tag management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_observable_tags_add_and_remove(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    added = await executor.execute_tool(
        ToolCall(
            id="c17",
            name="manage_anomali_observable_tags",
            arguments={
                "action": "add",
                "observable_ids": "1,2",
                "tags": "apt,seen-in-ir",
                # Padded/cased TLP must be normalised, not forwarded verbatim.
                "tlp": " Red ",
            },
        )
    )
    assert not added.is_error
    add_call = recorder.calls[0]
    assert add_call["method"] == "post"
    assert add_call["url"].endswith("/api/v2/intelligence/bulk_tagging/")
    # IDs ride as integers (consistent with associate/add_elements payloads)
    assert add_call["json"]["ids"] == [1, 2]
    assert add_call["json"]["tags"] == [
        {"name": "apt", "tlp": "red"},
        {"name": "seen-in-ir", "tlp": "red"},
    ]

    removed = await executor.execute_tool(
        ToolCall(
            id="c18",
            name="manage_anomali_observable_tags",
            arguments={"action": "remove", "observable_ids": "1", "tags": "apt"},
        )
    )
    assert not removed.is_error
    rm_call = recorder.calls[1]
    assert rm_call["method"] == "patch"
    assert rm_call["url"].endswith("/api/v2/intelligence/bulk_remove_tags/")
    assert rm_call["json"]["ids"] == [1]
    assert rm_call["json"]["tags"] == [{"name": "apt"}]


@pytest.mark.asyncio
async def test_manage_observable_tags_rejects_non_numeric_ids(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c18b",
            name="manage_anomali_observable_tags",
            arguments={
                "action": "add",
                "observable_ids": "1,evil.example.com",
                "tags": "apt",
            },
        )
    )
    assert "observable_ids must be a CSV of numeric IDs" in result.content
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# URL path-segment encoding + null tolerance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_segments_are_percent_encoded(monkeypatch):
    # An LLM-provided pivot value or ID containing '/', '?' or '#' must not
    # rewrite the request path.
    executor, recorder = _build(monkeypatch, lambda m, u: {"results": []})
    await executor.execute_tool(
        ToolCall(
            id="c19",
            name="get_anomali_passive_dns",
            arguments={"value_type": "domain", "value": "evil.example.com/../admin"},
        )
    )
    url = recorder.calls[0]["url"]
    assert "evil.example.com%2F..%2Fadmin" in url
    assert "/../" not in url

    await executor.execute_tool(
        ToolCall(
            id="c20",
            name="get_anomali_observable",
            arguments={"observable_id": "1/../../secret"},
        )
    )
    assert "1%2F..%2F..%2Fsecret" in recorder.calls[1]["url"]


@pytest.mark.asyncio
async def test_sandbox_report_url_and_results_envelope(monkeypatch):
    # ThreatStream (Django) redirects slash-less paths with a 301 that httpx
    # does not follow; the report endpoint must carry the trailing slash.
    # The detonation report rides in a Cuckoo-style ``results`` envelope
    # (per the Cortex XSOAR v3 reference connector).
    payload = {
        "results": {
            "info": {"started": "2026-01-01 10:00:00", "duration": 120, "score": 7.5}
        }
    }
    executor, recorder = _build(monkeypatch, lambda m, u: payload)
    result = await executor.execute_tool(
        ToolCall(
            id="c21",
            name="manage_anomali_sandbox",
            arguments={"action": "report", "report_id": "777"},
        )
    )
    assert recorder.calls[0]["url"].endswith("/api/v1/submit/777/report/")
    assert "started: 2026-01-01 10:00:00" in result.content
    assert "duration: 120" in result.content
    assert "score: 7.5" in result.content


@pytest.mark.asyncio
async def test_search_clamps_confidence_bounds_to_100(monkeypatch):
    # Out-of-range confidence bounds (e.g. 1000) must be clamped to the
    # documented 0-100 range instead of drawing an avoidable 4xx from
    # ThreatStream; negative values keep meaning "no filter".
    executor, recorder = _build(monkeypatch, lambda m, u: _INTEL_PAGE)
    result = await executor.execute_tool(
        ToolCall(
            id="c22a",
            name="search_anomali_intelligence",
            arguments={"confidence_min": 1000, "confidence_max": 250},
        )
    )
    assert not result.is_error
    params = recorder.calls[0]["params"]
    assert params["confidence__gte"] == 100
    assert params["confidence__lte"] == 100

    await executor.execute_tool(
        ToolCall(
            id="c22b",
            name="search_anomali_intelligence",
            arguments={"confidence_min": -5, "confidence_max": 80},
        )
    )
    params = recorder.calls[1]["params"]
    assert "confidence__gte" not in params
    assert params["confidence__lte"] == 80


@pytest.mark.asyncio
async def test_search_tolerates_explicit_json_nulls(monkeypatch):
    # An explicit JSON null on optional numeric params must not raise a
    # TypeError inside the tool (None >= 0 comparisons).
    executor, recorder = _build(monkeypatch, lambda m, u: _INTEL_PAGE)
    result = await executor.execute_tool(
        ToolCall(
            id="c22",
            name="search_anomali_intelligence",
            arguments={
                "confidence_min": None,
                "confidence_max": None,
                "feed_id": None,
                "limit": None,
                "offset": None,
            },
        )
    )
    assert not result.is_error
    params = recorder.calls[0]["params"]
    assert params["limit"] == 25
    assert params["offset"] == 0
    assert "confidence__gte" not in params
    assert "confidence__lte" not in params
    assert "feed_id" not in params
