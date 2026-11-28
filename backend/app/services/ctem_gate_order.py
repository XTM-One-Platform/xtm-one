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

One rule holds at the case write (#1954): a gate reaches ``waited`` only
through its decision. :func:`waited_refusal` refuses a ``waited`` gate event
while no open ``needs_human`` decision item keyed for that gate and case
exists (:func:`open_gate_item_keys`), so a case never reads "waiting for a
person" with nothing in anyone's Waiting for you list. The same holds for an
agent's bare state move to ``waiting_decision`` (#2782):
:func:`waiting_state_refusal` refuses it while no open decision of the case,
whatever its key, exists.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping
from typing import Any

from app.services.ctem_case_gate_records import GATE_ITEM_PREFIXES

logger = logging.getLogger(__name__)

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


#: Keys a gate's decision may also be filed under, past ``GATE_ITEM_PREFIXES`` (the F1 launch key of a validation).
_WAITED_EXTRA_PREFIXES: dict[str, tuple[str, ...]] = {"ctem.validation.non_readonly": _EXTRA_GATE_PREFIXES}


def waited_item_keys(gate: str, case_id: str) -> list[str]:
    """The decision item keys a ``waited`` event of *gate* on *case_id* may rest on; empty: any ``ctem:`` key of the case. Pure."""
    prefixes = (*GATE_ITEM_PREFIXES.get(gate, ()), *_WAITED_EXTRA_PREFIXES.get(gate, ()))
    return [prefix + case_id for prefix in prefixes]


def waited_refusal(gate: str, case_id: str, open_keys: set[str]) -> str | None:
    """Why a ``waited`` gate event of *case_id* is refused, else ``None``. Pure.

    *open_keys* are the correlation keys of the open ``needs_human`` items of the
    case (:func:`open_gate_item_keys`). A gate with keys of its own (every gate of
    the catalog since #2464) needs one of them, so ``decision_filed`` is per gate;
    a gate outside the catalog needs any open ``ctem:`` decision of the case.
    """
    keys = waited_item_keys(gate, case_id)
    if keys and any(key in open_keys for key in keys):
        return None
    if not keys and any(key.startswith("ctem:") and key.endswith(case_id) for key in open_keys):
        return None
    named = ", ".join(keys) if keys else f"a ctem: key of the gate ending in {case_id}"
    return (
        f"the gate {gate} waits only on its decision, and no open decision item keyed {named} exists for this "
        "case: file it first with create_work_item (that correlation_key, needs_human true, an ask with its "
        "question, options and recommendation, addressed to the person or team who decides), then write the "
        "gate outcome waited"
    )


def waiting_state_refusal(case_id: str, open_keys: set[str]) -> str | None:
    """Why an agent may not move *case_id* to ``waiting_decision`` without a waited gate (#2782), else ``None``. Pure.

    The case then waits on a decision the page can show: one open ``needs_human``
    item keyed ``ctem:<x>:<case id>`` (*open_keys*, :func:`open_gate_item_keys`).
    A person moving the state by hand with a reason is not bound (UX r1 B2 on #1515).
    """
    if any(key.startswith("ctem:") and key.endswith(case_id) for key in open_keys):
        return None
    return (
        f"the case waits only on a decision, and no open decision item of this case exists: file it first with "
        f"create_work_item (correlation_key ctem:, then what it decides, then :{case_id}, for instance "
        f"ctem:evidence:{case_id}; needs_human true, an ask with "
        "its question, options and recommendation, addressed to the person or team who decides), then move the "
        "case to waiting_decision; nothing was written"
    )


async def pending_decisions(case_id: str, *, user_id: str) -> list[dict[str, Any]]:
    """The open decisions *case_id* waits on that *user_id* may read (#2782), oldest first.

    One ``{work_item_id, key, title, deadline}`` per open ``needs_human`` item keyed
    ``ctem:<x>:<case id>``, read with the user's own work-item visibility. Never
    raises: an unreadable list answers ``[]``, so a case read never fails on it.
    """
    from app.services import work_item as work_item_service

    try:
        found = await work_item_service.list_work_items(
            user_id=user_id, correlation_key_prefix="ctem:", search=case_id, open_only=True, limit=50
        )
    except Exception:
        logger.warning("CTEM case pending decisions unreadable; the case read answers without them", exc_info=True)
        return []
    pending = [
        {
            "work_item_id": item.get("id"),
            "key": item.get("correlation_key"),
            "title": item.get("title"),
            "deadline": item.get("due_at") or ((item.get("ask") or {}).get("deadline")),
            "_at": item.get("created_at") or "",
        }
        for item in found
        if item.get("needs_human") and str(item.get("correlation_key") or "").endswith(case_id)
    ]
    pending.sort(key=lambda p: str(p.pop("_at")))
    return pending


async def open_gate_item_keys(db: Any, case_id: str) -> set[str]:
    """The correlation keys of the open ``needs_human`` ``ctem:`` items of *case_id*, whoever reads them.

    Read without the viewer's visibility on purpose: it answers whether a decision
    exists (a key, never its content), the same read as the stall sweep
    (``ctem_case_stall._waits_on_someone``).
    """
    from sqlalchemy import or_, select

    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem
    from app.services.ctem_decision_groups import GROUPED_REASON_LEAD

    rows = await db.execute(
        select(WorkItem.correlation_key).where(
            # A grouped decision waits on its group's card (#2451): it is filed all the same.
            or_(WorkItem.needs_human.is_(True), WorkItem.blocked_reason.like(f"{GROUPED_REASON_LEAD}%")),
            WorkItem.status.not_in(WORK_ITEM_TERMINAL_STATUSES),
            WorkItem.correlation_key.like("ctem:%" + case_id),
        )
    )
    return {str(key) for key in rows.scalars().all() if key}


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


#: An option or context saying another case holds part of the exposure (#2778).
_OTHER_CASE_CLAIM = re.compile(
    r"\b(?:tracked|held|followed|covered|handled|kept)\b[^.\n]{0,60}?\b(?:its own|own|a separate|separate|"
    r"another|a new|new|other|a different|different)\s+case\b",
    re.I,
)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def claims_other_case(ask: object) -> bool:
    """Whether a gate card says another case tracks part of the exposure. Pure."""
    if not isinstance(ask, Mapping):
        return False
    texts = [ask.get("context")]
    texts += [o.get(f) for o in ask.get("options") or [] if isinstance(o, Mapping) for f in ("label", "effect")]
    return any(isinstance(text, str) and _OTHER_CASE_CLAIM.search(text) for text in texts)


def _case_ids(text: object) -> set[uuid.UUID]:
    return {uuid.UUID(found) for found in _UUID.findall(text)} if isinstance(text, str) else set()


def missing_case_message(subject: str) -> str:
    """The warning on a gate whose key names no case. Pure."""
    return (
        f"this gate's key names the case {subject}, and no such case exists: the decision will not show on any "
        "case page and its answer resumes nothing. Check the case id and file the decision under the right key"
    )


UNCHECKED_CASE_CLAIM_MESSAGE = (
    "this gate card says another case tracks part of the exposure, and no existing case id is cited in "
    "blocked_reason: the platform cannot check that case exists. Cite its id in blocked_reason, or, when "
    "record_ctem_signal was refused or not yet recorded, write the exposure as pending with its retry item "
    "instead of tracked in a case"
)


async def case_reference_warnings(db: Any, correlation_key: object, ask: object, blocked_reason: object) -> list[str]:
    """What a CTEM gate card states about cases that the platform could not find (#2778). Never a refusal.

    (1) The case the gate key names must exist. (2) A card saying another case
    tracks part of the exposure must cite that case's id in *blocked_reason*
    (the card itself carries no id, #776), and that case must exist. One read of
    ``ctem_cases``; a key that is not a CTEM gate reads nothing.
    """
    found = gate_of(correlation_key)
    if found is None:
        return []
    try:
        subject = uuid.UUID(found[1])
    except ValueError:
        subject = None
    claim = claims_other_case(ask)
    cited = _case_ids(blocked_reason) - ({subject} if subject else set())
    wanted = ({subject} if subject else set()) | (cited if claim else set())
    if not wanted:
        return [UNCHECKED_CASE_CLAIM_MESSAGE] if claim else []
    from sqlalchemy import select

    from app.models.ctem_case import CtemCase

    rows = await db.execute(select(CtemCase.id).where(CtemCase.id.in_(sorted(wanted, key=str))))
    existing = {uuid.UUID(str(row)) for row in rows.scalars().all() if row}
    warnings: list[str] = []
    if subject is not None and subject not in existing:
        warnings.append(missing_case_message(found[1]))
    if claim and not (cited & existing):
        warnings.append(UNCHECKED_CASE_CLAIM_MESSAGE)
    return warnings


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
    "UNCHECKED_CASE_CLAIM_MESSAGE",
    "case_reference_warnings",
    "claims_other_case",
    "gate_of",
    "missing_case_message",
    "mobilize_before_review_warning",
    "mobilize_blocked_message",
    "open_gate_item_keys",
    "pending_decisions",
    "prerequisite_keys",
    "refuse_question_change",
    "refuse_run_pointer",
    "waited_item_keys",
    "waited_refusal",
    "waiting_state_refusal",
]
