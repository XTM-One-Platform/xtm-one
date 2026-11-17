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

"""Pre-flight of a run named after a work item.

A background task whose task text names its work item (``work_item_id: <id>``,
the field the work-item trigger payload carries) is checked before its agent
turn: when that item is already ``done`` or ``cancelled`` the task ends at
once, with no model call, and its result is what a claim on the item answers
(when, by whom, the stored result). Another run of the same desk spawned on a
finished item used to re-read the repository for dozens of iterations before
its first claim told it the item was done.

Only the explicit field is read, never an id mentioned in prose: a task that
cites a finished item as context is not a task *on* that item. Nor is a field
inside a quarantine block (``<<UNTRUSTED-...>>``): a platform event watch
appends the event's payload there, and a watch on ``work_item.completed``
carries a finished item's ``work_item_id`` by definition. A finished item
that must be done again is put back in the queue first (``requeue_work_item``),
which this check then lets through.

An item another run is still working on ends the task the same way:
claimed or in progress, held by a run or background task that is alive and
is not the task itself, its own child run or the run that spawned it. The
answer names that run and when it claimed the item. Once the holder ended,
the task goes ahead and its claim takes the item over.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

#: ``work_item_id: <id>``, ``work_item_id=<id>`` and the JSON form
#: ``"work_item_id": "<id>"`` (a trigger payload pasted into a task).
_WORK_ITEM_FIELD = re.compile(
    r"""\bwork_item_id["']?\s*[:=]\s*["'`]?(""" + _UUID + r")",
    re.IGNORECASE,
)

#: A quarantine block as ``content_safety.wrap_untrusted`` writes it, opening
#: and closing markers with the same token. Its content is data the task
#: analyses (an event payload, a fetched page), never the item it works.
_UNTRUSTED_BLOCK = re.compile(r"<<UNTRUSTED-([0-9a-zA-Z_-]+)>>.*?<<END-UNTRUSTED-\1>>", re.DOTALL)


def named_work_item_id(task_text: str | None) -> str | None:
    """The one work item a task text names in a ``work_item_id`` field, else ``None``.

    Several different ids name no single item: nothing is checked. A field
    inside a quarantine block is data, not the task's item, and is skipped.
    """
    if not task_text:
        return None
    ids = {match.lower() for match in _WORK_ITEM_FIELD.findall(_UNTRUSTED_BLOCK.sub(" ", task_text))}
    return ids.pop() if len(ids) == 1 else None


def finished_item_text(it: dict) -> str:
    """What a run on a finished item is told: when, by whom, and the stored result.

    Said at once so a run spawned on an item another run already finished
    stops at its first call (or before it) instead of redoing the work.
    """
    by = it.get("claimed_by_agent_id") or it.get("owner_agent_id") or "an unknown agent"
    run = it.get("origin_run_id")
    lines = [
        f"Work item `{it.get('id')}` **{it.get('title') or ''}** is already {it['status']} "
        f"(at {it.get('updated_at') or 'an unknown time'}, by agent `{by}`"
        + (f", origin run `{run}`" if run else "")
        + "). Nothing is left to claim: report the stored result below and stop, "
        "unless your task explicitly says to redo this item.",
        "",
        "Result:" if it.get("result") else "Result: (none stored)",
    ]
    if it.get("result"):
        lines.append(str(it["result"]))
    return "\n".join(lines)


def held_by_another_run(it: dict, own_run_ids: Iterable[object] = ()) -> bool:
    """Whether *it* is claimed or in progress by a live run that is not one of *own_run_ids*.

    Read from ``claimed_by_run`` as :func:`~app.services.work_item.get_work_item`
    serializes it: a claim that records no run, or whose run ended, is not held.
    """
    from app.services.work_item import CLAIM_HELD_STATUSES

    if it.get("status") not in CLAIM_HELD_STATUSES:
        return False
    run = it.get("claimed_by_run") or {}
    if not run.get("id") or not run.get("alive"):
        return False
    own = {str(rid).lower() for rid in own_run_ids if rid}
    return str(run["id"]).lower() not in own


