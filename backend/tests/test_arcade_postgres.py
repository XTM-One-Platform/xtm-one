"""Real-PostgreSQL execution of the waiting arcade's leaderboard SQL.

``services/arcade.py`` builds the boards with ``DISTINCT ON`` (best run per
player, the earlier run winning a tie), a window rank and count over the
visible players, and a visibility filter that must hide an opted-out player
from everyone but themselves. None of that can be checked against a fake
session, so it runs here against a real database, in an isolated temporary one.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.arcade import ArcadeProfile, ArcadeScore
from app.models.user import User
from app.services import arcade
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

_TABLES = [User.__table__, ArcadeScore.__table__, ArcadeProfile.__table__]
_IDENTIFYING = {"user_id", "display_name", "full_name", "name", "email"}


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    """Isolated database with just the arcade tables, dropped on exit."""
    async with scratch_database("copilot_arcade", tables=_TABLES) as db:
        yield db.sessions


NOW = datetime.now(UTC)


def _user(name: str, *, active: bool = True) -> User:
    local = name.lower().replace(" ", ".")
    return User(id=uuid.uuid4(), email=f"{local}@example.com", full_name=name, is_active=active)


def _run(user: User, score: int, *, ago: timedelta, ranked: bool = True, **stats) -> ArcadeScore:
    return ArcadeScore(
        user_id=user.id,
        score=score,
        ranked=ranked,
        created_at=NOW - ago,
        wave=stats.pop("wave", 3),
        **stats,
    )


async def _seed(session) -> dict[str, User]:
    people = {
        "alice": _user("Alice Martin"),
        "bob": _user("Bob Stone"),
        "carol": _user("Carol Hidden"),
        "dave": _user("Dave Gone", active=False),
        "erin": _user("Erin Zero"),
        "frank": _user("Frank Cheat"),
        "gina": _user("Gina Old"),
    }
    session.add_all(people.values())
    await session.flush()
    session.add_all(
        [
            _run(people["alice"], 1_000, ago=timedelta(hours=2), shots=200, hits=150),
            _run(people["alice"], 5_000, ago=timedelta(hours=1), shots=200, hits=150),
            # Same best score as Alice, reached earlier: Bob ranks above her.
            _run(people["bob"], 5_000, ago=timedelta(hours=3), shots=200, hits=190),
            # Opted out: the best score and the most wreckage of the day.
            _run(people["carol"], 9_000, ago=timedelta(hours=1), ui_destroyed=500),
            _run(people["dave"], 20_000, ago=timedelta(hours=1)),
            # A zero-point run holds nothing: no rank, no title.
            _run(people["erin"], 0, ago=timedelta(hours=1), ui_destroyed=300),
            _run(people["frank"], 30_000, ago=timedelta(hours=1), ranked=False),
            # Three days ago: on the week and all-time boards, not the day's.
            _run(people["gina"], 7_000, ago=timedelta(days=3)),
        ]
    )
    session.add(ArcadeProfile(user_id=people["carol"].id, hidden=True, callsign="CAR"))
    session.add(ArcadeProfile(user_id=people["bob"].id, callsign="BOB"))
    await session.commit()
    return people


def _viewer(user: User) -> SimpleNamespace:
    return SimpleNamespace(id=user.id, full_name=user.full_name, email=user.email)


def _board(result: dict) -> list[tuple[int, str, int, bool]]:
    return [(e["rank"], e["callsign"], e["best_score"], e["is_self"]) for e in result["entries"]]


async def test_the_boards_rank_best_runs_and_list_only_visible_players():
    async with _temp_database() as factory, factory() as session:
        people = await _seed(session)
        alice = _viewer(people["alice"])

        day = await arcade.leaderboard(session, alice, period="day")
        # Carol opted out, Dave is deactivated, Erin scored nothing, Frank
        # cheated, Gina played three days ago; Bob's 5,000 came first.
        assert _board(day) == [(1, "BOB", 5_000, False), (2, "AMA", 5_000, True)]
        assert day["total_players"] == 2
        assert day["me"]["rank"] == 2 and day["me"]["games"] == 2
        assert day["titles"]["top_vandal"] is None

        week = await arcade.leaderboard(session, alice, period="week")
        assert _board(week) == [(1, "GOL", 7_000, False), (2, "BOB", 5_000, False), (3, "AMA", 5_000, True)]
        assert week["total_players"] == 3
        all_time = await arcade.leaderboard(session, alice, period="all")
        assert _board(all_time) == _board(week)

        # The top of the board and the viewer's own row come from one pass.
        top = await arcade.leaderboard(session, alice, period="week", limit=1)
        assert _board(top) == [(1, "GOL", 7_000, False)]
        assert top["me"]["rank"] == 3 and top["total_players"] == 3

        for board in (day, week, all_time, top):
            for entry in [*board["entries"], board["me"]]:
                assert not _IDENTIFYING & set(entry)


async def test_an_opted_out_player_is_hidden_from_every_other_view():
    async with _temp_database() as factory, factory() as session:
        people = await _seed(session)
        alice, bob, carol = (_viewer(people[name]) for name in ("alice", "bob", "carol"))

        for period in arcade.PERIODS:
            seen_by_alice = await arcade.leaderboard(session, alice, period=period)
            assert "CAR" not in {e["callsign"] for e in seen_by_alice["entries"]}
            titles = seen_by_alice["titles"]
            assert all(t is None or t["callsign"] != "CAR" for t in titles.values())
            assert all(t is None or not _IDENTIFYING & set(t) for t in titles.values())

        # The rival ladder skips her too: only Gina is above Bob this week.
        ladder = await arcade.rival_ladder(session, bob)
        assert [(r["callsign"], r["score"]) for r in ladder["ladder"]] == [("GOL", 7_000)]
        assert ladder["leader"]["callsign"] == "GOL" and ladder["week_rank"] == 2
        assert not _IDENTIFYING & set(ladder["leader"])
        assert await arcade.rank_of(session, bob, "week") == 2

        # She still sees her own rank, among the visible players plus herself,
        # and the title she holds.
        own = await arcade.leaderboard(session, carol, period="day")
        assert _board(own)[0] == (1, "CAR", 9_000, True)
        assert own["total_players"] == 3
        assert own["titles"]["top_vandal"]["callsign"] == "CAR"
        assert own["titles"]["top_vandal"]["is_self"] is True


async def test_a_recorded_run_lands_ranked_and_merges_achievements():
    async with _temp_database() as factory, factory() as session:
        people = await _seed(session)
        alice = people["alice"]
        session_payload = {"user_id": str(alice.id), "started_at": arcade._now() - 120, "mode": "free"}
        stats = arcade.RunStats(
            score=12_000, wave=4, kills=150, shots=300, hits=200, ui_destroyed=3,
            bosses=0, max_combo=20, ufo_kills=1, duration_ms=110_000,
        )
        first = await arcade.record_run(
            session, alice, session=session_payload, stats=stats, cheat=False,
            claimed_achievements=["first_blood", "lucky_23", "wave_10"],
        )
        await session.commit()
        assert first["ranked"] and first["personal_best"] and first["previous_best"] == 5_000
        assert first["new_achievements"] == ["first_blood", "lucky_23"]

        second = await arcade.record_run(
            session, alice, session=session_payload, stats=stats, cheat=False,
            claimed_achievements=["first_blood", "night_owl"],
        )
        await session.commit()
        assert second["new_achievements"] == ["night_owl"]
        profile = (await session.execute(select(ArcadeProfile).where(ArcadeProfile.user_id == alice.id))).scalar_one()
        assert set(profile.achievements) == {"first_blood", "lucky_23", "night_owl"}
        assert await arcade.personal_best(session, alice.id) == 12_000
        assert await arcade.rank_of(session, _viewer(alice), "day") == 1


async def test_a_run_over_the_ceiling_of_its_waves_writes_nothing():
    """An hour-old session licenses 5.4 million points by time alone; at wave 1
    the run is refused before a row, a profile or an achievement is written."""
    async with _temp_database() as factory, factory() as session:
        people = await _seed(session)
        alice = people["alice"]
        session_payload = {"user_id": str(alice.id), "started_at": arcade._now() - 3600, "mode": "free"}
        stats = arcade.RunStats(
            score=5_000_000, wave=1, kills=55, shots=300, hits=200, ui_destroyed=0,
            bosses=0, max_combo=20, ufo_kills=0, duration_ms=3_600_000,
        )
        with pytest.raises(arcade.ArcadeError) as caught:
            await arcade.record_run(
                session, alice, session=session_payload, stats=stats, cheat=False,
                claimed_achievements=["first_blood"],
            )
        assert caught.value.message == "This score is more than the game can yield by wave 1."
        runs = await session.execute(
            select(func.count()).select_from(ArcadeScore).where(ArcadeScore.user_id == alice.id)
        )
        assert runs.scalar_one() == 2
        assert await arcade.personal_best(session, alice.id) == 5_000
        assert await session.get(ArcadeProfile, alice.id) is None


async def test_the_board_index_holds_the_best_run_order():
    """``DISTINCT ON (user_id) ORDER BY score DESC, created_at`` reads the
    index in order; the model and the migration must both declare it so."""
    async with _temp_database() as factory, factory() as session:
        indexdef = (
            await session.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_arcade_scores_user_best'")
            )
        ).scalar_one()
        assert "(user_id, score DESC, created_at)" in indexdef
