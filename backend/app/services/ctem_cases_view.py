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
import re
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
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
    #: True when this viewer's own OpenCRQ read is available (#936): only then
    #: are the OpenCRQ asset names and scenario ids of a case's exploitation shown.
    opencrq: bool = False
    #: True when this viewer's own OpenCTI read is available (CTEM-16-UI, #1115): only
    #: then are the name, ids and link of a case's threat package shown.
    opencti: bool = False

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
                "opencrq": self.opencrq,
                "opencti": self.opencti,
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
            opencrq=bool(data.get("opencrq", False)),
            opencti=bool(data.get("opencti", False)),
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


def _view_key(user_id: str, vertical: str, generation: str = "0") -> str:
    return f"ctem:cases:view:{user_id}:{vertical}:{generation}"


def _generation_key(vertical: str) -> str:
    return f"ctem:cases:view-generation:{vertical}"


async def _generation(client: Any, vertical: str) -> str:
    """The current generation of every cached view of *vertical* (``0`` before the first bump)."""
    try:
        raw = await client.get(_generation_key(vertical))
    except Exception:
        logger.info("CTEM cases: view generation unreadable for %s", vertical, exc_info=True)
        return "0"
    if isinstance(raw, bytes):
        raw = raw.decode()
    return str(raw) if raw else "0"


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
    seen = set(view.issues) | set(view.unavailable)
    return replace(view, signals=await signal_members(db, user_id) | await source_signal_members(db, seen))


async def source_signal_members(db: Any, seen: set[str]) -> set[str]:
    """The ``signal:<id>`` members a source reader recorded for issues this viewer sees (#1529).

    A signal of an OpenCTI or OpenAEV issue is recorded by the system; it is
    as visible as its issue (``evidence.source_issue``), never more.
    """
    if not seen:
        return set()
    from app.services.ctem_source_signals import SOURCE_PATHS

    query = select(CtemSignal.id, CtemSignal.evidence).where(
        CtemSignal.case_id.is_not(None), CtemSignal.source_provider.in_(tuple(SOURCE_PATHS))
    )
    try:
        rows = (await db.execute(query)).all()
    except Exception:
        logger.info("CTEM cases: source signal members unreadable", exc_info=True)
        return set()
    return {
        f"{SIGNAL_SOURCE}:{signal_id}"
        for signal_id, evidence in rows
        if isinstance(evidence, dict) and evidence.get("source_issue") in seen
    }


async def _product_view(db: Any, user_id: str, vertical: str, *, fresh: bool) -> ViewerView:
    from app.services import ctem_cases_source as source

    client = await _redis()
    generation = await _generation(client, vertical) if client is not None else "0"
    if client is not None and not fresh:
        try:
            cached = await client.get(_view_key(user_id, vertical, generation))
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
            await client.set(_view_key(user_id, vertical, generation), view.to_json(), ex=VIEW_TTL_SECONDS)
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
        await client.delete(_view_key(user_id, vertical, await _generation(client, vertical)))
    except Exception:
        logger.info("CTEM cases: view cache not dropped for %s", user_id, exc_info=True)


async def forget_all_views(vertical: str) -> None:
    """Drop every viewer's cached view of *vertical* (a case opened, a signal joined one).

    The cases changed for everyone, not only for the caller, so the views are
    retired by bumping their generation: the next read of each viewer misses
    and reads its own source again; the old entries expire with their TTL.
    """
    client = await _redis()
    if client is None:
        return
    try:
        await client.incr(_generation_key(vertical))
    except Exception:
        logger.info("CTEM cases: view generation not bumped for %s", vertical, exc_info=True)


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
        # The version knowledge of this viewer's own OpenCTI read (CTEM-11, #946; shown by #928).
        "installed_version": issue.installed_version,
        "fixed_version": issue.fixed_version,
        "fixed_by": issue.fixed_by,
        "course_of_action_name": issue.course_of_action_name if issue.fixed_version and not issue.fixed_by else None,
        "not_affected": bool(issue.not_affected),
        "declared_version_known": issue.declared_version_known,
        # The OpenAEV finding lifecycle (CTEM-14-UI): absent when the source carries none.
        **(
            {
                "retest_status": issue.retest_status,
                "last_tested_at": issue.last_tested_at,
                "retest_pending": issue.retest_pending,
            }
            if issue.retest_status or issue.retest_pending
            else {}
        ),
    }


