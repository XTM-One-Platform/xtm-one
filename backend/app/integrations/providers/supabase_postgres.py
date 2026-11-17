# SPDX-License-Identifier: Apache-2.0

"""Supabase database role access - the project's Postgres, logged in as one role.

The Management API reaches a database only as ``postgres`` or as
``supabase_read_only_user``, and both bypass row level security. A Supabase
integration can instead be given a Postgres login role of the project (its
name, password, database and connection host): the database calls of the
integration then log in as that role, so its grants and its RLS policies are
the database's own guarantee, not a filter of this platform.

In that mode the agent reaches the data only through functions:

* the integration names the functions it may call (``db_functions``,
  ``schema.name`` each); a function outside that list is refused before any
  connection, whatever the role may execute;
* ``call_supabase_function`` looks the function up in the catalog as the role,
  builds the call itself (identifiers quoted, every argument bound as a
  parameter and cast to the declared type), and never runs SQL written by the
  model;
* the result is cut server side: each row's text is sliced to the output
  budget and rows stop once the budget is used, so what crosses the wire stays
  bounded whatever the function returns. A longer value is read on with
  ``offset``.

Connection
----------
The host is a Supabase pooler (``*.pooler.supabase.com``, user
``role.project_ref``) or the project's direct host (``db.<ref>.supabase.co``,
user ``role``). It is resolved once; every address must be public
(``destination_policy``) and the connection goes to that address, the TLS
certificate verified against the host name (system trust store, or the
project's CA certificate when one is configured). A connection never leaves
through a relay group: a relay carries HTTP only, so a relay-routed
integration refuses the database calls rather than leaving directly.

Every failure is worded here for the model and the admin; a database message
(``permission denied for view ...``) comes back quarantined, the password and
the connection string never.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import re
import socket
import ssl
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.core.destination_policy import forbidden_address_reason
from app.core.egress import current_route

logger = logging.getLogger(__name__)

# Marks the database segment of the compound token (``integration_tools``).
TOKEN_SEGMENT_PREFIX = "pg:"  # noqa: S105 - a marker, not a secret

DEFAULT_DATABASE = "postgres"
DEFAULT_PORT = 5432
_PORTS = (5432, 6543)
CONNECT_TIMEOUT = 10
# One call end to end: connect, look the function up, run it.
CALL_TIMEOUT = 90
STATEMENT_TIMEOUT = "60s"
MAX_FUNCTIONS = 100
MAX_ARGUMENT_CHARS = 100_000

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,62}")
_DATABASE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,62}")
_FUNCTION_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_$]{0,62})\.([A-Za-z_][A-Za-z0-9_$]{0,62})")
_POOLER_HOST_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+pooler\.supabase\.com")
_DIRECT_HOST_RE = re.compile(r"db\.([a-z]{20})\.supabase\.co")
_PROJECT_REF_RE = re.compile(r"[a-z]{20}")

# Schemas a project's own functions never live in: Postgres', Supabase's and
# the extensions'. The probe lists the functions of every other schema.
SYSTEM_SCHEMAS = (
    "pg_catalog",
    "information_schema",
    "pg_toast",
    "auth",
    "storage",
    "realtime",
    "_realtime",
    "_analytics",
    "graphql",
    "graphql_public",
    "extensions",
    "pgsodium",
    "pgsodium_masks",
    "vault",
    "net",
    "cron",
    "pgbouncer",
    "supabase_functions",
    "supabase_migrations",
    "pgmq",
    "pgtle",
    "tiger",
    "topology",
)

# A role the Supabase platform owns: never offered as the integration's role.
_PLATFORM_ROLE_RE = re.compile(r"^(pg_|supabase_)|^(authenticator|pgbouncer|dashboard_user|pgsodium_.*)$")


class DatabaseAccessError(Exception):
    """A database call that could not run; ``str()`` is worded for the model and the admin.

    *untrusted* holds the database's own message (``permission denied for
    ...``), which the caller quarantines; it never carries a credential.
    """

    def __init__(self, message: str, *, untrusted: str = "") -> None:
        super().__init__(message)
        self.untrusted = untrusted


@dataclass(frozen=True)
class DatabaseAccess:
    """The database role an integration logs in as (``extra_config`` ``db_*``)."""

    role: str
    password: str = field(repr=False)
    host: str
    port: int = DEFAULT_PORT
    database: str = DEFAULT_DATABASE
    ca_cert: str = field(default="", repr=False)
    functions: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Configuration: extra_config <-> token segment, validation
# ---------------------------------------------------------------------------


def split_functions(value: Any) -> list[str]:
    """``db_functions`` as a list: a comma / newline separated string or a list."""
    items = [str(item) for item in value] if isinstance(value, list | tuple) else re.split(r"[,\n]", str(value or ""))
    return list(dict.fromkeys(item.strip() for item in items if item and item.strip()))


def encode_access(extra: dict[str, Any]) -> str:
    """The token segment for *extra*'s database role, ``""`` when none is set.

    Base64url JSON behind a prefix: the password may hold ``||`` or anything
    else, the segment never does.
    """
    role = str(extra.get("db_role") or "").strip()
    if not role:
        return ""
    document = {
        "role": role,
        "password": str(extra.get("db_password") or ""),
        "host": str(extra.get("db_host") or "").strip().lower(),
        "port": str(extra.get("db_port") or "").strip(),
        "database": str(extra.get("db_name") or "").strip(),
        "ca_cert": str(extra.get("db_ca_cert") or "").strip(),
        "functions": split_functions(extra.get("db_functions")),
    }
    raw = json.dumps(document, separators=(",", ":")).encode()
    return TOKEN_SEGMENT_PREFIX + base64.urlsafe_b64encode(raw).decode()


def decode_access(segment: str) -> dict[str, Any]:
    """The raw document of a token segment; ``ValueError`` when unreadable."""
    if not segment.startswith(TOKEN_SEGMENT_PREFIX):
        raise ValueError("not a database segment")
    try:
        document = json.loads(base64.urlsafe_b64decode(segment[len(TOKEN_SEGMENT_PREFIX) :].encode()))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("unreadable database segment") from exc
    if not isinstance(document, dict):
        raise ValueError("unreadable database segment")
    return document


def validate_access(document: dict[str, Any], project_ref: str) -> tuple[DatabaseAccess | None, str | None]:
    """A ``DatabaseAccess`` from a raw document, or why it is unusable.

    Every check fails closed: a database role that is set but unusable refuses
    the database calls, it never falls back to the Management API's roles.
    """
    role = str(document.get("role") or "").strip()
    if not _IDENTIFIER_RE.fullmatch(role):
        return None, (
            "the database role must be a Postgres role name (letters, digits, '_' or '$', "
            "at most 63 characters, not starting with a digit)"
        )
    if _PLATFORM_ROLE_RE.search(role) or role in ("postgres", "service_role"):
        return None, f"the database role {role} is a Supabase platform role that bypasses row level security"
    password = str(document.get("password") or "")
    if not password:
        return None, "the database role has no password"
    host = str(document.get("host") or "").strip().lower()
    if not host:
        return (
            None,
            "the database host is not set (a Supabase pooler host or the project's direct host db.PROJECT_REF.supabase.co)",
        )
    direct = _DIRECT_HOST_RE.fullmatch(host)
    if direct:
        if project_ref and direct.group(1) != project_ref:
            return None, f"the database host belongs to another project than {project_ref}"
    elif not _POOLER_HOST_RE.fullmatch(host):
        return (
            None,
            "the database host must be a Supabase pooler (*.pooler.supabase.com) or the project's direct host db.PROJECT_REF.supabase.co",
        )
    elif not _PROJECT_REF_RE.fullmatch(project_ref or ""):
        return None, "a pooler connection needs the integration's Project ref (its user is role.project_ref)"
    port_text = str(document.get("port") or "").strip() or str(DEFAULT_PORT)
    if not port_text.isdigit() or int(port_text) not in _PORTS:
        return None, "the database port must be 5432 (session pooler or direct) or 6543 (transaction pooler)"
    database = str(document.get("database") or "").strip() or DEFAULT_DATABASE
    if not _DATABASE_RE.fullmatch(database):
        return None, "the database name must be at most 63 letters, digits, '_' or '-'"
    ca_cert = str(document.get("ca_cert") or "").strip()
    if ca_cert and "-----BEGIN CERTIFICATE-----" not in ca_cert:
        return None, "the CA certificate must be PEM (-----BEGIN CERTIFICATE-----)"
    functions = split_functions(document.get("functions"))
    if len(functions) > MAX_FUNCTIONS:
        return None, f"at most {MAX_FUNCTIONS} functions may be allowed"
    invalid = [name for name in functions if not _FUNCTION_RE.fullmatch(name)]
    if invalid:
        return None, "every allowed function must be written schema.name (e.g. public.get_document)"
    return (
        DatabaseAccess(
            role=role,
            password=password,
            host=host,
            port=int(port_text),
            database=database,
            ca_cert=ca_cert,
            functions=tuple(functions),
        ),
        None,
    )


def login_user(access: DatabaseAccess, project_ref: str) -> str:
    """The user name the host expects: ``role.project_ref`` on a pooler, the role otherwise."""
    if _POOLER_HOST_RE.fullmatch(access.host):
        return f"{access.role}.{project_ref}"
    return access.role


def is_platform_role(name: str) -> bool:
    return bool(_PLATFORM_ROLE_RE.search(name or ""))


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class _HostnameContext(ssl.SSLContext):
    """A client TLS context that verifies one host name whatever address is dialled.

    The connection goes to the address resolved (and vetted) once; asyncpg
    would otherwise check the certificate against that address.
    """

    verified_hostname: str = ""

    def wrap_bio(self, incoming, outgoing, server_side=False, server_hostname=None, session=None):  # type: ignore[override]
        return super().wrap_bio(
            incoming,
            outgoing,
            server_side=server_side,
            server_hostname=self.verified_hostname,
            session=session,
        )


def _tls_context(access: DatabaseAccess) -> ssl.SSLContext:
    context = _HostnameContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verified_hostname = access.host
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    if access.ca_cert:
        try:
            context.load_verify_locations(cadata=access.ca_cert)
        except (ssl.SSLError, ValueError) as exc:
            raise DatabaseAccessError(
                "Error: the integration's database CA certificate cannot be read - paste the "
                "PEM certificate of Project Settings > Database > SSL Configuration."
            ) from exc
    else:
        context.load_default_certs()
    return context


async def _resolve(host: str, port: int) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """The one address the connection dials; refused if any address of *host* is not public."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP),
            timeout=CONNECT_TIMEOUT,
        )
    except (OSError, TimeoutError) as exc:
        raise DatabaseAccessError(f"Error: the database host {host} cannot be resolved.") from exc
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addresses:
        raise DatabaseAccessError(f"Error: the database host {host} cannot be resolved.")
    for address in addresses:
        if reason := forbidden_address_reason(address):
            logger.warning("Supabase database host %s resolves to a refused address (%s)", host, reason)
            raise DatabaseAccessError(f"Error: the database host {host} resolves to an address the platform refuses.")
    # IPv4 first: a direct db.<ref> host is IPv6 only, a pooler answers both.
    addresses.sort(key=lambda address: address.version)
    return addresses[0]


