"""add llm_provider_configs.source_integration_id

The integration an LLM provider config was populated from: a company-managed
OpenRouter integration creates (or adopts) the platform's OpenRouter provider
and keeps its API key in step (``services/openrouter_llm_provider.py``). NULL
for every existing config - they were all created in Settings - so there is no
backfill.

Lock profile: a nullable column with no default is metadata-only on PG 11+,
but the ALTER takes ACCESS EXCLUSIVE on ``llm_provider_configs``, which every
LLM call reads through the provider cache, so the canonical pre-flight runs
first: idempotency probe -> terminate_lock_holders -> set_lock_timeout ->
guarded DDL.

The foreign key is added in two steps. ``ADD CONSTRAINT ... NOT VALID`` skips
the scan (trivial anyway: the column was created NULL two statements earlier)
and ``VALIDATE CONSTRAINT`` runs under SHARE UPDATE EXCLUSIVE, which blocks
neither reads nor writes. The constraint also locks ``integrations`` (SHARE ROW
EXCLUSIVE), so lock holders are cleared on both tables. ``ON DELETE SET NULL``
matches the model: deleting the integration leaves the provider running on the
key it holds.

A partial UNIQUE index (``WHERE source_integration_id IS NOT NULL``) makes an
integration feed at most one config: the service's check before a create or an
adoption is not atomic with its insert. It also serves the referential action.
Built in place, not CONCURRENTLY: the table holds a handful of rows, so the
build is instant, and a best-effort concurrent build (which gives up on a
timeout) cannot carry a rule. ``CREATE INDEX`` takes SHARE on the table - reads
go on, writes wait the instant it takes - under the same pre-flight and
``lock_timeout`` belt as the ALTERs.

Revision ID: o1rtlink0001
Revises: r5140relay01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "o1rtlink0001"
down_revision: str | None = "r5140relay01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llm_provider_configs"
_COLUMN = "source_integration_id"
_FK_NAME = "fk_llm_provider_configs_source_integration_id_integrations"
_UNIQUE_INDEX = "uq_llm_provider_configs_source_integration_id"


def _has_index(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM pg_indexes "
                "WHERE schemaname = current_schema() AND indexname = :n"
            ),
            {"n": name},
        ).scalar()
    )


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
    """``"valid"`` / ``"not_valid"`` for *name*, or ``None`` when absent.

    A replica that crashed between the two steps resumes at the validation
    instead of adding the constraint a second time.
    """
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


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return

    if not _has_column(conn, _TABLE, _COLUMN):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(sa.text(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} UUID"))

    if not _has_table(conn, "integrations"):
        return

    state = _constraint_state(conn, _FK_NAME)
    if state is None:
        terminate_lock_holders(conn, _TABLE)
        terminate_lock_holders(conn, "integrations")
        set_lock_timeout(conn)
        op.execute(
            sa.text(
                f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_FK_NAME} "
                f"FOREIGN KEY ({_COLUMN}) REFERENCES integrations(id) "
                "ON DELETE SET NULL NOT VALID"
            )
        )
        state = "not_valid"

    if state == "not_valid":
        op.execute(sa.text(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_FK_NAME}"))

    if not _has_index(conn, _UNIQUE_INDEX):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(
            sa.text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {_UNIQUE_INDEX} ON {_TABLE} "
                f"({_COLUMN}) WHERE {_COLUMN} IS NOT NULL"
            )
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE) or not _has_column(conn, _TABLE, _COLUMN):
        return
    # Dropping the column drops its constraint with it; the constraint locks
    # ``integrations`` too, so holders are cleared there as well.
    terminate_lock_holders(conn, _TABLE)
    if _has_table(conn, "integrations"):
        terminate_lock_holders(conn, "integrations")
    set_lock_timeout(conn)
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_UNIQUE_INDEX}"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}"))
