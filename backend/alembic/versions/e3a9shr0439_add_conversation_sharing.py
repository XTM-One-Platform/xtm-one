"""add conversation sharing

Lets a conversation owner widen access without dropping RBAC:

  - new table ``conversation_shares`` — one row per grant, naming either a
    user or a group (exactly one, enforced by CHECK). Real FKs with
    ``ON DELETE CASCADE`` on the principal, so a deleted user or group takes
    its grants with it instead of leaving orphaned rows that a read-side
    filter would have to remember to exclude.
  - new column ``conversations.link_access`` — ``private`` (default) or
    ``org_read``, the "anyone on the platform with the link may read" tier.
    Constant server default, so the ADD COLUMN is metadata-only on PG 11+.

Lock safety: ``CREATE TABLE`` on a brand-new relation contends with nothing
(no other session can hold a lock on a table that does not exist yet), so it
needs no belt. The ``ALTER TABLE conversations`` does — ``conversations`` is
one of the hottest tables on the platform — and follows the canonical recipe:
idempotency probe -> terminate_lock_holders -> set_lock_timeout -> guarded
DDL. Every index is built ``CONCURRENTLY`` inside an ``autocommit_block``,
outside the lock_timeout belt.

Revision ID: e3a9shr0439
Revises: e3t9tpl2636
Create Date: 2026-07-30 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "e3a9shr0439"
# Re-pointed twice, both times because a sibling PR landed on the revision
# this one was chained to, and Alembic refuses to start with two heads:
#   1. ``c2f7sk1ll2628`` (this branch's fork point) -> ``f6a2oash0001``;
#   2. ``f6a2oash0001`` -> ``e3t9tpl2636`` (merged first), because
#      the template-container migration and this one both chained onto ``f6a2oash0001``.
# The two are independent — the template-container migration creates ``template_containers`` and adds
# columns to ``file_attachments``, this one creates ``conversation_shares``
# and adds ``conversations.link_access`` — so serialising them in merge order
# is behaviour-preserving.
down_revision: str | None = "e3t9tpl2636"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHARES_TABLE = "conversation_shares"
_CONVERSATIONS_TABLE = "conversations"
_LINK_ACCESS_COLUMN = "link_access"

#: Match the names ``create_all`` produces on a fresh install (where this body
#: never runs), so both install paths end up with identically-named objects and
#: a future migration can drop or alter them by one name.
_INDEXES: tuple[tuple[str, str], ...] = (
    (
        "ix_conversation_shares_conversation_id",
        f"{_SHARES_TABLE} (conversation_id)",
    ),
    (
        "ix_conversation_shares_shared_with_user_id",
        f"{_SHARES_TABLE} (shared_with_user_id)",
    ),
    (
        "ix_conversation_shares_shared_with_group_id",
        f"{_SHARES_TABLE} (shared_with_group_id)",
    ),
    # Partial uniques: the unused principal column is NULL on every row, and
    # NULLs never conflict in a plain UNIQUE — a composite unique over both
    # would let the same (conversation, user) pair be inserted without limit.
    (
        "uq_conversation_shares_user",
        f"{_SHARES_TABLE} (conversation_id, shared_with_user_id) "
        "WHERE shared_with_user_id IS NOT NULL",
    ),
    (
        "uq_conversation_shares_group",
        f"{_SHARES_TABLE} (conversation_id, shared_with_group_id) "
        "WHERE shared_with_group_id IS NOT NULL",
    ),
)
_UNIQUE_INDEX_NAMES = frozenset(
    {"uq_conversation_shares_user", "uq_conversation_shares_group"}
)


def _column_exists(conn, table: str, column: str) -> bool:
    # Scope the probe to the active schema — information_schema.columns spans
    # ALL schemas, so a same-named table elsewhere must not make the probe
    # skip the ADD COLUMN in the schema being migrated.
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def _constraint_exists(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
            {"name": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    # ------------------------------------------------------------------
    # 1. conversation_shares
    #
    # No lock belt: a CREATE TABLE IF NOT EXISTS on a relation that does not
    # exist yet cannot contend with any other session, and when it *does*
    # already exist the statement is a no-op that takes no lock either.
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_SHARES_TABLE} (
            id UUID PRIMARY KEY,
            conversation_id UUID NOT NULL
                REFERENCES conversations(id) ON DELETE CASCADE,
            shared_with_user_id UUID
                REFERENCES users(id) ON DELETE CASCADE,
            shared_with_group_id UUID
                REFERENCES groups(id) ON DELETE CASCADE,
            permission VARCHAR(20) NOT NULL DEFAULT 'read',
            created_by UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_conversation_shares_exactly_one_principal
                CHECK (num_nonnulls(shared_with_user_id, shared_with_group_id) = 1),
            CONSTRAINT ck_conversation_shares_permission_valid
                CHECK (permission IN ('read'))
        )
        """
    )

    # ------------------------------------------------------------------
    # 2. conversations.link_access
    # ------------------------------------------------------------------
    if not _column_exists(conn, _CONVERSATIONS_TABLE, _LINK_ACCESS_COLUMN):
        # Pre-flight: kill same-role lock holders so the ALTER is instant.
        terminate_lock_holders(conn, _CONVERSATIONS_TABLE)
        # Safety belt: fail fast (55P03) instead of wedging the lock queue on
        # one of the busiest tables on the platform.
        set_lock_timeout(conn)
        # Constant default => metadata-only, no table rewrite (PG 11+).
        op.execute(
            f"ALTER TABLE {_CONVERSATIONS_TABLE} "
            f"ADD COLUMN IF NOT EXISTS {_LINK_ACCESS_COLUMN} "
            "VARCHAR(20) NOT NULL DEFAULT 'private'"
        )

    if _column_exists(
        conn, _CONVERSATIONS_TABLE, _LINK_ACCESS_COLUMN
    ) and not _constraint_exists(conn, "ck_conversations_link_access_valid"):
        terminate_lock_holders(conn, _CONVERSATIONS_TABLE)
        set_lock_timeout(conn)
        # ``NOT VALID`` skips the full-table validation scan, so the ALTER
        # stays metadata-only. Every existing row already holds the server
        # default, and every new row is checked from here on — the historical
        # rows this skips validating cannot violate the constraint.
        op.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_conversations_link_access_valid'
                ) THEN
                    ALTER TABLE conversations
                        ADD CONSTRAINT ck_conversations_link_access_valid
                        CHECK (link_access IN ('private', 'org_read'))
                        NOT VALID;
                END IF;
            END $$;
            """
        )

    # ------------------------------------------------------------------
    # 3. Indexes — CONCURRENTLY, outside any transaction, and deliberately
    #    NOT under a lock_timeout (the build legitimately waits on snapshots).
    # ------------------------------------------------------------------
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = 0")
        for name, definition in _INDEXES:
            unique = "UNIQUE " if name in _UNIQUE_INDEX_NAMES else ""
            op.execute(
                f"CREATE {unique}INDEX CONCURRENTLY IF NOT EXISTS {name} "
                f"ON {definition}"
            )


def downgrade() -> None:
    conn = op.get_bind()

    with op.get_context().autocommit_block():
        for name, _definition in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")

    # Dropping the table takes its own constraints and FKs with it.
    op.execute(f"DROP TABLE IF EXISTS {_SHARES_TABLE}")

    if _constraint_exists(conn, "ck_conversations_link_access_valid"):
        terminate_lock_holders(conn, _CONVERSATIONS_TABLE)
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE {_CONVERSATIONS_TABLE} "
            "DROP CONSTRAINT IF EXISTS ck_conversations_link_access_valid"
        )

    if _column_exists(conn, _CONVERSATIONS_TABLE, _LINK_ACCESS_COLUMN):
        terminate_lock_holders(conn, _CONVERSATIONS_TABLE)
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE {_CONVERSATIONS_TABLE} "
            f"DROP COLUMN IF EXISTS {_LINK_ACCESS_COLUMN}"
        )
