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

"""The one place that answers "may this execution act on this run?".

One module, so call sites cannot drift apart: each site's tests exercise only
that site, and divergent copies of this rule went unnoticed by them.

The axis is NOT ownership. It is **what a mistake costs**:

* **Repeating the write is harmless** — failing a run that is already failed.
  An unowned row may be matched: two overlapping executions both acting on it
  costs nothing, and excluding it would strand rows that predate
  ``owner_execution_id`` and will never have an owner. Unless another
  live execution of its evaluation may still hold it: the release
  before ownership tracking names only the evaluation, and failing a run its
  worker is still writing destroys live work.
* **Repeating the write is not harmless** — dispatching a run re-charges the
  judge and races two writers onto one row; finalizing an episode destroys
  live callback work. An unowned row must NOT be matched: it has to be claimed
  first, then selected on strict equality.

A third case needs no check at all: rows the caller already holds locks on in
the same transaction, because nothing can have taken them in between.

WHAT THIS DOES NOT COVER. Only the ownership arm is shared. Each call site
keeps its own status filter, because those differ on purpose — the reclaim
looks at ``"running"`` alone (a ``"pending"`` run was never started), while
the episode-repair set is wider to reach runs the reclaim skipped. A shared
status filter would let a new caller inherit one that is wrong for it.

WHAT EACH HELPER ANSWERS. The docstrings say WHY; this says WHICH.

* ``runs_this_execution_may_touch(id, repeating_is_harmless=)`` — "is this row
  mine?", widened to include unclaimed rows when repeating a write costs
  nothing. The one to reach for; the others are its neighbours.
* ``runs_owned_by_nobody()`` — "has anyone claimed this row?" The target of
  dispatch's claim, and the stranded-run sweep's unowned arm.
* ``runs_no_other_live_execution_may_hold(id=None)`` — "could a live execution
  other than *id* still be running this unowned row?", asked through its
  evaluation, the only link an unowned row has.
* ``runs_unowned_and_unclaimable()`` — "is this row unclaimed AND past the
  point anyone could claim it?", so acting on it destroys nothing.
* ``runs_owned_by_another(id)`` — "whose is it, if not mine?" For reporting
  what a cleanup declined to touch, not for deciding to act.
* ``runs_with_an_owner()`` — the complement of ``runs_owned_by_nobody``.
* ``run_owned_by_execution_join()`` — the same question as a JOIN condition.

ONE STATUS REFERENCE LIVES HERE ANYWAY: ``runs_unowned_and_unclaimable``
mentions ``"pending"`` to ask whether anyone can still take the run (dispatch
claims only ``"pending"``), an ownership question. The reclaim's
``status == "running"`` is a business filter, and stays at its call site.
"""

from __future__ import annotations

import uuid

from sqlalchemy import String, and_, cast, or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.models.evaluation import EvaluationRun

__all__ = [
    "run_owned_by_execution_join",
    "runs_no_other_live_execution_may_hold",
    "runs_owned_by_another",
    "runs_owned_by_nobody",
    "runs_this_execution_may_touch",
    "runs_unowned_and_unclaimable",
    "runs_with_an_owner",
]


def runs_this_execution_may_touch(
    execution_id: uuid.UUID, *, repeating_is_harmless: bool
) -> ColumnElement[bool]:
    """Ownership arm for *execution_id*. Combine with the call site's own filters.

    *repeating_is_harmless* is the caller stating what a second write would
    cost, which is the only thing that decides whether an unowned row is in
    scope. It is keyword-only and has no default: picking wrong is the failure
    this module exists to prevent, so it has to be stated.
    """
    if repeating_is_harmless:
        return or_(
            EvaluationRun.owner_execution_id == execution_id,
            and_(
                EvaluationRun.owner_execution_id.is_(None),
                runs_no_other_live_execution_may_hold(execution_id),
            ),
        )
    return EvaluationRun.owner_execution_id == execution_id


def runs_owned_by_another(execution_id: uuid.UUID) -> ColumnElement[bool]:
    """Rows a cleanup declined to touch because ownership had moved on.

    The inverse of the harmless arm, for reporting rather than for acting. An
    unowned row is NOT "another's" — it is in scope for every cleanup — so the
    ``IS NOT NULL`` half is what makes this a complement rather than a negation.
    """
    return and_(
        EvaluationRun.owner_execution_id.is_not(None),
        EvaluationRun.owner_execution_id != execution_id,
    )