def visible_members(rows: list[CtemCaseIssue], view: ViewerView) -> list[CtemCaseIssue]:
    return [r for r in rows if view.sees(_member_id(r))]


SEVERITY_PRIORITY: dict[str, str] = {"P1": "critical", "P2": "high", "P3": "medium"}


def case_priority(severity: str | None, details: list[Any]) -> str:
    """The priority a case shows: its loop severity when set, else the default policy."""
    if severity in SEVERITY_PRIORITY:
        return SEVERITY_PRIORITY[severity]
    return rules.default_priority(details) if details else "low"


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
        # One case, one urgency (UX round 1 on #897, B3): a loop severity sets the
        # priority; otherwise computed on what this viewer sees.
        "priority": case_priority(case.severity, details),
        "priority_basis": (
            "loop severity" if case.severity in SEVERITY_PRIORITY else "default policy, on the issues you can see"
        ),
        "issue_count": len(rows),
        "open_issue_count": sum(1 for r in rows if r.open),
        "restricted_count": len(rows) - len(visible),
        "asset_count": agg["asset_count"],
        "crown_jewel_count": agg["crown_jewel_count"],
        "kev_count": agg["kev_count"],
        "max_cvss": agg["max_cvss"],
        "max_epss": agg["max_epss"],
        "first_seen_at": agg["first_seen_at"].isoformat() if agg["first_seen_at"] else None,
        # When the case was opened and last changed (#1176): the list's Created, Age and Last change columns.
        "created_at": case.created_at.isoformat() if case.created_at else None,
        "updated_at": case.updated_at.isoformat() if case.updated_at else None,
        # The signals that opened or joined the case, counted on every member, seen or not.
        "signal_count": sum(1 for r in rows if r.source == SIGNAL_SOURCE),
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
    # A treatment plan's name and OpenCRQ link go to a viewer who reads OpenCRQ only (#1068).
    # A threat package's Grouping and coverage go to a viewer who reads OpenCTI only (#1115).
    hidden = (row.kind == "treatment_plan" and not view.opencrq) or (row.kind == "threat_package" and not view.opencti)
    return {
        "at": row.at.isoformat() if row.at else None,
        "actor": row.actor,
        "kind": row.kind,
        "issue": issue,
        "from": None if hidden else row.from_value,
        "to": None if hidden else row.to_value,
        "reason": None if hidden else row.reason,
        "evidence": None if hidden else row.evidence,
    }


def treatment_plan_of(events: list[CtemCaseEvent]) -> dict[str, Any] | None:
    """The treatment plan attached to the case: its newest ``treatment_plan`` event (#1068), whatever the order."""
    plans = [row for row in events if row.kind == "treatment_plan" and row.at is not None]
    newest = max(plans, key=lambda row: row.at) if plans else None
    if newest is None or not newest.to_value:
        return None
    return {
        "id": newest.to_value,
        "name": newest.reason,
        "url": newest.evidence,
        "at": newest.at.isoformat() if newest.at else None,
    }


def treatment_plan_for(events: list[CtemCaseEvent], view: ViewerView) -> dict[str, Any] | None:
    """The case's treatment plan as this viewer may read it (#1068).

    Whether a plan is attached, and when, goes to every viewer of the case; the
    plan's id, name and OpenCRQ link only to a viewer whose own OpenCRQ read is
    available (as the exploitation reading, #936), else ``restricted``.
    """
    plan = treatment_plan_of(events)
    if plan is None:
        return None
    if not view.opencrq:
        return {"restricted": True, "at": plan["at"]}
    return {**plan, "restricted": False}


