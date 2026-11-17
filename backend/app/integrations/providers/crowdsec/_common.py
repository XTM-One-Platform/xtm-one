"""Shared helpers for the CrowdSec provider package.

CrowdSec exposes two independent APIs, each authenticated with its own API key
sent in the ``x-api-key`` header:

* CTI API v2 (``https://cti.api.crowdsec.net/v2``) - crowd-sourced IP
  reputation: single lookup (``/smoke/{ip}``), Lucene search
  (``/smoke/search``), batch lookup (``/smoke``, premium) and the community
  blocklist (``/fire``, premium).
* Service API v1 (``https://admin.api.crowdsec.net/v1``) - console
  management: decisions, blocklists, allowlists, integration endpoints, the
  CVE Live Exploit Tracker, and remediation metrics.

The dispatcher (``integration_tools._get_token``) hands the tool builder the
compound ``cti_api_key||service_api_key`` token (CTI key from ``api_key_enc``,
Service API key from ``extra_config.service_api_key``). Either key may be
omitted - the matching tool subset degrades with an actionable error at call
time instead of silently disappearing.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client

_PROVIDER = "CrowdSec"
_SOURCE = "integration:crowdsec"

# Fixed SaaS hosts - CrowdSec has no self-hosted CTI/Service API, so there is
# no per-integration base URL (and nothing to register in
# integration_tools._USER_FACING_BASE_URL_PROVIDERS).
CTI_API = "https://cti.api.crowdsec.net/v2"
SERVICE_API = "https://admin.api.crowdsec.net/v1"

# Actionable errors returned when a tool needs a key that is not configured.
MISSING_CTI_KEY_MSG = (
    "Error: the CrowdSec CTI API key is not configured. Create one in the "
    "CrowdSec Console under Settings > CTI API Keys and set it as this "
    "integration's API Key."
)
MISSING_SERVICE_KEY_MSG = (
    "Error: the CrowdSec Service API key is not configured. Create one in the "
    "CrowdSec Console under Settings > Service API Keys (a premium feature) "
    "and set it in this integration's 'service_api_key' field."
)

# CTI durations: <n>h / <n>d / <n>m (hours, days, minutes). The Service API
# tracker endpoints accept only <n>h / <n>d.
_CTI_DURATION_RE = re.compile(r"^\d+[hdm]$")
_SERVICE_DURATION_RE = re.compile(r"^\d+[hd]$")

_REPUTATION_ICONS: dict[str, str] = {
    "malicious": "\U0001f534",  # red circle
    "suspicious": "\U0001f7e0",  # orange circle
    "unknown": "\u26aa",  # white circle
    "known": "\U0001f535",  # blue circle
    "safe": "\U0001f7e2",  # green circle
    "benign": "\U0001f7e2",
}


def build_crowdsec_headers(api_key: str) -> dict[str, str]:
    """Build CrowdSec request headers (the API key goes in ``x-api-key``).

    Both the CTI API and the Service API authenticate this way. The key is
    stripped so trailing/leading whitespace (common when pasting credentials)
    can never produce a malformed header. Single chokepoint shared by the tool
    builders and the connectivity test.
    """
    return {
        "x-api-key": (api_key or "").strip(),
        "Accept": "application/json",
    }


def parse_crowdsec_token(token: str) -> tuple[str, str]:
    """Split the compound ``cti_api_key||service_api_key`` token.

    Returns ``(cti_key, service_key)``, both stripped; either may be ``""``
    when that key was not configured.
    """
    parts = (token or "").split("||")
    cti_key = parts[0].strip() if parts else ""
    service_key = parts[1].strip() if len(parts) > 1 else ""
    return cti_key, service_key


def client(timeout: int = 30) -> httpx.AsyncClient:
    """Return a fresh async HTTP client for a single CrowdSec call."""
    return egress_client(timeout=timeout)


def clamp(value: Any, default: int = 50, hi: int = 100, lo: int = 1) -> int:
    """Clamp a caller-supplied integer into ``[lo, hi]``; fall back on garbage."""
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def trunc(text: Any, max_len: int = 300) -> str:
    """Collapse whitespace and truncate ``text`` to ``max_len`` chars."""
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("\u2026" if len(text) > max_len else "")


def dt(value: Any) -> str:
    """Trim an ISO timestamp to second precision for display."""
    return str(value or "")[:19]


def is_valid_ip(value: Any) -> bool:
    """True if *value* parses as an IP address (v4 or v6).

    IPs are dropped into request paths, so strict parsing doubles as the
    path-injection guard: a parseable address can never contain ``/``, ``?``,
    ``#``, whitespace, or traversal tokens.
    """
    try:
        ipaddress.ip_address(str(value or "").strip())
        return True
    except ValueError:
        return False


# Service API object ids are Mongo ObjectIds (24 hex chars). Strict matching
# doubles as the path-injection guard for /blocklists/{id}, /allowlists/{id},
# item ids and integration ids.
_OBJECT_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")

_CVE_ID_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)

# CrowdSec decision durations are Go-style compound durations, optionally with
# a day suffix (e.g. ``24h``, ``30m``, ``1h30m``, ``7d``).
_DECISION_DURATION_RE = re.compile(r"^(\d+[smhd])+$")


def is_valid_object_id(value: Any) -> bool:
    """True if *value* is a well-formed Service API ObjectId (24 hex chars)."""
    return bool(_OBJECT_ID_RE.fullmatch(str(value or "").strip()))


def is_valid_cve_id(value: Any) -> bool:
    """True if *value* is a well-formed CVE id (``CVE-YYYY-NNNN...``)."""
    return bool(_CVE_ID_RE.fullmatch(str(value or "").strip()))


def is_valid_decision_duration(value: Any) -> bool:
    """True if *value* looks like a CrowdSec decision duration (24h, 1h30m, 7d)."""
    return bool(_DECISION_DURATION_RE.fullmatch(str(value or "").strip()))


def quote_path(value: Any) -> str:
    """Percent-encode a free-form path segment (vendor/product/tag/rule names).

    ``safe=""`` also encodes ``/``, so a crafted name can never traverse into a
    different endpoint.
    """
    return quote(str(value or "").strip(), safe="")


def validate_iso_datetime(value: str, param: str) -> str | None:
    """Return an error string when *value* is not an ISO 8601 datetime.

    Accepts a trailing ``Z`` (normalised by the caller before sending when
    needed - the Service API accepts both forms).
    """
    raw = str(value or "").strip()
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return (
            f"Error: '{param}' must be an ISO 8601 datetime like "
            f"'2026-12-31T00:00:00Z' - got '{value}'."
        )
    return None


def is_valid_ip_or_network(value: Any) -> bool:
    """True if *value* parses as an IP address or a CIDR network."""
    raw = str(value or "").strip()
    try:
        ipaddress.ip_address(raw)
        return True
    except ValueError:
        pass
    try:
        ipaddress.ip_network(raw, strict=False)
        return True
    except ValueError:
        return False


def validate_cti_duration(value: str, param: str = "since") -> str | None:
    """Return an error string when *value* is not a CTI duration (else None).

    CTI durations are ``<n>h`` (hours), ``<n>d`` (days) or ``<n>m`` (minutes),
    e.g. ``24h``, ``7d``, ``30m``.
    """
    if not _CTI_DURATION_RE.fullmatch(str(value or "").strip()):
        return (
            f"Error: '{param}' must be a duration like '24h' (hours), "
            f"'7d' (days) or '30m' (minutes) - got '{value}'."
        )
    return None


def validate_service_duration(value: str, param: str = "since") -> str | None:
    """Return an error string when *value* is not a Service API duration.

    The Service API tracker endpoints accept ``<n>h`` (hours) or ``<n>d``
    (days), e.g. ``24h``, ``7d``.
    """
    if not _SERVICE_DURATION_RE.fullmatch(str(value or "").strip()):
        return (
            f"Error: '{param}' must be a duration like '24h' (hours) or "
            f"'7d' (days) - got '{value}'."
        )
    return None


def reputation_icon(reputation: Any) -> str:
    """Return the indicator emoji for a CTI reputation value."""
    return _REPUTATION_ICONS.get(str(reputation or "").strip().lower(), "\u26aa")


def _named_list(items: Any, *, key: str = "label", fallback: str = "name", limit: int = 8) -> str:
    """Join a list of ``{name, label, description}`` objects into one line."""
    if not isinstance(items, list) or not items:
        return ""
    names: list[str] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        names.append(str(item.get(key) or item.get(fallback) or "?"))
    extra = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return ", ".join(names) + extra


def format_cti_scores(scores: dict[str, Any]) -> str:
    """Render the CTI ``scores`` block as one compact line (each 0-5)."""
    if not isinstance(scores, dict) or not scores:
        return ""
    parts: list[str] = []
    for window, label in (
        ("overall", "overall"),
        ("last_day", "day"),
        ("last_week", "week"),
        ("last_month", "month"),
    ):
        block = scores.get(window)
        if isinstance(block, dict) and block.get("total") is not None:
            parts.append(f"{label} {block['total']}")
    return " | ".join(parts)


def format_cti_ip(entry: dict[str, Any]) -> str:
    """Full multi-line summary of a CTI IP object (``/smoke/{ip}`` shape)."""
    ip = entry.get("ip", "?")
    reputation = entry.get("reputation") or "unknown"
    confidence = entry.get("confidence")
    head = f"IP {ip} - {reputation_icon(reputation)} {reputation}"
    if confidence:
        head += f" (confidence: {confidence})"
    lines = [head]

    location = entry.get("location") or {}
    geo = ", ".join(
        str(part)
        for part in (location.get("country"), location.get("city"))
        if part
    )
    as_name = entry.get("as_name")
    context = (
        f"  AS: {as_name or '?'}"
        + (f" (AS{entry['as_num']})" if entry.get("as_num") else "")
        + (f" | Location: {geo}" if geo else "")
        + (f" | rDNS: {entry['reverse_dns']}" if entry.get("reverse_dns") else "")
    )
    lines.append(context)

    if entry.get("ip_range") or entry.get("ip_range_24"):
        range_bits: list[str] = []
        if entry.get("ip_range"):
            range_bits.append(
                f"range {entry['ip_range']} (score {entry.get('ip_range_score', '?')}/5)"
            )
        if entry.get("ip_range_24"):
            range_bits.append(
                f"/24 {entry['ip_range_24']} - "
                f"{entry.get('ip_range_24_reputation') or 'unknown'} "
                f"(score {entry.get('ip_range_24_score', '?')}/5)"
            )
        lines.append("  Ranges: " + " | ".join(range_bits))

    if entry.get("background_noise"):
        lines.append(
            f"  Background noise: {entry['background_noise']}"
            + (
                f" ({entry['background_noise_score']}/10)"
                if entry.get("background_noise_score") is not None
                else ""
            )
        )

    history = entry.get("history") or {}
    if history.get("first_seen") or history.get("last_seen"):
        lines.append(
            f"  Seen: first {dt(history.get('first_seen')) or '?'} | "
            f"last {dt(history.get('last_seen')) or '?'}"
            + (
                f" | active {history.get('days_age')}/{history.get('full_age')} days"
                if history.get("full_age") is not None
                else ""
            )
        )

    scores_line = format_cti_scores(entry.get("scores") or {})
    if scores_line:
        lines.append(f"  Threat scores (total 0-5): {scores_line}")

    behaviors = _named_list(entry.get("behaviors"))
    if behaviors:
        lines.append(f"  Behaviors: {behaviors}")

    attack_details = _named_list(entry.get("attack_details"), limit=6)
    if attack_details:
        lines.append(f"  Attack scenarios: {attack_details}")

    mitre = entry.get("mitre_techniques")
    if isinstance(mitre, list) and mitre:
        techniques = ", ".join(
            f"{t.get('name', '?')} ({t.get('label', '?')})"
            for t in mitre[:6]
            if isinstance(t, dict)
        )
        extra = f" (+{len(mitre) - 6} more)" if len(mitre) > 6 else ""
        lines.append(f"  MITRE techniques: {techniques}{extra}")

    cves = entry.get("cves")
    if isinstance(cves, list) and cves:
        shown = ", ".join(str(c) for c in cves[:10])
        extra = f" (+{len(cves) - 10} more)" if len(cves) > 10 else ""
        lines.append(f"  CVEs: {shown}{extra}")

    classifications = entry.get("classifications") or {}
    classes = _named_list(classifications.get("classifications"))
    if classes:
        lines.append(f"  Classifications: {classes}")
    false_positives = _named_list(classifications.get("false_positives"))
    if false_positives:
        lines.append(
            f"  FALSE POSITIVE tags: {false_positives} - per CrowdSec, an IP "
            "with false_positives tags should NOT be treated as malicious."
        )

    targets = entry.get("target_countries")
    if isinstance(targets, dict) and targets:
        top = sorted(targets.items(), key=lambda kv: kv[1], reverse=True)[:5]
        lines.append(
            "  Top targeted countries: "
            + ", ".join(f"{country} ({share}%)" for country, share in top)
        )

    # Fire (community blocklist) entries carry two extra fields.
    if entry.get("state"):
        lines.append(
            f"  Community blocklist: state {entry['state']}"
            + (f" | expires {dt(entry.get('expiration'))}" if entry.get("expiration") else "")
        )

    return "\n".join(lines)


def format_cti_ip_brief(entry: dict[str, Any]) -> str:
    """One-line summary of a CTI IP object (for search / fire listings)."""
    reputation = entry.get("reputation") or "unknown"
    behaviors = _named_list(entry.get("behaviors"), limit=3)
    location = entry.get("location") or {}
    bits = [
        f"  {reputation_icon(reputation)} {entry.get('ip', '?')} [{reputation}"
        + (f", {entry['confidence']} confidence]" if entry.get("confidence") else "]"),
        f"AS: {entry.get('as_name') or '?'}",
    ]
    if location.get("country"):
        bits.append(str(location["country"]))
    if entry.get("background_noise"):
        bits.append(f"noise: {entry['background_noise']}")
    if behaviors:
        bits.append(f"behaviors: {behaviors}")
    history = entry.get("history") or {}
    if history.get("last_seen"):
        bits.append(f"last seen: {dt(history['last_seen'])}")
    if entry.get("state"):
        bits.append(f"state: {entry['state']}")
    return " | ".join(bits)


def preview_text(text: str, header: str, limit: int = 200) -> str:
    """Render a capped line-by-line preview of a raw text payload.

    Blocklist downloads and integration-endpoint content can be 100k+ lines;
    the cap keeps a single tool call from flooding the agent's context.
    ``header`` is the lead-in before the entry count, e.g.
    ``"Blocklist 5f9d... content -"`` renders as
    ``"Blocklist 5f9d... content - 250 entries:"``.
    """
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    shown = lines[:limit]
    noun = "entry" if len(lines) == 1 else "entries"
    out = f"{header} {len(lines)} {noun}:\n" + "\n".join(f"  {ln}" for ln in shown)
    if len(lines) > len(shown):
        out += f"\n[Showing the first {len(shown)} of {len(lines)} entries]"
    return out


def next_page_hint(data: dict[str, Any], page: int) -> str:
    """Pagination hint for the CTI HAL ``_links`` block (next page present)."""
    links = data.get("_links")
    if isinstance(links, dict) and isinstance(links.get("next"), dict):
        return f"\n[More results available - pass page={page + 1}]"
    return ""


def page_footer(data: dict[str, Any]) -> str:
    """Pagination footer for Service API fastapi-pagination pages.

    Renders ``{items, total, page, size, pages}`` responses as
    ``[Page 1/4 - 182 total - pass page=2 for more]`` (or ``""`` on the last
    page / when the envelope is absent).
    """
    if not isinstance(data, dict):
        return ""
    page = data.get("page")
    pages = data.get("pages")
    total = data.get("total")
    if not isinstance(page, int) or not isinstance(pages, int) or pages <= 1:
        return ""
    out = f"\n[Page {page}/{pages}"
    if isinstance(total, int):
        out += f" - {total} total"
    if page < pages:
        out += f" - pass page={page + 1} for more"
    return out + "]"
