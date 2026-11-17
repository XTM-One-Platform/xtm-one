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

"""Ratings of agent work, whatever the work is.

``/api/v1/annotations/{target_type}/{target_id}`` reads, sets and retracts the
caller's own rating of one target - a chat message, an assignment run, a
background task, a delegation - under that target's own read rule
(``services.annotations.resolve_target``). ``GET /api/v1/annotations`` and
``/summary`` list and count every rating of one agent's work for the people who
may manage the agent, the same audience as the agent's feedback comments.

A ``message`` target is written through the chat's own path
(``chat.apply_message_feedback``), so rating a message here and from the chat
bubble is the same write: same row, same Langfuse and trace-store scores.

A rating may carry one score per enabled feedback criterion; the scores
are validated against their criteria before anything is written, and written in
the rating's own transaction.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, StrictFloat, StrictInt, StrictStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.chat import (
    apply_message_feedback,
    load_message_feedback,
    retract_message_feedback,
)
from app.api.v1.helpers import parse_path_id
from app.core.database import get_db
from app.models.agent import Agent
from app.models.user import User
from app.services import annotations as annotation_service
from app.services.annotations import (
    MAX_COMMENT_LENGTH,
    AnnotationRating,
    AnnotationTarget,
    AnnotationTargetNotFoundError,
    AnnotationTargetType,
    normalize_comment,
)
from app.services.feedback_criteria import (
    CriterionError,
    ValidatedScore,
    aggregate_weighted_score,
    load_criteria,
    load_scores,
    rating_weighted_score,
    score_value,
    validate_scores,
)

router = APIRouter()

#: One answer for a target that is unknown, unreadable or not an id at all.
TARGET_NOT_FOUND = "Target not found"

#: More scores than criteria could ever exist is a malformed request.
MAX_SCORES_PER_RATING = 50


class ScoreInput(BaseModel):
    """One criterion's score: an option (categorical) or a number (numeric, scale)."""

    criterion_id: uuid.UUID
    # Strict so a JSON true / false is refused instead of read as 1 / 0.
    value: StrictInt | StrictFloat | StrictStr


class AnnotationUpsert(BaseModel):
    rating: Literal["positive", "negative"]
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_LENGTH)
    #: The rating's per-criterion scores. Sent: they replace the ones it
    #: had (``[]`` clears them). Omitted: they stay as they were.
    scores: list[ScoreInput] | None = Field(default=None, max_length=MAX_SCORES_PER_RATING)


class ScoreResponse(BaseModel):
    criterion_id: str
    value: float | str


class AnnotationResponse(BaseModel):
    id: str
    target_type: AnnotationTargetType
    target_id: str
    rating: Literal["positive", "negative"]
    comment: str | None = None
    scores: list[ScoreResponse] = Field(default_factory=list)
    #: In [0, 1]: the criterion scores weighted, or the thumbs when there are none.
    weighted_score: float
    created_at: str
    updated_at: str


class AnnotationStatsResponse(BaseModel):
    target_type: AnnotationTargetType
    target_id: str
    positive: int
    negative: int
    total: int


class AgentAnnotationItem(BaseModel):
    id: str
    target_type: AnnotationTargetType
    target_id: str
    #: The conversation of a rated chat message (links it); ``None`` otherwise.
    conversation_id: str | None = None
    #: Whether the caller may open the rated work where it lives (its run page,
    #: the agent's chat history): a manager reads ratings of work they may not open.
    can_open: bool = False
    rating: Literal["positive", "negative"]
    comment: str | None = None
    weighted_score: float
    #: The XTM One user who rated; both ``None`` for a platform rater (a reaction).
    user_name: str | None = None
    user_email: str | None = None
    #: Where the rating was given (``web``, ``teams_agent_account``...) and the reaction that gave it.
    source: str = "web"
    emoji: str | None = None
    created_at: str
    updated_at: str


class AgentAnnotationListResponse(BaseModel):
    items: list[AgentAnnotationItem]
    total: int
    limit: int
    offset: int


class AnnotationTypeSummary(BaseModel):
    target_type: AnnotationTargetType
    positive: int
    negative: int
    total: int
    positive_ratio: float | None = None


class HistogramBucketResponse(BaseModel):
    key: str
    label: str
    count: int


class CriterionDayResponse(BaseModel):
    date: str
    count: int
    mean_score: float


class CriterionSummary(BaseModel):
    criterion_id: str
    key: str
    name: str
    type: str
    weight: float
    enabled: bool
    count: int
    #: Mean normalized score in [0, 1]; ``None`` when never scored.
    mean_score: float | None = None
    histogram: list[HistogramBucketResponse]
    #: Daily mean over the last 30 days, oldest first; days without a score are absent.
    daily: list[CriterionDayResponse]


