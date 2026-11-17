"""forget the Slack channel identities that name a channel, not a member

The Slack adapter put the channel id in ``InboundMessage.channel_user_id``, so
the router auto-linked ``slack:<channel id>`` to the first sender of a channel
who resolved by email, and resolved every later sender of that channel whose
email did not resolve as that user. The adapter now passes the sender's member
id (``U...`` / ``W...``); a key the old code linked names a public channel
(``C...``), a private channel or group DM (``G...``) or a direct message
(``D...``), and is removed from ``users.channel_identities``. A key an
administrator linked to a member id is kept.

Lock profile: no DDL. The ``UPDATE`` takes row locks on the users that hold
such a key only, in batches of ``_BATCH`` rows, under ``set_lock_timeout`` so a
row a long transaction holds fails fast into the runner's retry.

Re-runnable: with no such key left, the probe finds no row and nothing is
written. ``downgrade`` is a no-op: the removed keys only ever resolved a sender
as someone else, and nothing reads them any more.

Revision ID: s5811slackid01
Revises: v5710bound01
Create Date: 2026-10-24 12:00:00.000000
"""
# ruff: noqa: S608 - the only text interpolated into these statements is this
# module's own SQL fragments; the batch size is a bound parameter.

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "s5811slackid01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "v5710bound01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH = 500


def _names_a_channel(key: str) -> str:
    """SQL true when the ``channel_identities`` key *key* is a Slack channel id."""
    return f"(left({key}, 6) = 'slack:' AND substr({key}, 7, 1) IN ('C', 'G', 'D'))"


_HOLDERS = (
    "SELECT id FROM users "
    "WHERE jsonb_typeof(channel_identities) = 'object' "
    "  AND EXISTS (SELECT 1 FROM jsonb_object_keys(channel_identities) AS k "
    f"              WHERE {_names_a_channel('k')}) "
    "ORDER BY id LIMIT :batch"
)

_FORGET = (
    "UPDATE users SET channel_identities = ("
    "  SELECT coalesce(jsonb_object_agg(e.key, e.value), '{}'::jsonb) "
    "  FROM jsonb_each(users.channel_identities) AS e "
    f"  WHERE NOT {_names_a_channel('e.key')}"
    ") "
    f"WHERE id IN ({_HOLDERS})"
)


def upgrade() -> None:
    conn = op.get_bind()
    probe = conn.execute(sa.text(_HOLDERS), {"batch": 1}).first()
    if probe is None:
        return
    set_lock_timeout(conn)
    while conn.execute(sa.text(_FORGET), {"batch": _BATCH}).rowcount:
        pass


def downgrade() -> None:
    """Nothing to restore: the removed keys only ever resolved the wrong sender."""
