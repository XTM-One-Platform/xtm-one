# SPDX-License-Identifier: Apache-2.0

"""The canonical asset store of the organization context (#1178, S2): build and read.

The identity engine (``organization_asset_identity``, S1) is pure; this module
persists its answer in ``models/organization_asset.py`` and answers the reads:

- :func:`rebuild_from_profile` turns the ``assets_exposure`` rows of an
  organization's current profile into canonical assets (one per real thing,
  with every source record, every field's per-source values and the merged
  software), replacing what the store held for that organization. The profile
  caps its rows at 5,000, so a rebuild is bounded.
- :func:`ensure_built` rebuilds only when the store was built from an older
  profile version (or never): the migration backfills nothing, the first read
  after a save builds the store, so an existing profile keeps working.
- :func:`list_assets`, :func:`get_asset` and :func:`assets_of_software` are the
  paginated reads of the API (``GET /api/v1/organization-context/assets``).

- :func:`record_decision` and :func:`remove_decision` keep an administrator's
  merge (records that are one asset) or split (two records never one asset);
  every rebuild applies them (``organization_asset_decisions``, S3).
- :func:`refresh_after_save` rebuilds the store in the transaction that saved a
  new profile version (a sync run, an agent's row upsert), so a read never pays
  for a sync's rows.

The profile stays the source of the rows; the store is its canonical view.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization_asset import (
    OrganizationAsset,
    OrganizationAssetDecision,
    OrganizationAssetSoftware,
    OrganizationAssetSource,
)
from app.models.organization_context import OrganizationContextVersion
from app.services import organization_asset_identity as identity

#: The most rows a page answers; a larger ``limit`` is clamped.
MAX_PAGE = 200


def _key(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _seen(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _cut(value: Any, size: int) -> str:
    return str(value or "").strip()[:size]


def canonical_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    precedence: Mapping[str, Sequence[str]] | None = None,
    decisions: identity.Decisions | None = None,
) -> list[dict[str, Any]]:
    """The canonical assets of profile *rows*: what :func:`rebuild_from_profile` writes (pure, testable).

    Each answer holds ``canonical_key``, ``name``, ``fields`` (field ->
    ``FieldValue.as_dict()``), ``merged_on``, ``possible_matches``,
    ``sources`` (one per record) and ``software`` (``merge_software``).
    """
    records = [identity.section_record("assets", row, index=i) for i, row in enumerate(rows)]
    out: list[dict[str, Any]] = []
    for thing in identity.match_records(records, decisions=decisions):
        fields = identity.resolve_fields(thing.records, precedence=precedence)
        name = fields.get("name")
        out.append(
            {
                "canonical_key": thing.id,
                "name": str(name.value if name else "")[:300],
                "fields": {k: v.as_dict() for k, v in fields.items()},
                "merged_on": thing.merged_on,
                "possible_matches": thing.possible_matches,
                "explanation": thing.explanation(),
                "sources": [
                    {
                        "source": r.source,
                        "external_id": r.external_id,
                        "identifiers": {k: [str(v) for v in vals if v] for k, vals in r.identifiers.items() if vals},
                        "fields": r.fields,
                        "last_seen": r.last_seen,
                    }
                    for r in thing.records
                ],
                "software": identity.merge_software(thing.records),
            }
        )
    return out


async def _current(db: AsyncSession, organization_id: uuid.UUID | None) -> OrganizationContextVersion | None:
    from app.services.organization_context import current_version

    return await current_version(db, organization_id)


async def rebuild_from_profile(db: AsyncSession, organization_id: uuid.UUID | None = None) -> int:
    """Replace the store of *organization_id* (the primary when ``None``) with its profile's canonical assets.

    Answers the number of canonical assets written (the caller commits). No
    profile yet: the store is emptied and 0 is answered.
    """
    version = await _current(db, organization_id)
    if version is None:
        if organization_id is not None:
            await db.execute(delete(OrganizationAsset).where(OrganizationAsset.organization_id == organization_id))
        return 0
    org = version.organization_id
    # Two first reads racing would both rebuild: the second waits for the first's transaction.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"organization_assets:{org}"})
    rows = [row for row in (version.profile or {}).get("assets_exposure") or [] if isinstance(row, Mapping)]
    things = canonical_rows(rows, decisions=await load_decisions(db, org))
    # An asset whose canonical key is unchanged keeps its id across rebuilds, so an id
    # read from the API before an unrelated profile save still answers afterwards.
    known: dict[str, uuid.UUID] = {
        key: asset_id
        for key, asset_id in (
            await db.execute(
                select(OrganizationAsset.canonical_key, OrganizationAsset.id).where(
                    OrganizationAsset.organization_id == org
                )
            )
        ).all()
    }
    # Children go with their asset (ON DELETE CASCADE).
    await db.execute(delete(OrganizationAsset).where(OrganizationAsset.organization_id == org))
    for thing in things:
        asset = OrganizationAsset(
            id=known.get(thing["canonical_key"]) or uuid.uuid4(),
            organization_id=org,
            canonical_key=thing["canonical_key"],
            name=thing["name"],
            name_key=_key(thing["name"])[:300],
            fields=_jsonable(thing["fields"]),
            merged_on=thing["merged_on"],
            possible_matches=thing["possible_matches"],
            profile_version=int(version.version),
        )
        db.add(asset)
        for rec in thing["sources"]:
            db.add(
                OrganizationAssetSource(
                    organization_id=org,
                    asset_id=asset.id,
                    source=_cut(rec["source"], 80),
                    external_id=_cut(rec["external_id"], 200),
                    identifiers=rec["identifiers"],
                    fields=_jsonable(rec["fields"]),
                    last_seen_at=rec["last_seen"],
                )
            )
        for soft in thing["software"]:
            db.add(
                OrganizationAssetSoftware(
                    organization_id=org,
                    asset_id=asset.id,
                    vendor=_cut(soft["vendor"], 200),
                    product=_cut(soft["product"], 200),
                    vendor_key=_key(soft["vendor"])[:200],
                    product_key=_key(soft["product"])[:200],
                    version=_cut(soft["version"], 100) or identity.UNKNOWN_VERSION,
                    cpe=_cut(soft["cpe"], 300),
                    purl=_cut(soft["purl"], 500),
                    install_date=_cut(soft["install_date"], 40),
                    first_seen_at=_seen(soft["first_seen"]),
                    last_seen_at=_seen(soft["last_seen"]),
                    sources=list(soft["sources"]),
                )
            )
    await db.flush()
    return len(things)


# ── Administrator decisions: merge and split (S3) ──────────────────────

MERGE = "merge"
SPLIT = "split"
DECISION_KINDS: tuple[str, ...] = (MERGE, SPLIT)
#: Most records one merge may name.
MAX_MERGE_RECORDS = 50


class DecisionError(ValueError):
    """A merge or split the store refuses (the API answers 422 with its message)."""


def _refs(records: Sequence[Mapping[str, Any]]) -> list[identity.RecordRef]:
    refs: list[identity.RecordRef] = []
    for rec in records:
        source = str(rec.get("source") or "").strip().lower()
        external_id = str(rec.get("external_id") or "").strip()
        if not source or not external_id:
            raise DecisionError("each record needs a source and an external_id")
        ref = identity.RecordRef(source, external_id)
        if ref not in refs:
            refs.append(ref)
    return refs


def decisions_of(rows: Sequence[Any]) -> identity.Decisions:
    """The stored *rows* as the engine's :class:`~identity.Decisions` (a malformed row is ignored)."""
    merges: list[frozenset[identity.RecordRef]] = []
    splits: set[frozenset[identity.RecordRef]] = set()
    for row in rows:
        try:
            refs = _refs(getattr(row, "records", None) or [])
        except DecisionError:
            continue
        kind = getattr(row, "kind", "")
        if kind == MERGE and len(refs) >= 2:
            merges.append(frozenset(refs))
        elif kind == SPLIT and len(refs) == 2:
            splits.add(frozenset(refs))
    return identity.Decisions(merges=tuple(merges), splits=frozenset(splits))


