"""rewrite the stored names of the OpenCRQ tools the provider renamed or removed

The OpenCRQ provider was aligned on the current OpenCRQ API: 11 tools changed
name and 17 left with no successor. Tool names are persisted, and nothing
rewrites them on its own:

* ``agents.tool_deny_list`` - the urgent one. A stale entry no longer matches
  the renamed tool (``apply_tool_deny_list`` matches literal names), so a tool
  an administrator blocked becomes callable again, with no error and no log
  line. It fails **open**.
* the values of ``agents.integration_allowed_tools``,
  ``assignments.integration_allowed_tools`` and ``agents.mcp_allowed_tools`` -
  a stale name in an allow list silently withholds the renamed tool.
* ``agents.prepare_tool_ref`` and ``agents.finalize_tool_ref`` - the pipeline
  tool of an agent, ``{"source": "integration", "provider": "opencrq",
  "tool_name": ...}``. A stale name no longer loads, and the stage errors.
* ``hitl_whitelist_overrides.tool_key`` - an administrator's decision about one
  tool's approval gating. A stale ``remove`` (re-gate this read tool) stops
  matching, so the renamed tool is exempt from approval again: it fails
  **open** too.
* ``tool_approval_preferences.tool_key`` - a user's standing "always allow". A
  stale row only makes the renamed tool prompt again.

A name may carry the ``__<slug>`` suffix a multi-instance binding appends; the
base name is what is matched and the suffix is kept.

Every rewrite is filtered to the rows that name a stale tool, so a re-run finds
nothing and writes nothing. Rewrites are bounded: rows are read and written in
keyset batches, and the migration takes no DDL lock.

* A renamed name already present in the same list is not added twice.
* A removed name is dropped. A list this empties stays ``[]``: an empty allow
  list restricts the provider to no tool, a missing one lifts every
  restriction, and the migration must never widen what an agent may call.
* A pipeline ref whose ``tool_name`` is renamed keeps the rest of the ref. One
  naming a removed tool is set to NULL: both stages read a falsy ref as "no
  tool to run", so the stage stops running a tool that no longer exists
  instead of failing on it. Only a ref whose ``provider`` is ``opencrq`` is
  read.
* On ``tool_key``, a removed name deletes its row. When a renamed row's target
  already exists (UNIQUE ``tool_key`` on the overrides; the active
  ``(user_id, tool_key)`` pair on the preferences), one of the two is deleted
  and the collision is printed. On the overrides the stricter decision stays: a
  ``remove`` (gate the tool) beats an ``add`` (exempt it), whichever name it
  was under, so the migration never exempts a tool an administrator gated. On
  the preferences both rows are the same standing approval, and the one under
  the new name stays. ``schema_fingerprint`` is left as it is.
* A row whose JSON PostgreSQL stores but this revision cannot read back stops
  the upgrade in ``tool_deny_list``, where leaving it would fail open, and is
  left unchanged elsewhere, where a stale name only withholds a tool.

``downgrade`` undoes the 11 renames. The removed names are not recreated: the
rows and entries dropped carried nothing to restore. A rename that would
collide keeps the row already under the old name and leaves the other as it is.

Revision ID: x5762ocqtool01
Revises: v5991rsupr01
Create Date: 2026-10-14 18:00:00.000000
"""
# ruff: noqa: S608 - the only values interpolated into these statements are
# the table and column names in the tuples below, this module's own constants;
# every tool name is passed as a bound parameter.

import json
from collections.abc import Callable, Iterator, Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "x5762ocqtool01"
down_revision: str | Sequence[str] | None = "v5991rsupr01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RENAMES: dict[str, str] = {
    "list_opencrq_assessments": "list_opencrq_compliance_assessments",
    "get_opencrq_assessment": "get_opencrq_compliance_assessment",
    "create_opencrq_assessment": "create_opencrq_compliance_assessment",
    "update_opencrq_assessment": "manage_opencrq_compliance_assessment",
    "update_opencrq_requirement_assessment": "update_opencrq_requirement_answer",
    "list_opencrq_controls": "list_opencrq_control_capabilities",
    "get_opencrq_control": "get_opencrq_control_capability",
    "update_opencrq_control_maturity": "update_opencrq_control_capability_assessment",
    "get_opencrq_control_maturity_history": "get_opencrq_control_capability_history",
    "get_opencrq_tenant_cve_posture": "list_opencrq_unique_findings",
    "list_opencrq_control_requirement_mappings": "list_opencrq_drata_controls",
}