def _connect_error(exc: BaseException, access: DatabaseAccess) -> DatabaseAccessError:
    """A connection failure in fixed words; the server's own words stay in the log."""
    logger.info("Supabase database login as %s on %s failed: %r", access.role, access.host, str(exc)[:300])
    if isinstance(exc, asyncpg.InvalidPasswordError):
        return DatabaseAccessError(f"Error: the database refused the login of role {access.role} - check its password.")
    if isinstance(exc, asyncpg.InvalidCatalogNameError):
        return DatabaseAccessError(f"Error: the database {access.database} does not exist on this project.")
    if isinstance(exc, asyncpg.InvalidAuthorizationSpecificationError):
        return DatabaseAccessError(
            f"Error: the database refused role {access.role} - it may not exist, not be a LOGIN role, "
            f"or not be allowed to connect to {access.database}."
        )
    if isinstance(exc, ssl.SSLCertVerificationError):
        return DatabaseAccessError(
            f"Error: the TLS certificate of {access.host} could not be verified - paste the project's CA "
            "certificate (Project Settings > Database > SSL Configuration) in the integration."
        )
    if isinstance(exc, TimeoutError):
        return DatabaseAccessError(f"Error: the database host {access.host} did not answer in time.")
    if isinstance(exc, OSError | ConnectionError):
        return DatabaseAccessError(
            f"Error: the database host {access.host}:{access.port} cannot be reached from the platform "
            "(the outbound Postgres port may be closed)."
        )
    return DatabaseAccessError(
        f"Error: the login of role {access.role} on {access.host} failed (a pooler answers this way when the "
        "role or the project is unknown to it)."
    )


