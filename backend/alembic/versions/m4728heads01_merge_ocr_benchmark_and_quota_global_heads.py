"""merge the e2232seedslug1 and q4711glob01 heads

``e2232seedslug1`` and ``q4711glob01`` were both chained onto
``a4732signin1`` and merged a few minutes apart, leaving main with two heads,
which ``alembic upgrade head`` refuses on every startup. This empty merge
revision joins them. Re-pointing either one is not safe: a database that
already applied it from its branch would then look up to date and never run
the other. The two migrations touch different tables (``datasets`` /
``evaluations`` and ``quota_global_usage``), so their order does not matter.

Revision ID: m4728heads01
Revises: e2232seedslug1, q4711glob01
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "m4728heads01"
down_revision: str | Sequence[str] | None = ("e2232seedslug1", "q4711glob01")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
