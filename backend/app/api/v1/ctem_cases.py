# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases API (#11545): list, read and change the organization's shared cases.

Cases are shared; their content is not (#11613). Every route renders a case
for the caller through ``services/ctem_cases_view.py`` from the caller's own
source read: a case with no issue the caller sees is absent from the list and
the counts and a 404 on read or change; an issue the caller cannot see is a
"Restricted" row; priority, scores and counts cover what the caller sees.

XTM One mode only. A case groups the open exposures one remediation closes;
the rules are ``services/ctem_cases.py``, the writes ``services/ctem_cases_store.py``,
the design ``dev-docs/ctem-cases.md``. No model call: the routes stay open
with AI off, like the posture they come from.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.config import settings
from app.core.database import get_db
from app.core.redis import redis_client
from app.models.ctem_case import CASE_STATES
from app.models.user import User
from app.services import ctem_access
from app.services import ctem_case_audit_view as case_audit_view
from app.services import ctem_case_gate_records_view as case_gates_view
from app.services import ctem_case_loop as case_loop
from app.services import ctem_case_routing as case_routing
from app.services import ctem_case_version_history as version_history
from app.services import ctem_case_versions as case_versions
from app.services import ctem_cases as rules
from app.services import ctem_cases_overview as overview
from app.services import ctem_cases_store as store
from app.services import ctem_cases_view as view_of
from app.services import ctem_loop_measures as measures
from app.services import ctem_signals as signal_store
from app.services import ctem_ticketing as ticketing
from app.services.ctem_verticals import normalize_vertical
from app.services.organizations import get_primary

router = APIRouter()

CaseState = Literal[
    "new",
    "analyzing",
    "waiting_decision",
    "remediating",
    "validating",
    "mitigated_watching",
    "resolved",
    "accepted_risk",
]


class CaseValidation(BaseModel):
    """A validation reference (#857): an OpenAEV run or re-test, a provider re-scan, a directory check.

    ``passed`` or ``not_exposed`` resolves the case (``mitigation_only``: to
    mitigated, watching, the re-test stays on); ``failed`` reopens a resolved or
    watched case; ``pending`` only records it.
    """

    provider: str = Field(min_length=1, max_length=40)
    kind: str = Field(default="validation", min_length=1, max_length=40)
    ref: str = Field(min_length=1, max_length=300)
    result: Literal["passed", "not_exposed", "failed", "pending"]
    mitigation_only: bool = False
    note: str | None = Field(default=None, max_length=500)
    #: The target asset groups in the order they were tested.
    run_order: list[str] | None = Field(default=None, max_length=20)
    #: #1500: remediation (a version inventory) or validation (a test); a patch or upgrade case resolves
    #: only on remediation evidence.
    evidence_type: Literal["remediation", "validation"] | None = None


class CaseUpdate(BaseModel):
    """A change of one case; a state change carries its reason or evidence."""

    state: CaseState | None = None
    owner_user_id: uuid.UUID | None = None
    clear_owner: bool = False
    due_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=1000)
    evidence: str | None = Field(default=None, max_length=1000)
    accepted_until: datetime | None = None
    note: str | None = Field(default=None, max_length=2000)


class CaseSplit(BaseModel):
    """Split open issues (``source:external_id``) out of their cases into a new manual case."""

    title: str = Field(min_length=1, max_length=300)
    issue_ids: list[str] = Field(min_length=1, max_length=500)
    reason: str | None = Field(default=None, max_length=1000)


class CaseMerge(BaseModel):
    """Merge cases into one surviving case, which keeps its state, owner and history."""

    target_id: uuid.UUID
    case_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)
    reason: str | None = Field(default=None, max_length=1000)


class CaseBulkUpdate(CaseUpdate):
    """One change applied to several cases; each case is checked and changed on its own."""

    case_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


def _require_xtm_one() -> None:
    if settings.is_copilot_like:
        raise HTTPException(status_code=404, detail="CTEM cases are only available in XTM One mode")


async def _organization_id(db: AsyncSession) -> uuid.UUID:
    primary = await get_primary(db)
    if primary is None:
        raise HTTPException(status_code=404, detail="No organization set up yet")
    return primary.id


