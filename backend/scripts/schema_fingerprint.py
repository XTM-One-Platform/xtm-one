#!/usr/bin/env python3
"""Deep PostgreSQL schema fingerprint + diff — the migration-squash safety net.

This tool inspects a live database straight from ``pg_catalog`` /
``information_schema`` and emits a normalised, order-independent fingerprint of
its schema: columns (type, nullability, default), primary keys, foreign keys,
unique + check constraints, full index definitions, enums, sequences, triggers,
and functions.

It is deliberately **stricter than Alembic autogenerate**, which silently skips
operator-class indexes (HNSW / trigram) and is lax about server defaults and
check constraints.  Two schemas that ``--autogenerate`` calls "equal" can still
differ here — which is exactly the drift a migration squash must not introduce.

The tool is **application-agnostic**: it imports nothing from ``app`` and only
needs ``asyncpg``.  That means the SAME binary can fingerprint a DB built by the
old migration chain and a DB built by the new baseline, in separate processes,
with no risk of Python module clashes.

Connection parameters come from the environment (defaults match the dev
docker-compose setup):

    PGHOST      (default: localhost)
    PGPORT      (default: 5432)
    PGUSER      (default: copilot)
    PGPASSWORD  (default: copilot_dev)
    PGADMINDB   (default: copilot)   # maintenance DB for CREATE/DROP DATABASE

Sub-commands
------------
    createdb   NAME            drop-if-exists, create, enable ``vector`` extension
    dropdb     NAME            drop database if it exists
    set-version NAME REV       overwrite the single ``alembic_version`` row
    count-tables NAME          print number of public tables (excl. alembic_version)
    row-counts NAME [OUT]      write table row counts to OUT (or stdout); useful to
                               prove a recovery did not drop/recreate data
    fingerprint NAME [OUT]     write the JSON fingerprint to OUT (or stdout)
    diff FILE_A FILE_B         structured diff of two fingerprint files;
                               exit 0 if identical, 1 if they differ

Every destructive sub-command refuses to touch a database whose name does not
start with the throwaway prefix ``sqsh_verify_`` unless ``ALLOW_UNSAFE_DB=1``
is set, so the tool can never drop your dev or production database by mistake.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import asyncpg

SAFE_PREFIX = "sqsh_verify_"


def _conn_kwargs(database: str) -> dict:
    return {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": int(os.environ.get("PGPORT", "5432")),
        "user": os.environ.get("PGUSER", "copilot"),
        "password": os.environ.get("PGPASSWORD", "copilot_dev"),
        "database": database,
    }


def _admin_db() -> str:
    return os.environ.get("PGADMINDB", "copilot")


def _assert_safe(name: str) -> None:
    if name.startswith(SAFE_PREFIX) or os.environ.get("ALLOW_UNSAFE_DB") == "1":
        return
    sys.exit(
        f"refusing to modify database {name!r}: name must start with "
        f"{SAFE_PREFIX!r} (or set ALLOW_UNSAFE_DB=1 to override)"
    )


# --------------------------------------------------------------------------- #
# Introspection queries
# --------------------------------------------------------------------------- #
Q_COLUMNS = """
select c.table_name, c.column_name, c.ordinal_position,
       format_type(a.atttypid, a.atttypmod) as data_type,
       not a.attnotnull as is_nullable,
       pg_get_expr(ad.adbin, ad.adrelid) as column_default
from information_schema.columns c
join pg_attribute a
  on a.attname = c.column_name
 and a.attrelid = (quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))::regclass
left join pg_attrdef ad on ad.adrelid = a.attrelid and ad.adnum = a.attnum
where c.table_schema = 'public'
  and c.table_name <> 'alembic_version'
  and a.attnum > 0 and not a.attisdropped
order by c.table_name, c.ordinal_position
"""

Q_CONSTRAINTS = """
select conrelid::regclass::text as table_name, contype,
       pg_get_constraintdef(oid) as def
from pg_constraint
where connamespace = 'public'::regnamespace
  and conrelid::regclass::text <> 'alembic_version'
