"""apply the TOOL_APPROVALS_ENABLED seed to an existing install

``_seed_tool_approvals`` runs where the platform settings row is *created*, so
it reaches a fresh install and nothing else. An upgraded deployment already has
that row, and ``d4a2all3282`` gives the new column ``server_default TRUE`` — so
setting the variable before an upgrade did nothing at all, and the documented
behaviour ("an upgrade can land with the switch already off") was false.

This is the one-time seed, applied where the schema arrives. A migration runs
once by definition, which is exactly the contract the variable already claims:
read once, then the stored value is authoritative and the variable is ignored.

Only ever writes FALSE. The column already defaults TRUE, so an absent or
unrecognised variable leaves approvals on — a typo must not be the thing that
disables an approval control.

Revision ID: i1seed3311
Revises: h1bot3311
Create Date: 2026-09-04 09:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "i1seed3311"
down_revision: str | None = "h1bot3311"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FALSY = {"0", "false", "no", "off"}


def upgrade() -> None:
    # Same variable the fresh-install seed reads through pydantic
    # (``platform_settings_helpers.py``). Reading ``os.environ`` here meant the
    # kill switch worked on a fresh install and was silently ignored on the
    # upgrade path this migration exists for.
    from app.core.config import settings

    configured = (settings.tool_approvals_enabled or "").strip().lower()
    if configured not in _FALSY:
        return

    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("platform_settings"):
        return
    columns = {c["name"] for c in inspector.get_columns("platform_settings")}
    if "tool_approvals_enabled" not in columns:
        return

    terminate_lock_holders(conn, "platform_settings")
    set_lock_timeout(conn)
    # The singleton only; bounded by definition, and predicated so a rerun of a
    # resumed upgrade changes nothing.
    op.execute(
        "UPDATE platform_settings SET tool_approvals_enabled = false "
        "WHERE tool_approvals_enabled IS DISTINCT FROM false"
    )


def downgrade() -> None:
    """Nothing to undo.

    The value is an administrator's setting from the moment this runs; putting
    it back to TRUE on a rollback would re-enable a control somebody may have
    since turned off on purpose, which is the same lossy rewrite the reviewer
    flagged elsewhere in this chain.
    """
