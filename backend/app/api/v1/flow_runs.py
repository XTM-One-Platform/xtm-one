# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Flow execution API — activate, run, history, cancel.

Separate router from ``flows.py`` so execution stays distinct from authoring:
the canvas saves a graph, this router makes what you drew actually run.  Every
mutating route passes the agentic licence gate.

Activation is an explicit act, not a status write: ``PUT /flows/{id}`` still
refuses ``active`` and points here.  The gate is stricter than saving —
validation **errors** block a save too, but activation additionally requires
at least one trigger node, since an active flow nothing can start is a lie.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_agentic_ai_licensed
from app.api.v1.helpers import (
    can_manage,
    get_manageable_entity,
    get_user_group_ids,
    parse_path_id,
    user_visible_filter,
)
from app.api.v1.schemas.flow import (
    FlowActivateResponse,
    FlowFileUploadResponse,
    FlowRunDetailResponse,
    FlowRunListResponse,
    FlowRunRequest,
    FlowRunStatusCountsResponse,
    FlowRunStepResponse,
    FlowRunSummaryResponse,
)
from app.core.config import settings
from app.core.database import get_db
from app.models.decision_hooks import open_flow_park
from app.models.flow import (
    FLOW_STATUS_ACTIVE,
    FLOW_STATUS_DRAFT,
    Flow,
)
from app.models.flow_run import (
    FLOW_RUN_STATUS_AWAITING,
    FLOW_RUN_STATUS_PENDING,
    FLOW_RUN_STATUS_RUNNING,
    FLOW_RUN_STATUSES,
    FlowRun,
    FlowRunStep,
)
from app.models.user import User
from app.services.assignment_pause import refuse_flow_if_owner_deactivated
from app.services.audit import log_audit
from app.services.flow_access import get_visible_run
from app.services.flow_graph import validate_graph
from app.services.flow_runner import create_flow_run, find_trigger_nodes
from app.services.health.sandbox.flows import production_runs_only
from app.services.visibility import SegmentCounts, Visibility, segment_counts

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _run_summary(run: FlowRun) -> FlowRunSummaryResponse:
    return FlowRunSummaryResponse(
        id=str(run.id),
        flow_id=str(run.flow_id),
        flow_version_number=run.flow_version_number,
        status=run.status,
        trigger_type=run.trigger_type,
        depth=run.depth,
        parent_run_id=str(run.parent_run_id) if run.parent_run_id else None,
        result_summary=run.result_summary,
        error=run.error,
        created_at=run.created_at.isoformat(),
        started_at=_iso(run.started_at),
        completed_at=_iso(run.completed_at),
    )


async def _get_visible_flow(db: AsyncSession, user: User, flow_id: str) -> Flow:
    from app.api.v1.helpers import get_user_group_ids

    fid = parse_path_id(flow_id, not_found_detail="Flow not found")
    group_ids = await get_user_group_ids(db, user.id)
    flow = (
        await db.execute(
            select(Flow).where(
                Flow.id == fid,
                user_visible_filter(Flow, user.id, group_ids, is_admin=user.is_admin),
            )
        )
    ).scalar_one_or_none()
    if flow is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    return flow


async def _get_controllable_run(
    db: AsyncSession, user: User, run_id: str, *, action: str
) -> FlowRun:
    """Load a run the caller may *control*, not merely read.

    Visibility is not control. ``get_visible_run`` deliberately admits every
    authenticated user to a company-managed run, and any read-only shared
    viewer to a shared one — the right audience for reading a journal, the
    wrong one for cancelling somebody else's automation or single-stepping its
    execution (a debugger reads and steers intermediate state).

    Control is granted to:

    - whoever started the run (``run.user_id``) — ``run_flow`` only requires
      *visibility*, so a viewer can legitimately start runs and must be able
      to stop their own; and
    - anyone who may manage the owning flow (``can_manage``: admin, owner, or
      a ``config`` grant) — the same contract ``activate``/``deactivate`` use.

    The manage check resolves against the **live flow**, not the run's sharing
    snapshot, so revoking someone's access also takes their control of the
    runs that were started while they had it.
    """
    run = await get_visible_run(db, user, run_id)
    if run.user_id is not None and run.user_id == user.id:
        return run
    flow = (
        await db.execute(select(Flow).where(Flow.id == run.flow_id))
    ).scalar_one_or_none()
    group_ids = await get_user_group_ids(db, user.id)
    if flow is None or not can_manage(user, flow, group_ids):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Only an admin can {action} runs of a company-managed flow"
                if flow is not None and flow.is_company_managed
                else f"Only the flow's owner or an admin can {action} this run"
            ),
        )
    return run


