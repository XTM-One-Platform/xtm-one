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

"""Rating agent work: the polymorphic annotation service.

A user can rate what an agent produced wherever they read it: a chat message, the
result of an assignment run, a background task, the answer to a delegation, an
agent account's answer (also rated by the reactions people put on it in the
platform, ``services.reaction_feedback``). One
registry here resolves every rateable ``(target_type, target_id)`` to the row it
names, under the SAME read rule as that target's own detail endpoint, and to the
agent that did the work. The router (``api/v1/annotations.py``) never re-derives
a visibility rule, and a new rateable surface is one registry entry here plus the
place the UI offers the rating.

Storage: a ``message`` target is the chat's own ``message_feedback`` row, written
through ``api.v1.chat.apply_message_feedback`` / ``retract_message_feedback`` -
the one path the chat endpoint also uses. Every other target is an
``Annotation`` row (``models/annotation.py`` explains why the two were not
merged). Reads that span targets (an agent's feedback) union both.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, get_args

from sqlalchemy import String, and_, cast, func, literal, null, or_, select, union_all
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.v1.helpers import (
    can_manage,
    can_view_agent_chats,
    get_user_agent,
    get_user_group_ids,
    run_visible_filter,
)
from app.core.utils import format_exception_message
from app.models.agent import Agent
from app.models.agent_account import AgentAccount, AgentAccountReply
from app.models.agent_delegation import AgentDelegation
from app.models.annotation import Annotation, AnnotationScore
from app.models.assignment import AssignmentRun
from app.models.chat import Conversation, Message, MessageFeedback
from app.models.sub_agent_task import SubAgentTask
from app.models.user import User
from app.services.feedback_criteria import (
    CriterionStatistics,
    ValidatedScore,
    aggregate_weighted_score,
    criterion_statistics,
    load_criteria,
    load_scores,
    normalized_value,
    rating_weighted_score,
    replace_scores,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

AnnotationTargetType = Literal[
    "message", "assignment_run", "sub_agent_task", "agent_delegation", "agent_account_reply"
]
ANNOTATION_TARGET_TYPES: tuple[str, ...] = get_args(AnnotationTargetType)
AnnotationRating = Literal["positive", "negative"]

#: The target types stored in ``annotations``, with the model their
#: ``target_id`` points at: what every live-target check and the orphan sweep
#: join. ``message`` is absent on purpose - it lives in ``message_feedback``,
#: whose real foreign key already removes a rating with its message.
_STORED_TARGET_MODELS: dict[str, Any] = {
    "assignment_run": AssignmentRun,
    "sub_agent_task": SubAgentTask,
    "agent_delegation": AgentDelegation,
    "agent_account_reply": AgentAccountReply,
}

#: Same bound as the chat feedback comment (``MessageFeedbackUpsert``).
MAX_COMMENT_LENGTH = 2000
#: Most targets one ``list_own_annotations`` call reads: a page of a list.
MAX_OWN_BATCH = 100
#: Rows the hourly orphan sweep deletes per call, like the other cleanup sweeps.
ORPHAN_SWEEP_BATCH = 500

#: The score name the chat already forwards to Langfuse, so a run rating reads
#: exactly like a message rating there.
_LANGFUSE_SCORE_NAME = "user-feedback"


class AnnotationTargetNotFoundError(LookupError):
    """The target does not exist, or the caller may not read it (never told apart)."""


@dataclass(frozen=True, slots=True)
class AnnotationTarget:
    """A resolved target: what was rated, and who did the work."""

    target_type: str
    target_id: uuid.UUID
    #: The agent that produced the rated work. ``None`` only for a message of a
    #: General Assistant conversation, which has no agent.
    agent_id: uuid.UUID | None
    #: Where Langfuse keeps the work's trace, when the target records one.
    langfuse_trace_id: str | None = None
    #: The message row itself, for the ``message`` branch (chat's write path).
    message: Message | None = None


@dataclass(frozen=True, slots=True)
class AnnotationListEntry:
    """One rating in an agent's feedback, whatever its target type."""

    id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    conversation_id: uuid.UUID | None
    rating: str
    comment: str | None
    user_name: str | None
    user_email: str | None
    created_at: datetime
    updated_at: datetime
    #: The rating's weighted score in [0, 1]: its criterion scores, or
    #: the thumbs when it has none (``feedback_criteria.weighted_score``).
    weighted_score: float = 0.0
    #: Where it was given (``web`` or a platform) and, for a reaction, the emoji.
    source: str = "web"
    emoji: str | None = None
    #: Who gave it (``None`` for a platform rater): a reader honouring a user's
    #: opt-out (the Concierge, #8259) filters on it; never rendered.
    user_id: uuid.UUID | None = None


