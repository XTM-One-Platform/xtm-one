"""The #1844 revision against a real PostgreSQL: no primary asset link is lost.

The runner calls ``create_all`` before the pending revisions
(``app/core/migrations.py``; ``migrations-safety.md`` rule 5), so on an
upgrade the renamed table ``organization_primary_asset_links`` already exists,
empty, when ``s1844primasset01`` runs. Pinned here: the links of the old table
survive that ordering, a re-run changes nothing, the batched rewrite of the
signal decisions reaches every row, and the downgrade names the table back.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app.models.organization_asset_record import OrganizationPrimaryAssetLink
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

_REVISION = next((Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("s1844primasset01_*.py"))
_OLD, _NEW = "organization_crown_jewel_links", "organization_primary_asset_links"


def _load_revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("s1844primasset01", _REVISION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


revision = _load_revision()


def _model_table(metadata: sa.MetaData) -> sa.Table:
    """The model's link table without its foreign keys (the scratch database has no parents)."""
    model: Any = OrganizationPrimaryAssetLink.__table__
    table = sa.Table(
        model.name,
        metadata,
        *(
            sa.Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable, server_default=c.server_default)
            for c in model.columns
        ),
    )
    for index in model.indexes:
        sa.Index(index.name, *(table.c[c.name] for c in index.columns))
    return table


_OLD_DDL = f"""
CREATE TABLE {_OLD} (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    crown_jewel_key varchar(200) NOT NULL,
    record_id uuid,
    group_id uuid,
    source varchar(80) NOT NULL DEFAULT 'manual',
    external_id varchar(200) NOT NULL DEFAULT '',
    roles jsonb NOT NULL DEFAULT '[]'::jsonb,
    inherited_needs jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    via varchar(200) NOT NULL DEFAULT '',
    removed boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_{_OLD}_jewel ON {_OLD} (organization_id, crown_jewel_key);
CREATE INDEX ix_{_OLD}_record ON {_OLD} (record_id);
CREATE INDEX ix_{_OLD}_group ON {_OLD} (group_id);
"""


async def _migrate(engine: Any, step: Callable[[], None]) -> None:
    def _run(sync_conn: Any) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            step()

    async with engine.begin() as conn:
        await conn.run_sync(_run)


async def _seed_old(engine: Any, rows: int) -> list[uuid.UUID]:
    ids = [uuid.uuid4() for _ in range(rows)]
    org = uuid.uuid4()
    async with engine.begin() as conn:
        for statement in filter(str.strip, _OLD_DDL.split(";")):
            await conn.execute(text(statement))
        for i, row_id in enumerate(ids):
            await conn.execute(
                text(
                    f"INSERT INTO {_OLD} (id, organization_id, crown_jewel_key, source, roles) "  # noqa: S608
                    "VALUES (:id, :org, :key, :source, CAST(:roles AS jsonb))"
                ),
                {"id": row_id, "org": org, "key": f"payments-{i}", "source": "manual", "roles": json.dumps(["stores"])},
            )
    return ids


async def _create_all(engine: Any) -> None:
    metadata = sa.MetaData()
    _model_table(metadata)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)


async def _tables(engine: Any) -> set[str]:
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda c: sa.inspect(c).get_table_names()))


async def _keys(engine: Any, table: str, column: str) -> dict[uuid.UUID, str]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text(f"SELECT id, {column} FROM {table}"))).all()  # noqa: S608
    return {row[0]: row[1] for row in rows}


@pytest.mark.asyncio
async def test_the_links_survive_create_all_before_the_upgrade() -> None:
    async with scratch_database("primasset_mig", tables=[]) as db:
        engine = db.engine
        ids = await _seed_old(engine, 3)
        await _create_all(engine)  # the runner's create_all: the new table exists, empty

        await _migrate(engine, revision.upgrade)
        await _migrate(engine, revision.upgrade)  # a re-run changes nothing

        assert _OLD not in await _tables(engine)
        links = await _keys(engine, _NEW, "primary_asset_key")
        assert set(links) == set(ids)
        assert sorted(links.values()) == ["payments-0", "payments-1", "payments-2"]
        async with engine.connect() as conn:
            indexes = set(await conn.run_sync(lambda c: {i["name"] for i in sa.inspect(c).get_indexes(_NEW)}))
        assert {f"ix_{_NEW}_{suffix}" for suffix in ("jewel", "record", "group")} <= indexes

        await _migrate(engine, revision.downgrade)
        assert set(await _keys(engine, _OLD, "crown_jewel_key")) == set(ids)


@pytest.mark.asyncio
async def test_rows_written_to_the_new_table_are_kept_and_the_old_ones_copied() -> None:
    async with scratch_database("primasset_mig", tables=[]) as db:
        engine = db.engine
        ids = await _seed_old(engine, 2)
        await _create_all(engine)
        fresh = uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text(f"INSERT INTO {_NEW} (id, organization_id, primary_asset_key) VALUES (:id, :org, 'mail')"),  # noqa: S608
                {"id": fresh, "org": uuid.uuid4()},
            )

        await _migrate(engine, revision.upgrade)

        assert _OLD not in await _tables(engine)
        links = await _keys(engine, _NEW, "primary_asset_key")
        assert set(links) == {*ids, fresh}
        assert links[fresh] == "mail"


@pytest.mark.asyncio
async def test_the_signal_decisions_are_rewritten_in_bounded_batches() -> None:
    async with scratch_database("primasset_mig", tables=[]) as db:
        engine = db.engine
        jewel = "crown" + "_jewel"
        decision = {"reason_code": jewel + "_below_policy", "checks": [{"rule": "min_" + jewel + "_criticality"}]}
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE ctem_signals (id uuid PRIMARY KEY, decision json)"))
            for _ in range(7):
                await conn.execute(
                    text("INSERT INTO ctem_signals (id, decision) VALUES (:id, CAST(:d AS json))"),
                    {"id": uuid.uuid4(), "d": json.dumps(decision)},
                )
            await conn.execute(
                text("INSERT INTO ctem_signals (id, decision) VALUES (:id, CAST(:d AS json))"),
                {"id": uuid.uuid4(), "d": json.dumps({"reason_code": "kev"})},
            )

        def _step() -> None:
            from alembic import op

            revision._rewrite_json(op.get_bind(), "ctem_signals", ("decision",), revision.NAMES, "json", batch_size=3)

        await _migrate(engine, _step)

        async with engine.connect() as conn:
            stored = [row[0] for row in (await conn.execute(text("SELECT decision::text FROM ctem_signals"))).all()]
        assert not any(jewel in value for value in stored)
        assert sum("primary_asset_below_policy" in value for value in stored) == 7
