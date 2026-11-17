"""add a2a_contexts and point a2a_tasks.context_id at it

``contextId`` was a per-task random value that grouped nothing, so a transfer
could not own the conversation the protocol says it owns. This gives it a row:
the client that may continue it, the agent that answers it now, and the card it
was created under.

The column already exists and already holds a value per task, so the foreign key
goes on over populated data. Three steps, in this order:

1. create the table;
2. backfill one context per distinct existing ``context_id`` — preserving the
   identifier, because a partner may be holding it — taking the client and
   exposure from any task under it and the agent from that exposure;
3. add the constraint ``NOT VALID``, then ``VALIDATE`` it separately.

Step 3 is split because ``ADD CONSTRAINT`` with validation scans the whole table
under ACCESS EXCLUSIVE, while ``VALIDATE CONSTRAINT`` takes SHARE UPDATE
EXCLUSIVE and blocks neither reads nor writes. New rows are checked from the
moment the constraint exists either way.

Revision ID: a2a7context01
Revises: a2a6delegidx
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a2a7context01"
down_revision: str | None = "a2a6delegidx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "a2a_contexts"
_TASKS = "a2a_tasks"
_FK = "a2a_tasks_context_id_fkey"


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


def _has_constraint(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint WHERE conname = :n "
                "AND connamespace = current_schema()::regnamespace"
            ),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, _TABLE):
        op.create_table(
            _TABLE,
            sa.Column(
                "id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True
            ),
            sa.Column("client_id", sa.String(255), nullable=False),
            sa.Column(
                "responsible_agent_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "exposure_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("a2a_exposures.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )
        op.create_index(
            "ix_a2a_contexts_client_id_ctx", _TABLE, ["client_id", "id"]
        )
        op.create_index("ix_a2a_contexts_client_id", _TABLE, ["client_id"])
        op.create_index(
            "ix_a2a_contexts_responsible_agent_id", _TABLE, ["responsible_agent_id"]
        )
        op.create_index("ix_a2a_contexts_exposure_id", _TABLE, ["exposure_id"])

    if not _has_table(conn, _TASKS):
        return

    # Preserve the identifier a partner may already hold. Bounded by the number
    # of existing external tasks, which is small — this surface is per-partner,
    # not per-user.
    op.execute(
        sa.text(
            f"""
            INSERT INTO {_TABLE}
                (id, client_id, responsible_agent_id, exposure_id,
                 created_at, updated_at)
            SELECT DISTINCT ON (t.context_id)
                   t.context_id, t.client_id, e.agent_id, t.exposure_id,
                   now(), now()
              FROM {_TASKS} t
              JOIN a2a_exposures e ON e.id = t.exposure_id
             WHERE t.context_id IS NOT NULL
             ORDER BY t.context_id, t.created_at
            ON CONFLICT (id) DO NOTHING
            """  # noqa: S608 - no input: every name is a module constant
        )
    )

    if not _has_constraint(conn, _FK):
        terminate_lock_holders(conn, _TASKS)
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE {_TASKS} ADD CONSTRAINT {_FK} "
            f"FOREIGN KEY (context_id) REFERENCES {_TABLE}(id) "
            "ON DELETE CASCADE NOT VALID"
        )
    # Outside the lock belt above: SHARE UPDATE EXCLUSIVE, blocking nothing.
    op.execute(f"ALTER TABLE {_TASKS} VALIDATE CONSTRAINT {_FK}")


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _TASKS) and _has_constraint(conn, _FK):
        terminate_lock_holders(conn, _TASKS)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TASKS} DROP CONSTRAINT {_FK}")
    if _has_table(conn, _TABLE):
        op.drop_table(_TABLE)