def normalize_comment(value: str | None) -> str | None:
    """A comment as stored: surrounding whitespace dropped, blank read as none."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _like_pattern(term: str) -> str:
    """``%term%`` with the LIKE wildcards of *term* escaped (backslash escape)."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ---------------------------------------------------------------------------
# Target resolution - each target's own read rule, never a new one
# ---------------------------------------------------------------------------


def _readable_runs(user: User) -> list[Any]:
    """The clauses of the runs *user* may read: the run page's own rule
    (``assignments.get_run_detail``), every run for an admin."""
    return [] if user.is_admin else [run_visible_filter(user.id)]


async def resolve_target(
    db: AsyncSession,
    user: User,
    target_type: str,
    target_id: uuid.UUID,
) -> AnnotationTarget:
    """The target, if *user* may read it; otherwise ``AnnotationTargetNotFoundError``.

    Each branch mirrors the target's own detail endpoint, so a rating is offered
    exactly where the rated work can be read:

    - ``message``: the conversation owner (``chat._get_owned_message``);
    - ``assignment_run``: any run for an admin, otherwise own + company-managed
      runs (``assignments.get_run_detail``, ``run_visible_filter``);
    - ``sub_agent_task``: the task's user (``sub_agent.get_task_result``);
    - ``agent_delegation``: the delegation's user, for a consultation only. An
      admin's audited oversight read of a colleague's delegations
      (``chat.get_delegation``) does not make its answer theirs to rate.
      A background task's delegation carries the task's own result (rated as
      the task, or as its run) and a transfer's the answer the chat already
      shows (rated in the chat): rating those as well would count the same
      work twice.
    - ``agent_account_reply``: a posted answer of an agent account, for the
      account's owner and an administrator (``agent_accounts._load_reply``).
      Its agent is the one that wrote it (``AgentAccountReply.agent_id``), the
      account's current one for a reply recorded before that column.
    """
    if target_type == "message":
        row = (
            await db.execute(
                select(Message, Conversation.agent_id)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Message.id == target_id, Conversation.user_id == user.id)
            )
        ).first()
        if row is None:
            raise AnnotationTargetNotFoundError(target_type)
        msg, agent_id = row
        return AnnotationTarget(target_type, target_id, agent_id, message=msg)

    if target_type == "assignment_run":
        query = select(AssignmentRun.agent_id, AssignmentRun.langfuse_trace_id).where(
            AssignmentRun.id == target_id, *_readable_runs(user)
        )
        row = (await db.execute(query)).first()
        if row is None:
            raise AnnotationTargetNotFoundError(target_type)
        return AnnotationTarget(target_type, target_id, row[0], langfuse_trace_id=row[1])

    if target_type == "sub_agent_task":
        agent_id = await db.scalar(
            select(SubAgentTask.target_agent_id).where(
                SubAgentTask.id == target_id, SubAgentTask.user_id == user.id
            )
        )
        if agent_id is None:
            raise AnnotationTargetNotFoundError(target_type)
        return AnnotationTarget(target_type, target_id, agent_id)

    if target_type == "agent_delegation":
        agent_id = await db.scalar(
            select(AgentDelegation.target_agent_id).where(
                AgentDelegation.id == target_id,
                AgentDelegation.user_id == user.id,
                AgentDelegation.type == "consult",
            )
        )
        if agent_id is None:
            raise AnnotationTargetNotFoundError(target_type)
        return AnnotationTarget(target_type, target_id, agent_id)

    if target_type == "agent_account_reply":
        query = (
            select(func.coalesce(AgentAccountReply.agent_id, AgentAccount.agent_id))
            .join(AgentAccount, AgentAccount.id == AgentAccountReply.account_id)
            .where(AgentAccountReply.id == target_id, AgentAccountReply.status == "sent")
        )
        if not user.is_admin:
            query = query.where(AgentAccount.user_id == user.id)
        agent_id = await db.scalar(query)
        if agent_id is None:
            raise AnnotationTargetNotFoundError(target_type)
        return AnnotationTarget(target_type, target_id, agent_id)

    raise AnnotationTargetNotFoundError(target_type)


# ---------------------------------------------------------------------------
# Stored annotations (every target but ``message``)
# ---------------------------------------------------------------------------


def _own_row(target: AnnotationTarget, user_id: uuid.UUID):
    return and_(
        Annotation.target_type == target.target_type,
        Annotation.target_id == target.target_id,
        Annotation.user_id == user_id,
    )


async def get_annotation(
    db: AsyncSession, target: AnnotationTarget, user_id: uuid.UUID
) -> Annotation | None:
    """*user_id*'s annotation of a stored *target*, if any."""
    result = await db.execute(select(Annotation).where(_own_row(target, user_id)))
    return result.scalar_one_or_none()


