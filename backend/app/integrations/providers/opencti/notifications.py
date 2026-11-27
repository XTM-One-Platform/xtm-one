"""OpenCTI notifier, trigger/digest, and inbox-notification tools.

* ``manage_opencti_notifier``     - notifier connectors CRUD + test
  (``src/modules/notifier/notifier.graphql``)
* ``manage_opencti_trigger``      - live + digest triggers, knowledge + activity
  kinds (``src/modules/notification/notification.graphql``)
* ``manage_opencti_notification`` - the current user's notification inbox

Not draft-gated (platform configuration, not STIX knowledge).
"""

from __future__ import annotations

import json
import logging

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _client, _edges, _graphql
from .access import _refuse_unapplied
from .workflow import resolve_filters_json, with_status_notes

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencti"
_NOTIFIER_DEFAULT_LIMIT = 50
_NOTIFIER_DEFAULT_TEST_ID = "default_notification"
_TRIGGER_DEFAULT_LIMIT = 50
_TRIGGER_DEFAULT_PERIOD = "day"
_NOTIFICATION_DEFAULT_LIMIT = 20
# The fields each action of the three tools reads; any other field is refused.
_NOTIFIER_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"search", "limit"}),
    "get": frozenset({"notifier_id"}),
    "create": frozenset({"name", "description", "notifier_connector_id", "notifier_configuration"}),
    "update": frozenset({"notifier_id", "name", "description", "notifier_configuration"}),
    "delete": frozenset({"notifier_id"}),
    "test": frozenset({"notifier_connector_id", "notifier_configuration", "notifier_test_id"}),
}
_TRIGGER_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"kind", "search", "limit"}),
    "get": frozenset({"kind", "trigger_id"}),
    "create_live": frozenset(
        {"kind", "name", "description", "event_types", "instance_trigger", "filters", "notifier_ids", "recipients"}
    ),
    "create_digest": frozenset(
        {"kind", "name", "description", "trigger_ids", "period", "trigger_time", "notifier_ids", "recipients"}
    ),
    "update": frozenset({"kind", "trigger_id", "name", "description", "filters", "notifier_ids"}),
    "delete": frozenset({"kind", "trigger_id"}),
}
# An activity live trigger has no event types and no instance mode.
_ACTIVITY_TRIGGER_APPLIES: dict[str, frozenset[str]] = {
    **_TRIGGER_APPLIES,
    "create_live": _TRIGGER_APPLIES["create_live"] - {"event_types", "instance_trigger"},
}
_NOTIFICATION_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"search", "limit"}),
    "mark_read": frozenset({"notification_id"}),
    "mark_unread": frozenset({"notification_id"}),
    "delete": frozenset({"notification_id"}),
}