async def _refuse_if_run_flow_paused(db: AsyncSession, run: FlowRun) -> None:
    """Hold a parked run of a flow whose owner is deactivated.

    Releasing it is what would make it go on, so an answer or a breakpoint
    release waits for the account to be back; cancelling still works.
    """
    flow = (
        await db.execute(select(Flow).where(Flow.id == run.flow_id))
    ).scalar_one_or_none()
    if flow is not None:
        await refuse_flow_if_owner_deactivated(db, flow)


def _blocking_issues(graph: dict) -> list[dict]:
    result = validate_graph(graph)
    return [i.to_dict() for i in result.issues if i.severity == "error"]


# ---------------------------------------------------------------------------
# Debugger
# ---------------------------------------------------------------------------


@router.post("/runs/{run_id}/debug", response_model=FlowRunSummaryResponse)
async def debug_flow_run(
    run_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
) -> FlowRunSummaryResponse:
    """Release a run paused on a breakpoint.

    ``{"action": "continue"}`` resumes normally; ``{"action": "step"}``
    resumes AND arms step mode, so the walker pauses again before the very
    next node — the UE step-over.
    """
    action = str(body.get("action") or "continue")
    if action not in ("continue", "step"):
        raise HTTPException(status_code=422, detail="action must be continue or step")
    run = await _get_controllable_run(db, user, run_id, action="debug")
    state = dict(run.state or {})
    waiting = dict(state.get("waiting") or {})
    if waiting.get("kind") != "breakpoint":
        raise HTTPException(
            status_code=409, detail="The run is not paused on a breakpoint."
        )
    await _refuse_if_run_flow_paused(db, run)
    waiting["released"] = True
    state["waiting"] = waiting
    state["step_mode"] = action == "step"
    run.state = state
    await db.commit()
    from app.services import orchestration_engine as engine

    await engine.notify_orchestration_wake()
    return _run_summary(run)


@router.post("/runs/{run_id}/respond", response_model=FlowRunSummaryResponse)
async def respond_flow_run(
    run_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
) -> FlowRunSummaryResponse:
    """Answer a run paused on an Ask-user node.

    Stores the answer in the park state and releases it; the walker resumes,
    the answer becomes the node's ``response`` output and the flow continues.

    Who may answer deliberately tracks **who the question was rung to**, since
    that is the audience the HITL contract is about:

    - a company-managed run broadcasts the bell on ``activity:company``, so
      every user who can see the run may answer it — a company flow asks the
      company, and ``responded_by`` records who picked it up;
    - a personal run rings only its owner's bell, so answering it needs
      control of the run (owner, or flow manage) rather than mere read-only
      shared visibility — otherwise a read-only viewer could inject
      ``{{ask.response}}`` into a run nobody asked them about.
    """
    run = await answer_flow_park(db, user, run_id, str(body.get("response") or ""))
    return _run_summary(run)


