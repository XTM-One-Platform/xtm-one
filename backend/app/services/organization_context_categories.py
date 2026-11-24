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

"""The categories of the organization context, and how an agent resolves them (#11533).

The profile has a short identity (name, sector, regions, size, risk
appetite) and long lists: the crown jewels, the software,
the PIRs, the tracked organizations. The identity is always in the prompt
(``organization_context_injection``); a list is a CATEGORY: announced in the
prompt with its size, preloaded only when the cascade selects it, and read
page by page through ``get_organization_context(category=..., query=...,
offset=..., limit=...)`` whatever its length.

The registry is generated from :class:`OrganizationProfile`: every list
field that is not identity is a category, so a list added to the model is a
category without a change here. ``tracked_organizations`` is the one category
that does not live in the profile (``services/organizations.py``).

Three categories came with #11543: ``assets_exposure`` and
``enterprise_knowledge`` are list fields (generated like the others), and
``prioritization_policy`` is an object, so it is declared in
:data:`_OBJECT_CATEGORIES` and read as one row per explicit rule.
``enterprise_knowledge`` is answered by ``organization_enterprise_knowledge``
(the linked knowledge bases, and passages for a query, searched only where
the caller can read).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, get_origin

from pydantic import BaseModel

from app.services.organization_context import CONTRACT_FIELDS, OrganizationProfile

#: List fields of the profile that are identity or bookkeeping, never a category.
#: ``fx_rates`` (#1383) is a handful of rates shown with the currency in the basic block.
_NOT_CATEGORIES: frozenset[str] = frozenset({"regions", "onboarding_skipped", "fx_rates"})
#: The category read from the tracked organizations, not from the profile.
TRACKED_ORGANIZATIONS = "tracked_organizations"
#: Object fields of the profile read as a category, one row per explicit rule (#11543).
#: ``attack_surface`` (CTEM-26) is one row, its settings, once any differs from the default (#1691).
_OBJECT_CATEGORIES: tuple[str, ...] = ("prioritization_policy", "attack_surface")
#: The category answered with the linked knowledge bases and their passages (#11543).
ENTERPRISE_KNOWLEDGE = "enterprise_knowledge"
#: Labels of the fields that are not contract fields (``CONTRACT_FIELDS`` names the others).
_LABELS: dict[str, str] = {
    "assets_exposure": "assets and exposure",
    "asset_groups": "asset groups",
    "shipped_products": "shipped products",
    "prioritization_policy": "prioritization policy",
    "attack_surface": "attack surface pass: enabled, provider integrations, cadence",
    "software": "software: vendor, product, type, versions, CPE, asset count",
    ENTERPRISE_KNOWLEDGE: "procedures and reference documents, read by query",
}
#: Rows a page answers by default, and at most.
DEFAULT_PAGE = 50
MAX_PAGE = 200
#: Cap of ONE preloaded category in the prompt, in characters (about 1,500 tokens).
PRELOAD_MAX_CHARS = 6000


@dataclass(frozen=True)
class Category:
    """One category: its name in tools and settings, the profile field it reads, its label."""

    name: str
    field: str
    label: str


#: The profile field the Software category reads before the store is built (#1495).
SOFTWARE_FIELD = "software"
#: The category of the organization's software (#1495): the Software entity. No alias (Sam, 24 Nov 2026).
SOFTWARE = "software"
#: The profile field the primary assets are stored under (#1591: OpenCRQ's primary assets, the stored key kept).
PRIMARY_ASSETS_FIELD = "crown_jewels"
#: The category of the organization's primary assets (#1591), in OpenCRQ's words.
PRIMARY_ASSETS = "primary_assets"
#: Deprecated since #1591 (kept one release): ``crown_jewels`` answers the ``primary_assets`` category.
CROWN_JEWELS_ALIAS = "crown_jewels"
#: Each deprecated category name, to the category it answers.
CATEGORY_ALIASES: dict[str, str] = {CROWN_JEWELS_ALIAS: PRIMARY_ASSETS}


def _category_name(field: str) -> str:
    if field == SOFTWARE_FIELD:
        return SOFTWARE
    if field == PRIMARY_ASSETS_FIELD:
        return PRIMARY_ASSETS
    # ``pir_ids`` is read as ``pirs``: a category names what it lists.
    return field[: -len("_ids")] + "s" if field.endswith("_ids") else field


def _registry() -> tuple[Category, ...]:
    labels = {**dict(CONTRACT_FIELDS), **_LABELS}
    found: list[Category] = []
    for field, info in OrganizationProfile.model_fields.items():
        if field in _NOT_CATEGORIES or (get_origin(info.annotation) is not list and field not in _OBJECT_CATEGORIES):
            continue
        found.append(Category(_category_name(field), field, labels.get(field, field.replace("_", " "))))
    found.append(Category(TRACKED_ORGANIZATIONS, "", "tracked organizations"))
    return tuple(found)


#: Every category, in the model's order, ``tracked_organizations`` last.
CATEGORIES: tuple[Category, ...] = _registry()
CATEGORY_NAMES: tuple[str, ...] = tuple(category.name for category in CATEGORIES)
_BY_NAME = {category.name: category for category in CATEGORIES}


def available() -> tuple[Category, ...]:
    """The categories this platform's mode exposes: every one, none in Copilot mode (#1690).

    Copilot has no CTEM loop: its agents read the identity alone, so no category is
    listed, readable, preloaded or offered in a picker. The stored data stays untouched.
    """
    from app.services.organization_context import identity_only

    return () if identity_only() else CATEGORIES


def available_names() -> tuple[str, ...]:
    """The names of :func:`available`."""
    return tuple(category.name for category in available())


def canonical_name(name: object) -> str:
    """*name* lower-cased, a deprecated alias read as the category it answers (``crown_jewels`` -> ``primary_assets``)."""
    wanted = str(name or "").strip().lower()
    return CATEGORY_ALIASES.get(wanted, wanted)


def get(name: str) -> Category | None:
    return _BY_NAME.get(canonical_name(name))


def normalize_categories(value: object) -> list[str]:
    """A stored list, deduplicated, unknown names dropped (a category removed from the model)."""
    if not isinstance(value, list | tuple):
        return []
    return [name for name in dict.fromkeys(canonical_name(v) for v in value) if name in _BY_NAME]


def check_categories(value: object) -> list[str] | None:
    """A submitted selection: ``None`` (inherit) or a list of known names (``ValueError`` names the accepted ones).

    A string is read as a comma-separated list; ``"inherit"`` or ``""`` is
    ``None``; ``"none"`` is the explicit empty list.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "inherit"):
            return None
        if text == "none":
            return []
        value = [part for part in (p.strip() for p in text.split(",")) if part]
    if not isinstance(value, list | tuple):
        raise ValueError("organization_context_categories must be a list of category names")
    names = [canonical_name(v) for v in value]
    unknown = [name for name in names if name not in _BY_NAME]
    if unknown:
        raise ValueError(
            f"organization_context_categories {', '.join(unknown)} unknown; one of {', '.join(CATEGORY_NAMES)}"
        )
    return list(dict.fromkeys(names))


