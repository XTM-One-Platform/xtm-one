"""Users that differ only by email case are merged, then the index forbids them.

SSO created ``Jane@x`` beside ``jane@x``. The revision folds every such group
into one account and indexes ``lower(email)``. A wrong survivor, a reference
left on a deleted row, or a ``CASCADE`` that ran before the move each destroy
what a person created under the SSO account, so it runs against real
PostgreSQL: the foreign keys are read from the catalog and the cascades are
the database's own.

The revision is loaded from its file, like the other migration tests here.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import app.models  # noqa: F401 - registers every table, so every FK onto users exists
from app.models.chat import Conversation
from app.models.group import Group, GroupMember
from app.models.llm_trace import LLMDailyStats
from app.models.nova_rule import NovaRule
from app.models.platform_settings import PlatformSettings
from app.models.quota import QuotaUsage
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "u6299emlcase01_merge_users_differing_by_email_case.py"
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _load_revision():
    spec = importlib.util.spec_from_file_location("u6299emlcase01", _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


revision = _load_revision()


@asynccontextmanager
async def _database() -> AsyncIterator[AsyncEngine]:
    """The pre-fix schema: every table, but not the index this revision adds."""
    async with scratch_database(
        "users_email_case", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        async with db.engine.begin() as conn:
            await conn.execute(text("DROP INDEX IF EXISTS ix_users_email_lower"))
        yield db.engine


async def _upgrade(engine: AsyncEngine) -> None:
    """Run the revision as ``env.py`` does: the context owns the transaction."""

    def _call(sync_conn) -> None:
        context = MigrationContext.configure(sync_conn)
        with Operations.context(context), context.begin_transaction():
            revision.upgrade()

    async with engine.connect() as conn:
        await conn.run_sync(_call)


async def _downgrade(engine: AsyncEngine) -> None:
    def _call(sync_conn) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            revision.downgrade()

    async with engine.begin() as conn:
        await conn.run_sync(_call)


async def _user(db: AsyncSession, email: str, *, age: int = 0, **fields) -> User:
    user = User(
        email=email,
        full_name=email,
        created_at=_NOW - timedelta(days=age),
        **fields,
    )
    db.add(user)
    await db.flush()
    return user


async def _emails(engine: AsyncEngine) -> list[str]:
    async with AsyncSession(engine, expire_on_commit=False) as db:
        return sorted((await db.execute(select(User.email))).scalars())


async def _conversation_owners(engine: AsyncEngine) -> set[uuid.UUID]:
    async with AsyncSession(engine, expire_on_commit=False) as db:
        return set((await db.execute(select(Conversation.user_id))).scalars())


async def test_the_lowercase_account_survives_and_inherits_what_the_sso_account_made():
    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            original = await _user(db, "jane@example.com", age=30, role="user", hashed_password="hash")
            sso = await _user(
                db,
                "Jane@Example.com",
                age=1,
                role="admin",
                sso_provider="oidc",
                sso_subject="idp|jane",
            )
            db.add(Conversation(user_id=sso.id))
            db.add(Conversation(user_id=original.id))
            await db.commit()
            original_id = original.id

        await _upgrade(engine)

        assert await _emails(engine) == ["jane@example.com"]
        async with AsyncSession(engine, expire_on_commit=False) as db:
            survivor = (await db.execute(select(User))).scalar_one()
            assert survivor.id == original_id
            assert survivor.hashed_password == "hash"
            assert survivor.role == "user"  # the survivor keeps its own role
            assert (survivor.sso_provider, survivor.sso_subject) == ("oidc", "idp|jane")
        assert await _conversation_owners(engine) == {original_id}


async def test_rows_the_survivor_already_has_an_equivalent_of_go_with_the_duplicate():
    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            original = await _user(db, "jane@example.com", age=30)
            sso = await _user(db, "JANE@example.com", age=1)
            shared, only_sso = Group(name="shared"), Group(name="only-sso")
            db.add_all([shared, only_sso])
            await db.flush()
            db.add_all(
                [
                    GroupMember(group_id=shared.id, user_id=original.id),
                    GroupMember(group_id=shared.id, user_id=sso.id),  # collides on UNIQUE
                    GroupMember(group_id=only_sso.id, user_id=sso.id),
                ]
            )
            await db.commit()
            original_id, shared_id, only_sso_id = original.id, shared.id, only_sso.id

        await _upgrade(engine)

        async with AsyncSession(engine, expire_on_commit=False) as db:
            members = (await db.execute(select(GroupMember))).scalars().all()
        assert sorted((str(m.group_id), m.user_id) for m in members) == sorted(
            [(str(shared_id), original_id), (str(only_sso_id), original_id)]
        )


async def test_sharing_lists_follow_the_survivor():
    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            original = await _user(db, "jane@example.com", age=30)
            sso = await _user(db, "Jane@example.com", age=1)
            owner = await _user(db, "owner@example.com")
            db.add(
                NovaRule(
                    user_id=owner.id,
                    name="Shared",
                    shared_user_ids=[str(sso.id)],
                    shared_user_permissions={str(sso.id).upper(): "read_only"},
                )
            )
            await db.commit()
            original_id = original.id

        await _upgrade(engine)

        async with AsyncSession(engine, expire_on_commit=False) as db:
            rule = (await db.execute(select(NovaRule))).scalar_one()
        assert rule.shared_user_ids == [str(original_id)]
        assert rule.shared_user_permissions == {str(original_id): "read_only"}


async def test_the_survivor_keeps_its_own_active_state_whatever_its_duplicate_had():
    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await _user(db, "active@example.com", age=30, is_active=True)
            await _user(db, "Active@example.com", age=1, is_active=False)  # disabled as a workaround
            await _user(db, "off@example.com", age=30, is_active=False)
            await _user(db, "Off@example.com", age=1, is_active=True)
            await db.commit()

        await _upgrade(engine)

        async with AsyncSession(engine) as db:
            state = dict((await db.execute(select(User.email, User.is_active))).all())
        assert state == {"active@example.com": True, "off@example.com": False}


async def test_with_no_lowercase_row_the_oldest_survives_lowercased():
    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            oldest = await _user(db, "Jane@Example.com", age=30)
            await _user(db, "JANE@EXAMPLE.COM", age=1)
            await _user(db, "Solo@Example.com")  # no twin: only lowercased
            await db.commit()
            oldest_id = oldest.id

        await _upgrade(engine)

        assert await _emails(engine) == ["jane@example.com", "solo@example.com"]
        async with AsyncSession(engine, expire_on_commit=False) as db:
            ids = set((await db.execute(select(User.id).where(User.email == "jane@example.com"))).scalars())
        assert ids == {oldest_id}


async def test_the_index_refuses_a_case_variant_and_the_revision_reruns_clean():
    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await _user(db, "jane@example.com")
            await db.commit()

        await _upgrade(engine)
        await _upgrade(engine)

        async with AsyncSession(engine, expire_on_commit=False) as db:
            db.add(User(email="Jane@Example.com", full_name="Jane"))
            with pytest.raises(IntegrityError):
                await db.commit()

        await _downgrade(engine)
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT to_regclass('ix_users_email_lower')"))
            ).scalar() is None


async def test_a_three_way_group_folds_into_one_account_and_each_child_follows():
    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            original = await _user(db, "jane@example.com", age=30)
            first = await _user(db, "Jane@example.com", age=20)
            second = await _user(db, "JANE@EXAMPLE.COM", age=10)
            for user in (first, second):
                db.add(Conversation(user_id=user.id))
            await db.commit()
            original_id = original.id

        await _upgrade(engine)

        assert await _emails(engine) == ["jane@example.com"]
        assert await _conversation_owners(engine) == {original_id}


async def test_the_survivors_own_sso_link_wins_over_a_duplicates():
    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await _user(db, "jane@example.com", age=30, sso_provider="saml", sso_subject="saml|jane")
            await _user(db, "Jane@example.com", age=1, sso_provider="oidc", sso_subject="idp|jane")
            await db.commit()

        await _upgrade(engine)

        async with AsyncSession(engine) as db:
            survivor = (await db.execute(select(User))).scalar_one()
        assert (survivor.sso_provider, survivor.sso_subject) == ("saml", "saml|jane")


async def test_a_user_id_column_with_no_foreign_key_follows_the_survivor():
    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            original = await _user(db, "jane@example.com", age=30)
            sso = await _user(db, "Jane@example.com", age=1)
            db.add(PlatformSettings(tou_accepted_by_user_id=sso.id))
            await db.commit()
            original_id = original.id

        await _upgrade(engine)

        async with AsyncSession(engine) as db:
            settings_row = (await db.execute(select(PlatformSettings))).scalar_one()
        assert settings_row.tou_accepted_by_user_id == original_id


async def test_a_case_variant_inserted_before_the_index_build_is_merged_on_the_next_round(
    monkeypatch,
):
    """A pod still on the old code can insert ``Jane@x`` between the merge and
    the index build; the build fails on it and the revision merges again."""
    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            original = await _user(db, "jane@example.com", age=30)
            await db.commit()
            original_id = original.id

        real = revision.create_index_in_place
        calls: list[int] = []

        def racing(conn, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                conn.execute(
                    text(
                        "INSERT INTO users (id, email, full_name, role, is_active, language, "
                        "ctem_vertical, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), 'Jane@Example.com', 'Jane', 'user', true, 'auto', "
                        "'cyber', now(), now())"
                    )
                )
            return real(conn, *args, **kwargs)

        monkeypatch.setattr(revision, "create_index_in_place", racing)

        await _upgrade(engine)

        assert len(calls) == 2
        assert await _emails(engine) == ["jane@example.com"]
        async with AsyncSession(engine) as db:
            assert (await db.execute(select(User.id))).scalar_one() == original_id


async def test_a_list_shared_with_both_twins_keeps_one_entry_at_the_highest_tier():
    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            original = await _user(db, "jane@example.com", age=30)
            sso = await _user(db, "Jane@example.com", age=1)
            owner = await _user(db, "owner@example.com")
            db.add_all(
                [
                    NovaRule(  # the duplicate holds the higher tier, listed first
                        user_id=owner.id,
                        name="Loser higher",
                        shared_user_ids=[str(sso.id), str(original.id)],
                        shared_user_permissions={
                            str(sso.id): "config_with_chats",
                            str(original.id): "read_only",
                        },
                    ),
                    NovaRule(  # the survivor holds the higher tier, listed first
                        user_id=owner.id,
                        name="Winner higher",
                        shared_user_ids=[str(original.id), str(sso.id)],
                        shared_user_permissions={
                            str(original.id): "config_with_chats",
                            str(sso.id): "read_only",
                        },
                    ),
                ]
            )
            await db.commit()
            original_id = str(original.id)

        await _upgrade(engine)

        async with AsyncSession(engine) as db:
            rules = (await db.execute(select(NovaRule))).scalars().all()
        assert len(rules) == 2
        for rule in rules:
            assert rule.shared_user_ids == [original_id]
            assert rule.shared_user_permissions == {original_id: "config_with_chats"}


async def test_a_twin_that_collides_with_the_lowercasing_is_merged_on_the_next_round(monkeypatch):
    """The lowercasing runs in the retried block too: a variant committed after
    the groups were read collides with the plain UNIQUE on ``email`` there."""
    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            original = await _user(db, "jane@example.com", age=30)
            await _user(db, "Solo@Example.com")  # gives the merge something to do
            await db.commit()
            original_id = original.id

        real = revision._merge_duplicates
        calls: list[int] = []

        def racing(conn):
            real(conn)
            calls.append(1)
            if len(calls) == 1:
                conn.execute(
                    text(
                        "INSERT INTO users (id, email, full_name, role, is_active, language, "
                        "ctem_vertical, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), 'Jane@Example.com', 'Jane', 'user', true, 'auto', "
                        "'cyber', now(), now())"
                    )
                )

        monkeypatch.setattr(revision, "_merge_duplicates", racing)

        await _upgrade(engine)

        assert len(calls) == 2
        assert await _emails(engine) == ["jane@example.com", "solo@example.com"]
        async with AsyncSession(engine) as db:
            kept = (await db.execute(select(User.id).where(User.email == "jane@example.com"))).scalar_one()
        assert kept == original_id


async def test_daily_usage_rollups_add_up_on_the_survivor():
    day, other_day = date(2026, 1, 1), date(2026, 1, 2)
    agent = uuid.uuid4()

    def stats(user, on, trace_count, cost, duration, tokens=10):
        return LLMDailyStats(
            date=on,
            agent_id=agent,
            user_id=user,
            model="m",
            source="chat",
            trace_count=trace_count,
            error_count=1,
            total_input_tokens=tokens,
            total_output_tokens=tokens,
            total_cost=cost,
            avg_duration_ms=duration,
        )

    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            survivor = await _user(db, "jane@example.com", age=30)
            first = await _user(db, "Jane@example.com", age=20)
            second = await _user(db, "JANE@EXAMPLE.COM", age=10)
            db.add_all(
                [
                    stats(survivor.id, day, 1, 1, 100),  # the same key on all three
                    stats(first.id, day, 3, 2, 200),
                    stats(second.id, day, 4, None, 300),
                    stats(first.id, other_day, 2, 5, 50),  # the survivor has none that day
                    stats(second.id, other_day, 2, 1, 150),
                ]
            )
            await db.commit()
            survivor_id = survivor.id

        await _upgrade(engine)

        async with AsyncSession(engine) as db:
            rows = {r.date: r for r in (await db.execute(select(LLMDailyStats))).scalars()}
        assert set(rows) == {day, other_day}
        merged = rows[day]
        assert merged.user_id == survivor_id
        assert (merged.trace_count, merged.error_count, merged.total_input_tokens) == (8, 3, 30)
        assert float(merged.total_cost) == 3
        assert merged.avg_duration_ms == pytest.approx((100 * 1 + 200 * 3 + 300 * 4) / 8)
        carried = rows[other_day]
        assert carried.user_id == survivor_id
        assert (carried.trace_count, float(carried.total_cost)) == (4, 6)


async def test_quota_counters_add_up_on_the_survivor():
    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            survivor = await _user(db, "jane@example.com", age=30)
            first = await _user(db, "Jane@example.com", age=20)
            second = await _user(db, "JANE@EXAMPLE.COM", age=10)

            def usage(user, period, count):
                return QuotaUsage(
                    user_id=user.id, usage_type="agentic", model=None, period=period, count=count
                )

            db.add_all(
                [
                    usage(survivor, "2026-10", 5),
                    usage(first, "2026-10", 7),  # the same period on all three accounts
                    usage(second, "2026-10", 11),
                    usage(first, "2026-09", 3),  # a period only the duplicates have
                    usage(second, "2026-09", 4),
                ]
            )
            await db.commit()
            survivor_id = survivor.id

        await _upgrade(engine)

        async with AsyncSession(engine) as db:
            rows = (await db.execute(select(QuotaUsage))).scalars().all()
        assert {(r.user_id, r.period): r.count for r in rows} == {
            (survivor_id, "2026-10"): 23,
            (survivor_id, "2026-09"): 7,
        }


async def test_a_merged_day_adds_the_latency_histograms_and_takes_the_busier_percentiles():
    day, agent = date(2026, 1, 1), uuid.uuid4()

    def stats(user, traces, p50, buckets):
        return LLMDailyStats(
            date=day,
            agent_id=agent,
            user_id=user,
            model="m",
            source="chat",
            trace_count=traces,
            p50_duration_ms=p50,
            p95_duration_ms=p50 * 2,
            duration_histogram=None if buckets is None else {"v": 1, "b": buckets},
        )

    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            survivor = await _user(db, "jane@example.com", age=30)  # the less busy one
            duplicate = await _user(db, "Jane@example.com", age=1)
            db.add_all(
                [
                    stats(survivor.id, 1, 10.0, {"5": 1}),
                    stats(duplicate.id, 400, 90.0, {"5": 100, "9": 300}),
                ]
            )
            await db.commit()

        await _upgrade(engine)

        async with AsyncSession(engine) as db:
            row = (await db.execute(select(LLMDailyStats))).scalar_one()
        assert row.trace_count == 401
        assert row.duration_histogram == {"v": 1, "b": {"5": 101, "9": 300}}
        assert (row.p50_duration_ms, row.p95_duration_ms) == (90.0, 180.0)


async def test_a_merged_day_with_a_row_that_has_no_histogram_has_none():
    day = date(2026, 1, 1)
    async with _database() as engine:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            survivor = await _user(db, "jane@example.com", age=30)
            duplicate = await _user(db, "Jane@example.com", age=1)
            for user, histogram in ((survivor, None), (duplicate, {"v": 1, "b": {"5": 2}})):
                db.add(
                    LLMDailyStats(
                        date=day, user_id=user.id, model="m", source="chat",
                        trace_count=2, duration_histogram=histogram,
                    )
                )
            await db.commit()

        await _upgrade(engine)

        async with AsyncSession(engine) as db:
            row = (await db.execute(select(LLMDailyStats))).scalar_one()
        assert row.trace_count == 4
        assert row.duration_histogram is None
