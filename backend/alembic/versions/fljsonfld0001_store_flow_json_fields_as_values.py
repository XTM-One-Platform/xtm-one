"""store the JSON fields of flow nodes as the value their text holds

The builder edited a JSON config field (a Structured output schema, a trigger
filter, a payload, arguments) as text and saved the text, while every reader
of such a field takes the object or the list: a flow's Structured output was
ignored, a trigger filter never matched, a payload was sent empty. The write
path now stores the value (``app.services.flow_graph.parse_json_fields``);
this revision converts what was stored before, in ``flows.graph`` and in
``flow_versions.graph`` (a restore writes a version's graph back as it is).

Row by row in Python with ``parse_json_fields``, the function the write path
uses, in batches of ``_BATCH`` rows; only a row whose graph changes is
written. Text that is not JSON is kept, as on a save. Idempotent: a converted
field is no longer text. Row writes take no table lock; the lock timeout bounds
a wait on a row a running save holds, and the runner retries.

Downgrade: a no-op. The text held exactly the value now stored, and the
earlier release reads a stored value too.

Revision ID: fljsonfld0001
Revises: x11380focdrop1
Create Date: 2026-11-12 12:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "fljsonfld0001"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "x11380focdrop1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[str, ...] = ("flows", "flow_versions")
_BATCH = 500


def _load(value):
    if value is None or isinstance(value, dict):
        return value
    return json.loads(value)


def converted_graph(graph):
    """*graph* with its nodes' JSON fields parsed, or ``None`` when unchanged.

    Pure, so ``tests/test_flow_json_fields.py`` pins it.
    """
    from app.services.flow_graph import parse_json_fields

    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        return None
    changed = False
    nodes = []
    for node in graph["nodes"]:
        if isinstance(node, dict) and isinstance(node.get("config"), dict):
            config = parse_json_fields(node.get("kind"), node["config"])
            if config != node["config"]:
                node = {**node, "config": config}
                changed = True
        nodes.append(node)
    return {**graph, "nodes": nodes} if changed else None


def upgrade() -> None:
    from app.core.migration_ddl import set_lock_timeout

    conn = op.get_bind()
    set_lock_timeout(conn)
    for table in _TABLES:
        last_id = None
        while True:
            # No input: table from _TABLES; values are bound.
            rows = conn.execute(
                sa.text(
                    f"SELECT id, graph FROM {table} "  # noqa: S608
                    + ("WHERE id > :last_id " if last_id is not None else "")
                    + "ORDER BY id LIMIT :limit"
                ),
                {"last_id": last_id, "limit": _BATCH} if last_id is not None else {"limit": _BATCH},
            ).fetchall()
            if not rows:
                break
            for row in rows:
                graph = converted_graph(_load(row.graph))
                if graph is not None:
                    conn.execute(
                        sa.text(f"UPDATE {table} SET graph = CAST(:graph AS JSON) WHERE id = :id"),  # noqa: S608
                        {"graph": json.dumps(graph), "id": row.id},
                    )
            last_id = rows[-1].id


def downgrade() -> None:
    """Nothing to undo: the stored value is what the text held."""
