# SPDX-License-Identifier: Apache-2.0

"""The HubSpot note and task tools: write a note or a task on a CRM record.

A note or a task is an engagement on a record's timeline. Each tool creates the
engagement then associates it with the record through HubSpot's default
association, the way ``upload_hubspot_attachment`` attaches its note.
``hubspot.build_tools`` builds one :class:`HubSpotNotes` per toolbox around its
rate-limited request function and adds each tool's scopes to its description;
the object-type Scope(s) guard checks ``object_type`` before any request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.providers.hubspot_records import parse_hubspot_time
from app.integrations.providers.hubspot_scope import api_object_type
from app.tools.base import ToolDefinition

_BASE_URL = "https://api.hubapi.com"

#: The rate-limited request of ``hubspot.build_tools`` (``safe_request``'s contract).
Request = Callable[..., Awaitable[tuple[httpx.Response | None, str | None]]]

#: The task values HubSpot accepts.
TASK_PRIORITIES = ("LOW", "MEDIUM", "HIGH")
TASK_TYPES = ("TODO", "CALL", "EMAIL", "LINKED_IN", "MEETING")
TASK_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "WAITING", "COMPLETED", "DEFERRED")


def _body(resp: httpx.Response | None) -> dict[str, Any]:
    if resp is None:
        return {}
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _choice(value: str, allowed: tuple[str, ...], argument: str) -> tuple[str, str | None]:
    """*value* upper-cased when it is one of *allowed* (empty stays empty), else an error."""
    text = str(value or "").strip().upper()
    if text and text not in allowed:
        return "", f"Error: {argument} must be one of {', '.join(allowed)}."
    return text, None


def _timestamp(value: str, argument: str) -> tuple[str, str | None]:
    """An ISO-8601 UTC timestamp for *value* (now when empty), or an error naming *argument*."""
    if not str(value or "").strip():
        return datetime.now(UTC).isoformat(), None
    parsed = parse_hubspot_time(value)
    if parsed is None:
        return "", f"Error: {argument} must be a date or an ISO-8601 date and time (e.g. 2026-12-01T09:00:00Z)."
    return parsed.isoformat(), None


class HubSpotNotes:
    """The note and task tools of one toolbox."""

    def __init__(self, request: Request, *, headers: Mapping[str, str]) -> None:
        self._request = request
        self._headers = dict(headers)

    async def _call(
        self, client: httpx.AsyncClient, method: str, path: str, action: str, **kwargs: Any
    ) -> tuple[httpx.Response | None, str | None]:
        return await self._request(
            client, method, f"{_BASE_URL}{path}", "HubSpot", action, headers=self._headers, **kwargs
        )

    async def _create_on_record(
        self, kind: str, object_type: str, record_id: str, properties: dict[str, Any]
    ) -> tuple[str, str | None]:
        """Create one engagement of *kind* (notes, tasks) and associate it with the record."""
        noun = kind[:-1]
        async with egress_client(timeout=30) as client:
            resp, err = await self._call(
                client,
                "post",
                f"/crm/v3/objects/{kind}",
                f"create a {noun} on {object_type} {record_id}",
                json={"properties": properties},
            )
            if err:
                return "", err
            engagement_id = str(_body(resp).get("id") or "")
            if not engagement_id:
                return "", f"Error: HubSpot created the {noun} but returned no id."
            _, err = await self._call(
                client,
                "put",
                f"/crm/v4/objects/{kind}/{engagement_id}/associations/default/{object_type}/{record_id}",
                f"associate {noun} {engagement_id} with {object_type} {record_id}",
            )
            if err:
                return engagement_id, (
                    f"{err}\n({noun.capitalize()} {engagement_id} was created but is not associated with "
                    f"{object_type} {record_id}: create_hubspot_association links it.)"
                )
        return engagement_id, None

    async def create_hubspot_note(self, object_type: str, record_id: str, body: str, timestamp: str = "") -> str:
        """Write a note on a CRM record's timeline."""
        object_type = api_object_type(object_type)
        record_id = str(record_id or "").strip()
        text = str(body or "").strip()
        if not object_type or not record_id:
            return "Error: pass object_type and record_id."
        if not text:
            return "Error: pass body, the text of the note."
        when, err = _timestamp(timestamp, "timestamp")
        if err:
            return err
        note_id, err = await self._create_on_record(
            "notes", object_type, record_id, {"hs_note_body": text, "hs_timestamp": when}
        )
        if err:
            return err
        return f"Created note {note_id} on {object_type} {record_id} (timestamp {when})."

    async def create_hubspot_task(
        self,
        object_type: str,
        record_id: str,
        subject: str,
        body: str = "",
        due_date: str = "",
        owner_id: str = "",
        priority: str = "",
        task_type: str = "",
        status: str = "",
    ) -> str:
        """Create a task on a CRM record, optionally assigned to an owner with a due date."""
        object_type = api_object_type(object_type)
        record_id = str(record_id or "").strip()
        title = str(subject or "").strip()
        if not object_type or not record_id:
            return "Error: pass object_type and record_id."
        if not title:
            return "Error: pass subject, the title of the task."
        due, err = _timestamp(due_date, "due_date")
        if err:
            return err
        props: dict[str, Any] = {"hs_task_subject": title, "hs_timestamp": due}
        for value, allowed, argument, prop in (
            (priority, TASK_PRIORITIES, "priority", "hs_task_priority"),
            (task_type, TASK_TYPES, "task_type", "hs_task_type"),
            (status, TASK_STATUSES, "status", "hs_task_status"),
        ):
            chosen, err = _choice(value, allowed, argument)
            if err:
                return err
            if chosen:
                props[prop] = chosen
        if str(body or "").strip():
            props["hs_task_body"] = str(body).strip()
        if str(owner_id or "").strip():
            props["hubspot_owner_id"] = str(owner_id).strip()
        task_id, err = await self._create_on_record("tasks", object_type, record_id, props)
        if err:
            return err
        owner = f", owner {props['hubspot_owner_id']}" if "hubspot_owner_id" in props else ""
        return f"Created task {task_id} '{title}' on {object_type} {record_id} (due {due}{owner})."

    def handlers(self) -> dict[str, Callable[..., Awaitable[Any]]]:
        return {
            "create_hubspot_note": self.create_hubspot_note,
            "create_hubspot_task": self.create_hubspot_task,
        }


