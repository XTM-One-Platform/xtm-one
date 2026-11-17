"""Tests for the HarfangLab EDR integration provider.

Covers the security-event triage flow (list filters / details / status
update), threat management (status, notes), endpoint containment
(isolate / deisolate with the ``requested`` / ``policy_not_allowed``
response contract), telemetry hunting, IOC management (duplicate guard,
payload shaping), investigation jobs (create → status → results), and
tool introspection. Every API call routes through the shared
``safe_request`` helper, which is monkeypatched here so no network is
touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import harfanglab as hl
from app.integrations.providers.harfanglab import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://edr.example.com||api-token-123"


class _StubResponse:
    def __init__(self, payload: Any, status_code: int = 200, text: str | None = None):
        self._payload = payload
        self.status_code = status_code
        # _api() checks resp.text to tell an empty 2xx body apart from a
        # non-JSON body; a regular payload stub reads as non-empty JSON.
        self.text = text if text is not None else ("" if payload is None else "{}")

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls.

    The router returns either a JSON payload (a 200 success) or a
    ``(payload, status_code, error_string)`` tuple to simulate a failed
    response the way ``safe_request`` reports it when the caller passes
    ``expected_statuses`` (response + error string).
    """

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        out = self._router(method, url)
        if isinstance(out, tuple):
            payload, status_code, err = out
            if status_code is None:
                # Simulates the no-response failure contract (network error,
                # or non-2xx without ``expected_statuses``): (None, err).
                return None, err
            return _StubResponse(payload, status_code), err
        return _StubResponse(out), None


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(hl, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    names = {t.name for t in tools}
    assert "list_harfanglab_security_events" in names
    assert "update_harfanglab_threat_status" in names
    assert "isolate_harfanglab_endpoint" in names
    assert "run_harfanglab_hunt_job" in names
    assert "add_harfanglab_ioc" in names
    assert len(tools) == 21
    # The destructive isolate must never be retained by truncation while its
    # undo (deisolate) is dropped — they share the same priority tier.
    by_name = {t.name: t for t in tools}
    assert (
        by_name["isolate_harfanglab_endpoint"].priority
        == by_name["deisolate_harfanglab_endpoint"].priority
    )


@pytest.mark.asyncio
async def test_build_tools_strips_credential_whitespace(monkeypatch):
    recorder = _Recorder(lambda m, u: {"count": 0, "results": []})
    monkeypatch.setattr(hl, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=" https://edr.example.com/ || api-token-123 ", executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="ws1", name="list_harfanglab_endpoints", arguments={})
    )
    assert not result.is_error
    call = recorder.calls[0]
    # Pasted whitespace must not leak into the URL or the auth header.
    assert call["url"] == "https://edr.example.com/api/data/endpoint/Agent/"
    assert call["headers"]["Authorization"] == "Token api-token-123"


@pytest.mark.asyncio
async def test_non_json_2xx_body_is_surfaced_as_error(monkeypatch):
    async def non_json(_client, method, url, _provider, action, **kwargs):
        return (
            _StubResponse(ValueError("not json"), text="<html>Bad Gateway</html>"),
            None,
        )

    monkeypatch.setattr(hl, "safe_request", non_json)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="nj1",
            name="get_harfanglab_threat",
            arguments={"threat_id": "t1"},
        )
    )
    assert "non-JSON response" in result.content


