"""prune the share grants naming a user or a group that no longer exists

``shared_user_ids`` / ``shared_group_ids`` are JSON arrays with no foreign key,
so deleting a user or a group prunes them itself. It used to walk explicit model
lists, and several shareable models were missing from them: flows, flow event
types, NOVA rules, workspaces and relay groups for both principals;
presentations, runners and template containers for groups; LLM providers for
users. Their grants still name accounts and groups that are gone, and the
sharing UI still lists them. The deletion paths now prune every model holding
grants (``helpers.share_grant_models``); this revision removes what the earlier
deletions left behind, on every table, once.

A user id is kept when its canonical form names an existing account, as the
grant resolvers canonicalise user ids; a group id when it names an existing
group exactly, as they match group ids. Any other element names no one and
grants nothing: it goes, and so does its permission entry. A list left empty is
stored as NULL, as the deletion paths store it. ``flow_runs`` is not touched: a
run's copy of its flow's audience is history (``helpers.GRANT_SNAPSHOT_MODELS``).

Row writes, no DDL, in bounded batches outside the migration transaction
(``autocommit_block``): the rows carrying a grant are read ``_BATCH_SIZE`` at a
time in id order, and every rewrite is its own statement, committed on its own,
so no transaction holds more than one row lock and memory stays bounded however
many entities an install has. A row is rewritten only while its grants still
read as they did (compare-and-set), so an edit committed meanwhile, by a replica
still serving the previous release among others, is kept rather than
overwritten. ``lock_timeout`` bounds the wait on a row such a replica holds, and
the startup runner retries a timed-out run; an interrupted run resumes cleanly,
since a pruned row no longer changes.

Downgrade is a no-op: the ids removed named no one, so there is nothing to put
back.

Revision ID: q5257shrgnt1
Revises: c5x1chn5134
Create Date: 2026-10-03 09:00:00.000000
"""

import json
import logging
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import DEFAULT_LOCK_TIMEOUT

# revision identifiers, used by Alembic.
revision: str = "q5257shrgnt1"
down_revision: str | None = "c5x1chn5134"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

#: Every table holding share grants at this revision. Explicit rather than
#: derived from ``Base.metadata`` so the migration describes a fixed historical
#: schema and cannot silently widen when a future model gains the columns.
_TABLES = (
    "agent_skills",
    "agents",
    "channel_bots",
    "custom_tools",
    "datasets",
    "evaluations",
    "flow_event_types",
    "flows",
    "integrations",
    "knowledge_bases",
    "llm_provider_configs",
    "mcp_servers",
    "nova_rules",
    "presentations",
    "prompts",
    "relay_groups",
    "runners",
    "template_containers",
    "variables",
    "workspaces",
)

#: ``(ids column, permission map column, principal table, canonicalise)``.
_GRANTS = (
    ("shared_user_ids", "shared_user_permissions", "users", True),
    ("shared_group_ids", "shared_group_permissions", "groups", False),
)

_BATCH_SIZE = 500
_LOOKUP_CHUNK = 1000

#: ``(table, ids column, permission map column or None, principal table, canonicalise)``.
_Target = tuple[str, str, str | None, str, bool]


