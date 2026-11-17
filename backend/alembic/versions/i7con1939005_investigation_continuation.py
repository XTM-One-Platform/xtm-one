"""continue an investigation past its budget

Two additive columns, and both exist because a run that stopped on its budget
must be *continuable* rather than merely re-runnable.

``investigation_steps.source_ref``
    What a step would have queried, as a self-describing dict — the kind plus
    the ids that address it (``{"kind": "mcp_tool", "server_id": …}``).

    Without it a skipped step cannot be re-executed at all. The row already
    carries ``source_name`` and ``query``, and for an HTTP source that is
    almost enough because the query IS the URL — but a knowledge-base or MCP
    source is named ``"{server_name} · {tool}"``, and a NAME is not an
    address. Matching it back by name would break on a rename and be ambiguous
    between two servers offering the same tool.

    **It carries ids, never credentials.** A continuation re-resolves every id
    against the caller's own visibility at the moment they ask, so a corpus
    they have since lost access to is refused and disclosed rather than
    queried off a stale reference. The column is a pointer, not a grant.

``investigations.continues_investigation_id``
    The parent whose unqueried sources this run picked up.

    A continuation is a NEW run, not a resumed one, and the reason is written
    into the state machine it would otherwise have to break: ``skipped`` is in
    ``TERMINAL_STEP_STATUSES``, which is what enforces "a failure never
    becomes a success". Letting ``skipped -> active`` happen would punch a hole
    in the one invariant that stops a step which never ran from later
    rendering as one that succeeded.

    There is a second reason, and it is the one a reader will actually feel:
    **citation numbering**. A report's source key is numbered per run. Mutating
    a run whose report someone has already read would renumber the sources
    under them. A linked child run keeps each report internally coherent and
    still lets the chain be summed — which is what the cumulative spend cap
    needs.

    ``ON DELETE SET NULL``: losing the parent costs the child nothing but the
    link. Its own steps, findings and report are intact, and a chain that has
    lost its head is still a valid run — the opposite of the tool tables,
    where the pointer is the only thing keeping a corpus reachable.

**Lock safety.** Both tables are populated and both ALTERs are metadata-only
on PG 11+ (a nullable column with no default), but per the lock rules there is
no cold table: the recipe is the idempotency probe, then
``terminate_lock_holders`` + ``set_lock_timeout``, then the guarded DDL. The FK
is added ``NOT VALID`` and validated in a separate statement, because
``VALIDATE CONSTRAINT`` takes only ``SHARE UPDATE EXCLUSIVE`` and therefore
never blocks the readers an in-line FK would queue behind. The index is built
in place (``create_index_in_place``) in an autocommit block, not
``CONCURRENTLY``: a concurrent build waits for every older snapshot on the
whole database, which a rolling deploy on a busy install never lets it
outlast, while a plain build holds a SHARE lock on this one table for the
seconds it runs.

Revision ID: i7con1939005
Revises: i6rep1939004
Create Date: 2026-09-10 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import (
    create_index_in_place,
    set_lock_timeout,
    terminate_lock_holders,
)

revision: str = "i7con1939005"
down_revision: str | Sequence[str] | None = "i6rep1939004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK_NAME = "fk_investigations_continues_investigation_id"
_IX_NAME = "ix_investigations_continues_investigation_id"


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


def _constraint_state(conn, name: str) -> bool | None:
    """``convalidated`` for a constraint, or ``None`` when it does not exist.

    Three-valued for the same reason the index probes are: absent
    means "add it", present-but-not-validated means "validate it", validated
    means "leave it alone". A boolean probe would skip the validation step on a
    re-run that crashed between ADD and VALIDATE, leaving a FK that never
    enforces.
    """
    return conn.execute(
        sa.text("SELECT convalidated FROM pg_constraint WHERE conname = :n"),
        {"n": name},
    ).scalar()


def upgrade() -> None:
    conn = op.get_bind()

    # ── investigation_steps.source_ref ──────────────────────────────────
    if not _has_column(conn, "investigation_steps", "source_ref"):
        terminate_lock_holders(conn, "investigation_steps")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE investigation_steps "
            "ADD COLUMN IF NOT EXISTS source_ref JSON"
        )

    # ── investigations.continues_investigation_id ───────────────────────
    if not _has_column(conn, "investigations", "continues_investigation_id"):
        terminate_lock_holders(conn, "investigations")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE investigations "
            "ADD COLUMN IF NOT EXISTS continues_investigation_id UUID"
        )

    # The FK in two steps. ADD ... NOT VALID takes a brief lock and skips the
    # table scan; VALIDATE does the scan under SHARE UPDATE EXCLUSIVE, which
    # readers and writers do not queue behind. Split because an in-line FK
    # would hold ACCESS EXCLUSIVE for the whole scan on a table the agentic
    # loop keeps open transactions against.
    validated = _constraint_state(conn, _FK_NAME)
    if validated is None:
        terminate_lock_holders(conn, "investigations")
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE investigations ADD CONSTRAINT {_FK_NAME} "
            "FOREIGN KEY (continues_investigation_id) "
            "REFERENCES investigations(id) ON DELETE SET NULL NOT VALID"
        )
        validated = False
    if validated is False:
        # No lock recipe: VALIDATE does not take ACCESS EXCLUSIVE.
        op.execute(f"ALTER TABLE investigations VALIDATE CONSTRAINT {_FK_NAME}")

    # Walking a chain reads by parent id, so the lookup is indexed.
    with op.get_context().autocommit_block():
        create_index_in_place(
            conn, _IX_NAME, "investigations", "(continues_investigation_id)"
        )


def downgrade() -> None:
    conn = op.get_bind()

    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_IX_NAME}")

    # The FK falls with the column, so it is not dropped separately — but the
    # column drop still needs the lock recipe.
    if _has_column(conn, "investigations", "continues_investigation_id"):
        terminate_lock_holders(conn, "investigations")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE investigations "
            "DROP COLUMN IF EXISTS continues_investigation_id"
        )

    if _has_column(conn, "investigation_steps", "source_ref"):
        terminate_lock_holders(conn, "investigation_steps")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE investigation_steps DROP COLUMN IF EXISTS source_ref"
        )