class AgentAnnotationSummaryResponse(BaseModel):
    agent_id: str
    positive: int
    negative: int
    total: int
    positive_ratio: float | None = None
    by_target_type: list[AnnotationTypeSummary]
    #: Per-criterion statistics, every enabled criterion and every one
    #: this agent holds scores for.
    criteria: list[CriterionSummary] = Field(default_factory=list)
    #: In [0, 1]: the enabled criteria's means, weighted; the share of thumbs up
    #: when no criterion was scored; ``None`` when nothing was rated.
    weighted_score: float | None = None


def _ratio(positive: int, total: int) -> float | None:
    """Share of positive ratings, as a percentage like the agent chat stats."""
    return round(positive / total * 100, 1) if total else None


async def _resolve(
    db: AsyncSession, user: User, target_type: str, target_id: uuid.UUID
) -> AnnotationTarget:
    try:
        return await annotation_service.resolve_target(db, user, target_type, target_id)
    except AnnotationTargetNotFoundError as exc:
        # 404 whether the target is missing or not readable: a rating endpoint
        # must not tell a stranger which runs or tasks exist.
        raise HTTPException(status_code=404, detail=TARGET_NOT_FOUND) from exc


async def _managed_agent(db: AsyncSession, user: User, agent_id: str) -> Agent:
    """The agent, if *user* may read its feedback (``load_feedback_agent``)."""
    try:
        return await annotation_service.load_feedback_agent(db, user, agent_id)
    except annotation_service.AgentFeedbackForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("", response_model=AgentAnnotationListResponse)
