"""share coordination state per workspace, not per user

Replaces the per-user unique constraint on ``shared_states`` and ``worksets``
with two PARTIAL unique indexes each, so a row's identity is per-USER when it
is unfiled and per-WORKSPACE when it is filed.

This is the change that makes a workspace shared between two PEOPLE rather
than between two agents acting for one person. Before it, `w3wsp3314003` gave
the tables a `workspace_id` but `(user_id, key)` still decided identity, so
two users in one workspace each built their own state and neither saw the
other's.

**Why not one composite constraint.** A single unique cannot express "unique
per user OR unique per workspace". The tempting shortcut — set `user_id = NULL`
on workspace rows to make them user-agnostic — is worse than it looks: both
models used to record that *"NULLs are distinct in Postgres, so anonymous rows
never collide"*, and that property would silently disable uniqueness
altogether. Every write would insert a new row instead of updating one, and
`resolve_workset`'s ON CONFLICT would stop conflicting. So the predicates key
on ``workspace_id``, which is never NULL on the rows it governs.

**Ordering, and why the indexes are renamed.** The replacements are built
FIRST and the old constraint is dropped only after — otherwise there is a
window with no uniqueness at all, which on a rolling deploy is a window for an
older replica to insert a duplicate. Two objects cannot share a name and the
old CONSTRAINT owns an index called ``uq_<table>_user_key``, so the unfiled
replacement is named ``uq_<table>_user_key_unfiled``.

**The replacements are built IN PLACE, not CONCURRENTLY**
(``create_index_in_place``, inside an ``autocommit_block``). A concurrent
build waits for every snapshot older than itself on the whole database;
during a rolling deploy on a busy install the previous release always holds
one, so the build rode its ceiling on every boot and the upgrade never
landed. A plain build takes a SHARE lock on its own table for the seconds it
runs (writes wait, reads do not), granted at once after the terminate
pre-flight and bounded by ``lock_timeout``; a timeout is raised and retried by
the startup runner. The constraint DROP is ordinary lock-taking DDL on a hot
table, so it uses ``terminate_lock_holders`` + ``set_lock_timeout``.

No data moves: this revision writes nothing to either table.

## The two hazards this revision has to survive, and how

Both come from the concurrent builds the first version of this file ran — a
``CONCURRENTLY`` build is the one kind of DDL that can fail *halfway*, and an
install that attempted it may still carry its remains — and both were live
defects in that version.

**1. A retry must not be able to remove uniqueness.** An interrupted
concurrent build (a cancelled deploy, a dropped connection, a deadlock) leaves
the index present but ``indisvalid = false``. ``CREATE UNIQUE INDEX
CONCURRENTLY IF NOT EXISTS`` matches that carcass by NAME and creates nothing,
so a re-run used to sail past step 1 and then drop the old constraint — ending
with an invalid index, no constraint, and **no enforced uniqueness at all** on
a table whose identity model is the entire point of the change. Two guards,
because either alone is insufficient:

* an invalid leftover is dropped before the rebuild, so ``IF NOT EXISTS``
  cannot match it;
* the old constraint is dropped **only after both replacements are confirmed
  ``indisvalid``**. This is the load-bearing one: it means no failure mode,
  anticipated or not, can leave the table unprotected — the worst outcome
  becomes a failed migration with the old constraint still standing, which is
  a deploy to retry rather than data to reconcile.

**2. Rows that the new index cannot accept may already exist.** The first
version of this file asserted that "every existing row has ``workspace_id IS
NULL``", and that was false for ``worksets``: the immediately preceding
revision's PR populates ``worksets.workspace_id`` (``resolve_workset`` writes
the typed column), while identity was still ``(user_id, key)``. So two users
acting in one shared workspace could each insert a row with the SAME
``(workspace_id, key)`` and a different ``user_id`` — permitted by the old
constraint, rejected by the new index. Merging this chain PR by PR is exactly
the deployment shape that produces them.

A duplicate is therefore probed for **before any DDL runs** and refused with
the offending groups named. Not deduplicated: picking a winner would delete a
workset row, and a workset row owns a knowledge base, its documents and its
vectors. Choosing which person's corpus to destroy is not a decision a
migration gets to make silently — the downgrade already refuses on the same
grounds, and for the same reason.

Revision ID: w4wsp3314004
Revises: w3wsp3314003
Create Date: 2026-09-04 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import (
    create_index_in_place,
    set_lock_timeout,
    terminate_lock_holders,
)

revision: str = "w4wsp3314004"
down_revision: str | Sequence[str] | None = "w3wsp3314003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("shared_states", "worksets")

#: The partial uniques this revision installs, as
#: ``(name suffix, indexed columns, predicate)``. Single-sourced so the
#: duplicate probe, the invalid-leftover drop, the build and the validity
#: check cannot describe different indexes — a probe that checked a predicate
#: the build did not use would clear a table it never looked at.
_REPLACEMENTS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("user_key_unfiled", ("user_id", "key"), "workspace_id IS NULL"),
    ("workspace_key", ("workspace_id", "key"), "workspace_id IS NOT NULL"),
)


def _has_constraint(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}
        ).scalar()
    )


def _index_validity(conn, name: str) -> bool | None:
    """``indisvalid`` for an index, or ``None`` when there is no such index.

    Three-valued on purpose. ``pg_indexes``/``information_schema`` cannot tell
    a valid index from the invalid remains of a failed ``CONCURRENTLY`` build,
    and that distinction is the whole point here: absent means "build it",
    invalid means "drop it and build it", valid means "leave it alone". A
    boolean probe collapses the middle case into the last one, which is the
    bug this replaces.
    """
    return conn.execute(
        sa.text(
            "SELECT i.indisvalid "
            "FROM pg_class c "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE c.relname = :n "
            "AND c.relnamespace = ("
            "    SELECT oid FROM pg_namespace WHERE nspname = current_schema()"
            ")"
        ),
        {"n": name},
    ).scalar()


def _duplicate_groups(
    conn, table: str, columns: tuple[str, ...], predicate: str
) -> list[tuple]:
    """Groups that would violate the unique index described by *columns*.

    The index's own predicate is not enough to reproduce its semantics: a
    unique index treats NULLs as DISTINCT, so rows with a NULL indexed column
    never collide, while ``GROUP BY`` folds them into one group. Probing
    without excluding them would report an anonymous ``(NULL, 'k')`` pair as a
    conflict and refuse a migration that would have succeeded.
    """
    cols = ", ".join(columns)
    nulls = " ".join(f"AND {c} IS NOT NULL" for c in columns)
    rows = conn.execute(
        sa.text(
            f"SELECT {cols}, count(*) AS n FROM {table} "  # noqa: S608 - names are literals above
            f"WHERE {predicate} {nulls} "
            f"GROUP BY {cols} HAVING count(*) > 1 "
            f"ORDER BY n DESC LIMIT 20"
        )
    ).all()
    return [tuple(r) for r in rows]


def upgrade() -> None:
    conn = op.get_bind()

    # ── 0. Pre-flight, before ANY DDL ────────────────────────────────────
    # Refused rather than deduplicated, and refused here rather than by the
    # index build, so the error names the offending groups instead of a bare
    # unique violation.
    conflicts: list[str] = []
    for table in _TABLES:
        for _suffix, columns, predicate in _REPLACEMENTS:
            for group in _duplicate_groups(conn, table, columns, predicate):
                *values, count = group
                pairs = ", ".join(
                    f"{c}={v!r}" for c, v in zip(columns, values, strict=True)
                )
                conflicts.append(f"{table}: {pairs} ({count} rows)")

    if conflicts:
        raise RuntimeError(
            "Cannot install the workspace identity indexes: rows already "
            "exist that they would reject. These were writable while "
            "identity was still (user_id, key) — most likely two users "
            "acting in one shared workspace — and one of each group has to "
            "be kept deliberately, because a `worksets` row owns a knowledge "
            "base, its documents and its vectors. Reconcile them, then "
            "re-run this migration:\n  "
            + "\n  ".join(conflicts)
        )

    # ── 1. Build both replacements before anything is dropped ────────────
    # Required: a lock or statement timeout raises, and the startup runner
    # retries it. An invalid leftover of an earlier concurrent attempt is
    # dropped by the helper before the rebuild.
    with op.get_context().autocommit_block():
        for table in _TABLES:
            for suffix, columns, predicate in _REPLACEMENTS:
                create_index_in_place(
                    conn,
                    f"uq_{table}_{suffix}",
                    table,
                    f"({', '.join(columns)}) WHERE {predicate}",
                    unique=True,
                    required=True,
                )

    # ── 2. Confirm the replacements before removing the thing they replace ─
    # The guarantee that matters: the old constraint is never dropped unless
    # both partial uniques are actually enforcing. Any failure — including
    # ones not anticipated here — therefore ends with the table still
    # protected by the constraint it started with.
    unusable = [
        f"uq_{table}_{suffix}"
        for table in _TABLES
        for suffix, _, _ in _REPLACEMENTS
        if _index_validity(conn, f"uq_{table}_{suffix}") is not True
    ]
    if unusable:
        raise RuntimeError(
            "Refusing to drop the per-user constraint: these replacement "
            "indexes are missing or INVALID, so dropping it would leave the "
            "table with no enforced uniqueness. Drop them (DROP INDEX) and "
            "re-run:\n  " + "\n  ".join(unusable)
        )

    # ── 3. Only now drop the constraint the pair replaces ────────────────
    # Between step 1 and here both the old constraint and the new indexes
    # enforce at once. That is redundant rather than contradictory for
    # UNFILED rows, which the constraint and the unfiled predicate cover
    # identically. For FILED rows the two genuinely disagree — the constraint
    # allows what the workspace index forbids — which is why step 0 refuses
    # when such a row already exists rather than discovering it here.
    for table in _TABLES:
        name = f"uq_{table}_user_key"
        if not _has_constraint(conn, name):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")


def downgrade() -> None:
    conn = op.get_bind()

    # Restore the constraint first, for the same reason the upgrade builds
    # before dropping: no window without uniqueness. It can only be restored
    # if no user holds one key twice - once unfiled and once filed into a
    # workspace, which is exactly what this migration made possible - so a
    # downgrade after such a pair exists MUST fail loudly rather than pick a
    # winner. Losing one person's state silently would be worse than a failed
    # downgrade. ADD CONSTRAINT raises on its own here, so the refusal is the
    # database's and cannot drift from what the constraint actually accepts.
    for table in _TABLES:
        name = f"uq_{table}_user_key"
        if _has_constraint(conn, name):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE (user_id, key)"
        )

    # Dropped only after the constraint is back, and only then: an invalid
    # leftover is dropped by the same statement, since IF EXISTS matches on
    # name regardless of validity.
    with op.get_context().autocommit_block():
        for table in _TABLES:
            for suffix, _, _ in _REPLACEMENTS:
                op.execute(
                    f"DROP INDEX CONCURRENTLY IF EXISTS uq_{table}_{suffix}"
                )