def held_item_text(it: dict) -> str:
    """What a run on an item another live run holds is told: by which run, since when.

    The same text for a refused claim, ``get_work_item`` from another run and
    the pre-flight of a background task, so the second run stops at once
    instead of working the item twice.
    """
    run = it.get("claimed_by_run") or {}
    kind = "background task" if run.get("kind") == "background_task" else "run"
    by = it.get("claimed_by_agent_id") or it.get("owner_agent_id") or "an unknown agent"
    return (
        f"Work item `{it.get('id')}` **{it.get('title') or ''}** is {it.get('status')} "
        f"since {it.get('claimed_at') or 'an unknown time'} by {kind} `{run.get('id')}` "
        f"of agent `{by}`, which is still {run.get('status') or 'alive'}. Stop unless your "
        "task explicitly says to take it over: another run is working on it, and its "
        "claim is released when that run ends."
    )


def finished_under_holder_text(it: dict[str, Any], own_run_ids: Iterable[object] | None = None) -> str | None:
    """The stop line of an item another writer finished while a live run held it (#11554).

    Read from the newest ``finished_under_holder`` claim note. With
    *own_run_ids*, the newest note naming one of them as the holder is
    answered (a later take-over of the next holder does not hide it, #11751):
    the run that held the item is the one to stop. ``None`` otherwise.
    """
    from app.services.work_item import FINISHED_UNDER_HOLDER

    notes = [n for n in it.get("claim_notes") or [] if isinstance(n, dict) and n.get("kind") == FINISHED_UNDER_HOLDER]
    if own_run_ids is not None:
        own = {str(rid).lower() for rid in own_run_ids if rid}
        notes = [n for n in notes if str(n.get("holder_run_id") or "").lower() in own]
    if not notes:
        return None
    note = notes[-1]
    return (
        f"STOP: item {note.get('status') or 'finished'} by {note.get('by') or 'another writer'} "
        f"at {note.get('finished_at') or 'an unknown time'} while run {note.get('holder_run_id')} held it. "
        "Write nothing more for it (no push, no pull request) and end the run."
    )


def work_item_write_refusal(it: dict[str, Any] | None) -> str | None:
    """Why a code-host write must not be made for item *it*: it is cancelled. ``None`` otherwise.

    A pull request opened for a cancelled item is work nobody wants (#11554);
    a done item still takes one (a follow-up PR named on a closed item).
    """
    if not it or it.get("status") != "cancelled":
        return None
    stop = finished_under_holder_text(it)
    when = it.get("completed_at") or it.get("updated_at") or "an unknown time"
    head = f"work item `{it.get('id')}` is cancelled (at {when})"
    return f"{head}. " + (stop or "Write nothing more for it and end the run.")


async def finished_work_item_answer(
    task_text: str | None,
    *,
    user_id: str | None,
    own_run_ids: Iterable[object] = (),
) -> str | None:
    """The answer that ends a task before its turn, else ``None``.

    It ends when the item its text names is finished, or is held by a live
    run that is not one of *own_run_ids* (the task, its child run, the run
    that spawned it). The item is read with the visibility of the person the
    task acts for; an item they cannot see, an open one, one held by the task's
    own runs or by a run that ended, or a failed read lets the task run.
    """
    from app.services.work_item import get_work_item

    work_item_id = named_work_item_id(task_text)
    if work_item_id is None:
        return None
    item = await get_work_item(work_item_id, user_id=user_id)
    if not item:
        return None
    if item.get("status") in WORK_ITEM_TERMINAL_STATUSES:
        return (
            "Ended before its first model call: the work item this task names is finished.\n\n"
            + finished_item_text(item)
        )
    if held_by_another_run(item, own_run_ids):
        return (
            "Ended before its first model call: another live run holds the work item "
            "this task names.\n\n" + held_item_text(item)
        )
    return None
