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

"""Built-in work-item tools.

Exposes the durable work-item queue and assignment-targeted delegation to the
agentic loop, gated behind the ``work_items`` built-in tool id:

* ``create_work_item`` / ``update_work_item`` - create or mutate an owned,
  trackable unit of work that survives the run. ``owner_agent_id`` is a routing
  label, not a trigger.
* ``list_work_items`` - exact-match queue queries (owner / status / correlation
  key, or just the stale ones), each item with the start of its description.
* ``get_work_item`` - one item in full: payload, result, the files its creator
  attached, registered for ``read_file_content``, and its description,
  bounded when long and readable by window.
* ``claim_work_item`` - atomic claim so two agents never double-process an item.
* ``complete_work_item`` - close it out with a result.
* ``delegate_to_assignment`` - enqueue a tracked, observable run of a SPECIFIC
  assignment (not a one-shot persona reply) - the B3 fix.

Work items are pure shared data: they never auto-run anyone. Pickup is via the
agentic workflow (the owner's scheduled sweep, or an explicit handover /
``delegate_to_assignment``).

A company-managed item is read by every agent that sees it and written - or
claimed - only by a company-managed agent (``services/company_managed_writes.py``);
a personal agent's write of one is refused with the reason and writes nothing.
The agent that created a company-managed item may narrow which agents read and
write it: the listing leaves out the items this agent may not read and marks
the ones it may not change.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.core.utils import format_exception_message
from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES
from app.services.company_managed_writes import (
    CompanyManagedWriteRefusedError,
    agent_writer,
)
from app.services.coordination_scope import SCOPE_SCHEMA
from app.services.work_item_fleet_impact import FLEET_IMPACT_KINDS
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult, WriteReadBack

from ._shared import list_available_files, logger
from .paging import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, answer_budget, page_bounds, page_line, rows_that_fit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.work_item import WorkItemOrigin
    from app.tools.base import FileContext

#: How much of each description ``list_work_items`` shows; ``get_work_item``
#: returns the rest.
LIST_DESCRIPTION_PREVIEW_CHARS = 500

#: The longest ``list_work_items`` ``query``: a title or key fragment, not a text.
LIST_QUERY_MAX_CHARS = 200

#: How much of a long description ``get_work_item`` returns when no window is
#: asked: its head, where ``update_work_item(note=...)`` prepends the
#: latest progress notes, and its tail, where the original brief stays. The
#: characters between are named with the offset that reads them.
DESCRIPTION_HEAD_CHARS = 16_000
DESCRIPTION_TAIL_CHARS = 8_000
#: The largest window ``get_work_item(offset=..., max_chars=...)`` returns. The
#: agent loop cuts any tool result from its end to ``max_tool_result_chars``
#: (57,600 characters on the 128K-token fallback window), so a window stays
#: under that with room for the item's other fields; the description comes
#: last and its first line names the next offset, so a cut never hides how to
#: continue.
DESCRIPTION_WINDOW_MAX_CHARS = 40_000
#: The whole ``get_work_item`` answer stays under this, and under the running
#: loop's own tool-result cap when one is bound (``tool_result_char_cap``,
#: 3,686 characters on an 8K-token model): the description gets only what
#: the item's other fields leave of it, and the line saying what part is
#: shown and how to read on sits right under the status line, above every
#: unbounded field, so an end cut never removes it.
RESULT_BUDGET_CHARS = 50_000
#: Below this much room the default view shows no description body: its
#: status line says so and names ``offset=0``, which reads it alone.
DESCRIPTION_MIN_CHARS = 200
#: Room kept for the description's status line, header and marker.
_DESCRIPTION_FRAME_CHARS = 1_000
#: The share of the answer's budget each of the claim notes, the payload, the
#: result and the file list may take in the default view (an eighth each, so
#: the four together never take more than half): a larger one is cut with a
#: marker naming what was left out, so no field can push the answer past its
#: budget.
_FIELD_SHARE_DIVISOR = 8
#: Room kept for the marker ``_clipped`` appends (about 85 characters).
_CLIP_MARKER_CHARS = 100
#: How much of the title ``get_work_item`` shows on its first line, so the
#: status and description lines under it survive a low tool-result cap.
_TITLE_MAX_CHARS = 300


def _clipped(text: str, limit: int, what: str) -> str:
    """*text* cut to *limit* characters with a marker naming *what* and how
    much was left out; *text* itself when it fits."""
    if len(text) <= limit:
        return text
    if limit < _CLIP_MARKER_CHARS:
        return text[: max(limit, 0)]
    kept = limit - _CLIP_MARKER_CHARS
    return text[:kept] + f"[... {len(text) - kept:,} characters of the {what} left out: this answer's budget ...]"


def _answer_budget() -> int:
    """How long a ``get_work_item`` answer may be: ``RESULT_BUDGET_CHARS``,
    or less when the running loop caps tool results lower."""
    from app.tools.base import tool_result_char_cap

    cap = tool_result_char_cap()
    if cap is None:
        return RESULT_BUDGET_CHARS
    # 200: the loop's own truncation notice, never needed when under the cap.
    return max(min(RESULT_BUDGET_CHARS, cap - 200), 0)


#: An argument the caller did not give, apart from an explicit null (which clears).
_UNSET: Any = object()

#: What update_work_item answers for an empty owner_agent_id (#862): never a clear.
OWNER_KEPT_LINE = (
    "owner_agent_id was empty: the owner is kept (an agent never clears an item's owner; "
    "name another agent to re-route it)"
)


def _clearable(value: Any) -> str | None:
    """A clearable date argument for the service, which reads ``None`` as
    unchanged and ``""`` as cleared: only an explicit null clears, an empty
    string (what a model sends for an argument it does not use) leaves it."""
    if value is _UNSET or value == "":
        return None
    return "" if value is None else str(value)


def _due_deferral_line(item: dict, now: datetime | None = None) -> str:
    """The line of an open, owned item whose future ``not_before`` defers its pickup.

    An inbox takes an item only from its ``not_before`` on
    (``list_dispatchable_work_items``; ``due_at`` is the deadline and defers
    nothing, #8753), so an item with a later pickup stays open and unclaimed
    until then: the writer reads it here rather than as a stuck inbox.
    Empty otherwise.
    """
    if item.get("status") != "open" or not item.get("owner_agent_id") or not item.get("not_before"):
        return ""
    try:
        at = datetime.fromisoformat(str(item["not_before"]).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    if at <= (now or datetime.now(UTC)):
        return ""
    return (
        f"  - not picked up before {item['not_before']} (its not_before). "
        "Set not_before to null to have it picked up on the next tick."
    )


def _not_before_text(item: dict, now: datetime | None = None) -> str:
    """How a listing or a status block names an item's ``not_before``.

    ``"deferred until T"`` for an open item whose ``not_before`` T is still
    ahead (no inbox takes it before then, so a reader tells it from a stuck
    item), ``"not_before T"`` for any other item that carries one, and
    ``""`` when it carries none.
    """
    raw = item.get("not_before")
    if not raw:
        return ""
    if item.get("status") == "open":
        try:
            at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            at = None
        if at is not None:
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            if at > (now or datetime.now(UTC)):
                return f"deferred until {raw}"
    return f"not_before {raw}"


def _owner_pickup_line(owner: str, state: str | None, not_before: str | None) -> str:
    """What happens to a new item routed to *owner*, from its pickup *state*
    (``work_item_context.PICKUP_STATES``; ``None`` when it could not be read)."""
    act_now = "hand off or delegate_to_assignment to make it act now"
    if state == "automatic":
        return (
            f"  - owner agent `{owner}` has a work-item inbox that will pick it up "
            "automatically (the scheduler checks every minute)."
        )
    if state == "waiting_until_due":
        return (
            f"  - owner agent `{owner}` has a work-item inbox that will pick it up "
            f"from its not_before ({not_before})."
        )
    if state == "filtered_out":
        return (
            f"  - owner agent `{owner}` has a work-item inbox, but its priority / "
            "correlation filters do not take this item: nothing will pick it up "
            f"automatically. Change its priority or correlation_key, or {act_now}."
        )
    if state == "manual":
        return (
            f"  - owner agent `{owner}` has no work-item inbox (no assignment or flow "
            "drains its queue), so nothing will pick it up automatically: it waits "
            f"until that agent is asked to process its queue. To have it done, {act_now}."
        )
    return (
        f"  - owner agent `{owner}` will pick it up if a work-item inbox drains its "
        f"queue; {act_now}."
    )


def _indented(text: str, prefix: str = "    ") -> str:
    return text.replace("\n", "\n" + prefix)


def _description_preview(it: dict) -> tuple[list[str], bool]:
    """The ``list_work_items`` lines showing the start of *it*'s description.

    Returns the lines and whether the description was cut.
    """
    description = (it.get("description") or "").strip()
    if not description:
        return [], False
    preview = description[:LIST_DESCRIPTION_PREVIEW_CHARS].rstrip()
    lines = [f"    description: {_indented(preview, '      ')}"]
    truncated = len(description) > LIST_DESCRIPTION_PREVIEW_CHARS
    if truncated:
        lines.append(
            f"    [description truncated: {LIST_DESCRIPTION_PREVIEW_CHARS:,} of "
            f"{len(description):,} characters - get_work_item returns its newest part and its brief, "
            "offset/max_chars any window]"
        )
    return lines, truncated


def _description_view(
    description: str, offset: int | None, max_chars: int | None, room: int, name: str = "description"
) -> tuple[str | None, list[str]]:
    """The description (or the field *name*, the result) as ``get_work_item`` shows it.

    Returns the status-block line saying what part is shown and how to read
    on (``None`` when it comes whole) and the description's own lines. *room*
    is what the item's other fields leave of the answer's budget.

    Without *offset* and *max_chars* a description that fits comes whole; a
    longer one comes as its head (the newest notes) and its tail (the brief)
    with a marker naming what was left out. With either, the window from
    *offset* (default 0) of *max_chars* characters (default the head plus tail
    bound) comes, never more than ``DESCRIPTION_WINDOW_MAX_CHARS`` or *room*,
    with the offset that continues while more remains.
    """
    label = name.capitalize()
    total = len(description)
    bound = min(DESCRIPTION_HEAD_CHARS + DESCRIPTION_TAIL_CHARS, room)
    if room < DESCRIPTION_MIN_CHARS:
        where = (
            "get_work_item with offset=0 reads it alone"
            if offset is None and max_chars is None
            else "this run's tool-result cap leaves no room for a window"
        )
        return (
            f"  - {name}: {total:,} characters, not shown: the other fields fill this answer; {where}",
            [f"{label}: not shown ({total:,} characters): {where}."],
        )
    if offset is None and max_chars is None:
        if total <= bound:
            return None, ["Description:", description]
        head = bound * DESCRIPTION_HEAD_CHARS // (DESCRIPTION_HEAD_CHARS + DESCRIPTION_TAIL_CHARS)
        cut_end = total - (bound - head)
        where = (
            f"characters {head:,} to {cut_end:,} left out: get_work_item with offset={head} "
            "reads them"
        )
        return (
            f"  - description: {total:,} characters, the newest {head:,} and the last "
            f"{total - cut_end:,} shown below; {where}",
            [
                f"Description ({total:,} characters; the newest {head:,} and the last "
                f"{total - cut_end:,} are shown, {where}):",
                description[:head],
                f"[... {cut_end - head:,} characters left out here: {where} ...]",
                description[cut_end:],
            ],
        )
    start = offset or 0
    if start >= total:
        return None, [f"{label}: offset {start:,} is past its end ({total:,} characters)."]
    asked = DESCRIPTION_HEAD_CHARS + DESCRIPTION_TAIL_CHARS if max_chars is None else max_chars
    end = min(start + min(asked, DESCRIPTION_WINDOW_MAX_CHARS, room), total)
    more = f"; {total - end:,} more characters: continue with offset={end}" if end < total else "; the end"
    lines = [f"{label}, characters {start:,} to {end:,} of {total:,}{more}:", description[start:end]]
    if end < total:
        lines.append(f"[{total - end:,} more characters: continue with offset={end}]")
    return f"  - {name}: characters {start:,} to {end:,} of {total:,} shown below{more}", lines


def _window_argument_error(offset: object, max_chars: object) -> str | None:
    """Why *offset* / *max_chars* cannot be used, or ``None`` when they can."""
    for name, value, least in (("offset", offset, 0), ("max_chars", max_chars, 1)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            return f"Error: {name} must be an integer."
        if value < least:
            return f"Error: {name} must be at least {least}."
    return None


def _file_reader_offered() -> bool:
    from app.tools.base import offered_tools

    return any(getattr(tool, "name", None) == "read_file_content" for tool in offered_tools() or ())


def _holder_run_line(it: dict) -> str:
    """The run holding *it* and whether it is alive, or an empty string."""
    run = it.get("claimed_by_run") or {}
    if not run.get("id"):
        return ""
    state = "alive" if run.get("alive") else f"ended ({run.get('status') or 'no longer recorded'})"
    return f"  - held by run: {run['id']}, {state}"


def _full_item_text(
    it: dict,
    refs: list,
    gone: int,
    registered: bool,
    own_run_ids: tuple = (),
    offset: int | None = None,
    max_chars: int | None = None,
    field: str = "description",
) -> str:
    """What ``get_work_item`` answers: the item in full, its files named.

    *field* ``"result"`` windows the result instead of the description
    (*offset* default 0): the status block, then the window alone.

    A long description comes bounded or as the window asked, LAST, after
    every other field, sized to what they leave of the answer's budget (the
    loop's own tool-result cap when one is bound); the line naming the part
    shown and how to read on sits right under the status line. A
    window comes with the status alone, no other field.

    The files are said to be readable only when they were registered in the
    turn's context AND the loop offers the tool that reads them. Read from a
    run other than the live one holding the item (*own_run_ids* are the
    reader's), it first says so.
    """
    from app.services.sub_agent_files import format_file_line, format_result_files_section
    from app.services.work_item_preflight import finished_under_holder_text, held_by_another_run, held_item_text

    # The title is bounded too: a long one (up to 2,000 characters) must not
    # take the room of the status and description lines under it.
    lines = [f"Work item **{_clipped(str(it['title']), _TITLE_MAX_CHARS, 'title')}** (id: `{it['id']}`)"]
    if it.get("status") in _TERMINAL_STATUSES:
        # The completion time, never updated_at: a note added after the
        # completion moves updated_at. An item finished before completed_at
        # was recorded falls back to updated_at and says so.
        when = (
            it["completed_at"]
            if it.get("completed_at")
            else f"{it.get('updated_at')} (last update; completed_at not recorded)"
        )
        lines.append(
            f"  - ALREADY {str(it['status']).upper()} at {when}: nothing is left to do unless your "
            "task explicitly says to redo it; report the result below."
        )
        # Finished by another writer while this run held it: say so first (#11554).
        stop = finished_under_holder_text(it, own_run_ids or ())
        if stop:
            lines.append(f"  - {stop}")
    elif own_run_ids and held_by_another_run(it, own_run_ids):
        lines.append(f"  - IN PROGRESS BY ANOTHER RUN: {held_item_text(it)}")
    if it.get("status") not in _TERMINAL_STATUSES:
        from app.services.work_item_main_red import same_red_text

        same_red = same_red_text(list(it.get("same_red") or []), own_run_ids)
        if same_red:
            lines.append(same_red)
    status = f"  - status: {it['status']}, priority: {it['priority']}"
    if it.get("is_stale"):
        status += " (STALE)"
    not_before = _not_before_text(it)
    if not_before:
        # With the status, a window included: a deferred open item is read
        # as waiting for its pickup time, not as stuck.
        status += f", {not_before}"
    lines.append(status)
    # The holder run and whether it is alive sit with the status, a window
    # included: a run reading a long item must still know whether another
    # run is working on it before it takes the item over.
    holder_line = _holder_run_line(it)
    if holder_line:
        lines.append(holder_line)
    # What it waits for sits with the status too: a window never hides a blocker (#11711).
    if it.get("blocked_on"):
        lines.append(_blocked_on_line(it["blocked_on"]))
    lines.extend(_blocked_why_lines(it))
    # The description's status line goes right here, above the claim notes
    # and every other field a result cap could reach.
    status_end = len(lines)
    if field == "result":
        return _with_result_window(lines, status_end, it, offset, max_chars)
    if offset is not None or max_chars is not None:
        # A window reads the description alone: no other field, however
        # large, takes its room or pushes it past the loop's cut.
        lines.append(
            "  - claim, payload, result and files: get_work_item without offset/max_chars shows them"
        )
        return _with_description(lines, status_end, it, offset, max_chars)
    lines.append(f"  - owner agent: {it.get('owner_agent_id') or 'unassigned'}")
    lines.append(f"  - created by agent: {it.get('created_by_agent_id') or 'none'}")
    if it.get("claimed_by_agent_id"):
        lines.append(
            f"  - claimed by agent: {it['claimed_by_agent_id']} at {it.get('claimed_at')}"
        )
    share = _answer_budget() // _FIELD_SHARE_DIVISOR
    notes = "\n".join(f"  - claim note: {note.get('text')}" for note in it.get("claim_notes") or [])
    if notes:
        lines.append(_clipped(notes, share, "claim notes"))
    if it.get("correlation_key"):
        lines.append(f"  - correlation_key: {it['correlation_key']}")
    if it.get("due_at"):
        lines.append(f"  - due: {it['due_at']}")
    timestamps = (
        f"  - created: {it.get('created_at')}, updated: {it.get('updated_at')}, "
        f"last activity: {it.get('last_activity_at')}"
    )
    if it.get("completed_at"):
        timestamps += f", completed: {it['completed_at']}"
    lines.append(timestamps)
    if it.get("read_only"):
        lines.append("  - company-managed, read-only for this agent")
    payload = it.get("payload") or {}
    lines.append("")
    if payload:
        lines.append("Payload:")
        lines.append(_clipped(json.dumps(payload, indent=2, ensure_ascii=False, default=str), share, "payload"))
    else:
        lines.append("Payload: (none)")
    if it.get("result"):
        result_text = str(it["result"])
        lines.append("")
        if len(result_text) > share:
            lines.append(
                f"Result ({len(result_text):,} characters; get_work_item with field=\"result\" "
                "and offset/max_chars reads it whole, by window):"
            )
        else:
            lines.append("Result:")
        lines.append(_clipped(result_text, share, "result"))
    if refs:
        lines.append("")
        if registered and _file_reader_offered():
            files = format_result_files_section(refs, heading="Files attached to this work item")
        else:
            files = "\n".join(
                [f"Files attached to this work item ({len(refs)}):"]
                + [format_file_line(ref) for ref in refs]
                + (
                    ["This agent has no tool that reads a file's content, so it cannot open them here."]
                    if registered
                    else []
                )
            )
        lines.append(_clipped(files, share, "file list"))
    if gone:
        lines.append(f"  ({gone} attached file(s) no longer exist.)")
    return _with_description(lines, status_end, it, offset, max_chars)


def _with_result_window(
    lines: list[str], status_end: int, it: dict[str, Any], offset: int | None, max_chars: int | None
) -> str:
    """*lines* (the status block) with a window of the item's result, the way
    ``_with_description`` windows the description: from *offset* (default 0),
    sized to the answer's budget, the next offset named under the status."""
    # Verbatim, never stripped: the offsets count the stored text.
    result = str(it.get("result") or "")
    budget = _answer_budget()
    lines.append("  - description, claim, payload and files: get_work_item without field=\"result\" shows them")
    lines.append("")
    if not result:
        lines.append("Result: (none)")
        return _clipped("\n".join(lines), budget, "answer")
    room = budget - len("\n".join(lines)) - _DESCRIPTION_FRAME_CHARS
    summary, body = _description_view(result, offset or 0, max_chars, room, name="result")
    if summary:
        lines.insert(status_end, summary)
    lines.extend(body)
    return _clipped("\n".join(lines), budget, "answer")


def _with_description(
    lines: list[str], status_end: int, it: dict[str, Any], offset: int | None, max_chars: int | None
) -> str:
    """*lines* with the item's description appended, sized to what they leave
    of the answer's budget (``_answer_budget``), and its status line inserted
    at *status_end*."""
    description = (it.get("description") or "").strip()
    budget = _answer_budget()
    lines.append("")
    if not description:
        lines.append("Description: (none)")
        return _clipped("\n".join(lines), budget, "answer")
    room = budget - len("\n".join(lines)) - _DESCRIPTION_FRAME_CHARS
    summary, body = _description_view(description, offset, max_chars, room)
    if summary:
        lines.insert(status_end, summary)
    lines.extend(body)
    # Every field above is bounded, so this cut acts only when the fixed
    # lines alone pass a very low cap; the description is then "not shown".
    return _clipped("\n".join(lines), budget, "answer")


#: The statuses after which a work item has nothing left to do.
_TERMINAL_STATUSES = frozenset({"done", "cancelled"})


def _already_finished_text(it: dict) -> str:
    """What a claim on a finished item answers: when, by whom, and the stored result.

    The same text the pre-flight of a task on a finished item ends with
    (``work_item_preflight.finished_item_text``).
    """
    from app.services.work_item_preflight import finished_item_text

    return finished_item_text(it)


#: When the items of ``blocked_on`` release the waiter (#11214).
_RELEASE_ON_SCHEMA: dict[str, object] = {
    "type": "string",
    "enum": ["done", "passed", "deployed"],
    "description": (
        "blocked_on release: done (default, any completion), passed (each done with verdict PASS) "
        "or deployed (done, then the build carrying its PR on a deploy-watched repo)"
    ),
}

#: Take an item from the live run holding it, on purpose (#11740).
_TAKE_OVER_SCHEMA: dict[str, object] = {
    "type": "boolean",
    "description": "Take an item another live run holds (refused without it); that run is told to stop",
}

#: The fields that say why an item is blocked: create and update share them.
_BLOCKED_WHY_SCHEMA: dict[str, dict] = {
    "blocked_on_refs": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name"},
                "kind": {"type": "string", "enum": ["pull_request", "issue", "deployment", "workflow_run"]},
                "number": {"type": "integer"},
                "sha": {"type": "string"},
                "workflow": {"type": "string"},
                "after": {"type": "string"},
                "branch": {"type": "string"},
                "title_contains": {"type": "string"},
                "carries": {"type": "string"},
                "release_on": {
                    "type": "string",
                    "enum": ["merged", "closed", "merged_or_closed", "deployed", "succeeded"],
                },
            },
            "required": ["repo"],
        },
        "description": (
            "Waits for (replaces; [] clears), reopens when all met. PR (branch: "
            "successors too) or issue: release_on (default merged_or_closed, issue closed). deployment (number "
            "or sha): build has the merge. workflow_run: a later success."
        ),
    },
    "blocked_reason": {
        "type": "string",
        "description": (
            "Context, never the blocker: blocked needs an owner and blocked_on, blocked_on_refs or "
            "needs_human with ask."
        ),
    },
    "needs_human": {"type": "boolean", "description": "Blocked on a person's decision; needs ask."},
    "recipient": {
        "type": "string",
        "description": "needs_human: who decides (user id or email); default: run's person, agent owner, admins",
    },
    "ask": {
        "type": "object",
        "description": (
            "{question, context, options:[{key,label,effect,keeps_waiting?,confirm?}], recommendation: option key, deadline, "
            "links:[{label,url}]}. needs_human: all but deadline, links; 2+ options. Plain words; "
            "no timestamp, id, key or 'card sent' (use blocked_reason). "
            "Good: 'Post Legal's comment asking for the 4 missing documents?'. "
            "Bad: 'CARD SENT 2026-11-20T11:43Z: waiting for GO'"
        ),
    },
}


