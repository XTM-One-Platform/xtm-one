"""Shared helpers for the VirusTotal provider package.

VirusTotal API v3 exposes every surface under one host
(``https://www.virustotal.com/api/v3``), all authenticated with the same
``x-apikey`` header:

* IOC reports (``/files`` / ``/urls`` / ``/domains`` / ``/ip_addresses``)
  with relationships, comments, and votes
* Analyses (``/analyses``) - submissions polled until completed
* Search (``/search``) and Intelligence corpus search
  (``/intelligence/search``, premium)
* Livehunt (``/intelligence/hunting_rulesets`` +
  ``/intelligence/hunting_notifications``, premium)
* Retrohunt (``/intelligence/retrohunt_jobs``, premium)
* Collections (``/collections``)
* Popular threat categories (``/popular_threat_categories``)

The credential is a single API key stored in ``api_key_enc`` - the dispatcher
hands ``build_tools`` the bare key (no compound token). Public keys are rate
limited to 4 requests/minute and 500/day; the shared ``safe_request`` helper
retries 429s with decorrelated jitter.
"""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client

_PROVIDER = "VirusTotal"
_SOURCE = "integration:virustotal"

# Fixed public API host - VirusTotal has no self-hosted variant.
VT_API = "https://www.virustotal.com/api/v3"

# Tool ``entity_type`` argument -> API path segment.
ENTITY_PATHS: dict[str, str] = {
    "file": "files",
    "url": "urls",
    "domain": "domains",
    "ip": "ip_addresses",
}

# Entity types accepted by the generic relationship tool (collections also
# expose relationships such as files / urls / domains / ip_addresses).
RELATIONSHIP_ENTITY_PATHS: dict[str, str] = {
    **ENTITY_PATHS,
    "collection": "collections",
}

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# C0 control characters + DEL - covers CR/LF (log/output line injection) and
# escape sequences (terminal injection) in one chokepoint.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


def build_vt_headers(api_key: str) -> dict[str, str]:
    """Build VirusTotal request headers (``x-apikey`` + JSON negotiation).

    The key is stripped so trailing/leading whitespace or newlines (common
    when pasting credentials) can never produce a malformed header value
    (httpx/h11 reject header values containing newlines). Single chokepoint
    shared by the tool builder and the connectivity test.
    """
    return {
        "x-apikey": (api_key or "").strip(),
        "Accept": "application/json",
    }


def client(timeout: int = 30) -> httpx.AsyncClient:
    """Return a fresh async HTTP client for a single VirusTotal call."""
    return egress_client(timeout=timeout)


def clamp(limit: Any, default: int = 10, hi: int = 40, lo: int = 1) -> int:
    """Clamp a caller-supplied ``limit`` into ``[lo, hi]``; fall back on garbage.

    VirusTotal collection endpoints accept ``limit`` up to 40 in most cases.
    """
    try:
        return max(lo, min(int(limit), hi))
    except (TypeError, ValueError):
        return default


def trunc(text: Any, max_len: int = 300) -> str:
    """Neutralise control characters and truncate ``text`` to ``max_len`` chars.

    The single sanitisation chokepoint for every untrusted value echoed into
    ``safe_request`` action strings or tool result text (user arguments AND
    VirusTotal/community-supplied attributes) - collapses C0 control
    characters incl. CR/LF and DEL into spaces so they cannot inject log
    lines, terminal escapes, or fake output lines.
    """
    if text is None:
        return ""
    text = _CONTROL_CHARS_RE.sub(" ", str(text)).strip()
    return text[:max_len] + ("\u2026" if len(text) > max_len else "")


def as_int(value: Any, default: int = 0) -> int:
    """Coerce an untrusted numeric attribute to ``int``; fall back on garbage.

    VirusTotal stats/counters should be integers, but a hostile or corrupt
    payload can carry strings (or ``None``) - ``int("x")`` would crash the
    tool call and a raw echo would bypass ``trunc()``.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ts(value: Any) -> str:
    """Render a VirusTotal epoch timestamp as a UTC ``YYYY-MM-DD HH:MM:SS``.

    VirusTotal attributes carry Unix epoch integers (``last_analysis_date``,
    ``creation_date``, ...). Returns ``""`` for missing/garbage values.
    """
    try:
        epoch = int(value)
    except (TypeError, ValueError):
        return ""
    if epoch <= 0:
        return ""
    try:
        return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        # An absurd epoch from a hostile/corrupt API payload must degrade to
        # "missing", not crash the whole tool call.
        return ""


def csv_list(value: str) -> list[str]:
    """Split a comma-separated string into trimmed, non-empty items."""
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def url_identifier(url_or_id: str) -> str:
    """Return the VirusTotal URL identifier for a raw URL or a ready-made id.

    The ``/urls/{id}`` endpoints accept either the SHA-256 of the canonical
    URL or the unpadded base64url form of the URL itself. When the caller
    passes a raw URL (anything with ``://`` or that is not a SHA-256 /
    base64url token) we compute the base64url form; ready-made identifiers
    pass through untouched.
    """
    value = (url_or_id or "").strip()
    if not value:
        return ""
    if _SHA256_RE.match(value):
        return value
    if "://" not in value and re.fullmatch(r"[A-Za-z0-9_-]+", value):
        # Already looks like an unpadded base64url identifier.
        return value
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def data_of(payload: Any) -> Any:
    """Return the ``data`` member of a v3 response envelope (or the payload)."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def cursor_of(payload: Any) -> str | None:
    """Extract the next-page cursor from a v3 collection response, if any."""
    if isinstance(payload, dict):
        meta = payload.get("meta")
        if isinstance(meta, dict):
            cursor = meta.get("cursor")
            if isinstance(cursor, str) and cursor:
                return cursor
    return None