async def load_decisions(db: AsyncSession, organization_id: uuid.UUID) -> identity.Decisions:
    rows = (
        (
            await db.execute(
                select(OrganizationAssetDecision)
                .where(OrganizationAssetDecision.organization_id == organization_id)
                .order_by(OrganizationAssetDecision.created_at, OrganizationAssetDecision.id)
            )
        )
        .scalars()
        .all()
    )
    return decisions_of(rows)


#: The address ``identity.section_record`` gives a row typed by hand (no ``external_id``):
#: its position in the section, which moves when a row above it is removed.
_POSITIONAL_ID = re.compile(r"row-\d+")


def check_decision(kind: str, records: Sequence[Mapping[str, Any]]) -> list[identity.RecordRef]:
    """The records of a merge or split, checked (``DecisionError`` on a bad request).

    A record addressed by its position (a row typed by hand, ``row-<index>``) is
    refused: a stored decision on it would apply to another row once the list
    changes. Such a row gets an ``external_id`` first.
    """
    if kind not in DECISION_KINDS:
        raise DecisionError(f"kind must be one of {', '.join(DECISION_KINDS)}")
    refs = _refs(records)
    positional = [r.label() for r in refs if _POSITIONAL_ID.fullmatch(r.external_id)]
    if positional:
        raise DecisionError(
            "record(s) addressed by their position, give the row an external_id first: " + ", ".join(positional[:10])
        )
    if kind == MERGE and not 2 <= len(refs) <= MAX_MERGE_RECORDS:
        raise DecisionError(f"a merge names 2 to {MAX_MERGE_RECORDS} different records")
    if kind == SPLIT and len(refs) != 2:
        raise DecisionError("a split names exactly 2 different records")
    return refs


