# SPDX-License-Identifier: Apache-2.0

"""Vulnerability Intelligence integration - NVD CVE + CISA KEV + FIRST EPSS.

A single provider aggregating the three authoritative FREE vulnerability data
sources so agents can answer "is this CVE exploited in the wild and how urgent
is it?" without a commercial subscription:

  - **NVD CVE API 2.0** (NIST National Vulnerability Database,
    ``https://services.nvd.nist.gov/rest/json/cves/2.0``): CVE lookup by id,
    keyword / CPE / severity / CWE search, recent CVEs, CVSS v2/v3/v4 metrics,
    weaknesses, configurations, references, and per-CVE change history. An
    OPTIONAL free API key (``apiKey`` request header) raises the rate limit
    from 5 requests / rolling 30 s to 50 requests / rolling 30 s. The strict
    NVD budget is enforced client-side with the shared token-bucket rate
    limiter so parallel tool calls can never breach it.

  - **CISA Known Exploited Vulnerabilities (KEV) catalog**
    (``https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json``):
    the authoritative "actively exploited in the wild" list, with the BOD 22-01
    federal remediation due date and the known-ransomware-campaign-use flag per
    entry. Unauthenticated single-feed download, cached in-process with a TTL
    and stale-serving fallback (mirrors the ``mitre_attack`` dataset cache).

  - **FIRST.org EPSS API** (``https://api.first.org/data/v1/epss``): the
    Exploit Prediction Scoring System - per-CVE probability of exploitation in
    the next 30 days plus the relative percentile, batch scoring (up to 100
    CVEs per call), historical/as-of-date scores, 30-day time series, and
    top-N / threshold queries. Unauthenticated.

The main tool is ``assess_vulnintel_cve``: it queries all three sources
concurrently for one CVE and merges them into a single deterministic urgency
verdict (KEV membership beats everything, then EPSS probability, then CVSS).

Auth model: the integration works with NO credential (``auth_type="none"``,
represented by the ``__no_auth__`` sentinel) or with an optional NVD API key
(``auth_type="api_key"``) that only changes the NVD rate budget - KEV and EPSS
never require a key and the tool SET is identical across both modes.

All three sources are read-only (no mutation endpoints exist), so every tool
is a read tool (priority 0).

NVD attribution: this product uses data from the NVD API but is not endorsed
or certified by the NVD.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    PLATFORM_USER_AGENT,
    app_token_cache_key,
    format_entity_list,
    get_shared_rate_limiter,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "Vulnerability Intelligence"
_SOURCE = "integration:vuln_intel"

# Data source endpoints (all free; only NVD accepts an optional API key).
NVD_CVE_BASE = "https://services.nvd.nist.gov/rest/json/cves/2.0"
NVD_HISTORY_BASE = "https://services.nvd.nist.gov/rest/json/cvehistory/2.0"
KEV_FEED_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
)
EPSS_BASE = "https://api.first.org/data/v1/epss"

# Sentinel produced by _get_token for auth_type="none" integrations - selects
# the keyless NVD tier. The catalog introspects with "__dummy__", which also
# resolves to keyless (no network call is made during introspection and the
# tool set is identical across tiers anyway).
_NO_AUTH_SENTINEL = "__no_auth__"  # not a secret, a tier marker
_DUMMY_TOKEN = "__dummy__"  # noqa: S105 - catalog introspection marker

_DEFAULT_TIMEOUT = 30
# The NVD API can be slow on cold queries - give it a longer budget.
_NVD_TIMEOUT = 60

# NVD enforces a rolling 30 s window: 5 requests without a key, 50 with one.
# The token bucket paces request STARTS so a parallel burst can never breach
# the cap: worst case in any 30 s window is ~capacity + rate * 30, so both
# configurations stay strictly under their budget.
_NVD_PUBLIC_RATE = 4.0 / 30.0  # + capacity 1  -> <= 5 requests / 30 s
_NVD_PUBLIC_CAPACITY = 1.0
_NVD_KEYED_RATE = 45.0 / 30.0  # + capacity 4  -> <= 49 requests / 30 s
_NVD_KEYED_CAPACITY = 4.0

# NVD date-range parameters accept at most 120 consecutive days.
_NVD_MAX_RANGE_DAYS = 120
# NVD hard-caps resultsPerPage at 2000; tools clamp far lower for readability.
_MAX_LIMIT = 50
# EPSS batch lookups accept up to 100 comma-separated CVE ids.
_EPSS_MAX_BATCH = 100

_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE)
_CVSS_SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

# NVD attribution notice (required by NIST for products using the NVD API).
_NVD_ATTRIBUTION = (
    "This product uses data from the NVD API but is not endorsed or certified "
    "by the NVD."
)


# ---------------------------------------------------------------------------
# Credential / header / URL chokepoints (shared with the connectivity test)
# ---------------------------------------------------------------------------


def resolve_nvd_key(token: str | None) -> str:
    """Resolve the optional NVD API key from the runtime token.

    Returns the key, or ``""`` when the integration runs keyless. A blank
    token, the ``__no_auth__`` sentinel, and the catalog ``__dummy__`` marker
    all resolve to keyless - the tool set is identical either way, only the
    NVD rate budget differs.
    """
    key = (token or "").strip()
    if not key or key in (_NO_AUTH_SENTINEL, _DUMMY_TOKEN):
        return ""
    return key


def build_vulnintel_headers(token: str | None) -> dict[str, str]:
    """Build the request headers for a token (single header chokepoint).

    The NVD API key rides in the ``apiKey`` header (case-sensitive value);
    keyless requests carry only the JSON Accept + a UA. KEV and EPSS ignore
    the extra header, so one header set safely serves all three sources.
    """
    headers = {"Accept": "application/json", "User-Agent": PLATFORM_USER_AGENT}
    api_key = resolve_nvd_key(token)
    if api_key:
        headers["apiKey"] = api_key
    return headers


def vulnintel_status_url(token: str | None) -> str:
    """Return the tier-appropriate connectivity-probe URL for a token.

    With an NVD API key the probe validates the key against the NVD CVE API
    (a 1-result page); keyless integrations probe the tiny unauthenticated
    EPSS endpoint instead of burning the strict public NVD budget.
    """
    if resolve_nvd_key(token):
        return f"{NVD_CVE_BASE}?resultsPerPage=1"
    return f"{EPSS_BASE}?cve=CVE-2021-44228"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


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


def _normalize_cve(value: str) -> str:
    """Normalize a CVE id to its canonical upper-case form, or ``""``."""
    candidate = (value or "").strip().upper()
    return candidate if _CVE_RE.match(candidate) else ""


def _fmt_date(value: Any) -> str:
    """Trim an ISO timestamp to date precision for compact output."""
    s = str(value or "").strip()
    return s[:10] if len(s) >= 10 else s


def _nvd_datetime(dt: datetime) -> str:
    """Format a datetime in the extended ISO-8601 form the NVD expects (UTC).

    The offset designator is optional per the NVD docs (offset-less values
    default to GMT), but the explicit ``Z`` removes any ambiguity and matches
    the documented ``...000Z`` example form.
    """
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _nvd_url(base: str, params: dict[str, Any], flags: list[str] | None = None) -> str:
    """Build an NVD request URL with regular params plus bare flag params.

    NVD flag parameters (``hasKev``, ``noRejected``, ``keywordExactMatch``)
    are documented as "provided without a parameter value", so they cannot go
    through a params dict - append them verbatim after the encoded pairs.
    """
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    for flag in flags or []:
        query = f"{query}&{flag}" if query else flag
    return f"{base}?{query}" if query else base


def _to_float(value: Any) -> float | None:
    """Parse EPSS decimal strings ("0.97543") defensively."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# NVD record formatting