async def connect(access: DatabaseAccess, project_ref: str, *, database: str | None = None) -> asyncpg.Connection:
    """A connection logged in as the role; ``DatabaseAccessError`` when refused."""
    if current_route() is not None:
        raise DatabaseAccessError(
            "Error: this integration goes through a relay group, which carries HTTP only - its "
            "database role cannot be reached without leaving the platform directly."
        )
    address = await _resolve(access.host, access.port)
    context = _tls_context(access)
    try:
        return await asyncpg.connect(
            # asyncpg cuts a bare host at its first ':': an IPv6 address is bracketed.
            host=f"[{address}]" if address.version == 6 else str(address),
            port=access.port,
            user=login_user(access, project_ref),
            password=access.password,
            database=database or access.database,
            ssl=context,
            timeout=CONNECT_TIMEOUT,
            command_timeout=CALL_TIMEOUT,
            # The transaction pooler keeps no named prepared statement.
            statement_cache_size=0,
            server_settings={"application_name": "xtm-one"},
        )
    except DatabaseAccessError:
        raise
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError, ssl.SSLError) as exc:
        raise _connect_error(exc, access) from exc


async def _close(connection: asyncpg.Connection) -> None:
    try:
        await connection.close(timeout=5)
    except Exception:
        # A close that fails must not hide the result.
        connection.terminate()


