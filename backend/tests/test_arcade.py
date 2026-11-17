"""Tests for the waiting arcade: run validation, achievements, callsigns,
game sessions, the live room, the route guards, and the tripwires that keep the
server's rules in step with the game's (``frontend/src/components/arcade``).

The leaderboard SQL (DISTINCT ON best run, window rank, visibility filter) runs
against a real PostgreSQL in ``test_arcade_postgres.py`` (integration marker);
here everything is pure or faked, like the rest of the unit suite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import uuid
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import arcade as arcade_api
from app.core import rate_limit
from app.core.config import settings
from app.core.database import get_db
from app.services import arcade

FRONTEND_ENGINE = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "arcade" / "engine"


def _run(**overrides) -> arcade.RunStats:
    base = {
        "score": 4200,
        "wave": 3,
        "kills": 120,
        "shots": 180,
        "hits": 130,
        "ui_destroyed": 6,
        "bosses": 0,
        "max_combo": 22,
        "ufo_kills": 1,
        "duration_ms": 95_000,
    }
    base.update(overrides)
    return arcade.RunStats(**base)


# ── validate_run ─────────────────────────────────────────────────────────────


def test_a_plausible_run_is_accepted():
    arcade.validate_run(_run(), server_elapsed_seconds=100)
    # A long, strong run: wave 12, a boss, 60k points in 10 minutes.
    arcade.validate_run(
        _run(score=60_000, wave=12, kills=700, shots=1200, hits=900, bosses=2, max_combo=80,
             ufo_kills=20, duration_ms=600_000),
        server_elapsed_seconds=640,
    )
    # A short game that ended on a lucky saucer still fits the allowance.
    arcade.validate_run(_run(score=1_300, wave=1, kills=40, duration_ms=8_000, ufo_kills=1),
                        server_elapsed_seconds=10)


@pytest.mark.parametrize(
    ("overrides", "elapsed", "fragment"),
    [
        ({"duration_ms": 200_000}, 100, "longer than its game session"),
        ({"score": 500_000}, 100, "not plausible"),
        ({"score": arcade.MAX_SCORE + 1, "duration_ms": 10_000_000}, 20_000, "off the charts"),
        ({"wave": 40}, 100, "wave is not reachable"),
        ({"hits": 200, "shots": 100}, 100, "hit more often"),
        ({"max_combo": 131}, 100, "combo cannot be longer"),
        ({"kills": 1000, "wave": 3}, 100, "Too many invaders"),
        ({"bosses": 1, "wave": 4}, 100, "fifth wave"),
        ({"ufo_kills": 40}, 100, "saucer does not come by"),
    ],
)
def test_an_implausible_run_is_refused(overrides, elapsed, fragment):
    with pytest.raises(arcade.ArcadeError) as caught:
        arcade.validate_run(_run(**overrides), server_elapsed_seconds=elapsed)
    assert fragment in caught.value.message
    assert caught.value.status_code == 422


def test_the_rate_is_measured_against_play_time_not_session_age():
    # A session left open for an hour does not license an hour's worth of
    # points for a 30-second game: the client's own play time is the stricter.
    with pytest.raises(arcade.ArcadeError):
        arcade.validate_run(_run(score=200_000, wave=2, duration_ms=30_000), server_elapsed_seconds=3600)


def test_the_clock_slack_is_never_play_time():
    """The slack forgives a client clock running a little ahead; it must not
    hand out 15 free seconds of points to a run finished the instant it began."""
    instant = _run(score=30_000, wave=1, kills=0, shots=0, hits=0, max_combo=0, ufo_kills=0, duration_ms=15_000)
    with pytest.raises(arcade.ArcadeError) as caught:
        arcade.validate_run(instant, server_elapsed_seconds=0.5)
    assert "not plausible" in caught.value.message


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        # Every counter a public title reads is bounded, not only the score.
        ({"shots": 100_000, "hits": 0, "max_combo": 0}, "More shots"),
        ({"ui_destroyed": arcade.MAX_UI_DESTROYED_PER_RUN + 1}, "interface"),
    ],
)
def test_the_counters_behind_the_titles_are_bounded(overrides, fragment):
    with pytest.raises(arcade.ArcadeError) as caught:
        arcade.validate_run(_run(**overrides), server_elapsed_seconds=100)
    assert fragment in caught.value.message


def test_the_fastest_legitimate_cannon_is_never_refused():
    # Rapid fire with the spread for the whole run: the cannon's ceiling.
    seconds = 100
    shots = int(3 * 1000 / 110 * seconds)
    arcade.validate_run(
        _run(shots=shots, hits=shots // 2, ui_destroyed=arcade.MAX_UI_DESTROYED_PER_RUN),
        server_elapsed_seconds=seconds + 5,
    )


# ── the score per wave ───────────────────────────────────────────────────────


def test_the_per_wave_table_is_the_game_s_worst_case():
    # A formation wave: 55 golden invaders, every kill at x4, and the clear bonus.
    assert arcade.max_wave_score(1) == 55 * 100 * 4 + 500
    assert arcade.max_wave_score(4) == 22_000 + 950
    assert arcade.max_wave_score(11) == arcade.max_wave_score(99) == 22_000 + 2_000
    # A boss wave: the boss, the six minions a Token Overflow splits into, at x4, and the clear bonus.
    assert arcade.max_wave_score(5) == 2_500 + 6 * 40 * 4 + 1_100
    assert arcade.max_wave_score(55) == arcade.max_wave_score(100) == 5_000 + 960 + 2_000
    # A run is bounded by every wave it reached.
    assert arcade.max_score_for_waves(1) == 22_500
    assert arcade.max_score_for_waves(5) == 22_500 + 22_650 + 22_800 + 22_950 + 4_560


def test_a_boss_wave_holds_its_clear_bonus_too():
    """A boss that finishes dying while the cannon is down ends its wave through
    the formation's clear: the cannon respawns on an empty formation, which pays
    the wave-clear bonus on top of the boss (``game.test.ts`` plays it)."""
    minions = arcade.MAX_MINIONS_PER_BOSS * arcade.MINION_POINTS * arcade.MAX_MULTIPLIER
    for wave in (5, 10, 15, 55, 100):
        assert arcade.max_wave_score(wave) == arcade.boss_points(wave) + minions + arcade.wave_clear_bonus(wave)


def test_the_ceiling_keeps_an_explicit_margin_over_the_rules():
    # Wave 1 with nothing else: the wave, the crater cap, the two once-per-game bonuses.
    rules = 22_500 + arcade.MAX_CRATER_POINTS + arcade.SURVIVAL_BONUS_MAX + arcade.PACIFIST_BONUS
    assert arcade.SCORE_CEILING_MARGIN > 1
    assert arcade.max_run_score(1, ufo_kills=0, ui_destroyed=0, seconds=0) == int(arcade.SCORE_CEILING_MARGIN * rules)
    # Saucers and wreckage are counted from the run's own numbers.
    more = arcade.max_run_score(1, ufo_kills=2, ui_destroyed=4, seconds=0)
    extra = 2 * arcade.MAX_UFO_POINTS + 4 * arcade.UI_DESTROYED_POINTS
    assert more == int(arcade.SCORE_CEILING_MARGIN * (rules + extra))
    # The Prompt Injector's payloads keep coming while it lives: time, not the wave, bounds them.
    before = arcade.max_run_score(arcade.FIRST_PAYLOAD_WAVE - 1, ufo_kills=0, ui_destroyed=0, seconds=600)
    assert before == arcade.max_run_score(arcade.FIRST_PAYLOAD_WAVE - 1, ufo_kills=0, ui_destroyed=0, seconds=0)
    after = arcade.max_run_score(arcade.FIRST_PAYLOAD_WAVE, ufo_kills=0, ui_destroyed=0, seconds=600)
    assert after > arcade.max_run_score(arcade.FIRST_PAYLOAD_WAVE, ufo_kills=0, ui_destroyed=0, seconds=0)


@pytest.mark.parametrize(
    "overrides",
    [
        # One wave and nothing else.
        {"wave": 1, "kills": 55, "ufo_kills": 0, "ui_destroyed": 0, "bosses": 0, "duration_ms": 60_000},
        # Several waves, two bosses, saucers and wreckage.
        {"wave": 12, "kills": 600, "ufo_kills": 10, "ui_destroyed": 40, "bosses": 2, "duration_ms": 600_000},
        # Past the first Prompt Injector: its payloads count by the time played.
        {"wave": 16, "kills": 700, "ufo_kills": 20, "ui_destroyed": 100, "bosses": 3, "duration_ms": 900_000},
    ],
)
def test_a_run_at_the_ceiling_is_accepted_and_one_point_more_is_refused(overrides):
    seconds = overrides["duration_ms"] / 1000
    ceiling = arcade.max_run_score(
        overrides["wave"],
        ufo_kills=overrides["ufo_kills"],
        ui_destroyed=overrides["ui_destroyed"],
        seconds=seconds,
    )
    # The time bound is not what decides here.
    assert ceiling + 1 <= arcade.max_plausible_score(seconds)
    arcade.validate_run(_run(score=ceiling, **overrides), server_elapsed_seconds=seconds + 1)
    with pytest.raises(arcade.ArcadeError) as caught:
        arcade.validate_run(_run(score=ceiling + 1, **overrides), server_elapsed_seconds=seconds + 1)
    assert caught.value.status_code == 422
    assert caught.value.message == f"This score is more than the game can yield by wave {overrides['wave']}."


def test_an_old_session_no_longer_licenses_any_score_at_a_low_wave():
    """The shape the time bound alone left open: an hour-old session licenses
    5.4 million points, at wave 1 as much as at wave 700."""
    forged = _run(score=5_000_000, wave=1, kills=55, ufo_kills=0, ui_destroyed=0, duration_ms=3_600_000)
    assert forged.score <= arcade.max_plausible_score(3600)
    with pytest.raises(arcade.ArcadeError) as caught:
        arcade.validate_run(forged, server_elapsed_seconds=3601)
    assert caught.value.message == "This score is more than the game can yield by wave 1."


# ── achievements ─────────────────────────────────────────────────────────────


def test_stat_achievements_are_rechecked_and_events_accepted_from_the_list():
    stats = _run(kills=10, wave=2, score=900, ui_destroyed=30)
    earned = arcade.filter_achievements(
        ["first_blood", "wave_10", "lucky_23", "made_up", "wrecking_ball", "first_blood"],
        stats,
        cheat=False,
    )
    assert earned == ["first_blood", "lucky_23", "wrecking_ball"]


def test_a_cheat_run_only_unlocks_the_cheat_achievements():
    stats = _run(kills=500, wave=9, score=40_000, duration_ms=500_000)
    earned = arcade.filter_achievements(["god_complex", "first_blood", "lucky_23", "konami_kid"], stats, cheat=True)
    assert earned == ["god_complex", "konami_kid"]
    assert arcade.CHEAT_ACHIEVEMENTS <= arcade.EVENT_ACHIEVEMENTS


# ── callsigns ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("sam", "SAM"), (" ab ", "AB"), ("7", "7"), ("R2D", "R2D"), ("", None), ("   ", None), (None, None)],
)
def test_callsigns_are_normalized(raw, expected):
    assert arcade.normalize_callsign(raw) == expected


@pytest.mark.parametrize("raw", ["ABCD", "A-B", "é", "a b", "ASS", "kkk", "ss", "A55", "5EX", "F4G", "N1G", "KY5"])
def test_bad_callsigns_are_refused(raw):
    with pytest.raises(arcade.ArcadeError):
        arcade.normalize_callsign(raw)


@pytest.mark.parametrize(
    ("name", "email", "expected"),
    [
        ("Ada Lovelace", None, "ALO"),
        ("Grace Brewster Hopper", None, "GBH"),
        ("Linus", None, "LIN"),
        ("Élodie Durand", None, "EDU"),
        ("山田太郎", "yamada.t@example.com", "YAM"),
        ("", "", arcade.FALLBACK_CALLSIGN),
        # Initials that would spell a blocked word fall through to the next form.
        ("Adam Ssmith", None, "ADA"),
        # ...and so do initials that spell one with digits for letters.
        ("Amy 55", None, "AMY"),
    ],
)
def test_default_callsigns_read_like_arcade_initials(name, email, expected):
    assert arcade.default_callsign(name, email) == expected


def test_a_player_who_scored_nothing_is_not_ranked():
    from sqlalchemy.dialects import postgresql

    board = arcade._ranked_board(uuid.uuid4(), None)
    sql = str(board.compile(dialect=postgresql.dialect()))
    assert "arcade_scores.score > " in sql
    assert "DISTINCT ON (arcade_scores.user_id)" in sql


# ── game sessions (fake Redis) ───────────────────────────────────────────────


class _FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    async def incr(self, key):
        self.kv[key] = str(int(self.kv.get(key, "0")) + 1)
        return int(self.kv[key])

    async def eval(self, script, numkeys, key, window):
        # The shared fixed-window counter (INCR_EXPIRE_LUA): increment, and
        # (re-)arm the expiry when the key has none.
        assert script == arcade.INCR_EXPIRE_LUA and numkeys == 1
        current = await self.incr(key)
        if current == 1 or await self.ttl(key) < 0:
            await self.expire(key, window)
        return current

    async def ttl(self, key):
        return self.ttls.get(key, -1 if key in self.kv else -2)

    async def expire(self, key, seconds):
        self.ttls[key] = seconds

    async def set(self, key, value, ex=None):
        self.kv[key] = value
        if ex:
            self.ttls[key] = ex

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, key):
        return 1 if self.kv.pop(key, None) is not None else 0

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key, *fields):
        for field in fields:
            self.hashes.get(key, {}).pop(field, None)

    def advance(self, seconds: float) -> None:
        """Let *seconds* pass: keys whose expiry runs out are gone."""
        for key in list(self.ttls):
            self.ttls[key] -= seconds
            if self.ttls[key] <= 0:
                del self.ttls[key]
                self.kv.pop(key, None)
                self.hashes.pop(key, None)


class _BrokenRedis:
    def __getattr__(self, name):
        async def _fail(*args, **kwargs):
            raise ConnectionError("redis down")

        return _fail


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(arcade, "redis_client", _client)
    # The heartbeat limit counts through the platform's shared rate-limit helper.
    monkeypatch.setattr(rate_limit, "redis_client", _client)
    return fake


def test_a_session_is_bound_to_its_player_and_consumed_once(fake_redis):
    alice, bob = uuid.uuid4(), uuid.uuid4()
    session = asyncio.run(arcade.start_session(alice, mode="wait"))
    key = arcade.session_key(session["session_id"])
    assert fake_redis.ttls[key] == arcade.SESSION_TTL_SECONDS
    stored = json.loads(fake_redis.kv[key])
    assert stored["user_id"] == str(alice) and stored["mode"] == "wait"

    # Another player cannot finish it - nor burn it: it reads like a missing one.
    with pytest.raises(arcade.ArcadeError) as caught:
        asyncio.run(arcade.consume_session(session["session_id"], bob))
    assert caught.value.status_code == 404
    assert key in fake_redis.kv

    payload = asyncio.run(arcade.consume_session(session["session_id"], alice))
    assert payload["user_id"] == str(alice)
    with pytest.raises(arcade.ArcadeError):
        asyncio.run(arcade.consume_session(session["session_id"], alice))


@pytest.mark.parametrize("session_id", ["", "../../etc", "arcade:rate:x", "Z" * 32, "a" * 31])
def test_a_malformed_session_id_never_reaches_redis(fake_redis, session_id):
    with pytest.raises(arcade.ArcadeError) as caught:
        asyncio.run(arcade.check_session(session_id, uuid.uuid4()))
    assert caught.value.status_code == 404


def test_unknown_modes_are_refused(fake_redis):
    with pytest.raises(arcade.ArcadeError):
        asyncio.run(arcade.start_session(uuid.uuid4(), mode="ranked-cheat"))


def test_starting_games_is_rate_limited_and_the_window_rearms(fake_redis):
    user = uuid.uuid4()
    for _ in range(arcade.MAX_SESSIONS_PER_HOUR):
        asyncio.run(arcade.start_session(user, mode="free"))
    with pytest.raises(arcade.ArcadeError) as caught:
        asyncio.run(arcade.start_session(user, mode="free"))
    assert caught.value.status_code == 429

    # A counter that lost its TTL gets one back instead of locking the player out.
    other = uuid.uuid4()
    rate_key = f"{arcade.RATE_KEY_PREFIX}{other}"
    fake_redis.kv[rate_key] = "5"
    asyncio.run(arcade.start_session(other, mode="free"))
    assert fake_redis.ttls[rate_key] == 3600


def test_a_redis_outage_refuses_to_start_a_ranked_game(monkeypatch):
    async def _client():
        return _BrokenRedis()

    monkeypatch.setattr(arcade, "redis_client", _client)
    with pytest.raises(arcade.ArcadeError) as caught:
        asyncio.run(arcade.start_session(uuid.uuid4(), mode="wait"))
    assert caught.value.status_code == 503
    # ...while the live room fails open.
    assert asyncio.run(arcade.list_live(uuid.uuid4())) == []
    assert asyncio.run(arcade.heartbeat_live(uuid.uuid4(), callsign="A", score=1, wave=1, hidden=False)) == []


# ── live room ────────────────────────────────────────────────────────────────


def test_the_live_room_hides_opted_out_players_and_prunes_stale_ones(fake_redis):
    me, visible, hidden, stale = (uuid.uuid4() for _ in range(4))
    asyncio.run(arcade.heartbeat_live(visible, callsign="VIS", score=900, wave=2, hidden=False))
    asyncio.run(arcade.heartbeat_live(hidden, callsign="HID", score=5000, wave=4, hidden=True))
    asyncio.run(arcade.heartbeat_live(stale, callsign="STA", score=100, wave=1, hidden=False))
    entry = json.loads(fake_redis.hashes[arcade.LIVE_KEY][str(stale)])
    entry["ts"] -= arcade.LIVE_TTL_SECONDS + 5
    fake_redis.hashes[arcade.LIVE_KEY][str(stale)] = json.dumps(entry)
    fake_redis.hashes[arcade.LIVE_KEY]["garbage"] = "{not json"

    room = asyncio.run(arcade.heartbeat_live(me, callsign="ME", score=1200, wave=3, hidden=True))
    # Opted out myself, I still see myself; I never see another opted-out player.
    assert [(p["callsign"], p["is_self"]) for p in room] == [("ME", True), ("VIS", False)]
    assert str(stale) not in fake_redis.hashes[arcade.LIVE_KEY]
    assert "garbage" not in fake_redis.hashes[arcade.LIVE_KEY]

    asyncio.run(arcade.leave_live(me))
    assert [p["callsign"] for p in asyncio.run(arcade.list_live(visible))] == ["VIS"]


# ── routes ───────────────────────────────────────────────────────────────────


def _user(**overrides):
    base = {"id": uuid.uuid4(), "full_name": "Ada Lovelace", "email": "ada@example.com", "is_admin": False}
    base.update(overrides)
    return SimpleNamespace(**base)


def _client(user):
    app = FastAPI()
    app.include_router(arcade_api.router, prefix="/api/v1/arcade")
    app.dependency_overrides[deps.get_current_user] = lambda: user

    async def _db():
        yield SimpleNamespace(commit=_noop)

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


async def _noop(*args, **kwargs):
    return None


def test_every_route_404s_when_the_arcade_is_off(monkeypatch):
    monkeypatch.setattr(settings, "arcade_enabled", False)
    client = _client(_user())
    assert client.get("/api/v1/arcade/leaderboard").status_code == 404
    assert client.get("/api/v1/arcade/profile").status_code == 404
    assert client.post("/api/v1/arcade/sessions", json={}).status_code == 404
    assert client.post(f"/api/v1/arcade/sessions/{'a' * 32}/finish", json={"score": 1}).status_code == 404


def test_nothing_is_recorded_while_impersonating(monkeypatch):
    monkeypatch.setattr(settings, "arcade_enabled", True)
    client = _client(_user(impersonated_by=str(uuid.uuid4())))
    assert client.post("/api/v1/arcade/sessions", json={"mode": "wait"}).status_code == 403
    finish = client.post(f"/api/v1/arcade/sessions/{'a' * 32}/finish", json={"score": 10})
    assert finish.status_code == 403
    heartbeat = client.post(f"/api/v1/arcade/sessions/{'a' * 32}/heartbeat", json={"score": 10, "wave": 1})
    assert heartbeat.status_code == 403
    assert client.put("/api/v1/arcade/profile", json={"callsign": "ABC"}).status_code == 403


def test_a_refused_run_has_already_burned_its_session(monkeypatch, fake_redis):
    """The session is consumed before the run is checked: a refused run cannot be
    resubmitted with smaller numbers."""
    monkeypatch.setattr(settings, "arcade_enabled", True)
    user = _user()
    session = asyncio.run(arcade.start_session(user.id, mode="wait"))
    client = _client(user)
    refused = client.post(
        f"/api/v1/arcade/sessions/{session['session_id']}/finish",
        json={"score": 9_999_999, "wave": 1, "duration_ms": 1000},
    )
    assert refused.status_code == 422
    assert arcade.session_key(session["session_id"]) not in fake_redis.kv
    retry = client.post(
        f"/api/v1/arcade/sessions/{session['session_id']}/finish",
        json={"score": 10, "wave": 1, "duration_ms": 1000},
    )
    assert retry.status_code == 404


def test_the_finish_payload_is_bounded():
    from app.api.v1.schemas.arcade import ArcadeFinishRequest

    with pytest.raises(ValueError):
        ArcadeFinishRequest(score=-1)
    with pytest.raises(ValueError):
        ArcadeFinishRequest(score=1, achievements=["x"] * 65)
    with pytest.raises(ValueError):
        ArcadeFinishRequest(score=1, duration_ms=arcade.MAX_DURATION_MS + 1)
    with pytest.raises(ValueError):
        ArcadeFinishRequest(score=1, achievements=["x" * 41])
    for bad in (float("nan"), float("inf"), 1.5, "many"):
        with pytest.raises(ValueError):
            ArcadeFinishRequest(score=bad)


def test_the_heartbeat_refuses_impersonation(monkeypatch, fake_redis):
    monkeypatch.setattr(settings, "arcade_enabled", True)
    user = _user(impersonated_by=str(uuid.uuid4()))
    session = asyncio.run(arcade.start_session(user.id, mode="wait"))
    beat = _client(user).post(
        f"/api/v1/arcade/sessions/{session['session_id']}/heartbeat", json={"score": 5, "wave": 1}
    )
    assert beat.status_code == 403
    assert arcade.LIVE_KEY not in fake_redis.hashes


def test_a_heartbeat_cannot_announce_a_score_the_session_is_too_young_for(monkeypatch, fake_redis):
    monkeypatch.setattr(settings, "arcade_enabled", True)
    player = _user()
    session = asyncio.run(arcade.start_session(player.id, mode="wait"))

    async def _no_profile(_db, _user_id):
        return None

    monkeypatch.setattr(arcade, "get_profile", _no_profile)
    beat = _client(player).post(
        f"/api/v1/arcade/sessions/{session['session_id']}/heartbeat",
        json={"score": arcade.MAX_SCORE, "wave": 90_000},
    )
    assert beat.status_code == 200
    [me] = beat.json()["live"]
    assert me["score"] <= arcade.max_plausible_score(5)
    assert me["wave"] <= arcade.max_plausible_wave(5)


def _age_session(fake_redis, session_id: str, seconds: float) -> None:
    """Move a stored session's start *seconds* into the past."""
    key = arcade.session_key(session_id)
    stored = json.loads(fake_redis.kv[key])
    stored["started_at"] -= seconds
    fake_redis.kv[key] = json.dumps(stored)