_RECORD_ARGUMENTS: dict[str, Any] = {
    "object_type": {"type": "string", "description": "Record type: contacts, companies, deals or tickets"},
    "record_id": {"type": "string", "description": "Record ID"},
}


def note_definitions(source: str) -> list[ToolDefinition]:
    """The note and task tools' definitions (``hubspot.build_tools`` adds their scopes)."""
    return [
        ToolDefinition(
            name="create_hubspot_note",
            description="Write a note on a HubSpot record's timeline (contact, company, deal or ticket).",
            input_schema={
                "type": "object",
                "properties": {
                    **_RECORD_ARGUMENTS,
                    "body": {"type": "string", "description": "Note text (HTML allowed)"},
                    "timestamp": {"type": "string", "description": "Note date, ISO-8601 (default now)", "default": ""},
                },
                "required": ["object_type", "record_id", "body"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="create_hubspot_task",
            description=(
                "Create a task on a HubSpot record (contact, company, deal or ticket), optionally assigned to an "
                "owner with a due date."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_RECORD_ARGUMENTS,
                    "subject": {"type": "string", "description": "Task title"},
                    "body": {"type": "string", "description": "Task notes", "default": ""},
                    "due_date": {"type": "string", "description": "Due date, ISO-8601 (default now)", "default": ""},
                    "owner_id": {"type": "string", "description": "Owner ID (list_hubspot_owners)", "default": ""},
                    "priority": {"type": "string", "enum": ["", *TASK_PRIORITIES], "default": ""},
                    "task_type": {"type": "string", "enum": ["", *TASK_TYPES], "default": ""},
                    "status": {"type": "string", "enum": ["", *TASK_STATUSES], "default": ""},
                },
                "required": ["object_type", "record_id", "subject"],
            },
            source=source,
            priority=1,
        ),
    ]