# Whether the role ``r`` escapes row level security: Postgres skips it for a
# superuser, a BYPASSRLS role, and on a table for its owner and every role
# inheriting the owner's privileges, unless the table forces RLS. On Supabase
# the public tables belong to postgres, so ``grant postgres to agent_reader``
# lifts RLS from every function the role calls.
BYPASSES_RLS = """(r.rolsuper or r.rolbypassrls or exists (
  select 1 from pg_catalog.pg_class c
  where c.relkind in ('r', 'p') and c.relrowsecurity and not c.relforcerowsecurity
    and pg_catalog.pg_has_role(r.oid, c.relowner, 'USAGE')))"""

_BYPASSES_RLS_SQL = f"""\
select {BYPASSES_RLS} as bypasses
from pg_catalog.pg_roles r
where r.rolname = current_user"""  # noqa: S608 - constant SQL


async def session[T](
    access: DatabaseAccess,
    project_ref: str,
    work: Callable[[asyncpg.Connection], Awaitable[T]],
    *,
    database: str | None = None,
    allow_rls_bypass: bool = False,
) -> T:
    """Run *work* on a connection logged in as the role, inside one transaction.

    The transaction carries a statement timeout and the whole call a deadline.
    A role that bypasses row level security (``BYPASSES_RLS``, possibly granted
    after it was chosen) is refused before *work* runs, unless
    *allow_rls_bypass* (the probe and the connection test, which report it).
    A database refusal comes back as ``DatabaseAccessError`` with the
    database's message in ``untrusted``.
    """
    try:
        async with asyncio.timeout(CALL_TIMEOUT):
            connection = await connect(access, project_ref, database=database)
            try:
                async with connection.transaction():
                    await connection.execute(f"set local statement_timeout = '{STATEMENT_TIMEOUT}'")
                    if not allow_rls_bypass and await connection.fetchval(_BYPASSES_RLS_SQL):
                        raise DatabaseAccessError(
                            f"Error: the database role {access.role} bypasses row level security (superuser, "
                            "BYPASSRLS, or owner of a table whose RLS is not forced), so its calls are refused: "
                            "give the integration a role under RLS."
                        )
                    return await work(connection)
            finally:
                await _close(connection)
    except TimeoutError as exc:
        raise DatabaseAccessError(
            f"Error: the database did not answer within {CALL_TIMEOUT} seconds (statement timeout {STATEMENT_TIMEOUT})."
        ) from exc
    except asyncpg.PostgresError as exc:
        # The condition (asyncpg's class for the SQLSTATE) is a fixed name the
        # admin can act on; the database's own words stay quarantined.
        condition = type(exc).__name__ + (f", SQLSTATE {exc.sqlstate}" if exc.sqlstate else "")
        raise DatabaseAccessError(
            f"Error: the database refused the call ({condition}).",
            untrusted=f"{type(exc).__name__}: {exc}"[:1000],
        ) from exc
    except (asyncpg.InterfaceError, OSError) as exc:
        logger.info("Supabase database call as %s failed: %r", access.role, str(exc)[:300])
        raise DatabaseAccessError("Error: the database connection broke during the call.") from exc


