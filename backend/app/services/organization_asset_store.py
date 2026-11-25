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
- :func:`ensure_built` makes sure the store is built (since #1543 every
  row write of ``organization_asset_records`` refreshes its own identity
  clusters, so this is one indexed probe after the first build).
- The software the assets run is read as Software entities by
  ``organization_software_store`` (#1495); the software read it replaced is gone.

Since #1543 the asset records are the source of the rows; the store is their canonical view.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization_asset import (
    OrganizationAsset,
    OrganizationAssetDecision,
    OrganizationAssetSbom,
    OrganizationAssetSoftware,
    OrganizationAssetSource,
    OrganizationSoftwareVersion,
)
from app.models.organization_asset_record import (
    OrganizationAssetGroup,
    OrganizationAssetGroupMember,
    OrganizationAssetRecord,
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
    key_order: Sequence[str] | None = None,
    decisions: identity.Decisions | None = None,
    sboms: Mapping[identity.RecordRef, Sequence[identity.SourceRecord]] | None = None,
) -> list[dict[str, Any]]:
    """The canonical assets of profile *rows*: what :func:`rebuild_from_profile` writes (pure, testable).

    Each answer holds ``canonical_key``, ``name``, ``fields`` (field ->
    ``FieldValue.as_dict()``), ``merged_on``, ``possible_matches``,
    ``sources`` (one per record) and ``software`` (``merge_software``).
    *sboms* maps a source record to the SBOM documents imported on it (source
    ``sbom``): their software joins the asset holding that record, whatever it
    was merged with; an SBOM on a record no longer present is left out.
    *key_order* and *precedence* are the organization's ``asset_identity``
    settings (:func:`identity_settings`); empty keeps the engine's defaults.
    """
    sboms = sboms or {}
    records = [identity.section_record("assets", row, index=i) for i, row in enumerate(rows)]
    out: list[dict[str, Any]] = []
    for thing in identity.match_records(
        records, key_order=tuple(key_order or identity.DEFAULT_KEY_ORDER), decisions=decisions
    ):
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
                "software": identity.merge_software(
                    [*thing.records, *(doc for r in thing.records for doc in sboms.get(r.ref, ()))]
                ),
            }
        )
    return out


def identity_settings(profile: Mapping[str, Any] | None) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """The key order and per-field precedence the profile's ``asset_identity`` sets (empty: the defaults).

    A stored value that no longer parses keeps the defaults rather than failing a rebuild.
    """
    from app.services.organization_context import AssetIdentity

    raw = (profile or {}).get("asset_identity") or {}
    try:
        settings = AssetIdentity.model_validate(raw)
    except ValueError:
        settings = AssetIdentity()
    return tuple(settings.key_order), {k: tuple(v) for k, v in settings.precedence.items()}


async def _current(db: AsyncSession, organization_id: uuid.UUID | None) -> OrganizationContextVersion | None:
    from app.services.organization_context import current_version

    return await current_version(db, organization_id)


async def rebuild_from_profile(db: AsyncSession, organization_id: uuid.UUID | None = None) -> int:
    """Rebuild the whole store of *organization_id* (the primary when ``None``) from its asset records.

    Since #1543 the records are the source of truth; the name is kept for its
    callers (a merge or split decision, an identity settings change, an admin
    rebuild). Answers the number of canonical assets written (the caller
    commits). No profile yet: the store is emptied and 0 is answered.
    """
    from app.services import organization_asset_records as records

    org = await records.ensure_records(db, organization_id)
    if org is None:
        return 0
    return await records.rebuild_from_records(db, org)


