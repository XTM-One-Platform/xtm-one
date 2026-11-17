# SPDX-License-Identifier: Apache-2.0

"""OpenAEV atomic testing + payload management tools.

* ``manage_openaev_atomic_testing`` - one-off inject tests: search/get/create/
  update/delete and launch/relaunch/duplicate (``AtomicTestingApi``). Launch
  and relaunch EXECUTE against the targeted assets.
* ``manage_openaev_payload`` - payload management gaps: list/get/update/delete/
  duplicate/upsert (``PayloadApi``). Payload creation with injector-contract
  polling stays in the core ``create_openaev_payload`` tool.
"""

from __future__ import annotations

import json
import logging

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _api, _normalize_output_parsers, _search_body

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def manage_openaev_atomic_testing(
        action: str = "",
        inject_id: str = "",
        title: str = "",
        description: str = "",
        injector_contract_id: str = "",
        asset_ids: list[str] | None = None,
        asset_group_ids: list[str] | None = None,
        content: str = "",
        recurrence: str = "",
        recurrence_start: str = "",
        recurrence_end: str = "",
        search: str = "",
        limit: int = 20,
    ) -> str:
        action = (action or "").strip().lower()

        def _atomic_body(for_create: bool) -> dict:
            # Only send fields the caller provided so an 'update' that changes
            # one attribute cannot wipe the title / targeting. On 'create' send
            # the full atomic-test shape the API expects (atomic tests have no
            # teams).
            body: dict = {}
            if title:
                body["inject_title"] = title
            if description:
                body["inject_description"] = description
            if injector_contract_id:
                body["inject_injector_contract"] = injector_contract_id
            if asset_ids is not None:
                body["inject_assets"] = asset_ids
            if asset_group_ids is not None:
                body["inject_asset_groups"] = asset_group_ids
            if content:
                try:
                    body["inject_content"] = json.loads(content)
                except json.JSONDecodeError:
                    body["inject_content"] = {"content": content}
            if for_create:
                body.setdefault("inject_assets", [])
                body.setdefault("inject_asset_groups", [])
                body.setdefault("inject_content", {})
                body["inject_teams"] = []
                body["inject_all_teams"] = False
            return body

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/atomic-testings/search",
                "search atomic tests",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No atomic tests found."
            lines = [f"Atomic tests ({len(items)}):"]
            for i in items:
                lines.append(
                    f"- {i.get('inject_title', '?')} (id: {i.get('inject_id')})"
                )
            return "\n".join(lines)

        if action == "get":
            if not inject_id:
                return "inject_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/atomic-testings/{inject_id}",
                "get atomic test",
            )
            if err:
                return err
            return json.dumps(resp.json(), default=str)[:3000]

        if action in ("create", "update"):
            if action == "create" and (not title or not injector_contract_id):
                return "title and injector_contract_id are required for 'create'."
            if action == "update" and not inject_id:
                return "inject_id is required for 'update'."
            body = _atomic_body(for_create=action == "create")
            if action == "update" and not body:
                return (
                    "Provide at least one field to update (title, description, "
                    "injector_contract_id, asset_ids, asset_group_ids, or content)."
                )
            if action == "create":
                resp, err = await _api(
                    base_url, headers, "post", "/api/atomic-testings",
                    "create atomic test", json=body,
                )
            else:
                resp, err = await _api(
                    base_url, headers, "put", f"/api/atomic-testings/{inject_id}",
                    "update atomic test", json=body,
                )
            if err:
                return err
            i = resp.json()
            verb = "Created" if action == "create" else "Updated"
            return f"{verb} atomic test '{i.get('inject_title')}' (id: {i.get('inject_id')})."

        if action == "delete":
            if not inject_id:
                return "inject_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"/api/atomic-testings/{inject_id}",
                "delete atomic test",
            )
            if err:
                return err
            return f"Deleted atomic test {inject_id}."

        if action in ("launch", "relaunch", "duplicate"):
            if not inject_id:
                return f"inject_id is required for action '{action}'."
            resp, err = await _api(
                base_url, headers, "post",
                f"/api/atomic-testings/{inject_id}/{action}",
                f"{action} atomic test",
            )
            if err:
                return err
            i = resp.json() if resp.content else {}
            if action == "duplicate":
                return f"Duplicated atomic test - new id: {i.get('inject_id')}."
            note = (
                " Each execution archives the previous results and creates fresh "
                "expectations for every target, so read the results again "
                "afterwards rather than reusing earlier verdicts."
            )
            return f"{action.capitalize()}ed atomic test {inject_id}.{note}"

        if action == "recurrence":
            if not inject_id:
                return "inject_id is required for action 'recurrence'."
            body: dict = {}
            if recurrence:
                body["inject_recurrence"] = recurrence
            if recurrence_start:
                body["inject_recurrence_start"] = recurrence_start
            if recurrence_end:
                body["inject_recurrence_end"] = recurrence_end
            if not body:
                return (
                    "Provide at least one field for 'recurrence': recurrence (a "
                    "cron expression), recurrence_start or recurrence_end (ISO "
                    "timestamps). Pass recurrence='' with a start/end to clear a "
                    "schedule."
                )
            resp, err = await _api(
                base_url, headers, "put",
                f"/api/atomic-testings/{inject_id}/recurrence",
                "set atomic test recurrence", json=body,
            )
            if err:
                return err
            return (
                f"Set the recurrence of atomic test {inject_id}. Every scheduled "
                "run behaves like a manual relaunch: previous results are archived "
                "and new expectations are created for all targets."
            )

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "delete, launch, relaunch, duplicate, recurrence."
        )

    async def manage_openaev_payload(
        action: str = "",
        payload_id: str = "",
        name: str = "",
        description: str = "",
        config: str = "",
        search: str = "",
        limit: int = 20,
    ) -> str:
        action = (action or "").strip().lower()

        extra: dict = {}
        if config:
            try:
                extra = json.loads(config)
            except ValueError as exc:
                return f"config must be a valid JSON object: {exc}"
            if not isinstance(extra, dict):
                return "config must be a JSON object of field->value pairs."

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/payloads/search", "search payloads",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No payloads found."
            lines = [f"Payloads ({len(items)}):"]
            for p in items:
                lines.append(
                    f"- {p.get('payload_name', '?')} [{p.get('payload_type', '?')}] "
                    f"(id: {p.get('payload_id')})"
                )
            return "\n".join(lines)

        if action == "get":
            if not payload_id:
                return "payload_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/payloads/{payload_id}", "get payload"
            )
            if err:
                return err
            return json.dumps(resp.json(), default=str)[:3000]

        if action == "update":
            if not payload_id:
                return "payload_id is required for action 'update'."
            body = dict(extra)
            if name:
                body["payload_name"] = name
            if description:
                body["payload_description"] = description
            if not body:
                return "Provide name, description, or config fields to update."
            parser_err = _normalize_output_parsers(body)
            if parser_err:
                return parser_err
            # The payload PUT is a full replace: any field the caller omits is nulled. Platforms
            # are mandatory (a payload with no platform cannot run), and omitting them used to null
            # them and break contract rebuilding, so carry the payload's current platforms forward
            # whenever the update did not set them explicitly.
            if not body.get("payload_platforms"):
                current, cur_err = await _api(
                    base_url, headers, "get", f"/api/payloads/{payload_id}",
                    "get payload",
                )
                if cur_err:
                    return cur_err
                existing = current.json().get("payload_platforms") or []
                if existing:
                    body["payload_platforms"] = existing
            resp, err = await _api(
                base_url, headers, "put", f"/api/payloads/{payload_id}",
                "update payload", json=body,
            )
            if err:
                return err
            return f"Updated payload {payload_id}."

        if action == "delete":
            if not payload_id:
                return "payload_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"/api/payloads/{payload_id}",
                "delete payload",
            )
            if err:
                return err
            return f"Deleted payload {payload_id}."

        if action == "duplicate":
            if not payload_id:
                return "payload_id is required for action 'duplicate'."
            resp, err = await _api(
                base_url, headers, "post", f"/api/payloads/{payload_id}/duplicate",
                "duplicate payload",
            )
            if err:
                return err
            p = resp.json()
            return f"Duplicated payload - new id: {p.get('payload_id')}."

        if action == "upsert":
            body = dict(extra)
            if name:
                body["payload_name"] = name
            if description:
                body["payload_description"] = description
            if not body:
                return "Provide config (and/or name) for 'upsert'."
            parser_err = _normalize_output_parsers(body)
            if parser_err:
                return parser_err
            resp, err = await _api(
                base_url, headers, "post", "/api/payloads/upsert", "upsert payload",
                json=body,
            )
            if err:
                return err
            p = resp.json()
            return f"Upserted payload '{p.get('payload_name')}' (id: {p.get('payload_id')})."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, update, delete, "
            "duplicate, upsert. (To create a payload use create_openaev_payload.)"
        )

    executor.register("manage_openaev_atomic_testing", manage_openaev_atomic_testing)
    executor.register("manage_openaev_payload", manage_openaev_payload)

    return [
        ToolDefinition(
            name="manage_openaev_atomic_testing",
            description=(
                "Manage OpenAEV atomic tests (one-off inject executions). Actions: "
                "'list' (search), 'get' (inject_id), 'create' (title + "
                "injector_contract_id, optional description/asset_ids/asset_group_ids/"
                "content JSON), 'update' (inject_id + fields), 'delete' (inject_id), "
                "'launch' / 'relaunch' (inject_id - EXECUTES the test against the "
                "targeted assets), 'duplicate' (inject_id), 'recurrence' "
                "(inject_id + recurrence cron, optional recurrence_start / "
                "recurrence_end - run the test on a schedule). Every launch, "
                "relaunch or scheduled run ARCHIVES the previous results and "
                "creates fresh expectations for all targets, so re-read the "
                "results afterwards instead of reusing earlier verdicts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list", "get", "create", "update", "delete", "launch",
                            "relaunch", "duplicate", "recurrence",
                        ],
                    },
                    "inject_id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "injector_contract_id": {"type": "string"},
                    "asset_ids": {"type": "array", "items": {"type": "string"}},
                    "asset_group_ids": {"type": "array", "items": {"type": "string"}},
                    "content": {"type": "string", "description": "Inject content as a JSON object string."},
                    "recurrence": {"type": "string", "description": "Cron expression for the 'recurrence' action."},
                    "recurrence_start": {"type": "string", "description": "ISO timestamp when the recurrence starts."},
                    "recurrence_end": {"type": "string", "description": "ISO timestamp when the recurrence ends."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_payload",
            description=(
                "Manage OpenAEV payloads directly (the raw payload record). "
                "PREFER manage_openaev_threat_arsenal: OpenAEV v3 manages attack "
                "content as Threat Arsenal 'actions' (payload + injector contract) "
                "and this payload API is deprecated for removal - use this tool "
                "only for payload-level fields the arsenal tool does not expose. "
                "Actions: 'list' (search), 'get' (payload_id), 'update' "
                "(payload_id + name/description and/or config JSON of payload "
                "fields), 'delete' (payload_id), 'duplicate' (payload_id), "
                "'upsert' (config JSON, optional name/description). Type-specific "
                "payload fields go in the 'config' JSON object (e.g. "
                "command_content, executable_file, dns_resolution_hostname, "
                "payload_platforms, payload_tags, payload_expectations)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "update", "delete", "duplicate", "upsert"],
                    },
                    "payload_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "config": {"type": "string", "description": "JSON object of payload fields (update/upsert)."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