async def list_agent_annotations(
    agent_id: str = Query(..., description="The agent whose work was rated"),
    target_type: list[AnnotationTargetType] | None = Query(None, description="Repeat for several kinds of rated work"),
    rating: Literal["positive", "negative"] | None = Query(None),
    search: str = Query("", max_length=200, description="Match the comment"),
    sort: Literal["date", "rating", "kind"] = Query(
        "date", description="Column to order by: 'date' (default), 'rating' or 'kind' (rated work type)"
    ),
    direction: Literal["asc", "desc"] = Query(
        "desc", description="Order direction; newest first breaks ties ('rating' asc puts negative first)"
    ),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Every rating of one agent's work across target types, newest first unless *sort* says otherwise."""
    agent = await _managed_agent(db, user, agent_id)
    entries, total = await annotation_service.list_agent_annotations(
        db,
        agent.id,
        target_type=target_type,
        rating=rating,
        search=search,
        sort=sort,
        direction=direction,
        limit=limit,
        offset=offset,
    )
    openable = await annotation_service.openable_entries(db, user, agent, entries)
    return AgentAnnotationListResponse(
        items=[
            AgentAnnotationItem(
                id=str(entry.id),
                target_type=entry.target_type,
                target_id=str(entry.target_id),
                conversation_id=str(entry.conversation_id) if entry.conversation_id else None,
                can_open=entry.id in openable,
                rating=entry.rating,
                comment=entry.comment,
                weighted_score=entry.weighted_score,
                user_name=entry.user_name,
                user_email=entry.user_email,
                source=entry.source,
                emoji=entry.emoji,
                created_at=entry.created_at.isoformat(),
                updated_at=entry.updated_at.isoformat(),
            )
            for entry in entries
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/summary", response_model=AgentAnnotationSummaryResponse)
async def get_agent_annotation_summary(
    agent_id: str = Query(..., description="The agent whose work was rated"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Positive / negative counts of one agent's work, per target type and overall."""
    agent = await _managed_agent(db, user, agent_id)
    counts = await annotation_service.summarize_agent_annotations(db, agent.id)
    by_type = [
        AnnotationTypeSummary(
            target_type=target_type,
            positive=positive,
            negative=negative,
            total=positive + negative,
            positive_ratio=_ratio(positive, positive + negative),
        )
        for target_type, (positive, negative) in counts.items()
    ]
    positive = sum(item.positive for item in by_type)
    negative = sum(item.negative for item in by_type)
    criteria_summary = await annotation_service.summarize_agent_criteria(db, agent.id)
    return AgentAnnotationSummaryResponse(
        agent_id=str(agent.id),
        positive=positive,
        negative=negative,
        total=positive + negative,
        positive_ratio=_ratio(positive, positive + negative),
        by_target_type=by_type,
        criteria=[
            CriterionSummary(
                criterion_id=str(stats.criterion.id),
                key=stats.criterion.key,
                name=stats.criterion.name,
                type=stats.criterion.type,
                weight=stats.criterion.weight,
                enabled=stats.criterion.enabled,
                count=stats.count,
                mean_score=stats.mean_score,
                histogram=[
                    HistogramBucketResponse(key=bucket.key, label=bucket.label, count=bucket.count)
                    for bucket in stats.histogram
                ],
                daily=[
                    CriterionDayResponse(
                        date=day.day.isoformat(), count=day.count, mean_score=day.mean_score
                    )
                    for day in criteria_summary.daily.get(stats.criterion.id, [])
                ],
            )
            for stats in criteria_summary.statistics
        ],
        weighted_score=aggregate_weighted_score(criteria_summary.statistics, positive, negative),
    )


@router.get("/mine", response_model=list[AnnotationResponse])
async def list_my_annotations(
    target_type: AnnotationTargetType = Query(...),
    target_id: list[uuid.UUID] = Query(..., description="Repeat for each target"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The caller's own ratings of many targets at once - a list page's rows in
    one request instead of one each. A target the caller has not rated is absent."""
    if len(target_id) > annotation_service.MAX_OWN_BATCH:
        raise HTTPException(
            status_code=422,
            detail=f"At most {annotation_service.MAX_OWN_BATCH} targets per request.",
        )
    rows = await annotation_service.list_own_annotations(
        db, user.id, target_type, list(dict.fromkeys(target_id))
    )
    return await _responses(db, target_type, rows)


@router.get("/{target_type}/{target_id}", response_model=AnnotationResponse | None)
async def get_my_annotation(
    target_type: AnnotationTargetType,
    target_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The caller's own rating of the target, or ``null`` when they have none."""
    target = await _resolve(
        db, user, target_type, parse_path_id(target_id, not_found_detail=TARGET_NOT_FOUND)
    )
    if target.message is not None:
        row = await load_message_feedback(db, target.message, user.id)
    else:
        row = await annotation_service.get_annotation(db, target, user.id)
    return await _response(db, target, row) if row else None


@router.post("/{target_type}/{target_id}", response_model=AnnotationResponse)
async def upsert_target_annotation(
    target_type: AnnotationTargetType,
    target_id: str,
    body: AnnotationUpsert,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set the caller's rating of the target (re-rating replaces it)."""
    target = await _resolve(
        db, user, target_type, parse_path_id(target_id, not_found_detail=TARGET_NOT_FOUND)
    )
    comment = normalize_comment(body.comment)
    rating: AnnotationRating = body.rating
    scores: list[ValidatedScore] | None = None
    if body.scores is not None:
        # Every score is checked before anything is written: a rating is never
        # saved without the scores it was sent with.
        try:
            scores = validate_scores(
                await load_criteria(db), [(score.criterion_id, score.value) for score in body.scores]
            )
        except CriterionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if target.message is not None:
        row = await apply_message_feedback(db, target.message, user, rating, comment, scores=scores)
        return await _response(db, target, row)
    annotation = await annotation_service.upsert_annotation(
        db, target, user.id, rating, comment, scores=scores
    )
    await annotation_service.forward_rating_to_langfuse(db, target, rating, comment)
    return await _response(db, target, annotation)


@router.delete("/{target_type}/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target_annotation(
    target_type: AnnotationTargetType,
    target_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retract the caller's rating of the target. Idempotent: nothing to retract is a 204."""
    target = await _resolve(
        db, user, target_type, parse_path_id(target_id, not_found_detail=TARGET_NOT_FOUND)
    )
    if target.message is not None:
        await retract_message_feedback(db, target.message, user)
    elif await annotation_service.delete_annotation(db, target, user.id):
        await annotation_service.retract_rating_from_langfuse(db, target)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{target_type}/{target_id}/stats", response_model=AnnotationStatsResponse)
async def get_annotation_stats(
    target_type: AnnotationTargetType,
    target_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """How everyone rated the target: counts only, never who or what they wrote."""
    target = await _resolve(
        db, user, target_type, parse_path_id(target_id, not_found_detail=TARGET_NOT_FOUND)
    )
    positive, negative = await annotation_service.target_rating_counts(db, target)
    return AnnotationStatsResponse(
        target_type=target.target_type,
        target_id=str(target.target_id),
        positive=positive,
        negative=negative,
        total=positive + negative,
    )


async def _responses(db: AsyncSession, target_type: str, rows) -> list[AnnotationResponse]:
    """``message_feedback`` rows or ``Annotation`` rows of one target type, with
    their scores, in the one API shape - one scores query whatever their number."""
    if not rows:
        return []
    ids = [row.id for row in rows]
    if target_type == "message":
        grouped = await load_scores(db, message_feedback_ids=ids)
    else:
        grouped = await load_scores(db, annotation_ids=ids)
    criteria_by_id = (
        {criterion.id: criterion for criterion in await load_criteria(db, include_disabled=True)}
        if grouped
        else {}
    )
    responses = []
    for row in rows:
        scores = grouped.get(row.id, [])
        responses.append(
            AnnotationResponse(
                id=str(row.id),
                target_type=target_type,
                target_id=str(row.message_id if target_type == "message" else row.target_id),
                rating=row.rating,
                comment=row.comment,
                scores=[
                    ScoreResponse(criterion_id=str(score.criterion_id), value=score_value(score))
                    for score in scores
                ],
                weighted_score=rating_weighted_score(row.rating, scores, criteria_by_id),
                created_at=row.created_at.isoformat(),
                updated_at=row.updated_at.isoformat(),
            )
        )
    return responses


async def _response(db: AsyncSession, target: AnnotationTarget, row) -> AnnotationResponse:
    [response] = await _responses(db, target.target_type, [row])
    return response
