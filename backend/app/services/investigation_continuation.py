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

"""Continuing an investigation past its spent budget.

A run that stops on ``run.budget_spent`` has left work on the table: sources
its plan named and its allowance never reached. This module is what lets an
analyst look at exactly that remainder, see what it cost so far, and choose
which of it to spend more on.

Three things decide the shape, and all three come from code that already
exists rather than from taste.

**A continuation is a NEW run.** ``skipped`` is in
``TERMINAL_STEP_STATUSES``, which is what enforces "a failure never becomes a
success"; letting ``skipped -> active`` happen would punch a hole in the one
invariant stopping a step that never ran from later rendering as one that
succeeded. The orchestrator is documented single-use for the same family of
reason. And the consequence a reader would actually feel is citation
numbering: a report's source key is numbered per run, so mutating a run whose
report someone has already read renumbers the sources under them. A linked
child run keeps every report internally coherent and still lets the chain be
summed.

**The cap that binds is cumulative, not per-run.** A budget you can always
click past is not a budget — it is a prompt. So ``max_iterations`` becomes the
*checkpoint interval* (how often we come back and ask) and
``CHAIN_MAX_ITERATIONS`` becomes the limit (what the chain may spend in
total). Without that inversion, "continue" is an unbounded spend with consent
collected one palier at a time, which is the shape of every runaway meter.

**Re-authorisation is delegated, never re-implemented.** A stored
``source_ref`` is a pointer, not a grant. Rather than re-checking visibility
here — and drifting from the picker the first time a condition is added —
:func:`resolve_selected` re-resolves the caller's packs through
``investigation_pack_resolver.resolve_packs`` and accepts a ref only if its
identity is among what that caller may query *right now*. A knowledge base
they lost access to, an MCP server since disabled, a tool whose designation
was withdrawn: all refused by construction, because the single source of
truth for "what may this caller ask?" is the one the picker uses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investigation import (
    TERMINAL_INVESTIGATION_STATUSES,
    Investigation,
    InvestigationStep,
)
from app.models.user import User
from app.services.investigation_packs import SourceDefinition, SourcePack

#: Statuses meaning "this step never ran".
#:
#: ``pending`` is a step the loop never reached at all; ``skipped`` is one it
#: reached and refused, carrying the run's own reason code. Both are offerable.
#: Everything else has a result — and a step that FAILED is deliberately not
#: here: retrying a timeout is a different question from continuing a plan,
#: it needs its own error taxonomy to decide what is even retryable, and
#: quietly folding it in would let "continue" re-run an auth failure forever.
UNQUERIED_STEP_STATUSES: tuple[str, ...] = ("pending", "skipped")

#: Run states with nothing more to come. Only a finished run has a remainder
#: worth offering — one still ``running`` may yet query the very steps we
#: would be inviting a second spend on.
#:
#: There is deliberately no companion list of *end reason codes*. An earlier
#: version gated on ``run.budget_spent`` / ``run.cancelled`` /
#: ``run.interrupted``, and that gate was redundant the moment the offer
#: started gathering across the whole chain: a run that queried everything has
#: no unqueried steps to gather, and one whose packs covered nothing planned
#: none. Two overlapping gates would only drift — and the drift would show up
#: as a remainder that exists in the rows and not in the block.
#: Aliased rather than re-listed: this is the same set `finish_run` accepts
#: and the orchestrator writes, and two copies of it would drift the day a
#: state is added — as a remainder that exists in the rows and not in the
#: block, which is the failure this module exists to prevent.
FINISHED_RUN_STATUSES: tuple[str, ...] = TERMINAL_INVESTIGATION_STATUSES

#: What a whole chain may spend, in source queries, across every continuation.
#:
#: This is the number that actually limits the feature. It is deliberately not
#: a multiple of the per-run default: a checkpoint every ten queries with a
#: forty-query ceiling means an analyst is asked three times before the answer
#: becomes "no", which is enough to make each answer considered rather than
#: reflexive.
CHAIN_MAX_ITERATIONS = 40

#: How many continuations deep a chain may go, independently of spend.
#:
#: Belt beside the braces: a chain of one-query continuations would stay under
#: the spend cap for ever while still turning the block into an infinite
#: prompt, and each link also costs a report generation the cap does not count.
MAX_CHAIN_DEPTH = 4


@dataclass(frozen=True)
class ContinuableStep:
    """One source the parent run planned and never queried."""

    step_id: uuid.UUID
    source_name: str
    source_pack_slug: str | None
    position: int
    source: SourceDefinition


@dataclass(frozen=True)
class Coverage:
    """What the parent run reached, and what it did not — derived, not judged.

    Every field here is counted from the step record. Nothing in this class
    involves a model call, which is the point: a hallucinated "not worth
    continuing" would close an investigation nobody decided to close, and the
    same reasoning already made ``classify_subject`` a regex rather than a
    prompt.
    """

    queried: int = 0
    unqueried: int = 0
    findings: int = 0
    #: Sources that answered "nothing found" — a neutral outcome, not a
    #: failure, and the one that most often makes continuing worthwhile.
    empty: int = 0
    failed: int = 0
    #: Packs from which at least one source was queried.
    packs_reached: tuple[str, ...] = ()
    #: Packs the plan named and the budget never touched at all. This is the
    #: structural gap worth surfacing: a verdict resting only on internal
    #: telemetry because no external source was reached is a different claim
    #: from one that consulted both.
    packs_untouched: tuple[str, ...] = ()

    @property
    def anything_found(self) -> bool:
        return self.findings > 0


@dataclass(frozen=True)
class ContinuationOffer:
    """The enriched block: what is left, what it cost, and what that implies."""

    investigation_id: uuid.UUID
    subject_value: str
    subject_type: str
    #: Queries this chain has already spent, summed over every run in it.
    spent: int
    #: Runs in this family, counting the original; 1 for a run never
    #: continued. Not the longest path: the cap exists to bound how many times
    #: an analyst is invited to spend again, and four continuations off one
    #: parent invite exactly as often as four in a line.
    chain_depth: int
    #: What the chain may still spend. 0 means the offer is closed.
    remaining_allowance: int
    #: Human label per pack slug, for every pack this caller can resolve.
    #:
    #: On the payload rather than mapped client-side, and that is a lesson
    #: rather than a preference: the first version kept a slug -> label table
    #: in the panel and it drifted immediately — `web-research` rendered as
    #: its raw slug beside a properly labelled pack, in the very first
    #: screenshot. The server already knows every label, so a second copy can
    #: only ever be the stale one.
    pack_labels: dict[str, str] = field(default_factory=dict)
    steps: tuple[ContinuableStep, ...] = ()
    #: Steps that cannot be offered, with why. A step written before
    #: ``source_ref`` existed, or one whose source the caller may no longer
    #: query. Named rather than dropped: a remainder that silently shrinks is
    #: the coverage gap this whole design exists to prevent.
    withheld: tuple[tuple[str, str], ...] = ()
    coverage: Coverage = field(default_factory=Coverage)
    #: A run of this chain is still planning or running: its pending steps
    #: are not a remainder yet, and offering them would pay for them twice.
    in_flight: bool = False

    @property
    def available(self) -> bool:
        """Whether anything can actually be continued right now."""
        return bool(self.steps) and self.remaining_allowance > 0 and not self.in_flight

    @property
    def blocked_reason(self) -> str | None:
        """Why the offer is closed, when it is — for the block's footer."""
        if self.in_flight:
            return "in_flight"
        if self.chain_depth > MAX_CHAIN_DEPTH:
            return "chain_depth"
        if self.remaining_allowance <= 0:
            return "chain_budget"
        if not self.steps:
            return "nothing_left" if not self.withheld else "nothing_addressable"
        return None


