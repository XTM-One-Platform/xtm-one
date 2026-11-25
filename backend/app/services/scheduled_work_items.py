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

"""Scheduled work items: one-off actions an agent runs at a set time.

A user asks an agent "post this in the EMT channel tomorrow at 9:00". The
agent schedules a work item whose ``payload.action`` is executable, and the
platform runs it at ``due_at`` - no assignment, no flow, no new object: the
item is an ordinary ``WorkItem`` and its execution an ordinary assignment-less
``AssignmentRun``. This module owns the rules every surface shares (the
always-on tools in ``builtin_tools/scheduling.py``, the REST routes in
``api/v1/work_items.py``, the dispatcher and executor in
``scheduled_work_item_runner.py``):

* **The action.** ``payload.action`` is ``{"mode": "exact", "tool", "arguments"}``
  (that exact tool call, no LLM) or ``{"mode": "instruction", "prompt"}`` (the
  owner agent runs the prompt in a background run). ``action_mode`` mirrors the
  mode and is written HERE alone (:func:`create_scheduled_work_item`,
  :func:`edit_scheduled_action`), so a generic write can never make an item
  executable (:func:`refuse_generic_action_write`).
* **Ownership.** An item is owned by the agent that scheduled it and acts for
  the person who asked (``user_id``). It is private to that person, whatever
  the agent's tier: it runs with their identity and shows their content. An
  agent's tools reach only the items it owns for the person it runs for.
* **Time.** ``due_at`` must carry its UTC offset; a time without one is never
  read as UTC (:func:`parse_due_at`). Every message states the time in the
  person's time zone and in UTC (:func:`describe_time`), and an offset that is
  not the person's zone on that date (a daylight saving change) is said
  (:func:`offset_note`).
* **State.** The item follows its run: ``open`` (scheduled) -> ``claimed`` ->
  ``in_progress`` -> ``done`` with the proof, or ``blocked`` with the reason
  (``outcome_code``); ``cancelled`` before ``due_at``. :func:`schedule_state`
  derives what the views show, ``missed`` included: an item the platform could
  not dispatch within the grace window is missed, never "stale".
* **Missed rule.** Dispatched up to ``SCHEDULED_WORK_ITEM_GRACE_MINUTES`` late,
  never later: past that window the item is blocked as missed and its creator
  notified, so nothing is posted at a time nobody chose. Rescheduling it is one
  click (or one tool call).
* **Audit.** Creation (with the frozen action), cancel, reschedule and every
  edit of the action are written to the audit log, entity ``work_item``.
* **Kill switch.** ``scheduled_work_items_enabled`` (Settings > Access and security,
  ``SCHEDULED_WORK_ITEMS_ENABLED``): off, the tools answer
  :data:`SCHEDULING_DISABLED_MESSAGE` and nothing is dispatched.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, or_, select

from app.core.config import settings
from app.core.security_options import security_option
from app.core.utils import format_exception_message, safe_uuid
from app.models.work_item import WORK_ITEM_ACTION_MODES, WorkItem

logger = logging.getLogger(__name__)

#: ``AssignmentRun.trigger_payload["trigger_type"]`` / ``trigger_context["type"]``
#: of a scheduled run, what Executions and Activity label "Scheduled".
SCHEDULED_TRIGGER_TYPE = "scheduled_work_item"
#: The orchestration adapter that runs a scheduled item.
ADAPTER_NAME = "scheduled_work_item"

SCHEDULING_DISABLED_MESSAGE = "Scheduling is disabled by the administrator."

#: Why a scheduled item is ``blocked`` (``WorkItem.outcome_code``).
OUTCOME_MISSED = "missed"
OUTCOME_DISABLED = "disabled"
OUTCOME_AGENT_UNAVAILABLE = "agent_unavailable"
OUTCOME_OWNER_DEACTIVATED = "owner_deactivated"
OUTCOME_PERMISSION = "permission"
OUTCOME_AI_UNAVAILABLE = "ai_unavailable"
OUTCOME_BINDING = "binding"
OUTCOME_VALIDATION = "validation"
OUTCOME_APPROVAL = "approval_required"
OUTCOME_EXECUTION = "execution_error"
OUTCOME_QUOTA = "quota"
OUTCOME_INTERRUPTED = "interrupted"
OUTCOME_RETRIES_EXHAUSTED = "retries_exhausted"
#: Cut by platform restarts past every resume and every fresh run of its owner agent.
OUTCOME_RESUME_EXHAUSTED = "resume_exhausted"
#: ``payload`` key of how often platform restarts made an item go on by itself.
RESTARTS_KEY = "restarts"

#: The states :func:`schedule_state` derives, in the order the views list them.
SCHEDULE_STATES: tuple[str, ...] = (
    "scheduled",
    "due",
    "running",
    "done",
    "blocked",
    "missed",
    "cancelled",
)
#: The filters :func:`list_scheduled_work_items` takes.
LIST_STATES: tuple[str, ...] = ("upcoming", "done", "blocked", "missed", "cancelled", "all")

#: The marker the chat renders as the scheduling card (``channels/base.py``
#: holds its pattern with the other artifact markers).
WORK_ITEM_MARKER_PREFIX = "[[WORK_ITEM:"

_MIN_LEAD_SECONDS = 30
_MAX_TITLE_CHARS = 300
_MAX_PROMPT_CHARS = 8000
_MAX_TOOL_NAME_CHARS = 200
_MAX_TARGET_CHARS = 300
_MAX_ARGUMENTS_BYTES = 64 * 1024
#: Longest ``scheduled.preview`` the API serves: one line of a
#: schedule row, cut here with an ellipsis so no view carries an unbounded
#: string; the whole content stays in the item's action.
PREVIEW_CHARS = 280
#: Scheduled items one person may have waiting at once, across their agents.
MAX_OPEN_PER_USER = 200


class ScheduledWorkItemError(ValueError):
    """A refused scheduling request; the message is what the caller shows."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def scheduling_enabled() -> bool:
    """The platform kill switch (Settings > Access and security)."""
    return bool(security_option("scheduled_work_items_enabled"))


def grace_window() -> timedelta:
    """How late an item is still dispatched; past it, the item is missed."""
    minutes = max(int(settings.scheduled_work_item_grace_minutes or 0), 1)
    return timedelta(minutes=minutes)


# ── Time ─────────────────────────────────────────────────────────────────────