def test_the_server_says_how_often_to_beat_on_the_session_and_on_every_beat(monkeypatch, fake_redis):
    monkeypatch.setattr(settings, "arcade_enabled", True)
    player = _user()

    async def _none(*_args, **_kwargs):
        return None

    async def _ladder(*_args, **_kwargs):
        return {"ladder": [], "leader": None, "week_rank": None}

    async def _zero(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(arcade, "get_profile", _none)
    monkeypatch.setattr(arcade, "rival_ladder", _ladder)
    monkeypatch.setattr(arcade, "personal_best", _zero)
    client = _client(player)
    started = client.post("/api/v1/arcade/sessions", json={"mode": "free"})
    assert started.status_code == 201
    assert started.json()["heartbeat_seconds"] == arcade.HEARTBEAT_SECONDS
    beat = client.post(
        f"/api/v1/arcade/sessions/{started.json()['session_id']}/heartbeat", json={"score": 10, "wave": 1}
    )
    assert beat.status_code == 200
    assert beat.json()["heartbeat_seconds"] == arcade.HEARTBEAT_SECONDS


def test_a_heartbeat_cannot_announce_more_points_than_its_waves_hold(monkeypatch, fake_redis):
    """An hour-old session lets a beat announce 5.4 million points by time
    alone; the waves announced cap it well below that."""
    monkeypatch.setattr(settings, "arcade_enabled", True)
    # A frozen clock: the session is exactly an hour old when it beats.
    frozen = arcade._now()
    monkeypatch.setattr(arcade, "_now", lambda: frozen)
    player = _user()
    session = asyncio.run(arcade.start_session(player.id, mode="free"))
    _age_session(fake_redis, session["session_id"], 3600)

    async def _no_profile(_db, _user_id):
        return None

    monkeypatch.setattr(arcade, "get_profile", _no_profile)
    beat = _client(player).post(
        f"/api/v1/arcade/sessions/{session['session_id']}/heartbeat", json={"score": 5_000_000, "wave": 1}
    )
    assert beat.status_code == 200
    [me] = beat.json()["live"]
    # A beat carries no counters: they count at the most the session's age allows.
    ceiling = arcade.max_run_score(
        1,
        ufo_kills=arcade.max_plausible_ufo_kills(3600),
        ui_destroyed=arcade.MAX_UI_DESTROYED_PER_RUN,
        seconds=3600,
    )
    assert me["score"] == ceiling < 5_000_000 <= arcade.max_plausible_score(3600)
    assert me["wave"] == 1


def _client_heartbeat_constant(name: str) -> int:
    source = (FRONTEND_ENGINE.parent / "heartbeat.ts").read_text(encoding="utf-8")
    match = re.search(rf"export const {name} = ([0-9_]+);", source)
    assert match, f"{name} not found in heartbeat.ts"
    return int(match.group(1).replace("_", ""))


@pytest.fixture
def beating(monkeypatch, fake_redis):
    """Routes on fake Redis, with no arcade profile to read."""
    monkeypatch.setattr(settings, "arcade_enabled", True)

    async def _no_profile(_db, _user_id):
        return None

    monkeypatch.setattr(arcade, "get_profile", _no_profile)
    return fake_redis


def _beat(client, session_id: str):
    return client.post(f"/api/v1/arcade/sessions/{session_id}/heartbeat", json={"score": 10, "wave": 1})


def test_a_burst_of_heartbeats_is_refused_with_a_retry_after_and_the_game_goes_on(beating):
    player = _user()
    session_id = asyncio.run(arcade.start_session(player.id, mode="free"))["session_id"]
    client = _client(player)
    answers = [_beat(client, session_id) for _ in range(arcade.HEARTBEAT_SESSION_LIMIT + 5)]
    assert [a.status_code for a in answers[: arcade.HEARTBEAT_SESSION_LIMIT]] == [200] * arcade.HEARTBEAT_SESSION_LIMIT
    refused = answers[arcade.HEARTBEAT_SESSION_LIMIT :]
    assert [a.status_code for a in refused] == [429] * 5
    assert 1 <= int(refused[0].headers["Retry-After"]) <= arcade.HEARTBEAT_RATE_WINDOW_SECONDS
    # A refused beat is a missed beat: the session is intact and its run can still be recorded.
    assert asyncio.run(arcade.check_session(session_id, player.id))["user_id"] == str(player.id)


def test_one_player_bursting_across_sessions_meets_the_per_user_limit(beating):
    player = _user()
    client = _client(player)
    sessions = [asyncio.run(arcade.start_session(player.id, mode="free"))["session_id"] for _ in range(3)]
    statuses = [_beat(client, sid).status_code for sid in sessions for _ in range(arcade.HEARTBEAT_SESSION_LIMIT)]
    assert statuses[: arcade.HEARTBEAT_USER_LIMIT] == [200] * arcade.HEARTBEAT_USER_LIMIT
    assert set(statuses[arcade.HEARTBEAT_USER_LIMIT :]) == {429}


def test_the_announced_cadence_and_a_jittered_one_are_never_refused(beating):
    """The announced cadence, the fastest the client ever beats, jitter, a tab
    coming back from background throttling, and a replay's first beat."""
    fastest = _client_heartbeat_constant("MIN_HEARTBEAT_SECONDS")
    player = _user()
    client = _client(player)
    session_id = asyncio.run(arcade.start_session(player.id, mode="free"))["session_id"]
    rng = random.Random(3888)  # noqa: S311 - seeded jitter, not a secret
    gaps = (
        [arcade.HEARTBEAT_SECONDS] * 30
        + [fastest] * 40
        + [rng.uniform(fastest, 3 * arcade.HEARTBEAT_SECONDS) for _ in range(80)]
        + [95.0, fastest, fastest, fastest]
    )
    for gap in gaps:
        beating.advance(gap)
        assert _beat(client, session_id).status_code == 200
    # A replay is a new session in the same user window: its first beat passes.
    replay = asyncio.run(arcade.start_session(player.id, mode="free"))["session_id"]
    assert _beat(client, replay).status_code == 200


def test_the_heartbeat_limit_leaves_room_above_the_announced_interval():
    window = arcade.HEARTBEAT_RATE_WINDOW_SECONDS
    # A fixed window holds this many beats of a client at its fastest (5 s)...
    fastest = window // _client_heartbeat_constant("MIN_HEARTBEAT_SECONDS") + 1
    # ...and the session limit sits above it, with room for a beat a waking tab fires late.
    session_limit, user_limit = arcade.HEARTBEAT_SESSION_LIMIT, arcade.HEARTBEAT_USER_LIMIT
    assert session_limit > fastest
    assert session_limit == 3 * window // arcade.HEARTBEAT_SECONDS
    # Two games of one player at the fastest pace fit under the per-user limit.
    assert user_limit >= 2 * session_limit


@pytest.mark.parametrize(("mode", "status_code"), [("open", 200), ("closed", 503)])
def test_the_heartbeat_limit_follows_the_platform_fail_mode_when_redis_is_down(beating, monkeypatch, mode, status_code):
    player = _user()
    session_id = asyncio.run(arcade.start_session(player.id, mode="free"))["session_id"]
    monkeypatch.setattr(settings, "rate_limit_fail_mode", mode)

    async def _broken():
        return _BrokenRedis()

    monkeypatch.setattr(rate_limit, "redis_client", _broken)
    assert _beat(_client(player), session_id).status_code == status_code


def test_a_run_refused_for_its_waves_says_why_and_is_logged_without_the_player(monkeypatch, fake_redis, caplog):
    monkeypatch.setattr(settings, "arcade_enabled", True)
    user = _user()
    session = asyncio.run(arcade.start_session(user.id, mode="wait"))
    _age_session(fake_redis, session["session_id"], 3600)
    with caplog.at_level(logging.INFO, logger=arcade_api.logger.name):
        refused = _client(user).post(
            f"/api/v1/arcade/sessions/{session['session_id']}/finish",
            json={"score": 5_000_000, "wave": 1, "kills": 55, "duration_ms": 3_600_000},
        )
    assert refused.status_code == 422
    assert refused.json()["detail"] == "This score is more than the game can yield by wave 1."
    [logged] = [r.getMessage() for r in caplog.records if "run refused" in r.getMessage()]
    # The numbers that would explain a false refusal...
    assert "can yield by wave 1" in logged and "5000000" in logged
    # ...and nothing that says who played.
    for identifying in (str(user.id), user.id.hex, user.email, user.full_name):
        assert identifying not in logged


def test_opting_out_mid_game_hides_the_player_at_the_next_heartbeat(monkeypatch, fake_redis):
    """The live room follows the profile as it is now, not as it was when the
    game started: a player who opts out mid-game is not re-announced."""
    monkeypatch.setattr(settings, "arcade_enabled", True)
    player, viewer = _user(), _user(full_name="Grace Hopper", email="grace@example.com")
    session = asyncio.run(arcade.start_session(player.id, mode="wait"))
    profile = SimpleNamespace(hidden=True, callsign="NEW")

    async def _profile(_db, user_id):
        return profile if user_id == player.id else None

    monkeypatch.setattr(arcade, "get_profile", _profile)
    beat = _client(player).post(
        f"/api/v1/arcade/sessions/{session['session_id']}/heartbeat", json={"score": 900, "wave": 2}
    )
    assert beat.status_code == 200
    # The player still sees themselves, under the callsign they have now...
    assert [(p["callsign"], p["is_self"]) for p in beat.json()["live"]] == [("NEW", True)]
    # ...and nobody else sees them.
    assert asyncio.run(arcade.list_live(viewer.id)) == []


def test_other_players_are_known_by_their_callsign_alone(fake_redis):
    from app.api.v1.schemas import arcade as schemas

    identifying = {"user_id", "display_name", "full_name", "name", "email", "last_played_at"}
    for model in (
        schemas.ArcadeLeaderboardEntry,
        schemas.ArcadeLivePlayer,
        schemas.ArcadeTitleHolder,
        schemas.ArcadeRival,
    ):
        assert not identifying & set(model.model_fields), model.__name__

    player, viewer = uuid.uuid4(), uuid.uuid4()
    session = asyncio.run(arcade.start_session(player, mode="free"))
    stored = json.loads(fake_redis.kv[arcade.session_key(session["session_id"])])
    assert not identifying & set(stored) - {"user_id"}
    asyncio.run(arcade.heartbeat_live(player, callsign="ALO", score=10, wave=1, hidden=False))
    entry = json.loads(fake_redis.hashes[arcade.LIVE_KEY][str(player)])
    assert not identifying & set(entry)
    assert asyncio.run(arcade.list_live(viewer)) == [
        {"callsign": "ALO", "score": 10, "wave": 1, "is_self": False}
    ]


def test_the_auth_config_tells_the_frontend_whether_the_arcade_is_on():
    from app.api.v1.auth import AuthConfigResponse

    assert AuthConfigResponse.model_fields["arcade_enabled"].default is True


# ── tripwires: the server and the game agree ─────────────────────────────────


def _frontend_achievements() -> dict[str, str]:
    source = (FRONTEND_ENGINE / "achievements.ts").read_text(encoding="utf-8")
    return dict(re.findall(r'\{ id: "([a-z0-9_]+)", kind: "(stat|event)"', source))


def test_the_server_knows_exactly_the_game_s_achievements():
    frontend = _frontend_achievements()
    assert frontend, "could not read the achievement catalog of the game"
    assert {i for i, k in frontend.items() if k == "stat"} == set(arcade.STAT_ACHIEVEMENTS)
    assert {i for i, k in frontend.items() if k == "event"} == set(arcade.EVENT_ACHIEVEMENTS)
    source = (FRONTEND_ENGINE / "achievements.ts").read_text(encoding="utf-8")
    cheat_block = source.split("CHEAT_ACHIEVEMENTS", 1)[1].split("]);", 1)[0]
    assert set(re.findall(r'"([a-z0-9_]+)"', cheat_block)) == set(arcade.CHEAT_ACHIEVEMENTS)


def test_a_game_is_paid_for_no_more_elements_than_the_server_accepts():
    """Tracking is bounded, damage is not: an element hit while the table is
    full recycles a tracked one. What a run is paid for is decided apart, when
    an element is admitted, against MAX_SCORED_ELEMENTS - the constant rules.ts
    pins to MAX_UI_DESTROYED_PER_RUN - and never refunded on recycling, so a run
    counts at most that many destroyed elements, and craters on no more."""
    source = (FRONTEND_ENGINE / "screen-damage.ts").read_text(encoding="utf-8")
    assert re.search(r'^import \{[^}]*\bMAX_SCORED_ELEMENTS\b[^}]*\} from "\./rules\.ts";', source, re.M), (
        "screen-damage.ts no longer reads MAX_SCORED_ELEMENTS from rules.ts"
    )
    # One place decides that an element is paid for, against the server's bound,
    # and the admission counter it reads only ever grows.
    assert re.findall(r"\bscored = ([^;]+);", source) == ["this.admitted < MAX_SCORED_ELEMENTS"]
    assert _writes(source, "admitted") == ["this.admitted +="]
    assert _ts_int(_rules(), "MAX_SCORED_ELEMENTS") == arcade.MAX_UI_DESTROYED_PER_RUN


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_POINTS_PER_SECOND", arcade.MAX_POINTS_PER_SECOND),
        ("SCORE_ALLOWANCE", arcade.SCORE_ALLOWANCE),
        ("MIN_SECONDS_PER_WAVE", arcade.MIN_SECONDS_PER_WAVE),
        ("MAX_SHOTS_PER_SECOND", arcade.MAX_SHOTS_PER_SECOND),
        ("SHOT_ALLOWANCE", arcade.SHOT_ALLOWANCE),
    ],
)
def test_the_plausibility_bounds_match_the_game_s_rules(name, value):
    source = (FRONTEND_ENGINE / "rules.ts").read_text(encoding="utf-8")
    match = re.search(rf"export const {name} = ([0-9_]+);", source)
    assert match, f"{name} not found in rules.ts"
    assert int(match.group(1).replace("_", "")) == value