# ---------------------------------------------------------------------------
# Security events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_security_events_builds_filters(monkeypatch):
    def router(method, url):
        return {
            "count": 1,
            "results": [
                {
                    "id": "a1",
                    "rule_name": "Suspicious PowerShell",
                    "alert_type": "sigma",
                    "level": "critical",
                    "status": "new",
                    "alert_time": "2026-07-01T12:00:00Z",
                    "agent": {"hostname": "WIN-01"},
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_harfanglab_security_events",
            arguments={"status": "new,investigating", "level": "critical"},
        )
    )
    assert not result.is_error
    assert "Suspicious PowerShell" in result.content
    call = recorder.calls[0]
    assert call["url"].endswith("/api/data/alert/alert/Alert/")
    assert call["params"]["status"] == "investigating,new"
    assert call["params"]["level"] == "critical"
    assert call["headers"]["Authorization"] == "Token api-token-123"


@pytest.mark.asyncio
async def test_list_security_events_rejects_invalid_level(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"results": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="list_harfanglab_security_events",
            arguments={"level": "catastrophic"},
        )
    )
    assert "invalid level" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_get_security_event_unwraps_alert_envelope(monkeypatch):
    def router(method, url):
        return {
            "alert": {
                "id": "a1",
                "rule_name": "Mimikatz execution",
                "level": "critical",
                "status": "new",
                "alert_type": "yara",
                "alert_time": "2026-07-01T12:00:00Z",
                "agent": {"hostname": "SRV-01", "agentid": "ag-1"},
                "process": {
                    "image_name": "mimikatz.exe",
                    "username": "corp\\admin",
                    "commandline": "mimikatz.exe sekurlsa::logonpasswords",
                    "hashes": {"sha256": "cafe" * 16},
                },
            }
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="get_harfanglab_security_event",
            arguments={"event_id": "a1"},
        )
    )
    assert not result.is_error
    assert "Mimikatz execution" in result.content
    assert "mimikatz.exe" in result.content
    assert "cafe" * 16 in result.content


@pytest.mark.asyncio
async def test_get_security_event_encodes_path_segment(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"alert": {"id": "x"}})
    result = await executor.execute_tool(
        ToolCall(
            id="c3a",
            name="get_harfanglab_security_event",
            arguments={"event_id": "a/1?x=../y"},
        )
    )
    assert not result.is_error
    url = recorder.calls[0]["url"]
    # The crafted ID must stay a single, fully-encoded path segment.
    assert url.endswith("/api/data/alert/alert/Alert/a%2F1%3Fx%3D..%2Fy/details/")


@pytest.mark.asyncio
async def test_update_security_event_status_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="update_harfanglab_security_event_status",
            arguments={"event_ids": "a1, a2", "status": "closed"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/data/alert/alert/Alert/tag/")
    assert call["json"] == {"ids": ["a1", "a2"], "new_status": "closed"}


@pytest.mark.asyncio
async def test_update_security_event_status_rejects_invalid(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="update_harfanglab_security_event_status",
            arguments={"event_ids": "a1", "status": "bogus"},
        )
    )
    assert "invalid status" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Threats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_threat_status_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="update_harfanglab_threat_status",
            arguments={"threat_id": "t1", "status": "false_positive"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "patch"
    assert call["url"].endswith("/api/data/alert/alert/Threat/status/")
    assert call["json"]["threat_ids"] == ["t1"]
    assert call["json"]["new_status"] == "false_positive"
    assert call["json"]["tag_security_events"] is True


@pytest.mark.asyncio
async def test_update_threat_status_strips_threat_id(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c6d",
            name="update_harfanglab_threat_status",
            arguments={"threat_id": " t1 ", "status": "closed"},
        )
    )
    assert not result.is_error
    assert recorder.calls[-1]["json"]["threat_ids"] == ["t1"]
    result = await executor.execute_tool(
        ToolCall(
            id="c6e",
            name="update_harfanglab_threat_status",
            arguments={"threat_id": "  ", "status": "closed"},
        )
    )
    assert "threat_id is required" in result.content


@pytest.mark.asyncio
async def test_add_threat_note_creates_when_absent(monkeypatch):
    def router(method, url):
        if url.endswith("/note/") and method == "get":
            return {}
        if url.endswith("/Threat/t1/"):
            return {"id": "t1", "slug": "emotet-campaign"}
        return {}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="add_harfanglab_threat_note",
            arguments={"threat_id": "t1", "content": "Confirmed malicious."},
        )
    )
    assert not result.is_error
    assert "added" in result.content.lower()
    write = recorder.calls[-1]
    assert write["method"] == "post"
    assert write["json"] == {"title": "emotet-campaign", "content": "Confirmed malicious."}


