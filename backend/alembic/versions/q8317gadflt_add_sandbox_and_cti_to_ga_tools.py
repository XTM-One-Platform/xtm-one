"""add the sandbox and the CTI toolkit to the General Assistant's built-in tools

``DEFAULT_GA_BUILTIN_TOOL_IDS`` is the one default set of both default
assistants and includes ``code_interpreter``, ``stix_bundle_validator`` and
``fimi_analytics`` (the ``cti`` group). That constant only seeds a
General Assistant row when it is created, so a platform whose row
already exists would never carry them: this revision appends the missing ones
to every ``general_assistant_config.builtin_tool_ids`` once, as
``f7030fleetsga`` did for ``fleets``.

A tool counts as present when the row names it or the group it merged into
(``cti`` for the two CTI ids), so a row an administrator saved in the
group form is left as it is. Rows that hold no tools are written too: a NULL
list is an empty one, as ``general_chat`` reads it. An administrator who
removes a tool later keeps it removed: the revision runs once.

The rows to change are read without a lock first; only when one needs a write
is the lock timeout set and the rows read again ``FOR UPDATE``.

Revision ID: q8317gadflt
Revises: p8270platgrp
Create Date: 2026-11-04 02:00:00.000000
"""

import json
import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

logger = logging.getLogger(__name__)

revision: str = "q8317gadflt"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "p8270platgrp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "general_assistant_config"
_COLUMN = "builtin_tool_ids"

#: The tools added to ``DEFAULT_GA_BUILTIN_TOOL_IDS``, each with the group
#: that stands for it, spelled here so the revision keeps meaning what it meant
#: once the constant and the alias registry move on.
ADDED_TOOL_IDS: dict[str, str] = {
    "code_interpreter": "code_interpreter",
    "stix_bundle_validator": "cti",
    "fimi_analytics": "cti",
}


def with_added_tools(stored: str | None) -> str | None:
    """*stored* with the missing ``ADDED_TOOL_IDS`` appended, as JSON text.

    ``None`` when there is nothing to write: every tool is there (by itself or
    its group), or the stored value is not a JSON list.
    """
    if stored is None:
        tool_ids: object = []
    else:
        try:
            tool_ids = json.loads(stored)
        except ValueError:
            return None
    if tool_ids is None:
        tool_ids = []
    if not isinstance(tool_ids, list):
        return None
    missing = [
        tool_id
        for tool_id, group in ADDED_TOOL_IDS.items()
        if tool_id not in tool_ids and group not in tool_ids
    ]
    if not missing:
        return None
    return json.dumps([*tool_ids, *missing])


def _pending_updates(conn, *, lock: bool) -> list[tuple[object, str]]:
    rows = conn.execute(
        sa.text(
            f"SELECT id, CAST({_COLUMN} AS text) FROM {_TABLE}"  # noqa: S608
            + (" FOR UPDATE" if lock else "")
        )
    ).all()
    return [(row_id, value) for row_id, stored in rows if (value := with_added_tools(stored)) is not None]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    if _COLUMN not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        return
    if not _pending_updates(conn, lock=False):
        return
    set_lock_timeout(conn)
    updates = _pending_updates(conn, lock=True)
    for row_id, value in updates:
        conn.execute(
            sa.text(f"UPDATE {_TABLE} SET {_COLUMN} = CAST(:value AS json) WHERE id = :id"),  # noqa: S608
            {"value": value, "id": row_id},
        )
    if updates:
        logger.warning(
            "The General Assistant now carries the sandbox (code_interpreter) and the CTI toolkit. "
            "Remove them in Settings > General Assistant."
        )


def downgrade() -> None:
    # Whether a tool was added by this revision or by an administrator is not
    # recorded, so there is nothing safe to undo.
    pass
