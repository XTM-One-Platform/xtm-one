"""A switchover terminating the bootstrap's sessions, against a real server (#10368).

The lock's own connection dies with the others, so its cleanup fails too: that
failure used to replace the bootstrap's error, and the startup must still see
an error it waits out, with the advisory lock released.
"""

from __future__ import annotations

import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy import text

from app.core import migrations
from app.core.db_availability import is_transient_database_error
from app.core.startup_locks import BOOTSTRAP_LOCK_ID
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

_TERMINATE_THE_OTHERS = text(
    "SELECT pg_terminate_backend(pid, 5000) FROM pg_stat_activity "
    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
)


async def test_a_switchover_mid_bootstrap_is_waited_out_and_frees_the_lock(monkeypatch):
    async with scratch_database("failover", tables=(), bind_app_sessions=False) as db:
        monkeypatch.setattr(migrations, "engine", db.engine)

        with pytest.raises(sa_exc.DBAPIError) as raised:
            async with migrations.bootstrap_lock():
                async with db.engine.connect() as work:
                    await work.execute(text("SELECT 1"))
                    async with db.engine.connect() as switchover:
                        await switchover.execute(_TERMINATE_THE_OTHERS)
                    await work.execute(text("SELECT 1"))

        assert "pg_advisory_unlock" not in str(raised.value)
        assert is_transient_database_error(raised.value)
        async with db.engine.connect() as conn:
            assert (await conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": BOOTSTRAP_LOCK_ID})).scalar()
            await conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": BOOTSTRAP_LOCK_ID})


async def test_a_missing_alembic_version_table_reads_as_undefined_table():
    """``run_migrations`` treats only this SQLSTATE (42P01) as a fresh database."""
    from app.core.db_availability import sqlstates_in_chain

    async with (
        scratch_database("fresh", tables=(), bind_app_sessions=False) as db,
        db.engine.connect() as conn,
    ):
        with pytest.raises(sa_exc.DBAPIError) as raised:
            await conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))

    assert "42P01" in sqlstates_in_chain(raised.value)


async def test_a_read_only_primary_is_named_by_its_sqlstate():
    """``DATABASE_URL`` on a replica: the readiness label must say so, not ``DBAPIError``."""
    from app.core.db_availability import is_transient_database_error, outage_label

    async with (
        scratch_database("read_only", tables=(), bind_app_sessions=False) as db,
        db.engine.connect() as conn,
    ):
        await conn.execute(text("SET default_transaction_read_only = on"))
        await conn.commit()
        with pytest.raises(sa_exc.DBAPIError) as raised:
            await conn.execute(text("CREATE TABLE t (id int)"))

    assert outage_label(raised.value) == "25006"
    assert is_transient_database_error(raised.value)