# ---------------------------------------------------------------------------
# Catalog SQL (run as the role)
# ---------------------------------------------------------------------------

# The arguments are zipped with ``rows from``, one qualified ``unnest`` per
# array: the multi-array ``unnest(a, b, c)`` is parser syntax an unqualified
# name only gets - ``pg_catalog.unnest`` of three arrays does not exist.
_FUNCTION_COLUMNS = """\
select n.nspname as schema,
       p.proname as name,
       p.oid::bigint as oid,
       pg_catalog.pg_get_function_identity_arguments(p.oid) as signature,
       pg_catalog.pg_get_function_result(p.oid) as returns,
       p.proretset as returns_set,
       p.prorettype = 'pg_catalog.void'::pg_catalog.regtype as returns_void,
       (p.prorettype = 'pg_catalog.record'::pg_catalog.regtype
          and not coalesce('o' = any(p.proargmodes) or 't' = any(p.proargmodes), false)) as untyped_record,
       p.prosecdef as security_definer,
       p.provolatile::text as volatility,
       p.pronargdefaults as defaults,
       pg_catalog.has_function_privilege(p.oid, 'EXECUTE') as executable,
       pg_catalog.obj_description(p.oid, 'pg_proc') as comment,
       coalesce((
         select pg_catalog.json_agg(pg_catalog.json_build_object(
                  'name', a.name, 'type', pg_catalog.format_type(a.typ, null), 'mode', coalesce(a.mode::text, 'i'))
                order by a.ord)
         from rows from (pg_catalog.unnest(coalesce(p.proallargtypes, p.proargtypes::pg_catalog.oid[])),
                         pg_catalog.unnest(p.proargmodes),
                         pg_catalog.unnest(p.proargnames)) with ordinality as a(typ, mode, name, ord)
       ), '[]'::pg_catalog.json) as args
from pg_catalog.pg_proc p
join pg_catalog.pg_namespace n on n.oid = p.pronamespace
where p.prokind = 'f'"""

# The allowed functions, looked up by ``schema.name``.
ALLOWED_FUNCTIONS_SQL = (
    _FUNCTION_COLUMNS + "\n  and (n.nspname || '.' || p.proname) = any($1::pg_catalog.text[])\norder by 1, 2, 4"
)

# Every function of the project's own schemas the role may execute (the probe).
EXECUTABLE_FUNCTIONS_SQL = (
    _FUNCTION_COLUMNS  # noqa: S608 - constant SQL, the schemas travel bound
    + """
  and n.nspname <> all($1::pg_catalog.text[])
  and n.nspname !~ '^pg_'
  and pg_catalog.has_function_privilege(p.oid, 'EXECUTE')
  and not exists (
    select 1 from pg_catalog.pg_depend d
    where d.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass and d.objid = p.oid and d.deptype = 'e'
  )
order by 1, 2, 4
limit 500"""
)

# What the role can read directly: the tables and views of the project's own
# schemas it holds SELECT on, on the whole table or on one of its columns. A
# role meant to read through functions only should have none.
READABLE_RELATIONS_SQL = """\
select n.nspname as schema, c.relname as name, c.relkind::text as kind,
       c.relrowsecurity as rls_enabled
from pg_catalog.pg_class c
join pg_catalog.pg_namespace n on n.oid = c.relnamespace
where c.relkind in ('r', 'p', 'v', 'm', 'f')
  and n.nspname <> all($1::pg_catalog.text[])
  and n.nspname !~ '^pg_'
  and pg_catalog.has_any_column_privilege(c.oid, 'SELECT')
order by 1, 2
limit 500"""

ROLE_ATTRIBUTES_SQL = f"""\
select r.rolname as name, r.rolsuper as superuser, {BYPASSES_RLS} as bypass_rls,
       r.rolcreaterole as create_role, pg_catalog.current_database() as database
from pg_catalog.pg_roles r
where r.rolname = current_user"""  # noqa: S608 - constant SQL

DATABASES_SQL = """\
select d.datname as name
from pg_catalog.pg_database d
where d.datallowconn and not d.datistemplate
  and pg_catalog.has_database_privilege(d.datname, 'CONNECT')
order by 1
limit 100"""


