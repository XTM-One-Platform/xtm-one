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

"""Who wrote each stamped note of a work item, by name (#776, scope F).

A note the platform stamps reads ``YYYY-MM-DD HH:MMZ (run <uuid>): ...``
(``stamped_note`` in ``services/work_item.py``). The detail dialog shows each
note as a timeline entry headed by the agent that wrote it, never the uuid:
this module answers, for the run ids the stamps name, the agent of each run as
the viewer may see it and whether the viewer may open the run.

Read path only, no model call: one query on the runs, one on the agents the
viewer may see, one on the runs the viewer may open. A run no longer recorded
is answered ``{"agent": None, "accessible": False}``.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

# The stamp opens a paragraph: "2026-11-20 22:21Z (run <uuid>"; a run named
# deeper in a note body is not the note's author and is not resolved.
_STAMP_RUN = re.compile(
    r"(?:^|\n\n)\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?Z \(run "
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

# A description holds a few hundred notes at most; the stamps beyond are old
# history the dialog folds anyway.
MAX_NOTE_RUNS = 200


def stamped_run_ids(description: str | None) -> list[uuid.UUID]:
    """The distinct run ids the note stamps of *description* name, in order."""
    seen: dict[uuid.UUID, None] = {}
    for match in _STAMP_RUN.finditer(description or ""):
        try:
            seen.setdefault(uuid.UUID(match.group(1)), None)
        except ValueError:
            continue
        if len(seen) >= MAX_NOTE_RUNS:
            break
    return list(seen)


# Any stamped note, with or without its run: "2026-11-20 22:21Z (run <uuid>): "
# or "2026-11-20 22:21Z: " (an answer the platform writes). Group 1 is the time,
# group 2 the run when named.
_STAMP_HEAD = re.compile(
    r"(?:^|\n\n)(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?Z)"
    r"(?: \(run ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\))?: ",
    re.IGNORECASE,
)

# The text of a matched note a listing row carries (#2263).
NEWEST_NOTE_MAX_CHARS = 300


def newest_note_with_prefix(description: str | None, prefix: str) -> dict[str, str] | None:
    """The newest stamped note of *description* whose text starts with *prefix* (#2263).

    Notes are put on top of the description, so the first match is the newest.
    Answers ``{"at", "run", "text"}`` (the text bounded to
    :data:`NEWEST_NOTE_MAX_CHARS`, ``run`` empty when the stamp names none), or
    ``None`` when no note starts with *prefix*. The match is literal and
    case-sensitive, leading spaces of the note ignored. A note runs to the next
    stamp; the oldest one ends at its first blank line, since the unstamped
    brief follows it.
    """
    text = description or ""
    wanted = prefix.strip()
    if not wanted:
        return None
    heads = list(_STAMP_HEAD.finditer(text))
    for index, head in enumerate(heads):
        if index + 1 < len(heads):
            body = text[head.end() : heads[index + 1].start()].strip()
        else:
            body = text[head.end() :].split("\n\n", 1)[0].strip()
        if not body.startswith(wanted):
            continue
        if len(body) > NEWEST_NOTE_MAX_CHARS:
            body = body[: NEWEST_NOTE_MAX_CHARS - 3].rstrip() + "..."
        return {"at": head.group(1), "run": head.group(2) or "", "text": body}
    return None


async def note_runs(db: AsyncSession, description: str | None, viewer: User) -> dict[str, dict[str, Any]]:
    """``{run_id: {"agent": {"id", "name"} | None, "accessible": bool}}``.

    The agent is named only when the viewer may see it; the run is
    ``accessible`` when the viewer may open it (``run_visible_filter``).
    """
    from app.api.v1.helpers import get_user_group_ids, run_visible_filter, user_visible_filter
    from app.models.agent import Agent
    from app.models.assignment import AssignmentRun

    run_ids = stamped_run_ids(description)
    if not run_ids:
        return {}
    rows = (
        await db.execute(select(AssignmentRun.id, AssignmentRun.agent_id).where(AssignmentRun.id.in_(run_ids)))
    ).all()
    agent_of = {rid: aid for rid, aid in rows}
    admin = bool(viewer.is_admin)
    agents: dict[uuid.UUID, str] = {}
    opened: set[uuid.UUID] = set()
    agent_ids = {aid for aid in agent_of.values() if aid is not None}
    if agent_ids:
        query = select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids))
        if not admin:
            group_ids = await get_user_group_ids(db, viewer.id)
            query = query.where(user_visible_filter(Agent, viewer.id, group_ids))
        agents = {aid: name for aid, name in (await db.execute(query)).all()}
    if agent_of:
        if admin:
            opened = set(agent_of)
        else:
            opened = set(
                (
                    await db.execute(
                        select(AssignmentRun.id).where(
                            AssignmentRun.id.in_(list(agent_of)), run_visible_filter(viewer.id)
                        )
                    )
                ).scalars()
            )
    out: dict[str, dict[str, Any]] = {}
    for rid in run_ids:
        aid = agent_of.get(rid)
        name = agents.get(aid) if aid is not None else None
        out[str(rid)] = {
            "agent": {"id": str(aid), "name": name} if name is not None else None,
            "accessible": rid in opened,
        }
    return out