order by table_name, contype
"""

Q_INDEXES = """
select tablename, indexname, indexdef
from pg_indexes
where schemaname = 'public' and tablename <> 'alembic_version'
order by tablename, indexname
"""

Q_ENUMS = """
select t.typname, array_agg(e.enumlabel order by e.enumsortorder) as labels
from pg_type t
join pg_enum e on e.enumtypid = t.oid
join pg_namespace n on n.oid = t.typnamespace and n.nspname = 'public'
group by t.typname
order by t.typname
"""

Q_SEQUENCES = """
select sequence_name, data_type
from information_schema.sequences
where sequence_schema = 'public'
order by sequence_name
"""

# Triggers and their functions are the ONE artifact category that
# ``Base.metadata.create_all`` does NOT manage (they live in migration bodies /
# runtime ``_ensure_schema_extras``), so a squash that dropped them would be
# invisible to a column/constraint/index diff.  Capture both so the fingerprint
# actually validates them.  User (non-system) triggers only — ``tgisinternal``
# filters out the ones PostgreSQL creates for FK/constraint enforcement (those
# are already covered by the constraint section).
Q_TRIGGERS = """
select c.relname as table_name, t.tgname as trigger_name,
       pg_get_triggerdef(t.oid) as def
from pg_trigger t
join pg_class c on c.oid = t.tgrelid
join pg_namespace n on n.oid = c.relnamespace and n.nspname = 'public'
where not t.tgisinternal
order by table_name, trigger_name
"""

# Functions/procedures defined in the public schema that belong to the
# APPLICATION (e.g. the ``knowledge_documents`` updated_at trigger function).
#
# Two filters keep this focused and correct:
#   * Extension-owned functions are EXCLUDED (``pg_depend`` deptype ``'e'``).
#     ``CREATE EXTENSION vector`` alone installs ~90 functions that are
#     identical on every DB with the same extension version — pure noise for a
#     migration diff, and many are overloaded (see the key note below).
#   * The key includes the argument signature
#     (``pg_get_function_identity_arguments``) so overloaded functions do NOT
#     collapse onto one dict key and produce false diffs.
Q_FUNCTIONS = """
select p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')'
           as function_key,
       pg_get_functiondef(p.oid) as def
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace and n.nspname = 'public'
where p.prokind in ('f', 'p')
  and not exists (
    select 1 from pg_depend d
    where d.objid = p.oid and d.deptype = 'e'
  )
