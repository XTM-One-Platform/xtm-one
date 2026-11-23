# SPDX-License-Identifier: Apache-2.0

"""The asset records of the organization context, the source of truth of the Inventory (#1543).

Every writer of assets and asset groups goes through this module: the form and
the REST API (one row, a bulk delete), the agent row tools, the syncs and the
imports (batches by ``(source, external_id)``, :func:`remove_gone` by set
difference). A write touches the rows it names, logs them in
``organization_asset_changes`` and refreshes the canonical store
(``organization_asset_store``) for the identity cluster of those rows only,
never the whole organization:

- :func:`identity_closure` collects the records that can change asset with the
  write: the touched records, the records of their current assets, every record
  sharing a strong key with one of them (``organization_asset_identifiers``,
  one indexed lookup per round) and the records an administrator's merge or
  split names with one of them, until nothing new joins.
- :func:`refresh_clusters` runs the identity engine on that closure, replaces
  its canonical assets (an asset whose canonical key is unchanged keeps its id)
  and recomputes the weak-key hints (an IP shared with another asset) of the
  assets it wrote and of their neighbours. The answer equals a full
  :func:`rebuild_from_records` (``tests/test_organization_asset_records_postgres.py``
  checks it on random writes).

The first touch of an organization imports its profile rows
(:func:`ensure_records`): the migration backfills nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization_asset import OrganizationAsset
from app.models.organization_asset_record import (
    OrganizationAssetChange,
    OrganizationAssetGroup,
    OrganizationAssetGroupMember,
    OrganizationAssetIdentifier,
    OrganizationAssetImport,
    OrganizationAssetRecord,
    OrganizationCrownJewelLink,
)
from app.services import organization_asset_identity as identity
from app.services import organization_asset_store as store

#: The most records one closure refreshes; past it the organization is rebuilt whole.
MAX_CLOSURE = 5000
#: The most rows one batch write or bulk delete takes.
MAX_BATCH = 10_000
#: Keys of a row that are columns, not fields.
_PROVENANCE = ("source", "external_id", "sync_id")
#: The source of a membership the record's own ``group`` field gives.
MEMBERSHIP_FROM_RECORD = "record"
#: Namespace of the ids a hand row gets (uuid5 of organization, position and name: a re-run gives the same id).
_HAND_NAMESPACE = uuid.UUID("5d0f6c2e-1543-4c1e-9a43-2f9b0e8a1543")


class RecordError(ValueError):
    """A row write the API answers 422 (a bad field, a synced field changed by hand, a missing group)."""


class StaleRowError(RecordError):
    """``If-Match`` names a row version that moved since (the API answers 409)."""


class RecordNotFoundError(RecordError):
    """The row is not one of this organization's (the API answers 404)."""


@dataclass
class WriteResult:
    """What a write did: the records created, updated, removed and left as they were."""

    created: list[uuid.UUID] = field(default_factory=list)
    updated: list[uuid.UUID] = field(default_factory=list)
    removed: list[uuid.UUID] = field(default_factory=list)
    unchanged: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": len(self.created),
            "updated": len(self.updated),
            "removed": len(self.removed),
            "unchanged": self.unchanged,
        }


def _now() -> datetime:
    return datetime.now(UTC)


def _as_map(rows: Iterable[Any]) -> dict[Any, Any]:
    """Two-column result rows as a dict (first column -> second)."""
    return {row[0]: row[1] for row in rows}


def _key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


# ── Rows <-> records ──────────────────────────────────────────────────


#: Most inline software entries one record keeps (a source's own list; an SBOM is a document of its own).
MAX_INLINE_SOFTWARE = 2000


def validate_asset(row: Mapping[str, Any]) -> dict[str, Any]:
    """*row* as the profile's ``AssetEntry`` would store it (``RecordError`` on a bad field).

    The inline ``software`` a source writes on its row (vendor, product,
    version, ``bundled_in``, ``depends_on``...) is not an ``AssetEntry`` field:
    it is kept on the record as it came, as the store before #1543 read it
    from the raw profile row, so the installations and dependency edges of a
    record match a full rebuild.
    """
    from pydantic import ValidationError

    from app.services.organization_context import AssetEntry

    data = dict(row)
    software = data.pop("software", None)
    for listed in ("mac", "ip"):
        if isinstance(data.get(listed), str):  # one address as a source writes it, read as the list of one
            data[listed] = [data[listed]]
    try:
        value = AssetEntry.model_validate(data).model_dump(mode="json")
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "asset"
        raise RecordError(f"{where}: {first.get('msg', 'invalid value')}") from None
    if isinstance(software, list):
        kept = [store._jsonable(dict(s)) for s in software[:MAX_INLINE_SOFTWARE] if isinstance(s, Mapping)]
        if kept:
            value["software"] = kept
    return value


def validate_group(row: Mapping[str, Any]) -> dict[str, Any]:
    """*row* as the profile's ``AssetGroupEntry`` would store it (``RecordError`` on a bad field)."""
    from pydantic import ValidationError

    from app.services.organization_context import AssetGroupEntry

    try:
        return AssetGroupEntry.model_validate(dict(row)).model_dump(mode="json")
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "asset group"
        raise RecordError(f"{where}: {first.get('msg', 'invalid value')}") from None


def hand_external_id(organization_id: uuid.UUID, position: int, name: str) -> str:
    """The id of a row typed by hand (no ``external_id``): stable for the same organization, position and name."""
    return str(uuid.uuid5(_HAND_NAMESPACE, f"{organization_id}:{position}:{_key(name)}"))


def _columns(value: Mapping[str, Any]) -> dict[str, Any]:
    """The record's columns from a validated asset *value*."""
    fields = {k: v for k, v in value.items() if k not in _PROVENANCE}
    return {
        "source": (str(value.get("source") or "manual").strip().lower() or "manual")[:80],
        "external_id": str(value.get("external_id") or "")[:200],
        "sync_id": str(value.get("sync_id") or "")[:64],
        "name": str(value.get("name") or "")[:200],
        "name_key": _key(value.get("name"))[:200],
        "category": str(value.get("category") or "generic_asset")[:40],
        "criticality": str(value.get("criticality") or "unknown")[:20],
        "environment": str(value.get("environment") or "")[:20],
        "internet_facing": str(value.get("internet_facing") or "unknown")[:10],
        "owner": str(value.get("owner") or "")[:200],
        "group_key": _key(value.get("group"))[:200],
        "fields": fields,
        "last_seen_at": identity._row_last_seen(value.get("last_seen")),
    }


def record_row(record: OrganizationAssetRecord) -> dict[str, Any]:
    """The record as a profile ``assets_exposure`` row (what the readers and the compat GET answer)."""
    return {
        **(record.fields or {}),
        "source": record.source,
        "external_id": record.external_id,
        "sync_id": record.sync_id,
    }


