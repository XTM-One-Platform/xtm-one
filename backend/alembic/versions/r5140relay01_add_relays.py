"""add relays: pull-based on-premise egress

Four new tables and one nullable column on two existing ones:

* ``relay_groups``          - a network zone; three-tier visibility.
* ``relays``                - one enrolled relay process (hashed ``rly-`` key).
* ``relay_enrollment_keys`` - hashed ``rlenr-`` keys, bound to a group.
* ``relay_request_logs``    - metadata of each relayed request (never a
  header value or a body).
* ``integrations.relay_group_id`` / ``mcp_servers.relay_group_id`` - the group
  their HTTP traffic is routed through, NULL = direct.

Purely additive: old replicas during a rolling deploy ignore the new tables
and the new columns (``SELECT`` lists are explicit in SQLAlchemy, and a NULL
``relay_group_id`` is the direct egress they already do).

The two foreign keys onto ``relay_groups`` are added in the lock-light two
steps of ``a1paus3198`` (``NOT VALID`` then ``VALIDATE``): the column was
created NULL two statements earlier, so the scan is trivial, but the ADD would
otherwise hold ``SHARE ROW EXCLUSIVE`` on ``integrations`` while it scans.
``ON DELETE RESTRICT``: a group in use cannot be deleted out from under the
resources routed to it (the API answers 409 first, this is the backstop).

The partial indexes on ``relay_group_id`` serve the RESTRICT check on a group
deletion and the group detail's "Routed resources" tab; built CONCURRENTLY.

Revision ID: r5140relay01
Revises: k5135apikeys
Create Date: 2026-10-02 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import (
    create_index_concurrently,
    set_lock_timeout,
    terminate_lock_holders,
)

revision: str = "r5140relay01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "k5135apikeys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, fk name, index name) for each routed resource table.
_ROUTED_TABLES: tuple[tuple[str, str, str], ...] = (
    (
        "integrations",
        "fk_integrations_relay_group_id_relay_groups",
        "ix_integrations_relay_group_id",
    ),
    (
        "mcp_servers",
        "fk_mcp_servers_relay_group_id_relay_groups",
        "ix_mcp_servers_relay_group_id",
    ),
)


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
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


def _constraint_state(conn, name: str) -> str | None:
    """``"valid"`` / ``"not_valid"`` for *name*, or ``None`` when absent."""
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


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _timestamp(name: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _create_tables(conn) -> None:
    if not _has_table(conn, "relay_groups"):
        # The FK to `users` installs its triggers there under SHARE ROW
        # EXCLUSIVE, which an in-flight write to `users` blocks: clear the
        # holders, then the belt makes a racing one fail fast (55P03).
        terminate_lock_holders(conn, "users")
        set_lock_timeout(conn)
        op.create_table(
            "relay_groups",
            _uuid_pk(),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("slug", sa.String(200), nullable=False),
            sa.Column("zone", sa.String(200), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("allowed_destinations", postgresql.JSON(), nullable=True),
            sa.Column("tags", postgresql.JSON(), nullable=True),
            sa.Column("is_company_managed", sa.Boolean(), nullable=True),
            sa.Column("shared_group_ids", postgresql.JSON(), nullable=True),
            sa.Column("shared_group_permissions", postgresql.JSON(), nullable=True),
            sa.Column("shared_user_ids", postgresql.JSON(), nullable=True),
            sa.Column("shared_user_permissions", postgresql.JSON(), nullable=True),
            sa.Column(
                "access_restricted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            _timestamp("created_at"),
            _timestamp("updated_at"),
            if_not_exists=True,
        )
        op.create_index(
            "ix_relay_groups_user_id", "relay_groups", ["user_id"], if_not_exists=True
        )
        op.create_index(
            "ix_relay_groups_is_company_managed",
            "relay_groups",
            ["is_company_managed"],
            if_not_exists=True,
        )
        op.create_index(
            "ix_relay_groups_slug",
            "relay_groups",
            ["slug"],
            unique=True,
            if_not_exists=True,
        )

    if not _has_table(conn, "relays"):
        terminate_lock_holders(conn, "users")
        set_lock_timeout(conn)
        op.create_table(
            "relays",
            _uuid_pk(),
            sa.Column(
                "group_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("relay_groups.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "enrolled_by_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("platform", sa.String(20), nullable=False),
            sa.Column("hostname", sa.String(255), nullable=True),
            sa.Column("machine_id", sa.String(128), nullable=True),
            sa.Column("key_prefix", sa.String(20), nullable=False),
            sa.Column("key_hash", sa.Text(), nullable=False),
            sa.Column("host_facts", postgresql.JSON(), nullable=True),
            sa.Column("agent_version", sa.String(40), nullable=True),
            sa.Column("protocol_version", sa.Integer(), nullable=True),
            sa.Column("capabilities", postgresql.JSON(), nullable=True),
            sa.Column("declared_destinations", postgresql.JSON(), nullable=True),
            sa.Column(
                "allows_all_destinations",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "max_concurrent_requests",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("32"),
            ),
            sa.Column("status", sa.String(20), nullable=False),
            _timestamp("last_seen_at"),
            sa.Column("transport", sa.String(20), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column(
                "request_count",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            _timestamp("enrolled_at"),
            _timestamp("created_at"),
            _timestamp("updated_at"),
            if_not_exists=True,
        )
        op.create_index("ix_relays_group_id", "relays", ["group_id"], if_not_exists=True)
        op.create_index("ix_relays_key_prefix", "relays", ["key_prefix"], if_not_exists=True)
        op.create_index(
            "ix_relays_group_machine",
            "relays",
            ["group_id", "machine_id"],
            if_not_exists=True,
        )

    if not _has_table(conn, "relay_enrollment_keys"):
        terminate_lock_holders(conn, "users")
        set_lock_timeout(conn)
        op.create_table(
            "relay_enrollment_keys",
            _uuid_pk(),
            sa.Column(
                "group_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("relay_groups.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("key_prefix", sa.String(20), nullable=False),
            sa.Column("key_hash", sa.Text(), nullable=False),
            sa.Column("max_uses", sa.Integer(), nullable=True),
            sa.Column(
                "use_count", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            _timestamp("expires_at"),
            _timestamp("last_used_at"),
            _timestamp("created_at"),
            if_not_exists=True,
        )
        op.create_index(
            "ix_relay_enrollment_keys_group_id",
            "relay_enrollment_keys",
            ["group_id"],
            if_not_exists=True,
        )
        op.create_index(
            "ix_relay_enrollment_keys_key_prefix",
            "relay_enrollment_keys",
            ["key_prefix"],
            if_not_exists=True,
        )

    if not _has_table(conn, "relay_request_logs"):
        set_lock_timeout(conn)
        op.create_table(
            "relay_request_logs",
            _uuid_pk(),
            sa.Column(
                "group_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("relay_groups.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "relay_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("relays.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("source_type", sa.String(20), nullable=True),
            sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("method", sa.String(10), nullable=False),
            sa.Column("scheme", sa.String(10), nullable=False),
            sa.Column("host", sa.String(255), nullable=False),
            sa.Column("port", sa.Integer(), nullable=True),
            sa.Column("path", sa.String(1000), nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=True),
            sa.Column("error_code", sa.String(40), nullable=True),
            sa.Column("bytes_sent", sa.BigInteger(), nullable=False),
            sa.Column("bytes_received", sa.BigInteger(), nullable=False),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            _timestamp("created_at"),
            if_not_exists=True,
        )
        op.create_index(
            "ix_relay_request_logs_group_created",
            "relay_request_logs",
            ["group_id", "created_at"],
            if_not_exists=True,
        )
        op.create_index(
            "ix_relay_request_logs_created_at",
            "relay_request_logs",
            ["created_at"],
            if_not_exists=True,
        )


def _add_routing_column(conn, table: str, fk_name: str) -> bool:
    """Add ``relay_group_id`` + its RESTRICT FK to *table*; False when skipped."""
    if not _has_table(conn, table) or not _has_table(conn, "relay_groups"):
        return False
    if not _has_column(conn, table, "relay_group_id"):
        # ADD COLUMN with no default: metadata-only on PG 11+.
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(
            sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS relay_group_id UUID")
        )

    state = _constraint_state(conn, fk_name)
    if state is None:
        # BOTH tables: the constraint locks the referenced table too.
        terminate_lock_holders(conn, table)
        terminate_lock_holders(conn, "relay_groups")
        set_lock_timeout(conn)
        op.execute(
            sa.text(
                f"ALTER TABLE {table} ADD CONSTRAINT {fk_name} "
                "FOREIGN KEY (relay_group_id) REFERENCES relay_groups(id) "
                "ON DELETE RESTRICT NOT VALID"
            )
        )
        state = "not_valid"
    if state == "not_valid":
        # SHARE UPDATE EXCLUSIVE: does not block reads or writes.
        op.execute(sa.text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {fk_name}"))
    return True


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "users"):
        return

    _create_tables(conn)

    added = [
        (table, index)
        for table, fk_name, index in _ROUTED_TABLES
        if _add_routing_column(conn, table, fk_name)
    ]

    # CONCURRENTLY must run outside a transaction and must not inherit the
    # lock_timeout belt above.
    with op.get_context().autocommit_block():
        for table, index in added:
            create_index_concurrently(
                conn,
                index,
                table,
                "(relay_group_id) WHERE relay_group_id IS NOT NULL",
            )


def downgrade() -> None:
    conn = op.get_bind()

    with op.get_context().autocommit_block():
        for _table, _fk, index in _ROUTED_TABLES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index}")

    for table, fk_name, _index in _ROUTED_TABLES:
        if not _has_table(conn, table):
            continue
        if _constraint_state(conn, fk_name) is not None:
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            op.execute(sa.text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {fk_name}"))
        if _has_column(conn, table, "relay_group_id"):
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            op.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS relay_group_id"))

    for name in (
        "relay_request_logs",
        "relay_enrollment_keys",
        "relays",
        "relay_groups",
    ):
        if _has_table(conn, name):
            # Dropping a table removes its FK triggers from `users` under an
            # ACCESS EXCLUSIVE lock there: the belt makes a busy `users` fail
            # fast instead of queueing behind it.
            set_lock_timeout(conn)
            op.drop_table(name, if_exists=True)
