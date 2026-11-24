# SPDX-License-Identifier: Apache-2.0

"""The sources of the organization profile and what each one wrote (#1276).

One answer per source of rows, for the one Sources and syncs table of Settings >
Organization and CTEM > Inventory:

- a source is the ``source`` of a profile row: a platform a sync reads
  (``opencti``, ``openaev``, ``opencrq``), a system an agent or a partner writes
  through the row tools or the API (``intune``, ``ciso-office``...), or a hand
  source (``manual``, ``csv``, ``api``);
- each source names its kind, a display name (never a raw slug when a better
  one is known), the agent that wrote its rows when the saved versions say so,
  its rows per profile field and the syncs that read it;
- the purge plan removes only the rows a source (or one sync) wrote: a row a
  person wrote or another source feeds is never touched, and the plan names the
  rows of other sources that carry the same name so the dialog lists them;
- the release plan keeps the rows and hands them to people (source ``manual``,
  the sync stamp cleared), the provenance kept in the saved version's basis.

Pure planning, no I/O, but for :func:`writer_agents` and :func:`sync_history`,
which read the saved versions and the audit trail.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.organization_context import AGENT_SOURCE_PREFIX, PLATFORM_SOURCES, OrganizationProfile

#: The profile lists that carry a row source, with the section key the syncs and the UI use.
SOURCED_FIELDS: dict[str, str] = {
    "crown_jewels": "crown_jewels",
    "software": "software",
    "assets_exposure": "assets",
    "asset_groups": "asset_groups",
}
#: Where each field is configured (the scopes of ``organization_context_sync.SECTION_SCOPES``).
FIELD_SCOPES: dict[str, str] = {
    "crown_jewels": "inventory",
    "software": "inventory",
    "assets_exposure": "inventory",
    "asset_groups": "inventory",
}
#: Display names of the systems known by their source key; any other key is shown as written.
DISPLAY_NAMES: dict[str, str] = {
    "opencti": "OpenCTI",
    "openaev": "OpenAEV",
    "opencrq": "OpenCRQ",
    "intune": "Microsoft Intune",
    "defender": "Microsoft Defender for Endpoint",
    "github": "GitHub",
    "gitlab": "GitLab",
    "crowdstrike": "CrowdStrike",
    "tanium": "Tanium",
    "sentinelone": "SentinelOne",
    "servicenow": "ServiceNow",
}
#: The kinds of source, in the order the table lists them.
KINDS: tuple[str, ...] = ("platform", "agent", "csv", "api", "manual")
#: How many saved versions :func:`writer_agents` reads back.
VERSIONS_READ = 300
#: What the release of a row leaves on it: a person owns it from then on.
RELEASED_SOURCE = "manual"
#: Names listed per outcome in a plan.
LISTED = 50

_ROWS_BASIS = re.compile(r"^rows of (?P<source>.+?) by agent (?P<agent>.+?):")


class SourceRefusedError(ValueError):
    """A purge the API refuses (a hand source, an unknown source)."""


def source_key(source: str | None) -> str:
    """The key a row counts under: its source lower-cased, ``manual`` for an empty one."""
    value = (source or "").strip().lower()
    return value or "manual"


def kind_of(key: str) -> str:
    """``platform`` (a sync reads it), ``agent`` (an agent or a partner writes it), or the hand kind."""
    if key in ("manual", "csv", "api"):
        return key
    if key in PLATFORM_SOURCES:
        return "platform"
    return "agent"


def display_name(key: str) -> str:
    """The name a person reads for *key*: the system's name when known, else the key as written."""
    if key.startswith(AGENT_SOURCE_PREFIX):
        return key[len(AGENT_SOURCE_PREFIX) :] or key
    return DISPLAY_NAMES.get(key, key)


@dataclass
class SourceSummary:
    key: str
    kind: str
    display_name: str
    #: Profile field -> rows of this source.
    rows: dict[str, int] = field(default_factory=dict)
    #: The agent that wrote this source's rows, by name, when a saved version says so.
    written_by_agent: str = ""
    syncs: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.rows.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "display_name": self.display_name,
            "rows": dict(self.rows),
            "total": self.total,
            "written_by_agent": self.written_by_agent,
            "sync_ids": list(self.syncs),
        }


def summarize(
    profile: OrganizationProfile,
    syncs: list[Any],
    *,
    scope: str = "",
    writers: dict[str, str] | None = None,
    connected: tuple[str, ...] = (),
) -> list[SourceSummary]:
    """Every source of the profile fields of *scope* with its rows, its writer and its syncs.

    A platform with a sync or connected is listed even without a row (the table offers to sync it);
    platforms first, then agents and partners, then CSV, API and hand rows.
    """
    writers = writers or {}
    out: dict[str, SourceSummary] = {}

    def entry(key: str) -> SourceSummary:
        if key not in out:
            out[key] = SourceSummary(
                key=key, kind=kind_of(key), display_name=display_name(key), written_by_agent=writers.get(key, "")
            )
        return out[key]

    for attr in SOURCED_FIELDS:
        if scope and FIELD_SCOPES[attr] != scope:
            continue
        for row in getattr(profile, attr, None) or []:
            summary = entry(source_key(getattr(row, "source", "")))
            summary.rows[attr] = summary.rows.get(attr, 0) + 1
    for sync in syncs:
        if not sync.source:
            continue
        entry(source_key(sync.source)).syncs.append(str(sync.id))
    for platform in connected:
        entry(source_key(platform))
    order = {kind: index for index, kind in enumerate(KINDS)}
    return sorted(out.values(), key=lambda s: (order.get(s.kind, len(KINDS)), s.display_name.lower()))


