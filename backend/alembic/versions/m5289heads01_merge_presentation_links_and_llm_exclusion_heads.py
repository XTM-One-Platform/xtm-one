"""merge the p5396link01 and x4684llmexcl heads

``p5396link01`` and ``x4684llmexcl`` were both chained onto
``k1hub0001`` and merged a few minutes apart, leaving main with two heads,
which ``alembic upgrade head`` refuses on every startup. This empty merge
revision joins them. Re-pointing either one is not safe: a database that
already applied it from its branch would then look up to date and never run
the other. The two migrations touch different tables
(``presentation_public_links`` and ``llm_provider_configs``), so their order
does not matter.

Revision ID: m5289heads01
Revises: p5396link01, x4684llmexcl
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "m5289heads01"
down_revision: str | Sequence[str] | None = ("p5396link01", "x4684llmexcl")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
