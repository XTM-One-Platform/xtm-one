# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The scheduling tools every agent carries.

``schedule_work_item``, ``list_scheduled_work_items``,
``cancel_scheduled_work_item`` and ``reschedule_scheduled_work_item`` are
built for every agent run by a signed-in person, whatever the agent's built-in
tool selection: they are ``core`` tools (``ToolDefinition.core``), which no
``tool_deny_list`` removes and the agent APIs refuse to deny
(:func:`refuse_core_tool_denials`).

They reach only the items the calling agent owns for the person it runs for.
Routing work to another agent, claiming and completing stay in the optional
``work_items`` group. The rules - the action, the time, ownership, audit -
are ``services/scheduled_work_items.py``'s; the run is
``services/scheduled_work_item_runner.py``'s.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.core.utils import format_exception_message
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

SCHEDULE_TOOL = "schedule_work_item"
LIST_TOOL = "list_scheduled_work_items"
CANCEL_TOOL = "cancel_scheduled_work_item"
RESCHEDULE_TOOL = "reschedule_scheduled_work_item"

#: The names no agent's deny list may name.
SCHEDULING_TOOL_NAMES: frozenset[str] = frozenset(
    {SCHEDULE_TOOL, LIST_TOOL, CANCEL_TOOL, RESCHEDULE_TOOL}
)
_SOURCE = "builtin:scheduling"


def refuse_core_tool_denials(deny_list: Iterable[str] | None) -> list[str] | None:
    """A ``tool_deny_list`` naming a scheduling tool is refused (a ``ValueError``)."""
    if deny_list is None:
        return None
    named = sorted({str(name) for name in deny_list} & SCHEDULING_TOOL_NAMES)
    if named:
        raise ValueError(
            f"{', '.join(named)} cannot be denied: every agent can schedule, list, "
            "cancel and reschedule its own one-off actions. An administrator turns "
            "scheduling off for the whole platform in Settings > Access and security "
            "(Scheduled work items)."
        )
    return list(deny_list)


#: The bounds of a deny list a caller writes.
MAX_TOOL_DENY_LIST_ENTRIES = 500
MAX_TOOL_DENY_LIST_NAME_LENGTH = 256


def normalize_tool_deny_list(deny_list: Iterable[str] | None) -> list[str] | None:
    """The deny list as stored, or a ``ValueError``: one rule for every writer.

    Names are trimmed (a padded name matches no tool and denies nothing),
    blanks and repeats dropped; a list or a name past the bounds, or a name
    of a scheduling tool (:func:`refuse_core_tool_denials`), is refused.
    ``None`` stays ``None``; the caller stores an empty result as NULL.
    """
    if deny_list is None:
        return None
    names = list(dict.fromkeys(str(name).strip() for name in deny_list if str(name).strip()))
    if len(names) > MAX_TOOL_DENY_LIST_ENTRIES:
        raise ValueError(
            f"A tool deny list holds at most {MAX_TOOL_DENY_LIST_ENTRIES} tool names."
        )
    if any(len(name) > MAX_TOOL_DENY_LIST_NAME_LENGTH for name in names):
        raise ValueError(
            f"A tool name in a deny list is at most {MAX_TOOL_DENY_LIST_NAME_LENGTH} characters."
        )
    return refuse_core_tool_denials(names)


async def _timezone(user_id: str) -> str | None:
    from sqlalchemy import select

    from app.core.database import async_session
    from app.core.utils import safe_uuid
    from app.models.user import User

    uid = safe_uuid(user_id)
    if uid is None:
        return None
    async with async_session() as db:
        return (
            await db.execute(select(User.timezone).where(User.id == uid))
        ).scalar_one_or_none()


#: The refusal of cancel / reschedule: the same words whether the item does not
#: exist or this agent may not change it.
_NOT_CHANGEABLE = (
    "`{work_item_id}` is not a scheduled work item this agent can change for this "
    "person: one it scheduled, one of an agent it hands work to, or a blocked or "
    "missed one. List its own with list_scheduled_work_items."
)


def _describe_item(item: dict, tz_name: str | None) -> str:
    from datetime import datetime

    from app.services.scheduled_work_items import describe_time

    scheduled = item.get("scheduled") or {}
    due = item.get("due_at")
    when = describe_time(datetime.fromisoformat(due), tz_name) if due else "no time"
    what = (
        f"exact {scheduled.get('tool')}"
        if scheduled.get("mode") == "exact"
        else "instruction"
    )
    target = f" -> {scheduled['target']}" if scheduled.get("target") else ""
    preview = f' "{scheduled["preview"]}"' if scheduled.get("preview") else ""
    line = (
        f"- `{item['id']}` **{item['title']}** - {scheduled.get('state')} - {when} - "
        f"{what}{target}{preview}"
    )
    if scheduled.get("state") in ("done", "blocked", "missed") and item.get("result"):
        line += f"\n  outcome: {str(item['result'])[:300]}"
    return line