def _is_unqueried(step: InvestigationStep) -> bool:
    return step.status in UNQUERIED_STEP_STATUSES


def analyse_coverage(steps: list[InvestigationStep]) -> Coverage:
    """Count what the run reached, by outcome and by pack.

    ``packs_untouched`` is the value this function exists for. Everything else
    is a tally an endpoint could have computed inline; a pack from which
    nothing at all was queried is the one fact that changes what the existing
    findings are *worth*, and it is only visible by comparing two sets.
    """
    queried = unqueried = findings = empty = failed = 0
    reached: set[str] = set()
    named: set[str] = set()
    for step in steps:
        slug = step.source_pack_slug or ""
        if slug:
            named.add(slug)
        if _is_unqueried(step):
            unqueried += 1
            continue
        queried += 1
        if slug:
            reached.add(slug)
        findings += step.findings_count or 0
        if step.status == "empty":
            empty += 1
        elif step.status in ("error", "degraded"):
            failed += 1
    return Coverage(
        queried=queried,
        unqueried=unqueried,
        findings=findings,
        empty=empty,
        failed=failed,
        packs_reached=tuple(sorted(reached)),
        packs_untouched=tuple(sorted(named - reached)),
    )


async def chain_of(
    db: AsyncSession, investigation: Investigation
) -> list[Investigation]:
    """Every run in this family, oldest first — ancestors AND descendants.

    Both directions, and the second one is not symmetry for its own sake. An
    earlier version walked parents only, and the consequence was the feature's
    central promise failing in production:

    * a parent's block reported the spend of its OWN run, so a chain that had
      spent 43 of a 40-query ceiling still read "2 of 40 used";
    * and the sources its children had already queried stayed on offer, so
      each click paid for them again. Four continuations off one parent, each
      spending from the same untouched remainder.

    A budget you can only click past is not a budget, and walking one
    direction is exactly how a cumulative cap becomes decorative.

    So: climb to the root, then collect the whole tree beneath it. Visited
    sets in both passes — the FK cannot express "no cycles" and a hand-written
    row could close one, and an infinite walk inside a request is a hung
    worker rather than an error anybody sees.
    """
    root = investigation
    climbed: set[uuid.UUID] = {investigation.id}
    while root.continues_investigation_id is not None:
        parent_id = root.continues_investigation_id
        if parent_id in climbed:
            break
        parent = (
            await db.execute(
                select(Investigation).where(Investigation.id == parent_id)
            )
        ).scalar_one_or_none()
        if parent is None:
            # Deleted parent: the FK set the link NULL. The family we can see
            # starts here — and the spend we can prove starts with it, which
            # is the conservative direction (we under-charge rather than
            # refuse on evidence we no longer hold).
            break
        climbed.add(parent_id)
        root = parent

    family: dict[uuid.UUID, Investigation] = {root.id: root}
    frontier = [root.id]
    while frontier:
        children = list(
            (
                await db.execute(
                    select(Investigation).where(
                        Investigation.continues_investigation_id.in_(frontier)
                    )
                )
            )
            .scalars()
            .all()
        )
        frontier = []
        for child in children:
            if child.id in family:
                continue
            family[child.id] = child
            frontier.append(child.id)

    # The offer walks this oldest-first so a source is attributed to the step
    # that first planned it — the one whose position the analyst saw.
    return sorted(family.values(), key=lambda inv: (inv.created_at, inv.id))


