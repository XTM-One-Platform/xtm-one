# SPDX-License-Identifier: Apache-2.0

"""CTEM cases as the record of the loop (#857): opened from signals, updated on events, closed on proof.

The third brick of the spine: signal (``ctem_signals``) -> reconcile and scope
gate (``ctem_scope_gate``) -> case. Rules here, no model call:

- :func:`open_from_signal`: a confirmed signal whose decision outcome is
  ``case`` opens the case of its fix, or joins the open case of the same fix
  (one case per remediation, every origin signal attached as a ``signal``
  member). Severity, SLA and risk scenarios come from the decision record; the
  signal moves to ``case_opened``.
- :func:`record_validation`: a validation result (OpenAEV run, re-test,
  provider re-scan) is written on the case as a reference; a pass resolves the
  case (or ``mitigated_watching`` when only the mitigation is proven), a fail
  reopens a resolved or watched case. No manual recompute.
- :func:`sweep`: the scheduled pass (``services/autonomous.py``): opens the
  cases of confirmed signals still waiting, escalates every SLA breach once,
  and raises every open case that waits on nobody (``ctem_case_stall``, #1736).

Every opening, reopen, resolve and SLA breach raises a platform event
(:data:`CASE_EVENTS`) once the caller has committed (:func:`publish_pending`),
so the seeded fleets can start a run from a case.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_case import (
    INCONCLUSIVE_RESULT,
    OPEN_STATES,
    REMEDIATION_KINDS,
    VALIDATION_RESULTS,
    CtemCase,
    CtemCaseEvent,
    CtemCaseIssue,
)
from app.models.ctem_signal import CtemSignal
from app.services import ctem_case_status as case_status
from app.services import ctem_cases as rules

logger = logging.getLogger(__name__)

#: The platform events a case raises (registered in ``platform_events.PLATFORM_EVENTS``).
CASE_OPENED = "ctem_case.opened"
CASE_REOPENED = "ctem_case.reopened"
CASE_RESOLVED = "ctem_case.resolved"
CASE_SLA_BREACHED = "ctem_case.sla_breached"
CASE_STALLED = "ctem_case.stalled"
CASE_EVENTS: tuple[str, ...] = (CASE_OPENED, CASE_REOPENED, CASE_RESOLVED, CASE_SLA_BREACHED, CASE_STALLED)

#: SLA per severity when the decision record carries none (dev-docs/ctem-scope-gate.md section 4).
DEFAULT_SLA_HOURS: dict[str, int] = {"P1": 24, "P2": 72, "P3": 720}

#: Which fix a signal's trigger path asks for when its subject names no product to upgrade.
PATH_REMEDIATION: dict[str, str] = {
    "new_vulnerability": "patch",
    "relevant_threat": "manual",
    "code_flaw": "patch",
    "proven_exploitable": "patch",
    "new_exposure": "close_exposure",
    "stale_asset": "decommission",
    "surface_drift": "close_exposure",
    "mitigation_regression": "restore_control",
    "control_decay": "restore_control",
    "credential_leak": "rotate_credential",
    "third_party_event": "supplier_action",
    "blind_spot": "restore_coverage",
}

_TITLES: dict[str, str] = {
    "patch": "Patch {subject}",
    "manual": "Respond to {subject}",
    "close_exposure": "Close the exposure of {subject}",
    "decommission": "Decommission {subject}",
    "restore_control": "Restore the control on {subject}",
    "rotate_credential": "Rotate the credential of {subject}",
    "supplier_action": "Act with the supplier {subject}",
    "restore_coverage": "Restore coverage of {subject}",
}

#: The action a ``control_decay`` signal asks for, by its evidence state (#2069): a control never
#: measured has nothing to restore. Titled on the asset name; the reason stays on the signal.
#: ``never_measured`` says nothing about a missing control, only that no test evidence exists yet:
#: the action is a first measurement of the asset (#2352).
_CONTROL_DECAY_TITLES: dict[str, str] = {
    "never_measured": "Measure {asset} for the first time (no test evidence yet)",
    "attested_only": "Measure the control on {asset} (attested only)",
    "expired": "Re-test the control on {asset}",
}

#: Most signals the sweep opens cases for in one pass.
SWEEP_BATCH = 500
#: The member source of a signal attached to a case.
SIGNAL_SOURCE = "signal"
#: Most never-announced cases one sweep announces.
ANNOUNCE_BATCH = 200
#: Width of ``CtemCase.title`` and ``CtemCaseEvent.to_value`` (the opening event carries the key).
COLUMN_MAX = 300
_KEY_SPACES = re.compile(r"\s+")
#: A CVE id anywhere in a text (#2558): a vulnerability patch case keys on it whatever the subject spelling.
_CVE_ID = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.IGNORECASE)
#: Why the sweep folded an open case into another one (#2558).
DUPLICATE_MERGE_REASON = "duplicate open case of the same fix (#2558)"


@dataclass
class CaseFix:
    """The fix a signal asks for: one case per key."""

    key: str
    kind: str
    title: str
    #: The version that fixes it, when the signal names one (an upgrade case keys and titles on it).
    fixed_version: str | None = None
    #: ``{platform: version}`` when the source names the fix of each platform (#1572).
    fixed_versions: dict[str, str] = field(default_factory=dict)
    #: Other spellings of the same key an open case may already hold (#2558): the signal joins it.
    aliases: tuple[str, ...] = ()


@dataclass
class PendingEvents:
    """Platform events to raise after the caller's commit, in order.

    ``verticals`` names the verticals whose case membership changed (a case
    opened, a signal joined one): their cached viewer views are dropped after
    the commit, so the case shows on the board and its page at once instead of
    after the view TTL.
    """

    items: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)
    verticals: set[str] = field(default_factory=set)
    #: Live frames for the web app (``ctem_case_update``, CTEM-03-UI #892): ids and states only.
    frames: list[dict[str, str]] = field(default_factory=list)

    def add(self, name: str, data: dict[str, Any], event_id: str) -> None:
        self.items.append((name, data, event_id))

    def frame(self, case: CtemCase, event_kind: str, at: datetime) -> None:
        from app.services.ctem_loop_stream import case_frame

        self.frames.append(case_frame(str(case.id), case.state, event_kind, at.isoformat()))


def _norm(value: str | None) -> str:
    return _KEY_SPACES.sub(" ", (value or "").strip().lower())


def _bounded_key(key: str) -> str:
    """*key* within :data:`COLUMN_MAX`; a longer one keeps its head and a digest of the whole, so two keys stay apart."""
    if len(key) <= COLUMN_MAX:
        return key
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return f"{key[: COLUMN_MAX - len(digest) - 1]}#{digest}"


def fix_of(signal: CtemSignal) -> CaseFix:
    """The remediation a signal opens: a CVE or software subject keys on the product, else on path and subject.

    A second signal about the same subject and path lands on the same key and
    joins the open case (one case per fix).
    """
    path = signal.trigger_path
    subject = (signal.subject_label or signal.subject_ref or "").strip()
    kind = PATH_REMEDIATION.get(path, "manual")
    evidence = getattr(signal, "evidence", None)
    evidence = evidence if isinstance(evidence, dict) else {}
    named = source_fix_of(evidence)
    if named is not None:
        return named
    package = str(evidence.get("affected_package") or "").strip()
    fixed = str(evidence.get("fixed_version") or "").strip()[:100]
    repository = str(evidence.get("repository") or "").strip()
    if kind == "patch" and package and fixed and repository:
        # A dependency alert on one repository (a Dependabot alert, #1331): the upgrade is
        # made in that repository, so the case keys and titles on it too.
        return CaseFix(
            key=_bounded_key(f"upgrade:{_norm(repository)}/{_norm(package)}@{_norm(fixed)}"),
            kind="upgrade",
            title=f"Upgrade {package} to {fixed} in {repository}"[:COLUMN_MAX],
            fixed_version=fixed,
        )
    if kind == "patch" and package and fixed:
        # A source that names the package and the version that fixes it (an Aikido dependency
        # issue, CTEM-25): the upgrade case keys and titles on that version.
        return CaseFix(
            key=_bounded_key(f"upgrade:-/{_norm(package)}@{_norm(fixed)}"),
            kind="upgrade",
            title=f"Upgrade {package} to {fixed}"[:COLUMN_MAX],
            fixed_version=fixed,
        )
    if signal.subject_kind == "software" and kind == "patch":
        return CaseFix(
            key=_bounded_key(f"upgrade:-/{_norm(signal.subject_ref)}@latest"),
            kind="upgrade",
            title=f"Upgrade {subject} to a fixed version"[:COLUMN_MAX],
        )
    key = _bounded_key(f"{kind}:{signal.subject_kind}/{_norm(signal.subject_ref)}")
    cve = signal_cve(signal) if kind == "patch" and signal.subject_kind == "vulnerability" else ""
    if cve:
        # #2558: one CVE is one patch case, whether its subject is the CVE id, its STIX id or a name.
        canonical = _bounded_key(f"patch:vulnerability/{cve.lower()}")
        aliases = (key,) if key != canonical else ()
        # A subject that is only a STIX id is titled on the CVE; any other keeps its own words.
        shown = subject if subject and not subject.startswith("vulnerability--") else cve
        return CaseFix(key=canonical, kind=kind, title=f"Patch {shown}"[:COLUMN_MAX], aliases=aliases)
    decay = _CONTROL_DECAY_TITLES.get(str(evidence.get("state") or "")) if path == "control_decay" else None
    asset = (signal.subject_ref or "").strip()
    if decay and asset:
        return CaseFix(key=key, kind=kind, title=decay.format(asset=asset)[:COLUMN_MAX])
    title = _TITLES.get(kind, "Remediate {subject}").format(subject=subject)[:COLUMN_MAX]
    return CaseFix(key=key, kind=kind, title=title)


def signal_cve(signal: Any) -> str:
    """The CVE id a signal is about, upper case, empty when none (#2558).

    Read from the evidence's ``cve`` first, then the subject reference and label.
    """
    evidence = getattr(signal, "evidence", None)
    evidence = evidence if isinstance(evidence, dict) else {}
    for value in (evidence.get("cve"), getattr(signal, "subject_ref", None), getattr(signal, "subject_label", None)):
        found = _CVE_ID.search(value) if isinstance(value, str) else None
        if found:
            return found.group(0).upper()
    return ""


def canonical_key(key: str) -> str:
    """The spelling of a case key two open cases of one fix share (#2558): a vulnerability patch on its CVE."""
    if key.startswith("patch:vulnerability/"):
        found = _CVE_ID.search(key)
        if found:
            return f"patch:vulnerability/{found.group(0).lower()}"
    return key


#: Why a software signal with nothing to install opens no upgrade case (#2047).
NOTHING_TO_UPGRADE = (
    "no CVE, no affected version range and no fixed version: nothing to install (a vendor fix made "
    "service-side); kept to validate, no upgrade case"
)
#: Evidence keys any one of which names something an upgrade can act on.
_UPGRADE_FACTS: tuple[str, ...] = (
    "cve",
    "cve_id",
    "cves",
    "vulnerable_range",
    "affected_versions",
    "affected_packages",
    "fixed_version",
    "patched_versions",
)


def nothing_to_upgrade(signal: CtemSignal, fix: CaseFix) -> bool:
    """Whether *fix* is an ``@latest`` upgrade the signal gives nothing to act on (pure, #2047).

    True only for an ``upgrade:...@latest`` key from a signal that names no CVE (subject or evidence),
    no affected range and no fixed version. A signal with any of them keeps its upgrade case.
    """
    if fix.kind != "upgrade" or not fix.key.endswith("@latest") or fix.fixed_version:
        return False
    for ref in (signal.subject_ref, signal.subject_label):
        if str(ref or "").strip().upper().startswith("CVE-"):
            return False
    evidence = getattr(signal, "evidence", None)
    evidence = evidence if isinstance(evidence, dict) else {}
    return not any(evidence.get(k) for k in _UPGRADE_FACTS)


def source_fix_of(evidence: dict[str, Any]) -> CaseFix | None:
    """The remediation a source reader named on its signal (#1529), else ``None``.

    The OpenCTI and OpenAEV readers record one signal per source issue with the
    remediation key the case rules group it on (``ctem_cases.remediation_of``),
    so the signals of one fix open or join one case, the case a recompute
    built before signals included.
    """
    fix = evidence.get("case_fix")
    if not isinstance(fix, dict):
        return None
    key = str(fix.get("key") or "").strip()
    kind = str(fix.get("kind") or "").strip()
    title = str(fix.get("title") or "").strip()
    if not key or not title or kind not in REMEDIATION_KINDS:
        return None
    fixed = str(fix.get("fixed_version") or "").strip()[:100] or None
    return CaseFix(
        key=_bounded_key(key),
        kind=kind,
        title=title[:COLUMN_MAX],
        fixed_version=fixed,
        fixed_versions=platform_fixes_of(fix.get("fixed_versions")),
    )


#: Most platforms a case keeps a fix for (the evidence is source data, bounded).
MAX_FIXED_PLATFORMS = 12


def platform_fixes_of(raw: Any) -> dict[str, str]:
    """``{platform: version}`` read from evidence, bounded and cleaned (#1572); ``{}`` when it holds none."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for platform, version in raw.items():
        p, v = str(platform or "").strip()[:80], str(version or "").strip()[:100]
        if p and v and len(out) < MAX_FIXED_PLATFORMS:
            out[p] = v
    return out


def severity_of(decision: dict[str, Any] | None) -> tuple[str | None, int | None, list[str]]:
    """Severity, SLA hours and risk scenario ids read from a scope gate decision record."""
    decision = decision or {}
    severity = decision.get("severity")
    severity = severity if severity in DEFAULT_SLA_HOURS else None
    hours = decision.get("sla_hours")
    if not isinstance(hours, int) or hours <= 0:
        hours = DEFAULT_SLA_HOURS.get(severity) if severity else None
    scope = decision.get("scope") or {}
    scenarios = [str(s.get("id")) for s in scope.get("risk_scenarios") or [] if isinstance(s, dict) and s.get("id")]
    return severity, hours, scenarios


_SEVERITY_RANK = {"P1": 0, "P2": 1, "P3": 2}


def severity_rank(severity: str | None) -> int:
    """0 for P1 (most urgent) to 2 for P3; 9 for no severity."""
    return _SEVERITY_RANK.get(severity or "", 9)


def case_data(case: CtemCase) -> dict[str, Any]:
    """The payload of a case event: the case id and its routing, nothing else (#933).

    Severity, SLA, owner, risk scenarios, the signal and the validation stay on
    the case, read with ``get_ctem_case`` under the run's own view. The event
    reaches only subscribers whose run-as user sees the case
    (``platform_events.can_read_object``).
    """
    return {
        "case_id": str(case.id),
        "vertical": case.vertical,
        "state": case.state,
        "trigger_path": case.trigger_path,
        "object_type": "ctem_case",
        "object_id": str(case.id),
        "participant_agent_ids": [],
    }


def opened_data(case: CtemCase) -> dict[str, Any]:
    """The payload of ``ctem_case.opened``: :func:`case_data` dated at the case's creation.

    The event is raised after the writer's commit, which can come long after the case was
    written (a long source read, the scheduled backfill): ``occurred_at`` stays the creation time
    (ctem-case-opened-event-backfill-delay), the dispatch time is the run's own ``triggered_at``.
    """
    created = rules.as_utc(getattr(case, "created_at", None)) or rules.as_utc(getattr(case, "state_since", None))
    data = case_data(case)
    if created is not None:
        data["occurred_at"] = created.isoformat()
    return data


def _event_id(name: str, case: CtemCase, marker: str) -> str:
    digest = hashlib.sha256(f"{name}\n{case.id}\n{marker}".encode()).hexdigest()[:32]
    return f"ctem-case-{digest}"


#: The stable event id of a case event, shared with the case store (``ctem_cases_store``).
case_event_id = _event_id


def _event(case_id: uuid.UUID, actor: str, kind: str, at: datetime, **kw: Any) -> CtemCaseEvent:
    return CtemCaseEvent(case_id=case_id, actor=actor, kind=kind, at=at, **kw)


def _is_postgres(db: AsyncSession) -> bool:
    """Whether *db* runs on PostgreSQL, the only database the advisory locks exist on."""
    get_bind = getattr(db, "get_bind", None)
    if get_bind is None:
        return False
    try:
        return bool(get_bind().dialect.name == "postgresql")
    except Exception:  # a session with no bind: nothing to serialise against
        return False


async def hold_key_lock(db: AsyncSession, organization_id: uuid.UUID, vertical: str, key: str) -> None:
    """Take the transaction-scoped advisory lock of one case key (#2558), PostgreSQL only.

    Two transactions that open the case of one fix (a listener delivery, a drain pass, a source
    recompute) meet here: the second waits for the first's commit, then finds its case and joins
    it, so one fix never opens two cases. Released at the commit or the rollback.
    """
    if not _is_postgres(db):
        return
    from sqlalchemy import text

    lock = f"ctem-case-key:{organization_id}:{vertical}:{canonical_key(key)}"
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": lock})


async def _open_case_for_key(
    db: AsyncSession, organization_id: uuid.UUID, vertical: str, key: str, aliases: tuple[str, ...] = ()
) -> CtemCase | None:
    rows = list(
        (
            await db.execute(
                select(CtemCase).where(
                    CtemCase.organization_id == organization_id,
                    CtemCase.vertical == vertical,
                    CtemCase.state.in_(OPEN_STATES),
                )
            )
        ).scalars()
    )
    for case in rows:
        if key in (case.keys or []):
            return case
    # #2558: the same CVE under another spelling (its STIX id, a name) joins the open case and adds the key.
    wanted = {canonical_key(key), *aliases}
    for case in rows:
        if any(k in wanted or canonical_key(k) in wanted for k in case.keys or []):
            case.keys = [*(case.keys or []), key]
            return case
    # The same fix of the same product under another spelling (vendor present or absent, label case)
    # joins the open case and adds its key there, so one fix never holds two SLAs (#2037).
    for case in rows:
        if any(rules.same_product_key(key, k) for k in case.keys or []):
            case.keys = [*(case.keys or []), key]
            return case
    return None


async def open_from_signal(
    db: AsyncSession,
    signal: CtemSignal,
    *,
    vertical: str = "cyber",
    events: PendingEvents | None = None,
    now: datetime | None = None,
) -> CtemCase | None:
    """Open (or join) the case of a confirmed, in-scope signal; ``None`` when the signal opens none.

    While an administrator paused case opening for *vertical* (#1951) it opens and joins nothing:
    the signal stays confirmed with ``case_id`` null. The switch is read here, fresh, whatever the
    caller checked before (#2536): the single choke point no automatic path can pass.

    The caller commits, then calls :func:`publish_pending` with *events*.
    """
    decision = signal.decision or {}
    if signal.state != "confirmed" or decision.get("outcome") != "case" or signal.case_id is not None:
        return None
    from app.services import ctem_case_opening as case_opening

    if not await case_opening.is_on(db, vertical):
        return None
    now = now or datetime.now(UTC)
    fix = fix_of(signal)
    if nothing_to_upgrade(signal, fix):
        # #2047: no CVE, no affected range, no fixed version: a vendor fix made service-side leaves nothing to
        # install, so no upgrade case opens; the signal is kept and parked for a person to validate.
        signal.state = "to_validate"
        signal.decision = {**decision, "outcome": "review", "state": "to_validate", "reason": NOTHING_TO_UPGRADE}
        return None
    severity, hours, scenarios = severity_of(decision)
    # #2558: the lookup and the insert under the key's lock, so two openings of one fix meet.
    await hold_key_lock(db, signal.organization_id, vertical, fix.key)
    case = await _open_case_for_key(db, signal.organization_id, vertical, fix.key, fix.aliases)
    if case is None and not await case_opening.take_opening_token(vertical):
        # #2570: the per-minute opening budget of the vertical is spent: the signal stays confirmed
        # with no case and opens on a next pass (the paced pass of the autonomous tick, the sweep).
        return None
    created = case is None
    if case is None:
        case = CtemCase(
            id=uuid.uuid4(),
            organization_id=signal.organization_id,
            vertical=vertical,
            title=fix.title,
            remediation_kind=fix.kind if fix.kind in REMEDIATION_KINDS else "manual",
            keys=[fix.key],
            pinned_issues=[],
            state="new",
            state_since=now,
            trigger_path=signal.trigger_path,
            risk_scenario_ids=scenarios,
            severity=severity,
            sla_due_at=now + timedelta(hours=hours) if hours else None,
            due_at=now + timedelta(hours=hours) if hours else None,
            tickets=[],
            validations=[],
            validation_plan=signal.trigger_path in ("proven_exploitable", "mitigation_regression"),
            fixed_version=fix.fixed_version,
            fixed_versions=dict(fix.fixed_versions),
            product_label=_product_label(signal),
            created_at=now,
        )
        db.add(case)
        await db.flush()
        db.add(_event(case.id, rules.SYSTEM_ACTOR, "created", now, to_value=fix.key, reason=f"signal {signal.id}"))
        # #2560: the majority asset group's owner, else a primary asset's, the default, the CTEM decider;
        # never the device user. The reason and the contributors go on the owner event.
        from app.services import ctem_case_owner

        owner, how, contributors = await ctem_case_owner.owner_for_signal(db, signal, case)
        if owner is not None:
            case.owner_user_id = owner
            route = ctem_case_owner.Route(owner, how, contributors)
            db.add(ctem_case_owner.owner_event(case.id, route, now=now, actor=rules.SYSTEM_ACTOR))
    else:
        # A second signal on the same fix: the most urgent severity and the earliest SLA win.
        if severity and (case.severity is None or _SEVERITY_RANK[severity] < _SEVERITY_RANK.get(case.severity, 9)):
            case.severity = severity
        if hours:
            due = now + timedelta(hours=hours)
            current = rules.as_utc(case.sla_due_at)
            if current is None or current > due:
                case.sla_due_at = due
        case.risk_scenario_ids = sorted(set(case.risk_scenario_ids or []) | set(scenarios))
    _link(db, signal, case, vertical=vertical, events=events, now=now, created=created)
    if created and await _resolved_on_inventory(db, case, events=events, now=now):
        return case
    if created and events is not None:
        events.add(CASE_OPENED, opened_data(case), _event_id(CASE_OPENED, case, "open"))
    return case


async def _resolved_on_inventory(
    db: AsyncSession, case: CtemCase, *, events: PendingEvents | None, now: datetime
) -> bool:
    """Resolve a new upgrade case the inventory already shows fixed on every asset, before the loop starts.

    The installed versions are read when the case opens, not after the review: a
    case whose every asset runs the fixed version or later is not affected, so it
    closes on that deterministic check (``fixed``, with the inventory as evidence)
    and raises ``ctem_case.resolved`` instead of ``ctem_case.opened``. No threat
    package is drafted and no gate reaches a person. An unknown or uncomparable
    version, a platform without its fix, or a case that needs its validation
    proof keeps the case open (doubt moves forward, #1606).
    """
    if case.remediation_kind != "upgrade" or not case.fixed_version or case.validation_plan:
        return False
    from app.services import ctem_case_versions as case_versions

    running = await case_versions.for_case(db, case)
    if not case_versions.all_on_fixed(running):
        return False
    assets = int((running or {}).get("assets_on_fixed") or 0)
    evidence = f"{rules.FIXED_IN_INVENTORY}: {assets} asset(s) on {case.fixed_version} or later"
    db.add(_transition(case, "resolved", now, reason="not affected at opening", evidence=evidence[:500]))
    case.resolution_kind = "fixed"
    if events is not None:
        events.add(CASE_RESOLVED, case_data(case), _event_id(CASE_RESOLVED, case, "open"))
        events.frame(case, "state", now)
    return True


async def join_case(
    db: AsyncSession,
    signal: CtemSignal,
    case: CtemCase,
    *,
    events: PendingEvents | None = None,
    now: datetime | None = None,
) -> bool:
    """Join a confirmed, in-scope signal to the case that already holds its fix, whatever the case's state (#1529).

    A source reader's signal about an issue a case already groups joins that
    case instead of opening a second one; ``False`` when the signal opens no case,
    and while case opening is paused for the case's vertical (#2536).
    """
    decision = signal.decision or {}
    if signal.state != "confirmed" or decision.get("outcome") != "case" or signal.case_id is not None:
        return False
    from app.services import ctem_case_opening as case_opening

    if not await case_opening.is_on(db, case.vertical):
        return False
    _link(db, signal, case, vertical=case.vertical, events=events, now=now or datetime.now(UTC), created=False)
    return True


def _link(
    db: AsyncSession,
    signal: CtemSignal,
    case: CtemCase,
    *,
    vertical: str,
    events: PendingEvents | None,
    now: datetime,
    created: bool,
) -> None:
    db.add(CtemCaseIssue(case_id=case.id, source=SIGNAL_SOURCE, external_id=str(signal.id), open=True))
    db.add(_event(case.id, rules.SYSTEM_ACTOR, "signal", now, issue=f"{SIGNAL_SOURCE}:{signal.id}"))
    signal.state = "case_opened"
    signal.case_id = case.id
    signal.updated_at = now
    if events is not None:
        events.verticals.add(vertical)
        events.frame(case, "created" if created else "signal", now)


def _product_label(signal: CtemSignal) -> str | None:
    """The product a source reader's fix names (the inventory version check reads it, #1252)."""
    evidence = getattr(signal, "evidence", None)
    fix = evidence.get("case_fix") if isinstance(evidence, dict) else None
    label = str(fix.get("product_label") or "").strip() if isinstance(fix, dict) else ""
    return label[:200] or None


async def locked_case(db: AsyncSession, case_id: uuid.UUID) -> CtemCase | None:
    """The case row read locked until the caller's commit (#2373), its attributes refreshed.

    Every write of a case reads it this way before it decides from its state:
    a concurrent write of the same case (a tool call, the API, a loop pass)
    waits for this one's commit, then reads the state it left, so a ``state``
    event never carries a ``from`` the case had already left.
    """
    return await db.get(CtemCase, case_id, with_for_update=True, populate_existing=True)


def _transition(
    case: CtemCase,
    target: str,
    at: datetime,
    *,
    reason: str | None = None,
    evidence: str | None = None,
    actor: str = rules.SYSTEM_ACTOR,
) -> CtemCaseEvent:
    row = _event(case.id, actor, "state", at, from_value=case.state, to_value=target, reason=reason, evidence=evidence)
    case.state = target
    case.state_since = at
    if target not in ("accepted_risk",):
        case.accepted_reason = None
        case.accepted_until = None
    return row


#: The asset groups one validation reference names in its run order (CTEM-12-UI).
RUN_ORDER_MAX = 20
#: The remediation kinds a version inventory resolves (a patch or an upgrade): a test never does.
VERSION_REMEDIATIONS: tuple[str, ...] = ("patch", "upgrade")
#: What a validation reference proves, as the agent says it: the fix in place (a version inventory), or a test.
REMEDIATION_EVIDENCE = "remediation"
VALIDATION_EVIDENCE = "validation"
#: A passive read that confirms the exposure (a version inventory below the fixed build, a configuration read)
#: (#2118): written with ``failed`` (still exposed) or ``pending``, never with a pass.
EXPOSURE_EVIDENCE = "exposure"
EVIDENCE_TYPES: tuple[str, ...] = (REMEDIATION_EVIDENCE, VALIDATION_EVIDENCE, EXPOSURE_EVIDENCE)


def _upsert_validation(validations: list[dict[str, Any]], entry: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """*validations* with *entry* written once per ``(provider, ref)`` (#1692), and whether it repeats.

    The newest entry of the same reference is replaced when it is pending or
    says the same outcome (the second answer is a repeat); any other outcome
    (a pass after a fail, a re-test) is a new entry, so the history keeps it.
    """
    same = [
        i
        for i, v in enumerate(validations)
        if isinstance(v, dict) and v.get("provider") == entry["provider"] and v.get("ref") == entry["ref"]
    ]
    if not same:
        return [*validations, entry], False
    last = validations[same[-1]]
    repeat = (
        last.get("result") == entry["result"]
        and last.get("result") != "pending"
        and bool(last.get("mitigation_only")) == bool(entry.get("mitigation_only"))
    )
    if last.get("result") != "pending" and not repeat:
        return [*validations, entry], False
    rest = [v for i, v in enumerate(validations) if i != same[-1]]
    return [*rest, entry], repeat


async def record_validation(
    db: AsyncSession,
    case: CtemCase,
    *,
    provider: str,
    kind: str,
    ref: str,
    result: str,
    mitigation_only: bool = False,
    note: str | None = None,
    run_order: list[str] | None = None,
    conclusive: bool | None = None,
    evidence_type: str | None = None,
    actor: str = rules.SYSTEM_ACTOR,
    events: PendingEvents | None = None,
    now: datetime | None = None,
) -> CtemCase:
    """Write a validation reference on *case* and move it: a pass proves, a fail reopens.

    ``result``: ``passed`` (the exposure is gone), ``not_exposed`` (it never
    was), ``failed`` (still exposed) or ``pending``. ``mitigation_only``: the
    pass proves a mitigation, not the permanent fix, so the case goes to
    ``mitigated_watching`` and the re-test stays on. ``run_order``: the target
    asset groups in the order they were tested (at most 20 names); a name that
    resolves to one canonical asset attaches it to the case (``assets``, provenance
    ``name_match_inferred``), one that does not is kept in ``unresolved`` (#2395). ``conclusive``
    (#1458), set by the agent that read the check: false when a pass proves
    nothing (the case stays open with its remediation), true when the check
    proves the exposure gone, None when the writer did not say (a scanner
    closing the finding at source). The platform never derives it from what the
    inject is (#1810). ``evidence_type`` (#1500):
    ``remediation`` (the fix in place: a version inventory showing every
    in-scope endpoint at or above the fixed build) or ``validation`` (a
    detection or behaviour test) or ``exposure`` (#2118: a passive read that
    confirms the case affected, written as ``failed``; ``conclusive`` then says
    the read proves its claim). A pass that proves nothing is stored as
    ``inconclusive`` (``reported_result`` keeps the word given), in the entry
    and the history event (#1692); one entry per ``(provider, ref)`` replaces a
    pending one, and the same outcome repeated only refreshes it. A patch or upgrade case resolves only on
    remediation evidence; when the writer does not say, a provider re-scan and
    a conclusive check (the fixed version read) count as remediation, anything
    else as validation, recorded and never resolving. The caller commits.
    """
    if result not in VALIDATION_RESULTS:
        raise rules.CaseRuleError(f"unknown validation result {result!r}")
    if not (provider or "").strip() or not (ref or "").strip():
        raise rules.CaseRuleError("a validation reference needs its provider and its id")
    now = now or datetime.now(UTC)
    at = now.isoformat()
    entry: dict[str, Any] = {
        "provider": provider.strip()[:40],
        "kind": (kind or "validation").strip()[:40],
        "ref": ref.strip()[:300],
        "result": result,
        "at": at,
        "mitigation_only": bool(mitigation_only),
        "note": (note or "").strip()[:500] or None,
    }
    if evidence_type is not None and evidence_type not in EVIDENCE_TYPES:
        raise rules.CaseRuleError(f"unknown evidence type {evidence_type!r}")
    if evidence_type == EXPOSURE_EVIDENCE and result in rules.PASSING_RESULTS:
        raise rules.CaseRuleError(
            "exposure evidence confirms the exposure: write it as failed (still exposed), never as a pass"
        )
    if (case.remediation_kind or "") in VERSION_REMEDIATIONS:
        if evidence_type is None:
            proven = conclusive is True or entry["kind"] == RESCAN_KIND
            evidence_type = REMEDIATION_EVIDENCE if proven else VALIDATION_EVIDENCE
        entry["evidence_type"] = evidence_type
        if evidence_type == VALIDATION_EVIDENCE and result in rules.PASSING_RESULTS:
            # A detection or behaviour test never resolves a patch or upgrade case (#1500).
            conclusive = False
    elif evidence_type is not None:
        entry["evidence_type"] = evidence_type
    if conclusive is not None:
        entry["conclusive"] = bool(conclusive)
    order = [str(name).strip()[:120] for name in (run_order or []) if str(name).strip()][:RUN_ORDER_MAX]
    if order:
        entry["run_order"] = order
        from app.services import ctem_case_assets as case_assets

        # The hosts the validation ran on become case assets with their provenance (#2395).
        resolved = await case_assets.resolve_names(db, getattr(case, "organization_id", None), order)
        if resolved is not None:
            case_assets.attach_run_order(entry, order, resolved, actor=actor, at=at)
    if result in rules.PASSING_RESULTS and conclusive is False:
        # A pass that proves nothing is stored, shown and measured as inconclusive (#1692).
        entry["result"] = INCONCLUSIVE_RESULT
        entry["reported_result"] = result
    stored = entry["result"]
    kept, repeat = _upsert_validation(case.validations or [], entry)
    case.validations = kept[-50:]
    case.validation_plan = True
    if repeat:
        # The same outcome on the same reference again: the entry is refreshed, nothing moves (#1692).
        return case
    db.add(_event(case.id, actor, "validation", now, to_value=stored, evidence=f"{entry['provider']}:{entry['ref']}"))
    evidence = f"validation {entry['provider']} {entry['ref']}: {stored}"
    if result in rules.PASSING_RESULTS and conclusive is False:
        pass  # inconclusive: recorded, the case keeps its state and its remediation (#1458)
    elif result in rules.PASSING_RESULTS:
        if mitigation_only and case.state in (*OPEN_STATES, "resolved"):
            if case.state != "mitigated_watching":
                db.add(_transition(case, "mitigated_watching", now, evidence=evidence, actor=actor))
                case.resolution_kind = None
        elif case.state in (*OPEN_STATES, "mitigated_watching"):
            db.add(_transition(case, "resolved", now, evidence=evidence, actor=actor))
            case.resolution_kind = "not_exposed" if result == "not_exposed" else "fixed"
            if events is not None:
                events.add(CASE_RESOLVED, case_data(case), _event_id(CASE_RESOLVED, case, at))
    elif result == "failed" and case.state in ("resolved", "mitigated_watching"):
        db.add(
            _transition(case, case_status.reopen_target(case), now, reason=f"re-test failed: {evidence}", actor=actor)
        )
        case.resolution_kind = None
        if events is not None:
            events.add(CASE_REOPENED, case_data(case), _event_id(CASE_REOPENED, case, at))
    else:
        # A pending re-test puts the case in validation; a failed one on an open case back to remediation (#1503).
        moved = case_status.after_validation(case, now, result=result, actor=actor, evidence=evidence)
        if moved is not None:
            db.add(moved)
    if events is not None:
        events.frame(case, "validation", now)
    return case


#: What a ticket reference's status may say, mapped from the ITSM's own workflow by the writer.
TICKET_STATUSES: tuple[str, ...] = ("open", "in_progress", "done", "cancelled")
#: Where a synced ticket stands against its case (#917): written by ``services/ctem_ticketing.py``.
TICKET_SYNC_STATES: tuple[str, ...] = ("linked", "in_sync", "out_of_date", "error")


def record_ticket(
    db: AsyncSession,
    case: CtemCase,
    *,
    integration: str,
    key: str,
    url: str | None = None,
    status: str = "open",
    actor: str = rules.SYSTEM_ACTOR,
    now: datetime | None = None,
    sync: dict[str, Any] | None = None,
) -> CtemCase:
    """Write or update a ticket reference on *case* (#891).

    A ticket in flight moves an open case to ``remediating``; a ticket done on a
    case with a re-test plan moves it to ``validating`` (#1503). One entry per
    ``(integration, key)``: a second write of the same ticket updates its status
    and url in place, so a rerun never lists a ticket twice. The case closes on
    proof (a validation reference), not on a ticket. The caller commits.

    *sync* (#917) is the sync record of a ticket the sync layer manages:
    ``{state, since, error, fix, provider, correlation, external_status}``; it
    replaces the stored one and alone never raises a ``ticket`` event.
    """
    integration = (integration or "").strip()[:80]
    key = (key or "").strip()[:120]
    if not integration or not key:
        raise rules.CaseRuleError("a ticket reference needs its integration and its key")
    if status not in TICKET_STATUSES:
        raise rules.CaseRuleError(f"unknown ticket status {status!r}: one of {', '.join(TICKET_STATUSES)}")
    now = now or datetime.now(UTC)
    entry: dict[str, Any] = {
        "integration": integration,
        "key": key,
        "url": (url or "").strip()[:500] or None,
        "status": status,
        "at": now.isoformat(),
    }
    if sync is not None:
        state = sync.get("state")
        if state not in TICKET_SYNC_STATES:
            raise rules.CaseRuleError(f"unknown ticket sync state {state!r}: one of {', '.join(TICKET_SYNC_STATES)}")
        entry["sync"] = {k: v for k, v in sync.items() if v not in (None, "")}
    tickets = [dict(t) for t in (case.tickets or [])]
    previous = next((t for t in tickets if (t.get("integration"), t.get("key")) == (integration, key)), None)
    if previous is not None:
        same_status = previous.get("status") == status and (entry["url"] in (None, previous.get("url")))
        if "sync" not in entry and previous.get("sync"):
            entry["sync"] = previous["sync"]
        if same_status and entry.get("sync") == previous.get("sync"):
            return case
        entry["url"] = entry["url"] or previous.get("url")
        if same_status:
            entry["at"] = previous.get("at", entry["at"])
            case.tickets = [entry if t is previous else t for t in tickets][-20:]
            return case
        tickets = [entry if t is previous else t for t in tickets]
    else:
        tickets.append(entry)
    case.tickets = tickets[-20:]
    db.add(
        _event(
            case.id,
            actor,
            "ticket",
            now,
            from_value=previous.get("status") if previous else None,
            to_value=status,
            evidence=f"{integration}:{key}",
        )
    )
    moved = case_status.after_ticket(case, now, status=status, actor=actor, evidence=f"{integration}:{key}")
    if moved is not None:
        db.add(moved)
    return case


#: The exposure classes OpenCRQ's topology rule reads (CTEM-24); anything else is ``unknown``.
EXPOSURES: tuple[str, ...] = ("internet_facing", "internal", "unknown")
#: Supporting assets one exploitation write keeps (the tool lists 30 a read).
EXPLOITATION_MAX = 30
_TECHNIQUES_MAX = 12
#: An ATT&CK technique id, alone or leading a label ("T1190 Exploit Public-Facing Application").
_TECHNIQUE_ID = re.compile(r"^(T\d{4}(?:\.\d{3})?)\b")


def _exposure(value: object) -> str:
    word = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return word if word in EXPOSURES else "unknown"


def _techniques(value: object) -> list[str]:
    out: list[str] = []
    for item in value if isinstance(value, list) else []:
        match = _TECHNIQUE_ID.match(str(item or "").strip().upper())
        if match and match.group(1) not in out:
            out.append(match.group(1))
    return out[:_TECHNIQUES_MAX]


def record_exploitation(
    db: AsyncSession,
    case: CtemCase,
    entries: list[dict[str, Any]],
    *,
    actor: str = rules.SYSTEM_ACTOR,
    now: datetime | None = None,
) -> CtemCase:
    """Write how the CVE would be exploited on the scenario's assets (CTEM-24, #936).

    *entries* is the triage step's ``exploitation`` array (from
    ``get_opencrq_cve_exploitation``): per supporting asset, the ATT&CK
    techniques in attack order, the exposure and the reading. The list is
    replaced whole (the newest read is the truth); an entry with no valid
    technique is dropped. One ``exploitation`` event when it changed, its
    evidence the technique ids alone (the history is served to every viewer,
    the asset names are not). A new case moves to ``analyzing`` (#1503). The
    caller commits.
    """
    now = now or datetime.now(UTC)
    kept: list[dict[str, Any]] = []
    for raw in entries if isinstance(entries, list) else []:
        if not isinstance(raw, dict):
            continue
        techniques = _techniques(raw.get("techniques"))
        asset = str(raw.get("asset") or "").strip()[:200]
        if not techniques or not asset:
            continue
        kept.append(
            {
                "risk_scenario_id": str(raw.get("risk_scenario_id") or "").strip()[:100] or None,
                "asset": asset,
                "exposure": _exposure(raw.get("exposure")),
                "techniques": techniques,
                "reading": str(raw.get("reading") or "").strip()[:500],
            }
        )
    if len(kept) > EXPLOITATION_MAX:
        kept = kept[:EXPLOITATION_MAX]
    if not kept:
        raise rules.CaseRuleError("an exploitation reading needs at least one asset with an ATT&CK technique id")
    previous = [{k: v for k, v in e.items() if k != "at"} for e in (case.exploitation or [])]
    if previous == kept:
        return case
    case.exploitation = [{**e, "at": now.isoformat()} for e in kept]
    ids = sorted({t for e in kept for t in e["techniques"]})
    db.add(
        _event(
            case.id,
            actor,
            "exploitation",
            now,
            from_value=str(len(previous)) if previous else None,
            to_value=str(len(kept)),
            evidence=", ".join(ids)[:300],
        )
    )
    moved = case_status.after_triage(case, now, actor=actor, what="exploitation reading")
    if moved is not None:
        db.add(moved)
    return case


#: Hops and reached assets one attack-path write keeps (the tool lists 40 a read).
ATTACK_PATH_MAX = 40
#: Primary assets behind one attack-path write keeps.
ATTACK_PATH_PRIMARY_MAX = 20
#: How a hop is known: shown by an OpenAEV execution, or inferred (CTEM-23).
HOP_NATURES: tuple[str, ...] = ("proven", "inferred")


def _asset_name(value: object) -> str:
    return str(value or "").strip()[:200]


#: A simulation page a hop links to: an absolute http(s) URL, nothing else (UX round 1 on #1296, B2).
_HOP_URL = re.compile(r"^https?://[^\s/?#]+[^\s]*$")


def _hop_url(value: object) -> str | None:
    url = str(value or "").strip()[:500]
    return url if _HOP_URL.match(url) else None


def _hop_date(value: object) -> str | None:
    """The ISO date of the execution that showed a hop, or None when it is not one."""
    text = str(value or "").strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat() if text else None
    except ValueError:
        return None


def record_attack_path(
    db: AsyncSession,
    case: CtemCase,
    reading: dict[str, Any],
    *,
    actor: str = rules.SYSTEM_ACTOR,
    now: datetime | None = None,
) -> CtemCase:
    """Write what a finding on the affected asset reaches (CTEM-23, #1289).

    *reading* is the triage step's ``attack_path`` object (from
    ``get_opencrq_attack_path_reach``): ``origin`` {asset, exposure}, ``hops``
    [{from, to, nature, simulation, simulation_name, simulation_url, seen_at}]
    nearest first (the simulation id is kept for the trace, never drawn: its
    name, page and date are), ``reached`` [{asset, depth,
    criticality}] and ``primary`` (the primary asset names behind). Replaced
    whole (the newest read is the truth); a hop with no end is dropped, a
    nature other than proven is inferred. An origin with no hop is a reading
    too: no attack path leaves the asset. One ``attack_path`` event when it
    changed, its evidence the counts alone (the history is served to every
    viewer, the asset names are not). Never moves the case state. The caller
    commits.
    """
    now = now or datetime.now(UTC)
    raw: dict[str, Any] = reading if isinstance(reading, dict) else {}

    def _list(key: str) -> list[Any]:
        value = raw.get(key)
        return value if isinstance(value, list) else []

    origin_value = raw.get("origin")
    origin_raw: dict[str, Any] = origin_value if isinstance(origin_value, dict) else {}
    origin = _asset_name(origin_raw.get("asset"))
    if not origin:
        raise rules.CaseRuleError("an attack-path reading needs its origin asset")
    hops: list[dict[str, Any]] = []
    for hop in _list("hops"):
        if not isinstance(hop, dict):
            continue
        source, target = _asset_name(hop.get("from")), _asset_name(hop.get("to"))
        if not source or not target:
            continue
        nature = str(hop.get("nature") or "").strip().lower()
        hops.append(
            {
                "from": source,
                "to": target,
                "nature": nature if nature in HOP_NATURES else "inferred",
                "simulation": str(hop.get("simulation") or "").strip()[:100] or None,
                "simulation_name": str(hop.get("simulation_name") or "").strip()[:200] or None,
                "simulation_url": _hop_url(hop.get("simulation_url")),
                "seen_at": _hop_date(hop.get("seen_at")),
            }
        )
    reached: list[dict[str, Any]] = []
    for asset in _list("reached"):
        if not isinstance(asset, dict) or not _asset_name(asset.get("asset")):
            continue
        depth = asset.get("depth")
        reached.append(
            {
                "asset": _asset_name(asset.get("asset")),
                "depth": depth if isinstance(depth, int) and not isinstance(depth, bool) and depth > 0 else None,
                "criticality": str(asset.get("criticality") or "").strip().lower()[:20] or None,
            }
        )
    primary: list[str] = []
    for name in _list("primary"):
        label = _asset_name(name)
        if label and label not in primary:
            primary.append(label)
    hops, reached, primary = hops[:ATTACK_PATH_MAX], reached[:ATTACK_PATH_MAX], primary[:ATTACK_PATH_PRIMARY_MAX]
    kept: dict[str, Any] = {
        "origin": {"asset": origin, "exposure": _exposure(origin_raw.get("exposure"))},
        "hops": hops,
        "reached": reached,
        "primary": primary,
    }
    previous = {k: v for k, v in (case.attack_path or {}).items() if k != "at"}
    if previous == kept:
        return case
    case.attack_path = {**kept, "at": now.isoformat()}
    proven = sum(1 for h in hops if h["nature"] == "proven")
    db.add(
        _event(
            case.id,
            actor,
            "attack_path",
            now,
            from_value=str(len(previous.get("hops") or [])) if previous else None,
            to_value=str(len(hops)),
            evidence=f"{len(hops)} hop(s), {proven} proven, {len(reached)} reached, {len(primary)} primary",
        )
    )
    moved = case_status.after_triage(case, now, actor=actor, what="attack path")
    if moved is not None:
        db.add(moved)
    return case


#: What happened at a gate of the fleet's approval catalog (CTEM-34, #916).
GATE_OUTCOMES: tuple[str, ...] = ("passed_automatically", "waited", "answered")


def record_gate(
    db: AsyncSession,
    case: CtemCase,
    *,
    key: str,
    outcome: str,
    by: str = "",
    option: str = "",
    actor: str = rules.SYSTEM_ACTOR,
    now: datetime | None = None,
) -> CtemCase:
    """Write a loop gate on the case's history (CTEM-34, #916): passed automatically, waited or answered.

    *key* is a gate of the CTEM catalog (``builtin_fleet_docs.CTEM_GATE_KEYS``);
    *by* names what let it run (the autonomy level or the pin) or who answered;
    *option* the option chosen. One ``gate`` event: ``from`` the key, ``to`` the
    outcome, ``reason`` the by, ``evidence`` the option, so the loop trace shows
    every gate, never hides an automatic pass. The gate also moves the case
    status (``ctem_case_status.after_gate``, #1503). The caller commits.
    """
    from app.agents.builtin_fleet_docs import CTEM_GATE_KEYS

    key = (key or "").strip()
    if key not in CTEM_GATE_KEYS:
        raise rules.CaseRuleError(f"unknown gate {key!r}: one of {', '.join(CTEM_GATE_KEYS)}")
    if outcome not in GATE_OUTCOMES:
        raise rules.CaseRuleError(f"unknown gate outcome {outcome!r}: one of {', '.join(GATE_OUTCOMES)}")
    db.add(
        _event(
            case.id,
            actor,
            "gate",
            now or datetime.now(UTC),
            from_value=key,
            to_value=outcome,
            reason=(by or "").strip()[:300] or None,
            evidence=(option or "").strip()[:300] or None,
        )
    )
    # The gate moves the case status (#1503): waited parks it on a person, the answer moves it on.
    moved = case_status.after_gate(case, now or datetime.now(UTC), key=key, outcome=outcome, by=by, actor=actor)
    if moved is not None:
        db.add(moved)
    return case


#: How an applied change was carried out (#2061): the ``kind`` of an ``applied_change`` event.
APPLIED_CHANGE_KINDS: tuple[str, ...] = ("ioc_prevention", "detection_rule", "configuration", "control_restored")


def record_applied_change(
    db: AsyncSession,
    case: CtemCase,
    change: dict[str, Any],
    *,
    actor: str = rules.SYSTEM_ACTOR,
    now: datetime | None = None,
) -> CtemCase:
    """Write a change applied through a bound integration under ``ctem.production_change`` (#2061).

    *change* is ``{integration, kind, pushed, ids, rollback}``: the integration that
    carried it, one of :data:`APPLIED_CHANGE_KINDS`, what was pushed (the indicators
    with their prevention action, scope and expiry; the rule; the setting), the ids
    the integration returned and how to revert it. One ``applied_change`` event:
    ``from`` the integration, ``to`` the kind, ``reason`` what was pushed,
    ``evidence`` ``{ids, rollback}`` as JSON. The rollback is required: a change
    nobody can revert is refused. An open case moves to ``remediating``. The
    gate itself is written by :func:`record_gate`. The caller commits.
    """
    import json

    if not isinstance(change, dict):
        raise rules.CaseRuleError("applied_change must be an object with integration, kind, pushed and rollback")
    integration = str(change.get("integration") or "").strip()[:300]
    kind = str(change.get("kind") or "").strip()
    pushed = str(change.get("pushed") or "").strip()[:4000]
    rollback = str(change.get("rollback") or "").strip()[:2000]
    if kind not in APPLIED_CHANGE_KINDS:
        raise rules.CaseRuleError(f"applied_change.kind must be one of {', '.join(APPLIED_CHANGE_KINDS)}")
    if not integration or not pushed or not rollback:
        raise rules.CaseRuleError("applied_change needs the integration, what was pushed and the rollback")
    raw_ids = change.get("ids") or []
    if not isinstance(raw_ids, list):
        raise rules.CaseRuleError("applied_change.ids must be the list of ids the integration returned")
    ids = [str(i).strip()[:200] for i in raw_ids if str(i).strip()][:200]
    at = now or datetime.now(UTC)
    db.add(
        _event(
            case.id,
            actor,
            "applied_change",
            at,
            from_value=integration,
            to_value=kind,
            reason=pushed,
            evidence=json.dumps({"ids": ids, "rollback": rollback}),
        )
    )
    moved = case_status.after_applied_change(case, at, actor=actor, evidence=f"{kind} via {integration}")
    if moved is not None:
        db.add(moved)
    return case


#: An OpenCRQ treatment plan page: ``https://host/<tenant>/treatment-plans/<uuid>`` (CTEM-18-UI, #1068).
_PLAN_URL = re.compile(r"^https?://[^\s/?#]+(?:/[A-Za-z0-9._-]+)*/treatment-plans/([0-9a-fA-F-]{36})$")


def record_treatment_plan(
    db: AsyncSession,
    case: CtemCase,
    plan: dict[str, Any],
    previous: dict[str, Any] | None = None,
    *,
    actor: str = rules.SYSTEM_ACTOR,
    now: datetime | None = None,
) -> CtemCase:
    """Attach the OpenCRQ treatment plan a remediation decision created to the case (CTEM-18-UI, #1068).

    *plan* is ``{id, name, url}`` as ``create_opencrq_treatment_plan`` answered it:
    the id a UUID, the url the plan's OpenCRQ page for that id. One
    ``treatment_plan`` event: ``to`` the plan id, ``reason`` the name,
    ``evidence`` the url; the view serves the name and the url only to a viewer
    who reads OpenCRQ. *previous* is the plan already attached (the newest
    event): the same plan again writes nothing. A new plan moves an open case to
    ``remediating`` (#1503). The caller commits.
    """
    if not isinstance(plan, dict):
        raise rules.CaseRuleError("treatment_plan must be an object with id, name and url")
    plan_id = str(plan.get("id") or "").strip().lower()
    try:
        uuid.UUID(plan_id)
    except ValueError:
        raise rules.CaseRuleError("treatment_plan.id must be the OpenCRQ plan UUID") from None
    url = str(plan.get("url") or "").strip()
    match = _PLAN_URL.match(url) if url else None
    if url and (match is None or match.group(1).lower() != plan_id):
        raise rules.CaseRuleError("treatment_plan.url must be the OpenCRQ page of that plan (its treatment-plans page)")
    name = str(plan.get("name") or "").strip()[:300]
    if (
        previous
        and previous.get("id") == plan_id
        and previous.get("name") == (name or None)
        and previous.get("url") == (url or None)
    ):
        return case
    db.add(
        _event(
            case.id,
            actor,
            "treatment_plan",
            now or datetime.now(UTC),
            from_value=(previous or {}).get("id"),
            to_value=plan_id,
            reason=name or None,
            evidence=url or None,
        )
    )
    moved = case_status.after_remediation_plan(case, now or datetime.now(UTC), actor=actor, evidence=url or plan_id)
    if moved is not None:
        db.add(moved)
    return case


#: An OpenCTI object id: its internal UUID or its STIX id ``grouping--<uuid>`` (CTEM-16-UI, #1115).
_GROUPING_ID = re.compile(
    r"^(?:grouping--)?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
#: An OpenCTI Grouping page: ``https://host/dashboard/analyses/groupings/<uuid>``.
_GROUPING_URL = re.compile(
    r"^https?://[^\s/?#]+(?:/[A-Za-z0-9._-]+)*/groupings/([0-9a-fA-F-]{36})(?:/[A-Za-z0-9._-]*)?$"
)
#: An OpenCTI draft page: ``https://host/dashboard/data/import/draft/<uuid>`` (#1453). A Grouping
#: drafted inside a draft has no live page until the draft is validated: its link is the draft's.
_DRAFT_URL = re.compile(
    r"^https?://[^\s/?#]+(?:/[A-Za-z0-9._-]+)*/data/import/draft/[0-9a-fA-F-]{36}(?:/[A-Za-z0-9._-]*)?$"
)


def record_threat_package(
    db: AsyncSession,
    case: CtemCase,
    package: dict[str, Any],
    previous: dict[str, Any] | None = None,
    *,
    actor: str = rules.SYSTEM_ACTOR,
    now: datetime | None = None,
) -> CtemCase:
    """Attach the threat package the review step drafted to the case (CTEM-16-UI, #1115).

    *package* is ``{grouping_id, name, url, coverage_id}``: the OpenCTI Grouping
    the orchestrator drafted (``create_opencti_container``), its name, its
    OpenCTI page (the draft holding it while the package is drafted, #1453;
    the Grouping page once the draft is validated), and the security coverage requested on it
    (``manage_opencti_security_coverage`` action ``create``), when requested.
    One ``threat_package`` event: ``to`` the Grouping id, ``reason`` the name,
    ``evidence`` the url, ``from`` the coverage id; the view serves the name,
    the url and the ids only to a viewer who reads OpenCTI. *previous* is the
    package already attached: the same package again writes nothing. Never
    moves the case state. The caller commits.
    """
    if not isinstance(package, dict):
        raise rules.CaseRuleError("threat_package must be an object with grouping_id, name, url and coverage_id")
    grouping_id = str(package.get("grouping_id") or "").strip()
    found = _GROUPING_ID.match(grouping_id)
    if found is None:
        raise rules.CaseRuleError(
            "threat_package.grouping_id must be the OpenCTI Grouping id (its UUID, or its STIX id: grouping-- then the UUID)"
        )
    url = str(package.get("url") or "").strip()
    match = _GROUPING_URL.match(url) if url else None
    drafted = bool(url) and _DRAFT_URL.match(url) is not None
    if url and not drafted and (match is None or match.group(1).lower() != found.group(1).lower()):
        raise rules.CaseRuleError(
            "threat_package.url must be the OpenCTI draft page holding the package while it is drafted "
            "(/dashboard/data/import/draft/ then the draft id), or the Grouping page once the draft is validated"
        )
    name = str(package.get("name") or "").strip()[:300]
    coverage_id = str(package.get("coverage_id") or "").strip()[:300]
    stored = {"grouping_id": grouping_id, "name": name or None, "url": url or None, "coverage_id": coverage_id or None}
    if previous and all(previous.get(k) == v for k, v in stored.items()):
        return case
    db.add(
        _event(
            case.id,
            actor,
            "threat_package",
            now or datetime.now(UTC),
            from_value=stored["coverage_id"],
            to_value=grouping_id,
            reason=stored["name"],
            evidence=stored["url"],
        )
    )
    moved = case_status.after_triage(case, now or datetime.now(UTC), actor=actor, what="threat package")
    if moved is not None:
        db.add(moved)
    return case


#: The validation kind written when the source closed every member of a case (CTEM-25).
RESCAN_KIND = "rescan"


async def close_signal_members(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    provider: str,
    external_id: str,
    events: PendingEvents | None = None,
    now: datetime | None = None,
) -> int:
    """The source closed its object: close the case members of the signals it recorded for it.

    How many members were closed. A case whose last open member closes is
    resolved on a passing provider re-scan reference (:func:`record_validation`,
    kind :data:`RESCAN_KIND`), so the proof rule and the events stay those of
    any validation. A case already closed, or with members still open, only
    records the closed member. The caller commits.
    """
    if not (provider or "").strip() or not (external_id or "").strip():
        return 0
    now = now or datetime.now(UTC)
    signals = (
        (
            await db.execute(
                select(CtemSignal).where(
                    CtemSignal.organization_id == organization_id,
                    CtemSignal.source_provider == provider,
                    CtemSignal.source_external_id == external_id,
                    CtemSignal.case_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    closed = 0
    for signal in signals:
        case_id = signal.case_id
        if case_id is None:
            continue
        member = (
            await db.execute(
                select(CtemCaseIssue).where(
                    CtemCaseIssue.case_id == case_id,
                    CtemCaseIssue.source == SIGNAL_SOURCE,
                    CtemCaseIssue.external_id == str(signal.id),
                    CtemCaseIssue.open.is_(True),
                )
            )
        ).scalar_one_or_none()
        if member is None:
            continue
        member.open = False
        closed += 1
        db.add(
            _event(
                case_id,
                rules.SYSTEM_ACTOR,
                "issue_closed",
                now,
                issue=f"{SIGNAL_SOURCE}:{signal.id}",
                reason=f"closed at source: {provider} {external_id}"[:300],
            )
        )
        await db.flush()
        case = await locked_case(db, case_id)
        if case is None or case.state not in OPEN_STATES:
            continue
        still_open = (
            await db.execute(
                select(CtemCaseIssue.id).where(CtemCaseIssue.case_id == case.id, CtemCaseIssue.open.is_(True)).limit(1)
            )
        ).first()
        if still_open is None:
            await record_validation(
                db,
                case,
                provider=provider,
                kind=RESCAN_KIND,
                ref=external_id,
                result="passed",
                note="closed at source",
                events=events,
                now=now,
            )
    return closed


def breached(case: CtemCase, now: datetime) -> bool:
    """True the first time an open case is past its SLA (the escalation is raised once)."""
    due = rules.as_utc(case.sla_due_at)
    return case.state in OPEN_STATES and due is not None and due <= now and case.sla_breached_at is None


async def sweep(
    db: AsyncSession,
    *,
    vertical: str = "cyber",
    events: PendingEvents | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """The scheduled pass: open the cases of waiting confirmed signals, close the cases of retired assets,
    escalate SLA breaches. The caller commits.

    While an administrator paused case opening (#1951, services/ctem_case_opening.py) the
    waiting signals stay confirmed with no case; the SLA, announce and stall passes still run.
    """
    from app.services import ctem_case_opening as case_opening

    now = now or datetime.now(UTC)
    counts = {
        "opened": 0,
        "joined": 0,
        "skipped": 0,
        "sla_breached": 0,
        "announced": 0,
        "stalled": 0,
        "retired": 0,
        "owned": 0,
        "merged": 0,
    }
    # #2558: every writer that takes several key locks (this sweep, a drain pass, a recompute) holds the
    # vertical's writer lock first, so their key locks never cross; a listener takes one key lock only.
    # The try form (#2555): when a drain pass or a recompute holds it, the sweep never waits on it; the
    # opening and duplicate passes wait for the next tick and the other passes run now.
    writer = not _is_postgres(db) or await case_opening.try_vertical_lock(db, vertical)
    if not writer:
        logger.info(
            "CTEM sweep of %s: the writer lock is busy, opening and duplicate merge wait for the next tick", vertical
        )
    if writer and await case_opening.may_open_waiting(db, vertical):
        await open_waiting(db, vertical=vertical, events=events, now=now, counts=counts)
    # #2452, #2560: an open case never waits with no owner, and the system-set owners are re-routed
    # to the asset group owner once (capped batch, no platform event).
    from app.services.ctem_case_owner import backfill_owners

    counts["owned"] = await backfill_owners(db, now=now, actor=rules.SYSTEM_ACTOR)
    # ctem-autoresolve-archived-asset: a case on an asset the inventory archived or removed closes, its
    # decisions cancelled, before the SLA pass escalates it.
    from app.services.ctem_case_asset_retire import close_retired_asset_cases

    counts["retired"] = await close_retired_asset_cases(db, events=events, now=now)
    rows = (
        (
            await db.execute(
                select(CtemCase).where(
                    CtemCase.state.in_(OPEN_STATES),
                    CtemCase.sla_due_at.is_not(None),
                    CtemCase.sla_due_at <= now,
                    CtemCase.sla_breached_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for case in rows:
        if not breached(case, now):
            continue
        case.sla_breached_at = now
        db.add(_event(case.id, rules.SYSTEM_ACTOR, "sla_breached", now, to_value=case.severity, reason="SLA breached"))
        counts["sla_breached"] += 1
        if events is not None:
            events.add(CASE_SLA_BREACHED, case_data(case), _event_id(CASE_SLA_BREACHED, case, "sla"))
            events.frame(case, "sla_breached", now)
    if writer:
        counts["merged"] = await merge_open_duplicates(db, vertical=vertical, events=events, now=now)
    counts["announced"] = await announce_unannounced(db, events=events, now=now)
    # #1736: an open case no gate and no reminder waits on is raised once per stall window.
    from app.services.ctem_case_stall import sweep_stalled

    counts["stalled"] = await sweep_stalled(db, events=events, now=now, actor=rules.SYSTEM_ACTOR)
    return counts


async def open_waiting(
    db: AsyncSession,
    *,
    vertical: str,
    events: PendingEvents | None,
    now: datetime,
    counts: dict[str, int],
) -> bool:
    """Open the cases of up to ``SWEEP_BATCH`` waiting confirmed signals, oldest sighting first; adds to *counts*.

    True when the pass stopped on the per-minute opening budget (#2570,
    ``ctem_case_opening.take_opening_token``): the rest waits for the next pass.

    The caller checked :func:`ctem_case_opening.may_open_waiting` and commits. The switch is read
    again before every signal: an administrator who pauses case opening mid-pass stops the pass at
    the next signal, not after the batch (#2536).
    """
    from app.services import ctem_case_opening as case_opening

    waiting = (
        (
            await db.execute(
                select(CtemSignal)
                .where(CtemSignal.state == "confirmed", CtemSignal.case_id.is_(None))
                .order_by(CtemSignal.last_seen_at.asc())
                .limit(SWEEP_BATCH)
            )
        )
        .scalars()
        .all()
    )
    for signal in waiting:
        if not await case_opening.is_on(db, vertical):
            logger.info("CTEM case opening paused for %s mid-pass; the rest of the batch waits", vertical)
            break
        if await case_opening.budget_left(vertical) == 0:
            logger.info("CTEM case opening of %s: the per-minute budget is spent; the rest waits", vertical)
            return True
        before = len(events.items) if events is not None else 0
        frames_before = len(events.frames) if events is not None else 0
        # Read before the savepoint: its rollback expires the signal, and a lazy refresh on an
        # AsyncSession raises MissingGreenlet inside the handler, which would stop the whole pass.
        signal_id = signal.id
        # One savepoint per signal: a signal that cannot be opened is skipped, never the whole pass.
        try:
            async with db.begin_nested():
                case = await open_from_signal(db, signal, vertical=vertical, events=events, now=now)
        except Exception:
            if events is not None:
                del events.items[before:]
                del events.frames[frames_before:]
            counts["skipped"] += 1
            logger.warning("CTEM case not opened for signal %s; skipped this sweep", signal_id, exc_info=True)
            continue
        if case is None:
            continue
        opened = events is not None and len(events.items) > before
        counts["opened" if opened else "joined"] += 1
    return False


def duplicate_groups(cases: list[Any]) -> list[list[Any]]:
    """The open cases that hold one fix, grouped, oldest first, groups of two or more only (#2558, pure).

    Two cases hold one fix when they share a key once each key is spelled canonically
    (:func:`canonical_key`); sharing is transitive, so three cases joined two by two are one group.
    """
    parent: dict[int, int] = {i: i for i in range(len(cases))}

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for index, case in enumerate(cases):
        for key in case.keys or []:
            canonical = canonical_key(key)
            if canonical in owner:
                parent[root(index)] = root(owner[canonical])
            else:
                owner[canonical] = index
    groups: dict[int, list[Any]] = {}
    for index, case in enumerate(cases):
        groups.setdefault(root(index), []).append(case)

    def age(case: Any) -> tuple[datetime, str]:
        created = rules.as_utc(getattr(case, "created_at", None)) or datetime.max.replace(tzinfo=UTC)
        return created, str(case.id)

    return [sorted(g, key=age) for g in groups.values() if len(g) > 1]


async def merge_open_duplicates(
    db: AsyncSession, *, vertical: str, events: PendingEvents | None, now: datetime | None = None
) -> int:
    """Fold every open case of a fix another open case already holds into the oldest one; how many folded (#2558).

    The repair of the cases two concurrent openings wrote before the key lock
    (:func:`hold_key_lock`): ``ctem_cases_store.merge_cases`` moves the keys, members,
    history and signals to the survivor, which keeps its state, owner and SLA, and
    records a system ``merge`` event. The caller commits.
    """
    from app.services import ctem_cases_store as store

    now = now or datetime.now(UTC)
    rows = list(
        (await db.execute(select(CtemCase).where(CtemCase.vertical == vertical, CtemCase.state.in_(OPEN_STATES))))
        .scalars()
        .all()
    )
    by_org: dict[uuid.UUID, list[CtemCase]] = {}
    for case in rows:
        by_org.setdefault(case.organization_id, []).append(case)
    merged = 0
    for organization_id, cases in by_org.items():
        for group in duplicate_groups(cases):
            survivor, others = group[0], group[1:]
            try:
                async with db.begin_nested():
                    for key in sorted({canonical_key(k) for c in group for k in c.keys or []}):
                        await hold_key_lock(db, organization_id, vertical, key)
                    await store.merge_cases(
                        db,
                        organization_id=organization_id,
                        vertical=vertical,
                        target_id=survivor.id,
                        source_ids=[c.id for c in others],
                        actor=rules.SYSTEM_ACTOR,
                        reason=DUPLICATE_MERGE_REASON,
                        now=now,
                    )
            except Exception:
                logger.warning(
                    "CTEM duplicate cases of %s not merged; the next sweep retries", survivor.id, exc_info=True
                )
                continue
            merged += len(others)
            logger.info("CTEM case %s took %s duplicate open case(s) of its fix", survivor.id, len(others))
            if events is not None:
                events.verticals.add(vertical)
                events.frame(survivor, "merge", now)
    return merged


async def announce_unannounced(db: AsyncSession, *, events: PendingEvents | None, now: datetime | None = None) -> int:
    """Raise ``ctem_case.opened`` once for every open case never announced; answers how many. The caller commits.

    The backfill of the cases a source read or a manual split wrote before
    they raised their own event (dev-docs/ctem-cases.md section 4.3): a case
    is announced when it holds an ``announced`` event, or a ``created`` event
    from a signal (``open_from_signal`` raised it). Idempotent: the written
    ``announced`` event keeps the case out of every later pass.
    """
    if events is None:
        return 0
    from sqlalchemy import exists, or_

    from app.services import ctem_cases_store as store

    done = exists().where(
        CtemCaseEvent.case_id == CtemCase.id,
        or_(
            CtemCaseEvent.kind == store.ANNOUNCED,
            (CtemCaseEvent.kind == "created") & CtemCaseEvent.reason.like("signal %"),
        ),
    )
    rows = (
        (
            await db.execute(
                select(CtemCase)
                .where(CtemCase.state.in_(OPEN_STATES), ~done)
                .order_by(CtemCase.created_at.asc())
                .limit(ANNOUNCE_BATCH)
            )
        )
        .scalars()
        .all()
    )
    for case in rows:
        store.announce(db, case, events, now or datetime.now(UTC), reason="backfill")
    return len(rows)


#: The cadence of the scheduled sweep when ``CTEM_CASE_SWEEP_SECONDS`` is not set.
DEFAULT_SWEEP_SECONDS = 3600


def sweep_seconds() -> int:
    """``CTEM_CASE_SWEEP_SECONDS`` (at least 60), else hourly."""
    import os

    try:
        value = int(os.environ.get("CTEM_CASE_SWEEP_SECONDS") or DEFAULT_SWEEP_SECONDS)
    except ValueError:
        value = DEFAULT_SWEEP_SECONDS
    return max(60, value)


async def sweep_due() -> bool:
    """Take the sweep slot for this cadence window; True when this tick runs it (or Redis is unavailable)."""
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        return bool(await client.set("ctem:cases:sweep", "1", nx=True, ex=sweep_seconds()))
    except Exception:  # no Redis: every tick sweeps; the sweep is idempotent
        return True


async def publish_pending(events: PendingEvents | None) -> int:
    """Drop the stale viewer views, then raise the live frames and the collected case events (after the commit); how many events were published.

    The views go first: a web app that refetches on a live frame reads the case at once.
    """
    if not events:
        return 0
    if events.verticals:
        from app.services.ctem_cases_view import forget_all_views

        for vertical in sorted(events.verticals):
            await forget_all_views(vertical)
        events.verticals.clear()
    if events.frames:
        from app.services.ctem_loop_stream import publish_case_updates

        frames, events.frames = events.frames, []
        await publish_case_updates(frames)
    if not events.items:
        return 0
    from app.services.ctem_case_routing import stamp_owners
    from app.services.platform_events import publish

    # Exactly one fleet hears each case event: its owner, computed now (#1390).
    await stamp_owners(data for _name, data, _eid in events.items)
    published = 0
    for name, data, event_id in events.items:
        if await publish(name, data, event_id=event_id):
            published += 1
    events.items.clear()
    return published
