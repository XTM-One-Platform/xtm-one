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

"""The connected prompt guard's verdict on a piece of text.

A prompt guard (Nova, while its integration is connected) evaluates text
before the platform acts on it, the user's chat message first.
:func:`evaluate_text` is the one way to ask it. The memory-write screen is the
exception: it runs after the write, in the background, in
``builtin_tools/memory.py``.

It takes no session, on purpose. A guard reads the database while it
evaluates (its rules, its models, the embedding and LLM providers), and the
evaluation is bounded: a bound that lands while one of those statements runs
cancels it, which aborts the transaction of the session it ran on. Handed the
request's session, a guard timeout failed the request on its next statement
and lost what the request had already written. :func:`evaluate_text` opens a
short-lived session of its own for the connection-state lookup and the
evaluation, and closes it; a guard that raises or runs out of time costs its
verdict and nothing else: it is skipped and logged (fail-open). That session
is statement-scoped (``core/database.StatementScopedSession``), like the
memory-write screen's: it holds a pooled connection only while one of its
statements runs, never while the engine waits on an embedding or a model.

With no guard connected a turn must cost nothing: the "none connected" answer
is kept for :data:`CONNECTION_STATE_TTL_SECONDS`, so such a turn opens no
session at all. A connected guard is looked up again on every evaluation, on
the session the evaluation opens anyway, so disconnecting it applies at once
and connecting it within that bound.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.utils import format_exception_message

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.nova_guard import NovaMatch

logger = logging.getLogger(__name__)

#: The integration whose connection activates the guard.
GUARD_PROVIDER = "nova"

SURFACE_CHAT_MESSAGE = "chat_message"
SURFACE_MEMORY_WRITE = "memory_write"
SURFACE_TOOL_RESULT = "tool_result"
#: The text of a file a public widget visitor attached.
SURFACE_WIDGET_UPLOAD = "widget_upload"

SURFACE_MODEL_OUTPUT = "model_output"

#: The bound on one evaluation, connection-state lookup included. Main's Nova
#: engine asks every ``llm:`` condition of the rule set in turn with no bound
#: of its own, and a chat message waited for all of them before this bound
#: existed: it only cuts an evaluation no realistic rule set takes.
GUARD_EVALUATION_TIMEOUT_SECS = 60.0

#: How long "no guard is connected" is reused before it is read again: the
#: bound ``ai_state`` and the security options keep for a switch.
CONNECTION_STATE_TTL_SECONDS = 5.0

# Monotonic time until which "no guard is connected" holds without a read.
_none_connected_until = 0.0


def invalidate_connection_cache() -> None:
    """Forget the cached "no guard is connected" answer."""
    global _none_connected_until
    _none_connected_until = 0.0


def _remember_none_connected() -> None:
    global _none_connected_until
    _none_connected_until = time.monotonic() + CONNECTION_STATE_TTL_SECONDS


def _none_connected() -> bool:
    return _none_connected_until > time.monotonic()


def known_unconnected() -> bool:
    """Whether "no guard is connected" is known right now, without a read.

    For a caller with work to do before :func:`evaluate_text` (reading a
    tool result's attachments): it skips that work while no guard is there.
    ``False`` does not mean one is connected, only that nobody asked lately.
    """
    return _none_connected()


@dataclass(frozen=True)
class GuardVerdict:
    """The rules a guard matched on one piece of text; none when it matched nothing."""

    matches: tuple[NovaMatch, ...] = ()
    provider: str = GUARD_PROVIDER

    @property
    def flagged(self) -> bool:
        return bool(self.matches)

    @property
    def rule_action(self) -> str | None:
        """``block`` when a matched rule blocks, ``log`` when they only log, ``None`` when nothing matched."""
        from app.services.nova_guard import worst_action

        return worst_action(list(self.matches))

    @property
    def blocked(self) -> bool:
        return self.rule_action == "block"

    @property
    def rules(self) -> list[str]:
        return [match.rule_name for match in self.matches]

    @property
    def categories(self) -> list[str]:
        return sorted({match.category for match in self.matches if match.category})

    @property
    def source(self) -> str:
        """The ``source`` of its audit entry: ``nova_guard`` for Nova."""
        return f"{self.provider}_guard"


NOT_FLAGGED = GuardVerdict()


async def _evaluate(text: str, user_id: str | None) -> GuardVerdict:
    from app.core.database import StatementScopedSession, async_session
    from app.services.integration_state import any_provider_connected
    from app.services.nova_engine import evaluate_prompt

    async with async_session() as session:
        guard_db = StatementScopedSession(session)
        if not await any_provider_connected(guard_db, GUARD_PROVIDER):
            _remember_none_connected()
            return NOT_FLAGGED
        matches = await evaluate_prompt(guard_db, text, user_id=user_id)
    return GuardVerdict(tuple(matches)) if matches else NOT_FLAGGED


async def evaluate_text(
    text: str,
    *,
    surface: str,
    user_id: str | None = None,
    timeout: float | None = None,
) -> GuardVerdict:
    """What the connected guard says about *text*. Never raises.

    :data:`NOT_FLAGGED` when no guard is connected, when nothing matched, and
    when the guard failed or took longer than *timeout* (by default
    :data:`GUARD_EVALUATION_TIMEOUT_SECS`): the caller acts on the verdict
    without asking first whether a guard exists.
    """
    if not text or _none_connected():
        return NOT_FLAGGED
    bound = GUARD_EVALUATION_TIMEOUT_SECS if timeout is None else timeout
    try:
        return await asyncio.wait_for(_evaluate(text, user_id), timeout=bound)
    except Exception as exc:
        logger.warning(
            "Prompt guard '%s' could not evaluate a %s, skipped: %s",
            GUARD_PROVIDER,
            surface,
            format_exception_message(exc),
        )
        return NOT_FLAGGED


async def guard_chat_message(
    db: AsyncSession,
    message: str,
    *,
    user_id: str | None,
    entity_id: Any,
    entity_name: str,
    details: dict[str, Any],
    enforce: bool = True,
    audit_anchor_user_id: str | None = None,
    anchor_agent_id: Any = None,
    surface: str = SURFACE_CHAT_MESSAGE,
    timeout: float | None = None,
) -> str | None:
    """Evaluate a user's chat message; the refusal to answer it with, or ``None``.

    A match is audited on *db* (the request's session, which the evaluation
    never touches) as a ``security_alert`` whose *details* are the surface's
    own context. With *enforce*, a ``block`` rule refuses the message before
    any LLM call and the entry's ``action`` is the rule's (agent chat, as it
    always was). Without it the message continues whatever the rule, and the
    entry says so: ``rule_action`` is the rule's, ``action`` is ``allowed``.

    A writer with no account (the public widget) is audited as the guard
    itself, anchored on *audit_anchor_user_id*: ``log_security_event`` drops an
    entry it cannot attribute to a user. With *anchor_agent_id*, the match of
    a user deleted mid-turn is kept under the guard's system actor, anchored
    on that agent's owner.

    *surface* and *timeout* are the evaluation's (:func:`evaluate_text`): the
    text of a file a widget visitor attached is checked the same way, as
    :data:`SURFACE_WIDGET_UPLOAD` (its *details* name the surface too).
    """
    verdict = await evaluate_text(message, surface=surface, user_id=user_id, timeout=timeout)
    if not verdict.flagged:
        return None
    from app.services.nova_guard import NOVA_REFUSAL_MESSAGE

    refused = enforce and verdict.blocked
    outcome = (
        {"action": verdict.rule_action, "blocked": verdict.blocked}
        if enforce
        else {"rule_action": verdict.rule_action, "action": "allowed", "blocked": False}
    )
    entry = {
        "action": "security_alert",
        "entity_type": "security",
        "entity_id": entity_id,
        "entity_name": entity_name,
        "summary": f"{verdict.provider.capitalize()} rule match",
        "details": {
            "source": verdict.source,
            "rules": verdict.rules,
            "categories": verdict.categories,
            **details,
            **outcome,
        },
    }
    if user_id is not None:
        from app.services.audit import log_security_event

        await log_security_event(
            db,
            user_id=user_id,
            system_actor=verdict.source if anchor_agent_id is not None else None,
            anchor_agent_id=anchor_agent_id,
            **entry,
        )
    elif audit_anchor_user_id is not None:
        from app.services.audit import log_system_audit

        await log_system_audit(db, actor=verdict.source, anchor_user_id=audit_anchor_user_id, **entry)
    else:
        logger.warning(
            "Prompt guard '%s' matched a chat message (rules=%s, refused=%s) with no user to audit it under",
            verdict.provider,
            verdict.rules,
            refused,
        )
    return NOVA_REFUSAL_MESSAGE if refused else None


# ---------------------------------------------------------------------------
# The model's answer
# ---------------------------------------------------------------------------

#: Delivered instead of an answer a guard withheld; nothing of it survives.
MODEL_OUTPUT_WITHHELD_MESSAGE = (
    "The answer was withheld: a safety rule matched its content. "
    "Please rephrase your request or contact an administrator."
)

#: The bound on one evaluation of an answer. In ``block`` mode the answer
#: waits for it; past it the answer is delivered unevaluated.
MODEL_OUTPUT_TIMEOUT_SECS = 20.0
#: The most text one evaluation reads: a longer answer is evaluated on its start.
MODEL_OUTPUT_MAX_CHARS = 20_000
#: Background evaluations running at once, per event loop: each holds a
#: pooled connection and, with ``llm:`` rules, the guard's model calls.
MODEL_OUTPUT_CONCURRENCY = 4
#: Background evaluations waiting or running, per process: past it an answer
#: is not evaluated, and the log says so.
MODEL_OUTPUT_BACKLOG = 64

_output_tasks: set[asyncio.Task] = set()
_output_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _output_slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slot = _output_slots.get(loop)
    if slot is None:
        slot = _output_slots[loop] = asyncio.Semaphore(MODEL_OUTPUT_CONCURRENCY)
    return slot


def _user_uuid(user_id: str | None) -> str | None:
    try:
        return str(uuid.UUID(str(user_id)))
    except (TypeError, ValueError):
        return None


async def _audit_model_output(
    verdict: GuardVerdict,
    *,
    withheld: bool,
    streamed: bool,
    user_id: str | None,
    audit_anchor_user_id: str | None,
    entity_id: Any,
    entity_name: str,
    details: dict[str, Any],
) -> None:
    """Audit a match on an answer on a session of its own. Never raises.

    The request's session may be closed (a background evaluation) or about to
    commit the answer, so the entry is committed here. An accountless answer
    (a widget visitor, an anonymous channel user) is audited as the guard,
    anchored on *audit_anchor_user_id*, else on the platform admin.
    """
    from sqlalchemy import func, select

    from app.core.config import settings
    from app.core.database import async_session
    from app.models.user import User
    from app.services.audit import log_security_event, log_system_audit

    entry = {
        "action": "security_alert",
        "entity_type": "security",
        "entity_id": entity_id,
        "entity_name": entity_name,
        "summary": f"{verdict.provider.capitalize()} rule match on a model answer",
        "details": {
            "source": verdict.source,
            "surface": SURFACE_MODEL_OUTPUT,
            "rules": verdict.rules,
            "categories": verdict.categories,
            **details,
            "rule_action": verdict.rule_action,
            "action": "block" if withheld else "allowed",
            "blocked": withheld,
            "streamed": streamed,
        },
    }

    async def _write() -> None:
        async with async_session() as audit_db:
            user = _user_uuid(user_id)
            if user is not None:
                await log_security_event(audit_db, user_id=user, **entry)
            else:
                anchor = audit_anchor_user_id or (
                    await audit_db.execute(select(User.id).where(func.lower(User.email) == settings.admin_email))
                ).scalar_one_or_none()
                if anchor is None:
                    logger.warning(
                        "Prompt guard '%s' matched a model answer (rules=%s) with no user to audit it under",
                        verdict.provider,
                        verdict.rules,
                    )
                    return
                await log_system_audit(audit_db, actor=verdict.source, anchor_user_id=anchor, **entry)
            await audit_db.commit()

    try:
        await asyncio.wait_for(_write(), timeout=MODEL_OUTPUT_TIMEOUT_SECS)
    except Exception as exc:
        logger.warning("Could not audit a prompt guard match on a model answer: %s", format_exception_message(exc))


async def _evaluate_answer(text: str, user_id: str | None) -> GuardVerdict:
    return await evaluate_text(
        text[:MODEL_OUTPUT_MAX_CHARS],
        surface=SURFACE_MODEL_OUTPUT,
        user_id=user_id,
        timeout=MODEL_OUTPUT_TIMEOUT_SECS,
    )


async def _evaluate_delivered_answer(text: str, *, streamed: bool, user_id: str | None, **audit: Any) -> None:
    try:
        async with _output_slot():
            verdict = await _evaluate_answer(text, user_id)
            if verdict.flagged:
                await _audit_model_output(verdict, withheld=False, streamed=streamed, user_id=user_id, **audit)
    except Exception as exc:
        logger.warning("Background evaluation of a model answer failed: %s", format_exception_message(exc))


async def guard_model_output(
    text: str | None,
    *,
    streamed: bool,
    user_id: str | None,
    entity_id: Any,
    entity_name: str,
    details: dict[str, Any],
    audit_anchor_user_id: str | None = None,
) -> str | None:
    """The answer to deliver: *text*, or :data:`MODEL_OUTPUT_WITHHELD_MESSAGE`.

    ``PROMPT_GUARD_MODEL_OUTPUT`` decides. ``off`` returns *text* and
    evaluates nothing. ``log`` returns *text* at once and evaluates it in the
    background (bounded, a strong reference held until it ends), auditing a
    match. ``block`` evaluates an answer delivered whole (*streamed* false)
    before returning, and withholds it when a ``block`` rule matched; a
    *streamed* answer has already been shown, so it is evaluated and audited
    in the background as in ``log``. A guard outage or timeout delivers the
    answer.
    """
    from app.core.security_options import security_option

    mode = security_option("prompt_guard_model_output")
    if mode not in ("log", "block") or not text or not text.strip():
        return text
    audit = {
        "audit_anchor_user_id": audit_anchor_user_id,
        "entity_id": entity_id,
        "entity_name": entity_name,
        "details": details,
    }
    if mode == "block" and not streamed:
        verdict = await _evaluate_answer(text, user_id)
        if not verdict.flagged:
            return text
        await _audit_model_output(verdict, withheld=verdict.blocked, streamed=False, user_id=user_id, **audit)
        return MODEL_OUTPUT_WITHHELD_MESSAGE if verdict.blocked else text
    if len(_output_tasks) >= MODEL_OUTPUT_BACKLOG:
        logger.warning(
            "Prompt guard evaluations of model answers are backed up (%d); this answer is not evaluated",
            len(_output_tasks),
        )
        return text
    task = asyncio.get_running_loop().create_task(
        _evaluate_delivered_answer(text, streamed=streamed, user_id=user_id, **audit)
    )
    _output_tasks.add(task)
    task.add_done_callback(_output_tasks.discard)
    return text