def zone_of(tz_name: str | None) -> ZoneInfo | None:
    if not tz_name:
        return None
    try:
        return ZoneInfo(str(tz_name))
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def format_offset(offset: timedelta | None) -> str:
    total = int((offset or timedelta()).total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _day(at: datetime) -> str:
    return f"{at:%a} {at.day} {at:%b} {at.year}"


def describe_time(at: datetime, tz_name: str | None) -> str:
    """``Thu 8 Oct 2026 09:00 Europe/Paris (CEST, UTC+02:00) = 07:00 UTC``."""
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    utc = at.astimezone(UTC)
    zone = zone_of(tz_name)
    if zone is None:
        return f"{_day(utc)} {utc:%H:%M} UTC"
    local = utc.astimezone(zone)
    return (
        f"{_day(local)} {local:%H:%M} {tz_name} ({local.tzname()}, "
        f"UTC{format_offset(local.utcoffset())}) = {utc:%H:%M} UTC"
    )


@dataclass(frozen=True)
class DueAt:
    """A parsed ``due_at``: the instant (UTC) and the value as written."""

    at: datetime
    given: datetime


def _missing_offset_message(naive: datetime, tz_name: str | None) -> str:
    written = naive.isoformat()
    message = (
        f"due_at '{written}' has no UTC offset, and a time without one is never "
        "read as UTC: nothing was scheduled."
    )
    zone = zone_of(tz_name)
    if zone is None:
        return (
            f"{message} Write the offset of the time zone meant, for example "
            f"{written}+02:00 for Paris summer time or {written}+00:00 for UTC."
        )
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    exists = first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive
    if not exists:
        return (
            f"{message} {written} does not exist in {tz_name}: the clocks go "
            "forward over it on that date. Pick another time, with its offset."
        )
    if first.utcoffset() != second.utcoffset():
        return (
            f"{message} In {tz_name} that wall time happens twice on that date "
            f"(the clocks go back): {written}{format_offset(first.utcoffset())} "
            f"is {first.astimezone(UTC):%H:%M} UTC, "
            f"{written}{format_offset(second.utcoffset())} is "
            f"{second.astimezone(UTC):%H:%M} UTC. Say which."
        )
    return (
        f"{message} In {tz_name}, the requester's time zone, that is "
        f"{written}{format_offset(first.utcoffset())} "
        f"({first.astimezone(UTC):%H:%M} UTC)."
    )


def parse_due_at(
    value: Any, *, tz_name: str | None = None, now: datetime | None = None
) -> DueAt:
    """Read ``due_at``: ISO-8601 with its UTC offset, in the future, within the horizon."""
    text = str(value or "").strip()
    if not text:
        raise ScheduledWorkItemError(
            "due_at is required: an ISO-8601 date and time with its UTC offset, "
            "for example 2026-10-08T09:00:00+02:00."
        )
    if text[-1] in "Zz":
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ScheduledWorkItemError(
            f"due_at '{value}' is not an ISO-8601 date and time. Write it with "
            "its UTC offset, for example 2026-10-08T09:00:00+02:00."
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ScheduledWorkItemError(_missing_offset_message(parsed, tz_name))
    now = now or datetime.now(UTC)
    at = parsed.astimezone(UTC)
    if at <= now + timedelta(seconds=_MIN_LEAD_SECONDS):
        raise ScheduledWorkItemError(
            f"due_at {describe_time(at, tz_name)} is not in the future (it is "
            f"now {describe_time(now, tz_name)}): nothing was scheduled. Pick a "
            "later time."
        )
    horizon = max(int(settings.scheduled_work_item_max_horizon_days or 0), 1)
    if at > now + timedelta(days=horizon):
        raise ScheduledWorkItemError(
            f"due_at {describe_time(at, tz_name)} is more than {horizon} days "
            "ahead. Schedule something nearer, or use a recurring assignment."
        )
    return DueAt(at=at, given=parsed)


def offset_note(given: datetime, tz_name: str | None) -> str | None:
    """Say when the offset written is not the requester's zone on that date."""
    zone = zone_of(tz_name)
    if zone is None or given.tzinfo is None:
        return None
    local = given.astimezone(zone)
    if local.utcoffset() == given.utcoffset():
        return None
    return (
        f"Note: UTC{format_offset(given.utcoffset())} is not {tz_name} time on "
        f"{local:%Y-%m-%d} ({tz_name} is at UTC{format_offset(local.utcoffset())} "
        f"then), so this runs at {local:%H:%M} {tz_name} time. Reschedule it if "
        f"{given:%H:%M} {tz_name} time was meant."
    )


# ── The action ───────────────────────────────────────────────────────────────


#: ``payload.action`` key naming the work item the action serves: once
#: that item is done or cancelled, a blocked action is cancelled by the
#: scheduler (``target already done``) instead of waiting for a click.
SERVES_KEY = "serves_work_item_id"
#: ``payload.action`` key: whether the outcome reaches the person's bell (and
#: their linked phones). False for a one-off an agent scheduled for itself
#: during a run; the outcome is still posted to the conversation.
NOTIFY_KEY = "notify"
#: ``payload.action`` key: the outcome is for the person whatever ``notify``
#: says, the one case a silent one-off still reaches the bell.
ESCALATE_KEY = "escalate_to_user"
_FLAG_KEYS = (NOTIFY_KEY, ESCALATE_KEY)

#: The ``notify`` of the scheduled one-off whose run is executing, set by the
#: scheduled runner around its instruction turn (and copied into every task
#: the turn starts). None outside such a run.
_RUNNING_ONE_OFF_NOTIFY: ContextVar[bool | None] = ContextVar("running_one_off_notify", default=None)


@contextmanager
def running_one_off(action: dict | None) -> Iterator[None]:
    """Mark the current context as the run of a scheduled one-off with *action*.

    A one-off scheduled during that run without an explicit ``notify``
    inherits this one's flag (:func:`default_notify`): a silent one-off that
    schedules its successor keeps the chain silent.
    """
    notify = (action or {}).get(NOTIFY_KEY)
    token = _RUNNING_ONE_OFF_NOTIFY.set(notify if isinstance(notify, bool) else False)
    try:
        yield
    finally:
        _RUNNING_ONE_OFF_NOTIFY.reset(token)


def default_notify(caller_is_person: bool) -> bool:
    """The ``notify`` a new one-off gets when the call does not set it.

    In the run of a scheduled one-off: that one-off's own flag (an agent
    runs it, whatever the turn looks like). Elsewhere: *caller_is_person*.
    """
    inherited = _RUNNING_ONE_OFF_NOTIFY.get()
    return caller_is_person if inherited is None else inherited


def build_action(
    mode: str,
    *,
    tool: str | None = None,
    arguments: Any = None,
    prompt: str | None = None,
    target: str | None = None,
    serves_work_item_id: str | None = None,
    notify: bool | None = None,
    escalate_to_user: bool | None = None,
) -> dict:
    """The ``payload.action`` of a new item, validated."""
    mode = str(mode or "").strip().lower()
    action: dict[str, Any]
    if mode == "exact":
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                raise ScheduledWorkItemError(
                    f"arguments is not valid JSON ({exc})."
                ) from None
        action = {
            "mode": "exact",
            "tool": str(tool or "").strip(),
            "arguments": arguments if arguments is not None else {},
        }
    elif mode == "instruction":
        action = {"mode": "instruction", "prompt": str(prompt or "").strip()}
    else:
        raise ScheduledWorkItemError(
            "mode must be 'exact' (run one tool call exactly as given, no model) "
            "or 'instruction' (the agent runs a prompt at that time)."
        )
    if target is not None and str(target).strip():
        action["target"] = str(target).strip()[:_MAX_TARGET_CHARS]
    if serves_work_item_id is not None and str(serves_work_item_id).strip():
        action[SERVES_KEY] = str(serves_work_item_id).strip().lower()
    if notify is not None:
        action[NOTIFY_KEY] = notify
    if escalate_to_user:
        action[ESCALATE_KEY] = True
    validate_action(action)
    return action


def validate_action(action: Any) -> str:
    """Check a stored or new ``payload.action``; returns its mode."""
    if not isinstance(action, dict):
        raise ScheduledWorkItemError("payload.action must be an object.")
    mode = action.get("mode")
    if mode not in WORK_ITEM_ACTION_MODES:
        raise ScheduledWorkItemError(
            "payload.action.mode must be 'exact' or 'instruction'."
        )
    if mode == "exact":
        tool = action.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise ScheduledWorkItemError("An exact action needs the name of the tool to run.")
        if len(tool) > _MAX_TOOL_NAME_CHARS:
            raise ScheduledWorkItemError("The tool name is too long.")
        arguments = action.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ScheduledWorkItemError("An exact action's arguments must be a JSON object.")
        try:
            size = len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            raise ScheduledWorkItemError("An exact action's arguments must be plain JSON.") from None
        if size > _MAX_ARGUMENTS_BYTES:
            raise ScheduledWorkItemError(
                f"An exact action's arguments are limited to {_MAX_ARGUMENTS_BYTES // 1024} KB."
            )
    else:
        prompt = action.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ScheduledWorkItemError("An instruction needs a prompt.")
        if len(prompt) > _MAX_PROMPT_CHARS:
            raise ScheduledWorkItemError(
                f"An instruction is limited to {_MAX_PROMPT_CHARS} characters."
            )
    serves = action.get(SERVES_KEY)
    if serves is not None and (not isinstance(serves, str) or safe_uuid(serves) is None):
        raise ScheduledWorkItemError(
            f"payload.action.{SERVES_KEY} must be the id of a work item."
        )
    pinned = action.get("pinned_integrations")
    if pinned is not None and not (
        isinstance(pinned, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in pinned.items())
    ):
        raise ScheduledWorkItemError("payload.action.pinned_integrations is malformed.")
    for key in _FLAG_KEYS:
        if key in action and not isinstance(action[key], bool):
            raise ScheduledWorkItemError(f"payload.action.{key} must be true or false.")
    return mode


def escalates(item: WorkItem) -> bool:
    """The outcome is for the person: asked at scheduling, or the item needs a human."""
    action = action_of(item.payload) or {}
    return action.get(ESCALATE_KEY) is True or bool(getattr(item, "needs_human", None))


def notifies_person(item: WorkItem) -> bool:
    """Whether the item's outcome goes to the person's bell, not only its conversation.

    An item scheduled before ``notify`` existed carries no flag: one an agent
    created (``created_by_agent_id``) or scheduled during a run (no
    conversation) is silent; any other one from a chat turn keeps notifying.
    """
    if escalates(item):
        return True
    notify = (action_of(item.payload) or {}).get(NOTIFY_KEY)
    if isinstance(notify, bool):
        return notify
    if getattr(item, "created_by_agent_id", None) is not None:
        return False
    return getattr(item, "conversation_id", None) is not None


def restarts_of(payload: Any) -> dict[str, int]:
    """How often platform restarts made an item go on by itself: ``resumed`` and ``requeued``."""
    found = payload.get(RESTARTS_KEY) if isinstance(payload, dict) else None
    found = found if isinstance(found, dict) else {}
    counts: dict[str, int] = {}
    for key in ("resumed", "requeued"):
        value = found.get(key)
        counts[key] = value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
    return counts


def action_of(payload: Any) -> dict | None:
    if not isinstance(payload, dict):
        return None
    action = payload.get("action")
    return action if isinstance(action, dict) else None


_UUID_IN_TEXT = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)


def served_work_item_ids(item: WorkItem) -> list[uuid.UUID]:
    """The work items a scheduled action serves, the item itself left out.

    ``payload.action.serves_work_item_id`` when the scheduling agent named it;
    otherwise the work item ids the action itself writes out in full (its
    title, description, target or prompt) and the reference arguments of an
    exact action (``work_item_id`` / ``*_work_item_id`` and ``work_item_ids`` /
    ``*_work_item_ids``, at any nesting level down to a fixed depth; a
    free-form argument such as a message body is never read), which the
    caller resolves against
    the person's work items. A follow-up such as "retest, then complete item
    <id>" has nothing left to do once that item is done or cancelled.
    """
    action = action_of(item.payload) or {}
    named = safe_uuid(action.get(SERVES_KEY)) if action.get(SERVES_KEY) else None
    if named is not None:
        return [named] if named != item.id else []
    texts = [item.title, item.description, action.get("target"), action.get("prompt")]
    found: dict[uuid.UUID, None] = {}
    for text in texts:
        for match in _UUID_IN_TEXT.findall(text or ""):
            value = safe_uuid(match)
            if value is not None and value != item.id:
                found[value] = None
    for value in _referenced_work_item_ids(action.get("arguments")):
        if value != item.id:
            found[value] = None
    return list(found)


REFERENCE_SCAN_MAX_DEPTH = 8


def _referenced_work_item_ids(arguments: Any) -> list[uuid.UUID]:
    """The work item ids an exact action's reference arguments name, in order.

    Walks nested objects and lists (a custom tool may carry
    ``{"request": {"work_item_id": ...}}``) down to
    ``REFERENCE_SCAN_MAX_DEPTH`` levels; deeper values are ignored, so a deep
    or self-referencing structure can neither recurse without bound nor raise.
    A value is read only under a reference key (``work_item_id`` /
    ``*_work_item_id``, ``work_item_ids`` / ``*_work_item_ids``), never from
    free text.
    """
    values: list[Any] = []
    _collect_reference_values(arguments, 0, values)
    ids: list[uuid.UUID] = []
    for value in values:
        parsed = safe_uuid(value) if isinstance(value, str) else None
        if parsed is not None:
            ids.append(parsed)
    return ids


def _collect_reference_values(node: Any, depth: int, values: list[Any]) -> None:
    """Append the values found under reference keys of ``node`` to ``values``."""
    if depth >= REFERENCE_SCAN_MAX_DEPTH:
        return
    if isinstance(node, list):
        for element in node:
            if isinstance(element, dict | list):
                _collect_reference_values(element, depth + 1, values)
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if not isinstance(key, str):
            continue
        if key == "work_item_id" or key.endswith("_work_item_id"):
            values.append(value)
        elif (key == "work_item_ids" or key.endswith("_work_item_ids")) and isinstance(value, list):
            values.extend(value)
        elif isinstance(value, dict | list):
            _collect_reference_values(value, depth + 1, values)


def refuse_generic_action_write(payload: Any) -> None:
    """A generic work-item write may not carry ``payload.action``.

    Only this module creates an executable item, through the scheduling tools
    or :func:`edit_scheduled_action`: a payload written through the generic
    paths (``create_work_item`` / ``update_work_item``, their REST routes) is
    data, and data carrying an ``action`` key would read as something the
    platform runs.
    """
    if isinstance(payload, dict) and "action" in payload:
        raise ScheduledWorkItemError(
            "payload.action is reserved for scheduled work items: schedule the "
            "action with the agent's schedule_work_item tool instead."
        )


def _longest_text(value: Any, depth: int = 0) -> str:
    if isinstance(value, str):
        return value
    if depth >= 3:
        return ""
    if isinstance(value, dict):
        values = list(value.values())
    elif isinstance(value, list):
        values = value
    else:
        return ""
    best = ""
    for nested in values:
        candidate = _longest_text(nested, depth + 1)
        if len(candidate) > len(best):
            best = candidate
    return best


def action_preview(action: dict | None) -> str:
    """The content a person recognises: the prompt, or the longest text argument."""
    if not action:
        return ""
    text = (
        action.get("prompt") or ""
        if action.get("mode") == "instruction"
        else _longest_text(action.get("arguments") or {})
    )
    text = " ".join(str(text).split())
    return text if len(text) <= PREVIEW_CHARS else f"{text[: PREVIEW_CHARS - 1]}\u2026"


def schedule_state(item: WorkItem, now: datetime | None = None) -> str | None:
    """What the views show for a scheduled item (``None`` for any other item)."""
    if not getattr(item, "action_mode", None):
        return None
    status = item.status
    if status == "open":
        due = item.due_at
        if due is None:
            return "scheduled"
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        now = now or datetime.now(UTC)
        if due > now:
            return "scheduled"
        return "missed" if now - due > grace_window() else "due"
    if status in ("claimed", "in_progress"):
        return "running"
    if status == "blocked":
        return "missed" if getattr(item, "outcome_code", None) == OUTCOME_MISSED else "blocked"
    return status if status in ("done", "cancelled") else "blocked"


def scheduled_view(item: WorkItem, now: datetime | None = None) -> dict | None:
    """The ``scheduled`` block of a serialized item."""
    if not getattr(item, "action_mode", None):
        return None
    action = action_of(item.payload) or {}
    run_id = getattr(item, "run_id", None)
    conversation_id = getattr(item, "conversation_id", None)
    return {
        "mode": item.action_mode,
        "tool": action.get("tool") if item.action_mode == "exact" else None,
        "target": action.get("target"),
        "preview": action_preview(action),
        "state": schedule_state(item, now),
        "run_id": str(run_id) if run_id else None,
        "conversation_id": str(conversation_id) if conversation_id else None,
        "outcome_code": getattr(item, "outcome_code", None),
        "notify": notifies_person(item),
        # Resumed by itself after platform restarts: "resumed N times".
        "restarts": restarts_of(item.payload),
    }


def marker(item_id: uuid.UUID | str) -> str:
    return f"{WORK_ITEM_MARKER_PREFIX}{str(item_id).lower()}]]"


def append_markers(text: str, item_ids: list[str]) -> str:
    """Append the card marker of every item a turn scheduled that its answer lacks."""
    missing = [m for m in dict.fromkeys(marker(i) for i in item_ids) if m not in (text or "")]
    if not missing:
        return text
    return "\n\n".join([(text or "").rstrip(), *missing]).strip()


# ── Reads and writes ─────────────────────────────────────────────────────────


def _owner_clause(
    user_uuid: uuid.UUID,
    agent_uuid: uuid.UUID | None,
    owner_scope: Any = None,
    cross_person_agents: Sequence[uuid.UUID] | None = None,
) -> Any:
    person = WorkItem.user_id == user_uuid
    if cross_person_agents:
        # #2220: an agent acting for its owner also reaches the one-offs it
        # (or a same-owner agent it hands work to) scheduled for another person.
        person = or_(person, WorkItem.owner_agent_id.in_(list(cross_person_agents)))
    clauses = [person, WorkItem.action_mode.is_not(None)]
    if agent_uuid is not None:
        clauses.append(WorkItem.owner_agent_id == agent_uuid)
    if owner_scope is not None:
        clauses.append(owner_scope)
    return and_(*clauses)


def _state_clause(state: str, now: datetime):
    missed_by = now - grace_window()
    if state == "upcoming":
        return WorkItem.status.in_(("open", "claimed", "in_progress"))
    if state == "done":
        return WorkItem.status == "done"
    if state == "cancelled":
        return WorkItem.status == "cancelled"
    if state == "missed":
        return or_(
            and_(WorkItem.status == "blocked", WorkItem.outcome_code == OUTCOME_MISSED),
            and_(WorkItem.status == "open", WorkItem.due_at < missed_by),
        )
    if state == "blocked":
        return and_(
            WorkItem.status == "blocked",
            or_(WorkItem.outcome_code.is_(None), WorkItem.outcome_code != OUTCOME_MISSED),
        )
    return None


async def cross_person_agents(db: Any, user_uuid: uuid.UUID, agent_uuid: uuid.UUID | None) -> list[uuid.UUID]:
    """The agents whose one-offs *agent_uuid* reaches for any person (#2220).

    Only when the acting person owns the acting agent: the agent itself and
    the agents it hands work to (``delegate`` / ``transfer``) that the same
    person owns. Empty otherwise, so another owner's or tenant's one-offs
    stay out of reach, and a run acting for someone else gains nothing.
    """
    from app.models.agent import Agent

    if agent_uuid is None:
        return []
    agent = (await db.execute(select(Agent).where(Agent.id == agent_uuid))).scalar_one_or_none()
    if agent is None or getattr(agent, "user_id", None) != user_uuid:
        return []
    targets = [
        target
        for target in (safe_uuid(str(t)) for t in getattr(agent, "handover_agent_ids", None) or ())
        if target is not None
        and hands_work_to(agent.handover_agent_ids, getattr(agent, "handover_types", None), target)
    ]
    owned = []
    if targets:
        owned = list(
            (await db.execute(select(Agent.id).where(Agent.id.in_(targets), Agent.user_id == user_uuid))).scalars()
        )
    return [agent_uuid, *owned]


async def _user(db, user_uuid: uuid.UUID):
    from app.models.user import User

    return (await db.execute(select(User).where(User.id == user_uuid))).scalar_one_or_none()


async def _audit(
    db,
    user_uuid: uuid.UUID,
    *,
    action: str,
    item: WorkItem,
    summary: str,
    details: dict,
) -> None:
    """One audit row per scheduling write; never fails the write itself."""
    from app.services.audit import log_audit

    try:
        user = await _user(db, user_uuid)
        if user is None:
            return
        await log_audit(
            db,
            user=user,
            action=action,
            entity_type="work_item",
            entity_id=item.id,
            entity_name=item.title[:255],
            summary=summary[:1000],
            details={"scheduled": True, **details},
        )
    except Exception as exc:
        logger.warning(
            "Could not audit scheduled work item %s (%s): %s",
            item.id,
            action,
            format_exception_message(exc),
        )


async def publish_update(item: WorkItem) -> None:
    """Tell the person's open pages the item changed (best effort)."""
    if not item.user_id:
        return
    try:
        import asyncio

        from app.core.redis import redis_client

        redis = await redis_client()
        payload = json.dumps(
            {
                "type": "work_item_update",
                "work_item_id": str(item.id),
                "conversation_id": str(item.conversation_id) if item.conversation_id else None,
                "status": item.status,
                "state": schedule_state(item),
            }
        )
        await asyncio.wait_for(redis.publish(f"activity:{item.user_id}", payload), timeout=2.0)
    except Exception:
        logger.debug("Could not publish the update of work item %s", item.id, exc_info=True)


def serialize(item: WorkItem) -> dict:
    from app.services.work_item import serialize_work_item

    return serialize_work_item(item)


async def create_scheduled_work_item(
    *,
    user_id: str,
    agent_id: str,
    agent_name: str,
    title: str,
    due: DueAt,
    action: dict,
    description: str | None = None,
    conversation_id: str | None = None,
    priority: str = "medium",
) -> dict:
    """Create a scheduled item owned by *agent_id*, acting for *user_id*."""
    from app.core.database import async_session
    from app.models.work_item import WORK_ITEM_PRIORITIES

    if not scheduling_enabled():
        raise ScheduledWorkItemError(SCHEDULING_DISABLED_MESSAGE, status_code=403)
    user_uuid = safe_uuid(user_id)
    agent_uuid = safe_uuid(agent_id)
    if user_uuid is None or agent_uuid is None:
        raise ScheduledWorkItemError(
            "Scheduling needs a signed-in person: the action runs as whoever asked for it."
        )
    mode = validate_action(action)
    title = " ".join(str(title or "").split())[:_MAX_TITLE_CHARS]
    if not title:
        raise ScheduledWorkItemError("A scheduled work item needs a short title.")
    now = datetime.now(UTC)
    async with async_session() as db:
        waiting = (
            await db.execute(
                select(WorkItem.id)
                .where(
                    WorkItem.user_id == user_uuid,
                    WorkItem.action_mode.is_not(None),
                    WorkItem.status.in_(("open", "claimed", "in_progress")),
                )
                .limit(MAX_OPEN_PER_USER)
            )
        ).all()
        if len(waiting) >= MAX_OPEN_PER_USER:
            raise ScheduledWorkItemError(
                f"You already have {MAX_OPEN_PER_USER} scheduled work items waiting: "
                "cancel some before scheduling more.",
                status_code=409,
            )
        item = WorkItem(
            user_id=user_uuid,
            is_company_managed=False,
            owner_agent_id=agent_uuid,
            created_by_agent_id=agent_uuid,
            title=title,
            description=description or None,
            payload={"action": action},
            status="open",
            priority=priority if priority in WORK_ITEM_PRIORITIES else "medium",
            due_at=due.at,
            source="agent",
            action_mode=mode,
            conversation_id=safe_uuid(conversation_id) if conversation_id else None,
            last_activity_at=now,
        )
        db.add(item)
        await db.flush()
        tz_name = getattr(await _user(db, user_uuid), "timezone", None)
        await _audit(
            db,
            user_uuid,
            action="create",
            item=item,
            summary=(
                f"Scheduled '{title}' on agent {agent_name} for "
                f"{describe_time(due.at, tz_name)}"
            ),
            details={
                "via": "agent",
                "agent_id": str(agent_uuid),
                "due_at": due.at.isoformat(),
                "due_at_given": due.given.isoformat(),
                "action": action,
                "conversation_id": conversation_id,
            },
        )
        await db.commit()
        await db.refresh(item)
        serialized = serialize(item)
    await publish_update(item)
    return serialized


async def get_scheduled_work_item(
    item_id: str, *, user_id: str, agent_id: str | None = None
) -> dict | None:
    from app.core.database import async_session

    wid, uid = safe_uuid(item_id), safe_uuid(user_id)
    if wid is None or uid is None:
        return None
    async with async_session() as db:
        item = (
            await db.execute(
                select(WorkItem).where(
                    WorkItem.id == wid,
                    _owner_clause(uid, safe_uuid(agent_id) if agent_id else None),
                )
            )
        ).scalar_one_or_none()
        return serialize(item) if item else None


async def list_scheduled_work_items(
    *,
    user_id: str,
    agent_id: str | None = None,
    state: str = "upcoming",
    limit: int = 50,
    owner_scope=None,
) -> list[dict]:
    """The person's scheduled items (of one agent when *agent_id* is given).

    *owner_scope* narrows further on ``WorkItem.owner_agent_id`` (a fleet's
    members or the playground: ``fleet_views.fleet_scope_clause``).
    """
    from app.core.database import async_session

    uid = safe_uuid(user_id)
    if uid is None:
        return []
    if state not in LIST_STATES:
        raise ScheduledWorkItemError(
            f"state must be one of: {', '.join(LIST_STATES)}."
        )
    now = datetime.now(UTC)
    agent_uuid = safe_uuid(agent_id) if agent_id else None
    order = (
        (WorkItem.due_at.asc(), WorkItem.created_at.asc())
        if state == "upcoming"
        else (WorkItem.updated_at.desc(),)
    )
    async with async_session() as db:
        reach = await cross_person_agents(db, uid, agent_uuid) if agent_uuid is not None else []
        clauses = [_owner_clause(uid, agent_uuid, owner_scope, cross_person_agents=reach)]
        state_clause = _state_clause(state, now)
        if state_clause is not None:
            clauses.append(state_clause)
        rows = (
            await db.execute(
                select(WorkItem).where(*clauses).order_by(*order).limit(min(max(limit, 1), 200))
            )
        ).scalars().all()
        return [serialize(row) for row in rows]


#: How an agent other than the owner reaches a scheduled item.
AUTHORITY_OWNER = "owner"
AUTHORITY_PERSON = "person"
AUTHORITY_HANDOVER = "handover"
AUTHORITY_LEFTOVER = "leftover"
#: The handover types that make an agent answerable for its target's work: it
#: hands the work over (``delegate``) or the conversation (``transfer``).
_RESPONSIBLE_HANDOVER_TYPES = frozenset({"delegate", "transfer"})


def hands_work_to(handover_agent_ids: Any, type_map: Any, target_id: uuid.UUID | str) -> bool:
    """Whether an agent hands work to *target_id* by ``delegate`` or ``transfer``.

    A target of a legacy map (no type recorded) runs every type; a target of an
    explicit map runs the type it has, ``delegate`` when it has none.
    """
    from app.services.handover_types import is_legacy_handover_map

    target = str(target_id)
    if target not in {str(agent_id) for agent_id in handover_agent_ids or ()}:
        return False
    if is_legacy_handover_map(type_map):
        return True
    return (type_map or {}).get(target, "delegate") in _RESPONSIBLE_HANDOVER_TYPES


def change_authority(
    item: WorkItem,
    *,
    acting_agent_id: uuid.UUID | None,
    acting_handover_ids: Any = None,
    acting_handover_types: Any = None,
    now: datetime | None = None,
) -> str | None:
    """Why the caller may cancel or reschedule *item*, ``None`` when it may not.

    The caller acts for the item's person (the queries pin ``user_id``). The
    person (no acting agent) and the agent that scheduled it always may. Another
    agent of the person may when it hands work to the scheduling agent
    (``delegate`` or ``transfer``: an orchestrator tidying its desk's actions),
    or when the item is a leftover nobody will run - blocked or missed - so a
    platform restart never leaves a row only a human click clears.
    """
    if acting_agent_id is None:
        return AUTHORITY_PERSON
    if item.owner_agent_id == acting_agent_id:
        return AUTHORITY_OWNER
    if item.owner_agent_id is not None and hands_work_to(
        acting_handover_ids, acting_handover_types, item.owner_agent_id
    ):
        return AUTHORITY_HANDOVER
    if schedule_state(item, now) in ("blocked", "missed"):
        return AUTHORITY_LEFTOVER
    return None


async def _locked_changeable_item(
    db,
    item_id: str,
    user_uuid: uuid.UUID,
    agent_uuid: uuid.UUID | None,
    *,
    across_persons: bool = False,
) -> tuple[WorkItem | None, str | None, str | None]:
    """The person's scheduled item, locked, with the caller's authority and name.

    ``(None, None, None)`` when the item is not the person's or the caller may
    not change it (:func:`change_authority`): the same answer either way, so a
    refusal never tells an agent that someone else's item exists.

    *across_persons* (cancel only, #2220) also reaches a one-off of the
    :func:`cross_person_agents` of the caller, scheduled for another person.
    """
    from app.models.agent import Agent

    reach = await cross_person_agents(db, user_uuid, agent_uuid) if across_persons else []
    item = await _locked_owned_item(db, item_id, user_uuid, None, cross_person=reach)
    if item is None:
        return None, None, None
    agent = None
    if agent_uuid is not None and item.owner_agent_id != agent_uuid:
        agent = (await db.execute(select(Agent).where(Agent.id == agent_uuid))).scalar_one_or_none()
        if agent is None:
            return None, None, None
    authority = change_authority(
        item,
        acting_agent_id=agent_uuid,
        acting_handover_ids=getattr(agent, "handover_agent_ids", None),
        acting_handover_types=getattr(agent, "handover_types", None),
    )
    if authority is None:
        return None, None, None
    return item, authority, getattr(agent, "name", None)


def _acting_details(agent_uuid: uuid.UUID | None, authority: str, agent_name: str | None) -> dict:
    if authority not in (AUTHORITY_HANDOVER, AUTHORITY_LEFTOVER):
        return {}
    return {
        "acting_agent_id": str(agent_uuid),
        "acting_agent_name": agent_name,
        "authority": authority,
    }


async def _locked_owned_item(
    db: Any,
    item_id: str,
    user_uuid: uuid.UUID,
    agent_uuid: uuid.UUID | None,
    cross_person: Sequence[uuid.UUID] | None = None,
) -> WorkItem | None:
    wid = safe_uuid(item_id)
    if wid is None:
        return None
    return (
        await db.execute(
            select(WorkItem)
            .where(WorkItem.id == wid, _owner_clause(user_uuid, agent_uuid, cross_person_agents=cross_person))
            .with_for_update()
        )
    ).scalar_one_or_none()


def cut_by_restart(item: WorkItem) -> bool:
    """Whether platform restarts cut *item* while it ran: resumed, re-queued or past every resume."""
    restarts = restarts_of(item.payload)
    return (
        getattr(item, "outcome_code", None) == OUTCOME_RESUME_EXHAUSTED
        or restarts["resumed"] > 0
        or restarts["requeued"] > 0
    )


def _refuse_restart_cut_reschedule(item: WorkItem) -> None:
    """An action a restart cut already ran, maybe in part: moving it would send it again blind.

    The REST route and the scheduling tool both reach this service, so the
    rule holds whatever the surface; the platform's own recovery (resume, a
    fresh read-first run of the owner agent) is the only way it runs again.
    """
    if cut_by_restart(item):
        raise ScheduledWorkItemError(
            f"'{item.title}' already ran and platform restarts cut it: the platform resumed it or ran it "
            "again for its agent, so it is not rescheduled. Schedule a new one if another run is wanted.",
            status_code=409,
        )


def _refuse_when_settled(item: WorkItem, verb: str) -> None:
    if item.status in ("claimed", "in_progress"):
        raise ScheduledWorkItemError(
            f"'{item.title}' is running now and can no longer be {verb}.", status_code=409
        )
    if item.status == "done":
        raise ScheduledWorkItemError(
            f"'{item.title}' already ran: schedule a new one instead.", status_code=409
        )
    if item.status == "cancelled":
        raise ScheduledWorkItemError(
            f"'{item.title}' was cancelled: schedule a new one instead.", status_code=409
        )


async def cancel_scheduled_work_item(
    item_id: str, *, user_id: str, agent_id: str | None = None, via: str = "agent"
) -> dict | None:
    """Cancel a scheduled (or blocked / missed) item before it runs: no run is created.

    *agent_id* is the calling agent (``None`` for the person): the scheduling
    agent, or another agent of the person :func:`change_authority` allows, whose
    name and reason are written on the item and in the audit row.
    """
    from app.core.database import async_session

    uid = safe_uuid(user_id)
    if uid is None:
        return None
    agent_uuid = safe_uuid(agent_id) if agent_id else None
    if agent_id and agent_uuid is None:
        return None
    async with async_session() as db:
        item, authority, agent_name = await _locked_changeable_item(db, item_id, uid, agent_uuid, across_persons=True)
        if item is None or authority is None:
            return None
        _refuse_when_settled(item, "cancelled")
        now = datetime.now(UTC)
        previous = item.status
        acting = _acting_details(agent_uuid, authority, agent_name)
        item.status = "cancelled"
        item.result = f"Cancelled on {now:%Y-%m-%d %H:%M} UTC, before it ran."
        if acting:
            reason = (
                "it hands work to the agent that scheduled it"
                if authority == AUTHORITY_HANDOVER
                else "the action was a blocked or missed leftover"
            )
            item.result = (
                f"Cancelled on {now:%Y-%m-%d %H:%M} UTC by {agent_name or 'another agent'} "
                f"({reason}), before it ran."
            )
        item.claimed_by_agent_id = None
        item.claimed_at = None
        item.last_activity_at = now
        await _audit(
            db,
            uid,
            action="cancel",
            item=item,
            summary=f"Cancelled the scheduled work item '{item.title}'",
            details={
                "via": via,
                "previous_status": previous,
                "due_at": _iso(item.due_at),
                **acting,
            },
        )
        await db.commit()
        await db.refresh(item)
        serialized = serialize(item)
    await publish_update(item)
    return serialized


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def reschedule_scheduled_work_item(
    item_id: str,
    due: DueAt,
    *,
    user_id: str,
    agent_id: str | None = None,
    via: str = "agent",
) -> dict | None:
    """Move an item that has not run (scheduled, blocked or missed) to a new time."""
    from app.core.database import async_session

    if not scheduling_enabled():
        raise ScheduledWorkItemError(SCHEDULING_DISABLED_MESSAGE, status_code=403)
    uid = safe_uuid(user_id)
    if uid is None:
        return None
    agent_uuid = safe_uuid(agent_id) if agent_id else None
    if agent_id and agent_uuid is None:
        return None
    async with async_session() as db:
        item, authority, agent_name = await _locked_changeable_item(db, item_id, uid, agent_uuid)
        if item is None or authority is None:
            return None
        _refuse_when_settled(item, "rescheduled")
        _refuse_restart_cut_reschedule(item)
        acting = _acting_details(agent_uuid, authority, agent_name)
        previous_due, previous_status = item.due_at, item.status
        now = datetime.now(UTC)
        item.due_at = due.at
        item.status = "open"
        item.outcome_code = None
        item.result = None
        item.run_id = None
        item.claimed_by_agent_id = None
        item.claimed_at = None
        item.last_activity_at = now
        tz_name = getattr(await _user(db, uid), "timezone", None)
        await _audit(
            db,
            uid,
            action="reschedule",
            item=item,
            summary=(
                f"Rescheduled '{item.title}' to {describe_time(due.at, tz_name)}"
            ),
            details={
                "via": via,
                "previous_due_at": _iso(previous_due),
                "previous_status": previous_status,
                "due_at": due.at.isoformat(),
                "due_at_given": due.given.isoformat(),
                **acting,
            },
        )
        await db.commit()
        await db.refresh(item)
        serialized = serialize(item)
    await publish_update(item)
    return serialized


async def edit_scheduled_action(
    item_id: str,
    action: dict,
    *,
    user_id: str,
    agent_id: str | None = None,
    via: str = "rest",
) -> dict | None:
    """Replace the action of an item that has not run; audited with before and after."""
    from app.core.database import async_session

    uid = safe_uuid(user_id)
    if uid is None:
        return None
    previous_pinned = None
    async with async_session() as db:
        item = await _locked_owned_item(db, item_id, uid, safe_uuid(agent_id) if agent_id else None)
        if item is None:
            return None
        if item.status != "open":
            raise ScheduledWorkItemError(
                f"'{item.title}' can only be edited while it waits to run.", status_code=409
            )
        before = action_of(item.payload) or {}
        previous_pinned = before.get("pinned_integrations")
        # The integrations an agent account turn pinned are recorded by the
        # server at creation, never taken from an edit.
        action = {k: v for k, v in action.items() if k != "pinned_integrations"}
        if previous_pinned:
            action["pinned_integrations"] = previous_pinned
        if SERVES_KEY not in action and before.get(SERVES_KEY):
            action[SERVES_KEY] = before[SERVES_KEY]
        for key in _FLAG_KEYS:
            if key not in action and key in before:
                action[key] = before[key]
        mode = validate_action(action)
        if mode != item.action_mode:
            raise ScheduledWorkItemError(
                "An edit keeps the item's mode; cancel it and schedule a new one to change it."
            )
        item.payload = {**(item.payload or {}), "action": action}
        item.last_activity_at = datetime.now(UTC)
        await _audit(
            db,
            uid,
            action="update",
            item=item,
            summary=f"Edited the action of the scheduled work item '{item.title}'",
            details={"via": via, "before": before, "after": action},
        )
        await db.commit()
        await db.refresh(item)
        serialized = serialize(item)
    await publish_update(item)
    return serialized


# ── Outcome delivery ─────────────────────────────────────────────────────────


_OUTCOME_HEADLINES = {
    "done": "Done",
    "missed": "Missed",
    "blocked": "Not run",
    "cancelled": "Cancelled",
}

#: Why an item did not run, in one clause, and one way to fix it, by
#: ``outcome_code``. The posted outcome says these, never the raw error: the
#: error stays in ``item.result``, on the card and in the run.
_BLOCKED_REASONS: dict[str | None, tuple[str, str]] = {
    OUTCOME_DISABLED: (
        "scheduling was turned off by an administrator",
        "Ask an administrator to turn it back on, then schedule it again.",
    ),
    OUTCOME_AGENT_UNAVAILABLE: (
        "its agent no longer exists or is disabled",
        "Schedule it again on an enabled agent.",
    ),
    OUTCOME_OWNER_DEACTIVATED: (
        "the account it runs as was deactivated",
        "Ask an administrator to reactivate the account.",
    ),
    OUTCOME_PERMISSION: (
        "you can no longer use its agent",
        "Ask the agent's owner for access, then schedule it again.",
    ),
    OUTCOME_AI_UNAVAILABLE: (
        "agentic AI was not available on the platform",
        "Schedule it again once AI is available.",
    ),
    OUTCOME_BINDING: (
        "its tool or integration is no longer available to the agent for you",
        "Reconnect or share the integration, then schedule it again.",
    ),
    OUTCOME_VALIDATION: (
        "its stored content is no longer valid for the tool",
        "Schedule it again with the content prepared anew.",
    ),
    OUTCOME_APPROVAL: (
        "the tool now needs an approval before it runs",
        "Run it from the chat, where you can approve it.",
    ),
    OUTCOME_EXECUTION: (
        "the tool or the agent returned an error",
        "Open the run for the error, then schedule it again.",
    ),
    OUTCOME_QUOTA: (
        "the usage quota was reached",
        "Schedule it again once the quota renews.",
    ),
    OUTCOME_INTERRUPTED: (
        "it was stopped while it ran",
        "Open the run to see what was done before the stop.",
    ),
    OUTCOME_RETRIES_EXHAUSTED: (
        "it kept failing after its automatic retries",
        "Open the run for the error, then schedule it again.",
    ),
    OUTCOME_RESUME_EXHAUSTED: (
        "platform restarts cut it more often than it may go on by itself",
        "Open the run to see what was done; schedule a new one if it is still needed.",
    ),
}
_UNKNOWN_REASON = ("it did not complete", "Open the run for the details.")
#: Longest agent answer an instruction outcome carries.
_INSTRUCTION_ANSWER_CHARS = 1500


def short_time(at: datetime, tz_name: str | None) -> str:
    """``07:00 Paris on Mon 9 Nov 2026``: the person's wall clock, no offset arithmetic."""
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    zone = zone_of(tz_name)
    if zone is None:
        utc = at.astimezone(UTC)
        return f"{utc:%H:%M} UTC on {_day(utc)}"
    local = at.astimezone(zone)
    place = str(tz_name).rsplit("/", 1)[-1].replace("_", " ")
    return f"{local:%H:%M} {place} on {_day(local)}"


def outcome_text(item: WorkItem, tz_name: str | None) -> str:
    """The readable outcome posted to the conversation and sent as the notification.

    One sentence saying what happened, where and when, in the person's time
    zone; for an item that did not run, the reason in one clause and one way
    to fix it. No raw tool result and no identifier: those stay in
    ``item.result``, on the card and in the run. An instruction's answer is
    the agent's own prose and follows the sentence as written.
    """
    state = schedule_state(item) or item.status
    headline = _OUTCOME_HEADLINES.get(state, str(state).capitalize())
    action = action_of(item.payload) or {}
    target = str(action.get("target") or "").strip()
    what = f"{item.title} ({target})" if target and target not in item.title else item.title
    due = getattr(item, "due_at", None)
    ran = getattr(item, "completed_at", None) or due
    if state == "done":
        sentence = f"**{headline}**: {what}" + (f", at {short_time(ran, tz_name)}." if ran else ".")
    elif state == "missed":
        when = f", due at {short_time(due, tz_name)}," if due else ""
        minutes = int(grace_window().total_seconds() // 60)
        sentence = (
            f"**{headline}**: {what}{when} was not run: the platform could not start it within "
            f"{minutes} minutes of its time. Reschedule it to run it now, or cancel it."
        )
    elif state == "cancelled":
        sentence = f"**{headline}**: {what}. It was not run."
    else:
        reason, fix = _BLOCKED_REASONS.get(getattr(item, "outcome_code", None), _UNKNOWN_REASON)
        when = f" at {short_time(ran, tz_name)}" if ran else ""
        sentence = f"**{headline}**: {what}{when}, because {reason}. {fix}"
    lines = [sentence]
    if state == "done" and getattr(item, "action_mode", None) == "instruction":
        answer = (item.result or "").strip()
        if len(answer) > _INSTRUCTION_ANSWER_CHARS:
            answer = f"{answer[: _INSTRUCTION_ANSWER_CHARS - 1]}\u2026"
        if answer:
            lines.append(answer)
    restarts = restarts_of(item.payload)
    if restarts["resumed"] or restarts["requeued"]:
        # A restart is never the person's to handle: said, not asked.
        lines.append(
            f"Platform restarts cut it while it ran: it was resumed {restarts['resumed']} time(s) "
            f"and run again by its agent {restarts['requeued']} time(s), by itself."
        )
    return "\n\n".join(lines)


def _plain(text: str) -> str:
    """The outcome for the bell: the sentence without its Markdown emphasis."""
    return text.replace("**", "")


#: Message metadata key of a scheduled outcome: the run it reports on, as data.
SCHEDULED_OUTCOME_META_KEY = "scheduled_outcome"


def run_url(run_id: object) -> str:
    """The absolute URL of a run in the web app: a link that leaves the web
    app (an embedded chat in OpenCTI, OpenAEV or OpenCRQ, the widget, a Teams
    or Slack copy) never resolves a relative path against its host."""
    from app.services.email import platform_url

    return f"{platform_url()}/operations/executions/{run_id}"


def run_link_markdown(run_id: object) -> str:
    """The outcome's trailing link to its run, absolute; empty without a run."""
    return f"\n\n[View the run]({run_url(run_id)})" if run_id else ""


def outcome_ref(item: WorkItem, state: str) -> dict[str, str | None]:
    """What the web chat draws its "View run" action from (``scheduled_outcome``)."""
    run_id = getattr(item, "run_id", None)
    return {
        "work_item_id": str(item.id),
        "run_id": str(run_id) if run_id else None,
        "state": state,
    }


async def deliver_outcome(item_id: uuid.UUID) -> None:
    """Post a terminal outcome to the item's conversation; notify its creator
    when :func:`notifies_person` says the outcome is theirs.

    Best effort, after the outcome is committed: a delivery failure never
    changes what the item says happened.
    """
    from app.core.database import async_session
    from app.models.agent import Agent

    try:
        async with async_session() as db:
            item = (
                await db.execute(select(WorkItem).where(WorkItem.id == item_id))
            ).scalar_one_or_none()
            if item is None or item.user_id is None:
                return
            if item.status not in ("done", "blocked", "cancelled"):
                # Still going on by itself (resumed, or run again for its agent
                # after a restart): nothing to say until it ends.
                return
            agent_name = "Agent"
            if item.owner_agent_id:
                agent_name = (
                    await db.execute(select(Agent.name).where(Agent.id == item.owner_agent_id))
                ).scalar_one_or_none() or agent_name
            tz_name = getattr(await _user(db, item.user_id), "timezone", None)
            text = outcome_text(item, tz_name)
            state = schedule_state(item) or item.status
            run_link = run_link_markdown(getattr(item, "run_id", None))
            if notifies_person(item):
                from app.services.notify import notify_web_user

                escalated = escalates(item)
                headline = _OUTCOME_HEADLINES.get(state, state).lower()
                await notify_web_user(
                    db,
                    str(item.user_id),
                    title=(
                        f"Scheduled work item {headline}, escalated by {agent_name}: {item.title}"
                        if escalated
                        else f"Scheduled work item {headline}: {item.title}"
                    ),
                    # The readable sentence, as posted; the raw result stays on the item.
                    summary=_plain(text)[:1000] or None,
                    source_label=agent_name,
                    extra_data={
                        # Shown by the bell and pushed to a linked phone as a
                        # reminder (``services/notification_feed.py``).
                        "notification_source": "work_item",
                        "work_item_id": str(item.id),
                        "run_id": str(item.run_id) if item.run_id else None,
                        "conversation_id": str(item.conversation_id) if item.conversation_id else None,
                        "schedule_state": state,
                        "scheduled_by_agent": agent_name,
                        "escalated": escalated,
                    },
                )
            conv = None
            if item.conversation_id:
                from app.models.chat import Conversation

                conv = (
                    await db.execute(
                        select(Conversation).where(
                            Conversation.id == item.conversation_id,
                            Conversation.user_id == item.user_id,
                        )
                    )
                ).scalar_one_or_none()
            if conv is not None:
                from app.channels.router import store_message

                # One readable line and a link to the run, no card marker: the
                # card the scheduling turn posted reads the item by id and
                # already updates in place with the state and the raw proof, so
                # a second card here would show the same outcome twice. The run
                # is also stored as data (``scheduled_outcome``): the web chat
                # draws its own "View run" action from it and hides the link,
                # every other surface keeps the absolute link (#11950).
                await store_message(
                    db,
                    conv.id,
                    role="assistant",
                    content=f"{text}{run_link}",
                    channel=conv.channel or "web",
                    metadata_json=json.dumps({SCHEDULED_OUTCOME_META_KEY: outcome_ref(item, state)}),
                )
            await db.commit()
            channel = getattr(conv, "channel", None) if conv is not None else None
            thread = getattr(conv, "channel_thread_id", None) if conv is not None else None
        if channel and channel not in ("web", "web_embed"):
            from app.services.sub_agent import _send_proactive_channel_message

            await _send_proactive_channel_message(
                channel=channel,
                channel_thread_id=thread,
                text=f"{text}{run_link}",
                agent_name=agent_name,
            )
        await publish_update(item)
    except Exception as exc:
        logger.warning(
            "Could not deliver the outcome of scheduled work item %s: %s",
            item_id,
            format_exception_message(exc),
        )
