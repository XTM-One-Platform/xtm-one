"""Tests for the urlscan.io integration provider.

Covers the credential chokepoint (header building + blank-key call-time
guard), tool registration and priorities, search query building + deep
pagination hints, scan submission (visibility validation, unlisted default,
scheme guard, tag cap), result formatting (verdicts, technologies,
in-progress 404 / deleted 410 handling), DOM truncation, and the Pro
structure-search entitlement message.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import urlscan
from app.integrations.providers.urlscan import (
    UrlscanIntegration,
    build_tools,
    build_urlscan_headers,
)
from app.services.integration_tools import _SafeIntegrationExecutor

_TOKEN = "us_test_key"
_UUID = "0e37e828-a9d9-45c0-ac50-1ca579b86c72"


class _FakeResp:
    def __init__(
        self,
        status_code: int,
        payload: dict | None = None,
        text: str = "",
    ):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


def _build(token: str = _TOKEN):
    executor = _SafeIntegrationExecutor()
    tools = build_tools(token, executor)
    return executor._handlers, tools


def _capture(
    monkeypatch,
    payload: dict | None = None,
    status: int = 200,
    text: str = "",
) -> dict:
    """Patch safe_request to capture call args and return a canned response."""
    captured: dict = {}

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured.update(kwargs)
        return _FakeResp(status, payload or {}, text=text), None

    monkeypatch.setattr(urlscan, "safe_request", _fake_safe_request)
    return captured


# ---------------------------------------------------------------------------
# Credential chokepoint
# ---------------------------------------------------------------------------


def test_headers_strip_whitespace_and_use_api_key_header():
    headers = build_urlscan_headers("  us_key \n")
    assert headers["API-Key"] == "us_key"
    assert headers["Accept"] == "application/json"


def test_provider_class_contract():
    integ = UrlscanIntegration()
    assert integ.provider_name == "urlscan"
    assert integ.auth_types == ["api_key"]
    assert integ.get_oauth_defaults() is None
    # The icon key must exist in the frontend ICON_MAP (constants.ts) or the
    # UI silently falls back to the generic Bot icon.
    assert integ.icon == "SearchCheck"


@pytest.mark.asyncio
async def test_validate_connection_rejects_blank_key(monkeypatch):
    """GET /user/quotas/ answers 200 anonymously, so a blank key must be
    rejected BEFORE the request or it would validate as connected."""

    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for a blank key")

    monkeypatch.setattr(urlscan, "safe_request", _must_not_call)
    integ = UrlscanIntegration()
    assert await integ.validate_connection("") is False
    assert await integ.validate_connection("   ") is False
    assert await integ.get_user_info("  ") == {}


@pytest.mark.asyncio
async def test_validate_connection_maps_error_to_false(monkeypatch):
    async def _fake_400(client, method, url, provider, action, **kwargs):
        assert kwargs["headers"]["API-Key"] == _TOKEN
        return None, "urlscan.io error: 400"

    monkeypatch.setattr(urlscan, "safe_request", _fake_400)
    integ = UrlscanIntegration()
    assert await integ.validate_connection(_TOKEN) is False
    assert await integ.get_user_info(_TOKEN) == {}

    async def _fake_200(client, method, url, provider, action, **kwargs):
        return _FakeResp(200, {"limits": {}}), None

    monkeypatch.setattr(urlscan, "safe_request", _fake_200)
    assert await integ.validate_connection(_TOKEN) is True
    assert await integ.get_user_info(_TOKEN) == {"limits": {}}


@pytest.mark.asyncio
async def test_blank_token_surfaces_call_time_error(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run without credentials")

    monkeypatch.setattr(urlscan, "safe_request", _must_not_call)
    handlers, tools = _build("   ")
    # Tools still build (the catalog introspects with a dummy token) …
    assert tools
    # … but every call reports the missing key instead of hitting the API.
    result = await handlers["search_urlscan_scans"](query="domain:example.com")
    assert "credentials are not configured" in result
    result = await handlers["submit_urlscan_scan"](url="https://example.com")
    assert "credentials are not configured" in result


def test_all_tools_registered_with_priorities():
    handlers, tools = _build()
    names = {t.name for t in tools}
    assert names == set(handlers)
    assert names == {
        "search_urlscan_scans",
        "submit_urlscan_scan",
        "get_urlscan_result",
        "get_urlscan_dom",
        "find_urlscan_similar_scans",
        "get_urlscan_quotas",
    }
    by_name = {t.name: t for t in tools}
    # Reads sort before the write (scan submission) and the status tool.
    assert by_name["search_urlscan_scans"].priority == 0
    assert by_name["get_urlscan_result"].priority == 0
    assert by_name["submit_urlscan_scan"].priority == 1
    assert by_name["get_urlscan_quotas"].priority == 2


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


_SEARCH_PAYLOAD = {
    "results": [
        {
            "_id": _UUID,
            "sort": [1700000000000, _UUID],
            "task": {
                "time": "2026-07-01T10:00:00.000Z",
                "visibility": "public",
                "url": "https://phish.example.net/login",
                "tags": ["phishing"],
            },
            "page": {
                "url": "https://phish.example.net/login",
                "domain": "phish.example.net",
                "ip": "203.0.113.5",
                "asn": "AS64500",
                "country": "DE",
                "server": "nginx",
            },
            "stats": {"requests": 42, "uniqIPs": 7},
        }
    ],
    "total": 12000,
    "has_more": True,
}


@pytest.mark.asyncio
async def test_search_builds_query_and_paginates(monkeypatch):
    captured = _capture(monkeypatch, _SEARCH_PAYLOAD)
    handlers, _ = _build()
    result = await handlers["search_urlscan_scans"](
        query="domain:example.net AND date:>now-7d", size=20
    )

    assert captured["url"].endswith("/api/v1/search/")
    assert captured["headers"]["API-Key"] == _TOKEN
    assert captured["params"]["q"] == "domain:example.net AND date:>now-7d"
    assert captured["params"]["size"] == 20
    assert _UUID in result
    assert "phish.example.net" in result
    assert "203.0.113.5" in result
    # Deep-pagination hint uses the last result's sort value.
    assert f'search_after="1700000000000,{_UUID}"' in result


@pytest.mark.asyncio
async def test_search_clamps_size_to_context_safe_cap(monkeypatch):
    """The API accepts up to 10k results per page, but the tool caps the
    page size at 100 to keep LLM context sane (deep-page with search_after)."""
    captured = _capture(monkeypatch, _SEARCH_PAYLOAD)
    handlers, _ = _build()
    await handlers["search_urlscan_scans"](query="domain:example.net", size=5000)
    assert captured["params"]["size"] == 100


@pytest.mark.asyncio
async def test_search_requires_a_query(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run without a query")

    monkeypatch.setattr(urlscan, "safe_request", _must_not_call)
    handlers, _ = _build()
    assert "Error" in await handlers["search_urlscan_scans"](query="  ")


# ---------------------------------------------------------------------------
# Scan submission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_defaults_to_unlisted(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "message": "Submission successful",
            "uuid": _UUID,
            "result": f"https://urlscan.io/result/{_UUID}/",
            "url": "https://example.com/",
            "visibility": "unlisted",
        },
    )
    handlers, _ = _build()
    result = await handlers["submit_urlscan_scan"](url="https://example.com/")

    assert captured["method"] == "post"
    assert captured["url"].endswith("/api/v1/scan/")
    assert captured["json"] == {"url": "https://example.com/", "visibility": "unlisted"}
    assert _UUID in result
    assert "10-30 seconds" in result


@pytest.mark.asyncio
async def test_submit_validates_input_before_any_request(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for invalid input")

    monkeypatch.setattr(urlscan, "safe_request", _must_not_call)
    handlers, _ = _build()
    submit = handlers["submit_urlscan_scan"]

    assert "Error" in await submit(url="")
    assert "scheme" in await submit(url="ftp://example.com/file")
    assert "visibility" in await submit(url="https://example.com", visibility="secret")
    assert "10 tags" in await submit(
        url="https://example.com", tags=[f"t{i}" for i in range(11)]
    )
    assert "country" in await submit(url="https://example.com", country="Germany")


@pytest.mark.asyncio
async def test_submit_defaults_bare_hostnames_to_https(monkeypatch):
    captured = _capture(monkeypatch, {"uuid": _UUID})
    handlers, _ = _build()
    await handlers["submit_urlscan_scan"](url="example.com/login")
    assert captured["json"]["url"] == "https://example.com/login"


# ---------------------------------------------------------------------------
# Result retrieval
# ---------------------------------------------------------------------------


_RESULT_PAYLOAD = {
    "task": {
        "uuid": _UUID,
        "time": "2026-07-01T10:00:00.000Z",
        "visibility": "unlisted",
        "url": "https://phish.example.net/login",
        "tags": ["phishing"],
        "reportURL": f"https://urlscan.io/result/{_UUID}/",
        "screenshotURL": f"https://urlscan.io/screenshots/{_UUID}.png",
        "domURL": f"https://urlscan.io/dom/{_UUID}/",
    },
    "page": {
        "url": "https://phish.example.net/login",
        "domain": "phish.example.net",
        "ip": "203.0.113.5",
        "asn": "AS64500",
        "asnname": "EXAMPLE-AS, DE",
        "country": "DE",
        "server": "nginx",
    },
    "stats": {
        "requests": 42,
        "secureRequests": 40,
        "securePercentage": 95,
        "malicious": 1,
        "adBlocked": 0,
    },
    "meta": {
        "processors": {
            "wappa": {
                "data": [
                    {"app": "Nginx", "categories": [{"name": "Web servers"}]},
                    {"app": "jQuery", "categories": [{"name": "JavaScript libraries"}]},
                ]
            }
        }
    },
    "lists": {
        "ips": ["203.0.113.5", "198.51.100.7"],
        "domains": ["phish.example.net", "cdn.example.org"],
        "hashes": ["a" * 64],
    },
    "data": {"requests": [{}] * 42},
    "verdicts": {
        "overall": {
            "score": 100,
            "malicious": True,
            "categories": ["phishing"],
            "brands": [{"name": "PayPal"}],
        },
        "community": {"votesMalicious": 3, "votesBenign": 0, "votesTotal": 3},
        "engines": {"maliciousTotal": 2, "enginesTotal": 10},
    },
}


@pytest.mark.asyncio
async def test_result_formats_verdicts_and_technologies(monkeypatch):
    captured = _capture(monkeypatch, _RESULT_PAYLOAD)
    handlers, _ = _build()
    result = await handlers["get_urlscan_result"](scan_id=_UUID)

    assert captured["url"].endswith(f"/api/v1/result/{_UUID}/")
    assert "score 100/100" in result
    assert "malicious: True" in result
    assert "PayPal" in result
    assert "Nginx (Web servers)" in result
    assert "phish.example.net" in result
    assert f"hash:{'a' * 64}" in result  # pivot hint
    assert f"/screenshots/{_UUID}.png" in result
    assert f"/dom/{_UUID}/" in result


@pytest.mark.asyncio
async def test_result_reports_in_progress_and_deleted(monkeypatch):
    handlers, _ = _build()

    async def _fake_404(client, method, url, provider, action, **kwargs):
        return _FakeResp(404), "urlscan.io error: 404"

    monkeypatch.setattr(urlscan, "safe_request", _fake_404)
    result = await handlers["get_urlscan_result"](scan_id=_UUID)
    assert "not finished yet" in result

    async def _fake_410(client, method, url, provider, action, **kwargs):
        return _FakeResp(410), "urlscan.io error: 410"

    monkeypatch.setattr(urlscan, "safe_request", _fake_410)
    result = await handlers["get_urlscan_result"](scan_id=_UUID)
    assert "deleted" in result


@pytest.mark.asyncio
async def test_result_rejects_non_uuid_ids(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for an invalid UUID")

    monkeypatch.setattr(urlscan, "safe_request", _must_not_call)
    handlers, _ = _build()
    for bad in ("", "not-a-uuid", f"{_UUID}/../x"):
        assert "Error" in await handlers["get_urlscan_result"](scan_id=bad)
        assert "Error" in await handlers["get_urlscan_dom"](scan_id=bad)
        assert "Error" in await handlers["find_urlscan_similar_scans"](scan_id=bad)


# ---------------------------------------------------------------------------
# DOM snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dom_truncates_to_max_chars(monkeypatch):
    _capture(monkeypatch, text="<html>" + "x" * 10_000)
    handlers, _ = _build()
    result = await handlers["get_urlscan_dom"](scan_id=_UUID, max_chars=500)
    assert "showing first 500" in result
    assert "truncated" in result


# ---------------------------------------------------------------------------
# Structure search (urlscan Pro)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_similar_scans_explains_missing_pro_entitlement(monkeypatch):
    async def _fake_403(client, method, url, provider, action, **kwargs):
        return _FakeResp(403), "urlscan.io error: 403"

    monkeypatch.setattr(urlscan, "safe_request", _fake_403)
    handlers, _ = _build()
    result = await handlers["find_urlscan_similar_scans"](scan_id=_UUID)
    assert "urlscan Pro" in result


@pytest.mark.asyncio
async def test_similar_scans_targets_pro_endpoint(monkeypatch):
    captured = _capture(monkeypatch, {"results": [], "total": 0})
    handlers, _ = _build()
    result = await handlers["find_urlscan_similar_scans"](
        scan_id=_UUID, query="date:>now-30d"
    )
    assert captured["url"].endswith(f"/api/v1/pro/result/{_UUID}/similar/")
    assert captured["params"]["q"] == "date:>now-30d"
    assert "No structurally similar scans" in result


# ---------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quotas_formats_per_action_windows(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "limits": {
                "search": {
                    "minute": {"limit": 120, "used": 3},
                    "hour": {"limit": 1000, "used": 10},
                    "day": {"limit": 1000, "used": 10},
                },
                "private": {"day": {"limit": 50, "used": 0}},
            }
        },
    )
    handlers, _ = _build()
    result = await handlers["get_urlscan_quotas"]()
    assert captured["url"].endswith("/user/quotas/")
    assert "search: minute: 3/120" in result
    assert "private: day: 0/50" in result
