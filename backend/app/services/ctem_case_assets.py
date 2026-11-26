# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases: the hosts a case is validated on, recorded as case assets (#2395).

A case's assets came only from its member issues. When the loop validates a
case on hosts it chose itself (a hostname matched on the product name), the
validation's ``run_order`` named them as free text and the case read answered
no asset while its validation and remediation options named two. This module
records them with their provenance:

- :func:`resolve_names`: the canonical assets (``organization_assets``) a
  ``run_order`` name resolves to, by the folded name, with their sources;
- :func:`attach_run_order`: the validation entry's ``assets`` (each with
  ``provenance`` ``name_match_inferred``) and ``unresolved`` names;
- :func:`attached_assets`: the assets every validation of a case attached,
  newest first, one per asset;
- :func:`merged_assets` and :func:`with_attached`: the case read's ``assets``
  and the case page's affected assets, the issues' own assets first;
- :func:`complete_gate_ask`: the ``Assets:`` line of a remediation or mobilize
  gate card, so a hostname match is shown as inferred;
- :func:`enrich_source_ids`: the module ids of rows stored before #2448.

A row's ``asset_id`` is the CTEM inventory id (``organization_assets.id``,
``id_space`` ``inventory``), never a module id: the module ids are its
``source_ids`` ``[{source, external_id}]`` (#2448), so an agent compares an
OpenAEV or OpenCTI id with ``source_ids[].external_id``.

Never a guessed asset: a name that matches no asset, or several, is kept as
unresolved and attaches nothing.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: Where a case asset comes from.
PROVENANCE_ISSUE = "issue"
PROVENANCE_INVENTORY = "inventory"
PROVENANCE_INFERRED = "name_match_inferred"
PROVENANCE_ANALYST = "analyst"
#: The id space of a row's ``asset_id`` (#2448): the CTEM inventory, never a module.
ID_SPACE = "inventory"
#: ``asset_count_source`` when the attached assets are the only count.
ATTACHED_SOURCE = "attached"
#: The most attached assets a case read serves.
MAX_ATTACHED = 50
#: The line a completed gate card carries.
LINE_LEAD = "Assets:"
_WORDS: dict[str, str] = {
    PROVENANCE_ISSUE: "named by an issue",
    PROVENANCE_INVENTORY: "running the product in the inventory",
    PROVENANCE_INFERRED: "inferred from a name match",
    PROVENANCE_ANALYST: "attached by an analyst",
}
_MAX_LINE_ASSETS = 10
_MAX_CONTEXT_CHARS = 800


def _key(value: object) -> str:
    """A name folded as ``organization_assets.name_key`` stores it (pure)."""
    return " ".join(str(value or "").split()).casefold()


async def resolve_names(
    db: AsyncSession, organization_id: uuid.UUID | None, names: Iterable[str]
) -> dict[str, dict[str, Any]] | None:
    """Per folded name, the one canonical asset it names: ``{asset_id, name, sources, source_ids}``.

    A name that matches several assets is left out (ambiguous, never guessed).
    ``None`` when the store cannot be read: the caller then flags nothing.
    """
    from app.models.organization_asset import OrganizationAsset

    keys = list(dict.fromkeys(_key(n)[:300] for n in names if _key(n)))
    if not keys or organization_id is None:
        return None
    try:
        async with db.begin_nested():
            rows = (
                await db.execute(
                    select(OrganizationAsset.id, OrganizationAsset.name, OrganizationAsset.name_key).where(
                        OrganizationAsset.organization_id == organization_id,
                        OrganizationAsset.name_key.in_(keys),
                    )
                )
            ).all()
            by_key: dict[str, list[Any]] = {}
            for row in rows:
                by_key.setdefault(str(row[2]), []).append(row)
            single = {k: v[0] for k, v in by_key.items() if len(v) == 1}
            ids = await _source_ids(db, [r[0] for r in single.values()]) if single else {}
    except Exception:  # the store never breaks a validation write
        logger.info("CTEM case asset resolution unavailable", exc_info=True)
        return None
    return {
        k: {
            "asset_id": str(r[0]),
            "name": str(r[1]),
            "sources": sorted({s["source"] for s in ids.get(str(r[0]), [])}),
            "source_ids": ids.get(str(r[0]), []),
        }
        for k, r in single.items()
    }


async def _source_ids(db: AsyncSession, asset_ids: Iterable[Any]) -> dict[str, list[dict[str, str]]]:
    """Per inventory asset id, its source records ``[{source, external_id}]``, sorted (#2448)."""
    from app.models.organization_asset import OrganizationAssetSource

    wanted = list(asset_ids)
    if not wanted:
        return {}
    found = (
        await db.execute(
            select(
                OrganizationAssetSource.asset_id, OrganizationAssetSource.source, OrganizationAssetSource.external_id
            ).where(OrganizationAssetSource.asset_id.in_(wanted))
        )
    ).all()
    out: dict[str, list[dict[str, str]]] = {}
    for asset_id, source, external_id in found:
        if not source or not external_id:
            continue
        pair = {"source": str(source), "external_id": str(external_id)}
        kept = out.setdefault(str(asset_id), [])
        if pair not in kept:
            kept.append(pair)
    return {k: sorted(v, key=lambda p: (p["source"], p["external_id"])) for k, v in out.items()}


async def enrich_source_ids(db: AsyncSession, rows: Iterable[Any] | None) -> None:
    """Fill ``source_ids`` and ``id_space`` on the *rows* with an ``asset_id`` stored without them (#2448).

    A validation written before #2448 kept the source names only: its module ids
    are read from the inventory at read time, so no stored validation is migrated.
    Never fails a read: a store that cannot be read leaves the rows as they are.
    """
    targets = [r for r in rows or () if isinstance(r, dict) and r.get("asset_id")]
    for row in targets:
        row.setdefault("id_space", ID_SPACE)
    missing: list[uuid.UUID] = []
    for row in targets:
        if "source_ids" in row:
            continue
        try:
            missing.append(uuid.UUID(str(row["asset_id"])))
        except ValueError:
            row["source_ids"] = []
    if not missing:
        return
    try:
        async with db.begin_nested():
            ids = await _source_ids(db, list(dict.fromkeys(missing)))
    except Exception:  # a read is served whatever the inventory answers
        logger.info("CTEM case asset source ids unavailable", exc_info=True)
        return
    for row in targets:
        if "source_ids" not in row:
            row["source_ids"] = ids.get(str(row["asset_id"]), [])


def attach_run_order(
    entry: dict[str, Any], order: list[str], resolved: Mapping[str, Mapping[str, Any]], *, actor: str, at: str
) -> None:
    """Write on the validation *entry* the assets its *order* resolves to and the names it does not (pure)."""
    assets: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for name in order:
        hit = resolved.get(_key(name)[:300])
        if hit is None:
            unresolved.append(name)
            continue
        if any(a["asset_id"] == hit["asset_id"] for a in assets):
            continue
        assets.append(
            {
                "asset_id": hit["asset_id"],
                "name": hit["name"],
                "id_space": ID_SPACE,
                "sources": list(hit.get("sources") or []),
                "source_ids": [dict(p) for p in hit.get("source_ids") or ()],
                "provenance": PROVENANCE_INFERRED,
                "matched": name,
                "attached_by": actor,
                "at": at,
            }
        )
    if assets:
        entry["assets"] = assets
    if unresolved:
        entry["unresolved"] = unresolved


def attached_assets(validations: Iterable[Any] | None) -> list[dict[str, Any]]:
    """The assets a case's *validations* attached, newest first, one per asset (pure)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in reversed(list(validations or [])):
        if not isinstance(entry, dict):
            continue
        for asset in entry.get("assets") or ():
            if not isinstance(asset, dict) or not asset.get("asset_id") or str(asset["asset_id"]) in seen:
                continue
            seen.add(str(asset["asset_id"]))
            out.append(dict(asset))
    return out[:MAX_ATTACHED]


def merged_assets(issues: Iterable[Mapping[str, Any]], attached: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The case's assets: those its visible issues name, then the attached ones they do not (pure)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for issue in issues:
        if issue.get("restricted"):
            continue
        provenance = PROVENANCE_INVENTORY if issue.get("assets_from") == PROVENANCE_INVENTORY else PROVENANCE_ISSUE
        for name in issue.get("assets") or ():
            if _key(name) and _key(name) not in seen:
                seen.add(_key(name))
                out.append({"name": str(name).strip(), "provenance": provenance})
    for asset in attached:
        if _key(asset.get("name")) not in seen:
            seen.add(_key(asset.get("name")))
            out.append(dict(asset))
    return out


def with_attached(answer: dict[str, Any], attached: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The case page's affected assets with the attached ones the issues do not name (pure)."""
    rows = answer.setdefault("assets", [])
    for row in rows:
        row.setdefault("provenance", PROVENANCE_ISSUE)
    names = {_key(r.get("name")) for r in rows}
    added = 0
    for asset in attached:
        if _key(asset.get("name")) in names:
            continue
        names.add(_key(asset.get("name")))
        rows.append(
            {
                "name": asset.get("name"),
                "asset_id": asset.get("asset_id"),
                "id_space": ID_SPACE,
                "sources": list(asset.get("sources") or []),
                **({"source_ids": [dict(p) for p in asset["source_ids"]]} if "source_ids" in asset else {}),
                "provenance": asset.get("provenance") or PROVENANCE_INFERRED,
                "issue_count": 0,
                "open_issue_count": 0,
                "kev": False,
                "max_cvss": None,
            }
        )
        added += 1
    answer["asset_count"] = int(answer.get("asset_count") or 0) + added
    return answer


def card_line(attached: Iterable[Mapping[str, Any]]) -> str | None:
    """``Assets: web-1 (inferred from a name match), ...``, ``None`` when nothing is attached (pure)."""
    shown = list(attached)
    if not shown:
        return None
    parts = [
        f"{a.get('name')} ({_WORDS.get(str(a.get('provenance')), str(a.get('provenance')))})"
        for a in shown[:_MAX_LINE_ASSETS]
    ]
    more = len(shown) - _MAX_LINE_ASSETS
    tail = f", and {more} more" if more > 0 else ""
    return f"{LINE_LEAD} {', '.join(parts)}{tail}."


def has_line(ask: Mapping[str, Any] | None) -> bool:
    """Whether an ask already states the assets line (its context names it)."""
    return LINE_LEAD.casefold() in str((ask or {}).get("context") or "").casefold()


async def complete_gate_ask(
    db: AsyncSession, correlation_key: object, ask: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """*ask* with the ``Assets:`` line first in its context when its case has attached assets.

    Only the remediation and mobilize gates of a case (``ctem_version_match.GATE_KEY_PREFIXES``).
    Never fails a work item write: a case that cannot be read leaves the ask as given.
    """
    if not ask or has_line(ask):
        return ask
    from app.services.ctem_version_match import case_id_of

    case_id = case_id_of(correlation_key)
    if case_id is None:
        return ask
    from app.models.ctem_case import CtemCase

    try:
        case = await db.get(CtemCase, case_id)
    except Exception:  # the card is written whatever the case read answers
        logger.info("CTEM case assets unavailable for %s", case_id, exc_info=True)
        return ask
    line = card_line(attached_assets(getattr(case, "validations", None))) if case is not None else None
    if line is None:
        return ask
    from app.services.decision_ask import fit

    out = dict(ask)
    rest = str(out.get("context") or "").strip()
    out["context"] = fit(f"{line}\n{rest}" if rest else line, _MAX_CONTEXT_CHARS)
    return out
