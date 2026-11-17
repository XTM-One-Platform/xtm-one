# SPDX-License-Identifier: Apache-2.0

"""Idempotent index reconciliation for the fresh-vs-existing convergence gap.

Why this exists
---------------
On a **fresh** database the app does ``Base.metadata.create_all`` +
``alembic stamp head`` and never executes any migration body
(see ``app/core/migrations.py``).  On an **existing** database the app runs
``alembic upgrade head``.  For years this meant any index created *only* in a
migration body — and not also declared on the SQLAlchemy model — was silently
absent on every fresh install (and on every install that was bootstrapped via
``stamp head`` and later upgraded).

The durable fix is two-fold:

1. **Every index is declared on its model** so ``create_all`` builds it on a
   fresh DB.
2. **This reconciler runs on every boot** and creates any model-declared index
   that is missing from the live database.  That self-heals platforms that were
   bootstrapped before the index was added to the model (they skipped the
   migration body) and makes the whole class of bug impossible to reintroduce:
   if it's on the model, it ends up in the DB.

Safety properties
------------------
* Every create is a plain, in-place ``CREATE INDEX IF NOT EXISTS`` — a cheap
  no-op when the index already exists.  We deliberately do **not** use
  ``CREATE INDEX CONCURRENTLY``: a concurrent build (and a concurrent drop) must
  wait for *every* overlapping transaction to drain before it can finish,
  regardless of which table that transaction touches.  During a rolling deploy
  the previous pod keeps serving traffic from a connection pool that holds
  long-lived / ``idle in transaction`` sessions, so a concurrent build hangs
  indefinitely (``_ensure_model_indexes`` runs it with ``statement_timeout = 0``,
  so it never gives up).  A plain build only takes a ``SHARE`` lock on its own
  table — compatible with concurrent ``SELECT``s — so it waits only for in-flight
  writes, builds in place, and completes.  This is the same in-place behaviour
  that lets an ordinary ``ALTER TABLE`` migration succeed while another pod is
  still live.
* Creation is **column-set guarded**: a model index is skipped when an existing
  index already covers the same ``(table, ordered columns)`` under a *different*
  name.  This avoids creating a duplicate of a legacy-named index that survived
  a table rename — e.g. the pre-rename ``ix_workflow_runs_status`` that lives on
  platforms which upgraded through the ``workflows`` → ``assignments`` rename
  still covers ``assignment_runs (status)``, so we do not also create
  ``ix_assignment_runs_status`` next to it.
* A leftover ``INVALID`` index (from a previously interrupted concurrent build —
  e.g. an earlier deploy that still used ``CREATE INDEX CONCURRENTLY``) is
  dropped before re-creating, because ``CREATE INDEX ... IF NOT EXISTS`` checks
  existence, not validity, and would otherwise silently skip forever.

The functions operate on a **synchronous** SQLAlchemy ``Connection``.  The async
boot path runs them on an ``AUTOCOMMIT`` connection (via
``AsyncConnection.run_sync``) so each ``CREATE INDEX`` commits — and releases its
``SHARE`` lock — immediately, instead of holding every lock until the end of one
big transaction.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from sqlalchemy import Column, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateIndex, Index

logger = logging.getLogger(__name__)

PG_DIALECT = postgresql.dialect()
_CREATE_INDEX_RE = re.compile(r"^CREATE (UNIQUE )?INDEX ", re.IGNORECASE)

# Index access methods whose builds are too slow for the startup path.  An
# HNSW build on a populated ``embeddings`` table takes minutes; running it
# inside the lifespan (before uvicorn accepts connections) starves the
# Kubernetes liveness probe and crash-loops the container.
# These indexes are excluded from the boot-time reconciler below and built
# by the post-startup background task in ``app/core/ann_index_builder.py``
# instead.  Fresh installs still get them instantly via ``create_all``
# (empty table).
DEFERRED_INDEX_ACCESS_METHODS = frozenset({"hnsw"})


def is_deferred_index(index: Index) -> bool:
    """``True`` when *index* must be built post-startup, not at boot."""
    using = _pg_option(index, "using")
    return bool(using) and str(using).lower() in DEFERRED_INDEX_ACCESS_METHODS


def deferred_model_indexes() -> list[Index]:
    """Every model-declared index whose build is deferred to post-startup."""
    from app.core.database import Base

    return [
        index
        for table in Base.metadata.sorted_tables
        for index in table.indexes
        if is_deferred_index(index)
    ]


def is_duplicate_index_error(exc: BaseException) -> bool:
    """``True`` when *exc* means the index was created concurrently elsewhere.

    Covers the two duplicate signals a ``CREATE INDEX IF NOT EXISTS`` can
    still raise when racing another in-flight build (e.g. an orphaned
    backend from a killed pod, or a parallel uvicorn worker / replica):

    * ``42P07`` duplicate_table — the index appeared between the existence
      check and the create.
    * ``23505`` unique_violation **on pg_class** — both builds passed the
      existence check before either committed; the second catalog insert
      fails on ``pg_class_relname_nsp_index``.

    A 23505 that does NOT mention ``pg_class`` is a genuine data error
    (e.g. ``CREATE UNIQUE INDEX`` over rows with duplicate values) and must
    propagate — swallowing it would silently leave the table unindexed.

    Walks the ``__cause__`` / ``__context__`` exception chain for a
    PostgreSQL SQLSTATE because asyncpg exposes it as ``sqlstate``/``pgcode``
    and SQLAlchemy wraps the driver error; each exception's ``.orig`` DBAPI
    error is inspected for codes but never descended into (driver error
    objects can expose arbitrary attribute chains).  Falls back to a string
    probe so detection stays robust across driver differences.
    """
    text_blob = str(exc).lower()

    candidates: list[object] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        candidates.append(cur)
        orig = getattr(cur, "orig", None)
        if orig is not None:
            candidates.append(orig)
        cur = cur.__cause__ or cur.__context__

    for obj in candidates:
        for attr in ("sqlstate", "pgcode"):
            code = getattr(obj, attr, None)
            if code == "42P07":
                return True
            if code == "23505" and (
                "pg_class" in str(obj).lower() or "pg_class" in text_blob
            ):
                return True
    return "pg_class_relname_nsp_index" in text_blob or "42p07" in text_blob


def idempotent_if_not_exists(ddl: str) -> str:
    """Turn a compiled ``CREATE [UNIQUE] INDEX`` into the idempotent form.

    A plain (in-place) ``CREATE INDEX IF NOT EXISTS`` — NOT
    ``CREATE INDEX CONCURRENTLY``.  A concurrent build must wait for every
    overlapping transaction to drain, so during a rolling deploy it hangs
    indefinitely behind the previous pod's long-lived / ``idle in transaction``
    pool sessions (with ``statement_timeout = 0`` it never gives up).  A plain
    build only takes a ``SHARE`` lock on its own table — compatible with
    concurrent reads — so it waits only for in-flight writes and completes.
    """
    return _CREATE_INDEX_RE.sub(
        lambda m: f"CREATE {m.group(1) or ''}INDEX IF NOT EXISTS ",
        ddl,
        count=1,
    )


def _pg_option(index: Index, key: str):
    try:
        return index.dialect_options["postgresql"][key]
    except Exception:  # pragma: no cover - defensive
        return None


def _plain_column_signature(
    index: Index,
) -> tuple[str, tuple[str, ...], bool] | None:
    """Return ``(table, (col, ...), is_unique)`` for a plain column btree index.

    Returns ``None`` for partial / expression / non-btree (e.g. GIN) indexes —
    those are matched by name only, never by column set, because their canonical
    names are stable across platforms (they were created with current table
    names and never renamed).

    The uniqueness flag is part of the signature so a UNIQUE model index is
    NOT treated as already-covered by a non-unique index over the same columns
    (which would silently skip building the uniqueness constraint), and vice
    versa.
    """
    if index.table is None:
        return None
    # Partial index — match by name only. Compare against ``None`` explicitly:
    # ``postgresql_where`` is a SQL clause whose ``__bool__`` raises.
    if _pg_option(index, "where") is not None:
        return None
    using = _pg_option(index, "using")
    # ``postgresql_using`` is ``False`` when unset, ``"gin"`` / ``"gist"`` /
    # etc. for non-btree access methods. Treat unset (falsy) and explicit
    # ``"btree"`` as plain column indexes; anything else is special.
    if using and using != "btree":
        return None
    cols: list[str] = []
    for expr in index.expressions:
        if isinstance(expr, Column):
            cols.append(expr.name)
        else:
            # A text()/functional expression — not a plain column index.
            return None
    if not cols:
        return None
    return (index.table.name, tuple(cols), bool(index.unique))


def existing_index_names(conn: Connection) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'i' "
            "AND n.nspname = ANY(current_schemas(false))"
        )
    )
    return {r[0] for r in rows}


def invalid_index_names(conn: Connection) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT c.relname FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE NOT i.indisvalid "
            "AND n.nspname = ANY(current_schemas(false))"
        )
    )
    return {r[0] for r in rows}


def _existing_column_signatures(
    conn: Connection,
) -> set[tuple[str, tuple[str, ...], bool]]:
    """``(table, ordered column-name tuple, is_unique)`` for VALID plain btree indexes.

    Excludes partial (``indpred``) and expression (``indexprs``) indexes so the
    signatures only describe simple column indexes — the only kind we match by
    column set.

    Only ``indisvalid`` indexes count as coverage: an INVALID index (a failed
    concurrent build, possibly under a legacy name) is unusable by the planner,
    so treating it as coverage would skip creating the canonical model index and
    leave the table with no usable index while reconciliation reported success.

    ``indisunique`` is part of the signature so a UNIQUE model index is not
    considered covered by a non-unique index over the same columns (which would
    skip enforcing the uniqueness constraint), and vice versa.
    """
    rows = conn.execute(
        text(
            """
            SELECT t.relname AS table_name,
                   i.relname AS index_name,
                   ix.indisunique AS is_unique,
                   array_agg(a.attname ORDER BY k.ord) AS cols
            FROM pg_index ix
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN pg_class t ON t.oid = ix.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
            WHERE n.nspname = ANY(current_schemas(false))
              AND ix.indisvalid
              AND ix.indpred IS NULL
              AND ix.indexprs IS NULL
              AND a.attnum > 0
            GROUP BY t.relname, i.relname, ix.indisunique
            """
        )
    )
    sigs: set[tuple[str, tuple[str, ...], bool]] = set()
    for table_name, _index_name, is_unique, cols in rows:
        sigs.add((table_name, tuple(cols), bool(is_unique)))
    return sigs


def reconcile_indexes(conn: Connection, indexes: Iterable[Index]) -> list[str]:
    """Create any of ``indexes`` missing from the live DB. Returns created names.

    ``conn`` is a synchronous connection; the boot path runs it in ``AUTOCOMMIT``
    isolation so each plain ``CREATE INDEX`` commits — and releases its ``SHARE``
    lock — immediately rather than holding every lock until one big transaction
    commits.
    """
    existing_names = existing_index_names(conn)
    invalid_names = invalid_index_names(conn)
    signatures = _existing_column_signatures(conn)

    created: list[str] = []
    for index in indexes:
        if not index.name or index.table is None:
            continue

        is_invalid = index.name in invalid_names

        # Already present and valid → nothing to do.
        if index.name in existing_names and not is_invalid:
            continue

        # Drop a leftover INVALID build of this exact name FIRST. It is unusable
        # and ``CREATE INDEX ... IF NOT EXISTS`` checks existence, not validity,
        # so it would otherwise be skipped forever. Dropping *before* the
        # coverage guard is what keeps the "never duplicate a legacy-covered
        # column set" guarantee: an INVALID canonical index must not bypass the
        # guard and get recreated next to a valid legacy index covering the
        # same columns. Plain ``DROP INDEX`` (not CONCURRENTLY): a concurrent
        # drop also waits for every overlapping transaction to drain and would
        # hang behind another live pod, whereas a plain drop only takes a brief
        # ACCESS EXCLUSIVE lock on the table.
        if is_invalid:
            conn.execute(text(f'DROP INDEX IF EXISTS "{index.name}"'))

        # Columns already covered by another VALID (e.g. legacy-named) index →
        # skip to avoid a duplicate. ``signatures`` already excludes INVALID
        # indexes, so this guard is applied regardless of whether the canonical
        # name was just dropped as INVALID. Only applies to plain column indexes.
        signature = _plain_column_signature(index)
        if signature is not None and signature in signatures:
            continue

        ddl = str(CreateIndex(index).compile(dialect=PG_DIALECT)).strip()
        try:
            conn.execute(text(idempotent_if_not_exists(ddl)))
        except Exception as exc:
            if is_duplicate_index_error(exc):
                # Another pod's in-flight build won the catalog race (23505
                # on pg_class / 42P07) — the index exists; nothing to do.
                logger.warning(
                    "CREATE INDEX for %s hit a concurrent-build duplicate "
                    "(another pod won the race) — index already exists; "
                    "skipping",
                    index.name,
                )
                continue
            raise
        created.append(f"{index.table.name}.{index.name}")

    return created


def reconcile_model_indexes(conn: Connection) -> list[str]:
    """Create every ``Base.metadata`` index missing from the live database.

    Imported lazily so this module stays import-safe for Alembic migrations
    that only need the low-level helpers.

    Deferred indexes (``DEFERRED_INDEX_ACCESS_METHODS`` — currently the HNSW
    ANN indexes) are excluded: their builds take minutes on populated tables
    and this reconciler runs inside the blocking startup path, where a long
    build starves the liveness probe and crash-loops the container.
    The post-startup background builder
    (``app/core/ann_index_builder.py``) owns them instead.
    """
    from app.core.database import Base

    all_indexes: list[Index] = [
        index
        for table in Base.metadata.sorted_tables
        for index in table.indexes
        if not is_deferred_index(index)
    ]
    return reconcile_indexes(conn, all_indexes)
