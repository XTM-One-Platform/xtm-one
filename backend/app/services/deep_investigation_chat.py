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

"""Running a Deep Investigation as a chat turn.

The chat handler hands a subject to :func:`run_deep_investigation_chat` instead
of to the agent loop, and gets back the assistant text to persist. Everything
else on the turn — the user message, the ``done`` frame, attachments, the turn
guard — is untouched, which is the point: this is a routing branch, not a
second chat implementation.

Why the assistant text is generated, not written by a model
-----------------------------------------------------------
The message body is derived deterministically from the run's own summary and
carries the ``[[INVESTIGATION:uuid]]`` marker. No LLM writes it, and that is a
requirement rather than a shortcut: a model asked to summarise the run could
describe it as a success while the block below it shows two failed sources.
Generating the sentence from ``RunSummary`` makes the prose and the block
incapable of disagreeing — the same reason the block reads
``summary.all_succeeded`` instead of counting for itself.

The counts are ordered problems-first for the same reason the block's collapsed
line is: an analyst checks what did NOT work before what did.

Two entry points, and why
-------------------------
:func:`run_deep_investigation_turn` is the whole turn: metering, the quota
gate, the run, and a message for every way it can fail.
:func:`run_deep_investigation_chat` is just the run — subject in, message plus
marker out.

They are split because the turn used to live inline in the chat's turn handler,
where nothing could call it: its metering, its quota gate and its failure
mapping could only be asserted by matching the handler's source text, which is
how four of them came to be missing at once without a single test noticing.
Everything except the stream-specific listener lifecycle is here now, so those are
ordinary tests.

Why the frames need no transport of their own
---------------------------------------------
The web chat's turn runner already forwards any progress event as
``{"type": "status", **event}`` — the channel the agent loop uses for
``tool_start`` / ``segment_complete``. Passing the handler's existing
``on_event`` straight to the orchestrator is therefore the whole of the
transport work; the ``investigation_*`` frames ride the stream the client is
already listening to.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.services.investigation_events import build_investigation_marker
from app.services.investigation_orchestrator import (
    DeepInvestigationOrchestrator,
    InvestigationResult,
)
from app.services.investigation_packs import (
    MAX_LIST_SEEDS,
    classify_subject,
    domain_list,
    recommend_packs,
    too_many_domains,
)
from app.services.investigation_queriers import make_dispatching_querier
from app.services.investigation_report_status import clear_report_writing, mark_report_writing
from app.services.investigation_state import RunSummary

logger = logging.getLogger(__name__)

#: Cap on the subject a client may submit. A Deep Investigation subject is an
#: indicator, a CVE or an actor name — not a paragraph — and every source URL
#: is built from it, so an unbounded value would produce unbounded requests.
MAX_SUBJECT_LENGTH = 500


class EmptySubjectError(ValueError):
    """The client sent Deep Investigation mode with no subject to investigate."""


class TooManyDomainsError(ValueError):
    """The client pasted a list of more domains than one run investigates.

    Refused rather than cut: a run over the first sixty would read as the
    whole list investigated.
    """


class EmptySelectionError(ValueError):
    """The client confirmed a source-pack selection containing no packs.

    Distinct from ``None``, which means "no selection was made, use the
    recommendation". An explicit empty list is an answer — "none of these" —
    and honouring it by querying everything recommended is the opposite of
    what was asked. Refused rather than widened, for the same reason
    ``UnknownSourcePackError`` is refused rather than substituted: a run's
    persisted ``source_pack_slugs`` must never name packs nobody confirmed.
    """


def _summary_sentence(
    subject_value: str, subject_type: str, pack_slugs: list[str], summary: RunSummary
) -> str:
    """One factual line about what the run did, problems first.

    Deliberately dull and deliberately generated: see the module docstring on
    why no model writes this.
    """
    parts: list[str] = []
    by_status = summary.by_status
    if by_status.get("error"):
        parts.append(f"{by_status['error']} failed")
    if by_status.get("degraded"):
        parts.append(f"{by_status['degraded']} partial")
    if by_status.get("skipped"):
        parts.append(f"{by_status['skipped']} not reached")
    if by_status.get("empty"):
        parts.append(f"{by_status['empty']} with nothing")
    if by_status.get("completed"):
        parts.append(f"{by_status['completed']} with findings")

    packs = ", ".join(f"`{slug}`" for slug in pack_slugs) or "no pack"
    if summary.total == 0:
        # Worded like the orchestrator's own `end_reason` so the sentence and
        # the block's footer say the same thing, and to dodge the a/an
        # agreement a code-formatted type makes awkward either way.
        return (
            f"No source in {packs} covers subject type `{subject_type}`, so "
            f"**{subject_value}** was not investigated."
        )
    sources = "1 source" if summary.total == 1 else f"{summary.total} sources"
    return (
        f"Investigated **{subject_value}** (`{subject_type}`) across {sources} "
        f"from {packs}: {', '.join(parts)}."
    )


def validate_turn_request(
    subject: str, source_pack_slugs: list[str] | None
) -> str:
    """The refusals that need no database, no model and no network.

    Split out so they can run BEFORE metering and the quota gate. Both are
    decisions about the request as it arrived — a blank subject, or a picker
    confirmed with nothing ticked — and neither queries a source or writes a
    row. Charging for them spent a user's agentic quota on a mis-click, and a
    few mis-clicks could lock them out of the feature; the metric merely
    drifts, the quota is actually gone.

    Returns the cleaned, bounded subject so the caller does not strip twice.
    Called from `run_deep_investigation_chat` as well, because that entry
    point is public and must refuse the same things when reached directly.
    """
    cleaned = (subject or "").strip()
    if not cleaned:
        raise EmptySubjectError("Deep Investigation needs a subject to investigate.")
    # Checked before any work, including `classify_subject`: reading the
    # guards top to bottom should show every refusal ahead of everything else.
    if source_pack_slugs is not None and not source_pack_slugs:
        raise EmptySelectionError(
            "No source packs were selected, so there is nothing to "
            "investigate. Pick at least one and try again."
        )
    # A pasted list of domains is kept whole: the run reads its seeds from it
    # and stores a summary (`list_summary`), so the subject column still
    # holds what is stored.
    if domain_list(cleaned):
        return cleaned
    if too_many_domains(cleaned):
        raise TooManyDomainsError(
            f"A list investigation takes up to {MAX_LIST_SEEDS} domains. "
            "Split the list and try again."
        )
    return cleaned[:MAX_SUBJECT_LENGTH]


async def run_deep_investigation_chat(
    db: AsyncSession,
    *,
    subject: str,
    user_id: uuid.UUID | str | None = None,
    conversation_id: uuid.UUID | str | None = None,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    source_pack_slugs: list[str] | None = None,
    max_iterations: int | None = None,
    cancel_event: asyncio.Event | None = None,
    packs: list | None = None,
    continues_investigation_id: uuid.UUID | None = None,
    pack_options: dict[str, dict[str, str]] | None = None,
    subject_type: str | None = None,
) -> tuple[str, InvestigationResult]:
    """Run one investigation and return ``(assistant_text, result)``.

    ``assistant_text`` is what the caller persists as the assistant message:
    a generated one-liner plus the ``[[INVESTIGATION:uuid]]`` marker the chat
    renderer turns into the live block.

    The subject is required and bounded — see :data:`MAX_SUBJECT_LENGTH`. A
    blank one raises rather than starting a run that could not query anything,
    because the client is the one that got it wrong and should say so.
    """
    cleaned = validate_turn_request(subject, source_pack_slugs)
    # A continuation names its parent's type rather than re-reading it from
    # the stored subject: a list run is stored under its summary ("56 domains:
    # a.com, ..."), which classifies as `unknown` and would plan nothing.
    subject_type = subject_type or classify_subject(cleaned)
    if source_pack_slugs is None:
        # No picker in this slice: the recommendation stands on its own, and
        # the packs actually used are persisted on the run either way, so the
        # record never claims a pack that was not queried.
        source_pack_slugs = [pack.slug for pack in recommend_packs(subject_type)]

    orchestrator = DeepInvestigationOrchestrator(
        db,
        # Routes by source kind: HTTP as before, plus knowledge bases
        # and MCP tools. It takes the session because those two need one — the
        # web fetcher still needs nothing.
        # The user reaches the web-search querier (whose provider key is
        # theirs) AND the knowledge-base querier, so the corpus read is
        # re-filtered at QUERY time rather than only when the pack was picked.
        make_dispatching_querier(db, user_id=user_id),
        on_event=on_event,
    )
    # This turn writes the run's report once it ends: said from the run's
    # first read, so the block shows "writing" on the writer's word rather
    # than a guess, and cleared once the writer is done, whatever it wrote.
    marked: list[uuid.UUID] = []

    async def _report_coming(run_id: uuid.UUID) -> None:
        marked.append(run_id)
        await mark_report_writing(run_id)

    try:
        result = await orchestrator.run(
            subject_value=cleaned,
            subject_type=subject_type,
            source_pack_slugs=source_pack_slugs,
            cancel_event=cancel_event,
            user_id=uuid.UUID(str(user_id)) if user_id else None,
            conversation_id=uuid.UUID(str(conversation_id)) if conversation_id else None,
            max_iterations=max_iterations,
            packs=packs,
            continues_investigation_id=continues_investigation_id,
            on_created=_report_coming,
            pack_options=pack_options,
        )
    except BaseException:
        for run_id in marked:
            await clear_report_writing(run_id)
        raise

    sentence = _summary_sentence(
        result.investigation.subject_value,
        result.investigation.subject_type,
        list(result.investigation.source_pack_slugs or []),
        result.summary,
    )

    # The synthesis across sources, on top of the run rather than inside it:
    # the orchestrator queries sources and stays free of model calls, and a
    # report that cannot be written must not fail a run whose sources answered.
    # A cancelled run gets no synthesis: Stop must stop the costly part too.
    cancelled = result.investigation.status == "cancelled" or (
        cancel_event is not None and cancel_event.is_set()
    )
    try:
        body = (
            None
            if cancelled
            else await _write_report(
                db,
                result,
                # As stored: a list run's summary, not the pasted list.
                subject=result.investigation.subject_value,
                subject_type=subject_type,
                user_id=user_id,
            )
        ) or sentence
    finally:
        for run_id in marked:
            await clear_report_writing(run_id)

    marker = build_investigation_marker(result.investigation.id)
    logger.info(
        "deep investigation %s finished: %s steps, status=%s",
        result.investigation.id,
        result.summary.total,
        result.investigation.status,
    )
    return f"{body}\n\n{marker}", result


async def run_deep_investigation_turn(
    db: AsyncSession,
    *,
    subject: str,
    user_id: uuid.UUID | str | None = None,
    conversation_id: uuid.UUID | str | None = None,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    source_pack_slugs: list[str] | None = None,
    cancel_event: asyncio.Event | None = None,
    result_out: list | None = None,
    packs: list | None = None,
    continues_investigation_id: uuid.UUID | None = None,
    pack_options: dict[str, dict[str, str]] | None = None,
    subject_type: str | None = None,
) -> str:
    """Run one Deep Investigation turn and return the assistant text.

    ``result_out``, when given, receives the ``InvestigationResult`` — for a
    caller that needs the run's id and not only its prose, such as the
    ``deep_investigation`` tool attaching the block to an agent's reply. It
    exists so that caller can go through THIS function rather than around it:
    everything below (metering, the quota gate, a message for every failure)
    is what the web chat's branch dropped once by routing around the wrapper,
    and a tool duplicating it would drop it again the next time one changed.

    Owns the responsibilities a chat turn carries beyond the run itself, all
    of which the web chat's branch silently dropped when it started routing
    around ``run_agent_chat``:

    * **Metering**, before the quota gate and mirroring ``agent_chat``: the
      execution happened whether or not it turned out to be chargeable.
    * **The quota gate.** The work is real — a bounded loop of outbound
      fetches, persisted state, and up to two synthesis calls at
      ``REPORT_MAX_TOKENS`` — so leaving it out made this mode a way around
      the per-user agentic limit. (This said "no model is called", which was
      true when it was written and stopped being true when the report landed;
      anyone setting quota policy from it would have under-counted the mode's
      cost. The synthesis call is traced like every other direct LLM call, so
      its tokens are attributable.)
    * **A message for every failure.** The turn has already consumed the
      user's message, so an exception that escaped here would surface as a
      socket error with no trace in the conversation.

    ``CancelledError`` is deliberately NOT mapped: it is a BaseException and
    must keep propagating so the turn unwinds and the orchestrator's own
    finalizer marks the run ``aborted``. Catching it here would swallow a
    cancellation and return a message for a turn that is being torn down.
    """
    from app.services.agentic_executions import (
        SOURCE_DEEP_INVESTIGATION,
        record_agentic_execution,
        refund_agentic_execution,
    )
    from app.services.investigation_packs import UnknownPackOptionError, UnknownSourcePackError
    from app.services.quota import (
        QuotaExceededError,
        check_agentic_quota,
        increment_agentic_usage,
        refund_agentic_usage,
    )

    # Refused before anything is metered or charged. These two need no
    # database, no model and no network, so there is nothing for the meter to
    # be honest about — see `validate_turn_request`.
    try:
        validate_turn_request(subject, source_pack_slugs)
    except (EmptySubjectError, EmptySelectionError, TooManyDomainsError) as exc:
        return str(exc)

    await record_agentic_execution(SOURCE_DEEP_INVESTIGATION)

    # The run id, taken from the first frame that carries one. The
    # orchestrator writes the skeleton and emits `planned` BEFORE it queries
    # anything, so by the time any failure can happen this is already set —
    # which is what lets the failure path below still point at the record.
    # Later frames without an id must not clobber it.
    run_id: str | None = None

    async def _capture(frame: dict[str, Any]) -> None:
        nonlocal run_id
        if run_id is None:
            run_id = frame.get("investigation_id")
        if on_event is not None:
            await on_event(frame)

    async def _give_back(*, usage: bool) -> None:
        """Refund the execution and, when ``usage``, the charge. Never raises:
        a refund that fails must not replace the turn's answer."""
        try:
            await refund_agentic_execution(SOURCE_DEEP_INVESTIGATION)
            if usage and user_id:
                await refund_agentic_usage(uuid.UUID(str(user_id)), db=db)
        except Exception as exc:
            logger.warning(
                "deep investigation refund failed: %s", format_exception_message(exc)
            )

    charged = False
    try:
        if user_id:
            await check_agentic_quota(db, user_id)
            await increment_agentic_usage(user_id, db=db)
            charged = True
        assistant_text, _result = await run_deep_investigation_chat(
            db,
            subject=subject,
            user_id=user_id,
            conversation_id=conversation_id,
            on_event=_capture,
            source_pack_slugs=source_pack_slugs,
            cancel_event=cancel_event,
            packs=packs,
            continues_investigation_id=continues_investigation_id,
            pack_options=pack_options,
            subject_type=subject_type,
        )
        if result_out is not None:
            result_out.append(_result)
        return assistant_text
    except QuotaExceededError as exc:
        # Metered before the gate, on purpose — the execution happened whether
        # or not it turned out to be chargeable — but a request refused BEFORE
        # any work is exactly the refund-safe condition
        # `refund_agentic_execution` documents. Without this, the execution
        # metric and the quota drift apart on precisely the installs that
        # enforce a quota, where every refusal would be reported as agentic
        # work performed. No source was queried and no run was written.
        await _give_back(usage=False)
        return str(exc)
    except (EmptySubjectError, EmptySelectionError, TooManyDomainsError) as exc:
        # Unreachable via this entry point — hoisted above the meter — but
        # kept: `run_deep_investigation_chat` owns these refusals and a future
        # caller reaching them from deeper in must still get a message rather
        # than a socket error.
        return str(exc)
    except UnknownSourcePackError:
        # Refused rather than silently substituted, so a run's persisted
        # ``source_pack_slugs`` can never name a pack that was not queried.
        #
        # NOT hoisted with the other two: resolving a pack is not a pure
        # check — it reads rows a caller may or may not see — so it cannot run
        # before the gate without moving that read there too. Refunded on both
        # counters instead, which is the same refund-safe condition both
        # primitives document: pack resolution is the orchestrator's first
        # act, so nothing was queried and no row was written.
        await _give_back(usage=True)
        return (
            "That source pack is no longer available. Pick another and try "
            "again."
        )
    except UnknownPackOptionError as exc:
        # Same refund-safe condition: options apply to the resolved packs,
        # before the run is written. The reason goes to the logs only.
        logger.info("deep investigation refused a pack option: %s", exc)
        await _give_back(usage=True)
        return "That source pack option is no longer available. Pick another and try again."
    except Exception as exc:
        logger.exception(
            "deep investigation turn failed: %s", format_exception_message(exc)
        )
        # ...and the record it DID write stays reachable. By the time anything
        # can fail the orchestrator has committed the skeleton and every step
        # it got through, so returning the sentence alone tells the user the
        # run "could not be completed" while a complete account of what was
        # attempted, and how far it got, exists and cannot be opened from the
        # conversation.
        #
        # That is exactly backwards: the failure case is when an analyst most
        # needs to see which sources were queried. The block's whole premise
        # is that it renders from the record alone — this is the path
        # where that matters. Nothing to point at only when it failed before
        # the skeleton existed, and then no marker is the honest answer.
        failed = "The investigation could not be completed."
        if run_id:
            from app.services.investigation_events import (
                build_investigation_marker,
            )

            failed += f"\n\n{build_investigation_marker(run_id)}"
        else:
            # Failed before the run's skeleton existed, so nothing was
            # queried: the same refund-safe condition as a refused pack.
            await _give_back(usage=charged)
        return failed


