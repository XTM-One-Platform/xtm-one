"""per-platform tool approval: declared capability + operator policy

Adds three columns to ``platform_registrations``.

A registered Suite platform (OpenCTI / OpenAEV / OpenCRQ) contributes tools XTM
One cannot classify: they are not on the shipped whitelist and never can be,
because we do not know what they do. So they gate by default, which means that
on upgrade every tool a connected platform contributed starts asking for
approval at once — across every agent that uses them.

Whether that is right depends on two different things, which is why this is
three columns and not one:

* ``supports_approval_prompts`` — a FACT the platform reports about itself: can
  its embedded assistant display an approval prompt and return a decision?
  NULL means it has never said, which is every build predating the capability
  and therefore most of the fleet today. NULL is deliberately not FALSE: an
  undeclared platform falls through to the operator's policy, while one that
  declares FALSE has actively told us its assistant cannot ask.

* ``approval_policy`` — a POLICY only an operator can set: what happens to
  those tools while no prompt can be shown. Seeded once, at row creation, from
  ``HITL_PLATFORM_DEFAULT_POLICY``.

* ``approval_policy_set_by`` — provenance. Lets the UI say whether a setting is
  protecting the operator or merely inherited from an install-time decision
  nobody remembers, and stops an explicit human choice being overwritten by a
  later automatic one.

Existing rows get ``require`` / ``default``: unchanged behaviour, because the
deployment variable seeds new registrations only. An operator who wants an
already-registered platform exempted makes that choice explicitly, and it is
recorded as theirs.

Idempotent: guarded by column-existence probes, so re-running converges without
redoing work. Follows the canonical lock-safe recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: b2p1lat0173
Revises: a1h2itl0173
Create Date: 2026-08-14 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "b2p1lat0173"
down_revision: str | None = "a1h2itl0173"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_registrations"

# Column NAMES only. The ``sa.Column`` objects are built fresh inside each
# function rather than held here: ``op.add_column`` binds the object it is
# given to an internal table, so a module-level instance is single-use and a
# later consumer can silently emit DDL without its NOT NULL / DEFAULT.
_COLUMN_NAMES = ("supports_approval_prompts", "approval_policy", "approval_policy_set_by")


def _new_columns() -> dict[str, sa.Column]:
    """Fresh column definitions, one set per call."""
    return {
        "supports_approval_prompts": sa.Column(
            "supports_approval_prompts", sa.Boolean(), nullable=True
        ),
        # ``server_default`` matters on both NOT NULL columns: registrations
        # are written by a token-authenticated endpoint that can race this
        # migration, and a Python-side default alone would fail those inserts
        # in the window between the DDL and the new code rolling out. It also
        # backfills existing rows to the safe value in the same statement.
        "approval_policy": sa.Column(
            "approval_policy", sa.String(20), nullable=False, server_default="require"
        ),
        "approval_policy_set_by": sa.Column(
            "approval_policy_set_by",
            sa.String(20),
            nullable=False,
            server_default="default",
        ),
    }


def _column_exists(conn, table: str, column: str) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).first()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # A deployment that has never had a Suite platform connected may not have
    # the table at all yet; nothing to widen in that case.
    if not inspector.has_table(_TABLE):
        return

    missing = [n for n in _COLUMN_NAMES if not _column_exists(conn, _TABLE, n)]
    if not missing:
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)

    columns = _new_columns()
    for name in missing:
        op.add_column(_TABLE, columns[name])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return

    present = [n for n in _COLUMN_NAMES if _column_exists(conn, _TABLE, n)]
    if not present:
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)

    for name in present:
        op.drop_column(_TABLE, name)
