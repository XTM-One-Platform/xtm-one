"""Regression tests for the DomainTools integration provider.

Pins the HMAC-SHA256 request signing (the API key must never travel on the
wire), the in-body error envelope handling (DomainTools returns errors inside
HTTP 200 bodies, e.g. the Iris code 413 "more than 10000 matched"), the
path-segment guard on domain/IP lookups, the credential guard, and the
consolidated ``manage_domaintools_detect`` action validation.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from app.integrations.providers import domaintools
from app.integrations.providers.domaintools import (
    build_tools,
    sign_domaintools_params,
)
from app.services.integration_tools import _SafeIntegrationExecutor

# api_username||api_key compound token
_TOKEN = "dt-user||dt-key"


class _FakeResp:
    """Minimal JSON response (routes through safe_request)."""

    def __init__(self, payload: object, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers: dict = {}

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> object:
        return self._payload


class _FakeClient:
    """httpx.AsyncClient stand-in recording every dispatched request."""

    def __init__(self, payload: object, status_code: int = 200):
        self._payload = payload
        self._status_code = status_code
        self.requests: list[tuple[str, str, dict]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def request(self, method, url, **kwargs) -> _FakeResp:
        self.requests.append((str(method).upper(), str(url), kwargs))
        return _FakeResp(self._payload, self._status_code)


def _patch_client(monkeypatch, payload: object, status_code: int = 200) -> dict:
    holder: dict = {}

    def _factory(**kw):
        holder["client"] = _FakeClient(payload, status_code)
        return holder["client"]

    monkeypatch.setattr(domaintools.httpx, "AsyncClient", _factory)
    return holder


def _handlers(token: str = _TOKEN) -> dict:
    executor = _SafeIntegrationExecutor()
    build_tools(token, executor)
    return executor._handlers


# ---------------------------------------------------------------------------
# HMAC signing
# ---------------------------------------------------------------------------


def test_signing_produces_valid_hmac_and_strips_credentials():
    params = sign_domaintools_params("  dt-user ", " dt-key\n", "/v1/account/")
    assert params["api_username"] == "dt-user"
    assert "api_key" not in params
    expected = hmac.new(
        b"dt-key",
        f"dt-user{params['timestamp']}/v1/account/".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert params["signature"] == expected


@pytest.mark.asyncio
async def test_request_is_signed_and_key_never_on_the_wire(monkeypatch):
    holder = _patch_client(
        monkeypatch,
        {"response": {"domain": "example.com", "risk_score": 42, "components": []}},
    )
    handler = _handlers()["get_domaintools_risk_score"]
    result = await handler(domain="example.com")

    assert "risk score 42" in result
    method, url, kwargs = holder["client"].requests[0]
    assert method == "GET"
    assert url.endswith("/v1/risk/")
    params = kwargs.get("params") or {}
    assert params.get("api_username") == "dt-user"
    assert "signature" in params and "timestamp" in params
    # The API key itself must never be sent.
    assert "dt-key" not in str(params.values())
    assert "api_key" not in params


# ---------------------------------------------------------------------------
# In-body error envelope (errors riding on HTTP 200)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iris_in_body_413_error_is_surfaced(monkeypatch):
    _patch_client(
        monkeypatch,
        {
            "response": {
                "error": {
                    "code": 413,
                    "message": "More than 10000 matched - you may need to refine your query.",
                },
                "limit_exceeded": True,
            }
        },
    )
    handler = _handlers()["search_domaintools_iris"]
    result = await handler(nameserver_domain="markmonitor.zone")
    assert "Error" in result and "413" in result and "10000" in result


@pytest.mark.asyncio
async def test_detect_http_422_surfaces_field_messages(monkeypatch):
    """Real Detect validation failures ride an HTTP 422 (format_http_error path)."""
    _patch_client(
        monkeypatch,
        {
            "error": {
                "code": 422,
                "summary": "The given data was invalid.",
                "messages": {"term": ["The term field is required."]},
            }
        },
        status_code=422,
    )
    handler = _handlers()["manage_domaintools_detect"]
    result = await handler(action="create_monitor", term="abcd")
    assert "Error" in result and "term" in result


@pytest.mark.asyncio
async def test_detect_envelope_on_http_200_uses_summary_key(monkeypatch):
    """The Detect validation envelope carries 'summary' (not 'message');
    _body_error must still surface it if it ever rides on a 2xx."""
    _patch_client(
        monkeypatch,
        {
            "error": {
                "code": 422,
                "summary": "The given data was invalid.",
                "messages": {"term": ["The term field is required."]},
            }
        },
    )
    handler = _handlers()["manage_domaintools_detect"]
    result = await handler(action="create_monitor", term="abcd")
    assert "Error" in result and "422" in result and "term" in result


# ---------------------------------------------------------------------------
# Path-segment guard + input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whois_rejects_path_traversal_before_http(monkeypatch):
    called = _patch_client(monkeypatch, {"response": {}})
    handler = _handlers()["get_domaintools_whois"]
    for bad in (
        "../account",
        "example.com/whois",
        "..",
        "a b",
        "a\tb",
        "a\nb",
        "a\rb",
        "a\x00b",
        "a\x7fb",
        # Unicode IDN must be passed in punycode (xn--) form: httpx would
        # percent-encode the raw path while the HMAC covers the raw string,
        # desyncing the signature into a misleading auth error.
        "b\u00fccher.de",
    ):
        result = await handler(query=bad)
        assert "Error" in result, f"expected rejection for {bad!r}"
    assert "client" not in called  # no HTTP request was ever dispatched


@pytest.mark.asyncio
async def test_iris_search_requires_a_base_parameter():
    handler = _handlers()["search_domaintools_iris"]
    result = await handler(tld="com")
    assert "at least one base search parameter" in result


@pytest.mark.asyncio
async def test_domain_intel_rejects_more_than_100_domains():
    handler = _handlers()["get_domaintools_domain_intel"]
    result = await handler(domains=",".join(f"d{i}.com" for i in range(101)))
    assert "at most 100" in result


# ---------------------------------------------------------------------------
# Credential guard (dummy/introspection token must still build tools)
# ---------------------------------------------------------------------------


def test_build_tools_with_dummy_token_still_returns_definitions():
    executor = _SafeIntegrationExecutor()
    defs = build_tools("__dummy__", executor)
    assert len(defs) == 22
    assert len({d.name for d in defs}) == 22
    assert set(executor._handlers) == {d.name for d in defs}


@pytest.mark.asyncio
async def test_missing_credentials_return_actionable_error():
    handler = _handlers(token="__dummy__")["get_domaintools_account"]
    result = await handler()
    assert "credentials are not configured" in result


# ---------------------------------------------------------------------------
# Consolidated Detect manager - action / parameter validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_detect_rejects_unknown_action():
    handler = _handlers()["manage_domaintools_detect"]
    result = await handler(action="does_not_exist")
    assert "unknown action" in result.lower()
    assert "create_monitor" in result  # lists supported actions


@pytest.mark.asyncio
async def test_manage_detect_create_requires_term():
    handler = _handlers()["manage_domaintools_detect"]
    result = await handler(action="create_monitor", term="ab")
    assert "at least 3 characters" in result


@pytest.mark.asyncio
async def test_manage_detect_escalate_validates_type_and_limits():
    handler = _handlers()["manage_domaintools_detect"]
    result = await handler(action="escalate_domains", domain_ids="a,b")
    assert "escalation_type" in result
    result = await handler(
        action="escalate_domains",
        domain_ids=",".join(f"id{i}" for i in range(11)),
        escalation_type="google_safe",
    )
    assert "at most 10" in result


@pytest.mark.asyncio
async def test_manage_detect_watch_sends_patch_with_ids(monkeypatch):
    holder = _patch_client(
        monkeypatch,
        {"watchlist_domains": [{"id": "abc", "domain": "fake-brand.com", "state": "watched"}]},
    )
    handler = _handlers()["manage_domaintools_detect"]
    result = await handler(action="watch_domains", domain_ids="abc")

    assert "watched" in result and "fake-brand.com" in result
    method, url, kwargs = holder["client"].requests[0]
    assert method == "PATCH"
    assert url.endswith("/v1/iris-detect/domains/")
    assert kwargs.get("json") == {"watchlist_domain_ids": ["abc"], "state": "watched"}


@pytest.mark.asyncio
async def test_detect_monitors_limit_clamped_per_include_counts(monkeypatch):
    """Vendor contract (Iris Detect API guide, Monitor list): limit max is
    500, restricted to 100 when include_counts=true. The clamp must follow
    that split - the domains endpoints' 100-cap does NOT apply here."""
    holder = _patch_client(monkeypatch, {"monitors": [], "total_count": 0})
    handler = _handlers()["list_domaintools_detect_monitors"]

    await handler(limit=800)
    _, _, kwargs = holder["client"].requests[0]
    assert (kwargs.get("params") or {}).get("limit") == 500

    holder = _patch_client(monkeypatch, {"monitors": [], "total_count": 0})
    await handler(limit=800, include_counts=True, datetime_counts_since="-86400")
    _, _, kwargs = holder["client"].requests[0]
    assert (kwargs.get("params") or {}).get("limit") == 100


