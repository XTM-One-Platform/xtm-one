"""The evaluation migration's data steps: the resume secrets encrypted, the positions made unique.

That no evaluation migration builds or drops an index ``CONCURRENTLY`` is
``test_migration_integrity.test_no_new_migration_builds_an_index_concurrently``.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa


def _migration():
    """Import the revision module by path — its filename is not a valid module name."""
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "e1780evp001_add_evaluation_pipeline.py"
    spec = importlib.util.spec_from_file_location("e1780evp001", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── The resume secrets ──
# A released install can hold a callback evaluation whose secret is in clear. The
# migration encrypts it, so its callbacks keep working and nothing stays readable.
# On PostgreSQL: the rows are found by a JSON lookup only a real database answers.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_secrets_stored_in_clear_are_encrypted_and_the_unusable_ones_dropped():
    import app.models  # noqa: F401 - register every mapper on Base.metadata
    from app.core.security import encrypt_value
    from app.models.evaluation import Evaluation
    from app.services.evaluation.resume_secret import resume_secret
    from tests.scratch_database import ALL_TABLES, scratch_database

    migration = _migration()
    stored_before = {
        "clear": {"await_callback": True, "resume_secret": "s3cret"},
        "a-number": {"resume_secret": 12345},
        "unusable": {"await_callback": True, "resume_secret": True},
        "a-vault-reference": {"resume_secret": "vault://kv/evaluations#resume"},
        "none": {"schedule": "0 * * * *"},
        "already-encrypted": {"resume_secret_enc": encrypt_value("kept")},
        # A config that is not an object holds no secret, whatever its items.
        "not-an-object": ["resume_secret"],
    }
    async with scratch_database("resumesecret", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as s:
            s.add_all(Evaluation(name=name, trigger_config=config) for name, config in stored_before.items())
            s.add_all(Evaluation(name=name, trigger_config={}) for name in ("a-raw-escape", "a-huge-number"))
            await s.commit()
            # A ``\u0000`` escape PostgreSQL keeps verbatim in a ``json`` column and a
            # ``::jsonb`` cast refuses: written in SQL, the ORM would clean it.
            await s.execute(
                sa.text(
                    "UPDATE evaluations SET trigger_config = "
                    r"""'{"note": "\u0000", "resume_secret": "raw"}'::json WHERE name = 'a-raw-escape'"""
                )
            )
            # A number past a float's range (``1e400``) or ``numeric``'s (``1e131072``) decodes
            # to ``inf``, which no JSON text holds: it is written back as null, the secret
            # beside it still encrypted.
            await s.execute(
                sa.text(
                    "UPDATE evaluations SET trigger_config = "
                    """'{"cap": 1e131072, "big": 1e400, "resume_secret": "big"}'::json WHERE name = 'a-huge-number'"""
                )
            )
            await s.commit()

        async def _configs() -> dict[str, dict]:
            async with db.sessions() as s:
                rows = (await s.execute(sa.select(Evaluation.name, Evaluation.trigger_config))).all()
            return {name: config for name, config in rows}

        async with db.engine.begin() as conn:
            await conn.run_sync(migration.encrypt_clear_secrets)
        after = await _configs()

        assert after["not-an-object"] == ["resume_secret"]
        after_objects = {name: config for name, config in after.items() if isinstance(config, dict)}
        assert not any("resume_secret" in config for config in after_objects.values())
        assert {name: resume_secret(config) for name, config in after_objects.items()} == {
            "clear": "s3cret",
            "a-number": "12345",
            "unusable": None,
            "a-vault-reference": None,
            "none": None,
            "already-encrypted": "kept",
            "a-raw-escape": "raw",
            "a-huge-number": "big",
        }
        assert after["a-raw-escape"]["note"] == "\x00"
        assert after["a-huge-number"]["cap"] is None
        assert after["a-huge-number"]["big"] is None
        assert after["clear"]["await_callback"] is True
        assert after["none"] == stored_before["none"]

        # A retry finds nothing left to convert.
        async with db.engine.begin() as conn:
            await conn.run_sync(migration.encrypt_clear_secrets)
        assert await _configs() == after

        # The downgrade puts each secret back in clear, the only key the code before reads.
        async with db.engine.begin() as conn:
            await conn.run_sync(migration.decrypt_secrets)
        downgraded = await _configs()
        assert downgraded["clear"] == {"await_callback": True, "resume_secret": "s3cret"}
        assert downgraded["already-encrypted"] == {"resume_secret": "kept"}
        assert downgraded["a-raw-escape"] == {"note": "\x00", "resume_secret": "raw"}
        assert downgraded["a-huge-number"] == {"cap": None, "big": None, "resume_secret": "big"}


# ── The renumbering and unique positions ──
# ``max(position) or -1`` gave every item appended after the first one position 0, and an
# evaluation package names an entry by its position: the results of tied entries landed
# on one entry on import.

_POSITION_INDEXES = ("uq_dataset_entries_position", "uq_evaluation_datasets_position")


async def _without_position_indexes(db) -> None:
    """The scratch tables as before the upgrade: ``create_all`` built the model's indexes."""
    async with db.engine.begin() as conn:
        for index in _POSITION_INDEXES:
            await conn.execute(sa.text(f"DROP INDEX {index}"))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tied_positions_are_renumbered_in_the_order_they_were_appended():
    from datetime import UTC, datetime, timedelta

    import app.models  # noqa: F401 - register every mapper on Base.metadata
    from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
    from app.models.evaluation import Evaluation
    from app.models.user import User
    from tests.scratch_database import scratch_database

    migration = _migration()
    tables = [User.__table__, Evaluation.__table__, Dataset.__table__, DatasetEntry.__table__, EvaluationDataset.__table__]
    start = datetime(2026, 1, 1, tzinfo=UTC)
    async with scratch_database("positions", tables=tables) as db:
        await _without_position_indexes(db)
        async with db.sessions() as s:
            tied, untied, evaluation = Dataset(name="tied"), Dataset(name="untied"), Evaluation(name="Eval")
            s.add_all([tied, untied, evaluation])
            await s.flush()
            # Added one at a time: the first at 0, every later one at 0 too.
            s.add_all(
                DatasetEntry(dataset_id=tied.id, position=0, expected_answer=f"t{i}", created_at=start + timedelta(minutes=i))
                for i in range(3)
            )
            # A gap left by a deletion is not a tie.
            s.add_all(
                DatasetEntry(dataset_id=untied.id, position=position, expected_answer=f"u{position}", created_at=start)
                for position in (0, 5)
            )
            s.add_all(
                EvaluationDataset(evaluation_id=evaluation.id, dataset_id=dataset.id, position=0, created_at=start + timedelta(minutes=i))
                for i, dataset in enumerate((tied, untied))
            )
            await s.commit()
            tied_id, evaluation_id = tied.id, evaluation.id

        async with db.engine.connect() as conn:
            renumbered = await conn.run_sync(migration.renumber_ties)
            await conn.commit()
            again = await conn.run_sync(migration.renumber_ties)
            await conn.commit()
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.run_sync(migration.add_unique_positions)
            indexes = {
                row[0]
                for row in await conn.execute(
                    sa.text("SELECT indexname FROM pg_indexes WHERE indexname IN :names").bindparams(
                        sa.bindparam("names", expanding=True)
                    ),
                    {"names": list(_POSITION_INDEXES)},
                )
            }

        # Once the index exists, an append at a taken position fails instead of tying.
        async with db.sessions() as s:
            s.add(DatasetEntry(dataset_id=tied_id, position=1, expected_answer="late"))
            with pytest.raises(sa.exc.IntegrityError):
                await s.commit()

        async with db.sessions() as s:
            entries = (
                await s.execute(sa.select(DatasetEntry.expected_answer, DatasetEntry.position).order_by(DatasetEntry.expected_answer))
            ).all()
            links = (
                await s.execute(
                    sa.select(EvaluationDataset.dataset_id, EvaluationDataset.position)
                    .where(EvaluationDataset.evaluation_id == evaluation_id)
                    .order_by(EvaluationDataset.position)
                )
            ).all()

    assert renumbered == 2
    assert again == 0
    assert indexes == set(_POSITION_INDEXES)
    assert [tuple(row) for row in entries] == [("t0", 0), ("t1", 1), ("t2", 2), ("u0", 0), ("u5", 5)]
    assert [row.position for row in links] == [0, 1]
    assert links[0].dataset_id == tied_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_renumbering_skips_a_table_that_does_not_exist():
    """A database without the tables (never created there) is left alone, not an error."""
    from tests.scratch_database import scratch_database

    migration = _migration()
    async with scratch_database("positionsabsent", tables=[]) as db, db.engine.connect() as conn:
        assert await conn.run_sync(migration.renumber_ties) == 0
        await conn.commit()
        autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.run_sync(migration.add_unique_positions)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_tie_appended_during_the_build_is_renumbered_and_the_build_retried(monkeypatch):
    """An older replica can append a tie between the renumbering and the build, which
    fails the build: the tie is renumbered and the index built on the next attempt."""
    import app.models  # noqa: F401 - register every mapper on Base.metadata
    from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
    from app.models.evaluation import Evaluation
    from app.models.user import User
    from tests.scratch_database import scratch_database

    migration = _migration()
    tables = [User.__table__, Evaluation.__table__, Dataset.__table__, DatasetEntry.__table__, EvaluationDataset.__table__]
    async with scratch_database("positionsrace", tables=tables) as db:
        await _without_position_indexes(db)
        async with db.sessions() as s:
            dataset = Dataset(name="raced")
            s.add(dataset)
            await s.flush()
            s.add(DatasetEntry(dataset_id=dataset.id, position=0, expected_answer="first"))
            await s.commit()
            dataset_id = dataset.id

        builds: list[str] = []
        build = migration.create_index_in_place

        def _tie_then_build(conn, index, *args, **kwargs):
            builds.append(index)
            if len(builds) == 1:
                conn.execute(
                    sa.insert(DatasetEntry.__table__).values(
                        id=uuid.uuid4(), dataset_id=dataset_id, position=0, expected_answer="raced"
                    )
                )
            return build(conn, index, *args, **kwargs)

        monkeypatch.setattr(migration, "create_index_in_place", _tie_then_build)
        async with db.engine.connect() as conn:
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.run_sync(migration.add_unique_positions)

        async with db.sessions() as s:
            positions = (
                await s.execute(
                    sa.select(DatasetEntry.expected_answer, DatasetEntry.position).order_by(DatasetEntry.position)
                )
            ).all()

    assert builds == ["uq_dataset_entries_position", "uq_dataset_entries_position", "uq_evaluation_datasets_position"]
    assert [tuple(row) for row in positions] == [("first", 0), ("raced", 1)]


