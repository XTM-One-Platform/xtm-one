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

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _api, _search_body, _validate_filter_group, json_answer

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"


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
    ) -> str:
        action = (action or "").strip().lower()
        if not asset_id:
            return "asset_id (endpoint_id) is required."

        if action == "update":
            body: dict = {}
            if name:
                body["asset_name"] = name
            if description:
                body["asset_description"] = description
            if tags is not None:
                body["asset_tags"] = tags
            if hostname:
                body["asset_hostname"] = hostname
            if ips is not None:
                body["asset_ips"] = ips
            if platform:
                body["endpoint_platform"] = platform
            if arch:
                body["endpoint_arch"] = arch
            if not body:
                return "Provide at least one field to update."
            _, err = await _api(
                base_url, headers, "put", f"/api/endpoints/{asset_id}",
                "update endpoint", json=body,
            )
            if err:
                return err
            return f"Updated endpoint {asset_id}."

        if action == "delete":
            _, err = await _api(
                base_url, headers, "delete", f"/api/endpoints/{asset_id}",
                "delete endpoint",
            )
            if err:
                return err
            return f"Deleted endpoint {asset_id}."

        return (
            f"Unknown action '{action}'. Valid actions: update, delete. "
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
                "(asset_id + any of name, description, tags, hostname, ips, platform "
                "[Windows/Linux/MacOS], arch [x86_64/arm64]) and 'delete' (asset_id). "
                "Endpoints are normally created by deploying an agent; use "
                "search_openaev_assets / get_openaev_asset to read."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["update", "delete"]},
                    "asset_id": {"type": "string", "description": "Endpoint ID."},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "hostname": {"type": "string"},
                    "ips": {"type": "array", "items": {"type": "string"}},
                    "platform": {"type": "string", "description": "Windows/Linux/MacOS."},
                    "arch": {"type": "string", "description": "x86_64/arm64."},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["action", "asset_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_asset_group",
            description=(
                "Manage OpenAEV asset groups. Actions: 'list' (search), 'get' "
                "(asset_group_id), 'create' (name, optional description/tags/"
                "dynamic_filter JSON), 'update' (asset_group_id + fields), "
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