async def _write_report(
    db: AsyncSession,
    result,
    *,
    subject: str,
    subject_type: str,
    user_id,
) -> str | None:
    """Synthesise one cited report over the run, persist it, and render it.

    Returns None when there is no report to publish — the run retained nothing
    a report could stand on, no model was available, or the draft kept citing
    excerpts that were never supplied. The caller then sends the derived
    summary sentence, which is true, rather than prose whose sources cannot be
    checked.

    Every failure here is swallowed on purpose. The sources were queried and
    their results are already committed; losing that because the optional
    synthesis on top of it raised would be the wrong trade by a wide margin.
    """
    from app.services.investigation_report import generate_report, render_markdown

    try:
        draft = await generate_report(
            db,
            subject=subject,
            subject_type=subject_type,
            steps=result.steps,
            user_id=user_id,
        )
    except Exception as exc:
        logger.warning(
            "investigation report failed for %s: %s",
            result.investigation.id,
            format_exception_message(exc),
        )
        return None

    if draft is None:
        return None

    # Written in a session of its own, not the caller's.
    #
    # `db.commit()` is not scoped to these four columns — it flushes every
    # dirty object in the session, so an optional synthesis on top of the run
    # decided the transaction boundary for whatever else the chat turn had
    # pending. The `rollback` on failure was worse: it discarded that unrelated
    # work too, to protect a report nobody had asked to be atomic.
    #
    # A short-lived session touching one row by id cannot do either. It also
    # makes the failure genuinely isolated, which is what the docstring above
    # already claimed.
    investigation = result.investigation
    stored = await _persist_report(investigation.id, draft)
    if stored:
        # Marked as ALREADY committed, not assigned. A plain assignment makes
        # the caller's persistent object dirty, so the chat turn's next commit
        # flushes a second UPDATE of values the short-lived session has
        # already written — handing back exactly the coupling that session was
        # introduced to remove. The in-memory object still has to agree with
        # the row, because the caller reads it; `set_committed_value` gives it
        # that without enlisting it in the caller's transaction.
        from sqlalchemy.orm.attributes import set_committed_value

        set_committed_value(investigation, "report", draft.text)
        set_committed_value(
            investigation, "report_sources", [s.as_json() for s in draft.cited_sources]
        )
        set_committed_value(investigation, "report_claims", draft.claim_sentences)
        set_committed_value(
            investigation, "report_uncited_claims", draft.uncited_sentences
        )

    # The artifact, after the report is on the run: filing is additive, and a
    # coordination write must not decide whether the reader gets the report.
    await _file_report_in_workspace(db, investigation, draft)

    return render_markdown(
        draft,
        steps=result.steps,
        leads_dropped=investigation.leads_dropped or 0,
        stored=stored,
    )


