"""the organization profile field ``tech_stack`` is named ``software`` (#1495, last slice)

Sam, 24 Nov 2026: nothing is released, so a retired name keeps no alias. The
import and sync section and the category were renamed by ``s1495swsection01``;
this moves the stored profile field itself:

* ``organization_context_versions.profile``: the key ``tech_stack`` becomes
  ``software`` (its rows unchanged; a profile holding both keeps ``software``
  and drops the old one); ``onboarding_skipped`` names ``software``.
* ``organization_context_versions.sources`` (the provenance): a ``tech_stack``
  entry, or one under the path ``tech_stack.`` / ``tech_stack[``, is renamed.
* ``organization_context_proposals`` still pending: a proposal of the field
  ``tech_stack`` proposes ``software``.

The per-asset and per-group product lists (``assets_exposure[].tech_stack``,
``asset_groups[].tech_stack``) are not this field and are left as they are.

Data only, no DDL, a no-op on re-run. The downgrade renames them back.

Revision ID: s1495swfield01
Revises: r1844noaisys01
Create Date: 2026-11-24 16:10:00.000000
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "s1495swfield01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r1844noaisys01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "tech_stack"
_NEW = "software"


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _renamed_path(path: str, old: str, new: str) -> str:
    if path == old:
        return new
    for sep in (".", "["):
        if path.startswith(old + sep):
            return new + path[len(old) :]
    return path


def rename_profile(profile: Any, old: str = _OLD, new: str = _NEW) -> dict[str, Any] | None:
    """*profile* with the field *old* named *new*; ``None`` when nothing changes."""
    if not isinstance(profile, dict):
        return None
    skipped = profile.get("onboarding_skipped")
    if old not in profile and not (isinstance(skipped, list) and old in skipped):
        return None
    out = dict(profile)
    rows = out.pop(old, None)
    if new not in out and rows is not None:
        out[new] = rows
    if isinstance(skipped, list):
        out["onboarding_skipped"] = list(dict.fromkeys(new if name == old else name for name in skipped))
    return out


def rename_sources(sources: Any, old: str = _OLD, new: str = _NEW) -> dict[str, Any] | None:
    """The provenance with every path of *old* under *new*; ``None`` when nothing changes."""
    if not isinstance(sources, dict):
        return None
    out: dict[str, Any] = {}
    changed = False
    for path, value in sources.items():
        renamed = _renamed_path(str(path), old, new)
        changed = changed or renamed != path
        if renamed != path and renamed in sources:
            continue  # the new path already holds its own provenance
        out[renamed] = value
    return out if changed else None


def rename_proposals(proposals: Any, old: str = _OLD, new: str = _NEW) -> list[Any] | None:
    """Pending *proposals* with the field *old* proposed as *new*; ``None`` when nothing changes."""
    if not isinstance(proposals, list):
        return None
    out: list[Any] = []
    changed = False
    for proposal in proposals:
        if isinstance(proposal, dict) and isinstance(proposal.get("field"), str):
            field = _renamed_path(proposal["field"], old, new)
            if field != proposal["field"]:
                proposal = {**proposal, "field": field}
                changed = True
        out.append(proposal)
    return out if changed else None


def _move(old: str, new: str) -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    inspector = sa.inspect(conn)
    needle = f'%"{old}%'
    if inspector.has_table("organization_context_versions"):
        rows = conn.execute(
            sa.text(
                "SELECT id, profile, sources FROM organization_context_versions "
                "WHERE profile::text LIKE :needle OR sources::text LIKE :needle"
            ),
            {"needle": needle},
        ).all()
        for row_id, profile, sources in rows:
            new_profile = rename_profile(_json(profile), old, new)
            new_sources = rename_sources(_json(sources), old, new)
            if new_profile is None and new_sources is None:
                continue
            conn.execute(
                sa.text(
                    "UPDATE organization_context_versions SET profile = CAST(:profile AS jsonb), "
                    "sources = CAST(:sources AS jsonb) WHERE id = :id"
                ),
                {
                    "profile": json.dumps(new_profile if new_profile is not None else _json(profile)),
                    "sources": None
                    if new_sources is None and sources is None
                    else json.dumps(new_sources if new_sources is not None else _json(sources)),
                    "id": row_id,
                },
            )
    if inspector.has_table("organization_context_proposals"):
        for row_id, proposals in conn.execute(
            sa.text(
                "SELECT id, proposals FROM organization_context_proposals "
                "WHERE status = 'pending' AND proposals::text LIKE :needle"
            ),
            {"needle": needle},
        ).all():
            renamed = rename_proposals(_json(proposals), old, new)
            if renamed is None:
                continue
            conn.execute(
                sa.text("UPDATE organization_context_proposals SET proposals = CAST(:proposals AS jsonb) WHERE id = :id"),
                {"proposals": json.dumps(renamed), "id": row_id},
            )


def upgrade() -> None:
    _move(_OLD, _NEW)


def downgrade() -> None:
    _move(_NEW, _OLD)