# ── tripwires: the score per wave is the game's own worst case ───────────────


def _rules() -> str:
    return (FRONTEND_ENGINE / "rules.ts").read_text(encoding="utf-8")


def _ts_int(source: str, name: str) -> int:
    match = re.search(rf"export const {name} = ([0-9_]+);", source)
    assert match, f"{name} not found in rules.ts"
    return int(match.group(1).replace("_", ""))


def _writes(source: str, field: str) -> list[str]:
    """Every write to ``this.<field>`` in *source*: assignments, compound
    assignments and increments, prefix or postfix."""
    return re.findall(rf"(?:\+\+|--)\s*this\.{field}\b|this\.{field}\s*(?:\+\+|--|[-+*/%]?=(?!=))", source)


def _ts_items(source: str, name: str) -> list[str]:
    """The entries of an array constant (``[a, b]``) or the values of a record (``{ k: v }``)."""
    match = re.search(rf"\bconst {name}\b[^=]*=\s*([\[{{])(.*?)[\]}}]", source, re.S)
    assert match, f"{name} not found in rules.ts"
    entries = [entry.strip() for entry in match.group(2).split(",") if entry.strip()]
    if match.group(1) == "{":
        entries = [entry.split(":", 1)[1].strip() for entry in entries]
    return entries


