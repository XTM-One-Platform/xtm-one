# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases: the order in which a case's decisions wait for a person (#1606).

A case moves scoping -> validation -> mobilization. Under human in the loop
each phase that waits is one ``needs_human`` work item keyed by its gate
(``ctem_case_gate_records.GATE_ITEM_PREFIXES``).

The order of the decisions is the agent's judgement, stated in its persona,
never a refusal (#1675): :func:`mobilize_before_review_warning` answers a
warning when a ``ctem.mobilize`` decision is filed while a
``ctem.analyst_review`` or ``ctem.validation.non_readonly`` decision of the
same case still waits, and the item is written.

Two card-hygiene rules hold at the write, whatever the prompt says, because
they protect what a person reads, not the order of the work:

- :func:`refuse_question_change`: an open gate whose question changes is
  cancelled with a note naming its replacement and filed again, never edited
  in place: the stored decision and the cards already sent keep the question
  they were written for.
- :func:`refuse_run_pointer`: a gate card carries its question, evidence and
  options; it never sends the person to a run, a trace or a transcript.

Autonomous mode files no gate item, so nothing here holds it: the case moves
from phase to phase by itself. Pure checks plus one read of the open items.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any

from app.services.ctem_case_gate_records import GATE_ITEM_PREFIXES

MOBILIZE = "ctem.mobilize"
#: The decisions a mobilize decision waits behind, for the same case.
MOBILIZE_PREREQUISITES: tuple[str, ...] = ("ctem.analyst_review", "ctem.validation.non_readonly")
#: The gate keys of builtin_agents.CTEM_*_GATE_PREFIXES the case page does not draw.
_EXTRA_GATE_PREFIXES: tuple[str, ...] = ("ctem:f1:launch:",)
_RUN_POINTER = re.compile(
    r"\b(?:see|read|open|check|look at|refer to|details in)\s+(?:the\s+|this\s+|that\s+)?"
    r"(?:agent\s+|assignment\s+|loop\s+)?(?:run|trace|transcript)\b",
    re.I,
)


def _key(correlation_key: object) -> str:
    return correlation_key.strip() if isinstance(correlation_key, str) else ""


def gate_of(correlation_key: object) -> tuple[str, str] | None:
    """``(gate key, subject id)`` of a CTEM gate item key, else ``None``. Pure."""
    key = _key(correlation_key)
    for gate, prefixes in GATE_ITEM_PREFIXES.items():
        for prefix in prefixes:
            if key.startswith(prefix) and key[len(prefix) :].strip():
                return gate, key[len(prefix) :].strip()
    for prefix in _EXTRA_GATE_PREFIXES:
        if key.startswith(prefix) and key[len(prefix) :].strip():
            return prefix.rstrip(":"), key[len(prefix) :].strip()
    return None


def prerequisite_keys(case_id: str) -> list[str]:
    """The keys of the review and validation decisions a mobilize decision of *case_id* waits behind. Pure."""
    return [prefix + case_id for gate in MOBILIZE_PREREQUISITES for prefix in GATE_ITEM_PREFIXES[gate]]


def mobilize_blocked_message(waiting: list[str]) -> str:
    """The warning on a mobilize decision filed before its review and validation are answered. Pure."""
    return (
        "this ctem.mobilize decision was filed while the review and validation of its case still wait: "
        f"{', '.join(waiting)}. The person may answer mobilize before knowing what validation proves; "
        "if that was not your intent, cancel it and file it again once they are answered"
    )


async def mobilize_before_review_warning(
    db: Any,
    correlation_key: object,
    needs_human: object,
    *,
    user_id: uuid.UUID | None,
    workspace_id: uuid.UUID | None,
) -> str | None:
    """The warning when a mobilize decision is parked ahead of its review or validation, else ``None``.

    Never a refusal (#1675): the agent decides the order; the item is written.
    """
    if not needs_human:
        return None
    found = gate_of(correlation_key)
    if found is None or found[0] != MOBILIZE:
        return None
    from sqlalchemy import or_, select

    from app.models.work_item import WORK_ITEM_OPEN_STATUSES, WorkItem

    scopes = [WorkItem.user_id == user_id] if user_id is not None else []
    if workspace_id is not None:
        scopes.append(WorkItem.workspace_id == workspace_id)
    if not scopes:
        return None
    waiting = (
        (
            await db.execute(
                select(WorkItem.correlation_key).where(
                    WorkItem.correlation_key.in_(prerequisite_keys(found[1])),
                    WorkItem.status.in_(WORK_ITEM_OPEN_STATUSES),
                    WorkItem.needs_human.is_(True),
                    or_(*scopes),
                )
            )
        )
        .scalars()
        .all()
    )
    return mobilize_blocked_message(sorted({str(k) for k in waiting})) if waiting else None


def _question(ask: object) -> str:
    if not isinstance(ask, Mapping):
        return ""
    return " ".join(str(ask.get("question") or "").split()).casefold()


def refuse_question_change(correlation_key: object, stored_ask: object, new_ask: object, *, waiting: bool) -> None:
    """``ValueError`` when the question of a waiting CTEM gate is changed in place. Pure."""
    if not waiting or gate_of(correlation_key) is None:
        return
    before, after = _question(stored_ask), _question(new_ask)
    if before and after and before != after:
        raise ValueError(
            "the question of a waiting CTEM gate is never changed in place: cancel this item with a note "
            "naming its replacement and file the new decision as a new item; nothing was written"
        )


def refuse_run_pointer(correlation_key: object, ask: object) -> None:
    """``ValueError`` when a CTEM gate card sends the person to a run instead of carrying its evidence. Pure."""
    if gate_of(correlation_key) is None or not isinstance(ask, Mapping):
        return
    texts = [ask.get("question"), ask.get("context")]
    texts += [o.get(f) for o in ask.get("options") or [] if isinstance(o, Mapping) for f in ("label", "effect")]
    for text in texts:
        if isinstance(text, str) and _RUN_POINTER.search(text):
            raise ValueError(
                "a CTEM gate card carries its question, evidence and options itself: write the evidence in the "
                "context instead of sending the person to a run, trace or transcript; nothing was written"
            )


__all__ = [
    "MOBILIZE",
    "MOBILIZE_PREREQUISITES",
    "gate_of",
    "mobilize_before_review_warning",
    "mobilize_blocked_message",
    "prerequisite_keys",
    "refuse_question_change",
    "refuse_run_pointer",
]
