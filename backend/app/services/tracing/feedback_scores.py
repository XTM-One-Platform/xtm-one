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

"""How a trace's ``user_feedback`` scores add up to one feedback value.

A rater can change their mind: a thumbs up switched to a thumbs down, or
retracted altogether. Each change used to append a row to ``llm_scores`` and
every reader averaged all of them, so a retracted thumbs up read as half a
thumbs down. The rule, in one place for every reader and both stores:

* the LATEST ``user_feedback`` row of a (trace, rater) pair is that rater's
  rating; a row without a ``user_id`` is a rater of its own;
* a latest row that is a retraction means the rater has no rating: it is left
  out, never averaged as a 0.

The rule is applied when reading. Rows the append-only chat writer left behind
carry no message identity, so the latest of them is right for one message
re-rated but not for two messages of one turn rated differently: those raters'
scores are rewritten once from their ratings (``trace_storage``).

Rows are ordered by the time of the rater's ACTION, not by when a detached
flush happened to land. Every writer reads that time the same way
(:func:`begin_score_action`): under the rater's score lock, off the database
clock, before any external call.

A retraction is spelled two ways. New ones are a NULL value that ``avg``
ignores; the PostgreSQL sink keeps it as the rater's single row, the marker a
delayed older rating is refused against. Rows written before that are a ``0`` with
:data:`LEGACY_RETRACTION_COMMENT` and NO metadata. The comment alone is free
text a rater can type, so the missing metadata is part of the test: every chat
rating now carries ``{"rating": ...}``, so no new chat row can read as a legacy
retraction. An admin REST score always carries a metadata object, which only
PostgreSQL tells apart from none: ClickHouse stores both as ``'{}'``, so there
the test also requires the version-4 id every older writer minted,
where an admin score now gets a version-7 one (:func:`new_score_id`).
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Message, MessageFeedback
from app.models.llm_trace import LLMScore

FEEDBACK_SCORE_NAME = "user_feedback"
LEGACY_RETRACTION_COMMENT = "feedback retracted by user"

_SCORE_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"
# Until the database clock has left the action's millisecond.
_SLEEP_PAST_MILLISECOND_SQL = (
    "SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM "
    "date_trunc('milliseconds', CAST(:action_at AS timestamptz)) + interval '1 millisecond' "
    "- clock_timestamp())))"
)


def score_lock_key(trace_id: uuid.UUID | str, user_id: uuid.UUID | str, name: str) -> str:
    """Advisory-lock key serializing writes of one rater's score on one trace.

    Shared by the admin REST path, the chat feedback endpoints and the tracing
    sink, so none of them races another into a duplicate or out of order.
    """
    return f"llm_score:{trace_id}:{user_id}:{name}"


async def lock_rater_score(
    db: AsyncSession, trace_id: uuid.UUID | str, user_id: uuid.UUID | str, name: str
) -> None:
    """Take the rater's score lock in the caller's transaction (see :func:`score_lock_key`)."""
    await db.execute(text(_SCORE_LOCK_SQL), {"lock_key": score_lock_key(trace_id, user_id, name)})


async def begin_score_action(
    db: AsyncSession, trace_id: uuid.UUID | str, user_id: uuid.UUID | str, name: str
) -> datetime:
    """Lock the rater's score and read the time of this action off the database clock.

    Under the lock, the later of two actions always reads the later time,
    whichever process ran it and whenever its detached flush lands.
    """
    await lock_rater_score(db, trace_id, user_id, name)
    return (await db.execute(text("SELECT clock_timestamp()"))).scalar_one()


async def end_score_action(db: AsyncSession, action_at: datetime) -> None:
    """Hold the rater's score lock until the action's millisecond is over.

    ClickHouse keeps ``created_at`` to the millisecond and would order two
    actions of one millisecond by their random ids; the next action reads its
    time once this one released the lock, so in a later millisecond.
    """
    await db.execute(text(_SLEEP_PAST_MILLISECOND_SQL), {"action_at": action_at})


def new_score_id() -> uuid.UUID:
    """The id of a score the admin REST path writes: a version-7 UUID (RFC 9562).

    Its version is what tells ClickHouse such a score apart from a legacy
    retraction of the same shape (see the module docstring). Python 3.13 has
    no ``uuid.uuid7``: the same layout is built by hand there.
    """
    make = getattr(uuid, "uuid7", None)
    if make is not None:
        return make()
    milliseconds = time.time_ns() // 1_000_000
    value = ((milliseconds & 0xFFFF_FFFF_FFFF) << 80) | int.from_bytes(os.urandom(10), "big")
    value = (value & ~(0xF << 76)) | (0x7 << 76)
    value = (value & ~(0x3 << 62)) | (0x2 << 62)
    return uuid.UUID(int=value)


async def rater_score_payload(
    db: AsyncSession,
    *,
    trace_id: uuid.UUID,
    user_id: uuid.UUID,
    created_at: datetime,
    conversation_id: uuid.UUID | None = None,
    focus_message_id: uuid.UUID | None = None,
) -> dict[str, object]:
    """The rater's one ``user_feedback`` score on a trace, from their chat ratings.

    The messages of one turn share its trace (a segment and the answer after
    it), and the stores keep one score per rater and trace: it is the
    mean of the rater's current ratings of the trace's messages, and a
    retraction only once none is left. The comment is that of
    *focus_message_id* when rated (the message just acted on), else of the
    most recently rated one. *created_at* is the time of the action.
    """
    query = (
        select(MessageFeedback.rating, MessageFeedback.comment)
        .join(Message, Message.id == MessageFeedback.message_id)
        .where(Message.llm_trace_id == trace_id, MessageFeedback.user_id == user_id)
    )
    if conversation_id is not None:
        query = query.where(Message.conversation_id == conversation_id)
    order = [MessageFeedback.updated_at.desc(), MessageFeedback.id.desc()]
    if focus_message_id is not None:
        order.insert(0, (MessageFeedback.message_id == focus_message_id).desc())
    ratings = (await db.execute(query.order_by(*order))).all()

    payload: dict[str, object] = {
        "trace_id": str(trace_id),
        "user_id": str(user_id),
        "name": FEEDBACK_SCORE_NAME,
        "source": "USER",
        "data_type": "BOOLEAN",
        # The sinks keep the rater's latest action by this time, whatever order
        # the detached flushes land in.
        "created_at": created_at,
    }
    if not ratings:
        # Not a 0: the rater has no rating any more. Both stores keep it
        # as a NULL value, which every average skips.
        payload.update(
            value=None,
            comment=LEGACY_RETRACTION_COMMENT,
            retract=True,
            metadata={"retracted": True},
        )
        return payload
    latest = ratings[0]
    payload["value"] = sum(1.0 for row in ratings if row.rating == "positive") / len(ratings)
    if latest.comment is not None:
        payload["comment"] = latest.comment
    # A marker the rater cannot type, so no rating reads as a legacy retraction.
    metadata: dict[str, object] = {"rating": latest.rating}
    if len(ratings) > 1:
        metadata["rated_messages"] = len(ratings)
    payload["metadata"] = metadata
    return payload


def feedback_per_trace_subquery():
    """One row per trace: ``trace_id`` and its ``feedback_value`` (PostgreSQL)."""
    rank = (
        func.row_number()
        .over(
            partition_by=(LLMScore.trace_id, func.coalesce(LLMScore.user_id, LLMScore.id)),
            order_by=(LLMScore.created_at.desc(), LLMScore.id.desc()),
        )
        .label("rank")
    )
    latest = (
        select(
            LLMScore.trace_id,
            LLMScore.value,
            LLMScore.comment,
            func.coalesce(func.jsonb_typeof(LLMScore.metadata_json), "null").label("metadata_type"),
            rank,
        )
        .where(LLMScore.name == FEEDBACK_SCORE_NAME)
        .subquery()
    )
    return (
        select(
            latest.c.trace_id.label("trace_id"),
            func.avg(latest.c.value).label("feedback_value"),
        )
        .where(
            latest.c.rank == 1,
            or_(
                latest.c.value.is_distinct_from(0),
                latest.c.comment.is_distinct_from(LEGACY_RETRACTION_COMMENT),
                latest.c.metadata_type != "null",
            ),
        )
        .group_by(latest.c.trace_id)
        .subquery()
    )


def _feedback_per_trace_sql_postgres(scope: str = "") -> str:
    return f"""
    SELECT trace_id, AVG(value) AS feedback_value
    FROM (
        SELECT
            trace_id,
            value,
            comment,
            COALESCE(jsonb_typeof(metadata), 'null') AS metadata_type,
            ROW_NUMBER() OVER (
                PARTITION BY trace_id, COALESCE(user_id, id)
                ORDER BY created_at DESC, id DESC
            ) AS rank
        FROM llm_scores
        WHERE name = '{FEEDBACK_SCORE_NAME}'{scope}
    ) latest
    WHERE rank = 1
      AND (
        value IS DISTINCT FROM 0
        OR comment IS DISTINCT FROM '{LEGACY_RETRACTION_COMMENT}'
        OR metadata_type <> 'null'
      )
    GROUP BY trace_id
