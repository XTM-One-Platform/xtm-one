# SPDX-License-Identifier: Apache-2.0

"""Waiting arcade API: game sessions, scores, leaderboards, profile.

Thin routes over ``services/arcade.py``, which owns the trust model (issued
sessions, plausibility checks, achievement verification) and the visibility
rules. Mounted under ``/api/v1/arcade``; every route answers 404 when the
deployment turned the arcade off (``ARCADE_ENABLED=false``).
"""

from __future__ import annotations

import logging
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.schemas.arcade import (
    ArcadeFinishRequest,
    ArcadeFinishResponse,
    ArcadeHeartbeatRequest,
    ArcadeHeartbeatResponse,
    ArcadeLeaderboardResponse,
    ArcadeProfileResponse,
    ArcadeProfileUpdate,
    ArcadeSessionStartRequest,
    ArcadeSessionStartResponse,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.models.arcade import ArcadeProfile
from app.models.user import User
from app.services import arcade

logger = logging.getLogger(__name__)


def _require_arcade_enabled() -> None:
    if not settings.arcade_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


router = APIRouter(dependencies=[Depends(_require_arcade_enabled)])


def _refuse(exc: arcade.ArcadeError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _refuse_impersonation(user: User) -> None:
    """An administrator impersonating someone plays as nobody: no run, profile
    change or live presence is recorded under the impersonated user's name."""
    if getattr(user, "impersonated_by", None) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Arcade scores are not recorded while impersonating another user.",
        )


def _live_identity(user: User, profile: ArcadeProfile | None) -> tuple[str, bool]:
    """The callsign and opt-out the player has right now."""
    callsign = (profile.callsign if profile else None) or arcade.default_callsign(
        user.full_name, user.email
    )
    return callsign, bool(profile.hidden) if profile else False


@router.get("/leaderboard", response_model=ArcadeLeaderboardResponse)
async def get_leaderboard(
    period: str = Query(arcade.DEFAULT_PERIOD, description="day, week or all"),
    limit: int = Query(
        arcade.LEADERBOARD_DEFAULT_LIMIT, ge=1, le=arcade.LEADERBOARD_MAX_LIMIT
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await arcade.leaderboard(db, user, period=period, limit=limit)
    except arcade.ArcadeError as exc:
        _refuse(exc)


@router.get("/profile", response_model=ArcadeProfileResponse)
async def get_arcade_profile(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    profile = await arcade.get_profile(db, user.id)
    stats = await arcade.player_stats(db, user.id)
    return arcade.serialize_profile(user, profile, stats)


@router.put("/profile", response_model=ArcadeProfileResponse)
async def update_arcade_profile(
    body: ArcadeProfileUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _refuse_impersonation(user)
    fields = body.model_fields_set
    try:
        profile = await arcade.update_profile(
            db,
            user,
            callsign=body.callsign,
            set_callsign="callsign" in fields,
            hidden=body.hidden if "hidden" in fields else None,
        )
    except arcade.ArcadeError as exc:
        _refuse(exc)
    await db.commit()
    if profile.hidden:
        # Opting out leaves the live room at once - after the commit, so a
        # heartbeat landing in between cannot read the old profile back in.
        await arcade.leave_live(user.id)
    stats = await arcade.player_stats(db, user.id)
    return arcade.serialize_profile(user, profile, stats)


@router.post(
    "/sessions",
    response_model=ArcadeSessionStartResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_arcade_session(
    body: ArcadeSessionStartRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ArcadeSessionStartResponse:
    _refuse_impersonation(user)
    profile = await arcade.get_profile(db, user.id)
    callsign, hidden = _live_identity(user, profile)
    try:
        session = await arcade.start_session(user.id, mode=body.mode)
    except arcade.ArcadeError as exc:
        _refuse(exc)
    ladder = await arcade.rival_ladder(db, user)
    unlocked = (profile.achievements if profile else None) or {}
    return ArcadeSessionStartResponse(
        session_id=session["session_id"],
        heartbeat_seconds=arcade.HEARTBEAT_SECONDS,
        personal_best=await arcade.personal_best(db, user.id),
        callsign=callsign,
        hidden=hidden,
        ladder=ladder["ladder"],
        leader=ladder["leader"],
        week_rank=ladder["week_rank"],
        achievements=sorted(k for k in unlocked if k in arcade.ACHIEVEMENT_IDS),
    )


@router.post("/sessions/{session_id}/heartbeat", response_model=ArcadeHeartbeatResponse)
async def arcade_heartbeat(
    session_id: str,
    body: ArcadeHeartbeatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Tell the room this player is still at it; answer with who else is.

    The callsign and the opt-out are read from the profile at every beat, not
    frozen when the game started, so opting out mid-game holds; the progress
    announced is capped at what the session's age and the waves announced
    allow. The answer says when to beat next; a beat faster than the cadence
    allows (with slack) is refused with 429 and ``Retry-After``, which the
    client takes as a missed beat.
    """
    _refuse_impersonation(user)
    window = arcade.HEARTBEAT_RATE_WINDOW_SECONDS
    await enforce_rate_limit(
        None, "arcade_heartbeat_user", limit=arcade.HEARTBEAT_USER_LIMIT, window_seconds=window, bucket=str(user.id)
    )
    try:
        session = await arcade.check_session(session_id, user.id)
    except arcade.ArcadeError as exc:
        _refuse(exc)
    await enforce_rate_limit(
        None, "arcade_heartbeat_session", limit=arcade.HEARTBEAT_SESSION_LIMIT, window_seconds=window, bucket=session_id
    )
    callsign, hidden = _live_identity(user, await arcade.get_profile(db, user.id))
    score, wave = arcade.plausible_progress(session, body.score, body.wave)
    live = await arcade.heartbeat_live(
        user.id, callsign=callsign, score=score, wave=wave, hidden=hidden
    )
    return {"live": live, "heartbeat_seconds": arcade.HEARTBEAT_SECONDS}


@router.post("/sessions/{session_id}/finish", response_model=ArcadeFinishResponse)
async def finish_arcade_session(
    session_id: str,
    body: ArcadeFinishRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record a finished run. The session is consumed first, so a refused run
    cannot be resubmitted with other numbers."""
    _refuse_impersonation(user)
    try:
        session = await arcade.consume_session(session_id, user.id)
    except arcade.ArcadeError as exc:
        _refuse(exc)
    await arcade.leave_live(user.id)
    stats = arcade.RunStats(
        score=body.score,
        wave=body.wave,
        kills=body.kills,
        shots=body.shots,
        hits=body.hits,
        ui_destroyed=body.ui_destroyed,
        bosses=body.bosses,
        max_combo=body.max_combo,
        ufo_kills=body.ufo_kills,
        duration_ms=body.duration_ms,
    )
    try:
        result = await arcade.record_run(
            db,
            user,
            session=session,
            stats=stats,
            cheat=body.cheat,
            claimed_achievements=body.achievements,
        )
    except arcade.ArcadeError as exc:
        # The reason and the run's numbers; never who played.
        logger.info("arcade: run refused: %s%s", exc.message, f" ({exc.log_detail})" if exc.log_detail else "")
        _refuse(exc)
    # Committed before answering: the client refetches the board the moment
    # this returns, and the dependency's own commit only runs after the response.
    await db.commit()
    ranks: dict[str, int | None] = {}
    if result["ranked"]:
        for period in arcade.PERIODS:
            ranks[period] = await arcade.rank_of(db, user, period)
    return {**result, "ranks": ranks}