async def _with_recipient_line(item: dict[str, Any]) -> dict[str, Any]:
    """*item* with the line naming who its decision waits for (#11893); never raises."""
    from app.services.work_item_recipient import recipient_line

    try:
        line = await recipient_line(item)
    except Exception as exc:
        logger.warning("work item recipient not read: %s", format_exception_message(exc))
        return item
    return {**item, "recipient_line": line} if line else item


def _blocked_on_refs_argument(value: object) -> tuple[list[dict] | None, str | None]:
    """``blocked_on_refs`` as given (a list, or its JSON text): ``(refs, error)``; ``None`` leaves it unchanged."""
    if value is None:
        return None, None
    if isinstance(value, str):
        if not value.strip():
            return None, None
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            return None, f"Error: blocked_on_refs is not valid JSON ({exc})."
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return None, "Error: blocked_on_refs must be a list of {repo, kind, number, release_on}."
    return value, None


def _blocked_why_lines(item: dict) -> list[str]:
    """What a tool result says of a blocked item's why."""
    lines: list[str] = []
    refs = item.get("blocked_on_refs") or []
    if refs:
        from app.services.work_item_blocked_refs import ref_label

        names = ", ".join(
            f"{ref_label(r)} ({r.get('state') or 'waiting'}, release_on {r.get('release_on')})" for r in refs
        )
        lines.append(
            f"  - blocked on {len(refs)} reference(s): {names}. It goes back to open by itself "
            "once every one of them is met as release_on asks."
        )
    from app.services.work_item_blocked_refs import bound_pull_request_wait

    bound = bound_pull_request_wait(item)
    if bound:
        lines.append(f"  - blocked on {bound}: it goes back to open with no write from you.")
    if item.get("blocked_reason"):
        lines.append(f"  - blocked reason: {item['blocked_reason']}")
    if item.get("status") in WORK_ITEM_TERMINAL_STATUSES:
        # A finished item waits for nobody: no person line, no pending ask (#1719).
        return lines
    if item.get("needs_human"):
        lines.append("  - needs a person (needs_human): nothing releases it by itself.")
        if item.get("recipient_line"):
            lines.append(item["recipient_line"])
    from app.services.work_item_ask import ask_lines

    lines.extend(ask_lines(item.get("ask")))
    why = item.get("blocked_why") or {}
    if why.get("missing"):
        lines.append(
            "  - BLOCKED WITH NO REAL BLOCKER: set blocked_on, blocked_on_refs, or needs_human with an ask "
            "card, and an owner; or reopen it with not_before."
        )
    return lines


#: The ``fleet_impact`` argument of create / update / complete_work_item.
_FLEET_IMPACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Optional: fleet agents its outcome matters to; work_item.completed reaches each target "
        "with this object and the result. On update or complete it replaces the value; {} clears it."
    ),
    "properties": {
        "targets": {"type": "array", "items": {"type": "string"}, "description": "Agent ids (UUIDs)"},
        "kind": {
            "type": "string",
            "enum": list(FLEET_IMPACT_KINDS),
            "description": "What the completion changes for them (required with targets)",
        },
        "instructions": {"type": "string", "description": "What the targets should do once it completes"},
    },
}


def _hold_create_refusal(
    status: str,
    blocked_on: Any,
    blocked_on_refs: Any,
    blocked_reason: str,
    needs_human: bool,
    *,
    holder: Any,
) -> str | None:
    """Why ``hold`` may not be taken on this create_work_item call, or ``None``.

    A held item is an open item its creating run keeps out of the queue until
    it releases it (#11515): it needs a run to hold it, and an item that waits
    or is finished is never dispatched anyway.
    """
    if (status or "open").strip() != "open":
        return "Error: hold is only taken with status open (the default). Nothing was created."
    if blocked_on or blocked_on_refs or blocked_reason or needs_human:
        return (
            "Error: hold is not taken on an item created blocked: a blocked item is never "
            "dispatched before its release anyway. Nothing was created."
        )
    if holder is None:
        return (
            "Error: hold needs a run or background task to hold the item, and this call "
            "has none: create it open once it is complete. Nothing was created."
        )
    return None


