"""Tests for the Ransomware.live integration provider.

Covers dual-tier routing (PRO api_key vs free v2), tool introspection with the
catalog dummy token, the tier resolver + header/URL chokepoints, victim / group
/ press / stats / status read tools (endpoint selection, client-side filtering
and ordering, formatting, truncation hints), input validation, and PRO-only
gating on the free tier. Every API call routes through the shared
``safe_request`` helper, which is monkeypatched here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import ransomware_live as rl
from app.integrations.providers.ransomware_live import (
    RANSOMLIVE_FREE_BASE,
    RANSOMLIVE_PRO_BASE,
    RansomwareLiveIntegration,
    build_ransomlive_headers,
    build_tools,
    ransomlive_status_url,
    resolve_tier,
)
from app.tools.base import CallableToolExecutor, ToolCall

_PRO_TOKEN = "rl-pro-test-key"
_NO_AUTH = "__no_auth__"

_TOOL_NAMES = {
    "list_ransomlive_recent_victims",
    "search_ransomlive_victims",
    "get_ransomlive_victim",
    "list_ransomlive_groups",
    "get_ransomlive_group",
    "list_ransomlive_press",
    "get_ransomlive_stats",
    "get_ransomlive_status",
    # PRO artifact intelligence surface
    "list_ransomlive_sectors",
    "get_ransomlive_iocs",
    "get_ransomlive_yara",
    "list_ransomlive_negotiations",
    "get_ransomlive_negotiation_chat",
    "list_ransomlive_ransomnotes",
    "get_ransomlive_ransomnote",
    "get_ransomlive_csirt_contacts",
    "list_ransomlive_8k_filings",
}

# The PRO-only tools that must refuse on the free (unauthenticated) tier.
_PRO_ONLY_TOOLS = {
    "get_ransomlive_victim",
    "list_ransomlive_sectors",
    "get_ransomlive_iocs",
    "get_ransomlive_yara",
    "list_ransomlive_negotiations",
    "get_ransomlive_negotiation_chat",
    "list_ransomlive_ransomnotes",
    "get_ransomlive_ransomnote",
    "get_ransomlive_csirt_contacts",
    "list_ransomlive_8k_filings",
}


class _StubResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x"

    def json(self) -> Any:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls."""

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        return _StubResponse(self._router(method, url)), None


class _ErrorRecorder:
    """Fake ``safe_request`` failing with *status_code*, honouring the
    ``expected_statuses`` opt-in contract (failed response returned alongside
    the error string so the caller can branch on ``resp.status_code``)."""

    def __init__(self, status_code: int, err: str):
        self._status = status_code
        self._err = err
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        if kwargs.get("expected_statuses") is not None:
            return _StubResponse({}, status_code=self._status), self._err
        return None, self._err


def _build(monkeypatch, router, token=_PRO_TOKEN) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(rl, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=token, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + tier helpers
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_catalog_dummy():
    executor = CallableToolExecutor()
    tools = build_tools(token="__dummy__", executor=executor)
    assert {t.name for t in tools} == _TOOL_NAMES
    assert all(t.source == "integration:ransomware_live" for t in tools)
    # The API is read-only, so every tool sorts as a read (priority 0).
    assert all(t.priority == 0 for t in tools)


def test_tool_set_is_identical_across_tiers():
    pro = {t.name for t in build_tools(_PRO_TOKEN, CallableToolExecutor())}
    free = {t.name for t in build_tools(_NO_AUTH, CallableToolExecutor())}
    assert pro == free == _TOOL_NAMES


def test_resolve_tier():
    assert resolve_tier(_PRO_TOKEN) == (True, _PRO_TOKEN)
    assert resolve_tier("  spaced-key  ") == (True, "spaced-key")
    assert resolve_tier(_NO_AUTH) == (False, "")
    assert resolve_tier("") == (False, "")
    assert resolve_tier(None) == (False, "")
    # The catalog dummy resolves to PRO so introspection sees the full surface.
    assert resolve_tier("__dummy__")[0] is True


def test_header_and_url_chokepoints():
    pro_headers = build_ransomlive_headers(_PRO_TOKEN)
    assert pro_headers["X-API-KEY"] == _PRO_TOKEN
    assert pro_headers["Accept"] == "application/json"
    assert "X-API-KEY" not in build_ransomlive_headers(_NO_AUTH)
    assert ransomlive_status_url(_PRO_TOKEN) == f"{RANSOMLIVE_PRO_BASE}/validate"
    assert ransomlive_status_url(_NO_AUTH) == f"{RANSOMLIVE_FREE_BASE}/info"
    # A key pasted with whitespace never yields a malformed header.
    assert build_ransomlive_headers("  k \n")["X-API-KEY"] == "k"


# ---------------------------------------------------------------------------
# Recent victims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_victims_pro_uses_recent_endpoint_and_order(monkeypatch):
    def router(method, url):
        return [
            {"victim": "Acme", "group": "lockbit3", "country": "US", "discovered": "2026-07-01T00:00:00"},
            {"victim": "Beta", "group": "clop", "country": "FR", "discovered": "2026-07-05T00:00:00"},
        ]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="r1", name="list_ransomlive_recent_victims", arguments={"limit": 10})
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["url"] == f"{RANSOMLIVE_PRO_BASE}/victims/recent"
    assert call["params"]["order"] == "discovered"
    assert call["headers"]["X-API-KEY"] == _PRO_TOKEN
    # Sorted newest-first by discovered.
    assert result.content.index("Beta") < result.content.index("Acme")