def _canonical(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _decode(text: str | None) -> object:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _existing(conn, principals: str, candidates: set[str]) -> set[str]:
    found: set[str] = set()
    ordered = sorted(candidates)
    for start in range(0, len(ordered), _LOOKUP_CHUNK):
        rows = conn.execute(
            sa.text(f"SELECT CAST(id AS text) FROM {principals} WHERE id = ANY(CAST(:ids AS uuid[]))"),  # noqa: S608
            {"ids": ordered[start : start + _LOOKUP_CHUNK]},
        )
        found.update(rows.scalars())
    return found


def _batch(conn, target: _Target, after: uuid.UUID | None, batch_size: int) -> list:
    """The next *batch_size* rows carrying a grant in *target*, after the id *after*."""
    table, ids_col, perms_col, _principals, _canonicalise = target
    perms_sql = f"CAST({perms_col} AS text)" if perms_col else "CAST(NULL AS text)"
    perms_filter = f" OR {perms_col} IS NOT NULL" if perms_col else ""
    after_filter = " AND id > :after" if after is not None else ""
    return conn.execute(
        sa.text(
            f"SELECT id, CAST({ids_col} AS text), {perms_sql} FROM {table} "  # noqa: S608
            f"WHERE ({ids_col} IS NOT NULL{perms_filter}){after_filter} ORDER BY id LIMIT :n"
        ),
        {"after": after, "n": batch_size} if after is not None else {"n": batch_size},
    ).all()


def _rewrite(conn, target: _Target, rows: list) -> int:
    """Drop the grants naming no one from *rows*; returns how many rows changed."""
    table, ids_col, perms_col, principals, canonicalise = target

    def key(value: object) -> str | None:
        return _canonical(value) if canonicalise else (value if isinstance(value, str) else None)

    decoded = []
    candidates: set[str] = set()
    for row_id, ids_text, perms_text in rows:
        ids, perms = _decode(ids_text), _decode(perms_text)
        decoded.append((row_id, ids_text, perms_text, ids, perms))
        for value in (*(ids if isinstance(ids, list) else ()), *(perms if isinstance(perms, dict) else ())):
            canonical = _canonical(value)
            if canonical is not None:
                candidates.add(canonical)
    existing = _existing(conn, principals, candidates)

    set_perms = f", {perms_col} = CAST(:perms AS json)" if perms_col else ""
    same_perms = f" AND CAST({perms_col} AS text) IS NOT DISTINCT FROM :old_perms" if perms_col else ""
    rewrite = sa.text(
        f"UPDATE {table} SET {ids_col} = CAST(:ids AS json){set_perms} "  # noqa: S608
        f"WHERE id = :id AND CAST({ids_col} AS text) IS NOT DISTINCT FROM :old_ids{same_perms}"
    )
    changed = 0
    for row_id, ids_text, perms_text, ids, perms in decoded:
        new_ids = [value for value in ids if key(value) in existing] if isinstance(ids, list) else ids
        new_perms = (
            {name: tier for name, tier in perms.items() if key(name) in existing} if isinstance(perms, dict) else perms
        )
        if new_ids == ids and new_perms == perms:
            continue
        params = {
            "id": row_id,
            "ids": json.dumps(new_ids) if new_ids else None,
            "old_ids": ids_text,
        }
        if perms_col:
            params["perms"] = json.dumps(new_perms) if new_perms else None
            params["old_perms"] = perms_text
        changed += conn.execute(rewrite, params).rowcount or 0
    return changed


def prune_in_batches(conn, targets: Sequence[_Target], batch_size: int = _BATCH_SIZE) -> int:
    """Prune every target *batch_size* rows at a time; returns how many rows changed.

    Meant for an autocommit connection, where every statement is its own
    transaction: ``lock_timeout`` is therefore set for the session and put back
    afterwards (``SET LOCAL`` would not outlive the statement).
    """
    previous = conn.execute(sa.text("SELECT current_setting('lock_timeout')")).scalar()
    conn.execute(sa.text("SELECT set_config('lock_timeout', :t, false)"), {"t": DEFAULT_LOCK_TIMEOUT})
    total = 0
    try:
        for target in targets:
            changed = 0
            after = None
            while rows := _batch(conn, target, after, batch_size):
                changed += _rewrite(conn, target, rows)
                after = rows[-1][0]
            if changed:
                logger.info("Pruned %s naming no one on %d %s row(s)", target[1], changed, target[0])
            total += changed
    finally:
        conn.execute(sa.text("SELECT set_config('lock_timeout', :t, false)"), {"t": previous})
    return total


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    present = set(inspector.get_table_names())

    targets: list[_Target] = []
    for table in _TABLES:
        # Fresh installs run create_all and stamp head: no grant to prune.
        if table not in present:
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        for ids_col, perms_col, principals, canonicalise in _GRANTS:
            if ids_col in columns:
                targets.append((table, ids_col, perms_col if perms_col in columns else None, principals, canonicalise))
    if not targets:
        return

    with op.get_context().autocommit_block():
        prune_in_batches(op.get_bind(), targets, _BATCH_SIZE)


def downgrade() -> None:
    # The ids removed named no user or group: there is nothing to put back.
    pass