def _terminal_create_refusal(
    terminal: bool,
    result: str,
    blocked_on: Any,
    blocked_on_refs: Any,
    blocked_reason: str,
    needs_human: bool,
    ask: Any,
) -> str | None:
    """Why a create_work_item call may not land as given, or ``None``.

    A ``result`` belongs to a finished item only; a finished item waits for
    nothing and asks nobody (#10190).
    """
    if not terminal:
        if (result or "").strip():
            return "Error: result is only taken with status done or cancelled. Nothing was created."
        return None
    waits = [
        name
        for name, value in (
            ("blocked_on", blocked_on),
            ("blocked_on_refs", blocked_on_refs),
            ("blocked_reason", blocked_reason),
            ("needs_human", needs_human),
            ("ask", ask),
        )
        if value
    ]
    if waits:
        return (
            f"Error: an item created done or cancelled waits for nothing; drop {', '.join(waits)}. "
            "Nothing was created."
        )
    return None


def _fleet_impact_argument(value: Any) -> tuple[dict | None, str | None]:
    """The ``fleet_impact`` argument as an object (``None``: not given) and the error to answer.

    A model may send the object or its JSON text; an empty string is "not given".
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            return None, f"Error: fleet_impact is not valid JSON ({exc}). Nothing was written."
    if not isinstance(value, dict):
        return None, "Error: fleet_impact must be an object {targets, kind, instructions}. Nothing was written."
    return value, None


def _fleet_impact_line(impact: dict | None, *, completed: bool = False) -> str:
    """The tool result's line for the agents an item's completion is routed to."""
    if not impact or not impact.get("targets"):
        return "  - fleet_impact: none (its completion reaches its creator, owner and holder only)."
    targets = ", ".join(str(t) for t in impact["targets"])
    when = "was routed" if completed else "is routed on completion"
    return (
        f"  - fleet_impact ({impact.get('kind')}): its work_item.completed event {when} to {targets} "
        "(their platform_event listeners filter on fleet_impact.targets)."
    )


def _cited_files_line(item: dict[str, object], cited: list[str], refused: list[str]) -> str:
    """The completion's line for the files its result cites.

    *cited* were admitted; the ones the item read back does not carry were
    left out by the attachment cap. *refused* the turn could not attach.
    """
    raw = item.get("attachment_file_ids")
    carried = {str(i) for i in raw} if isinstance(raw, list) else set()
    kept = [f for f in cited if f in carried]
    over_cap = [f for f in cited if f not in carried]
    parts: list[str] = []
    if kept:
        parts.append(
            f" {len(kept)} file(s) the result cites attached too: a later run opens them by file_id."
        )
    if over_cap:
        parts.append(
            f" Warning: {', '.join(over_cap)} cited but not attached: the item already carries "
            "the most files it can; a later run cannot open them."
        )
    if refused:
        parts.append(
            f" Warning: {', '.join(refused)} cited but not attached: not a stored file of this "
            "conversation or run, so a later run cannot open it by id."
        )
    return "".join(parts)


def _updated_files_line(item: dict[str, object], attached: list[str], removed: int) -> str:
    """The update's line for the files it attached or removed (#12092)."""
    raw = item.get("attachment_file_ids")
    carried = len(raw) if isinstance(raw, list) else 0
    parts = []
    if attached:
        parts.append(f"{len(attached)} file(s) attached ({', '.join(attached)})")
    if removed:
        parts.append(f"{removed} removed")
    return (
        f"  - Files: {', '.join(parts)}; the item carries {carried}. "
        "Its readers open them by file_id with get_work_item."
    )


def _blocked_on_line(blockers: list) -> str:
    """The tool result's line for an item blocked on other items."""
    return (
        f"  - blocked on {len(blockers)} work item(s): {', '.join(str(b) for b in blockers)}. "
        "It goes back to open by itself once every one of them is done; a cancelled one "
        "hands it back to its owner."
    )


