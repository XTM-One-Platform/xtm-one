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

"""The tool-capable activation gate of an assignment: the one module for the rule.

An assignment may declare ``activation_check``: one READ-ONLY tool call and a
condition on its result, evaluated by the platform with no model call::

    {"tool": "list_work_items",
     "arguments": {"owner_agent_id": "<agent id>", "open_only": true},
     "condition": "non_empty"}

When the agent binds several integrations of one provider, their tools carry a
per-instance suffix (``list_calendar_events__corp``). A check names one
instance's tool by its BASE name plus the integration id, either
``"tool": "list_calendar_events@<integration id>"`` or
``"tool": "list_calendar_events", "integration_id": "<integration id>"``; the
live name is resolved when the check runs, so the gate survives a change of
the suffix. The suffixed literal stays accepted.

The condition is ``non_empty`` (the default), ``empty``, or a path condition
``{"path": "$.status", "in": [...]}`` (also ``not_in`` or ``none_in`` with a
list, or ``contains`` with a string). A path is ``$`` followed by ``.key``,
``[n]`` and ``[*]`` segments; a ``.key`` read on a list reads it on each
element, so ``$.status`` on a list of work items is the status of each item.
``in`` and ``contains`` pass when ANY value the path reaches matches;
``not_in`` passes when the path reaches at least one value and none is in the
list; ``none_in`` passes when no value the path reaches is in the list, a path
that reaches nothing included ("go ahead unless the lease is held by a live
run": ``{"path": "$.held_by_run.alive", "none_in": [true]}``). A lease the
run holds itself is not another holder: when the result's ``held_by_run.id``
is the run evaluating the check, ``held_by_run`` is left out before the
condition is tested (#8831).

The arguments may name the run's trigger payload with ``{{payload.<a.b.c>}}``
(an event-driven run: ``{{payload.pull_request.head.ref}}``). A field the
payload does not carry, or that is not a plain value, leaves the check not
evaluated: the run proceeds.

Date placeholders name a day relative to the evaluation, in the assignment
trigger's timezone (``trigger_config.timezone``, else the agent owner's, else
UTC): ``{{date.today}}`` and ``{{date.today+N}}`` / ``{{date.today-N}}``
(``YYYY-MM-DD``), ``{{datetime.day_start+N}}`` and ``{{datetime.day_end+N}}``
(ISO 8601 with the offset of that local day, so a window crossing a DST change
stays one local day). ``N`` is 0 to 366. A ``{{...}}`` that is none of the
placeholders is refused at save, with the valid list (#10319).

A check may chain up to three more checks of the same shape under ``also``
(``{"tool": ..., "condition": ..., "also": [{"tool": ..., ...}]}``),
evaluated in order after the first one, still with no model call: the run is
skipped at the first check whose condition fails, so an event-driven run with
two independent reasons to stand down (a live lease, a claim another run took)
spends no token on either (#10597). An extra check that cannot be evaluated
is passed over like the first one; ``also`` does not nest.

A check may instead list up to three alternatives under ``or``
(``{"tool": ..., "condition": ..., "or": [{"tool": ..., ...}]}``): the run
goes ahead when ANY of the checks passes, the first included, and is skipped
only when every one of them fails, its summary naming each check that failed.
They are evaluated in order and the first that passes ends the evaluation; a
check that cannot be evaluated lets the run go ahead, as a single check does.
``or`` and ``also`` are not combined in one check, and ``or`` does not nest.

The check runs before the run is charged, before any model is resolved and
before the LLM activation gate (``activation_prompt``), which stays the second
stage when both are set. A check whose condition fails ends the run as
skipped with ``Skipped: gate check <tool> ...``, no token spent; in a chain the
summary ends with ``(check N of M: <tool> <resolved arguments>)`` (#10724). A
check that cannot be evaluated - the agent holds no such tool, the tool errors,
or its result carries no structured value - never blocks the run: the run
proceeds, the check is named in the run's
``trigger_context.gate_checks_not_evaluated`` and counted under the
assignment's drop reasons.

The tool is called through the same executors a run builds for the agent, so
the check can never read more than the agent could. Its result is read from
``ToolResult.structured`` (a tool that serves gate checks sets it), else from
its text when that text is JSON.

Everything that writes the setting (the assignments REST API, the agent-builder
``create_assignment`` / ``update_assignment`` tools) parses it here.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any

from app.services.tool_classification import infer_tool_type
from app.tools.base import ToolCall, ToolResult

#: How every skipped run's ``result_summary`` starts.
SKIP_SUMMARY_PREFIX = "Skipped: gate check"

_SHAPE_CONDITIONS = frozenset({"non_empty", "empty"})
_LIST_OPERATORS = frozenset({"in", "not_in", "none_in"})
_TEXT_OPERATORS = frozenset({"contains"})
_PATH_OPERATORS = _LIST_OPERATORS | _TEXT_OPERATORS
_KEYS = frozenset({"tool", "arguments", "condition", "integration_id", "also", "or"})
#: The keys that list further checks: ``also`` (all must pass) and ``or`` (one must pass).
_LIST_KEYS = ("also", "or")
#: How many checks ``also`` or ``or`` may list after the first one.
MAX_EXTRA_CHECKS = 3

_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,127}$")
_TOOL_REFERENCE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,127}(@[0-9A-Fa-f\-]{32,36})?$")
_PATH_SEGMENT = re.compile(r"\.([A-Za-z_][A-Za-z0-9_\-]*)|\[(\d+|\*)\]")
_PAYLOAD_PLACEHOLDER = re.compile(r"\{\{\s*payload\.([A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*)\s*\}\}")
_DATE_PLACEHOLDER = re.compile(
    r"\{\{\s*(date\.today|datetime\.day_start|datetime\.day_end)\s*(?:([+-])\s*(\d{1,3}))?\s*\}\}"
)
_ANY_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}")
_ID_PLACEHOLDERS = frozenset({"agent.id", "assignment.id"})
_MAX_DAY_OFFSET = 366
PLACEHOLDER_HELP = (
    "{{agent.id}}, {{assignment.id}}, {{payload.a.b}}, {{date.today}}, {{date.today+N}}, "
    "{{date.today-N}}, {{datetime.day_start+N}}, {{datetime.day_end+N}} (N from 0 to 366)"
)
_MAX_ARGUMENTS_BYTES = 4096
_MAX_OPTIONS = 50
_PREVIEW_CHARS = 2000

_FORMAT_HELP = (
    'Use {"tool": "READ_ONLY_TOOL", "arguments": {...}, "condition": '
    '"non_empty" | "empty" | {"path": "$.field", "in": [...]}}.'
)


@dataclass(frozen=True)
class ActivationCheckOutcome:
    """What a check decided. ``passed`` is True when the run goes ahead,
    which includes a check that could not be evaluated (``error`` says why)."""

    passed: bool
    summary: str
    error: str | None = None
    #: The tool's result as text, capped, for the run record.
    preview: str = ""
    #: The arguments the tool was called with, placeholders resolved
    #: (``None`` when it was not called): what a skip summary names (#10724).
    arguments: dict | None = None
    #: The run id a failed check read as the LIVE holder of the entry's lease
    #: (``held_by_run`` alive, never the evaluating run): the event the run
    #: stood down on is delivered again once that run ends (#11988).
    live_holder: str | None = None
    #: A permanent configuration error (#1400): the agent holds no such tool,
    #: or the object the check reads no longer exists. The run is skipped,
    #: never let through, and the reason is named (``error``).
    misconfigured: bool = False


#: The structured flag a gate tool sets when the object it was asked about
#: does not exist (or the caller may not read it): a configuration error.
TARGET_MISSING_KEY = "target_missing"


def _path_segments(path: str) -> list[str | int] | None:
    """The segments of *path*, ``None`` when it is not ``$`` plus segments."""
    if not path.startswith("$"):
        return None
    rest = path[1:]
    segments: list[str | int] = []
    position = 0
    while position < len(rest):
        match = _PATH_SEGMENT.match(rest, position)
        if match is None:
            return None
        key, index = match.group(1), match.group(2)
        if key is not None:
            segments.append(key)
        elif index == "*":
            segments.append("*")
        else:
            segments.append(int(index))
        position = match.end()
    return segments


def _condition_error(condition: Any) -> str | None:
    if isinstance(condition, str):
        if condition in _SHAPE_CONDITIONS:
            return None
        return f"condition must be 'non_empty', 'empty' or a path condition, not {condition!r}."
    if not isinstance(condition, dict):
        return "condition must be 'non_empty', 'empty' or a path condition object."
    path = condition.get("path")
    if not isinstance(path, str) or _path_segments(path.strip()) is None:
        return "a path condition needs a 'path' such as '$.status' or '$[*].subject'."
    operators = [key for key in condition if key != "path"]
    if len(operators) != 1 or operators[0] not in _PATH_OPERATORS:
        return "a path condition holds 'path' and exactly one of 'in', 'not_in', 'none_in' or 'contains'."
    operator = operators[0]
    value = condition[operator]
    if operator in _LIST_OPERATORS:
        if not isinstance(value, list) or not value or len(value) > _MAX_OPTIONS:
            return f"'{operator}' takes a list of 1 to {_MAX_OPTIONS} values."
        if any(isinstance(option, (dict, list)) for option in value):
            return f"'{operator}' compares plain values (strings, numbers, booleans, null)."
    elif not isinstance(value, str) or not value:
        return "'contains' takes a non-empty string."
    return None


def _parse_extra_checks(raw: object, *, strict_placeholders: bool, key: str = "also") -> tuple[list[dict], str | None]:
    """The ``also`` (or ``or``) list parsed check by check, or the first error met."""
    if raw is None or raw == []:
        return [], None
    if not isinstance(raw, list) or len(raw) > MAX_EXTRA_CHECKS:
        return [], f"activation_check {key} takes a list of 1 to {MAX_EXTRA_CHECKS} checks."
    checks: list[dict] = []
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict) or not item:
            return [], f"activation_check {key}[{position}] must be a check object. {_FORMAT_HELP}"
        nested = next((name for name in _LIST_KEYS if name in item), None)
        if nested is not None:
            return [], f"activation_check {key}[{position}] cannot hold its own {nested} list."
        parsed, error, _provided = parse_activation_check(item, strict_placeholders=strict_placeholders)
        if error is not None or parsed is None:
            return [], f"{key}[{position}]: {error}"
        checks.append(parsed)
    return checks, None


def parse_activation_check(raw: object, *, strict_placeholders: bool = True) -> tuple[dict | None, str | None, bool]:
    """``(value, error, provided)`` for an ``activation_check`` argument.

    A save refuses a ``{{...}}`` placeholder it does not know; the engine reads
    a stored check with ``strict_placeholders=False`` so a check saved before
    the rule keeps evaluating as it did.

    Accepts the object or its JSON text. ``none`` / ``clear`` (or ``null``, an
    empty object) clears it; a blank value is ``provided=False`` and leaves the
    setting unchanged. The value returned is normalized: ``arguments`` defaults
    to ``{}`` and ``condition`` to ``non_empty``.
    """
    if raw is None:
        return None, None, False
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, None, False
        if text.lower() in ("none", "clear", "null"):
            return None, None, True
        try:
            raw = json.loads(text)
        except ValueError:
            return None, f"activation_check is not valid JSON. {_FORMAT_HELP}", True
        if raw is None:
            return None, None, True
    if not isinstance(raw, dict):
        return None, f"activation_check must be an object. {_FORMAT_HELP}", True
    if not raw:
        return None, None, True
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        return None, f"activation_check has unknown key(s) {', '.join(unknown)}. {_FORMAT_HELP}", True
    tool = raw.get("tool")
    if not isinstance(tool, str) or not _TOOL_REFERENCE.match(tool.strip()):
        return None, f"activation_check needs the name of the tool to call. {_FORMAT_HELP}", True
    tool = tool.strip()
    integration_id = raw.get("integration_id")
    if "@" in tool:
        tool, _, from_tool = tool.partition("@")
        if integration_id not in (None, "", from_tool):
            return None, "activation_check names two different integrations (tool@id and integration_id).", True
        integration_id = from_tool
    if integration_id in (None, ""):
        integration_id = None
    else:
        try:
            integration_id = str(uuid.UUID(str(integration_id).strip()))
        except ValueError:
            return None, "activation_check integration_id must be an integration's UUID.", True
    if not _TOOL_NAME.match(tool):
        return None, f"activation_check needs the name of the tool to call. {_FORMAT_HELP}", True
    if infer_tool_type(tool) != "read":
        return None, (
            f"activation_check calls read-only tools only: '{tool}' reads as a tool that "
            "changes something. Name a list / get / search tool."
        ), True
    arguments = raw.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return None, "activation_check arguments must be an object of the tool's parameters.", True
    try:
        encoded = json.dumps(arguments)
    except (TypeError, ValueError):
        return None, "activation_check arguments must be plain JSON values.", True
    if len(encoded) > _MAX_ARGUMENTS_BYTES:
        return None, f"activation_check arguments must stay under {_MAX_ARGUMENTS_BYTES} bytes.", True
    if strict_placeholders:
        bad = unknown_placeholders(arguments)
        if bad:
            return (
                None,
                (
                    f"activation_check arguments hold unknown placeholder(s) {', '.join(bad)}. "
                    f"Valid placeholders: {PLACEHOLDER_HELP}."
                ),
                True,
            )
    condition = raw.get("condition", "non_empty")
    if isinstance(condition, str):
        condition = condition.strip().lower()
    error = _condition_error(condition)
    if error is not None:
        return None, f"activation_check {error}", True
    if isinstance(condition, dict):
        condition = {key: (value.strip() if key == "path" else value) for key, value in condition.items()}
    if raw.get("also") not in (None, []) and raw.get("or") not in (None, []):
        return (
            None,
            "activation_check takes 'also' (every check must pass) or 'or' (one check must pass), not both.",
            True,
        )
    extra, error = _parse_extra_checks(raw.get("also"), strict_placeholders=strict_placeholders)
    if error is not None:
        return None, error, True
    alternatives, error = _parse_extra_checks(raw.get("or"), strict_placeholders=strict_placeholders, key="or")
    if error is not None:
        return None, error, True
    parsed: dict = {"tool": tool, "arguments": json.loads(encoded), "condition": condition}
    if integration_id is not None:
        parsed["integration_id"] = integration_id
    if extra:
        parsed["also"] = extra
    if alternatives:
        parsed["or"] = alternatives
    return parsed, None, True


def chained_checks(check: dict) -> list[dict]:
    """*check* and the checks its ``also`` (or ``or``) lists, in evaluation order, each without them."""
    first = {key: value for key, value in check.items() if key not in _LIST_KEYS}
    listed = check.get("or") if is_any_of(check) else check.get("also")
    return [first, *[item for item in listed or [] if isinstance(item, dict)]]


def is_any_of(check: dict) -> bool:
    """Whether *check* lists alternatives under ``or``: the run goes ahead when one of them passes."""
    return bool(check.get("or"))


def stored_activation_check(value: object) -> dict | None:
    """The stored check as the engine reads it, ``None`` when there is none or it is unusable."""
    parsed, error, _provided = parse_activation_check(value, strict_placeholders=False)
    return parsed if error is None else None


def _is_empty(value: Any) -> bool:
    """``null``, ``false``, ``0`` and an empty string, list or object."""
    if value is None or value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    if isinstance(value, (str, list, dict, tuple)):
        return len(value) == 0
    return False


def _resolve(value: Any, segments: list[str | int]) -> list[Any]:
    current = [value]
    for segment in segments:
        reached: list[Any] = []
        for item in current:
            if segment == "*":
                if isinstance(item, list):
                    reached.extend(item)
                elif isinstance(item, dict):
                    reached.extend(item.values())
            elif isinstance(segment, int):
                if isinstance(item, list) and -len(item) <= segment < len(item):
                    reached.append(item[segment])
            elif isinstance(item, dict):
                if segment in item:
                    reached.append(item[segment])
            elif isinstance(item, list):
                reached.extend(element[segment] for element in item if isinstance(element, dict) and segment in element)
        current = reached
    return current


def _same(value: Any, option: Any) -> bool:
    if isinstance(value, str) and isinstance(option, str):
        return value.casefold() == option.casefold()
    if isinstance(value, bool) or isinstance(option, bool):
        return value is option
    return value == option


def condition_met(data: Any, condition: Any) -> bool:
    """Whether the tool's structured result *data* meets *condition*."""
    if condition == "empty":
        return _is_empty(data)
    if not isinstance(condition, dict):
        return not _is_empty(data)
    values = _resolve(data, _path_segments(condition["path"]) or [])
    if "in" in condition:
        return any(_same(value, option) for value in values for option in condition["in"])
    if "not_in" in condition:
        return bool(values) and not any(_same(value, option) for value in values for option in condition["not_in"])
    if "none_in" in condition:
        return not any(_same(value, option) for value in values for option in condition["none_in"])
    needle = str(condition["contains"]).casefold()
    return any(isinstance(value, str) and needle in value.casefold() for value in values)