_REMOVED: tuple[str, ...] = (
    "get_opencrq_control_settings",
    "update_opencrq_control_settings",
    "add_opencrq_assessment_evidence",
    "update_opencrq_assessment_evidence",
    "delete_opencrq_assessment_evidence",
    "list_opencrq_assessment_evidences",
    "get_opencrq_requirement_assessment_history",
    "get_opencrq_suggested_controls",
    "list_opencrq_attack_surfaces",
    "update_opencrq_attack_surface",
    "create_opencrq_loss_driver",
    "update_opencrq_loss_driver",
    "delete_opencrq_loss_drivers",
    "list_opencrq_asset_families",
    "manage_opencrq_asset_family",
    "get_opencrq_security_needs",
    "update_opencrq_security_needs",
)

#: Separator between a base tool name and the per-instance slug.
_INSTANCE_SUFFIX_SEP = "__"

_PROVIDER = "opencrq"

#: JSON arrays of tool names. ``agents.tool_deny_list`` fails open when stale.
_TOOL_NAME_ARRAY: tuple[tuple[str, str], ...] = (("agents", "tool_deny_list"),)

#: JSON objects whose values are arrays of tool names. ``mcp_allowed_tools`` is
#: keyed by MCP server UUID, so every value is read; the other two are keyed by
#: provider, and only the ``opencrq`` keys (``opencrq`` and
#: ``opencrq@<integration id>``) hold these names.
_TOOL_NAME_OBJECT_VALUES: tuple[tuple[str, str], ...] = (
    ("agents", "integration_allowed_tools"),
    ("agents", "mcp_allowed_tools"),
    ("assignments", "integration_allowed_tools"),
)
_PROVIDER_KEYED: frozenset[tuple[str, str]] = frozenset(
    {("agents", "integration_allowed_tools"), ("assignments", "integration_allowed_tools")}
)

#: JSON objects ``{"source", "provider", "tool_name", ...}`` naming the one tool
#: a pipeline stage runs. NULL, or any falsy value, means the stage has none.
_TOOL_REF: tuple[tuple[str, str], ...] = (
    ("agents", "prepare_tool_ref"),
    ("agents", "finalize_tool_ref"),
)

#: Columns holding one tool name per row.
_OVERRIDES = ("hitl_whitelist_overrides", "tool_key")
_PREFERENCES = ("tool_approval_preferences", "tool_key")

#: The override ``action`` that gates a tool; ``add`` exempts it.
_GATING = "remove"

#: Columns whose stale entry fails open when it cannot be rewritten (a stale
#: pipeline ref fails closed: the stage errors).
_DENYING: frozenset[tuple[str, str]] = frozenset({("agents", "tool_deny_list")})

_BATCH = 500

#: ``_convert`` and the rewriters return it for "remove this": the name from a
#: list, the whole value (set to NULL) for a pipeline ref.
_DROP = object()


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        ).scalar()
    )


def _column_exists(conn, table: str, column: str) -> bool:
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


def _ready(conn, table: str, column: str) -> bool:
    # Fresh install: create_all builds the schema and stamps head, so there
    # are no rows to carry over.
    return _table_exists(conn, table) and _column_exists(conn, table, column)


def _convert(name: str, renames: dict[str, str], removed: Sequence[str]):
    """*name* renamed, ``_DROP`` when removed, itself when untouched."""
    base, sep, slug = name.partition(_INSTANCE_SUFFIX_SEP)
    if base in removed:
        return _DROP
    if base in renames:
        return renames[base] + sep + slug
    return name


def _rewrite_list(items: list, renames: dict[str, str], removed: Sequence[str]) -> list | None:
    """*items* with the stale names renamed or dropped, ``None`` when nothing changed."""
    present = {item for item in items if isinstance(item, str)}
    rewritten: list = []
    added: set[str] = set()
    changed = False
    for item in items:
        new = _convert(item, renames, removed) if isinstance(item, str) else item
        if new is _DROP:
            changed = True
            continue
        if new != item:
            changed = True
            # The target is already listed: adding it again would duplicate it.
            if new in present or new in added:
                continue
            added.add(new)
        rewritten.append(new)
    return rewritten if changed else None


