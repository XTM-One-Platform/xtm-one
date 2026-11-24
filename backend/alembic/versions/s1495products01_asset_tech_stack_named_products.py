"""the per-asset and per-group product list ``tech_stack`` is named ``products`` (#1495, part 4b)

The profile field ``tech_stack`` became ``software`` in ``s1495swfield01``; the
list of product names each asset and each asset group carries kept the old
name. Sam, 24 Nov 2026: nothing is released, so the retired name keeps no
alias. This revision renames what is stored:

* ``organization_context_versions.profile``: the key ``tech_stack`` of every
  row of ``assets_exposure`` and ``asset_groups`` becomes ``products``;
  ``sources`` (the provenance): a path ``assets_exposure[...].tech_stack`` or
  ``asset_groups[...].tech_stack`` is renamed;
* ``organization_context_proposals`` still pending: the same rows, and a
  proposal field path, renamed;
* ``organization_asset_records.fields`` and ``organization_asset_groups.fields``
  (the inventory store): the top-level key ``tech_stack`` becomes ``products``;
* ``ctem_signals.decision``: the scope gate's ``reconcile.tech_stack`` becomes
  ``reconcile.products``.

A row that already holds ``products`` keeps it and drops the old key. The
asset change log (``organization_asset_changes``) is history and is left as
written. Each JSON rewrite pages by ``id`` in batches of ``_BATCH`` rows
(keyset), so no statement reads an unbounded set (backend rule 4). Data only,
no DDL, a no-op on re-run; the downgrade renames everything back.

Revision ID: s1495products01
Revises: s1844primasset01
Create Date: 2026-11-24 22:00:00.000000
"""

import json
from collections.abc import Callable, Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "s1495products01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "s1844primasset01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "tech_stack"
_NEW = "products"
_BATCH = 500
#: The profile lists whose rows carry the product list.
ROW_LISTS: tuple[str, ...] = ("assets_exposure", "asset_groups")


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def rename_key(row: Any, old: str = _OLD, new: str = _NEW) -> Any:
    """*row* with its key *old* named *new* (a row holding both keeps *new*); other values as they are."""
    if not isinstance(row, dict) or old not in row:
        return row
    return {(new if key == old else key): value for key, value in row.items() if not (key == old and new in row)}


def rename_profile(profile: Any, old: str = _OLD, new: str = _NEW) -> Any:
    """*profile* with *old* named *new* in every row of :data:`ROW_LISTS`."""
    if not isinstance(profile, dict):
        return profile
    out = dict(profile)
    for name in ROW_LISTS:
        rows = out.get(name)
        if isinstance(rows, list):
            out[name] = [rename_key(row, old, new) for row in rows]
    return out


def rename_path(path: str, old: str = _OLD, new: str = _NEW) -> str:
    """A provenance or proposal path under a row list with its last segment *old* named *new*."""
    if not any(path.startswith(name + "[") or path.startswith(name + ".") for name in ROW_LISTS):
        return path
    head, *rest = path.split(".")
    renamed = [new + part[len(old) :] if part == old or part.startswith(old + "[") else part for part in rest]
    return ".".join([head, *renamed])


def rename_sources(sources: Any, old: str = _OLD, new: str = _NEW) -> Any:
    if not isinstance(sources, dict):
        return sources
    out: dict[str, Any] = {}
    for path, value in sources.items():
        renamed = rename_path(str(path), old, new)
        if renamed != path and renamed in sources:
            continue  # the new path already holds its own provenance
        out[renamed] = value
    return out


def rename_proposals(proposals: Any, old: str = _OLD, new: str = _NEW) -> Any:
    if not isinstance(proposals, list):
        return proposals
    out: list[Any] = []
    for proposal in proposals:
        if isinstance(proposal, dict):
            proposal = dict(proposal)
            if isinstance(proposal.get("field"), str):
                proposal["field"] = rename_path(proposal["field"], old, new)
            if proposal.get("field") in ROW_LISTS and isinstance(proposal.get("value"), list):
                proposal["value"] = [rename_key(row, old, new) for row in proposal["value"]]
        out.append(proposal)
    return out


def rename_decision(decision: Any, old: str = _OLD, new: str = _NEW) -> Any:
    """A scope gate decision with ``reconcile.<old>`` named ``reconcile.<new>``."""
    if not isinstance(decision, dict) or not isinstance(decision.get("reconcile"), dict):
        return decision
    return {**decision, "reconcile": rename_key(decision["reconcile"], old, new)}


def _rewrite(
    conn: sa.engine.Connection,
    table: str,
    columns: dict[str, Callable[[Any], Any]],
    cast: str,
    old: str,
    where: str = "",
) -> None:
    """Every row of *table* whose *columns* name *old*, rewritten by its column function, in ``id`` pages."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return
    present = {c["name"] for c in inspector.get_columns(table)}
    cols = [c for c in columns if c in present]
    if not cols:
        return
    match = " OR ".join(f"{c}::text LIKE :needle" for c in cols)
    head = f"SELECT id, {', '.join(cols)} FROM {table} WHERE ({match}){where}"  # noqa: S608 - constant names
    sets = ", ".join(f"{c} = CAST(:{c} AS {cast})" for c in cols)
    update = sa.text(f"UPDATE {table} SET {sets} WHERE id = :id")  # noqa: S608 - constant names
    after: Any = None
    while True:
        page = head + (" AND id > :after" if after is not None else "") + " ORDER BY id LIMIT :limit"
        params: dict[str, Any] = {"needle": f'%"{old}"%', "limit": _BATCH}
        if after is not None:
            params["after"] = after
        rows = conn.execute(sa.text(page), params).all()
        if not rows:
            return
        for row in rows:
            values = {c: _json(v) for c, v in zip(cols, row[1:], strict=True)}
            new = {c: columns[c](v) for c, v in values.items()}
            if new != values:
                conn.execute(update, {**{c: None if v is None else json.dumps(v) for c, v in new.items()}, "id": row[0]})
        after = rows[-1][0]


def _move(old: str, new: str) -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    _rewrite(
        conn,
        "organization_context_versions",
        {"profile": lambda v: rename_profile(v, old, new), "sources": lambda v: rename_sources(v, old, new)},
        "jsonb",
        old,
    )
    _rewrite(
        conn,
        "organization_context_proposals",
        {"proposals": lambda v: rename_proposals(v, old, new)},
        "jsonb",
        old,
        " AND status = 'pending'",
    )
    for table in ("organization_asset_records", "organization_asset_groups"):
        _rewrite(conn, table, {"fields": lambda v: rename_key(v, old, new)}, "jsonb", old)
    _rewrite(conn, "ctem_signals", {"decision": lambda v: rename_decision(v, old, new)}, "json", old)


def upgrade() -> None:
    _move(_OLD, _NEW)


def downgrade() -> None:
    _move(_NEW, _OLD)
