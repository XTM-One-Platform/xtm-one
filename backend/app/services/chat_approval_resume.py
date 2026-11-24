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

"""Resuming a chat turn that ended at a gated tool call (FR-002c1/c2).

Two shapes of approval exist, and they are not interchangeable. Where the
transport can reach the client mid-turn — the web chat's stream, the platform
stream — the loop blocks, the reviewer answers, and the *same* turn continues
(FR-002c). Where it cannot, the turn has to end and the decision arrives on a
later request. This module is the second shape.

The distinction that matters, and the one FR-002c3 exists to state: handing a
decision to a turn still waiting in memory is *not* this. That is the first
shape wearing a second request as a costume, and it fails the only test that
counts — the process holding the paused turn can be gone. Restarted, or a
different replica entirely. Everything here is therefore rebuilt from what was
persisted: the proposals on the assistant message, and nothing else.

What makes that possible without a durable store of loop state is that a
proposal is self-describing. It carries the call id, the tool name and the exact
arguments the reviewer was shown, so the resumed turn can reconstruct the call
rather than asking the model to propose it again. That is required, not merely
convenient: FR-002c2 forbids re-planning, because a reviewer who approved one
action must not get another.

What is deliberately *not* here: any expiry on a clock. A proposal is retired
when the conversation moves on (FR-002c4), never because a reviewer was slow.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Message
from app.tools.approval import DECISION_IMPERSONATED_BY

logger = logging.getLogger(__name__)

# The metadata key the paused turn writes its proposals under, and the key this
# module writes when the conversation moves past them. Named here so the writer
# (``chat.py``) and every reader agree; a typo in either would silently look
# like "no approval pending", which is the one failure mode this feature cannot
# afford to have.
PENDING_KEY = "pending_approvals"
RETIRED_KEY = "approvals_retired"


class PartialDecisionSetError(Exception):
    """Decisions arrived for some of the pending calls but not all of them.

    Refused rather than part-applied. The conversation holds one assistant
    message with N ``tool_use`` blocks, and strict-mode Anthropic/OpenAI reject
    it unless every one has a matching ``tool_result`` — so applying a subset
    would produce exactly the orphaning the batch pre-screen exists to prevent.
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(
            "Decisions missing for pending tool call(s): " + ", ".join(missing)
        )