@pytest.mark.asyncio
async def test_list_threats_builds_comma_filters(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"count": 0, "results": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c6a",
            name="list_harfanglab_threats",
            arguments={"level": "High, Critical", "status": "new,investigating"},
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["url"].endswith("/api/data/alert/alert/Threat/")
    assert call["params"]["level"] == "critical,high"
    assert call["params"]["status"] == "investigating,new"


@pytest.mark.asyncio
async def test_list_threats_rejects_invalid_level_and_status(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"results": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c6b",
            name="list_harfanglab_threats",
            arguments={"level": "catastrophic"},
        )
    )
    assert "invalid level" in result.content.lower()
    result = await executor.execute_tool(
        ToolCall(
            id="c6c",
            name="list_harfanglab_threats",
            arguments={"status": "probable_false_positive"},
        )
    )
    assert "invalid status" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_add_threat_note_updates_when_present(monkeypatch):
    def router(method, url):
        if url.endswith("/note/") and method == "get":
            return {"title": "emotet-campaign", "content": "old note"}
        if url.endswith("/Threat/t1/"):
            return {"id": "t1", "slug": "emotet-campaign"}
        return {}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="add_harfanglab_threat_note",
            arguments={"threat_id": "t1", "content": "Updated findings."},
        )
    )
    assert not result.is_error
    assert "updated" in result.content.lower()
    assert recorder.calls[-1]["method"] == "patch"


@pytest.mark.asyncio
async def test_add_threat_note_treats_404_probe_as_missing_note(monkeypatch):
    def router(method, url):
        if url.endswith("/note/") and method == "get":
            # Expected miss: the note endpoint 404s when no note exists.
            return ({"detail": "Not found."}, 404, "HarfangLab error 404")
        if url.endswith("/Threat/t1/"):
            return {"id": "t1", "slug": "emotet-campaign"}
        return {}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c8a",
            name="add_harfanglab_threat_note",
            arguments={"threat_id": "t1", "content": "First note."},
        )
    )
    assert not result.is_error
    assert "added" in result.content.lower()
    assert recorder.calls[-1]["method"] == "post"


@pytest.mark.asyncio
async def test_add_threat_note_surfaces_real_note_error(monkeypatch):
    def router(method, url):
        if url.endswith("/note/") and method == "get":
            # A real failure (500) must be surfaced, not treated as "no note".
            return ({"detail": "boom"}, 500, "HarfangLab error 500: server error")
        if url.endswith("/Threat/t1/"):
            return {"id": "t1", "slug": "emotet-campaign"}
        return {}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c8b",
            name="add_harfanglab_threat_note",
            arguments={"threat_id": "t1", "content": "Should not be written."},
        )
    )
    assert "500" in result.content
    # No write must be attempted after the failed probe.
    assert all(c["method"] == "get" for c in recorder.calls)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_isolate_endpoint_success(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"requested": ["ag-1"], "policy_not_allowed": []}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="isolate_harfanglab_endpoint",
            arguments={"agent_id": "ag-1"},
        )
    )
    assert not result.is_error
    assert "successfully requested" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/data/endpoint/Agent/ag-1/isolate/")


@pytest.mark.asyncio
async def test_isolate_endpoint_policy_refusal(monkeypatch):
    executor, _ = _build(
        monkeypatch, lambda m, u: {"requested": [], "policy_not_allowed": ["ag-1"]}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="isolate_harfanglab_endpoint",
            arguments={"agent_id": "ag-1"},
        )
    )
    assert "policy does not allow" in result.content


