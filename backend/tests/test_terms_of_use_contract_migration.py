"""The contract phase of the Terms of Use move.

``c2797toudrop1`` drops the per-user schema that the expand phase left unmapped: the
``users.tou_accepted_version`` / ``users.tou_accepted_at`` columns and the
``terms_acceptances`` table. Two properties matter and are pinned here:

* nothing in the application still maps or names that schema, so dropping it
  cannot break a request on the release that ships the drop;
* the drop never runs while a replica that reads it can still be serving. Only
  releases 1.260804.0 to 1.260809.0 read it, and their replicas are the ones
  being replaced exactly when an upgrade STARTS between the revision that
  created the schema and the expand revision - including when a later run
  finishes such an upgrade after an ``autocommit_block()`` committed part of it.
  ``alembic/env.py`` records where each run started; the migration keeps the
  inert schema in both cases.

The offline tests read the revision graph and the ORM; ``TestOnPostgres`` runs
the real bodies against PostgreSQL (CI's Bootstrap job).
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import pathlib
import re
import uuid
from types import SimpleNamespace

import pytest

from app.core.migration_ddl import (
    STARTING_HEADS_ATTRIBUTE,
    applied_before_this_run,
    run_starting_heads,
)

BACKEND = pathlib.Path(__file__).resolve().parents[1]
VERSIONS = BACKEND / "alembic" / "versions"
REVISION = VERSIONS / "c2797toudrop1_drop_legacy_per_user_terms_of_use.py"
LEGACY_REVISION_FILE = VERSIONS / "a4c8tou10001_add_terms_of_use_acceptance.py"

_LEGACY_COLUMNS = {"tou_accepted_version", "tou_accepted_at"}
# The last revision before the expand phase (the head of 1.260804.0 -
# 1.260809.0), and the last one before the revision that created the per-user
# schema.
_BEFORE_EXPAND = "b2r7rep2720"
_BEFORE_LEGACY = "a4c8rdonly1863"
# The first revision an upgrade from the releases that read the schema commits
# mid-run (``a7f3run0001`` opens its ``autocommit_block()``).
_FIRST_RESUME_POINT = "f2adpst28350"

# The Alembic head of every release since 1.260731.0 (git tags). 1.260804.0 to
# 1.260809.0 read the per-user schema; 1.260813.0 / 1.260814.0 sit on a resume
# point, so a direct upgrade from them cannot be told apart from the rest of an
# upgrade from 1.260809.0.
_RELEASE_HEADS = (
    ("1.260731.0", "f6a2oash0001", "drop"),
    ("1.260804.0", "b2r7rep2720", "keep"),
    ("1.260805.0", "b2r7rep2720", "keep"),
    ("1.260809.0", "b2r7rep2720", "keep"),
    ("1.260809.1", "c5g9tou20001", "drop"),
    ("1.260811.0", "c4e1mcpdcr0001", "drop"),
    ("1.260813.0", "f2adpst28350", "keep"),
    ("1.260814.0", "f2adpst28350", "keep"),
    ("1.260817.0", "a7f3run0003", "drop"),
    ("1.260818.0", "a7f3run0003", "drop"),
    ("1.260820.0", "a7f3run0003", "drop"),
    ("1.260917.0", "o1crq2opencrq1", "drop"),
)

_COMMIT_SQL = re.compile(r"\s*COMMIT\b", re.IGNORECASE)


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


def _app_modules(path: pathlib.Path) -> list[pathlib.Path]:
    """The ``app`` modules *path* imports (one level: the migration helpers)."""
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "app":
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names if alias.name.split(".")[0] == "app")
    modules: list[pathlib.Path] = []
    for name in names:
        base = BACKEND.joinpath(*name.split("."))
        for candidate in (base.with_suffix(".py"), base / "__init__.py"):
            if candidate.is_file() and candidate not in modules:
                modules.append(candidate)
    return modules


def _commits(path: pathlib.Path) -> bool:
    """Does *path* call ``autocommit_block()`` or ``.commit()``, or execute ``COMMIT``?"""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"autocommit_block", "commit"}:
            return True
        if any(
            isinstance(arg, ast.Constant) and isinstance(arg.value, str) and _COMMIT_SQL.match(arg.value)
            for arg in node.args
        ):
            return True
    return False


def _resume_points(script, lower: str, upper: str) -> set[str]:
    """Every revision an interrupted upgrade can resume from strictly between *lower* and *upper*.

    A migration that commits mid-run commits the stamp of the revision before
    it, and that is where the next run starts.
    """
    points: set[str] = set()
    for rev in script.iterate_revisions(upper, lower):
        if rev.revision in (upper, lower):
            continue
        path = pathlib.Path(rev.path)
        if _commits(path) or any(_commits(module) for module in _app_modules(path)):
            down = rev.down_revision
            points.update((down,) if isinstance(down, str) else tuple(down or ()))
    return points


class TestRevision:
    def test_it_sits_on_the_single_line_after_the_expand_phase(self, migration):
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(_alembic_config())
        heads = script.get_heads()
        assert len(heads) == 1, f"expected a single head, got {heads}"
        lineage = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
        assert migration.revision in lineage
        # The contract must come after the expand, or it would drop what the
        # expand's own release still reads.
        ancestors = {
            rev.revision for rev in script.iterate_revisions(migration.revision, "base")
        }
        assert migration._EXPAND_REVISION in ancestors
        assert migration._LEGACY_REVISION in ancestors

    def test_the_revisions_the_guard_reasons_about_are_neighbours(self, migration):
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(_alembic_config())
        assert script.get_revision(migration._EXPAND_REVISION).down_revision == _BEFORE_EXPAND
        assert script.get_revision(migration._LEGACY_REVISION).down_revision == _BEFORE_LEGACY

    def test_the_resume_points_are_every_revision_an_interrupted_upgrade_restarts_from(self, migration):
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(_alembic_config())
        derived = _resume_points(script, migration._EXPAND_REVISION, migration.revision)
        assert derived == migration._RESUME_POINTS, (
            "c2797toudrop1._RESUME_POINTS must name the revision before every "
            "migration between the expand revision and it that can commit mid-run "
            "(autocommit_block): a run resumed after that commit starts there. "
            f"Missing: {sorted(derived - migration._RESUME_POINTS)}; "
            f"stale: {sorted(migration._RESUME_POINTS - derived)}."
        )
        assert _FIRST_RESUME_POINT in derived


class TestNothingReadsTheLegacySchema:
    def test_no_model_maps_it(self):
        import app.models  # noqa: F401 - registers every mapper
        from app.core.database import Base

        assert "terms_acceptances" not in Base.metadata.tables
        assert not _LEGACY_COLUMNS & set(Base.metadata.tables["users"].columns.keys())

    def test_no_application_code_names_the_table(self):
        # Code only, SQL strings included: a comment recording that the table
        # was dropped is not a reader.
        from tests import app_sources
        from tests.test_migration_integrity import _code_only

        names = ("terms_acceptances", "TermsAcceptance")
        offenders = []
        for path in app_sources.python_files(BACKEND / "app"):
            source = app_sources.source(path)
            # ``_code_only`` blanks characters in place, so a name its code
            # holds is in the source as written: the others need no tokenizing.
            if not any(name in source for name in names):
                continue
            code = _code_only(source)
            if any(name in code for name in names):
                offenders.append(str(path.relative_to(BACKEND)))
        assert offenders == []


class TestWhereTheRunStarted:
    def test_no_record_is_unknown(self):
        assert run_starting_heads(SimpleNamespace(config=None)) is None
        assert applied_before_this_run(SimpleNamespace(config=None), "c5g9tou20001") is None
        config = _alembic_config()
        assert STARTING_HEADS_ATTRIBUTE not in config.attributes
        assert run_starting_heads(SimpleNamespace(config=config)) is None
        assert applied_before_this_run(SimpleNamespace(config=config), "c5g9tou20001") is None

    def test_the_record_reads_back_as_recorded(self):
        assert run_starting_heads(_context(["b2r7rep2720"])) == ("b2r7rep2720",)
        assert run_starting_heads(_context(())) == ()

    def test_an_empty_database_has_applied_nothing(self):
        assert applied_before_this_run(_context(()), "c5g9tou20001") is False

    def test_ancestry_decides(self, migration):
        assert applied_before_this_run(_context((migration.down_revision,)), "c5g9tou20001") is True
        assert applied_before_this_run(_context(("c5g9tou20001",)), "c5g9tou20001") is True
        assert applied_before_this_run(_context((_BEFORE_EXPAND,)), "c5g9tou20001") is False

    def test_an_unresolvable_start_is_unknown(self):
        assert applied_before_this_run(_context(("not_a_revision",)), "c5g9tou20001") is None


class TestTheGuard:
    def test_an_upgrade_from_the_previous_revision_drops_it(self, migration):
        context = _context((migration.down_revision,))
        assert migration._legacy_schema_may_still_be_read(context) is False

    @pytest.mark.parametrize(
        "starting_heads",
        [
            pytest.param(("c5g9tou20001",), id="the-expand-phase-itself"),
            pytest.param((_BEFORE_LEGACY,), id="before-the-schema-existed"),
            pytest.param((), id="an-empty-database"),
        ],
    )
    def test_it_drops_when_no_replica_can_read_the_schema(self, migration, starting_heads):
        assert migration._legacy_schema_may_still_be_read(_context(starting_heads)) is False

    @pytest.mark.parametrize(
        "starting_heads",
        [
            pytest.param(("a4c8tou10001",), id="the-schema-just-created"),
            pytest.param((_BEFORE_EXPAND,), id="the-last-revision-before-the-expand"),
            pytest.param(("not_a_revision",), id="an-unresolvable-start"),
            pytest.param((_FIRST_RESUME_POINT,), id="an-upgrade-resumed-after-its-first-commit"),
        ],
    )
    def test_it_keeps_the_schema_while_a_reader_may_serve(self, migration, starting_heads):
        assert migration._legacy_schema_may_still_be_read(_context(starting_heads)) is True

    def test_it_keeps_the_schema_when_the_start_is_unknown(self, migration):
        assert migration._legacy_schema_may_still_be_read(SimpleNamespace(config=None)) is True

    def test_every_resume_point_keeps_it(self, migration):
        # Each one is past the expand, so "expand already applied" alone would drop.
        for point in migration._RESUME_POINTS:
            assert applied_before_this_run(_context((point,)), migration._EXPAND_REVISION) is True
            assert migration._legacy_schema_may_still_be_read(_context((point,))) is True, point

    @pytest.mark.parametrize(
        ("release_head", "outcome"),
        [pytest.param(head, outcome, id=release) for release, head, outcome in _RELEASE_HEADS],
    )
    def test_a_direct_upgrade_from_each_release(self, migration, release_head, outcome):
        kept = migration._legacy_schema_may_still_be_read(_context((release_head,)))
        assert kept is (outcome == "keep")


class TestEnvRecordsTheStart:
    def test_the_heads_are_read_before_the_first_migration_runs(self):
        tree = ast.parse((BACKEND / "alembic" / "env.py").read_text(encoding="utf-8"))
        fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "do_run_migrations"
        )
        source = ast.unparse(fn)
        record = "config.attributes[STARTING_HEADS_ATTRIBUTE]"
        assert record in source
        assert "get_current_heads()" in source
        assert source.index(record) < source.index("context.run_migrations()")


# ---------------------------------------------------------------------------
# PostgreSQL: the real bodies
# ---------------------------------------------------------------------------


def _run_body(module, name: str, sync_conn, starting_heads) -> None:
    """Run ``module.<name>()`` on *sync_conn* as a run started at *starting_heads*.

    ``starting_heads=None`` runs it with no record at all, the way a body
    driven outside ``env.py`` runs.
    """
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


def _install_legacy_schema(sync_conn) -> None:
    """The per-user schema exactly as ``a4c8tou10001`` created it on a live install."""
    _run_body(_load(LEGACY_REVISION_FILE), "upgrade", sync_conn, None)


def _legacy_state(sync_conn) -> dict:
    import sqlalchemy as sa

    inspector = sa.inspect(sync_conn)
    user_columns = {c["name"]: c for c in inspector.get_columns("users")}
    present = _LEGACY_COLUMNS & set(user_columns)
    state: dict = {
        "table": inspector.has_table("terms_acceptances"),
        "columns": {
            name: (repr(user_columns[name]["type"]), user_columns[name]["nullable"])
            for name in present
        },
    }
    if state["table"]:
        state["table_columns"] = {
            c["name"]: (repr(c["type"]), c["nullable"])
            for c in inspector.get_columns("terms_acceptances")
        }
        state["unique"] = {
            (uc["name"], tuple(uc["column_names"]))
            for uc in inspector.get_unique_constraints("terms_acceptances")
        }
        state["indexes"] = {
            (ix["name"], tuple(ix["column_names"]))
            for ix in inspector.get_indexes("terms_acceptances")
        }
        state["foreign_keys"] = {
            (fk["referred_table"], tuple(fk["constrained_columns"]), fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("terms_acceptances")
        }
        state["rows"] = sync_conn.execute(
            sa.text("SELECT count(*) FROM terms_acceptances")
        ).scalar_one()
    return state


async def _seed_legacy_acceptance(sm, engine) -> uuid.UUID:
    from sqlalchemy import text

    from app.models.user import User

    user_id = uuid.uuid4()
    async with sm() as session:
        session.add(
            User(
                id=user_id,
                email=f"tou-{uuid.uuid4().hex[:8]}@test.local",
                full_name="Accepted per user",
                hashed_password="x",
            )
        )
        await session.commit()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE users SET tou_accepted_version = '2026-08', "
                "tou_accepted_at = now() WHERE id = :id"
            ),
            {"id": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO terms_acceptances (id, user_id, tou_version, accepted_at) "
                "VALUES (:id, :user_id, '2026-08', now())"
            ),
            {"id": uuid.uuid4(), "user_id": user_id},
        )
    return user_id


async def _seed_legacy_acceptance_at(engine) -> uuid.UUID:
    """The same acceptance on a database at a release's head, where ``User`` does not fit."""
    from sqlalchemy import text

    user_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, full_name, is_active, role, created_at, "
                "updated_at, tou_accepted_version, tou_accepted_at) VALUES (:id, :email, "
                "'Accepted per user', true, 'user', now(), now(), '2026-08', now())"
            ),
            {"id": user_id, "email": f"tou-{uuid.uuid4().hex[:8]}@test.local"},
        )
        await conn.execute(
            text(
                "INSERT INTO terms_acceptances (id, user_id, tou_version, accepted_at) "
                "VALUES (:id, :user_id, '2026-08', now())"
            ),
            {"id": uuid.uuid4(), "user_id": user_id},
        )
    return user_id


