"""Shared helpers for the Google Threat Intelligence (GTI) provider package.

Google Threat Intelligence (the Google / Mandiant / VirusTotal merged threat
intel product, docs at gtidocs.virustotal.com) exposes three surfaces, all
under one host and all authenticated with the same ``x-apikey`` header:

* Core (``https://www.virustotal.com/api/v3``) - GTI assessments on IoC
  reports, the Threat Landscape (``/collections`` with ``collection_type``),
  curated Threat Lists (``/threat_lists``), the IoC Stream (``/ioc_stream``),
  Threat Profiles (``/threat_profiles``), batch IoC summaries
  (``/iocs/summary``), and Code Insight (``/codeinsights``).
* DTM (``https://www.virustotal.com/api/v3/dtm``) - Digital Threat Monitoring
  alerts, monitors, and deep/dark-web document search.
* ASM (``https://www.virustotal.com/api/v3/asm``) - Attack Surface Management
  projects, entities, issues, and technologies.

The credential is a single API key stored in ``api_key_enc`` - the dispatcher
hands ``build_tools`` the bare key (no compound token; the hosts are fixed).
The ``x-tool`` header MUST be sent on core IoC lookups to receive the
``gti_assessment`` attribute (verdict / threat score / severity) - per
https://gtidocs.virustotal.com/docs/missing-gti-asssessment. Quotas are
license-based; the shared ``safe_request`` helper retries 429s with
decorrelated jitter. Feature availability is license-tiered (Enterprise /
Enterprise Plus unlock threat actors, campaigns, most threat lists, DTM and
ASM) - tools surface the API's permission errors verbatim so the agent can
explain what the key is missing.
"""

from __future__ import annotations

import base64
import ipaddress
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx

from app.core.egress import egress_client

_PROVIDER = "Google Threat Intelligence"
_SOURCE = "integration:gti"

# Fixed public API hosts - GTI has no self-hosted variant.
GTI_API = "https://www.virustotal.com/api/v3"
GTI_DTM_API = f"{GTI_API}/dtm"
GTI_ASM_API = f"{GTI_API}/asm"

# Tool ``ioc_type`` argument -> core API path segment.
IOC_PATHS: dict[str, str] = {
    "file": "files",
    "url": "urls",
    "domain": "domains",
    "ip_address": "ip_addresses",
}

# Threat Landscape object types accepted by the /collections filter.
COLLECTION_TYPES = (
    "threat-actor",
    "malware-family",
    "software-toolkit",
    "campaign",
    "collection",
    "report",
    "vulnerability",
    "country-profile",
    "industry-profile",
)

_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# C0 control characters + DEL - covers CR/LF (log/output line injection) and
# escape sequences (terminal injection) in one chokepoint.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


def build_gti_headers(api_key: str) -> dict[str, str]:
    """Build GTI request headers (``x-apikey`` + ``x-tool`` + JSON accept).

    The key is stripped so trailing/leading whitespace or newlines (common
    when pasting credentials) can never produce a malformed header value
    (httpx/h11 reject header values containing newlines). The ``x-tool``
    header identifies this client and is REQUIRED for the ``gti_assessment``
    attribute to appear in IoC responses. Single chokepoint shared by the
    tool builder and the connectivity test.
    """
    return {
        "x-apikey": (api_key or "").strip(),
        "x-tool": "xtm-one",
        "Accept": "application/json",
    }


def client(timeout: int = 30) -> httpx.AsyncClient:
    """Return a fresh async HTTP client for a single GTI call."""
    return egress_client(timeout=timeout)


def clamp(limit: Any, default: int = 10, hi: int = 40, lo: int = 1) -> int:
    """Clamp a caller-supplied ``limit`` into ``[lo, hi]``; fall back on garbage.

    Core GTI collection endpoints accept ``limit`` up to 40; DTM alerts up to
    25; ASM searches up to 100 (pass ``hi`` accordingly).
    """
    try:
        return max(lo, min(int(limit), hi))
    except (TypeError, ValueError):
        return default


def trunc(text: Any, max_len: int = 300) -> str:
    """Neutralise control characters and truncate ``text`` to ``max_len`` chars.

    The single sanitisation chokepoint for every untrusted value echoed into
    ``safe_request`` action strings or tool result text (user arguments AND
    GTI/community/dark-web-supplied attributes) - collapses C0 control
    characters incl. CR/LF and DEL into spaces so they cannot inject log
    lines, terminal escapes, or fake output lines.
    """
    if text is None:
        return ""
    text = _CONTROL_CHARS_RE.sub(" ", str(text)).strip()
    return text[:max_len] + ("\u2026" if len(text) > max_len else "")