def runs_no_other_live_execution_may_hold(
    execution_id: uuid.UUID | None = None,
) -> ColumnElement[bool]:
    """No pending or running ``evaluation`` execution but *execution_id* names the row's evaluation.

    The only evidence about an UNOWNED row. The release before
    ``owner_execution_id`` ran an evaluation as one execution whose payload
    names the evaluation, never its runs, as 1780's do. While such an
    execution is live, during a rolling deploy for instance, its worker may
    still be writing the row. *execution_id* is the caller's own execution,
    live by definition; ``None`` for a caller that is not one (the sweep).

    Aliased, so a query that already joins ``orchestration_executions`` to the
    owner does not correlate the subquery to that row.
    """
    from app.models.orchestration import (
        NON_TERMINAL_EXECUTION_STATES,
        OrchestrationExecution,
    )

    live = aliased(OrchestrationExecution, name="live_execution")
    conditions = [
        live.adapter == "evaluation",
        live.state.in_(NON_TERMINAL_EXECUTION_STATES),
        # ``->>``, as ``is_callback_pending_for_run`` spells it.
        live.payload.op("->>")("evaluation_id")
        == cast(EvaluationRun.evaluation_id, String),
    ]
    if execution_id is not None:
        conditions.append(live.id != execution_id)
    return ~select(live.id).where(*conditions).exists()


def runs_owned_by_nobody() -> ColumnElement[bool]:
    """Rows no execution has claimed — the target of dispatch's claim.

    Not "is this mine" but "is this anyone's". Dispatch claims these with a
    conditional UPDATE and then selects on strict equality, so a
    harmful-to-repeat operation reaches legacy rows without two launches both
    matching one.

    Named here so no comparison on ``owner_execution_id`` exists outside this
    module, which the structural test enforces with no allowlist.

    The stranded-run sweep reads it too, for the unowned runs an upgrade leaves,
    with ``runs_no_other_live_execution_may_hold()``.
    """
    return EvaluationRun.owner_execution_id.is_(None)


def runs_unowned_and_unclaimable() -> ColumnElement[bool]:
    """Unowned rows no dispatch can take, so acting on them destroys nothing.

    Named here rather than assembled at a call site from
    ``runs_owned_by_nobody()`` AND ``status != "pending"``, which the
    structural guard could not see as a rule of its own.

    WHY THE STATUS BELONGS HERE: ``"pending"`` is the one status dispatch
    claims (``WHERE status = 'pending' AND owner_execution_id IS NULL``). An
    unowned pending run may be picked up by another launch at any moment, so
    finalizing it destroys live work; an unowned run in any other status can
    never be claimed and is safe to act on.

    WHAT IT IS FOR: legacy rows that predate ``owner_execution_id`` and will
    never have an owner. Strict ownership would strand them, and the harmless
    arm would admit claimable rows. This is the narrow middle.
    """
    return and_(
        EvaluationRun.owner_execution_id.is_(None),
        EvaluationRun.status != "pending",
    )


def runs_with_an_owner() -> ColumnElement[bool]:
    """Rows some execution has claimed — the complement of ``runs_owned_by_nobody``.

    For a caller that is NOT an execution acting on its own runs: the
    stranded-run sweep acts on runs whose execution is gone, so it has no
    ``execution_id`` to compare against.

    It must EXCLUDE runs no execution ever claimed from the owner's clock,
    because for those no owner clock exists — such a run either predates
    ownership tracking (no backfill) or a trigger path failed to stamp it, in
    which case a live worker may still hold it. The sweep reaches them through
    a separate arm, keyed on the executions of their evaluation.

    Through the sweep's outer join, an unowned row's NULL execution state would
    already fail the owned arm; this states the exclusion rather than
    inheriting it from NULL semantics.
    """
    return EvaluationRun.owner_execution_id.is_not(None)


def run_owned_by_execution_join() -> ColumnElement[bool]:
    """The join linking a run to the execution that owns it.

    ``owner_execution_id`` carries **no database foreign key**, so the join
    must be spelled out, and spelled out it is a comparison on the ownership
    column, which the structural guard forbids outside this module.
    """
    from app.models.orchestration import OrchestrationExecution

    return OrchestrationExecution.id == EvaluationRun.owner_execution_id