def _attachment_ids_argument(value: object) -> list[str]:
    """An id-list argument (``attachment_file_ids``, ``blocked_on``) as a list of ids:
    a list, a JSON list or a comma-separated string."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        text = value.strip()
        try:
            value = json.loads(text) if text.startswith("[") else text.split(",")
        except json.JSONDecodeError:
            # Not a JSON list: an ``[[FILE:id]]`` marker, or several.
            value = text.split(",")
    if not isinstance(value, list | tuple):
        return [str(value)]
    return [str(v).strip() for v in value if str(v or "").strip()]


def build_work_item_tools(
    db: AsyncSession,
    user_id: str,
    agent_id: str,
    *,
    is_company_managed: bool = False,
    company_managed_agent: bool = False,
    peer_agents: list[dict] | None = None,
    # The workspace this agent's work items are filed into. Unlike
    # shared state, a work item has no key to namespace — so this column and
    # the read filter are the only things isolating it.
    workspace_id: uuid.UUID | None = None,
    # Where the tools run: the conversation, assignment run and flow
    # run every item they create records, so its views link back to them.
    origin: WorkItemOrigin | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the work-item + delegate-to-assignment tools for an agent.

    *agent_id* is the acting agent (used as creator / claimer, and the agent
    the creators' coordination access admits or keeps out);
    *is_company_managed* makes created items fleet-visible;
    *company_managed_agent* says the acting agent is company-managed, which is
    what lets it change or claim a company-managed item (a personal agent only
    reads one).

    *peer_agents* is the configured work-item roster: a list of
    ``{"id", "name", "slug"}`` dicts naming the agents this agent may assign
    work items to. When set, it is surfaced in the ``create_work_item`` tool
    description (so the LLM is AWARE of which peers exist and never has to
    guess a UUID) and ``owner_agent_id`` is validated against it (resolved by
    id or slug, like handover). When empty, there is no roster: ``owner_agent_id``
    is not validated (back-compatible) - the agent may set any owner, or leave it
    empty to keep the item in its own backlog.
    """
    del db  # operations use their own sessions
    acting_user_id = user_id if user_id and user_id != "anonymous" else None
    writer = agent_writer(is_company_managed=company_managed_agent, agent_id=agent_id)

    def _refused(exc: CompanyManagedWriteRefusedError, work_item_id: str = "") -> str:
        from app.services.shared_state import WorkspaceWriteRefusedError

        if isinstance(exc, WorkspaceWriteRefusedError):
            return f"Error: {exc}, so nothing was written."
        item = f" `{work_item_id}`" if work_item_id else ""
        return (
            f"Error: {exc}. The work item{item} is shared by the fleet: this "
            "agent can read it but not change or claim it, so nothing was written."
        )

    # Build the owner-resolution lookup from the roster: id AND slug -> canonical
    # id (case-insensitive), mirroring resolve_handover_agent. Lets the LLM use
    # either a slug ("triage-bot") or a raw id.
    peers = peer_agents or []
    _peer_by_key: dict[str, str] = {}
    for _p in peers:
        _pid = str(_p.get("id") or "").strip()
        if not _pid:
            continue
        _peer_by_key[_pid.lower()] = _pid
        _slug = str(_p.get("slug") or "").strip()
        if _slug:
            _peer_by_key[_slug.lower()] = _pid

    def _resolve_owner(owner: str) -> tuple[str | None, str | None]:
        """Resolve an owner_agent_id input to (canonical_id_or_None, error_or_None).

        Empty input -> (None, None) (no owner / no change). With a roster, an
        unresolvable owner returns a friendly error naming the valid peers so
        the agent self-corrects instead of minting an item nobody can see. With
        no roster, the input is accepted as-is (back-compatible).
        """
        o = (owner or "").strip()
        if not o:
            return None, None
        if not peers:
            return o, None
        match = _peer_by_key.get(o.lower())
        if match is not None:
            return match, None
        names = ", ".join(
            f"{p.get('name') or p.get('id')} ({p.get('slug') or p.get('id')})"
            for p in peers
        )
        return None, (
            f"Error: '{owner}' is not one of this agent's work-item peers. "
            f"You can assign work items to: {names}. Use the agent id or slug, "
            "or leave owner_agent_id empty to keep the item in your own backlog."
        )

    # Roster line appended to the create_work_item description so the model
    # knows its assignable peers up front (awareness), not just at error time.
    if peers:
        _roster = "; ".join(
            f"{p.get('name') or p.get('id')} (slug: {p.get('slug') or 'n/a'}, "
            f"id: {p.get('id')})"
            for p in peers
        )
        _owner_help = (
            " Assign this item to one of your peer agents via owner_agent_id "
            f"(use the id or slug): {_roster}. Leave owner_agent_id empty to "
            "keep it in your own backlog."
        )
    else:
        _owner_help = (
            " No peer roster is configured, so owner_agent_id is optional and "
            "not validated - leave it empty to keep the item in your own backlog "
            "unless you have a specific agent id to route it to."
        )

    async def _create_work_item(
        title: str,
        description: str = "",
        owner_agent_id: str = "",
        priority: str = "medium",
        correlation_key: str = "",
        due_at: str = "",
        not_before: str = "",
        status: str = "open",
        payload_json: str = "",
        attachment_file_ids: list[str] | str | None = None,
        blocked_on: list[str] | str | None = None,
        blocked_on_refs: list[dict] | str | None = None,
        blocked_reason: str = "",
        needs_human: bool = False,
        ask: dict | str | None = None,
        recipient: str = "",
        dedupe: bool = False,
        dedupe_by: str = "",
        fleet_impact: dict | str | None = None,
        result: str = "",
        blocked_on_release_on: str = "",
        hold: bool = False,
        file_context: FileContext | None = None,
    ) -> str:
        from app.services.work_item import create_work_item, current_claim_holder
        from app.services.work_item_files import (
            MAX_WORK_ITEM_ATTACHMENTS_PER_CALL,
            resolve_attachable_files,
        )

        if not (title or "").strip():
            return "Error: a work item needs a non-empty title."
        # A carrier item (a fleet_impact handed over at a merge) lands
        # finished in one call: created held by this run, so no inbox
        # dispatches it, then completed below (#10190).
        terminal = (status or "").strip() in _TERMINAL_STATUSES
        terminal_err = _terminal_create_refusal(
            terminal, result, blocked_on, blocked_on_refs, blocked_reason, needs_human, ask
        )
        if terminal_err:
            return terminal_err
        held = bool(hold)
        if held:
            hold_err = _hold_create_refusal(
                status,
                blocked_on,
                blocked_on_refs,
                blocked_reason,
                needs_human,
                holder=current_claim_holder(origin.run_id if origin else None),
            )
            if hold_err:
                return hold_err
        wanted_files = _attachment_ids_argument(attachment_file_ids)
        if len(wanted_files) > MAX_WORK_ITEM_ATTACHMENTS_PER_CALL:
            return (
                f"Error: one call attaches at most {MAX_WORK_ITEM_ATTACHMENTS_PER_CALL} files "
                f"({len(wanted_files)} given). Nothing was created; attach the ones the owner needs."
            )
        attached = []
        if wanted_files:
            attachable = await resolve_attachable_files(
                wanted_files,
                file_context=file_context,
                conversation_id=origin.conversation_id if origin else None,
                assignment_run_id=origin.run_id if origin else None,
                user_id=acting_user_id,
            )
            if attachable.refused:
                refused = ", ".join(f"'{f}'" for f in attachable.refused)
                return (
                    f"Error: {refused} cannot be attached: only a stored file of this "
                    "conversation or run (an upload, a file you generated or downloaded), "
                    "or a stored file of your user's other conversations and runs can be, "
                    "by its file_id or an 8+ hex prefix of it. Nothing was created. "
                    f"{attachable.refusal_reason()} {list_available_files(file_context)}"
                )
            attached = attachable.refs
        # Optional structured context (mirrors set_shared_state's value_json):
        # a JSON object stored on the item's payload field for downstream
        # consumers. Blank = no payload.
        parsed_payload: dict | None = None
        if payload_json.strip():
            try:
                loaded = json.loads(payload_json)
            except json.JSONDecodeError as exc:
                return f"Error: payload_json is not valid JSON ({exc})."
            if not isinstance(loaded, dict):
                return "Error: payload_json must be a JSON object."
            parsed_payload = loaded
        refs, refs_err = _blocked_on_refs_argument(blocked_on_refs)
        if refs_err:
            return refs_err
        resolved_owner, owner_err = await _owner_argument(owner_agent_id)
        if owner_err:
            return owner_err
        impact, impact_err = _fleet_impact_argument(fleet_impact)
        if impact_err:
            return impact_err
        blockers, blockers_err = await _full_blocker_ids(blocked_on)
        if blockers_err:
            return blockers_err
        if not resolved_owner and agent_id and (blockers or refs or needs_human or status == "blocked"):
            # A blocked item always has an owner that resumes it (#762): left
            # empty, it stays in the calling agent's own backlog, as promised.
            resolved_owner = agent_id
        try:
            item = await create_work_item(
                user_id=acting_user_id,
                title=title,
                description=description or None,
                owner_agent_id=resolved_owner,
                created_by_agent_id=agent_id,
                priority=priority or "medium",
                status="claimed" if terminal or held else (status or "open"),
                correlation_key=correlation_key or None,
                due_at=due_at or None,
                not_before=not_before or None,
                payload=parsed_payload,
                is_company_managed=is_company_managed,
                workspace_id=workspace_id,
                origin=origin,
                attachment_file_ids=[ref.file_id for ref in attached] or None,
                blocked_on=blockers or None,
                blocked_on_refs=refs or None,
                blocked_reason=blocked_reason or None,
                needs_human=bool(needs_human) or None,
                ask=ask or None,
                recipient=recipient or None,
                blocked_on_release_on=blocked_on_release_on or None,
                dedupe=bool(dedupe),
                dedupe_by=dedupe_by or None,
                fleet_impact=impact,
                writer=writer,
                # An agent parking an item for a person writes the card they read (#776).
                require_person_ask=True,
            )
        except CompanyManagedWriteRefusedError as exc:
            return _refused(exc)
        except ValueError as exc:
            # Actionable validation error (e.g. an anonymous, non-company-managed
            # caller whose item would be invisible) - surface it so the agent gets
            # a clear reason rather than a generic "try again". Mirrors
            # set_shared_state.
            return f"Error: {exc}"
        except Exception as exc:
            logger.error("create_work_item tool failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to create the work item. Please try again."
        if item.get("deduplicated"):
            # Sweep idempotence: the same remainder is never split twice.
            matched = {
                "main_red": "the same red on main (payload main_red), under any key",
                "key": "this correlation_key",
                "similar": "this correlation_key and title, or a near-duplicate key or title of the same owner",
            }.get(item.get("dedupe_by") or "", "this correlation_key and title")
            return (
                f"Not created: work item **{item['title']}** (id: `{item['id']}`, status: "
                f"{item['status']}) already exists with {matched}. "
                "Use that item: update it instead of creating a second one."
            )
        if terminal:
            item, finish_err = await _finish_created(item, status.strip(), result)
            if finish_err:
                return finish_err
        item = await _with_recipient_line(item)
        lines = [
            f"Created work item **{item['title']}** (id: `{item['id']}`, "
            f"status: {item['status']}, priority: {item['priority']})."
        ]
        if item.get("correlation_key"):
            lines.append(f"  - correlation_key: {item['correlation_key']}")
        if item.get("blocked_on"):
            lines.append(_blocked_on_line(item["blocked_on"]))
        lines.extend(_blocked_why_lines(item))
        lines.extend(f"  - Warning: {w}" for w in item.get("warnings") or ())
        if item.get("fleet_impact"):
            lines.append(_fleet_impact_line(item["fleet_impact"]))
        if attached:
            from app.services.sub_agent_files import format_file_line

            lines.append(
                f"  - {len(attached)} attached file(s), which whoever reads the item "
                "opens by file_id (get_work_item names them):"
            )
            lines.extend(f"    {format_file_line(ref)}" for ref in attached)
        if terminal:
            lines.append(
                "  - Created finished: no inbox picks it up; its work_item.completed event "
                "is routed as any completion's."
            )
        elif held:
            lines.append(
                "  - Held by this run: no inbox picks it up until you release it with "
                "update_work_item(status=\"open\"); if this run ends first, it goes back "
                "to the queue by itself."
            )
        elif item.get("owner_agent_id"):
            from app.services import work_item_context

            # Tool-result grounding: whether anything will actually act on it,
            # read from the inboxes, so the agent never assumes a pickup that
            # no assignment or flow performs.
            lines.append(
                _owner_pickup_line(
                    item["owner_agent_id"],
                    await work_item_context.new_item_pickup_state(item),
                    item.get("not_before"),
                )
            )
        else:
            # Tool-result grounding: an unowned item is the #1 misuse - agents
            # create one like a human to-do note, but nothing routes it. Say so
            # in-loop, the moment it happens, so the agent sets an owner or hands
            # off instead of leaving an orphan nobody will ever pick up.
            lines.append(
                "  - No owner agent set: this item is UNASSIGNED and will not "
                "route to anyone automatically. Work items are an agent-to-agent "
                "queue, not a human to-do list - set `owner_agent_id` (the agent "
                "expected to act) or hand off / delegate_to_assignment so it "
                "actually gets picked up. Leave it unowned only if another agent "
                "is expected to drain the unassigned queue."
            )
        return "\n".join(lines)

    async def _finish_created(item: dict, status: str, result: str) -> tuple[dict, str | None]:
        """Complete *item*, just created held by this run, as *status* with *result*."""
        from app.services.work_item import complete_work_item

        try:
            done = await complete_work_item(
                item["id"],
                user_id=acting_user_id,
                result=result or None,
                status=status,
                writer=writer,
                writer_run_ids=_own_run_ids(),
            )
        except Exception as exc:
            logger.error("create_work_item completion failed: %s", format_exception_message(exc), exc_info=True)
            done = None
        if done is None:
            return item, (
                f"Error: work item `{item['id']}` was created but not completed; "
                "call complete_work_item on it."
            )
        return done, None

    async def _update_work_item(
        work_item_id: str,
        status: str = "",
        priority: str = "",
        description: str = "",
        owner_agent_id: str | None = None,
        correlation_key: str = "",
        due_at: str | None = _UNSET,
        not_before: str | None = _UNSET,
        title: str = "",
        blocked_on: list[str] | str | None = None,
        blocked_on_refs: list[dict] | str | None = None,
        blocked_reason: str | None = None,
        needs_human: bool | None = None,
        ask: dict | str | None = None,
        recipient: str = "",
        note: str = "",
        fleet_impact: dict | str | None = None,
        drop_open_blockers: bool = False,
        program_key: str | None = None,
        blocked_on_release_on: str = "",
        take_over: bool = False,
        attachment_file_ids: list[str] | str | None = None,
        remove_attachment_file_ids: list[str] | str | None = None,
        file_context: FileContext | None = None,
    ) -> str:
        from app.services.work_item import update_work_item
        from app.services.work_item_files import (
            MAX_WORK_ITEM_ATTACHMENTS_PER_CALL,
            resolve_attachable_files,
        )

        refs, refs_err = _blocked_on_refs_argument(blocked_on_refs)
        if refs_err:
            return refs_err
        wanted_files = _attachment_ids_argument(attachment_file_ids)
        dropped_files = _attachment_ids_argument(remove_attachment_file_ids)
        if len(wanted_files) > MAX_WORK_ITEM_ATTACHMENTS_PER_CALL:
            return (
                f"Error: one call attaches at most {MAX_WORK_ITEM_ATTACHMENTS_PER_CALL} files "
                f"({len(wanted_files)} given). Nothing was updated; attach the ones the readers need."
            )
        attached: list[str] = []
        if wanted_files:
            # The same rule as create_work_item: only a stored file the
            # writing turn holds or its user owns, so an id typed into the
            # call never becomes a grant (#12092).
            attachable = await resolve_attachable_files(
                wanted_files,
                file_context=file_context,
                conversation_id=origin.conversation_id if origin else None,
                assignment_run_id=origin.run_id if origin else None,
                user_id=acting_user_id,
            )
            if attachable.refused:
                refused = ", ".join(f"'{f}'" for f in attachable.refused)
                return (
                    f"Error: {refused} cannot be attached: only a stored file of this "
                    "conversation or run (an upload, a file you generated or downloaded), "
                    "or a stored file of your user's other conversations and runs can be, "
                    "by its file_id or an 8+ hex prefix of it. Nothing was updated. "
                    f"{attachable.refusal_reason()} {list_available_files(file_context)}"
                )
            attached = attachable.file_ids
        # An empty owner is never a clear through this tool: the owner is kept,
        # and the read-back says so instead of a silent no-op (#862).
        owner_kept = owner_agent_id is not None and not owner_agent_id.strip()
        resolved_owner, owner_err = await _owner_argument(owner_agent_id or "")
        if owner_err:
            return owner_err
        impact, impact_err = _fleet_impact_argument(fleet_impact)
        if impact_err:
            return impact_err
        work_item_id, ambiguous = await _full_item_id(work_item_id)
        if ambiguous:
            return ambiguous
        blockers, blockers_err = await _full_blocker_ids(blocked_on)
        if blockers_err:
            return blockers_err
        try:
            item = await update_work_item(
                work_item_id,
                user_id=acting_user_id,
                title=title or None,
                description=description or None,
                owner_agent_id=resolved_owner,
                priority=priority or None,
                status=status or None,
                correlation_key=correlation_key or None,
                due_at=_clearable(due_at),
                not_before=_clearable(not_before),
                blocked_on=blockers,
                blocked_on_refs=refs,
                blocked_reason=blocked_reason,
                needs_human=needs_human,
                ask=ask,
                recipient=recipient or None,
                note=note or None,
                fleet_impact=impact,
                program_key=program_key,
                blocked_on_release_on=blocked_on_release_on or None,
                writer=writer,
                # The platform dates the note and names its run: a time
                # the model types drifts from the store's.
                stamp_note=True,
                note_run_id=next(iter(_own_run_ids()), None),
                # A live run of another holder keeps its claim: an agent
                # cannot requeue an item a second run would then pick up.
                writer_run_ids=_own_run_ids(),
                drop_open_blockers=bool(drop_open_blockers),
                # Taken from a live holder on purpose: recorded, and that run is told (#11740).
                take_over=bool(take_over),
                attachment_file_ids=attached or None,
                remove_attachment_file_ids=dropped_files or None,
                # An agent parking an item for a person writes the card they read (#776).
                require_person_ask=True,
            )
        except CompanyManagedWriteRefusedError as exc:
            return _refused(exc, work_item_id)
        except ValueError as exc:
            # A scheduled work item changes through its own tools; a note
            # given with a description is refused before any read.
            return f"Error: {exc}"
        except Exception as exc:
            logger.error("update_work_item tool failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to update the work item. Please try again."
        if item is None:
            return f"Work item `{work_item_id}` not found or not accessible."
        item = await _with_recipient_line(item)
        msg = (
            f"Updated work item **{item['title']}** (id: `{item['id']}`, "
            f"status: {item['status']}, priority: {item['priority']})."
        )
        for warning in item.get("warnings") or ():
            msg += f"\n  - Warning: {warning}"
        # Another writer finished the item this run held: the read-back says stop (#11554).
        from app.services.work_item_preflight import finished_under_holder_text

        stop = finished_under_holder_text(item, _own_run_ids())
        if stop:
            msg += f"\n  - {stop}"
        if owner_kept and item.get("status") == "blocked":
            # Only where a clear would matter: a blocked item needs its owner (#762).
            msg += f"\n  - {OWNER_KEPT_LINE}"
        if note.strip():
            stamp = (item.get("description") or "").split(": ", 1)[0]
            msg += (
                f"\n  - Note added on top of the description, stamped {stamp} "
                f"(now {len(item.get('description') or '')} characters)."
            )
        if item.get("blocked_on"):
            msg += "\n" + _blocked_on_line(item["blocked_on"])
        for line in _blocked_why_lines(item):
            msg += "\n" + line
        if impact is not None:
            msg += "\n" + _fleet_impact_line(item.get("fleet_impact"))
        if attached or dropped_files:
            msg += "\n" + _updated_files_line(item, attached, len(dropped_files))
        if program_key is not None:
            linked = (item.get("payload") or {}).get("program_key")
            msg += "\n  - " + (
                f"Child of program {linked}; correlation key unchanged." if linked else "Linked to no program."
            )
        deferred = _due_deferral_line(item)
        if deferred:
            msg += "\n" + deferred
        return msg

    async def _list_work_items(
        owner_agent_id: str = "",
        status: str = "",
        correlation_key: str = "",
        open_only: bool = False,
        stale_only: bool = False,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        scope: str = "",
        completed_from: str = "",
        completed_to: str = "",
        updated_from: str = "",
        sort: str = "",
        blocked_on: str = "",
        correlation_key_prefix: str = "",
        query: str = "",
        compact: bool = False,
        group_by: str = "",
    ) -> str | ToolResult:
        from app.core.utils import safe_uuid
        from app.services import coordination_scope
        from app.services.work_item import (
            WORK_ITEM_GROUP_BY,
            count_work_items,
            count_work_items_grouped,
            list_work_items,
            normalize_sort,
            parse_time_filter,
        )

        grouping = (group_by or "").strip()
        if grouping and grouping not in WORK_ITEM_GROUP_BY:
            return (
                f"Error: group_by must be one of {', '.join(WORK_ITEM_GROUP_BY)}, got {grouping!r}. Nothing was listed."
            )
        size, start = page_bounds(limit, offset)
        try:
            order = normalize_sort(sort)
            bounds = {
                "completed_from": parse_time_filter(completed_from, "completed_from"),
                "completed_to": parse_time_filter(completed_to, "completed_to"),
                "updated_from": parse_time_filter(updated_from, "updated_from"),
            }
        except ValueError as exc:
            return f"Error: {exc}. Nothing was listed."
        text = (query or "").strip()
        if len(text) > LIST_QUERY_MAX_CHARS:
            return (
                f"Error: query is at most {LIST_QUERY_MAX_CHARS} characters, got {len(text)}. "
                "Nothing was listed."
            )
        from .id_prefix import ID_PREFIX_MIN_CHARS, id_prefix, unknown_prefix_text

        waited_for = None
        if blocked_on and blocked_on.strip():
            # A prefix resolves among the visible items like the other id
            # arguments; one that starts no item is refused, never answered
            # as an empty list that reads "nobody waits" (#11553).
            full, ambiguous = await _full_item_id(blocked_on.strip())
            if ambiguous:
                return ambiguous
            if safe_uuid(full) is None and id_prefix(full) is None:
                return (
                    f"Error: blocked_on must be a work item id or its first {ID_PREFIX_MIN_CHARS}+ "
                    f"hexadecimal characters, got {blocked_on.strip()!r}. Nothing was listed."
                )
            if safe_uuid(full) is None:
                return unknown_prefix_text(blocked_on, "work item").replace("Nothing was done", "Nothing was listed")
            waited_for = full
        owner_filter = (owner_agent_id or "").strip() or None
        if owner_filter is not None:
            # A filter is never dropped: a prefix resolves to the one agent it
            # starts, a peer slug to its id; anything else is refused, since a
            # dropped owner filter lists every owner's items (#11553).
            if peers and owner_filter.lower() in _peer_by_key:
                owner_filter = _peer_by_key[owner_filter.lower()]
            elif safe_uuid(owner_filter) is None:
                if id_prefix(owner_filter) is None:
                    return (
                        f"Error: owner_agent_id must be an agent id or its first {ID_PREFIX_MIN_CHARS}+ "
                        f"hexadecimal characters, got {owner_filter!r}. Nothing was listed."
                    )
                full_owner, owner_err = await _full_agent_id(owner_filter)
                if owner_err:
                    return owner_err.replace("Nothing was done", "Nothing was listed")
                owner_filter = full_owner
        try:
            resolved = await coordination_scope.resolve_coordination_scope(agent_id, acting_user_id, scope)
        except Exception as exc:
            logger.error("list_work_items scope failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to resolve the scope of the listing. Please try again."
        if isinstance(resolved, str):
            return resolved
        scope_clause = resolved.work_item_clause()
        from app.a2a.partner_scope import current_partner

        partner = current_partner()
        if partner is not None:
            # Inside an A2A partner task: the tickets that partner's own tasks
            # filed, on top of the scope, never the run-as identity's reach.
            from sqlalchemy import and_

            from app.a2a.partner_runs import partner_work_items_clause

            partner_clause = partner_work_items_clause(partner)
            scope_clause = partner_clause if scope_clause is None else and_(scope_clause, partner_clause)
        filters = {
            "user_id": acting_user_id,
            "owner_agent_id": owner_filter,
            "status": status or None,
            "correlation_key": correlation_key or None,
            "open_only": open_only,
            "stale_only": stale_only,
            "reader": writer,
            # The caller's fleet or own items; None reads all it can see.
            "scope": scope_clause,
            "blocked_on": waited_for,
            "correlation_key_prefix": correlation_key_prefix or None,
            "search": text or None,
            **bounds,
        }
        waits_line = f"Blocked on {waited_for}: {{n}} item(s). " if waited_for else ""
        if text:
            waits_line += f"Title or key contains {text!r}. "
        if grouping:
            return await _grouped_work_items(count_work_items_grouped, grouping, filters, waits_line, resolved)
        try:
            items = await list_work_items(**filters, sort=order, limit=size, offset=start)
            # A short page is the end: its total is known without a COUNT.
            total = (
                await count_work_items(**filters)
                if len(items) >= size or (start and not items)
                else start + len(items)
            )
        except Exception as exc:
            logger.error("list_work_items tool failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to list work items. Please try again."
        if not items and not total:
            return ToolResult(
                tool_call_id="",
                content=f"{waits_line.format(n=0)}No work items match those filters. {resolved.label()}",
                structured=[],
            )
        from app.services.work_item_files import attachment_ids

        blocks: list[str] = []
        needs_full: list[bool] = []
        for it in items:
            flags = []
            if it["is_stale"]:
                flags.append("STALE")
            not_before = _not_before_text(it)
            if not_before:
                flags.append(not_before)
            if it.get("blocked_on"):
                flags.append(f"blocked on {', '.join(it['blocked_on'])}")
            why = it.get("blocked_why") or {}
            if why.get("missing"):
                flags.append("BLOCKED WITH NO REASON")
            elif why.get("text") and (it.get("blocked_on_refs") or it.get("blocked_reason") or it.get("needs_human")):
                flags.append(why["text"].replace("Waits for: ", "waits for ", 1))
            if it.get("claimed_by_agent_id"):
                flags.append(f"claimed by {it['claimed_by_agent_id']}")
            if it.get("claimed_at"):
                flags.append(f"claimed at {it['claimed_at']}")
            if it.get("completed_at"):
                flags.append(f"completed at {it['completed_at']}")
            if it.get("read_only"):
                flags.append("company-managed, read-only for this agent")
            if it.get("payload"):
                flags.append("has a payload")
            files = len(attachment_ids(it))
            if compact:
                key = f" {it['correlation_key']}" if it.get("correlation_key") else ""
                deferred = f" ({not_before})" if not_before else ""
                blocks.append(f"- `{it['id']}`{key} [{it['status']}]{deferred} {it['title']}")
                needs_full.append(False)
                continue
            if files:
                flags.append(f"{files} attached file(s)")
            suffix = f" [{', '.join(flags)}]" if flags else ""
            owner = it.get("owner_agent_id") or "unassigned"
            corr = f", key={it['correlation_key']}" if it.get("correlation_key") else ""
            row = [
                f"- `{it['id']}` **{it['title']}** "
                f"(status: {it['status']}, priority: {it['priority']}, "
                f"owner: {owner}{corr}){suffix}"
            ]
            preview, truncated = _description_preview(it)
            row.extend(preview)
            blocks.append("\n".join(row))
            needs_full.append(truncated or bool(it.get("payload")) or bool(files))
        # The loop cuts a result from its end to its cap: keep only the rows
        # the answer can carry, so the page line names what is delivered and
        # the next offset is the first row left out.
        kept = rows_that_fit(blocks, answer_budget())
        items = items[:kept]
        structured = json.loads(json.dumps(items, default=str))
        sorted_by = "stale first" if stale_only else order
        lines = [
            f"{waits_line.format(n=total)}{page_line(start, kept, total, 'work items')} "
            f"Sorted {sorted_by}. {resolved.label()}"
        ]
        if kept:
            lines.append(f"{kept} work item(s):")
        lines.extend(blocks[:kept])
        if any(needs_full[:kept]):
            lines.append(
                "Read an item - its payload, attached files and description (a long "
                "one bounded to its newest part and its brief, offset/max_chars for "
                "any window) - with get_work_item(work_item_id)."
            )
        return ToolResult(tool_call_id="", content="\n".join(lines), structured=structured)

    async def _grouped_work_items(
        counter: Callable[..., Awaitable[list[tuple[object, int]]]],
        grouping: str,
        filters: dict[str, Any],
        waits_line: str,
        resolved: Any,
    ) -> str | ToolResult:
        """list_work_items(group_by=...): one line per value with its count, no item bodies."""
        try:
            rows = await counter(group_by=grouping, **filters)
        except Exception as exc:
            logger.error("list_work_items group_by failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to count work items. Please try again."
        total = sum(n for _value, n in rows)
        read_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            f"{waits_line.format(n=total)}{total} work item(s) in {len(rows)} group(s) by {grouping}, "
            f"read {read_at}. {resolved.label()}"
        ]
        none_label = {"owner_agent_id": "(unassigned)", "key_prefix": "(no key)"}.get(grouping, "(none)")
        lines.extend(f"- {none_label if value is None else value}: {n}" for value, n in rows)
        structured = [{"value": None if value is None else str(value), "count": n} for value, n in rows]
        return ToolResult(tool_call_id="", content="\n".join(lines), structured=structured)

    async def _full_item_id(work_item_id: str) -> tuple[str, str | None]:
        """*work_item_id* with an id prefix (8+ hexadecimal characters)
        replaced by the full id of the one visible item it starts, and the
        refusal naming the candidates when it starts several
        (``builtin_tools/id_prefix.py``)."""
        from app.services.work_item import work_items_by_id_prefix

        from .id_prefix import MAX_CANDIDATES, id_prefix, resolved_or_refused

        prefix = id_prefix(work_item_id)
        if prefix is None:
            return work_item_id, None
        try:
            items = await work_items_by_id_prefix(
                prefix, user_id=acting_user_id, reader=writer, limit=MAX_CANDIDATES + 1
            )
        except Exception as exc:
            logger.error("work item id prefix lookup failed: %s", format_exception_message(exc), exc_info=True)
            return work_item_id, None
        candidates = [(item["id"], item.get("title") or "") for item in items]
        return resolved_or_refused(work_item_id, "work items", candidates)

    async def _full_blocker_ids(value: object) -> tuple[list[str] | None, str | None]:
        """``blocked_on`` as given, each id prefix (8+ hexadecimal characters)
        replaced by the full id of the one visible item it starts (#11553).
        A prefix starting several items is refused with the candidates, one
        starting none is refused: nothing is written on a blocker the caller
        cannot name. ``None`` (not given) stays ``None``."""
        from .id_prefix import id_prefix, unknown_prefix_text

        if value is None:
            return None, None
        full_ids: list[str] = []
        for raw in _attachment_ids_argument(value):
            if id_prefix(raw) is None:
                full_ids.append(raw)
                continue
            full, refusal = await _full_item_id(raw)
            if refusal:
                return None, refusal
            if id_prefix(full) is not None:
                return None, unknown_prefix_text(raw, "work item")
            full_ids.append(str(full))
        return full_ids, None

    async def _full_agent_id(value: str) -> tuple[str | None, str | None]:
        """An agent id argument with an id prefix (8+ hexadecimal characters)
        replaced by the full id of the one agent the caller can see that it
        starts (#11553): one is taken, several are refused with the
        candidates, none is refused. A full id or any other value comes back
        as it is."""
        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session
        from app.core.utils import safe_uuid
        from app.models.agent import Agent

        from .id_prefix import id_prefix, resolve_id_prefix, unknown_prefix_text

        if id_prefix(value) is None:
            return value, None
        uid = safe_uuid(acting_user_id) if acting_user_id else None
        try:
            async with async_session() as session:
                group_ids = await get_user_group_ids(session, uid) if uid else []
                full, refusal = await resolve_id_prefix(
                    session, Agent, value, [caller_visible_filter(Agent, uid, group_ids)], noun="agents"
                )
        except Exception as exc:
            logger.error("agent id prefix lookup failed: %s", format_exception_message(exc), exc_info=True)
            return None, "Error: failed to resolve the agent id prefix. Please try again."
        if refusal:
            return None, refusal
        if id_prefix(full) is not None:
            return None, unknown_prefix_text(value, "agent")
        return str(full), None

    async def _owner_argument(owner: str) -> tuple[str | None, str | None]:
        """``owner_agent_id`` resolved: a peer by id, slug or id prefix when a
        roster is set, else an agent the caller can see by its id or id
        prefix. A prefix is never stored as is (it would leave the item with
        no owner)."""
        from .id_prefix import ambiguous_text, id_prefix, unknown_prefix_text

        o = (owner or "").strip()
        prefix = id_prefix(o)
        if prefix is None or (peers and o.lower() in _peer_by_key):
            return _resolve_owner(owner)
        if peers:
            hits = sorted({pid for pid in _peer_by_key.values() if pid.lower().startswith(prefix)})
            if len(hits) == 1:
                return hits[0], None
            if hits:
                return None, ambiguous_text(o, "peer agents", [(h, "") for h in hits])
            return _resolve_owner(owner)
        full, refusal = await _full_agent_id(o)
        if refusal:
            return None, refusal
        if full is None or id_prefix(full) is not None:
            return None, unknown_prefix_text(o, "agent")
        return full, None

    async def _get_work_item(
        work_item_id: str,
        offset: int | None = None,
        max_chars: int | None = None,
        field: str | None = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        from app.services.work_item import get_work_item
        from app.services.work_item_files import (
            register_work_item_files,
            work_item_file_refs,
        )

        refused = _window_argument_error(offset, max_chars)
        if refused:
            return refused
        field = (field or "description").strip().lower()
        if field not in ("description", "result"):
            return 'Error: field must be "description" or "result".'
        work_item_id, ambiguous = await _full_item_id(work_item_id)
        if ambiguous:
            return ambiguous
        try:
            item = await get_work_item(work_item_id, user_id=acting_user_id, reader=writer)
            outside = await _outside_scope(item, allow_all=True) if item is not None else None
        except Exception as exc:
            logger.error("get_work_item tool failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to read the work item. Please try again."
        if item is None:
            return f"Work item `{work_item_id}` not found or not accessible."
        if outside:
            return f"Work item `{work_item_id}` is {outside}."
        item = await _with_recipient_line(item)
        refs, gone = await work_item_file_refs(item)
        registered = register_work_item_files(file_context, refs)
        return ToolResult(
            tool_call_id="",
            content=_full_item_text(item, refs, gone, registered, _own_run_ids(), offset, max_chars, field),
            structured=json.loads(json.dumps(item, default=str)),
        )

    async def _outside_scope(item: dict, *, allow_all: bool) -> str | None:
        """Why *item* is outside this agent's default scope, else None.

        An agent in no fleet reads as before. A fleet agent reads an item
        outside its fleet only for an administrator (*allow_all*, the reads);
        a claim of one is always refused.
        """
        from app.services import coordination_scope

        resolved = await coordination_scope.resolve_coordination_scope(agent_id, acting_user_id, "")
        if isinstance(resolved, str) or resolved.admits(item):
            return None
        if allow_all and await coordination_scope.user_is_admin(acting_user_id):
            return None
        return resolved.refusal()

    def _own_run_ids() -> tuple:
        """The runs this tool runs in: its run and the background task around it."""
        from app.services.work_item import current_claim_holder

        run_id = origin.run_id if origin is not None else None
        return tuple(rid for rid in (run_id, current_claim_holder(run_id), current_claim_holder(None)) if rid)

    async def _claim_work_item(work_item_id: str) -> str:
        from app.services.work_item import claim_work_item, current_claim_holder
        from app.services.work_item_preflight import held_by_another_run, held_item_text

        # The run (or background task) this tool runs in holds the claim:
        # its end releases the item. Written by the platform, never
        # the agent; a claim made outside any run records none.
        claim_holder = current_claim_holder(origin.run_id if origin is not None else None)
        work_item_id, ambiguous = await _full_item_id(work_item_id)
        if ambiguous:
            return ambiguous
        try:
            outside = await _claim_outside_scope(work_item_id)
        except Exception as exc:
            logger.error("claim_work_item scope failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to claim the work item. Please try again."
        if outside:
            return f"Could not claim work item `{work_item_id}`: it is {outside}. Nothing was claimed."
        try:
            result = await claim_work_item(
                work_item_id,
                agent_id=agent_id,
                user_id=acting_user_id,
                writer=writer,
                **({"run_id": claim_holder} if claim_holder is not None else {}),
            )
        except CompanyManagedWriteRefusedError as exc:
            return _refused(exc, work_item_id)
        except Exception as exc:
            logger.error("claim_work_item tool failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to claim the work item. Please try again."
        if result.get("claimed"):
            took = (
                "The run that held it had ended, so its claim was taken over; "
                "its progress trail is in the description. "
                if result.get("taken_over")
                else ""
            )
            return (
                f"Claimed work item `{work_item_id}` - it is now yours to work on. {took}"
                "Read it (payload, attached files, description: a long one comes as "
                "its newest part and its brief, offset/max_chars read the rest) with "
                "get_work_item, update it to `in_progress` while you act, then "
                "complete it."
            )
        reason = result.get("reason", "could not claim")
        wi = result.get("work_item") or {}
        if wi.get("status") in _TERMINAL_STATUSES:
            return _already_finished_text(wi)
        if held_by_another_run(wi, _own_run_ids()):
            return f"Could not claim work item `{work_item_id}`. {held_item_text(wi)}"
        holder = wi.get("claimed_by_agent_id")
        extra = f" (currently held by agent `{holder}`" if holder else ""
        run = wi.get("claimed_by_run") or {}
        if holder and run.get("id"):
            extra += f" in run `{run['id']}`, {'alive' if run.get('alive') else 'ended'}"
        extra += ")" if holder else ""
        return (
            f"Could not claim work item `{work_item_id}`: {reason}{extra}. "
            "Another agent likely already picked it up - pick a different item."
        )

    async def _claim_outside_scope(work_item_id: str) -> str | None:
        from app.core.utils import safe_uuid
        from app.services import coordination_scope
        from app.services.work_item import get_work_item

        if await coordination_scope.read_home_fleet(safe_uuid(agent_id)) is None:
            return None  # an agent in no fleet claims as before
        item = await get_work_item(work_item_id, user_id=acting_user_id, reader=writer)
        return await _outside_scope(item, allow_all=False) if item is not None else None

    async def _cancel_scheduled_instead(work_item_id: str, refusal: str) -> str:
        from app.services.scheduled_work_items import ScheduledWorkItemError, cancel_scheduled_work_item

        if not acting_user_id:
            return f"Error: {refusal}"
        try:
            item = await cancel_scheduled_work_item(
                work_item_id, user_id=acting_user_id, agent_id=agent_id, via="complete_work_item"
            )
        except ScheduledWorkItemError as exc:
            return f"Error: {exc}"
        if item is None:
            return f"Error: {refusal}"
        return (
            f"Work item **{item['title']}** is a scheduled work item: cancelled before it "
            "ran, nothing will run (audited as a scheduled-item cancel)."
        )

    async def _complete_work_item(
        work_item_id: str,
        result: str = "",
        status: str = "done",
        fleet_impact: dict | str | None = None,
        attachment_file_ids: list[str] | str | None = None,
        verdict: str = "",
        file_context: FileContext | None = None,
        take_over: bool = False,
    ) -> str:
        from app.services.scheduled_work_items import ScheduledWorkItemError
        from app.services.work_item import complete_work_item
        from app.services.work_item_files import (
            MAX_WORK_ITEM_ATTACHMENTS_PER_CALL,
            cited_file_ids,
            resolve_attachable_files,
        )

        impact, impact_err = _fleet_impact_argument(fleet_impact)
        if impact_err:
            return impact_err
        wanted_files = _attachment_ids_argument(attachment_file_ids)
        if len(wanted_files) > MAX_WORK_ITEM_ATTACHMENTS_PER_CALL:
            return (
                f"Error: one call attaches at most {MAX_WORK_ITEM_ATTACHMENTS_PER_CALL} files "
                f"({len(wanted_files)} given). Nothing was completed; attach the ones the readers need."
            )
        attached: list[str] = []
        if wanted_files:
            # The same rule as create_work_item: only a stored file the
            # completing turn holds, so an id typed into the call never
            # becomes a grant.
            attachable = await resolve_attachable_files(
                wanted_files,
                file_context=file_context,
                conversation_id=origin.conversation_id if origin else None,
                assignment_run_id=origin.run_id if origin else None,
                user_id=acting_user_id,
            )
            if attachable.refused:
                refused = ", ".join(f"'{f}'" for f in attachable.refused)
                return (
                    f"Error: {refused} cannot be attached: only a stored file of this "
                    "conversation or run (an upload, a file you generated or downloaded), "
                    "or a stored file of your user's other conversations and runs can be, "
                    "by its file_id or an 8+ hex prefix of it. Nothing was completed. "
                    f"{attachable.refusal_reason()} {list_available_files(file_context)}"
                )
            attached = attachable.file_ids
        # A file the result cites ([[FILE:id]], file:<uuid>) is a file the
        # result is about: attach it under the same admission rule, so a
        # later run reads it by id. One the turn cannot attach is warned,
        # never refused: the completion is not lost over a citation.
        cited: list[str] = []
        cited_refused: list[str] = []
        wanted_cited = [c for c in cited_file_ids(result) if c not in attached]
        if wanted_cited:
            admitted = await resolve_attachable_files(
                wanted_cited,
                file_context=file_context,
                conversation_id=origin.conversation_id if origin else None,
                assignment_run_id=origin.run_id if origin else None,
                user_id=acting_user_id,
            )
            cited = [f for f in admitted.file_ids if f not in attached]
            cited_refused = admitted.refused
        work_item_id, ambiguous = await _full_item_id(work_item_id)
        if ambiguous:
            return ambiguous
        try:
            item = await complete_work_item(
                work_item_id,
                user_id=acting_user_id,
                result=result or None,
                status=status or "done",
                fleet_impact=impact,
                writer=writer,
                verdict=verdict or None,
                # A live run of another holder completes its item itself,
                # unless taken over on purpose (#11740).
                writer_run_ids=_own_run_ids(),
                take_over=bool(take_over),
                **({"attachment_file_ids": attached} if attached else {}),
                **({"cited_file_ids": cited} if cited else {}),
            )
        except CompanyManagedWriteRefusedError as exc:
            return _refused(exc, work_item_id)
        except ScheduledWorkItemError as exc:
            # A scheduled work item is cancelled, never completed: status
            # 'cancelled' goes through its audited cancel, under its own
            # authority rule (the scheduling agent, an agent handing work to
            # it, or a blocked or missed leftover).
            if (status or "").strip() != "cancelled":
                return f"Error: {exc}"
            return await _cancel_scheduled_instead(work_item_id, str(exc))
        except ValueError as exc:
            # An item a live run of another holder works is completed by that run.
            return f"Error: {exc}"
        except Exception as exc:
            logger.error("complete_work_item tool failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to complete the work item. Please try again."
        if item is None:
            return f"Work item `{work_item_id}` not found or not accessible."
        msg = f"Work item **{item['title']}** marked {item['status']}."
        if attached:
            msg += (
                f" {len(attached)} result file(s) attached: get_work_item lists them and "
                "its readers open them by file_id from any conversation."
            )
        msg += _cited_files_line(item, cited, cited_refused)
        if item.get("fleet_impact"):
            msg += "\n" + _fleet_impact_line(item["fleet_impact"], completed=True)
        return msg

    async def _delegate_to_assignment(assignment_id: str, task: str, file_ids: list[str] | None = None) -> str:
        return await _run_delegate_to_assignment(
            assignment_id=assignment_id,
            task=task,
            acting_user_id=acting_user_id,
            delegating_agent_id=agent_id,
            file_ids=file_ids,
        )

    async def _program_scope(scope: str) -> Any:
        from app.services import coordination_scope

        try:
            resolved = await coordination_scope.resolve_coordination_scope(agent_id, acting_user_id, scope)
        except Exception as exc:
            logger.error("program scope failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to resolve the scope of the read. Please try again."
        return resolved

    async def _list_programs(open_only: bool = True, scope: str = "") -> str | ToolResult:
        from app.services.work_item_programs import list_programs

        resolved = await _program_scope(scope)
        if isinstance(resolved, str):
            return resolved
        try:
            programs = await list_programs(
                user_id=acting_user_id, open_only=open_only, reader=writer, scope=resolved.work_item_clause()
            )
        except Exception as exc:
            logger.error("list_programs tool failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to list the programs. Please try again."
        if not programs:
            return ToolResult(
                tool_call_id="",
                content=(
                    "No program root (a work item keyed program:NAME) is visible. "
                    f"{resolved.label()}"
                ),
                structured=[],
            )
        lines = [f"{len(programs)} program(s), computed live from their work items. {resolved.label()}"]
        for p in programs:
            root = p["root"]
            counts = ", ".join(f"{n} {s}" for s, n in sorted(p["children_by_status"].items())) or "no child"
            extra = []
            if root.get("target_date"):
                extra.append(f"target {root['target_date']}")
            if p["owed_to_user"]:
                extra.append(f"{p['owed_to_user']} owed to the user")
            if p["flags"]:
                extra.append("FLAGGED: " + "; ".join(p["flags"]))
            tail = f" ({'; '.join(extra)})" if extra else ""
            lines.append(
                f"- {p['program']} `{root['id']}` [{root['status']}] {root.get('title')}: {counts}, "
                f"{p['next_steps']} next step(s){tail}; read {p['read_at']}"
            )
        lines.append("get_program_status(program) reads one with its pull requests.")
        return ToolResult(tool_call_id="", content="\n".join(lines), structured=programs)

    async def _get_program_status(program: str, scope: str = "") -> str | ToolResult:
        from app.services.work_item_programs import get_program_status

        if not (program or "").strip():
            return "Error: program is required (program:NAME, the slug, or the root item id)."
        resolved = await _program_scope(scope)
        if isinstance(resolved, str):
            return resolved
        try:
            status = await get_program_status(
                program, user_id=acting_user_id, reader=writer, scope=resolved.work_item_clause()
            )
        except Exception as exc:
            logger.error("get_program_status tool failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to read the program. Please try again."
        if status is None:
            return (
                f"No visible program root for {program!r}: a program is the work item keyed "
                "program:NAME; list_programs lists them."
            )
        root = status["root"]
        lines = [
            f"Program {status['program']} `{root['id']}` [{root['status']}] {root.get('title')}, "
            f"computed live at {status['read_at']}."
        ]
        for name in ("owner", "target_date", "exit_criterion", "next_owed_to_user"):
            if root.get(name):
                lines.append(f"  - {name}: {root[name]}")
        for flag in status["flags"]:
            lines.append(f"FLAG: {flag}")
        lines.append(f"Children ({len(status['children'])}):")
        for c in status["children"]:
            bits = [c["status"], f"owner {c.get('owner_agent_id') or 'unassigned'}"]
            if c["blocked_on"]:
                bits.append("blocked on " + ", ".join(c["blocked_on"]))
            if c["blocked_on_refs"]:
                bits.append(f"{len(c['blocked_on_refs'])} reference(s)")
            if c["needs_human"]:
                bits.append("needs a person")
            if c.get("due_at"):
                bits.append(f"due {c['due_at']}")
            lines.append(f"- `{c['id']}` {c.get('correlation_key') or ''} {c.get('title')} [{', '.join(bits)}]")
        if status["pull_requests"]:
            lines.append(f"Pull requests ({len(status['pull_requests'])}), read from the code host:")
            for pr in status["pull_requests"]:
                if pr["state"] == "open":
                    state = f"open{' draft' if pr.get('draft') else ''}, head {str(pr.get('head_sha') or '')[:8]}, CI {pr.get('ci') or 'running'}"
                elif pr["state"] == "merged":
                    state = f"merged {str(pr.get('merge_sha') or '')[:8]} at {pr.get('closed_at') or 'unknown'}"
                else:
                    state = pr["state"]
                lines.append(
                    f"- {pr['repo']}#{pr['number']}: {state} (items {', '.join(pr['item_ids'])}; read {pr['read_at']})"
                )
        lines.append("Next steps (open, nothing blocking): " + (", ".join(f"`{n['id']}`" for n in status["next_steps"]) or "none"))
        lines.append(
            "Owed to the user: "
            + (", ".join(f"`{o['id']}` ({o['age_hours']} h)" for o in status["owed_to_user"]) or "none")
        )
        return ToolResult(
            tool_call_id="", content="\n".join(lines), structured=json.loads(json.dumps(status, default=str))
        )

    executor = CallableToolExecutor()
    executor.register("list_programs", _list_programs)
    executor.register("get_program_status", _get_program_status)
    executor.register("create_work_item", _create_work_item)
    executor.register("update_work_item", _update_work_item)
    executor.register("list_work_items", _list_work_items)
    executor.register("get_work_item", _get_work_item)
    executor.register("claim_work_item", _claim_work_item)
    executor.register("complete_work_item", _complete_work_item)
    executor.register("delegate_to_assignment", _delegate_to_assignment)

    async def _read_for_card(work_item_id: str) -> dict[str, Any] | None:
        # The agent's own read access, as get_work_item: an item outside it is not found.
        from app.services.work_item import get_work_item

        full_id, ambiguous = await _full_item_id(work_item_id)
        if ambiguous:
            return None
        item = await get_work_item(full_id, user_id=acting_user_id, reader=writer)
        if item is None or await _outside_scope(item, allow_all=True):
            return None
        return item

    from app.services.decision_graph_card_tool import TOOL_NAME as _CARD_TOOL
    from app.services.decision_graph_card_tool import build_send_decision_card, decision_card_definition

    executor.register(_CARD_TOOL, build_send_decision_card(agent_id, acting_user_id, _read_for_card))

    definitions = [
        ToolDefinition(
            name="create_work_item",
            description=(
                "Create a DURABLE work item in a claimable agent-to-agent queue "
                "(not a human to-do list) to hand tracked follow-up work to an "
                "agent. owner_agent_id names who acts; an owner with a work-item "
                "inbox picks it up by itself. An exact correlation_key links "
                "related items." + _owner_help
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short, stable label of the work (no status in it)"},
                    "description": {"type": "string", "description": "Full instructions / context for the owner"},
                    "owner_agent_id": {"type": "string", "description": "UUID of the agent expected to act (a routing label; does not auto-trigger)"},
                    "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"], "description": "Priority (default medium)"},
                    "correlation_key": {"type": "string", "description": "Exact-match key grouping related items (e.g. a ticket id)"},
                    "due_at": {"type": "string", "description": "ISO-8601 deadline (optional); does not defer pickup"},
                    "not_before": {"type": "string", "description": "ISO-8601 time no inbox picks it up before (default now)"},
                    "status": {"type": "string", "enum": ["open", "claimed", "in_progress", "blocked", "done", "cancelled"], "description": "Initial status (default open). done/cancelled: created finished in one call (a fleet_impact carrier), never dispatched"},
                    "result": {"type": "string", "description": "With status done/cancelled: the outcome"},
                    "payload_json": {"type": "string", "description": "Optional JSON object, the payload. 'main_red' {repo, check, test|sha}: one carrier per red on main. A 'notify' key ({\"channel\", \"user\": recipient email, or \"chat_id\": an existing chat (48:notes: the sending account's own), or \"team_id\" and \"channel_id\": a channel, \"integration\": the sending integration's id or name, \"message\"?: the exact text sent, \"include_result\"?: true to add the item's result}) makes the platform send that message itself when the item is done or cancelled"},
                    "attachment_file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional file_ids (uploads, generated or downloaded files of this or another "
                            "conversation or run of your user) the owner opens by file_id; attach, never paste."
                        ),
                    },
                    "blocked_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Ids of the items it waits for: created blocked, reopened once all are done. "
                            "Wait cycles are refused: a review waits on the fix, the merge on the review."
                        ),
                    },
                    "blocked_on_release_on": _RELEASE_ON_SCHEMA,
                    **_BLOCKED_WHY_SCHEMA,
                    "dedupe": {
                        "type": "boolean",
                        "description": (
                            "When true and an unfinished item with the same correlation_key and title "
                            "exists, nothing is created and that item's id is answered (default false)."
                        ),
                    },
                    "dedupe_by": {
                        "type": "string",
                        "enum": ["key_and_title", "key", "similar"],
                        "description": (
                            "With dedupe: 'key_and_title' (default) matches the key and the title; "
                            "'key' matches the correlation_key alone, so a reworded title still "
                            "answers the unfinished item already filed for that key; 'similar' also "
                            "answers an item of the same owner whose slug or title is near-identical "
                            "(same numbers, not a child key; any key in 30 min)."
                        ),
                    },
                    "fleet_impact": _FLEET_IMPACT_SCHEMA,
                    "hold": {
                        "type": "boolean",
                        "description": (
                            "Held by this run: no inbox takes it until update_work_item status open (open items only)"
                        ),
                    },
                },
                "required": ["title"],
            },
            source="builtin:coordination",
        ),
        ToolDefinition(
            name="update_work_item",
            description=(
                "Update a work item's status, priority, owner, dates, text or files. "
                "To FINISH it, call complete_work_item (or status done/cancelled); "
                "the title is a stable label, never a status. `note` adds a "
                "platform-stamped note on top; `description` replaces the text."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "work_item_id": {"type": "string", "description": "UUID of the work item"},
                    "status": {"type": "string", "enum": ["open", "claimed", "in_progress", "blocked", "done", "cancelled"]},
                    "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
                    "description": {"type": "string", "description": "Replaces the WHOLE description. To add a progress note, use `note` instead."},
                    "note": {"type": "string", "description": "Note put on top of the description, rest kept, platform-stamped (UTC minute, run). Not with `description`."},
                    "owner_agent_id": {"type": "string", "description": "Routing label for the agent expected to act (does not auto-trigger)"},
                    "correlation_key": {"type": "string"},
                    "due_at": {"type": ["string", "null"], "description": "ISO-8601 deadline; null clears it; does not defer pickup"},
                    "not_before": {"type": ["string", "null"], "description": "ISO-8601 time no inbox picks it up before; null clears it"},
                    "title": {"type": "string", "description": "Stable label; never encode status in it (no '[DONE]'): use status or complete_work_item."},
                    "blocked_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Items it waits for (replaces; [] clears): blocked until all are done; dropping an open one is refused. Wait cycles are refused.",
                    },
                    "drop_open_blockers": {"type": "boolean"},
                    "take_over": _TAKE_OVER_SCHEMA,
                    "attachment_file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files of this run to attach (added, deduped)",
                    },
                    "remove_attachment_file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Attached file ids to drop",
                    },
                    "blocked_on_release_on": _RELEASE_ON_SCHEMA,
                    "program_key": {
                        "type": "string",
                        "description": "program:NAME: make it a child of that program, key kept; '' unlinks",
                    },
                    **_BLOCKED_WHY_SCHEMA,
                    "fleet_impact": _FLEET_IMPACT_SCHEMA,
                },
                "required": ["work_item_id"],
            },
            # The same item written again and again in one run is flagged.
            read_back=WriteReadBack(kind="work_item", identity=("work_item_id",)),
            source="builtin:coordination",
        ),
        ToolDefinition(
            name="list_work_items",
            # State other actors move (CI, reviews, other runs): a re-read later is a new question.
            live_read=True,
            description=(
                "List work items with EXACT-match filters, not semantic search. Each item "
                f"shows the first {LIST_DESCRIPTION_PREVIEW_CHARS} characters of its description "
                "(get_work_item reads one), claimed_at and completed_at. To process your items, "
                "DRAIN the queue: list your open items and claim + complete each one. "
                "blocked_on, a work item's id, lists the items waiting for it; "
                "correlation_key_prefix keeps the items whose key starts with it; "
                "query keeps the items whose title or key contains a text. Paged, "
                f"newest created first: {DEFAULT_PAGE_LIMIT} per call (limit up to {MAX_PAGE_LIMIT}); "
                "the first line gives the total and the next offset."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "owner_agent_id": {"type": "string", "description": "Filter to items owned by this agent"},
                    "status": {
                        "type": "string",
                        "description": (
                            "One status (open, claimed, in_progress, blocked, done, "
                            "cancelled) or several comma-separated, e.g. "
                            "'open,in_progress,blocked'"
                        ),
                    },
                    "correlation_key": {"type": "string", "description": "Exact correlation key"},
                    "correlation_key_prefix": {
                        "type": "string",
                        "description": (
                            "Key prefix, literal and case-sensitive (e.g. 'mail:')"
                        ),
                    },
                    "open_only": {"type": "boolean", "description": "Only non-terminal items"},
                    "stale_only": {"type": "boolean", "description": "Only stale (past-due / quiet) items"},
                    "blocked_on": {
                        "type": "string",
                        "description": (
                            "A work item id (or its first 8+ hexadecimal characters): only "
                            "the items whose blocked_on list carries it, every status unless "
                            "status or open_only narrows it; an id nobody waits on answers an empty list"
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Only the items whose title or correlation_key contains this "
                            "text, case-insensitive and literal (no wildcard), combined "
                            f"with the other filters; at most {LIST_QUERY_MAX_CHARS} characters"
                        ),
                    },
                    "compact": {
                        "type": "boolean",
                        "description": (
                            "One line per item (id, correlation_key, status, title), no "
                            "description preview or flags (default false)"
                        ),
                    },
                    "group_by": {
                        "type": "string",
                        "enum": ["status", "owner_agent_id", "priority", "key_prefix"],
                        "description": "Count per value, no items (key_prefix: key up to ':')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Items per page (default {DEFAULT_PAGE_LIMIT}, max {MAX_PAGE_LIMIT})",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Items to skip: the offset the previous page named (default 0)",
                    },
                    "scope": SCOPE_SCHEMA,
                    "completed_from": {
                        "type": "string",
                        "description": (
                            "ISO-8601: only items that reached done or cancelled at or "
                            "after this time (UTC when no offset)"
                        ),
                    },
                    "completed_to": {
                        "type": "string",
                        "description": "ISO-8601: only items completed before this time",
                    },
                    "updated_from": {
                        "type": "string",
                        "description": "ISO-8601: only items written at or after this time",
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["created_desc", "completed_desc", "updated_desc"],
                        "description": (
                            "created_desc (default), completed_desc (newest completion "
                            "first, open items after) or updated_desc"
                        ),
                    },
                },
                "required": [],
            },
            source="builtin:coordination",
        ),
        ToolDefinition(
            name="get_work_item",
            # State other actors move (CI, reviews, other runs): a re-read later is a new question.
            live_read=True,
            description=(
                "Read ONE work item in full: payload, correlation_key, status, "
                "owner and creator agents, timestamps, result, attached files "
                "(open them by file_id) and description. A description over "
                "24,000 characters comes as its newest 16,000 (notes go on top) "
                "and its last 8,000 (the brief), the offset of the rest named; "
                "offset and max_chars read any window (at most 40,000) of the "
                "description, or of the result with field='result', with the "
                "status block alone. Call it on an item before working on it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "work_item_id": {"type": "string", "description": "UUID of the work item"},
                    "offset": {
                        "type": "integer",
                        "description": "Character offset in the description to read from (default: the bounded head and tail view).",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Characters of the description to return from offset (default 24,000, at most 40,000).",
                    },
                    "field": {
                        "type": "string",
                        "enum": ["description", "result"],
                        "description": "What offset/max_chars page",
                    },
                },
                "required": ["work_item_id"],
            },
            source="builtin:coordination",
        ),
        ToolDefinition(
            name="claim_work_item",
            description=(
                "Atomically CLAIM an open work item before working on it. Only one "
                "agent can win the claim, so this prevents two agents from "
                "double-processing the same item. If the claim fails, the item is "
                "already taken - pick another. On an item already done or cancelled "
                "it answers when and by whom, with the stored result: report it and "
                "stop."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "work_item_id": {"type": "string", "description": "UUID of the work item to claim"},
                },
                "required": ["work_item_id"],
            },
            source="builtin:coordination",
        ),
        ToolDefinition(
            name="complete_work_item",
            description=(
                "Complete a work item (terminal). This - or update_work_item's "
                "status field - is the ONLY correct way to mark a work item "
                "finished; never rename the title to 'COMPLETED'/'DONE'. Record "
                "what was done in the result so the trail is auditable. Use "
                "status='cancelled' instead of 'done' when the item is no longer "
                "needed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "work_item_id": {"type": "string", "description": "UUID of the work item"},
                    "result": {"type": "string", "description": "Summary of the outcome"},
                    "status": {"type": "string", "enum": ["done", "cancelled"], "description": "Terminal status (default done)"},
                    "verdict": {
                        "type": "string",
                        "enum": ["PASS", "FAIL", "INCONCLUSIVE"],
                        "description": "A check's outcome; releases blocked_on_release_on=passed waiters on PASS",
                    },
                    "take_over": _TAKE_OVER_SCHEMA,
                    "fleet_impact": _FLEET_IMPACT_SCHEMA,
                    "attachment_file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files of this run the result cites, attached to the item",
                    },
                },
                "required": ["work_item_id"],
            },
            source="builtin:coordination",
        ),
        ToolDefinition(
            name="delegate_to_assignment",
            description=(
                "Start a tracked run of one assignment with your task and files "
                "as input: own status, retries, completion, a run id to follow."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "assignment_id": {"type": "string", "description": "Assignment UUID"},
                    "task": {"type": "string", "description": "Task for the run"},
                    "file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files the run reads",
                    },
                },
                "required": ["assignment_id", "task"],
            },
            source="builtin:coordination",
        ),
        ToolDefinition(
            name="list_programs",
            description=(
                "List programs: a work item keyed program:NAME is a root (payload.program: "
                "owner, target_date, exit_criterion, next_owed_to_user); children (keys "
                "program:NAME/..., or payload.program_key) counted live. Program status comes "
                "from these tools only, never a shared-state register or memory."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "open_only": {"type": "boolean", "description": "Unfinished roots only (default true)"},
                    "scope": SCOPE_SCHEMA,
                },
                "required": [],
            },
            source="builtin:coordination",
        ),
        ToolDefinition(
            name="get_program_status",
            # Polled: the same call later answers a state that moved on its own.
            live_state=True,
            description=(
                "One program's status computed live: children (status, owner, blockers, due), "
                "the pull requests they name (state, merge, CI read now), next steps, items owed "
                "to the user with age; read time and id per line. Flags a next_owed_to_user with "
                "no dated child."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "program": {"type": "string", "description": "program:NAME, NAME or the root id"},
                    "scope": SCOPE_SCHEMA,
                },
                "required": ["program"],
            },
            source="builtin:coordination",
        ),
        decision_card_definition(),
    ]

    return definitions, executor


