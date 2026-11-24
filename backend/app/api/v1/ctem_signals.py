# SPDX-License-Identifier: Apache-2.0

"""CTEM signals API (#826): record, list and read the changes in exposure that start the loop.

XTM One mode only, no model call. ``POST`` records a signal for the caller,
idempotent on its dedupe key (the same key again answers ``created: false``
with ``last_seen_at`` moved). An administrator reads every signal of the
organization; anyone else reads the signals recorded for them. The rules are
``services/ctem_signals.py``; the design ``dev-docs/ctem-signals.md``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.config import settings
from app.core.database import get_db
from app.models.ctem_signal import LENSES, SIGNAL_STATES, SUBJECT_KINDS, TRIGGER_PATHS
from app.models.user import User
from app.services import ctem_case_loop as case_loop
from app.services import ctem_scope_gate_store as scope_gate
from app.services import ctem_signals as signals
from app.services.ctem_loop_stream import publish_signal_update
from app.services.organizations import get_primary

router = APIRouter()

TriggerPath = Literal[TRIGGER_PATHS]  # type: ignore[valid-type]
Lens = Literal[LENSES]  # type: ignore[valid-type]
SignalState = Literal[SIGNAL_STATES]  # type: ignore[valid-type]
SubjectKind = Literal[SUBJECT_KINDS]  # type: ignore[valid-type]


class SignalSource(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    external_id: str | None = Field(default=None, max_length=300)


class SignalSubject(BaseModel):
    kind: SubjectKind
    ref: str = Field(min_length=1, max_length=300)
    label: str | None = Field(default=None, max_length=300)


class SignalCreate(BaseModel):
    """One change in exposure; the lens defaults to the path's usual one."""

    trigger_path: TriggerPath
    lens: Lens | None = None
    source: SignalSource
    subject: SignalSubject
    observed_at: datetime | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str | None = Field(default=None, max_length=200)


def _require_xtm_one() -> None:
    if settings.is_copilot_like:
        raise HTTPException(status_code=404, detail="CTEM signals are only available in XTM One mode")


def _reader(user: User) -> str | None:
    """``None`` reads the organization (an administrator); else the caller's own signals."""
    return None if getattr(user, "is_admin", False) else str(user.id)


async def _organization_id(db: AsyncSession) -> uuid.UUID:
    primary = await get_primary(db)
    if primary is None:
        raise HTTPException(status_code=404, detail="No organization set up yet")
    return primary.id


