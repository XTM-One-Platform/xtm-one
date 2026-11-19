# SPDX-License-Identifier: Apache-2.0

"""Ransomware.live integration - ransomware victim / group tracking.

Ransomware.live (Julien Mousqueton) monitors ransomware groups' extortion and
leak sites and normalizes every disclosure into a queryable dataset. Agents use
it to answer "was company X claimed by a ransomware group?", "what is group Y
and how does it operate?", "which victims were hit in country Z this month?",
to enrich incident context with negotiation / ransom-note intelligence, and to
track sector / geography victimology trends.

Dual-tier design
----------------
This provider supports BOTH of Ransomware.live's public tiers with a single
implementation, selected by the integration's auth type:

  - **API PRO** (``auth_type="api_key"``, recommended): the authenticated API at
    ``https://api-pro.ransomware.live``. Every request carries the
    ``X-API-KEY`` header. A free-forever key (500,000 requests/month) is minted
    at https://www.ransomware.live/my. The PRO tier exposes the full surface,
    including the group-artifact endpoints (IOCs, YARA, negotiations, ransom
    notes), reference data (sectors, CSIRT contacts), and SEC 8-K filings.

  - **Free v2** (``auth_type="none"``): the unauthenticated API at
    ``https://api.ransomware.live/v2``. No key required, but rate limited to
    ~1 request/minute per endpoint (handled by the shared ``safe_request`` 429
    retry) and intended for personal use. It covers recent victims, victim
    search by group / country / sector / keyword / month, group profiles, and
    cyberattack press coverage. The PRO-only surfaces return an actionable
    message pointing the user at the free PRO key.

The tier is resolved from the runtime token: a real API key selects PRO; the
``__no_auth__`` sentinel (produced for ``auth_type="none"`` integrations)
selects the free v2 tier. The tool SET is identical across tiers so the catalog
can introspect the provider with a dummy token; only the runtime routing (base
URL, headers, endpoint paths, and PRO-only gating) differs.

The API is entirely read-only - Ransomware.live exposes no mutation endpoints,
so every tool is a read tool (priority 0).

The module ships the full surface: the victim / group / press / stats
foundation (both tiers) plus the PRO-only group-artifact and reference tools
(sectors, IOCs, YARA rules, negotiation chats, ransom notes, CSIRT contacts,
SEC 8-K filings), which return actionable guidance on the free tier.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    PLATFORM_USER_AGENT,
    format_entity_list,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "Ransomware.live"
_SOURCE = "integration:ransomware_live"

# PRO (authenticated) and free (v2, unauthenticated) API hosts.
RANSOMLIVE_PRO_BASE = "https://api-pro.ransomware.live"
RANSOMLIVE_FREE_BASE = "https://api.ransomware.live/v2"

# Sentinel produced by _get_token for auth_type="none" integrations - selects
# the free v2 tier. The catalog introspects with "__dummy__", which resolves to
# the PRO tier (no network call is made during introspection).
_NO_AUTH_SENTINEL = "__no_auth__"  # not a secret, a tier marker

_DEFAULT_TIMEOUT = 30
_ORDER_VALUES = ("discovered", "attacked")

# The vendor caps "recent" endpoints at the latest 100 rows and returns bounded
# result sets everywhere else (no server-side pagination). Tools apply a
# client-side limit and append a truncation hint.
_MAX_LIMIT = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def resolve_tier(token: str | None) -> tuple[bool, str]:
    """Resolve the API tier from the runtime token.

    Returns ``(is_pro, api_key)``. A blank token or the ``__no_auth__``
    sentinel selects the free v2 tier (``is_pro=False``); any other value is
    treated as a PRO API key. The catalog dummy token (``__dummy__``) resolves
    to PRO so introspection exercises the full tool set - no request is made
    with it.
    """
    key = (token or "").strip()
    if not key or key == _NO_AUTH_SENTINEL:
        return False, ""
    return True, key


def _pro_headers(api_key: str) -> dict[str, str]:
    """PRO tier request headers (``X-API-KEY`` + JSON Accept).

    The key is stripped so a value pasted with surrounding whitespace can never
    produce a malformed header. This is the single chokepoint shared by the
    tool builder and the connectivity test.
    """
    return {
        "X-API-KEY": (api_key or "").strip(),
        "Accept": "application/json",
    }


def _free_headers() -> dict[str, str]:
    """Free v2 tier request headers (no auth; JSON Accept + a UA)."""
    return {"Accept": "application/json", "User-Agent": PLATFORM_USER_AGENT}


def build_ransomlive_headers(token: str | None) -> dict[str, str]:
    """Build the tier-appropriate request headers for a token.

    Shared by the tool builder and the connectivity test so both agree on the
    single header chokepoint (PRO -> X-API-KEY; free -> plain JSON + UA).
    """
    is_pro, api_key = resolve_tier(token)
    return _pro_headers(api_key) if is_pro else _free_headers()


def ransomlive_status_url(token: str | None) -> str:
    """Return the tier-appropriate connectivity-probe URL for a token.

    PRO validates the key via ``/validate``; the free tier probes the
    unauthenticated ``/info`` endpoint.
    """
    is_pro, _ = resolve_tier(token)
    if is_pro:
        return f"{RANSOMLIVE_PRO_BASE}/validate"
    return f"{RANSOMLIVE_FREE_BASE}/info"


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def _trunc(text: Any, max_len: int = 240) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _trunc_block(text: Any, max_len: int) -> str:
    """Truncate multi-line content while PRESERVING newlines.

    ``_trunc`` flattens newlines for one-line summaries; that would corrupt
    structured artifacts (a YARA rule with a ``//`` comment becomes invalid
    when flattened, and a ransom note turns into one unreadable line). Use
    this for full-content payloads.
    """
    if text is None:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:max_len] + ("\n..." if len(text) > max_len else "")


def _seg(value: str) -> str:
    """URL-encode a value destined for a URL path segment.

    Group names, country codes, sectors, note names, and chat ids all flow into
    the request PATH. Encoding with ``safe=""`` neutralises any ``/`` or ``..``
    a caller might inject and keeps the request targeted at the intended
    resource.
    """
    return quote(str(value or "").strip(), safe="")


def _pro_only(tool_hint: str) -> str:
    """Standard message for a PRO-only tool invoked on the free tier."""
    return (
        f"Error: {tool_hint} is only available on the Ransomware.live PRO API. "
        "This integration is configured for the free (unauthenticated) v2 tier. "
        "Get a free-forever PRO API key at https://www.ransomware.live/my, then "
        "reconfigure this integration with the 'API Key' authentication mode."
    )


def _inventory_items(data: Any) -> list[Any]:
    """Normalize a group-inventory payload to a list of entries.

    The PRO inventory endpoints (``/iocs``, ``/yara``, ``/negotiations``,
    ``/ransomnotes``) may return a bare list, a ``{"groups": [...]}``
    envelope, or a flat ``{group: count}`` map (the shape ``/listsectors``
    uses). Normalize all three so the formatters never miss data.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        groups = data.get("groups")
        if isinstance(groups, list):
            return groups
        return [{"name": k, "count": v} for k, v in data.items()]
    return []


def _fmt_date(value: Any) -> str:
    """Trim an ISO timestamp to minute precision for compact output."""
    s = str(value or "").strip()
    return s[:16] if len(s) >= 16 else s