def _rewrite_object_values(
    value, renames: dict[str, str], removed: Sequence[str], key_ok: Callable[[str], bool]
) -> dict | None:
    """*value* with the stale names of the arrays it maps to rewritten, ``None`` when unchanged.

    An array this empties stays ``[]``: an empty allow list restricts to no
    tool, so deleting the key would lift the restriction.
    """
    if not isinstance(value, dict):
        return None
    rewritten: dict = {}
    changed = False
    for key, items in value.items():
        if isinstance(items, list) and key_ok(key):
            new_items = _rewrite_list(items, renames, removed)
            if new_items is not None:
                rewritten[key] = new_items
                changed = True
                continue
        rewritten[key] = items
    return rewritten if changed else None


def _rewrite_tool_ref(value, renames: dict[str, str], removed: Sequence[str]):
    """*value*, a pipeline tool ref, with its tool renamed; ``_DROP`` when it names a removed tool.

    ``None`` when nothing changes. Only an ``opencrq`` provider's ref is read.
    """
    if not isinstance(value, dict) or value.get("provider") != _PROVIDER:
        return None
    name = value.get("tool_name")
    if not isinstance(name, str):
        return None
    new = _convert(name, renames, removed)
    if new is _DROP:
        return _DROP
    return None if new == name else {**value, "tool_name": new}


def _batches(conn, table: str, columns: str, where: str, params: dict) -> Iterator:
    """Rows of *table* matching *where*, by ascending id, one bounded batch at a time.

    Keyset paging rather than "the rows that still match": a row left unchanged
    would otherwise be read forever.
    """
    after = None
    while True:
        paging = " AND id > :after" if after is not None else ""
        rows = conn.execute(
            sa.text(
                f"SELECT id, {columns} FROM {table} WHERE {where}{paging} "
                "ORDER BY id LIMIT :batch"
            ),
            {**params, "batch": _BATCH, **({"after": after} if after is not None else {})},
        ).all()
        if not rows:
            return
        yield from rows
        after = rows[-1][0]


def _names_pattern(renames: dict[str, str], removed: Sequence[str]) -> str:
    return "|".join(sorted({*renames, *removed}))


def _rewrite_json_column(
    conn,
    table: str,
    column: str,
    rewrite: Callable[[object], object | None],
    renames: dict[str, str],
    removed: Sequence[str],
    *,
    tool_ref: bool = False,
) -> None:
    if not _ready(conn, table, column):
        return
    # A tool name is a whole JSON string, optionally followed by ``__<slug>``;
    # in a ref it is the value of ``tool_name``.
    names = f'"({_names_pattern(renames, removed)})({_INSTANCE_SUFFIX_SEP}[^"]*)?"'
    pattern = f'"tool_name"\\s*:\\s*{names}' if tool_ref else names
    for row_id, stored in _batches(
        conn,
        table,
        f"CAST({column} AS text)",
        f"{column} IS NOT NULL AND CAST({column} AS text) ~ :pattern",
        {"pattern": pattern},
    ):
        try:
            rewritten = rewrite(json.loads(stored))
            payload = (
                None
                if rewritten is None or rewritten is _DROP
                else json.dumps(rewritten, allow_nan=False)
            )
        except (ValueError, RecursionError):
            if (table, column) in _DENYING:
                raise RuntimeError(
                    f"Cannot rewrite the tool names of {table}.{column}: row {row_id} cannot be "
                    f"read, and left as it is its entries would stop withholding the renamed "
                    f"tools. Fix or clear the column of this row, then re-run the upgrade."
                ) from None
            print(  # noqa: T201 - migrations report through stdout
                f"opencrq tools: left {table}.{column} of row {row_id} unchanged: it cannot be "
                "read. Left under the old names it only withholds a tool, never grants one."
            )
            continue
        if rewritten is _DROP:
            conn.execute(sa.text(f"UPDATE {table} SET {column} = NULL WHERE id = :id"), {"id": row_id})
        elif payload is not None:
            conn.execute(
                sa.text(f"UPDATE {table} SET {column} = CAST(:value AS json) WHERE id = :id"),
                {"value": payload, "id": row_id},
            )


