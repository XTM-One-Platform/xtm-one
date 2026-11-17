"""add feedback_criteria and annotation_scores (per-criterion rating scores)

Purely additive: two new tables, no change to any existing column, so replicas
still on the previous version ignore tables they do not know about. Nothing is
backfilled: no rating carried a per-criterion score before.

Lock profile: both tables are new, so nothing can hold a lock on them and their
indexes are built non-concurrently, in the same transaction. ``feedback_criteria``
references nothing. ``annotation_scores`` references ``annotations``,
``feedback_criteria`` (both brand new) and ``message_feedback``: its foreign keys
install triggers there, which takes a SHARE ROW EXCLUSIVE lock on each. That
lock waits only for a transaction that WROTE one of those tables, never for a
reader, so the upgrade runs under the lock_timeout belt alone (a contended
attempt fails fast, 55P03, and the startup runner retries) - terminating the
lock holders would kill every in-flight session that merely read a chat
rating, for a lock it does not block (backend/AGENTS.md > "Database migrations
(safety)", rule 5). The downgrade is different: DROP TABLE takes an ACCESS
EXCLUSIVE lock on the referenced tables to remove the triggers, which readers
do block, so it runs the full pre-flight.

Revision ID: n1041crit001
Revises: n1040annot01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "n1041crit001"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "n1040annot01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CRITERIA = "feedback_criteria"
_SCORES = "annotation_scores"
#: The tables the score foreign keys lock to install (and remove) their triggers.
_SCORE_REFERENCED_TABLES = ("annotations", "message_feedback", _CRITERIA)

#: The same definition as ``AnnotationScore.__table_args__`` (the create_all path).
_SCORE_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_annotation_scores_criterion_id", ["criterion_id"]),
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


def upgrade() -> None:
    conn = op.get_bind()

    # Idempotency probes: fresh installs run create_all + stamp head (this body
    # never executes), and the migrations CI job re-runs the revision against
    # an already-upgraded database. Every table and index is probed on its own,
    # so a partial state converges too.
    if not _has_table(conn, _CRITERIA):
        op.create_table(
            _CRITERIA,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("key", sa.String(64), nullable=False),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("type", sa.String(20), nullable=False),
            sa.Column("weight", sa.Float(), nullable=False, server_default=sa.text("1.0")),
            sa.Column("options", postgresql.JSON(), nullable=True),
            sa.Column("min_value", sa.Float(), nullable=True),
            sa.Column("max_value", sa.Float(), nullable=True),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint("key", name="uq_feedback_criteria_key"),
            sa.CheckConstraint(
                "type IN ('categorical', 'numeric', 'scale')",
                name="ck_feedback_criteria_type_valid",
            ),
            sa.CheckConstraint("weight >= 0", name="ck_feedback_criteria_weight_non_negative"),
            if_not_exists=True,
        )

    if not _has_table(conn, _SCORES):
        # The belt alone: see the lock profile above.
        set_lock_timeout(conn)
        op.create_table(
            _SCORES,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "annotation_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("annotations.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column(
                "message_feedback_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("message_feedback.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column(
                "criterion_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("feedback_criteria.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("value_numeric", sa.Float(), nullable=True),
            sa.Column("value_text", sa.String(100), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.CheckConstraint(
                "num_nonnulls(annotation_id, message_feedback_id) = 1",
                name="ck_annotation_scores_one_rating",
            ),
            sa.CheckConstraint(
                "num_nonnulls(value_numeric, value_text) = 1",
                name="ck_annotation_scores_one_value",
            ),
            sa.UniqueConstraint(
                "annotation_id",
                "criterion_id",
                name="uq_annotation_scores_annotation_criterion",
            ),
            sa.UniqueConstraint(
                "message_feedback_id",
                "criterion_id",
                name="uq_annotation_scores_feedback_criterion",
            ),
            if_not_exists=True,
        )

    missing = [(name, columns) for name, columns in _SCORE_INDEXES if not _has_index(conn, name)]
    if missing:
        # Instant on the table created above; the belt matters for a table left
        # without an index, which may already be taking scores (the build's
        # SHARE lock waits for a writer, never for a reader).
        set_lock_timeout(conn)
        for name, columns in missing:
            op.create_index(name, _SCORES, columns, if_not_exists=True)


def downgrade() -> None:
    conn = op.get_bind()

    # Scores first: they reference the criteria. Dropping them takes an ACCESS
    # EXCLUSIVE lock on the tables they reference, to remove the foreign key
    # triggers: readers block that, so the canonical pre-flight runs first.
    drops = ((_SCORES, (_SCORES, *_SCORE_REFERENCED_TABLES)), (_CRITERIA, (_CRITERIA,)))
    for table, locked in drops:
        if _has_table(conn, table):
            for held in locked:
                terminate_lock_holders(conn, held)
            set_lock_timeout(conn)
            op.drop_table(table, if_exists=True)
