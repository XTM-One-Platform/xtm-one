"""crown jewels are primary assets everywhere, no alias (#1844, slice 2)

The organization's primary assets (OpenCRQ's word) were stored under the
``crown_jewels`` names since #1134; #1591 renamed the category and kept the
stored names. This revision renames what is stored, so no reader keeps an
alias:

- the link table ``organization_crown_jewel_links`` is
  ``organization_primary_asset_links``, its ``crown_jewel_key`` column
  ``primary_asset_key`` and its three indexes renamed with it (metadata only,
  no rewrite);
- every saved profile version, its ``sources`` and every pending proposal: the
  key ``crown_jewels`` is ``primary_assets`` (rows of both merged, the
  ``primary_assets`` row kept on a name clash), ``min_crown_jewel_criticality``
  is ``min_primary_asset_criticality``, the case attention weight
  ``crown_jewel`` is ``primary_asset``, and ``crown_jewels`` in a list of
  names (``onboarding_skipped``, a proposal's ``field``) is ``primary_assets``;
- the scheduled syncs of section ``crown_jewels`` are of section
  ``primary_assets``;
- the stored category selections (platform settings, agents, assignments);
- the scope gate decisions of the CTEM signals: the reason code
  ``crown_jewel_below_policy``, the source kind ``crown_jewel``, the rule
  ``min_crown_jewel_criticality`` and ``crown_jewels_named``.

Data and renames only; the downgrade names everything back (a profile that
held both lists keeps the merged one under ``crown_jewels``).

Revision ID: s1844primasset01
Revises: r1844noaisys01
Create Date: 2026-11-24 16:30:00.000000
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "s1844primasset01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r1844noaisys01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TABLE = "organization_crown_jewel_links"
_NEW_TABLE = "organization_primary_asset_links"
_OLD_COLUMN = "crown_jewel_key"
_NEW_COLUMN = "primary_asset_key"
_INDEXES = ("jewel", "record", "group")

#: Stored names, old -> new: dict keys and the exact strings of a list or a value.
NAMES: dict[str, str] = {
    "crown_jewels": "primary_assets",
    "crown_jewel": "primary_asset",
    "min_crown_jewel_criticality": "min_primary_asset_criticality",
    "crown_jewel_below_policy": "primary_asset_below_policy",
    "crown_jewels_named": "primary_assets_named",
}
#: The list whose rows of both names are merged rather than overwritten.
_MERGED = "primary_assets"

_SELECTION_TABLES = ("platform_settings", "agents", "assignments")


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _row_name(row: Any) -> str:
    return str(row.get("name") or "").strip().casefold() if isinstance(row, dict) else ""


def _merge(kept: list[Any], other: list[Any]) -> list[Any]:
    """*kept* then the rows of *other* whose name *kept* does not hold."""
    names = {_row_name(r) for r in kept if _row_name(r)}
    return kept + [r for r in other if not _row_name(r) or _row_name(r) not in names]


def rename(value: Any, names: dict[str, str] = NAMES) -> Any:
    """*value* with every stored name of *names* renamed: dict keys, list strings and string values."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            new_key = names.get(key, key)
            item = rename(item, names)
            if new_key in out and new_key == names.get("crown_jewels", "") == _MERGED:
                # the rows already under the new name come first and win a name clash
                first, second = (item, out[new_key]) if key == new_key else (out[new_key], item)
                if isinstance(first, list) and isinstance(second, list):
                    out[new_key] = _merge(first, second)
                    continue
            if new_key in out and key != new_key:
                continue  # the row already under the new name wins
            out[new_key] = item
        return out
    if isinstance(value, list):
        return [rename(item, names) for item in value]
    if isinstance(value, str):
        return names.get(value, value)
    return value


def _needle_clause(column: str, names: dict[str, str]) -> str:
    return " OR ".join(f"{column}::text LIKE :n{i}" for i in range(len(names)))


def _needles(names: dict[str, str]) -> dict[str, str]:
    return {f"n{i}": f'%"{old}"%' for i, old in enumerate(names)}