async def _known_refs(db: AsyncSession, organization_id: uuid.UUID) -> set[identity.RecordRef]:
    rows = (
        await db.execute(
            select(OrganizationAssetSource.source, OrganizationAssetSource.external_id).where(
                OrganizationAssetSource.organization_id == organization_id
            )
        )
    ).all()
    return {identity.RecordRef(source, external_id) for source, external_id in rows}


async def record_decision(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    kind: str,
    records: Sequence[Mapping[str, Any]],
    reason: str = "",
    author_user_id: uuid.UUID | None = None,
) -> OrganizationAssetDecision:
    """Keep a merge or split and rebuild the store with it (the caller commits and audits).

    Every record must be one the store holds; a merge or split that the stored
    decisions already say is refused as a no-op (``DecisionError``).
    """
    refs = check_decision(kind, records)
    unknown = [r.label() for r in refs if r not in await _known_refs(db, organization_id)]
    if unknown:
        raise DecisionError("unknown record(s): " + ", ".join(unknown[:10]))
    current = await load_decisions(db, organization_id)
    wanted = frozenset(refs)
    if (kind == MERGE and wanted in current.merges) or (kind == SPLIT and wanted in current.splits):
        raise DecisionError(f"this {kind} is already recorded")
    row = OrganizationAssetDecision(
        organization_id=organization_id,
        kind=kind,
        records=[{"source": r.source, "external_id": r.external_id} for r in refs],
        reason=_cut(reason, 500),
        author_user_id=author_user_id,
    )
    db.add(row)
    await db.flush()
    await rebuild_from_profile(db, organization_id)
    return row