async def _duplicate_of(run_id: str) -> str | None:
    """The run *run_id* was refused as a duplicate of, or ``None`` when it runs."""
    from sqlalchemy import select

    from app.core.database import async_session
    from app.core.utils import safe_uuid
    from app.models.assignment import AssignmentRun
    from app.services.event_run_dedup import DUPLICATE_OF_CONTEXT

    rid = safe_uuid(run_id)
    if rid is None:
        return None
    async with async_session() as db:
        context = (
            await db.execute(select(AssignmentRun.trigger_context).where(AssignmentRun.id == rid))
        ).scalar_one_or_none()
    original = (context or {}).get(DUPLICATE_OF_CONTEXT) if isinstance(context, dict) else None
    return str(original) if original else None


def _delegate_refusal_message(refusal, assignment) -> str:
    """Word a shared ``ManualTriggerRefusal`` for ``delegate_to_assignment``."""
    active = refusal.active_run
    try_again = (
        f" {active.describe()} Wait for that run (its id is what `until` takes) "
        "instead of delegating again."
        if active is not None
        else " Try again once it finishes."
    )
    return {
        "assignment_disabled": f"Assignment '{assignment.name}' is disabled.",
        "agent_missing": "The assignment's agent is missing or disabled.",
        "agent_disabled": "The assignment's agent is missing or disabled.",
        "admin_only": f"{refusal.detail}.",
        "own_run_active": (
            f"You already have a run of assignment '{assignment.name}' in "
            f"progress - skipped to avoid overlap.{try_again}"
        ),
        "run_active": (
            f"Assignment '{assignment.name}' already has an active run - "
            f"skipped to avoid overlap.{try_again}"
        ),
    }.get(refusal.reason, f"{refusal.detail}.")