async def upsert_annotation(
    db: AsyncSession,
    target: AnnotationTarget,
    user_id: uuid.UUID,
    rating: AnnotationRating,
    comment: str | None,
    *,
    scores: Sequence[ValidatedScore] | None = None,
) -> Annotation:
    """Record *user_id*'s rating of a stored *target*, replacing a previous one.

    One ``INSERT ... ON CONFLICT DO UPDATE`` on ``uq_annotations_target_user``:
    two concurrent ratings of the same target by the same user (a double click)
    converge on one row instead of one of them failing on the unique key.
    *scores* (validated) replace the rating's per-criterion scores in the same
    transaction; ``None`` leaves them as they are.
    """
    if target.agent_id is None:  # pragma: no cover - every stored target has an agent
        raise AnnotationTargetNotFoundError(target.target_type)
    now = datetime.now(UTC)
    statement = (
        pg_insert(Annotation)
        .values(
            id=uuid.uuid4(),
            target_type=target.target_type,
            target_id=target.target_id,
            user_id=user_id,
            agent_id=target.agent_id,
            rating=rating,
            comment=comment,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_annotations_target_user",
            set_={
                "rating": rating,
                "comment": comment,
                "agent_id": target.agent_id,
                "updated_at": now,
            },
        )
        .returning(Annotation)
        .execution_options(populate_existing=True)
    )
    annotation = (await db.execute(statement)).scalar_one()
    if scores is not None:
        await replace_scores(db, annotation_id=annotation.id, scores=scores)
    await db.commit()
    return annotation


async def delete_annotation(
    db: AsyncSession, target: AnnotationTarget, user_id: uuid.UUID
) -> bool:
    """Delete *user_id*'s annotation of a stored *target*; True when there was one."""
    result = await db.execute(Annotation.__table__.delete().where(_own_row(target, user_id)))
    await db.commit()
    return bool(result.rowcount)


# ---------------------------------------------------------------------------
# Platform raters: reactions, written by ``services.reaction_feedback``
# ---------------------------------------------------------------------------


def _target_rows(target: AnnotationTarget):
    return and_(
        Annotation.target_type == target.target_type,
        Annotation.target_id == target.target_id,
    )


async def platform_ratings(db: AsyncSession, target: AnnotationTarget) -> dict[str, Annotation]:
    """The platform raters' ratings of a stored *target*, by ``rater_ref``."""
    result = await db.execute(
        select(Annotation).where(_target_rows(target), Annotation.rater_ref.is_not(None))
    )
    return {row.rater_ref: row for row in result.scalars().all() if row.rater_ref}


async def upsert_platform_rating(
    db: AsyncSession,
    target: AnnotationTarget,
    *,
    rater_ref: str,
    source: str,
    rating: AnnotationRating,
    emoji: str,
) -> None:
    """Record a platform rater's rating of a stored *target*, replacing theirs.

    The conflict target is the partial unique index ``uq_annotations_target_rater``.
    The caller commits (``reaction_feedback.apply_reactions``).
    """
    if target.agent_id is None:  # pragma: no cover - every stored target has an agent
        raise AnnotationTargetNotFoundError(target.target_type)
    now = datetime.now(UTC)
    await db.execute(
        pg_insert(Annotation)
        .values(
            id=uuid.uuid4(),
            target_type=target.target_type,
            target_id=target.target_id,
            user_id=None,
            rater_ref=rater_ref,
            source=source,
            emoji=emoji,
            agent_id=target.agent_id,
            rating=rating,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["target_type", "target_id", "rater_ref"],
            index_where=Annotation.rater_ref.is_not(None),
            set_={
                "rating": rating,
                "emoji": emoji,
                "source": source,
                "agent_id": target.agent_id,
                "updated_at": now,
            },
        )
    )


async def delete_platform_ratings(
    db: AsyncSession, target: AnnotationTarget, rater_refs: Sequence[str]
) -> int:
    """Retract the ratings of *rater_refs* on a stored *target*; how many went. The caller commits."""
    result = await db.execute(
        Annotation.__table__.delete().where(_target_rows(target), Annotation.rater_ref.in_(rater_refs))
    )
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# Langfuse (stored targets; a message goes through the chat's own forwarding)
# ---------------------------------------------------------------------------


def _send_langfuse_score(client: Any, payload: dict[str, object], target: AnnotationTarget) -> None:
    try:
        if hasattr(client, "score"):
            client.score(**payload)
        elif hasattr(client, "create_score"):
            client.create_score(**payload)
    except Exception as exc:
        logger.warning(
            "Failed to forward the %s %s rating to Langfuse: %s",
            target.target_type,
            target.target_id,
            format_exception_message(exc),
        )


