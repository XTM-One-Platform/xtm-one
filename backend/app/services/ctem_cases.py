# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases: grouping by shared remediation, recompute and the state machine (#11545).

Design: ``dev-docs/ctem-cases.md`` sections 4 to 6.

- :func:`remediation_key` reads the key of one open issue from its data, first
  rule that answers wins: R1 a course of action, R2 a product and its fixed
  version (or ``@latest`` when the data names the product but no fixed
  version), R3 an OpenAEV CVE finding joins the key of the OpenCTI
  vulnerability with the same CVE, R4 nothing: no case, counted in "no known
  fix". Nothing is guessed.
- :func:`plan_recompute` merges what ONE user sees into the shared cases
  (#11613) without touching state, owner, due date or notes: new keys become
  cases in needs attention, an issue the user sees open is added or reopened,
  an issue is resolved only when that user's source read confirms it closed
  (never on absence from a narrower view), a case whose members are all
  resolved is resolved by the system unless its risk is accepted, a resolved
  case whose issues return is reopened.
- :func:`check_transition` is the state machine: accepted risk needs an owner,
  a reason and an expiry and is refused to an agent; resolved needs evidence;
  in progress needs an owner. :func:`expired_acceptance` returns a case whose
  acceptance expired to needs attention.

Pure functions over plain data: the API and the agent tools persist what they
return (``ctem_cases_store``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.models.ctem_case import CASE_STATES

#: Actor of the transitions recompute and the expiry sweep make.
SYSTEM_ACTOR = "system"
#: Accepted risk lasts at most this long without a policy saying otherwise.
DEFAULT_MAX_ACCEPTANCE = timedelta(days=90)
#: Evidence recorded when every member issue closed at source.
CLOSED_AT_SOURCE = "every member issue closed at source"

_SPACES = re.compile(r"\s+")


class CaseRuleError(ValueError):
    """A transition or an edit the state rules refuse; the message says why."""


@dataclass(frozen=True)
class SourceIssue:
    """One open issue read from OpenCTI or OpenAEV, normalized."""

    source: str  # opencti | openaev
    external_id: str
    name: str  # the CVE, or the finding name
    cve: str | None = None
    product: str | None = None
    vendor: str | None = None
    fixed_version: str | None = None
    course_of_action_id: str | None = None
    course_of_action_name: str | None = None
    cvss: float | None = None
    epss: float | None = None
    kev: bool = False
    assets: tuple[str, ...] = ()
    crown_jewels: tuple[str, ...] = ()
    first_seen_at: datetime | None = None

    @property
    def member_id(self) -> str:
        return f"{self.source}:{self.external_id}"


@dataclass(frozen=True)
class Remediation:
    """What one key stands for: the fix a case carries."""

    key: str
    kind: str  # upgrade | course_of_action
    title: str
    product_label: str | None = None
    fixed_version: str | None = None


def _norm(value: str | None) -> str:
    return _SPACES.sub(" ", (value or "").strip().lower())


def _product_label(vendor: str | None, product: str) -> str:
    vendor = (vendor or "").strip()
    product = product.strip()
    if vendor and not product.lower().startswith(vendor.lower()):
        return f"{vendor} {product}"
    return product


def remediation_of(issue: SourceIssue) -> Remediation | None:
    """R1 or R2 for one issue read alone; ``None`` when its data names no fix."""
    if issue.course_of_action_id:
        name = (issue.course_of_action_name or "").strip() or issue.course_of_action_id
        return Remediation(key=f"coa:{_norm(issue.course_of_action_id)}", kind="course_of_action", title=name)
    if issue.product and issue.product.strip():
        label = _product_label(issue.vendor, issue.product)
        vendor = _norm(issue.vendor) or "-"
        product = _norm(issue.product)
        version = _norm(issue.fixed_version)
        if version:
            fixed = (issue.fixed_version or "").strip()
            return Remediation(
                key=f"upgrade:{vendor}/{product}@{version}",
                kind="upgrade",
                title=f"Upgrade {label} to {fixed}",
                product_label=label,
                fixed_version=fixed,
            )
        return Remediation(
            key=f"upgrade:{vendor}/{product}@latest",
            kind="upgrade",
            title=f"Upgrade {label} to a fixed version",
            product_label=label,
        )
    return None


@dataclass
class Grouping:
    """The current issues grouped by key, and the ones with no known fix."""

    groups: dict[str, list[SourceIssue]] = field(default_factory=dict)
    remediations: dict[str, Remediation] = field(default_factory=dict)
    no_known_fix: list[SourceIssue] = field(default_factory=list)


def group_issues(issues: Iterable[SourceIssue]) -> Grouping:
    """Group open issues by remediation key (R1, R2, then R3 for OpenAEV CVE findings, R4).

    An issue is placed once per key it answers; duplicates of the same
    ``source:external_id`` under one key are kept once.
    """
    issues = list(issues)
    out = Grouping()
    cve_keys: dict[str, set[str]] = {}
    pending_aev: list[SourceIssue] = []

    def _add(key: str, issue: SourceIssue) -> None:
        members = out.groups.setdefault(key, [])
        if all(m.member_id != issue.member_id for m in members):
            members.append(issue)

    for issue in issues:
        remediation = remediation_of(issue)
        if remediation is None:
            if issue.source == "openaev" and issue.cve:
                pending_aev.append(issue)
            else:
                out.no_known_fix.append(issue)
            continue
        out.remediations.setdefault(remediation.key, remediation)
        _add(remediation.key, issue)
        if issue.source == "opencti" and issue.cve:
            cve_keys.setdefault(_norm(issue.cve), set()).add(remediation.key)

    # R3: an OpenAEV CVE finding joins every case its CVE is keyed to in OpenCTI.
    for issue in pending_aev:
        keys = sorted(cve_keys.get(_norm(issue.cve), ()))
        if not keys:
            out.no_known_fix.append(issue)
            continue
        for key in keys:
            _add(key, issue)
    return out


@dataclass
class CaseSnapshot:
    """What recompute reads of an existing case: its keys, state and members (id -> open)."""

    id: str
    keys: list[str]
    state: str
    pinned_issues: list[str] = field(default_factory=list)
    members: dict[str, bool] = field(default_factory=dict)


@dataclass
class CasePlan:
    """What one recompute does to one case (an existing one, or a new one when ``case_id`` is ``None``).

    ``add`` are new member ids, ``reopen`` and ``resolve`` member ids whose
    open flag changes; nothing else of a case is ever removed by recompute.
    """

    case_id: str | None
    remediation: Remediation | None
    add: list[str] = field(default_factory=list)
    reopen: list[str] = field(default_factory=list)
    resolve: list[str] = field(default_factory=list)
    new_state: str | None = None  # a system transition, else ``None``
    reason: str | None = None
    evidence: str | None = None


def plan_recompute(
    cases: Iterable[CaseSnapshot],
    issues: Iterable[SourceIssue],
    closed: Iterable[str] = (),
) -> list[CasePlan]:
    """Merge one user's view into the shared cases; never writes owner, due date or notes.

    *issues* are the open issues that user sees; *closed* the member ids that
    user's source read confirmed closed (the object is readable, the issue is
    no longer open). An open member that is in neither stays as it is: another
    user may see it, so a narrower view never closes or shrinks a case.
    Pinned issues (a manual split or a manual case) stay in their case and are
    taken out of the automatic groups.
    """
    cases = list(cases)
    issues = list(issues)
    closed_ids = set(closed)
    pinned: dict[str, str] = {}
    for case in cases:
        for member_id in case.pinned_issues:
            pinned[member_id] = case.id
    grouping = group_issues(i for i in issues if i.member_id not in pinned)

    key_owner: dict[str, CaseSnapshot] = {}
    for case in cases:
        for key in case.keys:
            key_owner.setdefault(key, case)

    seen_open: dict[str, set[str]] = {case.id: set() for case in cases}
    for issue in issues:
        if issue.member_id in pinned:
            seen_open[pinned[issue.member_id]].add(issue.member_id)

    plans: list[CasePlan] = []
    for key, members in grouping.groups.items():
        owner = key_owner.get(key)
        if owner is None:
            plans.append(
                CasePlan(
                    case_id=None,
                    remediation=grouping.remediations[key],
                    add=list(dict.fromkeys(m.member_id for m in members)),
                )
            )
            continue
        seen_open[owner.id].update(m.member_id for m in members)

    for case in cases:
        plan = CasePlan(case_id=case.id, remediation=None)
        for member_id in sorted(seen_open[case.id]):
            if member_id not in case.members:
                plan.add.append(member_id)
            elif not case.members[member_id]:
                plan.reopen.append(member_id)
        plan.resolve = sorted(
            m for m, is_open in case.members.items() if is_open and m in closed_ids and m not in seen_open[case.id]
        )
        after = dict(case.members)
        after.update({m: True for m in plan.add + plan.reopen})
        after.update({m: False for m in plan.resolve})
        any_open = any(after.values())
        if after and not any_open and case.state in ("needs_attention", "in_progress"):
            plan.new_state = "resolved"
            plan.evidence = CLOSED_AT_SOURCE
        elif any_open and case.state == "resolved":
            plan.new_state = "needs_attention"
            plan.reason = "reopened: issues open again at source"
        plans.append(plan)
    return plans


def as_utc(value: datetime | None) -> datetime | None:
    """A datetime given without an offset is read as UTC; an aware one is kept as it is."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def check_owner(state: str, owner_user_id: object | None) -> None:
    """Raise :class:`CaseRuleError` when the case's state cannot stand without an owner."""
    if owner_user_id is not None:
        return
    if state == "in_progress":
        raise CaseRuleError("a case in progress needs an owner")
    if state == "accepted_risk":
        raise CaseRuleError("an accepted risk needs an owner")


def check_transition(
    *,
    current: str,
    target: str,
    actor_kind: str,
    owner_user_id: object | None,
    reason: str | None = None,
    evidence: str | None = None,
    accepted_until: datetime | None = None,
    now: datetime,
    max_acceptance: timedelta = DEFAULT_MAX_ACCEPTANCE,
) -> None:
    """Raise :class:`CaseRuleError` when the transition is refused.

    ``actor_kind`` is ``user``, ``agent`` or ``system``.
    """
    if target not in CASE_STATES:
        raise CaseRuleError(f"unknown state {target!r}")
    if target == current:
        raise CaseRuleError(f"the case is already {target}")
    reason = (reason or "").strip()
    evidence = (evidence or "").strip()
    accepted_until = as_utc(accepted_until)
    if target == "in_progress" and owner_user_id is None:
        raise CaseRuleError("a case in progress needs an owner")
    if target == "resolved" and not evidence:
        raise CaseRuleError("resolving a case needs evidence (a ticket, a change or a validation run)")
    if target == "accepted_risk":
        if actor_kind != "user":
            raise CaseRuleError("only a person accepts risk")
        if owner_user_id is None:
            raise CaseRuleError("accepting risk needs an owner")
        if not reason:
            raise CaseRuleError("accepting risk needs a reason")
        if accepted_until is None:
            raise CaseRuleError("accepting risk needs an expiry date")
        if accepted_until <= now:
            raise CaseRuleError("the expiry date must be in the future")
        if accepted_until > now + max_acceptance:
            raise CaseRuleError(f"the expiry date is at most {max_acceptance.days} days away")
    if (
        target == "needs_attention"
        and current in ("resolved", "accepted_risk")
        and actor_kind == "agent"
        and not reason
    ):
        raise CaseRuleError("reopening a case needs a reason")


def expired_acceptance(state: str, accepted_until: datetime | None, now: datetime) -> bool:
    """True when an accepted risk has expired and the case returns to needs attention."""
    return state == "accepted_risk" and accepted_until is not None and accepted_until <= now


_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def default_priority(members: Iterable[SourceIssue]) -> str:
    """The priority without a prioritization policy: KEV or CVSS >= 9 critical, then CVSS and EPSS."""
    best = "low"
    for issue in members:
        cvss = issue.cvss or 0.0
        epss = issue.epss or 0.0
        if issue.kev or cvss >= 9.0:
            level = "critical"
        elif cvss >= 7.0 or epss >= 0.5:
            level = "high"
        elif cvss >= 4.0 or epss >= 0.1:
            level = "medium"
        else:
            level = "low"
        if _PRIORITY_RANK[level] < _PRIORITY_RANK[best]:
            best = level
    return best


def aggregates(members: Iterable[SourceIssue]) -> dict[str, object]:
    """The counts and maxima a case carries for its list row."""
    members = list(members)
    assets = {a for m in members for a in m.assets}
    jewels = {j for m in members for j in m.crown_jewels}
    cvss = [m.cvss for m in members if m.cvss is not None]
    epss = [m.epss for m in members if m.epss is not None]
    seen = [m.first_seen_at for m in members if m.first_seen_at is not None]
    return {
        "issue_count": len(members),
        "asset_count": len(assets),
        "crown_jewel_count": len(jewels),
        "kev_count": sum(1 for m in members if m.kev),
        "max_cvss": max(cvss) if cvss else None,
        "max_epss": max(epss) if epss else None,
        "first_seen_at": min(seen) if seen else None,
    }
