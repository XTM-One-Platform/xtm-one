# SPDX-License-Identifier: Apache-2.0

"""The organization profile inputs a CTEM posture build reads, and their fingerprint.

A cached posture (``api/v1/ctem_metrics.py``) is stale when the profile inputs
it was built from changed. Before this module it was stamped with the profile
VERSION, so any save (a description, a PIR detail, Owned domains) and any
sync writing a new version rebuilt every user's posture on their next visit
(20 to 30 s of "Syncing..."). The stamp is now a fingerprint of what the build
actually reads, declared here and nowhere else:

* the typed software rows of the profile (``tech_stack``: :data:`TYPED_SOFTWARE_FIELDS`)
  and the Software entities of the store (``organization_software_store.matcher_software``),
  read by ``ctem_metrics_collector._organization_software`` for the CVE matcher;
* the asset rows and asset groups (``assets_exposure``, ``asset_groups``), read by
  ``ctem_metrics_collector._profile_asset_rows`` for the reachability split and
  the Exposed join (``asset_reachability``): :data:`ASSET_FIELDS`, :data:`GROUP_FIELDS`.

Not inputs: Owned domains, address ranges, the policy, the PIRs, the
descriptions (the build never reads them), and ``missing_fields`` (applied by
``ctem_metrics._with_context`` on every read, never cached). A field another
release makes the build read is added here: ``tests/test_ctem_posture_inputs.py``
fails when the build reads a profile field, an asset field or a group field
this module does not declare.

Cheap per request: the profile row is the one the version stamp already read;
the asset records are hashed from the declared columns only (no row model, no
``AssetEntry`` validation), the Software store from its matcher read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: The profile fields the posture build reads (``OrganizationProfile`` attributes).
PROFILE_FIELDS: tuple[str, ...] = ("tech_stack", "assets_exposure", "asset_groups")
#: The fields of a typed software row the CVE matcher reads.
TYPED_SOFTWARE_FIELDS: tuple[str, ...] = ("vendor", "product", "cpe", "version")
#: The fields of an asset row the reachability rules read (``asset_reachability``).
ASSET_FIELDS: tuple[str, ...] = (
    "name",
    "source",
    "external_id",
    "category",
    "subcategory",
    "type",
    "internet_facing",
    "group",
)
#: The fields of an asset group the reachability rules read.
GROUP_FIELDS: tuple[str, ...] = ("name", "internet_facing")

#: The record columns a declared field is read from; the rest are read from ``fields``.
_RECORD_COLUMNS: frozenset[str] = frozenset({"source", "external_id"})


def _rows(rows: Iterable[Any] | None) -> list[Any]:
    """The dict rows (or projected value lists) of *rows*; anything else is not an input."""
    return [r for r in rows or () if isinstance(r, Mapping | list)]


def _project(rows: Iterable[Any] | None, fields: tuple[str, ...]) -> list[list[Any]]:
    return [[row.get(f) for f in fields] for row in rows or () if isinstance(row, Mapping)]


def fingerprint(
    organization_id: uuid.UUID | str,
    *,
    typed_software: Iterable[Any],
    software: Iterable[Any],
    assets: Iterable[Any],
    groups: Iterable[Any],
) -> str:
    """``<organization id>:<hash>`` of the declared inputs; equal inputs give an equal stamp.

    *typed_software*, *assets* and *groups* are rows as dicts (the profile JSON
    or the records); only the declared fields count, so a field the build never
    reads (``last_seen``, ``sync_id``, ``owner``...) never changes the stamp.
    *assets* holds dicts or the value lists :func:`_record_values` projects
    (in :data:`ASSET_FIELDS` order). *software* is ``matcher_software``'s answer.
    """
    asset_values = [list(a) if isinstance(a, list) else [a.get(f) for f in ASSET_FIELDS] for a in _rows(assets)]
    body = {
        "typed_software": sorted(_project(typed_software, TYPED_SOFTWARE_FIELDS), key=json.dumps),
        "software": sorted(
            (
                [s.get("vendor"), s.get("product"), s.get("cpe"), sorted(str(v) for v in s.get("versions") or [])]
                for s in software or ()
                if isinstance(s, Mapping)
            ),
            key=json.dumps,
        ),
        "assets": sorted(asset_values, key=lambda v: json.dumps(v, default=str)),
        "groups": sorted(_project(groups, GROUP_FIELDS), key=lambda v: json.dumps(v, default=str)),
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:24]
    return f"{organization_id}:{digest}"


async def _record_values(db: AsyncSession, organization_id: uuid.UUID) -> tuple[list[list[Any]], list[dict[str, Any]]]:
    """The declared asset and group fields of the records, read column by column (no row model)."""
    from app.models.organization_asset_record import OrganizationAssetGroup, OrganizationAssetRecord

    rec = OrganizationAssetRecord
    columns = [getattr(rec, f) if f in _RECORD_COLUMNS else rec.fields[f].astext.label(f) for f in ASSET_FIELDS]
    assets = [list(r) for r in (await db.execute(select(*columns).where(rec.organization_id == organization_id))).all()]
    grp = OrganizationAssetGroup
    groups = [
        dict(zip(GROUP_FIELDS, r, strict=True))
        for r in (
            await db.execute(
                select(*(grp.fields[f].astext.label(f) for f in GROUP_FIELDS)).where(
                    grp.organization_id == organization_id
                )
            )
        ).all()
    ]
    return assets, groups


async def posture_inputs_stamp(db: AsyncSession | None) -> str | None:
    """The fingerprint of the primary organization's posture inputs, ``None`` when no profile is saved.

    Reads what the build reads, from the same sources: the asset records once
    imported (#1543), the profile lists before; the Software store (#1495).
    An unreadable input reads as none, never a 500 (the posture is then
    rebuilt once, as before).
    """
    if db is None:
        return None
    from app.services import organization_asset_records as records
    from app.services import organization_context as org_context
    from app.services import organization_software_store as software_store

    try:
        row = await org_context.current_version(db)
        if row is None:
            return None
        raw = row.profile if isinstance(row.profile, Mapping) else {}
        organization_id = row.organization_id
        if organization_id is not None and await records.is_imported(db, organization_id):
            assets, groups = await _record_values(db, organization_id)
        else:
            assets = list(raw.get("assets_exposure") or [])
            groups = list(raw.get("asset_groups") or [])
        software = await software_store.matcher_software(db, organization_id) if organization_id is not None else []
    except Exception as exc:  # an unreadable profile reads as none, never a 500
        logger.info("CTEM posture inputs unreadable: %s", format_exception_message(exc))
        return None
    return fingerprint(
        organization_id or "",
        typed_software=list(raw.get("tech_stack") or []),
        software=software,
        assets=assets,
        groups=groups,
    )
