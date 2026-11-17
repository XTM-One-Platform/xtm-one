# SPDX-License-Identifier: Apache-2.0

"""ThreatConnect integration – Agentic threat intelligence platform (TIP).

Full-fledged ThreatConnect v3 REST API integration covering the threat
intelligence production → enrichment → workflow → automation loop for an
agentic security team:

  - Owners: Organizations / Communities / Sources the API user can access
  - Indicators (IOCs): list, get, create, update, delete (Address, Host,
    URL, File, EmailAddress, ASN, CIDR, Mutex, …) with Threat/Confidence
    ratings, tags, security labels, and false-positive flagging
  - Groups (intel objects): list, get, create, update, delete (Incident,
    Adversary, Campaign, Threat, Report, Email, Event, Malware, …)
  - Workflow Cases: list, get, create, update, delete plus notes, artifacts,
    tasks, and workflow templates (consolidated manage tool)
  - Tags (standard + MITRE ATT&CK technique tags) and Security Labels (TLP)
  - Victims and Victim Assets (consolidated manage tool)
  - Associations: link/unlink indicators ↔ groups ↔ cases ↔ artifacts
  - Attributes: list/add/update/delete attributes on indicators/groups/cases
  - Playbooks: inventory, executions, activate/deactivate
  - Raw TQL power-search across every v3 object type

Authentication
--------------
ThreatConnect signs every request with **HMAC-SHA256** — there is no bearer
token. Each request carries two headers:

  - ``Timestamp``: Unix epoch seconds (must be within 5 minutes of the
    server's clock)
  - ``Authorization: TC <access_id>:<signature>`` where ``signature`` is
    ``base64(HMAC_SHA256(secret_key, "<path+query>:<METHOD>:<timestamp>"))``
    computed over the URL-encoded API path (including the ``/api`` prefix and
    the query string exactly as sent).

``integration_tools._get_token`` assembles the compound
``base_url||access_id||secret_key`` token this builder parses. The base URL is
the ThreatConnect instance URL (Public Cloud ``https://app.threatconnect.com``
or a Dedicated Cloud / On-Prem host); the API lives at ``{base_url}/api``.
Because the signature covers the exact encoded path + query, every request
builds its full URL (query already percent-encoded) before signing, and the
same pre-encoded URL string is handed to httpx verbatim.

Exports ``threatconnect_api_base()`` / ``build_tc_headers()`` /
``validate_connection()`` shared with the connectivity test.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json as _json
import logging
import time
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from app.core.egress import egress_client
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "ThreatConnect"
_SOURCE = "integration:threatconnect"
_DEFAULT_TIMEOUT = 30

# v3 pagination bounds (resultLimit default 100, hard max 10000).
_MAX_RESULT_LIMIT = 500

# Accepted Indicator types (v3 ``type`` field values).
_INDICATOR_TYPES: tuple[str, ...] = (
    "Address",
    "EmailAddress",
    "File",
    "Host",
    "URL",
    "ASN",
    "CIDR",
    "Email Subject",
    "Hashtag",
    "Mutex",
    "Registry Key",
    "User Agent",
)

# Indicator type → the JSON field carrying the observable value on creation.
# Field names per the v3 docs (Indicator-Specific Fields); File is handled
# separately (md5/sha1/sha256 by hash length) and Registry Key is the one
# multi-field type (Key Name + Value Name + Value Type via extra_fields).
_INDICATOR_VALUE_FIELDS: dict[str, str] = {
    "address": "ip",
    "emailaddress": "address",
    "host": "hostName",
    "url": "text",
    "asn": "AS Number",
    "cidr": "Block",
    "email subject": "Subject",
    "hashtag": "Hashtag",
    "mutex": "Mutex",
    "user agent": "User Agent String",
}

# Accepted Group types (v3 ``type`` field values).
_GROUP_TYPES: tuple[str, ...] = (
    "Adversary",
    "Attack Pattern",
    "Campaign",
    "Course of Action",
    "Document",
    "Email",
    "Event",
    "Incident",
    "Intrusion Set",
    "Malware",
    "Report",
    "Signature",
    "Tactic",
    "Task",
    "Threat",
    "Tool",
    "Vulnerability",
)

# Object type → v3 endpoint path for the raw TQL query tool.
_QUERY_ENDPOINTS: dict[str, str] = {
    "indicators": "/v3/indicators",
    "groups": "/v3/groups",
    "cases": "/v3/cases",
    "tags": "/v3/tags",
    "victims": "/v3/victims",
    "victim_assets": "/v3/victimAssets",
    "artifacts": "/v3/artifacts",
    "notes": "/v3/notes",
    "tasks": "/v3/tasks",
    "workflow_templates": "/v3/workflowTemplates",
    "playbooks": "/v3/playbooks",
    "playbook_executions": "/v3/playbookExecutions",
    "owners": "/v3/security/owners",
    "attribute_types": "/v3/attributeTypes",
    "security_labels": "/v3/securityLabels",
}

# Victim asset type → the JSON field carrying the asset value.
_VICTIM_ASSET_FIELDS: dict[str, str] = {
    "emailaddress": "address",
    "networkaccount": "accountName",
    "phone": "phone",
    "socialnetwork": "accountName",
    "website": "website",
}

# Attribute parent object type → (endpoint, parent id field).
_ATTRIBUTE_ENDPOINTS: dict[str, tuple[str, str]] = {
    "indicator": ("/v3/indicatorAttributes", "indicatorId"),
    "group": ("/v3/groupAttributes", "groupId"),
    "case": ("/v3/caseAttributes", "caseId"),
}

# Association target type → the PUT body field on the source object.
_ASSOCIATION_FIELDS: dict[str, str] = {
    "indicator": "associatedIndicators",
    "group": "associatedGroups",
    "case": "associatedCases",
    "artifact": "associatedArtifacts",
    "victim_asset": "associatedVictimAssets",
}

# Source object type → v3 endpoint for association updates.
_ASSOCIATION_SOURCES: dict[str, str] = {
    "indicator": "/v3/indicators",
    "group": "/v3/groups",
    "case": "/v3/cases",
    "victim": "/v3/victims",
}


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with the connectivity test)
# ---------------------------------------------------------------------------


def threatconnect_api_base(base_url: str) -> str:
    """Return the ThreatConnect API base URL (``{instance}/api``).

    Accepts the instance URL with or without a trailing ``/api`` (both are
    normalised to exactly one ``/api`` suffix) and strips whitespace and
    trailing slashes so a pasted URL can never produce a doubled path.
    Returns ``""`` when no base URL is configured.
    """
    cleaned = (base_url or "").strip().rstrip("/")
    if not cleaned:
        return ""
    if cleaned.lower().endswith("/api"):
        cleaned = cleaned[: -len("/api")]
    return f"{cleaned}/api"


def build_tc_headers(
    access_id: str,
    secret_key: str,
    method: str,
    path_with_query: str,
    timestamp: int | None = None,
) -> dict[str, str]:
    """Build the signed ThreatConnect ``Timestamp`` + ``Authorization`` headers.

    ``path_with_query`` is the URL-encoded API path exactly as sent, including
    the ``/api`` prefix and the query string (e.g.
    ``/api/v3/indicators?resultLimit=25``). The signature is
    ``base64(HMAC_SHA256(secret_key, "<path_with_query>:<METHOD>:<ts>"))``.
    Credentials are stripped so pasted whitespace can never corrupt the HMAC.
    """
    ts = int(timestamp if timestamp is not None else time.time())
    message = f"{path_with_query}:{method.upper()}:{ts}"
    digest = hmac.new(
        secret_key.strip().encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")
    return {
        "Timestamp": str(ts),
        "Authorization": f"TC {access_id.strip()}:{signature}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _encode_query(params: list[tuple[str, Any]] | None) -> str:
    """Percent-encode query params deterministically (spaces as ``%20``).

    The signature covers the query string exactly as sent, so the query is
    encoded once here and the resulting URL is handed to httpx verbatim —
    httpx preserves valid percent-encoding, keeping the signature aligned.
    """
    if not params:
        return ""
    pairs = [(k, str(v)) for k, v in params if v is not None and str(v) != ""]
    if not pairs:
        return ""
    return urlencode(pairs, quote_via=quote)


async def validate_connection(
    base_url: str, access_id: str, secret_key: str
) -> tuple[bool, str]:
    """Verify ThreatConnect credentials with a signed owners lookup.

    Returns ``(ok, message)``. A successful ``GET /v3/security/owners`` proves
    the instance URL, Access ID, and Secret Key are all valid. Used by the
    integration connectivity test. Routes through the shared ``safe_request``
    (429 retry + standardized errors) like every other provider call, with
    ``expected_statuses`` so an auth failure can be told apart from a network
    error, and rejects 2xx responses that still carry a v3 error envelope.
    """
    api = threatconnect_api_base(base_url)
    if not api:
        return False, "Missing base URL (ThreatConnect instance URL)."
    if not access_id or not secret_key:
        return False, "Missing Access ID or Secret Key."
    query = _encode_query([("resultLimit", 1)])
    path = f"{urlparse(api).path}/v3/security/owners?{query}"
    url = f"{api}/v3/security/owners?{query}"
    headers = build_tc_headers(access_id, secret_key, "GET", path)
    async with egress_client(timeout=15) as client:
        resp, err = await safe_request(
            client,
            "get",
            url,
            _PROVIDER,
            "validate credentials",
            headers=headers,
            expected_statuses={401, 403},
        )
        if resp is not None and resp.status_code in (401, 403):
            return False, (
                "Authentication failed — check the Access ID and Secret Key, "
                "and confirm the server clock skew is under 5 minutes."
            )
        if err or resp is None:
            return False, err or "Could not connect to ThreatConnect."
        try:
            payload = resp.json()
        except Exception:
            # A 2xx that is not JSON is not a ThreatConnect API response
            # (e.g. a proxy or SSO portal returning HTML) — never report
            # success on it.
            return False, (
                "Unexpected non-JSON response from the ThreatConnect API — "
                "check that the instance URL points at a ThreatConnect "
                "server (the API lives at {base_url}/api)."
            )
    if isinstance(payload, dict) and payload.get("status") == "Error":
        return False, (
            "ThreatConnect rejected the request: "
            f"{_trunc(payload.get('message'), 300) or 'unknown error'}"
        )
    return True, "ThreatConnect is connected and authenticated."


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _dt(value: Any) -> str:
    """Trim an ISO timestamp to second precision for display."""
    return str(value or "")[:19]


def _names(container: Any, key: str = "name") -> str:
    """Join the ``name`` values from a v3 nested collection (``{"data": [...]}``)."""
    if isinstance(container, dict):
        items = container.get("data") or []
    elif isinstance(container, list):
        items = container
    else:
        return ""
    return ", ".join(str(i.get(key, "?")) for i in items if isinstance(i, dict))


def _tql_int(value: Any) -> int:
    """Coerce a value to int for safe TQL interpolation (raises on non-numeric)."""
    return int(str(value).strip())


def _fmt_indicator(i: dict) -> str:
    rating = i.get("rating")
    confidence = i.get("confidence")
    flags = []
    if i.get("active") is False:
        flags.append("inactive")
    if i.get("falsePositiveFlag"):
        flags.append("false-positive")
    flag_str = f" [{', '.join(flags)}]" if flags else ""
    return (
        f"  [{i.get('type', '?')}] {i.get('summary', '?')}{flag_str}\n"
        f"    id: {i.get('id', '?')} | Rating: {rating if rating is not None else 'N/A'} | "
        f"Confidence: {confidence if confidence is not None else 'N/A'} | "
        f"Owner: {i.get('ownerName', '?')} | Modified: {_dt(i.get('lastModified'))}"
    )


def _fmt_group(g: dict) -> str:
    status = f" — {g.get('status')}" if g.get("status") else ""
    return (
        f"  [{g.get('type', '?')}] {_trunc(g.get('name'), 100)}{status}\n"
        f"    id: {g.get('id', '?')} | Owner: {g.get('ownerName', '?')} | "
        f"Added: {_dt(g.get('dateAdded'))} | Modified: {_dt(g.get('lastModified'))}"
    )


def _fmt_case(c: dict) -> str:
    assignee = ((c.get("assignee") or {}).get("data") or {}) if isinstance(
        c.get("assignee"), dict
    ) else {}
    who = assignee.get("userName") or assignee.get("name") or "Unassigned"
    return (
        f"  #{c.get('id', '?')} [{c.get('severity', '?')}] {c.get('status', '?')} — "
        f"{_trunc(c.get('name'), 90)}\n"
        f"    Resolution: {c.get('resolution', 'N/A')} | Assignee: {who} | "
        f"Opened: {_dt(c.get('caseOpenTime'))} | Owner: {c.get('owner', '?')}"
    )


def _fmt_generic(item: dict) -> str:
    """Compact one-line summary for arbitrary v3 objects (TQL query tool)."""
    ident = item.get("id", "?")
    label = (
        item.get("name")
        or item.get("summary")
        or item.get("text")
        or item.get("fileName")
        or ""
    )
    parts = [f"  id {ident}: {_trunc(label, 90)}" if label else f"  id {ident}"]
    detail_bits = []
    for key in ("type", "status", "severity", "ownerName", "owner"):
        if item.get(key):
            detail_bits.append(f"{key}: {item[key]}")
    if item.get("dateAdded"):
        detail_bits.append(f"added: {_dt(item['dateAdded'])}")
    if detail_bits:
        parts.append(f"    {' | '.join(detail_bits)}")
    if not label and not detail_bits:
        parts.append(f"    {_trunc(_json.dumps(item, default=str), 220)}")
    return "\n".join(parts)


def _clamp_limit(limit: Any) -> int:
    """Clamp a requested page size to the v3 ``resultLimit`` bounds.

    Single source of truth shared by the request parameters
    (``_list_params``) and the next-page hint (``_pagination_note``), so an
    out-of-range ``limit`` (0, negative, or above ``_MAX_RESULT_LIMIT``)
    can never make the hint disagree with the request that was actually sent.
    """
    return max(1, min(int(limit or 25), _MAX_RESULT_LIMIT))


def _clamp_start(start: Any) -> int:
    """Normalise a requested result offset to a non-negative ``resultStart``."""
    return max(0, int(start or 0))


def _pagination_note(data: dict, count: int, start: int, limit: int) -> str:
    """Return a next-page hint when the response advertises more results.

    Compares against the same clamped limit/start that were sent on the
    request, not the caller's raw arguments.
    """
    if data.get("next") or count >= _clamp_limit(limit):
        return (
            f"\n[More results may be available — pass "
            f"start={_clamp_start(start) + count}]"
        )
    return ""


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build ThreatConnect tools from a ``base_url||access_id||secret_key`` token.

    ``base_url`` and ``access_id`` are populated by the 2-part introspection
    dummy; the secret key is supplied only at runtime, so guarding on the
    first two keeps catalog introspection working while a half-configured
    integration still surfaces a clear signing error at call time.
    """
    base_url, access_id, secret_key = (
        part.strip() for part in parse_compound_token(token, 3, greedy_last=True)
    )
    if not base_url or not access_id:
        return []

    api = threatconnect_api_base(base_url)
    api_path_prefix = urlparse(api).path  # e.g. "/api"

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        params: list[tuple[str, Any]] | None = None,
        json: Any | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[dict | None, str | None]:
        """Signed request helper returning ``(payload, error)``.

        Builds the fully encoded URL, signs the exact path + query with the
        HMAC helper, and routes through the shared ``safe_request`` (429
        retry + standardized errors).
        """
        if not secret_key:
            return None, (
                "Error: the ThreatConnect Secret Key is not configured. Set the "
                "Secret Key in the integration's API Key field."
            )
        query = _encode_query(params)
        suffix = f"?{query}" if query else ""
        signed_path = f"{api_path_prefix}{path}{suffix}"
        url = f"{api}{path}{suffix}"
        headers = build_tc_headers(access_id, secret_key, method, signed_path)
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                method,
                url,
                _PROVIDER,
                action,
                headers=headers,
                json=json,
            )
            if err:
                return None, err
            try:
                payload = resp.json()
            except Exception:
                # A 2xx with an empty body is a legitimate success for some
                # actions (e.g. v2 playbook activation); anything else
                # non-JSON is not a ThreatConnect API response.
                if not (getattr(resp, "content", b"") or b"").strip():
                    return {}, None
                return None, (
                    f"Error: {_PROVIDER} returned an unexpected non-JSON "
                    f"response to {action} — check that the instance URL "
                    "points at a ThreatConnect server."
                )
        if isinstance(payload, dict) and payload.get("status") == "Error":
            return None, (
                f"Error: {_PROVIDER} rejected the request to {action}: "
                f"{_trunc(payload.get('message'), 300) or 'unknown error'}"
            )
        return payload if isinstance(payload, dict) else {"data": payload}, None

    def _items(payload: dict | None) -> list[dict]:
        data = (payload or {}).get("data")
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    def _list_params(
        limit: int, start: int, extra: list[tuple[str, Any]] | None = None
    ) -> list[tuple[str, Any]]:
        params: list[tuple[str, Any]] = [("resultLimit", _clamp_limit(limit))]
        normalized_start = _clamp_start(start)
        if normalized_start:
            params.append(("resultStart", normalized_start))
        if extra:
            params.extend(extra)
        return params

    # ══════════════════════════════════════════════════════════════════════
    # 0. OWNERS + TQL SEARCH
    # ══════════════════════════════════════════════════════════════════════

    async def list_threatconnect_owners(limit: int = 50) -> str:
        """List the Organizations, Communities, and Sources the API user can access."""
        payload, err = await _api(
            "get",
            "/v3/security/owners",
            "list owners",
            params=_list_params(limit, 0),
        )
        if err:
            return err
        owners = _items(payload)

        def _fmt(o: dict) -> str:
            return (
                f"  {o.get('name', '?')} [{o.get('type', '?')}] — id: {o.get('id', '?')} | "
                f"Role: {o.get('ownerRole', 'N/A')} | "
                f"Indicators: {o.get('permIndicator', '?')} | Groups: {o.get('permGroup', '?')}"
            )

        return format_entity_list(owners, _fmt, noun="owner")

    async def query_threatconnect(
        object_type: str,
        tql: str = "",
        fields: str = "",
        owner: str = "",
        limit: int = 25,
        start: int = 0,
    ) -> str:
        """Run a raw TQL query against any ThreatConnect v3 object type."""
        endpoint = _QUERY_ENDPOINTS.get(object_type.lower().strip())
        if not endpoint:
            return (
                f"Error: unknown object_type '{object_type}'. Use one of: "
                f"{', '.join(sorted(_QUERY_ENDPOINTS))}."
            )
        extra: list[tuple[str, Any]] = []
        if tql:
            extra.append(("tql", tql))
        if owner:
            extra.append(("owner", owner))
        for field in (f.strip() for f in fields.split(",") if f.strip()):
            extra.append(("fields", field))
        payload, err = await _api(
            "get",
            endpoint,
            f"query {object_type}",
            params=_list_params(limit, start, extra),
        )
        if err:
            return err
        items = _items(payload)
        out = format_entity_list(items, _fmt_generic, noun=object_type.rstrip("s"))
        return out + _pagination_note(payload or {}, len(items), start, limit)

    # ══════════════════════════════════════════════════════════════════════
    # 1. INDICATORS (IOCs)
    # ══════════════════════════════════════════════════════════════════════

    async def list_threatconnect_indicators(
        indicator_type: str = "",
        tql: str = "",
        owner: str = "",
        active_only: bool = False,
        limit: int = 25,
        start: int = 0,
    ) -> str:
        """List Indicators (IOCs), optionally filtered by type, owner, or raw TQL."""
        clauses: list[str] = []
        if indicator_type:
            safe_type = indicator_type.replace("\\", "\\\\").replace('"', '\\"')
            clauses.append(f'typeName in ("{safe_type}")')
        if active_only:
            clauses.append("indicatorActive = true")
        if tql:
            clauses.append(f"({tql})")
        extra: list[tuple[str, Any]] = [("sorting", "lastModified desc")]
        if clauses:
            extra.append(("tql", " and ".join(clauses)))
        if owner:
            extra.append(("owner", owner))
        payload, err = await _api(
            "get",
            "/v3/indicators",
            "list indicators",
            params=_list_params(limit, start, extra),
        )
        if err:
            return err
        indicators = _items(payload)
        out = format_entity_list(indicators, _fmt_indicator, noun="indicator")
        return out + _pagination_note(payload or {}, len(indicators), start, limit)

    async def get_threatconnect_indicator(indicator: str, owner: str = "") -> str:
        """Get full detail for an Indicator by numeric ID or summary value."""
        value = str(indicator).strip()
        if not value:
            return "Error: an indicator ID or summary value is required."
        extra: list[tuple[str, Any]] = [
            ("fields", "tags"),
            ("fields", "attributes"),
            ("fields", "securityLabels"),
            ("fields", "associatedGroups"),
        ]
        if owner:
            extra.append(("owner", owner))
        payload, err = await _api(
            "get",
            f"/v3/indicators/{quote(value, safe='')}",
            f"get indicator {value}",
            params=extra,
        )
        if err:
            return err
        items = _items(payload)
        if not items:
            return f"Indicator {value} not found."
        i = items[0]
        lines = [
            f"Indicator: {i.get('summary', '?')} [{i.get('type', '?')}]",
            f"ID: {i.get('id', '?')} | Owner: {i.get('ownerName', '?')}",
            f"Threat Rating: {i.get('rating', 'N/A')} | "
            f"Confidence: {i.get('confidence', 'N/A')} | "
            f"Active: {i.get('active', '?')} | "
            f"False positive: {i.get('falsePositiveFlag', False)}",
            f"Added: {_dt(i.get('dateAdded'))} | Modified: {_dt(i.get('lastModified'))}",
        ]
        if i.get("description"):
            lines.append(f"Description: {_trunc(i.get('description'), 500)}")
        tags = _names(i.get("tags"))
        if tags:
            lines.append(f"Tags: {tags}")
        labels = _names(i.get("securityLabels"))
        if labels:
            lines.append(f"Security labels: {labels}")
        groups = i.get("associatedGroups") or {}
        group_items = groups.get("data") or [] if isinstance(groups, dict) else []
        if group_items:
            lines.append(f"Associated groups ({len(group_items)}):")
            for g in group_items[:15]:
                lines.append(
                    f"  - [{g.get('type', '?')}] {_trunc(g.get('name'), 80)} "
                    f"(id: {g.get('id', '?')})"
                )
        attrs = i.get("attributes") or {}
        attr_items = attrs.get("data") or [] if isinstance(attrs, dict) else []
        if attr_items:
            lines.append("Attributes:")
            for a in attr_items[:15]:
                lines.append(
                    f"  - {a.get('type', '?')} (id: {a.get('id', '?')}): "
                    f"{_trunc(a.get('value'), 150)}"
                )
        if i.get("webLink"):
            lines.append(f"Link: {i.get('webLink')}")
        return "\n".join(lines)

    async def create_threatconnect_indicator(
        indicator_type: str,
        value: str = "",
        *,
        rating: float | None = None,
        confidence: int | None = None,
        description: str = "",
        tags: str = "",
        security_label: str = "",
        active: bool = True,
        owner: str = "",
        extra_fields: str = "",
    ) -> str:
        """Create an Indicator (IOC) with optional rating, tags, and labels."""
        itype = indicator_type.strip()
        if itype not in _INDICATOR_TYPES:
            return (
                f"Error: invalid indicator_type '{indicator_type}'. Use one of: "
                f"{', '.join(_INDICATOR_TYPES)}."
            )
        body: dict[str, Any] = {"type": itype, "active": active}
        value = value.strip()
        value_field = _INDICATOR_VALUE_FIELDS.get(itype.lower())
        if value_field:
            if not value:
                return f"Error: a value is required for {itype} indicators."
            body[value_field] = value
        elif itype == "File":
            if not value:
                return "Error: a file hash (MD5, SHA1, or SHA256) is required."
            hash_field = {32: "md5", 40: "sha1", 64: "sha256"}.get(len(value))
            if not hash_field or not all(
                c in "0123456789abcdefABCDEF" for c in value
            ):
                return (
                    "Error: the File indicator value must be an MD5 (32), "
                    "SHA1 (40), or SHA256 (64 hex chars) hash."
                )
            body[hash_field] = value
        elif not extra_fields:
            # Registry Key is the one multi-field type — its value cannot be
            # expressed as a single ``value`` and must come via extra_fields.
            return (
                f"Error: {itype} indicators are multi-field — pass extra_fields "
                "JSON with the type-specific fields (Registry Key: 'Key Name', "
                "'Value Name', and 'Value Type', e.g. REG_SZ)."
            )
        if extra_fields:
            try:
                parsed = _json.loads(extra_fields)
            except ValueError:
                return "Error: extra_fields must be a valid JSON object."
            if not isinstance(parsed, dict):
                return "Error: extra_fields must be a JSON object."
            body.update(parsed)
        if rating is not None:
            body["rating"] = rating
        if confidence is not None:
            body["confidence"] = confidence
        if description:
            body["attributes"] = {
                "data": [
                    {"type": "Description", "value": description, "default": True}
                ]
            }
        tag_names = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_names:
            body["tags"] = {"data": [{"name": t} for t in tag_names]}
        if security_label:
            body["securityLabels"] = {"data": [{"name": security_label}]}
        params = [("owner", owner)] if owner else None
        payload, err = await _api(
            "post", "/v3/indicators", "create indicator", params=params, json=body
        )
        if err:
            return err
        created = _items(payload)
        new = created[0] if created else {}
        return (
            f"Indicator created: [{new.get('type', itype)}] "
            f"{new.get('summary', value)} (id: {new.get('id', '?')}, "
            f"owner: {new.get('ownerName', owner or 'default')})"
        )

    async def update_threatconnect_indicator(
        indicator_id: str,
        *,
        rating: float | None = None,
        confidence: int | None = None,
        active: bool | None = None,
        false_positive: bool | None = None,
        add_tags: str = "",
        remove_tags: str = "",
        security_label: str = "",
        owner: str = "",
    ) -> str:
        """Update an Indicator: rating, confidence, active state, tags, labels."""
        body: dict[str, Any] = {}
        if rating is not None:
            body["rating"] = rating
        if confidence is not None:
            body["confidence"] = confidence
        if active is not None:
            body["active"] = active
        if false_positive is not None:
            body["falsePositiveFlag"] = false_positive
        add_list = [t.strip() for t in add_tags.split(",") if t.strip()]
        remove_list = [t.strip() for t in remove_tags.split(",") if t.strip()]
        if add_list and remove_list:
            return (
                "Error: pass either add_tags or remove_tags in one call, not both "
                "(the API applies a single tag mode per request)."
            )
        if add_list:
            body["tags"] = {"data": [{"name": t} for t in add_list], "mode": "append"}
        if remove_list:
            body["tags"] = {
                "data": [{"name": t} for t in remove_list],
                "mode": "delete",
            }
        if security_label:
            body["securityLabels"] = {
                "data": [{"name": security_label}],
                "mode": "append",
            }
        if not body:
            return (
                "Error: nothing to update — provide at least one of rating, "
                "confidence, active, false_positive, add_tags, remove_tags, or "
                "security_label."
            )
        params = [("owner", owner)] if owner else None
        value = str(indicator_id).strip()
        _payload, err = await _api(
            "put",
            f"/v3/indicators/{quote(value, safe='')}",
            f"update indicator {value}",
            params=params,
            json=body,
        )
        if err:
            return err
        applied = ", ".join(sorted(body))
        return f"Indicator {value} updated ({applied})."

    async def delete_threatconnect_indicator(
        indicator_id: str, owner: str = ""
    ) -> str:
        """Delete an Indicator by ID or summary. Permanent, cannot be undone."""
        value = str(indicator_id).strip()
        if not value:
            return "Error: an indicator ID or summary value is required."
        params = [("owner", owner)] if owner else None
        _payload, err = await _api(
            "delete",
            f"/v3/indicators/{quote(value, safe='')}",
            f"delete indicator {value}",
            params=params,
        )
        if err:
            return err
        return f"Indicator {value} deleted."

    # ══════════════════════════════════════════════════════════════════════
    # 2. GROUPS (intel objects)
    # ══════════════════════════════════════════════════════════════════════

    async def list_threatconnect_groups(
        group_type: str = "",
        tql: str = "",
        owner: str = "",
        limit: int = 25,
        start: int = 0,
    ) -> str:
        """List intel Groups, optionally filtered by type, owner, or raw TQL."""
        clauses: list[str] = []
        if group_type:
            safe_type = group_type.replace("\\", "\\\\").replace('"', '\\"')
            clauses.append(f'typeName in ("{safe_type}")')
        if tql:
            clauses.append(f"({tql})")
        extra: list[tuple[str, Any]] = [("sorting", "lastModified desc")]
        if clauses:
            extra.append(("tql", " and ".join(clauses)))
        if owner:
            extra.append(("owner", owner))
        payload, err = await _api(
            "get",
            "/v3/groups",
            "list groups",
            params=_list_params(limit, start, extra),
        )
        if err:
            return err
        groups = _items(payload)
        out = format_entity_list(groups, _fmt_group, noun="group")
        return out + _pagination_note(payload or {}, len(groups), start, limit)

    async def get_threatconnect_group(group_id: str, owner: str = "") -> str:
        """Get full detail for a Group by ID, including tags and associations."""
        try:
            gid = _tql_int(group_id)
        except ValueError:
            return f"Error: group_id must be numeric, got '{group_id}'."
        extra: list[tuple[str, Any]] = [
            ("fields", "tags"),
            ("fields", "attributes"),
            ("fields", "securityLabels"),
            ("fields", "associatedIndicators"),
            ("fields", "associatedGroups"),
        ]
        if owner:
            extra.append(("owner", owner))
        payload, err = await _api(
            "get", f"/v3/groups/{gid}", f"get group {gid}", params=extra
        )
        if err:
            return err
        items = _items(payload)
        if not items:
            return f"Group {gid} not found."
        g = items[0]
        lines = [
            f"Group: {g.get('name', '?')} [{g.get('type', '?')}]",
            f"ID: {g.get('id', '?')} | Owner: {g.get('ownerName', '?')}",
        ]
        if g.get("status"):
            lines.append(f"Status: {g.get('status')}")
        if g.get("eventDate"):
            lines.append(f"Event date: {_dt(g.get('eventDate'))}")
        if g.get("subject"):
            lines.append(f"Subject: {_trunc(g.get('subject'), 150)}")
        if g.get("body"):
            lines.append(f"Body: {_trunc(g.get('body'), 400)}")
        lines.append(
            f"Added: {_dt(g.get('dateAdded'))} | Modified: {_dt(g.get('lastModified'))}"
        )
        tags = _names(g.get("tags"))
        if tags:
            lines.append(f"Tags: {tags}")
        labels = _names(g.get("securityLabels"))
        if labels:
            lines.append(f"Security labels: {labels}")
        for field, noun in (
            ("associatedIndicators", "indicator"),
            ("associatedGroups", "group"),
        ):
            container = g.get(field) or {}
            assoc = container.get("data") or [] if isinstance(container, dict) else []
            if assoc:
                lines.append(f"Associated {noun}s ({len(assoc)}):")
                for a in assoc[:15]:
                    label = a.get("summary") or a.get("name") or "?"
                    lines.append(
                        f"  - [{a.get('type', '?')}] {_trunc(label, 80)} "
                        f"(id: {a.get('id', '?')})"
                    )
        attrs = g.get("attributes") or {}
        attr_items = attrs.get("data") or [] if isinstance(attrs, dict) else []
        if attr_items:
            lines.append("Attributes:")
            for a in attr_items[:15]:
                lines.append(
                    f"  - {a.get('type', '?')} (id: {a.get('id', '?')}): "
                    f"{_trunc(a.get('value'), 150)}"
                )
        if g.get("webLink"):
            lines.append(f"Link: {g.get('webLink')}")
        return "\n".join(lines)

    async def create_threatconnect_group(
        group_type: str,
        name: str,
        *,
        status: str = "",
        event_date: str = "",
        description: str = "",
        tags: str = "",
        security_label: str = "",
        owner: str = "",
        extra_fields: str = "",
    ) -> str:
        """Create an intel Group (Incident, Adversary, Campaign, Threat, ...)."""
        gtype = group_type.strip()
        if gtype not in _GROUP_TYPES:
            return (
                f"Error: invalid group_type '{group_type}'. Use one of: "
                f"{', '.join(_GROUP_TYPES)}."
            )
        if not name.strip():
            return "Error: a group name is required."
        body: dict[str, Any] = {"type": gtype, "name": name.strip()}
        if status:
            body["status"] = status
        if event_date:
            body["eventDate"] = event_date
        if extra_fields:
            try:
                parsed = _json.loads(extra_fields)
            except ValueError:
                return "Error: extra_fields must be a valid JSON object."
            if not isinstance(parsed, dict):
                return "Error: extra_fields must be a JSON object."
            body.update(parsed)
        if description:
            body["attributes"] = {
                "data": [
                    {"type": "Description", "value": description, "default": True}
                ]
            }
        tag_names = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_names:
            body["tags"] = {"data": [{"name": t} for t in tag_names]}
        if security_label:
            body["securityLabels"] = {"data": [{"name": security_label}]}
        params = [("owner", owner)] if owner else None
        payload, err = await _api(
            "post", "/v3/groups", "create group", params=params, json=body
        )
        if err:
            return err
        created = _items(payload)
        new = created[0] if created else {}
        return (
            f"Group created: [{new.get('type', gtype)}] {new.get('name', name)} "
            f"(id: {new.get('id', '?')}, owner: {new.get('ownerName', owner or 'default')})"
        )

    async def update_threatconnect_group(
        group_id: str,
        *,
        name: str = "",
        status: str = "",
        add_tags: str = "",
        remove_tags: str = "",
        security_label: str = "",
        extra_fields: str = "",
        owner: str = "",
    ) -> str:
        """Update a Group: rename, set status, or manage tags and labels."""
        try:
            gid = _tql_int(group_id)
        except ValueError:
            return f"Error: group_id must be numeric, got '{group_id}'."
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if status:
            body["status"] = status
        add_list = [t.strip() for t in add_tags.split(",") if t.strip()]
        remove_list = [t.strip() for t in remove_tags.split(",") if t.strip()]
        if add_list and remove_list:
            return (
                "Error: pass either add_tags or remove_tags in one call, not both "
                "(the API applies a single tag mode per request)."
            )
        if add_list:
            body["tags"] = {"data": [{"name": t} for t in add_list], "mode": "append"}
        if remove_list:
            body["tags"] = {
                "data": [{"name": t} for t in remove_list],
                "mode": "delete",
            }
        if security_label:
            body["securityLabels"] = {
                "data": [{"name": security_label}],
                "mode": "append",
            }
        if extra_fields:
            try:
                parsed = _json.loads(extra_fields)
            except ValueError:
                return "Error: extra_fields must be a valid JSON object."
            if not isinstance(parsed, dict):
                return "Error: extra_fields must be a JSON object."
            body.update(parsed)
        if not body:
            return (
                "Error: nothing to update — provide at least one of name, status, "
                "add_tags, remove_tags, security_label, or extra_fields."
            )
        params = [("owner", owner)] if owner else None
        _payload, err = await _api(
            "put", f"/v3/groups/{gid}", f"update group {gid}", params=params, json=body
        )
        if err:
            return err
        return f"Group {gid} updated ({', '.join(sorted(body))})."

    async def delete_threatconnect_group(group_id: str, owner: str = "") -> str:
        """Delete a Group by ID. Permanent, cannot be undone."""
        try:
            gid = _tql_int(group_id)
        except ValueError:
            return f"Error: group_id must be numeric, got '{group_id}'."
        params = [("owner", owner)] if owner else None
        _payload, err = await _api(
            "delete", f"/v3/groups/{gid}", f"delete group {gid}", params=params
        )
        if err:
            return err
        return f"Group {gid} deleted."

    # ══════════════════════════════════════════════════════════════════════
    # 3. WORKFLOW CASES
    # ══════════════════════════════════════════════════════════════════════

    async def list_threatconnect_cases(
        status: str = "",
        severity: str = "",
        tql: str = "",
        limit: int = 25,
        start: int = 0,
    ) -> str:
        """List Workflow Cases, optionally filtered by status, severity, or TQL."""
        clauses: list[str] = []
        if status:
            safe_status = status.replace("\\", "\\\\").replace('"', '\\"')
            clauses.append(f'status = "{safe_status}"')
        if severity:
            safe_sev = severity.replace("\\", "\\\\").replace('"', '\\"')
            clauses.append(f'severity = "{safe_sev}"')
        if tql:
            clauses.append(f"({tql})")
        extra: list[tuple[str, Any]] = []
        if clauses:
            extra.append(("tql", " and ".join(clauses)))
        payload, err = await _api(
            "get",
            "/v3/cases",
            "list cases",
            params=_list_params(limit, start, extra),
        )
        if err:
            return err
        cases = _items(payload)
        out = format_entity_list(cases, _fmt_case, noun="case")
        return out + _pagination_note(payload or {}, len(cases), start, limit)

    async def get_threatconnect_case(case_id: str) -> str:
        """Get full detail for a Workflow Case, including notes/artifacts/tasks."""
        try:
            cid = _tql_int(case_id)
        except ValueError:
            return f"Error: case_id must be numeric, got '{case_id}'."
        extra: list[tuple[str, Any]] = [
            ("fields", "notes"),
            ("fields", "artifacts"),
            ("fields", "tasks"),
            ("fields", "tags"),
            ("fields", "associatedIndicators"),
            ("fields", "associatedGroups"),
        ]
        payload, err = await _api(
            "get", f"/v3/cases/{cid}", f"get case {cid}", params=extra
        )
        if err:
            return err
        items = _items(payload)
        if not items:
            return f"Case {cid} not found."
        c = items[0]
        assignee = ((c.get("assignee") or {}).get("data") or {}) if isinstance(
            c.get("assignee"), dict
        ) else {}
        lines = [
            f"Case #{c.get('id', '?')}: {c.get('name', '?')}",
            f"Status: {c.get('status', '?')} | Severity: {c.get('severity', '?')} | "
            f"Resolution: {c.get('resolution', 'N/A')}",
            f"Assignee: {assignee.get('userName') or assignee.get('name') or 'Unassigned'} | "
            f"Owner: {c.get('owner', '?')}",
            f"Opened: {_dt(c.get('caseOpenTime'))} | Closed: {_dt(c.get('caseCloseTime')) or 'N/A'}",
        ]
        if c.get("description"):
            lines.append(f"Description: {_trunc(c.get('description'), 500)}")
        tags = _names(c.get("tags"))
        if tags:
            lines.append(f"Tags: {tags}")
        for field, noun, label_keys in (
            ("artifacts", "artifact", ("summary", "type")),
            ("notes", "note", ("text",)),
            ("tasks", "task", ("name", "status")),
        ):
            container = c.get(field) or {}
            entries = container.get("data") or [] if isinstance(container, dict) else []
            if entries:
                lines.append(f"{noun.capitalize()}s ({len(entries)}):")
                for e in entries[:15]:
                    bits = [
                        _trunc(e.get(k), 100) for k in label_keys if e.get(k)
                    ]
                    lines.append(
                        f"  - id {e.get('id', '?')}: {' | '.join(bits) or '?'}"
                    )
        for field, noun in (
            ("associatedIndicators", "indicator"),
            ("associatedGroups", "group"),
        ):
            container = c.get(field) or {}
            assoc = container.get("data") or [] if isinstance(container, dict) else []
            if assoc:
                lines.append(f"Associated {noun}s ({len(assoc)}):")
                for a in assoc[:10]:
                    label = a.get("summary") or a.get("name") or "?"
                    lines.append(
                        f"  - [{a.get('type', '?')}] {_trunc(label, 80)} "
                        f"(id: {a.get('id', '?')})"
                    )
        return "\n".join(lines)

    async def create_threatconnect_case(
        name: str,
        severity: str = "Medium",
        status: str = "Open",
        description: str = "",
        assignee: str = "",
    ) -> str:
        """Open a Workflow Case (investigation) with severity and status."""
        if not name.strip():
            return "Error: a case name is required."
        sev = severity.strip().capitalize()
        if sev not in ("Low", "Medium", "High", "Critical"):
            return (
                f"Error: invalid severity '{severity}'. Use Low, Medium, High, "
                "or Critical."
            )
        st = status.strip().capitalize()
        if st not in ("Open", "Closed"):
            return f"Error: invalid status '{status}'. Use Open or Closed."
        body: dict[str, Any] = {"name": name.strip(), "severity": sev, "status": st}
        if description:
            body["description"] = description
        if assignee:
            body["assignee"] = {"type": "User", "data": {"userName": assignee}}
        payload, err = await _api("post", "/v3/cases", "create case", json=body)
        if err:
            return err
        created = _items(payload)
        new = created[0] if created else {}
        return (
            f"Case created: #{new.get('id', '?')} {new.get('name', name)} "
            f"[{new.get('severity', sev)}] — status {new.get('status', st)}"
        )

    async def update_threatconnect_case(
        case_id: str,
        *,
        status: str = "",
        severity: str = "",
        resolution: str = "",
        name: str = "",
        description: str = "",
        assignee: str = "",
    ) -> str:
        """Update a Workflow Case: close/resolve, re-severity, rename, reassign."""
        try:
            cid = _tql_int(case_id)
        except ValueError:
            return f"Error: case_id must be numeric, got '{case_id}'."
        body: dict[str, Any] = {}
        if status:
            body["status"] = status.strip().capitalize()
        if severity:
            body["severity"] = severity.strip().capitalize()
        if resolution:
            body["resolution"] = resolution
        if name:
            body["name"] = name
        if description:
            body["description"] = description
        if assignee:
            body["assignee"] = {"type": "User", "data": {"userName": assignee}}
        if not body:
            return (
                "Error: nothing to update — provide at least one of status, "
                "severity, resolution, name, description, or assignee."
            )
        _payload, err = await _api(
            "put", f"/v3/cases/{cid}", f"update case {cid}", json=body
        )
        if err:
            return err
        return f"Case {cid} updated ({', '.join(sorted(body))})."

    async def delete_threatconnect_case(case_id: str) -> str:
        """Delete a Workflow Case by ID. Permanent, cannot be undone."""
        try:
            cid = _tql_int(case_id)
        except ValueError:
            return f"Error: case_id must be numeric, got '{case_id}'."
        _payload, err = await _api(
            "delete", f"/v3/cases/{cid}", f"delete case {cid}"
        )
        if err:
            return err
        return f"Case {cid} deleted."

    # ══════════════════════════════════════════════════════════════════════
    # 4. TAGS, SECURITY LABELS, VICTIMS, PLAYBOOKS (reads)
    # ══════════════════════════════════════════════════════════════════════

    async def list_threatconnect_tags(
        tql: str = "",
        attack_only: bool = False,
        owner: str = "",
        limit: int = 25,
        start: int = 0,
    ) -> str:
        """List Tags (standard + ATT&CK technique tags), optional TQL filter."""
        clauses: list[str] = []
        if attack_only:
            clauses.append("techniqueId is not null")
        if tql:
            clauses.append(f"({tql})")
        extra: list[tuple[str, Any]] = []
        if clauses:
            extra.append(("tql", " and ".join(clauses)))
        if owner:
            extra.append(("owner", owner))
        payload, err = await _api(
            "get", "/v3/tags", "list tags", params=_list_params(limit, start, extra)
        )
        if err:
            return err
        tags = _items(payload)

        def _fmt(t: dict) -> str:
            technique = f" [ATT&CK {t.get('techniqueId')}]" if t.get("techniqueId") else ""
            desc = _trunc(t.get("description"), 100)
            return (
                f"  {t.get('name', '?')}{technique} — id: {t.get('id', '?')}"
                + (f" | {desc}" if desc else "")
            )

        out = format_entity_list(tags, _fmt, noun="tag")
        return out + _pagination_note(payload or {}, len(tags), start, limit)

    async def list_threatconnect_security_labels(
        owner: str = "", limit: int = 50
    ) -> str:
        """List Security Labels (TLP and custom classification labels)."""
        extra: list[tuple[str, Any]] = []
        if owner:
            extra.append(("owner", owner))
        payload, err = await _api(
            "get",
            "/v3/securityLabels",
            "list security labels",
            params=_list_params(limit, 0, extra),
        )
        if err:
            return err
        labels = _items(payload)

        def _fmt(sl: dict) -> str:
            return (
                f"  {sl.get('name', '?')} — id: {sl.get('id', '?')} | "
                f"{_trunc(sl.get('description'), 120) or 'No description'}"
            )

        return format_entity_list(labels, _fmt, noun="security label")

    async def list_threatconnect_victims(
        tql: str = "",
        owner: str = "",
        limit: int = 25,
        start: int = 0,
    ) -> str:
        """List Victim records (targeted organizations/individuals)."""
        extra: list[tuple[str, Any]] = []
        if tql:
            extra.append(("tql", tql))
        if owner:
            extra.append(("owner", owner))
        payload, err = await _api(
            "get",
            "/v3/victims",
            "list victims",
            params=_list_params(limit, start, extra),
        )
        if err:
            return err
        victims = _items(payload)

        def _fmt(v: dict) -> str:
            org_bits = " / ".join(
                str(v[k]) for k in ("org", "suborg") if v.get(k)
            )
            return (
                f"  {v.get('name', '?')} — id: {v.get('id', '?')}"
                + (f" | Org: {org_bits}" if org_bits else "")
                + (f" | Location: {v.get('workLocation')}" if v.get("workLocation") else "")
            )

        out = format_entity_list(victims, _fmt, noun="victim")
        return out + _pagination_note(payload or {}, len(victims), start, limit)

    async def list_threatconnect_playbooks(
        tql: str = "",
        active_only: bool = False,
        limit: int = 25,
        start: int = 0,
    ) -> str:
        """List automation Playbooks with trigger type and activation state."""
        clauses: list[str] = []
        if active_only:
            clauses.append('status = "Active"')
        if tql:
            clauses.append(f"({tql})")
        extra: list[tuple[str, Any]] = []
        if clauses:
            extra.append(("tql", " and ".join(clauses)))
        payload, err = await _api(
            "get",
            "/v3/playbooks",
            "list playbooks",
            params=_list_params(limit, start, extra),
        )
        if err:
            return err
        playbooks = _items(payload)

        def _fmt(p: dict) -> str:
            return (
                f"  {_trunc(p.get('name'), 90)} [{p.get('type', '?')}] — "
                f"id: {p.get('id', '?')}\n"
                f"    Trigger: {p.get('triggerType', 'N/A')} | "
                f"Status: {p.get('status', '?')} | Active: {p.get('active', '?')} | "
                f"Updated: {_dt(p.get('updated'))}"
            )

        out = format_entity_list(playbooks, _fmt, noun="playbook")
        return out + _pagination_note(payload or {}, len(playbooks), start, limit)

    # ══════════════════════════════════════════════════════════════════════
    # 5. ASSOCIATIONS
    # ══════════════════════════════════════════════════════════════════════

    async def associate_threatconnect_objects(
        source_type: str,
        source_id: str,
        target_type: str,
        target_ids: str,
        mode: str = "append",
        owner: str = "",
    ) -> str:
        """Link (append) or unlink (delete) associations between two objects."""
        src_endpoint = _ASSOCIATION_SOURCES.get(source_type.lower().strip())
        if not src_endpoint:
            return (
                f"Error: invalid source_type '{source_type}'. Use one of: "
                f"{', '.join(sorted(_ASSOCIATION_SOURCES))}."
            )
        assoc_field = _ASSOCIATION_FIELDS.get(target_type.lower().strip())
        if not assoc_field:
            return (
                f"Error: invalid target_type '{target_type}'. Use one of: "
                f"{', '.join(sorted(_ASSOCIATION_FIELDS))}."
            )
        mode = mode.lower().strip()
        if mode not in ("append", "delete"):
            return "Error: mode must be 'append' (link) or 'delete' (unlink)."
        try:
            ids = [_tql_int(t) for t in target_ids.split(",") if t.strip()]
        except ValueError:
            return "Error: target_ids must be a comma-separated list of numeric IDs."
        if not ids:
            return "Error: at least one target ID is required."
        src = str(source_id).strip()
        body = {assoc_field: {"data": [{"id": i} for i in ids], "mode": mode}}
        params = [("owner", owner)] if owner else None
        _payload, err = await _api(
            "put",
            f"{src_endpoint}/{quote(src, safe='')}",
            f"{'associate' if mode == 'append' else 'dissociate'} {target_type}(s) "
            f"on {source_type} {src}",
            params=params,
            json=body,
        )
        if err:
            return err
        verb = "linked to" if mode == "append" else "unlinked from"
        return (
            f"{len(ids)} {target_type}(s) {verb} {source_type} {src} "
            f"(ids: {', '.join(str(i) for i in ids)})."
        )

    # ══════════════════════════════════════════════════════════════════════
    # 6. CONSOLIDATED MANAGE TOOLS (long tail)
    # ══════════════════════════════════════════════════════════════════════

    async def manage_threatconnect_attributes(
        action: str,
        object_type: str = "",
        object_id: str = "",
        attribute_id: str = "",
        attribute_type: str = "",
        value: str = "",
    ) -> str:
        """Manage Attributes on indicators/groups/cases: list/add/update/delete/list_types."""
        action = action.lower().strip()
        if action == "list_types":
            payload, err = await _api(
                "get",
                "/v3/attributeTypes",
                "list attribute types",
                params=_list_params(100, 0),
            )
            if err:
                return err
            types = _items(payload)

            def _fmt(t: dict) -> str:
                return (
                    f"  {t.get('name', '?')} — id: {t.get('id', '?')} | "
                    f"{_trunc(t.get('description'), 100) or 'No description'}"
                )

            return format_entity_list(types, _fmt, noun="attribute type")

        entry = _ATTRIBUTE_ENDPOINTS.get(object_type.lower().strip())
        if action in ("list", "add") and not entry:
            return (
                f"Error: invalid object_type '{object_type}'. Use one of: "
                f"{', '.join(sorted(_ATTRIBUTE_ENDPOINTS))}."
            )

        if action == "list":
            try:
                oid = _tql_int(object_id)
            except ValueError:
                return "Error: a numeric object_id is required to list attributes."
            # Read attributes off the parent object (``?fields=attributes``)
            # rather than guessing per-endpoint TQL keywords.
            parent_endpoint = {
                "indicator": "/v3/indicators",
                "group": "/v3/groups",
                "case": "/v3/cases",
            }[object_type.lower().strip()]
            payload, err = await _api(
                "get",
                f"{parent_endpoint}/{oid}",
                f"list {object_type} attributes",
                params=[("fields", "attributes")],
            )
            if err:
                return err
            items = _items(payload)
            container = (items[0].get("attributes") or {}) if items else {}
            attrs = container.get("data") or [] if isinstance(container, dict) else []

            def _fmt(a: dict) -> str:
                return (
                    f"  {a.get('type', '?')} (id: {a.get('id', '?')}): "
                    f"{_trunc(a.get('value'), 200)}"
                )

            return format_entity_list(attrs, _fmt, noun="attribute")

        if action == "add":
            try:
                oid = _tql_int(object_id)
            except ValueError:
                return "Error: a numeric object_id is required to add an attribute."
            if not attribute_type or not value:
                return "Error: attribute_type and value are required to add an attribute."
            endpoint, id_field = entry
            body = {id_field: oid, "type": attribute_type, "value": value}
            payload, err = await _api(
                "post", endpoint, f"add {object_type} attribute", json=body
            )
            if err:
                return err
            created = _items(payload)
            new_id = created[0].get("id") if created else "?"
            return (
                f"Attribute '{attribute_type}' added to {object_type} {oid} "
                f"(attribute id: {new_id})."
            )

        if action in ("update", "delete"):
            if not entry:
                return (
                    f"Error: invalid object_type '{object_type}'. Use one of: "
                    f"{', '.join(sorted(_ATTRIBUTE_ENDPOINTS))}."
                )
            try:
                aid = _tql_int(attribute_id)
            except ValueError:
                return f"Error: a numeric attribute_id is required to {action}."
            endpoint, _ = entry
            if action == "update":
                if not value:
                    return "Error: a value is required to update an attribute."
                _payload, err = await _api(
                    "put",
                    f"{endpoint}/{aid}",
                    f"update attribute {aid}",
                    json={"value": value},
                )
                if err:
                    return err
                return f"Attribute {aid} updated."
            _payload, err = await _api(
                "delete", f"{endpoint}/{aid}", f"delete attribute {aid}"
            )
            if err:
                return err
            return f"Attribute {aid} deleted."

        return (
            f"Error: invalid action '{action}'. Use one of: list, add, update, "
            "delete, list_types."
        )

    async def manage_threatconnect_tags(
        action: str,
        tag_id: str = "",
        name: str = "",
        description: str = "",
        owner: str = "",
    ) -> str:
        """Manage Tags: create, update, or delete a tag definition."""
        action = action.lower().strip()
        params = [("owner", owner)] if owner else None
        if action == "create":
            if not name.strip():
                return "Error: a tag name is required."
            body: dict[str, Any] = {"name": name.strip()}
            if description:
                body["description"] = description
            payload, err = await _api(
                "post", "/v3/tags", "create tag", params=params, json=body
            )
            if err:
                return err
            created = _items(payload)
            new = created[0] if created else {}
            return f"Tag created: {new.get('name', name)} (id: {new.get('id', '?')})"
        if action in ("update", "delete"):
            try:
                tid = _tql_int(tag_id)
            except ValueError:
                return f"Error: a numeric tag_id is required to {action} a tag."
            if action == "update":
                body = {}
                if name:
                    body["name"] = name
                if description:
                    body["description"] = description
                if not body:
                    return "Error: provide a new name and/or description to update."
                _payload, err = await _api(
                    "put", f"/v3/tags/{tid}", f"update tag {tid}", params=params, json=body
                )
                if err:
                    return err
                return f"Tag {tid} updated."
            _payload, err = await _api(
                "delete", f"/v3/tags/{tid}", f"delete tag {tid}", params=params
            )
            if err:
                return err
            return f"Tag {tid} deleted (removed from all objects)."
        return f"Error: invalid action '{action}'. Use one of: create, update, delete."

    async def manage_threatconnect_victims(
        action: str,
        victim_id: str = "",
        name: str = "",
        org: str = "",
        suborg: str = "",
        nationality: str = "",
        work_location: str = "",
        asset_type: str = "",
        asset_value: str = "",
        owner: str = "",
    ) -> str:
        """Manage Victims: get/create/update/delete a victim, or add an asset."""
        action = action.lower().strip()
        params = [("owner", owner)] if owner else None

        def _profile_body() -> dict[str, Any]:
            body: dict[str, Any] = {}
            if name:
                body["name"] = name
            if org:
                body["org"] = org
            if suborg:
                body["suborg"] = suborg
            if nationality:
                body["nationality"] = nationality
            if work_location:
                body["workLocation"] = work_location
            return body

        if action == "create":
            body = _profile_body()
            if not body.get("name"):
                return "Error: a victim name is required."
            payload, err = await _api(
                "post", "/v3/victims", "create victim", params=params, json=body
            )
            if err:
                return err
            created = _items(payload)
            new = created[0] if created else {}
            return f"Victim created: {new.get('name', name)} (id: {new.get('id', '?')})"

        try:
            vid = _tql_int(victim_id)
        except ValueError:
            return f"Error: a numeric victim_id is required to {action}."

        if action == "get":
            extra: list[tuple[str, Any]] = [
                ("fields", "assets"),
                ("fields", "tags"),
                ("fields", "associatedGroups"),
            ]
            payload, err = await _api(
                "get", f"/v3/victims/{vid}", f"get victim {vid}", params=extra
            )
            if err:
                return err
            items = _items(payload)
            if not items:
                return f"Victim {vid} not found."
            v = items[0]
            lines = [
                f"Victim: {v.get('name', '?')} (id: {v.get('id', '?')})",
                f"Org: {v.get('org', 'N/A')} / {v.get('suborg', 'N/A')} | "
                f"Nationality: {v.get('nationality', 'N/A')} | "
                f"Location: {v.get('workLocation', 'N/A')}",
            ]
            assets = v.get("assets") or {}
            asset_items = assets.get("data") or [] if isinstance(assets, dict) else []
            if asset_items:
                lines.append(f"Assets ({len(asset_items)}):")
                for a in asset_items[:20]:
                    val = (
                        a.get("address")
                        or a.get("phone")
                        or a.get("website")
                        or a.get("accountName")
                        or "?"
                    )
                    lines.append(
                        f"  - [{a.get('type', '?')}] {val} (id: {a.get('id', '?')})"
                    )
            tags = _names(v.get("tags"))
            if tags:
                lines.append(f"Tags: {tags}")
            groups = _names(v.get("associatedGroups"))
            if groups:
                lines.append(f"Associated groups: {groups}")
            return "\n".join(lines)

        if action == "update":
            body = _profile_body()
            if not body:
                return "Error: provide at least one profile field to update."
            _payload, err = await _api(
                "put", f"/v3/victims/{vid}", f"update victim {vid}", params=params, json=body
            )
            if err:
                return err
            return f"Victim {vid} updated ({', '.join(sorted(body))})."

        if action == "delete":
            _payload, err = await _api(
                "delete", f"/v3/victims/{vid}", f"delete victim {vid}", params=params
            )
            if err:
                return err
            return f"Victim {vid} deleted."

        if action == "add_asset":
            asset_key = _VICTIM_ASSET_FIELDS.get(
                asset_type.lower().replace(" ", "").strip()
            )
            if not asset_key:
                return (
                    f"Error: invalid asset_type '{asset_type}'. Use one of: "
                    "EmailAddress, NetworkAccount, Phone, SocialNetwork, WebSite."
                )
            if not asset_value.strip():
                return "Error: an asset_value is required."
            canonical = {
                "emailaddress": "EmailAddress",
                "networkaccount": "NetworkAccount",
                "phone": "Phone",
                "socialnetwork": "SocialNetwork",
                "website": "WebSite",
            }[asset_type.lower().replace(" ", "").strip()]
            body = {
                "victimId": vid,
                "type": canonical,
                asset_key: asset_value.strip(),
            }
            payload, err = await _api(
                "post", "/v3/victimAssets", f"add asset to victim {vid}", json=body
            )
            if err:
                return err
            created = _items(payload)
            new_id = created[0].get("id") if created else "?"
            return (
                f"{canonical} asset '{asset_value.strip()}' added to victim {vid} "
                f"(asset id: {new_id})."
            )

        return (
            f"Error: invalid action '{action}'. Use one of: get, create, update, "
            "delete, add_asset."
        )

    async def manage_threatconnect_workflow(
        action: str,
        case_id: str = "",
        item_id: str = "",
        text: str = "",
        summary: str = "",
        artifact_type: str = "",
        task_name: str = "",
        task_status: str = "",
        due_date: str = "",
        limit: int = 25,
    ) -> str:
        """Manage case workflow items: notes, artifacts, tasks, and templates."""
        action = action.lower().strip()

        if action == "list_templates":
            payload, err = await _api(
                "get",
                "/v3/workflowTemplates",
                "list workflow templates",
                params=_list_params(limit, 0),
            )
            if err:
                return err
            templates = _items(payload)

            def _fmt(t: dict) -> str:
                return (
                    f"  {t.get('name', '?')} — id: {t.get('id', '?')} | "
                    f"v{t.get('version', '?')} | Active: {t.get('active', '?')} | "
                    f"{_trunc(t.get('description'), 100)}"
                )

            return format_entity_list(templates, _fmt, noun="workflow template")

        if action == "update_task":
            try:
                tid = _tql_int(item_id)
            except ValueError:
                return "Error: a numeric item_id (task ID) is required."
            body: dict[str, Any] = {}
            if task_name:
                body["name"] = task_name
            if task_status:
                body["status"] = task_status
            if due_date:
                body["dueDate"] = due_date
            if not body:
                return "Error: provide task_name, task_status, and/or due_date."
            _payload, err = await _api(
                "put", f"/v3/tasks/{tid}", f"update task {tid}", json=body
            )
            if err:
                return err
            return f"Task {tid} updated ({', '.join(sorted(body))})."

        try:
            cid = _tql_int(case_id)
        except ValueError:
            return f"Error: a numeric case_id is required to {action}."

        if action == "add_note":
            if not text.strip():
                return "Error: note text is required."
            payload, err = await _api(
                "post",
                "/v3/notes",
                f"add note to case {cid}",
                json={"caseId": cid, "text": text},
            )
            if err:
                return err
            created = _items(payload)
            new_id = created[0].get("id") if created else "?"
            return f"Note added to case {cid} (note id: {new_id})."

        if action == "add_artifact":
            if not summary.strip() or not artifact_type.strip():
                return (
                    "Error: summary (the artifact value) and artifact_type (e.g. "
                    "Host, IP Address, Email Address, URL, File Hash) are required."
                )
            payload, err = await _api(
                "post",
                "/v3/artifacts",
                f"add artifact to case {cid}",
                json={"caseId": cid, "summary": summary, "type": artifact_type},
            )
            if err:
                return err
            created = _items(payload)
            new_id = created[0].get("id") if created else "?"
            return (
                f"Artifact [{artifact_type}] '{summary}' added to case {cid} "
                f"(artifact id: {new_id})."
            )

        if action == "add_task":
            if not task_name.strip():
                return "Error: a task_name is required."
            body = {"caseId": cid, "name": task_name}
            if text:
                body["description"] = text
            if due_date:
                body["dueDate"] = due_date
            payload, err = await _api(
                "post", "/v3/tasks", f"add task to case {cid}", json=body
            )
            if err:
                return err
            created = _items(payload)
            new_id = created[0].get("id") if created else "?"
            return f"Task '{task_name}' added to case {cid} (task id: {new_id})."

        if action in ("list_notes", "list_artifacts", "list_tasks"):
            endpoint = {
                "list_notes": "/v3/notes",
                "list_artifacts": "/v3/artifacts",
                "list_tasks": "/v3/tasks",
            }[action]
            payload, err = await _api(
                "get",
                endpoint,
                f"{action.replace('_', ' ')} for case {cid}",
                params=_list_params(limit, 0, [("tql", f"caseId = {cid}")]),
            )
            if err:
                return err
            items = _items(payload)

            def _fmt(e: dict) -> str:
                label = (
                    e.get("text") or e.get("summary") or e.get("name") or "?"
                )
                bits = [f"  id {e.get('id', '?')}: {_trunc(label, 150)}"]
                detail = [
                    f"{k}: {e[k]}"
                    for k in ("type", "status", "dueDate")
                    if e.get(k)
                ]
                if detail:
                    bits.append(f"    {' | '.join(detail)}")
                return "\n".join(bits)

            noun = action.replace("list_", "").rstrip("s")
            return format_entity_list(items, _fmt, noun=noun)

        return (
            f"Error: invalid action '{action}'. Use one of: add_note, list_notes, "
            "add_artifact, list_artifacts, add_task, list_tasks, update_task, "
            "list_templates."
        )

    async def manage_threatconnect_playbooks(
        action: str,
        playbook_id: str = "",
        tql: str = "",
        limit: int = 25,
    ) -> str:
        """Manage Playbooks: activate/deactivate, or list recent executions."""
        action = action.lower().strip()
        if action == "executions":
            extra: list[tuple[str, Any]] = []
            if tql:
                extra.append(("tql", tql))
            payload, err = await _api(
                "get",
                "/v3/playbookExecutions",
                "list playbook executions",
                params=_list_params(limit, 0, extra),
            )
            if err:
                return err
            executions = _items(payload)

            def _fmt(e: dict) -> str:
                return (
                    f"  id {e.get('id', '?')}: {_trunc(e.get('playbookName') or e.get('name'), 80)} — "
                    f"Status: {e.get('status', '?')} | "
                    f"Started: {_dt(e.get('startTime') or e.get('dateAdded'))}"
                )

            return format_entity_list(executions, _fmt, noun="execution")

        if action in ("activate", "deactivate"):
            pid = str(playbook_id).strip()
            if not pid:
                return f"Error: a playbook_id is required to {action}."
            _payload, err = await _api(
                "post",
                f"/v2/playbooks/{quote(pid, safe='')}/{action}",
                f"{action} playbook {pid}",
            )
            if err:
                return err
            return f"Playbook {pid} {action}d."

        return (
            f"Error: invalid action '{action}'. Use one of: activate, deactivate, "
            "executions."
        )

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("list_threatconnect_owners", list_threatconnect_owners)
    executor.register("query_threatconnect", query_threatconnect)
    executor.register("list_threatconnect_indicators", list_threatconnect_indicators)
    executor.register("get_threatconnect_indicator", get_threatconnect_indicator)
    executor.register("create_threatconnect_indicator", create_threatconnect_indicator)
    executor.register("update_threatconnect_indicator", update_threatconnect_indicator)
    executor.register("delete_threatconnect_indicator", delete_threatconnect_indicator)
    executor.register("list_threatconnect_groups", list_threatconnect_groups)
    executor.register("get_threatconnect_group", get_threatconnect_group)
    executor.register("create_threatconnect_group", create_threatconnect_group)
    executor.register("update_threatconnect_group", update_threatconnect_group)
    executor.register("delete_threatconnect_group", delete_threatconnect_group)
    executor.register("list_threatconnect_cases", list_threatconnect_cases)
    executor.register("get_threatconnect_case", get_threatconnect_case)
    executor.register("create_threatconnect_case", create_threatconnect_case)
    executor.register("update_threatconnect_case", update_threatconnect_case)
    executor.register("delete_threatconnect_case", delete_threatconnect_case)
    executor.register("list_threatconnect_tags", list_threatconnect_tags)
    executor.register(
        "list_threatconnect_security_labels", list_threatconnect_security_labels
    )
    executor.register("list_threatconnect_victims", list_threatconnect_victims)
    executor.register("list_threatconnect_playbooks", list_threatconnect_playbooks)
    executor.register(
        "associate_threatconnect_objects", associate_threatconnect_objects
    )
    executor.register(
        "manage_threatconnect_attributes", manage_threatconnect_attributes
    )
    executor.register("manage_threatconnect_tags", manage_threatconnect_tags)
    executor.register("manage_threatconnect_victims", manage_threatconnect_victims)
    executor.register("manage_threatconnect_workflow", manage_threatconnect_workflow)
    executor.register("manage_threatconnect_playbooks", manage_threatconnect_playbooks)

    _pagination_props = {
        "limit": {
            "type": "integer",
            "default": 25,
            "description": "Maximum number of results to return (max 500)",
        },
        "start": {
            "type": "integer",
            "default": 0,
            "description": "Starting result index for pagination",
        },
    }
    _owner_prop = {
        "owner": {
            "type": "string",
            "description": (
                "Owner (Organization/Community/Source name) to scope the request "
                "to; defaults to the API user's Organization"
            ),
        }
    }

    return [
        # ── 0. Owners + TQL search ────────────────────────────────────────
        ToolDefinition(
            name="list_threatconnect_owners",
            description=(
                "List the ThreatConnect owners (Organizations, Communities, "
                "Sources) the API user can access, with per-owner permissions. "
                "Use this first to discover valid values for the 'owner' "
                "parameter on other tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 50},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="query_threatconnect",
            description=(
                "Run a raw ThreatConnect Query Language (TQL) query against any "
                "v3 object type (indicators, groups, cases, tags, victims, "
                "victim_assets, artifacts, notes, tasks, workflow_templates, "
                "playbooks, playbook_executions, owners, attribute_types, "
                "security_labels). Prefer the dedicated list tools for common "
                "filters; use this for advanced queries, e.g. "
                "'typeName in (\"Address\") and rating >= 4' or "
                "'threatAssessScore >= 500'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": {
                        "type": "string",
                        "enum": sorted(_QUERY_ENDPOINTS),
                        "description": "The v3 object type to query",
                    },
                    "tql": {
                        "type": "string",
                        "description": "TQL filter expression (optional)",
                    },
                    "fields": {
                        "type": "string",
                        "description": (
                            "Comma-separated extra response fields, e.g. "
                            "'tags,attributes,securityLabels'"
                        ),
                    },
                    **_owner_prop,
                    **_pagination_props,
                },
                "required": ["object_type"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 1. Indicators ────────────────────────────────────────────────
        ToolDefinition(
            name="list_threatconnect_indicators",
            description=(
                "List ThreatConnect Indicators (IOCs), newest-modified first. "
                "Filter by type (Address, Host, URL, File, EmailAddress, ASN, "
                "CIDR, Mutex, ...), owner, active state, or a raw TQL filter "
                "(e.g. 'rating >= 4' or 'dateAdded > \"2024-01-01\"'). Returns "
                "type, value, rating, confidence, and owner per indicator."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "indicator_type": {
                        "type": "string",
                        "description": "Filter by indicator type, e.g. 'Host' (optional)",
                    },
                    "tql": {
                        "type": "string",
                        "description": "Raw TQL filter combined with the other filters (optional)",
                    },
                    "active_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Only return active indicators",
                    },
                    **_owner_prop,
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_threatconnect_indicator",
            description=(
                "Get full detail for a ThreatConnect Indicator by numeric ID or "
                "by its summary value (e.g. 'badguy.com', an IP, a hash). "
                "Returns ratings, tags, security labels, attributes, associated "
                "groups, and the web link."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "indicator": {
                        "type": "string",
                        "description": "Indicator ID or summary value (IP, host, URL, hash, email)",
                    },
                    **_owner_prop,
                },
                "required": ["indicator"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_threatconnect_indicator",
            description=(
                "Create a ThreatConnect Indicator (IOC). indicator_type: "
                "Address (value=IP), Host (value=hostname), URL (value=URL), "
                "EmailAddress (value=email), File (value=MD5/SHA1/SHA256 hash), "
                "ASN (value=ASN-prefixed number, e.g. 'ASN204288'), CIDR "
                "(value=block), Email Subject, Hashtag, Mutex, or User Agent "
                "(value=the observable). Registry Key is multi-field: omit "
                "value and pass extra_fields JSON with 'Key Name', "
                "'Value Name', 'Value Type'. "
                "Supports Threat Rating (0-5), Confidence (0-100), a "
                "Description attribute, comma-separated tags, and a security "
                "label (e.g. 'TLP:AMBER'). If the indicator already exists in "
                "the owner it is updated instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "indicator_type": {
                        "type": "string",
                        "enum": list(_INDICATOR_TYPES),
                    },
                    "value": {
                        "type": "string",
                        "description": (
                            "The observable value (IP, hostname, URL, email, or "
                            "file hash) — required for every type except "
                            "Registry Key, which takes extra_fields instead"
                        ),
                    },
                    "rating": {
                        "type": "number",
                        "description": "Threat Rating 0-5 (optional)",
                    },
                    "confidence": {
                        "type": "integer",
                        "description": "Confidence Rating 0-100 (optional)",
                    },
                    "description": {"type": "string"},
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tag names to apply (optional)",
                    },
                    "security_label": {
                        "type": "string",
                        "description": "Security label name, e.g. TLP:AMBER (optional)",
                    },
                    "active": {"type": "boolean", "default": True},
                    "extra_fields": {
                        "type": "string",
                        "description": (
                            "JSON object of additional type-specific fields for "
                            "non-core types (e.g. Registry Key)"
                        ),
                    },
                    **_owner_prop,
                },
                # ``value`` is intentionally not schema-required: Registry Key
                # is multi-field and takes extra_fields instead. The runtime
                # guards still require a value for every single-field type.
                "required": ["indicator_type"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_threatconnect_indicator",
            description=(
                "Update a ThreatConnect Indicator by ID or summary: set the "
                "Threat Rating / Confidence, activate or deactivate it, flag it "
                "as a false positive, add or remove tags (one mode per call), "
                "or apply a security label."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "indicator_id": {
                        "type": "string",
                        "description": "Indicator ID or summary value",
                    },
                    "rating": {"type": "number", "description": "Threat Rating 0-5"},
                    "confidence": {
                        "type": "integer",
                        "description": "Confidence 0-100",
                    },
                    "active": {"type": "boolean"},
                    "false_positive": {"type": "boolean"},
                    "add_tags": {
                        "type": "string",
                        "description": "Comma-separated tags to add",
                    },
                    "remove_tags": {
                        "type": "string",
                        "description": "Comma-separated tags to remove",
                    },
                    "security_label": {"type": "string"},
                    **_owner_prop,
                },
                "required": ["indicator_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_threatconnect_indicator",
            description=(
                "Delete a ThreatConnect Indicator by ID or summary value. "
                "Permanent, cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "indicator_id": {
                        "type": "string",
                        "description": "Indicator ID or summary value",
                    },
                    **_owner_prop,
                },
                "required": ["indicator_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 2. Groups ────────────────────────────────────────────────────
        ToolDefinition(
            name="list_threatconnect_groups",
            description=(
                "List ThreatConnect intel Groups, newest-modified first. Filter "
                "by type (Incident, Adversary, Campaign, Threat, Report, Email, "
                "Event, Malware, Intrusion Set, Signature, Vulnerability, ...), "
                "owner, or a raw TQL filter (e.g. 'tag in (\"hacker\")'). "
                "Returns type, name, status, owner, and dates per group."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group_type": {
                        "type": "string",
                        "description": "Filter by group type, e.g. 'Incident' (optional)",
                    },
                    "tql": {"type": "string"},
                    **_owner_prop,
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_threatconnect_group",
            description=(
                "Get full detail for a ThreatConnect Group by numeric ID: "
                "status, tags, security labels, attributes, associated "
                "indicators/groups, and the web link."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group_id": {"type": "string"},
                    **_owner_prop,
                },
                "required": ["group_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_threatconnect_group",
            description=(
                "Create a ThreatConnect intel Group (Incident, Adversary, "
                "Campaign, Threat, Report, Email, Event, Malware, ...). "
                "Type-specific fields: Incident/Event accept status + "
                "event_date; other type-specific fields (email subject/body, "
                "report publishDate, signature fileName/fileType/fileText) go "
                "in extra_fields JSON. Supports a Description attribute, "
                "comma-separated tags (standard or ATT&CK technique names), and "
                "a security label."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group_type": {"type": "string", "enum": list(_GROUP_TYPES)},
                    "name": {"type": "string"},
                    "status": {
                        "type": "string",
                        "description": (
                            "Status for Incident (New, Open, Stalled, Containment "
                            "Achieved, Restoration Achieved, Incident Reported, "
                            "Closed, Rejected) or Event (New, In Progress, "
                            "Escalated, ...) groups (optional)"
                        ),
                    },
                    "event_date": {
                        "type": "string",
                        "description": "ISO 8601 event date for Incident/Event groups (optional)",
                    },
                    "description": {"type": "string"},
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tag names (optional)",
                    },
                    "security_label": {"type": "string"},
                    "extra_fields": {
                        "type": "string",
                        "description": "JSON object of additional type-specific fields",
                    },
                    **_owner_prop,
                },
                "required": ["group_type", "name"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_threatconnect_group",
            description=(
                "Update a ThreatConnect Group by ID: rename, set the status, "
                "add or remove tags (one mode per call), apply a security "
                "label, or set type-specific fields via extra_fields JSON."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group_id": {"type": "string"},
                    "name": {"type": "string"},
                    "status": {"type": "string"},
                    "add_tags": {"type": "string"},
                    "remove_tags": {"type": "string"},
                    "security_label": {"type": "string"},
                    "extra_fields": {"type": "string"},
                    **_owner_prop,
                },
                "required": ["group_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_threatconnect_group",
            description=(
                "Delete a ThreatConnect Group by ID. Permanent, cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group_id": {"type": "string"},
                    **_owner_prop,
                },
                "required": ["group_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 3. Workflow cases ────────────────────────────────────────────
        ToolDefinition(
            name="list_threatconnect_cases",
            description=(
                "List ThreatConnect Workflow Cases. Filter by status (Open, "
                "Closed), severity (Low, Medium, High, Critical), or a raw TQL "
                "filter (e.g. 'caseOpenTime GEQ \"2024-01-01\"'). Returns "
                "severity, status, resolution, and assignee per case."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Open or Closed"},
                    "severity": {
                        "type": "string",
                        "description": "Low, Medium, High, or Critical",
                    },
                    "tql": {"type": "string"},
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_threatconnect_case",
            description=(
                "Get full detail for a Workflow Case by ID, including notes, "
                "artifacts, tasks, tags, and associated indicators/groups."
            ),
            input_schema={
                "type": "object",
                "properties": {"case_id": {"type": "string"}},
                "required": ["case_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_threatconnect_case",
            description=(
                "Open a ThreatConnect Workflow Case (investigation). severity: "
                "Low, Medium, High, Critical. status: Open or Closed. "
                "Optionally assign to a user by username/email. Use "
                "manage_threatconnect_workflow to add notes, artifacts, and "
                "tasks afterwards."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["Low", "Medium", "High", "Critical"],
                        "default": "Medium",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["Open", "Closed"],
                        "default": "Open",
                    },
                    "description": {"type": "string"},
                    "assignee": {
                        "type": "string",
                        "description": "Username (email) to assign the case to (optional)",
                    },
                },
                "required": ["name"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_threatconnect_case",
            description=(
                "Update a Workflow Case by ID: set status (Open/Closed), "
                "severity, resolution (Containment Achieved, Escalated, False "
                "Positive, In Progress / Investigating, Rejected, Restoration "
                "Achieved, Deferred / Delayed, Not Specified), rename, "
                "re-describe, or reassign."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "status": {"type": "string"},
                    "severity": {"type": "string"},
                    "resolution": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "assignee": {"type": "string"},
                },
                "required": ["case_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_threatconnect_case",
            description=(
                "Delete a Workflow Case by ID. Permanent, cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {"case_id": {"type": "string"}},
                "required": ["case_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 4. Tags / labels / victims / playbooks (reads) ───────────────
        ToolDefinition(
            name="list_threatconnect_tags",
            description=(
                "List ThreatConnect Tags (standard and MITRE ATT&CK technique "
                "tags). Set attack_only to restrict to ATT&CK tags, or use TQL "
                "(e.g. 'techniqueId startswith \"T1566\"' or "
                "'associatedGroup EQ 11')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tql": {"type": "string"},
                    "attack_only": {"type": "boolean", "default": False},
                    **_owner_prop,
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_threatconnect_security_labels",
            description=(
                "List Security Labels (TLP:RED/AMBER/GREEN/CLEAR and custom "
                "classification labels) available in an owner. Use the returned "
                "names as the security_label parameter on create/update tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_owner_prop,
                    "limit": {"type": "integer", "default": 50},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_threatconnect_victims",
            description=(
                "List Victim records (targeted organizations/individuals) with "
                "their org and location. Use manage_threatconnect_victims for "
                "detail, creation, updates, and victim assets."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tql": {"type": "string"},
                    **_owner_prop,
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_threatconnect_playbooks",
            description=(
                "List automation Playbooks with type, trigger, and activation "
                "state. Use manage_threatconnect_playbooks to activate/"
                "deactivate a playbook or inspect executions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tql": {
                        "type": "string",
                        "description": "TQL filter, e.g. 'name = \"Enrich IOC\"' (optional)",
                    },
                    "active_only": {"type": "boolean", "default": False},
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 5. Associations ──────────────────────────────────────────────
        ToolDefinition(
            name="associate_threatconnect_objects",
            description=(
                "Link (mode=append) or unlink (mode=delete) associations "
                "between ThreatConnect objects: e.g. associate indicators to a "
                "group, groups to a case, or artifacts to a group. source_type: "
                "indicator, group, case, victim. target_type: indicator, group, "
                "case, artifact, victim_asset. The source is updated with the "
                "target IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source_type": {
                        "type": "string",
                        "enum": sorted(_ASSOCIATION_SOURCES),
                    },
                    "source_id": {
                        "type": "string",
                        "description": "ID (or indicator summary) of the object to update",
                    },
                    "target_type": {
                        "type": "string",
                        "enum": sorted(_ASSOCIATION_FIELDS),
                    },
                    "target_ids": {
                        "type": "string",
                        "description": "Comma-separated numeric IDs to link/unlink",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["append", "delete"],
                        "default": "append",
                    },
                    **_owner_prop,
                },
                "required": ["source_type", "source_id", "target_type", "target_ids"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 6. Consolidated manage tools ─────────────────────────────────
        ToolDefinition(
            name="manage_threatconnect_attributes",
            description=(
                "Manage Attributes on indicators, groups, and cases. action: "
                "'list' (attributes on an object), 'add' (attribute_type + "
                "value on an object), 'update' (attribute_id + value), 'delete' "
                "(attribute_id), 'list_types' (available attribute types like "
                "Description, Source)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "update", "delete", "list_types"],
                    },
                    "object_type": {
                        "type": "string",
                        "enum": ["indicator", "group", "case"],
                    },
                    "object_id": {
                        "type": "string",
                        "description": "Numeric ID of the parent object (for list/add)",
                    },
                    "attribute_id": {
                        "type": "string",
                        "description": "Attribute ID (for update/delete)",
                    },
                    "attribute_type": {
                        "type": "string",
                        "description": "Attribute type name, e.g. 'Description' (for add)",
                    },
                    "value": {"type": "string"},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_threatconnect_tags",
            description=(
                "Manage Tag definitions. action: 'create' (name + optional "
                "description), 'update' (tag_id + new name/description), "
                "'delete' (tag_id — removes the tag from every object, "
                "permanent). To apply/remove tags on objects use the "
                "update_threatconnect_* tools instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "delete"],
                    },
                    "tag_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    **_owner_prop,
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_threatconnect_victims",
            description=(
                "Manage Victim records. action: 'get' (full detail + assets), "
                "'create' (name + org/suborg/nationality/work_location), "
                "'update', 'delete' (permanent), 'add_asset' (asset_type: "
                "EmailAddress, NetworkAccount, Phone, SocialNetwork, WebSite + "
                "asset_value)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get", "create", "update", "delete", "add_asset"],
                    },
                    "victim_id": {"type": "string"},
                    "name": {"type": "string"},
                    "org": {"type": "string"},
                    "suborg": {"type": "string"},
                    "nationality": {"type": "string"},
                    "work_location": {"type": "string"},
                    "asset_type": {
                        "type": "string",
                        "description": "EmailAddress, NetworkAccount, Phone, SocialNetwork, or WebSite",
                    },
                    "asset_value": {"type": "string"},
                    **_owner_prop,
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_threatconnect_workflow",
            description=(
                "Manage Workflow Case items. action: 'add_note' (case_id + "
                "text), 'list_notes', 'add_artifact' (case_id + summary + "
                "artifact_type e.g. Host, IP Address, URL, File Hash), "
                "'list_artifacts', 'add_task' (case_id + task_name + optional "
                "text/due_date), 'list_tasks', 'update_task' (item_id + "
                "task_status/task_name/due_date), 'list_templates' (workflow "
                "templates)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "add_note",
                            "list_notes",
                            "add_artifact",
                            "list_artifacts",
                            "add_task",
                            "list_tasks",
                            "update_task",
                            "list_templates",
                        ],
                    },
                    "case_id": {"type": "string"},
                    "item_id": {
                        "type": "string",
                        "description": "Task ID for update_task",
                    },
                    "text": {
                        "type": "string",
                        "description": "Note text / task description",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Artifact value for add_artifact",
                    },
                    "artifact_type": {"type": "string"},
                    "task_name": {"type": "string"},
                    "task_status": {
                        "type": "string",
                        "description": "Not Started, In Progress, Completed, Waiting on Someone, Deferred",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "ISO 8601 due date for tasks",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_threatconnect_playbooks",
            description=(
                "Manage Playbooks. action: 'activate' / 'deactivate' (turn a "
                "playbook on/off by playbook_id), 'executions' (list recent "
                "playbook executions, optional TQL filter)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["activate", "deactivate", "executions"],
                    },
                    "playbook_id": {"type": "string"},
                    "tql": {"type": "string"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
