"""Tests for the Supabase function tools of a database role (``list_supabase_functions``, ``call_supabase_function``).

No database is reached: ``supabase_postgres.session`` is replaced by a fake
connection that records every statement and its bound arguments. Covered: the
compound token, the free SQL tools withheld in that mode, the allowlist
refused before any login, the call sent (bound arguments), the rendering of
rows, long values and errors, and the connection test of the role.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.integrations.providers import supabase
from app.integrations.providers import supabase_postgres as pg
from app.services.content_safety import UNTRUSTED_HEADER
from app.tools.base import CallableToolExecutor
from tests.integration_file_fakes import call, fenced_and_open

_REF = "abcdefghijklmnopqrst"
_PAT = "sbp_0123456789abcdef"
_POOLER = "aws-0-eu-west-1.pooler.supabase.com"
# Free SQL bypasses RLS and log lines carry row values: none next to a role.
_SQL_TOOLS = {"query_supabase_sql", "execute_supabase_sql", "apply_supabase_migration", "query_supabase_logs"}

_SETTINGS = {
    "project_ref": _REF,
    "db_role": "agent_reader",
    "db_password": "s3cret||pw",
    "db_host": _POOLER,
    "db_functions": "public.get_document,public.get_document_excerpt",
}


def _token(**overrides: Any) -> str:
    settings = {**_SETTINGS, **overrides}
    return f"{settings['project_ref']}||{pg.encode_access(settings)}||{_PAT}"


def _executor(token: str | None = None) -> tuple[CallableToolExecutor, list]:
    executor = CallableToolExecutor()
    tools = supabase.build_tools(token if token is not None else _token(), executor)
    return executor, tools


def _function(name: str = "get_document", **overrides: Any) -> dict[str, Any]:
    """A catalog row of ``ALLOWED_FUNCTIONS_SQL``."""
    row = {
        "schema": "public",
        "name": name,
        "oid": 16384,
        "signature": "p_document_id uuid",
        "returns": "text",
        "returns_set": False,
        "returns_void": False,
        "untyped_record": False,
        "security_definer": True,
        "volatility": "s",
        "defaults": 0,
        "executable": True,
        "comment": "One document by id",
        "args": [{"name": "p_document_id", "type": "uuid", "mode": "i"}],
    }
    row.update(overrides)
    return row


class _FakeConnection:
    """Answers the catalog lookup with *functions* and the call with *rows*."""

    def __init__(self, functions: list[dict], rows: list[dict] | Exception | None = None) -> None:
        self.functions = functions
        self.rows = rows or []
        self.fetched: list[tuple[str, tuple]] = []
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *arguments: Any) -> list[dict]:
        self.fetched.append((sql, arguments))
        if sql == pg.ALLOWED_FUNCTIONS_SQL:
            return [dict(row, args=list(row["args"])) for row in self.functions]
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows

    async def fetchrow(self, sql: str, *arguments: Any) -> dict | None:
        self.fetched.append((sql, arguments))
        return {"name": "agent_reader", "superuser": False, "bypass_rls": False, "create_role": False}

    async def execute(self, sql: str, *arguments: Any) -> str:
        self.executed.append((sql, arguments))
        return "SELECT 1"


@pytest.fixture
def database(monkeypatch):
    """Replace the session: records who logged in and runs the work on a fake connection."""
    state: dict[str, Any] = {"connection": _FakeConnection([_function()]), "logins": []}

    async def _session(access, project_ref, work, *, database=None, allow_rls_bypass=False):
        state["bypass_allowed"] = allow_rls_bypass
        state["logins"].append((access.role, access.host, project_ref, pg.login_user(access, project_ref)))
        return await work(state["connection"])

    monkeypatch.setattr(pg, "session", _session)
    return state


def _block(content: str) -> str:
    """The data of the single quarantine block of a result, its source line left out."""
    match = re.search(r"<<UNTRUSTED-([0-9a-f]+)>>\n\[source: [^\n]*\]\n(.*)\n<<END-UNTRUSTED-\1>>", content, re.S)
    assert match is not None, content
    return match.group(2)


def test_the_compound_token_is_split_in_both_formats():
    assert supabase.split_token(_token()) == (_REF, pg.encode_access(_SETTINGS), _PAT)
    assert supabase.split_token(f"{_REF}||||{_PAT}") == (_REF, "", _PAT)
    assert supabase.split_token(f"{_REF}||{_PAT}") == (_REF, "", _PAT)
    assert supabase.split_token(f"||{_PAT}") == ("", "", _PAT)


# ---------------------------------------------------------------------------
# The tools of the database role mode
# ---------------------------------------------------------------------------


def test_a_database_role_withholds_the_free_sql_tools():
    executor, tools = _executor()
    names = {tool.name for tool in tools}
    assert names & _SQL_TOOLS == set()
    assert {"list_supabase_functions", "call_supabase_function"} <= names
    assert set(executor._handlers) == names


def test_an_unusable_database_role_still_withholds_the_free_sql_tools():
    executor, tools = _executor(f"{_REF}||pg:%%%||{_PAT}")
    assert {tool.name for tool in tools} & _SQL_TOOLS == set()
    assert set(executor._handlers) & _SQL_TOOLS == set()


@pytest.mark.asyncio
async def test_without_a_database_role_the_function_tools_say_how_to_set_one(database):
    executor, tools = _executor(f"{_REF}||||{_PAT}")
    assert {tool.name for tool in tools} >= _SQL_TOOLS
    result = await call(executor, "call_supabase_function", function="public.get_document")
    assert result.is_error
    assert "has no database role" in result.content
    assert database["logins"] == []


@pytest.mark.asyncio
async def test_an_unusable_database_role_is_reported_without_a_login(database):
    executor, _tools = _executor(_token(db_host="evil.example.com"))
    result = await call(executor, "list_supabase_functions")
    assert result.is_error
    assert "database role is unusable" in result.content
    assert database["logins"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("function", ["public.rpc_secret_dump", "private.get_document", "rpc_secret_dump", ""])
async def test_a_function_outside_the_allowlist_is_refused_before_any_login(database, function):
    executor, _tools = _executor()
    result = await call(executor, "call_supabase_function", function=function)
    assert result.is_error
    assert database["logins"] == []


@pytest.mark.asyncio
async def test_a_call_binds_every_argument_and_casts_it_to_the_declared_type(database):
    database["connection"].rows = [{"j": "Speaker A: hello", "n": 16, "total": 1}]
    executor, _tools = _executor()
    call_id = "6f1c2c1e-3b7a-4c55-9a43-0d1f2b3c4d5e"
    result = await call(
        executor,
        "call_supabase_function",
        function="get_document",
        arguments={"p_document_id": call_id},
    )
    assert not result.is_error, result.content
    assert database["logins"] == [("agent_reader", _POOLER, _REF, f"agent_reader.{_REF}")]
    lookup, invocation = database["connection"].fetched
    assert lookup == (pg.ALLOWED_FUNCTIONS_SQL, (["public.get_document"],))
    sql, arguments = invocation
    assert sql == pg.result_sql('"public"."get_document"("p_document_id" => $3::pg_catalog.text::uuid)', 100)
    # $1 the offset, $2 the output budget, then the argument, bound.
    assert arguments == (0, supabase._MAX_OUTPUT_CHARS, call_id)
    assert call_id not in sql
    assert result.content.startswith(
        "public.get_document(p_document_id uuid) as database role agent_reader returned 1 row:"
    )
    assert _block(result.content) == "Speaker A: hello"


@pytest.mark.asyncio
async def test_an_injection_in_an_argument_stays_a_bound_value(database):
    executor, _tools = _executor()
    hostile = "x'); drop table public.documents; --"
    await call(executor, "call_supabase_function", function="public.get_document", arguments={"p_document_id": hostile})
    _lookup, (sql, arguments) = database["connection"].fetched
    assert hostile not in sql
    assert arguments[-1] == hostile


@pytest.mark.asyncio
async def test_a_long_value_says_which_offset_reads_on(database):
    database["connection"].rows = [{"j": "a" * 40_000, "n": 95_000, "total": 1}]
    executor, _tools = _executor()
    result = await call(
        executor,
        "call_supabase_function",
        function="public.get_document",
        arguments={"p_document_id": "x"},
        offset=40_000,
    )
    assert database["connection"].fetched[-1][1][:2] == (40_000, supabase._MAX_OUTPUT_CHARS)
    assert result.content.endswith(
        "[row 1: characters 40,001-80,000 of 95,000 shown; call again with offset=80000 to read on]"
    )
    # At least the 10,000 characters per call the pipelines need.
    assert _block(result.content).count("a") >= 10_000


@pytest.mark.asyncio
async def test_rows_stop_at_the_output_budget_and_say_so(database):
    database["connection"].rows = [{"j": "b" * 15_000, "n": 15_000, "total": 5} for _ in range(4)]
    executor, _tools = _executor()
    result = await call(
        executor, "call_supabase_function", function="public.get_document", arguments={"p_document_id": "x"}
    )
    assert result.content.startswith(
        "public.get_document(p_document_id uuid) as database role agent_reader returned 5 rows - "
        "2 shown within the output limit"
    )


@pytest.mark.asyncio
async def test_a_void_function_runs_and_returns_nothing(database):
    database["connection"].functions = [_function(returns="void", returns_void=True)]
    executor, _tools = _executor()
    result = await call(
        executor, "call_supabase_function", function="public.get_document", arguments={"p_document_id": "x"}
    )
    assert (
        result.content
        == "Called public.get_document(p_document_id uuid) as database role agent_reader; it returns nothing."
    )
    assert database["connection"].executed == [
        ('select "public"."get_document"("p_document_id" => $1::pg_catalog.text::uuid)', ("x",))
    ]


@pytest.mark.asyncio
async def test_an_untyped_record_set_and_an_ambiguous_overload_are_refused(database):
    database["connection"].functions = [_function(untyped_record=True, returns="SETOF record")]
    executor, _tools = _executor()
    untyped = await call(
        executor, "call_supabase_function", function="public.get_document", arguments={"p_document_id": "x"}
    )
    assert "untyped record set" in untyped.content
    database["connection"].functions = [
        _function(),
        _function(signature="p_document_id text", args=[{"name": "p_document_id", "type": "text", "mode": "i"}]),
    ]
    ambiguous = await call(
        executor, "call_supabase_function", function="public.get_document", arguments={"p_document_id": "x"}
    )
    assert "several versions" in ambiguous.content
    assert len(database["connection"].fetched) == 2  # the two lookups, never a call


@pytest.mark.asyncio
async def test_a_function_the_role_cannot_execute_is_refused(database):
    database["connection"].functions = [_function(executable=False)]
    executor, _tools = _executor()
    result = await call(
        executor, "call_supabase_function", function="public.get_document", arguments={"p_document_id": "x"}
    )
    assert result.is_error
    assert "cannot execute public.get_document" in result.content


@pytest.mark.asyncio
async def test_a_database_refusal_comes_back_quarantined(monkeypatch):
    async def _refused(*_args, **_kwargs):
        raise pg.DatabaseAccessError(
            "Error: the database refused the call.",
            untrusted="InsufficientPrivilegeError: permission denied for view v_reader_scope",
        )

    monkeypatch.setattr(pg, "session", _refused)
    executor, _tools = _executor()
    result = await call(executor, "list_supabase_functions")
    assert result.is_error
    inside, outside = fenced_and_open(result.content)
    assert "permission denied for view v_reader_scope" in inside
    assert "v_reader_scope" not in outside
    assert UNTRUSTED_HEADER in result.content


@pytest.mark.asyncio
async def test_the_function_listing_names_what_is_missing(database):
    database["connection"].functions = [_function()]
    executor, _tools = _executor()
    result = await call(executor, "list_supabase_functions")
    assert result.content.startswith(
        "1 function callable through call_supabase_function as database role agent_reader "
        "(1 allowed name(s) not found: public.get_document_excerpt):"
    )
    assert "public.get_document(p_document_id uuid) -> text [security definer]" in _block(result.content)


@pytest.mark.asyncio
async def test_no_allowed_function_reaches_no_data(database):
    executor, _tools = _executor(_token(db_functions=""))
    result = await call(executor, "list_supabase_functions")
    assert result.content.startswith("No function is allowed yet")
    assert database["logins"] == []


# ---------------------------------------------------------------------------
# Test Connection and the Management API probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_database_reports_the_role_and_its_functions(database):
    ok, message = await supabase.validate_database(_SETTINGS)
    assert ok
    assert message == "database role agent_reader logged in; 1 of 2 allowed functions executable"
    # Test Connection reports a role that bypasses RLS rather than failing on it.
    assert database["bypass_allowed"] is True
    assert await supabase.validate_database({"project_ref": _REF}) == (True, "")
    ok, message = await supabase.validate_database({**_SETTINGS, "db_role": "postgres"})
    assert not ok
    assert "platform role" in message


class _RefusingConnection:
    """A logged-in connection whose catalog read of the functions is refused."""

    def transaction(self):
        connection = self

        class _Transaction:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_exc):
                return False

        return _Transaction()

    async def execute(self, _sql: str) -> None:
        return None

    async def fetchrow(self, _sql: str, *_args: Any) -> dict:
        return {"name": "ci_reader", "superuser": False, "bypass_rls": False, "create_role": False}

    async def fetch(self, sql: str, *_args: Any) -> list[dict]:
        if sql == pg.EXECUTABLE_FUNCTIONS_SQL:
            raise pg.asyncpg.InsufficientPrivilegeError("permission denied for table v_secret_scope")
        return []

    async def close(self, timeout: float | None = None) -> None:
        return None


@pytest.mark.asyncio
async def test_the_probe_names_the_step_and_the_condition_a_refusal_came_from(monkeypatch):
    from app.api.v1 import supabase_console as console
    from app.api.v1.schemas.supabase import SupabaseProbeResponse

    async def _connect(*_args: Any, **_kwargs: Any) -> _RefusingConnection:
        return _RefusingConnection()

    monkeypatch.setattr(pg, "connect", _connect)
    response = SupabaseProbeResponse()
    await console._probe_database({**_SETTINGS, "db_role": "ci_reader"}, response)
    assert response.errors["database"] == (
        "the database refused the call (InsufficientPrivilegeError, SQLSTATE 42501) "
        "while listing the functions the role may execute."
    )
    # The database's own words stay in the log.
    assert "v_secret_scope" not in response.errors["database"]
