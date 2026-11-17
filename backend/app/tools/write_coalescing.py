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

"""Write coalescing: several updates of one object in one tool round.

A model that changes an event's body and then its location in two calls of the
same round makes the provider send two update notifications to every
attendee within the same minute. The loop hands each validated round to
:func:`coalesce_write_calls` before dispatch: the calls of a tool that
declares ``ToolDefinition.coalesce`` (:class:`~app.tools.base.WriteCoalescing`)
and name the same object (same tool, same identity arguments) are merged into
the FIRST of them, which runs once with the merged arguments; every other
call of the group is answered by the loop, naming the call that carried its
patch.

The merge follows the tool's declared mode:

- ``patch`` (an omitted or empty argument means "leave it unchanged"): the
  later non-empty values are laid over the earlier ones; a declared
  ``union_lists`` argument is the union, in order.
- ``replace`` (the call sends the whole object): the last call's arguments
  are the ones sent.

A call of the round that is not part of the group but names the same object
(a read, a delete, a reply) closes the group, so what the round reads or
deletes in between still sees the writes before it. The module names no tool:
what is coalescible is declared on each integration's definitions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.tools.base import ToolCall, WriteCoalescing

#: The declared coalescing of a tool, by the name the executor runs.
CoalescingLookup = Callable[[str], WriteCoalescing | None]


@dataclass
class CoalescedRound:
    """The round to dispatch and the calls the loop answers itself.

    ``calls`` keeps the round's order; a leader is a copy of its call carrying
    the merged arguments (the assistant message keeps what the model wrote).
    ``absorbed`` pairs every absorbed call with the leader that carries its
    patch, in the order they were asked.
    """

    calls: list[ToolCall]
    absorbed: list[tuple[ToolCall, ToolCall]] = field(default_factory=list)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _identity(call: ToolCall, spec: WriteCoalescing) -> tuple[str, ...] | None:
    args = call.arguments if isinstance(call.arguments, dict) else {}
    if not spec.identity or not _text(args.get(spec.identity[0])):
        return None
    return tuple(_text(args.get(key)) for key in spec.identity)


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def merge_arguments(base: dict[str, Any], later: dict[str, Any], spec: WriteCoalescing) -> dict[str, Any]:
    """Lay ``later`` over ``base`` per the declared mode (module docstring)."""
    if spec.mode == "replace":
        return dict(later)
    merged = dict(base)
    for key, value in later.items():
        earlier = merged.get(key)
        if key in spec.union_lists and isinstance(value, list):
            merged[key] = list(dict.fromkeys([*(earlier if isinstance(earlier, list) else []), *value]))
        elif not _is_empty(value):
            merged[key] = value
        else:
            merged.setdefault(key, value)
    return merged


def _named_values(call: ToolCall) -> set[str]:
    """Every non-empty string argument of a call: what an object id is matched on."""
    args = call.arguments if isinstance(call.arguments, dict) else {}
    return {value.strip() for value in args.values() if isinstance(value, str) and value.strip()}


def coalesce_write_calls(
    calls: list[ToolCall],
    *,
    coalescing_for: CoalescingLookup,
    original_name: Callable[[str], str] | None = None,
) -> CoalescedRound:
    """Merge the repeated updates of one object in a validated round.

    ``coalescing_for`` answers a tool's declared :class:`WriteCoalescing`
    (``None`` for every other tool); ``original_name`` maps the LLM-visible
    name to the executor's (the loop's ``_tool_name_map``), so two spellings
    of one tool are one group. A round without two updates of one object
    comes back unchanged (``absorbed`` empty).
    """
    resolve = original_name or (lambda name: name)
    out: list[ToolCall] = []
    absorbed: list[tuple[ToolCall, ToolCall]] = []
    open_groups: dict[tuple[str, tuple[str, ...]], tuple[ToolCall, WriteCoalescing]] = {}
    for call in calls:
        name = resolve(call.name)
        spec = coalescing_for(name)
        identity = _identity(call, spec) if spec is not None else None
        if spec is None or identity is None:
            # Not a coalescible update: a call naming an open group's object
            # closes it, so the round's reads and deletes keep their order.
            named = _named_values(call)
            for key in [k for k in open_groups if k[1][0] in named]:
                open_groups.pop(key, None)
            out.append(call)
            continue
        key = (name, identity)
        if key in open_groups:
            leader, _spec = open_groups[key]
            leader.arguments = merge_arguments(dict(leader.arguments or {}), dict(call.arguments or {}), spec)
            absorbed.append((call, leader))
            continue
        leader = ToolCall(id=call.id, name=call.name, arguments=dict(call.arguments or {}))
        open_groups[key] = (leader, spec)
        out.append(leader)
    if not absorbed:
        return CoalescedRound(calls=list(calls))
    return CoalescedRound(calls=out, absorbed=absorbed)


def format_coalesced_answer(leader: ToolCall) -> str:
    """The tool result of an absorbed call: one line, naming the carrier."""
    return (
        f"[Write coalescing] Not sent on its own: this update of the same object as call "
        f"{leader.id} in this round was merged into that call, which the provider receives once "
        f"(one update, one notification). Read call {leader.id}'s result for the object as updated; "
        "do not repeat this call."
    )