# ── An install on main's schema, upgraded ──
# The revision's downgrade gives main's schema back, so a scratch database built from
# the models and taken down to main's revision stands in for an install that never ran
# 1780. Its rows are written in SQL, as main's code wrote them.

_MAIN_REVISION = "v6539rret01"
_BACKEND = Path(__file__).resolve().parents[1]


def _alembic(url: str, *args: str) -> None:
    import os
    import subprocess
    import sys

    done = subprocess.run(  # noqa: S603 - the interpreter and alembic's own CLI
        [sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", *args],
        capture_output=True,
        text=True,
        cwd=_BACKEND,
        env={**os.environ, "DATABASE_URL": url},
    )
    assert done.returncode == 0, f"alembic {' '.join(args)}:\n{done.stdout}\n{done.stderr}"


async def _insert(conn, table: str, **values) -> uuid.UUID:
    """A row of *table* as the schema stands: *values*, and a plain value for each other required column."""
    from datetime import UTC, datetime

    required = (
        await conn.execute(
            sa.text(
                "SELECT column_name, data_type, character_maximum_length FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t "
                "AND is_nullable = 'NO' AND column_default IS NULL"
            ),
            {"t": table},
        )
    ).all()
    plain = {
        "uuid": uuid.uuid4,
        "text": lambda: f"x-{uuid.uuid4().hex[:8]}",
        "character varying": lambda: f"x-{uuid.uuid4().hex[:8]}",
        "integer": lambda: 0,
        "double precision": lambda: 0.0,
        "boolean": lambda: False,
        "json": lambda: "{}",
        "jsonb": lambda: "{}",
        "timestamp with time zone": lambda: datetime.now(UTC),
    }
    row = {"id": uuid.uuid4()}
    for name, data_type, length in required:
        if name not in row:
            value = plain[data_type]()
            row[name] = value[:length] if isinstance(value, str) and length else value
    row.update(values)
    columns = ", ".join(row)
    await conn.execute(sa.text(f"INSERT INTO {table} ({columns}) VALUES ({', '.join(':' + c for c in row)})"), row)  # noqa: S608 - test-chosen names
    return row["id"]


async def _speed_tolerance(conn, evaluation: uuid.UUID) -> float | None:
    from app.core.migration_ddl import column_exists

    if not await conn.run_sync(column_exists, "evaluations", "speed_tolerance"):
        return None
    return (
        await conn.execute(sa.text("SELECT speed_tolerance FROM evaluations WHERE id = :id"), {"id": evaluation})
    ).scalar_one()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_install_on_mains_schema_upgrades_keeping_its_data():
    """Every value main stored survives the upgrade and the downgrade, a result outlives
    its deleted entry, and no constraint is left unvalidated."""
    import json

    from tests.scratch_database import ALL_TABLES, scratch_database

    async with scratch_database("mainupgrade", tables=ALL_TABLES, extensions=("vector",), bind_app_sessions=False) as db:
        _alembic(db.url, "stamp", "head")
        _alembic(db.url, "downgrade", _MAIN_REVISION)
        async with db.engine.begin() as conn:
            agent = await _insert(conn, "agents", name="Agent")
            evaluation = await _insert(
                conn,
                "evaluations",
                name="Eval",
                speed_tolerance=7.5,
                timeout_seconds=300,
                trigger_config=json.dumps({"resume_secret": "s3cret"}),
            )
            dataset = await _insert(conn, "datasets", name="Dataset")
            entry = await _insert(conn, "dataset_entries", dataset_id=dataset, position=0, expected_answer="a")
            run = await _insert(
                conn, "evaluation_runs", evaluation_id=evaluation, agent_id=agent, status="completed", trigger_reason="manual"
            )
            result = await _insert(conn, "evaluation_results", run_id=run, entry_id=entry, agent_output="ok", score=80.0)

        _alembic(db.url, "upgrade", "head")
        async with db.engine.begin() as conn:
            tolerance = (
                await conn.execute(
                    sa.text("SELECT performance_tolerance FROM evaluations WHERE id = :id"), {"id": evaluation}
                )
            ).scalar_one()
            config = json.loads(
                (
                    await conn.execute(
                        sa.text("SELECT trigger_config::text FROM evaluations WHERE id = :id"), {"id": evaluation}
                    )
                ).scalar_one()
            )
            unvalidated = (
                await conn.execute(
                    sa.text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE connamespace = current_schema()::regnamespace AND NOT convalidated"
                    )
                )
            ).scalars().all()
            await conn.execute(sa.text("DELETE FROM dataset_entries WHERE id = :id"), {"id": entry})
            kept = (
                await conn.execute(sa.text("SELECT entry_id FROM evaluation_results WHERE id = :id"), {"id": result})
            ).one_or_none()

        _alembic(db.url, "downgrade", _MAIN_REVISION)
        async with db.engine.begin() as conn:
            restored = await _speed_tolerance(conn, evaluation)

    assert tolerance == 7.5
    assert "resume_secret" not in config and config.get("resume_secret_enc")
    assert unvalidated == []
    assert kept is not None and kept.entry_id is None
    assert restored == 7.5


_DROP_REVISION = "e686spdtol01"
_BEFORE_DROP = "e684cbkeys01"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_speed_tolerance_is_dropped_and_its_downgrade_restores_the_current_tolerances():
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with scratch_database("speedtolerance", tables=ALL_TABLES, extensions=("vector",), bind_app_sessions=False) as db:
        _alembic(db.url, "stamp", _BEFORE_DROP)
        _alembic(db.url, "upgrade", _DROP_REVISION)
        async with db.engine.begin() as conn:
            evaluation = await _insert(conn, "evaluations", name="Eval", timeout_seconds=300, performance_tolerance=3.0)
            fresh = await _speed_tolerance(conn, evaluation)

        _alembic(db.url, "downgrade", _BEFORE_DROP)
        async with db.engine.begin() as conn:
            restored = await _speed_tolerance(conn, evaluation)
            created = await _insert(conn, "evaluations", name="Created before", timeout_seconds=300)
            defaulted = await _speed_tolerance(conn, created)

        _alembic(db.url, "upgrade", _DROP_REVISION)
        async with db.engine.begin() as conn:
            dropped = await _speed_tolerance(conn, evaluation)

    assert fresh is None
    assert (restored, defaulted) == (3.0, 5.0)
    assert dropped is None