async def answer_flow_park(db: AsyncSession, user: User, run_id: str, response: str) -> FlowRun:
    """Answer a flow run's Ask-user park as *user*: the one path the respond
    endpoint and the decisions router (#842, S2) both take, so the access rule
    and the refusals cannot drift between them."""
    response = response.strip()
    if not response:
        raise HTTPException(status_code=422, detail="A response is required.")
    run = await get_visible_run(db, user, run_id)
    if not run.is_company_managed:
        run = await _get_controllable_run(db, user, run_id, action="answer")
    state = dict(run.state or {})
    waiting = dict(state.get("waiting") or {})
    if waiting.get("kind") != "human":
        raise HTTPException(
            status_code=409, detail="The run is not waiting for a human answer."
        )
    await _refuse_if_run_flow_paused(db, run)
    from app.services.decision_ask import flow_option

    # An Ask-user node with options (#842, S6): the chosen key is {{ask.option}}.
    ask = waiting.get("ask") if isinstance(waiting.get("ask"), dict) else None
    option = flow_option(ask, response)
    waiting["released"] = True
    waiting["response"] = response
    if option:
        waiting["option"] = option
    waiting["responded_by"] = user.email or str(user.id)
    state["waiting"] = waiting
    run.state = state
    from app.models.decision_hooks import answer_actor

    token = answer_actor.set(user.id)
    try:
        await db.commit()
    finally:
        answer_actor.reset(token)
    from app.services import orchestration_engine as engine

    await engine.notify_orchestration_wake()
    return run


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


