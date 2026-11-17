"""Whitelist layering against real Postgres (FR-001d/e/f).

The unit suite fakes the database, so it proves the *arithmetic* of
``(shipped_default - removals) | additions`` but not that the overlay actually
persists, that the unique constraint behaves, or that a real upgrade leaves
operator decisions intact.

The rule that matters most is FR-001e: an administrator's removal must survive a
release that puts the tool back into the shipped default. Getting it wrong
silently re-exempts a tool somebody deliberately gated — the failure mode is
invisible, which is why it is worth proving against the real thing.

An "upgrade" is simulated by swapping ``SHIPPED_DEFAULT_WHITELIST``, which is
exactly what deploying new code does: the default is a constant, not data.

The same rule has to hold for the derived half of the default — the
read-classified tools of the registered platforms — and that half depends on a
real ``platform_registrations`` row, which is precisely what the unit suite
cannot fake convincingly. The last tests here cover it end to end.
"""

from __future__ import annotations

import contextlib
import os
import urllib.parse
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models as _models  # noqa: F401 — registers every model
from app.core.database import Base
from app.models.tool_approval import HitlWhitelistOverride
from app.services import hitl_whitelist
from app.services.hitl_whitelist import (
    ACTION_ADD,
    ACTION_REMOVE,
    clear_override,
    resolve_whitelist,
    set_override,
)

pytestmark = pytest.mark.integration


def _swap_database(url: str, new_database: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return parsed._replace(path=f"/{new_database}").geturl()


@contextlib.asynccontextmanager
async def _temporary_database():
    base_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot",
    )
    db_name = f"copilot_hitl_layering_{uuid.uuid4().hex[:12]}"
    admin_url = _swap_database(base_url, "postgres")
    fresh_url = _swap_database(base_url, db_name)

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    except (DBAPIError, OperationalError, ProgrammingError) as exc:
        await admin_engine.dispose()
        orig = getattr(exc, "orig", None)
        pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
        if pgcode != "42501":
            raise
        pytest.skip(
            "Cannot create a dedicated test database — the DATABASE_URL user "
            f"needs CREATEDB (SQLSTATE 42501). Original: {exc}"
        )
    await admin_engine.dispose()

    schema_engine = create_async_engine(fresh_url)
    try:
        try:
            async with schema_engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await schema_engine.dispose()
        yield fresh_url
    finally:
        admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin_engine.connect() as conn:
                await conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :db AND pid <> pg_backend_pid()"
                    ),
                    {"db": db_name},
                )
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        finally:
            await admin_engine.dispose()


