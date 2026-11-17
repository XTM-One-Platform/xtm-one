# SPDX-License-Identifier: Apache-2.0

"""SOCRadar Extended Threat Intelligence integration - Agentic XTI/DRP management.

Full-fledged SOCRadar (Extended Threat Intelligence platform - CTI + Digital
Risk Protection + External Attack Surface Management) integration covering the
digital-risk loop for an agentic security team:

  - Incidents / alarms (*gathering incidents + triage + response*): list with
    rich filters (status, severity, alarm type, dates), get one alarm with its
    full content and related entities, change status across the 11 resolution
    reasons (resolve / investigate / mark false positive / ...), comment,
    assign analysts, and add/remove tags
  - Threat intelligence (*enrichment*): ThreatFusion indicator analysis for
    IPs, domains, URLs and hashes (risk score, whois, DNS, geo, findings) and
    threat feed collection consumption (the same feed the OpenCTI SOCRadar
    connector ingests)
  - Dark web monitoring (*hunting*): one consolidated search over the five
    v2 sources - botnet data (stealer logs), black market listings,
    suspicious content, PII exposure, and IM (instant messaging) content
  - Brand protection (*digital risk*): one consolidated read over the five
    v2 categories - impersonating domains, impersonating accounts, rogue
    mobile applications, bad reputation findings, and social media findings
  - Fraud protection (*digital risk*): compromised payment card monitoring
  - Attack surface management (*digital footprint*): digital asset inventory
    by type, asset registration, false-positive marking and monitoring
    toggles
  - Takedowns (*response*): phishing-domain, social-media and
    source-code-leak takedown requests + progress tracking
  - Audit logs (*governance*): the platform user activity trail

Authentication
--------------
Every request carries the company API key in the ``API-Key`` header (no
OAuth). The key is generated in SOCRadar under Settings > API & Integrations >
API Options, where the numeric Company ID is displayed next to it. The
ThreatFusion threat-analysis endpoint additionally takes the key as the
``key`` query parameter (the vendor's own clients pass it that way).

``integration_tools._get_token`` assembles the compound
``base_url||company_id||api_key`` token this builder parses. The base URL
defaults to the SOCRadar SaaS host (``https://platform.socradar.com/api``).
The API key (the credential) lives in ``api_key_enc``; the company ID and the
base URL live in ``extra_config``.

Pagination is page-based (``page`` + ``limit``, max 100 per page on the
incidents API); responses use the ``{"is_success": bool, "message": str,
"data": ...}`` envelope.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.credential_exposure import (
    is_secret_key,
    render_exposed_value,
    scrub_exposure_text,
)
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "SOCRadar"
_SOURCE = "integration:socradar"

# Default SOCRadar SaaS API host (includes the /api prefix). The stored
# base_url only exists for testing/proxy setups and defaults to this value.
_DEFAULT_BASE_URL = "https://platform.socradar.com/api"

# The incidents v4 API caps a single page at 100 records.
_MAX_LIMIT = 100

# Alarm status reasons accepted by POST /alarms/status/change, mapped to the
# numeric codes the API expects (the vendor's own XSOAR v4 client map).
_STATUS_REASONS: dict[str, int] = {
    "OPEN": 0,
    "INVESTIGATING": 1,
    "RESOLVED": 2,
    "PENDING_INFO": 4,
    "LEGAL_REVIEW": 5,
    "VENDOR_ASSESSMENT": 6,
    "FALSE_POSITIVE": 9,
    "DUPLICATE": 10,
    "PROCESSED_INTERNALLY": 11,
    "MITIGATED": 12,
    "NOT_APPLICABLE": 13,
}

_INCIDENT_STATUSES: frozenset[str] = frozenset({"OPEN", "CLOSED", "ON_HOLD"})
_SEVERITIES: frozenset[str] = frozenset(
    {"INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
)

# Dark web monitoring v2 sources, mapped to their endpoint path segment
# (the vendor's own n8n node endpoint map).
_DARK_WEB_SOURCES: dict[str, str] = {
    "botnet_data": "botnet-data",
    "blackmarket": "blackmarket",
    "suspicious_content": "suspicious-content",
    "pii_exposure": "pii-exposure",
    "im_content": "im-content",
}

# Brand protection v2 categories, mapped to their endpoint path segment.
_BRAND_CATEGORIES: dict[str, str] = {
    "impersonating_domains": "impersonating-domains",
    "impersonating_accounts": "impersonating-accounts",
    "rogue_mobile_applications": "rogue-mobile-applications",
    "bad_reputation": "bad-reputation",
    "social_media_findings": "social-media-findings",
}

# Exposure findings are rendered from an allow-list (#5686): the fields below
# are shown by value (through the shared value-shape scrub), a field naming a
# secret (password, cookie, token, card number, CVV, ...) is shown as
# captured / not captured or BIN + last four by
# ``credential_exposure.render_exposed_value``, and every other field is named,
# never shown by value. The exact v2 field names per source are not public, so
# a field missing here fails closed: it is listed by name only.
_FRAUD_PROTECTION = "fraud_protection"
_FINDING_FIELDS: dict[str, tuple[str, ...]] = {
    "botnet_data": (
        "username", "user", "login", "email", "domain", "url",
        "malwareFamily", "malware", "stealerFamily", "country",
    ),
    "blackmarket": ("title", "domain", "marketplace", "seller", "price", "category"),
    "suspicious_content": ("title", "domain", "url", "category"),
    "pii_exposure": ("email", "domain", "title", "category"),
    "im_content": ("platform", "channel", "channelName", "author", "domain"),
    "impersonating_domains": ("domain", "riskScore", "registrar", "ip", "creationDate"),
    "impersonating_accounts": ("platform", "accountName", "username", "url"),
    "rogue_mobile_applications": ("appName", "store", "developer", "url"),
    "bad_reputation": ("domain", "ip", "blacklist", "reputation"),
    "social_media_findings": ("platform", "accountName", "url", "title"),
    _FRAUD_PROTECTION: (
        "bin", "issuer", "bank", "cardBrand", "brand", "cardType", "country",
        "expiry", "expirationDate", "expired",
    ),
}

# Finding keys the header renders, or structures never rendered by value.
_FINDING_HEADER_KEYS: frozenset[str] = frozenset(
    {
        "alarmId",
        "notificationId",
        "status",
        "statusReason",
        "source",
        "discoveryDate",
        "contentPreview",
        "relatedAlarm",
        "history",
        "screenshots",
    }
)

# Alarm ``content`` fields shown by value, per alarm main or sub type
# (lower-cased). A type missing here shows its content field names only. The
# "Stolen Credentials" content carries ``credential_details`` (a secret,
# shown as presence) and ``log_content_link`` (a link to the stealer log,
# named only), per the vendor's own XSOAR integration.
_ALARM_CONTENT_FIELDS: dict[str, tuple[str, ...]] = {
    "brand protection": ("dns_information", "domain", "registrar", "ip"),
    "stolen credentials": ("asset_match",),
}

# An alarm's related entities and assets are ``{"key": <type>, "value": ...}``
# pairs. Only the keys below, which name an identifier type, are shown by value
# (through the shared scrub); a secret-named key reads as presence (a card as
# BIN + last four); any other key ("Leaked Data", ...) is named, never shown by
# value. The vendor does not publish its key list, so an unknown key fails
# closed. Keys are compared lower-cased with non-alphanumerics dropped.
_RELATED_VALUE_KEYS: frozenset[str] = frozenset(
    {
        "domain",
        "subdomain",
        "hostname",
        "host",
        "ip",
        "ipaddress",
        "ipv4",
        "ipv6",
        "cidr",
        "asn",
        "url",
        "email",
        "emailaddress",
        "cve",
        "hash",
        "md5",
        "sha1",
        "sha256",
    }
)

# How many field names an exposure record lists when their values stay out.
_MAX_FIELD_NAMES = 10

# ASM digital asset types accepted by /asm (assetType) and /asm/add/{type}
# (the vendor's own n8n node asset-type list).
_ASSET_TYPES: frozenset[str] = frozenset(
    {
        "domain",
        "ipAddress",
        "website",
        "loginPage",
        "dnsRecord",
        "sslCertificate",
        "ipBlock",
        "mobileApplication",
        "technology",
        "cloudBuckets",
        "socialAccounts",
        "sourceCodeRepo",
    }
)

# manage_socradar_digital_asset actions.
_ASSET_ACTIONS: frozenset[str] = frozenset(
    {"mark_false_positive", "enable_monitoring", "disable_monitoring"}
)

# create_socradar_takedown kinds, mapped to their endpoint suffix (empty =
# the base domain-takedown route).
_TAKEDOWN_KINDS: dict[str, str] = {
    "domain": "",
    "social_media": "/social_media_risks",
    "source_code": "/source_code_leaks",
}


def _clamp(value: Any, default: int, lo: int = 1, hi: int = _MAX_LIMIT) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def _int_or(value: Any, default: int) -> int:
    """Coerce an LLM-provided value to int, tolerating explicit JSON null."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _seg(value: Any) -> str:
    """Percent-encode an LLM-provided value used as a URL path segment.

    Company IDs, alarm IDs and feed collection IDs are interpolated into
    endpoint paths; a value containing ``/``, ``?``, or ``#`` would otherwise
    rewrite the request path.
    """
    return quote(str(value or "").strip(), safe="")