@router.post("/{flow_id}/activate", response_model=FlowActivateResponse)
async def activate_flow(
    flow_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
) -> FlowActivateResponse:
    """Make the flow's triggers live.

    Requires write access, zero validation errors and at least one trigger
    node.  A `trigger.webhook` node mints the flow's shared secret on first
    activation; the secret (and the delivery URL) is returned **here only**.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    flow = await get_manageable_entity(
        db, Flow, user, flow_id, entity_label="Flow", user_group_ids=user_group_ids
    )

    errors = _blocking_issues(flow.graph or {})
    if errors:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Fix the validation errors before activating.",
                "issues": errors,
            },
        )
    # An imported node awaiting review never runs: a live trigger would only
    # start runs that fail on it (or, for the trigger itself, fire as it was
    # converted). A manual run stays possible — it is the test button.
    awaiting_review = [
        issue.to_dict()
        for issue in validate_graph(flow.graph or {}).issues
        if issue.code == "node.import_review"
    ]
    if awaiting_review:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Review the imported nodes before activating.",
                "issues": awaiting_review,
            },
        )
    triggers = find_trigger_nodes(flow.graph or {})
    if not triggers:
        raise HTTPException(
            status_code=422,
            detail="Add a trigger node — an active flow needs a way to start.",
        )

    warnings: list[dict] = []
    if flow.user_id is None:
        # Manual and chat are user-initiated; only the truly automatic triggers
        # (schedule / webhook / integration event) need an owner to fire.
        automatic = [
            n
            for n in triggers
            if (n.get("kind") or "") not in ("trigger.manual", "trigger.chat")
        ]
        if automatic:
            warnings.append(
                {
                    "code": "flow.ownerless_triggers",
                    "message": (
                        "This company-managed flow has no owner: its automatic "
                        "triggers will not fire until an owner is assigned. "
                        "Manual runs work."
                    ),
                    "severity": "warning",
                    "node_id": automatic[0].get("id"),
                    "edge_id": None,
                    "field": None,
                }
            )

    webhook_url: str | None = None
    webhook_secret: str | None = None
    webhook_nodes = find_trigger_nodes(flow.graph or {}, "trigger.webhook")
    if webhook_nodes:
        if not flow.webhook_secret:
            flow.webhook_secret = secrets.token_urlsafe(32)
        webhook_secret = flow.webhook_secret
        path = str(
            (webhook_nodes[0].get("config") or {}).get("path") or ""
        ).strip().strip("/")
        base = (settings.base_url or "").rstrip("/")
        webhook_url = f"{base}/api/hooks/flows/{flow.id}/{path}"

    flow.status = FLOW_STATUS_ACTIVE
    await log_audit(
        db,
        user=user,
        action="flow.activate",
        entity_type="flow",
        entity_id=flow.id,
        entity_name=flow.name,
        summary=f"Activated flow '{flow.name}'",
        request=request,
    )
    await db.commit()
    await db.refresh(flow)

    from app.api.v1.flows import _owner_paused, _to_response

    return FlowActivateResponse(
        flow=_to_response(
            flow,
            can_manage=can_manage(user, flow, user_group_ids),
            owner_name=user.full_name,
            owner_paused=await _owner_paused(db, flow, user),
        ),
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        warnings=warnings,
    )


@router.post("/{flow_id}/deactivate", response_model=FlowActivateResponse)
async def deactivate_flow(
    flow_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
) -> FlowActivateResponse:
    """Back to draft.  Parked runs finish on their own; triggers stop firing."""
    user_group_ids = await get_user_group_ids(db, user.id)
    flow = await get_manageable_entity(
        db, Flow, user, flow_id, entity_label="Flow", user_group_ids=user_group_ids
    )
    flow.status = FLOW_STATUS_DRAFT
    await log_audit(
        db,
        user=user,
        action="flow.deactivate",
        entity_type="flow",
        entity_id=flow.id,
        entity_name=flow.name,
        summary=f"Deactivated flow '{flow.name}'",
        request=request,
    )
    await db.commit()
    await db.refresh(flow)

    from app.api.v1.flows import _owner_paused, _to_response

    return FlowActivateResponse(
        flow=_to_response(
            flow,
            can_manage=can_manage(user, flow, user_group_ids),
            owner_name=user.full_name,
            owner_paused=await _owner_paused(db, flow, user),
        )
    )


# ---------------------------------------------------------------------------
# Run attachments
# ---------------------------------------------------------------------------


@router.post(
    "/{flow_id}/files",
    response_model=FlowFileUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_flow_run_file(
    flow_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FlowFileUploadResponse:
    """Upload a file to attach to a run of this flow.

    Same store as chat attachments (``file_attachments`` + S3) so the agent
    pipeline reads them natively; the returned ``file_id`` goes into the run
    request's ``file_ids``. Ownership is the uploader's — resolving ids at run
    time is scoped to the caller, so an id can never smuggle someone else's
    file into a run.
    """
    from app.models.file_attachment import FileAttachment
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES, file_storage

    await _get_visible_flow(db, user, flow_id)

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"File exceeds the "
                f"{MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB upload limit."
            ),
        )
    filename = file.filename or "file"
    content_type = file.content_type or "application/octet-stream"
    s3_object_name = await file_storage.upload(
        raw, filename, content_type, prefix=f"flows/{flow_id}"
    )
    attachment = FileAttachment(
        user_id=user.id,
        filename=filename,
        content_type=content_type,
        size=len(raw),
        s3_object_name=s3_object_name,
        source="flow",
    )
    db.add(attachment)
    await db.commit()
    await db.refresh(attachment)
    return FlowFileUploadResponse(
        file_id=str(attachment.id),
        filename=filename,
        content_type=content_type,
        size=len(raw),
    )


# ---------------------------------------------------------------------------
# Manual run
# ---------------------------------------------------------------------------


@router.post(
    "/{flow_id}/run",
    response_model=FlowRunSummaryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def run_flow(
    flow_id: str,
    body: FlowRunRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
) -> FlowRunSummaryResponse:
    """Run the flow now, as the caller.

    Works on drafts too — this is also the builder's "test run" button, and
    requiring activation to test would force half-built flows live.  The
    entry point is the `trigger.manual` node when one exists, else the first
    trigger; ``parameters`` become the trigger step's outputs.
    """
    flow = await _get_visible_flow(db, user, flow_id)
    # Whoever asks, an admin included: the flow is its owner's.
    await refuse_flow_if_owner_deactivated(db, flow)

    errors = _blocking_issues(flow.graph or {})
    if errors:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Fix the validation errors before running.",
                "issues": errors,
            },
        )
    # Prefer a manual trigger, then a chat trigger, else the first trigger —
    # both manual and chat are user-initiated entry points a "run now" fits.
    triggers = (
        find_trigger_nodes(flow.graph or {}, "trigger.manual")
        or find_trigger_nodes(flow.graph or {}, "trigger.chat")
        or find_trigger_nodes(flow.graph or {})
    )
    if not triggers:
        raise HTTPException(
            status_code=422,
            detail="Add a trigger node — a run needs an entry point.",
        )
    entry = triggers[0]
    entry_kind = entry.get("kind") or "trigger.manual"
    trigger_type = (
        entry_kind.removeprefix("trigger.")
        if entry_kind.startswith("trigger.")
        else "manual"
    )

    from app.services import orchestration_engine as engine

    # The run's input text: what the caller typed, else the trigger's own
    # default. Shaped as that trigger kind's DECLARED outputs so the walker
    # passes it through verbatim — a chat trigger publishes `{{<chat>.message}}`,
    # the others `{{<trigger>.prompt}}`.
    text = (body.prompt or "").strip()
    if entry_kind == "trigger.chat":
        # Builder test runs fall back to the message typed on the node itself.
        default_message = str(
            (entry.get("config") or {}).get("default_message") or ""
        ).strip()
        trigger_payload: dict[str, Any] = {"message": text or default_message}
    else:
        # A manual trigger with declared parameters is a typed signature: fill
        # defaults, coerce, and refuse a mismatch — the UE "call a function
        # with the wrong pins" red compile error, server-side.
        from app.services.flow_runner import coerce_parameters, declared_parameters

        declared = (
            declared_parameters(entry.get("config") or {})
            if entry_kind == "trigger.manual"
            else []
        )
        provided = dict(body.parameters or {})
        if declared:
            payload, param_errors = coerce_parameters(declared, provided)
            if param_errors:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "Run parameters do not match the trigger's declaration.",
                        "issues": param_errors,
                    },
                )
        else:
            payload = provided
        trigger_payload = {
            "payload": payload,
            "prompt": text or str((entry.get("config") or {}).get("prompt") or "").strip(),
        }

    # Run attachments: resolve the ids OWNER-SCOPED (an id can never smuggle
    # someone else's file in) and freeze their metadata into the trigger
    # payload — `{{<trigger>.files}}` downstream, and the walker hands the
    # actual content to every agent node the way chat attachments arrive.
    if body.file_ids:
        from app.api.v1.helpers import parse_uuid_list
        from app.models.file_attachment import FileAttachment

        parsed_ids = parse_uuid_list(body.file_ids[:20], "file")
        rows = (
            (
                await db.execute(
                    select(FileAttachment).where(
                        FileAttachment.id.in_(parsed_ids),
                        FileAttachment.user_id == user.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            raise HTTPException(
                status_code=422,
                detail="None of the provided file_ids could be resolved.",
            )
        trigger_payload["files"] = [
            {
                "file_id": str(fa.id),
                "filename": fa.filename,
                "content_type": fa.content_type,
                "size": fa.size,
            }
            for fa in rows
        ]
    # Per flow, not per platform: the endpoint gate above is now the licence
    # question only, so a deterministic flow runs with AI off (FR-014) and an
    # AI-bearing one is refused here. Refused rather than recorded as skipped —
    # someone clicked Run and deserves to be told, where an automatic trigger
    # has nobody to tell and gets the skip record instead (FR-016).
    from app.services.agentic_gate import refuse_ai_disabled
    from app.services.ai_state import is_ai_enabled
    from app.services.flow_triggers import flow_requires_ai

    if not await is_ai_enabled(db) and await flow_requires_ai(db, flow):
        refuse_ai_disabled()

    run = await create_flow_run(
        db,
        flow,
        trigger_type=trigger_type,
        trigger_payload=trigger_payload,
        trigger_context={
            "type": trigger_type,
            "trigger_node_id": entry.get("id"),
            "triggered_by": str(user.id),
        },
        user_id=user.id,
    )
    await log_audit(
        db,
        user=user,
        action="flow.run",
        entity_type="flow",
        entity_id=flow.id,
        entity_name=flow.name,
        summary=f"Manually ran flow '{flow.name}'",
        details={"run_id": str(run.id)},
        request=request,
    )
    await db.commit()
    await db.refresh(run)
    await engine.notify_orchestration_wake()
    return _run_summary(run)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@router.get("/{flow_id}/runs", response_model=FlowRunListResponse)
async def list_flow_runs(
    flow_id: str,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FlowRunListResponse:
    flow = await _get_visible_flow(db, user, flow_id)
    limit = max(1, min(limit, 100))
    total = (
        await db.execute(
            select(func.count(FlowRun.id)).where(FlowRun.flow_id == flow.id, production_runs_only())
        )
    ).scalar_one()
    rows = (
        (
            await db.execute(
                select(FlowRun)
                .where(FlowRun.flow_id == flow.id, production_runs_only())
                .order_by(FlowRun.created_at.desc())
                .limit(limit)
                .offset(max(0, offset))
            )
        )
        .scalars()
        .all()
    )
    return FlowRunListResponse(runs=[_run_summary(r) for r in rows], total=total)


# The Operations → Executions status pills are assignment vocabulary; map the
# two that differ onto flow-run statuses so one filter drives both feeds.
_GLOBAL_STATUS_FILTERS = {
    "running": [FLOW_RUN_STATUS_RUNNING, FLOW_RUN_STATUS_PENDING],
    "awaiting_human": [FLOW_RUN_STATUS_AWAITING],
}


FLOW_RUN_SORT_DESCRIPTION = (
    "Order of the runs, the assignment feed's vocabulary (#9378 slice 8f): 'newest' (default), "
    "'oldest', 'status' or 'status_desc', 'agent' or 'agent_desc' (the flow's name), "
    "'longest' or 'shortest' (duration, runs without one last)."
)
FLOW_RUN_SORT_PATTERN = "^(newest|oldest|status|status_desc|agent|agent_desc|longest|shortest)$"


@router.get("/runs/all", response_model=FlowRunListResponse)
async def list_all_flow_runs(
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    # Aliased: a parameter literally named `status` would shadow the
    # `fastapi.status` module this file uses for HTTP codes.
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = None,
    ownership: str | None = None,
    vis: str | None = None,
    sort: Annotated[str | None, Query(description=FLOW_RUN_SORT_DESCRIPTION, pattern=FLOW_RUN_SORT_PATTERN)] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FlowRunListResponse:
    """List flow runs across every visible flow (Operations → Executions).

    Visibility rides the run's sharing snapshot through the same
    ``user_visible_filter`` the flow listing uses — tier for tier what
    ``get_visible_run`` enforces per run, in SQL.

    Query params mirror the assignment feed this list sits next to:
      - status: a flow-run status, or the Executions-tab vocabulary
        (``running`` also matches ``pending``; ``awaiting_human`` maps to
        ``awaiting``)
      - search: case-insensitive across flow name, result summary and error
      - vis: ``mine`` | ``shared`` (not mine, not company-managed) |
        ``company`` (company-managed, not mine) | ``all``; wins over ``ownership``
      - ownership: [DEPRECATED] ``mine`` for runs I started, ``shared`` (or its alias
        ``company_managed``, the assignment feed's spelling) for everybody
        else's, omit (or ``all``) for both; any other value is a 422

    Declared before ``GET /runs/{run_id}`` so the literal wins the match.
    """
    from app.api.v1.helpers import get_user_group_ids, resolve_list_ownership

    # Before any database access: a bad filter is the caller's error.
    ownership = resolve_list_ownership(vis, ownership)
    group_ids = await get_user_group_ids(db, user.id)
    query = _global_runs_query(user, group_ids, status_filter=status_filter, search=search, ownership=ownership)
    total = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            query.order_by(*flow_run_order(sort)).limit(limit).offset(offset)
        )
    ).all()
    runs = []
    for run, flow_name in rows:
        summary = _run_summary(run)
        summary.flow_name = flow_name
        runs.append(summary)
    return FlowRunListResponse(runs=runs, total=total)


@router.get("/runs/segment-counts", response_model=SegmentCounts)
async def flow_run_segment_counts(
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SegmentCounts:
    """How many flow runs each visibility segment holds under the feed's other filters.

    Declared before ``GET /runs/{run_id}`` so the literal wins the match.
    """
    from app.api.v1.helpers import get_user_group_ids, resolve_list_ownership

    group_ids = await get_user_group_ids(db, user.id)

    async def count(segment: Visibility) -> int:
        query = _global_runs_query(
            user,
            group_ids,
            status_filter=status_filter,
            search=search,
            ownership=resolve_list_ownership(segment, None),
        )
        return (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()

    return await segment_counts(count)


def executions_status(flow_status: str) -> str:
    """A flow-run status in the Executions vocabulary (the inverse of ``_GLOBAL_STATUS_FILTERS``)."""
    for executions_value, flow_statuses in _GLOBAL_STATUS_FILTERS.items():
        if flow_status in flow_statuses:
            return executions_value
    return flow_status


@router.get("/runs/status-counts", response_model=FlowRunStatusCountsResponse)
async def flow_run_status_counts(
    search: str | None = None,
    ownership: str | None = None,
    vis: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FlowRunStatusCountsResponse:
    """How many flow runs each status holds under the feed's other filters (#9378 slice 8f).

    Keyed in the Executions vocabulary (``pending`` counts under ``running``,
    ``awaiting`` under ``awaiting_human``), so it adds to the assignment feed's counts.
    Declared before ``GET /runs/{run_id}`` so the literal wins the match.
    """
    from app.api.v1.helpers import get_user_group_ids, resolve_list_ownership

    ownership = resolve_list_ownership(vis, ownership)
    group_ids = await get_user_group_ids(db, user.id)
    base = _global_runs_query(user, group_ids, status_filter=None, search=search, ownership=ownership).subquery()
    rows = (await db.execute(select(base.c.status, func.count()).group_by(base.c.status))).all()
    counts: dict[str, int] = {}
    for flow_status, n in rows:
        if not flow_status:
            continue
        key = executions_status(str(flow_status))
        counts[key] = counts.get(key, 0) + int(n)
    return FlowRunStatusCountsResponse(counts=counts)


def flow_run_order(sort: str | None) -> tuple[Any, ...]:
    """ORDER BY clauses of the global flow-run feed; the id breaks ties so paging never skips a run."""
    if sort == "oldest":
        return (FlowRun.created_at.asc(), FlowRun.id.asc())
    newest = (FlowRun.created_at.desc(), FlowRun.id.desc())
    if sort == "status":
        return (FlowRun.status.asc(), *newest)
    if sort == "status_desc":
        return (FlowRun.status.desc(), *newest)
    if sort == "agent":
        return (Flow.name.asc().nulls_last(), *newest)
    if sort == "agent_desc":
        return (Flow.name.desc().nulls_last(), *newest)
    duration = FlowRun.completed_at - FlowRun.started_at
    if sort == "longest":
        return (duration.desc().nulls_last(), *newest)
    if sort == "shortest":
        return (duration.asc().nulls_last(), *newest)
    return newest


def _global_runs_query(
    user: User,
    group_ids: list,
    *,
    status_filter: str | None,
    search: str | None,
    ownership: str | None,
):
    """The Executions flow-run feed's query: the list and its segment counts read the same one."""
    query = (
        select(FlowRun, Flow.name)
        .join(Flow, Flow.id == FlowRun.flow_id)
        .where(
            user_visible_filter(FlowRun, user.id, group_ids, is_admin=user.is_admin),
            production_runs_only(),
        )
    )
    if status_filter:
        # One status or several comma-separated (the multi-select Status pill, #9378 slice 8f).
        statuses: list[str] = []
        for value in dict.fromkeys(part.strip() for part in status_filter.split(",") if part.strip()):
            mapped = _GLOBAL_STATUS_FILTERS.get(value)
            if mapped is None:
                if value not in FLOW_RUN_STATUSES:
                    # A typo must read as "bad filter", not as "no runs".
                    raise HTTPException(
                        status_code=422,
                        detail=f"Unknown status filter `{value}`.",
                    )
                mapped = [value]
            statuses.extend(s for s in mapped if s not in statuses)
        if not statuses:
            raise HTTPException(status_code=422, detail="The status filter names no status.")
        query = query.where(FlowRun.status.in_(statuses))
    if search:
        needle = f"%{search}%"
        query = query.where(
            or_(
                Flow.name.ilike(needle),
                FlowRun.result_summary.ilike(needle),
                FlowRun.error.ilike(needle),
            )
        )
    if ownership == "mine":
        query = query.where(FlowRun.user_id == user.id)
    elif ownership in ("shared", "company_managed"):
        query = query.where(
            or_(FlowRun.user_id != user.id, FlowRun.user_id.is_(None))
        )
    elif ownership == "company":
        query = query.where(
            FlowRun.is_company_managed.is_(True),
            or_(FlowRun.user_id != user.id, FlowRun.user_id.is_(None)),
        )
    elif ownership == "shared_only":
        query = query.where(
            FlowRun.is_company_managed.is_not(True),
            or_(FlowRun.user_id != user.id, FlowRun.user_id.is_(None)),
        )
    return query


def awaiting_human(state: dict[str, Any] | None) -> dict[str, Any]:
    """The Ask-user park a person can still answer, else an empty dict.

    A park whose answer was taken (``released``) is not answerable any more:
    the run panel shows no card for it while the worker resumes the run, so a
    second answer never meets the 409 of the respond route (#842, S6).
    """
    return state["waiting"] if state and open_flow_park(state) else {}


@router.get("/runs/{run_id}", response_model=FlowRunDetailResponse)
async def get_flow_run(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FlowRunDetailResponse:
    run = await get_visible_run(db, user, run_id)
    steps = (
        (
            await db.execute(
                select(FlowRunStep)
                .where(FlowRunStep.flow_run_id == run.id)
                .order_by(FlowRunStep.seq)
            )
        )
        .scalars()
        .all()
    )
    summary = _run_summary(run)
    return FlowRunDetailResponse(
        **summary.model_dump(),
        trigger_payload=run.trigger_payload,
        trigger_context=run.trigger_context,
        paused_node_id=(
            (run.state or {}).get("waiting", {}).get("node")
            if ((run.state or {}).get("waiting") or {}).get("kind") == "breakpoint"
            else None
        ),
        step_mode=bool((run.state or {}).get("step_mode")),
        awaiting_input_node_id=awaiting_human(run.state).get("node"),
        awaiting_input_question=awaiting_human(run.state).get("question"),
        awaiting_input_ask=(
            awaiting_human(run.state).get("ask") if isinstance(awaiting_human(run.state).get("ask"), dict) else None
        ),
        steps=[
            FlowRunStepResponse(
                id=str(s.id),
                seq=s.seq,
                node_id=s.node_id,
                node_kind=s.node_kind,
                iteration=s.iteration,
                status=s.status,
                input=s.input,
                output=s.output,
                error=s.error,
                warning=s.warning,
                started_at=s.started_at.isoformat(),
                completed_at=_iso(s.completed_at),
            )
            for s in steps
        ],
    )


@router.post("/runs/{run_id}/cancel", response_model=FlowRunSummaryResponse)
async def cancel_flow_run(
    run_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
) -> FlowRunSummaryResponse:
    """Stop an active run.

    The status flip is what the walker's per-step cooperative check reads; the
    engine cancel frees the reconciler slot for a parked run immediately
    instead of at its next wake.
    """
    run = await _get_controllable_run(db, user, run_id, action="cancel")
    from app.services.flow_runner import stop_flow_run

    stopped = await stop_flow_run(db, run)
    if stopped:
        await log_audit(
            db,
            user=user,
            action="flow.run_cancel",
            entity_type="flow",
            entity_id=run.flow_id,
            summary="Cancelled a flow run",
            details={"run_id": str(run.id)},
            request=request,
        )
    # Before the answer either way: a run that had already ended wrote nothing.
    await db.commit()
    if stopped:
        await db.refresh(run)
        from app.services.flow_run_stream import publish_flow_run_update

        # Other connected viewers see the cancellation immediately — the
        # walker only publishes when its cooperative check catches up.
        await publish_flow_run_update(run)
    return _run_summary(run)
