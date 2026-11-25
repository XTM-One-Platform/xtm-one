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
  cases in ``new``, an issue the user sees open is added or reopened,
  an issue is resolved only when that user's source read confirms it closed
  (never on absence from a narrower view), a case whose members are all
  resolved is resolved by the system unless its risk is accepted, a resolved
  case whose issues return is reopened.
- :func:`check_transition` is the state machine: accepted risk needs an owner,
  a reason and an expiry and is refused to an agent; resolved needs evidence;
  a person moving a case to remediating names its owner. :func:`expired_acceptance`
  returns a case whose acceptance expired to ``new``. The loop's own moves
  between the open statuses are ``ctem_case_status`` (#1503).

Pure functions over plain data: the API and the agent tools persist what they
return (``ctem_cases_store``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from app.models.ctem_case import CASE_STATES, OPEN_STATES, RESOLUTION_KINDS
from app.services.ctem_fixed_version import compare_versions, fold_by_platform, smallest_fixing_every

#: Actor of the transitions recompute and the expiry sweep make.
SYSTEM_ACTOR = "system"
#: Accepted risk lasts at most this long without a policy saying otherwise.
DEFAULT_MAX_ACCEPTANCE = timedelta(days=90)
#: Evidence recorded when every member issue closed at source.
CLOSED_AT_SOURCE = "every member issue closed at source"
#: The evidence of an upgrade case the asset inventory shows fixed on every asset (#1252).
FIXED_IN_INVENTORY = "every asset running the product runs the fixed version or later (asset inventory)"
#: The evidence of one issue whose known fixed version is at or below every running version (#2047).
FIXED_BELOW_RUNNING = "fixed version at or below every version the asset inventory shows running"
#: The evidence of a case whose every issue is fixed below every running version (#2047).
ALL_FIXED_BELOW_RUNNING = (
    "every issue of the case is fixed in a version at or below every version running (asset inventory)"
)

_SPACES = re.compile(r"\s+")


class CaseRuleError(ValueError):
    """A transition or an edit the state rules refuse; the message says why."""


#: The outcome OpenCTI gives one installed version of an issue (#1783).
VERSION_AFFECTED = "affected"
VERSION_NOT_AFFECTED = "not_affected"
#: No Software of that version in OpenCTI: "version not known to OpenCTI".
VERSION_UNKNOWN = "unknown"


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
    primary_assets: tuple[str, ...] = ()
    first_seen_at: datetime | None = None
    #: The version the organization profile declares for the matched product (CTEM-11).
    installed_version: str | None = None
    #: Set when OpenCTI relates no vulnerability to the declared version: the reason; no case opens.
    not_affected: str | None = None
    #: What OpenCTI could not tell ("version not known to OpenCTI", "fixed version unknown"), else ``None``.
    version_note: str | None = None
    #: The OpenCTI Software ("<name> <version>") whose ``remediates`` gave ``fixed_version`` (#928), else ``None``.
    fixed_by: str | None = None
    #: ``((platform, version), ...)`` when the remediating Software names its platform (#1572), else ``()``.
    fixed_versions: tuple[tuple[str, str], ...] = ()
    #: With installed versions: whether OpenCTI holds a Software of one of them (#928); ``None`` when none is known.
    declared_version_known: bool | None = None
    #: ``((version, outcome), ...)``: each installed version and what OpenCTI says of it (#1783), else ``()``.
    version_outcomes: tuple[tuple[str, str], ...] = ()
    #: OpenAEV finding lifecycle (CTEM-14-UI): ``open``, ``not_reproduced`` or ``resolved``;
    #: ``None`` when the source does not carry it (OpenCTI, an OpenAEV older than the lifecycle).
    retest_status: str | None = None
    #: When a re-test last ended on the finding, ISO 8601, else ``None``.
    last_tested_at: str | None = None
    #: A re-test is queued on the finding and has not finished.
    retest_pending: bool = False

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
    #: ``((platform, version), ...)``: the fix of each platform the remediation names (#1572), else ``()``.
    fixed_versions: tuple[tuple[str, str], ...] = ()


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
        return Remediation(
            key=f"coa:{_norm(issue.course_of_action_id)}",
            kind="course_of_action",
            title=name,
            # A course of action that is a plain upgrade names its version (CTEM-11).
            fixed_version=(issue.fixed_version or "").strip() or None,
            fixed_versions=issue.fixed_versions,
        )
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
                fixed_versions=issue.fixed_versions,
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
    #: Issues OpenCTI does not relate to the declared version (CTEM-11): listed, never a case.
    not_affected: list[SourceIssue] = field(default_factory=list)


def _versions_known(issue: SourceIssue) -> bool:
    """Whether the issue's installed version, or its installed versions (#1783), are known."""
    return bool(issue.installed_version or issue.version_outcomes)


def _fold_upgrades(issues: list[SourceIssue]) -> list[SourceIssue]:
    """One target per product when the profile declares its installed version (CTEM-11).

    The upgrade members of one product whose installed version is declared
    take the smallest version that fixes every one of them, so they share one
    key. Without a declared version nothing changes.
    """
    targets: dict[tuple[str, str], list[str]] = {}
    for issue in issues:
        if issue.course_of_action_id or not (issue.product or "").strip() or not _versions_known(issue):
            continue
        if issue.fixed_version:
            targets.setdefault((_norm(issue.vendor), _norm(issue.product)), []).append(issue.fixed_version.strip())
    target = {k: smallest_fixing_every(v) for k, v in targets.items()}
    # The per-platform fixes fold the same way (#1572); one member without them makes the fold generic.
    per_platform: dict[tuple[str, str], list[dict[str, str]]] = {}
    for issue in issues:
        if issue.course_of_action_id or not (issue.product or "").strip() or not _versions_known(issue):
            continue
        if issue.fixed_version:
            per_platform.setdefault((_norm(issue.vendor), _norm(issue.product)), []).append(dict(issue.fixed_versions))
    platform_target = {k: tuple(sorted(fold_by_platform(v).items())) for k, v in per_platform.items()}
    out: list[SourceIssue] = []
    for issue in issues:
        key = (_norm(issue.vendor), _norm(issue.product))
        fixed = target.get(key)
        if fixed and _versions_known(issue) and issue.fixed_version and not issue.course_of_action_id:
            issue = replace(issue, fixed_version=fixed, fixed_versions=platform_target.get(key, ()))
        out.append(issue)
    return out


def group_issues(all_issues: Iterable[SourceIssue]) -> Grouping:
    """Group open issues by remediation key (R1, R2, then R3 for OpenAEV CVE findings, R4).

    An issue is placed once per key it answers; duplicates of the same
    ``source:external_id`` under one key are kept once. An issue whose declared
    version OpenCTI does not relate to the vulnerability goes to ``not_affected`` (CTEM-11).
    """
    out = Grouping()
    issues: list[SourceIssue] = []
    for issue in all_issues:
        if issue.not_affected:
            out.not_affected.append(issue)
        else:
            issues.append(issue)
    issues = _fold_upgrades(issues)
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
    #: A case with a validation plan never resolves on absence at source (#857).
    validation_plan: bool = False
    #: An upgrade case whose product the asset inventory shows on the fixed version or later on every
    #: asset that runs it (#1252): it leaves the open cases even while its issues stay open at source.
    fixed_in_inventory: bool = False
    #: The evidence of the decision that resolved the case when a person or an agent resolved it (a gate
    #: answered not exploitable, a passing re-test), ``None`` for a resolution recompute made (#1646): such
    #: a closure holds while the case's issues stay as they were, whatever their state at source.
    decided_closure: str | None = None
    #: Every version of the case's product the asset inventory shows running (#2047), ``None`` when not read or
    #: when no asset runs it. An issue whose known fixed version is at or below all of them leaves the case.
    running_versions: tuple[str, ...] | None = None


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
    #: Keys an existing case takes over: a folded upgrade target that moved (CTEM-11).
    add_keys: list[str] = field(default_factory=list)
    #: Members of ``resolve`` (or seen members not added) settled because their fix is below every running
    #: version (#2047): their ``issue_closed`` event carries :data:`FIXED_BELOW_RUNNING`.
    settled: list[str] = field(default_factory=list)


def fixed_below_running(issue: SourceIssue, running: Iterable[str] | None) -> bool:
    """Whether *issue* is fixed in a version at or below every running version (pure, #2047).

    Known data only: ``False`` without a fixed version, without a running version, or when one
    running version (or one per-platform fix) cannot be compared. Per-platform fixes count too: the
    highest of them must be at or below every running version. An unknown is never read as fixed.
    """
    running = [str(v).strip() for v in running or ()]
    fixes = [(issue.fixed_version or "").strip(), *(str(v).strip() for _p, v in issue.fixed_versions)]
    fixes = [f for f in fixes if f]
    if not running or not fixes or not (issue.fixed_version or "").strip():
        return False
    for fixed in fixes:
        for version in running:
            cmp = compare_versions(version, fixed)
            if cmp is None or cmp < 0:
                return False
    return True


def _upgrade_prefix(key: str) -> str | None:
    """``upgrade:<vendor>/<product>@`` for an upgrade key, else ``None``."""
    if not key.startswith("upgrade:") or "@" not in key:
        return None
    return key.rsplit("@", 1)[0] + "@"


def _upgrade_parts(key: str) -> tuple[str, str, str] | None:
    """``(vendor, product, version)`` of an upgrade key, lower-cased as keys are; ``None`` for another key."""
    if not key.startswith("upgrade:") or "@" not in key or "/" not in key:
        return None
    head, version = key[len("upgrade:") :].rsplit("@", 1)
    vendor, product = head.split("/", 1)
    return vendor.strip().casefold(), product.strip().casefold(), version.strip().casefold()


def same_product_key(left: str, right: str) -> bool:
    """Whether two upgrade keys name one fix of one product under two spellings (#2037).

    Same version, and the product named the same once the vendor is set apart:
    ``-/minio`` and ``minio/minio``, ``-/windows``, ``-/microsoft windows`` and
    ``microsoft/windows``. Two known vendors that differ never match; two
    vendor-less names match only when equal (nothing tells their vendor).
    """
    if left == right:
        return True
    a, b = _upgrade_parts(left), _upgrade_parts(right)
    if a is None or b is None or a[2] != b[2]:
        return False
    (va, pa, _), (vb, pb, _) = a, b
    if va != "-" and vb != "-" and va != vb:
        return False

    def bare(vendor: str, product: str) -> str:
        if vendor != "-" and product.startswith(vendor + " "):
            return product[len(vendor) + 1 :].strip()
        return product

    def full(vendor: str, product: str) -> str:
        if vendor == "-" or product == vendor or product.startswith(vendor + " "):
            return product
        return f"{vendor} {product}"

    if va == "-" and vb == "-":
        return pa == pb
    return bare(va, pa) == bare(vb, pb) or full(va, pa) == full(vb, pb)


def _alias_owner(key: str, cases: list[CaseSnapshot]) -> CaseSnapshot | None:
    """The open case whose keys name the same fix of the same product under another spelling (#2037)."""
    for case in cases:
        if case.state in OPEN_STATES and any(same_product_key(key, k) for k in case.keys):
            return case
    return None


def _holder_of(key: str, members: list[SourceIssue], cases: list[CaseSnapshot]) -> CaseSnapshot | None:
    """The existing case of the same product that already holds members of an unowned upgrade key.

    The folded target of a product (section 4.2bis) moves when a member with a
    higher fix appears, when the member that set it is patched, or the first
    time a version is declared on an ``@latest`` case. The case that holds
    those members keeps them and takes the new key, so no second case opens.
    """
    prefix = _upgrade_prefix(key)
    if prefix is None:
        return None
    ids = {m.member_id for m in members}
    best: CaseSnapshot | None = None
    best_count = 0
    for case in cases:
        if not any(k.startswith(prefix) for k in case.keys):
            continue
        count = len(ids & set(case.members))
        if count > best_count:
            best, best_count = case, count
    return best


def _running_of_product(key: str, cases: list[CaseSnapshot]) -> tuple[str, ...] | None:
    """The running versions read on an existing case of the same upgrade product as *key*, else ``None``."""
    prefix = _upgrade_prefix(key)
    if prefix is None:
        return None
    for case in cases:
        if case.running_versions and any(k.startswith(prefix) for k in case.keys):
            return case.running_versions
    return None


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
        if issue.member_id in pinned and not issue.not_affected:
            seen_open[pinned[issue.member_id]].add(issue.member_id)

    plans: list[CasePlan] = []
    taken_keys: dict[str, list[str]] = {}
    retarget: dict[str, Remediation] = {}
    for key, members in grouping.groups.items():
        owner = key_owner.get(key)
        if owner is None:
            owner = _holder_of(key, members, cases)
            if owner is not None:
                key_owner[key] = owner
                taken_keys.setdefault(owner.id, []).append(key)
                retarget[owner.id] = grouping.remediations[key]
        if owner is None:
            # The same fix of the same product under another spelling joins its open case (#2037).
            owner = _alias_owner(key, cases)
            if owner is not None:
                key_owner[key] = owner
                taken_keys.setdefault(owner.id, []).append(key)
        if owner is None:
            # #2047: a new upgrade group whose issues are all fixed below every version the product runs (read
            # on a case of the same product) opens no case; the issues fixed below are left out of it.
            running = _running_of_product(key, cases)
            members = [m for m in members if not fixed_below_running(m, running)]
            if not members:
                continue
            plans.append(
                CasePlan(
                    case_id=None,
                    remediation=grouping.remediations[key],
                    add=list(dict.fromkeys(m.member_id for m in members)),
                )
            )
            continue
        seen_open[owner.id].update(m.member_id for m in members)

    by_member = {i.member_id: i for i in issues}
    for case in cases:
        plan = CasePlan(case_id=case.id, remediation=retarget.get(case.id), add_keys=taken_keys.get(case.id, []))
        # #2047: an issue whose known fix is at or below every running version leaves the active issues
        # (kept in history); an unknown fix or an unknown running version keeps it, to validate.
        settled = {
            m for m in seen_open[case.id] if m in by_member and fixed_below_running(by_member[m], case.running_versions)
        }
        for member_id in sorted(seen_open[case.id] - settled):
            if member_id not in case.members:
                plan.add.append(member_id)
            elif not case.members[member_id]:
                plan.reopen.append(member_id)
        plan.settled = sorted(m for m in settled if case.members.get(m))
        plan.resolve = sorted(
            {
                *(
                    m
                    for m, is_open in case.members.items()
                    if is_open and m in closed_ids and m not in seen_open[case.id]
                ),
                *plan.settled,
            }
        )
        after = dict(case.members)
        after.update({m: True for m in plan.add + plan.reopen})
        after.update({m: False for m in plan.resolve})
        any_open = any(after.values())
        # A member this read sees open with no known fixed version keeps the case open, to validate (#2047).
        unknown_fix = any(
            is_open and m in by_member and not (by_member[m].fixed_version or "").strip()
            for m, is_open in after.items()
        )
        if case.fixed_in_inventory and case.id not in retarget and not unknown_fix:
            # No asset is left below the fix: the case resolves (a validation plan still needs its proof)
            # and is never reopened by issues that stay open at source on a version nobody runs.
            if case.state in OPEN_STATES and not case.validation_plan:
                plan.new_state = "resolved"
                plan.evidence = FIXED_IN_INVENTORY
        elif after and not any_open and case.state in OPEN_STATES and not case.validation_plan:
            plan.new_state = "resolved"
            plan.evidence = ALL_FIXED_BELOW_RUNNING if plan.settled else CLOSED_AT_SOURCE
        elif any_open and case.state == "resolved":
            if case.decided_closure is None:
                plan.new_state = "new"
                plan.reason = "reopened: issues open again at source"
            elif plan.add or plan.reopen or case.id in retarget:
                # A decided closure reopens only on a changed issue: a new CVE or asset, an issue that closed
                # and came back, or a moved fixed version; the event names the decision it overrides (#1646).
                plan.new_state = "new"
                plan.reason = f"reopened: issues changed since the closure decision ({case.decided_closure})"[:500]
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
    if state == "accepted_risk":
        raise CaseRuleError("an accepted risk needs an owner")


#: Validation results that prove the exposure gone (or never there).
PASSING_RESULTS = ("passed", "not_exposed")


def latest_validation(validations: Iterable[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The newest validation reference of a case (the list is kept oldest first), ``None`` when none."""
    items = [v for v in (validations or []) if isinstance(v, dict)]
    return items[-1] if items else None


def passing(validation: dict[str, Any] | None) -> bool:
    """A pass that proves: an entry marked inconclusive (``conclusive`` false, #1458) proves nothing."""
    return (
        validation is not None
        and validation.get("result") in PASSING_RESULTS
        and validation.get("conclusive") is not False
    )


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
    validation_plan: bool = False,
    validations: Iterable[dict[str, Any]] | None = None,
    resolution_kind: str | None = None,
    close_without_retest_answered: bool = False,
) -> None:
    """Raise :class:`CaseRuleError` when the transition is refused.

    ``actor_kind`` is ``user``, ``agent`` or ``system``. A person who moves a
    case to ``remediating`` takes or names its owner (#1503; the loop moves it
    there on a ticket or a decision without one). A case with a
    validation plan resolves, or goes to ``mitigated_watching``, only when its
    newest validation reference passed (#857): free-text evidence is not proof,
    unless a person decided otherwise (#1675): a person's own move, or a
    ``ctem.close_without_retest`` decision answered on the case
    (*close_without_retest_answered*), lets it close without the re-test.
    """
    if target not in CASE_STATES:
        raise CaseRuleError(f"unknown state {target!r}")
    if target == current:
        raise CaseRuleError(f"the case is already {target}")
    if resolution_kind is not None and resolution_kind not in RESOLUTION_KINDS:
        raise CaseRuleError(f"unknown resolution kind {resolution_kind!r}")
    reason = (reason or "").strip()
    evidence = (evidence or "").strip()
    accepted_until = as_utc(accepted_until)
    if target == "remediating" and actor_kind == "user" and owner_user_id is None:
        raise CaseRuleError("a person moving a case to remediating names its owner")
    if target == "waiting_decision" and actor_kind == "user" and not reason:
        raise CaseRuleError("waiting for decision needs what the case waits on (the reason)")
    if target == "resolved" and not evidence:
        raise CaseRuleError("resolving a case needs evidence (a ticket, a change or a validation run)")
    proven = passing(latest_validation(validations))
    decided = actor_kind == "user" or close_without_retest_answered
    if target in ("resolved", "mitigated_watching") and validation_plan and not proven and not decided:
        raise CaseRuleError("this case has a validation plan: it closes only on a passing validation reference")
    if target == "mitigated_watching" and not proven and not decided:
        raise CaseRuleError("mitigated, watching needs a passing validation reference of the mitigation")
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
        target in OPEN_STATES
        and current in ("resolved", "accepted_risk", "mitigated_watching")
        and actor_kind == "agent"
        and not reason
    ):
        raise CaseRuleError("reopening a case needs a reason")


def expired_acceptance(state: str, accepted_until: datetime | None, now: datetime) -> bool:
    """True when an accepted risk has expired and the case returns to ``new``."""
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
    jewels = {j for m in members for j in m.primary_assets}
    cvss = [m.cvss for m in members if m.cvss is not None]
    epss = [m.epss for m in members if m.epss is not None]
    seen = [m.first_seen_at for m in members if m.first_seen_at is not None]
    return {
        "issue_count": len(members),
        "asset_count": len(assets),
        "primary_asset_count": len(jewels),
        "kev_count": sum(1 for m in members if m.kev),
        "max_cvss": max(cvss) if cvss else None,
        "max_epss": max(epss) if epss else None,
        "first_seen_at": min(seen) if seen else None,
    }