def test_the_per_wave_ceiling_is_the_game_s_own_worst_case():
    """Recomputed from engine/rules.ts: raise a value there and this fails
    until the server knows it, so the ceiling can never refuse a real run."""
    source = _rules()
    formation = len(_ts_items(source, "FORMATION_ROWS")) * _ts_int(source, "FORMATION_COLUMNS")
    best_invader = max(int(v) for v in _ts_items(source, "INVADER_POINTS"))
    multiplier = _ts_int(source, "MAX_MULTIPLIER")
    every = _ts_int(source, "BOSS_WAVE_EVERY")
    clear = [_ts_int(source, f"WAVE_CLEAR_BONUS_{part}") for part in ("BASE", "STEP", "MAX")]
    boss = [_ts_int(source, f"BOSS_POINTS_{part}") for part in ("BASE", "STEP", "MAX")]
    minions = len(_ts_items(source, "OVERFLOW_SPLIT_THRESHOLDS")) * _ts_int(source, "MINIONS_PER_SPLIT")
    minion_points = _ts_int(source, "MINION_POINTS")
    for wave in range(1, 301):
        if wave % every == 0:
            nth = wave // every
            enemies = min(boss[2], boss[0] + boss[1] * (nth - 1)) + minions * minion_points * multiplier
        else:
            enemies = formation * best_invader * multiplier
        # Every wave can end on its clear bonus, a boss wave too (test_a_boss_wave_holds_its_clear_bonus_too).
        expected = enemies + min(clear[2], clear[0] + clear[1] * (wave - 1))
        assert arcade.max_wave_score(wave) == expected, f"wave {wave}"