def record_out(record: OrganizationAssetRecord) -> dict[str, Any]:
    """The record as the row API answers it: the row, its id, asset, version and audit columns."""
    return {
        **record_row(record),
        "id": str(record.id),
        "asset_id": str(record.asset_id) if record.asset_id else None,
        "row_version": record.row_version,
        "created_by": record.created_by,
        "updated_by": record.updated_by,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


def group_row(group: OrganizationAssetGroup) -> dict[str, Any]:
    return {**(group.fields or {}), "source": group.source, "external_id": group.external_id, "sync_id": group.sync_id}


def group_out(group: OrganizationAssetGroup) -> dict[str, Any]:
    return {**group_row(group), "id": str(group.id), "row_version": group.row_version}


def _source_record(record: OrganizationAssetRecord) -> identity.SourceRecord:
    return identity.section_record("assets", record_row(record))


def identifier_rows(row: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The normalised ``(kind, value)`` identity keys of an asset *row*, every kind (strong and weak)."""
    out: list[tuple[str, str]] = []
    for kind, values in identity._asset_ids(row).items():
        for value in values:
            key = identity.normalize_identifier(kind, value)
            if key and (kind, key) not in out:
                out.append((kind, key))
    return out


# ── The change log ────────────────────────────────────────────────────


def _diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    return {k: before.get(k) for k in changed}, {k: after.get(k) for k in changed}


def _log(
    db: AsyncSession,
    organization_id: uuid.UUID,
    entity: str,
    entity_id: uuid.UUID,
    op: str,
    *,
    origin: str,
    actor: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> None:
    db.add(
        OrganizationAssetChange(
            organization_id=organization_id,
            entity=entity,
            entity_id=entity_id,
            op=op,
            origin=origin[:80],
            actor=actor[:120],
            before=dict(before) if before is not None else None,
            after=dict(after) if after is not None else None,
        )
    )


# ── The first touch: the profile rows move into the records ──────────


async def _lock(db: AsyncSession, organization_id: uuid.UUID) -> None:
    """The organization's write lock (the asset store's key): writers of one organization run one at a time."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"organization_assets:{organization_id}"}
    )


async def is_imported(db: AsyncSession, organization_id: uuid.UUID) -> bool:
    return (
        await db.execute(
            select(OrganizationAssetImport.organization_id).where(
                OrganizationAssetImport.organization_id == organization_id
            )
        )
    ).scalar() is not None


async def ensure_records(db: AsyncSession, organization_id: uuid.UUID | None = None) -> uuid.UUID | None:
    """Import the organization's profile rows into the records once; answer the organization id.

    ``None`` names the primary organization; no profile and no organization
    answers ``None``. The import reads the current profile version's
    ``assets_exposure``, ``asset_groups`` and crown jewels' ``asset_names``,
    writes one ``import`` change, then builds the canonical store from the
    records. The caller commits.
    """
    from app.services.organization_context import current_version

    version = await current_version(db, organization_id)
    org = version.organization_id if version is not None else organization_id
    if org is None:
        return None
    if await is_imported(db, org):
        await _catch_up(db, org, version)
        return org
    await _lock(db, org)
    if await is_imported(db, org):  # another writer imported while this one waited
        await _catch_up(db, org, version)
        return org
    profile = dict(version.profile or {}) if version is not None else {}
    groups: list[dict[str, Any]] = []
    for position, raw in enumerate(profile.get("asset_groups") or []):
        if not isinstance(raw, Mapping):
            continue
        try:
            value = validate_group(raw)
        except RecordError:
            continue  # a stored row that no longer parses stays in the profile history
        if not value["external_id"]:
            value["external_id"] = hand_external_id(org, position, value["name"])
        groups.append(value)
    for value in groups:
        db.add(
            OrganizationAssetGroup(
                organization_id=org,
                source=value["source"],
                external_id=value["external_id"],
                sync_id=value["sync_id"],
                name=value["name"],
                name_key=_key(value["name"])[:200],
                fields={k: v for k, v in value.items() if k not in _PROVENANCE},
                created_by="import",
                updated_by="import",
            )
        )
    await db.flush()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for position, raw in enumerate(profile.get("assets_exposure") or []):
        if not isinstance(raw, Mapping):
            continue
        try:
            value = validate_asset(raw)
        except RecordError:
            continue
        if not value["external_id"]:
            value["external_id"] = hand_external_id(org, position, value["name"])
        ref = (value["source"], value["external_id"])
        if ref in seen:
            continue  # the unique key keeps the first, as the store's sources did
        seen.add(ref)
        rows.append(value)
    await _insert_records(db, org, rows, actor="import")
    await _link_crown_jewels(db, org, profile.get("crown_jewels") or [])
    db.add(OrganizationAssetImport(organization_id=org, profile_version=int(version.version) if version else 0))
    _log(
        db,
        org,
        "record",
        org,
        "import",
        origin="profile",
        actor="import",
        before=None,
        after={"profile_version": int(version.version) if version else 0, "assets": len(rows), "groups": len(groups)},
    )
    await db.flush()
    await rebuild_from_records(db, org, locked=True)
    return org


class _ProfileLists:
    """The asset and group lists of a stored profile JSON, as :func:`apply_profile_lists` reads a profile model."""

    def __init__(self, profile: Mapping[str, Any] | None) -> None:
        data = profile or {}
        self.assets_exposure = [r for r in data.get("assets_exposure") or [] if isinstance(r, Mapping)]
        self.asset_groups = [g for g in data.get("asset_groups") or [] if isinstance(g, Mapping)]


async def imported_version(db: AsyncSession, organization_id: uuid.UUID) -> int:
    """The profile version the records last took the lists of (0: none yet)."""
    found = (
        await db.execute(
            select(OrganizationAssetImport.profile_version).where(
                OrganizationAssetImport.organization_id == organization_id
            )
        )
    ).scalar()
    return int(found or 0)


async def mark_version(db: AsyncSession, organization_id: uuid.UUID, profile_version: int) -> None:
    await db.execute(
        update(OrganizationAssetImport)
        .where(OrganizationAssetImport.organization_id == organization_id)
        .values(profile_version=profile_version)
    )


async def _catch_up(db: AsyncSession, organization_id: uuid.UUID, version: Any) -> None:
    """Apply a profile version the records never saw (one written straight to the table) as row writes.

    ``save_version`` marks the version it writes, so a save through the
    service costs one indexed read here; only a version inserted by another
    path (a restore of an old release, a test) is diffed against the records.
    """
    if version is None or int(version.version) <= await imported_version(db, organization_id):
        return
    await _lock(db, organization_id)
    if int(version.version) <= await imported_version(db, organization_id):
        return
    await mark_version(db, organization_id, int(version.version))
    await apply_profile_lists(db, organization_id, _ProfileLists(version.profile), origin="profile", actor="import")


async def _insert_records(
    db: AsyncSession, organization_id: uuid.UUID, rows: Sequence[Mapping[str, Any]], *, actor: str
) -> list[OrganizationAssetRecord]:
    """Insert validated asset *rows* as records with their identifiers and memberships (no log, no refresh)."""
    records: list[OrganizationAssetRecord] = []
    for value in rows:
        record = OrganizationAssetRecord(
            id=uuid.uuid4(), organization_id=organization_id, created_by=actor, updated_by=actor, **_columns(value)
        )
        db.add(record)
        records.append(record)
    await db.flush()
    await _write_identifiers(db, organization_id, records)
    await _write_memberships(db, organization_id, records)
    return records


async def _write_identifiers(
    db: AsyncSession, organization_id: uuid.UUID, records: Sequence[OrganizationAssetRecord]
) -> None:
    if not records:
        return
    await db.execute(
        delete(OrganizationAssetIdentifier).where(OrganizationAssetIdentifier.record_id.in_([r.id for r in records]))
    )
    for record in records:
        for kind, value in identifier_rows(record_row(record)):
            db.add(
                OrganizationAssetIdentifier(
                    organization_id=organization_id, record_id=record.id, kind=kind, value=value
                )
            )
    await db.flush()


async def _write_memberships(
    db: AsyncSession, organization_id: uuid.UUID, records: Sequence[OrganizationAssetRecord]
) -> None:
    """The ``record`` memberships of *records*: each in the group its ``group`` field names, when it exists.

    Memberships another source wrote (a person, a group sync) are left as they are.
    """
    if not records:
        return
    ids = [r.id for r in records]
    member = OrganizationAssetGroupMember
    await db.execute(delete(member).where(member.record_id.in_(ids), member.source == MEMBERSHIP_FROM_RECORD))
    wanted = {r.group_key for r in records if r.group_key}
    if not wanted:
        return
    groups = _as_map(
        (
            await db.execute(
                select(OrganizationAssetGroup.name_key, OrganizationAssetGroup.id).where(
                    OrganizationAssetGroup.organization_id == organization_id,
                    OrganizationAssetGroup.name_key.in_(wanted),
                )
            )
        ).all()
    )
    pairs = [(groups[r.group_key], r.id) for r in records if r.group_key in groups]
    taken = set()
    if pairs:
        taken = {
            (g, r)
            for g, r in (
                await db.execute(
                    select(member.group_id, member.record_id).where(
                        tuple_(member.group_id, member.record_id).in_(pairs)
                    )
                )
            ).all()
        }
    for group_id, record_id in pairs:
        if (group_id, record_id) not in taken:  # another source already holds it: that row stays
            db.add(member(group_id=group_id, record_id=record_id, source=MEMBERSHIP_FROM_RECORD))
    await db.flush()