# ---------------------------------------------------------------------------


def _en_description(cve: dict) -> str:
    descriptions = cve.get("descriptions") or []
    for entry in descriptions:
        if (entry or {}).get("lang") == "en":
            return str(entry.get("value") or "")
    if descriptions:
        return str((descriptions[0] or {}).get("value") or "")
    return ""


def _best_cvss(cve: dict) -> tuple[float | None, str, str]:
    """Return ``(score, severity, version)`` preferring the newest CVSS."""
    metrics = cve.get("metrics") or {}
    for key, version in (
        ("cvssMetricV40", "4.0"),
        ("cvssMetricV31", "3.1"),
        ("cvssMetricV30", "3.0"),
        ("cvssMetricV2", "2.0"),
    ):
        rows = metrics.get(key) or []
        if not rows:
            continue
        row = rows[0] or {}
        data = row.get("cvssData") or {}
        score = data.get("baseScore")
        severity = data.get("baseSeverity") or row.get("baseSeverity") or ""
        if score is not None:
            try:
                return float(score), str(severity), version
            except (TypeError, ValueError):
                continue
    return None, "", ""


def _cvss_lines(cve: dict) -> list[str]:
    """All available CVSS vectors, newest version first."""
    metrics = cve.get("metrics") or {}
    lines: list[str] = []
    for key, version in (
        ("cvssMetricV40", "4.0"),
        ("cvssMetricV31", "3.1"),
        ("cvssMetricV30", "3.0"),
        ("cvssMetricV2", "2.0"),
    ):
        rows = metrics.get(key) or []
        if not rows:
            continue
        row = rows[0] or {}
        data = row.get("cvssData") or {}
        score = data.get("baseScore")
        severity = data.get("baseSeverity") or row.get("baseSeverity") or ""
        vector = data.get("vectorString") or ""
        if score is None:
            continue
        line = f"  CVSS v{version}: {score}"
        if severity:
            line += f" {severity}"
        if vector:
            line += f" ({vector})"
        lines.append(line)
    return lines


def _cwe_ids(cve: dict) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for weakness in cve.get("weaknesses") or []:
        for desc in (weakness or {}).get("description") or []:
            value = (desc or {}).get("value") or ""
            if value and value not in seen:
                seen.add(value)
                out.append(value)
    return out


def _vulnerable_cpes(cve: dict, cap: int = 5) -> tuple[list[str], int]:
    """First *cap* vulnerable CPE criteria plus the total count."""
    found: list[str] = []
    total = 0
    for config in cve.get("configurations") or []:
        for node in (config or {}).get("nodes") or []:
            for match in (node or {}).get("cpeMatch") or []:
                if not (match or {}).get("vulnerable"):
                    continue
                total += 1
                if len(found) < cap:
                    found.append(str(match.get("criteria") or "?"))
    return found, total


def _fmt_cve_summary(cve: dict) -> str:
    """One-line-ish CVE summary with the ids the agent needs for follow-up."""
    cve_id = cve.get("id") or "?"
    score, severity, version = _best_cvss(cve)
    head = f"- {cve_id}"
    meta: list[str] = []
    if score is not None:
        meta.append(f"CVSS {score} {severity or ''} (v{version})".replace("  ", " "))
    if cve.get("published"):
        meta.append(f"published {_fmt_date(cve.get('published'))}")
    if cve.get("vulnStatus"):
        meta.append(str(cve.get("vulnStatus")))
    if cve.get("cisaExploitAdd"):
        meta.append("IN CISA KEV")
    out = head
    if meta:
        out += " | " + " | ".join(meta)
    desc = _trunc(_en_description(cve), 200)
    if desc:
        out += f"\n    {desc}"
    return out


# ---------------------------------------------------------------------------
# CISA KEV catalog cache (single feed, TTL + stale-serving fallback)
# ---------------------------------------------------------------------------

#: Refresh the KEV feed after this many seconds (CISA updates the catalog
#: within minutes of changes, but an hour of staleness is acceptable for the
#: prioritization use case and avoids re-downloading the ~3 MB feed per call).
_KEV_CACHE_TTL_SECONDS = 60 * 60
#: Back-off before retrying a failed refresh while serving a stale cache.
_KEV_RETRY_DELAY_SECONDS = 5 * 60


@dataclass
class _KevStore:
    """Parsed KEV catalog with a by-CVE index."""

    entries: list[dict[str, Any]]
    by_cve: dict[str, dict[str, Any]]
    catalog_version: str
    date_released: str
    fetched_at: float = field(default_factory=time.monotonic)
    retry_at: float = 0.0

    @property
    def needs_refresh(self) -> bool:
        age = time.monotonic() - self.fetched_at
        if age <= _KEV_CACHE_TTL_SECONDS:
            return False
        return time.monotonic() >= self.retry_at

    def defer_retry(self) -> None:
        self.retry_at = time.monotonic() + _KEV_RETRY_DELAY_SECONDS


# Single-slot in-process cache (a dict mirrors the mitre_attack store cache
# and avoids rebinding a module global from inside the loader).
_KEV_CACHE: dict[str, _KevStore] = {}
_KEV_CACHE_KEY = "kev"
_KEV_LOCK = asyncio.Lock()


def _parse_kev_payload(payload: Any) -> _KevStore | None:
    if not isinstance(payload, dict):
        return None
    raw_entries = payload.get("vulnerabilities")
    if not isinstance(raw_entries, list):
        return None
    entries = [e for e in raw_entries if isinstance(e, dict)]
    by_cve = {
        str(e.get("cveID") or "").upper(): e for e in entries if e.get("cveID")
    }
    return _KevStore(
        entries=entries,
        by_cve=by_cve,
        catalog_version=str(payload.get("catalogVersion") or "?"),
        date_released=str(payload.get("dateReleased") or "?"),
    )


async def load_kev_catalog() -> tuple[_KevStore | None, str | None]:
    """Return the cached (or freshly downloaded) KEV catalog.

    On refresh failure a stale cached copy is served (with a retry back-off)
    rather than failing the tool - the KEV list changes slowly enough that a
    stale catalog still beats no answer.
    """
    cached = _KEV_CACHE.get(_KEV_CACHE_KEY)
    if cached and not cached.needs_refresh:
        return cached, None
    async with _KEV_LOCK:
        cached = _KEV_CACHE.get(_KEV_CACHE_KEY)
        if cached and not cached.needs_refresh:
            return cached, None
        async with egress_client(
            timeout=_NVD_TIMEOUT, follow_redirects=True
        ) as client:
            resp, err = await safe_request(
                client,
                "get",
                KEV_FEED_URL,
                _PROVIDER,
                "download the CISA KEV catalog",
                headers={"Accept": "application/json", "User-Agent": PLATFORM_USER_AGENT},
            )
        if err or resp is None:
            if cached:
                cached.defer_retry()
                return cached, None
            return None, err or f"Error: {_PROVIDER} could not download the KEV catalog."
        try:
            store = _parse_kev_payload(resp.json())
        except ValueError:
            store = None
        if store is None:
            if cached:
                cached.defer_retry()
                return cached, None
            return None, (
                f"Error: {_PROVIDER} received an unexpected CISA KEV catalog "
                "payload (not the documented JSON feed shape)."
            )
        _KEV_CACHE[_KEV_CACHE_KEY] = store
        return store, None