def _format_victim(v: dict[str, Any]) -> str:
    """One-line victim summary with the identifiers needed for follow-up."""
    name = v.get("victim") or v.get("post_title") or "(unknown)"
    group = v.get("group") or v.get("group_name") or "?"
    country = v.get("country") or "?"
    activity = v.get("activity") or v.get("sector") or ""
    attacked = _fmt_date(v.get("attackdate") or v.get("published"))
    discovered = _fmt_date(v.get("discovered"))
    website = v.get("website") or v.get("domain") or ""
    lines = [
        f"- {name} [group: {group}]"
        + (f" | {activity}" if activity else "")
        + (f" | {country}" if country and country != "?" else ""),
    ]
    meta = []
    if attacked:
        meta.append(f"attacked {attacked}")
    if discovered:
        meta.append(f"discovered {discovered}")
    if website:
        meta.append(f"site {website}")
    if meta:
        lines.append("    " + " | ".join(meta))
    # The Base64 id (PRO /victim/{id}) is present on PRO listings; the v2
    # listings expose the permalink instead. Surface whichever is available so
    # the agent can drill into get_ransomlive_victim (PRO) or the permalink.
    vid = v.get("id")
    if vid:
        lines.append(f"    victim_id: {vid}")
    return "\n".join(lines)


def _limit_victims(items: list[dict], limit: int, noun: str) -> str:
    """Format a bounded victim list with a truncation hint."""
    total = len(items)
    shown = items[:limit]
    out = format_entity_list(
        shown,
        _format_victim,
        noun=noun,
        total=total,
        empty_msg=f"No {noun}s matched.",
    )
    if total > len(shown):
        out += (
            f"\n[Showing {len(shown)} of {total} - raise 'limit' (max "
            f"{_MAX_LIMIT}) or narrow the filters to see more.]"
        )
    return out


# ---------------------------------------------------------------------------
# Integration class
# ---------------------------------------------------------------------------