async def _langfuse_client_for(db: AsyncSession, target: AnnotationTarget) -> Any | None:
    if not target.langfuse_trace_id:
        return None
    from app.services.langfuse_tracing import get_langfuse_client

    try:
        return await get_langfuse_client(db)
    except Exception as exc:
        logger.warning("Langfuse client unavailable: %s", format_exception_message(exc))
        return None


async def forward_rating_to_langfuse(
    db: AsyncSession,
    target: AnnotationTarget,
    rating: AnnotationRating,
    comment: str | None,
) -> None:
    """Best-effort: score the target's Langfuse trace like a chat rating is scored."""
    client = await _langfuse_client_for(db, target)
    if client is None:
        return
    payload: dict[str, object] = {
        "name": _LANGFUSE_SCORE_NAME,
        "value": 1 if rating == "positive" else 0,
        "trace_id": target.langfuse_trace_id,
    }
    if comment:
        payload["comment"] = comment
    _send_langfuse_score(client, payload, target)


async def retract_rating_from_langfuse(db: AsyncSession, target: AnnotationTarget) -> None:
    """Best-effort retraction. Langfuse scores are append-only, so a retraction is a
    new ``value=0`` score saying so - the chat's convention, kept identical."""
    client = await _langfuse_client_for(db, target)
    if client is None:
        return
    _send_langfuse_score(
        client,
        {
            "name": _LANGFUSE_SCORE_NAME,
            "value": 0,
            "comment": "feedback retracted by user",
            "trace_id": target.langfuse_trace_id,
        },
        target,
    )


# ---------------------------------------------------------------------------
# Reads across users and targets
# ---------------------------------------------------------------------------

#: Why someone who sees an agent but does not manage it is refused its feedback.
FEEDBACK_ACCESS_DETAIL = "Agent feedback requires agent owner, admin, or config group permission."


class AgentFeedbackForbiddenError(PermissionError):
    """The caller sees the agent but is not among its managers."""