def checked_for_api(value: object) -> list[str] | None:
    """A REST body's list, checked: ``HTTPException(422)`` naming the accepted categories."""
    from fastapi import HTTPException

    try:
        return check_categories(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def resolve_categories(
    *,
    platform_categories: object,
    agent_categories: object = None,
    assignment_categories: object = None,
) -> list[str]:
    """The preloaded categories: assignment > agent > platform, the most specific explicit list wins.

    ``None`` inherits; an explicit empty list preloads nothing.
    """
    for value in (assignment_categories, agent_categories):
        if value is not None:
            return normalize_categories(value)
    return normalize_categories(platform_categories)


def rows_of(profile: OrganizationProfile, category: Category) -> list[Any]:
    """The rows of a profile category, as JSON values."""
    if category.field == "prioritization_policy":
        return policy_rows(profile)
    if category.field == "attack_surface":
        return attack_surface_rows(profile)
    values = getattr(profile, category.field, None) or []
    if category.field == "pir_ids":
        # #1773: each PIR with what the organization says about it, when it says something.
        rows: list[Any] = []
        for pir in values:
            row: dict[str, Any] = {"id": pir, "name": profile.pir_names.get(pir, "")}
            detail = profile.pir_details.get(pir)
            if detail is not None:
                row.update({k: v for k, v in detail.model_dump(mode="json").items() if v not in ("", [])})
            rows.append(row)
        return rows
    return [value.model_dump(mode="json") if isinstance(value, BaseModel) else value for value in values]


def policy_rows(profile: OrganizationProfile) -> list[dict[str, Any]]:
    """The prioritization policy as rows: each rule set away from its default, each exclusion, each accepted risk.

    A policy left at its defaults has no row (the category is not filled, and
    the funnel ranks as before); the count matches ``exposure_counts``.
    """
    from app.services.organization_context import PrioritizationPolicy

    policy = profile.prioritization_policy
    defaults = PrioritizationPolicy()
    rows: list[dict[str, Any]] = [
        {"rule": name, "value": value}
        for name, value in policy.model_dump(mode="json", exclude={"exclusions", "accepted_risks"}).items()
        if getattr(policy, name) != getattr(defaults, name)
    ]
    rows += [{"rule": "exclusion", **item.model_dump(mode="json")} for item in policy.exclusions]
    rows += [{"rule": "accepted_risk", **item.model_dump(mode="json")} for item in policy.accepted_risks]
    return rows


def attack_surface_rows(profile: OrganizationProfile) -> list[dict[str, Any]]:
    """The attack surface pass as one row of its settings; none while every setting is the default (#1691)."""
    from app.services.organization_context import AttackSurfacePass

    surface = profile.attack_surface
    return [] if surface == AttackSurfacePass() else [surface.model_dump(mode="json")]


def tracked_rows(organizations: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": o.name,
            "id": str(o.id),
            "relationship": o.relationship,
            "criticality": o.criticality,
            "monitoring": o.monitoring,
            "opencti_organization_id": getattr(o, "opencti_organization_id", None),
        }
        for o in organizations
        if getattr(o, "role", None) == "tracked"
    ]


