# SPDX-License-Identifier: Apache-2.0

"""Flashpoint integration - agentic threat intelligence access (Ignite API).

Full-fledged Flashpoint Ignite integration covering the platform's read
surfaces for an agentic CTI/SOC team:

Finished Intelligence (``/finished-intelligence/v1``)
  - Search analyst-written intelligence reports by keyword and date window
  - Read one report (summary, tags, platform link) and pivot to related
    reports

Technical Intelligence (``/technical-intelligence/v2`` + ``/v1``)
  - IOC hunting with the full filter surface (types, actors, malware,
    sources, tags, MITRE ATT&CK ids, score tiers, CIDR ranges, date windows)
  - Single-IOC reputation lookup and full indicator context (sightings,
    related IOCs, malware/actor descriptions)
  - Technical intelligence events (observations) by ATT&CK id or report

Communities Intelligence (``/sources/v2``)
  - Full-text search over illicit communities, forums, chats, and paste
    sites, plus single-document reads listing their media
  - Media assets (``/sources/v1/media``) stored as files of the run -
    anything that does not decode as an image or read as an MP4 / MOV only as
    a sealed ZIP

Compromised Credentials Monitoring / CCM (``/sources/v1``)
  - Credential-sighting search (breach data) with freshness, date-window,
    and Lucene-style query refinement

Alerting (``/alert-management/v1``)
  - The Ignite alerting inbox (saved-search and asset alerts) with source,
    status, tag, and date filters (cursor pagination)

Vulnerability Intelligence / VulnDB (``/vulnerability-intelligence/v1``)
  - Vulnerability search (severity, EPSS, ransomware score, CVSS ranges,
    products, vendors, CWEs, date windows), CVE lookup, single-vulnerability
    detail, affected libraries/packages, and the vendors/products catalog

The Ignite API is read-only - there is no public write surface, so every
tool is a read (priority 0).

Authentication
--------------
Flashpoint authenticates every request with a single API token sent as a
Bearer token (``Authorization: Bearer <token>``). Tokens are generated at
https://app.flashpoint.io/tokens. ``integration_tools._get_token`` hands this
builder the compound ``base_url||api_token`` token; ``base_url`` is optional
and defaults to ``https://api.flashpoint.io`` (only proxies override it).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.credential_exposure import presence, scrub_exposure_text
from app.integrations.file_content import keep_file, kept_file_line
from app.integrations.file_delivery import (
    PersistFileFn,
    deliver_file,
    deliver_live_artefact,
    fetch_file,
    format_kb,
)
from app.integrations.http import (
    bearer_auth_headers,
    format_entity_list,
    format_pagination_hint,
    parse_compound_token,
    safe_request,
)
from app.integrations.providers.social_media import media_type, mp4_geometry
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "Flashpoint"
_SOURCE = "integration:flashpoint"

# Default Flashpoint Ignite API host. Flashpoint is a single-host SaaS; the
# stored base_url only overrides this for API proxies.
_DEFAULT_BASE_URL = "https://api.flashpoint.io"

# A media asset (``GET /sources/v1/media``) stored as a file of the run.
_MAX_MEDIA_BYTES = 100 * 1024 * 1024
_VIDEO_EXTENSIONS = {"video/mp4": ".mp4", "video/quicktime": ".mov"}

# The Ignite web application host - fixed and public, used only to build
# convenience deep links (the API also returns platform_url(s) on many
# objects, which are preferred when present).
_APP_URL = "https://app.flashpoint.io"

# Indicator score tiers (worst-first) used by min_score/max_score filters.
_SCORE_TIERS = ("malicious", "suspicious", "informational")

# Alert source labels -> API source keys (mirrors the vendor's own
# integration mapping).
_ALERT_SOURCES = {
    "github": "data_exposure__github",
    "gitlab": "data_exposure__gitlab",
    "bitbucket": "data_exposure__bitbucket",
    "communities": "communities",
    "images": "media",
    "marketplaces": "marketplaces",
}

# Alert statuses: the UI's "starred" is "flagged" on the wire.
_ALERT_STATUSES = {"archived": "archived", "starred": "flagged", "sent": "sent"}

_VULN_SEVERITIES = ("critical", "high", "medium", "low", "informational")
_VULN_RANSOMWARE_SCORES = ("critical", "high", "medium", "low")
_VULN_SORT_FIELDS = ("published_at", "id", "severity", "title", "cvssv3_score")


async def _verified_media_type(data: bytes) -> str | None:
    """The type of an image or a video the bytes really are, or ``None``.

    A file posted in an illicit community can be live malware under any name
    or label, and a magic number is a prefix anyone can write: an image counts
    only once it decodes, a video only once its ``moov`` box reads a duration
    and a video track.
    """
    from app.services.builtin_tools.image_assets import sniff_raster_type
    from app.services.image_summary import summarize_image

    raster = sniff_raster_type(data)
    if raster:
        return raster if await summarize_image(data) is not None else None
    video = media_type(data, "", "")
    # A fragmented MP4 declares a zero duration: read, not missing.
    if video in _VIDEO_EXTENSIONS and None not in mp4_geometry(data):
        return video
    return None


def _media_filename(name: str, content_type: str) -> str:
    """*name* ending with the extension of the type its bytes are."""
    from app.services.builtin_tools.image_assets import normalize_asset_filename

    extension = _VIDEO_EXTENSIONS.get(content_type)
    if extension is None:
        return normalize_asset_filename(name, "", content_type)
    return name if name.lower().endswith(extension) else f"{name}{extension}"


def _path(value: Any) -> str:
    """Percent-encode a value for use as a single URL path segment.

    User-supplied ids (report ids, indicator ids, event fpids, ...) are
    interpolated into request paths; encoding with ``safe=""`` prevents an id
    containing ``/`` or ``..`` from escaping its path segment (same pattern
    as the Sekoia / Recorded Future providers).
    """
    return quote(str(value).strip(), safe="")


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _media_lines(media: Any) -> list[str]:
    """One line per ``media_v2`` entry of a document or alert, with its asset id."""
    entries = media if isinstance(media, list) else [media] if isinstance(media, dict) else []
    lines: list[str] = []
    for m in entries[:10]:
        if not isinstance(m, dict) or not m.get("storage_uri"):
            continue
        lines.append(
            f"    Media: {_trunc(m.get('file_name'), 80) or '?'} "
            f"[{m.get('media_type') or m.get('mime_type') or '?'}] "
            f"sha1 {m.get('sha1', '?')} - storage_uri: {m['storage_uri']}"
        )
    return lines


def _ts(value: Any) -> str:
    """Render a Flashpoint timestamp (ISO string or nested date object)."""
    if isinstance(value, dict):
        value = value.get("date-time") or value.get("raw") or value.get("timestamp")
    if value is None or value == "":
        return "?"
    return str(value)[:19]


def _clamp(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(value if value is not None else default), hi))
    except (TypeError, ValueError):
        return default


def _csv(value: str) -> str:
    """Normalise a comma-separated filter string (strip empty segments)."""
    return ",".join(p.strip() for p in str(value or "").split(",") if p.strip())


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def flashpoint_api_base(base_url: str) -> str:
    """Return the Flashpoint API base URL (configured host, no trailing slash).

    Whitespace is stripped before the default fallback and the trailing-slash
    trim (matching ``sekoia_api_base`` / ``rf_api_base``), so a pasted
    ``base_url`` with surrounding whitespace - or an entirely whitespace-only
    value - normalises to a valid host instead of an empty string.
    """
    return ((base_url or "").strip() or _DEFAULT_BASE_URL).rstrip("/")


def build_flashpoint_headers(api_token: str) -> dict[str, str]:
    """Build Flashpoint request headers (Bearer token + JSON accept).

    The token is normalised by ``bearer_auth_headers`` (stripped,
    blank-guarded) so pasted whitespace can never produce a malformed
    ``Authorization`` header. Single chokepoint shared by the tool builder
    and the connectivity test.
    """
    return bearer_auth_headers(
        api_token,
        extra={
            "Accept": "application/json",
            "x-fp-integrationplatform": "XTM One",
        },
    )


class FlashpointIntegration(BaseIntegration):
    """Flashpoint Ignite threat intelligence integration (API token, Bearer)."""

    provider_name = "flashpoint"
    display_name = "Flashpoint"
    icon = "Flame"
    description = (
        "Connect Flashpoint (Ignite) for threat intelligence: search analyst "
        "intelligence reports, hunt and enrich IOCs, search illicit "
        "communities and forums, monitor compromised credentials (CCM), "
        "review alerts, and query vulnerability intelligence (VulnDB)."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        """Flashpoint uses a Bearer API token, not OAuth."""
        return None

    async def validate_connection(self, token: str) -> bool:
        """Validate the token against a minimal indicators query."""
        base_url, api_token = (
            p.strip() for p in parse_compound_token(token, 2, greedy_last=True)
        )
        if not api_token:
            return False
        api = flashpoint_api_base(base_url)
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/technical-intelligence/v2/indicators",
                _PROVIDER,
                "validate API token",
                headers=build_flashpoint_headers(api_token),
                params={"size": 1},
            )
            return err is None and resp is not None and resp.is_success

    async def get_user_info(self, token: str) -> dict:
        """Return minimal account context (identity is bound to the token)."""
        base_url, _ = (
            p.strip() for p in parse_compound_token(token, 2, greedy_last=True)
        )
        return {"name": "Flashpoint", "base_url": flashpoint_api_base(base_url)}


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    persist_file_fn: PersistFileFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Flashpoint tools from a ``base_url||api_token`` compound token.

    ``persist_file_fn`` (injected by ``integration_tools``) stores a media
    asset as a file of the run.
    """
    # ``parse_compound_token`` does not normalise - strip each segment so
    # pasted whitespace can't yield a malformed base URL / Authorization
    # header, and so the empty-credential guard treats a whitespace-only
    # token as missing.
    base_url, api_token = (
        part.strip() for part in parse_compound_token(token, 2, greedy_last=True)
    )
    if not api_token:
        logger.debug(
            "Flashpoint API token is empty - returning no tools. Edit the "
            "integration and set the API token."
        )
        return []

    api = flashpoint_api_base(base_url)
    headers = build_flashpoint_headers(api_token)

    def _client(timeout: int = 30) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_flashpoint_status() -> str:
        """Validate the Flashpoint connection and API token."""
        async with _client() as client:
            _resp, err = await safe_request(
                client,
                "get",
                f"{api}/technical-intelligence/v2/indicators",
                _PROVIDER,
                "validate API token",
                headers=headers,
                params={"size": 1},
            )
            if err:
                return err
        return (
            f"Connected to Flashpoint Ignite at {api}. API token is valid. "
            f"Platform UI: {_APP_URL}"
        )

    # ══════════════════════════════════════════════════════════════════════
    # 1. FINISHED INTELLIGENCE - REPORTS
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_report(r: dict) -> str:
        tags = r.get("tags") or []
        line = (
            f"  {_trunc(r.get('title'), 100)} (id {r.get('id', '?')})\n"
            f"    Posted: {_ts(r.get('posted_at'))} | Updated: {_ts(r.get('updated_at'))}"
        )
        if tags:
            line += f" | Tags: {_trunc(', '.join(map(str, tags[:8])), 160)}"
        if r.get("summary"):
            line += f"\n    {_trunc(r.get('summary'), 240)}"
        if r.get("platform_url"):
            line += f"\n    Link: {r['platform_url']}"
        return line

    async def search_flashpoint_reports(
        query: str = "",
        since: str = "",
        until: str = "",
        limit: int = 10,
        skip: int = 0,
        sort: str = "",
    ) -> str:
        """Search finished intelligence reports by keyword and date window."""
        params: dict[str, Any] = {
            "limit": _clamp(limit, 10, 1, 100),
            "skip": max(0, int(skip or 0)),
        }
        if query.strip():
            params["query"] = query.strip()
        if since.strip():
            params["since"] = since.strip()
        if until.strip():
            params["until"] = until.strip()
        if sort.strip():
            params["sort"] = sort.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/finished-intelligence/v1/reports",
                _PROVIDER,
                "search reports",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        reports = data.get("data", []) or []
        total = data.get("total")
        out = format_entity_list(
            reports,
            _fmt_report,
            noun="report",
            total=total if isinstance(total, int) else None,
            empty_msg=(
                "No intelligence reports matched. Broaden the query or widen "
                "the since/until window."
            ),
        )
        if isinstance(total, int) and total > params["skip"] + len(reports):
            out += (
                f"\n[Showing {len(reports)} from offset {params['skip']} of "
                f"{total} - pass skip={params['skip'] + len(reports)} for more]"
            )
        return out

    async def get_flashpoint_report(report_id: str) -> str:
        """Get one finished intelligence report (summary, tags, body)."""
        rid = report_id.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/finished-intelligence/v1/reports/{_path(rid)}",
                _PROVIDER,
                f"get report {rid}",
                headers=headers,
            )
            if err:
                return err
            r = resp.json() or {}
        lines = [
            f"Report: {_trunc(r.get('title'), 160)} (id {r.get('id', rid)})",
            f"Posted: {_ts(r.get('posted_at'))} | Updated: {_ts(r.get('updated_at'))} | "
            f"Notified: {_ts(r.get('notified_at'))}",
        ]
        tags = r.get("tags") or []
        if tags:
            lines.append(f"Tags: {_trunc(', '.join(map(str, tags)), 400)}")
        if r.get("summary"):
            lines.append(f"\nSummary: {_trunc(r.get('summary'), 1200)}")
        if r.get("body"):
            lines.append(f"\nBody (truncated): {_trunc(r.get('body'), 2000)}")
        if r.get("platform_url"):
            lines.append(f"\nLink: {r['platform_url']}")
        return "\n".join(lines)

    async def list_flashpoint_related_reports(report_id: str, limit: int = 10) -> str:
        """List reports related to a given report."""
        rid = report_id.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/finished-intelligence/v1/reports/{_path(rid)}/related",
                _PROVIDER,
                f"list related reports {rid}",
                headers=headers,
                params={"limit": _clamp(limit, 10, 1, 100)},
            )
            if err:
                return err
            data = resp.json() or {}
        reports = data.get("data", []) or []
        return format_entity_list(
            reports,
            _fmt_report,
            noun="related report",
            total=data.get("total") if isinstance(data.get("total"), int) else None,
            empty_msg=f"No reports related to {rid}.",
        )

    # ══════════════════════════════════════════════════════════════════════
    # 2. TECHNICAL INTELLIGENCE - INDICATORS (IOCs)
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_indicator(ind: dict) -> str:
        score = ind.get("score") or {}
        line = (
            f"  [{ind.get('type', '?')}] {_trunc(ind.get('value'), 120)} "
            f"(id {ind.get('id', '?')})\n"
            f"    Score: {score.get('value', 'unscored')} | "
            f"Last seen: {_ts(ind.get('last_seen_at'))} | "
            f"Modified: {_ts(ind.get('modified_at'))} | "
            f"Sightings: {ind.get('total_sightings', '?')}"
        )
        latest = ind.get("latest_sighting") or {}
        tags = latest.get("tags") or []
        if tags:
            line += f"\n    Tags: {_trunc(', '.join(map(str, tags[:10])), 200)}"
        mitre = ind.get("mitre_attack_ids") or []
        if mitre:
            ids = [m.get("id", str(m)) if isinstance(m, dict) else str(m) for m in mitre[:8]]
            line += f"\n    MITRE ATT&CK: {', '.join(ids)}"
        ignite_url = (ind.get("platform_urls") or {}).get("ignite")
        if ignite_url:
            line += f"\n    Link: {ignite_url}"
        return line

    async def search_flashpoint_indicators(
        ioc_value: str = "",
        ioc_types: str = "",
        cidr_range: str = "",
        actors: str = "",
        malware: str = "",
        sources: str = "",
        tags: str = "",
        mitre_attack_ids: str = "",
        min_score: str = "",
        max_score: str = "",
        has_intel_report: bool = False,
        last_seen_after: str = "",
        modified_after: str = "",
        sort: str = "last_seen_at:desc",
        size: int = 20,
        from_offset: int = 0,
    ) -> str:
        """Hunt IOCs in the Technical Intelligence v2 dataset with filters."""
        params: dict[str, Any] = {
            "size": _clamp(size, 20, 1, 500),
            "from": max(0, int(from_offset or 0)),
            "sort": sort.strip() or "last_seen_at:desc",
            "include_total_count": True,
        }
        if ioc_value.strip():
            params["ioc_value"] = ioc_value.strip()
        if _csv(ioc_types):
            params["ioc_types"] = _csv(ioc_types)
        if cidr_range.strip():
            params["cidr_range"] = cidr_range.strip()
        if _csv(actors):
            params["actors"] = _csv(actors)
        if _csv(malware):
            params["malware"] = _csv(malware)
        if _csv(sources):
            params["sources"] = _csv(sources)
        if _csv(tags):
            params["tags"] = _csv(tags)
        if _csv(mitre_attack_ids):
            params["mitre_attack_ids"] = _csv(mitre_attack_ids)
        if min_score.strip():
            if min_score.strip().lower() not in _SCORE_TIERS:
                return f"Error: min_score must be one of {', '.join(_SCORE_TIERS)}."
            params["min_score"] = min_score.strip().lower()
        if max_score.strip():
            if max_score.strip().lower() not in _SCORE_TIERS:
                return f"Error: max_score must be one of {', '.join(_SCORE_TIERS)}."
            params["max_score"] = max_score.strip().lower()
        if has_intel_report:
            params["has_intel_report"] = True
        if last_seen_after.strip():
            params["last_seen_after"] = last_seen_after.strip()
        if modified_after.strip():
            params["modified_after"] = modified_after.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/technical-intelligence/v2/indicators",
                _PROVIDER,
                "search indicators",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items", []) or []
        total = data.get("total")
        if isinstance(total, dict):
            total = total.get("value")
        out = format_entity_list(
            items,
            _fmt_indicator,
            noun="indicator",
            total=total if isinstance(total, int) else None,
            empty_msg="No indicators matched the filters.",
        )
        if isinstance(total, int) and total > params["from"] + len(items):
            out += (
                f"\n[Showing {len(items)} from offset {params['from']} of {total} - "
                f"pass from_offset={params['from'] + len(items)} for more]"
            )
        return out

    async def lookup_flashpoint_ioc(value: str, limit: int = 10) -> str:
        """Reputation lookup: is this IOC known to Flashpoint, and how bad?"""
        val = value.strip()
        if not val:
            return "Error: value is required."
        params: dict[str, Any] = {
            "ioc_value": val,
            "size": _clamp(limit, 10, 1, 100),
            "embed": "all",
            "sort": "last_seen_at:desc",
        }
        async with _client(timeout=45) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/technical-intelligence/v2/indicators",
                _PROVIDER,
                f"look up IOC {val}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items", []) or []
        if not items:
            return (
                f"'{val}' is unknown to the Flashpoint IOC dataset (no "
                "indicator matched). It may still appear in communities - try "
                "search_flashpoint_communities."
            )
        lines = [f"Flashpoint knows {len(items)} indicator(s) matching '{val}':"]
        for ind in items:
            lines.append(_fmt_indicator(ind))
            for key, label in (
                ("malware_description", "Malware"),
                ("apt_description", "Actor"),
            ):
                if ind.get(key):
                    lines.append(f"    {label}: {_trunc(ind[key], 300)}")
            related = ((ind.get("relationships") or {}).get("iocs")) or []
            if related:
                rel = ", ".join(
                    f"{r.get('type', '?')}:{_trunc(r.get('value'), 60)}"
                    for r in related[:5]
                    if isinstance(r, dict)
                )
                lines.append(f"    Related IOCs: {rel}")
        return "\n".join(lines)

    async def get_flashpoint_indicator(indicator_id: str, sighting_count: int = 5) -> str:
        """Get one indicator's full context (sightings, related IOCs)."""
        iid = indicator_id.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/technical-intelligence/v2/indicators/{_path(iid)}",
                _PROVIDER,
                f"get indicator {iid}",
                headers=headers,
                params={"sighting_count": _clamp(sighting_count, 5, 1, 100), "embed": "all"},
            )
            if err:
                return err
            ind = resp.json() or {}
        score = ind.get("score") or {}
        lines = [
            f"Indicator [{ind.get('type', '?')}] {_trunc(ind.get('value'), 160)} "
            f"(id {ind.get('id', iid)})",
            f"Score: {score.get('value', 'unscored')} | "
            f"Created: {_ts(ind.get('created_at'))} | "
            f"Modified: {_ts(ind.get('modified_at'))} | "
            f"Last seen: {_ts(ind.get('last_seen_at'))} | "
            f"Total sightings: {ind.get('total_sightings', '?')}",
        ]
        for key, label in (
            ("malware_description", "Malware"),
            ("apt_description", "Actor"),
        ):
            if ind.get(key):
                lines.append(f"{label}: {_trunc(ind[key], 500)}")
        mitre = ind.get("mitre_attack_ids") or []
        if mitre:
            ids = [
                f"{m.get('id', '?')} ({m.get('name', '?')})" if isinstance(m, dict) else str(m)
                for m in mitre[:12]
            ]
            lines.append(f"MITRE ATT&CK: {', '.join(ids)}")
        hashes = ind.get("hashes") or {}
        if hashes:
            lines.append(
                "Hashes: "
                + ", ".join(f"{k}={_trunc(v, 70)}" for k, v in list(hashes.items())[:6])
            )
        sightings = ind.get("sightings") or []
        if sightings:
            lines.append(f"Sightings ({len(sightings)} shown):")
            for s in sightings[:10]:
                stags = s.get("tags") or []
                lines.append(
                    f"  - {_ts(s.get('sighted_at'))} via {s.get('source', '?')}"
                    + (f" | tags: {_trunc(', '.join(map(str, stags[:6])), 140)}" if stags else "")
                )
        related = ((ind.get("relationships") or {}).get("iocs")) or []
        if related:
            lines.append("Related IOCs:")
            for r in related[:10]:
                if isinstance(r, dict):
                    lines.append(
                        f"  - [{r.get('type', '?')}] {_trunc(r.get('value'), 90)} "
                        f"(id {r.get('id', '?')})"
                    )
        ignite_url = (ind.get("platform_urls") or {}).get("ignite")
        if ignite_url:
            lines.append(f"Link: {ignite_url}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 3. TECHNICAL INTELLIGENCE - EVENTS
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_event(ev: dict) -> str:
        event = ev.get("Event", ev) or {}
        name = event.get("info") or ev.get("name") or "?"
        tags = ev.get("Tag") or event.get("Tag") or []
        tag_names = [
            t.get("name", str(t)) if isinstance(t, dict) else str(t) for t in tags[:8]
        ]
        line = f"  {_trunc(name, 110)} (fpid {ev.get('fpid', event.get('fpid', '?'))})"
        ts = event.get("timestamp") or ev.get("timestamp")
        if ts:
            line += f"\n    Observed: {_ts(ts)}"
        if tag_names:
            line += f"\n    Tags: {_trunc(', '.join(tag_names), 200)}"
        return line

    async def list_flashpoint_events(
        time_period: str = "",
        report_fpid: str = "",
        attack_ids: str = "",
        limit: int = 10,
    ) -> str:
        """List technical intelligence events (observations)."""
        params: dict[str, Any] = {
            "limit": _clamp(limit, 10, 1, 1000),
            "sort_timestamp": "desc",
        }
        if time_period.strip():
            params["time_period"] = time_period.strip()
        if report_fpid.strip():
            params["report"] = report_fpid.strip()
        if _csv(attack_ids):
            params["attack_ids"] = _csv(attack_ids)
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/technical-intelligence/v1/event",
                _PROVIDER,
                "list events",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or []
        events = data if isinstance(data, list) else data.get("data", []) or []
        return format_entity_list(
            events,
            _fmt_event,
            noun="event",
            empty_msg=(
                "No events matched. Widen time_period (e.g. '30d') or drop "
                "the attack_ids filter."
            ),
        )

    async def get_flashpoint_event(event_id: str) -> str:
        """Get one technical intelligence event by FPID or UUID."""
        eid = event_id.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/technical-intelligence/v1/event/{_path(eid)}",
                _PROVIDER,
                f"get event {eid}",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() or []
        items = data if isinstance(data, list) else [data]
        if not items:
            return f"Event {eid} was not found."
        ev = items[0] or {}
        event = ev.get("Event", ev) or {}
        lines = [
            f"Event: {_trunc(event.get('info') or ev.get('name'), 160)} "
            f"(fpid {ev.get('fpid', eid)})",
            f"Observed: {_ts(event.get('timestamp') or ev.get('timestamp'))} | "
            f"UUID: {event.get('uuid', ev.get('uuid', '?'))} | "
            f"Creator: {event.get('event_creator_email', '?')}",
        ]
        tags = ev.get("Tag") or event.get("Tag") or []
        if tags:
            tag_names = [
                t.get("name", str(t)) if isinstance(t, dict) else str(t) for t in tags[:15]
            ]
            lines.append(f"Tags: {_trunc(', '.join(tag_names), 400)}")
        if ev.get("malware_description"):
            lines.append(f"Malware: {_trunc(ev.get('malware_description'), 600)}")
        attrs = ev.get("Attribute") or event.get("Attribute") or []
        if attrs:
            lines.append(f"Attributes ({min(len(attrs), 15)} of {len(attrs)}):")
            for a in attrs[:15]:
                lines.append(
                    f"  - [{a.get('type', '?')}] {_trunc(a.get('value'), 100)} "
                    f"(category {a.get('category', '?')})"
                )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 4. COMMUNITIES INTELLIGENCE
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_community(doc: dict) -> str:
        container = doc.get("container") or {}
        site = doc.get("site") or {}
        actor = doc.get("site_actor") or {}
        actor_name = (actor.get("names") or {}).get("handle") or actor.get("username")
        line = (
            f"  {_trunc(doc.get('title') or (doc.get('message') or '')[:80], 100)} "
            f"(id {doc.get('id', '?')})\n"
            f"    Source: {site.get('title', doc.get('source', '?'))} | "
            f"Container: {container.get('title') or container.get('name') or 'N/A'} | "
            f"Date: {_ts(doc.get('date') or doc.get('sort_date'))}"
        )
        if actor_name:
            line += f" | Actor: {_trunc(actor_name, 40)}"
        body = doc.get("message") or (doc.get("body") or {}).get("text/plain") or ""
        if body:
            line += f"\n    {_trunc(body, 240)}"
        line += f"\n    Link: {_APP_URL}/search/context/communities/{_path(doc.get('id', ''))}"
        return line

    async def search_flashpoint_communities(
        query: str,
        since: str = "",
        until: str = "",
        size: int = 10,
        page: int = 0,
    ) -> str:
        """Search illicit communities (forums, chats, paste sites) posts."""
        if not query.strip():
            return "Error: query is required."
        body: dict[str, Any] = {
            "query": query.strip(),
            "size": _clamp(size, 10, 1, 100),
            "page": max(0, int(page or 0)),
            "sort": {"date": "desc"},
        }
        if since.strip() or until.strip():
            body["include"] = {
                "date": {"start": since.strip(), "end": until.strip()}
            }
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/sources/v2/communities",
                _PROVIDER,
                "search communities",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items", []) or []
        total = data.get("total")
        if isinstance(total, dict):
            total = total.get("value")
        out = format_entity_list(
            items,
            _fmt_community,
            noun="community post",
            total=total if isinstance(total, int) else None,
            empty_msg=(
                f"No community posts matched '{query.strip()}'. Communities "
                "search supports quoted phrases and boolean operators."
            ),
        )
        if isinstance(total, int) and total > (body["page"] + 1) * body["size"]:
            out += f"\n[More results - pass page={body['page'] + 1}]"
        return out

    async def get_flashpoint_community_post(document_id: str) -> str:
        """Get one communities document (forum post, chat message, ...)."""
        did = document_id.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/sources/v2/communities/{_path(did)}",
                _PROVIDER,
                f"get community document {did}",
                headers=headers,
            )
            if err:
                return err
            doc = resp.json() or {}
        container = doc.get("container") or {}
        site = doc.get("site") or {}
        actor = doc.get("site_actor") or {}
        actor_name = (actor.get("names") or {}).get("handle") or actor.get("username")
        lines = [
            f"Community document {doc.get('id', did)}",
            f"Title: {_trunc(doc.get('title'), 160) or 'N/A'}",
            f"Source: {site.get('title', doc.get('source', '?'))} | "
            f"Container: {container.get('title') or container.get('name') or 'N/A'} | "
            f"Date: {_ts(doc.get('date') or doc.get('sort_date'))}",
        ]
        if actor_name:
            lines.append(f"Actor: {_trunc(actor_name, 80)}")
        body = doc.get("message") or (doc.get("body") or {}).get("text/plain") or ""
        if body:
            lines.append(f"\nContent: {_trunc(body, 2000)}")
        media = _media_lines(doc.get("media_v2"))
        if media:
            lines.append("")
            lines.extend(media)
            lines.append("download_flashpoint_media(storage_uri) stores a media asset as a file.")
        lines.append(
            f"\nLink: {_APP_URL}/search/context/communities/{_path(doc.get('id', did))}"
        )
        return "\n".join(lines)

    async def download_flashpoint_media(
        storage_uri: str = "", media_id: str = "", file_context: FileContext | None = None
    ) -> str | ToolResult:
        """Store a media asset (image, video, file posted in a community) as a file of the run."""
        storage_uri = (storage_uri or "").strip()
        media_id = (media_id or "").strip()
        if bool(storage_uri) == bool(media_id):
            return (
                "Error: provide exactly one of 'storage_uri' (from a document's "
                "or alert's media) or 'media_id' (a media document id)."
            )
        name = ""
        async with _client(timeout=120) as client:
            if media_id:
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{api}/sources/v2/media/{_path(media_id)}",
                    _PROVIDER,
                    f"get media document {media_id}",
                    headers=headers,
                )
                if err:
                    return err
                doc = resp.json() or {}
                storage_uri = str(doc.get("storage_uri") or "")
                name = str(doc.get("file_name") or "")
                if not storage_uri:
                    return f"Media document {media_id} carries no stored asset."
            fetched = await fetch_file(
                client,
                "get",
                f"{api}/sources/v1/media",
                provider=_PROVIDER,
                action="download media asset",
                max_bytes=_MAX_MEDIA_BYTES,
                headers={**headers, "Accept": "*/*"},
                params={"cdn": "false", "asset_id": storage_uri},
                follow_redirects=True,
            )
        link = f"{api}/sources/v1/media?cdn=false&asset_id={quote(storage_uri, safe='')}"
        if isinstance(fetched, str):
            return f"{fetched}\nDownload URL: {link}"
        data = fetched.data
        name = name or fetched.filename or storage_uri.rstrip("/").rsplit("/", 1)[-1] or "flashpoint-media"
        content_type = await _verified_media_type(data)
        if content_type is None:
            return await deliver_live_artefact(
                data=data,
                filename=name,
                source=_PROVIDER,
                persist_file_fn=persist_file_fn,
                links=[("Download URL", link)],
            )
        name = _media_filename(name, content_type)
        if content_type in _VIDEO_EXTENSIONS:
            file_ref = await keep_file(persist_file_fn, file_context, name, data, content_type)
            # The video stays off the tool result: the model reads no video,
            # and whatever else its container holds never reaches it as text.
            return ToolResult(
                tool_call_id="",
                content="\n".join(
                    [
                        f"Video: {name} ({content_type}, {format_kb(len(data))}) from {_PROVIDER}.",
                        kept_file_line(file_ref),
                        f"Download URL: {link}",
                        "It is a video, so its content is not shown here.",
                    ]
                ),
            )
        return await deliver_file(
            data=data,
            filename=name,
            content_type=content_type,
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[("Download URL", link)],
        )

    # ══════════════════════════════════════════════════════════════════════
    # 5. COMPROMISED CREDENTIALS MONITORING (CCM)
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_credential(hit: dict) -> str:
        src = hit.get("_source", hit) or {}
        breach = src.get("breach") or {}
        line = (
            f"  {_trunc(src.get('email') or src.get('username'), 80)} "
            f"(fpid {src.get('fpid', '?')})\n"
            f"    Domain: {src.get('domain', 'N/A')} | "
            f"Affected: {_trunc(scrub_exposure_text(src.get('affected_domain') or src.get('affected_url')), 60) or 'N/A'} | "
            f"Fresh: {src.get('is_fresh', '?')} | "
            f"Password: {presence(src.get('password'))}"
        )
        line += (
            f"\n    Breach: {_trunc(breach.get('title'), 110)} "
            f"(source: {breach.get('source', '?')} / {breach.get('source_type', '?')}, "
            f"first observed {_ts(breach.get('first_observed_at'))})"
        )
        return line

    async def search_flashpoint_credentials(
        query: str = "",
        start_date: str = "",
        end_date: str = "",
        filter_date: str = "created_at",
        fresh_only: bool = False,
        limit: int = 20,
        skip: int = 0,
        sort_order: str = "",
    ) -> str:
        """Search compromised credential sightings (CCM breach data)."""
        fdate = filter_date.strip() or "created_at"
        if fdate not in ("created_at", "first_observed_at"):
            return "Error: filter_date must be 'created_at' or 'first_observed_at'."
        lucene = "+basetypes:(credential-sighting)"
        if fresh_only:
            lucene += " +is_fresh:true"
        if start_date.strip():
            end = end_date.strip() or "now"
            lucene += f" +breach.{fdate}.date-time: [{start_date.strip()} TO {end}]"
        if query.strip():
            lucene += f" {query.strip()}"
        params: dict[str, Any] = {
            "query": lucene,
            "limit": _clamp(limit, 20, 1, 1000),
            "skip": max(0, int(skip or 0)),
        }
        if sort_order.strip():
            if sort_order.strip().lower() not in ("asc", "desc"):
                return "Error: sort_order must be 'asc' or 'desc'."
            params["sort"] = f"breach.{fdate}.timestamp:{sort_order.strip().lower()}"
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/sources/v1/noncommunities/search",
                _PROVIDER,
                "search compromised credentials",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        hits = (data.get("hits") or {}).get("hits", []) or []
        total = (data.get("hits") or {}).get("total")
        if isinstance(total, dict):
            total = total.get("value")
        out = format_entity_list(
            hits,
            _fmt_credential,
            noun="credential sighting",
            total=total if isinstance(total, int) else None,
            empty_msg=(
                "No compromised credential sightings matched. CCM requires an "
                "active Flashpoint CCM subscription and a token created after "
                "the subscription was enabled."
            ),
        )
        if isinstance(total, int) and total > params["skip"] + len(hits):
            out += (
                f"\n[Showing {len(hits)} from offset {params['skip']} of {total} - "
                f"pass skip={params['skip'] + len(hits)} for more]"
            )
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 6. ALERTING
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_alert(a: dict) -> str:
        reason = a.get("reason") or {}
        resource = a.get("resource") or {}
        title = (
            resource.get("title")
            or a.get("highlight_text")
            or (resource.get("site") or {}).get("title")
            or resource.get("repo")
            or "?"
        )
        line = (
            f"  {_trunc(title, 100)} (alert {a.get('id', '?')})\n"
            f"    Source: {a.get('source', a.get('data_type', '?'))} | "
            f"Reason: {_trunc(reason.get('name') or reason.get('text'), 60) or 'N/A'} "
            f"({reason.get('origin', '?')}) | "
            f"Status: {a.get('status') or 'none'} | "
            f"Created: {_ts(a.get('created_at'))}"
        )
        tags = a.get("tags") or {}
        if isinstance(tags, list) and tags:
            line += f"\n    Tags: {_trunc(', '.join(map(str, tags[:8])), 160)}"
        if resource.get("url"):
            line += f"\n    Resource: {_trunc(resource.get('url'), 140)}"
        media = _media_lines(resource.get("media_v2"))
        if media:
            line += "\n" + "\n".join(media)
        return line

    async def list_flashpoint_alerts(
        created_after: str = "",
        created_before: str = "",
        status: str = "",
        origin: str = "",
        sources: str = "",
        tags: str = "",
        asset_ip: str = "",
        size: int = 20,
        cursor: str = "",
    ) -> str:
        """List Ignite alert notifications (saved-search + asset alerts)."""
        params: dict[str, Any] = {"size": _clamp(size, 20, 1, 100)}
        if created_after.strip():
            params["created_after"] = created_after.strip()
        if created_before.strip():
            params["created_before"] = created_before.strip()
        if cursor.strip():
            params["cursor"] = cursor.strip()
        if status.strip():
            key = status.strip().lower()
            if key not in _ALERT_STATUSES:
                return (
                    f"Error: status must be one of {', '.join(sorted(_ALERT_STATUSES))}."
                )
            params["status"] = _ALERT_STATUSES[key]
        if origin.strip():
            if origin.strip().lower() not in ("searches", "assets"):
                return "Error: origin must be 'searches' or 'assets'."
            params["origin"] = origin.strip().lower()
        if sources.strip():
            mapped = []
            for s in sources.split(","):
                key = s.strip().lower()
                if not key:
                    continue
                if key not in _ALERT_SOURCES:
                    return (
                        f"Error: sources values must be among "
                        f"{', '.join(sorted(_ALERT_SOURCES))}."
                    )
                mapped.append(_ALERT_SOURCES[key])
            if mapped:
                params["sources"] = ",".join(mapped)
        if _csv(tags):
            params["tags"] = _csv(tags)
        if asset_ip.strip():
            params["asset_ip"] = asset_ip.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/alert-management/v1/notifications",
                _PROVIDER,
                "list alerts",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items", []) or []
        out = format_entity_list(
            items,
            _fmt_alert,
            noun="alert",
            empty_msg="No alerts matched the filters.",
        )
        next_url = (data.get("pagination") or {}).get("next")
        if next_url:
            next_cursor = (parse_qs(urlparse(str(next_url)).query).get("cursor") or [""])[0]
            if next_cursor:
                out += format_pagination_hint(next_cursor, param_name="cursor")
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 7. VULNERABILITY INTELLIGENCE (VulnDB)
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_vulnerability(v: dict) -> str:
        scores = v.get("scores") or {}
        timelines = v.get("timelines") or {}
        cves = v.get("cve_ids") or []
        line = (
            f"  {_trunc(v.get('title'), 110)} (id {v.get('id', '?')})\n"
            f"    CVEs: {_trunc(', '.join(map(str, cves[:6])), 120) or 'not yet assigned'} | "
            f"Severity: {scores.get('severity', '?')} | "
            f"CVSSv3: {scores.get('cvssv3_score', 'N/A')} | "
            f"EPSS: {scores.get('epss_score', 'N/A')} | "
            f"Ransomware: {scores.get('ransomware_score', 'N/A')}"
        )
        line += (
            f"\n    Published: {_ts(timelines.get('published_at'))} | "
            f"Disclosed: {_ts(timelines.get('disclosed_at'))} | "
            f"Exploits: {v.get('exploits_count', 0)} | "
            f"Status: {v.get('vuln_status', '?')}"
        )
        if v.get("description"):
            line += f"\n    {_trunc(v.get('description'), 240)}"
        line += f"\n    Link: {_APP_URL}/vuln/vulnerabilities/{_path(v.get('id', ''))}"
        return line

    def _vulnerability_detail(v: dict) -> str:
        scores = v.get("scores") or {}
        timelines = v.get("timelines") or {}
        cves = v.get("cve_ids") or []
        lines = [
            f"Vulnerability: {_trunc(v.get('title'), 200)} (id {v.get('id', '?')})",
            f"CVEs: {', '.join(map(str, cves)) or 'not yet assigned'}",
            f"Severity: {scores.get('severity', '?')} | "
            f"CVSSv3: {scores.get('cvssv3_score', 'N/A')} | "
            f"EPSS: {scores.get('epss_score', 'N/A')} | "
            f"Ransomware score: {scores.get('ransomware_score', 'N/A')} | "
            f"Status: {v.get('vuln_status', '?')}",
            f"Published: {_ts(timelines.get('published_at'))} | "
            f"Disclosed: {_ts(timelines.get('disclosed_at'))} | "
            f"Last modified: {_ts(timelines.get('last_modified_at'))} | "
            f"Exploited in the wild: {_ts(timelines.get('exploited_in_the_wild_at'))}",
        ]
        if v.get("description"):
            lines.append(f"\nDescription: {_trunc(v.get('description'), 1200)}")
        if v.get("solution"):
            lines.append(f"Solution: {_trunc(v.get('solution'), 600)}")
        cwes = v.get("cwes") or []
        if cwes:
            lines.append(
                "CWEs: "
                + ", ".join(
                    f"{c.get('cwe_id', '?')} {_trunc(c.get('name'), 60)}" for c in cwes[:8]
                )
            )
        exploits = v.get("exploits") or []
        if exploits:
            lines.append(f"Exploits ({len(exploits)}):")
            for e in exploits[:6]:
                lines.append(f"  - [{e.get('type', '?')}] {_trunc(e.get('value'), 140)}")
        refs = v.get("ext_references") or []
        if refs:
            lines.append(f"References ({min(len(refs), 8)} of {len(refs)}):")
            for r in refs[:8]:
                lines.append(f"  - [{r.get('type', '?')}] {_trunc(r.get('value'), 130)}")
        lines.append(f"\nLink: {_APP_URL}/vuln/vulnerabilities/{_path(v.get('id', ''))}")
        return "\n".join(lines)

    async def search_flashpoint_vulnerabilities(
        severities: str = "",
        ransomware_scores: str = "",
        products: str = "",
        vendors: str = "",
        cwe_ids: str = "",
        tags: str = "",
        min_cvssv3_score: float | None = None,
        max_cvssv3_score: float | None = None,
        min_epss_score: float | None = None,
        max_epss_score: float | None = None,
        published_after: str = "",
        published_before: str = "",
        last_touched_after: str = "",
        last_touched_before: str = "",
        size: int = 10,
        from_offset: int = 0,
        sort_by: str = "published_at",
        sort_order: str = "desc",
    ) -> str:
        """Search VulnDB vulnerabilities with score/product/date filters."""
        sort_field = sort_by.strip().lower() or "published_at"
        if sort_field not in _VULN_SORT_FIELDS:
            return f"Error: sort_by must be one of {', '.join(_VULN_SORT_FIELDS)}."
        order = sort_order.strip().lower() or "desc"
        if order not in ("asc", "desc"):
            return "Error: sort_order must be 'asc' or 'desc'."
        query_params: dict[str, Any] = {
            "size": _clamp(size, 10, 1, 1000),
            "from": max(0, int(from_offset or 0)),
            "sort": f"{sort_field}:{order}" if order == "desc" else sort_field,
        }
        body: dict[str, Any] = {}
        if severities.strip():
            vals = [s.strip().lower() for s in severities.split(",") if s.strip()]
            bad = [s for s in vals if s not in _VULN_SEVERITIES]
            if bad:
                return f"Error: severities must be among {', '.join(_VULN_SEVERITIES)}."
            body["severity"] = ",".join(vals)
        if ransomware_scores.strip():
            vals = [s.strip().lower() for s in ransomware_scores.split(",") if s.strip()]
            bad = [s for s in vals if s not in _VULN_RANSOMWARE_SCORES]
            if bad:
                return (
                    f"Error: ransomware_scores must be among "
                    f"{', '.join(_VULN_RANSOMWARE_SCORES)}."
                )
            body["ransomware_score"] = ",".join(vals)
        if _csv(products):
            body["products"] = _csv(products)
        if _csv(vendors):
            body["vendors"] = _csv(vendors)
        if _csv(cwe_ids):
            body["cwe_ids"] = _csv(cwe_ids)
        if _csv(tags):
            body["tags"] = _csv(tags)
        for key, value, lo, hi in (
            ("min_cvssv3_score", min_cvssv3_score, 0.0, 10.0),
            ("max_cvssv3_score", max_cvssv3_score, 0.0, 10.0),
            ("min_epss_score", min_epss_score, 0.0, 1.0),
            ("max_epss_score", max_epss_score, 0.0, 1.0),
        ):
            if value is not None:
                try:
                    fval = float(value)
                except (TypeError, ValueError):
                    return f"Error: {key} must be a number between {lo} and {hi}."
                if not lo <= fval <= hi:
                    return f"Error: {key} must be between {lo} and {hi}."
                body[key] = fval
        for key, value in (
            ("published_after", published_after),
            ("published_before", published_before),
            ("last_touched_after", last_touched_after),
            ("last_touched_before", last_touched_before),
        ):
            if value.strip():
                body[key] = value.strip()
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/vulnerability-intelligence/v1/vulnerabilities",
                _PROVIDER,
                "search vulnerabilities",
                headers=headers,
                params=query_params,
                json=body,
            )
            if err:
                return err
            data = resp.json() or {}
        results = data.get("results", []) or []
        total = data.get("total")
        out = format_entity_list(
            results,
            _fmt_vulnerability,
            noun="vulnerability",
            total=total if isinstance(total, int) else None,
            empty_msg="No vulnerabilities matched the filters.",
        )
        if isinstance(total, int) and total > query_params["from"] + len(results):
            out += (
                f"\n[Showing {len(results)} from offset {query_params['from']} of "
                f"{total} - pass from_offset={query_params['from'] + len(results)} "
                "for more]"
            )
        return out

    async def lookup_flashpoint_cve(cve_ids: str) -> str:
        """Look up vulnerabilities by CVE id(s) in VulnDB."""
        cves = _csv(cve_ids)
        if not cves:
            return "Error: cve_ids is required (e.g. 'CVE-2024-3400' or a comma-separated list)."
        async with _client(timeout=45) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/vulnerability-intelligence/v1/vulnerabilities",
                _PROVIDER,
                f"look up CVE {cves}",
                headers=headers,
                params={"cves": cves},
            )
            if err:
                return err
            data = resp.json() or {}
        results = data.get("results", []) or []
        if not results:
            return (
                f"No VulnDB entry found for {cves}. Flashpoint may track "
                "unpublished vulnerabilities under a title instead of a CVE id "
                "- try search_flashpoint_vulnerabilities."
            )
        return "\n\n".join(_vulnerability_detail(v) for v in results[:5])

    async def get_flashpoint_vulnerability(vulnerability_id: str) -> str:
        """Get one vulnerability by its Flashpoint (VulnDB) numeric id."""
        vid = vulnerability_id.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/vulnerability-intelligence/v1/vulnerabilities/{_path(vid)}",
                _PROVIDER,
                f"get vulnerability {vid}",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() or {}
        results = data.get("results") if isinstance(data, dict) else None
        v = (results[0] if results else data) or {}
        if not v.get("id") and not v.get("title"):
            return f"Vulnerability {vid} was not found in VulnDB."
        return _vulnerability_detail(v)

    async def list_flashpoint_affected_components(
        vulnerability_id: str,
        component_type: str = "packages",
        query: str = "",
        name: str = "",
        size: int = 10,
        from_offset: int = 0,
    ) -> str:
        """List libraries or OS packages affected by a vulnerability."""
        ctype = component_type.strip().lower() or "packages"
        if ctype not in ("packages", "libraries"):
            return "Error: component_type must be 'packages' or 'libraries'."
        vid = vulnerability_id.strip()
        params: dict[str, Any] = {
            "size": _clamp(size, 10, 1, 1000),
            "from": max(0, int(from_offset or 0)),
        }
        if query.strip():
            params["query"] = query.strip()
        if name.strip():
            params["name"] = name.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/vulnerability-intelligence/v1/vulnerabilities/{_path(vid)}/{ctype}",
                _PROVIDER,
                f"list {ctype} for vulnerability {vid}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        results = data.get("results", []) or []
        total = data.get("total")

        def _fmt_component(c: dict) -> str:
            if ctype == "libraries":
                return (
                    f"  {_trunc(c.get('name'), 70)} {c.get('version', '?')} "
                    f"[{c.get('type', '?')}] affected: {c.get('affected', '?')} | "
                    f"purl: {_trunc(c.get('constructed_purl') or c.get('purl'), 110)}"
                )
            return (
                f"  {_trunc(c.get('name'), 70)} {c.get('operator', '')}{c.get('version', '?')} "
                f"on {c.get('os', '?')} {c.get('os_version', '')} "
                f"affected: {c.get('affected', '?')} | "
                f"purl: {_trunc(c.get('purl'), 110)}"
            )

        out = format_entity_list(
            results,
            _fmt_component,
            # Explicit labels: format_entity_list pluralises by appending "s",
            # so "libraries".rstrip("s") would render "1 librarie" and a bare
            # "library" would render "2 librarys".
            noun="library component" if ctype == "libraries" else "package",
            total=total if isinstance(total, int) else None,
            empty_msg=f"No {ctype} recorded for vulnerability {vid}.",
        )
        if isinstance(total, int) and total > params["from"] + len(results):
            out += (
                f"\n[Showing {len(results)} from offset {params['from']} of {total} - "
                f"pass from_offset={params['from'] + len(results)} for more]"
            )
        return out

    async def search_flashpoint_vuln_catalog(
        entity_type: str,
        name: str = "",
        ids: str = "",
        vendor_ids: str = "",
        updated_after: str = "",
        size: int = 10,
        from_offset: int = 0,
    ) -> str:
        """Search the VulnDB vendors/products reference catalog."""
        etype = entity_type.strip().lower()
        if etype not in ("vendors", "products"):
            return "Error: entity_type must be 'vendors' or 'products'."
        params: dict[str, Any] = {
            "size": _clamp(size, 10, 1, 1000),
            "from": max(0, int(from_offset or 0)),
        }
        if name.strip():
            params["name"] = name.strip()
        if _csv(ids):
            params["ids"] = _csv(ids)
        if etype == "products" and _csv(vendor_ids):
            params["vendor_ids"] = _csv(vendor_ids)
        if updated_after.strip():
            params["updated_after"] = updated_after.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/vulnerability-intelligence/v1/{etype}",
                _PROVIDER,
                f"search {etype}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        results = data.get("results", []) or []
        total = data.get("total")

        def _fmt_entry(e: dict) -> str:
            line = f"  {_trunc(e.get('name'), 90)} (id {e.get('id', '?')})"
            vendor = e.get("vendor") or {}
            if isinstance(vendor, dict) and vendor.get("name"):
                line += f" | vendor: {_trunc(vendor.get('name'), 60)} (id {vendor.get('id', '?')})"
            return line

        out = format_entity_list(
            results,
            _fmt_entry,
            noun=etype.rstrip("s"),
            total=total if isinstance(total, int) else None,
            empty_msg=f"No {etype} matched.",
        )
        if isinstance(total, int) and total > params["from"] + len(results):
            out += (
                f"\n[Showing {len(results)} from offset {params['from']} of {total} - "
                f"pass from_offset={params['from'] + len(results)} for more]"
            )
        return out

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_flashpoint_status", get_flashpoint_status)
    executor.register("search_flashpoint_reports", search_flashpoint_reports)
    executor.register("get_flashpoint_report", get_flashpoint_report)
    executor.register("list_flashpoint_related_reports", list_flashpoint_related_reports)
    executor.register("search_flashpoint_indicators", search_flashpoint_indicators)
    executor.register("lookup_flashpoint_ioc", lookup_flashpoint_ioc)
    executor.register("get_flashpoint_indicator", get_flashpoint_indicator)
    executor.register("list_flashpoint_events", list_flashpoint_events)
    executor.register("get_flashpoint_event", get_flashpoint_event)
    executor.register("search_flashpoint_communities", search_flashpoint_communities)
    executor.register("get_flashpoint_community_post", get_flashpoint_community_post)
    executor.register("download_flashpoint_media", download_flashpoint_media)
    executor.register("search_flashpoint_credentials", search_flashpoint_credentials)
    executor.register("list_flashpoint_alerts", list_flashpoint_alerts)
    executor.register(
        "search_flashpoint_vulnerabilities", search_flashpoint_vulnerabilities
    )
    executor.register("lookup_flashpoint_cve", lookup_flashpoint_cve)
    executor.register("get_flashpoint_vulnerability", get_flashpoint_vulnerability)
    executor.register(
        "list_flashpoint_affected_components", list_flashpoint_affected_components
    )
    executor.register("search_flashpoint_vuln_catalog", search_flashpoint_vuln_catalog)

    return [
        ToolDefinition(
            name="get_flashpoint_status",
            description=(
                "Validate the Flashpoint connection and API token. Use first if "
                "other Flashpoint tools fail with authentication errors."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="search_flashpoint_reports",
            description=(
                "Search Flashpoint finished intelligence reports (analyst-written "
                "threat reporting: actor profiles, malware deep-dives, sector "
                "risk, strategic analysis) by keyword and date window. Returns "
                "titles, summaries, tags, and report ids - pass an id to "
                "get_flashpoint_report for the full detail."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword search, e.g. 'LockBit' or 'ChatGPT'",
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Only reports updated after this time (ISO8601 like "
                            "'2026-06-01T00:00:00Z' or relative like '-30d')"
                        ),
                    },
                    "until": {
                        "type": "string",
                        "description": "Only reports updated before this time",
                    },
                    "limit": {"type": "integer", "default": 10, "description": "Max 100"},
                    "skip": {"type": "integer", "default": 0, "description": "Pagination offset"},
                    "sort": {
                        "type": "string",
                        "description": "Sort, e.g. 'updated_at:desc' or 'posted_at:asc'",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_flashpoint_report",
            description=(
                "Get one Flashpoint intelligence report by id: title, summary, "
                "tags, body (truncated), and the Ignite platform link."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "description": "Report id from search_flashpoint_reports",
                    },
                },
                "required": ["report_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_flashpoint_related_reports",
            description=(
                "List intelligence reports related to a given Flashpoint report "
                "- use to pivot from one report to the surrounding analyst "
                "reporting on the same topic."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "report_id": {"type": "string", "description": "Report id"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["report_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_flashpoint_indicators",
            description=(
                "Hunt IOCs in the Flashpoint Technical Intelligence dataset with "
                "structured filters: indicator types, actor/malware/source tags, "
                "MITRE ATT&CK technique ids, score tiers, CIDR ranges, and "
                "last-seen/modified windows. Returns compact indicator summaries "
                "with ids for get_flashpoint_indicator. For a single-IOC "
                "reputation check prefer lookup_flashpoint_ioc."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ioc_value": {
                        "type": "string",
                        "description": "Match indicator values (supports partial matching)",
                    },
                    "ioc_types": {
                        "type": "string",
                        "description": (
                            "Comma-separated types, e.g. 'ipv4,domain,url,file,"
                            "email,md5,sha1,sha256'"
                        ),
                    },
                    "cidr_range": {
                        "type": "string",
                        "description": "CIDR to match ipv4/ipv6 indicators, e.g. '1.2.3.0/24'",
                    },
                    "actors": {
                        "type": "string",
                        "description": "Comma-separated exact actor tags, e.g. 'FIN7'",
                    },
                    "malware": {
                        "type": "string",
                        "description": "Comma-separated exact malware tags, e.g. 'xworm,redline'",
                    },
                    "sources": {
                        "type": "string",
                        "description": "Comma-separated source tags, e.g. 'flashpoint_extraction'",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated exact tags, e.g. 'extracted_config:true'",
                    },
                    "mitre_attack_ids": {
                        "type": "string",
                        "description": "Comma-separated ATT&CK ids, e.g. 'T1055,T1071'",
                    },
                    "min_score": {
                        "type": "string",
                        "enum": list(_SCORE_TIERS),
                        "description": "Minimum score tier (malicious is the worst)",
                    },
                    "max_score": {
                        "type": "string",
                        "enum": list(_SCORE_TIERS),
                        "description": "Maximum score tier",
                    },
                    "has_intel_report": {
                        "type": "boolean",
                        "default": False,
                        "description": "Only indicators with an associated intelligence report",
                    },
                    "last_seen_after": {
                        "type": "string",
                        "description": "ISO8601 or relative ('-7d') last-seen lower bound",
                    },
                    "modified_after": {
                        "type": "string",
                        "description": "ISO8601 or relative modified lower bound",
                    },
                    "sort": {
                        "type": "string",
                        "default": "last_seen_at:desc",
                        "description": "field:direction, e.g. 'modified_at:asc'",
                    },
                    "size": {"type": "integer", "default": 20, "description": "Max 500"},
                    "from_offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_flashpoint_ioc",
            description=(
                "Reputation check for a single IOC (IP, domain, URL, file hash, "
                "email) against the Flashpoint IOC dataset. Returns the score "
                "tier (malicious/suspicious/informational), sighting recency, "
                "malware/actor context, related IOCs, and the Ignite link. Use "
                "during alert triage to answer 'is this IOC bad?'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "The IOC value, e.g. '1.2.3.4' or 'evil.example.com'",
                    },
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["value"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_flashpoint_indicator",
            description=(
                "Get one Flashpoint indicator by id: score, timeline, "
                "malware/actor descriptions, MITRE ATT&CK techniques, file "
                "hashes, recent sightings with tags, and related IOCs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "indicator_id": {
                        "type": "string",
                        "description": "Indicator id from search_flashpoint_indicators",
                    },
                    "sighting_count": {
                        "type": "integer",
                        "default": 5,
                        "description": "Most recent sightings to include (max 100)",
                    },
                },
                "required": ["indicator_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_flashpoint_events",
            description=(
                "List Flashpoint technical intelligence events (observations "
                "grouping IOCs, newest first). Filter by MITRE ATT&CK ids, a "
                "report FPID (events referenced by a report), or a time period."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "time_period": {
                        "type": "string",
                        "description": "Search window, e.g. '24h', '7d', '30d'",
                    },
                    "report_fpid": {
                        "type": "string",
                        "description": "Only events referenced by this report FPID",
                    },
                    "attack_ids": {
                        "type": "string",
                        "description": "Comma-separated MITRE ATT&CK ids, e.g. 'T1001'",
                    },
                    "limit": {"type": "integer", "default": 10, "description": "Max 1000"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_flashpoint_event",
            description=(
                "Get one technical intelligence event by FPID or UUID: name, "
                "observed time, tags, malware description, and the IOC "
                "attributes it contains."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "Event FPID or UUID from list_flashpoint_events",
                    },
                },
                "required": ["event_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_flashpoint_communities",
            description=(
                "Full-text search of Flashpoint Communities Intelligence - "
                "illicit forums, chat services, marketplaces, and paste sites. "
                "Use for questions like 'is anyone discussing a company, brand or "
                "CVE underground?'. Supports quoted phrases and boolean "
                "operators. Returns post snippets, source sites, actor handles, "
                "and document ids for get_flashpoint_community_post."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search terms, e.g. '\"acme corp\" AND vpn'",
                    },
                    "since": {
                        "type": "string",
                        "description": "Window start (ISO8601 or relative like 'now-30d')",
                    },
                    "until": {
                        "type": "string",
                        "description": "Window end (ISO8601; empty = now)",
                    },
                    "size": {"type": "integer", "default": 10, "description": "Max 100"},
                    "page": {"type": "integer", "default": 0, "description": "Page number (0-based)"},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_flashpoint_community_post",
            description=(
                "Get one communities document (forum post, chat message, paste) "
                "by id: full content, source site/container, actor handle, its "
                "media (images, videos, files with their storage_uri), and the "
                "Ignite context link."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "string",
                        "description": "Document id from search_flashpoint_communities",
                    },
                },
                "required": ["document_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="download_flashpoint_media",
            workspace_ingest=True,
            description=(
                "Store a Flashpoint media asset - by the storage_uri a community "
                "document or media alert lists, or by media document id - as a "
                "file of the conversation (download card + file_id to forward) "
                "with its link. An image that decodes or an MP4 / MOV with a video "
                "track (judged by the bytes, never the label) is stored under its "
                "type's extension; an image is also shown inline, a video is not. "
                "Any other file "
                "posted in an illicit community can be live malware: it is stored "
                "only when it is a ZIP sealed with the password 'infected', never "
                "read inline; otherwise its link and the reason are given."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "storage_uri": {
                        "type": "string",
                        "description": "The media's storage_uri (from get_flashpoint_community_post or list_flashpoint_alerts)",
                    },
                    "media_id": {
                        "type": "string",
                        "description": "A media document id (instead of storage_uri)",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_flashpoint_credentials",
            description=(
                "Search Flashpoint Compromised Credentials Monitoring (CCM) "
                "sightings - employee credentials found in stealer logs, breach "
                "dumps, and dark-web marketplaces. Filter by breach date window "
                "and freshness; refine with an extra Lucene query (e.g. "
                "'+domain:example.com' for a specific email domain). Requires a "
                "CCM subscription. Returns email/username, breach source, "
                "password-capture state, and freshness."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional extra Lucene filter appended to the "
                            "credential-sighting query, e.g. '+domain:example.com'"
                        ),
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Breach window start (ISO8601 like '2026-06-01T00:00:00Z' or 'now-7d')",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Breach window end (defaults to now when start_date is set)",
                    },
                    "filter_date": {
                        "type": "string",
                        "enum": ["created_at", "first_observed_at"],
                        "default": "created_at",
                        "description": "Which breach date the window filters on",
                    },
                    "fresh_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Only never-seen-before credentials",
                    },
                    "limit": {"type": "integer", "default": 20, "description": "Max 1000"},
                    "skip": {"type": "integer", "default": 0, "description": "Pagination offset (limit+skip <= 10000)"},
                    "sort_order": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "description": "Sort by the filter_date field",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_flashpoint_alerts",
            description=(
                "List Flashpoint Ignite alert notifications - hits on your "
                "saved searches and monitored assets (keyword mentions, data "
                "exposure on GitHub/GitLab/Bitbucket, communities chatter, "
                "marketplace listings, leaked images). Filter by date window, "
                "source, status, origin, tags, or asset IP. Cursor-paginated."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "created_after": {
                        "type": "string",
                        "description": "ISO8601 lower bound, e.g. '2026-07-01T00:00:00Z'",
                    },
                    "created_before": {"type": "string", "description": "ISO8601 upper bound"},
                    "status": {
                        "type": "string",
                        "enum": sorted(_ALERT_STATUSES),
                        "description": "Alert status filter",
                    },
                    "origin": {
                        "type": "string",
                        "enum": ["searches", "assets"],
                        "description": "Saved-search alerts or monitored-asset alerts",
                    },
                    "sources": {
                        "type": "string",
                        "description": (
                            "Comma-separated sources among: "
                            + ", ".join(sorted(_ALERT_SOURCES))
                        ),
                    },
                    "tags": {"type": "string", "description": "Comma-separated tag filter"},
                    "asset_ip": {"type": "string", "description": "Filter by monitored asset IP"},
                    "size": {"type": "integer", "default": 20, "description": "Max 100"},
                    "cursor": {
                        "type": "string",
                        "description": "Pagination cursor from the previous call",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_flashpoint_vulnerabilities",
            description=(
                "Search Flashpoint VulnDB vulnerability intelligence (includes "
                "100k+ vulnerabilities missing from NVD/CVE) with filters: "
                "severity, ransomware exploitation score, EPSS and CVSSv3 "
                "ranges, affected products/vendors, CWE ids, tags, and "
                "published/last-touched windows. Returns Flashpoint ids for "
                "get_flashpoint_vulnerability. For a known CVE id use "
                "lookup_flashpoint_cve instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "severities": {
                        "type": "string",
                        "description": (
                            "Comma-separated among: " + ", ".join(_VULN_SEVERITIES)
                        ),
                    },
                    "ransomware_scores": {
                        "type": "string",
                        "description": (
                            "Comma-separated among: " + ", ".join(_VULN_RANSOMWARE_SCORES)
                        ),
                    },
                    "products": {
                        "type": "string",
                        "description": "Comma-separated product names (case-insensitive)",
                    },
                    "vendors": {
                        "type": "string",
                        "description": "Comma-separated vendor names (case-insensitive)",
                    },
                    "cwe_ids": {
                        "type": "string",
                        "description": "Comma-separated numeric CWE ids, e.g. '79,89'",
                    },
                    "tags": {"type": "string", "description": "Comma-separated tags"},
                    "min_cvssv3_score": {"type": "number", "description": "0-10"},
                    "max_cvssv3_score": {"type": "number", "description": "0-10"},
                    "min_epss_score": {"type": "number", "description": "0-1, e.g. 0.5"},
                    "max_epss_score": {"type": "number", "description": "0-1"},
                    "published_after": {
                        "type": "string",
                        "description": "ISO8601 or relative, e.g. '-30d'",
                    },
                    "published_before": {"type": "string"},
                    "last_touched_after": {
                        "type": "string",
                        "description": "Any-field update lower bound",
                    },
                    "last_touched_before": {"type": "string"},
                    "size": {"type": "integer", "default": 10, "description": "Max 1000"},
                    "from_offset": {"type": "integer", "default": 0},
                    "sort_by": {
                        "type": "string",
                        "enum": list(_VULN_SORT_FIELDS),
                        "default": "published_at",
                    },
                    "sort_order": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "default": "desc",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_flashpoint_cve",
            description=(
                "Look up one or more CVE ids in Flashpoint VulnDB. Returns the "
                "full vulnerability record(s): severity, CVSS/EPSS/ransomware "
                "scores, timeline (disclosure, exploit publication, exploited "
                "in the wild), description, solution, CWEs, exploits, and "
                "references."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve_ids": {
                        "type": "string",
                        "description": "CVE id or comma-separated list, e.g. 'CVE-2024-3400'",
                    },
                },
                "required": ["cve_ids"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_flashpoint_vulnerability",
            description=(
                "Get one vulnerability by its Flashpoint (VulnDB) numeric id - "
                "full detail including scores, timeline, solution, CWEs, "
                "exploits, and external references. Use lookup_flashpoint_cve "
                "when you have a CVE id instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "vulnerability_id": {
                        "type": "string",
                        "description": "Flashpoint vulnerability id, e.g. '351481'",
                    },
                },
                "required": ["vulnerability_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_flashpoint_affected_components",
            description=(
                "List the software components affected by a VulnDB "
                "vulnerability: 'packages' (OS packages with distro/version "
                "constraints) or 'libraries' (language ecosystem packages with "
                "purl identifiers). Use to assess exposure of a specific stack."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "vulnerability_id": {
                        "type": "string",
                        "description": "Flashpoint vulnerability id",
                    },
                    "component_type": {
                        "type": "string",
                        "enum": ["packages", "libraries"],
                        "default": "packages",
                    },
                    "query": {
                        "type": "string",
                        "description": "Free-text filter (name, version, OS, ecosystem)",
                    },
                    "name": {"type": "string", "description": "Exact component name filter"},
                    "size": {"type": "integer", "default": 10, "description": "Max 1000"},
                    "from_offset": {"type": "integer", "default": 0},
                },
                "required": ["vulnerability_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_flashpoint_vuln_catalog",
            description=(
                "Search the VulnDB reference catalog of 'vendors' or 'products' "
                "- resolve the exact vendor/product names and ids used by "
                "search_flashpoint_vulnerabilities filters."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "enum": ["vendors", "products"],
                    },
                    "name": {
                        "type": "string",
                        "description": "Name filter (case-insensitive), e.g. 'fortinet'",
                    },
                    "ids": {"type": "string", "description": "Comma-separated numeric ids"},
                    "vendor_ids": {
                        "type": "string",
                        "description": "Products only: filter by vendor id(s)",
                    },
                    "updated_after": {
                        "type": "string",
                        "description": "ISO8601 or relative update lower bound",
                    },
                    "size": {"type": "integer", "default": 10, "description": "Max 1000"},
                    "from_offset": {"type": "integer", "default": 0},
                },
                "required": ["entity_type"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