def describe_condition(condition: Any) -> str:
    """The condition as one short line, for the run record and ``get_assignment``."""
    if isinstance(condition, dict):
        operator = next(key for key in condition if key != "path")
        return f"{condition['path']} {operator.replace('_', ' ')} {json.dumps(condition[operator])}"
    return str(condition or "non_empty").replace("_", "-")


def check_tool_label(check: dict) -> str:
    """The check's tool as written: ``tool`` or ``tool@<integration id>``."""
    integration_id = check.get("integration_id")
    return f"{check.get('tool')}@{integration_id}" if integration_id else str(check.get("tool"))


def describe_check(check: dict) -> str:
    """A stored check as one line: the call and its condition."""
    lines = []
    for item in chained_checks(check):
        arguments = json.dumps(item.get("arguments") or {})
        lines.append(f"{check_tool_label(item)}({arguments}), runs when {describe_condition(item.get('condition'))}")
    if is_any_of(check):
        return "any of: " + "; or ".join(lines)
    return "; then ".join(lines)


def skip_summary(tool: str, condition: Any) -> str:
    """The ``result_summary`` of a run whose check failed."""
    if condition == "empty":
        return f"{SKIP_SUMMARY_PREFIX} {tool} returned a non-empty result"
    if isinstance(condition, dict):
        operator = next(key for key in condition if key != "path")
        if operator == "none_in":
            return f"{SKIP_SUMMARY_PREFIX} {tool}: {condition['path']} is in {json.dumps(condition[operator])}"
        expected = {"in": "in", "not_in": "outside", "contains": "containing"}[operator]
        return f"{SKIP_SUMMARY_PREFIX} {tool}: no {condition['path']} {expected} {json.dumps(condition[operator])}"
    return f"{SKIP_SUMMARY_PREFIX} {tool} returned empty"


