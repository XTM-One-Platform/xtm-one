"""move the persona rewrites into improvement_proposals (#8256)

The persona self-improvement loop (#1044) becomes the ``persona`` type of the
improvement proposals:

- Data: every ``persona_improvements`` row is copied in under its own id, as
  ``proposal_type='persona'`` on ``target_type='agent'``; ``evaluating``
  becomes ``watching``; the baseline run becomes its one evidence reference and
  its dedup key is the one ``improvement_proposals.dedup.dedup_key`` computes
  for it. Copied in bounded batches of source ids, ``ON CONFLICT (id) DO
  NOTHING``: a re-run copies nothing twice, and a batch already copied does
  not end the walk before the rows after it.
- ``persona_improvements`` stays: a replica still on the previous code reads
  and writes it through a rolling deploy, and dropping it here would take an
  ACCESS EXCLUSIVE lock on ``agents`` (its foreign key's triggers) that every
  reader of ``agents`` blocks. A later release drops it. A rewrite such a
  replica files or decides after the copy is not carried over.

Lock profile: row writes only, on a table no reader blocks; no DDL on upgrade.

Downgrade: the ``persona`` rows are written back into ``persona_improvements``
(``watching`` -> ``evaluating``; a row the old table cannot hold - no
evaluation, a status it does not know - is left out). Settled rows go first,
so a legacy row still open there is closed before the agent's new open one
arrives (``uq_persona_improvements_open``); an open row that would still
collide with a legacy open one is left out rather than abort. A fresh install
(``create_all`` + ``stamp head``) never had the old table: it is recreated
first, with its foreign key to ``agents`` (SHARE ROW EXCLUSIVE there, under the
lock_timeout belt alone).

Revision ID: c3cnc8254032
Revises: c3cnc8254031
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "c3cnc8254032"
down_revision: str | Sequence[str] | None = "c3cnc8254031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "improvement_proposals"
_LEGACY = "persona_improvements"
_BATCH = 500
_LEGACY_STATUSES = ("pending_approval", "evaluating", "rejected", "kept", "rolled_back", "regression_kept", "failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


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


# The key ``app.services.improvement_proposals.dedup.dedup_key`` computes for a
# persona rewrite whose one evidence is its baseline run:
# sha256("persona|agent|<agent>|" + sha256("evaluation_run:<baseline>")).
_DEDUP_SQL = (
    "encode(sha256(convert_to('persona|agent|' || p.agent_id::text || '|' || "
    "encode(sha256(convert_to('evaluation_run:' || p.baseline_run_id::text, 'UTF8')), 'hex'), "
    "'UTF8')), 'hex')"
)

# Module constants only, no input: S608 is a false positive.
_COPY_IN = f"""
INSERT INTO {_TABLE} (
    id, proposal_type, target_type, target_id, agent_id, status, dedup_key,
    base_content, proposed_content, summary,
    base_version_number, applied_version_number, rollback_version_number,
    evidence_refs, evaluation_id, baseline_run_id, candidate_run_id,
    quality_metric, baseline_score, candidate_score, delta, verdict, outcome_note,
    requested_by_user_id, decided_by_user_id,
    created_at, updated_at, decided_at, applied_at, settled_at
)
SELECT
    p.id, 'persona', 'agent', p.agent_id, p.agent_id,
    CASE WHEN p.status = 'evaluating' THEN 'watching' ELSE p.status END,
    {_DEDUP_SQL},
    p.base_persona, p.proposed_persona, p.summary,
    p.base_version_number, p.applied_version_number, p.rollback_version_number,
    jsonb_build_array(jsonb_build_object('kind', 'evaluation_run', 'id', p.baseline_run_id::text)),
    p.evaluation_id, p.baseline_run_id, p.candidate_run_id,
    p.quality_metric, p.baseline_score, p.candidate_score, p.delta, p.verdict, p.outcome_note,
    p.requested_by_user_id, p.decided_by_user_id,
    p.created_at, p.updated_at, p.decided_at,
    CASE WHEN p.applied_version_number IS NOT NULL THEN p.decided_at END,
    p.settled_at
FROM {_LEGACY} p
WHERE p.id = ANY(:ids)
ON CONFLICT (id) DO NOTHING
"""  # noqa: S608

_LEGACY_IDS = f"SELECT id FROM {_LEGACY} WHERE id > :after ORDER BY id LIMIT {_BATCH}"  # noqa: S608

# The persona rows the old table can hold.
_MOVABLE = f"""
    proposal_type = 'persona'
    AND agent_id IS NOT NULL
    AND evaluation_id IS NOT NULL
    AND baseline_run_id IS NOT NULL
    AND status IN ({", ".join(f"'{s}'" for s in ("watching", *_LEGACY_STATUSES))})
