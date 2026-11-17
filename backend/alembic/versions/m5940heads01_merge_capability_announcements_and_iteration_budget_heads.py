"""merge the v5936capann01 and i5940iter01 heads

``v5936capann01`` and ``i5940iter01`` were both chained onto
``r5931gareport`` and merged a minute apart, leaving main with two heads,
which ``alembic upgrade head`` refuses on every startup. This empty merge
revision joins them. Re-pointing either one is not safe: a database that
already applied it from its branch would then look up to date and never run
the other. The two migrations touch different tables
(``model_catalog_announcements``, and ``general_assistant_config`` with
``agents``), so their order does not matter.

Revision ID: m5940heads01
Revises: v5936capann01, i5940iter01
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "m5940heads01"
down_revision: str | Sequence[str] | None = ("v5936capann01", "i5940iter01")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