async def _steps_by_run(
    db: AsyncSession, investigation_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[InvestigationStep]]:
    """Every step of these runs, grouped, in display order.

    One query for the whole chain rather than one per run: a chain is short,
    but this is called on every render of the block and an N+1 over runs would
    grow with exactly the thing the depth cap already permits.

    Spend is then counted from the steps that ran, NOT from
    ``max_iterations``: the latter is the allowance a run was *granted*, and
    charging a chain for an allowance it did not use would close the offer
    early on precisely the runs that were frugal.
    """
    if not investigation_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(InvestigationStep)
                .where(InvestigationStep.investigation_id.in_(investigation_ids))
                .order_by(
                    InvestigationStep.investigation_id, InvestigationStep.position
                )
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[InvestigationStep]] = {}
    for row in rows:
        grouped.setdefault(row.investigation_id, []).append(row)
    return grouped


async def build_offer(
    db: AsyncSession, investigation: Investigation, user: User | None
) -> ContinuationOffer:
    """The continuation offer for a finished run, addressable or not.

    Always returns an offer rather than ``None``. A closed offer carries WHY
    it is closed (``blocked_reason``) and the coverage that was reached, and
    the block needs both: "the budget stopped here and nothing more may be
    spent" is information an analyst acts on, while a missing block reads as
    "the run simply finished".
    """
    chain = await chain_of(db, investigation)
    chain_ids = [inv.id for inv in chain]
    by_run = await _steps_by_run(db, chain_ids)

    # Coverage describes THIS run — it is what the block renders beside this
    # run's own findings, and merging the chain's tallies would tell an analyst
    # their current step list reached sources it never touched.
    coverage = analyse_coverage(by_run.get(investigation.id, []))

    spent = sum(
        1
        for steps in by_run.values()
        for step in steps
        if step.status not in UNQUERIED_STEP_STATUSES
    )
    depth = len(chain)
    remaining = max(0, CHAIN_MAX_ITERATIONS - spent)
    if depth > MAX_CHAIN_DEPTH:
        remaining = 0

    offerable: list[ContinuableStep] = []
    withheld: list[tuple[str, str]] = []
    allowed: set[tuple] | None = None
    pack_labels: dict[str, str] = {}

    if investigation.status in FINISHED_RUN_STATUSES:
        allowed, pack_labels = await _addressable_identities(db, user)
        # The remainder belongs to the CHAIN, not to the latest run. Continuing
        # with two of seven must not retire the other five: they were planned,
        # never queried, and an analyst who picks a couple then finds the rest
        # gone has been shown a shrinking remainder — the exact coverage gap
        # this feature exists to close.
        #
        # Walked oldest run first so a source is offered under the step that
        # first planned it, which is the one whose position the analyst saw.
        seen: set[tuple] = set()
        queried: set[tuple] = set()
        candidates: list[tuple[InvestigationStep, SourceDefinition]] = []
        for run_id in chain_ids:
            for step in by_run.get(run_id, []):
                if not step.source_ref:
                    if _is_unqueried(step):
                        # Written before this column existed. Named, not hidden.
                        withheld.append((step.source_name, "no_address"))
                    continue
                try:
                    source = SourceDefinition.from_ref(
                        step.source_ref,
                        subject_types=(investigation.subject_type,),
                    )
                except ValueError:
                    if _is_unqueried(step):
                        withheld.append((step.source_name, "unreadable_address"))
                    continue
                identity = source.identity()
                if not _is_unqueried(step):
                    # Queried somewhere in the chain — by this run or by a
                    # sibling continuation. Either way it must not be offered
                    # again, or the same source gets paid for twice.
                    queried.add(identity)
                    continue
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append((step, source))

        for step, source in candidates:
            if source.identity() in queried:
                continue
            if (
                allowed is not None
                and source.access_identity() not in allowed
                and not _is_followable_lead(step, source)
            ):
                # Visible when the plan was written, not now: a corpus whose
                # sharing changed, a server disabled, a designation withdrawn.
                withheld.append((step.source_name, "no_longer_permitted"))
                continue
            offerable.append(
                ContinuableStep(
                    step_id=step.id,
                    source_name=step.source_name,
                    source_pack_slug=step.source_pack_slug,
                    position=step.position,
                    source=source,
                )
            )

    in_flight = any(inv.status not in FINISHED_RUN_STATUSES for inv in chain)
    return ContinuationOffer(
        investigation_id=investigation.id,
        subject_value=investigation.subject_value,
        subject_type=investigation.subject_type,
        spent=spent,
        chain_depth=depth,
        remaining_allowance=remaining,
        pack_labels=pack_labels,
        steps=() if in_flight else tuple(offerable),
        withheld=tuple(withheld),
        coverage=coverage,
        in_flight=in_flight,
    )


