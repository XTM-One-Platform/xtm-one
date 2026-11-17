"""designate an MCP tool as a Deep Investigation source

Adds ``mcp_servers.investigation_tool_name`` and
``mcp_servers.investigation_argument``.

An investigation can now query an MCP tool, which raises a question the HTTP
sources never did: **which** tool may it call? ``discovered_tools`` holds a
name and a description per tool and nothing more — no read-only annotation, no
input schema. Nothing there separates ``lookup_indicator`` from
``create_issue``, and an investigation that guessed wrong would mutate the
world while its report claimed it had read it.

So the designation is opt-in and human-made: NULL means "not an investigation
source", which is the correct state for every server that exists today and for
every one created afterwards. There is no backfill and no default — inferring
a tool from its name would reintroduce exactly the guess these columns exist
to avoid.

``investigation_argument`` is separately nullable because it has a safe
derivation: with a NULL argument the querier reads the tool's input schema at
call time and uses its single required string property, failing the step with
a plain message when the schema makes that ambiguous. Storing it lets an
operator pin the argument for a tool whose schema is unhelpful.

Idempotent: guarded by column-existence probes, so re-running converges.
Follows the canonical lock-safe recipe: probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL. Both columns are nullable with no default, so
the ``ALTER`` is a catalogue update and rewrites no rows.

Revision ID: i4mcp1939002
Revises: i3evd1939001
Create Date: 2026-09-04 09:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "i4mcp1939002"
down_revision: str | Sequence[str] | None = "i3evd1939001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "mcp_servers"
_COLUMNS = ("investigation_tool_name", "investigation_argument")


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
    if not inspector.has_table(_TABLE):
        return

    missing = [c for c in _COLUMNS if not _column_exists(conn, _TABLE, c)]
    if not missing:
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)

    for column in missing:
        op.add_column(_TABLE, sa.Column(column, sa.String(200), nullable=True))


def downgrade() -> None:
    """Drop both columns, losing every designation.

    Stated rather than worked around: after a downgrade, no MCP server is an
    investigation source any more, and re-upgrading does not bring the
    designations back — they were operator choices, not derived data. That is
    the honest cost of removing the only place they were recorded.
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return

    present = [c for c in _COLUMNS if _column_exists(conn, _TABLE, c)]
    if not present:
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)

    for column in present:
        op.drop_column(_TABLE, column)