"""  # noqa: S608 - built from module constants, not input


# The same rule as raw SQL, for the queries written as text.
FEEDBACK_PER_TRACE_SQL_POSTGRES = _feedback_per_trace_sql_postgres()

# The same rule over one day's traces, for the day scans of the rollup (they
# bind ``:day_start``, ``:day_end`` and ``:sources``): only the scores of that
# day's traces are ranked, not every feedback row once per day. A trace is kept
# or dropped whole, so each of its raters is ranked over all their rows.
_ONE_DAY_OF_TRACES = """
          AND trace_id IN (
              SELECT id
              FROM llm_traces
              WHERE started_at >= :day_start
                AND started_at < :day_end
                AND source = ANY(:sources)
          )"""
FEEDBACK_PER_TRACE_SQL_POSTGRES_ONE_DAY = _feedback_per_trace_sql_postgres(_ONE_DAY_OF_TRACES)

# ClickHouse: ``argMax`` skips NULL arguments, which would resurrect the rating
# a NULL retraction replaced, so the latest row is taken as a (non-nullable)
# tuple and unpacked. The 15th character of a UUID string is its version.
FEEDBACK_PER_TRACE_SQL_CLICKHOUSE = f"""
    SELECT trace_id, avg(latest_value) AS feedback_value
    FROM (
        SELECT
            trace_id,
            tupleElement(latest, 1) AS latest_value,
            tupleElement(latest, 2) AS latest_comment,
            tupleElement(latest, 3) AS latest_metadata,
            tupleElement(latest, 4) AS latest_id
        FROM (
            SELECT
                trace_id,
                argMax(tuple(value, ifNull(comment, ''), metadata, id), (created_at, id)) AS latest
            FROM llm_scores
            WHERE name = '{FEEDBACK_SCORE_NAME}'
            GROUP BY trace_id, ifNull(toString(user_id), toString(id))
        )
    )
    WHERE NOT (
        latest_comment = '{LEGACY_RETRACTION_COMMENT}'
        AND ifNull(latest_value, -1) = 0
        AND latest_metadata IN ('', '{{}}', 'null')
        AND substring(toString(latest_id), 15, 1) = '4'
    )
    GROUP BY trace_id
"""  # noqa: S608 - built from module constants, not input