def matches(row: Any, query: str) -> bool:
    """Whether *row* holds *query* in any of its values (case-insensitive substring)."""
    needle = query.strip().lower()
    if not needle:
        return True
    values = row.values() if isinstance(row, dict) else [row]
    return any(needle in str(value).lower() for value in values)


#: Categories the index names only once filled (#1432): most organizations ship nothing, and the
#: basic block keeps its size for every profile that does not fill them.
OPTIONAL_CATEGORIES: frozenset[str] = frozenset({"shipped_products", "attack_surface"})


def indexed(category: Category, count: int) -> bool:
    """Whether the category index of the basic block names *category*."""
    return bool(count) or category.name not in OPTIONAL_CATEGORIES


def index_line(category: Category, count: int) -> str:
    """One line of the category index the basic block carries."""
    state = f"{count} row{'s' if count != 1 else ''}" if count else "not filled"
    call = f'get_organization_context(category="{category.name}")'
    return f"- {category.name} ({category.label}): {state}" + (f"; {call}" if count else "")


def render_page(
    category: Category, rows: list[Any], *, query: str = "", offset: int = 0, limit: int = DEFAULT_PAGE
) -> str:
    """One page of a category as an agent reads it: the position, the rows quarantined, the next call."""
    from app.services.content_safety import wrap_untrusted

    limit = max(1, min(int(limit or DEFAULT_PAGE), MAX_PAGE))
    offset = max(0, int(offset or 0))
    found = [row for row in rows if matches(row, query)] if query.strip() else rows
    page = found[offset : offset + limit]
    scope = f" matching {query.strip()!r}" if query.strip() else ""
    if not found:
        return f"Category {category.name} ({category.label}): no row{scope} (of {len(rows)}). Do not guess it."
    head = f"Category {category.name} ({category.label}): rows {offset + 1}-{offset + len(page)} of {len(found)}{scope}"
    if not page:
        head = f"Category {category.name} ({category.label}): offset {offset} is past the {len(found)} rows{scope}"
    nxt = offset + len(page)
    tail = (
        f'More: get_organization_context(category="{category.name}"'
        + (f", query={query.strip()!r}" if scope else "")
        + f", offset={nxt}, limit={limit})."
        if nxt < len(found)
        else "End of the category."
    )
    return "\n".join([head + ".", wrap_untrusted(json.dumps(page, ensure_ascii=False), "organization profile"), tail])


