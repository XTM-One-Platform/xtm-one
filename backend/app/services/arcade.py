# SPDX-License-Identifier: Apache-2.0

"""Waiting arcade: game sessions, run validation, leaderboards, live players.

While an agent works, the chat's waiting panel runs a self-playing Space
Invaders; a user can take control of it and play a full game over the whole
app. This module is everything the server knows about those games.

Trust model - proportionate to an internal leaderboard
------------------------------------------------------
The game runs in the browser, so a score is a claim. The server cannot replay
it, but it can make forging one more work than playing:

* A run is only accepted against a **game session** the server issued
  (``start_session``): a random id in Redis, bound to the user, with the
  server's own start time and a TTL. A session is consumed by the run that
  finishes it - one run per session.
* The run is checked against the time the SERVER measured
  (``validate_run``): a score, a wave or a kill count the game cannot produce
  in that time is refused, and so is a run that claims to have lasted longer
  than its session. The bounds mirror the game's scoring rules
  (``frontend/src/components/arcade/engine/rules.ts``) with a wide margin: a
  strong legitimate player must never be refused.
* The score is also checked against the waves the run reached
  (``max_run_score``): the most each wave can hold by the game's own rules
  (every formation slot a golden invader, every kill at the top multiplier),
  plus what the waves do not bound, counted from the run's own numbers
  (saucers, destroyed elements) or its time (the Prompt Injector's payloads),
  plus an explicit margin. It closes the old session that licensed any score
  at any wave; it does not stop a forger who claims the waves to go with it.
* Starting sessions is rate limited per user.
* A run played with a cheat code is kept (it is the player's history) but is
  never ranked, and only unlocks the cheat achievements.
* Nothing is written while an administrator impersonates a user (enforced by
  the route).

Achievements are split the same way: the ones the run's own numbers prove are
re-checked here (``STAT_ACHIEVEMENTS``); the ones tied to an in-game event the
server cannot observe (a secret code, the saucer's legendary shot, destroying
the logo...) are accepted from a closed list (``EVENT_ACHIEVEMENTS``) - they
are badges, not points.

Visibility
----------
Other players are known by their arcade callsign alone: no response carries
another user's id, name or email, and neither does anything stored in Redis
(the live room is keyed by user id server-side only). A player who opted out
(``ArcadeProfile.hidden``) disappears from what OTHER players see - boards,
titles, rival ladders, live players - and still sees their own rank, computed
among the visible players plus themselves. The live room reads the profile at
every heartbeat, so opting out (or a new callsign) holds mid-game. Deactivated
users are never listed.

Redis
-----
Sessions, the start rate limit and the live-player room live in Redis: they are
ephemeral by nature. A Redis outage makes the arcade refuse to START a ranked
game (the client then plays offline and says so) rather than accept runs it
cannot check; the live room fails open (empty).
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import INCR_EXPIRE_LUA
from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.models.arcade import ArcadeProfile, ArcadeScore
from app.models.user import User

logger = logging.getLogger(__name__)

# ── Game sessions ────────────────────────────────────────────────────────────

#: A session outlives any sane game (the waiting panel shows up for long agent
#: turns, a free game can go on) but not a forgotten tab.
SESSION_TTL_SECONDS = 3 * 60 * 60
SESSION_KEY_PREFIX = "arcade:session:"
#: Games a user may start per hour (a fixed window from the first start).
MAX_SESSIONS_PER_HOUR = 60
RATE_WINDOW_SECONDS = 3600
RATE_KEY_PREFIX = "arcade:rate:"
SESSION_MODES: tuple[str, ...] = ("wait", "free")

# ── Live players ─────────────────────────────────────────────────────────────

LIVE_KEY = "arcade:live"
#: How often a playing client heartbeats - the one source of the cadence: sent
#: with the session a game starts on and with every heartbeat answer. The
#: client falls back to the same value when the server says nothing
#: (``DEFAULT_HEARTBEAT_SECONDS``, ``frontend/src/components/arcade/heartbeat.ts``).
HEARTBEAT_SECONDS = 10
#: An entry older than this is a closed tab: three missed beats and slack.
LIVE_TTL_SECONDS = 3 * HEARTBEAT_SECONDS + 5
#: The announced cadence is enforced per fixed window (``core/rate_limit.py``),
#: three times over: a client never beats faster than every 5 s (13 beats in a
#: window at most), and a tab waking from background throttling fires late,
#: never early. A beat over the limit is a missed beat (429 + ``Retry-After``).
HEARTBEAT_RATE_WINDOW_SECONDS = 60
HEARTBEAT_SESSION_LIMIT = 3 * HEARTBEAT_RATE_WINDOW_SECONDS // HEARTBEAT_SECONDS
#: Two games of one player at once, each at the fastest client pace.
HEARTBEAT_USER_LIMIT = 2 * HEARTBEAT_SESSION_LIMIT

# ── Plausibility (mirrors engine/rules.ts, with margin) ──────────────────────

#: Sustained scoring rate no legitimate run exceeds: a full wave at the top
#: multiplier plus its clear bonus and a saucer, cleared unrealistically fast.
MAX_POINTS_PER_SECOND = 1500
#: Head start covering short runs that end on a boss or a saucer.
SCORE_ALLOWANCE = 10_000
MAX_SCORE = 50_000_000
#: No wave falls in less than this, even with every power-up.
MIN_SECONDS_PER_WAVE = 5
#: A wave holds 55 invaders; bosses, minions and decoys add a few.
MAX_KILLS_PER_WAVE = 80
#: Network latency and clock granularity between the client's timer and ours.
DURATION_SLACK_SECONDS = 15
MAX_DURATION_MS = SESSION_TTL_SECONDS * 1000
#: The saucer crosses at most every ~12 seconds.
MIN_SECONDS_PER_UFO = 8
#: The cannon's fastest cadence - rapid fire with the three-shot spread, about
#: 27 shots a second - with margin.
MAX_SHOTS_PER_SECOND = 30
SHOT_ALLOWANCE = 30
#: The most destroyed elements a game counts (``MAX_SCORED_ELEMENTS``,
#: engine/rules.ts). A game is paid for the damage to the first that many
#: elements it tracks (``DamageResult.scored``, engine/screen-damage.ts) and its
#: count stops at the cap: past them - recycled room, a nuke on a busy page - the
#: interface still crumbles, for show, so a real run never exceeds it.
MAX_UI_DESTROYED_PER_RUN = 320

# ── Score per wave (mirrors engine/rules.ts and engine/game.ts) ──────────────
# Every place the game adds to the score, bounded by what bounds it: the waves
# (fixed enemies), the run's own counters, its time, or once per game.
# ``tests/test_arcade.py`` recomputes all of it from the game's sources.

#: A formation wave is 5 rows of 11 invaders, built once.
FORMATION_SIZE = 55
#: The golden invader, the best of them: the worst case fills every slot with it.
MAX_INVADER_POINTS = 100
#: The combo multiplier's ceiling: the worst case scores every kill at it.
MAX_MULTIPLIER = 4
#: Every fifth wave is a boss instead of a formation.
BOSS_WAVE_EVERY = 5
WAVE_CLEAR_BONUS_BASE = 500
WAVE_CLEAR_BONUS_STEP = 150
WAVE_CLEAR_BONUS_MAX = 2000
BOSS_POINTS_BASE = 2500
BOSS_POINTS_STEP = 250
BOSS_POINTS_MAX = 5000
#: The Token Overflow splits twice into three minions; no other boss has any.
MAX_MINIONS_PER_BOSS = 6
MINION_POINTS = 40
#: The best a saucer pays: the rubber duck (the legendary shot pays 300, Pi Day 314).
MAX_UFO_POINTS = 500
#: A crumbled element (a nuke's pays 10).
UI_DESTROYED_POINTS = 25
#: A hit that does not crumble its element scores a crater. An element has at
#: most 9 hit points and the gentlest hit (a shot) takes 0.34, so it takes at
#: most 27 hits, and a game is paid for the damage to at most
#: MAX_UI_DESTROYED_PER_RUN elements (``DamageResult.scored``, engine/screen-damage.ts).
UI_CRATER_POINTS = 2
CRATER_HITS_PER_ELEMENT = 27
MAX_CRATER_POINTS = MAX_UI_DESTROYED_PER_RUN * CRATER_HITS_PER_ELEMENT * UI_CRATER_POINTS
#: The Prompt Injector drops a payload every few seconds for as long as it
#: lives: the one source of points on a wave that time bounds, not the wave.
PAYLOAD_POINTS = 50
PAYLOAD_INTERVAL_SECONDS = 6
#: The first Prompt Injector, third boss of the rotation.
FIRST_PAYLOAD_WAVE = 15
#: Once per game: the agent's answer arrived with you alive; thirty seconds without firing.
SURVIVAL_BONUS_MAX = 3000
PACIFIST_BONUS = 500
#: Explicit headroom over the rules' worst case, which no real run comes near
#: already: a run the game can produce must never be refused.
SCORE_CEILING_MARGIN = 1.25

# ── Leaderboards ─────────────────────────────────────────────────────────────

PERIODS: dict[str, timedelta | None] = {
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "all": None,
}
DEFAULT_PERIOD = "week"
LEADERBOARD_DEFAULT_LIMIT = 20
LEADERBOARD_MAX_LIMIT = 50
#: Rivals handed to a starting game: the players right above you this week.
LADDER_SIZE = 5
#: The sharpshooter title needs a real sample, not three lucky shots.
MIN_SHOTS_FOR_ACCURACY_TITLE = 100


class ArcadeError(Exception):
    """A refusal the route relays verbatim, with its HTTP status.

    ``log_detail`` is for the server log only: the numbers behind the refusal,
    never who played.
    """

    def __init__(self, message: str, status_code: int = 422, *, log_detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.log_detail = log_detail


# ── Runs ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunStats:
    """What a finished game claims, as the finish payload carries it."""

    score: int
    wave: int
    kills: int
    shots: int
    hits: int
    ui_destroyed: int
    bosses: int
    max_combo: int
    ufo_kills: int
    duration_ms: int

    @property
    def accuracy(self) -> float:
        return self.hits / self.shots if self.shots else 0.0


def max_plausible_score(seconds: float) -> int:
    return int(SCORE_ALLOWANCE + MAX_POINTS_PER_SECOND * max(0.0, seconds))


def max_plausible_wave(seconds: float) -> int:
    return 1 + int(max(0.0, seconds) // MIN_SECONDS_PER_WAVE)


def max_plausible_ufo_kills(seconds: float) -> int:
    return 1 + int(max(0.0, seconds) // MIN_SECONDS_PER_UFO)


def wave_clear_bonus(wave: int) -> int:
    return min(WAVE_CLEAR_BONUS_MAX, WAVE_CLEAR_BONUS_BASE + WAVE_CLEAR_BONUS_STEP * max(0, wave - 1))


def boss_points(wave: int) -> int:
    nth = max(1, wave // BOSS_WAVE_EVERY)
    return min(BOSS_POINTS_MAX, BOSS_POINTS_BASE + BOSS_POINTS_STEP * (nth - 1))


def max_wave_score(wave: int) -> int:
    """The most points one wave can hold: every formation slot a golden invader
    killed at the top multiplier, or the boss plus every minion it can spawn at
    the top multiplier - and the clear bonus either way: a boss that finishes
    dying while the cannon is down leaves its wave to the formation's clear."""
    if wave % BOSS_WAVE_EVERY == 0:
        enemies = boss_points(wave) + MAX_MINIONS_PER_BOSS * MINION_POINTS * MAX_MULTIPLIER
    else:
        enemies = FORMATION_SIZE * MAX_INVADER_POINTS * MAX_MULTIPLIER
    return enemies + wave_clear_bonus(wave)


