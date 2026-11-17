"""a flow as evaluation subject (#8257)

An evaluation's subject is an agent or a flow: ``evaluation_agents`` and
``evaluation_runs`` gain ``flow_id`` (``ON DELETE CASCADE``, like ``agent_id``),
``agent_id`` becomes nullable, and a CHECK keeps exactly one of the two set.
``evaluation_runs.flow_version_number`` is the flow version a run snapshots, as
``persona_version_number`` is an agent's.

Both tables are existing and can be hot (the run dispatcher polls
``evaluation_runs``), so every statement follows the lock-safe recipe:
idempotency probe and early return, ``terminate_lock_holders``,
``set_lock_timeout``, guarded DDL. As in ``w3wsp3314003``, foreign keys and
CHECKs are added ``NOT VALID`` then validated (SHARE UPDATE EXCLUSIVE, no
blocking scan), and the indexes are built in place. ``DROP NOT NULL`` is
metadata-only.

Downgrade deletes the flow-subject rows (they cannot exist before this
revision), then restores ``NOT NULL`` through a validated CHECK, so ``SET NOT
NULL`` skips its scan.

Revision ID: c4cnc8254042
Revises: c4cnc8254041
Create Date: 2026-11-03 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import (
    create_index_in_place,
    set_lock_timeout,
    terminate_lock_holders,
)

revision: str = "c4cnc8254042"
down_revision: str | Sequence[str] | None = "c4cnc8254041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("evaluation_agents", "evaluation_runs")
_SUBJECT_CHECK = "num_nonnulls(agent_id, flow_id) = 1"


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


def _is_nullable(conn, table: str, column: str) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
        == "YES"
    )


def _has_constraint(conn, name: str) -> bool:
    return bool(
        conn.execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}).scalar()
    )


def _constraint_validated(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT convalidated FROM pg_constraint WHERE conname = :n"), {"n": name}
        ).scalar()
    )


def _locked(conn, table: str) -> None:
    terminate_lock_holders(conn, table)
    set_lock_timeout(conn)


def _fk_name(table: str) -> str:
    """The name ``create_all`` gives the model's unnamed ForeignKey."""
    return f"{table}_flow_id_fkey"


def _check_name(table: str) -> str:
    return f"ck_{table}_one_subject"


def upgrade() -> None:
    conn = op.get_bind()

    for table in _TABLES:
        if not _has_column(conn, table, "flow_id"):
            _locked(conn, table)
            op.add_column(table, sa.Column("flow_id", postgresql.UUID(as_uuid=True), nullable=True))
        if not _has_constraint(conn, _fk_name(table)):
            _locked(conn, table)
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {_fk_name(table)} FOREIGN KEY "
                "(flow_id) REFERENCES flows(id) ON DELETE CASCADE NOT VALID"
            )
        if not _constraint_validated(conn, _fk_name(table)):
            op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {_fk_name(table)}")
        if not _is_nullable(conn, table, "agent_id"):
            _locked(conn, table)
            op.alter_column(table, "agent_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
        if not _has_constraint(conn, _check_name(table)):
            _locked(conn, table)
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {_check_name(table)} "
                f"CHECK ({_SUBJECT_CHECK}) NOT VALID"
            )
        if not _constraint_validated(conn, _check_name(table)):
            op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {_check_name(table)}")

    if not _has_column(conn, "evaluation_runs", "flow_version_number"):
        _locked(conn, "evaluation_runs")
        op.add_column("evaluation_runs", sa.Column("flow_version_number", sa.Integer(), nullable=True))

    with op.get_context().autocommit_block():
        create_index_in_place(conn, "ix_evaluation_runs_flow_id", "evaluation_runs", "(flow_id)")
        create_index_in_place(
            conn,
            "uq_eval_flow",
            "evaluation_agents",
            "(evaluation_id, flow_id) WHERE flow_id IS NOT NULL",
            unique=True,
        )


def downgrade() -> None:
    conn = op.get_bind()

    for index, table in (("uq_eval_flow", "evaluation_agents"), ("ix_evaluation_runs_flow_id", "evaluation_runs")):
        _locked(conn, table)
        op.execute(f"DROP INDEX IF EXISTS {index}")

    if _has_column(conn, "evaluation_runs", "flow_version_number"):
        _locked(conn, "evaluation_runs")
        op.drop_column("evaluation_runs", "flow_version_number")

    for table in _TABLES:
        if not _has_column(conn, table, "flow_id"):
            continue
        # A flow subject cannot exist before this revision: its runs (and their
        # results, by CASCADE) and its links go with it.
        op.execute(f"DELETE FROM {table} WHERE agent_id IS NULL")  # noqa: S608 - table from _TABLES
        if _has_constraint(conn, _check_name(table)):
            _locked(conn, table)
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {_check_name(table)}")
        if _is_nullable(conn, table, "agent_id"):
            not_null = f"ck_{table}_agent_id_not_null"
            if not _has_constraint(conn, not_null):
                _locked(conn, table)
                op.execute(
                    f"ALTER TABLE {table} ADD CONSTRAINT {not_null} CHECK (agent_id IS NOT NULL) NOT VALID"
                )
            op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {not_null}")
            # The validated CHECK proves the column has no NULL: no scan here.
            _locked(conn, table)
            op.alter_column(table, "agent_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {not_null}")
        if _has_constraint(conn, _fk_name(table)):
            _locked(conn, table)
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {_fk_name(table)}")
        _locked(conn, table)
        op.drop_column(table, "flow_id")