def _fmt_kev_entry(entry: dict[str, Any], *, today: Any = None) -> str:
    cve_id = entry.get("cveID") or "?"
    vendor = entry.get("vendorProject") or "?"
    product = entry.get("product") or "?"
    lines = [f"- {cve_id} | {vendor} {product}"]
    name = _trunc(entry.get("vulnerabilityName"), 160)
    if name:
        lines.append(f"    {name}")
    meta: list[str] = []
    if entry.get("dateAdded"):
        meta.append(f"added {entry.get('dateAdded')}")
    due = str(entry.get("dueDate") or "")
    if due:
        marker = ""
        if today is not None:
            try:
                due_date = datetime.strptime(due, "%Y-%m-%d").date()
                marker = " (OVERDUE)" if due_date < today else ""
            except ValueError:
                marker = ""
        meta.append(f"due {due}{marker}")
    if str(entry.get("knownRansomwareCampaignUse") or "") == "Known":
        meta.append("used in RANSOMWARE campaigns")
    if meta:
        lines.append("    " + " | ".join(meta))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Integration class
# ---------------------------------------------------------------------------


class VulnIntelIntegration(BaseIntegration):
    """Vulnerability Intelligence (NVD + CISA KEV + FIRST EPSS) integration."""

    provider_name = "vuln_intel"
    display_name = "Vulnerability Intelligence"
    icon = "ShieldAlert"
    description = (
        "Aggregated vulnerability intelligence from the three authoritative "
        "free sources - NVD CVE 2.0 (CVE lookup and search, CVSS v2/v3/v4, "
        "CWE, affected products, change history), the CISA Known Exploited "
        "Vulnerabilities catalog (actively-exploited verdicts, remediation "
        "due dates, ransomware campaign usage), and FIRST.org EPSS "
        "(exploitation probability scores and percentiles). Includes a "
        "one-call CVE urgency assessment that merges all three sources. "
        "Works without credentials; an optional free NVD API key raises the "
        "NVD rate limit."
    )
    auth_types = ["api_key", "none"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        """Validate credentials / reachability for the resolved tier.

        With an NVD API key: a 1-result CVE page must return 200 (an invalid
        key yields 403/404). Keyless: the tiny unauthenticated EPSS probe
        must return 200. Never raises - returns ``False`` on any failure.
        """
        try:
            async with egress_client(
                timeout=_NVD_TIMEOUT, follow_redirects=False
            ) as client:
                resp = await client.get(
                    vulnintel_status_url(token),
                    headers=build_vulnintel_headers(token),
                )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def get_user_info(self, token: str) -> dict:
        """Return a small tier descriptor for display (no network call)."""
        api_key = resolve_nvd_key(token)
        return {
            "tier": "nvd_api_key" if api_key else "public",
            "sources": "NVD CVE 2.0, CISA KEV, FIRST EPSS",
            "nvd_rate_limit": (
                "50 requests / 30s" if api_key else "5 requests / 30s"
            ),
        }


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build the Vulnerability Intelligence tool set (all read-only).

    The tools always build (the catalog introspects with a dummy token); the
    optional NVD key only changes the rate budget at call time.
    """
    api_key = resolve_nvd_key(token)
    headers = build_vulnintel_headers(token)

    def _nvd_limiter():
        # One process-wide bucket per NVD credential: every concurrent caller
        # using the same key (or the public tier) shares the same budget. The
        # key is hashed so the limiter registry never stores the secret.
        return get_shared_rate_limiter(
            f"nvd:{app_token_cache_key('nvd', api_key or 'public')}",
            rate=_NVD_KEYED_RATE if api_key else _NVD_PUBLIC_RATE,
            capacity=_NVD_KEYED_CAPACITY if api_key else _NVD_PUBLIC_CAPACITY,
        )

    async def _nvd_get(url: str, action: str) -> tuple[dict | None, str | None]:
        """Rate-limited NVD GET returning ``(json, error)``."""
        async with _nvd_limiter().slot(), egress_client(
            timeout=_NVD_TIMEOUT, follow_redirects=False
        ) as client:
            resp, err = await safe_request(
                client, "get", url, _PROVIDER, action, headers=headers
            )
        if err or resp is None:
            return None, err
        try:
            return resp.json(), None
        except ValueError:
            return None, (
                f"Error: {_PROVIDER} (NVD) returned a non-JSON response while "
                f"trying to {action}."
            )

    async def _epss_get(
        params: dict[str, Any], action: str
    ) -> tuple[dict | None, str | None]:
        """EPSS GET returning ``(json, error)``."""
        async with egress_client(
            timeout=_DEFAULT_TIMEOUT, follow_redirects=False
        ) as client:
            resp, err = await safe_request(
                client,
                "get",
                EPSS_BASE,
                _PROVIDER,
                action,
                headers={"Accept": "application/json", "User-Agent": PLATFORM_USER_AGENT},
                params={k: v for k, v in params.items() if v not in (None, "")},
            )
        if err or resp is None:
            return None, err
        try:
            return resp.json(), None
        except ValueError:
            return None, (
                f"Error: {_PROVIDER} (EPSS) returned a non-JSON response while "
                f"trying to {action}."
            )

    async def _nvd_cve_page(
        params: dict[str, Any],
        flags: list[str],
        limit: int,
        offset_from_newest: int,
        action: str,
    ) -> tuple[list[dict] | None, int, str | None]:
        """Fetch a newest-first page of CVE records for a filtered query.

        The NVD sorts results ASCENDING by published date, so a plain first
        page returns the OLDEST matches - useless for monitoring. This helper
        probes ``totalResults`` with a 1-result request, then fetches the tail
        page and reverses it, giving deterministic newest-first output.
        ``offset_from_newest`` skips the N most recent records (pagination).
        Returns ``(records, total_results, error)``.
        """
        probe_url = _nvd_url(
            NVD_CVE_BASE, {**params, "resultsPerPage": 1, "startIndex": 0}, flags
        )
        probe, err = await _nvd_get(probe_url, f"{action} (count probe)")
        if err:
            return None, 0, err
        total = int((probe or {}).get("totalResults") or 0)
        if total == 0:
            return [], 0, None
        start = max(total - limit - offset_from_newest, 0)
        # Clamp the page so an offset past the oldest record returns the
        # remainder instead of duplicating the first page.
        page_size = min(limit, max(total - offset_from_newest, 0))
        if page_size <= 0:
            return [], total, None
        page_url = _nvd_url(
            NVD_CVE_BASE,
            {**params, "resultsPerPage": page_size, "startIndex": start},
            flags,
        )
        data, err = await _nvd_get(page_url, action)
        if err:
            return None, total, err
        records = [
            (v or {}).get("cve") or {}
            for v in (data or {}).get("vulnerabilities") or []
        ]
        records.reverse()  # ascending by published -> newest first
        return records, total, None

    def _pagination_hint(shown: int, offset: int, total: int) -> str:
        remaining = total - offset - shown
        if remaining <= 0:
            return ""
        return (
            f"\n[{remaining} older result{'s' if remaining != 1 else ''} "
            f"available - pass start_index={offset + shown} to page further "
            "back.]"
        )

    async def _fetch_nvd_record(cve: str) -> tuple[dict | None, str | None]:
        """Fetch one CVE record by id; ``(None, None)`` when unknown."""
        url = _nvd_url(NVD_CVE_BASE, {"cveId": cve})
        data, err = await _nvd_get(url, f"look up {cve}")
        if err:
            return None, err
        vulns = (data or {}).get("vulnerabilities") or []
        if not vulns:
            return None, None
        return (vulns[0] or {}).get("cve") or {}, None

    # ══════════════════════════════════════════════════════════════════════
    # MERGED ASSESSMENT (the main tool)
    # ══════════════════════════════════════════════════════════════════════

    async def assess_vulnintel_cve(cve: str = "") -> str:
        """Merged NVD + CISA KEV + EPSS urgency assessment for one CVE."""
        cve = _normalize_cve(cve)
        if not cve:
            return (
                "Error: cve is required and must be a valid CVE id (e.g. "
                "CVE-2024-3400)."
            )
        (nvd_record, nvd_err), (kev_store, kev_err), (epss_data, epss_err) = (
            await asyncio.gather(
                _fetch_nvd_record(cve),
                load_kev_catalog(),
                _epss_get({"cve": cve}, f"get the EPSS score for {cve}"),
            )
        )

        kev_entry = kev_store.by_cve.get(cve) if kev_store else None
        epss_score: float | None = None
        epss_percentile: float | None = None
        if epss_data:
            rows = epss_data.get("data") or []
            if rows:
                epss_score = _to_float((rows[0] or {}).get("epss"))
                epss_percentile = _to_float((rows[0] or {}).get("percentile"))

        if nvd_record is None and kev_entry is None and epss_score is None:
            failures = [e for e in (nvd_err, kev_err, epss_err) if e]
            if failures:
                return failures[0]
            return (
                f"{cve} was not found in the NVD, the CISA KEV catalog, or the "
                "EPSS dataset. Check the id (it may be too recent, reserved, "
                "or rejected)."
            )

        score, severity, cvss_version = (
            _best_cvss(nvd_record) if nvd_record else (None, "", "")
        )
        today = datetime.now(UTC).date()

        # Deterministic verdict: confirmed exploitation (KEV) always wins,
        # then predicted exploitation probability (EPSS), then impact (CVSS).
        ransomware = (
            str(kev_entry.get("knownRansomwareCampaignUse") or "") == "Known"
            if kev_entry
            else False
        )
        if kev_entry:
            verdict = (
                "CRITICAL - EXPLOITED IN THE WILD. This CVE is in the CISA KEV "
                "catalog (confirmed active exploitation). Remediate immediately."
            )
            if ransomware:
                verdict += " It is known to be used in ransomware campaigns."
        elif epss_score is not None and epss_score >= 0.5:
            verdict = (
                "CRITICAL - very high exploitation probability (EPSS >= 0.5). "
                "Not (yet) in the CISA KEV catalog, but treat as actively "
                "targeted and prioritize remediation."
            )
        elif (epss_score is not None and epss_score >= 0.1) or (
            score is not None and score >= 9.0
        ):
            verdict = (
                "HIGH - elevated exploitation likelihood or critical impact. "
                "Prioritize in the next patch cycle."
            )
        elif (epss_score is not None and epss_score >= 0.01) or (
            score is not None and score >= 7.0
        ):
            verdict = (
                "MEDIUM - moderate risk. Schedule remediation through normal "
                "patch management."
            )
        else:
            verdict = (
                "LOW - no confirmed exploitation and low predicted "
                "probability. Remediate opportunistically."
            )

        lines = [f"Vulnerability assessment for {cve}", f"VERDICT: {verdict}", ""]

        # NVD section.
        if nvd_record:
            lines.append("NVD:")
            if score is not None:
                lines.append(
                    f"  CVSS v{cvss_version}: {score}"
                    + (f" {severity}" if severity else "")
                )
            if nvd_record.get("published"):
                lines.append(
                    f"  Published: {_fmt_date(nvd_record.get('published'))} | "
                    f"Status: {nvd_record.get('vulnStatus') or '?'}"
                )
            cwes = _cwe_ids(nvd_record)
            if cwes:
                lines.append(f"  Weaknesses: {', '.join(cwes[:6])}")
            desc = _trunc(_en_description(nvd_record), 400)
            if desc:
                lines.append(f"  {desc}")
        elif nvd_err:
            lines.append(f"NVD: unavailable ({_trunc(nvd_err, 160)})")
        else:
            lines.append("NVD: no record found (too recent, reserved, or rejected).")

        # KEV section.
        if kev_entry:
            due = str(kev_entry.get("dueDate") or "")
            overdue = ""
            if due:
                try:
                    overdue = (
                        " (OVERDUE)"
                        if datetime.strptime(due, "%Y-%m-%d").date() < today
                        else ""
                    )
                except ValueError:
                    overdue = ""
            lines.append("CISA KEV: LISTED - exploited in the wild")
            lines.append(
                f"  Added: {kev_entry.get('dateAdded') or '?'} | "
                f"Federal remediation due: {due or '?'}{overdue} | "
                f"Ransomware use: {kev_entry.get('knownRansomwareCampaignUse') or '?'}"
            )
            action = _trunc(kev_entry.get("requiredAction"), 200)
            if action:
                lines.append(f"  Required action: {action}")
        elif kev_err:
            lines.append(f"CISA KEV: unavailable ({_trunc(kev_err, 160)})")
        else:
            lines.append("CISA KEV: not listed (no confirmed in-the-wild exploitation).")

        # EPSS section.
        if epss_score is not None:
            pct = (
                f" (higher than {epss_percentile * 100:.1f}% of all CVEs)"
                if epss_percentile is not None
                else ""
            )
            lines.append(
                f"EPSS: {epss_score:.5f} probability of exploitation in the "
                f"next 30 days{pct}"
            )
        elif epss_err:
            lines.append(f"EPSS: unavailable ({_trunc(epss_err, 160)})")
        else:
            lines.append("EPSS: no score available for this CVE.")

        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # NVD CVE
    # ══════════════════════════════════════════════════════════════════════

    async def get_vulnintel_cve(cve: str = "") -> str:
        """Full NVD record for a single CVE."""
        cve = _normalize_cve(cve)
        if not cve:
            return (
                "Error: cve is required and must be a valid CVE id (e.g. "
                "CVE-2021-44228)."
            )
        record, err = await _fetch_nvd_record(cve)
        if err:
            return err
        if not record:
            return (
                f"{cve} was not found in the NVD (it may be too recent, "
                "reserved, or rejected)."
            )
        lines = [
            f"{record.get('id') or cve}",
            f"Status: {record.get('vulnStatus') or '?'} | "
            f"Published: {_fmt_date(record.get('published')) or '?'} | "
            f"Last modified: {_fmt_date(record.get('lastModified')) or '?'}",
        ]
        desc = _trunc(_en_description(record), 800)
        if desc:
            lines.append(f"\n{desc}")
        cvss = _cvss_lines(record)
        if cvss:
            lines.append("\nCVSS metrics:")
            lines.extend(cvss)
        cwes = _cwe_ids(record)
        if cwes:
            lines.append("Weaknesses: " + ", ".join(cwes[:10]))
        if record.get("cisaExploitAdd"):
            lines.append(
                "\nCISA KEV: LISTED - added "
                f"{record.get('cisaExploitAdd')}, action due "
                f"{record.get('cisaActionDue') or '?'} "
                f"({_trunc(record.get('cisaRequiredAction'), 160) or 'see KEV'})"
            )
        cpes, cpe_total = _vulnerable_cpes(record)
        if cpes:
            extra = f" (+{cpe_total - len(cpes)} more)" if cpe_total > len(cpes) else ""
            lines.append("\nVulnerable products (CPE): " + ", ".join(cpes) + extra)
        refs = record.get("references") or []
        if refs:
            lines.append(f"\nReferences ({len(refs)}):")
            for ref in refs[:5]:
                url = (ref or {}).get("url") or "?"
                tags = ", ".join((ref or {}).get("tags") or [])
                lines.append(f"  {_trunc(url, 200)}" + (f" [{tags}]" if tags else ""))
            if len(refs) > 5:
                lines.append(f"  ... and {len(refs) - 5} more")
        lines.append(
            "\nUse assess_vulnintel_cve for the merged exploitation/urgency "
            "verdict (KEV + EPSS)."
        )
        return "\n".join(lines)

    async def search_vulnintel_cves(
        keyword: str = "",
        exact_match: bool = False,
        cpe_name: str = "",
        cvss_v3_severity: str = "",
        cvss_v4_severity: str = "",
        cwe: str = "",
        kev_only: bool = False,
        include_rejected: bool = False,
        pub_within_days: int = 0,
        modified_within_days: int = 0,
        limit: int = 10,
        start_index: int = 0,
    ) -> str:
        """Search NVD CVEs by keyword / CPE / severity / CWE / KEV / dates."""
        keyword = (keyword or "").strip()
        cpe_name = (cpe_name or "").strip()
        cvss_v3_severity = (cvss_v3_severity or "").strip().upper()
        cvss_v4_severity = (cvss_v4_severity or "").strip().upper()
        cwe = (cwe or "").strip().upper()
        limit = _clamp(limit, 1, _MAX_LIMIT, 10)
        start_index = max(_clamp(start_index, 0, 1_000_000, 0), 0)
        pub_within_days = _clamp(pub_within_days, 0, _NVD_MAX_RANGE_DAYS, 0)
        modified_within_days = _clamp(
            modified_within_days, 0, _NVD_MAX_RANGE_DAYS, 0
        )

        if cvss_v3_severity and cvss_v3_severity not in _CVSS_SEVERITIES:
            return (
                "Error: cvss_v3_severity must be one of LOW, MEDIUM, HIGH, "
                "CRITICAL."
            )
        if cvss_v4_severity and cvss_v4_severity not in _CVSS_SEVERITIES:
            return (
                "Error: cvss_v4_severity must be one of LOW, MEDIUM, HIGH, "
                "CRITICAL."
            )
        if cvss_v3_severity and cvss_v4_severity:
            return (
                "Error: the NVD does not accept cvss_v3_severity and "
                "cvss_v4_severity in the same request - pick one."
            )
        if cwe and not cwe.startswith("CWE-"):
            return "Error: cwe must be a CWE id like CWE-79."
        if not any(
            [
                keyword,
                cpe_name,
                cvss_v3_severity,
                cvss_v4_severity,
                cwe,
                kev_only,
                pub_within_days,
                modified_within_days,
            ]
        ):
            return (
                "Error: provide at least one filter - keyword, cpe_name, "
                "cvss_v3_severity, cvss_v4_severity, cwe, kev_only, "
                "pub_within_days, or modified_within_days."
            )

        now = datetime.now(UTC)
        params: dict[str, Any] = {
            "keywordSearch": keyword,
            "cpeName": cpe_name,
            "cvssV3Severity": cvss_v3_severity,
            "cvssV4Severity": cvss_v4_severity,
            "cweId": cwe,
        }
        if pub_within_days:
            params["pubStartDate"] = _nvd_datetime(
                now - timedelta(days=pub_within_days)
            )
            params["pubEndDate"] = _nvd_datetime(now)
        if modified_within_days:
            params["lastModStartDate"] = _nvd_datetime(
                now - timedelta(days=modified_within_days)
            )
            params["lastModEndDate"] = _nvd_datetime(now)
        flags: list[str] = []
        if keyword and exact_match:
            flags.append("keywordExactMatch")
        if kev_only:
            flags.append("hasKev")
        if not include_rejected:
            flags.append("noRejected")

        records, total, err = await _nvd_cve_page(
            params, flags, limit, start_index, "search CVEs"
        )
        if err:
            return err
        out = format_entity_list(
            records or [],
            _fmt_cve_summary,
            noun="CVE",
            total=total,
            empty_msg="No CVEs matched the given filters.",
        )
        out += _pagination_hint(len(records or []), start_index, total)
        return out

    async def list_vulnintel_recent_cves(
        days: int = 7,
        cvss_v3_severity: str = "",
        kev_only: bool = False,
        limit: int = 20,
        start_index: int = 0,
    ) -> str:
        """Newest CVEs published in the last N days (newest first)."""
        days = _clamp(days, 1, _NVD_MAX_RANGE_DAYS, 7)
        cvss_v3_severity = (cvss_v3_severity or "").strip().upper()
        if cvss_v3_severity and cvss_v3_severity not in _CVSS_SEVERITIES:
            return (
                "Error: cvss_v3_severity must be one of LOW, MEDIUM, HIGH, "
                "CRITICAL."
            )
        limit = _clamp(limit, 1, _MAX_LIMIT, 20)
        start_index = max(_clamp(start_index, 0, 1_000_000, 0), 0)
        now = datetime.now(UTC)
        params: dict[str, Any] = {
            "pubStartDate": _nvd_datetime(now - timedelta(days=days)),
            "pubEndDate": _nvd_datetime(now),
            "cvssV3Severity": cvss_v3_severity,
        }
        flags = ["noRejected"]
        if kev_only:
            flags.append("hasKev")
        records, total, err = await _nvd_cve_page(
            params, flags, limit, start_index, f"list CVEs from the last {days} days"
        )
        if err:
            return err
        header_filters = []
        if cvss_v3_severity:
            header_filters.append(f"severity {cvss_v3_severity}")
        if kev_only:
            header_filters.append("KEV only")
        out = format_entity_list(
            records or [],
            _fmt_cve_summary,
            noun="recent CVE",
            total=total,
            empty_msg=(
                f"No CVEs published in the last {days} days"
                + (f" ({', '.join(header_filters)})" if header_filters else "")
                + "."
            ),
        )
        out += _pagination_hint(len(records or []), start_index, total)
        return out

    async def get_vulnintel_cve_history(cve: str = "", limit: int = 25) -> str:
        """NVD change history (audit trail) for one CVE."""
        cve = _normalize_cve(cve)
        if not cve:
            return (
                "Error: cve is required and must be a valid CVE id (e.g. "
                "CVE-2024-3400)."
            )
        limit = _clamp(limit, 1, 100, 25)
        # The history API sorts ascending by change date, so fetch the whole
        # audit trail (resultsPerPage max is 5000 - far beyond any single
        # CVE's history) and reverse for a truthful newest-first view.
        url = _nvd_url(
            NVD_HISTORY_BASE, {"cveId": cve, "resultsPerPage": 5000}
        )
        data, err = await _nvd_get(url, f"get the change history for {cve}")
        if err:
            return err
        changes = [
            (c or {}).get("change") or {}
            for c in (data or {}).get("cveChanges") or []
        ]
        if not changes:
            return f"No change history found for {cve} in the NVD."
        changes.reverse()  # ascending by created -> newest first
        total = int((data or {}).get("totalResults") or len(changes))
        shown = changes[:limit]

        def _fmt_change(change: dict) -> str:
            head = (
                f"- {change.get('eventName') or '?'} | "
                f"{_fmt_date(change.get('created')) or '?'} | "
                f"source: {change.get('sourceIdentifier') or '?'}"
            )
            details = change.get("details") or []
            parts = []
            for d in details[:3]:
                action = (d or {}).get("action") or "?"
                dtype = (d or {}).get("type") or "?"
                parts.append(f"{action} {dtype}")
            if parts:
                head += "\n    " + "; ".join(parts)
                if len(details) > 3:
                    head += f"; ... {len(details) - 3} more changes"
            return head

        out = format_entity_list(
            shown,
            _fmt_change,
            noun="change event",
            total=total,
            empty_msg=f"No change history for {cve}.",
        )
        if total > len(shown):
            out += f"\n[Showing the {len(shown)} most recent of {total} events.]"
        return out

    # ══════════════════════════════════════════════════════════════════════
    # CISA KEV
    # ══════════════════════════════════════════════════════════════════════

    async def check_vulnintel_kev(cve: str = "") -> str:
        """Authoritative exploited-in-the-wild verdict for one CVE."""
        cve = _normalize_cve(cve)
        if not cve:
            return (
                "Error: cve is required and must be a valid CVE id (e.g. "
                "CVE-2023-4966)."
            )
        store, err = await load_kev_catalog()
        if store is None:
            return err or f"Error: {_PROVIDER} could not load the CISA KEV catalog."
        entry = store.by_cve.get(cve)
        if not entry:
            return (
                f"{cve} is NOT in the CISA KEV catalog (catalog version "
                f"{store.catalog_version}, {len(store.entries)} entries). No "
                "confirmed in-the-wild exploitation per CISA - check "
                "get_vulnintel_epss for the predicted exploitation probability."
            )
        today = datetime.now(UTC).date()
        lines = [
            f"{cve} IS in the CISA KEV catalog - confirmed exploited in the wild.",
            f"Name: {entry.get('vulnerabilityName') or '?'}",
            f"Vendor/product: {entry.get('vendorProject') or '?'} "
            f"{entry.get('product') or '?'}",
        ]
        due = str(entry.get("dueDate") or "")
        overdue = ""
        if due:
            try:
                overdue = (
                    " (OVERDUE)"
                    if datetime.strptime(due, "%Y-%m-%d").date() < today
                    else ""
                )
            except ValueError:
                overdue = ""
        lines.append(
            f"Added: {entry.get('dateAdded') or '?'} | Federal remediation "
            f"due: {due or '?'}{overdue}"
        )
        lines.append(
            "Known ransomware campaign use: "
            f"{entry.get('knownRansomwareCampaignUse') or '?'}"
        )
        desc = _trunc(entry.get("shortDescription"), 400)
        if desc:
            lines.append(f"\n{desc}")
        action = _trunc(entry.get("requiredAction"), 300)
        if action:
            lines.append(f"Required action: {action}")
        cwes = entry.get("cwes") or []
        if isinstance(cwes, list) and cwes:
            lines.append("CWEs: " + ", ".join(str(c) for c in cwes[:8]))
        return "\n".join(lines)

    async def search_vulnintel_kev(
        query: str = "",
        vendor: str = "",
        product: str = "",
        added_within_days: int = 0,
        due_within_days: int = 0,
        overdue_only: bool = False,
        ransomware_only: bool = False,
        limit: int = 25,
    ) -> str:
        """Search / monitor the CISA KEV catalog (recent additions, due dates)."""
        query = (query or "").strip().lower()
        vendor = (vendor or "").strip().lower()
        product = (product or "").strip().lower()
        added_within_days = _clamp(added_within_days, 0, 10_000, 0)
        due_within_days = _clamp(due_within_days, 0, 10_000, 0)
        limit = _clamp(limit, 1, 100, 25)
        store, err = await load_kev_catalog()
        if store is None:
            return err or f"Error: {_PROVIDER} could not load the CISA KEV catalog."
        today = datetime.now(UTC).date()

        def _parse_day(value: Any) -> Any:
            try:
                return datetime.strptime(str(value or ""), "%Y-%m-%d").date()
            except ValueError:
                return None

        def _match(entry: dict) -> bool:
            if vendor and vendor not in str(entry.get("vendorProject") or "").lower():
                return False
            if product and product not in str(entry.get("product") or "").lower():
                return False
            if (
                ransomware_only
                and str(entry.get("knownRansomwareCampaignUse") or "") != "Known"
            ):
                return False
            if added_within_days:
                added = _parse_day(entry.get("dateAdded"))
                # Exclude invalid AND future-dated entries: a future dateAdded
                # would make (today - added).days negative and sneak feed
                # glitches into "recent additions" monitoring.
                if (
                    added is None
                    or added > today
                    or (today - added).days > added_within_days
                ):
                    return False
            due = _parse_day(entry.get("dueDate"))
            if overdue_only and (due is None or due >= today):
                return False
            if due_within_days and (
                due is None or due < today or (due - today).days > due_within_days
            ):
                return False
            if query:
                hay = " ".join(
                    str(entry.get(k) or "")
                    for k in (
                        "cveID",
                        "vendorProject",
                        "product",
                        "vulnerabilityName",
                        "shortDescription",
                    )
                ).lower()
                if query not in hay:
                    return False
            return True

        matches = [e for e in store.entries if _match(e)]
        matches.sort(key=lambda e: str(e.get("dateAdded") or ""), reverse=True)
        total = len(matches)
        shown = matches[:limit]
        out = format_entity_list(
            shown,
            lambda e: _fmt_kev_entry(e, today=today),
            noun="KEV entry",
            total=total,
            empty_msg=(
                "No KEV entries matched (catalog version "
                f"{store.catalog_version}, {len(store.entries)} entries)."
            ),
        )
        if total > len(shown):
            out += (
                f"\n[Showing {len(shown)} of {total} - raise 'limit' (max 100) "
                "or narrow the filters.]"
            )
        return out

    # ══════════════════════════════════════════════════════════════════════
    # FIRST EPSS
    # ══════════════════════════════════════════════════════════════════════

    async def get_vulnintel_epss(
        cves: str = "",
        date: str = "",
        time_series: bool = False,
    ) -> str:
        """EPSS exploitation probability for one or many CVEs (batch <= 100)."""
        raw_ids = [c for c in re.split(r"[\s,]+", (cves or "").strip()) if c]
        ids: list[str] = []
        for raw in raw_ids:
            normalized = _normalize_cve(raw)
            if not normalized:
                return f"Error: '{raw}' is not a valid CVE id (e.g. CVE-2024-3400)."
            if normalized not in ids:
                ids.append(normalized)
        if not ids:
            return (
                "Error: cves is required - one CVE id or a comma-separated "
                "list of up to 100 (e.g. 'CVE-2024-3400, CVE-2023-4966')."
            )
        if len(ids) > _EPSS_MAX_BATCH:
            return (
                f"Error: at most {_EPSS_MAX_BATCH} CVE ids per call "
                f"(got {len(ids)})."
            )
        date = (date or "").strip()
        if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            return "Error: date must be YYYY-MM-DD (EPSS history starts 2021-04-14)."
        if time_series and len(ids) > 1:
            return "Error: time_series only works with a single CVE id."
        params: dict[str, Any] = {"cve": ",".join(ids)}
        if date:
            params["date"] = date
        if time_series:
            params["scope"] = "time-series"
        data, err = await _epss_get(
            params, f"get EPSS scores for {len(ids)} CVE(s)"
        )
        if err:
            return err
        rows = (data or {}).get("data") or []
        if not rows:
            return (
                "No EPSS scores found for "
                + ", ".join(ids)
                + (f" on {date}" if date else "")
                + " (very recent CVEs may not be scored yet)."
            )

        lines = [f"EPSS scores ({len(rows)} of {len(ids)} requested CVEs):"]
        scored = {str((r or {}).get("cve") or "") for r in rows}
        for row in rows:
            row = row or {}
            score = _to_float(row.get("epss"))
            pct = _to_float(row.get("percentile"))
            line = f"- {row.get('cve') or '?'}: "
            if score is not None:
                line += f"{score:.5f} probability of exploitation in 30 days"
            else:
                line += "no score"
            if pct is not None:
                line += f" | percentile {pct * 100:.1f}%"
            if row.get("date"):
                line += f" | as of {row.get('date')}"
            lines.append(line)
            if time_series:
                series = row.get("time-series") or []
                for point in series[:30]:
                    point = point or {}
                    p_score = _to_float(point.get("epss"))
                    p_value = f"{p_score:.5f}" if p_score is not None else "no score"
                    lines.append(f"    {point.get('date') or '?'}: {p_value}")
        missing = [c for c in ids if c not in scored]
        if missing:
            lines.append(
                "No score returned for: "
                + ", ".join(missing[:20])
                + (" ..." if len(missing) > 20 else "")
            )
        return "\n".join(lines)

    async def list_vulnintel_top_epss(
        min_epss: float = 0.0,
        min_percentile: float = 0.0,
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """Most-likely-to-be-exploited CVEs, ranked by EPSS (descending)."""
        limit = _clamp(limit, 1, 100, 20)
        offset = max(_clamp(offset, 0, 1_000_000, 0), 0)
        try:
            min_epss = float(min_epss or 0)
            min_percentile = float(min_percentile or 0)
        except (TypeError, ValueError):
            return "Error: min_epss and min_percentile must be numbers between 0 and 1."
        if not (0 <= min_epss <= 1) or not (0 <= min_percentile <= 1):
            return "Error: min_epss and min_percentile must be between 0 and 1."
        params: dict[str, Any] = {
            "order": "!epss",
            "limit": limit,
            "offset": offset,
        }
        if min_epss:
            params["epss-gt"] = f"{min_epss}"
        if min_percentile:
            params["percentile-gt"] = f"{min_percentile}"
        data, err = await _epss_get(params, "list top EPSS CVEs")
        if err:
            return err
        rows = (data or {}).get("data") or []
        total = (data or {}).get("total")

        def _fmt(row: dict) -> str:
            score = _to_float(row.get("epss"))
            pct = _to_float(row.get("percentile"))
            out = f"- {row.get('cve') or '?'}"
            if score is not None:
                out += f" | EPSS {score:.5f}"
            if pct is not None:
                out += f" | percentile {pct * 100:.1f}%"
            return out

        out = format_entity_list(
            [r or {} for r in rows],
            _fmt,
            noun="CVE",
            total=total if isinstance(total, int) else None,
            empty_msg="No CVEs matched the EPSS thresholds.",
        )
        if isinstance(total, int) and total > offset + len(rows):
            out += (
                f"\n[More results available - pass offset={offset + len(rows)} "
                "to page further.]"
            )
        out += (
            "\nUse assess_vulnintel_cve on any of these ids for the merged "
            "NVD + KEV + EPSS verdict."
        )
        return out

    # ══════════════════════════════════════════════════════════════════════
    # STATUS
    # ══════════════════════════════════════════════════════════════════════

    async def get_vulnintel_status() -> str:
        """Check connectivity to all three sources and report the NVD tier."""
        tier = (
            "NVD API key configured (50 requests / rolling 30s)"
            if api_key
            else "no NVD API key (public tier, 5 requests / rolling 30s)"
        )
        lines = [f"Vulnerability Intelligence status - {tier}."]

        probe_url = _nvd_url(NVD_CVE_BASE, {"resultsPerPage": 1})
        nvd_data, nvd_err = await _nvd_get(probe_url, "probe the NVD CVE API")
        if nvd_err:
            lines.append(f"- NVD CVE API 2.0: UNAVAILABLE ({_trunc(nvd_err, 160)})")
        else:
            total = (nvd_data or {}).get("totalResults")
            lines.append(
                f"- NVD CVE API 2.0: OK ({total} CVE records)"
                if isinstance(total, int)
                else "- NVD CVE API 2.0: OK"
            )

        store, kev_err = await load_kev_catalog()
        if store is None:
            lines.append(
                f"- CISA KEV catalog: UNAVAILABLE ({_trunc(kev_err, 160)})"
            )
        else:
            lines.append(
                f"- CISA KEV catalog: OK (version {store.catalog_version}, "
                f"{len(store.entries)} exploited CVEs, released "
                f"{_fmt_date(store.date_released)})"
            )

        epss_data, epss_err = await _epss_get(
            {"cve": "CVE-2021-44228"}, "probe the EPSS API"
        )
        if epss_err:
            lines.append(f"- FIRST EPSS API: UNAVAILABLE ({_trunc(epss_err, 160)})")
        else:
            rows = (epss_data or {}).get("data") or []
            as_of = (rows[0] or {}).get("date") if rows else ""
            lines.append(
                f"- FIRST EPSS API: OK (scores as of {as_of})"
                if as_of
                else "- FIRST EPSS API: OK"
            )

        if not api_key:
            lines.append(
                "\nTip: a free NVD API key (https://nvd.nist.gov/developers/"
                "request-an-api-key) raises the NVD rate limit 10x."
            )
        lines.append(f"\n{_NVD_ATTRIBUTION}")
        return "\n".join(lines)

    # ── Register ──────────────────────────────────────────────────────────

    executor.register("assess_vulnintel_cve", assess_vulnintel_cve)
    executor.register("get_vulnintel_cve", get_vulnintel_cve)
    executor.register("search_vulnintel_cves", search_vulnintel_cves)
    executor.register("list_vulnintel_recent_cves", list_vulnintel_recent_cves)
    executor.register("get_vulnintel_cve_history", get_vulnintel_cve_history)
    executor.register("check_vulnintel_kev", check_vulnintel_kev)
    executor.register("search_vulnintel_kev", search_vulnintel_kev)
    executor.register("get_vulnintel_epss", get_vulnintel_epss)
    executor.register("list_vulnintel_top_epss", list_vulnintel_top_epss)
    executor.register("get_vulnintel_status", get_vulnintel_status)

    return [
        ToolDefinition(
            name="assess_vulnintel_cve",
            description=(
                "Answer 'is this CVE exploited in the wild and how urgent is "
                "it?' in ONE call: queries the NVD (CVSS impact, description, "
                "weaknesses), the CISA KEV catalog (confirmed in-the-wild "
                "exploitation, federal remediation due date, ransomware "
                "campaign use), and FIRST EPSS (predicted exploitation "
                "probability) concurrently, then returns a deterministic "
                "urgency VERDICT (CRITICAL/HIGH/MEDIUM/LOW) with the evidence "
                "from each source. Prefer this over the individual tools when "
                "assessing or prioritizing a specific CVE."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve": {
                        "type": "string",
                        "description": "CVE id to assess (e.g. CVE-2024-3400).",
                    },
                },
                "required": ["cve"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vulnintel_cve",
            description=(
                "Get the full NVD record for a single CVE: description, all "
                "CVSS metrics (v2/v3/v4 with vector strings), CWE weaknesses, "
                "NVD status, publication/modification dates, CISA KEV fields, "
                "affected products (CPE), and references. Use for deep detail "
                "on one CVE; for an exploitation/urgency verdict use "
                "assess_vulnintel_cve instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve": {
                        "type": "string",
                        "description": "CVE id (e.g. CVE-2021-44228).",
                    },
                },
                "required": ["cve"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_vulnintel_cves",
            description=(
                "Search the NVD for CVEs by keyword, affected product (CPE "
                "2.3 name), CVSS v3/v4 severity, CWE weakness, CISA KEV "
                "membership, and/or publication / modification windows. "
                "Results are returned NEWEST FIRST with CVSS, status, and a "
                "short description. At least one filter is required; filters "
                "combine with AND. Rejected CVEs are excluded unless "
                "include_rejected is set. Use list_vulnintel_recent_cves for "
                "plain 'what is new' monitoring."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": (
                            "Words matched against CVE descriptions (AND "
                            "semantics, e.g. 'apache log4j')."
                        ),
                    },
                    "exact_match": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Match the keyword phrase exactly instead of "
                            "each word independently."
                        ),
                    },
                    "cpe_name": {
                        "type": "string",
                        "description": (
                            "CPE 2.3 product name, full or prefix (e.g. "
                            "'cpe:2.3:o:microsoft:windows_10:1607')."
                        ),
                    },
                    "cvss_v3_severity": {
                        "type": "string",
                        "enum": list(_CVSS_SEVERITIES),
                        "description": (
                            "CVSS v3 qualitative severity filter (cannot be "
                            "combined with cvss_v4_severity)."
                        ),
                    },
                    "cvss_v4_severity": {
                        "type": "string",
                        "enum": list(_CVSS_SEVERITIES),
                        "description": (
                            "CVSS v4 qualitative severity filter (cannot be "
                            "combined with cvss_v3_severity)."
                        ),
                    },
                    "cwe": {
                        "type": "string",
                        "description": "CWE weakness id filter (e.g. CWE-79).",
                    },
                    "kev_only": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Only CVEs in the CISA KEV catalog (exploited in "
                            "the wild)."
                        ),
                    },
                    "include_rejected": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include rejected/withdrawn CVE records.",
                    },
                    "pub_within_days": {
                        "type": "integer",
                        "description": (
                            "Only CVEs published in the last N days (1-120)."
                        ),
                    },
                    "modified_within_days": {
                        "type": "integer",
                        "description": (
                            "Only CVEs modified in the last N days (1-120)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max CVEs to return (1-50).",
                    },
                    "start_index": {
                        "type": "integer",
                        "default": 0,
                        "description": (
                            "Pagination offset from the NEWEST match (0 = "
                            "most recent page)."
                        ),
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_vulnintel_recent_cves",
            description=(
                "List the newest CVEs published to the NVD in the last N days "
                "(newest first), optionally filtered by CVSS v3 severity or "
                "restricted to CISA KEV (exploited) entries. Use for daily/"
                "weekly vulnerability monitoring and digests. Returns CVE id, "
                "CVSS, status, and a short description per entry."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "default": 7,
                        "description": "Lookback window in days (1-120).",
                    },
                    "cvss_v3_severity": {
                        "type": "string",
                        "enum": list(_CVSS_SEVERITIES),
                        "description": "Optional CVSS v3 severity filter.",
                    },
                    "kev_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Only CVEs in the CISA KEV catalog.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max CVEs to return (1-50).",
                    },
                    "start_index": {
                        "type": "integer",
                        "default": 0,
                        "description": (
                            "Pagination offset from the NEWEST CVE (0 = most "
                            "recent page)."
                        ),
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vulnintel_cve_history",
            description=(
                "Get the NVD change history (audit trail) for a CVE: when it "
                "was received, analyzed, modified, translated, KEV-updated, "
                "or rejected, by which source, and what changed (CVSS, CWE, "
                "CPE, references). Newest events first. Use to understand how "
                "a CVE record evolved or verify when scoring/KEV data landed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve": {
                        "type": "string",
                        "description": "CVE id (e.g. CVE-2024-3400).",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": "Max change events to return (1-100).",
                    },
                },
                "required": ["cve"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="check_vulnintel_kev",
            description=(
                "Check whether a CVE is in the CISA Known Exploited "
                "Vulnerabilities (KEV) catalog - the authoritative 'exploited "
                "in the wild' verdict. Returns a clear IS / IS NOT answer "
                "plus, when listed, the date added, the BOD 22-01 federal "
                "remediation due date (with an OVERDUE marker), the known-"
                "ransomware-campaign-use flag, and the required action. For a "
                "full urgency assessment use assess_vulnintel_cve."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve": {
                        "type": "string",
                        "description": "CVE id to check (e.g. CVE-2023-4966).",
                    },
                },
                "required": ["cve"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_vulnintel_kev",
            description=(
                "Search and monitor the CISA KEV catalog: recent additions "
                "(added_within_days), upcoming federal remediation deadlines "
                "(due_within_days), overdue entries, ransomware-linked "
                "entries, and vendor/product/keyword hunting. All filters "
                "combine with AND; results sort newest-added first. Use for "
                "'what was added to KEV this week?' or 'which exploited "
                "Cisco products do we need to patch?'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Keyword matched against CVE id, vendor, product, "
                            "name, and description."
                        ),
                    },
                    "vendor": {
                        "type": "string",
                        "description": "Vendor/project substring (e.g. 'Cisco').",
                    },
                    "product": {
                        "type": "string",
                        "description": "Product substring (e.g. 'IOS XE').",
                    },
                    "added_within_days": {
                        "type": "integer",
                        "description": (
                            "Only entries added to the catalog in the last N "
                            "days (e.g. 7 for the last week)."
                        ),
                    },
                    "due_within_days": {
                        "type": "integer",
                        "description": (
                            "Only entries whose remediation due date falls "
                            "within the next N days."
                        ),
                    },
                    "overdue_only": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Only entries whose remediation due date has "
                            "already passed."
                        ),
                    },
                    "ransomware_only": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Only entries known to be used in ransomware "
                            "campaigns."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": "Max entries to return (1-100).",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vulnintel_epss",
            description=(
                "Get the FIRST EPSS exploitation probability for one or many "
                "CVEs (comma-separated batch of up to 100): the probability "
                "of exploitation in the next 30 days plus the percentile "
                "relative to all scored CVEs. Optionally as of a historical "
                "date (back to 2021-04-14) or as a 30-day time series for a "
                "single CVE. Use for prioritization and batch triage; for the "
                "merged verdict on one CVE use assess_vulnintel_cve."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cves": {
                        "type": "string",
                        "description": (
                            "One CVE id or a comma-separated list, up to 100 "
                            "(e.g. 'CVE-2024-3400, CVE-2023-4966')."
                        ),
                    },
                    "date": {
                        "type": "string",
                        "description": (
                            "Optional as-of date, YYYY-MM-DD (history starts "
                            "2021-04-14)."
                        ),
                    },
                    "time_series": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Return the past 30 days of scores (single CVE "
                            "only)."
                        ),
                    },
                },
                "required": ["cves"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_vulnintel_top_epss",
            description=(
                "List the CVEs most likely to be exploited according to EPSS, "
                "ranked by score (descending), optionally above an EPSS "
                "probability and/or percentile threshold. Use to build a "
                "priority hunting/patching list or to answer 'which CVEs are "
                "most likely to be attacked right now?'. Follow up with "
                "assess_vulnintel_cve on interesting ids."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "min_epss": {
                        "type": "number",
                        "default": 0,
                        "description": (
                            "Only CVEs with an EPSS probability greater than "
                            "this (0-1, e.g. 0.9)."
                        ),
                    },
                    "min_percentile": {
                        "type": "number",
                        "default": 0,
                        "description": (
                            "Only CVEs above this percentile (0-1, e.g. "
                            "0.99)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max CVEs to return (1-100).",
                    },
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "description": "Pagination offset.",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vulnintel_status",
            description=(
                "Check connectivity to all three vulnerability intelligence "
                "sources (NVD CVE API, CISA KEV catalog, FIRST EPSS API) and "
                "report the active NVD tier (API key vs public) with its rate "
                "limit, the loaded KEV catalog version and entry count, and "
                "EPSS data freshness. Use to diagnose configuration issues."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
    ]
