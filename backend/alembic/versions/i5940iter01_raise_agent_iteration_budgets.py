"""raise the agent iteration budgets: 300 by default, at least 100

The platform default of the agentic loop's iteration cap goes from 25 to 300
(``DEFAULT_MAX_ITERATIONS``), and every cap people configured below 100 is
raised to 100:

* ``general_assistant_config.max_iterations``: the column default follows the
  model (25 -> 300). The settings row still at the old default moves to 300 -
  it is the General Assistant's cap and the one every agent without its own
  value inherits. Any other value below 100 is raised to 100; a value of 100
  or more is an administrator's choice and is kept.
* ``agents.max_iterations``: a value below 100 is raised to 100. ``NULL``
  (inherit the platform default) is left alone, and so are the built-in agents
  (``is_builtin``): the seed rewrites their cap from ``BUILTIN_AGENTS`` on every
  boot.

Lock profile: ``ALTER COLUMN ... SET DEFAULT`` is metadata-only but takes
ACCESS EXCLUSIVE on ``general_assistant_config``, which every chat turn reads,
so it follows the canonical pre-flight: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL. The two UPDATEs take
row locks only, on the singleton row and on the agents below the floor, under
the same lock_timeout belt; a re-run finds nothing below the floor and writes
nothing.

Downgrade: the column default goes back to 25. The raised values stay: the
previous ones are not recorded, and every value left is a valid cap for the
older release (1 to ``MAX_ITERATIONS_CEILING``).

Revision ID: i5940iter01
Revises: r5931gareport
Create Date: 2026-10-17 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "i5940iter01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r5931gareport"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GA_TABLE = "general_assistant_config"
_AGENTS_TABLE = "agents"
_COLUMN = "max_iterations"

_PREVIOUS_DEFAULT = 25
_NEW_DEFAULT = 300
_FLOOR = 100


def _column_default(conn, table: str) -> str | None:
    """The column's default expression, or ``None`` when the column is absent."""
    row = conn.execute(
        sa.text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": _COLUMN},
    ).first()
    if row is None:
        return None
    return (row[0] or "").strip()


def _set_default(conn, value: int) -> None:
    current = _column_default(conn, _GA_TABLE)
    if current is None or current == str(value):
        return
    terminate_lock_holders(conn, _GA_TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_GA_TABLE} ALTER COLUMN {_COLUMN} SET DEFAULT {value}")


def upgrade() -> None:
    conn = op.get_bind()
    _set_default(conn, _NEW_DEFAULT)
    set_lock_timeout(conn)
    conn.execute(
        sa.text(
            f"UPDATE {_GA_TABLE} SET {_COLUMN} = CASE "  # noqa: S608
            f"WHEN {_COLUMN} = :previous THEN :new ELSE :floor END "
            f"WHERE {_COLUMN} < :floor"
        ),
        {"previous": _PREVIOUS_DEFAULT, "new": _NEW_DEFAULT, "floor": _FLOOR},
    )
    conn.execute(
        sa.text(
            f"UPDATE {_AGENTS_TABLE} SET {_COLUMN} = :floor "  # noqa: S608
            f"WHERE {_COLUMN} < :floor AND is_builtin IS NOT TRUE"
        ),
        {"floor": _FLOOR},
    )


def downgrade() -> None:
    _set_default(op.get_bind(), _PREVIOUS_DEFAULT)