def structured_result(result: ToolResult) -> tuple[Any, bool]:
    """The structured value of *result*, and whether it has one.

    ``ToolResult.structured`` when the tool set it, else its text when that
    text is a JSON object or array.
    """
    if result.structured is not None:
        return result.structured, True
    text = (result.content or "").strip()
    if text[:1] in ("{", "["):
        try:
            return json.loads(text), True
        except ValueError:
            return None, False
    return None, False


def _preview(result: ToolResult) -> str:
    content = result.content or ""
    return content if len(content) <= _PREVIEW_CHARS else content[:_PREVIEW_CHARS] + "..."


def without_own_hold(data: Any, run_id: str | None) -> Any:
    """*data* without its ``held_by_run`` when that holder is *run_id* itself.

    A shared-state read names the run holding the entry's lease; the run
    evaluating a gate check is never the live holder it waits on (#8831).
    """
    if not run_id or not isinstance(data, dict):
        return data
    holder = data.get("held_by_run")
    if isinstance(holder, dict) and str(holder.get("id") or "") == str(run_id):
        return {k: v for k, v in data.items() if k != "held_by_run"}
    return data


def live_holder_id(data: Any) -> str | None:
    """The id of the live run *data* names under ``held_by_run``, ``None`` when it names none alive."""
    if not isinstance(data, dict):
        return None
    holder = data.get("held_by_run")
    if not isinstance(holder, dict) or holder.get("alive") is not True:
        return None
    holder_id = str(holder.get("id") or "").strip()
    return holder_id or None


