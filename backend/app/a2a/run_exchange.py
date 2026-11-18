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

"""Where an external run stands in its task's exchange with the caller.

An exposed agent that calls ``ask_the_caller`` ends its run: the run is
``completed`` and the A2A task is ``input-required``, its question on
``A2ATask.caller_question``. The caller's answer starts a second run on the
same task (``tasks.answer_question``), and ``A2ATask.run_id`` moves to it. The
run pages read neither the task nor that history on their own, so a run that
asked read "Completed / No result produced" and the run that continued it
stood alone. :func:`run_exchanges` is the one projection they read instead.

Per run, two halves, either or both (a continued run can ask again):

* ``asked``: this run asked its caller ``question``. ``state`` is ``waiting``
  while the task is ``input-required`` on it, ``answered`` once the caller's
  answer started ``answered_by_run_id``, ``withdrawn`` when the task was
  cancelled before an answer (by its partner, or by the lifetime bound).
* ``answers``: this run continues the task after its caller answered
  ``question``; ``previous_run_id`` is the run that asked.

A linked run the viewer cannot open is not linked (``None``): the same rule as
a delegation's parent run. Runs answered before the links were stamped carry
no ``answered`` half; the rest still reads from the task.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: ``trigger_payload`` key of a continued run: the run that asked.
PREVIOUS_RUN_KEY = "a2a_previous_run_id"
#: ``trigger_payload`` key of a run that asked: the run its answer started.
ANSWERED_BY_RUN_KEY = "a2a_answered_by_run_id"
#: ``trigger_payload`` key of a continued run holding the question it answers
#: (written by ``tasks.answer_question``, read by the executor too).
QUESTION_KEY = "a2a_question"

EXCHANGE_WAITING = "waiting"
EXCHANGE_ANSWERED = "answered"
EXCHANGE_WITHDRAWN = "withdrawn"


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


async def run_exchanges(
    db: AsyncSession,
    runs: Iterable[Any],
    *,
    user_id: uuid.UUID,
    is_admin: bool,
) -> dict[uuid.UUID, dict[str, Any]]:
    """The exchange of each external run in *runs*, keyed by run id.

    *runs* are ``AssignmentRun`` rows (or anything with ``id``,
    ``trigger_context`` and ``trigger_payload``). Runs with no exchange are
    absent. Four batched queries at most, none for a page of internal runs.
    """
    from app.api.v1.helpers import run_visible_filter
    from app.models.a2a import A2ATask
    from app.models.assignment import AssignmentRun

    external = [r for r in runs if isinstance(r.trigger_context, dict) and r.trigger_context.get("source") == "a2a"]
    if not external:
        return {}

    out: dict[uuid.UUID, dict[str, Any]] = {}
    payloads = {r.id: (r.trigger_payload if isinstance(r.trigger_payload, dict) else {}) for r in external}

    # The halves stamped on the runs themselves.
    answered_by: dict[uuid.UUID, uuid.UUID] = {}
    previous: dict[uuid.UUID, uuid.UUID | None] = {}
    for run_id, payload in payloads.items():
        nxt = _uuid(payload.get(ANSWERED_BY_RUN_KEY))
        if nxt is not None:
            answered_by[run_id] = nxt
        if payload.get(QUESTION_KEY):
            previous[run_id] = _uuid(payload.get(PREVIOUS_RUN_KEY))

    # A run still asking is the one its task points at, with the question on it.
    asking = (
        await db.execute(
            select(A2ATask.run_id, A2ATask.caller_question, A2ATask.cancel_requested_at).where(
                A2ATask.run_id.in_([r.id for r in external]),
                A2ATask.caller_question.isnot(None),
            )
        )
    ).all()

    # The linked runs. The question an answered run asked lives on the run its
    # answer started; it is this run's own question, so it is read whoever may
    # open that run, but a link is only given to a run the viewer may open.
    linked = set(answered_by.values()) | {rid for rid in previous.values() if rid is not None}
    linked_payloads: dict[uuid.UUID, dict] = {}
    openable: set[uuid.UUID] = set()
    if linked:
        rows = (
            await db.execute(
                select(AssignmentRun.id, AssignmentRun.trigger_payload).where(AssignmentRun.id.in_(linked))
            )
        ).all()
        linked_payloads = {row[0]: (row[1] if isinstance(row[1], dict) else {}) for row in rows}
        openable = set(linked_payloads)
        if not is_admin and openable:
            openable = set(
                (
                    await db.execute(
                        select(AssignmentRun.id).where(AssignmentRun.id.in_(openable), run_visible_filter(user_id))
                    )
                ).scalars()
            )

    for run_id, nxt in answered_by.items():
        question = (linked_payloads.get(nxt) or {}).get(QUESTION_KEY)
        if question:
            out.setdefault(run_id, {})["asked"] = {
                "question": str(question),
                "state": EXCHANGE_ANSWERED,
                "answered_by_run_id": str(nxt) if nxt in openable else None,
            }
    for run_id, question, cancel_requested_at in asking:
        out.setdefault(run_id, {})["asked"] = {
            "question": question,
            "state": EXCHANGE_WITHDRAWN if cancel_requested_at else EXCHANGE_WAITING,
            "answered_by_run_id": None,
        }
    for run_id, prev in previous.items():
        out.setdefault(run_id, {})["answers"] = {
            "question": str(payloads[run_id][QUESTION_KEY]),
            "answer": str(payloads[run_id].get("message") or ""),
            "previous_run_id": str(prev) if prev is not None and prev in openable else None,
        }
    return out