def test_what_the_waves_do_not_bound_is_counted_at_the_game_s_values():
    source = _rules()
    saucers = [
        *(int(v) for v in _ts_items(source, "UFO_REGULAR_TABLE")),
        *(_ts_int(source, name) for name in ("UFO_LEGENDARY_POINTS", "UFO_DUCK_POINTS", "UFO_PI_POINTS")),
    ]
    assert max(saucers) == arcade.MAX_UFO_POINTS
    destroyed = max(_ts_int(source, "UI_DESTROYED_POINTS"), _ts_int(source, "NUKE_UI_POINTS"))
    assert destroyed == arcade.UI_DESTROYED_POINTS
    assert _ts_int(source, "UI_CRATER_POINTS") == arcade.UI_CRATER_POINTS
    assert _ts_int(source, "PAYLOAD_POINTS") == arcade.PAYLOAD_POINTS
    assert _ts_int(source, "PAYLOAD_INTERVAL_MS") == arcade.PAYLOAD_INTERVAL_SECONDS * 1000
    rotation = [kind.strip('"') for kind in _ts_items(source, "BOSS_ROTATION")]
    first_injector = (rotation.index("promptInjector") + 1) * _ts_int(source, "BOSS_WAVE_EVERY")
    assert first_injector == arcade.FIRST_PAYLOAD_WAVE
    assert _ts_int(source, "SURVIVAL_BONUS_MAX") == arcade.SURVIVAL_BONUS_MAX
    assert _ts_int(source, "PACIFIST_BONUS") == arcade.PACIFIST_BONUS
    assert _ts_int(source, "DECOY_POINTS") == 0