class UnresolvedPlaceholderError(ValueError):
    """A ``{{payload.<path>}}`` the run's trigger payload does not carry as a plain value."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


def _payload_value(payload: Any, path: str) -> str:
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise UnresolvedPlaceholderError(path)
        current = current[part]
    if current is None or isinstance(current, (dict, list)):
        raise UnresolvedPlaceholderError(path)
    if isinstance(current, bool):
        return "true" if current else "false"
    return str(current)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    return []


def _known_placeholder(body: str) -> bool:
    text = "{{" + body + "}}"
    if body.strip() in _ID_PLACEHOLDERS or _PAYLOAD_PLACEHOLDER.fullmatch(text):
        return True
    match = _DATE_PLACEHOLDER.fullmatch(text)
    return match is not None and int(match.group(3) or 0) <= _MAX_DAY_OFFSET


def unknown_placeholders(arguments: Any) -> list[str]:
    """Every ``{{...}}`` of *arguments*' string values that is not a placeholder the check resolves."""
    bad: list[str] = []
    for text in _strings(arguments):
        for match in _ANY_PLACEHOLDER.finditer(text):
            if not _known_placeholder(match.group(1)) and match.group(0) not in bad:
                bad.append(match.group(0))
    return bad


def uses_date_placeholders(arguments: Any) -> bool:
    """Whether a string value of *arguments* names a date placeholder."""
    return any(_DATE_PLACEHOLDER.search(text) for text in _strings(arguments))


