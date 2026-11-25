# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Assets and exposure from a CSV file (#11543): a preview with per-row errors, then the merge.

The administrator uploads a CSV, reads the preview (every row parsed or
refused with its reason, the primary asset links it adds, the rows it would
replace) and only then applies it: :func:`merge_assets` returns the next
profile, which the API saves as a new version. Nothing is written by
:func:`parse_assets_csv`.

Columns (header row, case-insensitive, any order; only ``name`` is required):
``name``, ``category`` and ``subcategory`` (OpenAEV's taxonomy, #11781),
``criticality``, ``environment``, ``internet_facing``, ``owner``,
``group``, ``products`` and ``tags`` (separated by ``;`` or ``|``),
``primary_assets`` (primary asset names the asset runs, ``;`` separated),
``external_id``. The flat ``type`` of #11543 is still read, as its category
and subcategory; a ``type`` of ``asset_group`` adds the row to ``asset_groups``.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.services.organization_context import (
    LEGACY_ASSET_GROUP_TYPE,
    AssetEntry,
    AssetGroupEntry,
    OrganizationProfile,
    legacy_group_row,
)

#: Rows one file may carry (the profile caps ``assets_exposure`` at 5,000).
MAX_ROWS = 5000
#: Bytes one file may weigh.
MAX_BYTES = 2 * 1024 * 1024
COLUMNS: tuple[str, ...] = (
    "name",
    "category",
    "subcategory",
    "criticality",
    "type",
    "environment",
    "internet_facing",
    "owner",
    "group",
    "products",
    "tags",
    "primary_assets",
    "external_id",
)
_LIST_COLUMNS = ("products", "tags", "primary_assets")
#: Words a spreadsheet writes for ``internet_facing``, read as the stored value.
_YES_NO = {
    "yes": "yes",
    "y": "yes",
    "true": "yes",
    "1": "yes",
    "no": "no",
    "n": "no",
    "false": "no",
    "0": "no",
    "unknown": "unknown",
    "": "unknown",
}


@dataclass
class RowError:
    line: int
    name: str
    message: str


@dataclass
class AssetsPreview:
    assets: list[AssetEntry] = field(default_factory=list)
    #: The groups the file declares (rows typed ``asset_group``), upserted by name.
    groups: list[AssetGroupEntry] = field(default_factory=list)
    #: primary asset name -> asset names the file links to it.
    links: dict[str, list[str]] = field(default_factory=dict)
    errors: list[RowError] = field(default_factory=list)
    #: Primary asset names the file names that the profile does not hold.
    unknown_primary_assets: list[str] = field(default_factory=list)
    #: Asset names already in the profile that the file replaces.
    replaced: list[str] = field(default_factory=list)


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.replace("|", ";").split(";") if part.strip()]


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ())) or "row"
    return f"{where}: {first.get('msg', 'invalid value')}"