def threat_package_of(events: list[CtemCaseEvent]) -> dict[str, Any] | None:
    """The threat package attached to the case: its newest ``threat_package`` event (#1115), whatever the order."""
    packages = [row for row in events if row.kind == "threat_package" and row.at is not None]
    newest = max(packages, key=lambda row: row.at) if packages else None
    if newest is None or not newest.to_value:
        return None
    return {
        "grouping_id": newest.to_value,
        "name": newest.reason,
        "url": newest.evidence,
        "coverage_id": newest.from_value,
        "at": newest.at.isoformat() if newest.at else None,
    }


#: An OpenCTI draft page: the package still sits in a draft (#1453, #1480); the same
#: pattern ``ctem_case_loop.record_threat_package`` accepts.
_DRAFT_PAGE = re.compile(
    r"^https?://[^\s/?#]+(?:/[A-Za-z0-9._-]+)*/data/import/draft/[0-9a-fA-F-]{36}(?:/[A-Za-z0-9._-]*)?$"
)


def threat_package_state(package: dict[str, Any]) -> str:
    """``validated`` only when the package's page is its live Grouping page, else ``drafted`` (#1480, #1547).

    A drafted package and its coverage are not in OpenCTI's live graph: the
    coverage has not reached OpenAEV. The Grouping page is written on the case
    only once the Grouping resolves in the live graph: ``update_ctem_case``
    checks it at record time (``ctem_threat_package_live.verify_package``) and
    the packages stored before that check are repaired by the administrator
    route. A package with no page, or the draft page, is not proven live and
    reads ``drafted``.
    """
    url = str(package.get("url") or "").strip()
    return "validated" if url and not _DRAFT_PAGE.match(url) else "drafted"


def threat_package_for(
    events: list[CtemCaseEvent], view: ViewerView, *, sla_due_at: datetime | None = None, now: datetime | None = None
) -> dict[str, Any] | None:
    """The case's threat package as this viewer may read it (CTEM-16-UI, #1115).

    Whether a package was drafted or validated, whether its coverage was
    requested, and when, go to every viewer of the case; the Grouping's name,
    ids and OpenCTI link only to a viewer whose own OpenCTI read is available,
    else ``restricted``. ``coverage_requested`` is true only once the draft is
    validated (#1480): a coverage written in a draft never reached OpenAEV, and
    reads ``coverage_drafted``. ``draft_overdue``: still drafted past the case SLA.
    """
    package = threat_package_of(events)
    if package is None:
        return None
    state = threat_package_state(package)
    has_coverage = bool(package["coverage_id"])
    at = now or datetime.now(UTC)
    status = {
        "state": state,
        "coverage_requested": has_coverage and state == "validated",
        "coverage_drafted": has_coverage and state == "drafted",
        "draft_overdue": state == "drafted" and sla_due_at is not None and _aware(sla_due_at) < at,
    }
    if not view.opencti:
        return {"restricted": True, **status, "at": package["at"]}
    return {**package, **status, "restricted": False}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


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
        "exploitation": exploitation_for(case, view),
        "attack_path": attack_path_for(case, view),
        "treatment_plan": treatment_plan_for(events, view),
        "threat_package": threat_package_for(events, view, sla_due_at=case.sla_due_at),
    }


def exploitation_for(case: CtemCase, view: ViewerView) -> list[dict[str, Any]]:
    """The case's exploitation reading as this viewer may read it (#936).

    Techniques, exposure class and reading are served to every viewer of the
    case; the OpenCRQ asset name and scenario id only to a viewer whose own
    OpenCRQ read is available, else the asset is ``Restricted`` and the
    scenario absent.
    """
    out: list[dict[str, Any]] = []
    for entry in getattr(case, "exploitation", None) or []:
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                "asset": entry.get("asset") if view.opencrq else RESTRICTED,
                "risk_scenario_id": entry.get("risk_scenario_id") if view.opencrq else None,
                "exposure": entry.get("exposure") or "unknown",
                "techniques": list(entry.get("techniques") or []),
                "reading": entry.get("reading") or "",
                "at": entry.get("at"),
            }
        )
    return out