@pytest.mark.asyncio
async def test_deisolate_endpoint_url(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"requested": ["ag-1"]})
    result = await executor.execute_tool(
        ToolCall(
            id="c11",
            name="deisolate_harfanglab_endpoint",
            arguments={"agent_id": "ag-1"},
        )
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"].endswith("/api/data/endpoint/Agent/ag-1/deisolate/")


@pytest.mark.asyncio
async def test_list_endpoints_formats_agents(monkeypatch):
    def router(method, url):
        return {
            "count": 1,
            "results": [
                {
                    "agentid": "ag-1",
                    "hostname": "WIN-01",
                    "domainname": "corp.local",
                    "osproducttype": "Windows 11 Pro",
                    "version": "4.5.0",
                    "status": "online",
                    "lastseen": "2026-07-10T08:00:00Z",
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c12",
            name="list_harfanglab_endpoints",
            arguments={"search": "WIN"},
        )
    )
    assert not result.is_error
    assert "WIN-01" in result.content
    assert "ag-1" in result.content
    assert recorder.calls[0]["params"]["search"] == "WIN"


# ---------------------------------------------------------------------------
# Telemetry hunting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_telemetry_maps_hash_by_length(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"count": 0, "results": []})
    sha256 = "ab" * 32
    result = await executor.execute_tool(
        ToolCall(
            id="c13",
            name="search_harfanglab_telemetry",
            arguments={"event_type": "processes", "file_hash": sha256},
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert call["url"].endswith("/api/data/telemetry/Processes/")
    assert call["params"]["hashes.sha256"] == sha256


@pytest.mark.asyncio
async def test_search_telemetry_rejects_invalid_hash(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"results": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c13a",
            name="search_harfanglab_telemetry",
            arguments={"event_type": "processes", "file_hash": "not-a-hash"},
        )
    )
    assert "invalid file_hash" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_search_telemetry_rejects_invalid_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"results": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c14",
            name="search_harfanglab_telemetry",
            arguments={"event_type": "registry"},
        )
    )
    assert "invalid event_type" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_hunt_process_hash_queries_both_sources(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"count": 0, "results": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c15",
            name="hunt_harfanglab_process_hash",
            arguments={"file_hash": "cd" * 32},
        )
    )
    assert not result.is_error
    urls = [c["url"] for c in recorder.calls]
    assert any(u.endswith("/api/data/investigation/hunting/Process/") for u in urls)
    assert any(u.endswith("/api/data/telemetry/Processes/") for u in urls)


@pytest.mark.asyncio
async def test_hunt_process_hash_rejects_invalid_hash(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"count": 0, "results": []})
    result = await executor.execute_tool(
        ToolCall(
            id="c15a",
            name="hunt_harfanglab_process_hash",
            arguments={"file_hash": "zz" * 16},
        )
    )
    assert "invalid file_hash" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# IOC management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_ioc_payload(monkeypatch):
    def router(method, url):
        if method == "get":
            return {"count": 0, "results": []}
        return {"id": "ioc-1"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c16",
            name="add_harfanglab_ioc",
            arguments={
                "value": "evil.example.com",
                "ioc_type": "domain_name",
                "source_id": "src-1",
                "comment": "C2 domain",
            },
        )
    )
    assert not result.is_error
    write = recorder.calls[-1]
    assert write["method"] == "post"
    assert write["url"].endswith("/api/data/threat_intelligence/IOCRule/")
    assert write["json"]["type"] == "domain_name"
    assert write["json"]["value"] == "evil.example.com"
    assert write["json"]["source_id"] == "src-1"
    assert write["json"]["hl_status"] == "enabled"


@pytest.mark.asyncio
async def test_add_ioc_duplicate_guard(monkeypatch):
    def router(method, url):
        if method == "get":
            return {"count": 1, "results": [{"value": "evil.example.com"}]}
        return {}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c17",
            name="add_harfanglab_ioc",
            arguments={
                "value": "evil.example.com",
                "ioc_type": "domain_name",
                "source_id": "src-1",
            },
        )
    )
    assert "already exists" in result.content
    assert all(c["method"] == "get" for c in recorder.calls)


@pytest.mark.asyncio
async def test_add_ioc_strips_whitespace_before_guard_and_post(monkeypatch):
    def router(method, url):
        if method == "get":
            return {"count": 0, "results": []}
        return {"id": "ioc-1"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c17a",
            name="add_harfanglab_ioc",
            arguments={
                "value": "  evil.example.com  ",
                "ioc_type": "domain_name",
                "source_id": " src-1 ",
            },
        )
    )
    assert not result.is_error
    guard = recorder.calls[0]
    # The duplicate guard must query with the normalized values...
    assert guard["params"] == {"source_id": "src-1", "search": "evil.example.com"}
    write = recorder.calls[-1]
    # ...and the POST body must carry the same normalized values.
    assert write["json"]["value"] == "evil.example.com"
    assert write["json"]["source_id"] == "src-1"


@pytest.mark.asyncio
async def test_add_ioc_rejects_invalid_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c18",
            name="add_harfanglab_ioc",
            arguments={"value": "x", "ioc_type": "registry", "source_id": "src-1"},
        )
    )
    assert "invalid ioc_type" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_delete_ioc_url(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="c19", name="delete_harfanglab_ioc", arguments={"ioc_id": "ioc-1"})
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "delete"
    assert call["url"].endswith("/api/data/threat_intelligence/IOCRule/ioc-1/")


