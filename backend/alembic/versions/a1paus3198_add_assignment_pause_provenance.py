"""record who/what paused an assignment

``assignments.enabled = False`` renders as "Paused" in the UI and carried no
provenance at all, while five unrelated code paths write it: a human toggling
the switch, an agent calling the ``update_assignment`` builder tool, the
integration auth-failure quarantine, the scheduler disabling an assignment
whose cron it cannot parse, and the copy paths (duplicate / import / catalog
install) that create one switched off. An assignment that silently stopped
firing was indistinguishable from one somebody paused on purpose weeks ago.

Five nullable columns, no backfill — all written and cleared as one block by
``app/services/assignment_pause.py``:

* ``paused_at``          — when the current pause started;
* ``paused_reason``      — the machine-readable cause (``user_toggle`` /
  ``agent_tool`` / ``auth_quarantine`` / ``invalid_schedule`` /
  ``created_disabled``). The actor class is derivable from it, hence no
  separate actor column;
* ``paused_by_user_id``  — the human behind it (the one who toggled it, or the
  one an agent acted as); NULL for autonomous system pauses;
* ``paused_by_label``    — denormalized display label (user email, agent name,
  component), so the synchronous assignment serializer needs no join and the
  explanation survives the actor being deleted;
* ``paused_details``     — the already-composed human-readable explanation.

Historic rows read as NULL, which is honest: those pauses were never recorded
and inventing an actor for them would poison the very question this feature
exists to answer. The UI falls back to "reason unknown (paused before this was
tracked)".

Additive and lock-light: ``ADD COLUMN`` with no default is metadata-only on
PG 11+ (no table rewrite). No index — these columns are only ever read for one
already-fetched assignment, never filtered on.

The ``paused_by_user_id`` FK IS created here, in two lock-light steps. Skipping
it (the first draft) would have left fresh installs — which get it from
``create_all`` — and migrated databases with two different production schemas,
and left ``alembic revision --autogenerate`` proposing the constraint forever.

What the two-step buys is the SCAN, not the lock duration. A plain
``ADD CONSTRAINT`` holds ``SHARE ROW EXCLUSIVE`` on ``assignments`` *while it
scans the whole table*; ``NOT VALID`` skips the scan entirely, and the later
``VALIDATE CONSTRAINT`` does it under the weaker ``SHARE UPDATE EXCLUSIVE``
(which does not block reads or writes). It does NOT release the ``users`` lock
any earlier: Alembic runs all pending migrations in ONE transaction (see
``migration_ddl.set_lock_timeout``), so every lock this migration takes is held
to commit either way. The scan is trivial here regardless — the column was
created NULL two statements earlier.

Because ``ADD CONSTRAINT ... REFERENCES users(id)`` locks ``users`` as well as
``assignments``, the pre-flight clears lock holders on BOTH. Clearing only the
referencing table would leave a long-lived agentic session's ``ACCESS SHARE``
on ``users`` to make the ALTER wait, hit the ``lock_timeout`` and fail 55P03 —
the precise outage ``migration_ddl`` exists to prevent, and one that would
repeat on every retry until that session ended.

One index IS created: a partial index on ``paused_by_user_id``. Not for
querying — nothing filters on it — but for the ``ON DELETE SET NULL``
referential action. Without it PostgreSQL's RI trigger runs
``... WHERE paused_by_user_id = $1`` as a sequential scan of ``assignments`` on
every user deletion. Partial (``WHERE paused_by_user_id IS NOT NULL``) keeps it
tiny: only pauses with a human actor populate the column at all.

``ON DELETE SET NULL`` matches the model. Deleting a user must not delete the
record of what they paused — and the pause stays readable regardless, because
``paused_by_label`` is denormalized precisely so a removed actor does not blank
out the explanation.

Revision ID: a1paus3198
Revises: x1xtmmcp0001
Create Date: 2026-08-29 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import (
    create_index_concurrently,
    set_lock_timeout,
    terminate_lock_holders,
)

revision: str = "a1paus3198"
down_revision: str | Sequence[str] | None = "x1xtmmcp0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "assignments"
_FK_NAME = "fk_assignments_paused_by_user_id_users"
_FK_INDEX = "ix_assignments_paused_by_user_id"

# (column, type) — every column this migration owns.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("paused_at", "TIMESTAMPTZ"),
    ("paused_reason", "VARCHAR(32)"),
    ("paused_by_user_id", "UUID"),
    ("paused_by_label", "VARCHAR(320)"),
    ("paused_details", "TEXT"),
)


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _has_table(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        ).scalar()
    )


def _constraint_state(conn, name: str) -> str | None:
    """Return ``"valid"`` / ``"not_valid"`` for *name*, or ``None`` if absent.

    Distinguishing the two is what makes the two-step add re-runnable: a
    replica that crashed between ``ADD ... NOT VALID`` and ``VALIDATE`` must
    resume at the validation, not try to add the constraint again.
    """
    row = conn.execute(
        sa.text(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname = :n AND connamespace = current_schema()::regnamespace"
        ),
        {"n": name},
    ).scalar()
    if row is None:
        return None
    return "valid" if row else "not_valid"


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return

    for column, coltype in _COLUMNS:
        # Idempotency probe first, so a steady-state re-run (fresh installs
        # stamp head; a retrying replica re-enters here) takes NO lock at all.
        if _has_column(conn, _TABLE, column):
            continue
        # An agentic loop holds a session open for minutes, and every
        # subsequent reader queues behind a blocked ALTER — clear same-role
        # lock holders so the brief ACCESS EXCLUSIVE is acquired instantly,
        # with a short lock_timeout so a racing reconnect fails fast (55P03)
        # instead of wedging the queue. The startup runner retries.
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(
            sa.text(
                f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {column} {coltype}"
            )
        )

    # The FK, in two lock-light steps (see the module docstring). Guarded on
    # the users table existing so a partial schema skips rather than aborts.
    if not _has_table(conn, "users") or not _has_column(
        conn, _TABLE, "paused_by_user_id"
    ):
        return

    state = _constraint_state(conn, _FK_NAME)
    if state is None:
        # BOTH tables: the constraint locks the referenced table too, so
        # clearing holders on ``assignments`` alone would still let a session
        # holding ACCESS SHARE on ``users`` time the ALTER out (55P03).
        terminate_lock_holders(conn, _TABLE)
        terminate_lock_holders(conn, "users")
        set_lock_timeout(conn)
        # NOT VALID: no table scan (the lock is held to commit either way —
        # Alembic wraps all pending migrations in one transaction).
        op.execute(
            sa.text(
                f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_FK_NAME} "
                "FOREIGN KEY (paused_by_user_id) REFERENCES users(id) "
                "ON DELETE SET NULL NOT VALID"
            )
        )
        state = "not_valid"

    if state == "not_valid":
        # SHARE UPDATE EXCLUSIVE — does not block reads or writes. The scan is
        # trivial: the column was created NULL just above.
        op.execute(sa.text(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_FK_NAME}"))

    # Index for the ON DELETE SET NULL referential action (see the module
    # docstring), NOT for querying. CONCURRENTLY must run outside a
    # transaction and must not inherit the lock_timeout belt above.
    with op.get_context().autocommit_block():
        create_index_concurrently(
            conn,
            _FK_INDEX,
            _TABLE,
            "(paused_by_user_id) WHERE paused_by_user_id IS NOT NULL",
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return

    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_FK_INDEX}")

    # Drop the FK before its column (dropping the column would take it along,
    # but being explicit keeps the downgrade readable and order-independent).
    if _constraint_state(conn, _FK_NAME) is not None:
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(
            sa.text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_FK_NAME}")
        )

    for column, _coltype in _COLUMNS:
        if not _has_column(conn, _TABLE, column):
            continue
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}"))
