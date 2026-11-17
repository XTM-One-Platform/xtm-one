"""add the platform builder and web fetch to existing General Assistant tools

``agent_builder`` and ``web_fetch`` are added to ``DEFAULT_GA_BUILTIN_TOOL_IDS``,
which a General Assistant row receives only when it is created. This adds them
to the rows that already exist, once: a migration runs once per database, so an
administrator who removes either tool afterwards keeps that choice. Fresh
installs run ``create_all`` + ``stamp head``: the body never executes there and
the row is created later with the constant.

``users.ga_builtin_tool_ids`` (the tools a user picked for their own General
Assistant in the profile) is left alone: that list is the user's choice.

The list is read and written back in Python rather than cast to ``jsonb``, so
a stored value PostgreSQL cannot parse, or one that is not a list, is
left as it is instead of failing the upgrade. No DDL. A first read takes no
lock and returns when there is nothing to write (a re-run finds both tools
present); otherwise the rows are read again ``FOR UPDATE``, under
``set_lock_timeout``, and written from that read. A run that writes logs one
operator notice naming the tools and the switch that bounds an admin's reach.

Revision ID: g5209gadefault
Revises: p1panic0001
Create Date: 2026-10-02 18:00:00.000000
"""

import json
import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

logger = logging.getLogger(__name__)

revision: str = "g5209gadefault"
down_revision: str | Sequence[str] | None = "p1panic0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "general_assistant_config"
_COLUMN = "builtin_tool_ids"

#: The tools added to ``DEFAULT_GA_BUILTIN_TOOL_IDS``, spelled here so the
#: revision keeps meaning what it meant once the constant moves on.
ADDED_TOOL_IDS: tuple[str, ...] = ("agent_builder", "web_fetch")


def with_added_tools(stored: str | None) -> str | None:
    """*stored* with the missing ``ADDED_TOOL_IDS`` appended, as JSON text.

    ``None`` when there is nothing to write: both tools are there, or the
    stored value is not a JSON list. A NULL list is an empty one, as
    ``general_chat`` reads it.
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
    """``(id, new value)`` for every row missing a tool, read as committed now."""
    rows = conn.execute(
        sa.text(
            f"SELECT id, CAST({_COLUMN} AS text) FROM {_TABLE}"  # noqa: S608
            + (" FOR UPDATE" if lock else "")
        )
    ).all()
    return [
        (row_id, value)
        for row_id, stored in rows
        if (value := with_added_tools(stored)) is not None
    ]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    if _COLUMN not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        return
    if not _pending_updates(conn, lock=False):
        return
    # Read again under the row lock: an administrator's save committed while
    # this revision runs (a rolling deploy) is the list the tools join, never
    # one this revision overwrites.
    set_lock_timeout(conn)
    updates = _pending_updates(conn, lock=True)
    for row_id, value in updates:
        conn.execute(
            sa.text(
                f"UPDATE {_TABLE} SET {_COLUMN} = CAST(:value AS json) WHERE id = :id"  # noqa: S608
            ),
            {"value": value, "id": row_id},
        )
    if updates:
        logger.warning(
            "The General Assistant now runs the platform builder (agent_builder) "
            "and web fetch (web_fetch). The platform builder acts as the "
            "chatting user; while AGENT_TOOLS_STRICT_ADMIN_ACCESS is off it reaches "
            "an admin's colleagues' private objects, as the REST API does. Remove "
            "either tool in Settings > General Assistant, or turn that switch on in "
            "Settings > Security. See UPGRADING.md."
        )


def downgrade() -> None:
    # Whether a tool was added by this revision or by an administrator is not
    # recorded, so there is nothing safe to undo. The previous release runs
    # both tools.
    pass