def _is_followable_lead(step: InvestigationStep, source: SourceDefinition) -> bool:
    """Whether a step is a lead that pack membership cannot speak for.

    A lead is discovered mid-run from a page another source cited, so it is a
    member of no pack and its identity is in no resolved set. Gating it on
    pack membership therefore withholds every lead as `no_longer_permitted` —
    "you no longer have access to it", about a source nobody ever had
    pack-level access to. That is the second false explanation this remainder
    was given, and storing the address alone does not remove it.

    The gate is narrow on purpose, in both directions:

    * Only a step DISCOVERED as a lead qualifies (`lead_from_step_id`), so a
      planned step in a pack the caller lost access to is still withheld.
    * Only an ``http`` ref qualifies. That is the whole of what a lead can be
      today — `_pages_worth_reading` is the only producer and it emits a URL —
      and it is the only kind carrying no per-user privilege for the gate to
      re-check. A knowledge base has three-tier visibility and an MCP server
      has to be enabled and designated; a public URL has neither, and is
      bounded instead by the SSRF policy every fetch already runs. So a lead
      that somehow arrived as a KB or MCP ref keeps the gate.

    What authorises it is not "nothing": the run that discovered it was
    permitted to query the source that cited it, and the address still has to
    survive `validate_outbound_url` at query time.
    """
    return step.lead_from_step_id is not None and source.kind == "http"


