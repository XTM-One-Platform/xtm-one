# SPDX-License-Identifier: Apache-2.0

"""Waiting arcade - the Space Invaders runs and the players behind them.

A user can take control of the chat's waiting game while an agent works. Every
finished run is one ``ArcadeScore`` row, written only after the server checked
it against the game session it issued (``services/arcade.py``); the cross-user
leaderboard is computed from these rows (best run per player and period), never
from counters that could drift from them.

``ArcadeProfile`` holds what belongs to the player rather than to a run: the
3-letter arcade callsign, the opt-out from the public board and the set of
unlocked achievements. A missing row means defaults (initials, visible, none).
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ArcadeScore(Base):
    """One finished run the server accepted.

    Growth is bounded by the session start rate limit
    (``services.arcade.MAX_SESSIONS_PER_HOUR`` games per player and hour) and
    nothing is pruned on purpose: the all-time board and a player's lifetime
    stats read every run, and a row is about a hundred bytes. Retention, if it
    is ever needed, must fold old runs into per-player totals first, or those
    lifetime numbers change.
    """

    __tablename__ = "arcade_scores"
    __table_args__ = (
        # The 24-hour and 7-day boards filter on the run date first.
        Index("ix_arcade_scores_created_at", "created_at"),
        # Best run per player (DISTINCT ON user_id ORDER BY score DESC,
        # created_at) read in index order, a player's best and stats, and the
        # lookup behind the users ON DELETE CASCADE.
        Index("ix_arcade_scores_user_best", "user_id", sa.text("score DESC"), "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    wave: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=sa.text("1")
    )
    kills: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    shots: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    hits: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    # Interface elements the player crumbled during the run.
    ui_destroyed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    bosses: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    max_combo: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    ufo_kills: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    # "wait" (taken over from the chat's waiting panel) or "free" (palette,
    # hidden code). Informational: both are ranked.
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="wait", server_default=sa.text("'wait'")
    )
    # False for a run played with a cheat code: kept for the player's history,
    # never on a leaderboard.
    ranked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa.text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=sa.text("now()"),
    )


class ArcadeProfile(Base):
    __tablename__ = "arcade_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # 1 to 3 characters, A-Z / 0-9, moderated. NULL = the player's initials.
    callsign: Mapped[str | None] = mapped_column(String(3), nullable=True)
    # Opt-out from what OTHER players see (leaderboards, live players, rival
    # ladders). The player still sees their own runs and rank.
    hidden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    # {achievement id: ISO-8601 unlock time}. Only ids known to the service.
    achievements: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=sa.text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=sa.text("now()"),
    )