#: Files one delegation hands its run, as many as a work item attaches.
_MAX_DELEGATED_FILES = 20

#: A work item id written in a delegation's task text.
_TASK_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
#: How many ids of a task are looked up, so a long task stays one cheap read.
_MAX_TASK_ITEM_IDS = 20


async def _held_work_item_named(db: Any, task: str, agent_id: Any) -> tuple[str, str] | None:
    """The first work item *task* names that a live run of *agent_id* holds, as ``(item id, run id)``.

    A delegation to an agent's pick-up assignment while that agent's own run
    holds the item starts a second run that can only stand down (gap
    work-item-double-dispatch). Only items owned by *agent_id* and held by a
    run that is still alive refuse; any other id in the text is ignored.
    """
    from sqlalchemy import select

    from app.models.work_item import WorkItem
    from app.services.work_item import held_by_live_run, holding_runs

    ids: list[uuid.UUID] = []
    for match in _TASK_UUID_RE.findall(task or ""):
        value = uuid.UUID(match)
        if value not in ids:
            ids.append(value)
        if len(ids) >= _MAX_TASK_ITEM_IDS:
            break
    if not ids or agent_id is None:
        return None
    rows = (
        (await db.execute(select(WorkItem).where(WorkItem.id.in_(ids), WorkItem.owner_agent_id == agent_id)))
        .scalars()
        .all()
    )
    held = [row for row in rows if getattr(row, "claimed_by_run_id", None) is not None]
    if not held:
        return None
    holders = await holding_runs(db, [row.claimed_by_run_id for row in held])
    order = {value: index for index, value in enumerate(ids)}
    for row in sorted(held, key=lambda r: order.get(r.id, len(ids))):
        if held_by_live_run(row, holders):
            return str(row.id), str(row.claimed_by_run_id)
    return None