async def _addressable_identities(
    db: AsyncSession, user: User | None
) -> tuple[set[tuple] | None, dict[str, str]]:
    """Identities this caller may query right now, or ``None`` for no user.

    Delegated to ``resolve_packs`` rather than re-derived. That function is
    what the picker offers from, so it already encodes every condition — a
    knowledge base's three-tier visibility, an MCP server being enabled AND
    carrying a designated investigation tool. Re-checking those here would
    duplicate them, and the duplicate would drift the first time one changed.

    ``None`` (no user on the row) means no re-authorisation is possible, so
    the caller treats every stored ref as addressable. That is the same
    posture ``resolve_packs`` already takes for a userless run: it offers the
    built-ins, which is what such a run planned from in the first place.
    """
    if user is None:
        return None, {}
    from app.services.investigation_pack_resolver import resolve_packs

    packs = await resolve_packs(db, user)
    # Access identities: a hop to another subject is permitted where its
    # pack's source is — visibility belongs to the place asked, not the value.
    identities = {
        source.access_identity() for pack in packs.values() for source in pack.sources
    }
    # Labels come from the same resolution, so a pack can never be offered
    # under one name and labelled under another.
    return identities, {slug: pack.label for slug, pack in packs.items()}


#: Slug of the pack a continuation builds on the fly.
#:
#: A real slug rather than a reused one so the child run's
#: ``source_pack_slugs`` says what it actually was — a hand-picked remainder,
#: not a fresh selection of "knowledge-bases". Reading a child row and seeing
#: the parent's pack slugs would misreport how those sources were chosen.
CONTINUATION_PACK_SLUG = "continuation"


def pack_from_steps(
    steps: tuple[ContinuableStep, ...], *, subject_type: str, allowance: int
) -> SourcePack:
    """A synthetic pack holding exactly the sources an analyst ticked.

    ``max_iterations`` is the allowance, not the source count: the chain cap
    may permit fewer queries than were selected, and the orchestrator's own
    budget machinery is then what stops it — leaving the unreached ones
    ``skipped`` for ``run.budget_spent``, which makes the child continuable in
    turn. That is the same mechanism as the parent's, deliberately: one way
    for a run to stop short, one way to see what is left.
    """
    return SourcePack(
        slug=CONTINUATION_PACK_SLUG,
        label="Continued sources",
        description=(
            "Sources a previous run planned and its budget did not reach, "
            "picked by hand."
        ),
        subject_types=(subject_type,),
        sources=tuple(s.source for s in steps),
        max_iterations=allowance,
        auto_select=False,
    )


async def resolve_selected(
    db: AsyncSession,
    investigation: Investigation,
    user: User | None,
    step_ids: list[uuid.UUID],
) -> tuple[SourcePack, ContinuationOffer, list[uuid.UUID]]:
    """Turn a selection of the parent's unqueried steps into a runnable pack.

    Returns ``(pack, offer, rejected_step_ids)``. Rejection is reported rather
    than raised for a selection that is merely stale — a step someone else's
    continuation already picked up, or one withheld between rendering the
    block and clicking it — because the honest answer is "these N ran, that
    one did not, here is why" and not a failed request that ran nothing.

    Raises ``ValueError`` only when nothing at all can run: an empty
    selection, or a chain with no allowance left. Those are refusals, not
    partial successes.
    """
    # Serialised per chain, check-and-create: two requests on one remainder
    # both passed the check before either run existed. Transaction-scoped, so
    # it is held until the orchestrator commits the new run, which the next
    # request's offer then sees in flight.
    root = (await chain_of(db, investigation))[0]
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"investigation-chain:{root.id}"},
    )
    offer = await build_offer(db, investigation, user)
    if offer.in_flight or offer.remaining_allowance <= 0:
        raise ValueError(offer.blocked_reason or "chain_budget")

    by_id = {s.step_id: s for s in offer.steps}
    wanted = list(dict.fromkeys(step_ids))  # de-duplicated, order kept
    chosen = [by_id[sid] for sid in wanted if sid in by_id]
    rejected = [sid for sid in wanted if sid not in by_id]
    if not chosen:
        raise ValueError("nothing_selected")

    # The selection is capped by what the chain may still spend. Truncated in
    # the analyst's own order rather than re-sorted: they ticked these, and a
    # cap that silently re-prioritised would spend on a source they de-ranked.
    allowance = min(len(chosen), offer.remaining_allowance)
    pack = pack_from_steps(
        tuple(chosen), subject_type=investigation.subject_type, allowance=allowance
    )
    return pack, offer, rejected
