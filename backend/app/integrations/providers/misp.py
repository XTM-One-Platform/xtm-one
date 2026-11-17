# SPDX-License-Identifier: Apache-2.0

"""MISP integration - Agentic threat intelligence sharing.

Full-fledged MISP (Malware Information Sharing Platform / Open Source Threat
Intelligence Platform) integration, designed as a best-in-class building block
for an agentic CTI / SOC team:

  - Events (*gathering + producing intelligence*): restSearch, get, create,
    update, publish/unpublish, delete
  - Attributes (*IOC management*): restSearch, get, add, edit, delete,
    describeTypes (valid type/category discovery)
  - Tags (*context + TLP*): list/search, attach/remove on events AND
    attributes via UUID (supports local tags)
  - Sightings (*detection feedback loop*): add (sighting / false-positive /
    expiration), list per event
  - Objects: restSearch over structured MISP objects (file, domain-ip, ...)
  - Galaxies & clusters (*threat actors, malware, ATT&CK*): browse + drill in
  - Warninglists (*false-positive triage*): list + checkValue
  - Feeds (*OSINT ingestion*): list + enable/disable/fetch/cache
  - Organisations & users (read) + instance version / connected identity

Authentication
--------------
MISP authenticates with an **automation key** (auth key) sent RAW in the
``Authorization`` header (``Authorization: <key>`` - no ``Bearer``/``Token``
prefix), plus ``Accept: application/json`` and ``Content-Type:
application/json``. ``integration_tools._get_token`` assembles the compound
``base_url||api_key`` token this builder parses (base URL = the MISP instance
host - the API lives at the web UI host root). Note: MISP returns HTTP 403
(not 401) for an invalid key.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    format_entity_list,
    format_pagination_hint,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "MISP"
_SOURCE = "integration:misp"

# Upper bound on the number of items requested in a single paginated call.
_MAX_LIMIT = 500

# MISP enum value maps - rendered in tool output so the agent never has to
# guess what a numeric ID means, and accepted as friendly inputs on writes.
_THREAT_LEVELS = {"1": "High", "2": "Medium", "3": "Low", "4": "Undefined"}
_THREAT_LEVEL_INPUT = {
    "high": "1",
    "medium": "2",
    "low": "3",
    "undefined": "4",
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
}
_ANALYSIS_LEVELS = {"0": "Initial", "1": "Ongoing", "2": "Complete"}
_ANALYSIS_INPUT = {
    "initial": "0",
    "ongoing": "1",
    "complete": "2",
    "0": "0",
    "1": "1",
    "2": "2",
}
_DISTRIBUTIONS = {
    "0": "Your organisation only",
    "1": "This community only",
    "2": "Connected communities",
    "3": "All communities",
    "4": "Sharing group",
    "5": "Inherit event",
}
_SIGHTING_TYPES = {"sighting": "0", "false_positive": "1", "expiration": "2"}
_SIGHTING_LABELS = {"0": "Sighting", "1": "False positive", "2": "Expiration"}
_FEED_ACTIONS = frozenset({"enable", "disable", "fetch", "fetch_all", "cache"})
_PUBLISH_ACTIONS = frozenset({"publish", "unpublish"})


def misp_api_base(base_url: str) -> str:
    """Normalise the MISP instance base URL (strip whitespace + trailing '/').

    MISP serves the REST API at the web UI host root (no ``/api`` prefix), so
    the returned value is used directly as the endpoint prefix. Returns ``""``
    when no base URL is configured.
    """
    return (base_url or "").strip().rstrip("/")


def build_misp_headers(api_key: str | None) -> dict[str, str]:
    """Build MISP auth headers: the automation key goes RAW in Authorization.

    MISP expects ``Authorization: <key>`` with NO ``Bearer``/``Token`` prefix.
    A blank/whitespace-only key omits the header entirely so the request falls
    through to a clean 403 instead of sending an empty header value.
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    key = (api_key or "").strip()
    if key:
        headers["Authorization"] = key
    return headers


class MISPIntegration(BaseIntegration):
    provider_name = "misp"
    display_name = "MISP"
    icon = "Share2"
    description = (
        "Connect MISP (Open Source Threat Intelligence Platform) for threat "
        "event and IOC management, tagging, sightings, galaxies, "
        "warninglists, and feeds."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        base_raw, api_key = parse_compound_token(token, 2, greedy_last=True)
        base_url = misp_api_base(base_raw)
        if not base_url or not (api_key or "").strip():
            return False
        async with egress_client(timeout=15) as client:
            resp, _err = await safe_request(
                client,
                "get",
                f"{base_url}/users/view/me",
                _PROVIDER,
                "validate connection",
                headers=build_misp_headers(api_key),
            )
            return resp is not None

    async def get_user_info(self, token: str) -> dict:
        base_raw, api_key = parse_compound_token(token, 2, greedy_last=True)
        base_url = misp_api_base(base_raw)
        # Same guards as validate_connection: a misconfigured integration
        # (missing base_url yields a ``||key`` compound token) must surface a
        # clear configuration error, not a relative-URL request failure.
        if not base_url:
            return {
                "error": (
                    "MISP base URL is not configured - set base_url in the "
                    "integration's extra config."
                )
            }
        if not (api_key or "").strip():
            return {"error": "MISP API key is not configured."}
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/users/view/me",
                _PROVIDER,
                "get user info",
                headers=build_misp_headers(api_key),
            )
            if resp is None:
                return {"error": err}
            data = resp.json()
            user = data.get("User", {}) if isinstance(data, dict) else {}
            org = data.get("Organisation", {}) if isinstance(data, dict) else {}
            role = data.get("Role", {}) if isinstance(data, dict) else {}
            return {
                "email": user.get("email"),
                "org": org.get("name"),
                "role": role.get("name"),
            }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _clamp(limit: Any, default: int = 25) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, _MAX_LIMIT))


def _page_hint(count: int, limit: int, page: int) -> str:
    """Pagination hint when a full page came back (MISP has no cursor/total)."""
    return format_pagination_hint(
        str(page + 1) if count >= limit else None, param_name="page"
    )


def _tag_names(container: dict) -> list[str]:
    names: list[str] = []
    for entry in container.get("Tag") or []:
        if isinstance(entry, dict) and entry.get("name"):
            names.append(str(entry["name"]))
    return names


def _split_tags(tags: str) -> list[str]:
    """Split a comma-separated tag filter into a list (keeps '!' exclusions)."""
    return [t.strip() for t in (tags or "").split(",") if t.strip()]


def _fmt_event(event: dict, *, detail: bool = False) -> str:
    """Format a MISP event dict (the inner ``Event`` object) readably."""
    ev_id = event.get("id", "?")
    uuid = event.get("uuid", "?")
    info = _trunc(event.get("info", "No info"), 160)
    date = event.get("date", "?")
    threat = _THREAT_LEVELS.get(str(event.get("threat_level_id", "")), "?")
    analysis = _ANALYSIS_LEVELS.get(str(event.get("analysis", "")), "?")
    dist = _DISTRIBUTIONS.get(str(event.get("distribution", "")), "?")
    published = "published" if event.get("published") else "unpublished"
    attr_count = event.get("attribute_count", "?")
    orgc = (event.get("Orgc") or {}).get("name", "?")

    lines = [f"Event {ev_id}: {info}"]
    lines.append(
        f"  Date: {date} | Threat: {threat} | Analysis: {analysis} | {published}"
    )
    lines.append(f"  Org: {orgc} | Attributes: {attr_count} | UUID: {uuid}")
    tags = _tag_names(event)
    if tags:
        lines.append(f"  Tags: {', '.join(tags[:10])}")
    if detail:
        lines.append(f"  Distribution: {dist}")
        attributes = event.get("Attribute") or []
        if attributes:
            lines.append(f"\n  Attributes ({len(attributes)} shown):")
            for attr in attributes[:30]:
                lines.append("    " + _fmt_attribute(attr, compact=True))
            if len(attributes) > 30:
                lines.append(f"    ... and {len(attributes) - 30} more")
        objects = event.get("Object") or []
        if objects:
            lines.append(f"\n  Objects ({len(objects)} shown):")
            for obj in objects[:15]:
                name = obj.get("name", "?")
                obj_id = obj.get("id", "?")
                n_attrs = len(obj.get("Attribute") or [])
                lines.append(f"    Object {obj_id}: {name} ({n_attrs} attributes)")
            if len(objects) > 15:
                lines.append(f"    ... and {len(objects) - 15} more")
    return "\n".join(lines)


