"""The contract phase of the focus mode and development AI adoption removal (#11380).

``x11380focdrop1`` drops the columns ``x11380devadopt1`` left unmapped. Pinned here:

* nothing in the application still maps or names them, so dropping them cannot
  break a request on the release that ships the drop;
* the drop never runs while a replica that maps them can still be serving: only
  a release before the expand revision maps them, so a run that STARTED before
  it (or whose start is unknown) keeps them;
* on PostgreSQL the body drops them, is idempotent, and the downgrade rebuilds
  each column with its type, nullability and server default.
"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid
from types import SimpleNamespace

import pytest

from app.core.migration_ddl import STARTING_HEADS_ATTRIBUTE

BACKEND = pathlib.Path(__file__).resolve().parents[1]
REVISION = BACKEND / "alembic" / "versions" / "x11380focdrop1_drop_focus_mode_and_dev_adoption_columns.py"

# The revision before the expand phase: the head of the release that maps them.
_BEFORE_EXPAND = "x11033jirascope1"


def _load(path: pathlib.Path):
    # ``alembic/versions`` is not a package: load the revision by path.
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration():
    return _load(REVISION)


def _alembic_config():
    from app.core.migrations import _build_alembic_config

    return _build_alembic_config()


def _context(starting_heads):
    """A migration context carrying *starting_heads* the way ``env.py`` records them."""
    config = _alembic_config()
    config.attributes[STARTING_HEADS_ATTRIBUTE] = starting_heads
    return SimpleNamespace(config=config)


def _dropped(migration) -> dict[str, set[str]]:
    by_table: dict[str, set[str]] = {}
    for table, column, _ddl in migration._COLUMNS:
        by_table.setdefault(table, set()).add(column)
    return by_table


class TestRevision:
    def test_it_comes_right_after_the_expand_phase(self, migration):
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(_alembic_config())
        assert migration.down_revision == migration._EXPAND_REVISION
        assert script.get_revision(migration._EXPAND_REVISION).down_revision == _BEFORE_EXPAND
        ancestors = {rev.revision for rev in script.iterate_revisions(script.get_heads()[0], "base")}
        assert migration.revision in ancestors


class TestNothingReadsTheColumns:
    def test_no_model_maps_them(self, migration):
        import app.models  # noqa: F401 - registers every mapper
        from app.core.database import Base

        for table, columns in _dropped(migration).items():
            mapped = set(Base.metadata.tables[table].columns.keys())
            assert not columns & mapped, (table, sorted(columns & mapped))

    def test_no_application_code_names_them(self, migration):
        # Code only, SQL strings included: a comment is not a reader.
        from tests import app_sources
        from tests.test_migration_integrity import _code_only

        names = {column for _table, column, _ddl in migration._COLUMNS}
        offenders = []
        for path in app_sources.python_files(BACKEND / "app"):
            source = app_sources.source(path)
            if not any(name in source for name in names):
                continue
            code = _code_only(source)
            if any(name in code for name in names):
                offenders.append(str(path.relative_to(BACKEND)))
        assert offenders == []


class TestTheGuard:
    @pytest.mark.parametrize(
        "starting_heads",
        [
            pytest.param(("x11380devadopt1",), id="the-expand-release"),
            pytest.param((), id="an-empty-database"),
        ],
    )
    def test_it_drops_when_no_old_replica_maps_them(self, migration, starting_heads):
        assert migration._old_readers_may_serve(_context(starting_heads)) is False

    @pytest.mark.parametrize(
        "starting_heads",
        [
            pytest.param((_BEFORE_EXPAND,), id="the-release-before-the-expand"),
            pytest.param(("not_a_revision",), id="an-unresolvable-start"),
        ],
    )
    def test_it_keeps_them_while_an_old_replica_may_serve(self, migration, starting_heads):
        assert migration._old_readers_may_serve(_context(starting_heads)) is True

    def test_it_keeps_them_when_the_start_is_unknown(self, migration):
        assert migration._old_readers_may_serve(SimpleNamespace(config=None)) is True


# ---------------------------------------------------------------------------
# PostgreSQL: the real bodies
# ---------------------------------------------------------------------------


def _run_body(module, name: str, sync_conn, starting_heads) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.runtime.environment import EnvironmentContext
    from alembic.script import ScriptDirectory

    if starting_heads is None:
        context = MigrationContext.configure(sync_conn)
    else:
        config = _alembic_config()
        config.attributes[STARTING_HEADS_ATTRIBUTE] = starting_heads
        environment = EnvironmentContext(config, ScriptDirectory.from_config(config))
        context = MigrationContext.configure(sync_conn, environment_context=environment)
    with Operations.context(context):
        getattr(module, name)()


def _state(migration):
    def read(sync_conn) -> dict:
        import sqlalchemy as sa

        inspector = sa.inspect(sync_conn)
        state = {}
        for table, columns in _dropped(migration).items():
            for column in inspector.get_columns(table):
                if column["name"] in columns:
                    state[(table, column["name"])] = (
                        repr(column["type"]),
                        column["nullable"],
                        column["default"],
                    )
        return state

    return read


async def _run(engine, module, name: str, starting_heads) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: _run_body(module, name, sync_conn, starting_heads))


async def _read(engine, migration) -> dict:
    async with engine.connect() as conn:
        return await conn.run_sync(_state(migration))


@pytest.mark.integration
class TestOnPostgres:
    async def test_the_downgrade_the_drop_and_the_round_trip(self, migration):
        from sqlalchemy import text

        from app.models.user import User
        from tests.scratch_database import ALL_TABLES, scratch_database

        async with scratch_database("focdrop", tables=ALL_TABLES, extensions=("vector",)) as db:
            engine = db.sessions.kw["bind"]
            start = (migration.down_revision,)
            # The current models do not carry the columns: the downgrade rebuilds them.
            assert await _read(engine, migration) == {}
            user_id = uuid.uuid4()
            async with db.sessions() as session:
                session.add(
                    User(
                        id=user_id,
                        email=f"foc-{uuid.uuid4().hex[:8]}@test.local",
                        full_name="Focus",
                        hashed_password="x",
                    )
                )
                await session.commit()

            await _run(engine, migration, "downgrade", start)
            rebuilt = await _read(engine, migration)
            assert set(rebuilt) == {(t, c) for t, c, _ddl in migration._COLUMNS}
            assert rebuilt[("users", "standalone_mode")][1] is False
            assert "false" in str(rebuilt[("users", "standalone_mode")][2])
            assert "true" in str(rebuilt[("platform_settings", "ai_metrics_sync_enabled")][2])
            assert rebuilt[("groups", "focus_mode_policy")][1] is True
            # Idempotent: a second downgrade finds every column present.
            await _run(engine, migration, "downgrade", start)
            assert await _read(engine, migration) == rebuilt

            # A run that started before the expand keeps them.
            await _run(engine, migration, "upgrade", (_BEFORE_EXPAND,))
            assert await _read(engine, migration) == rebuilt
            await _run(engine, migration, "upgrade", None)
            assert await _read(engine, migration) == rebuilt

            await _run(engine, migration, "upgrade", start)
            assert await _read(engine, migration) == {}
            async with engine.connect() as conn:
                count = (
                    await conn.execute(text("SELECT count(*) FROM users WHERE id = :id"), {"id": user_id})
                ).scalar_one()
            assert count == 1
            # Idempotent: a re-run finds nothing to drop.
            await _run(engine, migration, "upgrade", start)
            assert await _read(engine, migration) == {}
