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

"""Existing decision cards rewritten for a person, once per release (#776, slice 5).

Agents now write a readable ask when they park an item for a person (slice 3),
but the items parked before that still show what agents wrote for agents: a
``blocked_reason`` with ids, timestamps and message ids, or an ask whose
question holds them. At the first boot of a build (:func:`on_release_deployed`,
scheduled by ``platform_builds.record_running_build``) this pass reads every
open ``needs_human`` item of every fleet whose card is not readable
(:func:`card_unreadable`) and, on the platform routing model, writes the
question, the context and the options a person reads into its ask:

* a readable ask that only lacks its context keeps its question, options,
  recommendation and deadline: only the context is added;
* any other is rebuilt from the item's title, description, ask and
  ``blocked_reason``; a question it replaces is kept at the end of
  ``blocked_reason``, which the card shows under "Technical details".

A rewrite is stored only when :func:`app.services.work_item_ask.normalize_ask`
accepts it and it is complete (question, context, 2 options or more); the rest
are logged with their id and fleet, and ``get_fleet_health`` reports them as
``decision_card_not_readable``. Idempotent: a readable card is never read
again, so a second pass changes nothing. One process per build runs it (a
Redis lease keyed by the build); nothing runs while agentic AI may not.
No status, owner, activity time or event is written: the ask (and the kept
question) only, and no model is called on any read path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import format_exception_message
from app.services.work_item_ask import normalize_ask, person_ask_missing, unreadable_reasons, with_source_links

logger = logging.getLogger(__name__)

#: Items rewritten per release at most (the next release takes the rest).
MAX_ITEMS = 200
#: How long the per-build lease is kept: one pass per build, whatever its replicas.
LEASE_SECONDS = 30 * 24 * 3600
LEASE_PREFIX = "work_items:readable_ask_backfill:"
_MAX_OUTPUT_TOKENS = 700
_TITLE_CHARS = 300
_DESCRIPTION_CHARS = 3000
_REASON_CHARS = 1500
_ASK_CHARS = 1500
KEPT_QUESTION_PREFIX = "Question before the readable card rewrite:"

SYSTEM_PROMPT = (
    "You rewrite a decision card that a person reads in a work queue. The input is a work item an "
    "agent parked for a person: its title, description, technical blocked reason and current ask. "
    "Answer with ONE JSON object and nothing else: "
    '{"question": one sentence, the decision in the reader\'s terms, '
    '"context": 2 to 4 short plain lines separated by \\n (what the matter is, what was found, '
    'what each answer leads to), "options": 2 to 4 objects {"key": "A", "label": the button text, '
    '"effect": what choosing it does}, "recommendation": the key of the option the item recommends}. '
    "Plain words only: no timestamp, no id, no UUID, no SHA, no message id, no "
    "correlation or timer key, no 'card sent' or 'msg'; name things by what they are. Keep the "
    "item's language. When the item does not say what the decision is, answer {}."
)
CONTEXT_PROMPT = (
    "You add the context to a decision card a person reads. The input is a work item and its "
    "question. Answer with ONE JSON object and nothing else: "
    '{"context": 2 to 4 short plain lines separated by \\n (what the matter is, what was found, '
    "what each answer leads to)}. Plain words only: no timestamp, no id, no UUID, no SHA, no "
    "message id, no correlation or timer key. Keep the item's language. When the item does not say "
    "enough, answer {}."
)

#: ``(system prompt, user prompt) -> the model's answer``, ``None`` when unreachable.
Rewriter = Callable[[str, str], Awaitable[str | None]]


def _texts(ask: dict[str, Any]) -> list[str]:
    texts = [str(ask.get("question") or ""), str(ask.get("context") or "")]
    for option in ask.get("options") or []:
        if isinstance(option, dict):
            texts.append(f"{option.get('label') or ''} {option.get('effect') or ''}")
    return texts


def ask_readable(ask: object) -> bool:
    """Whether *ask* has a question a person can read (no id, timestamp or key in its texts)."""
    if not isinstance(ask, dict) or not str(ask.get("question") or "").strip():
        return False
    return not any(unreadable_reasons(text) for text in _texts(ask))


def card_complete(ask: object) -> bool:
    """Whether *ask* is complete for both rules: #776 (question, context, 2+ options) and #762 (a recommendation).

    A card this pass stores must pass ``refuse_needs_human_without_ask``, so a
    later write that keeps ``needs_human`` on the item is never refused. Pure.
    """
    from app.services.work_item_blocked_refs import ask_is_complete

    return not person_ask_missing(ask) and ask_is_complete(ask)


def card_unreadable(ask: object) -> bool:
    """Whether the card of a ``needs_human`` item fails a person: unreadable or incomplete.

    Pure, no model call: what ``get_fleet_health`` reports and this pass selects.
    """
    return not ask_readable(ask) or not card_complete(ask)


def _clip(value: object, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit]


def item_prompt(item: Any) -> str:
    """What the model reads of *item*, bounded."""
    parts = [f"Title: {_clip(getattr(item, 'title', '') or '', _TITLE_CHARS)}"]
    if getattr(item, "description", None):
        parts.append(f"Description:\n{_clip(item.description, _DESCRIPTION_CHARS)}")
    if getattr(item, "blocked_reason", None):
        parts.append(f"Blocked reason (technical):\n{_clip(item.blocked_reason, _REASON_CHARS)}")
    if getattr(item, "ask", None):
        parts.append(f"Current ask:\n{_clip(item.ask, _ASK_CHARS)}")
    return "\n\n".join(parts)


_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def parse_answer(answer: str | None) -> dict[str, Any]:
    """The JSON object of a model answer (fenced or not); ``{}`` when there is none."""
    if not answer:
        return {}
    match = _OBJECT.search(answer)
    if match is None:
        return {}
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


@dataclass
class Rewrite:
    """What the pass writes on one item: the new ask, and the blocked_reason when a question was kept."""

    ask: dict[str, Any]
    blocked_reason: str | None = None


async def rewrite_item(item: Any, rewriter: Rewriter) -> Rewrite | None:
    """The readable ask of *item*, or ``None`` when it cannot be written with confidence."""
    current = item.ask if isinstance(getattr(item, "ask", None), dict) else None
    if (
        current is not None
        and ask_readable(current)
        and len(current.get("options") or []) >= 2
        and str(current.get("recommendation") or "").strip()
    ):
        # A readable structured ask is kept: only its missing context is added.
        answer = parse_answer(await rewriter(CONTEXT_PROMPT, f"{item_prompt(item)}\n\nQuestion: {current['question']}"))
        candidate = {**current, "context": answer.get("context")}
    else:
        answer = parse_answer(await rewriter(SYSTEM_PROMPT, item_prompt(item)))
        if not answer:
            return None
        candidate = {k: answer.get(k) for k in ("question", "context", "options", "recommendation")}
        candidate = {k: v for k, v in candidate.items() if v not in (None, "", [])}
        for kept in ("deadline", "links"):
            if current and current.get(kept):
                candidate[kept] = current[kept]
    try:
        ask = normalize_ask(candidate)
    except ValueError:
        return None
    if ask is None or not card_complete(ask):
        return None
    # The sources the reader opens to decide, from the item's own text (#834): no model.
    ask = with_source_links(ask, getattr(item, "description", None), getattr(item, "payload", None)) or ask
    reason = str(getattr(item, "blocked_reason", None) or "")
    old_question = str((current or {}).get("question") or "").strip()
    if old_question and old_question != ask["question"]:
        kept = f"{KEPT_QUESTION_PREFIX} {old_question}"
        reason = f"{reason.rstrip()}\n\n{kept}" if reason.strip() else kept
        return Rewrite(ask=ask, blocked_reason=reason)
    return Rewrite(ask=ask)


@dataclass
class BackfillReport:
    rewritten: list[str] = field(default_factory=list)
    #: Readable cards that only gained their source links (no model call).
    linked: list[str] = field(default_factory=list)
    #: ``(item id, fleet id or "")`` of the cards left unreadable.
    unrewritable: list[tuple[str, str]] = field(default_factory=list)


async def _waiting_items(db: Any) -> list[Any]:
    """The open items parked for a person, oldest first."""
    from sqlalchemy import select

    from app.models.work_item import WORK_ITEM_OPEN_STATUSES, WorkItem

    result = await db.execute(
        select(WorkItem)
        .where(WorkItem.status.in_(WORK_ITEM_OPEN_STATUSES), WorkItem.needs_human.is_(True))
        .order_by(WorkItem.created_at)
    )
    return list(result.scalars().all())


async def _fleets_by_workspace(db: Any) -> dict[Any, str]:
    from sqlalchemy import select

    from app.models.fleet import Fleet

    rows = (await db.execute(select(Fleet.id, Fleet.workspace_id))).all()
    return {workspace: str(fleet_id) for fleet_id, workspace in rows if workspace is not None}


async def _store(db: Any, item_id: Any, values: dict[str, Any]) -> bool:
    """A plain column write: no status, activity time or platform event; whether it landed."""
    from sqlalchemy import update

    from app.models.work_item import WorkItem

    result = await db.execute(
        update(WorkItem)
        .where(WorkItem.id == item_id, WorkItem.needs_human.is_(True))
        .values(**values)
        .returning(WorkItem.id)
    )
    return result.first() is not None


async def backfill_readable_asks(db: Any, rewriter: Rewriter, *, limit: int = MAX_ITEMS) -> BackfillReport:
    """Rewrite the unreadable cards of the open ``needs_human`` items; what was done. Commits nothing."""
    waiting = await _waiting_items(db)
    pending = [item for item in waiting if card_unreadable(item.ask)][:limit]
    report = BackfillReport()
    for item in waiting:
        # A readable card without its sources gains them from the item's own text (#834).
        if item in pending or not isinstance(item.ask, dict) or item.ask.get("links"):
            continue
        linked = with_source_links(item.ask, getattr(item, "description", None), getattr(item, "payload", None))
        if linked is not item.ask and await _store(db, item.id, {"ask": linked}):
            report.linked.append(str(item.id))
    if not pending:
        return report
    fleets = await _fleets_by_workspace(db)
    for item in pending:
        rewrite = await rewrite_item(item, rewriter)
        if rewrite is None:
            report.unrewritable.append((str(item.id), fleets.get(item.workspace_id, "")))
            continue
        values: dict[str, Any] = {"ask": rewrite.ask}
        if rewrite.blocked_reason is not None:
            values["blocked_reason"] = rewrite.blocked_reason
        if await _store(db, item.id, values):
            report.rewritten.append(str(item.id))
    return report


async def _routing_rewriter(db: Any) -> Rewriter | None:
    """A :data:`Rewriter` on the platform routing model; ``None`` when none is set."""
    from app.llm.base import ChatMessage
    from app.llm.router import get_llm_for_user
    from app.services.flow_llm import routing_model
    from app.services.langfuse_tracing import traced_generation

    model, provider_id = await routing_model(db)
    if not model:
        return None
    llm, resolved = await get_llm_for_user(db, None, model_override=model, provider_id_override=provider_id)
    name = resolved or model

    async def rewrite(system: str, user: str) -> str | None:
        messages = [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)]
        try:
            async with traced_generation(name="work_item_ask_backfill", model=name, messages=messages) as gen:
                gen.response = await llm.chat(
                    messages=messages, model=name, temperature=0.2, max_tokens=_MAX_OUTPUT_TOKENS
                )
            return gen.response.content or None
        except Exception as exc:
            logger.warning("Decision card rewrite failed: %s", format_exception_message(exc))
            return None

    return rewrite


def _lease_key(release: dict[str, Any]) -> str:
    return f"{LEASE_PREFIX}{release.get('version') or ''}:{release.get('git_sha') or ''}"


#: The builds whose pass ran in this process: the scheduler tick asks no more of them.
_done: set[str] = set()


async def _lease_held(release: dict[str, Any]) -> bool:
    """Whether a process already ran (or runs) the pass of this build; ``False`` when unread."""
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        return bool(await redis.exists(_lease_key(release)))
    except Exception as exc:
        logger.debug("Decision card backfill lease not read: %s", format_exception_message(exc))
        return False


async def _take_lease(release: dict[str, Any]) -> bool:
    from app.core.redis import redis_client

    key = _lease_key(release)
    try:
        redis = await redis_client()
        return bool(await redis.set(key, "1", nx=True, ex=LEASE_SECONDS))
    except Exception as exc:
        logger.debug("Decision card backfill lease not taken: %s", format_exception_message(exc))
        return False


async def on_release_deployed(release: dict[str, Any], *, rewriter: Rewriter | None = None) -> BackfillReport | None:
    """The pass of one build, run by one process; ``None`` when it did not run. Never raises."""
    try:
        from app.core.database import async_session
        from app.services.agentic_gate import agentic_refusal

        if not release.get("version"):
            return None
        if _lease_key(release) in _done:
            return None
        if await _lease_held(release):
            _done.add(_lease_key(release))
            logger.warning("Decision card backfill skipped for %s: another process ran it.", _lease_key(release))
            return None
        async with async_session() as db:
            refusal = await agentic_refusal(db)
            if refusal is not None:
                # Not taken: the scheduler tick asks again (:func:`schedule_if_due`).
                logger.warning("Decision card backfill deferred: agentic AI may not run (%s).", refusal)
                return None
            if rewriter is None:
                rewriter = await _routing_rewriter(db)
            if rewriter is None:
                logger.warning("Decision card backfill deferred: no routing model is set.")
                return None
            if not await _take_lease(release):
                _done.add(_lease_key(release))
                logger.warning("Decision card backfill skipped for %s: lease not taken.", _lease_key(release))
                return None
            _done.add(_lease_key(release))
            report = await backfill_readable_asks(db, rewriter)
            await db.commit()
        label = str(release.get("release") or release.get("version"))
        # WARNING, not INFO: production keeps WARNING and above, and the count is
        # what tells a pass that ran and found nothing from one that never ran (#834).
        logger.warning(
            "Decision card backfill of release %s: %d card(s) rewritten for their reader, %d given their "
            "source links, %d left unreadable.",
            label,
            len(report.rewritten),
            len(report.linked),
            len(report.unrewritable),
        )
        for item_id, fleet_id in report.unrewritable:
            logger.warning("Decision card not readable: item %s, fleet %s", item_id, fleet_id or "none")
        return report
    except Exception as exc:
        logger.warning("Decision card backfill not run: %s", format_exception_message(exc))
        return None


_tasks: set[asyncio.Task[Any]] = set()


def schedule_readable_ask_backfill(release: dict[str, Any]) -> None:
    """Run :func:`on_release_deployed` in the background (a startup step must not wait on a model)."""
    coro = on_release_deployed(dict(release))
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:  # no loop: nothing to schedule on
        coro.close()
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def schedule_if_due(release: dict[str, Any]) -> None:
    """The scheduler tick's retry of the running build's pass (#806).

    The boot call (``record_running_build``) runs before the licence and the AI
    switch are read, so its refusal used to end the build's only chance. The
    lease is taken only once the gates pass: until then each tick asks again,
    and once a process ran the pass nothing more is read.
    """
    if not release.get("version") or _lease_key(release) in _done:
        return
    if any(not task.done() for task in _tasks):
        return
    schedule_readable_ask_backfill(release)


__all__ = [
    "BackfillReport",
    "Rewrite",
    "ask_readable",
    "backfill_readable_asks",
    "card_unreadable",
    "on_release_deployed",
    "parse_answer",
    "rewrite_item",
    "schedule_if_due",
    "schedule_readable_ask_backfill",
]