def as_int(value: Any, default: int = 0) -> int:
    """Coerce an untrusted numeric attribute to ``int``; fall back on garbage."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ts(value: Any) -> str:
    """Render a GTI epoch timestamp as a UTC ``YYYY-MM-DD HH:MM:SS``.

    GTI attributes carry Unix epoch integers (``last_analysis_date``,
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
    """Split a comma/newline-separated string into trimmed, non-empty items."""
    return [
        item.strip()
        for item in re.split(r"[,\n]", value or "")
        if item.strip()
    ]


def url_identifier(url_or_id: str) -> str:
    """Return the GTI URL identifier for a raw URL or a ready-made id.

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


def classify_ioc(value: str) -> str:
    """Best-effort IoC type detection for batch summary descriptors.

    Hashes (MD5/SHA-1/SHA-256) -> ``file``; anything with a scheme or path
    separator -> ``url``; values that parse as an actual IPv4/IPv6 literal
    (via :mod:`ipaddress`, brackets tolerated) -> ``ip_address``; else
    ``domain``. Parsing real literals instead of matching "contains a colon
    or dotted quad" keeps host:port strings (``example.com:443``) and
    malformed quads (``999.1.1.1``) out of the ``ip_address`` bucket, where
    they would poison the all-or-nothing ``/iocs/summary`` batch.
    """
    value = (value or "").strip()
    if _MD5_RE.match(value) or _SHA1_RE.match(value) or _SHA256_RE.match(value):
        return "file"
    if "://" in value or "/" in value:
        return "url"
    try:
        ipaddress.ip_address(value.removeprefix("[").removesuffix("]"))
    except ValueError:
        return "domain"
    return "ip_address"


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


def context_of(obj: Any) -> dict[str, Any]:
    """Return the ``context_attributes`` dict of a v3 object (defensive)."""
    if isinstance(obj, dict):
        ctx = obj.get("context_attributes")
        if isinstance(ctx, dict):
            return ctx
    return {}


def next_page_token(resp: httpx.Response | None) -> str | None:
    """Extract the opaque ``page`` token from a DTM ``Link: <...>; rel="next"``.

    DTM (alerts / monitors / docs search) paginates via the response ``Link``
    header instead of a body cursor. Returns the ``page`` query-string value
    of the ``rel="next"`` URI, or ``None`` when there is no next page.
    """
    if resp is None:
        return None
    link = resp.headers.get("link") or ""
    for part in link.split(","):
        if 'rel="next"' not in part:
            continue
        start = part.find("<")
        end = part.find(">")
        if start == -1 or end == -1 or end <= start:
            continue
        candidate = part[start + 1 : end]
        page_values = parse_qs(urlparse(candidate).query).get("page")
        if page_values and page_values[0]:
            return page_values[0]
    return None


def gui_link(kind: str, entity_id: str) -> str:
    """Return the public Google TI GUI link for an object.

    The Google TI UI lives at ``https://www.virustotal.com/gui/<type>/<id>``
    with the same identifiers as the API (threat objects use the
    ``collection`` kind). The id is percent-encoded so an untrusted value
    (user argument or API-supplied id) cannot extend the link with extra path
    segments, a query string, or whitespace that breaks the rendered link.
    """
    return f"https://www.virustotal.com/gui/{kind}/{quote(str(entity_id or ''), safe='')}"


def verdict_label(value: Any) -> str:
    """Render a ``VERDICT_*`` / ``SEVERITY_*`` enum as a compact word."""
    text = trunc(value, 60)
    for prefix in ("VERDICT_", "SEVERITY_"):
        if text.startswith(prefix):
            return text[len(prefix) :].lower()
    return text


def format_gti_assessment(attrs: dict[str, Any]) -> list[str]:
    """Render the ``gti_assessment`` block (verdict / score / severity).

    Only present when the ``x-tool`` header was sent AND the key's license
    includes Google TI assessments - degrades to a hint line when missing so
    the agent understands why there is no verdict.
    """
    assessment = attrs.get("gti_assessment")
    if not isinstance(assessment, dict) or not assessment:
        return [
            "Google TI assessment: not available for this key/object "
            "(license-dependent)."
        ]
    lines: list[str] = []
    verdict = assessment.get("verdict")
    verdict_value = verdict.get("value") if isinstance(verdict, dict) else verdict
    score = assessment.get("threat_score")
    score_value = score.get("value") if isinstance(score, dict) else score
    severity = assessment.get("severity")
    severity_value = severity.get("value") if isinstance(severity, dict) else severity
    bits: list[str] = []
    if verdict_value:
        bits.append(f"verdict {verdict_label(verdict_value)}")
    if score_value is not None:
        bits.append(f"threat score {as_int(score_value)}/100")
    if severity_value:
        bits.append(f"severity {verdict_label(severity_value)}")
    if bits:
        lines.append("Google TI assessment: " + ", ".join(bits))
    description = assessment.get("description")
    if description:
        lines.append(f"Assessment summary: {trunc(description, 400)}")
    return lines


def format_analysis_stats(attrs: dict[str, Any], *, key: str = "last_analysis_stats") -> str:
    """Render a ``last_analysis_stats``-style block as a one-line summary."""
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
        f"Detections: {malicious} malicious, {suspicious} suspicious, "
        f"{harmless} harmless, {undetected} undetected (of {total} engines)"
    )


def format_threat_summary(obj: dict[str, Any]) -> str:
    """One-line summary of a Threat Landscape object (any collection_type)."""
    attrs = attributes_of(obj)
    kind = trunc(attrs.get("collection_type"), 40) or "threat"
    name = trunc(attrs.get("name"), 100) or "?"
    origin = trunc(attrs.get("origin"), 60)
    modified = ts(attrs.get("last_modification_date"))
    bits = [kind]
    if origin:
        bits.append(f"origin {origin}")
    if modified:
        bits.append(f"updated {modified}")
    return f"  {name} [{', '.join(bits)}] (id: {trunc(obj.get('id'), 100) or '?'})"
