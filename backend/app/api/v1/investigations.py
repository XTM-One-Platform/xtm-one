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

"""Investigation API — read access to Deep Investigation runs.

Runs are written exclusively by ``DeepInvestigationOrchestrator``; there is
deliberately no create/update endpoint, for the same reason as reports and
decks: the state machine in ``investigation_state`` is what guarantees a failed
source can never be stored as a success, and a client-writable surface would be
a way around it.

This endpoint is what makes the live block robust rather than merely pretty.
The #3302 requirement is that the block renders identically from the record
alone — page reload mid-run, late join, stream unavailable — so the frames
are an optimisation and *this* is the source of truth.

Access is owner-or-admin, mirroring ``GET /reports/{id}``: an investigation
names the subject an analyst was looking into, which is as sensitive as the
conversation that produced it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_agentic_ai
from app.api.v1.helpers import parse_path_id, parse_uuid
from app.api.v1.schemas.investigation import (
    ContinuationCoverage,
    ContinuationOfferResponse,
    ContinuationRequest,
    ContinuationStartedResponse,
    ContinuationStepOption,
    InvestigationResponse,
    PackChoiceModel,
    PackOptionModel,
    SourcePackOption,
    SubjectPlanResponse,
)
from app.core.database import get_db
from app.models.investigation import (
    SUCCESS_STEP_STATUSES,
    Investigation,
    InvestigationStep,
)
from app.models.user import User
from app.services.deep_investigation_chat import run_deep_investigation_turn
from app.services.investigation_continuation import (
    CHAIN_MAX_ITERATIONS,
    build_offer,
    resolve_selected,
)
from app.services.investigation_state import RunSummary
from app.services.investigations import serialize_investigation

logger = logging.getLogger(__name__)
router = APIRouter()

#: Cap on steps returned for one run. Well above `MAX_LEAD_STEPS` + any
#: realistic pack, so it only ever fires on a pathological row set — but it
#: fires rather than letting one run's payload grow without bound.
MAX_STEPS = 500


def _assert_readable(investigation: Investigation, user: User) -> None:
    """Owner-or-admin gate. A run the caller may not read is answered like an
    unknown id, so the answer never confirms that someone else's run exists.
    A run whose owner was deleted stays readable to admins, for auditing."""
    if user.is_admin or (investigation.user_id is not None and investigation.user_id == user.id):
        return
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")


async def _run_summaries(db: AsyncSession, run_ids: list) -> dict:
    """The true summary of each run, aggregated over EVERY step.

    The serializer derives its summary from the steps it was handed, and the
    read endpoints hand it at most `MAX_STEPS`. Gating `all_succeeded` on
    truncation stopped the green banner, but `by_status`, `failed`,
    `unfinished` and `confidence` still described the sample: a run whose only
    `error` sits past the cap reported `failed: 0`, and `by_status` summed to
    500 while `total` said 600.

    `failed: 0` is a claim, and this module's rule is that a truncated view
    makes none. Rather than blanking the fields, the counts are taken from the
    database over all steps, so every number stays true and truncation goes
    back to being what it should be — a display concern about which steps came
    back, not about what the run contained.

    One `GROUP BY` for the whole page, not one per run.
    """
    rows = (
        await db.execute(
            select(
                InvestigationStep.investigation_id,
                InvestigationStep.status,
                func.count().label("n"),
                func.sum(InvestigationStep.confidence).label("conf_sum"),
                func.count(InvestigationStep.confidence).label("conf_n"),
            )
            .where(InvestigationStep.investigation_id.in_(run_ids))
            .group_by(InvestigationStep.investigation_id, InvestigationStep.status)
        )
    ).all()

    by_status: dict = {}
    conf: dict = {}
    for run_id, status_name, n, conf_sum, conf_n in rows:
        by_status.setdefault(run_id, {})[status_name] = n
        total_sum, total_n = conf.get(run_id, (0.0, 0))
        conf[run_id] = (total_sum + float(conf_sum or 0), total_n + (conf_n or 0))

    summaries: dict = {}
    for run_id in run_ids:
        counts = by_status.get(run_id, {})
        conf_sum, conf_n = conf.get(run_id, (0.0, 0))
        summaries[run_id] = RunSummary(
            total=sum(counts.values()),
            by_status=counts,
            all_succeeded=bool(counts)
            and set(counts) <= set(SUCCESS_STEP_STATUSES),
            confidence=(conf_sum / conf_n) if conf_n else None,
            confidence_samples=conf_n,
        )
    return summaries


def _pack_origin(pack, user: User) -> str:
    """Whose ``pack`` is, for the picker: from the pack's row, never its text."""
    owner = pack.owner
    if owner is None:
        return "builtin"
    if owner.company_managed:
        return "company"
    return "mine" if owner.user_id is not None and owner.user_id == str(user.id) else "shared"