@pytest.mark.asyncio
async def test_recent_victims_free_uses_v2_endpoint(monkeypatch):
    def router(method, url):
        return [{"victim": "Gamma", "group": "akira", "country": "DE", "discovered": "2026-07-02"}]

    executor, recorder = _build(monkeypatch, router, token=_NO_AUTH)
    result = await executor.execute_tool(
        ToolCall(id="r2", name="list_ransomlive_recent_victims", arguments={})
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["url"] == f"{RANSOMLIVE_FREE_BASE}/recentvictims"
    # No auth header on the free tier.
    assert "X-API-KEY" not in call["headers"]


@pytest.mark.asyncio
async def test_recent_victims_clamps_limit(monkeypatch):
    def router(method, url):
        return [{"victim": f"V{i}", "group": "g", "discovered": f"2026-07-{i:02d}"} for i in range(1, 40)]

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="r3", name="list_ransomlive_recent_victims", arguments={"limit": 5})
    )
    assert not result.is_error
    assert "Showing 5 of 39" in result.content


# ---------------------------------------------------------------------------
# Victim search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_requires_a_filter(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(id="s0", name="search_ransomlive_victims", arguments={})
    )
    assert "at least one filter" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_search_month_requires_year(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(id="s0b", name="search_ransomlive_victims", arguments={"month": "03"})
    )
    assert "requires 'year'" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_search_pro_keyword_uses_search_endpoint(monkeypatch):
    def router(method, url):
        return [{"victim": "Hospital X", "group": "lockbit", "country": "US"}]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s1",
            name="search_ransomlive_victims",
            arguments={"query": "hospital", "country": "us"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["url"] == f"{RANSOMLIVE_PRO_BASE}/victims/search"
    assert call["params"]["q"] == "hospital"
    # Country is upper-cased.
    assert call["params"]["country"] == "US"


@pytest.mark.asyncio
async def test_search_pro_filters_use_victims_endpoint(monkeypatch):
    def router(method, url):
        return [{"victim": "Acme", "group": "lockbit", "country": "FR"}]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s2",
            name="search_ransomlive_victims",
            arguments={"group": "lockbit", "year": "2024", "month": "03"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["url"] == f"{RANSOMLIVE_PRO_BASE}/victims/"
    assert call["params"]["group"] == "lockbit"
    assert call["params"]["year"] == "2024"
    assert call["params"]["month"] == "03"


@pytest.mark.asyncio
async def test_search_pro_filter_month_is_zero_padded(monkeypatch):
    def router(method, url):
        return []

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s2c",
            name="search_ransomlive_victims",
            arguments={"group": "lockbit", "year": "2024", "month": "3"},
        )
    )
    assert not result.is_error
    # The PRO /victims/ endpoint documents the 2-digit month form - "3" must
    # be sent as "03" (matching the local date-filter comparison).
    assert recorder.calls[-1]["params"]["month"] == "03"


@pytest.mark.asyncio
async def test_search_free_date_path_month_is_zero_padded(monkeypatch):
    def router(method, url):
        return []

    executor, recorder = _build(monkeypatch, router, token=_NO_AUTH)
    result = await executor.execute_tool(
        ToolCall(
            id="s2d",
            name="search_ransomlive_victims",
            arguments={"year": "2024", "month": "3"},
        )
    )
    assert not result.is_error
    # The free v2 date endpoint expects /victims/{year}/{MM} - "3" -> "03".
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_FREE_BASE}/victims/2024/03"


