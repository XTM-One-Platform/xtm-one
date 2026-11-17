"""reactions as feedback: platform raters, the reaction setting, the answer lookup

A reaction a person puts on an agent account's answer in Teams becomes a rating
of that answer, stored as an ``annotations`` row of target type
``agent_account_reply``. The person may have no XTM One account, so:

* ``annotations.user_id`` becomes nullable and ``rater_ref`` (a keyed digest of
  the platform id, never the id) carries the rater instead; the check
  ``ck_annotations_one_rater`` keeps exactly one of the two, and the partial
  unique index ``uq_annotations_target_rater`` one rating per platform rater
  and target. ``source`` (``web`` for every existing row) and ``emoji`` say
  where and how the rating was given.
* ``agent_accounts.reaction_feedback`` holds the account's setting (NULL = the
  defaults: enabled, thumbs up positive, thumbs down negative).
* ``agent_account_subscriptions.change_type`` records what a Graph subscription
  is notified of (``created`` for every existing one), so a sync recreates the
  subscriptions that must now also hear ``updated``.
* ``agent_account_replies.agent_id`` records the agent that wrote an answer
  (no foreign key: it would lock ``agents``), the agent its ratings go to;
  ``ix_agent_account_replies_sent_message`` finds the answer a reaction is on.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - each step is probed first (columns, nullability, constraint,
  indexes) and guarded (``IF NOT EXISTS``, ``create_index_in_place``).
* Lock-safe - nullable columns and a constant-default NOT NULL column are
  metadata-only, ``DROP NOT NULL`` too; each table gets the canonical recipe
  (probe, ``terminate_lock_holders``, ``set_lock_timeout``). The check is added
  ``NOT VALID`` and validated by a separate statement, but in the same
  transaction: on the run that adds it, the scan of ``annotations`` happens
  while the ``ACCESS EXCLUSIVE`` lock of the earlier ``ALTER TABLE``s is still
  held (one row per human rating: a small table, a short scan).
  Only a re-run that finds the check ``NOT VALID`` validates it under
  ``SHARE UPDATE EXCLUSIVE`` alone. Every existing row has a ``user_id`` and no
  ``rater_ref``, so it passes. The
  indexes are built in place in autocommit blocks, never ``CONCURRENTLY``;
  the unique one is ``required`` - nothing may write a platform
  rating before it exists - and is built while no row has a ``rater_ref``.
* Reversible - the downgrade deletes the platform ratings (they have no user
  to give back to ``user_id``), restores ``NOT NULL`` and drops what was added.
  ``SET NOT NULL`` scans ``annotations`` under the ALTER's lock: one row per
  human rating, a small table, and a downgrade only.

Revision ID: r1rxn5511001
Revises: l1rtm5466001
Create Date: 2026-10-08 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import (
    create_index_in_place,
    set_lock_timeout,
    terminate_lock_holders,
)

revision: str = "r1rxn5511001"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "l1rtm5466001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ANNOTATIONS = "annotations"
_ANNOTATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("rater_ref", "VARCHAR(64)"),
    ("source", "VARCHAR(32) NOT NULL DEFAULT 'web'"),
    ("emoji", "VARCHAR(32)"),
)
_CHECK = "ck_annotations_one_rater"
_RATER_INDEX = "uq_annotations_target_rater"

_ACCOUNTS = "agent_accounts"
_SUBSCRIPTIONS = "agent_account_subscriptions"
_REPLIES = "agent_account_replies"
_REPLY_INDEX = "ix_agent_account_replies_sent_message"

#: Platform ratings the downgrade deletes per statement.
_DELETE_BATCH = 1000


def _columns(conn, table: str) -> dict[str, bool]:
    """``{column: nullable}`` of *table*."""
    return {
        row[0]: row[1] == "YES"
        for row in conn.execute(
            sa.text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        )
    }


def _constraint_state(conn) -> str | None:
    """``valid``, ``not_valid`` or ``None`` (absent) for the rater check."""
    row = conn.execute(
        sa.text(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname = :name AND conrelid = to_regclass(:table)"
        ),
        {"name": _CHECK, "table": _ANNOTATIONS},
    ).first()
    if row is None:
        return None
    return "valid" if row[0] else "not_valid"


def _add_column(conn, table: str, name: str, ddl: str) -> None:
    if name in _columns(conn, table):
        return
    terminate_lock_holders(conn, table)
    set_lock_timeout(conn)
    conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}"))


def _upgrade_annotations(conn) -> None:
    columns = _columns(conn, _ANNOTATIONS)
    missing = [(name, ddl) for name, ddl in _ANNOTATION_COLUMNS if name not in columns]
    state = _constraint_state(conn)
    if missing or not columns.get("user_id", True) or state is None:
        terminate_lock_holders(conn, _ANNOTATIONS)
        set_lock_timeout(conn)
        for name, ddl in missing:
            conn.execute(sa.text(f"ALTER TABLE {_ANNOTATIONS} ADD COLUMN IF NOT EXISTS {name} {ddl}"))
        conn.execute(sa.text(f"ALTER TABLE {_ANNOTATIONS} ALTER COLUMN user_id DROP NOT NULL"))
        if state is None:
            conn.execute(
                sa.text(
                    f"ALTER TABLE {_ANNOTATIONS} ADD CONSTRAINT {_CHECK} "
                    "CHECK (num_nonnulls(user_id, rater_ref) = 1) NOT VALID"
                )
            )
            state = "not_valid"
    if state == "not_valid":
        # SHARE UPDATE EXCLUSIVE on its own when the check was added by an
        # earlier run; right after the ALTERs above, their ACCESS EXCLUSIVE
        # lock is still held by this transaction.
        conn.execute(sa.text(f"ALTER TABLE {_ANNOTATIONS} VALIDATE CONSTRAINT {_CHECK}"))


def upgrade() -> None:
    conn = op.get_bind()
    _upgrade_annotations(conn)
    _add_column(conn, _ACCOUNTS, "reaction_feedback", "JSON")
    _add_column(conn, _SUBSCRIPTIONS, "change_type", "VARCHAR(50) NOT NULL DEFAULT 'created'")
    _add_column(conn, _REPLIES, "agent_id", "UUID")
    with op.get_context().autocommit_block():
        create_index_in_place(
            conn,
            _RATER_INDEX,
            _ANNOTATIONS,
            "(target_type, target_id, rater_ref) WHERE rater_ref IS NOT NULL",
            unique=True,
            required=True,
        )
        create_index_in_place(
            conn,
            _REPLY_INDEX,
            _REPLIES,
            "(account_id, sent_message_id) WHERE sent_message_id IS NOT NULL",
        )


def _drop_column(conn, table: str, name: str) -> None:
    if name not in _columns(conn, table):
        return
    terminate_lock_holders(conn, table)
    set_lock_timeout(conn)
    conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {name}"))


def downgrade() -> None:
    conn = op.get_bind()
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_REPLY_INDEX}"))
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_RATER_INDEX}"))
    _drop_column(conn, _REPLIES, "agent_id")
    _drop_column(conn, _SUBSCRIPTIONS, "change_type")
    _drop_column(conn, _ACCOUNTS, "reaction_feedback")

    columns = _columns(conn, _ANNOTATIONS)
    # A platform rating has no user to put back in ``user_id``: it goes, in
    # bounded batches (its scores follow by cascade).
    while True:
        deleted = conn.execute(
            sa.text(
                "DELETE FROM annotations WHERE id IN ("
                "SELECT id FROM annotations WHERE user_id IS NULL LIMIT :batch)"
            ),
            {"batch": _DELETE_BATCH},
        ).rowcount
        if not deleted:
            break
    present = [name for name, _ddl in _ANNOTATION_COLUMNS if name in columns]
    if not present and not columns.get("user_id", False) and _constraint_state(conn) is None:
        return
    terminate_lock_holders(conn, _ANNOTATIONS)
    set_lock_timeout(conn)
    conn.execute(sa.text(f"ALTER TABLE {_ANNOTATIONS} DROP CONSTRAINT IF EXISTS {_CHECK}"))
    conn.execute(sa.text(f"ALTER TABLE {_ANNOTATIONS} ALTER COLUMN user_id SET NOT NULL"))
    for name in present:
        conn.execute(sa.text(f"ALTER TABLE {_ANNOTATIONS} DROP COLUMN IF EXISTS {name}"))