def _with_history(running: dict[str, Any] | None, events: list[Any]) -> dict[str, Any] | None:
    """*running* with ``history``: the per-device changes the recomputes recorded, newest first (#1694)."""
    if running is None:
        return None
    return {**running, "history": version_history.history_of(events)}


async def visible_case(
    db: AsyncSession, case_id: uuid.UUID, user_id: str, vertical: str
) -> tuple[Any, dict[str, Any]] | None:
    """The stored case and its rendering for *user_id*, ``None`` when they see none of its issues."""
    found = await store.get_case(db, case_id, organization_id=await _organization_id(db))
    if found is None:
        return None
    case, rows, events = found
    rendered = view_of.hydrate(case, rows, events, await view_of.viewer_view(db, user_id, vertical))
    if rendered is None:
        return None
    # The list's asset-count rule on the single read (#1704): list and case read agree.
    version_history.with_asset_count(rendered, events)
    return found, rendered


async def require_acts_on(db: AsyncSession, user: User, rows: list[Any], vertical: str) -> None:
    """403 when *user* sees the case only through a CTEM view right (#1748): read, never change."""
    if not await view_of.acts_on_case(db, user, rows, vertical):
        raise HTTPException(status_code=403, detail=ctem_access.VIEW_ONLY_MESSAGE)


async def check_owner_sees(db: AsyncSession, rows: list[Any], owner_user_id: uuid.UUID | None, vertical: str) -> None:
    """Refuse an owner who sees none of the case's issues in the source products (their own read)."""
    if owner_user_id is None:
        return
    if not view_of.owner_sees(rows, await view_of.viewer_view(db, str(owner_user_id), vertical)):
        raise rules.CaseRuleError("the owner must be able to see this case's issues in the source products")