async def _scalar(engine, sql: str, **params):
    from sqlalchemy import text

    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def _state(engine) -> dict:
    async with engine.connect() as conn:
        return await conn.run_sync(_legacy_state)


async def _run(engine, module, name: str, starting_heads) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: _run_body(module, name, sync_conn, starting_heads))


@pytest.mark.integration
class TestOnPostgres:
    async def test_the_drop_the_downgrade_and_the_round_trip(self, migration):
        from tests.test_a2a_hooks_postgres import _temp_database

        async with _temp_database() as sm:
            engine = sm.kw["bind"]
            async with engine.begin() as conn:
                await conn.run_sync(_install_legacy_schema)
            as_first_created = await _state(engine)
            assert as_first_created["table"] is True
            assert set(as_first_created["columns"]) == _LEGACY_COLUMNS
            user_id = await _seed_legacy_acceptance(sm, engine)
            start = (migration.down_revision,)

            await _run(engine, migration, "upgrade", start)
            assert await _state(engine) == {"table": False, "columns": {}}
            # Only the legacy schema went: the user it described is still here.
            assert await _scalar(engine, "SELECT count(*) FROM users WHERE id = :id", id=user_id) == 1

            # Idempotent: a re-run finds nothing to drop.
            await _run(engine, migration, "upgrade", start)
            assert await _state(engine) == {"table": False, "columns": {}}

            # The downgrade rebuilds the shape the revision that created it
            # left, empty: the dropped rows are not recoverable.
            await _run(engine, migration, "downgrade", start)
            rebuilt = await _state(engine)
            assert rebuilt == as_first_created
            assert rebuilt["foreign_keys"] == {("users", ("user_id",), "CASCADE")}
            assert rebuilt["unique"] == {
                ("uq_terms_acceptances_user_version", ("user_id", "tou_version"))
            }
            assert ("ix_terms_acceptances_user_id", ("user_id",)) in rebuilt["indexes"]
            assert all(nullable for _type, nullable in rebuilt["columns"].values())
            assert rebuilt["rows"] == 0

            # Idempotent too, and the round trip converges.
            await _run(engine, migration, "downgrade", start)
            assert await _state(engine) == rebuilt

            # A history table present without its index (a restore) gets it back.
            async with engine.begin() as conn:
                await conn.exec_driver_sql("DROP INDEX ix_terms_acceptances_user_id")
            await _run(engine, migration, "downgrade", start)
            assert await _state(engine) == rebuilt

            await _run(engine, migration, "upgrade", start)
            assert await _state(engine) == {"table": False, "columns": {}}

    @pytest.mark.parametrize(
        "starting_heads",
        [
            pytest.param((_BEFORE_EXPAND,), id="a-release-that-reads-it"),
            pytest.param(None, id="no-record-of-the-start"),
        ],
    )
    async def test_an_upgrade_a_reader_may_still_serve_keeps_it_intact(
        self, migration, starting_heads
    ):
        from tests.test_a2a_hooks_postgres import _temp_database

        async with _temp_database() as sm:
            engine = sm.kw["bind"]
            async with engine.begin() as conn:
                await conn.run_sync(_install_legacy_schema)
            user_id = await _seed_legacy_acceptance(sm, engine)
            before = await _state(engine)

            await _run(engine, migration, "upgrade", starting_heads)

            assert await _state(engine) == before
            assert before["rows"] == 1
            accepted = await _scalar(
                engine, "SELECT tou_accepted_version FROM users WHERE id = :id", id=user_id
            )
            assert accepted == "2026-08"

    async def test_env_py_records_the_start_and_a_real_upgrade_drops_it(
        self, migration, monkeypatch
    ):
        """The path the startup runner takes: ``command.upgrade`` through ``env.py``."""
        from alembic import command
        from app.core import config as config_module
        from tests.test_a2a_hooks_postgres import _temp_database

        async with _temp_database() as sm:
            engine = sm.kw["bind"]
            async with engine.begin() as conn:
                await conn.run_sync(_install_legacy_schema)
            # ``env.py`` always takes its URL from the settings.
            monkeypatch.setattr(
                config_module.settings,
                "database_url",
                engine.url.render_as_string(hide_password=False),
            )
            await asyncio.to_thread(command.stamp, _alembic_config(), migration.down_revision)

            config = _alembic_config()
            await asyncio.to_thread(command.upgrade, config, migration.revision)

            assert config.attributes[STARTING_HEADS_ATTRIBUTE] == (migration.down_revision,)
            assert await _state(engine) == {"table": False, "columns": {}}

    @pytest.mark.parametrize(
        ("release_head", "interrupt_at", "dropped"),
        [
            pytest.param(_BEFORE_EXPAND, None, False, id="from-1.260809.0"),
            pytest.param(
                _BEFORE_EXPAND,
                "a7f3run0001",
                False,
                id="from-1.260809.0-retried-after-its-first-mid-run-commit",
            ),
            pytest.param("a7f3run0003", None, True, id="from-1.260820.0"),
        ],
    )
    async def test_the_startup_runner_upgrading_from_a_release(
        self, monkeypatch, release_head, interrupt_at, dropped
    ):
        """The production path: a database really at a release's head, upgraded
        by ``migrations._apply_pending_migrations`` through ``env.py``.

        ``interrupt_at`` fails the run once, as a lock timeout, while it stamps
        that revision - after ``a7f3run0001`` committed ``f2adpst28350`` - and
        the runner retries, as it does in production: the retry starts on a
        resume point while the replicas of the release it began on still serve.
        """
        from alembic.runtime import migration as alembic_migration
        from alembic.script import ScriptDirectory

        from alembic import command
        from app.core import config as config_module
        from app.core import migrations
        from tests.scratch_database import scratch_database

        async with scratch_database(
            "toucontract", tables=(), extensions=("vector",), refuse_other_databases=True
        ) as db:
            engine = db.engine
            monkeypatch.setattr(config_module.settings, "database_url", db.url)
            await asyncio.to_thread(command.upgrade, _alembic_config(), release_head)
            user_id = await _seed_legacy_acceptance_at(engine)

            build_config = migrations._build_alembic_config
            configs = []

            def recording_config():
                config = build_config()
                configs.append(config)
                return config

            stamp = alembic_migration.HeadMaintainer.update_to_step
            failures = []

            def interrupted_stamp(self, step):
                if (
                    isinstance(step, alembic_migration.RevisionStep)
                    and step.is_upgrade
                    and step.revision.revision == interrupt_at
                    and not failures
                ):
                    failures.append(interrupt_at)
                    raise RuntimeError("canceling statement due to lock timeout (simulated)")
                return stamp(self, step)

            monkeypatch.setattr(migrations, "_build_alembic_config", recording_config)
            monkeypatch.setattr(migrations, "_MIGRATION_RETRY_BASE_DELAY", 0.0)
            monkeypatch.setattr(alembic_migration.HeadMaintainer, "update_to_step", interrupted_stamp)

            await migrations._apply_pending_migrations()

            starts = [config.attributes[STARTING_HEADS_ATTRIBUTE] for config in configs]
            if interrupt_at:
                assert failures == [interrupt_at]
                assert starts == [(release_head,), (_FIRST_RESUME_POINT,)]
            else:
                assert starts == [(release_head,)]
            head = ScriptDirectory.from_config(build_config()).get_current_head()
            assert await _scalar(engine, "SELECT version_num FROM alembic_version") == head
            state = await _state(engine)
            if dropped:
                assert state == {"table": False, "columns": {}}
            else:
                assert state["table"] is True
                assert set(state["columns"]) == _LEGACY_COLUMNS
                assert state["rows"] == 1
                accepted = await _scalar(
                    engine, "SELECT tou_accepted_version FROM users WHERE id = :id", id=user_id
                )
                assert accepted == "2026-08"