@pytest.mark.asyncio
async def test_search_free_group_uses_groupvictims_and_filters_locally(monkeypatch):
    def router(method, url):
        # groupvictims returns all victims for the group; the country filter is
        # applied client-side on the free tier.
        return [
            {"victim": "FR Co", "group": "lockbit3", "country": "FR", "discovered": "2026-01-01"},
            {"victim": "US Co", "group": "lockbit3", "country": "US", "discovered": "2026-02-01"},
        ]

    executor, recorder = _build(monkeypatch, router, token=_NO_AUTH)
    result = await executor.execute_tool(
        ToolCall(
            id="s3",
            name="search_ransomlive_victims",
            arguments={"group": "lockbit3", "country": "FR"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["url"] == f"{RANSOMLIVE_FREE_BASE}/groupvictims/lockbit3"
    assert "FR Co" in result.content
    assert "US Co" not in result.content


@pytest.mark.asyncio
async def test_search_pro_keyword_applies_date_filter_locally(monkeypatch):
    def router(method, url):
        # /victims/search has no date params - the tool must filter locally.
        return [
            {"victim": "Acme 2024", "group": "lockbit", "attackdate": "2024-03-10"},
            {"victim": "Acme 2023", "group": "lockbit", "attackdate": "2023-11-02"},
        ]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s2b",
            name="search_ransomlive_victims",
            arguments={"query": "acme", "year": "2024", "month": "3"},
        )
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/victims/search"
    assert "Acme 2024" in result.content
    assert "Acme 2023" not in result.content


@pytest.mark.asyncio
async def test_search_free_year_without_month_is_actionable(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [], token=_NO_AUTH)
    result = await executor.execute_tool(
        ToolCall(id="s3b", name="search_ransomlive_victims", arguments={"year": "2024"})
    )
    # The free v2 date endpoint requires a month - no request is issued and
    # the agent gets concrete next steps instead of a January-only listing.
    assert recorder.calls == []
    assert "month" in result.content
    assert "PRO API key" in result.content


@pytest.mark.asyncio
async def test_search_free_sector_matches_sector_field_fallback(monkeypatch):
    def router(method, url):
        # Some v2 payloads carry 'sector' instead of 'activity' - both must
        # satisfy the sector filter (no false negatives).
        return [
            {"victim": "Clinic A", "group": "g", "sector": "Healthcare", "discovered": "2026-01-01"},
            {"victim": "Plant B", "group": "g", "activity": "Manufacturing", "discovered": "2026-01-02"},
        ]

    executor, _recorder = _build(monkeypatch, router, token=_NO_AUTH)
    result = await executor.execute_tool(
        ToolCall(
            id="s3c",
            name="search_ransomlive_victims",
            arguments={"group": "g", "sector": "healthcare"},
        )
    )
    assert not result.is_error
    assert "Clinic A" in result.content
    assert "Plant B" not in result.content


@pytest.mark.asyncio
async def test_search_free_path_segment_is_encoded(monkeypatch):
    def router(method, url):
        return []

    executor, recorder = _build(monkeypatch, router, token=_NO_AUTH)
    await executor.execute_tool(
        ToolCall(
            id="s4",
            name="search_ransomlive_victims",
            arguments={"group": "../../etc/passwd"},
        )
    )
    call = recorder.calls[-1]
    # The path traversal attempt is percent-encoded, keeping the request scoped.
    assert "/groupvictims/" in call["url"]
    assert "../" not in call["url"]
    assert "%2F" in call["url"] or "%2E%2E" in call["url"]


# ---------------------------------------------------------------------------
# Victim detail (PRO only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_victim_free_tier_is_gated(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {}, token=_NO_AUTH)
    result = await executor.execute_tool(
        ToolCall(id="v1", name="get_ransomlive_victim", arguments={"victim_id": "QWJjQGdycA=="})
    )
    assert "only available on the Ransomware.live PRO API" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_get_victim_requires_id(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="v2", name="get_ransomlive_victim", arguments={"victim_id": " "})
    )
    assert "victim_id is required" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_get_victim_rejects_bad_base64(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="v3", name="get_ransomlive_victim", arguments={"victim_id": "@@not-base64@@"})
    )
    assert "not a valid Base64 victim id" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_get_victim_404_returns_friendly_not_found(monkeypatch):
    recorder = _ErrorRecorder(404, "Error: Ransomware.live returned 404.")
    monkeypatch.setattr(rl, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_PRO_TOKEN, executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="v404", name="get_ransomlive_victim", arguments={"victim_id": "QWJjQGdycA=="})
    )
    # The expected 404 maps to the friendly message, not the raw HTTP error.
    assert "No victim found" in result.content
    assert "404" not in result.content


