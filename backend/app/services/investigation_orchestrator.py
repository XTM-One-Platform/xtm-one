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

"""The Deep Investigation bounded loop (state contract).

Drives one investigation: resolve the subject, plan the sources from the packs,
query them within an iteration budget, and close the run.  Every state
transition goes through ``investigation_state``, so the persisted rows — and
therefore the live block and the report's ``## Sources queried`` section — are
written by this loop and never inferred from model output.

Why the querier is injected
---------------------------
``run`` takes a ``SourceQuerier`` callable rather than reaching for
``web_fetch`` itself.  Two reasons, both load-bearing:

* **The loop is testable without a network.**  The budget, the skip path, the
  circular-lead guard and the error taxonomy are the parts most likely to be
  wrong, and they are exactly the parts a live HTTP dependency makes untestable.
* **The full feature swaps the querier, not the loop.**  Integration-backed
  packs (VirusTotal, OpenCTI, Recorded Future) are different queriers over the
  same loop; ``make_web_fetch_querier`` is simply the first one.

Which queriers produce leads
----------------------------
The loop implements multi-hop: a source outcome may carry ``leads``, which are
appended as steps, deduplicated against everything already queried, and run
until the budget is spent.

A lead is another SOURCE for the run's subject — unless it names a subject of
its own (``SourceDefinition.subject``), which is how a goal plan hops: the
passive-DNS querier answers a domain with its IP's sources and an IP with its
sites' fingerprints, all inside one run. A hop's step queries its own subject;
every other step the run's. A pack with a goal plan runs its actions in the
plan's order, so a hop found early is queried before a later action's source.

``make_web_search_querier`` produces them: the provider searched and
SUMMARISED, so the pages it cited were never read by this platform, and
fetching the most-cited ones gives the run their own text to quote instead of
a paraphrase of it.  ``make_web_fetch_querier`` still produces none — deciding
that a page mentions another indicator worth chasing needs a judgement this
layer does not make, and a regex over free text would chase every version
number that looks like a CVE.

What this slice deliberately does not produce
---------------------------------------------
**No confidence values.**  A generic page fetch gives no basis to compute one,
so every step in this slice stores ``confidence = None`` and the block shows
none.  That is the "no unearned metrics" invariant doing its job on day
one rather than being retrofitted: the honest output of a run that cannot
measure confidence is a run with no confidence, not a run with a default.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.investigation import (
    EVIDENCE_KINDS,
    TERMINAL_INVESTIGATION_STATUSES,
    Investigation,
    InvestigationStep,
)
from app.services import investigation_state as state
from app.services.investigation_events import (
    complete_frame,
    planned_frame,
    step_frame,
)
from app.services.investigation_packs import (
    SourceDefinition,
    SourcePack,
    classify_subject,
    domain_list,
    goal_plan_to_json,
    list_summary,
    plan_for,
    recommend_packs,
    with_seeds,
)

logger = logging.getLogger(__name__)

#: ``(frame) -> None``. Same contract as the agent loop's ``OnEventCallback``,
#: so a chat service can hand the orchestrator the callback it already holds.
OnEventCallback = Callable[[dict[str, Any]], Awaitable[None]]

#: Hard ceiling on steps appended as leads, independent of the iteration
#: budget. The budget bounds the WORK; this bounds the PLAN, so a source that
#: reports a hundred leads cannot make the block unreadable even though only
#: the first few would ever be queried.
MAX_LEAD_STEPS = 50


@dataclass(frozen=True)
class SourceOutcome:
    """What querying one source produced.

    Mirrors ``investigation_state.record_step_result``'s inputs on purpose:
    a querier reports facts (how many findings, what evidence, did it fail,
    was it partial) and never a step status. The status is derived downstream,
    which is what keeps a failed source from ever being stored as a success.
    """

    findings_count: int = 0
    evidence: tuple[Evidence, ...] = ()
    #: The prose this source produced, when it produced any. A provider web
    #: search writes an answer and attaches its sources to spans of it; that
    #: text is what the citations index into, so it is stored with them or
    #: they index into nothing.
    answer: str | None = None
    #: ``None`` unless the querier can genuinely derive one — see the module
    #: docstring. There is no default.
    confidence: float | None = None
    #: Rate-limited, truncated or auth-limited: answered, but not fully.
    partial: bool = False
    #: One of ``investigation_state``'s error types, or None on success.
    error_type: str | None = None
    detail: str | None = None
    #: Localizable form of ``detail``. Absent where the message comes from a
    #: third-party exception and cannot be translated.
    detail_code: str | None = None
    detail_params: dict | None = None
    #: Further sources this outcome suggests chasing (multi-hop).
    leads: tuple[SourceDefinition, ...] = field(default_factory=tuple)


#: ``(source, subject_value, query) -> outcome``. The *query* is the string
#: the step stores and shows, built once by ``SourceDefinition.describe`` and
#: passed in so the querier and the step's stored ``query`` cannot diverge. For
#: an HTTP source it is the URL; for a knowledge base or an MCP tool it is what
#: was asked of them — "what did this source get asked?" is the first question
#: an analyst has, and it must not be blank for two kinds out of three.
SourceQuerier = Callable[[SourceDefinition, str, str], Awaitable[SourceOutcome]]


@dataclass(frozen=True)
class Evidence:
    """One citation a step produced.

    ``label`` is always present because it is what the block renders; ``href``
    is set only when there is genuinely somewhere to go. A knowledge-base
    document and an MCP tool result have no address, and giving them one would
    render a citation as a link that leads nowhere — the same overstatement the
    step vocabulary exists to prevent.
    """

    kind: str
    label: str
    href: str | None = None
    #: The passage this citation rests on, verbatim. Retained because a reader
    #: asked to trust a claim needs to see the sentence it came from, and
    #: because a report that must cite cannot be written from counts.
    #:
    #: This reverses an earlier decision in this module — outcomes deliberately
    #: dropped every payload, on the reasoning that prose belongs to the report
    #: and a step is not a log. That was right while there was no report. It is
    #: what made one impossible: nothing was kept for it to cite.
    quote: str | None = None
    #: Half-open range into the step's ``answer``, when the source said which
    #: part of its own prose this citation supports. ``None`` means "this
    #: source was consulted" rather than "this source backs this sentence",
    #: and the two must not be rendered the same way.
    start: int | None = None
    end: int | None = None

    def as_json(self) -> dict:
        if self.kind not in EVIDENCE_KINDS:
            raise ValueError(f"unknown evidence kind {self.kind!r}; expected one of {EVIDENCE_KINDS}")
        data: dict = {"kind": self.kind, "label": self.label, "href": self.href}
        if self.quote:
            data["quote"] = self.quote
        # Both or neither: half a range cannot place anything, and storing one
        # would invite a reader of this JSON to default the other to zero.
        if self.start is not None and self.end is not None:
            data["start"], data["end"] = self.start, self.end
        return data

    @property
    def anchored(self) -> bool:
        """Whether this citation names a span of the answer."""
        return self.start is not None and self.end is not None


@dataclass
class InvestigationResult:
    """The run, its steps in display order, and the summary of both."""

    investigation: Investigation
    steps: list[InvestigationStep]
    summary: state.RunSummary


class DeepInvestigationOrchestrator:
    """Owns one investigation run's state from creation to close.

    The instance is single-use: ``run`` creates the record, so re-running would
    silently start a second investigation rather than resume the first.
    """

    def __init__(
        self,
        db: AsyncSession,
        querier: SourceQuerier,
        *,
        on_event: OnEventCallback | None = None,
        commit_each_step: bool = True,
    ) -> None:
        self.db = db
        self.querier = querier
        self.on_event = on_event
        # The live block's whole premise is that state is readable WHILE the
        # run happens, which a single commit at the end would defeat: a crash
        # mid-run would lose every step, and a concurrent reader would see
        # nothing until the loop finished. Callers that own a larger
        # transaction can opt out.
        self.commit_each_step = commit_each_step
        #: The run in flight, if any — see `_finalize`.
        self._run_id: uuid.UUID | None = None

    async def _checkpoint(self) -> None:
        if self.commit_each_step:
            await self.db.commit()

    async def _emit(self, frame: dict[str, Any]) -> None:
        """Send a frame, swallowing any delivery failure.

        Frames are advisory: the state they describe is already committed, and
        a client that missed one rebuilds the block from the record. So a
        dropped socket must not abort an investigation that is otherwise
        working — it is logged (with the status, so a delivery problem stays
        diagnosable) and the run continues.
        """
        if self.on_event is None:
            return
        try:
            await self.on_event(frame)
        except Exception:
            logger.warning(
                "investigation on_event failed (status=%s, step=%s, position=%s)"
                " — the client may miss this progress signal; the persisted "
                "record is unaffected",
                frame.get("status"),
                frame.get("step_id") or frame.get("id"),
                frame.get("position"),
                exc_info=True,
            )

    async def _checkpoint_and_emit(self, frame: dict[str, Any]) -> None:
        """Commit, THEN emit — never the other way round.

        A frame emitted before its commit could reach a client that then reads
        the record and sees older state, which is the one way the "frames are
        advisory, the record is authoritative" contract can be broken.
        """
        await self._checkpoint()
        await self._emit(frame)

    async def run(
        self,
        *,
        subject_value: str,
        subject_type: str | None = None,
        source_pack_slugs: Sequence[str] | None = None,
        cancel_event: asyncio.Event | None = None,
        user_id=None,
        conversation_id=None,
        assignment_run_id=None,
        max_iterations: int | None = None,
        packs: Sequence[SourcePack] | None = None,
        continues_investigation_id=None,
        on_created: Callable[[uuid.UUID], Awaitable[None]] | None = None,
        pack_options: Mapping[str, Mapping[str, str]] | None = None,
    ) -> InvestigationResult:
        """Run one investigation, and guarantee it reaches a terminal state.

        ``pack_options`` are the choices made in the picker, by pack slug
        (`SourcePack.with_options`); a choice the pack does not offer raises
        `UnknownPackOptionError` before the run is written.

        ``on_created`` is awaited with the run's id as soon as its row exists,
        before any step is visible: what a caller announces about the run (a
        report on its way) is then true from the run's first read.

        The loop is wrapped because *not* reaching one is not merely an untidy
        row. The block polls on `status` and stops only when it is terminal, so
        a run stranded in ``running`` makes every future open of that
        conversation issue a request every few seconds, per mounted block,
        forever. One crashed run becomes permanent background load.

        Per-source failures are already contained (`_query` turns them into an
        ``error`` step and the loop continues). What this catches is everything
        OUTSIDE that: a failing checkpoint commit, a worker shutdown, the socket
        teardown that cancels the turn. ``BaseException`` rather than
        ``Exception`` precisely so `CancelledError` — the likeliest of the
        three — is included; it is re-raised either way, so the caller still
        unwinds normally and a cancelled turn stays cancelled.

        A cancellation closes the run as ``cancelled`` and anything else as
        ``aborted``, matching the vocabulary the graceful `cancel_event` path
        already uses.
        """
        self._run_id = None
        try:
            return await self._run(
                subject_value=subject_value,
                subject_type=subject_type,
                source_pack_slugs=source_pack_slugs,
                cancel_event=cancel_event,
                user_id=user_id,
                conversation_id=conversation_id,
                assignment_run_id=assignment_run_id,
                max_iterations=max_iterations,
                packs=packs,
                continues_investigation_id=continues_investigation_id,
                on_created=on_created,
                pack_options=pack_options,
            )
        except asyncio.CancelledError:
            # SHIELDED, because this handler runs for a cancellation already
            # delivered and `_finalize` awaits four times. A second delivery —
            # a worker shutting down, a socket teardown that cancels the turn
            # twice, `Task.cancel()` called more than once — would otherwise
            # interrupt the close partway, and the run stays `running`: the
            # bug this wrapper exists to end, reappearing on its likeliest
            # path. `_finalize` swallowing the error is not enough; the close
            # has to actually finish.
            await self._finalize_shielded("cancelled", "the run was cancelled")
            raise
        except BaseException as exc:
            await self._finalize_shielded(
                "aborted",
                f"the run stopped on an unrecoverable error: "
                f"{format_exception_message(exc)}",
            )
            raise

    async def _finalize_shielded(self, status: str, reason: str) -> None:
        """Run the close, and do not return until it has actually finished.

        `shield` alone does NOT achieve that, which is the trap: it protects
        the inner coroutine from the cancellation, but the OUTER await raises
        immediately, the suppression swallows it, and `_finalize` is left
        running as a detached task nobody awaits. Two things then go wrong on
        the exact path this exists to protect:

        * The caller unwinds and disposes its `AsyncSession` while the orphan
          is still mid-await on it, so `finish_run` / `commit` hit a closed
          session, get swallowed and logged, and the run stays `running` —
          the original bug, on its likeliest path.
        * An `AsyncSession` is not safe for concurrent use, and the orphan now
          shares one with the caller's own teardown: interleaved I/O on a
          single connection, which surfaces as an `InterfaceError` rather than
          a clean failure.

        So the task is held and re-awaited until it is genuinely done. Each
        iteration makes progress because the task keeps running however often
        the outer await is cancelled, so this terminates. `_finalize` swallows
        and logs everything internally, so nothing here can fail upward or
        replace the exception the caller is unwinding with.
        """
        task = asyncio.ensure_future(self._finalize(status, reason))
        while not task.done():
            try:
                await asyncio.shield(task)
            except BaseException:  # noqa: S112 — see the docstring
                # Deliberately silent: the only thing that reaches here is the
                # cancellation being re-delivered to the outer await, which is
                # the condition this loop exists to ride out. `_finalize` logs
                # its own failures.
                continue

    async def _finalize(self, status: str, reason: str) -> None:
        """Best-effort close of a run that did not finish on its own terms.

        Best-effort, and deliberately so: this runs on a path where something
        has ALREADY gone wrong, quite possibly the database itself. Failing
        here must not replace the original exception — which is the one that
        explains what happened — so everything is swallowed and logged.

        The rollback first is what makes the write possible at all: whatever
        raised may have left the session with a half-applied flush, and the step
        history is not at risk because every step was committed as it completed.
        The row is re-read rather than reused for the same reason, and a run
        already terminal is left alone.

        A caller that opted out of per-step commits owns the surrounding
        transaction; unwinding it is theirs to do, and a commit here would
        destroy work they have not finished.
        """
        if self._run_id is None or not self.commit_each_step:
            return
        try:
            await self.db.rollback()
            investigation = await self.db.get(Investigation, self._run_id)
            if investigation is None:
                return  # never committed; there is nothing to strand
            if investigation.status in TERMINAL_INVESTIGATION_STATUSES:
                return
            await state.finish_run(
                self.db,
                investigation,
                status=status,
                end_reason=reason,
                end_reason_code="run.interrupted",
                end_reason_params={},
            )
            await self.db.commit()
        except BaseException:
            # `BaseException`, not `Exception`: a `CancelledError` delivered
            # here is precisely what used to escape, replace the original
            # exception and strand the run. This method is only ever called
            # from a handler that re-raises, so swallowing everything loses
            # nothing and is what makes the close unable to fail upward.
            logger.exception(
                "could not close investigation %s as %s; it may stay running",
                self._run_id,
                status,
            )

    async def _run(
        self,
        *,
        subject_value: str,
        subject_type: str | None = None,
        source_pack_slugs: Sequence[str] | None = None,
        cancel_event: asyncio.Event | None = None,
        user_id=None,
        conversation_id=None,
        assignment_run_id=None,
        max_iterations: int | None = None,
        packs: Sequence[SourcePack] | None = None,
        continues_investigation_id=None,
        on_created: Callable[[uuid.UUID], Awaitable[None]] | None = None,
        pack_options: Mapping[str, Mapping[str, str]] | None = None,
    ) -> InvestigationResult:
        """Plan, execute and close one investigation.

        ``subject_type`` may be supplied by a caller that classified the
        subject some other way (an LLM pass for an actor name); otherwise it is
        derived by shape. ``source_pack_slugs`` likewise overrides the
        recommendation — that is the picker's confirmed selection.

        ``cancel_event`` is checked between sources, which is the only place a
        run can stop cleanly: a source fetch is a single awaited HTTP call, so
        interrupting mid-fetch would leave the step ``active`` with no result.
        The run then closes as ``cancelled``, its unreached steps ``skipped``,
        and the block stops polling — a Stop that left the row ``running``
        would poll for ever.
        """
        resolved_type = subject_type or classify_subject(subject_value)
        # A pasted list of domains: the seeds ride in the list sources, and the
        # run is stored under a summary of them, which a subject column holds.
        seeds = domain_list(subject_value) if resolved_type == "domain_list" else None
        if seeds:
            subject_value = list_summary(seeds)
        # A caller may hand the packs over already built. That is how a
        # continuation runs: its sources are a hand-picked remainder of a
        # previous plan, not a slug the registry could resolve.
        #
        # That used to rest on the caller having re-authorised them, which was
        # true of the only caller and is not something a comment can enforce.
        # `run(packs=...)` is a public entry point accepting a fully-formed
        # source list, so the next caller — a flow action, a scheduled re-run —
        # would inherit "query any corpus in the database" without ever reading
        # this paragraph. It is checked here now.
        if packs is None:
            packs = await self._resolve_packs(resolved_type, source_pack_slugs, user_id)
        elif user_id is not None:
            await self._assert_caller_may_query(packs, user_id)
        # Every option at its default unless chosen: a built pack's step that
        # is off by default is off for a run that made no choice.
        chosen = pack_options or {}
        packs = [pack.with_options(chosen.get(pack.slug) or {}, resolved_type) for pack in packs]
        packs = [with_seeds(pack, seeds) for pack in packs] if seeds else list(packs)
        # `is None`, not `or`: an explicit 0 is a caller asking for no
        # queries at all (a plan-only run, or a budget derived from an
        # allowance that has reached zero), and `or` silently replaced it with
        # the pack's ceiling of ten.
        #
        # Summed, not `max`: a pack's ceiling is the allowance IT asks for, and
        # selecting two packs asks for both. Under `max` the picker's own
        # feature broke the moment a second pack was available — tick
        # "Knowledge bases" beside "General web" and the run plans both, spends
        # the single largest allowance on whichever comes first, and marks
        # every remaining corpus step `skipped` for a spent budget, without
        # anyone having asked for a smaller run. Summing keeps a lone pack
        # exactly as it was, including one that deliberately declares fewer
        # iterations than it has sources.
        budget = (
            max_iterations
            if max_iterations is not None
            else sum(p.max_iterations for p in packs)
        )

        # A built pack's version and plan, recorded: the pack lives in the
        # database and may change or go after the run (`run_goal_plan`).
        versions = {p.slug: p.version for p in packs if p.version is not None}
        first_planned = next((p for p in packs if p.goal_for(resolved_type) is not None), None)
        declared_plan = (
            goal_plan_to_json(first_planned.slug, first_planned.goal_for(resolved_type))
            if first_planned is not None and first_planned.version is not None
            else None
        )
        investigation = await state.create_investigation(
            self.db,
            subject_type=resolved_type,
            subject_value=subject_value,
            source_pack_versions=versions,
            declared_plan=declared_plan,
            source_pack_slugs=[p.slug for p in packs],
            user_id=user_id,
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
            max_iterations=budget,
            continues_investigation_id=continues_investigation_id,
        )

        # Remembered as an id rather than as the ORM object: `_finalize`
        # runs after a failure that may have expired or detached it.
        self._run_id = investigation.id
        if on_created is not None:
            await on_created(investigation.id)

        # `run` wraps everything from here on, so a failure anywhere —
        # including in the planning statements below, where `start_run`
        # has already flipped the status — still closes the run.
        planned, executable = plan_for(packs, resolved_type, investigation.subject_value)
        steps = await state.plan_steps(self.db, investigation, planned)
        await state.start_run(self.db, investigation)
        # Checkpoint before the first query: this is what makes the
        # skeleton visible to the block before any work has happened.
        await self._checkpoint_and_emit(planned_frame(investigation, steps))

        # (pack_slug, source, step) triples, consumed in order. Leads append.
        queue: list[tuple[str, SourceDefinition, InvestigationStep]] = [
            (pack_slug, source, step) for (pack_slug, source), step in zip(executable, steps, strict=True)
        ]
        # Circular-lead guard, keyed on the URL a step will fetch rather than
        # on the source name: two packs can name one source differently, and a
        # lead pointing back at a URL already in the queue is the loop's most
        # likely way to spin.
        #
        # Deduplicating at ENQUEUE time (rather than when a step comes up for
        # execution) is what keeps duplicates out of the block entirely: a lead
        # that merely got skipped later would still have rendered as a second
        # step for the same source. Seeded with the planned URLs so a lead
        # pointing at an already-planned source is dropped too. One dedupe
        # point is sufficient because a source's identity is deterministic —
        # the same source can never produce two of them.
        #
        # Keyed on `identity()` rather than on the built URL: a
        # knowledge-base or MCP source has no URL, and `build_url` now
        # refuses to invent one for them. Identity is kind-aware, so two
        # packs naming the same KB collapse and an HTTP source is still
        # deduplicated by template plus transform exactly as before.
        enqueued: set[tuple] = {source.identity() for _pack_slug, source in executable}
        # A pack following a goal plan runs its actions in the plan's order:
        # a lead found by an early action (the IP a domain resolved to) is
        # queried before a later action's planned source (the doctrine), as
        # the plan says. Without a plan every source ranks the same and the
        # queue is first-in, first-out, as it always was.
        plans = {p.slug: p.goal_for(resolved_type) for p in packs if p.goal_for(resolved_type) is not None}
        # Under a plan, packs stay grouped in the order they were selected and
        # a source whose action the plan does not know runs after the plan's
        # own work; a run with no plan keeps its first-in, first-out queue.
        pack_order = {p.slug: index for index, p in enumerate(packs)}

        def _rank(position: int) -> tuple[int, int, int]:
            if not plans:
                return (0, 0, position)
            pack_slug, source, _step = queue[position]
            plan = plans.get(pack_slug)
            if plan is None:
                return (pack_order.get(pack_slug, len(pack_order)), 0, position)
            rank = plan.rank(source.action)
            return (pack_order.get(pack_slug, len(pack_order)), len(plan.actions) if rank < 0 else rank, position)

        all_steps: list[InvestigationStep] = list(steps)
        lead_steps = 0
        # Leads that would have become steps but hit the plan cap. Counted
        # rather than persisted: the cap exists to bound the PLAN, so
        # recording them as steps would defeat it — but dropping them with
        # no trace at all left the run reporting "all planned sources
        # queried" while it had found and discarded leads, which is the
        # silent coverage gap this whole design exists to prevent.
        leads_dropped = 0
        iterations = 0
        reason_code = "run.all_sources_queried"
        reason_params: dict = {}
        if executable:
            end_reason = "all planned sources queried"
        else:
            # No source in the selected packs covers this subject type — a real
            # state, not a bug: `web-general` has no keyless endpoint for file
            # hashes, for instance. Saying so beats both a silent zero-step
            # "completed" run and querying sources that cannot answer.
            end_reason = f"no source in the selected packs covers subject type {resolved_type!r}"
            reason_code = "run.no_covering_source"
            reason_params = {"subject_type": resolved_type}

        # Positions of the queue entries not reached yet, picked by `_rank`.
        remaining: list[int] = list(range(len(queue)))
        run_status = "completed"
        while remaining:
            index = min(remaining, key=_rank)
            pack_slug, source, step = queue[index]

            # Checked BEFORE the entry leaves `remaining`, so a step we refuse
            # to run stays among the unreached ones below rather than being
            # silently dropped between the two.
            # Cancellation is checked in the same place as the budget and for
            # the same reason: a source fetch is one awaited call, so stopping
            # mid-fetch would leave the step `active` with no result.
            if cancel_event is not None and cancel_event.is_set():
                run_status = "cancelled"
                end_reason = "cancelled by the user"
                reason_code = "run.cancelled"
                reason_params = {}
                break
            if iterations >= budget:
                end_reason = f"iteration budget spent ({budget})"
                reason_code = "run.budget_spent"
                reason_params = {"budget": budget}
                break
            remaining.remove(index)

            # A hop queries its own subject (the domain's IP, the IP's sites);
            # every other source the run's.
            subject = source.subject_for(investigation.subject_value)
            query = source.describe(subject)
            iterations += 1
            await state.mark_step_active(self.db, investigation, step, query=query, iteration=iterations)
            await self._checkpoint_and_emit(step_frame(investigation, step))

            outcome = await self._query(source, subject, query)

            await state.record_step_result(
                self.db,
                investigation,
                step,
                findings_count=outcome.findings_count,
                evidence=[e.as_json() for e in outcome.evidence],
                answer=outcome.answer,
                confidence=outcome.confidence,
                partial=outcome.partial,
                error_type=outcome.error_type,
                detail=outcome.detail,
                detail_code=outcome.detail_code,
                detail_params=outcome.detail_params,
            )
            await self._checkpoint_and_emit(step_frame(investigation, step))

            for lead in outcome.leads:
                # A lead serves the action that found it unless it says
                # otherwise, and a hop back to the run's own subject is the
                # run's source, so it de-duplicates against it.
                if lead.action is None and source.action is not None:
                    lead = dataclasses.replace(lead, action=source.action)
                if lead.subject is not None and lead.subject == investigation.subject_value:
                    lead = dataclasses.replace(lead, subject=None)
                if not lead.can_query(lead.subject_for(investigation.subject_value)):
                    continue
                lead_url = lead.identity()
                if lead_url in enqueued:
                    # Already covered by a planned step or an earlier lead:
                    # redundant, not dropped, so it is not counted.
                    continue
                # Recorded BEFORE the cap check, so dropped-ness is per
                # DISTINCT lead. Adding it only below the cap meant two
                # sources returning the same new URL past the cap each
                # incremented the counter, and the footer then reported more
                # leads lost than exist — in the one sentence whose whole
                # purpose is to be an honest statement of coverage.
                enqueued.add(lead_url)
                if lead_steps >= MAX_LEAD_STEPS:
                    # `continue`, not `break`: the count has to be complete
                    # to be worth reporting.
                    leads_dropped += 1
                    # Written to the row as it happens, not held in a local
                    # until the close. Everything else about this run is
                    # readable WHILE it runs, and a count that exists only at
                    # the end is lost by every path that does not reach the
                    # normal close — the finalizer included, which is exactly
                    # when a reader most needs to know coverage was partial.
                    investigation.leads_dropped = leads_dropped
                    continue
                # `iteration` here is when the lead was DISCOVERED; it is
                # overwritten with the query iteration by `mark_step_active`
                # if the lead is reached. A lead the budget never reaches
                # therefore keeps saying when it came up, which is what the
                # report's "leads not followed" section needs.
                lead_step = await state.append_lead_step(
                    self.db,
                    investigation,
                    source_name=lead.name,
                    source_pack_slug=pack_slug,
                    lead_from_step_id=step.id,
                    iteration=iterations,
                    # The lead is as continuable as a planned step, and this
                    # loop is already holding the `SourceDefinition`.
                    source_ref=lead.to_ref(),
                    # Passed rather than looked up: this loop appends every
                    # step of the run, so it already knows the next position,
                    # and asking the database for it cost one round-trip per
                    # lead — up to `MAX_LEAD_STEPS` of them, mid-loop, for a
                    # number sitting in local state.
                    position=len(all_steps),
                )
                queue.append((pack_slug, lead, lead_step))
                remaining.append(len(queue) - 1)
                all_steps.append(lead_step)
                lead_steps += 1
                # One frame per lead rather than a batch: a lead is an addition
                # to the plan the analyst is already watching, and the block
                # appends it at `position` on arrival.
                await self._checkpoint_and_emit(step_frame(investigation, lead_step))

        # Whatever the loop did not reach is reported as skipped, with the
        # reason: an unqueried source must not be left looking pending forever,
        # and it must not be coerced into a terminal success either.
        for _pack_slug, _source, step in (queue[i] for i in sorted(remaining)):
            if not state.is_terminal(step):
                await state.mark_step_skipped(
                    self.db,
                    investigation,
                    step,
                    reason=end_reason,
                    detail_code=reason_code,
                    detail_params=reason_params,
                )
                await self._checkpoint_and_emit(step_frame(investigation, step))

        # `leads_dropped` is a column now, so the count stops being
        # concatenated into the sentence: the block renders it as a number
        # instead of parsing it back out of prose.
        await state.finish_run(
            self.db,
            investigation,
            status=run_status,
            end_reason=end_reason,
            end_reason_code=reason_code,
            end_reason_params=reason_params,
            leads_dropped=leads_dropped,
        )
        summary = state.summarize(all_steps)
        await self._checkpoint_and_emit(complete_frame(investigation, summary))

        return InvestigationResult(
            investigation=investigation,
            steps=all_steps,
            summary=summary,
        )

    async def _resolve_packs(
        self, subject_type: str, slugs: Sequence[str] | None, user_id
    ) -> list[SourcePack]:
        """The packs this run will use.

        Async because the offer is no longer code alone: a knowledge base and
        a designated MCP tool are rows, visible to some callers and not
        others, so resolving a slug needs the session and the caller.

        A recommendation still needs neither. `recommend_packs` only ever
        returns auto-selecting packs, and the install-specific ones never are
        — searching every corpus the caller can see is a decision somebody
        makes, not one a subject regex makes for them.
        """
        from app.services import investigation_packs as packs_module
        from app.services.investigation_pack_resolver import resolve_selected

        if not slugs:
            recommended = recommend_packs(subject_type)
            if not any(p.knowledge_bases for p in recommended):
                return recommended
            # A recommended pack that declares corpora is resolved for the
            # caller like a picked one: which corpora answer depends on who
            # asks.
            from app.services.investigation_pack_resolver import (
                expand_declared_knowledge_bases,
            )

            user = await self._load_user(user_id)
            return await expand_declared_knowledge_bases(self.db, user, recommended)

        # Explicit selection is honoured as given, including the order:
        # `plan_for` deduplicates a shared source under the FIRST pack that
        # carries it, so the caller's order decides attribution.
        #
        # A selection made entirely of built-in packs is resolved without a
        # session and without loading the caller. Not an optimisation for its
        # own sake: this is the chat path, which has no picker and always
        # selects the recommendation, and it stayed database-free before the
        # install-specific packs existed. Sound because the dynamic packs
        # cannot change what a built-in slug resolves to — their slugs are
        # distinct constants, and `resolve_packs` layers them on top of the
        # built-ins rather than replacing any.
        builtins = packs_module.BUILTIN_PACKS
        if all(s in builtins and not builtins[s].knowledge_bases for s in slugs):
            return [builtins[s] for s in slugs]

        user = await self._load_user(user_id)
        return await resolve_selected(self.db, user, list(slugs))

    async def _assert_caller_may_query(self, packs, user_id) -> None:
        """Refuse caller-supplied sources this user may not reach.

        Delegated to the picker's own resolver, so there is one visibility
        policy rather than a second one drifting beside it.

        Only PRIVILEGED kinds are checked. A knowledge base has three-tier
        visibility and an MCP server has to be enabled and carry a designated
        tool — those are the ones where "query any of them in the database"
        would be a real escalation, and they are what this guard is for. An
        ``http`` source carries no per-user access at all; it is bounded by the
        SSRF policy every fetch runs, and it is also what a followed lead is,
        so gating it here would refuse the continuations this entry point
        exists to serve (see `_is_followable_lead`).
        """
        from app.services.investigation_continuation import _addressable_identities

        user = await self._load_user(user_id)
        if user is None:
            return
        allowed, _labels = await _addressable_identities(self.db, user)
        if allowed is None:
            return
        refused = sorted(
            {
                source.name
                for pack in packs
                for source in pack.sources
                if source.kind != "http" and source.access_identity() not in allowed
            }
        )
        if refused:
            raise ValueError(
                "sources not permitted for this caller: " + ", ".join(refused)
            )

    async def _load_user(self, user_id):
        """The caller, for visibility. None when the run has no user.

        A run with no user gets the built-in packs only — see `resolve_packs`.
        """
        import uuid as _uuid

        from app.models.user import User

        if user_id is None:
            return None
        try:
            key = user_id if isinstance(user_id, _uuid.UUID) else _uuid.UUID(str(user_id))
        except (ValueError, AttributeError, TypeError):
            return None
        return await self.db.get(User, key)

    async def _query(self, source: SourceDefinition, subject_value: str, url: str) -> SourceOutcome:
        """Call the querier, converting an unexpected raise into an error step.

        A querier that raises is a bug in that querier, not a reason to abort
        the run: the remaining sources are still worth querying, and the step
        the analyst sees says the source failed rather than vanishing.
        """
        try:
            return await self.querier(source, subject_value, url)
        except Exception as exc:  # see docstring: one bad querier != a dead run
            logger.warning(
                "investigation querier raised for source %s: %s",
                source.name,
                format_exception_message(exc),
            )
            return SourceOutcome(
                error_type="invalid_response",
                detail=f"querier raised: {format_exception_message(exc)}"[:300],
                detail_code="source.querier_error",
            )