def _csv(value: str) -> list[str]:
    """Split a CSV string into a clean list (empty -> [])."""
    return [part.strip() for part in (value or "").split(",") if part.strip()]


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def socradar_api_base(base_url: str) -> str:
    """Return the SOCRadar API base URL (no trailing slash, /api suffix kept).

    The value is stripped first: a base URL pasted with a trailing
    newline/space would otherwise yield an invalid host. The canonical base
    INCLUDES the ``/api`` prefix (``https://platform.socradar.com/api``); a
    pasted platform host without it (``https://platform.socradar.com``) gets
    the suffix appended so endpoints never miss the prefix. Falls back to the
    SaaS host when empty.
    """
    cleaned = (base_url or "").strip().rstrip("/")
    if not cleaned:
        return _DEFAULT_BASE_URL
    if not cleaned.lower().endswith("/api"):
        cleaned += "/api"
    return cleaned


def build_socradar_headers(api_key: str) -> dict[str, str]:
    """Build SOCRadar request headers (``API-Key`` + JSON accept).

    The key is stripped: a credential pasted with a trailing newline/space
    would otherwise produce a header value httpx rejects as illegal.
    """
    return {
        "API-Key": (api_key or "").strip(),
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Response parsing helpers
# ---------------------------------------------------------------------------


def _json_body(resp: httpx.Response) -> Any:
    """Parse a JSON body, returning ``None`` on empty/non-JSON responses."""
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError):
        return None


def _unwrap(data: Any, action: str) -> tuple[Any, str | None]:
    """Unwrap the SOCRadar ``{"is_success", "message", "data"}`` envelope.

    Returns ``(payload, None)`` on success and ``(None, error_string)`` when
    the API reports an in-body failure (some SOCRadar failures ride on
    HTTP 200 with ``is_success: false``).
    """
    if isinstance(data, dict) and "is_success" in data:
        if not data.get("is_success", True):
            message = str(data.get("message") or "unknown error")[:300]
            return None, f"Error: SOCRadar could not {action}: {message}"
        return data.get("data"), None
    return data, None


def _alarms_payload(payload: Any) -> tuple[list[dict], int | None]:
    """Normalise the incidents v4 payload to ``(alarms, total_records)``.

    With ``include_total_records=true`` the payload is a dict carrying
    ``alarms`` / ``total_pages`` / ``total_records``; without it the payload
    is a bare list of alarms.
    """
    if isinstance(payload, dict):
        alarms = payload.get("alarms")
        alarms = [a for a in alarms if isinstance(a, dict)] if isinstance(alarms, list) else []
        total = payload.get("total_records")
        return alarms, total if isinstance(total, int) else None
    if isinstance(payload, list):
        return [a for a in payload if isinstance(a, dict)], None
    return [], None


def _trunc(text: Any, max_len: int = 200) -> str:
    if text is None:
        return ""
    cleaned = str(text).replace("\r", " ").replace("\n", " ").strip()
    return cleaned[:max_len] + ("..." if len(cleaned) > max_len else "")


def _ts(value: Any) -> str:
    """Compact a SOCRadar timestamp to minute precision for list output."""
    text = str(value or "")
    return text[:16].replace("T", " ") if text else ""


def _page_hint(count: int, limit: int, page: int, total: int | None) -> str:
    """Page-based pagination hint (SOCRadar paginates with page + limit)."""
    if total is not None and page * limit >= total:
        return ""
    if count < limit:
        return ""
    return f"\nMore results may be available - request page={page + 1} for the next page."


def _format_alarm(alarm: dict) -> str:
    """One incident/alarm as a compact multi-line summary."""
    details = alarm.get("alarm_type_details")
    details = details if isinstance(details, dict) else {}
    main_type = details.get("alarm_main_type", "")
    sub_type = details.get("alarm_sub_type", "")
    type_label = " / ".join(t for t in (main_type, sub_type) if t)
    header = (
        f"#{alarm.get('alarm_id', '?')} "
        f"[{alarm.get('alarm_risk_level', '?')}] "
        f"{alarm.get('status', '?')}"
    )
    if type_label:
        header += f" - {type_label}"
    lines = [header]
    ctx_parts = []
    asset = alarm.get("alarm_asset")
    if asset:
        ctx_parts.append(f"Asset: {_trunc(scrub_exposure_text(asset), 160)}")
    date = _ts(alarm.get("date"))
    if date:
        ctx_parts.append(f"Date: {date}")
    if ctx_parts:
        lines.append("  " + " | ".join(ctx_parts))
    tags = alarm.get("tags")
    if isinstance(tags, list) and tags:
        lines.append(f"  Tags: {', '.join(str(t) for t in tags[:10])}")
    text = alarm.get("alarm_text")
    if text:
        lines.append(f"  {_trunc(scrub_exposure_text(text), 200)}")
    return "\n".join(lines)


def _field_names(keys: list[Any]) -> str:
    """Field names listed when their values stay out of the result."""
    names = ", ".join(_trunc(k, 40) for k in keys[:_MAX_FIELD_NAMES])
    if len(keys) > _MAX_FIELD_NAMES:
        names += f", ... ({len(keys) - _MAX_FIELD_NAMES} more)"
    return names


def _exposed_line(key: Any, value: Any, max_len: int) -> str:
    """``key: value`` for an exposure record, the secret never by value."""
    if isinstance(value, list) and not is_secret_key(key):
        value = [v for v in value[:5] if isinstance(v, (str, int, float))]
    return f"{_trunc(key, 40)}: {_trunc(render_exposed_value(key, value), max_len)}"


def _alarm_content_fields(alarm: dict) -> tuple[str, ...]:
    """The ``content`` fields an alarm's type declares shown by value."""
    details = alarm.get("alarm_type_details")
    details = details if isinstance(details, dict) else {}
    for label in (details.get("alarm_sub_type"), details.get("alarm_main_type")):
        fields = _ALARM_CONTENT_FIELDS.get(str(label or "").strip().lower())
        if fields is not None:
            return fields
    return ()


def _related_lines(label: str, items: Any) -> list[str]:
    """An alarm's related entities or assets, through the allow-list.

    A declared identifier key and a secret-named key (as presence) get a
    ``key: value`` line; every other key is named on one line, its value
    never shown.
    """
    if not isinstance(items, list) or not items:
        return []
    lines = [f"  Related {label}:"]
    unlisted: list[str] = []
    for item in items[:10]:
        if not isinstance(item, dict) or not item.get("key"):
            continue
        key, value = item.get("key"), item.get("value")
        declared = re.sub(r"[^a-z0-9]", "", str(key).lower()) in _RELATED_VALUE_KEYS
        if is_secret_key(key) or (declared and isinstance(value, (str, int, float, list))):
            lines.append(f"    {_exposed_line(key, value, 120)}")
        elif str(key) not in unlisted:
            unlisted.append(str(key))
    if unlisted:
        lines.append(f"    Other {label} (values not shown): {_field_names(unlisted)}")
    if len(items) > 10:
        lines.append(f"    ... and {len(items) - 10} more {label}")
    return lines


def _format_alarm_detail(alarm: dict) -> str:
    """One alarm with the full triage context (detail view)."""
    lines = [_format_alarm(alarm)]
    lines += _related_lines("entities", alarm.get("alarm_related_entities"))
    lines += _related_lines("assets", alarm.get("alarm_related_assets"))
    content = alarm.get("content")
    if isinstance(content, dict) and content:
        keys = list(content.keys())
        lines.append(f"  Content fields: {_field_names(keys)}")
        # Values only for the fields the alarm's type declares; a secret-named
        # field reads as presence. Every other field is named above, never
        # shown by value (fail closed on an unknown type or field).
        shown = _alarm_content_fields(alarm)
        rendered = [
            key
            for key in keys
            if is_secret_key(key)
            or (key in shown and isinstance(content.get(key), (str, int, float, bool, list)))
        ]
        for key in rendered[:_MAX_FIELD_NAMES]:
            lines.append(f"    {_exposed_line(key, content.get(key), 160)}")
    details = alarm.get("alarm_type_details")
    if isinstance(details, dict):
        mitigation = details.get("alarm_default_mitigation_plan")
        if mitigation:
            lines.append(f"  Mitigation: {_trunc(mitigation, 300)}")
    return "\n".join(lines)


def _format_feed_item(item: dict) -> str:
    """One threat feed IOC as a compact line pair."""
    extra = item.get("extra_info")
    extra = extra if isinstance(extra, dict) else {}
    line = f"{item.get('feed', '?')} ({item.get('feed_type', '?')})"
    parts = []
    if item.get("maintainer_name"):
        parts.append(f"Source: {item['maintainer_name']}")
    if extra.get("score") is not None:
        parts.append(f"Score: {extra['score']}")
    if extra.get("seen_count") is not None:
        parts.append(f"Seen: {extra['seen_count']}x")
    first = _ts(item.get("first_seen_date"))
    last = _ts(item.get("latest_seen_date"))
    if first or last:
        parts.append(f"First {first or '?'} | Last {last or '?'}")
    if parts:
        line += "\n  " + " | ".join(parts)
    return line