async def _persist_report(investigation_id, draft) -> bool:
    """Store the report on its own row, in its own session. Never raises.

    Returns whether it landed, because the reader is told: a report that exists
    only in the transcript disappears on reload — the block renders from the
    record — and discovering that by refreshing is the worst way to learn it.
    """
    from sqlalchemy import update

    from app.core.database import async_session
    from app.models.investigation import Investigation

    try:
        async with async_session() as session:
            await session.execute(
                update(Investigation)
                .where(Investigation.id == investigation_id)
                .values(
                    report=draft.text,
                    report_sources=[s.as_json() for s in draft.cited_sources],
                    report_claims=draft.claim_sentences,
                    report_uncited_claims=draft.uncited_sentences,
                )
            )
            await session.commit()
        return True
    except Exception as exc:
        logger.warning(
            "could not persist the report for %s: %s",
            investigation_id,
            format_exception_message(exc),
        )
        return False


async def _file_report_in_workspace(db: AsyncSession, investigation, draft) -> bool:
    """Write the report into the conversation's workspace, if it has one.

    The report must be available to the requesting agent and to any flows
    linked to that agent. On this platform that means not an entity tree but
    a NAMED SCOPE — so the artifact is a
    workspace-scoped shared-state entry, which is exactly what another agent
    in that workspace already knows how to read.

    **An unfiled conversation writes nothing, deliberately.** There
    is no default workspace binding anywhere: a workspace exists
    when someone decided it should. Inventing one here to have somewhere to
    put the artifact would create a workspace nobody asked for, on the first
    investigation anyone runs.

    Failure is swallowed and reported by the return value. The report is
    already persisted on the run and already in the reply; losing either
    because a coordination write failed would be a bad trade.
    """
    from app.services.investigation_report import artifact_key, artifact_value
    from app.services.shared_state import upsert_state

    conversation_id = getattr(investigation, "conversation_id", None)
    if conversation_id is None:
        # No conversation means no workspace to file into — an assignment run,
        # or an A2A task. The report still lives on the investigation.
        return False

    from app.models.chat import Conversation

    conversation = await db.get(Conversation, conversation_id)
    workspace_id = getattr(conversation, "workspace_id", None) if conversation else None
    if workspace_id is None:
        return False

    try:
        await upsert_state(
            user_id=str(investigation.user_id) if investigation.user_id else None,
            key=artifact_key(workspace_id, investigation.id),
            label=f"Investigation — {investigation.subject_value}"[:200],
            value=artifact_value(investigation, draft),
            # Replaced rather than merged: the key is per investigation, so
            # anything already under it is a re-run of the same one, and half
            # of an old report merged into a new one is a document nobody
            # wrote.
            replace_value=True,
            workspace_id=workspace_id,
        )
    except Exception as exc:
        logger.warning(
            "could not file the report for %s in workspace %s: %s",
            investigation.id,
            workspace_id,
            format_exception_message(exc),
        )
        return False
    return True
