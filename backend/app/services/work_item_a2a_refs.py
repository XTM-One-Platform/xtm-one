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

"""A work item that waits for the end of an outbound A2A task (#2265).

A ``blocked_on_refs`` entry ``{"kind": "a2a_task", "task": "<collect_task_id>",
"release_on": "completed" | "terminal"}`` names the partner task an outbound
delegation answered (``collect_task_id`` / ``continue_task_id``). The platform's own
record of it is :class:`~app.models.a2a.A2ADelegation` (``partner_task_id``,
``outcome``, ``terminal_at``): no code host is read.

- At the write, :func:`refuse_unknown_a2a_tasks` refuses a task id the item's user
  has no delegation for (unknown, or another user's), so a wait that nothing could
  ever release is never stored. A company-managed item with no person (``user_id``
  NULL, #2591) is matched by the delegating agent instead: a task its creator or
  owner agent delegated is known, another agent's is refused.
- :func:`release_ended_a2a_tasks` (the polling pass of the autonomous tick, and once
  at the write through :func:`schedule_a2a_task_check`) records the end of each
  task on its reference: ``state`` ``completed`` when the outcome is completed,
  ``ended`` with the ``outcome`` otherwise. ``release_on: terminal`` is met by
  either; ``release_on: completed`` is met by ``completed``, and a task that ended
  another way hands the item back too, its note naming the outcome, as a pull
  request closed unmerged does for a reference that wanted it merged: nothing else
  would ever release it.
- An item whose references are all met, with no work item and no person to wait
  for, goes back to ``open``, unclaimed, with the release note on top.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Text, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.work_item import WorkItem
from app.services.work_item_blocked_refs import (
    BlockedRefError,
    _publish_surfaced,
    _satisfied,
    _spawn,
    _stamp,
    all_satisfied,
    due_deferral,
    ref_label,
)

logger = logging.getLogger(__name__)

#: Blocked items one pass reads, at most.
MAX_ITEMS_PER_PASS = 100


def a2a_task_ids(refs: object) -> list[str]:
    """The task ids of the open ``a2a_task`` references of *refs*, first seen first. Pure."""
    out: list[str] = []
    for ref in refs if isinstance(refs, list) else []:
        if isinstance(ref, dict) and ref.get("kind") == "a2a_task" and not _satisfied(ref):
            task = ref.get("task")
            if isinstance(task, str) and task and task not in out:
                out.append(task)
    return out


def end_of(outcome: str | None) -> str | None:
    """The state an ``a2a_task`` reference records for a delegation *outcome*; ``None`` while it runs. Pure."""
    if outcome is None:
        return None
    return "completed" if outcome == "completed" else "ended"


def apply_outcomes(
    refs: list[dict[str, Any]], outcomes: dict[str, str | None], now: datetime
) -> tuple[list[dict[str, Any]], list[str]]:
    """*refs* with the end of each task *outcomes* knows recorded: ``(refs, released)``. Pure.

    *outcomes* maps a task id to its delegation outcome (``None``: still running). A
    task absent from *outcomes* is left as it is.
    """
    out: list[dict[str, Any]] = []
    released: list[str] = []
    for ref in refs:
        ref = dict(ref)
        task = ref.get("task")
        if ref.get("kind") == "a2a_task" and not _satisfied(ref) and isinstance(task, str) and task in outcomes:
            outcome = outcomes[task]
            state = end_of(outcome)
            if state is not None:
                ref["state"] = state
                ref["at"] = now.isoformat()
                ref["outcome"] = str(outcome)
                wanted = ref.get("release_on")
                if state == "ended" and wanted == "completed":
                    released.append(f"{ref_label(ref)} ended {outcome}, not completed (release_on completed)")
                else:
                    released.append(f"{ref_label(ref)} {outcome}")
        out.append(ref)
    return out, released


def a2a_release_note(released: list[str], now: datetime) -> str:
    """The note of an item the end of its A2A tasks released. Pure."""
    return f"released: {'; '.join(released)}; released at {_stamp(now)}"


def delegating_agents(*agent_ids: Any) -> list[uuid.UUID]:
    """The distinct agent ids among *agent_ids* (UUIDs or their text), ``None`` and junk left out. Pure."""
    out: list[uuid.UUID] = []
    for value in agent_ids:
        agent = value if isinstance(value, uuid.UUID) else _uuid(value) if value else None
        if agent is not None and agent not in out:
            out.append(agent)
    return out


async def _outcomes(
    db: AsyncSession, user_id: Any, task_ids: list[str], agent_ids: list[uuid.UUID] | None = None
) -> dict[str, str | None]:
    """Each task id of the item's delegations among *task_ids*: its outcome (``None`` while running).

    The delegations of *user_id*; on an item with no user (company-managed, #2591),
    the delegations *agent_ids* (its creator and owner agents) made. Where one task
    id has several rows, an outstanding one wins (the wait tool's rule).
    """
    from app.models.a2a import A2ADelegation

    if not task_ids:
        return {}
    if user_id is not None:
        whose = A2ADelegation.user_id == user_id
    elif agent_ids:
        whose = A2ADelegation.agent_id.in_(agent_ids)
    else:
        return {}
    rows = await db.execute(
        select(A2ADelegation.partner_task_id, A2ADelegation.outcome, A2ADelegation.terminal_at)
        .where(A2ADelegation.partner_task_id.in_(task_ids), whose)
        .order_by(A2ADelegation.outcome.is_(None).desc(), A2ADelegation.submitted_at.desc())
    )
    out: dict[str, str | None] = {}
    for task_id, outcome, terminal_at in rows.all():
        if task_id in out:
            continue
        out[task_id] = outcome if outcome is not None and terminal_at is not None else None
    return out


async def refuse_unknown_a2a_tasks(
    db: AsyncSession, user_id: Any, refs: object, *, agent_ids: list[uuid.UUID] | None = None
) -> None:
    """Raise :class:`BlockedRefError` on an ``a2a_task`` reference the item has no delegation for.

    *user_id*'s delegations; with no user, those of *agent_ids* (the item's creator
    and owner agents, :func:`delegating_agents`).
    """
    ids = a2a_task_ids(refs)
    if not ids:
        return
    uid = user_id if isinstance(user_id, uuid.UUID) else _uuid(user_id) if user_id else None
    known = await _outcomes(db, uid, ids, agent_ids)
    unknown = [t for t in ids if t not in known]
    if unknown:
        whose = "this item's user" if uid is not None else "this item's creator or owner agent"
        raise BlockedRefError(
            f"blocked_on_refs: no outbound A2A delegation of {whose} has task {unknown[0]!r}: name the "
            "collect_task_id (or continue_task_id) the delegation tool answered; nothing else would release the item"
        )


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def release_ended_a2a_tasks(now: datetime | None = None, *, item_id: uuid.UUID | str | None = None) -> int:
    """Record the end of the A2A tasks blocked items wait for; release the items whose references are all met.

    Every blocked item naming an ``a2a_task`` reference, or *item_id* alone. Returns
    the number of items released. Never raises.
    """
    from app.core.database import async_session

    now = now or datetime.now(UTC)
    released: list[tuple[uuid.UUID, str]] = []
    try:
        async with async_session() as db:
            query = select(WorkItem).where(
                WorkItem.status == "blocked",
                WorkItem.blocked_on_refs.is_not(None),
                WorkItem.action_mode.is_(None),
                cast(WorkItem.blocked_on_refs, Text).ilike('%"a2a_task"%'),
            )
            if item_id is not None:
                one = _uuid(item_id)
                if one is None:
                    return 0
                query = query.where(WorkItem.id == one)
            rows = await db.execute(
                query.order_by(WorkItem.last_activity_at.asc())
                .limit(MAX_ITEMS_PER_PASS)
                .with_for_update(skip_locked=True)
            )
            for item in rows.scalars().all():
                refs = [r for r in (item.blocked_on_refs or []) if isinstance(r, dict)]
                outcomes = await _outcomes(
                    db,
                    item.user_id,
                    a2a_task_ids(refs),
                    delegating_agents(item.created_by_agent_id, item.owner_agent_id),
                )
                refs, ended = apply_outcomes(refs, outcomes, now)
                if not ended:
                    continue
                item.blocked_on_refs = refs
                if all_satisfied(refs) and not item.blocked_on and not item.needs_human:
                    note = a2a_release_note(ended, now) + due_deferral(item.not_before, now)
                    previous = item.description or ""
                    item.description = f"{note}\n\n{previous}" if previous else note
                    item.claim_notes = [*(item.claim_notes or []), {"at": now.isoformat(), "text": note}]
                    item.status = "open"
                    item.claimed_by_agent_id = None
                    item.claimed_by_run_id = None
                    item.claimed_at = None
                    item.blocked_on_refs = None
                    item.blocked_reason = None
                    item.needs_human = None
                    item.last_activity_at = now
                    released.append((item.id, note))
                else:
                    item.claim_notes = [
                        *(item.claim_notes or []),
                        {"at": now.isoformat(), "text": f"{a2a_release_note(ended, now)}; still waiting"},
                    ]
            await db.commit()
    except Exception as exc:
        logger.warning("Work items waiting on A2A tasks not read: %s", format_exception_message(exc))
        return 0
    for released_id, note in released:
        await _publish_surfaced(released_id, note)
    if released:
        logger.info("Released %d work item(s) whose A2A tasks ended", len(released))
    return len(released)


def names_a2a_task(refs: object) -> bool:
    """Whether *refs* (normalised ``blocked_on_refs``) waits for an A2A task. Pure."""
    return bool(a2a_task_ids(refs))


def schedule_a2a_task_check(item_id: uuid.UUID | str) -> None:
    """Read the tasks of one item once, at the write that blocks it (the task may have ended already)."""
    _spawn(release_ended_a2a_tasks(item_id=item_id))