def _fmt_attribute(attr: dict, *, compact: bool = False) -> str:
    """Format a MISP attribute dict readably."""
    attr_id = attr.get("id", "?")
    attr_type = attr.get("type", "?")
    category = attr.get("category", "?")
    value = _trunc(attr.get("value", ""), 200)
    to_ids = "IDS" if attr.get("to_ids") else "no-IDS"
    if compact:
        return f"[{attr_id}] {attr_type} = {value} ({category}, {to_ids})"
    event_id = attr.get("event_id", "?")
    uuid = attr.get("uuid", "?")
    lines = [f"Attribute {attr_id}: {attr_type} = {value}"]
    lines.append(f"  Category: {category} | {to_ids} | Event: {event_id}")
    lines.append(f"  UUID: {uuid}")
    comment = attr.get("comment", "")
    if comment:
        lines.append(f"  Comment: {_trunc(comment, 150)}")
    tags = _tag_names(attr)
    if tags:
        lines.append(f"  Tags: {', '.join(tags[:8])}")
    return "\n".join(lines)


def _extract_events(data: Any) -> list[dict]:
    """Unwrap an events restSearch response into inner Event dicts."""
    if isinstance(data, dict):
        data = data.get("response", data)
    if not isinstance(data, list):
        return []
    events = []
    for item in data:
        if isinstance(item, dict):
            events.append(item.get("Event", item))
    return events


def _extract_attributes(data: Any) -> list[dict]:
    """Unwrap an attributes restSearch response into Attribute dicts."""
    if isinstance(data, dict):
        inner = data.get("response", data)
        if isinstance(inner, dict):
            attrs = inner.get("Attribute", [])
            return attrs if isinstance(attrs, list) else []
        data = inner
    if isinstance(data, list):
        return [a for a in data if isinstance(a, dict)]
    return []