"""

_OPEN = "status IN ('pending_approval', 'watching')"
_SETTLED_IDS = (
    f"SELECT id FROM {_TABLE} WHERE {_MOVABLE} AND NOT ({_OPEN}) AND id > :after ORDER BY id LIMIT {_BATCH}"  # noqa: S608
)
_OPEN_IDS = f"SELECT id FROM {_TABLE} WHERE {_MOVABLE} AND {_OPEN} AND id > :after ORDER BY id LIMIT {_BATCH}"  # noqa: S608

_COPY_BACK = f"""
INSERT INTO {_LEGACY} (
    id, agent_id, status, evaluation_id, baseline_run_id, candidate_run_id,
    base_persona, base_version_number, proposed_persona, summary,
    applied_version_number, rollback_version_number,
    quality_metric, baseline_score, candidate_score, delta, verdict, outcome_note,
    requested_by_user_id, decided_by_user_id, created_at, updated_at, decided_at, settled_at
)
SELECT
    i.id, i.agent_id, CASE WHEN i.status = 'watching' THEN 'evaluating' ELSE i.status END,
    i.evaluation_id, i.baseline_run_id, i.candidate_run_id,
    i.base_content, i.base_version_number, i.proposed_content, i.summary,
    i.applied_version_number, i.rollback_version_number,
    i.quality_metric, i.baseline_score, i.candidate_score, i.delta, i.verdict, i.outcome_note,
    i.requested_by_user_id, i.decided_by_user_id, i.created_at, i.updated_at, i.decided_at, i.settled_at
FROM {_TABLE} i
WHERE i.id = ANY(:ids)
    -- Another open rewrite of the agent there (one an old replica filed after
    -- the copy) would violate uq_persona_improvements_open.
    AND NOT EXISTS (
        SELECT 1 FROM {_LEGACY} l
        WHERE l.agent_id = i.agent_id AND l.id <> i.id AND l.status IN ('pending_approval', 'evaluating')
            AND i.status IN ('pending_approval', 'watching')
    )
ON CONFLICT (id) DO UPDATE SET
    status = EXCLUDED.status,
    candidate_run_id = EXCLUDED.candidate_run_id,
    applied_version_number = EXCLUDED.applied_version_number,
    rollback_version_number = EXCLUDED.rollback_version_number,
    candidate_score = EXCLUDED.candidate_score,
    delta = EXCLUDED.delta,
    verdict = EXCLUDED.verdict,
    outcome_note = EXCLUDED.outcome_note,
    decided_by_user_id = EXCLUDED.decided_by_user_id,
    decided_at = EXCLUDED.decided_at,
    settled_at = EXCLUDED.settled_at,
    updated_at = EXCLUDED.updated_at
"""  # noqa: S608


def _copy(conn, next_ids: str, statement: str) -> None:
    """Run *statement* on each batch of source ids *next_ids* reads, in id order,
    until there is none left - whatever each batch copied."""
    copy = sa.text(statement).bindparams(sa.bindparam("ids", type_=postgresql.ARRAY(postgresql.UUID(as_uuid=True))))
    after = uuid.UUID(int=0)
    while True:
        ids = [row[0] for row in conn.execute(sa.text(next_ids), {"after": after})]
        if not ids:
            return
        conn.execute(copy, {"ids": ids})
        after = ids[-1]


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _LEGACY) and _has_table(conn, _TABLE):
        _copy(conn, _LEGACY_IDS, _COPY_IN)


def _create_legacy_table() -> None:
    """``persona_improvements`` as ``n1044impr001`` made it: a fresh install
    (``create_all`` + ``stamp head``) never had it."""
    op.create_table(
        _LEGACY,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("evaluation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("baseline_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("base_persona", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("base_version_number", sa.Integer(), nullable=True),
        sa.Column("proposed_persona", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("applied_version_number", sa.Integer(), nullable=True),
        sa.Column("rollback_version_number", sa.Integer(), nullable=True),
        sa.Column("quality_metric", sa.String(32), nullable=False, server_default=sa.text("'score'")),
        sa.Column("baseline_score", sa.Float(), nullable=True),
        sa.Column("candidate_score", sa.Float(), nullable=True),
        sa.Column("delta", sa.Float(), nullable=True),
        sa.Column("verdict", sa.String(16), nullable=True),
        sa.Column("outcome_note", sa.Text(), nullable=True),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_in("status", _LEGACY_STATUSES), name="ck_persona_improvements_status_valid"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_persona_improvements_agent_created", _LEGACY, ["agent_id", "created_at"], if_not_exists=True
    )
    op.create_index(
        "uq_persona_improvements_open",
        _LEGACY,
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending_approval', 'evaluating')"),
        if_not_exists=True,
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return
    if not _has_table(conn, _LEGACY):
        set_lock_timeout(conn)
        _create_legacy_table()
    _copy(conn, _SETTLED_IDS, _COPY_BACK)
    _copy(conn, _OPEN_IDS, _COPY_BACK)
