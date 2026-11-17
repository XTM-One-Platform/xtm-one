"""merge the m4584heads01 and a2ah1stopped1 heads

The ``a2ag1cnlowed1`` -> ``a2ah1stopped1`` pair was cut from ``s2977stale01``
and merged after main had moved to ``m4584heads01``, leaving main with two
heads, which ``alembic upgrade head`` refuses on every startup. This empty
merge revision joins them. Re-pointing ``a2ag1cnlowed1`` is not safe: a
database that already applied it from its branch would then look up to date
and never run ``c4249tools01`` / ``e4059embfk01``. The branches are
independent, so their order does not matter.

Revision ID: m4630heads01
Revises: m4584heads01, a2ah1stopped1
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "m4630heads01"
down_revision: str | Sequence[str] | None = ("m4584heads01", "a2ah1stopped1")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
