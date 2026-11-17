"""merge the s7519sshold01 and w7518gwpoll1 heads

``s7519sshold01`` and ``w7518gwpoll1`` were both chained onto
``w7498wainbox1`` and merged one after the other, leaving main with two heads,
which ``alembic upgrade head`` refuses on every startup. This empty merge
revision joins them. Re-pointing either one is not safe: a database that
already applied it from its branch would then look up to date and never run
the other. The two migrations touch different tables (``shared_state``, and
the WhatsApp poll tallies), so their order does not matter.

Revision ID: m7544heads01
Revises: s7519sshold01, w7518gwpoll1
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "m7544heads01"
down_revision: str | Sequence[str] | None = ("s7519sshold01", "w7518gwpoll1")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