def _error_detail(data: Any) -> str:
    """Extract MISP's error envelope message when a 200 carries errors."""
    if isinstance(data, dict):
        errors = data.get("errors")
        if errors:
            return _trunc(errors, 300)
        if data.get("saved") is False and data.get("message"):
            return _trunc(data["message"], 300)
    return ""


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    base_raw, api_key = parse_compound_token(token, 2, greedy_last=True)
    base_url = misp_api_base(base_raw)

    if not base_url:
        return []  # No base_url - can't make API calls
    if not (api_key or "").strip():
        return []  # No API key - unusable tools would 403 on every call

    headers = build_misp_headers(api_key)

    # ══════════════════════════════════════════════════════════════════════
    # 0. INSTANCE / IDENTITY
    # ══════════════════════════════════════════════════════════════════════

    async def get_misp_version() -> str:
        """Get the MISP instance version and connectivity status."""
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/servers/getVersion",
                _PROVIDER,
                "get instance version",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            version = data.get("version", "?")
            return f"MISP instance version: {version}"

    async def get_misp_me() -> str:
        """Get the profile of the account this MISP integration connects as."""
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/users/view/me",
                _PROVIDER,
                "get current user profile",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            user = data.get("User", {})
            org = data.get("Organisation", {})
            role = data.get("Role", {})
            lines = [
                f"Authenticated as: {user.get('email', '?')}",
                f"  User ID: {user.get('id', '?')} | Org: {org.get('name', '?')} "
                f"(id {user.get('org_id', '?')})",
                f"  Role: {role.get('name', '?')}",
            ]
            if role.get("perm_admin") or role.get("perm_site_admin"):
                lines.append("  (Admin privileges - user management tools available)")
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 1. SEARCH (restSearch)
    # ══════════════════════════════════════════════════════════════════════

    async def search_misp_attributes(
        value: str = "",
        type: str = "",
        category: str = "",
        tags: str = "",
        event_id: str = "",
        org: str = "",
        date_from: str = "",
        date_to: str = "",
        to_ids: bool = False,
        published_only: bool = False,
        enforce_warninglist: bool = False,
        limit: int = 25,
        page: int = 1,
    ) -> str:
        """Search MISP attributes (IOCs) with structured filters via restSearch.

        The primary IOC lookup: pass an indicator in value (e.g. an IP, domain,
        hash, URL) to find every attribute matching it across events.
        """
        limit = _clamp(limit)
        page = max(1, int(page or 1))
        body: dict[str, Any] = {"returnFormat": "json", "limit": limit, "page": page}
        if value:
            body["value"] = value
        if type:
            body["type"] = type
        if category:
            body["category"] = category
        if tags:
            body["tags"] = _split_tags(tags)
        if event_id:
            body["eventid"] = event_id
        if org:
            body["org"] = org
        if date_from:
            body["from"] = date_from
        if date_to:
            body["to"] = date_to
        if to_ids:
            body["to_ids"] = 1
        if published_only:
            body["published"] = 1
        if enforce_warninglist:
            body["enforceWarninglist"] = 1

        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/attributes/restSearch",
                _PROVIDER,
                "search attributes",
                headers=headers,
                json=body,
            )
            if err:
                return err
            attrs = _extract_attributes(resp.json())
            result = format_entity_list(
                attrs,
                lambda a: _fmt_attribute(a, compact=True)
                + f" [event {a.get('event_id', '?')}]",
                noun="attribute",
                empty_msg="No attributes found for the given filters.",
            )
            return result + _page_hint(len(attrs), limit, page)

    async def search_misp_events(
        keyword: str = "",
        event_info: str = "",
        tags: str = "",
        org: str = "",
        date_from: str = "",
        date_to: str = "",
        threat_level: str = "",
        published_only: bool = False,
        limit: int = 25,
        page: int = 1,
    ) -> str:
        """Search MISP events with structured filters via restSearch.

        Returns event metadata only (no attribute payloads) - use
        get_misp_event for the full detail of a specific event. Note: when
        keyword is set, MISP's searchall/wildcard mode ignores every other
        filter, so use keyword alone or the structured filters alone.
        """
        limit = _clamp(limit)
        page = max(1, int(page or 1))
        body: dict[str, Any] = {
            "returnFormat": "json",
            "metadata": 1,  # metadata only - never pull full attribute payloads
            "limit": limit,
            "page": page,
        }
        if keyword:
            # searchall/wildcard is a raw SQL LIKE: substring matching needs
            # explicit '%' delimiters, so wrap bare keywords for the
            # "full-text keyword" semantics the tool advertises.
            body["value"] = keyword if "%" in keyword else f"%{keyword}%"
            body["searchall"] = 1
        if event_info:
            body["eventinfo"] = event_info
        if tags:
            body["tags"] = _split_tags(tags)
        if org:
            body["org"] = org
        if date_from:
            body["from"] = date_from
        if date_to:
            body["to"] = date_to
        if threat_level:
            level = _THREAT_LEVEL_INPUT.get(threat_level.strip().lower())
            if not level:
                return (
                    "Error: threat_level must be one of: high, medium, low, undefined."
                )
            body["threat_level_id"] = level
        if published_only:
            body["published"] = 1

        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/events/restSearch",
                _PROVIDER,
                "search events",
                headers=headers,
                json=body,
            )
            if err:
                return err
            events = _extract_events(resp.json())
            result = format_entity_list(
                events,
                _fmt_event,
                noun="event",
                empty_msg="No events found for the given filters.",
            )
            return result + _page_hint(len(events), limit, page)

    # ══════════════════════════════════════════════════════════════════════
    # 2. EVENTS
    # ══════════════════════════════════════════════════════════════════════

    async def get_misp_event(event_id: str) -> str:
        """Get full details of a MISP event (attributes, objects, tags)."""
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/events/view/{event_id}",
                _PROVIDER,
                f"get event {event_id}",
                headers=headers,
            )
            if err:
                return err
            event = resp.json().get("Event", {})
            if not event:
                return f"Event {event_id} not found or returned no data."
            result = _fmt_event(event, detail=True)
            return result + f"\n\nEvent link: {base_url}/events/view/{event.get('id', event_id)}"

    async def create_misp_event(
        info: str,
        distribution: int = 0,
        threat_level: str = "undefined",
        analysis: str = "initial",
        date: str = "",
        tags: str = "",
    ) -> str:
        """Create a new MISP event. Add IOCs afterwards with add_misp_attribute."""
        level = _THREAT_LEVEL_INPUT.get(threat_level.strip().lower())
        if not level:
            return "Error: threat_level must be one of: high, medium, low, undefined."
        analysis_id = _ANALYSIS_INPUT.get(analysis.strip().lower())
        if analysis_id is None:
            return "Error: analysis must be one of: initial, ongoing, complete."
        event_body: dict[str, Any] = {
            "info": info,
            "distribution": str(distribution),
            "threat_level_id": level,
            "analysis": analysis_id,
        }
        if date:
            event_body["date"] = date
        if tags:
            event_body["Tag"] = [{"name": t} for t in _split_tags(tags)]

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/events/add",
                _PROVIDER,
                "create event",
                headers=headers,
                json={"Event": event_body},
            )
            if err:
                return err
            data = resp.json()
            detail = _error_detail(data)
            if detail:
                return f"Error: MISP rejected the event: {detail}"
            event = data.get("Event", {})
            return (
                f"Event created: id {event.get('id', '?')} "
                f"(uuid {event.get('uuid', '?')}) - {event.get('info', info)}\n"
                f"Link: {base_url}/events/view/{event.get('id', '')}"
            )

    async def update_misp_event(
        event_id: str,
        info: str = "",
        distribution: int = -1,
        threat_level: str = "",
        analysis: str = "",
        date: str = "",
    ) -> str:
        """Update metadata on an existing MISP event. Only provided fields change."""
        event_body: dict[str, Any] = {}
        if info:
            event_body["info"] = info
        if distribution >= 0:
            event_body["distribution"] = str(distribution)
        if threat_level:
            level = _THREAT_LEVEL_INPUT.get(threat_level.strip().lower())
            if not level:
                return (
                    "Error: threat_level must be one of: high, medium, low, undefined."
                )
            event_body["threat_level_id"] = level
        if analysis:
            analysis_id = _ANALYSIS_INPUT.get(analysis.strip().lower())
            if analysis_id is None:
                return "Error: analysis must be one of: initial, ongoing, complete."
            event_body["analysis"] = analysis_id
        if date:
            event_body["date"] = date
        if not event_body:
            return "No fields to update."

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "put",
                f"{base_url}/events/edit/{event_id}",
                _PROVIDER,
                f"update event {event_id}",
                headers=headers,
                json={"Event": event_body},
            )
            if err:
                return err
            detail = _error_detail(resp.json())
            if detail:
                return f"Error: MISP rejected the update: {detail}"
            return f"Event {event_id} updated successfully."

    async def publish_misp_event(event_id: str, action: str = "publish") -> str:
        """Publish or unpublish a MISP event (action: 'publish' or 'unpublish').

        Publishing distributes the event to the configured communities and
        synced servers - make sure the event content is final first.
        """
        action = (action or "publish").strip().lower()
        if action not in _PUBLISH_ACTIONS:
            return "Error: action must be one of: publish, unpublish."
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/events/{action}/{event_id}",
                _PROVIDER,
                f"{action} event {event_id}",
                headers=headers,
            )
            if err:
                return err
            detail = _error_detail(resp.json())
            if detail:
                return f"Error: MISP could not {action} the event: {detail}"
            return f"Event {event_id} {action}ed successfully."

    async def delete_misp_event(event_id: str) -> str:
        """Delete a MISP event permanently. This action cannot be undone."""
        # POST is the documented automation form (PyMISP does the same);
        # the controller also accepts HTTP DELETE, but POST survives strict
        # proxies/WAFs in front of self-hosted instances.
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/events/delete/{event_id}",
                _PROVIDER,
                f"delete event {event_id}",
                headers=headers,
            )
            if err:
                return err
            detail = _error_detail(resp.json())
            if detail:
                return f"Error: MISP could not delete the event: {detail}"
            return f"Event {event_id} deleted."

    # ══════════════════════════════════════════════════════════════════════
    # 3. ATTRIBUTES
    # ══════════════════════════════════════════════════════════════════════

    async def get_misp_attribute(attribute_id: str) -> str:
        """Get full details of a single MISP attribute by ID or UUID."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/attributes/view/{attribute_id}",
                _PROVIDER,
                f"get attribute {attribute_id}",
                headers=headers,
            )
            if err:
                return err
            attr = resp.json().get("Attribute", {})
            if not attr:
                return f"Attribute {attribute_id} not found or returned no data."
            return _fmt_attribute(attr)

    async def describe_misp_attribute_types(category: str = "") -> str:
        """List valid MISP attribute categories and types before adding IOCs.

        Without a category: lists all categories. With a category (e.g.
        'Network activity'): lists the attribute types valid in that category.
        """
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/attributes/describeTypes",
                _PROVIDER,
                "describe attribute types",
                headers=headers,
            )
            if err:
                return err
            result = resp.json().get("result", {})
            mappings = result.get("category_type_mappings", {})
            if category:
                types = mappings.get(category)
                if types is None:
                    available = ", ".join(sorted(mappings.keys()))
                    return (
                        f"Unknown category '{category}'. "
                        f"Available categories: {available}"
                    )
                return (
                    f"Attribute types valid in category '{category}' "
                    f"({len(types)}):\n  " + ", ".join(types)
                )
            categories = result.get("categories", sorted(mappings.keys()))
            lines = [f"MISP attribute categories ({len(categories)}):"]
            for cat in categories:
                n = len(mappings.get(cat, []))
                lines.append(f"  {cat} ({n} types)")
            lines.append(
                "\nCall again with a category to list its valid attribute types."
            )
            return "\n".join(lines)

    async def add_misp_attribute(
        event_id: str,
        type: str,
        value: str,
        category: str = "",
        to_ids: bool = True,
        comment: str = "",
        distribution: int = 5,
    ) -> str:
        """Add an attribute (IOC) to a MISP event.

        Use describe_misp_attribute_types first if unsure about the valid
        type/category combination.
        """
        attr_body: dict[str, Any] = {
            "type": type,
            "value": value,
            "to_ids": to_ids,
            "distribution": str(distribution),
        }
        if category:
            attr_body["category"] = category
        if comment:
            attr_body["comment"] = comment

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/attributes/add/{event_id}",
                _PROVIDER,
                f"add attribute to event {event_id}",
                headers=headers,
                json=attr_body,
            )
            if err:
                return err
            data = resp.json()
            detail = _error_detail(data)
            if detail:
                return f"Error: MISP rejected the attribute: {detail}"
            attr = data.get("Attribute", {})
            return (
                f"Attribute added to event {event_id}: id {attr.get('id', '?')} "
                f"({attr.get('type', type)} = {_trunc(attr.get('value', value), 100)})"
            )

    async def update_misp_attribute(
        attribute_id: str,
        value: str = "",
        type: str = "",
        category: str = "",
        to_ids: int = -1,
        comment: str = "",
    ) -> str:
        """Update an existing MISP attribute. Only provided fields change.

        Pass to_ids as 1 (enable IDS flag) or 0 (disable); -1 leaves it as-is.
        """
        attr_body: dict[str, Any] = {}
        if value:
            attr_body["value"] = value
        if type:
            attr_body["type"] = type
        if category:
            attr_body["category"] = category
        if to_ids in (0, 1):
            attr_body["to_ids"] = bool(to_ids)
        if comment:
            attr_body["comment"] = comment
        if not attr_body:
            return "No fields to update."

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "put",
                f"{base_url}/attributes/edit/{attribute_id}",
                _PROVIDER,
                f"update attribute {attribute_id}",
                headers=headers,
                json=attr_body,
            )
            if err:
                return err
            detail = _error_detail(resp.json())
            if detail:
                return f"Error: MISP rejected the update: {detail}"
            return f"Attribute {attribute_id} updated successfully."

    async def delete_misp_attribute(attribute_id: str, hard: bool = False) -> str:
        """Delete a MISP attribute (soft delete by default; hard is permanent)."""
        url = f"{base_url}/attributes/delete/{attribute_id}"
        if hard:
            url += "/1"
        # POST is the documented automation form ("POST /attributes/delete/"
        # in the MISP automation book; PyMISP does the same). The controller
        # also accepts HTTP DELETE, but POST survives strict proxies/WAFs.
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                url,
                _PROVIDER,
                f"delete attribute {attribute_id}",
                headers=headers,
            )
            if err:
                return err
            detail = _error_detail(resp.json())
            if detail:
                return f"Error: MISP could not delete the attribute: {detail}"
            mode = "hard-deleted (permanent)" if hard else "soft-deleted"
            return f"Attribute {attribute_id} {mode}."

    # ══════════════════════════════════════════════════════════════════════
    # 4. TAGS
    # ══════════════════════════════════════════════════════════════════════

    async def list_misp_tags(search: str = "", limit: int = 50) -> str:
        """List or search tags on the MISP instance (taxonomies, TLP, galaxies)."""
        limit = _clamp(limit, default=50)
        if search:
            # /tags/search/<term> is a raw SQL LIKE on the tag name (PyMISP:
            # "use % for substrings matches"), so wrap bare terms in '%' for
            # the substring semantics this tool advertises. The term is then
            # percent-encoded so '/' or '%' cannot break the path.
            term = search if "%" in search else f"%{search}%"
            url = f"{base_url}/tags/search/{quote(term, safe='')}"
        else:
            url = f"{base_url}/tags"
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                url,
                _PROVIDER,
                "list tags",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            raw = data.get("Tag", data) if isinstance(data, dict) else data
            tags: list[dict] = []
            if isinstance(raw, list):
                for entry in raw:
                    if isinstance(entry, dict):
                        tags.append(entry.get("Tag", entry))
            tags = tags[:limit]
            return format_entity_list(
                tags,
                lambda t: f"  {t.get('name', '?')} (id {t.get('id', '?')})",
                noun="tag",
                empty_msg="No tags found.",
            )

    async def add_misp_tag(uuid: str, tag: str, local: bool = False) -> str:
        """Attach a tag to a MISP event OR attribute by its UUID.

        Set local=true for an instance-local tag that is never synced to
        other MISP servers.
        """
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/tags/attachTagToObject",
                _PROVIDER,
                f"attach tag to {uuid}",
                headers=headers,
                json={"uuid": uuid, "tag": tag, "local": 1 if local else 0},
            )
            if err:
                return err
            data = resp.json()
            if data.get("saved") is False or data.get("errors"):
                return f"Error: could not attach tag: {_error_detail(data) or data.get('name', 'unknown error')}"
            scope = " (local)" if local else ""
            return f"Tag '{tag}'{scope} attached to {uuid}."

    async def remove_misp_tag(uuid: str, tag: str) -> str:
        """Remove a tag from a MISP event OR attribute by its UUID."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/tags/removeTagFromObject",
                _PROVIDER,
                f"remove tag from {uuid}",
                headers=headers,
                json={"uuid": uuid, "tag": tag},
            )
            if err:
                return err
            data = resp.json()
            if data.get("saved") is False or data.get("errors"):
                return f"Error: could not remove tag: {_error_detail(data) or data.get('name', 'unknown error')}"
            return f"Tag '{tag}' removed from {uuid}."

    # ══════════════════════════════════════════════════════════════════════
    # 5. SIGHTINGS
    # ══════════════════════════════════════════════════════════════════════

    async def add_misp_sighting(
        value: str = "",
        attribute_id: str = "",
        sighting_type: str = "sighting",
        source: str = "",
    ) -> str:
        """Record a sighting for an IOC (by value, or attribute id/UUID)."""
        stype = _SIGHTING_TYPES.get((sighting_type or "sighting").strip().lower())
        if stype is None:
            return (
                "Error: sighting_type must be one of: sighting, false_positive, "
                "expiration."
            )
        if not value and not attribute_id:
            return "Error: provide either value or attribute_id."
        body: dict[str, Any] = {"type": stype}
        if value:
            body["value"] = value
        if attribute_id:
            body["id"] = attribute_id
        if source:
            body["source"] = source

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/sightings/add",
                _PROVIDER,
                "add sighting",
                headers=headers,
                json=body,
            )
            if err:
                return err
            detail = _error_detail(resp.json())
            if detail:
                return f"Error: MISP rejected the sighting: {detail}"
            label = _SIGHTING_LABELS.get(stype, "Sighting")
            target = value or f"attribute {attribute_id}"
            return f"{label} recorded for {target}."

    async def list_misp_sightings(event_id: str) -> str:
        """List sightings recorded on a MISP event's attributes."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/sightings/index/{event_id}",
                _PROVIDER,
                f"list sightings for event {event_id}",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            sightings: list[dict] = []
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict):
                        sightings.append(entry.get("Sighting", entry))

            def _fmt_sighting(s: dict) -> str:
                label = _SIGHTING_LABELS.get(str(s.get("type", "0")), "Sighting")
                return (
                    f"  {label}: attribute {s.get('attribute_id', '?')} "
                    f"(source: {s.get('source') or 'n/a'}, "
                    f"org {(s.get('Organisation') or {}).get('name', '?')})"
                )

            return format_entity_list(
                sightings,
                _fmt_sighting,
                noun="sighting",
                empty_msg=f"No sightings recorded for event {event_id}.",
            )

    # ══════════════════════════════════════════════════════════════════════
    # 6. OBJECTS
    # ══════════════════════════════════════════════════════════════════════

    async def search_misp_objects(
        object_name: str = "",
        value: str = "",
        event_id: str = "",
        limit: int = 25,
        page: int = 1,
    ) -> str:
        """Search structured MISP objects (file, domain-ip, ...) via restSearch."""
        limit = _clamp(limit)
        page = max(1, int(page or 1))
        body: dict[str, Any] = {"returnFormat": "json", "limit": limit, "page": page}
        if object_name:
            body["object_name"] = object_name
        if value:
            body["value"] = value
        if event_id:
            body["eventid"] = event_id

        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/objects/restsearch",
                _PROVIDER,
                "search objects",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data = resp.json()
            raw = data.get("response", data) if isinstance(data, dict) else data
            objects: list[dict] = []
            if isinstance(raw, list):
                for entry in raw:
                    if isinstance(entry, dict):
                        objects.append(entry.get("Object", entry))

            def _fmt_object(obj: dict) -> str:
                lines = [
                    f"Object {obj.get('id', '?')}: {obj.get('name', '?')} "
                    f"(event {obj.get('event_id', '?')}, "
                    f"uuid {obj.get('uuid', '?')})"
                ]
                for attr in (obj.get("Attribute") or [])[:10]:
                    lines.append("    " + _fmt_attribute(attr, compact=True))
                return "\n".join(lines)

            result = format_entity_list(
                objects,
                _fmt_object,
                noun="object",
                empty_msg="No objects found for the given filters.",
            )
            return result + _page_hint(len(objects), limit, page)

    # ══════════════════════════════════════════════════════════════════════
    # 7. GALAXIES & CLUSTERS
    # ══════════════════════════════════════════════════════════════════════

    async def list_misp_galaxies(search: str = "", limit: int = 50) -> str:
        """List galaxy definitions (threat actors, malware families, ATT&CK)."""
        limit = _clamp(limit, default=50)
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/galaxies",
                _PROVIDER,
                "list galaxies",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            galaxies: list[dict] = []
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict):
                        galaxies.append(entry.get("Galaxy", entry))
            if search:
                needle = search.lower()
                galaxies = [
                    g
                    for g in galaxies
                    if needle in str(g.get("name", "")).lower()
                    or needle in str(g.get("type", "")).lower()
                ]
            galaxies = galaxies[:limit]
            return format_entity_list(
                galaxies,
                lambda g: (
                    f"  Galaxy {g.get('id', '?')}: {g.get('name', '?')} "
                    f"(type: {g.get('type', '?')}) - "
                    f"{_trunc(g.get('description', ''), 100)}"
                ),
                noun="galaxy",
                empty_msg="No galaxies found.",
            )

    async def list_misp_galaxy_clusters(
        galaxy_id: str, search: str = "", limit: int = 50
    ) -> str:
        """List the clusters of a galaxy (e.g. the threat actor entries).

        Call list_misp_galaxies first to find the galaxy_id. Cluster tag names
        can be attached to events/attributes with add_misp_tag.
        """
        limit = _clamp(limit, default=50)
        # The documented "Search galaxy clusters" operation is a POST to the
        # index endpoint with a {"searchall": ...} JSON body - a GET query
        # string is silently ignored by MISP (CakePHP named params only).
        kwargs: dict[str, Any] = {}
        method = "get"
        if search:
            method = "post"
            kwargs["json"] = {"searchall": search}
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{base_url}/galaxy_clusters/index/{galaxy_id}",
                _PROVIDER,
                f"list clusters for galaxy {galaxy_id}",
                headers=headers,
                **kwargs,
            )
            if err:
                return err
            data = resp.json()
            clusters: list[dict] = []
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict):
                        clusters.append(entry.get("GalaxyCluster", entry))
            clusters = clusters[:limit]
            return format_entity_list(
                clusters,
                lambda c: (
                    f"  Cluster {c.get('id', '?')}: {c.get('value', '?')} "
                    f"(tag: {c.get('tag_name', '?')})"
                ),
                noun="cluster",
                empty_msg=f"No clusters found in galaxy {galaxy_id}.",
            )

    # ══════════════════════════════════════════════════════════════════════
    # 8. WARNINGLISTS
    # ══════════════════════════════════════════════════════════════════════

    async def list_misp_warninglists(search: str = "", limit: int = 50) -> str:
        """List warninglists (known-benign indicators) on the instance."""
        limit = _clamp(limit, default=50)
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/warninglists",
                _PROVIDER,
                "list warninglists",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            raw = (
                data.get("Warninglists", data) if isinstance(data, dict) else data
            )
            lists: list[dict] = []
            if isinstance(raw, list):
                for entry in raw:
                    if isinstance(entry, dict):
                        lists.append(entry.get("Warninglist", entry))
            if search:
                needle = search.lower()
                lists = [
                    w for w in lists if needle in str(w.get("name", "")).lower()
                ]
            lists = lists[:limit]
            return format_entity_list(
                lists,
                lambda w: (
                    f"  [{w.get('id', '?')}] {w.get('name', '?')} "
                    f"({'enabled' if w.get('enabled') else 'disabled'}, "
                    f"{w.get('warninglist_entry_count', '?')} entries)"
                ),
                noun="warninglist",
                empty_msg="No warninglists found.",
            )

    async def check_misp_warninglists(value: str) -> str:
        """Check indicator value(s) against enabled warninglists (FP triage).

        A hit means the value is likely benign infrastructure (e.g. a public
        DNS resolver or a known CDN range) and probably a false positive.
        Accepts a single value or a comma-separated list.
        """
        values = [v.strip() for v in (value or "").split(",") if v.strip()]
        if not values:
            return "Error: provide at least one value to check."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/warninglists/checkValue",
                _PROVIDER,
                "check values against warninglists",
                headers=headers,
                json=values,
            )
            if err:
                return err
            data = resp.json()
            if not isinstance(data, dict) or not data:
                return "No warninglist hits - the value(s) look clean."
            lines: list[str] = []
            for val in values:
                hits = data.get(val)
                if isinstance(hits, list) and hits:
                    names = ", ".join(
                        str(h.get("name", "?")) for h in hits if isinstance(h, dict)
                    )
                    lines.append(
                        f"  {val}: HIT ({names}) - likely benign / false positive"
                    )
                else:
                    lines.append(f"  {val}: no hits")
            return "Warninglist check results:\n" + "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 9. FEEDS
    # ══════════════════════════════════════════════════════════════════════

    async def list_misp_feeds() -> str:
        """List the OSINT/threat feeds configured on the MISP instance."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/feeds",
                _PROVIDER,
                "list feeds",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            feeds: list[dict] = []
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict):
                        feeds.append(entry.get("Feed", entry))
            return format_entity_list(
                feeds,
                lambda f: (
                    f"  [{f.get('id', '?')}] {f.get('name', '?')} "
                    f"({'enabled' if f.get('enabled') else 'disabled'}, "
                    f"provider: {f.get('provider', '?')}, "
                    f"format: {f.get('source_format', '?')})"
                ),
                noun="feed",
                empty_msg="No feeds configured on this MISP instance.",
            )

    async def manage_misp_feed(action: str, feed_id: str = "") -> str:
        """Manage MISP feeds: enable, disable, fetch, fetch_all, or cache.

        enable/disable/fetch require feed_id (from list_misp_feeds);
        fetch_all pulls every enabled feed; cache re-caches all feeds.
        Fetch operations are queued as background jobs on the MISP server.
        """
        action = (action or "").strip().lower()
        if action not in _FEED_ACTIONS:
            return "Error: action must be one of: enable, disable, fetch, fetch_all, cache."
        if action in ("enable", "disable", "fetch") and not feed_id:
            return f"Error: feed_id is required for the '{action}' action."

        if action == "fetch_all":
            url = f"{base_url}/feeds/fetchFromAllFeeds"
            action_desc = "fetch all feeds"
        elif action == "cache":
            url = f"{base_url}/feeds/cacheFeeds/all"
            action_desc = "cache feeds"
        elif action == "fetch":
            url = f"{base_url}/feeds/fetchFromFeed/{feed_id}"
            action_desc = f"fetch feed {feed_id}"
        else:
            url = f"{base_url}/feeds/{action}/{feed_id}"
            action_desc = f"{action} feed {feed_id}"

        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                url,
                _PROVIDER,
                action_desc,
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            message = "OK"
            if isinstance(data, dict):
                message = data.get("message") or data.get("name") or "OK"
            return f"Feed action '{action}' executed: {_trunc(message, 200)}"

    # ══════════════════════════════════════════════════════════════════════
    # 10. ORGANISATIONS & USERS
    # ══════════════════════════════════════════════════════════════════════

    async def list_misp_organisations(search: str = "", limit: int = 50) -> str:
        """List organisations known to the MISP instance (sharing context)."""
        limit = _clamp(limit, default=50)
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/organisations",
                _PROVIDER,
                "list organisations",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            orgs: list[dict] = []
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict):
                        orgs.append(entry.get("Organisation", entry))
            if search:
                needle = search.lower()
                orgs = [o for o in orgs if needle in str(o.get("name", "")).lower()]
            orgs = orgs[:limit]
            return format_entity_list(
                orgs,
                lambda o: (
                    f"  [{o.get('id', '?')}] {o.get('name', '?')} "
                    f"({'local' if o.get('local') else 'remote'}, "
                    f"uuid {o.get('uuid', '?')})"
                ),
                noun="organisation",
                empty_msg="No organisations found.",
            )

    async def list_misp_users(limit: int = 50) -> str:
        """List user accounts on the MISP instance (requires an admin key)."""
        limit = _clamp(limit, default=50)
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/admin/users",
                _PROVIDER,
                "list users",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            users: list[dict] = []
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict):
                        user = dict(entry.get("User", {}))
                        user["_org"] = (entry.get("Organisation") or {}).get(
                            "name", "?"
                        )
                        user["_role"] = (entry.get("Role") or {}).get("name", "?")
                        users.append(user)
            users = users[:limit]
            return format_entity_list(
                users,
                lambda u: (
                    f"  [{u.get('id', '?')}] {u.get('email', '?')} "
                    f"(org: {u.get('_org', '?')}, role: {u.get('_role', '?')}"
                    f"{', disabled' if u.get('disabled') else ''})"
                ),
                noun="user",
                empty_msg="No users returned (an admin-level key is required).",
            )

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    # 0. Instance / identity
    executor.register("get_misp_version", get_misp_version)
    executor.register("get_misp_me", get_misp_me)
    # 1. Search
    executor.register("search_misp_attributes", search_misp_attributes)
    executor.register("search_misp_events", search_misp_events)
    # 2. Events
    executor.register("get_misp_event", get_misp_event)
    executor.register("create_misp_event", create_misp_event)
    executor.register("update_misp_event", update_misp_event)
    executor.register("publish_misp_event", publish_misp_event)
    executor.register("delete_misp_event", delete_misp_event)
    # 3. Attributes
    executor.register("get_misp_attribute", get_misp_attribute)
    executor.register("describe_misp_attribute_types", describe_misp_attribute_types)
    executor.register("add_misp_attribute", add_misp_attribute)
    executor.register("update_misp_attribute", update_misp_attribute)
    executor.register("delete_misp_attribute", delete_misp_attribute)
    # 4. Tags
    executor.register("list_misp_tags", list_misp_tags)
    executor.register("add_misp_tag", add_misp_tag)
    executor.register("remove_misp_tag", remove_misp_tag)
    # 5. Sightings
    executor.register("add_misp_sighting", add_misp_sighting)
    executor.register("list_misp_sightings", list_misp_sightings)
    # 6. Objects
    executor.register("search_misp_objects", search_misp_objects)
    # 7. Galaxies
    executor.register("list_misp_galaxies", list_misp_galaxies)
    executor.register("list_misp_galaxy_clusters", list_misp_galaxy_clusters)
    # 8. Warninglists
    executor.register("list_misp_warninglists", list_misp_warninglists)
    executor.register("check_misp_warninglists", check_misp_warninglists)
    # 9. Feeds
    executor.register("list_misp_feeds", list_misp_feeds)
    executor.register("manage_misp_feed", manage_misp_feed)
    # 10. Organisations & users
    executor.register("list_misp_organisations", list_misp_organisations)
    executor.register("list_misp_users", list_misp_users)

    _limit_schema = {
        "type": "integer",
        "description": "Max results to return (max 500)",
        "default": 25,
    }
    _page_schema = {
        "type": "integer",
        "description": "1-based page number for pagination",
        "default": 1,
    }

    return [
        # ── 0. Instance / identity ────────────────────────────────────────
        ToolDefinition(
            name="get_misp_version",
            description=(
                "Get the MISP instance version. Use as a quick connectivity/"
                "health check of the configured MISP integration."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_misp_me",
            description=(
                "Get the profile of the account this MISP integration connects "
                "as (email, organisation, role). Use it to understand which "
                "org's data you create and whether admin tools are available."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        # ── 1. Search ─────────────────────────────────────────────────────
        ToolDefinition(
            name="search_misp_attributes",
            description=(
                "Search MISP attributes (IOCs) with structured filters - the "
                "primary indicator lookup. Pass an indicator in value (IP, "
                "domain, hash, URL, email...) to find every matching attribute "
                "across events, or filter by type/category/tags/event/org/date "
                "range. Prefer the structured parameters over free text. "
                "Set enforce_warninglist=true to drop known-benign hits. "
                "Returns attribute summaries with the event IDs needed for "
                "get_misp_event. Paginate with page."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Indicator value to search (e.g. '8.8.8.8', 'evil.com', a hash)",
                        "default": "",
                    },
                    "type": {
                        "type": "string",
                        "description": "MISP attribute type (e.g. 'ip-dst', 'domain', 'sha256', 'url') - see describe_misp_attribute_types",
                        "default": "",
                    },
                    "category": {
                        "type": "string",
                        "description": "Attribute category (e.g. 'Network activity', 'Payload delivery')",
                        "default": "",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags to filter; prefix with '!' to exclude (e.g. 'tlp:red,!tlp:white')",
                        "default": "",
                    },
                    "event_id": {
                        "type": "string",
                        "description": "Restrict to a single event ID",
                        "default": "",
                    },
                    "org": {
                        "type": "string",
                        "description": "Filter by creator organisation (name or ID)",
                        "default": "",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Event date lower bound (YYYY-MM-DD)",
                        "default": "",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Event date upper bound (YYYY-MM-DD)",
                        "default": "",
                    },
                    "to_ids": {
                        "type": "boolean",
                        "description": "Only return attributes flagged for IDS export",
                        "default": False,
                    },
                    "published_only": {
                        "type": "boolean",
                        "description": "Only return attributes from published events",
                        "default": False,
                    },
                    "enforce_warninglist": {
                        "type": "boolean",
                        "description": "Drop attributes that hit a warninglist (known-benign)",
                        "default": False,
                    },
                    "limit": _limit_schema,
                    "page": _page_schema,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_misp_events",
            description=(
                "Search MISP events with structured filters (keyword across "
                "info/attributes, event info, tags, org, date range, threat "
                "level, published state). keyword is exclusive: MISP ignores "
                "the other filters when it is set, so pass keyword alone or "
                "structured filters alone. Returns event metadata only - use "
                "get_misp_event for the full attribute list of one event. "
                "Paginate with page."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Full-text keyword searched across event info, tags, and attribute values/comments",
                        "default": "",
                    },
                    "event_info": {
                        "type": "string",
                        "description": "Filter on the event info (title) field only",
                        "default": "",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags; prefix with '!' to exclude (e.g. 'tlp:amber,!tlp:red')",
                        "default": "",
                    },
                    "org": {
                        "type": "string",
                        "description": "Filter by creator organisation (name or ID)",
                        "default": "",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Event date lower bound (YYYY-MM-DD)",
                        "default": "",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Event date upper bound (YYYY-MM-DD)",
                        "default": "",
                    },
                    "threat_level": {
                        "type": "string",
                        "description": "Filter by threat level",
                        "enum": ["", "high", "medium", "low", "undefined"],
                        "default": "",
                    },
                    "published_only": {
                        "type": "boolean",
                        "description": "Only return published events",
                        "default": False,
                    },
                    "limit": _limit_schema,
                    "page": _page_schema,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 2. Events ─────────────────────────────────────────────────────
        ToolDefinition(
            name="get_misp_event",
            description=(
                "Get the full details of a MISP event by ID or UUID: metadata, "
                "tags, attributes (IOCs), and objects, plus a deep link to the "
                "event page. Find event IDs with search_misp_events or "
                "search_misp_attributes first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "Event ID (numeric) or UUID",
                    },
                },
                "required": ["event_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_misp_event",
            description=(
                "Create a new MISP event (an incident/report container). The "
                "event starts unpublished; add IOCs with add_misp_attribute, "
                "tags with add_misp_tag, then distribute with "
                "publish_misp_event. Distribution: 0=your org only, 1=this "
                "community, 2=connected communities, 3=all communities."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "info": {
                        "type": "string",
                        "description": "Event title/description (e.g. 'Phishing campaign targeting finance dept')",
                    },
                    "distribution": {
                        "type": "integer",
                        "description": "0=org only, 1=this community, 2=connected communities, 3=all communities",
                        "enum": [0, 1, 2, 3],
                        "default": 0,
                    },
                    "threat_level": {
                        "type": "string",
                        "description": "Threat level of the event",
                        "enum": ["high", "medium", "low", "undefined"],
                        "default": "undefined",
                    },
                    "analysis": {
                        "type": "string",
                        "description": "Analysis maturity of the event",
                        "enum": ["initial", "ongoing", "complete"],
                        "default": "initial",
                    },
                    "date": {
                        "type": "string",
                        "description": "Event date (YYYY-MM-DD, defaults to today)",
                        "default": "",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags to attach on creation (e.g. 'tlp:amber')",
                        "default": "",
                    },
                },
                "required": ["info"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_misp_event",
            description=(
                "Update metadata on an existing MISP event (info, distribution, "
                "threat level, analysis, date). Only provided fields are "
                "changed. Use add_misp_attribute / add_misp_tag for content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "Event ID or UUID",
                    },
                    "info": {
                        "type": "string",
                        "description": "New event title (optional)",
                        "default": "",
                    },
                    "distribution": {
                        "type": "integer",
                        "description": "New distribution (0-3); -1 leaves it unchanged",
                        "default": -1,
                    },
                    "threat_level": {
                        "type": "string",
                        "description": "New threat level (optional)",
                        "enum": ["", "high", "medium", "low", "undefined"],
                        "default": "",
                    },
                    "analysis": {
                        "type": "string",
                        "description": "New analysis state (optional)",
                        "enum": ["", "initial", "ongoing", "complete"],
                        "default": "",
                    },
                    "date": {
                        "type": "string",
                        "description": "New event date (YYYY-MM-DD, optional)",
                        "default": "",
                    },
                },
                "required": ["event_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="publish_misp_event",
            description=(
                "Publish a MISP event to its configured communities and synced "
                "servers, or unpublish it. Publishing distributes the data - "
                "confirm the event content is final before publishing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "Event ID or UUID",
                    },
                    "action": {
                        "type": "string",
                        "description": "Whether to publish or unpublish",
                        "enum": ["publish", "unpublish"],
                        "default": "publish",
                    },
                },
                "required": ["event_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_misp_event",
            description=(
                "Delete a MISP event and all of its attributes permanently. "
                "This action cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "Event ID or UUID to delete",
                    },
                },
                "required": ["event_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 3. Attributes ─────────────────────────────────────────────────
        ToolDefinition(
            name="get_misp_attribute",
            description=(
                "Get the full details of a single MISP attribute (IOC) by its "
                "numeric ID or UUID, including tags and comment."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "attribute_id": {
                        "type": "string",
                        "description": "Attribute ID (numeric) or UUID",
                    },
                },
                "required": ["attribute_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="describe_misp_attribute_types",
            description=(
                "List the valid MISP attribute categories, and the attribute "
                "types allowed within a category. Call this before "
                "add_misp_attribute when unsure which type/category to use "
                "(e.g. 'ip-dst' vs 'ip-src' in 'Network activity')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Category to list types for (e.g. 'Network activity'); empty lists all categories",
                        "default": "",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_misp_attribute",
            description=(
                "Add an attribute (IOC) to a MISP event. Provide the MISP "
                "attribute type (e.g. 'ip-dst', 'domain', 'sha256', 'url', "
                "'email-src') and the value. to_ids=true flags it for IDS "
                "export. Distribution 5 inherits the event's distribution."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "Event ID or UUID to add the attribute to",
                    },
                    "type": {
                        "type": "string",
                        "description": "MISP attribute type (e.g. 'ip-dst', 'domain', 'sha256')",
                    },
                    "value": {
                        "type": "string",
                        "description": "The indicator value",
                    },
                    "category": {
                        "type": "string",
                        "description": "Attribute category (e.g. 'Network activity'); MISP infers a default when omitted",
                        "default": "",
                    },
                    "to_ids": {
                        "type": "boolean",
                        "description": "Flag the attribute for IDS signature export",
                        "default": True,
                    },
                    "comment": {
                        "type": "string",
                        "description": "Analyst comment for context",
                        "default": "",
                    },
                    "distribution": {
                        "type": "integer",
                        "description": "0=org, 1=community, 2=connected, 3=all, 5=inherit event",
                        "enum": [0, 1, 2, 3, 5],
                        "default": 5,
                    },
                },
                "required": ["event_id", "type", "value"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_misp_attribute",
            description=(
                "Update an existing MISP attribute (value, type, category, IDS "
                "flag, comment). Only provided fields are changed. Pass to_ids "
                "as 1 or 0 to change the IDS flag; -1 leaves it unchanged."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "attribute_id": {
                        "type": "string",
                        "description": "Attribute ID or UUID",
                    },
                    "value": {
                        "type": "string",
                        "description": "New indicator value (optional)",
                        "default": "",
                    },
                    "type": {
                        "type": "string",
                        "description": "New attribute type (optional)",
                        "default": "",
                    },
                    "category": {
                        "type": "string",
                        "description": "New category (optional)",
                        "default": "",
                    },
                    "to_ids": {
                        "type": "integer",
                        "description": "1 to enable the IDS flag, 0 to disable, -1 to leave unchanged",
                        "enum": [-1, 0, 1],
                        "default": -1,
                    },
                    "comment": {
                        "type": "string",
                        "description": "New analyst comment (optional)",
                        "default": "",
                    },
                },
                "required": ["attribute_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_misp_attribute",
            description=(
                "Delete a MISP attribute. Soft delete by default (the "
                "attribute is retired but recoverable and propagates as "
                "deleted to synced servers); hard=true removes it permanently "
                "- a hard delete cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "attribute_id": {
                        "type": "string",
                        "description": "Attribute ID or UUID to delete",
                    },
                    "hard": {
                        "type": "boolean",
                        "description": "Permanently remove instead of soft-deleting",
                        "default": False,
                    },
                },
                "required": ["attribute_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 4. Tags ───────────────────────────────────────────────────────
        ToolDefinition(
            name="list_misp_tags",
            description=(
                "List or search the tags available on the MISP instance "
                "(taxonomy tags like tlp:/misp-galaxy:, plus custom tags). Use "
                "it to discover exact tag names before add_misp_tag or before "
                "tag-filtered searches."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Substring to search tag names for (e.g. 'tlp')",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max tags to return (max 500)",
                        "default": 50,
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_misp_tag",
            description=(
                "Attach a tag to a MISP event OR attribute, addressed by its "
                "UUID (both entity types work). Use galaxy cluster tag names "
                "(e.g. 'misp-galaxy:threat-actor=\"APT29\"') to link galaxy "
                "context. local=true keeps the tag on this instance only "
                "(never synced)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "uuid": {
                        "type": "string",
                        "description": "UUID of the event or attribute to tag",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Tag name (e.g. 'tlp:amber'); created if it does not exist",
                    },
                    "local": {
                        "type": "boolean",
                        "description": "Attach as a local (non-synced) tag",
                        "default": False,
                    },
                },
                "required": ["uuid", "tag"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="remove_misp_tag",
            description=(
                "Remove a tag from a MISP event OR attribute, addressed by its "
                "UUID."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "uuid": {
                        "type": "string",
                        "description": "UUID of the event or attribute",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Tag name to remove (e.g. 'tlp:amber')",
                    },
                },
                "required": ["uuid", "tag"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 5. Sightings ──────────────────────────────────────────────────
        ToolDefinition(
            name="add_misp_sighting",
            description=(
                "Record a sighting on a MISP attribute - the detection "
                "feedback loop. Report that an IOC was actually observed "
                "(sighting), is a false positive, or has expired. Target by "
                "indicator value (matches every attribute with that value) or "
                "by a specific attribute id/UUID."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Indicator value that was sighted (matches all attributes with this value)",
                        "default": "",
                    },
                    "attribute_id": {
                        "type": "string",
                        "description": "Specific attribute ID or UUID (alternative to value)",
                        "default": "",
                    },
                    "sighting_type": {
                        "type": "string",
                        "description": "Kind of sighting to record",
                        "enum": ["sighting", "false_positive", "expiration"],
                        "default": "sighting",
                    },
                    "source": {
                        "type": "string",
                        "description": "Where the sighting came from (e.g. 'SIEM', 'EDR')",
                        "default": "",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_misp_sightings",
            description=(
                "List the sightings recorded on a MISP event's attributes - "
                "how often its IOCs were observed, marked false positive, or "
                "expired, and by which organisation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "Event ID to list sightings for",
                    },
                },
                "required": ["event_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 6. Objects ────────────────────────────────────────────────────
        ToolDefinition(
            name="search_misp_objects",
            description=(
                "Search structured MISP objects (grouped attributes following "
                "an object template, e.g. 'file' with filename+hashes, "
                "'domain-ip' pairs). Filter by object template name, a "
                "contained value, or event. Paginate with page."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_name": {
                        "type": "string",
                        "description": "Object template name (e.g. 'file', 'domain-ip', 'url')",
                        "default": "",
                    },
                    "value": {
                        "type": "string",
                        "description": "Value contained in any of the object's attributes",
                        "default": "",
                    },
                    "event_id": {
                        "type": "string",
                        "description": "Restrict to a single event ID",
                        "default": "",
                    },
                    "limit": _limit_schema,
                    "page": _page_schema,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 7. Galaxies ───────────────────────────────────────────────────
        ToolDefinition(
            name="list_misp_galaxies",
            description=(
                "List MISP galaxy definitions - the contextual knowledge "
                "libraries (threat actors, malware families, ransomware, "
                "MITRE ATT&CK techniques...). Returns galaxy IDs for "
                "list_misp_galaxy_clusters."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Filter galaxies by name/type substring (e.g. 'threat actor', 'attack')",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max galaxies to return (max 500)",
                        "default": 50,
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_misp_galaxy_clusters",
            description=(
                "List the clusters inside a MISP galaxy (e.g. each threat "
                "actor in the threat-actor galaxy). Returns the cluster tag "
                "names that can be attached to events/attributes with "
                "add_misp_tag to link the context."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "galaxy_id": {
                        "type": "string",
                        "description": "Galaxy ID (from list_misp_galaxies)",
                    },
                    "search": {
                        "type": "string",
                        "description": "Search clusters by name (e.g. 'APT29')",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max clusters to return (max 500)",
                        "default": 50,
                    },
                },
                "required": ["galaxy_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 8. Warninglists ───────────────────────────────────────────────
        ToolDefinition(
            name="list_misp_warninglists",
            description=(
                "List the warninglists on the MISP instance - curated lists "
                "of known-benign indicators (public DNS resolvers, CDN "
                "ranges, ...) used to catch false positives."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Filter warninglists by name substring",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max warninglists to return (max 500)",
                        "default": 50,
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="check_misp_warninglists",
            description=(
                "Check one or more indicator values (comma-separated) against "
                "the enabled warninglists. A hit means the value is likely "
                "benign infrastructure and probably a false positive - do "
                "this before adding IOCs or acting on a detection."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Value(s) to check, comma-separated (e.g. '8.8.8.8,1.1.1.1')",
                    },
                },
                "required": ["value"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 9. Feeds ──────────────────────────────────────────────────────
        ToolDefinition(
            name="list_misp_feeds",
            description=(
                "List the OSINT/threat feeds configured on the MISP instance "
                "with their enabled state, provider, and format. Returns the "
                "feed IDs used by manage_misp_feed."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_misp_feed",
            description=(
                "Manage MISP feeds: enable or disable a feed, fetch one feed "
                "(fetch), fetch every enabled feed (fetch_all), or re-cache "
                "all feeds (cache). Fetches run as background jobs on the "
                "MISP server and may take a while to complete."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Feed operation to perform",
                        "enum": ["enable", "disable", "fetch", "fetch_all", "cache"],
                    },
                    "feed_id": {
                        "type": "string",
                        "description": "Feed ID (required for enable/disable/fetch)",
                        "default": "",
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 10. Organisations & users ─────────────────────────────────────
        ToolDefinition(
            name="list_misp_organisations",
            description=(
                "List the organisations known to the MISP instance (local and "
                "remote) - useful for understanding sharing scope and for the "
                "org filters of the search tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Filter organisations by name substring",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max organisations to return (max 500)",
                        "default": 50,
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_misp_users",
            description=(
                "List the user accounts on the MISP instance with their org "
                "and role (read-only; requires the integration key to belong "
                "to an admin user)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max users to return (max 500)",
                        "default": 50,
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
