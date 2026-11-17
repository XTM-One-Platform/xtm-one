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

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.redis import redis_client
from app.models.user import User
from app.services import ctem_cases as rules
from app.services import ctem_cases_source as source
from app.services import ctem_cases_store as store
from app.services import ctem_cases_view as view_of
from app.services.ctem_verticals import normalize_vertical
from app.services.organizations import get_primary

router = APIRouter()

CaseState = Literal["needs_attention", "in_progress", "resolved", "accepted_risk"]


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


async def visible_case(
    db: AsyncSession, case_id: uuid.UUID, user_id: str, vertical: str
) -> tuple[Any, dict[str, Any]] | None:
    """The stored case and its rendering for *user_id*, ``None`` when they see none of its issues."""
    found = await store.get_case(db, case_id, organization_id=await _organization_id(db))
    if found is None:
        return None
    case, rows, events = found
    rendered = view_of.hydrate(case, rows, events, await view_of.viewer_view(db, user_id, vertical))
    return None if rendered is None else (found, rendered)


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
    limit: int = Query(default=50, ge=1, le=store.MAX_PAGE),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One page of the cases the caller sees and their counts by state (the board and the list)."""
    _require_xtm_one()
    vertical = normalize_vertical(vertical or getattr(user, "ctem_vertical", None))
    primary = await get_primary(db)
    if primary is None:
        # A deployment with no organization context yet holds no case: an empty
        # list, never a 404 the stage pages would show as a failed read.
        return {
            "vertical": vertical,
            "counts": {s: 0 for s in ("needs_attention", "in_progress", "resolved", "accepted_risk")},
            "cases": [],
            "details_unavailable": [],
        }
    cases = await store.list_cases(db, organization_id=primary.id, vertical=vertical)
    view = await view_of.viewer_view(db, str(user.id), vertical)
    rows, counts = view_of.list_for_viewer(
        cases,
        view,
        state=state,
        owner_user_id=str(owner_user_id) if owner_user_id else None,
        priority=priority,
        limit=limit,
        offset=offset,
    )
    return {
        "vertical": vertical,
        "counts": {s: counts.get(s, 0) for s in ("needs_attention", "in_progress", "resolved", "accepted_risk")},
        "cases": rows,
        "details_unavailable": view.missing,
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
    try:
        case = await store.create_manual_case(
            db,
            organization_id=organization_id,
            vertical=vertical,
            title=body.title,
            member_ids=body.issue_ids,
            visible=set(view.open_ids),
            actor=str(user.id),
            reason=(body.reason or "").strip() or None,
        )
    except rules.CaseRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
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
        if await visible_case(db, case_id, str(user.id), vertical) is None:
            raise HTTPException(status_code=404, detail="Case not found")
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


#: One on-demand recompute per user per window (design section 4.3).
RECOMPUTE_WINDOW_SECONDS = 60


@router.post("/recompute")
async def recompute_ctem_cases(
    vertical: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Merge the open issues the caller sees into the shared cases now (one per minute per user).

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
    return await source.recompute_for_user(db, organization_id=organization_id, user_id=str(user.id), vertical=vertical)


async def recompute_slot(user_id: str, vertical: str) -> bool:
    """Take the per-user recompute slot; True when free (or Redis is unavailable)."""
    try:
        client = await redis_client()
        return bool(
            await client.set(f"ctem:cases:recompute:{user_id}:{vertical}", "1", nx=True, ex=RECOMPUTE_WINDOW_SECONDS)
        )
    except Exception:  # no Redis: the endpoint stays usable, only unthrottled
        return True


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
    return shown[1]


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
    try:
        await _apply_update(db, case, rows, body, user, vertical)
    except rules.CaseRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    refreshed = await visible_case(db, case_id, str(user.id), vertical)
    if refreshed is None:  # deleted by a concurrent writer between the commit and the read
        raise HTTPException(status_code=404, detail="Case not found")
    return refreshed[1]