def build_scheduling_tools(
    *,
    user_id: str,
    agent_id: str,
    agent_name: str,
    conversation_id: str | None = None,
    pinned_integrations: dict[str, str] | None = None,
    item_sink: list[str] | None = None,
    caller_is_person: bool = False,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The four scheduling tools of *agent_id*, acting for *user_id*.

    *caller_is_person* is true in a turn a person asked in chat: the default
    of ``notify``. A one-off an agent schedules during a run (an assignment
    run, a background task) is silent by default: its outcome is posted to
    the conversation and listed, never sent to the person's bell unless the
    call sets ``notify`` or ``escalate_to_user``. In the run of a scheduled
    one-off, the default is that one-off's own ``notify``
    (``scheduled_work_items.default_notify``).

    *pinned_integrations* are the integrations an agent account's turn speaks
    through: recorded on the action by the platform, so the run later
    reaches the platform as that same account. *item_sink* collects the items
    this turn scheduled or rescheduled; the turn appends their card markers to
    its answer (``scheduled_work_items.append_markers``).
    """
    from app.services import scheduled_work_items as swi

    def _sink(item: dict) -> None:
        if item_sink is not None and item.get("id") not in item_sink:
            item_sink.append(item["id"])

    async def _schedule(
        title: str,
        due_at: str,
        mode: str,
        tool: str = "",
        arguments: Any = None,
        prompt: str = "",
        target: str = "",
        description: str = "",
        notify: bool | None = None,
        escalate_to_user: bool = False,
    ) -> str:
        if not swi.scheduling_enabled():
            return f"Error: {swi.SCHEDULING_DISABLED_MESSAGE} Nothing was scheduled."
        tz_name = await _timezone(user_id)
        try:
            due = swi.parse_due_at(due_at, tz_name=tz_name)
            action = swi.build_action(
                mode,
                tool=tool,
                arguments=arguments,
                prompt=prompt,
                target=target,
                notify=swi.default_notify(caller_is_person) if notify is None else bool(notify),
                escalate_to_user=bool(escalate_to_user),
            )
            if pinned_integrations:
                action["pinned_integrations"] = dict(pinned_integrations)
            if action["mode"] == "exact":
                from sqlalchemy import select

                from app.core.database import async_session
                from app.models.agent import Agent
                from app.services.scheduled_work_item_runner import check_exact_action

                async with async_session() as db:
                    agent = (
                        await db.execute(select(Agent).where(Agent.id == agent_id))
                    ).scalar_one()
                    action, refusal = await check_exact_action(db, agent, user_id, action)
                if refusal:
                    return f"Error: {refusal}"
            item = await swi.create_scheduled_work_item(
                user_id=user_id,
                agent_id=agent_id,
                agent_name=agent_name,
                title=title,
                due=due,
                action=action,
                description=description or None,
                conversation_id=conversation_id,
            )
        except swi.ScheduledWorkItemError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            logger.error("schedule_work_item failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: the work item could not be scheduled. Nothing was scheduled; try again."
        _sink(item)
        lines = [
            f"Scheduled **{item['title']}** (id `{item['id']}`) for "
            f"{swi.describe_time(due.at, tz_name)}.",
            _describe_item(item, tz_name),
            "It runs as the person who asked, on this agent, with no assignment. Its "
            "card in the conversation shows the time and can cancel or reschedule it.",
        ]
        note = swi.offset_note(due.given, tz_name)
        if note:
            lines.append(note)
        return "\n".join(lines)

    async def _list(state: str = "upcoming") -> str:
        tz_name = await _timezone(user_id)
        try:
            items = await swi.list_scheduled_work_items(
                user_id=user_id, agent_id=agent_id, state=state or "upcoming"
            )
        except swi.ScheduledWorkItemError as exc:
            return f"Error: {exc}"
        head = (
            ""
            if swi.scheduling_enabled()
            else f"{swi.SCHEDULING_DISABLED_MESSAGE} Nothing scheduled will run until it is turned back on.\n"
        )
        if not items:
            return f"{head}No {state or 'upcoming'} scheduled work items on this agent."
        return head + "\n".join(
            [f"{len(items)} scheduled work item(s) ({state or 'upcoming'}):"]
            + [_describe_item(item, tz_name) for item in items]
        )

    async def _cancel(work_item_id: str) -> str:
        try:
            item = await swi.cancel_scheduled_work_item(
                work_item_id, user_id=user_id, agent_id=agent_id
            )
        except swi.ScheduledWorkItemError as exc:
            return f"Error: {exc}"
        if item is None:
            return f"Error: {_NOT_CHANGEABLE.format(work_item_id=work_item_id)}"
        return f"Cancelled **{item['title']}** (`{item['id']}`): nothing will run."

    async def _reschedule(work_item_id: str, due_at: str) -> str:
        if not swi.scheduling_enabled():
            return f"Error: {swi.SCHEDULING_DISABLED_MESSAGE} Nothing was rescheduled."
        tz_name = await _timezone(user_id)
        try:
            due = swi.parse_due_at(due_at, tz_name=tz_name)
            item = await swi.reschedule_scheduled_work_item(
                work_item_id, due, user_id=user_id, agent_id=agent_id
            )
        except swi.ScheduledWorkItemError as exc:
            return f"Error: {exc}"
        if item is None:
            return f"Error: {_NOT_CHANGEABLE.format(work_item_id=work_item_id)}"
        _sink(item)
        lines = [
            f"Rescheduled **{item['title']}** (`{item['id']}`) to "
            f"{swi.describe_time(due.at, tz_name)}."
        ]
        note = swi.offset_note(due.given, tz_name)
        if note:
            lines.append(note)
        return "\n".join(lines)

    executor = CallableToolExecutor()
    executor.register(SCHEDULE_TOOL, _schedule)
    executor.register(LIST_TOOL, _list)
    executor.register(CANCEL_TOOL, _cancel)
    executor.register(RESCHEDULE_TOOL, _reschedule)

    due_at_help = (
        "ISO-8601 date and time WITH its UTC offset, e.g. 2026-10-08T09:00:00+02:00 "
        "(a time without an offset is refused, never read as UTC). Use the offset of "
        "the requester's time zone on that date."
    )
    definitions = [
        ToolDefinition(
            name=SCHEDULE_TOOL,
            description=(
                "Schedule a ONE-OFF action this agent runs by itself at a given time, "
                "as the person asking (no assignment needed; recurring work is an "
                "assignment). mode 'exact' runs one of this agent's tools exactly as "
                "given, with no model: the arguments are sent byte for byte, so put "
                "the final content in them. mode 'instruction' runs a prompt at that "
                "time. The outcome is posted back to this conversation. notify: "
                "whether the outcome also reaches the person's notification bell; "
                "default true when a person asked in chat, false in an agent run "
                "(assignment run, background task); in the run of a scheduled "
                "one-off, that one-off's own notify. escalate_to_user: true notifies "
                "the person whatever notify says, for an outcome that needs them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short label, e.g. 'Post the EMT reminder'"},
                    "due_at": {"type": "string", "description": due_at_help},
                    "mode": {"type": "string", "enum": ["exact", "instruction"]},
                    "tool": {"type": "string", "description": "exact: the name of the tool to run"},
                    "arguments": {"type": "object", "description": "exact: the tool's arguments, final"},
                    "prompt": {"type": "string", "description": "instruction: what to do at that time"},
                    "target": {"type": "string", "description": "Where it goes, as the person would say it (e.g. 'EMT channel'), shown on the card"},
                    "description": {"type": "string", "description": "Optional notes"},
                    "notify": {"type": "boolean", "description": "Outcome to the person's bell too (default: true in a person's chat, false in an agent run)"},
                    "escalate_to_user": {"type": "boolean", "description": "The outcome needs the person: notify whatever notify says (default false)"},
                },
                "required": ["title", "due_at", "mode"],
            },
            source=_SOURCE,
            priority=0,
            core=True,
        ),
        ToolDefinition(
            name=LIST_TOOL,
            description=(
                "List the one-off actions this agent scheduled for the person asking, "
                "with their state and time."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": list(swi.LIST_STATES),
                        "description": "Default upcoming",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
            core=True,
        ),
        ToolDefinition(
            name=CANCEL_TOOL,
            description=(
                "Cancel a scheduled one-off action before it runs: nothing runs and no "
                "run is created. Also one an agent you hand work to scheduled, or a "
                "blocked or missed one of the person's agents."
            ),
            input_schema={
                "type": "object",
                "properties": {"work_item_id": {"type": "string"}},
                "required": ["work_item_id"],
            },
            source=_SOURCE,
            priority=0,
            core=True,
        ),
        ToolDefinition(
            name=RESCHEDULE_TOOL,
            description=(
                "Move a scheduled one-off action that has not run (waiting, blocked "
                "or missed) to a new time."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "work_item_id": {"type": "string"},
                    "due_at": {"type": "string", "description": due_at_help},
                },
                "required": ["work_item_id", "due_at"],
            },
            source=_SOURCE,
            priority=0,
            core=True,
        ),
    ]
    return definitions, executor
