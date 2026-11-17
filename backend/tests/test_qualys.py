"""Tests for the Qualys VMDR integration provider.

Covers tool introspection (incl. the 2-part catalog introspection dummy), the
HTTP Basic auth + mandatory X-Requested-With header helpers and base-URL
normalisation, the XML-parsing host-detection listing (nested DETECTION parsing
+ severity sort), KnowledgeBase QID detail (CVE/CVSS extraction + HTML
stripping), the SIMPLE_RETURN error-envelope surfacing, scan-results JSON
parsing, and the launch/manage scan tools (reference parsing + action guard).
Every API call routes through the shared ``safe_request`` helper, which is
monkeypatched here so no network is touched.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from app.integrations.providers import qualys as ql
from app.integrations.providers.qualys import (
    build_qualys_headers,
    build_tools,
    qualys_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://qualysapi.qualys.com||svc-user||s3cret-pass"


class _StubResponse:
    """Minimal httpx.Response stand-in exposing ``.text`` (XML) and ``.json()``."""

    def __init__(self, payload: Any):
        self._payload = payload

    @property
    def text(self) -> str:
        return self._payload if isinstance(self._payload, str) else ""

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
    monkeypatch.setattr(ql, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Sample Qualys XML payloads
# ---------------------------------------------------------------------------

_HOST_DETECTION_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<!DOCTYPE HOST_LIST_VM_DETECTION_OUTPUT SYSTEM "https://qualysapi.qualys.com/x.dtd">
<HOST_LIST_VM_DETECTION_OUTPUT><RESPONSE>
  <HOST_LIST>
    <HOST>
      <ID>524323</ID><IP>10.0.0.5</IP><DNS>web-01</DNS>
      <OS><![CDATA[Linux 2.6]]></OS>
      <DETECTION_LIST>
        <DETECTION><QID>38170</QID><TYPE>Confirmed</TYPE><SEVERITY>2</SEVERITY>
          <PORT>3389</PORT><PROTOCOL>tcp</PROTOCOL><STATUS>Active</STATUS>
          <LAST_FOUND_DATETIME>2026-01-02T03:04:05Z</LAST_FOUND_DATETIME></DETECTION>
        <DETECTION><QID>86002</QID><TYPE>Confirmed</TYPE><SEVERITY>5</SEVERITY>
          <STATUS>New</STATUS></DETECTION>
      </DETECTION_LIST>
    </HOST>
  </HOST_LIST>
</RESPONSE></HOST_LIST_VM_DETECTION_OUTPUT>"""

_KB_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<KNOWLEDGE_BASE_VULN_LIST_OUTPUT><RESPONSE>
  <VULN_LIST>
    <VULN>
      <QID>86002</QID><VULN_TYPE>Confirmed Vulnerability</VULN_TYPE>
      <SEVERITY_LEVEL>5</SEVERITY_LEVEL><TITLE><![CDATA[Apache HTTP RCE]]></TITLE>
      <CATEGORY>Web server</CATEGORY><PATCHABLE>1</PATCHABLE>
      <PUBLISHED_DATETIME>2025-06-01T00:00:00Z</PUBLISHED_DATETIME>
      <CVSS><BASE>7.5</BASE></CVSS>
      <CVSS_V3><BASE>9.8</BASE></CVSS_V3>
      <CVE_LIST>
        <CVE><ID>CVE-2021-1234</ID><URL>http://example/cve</URL></CVE>
        <CVE><ID>CVE-2021-9999</ID></CVE>
      </CVE_LIST>
      <SOLUTION><![CDATA[<P>Upgrade Apache to the latest version.</P>]]></SOLUTION>
    </VULN>
  </VULN_LIST>
</RESPONSE></KNOWLEDGE_BASE_VULN_LIST_OUTPUT>"""

_LAUNCH_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<SIMPLE_RETURN><RESPONSE>
  <DATETIME>2026-01-01T00:00:00Z</DATETIME>
  <TEXT>New vm scan launched</TEXT>
  <ITEM_LIST>
    <ITEM><KEY>ID</KEY><VALUE>9876543</VALUE></ITEM>
    <ITEM><KEY>REFERENCE</KEY><VALUE>scan/1700000000.12345</VALUE></ITEM>
  </ITEM_LIST>
</RESPONSE></SIMPLE_RETURN>"""

_ERROR_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<SIMPLE_RETURN><RESPONSE>
  <DATETIME>2026-01-01T00:00:00Z</DATETIME>
  <CODE>1905</CODE>
  <TEXT>IP(s) not in subscription</TEXT>
