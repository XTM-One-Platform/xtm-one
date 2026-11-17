"""``m9157orgs01`` on a real PostgreSQL: an install with saved profiles keeps reading them (#9157).

The table as main leaves it (one ``unique(version)`` sequence), two saved
versions, then the upgrade: the primary organization exists, named after
the latest profile, owns both versions, and a tracked organization gets its
own version 1. The upgrade re-runs as a no-op and the downgrade restores
the single sequence.
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
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

_PATH = next((Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("m9157orgs01_*.py"))


def _swap(url: str, database: str) -> str:
    return urllib.parse.urlparse(url)._replace(path=f"/{database}").geturl()


@contextlib.asynccontextmanager
async def _database():
    base = os.environ.get("DATABASE_URL", "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot")
    name = f"copilot_orgs_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(_swap(base, "postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(_swap(base, name))
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        await admin.dispose()


def _step(step: str):
    def _body(sync_conn) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("_mig_m9157orgs01", _PATH)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        # As the startup runner does: Alembic owns the transaction, so the
        # migration's autocommit_block can commit it and build the index.
        context = MigrationContext.configure(sync_conn)
        with Operations.context(context), context.begin_transaction():
            getattr(module, step)()

    return _body


async def _migrate(engine, step: str) -> None:
    async with engine.connect() as conn:
        await conn.run_sync(_step(step))
        await conn.commit()


async def _seed_main(engine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE organization_context_versions ("
                " id uuid PRIMARY KEY, version integer NOT NULL UNIQUE,"
                " profile jsonb NOT NULL DEFAULT '{}'::jsonb, source varchar(16) NOT NULL,"
                " author_user_id uuid NULL, created_at timestamptz NOT NULL DEFAULT now())"
            )
        )
        for version, name in ((1, "Old name"), (2, "Acme Energy")):
            await conn.execute(
                text(
                    "INSERT INTO organization_context_versions (id, version, profile, source) "
                    "VALUES (gen_random_uuid(), :v, jsonb_build_object('name', CAST(:n AS text)), 'form')"
                ),
                {"v": version, "n": name},
            )


async def _insert_version(engine, organization_id, version: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organization_context_versions (id, organization_id, version, source) "
                "VALUES (gen_random_uuid(), :o, :v, 'form')"
            ),
            {"o": organization_id, "v": version},
        )


async def test_an_install_with_profiles_upgrades_reruns_and_downgrades():
    async with _database() as engine:
        await _seed_main(engine)
        await _migrate(engine, "upgrade")
        async with engine.begin() as conn:
            primary = (await conn.execute(text("SELECT id, name FROM organizations WHERE role = 'primary'"))).one()
            assert primary.name == "Acme Energy"
            owners = (await conn.execute(text("SELECT DISTINCT organization_id FROM organization_context_versions"))).all()
            assert [o[0] for o in owners] == [primary.id]
            tracked = (
                await conn.execute(
                    text(
                        "INSERT INTO organizations (id, name, role, relationship) "
                        "VALUES (gen_random_uuid(), 'Acme Cloud', 'tracked', 'provider') RETURNING id"
                    )
                )
            ).scalar()
        # Version numbers are per organization now.
        await _insert_version(engine, tracked, 1)
        with pytest.raises(Exception, match="uq_organization_context_versions_org_version"):
            await _insert_version(engine, primary.id, 2)
        with pytest.raises(Exception, match="ck_organizations_relationship_valid"):
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO organizations (id, name, role) VALUES (gen_random_uuid(), 'X', 'tracked')")
                )
        await _migrate(engine, "upgrade")  # re-run: a no-op
        await _migrate(engine, "downgrade")
        async with engine.begin() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM organization_context_versions"))).scalar() == 2
            assert (await conn.execute(text("SELECT to_regclass('organizations')"))).scalar() is None