def _date_value(match: re.Match[str], now: datetime, zone: tzinfo) -> str:
    name, sign, digits = match.group(1), match.group(2), match.group(3)
    offset = int(digits or 0)
    if offset > _MAX_DAY_OFFSET:
        return match.group(0)
    day = now.astimezone(zone).date() + timedelta(days=-offset if sign == "-" else offset)
    if name == "date.today":
        return day.isoformat()
    # The wall time is fixed first, then the zone gives that local day its own
    # offset: a day after a DST change gets the new offset, not today's.
    wall = datetime.combine(day, time.min if name == "datetime.day_start" else time(23, 59, 59))
    return wall.replace(tzinfo=zone).isoformat()


def resolved_arguments(
    arguments: dict,
    placeholders: dict[str, str],
    payload: Any = None,
    *,
    now: datetime | None = None,
    zone: tzinfo | None = None,
) -> dict:
    """*arguments* with each ``{{name}}`` of *placeholders* replaced in string values.

    ``{{agent.id}}`` and ``{{assignment.id}}`` let a check name the agent it
    runs for, so a duplicated or cloned assignment checks its own agent.
    ``{{payload.<a.b.c>}}`` names a plain value of the run's trigger *payload*;
    one it does not carry raises ``UnresolvedPlaceholderError``. The date
    placeholders resolve against *now* (default: the current instant) in
    *zone* (default UTC).
    """
    instant = now or datetime.now(UTC)
    local_zone = zone or UTC

    def _resolve_value(value: Any) -> Any:
        if isinstance(value, str):
            for name, replacement in placeholders.items():
                value = value.replace("{{" + name + "}}", replacement)
            value = _DATE_PLACEHOLDER.sub(lambda match: _date_value(match, instant, local_zone), value)
            return _PAYLOAD_PLACEHOLDER.sub(lambda match: _payload_value(payload, match.group(1)), value)
        if isinstance(value, list):
            return [_resolve_value(item) for item in value]
        if isinstance(value, dict):
            return {key: _resolve_value(item) for key, item in value.items()}
        return value

    return {key: _resolve_value(value) for key, value in arguments.items()}