@pytest.mark.asyncio
async def test_get_group_404_returns_friendly_not_found(monkeypatch):
    recorder = _ErrorRecorder(404, "Error: Ransomware.live returned 404.")
    monkeypatch.setattr(rl, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_PRO_TOKEN, executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="g404", name="get_ransomlive_group", arguments={"group": "nogroup"})
    )
    assert "No group found named 'nogroup'" in result.content
    assert "404" not in result.content


@pytest.mark.asyncio
async def test_get_victim_500_still_surfaces_http_error(monkeypatch):
    recorder = _ErrorRecorder(500, "Error: Ransomware.live returned 500.")
    monkeypatch.setattr(rl, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_PRO_TOKEN, executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="v500", name="get_ransomlive_victim", arguments={"victim_id": "QWJjQGdycA=="})
    )
    # Unexpected statuses keep the actionable HTTP error string.
    assert "500" in result.content


@pytest.mark.asyncio
async def test_get_victim_pro_returns_detail(monkeypatch):
    def router(method, url):
        return {
            "victim": "Acme Corp",
            "group": "lockbit3",
            "country": "US",
            "activity": "Manufacturing",
            "attackdate": "2026-06-01T00:00:00",
            "description": "Acme was listed on the leak site.",
            "permalink": "https://www.ransomware.live/id/abc",
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="v4", name="get_ransomlive_victim", arguments={"victim_id": "QWNtZUBscms="})
    )
    assert not result.is_error
    assert "Acme Corp" in result.content
    assert "lockbit3" in result.content
    assert recorder.calls[-1]["url"].startswith(f"{RANSOMLIVE_PRO_BASE}/victim/")


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_groups_both_tiers_use_groups_endpoint(monkeypatch):
    def router(method, url):
        return [{"group": "lockbit3", "victims": 2000}, {"name": "clop", "victims": 500}]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="g1", name="list_ransomlive_groups", arguments={})
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/groups"
    assert "lockbit3" in result.content


@pytest.mark.asyncio
async def test_get_group_free_uses_singular_endpoint(monkeypatch):
    def router(method, url):
        return {"name": "lockbit3", "description": "Prolific RaaS group.", "victims": 2000}

    executor, recorder = _build(monkeypatch, router, token=_NO_AUTH)
    result = await executor.execute_tool(
        ToolCall(id="g2", name="get_ransomlive_group", arguments={"group": "lockbit3"})
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_FREE_BASE}/group/lockbit3"


@pytest.mark.asyncio
async def test_get_group_pro_uses_plural_endpoint_and_renders_intel(monkeypatch):
    def router(method, url):
        return {
            "name": "lockbit3",
            "description": "Prolific RaaS group.",
            "victims": 2000,
            "locations": [{"fqdn": "lockbitxxx.onion", "enabled": True}],
            "ttps": [{"id": "T1486"}],
            "vulnerabilities": [{"cve": "CVE-2023-0669"}],
            "tools": [{"name": "Cobalt Strike"}],
            "has_negotiations": True,
            "negotiation_count": 12,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="g3", name="get_ransomlive_group", arguments={"group": "lockbit3"})
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/groups/lockbit3"
    assert "T1486" in result.content
    assert "CVE-2023-0669" in result.content
    assert "Cobalt Strike" in result.content
    assert "negotiations" in result.content


@pytest.mark.asyncio
async def test_get_group_requires_name(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="g4", name="get_ransomlive_group", arguments={"group": ""})
    )
    assert "group is required" in result.content
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Press
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_press_pro_recent_and_all(monkeypatch):
    def router(method, url):
        return [{"title": "Attack on X", "date": "2026-06-01", "country": "FR", "victim": "X"}]

    executor, recorder = _build(monkeypatch, router)
    # No year -> /press/recent
    await executor.execute_tool(
        ToolCall(id="p1", name="list_ransomlive_press", arguments={"country": "fr"})
    )
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/press/recent"
    assert recorder.calls[-1]["params"]["country"] == "FR"
    # With year -> /press/all
    await executor.execute_tool(
        ToolCall(id="p2", name="list_ransomlive_press", arguments={"year": "2024"})
    )
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/press/all"
    assert recorder.calls[-1]["params"]["year"] == "2024"