async def load_feedback_agent(db: AsyncSession, user: User, agent_id: str) -> Agent:
    """The agent whose feedback *user* may read: its managers - owner, admin, a
    config-tier grant - as for the agent's feedback comments.

    The one gate of every read of an agent's feedback (the REST list and summary,
    the ``get_agent_feedback`` tool). An agent the user cannot see is
    ``get_user_agent``'s 404; one they see without managing it raises
    ``AgentFeedbackForbiddenError``.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(db, user.id, agent_id, user_group_ids, is_admin=user.is_admin)
    if not can_manage(user, agent, user_group_ids):
        raise AgentFeedbackForbiddenError(FEEDBACK_ACCESS_DETAIL)
    return agent


async def openable_entries(
    db: AsyncSession, user: User, agent: Agent, entries: Sequence[AnnotationListEntry]
) -> set[uuid.UUID]:
    """The ids of the *entries* whose rated work *user* may open where it lives.

    A manager reads every rating of the agent's work, not every rated run or
    conversation: a run opens under the run page's own rule, a chat message in
    the agent's chat history under the chats' own rule
    (``helpers.can_view_agent_chats``). A background task and a delegation have
    no page of their own.
    """
    run_ids = {entry.target_id for entry in entries if entry.target_type == "assignment_run"}
    readable_runs: set[uuid.UUID] = set()
    if run_ids:
        result = await db.execute(
            select(AssignmentRun.id).where(AssignmentRun.id.in_(run_ids), *_readable_runs(user))
        )
        readable_runs = {row[0] for row in result.all()}
    chats_open = False
    if any(entry.target_type == "message" for entry in entries):
        chats_open = can_view_agent_chats(user, agent, await get_user_group_ids(db, user.id))
    return {
        entry.id
        for entry in entries
        if (entry.target_type == "assignment_run" and entry.target_id in readable_runs)
        or (entry.target_type == "message" and chats_open and entry.conversation_id is not None)
    }


def agent_answer_clauses(agent_id: uuid.UUID) -> list[Any]:
    """The clauses of the chat messages rated as *agent_id*'s work: the answers
    of its conversations. A user may rate any message of their conversation,
    their own prompts included, and those say nothing about the agent."""
    return [Conversation.agent_id == agent_id, Message.role == "assistant"]


def live_target_clause():
    """``annotations`` rows whose target still exists.

    Without a foreign key on ``target_id`` a deleted run leaves its annotation
    behind until the hourly sweep; this is what keeps every listing and count
    from showing it meanwhile. A target type this code does not know (a newer
    replica's) is excluded too: nothing here could render it.
    """
    return or_(
        *(
            and_(
                Annotation.target_type == target_type,
                select(model.id).where(model.id == Annotation.target_id).exists(),
            )
            for target_type, model in _STORED_TARGET_MODELS.items()
        )
    )


async def list_own_annotations(
    db: AsyncSession,
    user_id: uuid.UUID,
    target_type: str,
    target_ids: Sequence[uuid.UUID],
) -> list[Any]:
    """*user_id*'s own ratings of *target_ids*, in one query: a list page's rows.

    ``message_feedback`` rows for ``message``, ``Annotation`` rows otherwise.
    Only the caller's own rows are read, so no target is resolved - a rating the
    caller left tells them nothing new - and a stored rating whose target is
    gone is left out, as everywhere.
    """
    if not target_ids:
        return []
    if target_type == "message":
        query = select(MessageFeedback).where(
            MessageFeedback.user_id == user_id,
            MessageFeedback.message_id.in_(target_ids),
        )
    else:
        query = select(Annotation).where(
            Annotation.user_id == user_id,
            Annotation.target_type == target_type,
            Annotation.target_id.in_(target_ids),
            live_target_clause(),
        )
    return list((await db.execute(query)).scalars().all())


async def target_rating_counts(
    db: AsyncSession, target: AnnotationTarget
) -> tuple[int, int]:
    """``(positive, negative)`` ratings of *target*, all users together."""
    if target.target_type == "message":
        query = (
            select(MessageFeedback.rating, func.count())
            .where(MessageFeedback.message_id == target.target_id)
            .group_by(MessageFeedback.rating)
        )
    else:
        query = (
            select(Annotation.rating, func.count())
            .where(
                Annotation.target_type == target.target_type,
                Annotation.target_id == target.target_id,
            )
            .group_by(Annotation.rating)
        )
    counts = {rating: int(count) for rating, count in (await db.execute(query)).all()}
    return counts.get("positive", 0), counts.get("negative", 0)


def _message_rows(agent_id: uuid.UUID, rating: str | None, search: str):
    query = (
        select(
            MessageFeedback.id.label("id"),
            literal("message", String(32)).label("target_type"),
            MessageFeedback.message_id.label("target_id"),
            Conversation.id.label("conversation_id"),
            MessageFeedback.user_id.label("user_id"),
            MessageFeedback.rating.label("rating"),
            MessageFeedback.comment.label("comment"),
            literal("web", String(32)).label("source"),
            cast(null(), String(32)).label("emoji"),
            MessageFeedback.created_at.label("created_at"),
            MessageFeedback.updated_at.label("updated_at"),
        )
        .join(Message, MessageFeedback.message_id == Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(*agent_answer_clauses(agent_id))
    )
    if rating:
        query = query.where(MessageFeedback.rating == rating)
    if search:
        query = query.where(MessageFeedback.comment.ilike(_like_pattern(search), escape="\\"))
    return query


def _stored_rows(agent_id: uuid.UUID, target_types: frozenset[str] | None, rating: str | None, search: str):
    query = select(
        Annotation.id.label("id"),
        Annotation.target_type.label("target_type"),
        Annotation.target_id.label("target_id"),
        cast(null(), PG_UUID(as_uuid=True)).label("conversation_id"),
        Annotation.user_id.label("user_id"),
        Annotation.rating.label("rating"),
        Annotation.comment.label("comment"),
        Annotation.source.label("source"),
        Annotation.emoji.label("emoji"),
        Annotation.created_at.label("created_at"),
        Annotation.updated_at.label("updated_at"),
    ).where(Annotation.agent_id == agent_id, live_target_clause())
    if target_types:
        query = query.where(Annotation.target_type.in_(sorted(target_types)))
    if rating:
        query = query.where(Annotation.rating == rating)
    if search:
        query = query.where(Annotation.comment.ilike(_like_pattern(search), escape="\\"))
    return query


#: The columns ``list_agent_annotations`` sorts on (the API's ``sort`` parameter).
AgentAnnotationSort = Literal["date", "rating", "kind"]
SortDirection = Literal["asc", "desc"]


def _agent_annotation_order(rows: Any, sort: AgentAnnotationSort, direction: SortDirection) -> tuple[Any, ...]:
    """ORDER BY clauses of an agent's ratings; newest first breaks ties, then the id, so pages never overlap."""
    newest = rows.c.updated_at.desc()
    if sort == "date":
        return (rows.c.updated_at.asc() if direction == "asc" else newest, rows.c.id)
    # "negative" < "positive": ascending puts the ratings to look at first.
    column = rows.c.rating if sort == "rating" else rows.c.target_type
    return (column.asc() if direction == "asc" else column.desc(), newest, rows.c.id)


async def list_agent_annotations(
    db: AsyncSession,
    agent_id: uuid.UUID,
    *,
    target_type: str | Sequence[str] | None = None,
    rating: str | None = None,
    search: str = "",
    sort: AgentAnnotationSort = "date",
    direction: SortDirection = "desc",
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[AnnotationListEntry], int]:
    """Every rating of *agent_id*'s work in the *sort* order, and how many there are.

    *sort* is the rating's ``date`` (default, newest first), its ``rating``
    (ascending: negative first) or the rated work's ``kind``, in *direction*;
    newest first breaks ties.

    The caller authorizes the agent; this only reads. Chat-message ratings come
    from ``message_feedback`` (by the conversation's agent), the others from
    ``annotations`` (by the agent that did the work, live targets only).
    """
    search = search.strip()
    # One type or several (OR); none or empty is every type.
    types = frozenset([target_type] if isinstance(target_type, str) else target_type or ()) or None
    stored_types = types - {"message"} if types else None
    parts = []
    if types is None or "message" in types:
        parts.append(_message_rows(agent_id, rating, search))
    if types is None or stored_types:
        parts.append(_stored_rows(agent_id, stored_types, rating, search))
    rows = (union_all(*parts) if len(parts) > 1 else parts[0]).subquery()

    total = int(await db.scalar(select(func.count()).select_from(rows)) or 0)
    result = await db.execute(
        select(rows, User.full_name.label("user_name"), User.email.label("user_email"))
        .outerjoin(User, User.id == rows.c.user_id)
        .order_by(*_agent_annotation_order(rows, sort, direction))
        .offset(offset)
        .limit(limit)
    )
    page = result.all()
    # Only this page's scores: a page is bounded, an agent's history is not.
    scores = await load_scores(
        db,
        annotation_ids=[row.id for row in page if row.target_type != "message"],
        message_feedback_ids=[row.id for row in page if row.target_type == "message"],
    )
    criteria_by_id = (
        {criterion.id: criterion for criterion in await load_criteria(db, include_disabled=True)}
        if scores
        else {}
    )
    entries = [
        AnnotationListEntry(
            id=row.id,
            target_type=row.target_type,
            target_id=row.target_id,
            conversation_id=row.conversation_id,
            rating=row.rating,
            comment=row.comment,
            user_name=row.user_name,
            user_email=row.user_email,
            created_at=row.created_at,
            updated_at=row.updated_at,
            weighted_score=rating_weighted_score(row.rating, scores.get(row.id, ()), criteria_by_id),
            source=row.source,
            emoji=row.emoji,
            user_id=row.user_id,
        )
        for row in page
    ]
    return entries, total


async def agent_rating_raters(
    db: AsyncSession, agent_id: uuid.UUID, rating_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID | None]:
    """``{rating id: its user (None for a platform rater)}`` for those of
    *rating_ids* that are ratings of *agent_id*'s work, in either storage.

    The check a reader citing ratings as evidence runs on the rows (#8259):
    the same rows ``list_agent_annotations`` lists for the agent, nothing else.
    """
    if not rating_ids:
        return {}
    found: dict[uuid.UUID, uuid.UUID | None] = {}
    for rows in (
        _message_rows(agent_id, None, "").where(MessageFeedback.id.in_(rating_ids)).subquery(),
        _stored_rows(agent_id, None, None, "").where(Annotation.id.in_(rating_ids)).subquery(),
    ):
        for rating_id, user_id in (await db.execute(select(rows.c.id, rows.c.user_id))).all():
            found[rating_id] = user_id
    return found


async def summarize_agent_annotations(
    db: AsyncSession, agent_id: uuid.UUID, *, raters: Literal["all", "users", "platform"] = "all"
) -> dict[str, tuple[int, int]]:
    """``{target_type: (positive, negative)}`` for *agent_id*, every type present.

    *raters* keeps the ratings of XTM One users (``users``: the web thumbs) or
    of platform raters (``platform``: reactions, ``rater_ref``) apart, for a
    reader that must not weigh one as the other.
    """
    counts: dict[str, dict[str, int]] = {t: {} for t in ANNOTATION_TARGET_TYPES}
    if raters != "platform":
        # Chat answers are rated in the web chat only: every one is a user's.
        message_counts = await db.execute(
            select(MessageFeedback.rating, func.count())
            .join(Message, MessageFeedback.message_id == Message.id)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(*agent_answer_clauses(agent_id))
            .group_by(MessageFeedback.rating)
        )
        for rating, count in message_counts.all():
            counts["message"][rating] = int(count)
    query = select(Annotation.target_type, Annotation.rating, func.count()).where(
        Annotation.agent_id == agent_id, live_target_clause()
    )
    if raters == "users":
        query = query.where(Annotation.user_id.is_not(None))
    elif raters == "platform":
        query = query.where(Annotation.rater_ref.is_not(None))
    stored_counts = await db.execute(query.group_by(Annotation.target_type, Annotation.rating))
    for target_type, rating, count in stored_counts.all():
        if target_type in counts:
            counts[target_type][rating] = int(count)
    return {
        target_type: (by_rating.get("positive", 0), by_rating.get("negative", 0))
        for target_type, by_rating in counts.items()
    }


@dataclass(slots=True)
class CriterionDay:
    day: date
    count: int
    mean_score: float


@dataclass(slots=True)
class AgentCriteriaSummary:
    """Per-criterion statistics of one agent's ratings."""

    statistics: list[CriterionStatistics]
    #: ``{criterion_id: [CriterionDay, ...]}`` over the last ``days``, oldest first.
    daily: dict[uuid.UUID, list[CriterionDay]]


def _agent_scores(agent_id: uuid.UUID):
    """Every per-criterion score given to *agent_id*'s work, both storages."""
    columns = (
        AnnotationScore.criterion_id.label("criterion_id"),
        AnnotationScore.value_numeric.label("value_numeric"),
        AnnotationScore.value_text.label("value_text"),
        AnnotationScore.created_at.label("created_at"),
    )
    via_messages = (
        select(*columns)
        .join(MessageFeedback, AnnotationScore.message_feedback_id == MessageFeedback.id)
        .join(Message, MessageFeedback.message_id == Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(*agent_answer_clauses(agent_id))
    )
    via_annotations = (
        select(*columns)
        .join(Annotation, AnnotationScore.annotation_id == Annotation.id)
        .where(Annotation.agent_id == agent_id, live_target_clause())
    )
    return union_all(via_messages, via_annotations).subquery()


async def summarize_agent_criteria(
    db: AsyncSession, agent_id: uuid.UUID, *, days: int = 30
) -> AgentCriteriaSummary:
    """Count, mean and histogram per criterion, and a daily mean over *days*.

    Grouped in SQL by criterion and value, so the cost follows the number of
    distinct values, not the number of ratings; the values are normalized here
    with the criterion's current definition (``feedback_criteria``). Every
    criterion that is enabled or holds a score of this agent is listed.
    """
    criteria = await load_criteria(db, include_disabled=True)
    scores = _agent_scores(agent_id)
    grouped = await db.execute(
        select(scores.c.criterion_id, scores.c.value_numeric, scores.c.value_text, func.count())
        .group_by(scores.c.criterion_id, scores.c.value_numeric, scores.c.value_text)
    )
    groups: dict[uuid.UUID, list[tuple[float | None, str | None, int]]] = {}
    for criterion_id, value_numeric, value_text, count in grouped.all():
        groups.setdefault(criterion_id, []).append((value_numeric, value_text, int(count)))

    since = datetime.now(UTC) - timedelta(days=days)
    day = func.date_trunc("day", scores.c.created_at)
    per_day = await db.execute(
        select(scores.c.criterion_id, day, scores.c.value_numeric, scores.c.value_text, func.count())
        .where(scores.c.created_at >= since)
        .group_by(scores.c.criterion_id, day, scores.c.value_numeric, scores.c.value_text)
    )
    by_id = {criterion.id: criterion for criterion in criteria}
    day_totals: dict[tuple[uuid.UUID, date], tuple[int, float]] = {}
    for criterion_id, bucket, value_numeric, value_text, count in per_day.all():
        criterion = by_id.get(criterion_id)
        if criterion is None:
            continue
        normalized = normalized_value(criterion, value_numeric, value_text)
        if normalized is None:
            continue
        key = (criterion_id, bucket.date() if isinstance(bucket, datetime) else bucket)
        seen, total = day_totals.get(key, (0, 0.0))
        day_totals[key] = (seen + int(count), total + normalized * int(count))
    daily: dict[uuid.UUID, list[CriterionDay]] = {}
    for (criterion_id, bucket), (count, total) in sorted(day_totals.items(), key=lambda item: item[0][1]):
        daily.setdefault(criterion_id, []).append(CriterionDay(bucket, count, total / count))

    statistics = [
        criterion_statistics(criterion, groups.get(criterion.id, ()))
        for criterion in criteria
        if criterion.enabled or criterion.id in groups
    ]
    return AgentCriteriaSummary(statistics=statistics, daily=daily)


@dataclass(frozen=True, slots=True)
class WindowScore:
    """An agent's weighted score over a window, and the ratings it rests on."""

    score: float | None
    ratings: int


async def agents_weighted_score_window(
    db: AsyncSession, agent_ids: Sequence[uuid.UUID], *, since: datetime, until: datetime
) -> dict[uuid.UUID, WindowScore]:
    """:func:`~app.services.feedback_criteria.aggregate_weighted_score` of each of
    *agent_ids* over the ratings given or changed in ``[since, until)``.

    Batched for the Concierge's feedback signal (#8261): three grouped queries
    whatever the number of agents, each riding a ``(…, updated_at)`` /
    ``created_at`` range. A rating counts at the time it was last given.
    """
    if not agent_ids:
        return {}
    ids = list(agent_ids)
    thumbs: dict[uuid.UUID, list[int]] = {agent_id: [0, 0] for agent_id in ids}
    message_counts = await db.execute(
        select(Conversation.agent_id, MessageFeedback.rating, func.count())
        .join(Message, MessageFeedback.message_id == Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.agent_id.in_(ids),
            Message.role == "assistant",
            MessageFeedback.updated_at >= since,
            MessageFeedback.updated_at < until,
        )
        .group_by(Conversation.agent_id, MessageFeedback.rating)
    )
    stored_counts = await db.execute(
        select(Annotation.agent_id, Annotation.rating, func.count())
        .where(
            Annotation.agent_id.in_(ids),
            Annotation.updated_at >= since,
            Annotation.updated_at < until,
            live_target_clause(),
        )
        .group_by(Annotation.agent_id, Annotation.rating)
    )
    for agent_id, rating, count in [*message_counts.all(), *stored_counts.all()]:
        if agent_id in thumbs and rating in ("positive", "negative"):
            thumbs[agent_id][0 if rating == "positive" else 1] += int(count)

    columns = (
        AnnotationScore.criterion_id.label("criterion_id"),
        AnnotationScore.value_numeric.label("value_numeric"),
        AnnotationScore.value_text.label("value_text"),
    )
    in_window = (AnnotationScore.created_at >= since, AnnotationScore.created_at < until)
    scores = union_all(
        select(Conversation.agent_id.label("agent_id"), *columns)
        .join(MessageFeedback, AnnotationScore.message_feedback_id == MessageFeedback.id)
        .join(Message, MessageFeedback.message_id == Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.agent_id.in_(ids), Message.role == "assistant", *in_window),
        select(Annotation.agent_id.label("agent_id"), *columns)
        .join(Annotation, AnnotationScore.annotation_id == Annotation.id)
        .where(Annotation.agent_id.in_(ids), live_target_clause(), *in_window),
    ).subquery()
    grouped = await db.execute(
        select(scores.c.agent_id, scores.c.criterion_id, scores.c.value_numeric, scores.c.value_text, func.count())
        .group_by(scores.c.agent_id, scores.c.criterion_id, scores.c.value_numeric, scores.c.value_text)
    )
    groups: dict[uuid.UUID, dict[uuid.UUID, list[tuple[float | None, str | None, int]]]] = {}
    for agent_id, criterion_id, value_numeric, value_text, count in grouped.all():
        groups.setdefault(agent_id, {}).setdefault(criterion_id, []).append((value_numeric, value_text, int(count)))

    criteria = await load_criteria(db, include_disabled=True)
    result: dict[uuid.UUID, WindowScore] = {}
    for agent_id in ids:
        positive, negative = thumbs[agent_id]
        by_criterion = groups.get(agent_id, {})
        statistics = [
            criterion_statistics(criterion, by_criterion.get(criterion.id, ()))
            for criterion in criteria
            if criterion.enabled or criterion.id in by_criterion
        ]
        result[agent_id] = WindowScore(
            score=aggregate_weighted_score(statistics, positive, negative), ratings=positive + negative
        )
    return result


# ---------------------------------------------------------------------------
# Hourly backstop
# ---------------------------------------------------------------------------


async def purge_orphan_annotations(*, limit: int = ORPHAN_SWEEP_BATCH) -> int:
    """Delete up to *limit* annotations whose target no longer exists.

    A run or a task can be deleted in several places (single delete, bulk
    delete, an assignment's cascade), and ``target_id`` has no key to follow
    them. Readers already hide such rows (``live_target_clause``); this removes
    them, oldest first. A target type this code does not know is left alone: a
    newer replica may have written it during a rolling deploy.
    """
    from app.core.database import async_session

    orphaned = or_(
        *(
            and_(
                Annotation.target_type == target_type,
                ~select(model.id).where(model.id == Annotation.target_id).exists(),
            )
            for target_type, model in _STORED_TARGET_MODELS.items()
        )
    )
    batch = select(Annotation.id).where(orphaned).order_by(Annotation.updated_at).limit(limit)
    async with async_session() as db:
        result = await db.execute(Annotation.__table__.delete().where(Annotation.id.in_(batch)))
        await db.commit()
        return int(result.rowcount or 0)
