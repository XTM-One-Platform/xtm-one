# SPDX-License-Identifier: Apache-2.0

"""The start-time check of a case run: is the case it names still there (#2289)?

A ``ctem_case.opened`` run can wait in the queue while its case is merged into
another or deleted. Started blind, the agent searched a whole model run for a
case that was gone. At run start, before any model call:

- the case exists: the run goes ahead unchanged;
- the case was merged away: the run goes ahead with ``merged_into`` (the
  survivor id, the merge time and actor) in its trigger context, so it reads
  the survivor first;
- the case is gone and was never merged: the run is skipped at zero tokens.

The check reads the trigger context alone (``event``, ``object_type``,
``object_id``), so a gate on another case event (a closed case for
``ctem_case.reopened``) can reuse :func:`case_start` with its own events.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_case import CtemCase

#: The case events whose run is checked at start.
CHECKED_EVENTS: frozenset[str] = frozenset({"ctem_case.opened"})
#: The trigger context key a run of a merged-away case carries.
MERGED_INTO_KEY = "merged_into"
#: The error of a run skipped because its case is gone.
CASE_GONE = "case_gone"


@dataclass(frozen=True)
class CaseStart:
    """What a case run finds at start: ``proceed``, the context to add, the skip summary."""

    proceed: bool
    context: dict[str, Any]
    summary: str = ""


def case_of(trigger_context: dict[str, Any] | None, events: frozenset[str] = CHECKED_EVENTS) -> uuid.UUID | None:
    """The case id a run's trigger context names for one of *events*, else ``None``."""
    context = trigger_context or {}
    if context.get("event") not in events or context.get("object_type") != "ctem_case":
        return None
    try:
        return uuid.UUID(str(context.get("object_id")))
    except (TypeError, ValueError):
        return None


async def case_start(db: AsyncSession, case_id: uuid.UUID) -> CaseStart:
    """The start verdict of a run about *case_id* (module docstring)."""
    from app.services import ctem_cases_store as store

    if (await db.execute(select(CtemCase.id).where(CtemCase.id == case_id))).scalar_one_or_none() is not None:
        return CaseStart(proceed=True, context={})
    merged = await store.merged_into(db, case_id, organization_id=None)
    if merged is not None:
        return CaseStart(
            proceed=True,
            context={
                MERGED_INTO_KEY: {
                    "case_id": str(merged.survivor_id),
                    "at": merged.at.isoformat(),
                    "actor": merged.actor,
                    "note": merged.answer(),
                }
            },
        )
    return CaseStart(
        proceed=False,
        context={},
        summary=f"Skipped: case {case_id} no longer exists (deleted before the run started); no model call.",
    )


__all__ = ["CASE_GONE", "CHECKED_EVENTS", "MERGED_INTO_KEY", "CaseStart", "case_of", "case_start"]
