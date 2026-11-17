"""Tests for ``supabase_postgres``: a Supabase project's Postgres, logged in as one role.

No database is reached: the address resolution and ``asyncpg.connect`` are
fakes. Covered: the settings carried in the compound token and their
validation (host allowlist, platform roles, ports, function names), the
catalog lookup, the call built for a function (quoted identifiers, every
argument bound and cast to its declared type, defaults, positional calls,
arrays), the server-side bounded result query, the address policy, the relay
refusal, the TLS host name, the login errors and the refusal of a role that
bypasses row level security.
"""

from __future__ import annotations

import contextlib
import socket
import ssl
from typing import Any

import pytest

from app.core.egress import EgressRoute, egress_route
from app.integrations.providers import supabase_postgres as pg

_REF = "abcdefghijklmnopqrst"
_POOLER = "aws-0-eu-west-1.pooler.supabase.com"

_SETTINGS = {
    "project_ref": _REF,
    "db_role": "agent_reader",
    "db_password": "s3cret||pw",
    "db_host": _POOLER,
    "db_functions": "public.get_document,public.get_document_excerpt",
}


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


# ---------------------------------------------------------------------------
# Settings: token segment and validation
# ---------------------------------------------------------------------------


def test_the_segment_round_trips_any_password_and_is_empty_without_a_role():
    segment = pg.encode_access(_SETTINGS)
    assert segment.startswith(pg.TOKEN_SEGMENT_PREFIX)
    assert "|" not in segment
    assert pg.decode_access(segment)["password"] == "s3cret||pw"
    assert pg.encode_access({"db_password": "x", "db_host": _POOLER}) == ""


def test_a_valid_setting_gives_the_role_and_its_pooler_user():
    access, problem = pg.validate_access(pg.decode_access(pg.encode_access(_SETTINGS)), _REF)
    assert problem is None
    assert access == pg.DatabaseAccess(
        role="agent_reader",
        password="s3cret||pw",
        host=_POOLER,
        port=5432,
        database="postgres",
        ca_cert="",
        functions=("public.get_document", "public.get_document_excerpt"),
    )
    assert pg.login_user(access, _REF) == f"agent_reader.{_REF}"
    direct, _ = pg.validate_access(
        pg.decode_access(pg.encode_access({**_SETTINGS, "db_host": f"db.{_REF}.supabase.co"})), _REF
    )
    assert direct is not None
    assert pg.login_user(direct, _REF) == "agent_reader"
    assert "s3cret" not in repr(access)


@pytest.mark.parametrize(
    ("overrides", "project_ref", "problem"),
    [
        ({"db_role": "postgres"}, _REF, "platform role"),
        ({"db_role": "service_role"}, _REF, "platform role"),
        ({"db_role": "supabase_read_only_user"}, _REF, "platform role"),
        ({"db_role": "pg_read_all_data"}, _REF, "platform role"),
        ({"db_role": "ci agent"}, _REF, "Postgres role name"),
        ({"db_role": 'x"; drop'}, _REF, "Postgres role name"),
        ({"db_password": ""}, _REF, "no password"),
        ({"db_host": ""}, _REF, "host is not set"),
        ({"db_host": "evil.example.com"}, _REF, "must be a Supabase pooler"),
        ({"db_host": "10.0.0.1"}, _REF, "must be a Supabase pooler"),
        ({"db_host": "pooler.supabase.com.evil.com"}, _REF, "must be a Supabase pooler"),
        ({"db_host": "db.zyxwvutsrqponmlkjihg.supabase.co"}, _REF, "another project"),
        ({}, "", "needs the integration's Project ref"),
        ({"db_port": "5433"}, _REF, "port must be"),
        ({"db_name": "postgres; drop"}, _REF, "database name"),
        ({"db_ca_cert": "not a pem"}, _REF, "PEM"),
        ({"db_functions": "get_document"}, _REF, "schema.name"),
        ({"db_functions": "public.f(); drop"}, _REF, "schema.name"),
        ({"db_functions": ",".join(f"public.f{i}" for i in range(101))}, _REF, "at most 100"),
    ],
)
def test_an_unusable_setting_is_refused(overrides, project_ref, problem):
    access, message = pg.validate_access(pg.decode_access(pg.encode_access({**_SETTINGS, **overrides})), project_ref)
    assert access is None
    assert problem in message


def test_an_unreadable_segment_is_refused():
    with pytest.raises(ValueError):
        pg.decode_access("pg:%%%")
    with pytest.raises(ValueError):
        pg.decode_access("pg:" + "WzFd")  # a JSON list, not an object


# ---------------------------------------------------------------------------
# Catalog lookup and the call of a function
# ---------------------------------------------------------------------------