def parse_assets_csv(data: bytes, profile: OrganizationProfile) -> AssetsPreview:
    """Parse *data* against *profile*; never raises on a bad row (``ValueError`` on a bad file)."""
    if len(data) > MAX_BYTES:
        raise ValueError(f"the file is over {MAX_BYTES // (1024 * 1024)} MB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("the file is not UTF-8 text") from None
    reader = csv.DictReader(io.StringIO(text))
    headers = [str(h or "").strip().lower() for h in (reader.fieldnames or [])]
    if "name" not in headers:
        raise ValueError(f"the header row has no name column; the columns are {', '.join(COLUMNS)}")
    unknown = [h for h in headers if h and h not in COLUMNS]
    if unknown:
        raise ValueError(f"unknown column(s) {', '.join(unknown)}; the columns are {', '.join(COLUMNS)}")
    preview = AssetsPreview()
    pa_rows = {pa_row.name.lower(): pa_row.name for pa_row in profile.primary_assets}
    existing = {asset.name.lower() for asset in profile.assets_exposure}
    existing_groups = {group.name.lower() for group in profile.asset_groups}
    seen: set[str] = set()
    unknown_pa_rows: dict[str, None] = {}
    for line, raw in enumerate(reader, start=2):
        if len(preview.assets) + len(preview.errors) >= MAX_ROWS:
            preview.errors.append(RowError(line, "", f"more than {MAX_ROWS} rows; the rest is not read"))
            break
        row = {str(k or "").strip().lower(): str(v or "").strip() for k, v in raw.items() if k is not None}
        name = row.get("name", "")
        if not any(row.values()):
            continue
        if name.lower() in seen:
            preview.errors.append(RowError(line, name, "the name is already on an earlier row of the file"))
            continue
        facing = _YES_NO.get(row.get("internet_facing", "").lower())
        if facing is None:
            preview.errors.append(RowError(line, name, "internet_facing must be yes, no or unknown"))
            continue
        keys = (
            "name",
            "category",
            "subcategory",
            "criticality",
            "type",
            "environment",
            "owner",
            "group",
            "external_id",
        )
        values: dict[str, Any] = {key: row[key] for key in keys if row.get(key)}
        for key in ("category", "subcategory", "criticality", "type", "environment"):
            if key in values:
                values[key] = values[key].lower().replace(" ", "_")
        values.update(
            internet_facing=facing,
            products=_split(row.get("products", "")),
            tags=_split(row.get("tags", "")),
            source="csv",
        )
        if values.get("type") == LEGACY_ASSET_GROUP_TYPE and not values.get("category"):
            try:
                group = AssetGroupEntry.model_validate(
                    legacy_group_row({k: v for k, v in values.items() if k != "criticality"})
                )
            except ValidationError as exc:
                preview.errors.append(RowError(line, name, _first_error(exc)))
                continue
            seen.add(group.name.lower())
            preview.groups.append(group)
            if group.name.lower() in existing_groups:
                preview.replaced.append(group.name)
            asset_name: str = group.name
        else:
            try:
                asset = AssetEntry.model_validate(values)
            except (ValidationError, ValueError) as exc:
                message = _first_error(exc) if isinstance(exc, ValidationError) else str(exc)
                preview.errors.append(RowError(line, name, message))
                continue
            seen.add(asset.name.lower())
            preview.assets.append(asset)
            if asset.name.lower() in existing:
                preview.replaced.append(asset.name)
            asset_name = asset.name
        for pa_row in _split(row.get("primary_assets", "")):
            known = pa_rows.get(pa_row.lower())
            if known is None:
                unknown_pa_rows[pa_row] = None
                continue
            preview.links.setdefault(known, [])
            if asset_name not in preview.links[known]:
                preview.links[known].append(asset_name)
    preview.unknown_primary_assets = list(unknown_pa_rows)
    return preview


def merge_assets(profile: OrganizationProfile, preview: AssetsPreview) -> OrganizationProfile:
    """*profile* with the preview's assets upserted by name and its primary asset links added.

    Rows the file does not name are kept as they are (typed by hand or from
    another source); a row it names is replaced whole.
    """
    incoming = {asset.name.lower(): asset for asset in preview.assets}
    kept = [asset for asset in profile.assets_exposure if asset.name.lower() not in incoming]
    incoming_groups = {group.name.lower() for group in preview.groups}
    kept_groups = [group for group in profile.asset_groups if group.name.lower() not in incoming_groups]
    pa_rows = []
    for pa_row in profile.primary_assets:
        added = preview.links.get(pa_row.name, [])
        pa_rows.append(pa_row.model_copy(update={"asset_names": list(dict.fromkeys([*pa_row.asset_names, *added]))}))
    data = profile.model_dump(mode="json")
    data["assets_exposure"] = [a.model_dump(mode="json") for a in [*kept, *preview.assets]]
    data["asset_groups"] = [g.model_dump(mode="json") for g in [*kept_groups, *preview.groups]]
    data["primary_assets"] = [j.model_dump(mode="json") for j in pa_rows]
    return OrganizationProfile.model_validate(data)


def check_links(profile: OrganizationProfile) -> list[str]:
    """The problems of *profile*'s asset links: a primary asset or an asset naming an asset or group it lacks."""
    groups = {group.name.lower() for group in profile.asset_groups}
    names = {asset.name.lower() for asset in profile.assets_exposure} | groups
    problems = [
        f"primary asset {pa_row.name!r} links {name!r}, which is not an asset"
        for pa_row in profile.primary_assets
        for name in pa_row.asset_names
        if name.lower() not in names
    ]
    problems += [
        f"asset {asset.name!r} is in group {asset.group!r}, which is not an asset group"
        for asset in profile.assets_exposure
        if asset.group and asset.group.lower() not in groups
    ]
    return problems
