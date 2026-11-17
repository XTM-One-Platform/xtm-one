"""Tests for the ANY.RUN integration provider.

Covers the credential chokepoint (Authorization header + API-KEY prefix
canonicalisation + blank-key guard), tool registration + priorities, sandbox
report formatting, the URL/file detonation request bodies (JSON + multipart
with lowercase form booleans), strict task-UUID validation, YARA search id +
results parsing, TI Lookup query building + response formatting, and the IOC
reputation filter + output cap.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import anyrun
from app.integrations.providers.anyrun import (
    AnyRunIntegration,
    build_anyrun_headers,
    build_tools,
)
from app.services.integration_tools import _SafeIntegrationExecutor
from tests.integration_file_fakes import HOSTILE_FILE_NAMES, assert_name_only_inside, fenced_and_open

_TOKEN = "anyrun_test_key"
_UUID = "0e1f2a3b-4c5d-6e7f-8a9b-0c1d2e3f4a5b"


class _FakeResp:
    def __init__(self, status_code: int, payload: dict | list | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> dict | list:
        return self._payload


def _build(token: str = _TOKEN):
    executor = _SafeIntegrationExecutor()
    tools = build_tools(token, executor)
    return executor._handlers, tools


def _capture(monkeypatch, payload: dict | list | None = None, status: int = 200) -> dict:
    """Patch safe_request to capture call args and return a canned response."""
    captured: dict = {}

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured.update(kwargs)
        return _FakeResp(status, payload if payload is not None else {}), None

    monkeypatch.setattr(anyrun, "safe_request", _fake_safe_request)
    return captured


# ---------------------------------------------------------------------------
# Credential chokepoint
# ---------------------------------------------------------------------------


def test_headers_normalize_prefix_and_strip_whitespace():
    headers = build_anyrun_headers("  raw_key \n")
    assert headers["Authorization"] == "API-KEY raw_key"
    assert headers["Accept"] == "application/json"


def test_headers_canonicalize_existing_prefix():
    assert build_anyrun_headers("API-KEY abc")["Authorization"] == "API-KEY abc"
    # A pasted prefix in any casing is canonicalised to the uppercase scheme.
    assert build_anyrun_headers("api-key abc")["Authorization"] == "API-KEY abc"
    assert build_anyrun_headers("Api-Key  abc ")["Authorization"] == "API-KEY abc"
    # The legacy Basic scheme is passed through untouched.
    assert build_anyrun_headers("Basic dXNlcg==")["Authorization"] == "Basic dXNlcg=="


def test_headers_blank_key_stays_blank():
    assert build_anyrun_headers("")["Authorization"] == ""
    assert build_anyrun_headers("   ")["Authorization"] == ""
    assert build_anyrun_headers("api-key   ")["Authorization"] == ""


def test_blank_token_builds_no_tools():
    handlers, tools = _build("   ")
    assert tools == []
    assert handlers == {}


def test_prefix_only_token_builds_no_tools():
    # A bare "api-key" paste normalises to an empty credential - the provider
    # must not register tools that are guaranteed to fail unauthenticated.
    for token in ("api-key", "API-KEY   ", "Api-Key"):
        handlers, tools = _build(token)
        assert tools == []
        assert handlers == {}


def test_provider_class_contract():
    integ = AnyRunIntegration()
    assert integ.provider_name == "anyrun"
    assert integ.display_name == "ANY.RUN"
    assert integ.auth_types == ["api_key"]
    # Pinned: the icon key must exist in the frontend ICON_MAP (constants.ts)
    # or the UI silently falls back to the generic Bot icon.
    assert integ.icon == "FlaskConical"
    assert integ.get_oauth_defaults() is None


@pytest.mark.asyncio
async def test_validate_connection_uses_safe_request(monkeypatch):
    captured = _capture(monkeypatch, {"data": {"limits": {}}})
    integ = AnyRunIntegration()
    assert await integ.validate_connection(_TOKEN) is True
    assert captured["url"].endswith("/user")
    assert captured["headers"]["Authorization"] == f"API-KEY {_TOKEN}"


@pytest.mark.asyncio
async def test_validate_connection_returns_false_on_error(monkeypatch):
    async def _fail(*args, **kwargs):
        return None, "ANY.RUN error: 401 unauthorized"

    monkeypatch.setattr(anyrun, "safe_request", _fail)
    integ = AnyRunIntegration()
    assert await integ.validate_connection(_TOKEN) is False


@pytest.mark.asyncio
async def test_connection_methods_short_circuit_blank_credential(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for a blank credential")

    monkeypatch.setattr(anyrun, "safe_request", _must_not_call)
    integ = AnyRunIntegration()
    assert await integ.validate_connection("   ") is False
    assert await integ.validate_connection("api-key") is False
    assert await integ.get_user_info("") == {}


@pytest.mark.asyncio
async def test_get_user_info_returns_dict_or_empty(monkeypatch):
    _capture(monkeypatch, {"data": {"name": "analyst"}})
    integ = AnyRunIntegration()
    assert await integ.get_user_info(_TOKEN) == {"data": {"name": "analyst"}}

    async def _fail(*args, **kwargs):
        return None, "ANY.RUN error: 403 forbidden"

    monkeypatch.setattr(anyrun, "safe_request", _fail)
    assert await integ.get_user_info(_TOKEN) == {}


def test_all_tools_registered_with_priorities():
    handlers, tools = _build()
    names = {t.name for t in tools}
    assert names == set(handlers)
    assert names == {
        "get_anyrun_status",
        "list_anyrun_analyses",
        "get_anyrun_analysis_report",
        "get_anyrun_analysis_iocs",
        "download_anyrun_analysis_file",
        "get_anyrun_environment",
        "lookup_anyrun_ti",
        "get_anyrun_yara_results",
        "submit_anyrun_url_analysis",
        "submit_anyrun_file_analysis",
        "search_anyrun_yara",
        "add_anyrun_analysis_time",
        "stop_anyrun_analysis",
        "delete_anyrun_analysis",
    }
    by_name = {t.name: t for t in tools}
    # Reads (0) < writes (1) < destructive (2).
    assert by_name["get_anyrun_analysis_report"].priority == 0
    assert by_name["lookup_anyrun_ti"].priority == 0
    assert by_name["submit_anyrun_url_analysis"].priority == 1
    assert by_name["submit_anyrun_file_analysis"].priority == 1
    assert by_name["delete_anyrun_analysis"].priority == 2


# ---------------------------------------------------------------------------
# Sandbox report formatting + UUID validation
# ---------------------------------------------------------------------------


_REPORT_PAYLOAD = {
    "data": {
        "analysis": {
            "uuid": _UUID,
            "scores": {"verdict": {"threatLevel": 2, "threatLevelText": "Malicious activity"}},
            "tags": [{"tag": "trojan"}, {"tag": "stealer"}],
            "content": {
                "mainObject": {
                    "type": "file",
                    "name": "invoice.exe",
                    "hashes": {"md5": "abc", "sha256": "def"},
                }
            },
        },
        "mitre": [{"id": "T1071", "name": "Application Layer Protocol"}],
        "processes": [{"scores": {"verdict": {"threatLevel": 2}}}, {}],
    }
}


@pytest.mark.asyncio
async def test_get_report_formats_verdict_and_mitre(monkeypatch):
    captured = _capture(monkeypatch, _REPORT_PAYLOAD)
    handlers, _ = _build()
    result = await handlers["get_anyrun_analysis_report"](task_uuid=_UUID)

    assert captured["url"].endswith(f"/analysis/{_UUID}")
    assert captured["headers"]["Authorization"] == "API-KEY anyrun_test_key"
    assert "Malicious activity" in result
    assert "T1071" in result
    assert "trojan" in result
    assert "invoice.exe" in result


@pytest.mark.asyncio
async def test_report_tolerates_malformed_shapes(monkeypatch):
    # Non-dict hashes / scores / verdict and string threat levels must not
    # raise, and a string "0" process threat level must not count as flagged.
    payload = {
        "data": {
            "analysis": {
                "uuid": _UUID,
                "scores": {"verdict": {"threatLevel": "2"}},
                "content": {"mainObject": {"type": "file", "hashes": "corrupt"}},
            },
            "processes": [
                {"scores": {"verdict": {"threatLevel": "0"}}},
                {"scores": {"verdict": {"threatLevel": "2"}}},
                {"scores": "corrupt"},
                "not-a-dict",
            ],
        }
    }
    _capture(monkeypatch, payload)
    handlers, _ = _build()
    result = await handlers["get_anyrun_analysis_report"](task_uuid=_UUID)

    assert "[malicious]" in result
    assert "Hashes:" not in result
    assert "4 observed (1 flagged)" in result


@pytest.mark.asyncio
async def test_uuid_validation_blocks_requests(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for an invalid UUID")

    monkeypatch.setattr(anyrun, "safe_request", _must_not_call)
    handlers, _ = _build()
    for bad in ("", "not-a-uuid", "../../etc", "123"):
        assert "Error" in await handlers["get_anyrun_analysis_report"](task_uuid=bad)
        assert "Error" in await handlers["delete_anyrun_analysis"](task_uuid=bad)
        assert "Error" in await handlers["stop_anyrun_analysis"](task_uuid=bad)
        assert "Error" in await handlers["get_anyrun_yara_results"](search_uuid=bad)


# ---------------------------------------------------------------------------
# Detonation submission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_url_posts_expected_body(monkeypatch):
    captured = _capture(monkeypatch, {"data": {"taskid": _UUID}})
    handlers, _ = _build()
    result = await handlers["submit_anyrun_url_analysis"](url="https://evil.example.com")

    assert captured["method"] == "post"
    assert captured["url"].endswith("/analysis")
    body = captured["json"]
    assert body["obj_type"] == "url"
    assert body["obj_url"] == "https://evil.example.com"
    assert body["env_os"] == "windows"
    assert _UUID in result
    assert "app.any.run/tasks/" in result


@pytest.mark.asyncio
async def test_submit_url_rejects_non_http(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for a non-http URL")

    monkeypatch.setattr(anyrun, "safe_request", _must_not_call)
    handlers, _ = _build()
    assert "Error" in await handlers["submit_anyrun_url_analysis"](url="ftp://x")
    assert "Error" in await handlers["submit_anyrun_url_analysis"](url="")


@pytest.mark.asyncio
async def test_submit_url_clamps_timeout(monkeypatch):
    captured = _capture(monkeypatch, {"data": {"taskid": _UUID}})
    handlers, _ = _build()
    await handlers["submit_anyrun_url_analysis"](url="https://a.com", opt_timeout=9000)
    assert captured["json"]["opt_timeout"] == 660


# ---------------------------------------------------------------------------
# IOC reputation filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iocs_filter_by_reputation(monkeypatch):
    payload = [
        {"category": "domain", "ioc": "good.com", "reputation": 0},
        {"category": "domain", "ioc": "bad.com", "reputation": 2},
    ]
    _capture(monkeypatch, payload)
    handlers, _ = _build()
    # Default 'suspicious' drops the reputation-0 entry.
    result = await handlers["get_anyrun_analysis_iocs"](task_uuid=_UUID)
    assert "bad.com" in result
    assert "good.com" not in result
    # 'all' keeps both.
    result_all = await handlers["get_anyrun_analysis_iocs"](task_uuid=_UUID, reputation="all")
    assert "good.com" in result_all and "bad.com" in result_all


@pytest.mark.asyncio
async def test_iocs_output_is_capped(monkeypatch):
    payload = [
        {"category": "url", "ioc": f"http://x{i}.example", "reputation": 2}
        for i in range(150)
    ]
    _capture(monkeypatch, payload)
    handlers, _ = _build()
    result = await handlers["get_anyrun_analysis_iocs"](task_uuid=_UUID, reputation="all")
    assert "Found 150 IOCs" in result
    assert "http://x99.example" in result
    assert "http://x100.example" not in result
    assert "showing the first 100 of 150" in result


@pytest.mark.asyncio
async def test_iocs_rejects_bad_reputation(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for a bad reputation")

    monkeypatch.setattr(anyrun, "safe_request", _must_not_call)
    handlers, _ = _build()
    assert "Error" in await handlers["get_anyrun_analysis_iocs"](
        task_uuid=_UUID, reputation="bogus"
    )


# ---------------------------------------------------------------------------
# File detonation (multipart)
# ---------------------------------------------------------------------------


class _FakeFileContext:
    """Minimal FileContext stand-in resolving one known file id."""

    def __init__(self, file_id: str, data: bytes, filename: str, mime: str):
        self._file_id = file_id
        self._resolved = (data, filename, mime)

    async def resolve_upload(self, file_id: str):
        return self._resolved if file_id == self._file_id else None


@pytest.mark.asyncio
async def test_submit_file_sends_multipart_with_lowercase_booleans(monkeypatch):
    captured = _capture(monkeypatch, {"data": {"taskid": _UUID}})
    handlers, _ = _build()
    ctx = _FakeFileContext("f-1", b"MZbytes", "invoice.exe", "application/x-msdownload")
    result = await handlers["submit_anyrun_file_analysis"](
        file_id="f-1", file_context=ctx
    )

    assert captured["method"] == "post"
    assert captured["url"].endswith("/analysis")
    form = captured["data"]
    # The form API documents lowercase "true"/"false", not Python's str(True).
    assert form["obj_type"] == "file"
    assert form["opt_network_connect"] == "true"
    assert "obj_url" not in form
    assert captured["files"]["file"][0] == "invoice.exe"
    assert captured["files"]["file"][1] == b"MZbytes"
    inside, outside = fenced_and_open(result)
    assert outside.startswith(f"Submitted file 'f-1' (0.0 KB) to ANY.RUN for analysis.\nTask UUID: {_UUID}\n")
    assert "Name: invoice.exe" in inside.splitlines()


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_submit_file_names_the_file_only_inside_the_block(monkeypatch, hostile):
    """The stored name reaches the model only in the closing block,
    on the answer and on a refused submission's error (the action label)."""
    name = HOSTILE_FILE_NAMES[hostile]
    _capture(monkeypatch, {"data": {"taskid": _UUID}})
    handlers, _ = _build()
    ctx = _FakeFileContext("f-1", b"MZ", name, "application/octet-stream")
    result = await handlers["submit_anyrun_file_analysis"](file_id="f-1", file_context=ctx)
    outside = assert_name_only_inside(result, name)
    assert "file 'f-1'" in outside

    async def _refused(client, method, url, provider, action, **kwargs):
        return None, f"Error: {provider} access denied while trying to {action}."

    monkeypatch.setattr(anyrun, "safe_request", _refused)
    refused = await handlers["submit_anyrun_file_analysis"](file_id="f-1", file_context=ctx)
    assert refused == "Error: ANY.RUN access denied while trying to submit file 'f-1'."