async def write_canonical(
    db: AsyncSession,
    organization_id: uuid.UUID,
    things: Sequence[Mapping[str, Any]],
    known: Mapping[str, uuid.UUID],
    *,
    profile_version: int,
) -> dict[identity.RecordRef, uuid.UUID]:
    """Write *things* (:func:`canonical_rows`) as assets with their sources and installations; flushed.

    The caller deleted the assets they replace. An asset whose canonical key is in
    *known* keeps that id. Answers the asset id of every source record written.
    """
    held: dict[identity.RecordRef, uuid.UUID] = {}
    for thing in things:
        asset = OrganizationAsset(
            id=known.get(thing["canonical_key"]) or uuid.uuid4(),
            organization_id=organization_id,
            canonical_key=thing["canonical_key"],
            name=thing["name"],
            name_key=_key(thing["name"])[:300],
            fields=_jsonable(thing["fields"]),
            merged_on=thing["merged_on"],
            possible_matches=thing["possible_matches"],
            profile_version=profile_version,
        )
        db.add(asset)
        for rec in thing["sources"]:
            held[identity.RecordRef(rec["source"], rec["external_id"])] = asset.id
            db.add(
                OrganizationAssetSource(
                    organization_id=organization_id,
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
                    organization_id=organization_id,
                    asset_id=asset.id,
                    vendor=_cut(soft["vendor"], 200),
                    product=_cut(soft["product"], 200),
                    vendor_key=_key(soft["vendor"])[:200],
                    product_key=_key(soft["product"])[:200],
                    version=_cut(soft["version"], 100) or identity.UNKNOWN_VERSION,
                    cpe=_cut(soft["cpe"], 300),
                    purl=_cut(soft["purl"], 500),
                    install_date=_cut(soft["install_date"], 40),
                    bundled_in=_cut(soft.get("bundled_in"), 300),
                    first_seen_at=_seen(soft["first_seen"]),
                    last_seen_at=_seen(soft["last_seen"]),
                    sources=list(soft["sources"]),
                )
            )
    await db.flush()
    return held


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
    """The records of *organization_id* (since #1543 the asset records, imported on first touch)."""
    from app.models.organization_asset_record import OrganizationAssetRecord
    from app.services import organization_asset_records as records

    await records.ensure_records(db, organization_id)
    rows = (
        await db.execute(
            select(OrganizationAssetRecord.source, OrganizationAssetRecord.external_id).where(
                OrganizationAssetRecord.organization_id == organization_id
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


# ── SBOM import: software of an application or image on an asset ──────

#: The source name SBOM components carry on an asset's software.
SBOM_SOURCE = "sbom"


class SbomImportError(ValueError):
    """An SBOM import the store refuses on an unknown asset (the API answers 404)."""


def sbom_components(sbom: Any) -> list[dict[str, Any]]:
    """The rows an SBOM gives an asset: one per vendor, product and version, bundling component kept.

    A version the SBOM does not give is ``unknown``, never an empty string; a
    component listed twice (same vendor, product and version) is kept once.
    """
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for comp in sbom.components:
        version = comp.version or identity.UNKNOWN_VERSION
        key = (_key(comp.vendor or ""), _key(comp.product), version)
        if key in out:
            continue
        out[key] = {
            "vendor": _cut(comp.vendor, 200),
            "product": _cut(comp.product, 200),
            "version": _cut(version, 100),
            "cpe": _cut(comp.cpe, 300),
            "purl": _cut(comp.purl, 500),
            "bundled_in": _cut(sbom.bundled_in(comp), 300),
            # What the component is (#1495): library, operating_system, ... (the software's type).
            "type": comp.software_type,
            # The components it depends on (#1495), ``product version`` labels.
            "depends_on": [_cut(label, 300) for label in sbom.depends_on(comp)],
        }
    return list(out.values())


def sbom_document_key(doc: Any, file_name: str) -> str:
    """What an SBOM describes, so importing the same document again replaces it.

    CycloneDX: ``metadata.component`` (``name version``); SPDX: ``name``; else the file name.
    """
    if isinstance(doc, Mapping):
        meta = doc.get("metadata")
        main = meta.get("component") if isinstance(meta, Mapping) else None
        if isinstance(main, Mapping) and str(main.get("name") or "").strip():
            return _cut(f"{main.get('name')} {main.get('version') or ''}", 300)
        if doc.get("spdxVersion") and str(doc.get("name") or "").strip():
            return _cut(doc.get("name"), 300)
    return _cut(file_name or "sbom", 300)


#: Software documents an endpoint source writes on its own device records (#1251): ``format`` is the
#: source, so the software ranks by that source's class (``mdm``, ``edr``), not as an SBOM.
ENDPOINT_SOFTWARE_FORMATS: frozenset[str] = frozenset({"intune", "defender", "tanium", "github", "gitlab"})


#: The format of the hand document that carries an administrator's software attachments on an asset (#1495).
HAND_SOFTWARE_FORMAT = "manual"


def _sbom_record(row: OrganizationAssetSbom) -> identity.SourceRecord:
    source = row.format if row.format in ENDPOINT_SOFTWARE_FORMATS | {HAND_SOFTWARE_FORMAT} else SBOM_SOURCE
    return identity.SourceRecord(
        source=source,
        external_id=f"{row.anchor_source}:{row.anchor_external_id}:{row.document_key}",
        software=[
            {**c, "first_seen": row.imported_at, "last_seen": row.imported_at}
            for c in row.components or []
            if isinstance(c, Mapping)
        ],
        last_seen=row.imported_at,
    )


async def load_sboms(
    db: AsyncSession, organization_id: uuid.UUID
) -> dict[identity.RecordRef, list[identity.SourceRecord]]:
    """The SBOMs of *organization_id*, by the source record they are anchored on, as ``sbom`` records."""
    rows = (
        (
            await db.execute(
                select(OrganizationAssetSbom)
                .where(OrganizationAssetSbom.organization_id == organization_id)
                .order_by(OrganizationAssetSbom.imported_at, OrganizationAssetSbom.id)
            )
        )
        .scalars()
        .all()
    )
    out: dict[identity.RecordRef, list[identity.SourceRecord]] = {}
    for row in rows:
        out.setdefault(identity.RecordRef(row.anchor_source, row.anchor_external_id), []).append(_sbom_record(row))
    return out


async def replace_endpoint_software(
    db: AsyncSession,
    organization_id: uuid.UUID,
    source: str,
    software: Mapping[str, list[dict[str, Any]]],
    *,
    complete: bool,
    unread: Collection[str] = (),
) -> int:
    """Store the software an endpoint source read per device, anchored on its device record (#1251).

    One document per device (``document_key`` the source): a re-run replaces its
    components, so versions follow the source. When the read was *complete*, the
    documents of devices it no longer returned are removed with them, except the
    *unread* devices (their software read failed): their stored document is kept.
    Answers the number of documents written or removed; the caller rebuilds the store.
    """
    if source not in ENDPOINT_SOFTWARE_FORMATS:
        raise ValueError(f"source must be one of {', '.join(sorted(ENDPOINT_SOFTWARE_FORMATS))}")
    stored = {
        row.anchor_external_id: row
        for row in (
            await db.execute(
                select(OrganizationAssetSbom).where(
                    OrganizationAssetSbom.organization_id == organization_id,
                    OrganizationAssetSbom.anchor_source == source,
                    OrganizationAssetSbom.document_key == source,
                )
            )
        )
        .scalars()
        .all()
    }
    changed = 0
    now = datetime.now(UTC)
    for external_id, entries in software.items():
        external_id = _cut(external_id, 200)
        components = [
            {
                "vendor": _cut(e.get("vendor"), 200),
                "product": _cut(e.get("product"), 300),
                "version": _cut(e.get("version"), 100) or identity.UNKNOWN_VERSION,
                "cpe": _cut(e.get("cpe"), 500),
                "purl": _cut(e.get("purl"), 500),
                "bundled_in": "",
            }
            for e in entries
            if isinstance(e, Mapping) and str(e.get("product") or "").strip()
        ]
        row = stored.pop(external_id, None)
        if row is None:
            row = OrganizationAssetSbom(
                organization_id=organization_id,
                anchor_source=source,
                anchor_external_id=external_id,
                document_key=source,
                format=source,
                file_name="",
            )
            db.add(row)
        elif row.components == components:
            continue
        row.components = components
        row.imported_at = now
        changed += 1
    if complete:
        keep = {_cut(external_id, 200) for external_id in unread}
        for external_id, row in stored.items():
            if external_id in keep:
                continue
            await db.delete(row)
            changed += 1
    await db.flush()
    return changed


async def import_sbom(
    db: AsyncSession,
    organization_id: uuid.UUID,
    asset_id: uuid.UUID,
    *,
    data: bytes,
    file_name: str = "",
    author_user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Import an SBOM on an asset and rebuild the store (the caller commits and audits).

    The document is anchored on the asset's first source record, so it follows
    the asset through every rebuild and merge. The same document imported again
    replaces its components (idempotent); components another document gave are
    kept. ``SbomImportError`` names an unknown asset; ``sbom_parser.SbomError``
    a document refused.
    """
    import json

    from app.services import sbom_parser

    anchor = (
        await db.execute(
            select(OrganizationAssetSource.source, OrganizationAssetSource.external_id)
            .join(OrganizationAsset, OrganizationAsset.id == OrganizationAssetSource.asset_id)
            .where(OrganizationAsset.organization_id == organization_id, OrganizationAsset.id == asset_id)
            .order_by(OrganizationAssetSource.source, OrganizationAssetSource.external_id)
            .limit(1)
        )
    ).first()
    if anchor is None:
        raise SbomImportError("Asset not found")
    sbom = sbom_parser.parse_bytes(data)
    components = sbom_components(sbom)
    document_key = sbom_document_key(json.loads(data), file_name)
    source, external_id = str(anchor[0]), str(anchor[1])
    row = (
        await db.execute(
            select(OrganizationAssetSbom).where(
                OrganizationAssetSbom.organization_id == organization_id,
                OrganizationAssetSbom.anchor_source == source,
                OrganizationAssetSbom.anchor_external_id == external_id,
                OrganizationAssetSbom.document_key == document_key,
            )
        )
    ).scalar_one_or_none()
    replaced = row is not None
    if row is None:
        row = OrganizationAssetSbom(
            organization_id=organization_id,
            anchor_source=source,
            anchor_external_id=external_id,
            document_key=document_key,
        )
        db.add(row)
    row.format = sbom.format
    row.file_name = _cut(file_name, 300)
    row.components = components
    row.author_user_id = author_user_id
    row.imported_at = datetime.now(UTC)
    await db.flush()
    await rebuild_from_profile(db, organization_id)
    return {
        "id": str(row.id),
        "format": sbom.format,
        "document": document_key,
        "components": len(components),
        "unknown_versions": sum(1 for c in components if c["version"] == identity.UNKNOWN_VERSION),
        "bundled": sum(1 for c in components if c["bundled_in"]),
        "replaced": replaced,
    }


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
    """Make sure the store is built from the asset records; answer the organization id (``None``: no profile).

    Since #1543 the records (``organization_asset_records``) are the source of
    truth and every row write refreshes its identity clusters itself: the first
    call for an organization imports its profile rows and builds the store
    once, later calls cost one indexed probe.
    """
    from app.services import organization_asset_records as records

    org = await records.ensure_records(db, organization_id)
    if org is not None and await _unlinked_installation(db, org):
        # A store built before software was an entity (#1495): one rebuild links it.
        await records.rebuild_from_records(db, org)
    return org


async def _unlinked_installation(db: AsyncSession, organization_id: uuid.UUID) -> bool:
    """Whether an installation of *organization_id* names no software version yet (one indexed probe)."""
    found = await db.execute(
        select(OrganizationAssetSoftware.id)
        .where(
            OrganizationAssetSoftware.organization_id == organization_id,
            OrganizationAssetSoftware.software_version_id.is_(None),
        )
        .limit(1)
    )
    return found.first() is not None


@dataclass
class Page:
    items: list[dict[str, Any]]
    total: int
    offset: int
    limit: int


def _limit(limit: int) -> int:
    return max(1, min(int(limit or 50), MAX_PAGE))


async def _attach_record_ids(
    db: AsyncSession,
    organization_id: uuid.UUID,
    asset_ids: Sequence[uuid.UUID],
    refs: Mapping[uuid.UUID, list[dict[str, str]]],
) -> None:
    """Add ``id`` (the asset record a row API writes, #1543) to each ``(source, external_id)`` ref of *refs*."""
    from app.models.organization_asset_record import OrganizationAssetRecord

    rec = OrganizationAssetRecord
    found = {
        (asset_id, source, external_id): str(record_id)
        for record_id, asset_id, source, external_id in (
            await db.execute(
                select(rec.id, rec.asset_id, rec.source, rec.external_id).where(
                    rec.organization_id == organization_id, rec.asset_id.in_(list(asset_ids))
                )
            )
        ).all()
    }
    for asset_id, items in refs.items():
        for ref in items:
            record_id = found.get((asset_id, ref["source"], ref["external_id"]))
            if record_id:
                ref["id"] = record_id


def _summary(
    asset: OrganizationAsset,
    sources: list[str],
    software_count: int,
    records: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
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
        # The source records merged into it, by address: what a merge of assets names (#1178).
        "records": records or [],
    }


#: The asset fields the list filters on (``fields.<name>.value``), in the order the facets answer them.
FILTER_FIELDS: tuple[str, ...] = ("category", "subcategory", "criticality", "internet_facing")
#: Fields a list filters on with no facet in the sidebar: the agent read of the inventory (#1774).
LIST_ONLY_FILTER_FIELDS: tuple[str, ...] = ("os_family",)
#: The fields a free-text search reads besides the name (#2085): an OS family or a build number finds its hosts.
TEXT_FIELDS: tuple[str, ...] = ("os_family", "os_version", "owner")
#: The most values :func:`held_values` names per field.
MAX_HELD_VALUES = 20


def _field_value(name: str) -> Any:
    return OrganizationAsset.fields[name]["value"].astext


def _text(value: str) -> str:
    """*value* as a free-text search compares it: whitespace collapsed, lower case (PostgreSQL ``lower``)."""
    return " ".join(str(value or "").split()).lower()


def _group_ids(values: Sequence[str]) -> list[uuid.UUID]:
    """The asset group ids among *values*; a value that is not a UUID names no group."""
    out: list[uuid.UUID] = []
    for value in values:
        try:
            out.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    return out


def _assets_in_groups(organization_id: uuid.UUID, group_ids: Sequence[uuid.UUID]) -> Any:
    """The canonical assets one of whose records is a member of one of *group_ids* (#1556)."""
    return (
        select(OrganizationAssetRecord.asset_id)
        .join(OrganizationAssetGroupMember, OrganizationAssetGroupMember.record_id == OrganizationAssetRecord.id)
        .where(
            OrganizationAssetRecord.organization_id == organization_id,
            OrganizationAssetRecord.asset_id.is_not(None),
            OrganizationAssetGroupMember.group_id.in_(list(group_ids)),
        )
    )


def _asset_where(
    organization_id: uuid.UUID,
    *,
    q: str = "",
    filters: Mapping[str, Sequence[str]] | None = None,
    skip: str | None = None,
) -> list[Any]:
    """The clauses of the asset list: a text, then each filter but *skip* (OR within, AND across).

    The text is a case-insensitive substring of the name or of one of :data:`TEXT_FIELDS`; a field
    filter compares case-insensitively, so ``os_family:windows`` keeps the ``Windows`` hosts (#2085).
    """
    where: list[Any] = [OrganizationAsset.organization_id == organization_id]
    if q.strip():
        text = _text(q)
        where.append(
            or_(
                OrganizationAsset.name_key.contains(_key(q)),
                *(func.lower(_field_value(name)).contains(text, autoescape=True) for name in TEXT_FIELDS),
            )
        )
    for name, values in (filters or {}).items():
        wanted = [str(v) for v in values if str(v).strip()]
        if not wanted or name == skip:
            continue
        if name == "group":
            # A group filter of ids none of which parses keeps no asset, never every asset.
            where.append(OrganizationAsset.id.in_(_assets_in_groups(organization_id, _group_ids(wanted))))
        elif name == "source":
            where.append(
                OrganizationAsset.id.in_(
                    select(OrganizationAssetSource.asset_id).where(
                        OrganizationAssetSource.organization_id == organization_id,
                        OrganizationAssetSource.source.in_(wanted),
                    )
                )
            )
        elif name in FILTER_FIELDS or name in LIST_ONLY_FILTER_FIELDS:
            where.append(func.lower(_field_value(name)).in_(sorted({v.strip().lower() for v in wanted})))
    return where


async def held_values(
    db: AsyncSession,
    organization_id: uuid.UUID,
    filters: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, list[str]]:
    """The values each field filter of *filters* holds across the organization's assets (#2085).

    What a read that kept no row names, so a zero is never silent: ``{"os_family": ["Linux", "Windows"]}``.
    Only the ``fields`` filters are answered (never ``source`` or ``group``), at most
    :data:`MAX_HELD_VALUES` values each, sorted.
    """
    out: dict[str, list[str]] = {}
    for name, values in (filters or {}).items():
        if not any(str(v).strip() for v in values):
            continue
        if name not in FILTER_FIELDS and name not in LIST_ONLY_FILTER_FIELDS:
            continue
        column = _field_value(name)
        rows = (
            await db.execute(
                select(column)
                .where(OrganizationAsset.organization_id == organization_id, column.is_not(None), column != "")
                .group_by(column)
                .order_by(column)
                .limit(MAX_HELD_VALUES)
            )
        ).scalars()
        out[name] = [str(value) for value in rows]
    return out


async def asset_facets(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    q: str = "",
    filters: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Each filter's values with their asset count, for the faceted sidebar of the asset list (#1501).

    A facet's counts apply the search and every OTHER facet's picked values, so a count says how many
    assets picking that value would add (OR within a facet, AND across). A subcategory names its category.
    """
    facets: dict[str, list[dict[str, Any]]] = {}
    for name in FILTER_FIELDS:
        column = _field_value(name)
        where = [*_asset_where(organization_id, q=q, filters=filters, skip=name), column.is_not(None), column != ""]
        if name == "subcategory":
            parent = _field_value("category")
            rows = (
                await db.execute(
                    select(parent, column, func.count())
                    .select_from(OrganizationAsset)
                    .where(*where)
                    .group_by(parent, column)
                    .order_by(parent, column)
                    .limit(MAX_PAGE)
                )
            ).all()
            facets[name] = [
                {"value": str(value), "count": int(count), "category": str(category or "")}
                for category, value, count in rows
            ]
            continue
        rows = (
            await db.execute(
                select(column, func.count())
                .select_from(OrganizationAsset)
                .where(*where)
                .group_by(column)
                .order_by(column)
                .limit(MAX_PAGE)
            )
        ).all()
        facets[name] = [{"value": str(value), "count": int(count)} for value, count in rows]
    kept = select(OrganizationAsset.id).where(*_asset_where(organization_id, q=q, filters=filters, skip="source"))
    sources = (
        await db.execute(
            select(OrganizationAssetSource.source, func.count(func.distinct(OrganizationAssetSource.asset_id)))
            .where(
                OrganizationAssetSource.organization_id == organization_id,
                OrganizationAssetSource.asset_id.in_(kept),
            )
            .group_by(OrganizationAssetSource.source)
            .order_by(OrganizationAssetSource.source)
            .limit(MAX_PAGE)
        )
    ).all()
    facets["source"] = [{"value": str(value), "count": int(count)} for value, count in sources]
    # The asset groups (#1556): each group's id as the value, its name as the label, its kept assets counted once.
    kept = select(OrganizationAsset.id).where(*_asset_where(organization_id, q=q, filters=filters, skip="group"))
    rec, member, grp = OrganizationAssetRecord, OrganizationAssetGroupMember, OrganizationAssetGroup
    groups = (
        await db.execute(
            select(grp.id, grp.name, func.count(func.distinct(rec.asset_id)))
            .join(member, member.group_id == grp.id)
            .join(rec, rec.id == member.record_id)
            .where(grp.organization_id == organization_id, rec.asset_id.in_(kept))
            .group_by(grp.id, grp.name, grp.name_key)
            .order_by(grp.name_key, grp.id)
            .limit(MAX_PAGE)
        )
    ).all()
    facets["group"] = [{"value": str(gid), "count": int(count), "label": str(name)} for gid, name, count in groups]
    return facets


async def list_assets(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    q: str = "",
    offset: int = 0,
    limit: int = 50,
    filters: Mapping[str, Sequence[str]] | None = None,
) -> Page:
    """The canonical assets of *organization_id*, by name, filtered on a text *q* (name, OS, owner).

    *filters* keeps the assets whose field (``FILTER_FIELDS``) holds one of the
    values given, and with ``source`` the assets one of whose records comes from
    one of the sources given; an empty list filters nothing.
    """
    limit, offset = _limit(limit), max(0, int(offset or 0))
    where = _asset_where(organization_id, q=q, filters=filters)
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
    refs: dict[uuid.UUID, list[dict[str, str]]] = {}
    counts: dict[uuid.UUID, int] = {}
    if ids:
        for asset_id, source, external_id in (
            await db.execute(
                select(
                    OrganizationAssetSource.asset_id,
                    OrganizationAssetSource.source,
                    OrganizationAssetSource.external_id,
                )
                .where(OrganizationAssetSource.asset_id.in_(ids))
                .order_by(OrganizationAssetSource.source, OrganizationAssetSource.external_id)
            )
        ).all():
            names = sources.setdefault(asset_id, [])
            if source not in names:
                names.append(source)
            refs.setdefault(asset_id, []).append({"source": source, "external_id": external_id})
        await _attach_record_ids(db, organization_id, ids, refs)
        for asset_id, count in (
            await db.execute(
                select(OrganizationAssetSoftware.asset_id, func.count())
                .where(OrganizationAssetSoftware.asset_id.in_(ids))
                .group_by(OrganizationAssetSoftware.asset_id)
            )
        ).all():
            counts[asset_id] = int(count)
    return Page(
        items=[_summary(a, sources.get(a.id, []), int(counts.get(a.id, 0)), refs.get(a.id, [])) for a in assets],
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
        "bundled_in": row.bundled_in or None,
        "first_seen": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_seen": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "sources": list(row.sources or []),
        # The software version it installs (#1495): the software page links from here.
        "software_version_id": str(row.software_version_id) if row.software_version_id else None,
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
    # The software each row installs (#1495 slice 2): the asset page links every row to its software page.
    version_ids = {s.software_version_id for s in software if s.software_version_id}
    software_of: dict[uuid.UUID, uuid.UUID] = (
        {
            vid: sid
            for vid, sid in (
                await db.execute(
                    select(OrganizationSoftwareVersion.id, OrganizationSoftwareVersion.software_id).where(
                        OrganizationSoftwareVersion.id.in_(version_ids)
                    )
                )
            ).all()
        }
        if version_ids
        else {}
    )
    names = list(dict.fromkeys(r.source for r in records))
    detail_refs = {asset.id: [{"source": r.source, "external_id": r.external_id} for r in records]}
    await _attach_record_ids(db, organization_id, [asset.id], detail_refs)
    return {
        **_summary(asset, names, int(soft_total)),
        "groups": await _asset_groups(db, organization_id, asset.id),
        # #1591 S3: the primary assets the asset supports (directly or through a group), with role and source.
        "primary_assets": await _asset_primary_assets(db, organization_id, asset.id),
        "fields": asset.fields or {},
        "explanation": identity.Canonical(
            id=asset.canonical_key, records=[], merged_on=list(asset.merged_on or [])
        ).explanation(),
        "possible_matches": asset.possible_matches or [],
        "records": [
            {
                **ref,
                "identifiers": r.identifiers or {},
                "fields": r.fields or {},
                "last_seen": r.last_seen_at.isoformat() if r.last_seen_at else None,
            }
            for r, ref in zip(records, detail_refs[asset.id], strict=True)
        ],
        "software": {
            "items": [
                {
                    **_software_dict(s),
                    "software_id": (
                        str(software_of[s.software_version_id]) if s.software_version_id in software_of else None
                    ),
                }
                for s in software
            ],
            "total": int(soft_total),
            "offset": offset,
            "limit": limit,
        },
    }


async def _asset_primary_assets(
    db: AsyncSession, organization_id: uuid.UUID, asset_id: uuid.UUID
) -> list[dict[str, Any]]:
    from app.services.ctem_primary_assets import asset_primary_assets

    return await asset_primary_assets(db, organization_id, asset_id)


async def _asset_groups(db: AsyncSession, organization_id: uuid.UUID, asset_id: uuid.UUID) -> list[dict[str, str]]:
    """The asset groups any record of *asset_id* is a member of, each once, by name (#1556)."""
    rec, member, grp = OrganizationAssetRecord, OrganizationAssetGroupMember, OrganizationAssetGroup
    rows = (
        await db.execute(
            select(grp.id, grp.name, grp.source)
            .join(member, member.group_id == grp.id)
            .join(rec, rec.id == member.record_id)
            .where(grp.organization_id == organization_id, rec.asset_id == asset_id)
            .group_by(grp.id, grp.name, grp.source, grp.name_key)
            .order_by(grp.name_key, grp.id)
            .limit(MAX_PAGE)
        )
    ).all()
    return [{"id": str(gid), "name": str(name), "source": str(source)} for gid, name, source in rows]


#: The most identifiers one lookup reads (a page of a platform's list, its hero).
MAX_LOOKUP_KEYS = 200
#: The identifier kinds another platform names an asset by: what it holds itself. A serial or a
#: cloud id is never guessed from a free value (``web-02`` would read as the serial ``WEB02``).
LOOKUP_KINDS: tuple[str, ...] = ("fqdn", "hostname", "mac", "ip")


#: A source reference another product holds for the same thing: ``<source>:<external id>``, e.g.
#: ``openaev:3f2a...`` for the OpenAEV asset both products imported. Matched exactly on the source
#: records, before the identifiers and the name, since it names one record of one source.
_SOURCE_REFERENCE = re.compile(r"^([a-z][a-z0-9_-]{1,79}):(\S{1,200})$")


def _source_reference(value: str) -> tuple[str, str] | None:
    """``(source, external_id)`` when *value* reads as a source reference, else ``None``."""
    found = _SOURCE_REFERENCE.match(value)
    return (found.group(1), found.group(2)) if found else None


def _lookup_keys(value: str) -> set[tuple[str, str]]:
    """The (kind, normalised value) pairs *value* may be: an FQDN and its hostname, a MAC, an IP."""
    return {(kind, key) for kind in LOOKUP_KINDS if (key := identity.normalize_identifier(kind, value))}


async def find_assets_by_identifier(
    db: AsyncSession,
    organization_id: uuid.UUID,
    values: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """The canonical asset each of *values* names, by the value as asked.

    A value is read as every :data:`LOOKUP_KINDS` kind it can be (``web-02`` a
    hostname, ``10.0.0.5`` an IP, ``aa:bb:...`` a MAC, ``web-02.corp.example``
    an FQDN and the hostname ``web-02``) and matched against the identifiers of every
    source record, normalised as the identity engine compares them, then
    against the asset's name. A value of the form ``<source>:<external id>``
    (``openaev:<asset id>``) names one source record: it is matched exactly on
    the records' source and external id and, when found, wins over every
    weak reading of the same value. A value no asset holds is absent; when
    several assets hold it (a weak key such as a shared IP), the first by name
    answers. The profile caps its rows at 5,000, so one scan of the records is bounded.
    """
    asked: dict[str, set[tuple[str, str]]] = {}
    references: dict[str, tuple[str, str]] = {}
    for value in values[:MAX_LOOKUP_KEYS]:
        text_value = str(value or "").strip()
        if text_value and text_value not in asked:
            asked[text_value] = _lookup_keys(text_value)
            if (reference := _source_reference(text_value)) is not None:
                references[text_value] = reference
    if not asked:
        return {}
    held: dict[tuple[str, str], set[uuid.UUID]] = {}
    by_reference: dict[tuple[str, str], set[uuid.UUID]] = {}
    wanted_references = set(references.values())
    for asset_id, source, external_id, identifiers in (
        await db.execute(
            select(
                OrganizationAssetSource.asset_id,
                OrganizationAssetSource.source,
                OrganizationAssetSource.external_id,
                OrganizationAssetSource.identifiers,
            ).where(OrganizationAssetSource.organization_id == organization_id)
        )
    ).all():
        if (source, external_id) in wanted_references:
            by_reference.setdefault((source, external_id), set()).add(asset_id)
        record = identity.SourceRecord(source="lookup", external_id="", identifiers=dict(identifiers or {}))
        for kind in LOOKUP_KINDS:
            for key in record.keys(kind):
                held.setdefault((kind, key), set()).add(asset_id)
    by_name: dict[str, set[uuid.UUID]] = {}
    names = {_key(v) for v in asked}
    for asset_id, name_key in (
        await db.execute(
            select(OrganizationAsset.id, OrganizationAsset.name_key).where(
                OrganizationAsset.organization_id == organization_id, OrganizationAsset.name_key.in_(names)
            )
        )
    ).all():
        by_name.setdefault(name_key, set()).add(asset_id)
    matched: dict[str, set[uuid.UUID]] = {}
    for value, keys in asked.items():
        exact = by_reference.get(references[value], set()) if value in references else set()
        if exact:
            matched[value] = exact
            continue
        ids: set[uuid.UUID] = set()
        for pair in keys:
            ids |= held.get(pair, set())
        ids |= by_name.get(_key(value), set())
        if ids:
            matched[value] = ids
    if not matched:
        return {}
    every = {i for ids in matched.values() for i in ids}
    assets = {
        a.id: a
        for a in (await db.execute(select(OrganizationAsset).where(OrganizationAsset.id.in_(every)))).scalars().all()
    }
    out: dict[str, dict[str, Any]] = {}
    for value, ids in matched.items():
        found = sorted((assets[i] for i in ids if i in assets), key=lambda a: (a.name_key, a.canonical_key))
        if found:
            asset = found[0]
            summary = _summary(asset, [], 0)
            out[value] = {
                "id": summary["id"],
                "name": summary["name"],
                "category": summary["category"],
                "criticality": summary["criticality"],
                "internet_facing": summary["internet_facing"],
                "owner": summary["owner"],
                "matches": len(found),
            }
    return out


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