@router.get("")
async def list_ctem_cases(
    vertical: str | None = Query(default=None),
    state: CaseState | None = Query(default=None),
    priority: Literal["critical", "high", "medium", "low"] | None = Query(default=None),
    owner_user_id: uuid.UUID | None = Query(default=None),
    asset: str | None = Query(default=None, max_length=300),
    limit: int = Query(default=50, ge=1, le=store.MAX_PAGE),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One page of the cases the caller sees and their counts by state (the board and the list).

    ``asset`` keeps the cases on one asset (an issue the caller sees names it): the asset page's cases.
    """
    _require_xtm_one()
    vertical = normalize_vertical(vertical or getattr(user, "ctem_vertical", None))
    primary = await get_primary(db)
    if primary is None:
        # A deployment with no organization context yet holds no case: an empty
        # list, never a 404 the stage pages would show as a failed read.
        return {
            "vertical": vertical,
            "counts": {s: 0 for s in CASE_STATES},
            "cases": [],
            "details_unavailable": [],
            "details_unavailable_reasons": {},
            "ticketing": {"connected": False, "reason": ticketing.NOT_CONNECTED, "settings": ticketing.SETTINGS_PATH},
        }
    # Cases stay current with no button (s.4.3): a viewer whose last recompute is
    # older than the interval gets one in the background; the list re-reads on its frame.
    from app.services import ctem_cases_refresh as refresh

    await refresh.refresh_on_open(db, organization_id=primary.id, user_id=str(user.id), vertical=vertical)
    cases = await store.list_cases(db, organization_id=primary.id, vertical=vertical)
    view = await view_of.viewer_view(db, str(user.id), vertical)
    rows, counts = view_of.list_for_viewer(
        cases,
        view,
        state=state,
        owner_user_id=str(owner_user_id) if owner_user_id else None,
        priority=priority,
        asset=asset,
        limit=limit,
        offset=offset,
    )
    # The CTEM phase of each case on the page and what it waits on (#1503): the list groups by
    # phase, the Kanban lays its columns out by it, a case waiting for decision says on whom.
    from app.services import ctem_case_delete as case_delete

    history = await store.events_of(db, [uuid.UUID(row["id"]) for row in rows])
    for row in rows:
        row.update(view_of.case_position(history.get(uuid.UUID(row["id"]), []), view))
        # How the case came to be (#1723): the list's "Opened by" filter, the same reading as bulk-delete.
        row["opened_by"] = case_delete.origin(history.get(uuid.UUID(row["id"]), []))
        # Issues that name no asset: the inventory count, else "unknown" with its reason (#1694).
        version_history.with_asset_count(row, history.get(uuid.UUID(row["id"]), []))
    return {
        "vertical": vertical,
        "counts": {s: counts.get(s, 0) for s in CASE_STATES},
        "cases": rows,
        # The open cases the caller sees none of (a count only): the list is never the org total.
        "hidden_cases": view_of.hidden_case_count(cases, view),
        "details_unavailable": view.missing,
        # Why each of those products did not answer this viewer's read (#1852): rejected, too_large,
        # timeout, not_connected or unavailable, so the empty state says what failed.
        "details_unavailable_reasons": {p: view.reasons.get(p, "unavailable") for p in view.missing},
        # Whether a ticketing target is declared (CTEM-19-UI, #1032): the case
        # screens say "No ticketing connected" with the settings link.
        "ticketing": await ticketing.ticketing_target(db),
        # When the cases were last recomputed, and a product that did not answer since (s.4.3).
        "freshness": await refresh.freshness(vertical),
    }


async def _apply_update(
    db: AsyncSession, case: Any, rows: list[Any], body: CaseUpdate, user: User, vertical: str
) -> None:
    """One case change as the caller (the owner check, then the state rules); raises CaseRuleError."""
    if body.owner_user_id and body.owner_user_id != case.owner_user_id:
        await check_owner_sees(db, rows, body.owner_user_id, vertical)
    await store.update_case(
        db,
        case,
        actor=str(user.id),
        actor_kind="user",
        state=body.state,
        owner_user_id=body.owner_user_id,
        clear_owner=body.clear_owner,
        due_at=body.due_at,
        reason=body.reason,
        evidence=body.evidence,
        accepted_until=body.accepted_until,
        note=body.note,
    )


@router.post("/bulk")
async def bulk_update_ctem_cases(
    body: CaseBulkUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Apply one change to several cases (the list's bulk actions).

    Each case keeps the rules of a single change: a case the caller does not see
    fails as "Case not found", a refused change fails with its rule, and the
    others are written in one commit. Answers ``updated`` (ids) and ``failed``
    (``{id, reason}``).
    """
    _require_xtm_one()
    vertical = normalize_vertical(getattr(user, "ctem_vertical", None))
    change = CaseUpdate(**body.model_dump(exclude={"case_ids"}))
    updated: list[str] = []
    failed: list[dict[str, str]] = []
    for case_id in dict.fromkeys(body.case_ids):
        shown = await visible_case(db, case_id, str(user.id), vertical)
        if shown is None:
            failed.append({"id": str(case_id), "reason": "Case not found"})
            continue
        (case, rows, _), _ = shown
        if not await view_of.acts_on_case(db, user, rows, vertical):
            failed.append({"id": str(case_id), "reason": ctem_access.VIEW_ONLY_MESSAGE})
            continue
        try:
            await _apply_update(db, case, rows, change, user, vertical)
        except rules.CaseRuleError as exc:
            failed.append({"id": str(case_id), "reason": str(exc)})
            continue
        updated.append(str(case_id))
    # A refused change wrote nothing (the rules run before any write), so one
    # commit carries exactly the cases in ``updated``.
    await db.commit()
    return {"updated": updated, "failed": failed}


@router.post("/split")
async def split_ctem_case(
    body: CaseSplit,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Split open issues the caller sees out of their cases into a new manual case; answers it."""
    _require_xtm_one()
    vertical = normalize_vertical(getattr(user, "ctem_vertical", None))
    organization_id = await _organization_id(db)
    view = await view_of.viewer_view(db, str(user.id), vertical)
    visible = set(view.open_ids)
    if await ctem_access.level(db, user) == "view":
        # A CTEM view right is strictly read only (#1748).
        raise HTTPException(status_code=403, detail=ctem_access.VIEW_ONLY_MESSAGE)
    pending = case_loop.PendingEvents()
    try:
        case = await store.create_manual_case(
            db,
            organization_id=organization_id,
            vertical=vertical,
            title=body.title,
            member_ids=body.issue_ids,
            visible=visible,
            actor=str(user.id),
            reason=(body.reason or "").strip() or None,
            events=pending,
        )
    except rules.CaseRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    await case_loop.publish_pending(pending)
    shown = await visible_case(db, case.id, str(user.id), vertical)
    if shown is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return shown[1]


@router.post("/merge")
async def merge_ctem_cases(
    body: CaseMerge,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Merge cases the caller sees into the surviving one; answers the survivor.

    Every case named must be visible to the caller (404 otherwise, as for an
    unknown one): a person cannot fold a case they see nothing of.
    """
    _require_xtm_one()
    vertical = normalize_vertical(getattr(user, "ctem_vertical", None))
    for case_id in [body.target_id, *body.case_ids]:
        found = await visible_case(db, case_id, str(user.id), vertical)
        if found is None:
            raise HTTPException(status_code=404, detail="Case not found")
        await require_acts_on(db, user, found[0][1], vertical)
    try:
        await store.merge_cases(
            db,
            organization_id=await _organization_id(db),
            vertical=vertical,
            target_id=body.target_id,
            source_ids=body.case_ids,
            actor=str(user.id),
            reason=(body.reason or "").strip() or None,
        )
    except rules.CaseRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    shown = await visible_case(db, body.target_id, str(user.id), vertical)
    if shown is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return shown[1]


class CaseBulkDelete(BaseModel):
    """Cases to delete (#1723): named by id, selected by a filter, or both (a named case must match it)."""

    case_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    states: list[CaseState] = Field(default_factory=list)
    #: ``legacy``: built by a source read before signals; ``signal``; ``split``.
    opened_by: Literal["legacy", "signal", "split"] | None = None
    vertical: str | None = Field(default=None, max_length=40)
    reason: str | None = Field(default=None, max_length=500)
    #: ``true`` (default) answers what would be deleted and writes nothing.
    dry_run: bool = True


async def _delete(db: AsyncSession, admin: User, cases: list[Any], reason: str | None, dry_run: bool) -> dict[str, Any]:
    from sqlalchemy.exc import DBAPIError

    from app.services import ctem_case_delete as case_delete

    try:
        result = await case_delete.delete_cases(db, cases, actor=admin, reason=reason, dry_run=dry_run)
        # A dry run writes nothing, so the commit is a no-op there.
        await db.commit()
    except DBAPIError as exc:
        await db.rollback()
        if case_delete.is_lock_wait(exc):
            raise HTTPException(status_code=409, detail=case_delete.LOCK_BUSY_DETAIL) from exc
        raise
    # The released signals are decided again after the commit, outside this request (#1950).
    return case_delete.hand_off_released(result)


@router.post("/bulk-delete")
async def bulk_delete_ctem_cases(
    body: CaseBulkDelete,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Delete 1 to 200 cases, administrators only (#1723); ``dry_run`` (default) writes nothing.

    Each case goes with its members and history; its open work items and armed
    timers are cancelled, its signals go back to the scope gate, its fix key is
    free for a fresh case. One audit row per case. Answers ``{dry_run, count,
    cases: [summary], unknown}``.
    """
    _require_xtm_one()
    from app.services import ctem_case_delete as case_delete

    if not body.case_ids and not (body.states or body.opened_by or body.vertical):
        raise HTTPException(
            status_code=422, detail="Name the cases (case_ids) or a filter (states, opened_by, vertical)"
        )
    cases, unknown = await case_delete.select_cases(
        db,
        organization_id=await _organization_id(db),
        case_ids=body.case_ids,
        states=body.states,
        opened_by=body.opened_by,
        vertical=body.vertical,
    )
    result = await _delete(db, admin, cases, body.reason, body.dry_run)
    return {**result, "unknown": unknown}


class ThreatPackageRepair(BaseModel):
    #: ``true`` (default) answers what the repair would correct and writes nothing.
    dry_run: bool = True


@router.post("/threat-packages/repair")
async def repair_ctem_threat_packages(
    body: ThreatPackageRepair,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Correct the threat packages stored with a Grouping page whose Grouping is not live (#1547).

    One-off repair, administrators: each case's newest package recorded with a
    Grouping page is re-read in OpenCTI with the caller's own access; a Grouping
    inside an open draft gets the draft page, one found nowhere gets no page
    (both read ``drafted``). ``dry_run`` answers the count and the cases.
    """
    _require_xtm_one()
    from app.services import ctem_threat_package_live as package_live

    result = await package_live.repair_stored_packages(
        db, await _organization_id(db), str(admin.id), dry_run=body.dry_run
    )
    # A dry run writes nothing, so the commit is a no-op there.
    await db.commit()
    return result


#: One on-demand recompute per user per window (design section 4.3).
RECOMPUTE_WINDOW_SECONDS = 60


@router.post("/recompute")
async def recompute_ctem_cases(
    vertical: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Merge the open issues the caller sees into the shared cases now (one per minute per user).

    Internal since the cases recompute by themselves (s.4.3): no screen calls it;
    agents and administrators may.

    Reads OpenCTI and OpenAEV with the caller's own credentials (#11613): adds
    and reopens what the caller sees open, resolves only what the caller's read
    confirms closed, so any user may run it and a narrower view closes
    nothing. A product that cannot answer makes the recompute write nothing
    (``applied`` false and the product named in ``missing``).
    """
    _require_xtm_one()
    vertical = normalize_vertical(vertical or getattr(user, "ctem_vertical", None))
    organization_id = await _organization_id(db)
    if not await recompute_slot(str(user.id), vertical):
        raise HTTPException(status_code=429, detail="One recompute per minute: try again shortly")
    from app.services import ctem_cases_refresh as refresh

    answer = await refresh.recompute_as(db, organization_id=organization_id, user_id=str(user.id), vertical=vertical)
    if answer.get("busy"):
        # One recompute at a time per organization (#1953): never a second one queued behind it.
        raise HTTPException(status_code=409, detail="A recompute of the cases is already running: try again shortly")
    return answer


async def recompute_slot(user_id: str, vertical: str) -> bool:
    """Take the per-user recompute slot; True when free (or Redis is unavailable)."""
    try:
        client = await redis_client()
        return bool(
            await client.set(f"ctem:cases:recompute:{user_id}:{vertical}", "1", nx=True, ex=RECOMPUTE_WINDOW_SECONDS)
        )
    except Exception:  # no Redis: the endpoint stays usable, only unthrottled
        return True


@router.get("/measures")
async def ctem_loop_measures(
    vertical: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """How fast the loop runs on the cases the caller sees (CTEM-30, #937): signal to case, to
    validation, to decision, to passing re-test, and the share of resolved cases closed on proof.

    Read from the case history and the origin signals the caller may read; a
    measure with no sample is ``null`` with a count of 0, never 0 hours.
    """
    _require_xtm_one()
    vertical = normalize_vertical(vertical or getattr(user, "ctem_vertical", None))
    primary = await get_primary(db)
    if primary is None:
        return {"vertical": vertical, "measures": measures.summarize([], {}, {})}
    cases = await store.list_cases(db, organization_id=primary.id, vertical=vertical)
    view = await view_of.viewer_view(db, str(user.id), vertical)
    seen = [case for case, rows in cases if view_of.summary(case, rows, view) is not None]
    ids = [case.id for case in seen]
    events = await store.events_of(db, ids)
    reader = await ctem_access.signal_reader(db, user)
    observed = await signal_store.observed_by_case(db, ids, organization_id=primary.id, recorded_by=reader)
    return {"vertical": vertical, "measures": measures.summarize(seen, events, observed)}


@router.get("/overview")
async def ctem_cases_overview(
    vertical: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The cases in motion for the command center and the stage pages (#1621): counts by status,
    open and waiting per CTEM phase with the five that need attention first, the decisions waiting
    (the oldest one), and the funnel step (OpenCTI vulnerabilities carried by an open case).

    Same visibility as the list, so each count equals the list it links to. One read of the
    cases, one of the viewer's view and one of the history of every visible case.
    """
    _require_xtm_one()
    vertical = normalize_vertical(vertical or getattr(user, "ctem_vertical", None))
    primary = await get_primary(db)
    if primary is None:
        return {"vertical": vertical, **overview.empty()}
    cases = await store.list_cases(db, organization_id=primary.id, vertical=vertical)
    view = await view_of.viewer_view(db, str(user.id), vertical)
    seen = [case.id for case, rows in cases if view_of.visible_members(rows, view)]
    history = await store.events_of(db, seen)
    return {"vertical": vertical, **overview.build(cases, view, history)}


@router.get("/{case_id}")
async def get_ctem_case(
    case_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One case as the caller sees it: its fix, their visible issues, Restricted rows, its history."""
    _require_xtm_one()
    vertical = normalize_vertical(getattr(user, "ctem_vertical", None))
    shown = await visible_case(db, case_id, str(user.id), vertical)
    if shown is None:
        raise HTTPException(status_code=404, detail="Case not found")
    (case, _, events), rendered = shown
    # The audit trail (#1490): each event with its CTEM phase, autonomy class and actor, and the phases.
    audit = await case_audit_view.audit_trail(db, rendered.get("history") or [], viewer_id=user.id)
    # One resolved decision record per gate (#1518): the decision row joined with the gate events;
    # the waiting gate events of the history carry the same record in ``answer``.
    gate_records = await case_gates_view.gate_records(db, str(case.id), audit["history"], viewer_id=user.id)
    return {
        **rendered,
        **audit,
        "gates": gate_records,
        # The versions the case's product runs on the assets (#1252), never a typed software version.
        "running_versions": _with_history(await case_versions.for_case(db, case), events),
        # The assets its visible issues name, with the version each runs from the inventory (#1176 slice 3b).
        "affected_assets": await case_versions.assets_for_case(db, case, rendered.get("issues") or []),
        "ticketing": await ticketing.ticketing_target(db),
        # The fleet that runs the case (#1390): {slug, name, active}; active false = no active fleet owns it.
        "owner_fleet": await case_routing.owner_view(db, case),
    }


@router.delete("/{case_id}")
async def delete_ctem_case(
    case_id: uuid.UUID,
    dry_run: bool = Query(default=False),
    reason: str | None = Query(default=None, max_length=500),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Delete one case, administrators only (#1723), as ``bulk-delete`` does; 404 when unknown."""
    _require_xtm_one()
    from app.services import ctem_case_delete as case_delete

    cases, _unknown = await case_delete.select_cases(db, organization_id=await _organization_id(db), case_ids=[case_id])
    if not cases:
        raise HTTPException(status_code=404, detail="Case not found")
    return await _delete(db, admin, cases, reason, dry_run)


@router.patch("/{case_id}")
async def update_ctem_case(
    case_id: uuid.UUID,
    body: CaseUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Change the owner, due date, a note or the state; 422 with the rule when it is refused."""
    _require_xtm_one()
    vertical = normalize_vertical(getattr(user, "ctem_vertical", None))
    shown = await visible_case(db, case_id, str(user.id), vertical)
    if shown is None:
        raise HTTPException(status_code=404, detail="Case not found")
    (case, rows, _), _ = shown
    await require_acts_on(db, user, rows, vertical)
    try:
        await _apply_update(db, case, rows, body, user, vertical)
    except rules.CaseRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    refreshed = await visible_case(db, case_id, str(user.id), vertical)
    if refreshed is None:  # deleted by a concurrent writer between the commit and the read
        raise HTTPException(status_code=404, detail="Case not found")
    return refreshed[1]


@router.post("/{case_id}/validations")
async def record_ctem_case_validation(
    case_id: uuid.UUID,
    body: CaseValidation,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Record a validation reference on a case the caller sees (#857); a pass proves, a fail reopens.

    The case then carries a validation plan: it resolves only on a passing
    reference. Raises ``ctem_case.resolved`` or ``ctem_case.reopened`` after the commit.
    """
    _require_xtm_one()
    vertical = normalize_vertical(getattr(user, "ctem_vertical", None))
    shown = await visible_case(db, case_id, str(user.id), vertical)
    if shown is None:
        raise HTTPException(status_code=404, detail="Case not found")
    (case, rows, _), _ = shown
    await require_acts_on(db, user, rows, vertical)
    pending = case_loop.PendingEvents()
    try:
        await case_loop.record_validation(
            db,
            case,
            provider=body.provider,
            kind=body.kind,
            ref=body.ref,
            result=body.result,
            mitigation_only=body.mitigation_only,
            note=body.note,
            run_order=body.run_order,
            evidence_type=body.evidence_type,
            actor=str(user.id),
            events=pending,
        )
    except rules.CaseRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    await case_loop.publish_pending(pending)
    refreshed = await visible_case(db, case_id, str(user.id), vertical)
    if refreshed is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return refreshed[1]