#: Every scoring constant of engine/rules.ts. A new one is a new way to score:
#: the ceiling must learn it (and this list with it) before a run can use it.
_SCORING_CONSTANTS = {
    "INVADER_POINTS",
    "UFO_LEGENDARY_POINTS",
    "UFO_DUCK_POINTS",
    "UFO_PI_POINTS",
    "WAVE_CLEAR_BONUS_BASE",
    "WAVE_CLEAR_BONUS_STEP",
    "WAVE_CLEAR_BONUS_MAX",
    "BOSS_POINTS_BASE",
    "BOSS_POINTS_STEP",
    "BOSS_POINTS_MAX",
    "SURVIVAL_BONUS_MAX",
    "UI_CRATER_POINTS",
    "UI_DESTROYED_POINTS",
    "NUKE_UI_POINTS",
    "PACIFIST_BONUS",
    "PAYLOAD_POINTS",
    "DECOY_POINTS",
    "MINION_POINTS",
    # The plausibility rate, pinned above.
    "MAX_POINTS_PER_SECOND",
}


def test_every_scoring_constant_of_the_game_is_one_the_ceiling_knows():
    found = set(re.findall(r"export const ([A-Z0-9_]*(?:POINTS|BONUS)[A-Z0-9_]*)\b", _rules()))
    assert found == _SCORING_CONSTANTS