def _findings_payload(payload: Any) -> tuple[list[dict], int | None]:
    """Normalise a dark web / brand / fraud v2 payload to ``(items, total)``.

    The v2 endpoints nest the rows one level deeper than the incidents API:
    ``{"data": {"data": [...], "total_data_count": N}, "is_success": ...}``
    (the outer envelope is already unwrapped by ``_unwrap``). Some responses
    return the rows as a bare list instead.
    """
    if isinstance(payload, dict):
        items = payload.get("data")
        items = (
            [i for i in items if isinstance(i, dict)]
            if isinstance(items, list)
            else []
        )
        total = payload.get("total_data_count")
        return items, total if isinstance(total, int) else None
    if isinstance(payload, list):
        return [i for i in payload if isinstance(i, dict)], None
    return [], None


def _format_finding(finding: dict, kind: str = "") -> str:
    """One dark web / brand / fraud finding as a compact multi-line summary.

    The v2 finding shape varies per source (stealer logs, card dumps,
    impersonating domains, ...) and the sources whose point is a leaked
    secret carry it in the row. So the common keys render in the header, the
    fields ``_FINDING_FIELDS`` declares for *kind* render by value through
    the value-shape scrub, a secret-named field renders as presence (a card
    number as BIN + last four), and every other field is named only (#5686).
    """
    header_id = finding.get("alarmId") or finding.get("notificationId") or "?"
    header = f"#{_trunc(header_id, 40)}"
    status = finding.get("status")
    if status:
        header += f" [{_trunc(status, 40)}]"
    source = finding.get("source")
    if source:
        header += f" - {_trunc(scrub_exposure_text(source), 80)}"
    date = _ts(finding.get("discoveryDate"))
    if date:
        header += f" ({date})"
    lines = [header]
    preview = finding.get("contentPreview")
    if preview:
        lines.append(f"  {_trunc(scrub_exposure_text(preview), 200)}")
    related = finding.get("relatedAlarm")
    if isinstance(related, dict) and related.get("status"):
        lines.append(f"  Related alarm: {_trunc(related.get('status'), 40)}")
    declared = _FINDING_FIELDS.get(kind, ())
    fields: list[str] = []
    secrets: list[str] = []
    unlisted: list[str] = []
    for key, value in finding.items():
        if key in _FINDING_HEADER_KEYS:
            continue
        if is_secret_key(key):
            secrets.append(_exposed_line(key, value, 60))
        elif key in declared and isinstance(value, (str, int, float, bool, list)):
            if value not in ("", None, []):
                fields.append(_exposed_line(key, value, 80))
        else:
            unlisted.append(key)
    if fields:
        lines.append("  " + " | ".join(fields[:_MAX_FIELD_NAMES]))
    if secrets:
        lines.append("  " + " | ".join(secrets[:_MAX_FIELD_NAMES]))
    if unlisted:
        lines.append(f"  Other fields (values not shown): {_field_names(unlisted)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Integration class
# ---------------------------------------------------------------------------


class SOCRadarIntegration(BaseIntegration):
    """SOCRadar Extended Threat Intelligence integration (API-Key header)."""

    provider_name = "socradar"
    display_name = "SOCRadar"
    icon = "Radar"
    description = (
        "Connect SOCRadar Extended Threat Intelligence for incident/alarm "
        "triage, ThreatFusion IOC enrichment, threat feed consumption, dark "
        "web monitoring, brand and fraud protection, attack surface "
        "management, takedown requests, and platform audit logs."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        """SOCRadar uses a static company API key, not OAuth."""
        return None

    async def validate_connection(self, token: str) -> bool:
        """Validate the key + company ID with a 1-record incidents read."""
        base_url, company_id, api_key = (
            p.strip()
            for p in parse_compound_token(token, 3, greedy_last=True)
        )
        if not (company_id and api_key):
            return False
        api = socradar_api_base(base_url)
        async with egress_client(timeout=20) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/company/{_seg(company_id)}/incidents/v4",
                _PROVIDER,
                "validate connection",
                headers=build_socradar_headers(api_key),
                params={"limit": 1, "page": 1, "include_total_records": "false"},
            )
            if err or resp is None:
                return False
            data = _json_body(resp)
            return not (isinstance(data, dict) and data.get("is_success") is False)

    async def get_user_info(self, token: str) -> dict:
        """Return minimal account context (identity is bound to the API key)."""
        base_url, company_id, _ = (
            p.strip()
            for p in parse_compound_token(token, 3, greedy_last=True)
        )
        return {
            "name": "SOCRadar",
            "company_id": company_id,
            "base_url": socradar_api_base(base_url),
        }


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build SOCRadar tools from a ``base_url||company_id||api_key`` token."""
    base_url, company_id, api_key = (
        part.strip()
        for part in parse_compound_token(token, 3, greedy_last=True)
    )
    # ``base_url`` and ``company_id`` are populated by the 2-part introspection
    # dummy (``https://dummy.example.com||__dummy__``); the API key is supplied
    # only at runtime, so guarding on these two keeps catalog introspection
    # working while still refusing a half-configured integration at runtime.
    if not (base_url and company_id):
        logger.debug(
            "SOCRadar base URL or company ID is empty - returning no tools. "
            "Edit the integration and set the Company ID."
        )
        return []

    api = socradar_api_base(base_url)
    company = f"{api}/company/{_seg(company_id)}"
    headers = build_socradar_headers(api_key)

    def _client(timeout: int = 30) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_socradar_status() -> str:
        """Validate the SOCRadar connection and report the company scope."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{company}/incidents/v4",
                _PROVIDER,
                "validate connection",
                headers=headers,
                params={"limit": 1, "page": 1, "include_total_records": "true"},
            )
            if err:
                return err
            payload, env_err = _unwrap(_json_body(resp), "validate the connection")
            if env_err:
                return env_err
            _alarms, total = _alarms_payload(payload)
            total_label = total if total is not None else "?"
            return (
                f"Connected to SOCRadar at {api} (company ID {company_id}).\n"
                f"  Incidents visible to this company: {total_label}."
            )

    # ══════════════════════════════════════════════════════════════════════
    # 1. INCIDENTS (ALARMS) - triage + response
    # ══════════════════════════════════════════════════════════════════════

    async def list_socradar_incidents(
        status: str = "",
        severities: str = "",
        alarm_main_types: str = "",
        alarm_sub_types: str = "",
        alarm_type_ids: str = "",
        excluded_alarm_type_ids: str = "",
        start_date: str = "",
        end_date: str = "",
        limit: int = 20,
        page: int = 1,
    ) -> str:
        """List SOCRadar incidents (alarms) with structured filters."""
        params: dict[str, Any] = {
            "limit": _clamp(limit, 20),
            "page": max(1, _int_or(page, 1)),
            "include_total_records": "true",
        }
        statuses = [s.upper() for s in _csv(status)]
        invalid = [s for s in statuses if s not in _INCIDENT_STATUSES]
        if invalid:
            return (
                "Error: status values must be among "
                f"{', '.join(sorted(_INCIDENT_STATUSES))} (got '{', '.join(invalid)}')."
            )
        if statuses:
            params["status"] = statuses
        sev = [s.upper() for s in _csv(severities)]
        invalid = [s for s in sev if s not in _SEVERITIES]
        if invalid:
            return (
                "Error: severities values must be among "
                f"{', '.join(sorted(_SEVERITIES))} (got '{', '.join(invalid)}')."
            )
        if sev:
            params["severities"] = sev
        if alarm_main_types:
            params["alarm_main_types"] = _csv(alarm_main_types)
        if alarm_sub_types:
            params["alarm_sub_types"] = _csv(alarm_sub_types)
        if alarm_type_ids:
            params["alarm_type_ids"] = _csv(alarm_type_ids)
        if excluded_alarm_type_ids:
            params["excluded_alarm_type_ids"] = _csv(excluded_alarm_type_ids)
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        async with _client(60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{company}/incidents/v4",
                _PROVIDER,
                "list incidents",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload, env_err = _unwrap(_json_body(resp), "list incidents")
            if env_err:
                return env_err
            alarms, total = _alarms_payload(payload)
            result = format_entity_list(
                alarms,
                _format_alarm,
                noun="incident",
                total=total,
                empty_msg="No incidents matched the filters.",
            )
            return result + _page_hint(
                len(alarms), params["limit"], params["page"], total
            )

    async def get_socradar_incident(alarm_id: str) -> str:
        """Get one SOCRadar incident with full content and related entities."""
        cleaned = (alarm_id or "").strip()
        if not cleaned:
            return "Error: alarm_id is required."
        async with _client(60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{company}/incidents/v4",
                _PROVIDER,
                f"get incident {cleaned}",
                headers=headers,
                params={"notification_ids": [cleaned]},
            )
            if err:
                return err
            payload, env_err = _unwrap(_json_body(resp), f"get incident {cleaned}")
            if env_err:
                return env_err
            alarms, _total = _alarms_payload(payload)
            if not alarms:
                return (
                    f"No incident found for alarm ID {cleaned}. Verify the ID "
                    "with list_socradar_incidents."
                )
            return "\n\n".join(_format_alarm_detail(a) for a in alarms[:3])

    async def change_socradar_incident_status(
        alarm_ids: str,
        status_reason: str,
        comments: str = "",
    ) -> str:
        """Change the status of one or more alarms (triage/resolution)."""
        ids = _csv(alarm_ids)
        if not ids:
            return "Error: alarm_ids is required (CSV of numeric alarm IDs)."
        reason = (status_reason or "").strip().upper()
        if reason not in _STATUS_REASONS:
            return (
                "Error: status_reason must be one of "
                f"{', '.join(sorted(_STATUS_REASONS))} (got '{status_reason}')."
            )
        body: dict[str, Any] = {
            "alarm_ids": ids,
            "status": _STATUS_REASONS[reason],
            "comments": comments or "",
        }
        async with _client(60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{company}/alarms/status/change",
                _PROVIDER,
                f"change status of {len(ids)} alarm(s)",
                headers=headers,
                json=body,
            )
            if err:
                return err
            _payload, env_err = _unwrap(_json_body(resp), "change the alarm status")
            if env_err:
                return env_err
            return (
                f"Status changed to {reason} for {len(ids)} alarm(s): "
                f"{', '.join(ids)}."
            )

    async def add_socradar_incident_comment(
        alarm_id: str,
        comment: str,
        user_email: str,
    ) -> str:
        """Add an investigation comment to an alarm."""
        cleaned = (alarm_id or "").strip()
        if not (cleaned and comment and user_email):
            return "Error: alarm_id, comment and user_email are required."
        alarm_id_int = _int_or(cleaned, -1)
        if alarm_id_int < 0:
            return f"Error: alarm_id must be numeric (got '{alarm_id}')."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{company}/alarm/add/comment/v2",
                _PROVIDER,
                f"comment on alarm {cleaned}",
                headers=headers,
                json={
                    "alarm_id": alarm_id_int,
                    "user_email": user_email.strip(),
                    "comment": comment,
                },
            )
            if err:
                return err
            _payload, env_err = _unwrap(_json_body(resp), "add the comment")
            if env_err:
                return env_err
            return f"Comment added to alarm {cleaned}."

    async def assign_socradar_incident(alarm_id: str, user_emails: str) -> str:
        """Assign an alarm to one or more analysts by email."""
        cleaned = (alarm_id or "").strip()
        emails = _csv(user_emails)
        if not (cleaned and emails):
            return "Error: alarm_id and user_emails are required."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{company}/alarm/{_seg(cleaned)}/assignee",
                _PROVIDER,
                f"assign alarm {cleaned}",
                headers=headers,
                json={"user_emails": emails},
            )
            if err:
                return err
            _payload, env_err = _unwrap(_json_body(resp), "assign the alarm")
            if env_err:
                return env_err
            return f"Alarm {cleaned} assigned to {', '.join(emails)}."

    async def manage_socradar_incident_tags(
        action: str,
        alarm_id: str,
        tag: str,
    ) -> str:
        """Add or remove a tag on an alarm."""
        act = (action or "").strip().lower()
        if act not in ("add", "remove"):
            return "Error: action must be one of add, remove."
        cleaned = (alarm_id or "").strip()
        if not (cleaned and tag):
            return "Error: alarm_id and tag are required."
        alarm_id_int = _int_or(cleaned, -1)
        if alarm_id_int < 0:
            return f"Error: alarm_id must be numeric (got '{alarm_id}')."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post" if act == "add" else "delete",
                f"{company}/alarm/tag",
                _PROVIDER,
                f"{act} tag on alarm {cleaned}",
                headers=headers,
                json={"alarm_id": alarm_id_int, "tag": tag.strip()},
            )
            if err:
                return err
            _payload, env_err = _unwrap(_json_body(resp), f"{act} the tag")
            if env_err:
                return env_err
            verb = "added to" if act == "add" else "removed from"
            return f"Tag '{tag.strip()}' {verb} alarm {cleaned}."

    # ══════════════════════════════════════════════════════════════════════
    # 2. THREAT INTELLIGENCE - enrichment
    # ══════════════════════════════════════════════════════════════════════

    async def analyze_socradar_indicator(entity: str) -> str:
        """Enrich an IOC (IP, domain, URL, hash) via ThreatFusion analysis."""
        cleaned = (entity or "").strip()
        if not cleaned:
            return "Error: entity is required (an IP, domain, URL or file hash)."
        async with _client(60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/threat/analysis",
                _PROVIDER,
                f"analyze indicator {cleaned}",
                headers=headers,
                # The vendor's ThreatFusion clients pass the key as the ``key``
                # query parameter on this endpoint (in addition to the header).
                params={"key": api_key, "entity": cleaned},
            )
            if err:
                return err
            payload, env_err = _unwrap(
                _json_body(resp), f"analyze indicator {cleaned}"
            )
            if env_err:
                return env_err
            data = payload if isinstance(payload, dict) else {}
            lines = [
                f"ThreatFusion analysis for {cleaned}"
                + (
                    f" (classification: {data['classification']})"
                    if data.get("classification")
                    else ""
                )
                + ":"
            ]
            score = data.get("score")
            whitelisted = "yes" if data.get("is_whitelisted") else "no"
            lines.append(
                f"  Risk score: {score if score is not None else '?'}/1000 "
                f"| Whitelisted: {whitelisted}"
            )
            score_details = data.get("score_details")
            if isinstance(score_details, dict) and score_details:
                detail = ", ".join(
                    f"{k}: {v}" for k, v in list(score_details.items())[:6]
                )
                lines.append(f"  Score details: {detail}")
            findings = data.get("findings")
            if isinstance(findings, list):
                lines.append(f"  Findings (feed encounters): {len(findings)}")
            whois = data.get("whois")
            if isinstance(whois, dict) and whois:
                parts = []
                for key in (
                    "registrar",
                    "creation_date",
                    "expiration_date",
                    "org",
                    "country",
                    "asn",
                    "asn_description",
                ):
                    value = whois.get(key)
                    if isinstance(value, list) and value:
                        value = value[0]
                    if value:
                        parts.append(f"{key}: {_trunc(value, 60)}")
                if parts:
                    lines.append(f"  Whois: {' | '.join(parts[:5])}")
            dns_info = data.get("dns_info")
            if isinstance(dns_info, dict) and dns_info:
                parts = []
                for record_type in ("A", "AAAA", "NS", "MX"):
                    records = dns_info.get(record_type)
                    if isinstance(records, list) and records:
                        parts.append(
                            f"{record_type}: {', '.join(str(r) for r in records[:3])}"
                        )
                if parts:
                    lines.append(f"  DNS: {' | '.join(parts)}")
            geo = data.get("geo_location")
            if isinstance(geo, list) and geo and isinstance(geo[0], dict):
                geo_parts = [
                    f"{k}: {v}"
                    for k, v in geo[0].items()
                    if k.lower() != "ip" and v not in (None, "")
                ]
                if geo_parts:
                    lines.append(f"  Geo: {' | '.join(geo_parts[:5])}")
            subdomains = data.get("subdomains")
            if isinstance(subdomains, list) and subdomains:
                shown = ", ".join(str(s) for s in subdomains[:10])
                more = (
                    f" ... and {len(subdomains) - 10} more"
                    if len(subdomains) > 10
                    else ""
                )
                lines.append(f"  Subdomains ({len(subdomains)}): {shown}{more}")
            return "\n".join(lines)

    async def get_socradar_threat_feed(
        collection_id: str,
        limit: int = 50,
    ) -> str:
        """Pull the IOCs of a SOCRadar threat feed collection."""
        cleaned = (collection_id or "").strip()
        if not cleaned:
            return (
                "Error: collection_id is required (the feed list ID from "
                "SOCRadar's Threat Intelligence Feeds page)."
            )
        capped = _clamp(limit, 50, hi=500)
        async with _client(120) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/threat/intelligence/feed_list/{_seg(cleaned)}.json",
                _PROVIDER,
                f"fetch threat feed {cleaned}",
                headers=headers,
                params={"v": 2, "key": api_key},
            )
            if err:
                return err
            data = _json_body(resp)
            if isinstance(data, dict):
                payload, env_err = _unwrap(data, f"fetch threat feed {cleaned}")
                if env_err:
                    return env_err
                data = payload
            items = [i for i in data if isinstance(i, dict)] if isinstance(data, list) else []
            result = format_entity_list(
                items[:capped],
                _format_feed_item,
                noun="feed IOC",
                total=len(items),
                empty_msg=f"Feed collection {cleaned} returned no IOCs.",
            )
            if len(items) > capped:
                result += (
                    f"\nShowing the first {capped} of {len(items)} IOCs - "
                    "raise limit (max 500) to see more."
                )
            return result

    # ══════════════════════════════════════════════════════════════════════
    # 3. DARK WEB MONITORING - hunting
    # ══════════════════════════════════════════════════════════════════════

    async def search_socradar_dark_web(
        source: str,
        search_string: str = "",
        record_status: str = "",
        start_date: str = "",
        end_date: str = "",
        alarm_id: str = "",
        limit: int = 20,
        page: int = 1,
    ) -> str:
        """Search one of the five SOCRadar dark web monitoring sources."""
        key = (source or "").strip().lower()
        segment = _DARK_WEB_SOURCES.get(key)
        if not segment:
            return (
                "Error: source must be one of "
                f"{', '.join(sorted(_DARK_WEB_SOURCES))} (got '{source}')."
            )
        params: dict[str, Any] = {
            "limit": _clamp(limit, 20),
            "page": max(1, _int_or(page, 1)),
        }
        # Per-source filters mirror the vendor's own client: botnet data
        # filters on searchString/recordStatus/alarmid, IM content on
        # searchString + dates, the rest on dates + alarmId.
        cleaned_alarm = (alarm_id or "").strip()
        if search_string and key in ("botnet_data", "im_content"):
            params["searchString"] = search_string
        elif search_string:
            return (
                "Error: search_string is only supported for the botnet_data "
                "and im_content sources - filter the other sources with "
                "start_date/end_date and alarm_id."
            )
        if record_status:
            if key != "botnet_data":
                return (
                    "Error: record_status is only supported for the "
                    "botnet_data source."
                )
            params["recordStatus"] = record_status
        if (start_date or end_date) and key == "botnet_data":
            return (
                "Error: start_date/end_date are not supported for the "
                "botnet_data source - filter it with search_string, "
                "record_status and alarm_id instead."
            )
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        if cleaned_alarm:
            if key == "im_content":
                return (
                    "Error: alarm_id is not supported for the im_content "
                    "source - use search_string instead."
                )
            params["alarmid" if key == "botnet_data" else "alarmId"] = cleaned_alarm
        async with _client(60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{company}/dark-web-monitoring/{segment}/v2",
                _PROVIDER,
                f"search dark web {key} findings",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload, env_err = _unwrap(
                _json_body(resp), f"search dark web {key} findings"
            )
            if env_err:
                return env_err
            items, total = _findings_payload(payload)
            result = format_entity_list(
                items,
                functools.partial(_format_finding, kind=key),
                noun=f"{key} finding",
                total=total,
                empty_msg=f"No {key} findings matched the filters.",
            )
            return result + _page_hint(
                len(items), params["limit"], params["page"], total
            )

    # ══════════════════════════════════════════════════════════════════════
    # 4. BRAND PROTECTION + FRAUD PROTECTION - digital risk
    # ══════════════════════════════════════════════════════════════════════

    async def list_socradar_brand_protection(
        category: str,
        search_string: str = "",
        record_status: str = "",
        alarm_status: str = "",
        country_code: str = "",
        start_date: str = "",
        end_date: str = "",
        include_historical: bool = False,
        limit: int = 20,
        page: int = 1,
    ) -> str:
        """List SOCRadar brand protection findings for one category."""
        key = (category or "").strip().lower()
        segment = _BRAND_CATEGORIES.get(key)
        if not segment:
            return (
                "Error: category must be one of "
                f"{', '.join(sorted(_BRAND_CATEGORIES))} (got '{category}')."
            )
        params: dict[str, Any] = {
            "limit": _clamp(limit, 20),
            "page": max(1, _int_or(page, 1)),
        }
        if search_string:
            params["searchString"] = search_string
        if record_status:
            params["recordStatus"] = record_status
        if alarm_status:
            params["alarmStatus"] = alarm_status
        if country_code:
            params["countryCode"] = country_code
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        if include_historical:
            params["includeHistorical"] = True
        async with _client(60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{company}/brand-protection/{segment}/v2",
                _PROVIDER,
                f"list brand protection {key} findings",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload, env_err = _unwrap(
                _json_body(resp), f"list brand protection {key} findings"
            )
            if env_err:
                return env_err
            items, total = _findings_payload(payload)
            result = format_entity_list(
                items,
                functools.partial(_format_finding, kind=key),
                noun=f"{key} finding",
                total=total,
                empty_msg=f"No {key} findings matched the filters.",
            )
            return result + _page_hint(
                len(items), params["limit"], params["page"], total
            )

    async def list_socradar_fraud_protection(
        search_string: str = "",
        record_status: str = "",
        alarm_status: str = "",
        cvv: str = "",
        expired: str = "",
        start_date: str = "",
        end_date: str = "",
        include_history: bool = False,
        limit: int = 20,
        page: int = 1,
    ) -> str:
        """List SOCRadar fraud protection (compromised card) findings."""
        params: dict[str, Any] = {
            "limit": _clamp(limit, 20),
            "page": max(1, _int_or(page, 1)),
        }
        if search_string:
            params["searchString"] = search_string
        if record_status:
            params["recordStatus"] = record_status
        if alarm_status:
            params["alarmStatus"] = alarm_status
        if cvv:
            params["cvv"] = cvv
        expired_flag = (expired or "").strip().lower()
        if expired_flag:
            if expired_flag not in ("true", "false"):
                return "Error: expired must be 'true', 'false', or empty."
            params["expired"] = expired_flag == "true"
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        if include_history:
            params["includeHistory"] = True
        async with _client(60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{company}/fraud-protection/v2",
                _PROVIDER,
                "list fraud protection findings",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload, env_err = _unwrap(
                _json_body(resp), "list fraud protection findings"
            )
            if env_err:
                return env_err
            items, total = _findings_payload(payload)
            result = format_entity_list(
                items,
                functools.partial(_format_finding, kind=_FRAUD_PROTECTION),
                noun="fraud protection finding",
                total=total,
                empty_msg="No fraud protection findings matched the filters.",
            )
            return result + _page_hint(
                len(items), params["limit"], params["page"], total
            )

    # ══════════════════════════════════════════════════════════════════════
    # 5. ATTACK SURFACE MANAGEMENT (DIGITAL FOOTPRINT)
    # ══════════════════════════════════════════════════════════════════════

    async def list_socradar_digital_assets(
        asset_type: str,
        sort_by: str = "",
        sort_desc: bool = False,
        limit: int = 20,
        page: int = 1,
    ) -> str:
        """List the company's ASM digital assets of one type."""
        cleaned_type = (asset_type or "").strip()
        if cleaned_type not in _ASSET_TYPES:
            return (
                "Error: asset_type must be one of "
                f"{', '.join(sorted(_ASSET_TYPES))} (got '{asset_type}')."
            )
        params: dict[str, Any] = {
            "assetType": cleaned_type,
            "pageNumber": max(1, _int_or(page, 1)),
            "pageLimit": _clamp(limit, 20),
        }
        if sort_by:
            params["sortBy"] = sort_by
        if sort_desc:
            params["sortDesc"] = True
        async with _client(60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{company}/asm",
                _PROVIDER,
                f"list {cleaned_type} digital assets",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload, env_err = _unwrap(
                _json_body(resp), f"list {cleaned_type} digital assets"
            )
            if env_err:
                return env_err
            items, total = _findings_payload(payload)

            def _format_asset(asset: dict) -> str:
                asset_id = asset.get("id") or asset.get("asset_id") or "?"
                name = (
                    asset.get("asset_name")
                    or asset.get("name")
                    or asset.get("domain")
                    or asset.get("ip_address")
                    or asset.get("url")
                    or "?"
                )
                line = f"#{asset_id} {name}"
                parts = []
                monitored = asset.get("is_monitor_on")
                if monitored is not None:
                    parts.append(
                        "monitored" if monitored else "monitoring off"
                    )
                source = asset.get("source")
                if source:
                    parts.append(f"source: {source}")
                for key in ("sub_type", "status", "risk_score"):
                    value = asset.get(key)
                    if value not in (None, ""):
                        parts.append(f"{key}: {_trunc(value, 40)}")
                if parts:
                    line += "\n  " + " | ".join(parts)
                return line

            result = format_entity_list(
                items,
                _format_asset,
                noun=f"{cleaned_type} asset",
                total=total,
                empty_msg=f"No {cleaned_type} assets found.",
            )
            return result + _page_hint(
                len(items), params["pageLimit"], params["pageNumber"], total
            )

    async def add_socradar_digital_asset(
        asset_type: str,
        asset_name: str,
        domain_type: str = "",
        cloud_platform_name: str = "",
        access_state: str = "",
    ) -> str:
        """Register a new digital asset for ASM monitoring."""
        cleaned_type = (asset_type or "").strip()
        if cleaned_type not in _ASSET_TYPES:
            return (
                "Error: asset_type must be one of "
                f"{', '.join(sorted(_ASSET_TYPES))} (got '{asset_type}')."
            )
        cleaned_name = (asset_name or "").strip()
        if not cleaned_name:
            return "Error: asset_name is required."
        # Per-type payloads mirror the vendor's own client: the asset value
        # rides a type-specific field next to the generic asset_name.
        body: dict[str, Any] = {"asset_name": cleaned_name, "source": "manual"}
        if cleaned_type == "domain":
            if not domain_type:
                return (
                    "Error: domain_type is required when asset_type is "
                    "'domain' (e.g. 'main' or 'sub')."
                )
            body["domain"] = cleaned_name
            body["domain_type"] = domain_type.strip()
        elif cleaned_type == "cloudBuckets":
            if not (cloud_platform_name and access_state):
                return (
                    "Error: cloud_platform_name and access_state are "
                    "required when asset_type is 'cloudBuckets' (e.g. "
                    "cloud_platform_name='aws', access_state='public')."
                )
            body["cloud_platform_name"] = cloud_platform_name.strip()
            body["access_state"] = access_state.strip()
            body["service_name"] = "manual_entry"
        elif cleaned_type == "ipAddress":
            body["ip_address"] = cleaned_name
        elif cleaned_type in ("website", "loginPage"):
            body["url"] = cleaned_name
        elif cleaned_type == "sslCertificate":
            body["hostname"] = cleaned_name
        elif cleaned_type == "ipBlock":
            body["cidr"] = cleaned_name
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{company}/asm/add/{_seg(cleaned_type)}",
                _PROVIDER,
                f"add {cleaned_type} asset {cleaned_name}",
                headers=headers,
                json=body,
            )
            if err:
                return err
            _payload, env_err = _unwrap(_json_body(resp), "add the asset")
            if env_err:
                return env_err
            return (
                f"Asset '{cleaned_name}' ({cleaned_type}) registered for "
                "monitoring. It appears in list_socradar_digital_assets "
                "once SOCRadar finishes the initial discovery."
            )

    async def manage_socradar_digital_asset(
        action: str,
        asset_id: str,
        asset_type: str,
        sub_type: str = "",
        notes: str = "",
    ) -> str:
        """Mark an ASM asset false positive or toggle its monitoring."""
        act = (action or "").strip().lower()
        if act not in _ASSET_ACTIONS:
            return (
                "Error: action must be one of "
                f"{', '.join(sorted(_ASSET_ACTIONS))} (got '{action}')."
            )
        cleaned_type = (asset_type or "").strip()
        if cleaned_type not in _ASSET_TYPES:
            return (
                "Error: asset_type must be one of "
                f"{', '.join(sorted(_ASSET_TYPES))} (got '{asset_type}')."
            )
        asset_id_int = _int_or((asset_id or "").strip(), -1)
        if asset_id_int < 0:
            return f"Error: asset_id must be numeric (got '{asset_id}')."
        if act == "mark_false_positive":
            endpoint = f"{company}/asm/fp"
            body: dict[str, Any] = {
                "asset_id": asset_id_int,
                "asset_type": cleaned_type,
                "sub_type": sub_type.strip(),
                "notes": notes,
            }
            action_label = f"mark asset {asset_id_int} false positive"
            success = (
                f"Asset {asset_id_int} ({cleaned_type}) marked as a false "
                "positive."
            )
        else:
            monitor_on = act == "enable_monitoring"
            endpoint = f"{company}/asm/monitor"
            body = {
                "asset_id": asset_id_int,
                "asset_type": cleaned_type,
                "is_monitor_on": monitor_on,
            }
            action_label = (
                f"{'enable' if monitor_on else 'disable'} monitoring for "
                f"asset {asset_id_int}"
            )
            success = (
                f"Monitoring {'enabled' if monitor_on else 'disabled'} for "
                f"asset {asset_id_int} ({cleaned_type})."
            )
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                endpoint,
                _PROVIDER,
                action_label,
                headers=headers,
                json=body,
            )
            if err:
                return err
            _payload, env_err = _unwrap(_json_body(resp), action_label)
            if env_err:
                return env_err
            return success

    # ══════════════════════════════════════════════════════════════════════
    # 6. TAKEDOWNS - response
    # ══════════════════════════════════════════════════════════════════════

    async def create_socradar_takedown(
        kind: str,
        entity: str,
        abuse_type: str = "",
        domain_type: str = "",
        notes: str = "",
        send_alarm: bool = True,
        email: str = "",
    ) -> str:
        """File a SOCRadar takedown request (domain / social media / source code)."""
        key = (kind or "").strip().lower()
        if key not in _TAKEDOWN_KINDS:
            return (
                "Error: kind must be one of "
                f"{', '.join(sorted(_TAKEDOWN_KINDS))} (got '{kind}')."
            )
        cleaned_entity = (entity or "").strip()
        if not cleaned_entity:
            return (
                "Error: entity is required (the domain, the impersonating "
                "account URL, or the source-code-leak finding ID)."
            )
        body: dict[str, Any]
        if key == "domain":
            if not abuse_type:
                return (
                    "Error: abuse_type is required for domain takedowns "
                    "(e.g. phishing, malware, brand_infringement, data_leak "
                    "- as defined in the SOCRadar takedown module)."
                )
            body = {
                "entity": cleaned_entity,
                "abuse_type": abuse_type.strip(),
                "type": (domain_type or "domain").strip(),
            }
            if send_alarm is not None:
                body["send_alarm"] = bool(send_alarm)
        elif key == "social_media":
            body = {"impersonating_account": cleaned_entity}
            if send_alarm is not None:
                body["send_alarm"] = bool(send_alarm)
        else:  # source_code
            leak_id = _int_or(cleaned_entity, -1)
            if leak_id < 0:
                return (
                    "Error: for source_code takedowns, entity must be the "
                    "numeric source-code-leak finding ID."
                )
            body = {"id": leak_id}
        if notes:
            body["notes"] = notes
        if email:
            body["email"] = email.strip()
        async with _client(60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/add/company/{_seg(company_id)}/takedown/request"
                f"{_TAKEDOWN_KINDS[key]}",
                _PROVIDER,
                f"create {key} takedown for {cleaned_entity}",
                headers=headers,
                json=body,
            )
            if err:
                return err
            _payload, env_err = _unwrap(
                _json_body(resp), "create the takedown request"
            )
            if env_err:
                return env_err
            return (
                f"Takedown request ({key}) filed for '{cleaned_entity}'. "
                "Track it with get_socradar_takedown_progress."
            )

    async def get_socradar_takedown_progress(asset_id: str, kind: str) -> str:
        """Track the progress of a SOCRadar takedown request."""
        cleaned = (asset_id or "").strip()
        cleaned_kind = (kind or "").strip()
        if not (cleaned and cleaned_kind):
            return "Error: asset_id and kind are required."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/get/company/{_seg(company_id)}/takedown/progress",
                _PROVIDER,
                f"get takedown progress for {cleaned}",
                headers=headers,
                params={"asset_id": cleaned, "type": cleaned_kind},
            )
            if err:
                return err
            payload, env_err = _unwrap(
                _json_body(resp), "get the takedown progress"
            )
            if env_err:
                return env_err
            if isinstance(payload, dict) and payload:
                lines = [f"Takedown progress for {cleaned}:"]
                for pkey, value in payload.items():
                    if isinstance(value, (str, int, float, bool)):
                        lines.append(f"  {pkey}: {_trunc(value, 120)}")
                return "\n".join(lines)
            if isinstance(payload, list) and payload:
                lines = [f"Takedown progress for {cleaned}:"]
                for row in payload[:10]:
                    if isinstance(row, dict):
                        parts = [
                            f"{k}: {_trunc(v, 60)}"
                            for k, v in row.items()
                            if isinstance(v, (str, int, float, bool))
                        ]
                        lines.append("  - " + " | ".join(parts[:6]))
                return "\n".join(lines)
            return f"No takedown progress found for asset {cleaned}."

    # ══════════════════════════════════════════════════════════════════════
    # 7. AUDIT LOGS - governance
    # ══════════════════════════════════════════════════════════════════════

    async def list_socradar_audit_logs(
        start_date: str = "",
        end_date: str = "",
        user_id: str = "",
        action_type: str = "",
        limit: int = 20,
        page: int = 1,
    ) -> str:
        """List the SOCRadar platform user audit trail."""
        params: dict[str, Any] = {
            "limit": _clamp(limit, 20),
            "page": max(1, _int_or(page, 1)),
        }
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        if user_id:
            params["userId"] = user_id.strip()
        if action_type:
            params["actionType"] = action_type.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{company}/auditlogs",
                _PROVIDER,
                "list audit logs",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload, env_err = _unwrap(_json_body(resp), "list audit logs")
            if env_err:
                return env_err
            items, total = _findings_payload(payload)

            def _format_log(log: dict) -> str:
                when = _ts(
                    log.get("date")
                    or log.get("created_at")
                    or log.get("timestamp")
                )
                who = (
                    log.get("user_email")
                    or log.get("user")
                    or log.get("email")
                    or "?"
                )
                what = (
                    log.get("action")
                    or log.get("action_type")
                    or log.get("activity")
                    or "?"
                )
                line = f"{when or '?'} {who}: {_trunc(what, 100)}"
                detail = log.get("detail") or log.get("description")
                if detail:
                    line += f"\n  {_trunc(detail, 160)}"
                return line

            result = format_entity_list(
                items,
                _format_log,
                noun="audit log entry",
                total=total,
                empty_msg="No audit log entries matched the filters.",
            )
            return result + _page_hint(
                len(items), params["limit"], params["page"], total
            )

    # ── Registration ─────────────────────────────────────────────────────

    executor.register("get_socradar_status", get_socradar_status)
    executor.register("list_socradar_incidents", list_socradar_incidents)
    executor.register("get_socradar_incident", get_socradar_incident)
    executor.register(
        "change_socradar_incident_status", change_socradar_incident_status
    )
    executor.register(
        "add_socradar_incident_comment", add_socradar_incident_comment
    )
    executor.register("assign_socradar_incident", assign_socradar_incident)
    executor.register(
        "manage_socradar_incident_tags", manage_socradar_incident_tags
    )
    executor.register("analyze_socradar_indicator", analyze_socradar_indicator)
    executor.register("get_socradar_threat_feed", get_socradar_threat_feed)
    executor.register("search_socradar_dark_web", search_socradar_dark_web)
    executor.register(
        "list_socradar_brand_protection", list_socradar_brand_protection
    )
    executor.register(
        "list_socradar_fraud_protection", list_socradar_fraud_protection
    )
    executor.register(
        "list_socradar_digital_assets", list_socradar_digital_assets
    )
    executor.register("add_socradar_digital_asset", add_socradar_digital_asset)
    executor.register(
        "manage_socradar_digital_asset", manage_socradar_digital_asset
    )
    executor.register("create_socradar_takedown", create_socradar_takedown)
    executor.register(
        "get_socradar_takedown_progress", get_socradar_takedown_progress
    )
    executor.register("list_socradar_audit_logs", list_socradar_audit_logs)

    _int = {"type": "integer"}

    return [
        # ── 0. Connection ─────────────────────────────────────────────────
        ToolDefinition(
            name="get_socradar_status",
            description=(
                "Validate the SOCRadar connection and report the company "
                "scope (company ID and the number of incidents visible to "
                "it). Use this first when SOCRadar calls fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        # ── 1. Incidents (alarms) ─────────────────────────────────────────
        ToolDefinition(
            name="list_socradar_incidents",
            description=(
                "List SOCRadar incidents (alarms) with structured filters: "
                "status (OPEN, CLOSED, ON_HOLD), severities (INFO, LOW, "
                "MEDIUM, HIGH, CRITICAL), alarm main/sub types (e.g. 'Brand "
                "Protection', 'Impersonating Domain'), numeric alarm type "
                "IDs, and a start/end date window. Returns alarm IDs for "
                "follow-up calls (get / change status / comment / assign / "
                "tag). Use page for pagination (max 100 per page)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Status filter (CSV of OPEN, CLOSED, ON_HOLD; empty = all)",
                        "default": "",
                    },
                    "severities": {
                        "type": "string",
                        "description": "Severity filter (CSV of INFO, LOW, MEDIUM, HIGH, CRITICAL; empty = all)",
                        "default": "",
                    },
                    "alarm_main_types": {
                        "type": "string",
                        "description": "Main alarm type filter (CSV, e.g. 'Brand Protection,Cyber Threat Intelligence')",
                        "default": "",
                    },
                    "alarm_sub_types": {
                        "type": "string",
                        "description": "Alarm sub-type filter (CSV, e.g. 'Impersonating Domain')",
                        "default": "",
                    },
                    "alarm_type_ids": {
                        "type": "string",
                        "description": "Numeric alarm type IDs to include (CSV)",
                        "default": "",
                    },
                    "excluded_alarm_type_ids": {
                        "type": "string",
                        "description": "Numeric alarm type IDs to exclude (CSV)",
                        "default": "",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Only alarms at/after this date (YYYY-MM-DD)",
                        "default": "",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Only alarms at/before this date (YYYY-MM-DD)",
                        "default": "",
                    },
                    "limit": {
                        **_int,
                        "description": "Max results per page (max 100)",
                        "default": 20,
                    },
                    "page": {
                        **_int,
                        "description": "Page number, 1-indexed",
                        "default": 1,
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_socradar_incident",
            description=(
                "Get one SOCRadar incident (alarm) by its numeric alarm ID "
                "with the full triage context: related entities and assets, "
                "alarm content fields, and the suggested mitigation plan. "
                "Call list_socradar_incidents first to find alarm IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alarm_id": {
                        "type": "string",
                        "description": "Numeric alarm ID",
                    },
                },
                "required": ["alarm_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="change_socradar_incident_status",
            description=(
                "Change the status of one or more SOCRadar alarms - the "
                "triage/resolution action. status_reason is one of the 11 "
                "resolution reasons: OPEN, INVESTIGATING, RESOLVED, "
                "PENDING_INFO, LEGAL_REVIEW, VENDOR_ASSESSMENT, "
                "FALSE_POSITIVE, DUPLICATE, PROCESSED_INTERNALLY, MITIGATED, "
                "NOT_APPLICABLE. Use FALSE_POSITIVE to dismiss noise and "
                "RESOLVED to close out a handled alarm."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alarm_ids": {
                        "type": "string",
                        "description": "CSV of numeric alarm IDs",
                    },
                    "status_reason": {
                        "type": "string",
                        "description": "New status reason",
                        "enum": sorted(_STATUS_REASONS),
                    },
                    "comments": {
                        "type": "string",
                        "description": "Optional note explaining the status change",
                        "default": "",
                    },
                },
                "required": ["alarm_ids", "status_reason"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_socradar_incident_comment",
            description=(
                "Add an investigation comment to a SOCRadar alarm. "
                "user_email identifies the SOCRadar platform user the "
                "comment is posted as (must belong to the company)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alarm_id": {
                        "type": "string",
                        "description": "Numeric alarm ID",
                    },
                    "comment": {
                        "type": "string",
                        "description": "Comment text",
                    },
                    "user_email": {
                        "type": "string",
                        "description": "Email of the SOCRadar user posting the comment",
                    },
                },
                "required": ["alarm_id", "comment", "user_email"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="assign_socradar_incident",
            description=(
                "Assign a SOCRadar alarm to one or more analysts by email. "
                "The users must exist in the same SOCRadar company."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alarm_id": {
                        "type": "string",
                        "description": "Numeric alarm ID",
                    },
                    "user_emails": {
                        "type": "string",
                        "description": "CSV of analyst email addresses",
                    },
                },
                "required": ["alarm_id", "user_emails"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_socradar_incident_tags",
            description=(
                "Add or remove a tag on a SOCRadar alarm. action='add' "
                "applies the tag, action='remove' strips it. Tags help "
                "organize triage (e.g. 'reviewed', 'escalated')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove"],
                        "description": "Whether to add or remove the tag",
                    },
                    "alarm_id": {
                        "type": "string",
                        "description": "Numeric alarm ID",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Tag name",
                    },
                },
                "required": ["action", "alarm_id", "tag"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 2. Threat intelligence ────────────────────────────────────────
        ToolDefinition(
            name="analyze_socradar_indicator",
            description=(
                "Enrich an IOC with SOCRadar ThreatFusion threat analysis: "
                "risk score (0-1000), whitelist status, threat feed "
                "encounters, whois, DNS records, geo/ASN context (IPs), and "
                "subdomains (domains). entity is a single IP address, "
                "domain, URL, or file hash (MD5/SHA-1). Note: the "
                "ThreatFusion API is rate limited (~6 requests/minute) - "
                "prefer one call per distinct indicator."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity": {
                        "type": "string",
                        "description": "The IOC to analyze (IP, domain, URL, or file hash)",
                    },
                },
                "required": ["entity"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_socradar_threat_feed",
            description=(
                "Pull the IOCs of a SOCRadar threat intelligence feed "
                "collection (value, type, maintainer, score, seen count, "
                "first/last seen). collection_id is the feed list ID from "
                "SOCRadar's Threat Intelligence Feeds page. Feeds can be "
                "large - the output is capped by limit."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "collection_id": {
                        "type": "string",
                        "description": "Feed list (collection) ID",
                    },
                    "limit": {
                        **_int,
                        "description": "Max IOCs to show (max 500)",
                        "default": 50,
                    },
                },
                "required": ["collection_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 3. Dark web monitoring ────────────────────────────────────────
        ToolDefinition(
            name="search_socradar_dark_web",
            description=(
                "Search SOCRadar dark web monitoring findings for the "
                "company. source selects the dataset: botnet_data (stealer "
                "log / botnet credentials - supports search_string, "
                "record_status and alarm_id), blackmarket (dark web "
                "marketplace listings), suspicious_content (threatening "
                "posts), pii_exposure (leaked personal data), or im_content "
                "(Telegram/Discord chatter - supports search_string). "
                "blackmarket / suspicious_content / pii_exposure filter on "
                "start_date/end_date and alarm_id. Returns alarm IDs usable "
                "with the incident tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": sorted(_DARK_WEB_SOURCES),
                        "description": "Dark web dataset to search",
                    },
                    "search_string": {
                        "type": "string",
                        "description": "Keyword filter (botnet_data and im_content only)",
                        "default": "",
                    },
                    "record_status": {
                        "type": "string",
                        "description": "Record status filter (botnet_data only, e.g. OPEN)",
                        "default": "",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Only findings at/after this date (YYYY-MM-DD; not for botnet_data)",
                        "default": "",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Only findings at/before this date (YYYY-MM-DD; not for botnet_data)",
                        "default": "",
                    },
                    "alarm_id": {
                        "type": "string",
                        "description": "Filter to one alarm's findings (not for im_content)",
                        "default": "",
                    },
                    "limit": {
                        **_int,
                        "description": "Max results per page (max 100)",
                        "default": 20,
                    },
                    "page": {
                        **_int,
                        "description": "Page number, 1-indexed",
                        "default": 1,
                    },
                },
                "required": ["source"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 4. Brand + fraud protection ───────────────────────────────────
        ToolDefinition(
            name="list_socradar_brand_protection",
            description=(
                "List SOCRadar brand protection findings for one category: "
                "impersonating_domains (lookalike/phishing domains), "
                "impersonating_accounts (fake social accounts), "
                "rogue_mobile_applications (unauthorized app store "
                "listings), bad_reputation (blacklisted company assets), or "
                "social_media_findings. Filter by search string, record/"
                "alarm status, country code and a date window. Returns "
                "alarm IDs usable with the incident tools (e.g. to resolve "
                "the alarm or mark it a false positive)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": sorted(_BRAND_CATEGORIES),
                        "description": "Brand protection category to list",
                    },
                    "search_string": {
                        "type": "string",
                        "description": "Keyword filter (e.g. a domain fragment)",
                        "default": "",
                    },
                    "record_status": {
                        "type": "string",
                        "description": "Record status filter (e.g. OPEN, CLOSED)",
                        "default": "",
                    },
                    "alarm_status": {
                        "type": "string",
                        "description": "Alarm status filter (e.g. OPEN, RESOLVED)",
                        "default": "",
                    },
                    "country_code": {
                        "type": "string",
                        "description": "2-letter country code filter (e.g. US)",
                        "default": "",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Only findings at/after this date (YYYY-MM-DD)",
                        "default": "",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Only findings at/before this date (YYYY-MM-DD)",
                        "default": "",
                    },
                    "include_historical": {
                        "type": "boolean",
                        "description": "Include historical (closed-out) findings",
                        "default": False,
                    },
                    "limit": {
                        **_int,
                        "description": "Max results per page (max 100)",
                        "default": 20,
                    },
                    "page": {
                        **_int,
                        "description": "Page number, 1-indexed",
                        "default": 1,
                    },
                },
                "required": ["category"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_socradar_fraud_protection",
            description=(
                "List SOCRadar fraud protection findings - payment cards "
                "belonging to the company's BINs detected in dark web "
                "dumps and stealer logs. Filter by search string, record/"
                "alarm status, CVV presence, expiry state and a date "
                "window. Card data is masked by SOCRadar; use the alarm "
                "IDs with the incident tools for triage."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search_string": {
                        "type": "string",
                        "description": "Keyword filter (e.g. a BIN prefix)",
                        "default": "",
                    },
                    "record_status": {
                        "type": "string",
                        "description": "Record status filter (e.g. OPEN, CLOSED)",
                        "default": "",
                    },
                    "alarm_status": {
                        "type": "string",
                        "description": "Alarm status filter (e.g. OPEN, RESOLVED)",
                        "default": "",
                    },
                    "cvv": {
                        "type": "string",
                        "description": "CVV presence filter as exposed by the API",
                        "default": "",
                    },
                    "expired": {
                        "type": "string",
                        "enum": ["", "true", "false"],
                        "description": "Filter by card expiry state (empty = all)",
                        "default": "",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Only findings at/after this date (YYYY-MM-DD)",
                        "default": "",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Only findings at/before this date (YYYY-MM-DD)",
                        "default": "",
                    },
                    "include_history": {
                        "type": "boolean",
                        "description": "Include the finding status history",
                        "default": False,
                    },
                    "limit": {
                        **_int,
                        "description": "Max results per page (max 100)",
                        "default": 20,
                    },
                    "page": {
                        **_int,
                        "description": "Page number, 1-indexed",
                        "default": 1,
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 5. Attack surface management ──────────────────────────────────
        ToolDefinition(
            name="list_socradar_digital_assets",
            description=(
                "List the company's SOCRadar attack surface (ASM) digital "
                "assets of one type: domain, ipAddress, website, loginPage, "
                "dnsRecord, sslCertificate, ipBlock, mobileApplication, "
                "technology, cloudBuckets, socialAccounts, or "
                "sourceCodeRepo. Returns asset IDs used by "
                "manage_socradar_digital_asset (false-positive marking, "
                "monitoring toggles)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "asset_type": {
                        "type": "string",
                        "enum": sorted(_ASSET_TYPES),
                        "description": "Digital asset type to list",
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "Field to sort by (as exposed by the ASM API)",
                        "default": "",
                    },
                    "sort_desc": {
                        "type": "boolean",
                        "description": "Sort descending",
                        "default": False,
                    },
                    "limit": {
                        **_int,
                        "description": "Max results per page (max 100)",
                        "default": 20,
                    },
                    "page": {
                        **_int,
                        "description": "Page number, 1-indexed",
                        "default": 1,
                    },
                },
                "required": ["asset_type"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_socradar_digital_asset",
            description=(
                "Register a new digital asset for SOCRadar ASM monitoring. "
                "asset_name is the asset value (the domain, IP address, "
                "URL, hostname, CIDR, bucket name, ...). domain assets "
                "additionally need domain_type; cloudBuckets need "
                "cloud_platform_name and access_state. Adding an asset "
                "expands what SOCRadar scans and alarms on."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "asset_type": {
                        "type": "string",
                        "enum": sorted(_ASSET_TYPES),
                        "description": "Digital asset type to add",
                    },
                    "asset_name": {
                        "type": "string",
                        "description": (
                            "Asset value (domain, IP, URL, hostname, CIDR, "
                            "bucket name, ...)"
                        ),
                    },
                    "domain_type": {
                        "type": "string",
                        "description": "Domain type (domain assets only, e.g. 'main' or 'sub')",
                        "default": "",
                    },
                    "cloud_platform_name": {
                        "type": "string",
                        "description": "Cloud platform (cloudBuckets only, e.g. 'aws')",
                        "default": "",
                    },
                    "access_state": {
                        "type": "string",
                        "description": "Bucket access state (cloudBuckets only, e.g. 'public')",
                        "default": "",
                    },
                },
                "required": ["asset_type", "asset_name"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_socradar_digital_asset",
            description=(
                "Manage an existing SOCRadar ASM digital asset: "
                "mark_false_positive removes a wrongly attributed asset "
                "from the attack surface (give context in notes), "
                "enable_monitoring / disable_monitoring toggle scanning "
                "for it. Get asset IDs from list_socradar_digital_assets."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_ASSET_ACTIONS),
                        "description": "What to do with the asset",
                    },
                    "asset_id": {
                        "type": "string",
                        "description": "Numeric asset ID",
                    },
                    "asset_type": {
                        "type": "string",
                        "enum": sorted(_ASSET_TYPES),
                        "description": "The asset's type",
                    },
                    "sub_type": {
                        "type": "string",
                        "description": "Asset sub-type (mark_false_positive only, as shown in the asset list)",
                        "default": "",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Why the asset is a false positive (mark_false_positive only)",
                        "default": "",
                    },
                },
                "required": ["action", "asset_id", "asset_type"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 6. Takedowns ──────────────────────────────────────────────────
        ToolDefinition(
            name="create_socradar_takedown",
            description=(
                "File a SOCRadar takedown request against malicious "
                "content. kind selects the flow: 'domain' takes entity = "
                "the offending domain plus abuse_type (e.g. phishing, "
                "malware, brand_infringement, data_leak); 'social_media' "
                "takes entity = the impersonating account URL/handle; "
                "'source_code' takes entity = the numeric "
                "source-code-leak finding ID. Takedowns are executed by "
                "SOCRadar's takedown team and may take days - track with "
                "get_socradar_takedown_progress. Requires the takedown "
                "module subscription."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": sorted(_TAKEDOWN_KINDS),
                        "description": "Takedown flow to use",
                    },
                    "entity": {
                        "type": "string",
                        "description": (
                            "Domain (kind=domain), impersonating account "
                            "URL/handle (kind=social_media), or numeric "
                            "leak finding ID (kind=source_code)"
                        ),
                    },
                    "abuse_type": {
                        "type": "string",
                        "description": (
                            "Abuse type for domain takedowns (e.g. "
                            "phishing, malware, brand_infringement, "
                            "data_leak)"
                        ),
                        "default": "",
                    },
                    "domain_type": {
                        "type": "string",
                        "description": "Takedown request type for domain takedowns (default 'domain')",
                        "default": "",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Context for the takedown team",
                        "default": "",
                    },
                    "send_alarm": {
                        "type": "boolean",
                        "description": "Also raise a SOCRadar alarm for this takedown",
                        "default": True,
                    },
                    "email": {
                        "type": "string",
                        "description": "Email to notify about takedown updates",
                        "default": "",
                    },
                },
                "required": ["kind", "entity"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_socradar_takedown_progress",
            description=(
                "Track the progress of a SOCRadar takedown request. "
                "asset_id is the takedown target's asset/finding ID and "
                "kind is the takedown type used when filing it (as in "
                "create_socradar_takedown)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "asset_id": {
                        "type": "string",
                        "description": "Takedown target asset/finding ID",
                    },
                    "kind": {
                        "type": "string",
                        "description": "Takedown type (as used when filing)",
                    },
                },
                "required": ["asset_id", "kind"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 7. Audit logs ─────────────────────────────────────────────────
        ToolDefinition(
            name="list_socradar_audit_logs",
            description=(
                "List the SOCRadar platform user audit trail (who did what "
                "and when in the SOCRadar UI/API). Filter by a date "
                "window, a platform user ID, and an action type. Useful "
                "for governance reviews and investigating configuration "
                "changes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Only entries at/after this date (YYYY-MM-DD)",
                        "default": "",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Only entries at/before this date (YYYY-MM-DD)",
                        "default": "",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "Filter to one platform user's actions",
                        "default": "",
                    },
                    "action_type": {
                        "type": "string",
                        "description": "Filter by action type (as exposed by the audit API)",
                        "default": "",
                    },
                    "limit": {
                        **_int,
                        "description": "Max results per page (max 100)",
                        "default": 20,
                    },
                    "page": {
                        **_int,
                        "description": "Page number, 1-indexed",
                        "default": 1,
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