def _rewrite_json(
    conn: sa.engine.Connection, table: str, columns: tuple[str, ...], names: dict[str, str], cast: str
) -> None:
    """Every row of *table* whose *columns* name an old name, rewritten with :func:`rename`."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return
    present = {c["name"] for c in inspector.get_columns(table)}
    cols = tuple(c for c in columns if c in present)
    if not cols:
        return
    where = " OR ".join(f"({_needle_clause(c, names)})" for c in cols)
    select = f"SELECT id, {', '.join(cols)} FROM {table} WHERE {where}"  # noqa: S608 - constant names
    for row in conn.execute(sa.text(select), _needles(names)).all():
        values = {c: _json(v) for c, v in zip(cols, row[1:], strict=True)}
        new = {c: rename(v, names) for c, v in values.items()}
        if new == values:
            continue
        sets = ", ".join(f"{c} = CAST(:{c} AS {cast})" for c in cols)
        params = {c: None if v is None else json.dumps(v) for c, v in new.items()}
        conn.execute(sa.text(f"UPDATE {table} SET {sets} WHERE id = :id"), {**params, "id": row[0]})  # noqa: S608


def _rename_sync_sections(conn: sa.engine.Connection, old: str, new: str) -> None:
    if sa.inspect(conn).has_table("organization_context_syncs"):
        conn.execute(
            sa.text("UPDATE organization_context_syncs SET section = :new WHERE section = :old"),
            {"old": old, "new": new},
        )


def _rename_selections(conn: sa.engine.Connection, old: str, new: str) -> None:
    inspector = sa.inspect(conn)
    for table in _SELECTION_TABLES:
        if not inspector.has_table(table):
            continue
        if "organization_context_categories" not in {c["name"] for c in inspector.get_columns(table)}:
            continue
        rows = conn.execute(
            sa.text(
                f"SELECT id, organization_context_categories FROM {table} "  # noqa: S608 - a constant table name
                "WHERE organization_context_categories::text LIKE :needle"
            ),
            {"needle": f'%"{old}"%'},
        ).all()
        for row_id, value in rows:
            names = _json(value)
            if not isinstance(names, list):
                continue
            renamed = list(dict.fromkeys(new if name == old else name for name in names))
            conn.execute(
                sa.text(
                    f"UPDATE {table} SET organization_context_categories = CAST(:value AS json) "  # noqa: S608
                    "WHERE id = :id"
                ),
                {"value": json.dumps(renamed), "id": row_id},
            )


def _rename_table(conn: sa.engine.Connection, old: str, new: str, old_column: str, new_column: str) -> None:
    inspector = sa.inspect(conn)
    if not inspector.has_table(old) or inspector.has_table(new):
        return
    terminate_lock_holders(conn, old)
    set_lock_timeout(conn)
    op.rename_table(old, new)
    if old_column in {c["name"] for c in sa.inspect(conn).get_columns(new)}:
        op.alter_column(new, old_column, new_column_name=new_column)
    old_prefix, new_prefix = f"ix_{old}_", f"ix_{new}_"
    for suffix in _INDEXES:
        conn.execute(sa.text(f'ALTER INDEX IF EXISTS "{old_prefix}{suffix}" RENAME TO "{new_prefix}{suffix}"'))


def _data(conn: sa.engine.Connection, names: dict[str, str]) -> None:
    _rewrite_json(conn, "organization_context_versions", ("profile", "sources"), names, "jsonb")
    _rewrite_json(conn, "organization_context_proposals", ("proposals",), names, "jsonb")
    _rewrite_json(conn, "ctem_signals", ("decision",), names, "json")
    old_section, new_section = next(iter(names.items()))
    _rename_sync_sections(conn, old_section, new_section)
    _rename_selections(conn, old_section, new_section)


def upgrade() -> None:
    conn = op.get_bind()
    _rename_table(conn, _OLD_TABLE, _NEW_TABLE, _OLD_COLUMN, _NEW_COLUMN)
    set_lock_timeout(conn)
    _data(conn, NAMES)


def downgrade() -> None:
    conn = op.get_bind()
    _rename_table(conn, _NEW_TABLE, _OLD_TABLE, _NEW_COLUMN, _OLD_COLUMN)
    set_lock_timeout(conn)
    _data(conn, {new: old for old, new in NAMES.items()})
