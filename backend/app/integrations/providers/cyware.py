# SPDX-License-Identifier: Apache-2.0

"""Cyware Intel Exchange (CTIX) integration – threat intelligence platform.

CTIX v3 Open API integration covering the full consume → curate →
produce loop for an agentic security team:

  - Threat data: CQL search across every STIX object (indicators, malware,
    threat actors, vulnerabilities, campaigns, …), object detail, relations
  - Context: tags, feed sources, published collections, subscribers, and
    saved searches
  - Intel production (writes): quick-add intel from IOCs/SDOs, free-text
    IOC parsing (submit → poll → results), tag creation
  - IOC curation (writes): bulk tag add/remove and bulk curation actions
    (false-positive, deprecate, allowed-list, watchlist, analyst score/TLP,
    manual rule runs)
  - Knowledge reads: threat bulletins, notes, tasks, automation rules,
    allowed indicators, watchlist keywords, enrichment verdicts, ATT&CK
    navigator
  - Long tail: consolidated admin reads (``manage_cyware_admin``) and the
    destructive ``delete_cyware_object``

Authentication
--------------
CTIX Open API signs every request with **HMAC-SHA1 query parameters** —
there is no bearer token. Each request carries three query params:

  - ``AccessID``: the Open API access ID
  - ``Expires``: unix epoch seconds, at most 30 seconds in the future
  - ``Signature``: ``base64(HMAC_SHA1(secret_key, "<access_id>\\n<expires>"))``

``integration_tools._get_token`` assembles the compound
``base_url||access_id||secret_key`` token this builder parses. The base URL
is the tenant's Open API endpoint URL copied from Administration >
Integration Management > CTIX Integrators (usually ends with ``/ctixapi``,
e.g. ``https://tenant.cyware.com/ctixapi``); API paths are appended to it
directly. The raw base64 signature is passed through httpx ``params`` so it
is percent-encoded exactly once.

Exports ``cyware_api_base()`` / ``build_cyware_auth_params()`` /
``validate_connection()`` shared with the connectivity test.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json as _json
import logging
import time
from typing import Any
from urllib.parse import quote

from app.core.egress import egress_client
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "Cyware"
_SOURCE = "integration:cyware"
_DEFAULT_TIMEOUT = 30

# CTIX paginates with page/page_size; keep page sizes sane for LLM output.
_MAX_PAGE_SIZE = 100

# Signature validity window (the platform caps Expires at 30 seconds).
_SIGNATURE_TTL_SECONDS = 20

# IOC keys accepted by the quick-add ``indicators`` object.
_INDICATOR_KEYS: tuple[str, ...] = (
    "ipv4-addr",
    "ipv6-addr",
    "domain",
    "url",
    "email",
    "md5",
    "sha1",
    "sha224",
    "sha256",
    "sha384",
    "sha512",
    "ssdeep",
)

# SDO keys accepted by the quick-add ``sdos`` object.
_SDO_KEYS: tuple[str, ...] = (
    "vulnerability",
    "malware",
    "campaign",
    "threat-actor",
    "intrusion-set",
    "attack-pattern",
    "incident",
    "course-of-action",
    "identity",
    "tool",
    "infrastructure",
    "location",
    "malware-analysis",
)

# Bulk curation action → the ``data`` payload key it requires (None = no data).
_BULK_ACTIONS: dict[str, str | None] = {
    "false_positive": None,
    "un_false_positive": None,
    "deprecate": None,
    "un_deprecate": None,
    "whitelist": None,
    "un_whitelist": None,
    "manual_review": None,
    "reviewed": None,
    "watchlist": "name",
    "un_watchlist": "name",
    "analyst_score": "analyst_score",
    "analyst_tlp": "analyst_tlp",
    "run_rule": "id",
}

# Parse-task status codes (GET conversion/tasks/{task_id}/).
_PARSE_STATUS: dict[int, str] = {
    0: "enqueued",
    1: "running",
    2: "failed",
    3: "done",
    5: "delayed",
    6: "cancelled",
    7: "aborted",
}

# manage_cyware_admin area → (path, is_paginated).
_ADMIN_AREAS: dict[str, tuple[str, bool]] = {
    "heartbeat": ("/publishing/heartbeat/", False),
    "tenant_config": ("/conversion/tenant/config/", False),
    "license": ("/rest-auth/tenant-permit/", False),
    "rate_limits": ("/rest-auth/rate-limit/openapi/", False),
    "taxii_config": ("/publishing/config/", False),
    "enrichment_tools": ("/integration/apps/actions/", True),
    "users": ("/rest-auth/users/", True),
    "user_groups": ("/rest-auth/groups/", True),
    "audit_logs": ("/event-logs/audit-logs/", True),
    "dashboards": ("/ingestion/analytics/dashboard/", True),
    "reports": ("/ingestion/reports/", True),
    "intel_history": ("/conversion/intel-history/", True),
    "ioc_types": ("/ingestion/utilities/ioc-types/", True),
    "source_collections": ("/conversion/feed-sources/collection/", True),
    "threat_mailbox": ("/conversion/feed-sources/email/inbox/", True),
    "rss_articles": ("/conversion/feed-sources/rss/articles/", True),
    "me": ("/rest-auth/user-details/", False),
}

# delete_cyware_object kind → path template.
_DELETE_KINDS: dict[str, str] = {
    "note": "/ingestion/notes/{id}/",
    "task": "/ingestion/tasks/{id}/",
    "saved_search": "/ingestion/saved-searches/{id}/",
    "allowed_indicator": "/conversion/allowed_indicators/{id}/",
    "watchlist_keyword": "/ingestion/watchlist/{id}/",
    "report": "/ingestion/reports/{id}/",
    "dashboard": "/ingestion/analytics/dashboard/{id}/",
}

# STIX Domain Object types accepted by the threat-data object endpoints
# (the CQL ``type`` field uses the same values).
_OBJECT_TYPES: tuple[str, ...] = (
    "indicator",
    "attack-pattern",
    "campaign",
    "course-of-action",
    "identity",
    "infrastructure",
    "intrusion-set",
    "location",
    "malware",
    "malware-analysis",
    "observed-data",
    "opinion",
    "report",
    "threat-actor",
    "tool",
    "vulnerability",
)


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with the connectivity test)
# ---------------------------------------------------------------------------


def cyware_api_base(base_url: str) -> str:
    """Return the CTIX Open API base URL with no trailing slash.

    The stored base URL is the tenant's Open API endpoint URL exactly as
    copied from CTIX Integrators (typically ``https://<tenant>.cyware.com/
    ctixapi``) — deployments vary, so no path suffix is forced. Strips
    whitespace and trailing slashes so a pasted URL can never produce a
    doubled separator. Returns ``""`` when no base URL is configured.
    """
    return (base_url or "").strip().rstrip("/")


def build_cyware_auth_params(
    access_id: str,
    secret_key: str,
    now: int | None = None,
) -> dict[str, str]:
    """Build the CTIX ``AccessID``/``Expires``/``Signature`` query params.

    The signature is ``base64(HMAC_SHA1(secret_key, "<access_id>\\n<expires>"))``
    with ``expires`` = now + 20 seconds (the platform caps the window at 30).
    The raw base64 value is returned unencoded — pass the dict through httpx
    ``params`` so it is percent-encoded exactly once. Credentials are
    stripped so pasted whitespace can never corrupt the HMAC.
    """
    ts = int(now if now is not None else time.time())
    expires = ts + _SIGNATURE_TTL_SECONDS
    to_sign = f"{access_id.strip()}\n{expires}"
    digest = hmac.new(
        secret_key.strip().encode("utf-8"),
        to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")
    return {
        "AccessID": access_id.strip(),
        "Signature": signature,
        "Expires": str(expires),
    }


async def validate_connection(
    base_url: str, access_id: str, secret_key: str
) -> tuple[bool, str]:
    """Verify CTIX Open API credentials with the vendor's ping probe.

    Returns ``(ok, message)``. A successful ``GET /ping/`` proves the
    endpoint URL, Access ID, and Secret Key are all valid (CTIX answers
    ``{"result": "pong"}``). Used by the integration connectivity test.
    Routes through the shared ``safe_request`` (429 retry + standardized
    errors) with ``expected_statuses`` so an auth failure can be told apart
    from a network error, and rejects 2xx responses that are not JSON.
    """
    api = cyware_api_base(base_url)
    if not api:
        return False, (
            "Missing base URL (the Open API endpoint URL from CTIX "
            "Integrators, e.g. https://tenant.cyware.com/ctixapi)."
        )
    if not access_id or not secret_key:
        return False, "Missing Access ID or Secret Key."
    async with egress_client(timeout=15) as client:
        resp, err = await safe_request(
            client,
            "get",
            f"{api}/ping/",
            _PROVIDER,
            "validate credentials",
            params=build_cyware_auth_params(access_id, secret_key),
            expected_statuses={401, 403},
        )
        if resp is not None and resp.status_code in (401, 403):
            return False, (
                "Authentication failed — check the Access ID and Secret Key, "
                "confirm the credentials have not expired, and verify this "
                "host is in the credential's allowed IPs (if configured)."
            )
        if err or resp is None:
            return False, err or "Could not connect to Cyware Intel Exchange."
        try:
            resp.json()
        except Exception:
            # A 2xx that is not JSON is not a CTIX Open API response (e.g. a
            # proxy or SSO portal returning HTML) — never report success.
            return False, (
                "Unexpected non-JSON response from the Cyware API — check "
                "that the base URL is the Open API endpoint URL from CTIX "
                "Integrators (usually ending in /ctixapi)."
            )
    return True, "Cyware Intel Exchange is connected and authenticated."


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _epoch(value: Any) -> str:
    """Render a CTIX epoch timestamp (or ISO string) as a compact UTC date."""
    if value in (None, "", 0):
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(float(value))))
    except (TypeError, ValueError):
        return str(value)[:19]


def _clamp_page_size(page_size: Any) -> int:
    """Clamp a requested page size to the [1, 100] bounds actually sent."""
    return max(1, min(int(page_size or 10), _MAX_PAGE_SIZE))


def _clamp_page(page: Any) -> int:
    return max(1, int(page or 1))


def _results(payload: dict | None) -> list[dict]:
    """Extract the item list from a CTIX paginated envelope.

    CTIX responses are DRF-style ``{next, previous, results: [...]}``; a few
    endpoints nest the same envelope under ``data``.
    """
    body = payload or {}
    if isinstance(body.get("data"), dict):
        body = body["data"]
    items = body.get("results")
    if isinstance(items, list):
        return [i for i in items if isinstance(i, dict)]
    if isinstance(body.get("data"), list):
        return [i for i in body["data"] if isinstance(i, dict)]
    return []


def _pagination_note(payload: dict | None, count: int, page: int, page_size: int) -> str:
    """Return a next-page hint when the response advertises more results."""
    body = payload or {}
    if isinstance(body.get("data"), dict):
        body = body["data"]
    if body.get("next") or count >= _clamp_page_size(page_size):
        return f"\n[More results may be available — pass page={_clamp_page(page) + 1}]"
    return ""


def _flags(obj: dict) -> str:
    flags = []
    if obj.get("is_deprecated"):
        flags.append("deprecated")
    if obj.get("is_false_positive"):
        flags.append("false-positive")
    if obj.get("is_whitelisted"):
        flags.append("allowed-list")
    if obj.get("is_revoked"):
        flags.append("revoked")
    if obj.get("is_under_review") or obj.get("is_under_manual_review"):
        flags.append("under-review")
    return f" [{', '.join(flags)}]" if flags else ""


def _fmt_threat_object(o: dict) -> str:
    label = o.get("name") or o.get("primary_attribute") or o.get("value") or "?"
    otype = o.get("type") or o.get("sub_type") or "?"
    sub = f"/{o.get('ioc_type')}" if o.get("ioc_type") else ""
    score = o.get("confidence_score")
    analyst = o.get("analyst_score")
    score_str = f"{score}" if score is not None else "N/A"
    if analyst is not None:
        score_str += f" (analyst: {analyst})"
    return (
        f"  [{otype}{sub}] {_trunc(label, 100)}{_flags(o)}\n"
        f"    id: {o.get('id', '?')} | Risk: {score_str} | "
        f"TLP: {o.get('tlp') or o.get('analyst_tlp') or 'NONE'} | "
        f"Created: {_epoch(o.get('ctix_created') or o.get('created'))} | "
        f"Modified: {_epoch(o.get('ctix_modified') or o.get('modified'))}"
    )


def _fmt_tag(t: dict) -> str:
    who = _who(t.get("created_by"))
    return (
        f"  {t.get('name', '?')} [{t.get('tag_type', 'user')}] — "
        f"id: {t.get('id', '?')}"
        + (f" | By: {who}" if who else "")
        + (f" | Created: {_epoch(t.get('created'))}" if t.get("created") else "")
    )


def _fmt_source(s: dict) -> str:
    return (
        f"  {_trunc(s.get('name'), 80)} — id: {s.get('id', '?')}"
        + (f" | Type: {s.get('source_type')}" if s.get("source_type") else "")
        + (f" | Created: {_epoch(s.get('created'))}" if s.get("created") else "")
    )


def _fmt_collection(c: dict) -> str:
    active = "active" if c.get("is_active", True) else "inactive"
    return (
        f"  {_trunc(c.get('name'), 80)} [{active}] — id: {c.get('id', '?')}"
        + (f" | {_trunc(c.get('description'), 100)}" if c.get("description") else "")
    )


def _fmt_subscriber(s: dict) -> str:
    active = "active" if s.get("active", s.get("is_active", True)) else "inactive"
    return (
        f"  {_trunc(s.get('name') or s.get('username'), 80)} [{active}] — "
        f"id: {s.get('id', '?')}"
        + (
            f" | Org: {_trunc(s.get('organization_name'), 60)}"
            if s.get("organization_name")
            else ""
        )
    )


def _fmt_saved_search(s: dict) -> str:
    meta = s.get("meta_data")
    if not isinstance(meta, dict):
        meta = {}
    return (
        f"  {_trunc(s.get('name'), 80)} — id: {s.get('id', '?')}\n"
        f"    Query: {_trunc(meta.get('query') or s.get('query'), 160)}"
        + (f" | {_trunc(s.get('description'), 80)}" if s.get("description") else "")
    )


def _fmt_relation(r: dict) -> str:
    rel_obj = r.get("object") or r.get("related_object") or {}
    if not isinstance(rel_obj, dict):
        rel_obj = {}
    label = (
        rel_obj.get("name")
        or rel_obj.get("primary_attribute")
        or r.get("name")
        or "?"
    )
    rtype = r.get("relationship_type") or r.get("relation_type") or "related-to"
    otype = rel_obj.get("type") or r.get("object_type") or "?"
    ident = rel_obj.get("id") or r.get("object_id") or r.get("id") or "?"
    return f"  --{rtype}--> [{otype}] {_trunc(label, 90)} (id: {ident})"


def _who(created_by: Any) -> str:
    if not isinstance(created_by, dict):
        return ""
    return (
        f"{created_by.get('first_name', '')} {created_by.get('last_name', '')}".strip()
        or created_by.get("email", "")
    )


def _fmt_bulletin(b: dict) -> str:
    title = (b.get("metadata") or {}).get("title") if isinstance(
        b.get("metadata"), dict
    ) else None
    title = title or b.get("title") or b.get("name") or "?"
    state = "published" if b.get("published") else "draft"
    return (
        f"  {_trunc(title, 100)} [{state}] — id: {b.get('id', '?')}\n"
        f"    TLP: {b.get('tlp', 'NONE')} | By: {_who(b.get('created_by')) or '?'} | "
        f"Created: {_epoch(b.get('created'))}"
    )


def _fmt_note(n: dict) -> str:
    text = n.get("title") or n.get("text") or ""
    target = (
        f" | On: {n.get('component')}/{n.get('object_id')}"
        if n.get("object_id")
        else ""
    )
    return (
        f"  {_trunc(text, 120)} — id: {n.get('id', '?')}{target}\n"
        f"    By: {_who(n.get('created_by')) or '?'} | Created: {_epoch(n.get('created'))}"
    )


def _fmt_task(t: dict) -> str:
    label = t.get("name") or t.get("title") or t.get("description") or "?"
    return (
        f"  {_trunc(label, 100)} — id: {t.get('id', '?')}"
        + (f" | Status: {t.get('status')}" if t.get("status") else "")
        + (f" | Due: {_epoch(t.get('due_date'))}" if t.get("due_date") else "")
        + (f" | Assignee: {_who(t.get('assignee'))}" if t.get("assignee") else "")
    )


def _fmt_rule(r: dict) -> str:
    active = "active" if r.get("is_active", True) else "inactive"
    return (
        f"  {_trunc(r.get('name'), 90)} [{active}] — id: {r.get('id', '?')}"
        + (f" | {_trunc(r.get('description'), 100)}" if r.get("description") else "")
    )


def _fmt_allowed(a: dict) -> str:
    label = a.get("value") or a.get("name") or a.get("primary_attribute") or "?"
    return (
        f"  {_trunc(label, 100)} [{a.get('type', '?')}] — id: {a.get('id', '?')}"
        + (f" | Created: {_epoch(a.get('created'))}" if a.get("created") else "")
    )


def _fmt_watchlist(w: dict) -> str:
    active = "active" if w.get("is_active", True) else "inactive"
    label = w.get("keyword") or w.get("name") or w.get("value") or "?"
    return (
        f"  {_trunc(label, 100)} [{active}] — id: {w.get('id', '?')}"
        + (f" | Created: {_epoch(w.get('created'))}" if w.get("created") else "")
    )


def _fmt_technique(t: dict) -> str:
    tactics = t.get("tactics")
    if isinstance(tactics, list):
        tactics = ", ".join(
            str(x.get("name") if isinstance(x, dict) else x) for x in tactics[:6]
        )
    return (
        f"  {t.get('id', '?')}: {_trunc(t.get('name'), 90)}"
        + (f" | Tactics: {tactics}" if tactics else "")
        + (f" | Related APTs: {t.get('apt_count')}" if t.get("apt_count") else "")
    )


def _fmt_generic(item: dict) -> str:
    """Compact one-line summary for arbitrary CTIX objects (admin areas)."""
    ident = item.get("id", "")
    label = (
        item.get("name")
        or item.get("title")
        or item.get("email")
        or item.get("subject")
        or item.get("api_name")
        or ""
    )
    parts = [f"  {_trunc(label, 90)}" if label else "  (unnamed)"]
    if ident:
        parts[0] += f" — id: {ident}"
    detail_bits = []
    for key in ("type", "status", "source_type", "is_active", "created"):
        if item.get(key) not in (None, ""):
            value = _epoch(item[key]) if key == "created" else item[key]
            detail_bits.append(f"{key}: {value}")
    if detail_bits:
        parts.append(f"    {' | '.join(detail_bits)}")
    if not label and not detail_bits:
        parts.append(f"    {_trunc(_json.dumps(item, default=str), 220)}")
    return "\n".join(parts)


def _parse_json_object(raw: str, param: str) -> tuple[dict | None, str | None]:
    """Parse a JSON-object tool argument, returning ``(value, error)``."""
    if not (raw or "").strip():
        return {}, None
    try:
        value = _json.loads(raw)
    except ValueError:
        return None, f"Error: {param} must be a valid JSON object."
    if not isinstance(value, dict):
        return None, f"Error: {param} must be a JSON object, not {type(value).__name__}."
    return value, None


def _csv_list(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _quick_add_values(value: Any) -> str:
    """Normalise a quick-add map value to the comma-separated string CTIX wants.

    The documented shape is ``{"ipv4-addr": "1.2.3.4,5.6.7.8"}``, but LLMs
    naturally pass JSON arrays — join them instead of sending ``str(list)``.
    """
    if isinstance(value, list):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value)


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Cyware CTIX tools from a ``base_url||access_id||secret_key`` token.

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

    api = cyware_api_base(base_url)

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[dict | None, str | None]:
        """Signed request helper returning ``(payload, error)``.

        Merges the per-request HMAC auth params into the query and routes
        through the shared ``safe_request`` (429 retry + standardized
        errors). ``path`` starts with ``/`` and ends with ``/`` (CTIX routes
        use trailing slashes).
        """
        if not secret_key:
            return None, (
                "Error: the Cyware Secret Key is not configured. Set the "
                "Secret Key in the integration's API Key field."
            )
        query: dict[str, Any] = {
            k: v for k, v in (params or {}).items() if v is not None and v != ""
        }
        query.update(build_cyware_auth_params(access_id, secret_key))
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{api}{path}",
                _PROVIDER,
                action,
                params=query,
                json=json,
            )
            if err:
                return None, err
            try:
                payload = resp.json()
            except Exception:
                # A 2xx with an empty body is a legitimate success for some
                # actions (e.g. DELETE); anything else non-JSON is not a
                # CTIX Open API response.
                if not (getattr(resp, "content", b"") or b"").strip():
                    return {}, None
                return None, (
                    f"Error: {_PROVIDER} returned an unexpected non-JSON "
                    f"response to {action} — check that the base URL is the "
                    "Open API endpoint URL from CTIX Integrators."
                )
        if isinstance(payload, dict):
            return payload, None
        if isinstance(payload, list):
            return {"results": payload}, None
        return {"result": payload}, None

    def _page_params(
        page: int, page_size: int, extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": _clamp_page(page),
            "page_size": _clamp_page_size(page_size),
        }
        if extra:
            params.update(extra)
        return params

    # ══════════════════════════════════════════════════════════════════════
    # 1. THREAT DATA (CQL search + object detail + relations)
    # ══════════════════════════════════════════════════════════════════════

    async def search_cyware_threat_data(
        query: str = "",
        page: int = 1,
        page_size: int = 10,
    ) -> str:
        """Search threat data objects with a CQL query (empty = most recent)."""
        payload, err = await _api(
            "post",
            "/ingestion/threat-data/list/",
            "search threat data",
            params=_page_params(page, page_size),
            json={"query": query.strip()},
        )
        if err:
            return err
        items = _results(payload)
        out = format_entity_list(
            items,
            _fmt_threat_object,
            noun="threat data object",
            empty_msg=(
                "No threat data objects matched. Check the CQL field names "
                "(e.g. type, ioc_type, value, confidence_score, tlp, tags) "
                "and operators (=, !=, >, <, IN, CONTAINS, AND, OR)."
            ),
        )
        return out + _pagination_note(payload, len(items), page, page_size)

    async def get_cyware_object(object_type: str, object_id: str) -> str:
        """Get the correlated basic details of one threat data object."""
        otype = object_type.strip().lower()
        oid = object_id.strip()
        if not oid:
            return "Error: object_id is required (a threat data object UUID)."
        if otype not in _OBJECT_TYPES and not otype.startswith("x-"):
            return (
                f"Error: unknown object_type '{object_type}'. Use one of: "
                f"{', '.join(_OBJECT_TYPES)} (or a custom x-* type)."
            )
        payload, err = await _api(
            "get",
            f"/ingestion/threat-data/{quote(otype, safe='')}/{quote(oid, safe='')}/basic/",
            f"get {otype} {oid}",
        )
        if err:
            return err
        obj = payload or {}
        if isinstance(obj.get("data"), dict):
            obj = obj["data"]
        if not obj:
            return f"Object {oid} ({otype}) not found."
        label = obj.get("name") or obj.get("primary_attribute") or "?"
        lines = [
            f"{otype}: {_trunc(label, 150)}{_flags(obj)}",
            f"ID: {obj.get('id', oid)}"
            + (f" | IOC type: {obj.get('ioc_type')}" if obj.get("ioc_type") else ""),
            f"Risk score: {obj.get('confidence_score', 'N/A')}"
            + (
                f" | Analyst score: {obj.get('analyst_score')}"
                if obj.get("analyst_score") is not None
                else ""
            )
            + f" | TLP: {obj.get('tlp') or obj.get('analyst_tlp') or 'NONE'}"
            + (f" | Severity: {obj.get('severity')}" if obj.get("severity") else ""),
            f"Created: {_epoch(obj.get('ctix_created') or obj.get('created'))} | "
            f"Modified: {_epoch(obj.get('ctix_modified') or obj.get('modified'))}"
            + (
                f" | First seen: {_epoch(obj.get('first_seen'))}"
                if obj.get("first_seen")
                else ""
            )
            + (
                f" | Last seen: {_epoch(obj.get('last_seen'))}"
                if obj.get("last_seen")
                else ""
            ),
        ]
        if obj.get("analyst_description") or obj.get("description"):
            lines.append(
                "Description: "
                f"{_trunc(obj.get('analyst_description') or obj.get('description'), 500)}"
            )
        if obj.get("country"):
            lines.append(f"Country: {obj.get('country')}")
        sources = obj.get("sources") or []
        if isinstance(sources, list) and sources:
            names = ", ".join(
                _trunc(s.get("name"), 40) for s in sources[:10] if isinstance(s, dict)
            )
            lines.append(f"Sources ({len(sources)}): {names}")
        for key, title in (
            ("source_collections", "Source collections"),
            ("published_collections", "Published collections"),
        ):
            values = obj.get(key) or []
            if isinstance(values, list) and values:
                names = ", ".join(
                    _trunc(v.get("name"), 40) for v in values[:10] if isinstance(v, dict)
                )
                lines.append(f"{title} ({len(values)}): {names}")
        tags = obj.get("tags") or []
        if isinstance(tags, list) and tags:
            names = ", ".join(
                _trunc(t.get("name") if isinstance(t, dict) else t, 40)
                for t in tags[:15]
            )
            lines.append(f"Tags: {names}")
        return "\n".join(lines)

    async def get_cyware_object_relations(
        object_type: str,
        object_id: str,
        page: int = 1,
        page_size: int = 25,
    ) -> str:
        """List the related threat data objects of one object."""
        otype = object_type.strip().lower()
        oid = object_id.strip()
        if not oid:
            return "Error: object_id is required (a threat data object UUID)."
        if otype not in _OBJECT_TYPES and not otype.startswith("x-"):
            return (
                f"Error: unknown object_type '{object_type}'. Use one of: "
                f"{', '.join(_OBJECT_TYPES)} (or a custom x-* type)."
            )
        payload, err = await _api(
            "get",
            f"/ingestion/threat-data/{quote(otype, safe='')}/{quote(oid, safe='')}/relations/",
            f"list relations of {otype} {oid}",
            params=_page_params(page, page_size),
        )
        if err:
            return err
        items = _results(payload)
        out = format_entity_list(
            items,
            _fmt_relation,
            noun="relation",
            empty_msg=f"No relations found for {otype} {oid}.",
        )
        return out + _pagination_note(payload, len(items), page, page_size)

    # ══════════════════════════════════════════════════════════════════════
    # 2. CONTEXT (tags, sources, collections, subscribers, saved searches)
    # ══════════════════════════════════════════════════════════════════════

    async def list_cyware_tags(
        tag_type: str = "",
        page: int = 1,
        page_size: int = 25,
    ) -> str:
        """List tags (returns the tag IDs used by the tagging/bulk tools)."""
        extra: dict[str, Any] = {}
        if tag_type.strip():
            extra["tag_type"] = tag_type.strip().lower()
        payload, err = await _api(
            "get",
            "/ingestion/tags/",
            "list tags",
            params=_page_params(page, page_size, extra),
        )
        if err:
            return err
        items = _results(payload)
        out = format_entity_list(items, _fmt_tag, noun="tag")
        return out + _pagination_note(payload, len(items), page, page_size)

    async def list_cyware_sources(
        source_type: str = "",
        page: int = 1,
        page_size: int = 25,
    ) -> str:
        """List the threat intel feed sources configured in Intel Exchange."""
        extra: dict[str, Any] = {}
        if source_type.strip():
            extra["source_type"] = source_type.strip().upper()
        payload, err = await _api(
            "get",
            "/conversion/feed-sources/",
            "list feed sources",
            params=_page_params(page, page_size, extra),
        )
        if err:
            return err
        items = _results(payload)
        out = format_entity_list(items, _fmt_source, noun="feed source")
        return out + _pagination_note(payload, len(items), page, page_size)

    async def list_cyware_collections(
        q: str = "",
        active_only: bool = False,
        page: int = 1,
        page_size: int = 25,
    ) -> str:
        """List the STIX collections that intel is published to."""
        extra: dict[str, Any] = {}
        if q.strip():
            extra["q"] = q.strip()
        if active_only:
            extra["is_active"] = "true"
        payload, err = await _api(
            "get",
            "/publishing/collection/",
            "list collections",
            params=_page_params(page, page_size, extra),
        )
        if err:
            return err
        items = _results(payload)
        out = format_entity_list(items, _fmt_collection, noun="collection")
        return out + _pagination_note(payload, len(items), page, page_size)

    async def list_cyware_subscribers(
        organization: str = "",
        active_only: bool = False,
        page: int = 1,
        page_size: int = 25,
    ) -> str:
        """List the subscribers that intel is shared with."""
        extra: dict[str, Any] = {}
        if organization.strip():
            extra["organization_name"] = organization.strip()
        if active_only:
            extra["active"] = "true"
        payload, err = await _api(
            "get",
            "/publishing/subscriber/",
            "list subscribers",
            params=_page_params(page, page_size, extra),
        )
        if err:
            return err
        items = _results(payload)
        out = format_entity_list(items, _fmt_subscriber, noun="subscriber")
        return out + _pagination_note(payload, len(items), page, page_size)

    async def list_cyware_saved_searches(
        page: int = 1,
        page_size: int = 25,
    ) -> str:
        """List saved threat data searches (reusable CQL queries)."""
        payload, err = await _api(
            "get",
            "/ingestion/saved-searches/",
            "list saved searches",
            params=_page_params(page, page_size),
        )
        if err:
            return err
        items = _results(payload)
        out = format_entity_list(items, _fmt_saved_search, noun="saved search")
        return out + _pagination_note(payload, len(items), page, page_size)

    # ══════════════════════════════════════════════════════════════════════
    # 3. INTEL PRODUCTION (writes)
    # ══════════════════════════════════════════════════════════════════════

    async def create_cyware_intel(
        title: str = "",
        description: str = "",
        indicators: str = "",
        sdos: str = "",
        observables: str = "",
        tlp: str = "",
        confidence: int | None = None,
        tag_ids: str = "",
        publish: bool = True,
    ) -> str:
        """Create intel (quick add) from IOCs and/or SDOs."""
        ioc_map, err = _parse_json_object(indicators, "indicators")
        if err:
            return err
        sdo_map, err = _parse_json_object(sdos, "sdos")
        if err:
            return err
        sco_map, err = _parse_json_object(observables, "observables")
        if err:
            return err
        if not ioc_map and not sdo_map and not sco_map:
            return (
                "Error: at least one of indicators, sdos, or observables is "
                'required, e.g. indicators=\'{"ipv4-addr": "1.2.3.4"}\' or '
                'sdos=\'{"malware": "Emotet"}\'.'
            )
        bad_iocs = [k for k in ioc_map if k not in _INDICATOR_KEYS]
        if bad_iocs:
            return (
                f"Error: unknown indicator key(s) {', '.join(bad_iocs)}. Use: "
                f"{', '.join(_INDICATOR_KEYS)}."
            )
        bad_sdos = [k for k in sdo_map if k not in _SDO_KEYS]
        if bad_sdos:
            return (
                f"Error: unknown SDO key(s) {', '.join(bad_sdos)}. Use: "
                f"{', '.join(_SDO_KEYS)}."
            )
        tlp_value = tlp.strip().upper()
        if tlp_value and tlp_value not in ("RED", "AMBER", "GREEN", "WHITE", "NONE"):
            return "Error: tlp must be one of RED, AMBER, GREEN, WHITE, NONE."
        metadata: dict[str, Any] = {"is_apply_all": True}
        if tlp_value:
            metadata["tlp"] = tlp_value
        if confidence is not None:
            metadata["confidence"] = max(0, min(int(confidence), 100))
        if description.strip():
            metadata["description"] = description.strip()[:1000]
        labels = _csv_list(tag_ids)
        if labels:
            metadata["labels"] = labels
        body: dict[str, Any] = {
            "metadata": metadata,
            "create_intel_feed": bool(publish),
        }
        if title.strip():
            body["title"] = title.strip()[:100]
        if ioc_map:
            body["indicators"] = {k: _quick_add_values(v) for k, v in ioc_map.items()}
        if sdo_map:
            body["sdos"] = {k: _quick_add_values(v) for k, v in sdo_map.items()}
        if sco_map:
            body["observables"] = {k: _quick_add_values(v) for k, v in sco_map.items()}
        payload, err = await _api(
            "post",
            "/conversion/quick-intel/create-stix/",
            "create intel",
            json=body,
        )
        if err:
            return err
        task_id = (payload or {}).get("task_id", "")
        return (
            "Intel submitted to the creation queue"
            + (f" (task id: {task_id})" if task_id else "")
            + ". The objects will appear in threat data once processing "
            "completes — verify with search_cyware_threat_data."
        )

    async def parse_cyware_iocs(
        text: str,
        is_url: bool = False,
        wait_seconds: int = 20,
    ) -> str:
        """Extract IOCs from free text or a web page (submit + poll + results)."""
        data = (text or "").strip()
        if not data:
            return "Error: text is required (free text or a web page URL)."
        payload, err = await _api(
            "post",
            "/conversion/quick-intel/free-text/",
            "submit IOC parsing task",
            json={"data": data[:50000], "url": bool(is_url)},
        )
        if err:
            return err
        body = payload or {}
        task_id = body.get("task_id")
        if not task_id and isinstance(body.get("details"), dict):
            task_id = body["details"].get("task_id")
        if not task_id:
            return (
                "Error: Cyware did not return a task_id for the parsing task. "
                f"Response: {_trunc(_json.dumps(payload, default=str), 300)}"
            )
        deadline = time.monotonic() + max(5, min(int(wait_seconds or 20), 60))
        status_label = "enqueued"
        while time.monotonic() < deadline:
            status_payload, err = await _api(
                "get",
                f"/conversion/tasks/{task_id}/",
                "poll IOC parsing task",
            )
            if err:
                return err
            raw_status = (status_payload or {}).get("status")
            fallback = str(raw_status) if raw_status not in (None, "") else "unknown"
            if isinstance(raw_status, str) and raw_status:
                status_label = raw_status.lower()
            else:
                try:
                    status_label = _PARSE_STATUS.get(
                        int((status_payload or {}).get("num_status", -1)),
                        fallback,
                    )
                except (TypeError, ValueError):
                    status_label = fallback
            if status_label == "done":
                break
            if status_label in ("failed", "cancelled", "aborted"):
                return f"IOC parsing task {task_id} ended with status: {status_label}."
            await asyncio.sleep(2)
        if status_label != "done":
            return (
                f"IOC parsing task {task_id} is still {status_label}. Retry "
                "shortly with a longer wait_seconds, or re-submit."
            )
        parsed, err = await _api(
            "get",
            f"/conversion/tasks/{task_id}/parse-iocs/",
            "fetch parsed IOCs",
            params={"page": 1, "page_size": _MAX_PAGE_SIZE},
        )
        if err:
            return err
        body = parsed or {}
        if isinstance(body.get("data"), dict):
            body = body["data"]
        lines: list[str] = []
        total = 0
        for ioc_type, values in body.items():
            if not isinstance(values, list) or not values:
                continue
            rendered = []
            for v in values[:50]:
                if isinstance(v, dict):
                    val = str(v.get("value", "?"))
                    if v.get("is_whitelisted"):
                        val += " (on allowed list)"
                    rendered.append(val)
                else:
                    rendered.append(str(v))
            if rendered:
                total += len(rendered)
                lines.append(f"  {ioc_type}: {', '.join(rendered)}")
        if not lines:
            return f"IOC parsing task {task_id} completed but found no IOCs."
        return (
            f"Parsed {total} IOC(s) (task id: {task_id}):\n"
            + "\n".join(lines)
            + "\n[Use create_cyware_intel to add these to Intel Exchange]"
        )

    async def create_cyware_tags(names: str, tag_type: str = "user") -> str:
        """Create up to 15 tags in one request."""
        tag_names = _csv_list(names)
        if not tag_names:
            return "Error: names is required (comma-separated tag names)."
        if len(tag_names) > 15:
            return "Error: a maximum of 15 tags can be created per request."
        kind = tag_type.strip().lower() or "user"
        if kind not in ("user", "system", "privileged"):
            return "Error: tag_type must be one of user, system, privileged."
        payload, err = await _api(
            "post",
            "/ingestion/tags/bulk-actions/",
            "create tags",
            json={"name": ",".join(tag_names), "tag_type": kind, "action": "add"},
        )
        if err:
            return err
        result = (payload or {}).get("result") or (payload or {}).get("details")
        return (
            f"Created {len(tag_names)} tag(s): {', '.join(tag_names)}."
            + (f" {_trunc(result, 200)}" if isinstance(result, str) else "")
            + " Use list_cyware_tags to get their IDs."
        )

    # ══════════════════════════════════════════════════════════════════════
    # 4. IOC CURATION (bulk actions)
    # ══════════════════════════════════════════════════════════════════════

    async def tag_cyware_objects(
        action: str,
        object_type: str,
        object_ids: str,
        tag_ids: str,
    ) -> str:
        """Add or remove tags on up to 100 threat data objects."""
        mode = action.strip().lower()
        if mode not in ("add", "remove"):
            return "Error: action must be 'add' or 'remove'."
        ids = _csv_list(object_ids)
        tags = _csv_list(tag_ids)
        if not ids or not tags:
            return "Error: object_ids and tag_ids are both required (comma-separated)."
        if len(ids) > 100 or len(tags) > 50:
            return "Error: at most 100 object_ids and 50 tag_ids per request."
        action_type = "add_tag" if mode == "add" else "remove_tag"
        # The add and remove variants use different data keys (tag_id vs
        # tag_ids) per the CTIX v3 bulk-action reference.
        data = {"tag_id": tags} if mode == "add" else {"tag_ids": tags}
        payload, err = await _api(
            "post",
            f"/ingestion/threat-data/bulk-action/{action_type}/",
            f"{mode} tags",
            json={
                "object_type": object_type.strip().lower(),
                "object_ids": ids,
                "data": data,
            },
        )
        if err:
            return err
        message = (payload or {}).get("message") or "Action Successfully Executed"
        return (
            f"{'Added' if mode == 'add' else 'Removed'} {len(tags)} tag(s) "
            f"{'to' if mode == 'add' else 'from'} {len(ids)} object(s). {message}"
        )

    async def update_cyware_threat_data(
        action: str,
        object_type: str,
        object_ids: str,
        value: str = "",
    ) -> str:
        """Run a curation bulk action on up to 100 threat data objects."""
        action_type = action.strip().lower()
        if action_type not in _BULK_ACTIONS:
            return (
                f"Error: invalid action '{action}'. Use one of: "
                f"{', '.join(sorted(_BULK_ACTIONS))}."
            )
        ids = _csv_list(object_ids)
        if not ids:
            return "Error: object_ids is required (comma-separated object UUIDs)."
        if len(ids) > 100:
            return "Error: at most 100 object_ids per request."
        body: dict[str, Any] = {
            "object_type": object_type.strip().lower(),
            "object_ids": ids,
        }
        data_key = _BULK_ACTIONS[action_type]
        if data_key:
            raw = value.strip()
            if not raw:
                hint = {
                    "name": "a watchlist name",
                    "analyst_score": "a score between 0 and 100",
                    "analyst_tlp": "RED, AMBER, GREEN, WHITE, or NONE",
                    "id": "a rule ID (see list_cyware_rules)",
                }[data_key]
                return f"Error: action '{action_type}' requires value ({hint})."
            if data_key == "analyst_score":
                try:
                    body["data"] = {data_key: max(0, min(int(raw), 100))}
                except ValueError:
                    return "Error: value must be an integer score between 0 and 100."
            elif data_key == "analyst_tlp":
                tlp_value = raw.upper()
                if tlp_value not in ("RED", "AMBER", "GREEN", "WHITE", "NONE"):
                    return (
                        "Error: value must be one of RED, AMBER, GREEN, WHITE, NONE."
                    )
                body["data"] = {data_key: tlp_value}
            elif action_type in ("watchlist", "un_watchlist"):
                body["data"] = {data_key: _csv_list(raw)}
            else:
                body["data"] = {data_key: raw}
        payload, err = await _api(
            "post",
            f"/ingestion/threat-data/bulk-action/{action_type}/",
            f"bulk {action_type}",
            json=body,
        )
        if err:
            return err
        message = (payload or {}).get("message") or "Action Successfully Executed"
        return f"Bulk action '{action_type}' applied to {len(ids)} object(s). {message}"

    # ══════════════════════════════════════════════════════════════════════
    # 5. KNOWLEDGE (bulletins, notes, tasks, rules, allowed, watchlist,
    #    enrichment, ATT&CK)
    # ══════════════════════════════════════════════════════════════════════

    async def list_cyware_threat_bulletins(
        q: str = "",
        page: int = 1,
        page_size: int = 25,
    ) -> str:
        """List threat bulletins (finished intel reports)."""
        extra: dict[str, Any] = {}
        if q.strip():
            extra["q"] = q.strip()
        payload, err = await _api(
            "get",
            "/conversion/threat-bulletin/",
            "list threat bulletins",
            params=_page_params(page, page_size, extra),
        )
        if err:
            return err
        items = _results(payload)
        out = format_entity_list(items, _fmt_bulletin, noun="threat bulletin")
        return out + _pagination_note(payload, len(items), page, page_size)

    async def get_cyware_threat_bulletin(bulletin_id: str) -> str:
        """Get the full detail of one threat bulletin."""
        ident = bulletin_id.strip()
        if not ident:
            return "Error: bulletin_id is required."
        payload, err = await _api(
            "get",
            f"/conversion/threat-bulletin/{quote(ident, safe='')}/",
            f"get threat bulletin {ident}",
        )
        if err:
            return err
        b = payload or {}
        if isinstance(b.get("data"), dict):
            b = b["data"]
        if not b:
            return f"Threat bulletin {ident} not found."
        meta = b.get("metadata") if isinstance(b.get("metadata"), dict) else {}
        title = meta.get("title") or b.get("title") or b.get("name") or "?"
        lines = [
            f"Threat bulletin: {_trunc(title, 150)}",
            f"ID: {b.get('id', ident)} | "
            f"State: {'published' if b.get('published') else 'draft'} | "
            f"TLP: {b.get('tlp', 'NONE')}",
            f"By: {_who(b.get('created_by')) or '?'} | "
            f"Created: {_epoch(b.get('created'))} | "
            f"Modified: {_epoch(b.get('modified'))}"
            + (
                f" | Published: {_epoch(b.get('published_on'))}"
                if b.get("published_on")
                else ""
            ),
        ]
        if b.get("description"):
            lines.append(f"Content: {_trunc(b.get('description'), 1500)}")
        attachments = b.get("attachments") or []
        if isinstance(attachments, list) and attachments:
            lines.append(f"Attachments: {len(attachments)}")
        return "\n".join(lines)

    async def list_cyware_notes(
        object_id: str = "",
        q: str = "",
        page: int = 1,
        page_size: int = 25,
    ) -> str:
        """List analyst notes (global, or scoped to one threat data object)."""
        extra: dict[str, Any] = {}
        if object_id.strip():
            extra["object_id"] = object_id.strip()
        if q.strip():
            extra["q"] = q.strip()
        payload, err = await _api(
            "get",
            "/ingestion/notes/",
            "list notes",
            params=_page_params(page, page_size, extra),
        )
        if err:
            return err
        items = _results(payload)
        out = format_entity_list(items, _fmt_note, noun="note")
        return out + _pagination_note(payload, len(items), page, page_size)

    async def list_cyware_tasks(
        page: int = 1,
        page_size: int = 25,
    ) -> str:
        """List analyst tasks tracked in Intel Exchange."""
        payload, err = await _api(
            "get",
            "/ingestion/tasks/",
            "list tasks",
            params=_page_params(page, page_size),
        )
        if err:
            return err
        items = _results(payload)
        out = format_entity_list(items, _fmt_task, noun="task")
        return out + _pagination_note(payload, len(items), page, page_size)

    async def list_cyware_rules(
        rule_id: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 25,
    ) -> str:
        """List automation rules, or get one rule's detail by rule_id."""
        if rule_id.strip():
            payload, err = await _api(
                "get",
                f"/ingestion/rules/{quote(rule_id.strip(), safe='')}/",
                f"get rule {rule_id.strip()}",
            )
            if err:
                return err
            r = payload or {}
            if isinstance(r.get("data"), dict):
                r = r["data"]
            if not r:
                return f"Rule {rule_id} not found."
            lines = [
                f"Rule: {_trunc(r.get('name'), 120)}",
                f"ID: {r.get('id', rule_id)} | "
                f"Active: {r.get('is_active', '?')}",
            ]
            if r.get("description"):
                lines.append(f"Description: {_trunc(r.get('description'), 400)}")
            actions = r.get("actions") or []
            if isinstance(actions, list) and actions:
                names = ", ".join(
                    _trunc(a.get("name") or a.get("operation"), 50)
                    for a in actions[:10]
                    if isinstance(a, dict)
                )
                lines.append(f"Actions ({len(actions)}): {names}")
            return "\n".join(lines)
        extra: dict[str, Any] = {}
        if status.strip():
            extra["status"] = status.strip()
        payload, err = await _api(
            "get",
            "/ingestion/rules/",
            "list rules",
            params=_page_params(page, page_size, extra),
        )
        if err:
            return err
        items = _results(payload)
        out = format_entity_list(items, _fmt_rule, noun="rule")
        return out + _pagination_note(payload, len(items), page, page_size)

    async def list_cyware_allowed_indicators(
        ioc_type: str = "",
        page: int = 1,
        page_size: int = 25,
    ) -> str:
        """List the allowed (benign) indicators excluded from threat data."""
        extra: dict[str, Any] = {}
        if ioc_type.strip():
            extra["type"] = ioc_type.strip().lower()
        payload, err = await _api(
            "get",
            "/conversion/allowed_indicators/",
            "list allowed indicators",
            params=_page_params(page, page_size, extra),
        )
        if err:
            return err
        items = _results(payload)
        out = format_entity_list(items, _fmt_allowed, noun="allowed indicator")
        return out + _pagination_note(payload, len(items), page, page_size)

    async def list_cyware_watchlist(
        active_only: bool = False,
        page: int = 1,
        page_size: int = 25,
    ) -> str:
        """List the watchlist keywords monitored in Intel Exchange."""
        extra: dict[str, Any] = {}
        if active_only:
            extra["is_active"] = "true"
        payload, err = await _api(
            "get",
            "/ingestion/watchlist/",
            "list watchlist keywords",
            params=_page_params(page, page_size, extra),
        )
        if err:
            return err
        items = _results(payload)
        out = format_entity_list(items, _fmt_watchlist, noun="watchlist keyword")
        return out + _pagination_note(payload, len(items), page, page_size)

    async def get_cyware_enrichment(
        object_type: str,
        object_id: str,
        app_slug: str,
        action_slug: str = "",
        value: str = "",
    ) -> str:
        """Get a third-party enrichment verdict for one threat data object."""
        otype = object_type.strip().lower()
        oid = object_id.strip()
        slug = app_slug.strip()
        if not (otype and oid and slug):
            return (
                "Error: object_type, object_id, and app_slug are required. "
                "Get app slugs from manage_cyware_admin area=enrichment_tools."
            )
        params: dict[str, Any] = {
            "object_type": otype,
            "object_id": oid,
            "app_slug": slug,
        }
        if action_slug.strip():
            params["action_slug"] = action_slug.strip()
        if value.strip():
            params["value"] = value.strip()
        payload, err = await _api(
            "get",
            "/integration/apps/update/threatdata/",
            f"get enrichment for {otype} {oid}",
            params=params,
        )
        if err:
            return err
        body = payload or {}
        if not body:
            return f"No enrichment data returned for {otype} {oid} from {slug}."
        return (
            f"Enrichment from {slug} for {otype} {oid}:\n"
            f"{_trunc(_json.dumps(body, default=str, indent=None), 2500)}"
        )

    async def list_cyware_attack_techniques(
        q: str = "",
        domain: str = "",
        tactics: bool = False,
        page: int = 1,
        page_size: int = 25,
    ) -> str:
        """List ATT&CK techniques (or tactics) from the navigator."""
        extra: dict[str, Any] = {}
        if q.strip():
            extra["q"] = q.strip()
        if domain.strip():
            extra["domain"] = domain.strip()
        path = (
            "/ingestion/attack-navigator/tactics/"
            if tactics
            else "/ingestion/attack-navigator/techniques/"
        )
        payload, err = await _api(
            "get",
            path,
            "list ATT&CK tactics" if tactics else "list ATT&CK techniques",
            params=_page_params(page, page_size, extra),
        )
        if err:
            return err
        items = _results(payload)
        noun = "tactic" if tactics else "technique"
        out = format_entity_list(items, _fmt_technique, noun=noun)
        return out + _pagination_note(payload, len(items), page, page_size)

    # ══════════════════════════════════════════════════════════════════════
    # 6. LONG TAIL (consolidated admin reads + destructive delete)
    # ══════════════════════════════════════════════════════════════════════

    async def manage_cyware_admin(
        area: str,
        q: str = "",
        page: int = 1,
        page_size: int = 25,
    ) -> str:
        """Read platform administration context (consolidated long tail)."""
        key = area.strip().lower()
        if key not in _ADMIN_AREAS:
            return (
                f"Error: invalid area '{area}'. Use one of: "
                f"{', '.join(sorted(_ADMIN_AREAS))}."
            )
        path, paginated = _ADMIN_AREAS[key]
        params: dict[str, Any] = {}
        if paginated:
            params = _page_params(page, page_size)
            if q.strip():
                params["q"] = q.strip()
        payload, err = await _api("get", path, f"read {key}", params=params)
        if err:
            return err
        if paginated:
            items = _results(payload)
            out = format_entity_list(
                items, _fmt_generic, noun=key.replace("_", " ").rstrip("s")
            )
            return out + _pagination_note(payload, len(items), page, page_size)
        body = payload or {}
        if isinstance(body.get("data"), dict):
            body = body["data"]
        if not body:
            return f"No data returned for {key}."
        return f"{key}:\n{_trunc(_json.dumps(body, default=str, indent=None), 2500)}"

    async def delete_cyware_object(kind: str, object_id: str) -> str:
        """Delete a platform object by kind + ID. Permanent, cannot be undone."""
        key = kind.strip().lower()
        if key not in _DELETE_KINDS:
            return (
                f"Error: invalid kind '{kind}'. Use one of: "
                f"{', '.join(sorted(_DELETE_KINDS))}."
            )
        ident = object_id.strip()
        if not ident:
            return "Error: object_id is required."
        payload, err = await _api(
            "delete",
            _DELETE_KINDS[key].format(id=quote(ident, safe="")),
            f"delete {key} {ident}",
        )
        if err:
            return err
        message = (payload or {}).get("message") or (payload or {}).get("details") or ""
        return f"Deleted {key} {ident}." + (
            f" {_trunc(message, 200)}" if isinstance(message, str) and message else ""
        )

    # ── registration ────────────────────────────────────────────────────

    executor.register("search_cyware_threat_data", search_cyware_threat_data)
    executor.register("get_cyware_object", get_cyware_object)
    executor.register("get_cyware_object_relations", get_cyware_object_relations)
    executor.register("list_cyware_tags", list_cyware_tags)
    executor.register("list_cyware_sources", list_cyware_sources)
    executor.register("list_cyware_collections", list_cyware_collections)
    executor.register("list_cyware_subscribers", list_cyware_subscribers)
    executor.register("list_cyware_saved_searches", list_cyware_saved_searches)
    executor.register("create_cyware_intel", create_cyware_intel)
    executor.register("parse_cyware_iocs", parse_cyware_iocs)
    executor.register("create_cyware_tags", create_cyware_tags)
    executor.register("tag_cyware_objects", tag_cyware_objects)
    executor.register("update_cyware_threat_data", update_cyware_threat_data)
    executor.register("list_cyware_threat_bulletins", list_cyware_threat_bulletins)
    executor.register("get_cyware_threat_bulletin", get_cyware_threat_bulletin)
    executor.register("list_cyware_notes", list_cyware_notes)
    executor.register("list_cyware_tasks", list_cyware_tasks)
    executor.register("list_cyware_rules", list_cyware_rules)
    executor.register(
        "list_cyware_allowed_indicators", list_cyware_allowed_indicators
    )
    executor.register("list_cyware_watchlist", list_cyware_watchlist)
    executor.register("get_cyware_enrichment", get_cyware_enrichment)
    executor.register("list_cyware_attack_techniques", list_cyware_attack_techniques)
    executor.register("manage_cyware_admin", manage_cyware_admin)
    executor.register("delete_cyware_object", delete_cyware_object)

    _pagination_props = {
        "page": {
            "type": "integer",
            "default": 1,
            "description": "1-based page number",
        },
        "page_size": {
            "type": "integer",
            "default": 25,
            "description": "Results per page (max 100)",
        },
    }

    return [
        # ── 1. Threat data (reads) ───────────────────────────────────────
        ToolDefinition(
            name="search_cyware_threat_data",
            description=(
                "Search threat data objects in Cyware Intel Exchange (CTIX) "
                "with a CQL query — the primary IOC lookup and hunting tool. "
                "Returns IDs, risk scores, TLP, and status flags for "
                "follow-up calls (get_cyware_object, "
                "get_cyware_object_relations). CQL "
                "examples: 'value = \"evil.com\"' (find an IOC), 'type = "
                "indicator AND ioc_type = ipv4-addr AND confidence_score >= "
                "80' (high-risk IPs), 'type = malware' (malware SDOs), "
                "'is_false_positive = false AND is_deprecated = false'. "
                "Fields: type, ioc_type, value, confidence_score, "
                "analyst_score, tlp, tags, source, ctix_created, "
                "ctix_modified, is_deprecated, is_false_positive, "
                "is_whitelisted, is_reviewed (dates are EPOCH seconds). "
                "Empty query returns the most recent objects."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "CQL query, e.g. 'type = indicator AND "
                            "confidence_score >= 80' (empty = most recent)"
                        ),
                        "default": "",
                    },
                    "page": _pagination_props["page"],
                    "page_size": {
                        "type": "integer",
                        "default": 10,
                        "description": "Results per page (max 100)",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_cyware_object",
            description=(
                "Get the full correlated detail of one threat data object by "
                "type + UUID: risk/analyst scores, TLP, severity, "
                "description, sources, collections, tags, and status flags. "
                "Get the UUID from search_cyware_threat_data. Use "
                "get_cyware_object_relations for the related-objects graph."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": {
                        "type": "string",
                        "description": (
                            "STIX object type as returned by search: "
                            f"{', '.join(_OBJECT_TYPES)}, or a custom x-* type"
                        ),
                    },
                    "object_id": {
                        "type": "string",
                        "description": "Threat data object UUID",
                    },
                },
                "required": ["object_type", "object_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_cyware_object_relations",
            description=(
                "List the related threat data objects of one object (e.g. "
                "which malware, threat actors, and campaigns an indicator is "
                "linked to). Returns the relationship type and each related "
                "object's type, name, and UUID for follow-up lookups."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": {
                        "type": "string",
                        "description": (
                            "STIX object type as returned by search: "
                            f"{', '.join(_OBJECT_TYPES)}, or a custom x-* type"
                        ),
                    },
                    "object_id": {
                        "type": "string",
                        "description": "Threat data object UUID",
                    },
                    **_pagination_props,
                },
                "required": ["object_type", "object_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 2. Context (reads) ───────────────────────────────────────────
        ToolDefinition(
            name="list_cyware_tags",
            description=(
                "List tags in Intel Exchange with their IDs and types. Tags "
                "can scope CQL searches in search_cyware_threat_data (e.g. "
                "'tags = \"TAG_NAME\"'). Filter by tag_type: user, source, "
                "system, privileged, group."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tag_type": {
                        "type": "string",
                        "enum": ["", "user", "source", "system", "privileged", "group"],
                        "default": "",
                        "description": "Restrict to one tag category (empty = all)",
                    },
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_cyware_sources",
            description=(
                "List the threat intel feed sources configured in Intel "
                "Exchange (STIX/TAXII, API feeds, email, RSS, web scraper). "
                "Source IDs are usable in CQL ('source = \"ID\"') to scope "
                "searches to one feed. Filter by source_type: "
                "CUSTOM_STIX_SOURCES, API_FEEDS, EMAIL_ACCOUNTS, RSS_FEED, "
                "TWITTER_FEEDS, WEB_SCRAPPER, MALWARE_SANDBOX, MISCELLANEOUS."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source_type": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Feed source category, e.g. API_FEEDS (empty = all)"
                        ),
                    },
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_cyware_collections",
            description=(
                "List the STIX collections intel is published to in Intel "
                "Exchange, with their IDs and active state. Collection "
                "names/IDs are usable in CQL published_collection filters."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "q": {
                        "type": "string",
                        "default": "",
                        "description": "Search collections by name",
                    },
                    "active_only": {"type": "boolean", "default": False},
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_cyware_subscribers",
            description=(
                "List the subscribers (member organizations/entities) that "
                "intel is shared with from this Intel Exchange instance, "
                "with their active state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "organization": {
                        "type": "string",
                        "default": "",
                        "description": "Filter by organization name",
                    },
                    "active_only": {"type": "boolean", "default": False},
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_cyware_saved_searches",
            description=(
                "List saved threat data searches (reusable CQL queries "
                "analysts have pinned). Run one by passing its query text to "
                "search_cyware_threat_data."
            ),
            input_schema={
                "type": "object",
                "properties": {**_pagination_props},
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 3. Intel production (writes) ─────────────────────────────────
        ToolDefinition(
            name="create_cyware_intel",
            description=(
                "Create intel in Intel Exchange (quick add): pass IOCs and/or "
                "STIX Domain Objects and they are ingested as threat data "
                "(default source: Import). indicators is a JSON object of "
                "IOC key -> comma-separated values, e.g. '{\"ipv4-addr\": "
                "\"1.2.3.4,5.6.7.8\", \"domain\": \"evil.com\"}' (keys: "
                "ipv4-addr, ipv6-addr, domain, url, email, md5, sha1, "
                "sha224, sha256, sha384, sha512, ssdeep). sdos maps SDO key "
                "-> comma-separated names, e.g. '{\"malware\": \"Emotet\", "
                "\"threat-actor\": \"TA505\"}'. Processing is asynchronous — "
                "verify afterwards with search_cyware_threat_data. Set "
                "publish=false to create a draft instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Report title (max 100 chars)",
                        "default": "",
                    },
                    "description": {
                        "type": "string",
                        "description": "Intel description (max 1000 chars)",
                        "default": "",
                    },
                    "indicators": {
                        "type": "string",
                        "description": (
                            "JSON object: IOC key -> comma-separated values"
                        ),
                        "default": "",
                    },
                    "sdos": {
                        "type": "string",
                        "description": (
                            "JSON object: SDO key -> comma-separated names"
                        ),
                        "default": "",
                    },
                    "observables": {
                        "type": "string",
                        "description": (
                            "JSON object: SCO key -> comma-separated values "
                            "(e.g. mutex, software, user-account)"
                        ),
                        "default": "",
                    },
                    "tlp": {
                        "type": "string",
                        "enum": ["", "RED", "AMBER", "GREEN", "WHITE", "NONE"],
                        "default": "",
                    },
                    "confidence": {
                        "type": "integer",
                        "description": "Source confidence score 0-100",
                    },
                    "tag_ids": {
                        "type": "string",
                        "description": (
                            "Comma-separated tag IDs to label the intel "
                            "(from list_cyware_tags)"
                        ),
                        "default": "",
                    },
                    "publish": {
                        "type": "boolean",
                        "default": True,
                        "description": "false = create a draft intel instead",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="parse_cyware_iocs",
            description=(
                "Extract IOCs (IPs, domains, URLs, hashes, emails, CVEs, "
                "registry keys, file paths) from free text or a web page "
                "using Intel Exchange's parser. Submits the parsing task, "
                "polls until done, and returns the parsed IOCs grouped by "
                "type (flagging any already on the allowed list). Use "
                "create_cyware_intel to ingest the results. Max 50,000 "
                "characters of input."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Free text to parse, or a web page URL",
                    },
                    "is_url": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "true = treat text as a web page URL and scrape it"
                        ),
                    },
                    "wait_seconds": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max seconds to wait for parsing (5-60)",
                    },
                },
                "required": ["text"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="create_cyware_tags",
            description=(
                "Create tags in Intel Exchange (max 15 per request). New "
                "tags can then be applied to threat data with "
                "tag_cyware_objects or attached to new intel via "
                "create_cyware_intel tag_ids."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "names": {
                        "type": "string",
                        "description": "Comma-separated tag names",
                    },
                    "tag_type": {
                        "type": "string",
                        "enum": ["user", "system", "privileged"],
                        "default": "user",
                    },
                },
                "required": ["names"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 4. IOC curation (writes) ─────────────────────────────────────
        ToolDefinition(
            name="tag_cyware_objects",
            description=(
                "Add or remove tags on up to 100 threat data objects in one "
                "request. Get object UUIDs from search_cyware_threat_data "
                "and tag IDs from list_cyware_tags. All objects must share "
                "the same object_type (e.g. indicator)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "remove"]},
                    "object_type": {
                        "type": "string",
                        "description": (
                            "STIX object type shared by all objects: "
                            f"{', '.join(_OBJECT_TYPES)}, or a custom x-* type"
                        ),
                    },
                    "object_ids": {
                        "type": "string",
                        "description": "Comma-separated object UUIDs (max 100)",
                    },
                    "tag_ids": {
                        "type": "string",
                        "description": "Comma-separated tag IDs (max 50)",
                    },
                },
                "required": ["action", "object_type", "object_ids", "tag_ids"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_cyware_threat_data",
            description=(
                "Run a curation bulk action on up to 100 threat data objects "
                "(same object_type). Actions: false_positive / "
                "un_false_positive (mark/unmark IOCs as false positives), "
                "deprecate / un_deprecate, whitelist / un_whitelist (add to "
                "or remove from the Allowed Indicators list), manual_review "
                "/ reviewed, watchlist / un_watchlist (value = watchlist "
                "name), analyst_score (value = 0-100), analyst_tlp (value = "
                "RED/AMBER/GREEN/WHITE/NONE), run_rule (value = rule ID from "
                "list_cyware_rules — only rules configured for manual run)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_BULK_ACTIONS),
                    },
                    "object_type": {
                        "type": "string",
                        "description": (
                            "STIX object type shared by all objects: "
                            f"{', '.join(_OBJECT_TYPES)}, or a custom x-* type"
                        ),
                    },
                    "object_ids": {
                        "type": "string",
                        "description": "Comma-separated object UUIDs (max 100)",
                    },
                    "value": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Action payload: watchlist name, analyst score "
                            "(0-100), TLP, or rule ID — see the action list"
                        ),
                    },
                },
                "required": ["action", "object_type", "object_ids"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 5. Knowledge (reads) ─────────────────────────────────────────
        ToolDefinition(
            name="list_cyware_threat_bulletins",
            description=(
                "List threat bulletins (finished intelligence reports "
                "authored or ingested in Intel Exchange) with their state "
                "and TLP. Use get_cyware_threat_bulletin for the content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "q": {
                        "type": "string",
                        "default": "",
                        "description": "Search bulletins by title/content",
                    },
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_cyware_threat_bulletin",
            description=(
                "Get one threat bulletin's full detail: title, state, TLP, "
                "author, timestamps, content, and attachment count."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "bulletin_id": {
                        "type": "string",
                        "description": "Bulletin ID from list_cyware_threat_bulletins",
                    },
                },
                "required": ["bulletin_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_cyware_notes",
            description=(
                "List analyst notes — global notes, or the notes on one "
                "threat data object when object_id is set. Returns note IDs "
                "(deletable via delete_cyware_object kind=note)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_id": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Scope to one threat data object UUID (optional)"
                        ),
                    },
                    "q": {
                        "type": "string",
                        "default": "",
                        "description": "Search note text",
                    },
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_cyware_tasks",
            description=(
                "List analyst tasks tracked in Intel Exchange with status, "
                "due date, and assignee."
            ),
            input_schema={
                "type": "object",
                "properties": {**_pagination_props},
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_cyware_rules",
            description=(
                "List the automation rules configured in Intel Exchange, or "
                "get one rule's detail (actions, active state) by rule_id. "
                "Rule IDs feed update_cyware_threat_data action=run_rule."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {
                        "type": "string",
                        "default": "",
                        "description": "Get one rule's detail (optional)",
                    },
                    "status": {
                        "type": "string",
                        "default": "",
                        "description": "Filter by rule status (optional)",
                    },
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_cyware_allowed_indicators",
            description=(
                "List the Allowed Indicators (benign IOCs excluded from "
                "threat data). Add/remove entries in bulk with "
                "update_cyware_threat_data action=whitelist/un_whitelist; "
                "delete one entry with delete_cyware_object "
                "kind=allowed_indicator."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ioc_type": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Filter by IOC type, e.g. ipv4-addr, domain-name, "
                            "url (optional)"
                        ),
                    },
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_cyware_watchlist",
            description=(
                "List the watchlist keywords monitored in Intel Exchange. "
                "Put objects on/off a watchlist with "
                "update_cyware_threat_data action=watchlist/un_watchlist."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "active_only": {"type": "boolean", "default": False},
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_cyware_enrichment",
            description=(
                "Get a third-party enrichment verdict (VirusTotal, "
                "AlienVault, etc.) for one threat data object through Intel "
                "Exchange's configured enrichment tools. Requires the "
                "enrichment app's slug — list the configured tools first "
                "with manage_cyware_admin area=enrichment_tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": {
                        "type": "string",
                        "description": (
                            "STIX object type as returned by search: "
                            f"{', '.join(_OBJECT_TYPES)}, or a custom x-* type"
                        ),
                    },
                    "object_id": {
                        "type": "string",
                        "description": "Threat data object UUID",
                    },
                    "app_slug": {
                        "type": "string",
                        "description": (
                            "Enrichment tool slug (from manage_cyware_admin "
                            "area=enrichment_tools)"
                        ),
                    },
                    "action_slug": {
                        "type": "string",
                        "default": "",
                        "description": "Enrichment action slug (optional)",
                    },
                    "value": {
                        "type": "string",
                        "default": "",
                        "description": "IOC value to enrich (optional)",
                    },
                },
                "required": ["object_type", "object_id", "app_slug"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_cyware_attack_techniques",
            description=(
                "List MITRE ATT&CK techniques from the Intel Exchange "
                "navigator (with their tactics and related-APT counts), or "
                "set tactics=true to list the ATT&CK tactics instead. "
                "Search by name with q."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "q": {
                        "type": "string",
                        "default": "",
                        "description": "Search by technique/tactic name",
                    },
                    "domain": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "ATT&CK domain, e.g. enterprise-attack (optional)"
                        ),
                    },
                    "tactics": {
                        "type": "boolean",
                        "default": False,
                        "description": "true = list tactics instead of techniques",
                    },
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 6. Long tail ─────────────────────────────────────────────────
        ToolDefinition(
            name="manage_cyware_admin",
            description=(
                "Read Intel Exchange administration context (consolidated "
                "long tail). area: heartbeat (service health), tenant_config, "
                "license, rate_limits (Open API limits), taxii_config, "
                "enrichment_tools (slugs for get_cyware_enrichment), users, "
                "user_groups, audit_logs (configuration change log), "
                "dashboards, reports, intel_history (quick-add history), "
                "ioc_types, source_collections, threat_mailbox (email feed "
                "inbox), rss_articles, me (the API user's own profile)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "area": {
                        "type": "string",
                        "enum": sorted(_ADMIN_AREAS),
                    },
                    "q": {
                        "type": "string",
                        "default": "",
                        "description": "Search filter for list areas (optional)",
                    },
                    **_pagination_props,
                },
                "required": ["area"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="delete_cyware_object",
            description=(
                "Delete a platform object by kind + ID. Permanent, cannot be "
                "undone. kind: note, task, saved_search, allowed_indicator, "
                "watchlist_keyword, report, dashboard. Threat data objects "
                "cannot be deleted — deprecate them with "
                "update_cyware_threat_data instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": sorted(_DELETE_KINDS),
                    },
                    "object_id": {"type": "string"},
                },
                "required": ["kind", "object_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
