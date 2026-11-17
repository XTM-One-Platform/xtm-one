# SPDX-License-Identifier: Apache-2.0

"""Pydantic schemas for the waiting arcade routes.

Other players are known by their arcade callsign alone: no response carries
another user's id, name or email (``services/arcade.py`` > "Visibility").
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.services.arcade import MAX_DURATION_MS, MAX_SCORE

ArcadePeriod = Literal["day", "week", "all"]
ArcadeMode = Literal["wait", "free"]

#: Generous ceilings on every counter a run reports - the plausibility checks
#: in ``services/arcade.validate_run`` are what actually bound a run.
_COUNTER_MAX = 10_000_000
_ACHIEVEMENTS_MAX = 64
#: Longer than any id of the catalog; unknown ids are dropped anyway.
_ACHIEVEMENT_ID_MAX = 40


class ArcadeSessionStartRequest(BaseModel):
    mode: ArcadeMode = "wait"


class ArcadeRival(BaseModel):
    callsign: str
    score: int
    rank: int
    is_self: bool = False


class ArcadeSessionStartResponse(BaseModel):
    session_id: str
    heartbeat_seconds: int
    personal_best: int
    callsign: str
    hidden: bool
    #: Players right above the caller on the weekly board, closest first.
    ladder: list[ArcadeRival] = Field(default_factory=list)
    leader: ArcadeRival | None = None
    week_rank: int | None = None
    #: Achievements already unlocked, so the game only celebrates new ones.
    achievements: list[str] = Field(default_factory=list)


class ArcadeHeartbeatRequest(BaseModel):
    score: int = Field(0, ge=0, le=MAX_SCORE)
    wave: int = Field(1, ge=1, le=100_000)


class ArcadeLivePlayer(BaseModel):
    callsign: str
    score: int
    wave: int
    is_self: bool = False


class ArcadeHeartbeatResponse(BaseModel):
    live: list[ArcadeLivePlayer] = Field(default_factory=list)
    #: When to beat next, as on the session: the client follows the server.
    heartbeat_seconds: int


class ArcadeFinishRequest(BaseModel):
    score: int = Field(ge=0, le=MAX_SCORE)
    wave: int = Field(1, ge=1, le=100_000)
    kills: int = Field(0, ge=0, le=_COUNTER_MAX)
    shots: int = Field(0, ge=0, le=_COUNTER_MAX)
    hits: int = Field(0, ge=0, le=_COUNTER_MAX)
    ui_destroyed: int = Field(0, ge=0, le=_COUNTER_MAX)
    bosses: int = Field(0, ge=0, le=_COUNTER_MAX)
    max_combo: int = Field(0, ge=0, le=_COUNTER_MAX)
    ufo_kills: int = Field(0, ge=0, le=_COUNTER_MAX)
    duration_ms: int = Field(0, ge=0, le=MAX_DURATION_MS)
    #: A cheat code was used: the run is recorded, never ranked.
    cheat: bool = False
    achievements: list[Annotated[str, Field(max_length=_ACHIEVEMENT_ID_MAX)]] = Field(
        default_factory=list, max_length=_ACHIEVEMENTS_MAX
    )


class ArcadeFinishResponse(BaseModel):
    score_id: str
    ranked: bool
    personal_best: bool
    previous_best: int
    new_achievements: list[str] = Field(default_factory=list)
    #: The caller's position on each board after this run (``None`` = not on it).
    ranks: dict[str, int | None] = Field(default_factory=dict)


class ArcadeLeaderboardEntry(BaseModel):
    rank: int
    callsign: str
    best_score: int
    best_wave: int
    games: int
    kills: int
    ui_destroyed: int
    achievements: int
    is_self: bool = False


class ArcadeTitleHolder(BaseModel):
    callsign: str
    value: float
    is_self: bool = False


class ArcadeTitles(BaseModel):
    top_vandal: ArcadeTitleHolder | None = None
    sharpshooter: ArcadeTitleHolder | None = None
    marathon: ArcadeTitleHolder | None = None


class ArcadeLeaderboardResponse(BaseModel):
    period: ArcadePeriod
    entries: list[ArcadeLeaderboardEntry] = Field(default_factory=list)
    me: ArcadeLeaderboardEntry | None = None
    total_players: int = 0
    live: list[ArcadeLivePlayer] = Field(default_factory=list)
    titles: ArcadeTitles = Field(default_factory=ArcadeTitles)


class ArcadePlayerStats(BaseModel):
    games: int = 0
    best_score: int = 0
    best_wave: int = 0
    total_kills: int = 0
    total_ui_destroyed: int = 0
    total_play_ms: int = 0
    bosses: int = 0


class ArcadeProfileResponse(BaseModel):
    callsign: str | None = None
    default_callsign: str
    display_callsign: str
    hidden: bool = False
    #: {achievement id: ISO-8601 unlock time}.
    achievements: dict[str, str] = Field(default_factory=dict)
    stats: ArcadePlayerStats = Field(default_factory=ArcadePlayerStats)


class ArcadeProfileUpdate(BaseModel):
    """Every field optional; an absent field is left unchanged.

    ``callsign`` set to ``null`` or ``""`` goes back to the initials.
    """

    callsign: str | None = Field(None, max_length=16)
    hidden: bool | None = None