def max_score_for_waves(wave: int) -> int:
    """The per-wave maxima of every wave a run reached, the first to *wave*.

    Linear in *wave*: callers bound it first (``max_plausible_wave``), which
    keeps it to a few thousand waves for the longest session.
    """
    return sum(max_wave_score(w) for w in range(1, max(1, wave) + 1))


def max_run_score(wave: int, *, ufo_kills: int, ui_destroyed: int, seconds: float) -> int:
    """The most a run that reached *wave* can score, margin included.

    Its waves, plus what the waves do not bound: the saucers and the crumbled
    elements from the run's own counters (both bounded by ``validate_run``),
    every crater a game can score, the Prompt Injector's payloads over the
    time played once one is met, and the two once-per-game bonuses.
    """
    payloads = 0.0
    if wave >= FIRST_PAYLOAD_WAVE:
        # One early payload per boss wave, then one per interval of play.
        payloads = PAYLOAD_POINTS * (wave // BOSS_WAVE_EVERY + max(0.0, seconds) / PAYLOAD_INTERVAL_SECONDS)
    rules = (
        max_score_for_waves(wave)
        + max(0, ufo_kills) * MAX_UFO_POINTS
        + max(0, ui_destroyed) * UI_DESTROYED_POINTS
        + MAX_CRATER_POINTS
        + payloads
        + SURVIVAL_BONUS_MAX
        + PACIFIST_BONUS
    )
    return int(SCORE_CEILING_MARGIN * rules)


def validate_run(stats: RunStats, *, server_elapsed_seconds: float) -> None:
    """Refuse a run the game cannot have produced in the time the server measured.

    Pure: the numbers of the run and the session's age are all it needs. The
    client's own duration is play time (pauses excluded), so it can only be
    shorter than the session; it is the stricter of the two and the rate is
    measured against it.
    """
    client_seconds = stats.duration_ms / 1000
    if client_seconds > server_elapsed_seconds + DURATION_SLACK_SECONDS:
        raise ArcadeError("This run lasted longer than its game session.")
    # The slack forgives a client clock running a little ahead; it is never play time.
    seconds = min(client_seconds, server_elapsed_seconds)
    if stats.score > MAX_SCORE:
        raise ArcadeError("This score is off the charts.")
    if stats.score > max_plausible_score(seconds):
        raise ArcadeError("This score is not plausible for the time played.")
    if stats.wave > max_plausible_wave(seconds):
        raise ArcadeError("This wave is not reachable in the time played.")
    # Every counter a public title reads is bounded too, not only the score.
    if stats.shots > SHOT_ALLOWANCE + MAX_SHOTS_PER_SECOND * seconds:
        raise ArcadeError("More shots than the cannon fires in the time played.")
    if stats.ui_destroyed > MAX_UI_DESTROYED_PER_RUN:
        raise ArcadeError("More of the interface destroyed than one game can reach.")
    if stats.hits > stats.shots:
        raise ArcadeError("A run cannot hit more often than it shot.")
    if stats.max_combo > stats.hits:
        raise ArcadeError("A combo cannot be longer than the hits it counts.")
    if stats.kills > MAX_KILLS_PER_WAVE * stats.wave:
        raise ArcadeError("Too many invaders for the waves reached.")
    if stats.bosses > stats.wave // BOSS_WAVE_EVERY:
        raise ArcadeError("Bosses only appear every fifth wave.")
    if stats.ufo_kills > max_plausible_ufo_kills(seconds):
        raise ArcadeError("The saucer does not come by that often.")
    # Last: the counters it reads are bounded above, the wave keeps the sum short.
    ceiling = max_run_score(stats.wave, ufo_kills=stats.ufo_kills, ui_destroyed=stats.ui_destroyed, seconds=seconds)
    if stats.score > ceiling:
        raise ArcadeError(
            f"This score is more than the game can yield by wave {stats.wave}.",
            log_detail=(
                f"score {stats.score} over the ceiling {ceiling} of wave {stats.wave} "
                f"with {stats.ufo_kills} saucers and {stats.ui_destroyed} elements in {seconds:.0f} s"
            ),
        )


def plausible_progress(session: dict[str, Any], score: int, wave: int) -> tuple[int, int]:
    """What a heartbeat may announce to the room: no more than the session's age
    allows, and no more points than the waves announced can hold."""
    elapsed = max(0.0, _now() - float(session.get("started_at") or 0))
    wave = min(wave, max_plausible_wave(elapsed))
    # A beat carries no counters: they count at the most the session's age allows.
    ceiling = max_run_score(
        wave,
        ufo_kills=max_plausible_ufo_kills(elapsed),
        ui_destroyed=MAX_UI_DESTROYED_PER_RUN,
        seconds=elapsed,
    )
    return min(score, max_plausible_score(elapsed), ceiling), wave


# ── Achievements ─────────────────────────────────────────────────────────────

#: Unlocked by the run's own numbers - re-checked here, whatever the client says.
#: Keep the ids in step with ``engine/achievements.ts`` (a test compares them).
STAT_ACHIEVEMENTS: dict[str, Callable[[RunStats], bool]] = {
    "first_blood": lambda s: s.kills >= 1,
    "centurion": lambda s: s.kills >= 100,
    "wave_5": lambda s: s.wave >= 5,
    "wave_10": lambda s: s.wave >= 10,
    "score_10k": lambda s: s.score >= 10_000,
    "score_50k": lambda s: s.score >= 50_000,
    "score_100k": lambda s: s.score >= 100_000,
    "sharpshooter": lambda s: s.shots >= 30 and s.accuracy >= 0.9,
    "combo_master": lambda s: s.max_combo >= 40,
    "boss_slayer": lambda s: s.bosses >= 1,
    "ufo_hunter": lambda s: s.ufo_kills >= 5,
    "wrecking_ball": lambda s: s.ui_destroyed >= 25,
    "total_annihilation": lambda s: s.ui_destroyed >= 100,
    "marathon": lambda s: s.duration_ms >= 10 * 60 * 1000,
}

#: Unlocked by an in-game event the server cannot observe. Closed list.
EVENT_ACHIEVEMENTS: frozenset[str] = frozenset(
    {
        # Secret codes.
        "konami_kid",
        "rainbow_road",
        "god_complex",
        "arsenal",
        "deep_thought",
        "hello_there",
        "adventurer",
        "permission_denied",
        # Saucers.
        "lucky_23",
        "rubber_duck",
        "pi_hunter",
        # Destroying the interface.
        "self_sabotage",
        "shoot_the_messenger",
        "brand_vandal",
        "radio_silence",
        "navigator_down",
        "nuked",
        "sudo",
        # Play style.
        "pacifist",
        "last_stand",
        "speed_demon",
        "untouchable",
        # Bosses.
        "hallucination_buster",
        "rate_limit_exceeded",
        "injection_proof",
        "overflow_handled",
        # Waiting for the agent.
        "patience",
        "overtime",
        "mission_complete",
        # When you play.
        "night_owl",
        "early_bird",
        "friday_deploy",
        "trick_or_treat",
        "ho_ho_ho",
        "april_fool",
    }
)

#: The only achievements a cheat run unlocks: the fun ones, never the skilled.
CHEAT_ACHIEVEMENTS: frozenset[str] = frozenset(
    {
        "konami_kid",
        "rainbow_road",
        "god_complex",
        "arsenal",
        "deep_thought",
        "hello_there",
        "adventurer",
        "permission_denied",
    }
)

ACHIEVEMENT_IDS: frozenset[str] = frozenset(STAT_ACHIEVEMENTS) | EVENT_ACHIEVEMENTS


def filter_achievements(claimed: Iterable[str], stats: RunStats, *, cheat: bool) -> list[str]:
    """The claimed achievements this run actually earned, deduplicated, in order."""
    earned: list[str] = []
    for achievement in claimed:
        if achievement in earned:
            continue
        if cheat and achievement not in CHEAT_ACHIEVEMENTS:
            continue
        check = STAT_ACHIEVEMENTS.get(achievement)
        if check is not None:
            if check(stats):
                earned.append(achievement)
        elif achievement in EVENT_ACHIEVEMENTS:
            earned.append(achievement)
    return earned


# ── Callsigns ────────────────────────────────────────────────────────────────

_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{1,3}$")
#: The classic arcade problem: three letters are enough to spell a slur. Kept
#: short and obvious on purpose - moderation, not a dictionary.
_BLOCKED_CALLSIGNS: frozenset[str] = frozenset(
    {
        "ASS", "CUM", "DIC", "DIK", "FAG", "FCK", "FKU", "FUC", "FUK", "KKK",
        "KYS", "NAZ", "NIG", "PD", "SEX", "SS", "TIT", "VAG",
    }
)
#: Digits read as the letters they stand in for ("A55" is "ASS").
_LOOKALIKE_DIGITS = str.maketrans("0134578", "OIEASTB")
FALLBACK_CALLSIGN = "XTM"


def _is_blocked(callsign: str) -> bool:
    return callsign in _BLOCKED_CALLSIGNS or callsign.translate(_LOOKALIKE_DIGITS) in _BLOCKED_CALLSIGNS


def normalize_callsign(raw: str | None) -> str | None:
    """Validate a callsign the player typed. ``None`` / blank = use the initials."""
    if raw is None:
        return None
    value = raw.strip().upper()
    if not value:
        return None
    if not _CALLSIGN_RE.match(value):
        raise ArcadeError("A callsign is 1 to 3 letters or digits (A-Z, 0-9).")
    if _is_blocked(value):
        raise ArcadeError("That callsign is not available. Pick another one.")
    return value


def _ascii_words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return [w for w in re.split(r"[^A-Za-z0-9]+", folded) if w]


def default_callsign(full_name: str | None, email: str | None = None) -> str:
    """Three letters from the player's name, the way arcade initials read.

    "Ada Lovelace" -> "ALO", "Grace Brewster Hopper" -> "GBH", "Linus" -> "LIN".
    Falls back to the email's local part, then to ``FALLBACK_CALLSIGN``, and
    never lands on a blocked callsign.
    """
    candidates: list[str] = []
    words = _ascii_words(full_name or "")
    if len(words) >= 3:
        candidates.append("".join(w[0] for w in words[:3]))
    if len(words) == 2:
        candidates.append(words[0][0] + words[1][:2])
    if words:
        candidates.append(words[0][:3])
    local = (email or "").split("@", 1)[0]
    email_words = _ascii_words(local)
    if email_words:
        candidates.append("".join(email_words)[:3])
    for candidate in candidates:
        value = candidate.upper()
        if _CALLSIGN_RE.match(value) and not _is_blocked(value):
            return value
    return FALLBACK_CALLSIGN


def _display_callsign(
    profile_callsign: str | None, full_name: str | None, email: str | None
) -> str:
    return profile_callsign or default_callsign(full_name, email)


# ── Redis: sessions, rate limit, live room ───────────────────────────────────


def session_key(session_id: str) -> str:
    return f"{SESSION_KEY_PREFIX}{session_id}"


def _now() -> float:
    return time.time()


async def start_session(user_id: uuid.UUID | str, *, mode: str) -> dict[str, Any]:
    """Issue a single-use game session bound to *user_id*.

    The session holds what the run is checked against - who, when, which mode -
    and nothing about the player: the live room reads the profile at each
    heartbeat instead, so a mid-game opt-out or callsign change holds.
    """
    if mode not in SESSION_MODES:
        raise ArcadeError(f"Unknown game mode {mode!r}.")
    session_id = uuid.uuid4().hex
    payload = {
        "user_id": str(user_id),
        "started_at": _now(),
        "mode": mode,
    }
    try:
        redis = await redis_client()
        rate_key = f"{RATE_KEY_PREFIX}{user_id}"
        # One atomic round trip, which also re-arms a counter that lost its
        # expiry - it would otherwise lock the player out for good.
        started = int(await redis.eval(INCR_EXPIRE_LUA, 1, rate_key, RATE_WINDOW_SECONDS))
        if started > MAX_SESSIONS_PER_HOUR:
            raise ArcadeError(
                "That is a lot of games for one hour. The invaders need a break too.",
                status_code=429,
            )
        await redis.set(session_key(session_id), json.dumps(payload), ex=SESSION_TTL_SECONDS)
    except ArcadeError:
        raise
    except Exception as exc:
        logger.warning("arcade: could not start a session: %s", format_exception_message(exc))
        raise ArcadeError(
            "The arcade cannot record scores right now.", status_code=503
        ) from exc
    return {"session_id": session_id, **payload}


_UNKNOWN_SESSION = "This game session has expired."


async def _read_session(session_id: str, user_id: uuid.UUID | str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{32}", session_id or ""):
        raise ArcadeError(_UNKNOWN_SESSION, status_code=404)
    try:
        redis = await redis_client()
        raw = await redis.get(session_key(session_id))
    except Exception as exc:
        logger.warning("arcade: could not read a session: %s", format_exception_message(exc))
        raise ArcadeError("The arcade cannot record scores right now.", status_code=503) from exc
    # Someone else's session reads exactly like a missing one: no oracle.
    if not raw:
        raise ArcadeError(_UNKNOWN_SESSION, status_code=404)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ArcadeError(_UNKNOWN_SESSION, status_code=404) from exc
    if not isinstance(payload, dict) or payload.get("user_id") != str(user_id):
        raise ArcadeError(_UNKNOWN_SESSION, status_code=404)
    return payload


async def check_session(session_id: str, user_id: uuid.UUID | str) -> dict[str, Any]:
    """The session's payload if it is live and belongs to *user_id*."""
    return await _read_session(session_id, user_id)


async def consume_session(session_id: str, user_id: uuid.UUID | str) -> dict[str, Any]:
    """Take the session for the run that finishes it. A second finish is refused.

    Ownership is checked BEFORE the delete, so nobody can burn another
    player's session; the delete's count decides which of two concurrent
    finishes wins.
    """
    payload = await _read_session(session_id, user_id)
    try:
        redis = await redis_client()
        deleted = int(await redis.delete(session_key(session_id)))
    except Exception as exc:
        logger.warning("arcade: could not consume a session: %s", format_exception_message(exc))
        raise ArcadeError("The arcade cannot record scores right now.", status_code=503) from exc
    if deleted != 1:
        raise ArcadeError("This game was already recorded.", status_code=409)
    return payload


def _decode_live(raw: dict[str, str] | None, *, viewer_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Decode the live room: the visible players, and the stale fields to prune."""
    cutoff = _now() - LIVE_TTL_SECONDS
    players: list[tuple[str, dict[str, Any]]] = []
    stale: list[str] = []
    for field, value in (raw or {}).items():
        try:
            data = json.loads(value)
        except (TypeError, ValueError):
            stale.append(field)
            continue
        ts = data.get("ts") if isinstance(data, dict) else None
        if not isinstance(ts, int | float) or ts < cutoff:
            stale.append(field)
            continue
        is_self = field == viewer_id
        if data.get("hidden") and not is_self:
            continue
        players.append(
            (
                field,
                {
                    "callsign": data.get("callsign") or FALLBACK_CALLSIGN,
                    "score": int(data.get("score") or 0),
                    "wave": int(data.get("wave") or 1),
                    "is_self": is_self,
                },
            )
        )
    # The user id only breaks ties so the order is stable; it never leaves.
    players.sort(key=lambda item: (-item[1]["score"], item[1]["callsign"], item[0]))
    return [player for _field, player in players], stale


async def list_live(viewer_id: uuid.UUID | str) -> list[dict[str, Any]]:
    """Who is playing right now (fail-open: an empty room on a Redis outage)."""
    try:
        redis = await redis_client()
        raw = await redis.hgetall(LIVE_KEY)
    except Exception as exc:
        logger.debug("arcade: live room unavailable: %s", format_exception_message(exc))
        return []
    players, stale = _decode_live(raw, viewer_id=str(viewer_id))
    if stale:
        try:
            await redis.hdel(LIVE_KEY, *stale)
        except Exception:
            logger.debug("arcade: could not prune the live room", exc_info=True)
    return players


async def heartbeat_live(
    user_id: uuid.UUID | str,
    *,
    callsign: str,
    score: int,
    wave: int,
    hidden: bool,
) -> list[dict[str, Any]]:
    """Record that *user_id* is playing and return the room (fail-open)."""
    entry = {
        "callsign": callsign,
        "score": max(0, int(score)),
        "wave": max(1, int(wave)),
        "hidden": bool(hidden),
        "ts": _now(),
    }
    try:
        redis = await redis_client()
        await redis.hset(LIVE_KEY, str(user_id), json.dumps(entry))
        await redis.expire(LIVE_KEY, LIVE_TTL_SECONDS * 2)
    except Exception as exc:
        logger.debug("arcade: live heartbeat skipped: %s", format_exception_message(exc))
        return []
    return await list_live(user_id)


async def leave_live(user_id: uuid.UUID | str) -> None:
    try:
        redis = await redis_client()
        await redis.hdel(LIVE_KEY, str(user_id))
    except Exception as exc:
        logger.debug("arcade: live leave skipped: %s", format_exception_message(exc))


# ── Profiles ─────────────────────────────────────────────────────────────────


async def get_profile(db: AsyncSession, user_id: uuid.UUID) -> ArcadeProfile | None:
    return await db.get(ArcadeProfile, user_id)


async def _profile_for_update(db: AsyncSession, user_id: uuid.UUID) -> ArcadeProfile:
    """The player's profile row, created if missing, locked until commit.

    ``ON CONFLICT DO NOTHING`` then ``SELECT ... FOR UPDATE``: two runs of the
    same player finishing at once serialize on the row instead of one of them
    losing its achievements to the other's write.
    """
    await db.execute(
        pg_insert(ArcadeProfile)
        .values(user_id=user_id, hidden=False)
        .on_conflict_do_nothing(index_elements=[ArcadeProfile.user_id])
    )
    result = await db.execute(
        select(ArcadeProfile).where(ArcadeProfile.user_id == user_id).with_for_update()
    )
    return result.scalar_one()


async def update_profile(
    db: AsyncSession,
    user: User,
    *,
    callsign: str | None = None,
    set_callsign: bool = False,
    hidden: bool | None = None,
) -> ArcadeProfile:
    profile = await _profile_for_update(db, user.id)
    if set_callsign:
        profile.callsign = normalize_callsign(callsign)
    if hidden is not None:
        profile.hidden = bool(hidden)
    profile.updated_at = datetime.now(UTC)
    await db.flush()
    return profile


async def player_stats(db: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    """Lifetime numbers over the player's ranked runs."""
    row = (
        await db.execute(
            select(
                func.count(ArcadeScore.id),
                func.coalesce(func.max(ArcadeScore.score), 0),
                func.coalesce(func.max(ArcadeScore.wave), 0),
                func.coalesce(func.sum(ArcadeScore.kills), 0),
                func.coalesce(func.sum(ArcadeScore.ui_destroyed), 0),
                func.coalesce(func.sum(ArcadeScore.duration_ms), 0),
                func.coalesce(func.sum(ArcadeScore.bosses), 0),
            ).where(ArcadeScore.user_id == user_id, ArcadeScore.ranked.is_(True))
        )
    ).one()
    games, best_score, best_wave, kills, ui_destroyed, play_ms, bosses = row
    return {
        "games": int(games or 0),
        "best_score": int(best_score or 0),
        "best_wave": int(best_wave or 0),
        "total_kills": int(kills or 0),
        "total_ui_destroyed": int(ui_destroyed or 0),
        "total_play_ms": int(play_ms or 0),
        "bosses": int(bosses or 0),
    }


def serialize_profile(
    user: User, profile: ArcadeProfile | None, stats: dict[str, int]
) -> dict[str, Any]:
    achievements = dict((profile.achievements if profile else None) or {})
    callsign = profile.callsign if profile else None
    return {
        "callsign": callsign,
        "default_callsign": default_callsign(user.full_name, user.email),
        "display_callsign": _display_callsign(callsign, user.full_name, user.email),
        "hidden": bool(profile.hidden) if profile else False,
        "achievements": {k: v for k, v in achievements.items() if k in ACHIEVEMENT_IDS},
        "stats": stats,
    }


# ── Leaderboards ─────────────────────────────────────────────────────────────


def normalize_period(period: str | None) -> str:
    value = (period or DEFAULT_PERIOD).strip().lower()
    if value not in PERIODS:
        raise ArcadeError(f"Unknown period {period!r}: use day, week or all.")
    return value


def period_start(period: str, now: datetime | None = None) -> datetime | None:
    window = PERIODS[period]
    if window is None:
        return None
    return (now or datetime.now(UTC)) - window


def _visible(viewer_id: uuid.UUID, user_id_column: Any) -> Any:
    """Active players who did not opt out - plus the viewer, always."""
    return and_(
        User.is_active.isnot(False),
        or_(
            ArcadeProfile.hidden.is_(None),
            ArcadeProfile.hidden.is_(False),
            user_id_column == viewer_id,
        ),
    )


def _ranked_board(viewer_id: uuid.UUID, since: datetime | None) -> Any:
    """One row per visible player: best run in the period, aggregates, rank.

    The best run is ``DISTINCT ON (user_id) ... ORDER BY score DESC,
    created_at ASC``: on a tie the earlier run wins, like an arcade cabinet,
    and the rank uses the same order so two players never share a position.
    ``ix_arcade_scores_user_best`` holds exactly that order. A player who
    scored nothing in the period is not on the board. ``total`` counts the
    board's players, so one evaluation serves the top, the viewer and the count.
    """
    best_query = (
        select(
            ArcadeScore.user_id.label("user_id"),
            ArcadeScore.score.label("score"),
            ArcadeScore.created_at.label("achieved_at"),
        )
        .where(ArcadeScore.ranked.is_(True), ArcadeScore.score > 0)
        .distinct(ArcadeScore.user_id)
        .order_by(ArcadeScore.user_id, ArcadeScore.score.desc(), ArcadeScore.created_at.asc())
    )
    agg_query = select(
        ArcadeScore.user_id.label("user_id"),
        func.count(ArcadeScore.id).label("games"),
        func.coalesce(func.sum(ArcadeScore.kills), 0).label("kills"),
        func.coalesce(func.sum(ArcadeScore.ui_destroyed), 0).label("ui_destroyed"),
        func.max(ArcadeScore.wave).label("best_wave"),
    ).where(ArcadeScore.ranked.is_(True))
    if since is not None:
        best_query = best_query.where(ArcadeScore.created_at >= since)
        agg_query = agg_query.where(ArcadeScore.created_at >= since)
    best = best_query.subquery("best")
    agg = agg_query.group_by(ArcadeScore.user_id).subquery("agg")
    return (
        select(
            best.c.user_id,
            best.c.score,
            best.c.achieved_at,
            agg.c.games,
            agg.c.kills,
            agg.c.ui_destroyed,
            agg.c.best_wave,
            User.full_name,
            User.email,
            ArcadeProfile.callsign,
            ArcadeProfile.achievements,
            func.rank()
            .over(order_by=(best.c.score.desc(), best.c.achieved_at.asc(), best.c.user_id))
            .label("rank"),
            func.count().over().label("total"),
        )
        .select_from(best)
        .join(agg, agg.c.user_id == best.c.user_id)
        .join(User, User.id == best.c.user_id)
        .outerjoin(ArcadeProfile, ArcadeProfile.user_id == best.c.user_id)
        .where(_visible(viewer_id, best.c.user_id))
        .subquery("ranked")
    )


def _entry(row: Any, viewer_id: uuid.UUID) -> dict[str, Any]:
    achievements = row.achievements if isinstance(row.achievements, dict) else {}
    return {
        "rank": int(row.rank),
        "callsign": _display_callsign(row.callsign, row.full_name, row.email),
        "best_score": int(row.score),
        "best_wave": int(row.best_wave or 1),
        "games": int(row.games or 0),
        "kills": int(row.kills or 0),
        "ui_destroyed": int(row.ui_destroyed or 0),
        "achievements": sum(1 for key in achievements if key in ACHIEVEMENT_IDS),
        "is_self": row.user_id == viewer_id,
    }


async def _title(
    db: AsyncSession,
    viewer_id: uuid.UUID,
    since: datetime | None,
    value: Any,
    *,
    having: Any = None,
) -> dict[str, Any] | None:
    """The visible player with the highest *value* aggregate in the period."""
    query = (
        select(
            ArcadeScore.user_id,
            User.full_name,
            User.email,
            ArcadeProfile.callsign,
            value.label("value"),
        )
        .join(User, User.id == ArcadeScore.user_id)
        .outerjoin(ArcadeProfile, ArcadeProfile.user_id == ArcadeScore.user_id)
        # Like the board: a zero-point run holds nothing.
        .where(
            ArcadeScore.ranked.is_(True),
            ArcadeScore.score > 0,
            _visible(viewer_id, ArcadeScore.user_id),
        )
        .group_by(ArcadeScore.user_id, User.full_name, User.email, ArcadeProfile.callsign)
        .order_by(value.desc(), ArcadeScore.user_id)
        .limit(1)
    )
    if since is not None:
        query = query.where(ArcadeScore.created_at >= since)
    if having is not None:
        query = query.having(having)
    row = (await db.execute(query)).first()
    if row is None or row.value is None or float(row.value) <= 0:
        return None
    return {
        "callsign": _display_callsign(row.callsign, row.full_name, row.email),
        "value": float(row.value),
        "is_self": row.user_id == viewer_id,
    }


async def leaderboard(
    db: AsyncSession,
    viewer: User,
    *,
    period: str = DEFAULT_PERIOD,
    limit: int = LEADERBOARD_DEFAULT_LIMIT,
) -> dict[str, Any]:
    period = normalize_period(period)
    limit = max(1, min(int(limit), LEADERBOARD_MAX_LIMIT))
    since = period_start(period)
    ranked = _ranked_board(viewer.id, since)

    # One pass: the top of the board plus the viewer's own row, wherever it is.
    rows = (
        await db.execute(
            select(ranked)
            .where(or_(ranked.c.rank <= limit, ranked.c.user_id == viewer.id))
            .order_by(ranked.c.rank)
        )
    ).all()
    entries = [_entry(row, viewer.id) for row in rows if row.rank <= limit]
    me_row = next((row for row in rows if row.user_id == viewer.id), None)
    total = int(rows[0].total) if rows else 0

    accuracy = func.sum(ArcadeScore.hits) * 1.0 / func.nullif(func.sum(ArcadeScore.shots), 0)
    titles = {
        "top_vandal": await _title(db, viewer.id, since, func.sum(ArcadeScore.ui_destroyed)),
        "sharpshooter": await _title(
            db,
            viewer.id,
            since,
            accuracy,
            having=func.sum(ArcadeScore.shots) >= MIN_SHOTS_FOR_ACCURACY_TITLE,
        ),
        "marathon": await _title(db, viewer.id, since, func.max(ArcadeScore.duration_ms)),
    }
    return {
        "period": period,
        "entries": entries,
        "me": _entry(me_row, viewer.id) if me_row is not None else None,
        "total_players": total,
        "live": await list_live(viewer.id),
        "titles": titles,
    }


async def rank_of(db: AsyncSession, viewer: User, period: str) -> int | None:
    ranked = _ranked_board(viewer.id, period_start(period))
    rank = (await db.execute(select(ranked.c.rank).where(ranked.c.user_id == viewer.id))).scalar()
    return int(rank) if rank is not None else None


async def personal_best(db: AsyncSession, user_id: uuid.UUID) -> int:
    best = (
        await db.execute(
            select(func.max(ArcadeScore.score)).where(
                ArcadeScore.user_id == user_id, ArcadeScore.ranked.is_(True)
            )
        )
    ).scalar()
    return int(best or 0)


async def rival_ladder(db: AsyncSession, viewer: User) -> dict[str, Any]:
    """The weekly board around the player: who is right above, and who leads.

    A starting game shows the next score to beat and announces each rival it
    passes, without polling the board mid-game.
    """
    ranked = _ranked_board(viewer.id, period_start("week"))
    me = (await db.execute(select(ranked).where(ranked.c.user_id == viewer.id))).first()
    my_best = int(me.score) if me is not None else 0
    above = (
        await db.execute(
            select(ranked)
            .where(ranked.c.user_id != viewer.id, ranked.c.score > my_best)
            .order_by(ranked.c.score.asc(), ranked.c.rank.desc())
            .limit(LADDER_SIZE)
        )
    ).all()
    leader = (await db.execute(select(ranked).order_by(ranked.c.rank).limit(1))).first()

    def rival(row: Any) -> dict[str, Any]:
        return {
            "callsign": _display_callsign(row.callsign, row.full_name, row.email),
            "score": int(row.score),
            "rank": int(row.rank),
            "is_self": row.user_id == viewer.id,
        }

    return {
        "ladder": [rival(row) for row in above],
        "leader": rival(leader) if leader is not None else None,
        "week_rank": int(me.rank) if me is not None else None,
    }


# ── Recording a run ──────────────────────────────────────────────────────────


async def record_run(
    db: AsyncSession,
    user: User,
    *,
    session: dict[str, Any],
    stats: RunStats,
    cheat: bool,
    claimed_achievements: Iterable[str],
) -> dict[str, Any]:
    """Validate and store a finished run; the caller consumed *session* and commits."""
    server_elapsed = max(0.0, _now() - float(session.get("started_at") or 0))
    validate_run(stats, server_elapsed_seconds=server_elapsed)
    mode = session.get("mode") if session.get("mode") in SESSION_MODES else "wait"
    earned = filter_achievements(claimed_achievements, stats, cheat=cheat)

    profile = await _profile_for_update(db, user.id)
    previous_best = await personal_best(db, user.id)

    score = ArcadeScore(
        user_id=user.id,
        score=stats.score,
        wave=stats.wave,
        kills=stats.kills,
        shots=stats.shots,
        hits=stats.hits,
        ui_destroyed=stats.ui_destroyed,
        bosses=stats.bosses,
        max_combo=stats.max_combo,
        ufo_kills=stats.ufo_kills,
        duration_ms=stats.duration_ms,
        mode=mode,
        ranked=not cheat,
    )
    db.add(score)

    unlocked = dict(profile.achievements or {})
    now_iso = datetime.now(UTC).isoformat()
    new_achievements = [a for a in earned if a not in unlocked]
    if new_achievements:
        unlocked.update({a: now_iso for a in new_achievements})
        # A new dict, not an in-place mutation: a plain JSON column does not
        # track changes inside the value.
        profile.achievements = unlocked
    profile.updated_at = datetime.now(UTC)
    await db.flush()

    return {
        "score_id": str(score.id),
        "ranked": not cheat,
        "personal_best": (not cheat) and stats.score > previous_best,
        "previous_best": previous_best,
        "new_achievements": new_achievements,
    }


__all__ = [
    "ACHIEVEMENT_IDS",
    "CHEAT_ACHIEVEMENTS",
    "DEFAULT_PERIOD",
    "EVENT_ACHIEVEMENTS",
    "FALLBACK_CALLSIGN",
    "HEARTBEAT_RATE_WINDOW_SECONDS",
    "HEARTBEAT_SECONDS",
    "HEARTBEAT_SESSION_LIMIT",
    "HEARTBEAT_USER_LIMIT",
    "LEADERBOARD_DEFAULT_LIMIT",
    "LEADERBOARD_MAX_LIMIT",
    "LIVE_TTL_SECONDS",
    "MAX_DURATION_MS",
    "MAX_POINTS_PER_SECOND",
    "MAX_SCORE",
    "MAX_SESSIONS_PER_HOUR",
    "MAX_SHOTS_PER_SECOND",
    "MAX_UI_DESTROYED_PER_RUN",
    "PERIODS",
    "SCORE_ALLOWANCE",
    "SCORE_CEILING_MARGIN",
    "SESSION_TTL_SECONDS",
    "SHOT_ALLOWANCE",
    "STAT_ACHIEVEMENTS",
    "ArcadeError",
    "RunStats",
    "check_session",
    "consume_session",
    "default_callsign",
    "filter_achievements",
    "get_profile",
    "heartbeat_live",
    "leaderboard",
    "leave_live",
    "list_live",
    "max_plausible_score",
    "max_plausible_ufo_kills",
    "max_plausible_wave",
    "max_run_score",
    "max_score_for_waves",
    "max_wave_score",
    "normalize_callsign",
    "normalize_period",
    "period_start",
    "personal_best",
    "plausible_progress",
    "player_stats",
    "rank_of",
    "record_run",
    "rival_ladder",
    "serialize_profile",
    "start_session",
    "update_profile",
    "validate_run",
]