# Declared BEFORE ``/{investigation_id}``: FastAPI matches in declaration
# order, so a literal path registered after a greedy ``{param}`` on the same
# prefix would be swallowed by it and answered as an invalid-UUID error.
@router.get("/source-packs", response_model=SubjectPlanResponse)
async def plan_source_packs(
    subject: str = Query(min_length=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Classify a subject and describe the packs that could investigate it.

    Read-only, and no longer database-free: the built-in packs are code, but a
    knowledge base and a designated MCP tool are rows with per-user
    visibility, so the offer is resolved against this caller. Two people
    asking about the same subject can legitimately be offered different packs.
    """
    from app.services.deep_investigation_chat import (
        EmptySubjectError,
        TooManyDomainsError,
        validate_turn_request,
    )
    from app.services.investigation_pack_resolver import resolve_packs
    from app.services.investigation_packs import classify_subject, domain_list, list_summary, recommend_packs

    # `min_length=1` counts characters, not content, so "%20" arrives as a
    # non-empty query and strips to nothing. Without this the picker offers a
    # full plan, `has_applicable_source: true` and all, for a subject
    # `run_deep_investigation_chat` then refuses with `EmptySubjectError` —
    # the endpoint and the turn disagreeing about the same string.
    # The same cleaning as the turn, so a pasted list of domains is offered
    # the plan the turn will run rather than one for its first 500 characters.
    try:
        cleaned = validate_turn_request(subject, None)
    except EmptySubjectError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="subject is required",
        ) from None
    except TooManyDomainsError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    subject_type = classify_subject(cleaned)
    recommended = {pack.slug for pack in recommend_packs(subject_type)}
    available = await resolve_packs(db, user)

    options: list[SourcePackOption] = []
    origins = {slug: _pack_origin(pack, user) for slug, pack in available.items()}
    # A built pack marked recommended is pre-ticked for its types when it is
    # the caller's or the company's. A colleague's is offered, never ticked:
    # one click would send the subject where that colleague chose.
    recommended |= {
        pack.slug
        for pack in available.values()
        if pack.recommended and subject_type in pack.subject_types and origins[pack.slug] in ("mine", "company")
    }
    for pack in available.values():
        applicable = pack.sources_for(subject_type)
        origin = origins[pack.slug]
        options.append(
            SourcePackOption(
                slug=pack.slug,
                label=pack.label,
                description=pack.description,
                subject_types=list(pack.subject_types),
                recommended=pack.slug in recommended,
                applicable_source_count=len(applicable),
                total_source_count=len(pack.sources),
                applicable_source_names=[source.name for source in applicable],
                max_iterations=pack.max_iterations,
                options=[
                    PackOptionModel(
                        key=o.key,
                        label=o.label,
                        description=o.description,
                        default=o.default,
                        choices=[PackChoiceModel(value=c.value, label=c.label, description=c.description) for c in o.choices],
                    )
                    for o in pack.options_for(subject_type)
                ],
                origin=origin,
                owner_name=pack.owner.name if origin == "shared" else None,
                destinations=pack.destinations_for(subject_type, cleaned),
            )
        )
    # Recommended first, then the widest match — the picker renders the order
    # it is given rather than re-sorting, so the server owns the ranking.
    options.sort(key=lambda o: (not o.recommended, -o.applicable_source_count, o.slug))
    seeds = domain_list(cleaned) if subject_type == "domain_list" else None
    return SubjectPlanResponse(
        subject=cleaned,
        subject_type=subject_type,
        subject_summary=list_summary(seeds) if seeds else cleaned,
        packs=options,
        has_applicable_source=any(o.applicable_source_count for o in options),
    )


@router.get("/{investigation_id}", response_model=InvestigationResponse)
async def get_investigation(
    investigation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return one run with its steps in display order."""
    # `parse_path_id`, not `parse_uuid`: this route answers an unknown id with
    # a 404, so a malformed one must get the same answer rather than a 422 —
    # a run id replayed from a stale link names no row either way, and the
    # difference would disclose which of the two it was.
    run_id = parse_path_id(investigation_id, not_found_detail="Investigation not found")
    result = await db.execute(select(Investigation).where(Investigation.id == run_id))
    investigation = result.scalar_one_or_none()
    if investigation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")
    _assert_readable(investigation, user)

    steps_result = await db.execute(
        select(InvestigationStep)
        .where(InvestigationStep.investigation_id == run_id)
        .order_by(InvestigationStep.position)
        .limit(MAX_STEPS)
    )
    steps = list(steps_result.scalars().all())
    # Counted separately, because the summary is derived from the rows in hand
    # and `MAX_STEPS` may have withheld some. Without the real total a
    # truncated read reports `all_succeeded` for a run whose failure sits past
    # the cap — the one aggregate this feature is built to keep honest.
    summary = (await _run_summaries(db, [run_id]))[run_id]
    from app.services.investigation_opencti import opencti_record_request
    from app.services.investigation_pack_resolver import visible_opencti_integration
    from app.services.investigation_report_status import report_status, reports_being_written

    opencti = await visible_opencti_integration(db, user)
    writing = await reports_being_written([investigation.id])
    record = None
    if opencti is not None and investigation.report:
        record = {
            "integration_id": opencti[0],
            "prompt": opencti_record_request(investigation.subject_value, str(investigation.id)),
        }
    return InvestigationResponse(
        **serialize_investigation(
            investigation,
            steps,
            total_steps=summary.total,
            summary=summary,
            page_limit=MAX_STEPS,
        ),
        opencti_available=opencti is not None,
        opencti_record=record,
        report_status=report_status(investigation, writing),
    )




@router.get("", response_model=list[InvestigationResponse])
async def list_investigations(
    conversation_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the caller's runs, most recent first.

    Returns the full shape rather than a trimmed summary: a run's steps are a
    bounded handful, and the conversation view needs the same payload it would
    fetch per block anyway — a trimmed list would only move the same rows into
    one request per mounted block. Each run's steps are capped at `MAX_STEPS`,
    as they are when fetched one at a time.
    """
    query = select(Investigation).where(Investigation.user_id == user.id)
    if conversation_id:
        query = query.where(Investigation.conversation_id == parse_uuid(conversation_id, label="conversation ID"))
    result = await db.execute(query.order_by(Investigation.created_at.desc()).limit(limit))
    runs = list(result.scalars().all())
    if not runs:
        return []

    # Capped PER RUN, at the same `MAX_STEPS` the single-run endpoint applies.
    # Without this the list is strictly weaker than the detail view it feeds:
    # one `IN (...)` over up to `limit` runs, loading every step row for all of
    # them, with the only bound being how many rows happen to exist. The
    # docstring's "a bounded handful" is true of a healthy run — `MAX_STEPS`
    # exists precisely because that assumption can fail, and it should not fail
    # differently depending on which endpoint asked.
    #
    # A window function rather than a total cap so no run is starved by the
    # ones ordered before it: each gets its first `MAX_STEPS` by position,
    # which is the same slice, and the same order, the detail endpoint returns.
    ranked = (
        select(
            InvestigationStep.id.label("step_id"),
            InvestigationStep.investigation_id.label("run_id"),
            func.row_number()
            .over(
                partition_by=InvestigationStep.investigation_id,
                order_by=InvestigationStep.position,
            )
            .label("rank"),
        )
        .where(InvestigationStep.investigation_id.in_([r.id for r in runs]))
        .subquery()
    )
    steps_result = await db.execute(
        select(InvestigationStep)
        .join(ranked, ranked.c.step_id == InvestigationStep.id)
        .where(ranked.c.rank <= MAX_STEPS)
        .order_by(InvestigationStep.investigation_id, InvestigationStep.position)
    )
    by_run: dict = {}
    for step in steps_result.scalars().all():
        by_run.setdefault(step.investigation_id, []).append(step)
    # One GROUP BY for the page: every count reflects the run, not the slice
    # of it that came back under `MAX_STEPS`.
    summaries = await _run_summaries(db, [r.id for r in runs])
    from app.services.investigation_report_status import report_status, reports_being_written

    # One MGET for the page, and only for the runs a report may still reach.
    writing = await reports_being_written([r.id for r in runs if not r.report])
    return [
        InvestigationResponse(
            **serialize_investigation(
                run,
                by_run.get(run.id, []),
                total_steps=summaries[run.id].total,
                page_limit=MAX_STEPS,
                summary=summaries[run.id],
            ),
            report_status=report_status(run, writing),
        )
        for run in runs
    ]


async def _load_readable(db: AsyncSession, investigation_id: str, user: User) -> Investigation:
    # Same rule as `get_investigation`: this answers an unknown id with a 404,
    # so a malformed one gets the same answer rather than a 422.
    run_id = parse_path_id(investigation_id, not_found_detail="Investigation not found")
    investigation = (
        await db.execute(select(Investigation).where(Investigation.id == run_id))
    ).scalar_one_or_none()
    if investigation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")
    _assert_readable(investigation, user)
    return investigation


def _serialize_offer(offer) -> ContinuationOfferResponse:
    return ContinuationOfferResponse(
        investigation_id=str(offer.investigation_id),
        subject_value=offer.subject_value,
        subject_type=offer.subject_type,
        available=offer.available,
        blocked_reason=offer.blocked_reason,
        spent=offer.spent,
        chain_depth=offer.chain_depth,
        remaining_allowance=offer.remaining_allowance,
        chain_max=CHAIN_MAX_ITERATIONS,
        pack_labels=dict(offer.pack_labels),
        steps=[
            ContinuationStepOption(
                step_id=str(s.step_id),
                source_name=s.source_name,
                source_pack_slug=s.source_pack_slug,
                position=s.position,
                kind=s.source.kind,
            )
            for s in offer.steps
        ],
        withheld=[[name, reason] for name, reason in offer.withheld],
        coverage=ContinuationCoverage(
            queried=offer.coverage.queried,
            unqueried=offer.coverage.unqueried,
            findings=offer.coverage.findings,
            empty=offer.coverage.empty,
            failed=offer.coverage.failed,
            packs_reached=list(offer.coverage.packs_reached),
            packs_untouched=list(offer.coverage.packs_untouched),
        ),
    )


@router.get("/{investigation_id}/continuation", response_model=ContinuationOfferResponse)
async def get_continuation_offer(
    investigation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """What is left of this run's plan, what the chain has spent, and the gap.

    Always answers, even when nothing can be continued. A closed offer carries
    the reason and the coverage that WAS reached, and the block needs both:
    "the budget stopped here and nothing more may be spent" is something an
    analyst acts on, whereas an absent block reads as "the run just finished".
    """
    investigation = await _load_readable(db, investigation_id, user)
    offer = await build_offer(db, investigation, user)
    return _serialize_offer(offer)


@router.post(
    "/{investigation_id}/continuation",
    response_model=ContinuationStartedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_continuation(
    investigation_id: str,
    body: ContinuationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai),
):
    """Query the selected remainder as a NEW run linked to this one.

    The module docstring above says there is deliberately no create endpoint,
    and this does not contradict it: nothing here writes a step. It builds a
    pack out of sources the caller is re-authorised for and hands it to
    ``DeepInvestigationOrchestrator``, which stays the exclusive writer and
    keeps the state machine the only thing that decides a step's outcome.

    It goes through ``run_deep_investigation_turn`` rather than around it, for
    the reason that function documents: metering and the per-user agentic quota
    live there, and a caller assembling the run itself would spend
    investigations nobody counted. That already happened once.

    Runs in the request rather than on a worker. The block hydrates from the
    record and polls, and the orchestrator commits each step, so progress is
    visible while this call is still open — and the work is bounded by the
    chain allowance, which is at most a handful of sources. A queued job would
    buy asynchrony at the cost of a second execution path for the same run.
    """
    investigation = await _load_readable(db, investigation_id, user)

    try:
        pack, _offer, rejected = await resolve_selected(
            db,
            investigation,
            user,
            [parse_uuid(sid, label="step ID") for sid in body.step_ids],
        )
    except ValueError as exc:
        # Every one of these is a refusal with a machine-readable reason, not
        # an internal error: the chain is out of allowance, too deep, or the
        # selection matched nothing offerable.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None

    message = await run_deep_investigation_turn(
        db,
        subject=investigation.subject_value,
        # The parent's type, not the stored subject's: a list run is stored
        # under its summary, which would classify as `unknown`.
        subject_type=investigation.subject_type,
        user_id=user.id,
        conversation_id=investigation.conversation_id,
        packs=[pack],
        continues_investigation_id=investigation.id,
        result_out=(results := []),
    )
    if not results:
        # The metered wrapper refused before producing a run — quota, most
        # likely — and `message` already says which. A 409 rather than a 201
        # so a client cannot mistake the explanation for a started run.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)

    child = results[0].investigation
    return ContinuationStartedResponse(
        investigation_id=str(child.id),
        continues_investigation_id=str(investigation.id),
        queued=len(pack.sources),
        rejected_step_ids=[str(sid) for sid in rejected],
        message=message,
    )