#: The ``field:value`` terms an agent's query on the assets category filters on (#1774): the
#: CTEM Inventory list's filters (``organization_asset_store``), ``source`` and ``group`` included.
ASSET_QUERY_FIELDS: tuple[str, ...] = (
    "category",
    "subcategory",
    "criticality",
    "internet_facing",
    "os_family",
    "source",
    "group",
)


def split_asset_query(query: str) -> tuple[dict[str, list[str]], str]:
    """``(filters, name text)`` of an assets query: ``os_family:windows source:intune web`` (#1774).

    A ``field:value`` term on a known field is a filter (a repeated field ORs its values,
    fields AND); every other word stays the name substring.
    """
    filters: dict[str, list[str]] = {}
    words: list[str] = []
    for term in str(query or "").split():
        name, sep, value = term.partition(":")
        if sep and name.lower() in ASSET_QUERY_FIELDS and value:
            filters.setdefault(name.lower(), []).append(value)
        else:
            words.append(term)
    return filters, " ".join(words)


def render_asset_page(rows: list[dict[str, Any]], *, total: int, query: str, offset: int, limit: int) -> str:
    """One page of the merged CTEM Inventory assets as an agent reads it (#1774)."""
    from app.services.content_safety import wrap_untrusted

    scope = f" matching {query.strip()!r}" if query.strip() else ""
    name = "assets_exposure (merged CTEM Inventory assets, one row per real asset)"
    if not total:
        return f"Category {name}: no row{scope}. Do not guess it."
    if not rows:
        return f"Category {name}: offset {offset} is past the {total} rows{scope}."
    nxt = offset + len(rows)
    tail = (
        'More: get_organization_context(category="assets_exposure"'
        + (f", query={query.strip()!r}" if scope else "")
        + f", offset={nxt}, limit={limit})."
        if nxt < total
        else "End of the category."
    )
    head = f"Category {name}: rows {offset + 1}-{nxt} of {total}{scope}."
    return "\n".join([head, wrap_untrusted(json.dumps(rows, ensure_ascii=False), "organization profile"), tail])


def render_categories(counts: dict[str, int]) -> str:
    """What ``get_organization_context(category="*")`` answers: every category with its size."""
    lines = [
        "Organization context categories (read one by name, e.g. "
        'get_organization_context(category="software"), narrow it with query=, page it with offset= and limit=, up to '
        f"{MAX_PAGE} rows a page):"
    ]
    if not available():
        return (
            "No organization context category on this platform: the profile is its identity alone "
            "(get_organization_context() reads it)."
        )
    lines += [index_line(category, counts.get(category.name, 0)) for category in available()]
    return "\n".join(lines)


def render_preloaded(category: Category, rows: list[Any]) -> str:
    """A category preloaded into the prompt, at most :data:`PRELOAD_MAX_CHARS`, the rest named with its call."""
    from app.services.content_safety import wrap_untrusted

    if not rows:
        return f"Preloaded {category.name} ({category.label}): not filled. Do not guess it."
    kept = list(rows)
    while True:
        shown = len(kept)
        rest = (
            f" Showing {shown} of {len(rows)}; the rest: "
            f'get_organization_context(category="{category.name}", offset={shown}).'
            if shown < len(rows)
            else ""
        )
        text = "\n".join(
            [
                f"Preloaded {category.name} ({category.label}), {len(rows)} rows.{rest}",
                wrap_untrusted(json.dumps(kept, ensure_ascii=False), "organization profile", stable=True),
            ]
        )
        if len(text) <= PRELOAD_MAX_CHARS or shown <= 1:
            return text if len(text) <= PRELOAD_MAX_CHARS else text[: PRELOAD_MAX_CHARS - 3] + "..."
        # Halve while far over, then one row at a time, so a 500-row list stays cheap.
        kept = kept[: max(1, shown // 2)] if len(text) > 2 * PRELOAD_MAX_CHARS else kept[:-1]
