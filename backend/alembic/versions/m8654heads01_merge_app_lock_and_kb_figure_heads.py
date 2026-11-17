"""merge the c2lck6592002 and c8488kbfigr01 heads

``c2lck6592002`` (the companion app lock, #6602) and ``c8488kbfigr01`` (the
knowledge-base figure description, #8536) were merged one after the other on
different parents, leaving main with two heads, which ``alembic upgrade head``
refuses on every startup. This empty merge revision joins them. Re-pointing
either one is not safe: a database that already applied it from its branch
would then look up to date and never run the other. The two migrations touch
different tables (``app_lock_*``, and ``knowledge_bases`` /
``knowledge_documents``), so their order does not matter.

Revision ID: m8654heads01
Revises: c2lck6592002, c8488kbfigr01
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "m8654heads01"
down_revision: str | Sequence[str] | None = ("c2lck6592002", "c8488kbfigr01")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