def test_the_allowed_functions_are_looked_up_by_a_bound_name_list():
    assert "(n.nspname || '.' || p.proname) = any($1::pg_catalog.text[])" in pg.ALLOWED_FUNCTIONS_SQL
    assert "p.prokind = 'f'" in pg.ALLOWED_FUNCTIONS_SQL


def test_catalog_rows_come_back_with_their_arguments_parsed():
    rows = pg.function_rows(
        [
            {"name": "f", "args": '[{"name": "p_id", "type": "uuid", "mode": "i"}, "junk"]'},
            {"name": "g", "args": "not json"},
            {"name": "h", "args": None},
        ]
    )
    assert [row["args"] for row in rows] == [[{"name": "p_id", "type": "uuid", "mode": "i"}], [], []]


def test_the_call_builder_handles_defaults_arrays_json_and_positions():
    function = _function(
        "get_document_excerpt",
        defaults=2,
        args=[
            {"name": "p_document_ids", "type": "uuid[]", "mode": "i"},
            {"name": "p_filters", "type": "jsonb", "mode": "i"},
            {"name": "p_limit", "type": "integer", "mode": "i"},
            {"name": "speaker", "type": "text", "mode": "o"},
        ],
    )
    call_expression, parameters, problem = pg.build_call(
        function, {"p_document_ids": ["a", "b"], "p_filters": {"lang": "fr"}}
    )
    assert problem is None
    assert call_expression == (
        '"public"."get_document_excerpt"('
        '"p_document_ids" => array(select pg_catalog.jsonb_array_elements_text($3::pg_catalog.jsonb))::uuid[], '
        '"p_filters" => $4::pg_catalog.text::jsonb)'
    )
    assert parameters == ['["a", "b"]', '{"lang": "fr"}']
    assert pg.build_call(function, {})[2] == "missing required argument(s): p_document_ids"
    assert pg.build_call(function, {"p_document_ids": [], "speaker": "x"})[2].startswith("unknown argument(s) speaker")
    positional = _function(
        args=[{"name": None, "type": "integer", "mode": None}, {"name": None, "type": "boolean", "mode": None}],
        defaults=1,
    )
    assert pg.build_call(positional, {"1": 7, "2": True}, first_parameter=1)[:2] == (
        '"public"."get_document"($1::pg_catalog.text::integer, $2::pg_catalog.text::boolean)',
        ["7", "true"],
    )
    assert pg.build_call(positional, {"2": True})[2] == "missing required argument(s): 1"
    all_defaults = {**positional, "defaults": 2}
    assert "without a gap" in pg.build_call(all_defaults, {"2": True})[2]
    variadic = _function(args=[{"name": "items", "type": "text[]", "mode": "v"}])
    assert "VARIADIC" in pg.build_call(variadic, {"items": ["a"]})[2]
    too_long = pg.build_call(_function(), {"p_document_id": "x" * (pg.MAX_ARGUMENT_CHARS + 1)})[2]
    assert "is over" in too_long


def test_the_result_query_is_bounded_server_side():
    sql = pg.result_sql('"public"."f"()', 25)
    assert 'from "public"."f"() as r' in sql
    assert "limit 26" in sql
    assert "pg_catalog.substr(t.j, $1 + 1, $2)" in sql
    assert "where coalesce(w.sent, 0) < $2" in sql


# ---------------------------------------------------------------------------
# Connection: address policy, relay, TLS host name
# ---------------------------------------------------------------------------


def _access(**overrides: Any) -> pg.DatabaseAccess:
    access, problem = pg.validate_access(pg.decode_access(pg.encode_access({**_SETTINGS, **overrides})), _REF)
    assert access is not None, problem
    return access


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1", "fd00:ec2::254"])
async def test_a_host_resolving_to_a_refused_address_is_never_dialled(monkeypatch, address):
    dialled = []

    async def _getaddrinfo(self, host, port, **_kwargs):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, port))]

    async def _connect(**kwargs):
        dialled.append(kwargs)

    monkeypatch.setattr("asyncio.BaseEventLoop.getaddrinfo", _getaddrinfo)
    monkeypatch.setattr(pg.asyncpg, "connect", _connect)
    with pytest.raises(pg.DatabaseAccessError, match="resolves to an address the platform refuses"):
        await pg.connect(_access(), _REF)
    assert dialled == []


