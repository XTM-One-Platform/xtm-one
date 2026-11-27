"""OpenCTI customization tools.

* ``manage_opencti_entity_setting``  - entity type settings (hidden, enforce
  references, attribute configuration) via ``entitySetting`` module
* ``manage_opencti_vocabulary``      - open vocabulary CRUD
* ``list_opencti_vocabulary``        - read one open vocabulary (no write), for
  agents denied the CRUD tool that must still read e.g. ``pattern_type_ov``
* ``manage_opencti_status_template`` - workflow status template CRUD
* ``list_opencti_subtypes``          - read entity sub-types + their workflow
  Status ids (to set a status) and template ids (for ``workflow_id`` filters)
* ``manage_opencti_decay_rule``      - indicator decay rule CRUD
* ``manage_opencti_retention_rule``  - data retention policy CRUD (+ check)
* ``manage_opencti_exclusion_list``  - IoC exclusion list CRUD (file upload)

All require SETTINGS_SET* capabilities. Not draft-gated: OpenCTI drafts stage
knowledge only, so a write of these tools always lands on the live platform.
While the run holds an active draft (the ``opencti-draft-id`` header), every
write action is refused with nothing sent (#2660): an agent staging knowledge
for review must never change a platform setting behind that review. Reads
stay available; outside a draft nothing changes.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _client, _edges, _graphql, _graphql_upload
from .access import _refuse_unapplied
from .workflow import fetch_type_workflows, format_type_workflow, resolve_filters_json, with_status_notes

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencti"
_RETENTION_SCOPES = ("knowledge", "file", "workbench", "history", "activity")
_RETENTION_UNITS = ("minutes", "hours", "days")
_STATUS_ACTIONS = ("add_status", "update_status", "delete_status")
_STATUS_SCOPES = {"global": "GLOBAL", "request_access": "REQUEST_ACCESS"}

# The fields each action of a customization tool reads; any other field passed (an
# argument the tool does not declare included) is refused by name and nothing is sent.
# A field left at its default (limit, scope) counts as not passed.
_ENTITY_SETTING_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"limit"}),
    "get_by_type": frozenset({"target_type"}),
    "patch": frozenset(
        {
            "target_type",
            "platform_hidden_type",
            "enforce_reference",
            "platform_entity_files_ref",
            "sync_workflow_status_by_name",
            "attributes_configuration",
        }
    ),
    "add_status": frozenset({"target_type", "template_id", "order", "scope"}),
    "update_status": frozenset({"target_type", "status_id", "order", "template_id"}),
    "delete_status": frozenset({"target_type", "status_id"}),
}
_VOCABULARY_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"category", "search", "limit"}),
    "get": frozenset({"vocabulary_id"}),
    "create": frozenset({"name", "category", "description", "aliases", "order"}),
    "update": frozenset({"vocabulary_id", "name", "description", "aliases", "order"}),
    "delete": frozenset({"vocabulary_id"}),
}
_STATUS_TEMPLATE_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"search", "limit"}),
    "get": frozenset({"template_id"}),
    "create": frozenset({"name", "color"}),
    "update": frozenset({"template_id", "name", "color"}),
    "delete": frozenset({"template_id"}),
}
_DECAY_FIELDS = frozenset(
    {
        "name",
        "description",
        "order",
        "active",
        "decay_lifetime",
        "decay_pound",
        "decay_points",
        "decay_revoke_score",
        "decay_filters",
    }
)
_DECAY_RULE_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"search", "limit"}),
    "get": frozenset({"decay_rule_id"}),
    "create": _DECAY_FIELDS,
    "update": _DECAY_FIELDS | {"decay_rule_id"},
    "delete": frozenset({"decay_rule_id"}),
}
_RETENTION_RULE_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"search", "limit"}),
    "get": frozenset({"retention_rule_id"}),
    "create": frozenset({"name", "max_retention", "retention_unit", "scope", "filters"}),
    "check": frozenset({"name", "max_retention", "retention_unit", "scope", "filters"}),
    "update": frozenset({"retention_rule_id", "name", "max_retention", "retention_unit", "filters"}),
    "delete": frozenset({"retention_rule_id"}),
}
_EXCLUSION_LIST_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"search", "limit"}),
    "get": frozenset({"exclusion_list_id"}),
    "create": frozenset({"name", "description", "entity_types", "content"}),
    "update": frozenset({"exclusion_list_id", "name", "description", "enabled"}),
    "delete": frozenset({"exclusion_list_id"}),
}


# The actions of each customization tool that only read. Any other action writes to
# the live platform and is refused while the run holds an active draft (#2660).
_READ_ACTIONS: dict[str, frozenset[str]] = {
    "manage_opencti_entity_setting": frozenset({"list", "get_by_type"}),
    "manage_opencti_vocabulary": frozenset({"list", "get"}),
    "manage_opencti_status_template": frozenset({"list", "get"}),
    "manage_opencti_decay_rule": frozenset({"list", "get"}),
    "manage_opencti_retention_rule": frozenset({"list", "get", "check"}),
    "manage_opencti_exclusion_list": frozenset({"list", "get"}),
}

#: The customization tools that write platform settings: the names a seeded agent
#: that must never change a setting denies (``tool_deny_list``, #2660).
CUSTOMIZATION_WRITE_TOOLS: tuple[str, ...] = tuple(_READ_ACTIONS)

_DRAFT_HEADER = "opencti-draft-id"


def _refuse_in_draft(headers: Any, tool: str, action: str) -> str | None:
    """The refusal of a live settings write while the run holds a draft, else None.

    OpenCTI drafts do not stage settings: a vocabulary, entity setting, status
    template, decay, retention or exclusion change made during a draft is live at
    once, outside the review the draft stands for. Unknown actions fall through to
    the tool's own "Unknown action" answer.
    """
    reads = _READ_ACTIONS[tool]
    draft_id = headers.get(_DRAFT_HEADER) if hasattr(headers, "get") else None
    if not draft_id or action in reads or not action:
        return None
    known = {
        "manage_opencti_entity_setting": {"patch", *_STATUS_ACTIONS},
        "manage_opencti_retention_rule": {"create", "update", "delete"},
    }.get(tool, {"create", "update", "delete"})
    if action not in known:
        return None
    return (
        f"Refused: `{tool}` action '{action}' writes a live OpenCTI platform setting, and "
        f"settings are not staged in a draft (this run holds draft {draft_id}). Nothing was "
        "written. Ask a person with the settings capability to make this change; read "
        f"actions ({', '.join(sorted(reads))}) stay available."
    )


def _unless(value: object, default: object) -> object:
    """*value*, or None when it is the parameter's default (a default is not a field passed)."""
    return None if value == default else value


