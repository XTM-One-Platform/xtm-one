"""An upgrade switches the per-tool approval gate on for no agent.

Maintainer decision, 2026-09-27: on an upgraded install nothing pauses until an
administrator turns approval on, per agent or install-wide, including agents
created after the upgrade. Since ``o1ptin0173`` a fresh install is opt-in too.
Driven on a disposable Postgres database because what is under test is DDL and
defaults.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import urllib.parse
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models as _models  # noqa: F401 — side-effect: register all models
from app.core.database import Base
from app.services.hitl_whitelist import resolve_approve_tool_calls

pytestmark = pytest.mark.integration

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _swap_database(url: str, new_database: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return parsed._replace(path=f"/{new_database}").geturl()


@contextlib.asynccontextmanager
async def _temporary_database():
    base_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot",
    )
    db_name = f"copilot_approval_upgrade_{uuid.uuid4().hex[:12]}"
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
            f"needs the CREATEDB privilege (SQLSTATE 42501). Original: {exc}"
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
async def db_url():
    async with _temporary_database() as url:
        yield url


async def _seed_pre_upgrade_fleet(session, *, count: int = 5) -> list[uuid.UUID]:
    """Agents as main leaves them (HITL off), for the migration tests here and
    in ``test_reland_approval_schema_migration``."""
    from app.models.agent import Agent
    from app.models.user import User

    user = User(id=uuid.uuid4(), email=f"fleet-{uuid.uuid4().hex[:8]}@example.com", full_name="Fleet Owner")
    session.add(user)
    await session.flush()
    ids = []
    for i in range(count):
        agent = Agent(id=uuid.uuid4(), user_id=user.id, name=f"legacy-agent-{i}", human_in_the_loop=False)
        session.add(agent)
        ids.append(agent.id)
    await session.commit()
    return ids


def _run(stem: str):
    def _body(sync_conn) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = next(_VERSIONS.glob(f"{stem}_*.py"))
        spec = importlib.util.spec_from_file_location(f"_mig_{stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(sync_conn)):
            module.upgrade()

    return _body


async def _pre_upgrade_install(engine, sessionmaker) -> dict:
    """A main install: agents with HITL off and on, an assignment with it on."""
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.models.user import User

    async with sessionmaker() as session:
        await get_platform_settings(session)
        user = User(id=uuid.uuid4(), email=f"u-{uuid.uuid4().hex[:8]}@example.com", full_name="Owner")
        session.add(user)
        await session.flush()
        off = Agent(id=uuid.uuid4(), user_id=user.id, name="hitl-off", human_in_the_loop=False)
        on = Agent(id=uuid.uuid4(), user_id=user.id, name="hitl-on", human_in_the_loop=True)
        session.add_all([off, on])
        await session.flush()
        session.add(
            Assignment(
                id=uuid.uuid4(),
                user_id=user.id,
                agent_id=on.id,
                name="Draft replies for flagged emails",
                trigger_type="schedule",
                prompt="draft",
                human_in_the_loop=True,
            )
        )
        await session.commit()
        ids = {"off": off.id, "on": on.id, "user": user.id}
    # The columns the upgrade adds, gone as they are on main.
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE agents DROP COLUMN approve_tool_calls"))
        await conn.execute(text("ALTER TABLE platform_settings DROP COLUMN approve_tool_calls_default"))
    return ids


async def _upgrade(engine) -> None:
    for stem in ("e5rel3298", "j1atc0173"):
        async with engine.begin() as conn:
            await conn.run_sync(_run(stem))


async def _agent(session, agent_id):
    from app.models.agent import Agent

    return await session.get(Agent, agent_id)


@pytest.fixture
async def install():
    async with _temporary_database() as url:
        engine = create_async_engine(url)
        try:
            yield engine, async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()


async def test_the_upgrade_rewrites_no_agent_and_gates_none(install):
    engine, sessionmaker = install
    ids = await _pre_upgrade_install(engine, sessionmaker)
    async with sessionmaker() as session:
        before = (await session.execute(text("SELECT id, human_in_the_loop, updated_at FROM agents ORDER BY id"))).all()

    await _upgrade(engine)

    async with sessionmaker() as session:
        after = (await session.execute(text("SELECT id, human_in_the_loop, updated_at FROM agents ORDER BY id"))).all()
        assert after == before, "the upgrade wrote an agent row"
        assert await session.scalar(text("SELECT count(*) FROM agents WHERE approve_tool_calls IS NOT NULL")) == 0
        assert await session.scalar(text("SELECT approve_tool_calls_default FROM platform_settings")) is False
        # HITL on, agent and assignment alike, keeps main's meaning: no gate.
        for key in ("off", "on"):
            assert await resolve_approve_tool_calls(session, await _agent(session, ids[key])) is False


async def test_an_agent_created_after_the_upgrade_is_not_gated(install):
    engine, sessionmaker = install
    ids = await _pre_upgrade_install(engine, sessionmaker)
    await _upgrade(engine)
    from app.models.agent import Agent

    async with sessionmaker() as session:
        new = Agent(id=uuid.uuid4(), user_id=ids["user"], name="after-upgrade")
        session.add(new)
        await session.commit()
        assert new.approve_tool_calls is None
        assert await resolve_approve_tool_calls(session, new) is False
        # The General Assistant is no agent row and follows the same default.
        assert await resolve_approve_tool_calls(session, None) is False


async def test_turning_one_agent_on_gates_only_that_agent(install):
    engine, sessionmaker = install
    ids = await _pre_upgrade_install(engine, sessionmaker)
    await _upgrade(engine)
    async with sessionmaker() as session:
        chosen = await _agent(session, ids["off"])
        chosen.approve_tool_calls = True
        await session.commit()
        assert await resolve_approve_tool_calls(session, chosen) is True
        assert await resolve_approve_tool_calls(session, await _agent(session, ids["on"])) is False


async def test_rerunning_the_upgrade_keeps_the_install_default_off(install):
    engine, sessionmaker = install
    await _pre_upgrade_install(engine, sessionmaker)
    await _upgrade(engine)
    await _upgrade(engine)
    async with sessionmaker() as session:
        assert await session.scalar(text("SELECT approve_tool_calls_default FROM platform_settings")) is False


async def test_a_fresh_install_gates_no_agent_until_an_administrator_opts_in(install):
    """``create_all`` then the first boot's settings row: approvals are opt-in."""
    _engine, sessionmaker = install
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.models.agent import Agent
    from app.models.user import User

    async with sessionmaker() as session:
        settings_row = await get_platform_settings(session)
        await session.commit()
        assert settings_row.approve_tool_calls_default is False
        user = User(id=uuid.uuid4(), email=f"u-{uuid.uuid4().hex[:8]}@example.com", full_name="Owner")
        session.add(user)
        await session.flush()
        agent = Agent(id=uuid.uuid4(), user_id=user.id, name="fresh")
        session.add(agent)
        await session.commit()
        assert await resolve_approve_tool_calls(session, agent) is False
        assert await resolve_approve_tool_calls(session, None) is False

        settings_row.approve_tool_calls_default = True
        await session.commit()
        assert await resolve_approve_tool_calls(session, agent) is True