def function_rows(records: Sequence[asyncpg.Record]) -> list[dict[str, Any]]:
    """Catalog records as plain dicts, ``args`` parsed."""
    rows = []
    for record in records:
        row = dict(record)
        args = row.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = []
        row["args"] = [arg for arg in args or [] if isinstance(arg, dict)]
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Building a call
# ---------------------------------------------------------------------------


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def input_arguments(function: dict[str, Any]) -> list[dict[str, Any]]:
    """The input parameters, in order, each with ``has_default``."""
    # No mode is an input: the catalog leaves proargmodes null when every argument is one.
    inputs = [arg for arg in function.get("args") or [] if (arg.get("mode") or "i") in ("i", "b", "v")]
    defaults = int(function.get("defaults") or 0)
    for index, arg in enumerate(inputs):
        arg["has_default"] = index >= len(inputs) - defaults
    return inputs


def _argument_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def build_call(
    function: dict[str, Any], arguments: dict[str, Any], *, first_parameter: int = 3
) -> tuple[str, list[Any], str | None]:
    """The call expression of *function* with *arguments*, its parameters, or why it cannot be called.

    The parameters start at ``$first_parameter`` (``$1`` and ``$2`` are the
    offset and the output budget of ``result_sql``). Every argument is bound as text and
    cast to the declared type, which ``format_type`` spelt; a list given for
    an array parameter is expanded from JSON.
    """
    inputs = input_arguments(function)
    if any(arg.get("mode") == "v" for arg in inputs):
        return "", [], "functions with a VARIADIC parameter cannot be called through this tool"
    named = all(arg.get("name") for arg in inputs)
    by_position = {str(index + 1): arg for index, arg in enumerate(inputs)}
    known = {arg["name"]: arg for arg in inputs if arg.get("name")} if named else by_position
    unknown = [key for key in arguments if key not in known]
    if unknown:
        expected = ", ".join(known) or "none"
        return "", [], f"unknown argument(s) {', '.join(sorted(unknown))[:200]} - the function takes: {expected}"
    missing = [key for key, arg in known.items() if not arg["has_default"] and key not in arguments]
    if missing:
        return "", [], f"missing required argument(s): {', '.join(missing)}"
    if not named:
        given = [int(key) for key in arguments]
        if given and sorted(given) != list(range(1, max(given) + 1)):
            return "", [], 'positional arguments must be given without a gap, from "1" on'
    parts: list[str] = []
    parameters: list[Any] = []
    for key, arg in known.items():
        if key not in arguments:
            continue
        value = arguments[key]
        text = _argument_text(value)
        if text is not None and len(text) > MAX_ARGUMENT_CHARS:
            return "", [], f"argument {key} is over {MAX_ARGUMENT_CHARS:,} characters"
        parameters.append(text)
        placeholder = f"${len(parameters) + first_parameter - 1}"
        declared = str(arg.get("type") or "text")
        if isinstance(value, list) and declared.endswith("[]"):
            expression = (
                f"array(select pg_catalog.jsonb_array_elements_text({placeholder}::pg_catalog.jsonb))::{declared}"
            )
        else:
            expression = f"{placeholder}::pg_catalog.text::{declared}"
        parts.append(f"{quote_identifier(key)} => {expression}" if named else expression)
    call = f"{quote_identifier(function['schema'])}.{quote_identifier(function['name'])}({', '.join(parts)})"
    return call, parameters, None


def result_sql(call: str, max_rows: int) -> str:
    """The query returning each row's text sliced to ``$2`` characters from ``$1``.

    A scalar text result comes back as the text itself, any other row as its
    JSON. Rows stop once the characters sent reach the budget: what crosses
    the wire is at most the budget plus one slice.
    """
    # Only the quoted call and the clamped integer reach the text of the query.
    return f"""\
select w.j, w.n, w.total from (
  select pg_catalog.substr(t.j, $1 + 1, $2) as j,
         pg_catalog.length(t.j) as n,
         t.ord,
         pg_catalog.count(*) over () as total,
         pg_catalog.sum(least(greatest(pg_catalog.length(t.j) - $1, 0), $2))
           over (order by t.ord rows between unbounded preceding and 1 preceding) as sent
  from (
    select case when pg_catalog.jsonb_typeof(s.v) = 'string' then s.v #>> '{{}}' else s.v::pg_catalog.text end as j,
           s.ord
    from (
      select pg_catalog.to_jsonb(r) as v, pg_catalog.row_number() over () as ord
      from {call} as r
      limit {int(max_rows) + 1}
    ) s
  ) t
) w
where coalesce(w.sent, 0) < $2
order by w.ord"""  # noqa: S608