@router.get("/paths")
async def list_ctem_signal_paths(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The 12 trigger paths, their default lens, whether a listener feeds them, and the signals the caller sees per path.

    Each path also names its sources and whether each is bound for the caller,
    its status, and its signals of the last 30 days (#1189, the signal sources dialog).
    """
    _require_xtm_one()
    from app.services.ctem_signal_sources import bound_sources

    primary = await get_primary(db)
    counts: dict[str, dict[str, int]] = {}
    activity: dict[str, dict[str, Any]] = {}
    if primary is not None:
        reader = _reader(user)
        counts = await signals.counts_by_path(db, organization_id=primary.id, recorded_by=reader)
        since = datetime.now(UTC) - timedelta(days=30)
        activity = await signals.activity_by_path(db, organization_id=primary.id, recorded_by=reader, since=since)
    bound = await bound_sources(db, user.id)
    return {"paths": signals.path_catalog(counts, bound=bound, activity=activity)}


@router.get("")
async def list_ctem_signals(
    state: SignalState | None = Query(default=None),
    trigger_path: TriggerPath | None = Query(default=None),
    lens: Lens | None = Query(default=None),
    case_id: uuid.UUID | None = Query(default=None),
    source_provider: str | None = Query(default=None, max_length=40),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=signals.MAX_PAGE),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One page of signals, newest sighting first, with the total and the counts by state.

    ``source_provider`` keeps one provider; ``since`` keeps the signals last seen at or after it (#1782).
    """
    _require_xtm_one()
    primary = await get_primary(db)
    if primary is None:
        return {"total": 0, "counts": {s: 0 for s in SIGNAL_STATES}, "signals": []}
    rows, total, counts = await signals.list_signals(
        db,
        organization_id=primary.id,
        recorded_by=_reader(user),
        state=state,
        trigger_path=trigger_path,
        lens=lens,
        case_id=case_id,
        source_provider=source_provider,
        since=since if since is None or since.tzinfo else since.replace(tzinfo=UTC),
        limit=limit,
        offset=offset,
    )
    return {"total": total, "counts": counts, "signals": [signals.to_dict(r) for r in rows]}


@router.post("")
async def record_ctem_signal(
    body: SignalCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Record one signal for the caller; the same dedupe key again moves ``last_seen_at``."""
    _require_xtm_one()
    organization_id = await _organization_id(db)
    try:
        row, created = await signals.record_signal(
            db,
            organization_id=organization_id,
            signal=signals.SignalInput(
                trigger_path=body.trigger_path,
                lens=body.lens or "",
                source_provider=body.source.provider,
                source_external_id=body.source.external_id,
                subject_kind=body.subject.kind,
                subject_ref=body.subject.ref,
                subject_label=body.subject.label,
                observed_at=body.observed_at,
                evidence=body.evidence,
                dedupe_key=body.dedupe_key,
            ),
            recorded_via="api",
            recorded_by=str(user.id),
        )
    except signals.SignalError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    pending = await scope_gate.evaluate_after_record(db, row, created=created)
    await db.commit()
    await case_loop.publish_pending(pending)
    await publish_signal_update(db, row)
    return {"created": created, "signal": signals.to_dict(row)}


@router.post("/evaluate")
async def evaluate_ctem_signals(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Run the reconcile and scope gate again on every open signal (after a policy change); counts by state."""
    _require_xtm_one()
    pending = case_loop.PendingEvents()
    counts = await scope_gate.evaluate_open(
        db, organization_id=await _organization_id(db), user_id=admin.id, events=pending
    )
    await db.commit()
    await case_loop.publish_pending(pending)  # ctem_signal.triaged per changed verdict (#1409)
    return {"evaluated": sum(counts.values()), "counts": counts}


@router.get("/attack-surface/passes")
async def list_attack_surface_passes(
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """The latest attack surface passes of the outside lens (#1498), newest first (administrators).

    Each pass names its providers, what each read covered (pages, records,
    truncated) per owned domain or range, the hosts observed and the signals per
    path, including a pass that found nothing.
    """
    _require_xtm_one()
    from app.services import ctem_attack_surface as surface

    rows = await surface.list_passes(db, await _organization_id(db), limit=limit)
    return {"passes": [surface.pass_to_dict(r) for r in rows]}


class SurfaceRepair(BaseModel):
    #: ``true`` (default) answers what the repair would discard and writes nothing.
    dry_run: bool = True


@router.post("/attack-surface/repair-new-hosts")
async def repair_attack_surface_new_hosts(
    body: SurfaceRepair,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Discard the path 7 new-host signals raised without a complete previous read (#1498).

    One-off repair, administrators: the hosts stay in the baseline; a signal
    that opened a case is left to the case step. ``dry_run`` answers the hosts.
    """
    _require_xtm_one()
    from app.services import ctem_attack_surface as surface

    result = await surface.repair_partial_read_new_hosts(db, await _organization_id(db), dry_run=body.dry_run)
    # A dry run writes nothing, so the commit is a no-op there; committing on every path
    # keeps the writes visible before the response is sent.
    await db.commit()
    return result


@router.post("/{signal_id}/evaluate")
async def evaluate_ctem_signal(
    signal_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Run the reconcile and scope gate again on one signal, OpenCRQ risk scenarios read with its
    recorder's rights (a system signal: the administrator's, #933); 409 once it opened a case."""
    _require_xtm_one()
    row = await signals.get_signal(db, signal_id, organization_id=await _organization_id(db), recorded_by=None)
    if row is None:
        raise HTTPException(status_code=404, detail="No such signal")
    pending = case_loop.PendingEvents()
    decision = await scope_gate.evaluate_one(db, row, evaluator_id=admin.id, events=pending)
    if decision is None:
        raise HTTPException(status_code=409, detail="This signal opened a case: the case step owns it now")
    await db.commit()
    await case_loop.publish_pending(pending)  # ctem_signal.triaged when the verdict changed (#1409)
    return signals.to_dict(row)


class TriageFilter(BaseModel):
    """Every signal the caller sees in one state (and trigger path), as the Signals page filters them."""

    state: SignalState | None = None
    trigger_path: TriggerPath | None = None


class SignalTriage(BaseModel):
    """Dismiss, restore or re-check signals (#1505): by id, or every signal a filter matches."""

    action: Literal[scope_gate.TRIAGE_ACTIONS]  # type: ignore[valid-type]
    ids: list[uuid.UUID] = Field(default_factory=list, max_length=scope_gate.MAX_BULK)
    filter: TriageFilter | None = None


@router.post("/triage")
async def triage_ctem_signals(
    body: SignalTriage,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Dismiss, restore or re-check the signals the caller may read (#1505), at most 2000 per call.

    Anyone who reads signals triages the ones they read (an administrator: the
    organization's). A signal that opened a case is skipped. A confirmed signal
    opens its case at the next sweep, so a bulk re-check never floods cases.
    """
    _require_xtm_one()
    if bool(body.ids) == (body.filter is not None):
        raise HTTPException(status_code=422, detail="Give either ids or a filter")
    organization_id = await _organization_id(db)
    rows = await signals.select_signals(
        db,
        organization_id=organization_id,
        recorded_by=_reader(user),
        ids=body.ids or None,
        state=body.filter.state if body.filter else None,
        trigger_path=body.filter.trigger_path if body.filter else None,
        limit=scope_gate.MAX_BULK,
    )
    actor = (getattr(user, "name", None) or getattr(user, "email", None) or str(user.id))[:200]
    counts = await scope_gate.triage(db, rows, action=body.action, actor=actor, evaluator_id=user.id)
    await db.commit()
    return {"matched": len(rows), "counts": counts}


class SignalInventoryAdd(BaseModel):
    """Add the subjects of unmatched signals to the inventory (#1505): by id, or every signal a filter matches."""

    ids: list[uuid.UUID] = Field(default_factory=list, max_length=scope_gate.MAX_BULK)
    filter: TriageFilter | None = None


@router.post("/add-to-inventory")
async def add_ctem_signals_to_inventory(
    body: SignalInventoryAdd,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Declare the subjects of the selected unmatched signals in the organization profile, then decide them again.

    An administrator only: it writes one profile version (a host becomes an
    asset, a software a software product, an identity its email domain as an
    owned domain). Only unmatched (``to_validate``) signals count; the others
    are left as they are. A signal the gate confirms opens its case at the
    next sweep, never one case per signal here. 422 when the profile would
    not validate.
    """
    _require_xtm_one()
    if bool(body.ids) == (body.filter is not None):
        raise HTTPException(status_code=422, detail="Give either ids or a filter")
    organization_id = await _organization_id(db)
    rows = await signals.select_signals(
        db,
        organization_id=organization_id,
        recorded_by=None,
        ids=body.ids or None,
        state=body.filter.state if body.filter else None,
        trigger_path=body.filter.trigger_path if body.filter else None,
        limit=scope_gate.MAX_BULK,
    )
    actor = (getattr(admin, "name", None) or getattr(admin, "email", None) or str(admin.id))[:200]
    try:
        result = await scope_gate.add_to_inventory(
            db, rows, organization_id=organization_id, admin_id=admin.id, actor=actor
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await db.commit()
    return {"matched": len(rows), **result}


#: The scope gate outcomes a scope proposal answers: the subject's criticality was known and below the threshold.
SCOPE_PROPOSAL_CODES = frozenset({"below_scope_threshold"})


@router.post("/{signal_id}/propose-scope")
async def propose_ctem_signal_scope(
    signal_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Propose to lower the scope threshold to this watchlisted signal's criticality (CTEM-30, #937).

    Stores one organization context proposal and nothing else: the profile
    changes only when an administrator accepts it in Settings > Organization.
    It is a suggestion, never a gate. 404 for a signal the caller cannot read,
    409 when the signal is not on the watchlist below the threshold.
    """
    _require_xtm_one()
    from app.services import organization_context_proposals as proposals

    row = await signals.get_signal(db, signal_id, organization_id=await _organization_id(db), recorded_by=_reader(user))
    if row is None:
        raise HTTPException(status_code=404, detail="No such signal")
    decision = row.decision or {}
    criticality = ((decision.get("scope") or {}).get("criticality")) or ""
    if row.state != "watchlist" or decision.get("reason_code") not in SCOPE_PROPOSAL_CODES or not criticality:
        raise HTTPException(
            status_code=409, detail="Only a watchlisted signal below the scope threshold can propose a scope change"
        )
    label = row.subject_label or row.subject_ref
    try:
        proposal = await proposals.propose(
            db,
            author_user_id=user.id,
            changes=[
                {
                    "field": "prioritization_policy.scope_min_criticality",
                    "value": criticality,
                    "basis": f"Watchlisted CTEM signal on {label}: criticality {criticality} below the scope threshold",
                }
            ],
        )
    except proposals.ProposalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return {
        "proposal_id": str(proposal.id),
        "status": proposal.status,
        "field": "prioritization_policy.scope_min_criticality",
        "value": criticality,
    }


@router.get("/{signal_id}")
async def get_ctem_signal(
    signal_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One signal the caller may see; 404 otherwise."""
    _require_xtm_one()
    row = await signals.get_signal(db, signal_id, organization_id=await _organization_id(db), recorded_by=_reader(user))
    if row is None:
        raise HTTPException(status_code=404, detail="No such signal")
    return signals.to_dict(row)