async def group_member_counts(db: AsyncSession, organization_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """Distinct canonical assets per group (records merged into one asset count once)."""
    member, rec = OrganizationAssetGroupMember, OrganizationAssetRecord
    rows = (
        await db.execute(
            select(member.group_id, func.count(func.distinct(func.coalesce(rec.asset_id, rec.id))))
            .join(rec, rec.id == member.record_id)
            .where(rec.organization_id == organization_id)
            .group_by(member.group_id)
        )
    ).all()
    return {g: int(n) for g, n in rows}


async def _link_crown_jewels(db: AsyncSession, organization_id: uuid.UUID, jewels: Iterable[Any]) -> None:
    """The crown jewels' ``asset_names`` as links to the records and groups of those names."""
    for jewel in jewels:
        if not isinstance(jewel, Mapping):
            continue
        await set_crown_jewel_links(db, organization_id, str(jewel.get("name") or ""), jewel.get("asset_names") or [])


async def set_crown_jewel_links(
    db: AsyncSession, organization_id: uuid.UUID, crown_jewel: str, names: Iterable[Any]
) -> list[str]:
    """Replace the links of *crown_jewel* with the records and groups *names* name; answer the names found none."""
    jewel_key = _key(crown_jewel)[:200]
    if not jewel_key:
        return []
    await db.execute(
        delete(OrganizationCrownJewelLink).where(
            OrganizationCrownJewelLink.organization_id == organization_id,
            OrganizationCrownJewelLink.crown_jewel_key == jewel_key,
        )
    )
    keys = {_key(n)[:200]: str(n) for n in names if _key(n)}
    if not keys:
        return []
    groups = (
        await db.execute(
            select(OrganizationAssetGroup.name_key, OrganizationAssetGroup.id).where(
                OrganizationAssetGroup.organization_id == organization_id, OrganizationAssetGroup.name_key.in_(keys)
            )
        )
    ).all()
    records = (
        await db.execute(
            select(OrganizationAssetRecord.name_key, OrganizationAssetRecord.id).where(
                OrganizationAssetRecord.organization_id == organization_id, OrganizationAssetRecord.name_key.in_(keys)
            )
        )
    ).all()
    found: set[str] = set()
    for name_key, group_id in groups:
        found.add(name_key)
        db.add(
            OrganizationCrownJewelLink(organization_id=organization_id, crown_jewel_key=jewel_key, group_id=group_id)
        )
    for name_key, record_id in records:
        found.add(name_key)
        db.add(
            OrganizationCrownJewelLink(organization_id=organization_id, crown_jewel_key=jewel_key, record_id=record_id)
        )
    await db.flush()
    return [keys[k] for k in keys if k not in found]


# ── Writes ────────────────────────────────────────────────────────────


def _is_hand(origin: str) -> bool:
    """A person's write (the form, the API): a sync, an import or an agent writing its source's rows owns them."""
    return not origin.startswith(("sync:", "agent:")) and origin not in ("import", "profile")


def _local_fields() -> frozenset[str]:
    from app.services.organization_context_import import SOURCED_FIELDS

    return frozenset(SOURCED_FIELDS["assets"][3]) | {"criticality", "tags", "description"}


def _synced_change(record: OrganizationAssetRecord, after: Mapping[str, Any]) -> list[str]:
    """The fields a hand write changes on a synced record that the #1367 lock keeps (empty: allowed)."""
    from app.services.organization_context import is_platform_source

    if not is_platform_source(record.source) or not record.external_id:
        return []
    before = record_row(record)
    free = _local_fields() | set(_PROVENANCE)
    return sorted(k for k in set(before) | set(after) if k not in free and before.get(k) != after.get(k))


async def _check_group(db: AsyncSession, organization_id: uuid.UUID, value: Mapping[str, Any]) -> None:
    name = str(value.get("group") or "").strip()
    if not name:
        return
    exists = (
        await db.execute(
            select(OrganizationAssetGroup.id).where(
                OrganizationAssetGroup.organization_id == organization_id,
                OrganizationAssetGroup.name_key == _key(name)[:200],
            )
        )
    ).scalar()
    if exists is None:
        raise RecordError(f"group: no asset group is named '{name}'")


async def get_record(db: AsyncSession, organization_id: uuid.UUID, record_id: uuid.UUID) -> OrganizationAssetRecord:
    record = (
        await db.execute(
            select(OrganizationAssetRecord).where(
                OrganizationAssetRecord.organization_id == organization_id, OrganizationAssetRecord.id == record_id
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise RecordNotFoundError("no asset record with this id")
    return record


async def create_record(
    db: AsyncSession, organization_id: uuid.UUID, row: Mapping[str, Any], *, origin: str, actor: str
) -> OrganizationAssetRecord:
    """Create one record (a person's row gets a generated ``external_id``); refresh its cluster."""
    await ensure_records(db, organization_id)
    await _lock(db, organization_id)
    value = validate_asset(row)
    if not value["external_id"]:
        value["external_id"] = str(uuid.uuid4())
    if _is_hand(origin):
        await _check_group(db, organization_id, value)
    taken = (
        await db.execute(
            select(OrganizationAssetRecord.id).where(
                OrganizationAssetRecord.organization_id == organization_id,
                OrganizationAssetRecord.source == value["source"],
                OrganizationAssetRecord.external_id == value["external_id"],
            )
        )
    ).scalar()
    if taken is not None:
        raise RecordError(f"an asset record from {value['source']} with external_id '{value['external_id']}' exists")
    (record,) = await _insert_records(db, organization_id, [value], actor=actor)
    _log(
        db,
        organization_id,
        "record",
        record.id,
        "create",
        origin=origin,
        actor=actor,
        before=None,
        after=record_row(record),
    )
    await refresh_clusters(db, organization_id, [record.id])
    return record


async def update_record(
    db: AsyncSession,
    organization_id: uuid.UUID,
    record_id: uuid.UUID,
    changes: Mapping[str, Any],
    *,
    origin: str,
    actor: str,
    if_match: int | None = None,
) -> OrganizationAssetRecord:
    """Change the fields *changes* names on one record (``source`` and ``external_id`` never move)."""
    await ensure_records(db, organization_id)
    await _lock(db, organization_id)
    record = await get_record(db, organization_id, record_id)
    if if_match is not None and if_match != record.row_version:
        raise StaleRowError(f"the asset record changed since version {if_match} (now {record.row_version})")
    before = record_row(record)
    merged = {**before, **{k: v for k, v in changes.items() if k not in ("source", "external_id", "id")}}
    value = validate_asset(merged)
    if _is_hand(origin):
        locked = _synced_change(record, value)
        if locked:
            raise RecordError(
                f"'{record.name}' is kept in sync from {record.source}; {', '.join(locked)} cannot be changed by hand"
            )
        await _check_group(db, organization_id, value)
    if value == before:
        return record
    _apply(record, value, actor)
    await db.flush()
    await _write_identifiers(db, organization_id, [record])
    await _write_memberships(db, organization_id, [record])
    old, new = _diff(before, value)
    _log(db, organization_id, "record", record.id, "update", origin=origin, actor=actor, before=old, after=new)
    await refresh_clusters(db, organization_id, [record.id])
    return record


def _apply(record: OrganizationAssetRecord, value: Mapping[str, Any], actor: str) -> None:
    for column, item in _columns(value).items():
        setattr(record, column, item)
    record.row_version = int(record.row_version or 0) + 1
    record.updated_by = actor[:120]
    record.updated_at = _now()


async def delete_records(
    db: AsyncSession,
    organization_id: uuid.UUID,
    record_ids: Sequence[uuid.UUID],
    *,
    origin: str,
    actor: str,
    if_match: int | None = None,
) -> list[uuid.UUID]:
    """Delete records of this organization (an unknown id is skipped); log each; refresh their clusters."""
    if len(record_ids) > MAX_BATCH:
        raise RecordError(f"at most {MAX_BATCH} records per call")
    await ensure_records(db, organization_id)
    await _lock(db, organization_id)
    records = list(
        (
            await db.execute(
                select(OrganizationAssetRecord).where(
                    OrganizationAssetRecord.organization_id == organization_id,
                    OrganizationAssetRecord.id.in_(list(record_ids)),
                )
            )
        )
        .scalars()
        .all()
    )
    if if_match is not None and len(records) == 1 and records[0].row_version != if_match:
        raise StaleRowError(f"the asset record changed since version {if_match} (now {records[0].row_version})")
    return await _remove(db, organization_id, records, origin=origin, actor=actor)


async def _remove(
    db: AsyncSession, organization_id: uuid.UUID, records: Sequence[OrganizationAssetRecord], *, origin: str, actor: str
) -> list[uuid.UUID]:
    if not records:
        return []
    ids = [r.id for r in records]
    # The closure is read before the rows go: their assets and key neighbours are re-matched without them.
    closure = await identity_closure(db, organization_id, ids)
    for record in records:
        _log(
            db,
            organization_id,
            "record",
            record.id,
            "delete",
            origin=origin,
            actor=actor,
            before=record_row(record),
            after=None,
        )
    await db.execute(delete(OrganizationAssetRecord).where(OrganizationAssetRecord.id.in_(ids)))
    await db.flush()
    if not closure.assets and len(closure) > MAX_CLOSURE:
        # An oversized closure carries no assets: rebuild the organization, so the
        # canonical assets and sources of the deleted records do not stay behind.
        await rebuild_from_records(db, organization_id, locked=True)
        return ids
    removed = set(ids)
    await refresh_clusters(
        db, organization_id, [], closure=[i for i in closure if i not in removed], gone=closure.assets
    )
    return ids


async def upsert_records(
    db: AsyncSession,
    organization_id: uuid.UUID,
    rows: Sequence[Mapping[str, Any]],
    *,
    origin: str,
    actor: str,
    keep_software: frozenset[tuple[str, str]] = frozenset(),
) -> WriteResult:
    """Create or update records by ``(source, external_id)`` in one pass (a sync, an import, an agent, the API).

    *keep_software*: the refs of rows that cannot carry inline software (a
    profile model row); their record keeps the software it holds.

    A row without ``external_id`` from a person gets a new one (a create). A row
    equal to the stored record is left as it was and not logged. Hand origins keep
    the #1367 lock and the group check; a sync owns its rows.
    """
    if len(rows) > MAX_BATCH:
        raise RecordError(f"at most {MAX_BATCH} rows per call")
    await ensure_records(db, organization_id)
    await _lock(db, organization_id)
    values: list[dict[str, Any]] = []
    for position, raw in enumerate(rows):
        try:
            value = validate_asset(raw)
        except RecordError as exc:
            raise RecordError(f"rows[{position}].{exc}") from None
        if not value["external_id"]:
            value["external_id"] = str(uuid.uuid4())
        values.append(value)
    refs = list({(v["source"], v["external_id"]) for v in values})
    stored: dict[tuple[str, str], OrganizationAssetRecord] = {}
    for start in range(0, len(refs), 1000):
        chunk = refs[start : start + 1000]
        for record in (
            await db.execute(
                select(OrganizationAssetRecord).where(
                    OrganizationAssetRecord.organization_id == organization_id,
                    tuple_(OrganizationAssetRecord.source, OrganizationAssetRecord.external_id).in_(chunk),
                )
            )
        ).scalars():
            stored[(record.source, record.external_id)] = record
    result = WriteResult()
    fresh: list[dict[str, Any]] = []
    changed: list[OrganizationAssetRecord] = []
    for value in values:
        ref = (value["source"], value["external_id"])
        found = stored.get(ref)
        if found is None:
            if _is_hand(origin):
                await _check_group(db, organization_id, value)
            fresh.append(value)  # a duplicate row of the batch is dropped below, the first kept
            continue
        record = found
        if ref in keep_software and "software" not in value and (record.fields or {}).get("software"):
            # A profile model row carries no inline software: the save keeps the record's own list.
            value["software"] = record.fields["software"]
        before = record_row(record)
        if before == value:
            result.unchanged += 1
            continue
        if _is_hand(origin):
            locked = _synced_change(record, value)
            if locked:
                raise RecordError(
                    f"'{record.name}' is kept in sync from {record.source}; {', '.join(locked)} cannot be changed by hand"
                )
            await _check_group(db, organization_id, value)
        _apply(record, value, actor)
        old, new = _diff(before, value)
        _log(db, organization_id, "record", record.id, "update", origin=origin, actor=actor, before=old, after=new)
        changed.append(record)
        result.updated.append(record.id)
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for v in fresh:
        ref = (v["source"], v["external_id"])
        if ref not in seen:
            seen.add(ref)
            unique.append(v)
    fresh = unique
    await db.flush()
    if changed:
        await _write_identifiers(db, organization_id, changed)
        await _write_memberships(db, organization_id, changed)
    for record in await _insert_records(db, organization_id, fresh, actor=actor):
        _log(
            db,
            organization_id,
            "record",
            record.id,
            "create",
            origin=origin,
            actor=actor,
            before=None,
            after=record_row(record),
        )
        result.created.append(record.id)
    await db.flush()
    touched = [*result.created, *result.updated]
    if touched:
        await refresh_clusters(db, organization_id, touched)
    return result


async def remove_gone(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    source: str,
    sync_id: str,
    seen_external_ids: Iterable[str],
    actor: str,
) -> list[uuid.UUID]:
    """Delete the records of *sync_id* (from *source*) whose ``external_id`` the sync no longer saw.

    The seen ids go to a temporary table and the set difference is one SQL
    statement, so a sync of a million rows never loads them.
    """
    await ensure_records(db, organization_id)
    await _lock(db, organization_id)
    await db.execute(
        text("CREATE TEMP TABLE IF NOT EXISTS _asset_seen (external_id varchar(200) PRIMARY KEY) ON COMMIT DROP")
    )
    await db.execute(text("TRUNCATE _asset_seen"))
    batch = [{"e": str(e)[:200]} for e in dict.fromkeys(seen_external_ids) if e]
    for start in range(0, len(batch), 5000):
        await db.execute(
            text("INSERT INTO _asset_seen (external_id) VALUES (:e) ON CONFLICT DO NOTHING"),
            batch[start : start + 5000],
        )
    gone_ids = [
        row[0]
        for row in (
            await db.execute(
                text(
                    "SELECT r.id FROM organization_asset_records r WHERE r.organization_id = :org AND r.source = :src "
                    "AND r.sync_id = :sync AND NOT EXISTS (SELECT 1 FROM _asset_seen s WHERE s.external_id = r.external_id)"
                ),
                {"org": organization_id, "src": source, "sync": sync_id},
            )
        ).all()
    ]
    records = (
        list(
            (
                await db.execute(select(OrganizationAssetRecord).where(OrganizationAssetRecord.id.in_(gone_ids)))
            ).scalars()
        )
        if gone_ids
        else []
    )
    return await _remove(db, organization_id, records, origin=f"sync:{sync_id}", actor=actor)


# ── Groups ────────────────────────────────────────────────────────────


async def get_group(db: AsyncSession, organization_id: uuid.UUID, group_id: uuid.UUID) -> OrganizationAssetGroup:
    group = (
        await db.execute(
            select(OrganizationAssetGroup).where(
                OrganizationAssetGroup.organization_id == organization_id, OrganizationAssetGroup.id == group_id
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise RecordNotFoundError("no asset group with this id")
    return group


async def upsert_group(
    db: AsyncSession,
    organization_id: uuid.UUID,
    row: Mapping[str, Any],
    *,
    origin: str,
    actor: str,
    group_id: uuid.UUID | None = None,
    if_match: int | None = None,
) -> OrganizationAssetGroup:
    """Create a group, or change the one *group_id* names; records naming it join it."""
    await ensure_records(db, organization_id)
    await _lock(db, organization_id)
    group = await get_group(db, organization_id, group_id) if group_id is not None else None
    if group is not None and if_match is not None and if_match != group.row_version:
        raise StaleRowError(f"the asset group changed since version {if_match} (now {group.row_version})")
    before = group_row(group) if group is not None else None
    value = validate_group({**(before or {}), **{k: v for k, v in row.items() if k != "id"}})
    if group is not None:
        value["source"], value["external_id"] = group.source, group.external_id
    elif not value["external_id"]:
        value["external_id"] = str(uuid.uuid4())
    name_key = _key(value["name"])[:200]
    clash = (
        await db.execute(
            select(OrganizationAssetGroup.id).where(
                OrganizationAssetGroup.organization_id == organization_id,
                OrganizationAssetGroup.name_key == name_key,
                OrganizationAssetGroup.id != (group.id if group is not None else uuid.UUID(int=0)),
            )
        )
    ).scalar()
    if clash is not None:
        raise RecordError(f"an asset group is already named '{value['name']}'")
    fields = {k: v for k, v in value.items() if k not in _PROVENANCE}
    if group is None:
        group = OrganizationAssetGroup(
            organization_id=organization_id,
            source=value["source"],
            external_id=value["external_id"],
            sync_id=value["sync_id"],
            name=value["name"],
            name_key=name_key,
            fields=fields,
            created_by=actor[:120],
            updated_by=actor[:120],
        )
        db.add(group)
        await db.flush()
        _log(db, organization_id, "group", group.id, "create", origin=origin, actor=actor, before=None, after=value)
    elif value != before:
        group.name, group.name_key, group.fields, group.sync_id = value["name"], name_key, fields, value["sync_id"]
        group.row_version = int(group.row_version or 0) + 1
        group.updated_by, group.updated_at = actor[:120], _now()
        old, new = _diff(before or {}, value)
        _log(db, organization_id, "group", group.id, "update", origin=origin, actor=actor, before=old, after=new)
        await db.flush()
    members = list(
        (
            await db.execute(
                select(OrganizationAssetRecord).where(
                    OrganizationAssetRecord.organization_id == organization_id,
                    OrganizationAssetRecord.group_key == name_key,
                )
            )
        ).scalars()
    )
    await _write_memberships(db, organization_id, members)
    return group


async def delete_group(
    db: AsyncSession,
    organization_id: uuid.UUID,
    group_id: uuid.UUID,
    *,
    origin: str,
    actor: str,
    if_match: int | None = None,
) -> None:
    """Delete a group; its records keep their rows and lose the group (each logged)."""
    await ensure_records(db, organization_id)
    await _lock(db, organization_id)
    group = await get_group(db, organization_id, group_id)
    if if_match is not None and if_match != group.row_version:
        raise StaleRowError(f"the asset group changed since version {if_match} (now {group.row_version})")
    members = list(
        (
            await db.execute(
                select(OrganizationAssetRecord).where(
                    OrganizationAssetRecord.organization_id == organization_id,
                    OrganizationAssetRecord.group_key == group.name_key,
                )
            )
        ).scalars()
    )
    for record in members:
        before = record_row(record)
        _apply(record, {**before, "group": ""}, actor)
        _log(
            db,
            organization_id,
            "record",
            record.id,
            "update",
            origin=origin,
            actor=actor,
            before={"group": before.get("group")},
            after={"group": ""},
        )
    _log(
        db,
        organization_id,
        "group",
        group.id,
        "delete",
        origin=origin,
        actor=actor,
        before=group_row(group),
        after=None,
    )
    await db.delete(group)
    await db.flush()
    if members:
        await refresh_clusters(db, organization_id, [r.id for r in members])


# ── Identity: the closure and the incremental refresh ────────────────


class Closure(list[uuid.UUID]):
    """Record ids, with the canonical assets they were in before the write (``assets``)."""

    assets: set[uuid.UUID]

    def __init__(self, ids: Iterable[uuid.UUID] = (), assets: Iterable[uuid.UUID] = ()) -> None:
        super().__init__(ids)
        self.assets = set(assets)


async def _settings(db: AsyncSession, organization_id: uuid.UUID) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    from app.services.organization_context import current_version

    version = await current_version(db, organization_id)
    return store.identity_settings(version.profile if version is not None else None)


async def identity_closure(db: AsyncSession, organization_id: uuid.UUID, record_ids: Iterable[uuid.UUID]) -> Closure:
    """The records whose canonical asset can change when *record_ids* change (see the module docstring).

    Past :data:`MAX_CLOSURE` records the closure stops growing and is answered
    with ``assets`` empty and every id: the caller rebuilds the organization.
    """
    key_order, _precedence = await _settings(db, organization_id)
    strong = tuple(key_order or identity.DEFAULT_KEY_ORDER)
    decisions = await store.load_decisions(db, organization_id)
    decision_groups = [set(g) for g in decisions.merges] + [set(p) for p in decisions.splits]
    ids: set[uuid.UUID] = set(record_ids)
    assets: set[uuid.UUID] = set()
    frontier = set(ids)
    while frontier:
        if len(ids) > MAX_CLOSURE:
            return Closure(ids, ())
        new: set[uuid.UUID] = set()
        # The current assets of the frontier and every record they hold.
        held = {
            a
            for (a,) in (
                await db.execute(
                    select(OrganizationAssetRecord.asset_id).where(
                        OrganizationAssetRecord.id.in_(frontier), OrganizationAssetRecord.asset_id.is_not(None)
                    )
                )
            ).all()
        } - assets
        if held:
            assets |= held
            new |= {
                r
                for (r,) in (
                    await db.execute(
                        select(OrganizationAssetRecord.id).where(OrganizationAssetRecord.asset_id.in_(held))
                    )
                ).all()
            }
        # Every record sharing a strong key with the frontier.
        mine = select(OrganizationAssetIdentifier.kind, OrganizationAssetIdentifier.value).where(
            OrganizationAssetIdentifier.record_id.in_(frontier), OrganizationAssetIdentifier.kind.in_(strong)
        )
        keys = (await db.execute(mine)).all()
        if keys:
            for start in range(0, len(keys), 1000):
                chunk = [tuple(k) for k in keys[start : start + 1000]]
                new |= {
                    r
                    for (r,) in (
                        await db.execute(
                            select(OrganizationAssetIdentifier.record_id).where(
                                OrganizationAssetIdentifier.organization_id == organization_id,
                                tuple_(OrganizationAssetIdentifier.kind, OrganizationAssetIdentifier.value).in_(chunk),
                            )
                        )
                    ).all()
                }
        # The records an administrator's merge or split names with the frontier.
        if decision_groups:
            refs = {
                identity.RecordRef(s, e)
                for s, e in (
                    await db.execute(
                        select(OrganizationAssetRecord.source, OrganizationAssetRecord.external_id).where(
                            OrganizationAssetRecord.id.in_(frontier)
                        )
                    )
                ).all()
            }
            named = {ref for group in decision_groups if group & refs for ref in group} - refs
            if named:
                new |= {
                    r
                    for (r,) in (
                        await db.execute(
                            select(OrganizationAssetRecord.id).where(
                                OrganizationAssetRecord.organization_id == organization_id,
                                tuple_(OrganizationAssetRecord.source, OrganizationAssetRecord.external_id).in_(
                                    [(ref.source, ref.external_id) for ref in named]
                                ),
                            )
                        )
                    ).all()
                }
        frontier = new - ids
        ids |= frontier
    return Closure(sorted(ids), assets)


async def refresh_clusters(
    db: AsyncSession,
    organization_id: uuid.UUID,
    record_ids: Sequence[uuid.UUID],
    *,
    closure: Sequence[uuid.UUID] | None = None,
    gone: Iterable[uuid.UUID] = (),
) -> int:
    """Re-match the identity closure of *record_ids* and rewrite its canonical assets; answer how many.

    *closure* passes a closure read before a delete (its records already gone);
    *gone* the assets those records were in.
    """
    if closure is None:
        found = await identity_closure(db, organization_id, record_ids)
        if record_ids and not found.assets and len(found) > MAX_CLOSURE:
            return await rebuild_from_records(db, organization_id, locked=True)
        closure, gone = list(found), found.assets
    records = (
        list(
            (
                await db.execute(select(OrganizationAssetRecord).where(OrganizationAssetRecord.id.in_(list(closure))))
            ).scalars()
        )
        if closure
        else []
    )
    old_assets = set(gone) | {r.asset_id for r in records if r.asset_id}
    known: dict[str, uuid.UUID] = {}
    if old_assets:
        known = _as_map(
            (
                await db.execute(
                    select(OrganizationAsset.canonical_key, OrganizationAsset.id).where(
                        OrganizationAsset.id.in_(old_assets)
                    )
                )
            ).all()
        )
        await db.execute(
            update(OrganizationAssetRecord)
            .where(OrganizationAssetRecord.asset_id.in_(old_assets))
            .values(asset_id=None)
        )
        await db.execute(delete(OrganizationAsset).where(OrganizationAsset.id.in_(old_assets)))
    written = await _write_things(db, organization_id, records, known)
    await _refresh_possible_matches(db, organization_id, written)
    await _sync_software(db, organization_id)
    return len(written)


async def _write_things(
    db: AsyncSession,
    organization_id: uuid.UUID,
    records: Sequence[OrganizationAssetRecord],
    known: Mapping[str, uuid.UUID],
) -> set[uuid.UUID]:
    if not records:
        return set()
    key_order, precedence = await _settings(db, organization_id)
    sources = [_source_record(r) for r in records]
    refs = {s.ref for s in sources}
    sboms = {ref: docs for ref, docs in (await store.load_sboms(db, organization_id)).items() if ref in refs}
    things = store.canonical_rows(
        [record_row(r) for r in records],
        key_order=key_order,
        precedence=precedence,
        decisions=await store.load_decisions(db, organization_id),
        sboms=sboms,
    )
    for thing in things:
        thing["possible_matches"] = []  # recomputed against the whole organization below
    held = await store.write_canonical(
        db, organization_id, things, known, profile_version=await imported_version(db, organization_id)
    )
    by_ref = {s.ref: r for s, r in zip(sources, records, strict=True)}
    for ref, asset_id in held.items():
        record = by_ref.get(ref)
        if record is not None:
            record.asset_id = asset_id
    await db.flush()
    return set(held.values())


async def _refresh_possible_matches(db: AsyncSession, organization_id: uuid.UUID, assets: set[uuid.UUID]) -> None:
    """The weak-key hints of *assets* and of every asset sharing a weak key with them, read from the identifiers."""
    if not assets:
        return
    key_order, _p = await _settings(db, organization_id)
    weak = sorted(k for k in identity.WEAK_KINDS if k not in key_order)
    if not weak:
        return
    ident, rec = OrganizationAssetIdentifier, OrganizationAssetRecord
    values = (
        await db.execute(
            select(ident.kind, ident.value)
            .join(rec, rec.id == ident.record_id)
            .where(rec.asset_id.in_(assets), ident.kind.in_(weak))
            .distinct()
        )
    ).all()
    if not values:
        return
    pairs = (
        await db.execute(
            select(ident.kind, ident.value, rec.asset_id)
            .join(rec, rec.id == ident.record_id)
            .where(
                ident.organization_id == organization_id,
                tuple_(ident.kind, ident.value).in_([tuple(v) for v in values]),
                rec.asset_id.is_not(None),
            )
            .distinct()
        )
    ).all()
    by_value: dict[tuple[str, str], set[uuid.UUID]] = {}
    for kind, value, asset_id in pairs:
        by_value.setdefault((kind, value), set()).add(asset_id)
    concerned = {a for holders in by_value.values() if len(holders) > 1 for a in holders} | assets
    canon = _as_map(
        (
            await db.execute(
                select(OrganizationAsset.id, OrganizationAsset.canonical_key).where(OrganizationAsset.id.in_(concerned))
            )
        ).all()
    )
    hints: dict[uuid.UUID, list[dict[str, str]]] = {a: [] for a in concerned}
    for (kind, value), holders in sorted(by_value.items()):
        for here in holders:
            for there in sorted(holders - {here}, key=lambda a: canon.get(a, "")):
                hint = {"kind": kind, "value": value, "with": canon.get(there, "")}
                if hint not in hints[here]:
                    hints[here].append(hint)
    for asset_id, items in hints.items():
        if asset_id in canon:
            await db.execute(
                update(OrganizationAsset).where(OrganizationAsset.id == asset_id).values(possible_matches=items)
            )


async def _sync_software(db: AsyncSession, organization_id: uuid.UUID) -> None:
    """Bring the software entity in line with the installations, with the dependency edges of each asset.

    The edges come from each canonical asset's merged software (its records'
    inline software and SBOM documents), per asset as a full rebuild computes
    them: a ``bundled_in`` or ``depends_on`` label resolves only within the
    asset that carries it.
    """
    from app.services import organization_software_store as software_store

    sboms = await store.load_sboms(db, organization_id)
    by_asset: dict[uuid.UUID, list[identity.SourceRecord]] = {}
    for record in (
        await db.execute(
            select(OrganizationAssetRecord).where(
                OrganizationAssetRecord.organization_id == organization_id,
                OrganizationAssetRecord.asset_id.is_not(None),
            )
        )
    ).scalars():
        source = _source_record(record)
        bucket = by_asset.setdefault(record.asset_id, [])  # type: ignore[arg-type]
        bucket.append(source)
        bucket.extend(sboms.get(source.ref, ()))
    edges = [
        edge
        for records in by_asset.values()
        for edge in software_store.dependency_edges(identity.merge_software(records))
    ]
    await software_store.sync_software(db, organization_id, edges)


async def rebuild_from_records(db: AsyncSession, organization_id: uuid.UUID, *, locked: bool = False) -> int:
    """Rebuild the organization's canonical store from all its records (an identity settings change, an admin)."""
    if not locked:
        await _lock(db, organization_id)
    known = _as_map(
        (
            await db.execute(
                select(OrganizationAsset.canonical_key, OrganizationAsset.id).where(
                    OrganizationAsset.organization_id == organization_id
                )
            )
        ).all()
    )
    await db.execute(
        update(OrganizationAssetRecord)
        .where(OrganizationAssetRecord.organization_id == organization_id)
        .values(asset_id=None)
    )
    await db.execute(delete(OrganizationAsset).where(OrganizationAsset.organization_id == organization_id))
    records = list(
        (
            await db.execute(
                select(OrganizationAssetRecord).where(OrganizationAssetRecord.organization_id == organization_id)
            )
        ).scalars()
    )
    written = await _write_things(db, organization_id, records, known)
    await _refresh_possible_matches(db, organization_id, written)
    await _sync_software(db, organization_id)
    return len(written)


# ── The profile save of one release: an old client's lists become row writes ──


async def apply_profile_lists(
    db: AsyncSession,
    organization_id: uuid.UUID,
    profile: Any,
    *,
    origin: str,
    actor: str,
    profile_version: int | None = None,
    remove_missing: bool = True,
) -> WriteResult | None:
    """Turn the ``assets_exposure`` of a saved *profile* into row writes against the records.

    *remove_missing* false keeps the stored records the list does not hold:
    a list read from the records past the 5,000 cap of the profile model is
    a page, not the whole inventory, so nothing it lacks is deleted.

    *profile_version* is the version ``save_version`` is about to write: the
    records are marked as having seen it, so the next read does not diff it again.

    The compat path of ``PUT /organization-context`` (and every writer still on
    ``save_version``): a row whose ``(source, external_id)`` is stored is updated
    when it differs, a new row is created, a stored record the list no longer
    holds is deleted. A hand row without ``external_id`` is matched by name
    among the hand records, else created; the id it gets is written back on
    *profile*, so the next save matches it exactly. ``None`` when *profile* has
    no asset list (a tracked organization's profile). Only the rows that differ
    are written, logged and re-matched.
    """
    from app.services.organization_context import HAND_SOURCES

    rows = getattr(profile, "assets_exposure", None)
    if rows is None:
        return None
    await ensure_records(db, organization_id)
    await _lock(db, organization_id)
    if profile_version is not None:
        await mark_version(db, organization_id, profile_version)
    gone_groups = await _apply_profile_groups(
        db, organization_id, getattr(profile, "asset_groups", None) or [], origin=origin, actor=actor
    )
    rec = OrganizationAssetRecord
    stored_refs = {
        (s, e): (i, n)
        for i, s, e, n in (
            await db.execute(
                select(rec.id, rec.source, rec.external_id, rec.name_key).where(rec.organization_id == organization_id)
            )
        ).all()
    }
    by_hand_name: dict[str, list[str]] = {}
    for (source, external_id), (_id, name_key) in stored_refs.items():
        if source in HAND_SOURCES:
            by_hand_name.setdefault(name_key, []).append(external_id)
    wanted: set[tuple[str, str]] = set()
    payload: list[dict[str, Any]] = []
    modelled: set[tuple[str, str]] = set()
    for row in rows:
        value = row.model_dump(mode="json") if hasattr(row, "model_dump") else dict(row)
        source = str(value.get("source") or "manual").strip().lower() or "manual"
        if not value.get("external_id"):
            free = [e for e in by_hand_name.get(_key(value.get("name")), []) if (source, e) not in wanted]
            value["external_id"] = free[0] if free and source in HAND_SOURCES else str(uuid.uuid4())
            if hasattr(row, "external_id"):
                row.external_id = value["external_id"]
        wanted.add((source, value["external_id"]))
        if hasattr(row, "model_dump"):
            modelled.add((source, value["external_id"]))
        payload.append(value)
    gone = [i for ref, (i, _n) in stored_refs.items() if ref not in wanted] if remove_missing else []
    result = WriteResult()
    for start in range(0, len(payload), MAX_BATCH):
        part = await upsert_records(
            db,
            organization_id,
            payload[start : start + MAX_BATCH],
            origin=origin,
            actor=actor,
            keep_software=frozenset(modelled),
        )
        result.created += part.created
        result.updated += part.updated
        result.unchanged += part.unchanged
    for start in range(0, len(gone), MAX_BATCH):
        result.removed += await delete_records(
            db, organization_id, gone[start : start + MAX_BATCH], origin=origin, actor=actor
        )
    for group_id in gone_groups:
        await delete_group(db, organization_id, group_id, origin=origin, actor=actor)
    return result


async def _apply_profile_groups(
    db: AsyncSession, organization_id: uuid.UUID, groups: Sequence[Any], *, origin: str, actor: str
) -> list[uuid.UUID]:
    """Upsert the groups of an old client's ``asset_groups`` (by source and id, else by name); answer the gone ones.

    Written before the assets, so an asset naming a new group finds it; the
    groups the list no longer holds are answered for deletion after the assets.
    """
    stored = list(
        (
            await db.execute(
                select(OrganizationAssetGroup).where(OrganizationAssetGroup.organization_id == organization_id)
            )
        ).scalars()
    )
    by_ref = {(g.source, g.external_id): g for g in stored}
    by_name = {g.name_key: g for g in stored}
    kept: set[uuid.UUID] = set()
    for row in groups:
        value = row.model_dump(mode="json") if hasattr(row, "model_dump") else dict(row)
        source = str(value.get("source") or "manual").strip().lower() or "manual"
        found = by_ref.get((source, str(value.get("external_id") or ""))) if value.get("external_id") else None
        found = found or by_name.get(_key(value.get("name"))[:200])
        if found is not None and value == group_row(found):
            kept.add(found.id)
            continue
        group = await upsert_group(
            db,
            organization_id,
            value,
            origin=origin,
            actor=actor,
            group_id=found.id if found is not None else None,
        )
        kept.add(group.id)
        if hasattr(row, "external_id") and not getattr(row, "external_id", ""):
            row.external_id = group.external_id
    return [g.id for g in stored if g.id not in kept]


# ── The agent row tools (upsert_organization_rows, remove_organization_rows) ──


def _section_model(section: str) -> tuple[Any, Any]:
    if section == "assets":
        return OrganizationAssetRecord, record_row
    if section == "asset_groups":
        return OrganizationAssetGroup, group_row
    raise RecordError("section must be assets or asset_groups")


async def rows_by_ref(
    db: AsyncSession, organization_id: uuid.UUID, section: str, refs: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """The stored rows of *section* that *refs* ``(source, external_id)`` name, as profile rows."""
    model, as_row = _section_model(section)
    await ensure_records(db, organization_id)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    unique = list(dict.fromkeys(refs))
    for start in range(0, len(unique), 1000):
        chunk = unique[start : start + 1000]
        for item in (
            await db.execute(
                select(model).where(
                    model.organization_id == organization_id,
                    tuple_(model.source, model.external_id).in_(chunk),
                )
            )
        ).scalars():
            out[(item.source, item.external_id)] = as_row(item)
    return out


async def name_owners(
    db: AsyncSession, organization_id: uuid.UUID, section: str, names: Iterable[str], *, source: str
) -> dict[str, str]:
    """The name keys among *names* a stored row already holds, mapped to its source.

    An asset of another source with the same name is a second record of the
    same thing (the store merges them): only *source*'s own records clash.
    Any group with the name clashes (one group per name).
    """
    model, _ = _section_model(section)
    keys = list({_key(n)[:200] for n in names if _key(n)})
    out: dict[str, str] = {}
    for start in range(0, len(keys), 1000):
        query = select(model.name_key, model.source).where(
            model.organization_id == organization_id, model.name_key.in_(keys[start : start + 1000])
        )
        if section == "assets":
            query = query.where(model.source == source)
        out.update(_as_map((await db.execute(query)).all()))
    return out


async def source_refs(
    db: AsyncSession, organization_id: uuid.UUID, section: str, source: str
) -> list[tuple[str, str, str]]:
    """``(external_id, name, sync_id)`` of every row of *section* from *source* (an import's gone rows).

    Three short columns per row, read by index: a sync of a million rows never
    loads their fields.
    """
    model, _ = _section_model(section)
    rows = await db.execute(
        select(model.external_id, model.name, model.sync_id).where(
            model.organization_id == organization_id, model.source == source
        )
    )
    return [(str(e), str(n), str(s or "")) for e, n, s in rows.all()]


async def write_rows(
    db: AsyncSession,
    organization_id: uuid.UUID,
    section: str,
    values: Sequence[Mapping[str, Any]],
    *,
    origin: str,
    actor: str,
) -> WriteResult:
    """Write validated rows of *section* by ``(source, external_id)``: the records or the groups they name."""
    if section == "assets":
        return await upsert_records(db, organization_id, values, origin=origin, actor=actor)
    _section_model(section)
    result = WriteResult()
    refs = list({(str(v["source"]), str(v["external_id"])) for v in values})
    stored: dict[tuple[str, str], OrganizationAssetGroup] = {}
    if refs:
        query = select(OrganizationAssetGroup).where(
            OrganizationAssetGroup.organization_id == organization_id,
            tuple_(OrganizationAssetGroup.source, OrganizationAssetGroup.external_id).in_(refs),
        )
        stored = {(g.source, g.external_id): g for g in (await db.execute(query)).scalars()}
    for value in values:
        found = stored.get((str(value["source"]), str(value["external_id"])))
        group = await upsert_group(
            db, organization_id, value, origin=origin, actor=actor, group_id=found.id if found is not None else None
        )
        (result.updated if found is not None else result.created).append(group.id)
    return result


async def remove_rows(
    db: AsyncSession,
    organization_id: uuid.UUID,
    section: str,
    refs: Sequence[tuple[str, str]],
    *,
    origin: str,
    actor: str,
) -> list[uuid.UUID]:
    """Delete the rows of *section* that *refs* ``(source, external_id)`` name (an unknown ref is skipped)."""
    model, _ = _section_model(section)
    if not refs:
        return []
    unique = list(dict.fromkeys(refs))
    ids: list[uuid.UUID] = []
    for start in range(0, len(unique), 1000):
        ids.extend(
            (
                await db.execute(
                    select(model.id).where(
                        model.organization_id == organization_id,
                        tuple_(model.source, model.external_id).in_(unique[start : start + 1000]),
                    )
                )
            ).scalars()
        )
    if section == "assets":
        # Removals are chunked like writes: an import or sync whose gone set passes
        # MAX_BATCH deletes in batches instead of failing on every run.
        removed: list[uuid.UUID] = []
        for start in range(0, len(ids), MAX_BATCH):
            removed.extend(
                await delete_records(db, organization_id, ids[start : start + MAX_BATCH], origin=origin, actor=actor)
            )
        return removed
    for group_id in ids:
        await delete_group(db, organization_id, group_id, origin=origin, actor=actor)
    return ids


# ── Reads ─────────────────────────────────────────────────────────────


async def asset_rows(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    limit: int = 1000,
    after: tuple[str, uuid.UUID] | None = None,
    category: str | None = None,
    source: str | None = None,
    sync_id: str | None = None,
) -> list[OrganizationAssetRecord]:
    """One keyset page of records, ordered by name then id; pass the last ``(name_key, id)`` as *after*."""
    rec = OrganizationAssetRecord
    query = select(rec).where(rec.organization_id == organization_id)
    if category:
        query = query.where(rec.category == category)
    if source:
        query = query.where(rec.source == source)
    if sync_id is not None:
        query = query.where(rec.sync_id == sync_id)
    if after is not None:
        query = query.where(or_(rec.name_key > after[0], and_(rec.name_key == after[0], rec.id > after[1])))
    query = query.order_by(rec.name_key, rec.id).limit(max(1, min(limit, MAX_BATCH)))
    return list((await db.execute(query)).scalars())


async def profile_lists(
    db: AsyncSession, organization_id: uuid.UUID, *, cap: int = 5000
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """``(assets_exposure, asset_groups, truncated)`` built from the tables, for the readers of one release."""
    page = await asset_rows(db, organization_id, limit=cap + 1)
    groups = list(
        (
            await db.execute(
                select(OrganizationAssetGroup)
                .where(OrganizationAssetGroup.organization_id == organization_id)
                .order_by(OrganizationAssetGroup.name_key)
            )
        ).scalars()
    )
    return [record_row(r) for r in page[:cap]], [group_row(g) for g in groups], len(page) > cap


async def groups_out(db: AsyncSession, organization_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every asset group as the row API answers it, by name, with its distinct asset count (``member_count``)."""
    members = await group_member_counts(db, organization_id)
    groups = (
        await db.execute(
            select(OrganizationAssetGroup)
            .where(OrganizationAssetGroup.organization_id == organization_id)
            .order_by(OrganizationAssetGroup.name_key)
        )
    ).scalars()
    return [{**group_out(g), "member_count": int(members.get(g.id, 0))} for g in groups]


async def list_counts(db: AsyncSession, organization_id: uuid.UUID) -> dict[str, int]:
    """``{assets_exposure, asset_groups}``: how many records and groups the organization holds (two counts)."""
    out: dict[str, int] = {}
    for name, model in (("assets_exposure", OrganizationAssetRecord), ("asset_groups", OrganizationAssetGroup)):
        out[name] = int(
            (
                await db.execute(
                    select(func.count()).select_from(model).where(model.organization_id == organization_id)
                )
            ).scalar()
            or 0
        )
    return out


async def counts(db: AsyncSession, organization_id: uuid.UUID) -> dict[str, Any]:
    """Counts by category, criticality and internet exposure (the prompt summary, the facets)."""
    rec = OrganizationAssetRecord
    out: dict[str, Any] = {}
    for column in (rec.category, rec.criticality, rec.internet_facing):
        rows = (
            await db.execute(
                select(column, func.count()).where(rec.organization_id == organization_id).group_by(column)
            )
        ).all()
        out[column.key] = {str(k): int(n) for k, n in rows}
    out["total"] = sum(out["category"].values())
    return out


async def changes(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    entity_id: uuid.UUID | None = None,
    since: datetime | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The change log, newest first (one asset record or group with *entity_id*); page with *before_id*."""
    chg = OrganizationAssetChange
    query = select(chg).where(chg.organization_id == organization_id)
    if entity_id is not None:
        query = query.where(chg.entity_id == entity_id)
    if since is not None:
        query = query.where(chg.at >= since)
    if before_id is not None:
        query = query.where(chg.id < before_id)
    rows = (await db.execute(query.order_by(chg.id.desc()).limit(max(1, min(limit, 200))))).scalars()
    return [
        {
            "id": c.id,
            "entity": c.entity,
            "entity_id": str(c.entity_id),
            "op": c.op,
            "origin": c.origin,
            "actor": c.actor,
            "before": c.before,
            "after": c.after,
            "at": c.at.isoformat(),
        }
        for c in rows
    ]


async def restore(
    db: AsyncSession, organization_id: uuid.UUID, change_ids: Sequence[int], *, actor: str
) -> WriteResult:
    """Undo record changes: a delete comes back, an update gets its ``before`` fields, a create is removed."""
    if len(change_ids) > MAX_BATCH:
        raise RecordError(f"at most {MAX_BATCH} changes per call")
    rows = list(
        (
            await db.execute(
                select(OrganizationAssetChange)
                .where(
                    OrganizationAssetChange.organization_id == organization_id,
                    OrganizationAssetChange.id.in_(list(change_ids)),
                    OrganizationAssetChange.entity == "record",
                )
                .order_by(OrganizationAssetChange.id.desc())
            )
        ).scalars()
    )
    result = WriteResult()
    for change in rows:
        if change.op == "delete" and change.before:
            res = await upsert_records(db, organization_id, [change.before], origin="restore", actor=actor)
            result.created += res.created
            result.updated += res.updated
        elif change.op == "update" and change.before is not None:
            try:
                record = await get_record(db, organization_id, change.entity_id)
            except RecordNotFoundError:
                continue
            await update_record(db, organization_id, record.id, change.before, origin="restore", actor=actor)
            result.updated.append(record.id)
        elif change.op == "create":
            result.removed += await delete_records(
                db, organization_id, [change.entity_id], origin="restore", actor=actor
            )
    return result
