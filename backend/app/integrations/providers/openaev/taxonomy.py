# SPDX-License-Identifier: Apache-2.0

"""OpenAEV taxonomy tools - tags, kill chain phases, attack patterns, variables.

* ``manage_openaev_tag``               - tag CRUD/upsert.
* ``manage_openaev_kill_chain_phase``  - kill chain phase CRUD/upsert.
* ``manage_openaev_attack_pattern``    - MITRE ATT&CK pattern CRUD.
* ``manage_openaev_variable``          - scenario/exercise variables CRUD.
"""

from __future__ import annotations

import logging
import re

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _api, _search_body

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"

#: ``VariableInput.key`` is ``@Pattern("^[a-z_]+$")``: anything else is a bare 400.
_VARIABLE_KEY = re.compile(r"[a-z_]+")
_VARIABLE_KEY_RULE = (
    "key must contain only lowercase letters and underscores (for example "
    "'target_host'): OpenAEV rejects digits, capitals, hyphens and spaces."
)


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def manage_openaev_tag(
        action: str = "",
        tag_id: str = "",
        name: str = "",
        color: str = "",
        search: str = "",
        limit: int = 50,
    ) -> str:
        action = (action or "").strip().lower()
        body = {}
        if name:
            body["tag_name"] = name
        if color:
            body["tag_color"] = color

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/tags/search", "search tags",
                json=_search_body(size=min(limit, 100), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No tags found."
            return f"Tags ({len(items)}):\n" + "\n".join(
                f"- {t.get('tag_name', '?')} (id: {t.get('tag_id')})" for t in items
            )
        if action in ("create", "upsert"):
            if not name or not color:
                return "name and color (hex) are required for 'create'/'upsert'."
            path = "/api/tags/upsert" if action == "upsert" else "/api/tags"
            resp, err = await _api(base_url, headers, "post", path, f"{action} tag", json=body)
            if err:
                return err
            t = resp.json()
            verb = "Upserted" if action == "upsert" else "Created"
            return f"{verb} tag '{t.get('tag_name')}' (id: {t.get('tag_id')})."
        if action == "update":
            if not tag_id:
                return "tag_id is required for action 'update'."
            if not body:
                return "Provide name and/or color to update."
            _, err = await _api(base_url, headers, "put", f"/api/tags/{tag_id}", "update tag", json=body)
            if err:
                return err
            return f"Updated tag {tag_id}."
        if action == "delete":
            if not tag_id:
                return "tag_id is required for action 'delete'."
            _, err = await _api(base_url, headers, "delete", f"/api/tags/{tag_id}", "delete tag")
            if err:
                return err
            return f"Deleted tag {tag_id}."
        return f"Unknown action '{action}'. Valid actions: list, create, update, upsert, delete."

    async def manage_openaev_kill_chain_phase(
        action: str = "",
        phase_id: str = "",
        kill_chain_name: str = "",
        name: str = "",
        shortname: str = "",
        external_id: str = "",
        description: str = "",
        order: int | None = None,
        search: str = "",
        limit: int = 50,
    ) -> str:
        action = (action or "").strip().lower()

        def _body() -> dict:
            b: dict = {}
            if kill_chain_name:
                b["phase_kill_chain_name"] = kill_chain_name
            if name:
                b["phase_name"] = name
            if shortname:
                b["phase_shortname"] = shortname
            if external_id:
                b["phase_external_id"] = external_id
            if description:
                b["phase_description"] = description
            if order is not None:
                b["phase_order"] = order
            return b

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/kill_chain_phases/search",
                "search kill chain phases",
                json=_search_body(size=min(limit, 100), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No kill chain phases found."
            return f"Kill chain phases ({len(items)}):\n" + "\n".join(
                f"- {p.get('phase_name', '?')} ({p.get('phase_kill_chain_name', '?')}) "
                f"(id: {p.get('phase_id')})" for p in items
            )
        if action in ("create", "upsert"):
            if not (kill_chain_name and name and shortname):
                return "kill_chain_name, name and shortname are required for 'create'/'upsert'."
            path = "/api/kill_chain_phases/upsert" if action == "upsert" else "/api/kill_chain_phases"
            resp, err = await _api(base_url, headers, "post", path, f"{action} kill chain phase", json=_body())
            if err:
                return err
            p = resp.json()
            verb = "Upserted" if action == "upsert" else "Created"
            return f"{verb} kill chain phase (id: {p.get('phase_id')})."
        if action == "update":
            if not phase_id:
                return "phase_id is required for action 'update'."
            body = _body()
            if not body:
                return "Provide at least one field to update."
            _, err = await _api(base_url, headers, "put", f"/api/kill_chain_phases/{phase_id}", "update kill chain phase", json=body)
            if err:
                return err
            return f"Updated kill chain phase {phase_id}."
        if action == "delete":
            if not phase_id:
                return "phase_id is required for action 'delete'."
            _, err = await _api(base_url, headers, "delete", f"/api/kill_chain_phases/{phase_id}", "delete kill chain phase")
            if err:
                return err
            return f"Deleted kill chain phase {phase_id}."
        return f"Unknown action '{action}'. Valid actions: list, create, update, upsert, delete."

    async def manage_openaev_attack_pattern(
        action: str = "",
        attack_pattern_id: str = "",
        name: str = "",
        external_id: str = "",
        description: str = "",
        platforms: list[str] | None = None,
        kill_chain_phase_ids: list[str] | None = None,
        parent_id: str = "",
        search: str = "",
        limit: int = 50,
    ) -> str:
        action = (action or "").strip().lower()

        def _body() -> dict:
            b: dict = {}
            if name:
                b["attack_pattern_name"] = name
            if external_id:
                b["attack_pattern_external_id"] = external_id
            if description:
                b["attack_pattern_description"] = description
            if platforms is not None:
                b["attack_pattern_platforms"] = platforms
            if kill_chain_phase_ids is not None:
                b["attack_pattern_kill_chain_phases"] = kill_chain_phase_ids
            if parent_id:
                b["attack_pattern_parent"] = parent_id
            return b

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/attack_patterns/search",
                "search attack patterns",
                json=_search_body(size=min(limit, 100), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No attack patterns found."
            return f"Attack patterns ({len(items)}):\n" + "\n".join(
                f"- {a.get('attack_pattern_external_id', '?')} {a.get('attack_pattern_name', '')} "
                f"(id: {a.get('attack_pattern_id')})" for a in items
            )
        if action in ("create", "update"):
            if action == "create" and (not name or not external_id):
                return "name and external_id (e.g. T1059) are required for 'create'."
            if action == "update" and not attack_pattern_id:
                return "attack_pattern_id is required for action 'update'."
            body = _body()
            if action == "update" and not body:
                return "Provide at least one field to update."
            if action == "create":
                resp, err = await _api(base_url, headers, "post", "/api/attack_patterns", "create attack pattern", json=body)
            else:
                resp, err = await _api(base_url, headers, "put", f"/api/attack_patterns/{attack_pattern_id}", "update attack pattern", json=body)
            if err:
                return err
            a = resp.json()
            verb = "Created" if action == "create" else "Updated"
            return f"{verb} attack pattern '{a.get('attack_pattern_name')}' (id: {a.get('attack_pattern_id')})."
        if action == "delete":
            if not attack_pattern_id:
                return "attack_pattern_id is required for action 'delete'."
            _, err = await _api(base_url, headers, "delete", f"/api/attack_patterns/{attack_pattern_id}", "delete attack pattern")
            if err:
                return err
            return f"Deleted attack pattern {attack_pattern_id}."
        return f"Unknown action '{action}'. Valid actions: list, create, update, delete."

    async def manage_openaev_variable(
        action: str = "",
        scope: str = "scenario",
        parent_id: str = "",
        variable_id: str = "",
        key: str = "",
        value: str = "",
        description: str = "",
    ) -> str:
        action = (action or "").strip().lower()
        scope = (scope or "scenario").strip().lower()
        root = "/api/exercises" if scope in ("exercise", "simulation") else "/api/scenarios"
        if not parent_id:
            return "parent_id (scenario_id or exercise_id) is required."
        base = f"{root}/{parent_id}/variables"

        def _body() -> dict:
            b: dict = {}
            if key:
                b["variable_key"] = key
            if value:
                b["variable_value"] = value
            if description:
                b["variable_description"] = description
            return b

        if action == "list":
            resp, err = await _api(base_url, headers, "get", base, "list variables")
            if err:
                return err
            items = resp.json() or []
            if not items:
                return "No variables found."
            return f"Variables ({len(items)}):\n" + "\n".join(
                f"- {v.get('variable_key', '?')} = {v.get('variable_value', '')} "
                f"(id: {v.get('variable_id')})" for v in items
            )
        if action in ("create", "update") and key and not _VARIABLE_KEY.fullmatch(key):
            return _VARIABLE_KEY_RULE
        if action == "create":
            if not key:
                return "key is required (lowercase letters and underscores only)."
            resp, err = await _api(base_url, headers, "post", base, "create variable", json=_body())
            if err:
                return err
            v = resp.json()
            return f"Created variable '{v.get('variable_key')}' (id: {v.get('variable_id')})."
        if action == "update":
            if not variable_id:
                return "variable_id is required for action 'update'."
            changes = _body()
            if not changes:
                return "Provide at least one field to update (key, value, or description)."
            # The PUT copies the whole VariableInput onto the variable (a missing
            # key is a 400, a missing value or description is wiped), so send the
            # stored fields with the changes laid over them.
            resp, err = await _api(base_url, headers, "get", base, "list variables")
            if err:
                return err
            listed = resp.json()
            current = next(
                (
                    v for v in (listed if isinstance(listed, list) else [])
                    if isinstance(v, dict) and v.get("variable_id") == variable_id
                ),
                None,
            )
            if current is None:
                return (
                    f"Error: variable {variable_id} was not found on this {scope}. "
                    "List the variables to get a valid variable_id."
                )
            body = {
                field: current.get(field)
                for field in ("variable_key", "variable_value", "variable_description")
            }
            body.update(changes)
            _, err = await _api(base_url, headers, "put", f"{base}/{variable_id}", "update variable", json=body)
            if err:
                return err
            return f"Updated variable {variable_id}."
        if action == "delete":
            if not variable_id:
                return "variable_id is required for action 'delete'."
            _, err = await _api(base_url, headers, "delete", f"{base}/{variable_id}", "delete variable")
            if err:
                return err
            return f"Deleted variable {variable_id}."
        return f"Unknown action '{action}'. Valid actions: list, create, update, delete."

    executor.register("manage_openaev_tag", manage_openaev_tag)
    executor.register("manage_openaev_kill_chain_phase", manage_openaev_kill_chain_phase)
    executor.register("manage_openaev_attack_pattern", manage_openaev_attack_pattern)
    executor.register("manage_openaev_variable", manage_openaev_variable)

    return [
        ToolDefinition(
            name="manage_openaev_tag",
            description=(
                "Manage OpenAEV tags. Actions: 'list' (search), 'create' (name + color "
                "hex), 'update' (tag_id + name/color), 'upsert' (name + color), "
                "'delete' (tag_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "update", "upsert", "delete"]},
                    "tag_id": {"type": "string"},
                    "name": {"type": "string"},
                    "color": {"type": "string", "description": "Hex color, e.g. #00b1ff."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_kill_chain_phase",
            description=(
                "Manage OpenAEV kill chain phases. Actions: 'list' (search), 'create' "
                "(kill_chain_name + name + shortname, optional external_id/description/"
                "order), 'update' (phase_id + fields), 'upsert', 'delete' (phase_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "update", "upsert", "delete"]},
                    "phase_id": {"type": "string"},
                    "kill_chain_name": {"type": "string", "description": "e.g. mitre-attack."},
                    "name": {"type": "string"},
                    "shortname": {"type": "string"},
                    "external_id": {"type": "string"},
                    "description": {"type": "string"},
                    "order": {"type": "integer"},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_attack_pattern",
            description=(
                "Manage OpenAEV MITRE ATT&CK patterns. Actions: 'list' (search), "
                "'create' (name + external_id e.g. T1059, optional description/"
                "platforms/kill_chain_phase_ids/parent_id), 'update' "
                "(attack_pattern_id + fields), 'delete' (attack_pattern_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "update", "delete"]},
                    "attack_pattern_id": {"type": "string"},
                    "name": {"type": "string"},
                    "external_id": {"type": "string", "description": "e.g. T1059."},
                    "description": {"type": "string"},
                    "platforms": {"type": "array", "items": {"type": "string"}},
                    "kill_chain_phase_ids": {"type": "array", "items": {"type": "string"}},
                    "parent_id": {"type": "string", "description": "Parent attack pattern (for sub-techniques)."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_variable",
            description=(
                "Manage OpenAEV variables on a scenario or simulation. scope: "
                "'scenario' or 'exercise'/'simulation'; parent_id is the scenario_id or "
                "exercise_id. Actions: 'list', 'create' (key [lowercase + underscores] "
                "+ optional value/description), 'update' (variable_id + fields), "
                "'delete' (variable_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "update", "delete"]},
                    "scope": {"type": "string", "enum": ["scenario", "exercise", "simulation"], "default": "scenario"},
                    "parent_id": {"type": "string"},
                    "variable_id": {"type": "string"},
                    "key": {"type": "string", "description": "Lowercase letters and underscores only."},
                    "value": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["action", "parent_id"],
            },
            source=_SOURCE,
        ),
    ]
