"""merge the v6164mref01 and w6168wiorg01 heads

``w6168wiorg01`` and ``v6164mref01`` were both chained onto
``v6163range01`` and merged minutes apart, leaving main with two heads, which
``alembic upgrade head`` refuses on every startup. This empty merge revision
joins them. Re-pointing either one is not safe: a database that already
applied it from its branch would then look up to date and never run the
other. The two migrations touch different tables (``work_items``, and
``video_clips`` with ``video_renders`` and ``video_projects``), so their order
does not matter.

Revision ID: m6176heads01
Revises: v6164mref01, w6168wiorg01
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "m6176heads01"
down_revision: str | Sequence[str] | None = ("v6164mref01", "w6168wiorg01")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