@pytest.mark.asyncio
async def test_the_connection_dials_the_vetted_address_with_the_pooler_user(monkeypatch):
    dialled = []

    async def _getaddrinfo(self, host, port, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("3.120.0.10", port))]

    async def _connect(**kwargs):
        dialled.append(kwargs)
        return "connection"

    monkeypatch.setattr("asyncio.BaseEventLoop.getaddrinfo", _getaddrinfo)
    monkeypatch.setattr(pg.asyncpg, "connect", _connect)
    assert await pg.connect(_access(), _REF) == "connection"
    (kwargs,) = dialled
    assert (kwargs["host"], kwargs["port"], kwargs["user"], kwargs["database"]) == (
        "3.120.0.10",
        5432,
        f"agent_reader.{_REF}",
        "postgres",
    )
    assert kwargs["password"] == "s3cret||pw"
    context = kwargs["ssl"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.verified_hostname == _POOLER


@pytest.mark.asyncio
async def test_an_ipv6_address_is_dialled_bracketed(monkeypatch):
    dialled = []

    async def _getaddrinfo(self, host, port, **_kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2a05:d018:135e:1601::1", port, 0, 0))]

    async def _connect(**kwargs):
        dialled.append(kwargs["host"])
        return "connection"

    monkeypatch.setattr("asyncio.BaseEventLoop.getaddrinfo", _getaddrinfo)
    monkeypatch.setattr(pg.asyncpg, "connect", _connect)
    await pg.connect(_access(db_host=f"db.{_REF}.supabase.co"), _REF)
    assert dialled == ["[2a05:d018:135e:1601::1]"]
    # asyncpg reads the bracketed form as one address on the given port.
    from asyncpg.connect_utils import _parse_hostlist

    assert _parse_hostlist(dialled[0], 5432) == (["2a05:d018:135e:1601::1"], [5432])


class _SessionConnection:
    def __init__(self, bypasses: bool) -> None:
        self.bypasses = bypasses
        self.worked = False

    def transaction(self):
        return contextlib.nullcontext()

    async def execute(self, _sql):
        return "SET"

    async def fetchval(self, sql):
        assert "rolbypassrls" in sql
        return self.bypasses

    async def close(self, timeout=None):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("bypasses", [True, False])
async def test_a_role_that_bypasses_rls_is_refused_before_any_work(monkeypatch, bypasses):
    connection = _SessionConnection(bypasses)

    async def _connect(*_args, **_kwargs):
        return connection

    async def _work(_connection):
        connection.worked = True
        return "rows"

    monkeypatch.setattr(pg, "connect", _connect)
    if bypasses:
        with pytest.raises(pg.DatabaseAccessError, match="bypasses row level security"):
            await pg.session(_access(), _REF, _work)
        assert connection.worked is False
        # The probe and Test Connection log in anyway, to report it.
        assert await pg.session(_access(), _REF, _work, allow_rls_bypass=True) == "rows"
    else:
        assert await pg.session(_access(), _REF, _work) == "rows"
    assert connection.worked is True


def test_the_tls_context_verifies_the_host_name_not_the_dialled_address():
    context = pg._tls_context(_access())
    tls = context.wrap_bio(ssl.MemoryBIO(), ssl.MemoryBIO(), server_hostname="3.120.0.10")
    assert tls.server_hostname == _POOLER


def test_an_unreadable_ca_certificate_is_refused():
    with pytest.raises(pg.DatabaseAccessError, match="CA certificate cannot be read"):
        pg._tls_context(_access(db_ca_cert="-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----"))


@pytest.mark.asyncio
async def test_a_relay_routed_integration_never_connects_directly(monkeypatch):
    async def _getaddrinfo(*_args, **_kwargs):
        raise AssertionError("resolved")

    monkeypatch.setattr("asyncio.BaseEventLoop.getaddrinfo", _getaddrinfo)
    route = EgressRoute(group_id="g", source_id="s")
    with egress_route(route), pytest.raises(pg.DatabaseAccessError, match="relay group"):
        await pg.connect(_access(), _REF)


@pytest.mark.parametrize(
    ("error", "wording"),
    [
        (pg.asyncpg.InvalidPasswordError("password authentication failed for user x"), "check its password"),
        (pg.asyncpg.InvalidCatalogNameError("database nope does not exist"), "does not exist"),
        (pg.asyncpg.InvalidAuthorizationSpecificationError("role x does not exist"), "not be a LOGIN role"),
        (ssl.SSLCertVerificationError("self signed certificate in chain"), "could not be verified"),
        (TimeoutError(), "did not answer in time"),
        (ConnectionRefusedError("refused"), "cannot be reached"),
    ],
)
def test_a_login_failure_is_worded_without_the_server_message(error, wording):
    message = str(pg._connect_error(error, _access()))
    assert wording in message
    assert str(error) == "" or str(error) not in message
    assert "s3cret" not in message