@pytest.mark.asyncio
async def test_submit_file_rejects_unknown_file(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for an unresolved file")

    monkeypatch.setattr(anyrun, "safe_request", _must_not_call)
    handlers, _ = _build()
    ctx = _FakeFileContext("f-1", b"x", "a.bin", "application/octet-stream")
    assert "Error" in await handlers["submit_anyrun_file_analysis"](
        file_id="nope", file_context=ctx
    )
    assert "Error" in await handlers["submit_anyrun_file_analysis"](
        file_id="f-1", file_context=None
    )


# ---------------------------------------------------------------------------
# YARA search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yara_search_returns_top_level_query_id(monkeypatch):
    captured = _capture(monkeypatch, {"queryId": _UUID})
    handlers, _ = _build()
    result = await handlers["search_anyrun_yara"](yara_rule="rule X { condition: true }")

    assert captured["method"] == "post"
    assert captured["url"].endswith("/intelligence/yara-lookup/search")
    assert captured["json"] == {"query": "rule X { condition: true }"}
    assert _UUID in result


@pytest.mark.asyncio
async def test_yara_results_read_top_level_matches(monkeypatch):
    _capture(
        monkeypatch,
        {
            "searchInfo": {"status": "done"},
            "matches": [{"uuid": _UUID, "name": "sample.exe", "verdict": "Malicious"}],
        },
    )
    handlers, _ = _build()
    result = await handlers["get_anyrun_yara_results"](search_uuid=_UUID)
    assert "sample.exe" in result
    assert _UUID in result


@pytest.mark.asyncio
async def test_yara_results_distinguish_done_from_running(monkeypatch):
    _capture(monkeypatch, {"searchInfo": {"status": "done"}, "matches": []})
    handlers, _ = _build()
    result = await handlers["get_anyrun_yara_results"](search_uuid=_UUID)
    assert "no matching samples" in result

    _capture(monkeypatch, {"searchInfo": {"status": "processing"}, "matches": []})
    handlers, _ = _build()
    result = await handlers["get_anyrun_yara_results"](search_uuid=_UUID)
    assert "still" in result and "processing" in result


# ---------------------------------------------------------------------------
# TI Lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ti_lookup_builds_query_from_filters(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "summary": {"threatLevel": 2, "tags": ["phishing"]},
            "relatedDNS": [{"domainName": "evil.top"}],
            "destinationIP": [],
            "relatedURLs": [],
            "relatedFiles": [],
            "sourceTasks": [{"related": _UUID}],
        },
    )
    handlers, _ = _build()
    result = await handlers["lookup_anyrun_ti"](domain="evil.top", threat_name="Phishing")

    assert captured["method"] == "post"
    assert captured["url"].endswith("/intelligence/api/search")
    body = captured["json"]
    assert 'domainName:"evil.top"' in body["query"]
    assert 'threatName:"Phishing"' in body["query"]
    assert " AND " in body["query"]
    assert "startDate" in body and "endDate" in body
    assert "Malicious" in result
    assert "evil.top" in result
    assert _UUID in result