@pytest.mark.asyncio
async def test_press_free_filters_locally(monkeypatch):
    def router(method, url):
        return [
            {"title": "FR incident", "date": "2024-03-02", "country": "FR"},
            {"title": "US incident", "date": "2024-03-05", "country": "US"},
        ]

    executor, recorder = _build(monkeypatch, router, token=_NO_AUTH)
    result = await executor.execute_tool(
        ToolCall(
            id="p3",
            name="list_ransomlive_press",
            arguments={"year": "2024", "country": "FR"},
        )
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_FREE_BASE}/allcyberattacks"
    assert "FR incident" in result.content
    assert "US incident" not in result.content


@pytest.mark.asyncio
async def test_press_pro_month_is_zero_padded(monkeypatch):
    def router(method, url):
        return []

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="p4",
            name="list_ransomlive_press",
            arguments={"year": "2024", "month": "3"},
        )
    )
    assert not result.is_error
    # The PRO /press/all endpoint gets the documented 2-digit month form,
    # consistent with the free-tier local filter and the tool's own examples.
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/press/all"
    assert recorder.calls[-1]["params"]["month"] == "03"


# ---------------------------------------------------------------------------
# Stats + status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_pro_nested_shape(monkeypatch):
    def router(method, url):
        return {"stats": {"victims": 29000, "groups": 356, "press": 5000}, "last_update": "2026-07-06"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="st1", name="get_ransomlive_stats", arguments={})
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/stats"
    assert "29000" in result.content
    assert "356" in result.content


@pytest.mark.asyncio
async def test_stats_free_flat_shape(monkeypatch):
    def router(method, url):
        return {"groups": 356, "victims": 29442}

    executor, recorder = _build(monkeypatch, router, token=_NO_AUTH)
    result = await executor.execute_tool(
        ToolCall(id="st2", name="get_ransomlive_stats", arguments={})
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_FREE_BASE}/info"
    assert "29442" in result.content
    assert "Free v2 tier" in result.content


@pytest.mark.asyncio
async def test_status_pro_validates_key(monkeypatch):
    def router(method, url):
        return {"client": "acme-client"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="st3", name="get_ransomlive_status", arguments={})
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/validate"
    assert "PRO API" in result.content
    assert "acme-client" in result.content


@pytest.mark.asyncio
async def test_status_free_reports_tier(monkeypatch):
    def router(method, url):
        return {"groups": 356, "victims": 29442}

    executor, recorder = _build(monkeypatch, router, token=_NO_AUTH)
    result = await executor.execute_tool(
        ToolCall(id="st4", name="get_ransomlive_status", arguments={})
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_FREE_BASE}/info"
    assert "free" in result.content.lower()


# ---------------------------------------------------------------------------
# Integration class
# ---------------------------------------------------------------------------


def test_integration_class_metadata():
    integ = RansomwareLiveIntegration()
    assert integ.provider_name == "ransomware_live"
    assert integ.display_name == "Ransomware.live"
    assert integ.auth_types == ["api_key", "none"]
    assert integ.get_oauth_defaults() is None


@pytest.mark.asyncio
async def test_connectivity_blank_key_fails_fast_instead_of_free_probe():
    # An api_key-mode integration missing its key must NOT silently pass the
    # connectivity test by probing the unauthenticated free /info endpoint
    # (the free tier is represented by the explicit "__no_auth__" sentinel).
    from app.api.v1.integration_testing import test_provider_connectivity

    result = await test_provider_connectivity(
        "ransomware_live", "  ", {}, "Ransomware.live"
    )
    assert result["status"] == "error"
    assert "Missing API key" in result["message"]