def _delegated_file_ids(file_ids: object) -> tuple[list[str], str | None]:
    """The delegation's file ids, canonical and de-duplicated in order, and the
    first one that is not a UUID (None when all are)."""
    import uuid as _uuid

    if not file_ids:
        return [], None
    if isinstance(file_ids, str):
        file_ids = [file_ids]
    out: list[str] = []
    for raw in file_ids if isinstance(file_ids, list | tuple) else [file_ids]:
        try:
            canonical = str(_uuid.UUID(str(raw).strip()))
        except (TypeError, ValueError, AttributeError):
            return [], str(raw)
        if canonical not in out:
            out.append(canonical)
    return out, None


async def _run_delegate_to_assignment(
    *,
    assignment_id: str,
    task: str,
    acting_user_id: str | None,
    delegating_agent_id: str | None,
    file_ids: list[str] | None = None,
) -> str:
    """Trigger a tracked run of a specific (visible) assignment. B3 fix.

    ``file_ids`` reach the run's ``trigger_payload["file_ids"]``, which the
    engine admits through the file access contract as the run's own identity
    (``load_authorized_trigger_files``): a foreign id is dropped there, never
    trusted here. Without them a file named in the task text alone was not
    readable by the delegated run.
    """
    from fastapi import HTTPException
    from sqlalchemy import select

    from app.core.database import async_session
    from app.core.utils import safe_uuid
    from app.models.agent import Agent
    from app.models.user import User

    aid = safe_uuid(assignment_id)
    if aid is None:
        return f"Error: '{assignment_id}' is not a valid assignment id."
    uid = safe_uuid(acting_user_id) if acting_user_id else None
    delegated_file_ids, bad_file_id = _delegated_file_ids(file_ids)
    if bad_file_id is not None:
        return f"Error: '{bad_file_id}' is not a valid file id."
    if len(delegated_file_ids) > _MAX_DELEGATED_FILES:
        return f"Error: {len(delegated_file_ids)} files named; a delegation hands at most {_MAX_DELEGATED_FILES}."

    try:
        async with async_session() as db:
            from app.api.v1.helpers import (
                active_run_refusal,
                agent_tool_admin_bypass,
                find_user_assignment,
                manual_trigger_overlap,
                manual_trigger_refusal,
                manual_trigger_run_scope,
                manual_trigger_skipped,
            )

            caller = (
                (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
                if uid is not None
                else None
            )
            is_admin = caller is not None and caller.is_admin
            # The parent agent's visibility, every assignment for an admin, as
            # on the REST trigger route (unless AGENT_TOOLS_STRICT_ADMIN_ACCESS).
            # A caller with no account gets the company-wide tier alone.
            assignment = await find_user_assignment(
                db, uid, aid, is_admin=agent_tool_admin_bypass(caller)
            )
            if assignment is None:
                return (
                    f"Assignment `{assignment_id}` not found or not accessible."
                )
            from app.services.assignment_pause import (
                OWNER_DEACTIVATED_MESSAGE,
                AssignmentOwnerDeactivatedError,
                is_paused_by_deactivated_owner,
            )

            agent = (
                await db.execute(
                    select(Agent).where(Agent.id == assignment.agent_id)
                )
            ).scalar_one_or_none()
            # The REST manual-trigger gate, shared: enabled assignment and
            # agent, company-managed admin-only unless it runs per user, then
            # the active-run guard (the caller's own run for a per-user one).
            refusal = manual_trigger_refusal(
                assignment, agent, is_admin=is_admin, action="delegate to"
            )
            if refusal is None and uid is None and assignment.is_company_managed:
                # A caller with no account has no run of its own to start, so
                # a company-managed assignment stays admin-only even when it
                # runs per user.
                return "Only admins can delegate to company-managed assignments."
            # Paused while its owner is deactivated, like the REST
            # trigger route: after the shared gate, before the overlap guard.
            if refusal is None and await is_paused_by_deactivated_owner(db, assignment):
                return (
                    f"Assignment '{assignment.name}' was not delegated to. "
                    f"{OWNER_DEACTIVATED_MESSAGE}"
                )
            if refusal is None and uid is not None:
                refusal = await manual_trigger_overlap(db, assignment, agent, uid)
            if refusal is not None:
                return _delegate_refusal_message(refusal, assignment)
            held = await _held_work_item_named(db, task, assignment.agent_id)
            if held is not None:
                item_id, holder_run_id = held
                return (
                    f"Not delegated: work item `{item_id}` named in the task is held by live run "
                    f"`{holder_run_id}` of the same agent. A second run on it would only stand down: "
                    "add a note to the item instead, or wait for that run (its id is what `until` takes)."
                )
            run_scope = manual_trigger_run_scope(assignment, agent, uid)

            from app.services.agentic_gate import (
                AI_DISABLED_CODE,
                refusal_code,
                refusal_message,
            )
            from app.services.assignment_engine import trigger_assignment

            # Answered in the tool's own terms. ``trigger_assignment``
            # raises ``HTTPException`` when AI is off, which reaches the
            # agent as an opaque 403 (/code-review, 2026-08-28). Caught
            # rather than pre-checked so no extra read is added to a hot
            # path, and nothing flushes the caller's pending state.
            try:
                run_id = await trigger_assignment(
                    assignment,
                    agent,
                    payload={
                        "trigger_type": "delegation",
                        "task": task,
                        "delegated_by_agent_id": delegating_agent_id,
                        **({"file_ids": delegated_file_ids} if delegated_file_ids else {}),
                    },
                    db=db,
                    triggered_by_user_id=uid,
                    # The atomic backstop of the check above: the caller's own
                    # run for a per-user assignment, the whole assignment else.
                    skip_if_active=True,
                    active_run_user_id=run_scope,
                )
            except HTTPException as exc:
                refusal = refusal_code(exc)
                if refusal == AI_DISABLED_CODE:
                    return (
                        "AI is disabled on this platform, so the assignment "
                        "cannot be delegated."
                    )
                if refusal is not None:
                    # Not licensed - Community Edition.
                    return (
                        "This deployment is not licensed for AI, so the "
                        f"assignment cannot be delegated. {refusal_message(refusal)}"
                    )
                if isinstance(exc, AssignmentOwnerDeactivatedError):
                    # Deactivated since the check above.
                    return (
                        f"Assignment '{assignment.name}' was not delegated to. "
                        f"{OWNER_DEACTIVATED_MESSAGE}"
                    )
                raise
        if run_id is None:
            async with async_session() as db:
                refusal = await active_run_refusal(db, assignment.id, run_scope)
            return _delegate_refusal_message(refusal or manual_trigger_skipped(run_scope), assignment)
        duplicate_of = await _duplicate_of(run_id)
        if duplicate_of is not None:
            # Refused as a duplicate (``services/event_run_dedup.py``): the mail
            # the task names already started a run of this agent. Said plainly,
            # so the caller waits for that run instead of delegating again.
            return (
                f"Not delegated: assignment '{assignment.name}' skipped run `{run_id}` as a "
                f"duplicate of run `{duplicate_of}`, which this agent already started for the "
                "same inbound message. Wait for that run (its id is what `until` takes) "
                "instead of delegating again."
            )
        return (
            f"Delegated to assignment '{assignment.name}' - tracked run "
            f"`{run_id}` was enqueued. It executes asynchronously with its own "
            "lifecycle (status, retries, completion)."
        )
    except Exception as exc:
        logger.error("delegate_to_assignment failed: %s", format_exception_message(exc), exc_info=True)
        return "Error: failed to delegate to the assignment. Please try again."


async def load_work_item_peers(
    *, owner_agent_ids: list | None, user_id: str | None
) -> list[dict]:
    """Resolve the configured work-item peer roster to ``{id, name, slug}`` dicts.

    Only agents that are BOTH in ``owner_agent_ids`` AND visible to the acting
    user (own + group-shared + company-managed) are returned - a stale or
    now-invisible peer simply drops out of the roster (and out of validation),
    so an agent that flipped private or was deleted can never strand the owner
    field. Configured order is preserved. Returns ``[]`` for an empty roster.
    """
    ids = [str(a) for a in (owner_agent_ids or []) if a]
    if not ids:
        return []

    from sqlalchemy import select

    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.core.database import async_session
    from app.core.utils import safe_uuid
    from app.models.agent import Agent

    uid = safe_uuid(user_id) if user_id else None
    uuid_ids = [u for u in (safe_uuid(i) for i in ids) if u is not None]
    if not uuid_ids:
        return []

    async with async_session() as db:
        if uid is None:
            clause = Agent.is_company_managed.is_(True)
        else:
            group_ids = await get_user_group_ids(db, uid)
            clause = user_visible_filter(Agent, uid, group_ids)
        rows = (
            await db.execute(
                select(Agent.id, Agent.name, Agent.slug).where(
                    Agent.id.in_(uuid_ids), clause
                )
            )
        ).all()

    by_id = {
        str(r[0]): {"id": str(r[0]), "name": r[1], "slug": r[2]} for r in rows
    }
    return [by_id[i] for i in ids if i in by_id]


def format_coordination_tools_prompt(builtin_tool_ids: list[str] | None) -> str:
    """Return a capability-gated system-prompt block for the durable
    coordination tools (work items / shared state).

    A tool being in the tool list only makes it *available*; LLMs reliably
    under-reach for cross-run coordination primitives unless the system prompt
    names them and says when to use them. This mirrors the existing
    :func:`format_knowledge_base_prompt` capability block, so an agent no
    longer needs the persona to spell out "you have work items, use them".

    The block is strictly gated on the tools actually being wired (and each
    bullet on its own tool id), so it never inflates the prompt of an agent
    that does not have them. Returns an empty string when neither tool is
    enabled.
    """
    ids = set(builtin_tool_ids or [])
    has_work_items = "work_items" in ids
    has_shared_state = "shared_state" in ids
    if not (has_work_items or has_shared_state):
        return ""

    lines = [
        "\n-- Coordination tools --",
        "You have durable tools that persist across runs and are shared with "
        "the rest of the fleet. Treat them as first-class capabilities: reach "
        "for them proactively to coordinate work, not only when explicitly "
        "asked to.",
    ]
    if has_work_items:
        lines.append(
            "- Work items: a claimable, trackable agent-to-agent task queue "
            "(not a human to-do list). `create_work_item` hands follow-up work "
            "to another agent: set `owner_agent_id` (the agent expected to act) "
            "or hand off / delegate, else the item just sits unactioned - "
            "`owner_agent_id` is a routing label, not an auto-trigger. To "
            "process a queue, `list_work_items` for your open items, "
            "`claim_work_item` before acting (so two agents never double-process "
            "one), then `complete_work_item` with a result."
        )
    if has_shared_state:
        lines.append(
            "- Shared state: a cross-run key/value blackboard for the current "
            "state of a thing (one STABLE key per thing - never a per-date key; "
            "put 'as of date' history in a `note`). `get_shared_state` at the "
            "start of a run to continue instead of cold-starting, "
            "`set_shared_state` to record updated state. You OWN the entries you "
            "create and must keep them tidy: when the thing concludes call "
            "`resolve_shared_state`, and `delete_shared_state` once an entry is "
            "useless - never bake a 'resolved' status into the value, and never "
            "let stale state pile up. At creation name what ends the entry: "
            "`work_item_id` (resolved when that item completes), `expires_at`, "
            "or `terminal_condition`. A lease (`hold`) ends with its hold. Keep "
            "lists bounded with `keep_last_json`; values and lists are capped and "
            "an archive is never archived again. For episodic 'what I saw on date "
            "X' recall, use memory instead."
        )
    return "\n".join(lines)
