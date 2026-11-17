"""widen a step's evidence from URLs to structured citations

`investigation_steps.evidence_urls` was a list of URL strings, which cannot
name every kind of source. A knowledge-base hit is a DOCUMENT and an MCP tool
result is a TOOL CALL — neither has an address a reader could open. Storing
them as URLs would have produced citations that render as links and lead
nowhere, which is the class of overstatement this feature's step vocabulary
exists to prevent.

So the column becomes `evidence`: a list of ``{kind, label, href}`` objects,
where `kind` is one of `EVIDENCE_KINDS` and `href` is present only when there
is genuinely somewhere to go.

**Converted, not dropped-and-recreated.** Existing rows carry real URLs, and
losing them would silently erase the evidence of every run made on this branch
so far. The UPDATE maps each string to ``{"kind": "url", "label": u, "href":
u}``. That is an in-transaction backfill, which the lock-light rules ban when
unbounded — so it is bounded explicitly: `investigation_steps` is a table this
same unmerged stack created, so it holds development rows only, and a bounded
probe (at most `_MAX_BACKFILL_ROWS` + 1 rows, read before any DDL) refuses the
migration on a larger table rather than dropping evidence it did not convert
or converting it under the DDL lock.

Rolling-deploy safety: `investigation_steps` is created by this same stack
(`i1inv3302001`), which has not been released, so no released version reads
or writes `evidence_urls` and dropping it cannot fail an older replica. The
new column is added and filled before the old one is dropped, in one
revision.

Revision ID: i3evd1939001
Revises: i2det3302002
Create Date: 2026-09-04 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "i3evd1939001"
down_revision: str | Sequence[str] | None = "i2det3302002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "investigation_steps"

#: Above this, the conversion is skipped rather than run inside the migration
#: transaction. Chosen well above any realistic development row count and far
#: below anything that would hold a lock noticeably.
_MAX_BACKFILL_ROWS = 50_000


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    # Bounded, and before any DDL: a count(*) under the ADD COLUMN lock would
    # scan the whole table only to decide not to convert it.
    if _has_column(conn, _TABLE, "evidence_urls"):
        rows = conn.execute(
            sa.text(
                f"SELECT count(*) FROM (SELECT 1 FROM {_TABLE} "  # noqa: S608 — fixed table name
                f"LIMIT {_MAX_BACKFILL_ROWS + 1}) AS probe"
            )
        ).scalar() or 0
        if rows > _MAX_BACKFILL_ROWS:
            raise RuntimeError(
                f"{_TABLE} holds more than {_MAX_BACKFILL_ROWS} rows: convert "
                "evidence_urls to evidence in batches, then re-run this "
                "migration (it refuses rather than drop citations it did not "
                "convert)."
            )

    if not _has_column(conn, _TABLE, "evidence"):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.add_column(
            _TABLE,
            sa.Column(
                "evidence",
                postgresql.JSON(),
                nullable=False,
                server_default=sa.text("'[]'::json"),
            ),
        )

    # Convert, only while the old column is still there to convert from (the
    # probe above bounded the table).
    if _has_column(conn, _TABLE, "evidence_urls"):
        conn.execute(
            sa.text(
                f"""
                UPDATE {_TABLE}
                   SET evidence = COALESCE(
                         (SELECT json_agg(
                             json_build_object(
                               'kind', 'url', 'label', u, 'href', u))
                            FROM json_array_elements_text(evidence_urls) AS u),
                         '[]'::json)
                 WHERE evidence_urls IS NOT NULL
                   AND json_array_length(evidence_urls) > 0
                """  # noqa: S608 — fixed table name
            )
        )

        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.drop_column(_TABLE, "evidence_urls")


def downgrade() -> None:
    conn = op.get_bind()

    if not _has_column(conn, _TABLE, "evidence_urls"):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.add_column(
            _TABLE,
            sa.Column(
                "evidence_urls",
                postgresql.JSON(),
                nullable=False,
                server_default=sa.text("'[]'::json"),
            ),
        )

    if _has_column(conn, _TABLE, "evidence"):
        # Only `url` evidence has anything to put back: a document or a tool
        # result has no URL, and inventing one would be worse than dropping it.
        # A downgrade therefore loses those citations, which is stated rather
        # than hidden — the alternative is refusing to downgrade at all.
        conn.execute(
            sa.text(
                f"""
                UPDATE {_TABLE}
                   SET evidence_urls = COALESCE(
                         (SELECT json_agg(e ->> 'href')
                            FROM json_array_elements(evidence) AS e
                           WHERE e ->> 'kind' = 'url'
                             AND e ->> 'href' IS NOT NULL),
                         '[]'::json)
                 WHERE evidence IS NOT NULL
                   AND json_array_length(evidence) > 0
                """  # noqa: S608 — fixed table name
            )
        )
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.drop_column(_TABLE, "evidence")