@pytest.mark.asyncio
async def test_ti_lookup_coerces_string_threat_level(monkeypatch):
    # A string threatLevel ("2") must produce the same text verdict as int 2.
    _capture(monkeypatch, {"summary": {"threatLevel": "2"}, "sourceTasks": [{"related": _UUID}]})
    handlers, _ = _build()
    result = await handlers["lookup_anyrun_ti"](domain="evil.top")
    assert "Malicious" in result
    assert "unknown" not in result


@pytest.mark.asyncio
async def test_ti_lookup_tolerates_malformed_shapes(monkeypatch):
    # summary as a list, relatedDNS as a dict, corrupt hashes: formatting must
    # not raise and must still render the counters line.
    _capture(
        monkeypatch,
        {
            "summary": ["corrupt"],
            "relatedDNS": {"corrupt": True},
            "destinationIP": [{"destinationIP": "198.51.100.1"}],
            "relatedFiles": [{"hashes": "corrupt", "fileName": "a.exe"}, "corrupt"],
            "sourceTasks": [{"related": _UUID}],
        },
    )
    handlers, _ = _build()
    result = await handlers["lookup_anyrun_ti"](domain="evil.top")
    assert "Related indicators: 2 files, 1 IPs, 0 domains, 0 URLs" in result
    assert "198.51.100.1" in result
    assert "a.exe" in result
    assert _UUID in result


@pytest.mark.asyncio
async def test_ti_lookup_prefers_raw_query(monkeypatch):
    captured = _capture(monkeypatch, {"summary": {}, "sourceTasks": []})
    handlers, _ = _build()
    await handlers["lookup_anyrun_ti"](query='md5:"deadbeef"', md5="ignored")
    assert captured["json"]["query"] == 'md5:"deadbeef"'


@pytest.mark.asyncio
async def test_ti_lookup_requires_a_filter(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run without any filter")

    monkeypatch.setattr(anyrun, "safe_request", _must_not_call)
    handlers, _ = _build()
    assert "Error" in await handlers["lookup_anyrun_ti"]()