@pytest.fixture
async def session():
    async with _temporary_database() as url:
        engine = create_async_engine(url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as s:
                yield s
        finally:
            await engine.dispose()


@pytest.fixture
def shipped(monkeypatch):
    """A small, known shipped default so tests do not track the real list."""
    default = frozenset({"get_agent", "list_agents", "search_memory"})
    monkeypatch.setattr(hitl_whitelist, "SHIPPED_DEFAULT_WHITELIST", default)
    return default


def _ship(monkeypatch, *names: str) -> None:
    """Simulate deploying a release with a different shipped default."""
    monkeypatch.setattr(
        hitl_whitelist, "SHIPPED_DEFAULT_WHITELIST", frozenset(names)
    )


async def test_no_overrides_yields_the_shipped_default(session, shipped):
    assert await resolve_whitelist(session) == shipped


async def test_addition_persists_and_applies(session, shipped):
    await set_override(
        session, tool_key="delete_entity", action=ACTION_ADD, user_id=None
    )
    await session.commit()

    assert "delete_entity" in await resolve_whitelist(session)


async def test_removal_persists_and_applies(session, shipped):
    await set_override(
        session, tool_key="get_agent", action=ACTION_REMOVE, user_id=None
    )
    await session.commit()

    effective = await resolve_whitelist(session)
    assert "get_agent" not in effective
    assert "list_agents" in effective


async def test_removal_survives_an_upgrade_that_re_adds_the_tool(
    session, shipped, monkeypatch
):
    """FR-001e, proven end to end. The dangerous direction.

    An administrator re-gates a tool; a later release ships it exempt again. If
    the removal did not win, the upgrade would silently un-gate something
    somebody deliberately gated, and nothing would surface it.
    """
    await set_override(
        session, tool_key="get_agent", action=ACTION_REMOVE, user_id=None
    )
    await session.commit()
    assert "get_agent" not in await resolve_whitelist(session)

    _ship(monkeypatch, "get_agent", "list_agents", "search_memory", "brand_new_tool")

    effective = await resolve_whitelist(session)
    assert "get_agent" not in effective, "upgrade re-exempted a re-gated tool"
    assert "brand_new_tool" in effective, "new default entry did not apply (FR-001f)"


async def test_addition_survives_an_upgrade_that_omits_it(
    session, shipped, monkeypatch
):
    await set_override(
        session, tool_key="delete_entity", action=ACTION_ADD, user_id=None
    )
    await session.commit()

    _ship(monkeypatch, "list_agents")

    assert "delete_entity" in await resolve_whitelist(session)


async def test_re_adding_flips_the_row_rather_than_duplicating(session, shipped):
    """One decision per tool — the unique constraint is real, not aspirational."""
    await set_override(
        session, tool_key="get_agent", action=ACTION_REMOVE, user_id=None
    )
    await session.commit()

    await set_override(
        session, tool_key="get_agent", action=ACTION_ADD, user_id=None
    )
    await session.commit()

    count = await session.scalar(
        text("SELECT count(*) FROM hitl_whitelist_overrides WHERE tool_key='get_agent'")
    )
    assert count == 1
    assert "get_agent" in await resolve_whitelist(session)


async def test_duplicate_tool_key_is_rejected_by_the_database(session, shipped):
    """The constraint must hold even if application code ever bypasses upsert."""
    session.add(HitlWhitelistOverride(tool_key="dupe", action=ACTION_ADD))
    await session.commit()

    session.add(HitlWhitelistOverride(tool_key="dupe", action=ACTION_REMOVE))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_clearing_an_override_restores_the_shipped_state(session, shipped):
    await set_override(
        session, tool_key="get_agent", action=ACTION_REMOVE, user_id=None
    )
    await session.commit()
    assert "get_agent" not in await resolve_whitelist(session)

    await clear_override(session, "get_agent")
    await session.commit()

    assert "get_agent" in await resolve_whitelist(session)


async def test_a_tool_in_neither_layer_is_not_whitelisted(session, shipped):
    """Absence is the safe state, and it survives a real round-trip."""
    assert "never_mentioned_tool" not in await resolve_whitelist(session)


# ── The derived platform-read layer, end to end ──────────────────


async def _register(session, identifier: str = "opencti") -> None:
    """Insert the registration row that makes a platform's reads exempt."""
    from app.models.platform_registration import PlatformRegistration

    session.add(
        PlatformRegistration(
            platform_identifier=identifier,
            platform_url="https://platform.example.com",
            platform_title=identifier,
            platform_version="6.9.0",
        )
    )
    await session.commit()


@pytest.fixture
def platform_reads(monkeypatch):
    """A small, known derived set so these do not track the real 107 names."""
    derived = frozenset({"search_opencti", "list_opencti_entities"})
    monkeypatch.setattr(
        hitl_whitelist,
        "read_tools_for",
        lambda providers: derived if "opencti" in providers else frozenset(),
    )
    return derived


async def test_a_registration_makes_its_read_tools_exempt(
    session, shipped, platform_reads
):
    assert await resolve_whitelist(session) == shipped
    await _register(session)
    assert await resolve_whitelist(session) == shipped | platform_reads


async def test_an_unregistered_platform_releases_its_read_tools(
    session, shipped, platform_reads
):
    """Disconnecting a platform re-gates the tools it was vouching for.

    ``platform_approval_by_provider`` excludes the terminal status, so this
    also pins that the exclusion reaches the derived whitelist and not only the
    per-source exemption it was written for.
    """
    from sqlalchemy import select as _select

    from app.models.platform_registration import PlatformRegistration

    await _register(session)
    registration = await session.scalar(_select(PlatformRegistration))
    registration.status = "unregistered"
    await session.commit()

    assert await resolve_whitelist(session) == shipped


async def test_re_gating_a_platform_read_survives_the_platform_reconnecting(
    session, shipped, platform_reads
):
    """The dangerous direction, for the derived half.

    An operator who decided a platform read must ask cannot have that undone by
    a heartbeat, an upgrade, or a reconnection — the same guarantee FR-001e
    gives the curated half.
    """
    await _register(session)
    await set_override(
        session, tool_key="search_opencti", action=ACTION_REMOVE, user_id=None
    )
    await session.commit()

    assert "search_opencti" not in await resolve_whitelist(session)

    # Re-registering (the heartbeat) and shipping a new curated default must
    # both leave the decision standing.
    await session.execute(
        text(
            "UPDATE platform_registrations SET platform_version = '7.0.0' "
            "WHERE platform_identifier = 'opencti'"
        )
    )
    await session.commit()

    assert "search_opencti" not in await resolve_whitelist(session)
    assert "list_opencti_entities" in await resolve_whitelist(session)


async def test_openaev_legacy_registration_governs_the_canonical_provider(
    session, shipped, monkeypatch
):
    """A platform registered as ``openbas`` still exempts ``openaev`` reads.

    Older installs register under the legacy identifier while their integration
    is created under the canonical one. Without the alias expansion their read
    tools would go on gating with nothing to explain why.
    """
    seen: list[frozenset[str]] = []

    def _capture(providers):
        seen.append(providers)
        return frozenset({"get_openaev_asset"}) if "openaev" in providers else frozenset()

    monkeypatch.setattr(hitl_whitelist, "read_tools_for", _capture)

    await _register(session, identifier="openbas")

    assert "get_openaev_asset" in await resolve_whitelist(session)
    assert any("openaev" in providers for providers in seen)


async def test_concurrent_overrides_on_one_tool_converge(session, shipped):
    """Two administrators deciding about the same tool at once must not collide.

    `set_override` is an `ON CONFLICT` upsert rather than select-then-insert.
    The earlier shape read first and inserted when it saw no row, so two
    concurrent callers both saw none, both inserted, and one died on
    `UNIQUE (tool_key)` — the opposite of the convergence Principle II asks
    for. One row per request made that window narrow; the bulk platform-read
    actions write dozens per transaction and widen it proportionally.

    Two sessions on the same database, committing in sequence with both having
    read "no such row" first, is the shape that used to raise.
    """
    from sqlalchemy import func
    from sqlalchemy import select as _select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(session.bind.engine.url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as a, maker() as b:
            # Both observe the absence first — the read that made the old shape
            # decide to INSERT.
            assert await a.scalar(
                _select(func.count()).select_from(HitlWhitelistOverride)
            ) == 0
            assert await b.scalar(
                _select(func.count()).select_from(HitlWhitelistOverride)
            ) == 0

            await set_override(
                a, tool_key="search_opencti", action=ACTION_REMOVE, user_id=None
            )
            await a.commit()

            await set_override(
                b, tool_key="search_opencti", action=ACTION_ADD, user_id=None
            )
            await b.commit()

        # One row, last writer wins — converged rather than raised.
        rows = (
            await session.execute(
                _select(HitlWhitelistOverride.tool_key, HitlWhitelistOverride.action)
            )
        ).all()
        assert rows == [("search_opencti", ACTION_ADD)]
    finally:
        await engine.dispose()


async def test_an_unreadable_registration_leaves_the_session_usable(session, shipped):
    """The refusal must not poison the transaction it refuses in.

    Postgres aborts the whole transaction on a failed statement, so a caller
    that reads the overlay *after* asking about the derived layer used to hit
    the aborted transaction rather than the refusal — a 500 from
    ``POST /hitl-whitelist`` where ``DELETE`` (which reads the overlay first)
    answered a clean 503. Found walking quickstart Scenario 5c against a live
    deployment; unit tests could not see it, because they raise the error
    directly instead of failing a real query.
    """
    from app.services.hitl_whitelist import (
        PlatformReadsUnavailableError,
        load_overrides,
        platform_read_default,
    )

    # CASCADE: an FK from ctem_metrics depends on the table. Renaming
    # instead would leave the FK pointing at the renamed table, which still
    # resolves — the read has to actually fail.
    await session.execute(text("DROP TABLE platform_registrations CASCADE"))
    await session.commit()

    with pytest.raises(PlatformReadsUnavailableError):
        await platform_read_default(session)

    # The whole point: the caller can still do its own work, so the endpoint
    # reaches its own error handling instead of dying on the dead transaction.
    assert await load_overrides(session) == {}
    await set_override(
        session, tool_key="get_agent", action=ACTION_REMOVE, user_id=None
    )
    await session.commit()
    assert await load_overrides(session) == {"get_agent": ACTION_REMOVE}