@pytest.mark.asyncio
async def test_detect_domains_rejects_invalid_state():
    handler = _handlers()["list_domaintools_detect_domains"]
    result = await handler(state="escalated")
    assert "state must be one of" in result


@pytest.mark.asyncio
async def test_manage_detect_update_can_disable_substring_variations(monkeypatch):
    """False must be sent (tri-state), not silently dropped like an omission."""
    holder = _patch_client(monkeypatch, {"monitor": {"id": "m1"}})
    handler = _handlers()["manage_domaintools_detect"]
    result = await handler(
        action="update_monitor", monitor_id="m1", match_substring_variations=False
    )

    assert "updated" in result
    method, url, kwargs = holder["client"].requests[0]
    assert method == "PUT"
    assert url.endswith("/v1/iris-detect/monitors/")
    assert kwargs.get("json") == {"match_substring_variations": False}


@pytest.mark.asyncio
async def test_manage_detect_update_omitted_substring_flag_requires_a_field():
    """Omitting match_substring_variations must NOT count as a change."""
    handler = _handlers()["manage_domaintools_detect"]
    result = await handler(action="update_monitor", monitor_id="m1")
    assert "requires at least one of" in result


# ---------------------------------------------------------------------------
# Iris sort_direction normalization ('desc' -> API token 'dsc')
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iris_sort_direction_desc_normalized_to_dsc(monkeypatch):
    holder = _patch_client(monkeypatch, {"response": {"results": []}})
    handler = _handlers()["search_domaintools_iris"]
    await handler(ip="1.2.3.4", sort_by="risk_score", sort_direction="desc")

    _, _, kwargs = holder["client"].requests[0]
    assert (kwargs.get("params") or {}).get("sort_direction") == "dsc"


@pytest.mark.asyncio
async def test_iris_sort_direction_rejects_invalid_value(monkeypatch):
    called = _patch_client(monkeypatch, {"response": {"results": []}})
    handler = _handlers()["search_domaintools_iris"]
    result = await handler(ip="1.2.3.4", sort_direction="upward")
    assert "sort_direction" in result and "Error" in result
    assert "client" not in called  # rejected before any HTTP request


# ---------------------------------------------------------------------------
# Pagination hints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iris_pagination_hint_surfaces_position(monkeypatch):
    _patch_client(
        monkeypatch,
        {
            "response": {
                "results": [{"domain": "example.com", "active": True}],
                "results_count": 1,
                "total_count": 700,
                "has_more_results": True,
                "position": "cursor123",
            }
        },
    )
    handler = _handlers()["search_domaintools_iris"]
    result = await handler(ip="1.2.3.4")
    assert "example.com" in result
    assert 'position="cursor123"' in result
