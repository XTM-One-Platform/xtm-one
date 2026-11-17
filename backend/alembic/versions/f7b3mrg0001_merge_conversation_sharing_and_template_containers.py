"""merge the conversation-sharing and template-container heads

``e3a9shr0439`` (conversation sharing) and ``e3t9tpl2636`` (template
containers) were both cut from ``f6a2oash0001`` and both merged, so the
history had **two heads**. Alembic refuses to ``upgrade head`` with an
ambiguous tip, and ``tests/test_migration_integrity.py::test_single_head``
fails for every branch based on that history.

This revision has no body on purpose: it only rejoins the two branches. The
two migrations it merges touch disjoint objects (``conversation_shares`` and
its indexes on one side, ``template_containers`` plus two ``file_attachments``
columns on the other), so there is nothing to reconcile between them — no DDL,
no data backfill, and therefore no lock to take.

The fork it was written for was *also* repaired the other way, by chaining
``e3a9shr0439`` behind ``e3t9tpl2636``. That left this revision claiming two
parents where one is now the other's ancestor — a diamond, which keeps the head
count at one but leaves the middle of the chain branched, so the Migrations
job's step-by-step ``upgrade +1`` walk dies with "Ambiguous walk" at
``e3t9tpl2636``. The redundant second parent is therefore dropped: the id stays
(deployments are already stamped past it) and the graph becomes linear.

Revision ID: f7b3mrg0001
Revises: e3a9shr0439
Create Date: 2026-07-31 00:00:00.000000
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "f7b3mrg0001"
down_revision: str | None = "e3a9shr0439"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: this revision exists only to rejoin two heads."""


def downgrade() -> None:
    """No-op: splitting the history back into two heads is the inverse."""
