"""recompute the workset counters from their items

Before the workset row lock of the item write path, ``item_count``,
``evicted_count`` and ``content_chars`` were moved in Python on a row loaded
without a lock, so two concurrent writers to one workset lost updates and the
stored counters drifted from what the items say. This recomputes the three of
them from ``workset_items`` for every existing workset: live items (every
status but ``evicted``), evicted items, and the content characters of the
live ones. A workset whose counters already agree is not written, so a re-run
is a no-op.

No DDL, so nothing lock-taking. The worksets are read ``_BATCH_SIZE`` at a
time by id and each batch's UPDATE is its own transaction
(``autocommit_block``), so no transaction holds more than ``_BATCH_SIZE`` row
locks; ``lock_timeout`` bounds each batch's wait behind a writer. Fresh
installs run ``create_all`` + ``stamp head``: the body never executes there
and there is nothing to reconcile.

Revision ID: w12165wscnt1
Revises: n12077kanon1
Create Date: 2026-11-16 04:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import DEFAULT_LOCK_TIMEOUT

revision: str = "w12165wscnt1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "n12077kanon1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH_SIZE = 500

_NEXT_IDS = sa.text(
    "SELECT id FROM worksets WHERE (CAST(:after AS uuid) IS NULL OR id > CAST(:after AS uuid)) ORDER BY id LIMIT :n"
)

#: One batch, the worksets with ids in [first, last]: the counters the items
#: say, written only where they differ from the stored ones.
_RECONCILE = sa.text(
    "UPDATE worksets AS w SET "
    "item_count = s.live, evicted_count = s.evicted, content_chars = s.chars "
    "FROM ("
    "  SELECT ws.id,"
    "    CAST(count(i.id) FILTER (WHERE i.status <> 'evicted') AS integer) AS live,"
    "    CAST(count(i.id) FILTER (WHERE i.status = 'evicted') AS integer) AS evicted,"
    "    CAST(COALESCE(sum(i.content_chars) FILTER (WHERE i.status <> 'evicted'), 0)"
    "      AS integer) AS chars"
    "  FROM worksets AS ws LEFT JOIN workset_items AS i ON i.workset_id = ws.id"
    "  WHERE ws.id BETWEEN CAST(:first AS uuid) AND CAST(:last AS uuid)"
    "  GROUP BY ws.id"
    ") AS s "
    "WHERE w.id = s.id AND (w.item_count, w.evicted_count, w.content_chars) "
    "IS DISTINCT FROM (s.live, s.evicted, s.chars)"
)


def reconcile_in_batches(conn: sa.engine.Connection, batch_size: int = _BATCH_SIZE) -> int:
    """Recompute every workset's counters *batch_size* worksets at a time.

    Returns how many worksets were corrected. Meant for an autocommit
    connection, where every statement is its own transaction:
    ``lock_timeout`` is therefore set for the session and put back afterwards
    (``SET LOCAL`` would not outlive the statement).
    """
    previous = conn.execute(sa.text("SELECT current_setting('lock_timeout')")).scalar()
    conn.execute(sa.text("SELECT set_config('lock_timeout', :t, false)"), {"t": DEFAULT_LOCK_TIMEOUT})
    corrected = 0
    after: str | None = None
    try:
        while True:
            ids = [str(row[0]) for row in conn.execute(_NEXT_IDS, {"after": after, "n": batch_size})]
            if not ids:
                return corrected
            result = conn.execute(_RECONCILE, {"first": ids[0], "last": ids[-1]})
            corrected += result.rowcount or 0
            after = ids[-1]
    finally:
        conn.execute(sa.text("SELECT set_config('lock_timeout', :t, false)"), {"t": previous})


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not (inspector.has_table("worksets") and inspector.has_table("workset_items")):
        return
    with op.get_context().autocommit_block():
        reconcile_in_batches(op.get_bind())


def downgrade() -> None:
    # A data normalization: the drifted values carried no information, so
    # there is nothing to restore.
    pass
