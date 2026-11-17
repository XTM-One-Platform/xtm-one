"""platform concierge: the companion's pet library (#8261)

Level 8 of the Platform Concierge (#1783): ``concierge_pets``, the pets
administrators upload, holding the spritesheet the platform re-encoded
(``services/concierge/pets.py``).

Safety contract (``dev-docs/reference/migrations-safety.md``):

* Idempotent - ``CREATE TABLE IF NOT EXISTS``.
* Lock-safe - the table is new and empty; its foreign key to ``users``
  takes a brief lock on ``users``, behind ``terminate_lock_holders`` +
  ``set_lock_timeout``.
* Reversible - the downgrade drops the table; uploaded pets go with it.

Revision ID: c8cnc8254083
Revises: c8cnc8254082
Create Date: 2026-11-05 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c8cnc8254083"
down_revision: str | Sequence[str] | None = "c8cnc8254082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PETS = "concierge_pets"


def upgrade() -> None:
    conn = op.get_bind()
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_PETS} (
            id UUID PRIMARY KEY,
            slug VARCHAR(64) NOT NULL,
            display_name VARCHAR(64) NOT NULL,
            description VARCHAR(280),
            sprite_version SMALLINT NOT NULL DEFAULT 1,
            spritesheet BYTEA NOT NULL,
            spritesheet_sha256 VARCHAR(64) NOT NULL,
            created_by_user_id UUID REFERENCES users (id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_concierge_pets_slug UNIQUE (slug)
        )
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_PETS}")