order by function_key
"""

Q_USER_TABLES = """
select tablename
from pg_tables
where schemaname = 'public' and tablename <> 'alembic_version'
order by tablename
"""


async def _fingerprint(database: str) -> dict:
    conn = await asyncpg.connect(**_conn_kwargs(database))
    try:
        cols: dict[str, list[str]] = {}
        for r in await conn.fetch(Q_COLUMNS):
            cols.setdefault(r["table_name"], []).append(
                f"{r['column_name']} :: {r['data_type']} | "
                f"null={r['is_nullable']} | default={r['column_default']}"
            )

        cons: dict[str, list[str]] = {}
        for r in await conn.fetch(Q_CONSTRAINTS):
            # Key on definition, not the (often auto-generated) constraint name.
            cons.setdefault(r["table_name"], []).append(f"[{r['contype']}] {r['def']}")
        for t in cons:
            cons[t] = sorted(cons[t])

        idx: dict[str, list[str]] = {}
        for r in await conn.fetch(Q_INDEXES):
            # Strip the concrete index name so name churn is not a false diff;
            # the column set + predicate + operator class still fully compared.
            normalized = r["indexdef"].replace(f" INDEX {r['indexname']} ", " INDEX <name> ")
            idx.setdefault(r["tablename"], []).append(normalized)
        for t in idx:
            idx[t] = sorted(idx[t])

        enums = {r["typname"]: list(r["labels"]) for r in await conn.fetch(Q_ENUMS)}
        seqs = {r["sequence_name"]: r["data_type"] for r in await conn.fetch(Q_SEQUENCES)}

        trigs: dict[str, list[str]] = {}
        for r in await conn.fetch(Q_TRIGGERS):
            # Strip the concrete trigger name so name churn is not a false diff;
            # timing/event/table/function in the def are still fully compared.
            normalized = r["def"].replace(
                f"TRIGGER {r['trigger_name']} ", "TRIGGER <name> "
            )
            trigs.setdefault(r["table_name"], []).append(normalized)
        for t in trigs:
            trigs[t] = sorted(trigs[t])

        funcs = {r["function_key"]: r["def"] for r in await conn.fetch(Q_FUNCTIONS)}
    finally:
        await conn.close()

    return {
        "columns": cols,
        "constraints": cons,
        "indexes": idx,
        "enums": enums,
        "sequences": seqs,
        "triggers": trigs,
        "functions": funcs,
    }


# --------------------------------------------------------------------------- #
# Admin helpers
# --------------------------------------------------------------------------- #
async def _createdb(name: str) -> None:
    _assert_safe(name)
    conn = await asyncpg.connect(**_conn_kwargs(_admin_db()))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    conn = await asyncpg.connect(**_conn_kwargs(name))
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await conn.close()


async def _dropdb(name: str) -> None:
    _assert_safe(name)
    conn = await asyncpg.connect(**_conn_kwargs(_admin_db()))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


async def _set_version(name: str, rev: str) -> None:
    _assert_safe(name)
    conn = await asyncpg.connect(**_conn_kwargs(name))
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num varchar(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
        await conn.execute("DELETE FROM alembic_version")
        await conn.execute("INSERT INTO alembic_version (version_num) VALUES ($1)", rev)
    finally:
        await conn.close()


async def _count_tables(name: str) -> int:
    conn = await asyncpg.connect(**_conn_kwargs(name))
    try:
        return await conn.fetchval(
            "select count(*) from pg_tables "
            "where schemaname='public' and tablename<>'alembic_version'"
        )
    finally:
        await conn.close()


async def _row_counts(database: str) -> dict[str, int]:
    """Return exact row counts per public table, excluding alembic_version."""
    conn = await asyncpg.connect(**_conn_kwargs(database))
    try:
        counts: dict[str, int] = {}
        for r in await conn.fetch(Q_USER_TABLES):
            table = r["tablename"]
            # An identifier cannot be a bind parameter; the name comes from
            # pg_catalog (Q_USER_TABLES), not user input, and is quote-escaped.
            quoted = table.replace('"', '""')
            counts[table] = await conn.fetchval(
                f'SELECT count(*) FROM "{quoted}"'  # noqa: S608
            )
        return counts
    finally:
        await conn.close()


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #
def _diff(file_a: str, file_b: str) -> int:
    with open(file_a, encoding="utf-8") as fh:
        a = json.load(fh)
    with open(file_b, encoding="utf-8") as fh:
        b = json.load(fh)
    label_a = os.path.basename(file_a)
    label_b = os.path.basename(file_b)
    total = 0
    for section in ("columns", "constraints", "indexes", "enums", "sequences", "triggers", "functions"):
        sa, sb = a.get(section, {}), b.get(section, {})
        lines: list[str] = []
        for key in sorted(set(sa) | set(sb)):
            va, vb = sa.get(key), sb.get(key)
            if va == vb:
                continue
            if key not in sa:
                lines.append(f"  [{section}] {key}: present only in {label_b}")
                continue
            if key not in sb:
                lines.append(f"  [{section}] {key}: present only in {label_a}")
                continue
            if isinstance(va, list):
                for x in sorted(set(vb) - set(va)):
                    lines.append(f"  [{section}:{key}] only in {label_b}:  {x}")
                for x in sorted(set(va) - set(vb)):
                    lines.append(f"  [{section}:{key}] only in {label_a}:  {x}")
            else:
                lines.append(f"  [{section}:{key}] {label_a}={va!r}  {label_b}={vb!r}")
        marker = "OK" if not lines else f"{len(lines)} DIFF"
        print(f"  {section:<12} {marker}")
        for ln in lines:
            print(ln)
        total += len(lines)
    print(f"  ---> total differences: {total}")
    return 0 if total == 0 else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]

    if cmd == "createdb":
        asyncio.run(_createdb(rest[0]))
    elif cmd == "dropdb":
        asyncio.run(_dropdb(rest[0]))
    elif cmd == "set-version":
        asyncio.run(_set_version(rest[0], rest[1]))
    elif cmd == "count-tables":
        print(asyncio.run(_count_tables(rest[0])))
    elif cmd == "row-counts":
        payload = json.dumps(asyncio.run(_row_counts(rest[0])), indent=2, sort_keys=True)
        if len(rest) > 1:
            with open(rest[1], "w") as fh:
                fh.write(payload)
        else:
            print(payload)
    elif cmd == "fingerprint":
        fp = asyncio.run(_fingerprint(rest[0]))
        payload = json.dumps(fp, indent=2, sort_keys=True, default=str)
        if len(rest) > 1:
            with open(rest[1], "w") as fh:
                fh.write(payload)
        else:
            print(payload)
    elif cmd == "diff":
        return _diff(rest[0], rest[1])
    else:
        print(f"unknown command: {cmd}")
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