async def test_the_in_use_query_agrees_with_the_full_read(install, monkeypatch):
    """``approvals_in_use`` (one EXISTS) answers what ``load_approval_usage`` does."""
    import itertools

    _engine, sessionmaker = install
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.models.agent import Agent
    from app.models.user import User
    from app.services.tool_approval_usage import approvals_in_use, load_approval_usage

    chat = {"enabled": False}

    async def _chat(_db, **_kw):
        return chat["enabled"]

    monkeypatch.setattr("app.api.v1.platform.is_chat_enabled", _chat)
    async with sessionmaker() as session:
        settings_row = await get_platform_settings(session)
        user = User(id=uuid.uuid4(), email=f"u-{uuid.uuid4().hex[:8]}@example.com", full_name="Owner")
        session.add(user)
        await session.flush()
        agent = Agent(id=uuid.uuid4(), user_id=user.id, name="one")
        # A disabled agent never counts, whatever it is set to.
        session.add_all(
            [agent, Agent(id=uuid.uuid4(), user_id=user.id, name="off", approve_tool_calls=True, enabled=False)]
        )
        await session.commit()
        for own, default, enabled, assistant in itertools.product(
            (None, True, False), (False, True), (True, False), (False, True)
        ):
            agent.approve_tool_calls = own
            settings_row.approve_tool_calls_default = default
            settings_row.tool_approvals_enabled = enabled
            chat["enabled"] = assistant
            await session.commit()
            usage = await load_approval_usage(session)
            case = (own, default, enabled, assistant)
            assert await approvals_in_use(session) is usage.in_use, case
            expected = ["one"] if (own if own is not None else default) else []
            assert [a.name for a in usage.asking_agents] == expected, case


async def test_an_unreadable_in_use_answer_leaves_what_the_caller_loaded_usable(install, monkeypatch):
    """``GET /tool-approvals`` serializes the user it loaded after asking: a failed read must not expire it."""
    _engine, sessionmaker = install
    from app.models.user import User
    from app.services import tool_approval_usage

    async def _failing_read(db):
        await db.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(tool_approval_usage, "_stored_switches", _failing_read)
    async with sessionmaker() as session:
        user = User(id=uuid.uuid4(), email=f"u-{uuid.uuid4().hex[:8]}@example.com", full_name="Owner")
        session.add(user)
        await session.commit()
        user = await session.get(User, user.id)

        assert await tool_approval_usage.approvals_in_use(session) is True
        # An expired instance would lazy-load here, which an async session refuses.
        assert user.full_name == "Owner"
        assert await session.scalar(text("SELECT 1")) == 1


async def _column_default(session) -> str:
    return await session.scalar(
        text(
            "SELECT column_default FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'platform_settings' AND column_name = 'approve_tool_calls_default'"
        )
    )


async def test_the_opt_in_migration_turns_the_column_default_off_and_keeps_every_stored_value(install):
    """``o1ptin0173``: a row a release created TRUE keeps it, a new row reads FALSE."""
    engine, sessionmaker = install
    from app.api.v1.platform_settings_helpers import get_platform_settings

    async with sessionmaker() as session:
        settings_row = await get_platform_settings(session)
        settings_row.approve_tool_calls_default = True
        await session.commit()
    # The column as j1atc0173 left it on a fresh install.
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE platform_settings ALTER COLUMN approve_tool_calls_default SET DEFAULT TRUE"))

    for _ in range(2):
        async with engine.begin() as conn:
            await conn.run_sync(_run("o1ptin0173"))

    async with sessionmaker() as session:
        assert (await _column_default(session)).lower() == "false"
        assert await session.scalar(text("SELECT approve_tool_calls_default FROM platform_settings")) is True
        assert await resolve_approve_tool_calls(session, None) is True