def _load_metadata(message: Message) -> dict[str, Any]:
    """Parse a message's metadata, treating unreadable as empty.

    Unreadable metadata must not be an exception here. It would turn a
    corrupted row into a 500 on every send to that conversation, locking the
    user out of a conversation rather than losing one pending approval — and the
    fail-closed reading of a proposal we cannot read is that nothing is pending,
    so nothing runs.
    """
    raw = getattr(message, "metadata_json", None)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Unreadable metadata on message %s; treating as no pending approvals",
            message.id,
        )
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def latest_assistant_message(
    db: AsyncSession, conversation_id: str | uuid.UUID
) -> Message | None:
    """The most recent assistant message in a conversation.

    Ordered by ``sequence`` and then ``created_at``: two messages written inside
    one turn can share a timestamp at the resolution Postgres stores, and
    picking the wrong one of those would read a stale proposal set.
    """
    result = await db.execute(
        select(Message)
        .where(
            Message.conversation_id == uuid.UUID(str(conversation_id)),
            Message.role == "assistant",
        )
        .order_by(Message.sequence.desc(), Message.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def load_pending(
    db: AsyncSession, conversation_id: str | uuid.UUID
) -> tuple[Message, list[dict]] | None:
    """The message carrying pending proposals, and the proposals, or ``None``.

    Only the *latest* assistant message is consulted. An older one may still
    carry proposals in its metadata — retired ones keep them for the expired
    state FR-002c4 requires — and treating those as answerable would let a
    decision reach a call the conversation has long since moved past.
    """
    message = await latest_assistant_message(db, conversation_id)
    if message is None:
        return None
    metadata = _load_metadata(message)
    if metadata.get(RETIRED_KEY):
        return None
    proposals = metadata.get(PENDING_KEY) or []
    if not isinstance(proposals, list) or not proposals:
        return None
    return message, usable(proposals)


def usable(proposals: Any) -> list[dict]:
    """The proposals every helper here agrees to act on.

    A proposal with no ``tool_call_id`` cannot be seeded as a tool call, cannot
    be matched to a decision, and cannot be approved — but
    ``_apply_approval_decisions`` iterates whatever it is given and appends one
    ``tool`` message per entry. Feeding it an unfiltered list therefore produced
    a ``tool_result`` with ``tool_call_id=None`` and no matching ``tool_use``:
    exactly the orphaning the completeness rule exists to prevent, and only when
    one bad entry sat among good ones. Filtering once, here, keeps the four
    helpers from disagreeing about what counts.
    """
    return [
        p
        for p in (proposals or [])
        if isinstance(p, dict) and p.get("tool_call_id")
    ]


def normalize_decisions(raw: Any) -> dict[str, dict]:
    """Index a client's decisions by call id, dropping anything unusable.

    Shaped like the approve routes' ``decisions`` on purpose: one
    decision format across every surface, so a client that already speaks one
    needs no second vocabulary to speak the other.
    """
    decisions: dict[str, dict] = {}
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        call_id = entry.get("tool_call_id") or entry.get("call_id")
        if not call_id:
            continue
        # Server-set only: a client naming the approver could attribute its
        # grant to any administrator, or erase one.
        decisions[str(call_id)] = {k: v for k, v in entry.items() if k != DECISION_IMPERSONATED_BY}
    return decisions


def require_complete(proposals: list[dict], decisions: dict[str, dict]) -> None:
    """Refuse a decision set that does not cover every pending call.

    Raises :class:`PartialDecisionSetError`, which the endpoint reports as 422 with
    the missing ids named — a client bug is worth telling the client about
    precisely, since the alternative is a conversation that half-resumes.
    """
    missing = [
        str(p.get("tool_call_id"))
        for p in proposals
        if p.get("tool_call_id")
        and str(p.get("tool_call_id")) not in decisions
    ]
    if missing:
        raise PartialDecisionSetError(missing)


def approved_ids(proposals: list[dict], decisions: dict[str, dict]) -> set[str]:
    """The call ids a human actually approved.

    Uses the same allow-list the applier uses, rather than a truthiness check on
    the verdict string. A value the transport let through — a typo, a stale
    client's spelling — must not become consent on the way past.
    """
    from app.tools.approval import is_approval

    return {
        str(p.get("tool_call_id"))
        for p in proposals
        if p.get("tool_call_id")
        and is_approval((decisions.get(str(p.get("tool_call_id"))) or {}).get("decision"))
    }


def approved_shapes(proposals: list[dict], decisions: dict[str, dict]) -> dict[str, str]:
    """``{call_id: fingerprint of the schema the human was shown}``.

    The companion to :func:`approved_ids`. An approved id bypasses the gate, and
    a resumable proposal deliberately never expires on a clock — so without a
    record of WHAT was approved, a turn reopened days later runs against
    whatever the tool has since become. This is what makes the re-check at
    execution time possible (FR-002c4, spec.md:207).
    """
    from app.tools.approval import fingerprint

    ids = approved_ids(proposals, decisions)
    return {
        str(p.get("tool_call_id")): fingerprint(p.get("input_schema"))
        for p in proposals
        if str(p.get("tool_call_id")) in ids
    }


def seed_gated_assistant_message(messages: list, proposals: list[dict]) -> None:
    """Put back the assistant message whose calls were never allowed to run.

    The paused turn did not persist it. It could not: what the client had to be
    shown was prose explaining that approval was needed, and a message carrying
    unexecuted ``tool_use`` blocks with no matching results is exactly the
    orphan strict-mode providers reject. So the tool calls live on in the
    proposals instead, and this rebuilds the message from them.

    Order is preserved from the proposal list, which is the order the model
    emitted the calls, because ``_apply_approval_decisions`` appends one result
    per call in that same order and the pairing is positional in every provider
    format.
    """
    from app.llm.base import ChatMessage
    from app.tools.base import ToolCall

    calls = [
        ToolCall(
            id=str(p.get("tool_call_id")),
            name=str(p.get("tool_name") or ""),
            arguments=dict(p.get("arguments") or {}),
        )
        for p in proposals
        if p.get("tool_call_id")
    ]
    if not calls:
        return
    messages.append(
        ChatMessage(
            role="assistant",
            # Empty rather than the prose the user saw. That text was written
            # for a person who could not answer; replaying it to the model would
            # tell it, in its own voice, that it had already given up.
            content="",
            tool_calls=calls,
        )
    )


async def mark_answered_by_id(
    db: AsyncSession, message_id: uuid.UUID | str
) -> None:
    """Re-fetch the message by id and mark its proposal set answered.

    The id rather than the instance, because the caller has just run a whole
    agent turn: anything inside it that rolls the session back — a failed
    platform read in the gate builder does exactly that, and the error is
    swallowed — expires every ORM object it was holding. Touching one then
    raises lazy IO outside a greenlet, which would 500 the request *after* the
    approved tool had run and leave the set answerable, so a retry would run it
    a second time.

    Silent when the row has gone: there is nothing to mark, and failing here
    would discard a turn that already succeeded.
    """
    result = await db.execute(
        select(Message).where(Message.id == uuid.UUID(str(message_id)))
    )
    message = result.scalar_one_or_none()
    if message is None:
        logger.warning(
            "Message %s vanished before its approvals could be marked answered",
            message_id,
        )
        return
    mark_answered(message)


def mark_answered(message: Message) -> None:
    """Record that this proposal set has been decided.

    Answered rather than deleted: the message stays as the record of what was
    asked, and a second decision arriving for it finds nothing pending and is
    refused (FR-020) instead of running the call twice.

    Synchronous, and takes no session: it mutates the ORM object and the
    caller's commit persists it. It previously accepted a session it never
    touched, which reads as though it writes on its own — the misreading that
    leaves a mutation uncommitted somewhere down the line.
    """
    metadata = _load_metadata(message)
    metadata.pop(PENDING_KEY, None)
    metadata[RETIRED_KEY] = "answered"
    message.metadata_json = json.dumps(metadata) if metadata else None


async def retire_pending(
    db: AsyncSession, conversation_id: str | uuid.UUID
) -> bool:
    """Retire a pending proposal because the conversation moved on (FR-002c4).

    Called when a user sends a new message instead of deciding, **on every send
    path**. The proposal stops being answerable, and the surface renders it as
    **expired** rather than removing it — which is why the proposals are kept
    and only a retirement marker is added. A question the product asked and
    nobody answered should leave a trace; a gated call that never ran should not
    vanish from the conversation that produced it.

    Two places hold a pending set, and both have to be cleared or the surfaces
    disagree about whether the prompt is live:

    * the assistant **message metadata**, written by a turn that ended (REST);
    * the conversation's **Redis key**, written by a turn that is waiting or was
      waiting (the web chat's stream and the platform stream), and read back by
      ``GET /conversations/{id}``.

    Retiring only the first would leave a streamed prompt answerable: the user
    abandoned it, typed something else, reloaded, and would be served the same
    proposals as live because the conversation response reads Redis. The Redis
    set is also the *only* copy on a stream, so it is copied onto the message
    on the way out — otherwise there is nothing for the expired state to render
    there.

    Returns whether anything was retired, so the caller can log it. There is no
    clock here and there must not be one: FR-015e's "no timeout, no
    auto-resolution" is about the person deciding, on every surface.
    """
    from app.services.chat_turn_guard import (
        clear_pending_approvals,
        get_pending_approvals,
    )

    conv_key = str(conversation_id)

    # Redis first: it is the copy a live-turn surface wrote, and the one the
    # conversation response reads. Never fatal — a Redis outage must not stop a
    # user sending their next message.
    live_proposals: list[dict] = []
    try:
        live_proposals = usable(await get_pending_approvals(conv_key))
    except Exception:
        logger.warning(
            "Could not read the pending approvals for %s while retiring", conv_key,
            exc_info=True,
        )

    pending = await load_pending(db, conversation_id)
    message = pending[0] if pending else await latest_assistant_message(db, conversation_id)
    stored_proposals = pending[1] if pending else []

    if not live_proposals and not stored_proposals:
        return False

    if message is not None:
        metadata = _load_metadata(message)
        already_settled = bool(metadata.get(RETIRED_KEY))
        if already_settled and not stored_proposals:
            # The latest assistant message has already been answered or retired,
            # so it is not the message these Redis proposals belong to — the turn
            # that paused never persisted one. Stamping them here would relabel a
            # message whose approval *was* given and whose tool *did* run as
            # "needed your approval and was never run", and would overwrite its
            # settled marker. Clear Redis and leave the transcript alone: an
            # unrenderable expired set is a smaller wrong than a false one.
            logger.info(
                "Retiring %d orphaned pending approval(s) on %s without a "
                "message to attach them to: the latest assistant message is "
                "already %s",
                len(live_proposals),
                conv_key,
                metadata.get(RETIRED_KEY),
            )
        else:
            # Union, not either-or. Both stores can hold a set — a turn that
            # ended wrote one to the message while an earlier live turn left one
            # in Redis — and dropping the second retired it with no record
            # anywhere, so nothing could render it as expired: the exact outcome
            # this function exists to prevent. Deduplicated by call id, the
            # message's copy winning, since that is the one the reviewer saw.
            merged: dict[str, dict] = {
                str(p["tool_call_id"]): p for p in live_proposals
            }
            merged.update(
                {str(p["tool_call_id"]): p for p in stored_proposals}
            )
            metadata[PENDING_KEY] = list(merged.values())
            metadata[RETIRED_KEY] = "superseded"
            message.metadata_json = json.dumps(metadata)

    try:
        await clear_pending_approvals(conv_key)
    except Exception:
        logger.warning(
            "Could not clear the pending approvals for %s after retiring", conv_key,
            exc_info=True,
        )

    logger.info(
        "Retired %d pending approval(s) on conversation %s: the conversation moved on",
        len(stored_proposals or live_proposals),
        conv_key,
    )
    return True


# --- one rule, one place ---------------------------------------------------
#
# Applied per call site, each rule misses a surface. Three surfaces carry an
# approval (REST, the web chat's stream, the platform stream) and a fourth
# arrives with the chat channels, so the rule lives here and the call
# sites just call it.


async def retire_for_new_turn(
    db: AsyncSession, conversation_id: str | uuid.UUID, *, surface: str
) -> bool:
    """Retire anything pending because a new turn is starting (FR-002c4).

    Call this from **every** send path. ``surface`` names the caller for the log
    line, which is the only thing that differs between them.

    Best-effort by construction: retiring a stale prompt is housekeeping, and
    housekeeping that fails must not cost the user the message they just sent.
    That guard belongs here rather than at each call site — it was duplicated
    twice and absent once.
    """
    try:
        retired = await retire_pending(db, conversation_id)
    except Exception:
        logger.warning(
            "Could not retire the pending approval on %s (%s)",
            conversation_id,
            surface,
            exc_info=True,
        )
        return False
    if retired:
        logger.info(
            "Retired a pending approval on %s (%s): a new message arrived",
            conversation_id,
            surface,
        )
    return retired


#: ``AuditLog.entity_type`` of an approval decision on an assignment run, the
#: same as its refusals: one place answers "who decided what on this run".
RUN_APPROVAL_ENTITY = "assignment_run_approval"


def approval_verdict_rows(proposals: Any, decisions: Any) -> list[dict[str, Any]]:
    """One ``{tool_call_id, tool, verdict, reason}`` per proposal, in call order.

    A call the set carries no verdict for reads ``undecided``: it is refused by
    default, and the row must not claim a verdict nobody gave.
    """
    decided = decisions if isinstance(decisions, dict) else {}
    rows: list[dict[str, Any]] = []
    for proposal in proposals or []:
        if not isinstance(proposal, dict):
            continue
        call_id = str(proposal.get("tool_call_id") or "")
        decision = decided.get(call_id)
        decision = decision if isinstance(decision, dict) else None
        rows.append(
            {
                "tool_call_id": call_id,
                "tool": proposal.get("tool_name"),
                "verdict": str(decision.get("decision") or "reject") if decision else "undecided",
                "reason": (decision or {}).get("rejection_reason") or None,
            }
        )
    return rows


async def record_approval_decision(
    db: AsyncSession,
    *,
    run: Any,
    user: Any | None,
    surface: str | None,
    system_actor: str | None = None,
) -> None:
    """Audit the verdicts just stored on *run* (``approval_decisions``).

    One row per decision set, one entry per call in ``details.verdicts``: the
    actor, each verdict, its reason, the tool, the run and the surface. A
    person's decision is written under *user*; a decision no person made (the
    deadline) passes ``user=None`` and *system_actor*, and is written as
    ``system:<actor>`` anchored on the run's owner, so the trail never credits
    a person with it. Flushed in the caller's transaction, inside a savepoint,
    and never raises: an audit write that failed must not cost the decision.
    """
    from app.services.audit import log_audit, log_system_audit

    verdicts = approval_verdict_rows(run.pending_approvals, run.approval_decisions)
    if not verdicts:
        return
    counts: dict[str, int] = {}
    for row in verdicts:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    tally = ", ".join(f"{n} {verdict}" for verdict, n in sorted(counts.items()))
    details = {"run_id": str(run.id), "surface": surface, "verdicts": verdicts}
    try:
        async with db.begin_nested():
            if user is not None:
                await log_audit(
                    db,
                    user=user,
                    action="decide",
                    entity_type=RUN_APPROVAL_ENTITY,
                    entity_id=run.id,
                    summary=f"Approval decision on run {run.id}: {tally}",
                    details=details,
                )
            elif system_actor and run.user_id:
                await log_system_audit(
                    db,
                    actor=system_actor,
                    anchor_user_id=run.user_id,
                    action="decide",
                    entity_type=RUN_APPROVAL_ENTITY,
                    entity_id=run.id,
                    summary=f"Approval decision on run {run.id} by {system_actor}: {tally}",
                    details=details,
                )
    except Exception:
        logger.warning("Could not audit the approval decision on run %s (%s)", run.id, surface, exc_info=True)


async def record_refused_decision(
    db: AsyncSession,
    *,
    user_id: str,
    conversation_id: str | uuid.UUID | None,
    reason: str,
    surface: str,
    summary: str,
    extra: dict[str, Any] | None = None,
    entity_type: str = "chat_tool_approval",
) -> None:
    """Record that a decision was refused (FR-008a, SC-017, FR-020).

    Every surface refuses for one of a small set of reasons — the caller is not
    the user whose call is paused, there is no pending proposal, the decision set
    is partial — and FR-008a requires each be recorded rather than silently
    ignored. One function so a new surface cannot quietly skip it.

    ``entity_type`` names what was refused. Assignment runs pass
    ``assignment_run_approval`` and their run id: the requirement is about the
    attempt, not the surface it arrived on, and assignments had no trail at all
    — someone walking run ids against the batch endpoint was refused every time
    and left nothing an investigator could find.

    Never raises. An audit write that failed must not turn the refusal it was
    describing into a 500: the caller is already on its way to answering 404,
    409 or 422, and that answer is the part the user depends on.
    """
    from app.services.audit import log_security_event

    try:
        await log_security_event(
            db,
            user_id=str(user_id),
            action="refuse",
            entity_type=entity_type,
            entity_id=str(conversation_id) if conversation_id else None,
            summary=summary,
            details={"reason": reason, "surface": surface, **(extra or {})},
        )
        await db.commit()
    except Exception:
        logger.warning(
            "Could not record a refused approval decision on %s (%s, %s)",
            conversation_id,
            surface,
            reason,
            exc_info=True,
        )

