"""the organization profile has no ai_systems: they are assets of category ai_target (#1844, S1)

Since #1501 an AI system is an asset of category ``ai_target``; until now the
profile model still read a stored ``ai_systems`` list into the assets on every
parse and every reader, import, sync and tool still named the section. The
product is not released: the alias goes, and the stored data moves once here.

* ``organization_context_versions.profile``: each ``ai_systems`` row becomes an
  ``ai_target`` asset of ``assets_exposure`` (the mapping #1501 applied on read:
  the kind becomes the OpenAEV subcategory or the tag ``ai_kind:<kind>``, the
  exposure sets ``internet_facing`` and stays as the tag ``exposure:<value>``),
  unless an asset of the same source and id, or an AI target of the same name,
  already carries it; then the key is dropped. ``onboarding_skipped`` loses
  ``ai_systems``.
* ``organization_context_versions.sources`` (the provenance): an ``ai_systems``
  entry is dropped.
* ``organization_context_syncs``: a sync of section ``ai_systems`` becomes a sync
  of section ``assets`` filtered on the category ``ai_target`` (the OpenAEV
  assets read includes the AI targets for that filter).
* ``organization_context_proposals`` still pending: a proposal of the field
  ``ai_systems`` is dropped (an empty proposal is rejected).
* ``assignments``: the AI security fleet's built-in "AI system registered"
  becomes "AI target added" (work-item prefix ``ctem:f3:ai-target:``) in
  place, so an administrator's switch and run-as survive; the seed would
  otherwise delete it as stale and create the new one switched off.

Data only, no DDL, a no-op on re-run. The downgrade is a no-op: the rows are
assets, which every version reads.

Revision ID: r1844noaisys01
Revises: w1748wigroups01
Create Date: 2026-11-24 11:00:00.000000
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "r1844noaisys01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "w1748wigroups01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEY = "ai_systems"
_KIND_SUBCATEGORY = {"agent": "ai_agent", "model_api": "llm_model"}
_EXPOSURE_FACING = {"public": "yes", "internal": "no", "partner": "unknown"}
#: The AI security fleet's work-item trigger, frozen here (a migration never imports the seed).
_F3_SLUG = "ctem-ai-security-orchestrator"
_F3_OLD = ("AI system registered", "ctem:f3:ai-system:")
_F3_NEW = ("AI target added", "ctem:f3:ai-target:")


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def asset_row(raw: Any) -> dict[str, Any] | None:
    """A stored AI system as an ``ai_target`` asset row (``None`` when it has no name)."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:200]
    if not name:
        return None
    kind = str(raw.get("kind") or "assistant").strip().lower()
    exposure = str(raw.get("exposure") or "internal").strip().lower()
    if exposure not in _EXPOSURE_FACING:
        exposure = "internal"
    source = str(raw.get("source") or "").strip().lower()
    external_id = str(raw.get("external_id") or "").strip()[:200]
    legacy_id = str(raw.get("openaev_asset_id") or "").strip()[:200]
    if legacy_id and not external_id:
        source, external_id = "openaev", legacy_id
    tags = [f"exposure:{exposure}"]
    subcategory = _KIND_SUBCATEGORY.get(kind, "")
    if not subcategory:
        tags.insert(0, f"ai_kind:{kind}")
    row: dict[str, Any] = {
        "name": name,
        "category": "ai_target",
        "subcategory": subcategory,
        "internet_facing": _EXPOSURE_FACING[exposure],
        "owner": str(raw.get("owner") or "").strip()[:200],
        "tags": tags,
        "source": source or "manual",
        "external_id": external_id,
        "sync_id": str(raw.get("sync_id") or "").strip()[:64],
    }
    return {k: v for k, v in row.items() if v not in ("", None) or k in ("name", "category")}


def _carried(assets: list[Any], row: dict[str, Any]) -> bool:
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        source = str(asset.get("source") or "manual").strip().lower()
        if row.get("external_id") and source == row["source"] and asset.get("external_id") == row["external_id"]:
            return True
        if (
            asset.get("category") == "ai_target"
            and str(asset.get("name") or "").strip().casefold() == row["name"].casefold()
        ):
            return True
    return False


def migrate_profile(profile: Any) -> dict[str, Any] | None:
    """*profile* without ``ai_systems``, its rows moved into the assets; ``None`` when nothing changes."""
    if not isinstance(profile, dict):
        return None
    skipped = profile.get("onboarding_skipped")
    if _KEY not in profile and not (isinstance(skipped, list) and _KEY in skipped):
        return None
    out = dict(profile)
    legacy = out.pop(_KEY, None) or []
    assets = list(out.get("assets_exposure") or [])
    for raw in legacy if isinstance(legacy, list) else []:
        row = asset_row(raw)
        if row is not None and not _carried(assets, row):
            assets.append(row)
    if legacy:
        out["assets_exposure"] = assets
    if isinstance(skipped, list):
        out["onboarding_skipped"] = [name for name in skipped if name != _KEY]
    return out