def _rekey_overrides(
    conn, renames: dict[str, str], removed: Sequence[str], *, drop_on_collision: bool
) -> None:
    table, column = _OVERRIDES
    if not _ready(conn, table, column):
        return
    pattern = f"^({_names_pattern(renames, removed)})({_INSTANCE_SUFFIX_SEP}.*)?$"
    for row_id, key, action in _batches(
        conn, table, f"{column}, action", f"{column} ~ :pattern", {"pattern": pattern}
    ):
        new = _convert(key, renames, removed)
        if new is _DROP:
            conn.execute(sa.text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
        elif new != key:
            target = conn.execute(
                sa.text(f"SELECT action, id FROM {table} WHERE {column} = :new"), {"new": new}
            ).first()
            if target is None:
                conn.execute(
                    sa.text(f"UPDATE {table} SET {column} = :new WHERE id = :id"),
                    {"new": new, "id": row_id},
                )
            elif drop_on_collision:
                target_action, target_id = target
                if action == _GATING and target_action != _GATING:
                    conn.execute(sa.text(f"DELETE FROM {table} WHERE id = :id"), {"id": target_id})
                    conn.execute(
                        sa.text(f"UPDATE {table} SET {column} = :new WHERE id = :id"),
                        {"new": new, "id": row_id},
                    )
                    kept = action
                else:
                    conn.execute(sa.text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
                    kept = target_action
                print(  # noqa: T201 - migrations report through stdout
                    f"opencrq tools: {table} held a decision under both {key} and {new}; "
                    f"kept the stricter one ({kept}) under {new}."
                )


def _rekey_preferences(
    conn, renames: dict[str, str], removed: Sequence[str], *, drop_on_collision: bool
) -> None:
    table, column = _PREFERENCES
    if not _ready(conn, table, column):
        return
    pattern = f"^({_names_pattern(renames, removed)})({_INSTANCE_SUFFIX_SEP}.*)?$"
    for row_id, key, user_id, revoked_at in _batches(
        conn, table, f"{column}, user_id, revoked_at", f"{column} ~ :pattern", {"pattern": pattern}
    ):
        new = _convert(key, renames, removed)
        if new is _DROP:
            conn.execute(sa.text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
        elif new != key:
            # Only an active row is bound by the unique (user_id, tool_key) index.
            taken = revoked_at is None and conn.execute(
                sa.text(
                    f"SELECT 1 FROM {table} WHERE user_id = :user_id AND {column} = :new "
                    f"AND revoked_at IS NULL"
                ),
                {"user_id": user_id, "new": new},
            ).scalar()
            if not taken:
                conn.execute(
                    sa.text(f"UPDATE {table} SET {column} = :new WHERE id = :id"),
                    {"new": new, "id": row_id},
                )
            elif drop_on_collision:
                conn.execute(sa.text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
                print(  # noqa: T201 - migrations report through stdout
                    f"opencrq tools: user {user_id} held a standing approval under both {key} "
                    f"and {new}; kept the one under {new}."
                )


def _migrate(
    conn, renames: dict[str, str], removed: Sequence[str], *, drop_on_collision: bool
) -> None:
    for table, column in _TOOL_NAME_ARRAY:
        _rewrite_json_column(
            conn,
            table,
            column,
            lambda value: _rewrite_list(value, renames, removed) if isinstance(value, list) else None,
            renames,
            removed,
        )
    for table, column in _TOOL_NAME_OBJECT_VALUES:
        provider_keyed = (table, column) in _PROVIDER_KEYED
        _rewrite_json_column(
            conn,
            table,
            column,
            lambda value, provider_keyed=provider_keyed: _rewrite_object_values(
                value,
                renames,
                removed,
                lambda key: not provider_keyed
                or key == _PROVIDER
                or key.startswith(f"{_PROVIDER}@"),
            ),
            renames,
            removed,
        )
    for table, column in _TOOL_REF:
        _rewrite_json_column(
            conn,
            table,
            column,
            lambda value: _rewrite_tool_ref(value, renames, removed),
            renames,
            removed,
            tool_ref=True,
        )
    _rekey_overrides(conn, renames, removed, drop_on_collision=drop_on_collision)
    _rekey_preferences(conn, renames, removed, drop_on_collision=drop_on_collision)


def upgrade() -> None:
    _migrate(op.get_bind(), _RENAMES, _REMOVED, drop_on_collision=True)


def downgrade() -> None:
    _migrate(
        op.get_bind(),
        {new: old for old, new in _RENAMES.items()},
        (),
        drop_on_collision=False,
    )
