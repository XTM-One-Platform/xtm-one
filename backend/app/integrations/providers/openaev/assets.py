# SPDX-License-Identifier: Apache-2.0

"""OpenAEV asset management tools (writes).

* ``manage_openaev_asset``            - endpoint update/delete (read via the
  core search_openaev_assets / get_openaev_asset tools).
* ``manage_openaev_asset_group``      - asset group CRUD + static membership.
* ``manage_openaev_security_platform``- security platform CRUD/upsert.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _api, _relation_ids, _search_body, _validate_filter_group, json_answer

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"

#: The fields of OpenAEV's EndpointInput, as an endpoint read answers them.
#: PUT /api/endpoints/{id} copies every one of them onto the endpoint, so an
#: update sends them all back (read, merge, write) or clears the ones left out.
_ENDPOINT_INPUT_FIELDS = (
    "asset_name",
    "asset_description",
    "asset_external_reference",
    "asset_tags",
    "endpoint_platform",
    "endpoint_arch",
    "asset_ips",
    "asset_hostname",
    "asset_url",
    "asset_mac_addresses",
    "endpoint_is_eol",
    "asset_category",
    "asset_subcategory",
    "asset_criticality",
    "asset_internet_facing",
    "asset_cloud_provider",
    "asset_cloud_native_type",
    "asset_cloud_region",
    "asset_linked_person",
    "asset_metadata",
)


#: Categories newer than some OpenAEV releases (xtm-one #1233): an update that
#: sets one of them and is refused for it is sent again as GENERIC_ASSET with no
#: subcategory.
_NEWER_CATEGORIES = frozenset({"SOURCE_CODE"})

#: The wording ``format_http_error`` gives a 400 and a 422.
_REJECTION_MARKERS = ("bad request", "validation error")


#: The update parameters an explicit ``clear`` unsets, with the value that
#: unsets each one in the endpoint body (the name, platform and arch are
#: required by OpenAEV and the category always has a value: never cleared).
_CLEARABLE: dict[str, tuple[str, Any]] = {
    "description": ("asset_description", None),
    "hostname": ("asset_hostname", None),
    "ips": ("asset_ips", []),
    "tags": ("asset_tags", []),
    "url": ("asset_url", None),
    "subcategory": ("asset_subcategory", None),
    "criticality": ("asset_criticality", None),
    "internet_facing": ("asset_internet_facing", None),
    "external_reference": ("asset_external_reference", None),
}


def _cleared(clear: list[str] | None) -> tuple[dict[str, Any], str]:
    """The endpoint body values that unset the fields named in *clear*.

    Returns ``(changes, refusal)``; an unknown name is refused before any call.
    """
    changes: dict[str, Any] = {}
    unknown: list[str] = []
    for raw in clear or []:
        name = str(raw or "").strip().lower()
        if not name:
            continue
        if name not in _CLEARABLE:
            unknown.append(name)
            continue
        key, value = _CLEARABLE[name]
        changes[key] = value
    if unknown:
        return {}, (
            f"clear names unknown field(s): {', '.join(unknown)}. Clearable: "
            f"{', '.join(_CLEARABLE)}. Nothing was written."
        )
    return changes, ""


def _asset_group_input(current: dict[str, Any]) -> dict[str, Any]:
    """The AssetGroupInput that keeps *current* (an asset group read) as it is."""
    body: dict[str, Any] = {}
    for key in ("asset_group_name", "asset_group_description"):
        if key in current:
            body[key] = current[key]
    if "asset_group_tags" in current:
        body["asset_group_tags"] = _relation_ids(current.get("asset_group_tags"), "tag_id")
    if isinstance(current.get("asset_group_dynamic_filter"), dict):
        body["asset_group_dynamic_filter"] = current["asset_group_dynamic_filter"]
    return body


def _category_refused(err: str | None, category: str) -> bool:
    """Whether *err* is OpenAEV rejecting *category* itself.

    Only a 400 or 422 whose detail names the category field (``asset_category``
    or the ``AssetCategory`` enum) or the value counts: a 5xx, a timeout or a
    rejection of another field (the subcategory included) is answered as it is,
    never retried with a downgraded category.
    """
    if not err:
        return False
    low = err.lower()
    if not any(marker in low for marker in _REJECTION_MARKERS):
        return False
    return any(word in low for word in ("asset_category", "assetcategory", category.lower()))


def _endpoint_input(current: dict[str, Any]) -> dict[str, Any]:
    """The update body that keeps *current* (an endpoint read) as it is."""
    body = {k: current[k] for k in _ENDPOINT_INPUT_FIELDS if k in current}
    tags = body.get("asset_tags")
    if isinstance(tags, list):
        # Tags read back as ids, or as objects on some builds.
        body["asset_tags"] = [
            t.get("tag_id") if isinstance(t, dict) else t
            for t in tags
            if (t.get("tag_id") if isinstance(t, dict) else t)
        ]
    return body


async def _bulk_update(
    base_url: str,
    headers: dict[str, str],
    *,
    asset_ids: list[str] | None,
    filter_json: str,
    category: str,
    subcategory: str,
    url: str,
    criticality: str,
    tags: list[str] | None,
    clear: list[str] | None = None,
    internet_facing: bool | None = None,
) -> str:
    """One ``PUT /api/assets/bulk`` (OpenAEV #9654) over ids or a filter group.

    ``internet_facing`` (true or false; None leaves it unchanged) needs OpenAEV
    #9936: an older OpenAEV refuses it alone with "Nothing to update" or drops
    it beside other fields, and both are answered, never claimed as applied.
    """
    ids = [i for i in (asset_ids or []) if i]
    if bool(ids) == bool(filter_json):
        return "bulk_update needs exactly one selection: asset_ids or asset_filter."
    body: dict[str, Any] = {}
    if ids:
        body["asset_ids_to_process"] = ids
    else:
        try:
            parsed = json.loads(filter_json)
        except ValueError as exc:
            return f"asset_filter must be valid JSON: {exc}"
        filter_err = _validate_filter_group(parsed)
        if filter_err:
            return f"asset_filter is invalid. {filter_err}"
        body["search_pagination_input"] = {"filterGroup": parsed}
    changes: dict[str, Any] = {}
    if category:
        changes["asset_category"] = category.strip().upper()
    if subcategory:
        changes["asset_subcategory"] = subcategory.strip().upper()
    if url:
        changes["asset_url"] = url
    clear_names = {str(c or "").strip().lower() for c in clear or []} - {""}
    if clear_names - {"url"}:
        return (
            "bulk_update clears the url only (OpenAEV stores a blank url as none); "
            "clear other fields asset by asset with action 'update'. Nothing was written."
        )
    if "url" in clear_names and not url:
        changes["asset_url"] = ""
    if criticality:
        changes["asset_criticality"] = criticality.strip().upper()
    if tags:
        changes["asset_tags_to_add"] = tags
    if internet_facing is not None:
        changes["asset_internet_facing"] = internet_facing
    if not changes:
        return (
            "Provide at least one of category, subcategory, url, criticality, tags, internet_facing or clear ['url']."
        )
    if "asset_subcategory" in changes and "asset_category" not in changes:
        return "A subcategory needs its category: pass category too."
    body.update(changes)
    resp, err = await _api(base_url, headers, "put", "/api/assets/bulk", "bulk update assets", json=body)
    if err:
        if "404" in err or "405" in err or "not found" in err.lower():
            return (
                f"{err}\nThis OpenAEV has no bulk asset update (it needs OpenAEV with "
                "PUT /api/assets/bulk): update the assets one by one with action 'update'."
            )
        if internet_facing is not None and "nothing to update" in err.lower():
            return f"{err}\n{_BULK_INTERNET_FACING_UNSUPPORTED}"
        return err
    updated = resp.json() if resp is not None else None
    count = len(updated) if isinstance(updated, list) else 0
    answer = f"Updated {count} asset(s): {', '.join(sorted(changes))}."
    if internet_facing is not None and isinstance(updated, list):
        missed = [
            a
            for a in updated
            if isinstance(a, dict)
            and "asset_internet_facing" in a
            and a["asset_internet_facing"] is not internet_facing
        ]
        if missed:
            answer += f"\ninternet_facing was NOT applied on {len(missed)} of them: {_BULK_INTERNET_FACING_UNSUPPORTED}"
    return answer


_BULK_INTERNET_FACING_UNSUPPORTED = (
    "This OpenAEV does not accept internet_facing in bulk yet (it needs OpenAEV with "
    "#9936): set it asset by asset with action 'update'."
)


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def manage_openaev_asset(
        action: str = "",
        asset_id: str = "",
        name: str = "",
        description: str = "",
        hostname: str = "",
        ips: list[str] | None = None,
        platform: str = "",
        arch: str = "",
        tags: list[str] | None = None,
        url: str = "",
        category: str = "",
        subcategory: str = "",
        criticality: str = "",
        internet_facing: bool | None = None,
        external_reference: str = "",
        asset_ids: list[str] | None = None,
        asset_filter: str = "",
        clear: list[str] | None = None,
    ) -> str:
        action = (action or "").strip().lower()
        if action == "bulk_update":
            return await _bulk_update(
                base_url,
                headers,
                asset_ids=asset_ids,
                filter_json=asset_filter,
                category=category,
                subcategory=subcategory,
                url=url,
                criticality=criticality,
                tags=tags,
                clear=clear,
                internet_facing=internet_facing,
            )
        if not asset_id:
            return "asset_id (endpoint_id) is required."

        if action == "update":
            changes, refusal = _cleared(clear)
            if refusal:
                return refusal
            if name:
                changes["asset_name"] = name
            if description:
                changes["asset_description"] = description
            if tags is not None:
                changes["asset_tags"] = tags
            if hostname:
                changes["asset_hostname"] = hostname
            if ips is not None:
                changes["asset_ips"] = ips
            if platform:
                changes["endpoint_platform"] = platform
            if arch:
                changes["endpoint_arch"] = arch
            if url:
                changes["asset_url"] = url
            if category:
                changes["asset_category"] = category.strip().upper()
            if subcategory:
                changes["asset_subcategory"] = subcategory.strip().upper()
            if criticality:
                changes["asset_criticality"] = criticality.strip().upper()
            if internet_facing is not None:
                changes["asset_internet_facing"] = internet_facing
            if external_reference:
                changes["asset_external_reference"] = external_reference
            if not changes:
                return "Provide at least one field to update, or name the fields to unset in clear."
            # OpenAEV's PUT replaces the whole endpoint (fields left out are
            # cleared, tags replaced): read it, then send it back with only
            # the requested fields changed.
            resp, err = await _api(
                base_url,
                headers,
                "get",
                f"/api/endpoints/{asset_id}",
                "read endpoint",
            )
            if err:
                return err
            current = resp.json() if resp is not None else None
            if not isinstance(current, dict) or not current:
                return f"Endpoint '{asset_id}' not found: nothing updated."
            body = _endpoint_input(current)
            body.update(changes)
            _, err = await _api(
                base_url, headers, "put", f"/api/endpoints/{asset_id}",
                "update endpoint", json=body,
            )
            fallback = ""
            requested = str(changes.get("asset_category") or "")
            if (
                requested in _NEWER_CATEGORIES
                and current.get("asset_category") != requested
                and _category_refused(err, requested)
            ):
                # An OpenAEV that predates the category refuses it: keep the asset
                # unclassified there (XTM One keeps the exact category).
                fallback = requested
                body["asset_category"] = "GENERIC_ASSET"
                body.pop("asset_subcategory", None)
                _, err = await _api(
                    base_url,
                    headers,
                    "put",
                    f"/api/endpoints/{asset_id}",
                    "update endpoint",
                    json=body,
                )
            if err:
                return err
            done = f"Updated endpoint {asset_id}: {', '.join(sorted(changes))}."
            if fallback:
                done += f" This OpenAEV does not know {fallback}: the category was set to GENERIC_ASSET there."
            return done

        if action == "delete":
            _, err = await _api(
                base_url, headers, "delete", f"/api/endpoints/{asset_id}",
                "delete endpoint",
            )
            if err:
                return err
            return f"Deleted endpoint {asset_id}."

        return (
            f"Unknown action '{action}'. Valid actions: update, delete, bulk_update. "
            "(Use search_openaev_assets / get_openaev_asset to read.)"
        )

    async def manage_openaev_asset_group(
        action: str = "",
        asset_group_id: str = "",
        name: str = "",
        description: str = "",
        tags: list[str] | None = None,
        dynamic_filter: str = "",
        asset_ids: list[str] | None = None,
        search: str = "",
        limit: int = 20,
    ) -> str:
        action = (action or "").strip().lower()

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/asset_groups/search",
                "search asset groups",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No asset groups found."
            lines = [f"Asset groups ({len(items)}):"]
            for g in items:
                lines.append(
                    f"- {g.get('asset_group_name', '?')} (id: {g.get('asset_group_id')})"
                )
            return "\n".join(lines)

        if action == "get":
            if not asset_group_id:
                return "asset_group_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/asset_groups/{asset_group_id}",
                "get asset group",
            )
            if err:
                return err
            return json_answer(resp.json(), 2000)

        if action in ("create", "update"):
            if action == "create" and not name:
                return "name is required for action 'create'."
            if action == "update" and not asset_group_id:
                return "asset_group_id is required for action 'update'."
            body: dict = {}
            if name:
                body["asset_group_name"] = name
            if description:
                body["asset_group_description"] = description
            if tags is not None:
                body["asset_group_tags"] = tags
            if dynamic_filter:
                try:
                    parsed_filter = json.loads(dynamic_filter)
                except ValueError as exc:
                    return f"dynamic_filter must be valid JSON: {exc}"
                # OpenAEV silently coerces an unknown operator to `eq`, so a
                # typo here would build a dynamic group that quietly matches
                # the wrong assets. Reject it before it is persisted.
                filter_err = _validate_filter_group(parsed_filter)
                if filter_err:
                    return f"dynamic_filter is invalid. {filter_err}"
                body["asset_group_dynamic_filter"] = parsed_filter
            if action == "update" and not body:
                return (
                    "Provide at least one field to update (name, description, tags, "
                    "or dynamic_filter)."
                )
            if action == "create":
                resp, err = await _api(
                    base_url, headers, "post", "/api/asset_groups",
                    "create asset group", json=body,
                )
            else:
                # OpenAEV's PUT replaces the whole group (AssetGroupInput): a
                # name left out is refused, tags and the dynamic filter left
                # out are reset. Read it and send it back with the changes.
                resp, err = await _api(
                    base_url,
                    headers,
                    "get",
                    f"/api/asset_groups/{asset_group_id}",
                    "read asset group",
                )
                if err:
                    return err
                current = resp.json() if resp is not None else None
                if not isinstance(current, dict) or not current:
                    return f"Asset group '{asset_group_id}' not found: nothing updated."
                body = {**_asset_group_input(current), **body}
                resp, err = await _api(
                    base_url, headers, "put", f"/api/asset_groups/{asset_group_id}",
                    "update asset group", json=body,
                )
            if err:
                return err
            g = resp.json()
            verb = "Created" if action == "create" else "Updated"
            return f"{verb} asset group '{g.get('asset_group_name')}' (id: {g.get('asset_group_id')})."

        if action == "set_assets":
            if not asset_group_id or asset_ids is None:
                return "asset_group_id and asset_ids are required for 'set_assets'."
            _, err = await _api(
                base_url, headers, "put",
                f"/api/asset_groups/{asset_group_id}/assets",
                "set asset group assets", json={"asset_group_assets": asset_ids},
            )
            if err:
                return err
            return f"Set {len(asset_ids)} assets on asset group {asset_group_id}."

        if action == "delete":
            if not asset_group_id:
                return "asset_group_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"/api/asset_groups/{asset_group_id}",
                "delete asset group",
            )
            if err:
                return err
            return f"Deleted asset group {asset_group_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "set_assets, delete."
        )

    async def manage_openaev_security_platform(
        action: str = "",
        security_platform_id: str = "",
        name: str = "",
        description: str = "",
        platform_type: str = "",
        tags: list[str] | None = None,
        logo_light: str = "",
        logo_dark: str = "",
        search: str = "",
        limit: int = 20,
    ) -> str:
        action = (action or "").strip().lower()

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/security_platforms/search",
                "search security platforms",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No security platforms found."
            lines = [f"Security platforms ({len(items)}):"]
            for s in items:
                sid = s.get("security_platform_id") or s.get("asset_id")
                lines.append(
                    f"- {s.get('asset_name', '?')} [{s.get('security_platform_type', '?')}] "
                    f"(security_platform_id: {sid})"
                )
            return "\n".join(lines)

        if action == "get":
            if not security_platform_id:
                return "security_platform_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get",
                f"/api/security_platforms/{security_platform_id}",
                "get security platform",
            )
            if err:
                return err
            return json_answer(resp.json(), 2000)

        if action in ("create", "update", "upsert"):
            if action != "update" and (not name or not platform_type):
                return "name and platform_type are required for 'create'/'upsert'."
            if action == "update" and not security_platform_id:
                return "security_platform_id is required for action 'update'."
            body: dict = {}
            if name:
                body["asset_name"] = name
            if description:
                body["asset_description"] = description
            if tags is not None:
                body["asset_tags"] = tags
            if platform_type:
                body["security_platform_type"] = platform_type
            if logo_light:
                body["security_platform_logo_light"] = logo_light
            if logo_dark:
                body["security_platform_logo_dark"] = logo_dark
            if action == "update" and not body:
                return (
                    "Provide at least one field to update (name, description, tags, "
                    "platform_type, logo_light, or logo_dark)."
                )
            if action == "create":
                resp, err = await _api(
                    base_url, headers, "post", "/api/security_platforms",
                    "create security platform", json=body,
                )
            elif action == "upsert":
                resp, err = await _api(
                    base_url, headers, "post", "/api/security_platforms/upsert",
                    "upsert security platform", json=body,
                )
            else:
                resp, err = await _api(
                    base_url, headers, "put",
                    f"/api/security_platforms/{security_platform_id}",
                    "update security platform", json=body,
                )
            if err:
                return err
            s = resp.json()
            verb = {"create": "Created", "upsert": "Upserted", "update": "Updated"}[action]
            sid = s.get("security_platform_id") or s.get("asset_id")
            return f"{verb} security platform '{s.get('asset_name')}' (security_platform_id: {sid})."

        if action == "delete":
            if not security_platform_id:
                return "security_platform_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete",
                f"/api/security_platforms/{security_platform_id}",
                "delete security platform",
            )
            if err:
                return err
            return f"Deleted security platform {security_platform_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "upsert, delete."
        )

    executor.register("manage_openaev_asset", manage_openaev_asset)
    executor.register("manage_openaev_asset_group", manage_openaev_asset_group)
    executor.register(
        "manage_openaev_security_platform", manage_openaev_security_platform
    )

    return [
        ToolDefinition(
            name="manage_openaev_asset",
            description=(
                "Update or delete an OpenAEV asset (endpoint). Actions: 'update' "
                "(asset_id + any of the fields; the others are kept; clear lists the "
                "fields to unset), 'delete' "
                "(asset_id) and 'bulk_update' (asset_ids or asset_filter; sets category, "
                "subcategory, url, criticality, internet_facing, adds tags on every match). "
                "Endpoints are normally created by deploying an agent; use "
                "search_openaev_assets / get_openaev_asset to read."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["update", "delete", "bulk_update"]},
                    "asset_id": {"type": "string", "description": "Endpoint ID."},
                    "asset_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "bulk_update: the asset ids.",
                    },
                    "asset_filter": {
                        "type": "string",
                        "description": "bulk_update: asset search filter group as JSON.",
                    },
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "hostname": {"type": "string"},
                    "ips": {"type": "array", "items": {"type": "string"}},
                    "platform": {"type": "string", "description": "Windows/Linux/MacOS."},
                    "arch": {"type": "string", "description": "x86_64/arm64."},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tag ids; replaces the asset's tags.",
                    },
                    "url": {"type": "string"},
                    "category": {
                        "type": "string",
                        "description": "HOST, WEB_APPLICATION, CLOUD_RESOURCE...",
                    },
                    "subcategory": {"type": "string", "description": "WEBSITE, SERVER..."},
                    "criticality": {"type": "string", "description": "LOW, MEDIUM, HIGH, VERY_HIGH."},
                    "internet_facing": {
                        "type": "boolean",
                        "description": "update and bulk_update; omit to keep it.",
                    },
                    "external_reference": {"type": "string"},
                    "clear": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(_CLEARABLE)},
                        "description": "Fields to unset (bulk_update: url only).",
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_asset_group",
            description=(
                "Manage OpenAEV asset groups. Actions: 'list' (search), 'get' "
                "(asset_group_id), 'create' (name, optional description/tags/"
                "dynamic_filter JSON), 'update' (asset_group_id + fields; the others are kept), "
                "'set_assets' (asset_group_id + asset_ids for static membership), "
                "'delete' (asset_group_id). dynamic_filter is a FilterGroup JSON "
                "object for dynamic membership."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "create", "update", "set_assets", "delete"],
                    },
                    "asset_group_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "dynamic_filter": {"type": "string", "description": "FilterGroup JSON object for dynamic membership."},
                    "asset_ids": {"type": "array", "items": {"type": "string"}, "description": "Static asset IDs (set_assets)."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_security_platform",
            description=(
                "Manage OpenAEV security platforms (the detection/prevention products "
                "expectations are scored against). Actions: 'list' (search), 'get' "
                "(security_platform_id), 'create' (name + platform_type, optional "
                "description/tags/logo_light/logo_dark), 'update' "
                "(security_platform_id + fields), 'upsert' (name + platform_type), "
                "'delete' (security_platform_id). platform_type examples: EDR, XDR, "
                "SIEM, SOAR, NDR."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "create", "update", "upsert", "delete"],
                    },
                    "security_platform_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "platform_type": {"type": "string", "description": "EDR/XDR/SIEM/SOAR/NDR/..."},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "logo_light": {"type": "string", "description": "Document ID for the light logo."},
                    "logo_dark": {"type": "string", "description": "Document ID for the dark logo."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