def attack_path_for(case: CtemCase, view: ViewerView) -> dict[str, Any] | None:
    """The case's attack-path reach as this viewer may read it (#1289); ``None`` before the first read.

    The shape (hops nearest first with proven or inferred, depths, criticality,
    exposure, counts) is served to every viewer of the case; the OpenCRQ asset
    names and the simulation (name, page, date) only to a viewer whose own
    OpenCRQ read is available, else each name is ``Restricted``. The raw
    simulation id is never served: it is kept for the trace (#1296, B2).
    """
    stored = getattr(case, "attack_path", None) or {}
    if not isinstance(stored, dict) or not isinstance(stored.get("origin"), dict):
        return None

    def name(value: object) -> str:
        return str(value or "") if view.opencrq else RESTRICTED

    origin = stored["origin"]
    return {
        "origin": {"asset": name(origin.get("asset")), "exposure": origin.get("exposure") or "unknown"},
        "hops": [
            {
                "from": name(h.get("from")),
                "to": name(h.get("to")),
                "nature": "proven" if h.get("nature") == "proven" else "inferred",
                "simulation_name": h.get("simulation_name") if view.opencrq else None,
                "simulation_url": h.get("simulation_url") if view.opencrq else None,
                "seen_at": h.get("seen_at"),
            }
            for h in stored.get("hops") or []
            if isinstance(h, dict)
        ],
        "reached": [
            {"asset": name(a.get("asset")), "depth": a.get("depth"), "criticality": a.get("criticality")}
            for a in stored.get("reached") or []
            if isinstance(a, dict)
        ],
        "primary": [name(p) for p in stored.get("primary") or []],
        "at": stored.get("at"),
    }


_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def list_for_viewer(
    cases: list[tuple[CtemCase, list[CtemCaseIssue]]],
    view: ViewerView,
    *,
    state: str | None = None,
    owner_user_id: str | None = None,
    priority: str | None = None,
    asset: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """One page of the cases this viewer sees and their counts by state.

    A case with no issue visible to the viewer is in neither: a count is data.
    *asset* keeps the cases one of whose issues the viewer sees names that asset
    (case-insensitive), the counts included: the cases on one asset.
    """
    if asset and asset.strip():
        cases = [(c, members) for c, members in cases if names_asset(members, view, asset)]
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


def names_asset(rows: list[CtemCaseIssue], view: ViewerView, asset: str) -> bool:
    """Whether an issue of the case this viewer sees names *asset* (case-insensitive, spaces folded)."""
    wanted = " ".join(asset.split()).casefold()
    for row in visible_members(rows, view):
        issue = view.issues.get(_member_id(row))
        if issue is not None and any(" ".join(a.split()).casefold() == wanted for a in issue.assets):
            return True
    return False


def owner_sees(rows: list[CtemCaseIssue], owner_view: ViewerView) -> bool:
    """Whether the would-be owner sees at least one issue of the case in the source products."""
    return bool(visible_members(rows, owner_view))


async def user_sees_case(db: Any, user_id: Any, case_id: Any, vertical: Any = None) -> bool:
    """Whether *user_id* may open case *case_id*: the API's rule (a 404 otherwise), from their own view.

    The platform events of a case reach only such users (#933). *vertical* is
    ignored: the case's own vertical decides the view read. Any unknown or
    unreadable input answers ``False``.
    """
    import uuid

    from app.services import ctem_cases_store as store
    from app.services.organizations import get_primary

    try:
        case_uuid = uuid.UUID(str(case_id))
        uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        return False
    primary = await get_primary(db)
    if primary is None:
        return False
    found = await store.get_case(db, case_uuid, organization_id=primary.id)
    if found is None:
        return False
    case, rows, _events = found
    return bool(visible_members(rows, await viewer_view(db, str(user_id), case.vertical)))