# ---------------------------------------------------------------------------
# Investigation jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_hunt_job_body_and_job_id(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [{"id": "job-1"}])
    result = await executor.execute_tool(
        ToolCall(
            id="c20",
            name="run_harfanglab_hunt_job",
            arguments={"agent_id": "ag-1", "job_type": "processes"},
        )
    )
    assert not result.is_error
    assert "job-1" in result.content
    call = recorder.calls[-1]
    assert call["url"].endswith("/api/data/Job/")
    assert call["json"]["targets"] == {"agents": ["ag-1"]}
    assert call["json"]["actions"] == [{"value": "getProcessList", "params": {}}]


@pytest.mark.asyncio
async def test_run_hunt_job_strips_agent_id(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [{"id": "job-1"}])
    result = await executor.execute_tool(
        ToolCall(
            id="c20a",
            name="run_harfanglab_hunt_job",
            arguments={"agent_id": " ag-1 ", "job_type": "processes"},
        )
    )
    assert not result.is_error
    assert recorder.calls[-1]["json"]["targets"] == {"agents": ["ag-1"]}


@pytest.mark.asyncio
async def test_isolate_endpoint_strips_agent_id(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"requested": ["ag-1"], "policy_not_allowed": []}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c9a",
            name="isolate_harfanglab_endpoint",
            arguments={"agent_id": " ag-1 "},
        )
    )
    # The stripped ID must match the API's requested list and build the URL.
    assert "successfully requested" in result.content
    assert recorder.calls[-1]["url"].endswith("/api/data/endpoint/Agent/ag-1/isolate/")


@pytest.mark.asyncio
async def test_run_hunt_job_rejects_invalid_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [{"id": "job-1"}])
    result = await executor.execute_tool(
        ToolCall(
            id="c21",
            name="run_harfanglab_hunt_job",
            arguments={"agent_id": "ag-1", "job_type": "ramdump"},
        )
    )
    assert "invalid job_type" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_job_status_computes_finished(monkeypatch):
    executor, _ = _build(
        monkeypatch,
        lambda m, u: {
            "instance": 1,
            "done": 1,
            "error": 0,
            "canceled": 0,
            "waiting": 0,
            "running": 0,
            "creationtime": "2026-07-10T08:00:00.000Z",
        },
    )
    result = await executor.execute_tool(
        ToolCall(id="c22", name="get_harfanglab_job_status", arguments={"job_id": "job-1"})
    )
    assert not result.is_error
    assert "finished" in result.content


@pytest.mark.asyncio
async def test_job_status_error_takes_precedence_over_done(monkeypatch):
    executor, _ = _build(
        monkeypatch,
        lambda m, u: {
            "instance": 2,
            "done": 2,
            "error": 1,
            "canceled": 0,
            "waiting": 0,
            "running": 0,
            "creationtime": "2026-07-10T08:00:00.000Z",
        },
    )
    result = await executor.execute_tool(
        ToolCall(id="c22a", name="get_harfanglab_job_status", arguments={"job_id": "job-1"})
    )
    assert not result.is_error
    assert "job-1: error" in result.content


@pytest.mark.asyncio
async def test_job_status_not_found_body_is_not_finished(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: {"detail": "Not found."})
    result = await executor.execute_tool(
        ToolCall(id="c22b", name="get_harfanglab_job_status", arguments={"job_id": "job-x"})
    )
    assert "not found" in result.content.lower()
    assert "finished" not in result.content.lower()


@pytest.mark.asyncio
async def test_job_results_uses_hunting_endpoint(monkeypatch):
    def router(method, url):
        return {
            "count": 1,
            "results": [
                {
                    "process_name": "powershell.exe",
                    "username": "corp\\bob",
                    "agent": {"hostname": "WIN-01"},
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c23",
            name="get_harfanglab_job_results",
            arguments={"job_id": "job-1", "job_type": "processes"},
        )
    )
    assert not result.is_error
    assert "powershell.exe" in result.content
    call = recorder.calls[-1]
    assert call["url"].endswith("/api/data/investigation/hunting/Process/")
    assert call["params"]["job_id"] == "job-1"
