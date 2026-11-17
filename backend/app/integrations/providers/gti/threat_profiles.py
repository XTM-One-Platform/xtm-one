"""Google TI Threat Profiles tools - personalised threat tracking.

A Threat Profile encodes an organisation's interests (targeted industries,
targeted/source regions, malware roles, actor motivations) and receives
ML-generated recommendations of relevant Threat Landscape objects, plus
user-added and connector-observed threats.

* ``list_gti_threat_profiles`` - ``GET /threat_profiles`` (with the caller's
  role: owner / editor / viewer).
* ``get_gti_threat_profile`` - ``GET /threat_profiles/{id}``.
* ``get_gti_threat_profile_recommendations`` -
  ``GET /threat_profiles/{id}/recommendations`` with the documented filters
  (recommendation_source, collection_type, regions, industries, ...).
* ``manage_gti_threat_profile`` - consolidated write tool: create
  (``POST /threat_profiles``), update (``PATCH /threat_profiles/{id}`` -
  interests are REPLACED wholesale), delete
  (``DELETE /threat_profiles/{id}``), and add/remove followed objects
  (``POST/DELETE /threat_profiles/{id}/relationships/recommendations``).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from app.integrations.http import format_pagination_hint, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    attributes_of,
    clamp,
    client,
    context_of,
    csv_list,
    cursor_of,
    data_of,
    format_threat_summary,
    trunc,
    ts,
)

_PROFILE_ACTIONS = frozenset(
    {"create", "update", "delete", "add_objects", "remove_objects"}
)
_INTEREST_KEYS = frozenset(
    {
        "INTEREST_TYPE_TARGETED_INDUSTRY",
        "INTEREST_TYPE_TARGETED_REGION",
        "INTEREST_TYPE_SOURCE_REGION",
        "INTEREST_TYPE_MALWARE_ROLE",
        "INTEREST_TYPE_ACTOR_MOTIVATION",
    }
)


def _format_profile(obj: dict[str, Any]) -> str:
    attrs = attributes_of(obj)
    ctx = context_of(obj)
    role = trunc(ctx.get("role"), 20)
    bits = []
    if role:
        bits.append(role)
    modified = ts(attrs.get("last_modification_date"))
    if modified:
        bits.append(f"updated {modified}")
    suffix = f" [{', '.join(bits)}]" if bits else ""
    return (
        f"  {trunc(attrs.get('name'), 100) or '?'}{suffix} "
        f"(id: {trunc(obj.get('id'), 80) or '?'})"
    )


def _parse_interests(interests: str) -> tuple[dict[str, list[str]] | None, str | None]:
    """Parse the interests JSON argument; returns (parsed, error)."""
    raw = (interests or "").strip()
    if not raw:
        return None, None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None, (
            "Error: interests must be a JSON object mapping interest types to "
            'string lists, e.g. {"INTEREST_TYPE_TARGETED_REGION": ["FR"], '
            '"INTEREST_TYPE_ACTOR_MOTIVATION": ["Espionage"]}.'
        )
    if not isinstance(parsed, dict):
        return None, "Error: interests must be a JSON object."
    unknown = sorted(set(parsed) - _INTEREST_KEYS)
    if unknown:
        return None, (
            f"Error: unknown interest type(s) {', '.join(trunc(u, 60) for u in unknown)}. "
            f"Allowed: {', '.join(sorted(_INTEREST_KEYS))}."
        )
    cleaned: dict[str, list[str]] = {}
    for key, values in parsed.items():
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            return None, f"Error: interests['{key}'] must be a list of strings."
        cleaned[key] = values
    return cleaned, None


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register Threat Profile tools and return their definitions."""

    async def list_gti_threat_profiles(limit: int = 10, cursor: str = "") -> str:
        """List the Threat Profiles visible to this account (with your role)."""
        params: dict[str, Any] = {"limit": clamp(limit)}
        if cursor.strip():
            params["cursor"] = cursor.strip()
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/threat_profiles",
                _PROVIDER,
                "list threat profiles",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = data_of(payload)
        items = items if isinstance(items, list) else []
        if not items:
            return (
                "No Threat Profiles found for this account. Create one with "
                "manage_gti_threat_profile(action='create')."
            )
        lines = [f"Threat Profiles ({len(items)} shown):"]
        lines.extend(_format_profile(o) for o in items if isinstance(o, dict))
        return "\n".join(lines) + format_pagination_hint(cursor_of(payload), "cursor")

    async def get_gti_threat_profile(profile_id: str) -> str:
        """Read one Threat Profile: name, interests, recommendation config, role."""
        profile_id = (profile_id or "").strip()
        if not profile_id:
            return "Error: profile_id is required (from list_gti_threat_profiles)."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/threat_profiles/{quote(profile_id, safe='')}",
                _PROVIDER,
                f"get threat profile {trunc(profile_id, 60)}",
                headers=headers,
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    return f"Threat Profile '{trunc(profile_id, 80)}' not found."
                return err
            payload = resp.json() or {}
        obj = data_of(payload)
        if not isinstance(obj, dict):
            return f"Threat Profile '{trunc(profile_id, 80)}' not found."
        attrs = attributes_of(obj)
        ctx = context_of(obj)
        safe_id = trunc(obj.get("id"), 80) or trunc(profile_id, 80)
        lines = [
            f"Threat Profile: {trunc(attrs.get('name'), 120) or '?'} (id: {safe_id})"
        ]
        role = ctx.get("role")
        if role:
            lines.append(f"Your role: {trunc(role, 20)}")
        interests = attrs.get("interests")
        if isinstance(interests, dict):
            for key in sorted(interests):
                values = interests.get(key)
                if isinstance(values, list) and values:
                    label = key.replace("INTEREST_TYPE_", "").replace("_", " ").title()
                    lines.append(
                        f"{label}: "
                        f"{trunc(', '.join(str(v) for v in values[:12]), 300)}"
                    )
        config = attrs.get("recommendation_config")
        if isinstance(config, dict) and config:
            lines.append(
                "Recommendation config: "
                + trunc(
                    ", ".join(f"{k}={v}" for k, v in config.items()),
                    200,
                )
            )
        created = ts(attrs.get("creation_date"))
        if created:
            lines.append(f"Created: {created} UTC")
        modified = ts(attrs.get("last_modification_date"))
        if modified:
            lines.append(f"Last modified: {modified} UTC")
        lines.append(
            "List its threats with get_gti_threat_profile_recommendations"
            f"(profile_id='{safe_id}')."
        )
        return "\n".join(lines)

    async def get_gti_threat_profile_recommendations(
        profile_id: str,
        filter: str = "",
        limit: int = 10,
        cursor: str = "",
    ) -> str:
        """List the threat objects associated with a Threat Profile (recommended / added / observed)."""
        profile_id = (profile_id or "").strip()
        if not profile_id:
            return "Error: profile_id is required."
        params: dict[str, Any] = {"limit": clamp(limit)}
        if filter.strip():
            params["filter"] = filter.strip()
        if cursor.strip():
            params["cursor"] = cursor.strip()
        url = f"{api}/threat_profiles/{quote(profile_id, safe='')}/recommendations"
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                url,
                _PROVIDER,
                f"get recommendations of threat profile {trunc(profile_id, 60)}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = data_of(payload)
        items = items if isinstance(items, list) else []
        if not items:
            return (
                f"Threat Profile '{trunc(profile_id, 80)}' has no associated "
                f"threat objects matching '{trunc(filter, 100) or 'all'}'."
            )
        lines = [
            f"Threat objects in profile {trunc(profile_id, 80)} ({len(items)} shown):"
        ]
        for obj in items:
            if not isinstance(obj, dict):
                continue
            line = format_threat_summary(obj)
            ctx = context_of(obj)
            source = ctx.get("source")
            if source:
                label = {
                    "SOURCE_RECOMMENDATION": "recommended",
                    "SOURCE_DIRECT_FOLLOW": "added by user",
                    "SOURCE_OBSERVED_THREAT": "observed in environment",
                }.get(str(source), trunc(source, 40))
                line += f" <- {label}"
            lines.append(line)
        return "\n".join(lines) + format_pagination_hint(cursor_of(payload), "cursor")

    async def manage_gti_threat_profile(
        action: str,
        profile_id: str = "",
        name: str = "",
        interests: str = "",
        object_ids: str = "",
    ) -> str:
        """Create, update, delete a Threat Profile, or add/remove followed threat objects."""
        action = (action or "").strip().lower()
        if action not in _PROFILE_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_PROFILE_ACTIONS))}."
        profile_id = (profile_id or "").strip()
        if action != "create" and not profile_id:
            return f"Error: profile_id is required for action '{action}'."
        async with client() as c:
            if action == "create":
                if not name.strip():
                    return "Error: name is required to create a Threat Profile."
                parsed, parse_err = _parse_interests(interests)
                if parse_err:
                    return parse_err
                attributes: dict[str, Any] = {"name": name.strip()}
                if parsed:
                    attributes["interests"] = parsed
                resp, err = await safe_request(
                    c,
                    "post",
                    f"{api}/threat_profiles",
                    _PROVIDER,
                    f"create threat profile '{trunc(name, 80)}'",
                    headers=headers,
                    json={"data": {"type": "threat_profile", "attributes": attributes}},
                )
                if err:
                    return err
                payload = resp.json() or {}
                obj = data_of(payload)
                new_id = (trunc(obj.get("id"), 80) if isinstance(obj, dict) else "") or "?"
                return f"Threat Profile '{trunc(name, 100)}' created (id: {new_id})."
            base_url = f"{api}/threat_profiles/{quote(profile_id, safe='')}"
            if action == "update":
                parsed, parse_err = _parse_interests(interests)
                if parse_err:
                    return parse_err
                attributes = {}
                if name.strip():
                    attributes["name"] = name.strip()
                if parsed is not None:
                    attributes["interests"] = parsed
                if not attributes:
                    return "Error: update requires name and/or interests."
                _resp, err = await safe_request(
                    c,
                    "patch",
                    base_url,
                    _PROVIDER,
                    f"update threat profile {trunc(profile_id, 60)}",
                    headers=headers,
                    json={
                        "data": {"type": "threat_profile", "attributes": attributes}
                    },
                )
                if err:
                    return err
                note = (
                    " Note: interests were replaced wholesale (the API does "
                    "not merge them)."
                    if parsed is not None
                    else ""
                )
                return f"Threat Profile '{trunc(profile_id, 80)}' updated.{note}"
            if action == "delete":
                _resp, err = await safe_request(
                    c,
                    "delete",
                    base_url,
                    _PROVIDER,
                    f"delete threat profile {trunc(profile_id, 60)}",
                    headers=headers,
                )
                if err:
                    return err
                return (
                    f"Threat Profile '{trunc(profile_id, 80)}' permanently "
                    "deleted."
                )
            # add_objects / remove_objects
            values = csv_list(object_ids)
            if not values:
                return (
                    "Error: object_ids (comma-separated threat object ids "
                    "from search_gti_threats) is required."
                )
            descriptors = [{"type": "collection", "id": v} for v in values]
            verb = "post" if action == "add_objects" else "delete"
            _resp, err = await safe_request(
                c,
                verb,
                f"{base_url}/relationships/recommendations",
                _PROVIDER,
                f"{action} on threat profile {trunc(profile_id, 60)}",
                headers=headers,
                json={"data": descriptors},
            )
            if err:
                return err
        did = "added to" if action == "add_objects" else "removed from"
        return (
            f"{len(values)} threat object(s) {did} profile "
            f"'{trunc(profile_id, 80)}'."
        )

    executor.register("list_gti_threat_profiles", list_gti_threat_profiles)
    executor.register("get_gti_threat_profile", get_gti_threat_profile)
    executor.register(
        "get_gti_threat_profile_recommendations", get_gti_threat_profile_recommendations
    )
    executor.register("manage_gti_threat_profile", manage_gti_threat_profile)

    return [
        ToolDefinition(
            name="list_gti_threat_profiles",
            description=(
                "List the Google TI Threat Profiles visible to this account - "
                "personalised threat-tracking profiles with ML "
                "recommendations. Shows each profile's name, your role "
                "(owner / editor / viewer), and id for "
                "get_gti_threat_profile."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max profiles to return (1-40)",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Pagination cursor from a previous call",
                    },
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_gti_threat_profile",
            description=(
                "Read one Threat Profile: name, configured interests "
                "(targeted industries and regions, source regions, malware "
                "roles, actor motivations), recommendation config, your "
                "role, and dates. Use "
                "get_gti_threat_profile_recommendations to list its threat "
                "objects."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "profile_id": {
                        "type": "string",
                        "description": "Threat Profile id from list_gti_threat_profiles",
                    },
                },
                "required": ["profile_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_gti_threat_profile_recommendations",
            description=(
                "List the threat objects associated with a Threat Profile - "
                "ML recommendations, user-added threats, and threats "
                "observed in your environment via connectors. Filter with "
                "e.g. 'recommendation_source:SOURCE_RECOMMENDATION', "
                "'collection_type:threat-actor', 'targeted_region:FR', "
                "'motivation:Espionage', 'last_modification_date:30d+' "
                "(combinable with AND/OR). Returns threat ids for "
                "get_gti_threat."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "profile_id": {
                        "type": "string",
                        "description": "Threat Profile id from list_gti_threat_profiles",
                    },
                    "filter": {
                        "type": "string",
                        "description": (
                            "Filter string - recommendation_source:, "
                            "collection_type:, targeted_region:, source_region:, "
                            "targeted_industry:, motivation:, origin:, dates"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max objects to return (1-40)",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Pagination cursor from a previous call",
                    },
                },
                "required": ["profile_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_gti_threat_profile",
            description=(
                "Manage Threat Profiles: 'create' (name + optional interests "
                "JSON), 'update' (rename and/or REPLACE the interests - the "
                "API replaces interests wholesale, so include every interest "
                "you want to keep), 'add_objects' / 'remove_objects' to "
                "follow or unfollow threat objects (comma-separated ids from "
                "search_gti_threats), or 'delete' the profile permanently "
                "(cannot be undone). Interests JSON keys: "
                "INTEREST_TYPE_TARGETED_INDUSTRY, "
                "INTEREST_TYPE_TARGETED_REGION (ISO-3166 codes), "
                "INTEREST_TYPE_SOURCE_REGION, INTEREST_TYPE_MALWARE_ROLE, "
                "INTEREST_TYPE_ACTOR_MOTIVATION."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_PROFILE_ACTIONS),
                    },
                    "profile_id": {
                        "type": "string",
                        "description": "Threat Profile id (all actions except create)",
                    },
                    "name": {
                        "type": "string",
                        "description": "Profile name (create) or new name (update)",
                    },
                    "interests": {
                        "type": "string",
                        "description": (
                            'JSON object of interests, e.g. '
                            '{"INTEREST_TYPE_TARGETED_REGION": ["FR"], '
                            '"INTEREST_TYPE_ACTOR_MOTIVATION": ["Espionage"]}'
                        ),
                    },
                    "object_ids": {
                        "type": "string",
                        "description": (
                            "Comma-separated threat object ids "
                            "(add_objects / remove_objects)"
                        ),
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