async def writer_agents(db: AsyncSession, *, limit: int = VERSIONS_READ) -> dict[str, str]:
    """Source key -> the agent that last wrote its rows, read from the recent saved versions' basis."""
    from app.models.organization_context import OrganizationContextVersion

    found: dict[str, str] = {}
    rows = (
        await db.execute(
            select(OrganizationContextVersion.sources)
            .where(OrganizationContextVersion.sources.is_not(None))
            .order_by(OrganizationContextVersion.created_at.desc())
            .limit(limit)
        )
    ).scalars()
    for sources in rows:
        for entry in (sources or {}).values():
            match = _ROWS_BASIS.match(str((entry or {}).get("basis", "")))
            if match:
                found.setdefault(source_key(match["source"]), match["agent"].strip())
    return found


@dataclass
class PurgePlan:
    """What purging (or releasing) one source's rows does: per field, the rows touched and those kept."""

    source: str
    mode: str
    #: Profile field -> names of the rows removed (purge) or handed to people (release).
    touched: dict[str, list[str]] = field(default_factory=dict)
    #: Profile field -> names of rows of OTHER sources with the same name, kept as they are.
    kept_elsewhere: dict[str, list[str]] = field(default_factory=dict)
    profile: OrganizationProfile | None = None

    @property
    def count(self) -> int:
        return sum(len(v) for v in self.touched.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "mode": self.mode,
            "count": self.count,
            "counts": {attr: len(names) for attr, names in self.touched.items()},
            "rows": {attr: names[:LISTED] for attr, names in self.touched.items()},
            "kept_elsewhere": {attr: names[:LISTED] for attr, names in self.kept_elsewhere.items() if names},
        }


def _row_name(row: Any) -> str:
    return str(getattr(row, "name", "") or getattr(row, "product", "") or "")


def sync_fields(section: str) -> tuple[str, ...]:
    """The profile fields a sync of *section* writes: its one row field, none for ``pirs`` or ``all``.

    A sync's purge or release is limited to them, so deleting the ``assets`` sync of a platform never
    touches that platform's software or asset groups (review on #1278).
    """
    return tuple(attr for attr, key in SOURCED_FIELDS.items() if key == section)


def plan_rows(
    profile: OrganizationProfile,
    *,
    source: str,
    mode: str,
    sync_id: str = "",
    fields: tuple[str, ...] | None = None,
) -> PurgePlan:
    """Purge or release the rows *source* wrote (only those *sync_id* wrote when given).

    *fields* limits the plan to those profile fields (``None``: every sourced field; empty: none).

    A row a sync wrote carries its ``sync_id``; rows written before the stamp count as the sync's when
    their source is the sync's. Hand sources are refused for a purge: a person's rows are never purged.
    """
    if mode not in ("purge", "release"):
        raise SourceRefusedError("mode must be purge or release")
    key = source_key(source)
    if mode == "purge" and key == "manual":
        raise SourceRefusedError("rows entered by hand are never purged; remove them in their table")
    plan = PurgePlan(source=key, mode=mode)
    draft = profile.model_copy(deep=True)
    for attr in tuple(SOURCED_FIELDS) if fields is None else fields:
        rows = list(getattr(draft, attr, None) or [])

        def owned(row: Any) -> bool:
            if source_key(getattr(row, "source", "")) != key:
                return False
            stamp = getattr(row, "sync_id", "") or ""
            return not sync_id or not stamp or stamp == sync_id

        touched = [r for r in rows if owned(r)]
        if not touched:
            continue
        names = {_row_name(r).casefold() for r in touched}
        plan.touched[attr] = [_row_name(r) for r in touched]
        plan.kept_elsewhere[attr] = sorted(
            {_row_name(r) for r in rows if not owned(r) and _row_name(r).casefold() in names}
        )
        if mode == "purge":
            setattr(draft, attr, [r for r in rows if not owned(r)])
        else:
            for row in touched:
                row.source = RELEASED_SOURCE
                row.sync_id = ""
    plan.profile = draft
    return plan


def plan_basis(plan: PurgePlan, *, who: str, sync_label: str = "") -> str:
    """The provenance the saved version keeps, so a released row still says where it came from."""
    origin = sync_label or display_name(plan.source)
    verb = "purged" if plan.mode == "purge" else "released to people (formerly synced from"
    tail = ")" if plan.mode == "release" else ""
    counts = ", ".join(f"{len(v)} {attr}" for attr, v in plan.touched.items())
    if plan.mode == "purge":
        return f"rows of {origin} {verb} by {who}: {counts}"[:500]
    return f"rows {verb} {origin}{tail} by {who}: {counts}"[:500]


async def sync_history(db: AsyncSession, sync_id: uuid.UUID, *, limit: int = 20) -> list[dict[str, Any]]:
    """The last *limit* audited events of a sync (runs that changed or failed, saves), newest first."""
    from app.models.audit_log import AuditLog

    rows = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.entity_type == "organization_context_sync", AuditLog.entity_id == sync_id)
            .order_by(AuditLog.created_at.desc())
            .limit(max(1, min(limit, 100)))
        )
    ).scalars()
    out: list[dict[str, Any]] = []
    for row in rows:
        details = row.details if isinstance(row.details, dict) else {}
        out.append(
            {
                "at": row.created_at,
                "action": row.action,
                "by": row.user_email,
                "summary": row.summary,
                "trigger": details.get("trigger", ""),
                "added": details.get("added"),
                "updated": details.get("updated"),
                "removed": details.get("removed"),
                "version": details.get("version"),
                "error": details.get("error"),
            }
        )
    return out
