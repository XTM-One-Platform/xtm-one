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
  cases of confirmed signals still waiting, escalates every SLA breach once.

Every opening, reopen, resolve and SLA breach raises a platform event
(:data:`CASE_EVENTS`) once the caller has committed (:func:`publish_pending`),
so the seeded fleets can start a run from a case.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_case import (
    OPEN_STATES,
    REMEDIATION_KINDS,
    VALIDATION_RESULTS,
    CtemCase,
    CtemCaseEvent,
    CtemCaseIssue,
)
from app.models.ctem_signal import CtemSignal
from app.services import ctem_cases as rules

logger = logging.getLogger(__name__)

#: The platform events a case raises (registered in ``platform_events.PLATFORM_EVENTS``).
CASE_OPENED = "ctem_case.opened"
CASE_REOPENED = "ctem_case.reopened"
CASE_RESOLVED = "ctem_case.resolved"
CASE_SLA_BREACHED = "ctem_case.sla_breached"
CASE_EVENTS: tuple[str, ...] = (CASE_OPENED, CASE_REOPENED, CASE_RESOLVED, CASE_SLA_BREACHED)

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

#: Most signals the sweep opens cases for in one pass.
SWEEP_BATCH = 500
#: The member source of a signal attached to a case.
SIGNAL_SOURCE = "signal"
_KEY_SPACES = re.compile(r"\s+")


@dataclass
class CaseFix:
    """The fix a signal asks for: one case per key."""

    key: str
    kind: str
    title: str


@dataclass
class PendingEvents:
    """Platform events to raise after the caller's commit, in order."""

    items: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)

    def add(self, name: str, data: dict[str, Any], event_id: str) -> None:
        self.items.append((name, data, event_id))


def _norm(value: str | None) -> str:
    return _KEY_SPACES.sub(" ", (value or "").strip().lower())


def fix_of(signal: CtemSignal) -> CaseFix:
    """The remediation a signal opens: a CVE or software subject keys on the product, else on path and subject.

    A second signal about the same subject and path lands on the same key and
    joins the open case (one case per fix).
    """
    path = signal.trigger_path
    subject = (signal.subject_label or signal.subject_ref or "").strip()
    kind = PATH_REMEDIATION.get(path, "manual")
    if signal.subject_kind == "software" and kind == "patch":
        return CaseFix(
            key=f"upgrade:-/{_norm(signal.subject_ref)}@latest",
            kind="upgrade",
            title=f"Upgrade {subject} to a fixed version",
        )
    key = f"{kind}:{signal.subject_kind}/{_norm(signal.subject_ref)}"
    return CaseFix(key=key, kind=kind, title=_TITLES.get(kind, "Remediate {subject}").format(subject=subject)[:300])


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


def case_data(case: CtemCase, **extra: Any) -> dict[str, Any]:
    """The payload of a case event: ids and facts only, never a source issue's content (no title)."""
    data = {
        "case_id": str(case.id),
        "organization_id": str(case.organization_id),
        "vertical": case.vertical,
        "state": case.state,
        "remediation_kind": case.remediation_kind,
        "trigger_path": case.trigger_path,
        "severity": case.severity,
        "sla_due_at": case.sla_due_at.isoformat() if case.sla_due_at else None,
        "owner_user_id": str(case.owner_user_id) if case.owner_user_id else None,
        "risk_scenario_ids": list(case.risk_scenario_ids or []),
        "object_type": "ctem_case",
        "object_id": str(case.id),
        "participant_agent_ids": [],
    }
    data.update(extra)
    return data


def _event_id(name: str, case: CtemCase, marker: str) -> str:
    digest = hashlib.sha256(f"{name}\n{case.id}\n{marker}".encode()).hexdigest()[:32]
    return f"ctem-case-{digest}"


def _event(case_id: uuid.UUID, actor: str, kind: str, at: datetime, **kw: Any) -> CtemCaseEvent:
    return CtemCaseEvent(case_id=case_id, actor=actor, kind=kind, at=at, **kw)