#: Every place engine/game.ts adds to the score, and the term of the ceiling that counts it.
_SCORING_SITES = Counter(
    {
        "survivalBonus(waitSeconds)": 1,  # once per game
        "bonus": 1,  # the wave-clear bonus (per wave)
        "points": 2,  # an invader at its multiplier (per wave), a saucer (ufo_kills)
        "MINION_POINTS * multiplierFor(this.combo)": 1,  # a Token Overflow minion (per wave)
        "bossPoints(this.wave)": 1,  # per boss wave
        "PAYLOAD_POINTS": 1,  # the Prompt Injector's payloads (by time)
        "destroyPoints": 1,  # a crumbled element (ui_destroyed)
        "PACIFIST_BONUS": 1,  # once per game
    }
)


def test_every_place_the_game_scores_is_one_the_ceiling_counts():
    source = (FRONTEND_ENGINE / "game.ts").read_text(encoding="utf-8")
    assert Counter(arg.strip() for arg in re.findall(r"this\.addScore\(([^,]+),", source)) == _SCORING_SITES
    # Outside addScore itself, only a crater adds to the score (the crater cap)...
    assert re.findall(r"this\.score \+= ([^;]+);", source) == ["UI_CRATER_POINTS", "Math.round(points)"]
    # ...and nothing else writes it, whatever the spelling (`=`, `-=`, `++`).
    assert len(_writes(source, "score")) == 2
    # The values the ceiling reads from rules.ts are the ones the game plays with.
    for name in ("UFO_DUCK_POINTS", "UFO_PI_POINTS", "PAYLOAD_INTERVAL_MS", "OVERFLOW_SPLIT_THRESHOLDS", "MINIONS_PER_SPLIT"):
        assert re.search(rf"\b{name}\b", source), f"game.ts no longer uses {name}"