class RansomwareLiveIntegration(BaseIntegration):
    """Ransomware.live victim / group tracking integration (dual-tier)."""

    provider_name = "ransomware_live"
    display_name = "Ransomware.live"
    icon = "ShieldAlert"
    description = (
        "Track ransomware victims and groups with Ransomware.live - recent "
        "victims, victim search by group/sector/country/date, per-victim "
        "detail (PRO), group profiles (TTPs, CVEs, tools, leak sites), "
        "cyberattack press coverage, global stats, and PRO group artifacts "
        "(IOCs, YARA rules, negotiation chats, ransom notes, CSIRT contacts, "
        "SEC 8-K filings). Supports the free PRO API key and the "
        "unauthenticated v2 tier."
    )
    auth_types = ["api_key", "none"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        """Validate credentials / reachability for the resolved tier.

        PRO: ``GET /validate`` must return 200. Free: ``GET /v2/info`` must
        return 200 (no auth). Never raises - returns ``False`` on any failure.
        """
        is_pro, api_key = resolve_tier(token)
        try:
            async with egress_client(
                timeout=_DEFAULT_TIMEOUT, follow_redirects=False
            ) as client:
                if is_pro:
                    resp = await client.get(
                        f"{RANSOMLIVE_PRO_BASE}/validate",
                        headers=_pro_headers(api_key),
                    )
                else:
                    resp = await client.get(
                        f"{RANSOMLIVE_FREE_BASE}/info", headers=_free_headers()
                    )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def get_user_info(self, token: str) -> dict:
        """Return a small identity/tier descriptor for display."""
        is_pro, api_key = resolve_tier(token)
        if not is_pro:
            return {"tier": "free", "api": RANSOMLIVE_FREE_BASE}
        try:
            async with egress_client(
                timeout=_DEFAULT_TIMEOUT, follow_redirects=False
            ) as client:
                resp = await client.get(
                    f"{RANSOMLIVE_PRO_BASE}/validate", headers=_pro_headers(api_key)
                )
            if resp.status_code == 200:
                body = resp.json() if resp.content else {}
                client_id = ""
                if isinstance(body, dict):
                    client_id = (
                        body.get("client") or body.get("client_id") or body.get("id") or ""
                    )
                return {"tier": "pro", "client": client_id, "api": RANSOMLIVE_PRO_BASE}
        except (httpx.HTTPError, ValueError):
            # Network / non-JSON hiccups are intentionally swallowed: this is
            # a best-effort display descriptor, so degrade to the minimal PRO
            # descriptor below instead of failing the caller.
            pass
        return {"tier": "pro", "api": RANSOMLIVE_PRO_BASE}


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build the Ransomware.live tool set (dual-tier, all read-only).

    The tools always build (the catalog introspects with a dummy token); tier
    resolution and any missing-key handling happen at call time.
    """
    is_pro, api_key = resolve_tier(token)

    def _client(timeout: int = _DEFAULT_TIMEOUT) -> httpx.AsyncClient:
        return egress_client(timeout=timeout, follow_redirects=False)

    async def _get(
        path_pro: str,
        path_free: str,
        action: str,
        *,
        params_pro: dict | None = None,
        params_free: dict | None = None,
        expected_statuses: set[int] | None = None,
    ) -> tuple[Any, str | None]:
        """Issue the tier-appropriate GET and return ``(json, error)``."""
        if is_pro:
            base, headers, path, params = (
                RANSOMLIVE_PRO_BASE,
                _pro_headers(api_key),
                path_pro,
                params_pro,
            )
        else:
            base, headers, path, params = (
                RANSOMLIVE_FREE_BASE,
                _free_headers(),
                path_free,
                params_free,
            )
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base}{path}",
                _PROVIDER,
                action,
                headers=headers,
                params=params or None,
                expected_statuses=expected_statuses,
            )
        if err:
            # A status the caller explicitly expects (e.g. a 404 "not found"
            # probe) is not an error: ``safe_request`` hands the failed
            # response back in that case, so surface it as "no data" and let
            # the caller emit its friendly message instead of the generic
            # HTTP error string.
            if (
                resp is not None
                and expected_statuses
                and resp.status_code in expected_statuses
            ):
                return None, None
            return None, err
        try:
            return resp.json(), None
        except ValueError:
            return None, (
                f"Error: {_PROVIDER} returned a non-JSON response while trying "
                f"to {action}."
            )

    # ══════════════════════════════════════════════════════════════════════
    # VICTIMS
    # ══════════════════════════════════════════════════════════════════════

    async def list_ransomlive_recent_victims(
        order: str = "discovered",
        limit: int = 25,
    ) -> str:
        """List the most recently claimed ransomware victims."""
        order = (order or "discovered").strip().lower()
        if order not in _ORDER_VALUES:
            order = "discovered"
        limit = _clamp(limit, 1, _MAX_LIMIT, 25)
        data, err = await _get(
            "/victims/recent",
            "/recentvictims",
            "list recent victims",
            params_pro={"order": order},
        )
        if err:
            return err
        items = data if isinstance(data, list) else []
        # The free /recentvictims endpoint has no server-side order param, so
        # sort client-side to honour the requested order on both tiers.
        key = "discovered" if order == "discovered" else "attackdate"
        items = sorted(
            items,
            key=lambda v: str(v.get(key) or v.get("published") or ""),
            reverse=True,
        )
        return _limit_victims(items, limit, "recent victim")

    async def search_ransomlive_victims(
        query: str = "",
        group: str = "",
        sector: str = "",
        country: str = "",
        year: str = "",
        month: str = "",
        order: str = "discovered",
        limit: int = 25,
    ) -> str:
        """Search claimed victims by keyword, group, sector, country, or date."""
        query = (query or "").strip()
        group = (group or "").strip()
        sector = (sector or "").strip()
        country = (country or "").strip().upper()
        year = (year or "").strip()
        # Zero-pad the month once at the input boundary so the server-side
        # params (PRO /victims/, free /victims/{year}/{month}, both expecting
        # the documented 2-digit form) and the local date matching always
        # agree (month="3" and month="03" behave identically).
        month = (month or "").strip().zfill(2) if (month or "").strip() else ""
        order = (order or "discovered").strip().lower()
        if order not in _ORDER_VALUES:
            order = "discovered"
        limit = _clamp(limit, 1, _MAX_LIMIT, 25)

        if month and not year:
            return "Error: 'month' requires 'year' (e.g. year=2024, month=03)."
        if not any([query, group, sector, country, year]):
            return (
                "Error: provide at least one filter - query (victim name / "
                "website keyword), group, sector, country (ISO alpha-2), or "
                "year (optionally with month). Use list_ransomlive_groups for "
                "valid group names."
            )

        # Date filtering applied locally when the server endpoint used for
        # this combination cannot filter by date itself (PRO /victims/search,
        # and every free-tier endpoint).
        def _match_date(v: dict) -> bool:
            if not year:
                return True
            dt = str(v.get("attackdate") or v.get("discovered") or "")
            if not dt.startswith(year):
                return False
            return not (month and dt[5:7] != month)

        if is_pro:
            # PRO exposes server-side search (/victims/search with q + filters)
            # and a pure-filter endpoint (/victims/). Use search when a keyword
            # is given, otherwise the filter endpoint.
            if query:
                params = {"q": query, "order": order}
                if group:
                    params["group"] = group
                if sector:
                    params["sector"] = sector
                if country:
                    params["country"] = country
                data, err = await _get(
                    "/victims/search",
                    "",
                    f"search victims for '{query}'",
                    params_pro=params,
                )
            else:
                params = {}
                if group:
                    params["group"] = group
                if sector:
                    params["sector"] = sector
                if country:
                    params["country"] = country
                if year:
                    params["year"] = year
                if month:
                    params["month"] = month
                data, err = await _get(
                    "/victims/",
                    "",
                    "filter victims",
                    params_pro=params,
                )
            if err:
                return err
            items = data if isinstance(data, list) else []
            # /victims/search has no date params - honour year/month locally
            # so the documented AND semantics hold for keyword + date searches
            # (the pure-filter /victims/ endpoint already filtered server-side).
            if query:
                items = [v for v in items if _match_date(v)]
        else:
            # Free tier: pick the most selective server endpoint, then apply the
            # remaining filters locally (AND logic) so multi-filter searches
            # still work without the PRO combined endpoints.
            if group:
                path = f"/groupvictims/{_seg(group)}"
                action = f"list victims for group {group}"
            elif country:
                path = f"/countryvictims/{_seg(country)}"
                action = f"list victims in country {country}"
            elif sector:
                path = f"/sectorvictims/{_seg(sector)}"
                action = f"list victims in sector {sector}"
            elif query:
                path = f"/searchvictims/{_seg(query)}"
                action = f"search victims for '{query}'"
            elif month:  # year + month
                path = f"/victims/{_seg(year)}/{_seg(month)}"
                action = f"list victims for {year}-{month}"
            else:
                # The free v2 date endpoint requires a month; sweeping all 12
                # months would burn the ~1 req/min rate limit. Ask the agent
                # to narrow instead of silently returning January only.
                return (
                    "Error: the free (unauthenticated) Ransomware.live v2 API "
                    "can only list victims for a specific year AND month "
                    f"(e.g. year={year}, month=03). Add 'month', combine "
                    "'year' with another filter (query/group/sector/country), "
                    "or configure a free PRO API key from "
                    "https://www.ransomware.live/my for whole-year searches."
                )
            data, err = await _get("", path, action)
            if err:
                return err
            items = data if isinstance(data, list) else []

            # Apply the remaining filters locally.
            def _match(v: dict) -> bool:
                if group and (v.get("group") or "").lower() != group.lower():
                    return False
                if sector:
                    # v2 responses carry the sector as 'activity' or 'sector'
                    # depending on the endpoint - check both (the formatter
                    # already falls back the same way).
                    sector_hay = (
                        f"{v.get('activity') or ''} {v.get('sector') or ''}"
                    ).lower()
                    if sector.lower() not in sector_hay:
                        return False
                if country and (v.get("country") or "").upper() != country:
                    return False
                if not _match_date(v):
                    return False
                if query:
                    hay = (
                        f"{v.get('victim', '')} {v.get('domain', '')} "
                        f"{v.get('website', '')}"
                    ).lower()
                    if query.lower() not in hay:
                        return False
                return True

            items = [v for v in items if _match(v)]

        key = "discovered" if order == "discovered" else "attackdate"
        items = sorted(
            items,
            key=lambda v: str(v.get(key) or v.get("published") or ""),
            reverse=True,
        )
        return _limit_victims(items, limit, "victim")

    async def get_ransomlive_victim(victim_id: str = "") -> str:
        """Get enriched detail for a single victim by its Base64 id (PRO only)."""
        victim_id = (victim_id or "").strip()
        if not victim_id:
            return (
                "Error: victim_id is required - the Base64-encoded id from a "
                "victim listing (the 'victim_id' field), e.g. "
                "'QWNtZUNvcnBAYWNtZWdyb3Vw'."
            )
        if not is_pro:
            return _pro_only("get_ransomlive_victim")
        # Guard against a malformed id producing a broken path (the id is
        # Base64; validate it decodes so we fail fast with a clear message).
        try:
            base64.b64decode(victim_id + "=" * (-len(victim_id) % 4), validate=True)
        except (ValueError, binascii.Error):
            return (
                f"Error: '{victim_id}' is not a valid Base64 victim id. Use the "
                "'victim_id' field returned by the victim listing/search tools."
            )
        data, err = await _get(
            f"/victim/{_seg(victim_id)}",
            "",
            f"get victim {victim_id}",
            expected_statuses={404},
        )
        if err:
            return err
        if not isinstance(data, dict) or not data:
            return f"No victim found for id '{victim_id}' (it may have been taken down)."
        lines = [
            f"Victim: {data.get('victim') or data.get('post_title') or '(unknown)'}",
            f"Group: {data.get('group') or data.get('group_name') or '?'}",
            f"Country: {data.get('country') or 'N/A'} | Sector: "
            f"{data.get('activity') or data.get('sector') or 'N/A'}",
            f"Attack date: {_fmt_date(data.get('attackdate') or data.get('published')) or 'N/A'} | "
            f"Discovered: {_fmt_date(data.get('discovered')) or 'N/A'}",
        ]
        if data.get("website") or data.get("domain"):
            lines.append(f"Website: {data.get('website') or data.get('domain')}")
        if data.get("description"):
            lines.append(f"\n{_trunc(data.get('description'), 800)}")
        if data.get("permalink") or data.get("url"):
            lines.append(f"\nPermalink: {data.get('permalink') or data.get('url')}")
        if data.get("press"):
            lines.append(f"Press: {data.get('press')}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # GROUPS
    # ══════════════════════════════════════════════════════════════════════

    async def list_ransomlive_groups(limit: int = 50) -> str:
        """List the tracked ransomware groups (alphabetical)."""
        limit = _clamp(limit, 1, 500, 50)
        data, err = await _get("/groups", "/groups", "list ransomware groups")
        if err:
            return err
        items = data if isinstance(data, list) else []
        total = len(items)
        shown = items[:limit]

        def _fmt(g: dict) -> str:
            name = g.get("group") or g.get("name") or "?"
            alt = g.get("altname")
            victims = g.get("victims")
            parts = [f"- {name}"]
            if alt:
                parts.append(f"(aka {alt})")
            if isinstance(victims, int):
                parts.append(f"- {victims} victims")
            return " ".join(parts)

        out = format_entity_list(
            shown,
            _fmt,
            noun="ransomware group",
            total=total,
            empty_msg="No ransomware groups found.",
        )
        if total > len(shown):
            out += (
                f"\n[Showing {len(shown)} of {total} groups - raise 'limit' to "
                "see more, or use get_ransomlive_group for a specific group.]"
            )
        return out

    async def get_ransomlive_group(group: str = "") -> str:
        """Get a ransomware group's profile (activity, leak sites, TTPs, CVEs)."""
        group = (group or "").strip()
        if not group:
            return (
                "Error: group is required (a group name, e.g. 'lockbit3', "
                "'blackcat', 'clop'). Use list_ransomlive_groups for the list."
            )
        data, err = await _get(
            f"/groups/{_seg(group)}",
            f"/group/{_seg(group)}",
            f"get group {group}",
            expected_statuses={404},
        )
        if err:
            return err
        if not isinstance(data, dict) or not data:
            return f"No group found named '{group}'. Check the spelling with list_ransomlive_groups."
        name = data.get("name") or data.get("group") or group
        lines = [f"Group: {name}"]
        if data.get("altname"):
            lines.append(f"Also known as: {data.get('altname')}")
        # Activity summary (PRO enriches with counts + first/last seen).
        summary = []
        if data.get("victims") is not None:
            summary.append(f"victims: {data.get('victims')}")
        if data.get("firstseen"):
            summary.append(f"first seen {_fmt_date(data.get('firstseen'))}")
        if data.get("lastseen"):
            summary.append(f"last seen {_fmt_date(data.get('lastseen'))}")
        if summary:
            lines.append("Activity: " + " | ".join(summary))
        if data.get("description"):
            lines.append(f"\n{_trunc(data.get('description'), 800)}")
        # Leak site locations.
        locations = data.get("locations") or []
        if isinstance(locations, list) and locations:
            live = [
                loc.get("fqdn") or loc.get("slug") or ""
                for loc in locations
                if isinstance(loc, dict)
            ]
            live = [x for x in live if x][:8]
            if live:
                lines.append("\nLeak sites: " + ", ".join(live))
        # MITRE TTPs (PRO).
        ttps = data.get("ttps") or []
        if isinstance(ttps, list) and ttps:
            names = []
            for t in ttps[:12]:
                if isinstance(t, dict):
                    names.append(t.get("id") or t.get("name") or "")
                else:
                    names.append(str(t))
            names = [n for n in names if n]
            if names:
                lines.append("MITRE TTPs: " + ", ".join(names))
        # Exploited CVEs (PRO).
        vulns = data.get("vulnerabilities") or []
        if isinstance(vulns, list) and vulns:
            cves = []
            for v in vulns[:12]:
                if isinstance(v, dict):
                    cves.append(v.get("cve") or v.get("id") or "")
                else:
                    cves.append(str(v))
            cves = [c for c in cves if c]
            if cves:
                lines.append("Exploited CVEs: " + ", ".join(cves))
        # Tools / malware families.
        tools = data.get("tools") or []
        if isinstance(tools, list) and tools:
            tnames = [
                (t.get("name") if isinstance(t, dict) else str(t)) for t in tools[:12]
            ]
            tnames = [t for t in tnames if t]
            if tnames:
                lines.append("Tools/malware: " + ", ".join(tnames))
        # Artifact availability flags (PRO), so the agent knows to drill in.
        avail = []
        if data.get("has_negotiations") or data.get("negotiation_count"):
            avail.append(
                f"negotiations ({data.get('negotiation_count', 'available')})"
            )
        if data.get("has_ransomnote") or data.get("ransomnotes_count"):
            avail.append(f"ransom notes ({data.get('ransomnotes_count', 'available')})")
        if avail:
            lines.append("Artifacts: " + ", ".join(avail))
        if data.get("url"):
            lines.append(f"\nProfile: {data.get('url')}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # PRESS / CYBERATTACKS
    # ══════════════════════════════════════════════════════════════════════

    async def list_ransomlive_press(
        country: str = "",
        year: str = "",
        month: str = "",
        limit: int = 25,
    ) -> str:
        """List cyberattack press coverage (optionally by country / date)."""
        country = (country or "").strip().upper()
        year = (year or "").strip()
        # Zero-pad once at the input boundary so the PRO /press/all `month`
        # param (documented 2-digit form, matching this tool's own "month=03"
        # examples) and the free-tier local filter always agree.
        month = (month or "").strip().zfill(2) if (month or "").strip() else ""
        limit = _clamp(limit, 1, _MAX_LIMIT, 25)
        if month and not year:
            return "Error: 'month' requires 'year' (e.g. year=2024, month=03)."

        if is_pro:
            if year:
                params = {"year": year}
                if month:
                    params["month"] = month
                if country:
                    params["country"] = country
                data, err = await _get(
                    "/press/all", "", "list press coverage", params_pro=params
                )
            else:
                params = {}
                if country:
                    params["country"] = country
                data, err = await _get(
                    "/press/recent", "", "list recent press", params_pro=params
                )
            if err:
                return err
            items = data if isinstance(data, list) else []
        else:
            # Free tier: /allcyberattacks when a date filter is requested (so we
            # can filter across the full set), else /recentcyberattacks.
            if year:
                data, err = await _get(
                    "", "/allcyberattacks", "list all cyberattacks"
                )
            else:
                data, err = await _get(
                    "", "/recentcyberattacks", "list recent cyberattacks"
                )
            if err:
                return err
            items = data if isinstance(data, list) else []

            def _match(p: dict) -> bool:
                if country and (p.get("country") or "").upper() != country:
                    return False
                if year:
                    dt = str(p.get("date") or p.get("added") or "")
                    if not dt.startswith(year):
                        return False
                    if month and dt[5:7] != month:
                        return False
                return True

            items = [p for p in items if _match(p)]

        items = sorted(
            items,
            key=lambda p: str(p.get("date") or p.get("added") or ""),
            reverse=True,
        )
        total = len(items)
        shown = items[:limit]

        def _fmt(p: dict) -> str:
            title = p.get("title") or "(untitled)"
            date = _fmt_date(p.get("date") or p.get("added"))
            country_code = p.get("country") or ""
            victim = p.get("victim") or ""
            gang = p.get("claim_gang") or ""
            head = f"- {title}"
            meta = []
            if date:
                meta.append(date)
            if country_code:
                meta.append(country_code)
            if victim:
                meta.append(f"victim: {victim}")
            if gang:
                meta.append(f"claimed by {gang}")
            out = head
            if meta:
                out += "\n    " + " | ".join(meta)
            if p.get("url"):
                out += f"\n    {p.get('url')}"
            return out

        out = format_entity_list(
            shown,
            _fmt,
            noun="press article",
            total=total,
            empty_msg="No press coverage matched.",
        )
        if total > len(shown):
            out += f"\n[Showing {len(shown)} of {total} - raise 'limit' to see more.]"
        return out

    # ══════════════════════════════════════════════════════════════════════
    # STATS / STATUS
    # ══════════════════════════════════════════════════════════════════════

    async def get_ransomlive_stats() -> str:
        """Get global dataset statistics (victim / group / press counts)."""
        data, err = await _get("/stats", "/info", "get statistics")
        if err:
            return err
        if not isinstance(data, dict):
            return "Ransomware.live returned no statistics."
        # PRO returns {stats: {victims, groups, press}, last_update}; the free
        # /info returns a flat {groups, victims}. Handle both shapes.
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else data
        lines = ["Ransomware.live dataset statistics:"]
        if stats.get("victims") is not None:
            lines.append(f"- Active victims: {stats.get('victims')}")
        if stats.get("groups") is not None:
            lines.append(f"- Tracked ransomware groups: {stats.get('groups')}")
        if stats.get("press") is not None:
            lines.append(f"- Press / cyberattack entries: {stats.get('press')}")
        if data.get("last_update"):
            lines.append(f"- Most recent victim added: {data.get('last_update')}")
        if len(lines) == 1:
            lines.append(f"(raw: {_trunc(data, 300)})")
        if not is_pro:
            lines.append(
                "\n(Free v2 tier: only group and victim counts are available. "
                "The PRO API adds press counts and the last-update timestamp.)"
            )
        return "\n".join(lines)

    async def get_ransomlive_status() -> str:
        """Check connectivity / credentials and report the active API tier."""
        if is_pro:
            data, err = await _get("/validate", "", "validate the API key")
            if err:
                return err
            client_id = ""
            if isinstance(data, dict):
                client_id = (
                    data.get("client")
                    or data.get("client_id")
                    or data.get("id")
                    or ""
                )
            return (
                "Connected to the Ransomware.live PRO API (X-API-KEY validated)."
                + (f" Client: {client_id}." if client_id else "")
                + " Quota: 500,000 requests/month."
            )
        data, err = await _get("", "/info", "check the free API")
        if err:
            return err
        groups = victims = "?"
        if isinstance(data, dict):
            groups = data.get("groups", "?")
            victims = data.get("victims", "?")
        return (
            "Connected to the Ransomware.live free (unauthenticated) v2 API "
            f"(~1 request/minute per endpoint). Tracking {groups} groups and "
            f"{victims} victims. For the full surface (per-victim detail, "
            "server-side search, whole-year date filters, press counts) "
            "configure a free PRO API key from https://www.ransomware.live/my."
        )

    # ══════════════════════════════════════════════════════════════════════
    # PRO-ONLY: REFERENCE DATA (sectors, CSIRT contacts, SEC 8-K filings)
    # ══════════════════════════════════════════════════════════════════════

    async def list_ransomlive_sectors() -> str:
        """List valid victim sector values with per-sector victim counts."""
        if not is_pro:
            return _pro_only("list_ransomlive_sectors")
        data, err = await _get("/listsectors", "", "list sectors")
        if err:
            return err
        rows: list[tuple[str, Any]] = []
        if isinstance(data, dict):
            # Either {sector: count} or {"sectors": [...]}.
            inner = data.get("sectors") if isinstance(data.get("sectors"), list) else None
            if inner is not None:
                for s in inner:
                    if isinstance(s, dict):
                        rows.append(
                            (s.get("sector") or s.get("name") or "?", s.get("count") or s.get("victims"))
                        )
                    else:
                        rows.append((str(s), None))
            else:
                rows = [(str(k), v) for k, v in data.items()]
        elif isinstance(data, list):
            for s in data:
                if isinstance(s, dict):
                    rows.append(
                        (s.get("sector") or s.get("name") or "?", s.get("count") or s.get("victims"))
                    )
                else:
                    rows.append((str(s), None))
        if not rows:
            return "No sectors returned."
        rows.sort(key=lambda r: str(r[0]).lower())
        lines = [f"Ransomware.live victim sectors ({len(rows)}):"]
        for name, count in rows:
            lines.append(f"- {name}" + (f" ({count} victims)" if count is not None else ""))
        lines.append(
            "\nUse any of these values as the 'sector' filter in "
            "search_ransomlive_victims."
        )
        return "\n".join(lines)

    async def get_ransomlive_csirt_contacts(country: str = "") -> str:
        """Get national CSIRT/CERT contacts for a country (ENISA + FIRST)."""
        if not is_pro:
            return _pro_only("get_ransomlive_csirt_contacts")
        country = (country or "").strip().upper()
        if not country:
            return (
                "Error: country is required - an ISO 3166-1 alpha-2 (e.g. FR) or "
                "alpha-3 (e.g. FRA) country code."
            )
        data, err = await _get(
            f"/csirt/{_seg(country)}",
            "",
            f"get CSIRT contacts for {country}",
            expected_statuses={404},
        )
        if err:
            return err
        items = data if isinstance(data, list) else (data.get("contacts") if isinstance(data, dict) else [])
        items = items or []
        if not items:
            return f"No CSIRT/CERT contacts found for country '{country}'."

        def _fmt(c: Any) -> str:
            if not isinstance(c, dict):
                return f"- {c}"
            name = c.get("name") or c.get("team") or c.get("official_name") or "?"
            parts = [f"- {name}"]
            detail = []
            for key in ("email", "website", "phone", "constituency", "country"):
                val = c.get(key)
                if val:
                    detail.append(f"{key}: {val}")
            out = parts[0]
            if detail:
                out += "\n    " + " | ".join(detail)
            return out

        return format_entity_list(
            items,
            _fmt,
            noun="CSIRT contact",
            empty_msg=f"No CSIRT contacts for {country}.",
        )

    async def list_ransomlive_8k_filings(
        ticker: str = "",
        cik: str = "",
        year: str = "",
        month: str = "",
        item105: bool = True,
        item801: bool = True,
        limit: int = 25,
    ) -> str:
        """List SEC Form 8-K cybersecurity incident filings (Item 1.05 / 8.01)."""
        if not is_pro:
            return _pro_only("list_ransomlive_8k_filings")
        ticker = (ticker or "").strip().upper()
        cik = (cik or "").strip()
        year = (year or "").strip()
        month = (month or "").strip()
        if month:
            month = month.zfill(2)
        limit = _clamp(limit, 1, _MAX_LIMIT, 25)
        if month and not year:
            return "Error: 'month' requires 'year' (e.g. year=2025, month=06)."
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if cik:
            params["cik"] = cik
        if year:
            params["year"] = year
        if month:
            params["month"] = month
        # Booleans are only meaningful when set to false (both default true).
        if not item105:
            params["item105"] = "false"
        if not item801:
            params["item801"] = "false"
        data, err = await _get("/8k", "", "list 8-K filings", params_pro=params)
        if err:
            return err
        items = data if isinstance(data, list) else (data.get("filings") if isinstance(data, dict) else [])
        items = items or []
        total = len(items)
        shown = items[:limit]

        def _fmt(f: dict) -> str:
            company = f.get("company") or f.get("name") or f.get("ticker") or "?"
            tkr = f.get("ticker") or ""
            date = _fmt_date(f.get("date") or f.get("filed") or f.get("filing_date"))
            item = f.get("item") or (
                "1.05"
                if f.get("item105")
                else ("8.01" if f.get("item801") else "")
            )
            head = f"- {company}" + (f" ({tkr})" if tkr and tkr != company else "")
            meta = []
            if date:
                meta.append(date)
            if item:
                meta.append(f"Item {item}")
            out = head
            if meta:
                out += " | " + " | ".join(meta)
            url = f.get("url") or f.get("link")
            if url:
                out += f"\n    {url}"
            return out

        out = format_entity_list(
            shown,
            _fmt,
            noun="8-K filing",
            total=total,
            empty_msg="No SEC 8-K cybersecurity filings matched.",
        )
        if total > len(shown):
            out += f"\n[Showing {len(shown)} of {total} - raise 'limit' or narrow filters.]"
        return out

    # ══════════════════════════════════════════════════════════════════════
    # PRO-ONLY: GROUP ARTIFACTS (IOCs, YARA, negotiations, ransom notes)
    # ══════════════════════════════════════════════════════════════════════

    async def get_ransomlive_iocs(
        group: str = "",
        type: str = "",  # matches the API's `type` query param
        limit: int = 50,
    ) -> str:
        """Get ransomware group IOCs (indicators of compromise)."""
        if not is_pro:
            return _pro_only("get_ransomlive_iocs")
        group = (group or "").strip()
        ioc_type = (type or "").strip().lower()
        limit = _clamp(limit, 1, 500, 50)
        if group:
            path = f"/iocs/{_seg(group)}"
            action = f"get IOCs for group {group}"
        else:
            path = "/iocs"
            action = "list groups with IOCs"
        params = {"type": ioc_type} if ioc_type else None
        data, err = await _get(
            path, "", action, params_pro=params, expected_statuses={404}
        )
        if err:
            return err
        if not group:
            # Inventory: groups with IOC-type breakdown.
            items = _inventory_items(data)

            def _fmt_group(g: Any) -> str:
                if not isinstance(g, dict):
                    return f"- {g}"
                name = g.get("group") or g.get("name") or "?"
                types = g.get("types") or g.get("iocs") or g.get("count")
                return f"- {name}" + (f": {types}" if types is not None else "")

            return format_entity_list(
                items,
                _fmt_group,
                noun="group with IOCs",
                plural="groups with IOCs",
                empty_msg="No groups with IOCs found.",
            )
        # Per-group indicators, usually keyed by type.
        lines = [f"IOCs for group '{group}'"]
        if ioc_type:
            lines[0] += f" (type: {ioc_type})"
        lines[0] += ":"
        shown_total = 0
        if isinstance(data, dict):
            # {md5: [...], ip: [...], ...} or {iocs: {...}}
            buckets = data.get("iocs") if isinstance(data.get("iocs"), dict) else data
            if isinstance(buckets, dict):
                for t, values in buckets.items():
                    if not isinstance(values, list) or not values:
                        continue
                    take = values[: min(limit, len(values))]
                    shown_total += len(take)
                    lines.append(f"\n{t} ({len(values)}):")
                    for v in take:
                        lines.append(f"  {v if not isinstance(v, dict) else v.get('value', v)}")
                    if len(values) > len(take):
                        lines.append(f"  ... and {len(values) - len(take)} more")
        elif isinstance(data, list):
            take = data[:limit]
            shown_total = len(take)
            for v in take:
                lines.append(f"  {v if not isinstance(v, dict) else v.get('value', v)}")
        if shown_total == 0:
            return f"No IOCs found for group '{group}'" + (
                f" of type '{ioc_type}'." if ioc_type else "."
            )
        return "\n".join(lines)

    async def get_ransomlive_yara(group: str = "") -> str:
        """Get YARA detection rules for a group, or list groups with rules."""
        if not is_pro:
            return _pro_only("get_ransomlive_yara")
        group = (group or "").strip()
        if not group:
            data, err = await _get("/yara", "", "list groups with YARA rules")
            if err:
                return err
            items = _inventory_items(data)

            def _fmt_group(g: Any) -> str:
                if not isinstance(g, dict):
                    return f"- {g}"
                name = g.get("group") or g.get("name") or "?"
                count = g.get("count") or g.get("rules")
                return f"- {name}" + (f" ({count} rules)" if count is not None else "")

            return format_entity_list(
                items,
                _fmt_group,
                noun="group with YARA rules",
                plural="groups with YARA rules",
                empty_msg="No groups with YARA rules found.",
            )
        data, err = await _get(
            f"/yara/{_seg(group)}",
            "",
            f"get YARA rules for group {group}",
            expected_statuses={404},
        )
        if err:
            return err
        rules = data if isinstance(data, list) else (data.get("rules") if isinstance(data, dict) else [])
        rules = rules or []
        if not rules:
            return f"No YARA rules found for group '{group}'."
        lines = [f"YARA rules for group '{group}' ({len(rules)}):"]
        for r in rules:
            if isinstance(r, dict):
                fname = r.get("filename") or r.get("name") or "rule"
                content = r.get("content") or ""
                lines.append(f"\n# {fname}\n{_trunc_block(content, 2000)}")
            else:
                lines.append(f"\n{_trunc_block(r, 2000)}")
        return "\n".join(lines)

    async def list_ransomlive_negotiations(group: str = "") -> str:
        """List groups with leaked negotiation chats, or chats for one group."""
        if not is_pro:
            return _pro_only("list_ransomlive_negotiations")
        group = (group or "").strip()
        if not group:
            data, err = await _get(
                "/negotiations", "", "list groups with negotiations"
            )
            if err:
                return err
            items = _inventory_items(data)

            def _fmt_group(g: Any) -> str:
                if not isinstance(g, dict):
                    return f"- {g}"
                name = g.get("group") or g.get("name") or "?"
                count = g.get("count") or g.get("chats")
                return f"- {name}" + (f" ({count} chats)" if count is not None else "")

            return format_entity_list(
                items,
                _fmt_group,
                noun="group with negotiations",
                plural="groups with negotiations",
                empty_msg="No groups with negotiation chats found.",
            )
        data, err = await _get(
            f"/negotiations/{_seg(group)}",
            "",
            f"list negotiation chats for group {group}",
            expected_statuses={404},
        )
        if err:
            return err
        chats = data if isinstance(data, list) else (data.get("chats") if isinstance(data, dict) else [])
        chats = chats or []
        if not chats:
            return f"No negotiation chats found for group '{group}'."

        def _fmt(c: dict) -> str:
            cid = c.get("id") or c.get("chat_id") or "?"
            msgs = c.get("message_count")
            initial = c.get("initialransom")
            negotiated = c.get("negotiatedransom")
            paid = c.get("paid")
            out = f"- chat_id: {cid}"
            meta = []
            if msgs is not None:
                meta.append(f"{msgs} messages")
            if initial:
                meta.append(f"initial demand {initial}")
            if negotiated:
                meta.append(f"negotiated {negotiated}")
            if paid is not None:
                meta.append(f"paid: {paid}")
            if meta:
                out += "\n    " + " | ".join(meta)
            return out

        out = format_entity_list(
            chats,
            _fmt,
            noun="negotiation chat",
            empty_msg=f"No negotiation chats for {group}.",
        )
        out += (
            "\nUse get_ransomlive_negotiation_chat with the group and a chat_id "
            "to read the full message thread."
        )
        return out

    async def get_ransomlive_negotiation_chat(
        group: str = "",
        chat_id: str = "",
        limit: int = 50,
    ) -> str:
        """Read the full message thread + ransom outcome for one chat."""
        if not is_pro:
            return _pro_only("get_ransomlive_negotiation_chat")
        group = (group or "").strip()
        chat_id = (chat_id or "").strip()
        if not group or not chat_id:
            return (
                "Error: both group and chat_id are required. Use "
                "list_ransomlive_negotiations with a group to discover chat ids."
            )
        limit = _clamp(limit, 1, 500, 50)
        data, err = await _get(
            f"/negotiations/{_seg(group)}/{_seg(chat_id)}",
            "",
            f"get negotiation chat {chat_id}",
            expected_statuses={404},
        )
        if err:
            return err
        if not isinstance(data, dict) or not data:
            return f"No negotiation chat '{chat_id}' found for group '{group}'."
        lines = [f"Negotiation chat {chat_id} (group: {group})"]
        meta = []
        if data.get("initialransom"):
            meta.append(f"initial demand: {data.get('initialransom')}")
        if data.get("negotiatedransom"):
            meta.append(f"negotiated: {data.get('negotiatedransom')}")
        if data.get("paid") is not None:
            meta.append(f"paid: {data.get('paid')}")
        if data.get("message_count") is not None:
            meta.append(f"{data.get('message_count')} messages")
        if meta:
            lines.append(" | ".join(meta))
        messages = data.get("messages") or []
        if isinstance(messages, list) and messages:
            take = messages[:limit]
            lines.append(f"\nMessages ({len(take)} of {len(messages)}):")
            for m in take:
                if isinstance(m, dict):
                    sender = m.get("party") or m.get("from") or m.get("sender") or "?"
                    ts = _fmt_date(m.get("timestamp") or m.get("date") or "")
                    body = m.get("message") or m.get("text") or m.get("body") or ""
                    prefix = f"  [{sender}]" + (f" {ts}" if ts else "")
                    lines.append(f"{prefix}: {_trunc(body, 500)}")
                else:
                    lines.append(f"  {_trunc(m, 500)}")
            if len(messages) > len(take):
                lines.append(
                    f"  ... and {len(messages) - len(take)} more messages (raise 'limit')."
                )
        return "\n".join(lines)

    async def list_ransomlive_ransomnotes(group: str = "") -> str:
        """List groups with ransom notes, or note identifiers for one group."""
        if not is_pro:
            return _pro_only("list_ransomlive_ransomnotes")
        group = (group or "").strip()
        if not group:
            data, err = await _get(
                "/ransomnotes", "", "list groups with ransom notes"
            )
            if err:
                return err
            items = _inventory_items(data)

            def _fmt_group(g: Any) -> str:
                if not isinstance(g, dict):
                    return f"- {g}"
                name = g.get("group") or g.get("name") or "?"
                count = g.get("count") or g.get("notes")
                return f"- {name}" + (f" ({count} notes)" if count is not None else "")

            return format_entity_list(
                items,
                _fmt_group,
                noun="group with ransom notes",
                plural="groups with ransom notes",
                empty_msg="No groups with ransom notes found.",
            )
        data, err = await _get(
            f"/ransomnotes/{_seg(group)}",
            "",
            f"list ransom notes for group {group}",
            expected_statuses={404},
        )
        if err:
            return err
        notes = data if isinstance(data, list) else (data.get("notes") if isinstance(data, dict) else [])
        notes = notes or []
        if not notes:
            return f"No ransom notes found for group '{group}'."
        names = [
            (n.get("name") or n.get("note_name") if isinstance(n, dict) else str(n))
            for n in notes
        ]
        names = [n for n in names if n]
        lines = [f"Ransom notes for group '{group}' ({len(names)}):"]
        for n in names:
            lines.append(f"- {n}")
        lines.append(
            "\nUse get_ransomlive_ransomnote with the group and a note name to "
            "read the full text."
        )
        return "\n".join(lines)

    async def get_ransomlive_ransomnote(group: str = "", note_name: str = "") -> str:
        """Get the full text of a specific ransom note."""
        if not is_pro:
            return _pro_only("get_ransomlive_ransomnote")
        group = (group or "").strip()
        note_name = (note_name or "").strip()
        if not group or not note_name:
            return (
                "Error: both group and note_name are required. Use "
                "list_ransomlive_ransomnotes with a group to discover note names."
            )
        data, err = await _get(
            f"/ransomnotes/{_seg(group)}/{_seg(note_name)}",
            "",
            f"get ransom note {note_name}",
            expected_statuses={404},
        )
        if err:
            return err
        if isinstance(data, dict):
            content = data.get("content") or ""
            ext = data.get("extension") or ""
        else:
            content = str(data or "")
            ext = ""
        if not content:
            return f"No content found for ransom note '{note_name}' (group '{group}')."
        header = f"Ransom note '{note_name}' (group: {group}"
        header += f", format: {ext})" if ext else ")"
        return f"{header}\n\n{_trunc_block(content, 4000)}"

    # ── Register ──────────────────────────────────────────────────────────

    executor.register("list_ransomlive_sectors", list_ransomlive_sectors)
    executor.register("get_ransomlive_iocs", get_ransomlive_iocs)
    executor.register("get_ransomlive_yara", get_ransomlive_yara)
    executor.register("list_ransomlive_negotiations", list_ransomlive_negotiations)
    executor.register(
        "get_ransomlive_negotiation_chat", get_ransomlive_negotiation_chat
    )
    executor.register("list_ransomlive_ransomnotes", list_ransomlive_ransomnotes)
    executor.register("get_ransomlive_ransomnote", get_ransomlive_ransomnote)
    executor.register("get_ransomlive_csirt_contacts", get_ransomlive_csirt_contacts)
    executor.register("list_ransomlive_8k_filings", list_ransomlive_8k_filings)

    executor.register("list_ransomlive_recent_victims", list_ransomlive_recent_victims)
    executor.register("search_ransomlive_victims", search_ransomlive_victims)
    executor.register("get_ransomlive_victim", get_ransomlive_victim)
    executor.register("list_ransomlive_groups", list_ransomlive_groups)
    executor.register("get_ransomlive_group", get_ransomlive_group)
    executor.register("list_ransomlive_press", list_ransomlive_press)
    executor.register("get_ransomlive_stats", get_ransomlive_stats)
    executor.register("get_ransomlive_status", get_ransomlive_status)

    return [
        ToolDefinition(
            name="list_ransomlive_recent_victims",
            description=(
                "List the most recently claimed ransomware victims from "
                "Ransomware.live (organizations posted on ransomware groups' "
                "leak/extortion sites). Use for monitoring, daily digests, or "
                "'who was hit recently?'. Returns victim name, ransomware group, "
                "country, sector, attack/discovery dates, website, and a "
                "victim_id for get_ransomlive_victim (PRO). Order by 'discovered' "
                "(when Ransomware.live first saw the listing) or 'attacked' "
                "(estimated attack date)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "order": {
                        "type": "string",
                        "enum": list(_ORDER_VALUES),
                        "default": "discovered",
                        "description": (
                            "Sort order: 'discovered' (default) or 'attacked'."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": "Max victims to return (1-100).",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_ransomlive_victims",
            description=(
                "Search claimed ransomware victims by keyword and/or structured "
                "filters. Prefer this over recent-victims when answering 'was "
                "company X claimed?', 'which victims in country Z?', or 'who did "
                "group Y hit?'. At least one filter is required. 'query' matches "
                "the victim name or website (substring); 'group', 'sector', and "
                "'country' (ISO alpha-2, e.g. US, FR) narrow further; 'year' "
                "(+ optional 'month' as 2 digits) filters by date. All filters "
                "combine with AND. Use list_ransomlive_groups for valid group "
                "names."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Keyword matched against victim name / website "
                            "(e.g. 'hospital', 'acme')."
                        ),
                    },
                    "group": {
                        "type": "string",
                        "description": "Ransomware group name (e.g. 'lockbit3').",
                    },
                    "sector": {
                        "type": "string",
                        "description": (
                            "Victim sector/industry, substring match (e.g. "
                            "'Healthcare', 'Manufacturing')."
                        ),
                    },
                    "country": {
                        "type": "string",
                        "description": "ISO 3166-1 alpha-2 country code (e.g. US, FR).",
                    },
                    "year": {"type": "string", "description": "4-digit year (e.g. 2024)."},
                    "month": {
                        "type": "string",
                        "description": "2-digit month, requires 'year' (e.g. 03).",
                    },
                    "order": {
                        "type": "string",
                        "enum": list(_ORDER_VALUES),
                        "default": "discovered",
                        "description": "Sort order: 'discovered' (default) or 'attacked'.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": "Max victims to return (1-100).",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_ransomlive_victim",
            description=(
                "Get enriched detail for a single ransomware victim by its "
                "Base64 victim_id (from the listing/search tools). Returns the "
                "full description, sector, country, attack/discovery dates, "
                "website, permalink, and any linked press. PRO API only - on the "
                "free tier it returns guidance to configure a PRO key."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "victim_id": {
                        "type": "string",
                        "description": (
                            "Base64-encoded victim id (the 'victim_id' field "
                            "from a victim listing)."
                        ),
                    },
                },
                "required": ["victim_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_ransomlive_groups",
            description=(
                "List the ransomware groups tracked by Ransomware.live "
                "(alphabetical), each with its name, alternative name, and "
                "victim count. Use to discover group names before calling "
                "get_ransomlive_group or filtering victims by group."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max groups to return (1-500).",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_ransomlive_group",
            description=(
                "Get a ransomware group's intelligence profile: description, "
                "activity summary (victim count, first/last seen), known leak "
                "site URLs, MITRE ATT&CK TTPs, exploited CVEs, tools/malware "
                "families, and whether negotiation chats / ransom notes are "
                "available (PRO). Use to answer 'what is group Y and how does it "
                "operate?'. Richer on the PRO API; the free tier returns the "
                "core profile."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group": {
                        "type": "string",
                        "description": (
                            "Ransomware group name, case-insensitive (e.g. "
                            "'lockbit3', 'blackcat', 'clop')."
                        ),
                    },
                },
                "required": ["group"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_ransomlive_press",
            description=(
                "List cyberattack press coverage tracked by Ransomware.live "
                "(news articles about incidents, enriched with a ransomware link "
                "where the victim domain matches a known victim). Filter by "
                "country (ISO alpha-2) and/or year (+ optional month). Use to "
                "surface public reporting on incidents or victims. Returns "
                "title, date, country, victim, claiming group, and article URL."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "country": {
                        "type": "string",
                        "description": "ISO 3166-1 alpha-2 country code (e.g. FR, US).",
                    },
                    "year": {"type": "string", "description": "4-digit year (e.g. 2024)."},
                    "month": {
                        "type": "string",
                        "description": "2-digit month, requires 'year' (e.g. 03).",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": "Max articles to return (1-100).",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_ransomlive_stats",
            description=(
                "Get global Ransomware.live dataset statistics: total active "
                "victims, number of tracked ransomware groups, press/cyberattack "
                "entry count (PRO), and the most-recent-victim timestamp (PRO). "
                "Use for a high-level landscape overview. The free tier returns "
                "group and victim counts only."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_ransomlive_status",
            description=(
                "Check Ransomware.live connectivity and report the active API "
                "tier. On the PRO tier it validates the X-API-KEY and returns "
                "the client id + monthly quota; on the free tier it confirms the "
                "unauthenticated v2 API is reachable and reports its rate limit. "
                "Use to diagnose configuration/auth issues."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_ransomlive_sectors",
            description=(
                "List the valid victim sector/industry values (with per-sector "
                "victim counts) used by Ransomware.live. Use to discover the "
                "exact sector strings before filtering with "
                "search_ransomlive_victims(sector=...). PRO API only."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_ransomlive_iocs",
            description=(
                "Get indicators of compromise (IOCs) published for ransomware "
                "groups. Omit 'group' to list which groups have IOCs (with a "
                "type breakdown); pass a group to retrieve its actual indicators "
                "(hashes, IPs, domains, emails, Bitcoin addresses, URLs), "
                "optionally narrowed to one 'type'. Use to enrich detections or "
                "seed blocklists. PRO API only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group": {
                        "type": "string",
                        "description": (
                            "Ransomware group name (e.g. 'lockbit3'). Omit to "
                            "list groups that have IOCs."
                        ),
                    },
                    "type": {
                        "type": "string",
                        "description": (
                            "Optional IOC type filter (e.g. md5, sha256, ip, "
                            "domain, email, btc, url)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max indicators per type to show (1-500).",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_ransomlive_yara",
            description=(
                "Get YARA detection rules associated with ransomware groups. "
                "Omit 'group' to list which groups have YARA rules (with counts); "
                "pass a group to retrieve the full rule content (deployable with "
                "a YARA scanner). PRO API only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group": {
                        "type": "string",
                        "description": (
                            "Ransomware group name (e.g. 'blackcat'). Omit to "
                            "list groups that have YARA rules."
                        ),
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_ransomlive_negotiations",
            description=(
                "List leaked ransom negotiation chats. Omit 'group' to list "
                "which groups have negotiation logs (with chat counts); pass a "
                "group to list its individual chats with ransom demand / "
                "negotiated amount / paid status and each chat_id. Follow up "
                "with get_ransomlive_negotiation_chat to read a thread. PRO API "
                "only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group": {
                        "type": "string",
                        "description": (
                            "Ransomware group name. Omit to list groups that "
                            "have negotiation chats."
                        ),
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_ransomlive_negotiation_chat",
            description=(
                "Read the complete message thread and ransom outcome (initial "
                "demand, negotiated amount, paid flag) for one leaked "
                "negotiation chat. Requires the group and a chat_id from "
                "list_ransomlive_negotiations. Use to study a group's "
                "negotiation tactics and payment outcomes. PRO API only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group": {"type": "string", "description": "Ransomware group name."},
                    "chat_id": {
                        "type": "string",
                        "description": (
                            "Chat identifier from list_ransomlive_negotiations."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max messages to show (1-500).",
                    },
                },
                "required": ["group", "chat_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_ransomlive_ransomnotes",
            description=(
                "List ransom notes. Omit 'group' to list which groups have "
                "ransom notes (with counts); pass a group to list its note "
                "identifiers. Follow up with get_ransomlive_ransomnote to read "
                "the full text. PRO API only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group": {
                        "type": "string",
                        "description": (
                            "Ransomware group name. Omit to list groups that "
                            "have ransom notes."
                        ),
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_ransomlive_ransomnote",
            description=(
                "Get the full text of a specific ransom note (identified by "
                "group + note name from list_ransomlive_ransomnotes). Long notes "
                "are truncated for context. Use to review a group's extortion "
                "messaging and payment instructions. PRO API only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group": {"type": "string", "description": "Ransomware group name."},
                    "note_name": {
                        "type": "string",
                        "description": (
                            "Note identifier from list_ransomlive_ransomnotes."
                        ),
                    },
                },
                "required": ["group", "note_name"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_ransomlive_csirt_contacts",
            description=(
                "Get national CSIRT/CERT contacts for a country (name, email, "
                "website, phone, constituency), sourced from ENISA and FIRST. "
                "Use to route an incident to the right national response team. "
                "Accepts ISO 3166-1 alpha-2 (e.g. FR) or alpha-3 (e.g. FRA). "
                "PRO API only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "country": {
                        "type": "string",
                        "description": (
                            "ISO 3166-1 country code, alpha-2 (FR) or alpha-3 (FRA)."
                        ),
                    },
                },
                "required": ["country"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_ransomlive_8k_filings",
            description=(
                "List SEC Form 8-K cybersecurity incident filings tracked by "
                "Ransomware.live: Item 1.05 (Material Cybersecurity Incident, "
                "mandatory since Dec 2023) and Item 8.01 (Other Events, used for "
                "cyber disclosures before 1.05). Filter by ticker, SEC CIK, year, "
                "and month, and toggle either item type. Use to correlate "
                "public-company breach disclosures with ransomware activity. PRO "
                "API only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Stock ticker symbol (e.g. MSFT, CRM).",
                    },
                    "cik": {
                        "type": "string",
                        "description": "SEC CIK code (e.g. 0001234567).",
                    },
                    "year": {"type": "string", "description": "4-digit filing year (e.g. 2025)."},
                    "month": {
                        "type": "string",
                        "description": "2-digit filing month, requires 'year' (e.g. 06).",
                    },
                    "item105": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include Item 1.05 (Material Cybersecurity Incident) filings.",
                    },
                    "item801": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include Item 8.01 (Other Events) filings.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": "Max filings to return (1-100).",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