def resolve_check_tool(check: dict, executor: Any) -> str | None:
    """The registered name of the check's tool on *executor*, ``None`` when it holds none.

    With an ``integration_id`` the tool is the one built for that integration
    whose base name (before any multi-instance suffix) is ``tool``, whatever
    its suffix is today; else the name resolves as a model call would.
    """
    tool = check["tool"]
    integration_id = check.get("integration_id")
    if not integration_id:
        return executor.resolve_canonical_name(tool)
    for definition in executor.all_tools:
        if str(definition.entity_id or "") == integration_id and (definition.base_name or definition.name) == tool:
            return definition.name
    return None


async def run_activation_check(
    check: dict,
    executor: Any,
    *,
    placeholders: dict[str, str] | None = None,
    payload: Any = None,
    run_id: str | None = None,
    zone: tzinfo | None = None,
    now: datetime | None = None,
) -> ActivationCheckOutcome:
    """Call the check's tool through *executor* and decide.

    *executor* is a ``CompositeToolExecutor`` holding the agent's tools,
    *payload* the run's trigger payload, *run_id* the run evaluating the
    check (a hold of its own is not read as another holder) and *zone* the
    timezone the date placeholders resolve in (UTC when ``None``). The run
    goes ahead whenever the check cannot be evaluated.
    """
    tool = check_tool_label(check)
    condition = check.get("condition", "non_empty")
    resolved = resolve_check_tool(check, executor)
    if resolved is None:
        reason = f"the agent holds no tool named {tool} a gate check can call"
        return ActivationCheckOutcome(
            False,
            f"{SKIP_SUMMARY_PREFIX} {tool} misconfigured: {reason}",
            error=reason,
            misconfigured=True,
        )
    try:
        arguments = resolved_arguments(
            dict(check.get("arguments") or {}), placeholders or {}, payload, now=now, zone=zone
        )
    except UnresolvedPlaceholderError as exc:
        return ActivationCheckOutcome(
            True,
            f"Gate check {tool} not evaluated",
            error=f"the trigger payload carries no plain value at {exc.path}",
        )
    result = await executor.execute_tool(
        ToolCall(id=f"activation-check-{uuid.uuid4().hex[:12]}", name=resolved, arguments=arguments)
    )
    preview = _preview(result)
    if result.is_error:
        return ActivationCheckOutcome(
            True,
            f"Gate check {tool} not evaluated",
            error=f"{tool} failed: {preview[:300]}",
            preview=preview,
            arguments=arguments,
        )
    data, readable = structured_result(result)
    if not readable:
        return ActivationCheckOutcome(
            True,
            f"Gate check {tool} not evaluated",
            error=f"{tool} returned no structured result to test the condition on",
            preview=preview,
            arguments=arguments,
        )
    if isinstance(data, dict) and data.get(TARGET_MISSING_KEY) is True:
        reason = f"{tool} found no object for {json.dumps(arguments, sort_keys=True, default=str)}: it no longer exists"
        return ActivationCheckOutcome(
            False,
            f"{SKIP_SUMMARY_PREFIX} {tool} misconfigured: {reason}",
            error=reason,
            preview=preview,
            arguments=arguments,
            misconfigured=True,
        )
    data = without_own_hold(data, run_id)
    if condition_met(data, condition):
        return ActivationCheckOutcome(
            True, f"Gate check {tool} passed ({describe_condition(condition)})", preview=preview, arguments=arguments
        )
    return ActivationCheckOutcome(
        False,
        skip_summary(tool, condition),
        preview=preview,
        arguments=arguments,
        live_holder=live_holder_id(data),
    )