# ---------------------------------------------------------------------------
# PRO artifact intelligence surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pro_only_tools_are_gated_on_free_tier(monkeypatch):
    # Every PRO-only tool must refuse (without any network call) on the free
    # tier, pointing the user at the free PRO key.
    executor, recorder = _build(monkeypatch, lambda m, u: {}, token=_NO_AUTH)
    minimal_args = {
        "get_ransomlive_victim": {"victim_id": "QWJjQGdycA=="},
        "get_ransomlive_negotiation_chat": {"group": "lockbit3", "chat_id": "20240517"},
        "get_ransomlive_ransomnote": {"group": "lockbit3", "note_name": "note"},
        "get_ransomlive_csirt_contacts": {"country": "US"},
    }
    for name in _PRO_ONLY_TOOLS:
        result = await executor.execute_tool(
            ToolCall(id=f"g-{name}", name=name, arguments=minimal_args.get(name, {}))
        )
        assert "only available on the Ransomware.live PRO API" in result.content, name
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_list_sectors_pro(monkeypatch):
    def router(method, url):
        return [
            {"sector": "Healthcare", "count": 1200},
            {"sector": "Education", "count": 800},
        ]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="sec1", name="list_ransomlive_sectors", arguments={})
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/listsectors"
    assert "Healthcare" in result.content
    assert "1200 victims" in result.content


@pytest.mark.asyncio
async def test_get_iocs_inventory_and_per_group(monkeypatch):
    def router(method, url):
        if url.endswith("/iocs"):
            return [{"group": "lockbit3", "types": "md5:10, ip:5"}]
        return {"md5": ["abc123", "def456"], "ip": ["1.2.3.4"]}

    executor, recorder = _build(monkeypatch, router)
    # Inventory (no group).
    inv = await executor.execute_tool(
        ToolCall(id="ioc1", name="get_ransomlive_iocs", arguments={})
    )
    assert not inv.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/iocs"
    assert "lockbit3" in inv.content
    # Per-group indicators grouped by type.
    detail = await executor.execute_tool(
        ToolCall(id="ioc2", name="get_ransomlive_iocs", arguments={"group": "lockbit3", "type": "md5"})
    )
    assert not detail.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/iocs/lockbit3"
    assert recorder.calls[-1]["params"]["type"] == "md5"
    assert "abc123" in detail.content


@pytest.mark.asyncio
async def test_get_yara_rules(monkeypatch):
    def router(method, url):
        if url.endswith("/yara"):
            return [{"group": "blackcat", "count": 3}]
        return [{"filename": "blackcat.yar", "content": "rule blackcat { condition: true }"}]

    executor, recorder = _build(monkeypatch, router)
    lst = await executor.execute_tool(
        ToolCall(id="y1", name="get_ransomlive_yara", arguments={})
    )
    assert "blackcat" in lst.content
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/yara"
    detail = await executor.execute_tool(
        ToolCall(id="y2", name="get_ransomlive_yara", arguments={"group": "blackcat"})
    )
    assert "rule blackcat" in detail.content
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/yara/blackcat"


@pytest.mark.asyncio
async def test_negotiations_list_and_chat(monkeypatch):
    def router(method, url):
        if url.endswith("/negotiations/lockbit3"):
            return [
                {
                    "id": "20240517",
                    "message_count": 12,
                    "initialransom": "$1,000,000",
                    "negotiatedransom": "$400,000",
                    "paid": True,
                }
            ]
        return {
            "initialransom": "$1,000,000",
            "negotiatedransom": "$400,000",
            "paid": True,
            "message_count": 2,
            "messages": [
                {"party": "victim", "message": "We cannot pay that."},
                {"party": "group", "message": "Final offer."},
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    lst = await executor.execute_tool(
        ToolCall(id="n1", name="list_ransomlive_negotiations", arguments={"group": "lockbit3"})
    )
    assert not lst.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/negotiations/lockbit3"
    assert "20240517" in lst.content
    chat = await executor.execute_tool(
        ToolCall(
            id="n2",
            name="get_ransomlive_negotiation_chat",
            arguments={"group": "lockbit3", "chat_id": "20240517"},
        )
    )
    assert not chat.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/negotiations/lockbit3/20240517"
    assert "Final offer" in chat.content


@pytest.mark.asyncio
async def test_negotiation_chat_requires_both_ids(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="n3", name="get_ransomlive_negotiation_chat", arguments={"group": "lockbit3"})
    )
    assert "both group and chat_id are required" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_ransomnotes_list_and_get(monkeypatch):
    def router(method, url):
        if url.endswith("/ransomnotes/clop"):
            return ["readme_clop", "note2"]
        return {"extension": "txt", "content": "Your files are encrypted. Contact us."}

    executor, recorder = _build(monkeypatch, router)
    lst = await executor.execute_tool(
        ToolCall(id="rn1", name="list_ransomlive_ransomnotes", arguments={"group": "clop"})
    )
    assert "readme_clop" in lst.content
    note = await executor.execute_tool(
        ToolCall(
            id="rn2",
            name="get_ransomlive_ransomnote",
            arguments={"group": "clop", "note_name": "readme_clop"},
        )
    )
    assert not note.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/ransomnotes/clop/readme_clop"
    assert "files are encrypted" in note.content


@pytest.mark.asyncio
async def test_csirt_contacts(monkeypatch):
    def router(method, url):
        return [
            {"name": "CERT-FR", "email": "cert@ssi.gouv.fr", "website": "https://cert.ssi.gouv.fr"}
        ]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="get_ransomlive_csirt_contacts", arguments={"country": "FR"})
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/csirt/FR"
    assert "CERT-FR" in result.content