def test_the_crater_cap_covers_every_hit_an_element_takes_before_it_crumbles():
    """A hit that does not crumble an element scores a crater. An element has at
    most N hit points and the gentlest hit takes d, so it takes no more than
    ceil(N / d) hits; a game is paid for the damage to at most
    MAX_UI_DESTROYED_PER_RUN elements (``DamageResult.scored``)."""
    damage = (FRONTEND_ENGINE / "screen-damage.ts").read_text(encoding="utf-8")
    hit_points = re.search(r"const maxHp = Math\.min\(([0-9.]+),", damage)
    assert hit_points, "the elements' hit point cap was not found in screen-damage.ts"
    game = (FRONTEND_ENGINE / "game.ts").read_text(encoding="utf-8")
    # Every hit's damage is a literal (or Infinity, which crumbles at once): the
    # gentlest one is only readable that way.
    assert not re.search(r"[{,]\s*damage: (?!Infinity\b)[A-Za-z_]", game), "a hit damage is no longer a literal in game.ts"
    hits = [float(v) for v in re.findall(r"[{,]\s*damage: ([0-9]*\.?[0-9]+)", game)]
    assert hits, "no hit damage found in game.ts"
    assert math.ceil(float(hit_points.group(1)) / min(hits)) <= arcade.CRATER_HITS_PER_ELEMENT
    assert arcade.MAX_CRATER_POINTS == (
        arcade.MAX_UI_DESTROYED_PER_RUN * arcade.CRATER_HITS_PER_ELEMENT * arcade.UI_CRATER_POINTS
    )


def test_a_game_counts_no_more_destroyed_elements_than_the_server_accepts():
    """Past the elements a game is paid for - recycled room, a nuke on a busy
    page - the interface still falls, for show: the game counts and pays only
    ``scored`` damage and stops COUNTING at MAX_SCORED_ELEMENTS, so the server's
    bound can never refuse a real run."""
    assert _ts_int(_rules(), "MAX_SCORED_ELEMENTS") == arcade.MAX_UI_DESTROYED_PER_RUN
    game = (FRONTEND_ENGINE / "game.ts").read_text(encoding="utf-8")
    # One place counts a destroyed element, whatever the spelling, and both gates guard that very place.
    assert _writes(game, "uiDestroyed") == ["this.uiDestroyed +="]
    guard = re.search(r"if \(([^)]*)\) \{\s*this\.uiDestroyed \+= 1;", game)
    assert guard, "the destroyed count is no longer incremented under an if"
    assert "this.uiDestroyed < MAX_SCORED_ELEMENTS" in guard.group(1), "game.ts no longer caps the destroyed count"
    assert "result.scored" in guard.group(1), "game.ts counts destroyed elements the game is not paid for"
    # The crater cap counts the paid elements only: so does the crater line.
    crater = re.search(r"else if \(([^)]*)\) \{\s*this\.score \+= UI_CRATER_POINTS;", game)
    assert crater, "the crater points are no longer scored under an else if"
    assert "result.scored" in crater.group(1), "game.ts pays craters on elements the game is not paid for"


def test_the_client_beats_at_the_server_s_cadence_when_the_server_says_nothing():
    source = (FRONTEND_ENGINE.parent / "heartbeat.ts").read_text(encoding="utf-8")

    def constant(name: str) -> int:
        match = re.search(rf"export const {name} = ([0-9_]+);", source)
        assert match, f"{name} not found in heartbeat.ts"
        return int(match.group(1).replace("_", ""))

    assert constant("DEFAULT_HEARTBEAT_SECONDS") == arcade.HEARTBEAT_SECONDS
    # The client's clamp never overrides what the server says...
    assert constant("MIN_HEARTBEAT_SECONDS") <= arcade.HEARTBEAT_SECONDS <= constant("MAX_HEARTBEAT_SECONDS")
    # ...and the live room forgives three missed beats of it.
    assert arcade.LIVE_TTL_SECONDS > 3 * arcade.HEARTBEAT_SECONDS
