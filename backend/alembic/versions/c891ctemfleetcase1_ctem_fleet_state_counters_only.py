"""ctem fleet state: counters only, the open work moves to the CTEM cases

The seeded CTEM loop fleets run on cases (#891): ``ctem:f1:state`` and
``ctem:f2:state`` keep counters only. This one-shot data migration rewrites
each such shared state entry: numbers and the last pass or refresh stay, every
other key (the open items, the closed and history lists) is dropped and the
entry gets one dated note naming how many open items were closed, and why.
A dropped open item is not lost: its CVE or threat is re-recorded as a signal
by the next fleet pass, and the scope gate opens or joins its case.

Data only, no DDL; touches at most a handful of rows. Downgrade is a no-op:
the dropped lists are not rebuilt.

Revision ID: c891ctemfleetcase1
Revises: c857ctemcaseloop1
Create Date: 2026-11-21 14:20:00.000000
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "c891ctemfleetcase1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c857ctemcaseloop1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The two fleet state keys, bare or namespaced (``workspace:<id>:ctem:f1:state``).
STATE_KEYS: tuple[str, ...] = ("ctem:f1:state", "ctem:f2:state")
#: The non-numeric keys a counters-only value keeps.
KEPT_KEYS: frozenset[str] = frozenset({"last_pass", "last_refresh"})
#: The keys that held open items, counted in the note.
OPEN_KEYS: tuple[str, ...] = ("open", "open_cases", "cases")
REASON = (
    "the open items moved to CTEM cases (#891): the fleet re-records each as a signal "
    "on its next pass and the scope gate opens or joins its case"
)


def is_fleet_state_key(key: str) -> bool:
    return any(key == k or key.endswith(f":{k}") for k in STATE_KEYS)


def _count(value: Any) -> int:
    if isinstance(value, (list, dict)):
        return len(value)
    return 0


def counters_only(value: dict[str, Any] | None) -> tuple[dict[str, Any], int, list[str]]:
    """(the counters-only value, the open items dropped, the keys dropped)."""
    value = dict(value or {})
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, item in value.items():
        if key in KEPT_KEYS or (isinstance(item, (int, float)) and not isinstance(item, bool)):
            kept[key] = item
        elif key == "counters" and isinstance(item, dict):
            kept[key] = {k: v for k, v in item.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        else:
            dropped.append(key)
    opened = sum(_count(value.get(k)) for k in OPEN_KEYS)
    return kept, opened, sorted(dropped)


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, key, value, notes FROM shared_states WHERE key LIKE '%ctem:f_:state'")
    ).fetchall()
    table = sa.table(
        "shared_states",
        sa.column("id"),
        sa.column("value", sa.JSON()),
        sa.column("notes", sa.JSON()),
    )
    now = datetime.now(UTC).isoformat()
    for row in rows:
        if not is_fleet_state_key(row.key):
            continue
        value = row.value if isinstance(row.value, dict) else {}
        kept, opened, dropped = counters_only(value)
        if not dropped:
            continue
        notes = list(row.notes or [])
        notes.append(
            {
                "at": now,
                "agent_id": None,
                "note": f"migration c891ctemfleetcase1: {opened} open item(s) closed, keys {', '.join(dropped)} "
                f"dropped; reason: {REASON}.",
            }
        )
        bind.execute(sa.update(table).where(table.c.id == row.id).values(value=kept, notes=notes))


def downgrade() -> None:
    """No-op: the dropped lists are not rebuilt."""
