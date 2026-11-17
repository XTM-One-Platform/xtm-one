# SPDX-License-Identifier: Apache-2.0

"""Sekoia.io integration - agentic SOC platform access (CTI + XDR).

Full-fledged Sekoia.io integration covering both major API surfaces for an
agentic security team:

Intelligence Center / CTI (``/v2/inthreat``)
  - Indicator lookup (is this IOC known?) and full indicator context
    (related malware, intrusion sets, campaigns, courses of action)
  - Full-text STIX object search (threat actors, malware, reports, ...)
  - Single object detail + optional context bundle
  - Observable search by value/type
  - Intelligence feed consumption (cursor-paginated collections)
  - IOC collection management (create, list/add/delete indicators)

Operations Center / XDR (``/v1/sic``, ``/v1/asset-management``)
  - Alerts: list with rich filters, get detail, workflow status transitions
    (acknowledge/reject/close/...), comments (read + write)
  - Cases: list and filter (including cases containing a given alert)
  - Events: asynchronous search over telemetry (Dork query -> search job ->
    poll -> results)
  - Detection rules catalog
  - Assets: inventory listing + per-asset detail

Authentication
--------------
Sekoia.io authenticates every request with a single API key sent as a Bearer
token (``Authorization: Bearer <API_KEY>``). Keys are permission-based
(created under Settings > Workspace > API Keys). ``integration_tools._get_token``
hands this builder the compound ``base_url||api_key`` token; ``base_url`` is
optional and defaults to ``https://api.sekoia.io`` (region-specific tenants,
e.g. ``https://app.mco1.sekoia.io/api``, override it).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    bearer_auth_headers,
    format_entity_list,
    format_pagination_hint,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "Sekoia.io"
_SOURCE = "integration:sekoia"

# Default Sekoia.io API host. Stored base_url overrides this for region-specific
# tenants (e.g. https://app.mco1.sekoia.io/api).
_DEFAULT_BASE_URL = "https://api.sekoia.io"

# Alert workflow transition UUIDs - fixed platform-wide values, used by
# Sekoia's own automation library (PATCH /v1/sic/alerts/{id}/workflow with
# {"action_uuid": <uuid>, "comment": ...}).
_ALERT_STATUS_ACTIONS: dict[str, str] = {
    "pending": "2efc4930-1442-4abb-acf2-58ba219a4fd0",
    "acknowledged": "8f206505-af6d-433e-93f4-775d46dc7d0f",
    "ongoing": "1f2f88d5-ff5b-48bf-bbbc-00c2fff82d9f",
    "rejected": "4f68da89-38e0-4703-a6ab-652f02bdf24e",
    "closed": "1738b1c1-767d-489e-bada-19176621a007",
}

# Event search job statuses (GET /v1/sic/conf/events/search/jobs/{uuid}).
_JOB_STATUS_NAMES: dict[int, str] = {
    0: "not started",
    1: "in progress",
    2: "done",
    3: "canceled",
    4: "failed",
}
_EVENT_SEARCH_POLL_INTERVAL = 2.0
_EVENT_SEARCH_MAX_POLLS = 15

_IOC_COLLECTION_ACTIONS: frozenset[str] = frozenset(
    {"create_collection", "list_indicators", "add_indicators", "delete_indicator"}
)


def _path(value: Any) -> str:
    """Percent-encode a value for use as a single URL path segment.

    User-supplied ids (alert ids, STIX object ids, collection ids, ...) are
    interpolated into request paths; encoding with ``safe=""`` prevents an id
    containing ``/`` or ``..`` from escaping its path segment and turning a
    scoped tool into a generic API caller (same pattern as the Recorded
    Future / Splunk / ServiceNow providers).
    """
    return quote(str(value).strip(), safe="")


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _ts(value: Any) -> str:
    """Render a Sekoia timestamp (ISO string or epoch int) compactly."""
    if value is None or value == "":
        return "?"
    return str(value)[:19]


def _stix_name(obj: dict) -> str:
    """Best-effort display name for a STIX object / observable."""
    return str(
        obj.get("name")
        or obj.get("x_inthreat_short_display")
        or obj.get("value")
        or obj.get("pattern")
        or obj.get("id", "?")
    )


def _fmt_stix_object(obj: dict) -> str:
    line = f"  [{obj.get('type', '?')}] {_trunc(_stix_name(obj), 90)} (id {obj.get('id', '?')})"
    desc = obj.get("description")
    if desc:
        line += f"\n    {_trunc(desc, 160)}"
    return line


def _extract_stix_items(data: Any) -> list[dict]:
    """Normalise the various Intelligence Center response shapes to a list.

    Endpoints return either ``{"items": [...]}``, a raw STIX bundle
    ``{"type": "bundle", "objects": [...]}``, or a bare list.
    """
    if isinstance(data, list):
        return [o for o in data if isinstance(o, dict)]
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return [o for o in items if isinstance(o, dict)]
        objects = data.get("objects")
        if isinstance(objects, list):
            return [o for o in objects if isinstance(o, dict)]
    return []


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def sekoia_api_base(base_url: str) -> str:
    """Return the Sekoia.io API base URL (configured host, no trailing slash).

    Whitespace is stripped before the default fallback and the trailing-slash
    trim (matching ``rapid7_api_base`` / ``rf_api_base``), so a pasted
    ``base_url`` with surrounding whitespace - or an entirely whitespace-only
    value - normalises to a valid host instead of an empty string.
    """
    return ((base_url or "").strip() or _DEFAULT_BASE_URL).rstrip("/")


def build_sekoia_headers(api_key: str) -> dict[str, str]:
    """Build Sekoia.io request headers (Bearer API key + JSON accept).

    The key is normalised by ``bearer_auth_headers`` (stripped, blank-guarded)
    so pasted whitespace can never produce a malformed ``Authorization``
    header. Single chokepoint shared by the tool builder and the connectivity
    test.
    """
    return bearer_auth_headers(api_key, extra={"Accept": "application/json"})


class SekoiaIntegration(BaseIntegration):
    """Sekoia.io SOC platform integration (API key, Bearer auth)."""

    provider_name = "sekoia"
    display_name = "Sekoia.io"
    icon = "ShieldHalf"
    description = (
        "Connect Sekoia.io for CTI and XDR: look up indicators and their "
        "threat context, search intelligence and observables, consume feeds, "
        "manage IOC collections, triage and update alerts, search events, "
        "and browse detection rules, cases, and assets."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        """Sekoia.io uses a Bearer API key, not OAuth."""
        return None

    async def validate_connection(self, token: str) -> bool:
        """Validate the API key against the lightweight auth-validate endpoint."""
        base_url, api_key = (
            p.strip() for p in parse_compound_token(token, 2, greedy_last=True)
        )
        if not api_key:
            return False
        api = sekoia_api_base(base_url)
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v1/auth/validate",
                _PROVIDER,
                "validate API key",
                headers=build_sekoia_headers(api_key),
            )
            return err is None and resp is not None and resp.is_success

    async def get_user_info(self, token: str) -> dict:
        """Return minimal account context (identity is bound to the API key)."""
        base_url, _ = (
            p.strip() for p in parse_compound_token(token, 2, greedy_last=True)
        )
        return {"name": "Sekoia.io", "base_url": sekoia_api_base(base_url)}


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Sekoia.io tools from a ``base_url||api_key`` compound token."""
    # ``parse_compound_token`` does not normalise - strip each segment so pasted
    # whitespace can't yield a malformed base URL / Authorization header, and so
    # the empty-credential guard treats a whitespace-only key as missing.
    base_url, api_key = (
        part.strip() for part in parse_compound_token(token, 2, greedy_last=True)
    )
    if not api_key:
        logger.debug(
            "Sekoia.io API key is empty - returning no tools. Edit the "
            "integration and set the API key."
        )
        return []

    api = sekoia_api_base(base_url)
    headers = build_sekoia_headers(api_key)

    def _client(timeout: int = 30) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_sekoia_status() -> str:
        """Validate the Sekoia.io connection and API key."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v1/auth/validate",
                _PROVIDER,
                "validate API key",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
        identity = ""
        if isinstance(data, dict):
            for key in ("name", "user", "identity", "caller"):
                if data.get(key):
                    identity = f" Identity: {_trunc(data[key], 80)}."
                    break
        return f"Connected to Sekoia.io at {api}. API key is valid.{identity}"

    # ══════════════════════════════════════════════════════════════════════
    # 1. INTELLIGENCE CENTER (CTI)
    # ══════════════════════════════════════════════════════════════════════

    async def lookup_sekoia_indicator(value: str, type: str = "", limit: int = 10) -> str:
        """Look up indicators matching an IOC value (+ optional observable type)."""
        params: dict[str, Any] = {
            "value": value.strip(),
            "limit": max(1, min(int(limit or 10), 100)),
        }
        if type.strip():
            params["type"] = type.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v2/inthreat/indicators",
                _PROVIDER,
                "look up indicator",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        items = _extract_stix_items(data)
        total = data.get("total") if isinstance(data, dict) else None

        def _fmt(ind: dict) -> str:
            validity = f"valid from {_ts(ind.get('valid_from'))}"
            if ind.get("valid_until"):
                validity += f" until {_ts(ind.get('valid_until'))}"
            line = (
                f"  {_trunc(_stix_name(ind), 90)} (id {ind.get('id', '?')})\n"
                f"    {validity} | revoked: {ind.get('revoked', False)}"
            )
            if ind.get("pattern"):
                line += f"\n    Pattern: {_trunc(ind.get('pattern'), 140)}"
            return line

        return format_entity_list(
            items,
            _fmt,
            noun="indicator",
            total=total if isinstance(total, int) else None,
            empty_msg=(
                f"No indicator found for '{value}' - the IOC is unknown to the "
                "Sekoia.io Intelligence Center."
            ),
        )

    async def get_sekoia_indicator_context(value: str, type: str) -> str:
        """Get the full threat context of an IOC (related threats + mitigations)."""
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v2/inthreat/indicators/context",
                _PROVIDER,
                "get indicator context",
                headers=headers,
                params={"value": value.strip(), "type": type.strip()},
            )
            if err:
                return err
            data = resp.json() or {}
        objects = _extract_stix_items(data)
        if not objects:
            return (
                f"No context found for {type} '{value}' - the IOC is unknown to "
                "the Sekoia.io Intelligence Center."
            )
        by_type: dict[str, list[dict]] = {}
        for obj in objects:
            by_type.setdefault(str(obj.get("type", "?")), []).append(obj)
        lines = [f"Context for {type} '{value}' ({len(objects)} related objects):"]
        # Threat objects first, plumbing (relationships/markings) last.
        priority_order = (
            "indicator", "malware", "intrusion-set", "threat-actor", "campaign",
            "attack-pattern", "course-of-action", "identity", "report",
        )
        ordered = [t for t in priority_order if t in by_type] + sorted(
            t for t in by_type if t not in priority_order
        )
        for obj_type in ordered:
            objs = by_type[obj_type]
            if obj_type in ("relationship", "marking-definition"):
                lines.append(f"  {obj_type}: {len(objs)} object(s)")
                continue
            lines.append(f"  {obj_type} ({len(objs)}):")
            for obj in objs[:8]:
                lines.append(f"    - {_trunc(_stix_name(obj), 90)} (id {obj.get('id', '?')})")
                if obj.get("description"):
                    lines.append(f"      {_trunc(obj.get('description'), 150)}")
            if len(objs) > 8:
                lines.append(f"    ... and {len(objs) - 8} more")
        return "\n".join(lines)

    async def search_sekoia_intelligence(term: str, limit: int = 20) -> str:
        """Full-text search of the Intelligence Center STIX objects."""
        params: dict[str, Any] = {
            "term": term.strip(),
            "limit": max(1, min(int(limit or 20), 100)),
        }
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/v2/inthreat/objects/search",
                _PROVIDER,
                "search intelligence",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        items = _extract_stix_items(data)
        total = data.get("total") if isinstance(data, dict) else None
        out = format_entity_list(
            items,
            _fmt_stix_object,
            noun="intelligence object",
            total=total if isinstance(total, int) else None,
        )
        if isinstance(data, dict) and data.get("has_more"):
            out += "\n[More results available - refine the search term or raise limit]"
        return out

    async def get_sekoia_intelligence_object(
        object_id: str, include_context: bool = False
    ) -> str:
        """Get one STIX object by id, optionally with its context bundle."""
        oid = object_id.strip()
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v2/inthreat/objects/{_path(oid)}",
                _PROVIDER,
                f"get object {oid}",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() or {}
            obj = data
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                obj = data["data"]
            items = _extract_stix_items(obj)
            if items:
                obj = items[0]
            lines = [
                f"[{obj.get('type', '?')}] {_trunc(_stix_name(obj), 120)}",
                f"ID: {obj.get('id', '?')}",
                f"Created: {_ts(obj.get('created'))} | Modified: {_ts(obj.get('modified'))} | "
                f"Confidence: {obj.get('confidence', 'N/A')} | Revoked: {obj.get('revoked', False)}",
            ]
            if obj.get("aliases"):
                lines.append(f"Aliases: {_trunc(', '.join(map(str, obj['aliases'])), 200)}")
            if obj.get("labels"):
                lines.append(f"Labels: {_trunc(', '.join(map(str, obj['labels'])), 200)}")
            if obj.get("pattern"):
                lines.append(f"Pattern: {_trunc(obj.get('pattern'), 300)}")
            if obj.get("description"):
                lines.append(f"\nDescription: {_trunc(obj.get('description'), 800)}")

            if include_context:
                ctx_resp, ctx_err = await safe_request(
                    client,
                    "get",
                    f"{api}/v2/inthreat/objects/{_path(oid)}/context",
                    _PROVIDER,
                    f"get object context {oid}",
                    headers=headers,
                )
                if ctx_err:
                    lines.append(f"\nContext lookup failed: {ctx_err}")
                else:
                    ctx_objects = [
                        o
                        for o in _extract_stix_items(ctx_resp.json() or {})
                        if o.get("id") != oid
                        and o.get("type") not in ("relationship", "marking-definition")
                    ]
                    if ctx_objects:
                        lines.append(f"\nRelated objects ({len(ctx_objects)}):")
                        for rel in ctx_objects[:15]:
                            lines.append(
                                f"  - [{rel.get('type', '?')}] "
                                f"{_trunc(_stix_name(rel), 80)} (id {rel.get('id', '?')})"
                            )
                        if len(ctx_objects) > 15:
                            lines.append(f"  ... and {len(ctx_objects) - 15} more")
        return "\n".join(lines)

    async def search_sekoia_observables(
        value: str = "", type: str = "", limit: int = 20, offset: int = 0
    ) -> str:
        """Search Intelligence Center observables by value and/or type."""
        if not value.strip() and not type.strip():
            return "Error: provide at least one of value or type."
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit or 20), 100)),
            "offset": max(0, int(offset or 0)),
        }
        if value.strip():
            params["match[value]"] = value.strip()
        if type.strip():
            params["match[type]"] = type.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v2/inthreat/observables",
                _PROVIDER,
                "search observables",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        items = _extract_stix_items(data)
        total = data.get("total") if isinstance(data, dict) else None
        total = total if isinstance(total, int) else None

        def _fmt(obs: dict) -> str:
            line = f"  [{obs.get('type', '?')}] {_trunc(_stix_name(obs), 90)} (id {obs.get('id', '?')})"
            tags = obs.get("x_inthreat_tags") or []
            if tags:
                names = [
                    str(t.get("name", t)) if isinstance(t, dict) else str(t)
                    for t in tags[:6]
                ]
                line += f"\n    Tags: {', '.join(names)}"
            return line

        out = format_entity_list(items, _fmt, noun="observable", total=total)
        if total and total > params["offset"] + len(items):
            out += (
                f"\n[Showing {len(items)} from offset {params['offset']} of {total} - "
                "pass a higher offset for more]"
            )
        return out

    async def get_sekoia_feed_objects(
        feed_id: str, limit: int = 50, cursor: str = ""
    ) -> str:
        """Consume STIX objects from an intelligence feed (cursor pagination)."""
        params: dict[str, Any] = {"limit": max(1, min(int(limit or 50), 2000))}
        if cursor.strip():
            params["cursor"] = cursor.strip()
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v2/inthreat/collections/{_path(feed_id)}/objects",
                _PROVIDER,
                f"get feed objects {feed_id}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        items = _extract_stix_items(data)
        out = format_entity_list(
            items,
            _fmt_stix_object,
            noun="feed object",
            empty_msg="Feed returned no new objects (end of the feed for this cursor).",
        )
        next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
        # A full page means more objects are likely available at the returned
        # cursor; a short page means the feed is exhausted for now.
        if next_cursor and len(items) >= params["limit"]:
            out += format_pagination_hint(str(next_cursor), param_name="cursor")
        return out

    async def manage_sekoia_ioc_collections(
        action: str,
        collection_id: str = "",
        name: str = "",
        indicators: str = "",
        description: str = "",
        term: str = "",
        indicator_id: str = "",
        limit: int = 25,
    ) -> str:
        """Manage IOC collections: create, list/add/delete indicators."""
        act = action.lower().strip()
        if act not in _IOC_COLLECTION_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_IOC_COLLECTION_ACTIONS))}."
        ioc_api = f"{api}/v2/inthreat/ioc-collections"
        cid = collection_id.strip()
        if act != "create_collection" and not cid:
            return f"Error: collection_id is required for action '{act}'."

        async with _client(timeout=60) as client:
            if act == "create_collection":
                if not name.strip():
                    return "Error: name is required to create an IOC collection."
                resp, err = await safe_request(
                    client,
                    "post",
                    ioc_api,
                    _PROVIDER,
                    "create IOC collection",
                    headers=headers,
                    json={"name": name.strip()},
                )
                if err:
                    return err
                data = resp.json() or {}
                return (
                    f"IOC collection '{name.strip()}' created with id "
                    f"{data.get('id', '?')}."
                )

            if act == "list_indicators":
                params: dict[str, Any] = {}
                if term.strip():
                    params["term"] = term.strip()
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{ioc_api}/{_path(cid)}/indicators",
                    _PROVIDER,
                    f"list IOC collection indicators {cid}",
                    headers=headers,
                    params=params,
                )
                if err:
                    return err
                items = _extract_stix_items(resp.json() or {})
                shown = items[: max(1, int(limit or 25))]

                def _fmt(ind: dict) -> str:
                    return (
                        f"  {_trunc(_stix_name(ind), 90)} (id {ind.get('id', '?')}) - "
                        f"revoked: {ind.get('revoked', False)}"
                    )

                out = format_entity_list(
                    shown, _fmt, noun="indicator", total=len(items)
                )
                if len(items) > len(shown):
                    out += f"\n[Showing {len(shown)} of {len(items)} - raise limit or filter with term]"
                return out

            if act == "add_indicators":
                if not indicators.strip():
                    return (
                        "Error: indicators is required (one IOC value per line, "
                        "e.g. IPs, domains, hashes, URLs)."
                    )
                body: dict[str, Any] = {
                    "format": "text",
                    "indicators": indicators.strip(),
                }
                if description.strip():
                    body["description"] = description.strip()
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{ioc_api}/{_path(cid)}/indicators/text",
                    _PROVIDER,
                    f"add indicators to IOC collection {cid}",
                    headers=headers,
                    json=body,
                )
                if err:
                    return err
                count = len([line for line in indicators.strip().splitlines() if line.strip()])
                return (
                    f"Submitted {count} indicator(s) to IOC collection {cid}. "
                    "Sekoia.io processes the import asynchronously."
                )

            # delete_indicator (destructive)
            if not indicator_id.strip():
                return "Error: indicator_id is required to delete an indicator."
            resp, err = await safe_request(
                client,
                "delete",
                f"{ioc_api}/{_path(cid)}/indicators/{_path(indicator_id)}",
                _PROVIDER,
                f"delete indicator {indicator_id} from IOC collection {cid}",
                headers=headers,
                expected_statuses={404},
            )
            if err and (resp is None or resp.status_code != 404):
                return err
            if resp is not None and resp.status_code == 404:
                return (
                    f"Indicator {indicator_id.strip()} was not found in IOC "
                    f"collection {cid} (already deleted?)."
                )
            return (
                f"Indicator {indicator_id.strip()} permanently deleted from IOC "
                f"collection {cid}."
            )

    # ══════════════════════════════════════════════════════════════════════
    # 2. XDR - ALERTS
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_alert(a: dict) -> str:
        rule = a.get("rule") or {}
        entity = a.get("entity") or {}
        status = a.get("status") or {}
        alert_type = a.get("alert_type") or {}
        return (
            f"  {a.get('short_id', '?')} [{status.get('name', '?')}] "
            f"urgency {a.get('urgency', '?')} - {_trunc(a.get('title') or rule.get('name'), 80)}\n"
            f"    Rule: {_trunc(rule.get('name'), 60) or 'N/A'} | "
            f"Entity: {entity.get('name', 'N/A')} | "
            f"Category: {alert_type.get('category', alert_type.get('value', 'N/A'))}\n"
            f"    Created: {_ts(a.get('created_at'))} | UUID: {a.get('uuid', '?')}"
        )

    async def list_sekoia_alerts(
        status: str = "",
        urgency: str = "",
        created_at: str = "",
        updated_at: str = "",
        rule_name: str = "",
        entity_name: str = "",
        sort_by: str = "created_at",
        direction: str = "desc",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """List XDR alerts with status/urgency/date/rule/entity filters."""
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit or 20), 100)),
            "offset": max(0, int(offset or 0)),
            "sort": sort_by.strip() or "created_at",
            "direction": "asc" if direction.lower().strip() == "asc" else "desc",
        }
        if status.strip():
            params["match[status_name]"] = status.strip()
        if urgency.strip():
            params["range[urgency]"] = urgency.strip()
        if created_at.strip():
            params["date[created_at]"] = created_at.strip()
        if updated_at.strip():
            params["date[updated_at]"] = updated_at.strip()
        if rule_name.strip():
            params["match[rule_name]"] = rule_name.strip()
        if entity_name.strip():
            params["match[entity_name]"] = entity_name.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v1/sic/alerts",
                _PROVIDER,
                "list alerts",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items", []) or []
        total = data.get("total", len(items))
        out = format_entity_list(items, _fmt_alert, noun="alert", total=total)
        if isinstance(total, int) and total > params["offset"] + len(items):
            out += (
                f"\n[Showing {len(items)} from offset {params['offset']} of {total} - "
                "pass a higher offset for more]"
            )
        return out

    async def get_sekoia_alert(alert_id: str) -> str:
        """Get full alert detail by UUID or short id (e.g. ALxxxxxxxxxx)."""
        aid = alert_id.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v1/sic/alerts/{_path(aid)}",
                _PROVIDER,
                f"get alert {aid}",
                headers=headers,
            )
            if err:
                return err
            a = resp.json() or {}
        rule = a.get("rule") or {}
        entity = a.get("entity") or {}
        status = a.get("status") or {}
        alert_type = a.get("alert_type") or {}
        lines = [
            f"Alert {a.get('short_id', aid)} (uuid {a.get('uuid', '?')})",
            f"Title: {_trunc(a.get('title') or rule.get('name'), 120)}",
            f"Status: {status.get('name', '?')} | Urgency: {a.get('urgency', '?')} | "
            f"Category: {alert_type.get('category', alert_type.get('value', 'N/A'))}",
            f"Rule: {_trunc(rule.get('name'), 80) or 'N/A'} "
            f"(severity {rule.get('severity', 'N/A')}, uuid {rule.get('uuid', '?')})",
            f"Entity: {entity.get('name', 'N/A')} (uuid {entity.get('uuid', '?')})",
            f"Created: {_ts(a.get('created_at'))} | Updated: {_ts(a.get('updated_at'))} | "
            f"First seen: {_ts(a.get('first_seen_at'))} | Last seen: {_ts(a.get('last_seen_at'))}",
            f"Comments: {a.get('number_of_total_comments', 0)} | "
            f"Kill chain: {a.get('kill_chain_short_id', 'N/A')}",
        ]
        if a.get("details"):
            lines.append(f"\nDetails: {_trunc(a.get('details'), 600)}")
        countermeasures = a.get("countermeasures") or []
        if countermeasures:
            lines.append("Countermeasures:")
            for cm in countermeasures[:5]:
                lines.append(
                    f"  - [{cm.get('status', '?')}] {_trunc(cm.get('name'), 90)}"
                )
        return "\n".join(lines)

    async def update_sekoia_alert_status(
        alert_id: str, status: str, comment: str = ""
    ) -> str:
        """Transition an alert's workflow status (acknowledge/reject/close/...)."""
        target = status.lower().strip()
        action_uuid = _ALERT_STATUS_ACTIONS.get(target)
        if not action_uuid:
            return (
                f"Error: status must be one of "
                f"{', '.join(sorted(_ALERT_STATUS_ACTIONS))}."
            )
        body: dict[str, Any] = {"action_uuid": action_uuid}
        if comment.strip():
            body["comment"] = comment.strip()
        aid = alert_id.strip()
        async with _client() as client:
            _, err = await safe_request(
                client,
                "patch",
                f"{api}/v1/sic/alerts/{_path(aid)}/workflow",
                _PROVIDER,
                f"update alert {aid} status",
                headers=headers,
                json=body,
            )
            if err:
                return err
        return f"Alert {aid} status changed to '{target}'."

    async def comment_sekoia_alert(alert_id: str, content: str, author: str = "") -> str:
        """Post an analyst comment on an alert."""
        if not content.strip():
            return "Error: content is required."
        body: dict[str, Any] = {"content": content.strip()}
        if author.strip():
            body["author"] = author.strip()
        aid = alert_id.strip()
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{api}/v1/sic/alerts/{_path(aid)}/comments",
                _PROVIDER,
                f"comment alert {aid}",
                headers=headers,
                json=body,
            )
            if err:
                return err
        return f"Comment posted on alert {aid}."

    async def list_sekoia_alert_comments(alert_id: str, limit: int = 25) -> str:
        """Read the comment thread of an alert."""
        aid = alert_id.strip()
        capped = max(1, min(int(limit or 25), 100))
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v1/sic/alerts/{_path(aid)}/comments",
                _PROVIDER,
                f"list alert comments {aid}",
                headers=headers,
                params={"limit": capped},
            )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items", []) or []
        # Defensive slice in case the server ignores/caps the limit differently.
        shown = items[:capped]

        def _fmt(c: dict) -> str:
            who = c.get("author") or c.get("created_by") or "?"
            return (
                f"  [{_ts(c.get('created_at'))}] {who} "
                f"({c.get('created_by_type', '?')}): {_trunc(c.get('content'), 300)}"
            )

        return format_entity_list(
            shown,
            _fmt,
            noun="comment",
            total=data.get("total", len(items)),
            empty_msg=f"Alert {aid} has no comments.",
        )

    # ══════════════════════════════════════════════════════════════════════
    # 3. XDR - CASES
    # ══════════════════════════════════════════════════════════════════════

    async def list_sekoia_cases(
        alert_uuid: str = "",
        case_short_id: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """List cases, optionally the ones containing a given alert."""
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit or 20), 100)),
            "offset": max(0, int(offset or 0)),
        }
        if alert_uuid.strip():
            params["match[alert_uuid]"] = alert_uuid.strip()
        if case_short_id.strip():
            params["match[short_id]"] = case_short_id.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v1/sic/cases",
                _PROVIDER,
                "list cases",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items", []) or []

        def _fmt(c: dict) -> str:
            return (
                f"  {c.get('short_id', '?')} [{c.get('status', c.get('status_name', '?'))}] "
                f"priority {c.get('priority', '?')} - {_trunc(c.get('title'), 80)}\n"
                f"    Created: {_ts(c.get('created_at'))} | Updated: {_ts(c.get('updated_at'))} | "
                f"UUID: {c.get('uuid', '?')}"
            )

        return format_entity_list(
            items, _fmt, noun="case", total=data.get("total", len(items))
        )

    # ══════════════════════════════════════════════════════════════════════
    # 4. XDR - EVENT SEARCH (async job flow)
    # ══════════════════════════════════════════════════════════════════════

    async def search_sekoia_events(
        query: str,
        earliest_time: str = "-1d",
        latest_time: str = "now",
        limit: int = 25,
    ) -> str:
        """Search events with a Dork query (async job -> poll -> results)."""
        body = {
            "term": query,
            "earliest_time": earliest_time.strip() or "-1d",
            "latest_time": latest_time.strip() or "now",
            "term_lang": "dork",
        }
        async with _client(timeout=60) as client:
            start_resp, start_err = await safe_request(
                client,
                "post",
                f"{api}/v1/sic/conf/events/search/jobs",
                _PROVIDER,
                "create event search job",
                headers=headers,
                json=body,
            )
            if start_err:
                return start_err
            job_uuid = (start_resp.json() or {}).get("uuid")
            if not job_uuid:
                return "Error: Sekoia.io did not return a search job uuid."
            job_path = _path(job_uuid)

            status = -1
            for _ in range(_EVENT_SEARCH_MAX_POLLS):
                await asyncio.sleep(_EVENT_SEARCH_POLL_INTERVAL)
                st_resp, st_err = await safe_request(
                    client,
                    "get",
                    f"{api}/v1/sic/conf/events/search/jobs/{job_path}",
                    _PROVIDER,
                    "poll event search job",
                    headers=headers,
                )
                if st_err:
                    return st_err
                status = (st_resp.json() or {}).get("status", -1)
                if status == 2:
                    break
                if status in (3, 4):
                    return (
                        f"Event search job {job_uuid} ended with status "
                        f"'{_JOB_STATUS_NAMES.get(status, status)}'."
                    )
            if status != 2:
                return (
                    f"Event search job {job_uuid} is still "
                    f"'{_JOB_STATUS_NAMES.get(status, status)}' - narrow the time "
                    "window or retry shortly."
                )

            ev_resp, ev_err = await safe_request(
                client,
                "get",
                f"{api}/v1/sic/conf/events/search/jobs/{job_path}/events",
                _PROVIDER,
                "get event search results",
                headers=headers,
                params={"limit": max(1, min(int(limit or 25), 1000))},
            )
            if ev_err:
                return ev_err
            data = ev_resp.json() or {}
        events = data.get("items", []) or []
        total = data.get("total", len(events))
        if not events:
            return (
                f"No events matched '{query}' between {body['earliest_time']} and "
                f"{body['latest_time']}."
            )
        lines = [f"Found {total} event(s) (showing {len(events)}, latest first):"]
        for ev in events:
            ts = ev.get("@timestamp") or ev.get("timestamp") or ""
            compact = json.dumps(
                {k: v for k, v in ev.items() if not str(k).startswith("sekoiaio_")},
                default=str,
                ensure_ascii=False,
            )
            lines.append(f"  [{_ts(ts)}] {_trunc(compact, 600)}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 5. XDR - RULES CATALOG
    # ══════════════════════════════════════════════════════════════════════

    async def list_sekoia_rules(name: str = "", limit: int = 25, page: int = 1) -> str:
        """Browse the detection rules catalog (optionally filtered by name)."""
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit or 25), 100)),
            "page": max(1, int(page or 1)),
        }
        if name.strip():
            params["match[name]"] = name.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v1/sic/conf/rules-catalog/rules",
                _PROVIDER,
                "list rules",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        # The endpoint normally returns {"items": [...], "total": N} but guard
        # the bare-list shape too (data.get on a list would raise).
        items = (
            [r for r in data if isinstance(r, dict)]
            if isinstance(data, list)
            else data.get("items") or []
        )

        def _fmt(r: dict) -> str:
            return (
                f"  {_trunc(r.get('name'), 80)} "
                f"[{'enabled' if r.get('enabled') else 'disabled'}] "
                f"severity {r.get('severity', '?')} | type {r.get('type', '?')}\n"
                f"    UUID: {r.get('uuid', '?')}"
            )

        out = format_entity_list(
            items, _fmt, noun="rule", total=data.get("total") if isinstance(data, dict) else None
        )
        if len(items) >= params["limit"]:
            out += f"\n[More rules may be available - pass page={params['page'] + 1}]"
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 6. XDR - ASSETS
    # ══════════════════════════════════════════════════════════════════════

    async def list_sekoia_assets(
        asset_type: str = "", limit: int = 25, offset: int = 0
    ) -> str:
        """List assets from the asset inventory."""
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit or 25), 100)),
            "offset": max(0, int(offset or 0)),
        }
        if asset_type.strip():
            params["match[type_name]"] = asset_type.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v1/asset-management/assets",
                _PROVIDER,
                "list assets",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items", []) or []

        def _fmt(a: dict) -> str:
            atype = a.get("asset_type") or {}
            crit = a.get("criticity") or {}
            crit_val = crit.get("value") if isinstance(crit, dict) else crit
            return (
                f"  {_trunc(a.get('name'), 70)} [{atype.get('name', a.get('type', '?'))}] "
                f"criticality {crit_val if crit_val is not None else '?'}\n"
                f"    UUID: {a.get('uuid', '?')} | Created: {_ts(a.get('created_at'))}"
            )

        return format_entity_list(
            items, _fmt, noun="asset", total=data.get("total", len(items))
        )

    async def get_sekoia_asset(asset_uuid: str) -> str:
        """Get one asset with its attributes, keys, and criticality."""
        uid = asset_uuid.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v1/asset-management/assets/{_path(uid)}",
                _PROVIDER,
                f"get asset {uid}",
                headers=headers,
            )
            if err:
                return err
            a = resp.json() or {}
        atype = a.get("asset_type") or {}
        crit = a.get("criticity") or {}
        crit_val = crit.get("value") if isinstance(crit, dict) else crit
        lines = [
            f"Asset {a.get('name', uid)} (uuid {a.get('uuid', uid)})",
            f"Type: {atype.get('name', '?')} | Criticality: {crit_val if crit_val is not None else '?'} | "
            f"Community: {a.get('community_uuid', '?')}",
            f"Created: {_ts(a.get('created_at'))} ({a.get('created_by_type', '?')}) | "
            f"Updated: {_ts(a.get('updated_at'))}",
        ]
        if a.get("description"):
            lines.append(f"Description: {_trunc(a.get('description'), 300)}")
        attrs = a.get("attributes") or []
        if attrs:
            lines.append("Attributes:")
            for attr in attrs[:15]:
                lines.append(
                    f"  - {attr.get('name', '?')} = {_trunc(attr.get('value'), 80)} "
                    f"(uuid {attr.get('uuid', '?')})"
                )
        keys = a.get("keys") or []
        if keys:
            lines.append("Keys:")
            for key in keys[:15]:
                lines.append(
                    f"  - {key.get('name', '?')} = {_trunc(key.get('value'), 80)} "
                    f"(uuid {key.get('uuid', '?')})"
                )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_sekoia_status", get_sekoia_status)
    executor.register("lookup_sekoia_indicator", lookup_sekoia_indicator)
    executor.register("get_sekoia_indicator_context", get_sekoia_indicator_context)
    executor.register("search_sekoia_intelligence", search_sekoia_intelligence)
    executor.register("get_sekoia_intelligence_object", get_sekoia_intelligence_object)
    executor.register("search_sekoia_observables", search_sekoia_observables)
    executor.register("get_sekoia_feed_objects", get_sekoia_feed_objects)
    executor.register("manage_sekoia_ioc_collections", manage_sekoia_ioc_collections)
    executor.register("list_sekoia_alerts", list_sekoia_alerts)
    executor.register("get_sekoia_alert", get_sekoia_alert)
    executor.register("update_sekoia_alert_status", update_sekoia_alert_status)
    executor.register("comment_sekoia_alert", comment_sekoia_alert)
    executor.register("list_sekoia_alert_comments", list_sekoia_alert_comments)
    executor.register("list_sekoia_cases", list_sekoia_cases)
    executor.register("search_sekoia_events", search_sekoia_events)
    executor.register("list_sekoia_rules", list_sekoia_rules)
    executor.register("list_sekoia_assets", list_sekoia_assets)
    executor.register("get_sekoia_asset", get_sekoia_asset)

    return [
        ToolDefinition(
            name="get_sekoia_status",
            description=(
                "Validate the Sekoia.io connection and API key. Use first if "
                "other Sekoia.io tools fail with authentication errors."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="lookup_sekoia_indicator",
            description=(
                "Check whether an IOC (IP, domain, URL, file hash, email, ...) is a "
                "known indicator in the Sekoia.io Intelligence Center. Returns "
                "matching indicators with pattern, validity window, and revocation "
                "state. Use get_sekoia_indicator_context afterwards for the full "
                "threat context (related malware, actors, mitigations)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "The observable value, e.g. '1.2.3.4' or 'evil.example.com'",
                    },
                    "type": {
                        "type": "string",
                        "description": (
                            "STIX observable type, e.g. ipv4-addr, ipv6-addr, "
                            "domain-name, url, file, email-addr. Recommended for "
                            "precise matches."
                        ),
                    },
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["value"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_sekoia_indicator_context",
            description=(
                "Get the full threat context of an IOC from the Sekoia.io "
                "Intelligence Center: the indicator plus its related malware, "
                "intrusion sets, threat actors, campaigns, attack patterns, and "
                "recommended courses of action. Use for enriching a matched IOC "
                "during alert triage."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "The observable value, e.g. '1.2.3.4'",
                    },
                    "type": {
                        "type": "string",
                        "description": (
                            "STIX observable type: ipv4-addr, ipv6-addr, "
                            "domain-name, url, file, email-addr, ..."
                        ),
                    },
                },
                "required": ["value", "type"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_sekoia_intelligence",
            description=(
                "Full-text search of Sekoia.io Intelligence Center STIX objects "
                "(threat actors, intrusion sets, malware, campaigns, reports, "
                "attack patterns). Use for questions like 'what does Sekoia know "
                "about APT28?'. Returns object names, types, ids, and summaries - "
                "pass an id to get_sekoia_intelligence_object for detail."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "term": {
                        "type": "string",
                        "description": "Search term, e.g. 'APT28' or 'Emotet'",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["term"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_sekoia_intelligence_object",
            description=(
                "Get one Intelligence Center STIX object by its id (e.g. "
                "'intrusion-set--...' from search_sekoia_intelligence). Set "
                "include_context=true to also list its first-level related "
                "objects (relationships, courses of action, sources)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_id": {
                        "type": "string",
                        "description": "STIX object id, e.g. 'malware--abc...'",
                    },
                    "include_context": {"type": "boolean", "default": False},
                },
                "required": ["object_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_sekoia_observables",
            description=(
                "Search Sekoia.io Intelligence Center observables by value and/or "
                "type. Unlike lookup_sekoia_indicator (which finds *indicators* - "
                "detection-ready IOCs), this searches the raw observable database "
                "including tags. Provide value, type, or both."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Observable value to match, e.g. '1.2.3.4'",
                    },
                    "type": {
                        "type": "string",
                        "description": "Observable type, e.g. ipv4-addr, domain-name, file",
                    },
                    "limit": {"type": "integer", "default": 20},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_sekoia_feed_objects",
            description=(
                "Consume STIX objects from a Sekoia.io intelligence feed "
                "(collection) with cursor pagination. Use the feed id from the "
                "Sekoia.io Intelligence Center Feeds page. Pass the returned "
                "cursor to continue where the previous call stopped."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "feed_id": {
                        "type": "string",
                        "description": "Feed / collection id (e.g. 'd6092c37-d8d7-45c3-8aff-c4dc26030608')",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Objects per page (max 2000)",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Pagination cursor returned by the previous call",
                    },
                },
                "required": ["feed_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_sekoia_ioc_collections",
            description=(
                "Manage Sekoia.io IOC collections (your own CTI used for "
                "detection). Actions: 'create_collection' (name required), "
                "'list_indicators' (collection_id required, optional term filter), "
                "'add_indicators' (collection_id + indicators required - one IOC "
                "value per line; import is asynchronous), and 'delete_indicator' "
                "(collection_id + indicator_id required - PERMANENT, cannot be "
                "undone)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_IOC_COLLECTION_ACTIONS),
                    },
                    "collection_id": {
                        "type": "string",
                        "description": "IOC collection id (e.g. 'ioc-collection--...')",
                    },
                    "name": {
                        "type": "string",
                        "description": "New collection name (create_collection)",
                    },
                    "indicators": {
                        "type": "string",
                        "description": (
                            "IOC values to add, one per line (IPs, domains, "
                            "hashes, URLs) - add_indicators"
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description for added indicators",
                    },
                    "term": {
                        "type": "string",
                        "description": "Filter term when listing indicators",
                    },
                    "indicator_id": {
                        "type": "string",
                        "description": "Indicator id to delete (delete_indicator)",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_sekoia_alerts",
            description=(
                "List Sekoia.io XDR alerts with filters: status "
                "(Pending/Acknowledged/Ongoing/Rejected/Closed, comma-separated), "
                "urgency range ('min,max' e.g. '80,100'), created_at/updated_at "
                "date ranges ('from,to' - ISO8601 or relative like '-3d,now'), "
                "rule_name, and entity_name. Returns short ids and UUIDs for "
                "follow-up calls (get_sekoia_alert, update_sekoia_alert_status)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Status name(s), comma-separated: Pending, Acknowledged, Ongoing, Rejected, Closed",
                    },
                    "urgency": {
                        "type": "string",
                        "description": "Urgency range 'min,max', e.g. '80,100'",
                    },
                    "created_at": {
                        "type": "string",
                        "description": "Creation date range 'from,to', e.g. '-7d,now'",
                    },
                    "updated_at": {
                        "type": "string",
                        "description": "Update date range 'from,to', e.g. '-1d,now'",
                    },
                    "rule_name": {"type": "string", "description": "Match by detection rule name"},
                    "entity_name": {"type": "string", "description": "Match by entity name"},
                    "sort_by": {
                        "type": "string",
                        "enum": ["created_at", "updated_at", "urgency", "status", "target"],
                        "default": "created_at",
                    },
                    "direction": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                    "limit": {"type": "integer", "default": 20, "description": "Max 100"},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_sekoia_alert",
            description=(
                "Get full detail of a Sekoia.io alert by UUID or short id (e.g. "
                "'ALUnyZCYZ9Ga'): rule, entity, urgency, status, timeline, "
                "comments count, and recommended countermeasures."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {
                        "type": "string",
                        "description": "Alert UUID or short id (ALxxxxxxxxxx)",
                    },
                },
                "required": ["alert_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="update_sekoia_alert_status",
            description=(
                "Change a Sekoia.io alert's workflow status: pending, "
                "acknowledged (being evaluated), ongoing (confirmed true "
                "positive), rejected (false positive), or closed (addressed). "
                "Optionally attach a comment explaining the transition."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {
                        "type": "string",
                        "description": "Alert UUID or short id",
                    },
                    "status": {
                        "type": "string",
                        "enum": sorted(_ALERT_STATUS_ACTIONS),
                    },
                    "comment": {
                        "type": "string",
                        "description": "Optional comment explaining the status change",
                    },
                },
                "required": ["alert_id", "status"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="comment_sekoia_alert",
            description="Post an analyst comment on a Sekoia.io alert (by UUID or short id).",
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string", "description": "Alert UUID or short id"},
                    "content": {"type": "string", "description": "Comment text"},
                    "author": {"type": "string", "description": "Optional author display name"},
                },
                "required": ["alert_id", "content"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_sekoia_alert_comments",
            description="Read the comment thread of a Sekoia.io alert (by UUID or short id).",
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string", "description": "Alert UUID or short id"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["alert_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_sekoia_cases",
            description=(
                "List Sekoia.io cases. Filter by alert_uuid to find the cases "
                "containing a given alert, or by case_short_id (e.g. "
                "'CAQNurTJM8q2') to fetch one case."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_uuid": {
                        "type": "string",
                        "description": "Only cases containing this alert UUID",
                    },
                    "case_short_id": {
                        "type": "string",
                        "description": "Case short id, e.g. 'CAQNurTJM8q2'",
                    },
                    "limit": {"type": "integer", "default": 20},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_sekoia_events",
            description=(
                "Search Sekoia.io XDR events (telemetry) with a Dork query - the "
                "same language as the Sekoia.io Events page, e.g. "
                "'source.ip:\"1.2.3.4\"' or 'process.name:powershell.exe'. Runs "
                "an asynchronous search job (create -> poll -> results) and "
                "returns the latest matching events. Times accept ISO8601 or "
                "relative formats ('-30d', '-2h', 'now')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Dork query, e.g. 'source.ip:\"1.2.3.4\"'",
                    },
                    "earliest_time": {
                        "type": "string",
                        "default": "-1d",
                        "description": "Window start (ISO8601 or relative, e.g. '-7d')",
                    },
                    "latest_time": {
                        "type": "string",
                        "default": "now",
                        "description": "Window end (ISO8601 or 'now')",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": "Max events returned (up to 1000)",
                    },
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_sekoia_rules",
            description=(
                "Browse the Sekoia.io detection rules catalog: name, enabled "
                "state, severity, and type (sigma, correlation, ...). Filter by "
                "rule name (comma-separated names supported)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Match rules by name"},
                    "limit": {"type": "integer", "default": 25, "description": "Max 100"},
                    "page": {"type": "integer", "default": 1, "description": "Page number (1-based)"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_sekoia_assets",
            description=(
                "List Sekoia.io assets (inventory) with their type and "
                "criticality. Filter by asset type name (e.g. 'computer', "
                "'network'). Returns UUIDs for get_sekoia_asset."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "asset_type": {
                        "type": "string",
                        "description": "Asset type name, e.g. 'computer'",
                    },
                    "limit": {"type": "integer", "default": 25, "description": "Max 100"},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_sekoia_asset",
            description=(
                "Get one Sekoia.io asset by UUID: type, criticality, description, "
                "attributes, and detection keys."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "asset_uuid": {"type": "string", "description": "Asset UUID"},
                },
                "required": ["asset_uuid"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
