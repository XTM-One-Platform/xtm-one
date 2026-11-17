"""Microsoft Purview Records Management / Data Lifecycle - retention labels.

Retention labels (``/security/labels/retentionLabels``) configure how long
Microsoft 365 content is retained and what happens at the end of the period
(retain, delete, start a disposition review, relabel). Event-based retention
adds *retention event types* (``/security/triggerTypes/retentionEventTypes``)
and *retention events* (``/security/triggers/retentionEvents``) that start a
retention clock when a real-world event occurs (e.g. an employee leaves).

Microsoft Graph only exposes these APIs to **delegated** (signed-in user)
credentials - application (app-only) permissions are not supported - so every
tool short-circuits with an actionable message in app-only mode.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import DELEGATED_ONLY_HINT, PurviewClient, dt, fmt_list, safe_json, trunc

SRC = "integration:purview"
_LABELS = "/security/labels/retentionLabels"
_EVENT_TYPES = "/security/triggerTypes/retentionEventTypes"
_EVENTS = "/security/triggers/retentionEvents"


def _retention_duration(label: dict[str, Any]) -> str:
    dur = label.get("retentionDuration") or {}
    days = dur.get("days")
    return f"{days} days" if days is not None else "unlimited"


def _fmt_label(label: dict[str, Any]) -> str:
    in_use = "in use" if label.get("isInUse") else "unused"
    return (
        f"- {label.get('displayName', 'Unnamed')} ({in_use})\n"
        f"  id: {label.get('id', '?')} | behavior: "
        f"{label.get('behaviorDuringRetentionPeriod', '?')} | after: "
        f"{label.get('actionAfterRetentionPeriod', '?')}\n"
        f"  trigger: {label.get('retentionTrigger', '?')} | duration: "
        f"{_retention_duration(label)} | {trunc(label.get('descriptionForAdmins'), 120)}"
    )


def _fmt_event_type(t: dict[str, Any]) -> str:
    return (
        f"- {t.get('displayName', 'Unnamed')} | id: {t.get('id', '?')}\n"
        f"  {trunc(t.get('description'), 140)} | created: {dt(t.get('createdDateTime'))}"
    )


def _fmt_event(e: dict[str, Any]) -> str:
    return (
        f"- {e.get('displayName', 'Unnamed')} | id: {e.get('id', '?')}\n"
        f"  triggered: {dt(e.get('eventTriggerDateTime'))} | "
        f"{trunc(e.get('description'), 140)}"
    )


def register(
    client: PurviewClient, executor: CallableToolExecutor
) -> list[ToolDefinition]:
    def _guard() -> str | None:
        return DELEGATED_ONLY_HINT if client.app_only else None

    async def list_purview_retention_labels(top: int = 50, skiptoken: str = "") -> str:
        if guard := _guard():
            return guard
        params: dict[str, Any] = {}
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.graph_paged(
            _LABELS, "list retention labels", top=min(top, 100), params=params
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_label,
            noun="retention label",
            skiptoken=next_tok,
            empty_msg="No retention labels are configured.",
        )

    async def get_purview_retention_label(label_id: str) -> str:
        if guard := _guard():
            return guard
        data, err = await client.graph_get_json(
            f"{_LABELS}/{quote(label_id.strip(), safe='')}",
            "get retention label",
            params={"$expand": "retentionEventType"},
        )
        if err:
            return err
        return _fmt_label(data or {})

    async def create_purview_retention_label(
        display_name: str,
        behavior_during_retention_period: str = "retain",
        action_after_retention_period: str = "none",
        retention_trigger: str = "dateCreated",
        retention_days: int = 0,
        description_for_admins: str = "",
        description_for_users: str = "",
    ) -> str:
        if guard := _guard():
            return guard
        body: dict[str, Any] = {
            "displayName": display_name,
            "behaviorDuringRetentionPeriod": behavior_during_retention_period,
            "actionAfterRetentionPeriod": action_after_retention_period,
            "retentionTrigger": retention_trigger,
        }
        if retention_days > 0:
            body["retentionDuration"] = {
                "@odata.type": "#microsoft.graph.security.retentionDurationInDays",
                "days": retention_days,
            }
        if description_for_admins:
            body["descriptionForAdmins"] = description_for_admins
        if description_for_users:
            body["descriptionForUsers"] = description_for_users
        resp, err = await client.graph_request(
            "post", _LABELS, "create retention label", json=body
        )
        if err:
            return err
        label = safe_json(resp)
        return (
            f"Created retention label '{label.get('displayName', display_name)}' "
            f"(id: {label.get('id', '?')})."
        )

    async def manage_purview_retention_label(
        action: str,
        label_id: str,
        display_name: str = "",
        description_for_admins: str = "",
        description_for_users: str = "",
    ) -> str:
        if guard := _guard():
            return guard
        lid = quote(label_id.strip(), safe="")
        if action == "update":
            patch: dict[str, Any] = {}
            if display_name:
                patch["displayName"] = display_name
            if description_for_admins:
                patch["descriptionForAdmins"] = description_for_admins
            if description_for_users:
                patch["descriptionForUsers"] = description_for_users
            if not patch:
                return "Error: provide display_name or a description to update."
            _, err = await client.graph_request(
                "patch", f"{_LABELS}/{lid}", "update retention label", json=patch
            )
            return err or f"Updated retention label {label_id}."
        if action == "delete":
            _, err = await client.graph_request(
                "delete", f"{_LABELS}/{lid}", "delete retention label"
            )
            return err or (
                f"Deleted retention label {label_id}. This is permanent; labels "
                f"currently applied to content cannot be deleted."
            )
        return "Error: unknown action. Valid actions: update, delete."

    async def list_purview_retention_event_types(
        top: int = 50, skiptoken: str = ""
    ) -> str:
        if guard := _guard():
            return guard
        params: dict[str, Any] = {}
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.graph_paged(
            _EVENT_TYPES, "list retention event types", top=min(top, 100), params=params
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_event_type,
            noun="retention event type",
            skiptoken=next_tok,
            empty_msg="No retention event types are configured.",
        )

    async def list_purview_retention_events(top: int = 50, skiptoken: str = "") -> str:
        if guard := _guard():
            return guard
        params: dict[str, Any] = {}
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.graph_paged(
            _EVENTS, "list retention events", top=min(top, 100), params=params
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_event,
            noun="retention event",
            skiptoken=next_tok,
            empty_msg="No retention events have been triggered.",
        )

    async def manage_purview_retention_event(
        action: str,
        event_id: str = "",
        display_name: str = "",
        description: str = "",
        event_type_id: str = "",
        event_trigger_date: str = "",
    ) -> str:
        if guard := _guard():
            return guard
        if action == "get":
            if not event_id:
                return "Error: event_id is required for action 'get'."
            data, err = await client.graph_get_json(
                f"{_EVENTS}/{quote(event_id.strip(), safe='')}", "get retention event"
            )
            return err or _fmt_event(data or {})
        if action == "create":
            if not (display_name and event_type_id):
                return (
                    "Error: display_name and event_type_id are required to create a "
                    "retention event (use list_purview_retention_event_types for the id)."
                )
            body: dict[str, Any] = {
                "displayName": display_name,
                "retentionEventType": {"id": event_type_id.strip()},
            }
            if description:
                body["description"] = description
            if event_trigger_date:
                body["eventTriggerDateTime"] = event_trigger_date.strip()
            resp, err = await client.graph_request(
                "post", _EVENTS, "create retention event", json=body
            )
            if err:
                return err
            evt = safe_json(resp)
            return (
                f"Created retention event '{evt.get('displayName', display_name)}' "
                f"(id: {evt.get('id', '?')}). Content with matching event-based "
                f"retention labels now starts its retention clock."
            )
        return "Error: unknown action. Valid actions: get, create."

    handlers = {
        "list_purview_retention_labels": list_purview_retention_labels,
        "get_purview_retention_label": get_purview_retention_label,
        "create_purview_retention_label": create_purview_retention_label,
        "manage_purview_retention_label": manage_purview_retention_label,
        "list_purview_retention_event_types": list_purview_retention_event_types,
        "list_purview_retention_events": list_purview_retention_events,
        "manage_purview_retention_event": manage_purview_retention_event,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    return [
        ToolDefinition(
            name="list_purview_retention_labels",
            description=(
                "List the Microsoft Purview retention labels (Data Lifecycle / "
                "Records Management) - the policies that control how long content "
                "is retained and what happens afterwards. Returns each label's "
                "retention behavior, action after the period, trigger, and "
                "duration. Requires delegated (signed-in) access."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {"type": "string"},
                },
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="get_purview_retention_label",
            description=(
                "Get one retention label by id, including its full retention "
                "settings and any associated retention event type. Requires "
                "delegated access."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "label_id": {"type": "string", "description": "The retention label id."}
                },
                "required": ["label_id"],
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="create_purview_retention_label",
            description=(
                "Create a retention label. behavior_during_retention_period is one "
                "of doNotRetain/retain/retainAsRecord/retainAsRegulatoryRecord; "
                "action_after_retention_period is none/delete/startDispositionReview/"
                "relabel; retention_trigger is dateLabeled/dateCreated/dateModified/"
                "dateOfEvent; retention_days sets the duration. Requires delegated "
                "access."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "display_name": {"type": "string"},
                    "behavior_during_retention_period": {
                        "type": "string",
                        "enum": [
                            "doNotRetain",
                            "retain",
                            "retainAsRecord",
                            "retainAsRegulatoryRecord",
                        ],
                        "default": "retain",
                    },
                    "action_after_retention_period": {
                        "type": "string",
                        "enum": ["none", "delete", "startDispositionReview", "relabel"],
                        "default": "none",
                    },
                    "retention_trigger": {
                        "type": "string",
                        "enum": ["dateLabeled", "dateCreated", "dateModified", "dateOfEvent"],
                        "default": "dateCreated",
                    },
                    "retention_days": {
                        "type": "integer",
                        "description": "Retention duration in days (0 = unlimited).",
                        "default": 0,
                    },
                    "description_for_admins": {"type": "string"},
                    "description_for_users": {"type": "string"},
                },
                "required": ["display_name"],
            },
            source=SRC,
            priority=1,
        ),
        ToolDefinition(
            name="manage_purview_retention_label",
            description=(
                "Update or delete a retention label. action='update' changes the "
                "display_name / descriptions; action='delete' permanently removes "
                "the label (labels applied to content cannot be deleted). Requires "
                "delegated access."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["update", "delete"]},
                    "label_id": {"type": "string"},
                    "display_name": {"type": "string"},
                    "description_for_admins": {"type": "string"},
                    "description_for_users": {"type": "string"},
                },
                "required": ["action", "label_id"],
            },
            source=SRC,
            priority=2,
        ),
        ToolDefinition(
            name="list_purview_retention_event_types",
            description=(
                "List retention event types - the named categories of real-world "
                "events (e.g. 'Employee departure', 'Contract expiration') that can "
                "trigger event-based retention. Use the returned id when creating a "
                "retention event. Requires delegated access."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {"type": "string"},
                },
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="list_purview_retention_events",
            description=(
                "List the retention events that have been triggered - each starts "
                "the retention clock for content tagged with a matching event-based "
                "retention label. Requires delegated access."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {"type": "string"},
                },
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="manage_purview_retention_event",
            description=(
                "Manage retention events. action='get' reads one event by event_id; "
                "action='create' triggers a new event (needs display_name and "
                "event_type_id from list_purview_retention_event_types, optional "
                "event_trigger_date ISO 8601). Requires delegated access."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "create"]},
                    "event_id": {"type": "string"},
                    "display_name": {"type": "string"},
                    "description": {"type": "string"},
                    "event_type_id": {"type": "string"},
                    "event_trigger_date": {
                        "type": "string",
                        "description": "ISO 8601 event date, e.g. 2026-06-01T00:00:00Z.",
                    },
                },
                "required": ["action"],
            },
            source=SRC,
            priority=1,
        ),
    ]
