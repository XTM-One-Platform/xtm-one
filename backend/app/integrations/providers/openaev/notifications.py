# SPDX-License-Identifier: Apache-2.0

"""OpenAEV notification tools (the v3 rework of notification rules).

``manage_openaev_notification`` covers the three controllers that replaced the
deleted ``/api/notification-rules`` surface:

* ``/api/notifiers`` - the delivery channels (UI, EMAIL, WEBHOOK).
* ``/api/notification-triggers`` - what to notify about: an event-driven or
  periodic-digest trigger, optionally filtered, bound to notifiers.
* ``/api/notifications`` - the per-user inbox.

The old flat rule shape (``resource_id`` / ``resource_type`` / ``subject`` /
``trigger``) is gone; the trigger fields are ``notification_trigger_*`` and the
single ``trigger`` value became the ARRAY ``notification_trigger_event_types``.
"""

from __future__ import annotations

import json
import logging

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _api, _search_body, _validate_filter_group, json_answer

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"

#: ``NotificationTriggerEventType`` / ``NotificationTriggerType`` /
#: ``NotificationTriggerPeriod`` / ``NotifierType``.
_EVENT_TYPES = ["CREATE", "UPDATE", "DELETE", "SCORE_DEGRADATION"]
_TRIGGER_TYPES = ["LIVE", "DIGEST"]
_TRIGGER_PERIODS = ["HOUR", "DAY", "WEEK", "MONTH"]
_NOTIFIER_TYPES = ["UI", "EMAIL", "WEBHOOK"]


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def manage_openaev_notification(
        action: str = "",
        notifier_id: str = "",
        trigger_id: str = "",
        notification_id: str = "",
        name: str = "",
        description: str = "",
        notifier_type: str = "",
        trigger_type: str = "",
        event_types: list[str] | None = None,
        period: str = "",
        resource_type: str = "",
        instance_id: str = "",
        notifier_ids: list[str] | None = None,
        enabled: bool | None = None,
        filter_group: str = "",
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

        parsed_filter: dict | None = None
        if filter_group:
            try:
                parsed_filter = json.loads(filter_group)
            except ValueError as exc:
                return f"filter_group must be valid JSON: {exc}"
            filter_err = _validate_filter_group(parsed_filter)
            if filter_err:
                return f"filter_group is invalid. {filter_err}"

        def _enum(value: str, allowed: list[str], label: str) -> tuple[str, str]:
            if not value:
                return "", ""
            match = next((a for a in allowed if a.lower() == value.strip().lower()), None)
            if match is None:
                return "", f"{label} must be one of: {', '.join(allowed)} (got '{value}')."
            return match, ""

        # ── Notifiers (delivery channels) ─────────────────────────────────
        if action == "list_notifiers":
            resp, err = await _api(
                base_url, headers, "get", "/api/notifiers", "list notifiers"
            )
            if err:
                return err
            items = resp.json() or []
            if not items:
                return "No notifiers configured."
            lines = [f"Notifiers ({len(items)}):"]
            for n in items:
                built_in = " [built-in, read-only]" if n.get("notifier_built_in") else ""
                lines.append(
                    f"- {n.get('notifier_name', '?')} "
                    f"[{n.get('notifier_type', '?')}] "
                    f"(id: {n.get('notifier_id')}){built_in}"
                )
            return "\n".join(lines)

        if action == "get_notifier":
            if not notifier_id:
                return "notifier_id is required for 'get_notifier'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/notifiers/{notifier_id}",
                "get notifier",
            )
            if err:
                return err
            return json_answer(resp.json(), 2500)

        if action in ("create_notifier", "update_notifier"):
            body: dict = dict(extra)
            if name:
                body["notifier_name"] = name
            if description:
                body["notifier_description"] = description
            if notifier_type:
                value, verr = _enum(notifier_type, _NOTIFIER_TYPES, "notifier_type")
                if verr:
                    return verr
                body["notifier_type"] = value
            if action == "create_notifier":
                if not body.get("notifier_name") or not body.get("notifier_type"):
                    return (
                        "name and notifier_type are required for "
                        f"'create_notifier' (type one of: {', '.join(_NOTIFIER_TYPES)})."
                    )
                resp, err = await _api(
                    base_url, headers, "post", "/api/notifiers", "create notifier",
                    json=body,
                )
            else:
                if not notifier_id:
                    return "notifier_id is required for 'update_notifier'."
                if not body:
                    return (
                        "Provide at least one field to update (name, description, "
                        "notifier_type, or config)."
                    )
                resp, err = await _api(
                    base_url, headers, "put", f"/api/notifiers/{notifier_id}",
                    "update notifier", json=body,
                )
            if err:
                if "built" in err.lower():
                    return (
                        "Built-in notifiers are read-only in OpenAEV. Create a new "
                        "notifier instead of editing the built-in one. Original "
                        "error: " + err
                    )
                return err
            n = resp.json() if resp.content else {}
            verb = "Created" if action == "create_notifier" else "Updated"
            return f"{verb} notifier '{n.get('notifier_name', name)}' (id: {n.get('notifier_id', notifier_id)})."

        if action == "delete_notifier":
            if not notifier_id:
                return "notifier_id is required for 'delete_notifier'."
            _, err = await _api(
                base_url, headers, "delete", f"/api/notifiers/{notifier_id}",
                "delete notifier",
            )
            if err:
                return err
            return f"Deleted notifier {notifier_id}."

        if action == "test_notifier":
            if not notifier_id:
                return "notifier_id is required for 'test_notifier'."
            _, err = await _api(
                base_url, headers, "post", f"/api/notifiers/{notifier_id}/test",
                "test notifier",
            )
            if err:
                return err
            return f"Sent a test notification through notifier {notifier_id}."

        # ── Triggers (what to notify about) ───────────────────────────────
        if action == "list_triggers":
            resp, err = await _api(
                base_url, headers, "post", "/api/notification-triggers/search",
                "search notification triggers",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No notification triggers configured."
            lines = [f"Notification triggers ({len(items)}):"]
            for t in items:
                events = t.get("notification_trigger_event_types") or []
                state = "" if t.get("notification_trigger_enabled", True) else " [disabled]"
                lines.append(
                    f"- {t.get('notification_trigger_name', '?')} "
                    f"[{t.get('notification_trigger_type', '?')}] "
                    f"events: {', '.join(str(e) for e in events) or 'none'} "
                    f"(id: {t.get('notification_trigger_id')}){state}"
                )
            return "\n".join(lines)

        if action == "get_trigger":
            if not trigger_id:
                return "trigger_id is required for 'get_trigger'."
            resp, err = await _api(
                base_url, headers, "get",
                f"/api/notification-triggers/{trigger_id}", "get notification trigger",
            )
            if err:
                return err
            return json_answer(resp.json(), 2500)

        if action in ("create_trigger", "update_trigger"):
            body = dict(extra)
            if name:
                body["notification_trigger_name"] = name
            if trigger_type:
                value, verr = _enum(trigger_type, _TRIGGER_TYPES, "trigger_type")
                if verr:
                    return verr
                body["notification_trigger_type"] = value
            if event_types is not None:
                resolved = []
                for e in event_types:
                    value, verr = _enum(e, _EVENT_TYPES, "event_types")
                    if verr:
                        return verr
                    resolved.append(value)
                body["notification_trigger_event_types"] = resolved
            if period:
                value, verr = _enum(period, _TRIGGER_PERIODS, "period")
                if verr:
                    return verr
                body["notification_trigger_period"] = value
            if resource_type:
                body["notification_trigger_resource_type"] = resource_type
            if instance_id:
                body["notification_trigger_instance_id"] = instance_id
            if notifier_ids is not None:
                body["notification_trigger_notifiers"] = notifier_ids
            if parsed_filter is not None:
                body["notification_trigger_filters"] = parsed_filter
            if enabled is not None:
                body["notification_trigger_enabled"] = enabled
            if action == "create_trigger":
                if not body.get("notification_trigger_name"):
                    return "name is required for 'create_trigger'."
                resp, err = await _api(
                    base_url, headers, "post", "/api/notification-triggers",
                    "create notification trigger", json=body,
                )
            else:
                if not trigger_id:
                    return "trigger_id is required for 'update_trigger'."
                if not body:
                    return (
                        "Provide at least one field to update (name, trigger_type, "
                        "event_types, period, resource_type, instance_id, "
                        "notifier_ids, enabled, filter_group, or config)."
                    )
                resp, err = await _api(
                    base_url, headers, "put",
                    f"/api/notification-triggers/{trigger_id}",
                    "update notification trigger", json=body,
                )
            if err:
                return err
            t = resp.json() if resp.content else {}
            verb = "Created" if action == "create_trigger" else "Updated"
            return (
                f"{verb} notification trigger "
                f"'{t.get('notification_trigger_name', name)}' "
                f"(id: {t.get('notification_trigger_id', trigger_id)})."
            )

        if action == "delete_trigger":
            if not trigger_id:
                return "trigger_id is required for 'delete_trigger'."
            _, err = await _api(
                base_url, headers, "delete",
                f"/api/notification-triggers/{trigger_id}",
                "delete notification trigger",
            )
            if err:
                return err
            return f"Deleted notification trigger {trigger_id}."

        # ── Inbox ─────────────────────────────────────────────────────────
        if action == "inbox":
            resp, err = await _api(
                base_url, headers, "post", "/api/notifications/me/search",
                "search my notifications",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No notifications."
            lines = [f"My notifications ({len(items)}):"]
            for n in items:
                unread = "" if n.get("notification_is_read") else " [unread]"
                lines.append(
                    f"- {n.get('notification_name', '?')} "
                    f"({n.get('notification_created_at', '')})"
                    f"{unread} (id: {n.get('notification_id')})"
                )
                if n.get("notification_content"):
                    lines.append(f"    {str(n['notification_content'])[:300]}")
            return "\n".join(lines)

        if action == "unread_count":
            resp, err = await _api(
                base_url, headers, "get", "/api/notifications/me/unread-count",
                "get unread notification count",
            )
            if err:
                return err
            return f"Unread notifications: {json_answer(resp.json(), 200)}"

        if action == "mark_all_read":
            _, err = await _api(
                base_url, headers, "put", "/api/notifications/me/read-all",
                "mark all notifications read",
            )
            if err:
                return err
            return "Marked all notifications as read."

        if action == "delete_notification":
            if not notification_id:
                return "notification_id is required for 'delete_notification'."
            _, err = await _api(
                base_url, headers, "delete",
                f"/api/notifications/{notification_id}", "delete notification",
            )
            if err:
                return err
            return f"Deleted notification {notification_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list_notifiers, "
            "get_notifier, create_notifier, update_notifier, delete_notifier, "
            "test_notifier, list_triggers, get_trigger, create_trigger, "
            "update_trigger, delete_trigger, inbox, unread_count, mark_all_read, "
            "delete_notification."
        )

    executor.register("manage_openaev_notification", manage_openaev_notification)

    return [
        ToolDefinition(
            name="manage_openaev_notification",
            description=(
                "Manage OpenAEV notifications: NOTIFIERS (delivery channels - UI, "
                "EMAIL, WEBHOOK), NOTIFICATION TRIGGERS (what to notify about: "
                "LIVE per-event or DIGEST per-period, optionally filtered and "
                "scoped to one entity) and the per-user INBOX. This replaces the "
                "removed notification-rules API - note the single 'trigger' value "
                "became the array event_types (CREATE, UPDATE, DELETE, "
                "SCORE_DEGRADATION), where SCORE_DEGRADATION fires when a "
                "simulation's expectation scores get worse. Actions: "
                "'list_notifiers', 'get_notifier', 'create_notifier' (name + "
                "notifier_type), 'update_notifier', 'delete_notifier', "
                "'test_notifier' (built-in notifiers are read-only); "
                "'list_triggers', 'get_trigger', 'create_trigger' (name, "
                "trigger_type, event_types, notifier_ids, optional period / "
                "resource_type / instance_id / filter_group), 'update_trigger', "
                "'delete_trigger'; 'inbox', 'unread_count', 'mark_all_read', "
                "'delete_notification'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list_notifiers", "get_notifier", "create_notifier",
                            "update_notifier", "delete_notifier", "test_notifier",
                            "list_triggers", "get_trigger", "create_trigger",
                            "update_trigger", "delete_trigger", "inbox",
                            "unread_count", "mark_all_read", "delete_notification",
                        ],
                    },
                    "notifier_id": {"type": "string"},
                    "trigger_id": {"type": "string"},
                    "notification_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "notifier_type": {"type": "string", "enum": _NOTIFIER_TYPES},
                    "trigger_type": {
                        "type": "string",
                        "enum": _TRIGGER_TYPES,
                        "description": "LIVE notifies per event; DIGEST batches over a period.",
                    },
                    "event_types": {
                        "type": "array",
                        "items": {"type": "string", "enum": _EVENT_TYPES},
                        "description": "Which events fire the trigger. SCORE_DEGRADATION means expectation scores got worse.",
                    },
                    "period": {
                        "type": "string",
                        "enum": _TRIGGER_PERIODS,
                        "description": "Digest period (trigger_type=DIGEST).",
                    },
                    "resource_type": {"type": "string", "description": "Entity type the trigger watches."},
                    "instance_id": {"type": "string", "description": "Restrict the trigger to one entity instance."},
                    "notifier_ids": {"type": "array", "items": {"type": "string"}, "description": "Notifiers that deliver this trigger."},
                    "enabled": {"type": "boolean", "description": "Enable or pause a trigger. Omit to leave unchanged."},
                    "filter_group": {"type": "string", "description": "FilterGroup JSON narrowing which events match."},
                    "config": {"type": "string", "description": "JSON object of raw notifier_* / notification_trigger_* fields."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
