"""The catalog SQL of ``supabase_postgres``, run on PostgreSQL as the role.

Every query the probe, the connection test and the function tools send is
run here as a plain role (``SET ROLE``): the catalog reads, and a call built
by ``build_call`` and wrapped by ``result_sql``. Postgres alone tells whether
a query parses - ``pg_catalog.unnest`` of three arrays or
``pg_catalog.greatest`` do not exist, and nothing else would notice.

The row level security bypass check of ``supabase_postgres``:

``BYPASSES_RLS`` decides whether a database role is refused (``session``) or
flagged (the probe, the connection test, the login roles list). Postgres
skips RLS for a superuser, a BYPASSRLS role, and the owner of a table - and
every role inheriting the owner's privileges - unless the table forces RLS:
each case is checked here on a real catalog, as the role (``SET ROLE``).

The roles are created inside a transaction that is rolled back, so nothing
outlives the test on the shared cluster.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
import pytest

from app.integrations.providers import supabase_postgres as pg
from tests.scratch_database import scratch_database


@asynccontextmanager
async def _rolled_back() -> AsyncIterator[tuple[asyncpg.Connection, str, str]]:
    """A connection in a transaction, an RLS table and a reader role, all rolled back."""
    async with scratch_database("supabase_rls", tables=(), bind_app_sessions=False) as db:
        connection = await asyncpg.connect(db.url.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            transaction = connection.transaction()
            await transaction.start()
            suffix = uuid.uuid4().hex[:10]
            owner, reader = f"rls_owner_{suffix}", f"rls_reader_{suffix}"
            try:
                await connection.execute(f"create role {owner} nologin")
                await connection.execute(f"create role {reader} nologin")
            except asyncpg.InsufficientPrivilegeError:
                await transaction.rollback()
                pytest.skip("the test database role cannot create roles")
            await connection.execute("create table documents (id int, owner_id uuid)")
            await connection.execute("alter table documents enable row level security")
            await connection.execute(f"alter table documents owner to {owner}")
            try:
                yield connection, owner, reader
            finally:
                await transaction.rollback()
        finally:
            await connection.close()


async def _bypasses_as(connection: asyncpg.Connection, role: str) -> tuple[bool, bool]:
    """The session check and the probe's flag, read as *role*."""
    await connection.execute(f"set local role {role}")
    try:
        refused = await connection.fetchval(pg._BYPASSES_RLS_SQL)
        flagged = (await connection.fetchrow(pg.ROLE_ATTRIBUTES_SQL))["bypass_rls"]
    finally:
        await connection.execute("reset role")
    return bool(refused), bool(flagged)


@pytest.mark.asyncio
async def test_a_role_under_rls_is_neither_refused_nor_flagged():
    async with _rolled_back() as (connection, _owner, reader):
        assert await _bypasses_as(connection, reader) == (False, False)


@pytest.mark.asyncio
async def test_a_role_inheriting_a_table_owner_bypasses_rls():
    async with _rolled_back() as (connection, owner, reader):
        await connection.execute(f"grant {owner} to {reader}")
        assert await _bypasses_as(connection, reader) == (True, True)
        assert await _bypasses_as(connection, owner) == (True, True)


@pytest.mark.asyncio
async def test_a_table_that_forces_rls_keeps_its_owner_under_it():
    async with _rolled_back() as (connection, owner, reader):
        await connection.execute(f"grant {owner} to {reader}")
        await connection.execute("alter table documents force row level security")
        assert await _bypasses_as(connection, reader) == (False, False)


@pytest.mark.asyncio
async def test_a_bypassrls_role_bypasses_rls():
    async with _rolled_back() as (connection, _owner, reader):
        await connection.execute(f"alter role {reader} bypassrls")
        assert await _bypasses_as(connection, reader) == (True, True)


@pytest.mark.asyncio
async def test_the_login_roles_list_flags_a_table_owner():
    from app.integrations.providers import supabase

    async with _rolled_back() as (connection, owner, reader):
        await connection.execute(f"alter role {reader} login")
        await connection.execute(f"alter role {owner} login")
        rows = {row["name"]: row["bypass_rls"] for row in await connection.fetch(supabase._LOGIN_ROLES_SQL)}
        assert rows[owner] is True
        assert rows[reader] is False


@pytest.mark.asyncio
async def test_the_probe_and_the_function_tools_run_as_a_plain_role():
    async with _rolled_back() as (connection, _owner, reader):
        await connection.execute("insert into documents values (1, null), (2, null)")
        await connection.execute(
            "create function public.count_documents(p_min int) returns bigint language sql stable "
            "security definer as $$ select count(*) from documents where id >= p_min $$"
        )
        await connection.execute(
            "create function public.search_documents(p_query text, p_ids int[] default null, "
            "out id int, out label text) returns setof record language sql stable "
            "as $$ select i, p_query || i from pg_catalog.unnest(coalesce(p_ids, array[1, 2])) as i $$"
        )
        for name in ("count_documents(int)", "search_documents(text, int[])"):
            await connection.execute(f"revoke execute on function public.{name} from public")
            await connection.execute(f"grant execute on function public.{name} to {reader}")
        await connection.execute(f"set local role {reader}")

        assert (await connection.fetchrow(pg.ROLE_ATTRIBUTES_SQL))["name"] == reader
        assert await connection.fetch(pg.DATABASES_SQL)
        assert await connection.fetch(pg.READABLE_RELATIONS_SQL, list(pg.SYSTEM_SCHEMAS)) == []
        executable = pg.function_rows(await connection.fetch(pg.EXECUTABLE_FUNCTIONS_SQL, list(pg.SYSTEM_SCHEMAS)))
        assert {(row["schema"], row["name"]) for row in executable} == {
            ("public", "count_documents"),
            ("public", "search_documents"),
        }
        allowed = {
            row["name"]: row
            for row in pg.function_rows(
                await connection.fetch(
                    pg.ALLOWED_FUNCTIONS_SQL, ["public.count_documents", "public.search_documents"]
                )
            )
        }
        search = allowed["search_documents"]
        assert [(arg["name"], arg["type"], arg["has_default"]) for arg in pg.input_arguments(search)] == [
            ("p_query", "text", False),
            ("p_ids", "integer[]", True),
        ]

        async def _call(function: dict, arguments: dict, offset: int = 0) -> list[str]:
            call, parameters, problem = pg.build_call(function, arguments)
            assert problem is None
            rows = await connection.fetch(pg.result_sql(call, 10), offset, 1000, *parameters)
            return [row["j"] for row in rows]

        assert await _call(allowed["count_documents"], {"p_min": 2}) == ["1"]
        assert await _call(search, {"p_query": "doc", "p_ids": [2]}) == ['{"id": 2, "label": "doc2"}']
        whole = await _call(search, {"p_query": "doc"})
        assert whole == ['{"id": 1, "label": "doc1"}', '{"id": 2, "label": "doc2"}']
        assert await _call(search, {"p_query": "doc"}, offset=10) == [text[10:] for text in whole]
