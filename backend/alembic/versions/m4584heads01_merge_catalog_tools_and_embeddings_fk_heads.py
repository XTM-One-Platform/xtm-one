"""merge the c4249tools01 and e4059embfk01 heads

``c4249tools01`` and ``e4059embfk01`` both revised
``s2977stale01`` and merged minutes apart, leaving main with two heads, which
``alembic upgrade head`` refuses on every startup. This empty merge revision
joins them. Re-pointing one onto the other is not safe: a database that already
applied ``e4059embfk01`` from its branch would then look up to date and never
run ``c4249tools01``. The two migrations are independent, so their order does
not matter, and a merge is correct for a database at either head.

Revision ID: m4584heads01
Revises: c4249tools01, e4059embfk01
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "m4584heads01"
down_revision: str | Sequence[str] | None = ("c4249tools01", "e4059embfk01")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