@pytest.mark.asyncio
async def test_8k_filings_params_and_booleans(monkeypatch):
    def router(method, url):
        return [
            {"company": "Acme Inc", "ticker": "ACME", "date": "2025-06-01", "item": "1.05"}
        ]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="k1",
            name="list_ransomlive_8k_filings",
            arguments={"ticker": "acme", "year": "2025", "item801": False},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["url"] == f"{RANSOMLIVE_PRO_BASE}/8k"
    # Ticker upper-cased; item801 explicitly disabled; item105 (default true) omitted.
    assert call["params"]["ticker"] == "ACME"
    assert call["params"]["item801"] == "false"
    assert "item105" not in call["params"]
    assert "Acme Inc" in result.content


@pytest.mark.asyncio
async def test_8k_month_requires_year(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(id="k2", name="list_ransomlive_8k_filings", arguments={"month": "06"})
    )
    assert "requires 'year'" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_8k_month_is_zero_padded(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [])
    result = await executor.execute_tool(
        ToolCall(
            id="k3",
            name="list_ransomlive_8k_filings",
            arguments={"year": "2025", "month": "6"},
        )
    )
    assert not result.is_error
    assert recorder.calls[-1]["params"]["month"] == "06"


@pytest.mark.asyncio
async def test_inventory_payload_shapes_are_normalized(monkeypatch):
    # Inventory endpoints may return plain strings or a flat {group: count}
    # map - both must render instead of crashing or reporting "no groups".
    def router(method, url):
        if url.endswith("/yara"):
            return ["blackcat", "clop"]
        return {"lockbit3": 15, "akira": 4}

    executor, _recorder = _build(monkeypatch, router)
    yara = await executor.execute_tool(
        ToolCall(id="s1", name="get_ransomlive_yara", arguments={})
    )
    assert not yara.is_error
    assert "blackcat" in yara.content and "clop" in yara.content
    iocs = await executor.execute_tool(
        ToolCall(id="s2", name="get_ransomlive_iocs", arguments={})
    )
    assert not iocs.is_error
    assert "lockbit3" in iocs.content and "akira" in iocs.content


@pytest.mark.asyncio
async def test_yara_and_ransomnote_content_preserves_newlines(monkeypatch):
    yara_rule = "rule demo {\n  // detects demo\n  condition:\n    true\n}"
    note_text = "ALL YOUR FILES ARE ENCRYPTED\n\nContact: evil@example.onion\nDo not rename files."

    def router(method, url):
        if "/yara/" in url:
            return [{"filename": "demo.yar", "content": yara_rule}]
        return {"extension": "txt", "content": note_text}

    executor, _recorder = _build(monkeypatch, router)
    yara = await executor.execute_tool(
        ToolCall(id="nl1", name="get_ransomlive_yara", arguments={"group": "demo"})
    )
    # Flattening a YARA rule would swallow everything after the '//' comment.
    assert "// detects demo\n  condition:" in yara.content
    note = await executor.execute_tool(
        ToolCall(
            id="nl2",
            name="get_ransomlive_ransomnote",
            arguments={"group": "demo", "note_name": "readme"},
        )
    )
    assert "ENCRYPTED\n\nContact:" in note.content


@pytest.mark.asyncio
async def test_csirt_country_is_uppercased(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: [{"name": "CERT-FR"}])
    result = await executor.execute_tool(
        ToolCall(id="c2", name="get_ransomlive_csirt_contacts", arguments={"country": "fr"})
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"] == f"{RANSOMLIVE_PRO_BASE}/csirt/FR"
