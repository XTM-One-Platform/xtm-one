# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases as one viewer sees them: the source products are the only judge (#11613).

Cases are shared by the organization and store no source content
(``models/ctem_case.py``). What a viewer sees is decided here, at read time,
from the viewer's OWN read of OpenCTI and OpenAEV (their own JWT, the same
reads recompute makes, ``ctem_cases_source.read_view``). XTM One mirrors no
group, role, marking, organization or scope: whatever the source returns to
that viewer is what they see.

- :func:`viewer_view` is the read, cached per user and vertical in Redis for
  :data:`VIEW_TTL_SECONDS`; the member ids it found are also kept, ids only,
  for :data:`LAST_SEEN_TTL_SECONDS`, so a source that is unreachable later
  still answers which cases the viewer may see (shown with their details
  marked unavailable, never empty and never someone else's).
- :func:`hydrate` renders one case for one viewer: ``None`` when none of its
  member issues is visible to them (absent from lists and counts, 404 on
  read); otherwise every visible issue in full, every other member as a
  ``{"restricted": true}`` row (the OpenCTI "Restricted" pattern), the
  priority, scores, assets and crown jewels computed on the visible issues
  only, the member count total with the restricted count, and the history
  with "Restricted" in place of an issue the viewer cannot see.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue
from app.models.ctem_signal import CtemSignal
from app.models.user import User
from app.services import ctem_cases as rules

logger = logging.getLogger(__name__)

#: How long one viewer's source read answers before it is read again.
VIEW_TTL_SECONDS = 300
#: How long the ids a viewer was shown decide visibility while a source is unreachable.
LAST_SEEN_TTL_SECONDS = 7 * 24 * 3600
#: The label of an issue the viewer cannot see, as OpenCTI shows it.
RESTRICTED = "Restricted"


@dataclass
class ViewerView:
    """What one viewer's own source read returned.

    ``issues`` are the issues they can read (open or closed) with their
    details; ``open_ids`` the open ones among them; ``unavailable`` the
    member ids they were shown before from a product that did not answer now;
    ``missing`` those products; ``complete`` false when a read hit its cap
    (recompute then resolves nothing).
    """

    issues: dict[str, rules.SourceIssue] = field(default_factory=dict)
    open_ids: set[str] = field(default_factory=set)
    unavailable: set[str] = field(default_factory=set)
    missing: list[str] = field(default_factory=list)
    complete: bool = True
    no_access: bool = False
    #: The signal members (``signal:<id>``) this viewer may read, read fresh on
    #: every call and never cached: the signals API judges them, not a product.
    signals: set[str] = field(default_factory=set)

    def sees(self, member_id: str) -> bool:
        return member_id in self.issues or member_id in self.unavailable or member_id in self.signals

    def to_json(self) -> str:
        return json.dumps(
            {
                "issues": [_issue_json(i) for i in self.issues.values()],
                "open_ids": sorted(self.open_ids),
                "unavailable": sorted(self.unavailable),
                "missing": self.missing,
                "complete": self.complete,
                "no_access": self.no_access,
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> ViewerView:
        data = json.loads(raw)
        issues = [_issue_from_json(i) for i in data.get("issues") or []]
        return cls(
            issues={i.member_id: i for i in issues},
            open_ids=set(data.get("open_ids") or []),
            unavailable=set(data.get("unavailable") or []),
            missing=list(data.get("missing") or []),
            complete=bool(data.get("complete", True)),
            no_access=bool(data.get("no_access", False)),
        )


def _issue_json(issue: rules.SourceIssue) -> dict[str, Any]:
    out = asdict(issue)
    out["assets"] = list(issue.assets)
    out["crown_jewels"] = list(issue.crown_jewels)
    out["first_seen_at"] = issue.first_seen_at.isoformat() if issue.first_seen_at else None
    return out


def _issue_from_json(data: dict[str, Any]) -> rules.SourceIssue:
    data = dict(data)
    data["assets"] = tuple(data.get("assets") or ())
    data["crown_jewels"] = tuple(data.get("crown_jewels") or ())
    seen = data.get("first_seen_at")
    data["first_seen_at"] = datetime.fromisoformat(seen) if seen else None
    return rules.SourceIssue(**data)


def _view_key(user_id: str, vertical: str) -> str:
    return f"ctem:cases:view:{user_id}:{vertical}"


def _seen_key(user_id: str, vertical: str) -> str:
    return f"ctem:cases:seen:{user_id}:{vertical}"


async def _redis() -> Any | None:
    try:
        from app.core.redis import redis_client

        return await redis_client()
    except Exception:  # no Redis: every read goes to the source
        return None


SIGNAL_SOURCE = "signal"


async def signal_members(db: Any, user_id: str) -> set[str]:
    """The ``signal:<id>`` members *user_id* may read: every case-bound signal for an
    administrator, else the signals recorded for them (the rule of ``GET /ctem-signals``).

    A case opened from a signal with no product issue (a path without a CVE) is
    then visible to whoever sees the signal that opened it, never to nobody.
    """
    try:
        user = await db.get(User, uuid.UUID(str(user_id)))
    except (ValueError, TypeError):
        user = None
    query = select(CtemSignal.id).where(CtemSignal.case_id.is_not(None))
    if not getattr(user, "is_admin", False):
        query = query.where(CtemSignal.recorded_by == str(user_id))
    try:
        ids = (await db.execute(query)).scalars()
    except Exception:
        logger.info("CTEM cases: signal members unreadable for %s", user_id, exc_info=True)
        return set()
    return {f"{SIGNAL_SOURCE}:{i}" for i in ids}


async def viewer_view(db: Any, user_id: str, vertical: str, *, fresh: bool = False) -> ViewerView:
    """The viewer's own view of the source, from the per-user cache unless *fresh*.

    The source read is ``ctem_cases_source.read_view``; it is given the member
    ids of the shared cases so the closed members the viewer can still read
    are found by id. The signal members are read fresh on every call.
    """
    view = await _product_view(db, user_id, vertical, fresh=fresh)
    return replace(view, signals=await signal_members(db, user_id))


async def _product_view(db: Any, user_id: str, vertical: str, *, fresh: bool) -> ViewerView:
    from app.services import ctem_cases_source as source

    client = await _redis()
    if client is not None and not fresh:
        try:
            cached = await client.get(_view_key(user_id, vertical))
            if cached:
                return ViewerView.from_json(cached)
        except Exception:
            logger.info("CTEM cases: view cache unreadable for %s", user_id, exc_info=True)
    view = await source.read_view(db, user_id, vertical)
    if view.missing and client is not None:
        # Ids the viewer was shown from a product that does not answer now:
        # still theirs to see, with the details marked unavailable.
        try:
            seen = await client.get(_seen_key(user_id, vertical))
            for member_id in json.loads(seen) if seen else []:
                if member_id.split(":", 1)[0] in view.missing and member_id not in view.issues:
                    view.unavailable.add(member_id)
        except Exception:
            logger.info("CTEM cases: last-seen ids unreadable for %s", user_id, exc_info=True)
    if client is not None:
        try:
            await client.set(_view_key(user_id, vertical), view.to_json(), ex=VIEW_TTL_SECONDS)
            visible = sorted(set(view.issues) | view.unavailable)
            await client.set(_seen_key(user_id, vertical), json.dumps(visible), ex=LAST_SEEN_TTL_SECONDS)
        except Exception:
            logger.info("CTEM cases: view cache not written for %s", user_id, exc_info=True)
    return view


async def forget_view(user_id: str, vertical: str) -> None:
    """Drop the cached view (after a recompute, so the next read is the new one)."""
    client = await _redis()
    if client is None:
        return
    try:
        await client.delete(_view_key(user_id, vertical))
    except Exception:
        logger.info("CTEM cases: view cache not dropped for %s", user_id, exc_info=True)


def _member_id(row: CtemCaseIssue) -> str:
    return f"{row.source}:{row.external_id}"


def _visible_issue(row: CtemCaseIssue, issue: rules.SourceIssue | None) -> dict[str, Any]:
    if row.source == SIGNAL_SOURCE:  # the signal that opened or joined the case: its page holds the details
        return {"source": row.source, "external_id": row.external_id, "open": row.open}
    if issue is None:  # seen before, its product does not answer now
        return {"source": row.source, "external_id": row.external_id, "open": row.open, "details_unavailable": True}
    return {
        "source": row.source,
        "external_id": row.external_id,
        "name": issue.name,
        "cve": issue.cve,
        "cvss": issue.cvss,
        "epss": issue.epss,
        "kev": issue.kev,
        "assets": list(issue.assets),
        "crown_jewels": list(issue.crown_jewels),
        "first_seen_at": issue.first_seen_at.isoformat() if issue.first_seen_at else None,
        "open": row.open,
    }


def visible_members(rows: list[CtemCaseIssue], view: ViewerView) -> list[CtemCaseIssue]:
    return [r for r in rows if view.sees(_member_id(r))]


def summary(case: CtemCase, rows: list[CtemCaseIssue], view: ViewerView) -> dict[str, Any] | None:
    """The list row of *case* for this viewer, ``None`` when they see none of its issues."""
    visible = visible_members(rows, view)
    if not visible:
        return None
    details = [view.issues[_member_id(r)] for r in visible if r.open and _member_id(r) in view.issues]
    agg = rules.aggregates(details)
    unavailable = sorted({r.source for r in visible if r.source != SIGNAL_SOURCE and _member_id(r) not in view.issues})
    return {
        "id": str(case.id),
        "title": case.title,
        "remediation_kind": case.remediation_kind,
        "product_label": case.product_label,
        "fixed_version": case.fixed_version,
        "state": case.state,
        "state_since": case.state_since.isoformat() if case.state_since else None,
        "owner_user_id": str(case.owner_user_id) if case.owner_user_id else None,
        "due_at": case.due_at.isoformat() if case.due_at else None,
        "accepted_reason": case.accepted_reason,
        "accepted_until": case.accepted_until.isoformat() if case.accepted_until else None,
        # Computed on what this viewer sees: another viewer may get another figure.
        "priority": rules.default_priority(details) if details else "low",
        "priority_basis": "default policy, on the issues you can see",
        "issue_count": len(rows),
        "open_issue_count": sum(1 for r in rows if r.open),
        "restricted_count": len(rows) - len(visible),
        "asset_count": agg["asset_count"],
        "crown_jewel_count": agg["crown_jewel_count"],
        "kev_count": agg["kev_count"],
        "max_cvss": agg["max_cvss"],
        "max_epss": agg["max_epss"],
        "first_seen_at": agg["first_seen_at"].isoformat() if agg["first_seen_at"] else None,
        "details_unavailable": unavailable,
        # The loop (#857): ids, enums, dates and references only, the same for every viewer.
        "trigger_path": case.trigger_path,
        "severity": case.severity,
        "sla_due_at": case.sla_due_at.isoformat() if case.sla_due_at else None,
        "sla_breached_at": case.sla_breached_at.isoformat() if case.sla_breached_at else None,
        "risk_scenario_ids": list(case.risk_scenario_ids or []),
        "tickets": list(case.tickets or []),
        "validations": list(case.validations or []),
        "validation_plan": bool(case.validation_plan),
        "resolution_kind": case.resolution_kind,
    }


def _event(row: CtemCaseEvent, view: ViewerView) -> dict[str, Any]:
    issue = row.issue
    if issue is not None and not view.sees(issue):
        issue = RESTRICTED
    return {
        "at": row.at.isoformat() if row.at else None,
        "actor": row.actor,
        "kind": row.kind,
        "issue": issue,
        "from": row.from_value,
        "to": row.to_value,
        "reason": row.reason,
        "evidence": row.evidence,
    }


def _rank(row: CtemCaseIssue, view: ViewerView) -> tuple:
    issue = view.issues.get(_member_id(row))
    if issue is None:
        return (1, not row.open, 0.0, 0.0)
    return (0, not row.open, -(issue.cvss or 0.0), -(issue.epss or 0.0))


def hydrate(
    case: CtemCase, rows: list[CtemCaseIssue], events: list[CtemCaseEvent], view: ViewerView
) -> dict[str, Any] | None:
    """One case for this viewer, ``None`` when they see none of its issues.

    A member they cannot see is ``{"restricted": true}`` and nothing else: no
    id, source, name, score, asset or link.
    """
    head = summary(case, rows, view)
    if head is None:
        return None
    visible = [r for r in sorted(rows, key=lambda r: _rank(r, view)) if view.sees(_member_id(r))]
    restricted = len(rows) - len(visible)
    return {
        **head,
        "keys": list(case.keys or []),
        "issues": [_visible_issue(r, view.issues.get(_member_id(r))) for r in visible]
        + [{"restricted": True} for _ in range(restricted)],
        "history": [_event(e, view) for e in events],
    }


_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def list_for_viewer(
    cases: list[tuple[CtemCase, list[CtemCaseIssue]]],
    view: ViewerView,
    *,
    state: str | None = None,
    owner_user_id: str | None = None,
    priority: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """One page of the cases this viewer sees and their counts by state.

    A case with no issue visible to the viewer is in neither: a count is data.
    """
    rows = [s for s in (summary(c, members, view) for c, members in cases) if s is not None]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    if state:
        rows = [r for r in rows if r["state"] == state]
    if owner_user_id:
        rows = [r for r in rows if r["owner_user_id"] == owner_user_id]
    if priority:
        rows = [r for r in rows if r["priority"] == priority]
    rows.sort(key=lambda r: (_PRIORITY_RANK.get(r["priority"], 9), -r["open_issue_count"], r["id"]))
    start = max(0, offset)
    return rows[start : start + max(1, limit)], counts


def owner_sees(rows: list[CtemCaseIssue], owner_view: ViewerView) -> bool:
    """Whether the would-be owner sees at least one issue of the case in the source products."""
    return bool(visible_members(rows, owner_view))