def _flag(value: bool | None) -> object:
    """A tri-state boolean as the guard sees it: None when not passed, else its text.

    The shared guard counts ``False`` as not passed; an explicit ``false`` on an action
    that does not apply the field must be refused like ``true``.
    """
    return None if value is None else str(bool(value)).lower()


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def _gql(query: str, variables: dict | None = None) -> tuple[dict, str | None]:
        async with _client(headers, timeout=30) as client:
            return await _graphql(client, base_url, headers, query, variables)

    async def _workflow_lines(type_id: str) -> list[str]:
        nodes, err = await fetch_type_workflows(_gql, type_id=type_id)
        if err:
            return [f"  workflow: could not be read ({err})"]
        node = nodes[0] if nodes else {}
        status_lines = format_type_workflow(node)
        return [
            f"  workflow enabled: {bool(node.get('workflowEnabled'))}",
            *(status_lines or ["  workflow statuses: none"]),
        ]

    # ── Entity settings ────────────────────────────────────────────────
    async def manage_opencti_entity_setting(
        action: str,
        target_type: str = "",
        platform_hidden_type: bool | None = None,
        enforce_reference: bool | None = None,
        platform_entity_files_ref: bool | None = None,
        sync_workflow_status_by_name: bool | None = None,
        attributes_configuration: str = "",
        limit: int = 100,
        template_id: str = "",
        status_id: str = "",
        order: int | None = None,
        scope: str = "global",
        **extra: object,
    ) -> str:
        action = (action or "").strip().lower()
        refused = _refuse_unapplied(
            action,
            _ENTITY_SETTING_APPLIES,
            {
                "target_type": target_type,
                "platform_hidden_type": _flag(platform_hidden_type),
                "enforce_reference": _flag(enforce_reference),
                "platform_entity_files_ref": _flag(platform_entity_files_ref),
                "sync_workflow_status_by_name": _flag(sync_workflow_status_by_name),
                "attributes_configuration": attributes_configuration,
                "limit": _unless(limit, 100),
                "template_id": template_id,
                "status_id": status_id,
                "order": order,
                "scope": _unless(scope, "global"),
            },
            dict(extra),
        )
        if refused:
            return refused
        if in_draft := _refuse_in_draft(headers, "manage_opencti_entity_setting", action):
            return in_draft

        if action in _STATUS_ACTIONS:
            return await _edit_type_status(
                action, target_type, template_id=template_id, status_id=status_id, order=order, scope=scope
            )

        if action == "list":
            query = (
                "query EntitySettings($first: Int) {"
                "  entitySettings(first: $first) { edges { node { id target_type"
                "    platform_hidden_type enforce_reference"
                "    platform_entity_files_ref } } } }"
            )
            data, err = await _gql(query, {"first": min(max(limit, 1), 500)})
            if err:
                return err
            nodes = _edges(data, "entitySettings")
            if not nodes:
                return "No entity settings found."
            lines = [f"Entity settings ({len(nodes)}):"]
            for n in nodes:
                flags = []
                if n.get("platform_hidden_type"):
                    flags.append("hidden")
                if n.get("enforce_reference"):
                    flags.append("enforce-refs")
                suffix = f" [{', '.join(flags)}]" if flags else ""
                lines.append(f"- {n.get('target_type')}{suffix} (id: {n.get('id')})")
            return "\n".join(lines)

        if action == "get_by_type":
            if not target_type:
                return "target_type is required for action 'get_by_type'."
            data, err = await _entity_setting_by_type(target_type)
            if err:
                return err
            es = data.get("entitySettingByType")
            if not es:
                return f"No entity setting for type '{target_type}'."
            lines = [
                f"Entity setting for {es.get('target_type')} (id: {es.get('id')})",
                f"  platform_hidden_type: {es.get('platform_hidden_type')}",
                f"  enforce_reference: {es.get('enforce_reference')}",
                f"  platform_entity_files_ref: {es.get('platform_entity_files_ref')}",
                *(
                    [f"  sync_workflow_status_by_name: {es.get('sync_workflow_status_by_name')}"]
                    if "sync_workflow_status_by_name" in es
                    else []
                ),
                f"  available settings: {', '.join(es.get('availableSettings') or [])}",
                f"  mandatory attributes: {', '.join(es.get('mandatoryAttributes') or [])}",
                f"  attributes_configuration: {es.get('attributes_configuration') or '-'}",
            ]
            if "workflow_configuration" in (es.get("availableSettings") or []):
                lines.extend(await _workflow_lines(es.get("target_type") or target_type))
            return "\n".join(lines)

        if action == "patch":
            if not target_type:
                return "target_type is required for action 'patch'."
            edits: list[dict] = []
            if platform_hidden_type is not None:
                edits.append({"key": "platform_hidden_type", "value": [platform_hidden_type]})
            if enforce_reference is not None:
                edits.append({"key": "enforce_reference", "value": [enforce_reference]})
            if platform_entity_files_ref is not None:
                edits.append(
                    {"key": "platform_entity_files_ref", "value": [platform_entity_files_ref]}
                )
            if sync_workflow_status_by_name is not None:
                # OpenCTI stores this setting from a string, as its own settings screen sends it.
                edits.append(
                    {
                        "key": "sync_workflow_status_by_name",
                        "value": ["true" if sync_workflow_status_by_name else "false"],
                    }
                )
            if attributes_configuration:
                try:
                    json.loads(attributes_configuration)
                except ValueError as exc:
                    return f"attributes_configuration must be valid JSON: {exc}"
                edits.append(
                    {"key": "attributes_configuration", "value": [attributes_configuration]}
                )
            if not edits:
                return (
                    "Provide at least one of platform_hidden_type, enforce_reference, "
                    "platform_entity_files_ref, sync_workflow_status_by_name, or attributes_configuration."
                )
            id_q = "query EntitySetting($t: String!) {  entitySettingByType(targetType: $t) { id availableSettings } }"
            data, err = await _gql(id_q, {"t": target_type})
            if err:
                return err
            es = data.get("entitySettingByType")
            if not es:
                return f"No entity setting for type '{target_type}'."
            if sync_workflow_status_by_name is not None and "sync_workflow_status_by_name" not in (
                es.get("availableSettings") or []
            ):
                return (
                    f"sync_workflow_status_by_name is not available for '{target_type}' on this OpenCTI "
                    f"(available settings: {', '.join(es.get('availableSettings') or []) or 'none'}). Nothing was written."
                )
            query = (
                "mutation EsPatch($ids: [ID!]!, $input: [EditInput!]!) {"
                "  entitySettingsFieldPatch(ids: $ids, input: $input) { id target_type } }"
            )
            _, err = await _gql(query, {"ids": [es["id"]], "input": edits})
            if err:
                return err
            return f"Updated entity setting for '{target_type}'."

        return (
            f"Unknown action '{action}'. Valid actions: list, get_by_type, patch, "
            "add_status, update_status, delete_status."
        )

    async def _entity_setting_by_type(target_type: str) -> tuple[dict[str, Any], str | None]:
        """Read one type's entity setting, with sync_workflow_status_by_name where OpenCTI knows it.

        An OpenCTI older than the setting refuses the field: the read is made again without it.
        """
        fields = (
            "id target_type platform_hidden_type enforce_reference platform_entity_files_ref"
            " attributes_configuration mandatoryAttributes availableSettings"
        )
        query = "query EntitySetting($t: String!) {  entitySettingByType(targetType: $t) { %s } }"
        data, err = await _gql(query % f"{fields} sync_workflow_status_by_name", {"t": target_type})
        if err and "sync_workflow_status_by_name" in err:
            data, err = await _gql(query % fields, {"t": target_type})
        return data, err

    async def _edit_type_status(
        action: str, target_type: str, *, template_id: str, status_id: str, order: int | None, scope: str
    ) -> str:
        """Write one workflow status of an entity type through ``subTypeEdit``.

        OpenCTI enables a type's workflow once it holds a global status, so
        ``add_status`` on a type without one turns its workflow on. The answer
        is the type's workflow read back, with the per-type Status ids.
        """
        if not target_type:
            return f"target_type is required for action '{action}'."
        if action == "add_status":
            scope_value = _STATUS_SCOPES.get((scope or "global").strip().lower())
            if not scope_value:
                return f"scope must be one of: {', '.join(_STATUS_SCOPES)}."
            if not template_id or order is None:
                return "template_id and order are required for action 'add_status'."
            query = (
                "mutation SubTypeStatusAdd($id: ID!, $input: StatusAddInput!) {"
                "  subTypeEdit(id: $id) { statusAdd(input: $input) { id } } }"
            )
            variables: dict[str, object] = {
                "id": target_type,
                "input": {"template_id": template_id, "order": order, "scope": scope_value},
            }
        elif action == "update_status":
            if not status_id:
                return "status_id (the per-type Status id, not the template id) is required for 'update_status'."
            edits: list[dict[str, object]] = []
            if order is not None:
                edits.append({"key": "order", "value": [order]})
            if template_id:
                edits.append({"key": "template_id", "value": [template_id]})
            if not edits:
                return "Provide order and/or template_id for action 'update_status'."
            query = (
                "mutation SubTypeStatusPatch($id: ID!, $statusId: String!, $input: [EditInput]!) {"
                "  subTypeEdit(id: $id) { statusFieldPatch(statusId: $statusId, input: $input) { id } } }"
            )
            variables = {"id": target_type, "statusId": status_id, "input": edits}
        else:
            if not status_id:
                return "status_id (the per-type Status id, not the template id) is required for 'delete_status'."
            query = (
                "mutation SubTypeStatusDelete($id: ID!, $statusId: String!) {"
                "  subTypeEdit(id: $id) { statusDelete(statusId: $statusId) { id } } }"
            )
            variables = {"id": target_type, "statusId": status_id}
        _, err = await _gql(query, variables)
        if err:
            return err
        done = {"add_status": "Added a status to", "update_status": "Updated a status of"}.get(
            action, "Removed a status from"
        )
        return "\n".join([f"{done} the {target_type} workflow. Read back:", *await _workflow_lines(target_type)])

    # ── Vocabularies ───────────────────────────────────────────────────
    async def manage_opencti_vocabulary(
        action: str,
        vocabulary_id: str = "",
        category: str = "",
        name: str = "",
        description: str = "",
        aliases: list[str] | None = None,
        order: int | None = None,
        search: str = "",
        limit: int = 50,
        **extra: object,
    ) -> str:
        action = (action or "").strip().lower()
        refused = _refuse_unapplied(
            action,
            _VOCABULARY_APPLIES,
            {
                "vocabulary_id": vocabulary_id,
                "category": category,
                "name": name,
                "description": description,
                "aliases": aliases,
                "order": order,
                "search": search,
                "limit": _unless(limit, 50),
            },
            dict(extra),
        )
        if refused:
            return refused
        if in_draft := _refuse_in_draft(headers, "manage_opencti_vocabulary", action):
            return in_draft

        if action == "list":
            query = (
                "query Vocabularies($category: VocabularyCategory, $first: Int,"
                " $search: String) {"
                "  vocabularies(category: $category, first: $first, search: $search,"
                "    orderBy: order, orderMode: asc) {"
                "    edges { node { id name description usages builtIn"
                "      category { key } } } } }"
            )
            data, err = await _gql(
                query,
                {
                    "category": category or None,
                    "first": min(max(limit, 1), 200),
                    "search": search or None,
                },
            )
            if err:
                return err
            nodes = _edges(data, "vocabularies")
            if not nodes:
                return "No vocabulary entries found."
            lines = [f"Vocabulary entries ({len(nodes)}):"]
            for n in nodes:
                cat = (n.get("category") or {}).get("key", "?")
                bi = " [built-in]" if n.get("builtIn") else ""
                lines.append(
                    f"- {n.get('name')} ({cat}) usages={n.get('usages', 0)}{bi} "
                    f"(id: {n.get('id')})"
                )
            return "\n".join(lines)

        if action == "get":
            if not vocabulary_id:
                return "vocabulary_id is required for action 'get'."
            query = (
                "query Vocabulary($id: String!) {"
                "  vocabulary(id: $id) { id name description aliases usages order"
                "    builtIn category { key } } }"
            )
            data, err = await _gql(query, {"id": vocabulary_id})
            if err:
                return err
            v = data.get("vocabulary")
            if not v:
                return f"Vocabulary {vocabulary_id} not found."
            return "\n".join(
                [
                    f"{v.get('name')} (id: {v.get('id')})",
                    f"  category: {(v.get('category') or {}).get('key')}",
                    f"  description: {v.get('description') or '-'}",
                    f"  aliases: {', '.join(v.get('aliases') or []) or '-'}",
                    f"  usages: {v.get('usages')}",
                ]
            )

        if action == "create":
            if not name or not category:
                return "name and category are required for action 'create'."
            inp: dict = {"name": name, "category": category}
            if description:
                inp["description"] = description
            if aliases:
                inp["aliases"] = aliases
            if order is not None:
                inp["order"] = order
            query = (
                "mutation VocabAdd($input: VocabularyAddInput!) {"
                "  vocabularyAdd(input: $input) { id name } }"
            )
            data, err = await _gql(query, {"input": inp})
            if err:
                return err
            v = data.get("vocabularyAdd") or {}
            return f"Created vocabulary '{v.get('name')}' (id: {v.get('id')})."

        if action == "update":
            if not vocabulary_id:
                return "vocabulary_id is required for action 'update'."
            edits = []
            if name:
                edits.append({"key": "name", "value": [name]})
            if description:
                edits.append({"key": "description", "value": [description]})
            if aliases is not None:
                edits.append({"key": "aliases", "value": aliases})
            if order is not None:
                edits.append({"key": "order", "value": [order]})
            if not edits:
                return "Provide name, description, aliases, or order to update."
            query = (
                "mutation VocabPatch($id: ID!, $input: [EditInput!]!) {"
                "  vocabularyFieldPatch(id: $id, input: $input) { id name } }"
            )
            _, err = await _gql(query, {"id": vocabulary_id, "input": edits})
            if err:
                return err
            return f"Updated vocabulary {vocabulary_id}."

        if action == "delete":
            if not vocabulary_id:
                return "vocabulary_id is required for action 'delete'."
            query = "mutation VocabDelete($id: ID!) { vocabularyDelete(id: $id) }"
            _, err = await _gql(query, {"id": vocabulary_id})
            if err:
                return err
            return f"Deleted vocabulary {vocabulary_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, delete."
        )

    # ── Status templates ───────────────────────────────────────────────
    async def manage_opencti_status_template(
        action: str,
        template_id: str = "",
        name: str = "",
        color: str = "",
        search: str = "",
        limit: int = 100,
        **extra: object,
    ) -> str:
        action = (action or "").strip().lower()
        refused = _refuse_unapplied(
            action,
            _STATUS_TEMPLATE_APPLIES,
            {
                "template_id": template_id,
                "name": name,
                "color": color,
                "search": search,
                "limit": _unless(limit, 100),
            },
            dict(extra),
        )
        if refused:
            return refused
        if in_draft := _refuse_in_draft(headers, "manage_opencti_status_template", action):
            return in_draft

        if action == "list":
            query = (
                "query StatusTemplates($first: Int, $search: String) {"
                "  statusTemplates(first: $first, search: $search, orderBy: name,"
                "    orderMode: asc) { edges { node { id name color usages } } } }"
            )
            data, err = await _gql(
                query, {"first": min(max(limit, 1), 500), "search": search or None}
            )
            if err:
                return err
            nodes = _edges(data, "statusTemplates")
            if not nodes:
                return "No status templates found."
            lines = [f"Status templates ({len(nodes)}):"]
            for n in nodes:
                lines.append(
                    f"- {n.get('name')} ({n.get('color')}) usages={n.get('usages', 0)} "
                    f"(id: {n.get('id')})"
                )
            return "\n".join(lines)

        if action == "get":
            if not template_id:
                return "template_id is required for action 'get'."
            query = (
                "query StatusTemplate($id: String!) {"
                "  statusTemplate(id: $id) { id name color usages } }"
            )
            data, err = await _gql(query, {"id": template_id})
            if err:
                return err
            t = data.get("statusTemplate")
            if not t:
                return f"Status template {template_id} not found."
            return (
                f"{t.get('name')} ({t.get('color')}) usages={t.get('usages', 0)} "
                f"(id: {t.get('id')})"
            )

        if action == "create":
            if not name or not color:
                return "name and color (hex, e.g. #00b1ff) are required for 'create'."
            query = (
                "mutation StAdd($input: StatusTemplateAddInput!) {"
                "  statusTemplateAdd(input: $input) { id name } }"
            )
            data, err = await _gql(query, {"input": {"name": name, "color": color}})
            if err:
                return err
            t = data.get("statusTemplateAdd") or {}
            return f"Created status template '{t.get('name')}' (id: {t.get('id')})."

        if action == "update":
            if not template_id:
                return "template_id is required for action 'update'."
            edits = []
            if name:
                edits.append({"key": "name", "value": [name]})
            if color:
                edits.append({"key": "color", "value": [color]})
            if not edits:
                return "Provide name and/or color to update."
            query = (
                "mutation StPatch($id: ID!, $input: [EditInput!]!) {"
                "  statusTemplateFieldPatch(id: $id, input: $input) { id name } }"
            )
            _, err = await _gql(query, {"id": template_id, "input": edits})
            if err:
                return err
            return f"Updated status template {template_id}."

        if action == "delete":
            if not template_id:
                return "template_id is required for action 'delete'."
            query = "mutation StDelete($id: ID!) { statusTemplateDelete(id: $id) }"
            _, err = await _gql(query, {"id": template_id})
            if err:
                return err
            return f"Deleted status template {template_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, delete."
        )

    async def list_opencti_subtypes(search: str = "", limit: int = 200, type: str = "") -> str:
        nodes, err = await fetch_type_workflows(
            _gql, type_id=(type or "").strip(), search=search, limit=limit
        )
        if err:
            return err
        if not nodes:
            return f"No sub-type '{type}'." if type else "No sub-types found."
        lines = [f"Entity sub-types ({len(nodes)}):"]
        for n in nodes:
            wf = " [workflow]" if n.get("workflowEnabled") else ""
            lines.append(f"- {n.get('label')} (id: {n.get('id')}){wf}")
            lines.extend(format_type_workflow(n))
        return "\n".join(lines)

    # ── Decay rules ────────────────────────────────────────────────────
    async def manage_opencti_decay_rule(
        action: str,
        decay_rule_id: str = "",
        name: str = "",
        description: str = "",
        order: int | None = None,
        active: bool | None = None,
        decay_lifetime: int | None = None,
        decay_pound: float | None = None,
        decay_points: list[int] | None = None,
        decay_revoke_score: int | None = None,
        decay_filters: str = "",
        search: str = "",
        limit: int = 50,
        **extra: object,
    ) -> str:
        action = (action or "").strip().lower()
        refused = _refuse_unapplied(
            action,
            _DECAY_RULE_APPLIES,
            {
                "decay_rule_id": decay_rule_id,
                "name": name,
                "description": description,
                "order": order,
                "active": _flag(active),
                "decay_lifetime": decay_lifetime,
                "decay_pound": decay_pound,
                "decay_points": decay_points,
                "decay_revoke_score": decay_revoke_score,
                "decay_filters": decay_filters,
                "search": search,
                "limit": _unless(limit, 50),
            },
            dict(extra),
        )
        if refused:
            return refused
        if in_draft := _refuse_in_draft(headers, "manage_opencti_decay_rule", action):
            return in_draft

        if action == "list":
            query = (
                "query DecayRules($first: Int, $search: String) {"
                "  decayRules(first: $first, search: $search, orderBy: order,"
                "    orderMode: asc) { edges { node { id name order active"
                "    decay_lifetime decay_revoke_score appliedIndicatorsCount"
                "    built_in } } } }"
            )
            data, err = await _gql(
                query, {"first": min(max(limit, 1), 200), "search": search or None}
            )
            if err:
                return err
            nodes = _edges(data, "decayRules")
            if not nodes:
                return "No decay rules found."
            lines = [f"Decay rules ({len(nodes)}):"]
            for n in nodes:
                state = "active" if n.get("active") else "inactive"
                bi = " [built-in]" if n.get("built_in") else ""
                lines.append(
                    f"- {n.get('name')} [{state}] order={n.get('order')} "
                    f"lifetime={n.get('decay_lifetime')}d "
                    f"applied={n.get('appliedIndicatorsCount', 0)}{bi} "
                    f"(id: {n.get('id')})"
                )
            return "\n".join(lines)

        if action == "get":
            if not decay_rule_id:
                return "decay_rule_id is required for action 'get'."
            query = (
                "query DecayRule($id: String!) {"
                "  decayRule(id: $id) { id name description order active decay_lifetime"
                "    decay_pound decay_points decay_revoke_score decay_filters"
                "    appliedIndicatorsCount built_in } }"
            )
            data, err = await _gql(query, {"id": decay_rule_id})
            if err:
                return err
            d = data.get("decayRule")
            if not d:
                return f"Decay rule {decay_rule_id} not found."
            return "\n".join(
                [
                    f"{d.get('name')} (id: {d.get('id')})",
                    f"  active: {d.get('active')} order: {d.get('order')}",
                    f"  lifetime: {d.get('decay_lifetime')} days, pound: {d.get('decay_pound')}",
                    f"  reaction points: {d.get('decay_points')}",
                    f"  revoke score: {d.get('decay_revoke_score')}",
                    f"  applied indicators: {d.get('appliedIndicatorsCount')}",
                    f"  filters: {d.get('decay_filters') or '-'}",
                ]
            )

        if action == "create":
            if not name:
                return "name is required for action 'create'."
            if decay_filters:
                try:
                    json.loads(decay_filters)
                except ValueError as exc:
                    return f"decay_filters must be a valid JSON string: {exc}"
            inp = {
                "name": name,
                "description": description or None,
                "order": order if order is not None else 0,
                "active": bool(active) if active is not None else True,
                "decay_lifetime": decay_lifetime if decay_lifetime is not None else 30,
                "decay_pound": decay_pound if decay_pound is not None else 1.0,
                "decay_points": decay_points,
                "decay_revoke_score": (
                    decay_revoke_score if decay_revoke_score is not None else 0
                ),
                "decay_filters": decay_filters or None,
            }
            query = (
                "mutation DecayAdd($input: DecayRuleAddInput!) {"
                "  decayRuleAdd(input: $input) { id name } }"
            )
            data, err = await _gql(query, {"input": inp})
            if err:
                return err
            d = data.get("decayRuleAdd") or {}
            return f"Created decay rule '{d.get('name')}' (id: {d.get('id')})."

        if action == "update":
            if not decay_rule_id:
                return "decay_rule_id is required for action 'update'."
            # Only patch fields the caller explicitly provided — otherwise an
            # update meant to change just the name would silently reset order,
            # active, lifetime, pound and revoke score to their defaults.
            if decay_filters:
                try:
                    json.loads(decay_filters)
                except ValueError as exc:
                    return f"decay_filters must be a valid JSON string: {exc}"
            edits = []
            if name:
                edits.append({"key": "name", "value": [name]})
            if description:
                edits.append({"key": "description", "value": [description]})
            if order is not None:
                edits.append({"key": "order", "value": [order]})
            if active is not None:
                edits.append({"key": "active", "value": [bool(active)]})
            if decay_lifetime is not None:
                edits.append({"key": "decay_lifetime", "value": [decay_lifetime]})
            if decay_pound is not None:
                edits.append({"key": "decay_pound", "value": [decay_pound]})
            if decay_revoke_score is not None:
                edits.append(
                    {"key": "decay_revoke_score", "value": [decay_revoke_score]}
                )
            if decay_points is not None:
                edits.append({"key": "decay_points", "value": decay_points})
            if decay_filters:
                edits.append({"key": "decay_filters", "value": [decay_filters]})
            if not edits:
                return (
                    "Provide at least one field to update (name, description, order, "
                    "active, decay_lifetime, decay_pound, decay_revoke_score, "
                    "decay_points, or decay_filters)."
                )
            query = (
                "mutation DecayPatch($id: ID!, $input: [EditInput!]!) {"
                "  decayRuleFieldPatch(id: $id, input: $input) { id name } }"
            )
            _, err = await _gql(query, {"id": decay_rule_id, "input": edits})
            if err:
                return err
            return f"Updated decay rule {decay_rule_id}."

        if action == "delete":
            if not decay_rule_id:
                return "decay_rule_id is required for action 'delete'."
            query = "mutation DecayDelete($id: ID!) { decayRuleDelete(id: $id) }"
            _, err = await _gql(query, {"id": decay_rule_id})
            if err:
                return err
            return f"Deleted decay rule {decay_rule_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, delete."
        )

    # ── Retention rules ────────────────────────────────────────────────
    async def manage_opencti_retention_rule(
        action: str,
        retention_rule_id: str = "",
        name: str = "",
        max_retention: int | None = None,
        retention_unit: str = "",
        scope: str = "knowledge",
        filters: str = "",
        search: str = "",
        limit: int = 50,
        **extra: object,
    ) -> str:
        action = (action or "").strip().lower()
        refused = _refuse_unapplied(
            action,
            _RETENTION_RULE_APPLIES,
            {
                "retention_rule_id": retention_rule_id,
                "name": name,
                "max_retention": max_retention,
                "retention_unit": retention_unit,
                "scope": _unless(scope, "knowledge"),
                "filters": filters,
                "search": search,
                "limit": _unless(limit, 50),
            },
            dict(extra),
        )
        if refused:
            return refused
        if in_draft := _refuse_in_draft(headers, "manage_opencti_retention_rule", action):
            return in_draft

        if action == "list":
            query = (
                "query RetentionRules($first: Int, $search: String) {"
                "  retentionRules(first: $first, search: $search, orderBy: name,"
                "    orderMode: asc) { edges { node { id name scope max_retention"
                "    retention_unit remaining_count last_execution_date } } } }"
            )
            data, err = await _gql(
                query, {"first": min(max(limit, 1), 200), "search": search or None}
            )
            if err:
                return err
            nodes = _edges(data, "retentionRules")
            if not nodes:
                return "No retention rules found."
            lines = [f"Retention rules ({len(nodes)}):"]
            for n in nodes:
                lines.append(
                    f"- {n.get('name')} scope={n.get('scope')} "
                    f"keep={n.get('max_retention')} {n.get('retention_unit')} "
                    f"remaining={n.get('remaining_count')} (id: {n.get('id')})"
                )
            return "\n".join(lines)

        if action == "get":
            if not retention_rule_id:
                return "retention_rule_id is required for action 'get'."
            query = (
                "query RetentionRule($id: String!) {"
                "  retentionRule(id: $id) { id name scope max_retention retention_unit"
                "    filters remaining_count last_deleted_count last_execution_date } }"
            )
            data, err = await _gql(query, {"id": retention_rule_id})
            if err:
                return err
            r = data.get("retentionRule")
            if not r:
                return f"Retention rule {retention_rule_id} not found."
            return "\n".join(
                [
                    f"{r.get('name')} (id: {r.get('id')})",
                    f"  scope: {r.get('scope')}",
                    f"  keep: {r.get('max_retention')} {r.get('retention_unit')}",
                    f"  remaining: {r.get('remaining_count')}",
                    f"  last execution: {r.get('last_execution_date') or '-'}",
                    f"  filters: {r.get('filters') or '-'}",
                ]
            )

        if action in ("create", "check"):
            if action == "create" and not name:
                return "name is required for action 'create'."
            unit = retention_unit or "days"
            if scope not in _RETENTION_SCOPES:
                return f"scope must be one of {', '.join(_RETENTION_SCOPES)}."
            if unit not in _RETENTION_UNITS:
                return f"retention_unit must be one of {', '.join(_RETENTION_UNITS)}."
            if filters:
                try:
                    json.loads(filters)
                except ValueError as exc:
                    return f"filters must be a valid JSON string: {exc}"
                filters, _, status_err = await resolve_filters_json(filters, _gql)
                if status_err:
                    return status_err
            inp = {
                "name": name or "preview",
                "max_retention": max(max_retention or 1, 1),
                "retention_unit": unit,
                "scope": scope,
                "filters": filters or None,
            }
            if action == "check":
                query = (
                    "mutation RetentionCheck($input: RetentionRuleAddInput) {"
                    "  retentionRuleCheck(input: $input) }"
                )
                data, err = await _gql(query, {"input": inp})
                if err:
                    return err
                return (
                    f"Retention rule preview: {data.get('retentionRuleCheck')} elements "
                    "currently match (would eventually be deleted)."
                )
            query = (
                "mutation RetentionAdd($input: RetentionRuleAddInput!) {"
                "  retentionRuleAdd(input: $input) { id name } }"
            )
            data, err = await _gql(query, {"input": inp})
            if err:
                return err
            r = data.get("retentionRuleAdd") or {}
            return f"Created retention rule '{r.get('name')}' (id: {r.get('id')})."

        if action == "update":
            if not retention_rule_id:
                return "retention_rule_id is required for action 'update'."
            # Only patch fields the caller explicitly provided — otherwise an
            # update meant to change just the name would silently reset the
            # retention window to the handler defaults (1 day).
            if retention_unit and retention_unit not in _RETENTION_UNITS:
                return f"retention_unit must be one of {', '.join(_RETENTION_UNITS)}."
            if filters:
                try:
                    json.loads(filters)
                except ValueError as exc:
                    return f"filters must be a valid JSON string: {exc}"
                filters, _, status_err = await resolve_filters_json(filters, _gql)
                if status_err:
                    return status_err
            edits = []
            if name:
                edits.append({"key": "name", "value": [name]})
            if max_retention is not None:
                edits.append({"key": "max_retention", "value": [max(max_retention, 1)]})
            if retention_unit:
                edits.append({"key": "retention_unit", "value": [retention_unit]})
            if filters:
                edits.append({"key": "filters", "value": [filters]})
            if not edits:
                return (
                    "Provide at least one field to update (name, max_retention, "
                    "retention_unit, or filters)."
                )
            query = (
                "mutation RetentionEdit($id: ID!, $input: [EditInput]!) {"
                "  retentionRuleEdit(id: $id) { fieldPatch(input: $input) { id name } } }"
            )
            _, err = await _gql(query, {"id": retention_rule_id, "input": edits})
            if err:
                return err
            return f"Updated retention rule {retention_rule_id}."

        if action == "delete":
            if not retention_rule_id:
                return "retention_rule_id is required for action 'delete'."
            query = (
                "mutation RetentionDelete($id: ID!) {"
                "  retentionRuleEdit(id: $id) { delete } }"
            )
            _, err = await _gql(query, {"id": retention_rule_id})
            if err:
                return err
            return f"Deleted retention rule {retention_rule_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, check, "
            "update, delete."
        )

    # ── Exclusion lists ────────────────────────────────────────────────
    async def manage_opencti_exclusion_list(
        action: str,
        exclusion_list_id: str = "",
        name: str = "",
        description: str = "",
        entity_types: list[str] | None = None,
        content: str = "",
        enabled: bool | None = None,
        search: str = "",
        limit: int = 50,
        **extra: object,
    ) -> str:
        action = (action or "").strip().lower()
        refused = _refuse_unapplied(
            action,
            _EXCLUSION_LIST_APPLIES,
            {
                "exclusion_list_id": exclusion_list_id,
                "name": name,
                "description": description,
                "entity_types": entity_types,
                "content": content,
                "enabled": _flag(enabled),
                "search": search,
                "limit": _unless(limit, 50),
            },
            dict(extra),
        )
        if refused:
            return refused
        if in_draft := _refuse_in_draft(headers, "manage_opencti_exclusion_list", action):
            return in_draft

        if action == "list":
            query = (
                "query ExclusionLists($first: Int, $search: String) {"
                "  exclusionLists(first: $first, search: $search, orderBy: name,"
                "    orderMode: asc) { edges { node { id name enabled"
                "    exclusion_list_entity_types exclusion_list_values_count } } } }"
            )
            data, err = await _gql(
                query, {"first": min(max(limit, 1), 200), "search": search or None}
            )
            if err:
                return err
            nodes = _edges(data, "exclusionLists")
            if not nodes:
                return "No exclusion lists found."
            lines = [f"Exclusion lists ({len(nodes)}):"]
            for n in nodes:
                state = "enabled" if n.get("enabled") else "disabled"
                types = ", ".join(n.get("exclusion_list_entity_types") or [])
                lines.append(
                    f"- {n.get('name')} [{state}] types=[{types}] "
                    f"values={n.get('exclusion_list_values_count', 0)} "
                    f"(id: {n.get('id')})"
                )
            return "\n".join(lines)

        if action == "get":
            if not exclusion_list_id:
                return "exclusion_list_id is required for action 'get'."
            query = (
                "query ExclusionList($id: String!) {"
                "  exclusionList(id: $id) { id name description enabled"
                "    exclusion_list_entity_types exclusion_list_values_count } }"
            )
            data, err = await _gql(query, {"id": exclusion_list_id})
            if err:
                return err
            e = data.get("exclusionList")
            if not e:
                return f"Exclusion list {exclusion_list_id} not found."
            return "\n".join(
                [
                    f"{e.get('name')} (id: {e.get('id')})",
                    f"  enabled: {e.get('enabled')}",
                    f"  entity types: {', '.join(e.get('exclusion_list_entity_types') or [])}",
                    f"  values: {e.get('exclusion_list_values_count')}",
                    f"  description: {e.get('description') or '-'}",
                ]
            )

        if action == "create":
            if not name or not entity_types or not content:
                return (
                    "name, entity_types and content (one value per line) are required "
                    "for action 'create'."
                )
            query = (
                "mutation ExclAdd($name: String!, $description: String,"
                " $types: [String!]!, $file: Upload!) {"
                "  exclusionListFileAdd(input: {name: $name, description: $description,"
                "    exclusion_list_entity_types: $types, file: $file}) { id name } }"
            )
            file_bytes = content.encode("utf-8")
            async with _client(headers, timeout=60) as client:
                data, err = await _graphql_upload(
                    client,
                    base_url,
                    headers,
                    query,
                    {
                        "name": name,
                        "description": description or None,
                        "types": entity_types,
                    },
                    "file",
                    f"{name}.txt",
                    file_bytes,
                    "text/plain",
                )
            if err:
                return err
            e = data.get("exclusionListFileAdd") or {}
            return f"Created exclusion list '{e.get('name')}' (id: {e.get('id')})."

        if action == "update":
            if not exclusion_list_id:
                return "exclusion_list_id is required for action 'update'."
            edits = []
            if name:
                edits.append({"key": "name", "value": [name]})
            if description:
                edits.append({"key": "description", "value": [description]})
            if enabled is not None:
                edits.append({"key": "enabled", "value": [enabled]})
            if not edits:
                return "Provide name, description, or enabled to update."
            query = (
                "mutation ExclPatch($id: ID!, $input: [EditInput!]) {"
                "  exclusionListFieldPatch(id: $id, input: $input) { id name enabled } }"
            )
            _, err = await _gql(query, {"id": exclusion_list_id, "input": edits})
            if err:
                return err
            return f"Updated exclusion list {exclusion_list_id}."

        if action == "delete":
            if not exclusion_list_id:
                return "exclusion_list_id is required for action 'delete'."
            query = "mutation ExclDelete($id: ID!) { exclusionListDelete(id: $id) }"
            _, err = await _gql(query, {"id": exclusion_list_id})
            if err:
                return err
            return f"Deleted exclusion list {exclusion_list_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, delete."
        )

    async def list_opencti_vocabulary(category: str = "", search: str = "", limit: int = 50) -> str:
        """Read open vocabulary entries, no write: the read path of an agent denied the CRUD tool."""
        return await manage_opencti_vocabulary("list", category=category, search=search, limit=limit)

    executor.register("manage_opencti_entity_setting", manage_opencti_entity_setting)
    executor.register("manage_opencti_vocabulary", manage_opencti_vocabulary)
    executor.register("list_opencti_vocabulary", list_opencti_vocabulary)
    executor.register("manage_opencti_status_template", manage_opencti_status_template)
    executor.register("list_opencti_subtypes", list_opencti_subtypes)
    executor.register("manage_opencti_decay_rule", manage_opencti_decay_rule)
    executor.register("manage_opencti_retention_rule", with_status_notes(manage_opencti_retention_rule))
    executor.register("manage_opencti_exclusion_list", manage_opencti_exclusion_list)

    return [
        ToolDefinition(
            name="manage_opencti_entity_setting",
            description=(
                "Manage OpenCTI entity-type settings / customization (requires "
                "SETTINGS_SETCUSTOMIZATION). Actions: 'list', 'get_by_type' "
                "(target_type, e.g. 'Report'; includes the workflow Status ids), 'patch' (target_type + any of "
                "platform_hidden_type, enforce_reference, platform_entity_files_ref, "
                "sync_workflow_status_by_name booleans, or attributes_configuration JSON string), 'add_status' (target_type + "
                "template_id + order; the first global status enables the workflow), 'update_status' "
                "(status_id + order and/or template_id), 'delete_status' (status_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get_by_type", "patch", *_STATUS_ACTIONS],
                    },
                    "target_type": {"type": "string", "description": "Entity type, e.g. Report, Malware, Incident."},
                    "platform_hidden_type": {"type": "boolean", "description": "Hide this type from the platform (patch)."},
                    "enforce_reference": {"type": "boolean", "description": "Require external references on update (patch)."},
                    "platform_entity_files_ref": {"type": "boolean", "description": "Enable file references (patch)."},
                    "sync_workflow_status_by_name": {
                        "type": "boolean",
                        "description": "Stream sync name-matches incoming workflow statuses (patch).",
                    },
                    "attributes_configuration": {"type": "string", "description": "Attributes configuration as a JSON string (patch)."},
                    "limit": {"type": "integer", "default": 100},
                    "template_id": {"type": "string", "description": "Status template id (add_status, update_status)."},
                    "status_id": {"type": "string", "description": "Per-type Status id from get_by_type."},
                    "order": {"type": "integer", "description": "Position in the workflow."},
                    "scope": {"type": "string", "enum": list(_STATUS_SCOPES), "default": "global"},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencti_vocabulary",
            description=(
                "Manage OpenCTI open vocabularies (requires SETTINGS_SETVOCABULARIES "
                "for writes). Actions: 'list' (optional category like "
                "'report_types_ov', search), 'get' (vocabulary_id), 'create' (name + "
                "category, optional description/aliases/order), 'update' "
                "(vocabulary_id + fields), 'delete' (vocabulary_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "create", "update", "delete"]},
                    "vocabulary_id": {"type": "string"},
                    "category": {"type": "string", "description": "Vocabulary category, e.g. report_types_ov, incident_type_ov."},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "order": {"type": "integer"},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencti_vocabulary",
            description=(
                "Read OpenCTI open vocabulary entries, no write: the values a field may take on "
                "this platform, e.g. category 'pattern_type_ov' for the indicator pattern types "
                "(kql, spl, sigma...) before creating indicators. Optional search and limit."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Vocabulary category, e.g. pattern_type_ov, report_types_ov.",
                    },
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencti_status_template",
            description=(
                "Manage OpenCTI workflow status templates (requires "
                "SETTINGS_SETSTATUSTEMPLATES for writes). Actions: 'list' (search), "
                "'get' (template_id), 'create' (name + color hex like '#00b1ff'), "
                "'update' (template_id + name/color), 'delete' (template_id). Template "
                "ids go in workflow_id filters; setting a status takes the per-type "
                "Status id (list_opencti_subtypes)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "create", "update", "delete"]},
                    "template_id": {"type": "string"},
                    "name": {"type": "string"},
                    "color": {"type": "string", "description": "Hex color, e.g. #00b1ff."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 100},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencti_subtypes",
            description=(
                "List OpenCTI entity sub-types and their workflow statuses (read-only): "
                "each status with its Status id, template id, order and color. workflow_id "
                "filters take the template id; setting a status (playbook 'change "
                "status', x_opencti_workflow_id) takes the Status id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "type": {"type": "string", "description": "One entity type exactly, e.g. Report."},
                    "limit": {"type": "integer", "default": 200},
                },
                "required": [],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencti_decay_rule",
            description=(
                "Manage OpenCTI indicator decay rules (requires "
                "SETTINGS_SETCUSTOMIZATION). Actions: 'list', 'get' (decay_rule_id), "
                "'create' (name + optional order + active + decay_lifetime days + "
                "decay_pound + decay_revoke_score + decay_points array + decay_filters "
                "JSON string), 'update' (decay_rule_id + only the fields you want to "
                "change), 'delete'. On 'update' only the fields you pass are modified — "
                "omitted fields keep their current value."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "create", "update", "delete"]},
                    "decay_rule_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "order": {"type": "integer", "description": "Evaluation order, lower wins (create default 0)."},
                    "active": {"type": "boolean", "description": "Whether the rule is active (create default true)."},
                    "decay_lifetime": {"type": "integer", "description": "Lifetime in days (create default 30)."},
                    "decay_pound": {"type": "number", "description": "Decay curve factor (create default 1.0)."},
                    "decay_points": {"type": "array", "items": {"type": "integer"}, "description": "Reaction score points."},
                    "decay_revoke_score": {"type": "integer", "description": "Score at which indicators are revoked (create default 0)."},
                    "decay_filters": {"type": "string", "description": "Scope filters as a JSON string."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencti_retention_rule",
            description=(
                "Manage OpenCTI data retention policies (requires "
                "SETTINGS_SETCUSTOMIZATION). Actions: 'list', 'get' "
                "(retention_rule_id), 'create' (name + max_retention + retention_unit "
                "[minutes/hours/days] + scope [knowledge/file/workbench/history/"
                "activity] + optional filters JSON string), 'check' (preview how many "
                "elements currently match WITHOUT creating), 'update' "
                "(retention_rule_id + only the fields you want to change), 'delete'. "
                "On 'update' only the fields you pass are modified — omitted fields "
                "keep their current value. 'filters' is a JSON STRING (serialized "
                "FilterGroup)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "create", "check", "update", "delete"]},
                    "retention_rule_id": {"type": "string"},
                    "name": {"type": "string"},
                    "max_retention": {"type": "integer", "description": "Keep elements for this many units, min 1 (create default 1)."},
                    "retention_unit": {"type": "string", "enum": ["minutes", "hours", "days"], "description": "Retention time unit (create default days)."},
                    "scope": {"type": "string", "enum": ["knowledge", "file", "workbench", "history", "activity"], "default": "knowledge"},
                    "filters": {"type": "string", "description": "JSON string of a FilterGroup."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencti_exclusion_list",
            description=(
                "Manage OpenCTI exclusion lists (allowlists that prevent matching "
                "observables/indicators; requires SETTINGS_SETCUSTOMIZATION). Actions: "
                "'list', 'get' (exclusion_list_id), 'create' (name + entity_types array "
                "e.g. ['IPv4-Addr','Domain-Name'] + content with one value per line), "
                "'update' (exclusion_list_id + name/description/enabled metadata), "
                "'delete'. To change values, recreate the list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "create", "update", "delete"]},
                    "exclusion_list_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "entity_types": {"type": "array", "items": {"type": "string"}, "description": "STIX types the list applies to (create)."},
                    "content": {"type": "string", "description": "List values, one per line (create)."},
                    "enabled": {"type": "boolean", "description": "Enable/disable the list (update)."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