# Built-in notifier connector IDs, copied verbatim from OpenCTI's
# ``notifier-statics.ts`` (NOTIFIER_CONNECTOR_*). These are the exact values the
# OpenCTI API expects, so they must NOT be "normalised" — in particular the
# User interface connector ID intentionally has a 9-character first segment
# (``f39b8ab2c``); it is not a typo and matching it exactly is what makes the
# built-in UI notifier resolvable.
_BUILTIN_CONNECTORS = (
    "Email (full template): 6f5e30a8-56d5-4ff1-8b8d-f90243f771dc; "
    "Simplified email: 9f73d9f8-cc4c-432b-b5b0-be6b6d6c8d87; "
    "Webhook: 08f9f00f-4e52-4466-ae27-be9fa9813a88; "
    "User interface: f39b8ab2c-8f5c-4167-a249-229f34d9442b"
)


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def _gql(query: str, variables: dict | None = None) -> tuple[dict, str | None]:
        async with _client(headers, timeout=30) as client:
            return await _graphql(client, base_url, headers, query, variables)

    # ── Notifiers ──────────────────────────────────────────────────────
    async def manage_opencti_notifier(
        action: str,
        notifier_id: str = "",
        name: str = "",
        description: str = "",
        notifier_connector_id: str = "",
        notifier_configuration: str = "",
        notifier_test_id: str = _NOTIFIER_DEFAULT_TEST_ID,
        search: str = "",
        limit: int = _NOTIFIER_DEFAULT_LIMIT,
        **extra: object,
    ) -> str:
        action = (action or "").strip().lower()
        # A field the action ignores (or an undeclared one) is refused by name:
        # nothing is sent, never a silent "Deleted". Defaults count as not passed.
        refused = _refuse_unapplied(
            action,
            _NOTIFIER_APPLIES,
            {
                "notifier_id": notifier_id,
                "name": name,
                "description": description,
                "notifier_connector_id": notifier_connector_id,
                "notifier_configuration": notifier_configuration,
                "notifier_test_id": None if notifier_test_id == _NOTIFIER_DEFAULT_TEST_ID else notifier_test_id,
                "search": search,
                "limit": None if limit == _NOTIFIER_DEFAULT_LIMIT else limit,
            },
            dict(extra),
        )
        if refused:
            return refused

        if action == "list":
            query = (
                "query Notifiers($first: Int, $search: String) {"
                "  notifiers(first: $first, search: $search) {"
                "    edges { node { id name description"
                "      notifier_connector { name } } } } }"
            )
            data, err = await _gql(
                query, {"first": min(max(limit, 1), 200), "search": search or None}
            )
            if err:
                return err
            nodes = _edges(data, "notifiers")
            if not nodes:
                return "No notifiers found."
            lines = [f"Notifiers ({len(nodes)}):"]
            for n in nodes:
                conn = (n.get("notifier_connector") or {}).get("name", "?")
                lines.append(f"- {n.get('name')} [{conn}] (id: {n.get('id')})")
            return "\n".join(lines)

        if action == "get":
            if not notifier_id:
                return "notifier_id is required for action 'get'."
            query = (
                "query Notifier($id: String!) {"
                "  notifier(id: $id) { id name description"
                "    notifier_connector { id name } notifier_configuration } }"
            )
            data, err = await _gql(query, {"id": notifier_id})
            if err:
                return err
            n = data.get("notifier")
            if not n:
                return f"Notifier {notifier_id} not found."
            conn = n.get("notifier_connector") or {}
            # The configuration can carry delivery endpoints / credentials
            # (e.g. webhook URLs with tokens, SMTP secrets). Never echo it back
            # verbatim — surface only whether it is set. Callers replace it via
            # the 'update' action.
            config = (
                "[hidden - may contain delivery endpoints/credentials; set it "
                "with the 'update' action]"
                if n.get("notifier_configuration")
                else "-"
            )
            return "\n".join(
                [
                    f"{n.get('name')} (id: {n.get('id')})",
                    f"  connector: {conn.get('name')} (id: {conn.get('id')})",
                    f"  description: {n.get('description') or '-'}",
                    f"  configuration: {config}",
                ]
            )

        if action == "create":
            if not name or not notifier_connector_id or not notifier_configuration:
                return (
                    "name, notifier_connector_id and notifier_configuration are "
                    f"required for 'create'. Built-in connectors -> {_BUILTIN_CONNECTORS}"
                )
            try:
                json.loads(notifier_configuration)
            except ValueError as exc:
                return f"notifier_configuration must be valid JSON: {exc}"
            query = (
                "mutation NotifierAdd($input: NotifierAddInput!) {"
                "  notifierAdd(input: $input) { id name } }"
            )
            data, err = await _gql(
                query,
                {
                    "input": {
                        "name": name,
                        "description": description or None,
                        "notifier_connector_id": notifier_connector_id,
                        "notifier_configuration": notifier_configuration,
                    }
                },
            )
            if err:
                return err
            n = data.get("notifierAdd") or {}
            return f"Created notifier '{n.get('name')}' (id: {n.get('id')})."

        if action == "update":
            if not notifier_id:
                return "notifier_id is required for action 'update'."
            edits = []
            if name:
                edits.append({"key": "name", "value": [name]})
            if description:
                edits.append({"key": "description", "value": [description]})
            if notifier_configuration:
                try:
                    json.loads(notifier_configuration)
                except ValueError as exc:
                    return f"notifier_configuration must be valid JSON: {exc}"
                edits.append(
                    {"key": "notifier_configuration", "value": [notifier_configuration]}
                )
            if not edits:
                return "Provide name, description, or notifier_configuration to update."
            # #2632: OpenCTI notifierEdit validates notifier_connector_id and
            # notifier_configuration read from the edit input on every patch (a
            # missing key reads as empty: "Invalid notifier connector"), so the
            # stored values travel with whatever field changes.
            current, err = await _gql(
                "query NotifierEditBase($id: String!) {"
                "  notifier(id: $id) { notifier_connector { id } notifier_configuration } }",
                {"id": notifier_id},
            )
            if err:
                return err
            stored = current.get("notifier")
            if not stored:
                return f"Notifier {notifier_id} not found."
            if not notifier_configuration:
                edits.append({"key": "notifier_configuration", "value": [stored.get("notifier_configuration") or ""]})
            edits.append(
                {"key": "notifier_connector_id", "value": [(stored.get("notifier_connector") or {}).get("id") or ""]}
            )
            query = (
                "mutation NotifierPatch($id: ID!, $input: [EditInput!]!) {"
                "  notifierFieldPatch(id: $id, input: $input) { id name } }"
            )
            _, err = await _gql(query, {"id": notifier_id, "input": edits})
            if err:
                return err
            return f"Updated notifier {notifier_id}."

        if action == "delete":
            if not notifier_id:
                return "notifier_id is required for action 'delete'."
            query = "mutation NotifierDelete($id: ID!) { notifierDelete(id: $id) }"
            _, err = await _gql(query, {"id": notifier_id})
            if err:
                return err
            return f"Deleted notifier {notifier_id}."

        if action == "test":
            if not notifier_connector_id or not notifier_configuration:
                return (
                    "notifier_connector_id and notifier_configuration are required "
                    "for 'test'."
                )
            try:
                json.loads(notifier_configuration)
            except ValueError as exc:
                return f"notifier_configuration must be valid JSON: {exc}"
            query = (
                "query NotifierTest($input: NotifierTestInput!) {"
                "  notifierTest(input: $input) }"
            )
            data, err = await _gql(
                query,
                {
                    "input": {
                        "notifier_test_id": notifier_test_id or "default_notification",
                        "notifier_connector_id": notifier_connector_id,
                        "notifier_configuration": notifier_configuration,
                    }
                },
            )
            if err:
                return err
            return f"Notifier test result: {data.get('notifierTest')}"

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "delete, test."
        )

    # ── Triggers / digests ─────────────────────────────────────────────
    async def manage_opencti_trigger(
        action: str,
        kind: str = "knowledge",
        trigger_id: str = "",
        name: str = "",
        description: str = "",
        event_types: list[str] | None = None,
        instance_trigger: bool = False,
        filters: str = "",
        notifier_ids: list[str] | None = None,
        recipients: list[str] | None = None,
        trigger_ids: list[str] | None = None,
        period: str = _TRIGGER_DEFAULT_PERIOD,
        trigger_time: str = "",
        search: str = "",
        limit: int = _TRIGGER_DEFAULT_LIMIT,
        **extra: object,
    ) -> str:
        action = (action or "").strip().lower()
        kind = (kind or "knowledge").strip().lower()
        is_activity = kind == "activity"
        # Refused before the filters are resolved, so nothing reaches OpenCTI.
        refused = _refuse_unapplied(
            action,
            _ACTIVITY_TRIGGER_APPLIES if is_activity else _TRIGGER_APPLIES,
            {
                "kind": kind,
                "trigger_id": trigger_id,
                "name": name,
                "description": description,
                "event_types": event_types,
                "instance_trigger": instance_trigger,
                "filters": filters,
                "notifier_ids": notifier_ids,
                "recipients": recipients,
                "trigger_ids": trigger_ids,
                "period": None if period == _TRIGGER_DEFAULT_PERIOD else period,
                "trigger_time": trigger_time,
                "search": search,
                "limit": None if limit == _TRIGGER_DEFAULT_LIMIT else limit,
            },
            dict(extra),
        )
        if refused:
            return refused

        if filters:
            try:
                json.loads(filters)
            except ValueError as exc:
                return f"filters must be a valid JSON string: {exc}"
            filters, _, status_err = await resolve_filters_json(filters, _gql)
            if status_err:
                return status_err

        if action == "list":
            field = "triggersActivity" if is_activity else "triggersKnowledge"
            query = (
                f"query Triggers($first: Int, $search: String) {{"
                f"  {field}(first: $first, search: $search) {{"
                f"    edges {{ node {{ id name trigger_type event_types period"
                f"      notifiers {{ name }} }} }} }} }}"
            )
            data, err = await _gql(
                query, {"first": min(max(limit, 1), 200), "search": search or None}
            )
            if err:
                return err
            nodes = _edges(data, field)
            if not nodes:
                return f"No {kind} triggers found."
            lines = [f"{kind.capitalize()} triggers ({len(nodes)}):"]
            for n in nodes:
                ttype = n.get("trigger_type")
                extra = (
                    f" period={n.get('period')}"
                    if ttype == "digest"
                    else f" events={','.join(n.get('event_types') or [])}"
                )
                lines.append(f"- {n.get('name')} [{ttype}]{extra} (id: {n.get('id')})")
            return "\n".join(lines)

        if action == "get":
            if not trigger_id:
                return "trigger_id is required for action 'get'."
            field = "triggerActivity" if is_activity else "triggerKnowledge"
            query = (
                f"query Trigger($id: String!) {{"
                f"  {field}(id: $id) {{ id name description trigger_type event_types"
                f"    filters period trigger_time instance_trigger"
                f"    notifiers {{ name }} recipients {{ name }} }} }}"
            )
            data, err = await _gql(query, {"id": trigger_id})
            if err:
                return err
            t = data.get(field)
            if not t:
                return f"Trigger {trigger_id} not found."
            notifiers = ", ".join(x.get("name", "") for x in (t.get("notifiers") or []))
            recips = ", ".join(x.get("name", "") for x in (t.get("recipients") or []))
            lines = [
                f"{t.get('name')} [{t.get('trigger_type')}] (id: {t.get('id')})",
                f"  description: {t.get('description') or '-'}",
                f"  notifiers: {notifiers or 'none'}",
                f"  recipients: {recips or 'self'}",
            ]
            if t.get("trigger_type") == "digest":
                lines.append(f"  period: {t.get('period')} at {t.get('trigger_time') or '-'}")
            else:
                lines.append(f"  event_types: {', '.join(t.get('event_types') or [])}")
                lines.append(f"  instance_trigger: {t.get('instance_trigger')}")
            if t.get("filters"):
                lines.append(f"  filters: {t['filters']}")
            return "\n".join(lines)

        if action == "create_live":
            if not name:
                return "name is required for action 'create_live'."
            if is_activity:
                if not recipients:
                    return "recipients are required for an activity live trigger."
                inp = {
                    "name": name,
                    "description": description or None,
                    "notifiers": notifier_ids or [],
                    "filters": filters or None,
                    "recipients": recipients,
                }
                query = (
                    "mutation($input: TriggerActivityLiveAddInput!) {"
                    "  triggerActivityLiveAdd(input: $input) { id name } }"
                )
                field = "triggerActivityLiveAdd"
            else:
                inp = {
                    "name": name,
                    "description": description or None,
                    "event_types": event_types or ["create", "update", "delete"],
                    "notifiers": notifier_ids or [],
                    "instance_trigger": bool(instance_trigger),
                    "filters": filters or None,
                    "recipients": recipients or None,
                }
                query = (
                    "mutation($input: TriggerLiveAddInput!) {"
                    "  triggerKnowledgeLiveAdd(input: $input) { id name } }"
                )
                field = "triggerKnowledgeLiveAdd"
            data, err = await _gql(query, {"input": inp})
            if err:
                return err
            t = data.get(field) or {}
            return f"Created live {kind} trigger '{t.get('name')}' (id: {t.get('id')})."

        if action == "create_digest":
            if not name or not trigger_ids:
                return "name and trigger_ids are required for action 'create_digest'."
            inp = {
                "name": name,
                "description": description or None,
                "trigger_ids": trigger_ids,
                "period": period,
                "trigger_time": trigger_time or None,
                "notifiers": notifier_ids or [],
            }
            if is_activity:
                if not recipients:
                    return "recipients are required for an activity digest."
                inp["recipients"] = recipients
                query = (
                    "mutation($input: TriggerActivityDigestAddInput!) {"
                    "  triggerActivityDigestAdd(input: $input) { id name } }"
                )
                field = "triggerActivityDigestAdd"
            else:
                inp["recipients"] = recipients or None
                query = (
                    "mutation($input: TriggerDigestAddInput!) {"
                    "  triggerKnowledgeDigestAdd(input: $input) { id name } }"
                )
                field = "triggerKnowledgeDigestAdd"
            data, err = await _gql(query, {"input": inp})
            if err:
                return err
            t = data.get(field) or {}
            return f"Created {kind} digest '{t.get('name')}' (id: {t.get('id')})."

        if action == "update":
            if not trigger_id:
                return "trigger_id is required for action 'update'."
            edits = []
            if name:
                edits.append({"key": "name", "value": [name]})
            if description:
                edits.append({"key": "description", "value": [description]})
            if filters:
                edits.append({"key": "filters", "value": [filters]})
            if notifier_ids is not None:
                edits.append({"key": "notifiers", "value": notifier_ids})
            if not edits:
                return "Provide name, description, filters, or notifier_ids to update."
            field = "triggerActivityFieldPatch" if is_activity else "triggerKnowledgeFieldPatch"
            query = (
                f"mutation($id: ID!, $input: [EditInput!]!) {{"
                f"  {field}(id: $id, input: $input) {{ id name }} }}"
            )
            _, err = await _gql(query, {"id": trigger_id, "input": edits})
            if err:
                return err
            return f"Updated {kind} trigger {trigger_id}."

        if action == "delete":
            if not trigger_id:
                return "trigger_id is required for action 'delete'."
            field = "triggerActivityDelete" if is_activity else "triggerKnowledgeDelete"
            query = f"mutation($id: ID!) {{ {field}(id: $id) }}"
            _, err = await _gql(query, {"id": trigger_id})
            if err:
                return err
            return f"Deleted {kind} trigger {trigger_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create_live, "
            "create_digest, update, delete."
        )

    # ── Notification inbox ─────────────────────────────────────────────
    async def manage_opencti_notification(
        action: str,
        notification_id: str = "",
        search: str = "",
        limit: int = _NOTIFICATION_DEFAULT_LIMIT,
        **extra: object,
    ) -> str:
        action = (action or "").strip().lower()
        refused = _refuse_unapplied(
            action,
            _NOTIFICATION_APPLIES,
            {
                "notification_id": notification_id,
                "search": search,
                "limit": None if limit == _NOTIFICATION_DEFAULT_LIMIT else limit,
            },
            dict(extra),
        )
        if refused:
            return refused

        if action == "list":
            query = (
                "query MyNotifications($first: Int, $search: String) {"
                "  myUnreadNotificationsCount"
                "  myNotifications(first: $first, search: $search,"
                "    orderBy: created, orderMode: desc) {"
                "    edges { node { id name notification_type is_read created } } } }"
            )
            data, err = await _gql(
                query, {"first": min(max(limit, 1), 200), "search": search or None}
            )
            if err:
                return err
            nodes = _edges(data, "myNotifications")
            unread = data.get("myUnreadNotificationsCount", 0)
            if not nodes:
                return f"No notifications. ({unread} unread)"
            lines = [f"Notifications ({len(nodes)} shown, {unread} unread):"]
            for n in nodes:
                flag = " " if n.get("is_read") else "*"
                lines.append(
                    f"{flag} [{n.get('notification_type')}] {n.get('name')} "
                    f"(id: {n.get('id')})"
                )
            return "\n".join(lines)

        if action in ("mark_read", "mark_unread"):
            if not notification_id:
                return f"notification_id is required for action '{action}'."
            read = action == "mark_read"
            query = (
                "mutation MarkRead($id: ID!, $read: Boolean!) {"
                "  notificationMarkRead(id: $id, read: $read) { id is_read } }"
            )
            data, err = await _gql(query, {"id": notification_id, "read": read})
            if err:
                return err
            n = data.get("notificationMarkRead") or {}
            return f"Notification {n.get('id')} marked {'read' if read else 'unread'}."

        if action == "delete":
            if not notification_id:
                return "notification_id is required for action 'delete'."
            query = (
                "mutation NotificationDelete($id: ID!) {"
                "  notificationDelete(id: $id) }"
            )
            _, err = await _gql(query, {"id": notification_id})
            if err:
                return err
            return f"Deleted notification {notification_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, mark_read, mark_unread, "
            "delete."
        )

    executor.register("manage_opencti_notifier", manage_opencti_notifier)
    executor.register("manage_opencti_trigger", with_status_notes(manage_opencti_trigger))
    executor.register("manage_opencti_notification", manage_opencti_notification)

    return [
        ToolDefinition(
            name="manage_opencti_notifier",
            description=(
                "Manage OpenCTI notifiers (notification delivery channels; requires "
                "SETTINGS_SETCUSTOMIZATION). Actions: 'list' (search), 'get' "
                "(notifier_id), 'create' (name + notifier_connector_id + "
                "notifier_configuration JSON), 'update' (notifier_id + fields), "
                "'delete' (notifier_id), 'test' (notifier_connector_id + "
                "notifier_configuration, optional notifier_test_id). Built-in "
                f"connector IDs -> {_BUILTIN_CONNECTORS}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "create", "update", "delete", "test"],
                    },
                    "notifier_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "notifier_connector_id": {"type": "string", "description": "Built-in or installed notifier connector ID."},
                    "notifier_configuration": {"type": "string", "description": "Connector configuration as a JSON string."},
                    "notifier_test_id": {
                        "type": "string",
                        "description": "Test sample: default_notification, default_digest, or default_activity.",
                        "default": "default_notification",
                    },
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencti_trigger",
            description=(
                "Manage OpenCTI triggers and digests. kind='knowledge' (default; "
                "alerts on STIX knowledge changes) or kind='activity' (security "
                "activity audit alerts, requires SETTINGS_SECURITYACTIVITY). Actions: "
                "'list', 'get' (trigger_id), 'create_live' (name; knowledge live also "
                "takes event_types [create/update/delete], instance_trigger, filters; "
                "activity live requires recipients), 'create_digest' (name + "
                "trigger_ids + period [hour/day/week/month] + optional trigger_time + "
                "notifier_ids), 'update' (trigger_id + name/description/filters/"
                "notifier_ids), 'delete' (trigger_id). 'filters' is a JSON STRING "
                "(serialized FilterGroup), not an object. notifier_ids and recipients "
                "are arrays of OpenCTI member/notifier IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list", "get", "create_live", "create_digest",
                            "update", "delete",
                        ],
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["knowledge", "activity"],
                        "default": "knowledge",
                    },
                    "trigger_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "event_types": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["create", "update", "delete"]},
                        "description": "Knowledge live trigger event types (default all).",
                    },
                    "instance_trigger": {"type": "boolean", "description": "Knowledge live: trigger on specific instances.", "default": False},
                    "filters": {"type": "string", "description": "JSON string of a FilterGroup (live triggers)."},
                    "notifier_ids": {"type": "array", "items": {"type": "string"}, "description": "Notifier IDs."},
                    "recipients": {"type": "array", "items": {"type": "string"}, "description": "Recipient member IDs (required for activity triggers)."},
                    "trigger_ids": {"type": "array", "items": {"type": "string"}, "description": "Source trigger IDs composing a digest (create_digest)."},
                    "period": {"type": "string", "enum": ["hour", "day", "week", "month"], "default": "day"},
                    "trigger_time": {"type": "string", "description": "Digest delivery time (e.g. '09:00' or weekday-time)."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencti_notification",
            description=(
                "Manage the authenticated user's OpenCTI notification inbox. Actions: "
                "'list' (recent notifications + unread count), 'mark_read' "
                "(notification_id), 'mark_unread' (notification_id), 'delete' "
                "(notification_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "mark_read", "mark_unread", "delete"],
                    },
                    "notification_id": {"type": "string"},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