async def _open_case_for_key(db: AsyncSession, organization_id: uuid.UUID, vertical: str, key: str) -> CtemCase | None:
    rows = (
        await db.execute(
            select(CtemCase).where(
                CtemCase.organization_id == organization_id,
                CtemCase.vertical == vertical,
                CtemCase.state.in_(OPEN_STATES),
            )
        )
    ).scalars()
    for case in rows:
        if key in (case.keys or []):
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

    The caller commits, then calls :func:`publish_pending` with *events*.
    """
    decision = signal.decision or {}
    if signal.state != "confirmed" or decision.get("outcome") != "case" or signal.case_id is not None:
        return None
    now = now or datetime.now(UTC)
    fix = fix_of(signal)
    severity, hours, scenarios = severity_of(decision)
    case = await _open_case_for_key(db, signal.organization_id, vertical, fix.key)
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
            state="needs_attention",
            state_since=now,
            trigger_path=signal.trigger_path,
            risk_scenario_ids=scenarios,
            severity=severity,
            sla_due_at=now + timedelta(hours=hours) if hours else None,
            due_at=now + timedelta(hours=hours) if hours else None,
            tickets=[],
            validations=[],
            validation_plan=signal.trigger_path in ("proven_exploitable", "mitigation_regression"),
        )
        db.add(case)
        await db.flush()
        db.add(_event(case.id, rules.SYSTEM_ACTOR, "created", now, to_value=fix.key, reason=f"signal {signal.id}"))
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
    db.add(CtemCaseIssue(case_id=case.id, source=SIGNAL_SOURCE, external_id=str(signal.id), open=True))
    db.add(_event(case.id, rules.SYSTEM_ACTOR, "signal", now, issue=f"{SIGNAL_SOURCE}:{signal.id}"))
    signal.state = "case_opened"
    signal.case_id = case.id
    signal.updated_at = now
    if created and events is not None:
        events.add(CASE_OPENED, case_data(case, signal_id=str(signal.id)), _event_id(CASE_OPENED, case, "open"))
    return case


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
    actor: str = rules.SYSTEM_ACTOR,
    events: PendingEvents | None = None,
    now: datetime | None = None,
) -> CtemCase:
    """Write a validation reference on *case* and move it: a pass proves, a fail reopens.

    ``result``: ``passed`` (the exposure is gone), ``not_exposed`` (it never
    was), ``failed`` (still exposed) or ``pending``. ``mitigation_only``: the
    pass proves a mitigation, not the permanent fix, so the case goes to
    ``mitigated_watching`` and the re-test stays on. The caller commits.
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
    case.validations = [*(case.validations or []), entry][-50:]
    case.validation_plan = True
    db.add(_event(case.id, actor, "validation", now, to_value=result, evidence=f"{entry['provider']}:{entry['ref']}"))
    evidence = f"validation {entry['provider']} {entry['ref']}: {result}"
    if result in rules.PASSING_RESULTS:
        if mitigation_only and case.state in (*OPEN_STATES, "resolved"):
            if case.state != "mitigated_watching":
                db.add(_transition(case, "mitigated_watching", now, evidence=evidence, actor=actor))
                case.resolution_kind = None
        elif case.state in (*OPEN_STATES, "mitigated_watching"):
            db.add(_transition(case, "resolved", now, evidence=evidence, actor=actor))
            case.resolution_kind = "not_exposed" if result == "not_exposed" else "fixed"
            if events is not None:
                events.add(
                    CASE_RESOLVED, case_data(case, validation=entry), _event_id(CASE_RESOLVED, case, at)
                )
    elif result == "failed" and case.state in ("resolved", "mitigated_watching"):
        db.add(_transition(case, "needs_attention", now, reason=f"re-test failed: {evidence}", actor=actor))
        case.resolution_kind = None
        if events is not None:
            events.add(CASE_REOPENED, case_data(case, validation=entry), _event_id(CASE_REOPENED, case, at))
    return case


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
    """The scheduled pass: open the cases of waiting confirmed signals, escalate SLA breaches. The caller commits."""
    now = now or datetime.now(UTC)
    counts = {"opened": 0, "joined": 0, "sla_breached": 0}
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
        before = len(events.items) if events is not None else 0
        case = await open_from_signal(db, signal, vertical=vertical, events=events, now=now)
        if case is None:
            continue
        opened = events is not None and len(events.items) > before
        counts["opened" if opened else "joined"] += 1
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
    return counts


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
    """Raise the collected case events (after the commit); how many were published."""
    if not events or not events.items:
        return 0
    from app.services.platform_events import publish

    published = 0
    for name, data, event_id in events.items:
        if await publish(name, data, event_id=event_id):
            published += 1
    events.items.clear()
    return published


def validation_matches(case: CtemCase, refs: Iterable[str]) -> bool:
    """Whether *case* already carries a validation reference among *refs* (an OpenAEV event updates it)."""
    wanted = {r for r in refs if r}
    return any(isinstance(v, dict) and v.get("ref") in wanted for v in case.validations or [])
