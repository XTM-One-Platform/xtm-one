"""add the Intelligence policy's stored PIRs, one minimum score each (#2435)

One new table, ``organization_pirs``: the OpenCTI PIRs XTM One reads in the
background, with the organization's policy on each (question, owner, review
cadence, primary assets served, minimum score, enabled in signals).

Carried over, no breaking change: every PIR of the primary profile's
``pir_ids`` becomes a stored PIR enabled in signals, with its ``pir_details``,
its saved name, and the profile's former threat-activity minimum
(``prioritization_policy.pir_min_score_threat``, 75 when unset) as its own
minimum score. The source integration is left empty: the first refresh adopts
the row. The organization profile's PIR sync (section ``pirs``) is retired:
its rows are removed. Bounded: one profile, at most a few hundred PIRs.

Lock profile: the foreign key to ``integrations`` installs a trigger there,
so its holders are cleared and the lock timeout set before the guarded
``CREATE TABLE``.

Revision ID: i2435pirpolicy01
Revises: p2202fleetrd01
Create Date: 2026-11-26 01:15:00.000000
"""

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "i2435pirpolicy01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "p2202fleetrd01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "organization_pirs"
_CADENCES = {"", "weekly", "monthly", "quarterly", "half_yearly", "yearly"}


def _has_table(conn: sa.engine.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def carried_rows(profile: dict) -> list[dict]:
    """The stored PIR rows a saved profile carries (pure; tested by tests/test_organization_pirs.py)."""
    policy = profile.get("prioritization_policy") if isinstance(profile.get("prioritization_policy"), dict) else {}
    threat = policy.get("pir_min_score_threat", 75)
    min_score = threat if isinstance(threat, int) and not isinstance(threat, bool) and 0 <= threat <= 100 else 75
    names = profile.get("pir_names") if isinstance(profile.get("pir_names"), dict) else {}
    details = profile.get("pir_details") if isinstance(profile.get("pir_details"), dict) else {}
    rows: list[dict] = []
    seen: set[str] = set()
    for raw in profile.get("pir_ids") or []:
        pir_id = str(raw or "").strip()[:100]
        if not pir_id or pir_id in seen:
            continue
        seen.add(pir_id)
        detail = details.get(pir_id) if isinstance(details.get(pir_id), dict) else {}
        cadence = str(detail.get("review_cadence") or "")
        assets = detail.get("primary_assets") if isinstance(detail.get("primary_assets"), list) else []
        rows.append(
            {
                "pir_id": pir_id,
                "name": str(names.get(pir_id) or "")[:500],
                "question": str(detail.get("question") or "")[:1000],
                "owner": str(detail.get("owner") or "")[:200],
                "review_cadence": cadence if cadence in _CADENCES else "",
                "primary_assets": [str(a)[:200] for a in assets if str(a).strip()][:50],
                "min_score": min_score,
            }
        )
    return rows


def _carry_profile(conn: sa.engine.Connection) -> None:
    profile = conn.execute(
        sa.text(
            "SELECT v.profile FROM organization_context_versions v "
            "JOIN organizations o ON o.id = v.organization_id "
            "WHERE o.role = 'primary' ORDER BY v.version DESC LIMIT 1"
        )
    ).scalar()
    if isinstance(profile, str):
        profile = json.loads(profile)
    for row in carried_rows(profile if isinstance(profile, dict) else {}):
        conn.execute(
            sa.text(
                f"INSERT INTO {_TABLE} (id, pir_id, name, question, owner, review_cadence, primary_assets, "
                "min_score, enabled) VALUES (:id, :pir_id, :name, :question, :owner, :review_cadence, "
                "CAST(:primary_assets AS jsonb), :min_score, true) ON CONFLICT DO NOTHING"
            ),
            {**row, "id": str(uuid.uuid4()), "primary_assets": json.dumps(row["primary_assets"])},
        )


def upgrade() -> None:
    conn = op.get_bind()
    # Idempotency probe: fresh installs run create_all + stamp head, and a crashed replica may retry.
    if _has_table(conn, _TABLE):
        return
    terminate_lock_holders(conn, "integrations")
    set_lock_timeout(conn)
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "integration_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("integrations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("pir_id", sa.String(100), nullable=False),
        sa.Column("name", sa.String(500), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("pir_type", sa.String(40), nullable=False, server_default=""),
        sa.Column("criteria_summary", sa.String(500), nullable=False, server_default=""),
        sa.Column("question", sa.String(1000), nullable=False, server_default=""),
        sa.Column("owner", sa.String(200), nullable=False, server_default=""),
        sa.Column("review_cadence", sa.String(16), nullable=False, server_default=""),
        sa.Column("primary_assets", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("min_score", sa.Integer(), nullable=False, server_default=sa.text("75")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("integration_id", "pir_id", name="uq_organization_pirs_integration_pir"),
        if_not_exists=True,
    )
    op.create_index("ix_organization_pirs_enabled", _TABLE, ["enabled"], if_not_exists=True)
    _carry_profile(conn)
    # The PIR sync of the profile is retired: the background refresh reads every OpenCTI's PIRs.
    if _has_table(conn, "organization_context_syncs"):
        conn.execute(sa.text("DELETE FROM organization_context_syncs WHERE section = 'pirs'"))


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    if _has_table(conn, _TABLE):
        op.drop_table(_TABLE, if_exists=True)