</RESPONSE></SIMPLE_RETURN>"""


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(
        token="https://dummy.example.com||__dummy__||__dummy__", executor=executor
    )
    names = {t.name for t in tools}
    assert "list_qualys_host_detections" in names
    assert "get_qualys_vulnerability" in names
    assert "list_qualys_hosts" in names
    assert "get_qualys_scan_results" in names
    assert "launch_qualys_scan" in names
    assert "manage_qualys_scan" in names
    assert len(tools) >= 12


def test_build_tools_works_with_two_part_introspection_dummy():
    # The catalog introspects compound providers with a 2-part url dummy
    # (``https://dummy.example.com||__dummy__``); the builder must still return
    # the full tool list so the catalog shows every Qualys tool.
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    assert len(tools) >= 12


def test_build_tools_empty_without_username():
    executor = CallableToolExecutor()
    # base URL only, no username/password -> no tools
    assert build_tools(token="https://qualysapi.qualys.com||", executor=executor) == []


def test_headers_and_base_url_helpers():
    headers = build_qualys_headers("alice", "s3cret", "XTM One")
    expected = base64.b64encode(b"alice:s3cret").decode("ascii")
    assert headers["Authorization"] == f"Basic {expected}"
    # The mandatory X-Requested-With header must always be present.
    assert headers["X-Requested-With"] == "XTM One"
    assert build_qualys_headers("u", "p")["X-Requested-With"]  # non-empty default
    # Whitespace pasted around the credentials is stripped: a trailing newline
    # would otherwise produce a Basic auth header that always fails.
    padded = build_qualys_headers("  alice\n", " s3cret\n")
    assert padded["Authorization"] == f"Basic {expected}"
    # The X-Requested-With value (the admin-configurable branding name) is coerced
    # to ASCII so a non-latin-1 brand can't make httpx reject the header: an ASCII
    # name is kept, a partly-non-ASCII name keeps its ASCII part, and an all-non-
    # ASCII name falls back to the fixed default.
    assert build_qualys_headers("u", "p", "Acme Sec")["X-Requested-With"] == "Acme Sec"
    assert build_qualys_headers("u", "p", "Filigr\u0144 \u2122")["X-Requested-With"] == "Filigr"
    assert build_qualys_headers("u", "p", "\u4f60\u597d")["X-Requested-With"] == "XTM One"
    # base URL default + trailing-slash normalisation + whitespace stripping
    assert qualys_api_base("") == "https://qualysapi.qualys.com"
    assert qualys_api_base("   \n") == "https://qualysapi.qualys.com"
    assert (
        qualys_api_base("https://qualysapi.qg2.apps.qualys.com/")
        == "https://qualysapi.qg2.apps.qualys.com"
    )
    assert (
        qualys_api_base("  https://qualysapi.qg2.apps.qualys.com/ \n")
        == "https://qualysapi.qg2.apps.qualys.com"
    )


# ---------------------------------------------------------------------------
# Host detections (XML parsing list tool)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_host_detections_parses_and_sorts(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _HOST_DETECTION_XML)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_qualys_host_detections",
            arguments={"severities": "4,5", "ips": "10.0.0.5"},
        )
    )
    assert not result.is_error
    assert "10.0.0.5" in result.content
    assert "web-01" in result.content
    # Both nested detections parsed; critical (5) sorts above low (2)
    assert "QID 86002" in result.content
    assert "QID 38170" in result.content
    assert result.content.index("QID 86002") < result.content.index("QID 38170")
    assert "Critical" in result.content
    # Filters + payload-reducing params forwarded to Qualys
    call = recorder.calls[0]
    assert call["params"]["severities"] == "4,5"
    assert call["params"]["ips"] == "10.0.0.5"
    assert call["params"]["show_results"] == "0"
    assert call["url"].endswith("/api/2.0/fo/asset/host/vm/detection/")


@pytest.mark.asyncio
async def test_api_error_envelope_surfaced(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: _ERROR_XML)
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="list_qualys_host_detections",
            arguments={"ips": "1.2.3.4"},
        )
    )
    # A SIMPLE_RETURN with a CODE is an API error and must be surfaced verbatim.
    assert "1905" in result.content
    assert "not in subscription" in result.content


# ---------------------------------------------------------------------------
# KnowledgeBase (CVE/CVSS extraction + HTML stripping)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_vulnerability_extracts_cves_and_cvss(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _KB_XML)
    result = await executor.execute_tool(
        ToolCall(id="c3", name="get_qualys_vulnerability", arguments={"qid": 86002})
    )
    assert not result.is_error
    assert "Apache HTTP RCE" in result.content
    assert "CVE-2021-1234" in result.content
    assert "CVE-2021-9999" in result.content
    assert "9.8" in result.content  # CVSS v3 base
    assert "7.5" in result.content  # CVSS v2 base
    assert "Critical" in result.content  # severity level 5
    # HTML tags stripped from the solution CDATA
    assert "Upgrade Apache to the latest version." in result.content
    assert "<P>" not in result.content
    # QID + details forwarded
    assert recorder.calls[0]["params"]["ids"] == "86002"
    assert recorder.calls[0]["params"]["details"] == "All"


# ---------------------------------------------------------------------------
# Scans - results (JSON), launch (reference parse), manage (action guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scan_results_parses_json(monkeypatch):
    payload = [
        {"launch_date": "2026-01-01", "scan_ref": "scan/1.2"},  # metadata row
        {
            "ip": "10.0.0.5",
            "dns": "web-01",
            "qid": "86002",
            "title": "Apache HTTP RCE",
            "severity": "5",
            "port": "443",
            "protocol": "tcp",
        },
    ]
    executor, recorder = _build(monkeypatch, lambda m, u: payload)
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="get_qualys_scan_results",
            arguments={"scan_ref": "scan/1.2"},
        )
    )
    assert not result.is_error
    assert "QID 86002" in result.content
    assert "10.0.0.5" in result.content
    # Only the finding row (with a qid) is shown, not the metadata row
    assert "Found 1 finding" in result.content
    assert recorder.calls[-1]["params"]["output_format"] == "json"
    assert recorder.calls[-1]["params"]["scan_ref"] == "scan/1.2"
    # This call fetches JSON, so it overrides the shared Accept: application/xml.
    assert recorder.calls[-1]["headers"]["Accept"] == "application/json"


@pytest.mark.asyncio
async def test_launch_scan_parses_reference(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _LAUNCH_XML)
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="launch_qualys_scan",
            arguments={"option_id": 12, "ip": "10.0.0.5", "scan_title": "Ad-hoc"},
        )
    )
    assert not result.is_error
    assert "scan/1700000000.12345" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/2.0/fo/scan/")
    # Launch params are sent as form data, not query params
    assert call["data"]["action"] == "launch"
    assert call["data"]["option_id"] == 12
    assert call["data"]["ip"] == "10.0.0.5"
    assert call["data"]["scan_title"] == "Ad-hoc"


@pytest.mark.asyncio
async def test_launch_scan_requires_option_and_target(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _LAUNCH_XML)
    no_option = await executor.execute_tool(
        ToolCall(id="c6", name="launch_qualys_scan", arguments={"ip": "10.0.0.5"})
    )
    assert "option" in no_option.content.lower()
    no_target = await executor.execute_tool(
        ToolCall(id="c7", name="launch_qualys_scan", arguments={"option_id": 12})
    )
    assert "target" in no_target.content.lower()
    # Neither invalid call should reach the API.
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_scan_rejects_invalid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _LAUNCH_XML)
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="manage_qualys_scan",
            arguments={"action": "nuke", "scan_ref": "scan/1.2"},
        )
    )
    assert "action must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_scan_handles_null_action(monkeypatch):
    # A null/missing action must not raise AttributeError; it should return the
    # helpful guard message and never reach the API.
    executor, recorder = _build(monkeypatch, lambda m, u: _LAUNCH_XML)
    result = await executor.execute_tool(
        ToolCall(
            id="c8b",
            name="manage_qualys_scan",
            arguments={"action": None, "scan_ref": "scan/1.2"},
        )
    )
    assert result.is_error
    assert "action must be one of" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_scan_valid_action(monkeypatch):
    sr = (
        '<?xml version="1.0" ?><SIMPLE_RETURN><RESPONSE>'
        "<TEXT>Scan paused</TEXT></RESPONSE></SIMPLE_RETURN>"
    )
    executor, recorder = _build(monkeypatch, lambda m, u: sr)
    result = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="manage_qualys_scan",
            arguments={"action": "pause", "scan_ref": "scan/1.2"},
        )
    )
    assert not result.is_error
    assert "paused" in result.content.lower()
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["data"]["action"] == "pause"
    assert call["data"]["scan_ref"] == "scan/1.2"