def attributes_of(obj: Any) -> dict[str, Any]:
    """Return the ``attributes`` dict of a v3 object (defensive)."""
    if isinstance(obj, dict):
        attrs = obj.get("attributes")
        if isinstance(attrs, dict):
            return attrs
    return {}


def verdict_emoji(malicious: int, suspicious: int) -> str:
    """Return an indicator emoji for detection counts."""
    if malicious > 0:
        return "\U0001f534"  # red circle
    if suspicious > 0:
        return "\U0001f7e1"  # yellow circle
    return "\u26aa"  # white circle


def format_analysis_stats(attrs: dict[str, Any], *, key: str = "last_analysis_stats") -> str:
    """Render a ``last_analysis_stats``-style block as a one-line summary.

    Works for object attributes (``last_analysis_stats``) and analysis
    objects (``stats``) alike via the ``key`` argument.
    """
    stats = attrs.get(key)
    if not isinstance(stats, dict) or not stats:
        return "Detections: no analysis stats available"
    malicious = as_int(stats.get("malicious"))
    suspicious = as_int(stats.get("suspicious"))
    harmless = as_int(stats.get("harmless"))
    undetected = as_int(stats.get("undetected"))
    timeout = as_int(stats.get("timeout"))
    total = malicious + suspicious + harmless + undetected + timeout
    return (
        f"Detections: {verdict_emoji(malicious, suspicious)} "
        f"{malicious} malicious, {suspicious} suspicious, {harmless} harmless, "
        f"{undetected} undetected (of {total} engines)"
    )


def format_threat_classification(attrs: dict[str, Any]) -> list[str]:
    """Render ``popular_threat_classification`` (files only), when present."""
    ptc = attrs.get("popular_threat_classification")
    if not isinstance(ptc, dict):
        return []
    lines: list[str] = []
    label = ptc.get("suggested_threat_label")
    if label:
        lines.append(f"Suggested threat label: {trunc(label, 120)}")
    categories = ptc.get("popular_threat_category")
    if isinstance(categories, list) and categories:
        names = ", ".join(str(c.get("value")) for c in categories if isinstance(c, dict) and c.get("value"))
        if names:
            lines.append(f"Threat categories: {trunc(names, 200)}")
    return lines


def format_common_attributes(attrs: dict[str, Any]) -> list[str]:
    """Render attributes shared by every IOC type (reputation, tags, dates)."""
    lines: list[str] = []
    reputation = attrs.get("reputation")
    if reputation is not None:
        lines.append(f"Community reputation: {as_int(reputation)}")
    votes = attrs.get("total_votes")
    if isinstance(votes, dict) and (votes.get("harmless") or votes.get("malicious")):
        lines.append(
            f"Community votes: {as_int(votes.get('harmless'))} harmless / "
            f"{as_int(votes.get('malicious'))} malicious"
        )
    tags = attrs.get("tags")
    if isinstance(tags, list) and tags:
        lines.append(f"Tags: {trunc(', '.join(str(t) for t in tags[:12]), 300)}")
    last_analysis = ts(attrs.get("last_analysis_date"))
    if last_analysis:
        lines.append(f"Last analysed: {last_analysis} UTC")
    return lines


def gui_link(entity_path: str, entity_id: str) -> str:
    """Return the public VirusTotal GUI link for an object.

    The public GUI URL scheme (``https://www.virustotal.com/gui/<type>/<id>``)
    is stable and documented; identifiers are the same as the API's. The id is
    percent-encoded so an untrusted value (user argument or API-supplied id)
    cannot extend the link with extra path segments, a query string, or
    whitespace that breaks the rendered link.
    """
    kind = {
        "files": "file",
        "urls": "url",
        "domains": "domain",
        "ip_addresses": "ip-address",
        "collections": "collection",
    }.get(entity_path, entity_path)
    return f"https://www.virustotal.com/gui/{kind}/{quote(str(entity_id or ''), safe='')}"
