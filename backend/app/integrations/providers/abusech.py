# SPDX-License-Identifier: Apache-2.0

"""abuse.ch integration - community threat intelligence (MalwareBazaar, URLhaus, ThreatFox).

One provider covering the three flagship abuse.ch community platforms, designed
as the *instant free IOC triage* building block for an agentic SOC/CTI team:

  - **MalwareBazaar** (``mb-api.abuse.ch``): malware sample intelligence -
    look up a file hash (SHA256/SHA1/MD5) for the full sample dossier
    (family signature, tags, code-signing certificate, vendor sandbox intel),
    list the newest samples, and search by tag / family signature / file type /
    ClamAV signature / imphash / TLSH / telfhash / gimphash / icon dhash /
    YARA rule / code-signing certificate attributes.
  - **URLhaus** (``urlhaus-api.abuse.ch``): malicious URL intelligence -
    look up a URL or a host (IP/domain) for its malware-distribution history,
    look up a payload hash, list the newest malicious URLs/payloads, and
    search URLs by tag or malware family signature.
  - **ThreatFox** (``threatfox-api.abuse.ch``): the IOC exchange - search IOCs
    (C2 IPs, domains, URLs, hashes) by value / file hash / tag / malware
    family, read a single IOC with its associated malware samples, pull the
    recent IOC feed (up to 7 days - older IOCs are expired since 2025-05-01),
    resolve free-form malware names to canonical Malpedia labels, browse the
    threat-type / tag / malware-family reference data, and share (submit)
    vetted IOCs back to the community.

Deliberately NOT exposed: malware sample *download* endpoints (pulling live
malware binaries into the platform is out of scope for an assistant tool) and
MalwareBazaar sample *upload* (requires a real file + strict vetting policy).

Authentication
--------------
Since 2024/2025 every abuse.ch API requires an **Auth-Key** - a single free
key from https://auth.abuse.ch that is shared across ALL abuse.ch platforms
(MalwareBazaar, URLhaus, ThreatFox, YARAify, Feodo Tracker). It is sent as the
``Auth-Key`` HTTP header on every request. The key is stored in
``api_key_enc`` and surfaces here as the plain token - no compound assembly.

The APIs are free of charge under the abuse.ch Fair Use Policy (heavy
commercial usage may require the paid enhanced API). There are no documented
hard per-endpoint rate limits; HTTP 429 responses ride the shared
``safe_request`` retry. All three APIs answer HTTP 200 with an in-body
``query_status`` field - non-``ok`` statuses (``hash_not_found``,
``no_results``, ``illegal_hash``, ...) are surfaced as readable messages via a
shared in-body status check instead of being mistaken for successful lookups.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    format_entity_list,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "abuse.ch"
_SOURCE = "integration:abusech"

# The three community API hosts (fixed - no self-hosted variant).
MALWAREBAZAAR_API = "https://mb-api.abuse.ch/api/v1/"
URLHAUS_API = "https://urlhaus-api.abuse.ch/v1"
THREATFOX_API = "https://threatfox-api.abuse.ch/api/v1/"

_DEFAULT_TIMEOUT = 30
# Search-style endpoints accept a limit up to 1000; keep agent output legible.
_API_MAX_LIMIT = 1000
_DISPLAY_MAX = 100

_MD5_RE = re.compile(r"[A-Fa-f0-9]{32}")
_SHA1_RE = re.compile(r"[A-Fa-f0-9]{40}")
_SHA256_RE = re.compile(r"[A-Fa-f0-9]{64}")

# In-body ``query_status`` values that mean success. Most endpoints answer
# ``ok``; MalwareBazaar's write endpoints answer ``success`` (add_comment),
# ``inserted`` (upload) or ``updated`` (update).
_OK_STATUSES: frozenset[str] = frozenset({"ok", "success", "inserted", "updated"})

# In-body ``query_status`` values that mean "the query worked but found
# nothing" - benign misses, not errors.
_NOT_FOUND_STATUSES: frozenset[str] = frozenset(
    {
        "no_results",
        "no_result",
        "hash_not_found",
        "tag_not_found",
        "signature_not_found",
        "clamav_not_found",
        "yara_not_found",
        "file_not_found",
        "url_not_found",
        "host_not_found",
        "ioc_not_found",
        "not_found",
    }
)


def _is_valid_hash(value: str) -> bool:
    """True if *value* is a well-formed MD5, SHA1 or SHA256 hex hash."""
    value = (value or "").strip()
    return bool(
        _MD5_RE.fullmatch(value)
        or _SHA1_RE.fullmatch(value)
        or _SHA256_RE.fullmatch(value)
    )


def _is_valid_sha256(value: str) -> bool:
    """True if *value* is a well-formed SHA256 hex hash."""
    return bool(_SHA256_RE.fullmatch((value or "").strip()))


def _trunc(text: Any, max_len: int = 200) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def _tags(entry: dict) -> str:
    tags = entry.get("tags")
    if not isinstance(tags, list) or not tags:
        return ""
    return ", ".join(str(t) for t in tags[:8])


def _as_list(value: Any) -> list:
    """Return *value* if it is a list, else ``[]``.

    The abuse.ch APIs normally return list payloads for ``data`` / ``urls`` /
    ``payloads``, but an unexpected shape (object/null despite
    ``query_status: ok``) must degrade to an empty result instead of raising
    through slicing/indexing and breaking the readable-strings contract.
    """
    return value if isinstance(value, list) else []


def _more_results_hint(
    total: int, shown: int, limit: int, *, narrow_hint: str = "narrow the search"
) -> str:
    """Truncation hint that only suggests raising ``limit`` when it can help.

    Output is always capped at :data:`_DISPLAY_MAX` rows to keep agent
    context legible, so once *limit* has reached that cap a higher limit
    cannot reveal more rows - say so instead of sending the agent in a loop.
    *narrow_hint* customises the guidance for that case (e.g. "narrow days"
    for feed-style tools where there is no search to narrow).
    """
    if total <= shown:
        return ""
    if limit < _DISPLAY_MAX:
        return f"\n[{total - shown} more results - raise limit to see more]"
    return (
        f"\n[{total - shown} more results - output is capped at "
        f"{_DISPLAY_MAX} rows; {narrow_hint} to see the rest]"
    )


def _parse_json_body(
    resp: httpx.Response, service: str, action: str
) -> tuple[Any, str | None]:
    """Parse a JSON response body -> ``(payload, None)`` or ``(None, error)``.

    ``safe_request`` only validates the HTTP status - an HTML error page or
    empty body would make ``resp.json()`` raise and crash the tool. Map parse
    failures to the same readable-error contract as every other failure mode.
    """
    try:
        return resp.json(), None
    except ValueError:
        return None, (
            f"Error: {_PROVIDER} {service} returned a non-JSON response "
            f"while trying to {action} - the service may be temporarily "
            "unavailable; retry shortly."
        )


def _body_status_error(
    payload: Any, service: str, action: str, *, empty_msg: str
) -> tuple[dict, str | None]:
    """Check the abuse.ch in-body ``query_status`` envelope.

    All three APIs answer HTTP 200 with ``{"query_status": "..."}`` - only
    ``ok`` carries data. Returns ``(payload_dict, None)`` on success,
    ``(payload_dict, message)`` on a benign miss (*empty_msg*) or a real
    in-body error (readable ``Error: ...`` string).
    """
    if not isinstance(payload, dict):
        return {}, f"Error: {_PROVIDER} {service} returned an unexpected response while trying to {action}."
    status = str(payload.get("query_status") or "").strip().lower()
    if status in _OK_STATUSES:
        return payload, None
    if status in _NOT_FOUND_STATUSES:
        return payload, empty_msg
    if status == "no_api_key":
        return payload, (
            f"Error: {_PROVIDER} rejected the request - no Auth-Key was "
            "accepted. Configure a valid abuse.ch Auth-Key on the integration "
            "(free from auth.abuse.ch)."
        )
    if status == "user_blacklisted":
        return payload, (
            "Error: this abuse.ch Auth-Key is blacklisted. Contact abuse.ch "
            "support via the Spamhaus Technology contact form."
        )
    return payload, (
        f"Error: {_PROVIDER} {service} rejected the request to {action} "
        f"(query_status: {status or 'unknown'})."
    )


# ---------------------------------------------------------------------------
# Auth helper (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def build_abusech_headers(auth_key: str) -> dict[str, str]:
    """Build abuse.ch request headers (``Auth-Key`` auth + JSON Accept).

    The key is stripped so a value pasted with trailing/leading whitespace can
    never produce a malformed header. This is the single chokepoint shared by
    the tool builder, ``validate_connection``, and the connectivity test.
    """
    return {
        "Auth-Key": (auth_key or "").strip(),
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_sample_line(s: dict) -> str:
    """One-line summary of a MalwareBazaar sample."""
    sig = s.get("signature") or "unclassified"
    tags = _tags(s)
    parts = [
        f"  {s.get('sha256_hash', '?')}",
        f"{sig} | {s.get('file_type') or '?'} | {s.get('file_size') or '?'} bytes",
        f"first seen {str(s.get('first_seen', ''))[:16] or '?'}",
    ]
    if s.get("file_name"):
        parts.append(_trunc(s.get("file_name"), 60))
    if tags:
        parts.append(f"[{tags}]")
    return " | ".join(parts)


def _format_url_line(u: dict) -> str:
    """One-line summary of a URLhaus URL entry."""
    parts = [
        f"  {_trunc(u.get('url'), 120) or '?'}",
        f"{u.get('url_status') or '?'}",
        f"added {str(u.get('date_added') or u.get('dateadded') or '')[:16] or '?'}",
    ]
    threat = u.get("threat")
    if threat:
        parts.append(str(threat))
    tags = _tags(u)
    if tags:
        parts.append(f"[{tags}]")
    ident = u.get("id") or u.get("url_id")
    if ident:
        parts.append(f"id {ident}")
    return " | ".join(parts)


def _format_payload_line(p: dict) -> str:
    """One-line summary of a URLhaus payload entry."""
    sig = p.get("signature") or "unclassified"
    vt = p.get("virustotal") or {}
    vt_part = f" | VT {vt.get('percent')}%" if isinstance(vt, dict) and vt.get("percent") else ""
    return (
        f"  {p.get('sha256_hash', '?')} | {sig} | {p.get('file_type') or '?'} | "
        f"{p.get('file_size') or '?'} bytes | first seen "
        f"{str(p.get('firstseen', ''))[:16] or '?'}{vt_part}"
    )


def _format_ioc_line(i: dict) -> str:
    """One-line summary of a ThreatFox IOC."""
    parts = [
        f"  #{i.get('id', '?')} {_trunc(i.get('ioc'), 100) or '?'}",
        f"{i.get('ioc_type') or '?'} / {i.get('threat_type') or '?'}",
        f"{i.get('malware_printable') or i.get('malware') or 'unknown malware'}",
        f"confidence {i.get('confidence_level', '?')}",
        f"first seen {str(i.get('first_seen', ''))[:16] or '?'}",
    ]
    tags = _tags(i)
    if tags:
        parts.append(f"[{tags}]")
    return " | ".join(parts)


def _format_blacklists(entry: dict) -> str:
    bl = entry.get("blacklists")
    if not isinstance(bl, dict) or not bl:
        return ""
    return ", ".join(f"{k}: {v}" for k, v in bl.items())


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class AbuseChIntegration(BaseIntegration):
    """abuse.ch community threat intelligence integration."""

    provider_name = "abusech"
    display_name = "abuse.ch"
    # "Radar" is a registered ICON_MAP key in frontend/src/lib/constants.ts
    # ("Bug" is not - an unregistered key silently falls back to the generic
    # icon on every provider-icon surface).
    icon = "Radar"
    description = (
        "Connect abuse.ch for free community threat intelligence: MalwareBazaar "
        "malware sample lookups, URLhaus malicious URL and host intelligence, "
        "and ThreatFox IOC search and sharing - instant IOC triage across "
        "hashes, URLs, and C2 infrastructure with a single free Auth-Key."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        auth_key = (token or "").strip()
        if not auth_key:
            return False
        # URLhaus "recent URLs" is the cheapest authenticated GET across the
        # three services (an invalid/missing Auth-Key returns HTTP 401).
        async with egress_client(timeout=15) as client:
            _resp, err = await safe_request(
                client,
                "get",
                f"{URLHAUS_API}/urls/recent/limit/1/",
                _PROVIDER,
                "validate connection",
                headers=build_abusech_headers(auth_key),
            )
        return err is None

    async def get_user_info(self, token: str) -> dict:
        # abuse.ch has no account-info endpoint on the community APIs; report
        # the services the key unlocks once it validates.
        if await self.validate_connection(token):
            return {"services": ["MalwareBazaar", "URLhaus", "ThreatFox"]}
        return {}


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------

# MalwareBazaar consolidated search selectors:
# search_by -> (query value, request param, supports limit)
_MB_SEARCH_SELECTORS: dict[str, tuple[str, str, bool]] = {
    "tag": ("get_taginfo", "tag", True),
    "signature": ("get_siginfo", "signature", True),
    "file_type": ("get_file_type", "file_type", True),
    "clamav": ("get_clamavinfo", "clamav", True),
    "imphash": ("get_imphash", "imphash", True),
    "tlsh": ("get_tlsh", "tlsh", True),
    "telfhash": ("get_telfhash", "telfhash", True),
    "gimphash": ("get_gimphash", "gimphash", True),
    "icon_dhash": ("get_dhash_icon", "dhash_icon", True),
    "yara_rule": ("get_yarainfo", "yara_rule", True),
    "issuer_cn": ("get_issuerinfo", "issuer_cn", False),
    "subject_cn": ("get_subjectinfo", "subject_cn", False),
    "certificate_serial": ("get_certificate", "serial_number", False),
}

# ThreatFox reference datasets exposed by list_abusech_threatfox_reference.
_TF_REFERENCE_QUERIES: dict[str, str] = {
    "threat_types": "types",
    "malware_list": "malware_list",
    "tag_list": "tag_list",
}


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build abuse.ch tools from the plain Auth-Key token.

    The tools always build (the catalog introspects providers with a dummy
    token); a missing/blank key is surfaced as a call-time error instead.
    """
    auth_key = (token or "").strip()
    headers = build_abusech_headers(auth_key)

    def _client(timeout: int = _DEFAULT_TIMEOUT) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    def _credentials_error() -> str | None:
        if not auth_key:
            return (
                "Error: abuse.ch credentials are not configured - set the "
                "Auth-Key on the integration (free from auth.abuse.ch) and "
                "test the connection."
            )
        return None

    async def _mb_query(
        action: str, data: dict[str, Any], *, empty_msg: str
    ) -> tuple[dict, str | None]:
        """POST form data to the MalwareBazaar API -> (payload, err)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                MALWAREBAZAAR_API,
                _PROVIDER,
                action,
                headers=headers,
                data=data,
            )
            if err:
                return {}, err
            payload, parse_err = _parse_json_body(resp, "MalwareBazaar", action)
            if parse_err:
                return {}, parse_err
            return _body_status_error(
                payload, "MalwareBazaar", action, empty_msg=empty_msg
            )

    async def _urlhaus_post(
        path: str, action: str, data: dict[str, Any], *, empty_msg: str
    ) -> tuple[dict, str | None]:
        """POST form data to a URLhaus endpoint -> (payload, err)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{URLHAUS_API}{path}",
                _PROVIDER,
                action,
                headers=headers,
                data=data,
            )
            if err:
                return {}, err
            payload, parse_err = _parse_json_body(resp, "URLhaus", action)
            if parse_err:
                return {}, parse_err
            return _body_status_error(
                payload, "URLhaus", action, empty_msg=empty_msg
            )

    async def _tf_query(
        action: str, body: dict[str, Any], *, empty_msg: str
    ) -> tuple[dict, str | None]:
        """POST a JSON query to the ThreatFox API -> (payload, err)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                THREATFOX_API,
                _PROVIDER,
                action,
                headers=headers,
                json=body,
            )
            if err:
                return {}, err
            payload, parse_err = _parse_json_body(resp, "ThreatFox", action)
            if parse_err:
                return {}, parse_err
            return _body_status_error(
                payload, "ThreatFox", action, empty_msg=empty_msg
            )

    # ══════════════════════════════════════════════════════════════════════
    # 1. MALWAREBAZAAR (malware sample intelligence)
    # ══════════════════════════════════════════════════════════════════════

    async def lookup_abusech_malware_hash(hash: str) -> str:
        """Look up a file hash on MalwareBazaar (full sample dossier)."""
        if err := _credentials_error():
            return err
        hash = (hash or "").strip()
        if not hash:
            return "Error: hash is required (a SHA256, SHA1 or MD5 hash)."
        if not _is_valid_hash(hash):
            return (
                f"Error: '{_trunc(hash, 80)}' is not a valid SHA256, SHA1 or "
                "MD5 hash."
            )
        payload, err = await _mb_query(
            f"look up hash {hash}",
            {"query": "get_info", "hash": hash},
            empty_msg=f"Hash {hash} is not known to MalwareBazaar.",
        )
        if err:
            return err
        entries = _as_list(payload.get("data"))
        if not entries:
            return f"Hash {hash} is not known to MalwareBazaar."
        s = entries[0] if isinstance(entries[0], dict) else {}
        lines = [
            f"MalwareBazaar sample {s.get('sha256_hash', hash)}",
            f"Family: {s.get('signature') or 'unclassified'} | "
            f"File: {_trunc(s.get('file_name'), 80) or '?'} "
            f"({s.get('file_type') or '?'}, {s.get('file_size') or '?'} bytes, "
            f"{s.get('file_type_mime') or '?'})",
            f"First seen: {s.get('first_seen') or '?'} | "
            f"Last seen: {s.get('last_seen') or 'never'} | "
            f"Reporter: {s.get('reporter') or '?'} "
            f"[{s.get('origin_country') or '?'}]",
            f"Hashes: MD5 {s.get('md5_hash') or '?'} | SHA1 {s.get('sha1_hash') or '?'}",
        ]
        extra_hashes = [
            f"{label} {s.get(key)}"
            for label, key in (
                ("imphash", "imphash"),
                ("tlsh", "tlsh"),
                ("telfhash", "telfhash"),
                ("gimphash", "gimphash"),
                ("ssdeep", "ssdeep"),
                ("icon dhash", "dhash_icon"),
            )
            if s.get(key)
        ]
        if extra_hashes:
            lines.append("Similarity hashes: " + " | ".join(extra_hashes))
        tags = _tags(s)
        if tags:
            lines.append(f"Tags: {tags}")
        if s.get("delivery_method"):
            lines.append(f"Delivery method: {s.get('delivery_method')}")
        code_sign = s.get("code_sign") or []
        if isinstance(code_sign, list) and code_sign:
            cert = code_sign[0] if isinstance(code_sign[0], dict) else {}
            cscb = ""
            if cert.get("cscb_listed") in (True, "True", "true"):
                cscb = f" | CSCB BLOCKLISTED ({cert.get('cscb_reason') or '?'})"
            lines.append(
                f"Code signing: subject '{_trunc(cert.get('subject_cn'), 60)}' "
                f"issued by '{_trunc(cert.get('issuer_cn'), 60)}' "
                f"(serial {cert.get('serial_number') or '?'}){cscb}"
            )
        yara_rules = s.get("yara_rules") or []
        if isinstance(yara_rules, list) and yara_rules:
            names = [
                str((r or {}).get("rule_name") or "?")
                for r in yara_rules[:8]
                if isinstance(r, dict)
            ]
            more = f" (+{len(yara_rules) - 8} more)" if len(yara_rules) > 8 else ""
            lines.append(f"YARA rules: {', '.join(names)}{more}")
        vendor_intel = s.get("vendor_intel") or {}
        if isinstance(vendor_intel, dict) and vendor_intel:
            lines.append(
                f"Vendor sandbox intel available from: "
                f"{', '.join(sorted(vendor_intel.keys())[:10])}"
            )
        intelligence = s.get("intelligence") or {}
        if isinstance(intelligence, dict):
            clamav = intelligence.get("clamav")
            if clamav:
                if isinstance(clamav, list):
                    clamav = ", ".join(str(c) for c in clamav[:5])
                lines.append(f"ClamAV: {_trunc(clamav, 200)}")
        lines.append(
            f"Reference: https://bazaar.abuse.ch/sample/{s.get('sha256_hash', hash)}/"
        )
        return "\n".join(lines)

    async def list_abusech_recent_malware(time_window: bool = True) -> str:
        """List the newest malware samples added to MalwareBazaar."""
        if err := _credentials_error():
            return err
        selector = "time" if time_window else "100"
        window = "past 60 minutes" if time_window else "latest 100 additions"
        payload, err = await _mb_query(
            "list recent malware samples",
            {"query": "get_recent", "selector": selector},
            empty_msg=f"No malware samples on MalwareBazaar in the {window}.",
        )
        if err:
            return err
        entries = _as_list(payload.get("data"))
        shown = entries[:_DISPLAY_MAX]
        return format_entity_list(
            shown,
            _format_sample_line,
            noun="recent malware sample",
            total=len(entries),
            empty_msg=f"No malware samples in the {window}.",
        ) + _more_results_hint(
            len(entries),
            len(shown),
            _DISPLAY_MAX,
            narrow_hint="use search_abusech_malware to target a tag or family",
        ) + f"\n[Window: {window}]"

    async def search_abusech_malware(
        search_by: str,
        value: str,
        limit: int = 25,
    ) -> str:
        """Search MalwareBazaar samples by tag/signature/filetype/hash-family/certificate."""
        if err := _credentials_error():
            return err
        search_by = (search_by or "").strip().lower()
        selector = _MB_SEARCH_SELECTORS.get(search_by)
        if selector is None:
            return (
                f"Error: '{search_by}' is not a valid search_by value. Use one "
                f"of: {', '.join(sorted(_MB_SEARCH_SELECTORS))}."
            )
        value = (value or "").strip()
        if not value:
            return "Error: value is required (the term to search for)."
        query, param, supports_limit = selector
        data: dict[str, Any] = {"query": query, param: value}
        limit = _clamp(limit, 1, _API_MAX_LIMIT, 25)
        if supports_limit:
            data["limit"] = limit
        payload, err = await _mb_query(
            f"search samples by {search_by} '{_trunc(value, 60)}'",
            data,
            empty_msg=f"No MalwareBazaar samples found for {search_by} "
            f"'{_trunc(value, 60)}'.",
        )
        if err:
            return err
        entries = _as_list(payload.get("data"))
        shown = entries[: min(limit, _DISPLAY_MAX)]
        out = format_entity_list(
            shown,
            _format_sample_line,
            noun="malware sample",
            total=len(entries),
            empty_msg=f"No MalwareBazaar samples found for {search_by} "
            f"'{_trunc(value, 60)}'.",
        )
        return out + _more_results_hint(len(entries), len(shown), limit)

    async def comment_abusech_sample(sha256_hash: str, comment: str) -> str:
        """Add a public comment to a MalwareBazaar sample (write)."""
        if err := _credentials_error():
            return err
        sha256_hash = (sha256_hash or "").strip()
        if not sha256_hash:
            return "Error: sha256_hash is required (the sample's SHA256 hash)."
        if not _is_valid_sha256(sha256_hash):
            return (
                f"Error: '{_trunc(sha256_hash, 80)}' is not a valid SHA256 "
                "hash (comments require the sample's SHA256)."
            )
        comment = (comment or "").strip()
        if not comment:
            return "Error: comment is required (the text to publish)."
        _payload, err = await _mb_query(
            f"comment on sample {sha256_hash}",
            {"query": "add_comment", "sha256_hash": sha256_hash, "comment": comment},
            empty_msg=f"Hash {sha256_hash} is not known to MalwareBazaar.",
        )
        if err:
            return err
        # add_comment answers query_status "success" (covered by _OK_STATUSES).
        return (
            f"Comment published on MalwareBazaar sample {sha256_hash}: "
            f"{_trunc(comment, 160)}"
        )

    # ══════════════════════════════════════════════════════════════════════
    # 2. URLHAUS (malicious URL intelligence)
    # ══════════════════════════════════════════════════════════════════════

    async def lookup_abusech_url(url: str) -> str:
        """Look up a URL on URLhaus (malware-distribution status + payloads)."""
        if err := _credentials_error():
            return err
        url = (url or "").strip()
        if not url:
            return "Error: url is required (the full URL to look up)."
        payload, err = await _urlhaus_post(
            "/url/",
            f"look up URL {_trunc(url, 80)}",
            {"url": url},
            empty_msg=f"URL {_trunc(url, 120)} is not known to URLhaus.",
        )
        if err:
            return err
        lines = [
            f"URLhaus entry {payload.get('id', '?')}: {_trunc(payload.get('url') or url, 160)}",
            f"Status: {payload.get('url_status') or '?'} | "
            f"Threat: {payload.get('threat') or '?'} | "
            f"Host: {payload.get('host') or '?'}",
            f"Added: {payload.get('date_added') or '?'} | "
            f"Last online: {payload.get('last_online') or 'n/a'} | "
            f"Reporter: {payload.get('reporter') or '?'} | "
            f"Reported to hoster: {payload.get('larted') or '?'}",
        ]
        blacklists = _format_blacklists(payload)
        if blacklists:
            lines.append(f"Blacklists: {blacklists}")
        tags = _tags(payload)
        if tags:
            lines.append(f"Tags: {tags}")
        payloads = _as_list(payload.get("payloads"))
        if payloads:
            lines.append(f"\nPayloads served ({min(len(payloads), 10)} of {len(payloads)}):")
            for p in payloads[:10]:
                sig = p.get("signature") or "unclassified"
                lines.append(
                    f"  {p.get('response_sha256', '?')} | {sig} | "
                    f"{p.get('file_type') or '?'} | "
                    f"{_trunc(p.get('filename'), 60) or 'no filename'} | "
                    f"first seen {p.get('firstseen') or '?'}"
                )
        if payload.get("urlhaus_reference"):
            lines.append(f"Reference: {payload.get('urlhaus_reference')}")
        return "\n".join(lines)

    async def lookup_abusech_host(host: str) -> str:
        """Look up a host (IP/domain) on URLhaus (malware URL history)."""
        if err := _credentials_error():
            return err
        host = (host or "").strip()
        if not host:
            return (
                "Error: host is required (an IPv4 address, hostname or "
                "domain name)."
            )
        payload, err = await _urlhaus_post(
            "/host/",
            f"look up host {_trunc(host, 60)}",
            {"host": host},
            empty_msg=f"Host {host} is not known to URLhaus - no malware URLs "
            "on record.",
        )
        if err:
            return err
        lines = [
            f"URLhaus host {payload.get('host') or host}",
            f"First seen: {payload.get('firstseen') or '?'} | "
            f"Malware URLs on record: {payload.get('url_count') or '?'}",
        ]
        blacklists = _format_blacklists(payload)
        if blacklists:
            lines.append(f"Blacklists: {blacklists}")
        urls = _as_list(payload.get("urls"))
        if urls:
            lines.append(f"\nMalware URLs ({min(len(urls), 15)} of {len(urls)}):")
            lines.extend(_format_url_line(u) for u in urls[:15])
        if payload.get("urlhaus_reference"):
            lines.append(f"Reference: {payload.get('urlhaus_reference')}")
        return "\n".join(lines)

    async def lookup_abusech_payload(hash: str) -> str:
        """Look up a payload hash on URLhaus (distribution URLs + AV detection)."""
        if err := _credentials_error():
            return err
        hash = (hash or "").strip()
        if not hash:
            return "Error: hash is required (an MD5 or SHA256 hash)."
        if not _is_valid_hash(hash) or _SHA1_RE.fullmatch(hash):
            return (
                f"Error: '{_trunc(hash, 80)}' is not a valid MD5 or SHA256 "
                "hash (URLhaus payload lookups accept MD5 or SHA256 only)."
            )
        param = "sha256_hash" if _is_valid_sha256(hash) else "md5_hash"
        payload, err = await _urlhaus_post(
            "/payload/",
            f"look up payload {hash}",
            {param: hash},
            empty_msg=f"Payload {hash} is not known to URLhaus.",
        )
        if err:
            return err
        vt = payload.get("virustotal") or {}
        vt_line = (
            f"VirusTotal: {vt.get('result') or '?'} ({vt.get('percent') or '?'}%)"
            if isinstance(vt, dict) and vt
            else "VirusTotal: n/a"
        )
        lines = [
            f"URLhaus payload {payload.get('sha256_hash') or hash}",
            f"Family: {payload.get('signature') or 'unclassified'} | "
            f"Type: {payload.get('file_type') or '?'} | "
            f"Size: {payload.get('file_size') or '?'} bytes",
            f"First seen: {payload.get('firstseen') or '?'} | "
            f"Last seen: {payload.get('lastseen') or 'n/a'} | "
            f"Distribution URLs: {payload.get('url_count') or '?'}",
            f"MD5: {payload.get('md5_hash') or '?'} | {vt_line}",
        ]
        urls = _as_list(payload.get("urls"))
        if urls:
            lines.append(f"\nDistribution URLs ({min(len(urls), 10)} of {len(urls)}):")
            for u in urls[:10]:
                lines.append(
                    f"  {_trunc(u.get('url'), 120) or '?'} | "
                    f"{u.get('url_status') or '?'} | "
                    f"{_trunc(u.get('filename'), 50) or 'no filename'} | "
                    f"id {u.get('url_id', '?')}"
                )
        return "\n".join(lines)

    async def list_abusech_recent_urls(limit: int = 25) -> str:
        """List the newest malicious URLs added to URLhaus (past 3 days)."""
        if err := _credentials_error():
            return err
        limit = _clamp(limit, 1, _API_MAX_LIMIT, 25)
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{URLHAUS_API}/urls/recent/limit/{limit}/",
                _PROVIDER,
                "list recent malicious URLs",
                headers=headers,
            )
            if err:
                return err
            parsed, parse_err = _parse_json_body(
                resp, "URLhaus", "list recent malicious URLs"
            )
            if parse_err:
                return parse_err
            payload, body_err = _body_status_error(
                parsed,
                "URLhaus",
                "list recent malicious URLs",
                empty_msg="No recent malicious URLs on URLhaus (past 3 days).",
            )
        if body_err:
            return body_err
        urls = _as_list(payload.get("urls"))
        shown = urls[:_DISPLAY_MAX]
        return format_entity_list(
            shown,
            _format_url_line,
            noun="recent malicious URL",
            total=len(urls),
            empty_msg="No recent malicious URLs on URLhaus (past 3 days).",
        ) + _more_results_hint(
            len(urls),
            len(shown),
            limit,
            narrow_hint="use search_abusech_urls to target a tag or family",
        )

    async def list_abusech_recent_payloads(limit: int = 25) -> str:
        """List the newest malware payloads seen by URLhaus (past 3 days)."""
        if err := _credentials_error():
            return err
        limit = _clamp(limit, 1, _API_MAX_LIMIT, 25)
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{URLHAUS_API}/payloads/recent/limit/{limit}/",
                _PROVIDER,
                "list recent payloads",
                headers=headers,
            )
            if err:
                return err
            parsed, parse_err = _parse_json_body(
                resp, "URLhaus", "list recent payloads"
            )
            if parse_err:
                return parse_err
            payload, body_err = _body_status_error(
                parsed,
                "URLhaus",
                "list recent payloads",
                empty_msg="No recent payloads on URLhaus (past 3 days).",
            )
        if body_err:
            return body_err
        payloads = _as_list(payload.get("payloads"))
        shown = payloads[:_DISPLAY_MAX]
        return format_entity_list(
            shown,
            _format_payload_line,
            noun="recent payload",
            total=len(payloads),
            empty_msg="No recent payloads on URLhaus (past 3 days).",
        ) + _more_results_hint(
            len(payloads),
            len(shown),
            limit,
            narrow_hint="use search_abusech_urls to target a tag or family",
        )

    async def search_abusech_urls(search_by: str, value: str) -> str:
        """Search URLhaus malware URLs by tag or malware family signature."""
        if err := _credentials_error():
            return err
        search_by = (search_by or "").strip().lower()
        if search_by not in ("tag", "signature"):
            return "Error: search_by must be 'tag' or 'signature'."
        value = (value or "").strip()
        if not value:
            return "Error: value is required (the tag or signature to search)."
        payload, err = await _urlhaus_post(
            f"/{search_by}/",
            f"search URLs by {search_by} '{_trunc(value, 60)}'",
            {search_by: value},
            empty_msg=f"No URLhaus URLs found for {search_by} "
            f"'{_trunc(value, 60)}'.",
        )
        if err:
            return err
        urls = _as_list(payload.get("urls"))
        header = [
            f"URLhaus {search_by} '{value}': "
            f"first seen {payload.get('firstseen') or '?'}, "
            f"last seen {payload.get('lastseen') or 'n/a'}, "
            f"{payload.get('url_count') or len(urls)} URL(s) on record"
        ]
        if search_by == "signature" and payload.get("payload_count"):
            header[0] += f", {payload.get('payload_count')} payload(s)"
        shown = urls[:_DISPLAY_MAX]
        body = format_entity_list(
            shown,
            _format_url_line,
            noun="malware URL",
            total=len(urls),
            empty_msg=f"No URLhaus URLs found for {search_by} '{value}'.",
        )
        # This endpoint has no limit parameter - the display cap is the
        # only truncation, so the hint always points at narrowing.
        return "\n".join(header) + "\n" + body + _more_results_hint(
            len(urls), len(shown), _DISPLAY_MAX
        )

    # ══════════════════════════════════════════════════════════════════════
    # 3. THREATFOX (IOC exchange)
    # ══════════════════════════════════════════════════════════════════════

    async def search_abusech_iocs(
        search_by: str,
        value: str,
        exact_match: bool = False,
        limit: int = 25,
    ) -> str:
        """Search ThreatFox IOCs by value, file hash, tag, or malware family."""
        if err := _credentials_error():
            return err
        search_by = (search_by or "").strip().lower()
        value = (value or "").strip()
        if not value:
            return "Error: value is required (the term to search for)."
        limit = _clamp(limit, 1, _API_MAX_LIMIT, 25)
        body: dict[str, Any]
        if search_by == "term":
            body = {"query": "search_ioc", "search_term": value}
            if exact_match:
                body["exact_match"] = True
        elif search_by == "hash":
            if not _is_valid_hash(value) or _SHA1_RE.fullmatch(value):
                return (
                    f"Error: '{_trunc(value, 80)}' is not a valid MD5 or "
                    "SHA256 hash (ThreatFox hash searches accept MD5 or "
                    "SHA256 only)."
                )
            body = {"query": "search_hash", "hash": value}
        elif search_by == "tag":
            body = {"query": "taginfo", "tag": value, "limit": limit}
        elif search_by == "malware":
            body = {"query": "malwareinfo", "malware": value, "limit": limit}
        else:
            return (
                "Error: search_by must be one of 'term' (IOC value wildcard "
                "search), 'hash' (MD5/SHA256 sample hash), 'tag', or "
                "'malware' (family name)."
            )
        payload, err = await _tf_query(
            f"search IOCs by {search_by} '{_trunc(value, 60)}'",
            body,
            empty_msg=f"No ThreatFox IOCs found for {search_by} "
            f"'{_trunc(value, 60)}'. Note: IOCs older than 6 months are "
            "expired and no longer served by the API.",
        )
        if err:
            return err
        entries = _as_list(payload.get("data"))
        shown = entries[: min(limit, _DISPLAY_MAX)]
        out = format_entity_list(
            shown,
            _format_ioc_line,
            noun="IOC",
            total=len(entries),
            empty_msg=f"No ThreatFox IOCs found for {search_by} "
            f"'{_trunc(value, 60)}'.",
        )
        out += _more_results_hint(len(entries), len(shown), limit)
        out += "\n[Use get_abusech_ioc with an IOC id for full detail incl. malware samples]"
        return out

    async def get_abusech_ioc(ioc_id: str) -> str:
        """Get a single ThreatFox IOC by id (full detail + malware samples)."""
        if err := _credentials_error():
            return err
        ioc_id = str(ioc_id or "").strip().lstrip("#")
        if not ioc_id.isdigit():
            return "Error: ioc_id must be a numeric ThreatFox IOC id (e.g. 41)."
        payload, err = await _tf_query(
            f"get IOC {ioc_id}",
            {"query": "ioc", "id": int(ioc_id)},
            empty_msg=f"ThreatFox IOC {ioc_id} was not found (it may be "
            "expired - IOCs older than 6 months are removed from the API).",
        )
        if err:
            return err
        data = payload.get("data")
        ioc = data[0] if isinstance(data, list) and data else data
        if not isinstance(ioc, dict) or not ioc:
            return f"ThreatFox IOC {ioc_id} was not found."
        lines = [
            f"ThreatFox IOC #{ioc.get('id', ioc_id)}: {_trunc(ioc.get('ioc'), 160) or '?'}",
            f"Type: {ioc.get('ioc_type') or '?'} ({_trunc(ioc.get('ioc_type_desc'), 100)})",
            f"Threat: {ioc.get('threat_type') or '?'} ({_trunc(ioc.get('threat_type_desc'), 100)})",
            f"Malware: {ioc.get('malware_printable') or ioc.get('malware') or 'unknown'}"
            + (f" (alias: {_trunc(ioc.get('malware_alias'), 80)})" if ioc.get("malware_alias") else ""),
            f"Confidence: {ioc.get('confidence_level', '?')}/100 | "
            f"First seen: {str(ioc.get('first_seen', ''))[:19] or '?'} | "
            f"Last seen: {str(ioc.get('last_seen') or '')[:19] or 'n/a'} | "
            f"Reporter: {ioc.get('reporter') or '?'}",
        ]
        if ioc.get("reference"):
            lines.append(f"Reference: {_trunc(ioc.get('reference'), 160)}")
        if ioc.get("comment"):
            lines.append(f"Comment: {_trunc(ioc.get('comment'), 200)}")
        tags = _tags(ioc)
        if tags:
            lines.append(f"Tags: {tags}")
        if ioc.get("malware_malpedia"):
            lines.append(f"Malpedia: {ioc.get('malware_malpedia')}")
        samples = _as_list(ioc.get("malware_samples"))
        if samples:
            lines.append(f"\nAssociated malware samples ({min(len(samples), 10)} of {len(samples)}):")
            for s in samples[:10]:
                lines.append(
                    f"  {s.get('sha256_hash', '?')} "
                    f"[{str(s.get('time_stamp', ''))[:16] or '?'}]"
                )
        return "\n".join(lines)

    async def list_abusech_recent_iocs(days: int = 1, limit: int = 50) -> str:
        """List IOCs recently added to ThreatFox (1-7 days back)."""
        if err := _credentials_error():
            return err
        days = _clamp(days, 1, 7, 1)
        limit = _clamp(limit, 1, _API_MAX_LIMIT, 50)
        # ThreatFox's get_iocs endpoint only accepts `days` - it has no
        # server-side limit parameter, so the whole window is returned and
        # `limit` only trims the displayed rows (documented in the schema).
        payload, err = await _tf_query(
            f"list IOCs from the last {days} day(s)",
            {"query": "get_iocs", "days": days},
            empty_msg=f"No IOCs were added to ThreatFox in the last {days} day(s).",
        )
        if err:
            return err
        entries = _as_list(payload.get("data"))
        shown = entries[: min(limit, _DISPLAY_MAX)]
        out = format_entity_list(
            shown,
            _format_ioc_line,
            noun="recent IOC",
            total=len(entries),
            empty_msg=f"No IOCs were added to ThreatFox in the last {days} day(s).",
        )
        return out + _more_results_hint(
            len(entries), len(shown), limit, narrow_hint="narrow days"
        )

    async def resolve_abusech_malware_label(
        malware: str, platform: str = ""
    ) -> str:
        """Resolve a free-form malware name to its canonical Malpedia label."""
        if err := _credentials_error():
            return err
        malware = (malware or "").strip()
        if not malware:
            return "Error: malware is required (e.g. 'warzone')."
        body: dict[str, Any] = {"query": "get_label", "malware": malware}
        platform = (platform or "").strip().lower()
        if platform:
            if platform not in ("win", "osx", "apk", "jar", "elf"):
                return "Error: platform must be one of win, osx, apk, jar, elf."
            body["platform"] = platform
        payload, err = await _tf_query(
            f"resolve malware label '{_trunc(malware, 60)}'",
            body,
            empty_msg=f"No Malpedia label matches '{_trunc(malware, 60)}'. "
            "Try list_abusech_threatfox_reference with reference='malware_list'.",
        )
        if err:
            return err
        entries = _as_list(payload.get("data"))
        if not entries:
            return f"No Malpedia label matches '{malware}'."

        def _fmt(m: dict) -> str:
            alias = f" | aliases: {_trunc(m.get('malware_alias'), 100)}" if m.get("malware_alias") else ""
            return (
                f"  {m.get('malware', '?')} "
                f"({m.get('malware_printable') or '?'}){alias}"
            )

        return format_entity_list(
            entries[:20],
            _fmt,
            noun="matching malware label",
            empty_msg=f"No Malpedia label matches '{malware}'.",
        ) + "\n[Use the first column (e.g. win.ave_maria) as the 'malware' value when submitting IOCs]"

    async def list_abusech_threatfox_reference(
        reference: str, filter: str = "", limit: int = 50
    ) -> str:
        """Browse ThreatFox reference data (threat types, malware list, tags)."""
        if err := _credentials_error():
            return err
        reference = (reference or "").strip().lower()
        query = _TF_REFERENCE_QUERIES.get(reference)
        if query is None:
            return (
                "Error: reference must be one of "
                f"{', '.join(sorted(_TF_REFERENCE_QUERIES))}."
            )
        limit = _clamp(limit, 1, 500, 50)
        filter_term = (filter or "").strip().lower()
        payload, err = await _tf_query(
            f"list ThreatFox {reference}",
            {"query": query},
            empty_msg=f"ThreatFox returned no {reference} data.",
        )
        if err:
            return err
        data = payload.get("data") or {}
        lines: list[str] = []
        if reference == "threat_types" and isinstance(data, dict):
            for _key, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                text = (
                    f"  ioc_type={entry.get('ioc_type', '?')} | "
                    f"threat_type={entry.get('fk_threat_type', '?')} | "
                    f"{_trunc(entry.get('description'), 110)}"
                )
                if filter_term and filter_term not in text.lower():
                    continue
                lines.append(text)
        elif reference == "malware_list" and isinstance(data, dict):
            for label, entry in data.items():
                entry = entry if isinstance(entry, dict) else {}
                text = f"  {label} ({entry.get('malware_printable') or '?'})"
                if entry.get("malware_alias"):
                    text += f" | aliases: {_trunc(entry.get('malware_alias'), 80)}"
                if filter_term and filter_term not in text.lower():
                    continue
                lines.append(text)
        elif reference == "tag_list" and isinstance(data, dict):
            for tag, entry in data.items():
                entry = entry if isinstance(entry, dict) else {}
                text = (
                    f"  {tag} | first seen {str(entry.get('first_seen', ''))[:10] or '?'} "
                    f"| last seen {str(entry.get('last_seen', ''))[:10] or '?'}"
                )
                if filter_term and filter_term not in text.lower():
                    continue
                lines.append(text)
        if not lines:
            suffix = f" matching '{filter_term}'" if filter_term else ""
            return f"No ThreatFox {reference} entries{suffix}."
        total = len(lines)
        shown = lines[:limit]
        header = f"ThreatFox {reference} ({len(shown)} of {total} shown"
        header += f", filter: '{filter_term}'):" if filter_term else "):"
        out = "\n".join([header, *shown])
        if total > len(shown):
            out += f"\n[{total - len(shown)} more entries - raise limit or add a filter]"
        return out

    async def submit_abusech_ioc(
        iocs: str,
        threat_type: str,
        ioc_type: str,
        malware: str,
        confidence_level: int = 50,
        reference: str = "",
        tags: str = "",
        comment: str = "",
        anonymous: bool = False,
    ) -> str:
        """Share (submit) vetted IOCs to ThreatFox (public write)."""
        if err := _credentials_error():
            return err
        ioc_list = [
            item.strip()
            for line in str(iocs or "").splitlines()
            for item in line.split(",")
            if item.strip()
        ]
        if not ioc_list:
            return (
                "Error: iocs is required - one or more IOC values, "
                "comma- or newline-separated (e.g. 'tooeviltoexist.com')."
            )
        threat_type = (threat_type or "").strip()
        ioc_type = (ioc_type or "").strip()
        malware = (malware or "").strip()
        if not threat_type or not ioc_type or not malware:
            return (
                "Error: threat_type, ioc_type and malware are all required. "
                "Use list_abusech_threatfox_reference (reference="
                "'threat_types') for valid threat_type/ioc_type combinations "
                "and resolve_abusech_malware_label for the canonical malware "
                "label (e.g. win.zloader)."
            )
        # Canonical Malpedia labels are always platform-prefixed
        # (win.zloader, elf.mirai, apk.cerberus, ...) - a dotless name is a
        # sure sign the caller skipped resolve_abusech_malware_label. The
        # valid threat_type/ioc_type combinations are dynamic reference data
        # served by the API's `types` query, so their validation is
        # deliberately left to the server (a local copy would drift).
        if "." not in malware:
            return (
                f"Error: '{_trunc(malware, 60)}' is not a canonical Malpedia "
                "label (expected platform.family, e.g. 'win.zloader'). Use "
                "resolve_abusech_malware_label to resolve the name first."
            )
        body: dict[str, Any] = {
            "query": "submit_ioc",
            "threat_type": threat_type,
            "ioc_type": ioc_type,
            "malware": malware,
            "confidence_level": _clamp(confidence_level, 0, 100, 50),
            "iocs": ioc_list,
            "anonymous": 1 if anonymous else 0,
        }
        reference = (reference or "").strip()
        if reference:
            body["reference"] = reference
        tag_list = [t.strip() for t in str(tags or "").split(",") if t.strip()]
        if tag_list:
            body["tags"] = tag_list
        comment = (comment or "").strip()
        if comment:
            body["comment"] = comment
        payload, err = await _tf_query(
            f"submit {len(ioc_list)} IOC(s)",
            body,
            empty_msg="ThreatFox did not accept any of the submitted IOCs.",
        )
        if err:
            return err
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            return f"Submitted {len(ioc_list)} IOC(s) to ThreatFox."
        accepted = _as_list(data.get("ok"))
        ignored = _as_list(data.get("ignored"))
        duplicated = _as_list(data.get("duplicated"))
        lines = [
            f"ThreatFox submission result: {len(accepted)} accepted, "
            f"{len(duplicated)} duplicate(s), {len(ignored)} ignored."
        ]
        if accepted:
            lines.append("Accepted: " + ", ".join(_trunc(i, 80) for i in accepted[:10]))
        if duplicated:
            lines.append("Duplicates: " + ", ".join(_trunc(i, 80) for i in duplicated[:10]))
        if ignored:
            lines.append("Ignored: " + ", ".join(_trunc(i, 80) for i in ignored[:10]))
        reward = data.get("reward")
        if reward is not None:
            lines.append(f"Credits earned: {reward}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("lookup_abusech_malware_hash", lookup_abusech_malware_hash)
    executor.register("list_abusech_recent_malware", list_abusech_recent_malware)
    executor.register("search_abusech_malware", search_abusech_malware)
    executor.register("comment_abusech_sample", comment_abusech_sample)
    executor.register("lookup_abusech_url", lookup_abusech_url)
    executor.register("lookup_abusech_host", lookup_abusech_host)
    executor.register("lookup_abusech_payload", lookup_abusech_payload)
    executor.register("list_abusech_recent_urls", list_abusech_recent_urls)
    executor.register("list_abusech_recent_payloads", list_abusech_recent_payloads)
    executor.register("search_abusech_urls", search_abusech_urls)
    executor.register("search_abusech_iocs", search_abusech_iocs)
    executor.register("get_abusech_ioc", get_abusech_ioc)
    executor.register("list_abusech_recent_iocs", list_abusech_recent_iocs)
    executor.register("resolve_abusech_malware_label", resolve_abusech_malware_label)
    executor.register(
        "list_abusech_threatfox_reference", list_abusech_threatfox_reference
    )
    executor.register("submit_abusech_ioc", submit_abusech_ioc)

    return [
        ToolDefinition(
            name="lookup_abusech_malware_hash",
            description=(
                "Look up a file hash (SHA256, SHA1 or MD5) on MalwareBazaar "
                "(abuse.ch). Returns the full sample dossier: malware family "
                "signature, file name/type/size, first/last seen, all hashes "
                "(incl. imphash/tlsh/telfhash/ssdeep for similarity pivots), "
                "tags, code-signing certificate + CSCB blocklist status, YARA "
                "rule matches, and which vendor sandboxes have intel. Use "
                "this FIRST when triaging a suspicious file hash. For the "
                "URLs that distributed a payload use lookup_abusech_payload; "
                "for IOCs extracted from a sample use search_abusech_iocs "
                "with search_by='hash'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": "SHA256, SHA1 or MD5 hash of the file.",
                    },
                },
                "required": ["hash"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_abusech_recent_malware",
            description=(
                "List the newest malware samples added to MalwareBazaar "
                "(abuse.ch): either everything from the past 60 minutes or "
                "the latest 100 additions. Use for fresh-malware situational "
                "awareness. Each line surfaces the SHA256 for follow-up with "
                "lookup_abusech_malware_hash."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "time_window": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "true = additions from the past 60 minutes; "
                            "false = the latest 100 additions regardless of age."
                        ),
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_abusech_malware",
            description=(
                "Search MalwareBazaar (abuse.ch) samples by a selector: tag "
                "(e.g. 'TrickBot'), signature (malware family, e.g. "
                "'AgentTesla'), file_type (e.g. 'elf'), clamav (ClamAV "
                "signature name), imphash / tlsh / telfhash / gimphash / "
                "icon_dhash (similarity-hash pivots to find related samples), "
                "yara_rule (rule name, e.g. 'win_remcos_g0'), or code-signing "
                "certificate attributes: issuer_cn / subject_cn / "
                "certificate_serial (hunt abused signing certs). Returns "
                "sample summaries with SHA256 hashes for follow-up."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search_by": {
                        "type": "string",
                        "enum": sorted(_MB_SEARCH_SELECTORS),
                        "description": "Which selector to search by.",
                    },
                    "value": {
                        "type": "string",
                        "description": (
                            "The value for the selector (e.g. tag 'TrickBot', "
                            "signature 'AgentTesla', file_type 'elf')."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": (
                            "Max results to display (1-1000; displayed "
                            "output is always capped at 100 rows). For "
                            "certificate selectors (issuer_cn/subject_cn/"
                            "certificate_serial) limit is not sent to the "
                            "API - which returns up to 100 samples - but it "
                            "still trims how many rows are displayed."
                        ),
                    },
                },
                "required": ["search_by", "value"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_abusech_url",
            description=(
                "Look up a URL on URLhaus (abuse.ch). Returns whether the URL "
                "is a known malware-distribution site, its online/offline "
                "status, threat class, host, Spamhaus DBL + SURBL blacklist "
                "status, tags, and the malware payloads it served (with "
                "SHA256 hashes for follow-up via lookup_abusech_malware_hash)."
                " Use this FIRST when triaging a suspicious URL. For all URLs "
                "on a host use lookup_abusech_host."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL to look up (incl. scheme).",
                    },
                },
                "required": ["url"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_abusech_host",
            description=(
                "Look up a host (IPv4 address, hostname or domain) on URLhaus "
                "(abuse.ch). Returns when it was first seen, how many malware "
                "URLs it has hosted, blacklist status, and the most recent "
                "malware URLs observed on it. Use to assess whether a host / "
                "domain is known malware-distribution infrastructure. For C2 "
                "infrastructure checks also run search_abusech_iocs with "
                "search_by='term'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "IPv4 address, hostname or domain (case insensitive).",
                    },
                },
                "required": ["host"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_abusech_payload",
            description=(
                "Look up a payload (malware file) by MD5 or SHA256 hash on "
                "URLhaus (abuse.ch). Returns the malware family, file "
                "type/size, VirusTotal detection ratio, and the malware URLs "
                "that distributed this payload. Complements "
                "lookup_abusech_malware_hash (sample dossier) with the "
                "distribution-infrastructure view."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": "MD5 or SHA256 hash of the payload.",
                    },
                },
                "required": ["hash"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_abusech_recent_urls",
            description=(
                "List the newest malicious URLs added to URLhaus (abuse.ch) "
                "in the past 3 days (max 1000). Use for fresh "
                "malware-distribution situational awareness and blocklist "
                "seeding. Each line surfaces the URLhaus id and status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": (
                            "Max results to fetch (1-1000). Displayed output "
                            "is capped at 100 rows."
                        ),
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_abusech_recent_payloads",
            description=(
                "List the newest malware payloads seen by URLhaus (abuse.ch) "
                "in the past 3 days (max 1000), with malware family, file "
                "type and VirusTotal detection percentage. Each line surfaces "
                "the SHA256 for follow-up via lookup_abusech_payload or "
                "lookup_abusech_malware_hash."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": (
                            "Max results to fetch (1-1000). Displayed output "
                            "is capped at 100 rows."
                        ),
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_abusech_urls",
            description=(
                "Search URLhaus (abuse.ch) malware URLs by tag (e.g. "
                "'emotet', reporter-assigned) or by signature (malware family "
                "identified by URLhaus from the served payload, e.g. 'Gozi' - "
                "not influenceable by reporters, so higher fidelity). Returns "
                "up to 1000 URLs with status and ids."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search_by": {
                        "type": "string",
                        "enum": ["tag", "signature"],
                        "description": "Search by reporter tag or by payload malware family.",
                    },
                    "value": {
                        "type": "string",
                        "description": "The tag or signature to search for (case insensitive).",
                    },
                },
                "required": ["search_by", "value"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_abusech_iocs",
            description=(
                "Search ThreatFox (abuse.ch) IOCs - C2 IP:port pairs, "
                "domains, URLs and hashes shared by the community. search_by: "
                "'term' = wildcard search on the IOC value (set "
                "exact_match=true for exact; e.g. an IP, domain, or URL "
                "fragment - the go-to C2 infrastructure check), 'hash' = find "
                "IOCs associated with a malware sample (MD5/SHA256), 'tag' = "
                "IOCs carrying a tag (e.g. 'Magecart'), 'malware' = IOCs of a "
                "malware family (use resolve_abusech_malware_label for the "
                "canonical name). Note: IOCs older than 6 months are expired "
                "and not returned."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search_by": {
                        "type": "string",
                        "enum": ["term", "hash", "tag", "malware"],
                        "description": "Which search mode to use.",
                    },
                    "value": {
                        "type": "string",
                        "description": (
                            "The search value (IOC value/fragment, MD5/SHA256 "
                            "hash, tag, or malware family name)."
                        ),
                    },
                    "exact_match": {
                        "type": "boolean",
                        "default": False,
                        "description": "For search_by='term': exact IOC match instead of wildcard.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": (
                            "Max results (1-1000; sent to the API for "
                            "tag/malware searches). Displayed output is "
                            "capped at 100 rows."
                        ),
                    },
                },
                "required": ["search_by", "value"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_abusech_ioc",
            description=(
                "Get one ThreatFox (abuse.ch) IOC by its numeric id. Returns "
                "the full record: IOC value, IOC/threat type with "
                "descriptions, malware family + aliases + Malpedia link, "
                "confidence level, reporter, reference, comment, tags, and "
                "the associated malware samples (SHA256 hashes for follow-up "
                "via lookup_abusech_malware_hash). Get ids from "
                "search_abusech_iocs or list_abusech_recent_iocs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ioc_id": {
                        "type": "string",
                        "description": "Numeric ThreatFox IOC id (e.g. '41').",
                    },
                },
                "required": ["ioc_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_abusech_recent_iocs",
            description=(
                "List IOCs recently added to ThreatFox (abuse.ch), filtered "
                "by first_seen over the last 1-7 days. Use for fresh C2 / "
                "payload-delivery infrastructure awareness and feed-style "
                "consumption. The API caps the window at 7 days."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "default": 1,
                        "description": "Days to look back (1-7).",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": (
                            "Max IOCs to display (1-1000; capped at 100 "
                            "displayed rows). The ThreatFox get_iocs API has "
                            "no server-side limit - the full days window is "
                            "fetched and limit only trims the displayed "
                            "rows; narrow days to reduce the fetch."
                        ),
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="resolve_abusech_malware_label",
            description=(
                "Resolve a free-form malware name (e.g. 'warzone') to the "
                "canonical Malpedia label ThreatFox uses (e.g. "
                "'win.ave_maria'), incl. printable name and aliases. Use "
                "BEFORE search_abusech_iocs with search_by='malware' and "
                "before submit_abusech_ioc, which both require the canonical "
                "label."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "malware": {
                        "type": "string",
                        "description": "Malware name to resolve (e.g. 'warzone').",
                    },
                    "platform": {
                        "type": "string",
                        "enum": ["win", "osx", "apk", "jar", "elf"],
                        "description": "Optional platform filter.",
                    },
                },
                "required": ["malware"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_abusech_threatfox_reference",
            description=(
                "Browse ThreatFox (abuse.ch) reference data: 'threat_types' "
                "(the valid ioc_type / threat_type combinations required by "
                "submit_abusech_ioc), 'malware_list' (all supported Malpedia "
                "malware families - large, use the filter), or 'tag_list' "
                "(known tags). Local-ish reference reads; use the filter "
                "parameter to narrow the output."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "reference": {
                        "type": "string",
                        "enum": sorted(_TF_REFERENCE_QUERIES),
                        "description": "Which reference dataset to list.",
                    },
                    "filter": {
                        "type": "string",
                        "description": "Case-insensitive substring filter on the entries.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max entries to display (1-500).",
                    },
                },
                "required": ["reference"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="comment_abusech_sample",
            description=(
                "Add a public comment to a MalwareBazaar (abuse.ch) sample "
                "(WRITE - the comment is public and attributed to the "
                "Auth-Key's account). Requires the sample's SHA256 hash. Keep "
                "comments factual (analysis findings, C2 extraction results, "
                "attribution context) and strip any PII."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sha256_hash": {
                        "type": "string",
                        "description": "SHA256 hash of the sample to comment on.",
                    },
                    "comment": {
                        "type": "string",
                        "description": "The comment text (public).",
                    },
                },
                "required": ["sha256_hash", "comment"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="submit_abusech_ioc",
            description=(
                "Share (submit) one or more vetted IOCs to ThreatFox "
                "(abuse.ch) (WRITE - submissions are public and attributed to "
                "the Auth-Key's account unless anonymous=true). ONLY submit "
                "confirmed/vetted IOCs - repeated policy violations get "
                "accounts banned. threat_type + ioc_type must be a valid "
                "combination from list_abusech_threatfox_reference "
                "(reference='threat_types', e.g. threat_type 'botnet_cc' + "
                "ioc_type 'ip:port') and malware must be the canonical "
                "Malpedia label from resolve_abusech_malware_label (e.g. "
                "'win.zloader'). Returns accepted/duplicate/ignored counts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "iocs": {
                        "type": "string",
                        "description": (
                            "IOC value(s) to submit, comma- or newline-"
                            "separated (e.g. '198.51.100.7:443')."
                        ),
                    },
                    "threat_type": {
                        "type": "string",
                        "description": "Threat type (e.g. 'botnet_cc', 'payload_delivery').",
                    },
                    "ioc_type": {
                        "type": "string",
                        "description": "IOC type (e.g. 'ip:port', 'domain', 'url', 'sha256_hash').",
                    },
                    "malware": {
                        "type": "string",
                        "description": "Canonical Malpedia malware label (e.g. 'win.zloader').",
                    },
                    "confidence_level": {
                        "type": "integer",
                        "default": 50,
                        "description": "Confidence 0-100 (default 50).",
                    },
                    "reference": {
                        "type": "string",
                        "description": "Optional reference URL (e.g. a tweet or blog post).",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Optional comma-separated tags (chars: A-Za-z0-9.- and space).",
                    },
                    "comment": {
                        "type": "string",
                        "description": "Optional comment on the IOCs (public).",
                    },
                    "anonymous": {
                        "type": "boolean",
                        "default": False,
                        "description": "Submit anonymously (not attributed to your account).",
                    },
                },
                "required": ["iocs", "threat_type", "ioc_type", "malware"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
