"""repair the assignment runs whose trigger_context PostgreSQL cannot parse

``assignment_runs.trigger_context`` is ``json``, which keeps a ``\\u0000`` or
lone UTF-16 surrogate escape verbatim. Every read that makes PostgreSQL
de-escape it (``->>``, a ``::jsonb`` cast) then aborts the whole statement on
that one row: the A2A Activity page of every partner answered 500 because one
run held a ``\\u0000`` (#11790). The engine's JSON serializer has kept such
values out of new writes for a while (``app.core.database.json_serializer``),
so only rows written before it, or around the ORM, hold one.

This revision rewrites each of them the way every JSON write stores a value
now (``sanitize_json_strings``: the NUL dropped, a lone surrogate U+FFFD, a
valid pair kept). ``_UNREADABLE`` picks the candidates with the test of
``app.core.json_sql.json_unparseable`` (spelled here: a migration keeps
meaning what it meant on the day it ran) and a real cast decides each, so a
row that casts is left alone. A row the repair cannot make castable is left
as it is and named by id: the read path guards against it
(``when_json_parseable``), so it is hidden, never fatal.

Lock-light: one read-only scan to find the candidates, then an UPDATE by
primary key per repaired row (a handful in practice), no DDL, no table
rewrite. Idempotent: a second run finds no candidate.

Downgrade is a no-op: the escapes removed cannot be told apart from the
values the write path stores, and putting them back would break the pages
again.

Revision ID: a2ah2runctx01
Revises: a2ah1dirrow01
Create Date: 2026-11-14 11:30:00.000000
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.utils import sanitize_json_strings

revision: str = "a2ah2runctx01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "a2ah1dirrow01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The test of ``app.core.json_sql.json_unparseable`` on the column, with the
#: ``_UNREADABLE_PARAMS`` as its patterns.
_UNREADABLE = """
    ((CAST(r.trigger_context AS text) ~ :refused_escape
      AND regexp_replace(replace(CAST(r.trigger_context AS text), :escaped_backslash, '_'),
                         :surrogate_pair, '_', 'g') ~ :refused_escape)
     OR CAST(r.trigger_context AS text) ~ :out_of_range_number)
"""

_UNREADABLE_PARAMS = {
    "refused_escape": r"\\u(0000|[dD][89a-fA-F])",
    "surrogate_pair": r"\\u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}",
    "escaped_backslash": "\\\\",
    "out_of_range_number": (
        r"(^|[:,\[])\s*-?([0-9]+(\.[0-9]+)?[eE][+-]?[0-9]{5}"
        r"|([0-9]+\.)?[0-9]{250}[0-9]{250}[0-9]{250}[0-9]{250})"
    ),
}


def _castable(conn, stored: str) -> bool:
    """Whether PostgreSQL casts *stored* to ``jsonb``: the exact answer, in a savepoint."""
    try:
        with conn.begin_nested():
            # IS NOT NULL: the value itself is never decoded by the driver.
            conn.execute(sa.text("SELECT CAST(CAST(:v AS json) AS jsonb) IS NOT NULL"), {"v": stored})
    except sa.exc.DBAPIError:
        return False
    return True


def _repaired_json(stored: str) -> str | None:
    """*stored* as the write path stores it now (``sanitize_json_strings``), ``None`` when unreadable."""
    try:
        return json.dumps(sanitize_json_strings(json.loads(stored)), allow_nan=False)
    except (ValueError, RecursionError):
        return None


def upgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table("assignment_runs"):
        return
    _repair_unreadable_trigger_contexts(conn)


def _repair_unreadable_trigger_contexts(conn) -> None:
    """Rewrite every unreadable ``trigger_context`` the repair can make castable; name the rest by id."""
    rows = conn.execute(
        sa.text(
            f"SELECT r.id, CAST(r.trigger_context AS text) FROM assignment_runs r WHERE {_UNREADABLE}"  # noqa: S608 - no input: the predicate is a module constant
        ),
        _UNREADABLE_PARAMS,
    ).all()
    repaired, left = 0, []
    for run_id, stored in rows:
        if _castable(conn, stored):
            continue
        clean = _repaired_json(stored)
        if clean is None or not _castable(conn, clean):
            left.append(str(run_id))
            continue
        conn.execute(
            sa.text("UPDATE assignment_runs SET trigger_context = CAST(:c AS json) WHERE id = :id"),
            {"c": clean, "id": run_id},
        )
        repaired += 1
    if repaired:
        print(  # noqa: T201 - migrations report through stdout
            f"assignment_runs: repaired the trigger_context of {repaired} run(s) holding a NUL or "
            "lone surrogate escape (stored as every JSON write now stores them)"
        )
    if left:
        print(  # noqa: T201 - migrations report through stdout
            f"assignment_runs: {len(left)} run(s) keep a trigger_context PostgreSQL cannot parse "
            f"and stay hidden from the guarded reads: {', '.join(left[:20])}"
        )


def downgrade() -> None:
    # Nothing to undo: see the module docstring.
    return None