async def remove_decision(
    db: AsyncSession, organization_id: uuid.UUID, decision_id: uuid.UUID
) -> OrganizationAssetDecision | None:
    """Drop a merge or split and rebuild the store without it (``None``: not found; the caller commits)."""
    row = (
        await db.execute(
            select(OrganizationAssetDecision).where(
                OrganizationAssetDecision.organization_id == organization_id,
                OrganizationAssetDecision.id == decision_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    await db.delete(row)
    await db.flush()
    await rebuild_from_profile(db, organization_id)
    return row


async def list_decisions(db: AsyncSession, organization_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        (
            await db.execute(
                select(OrganizationAssetDecision)
                .where(OrganizationAssetDecision.organization_id == organization_id)
                .order_by(OrganizationAssetDecision.created_at.desc(), OrganizationAssetDecision.id)
            )
        )
        .scalars()
        .all()
    )
    return [decision_dict(row) for row in rows]


def decision_dict(row: OrganizationAssetDecision) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "records": list(row.records or []),
        "reason": row.reason,
        "author_user_id": str(row.author_user_id) if row.author_user_id else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def refresh_after_save(db: AsyncSession, organization_id: uuid.UUID | None = None) -> uuid.UUID | None:
    """Rebuild the store after a new profile version was saved in this transaction.

    Only an organization with assets (now or in its store) is rebuilt, as
    :func:`ensure_built` decides. A store failure never fails the save: the
    next read rebuilds it. Answers the organization id, ``None`` when nothing
    ran or the refresh failed.
    """
    import logging

    try:
        async with db.begin_nested():
            return await ensure_built(db, organization_id)
    except Exception:  # the profile save stands; the store catches up on its next read
        logging.getLogger(__name__).warning("Organization asset store refresh failed", exc_info=True)
        return None


def _jsonable(value: Any) -> Any:
    """*value* with datetimes as ISO strings, for a JSONB column."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


async def ensure_built(db: AsyncSession, organization_id: uuid.UUID | None = None) -> uuid.UUID | None:
    """Build the store when it lags the current profile; answer the organization id (``None``: no profile)."""
    version = await _current(db, organization_id)
    if version is None:
        return None
    org: uuid.UUID = version.organization_id
    built = (
        await db.execute(
            select(func.max(OrganizationAsset.profile_version)).where(OrganizationAsset.organization_id == org)
        )
    ).scalar()
    has_rows = bool((version.profile or {}).get("assets_exposure"))
    if (built or 0) != int(version.version) and (has_rows or built is not None):
        await rebuild_from_profile(db, org)
    return org


@dataclass
class Page:
    items: list[dict[str, Any]]
    total: int
    offset: int
    limit: int


def _limit(limit: int) -> int:
    return max(1, min(int(limit or 50), MAX_PAGE))


def _summary(asset: OrganizationAsset, sources: list[str], software_count: int) -> dict[str, Any]:
    fields = asset.fields or {}

    def value(name: str) -> Any:
        entry = fields.get(name)
        return entry.get("value") if isinstance(entry, Mapping) else None

    return {
        "id": str(asset.id),
        "name": asset.name,
        "category": value("category"),
        "subcategory": value("subcategory"),
        "criticality": value("criticality"),
        "internet_facing": value("internet_facing"),
        "owner": value("owner"),
        "os_family": value("os_family"),
        "os_version": value("os_version"),
        "sources": sources,
        "software_count": software_count,
        "merged_on": asset.merged_on or [],
    }


async def list_assets(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    q: str = "",
    offset: int = 0,
    limit: int = 50,
) -> Page:
    """The canonical assets of *organization_id*, by name, filtered on a name substring *q*."""
    limit, offset = _limit(limit), max(0, int(offset or 0))
    where = [OrganizationAsset.organization_id == organization_id]
    if q.strip():
        where.append(OrganizationAsset.name_key.contains(_key(q)))
    total = (await db.execute(select(func.count()).select_from(OrganizationAsset).where(*where))).scalar() or 0
    assets = (
        (
            await db.execute(
                select(OrganizationAsset)
                .where(*where)
                .order_by(OrganizationAsset.name_key, OrganizationAsset.canonical_key)
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    ids = [a.id for a in assets]
    sources: dict[uuid.UUID, list[str]] = {}
    counts: dict[uuid.UUID, int] = {}
    if ids:
        for asset_id, source in (
            await db.execute(
                select(OrganizationAssetSource.asset_id, OrganizationAssetSource.source)
                .where(OrganizationAssetSource.asset_id.in_(ids))
                .order_by(OrganizationAssetSource.source)
            )
        ).all():
            names = sources.setdefault(asset_id, [])
            if source not in names:
                names.append(source)
        for asset_id, count in (
            await db.execute(
                select(OrganizationAssetSoftware.asset_id, func.count())
                .where(OrganizationAssetSoftware.asset_id.in_(ids))
                .group_by(OrganizationAssetSoftware.asset_id)
            )
        ).all():
            counts[asset_id] = int(count)
    return Page(
        items=[_summary(a, sources.get(a.id, []), int(counts.get(a.id, 0))) for a in assets],
        total=int(total),
        offset=offset,
        limit=limit,
    )


def _software_dict(row: OrganizationAssetSoftware) -> dict[str, Any]:
    return {
        "vendor": row.vendor,
        "product": row.product,
        "version": row.version,
        "cpe": row.cpe,
        "purl": row.purl,
        "install_date": row.install_date or None,
        "first_seen": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_seen": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "sources": list(row.sources or []),
    }


async def get_asset(
    db: AsyncSession,
    organization_id: uuid.UUID,
    asset_id: uuid.UUID,
    *,
    software_offset: int = 0,
    software_limit: int = 100,
) -> dict[str, Any] | None:
    """One asset with its fields and their origins, its source records and a page of its software."""
    asset = (
        await db.execute(
            select(OrganizationAsset).where(
                OrganizationAsset.organization_id == organization_id, OrganizationAsset.id == asset_id
            )
        )
    ).scalar_one_or_none()
    if asset is None:
        return None
    records = (
        (
            await db.execute(
                select(OrganizationAssetSource)
                .where(OrganizationAssetSource.asset_id == asset.id)
                .order_by(OrganizationAssetSource.source, OrganizationAssetSource.external_id)
            )
        )
        .scalars()
        .all()
    )
    limit, offset = _limit(software_limit), max(0, int(software_offset or 0))
    soft_where = OrganizationAssetSoftware.asset_id == asset.id
    soft_total = (
        await db.execute(select(func.count()).select_from(OrganizationAssetSoftware).where(soft_where))
    ).scalar() or 0
    software = (
        (
            await db.execute(
                select(OrganizationAssetSoftware)
                .where(soft_where)
                .order_by(
                    OrganizationAssetSoftware.vendor_key,
                    OrganizationAssetSoftware.product_key,
                    OrganizationAssetSoftware.version,
                )
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    names = list(dict.fromkeys(r.source for r in records))
    return {
        **_summary(asset, names, int(soft_total)),
        "fields": asset.fields or {},
        "explanation": identity.Canonical(
            id=asset.canonical_key, records=[], merged_on=list(asset.merged_on or [])
        ).explanation(),
        "possible_matches": asset.possible_matches or [],
        "records": [
            {
                "source": r.source,
                "external_id": r.external_id,
                "identifiers": r.identifiers or {},
                "fields": r.fields or {},
                "last_seen": r.last_seen_at.isoformat() if r.last_seen_at else None,
            }
            for r in records
        ],
        "software": {
            "items": [_software_dict(s) for s in software],
            "total": int(soft_total),
            "offset": offset,
            "limit": limit,
        },
    }


async def assets_of_software(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    product: str,
    vendor: str = "",
    version: str = "",
    offset: int = 0,
    limit: int = 50,
) -> Page:
    """The assets running *product* (and *vendor*, *version* when given), with the version each runs."""
    limit, offset = _limit(limit), max(0, int(offset or 0))
    where = [
        OrganizationAssetSoftware.organization_id == organization_id,
        OrganizationAssetSoftware.product_key == _key(product),
    ]
    if vendor.strip():
        where.append(OrganizationAssetSoftware.vendor_key == _key(vendor))
    if version.strip():
        where.append(OrganizationAssetSoftware.version == version.strip())
    total = (await db.execute(select(func.count()).select_from(OrganizationAssetSoftware).where(*where))).scalar() or 0
    rows = (
        await db.execute(
            select(OrganizationAssetSoftware, OrganizationAsset.name)
            .join(OrganizationAsset, OrganizationAsset.id == OrganizationAssetSoftware.asset_id)
            .where(*where)
            .order_by(OrganizationAsset.name_key, OrganizationAssetSoftware.version, OrganizationAsset.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return Page(
        items=[{"asset_id": str(s.asset_id), "asset_name": name, **_software_dict(s)} for s, name in rows],
        total=int(total),
        offset=offset,
        limit=limit,
    )
