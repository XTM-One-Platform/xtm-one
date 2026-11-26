"""add custom dashboards to existing General Assistant tools

``custom_dashboards`` is added to ``DEFAULT_GA_BUILTIN_TOOL_IDS``, which a
General Assistant row receives only when it is created. This adds it to the
rows that already exist, once, as ``v5774gadflt02`` did for the Video Studio:
a migration runs once per database, so an administrator who removes the tool
afterwards keeps that choice. Fresh installs run ``create_all`` + ``stamp
head``: the body never executes there and the row is created later with the
constant. The tool is only built while ``DASHBOARD_STUDIO`` is on, so a
deployment with the flag off sees no change.

``users.ga_builtin_tool_ids`` (the tools a user picked for their own General
Assistant) is left alone: that list is the user's choice.

The list is read and written back in Python rather than cast to ``jsonb``, so
a stored value PostgreSQL cannot parse, or one that is not a list, is left as
it is. No DDL. A first read takes no lock and returns when there is nothing to
write; otherwise the rows are read again ``FOR UPDATE`` under
``set_lock_timeout`` and written from that read.

Revision ID: s1925gadash01
Revises: s2434sigsrc01
Create Date: 2026-11-24 18:30:00.000000
"""

import json
import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

logger = logging.getLogger(__name__)

revision: str = "s1925gadash01"
down_revision: str | Sequence[str] | None = "s2434sigsrc01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "general_assistant_config"
_COLUMN = "builtin_tool_ids"

#: The tool added to ``DEFAULT_GA_BUILTIN_TOOL_IDS``, spelled here so the
#: revision keeps meaning what it meant once the constant moves on.
ADDED_TOOL_IDS: tuple[str, ...] = ("custom_dashboards",)


def with_added_tools(stored: str | None) -> str | None:
    """*stored* with the missing ``ADDED_TOOL_IDS`` appended, as JSON text.

    ``None`` when there is nothing to write: the tool is there, or the stored
    value is not a JSON list. A NULL list is an empty one, as ``general_chat``
    reads it.
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
    missing = [tool_id for tool_id in ADDED_TOOL_IDS if tool_id not in tool_ids]
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
            "The General Assistant now carries the custom dashboards tools (custom_dashboards), "
            "used while DASHBOARD_STUDIO is on. Remove them in Settings > General Assistant."
        )


def downgrade() -> None:
    # Whether the tool was added by this revision or by an administrator is not
    # recorded, so there is nothing safe to undo.
    pass