def migrate_sync(section: str, filters: Any) -> tuple[str, dict[str, Any]] | None:
    """An ``ai_systems`` sync as an ``assets`` sync on the category ``ai_target``; ``None`` for another section."""
    if section != _KEY:
        return None
    out = dict(filters) if isinstance(filters, dict) else {}
    out["category"] = ["ai_target"]
    return "assets", out


def migrate_proposals(proposals: Any) -> list[Any] | None:
    """*proposals* without the ``ai_systems`` field; ``None`` when nothing changes."""
    if not isinstance(proposals, list):
        return None
    kept = [p for p in proposals if not (isinstance(p, dict) and p.get("field") == _KEY)]
    return kept if len(kept) != len(proposals) else None


def upgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    inspector = sa.inspect(conn)
    if inspector.has_table("organization_context_versions"):
        rows = conn.execute(
            sa.text(
                "SELECT id, profile, sources FROM organization_context_versions "
                "WHERE profile::text LIKE :needle OR sources::text LIKE :needle"
            ),
            {"needle": f'%"{_KEY}"%'},
        ).all()
        for row_id, profile, sources in rows:
            new_profile = migrate_profile(_json(profile))
            new_sources = _json(sources)
            sources_changed = isinstance(new_sources, dict) and _KEY in new_sources
            if sources_changed:
                new_sources = {k: v for k, v in new_sources.items() if k != _KEY}
            if new_profile is None and not sources_changed:
                continue
            conn.execute(
                sa.text(
                    "UPDATE organization_context_versions SET profile = CAST(:profile AS jsonb), "
                    "sources = CAST(:sources AS jsonb) WHERE id = :id"
                ),
                {
                    "profile": json.dumps(new_profile if new_profile is not None else _json(profile)),
                    "sources": None if new_sources is None else json.dumps(new_sources),
                    "id": row_id,
                },
            )
    if inspector.has_table("organization_context_syncs"):
        for row_id, section, filters in conn.execute(
            sa.text("SELECT id, section, filters FROM organization_context_syncs WHERE section = :key"),
            {"key": _KEY},
        ).all():
            moved = migrate_sync(section, _json(filters))
            if moved is None:
                continue
            conn.execute(
                sa.text(
                    "UPDATE organization_context_syncs SET section = :section, "
                    "filters = CAST(:filters AS jsonb) WHERE id = :id"
                ),
                {"section": moved[0], "filters": json.dumps(moved[1]), "id": row_id},
            )
    if inspector.has_table("organization_context_proposals"):
        for row_id, proposals in conn.execute(
            sa.text(
                "SELECT id, proposals FROM organization_context_proposals "
                "WHERE status = 'pending' AND proposals::text LIKE :needle"
            ),
            {"needle": f'%"{_KEY}"%'},
        ).all():
            kept = migrate_proposals(_json(proposals))
            if kept is None:
                continue
            conn.execute(
                sa.text(
                    "UPDATE organization_context_proposals SET proposals = CAST(:proposals AS jsonb), "
                    "status = CASE WHEN :empty THEN 'rejected' ELSE status END WHERE id = :id"
                ),
                {"proposals": json.dumps(kept), "empty": not kept, "id": row_id},
            )
    if inspector.has_table("assignments") and inspector.has_table("agents"):
        conn.execute(
            sa.text(
                "UPDATE assignments AS a SET name = :new_name, "
                "trigger_config = CAST(jsonb_set(COALESCE(CAST(a.trigger_config AS jsonb), '{}'::jsonb), "
                "'{correlation_prefix}', to_jsonb(CAST(:new_prefix AS text))) AS json) "
                "FROM agents AS g WHERE a.agent_id = g.id AND g.is_builtin = true AND g.slug = :slug "
                "AND a.name = :old_name AND NOT EXISTS (SELECT 1 FROM assignments AS b "
                "WHERE b.agent_id = a.agent_id AND b.name = :new_name)"
            ),
            {"slug": _F3_SLUG, "old_name": _F3_OLD[0], "new_name": _F3_NEW[0], "new_prefix": _F3_NEW[1]},
        )


def downgrade() -> None:
    # The rows are assets of category ai_target, which every version reads.
    return