def chain_position(summary: str, *, position: int, total: int, tool: str, arguments: dict | None) -> str:
    """*summary* naming which check of a chain failed, as is for a single check (#10724).

    Two checks of one chain may call the same tool (a lease, then a per-head
    claim): the position and the resolved arguments tell them apart.
    """
    if total <= 1:
        return summary
    called = json.dumps(arguments or {}, sort_keys=True, default=str)
    return f"{summary} (check {position} of {total}: {tool} {called})"


def any_of_skip_summary(failures: list[tuple[str, str, dict | None]]) -> str:
    """The ``result_summary`` of a run whose ``or`` checks all failed.

    *failures* holds ``(tool label, outcome summary, resolved arguments)`` per
    check, in evaluation order: each check is named with what it found.
    """
    parts = []
    for position, (tool, summary, arguments) in enumerate(failures, start=1):
        reason = summary.removeprefix(SKIP_SUMMARY_PREFIX).strip(" :")
        called = json.dumps(arguments or {}, sort_keys=True, default=str)
        parts.append(f"check {position}: {tool} {called}: {reason}")
    return f"{SKIP_SUMMARY_PREFIX}s: none of {len(failures)} passed ({'; '.join(parts)})"


def unevaluated_reason(position: int, tool: str) -> str:
    """The drop reason counted on the assignment for a check passed over (no payload value in it)."""
    return f"gate check {position} not evaluated: {tool}"
