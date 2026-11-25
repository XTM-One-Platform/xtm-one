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

"""Assignment endpoints – CRUD + trigger + execution logs.

Assignments are automation rules attached to agents. Each assignment has a trigger
(schedule, webhook, integration event, manual) and a prompt that tells the AI
what to do when triggered.
"""

import logging
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Annotated, Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import delete as sql_delete
from sqlalchemy import false, func, or_, select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.a2a.providers import resolve_submitter_providers
from app.a2a.run_exchange import run_exchanges
from app.agents.builtin_agents import (
    builtin_assignment_write_allowed,
    is_admin_opt_in_builtin_assignment,
)
from app.api.deps import (
    get_current_user,
    require_admin,
    require_agentic_ai,
    require_ai_enabled,
    require_chat_enabled,
)
from app.api.v1.binding_access import (
    assert_channel_bot_additions_allowed,
    assert_integration_additions_allowed,
    assert_tool_source_additions_allowed,
    assert_trigger_integration_allowed,
    assert_whitelist_bypass_not_added,
    channel_bot_ids,
)
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    LIST_OWNERSHIP_SEGMENTS,
    EntityListScope,
    assert_writable_agent,
    assert_writable_assignment,
    assignment_visible_filter,
    fleet_filter,
    fleet_operations_filter,
    get_user_agent,
    get_user_assignment,
    get_user_group_ids,
    get_user_integration,
    list_scope_admin_bypass,
    manual_trigger_overlap,
    manual_trigger_refusal,
    manual_trigger_run_scope,
    manual_trigger_skipped,
    openable_parent_runs,
    parent_is_shown,
    parse_path_id,
    parse_uuid,
    require_run_access,
    resolve_list_ownership,
    run_answerable_filter,
    run_visible_filter,
    validate_run_ownership,
    webhook_secret_visible,
    without_hidden_parent,
)
from app.api.v1.schemas.assignment import (
    ApprovalDecision,
    ApproveByToolRequest,
    AssignmentResponse,
    AssignmentRunDetailResponse,
    AssignmentRunResponse,
    AssignmentScheduleResponse,
    BatchApprovalDecision,
    BatchApprovalRequest,
    BatchApprovalResponse,
    BatchApprovalResult,
    BulkCancelRunsRequest,
    BulkCancelRunsResponse,
    BulkDeleteRunsRequest,
    BulkDeleteRunsResponse,
    CreateAssignmentRequest,
    FollowupChatRequest,
    FollowupChatResponse,
    FollowupRunRequest,
    PendingByToolResponse,
    PendingMixedGroup,
    PendingToolSummary,
    ReleaseCoveredRequest,
    RespondToHumanInputRequest,
    RunActionRequestResponse,
    RunCountResponse,
    RunStatusCountsResponse,
    TriggerAssignmentRequest,
    UpdateAssignmentRequest,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.json_sql import when_json_parseable
from app.core.redaction import redact_known_secrets_for_display
from app.core.utils import (
    describe_schedule_next_run,
    ensure_webhook_secret,
    format_exception_message,
    parse_delay,
    safe_uuid,
    schedule_base,
    strip_webhook_secret,
)
from app.llm.base import is_working_file_source
from app.models.agent import Agent
from app.models.assignment import (
    EVENT_MATCHING_TRIGGER_TYPES,
    Assignment,
    AssignmentRun,
    new_trigger_config,
    uses_exact_event_matching,
)
from app.models.general_assistant_config import clamp_max_iterations
from app.models.user import User
from app.services.access_requests import close_run_action_requests, open_run_action_requests
from app.services.assignment_pause import (
    actor_display_name,
    ensure_not_emergency_stopped,
    is_paused_by_deactivated_owner,
    owner_not_deactivated,
    pause_assignment,
    record_pause_audit,
    refuse_if_owner_deactivated,
    resume_assignment,
    switch_off_reason,
)
from app.services.audit import log_audit
from app.services.chat_approval_resume import record_approval_decision, record_refused_decision
from app.services.element_scope import normalize_scope_enforcement
from app.services.organization_context_categories import checked_for_api as _checked_org_categories
from app.services.visibility import SegmentCounts, Visibility, segment_counts
from app.services.wait_settings import checked_wait_policy_override

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _isoformat_or_none(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _redact_trigger_config(trigger_config: dict | None) -> dict | None:
    """Return a copy of ``trigger_config`` safe for audit log details."""
    if not trigger_config or "webhook_secret" not in trigger_config:
        return trigger_config
    return {**trigger_config, "webhook_secret": "***"}


def _visible_trigger_config(
    w: Assignment,
    user: User,
    agent: Agent | None = None,
    user_group_ids: list[str] | None = None,
) -> dict | None:
    """Return ``trigger_config`` as the requesting *user* may see it.

    The ``webhook_secret`` is a credential for inbound webhook deliveries, so
    its visibility follows the *manage* contract (``webhook_secret_visible``):
    admins, owners, and ``config``-tier grantees (group or per-user) keep the
    full config so they can configure the sending system; non-admin viewers
    of company-managed assignments and ``read_only`` grantees get the secret
    stripped.
    """
    config = w.trigger_config
    if (
        isinstance(config, dict)
        and "webhook_secret" in config
        and not webhook_secret_visible(user, w, agent, user_group_ids)
    ):
        return strip_webhook_secret(config)
    return config


async def _resolve_run_as_user_id(
    db: AsyncSession,
    agent: Agent,
    raw_run_as_user_id: str | None,
) -> uuid.UUID | None:
    """Validate and resolve the optional per-assignment "run as" user.

    Only meaningful for company-managed agents — for any other agent the value
    is ignored (returns ``None`` so runs execute as the assignment owner).
    Raises 400 if the provided id is malformed or does not reference a user.
    """
    if not raw_run_as_user_id or not agent.is_company_managed:
        return None
    parsed = parse_uuid(raw_run_as_user_id, label="run_as_user_id")
    exists = await db.execute(select(User.id).where(User.id == parsed))
    if exists.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=400,
            detail="run_as_user_id does not reference a valid user",
        )
    return parsed


#: Most agent ids one run feed read filters by (``agent_ids``). Sized so the
#: comma-separated query string stays well under the 8 KB request line of a
#: default reverse proxy; mirrored by ``MAX_RUN_FILTER_AGENT_IDS`` in
#: ``frontend/src/lib/run-agent-filter.ts``.
MAX_RUN_FILTER_AGENT_IDS = 150
_AGENT_IDS_DESCRIPTION = (
    "Only the runs of these agents, comma-separated or repeated "
    f"(at most {MAX_RUN_FILTER_AGENT_IDS}). Present but empty matches no run."
)


_FLEET_DESCRIPTION = (
    "Only the runs of this fleet's current home members, orchestrator included "
    "and connected agents left out. A value that is not a UUID is a 422."
)

_REVIEW_DESCRIPTION = (
    "'unreviewed': only the runs whose ROI record nobody reviewed yet (#9310). "
    "A run without an ROI record never matches."
)
_REVIEW_PATTERN = "^unreviewed$"

_RUN_SORT_DESCRIPTION = (
    "Order of the runs: 'newest' (default, started date, newest first), "
    "'oldest' (started date, oldest first), 'status' or 'status_desc' (status, then newest first), "
    "'agent' or 'agent_desc' (agent name, then newest first), "
    "'longest' or 'shortest' (duration from start to end, runs without one last)."
)
_RUN_SORT_PATTERN = "^(newest|oldest|status|status_desc|agent|agent_desc|longest|shortest)$"


def run_status_values(status: str) -> list[str]:
    """The statuses a ``status`` filter names: one value, or several comma-separated.

    Blanks are dropped and the order kept, without duplicates; a value that holds no
    status at all (``","``) is the caller's error, never "no filter".
    """
    values = list(dict.fromkeys(part.strip() for part in status.split(",") if part.strip()))
    if not values:
        raise HTTPException(status_code=422, detail="The status filter names no status.")
    return values


def _run_order(sort: str | None) -> tuple[Any, ...]:
    """ORDER BY clauses of a runs list for its ``sort`` parameter (#9365).

    The id breaks ties so paging never skips or repeats a run.
    """
    if sort == "oldest":
        return (AssignmentRun.created_at.asc(), AssignmentRun.id.asc())
    newest = (AssignmentRun.created_at.desc(), AssignmentRun.id.desc())
    if sort == "status":
        return (AssignmentRun.status.asc(), *newest)
    if sort == "status_desc":
        return (AssignmentRun.status.desc(), *newest)
    # The agent's name (#9378 slice 8f): the list query joins Agent already.
    if sort == "agent":
        return (Agent.name.asc().nulls_last(), *newest)
    if sort == "agent_desc":
        return (Agent.name.desc().nulls_last(), *newest)
    duration = AssignmentRun.completed_at - AssignmentRun.started_at
    if sort == "longest":
        return (duration.desc().nulls_last(), *newest)
    if sort == "shortest":
        return (duration.asc().nulls_last(), *newest)
    return newest


def parse_run_agent_ids(values: list[str] | None) -> list[uuid.UUID] | None:
    """The ``agent_ids`` filter of the run feeds, or ``None`` when absent.

    Accepts repeated parameters and comma-separated values alike. A parameter
    that is present but names no id (``?agent_ids=``) is the empty list, which
    matches no run: an entity bound to no agent has no activity, never the
    whole feed. A malformed id is a 422 and more than
    ``MAX_RUN_FILTER_AGENT_IDS`` ids is a 422, never a silently shortened list.
    """
    if values is None:
        return None
    raw = [part.strip() for value in values for part in value.split(",")]
    ids = list(dict.fromkeys(parse_uuid(part, label="agent_ids") for part in raw if part))
    if len(ids) > MAX_RUN_FILTER_AGENT_IDS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"agent_ids accepts at most {MAX_RUN_FILTER_AGENT_IDS} agents, "
                f"got {len(ids)}."
            ),
        )
    return ids


def _filtered_runs_query(
    user: User,
    *,
    status: str | None = None,
    search: str | None = None,
    notifications: bool = False,
    digests: bool = False,
    ownership: str | None = None,
    agent_id: str | None = None,
    agent_ids: list[uuid.UUID] | None = None,
    assignment_id: str | None = None,
    parent_run_id: str | None = "none",
    a2a_client_ids: list[str] | None = None,
    a2a_provider: str | None = None,
    scope: str | None = None,
    fleet: str | None = None,
    review: str | None = None,
):
    """Build a filtered AssignmentRun query with Agent/Assignment joins.

    Shared by list, count, and bulk-delete endpoints so filter semantics
    stay consistent.

    Run visibility is intentionally NARROWER than agent/assignment
    visibility. A group-shared agent is read-only-visible to every group
    member (they can browse its config and assignments), but its *runs* stay
    private to the run owner. A run is openable by its owner, plus — for
    company-managed runs — by everyone read-only (only *mutating* actions on a
    company-managed run are admin-gated; see ``require_run_access`` and
    ``get_run_detail``). Group-shared agent runs are never openable by other
    members, so the list / count / notification surfaces must scope to own +
    company-managed runs only and NEVER include group-shared agent runs. This
    matches the run-detail / descendants / respond / dismiss / followup
    endpoints, which would otherwise 404 on a run that the list had surfaced.

    The ``ownership`` filter splits that visible set by the *run owner*:
    ``"mine"`` = runs whose ``user_id`` is me (including company-managed runs I
    own or triggered), ``"shared"``/``"company_managed"`` = company-managed runs
    owned by someone else (or anonymous), and the default (``"all"``) = the union
    of both. The ``?vis=`` segments (``resolve_list_ownership``) arrive as
    ``"company"`` (the same set as ``"shared"``: company-managed, not mine) and
    ``"shared_only"`` (shared with me, not company-managed): empty for
    assignment runs, whose visible set is own + company-managed only. The
    buckets are a disjoint partition so an agent owner sees their own
    company-managed agent's runs under "My Runs" (and the dashboard Live
    Activity), while other users still find them only under "Shared"/"All".

    An unknown ``ownership`` is a 422 (``validate_run_ownership``): it used to
    fall through every branch and drop the filter, which for the bulk delete
    widened a "delete all matching" to every visible matching run.

    ``scope="all"`` (``EntityListScope``) lifts the ``"all"`` bucket to every
    run for an admin: the Activity tab of an agent or an assignment, whose runs
    the admin opens one by one anyway. ``"mine"`` and ``"shared"`` keep their
    meaning, so for that feed they no longer add up to ``"all"``. It changes
    nothing for anyone else.

    ``fleet`` keeps the runs of the fleet's current home members and the runs
    whose ROI record carries the fleet (members who left included, #9837),
    applied on top of the visibility above like ``agent_ids``; a fleet id that
    is not a UUID is a 422.

    ``review="unreviewed"`` keeps the runs whose ROI record has no outcome yet
    (#9310, ``dev-docs/roi-outcomes.md`` section 4), on top of the rest: it
    only narrows the ``fleet`` population, never selects another one.
    """
    if ownership not in LIST_OWNERSHIP_SEGMENTS:
        ownership = validate_run_ownership(ownership)
    query = (
        select(AssignmentRun)
        .join(Agent, AssignmentRun.agent_id == Agent.id, isouter=True)
        .join(Assignment, AssignmentRun.assignment_id == Assignment.id, isouter=True)
    )

    # Ownership. "mine" and "shared" are a disjoint partition of "all"
    # (== ``run_visible_filter``): every visible run belongs to exactly one
    # bucket, keyed on the run owner, so a run never double-counts nor vanishes.
    if ownership == "mine":
        # "mine" = runs attributed to me by ``user_id`` — INCLUDING runs of a
        # company-managed agent I own. Scheduled / webhook / event runs stamp
        # ``user_id`` with the agent owner (``assignment.user_id``), and a run
        # I triggered manually is stamped with my id, so both are genuinely
        # "mine". The previous ``is_company_managed IS FALSE`` guard wrongly hid
        # an owner's own company-managed agent runs from "My Runs" and the
        # dashboard Live Activity, relegating them to "Shared".
        query = query.where(AssignmentRun.user_id == user.id)
    elif ownership == "shared_only":
        # The Shared segment: shared with me and not company-managed. A run of
        # a group-shared agent stays private to its owner (see above), so no
        # assignment run is ever in it.
        query = query.where(false())
    elif ownership in ("shared", "company_managed", "company"):
        # "shared"/"company_managed" = company-managed runs NOT attributed to me
        # (owned/triggered by someone else, or anonymous). My own company-managed
        # runs live under "mine". A NULL ``user_id`` (anonymous-triggered) counts
        # as "not mine". Group-shared agent runs are deliberately excluded — they
        # belong to the run owner, not the wider group.
        query = query.where(
            AssignmentRun.is_company_managed.is_(True),
            or_(
                AssignmentRun.user_id != user.id,
                AssignmentRun.user_id.is_(None),
            ),
            # The same narrowing as `run_visible_filter` (#11624).
            run_visible_filter(user.id),
        )
    elif not list_scope_admin_bypass(scope, user):
        # "all" — own runs + company-managed runs, for every user and for an
        # admin reading a feed without ``scope=all``: group members never see
        # another member's runs on a group-shared agent.
        query = query.where(run_visible_filter(user.id))

    if agent_id:
        # Support comma-separated list of agent IDs for group filtering
        if "," in agent_id:
            id_list = [aid.strip() for aid in agent_id.split(",") if aid.strip()]
            query = query.where(AssignmentRun.agent_id.in_(id_list))
        else:
            query = query.where(AssignmentRun.agent_id == agent_id)

    if agent_ids is not None:
        # The runs of the agents bound to an entity (its Activity tab). Applied
        # on top of the visibility above, so it only ever narrows the feed.
        query = query.where(AssignmentRun.agent_id.in_(agent_ids) if agent_ids else false())

    from app.models.roi import RoiRunRecord

    fleet_uuid = parse_uuid(fleet, label="fleet") if fleet else None
    if fleet:
        # One fleet population for every value of ``review`` (#9837): the runs
        # of the fleet's current members, plus the runs whose ROI record is
        # stamped with the fleet (``roi_report`` counts those for the review
        # rate, an agent that left the fleet included). ``review`` then only
        # narrows it, so Unreviewed is always a subset of All.
        fleet_records = select(RoiRunRecord.run_id).where(
            RoiRunRecord.run_id.is_not(None),
            RoiRunRecord.fleet_id == fleet_uuid,
        )
        query = query.where(
            or_(
                fleet_operations_filter(fleet, AssignmentRun.agent_id),
                AssignmentRun.id.in_(fleet_records),
            )
        )

    if review == "unreviewed":
        unreviewed = select(RoiRunRecord.run_id).where(
            RoiRunRecord.run_id.is_not(None), RoiRunRecord.outcome.is_(None)
        )
        if fleet_uuid is not None:
            # The unreviewed records of the fleet: exactly what the review rate
            # counts, and inside the fleet population above.
            unreviewed = unreviewed.where(RoiRunRecord.fleet_id == fleet_uuid)
        query = query.where(AssignmentRun.id.in_(unreviewed))

    if assignment_id:
        query = query.where(AssignmentRun.assignment_id == assignment_id)

    if a2a_client_ids is not None:
        # Narrow to work one A2A partner submitted. The registration that made
        # the submission is stamped on the run at creation
        # (``a2a.tasks.create_task``), and ``a2a.providers.partner_client_ids``
        # is the single place that says which registrations belong to an
        # integration — so this and the integration's last-activity read can
        # never disagree about what a partner has done.
        #
        # Filtering on the RUN rather than on bound agents is the whole point:
        # two A2A integrations bound to the same agent share every agent-scoped
        # query, and would otherwise each display the other's traffic as its
        # own.
        #
        # An empty list matches nothing, which is correct — an integration with
        # no registration has submitted nothing. It must never fall through to
        # "no filter", which would show a brand-new partner the entire history
        # of the agents it happens to be bound to.
        #
        # Guarded: ``trigger_context`` is ``json``, and ``->>`` on one row
        # holding a ``\\u0000`` or lone surrogate escape aborted the whole
        # statement, so one bad run took every partner's Activity page down
        # (#11790). Such a row is left out (``core/json_sql.py``).
        query = query.where(
            when_json_parseable(
                AssignmentRun.trigger_context,
                AssignmentRun.trigger_context["client_id"].astext.in_(a2a_client_ids),
            )
        )

    if a2a_provider:
        # The registration alone cannot always settle attribution. A federated
        # caller on an exposure minted by the old explicit toggle deliberately
        # reuses that exposure's OAuth-shaped registration — one partner, one
        # rate budget — so that one id serves both credential forms, and an
        # integration claiming it claims the other partner's submissions too.
        #
        # The credential form that actually verified is stamped on the run
        # (``a2a.tasks.create_task``), so for anything submitted since, this
        # separates them exactly. Runs predating the stamp carry no key and
        # ``->>`` yields NULL for them; they are kept rather than dropped,
        # because the alternative is a partner's history vanishing from a page
        # that showed it yesterday. Their ambiguity is irreducible — nothing
        # recorded which form was presented — and it is confined to legacy
        # exposures.
        query = query.where(
            when_json_parseable(
                AssignmentRun.trigger_context,
                or_(
                    AssignmentRun.trigger_context["a2a_provider"].astext.is_(None),
                    AssignmentRun.trigger_context["a2a_provider"].astext == a2a_provider,
                ),
            )
        )

    if digests:
        query = query.where(
            AssignmentRun.status == "completed",
            AssignmentRun.result_summary.isnot(None),
            AssignmentRun.result_summary != "",
            Assignment.show_in_digest.is_(True),
        )
    elif notifications:
        query = query.where(
            or_(
                AssignmentRun.status == "awaiting_human",
                AssignmentRun.notification_sent.is_(True),
            )
        )
    elif status:
        # One status, or several comma-separated (the Status pill of
        # Operations > Executions is multi-select, #9378 slice 8f).
        statuses = run_status_values(status)
        query = query.where(
            AssignmentRun.status == statuses[0] if len(statuses) == 1 else AssignmentRun.status.in_(statuses)
        )

    if search:
        pattern = f"%{search}%"
        query = query.where(
            or_(
                Agent.name.ilike(pattern),
                Assignment.name.ilike(pattern),
                AssignmentRun.result_summary.ilike(pattern),
            )
        )

    # Filter by parent_run_id for delegation tree navigation:
    #   "none" (default) → top-level runs only (parent_run_id IS NULL)
    #   <uuid> → children of a specific run
    #   "" (empty string) or None → no parent_run_id filter (include all runs)
    if parent_run_id == "none":
        query = query.where(AssignmentRun.parent_run_id.is_(None))
    elif parent_run_id is not None and parent_run_id != "":
        query = query.where(AssignmentRun.parent_run_id == parent_run_id)

    return query


async def _a2a_scope_for(
    db: AsyncSession, user: User, a2a_integration_id: str | None
) -> tuple[list[str] | None, str | None]:
    """``(registrations, provider)`` for *a2a_integration_id*, or ``(None, None)``.

    Both halves are needed and neither is sufficient. The registrations say
    which budget a submission belonged to; the provider says which credential
    form made it, and on a legacy exposure those genuinely differ.

    Wrapper around ``a2a.providers.partner_client_ids`` that first resolves the
    integration through ``get_user_integration``, so an id the caller cannot see
    is a 404 here rather than a filter that quietly matches nothing. The runs
    themselves are already scoped by ``run_visible_filter``, so this is not what
    keeps another tenant's traffic out — it keeps the *answer* honest, since
    "no runs" and "no such integration" are different facts.
    """
    if not a2a_integration_id:
        return None, None

    from app.a2a.providers import A2A_PROVIDERS, partner_client_ids

    user_group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, a2a_integration_id, user_group_ids, is_admin=user.is_admin
    )
    if integration.provider not in A2A_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail="Only A2A integrations have partner activity.",
        )
    return await partner_client_ids(db, integration), integration.provider


# ---------------------------------------------------------------------------
# CRUD – non-parameterised routes first
# ---------------------------------------------------------------------------


@router.get("", response_model=list[AssignmentResponse])
async def list_assignments(
    agent_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # An admin reads every assignment: one agent's (its detail page, opened for
    # any agent) and across agents (Flows > Schedule and admin API clients read
    # the whole platform). The Manual Triggers panel offers only the
    # assignments whose agent its viewer's agent list holds
    # (``lib/manual-triggers.ts``).
    admin_bypass = user.is_admin
    # Group memberships are irrelevant on the bypass path (the response
    # serializer short-circuits on admin too), so skip the DB roundtrip.
    user_group_ids = [] if admin_bypass else await get_user_group_ids(db, user.id)
    query = select(
        Assignment,
        # Read with the row, so the list stays one statement.
        (~owner_not_deactivated()).label("owner_paused"),
    ).where(
        assignment_visible_filter(user.id, user_group_ids, is_admin=admin_bypass)
    )
    if agent_id:
        query = query.where(Assignment.agent_id == agent_id)
    result = await db.execute(query.order_by(Assignment.created_at.desc()))
    pairs = result.all()
    rows = [row for row, _ in pairs]
    owner_paused = {row.id: bool(paused) for row, paused in pairs}

    # ``webhook_secret`` visibility depends on the parent agent's group-share
    # permission tier (``webhook_secret_visible``), so bulk-load the agents of
    # the rows that actually carry a secret and still need the group check
    # (admins always see it; company-managed redacts without the agent;
    # owners always see it).
    agents_by_id: dict[uuid.UUID, Agent] = {}
    if not user.is_admin:
        check_agent_ids = {
            w.agent_id
            for w in rows
            if isinstance(w.trigger_config, dict)
            and "webhook_secret" in w.trigger_config
            and not w.is_company_managed
            and w.user_id != user.id
        }
        if check_agent_ids:
            agent_rows = await db.execute(
                select(Agent).where(Agent.id.in_(check_agent_ids))
            )
            agents_by_id = {a.id: a for a in agent_rows.scalars().all()}

    return [
        _to_response(
            w,
            user=user,
            agent=agents_by_id.get(w.agent_id),
            user_group_ids=user_group_ids,
            owner_paused=owner_paused[w.id],
        )
        for w in rows
    ]


@router.post("", response_model=AssignmentResponse, status_code=status.HTTP_201_CREATED)
async def create_assignment(
    body: CreateAssignmentRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    # Load the parent agent with full three-tier visibility (own + group-shared
    # + company-managed) so admins and group ``config``-tier members can also
    # add assignments — then verify the user can actually mutate it.
    user_group_ids = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(
        db, user.id, body.agent_id, user_group_ids, is_admin=user.is_admin
    )
    # Mirror the UI gates: built-in agents are read-only in XTM One mode,
    # company-managed agents require admin, group-shared agents require
    # ``config`` permission. Without this check the API silently bypassed
    # what the UI enforced.
    assert_writable_agent(agent, user, user_group_ids, action="add an assignment to")
    await assert_tool_source_additions_allowed(
        db,
        user=user,
        agent=agent,
        value=body.tool_sources,
        baseline=None,
        user_group_ids=user_group_ids,
    )
    await assert_integration_additions_allowed(
        db,
        user=user,
        agent=agent,
        value=body.integration_bindings,
        baseline=None,
        user_group_ids=user_group_ids,
    )
    await _assert_channel_override_additions(
        db,
        user=user,
        agent=agent,
        value=body.channel_config,
        baseline=None,
        user_group_ids=user_group_ids,
    )
    await assert_trigger_integration_allowed(
        db,
        user=user,
        value=body.trigger_config,
        baseline=None,
        user_group_ids=user_group_ids,
    )
    from app.services.platform_events import fleet_scope_error

    fleet_error = await fleet_scope_error(body.trigger_type, body.trigger_config, agent.id)
    if fleet_error is not None:
        raise HTTPException(status_code=422, detail=fleet_error)
    from app.services.trigger_provisioning import provision_spec_error

    # A provision block the scheduler pass would refuse is refused now (#970).
    provision_error = provision_spec_error(body.trigger_type, body.trigger_config)
    if provision_error is not None:
        raise HTTPException(status_code=422, detail=provision_error)

    # Resolve the optional "run as" identity (company-managed agents only).
    # The agent is company-managed ⟹ assert_writable_agent above already
    # required admin, so reaching here with a run_as value means the caller is
    # an admin (who has access to the user list).
    run_as_user_id = await _resolve_run_as_user_id(db, agent, body.run_as_user_id)

    assignment_allowed_tools = (
        {k: v for k, v in body.integration_allowed_tools.items() if v is not None} or None
        if isinstance(body.integration_allowed_tools, dict)
        else body.integration_allowed_tools
    )
    from app.services.openapi_import import openapi_integration_ids as _openapi_ids
    from app.services.tool_scope_names import integration_allowed_or_422

    # A tool name its provider does not expose is refused (422 naming the
    # closest valid ones) instead of failing at run time; an ``openapi`` list
    # is checked against the bound OpenAPI integrations.
    _has_openapi_list = isinstance(assignment_allowed_tools, dict) and "openapi" in assignment_allowed_tools
    await integration_allowed_or_422(
        db,
        assignment_allowed_tools,
        _openapi_ids(body.integration_bindings or agent.integration_bindings) if _has_openapi_list else None,
    )
    if _has_openapi_list:
        # A list under the shared ``openapi`` key is read per integration.
        from app.services.openapi_import import (
            openapi_integration_ids,
            per_integration_tool_scopes,
        )

        assignment_allowed_tools = await per_integration_tool_scopes(
            db,
            assignment_allowed_tools,
            openapi_integration_ids(body.integration_bindings or agent.integration_bindings),
        )

    if body.activation_check is not None:
        await _gate_fleet_or_422(db, body.activation_check)
    assignment = Assignment(
        agent_id=agent.id,
        # Assignment ownership tracks the parent *agent's* owner so that the
        # agent owner can always list / manage assignments on their own
        # agent — even when the assignment was created by a group-shared
        # ``config``-tier member or by an admin operating on someone else's
        # private agent.  The owner is ALWAYS preserved (never nulled for
        # company-managed); visibility is driven by ``is_company_managed``.
        user_id=agent.user_id,
        is_company_managed=agent.is_company_managed,
        run_as_user_id=run_as_user_id,
        # Persist the per-user fan-out + run-visibility flags on create too, so the
        # create and update APIs stay consistent (these were previously only
        # applied on update, silently dropped here).
        run_as_all_users=body.run_as_all_users,
        hide_runs=body.hide_runs,
        name=body.name,
        description=body.description,
        trigger_type=body.trigger_type,
        # Webhook assignments are born with a server-generated secret when the
        # caller did not provide one (SEC-5) — the endpoint would otherwise be
        # authenticated by URL path only. A new trigger matches its event
        # exactly.
        trigger_config=ensure_webhook_secret(
            body.trigger_type, new_trigger_config(body.trigger_type, body.trigger_config)
        ),
        prompt=body.prompt,
        activation_prompt=body.activation_prompt,
        activation_check=body.activation_check,
        delay=body.delay,
        tool_sources=body.tool_sources,
        integration_bindings=body.integration_bindings,
        integration_allowed_tools=assignment_allowed_tools,
        builtin_allowed_tools=_builtin_allowed_or_422(body.builtin_allowed_tools),
        integration_element_bindings=body.integration_element_bindings,
        integration_element_enforcement=normalize_scope_enforcement(
            body.integration_element_enforcement, body.integration_element_bindings
        ),
        channel_config=body.channel_config,
        use_knowledge_bases=body.use_knowledge_bases,
        # Empty read selection is normalized to NULL (= inherit all agent KBs);
        # there is no "read nothing" state.
        read_knowledge_base_ids=body.read_knowledge_base_ids or None,
        write_knowledge_base_ids=body.write_knowledge_base_ids,
        # Runner refinement keeps the None-vs-[] distinction: None = inherit the
        # agent's runners, [] = explicitly none, subset = refine.
        runner_ids=body.runner_ids,
        write_knowledge_base_enabled=body.write_knowledge_base_enabled,
        target_knowledge_base_id=(
            uuid.UUID(body.target_knowledge_base_id)
            if body.target_knowledge_base_id
            else None
        ),
        ai_label=body.ai_label,
        ai_label_prompt=body.ai_label_prompt,
        notify_on_complete=body.notify_on_complete,
        show_in_digest=body.show_in_digest,
        human_in_the_loop=body.human_in_the_loop,
        human_in_the_loop_prompt=body.human_in_the_loop_prompt,
        # null = inherit (agent override -> platform default). A real value is
        # clamped to the shared ceiling so a direct API call can't exceed the
        # UI slider.
        max_iterations=(
            clamp_max_iterations(body.max_iterations)
            if body.max_iterations is not None
            else None
        ),
        wait_policy=checked_wait_policy_override(body.wait_policy),
        organization_context_mode=body.organization_context_mode or "inherit",
        organization_context_categories=_checked_org_categories(body.organization_context_categories),
        timeout_seconds=body.timeout_seconds,
        # dry_run was accepted by the schema but silently dropped here, so an
        # assignment could not be *created* in dry-run mode via the API — only
        # flipped afterwards with an update.
        dry_run=body.dry_run,
        ignore_self_triggered_events=body.ignore_self_triggered_events,
        retry_on_failure=body.retry_on_failure,
        max_retries=body.max_retries,
        manual_trigger_parameters=body.manual_trigger_parameters,
        enabled=body.enabled,
    )
    # An API client may create an assignment already switched off
    # (``enabled: false``); the current UI never does, but the field is
    # client-settable. Stamp it like the duplicate / import / catalog paths,
    # or the banner tells the user it was "paused before pause tracking was
    # added" about a row created seconds ago.
    if not assignment.enabled:
        pause_assignment(
            assignment,
            reason="created_disabled",
            details=(
                f"Created switched off via the API by "
                f"{actor_display_name(user)}. Review the configuration, then "
                "enable it."
            ),
            actor_user_id=user.id,
            actor_label=actor_display_name(user),
        )
    db.add(assignment)
    # Smart default: a work_item-trigger assignment is its agent's inbox
    # worker, which almost always needs to manage its queue (close / claim /
    # list / create). Auto-enable the ``work_items`` built-in tool on the agent
    # so it can actually close items, etc. Added once here (not forced on every
    # save), so it can be unchecked later if the agent only relays.
    if body.trigger_type == "work_item":
        _bt_ids = list(agent.builtin_tool_ids or [])
        if "work_items" not in _bt_ids:
            agent.builtin_tool_ids = [*_bt_ids, "work_items"]
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="assignment",
        entity_id=assignment.id,
        entity_name=assignment.name,
        summary=f"Created assignment '{assignment.name}'",
        details={
            "agent_id": str(assignment.agent_id),
            "trigger_type": assignment.trigger_type,
            "trigger_config": _redact_trigger_config(assignment.trigger_config),
            "enabled": assignment.enabled,
            "delay": assignment.delay,
            "ai_label": assignment.ai_label,
            "human_in_the_loop": assignment.human_in_the_loop,
        },
        request=request,
    )
    # Commit HERE, not on ``get_db`` teardown, which runs after the response is
    # sent: the agent page refetches its assignment list the moment this
    # answers, and a refetch that beats the commit reads the list without the
    # new row and keeps showing it for as long as the cache is fresh.
    await db.commit()
    return _to_response(
        assignment,
        user=user,
        agent=agent,
        user_group_ids=user_group_ids,
        owner_paused=await _owner_paused(db, assignment, user),
    )


def _may_decide(run: AssignmentRun, user: User | None) -> bool:
    """Whether *user* may decide *run*'s proposals, by the endpoint's own rule.

    Delegates to ``require_run_access`` rather than restating it: the two must
    never drift, or the serializers would hand back what the endpoint refuses.
    """
    if user is None:
        return False
    try:
        require_run_access(run, user, action="respond to")
    except HTTPException:
        return False
    return True


def _visible_pending_approvals(run: AssignmentRun, user: User | None) -> list | None:
    """``run.pending_approvals`` reduced to what *user* may see.

    Seeing what a gated call would do is the decider's privilege — the same one
    ``require_run_access`` guards on ``GET /pending-approvals``. These
    serializers are reachable by anyone who can see the run, which on a
    company-managed run is every authenticated user, so returning the raw
    column here handed every tenant member the full arguments of every gated
    call. Redacts rather than drops: the counts and tool names the run lists
    render are not the secret, the arguments are.

    Fail-closed on ``user=None``: a caller that cannot say who is asking does
    not get the payload.
    """
    from app.tools.approval import redact_proposals

    proposals = run.pending_approvals
    if not proposals or _may_decide(run, user):
        return proposals
    return redact_proposals(proposals)


# ---------------------------------------------------------------------------
# Runs – /runs/* routes MUST come before /{assignment_id} to avoid conflicts
# ---------------------------------------------------------------------------


def _visible_descendants(root_ids: list[uuid.UUID], user: User):
    """``(root_id, id)`` of every run below each of *root_ids* that *user* may
    open: what ``/descendants`` lists and what both ``child_run_count`` badges
    count, so a badge never counts a run the list does not show.

    The walk goes down ``parent_run_id`` through every row, visible or not, so
    a visible run below a hidden one is still reached; the rows are then held
    to ``run_visible_filter``, which an admin skips like on the run page.
    ``UNION``, not ``UNION ALL``: the column is a plain foreign key, so nothing
    stops a corrupted chain from looping back, and the walk must end there; a
    root reached again through such a loop is not its own descendant.
    """
    tree = (
        select(AssignmentRun.parent_run_id.label("root_id"), AssignmentRun.id.label("id"))
        .where(AssignmentRun.parent_run_id.in_(root_ids))
        .cte("descendant_tree", recursive=True)
    )
    tree = tree.union(
        select(tree.c.root_id, AssignmentRun.id).join(
            tree, AssignmentRun.parent_run_id == tree.c.id
        )
    )
    query = (
        select(tree.c.root_id, tree.c.id)
        .join(AssignmentRun, AssignmentRun.id == tree.c.id)
        .where(tree.c.id != tree.c.root_id)
    )
    if not user.is_admin:
        query = query.where(run_visible_filter(user.id))
    return query


async def _hides_parent_filter(db: AsyncSession, parent_run_id: str | None, user: User) -> bool:
    """Whether a ``parent_run_id=<id>`` filter names a run *user* cannot open.

    Such a filter matches nothing: listing or counting the children of a
    parent the caller may not open would confirm the link every run read
    hides from them. ``"none"`` / empty are not an id and pass; a
    malformed id matches nothing, for an admin too.
    """
    if parent_run_id in (None, "", "none"):
        return False
    parent = safe_uuid(parent_run_id)
    if parent is None:
        return True
    if user.is_admin:
        return False
    return parent not in await openable_parent_runs(db, {parent}, user.id, is_admin=False)


@router.get("/runs/all", response_model=list[AssignmentRunDetailResponse])
async def list_all_runs(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = None,
    search: str | None = None,
    notifications: bool = False,
    digests: bool = False,
    ownership: str | None = None,
    vis: str | None = None,
    agent_id: str | None = None,
    agent_ids: list[str] | None = Query(None, description=_AGENT_IDS_DESCRIPTION),
    assignment_id: str | None = None,
    parent_run_id: str | None = "none",
    a2a_integration_id: str | None = None,
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    fleet: Annotated[str | None, Query(description=_FLEET_DESCRIPTION)] = None,
    review: Annotated[str | None, Query(description=_REVIEW_DESCRIPTION, pattern=_REVIEW_PATTERN)] = None,
    sort: Annotated[str | None, Query(description=_RUN_SORT_DESCRIPTION, pattern=_RUN_SORT_PATTERN)] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all assignment runs across all assignments, with agent/assignment names.

    Includes company-managed runs (``is_company_managed``) so all users
    can see shared agent activity alongside their own.

    Query params:
      - status: filter by run status (running, completed, failed, awaiting_human)
      - search: case-insensitive search across agent name, assignment name, and result summary
      - notifications: if true, return only notification-worthy runs
        (awaiting_human OR notify_on_complete with a result)
      - digests: if true, return only digest-worthy runs (completed with a
        summary on a assignment that has show_in_digest enabled)
      - vis: mine | shared | company | all, the list pages' segment
        (``resolve_list_ownership``); wins over ``ownership``
      - ownership: [DEPRECATED] "mine" for runs attributed to me (own +
        company-managed runs I own or triggered), "shared"/"company_managed"
        for company-managed runs owned by others, omit (or "all") for both
        (own + all company-managed); any other value is a 422
      - agent_id: filter by a specific agent ID
      - agent_ids: only the runs of these agents (an entity's Activity tab);
        present but empty matches nothing
      - fleet: only the runs of the fleet's current home members
      - a2a_integration_id: return only work an A2A partner submitted through
        that integration. Scoped by the calling registration stamped on each
        run, NOT by the agents the integration is bound to — two A2A
        integrations on one agent would otherwise each report the other's
        traffic
      - scope: "all" lists every run for an admin (an entity's Activity tab),
        the default is the own + company-managed feed for everyone
      - offset: number of rows to skip (for pagination)
    """
    # Before the A2A scope lookup, which can reach the database: a bad filter
    # is the caller's error, answered the same way on every variant.
    ownership = resolve_list_ownership(vis, ownership)
    run_agent_ids = parse_run_agent_ids(agent_ids)
    a2a_client_ids, a2a_provider = await _a2a_scope_for(db, user, a2a_integration_id)
    if await _hides_parent_filter(db, parent_run_id, user):
        return []
    query = (
        _filtered_runs_query(
            user,
            status=status,
            search=search,
            notifications=notifications,
            digests=digests,
            ownership=ownership,
            agent_id=agent_id,
            agent_ids=run_agent_ids,
            assignment_id=assignment_id,
            parent_run_id=parent_run_id,
            a2a_client_ids=a2a_client_ids,
            a2a_provider=a2a_provider,
            scope=scope,
            fleet=fleet,
            review=review,
        )
        .order_by(*_run_order(sort))
        .offset(offset)
        .limit(limit)
    )

    result = await db.execute(query)
    runs = result.scalars().all()

    # Batch load agents and assignments for response building
    agent_ids = {r.agent_id for r in runs}
    wf_ids = {r.assignment_id for r in runs if r.assignment_id}
    run_ids = [r.id for r in runs]

    agents_result = await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
    agents_map = {a.id: a for a in agents_result.scalars().all()}

    wfs_map: dict = {}
    if wf_ids:
        wfs_result = await db.execute(
            select(Assignment).where(Assignment.id.in_(wf_ids))
        )
        wfs_map = {w.id: w for w in wfs_result.scalars().all()}

    # Total descendant counts (the whole chain, not just direct children) for
    # the delegation badge, in one query for the page: each run is counted
    # the rows ``/descendants`` would list the caller under it.
    child_counts: dict[uuid.UUID, int] = {}
    if run_ids:
        visible = _visible_descendants(run_ids, user).subquery()
        desc_count_result = await db.execute(
            select(visible.c.root_id, func.count()).group_by(visible.c.root_id)
        )
        for root_id, count in desc_count_result.all():
            child_counts[root_id] = count

    # Batch load parent agents for delegation runs (runs with parent_run_id)
    # that the caller may open: a parent they cannot open is not named.
    parent_run_agent_ids = await openable_parent_runs(
        db,
        {r.parent_run_id for r in runs if r.parent_run_id},
        user.id,
        is_admin=user.is_admin,
    )
    parent_agent_map: dict[uuid.UUID, Agent] = {}  # parent_run_id → Agent
    if parent_run_agent_ids:
        # Load the agents for those parent runs
        extra_agent_ids = set(parent_run_agent_ids.values()) - set(agents_map.keys())
        if extra_agent_ids:
            extra_result = await db.execute(
                select(Agent).where(Agent.id.in_(extra_agent_ids))
            )
            for a in extra_result.scalars().all():
                agents_map[a.id] = a
        for pr_id, pr_agent_id in parent_run_agent_ids.items():
            if pr_agent_id in agents_map:
                parent_agent_map[pr_id] = agents_map[pr_agent_id]

    a2a_providers = await resolve_submitter_providers(
        db, {r.id: r.trigger_context for r in runs}
    )
    a2a_exchanges = await run_exchanges(
        db, runs, user_id=user.id, is_admin=user.is_admin
    )

    items: list[AssignmentRunDetailResponse] = []
    for r in runs:
        ag = agents_map.get(r.agent_id)
        wf = wfs_map.get(r.assignment_id) if r.assignment_id else None
        parent_id = r.parent_run_id if r.parent_run_id in parent_run_agent_ids else None
        parent_ag = parent_agent_map.get(parent_id) if parent_id else None
        items.append(
            AssignmentRunDetailResponse(
                id=str(r.id),
                assignment_id=str(r.assignment_id) if r.assignment_id else None,
                assignment_name=wf.name if wf else None,
                agent_id=str(r.agent_id),
                agent_name=ag.name if ag else "Unknown",
                agent_icon=ag.icon if ag else "Bot",
                status=r.status,
                is_company_managed=r.is_company_managed,
                notification_sent=r.notification_sent,
                notify_on_complete=wf.notify_on_complete if wf else False,
                show_in_digest=wf.show_in_digest if wf else False,
                parent_run_id=str(parent_id) if parent_id else None,
                child_run_count=child_counts.get(r.id, 0),
                parent_agent_name=parent_ag.name if parent_ag else None,
                parent_agent_icon=parent_ag.icon if parent_ag else None,
                trigger_payload=without_hidden_parent(
                    r.trigger_payload,
                    parent_is_shown(r, parent_run_agent_ids, is_admin=user.is_admin),
                ),
                trigger_context=r.trigger_context,
                a2a_provider=a2a_providers.get(r.id),
                a2a_exchange=a2a_exchanges.get(r.id),
                actions_taken=r.actions_taken,
                result_summary=r.result_summary,
                error=r.error,
                pending_approvals=_visible_pending_approvals(r, user),
                approval_answered=bool(r.approval_decisions),
                failure_cause=r.failure_cause,
                failure_determination=r.failure_determination,
                stop_reason=r.stop_reason,
                human_input_question=r.human_input_question,
                human_input_response=r.human_input_response,
                human_input_history=r.human_input_history,
                langfuse_trace_id=r.langfuse_trace_id,
                llm_trace_id=str(r.llm_trace_id) if r.llm_trace_id else None,
                total_input_tokens=r.total_input_tokens,
                total_output_tokens=r.total_output_tokens,
                started_at=r.started_at.isoformat() if r.started_at else None,
                completed_at=r.completed_at.isoformat() if r.completed_at else None,
                created_at=r.created_at.isoformat(),
            )
        )
    return items


@router.get("/runs/count", response_model=RunCountResponse)
async def count_runs(
    status: str | None = None,
    search: str | None = None,
    notifications: bool = False,
    digests: bool = False,
    ownership: str | None = None,
    vis: str | None = None,
    agent_id: str | None = None,
    agent_ids: list[str] | None = Query(None, description=_AGENT_IDS_DESCRIPTION),
    assignment_id: str | None = None,
    parent_run_id: str | None = "none",
    a2a_integration_id: str | None = None,
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    fleet: Annotated[str | None, Query(description=_FLEET_DESCRIPTION)] = None,
    review: Annotated[str | None, Query(description=_REVIEW_DESCRIPTION, pattern=_REVIEW_PATTERN)] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the total number of runs matching the given filters.

    Accepts the same filter parameters as the list endpoint.  Used by the
    frontend to show "Select all N matching runs" in the bulk-selection UI
    and for pagination totals.
    """
    # Before the A2A scope lookup, which can reach the database: a bad filter
    # is the caller's error, answered the same way on every variant.
    ownership = resolve_list_ownership(vis, ownership)
    run_agent_ids = parse_run_agent_ids(agent_ids)
    a2a_client_ids, a2a_provider = await _a2a_scope_for(db, user, a2a_integration_id)
    if await _hides_parent_filter(db, parent_run_id, user):
        return RunCountResponse(count=0)
    base = _filtered_runs_query(
        user,
        status=status,
        search=search,
        notifications=notifications,
        digests=digests,
        ownership=ownership,
        agent_id=agent_id,
        agent_ids=run_agent_ids,
        assignment_id=assignment_id,
        parent_run_id=parent_run_id,
        a2a_client_ids=a2a_client_ids,
        a2a_provider=a2a_provider,
        scope=scope,
        fleet=fleet,
        review=review,
    )
    count_result = await db.execute(select(func.count()).select_from(base.subquery()))
    return RunCountResponse(count=count_result.scalar_one())


@router.get("/runs/segment-counts", response_model=SegmentCounts)
async def run_segment_counts(
    status: str | None = None,
    search: str | None = None,
    notifications: bool = False,
    digests: bool = False,
    agent_id: str | None = None,
    agent_ids: list[str] | None = Query(None, description=_AGENT_IDS_DESCRIPTION),
    assignment_id: str | None = None,
    parent_run_id: str | None = "none",
    a2a_integration_id: str | None = None,
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    fleet: Annotated[str | None, Query(description=_FLEET_DESCRIPTION)] = None,
    review: Annotated[str | None, Query(description=_REVIEW_DESCRIPTION, pattern=_REVIEW_PATTERN)] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SegmentCounts:
    """How many runs each visibility segment holds under the feed's other filters.

    The filters of ``GET /runs/count``, each segment counted as ``?vis=`` lists it.
    Declared before ``GET /runs/{run_id}`` so the literal wins the match.
    """
    run_agent_ids = parse_run_agent_ids(agent_ids)
    a2a_client_ids, a2a_provider = await _a2a_scope_for(db, user, a2a_integration_id)
    hidden = await _hides_parent_filter(db, parent_run_id, user)

    async def count(segment: Visibility) -> int:
        if hidden:
            return 0
        base = _filtered_runs_query(
            user,
            status=status,
            search=search,
            notifications=notifications,
            digests=digests,
            ownership=resolve_list_ownership(segment, None),
            agent_id=agent_id,
            agent_ids=run_agent_ids,
            assignment_id=assignment_id,
            parent_run_id=parent_run_id,
            a2a_client_ids=a2a_client_ids,
            a2a_provider=a2a_provider,
            scope=scope,
            fleet=fleet,
            review=review,
        )
        return (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()

    return await segment_counts(count)


@router.get("/runs/status-counts", response_model=RunStatusCountsResponse)
async def run_status_counts(
    search: str | None = None,
    ownership: str | None = None,
    vis: str | None = None,
    agent_id: str | None = None,
    agent_ids: list[str] | None = Query(None, description=_AGENT_IDS_DESCRIPTION),
    assignment_id: str | None = None,
    parent_run_id: str | None = "none",
    a2a_integration_id: str | None = None,
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    fleet: Annotated[str | None, Query(description=_FLEET_DESCRIPTION)] = None,
    review: Annotated[str | None, Query(description=_REVIEW_DESCRIPTION, pattern=_REVIEW_PATTERN)] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunStatusCountsResponse:
    """How many runs each status holds under the feed's other filters (#9378 slices 8f and 4a).

    The counts of the Status pill of Operations > Executions and of every runs
    Activity tab: the filters of ``GET /runs/count`` without ``status``, one GROUP BY.
    A status with no run is absent. Declared before ``GET /runs/{run_id}`` so the
    literal wins the match.
    """
    ownership = resolve_list_ownership(vis, ownership)
    run_agent_ids = parse_run_agent_ids(agent_ids)
    a2a_client_ids, a2a_provider = await _a2a_scope_for(db, user, a2a_integration_id)
    if await _hides_parent_filter(db, parent_run_id, user):
        return RunStatusCountsResponse(counts={})
    base = _filtered_runs_query(
        user,
        search=search,
        ownership=ownership,
        agent_id=agent_id,
        agent_ids=run_agent_ids,
        assignment_id=assignment_id,
        parent_run_id=parent_run_id,
        a2a_client_ids=a2a_client_ids,
        a2a_provider=a2a_provider,
        scope=scope,
        fleet=fleet,
        review=review,
    ).subquery()
    rows = (await db.execute(select(base.c.status, func.count()).group_by(base.c.status))).all()
    return RunStatusCountsResponse(counts={str(status_value): int(n) for status_value, n in rows if status_value})


@router.get("/runs/active-agents", response_model=list[str])
async def get_active_agent_ids(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return agent IDs that are currently active (assignment runs OR chat processing).

    Merges two sources:
    1. **Assignment runs**: agents with running/pending/delayed AssignmentRun records (DB)
    2. **Chat activity**: agents currently processing a chat message (Redis keys
       ``chat:active:{agent_id}`` set by ``run_agent_chat``).

    Lightweight endpoint polled by the frontend to show a "Running" spinner
    on agent cards, overview badges, and flow graphs. An admin gets every
    agent with a live run, so a colleague's agent they open shows its status.
    """
    # 1) Assignment-based activity (DB)
    active_runs = select(AssignmentRun.agent_id).where(
        AssignmentRun.status.in_(["running", "pending", "delayed"])
    )
    if not user.is_admin:
        active_runs = active_runs.where(run_visible_filter(user.id))
    result = await db.execute(active_runs.group_by(AssignmentRun.agent_id))
    active_ids: set[str] = {str(row[0]) for row in result.all()}

    # 2) Chat-based activity (Redis) — use SCAN to avoid blocking on large keyspaces
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        async for key in redis.scan_iter(match="chat:active:*", count=100):
            # key format: "chat:active:<uuid>"
            agent_id = key.rsplit(":", 1)[-1]
            active_ids.add(agent_id)
    except Exception:
        logger.warning("Redis chat-activity lookup failed", exc_info=True)

    return list(active_ids)


@router.get(
    "/runs/{run_id}/descendants", response_model=list[AssignmentRunDetailResponse]
)
async def get_run_descendants(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return ALL descendants of a run as a flat list (recursive).

    Uses a recursive CTE to walk the delegation chain:
    parent → child → grandchild → … all returned at the same level,
    ordered by created_at so the delegation sequence is chronological.

    Each row carries its ``parent_run_id``: the run page and the Activity tab
    rebuild the tree from it (``orderRunTree`` in ``frontend/src/lib/run-tree.ts``).
    A non-admin is listed only the rows ``run_visible_filter`` lets them open.
    """
    root_uuid = parse_path_id(run_id, not_found_detail="Run not found")

    # Verify the root run is accessible (admins can view any run)
    root_query = select(AssignmentRun.id).where(AssignmentRun.id == root_uuid)
    if not user.is_admin:
        root_query = root_query.where(run_visible_filter(user.id))
    root_result = await db.execute(root_query)
    if not root_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Run not found")

    # Each descendant under the rule the run page opens it by: a row the
    # caller could not open is not listed (its visible descendants hang under
    # the root, ``orderRunTree``).
    visible = _visible_descendants([root_uuid], user).subquery()
    runs_result = await db.execute(
        select(AssignmentRun)
        .join(visible, visible.c.id == AssignmentRun.id)
        .order_by(AssignmentRun.created_at.asc())
    )
    runs = list(runs_result.scalars().all())
    if not runs:
        return []

    # Batch-load agents, assignments, parent agents
    agent_ids = {r.agent_id for r in runs}
    agents_result = await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
    agents_map = {a.id: a for a in agents_result.scalars().all()}

    wf_ids = {r.assignment_id for r in runs if r.assignment_id}
    wfs_map: dict[uuid.UUID, Assignment] = {}
    if wf_ids:
        wf_result = await db.execute(
            select(Assignment).where(Assignment.id.in_(wf_ids))
        )
        wfs_map = {w.id: w for w in wf_result.scalars().all()}

    # A parent the caller could not open is not named: its id, agent name and
    # icon are left out, and the row hangs under the root (``orderRunTree``).
    openable_ids = {r.id for r in runs} | {root_uuid}
    shown_parent: dict[uuid.UUID, uuid.UUID | None] = {
        r.id: (
            r.parent_run_id
            if user.is_admin or r.parent_run_id in openable_ids
            else None
        )
        for r in runs
    }
    parent_run_ids = {pid for pid in shown_parent.values() if pid}
    parent_agent_map: dict[uuid.UUID, Agent] = {}
    if parent_run_ids:
        pr_result = await db.execute(
            select(AssignmentRun.id, AssignmentRun.agent_id).where(
                AssignmentRun.id.in_(parent_run_ids)
            )
        )
        pr_agent_ids: dict[uuid.UUID, uuid.UUID] = {}
        for pr_id, pr_agent_id in pr_result.all():
            pr_agent_ids[pr_id] = pr_agent_id
        unique_pa_ids = set(pr_agent_ids.values()) - agent_ids
        if unique_pa_ids:
            extra = await db.execute(select(Agent).where(Agent.id.in_(unique_pa_ids)))
            for a in extra.scalars().all():
                agents_map[a.id] = a
        for prid, paid in pr_agent_ids.items():
            if paid in agents_map:
                parent_agent_map[prid] = agents_map[paid]

    a2a_providers = await resolve_submitter_providers(
        db, {r.id: r.trigger_context for r in runs}
    )
    a2a_exchanges = await run_exchanges(
        db, runs, user_id=user.id, is_admin=user.is_admin
    )

    responses = []
    for r in runs:
        ag = agents_map.get(r.agent_id)
        wf = wfs_map.get(r.assignment_id) if r.assignment_id else None
        parent_id = shown_parent[r.id]
        parent_ag = parent_agent_map.get(parent_id) if parent_id else None
        responses.append(
            AssignmentRunDetailResponse(
                id=str(r.id),
                assignment_id=str(r.assignment_id) if r.assignment_id else None,
                assignment_name=wf.name if wf else None,
                agent_id=str(r.agent_id),
                agent_name=ag.name if ag else "Unknown",
                agent_icon=ag.icon if ag else "bot",
                status=r.status,
                is_company_managed=r.is_company_managed,
                notification_sent=r.notification_sent,
                parent_run_id=str(parent_id) if parent_id else None,
                child_run_count=0,
                parent_agent_name=parent_ag.name if parent_ag else None,
                parent_agent_icon=parent_ag.icon if parent_ag else None,
                trigger_payload=without_hidden_parent(
                    r.trigger_payload,
                    parent_is_shown(r, openable_ids, is_admin=user.is_admin),
                ),
                trigger_context=r.trigger_context,
                a2a_provider=a2a_providers.get(r.id),
                a2a_exchange=a2a_exchanges.get(r.id),
                actions_taken=r.actions_taken,
                result_summary=r.result_summary,
                error=r.error,
                pending_approvals=_visible_pending_approvals(r, user),
                approval_answered=bool(r.approval_decisions),
                failure_cause=r.failure_cause,
                failure_determination=r.failure_determination,
                stop_reason=r.stop_reason,
                human_input_question=r.human_input_question,
                human_input_response=r.human_input_response,
                human_input_history=r.human_input_history,
                langfuse_trace_id=r.langfuse_trace_id,
                llm_trace_id=str(r.llm_trace_id) if r.llm_trace_id else None,
                total_input_tokens=r.total_input_tokens,
                total_output_tokens=r.total_output_tokens,
                created_at=r.created_at.isoformat() if r.created_at else "",
                started_at=r.started_at.isoformat() if r.started_at else None,
                completed_at=r.completed_at.isoformat() if r.completed_at else None,
            )
        )

    return responses


@router.get("/runs/{run_id}", response_model=AssignmentRunDetailResponse)
async def get_run_detail(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single assignment run with full details.

    Admins can view any run. Non-admins can view their own runs and
    company-managed runs (``is_company_managed`` — runs always keep an owning
    ``user_id``, so company-managed is a flag, not a NULL owner).
    """
    uid = parse_path_id(run_id, not_found_detail="Run not found")
    query = select(AssignmentRun).where(AssignmentRun.id == uid)
    if not user.is_admin:
        query = query.where(run_visible_filter(user.id))
    result = await db.execute(query)
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")

    agent_result = await db.execute(select(Agent).where(Agent.id == r.agent_id))
    ag = agent_result.scalar_one_or_none()
    wf = None
    if r.assignment_id:
        wf_result = await db.execute(
            select(Assignment).where(Assignment.id == r.assignment_id)
        )
        wf = wf_result.scalar_one_or_none()

    # Load parent agent info for delegation runs, for a parent the caller may
    # open: one they cannot open is neither linked nor named.
    parent_ag = None
    parent_id = None
    openable: dict[uuid.UUID, uuid.UUID] = {}
    if r.parent_run_id:
        openable = await openable_parent_runs(
            db, {r.parent_run_id}, user.id, is_admin=user.is_admin
        )
        if r.parent_run_id in openable:
            parent_id = r.parent_run_id
            pa_result = await db.execute(select(Agent).where(Agent.id == openable[parent_id]))
            parent_ag = pa_result.scalar_one_or_none()

    # Every descendant (the whole chain, not just direct children) the
    # Sub-Runs list shows the caller.
    desc_count = await db.execute(
        select(func.count()).select_from(_visible_descendants([r.id], user).subquery())
    )
    child_run_count = desc_count.scalar_one()

    # Extract the rendered prompt from the stored messages (first user-role message)
    rendered_prompt: str | None = None
    agentic_steps: list[dict] | None = None
    if r.messages:
        for msg in r.messages:
            if isinstance(msg, dict) and msg.get("role") == "user":
                rendered_prompt = msg.get("content")
                break

        # Build agentic steps from the stored conversation.
        # Steps are: assistant messages (with tool_calls) paired with their
        # tool results, plus the final assistant response.
        steps = []
        for msg in r.messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    steps.append(
                        {
                            "type": "tool_calls",
                            "content": msg.get("content", ""),
                            "tool_calls": tool_calls,
                        }
                    )
                else:
                    # Final response (no tool calls)
                    steps.append(
                        {
                            "type": "response",
                            "content": msg.get("content", ""),
                        }
                    )
            elif role == "tool":
                steps.append(
                    {
                        "type": "tool_result",
                        "name": msg.get("name", ""),
                        "tool_call_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }
                )
        if steps:
            agentic_steps = steps

    _attachments = None
    # Only surface attachment metadata + download URLs to viewers who can
    # actually download the files. The download guard on
    # ``GET /chat/files/{id}/download`` (``_authorize_file_access``) gates
    # run-scoped files through ``require_run_access``: admins for
    # company-managed runs (``is_company_managed``), the owner for personal
    # runs. Under the always-set-owner invariant a company-managed run now
    # carries a run-as / triggering ``user_id`` (often a non-admin), so the
    # gate MUST mirror ``require_run_access`` exactly — keying on
    # ``r.user_id == user.id`` alone would leak filename / content-type / size
    # metadata (and hand out always-403 links) to that non-admin. A non-admin
    # can reach THIS endpoint for a company-managed run (the run query allows
    # company-managed rows), but every one of
    # those download URLs would 403 for them — so returning the metadata
    # here would both leak filename/content_type/size and hand out
    # always-broken links. Mirror the download contract exactly: surface
    # attachments only to an admin, or to the owner of a NON-company-managed
    # (personal) run.
    if user.is_admin or (not r.is_company_managed and r.user_id == user.id):
        try:
            from app.models.file_attachment import FileAttachment

            _att_result = await db.execute(
                select(FileAttachment)
                .where(FileAttachment.assignment_run_id == r.id)
                .order_by(FileAttachment.created_at, FileAttachment.id)
            )
            _att_rows = list(_att_result.scalars().all())
            if _att_rows:
                _attachments = [
                    {
                        "file_id": str(att.id),
                        "filename": att.filename,
                        "content_type": att.content_type,
                        "size": att.size,
                        "download_url": f"/api/v1/chat/files/{att.id}/download",
                        # Mirror ``attachment_meta_from_file_attachment`` in
                        # ``chat.py``: ``source`` is a provenance category, not
                        # a display tag. ``generate_file`` persists
                        # ``agent_working`` for scratch artifacts, a check's
                        # picture is ``agent_check``; everything
                        # else (``agent_download``, ``agent_generated``, …) is a
                        # user-facing deliverable. Surface the ``file_tag`` the
                        # frontend ``FileChip`` uses so working files render as
                        # compact chips instead of prominent download cards.
                        "file_tag": (
                            "working_file"
                            if is_working_file_source(getattr(att, "source", None))
                            else "download_file"
                        ),
                        "created_at": _isoformat_or_none(getattr(att, "created_at", None)),
                        "sha256": getattr(att, "sha256", None) or None,
                        # The tombstone (#396): the chip shows the file deleted.
                        "deleted_at": _isoformat_or_none(getattr(att, "deleted_at", None)),
                        "deleted_reason": getattr(att, "deleted_reason", None),
                    }
                    for att in _att_rows
                ]
        except Exception:
            # Attachments are best-effort metadata on the run detail
            # response; a failing query here must not break the rest of
            # the payload (agentic_steps, run status, etc.).  But the
            # silent ``None`` fallback that lived here previously made it
            # impossible to notice attachments quietly disappearing from
            # the API — log with ``exc_info`` so the failure surfaces in
            # audit tooling alongside every other unhandled error.
            logger.exception(
                "Failed to load attachments for assignment run %s; falling "
                "back to attachments=None on the response",
                r.id,
            )
            _attachments = None

    # The run's friction ledger, read in this session; never raises.
    from app.services.frictions import run_frictions

    _frictions = await run_frictions(db, r.id)

    # The run's ROI record for the outcome review row; never raises.
    from app.services.roi_records import review_block

    _roi = await review_block(
        db,
        run_id=r.id,
        user_id=user.id,
        is_admin=user.is_admin,
        subject_owner_id=r.user_id,
        company_managed=bool(r.is_company_managed),
    )

    return AssignmentRunDetailResponse(
        id=str(r.id),
        assignment_id=str(r.assignment_id) if r.assignment_id else None,
        assignment_name=wf.name if wf else None,
        agent_id=str(r.agent_id),
        agent_name=ag.name if ag else "Unknown",
        agent_icon=ag.icon if ag else "Bot",
        status=r.status,
        is_company_managed=r.is_company_managed,
        notification_sent=r.notification_sent,
        notify_on_complete=wf.notify_on_complete if wf else False,
        show_in_digest=wf.show_in_digest if wf else False,
        parent_run_id=str(parent_id) if parent_id else None,
        child_run_count=child_run_count,
        parent_agent_name=parent_ag.name if parent_ag else None,
        parent_agent_icon=parent_ag.icon if parent_ag else None,
        trigger_payload=without_hidden_parent(
            r.trigger_payload,
            parent_is_shown(r, openable, is_admin=user.is_admin),
        ),
        trigger_context=r.trigger_context,
        a2a_provider=(
            await resolve_submitter_providers(db, {r.id: r.trigger_context})
        ).get(r.id),
        a2a_exchange=(
            await run_exchanges(db, [r], user_id=user.id, is_admin=user.is_admin)
        ).get(r.id),
        actions_taken=r.actions_taken,
        result_summary=r.result_summary,
        rendered_prompt=rendered_prompt,
        agentic_steps=agentic_steps,
        attachments=_attachments,
        frictions=_frictions,
        frictions_undisposed=getattr(r, "frictions_undisposed", None),
        roi=_roi,
        error=r.error,
        pending_approvals=_visible_pending_approvals(r, user),
        approval_answered=bool(r.approval_decisions),
        failure_cause=r.failure_cause,
        failure_determination=r.failure_determination,
        stop_reason=r.stop_reason,
        integration_auth_failures=getattr(r, "integration_auth_failures", None),
        human_input_question=r.human_input_question,
        human_input_response=r.human_input_response,
        human_input_history=r.human_input_history,
        langfuse_trace_id=r.langfuse_trace_id,
        llm_trace_id=str(r.llm_trace_id) if r.llm_trace_id else None,
        total_input_tokens=r.total_input_tokens,
        total_output_tokens=r.total_output_tokens,
        started_at=r.started_at.isoformat() if r.started_at else None,
        completed_at=r.completed_at.isoformat() if r.completed_at else None,
        created_at=r.created_at.isoformat(),
    )


@router.delete("/runs/purge", status_code=status.HTTP_204_NO_CONTENT)
async def purge_all_runs(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete all assignment runs visible to the admin.

    Admins see both their own runs (user_id == admin.id) and company-managed
    runs (``is_company_managed`` — runs always keep an owning ``user_id``).
    Purge removes both sets and resets execution counts on the corresponding
    assignments.
    """
    # Clean up file attachments for runs being purged
    from app.services.file_cleanup import delete_file_attachments

    run_ids_result = await db.execute(
        select(AssignmentRun.id).where(run_visible_filter(user.id))
    )
    purged_run_ids = [row[0] for row in run_ids_result.all()]
    from app.a2a.tasks import lock_tasks_before_deletion

    visible_runs = select(AssignmentRun.id).where(run_visible_filter(user.id))
    # Before the first write: the tasks mirroring these runs are the first rows
    # every other writer of a task and its run takes.
    await lock_tasks_before_deletion(db, run_ids=visible_runs)
    for run_id in purged_run_ids:
        await delete_file_attachments(db, assignment_run_id=run_id)

    # Cancel orphaned orchestration executions for every purged run so the
    # engine doesn't keep working deleted rows / holding reconciler slots, then
    # cancel the decoupled background-task rows spawned under those runs.
    from app.services.orchestration_engine import cancel_executions_for_runs
    from app.services.sub_agent import mark_sub_agent_tasks_cancelled_for_runs

    await cancel_executions_for_runs(db, purged_run_ids)
    await mark_sub_agent_tasks_cancelled_for_runs(db, purged_run_ids)

    # Again in the statement before the DELETE, which also removes the runs
    # created since the first lock: their tasks must be held first too.
    await lock_tasks_before_deletion(db, run_ids=visible_runs)
    await db.execute(
        sql_delete(AssignmentRun).where(run_visible_filter(user.id))
    )
    # Reset execution_count on all affected assignments
    wf_result = await db.execute(
        select(Assignment).where(
            or_(Assignment.user_id == user.id, Assignment.is_company_managed.is_(True))
        )
    )
    for wf in wf_result.scalars().all():
        wf.execution_count = 0
    await db.commit()


@router.post("/runs/cleanup-stale")
async def cleanup_stale_runs(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Immediately recover stale orchestration executions across all adapters.

    Drives the orchestration engine's recovery sweeps on demand (the worker
    reconciler runs the same sweeps continuously): expired-lease executions with
    attempts remaining are re-queued, and deadline-exceeded / attempt-exhausted
    ones are dead-lettered with their backing row (``AssignmentRun``,
    ``SubAgentTask`` + linked run, or ``EvaluationRun``) marked failed.

    Sweeps EVERY adapter, not just ``assignment``: a stuck sub-agent delegation
    or evaluation run is just as much a durable execution, and when the worker
    reconciler is down (the only thing that normally claims/sweeps them) this
    button is the operator's recovery path — scoping it to ``assignment`` left
    past-deadline sub-agent / evaluation executions unrecoverable and made the
    button report "no stale runs found" while a sub-agent delegation hung on
    ``pending`` forever. ``run_dead_letter`` routes each dead-lettered row to its
    own adapter's ``on_dead_letter`` hook, so the right backing row is failed.

    Returns the number of executions dead-lettered (definitively failed).
    """
    from app.services import orchestration_engine as engine
    from app.services.orchestration_adapters import register_builtin_adapters
    from app.services.orchestration_reconciler import run_dead_letter

    register_builtin_adapters()
    dead_lettered = await engine.sweep_deadlines(db, limit=1000)
    dead_lettered += await engine.reclaim_expired_leases(db, limit=1000)
    await db.commit()

    # Drive each dead-letter cleanup through the reconciler's resilient helper:
    # it runs every ``on_dead_letter`` in its own session and logs+swallows
    # failures, so one corrupt payload (or an adapter that raises) can't 500 the
    # request, leave this ``db`` in a failed-transaction state, or block cleanup
    # of the remaining executions.
    for execution in dead_lettered:
        await run_dead_letter(execution, worker_id="admin-cleanup")

    # Also reconcile background (sub-agent) tasks orphaned in pending/running
    # whose backing execution is already terminal or gone: an execution failed
    # via ``engine.fail()`` (reconciler timeout / adapter crash) bypasses its
    # ``on_dead_letter`` cleanup, leaving the task + linked run + delegation
    # stuck. This is the on-demand twin of the autonomous orphan-task sweep.
    from app.services.sub_agent import recover_orphaned_sub_agent_tasks

    orphaned_tasks = await recover_orphaned_sub_agent_tasks(db)
    await db.commit()

    # ``cleaned`` counts only the dead-lettered (definitively failed)
    # executions. Recoverable ones (expired lease, attempts remaining) are
    # silently re-queued by ``reclaim_expired_leases`` and are NOT included
    # here — so the wording must say "dead-lettered", not "recovered".
    cleaned = len(dead_lettered)
    message = (
        f"Dead-lettered {cleaned} stale run(s) (deadline exceeded or attempts "
        f"exhausted); any recoverable runs were re-queued."
    )
    if orphaned_tasks:
        message += f" Reconciled {orphaned_tasks} orphaned background task(s)."
    await log_audit(
        db,
        user=user,
        action="cleanup_stale_runs",
        entity_type="assignment_run",
        summary=f"Dead-lettered {cleaned} stale run(s)",
        details={
            "dead_lettered_count": cleaned,
            "orphaned_tasks_recovered": orphaned_tasks,
        },
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return {
        "cleaned": cleaned,
        "orphaned_tasks_recovered": orphaned_tasks,
        "message": message,
    }


@router.post("/runs/bulk-cancel", response_model=BulkCancelRunsResponse)
async def bulk_cancel_runs(
    body: BulkCancelRunsRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Cancel every run that has not started and matches the filter.

    Administrators only. Touches ``pending`` / ``delayed`` / ``queued`` runs of
    an agent, an assignment and / or created before a moment; each ends
    ``cancelled`` with ``Cancelled by <user>`` and keeps its trail, unlike
    ``bulk-delete``. ``dry_run`` answers the count and changes nothing.
    """
    from app.services.run_bulk_cancel import (
        BulkCancelFilter,
        BulkCancelFilterError,
        cancel_waiting_runs,
        count_cancellable_runs,
    )

    try:
        filters = BulkCancelFilter(
            agent_id=parse_uuid(body.agent_id, label="agent_id") if body.agent_id else None,
            assignment_id=(
                parse_uuid(body.assignment_id, label="assignment_id")
                if body.assignment_id
                else None
            ),
            statuses=tuple(dict.fromkeys(body.status)),
            created_before=body.created_before,
        )
    except BulkCancelFilterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if body.dry_run:
        return BulkCancelRunsResponse(count=await count_cancellable_runs(db, filters), dry_run=True)

    actor = user.full_name or user.email
    count = await cancel_waiting_runs(db, filters, actor=actor)
    await log_audit(
        db,
        user=user,
        action="bulk_cancel_runs",
        entity_type="assignment_run",
        summary=f"Cancelled {count} waiting run(s)",
        details={
            "cancelled_count": count,
            "agent_id": body.agent_id,
            "assignment_id": body.assignment_id,
            "status": list(filters.statuses),
            "created_before": body.created_before.isoformat() if body.created_before else None,
        },
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return BulkCancelRunsResponse(count=count)


@router.post("/runs/bulk-delete", response_model=BulkDeleteRunsResponse)
async def bulk_delete_runs(
    body: BulkDeleteRunsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete multiple assignment runs at once.

    Two modes:
      1. **By IDs** – provide ``run_ids`` to delete specific runs.
      2. **By filters** – set ``all_matching_filters=True`` and provide filter
         params (same semantics as the list endpoint) to delete every matching
         run.

    Ownership rules are enforced: regular users can only delete their own runs,
    admins any run (``require_run_access``).
    """
    ids_to_delete: list = []

    if body.run_ids:
        # ------ Mode 1: delete by explicit IDs ------
        try:
            uuid_ids = [uuid.UUID(rid) for rid in body.run_ids]
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid run ID format"
            ) from exc

        # Fetch matching runs with ownership check. Non-admins are scoped to
        # runs they own AND that are not company-managed: a company-managed
        # manual/follow-up run stamps ``user_id`` with the acting (non-admin)
        # user, so a ``user_id``-only guard would let them delete a shared run
        # they merely triggered — company-managed run actions are admin-only.
        # An admin deletes the runs they selected, whoever owns them, as
        # ``DELETE /runs/{id}`` lets them.
        stmt = select(AssignmentRun).where(AssignmentRun.id.in_(uuid_ids))
        if user.role != "admin":
            stmt = stmt.where(
                AssignmentRun.user_id == user.id,
                AssignmentRun.is_company_managed.is_(False),
            )
        result = await db.execute(stmt)
        ids_to_delete = [r.id for r in result.scalars().all()]

    elif body.all_matching_filters:
        # ------ Mode 2: delete all matching filters ------
        # parent_run_id=None disables the top-level filter so bulk delete
        # also removes child (delegation) runs when purging an agent.
        base = _filtered_runs_query(
            user,
            status=body.status,
            search=body.search,
            notifications=body.notifications,
            digests=body.digests,
            ownership=resolve_list_ownership(body.vis, body.ownership),
            agent_id=body.agent_id,
            parent_run_id=None,
            scope=body.scope,
        )
        # Non-admins should never be able to delete company-managed runs even
        # if the ownership filter is omitted (the helper already scopes to the
        # user's own + company-managed, but non-admins shouldn't delete the
        # company-managed portion). The ``is_company_managed`` guard is required
        # in addition to the ``user_id`` scope because company-managed
        # manual/follow-up runs stamp ``user_id`` with the acting user, so a
        # ``user_id``-only filter would still expose shared runs the non-admin
        # triggered.
        if user.role != "admin":
            base = base.where(
                AssignmentRun.user_id == user.id,
                AssignmentRun.is_company_managed.is_(False),
            )

        result = await db.execute(base)
        ids_to_delete = [r.id for r in result.scalars().all()]

    else:
        raise HTTPException(
            status_code=400,
            detail="Provide either run_ids or set all_matching_filters=True",
        )

    if ids_to_delete:
        from app.a2a.tasks import lock_tasks_before_deletion

        # Before the first write: the tasks mirroring these runs are the first
        # rows every other writer of a task and its run takes.
        await lock_tasks_before_deletion(db, run_ids=ids_to_delete)
        # Clean up file attachments for runs being deleted
        from app.services.file_cleanup import delete_file_attachments

        for rid in ids_to_delete:
            await delete_file_attachments(db, assignment_run_id=rid)
        # Cancel orphaned orchestration executions for the deleted runs (and the
        # sub-agent delegations spawned under them) so they don't keep a
        # reconciler slot or wedge the fan-out counter on pending, then cancel
        # the decoupled background-task rows spawned under those runs.
        from app.services.orchestration_engine import cancel_executions_for_runs
        from app.services.sub_agent import mark_sub_agent_tasks_cancelled_for_runs

        await cancel_executions_for_runs(db, ids_to_delete)
        await mark_sub_agent_tasks_cancelled_for_runs(db, ids_to_delete)
        await db.execute(
            sql_delete(AssignmentRun).where(AssignmentRun.id.in_(ids_to_delete))
        )
        # Commit before answering: the get_db teardown only commits after the response is sent.
        await db.commit()

    return BulkDeleteRunsResponse(deleted=len(ids_to_delete))


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a single assignment run.

    Company-managed runs (``is_company_managed``) can only be deleted by admins.
    """
    result = await db.execute(
        select(AssignmentRun).where(
            AssignmentRun.id == parse_path_id(run_id, not_found_detail="Run not found")
        )
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    require_run_access(run, user, action="delete")
    from app.a2a.tasks import lock_tasks_before_deletion

    # Before the first write: a task mirroring this run is the first row every
    # other writer of a task and its run takes.
    await lock_tasks_before_deletion(db, run_ids=[run.id])
    # Then the run, before its assignment, as the agent deletion's batches take
    # them: a concurrent deletion of the same run waits here and finds it gone,
    # so the count below is lowered once.
    locked = (
        await db.execute(select(AssignmentRun.id).where(AssignmentRun.id == run.id).with_for_update())
    ).scalar_one_or_none()
    if locked is None:
        raise HTTPException(status_code=404, detail="Run not found")
    # Clean up any file attachments associated with this run
    from app.services.file_cleanup import delete_file_attachments

    await delete_file_attachments(db, assignment_run_id=run.id)
    # Decrement assignment execution_count, in SQL: a run finishing may write
    # it meanwhile.
    if run.assignment_id is not None:
        await db.execute(
            sql_update(Assignment)
            .where(Assignment.id == run.assignment_id)
            .values(execution_count=func.greatest(Assignment.execution_count - 1, 0))
        )

    # Collect all descendant run IDs (recursive) so we can cascade-delete
    # delegation records that reference any run in the tree.
    from sqlalchemy import text as _sa_text

    # ``UNION``, not ``UNION ALL``: a loop in ``parent_run_id`` ends the walk
    # instead of repeating forever.
    _desc_cte = _sa_text("""
        WITH RECURSIVE tree AS (
            SELECT id FROM assignment_runs WHERE id = :root_id
            UNION
            SELECT wr.id FROM assignment_runs wr
            INNER JOIN tree t ON wr.parent_run_id = t.id
        )
        SELECT id FROM tree
        """)
    _desc_result = await db.execute(_desc_cte, {"root_id": run.id})
    _all_run_ids = [str(row[0]) for row in _desc_result.all()]

    if _all_run_ids:
        # Cancel any live orchestration execution for this run (and its
        # delegation subtree) BEFORE deleting the rows, so we don't orphan a
        # pending/running execution that would keep a reconciler slot and
        # permanently inflate the sub-agent fan-out counter (the "deleted the
        # task but everything is still stuck on pending" failure mode).
        from app.services.orchestration_engine import cancel_executions_for_runs
        from app.services.sub_agent import mark_sub_agent_tasks_cancelled_for_runs

        await cancel_executions_for_runs(db, _all_run_ids)
        # Background tasks live in their own table (sub_agent_tasks) and are not
        # otherwise correlated to runs — cancel the ones spawned under this run
        # tree so deleting a run also kills its background tasks.
        await mark_sub_agent_tasks_cancelled_for_runs(db, _all_run_ids)

        from app.models.agent_delegation import AgentDelegation

        _del_result = await db.execute(select(AgentDelegation))
        for d in _del_result.scalars().all():
            meta = d.metadata_json or {}
            if meta.get("assignment_run_id") in _all_run_ids:
                await db.delete(d)

    await db.delete(run)
    await db.commit()


# ---------------------------------------------------------------------------
# Human-in-the-loop – respond to a pending question
# ---------------------------------------------------------------------------


def record_human_input_resolution(
    run: AssignmentRun,
    user: User,
    *,
    resolution: str,
    response: str | None,
    surface: str | None = None,
) -> dict:
    """Append a durable HITL resolution entry to ``run.human_input_history``.

    The engine clears the live ``human_input_question`` / ``_response``
    fields when the run resumes, so this trail is the only persistent record
    of who answered what and when. The list is reassigned (not
    mutated in place) so SQLAlchemy change-tracking picks up the JSON update.
    *surface* names an answer given outside the web app (``teams_card``).
    Returns the appended entry.
    """
    entry = {
        "question": run.human_input_question,
        "response": response,
        "resolution": resolution,
        "responded_at": datetime.now(UTC).isoformat(),
        "responded_by": user.full_name or user.email,
        "responded_by_id": str(user.id),
    }
    if surface:
        entry["surface"] = surface
    # A discarded pause ends gated calls that never ran and never got a verdict
    # (FR-020). Nothing else records them: there is no resume, so the
    # engine never stamps an outcome, and no tool-call row exists because the
    # call was never dispatched. Without this the reviewer's own audit trail
    # shows the run completed with the proposals simply gone.
    if resolution == "dismissed" and run.pending_approvals:
        from app.tools.approval import OUTCOME_DISMISSED

        entry["gate_outcomes"] = [
            {
                "tool_call_id": proposal.get("tool_call_id"),
                "tool_name": proposal.get("tool_name"),
                "outcome": OUTCOME_DISMISSED,
            }
            for proposal in run.pending_approvals
        ]
    run.human_input_history = [*(run.human_input_history or []), entry]
    return entry


def _apply_decisions_to_run(run, decisions, user, *, surface: str | None = None) -> None:
    """Validate a decision set against the run's pending proposals and store it.

    Every pending ``tool_call_id`` must appear exactly once. A partial set is
    refused rather than partially applied, because resuming with an undecided
    call would leave its ``tool_use`` block without a ``tool_result`` — the same
    invalid conversation the batch pre-screen exists to prevent, arriving by a
    different route.
    """
    pending = list(run.pending_approvals or [])
    if not pending:
        raise HTTPException(
            status_code=400,
            detail="This run has no pending approvals; use 'response' instead.",
        )

    pending_ids = [p.get("tool_call_id") for p in pending]
    submitted_ids = [d.tool_call_id for d in decisions]

    if sorted(submitted_ids) != sorted(pending_ids):
        missing = sorted(set(pending_ids) - set(submitted_ids))
        unknown = sorted(set(submitted_ids) - set(pending_ids))
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Decisions must cover every pending call exactly once",
                "missing": missing,
                "unknown": unknown,
                "duplicated": len(submitted_ids) != len(set(submitted_ids)),
            },
        )

    run.approval_decisions = {d.tool_call_id: d.model_dump() for d in decisions}

    summary = ", ".join(f"{d.tool_call_id}:{d.decision}" for d in decisions)
    record_human_input_resolution(
        run,
        user,
        resolution="answered",
        response=f"Approval decisions — {summary}",
        surface=surface,
    )


async def _grant_standing_approvals(db, run, decisions, user) -> None:
    """Persist a standing approval for every ``approve_always`` decision.

    Granted to the identity the run *executes as* — ``run.user_id``, the same
    ``effective_user_id`` the gate reads its exceptions for — rather than to
    whoever clicked. On a private run those are the same person. On a
    company-managed one they need not be: any administrator may resolve the
    pause, and granting to the clicker would write the preference onto an
    account the gate never consults, so the tool would go on prompting forever
    while the administrator's own preference list claimed it never would. A
    silent no-op is the worst available outcome for a control whose entire
    promise is "this will not ask again".

    The administrator is recorded rather than hidden. ``granted_by_admin_id``
    already exists for impersonation (FR-017b) and carries the same meaning
    here: somebody acted on another account's behalf, and the trail names both.

    Fingerprinted against the schema captured in the proposal rather than the
    tool's current definition: the user consented to the call they were shown,
    so if the tool changed between the pause and the decision, the approval
    describes what they actually saw and the changed tool re-prompts.
    """
    from app.services.tool_approval_service import grant_exception
    from app.tools.approval import fingerprint

    # Must mirror ``effective_user_id`` in the engine exactly — `run.user_id`
    # falling back to the agent's owner — because that is the account the gate
    # will read exceptions for. Resolving it differently writes the preference
    # where nothing looks, and "Yes, always" goes on prompting forever.
    beneficiary_id = getattr(run, "user_id", None)
    if beneficiary_id is None and getattr(run, "agent_id", None) is not None:
        # Only for an owner-less run, so the common path pays nothing.
        beneficiary_id = await db.scalar(
            select(Agent.user_id).where(Agent.id == run.agent_id)
        )
    # Last resort: an owner-less run on an owner-less agent leaves nobody for
    # the gate to find either, so attributing it to the clicker at least keeps
    # the preference visible to whoever made it.
    beneficiary_id = beneficiary_id or user.id
    # An impersonated session already names its administrator; otherwise one is
    # named only when the click lands on someone else's account.
    acting_admin_id = getattr(user, "impersonated_by", None) or (
        user.id if beneficiary_id != user.id else None
    )

    by_id = {p.get("tool_call_id"): p for p in (run.pending_approvals or [])}
    for decision in decisions:
        if decision.decision != "approve_always":
            continue
        proposal = by_id.get(decision.tool_call_id) or {}
        tool_key = proposal.get("tool_name")
        if not tool_key:
            continue
        await grant_exception(
            db,
            user_id=beneficiary_id,
            tool_key=tool_key,
            tool_source=proposal.get("source", "unknown"),
            tool_entity_id=proposal.get("tool_entity_id"),
            schema_fingerprint=fingerprint(proposal.get("input_schema")),
            # ``impersonated_by`` is a JWT claim, so it arrives as a string
            # where the column is a UUID. Both chat call sites already convert;
            # this one relied on the driver coercing it.
            granted_by_admin_id=(
                uuid.UUID(str(acting_admin_id)) if acting_admin_id else None
            ),
        )


@router.get("/runs/{run_id}/pending-approvals")
async def get_pending_approvals(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the structured proposals awaiting a decision on a paused run.

    ``kind`` distinguishes a per-tool approval pause from a classic free-text
    ``request_human_input`` pause; clients must branch on it rather than
    assuming one shape, since both resolve through the same endpoint.
    """
    result = await db.execute(
        select(AssignmentRun).where(
            AssignmentRun.id == parse_path_id(run_id, not_found_detail="Run not found")
        )
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Same access rule as responding: seeing what is proposed and deciding it
    # are the same privilege.
    require_run_access(run, user, action="respond to")

    if run.status != "awaiting_human":
        raise HTTPException(
            status_code=409,
            detail=f"Run is not awaiting human input (status: {run.status})",
        )

    proposals = list(run.pending_approvals or [])
    # The reviewer reads them: a secret variable shows as [secret], never as a restorable marker.
    return redact_known_secrets_for_display(
        {
            "run_id": str(run.id),
            "status": run.status,
            "kind": "action_proposal" if proposals else "free_text",
            "question": run.human_input_question,
            "proposals": proposals,
        }
    )


#: Statuses reachable from ``awaiting_human`` only by the resume path
#: (``assignment_engine.resume_assignment_run`` sets ``running``, and the turn
#: then ends ``completed`` or ``failed``). A run in one of these was plausibly
#: just answered; ``delayed`` and ``skipped`` are not reachable that way.
_POST_RESUME_STATUSES = frozenset({"running", "completed", "failed"})


def _refuse_not_awaiting(run) -> None:
    """Refuse a decision on a run that is no longer awaiting one (FR-030).

    One 400 used to cover every reason, so a reviewer answering a stale
    notification learned only that it did not work — not whether to go and read
    someone else's decision or to stop looking for a pause that never existed.

    **What this cannot do, deliberately.** The resume path clears
    ``pending_approvals``, ``approval_decisions``, ``human_input_question`` and
    ``human_input_response`` (``assignment_engine.py`` around the resume
    branch), so after an answer the run keeps no trace that it was ever paused.
    There is therefore no way to name who decided or when without new state,
    and the wording here must not imply otherwise: it says the run is no longer
    awaiting and that an answer elsewhere is the likely cause, which is all the
    data supports. Promising more would be a contract the storage cannot keep.

    409 rather than 400 for the resumable statuses: the request was well formed
    and would have been accepted a moment earlier. A run that was never
    awaiting input is the caller's own mistake, and stays 400.
    """
    if run.status in _POST_RESUME_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This run is no longer awaiting approval (now: {run.status}). "
                "It was most likely answered on another surface."
            ),
        )
    raise HTTPException(
        status_code=400,
        detail=f"Run is not awaiting human input (status: {run.status})",
    )


def _refuse_already_answered(run) -> None:
    """Refuse a decision on a run somebody has already answered (FR-030).

    Answering leaves ``status`` at ``awaiting_human`` until the worker drains
    the resume, so a status check cannot tell a fresh pause from an answered
    one. ``approval_decisions`` is what changes; after the drain
    ``_refuse_not_awaiting`` takes over.
    """
    if run.approval_decisions:
        raise HTTPException(
            status_code=409,
            detail=(
                "This run has already been answered, most likely on another "
                "surface. It is waiting to resume."
            ),
        )


async def _locked_run(db, run_id: uuid.UUID):
    """The run, read under ``FOR UPDATE``, ready to be decided.

    Serialises the deciders; ``_refuse_already_answered`` is what then turns the
    second one away, because the lock alone cannot. Taken one row at a time
    inside the loops, never for a whole request — see ``_answerable_pending_runs``.
    """
    return (
        await db.execute(
            select(AssignmentRun)
            .where(AssignmentRun.id == run_id)
            .with_for_update()
            # Or the identity map returns the copy loaded before the lock,
            # which a body naming the same run twice hits on its second pass.
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _answerable_pending_runs(
    db, user, agent_id: uuid.UUID | None = None, own_only: bool = False
) -> list:
    """Every paused run carrying proposals that *user* may decide.

    Selected with ``run_answerable_filter`` and then authorised row by row with
    ``require_run_access`` — the filter is the set form of that rule and the two
    are kept together, but the per-row check stays the thing that decides.

    ``agent_id`` narrows to one agent. It has to reach the *decision* endpoint
    and not only the summary: a control offered on an agent's page that resumed
    another agent's runs would act outside the scope it shows.

    ``own_only`` narrows further, to the runs carrying this user's identity.
    That is the reach of their own allow-all switch (FR-024), and an
    administrator may answer company-managed runs belonging to other people —
    so without it, switching the switch on would sweep in runs the switch
    itself will never affect.

    Never locks, and both callers depend on it: the summary path only counts,
    and a lock taken here would be dropped by the first answer's commit, leaving
    the rest of the sweep on a stale snapshot. Rows are locked one at a time
    through ``_locked_run`` instead.
    """
    conditions = [
        AssignmentRun.status == "awaiting_human",
        run_answerable_filter(user),
    ]
    if agent_id is not None:
        conditions.append(AssignmentRun.agent_id == agent_id)
    if own_only:
        conditions.append(AssignmentRun.user_id == user.id)
    result = await db.execute(select(AssignmentRun).where(*conditions))
    runs = []
    for run in result.scalars().all():
        if not run.pending_approvals:
            continue
        # Answered already, and merely waiting on the worker.
        # ``resume_assignment_run`` does not change the status — it emits a
        # resume outbox event and commits, so the run stays ``awaiting_human``
        # until the worker picks it up. Counting it would tell the reviewer
        # something still needs them when what it needs is a queue drain, and
        # the count would only settle whenever the worker got round to it.
        if run.approval_decisions:
            continue
        try:
            require_run_access(run, user, action="respond to")
        except HTTPException:
            continue
        runs.append(run)
    return runs


@router.post("/runs/approvals/release-covered", response_model=BatchApprovalResponse)
async def release_covered_runs(
    body: ReleaseCoveredRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Answer the named runs that the caller's standing approvals already cover.

    FR-039b is the rule this exists to enforce: a run qualifies only when
    **every** one of its pending calls is covered. One covered and one uncovered
    call means the run is answered by hand — releasing it would approve a call
    the reviewer never consented to, which is FR-006 defeated by arithmetic.

    That arithmetic used to live in the page alone, which sent the resulting
    per-call decisions to the batch endpoint. The batch cannot check the rule:
    it is *told* which calls to answer, so a decision naming a partially covered
    run is indistinguishable from an ordinary approval. Here the caller names
    runs and the server decides the calls, which is what makes the guarantee
    real rather than advisory.

    Grants nothing (FR-039c). The exceptions already exist — that is the whole
    premise — so a run is released against the approvals as they stand.

    **Tool drift is not detected here, and this must not claim otherwise.** Both
    sides of the coverage comparison come from state stored when the run paused:
    the standing approval's fingerprint on one side, the proposal's own
    ``input_schema`` on the other. A tool whose *live* definition changed since
    then still matches, so this can report a run released that then re-pauses at
    execution, where the gate re-fingerprints against the live tool. That makes
    the gate the backstop and this a display inaccuracy, not an escape —
    comparing against the live definition would need the tool registry in an API
    handler, which is the reason it stops here.

    Per member, like every other multi-run path: a run the caller may not answer
    refuses on its own and the rest proceed (FR-033).
    """
    from app.services.assignment_engine import resume_assignment_run
    from app.services.tool_approval_service import list_active_exceptions
    from app.tools.approval import fingerprint, standing_approval_covers

    results: list[BatchApprovalResult] = []

    # Keyed by the run's own user, not the caller: a manager may answer a run
    # they do not own, and the gate resolves approvals for the run's effective
    # user. Checking the caller's would promise coverage the gate never sees.
    # Each value is the approvals, the tool each was granted to, and the
    # connection (an MCP tool's server) each was granted on.
    by_owner: dict[
        uuid.UUID | None, tuple[dict[str, str], dict[str, str], dict[str, str]]
    ] = {}

    async def _covered_for(
        target: AssignmentRun,
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        # Same fallback the engine applies (``run.user_id or agent.user_id``),
        # so an owner-less company-managed run is checked against the approvals
        # it will actually be gated against.
        owner_id = target.user_id
        if owner_id is None:
            owner_id = (
                await db.execute(
                    select(Agent.user_id).where(Agent.id == target.agent_id)
                )
            ).scalar_one_or_none()
        if owner_id not in by_owner:
            granted_to: dict[str, str] = {}
            granted_on: dict[str, str] = {}
            approvals = await list_active_exceptions(
                db, owner_id, sources=granted_to, entity_ids=granted_on
            )
            by_owner[owner_id] = (approvals, granted_to, granted_on)
        return by_owner[owner_id]

    for raw_id in body.run_ids:
        def _refuse(reason: str, detail: str, *, _run=raw_id, _calls=None) -> None:
            # One row per pending call when we know them, so the caller can tell
            # which calls did not move; one bare row when we cannot.
            ids = _calls or [""]
            results.extend(
                BatchApprovalResult(
                    run_id=_run,
                    tool_call_id=call_id,
                    status="refused",
                    reason=reason,
                    detail=detail,
                )
                for call_id in ids
            )

        try:
            parsed_id = parse_uuid(raw_id, label="run_id")
        except HTTPException:
            _refuse("run_not_found", "Not a valid run id.")
            continue

        run = await _locked_run(db, parsed_id)
        if not run:
            _refuse("run_not_found", "Run not found.")
            continue

        try:
            require_run_access(run, user, action="respond to")
        except HTTPException as exc:
            # FR-008a / FR-028: refused AND recorded. Without this, somebody
            # walking run ids against this endpoint was correctly stopped every
            # time and left nothing an investigator could find — the one case
            # the requirement was written for.
            await record_refused_decision(
                db,
                user_id=str(user.id),
                conversation_id=run.id,
                reason="forbidden",
                surface="assignment_batch",
                summary=f"Refused an approval decision on run {run.id}",
                entity_type="assignment_run_approval",
            )
            _refuse("forbidden", str(exc.detail))
            continue

        if run.status != "awaiting_human":
            try:
                _refuse_not_awaiting(run)
            except HTTPException as exc:
                _refuse("run_not_awaiting", str(exc.detail))
            continue

        proposals = run.pending_approvals or []
        if not proposals:
            _refuse("run_not_awaiting", "This run has no pending approvals.")
            continue
        if run.approval_decisions:
            _refuse("already_answered", "This run has already been answered.")
            continue

        call_ids = [p.get("tool_call_id") or "" for p in proposals]
        # Contained like the resume below, and for the same reason: this reads
        # the database, ``list_active_exceptions`` raises its own error rather
        # than an ``HTTPException``, and an escape here would abort the request
        # after earlier runs had already resumed and committed — so the caller
        # would never learn which ones landed.
        try:
            covered, granted_to, granted_on = await _covered_for(run)
        except Exception:
            logger.exception("Could not resolve approvals for run %s", raw_id)
            _refuse(
                "rejected_by_applier",
                "This run's standing approvals could not be read. Nothing was "
                "changed for it.",
                _calls=call_ids,
            )
            continue
        # Fingerprints, not names alone: a standing approval recorded against a
        # different shape does not satisfy the gate. Note both fingerprints are
        # of STORED state, so this catches an approval that never matched this
        # proposal — not a tool that changed after the pause (see the docstring).
        # ``or "?"`` before the set, so a proposal with a missing or non-string
        # name cannot make this comparison raise on a mixed-type sort. The
        # proposal's source too: an approval granted to another tool of the same
        # name is not this call's.
        uncovered = sorted(
            {
                str(p.get("tool_name") or "?")
                for p in proposals
                if covered.get(str(p.get("tool_name") or "?"))
                != fingerprint(p.get("input_schema"))
                or not standing_approval_covers(
                    p.get("source"),
                    granted_to.get(str(p.get("tool_name") or "?")),
                    tool_entity_id=p.get("tool_entity_id"),
                    granted_entity_id=granted_on.get(str(p.get("tool_name") or "?")),
                )
            }
        )
        if uncovered:
            _refuse(
                "not_fully_covered",
                "Not every tool this run is waiting on is covered by a standing "
                f"approval: {', '.join(uncovered)}. Answer it directly instead.",
                _calls=call_ids,
            )
            continue

        # Contained, for the reason the batch endpoint states: an escape here
        # would abort the request with a bare error *after* earlier runs had
        # already resumed and committed, so the caller would learn nothing about
        # the ones that landed. Partial success is a success (FR-033).
        try:
            _apply_decisions_to_run(
                run,
                [
                    ApprovalDecision(tool_call_id=call_id, decision="approve")
                    for call_id in call_ids
                ],
                user,
            )
            await record_approval_decision(db, run=run, user=user, surface="assignment_release_covered")
            await resume_assignment_run(db, run)
        except HTTPException as exc:
            _refuse("rejected_by_applier", str(exc.detail), _calls=call_ids)
            continue
        except Exception:
            logger.exception("Could not release run %s", raw_id)
            _refuse(
                "rejected_by_applier",
                "This run could not be resumed. Nothing was changed for it.",
                _calls=call_ids,
            )
            continue
        results.extend(
            BatchApprovalResult(
                run_id=raw_id, tool_call_id=call_id, status="applied"
            )
            for call_id in call_ids
        )

    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return BatchApprovalResponse(results=results)


@router.get("/runs/approvals/pending-by-tool", response_model=PendingByToolResponse)
async def pending_approvals_by_tool(
    # Plain default, not ``Query(None)``: these endpoints are also called
    # directly in tests, where a ``Query`` sentinel would read as a real filter.
    agent_id: uuid.UUID | None = None,
    own_only: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """What is waiting, grouped by tool, across every page.

    ``agent_id`` scopes the answer to one agent, for the copy of this control
    that sits on an agent's own activity list. ``own_only`` scopes to the runs
    carrying the caller's identity, which is what the allow-all switch offers
    to clear (FR-043).

    The counts have to come from here rather than from the rows a client has
    loaded: a per-tool decision reaches past the page, so a total taken from
    one page would understate exactly what the reviewer is being asked to judge.

    Only runs whose pending calls are **all** one tool are counted under that
    tool: a run cannot be half-answered, so a decision about one of its tools
    does not reach it.

    A mixed run is still answerable once *every* tool it calls is selected,
    which is the rule ``approve_by_tool`` applies — so the combinations are
    returned, not just how many there are. Reporting the count alone let the
    confirmation understate its own reach: with A-only, B-only and A+B paused,
    selecting both disclosed two runs and answered three. It also left a tool
    that appears only in mixed runs impossible to select at all.
    """
    by_tool: dict[str, dict[str, int]] = {}
    by_combination: dict[tuple[str, ...], dict[str, int]] = {}
    for run in await _answerable_pending_runs(db, user, agent_id, own_only):
        names = {p.get("tool_name") for p in run.pending_approvals}
        if not all(names):
            # ``approve_by_tool`` compares the whole set, so a proposal with no
            # tool name makes its run unanswerable by tool at any selection. It
            # belongs in neither bucket: counting it under the names it does
            # carry would promise a release that cannot happen.
            continue
        if len(names) == 1:
            entry = by_tool.setdefault(names.pop(), {"run_count": 0, "call_count": 0})
        else:
            # Sorted so one combination has one key regardless of call order.
            entry = by_combination.setdefault(
                tuple(sorted(names)), {"run_count": 0, "call_count": 0}
            )
        entry["run_count"] += 1
        entry["call_count"] += len(run.pending_approvals)

    mixed = sorted(
        (
            PendingMixedGroup(tool_names=list(combination), **counts)
            for combination, counts in by_combination.items()
        ),
        key=lambda g: (-g.run_count, g.tool_names),
    )
    return PendingByToolResponse(
        tools=sorted(
            (
                PendingToolSummary(tool_name=n, **counts)
                for n, counts in by_tool.items()
            ),
            key=lambda t: (-t.run_count, t.tool_name),
        ),
        mixed=mixed,
        mixed_run_count=sum(g.run_count for g in mixed),
    )


@router.post("/runs/approvals/by-tool", response_model=BatchApprovalResponse)
async def approve_by_tool(
    body: ApproveByToolRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Answer every paused run whose calls are all among the named tools.

    This is the one approval path that reaches past the rows a client loaded,
    and it is safe for the same reason "Yes, always" is (FR-015): the reviewer
    is judging a **tool**, not each call's arguments. It is the weaker form of
    that decision — it answers what is waiting and grants nothing durable — so
    anywhere the standing exception is acceptable, this must be.

    Selecting several tools is what makes a mixed run answerable: a run calling
    two of them is covered once both are named, and stays paused until then.
    Approving it on one tool alone would sweep in a call the reviewer did not
    choose.

    ``approve_always`` is accepted, and is the clearest-consented form of it in
    the product: FR-032b asks that such a disclosure enumerate every tool being
    un-gated, and here the reviewer named the list. The grant runs per member
    through the shared helper, which reuses an existing active preference — so
    forty runs of one tool still produce one exception, not forty.
    """
    from app.services.assignment_engine import resume_assignment_run
    from app.tools.approval import VERDICT_APPROVE, VERDICT_APPROVE_ALWAYS

    wanted = {n for n in body.tool_names if n}
    if not wanted:
        raise HTTPException(status_code=400, detail="Name at least one tool.")

    results: list[BatchApprovalResult] = []
    for candidate in await _answerable_pending_runs(
        db, user, body.agent_id, body.own_only
    ):
        names = {p.get("tool_name") for p in candidate.pending_approvals}
        if not names <= wanted:
            continue

        # Re-read under the row lock the one run we are about to answer. The
        # selection stays unlocked on purpose: locking it would pin rows this
        # request never answers, and the first answer's commit would drop those
        # locks anyway, leaving runs 2..N decided from a pre-commit snapshot.
        run = await _locked_run(db, candidate.id)
        if (
            run is None
            or run.status != "awaiting_human"
            or run.approval_decisions
            or not {p.get("tool_name") for p in (run.pending_approvals or [])}
            <= wanted
        ):
            # Answered, dismissed or re-proposed since the select. The caller
            # named tools, not runs, so this one leaves the sweep silently —
            # reporting it refused would invent a member (FR-033).
            continue

        # ``approve_always`` writes a standing, deployment-wide consent onto the
        # run's OWNER — ``_grant_standing_approvals`` grants to ``run.user_id``,
        # correctly, so the gate finds it. For an admin sweeping the queue that
        # is consent the beneficiary never gave and never saw disclosed: FR-008a
        # reserves answering on someone's behalf to the impersonation route, and
        # FR-032b's disclosure is shown to whoever clicks, not to them. The run
        # is still answered — only the durable half is withheld.
        decision = body.decision
        if decision == VERDICT_APPROVE_ALWAYS and run.user_id != user.id:
            decision = VERDICT_APPROVE

        applied = [
            ApprovalDecision(
                tool_call_id=p.get("tool_call_id"),
                decision=decision,
                rejection_reason=body.rejection_reason,
            )
            for p in run.pending_approvals
        ]
        try:
            _apply_decisions_to_run(run, applied, user)
            await record_approval_decision(db, run=run, user=user, surface="assignment_by_tool")
            await _grant_standing_approvals(db, run, applied, user)
            await resume_assignment_run(db, run)
        except HTTPException as exc:
            detail = exc.detail
            results.extend(
                BatchApprovalResult(
                    run_id=str(run.id),
                    tool_call_id=d.tool_call_id,
                    status="refused",
                    reason="rejected_by_applier",
                    detail=detail if isinstance(detail, str) else str(detail),
                )
                for d in applied
            )
            continue

        results.extend(
            BatchApprovalResult(
                run_id=str(run.id), tool_call_id=d.tool_call_id, status="applied"
            )
            for d in applied
        )

    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return BatchApprovalResponse(results=results)


@router.post("/runs/approvals/batch", response_model=BatchApprovalResponse)
async def batch_respond_to_approvals(
    body: BatchApprovalRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Answer approvals across several runs in one action (FR-031).

    A convenience over N independent decisions, and deliberately no more than
    that. Each member is applied exactly as ``respond_to_human_input`` would:
    same authorisation, same decision applier, same resume. There is no
    batch-level audit record, because the group is not a thing that happened —
    the decisions are (FR-032).

    **Partial success is a success** (FR-033). A member that can no longer be
    answered is reported by name with a reason and does not roll back the ones
    that landed; they were independent decisions before this endpoint existed
    and they stay independent inside it. Ordering is not promised: runs resume
    on their own.

    Route order matters — this sits above ``/runs/{run_id}/respond`` so
    ``approvals`` is never captured as a run id.
    """
    from app.services.assignment_engine import resume_assignment_run

    by_run: dict[str, list[BatchApprovalDecision]] = {}
    for decision in body.decisions:
        by_run.setdefault(decision.run_id, []).append(decision)

    results: list[BatchApprovalResult] = []

    for run_id, decisions in by_run.items():
        def _refuse(reason: str, detail: str, *, _run=run_id, _ds=decisions) -> None:
            # Loop variables bound at definition: without the defaults every
            # refusal in the batch would report the LAST run's id (B023).
            results.extend(
                BatchApprovalResult(
                    run_id=_run,
                    tool_call_id=d.tool_call_id,
                    status="refused",
                    reason=reason,
                    detail=detail,
                )
                for d in _ds
            )

        try:
            parsed_id = parse_uuid(run_id, label="run_id")
        except HTTPException:
            _refuse("run_not_found", "Not a valid run id.")
            continue

        run = await _locked_run(db, parsed_id)
        if not run:
            _refuse("run_not_found", "Run not found.")
            continue

        # Per member, never per batch: bundling a run with ones the caller owns
        # must not grant reach over it (FR-028).
        try:
            require_run_access(run, user, action="respond to")
        except HTTPException as exc:
            # FR-008a / FR-028: refused AND recorded. Without this, somebody
            # walking run ids against this endpoint was correctly stopped every
            # time and left nothing an investigator could find — the one case
            # the requirement was written for.
            await record_refused_decision(
                db,
                user_id=str(user.id),
                conversation_id=run.id,
                reason="forbidden",
                surface="assignment_batch",
                summary=f"Refused an approval decision on run {run.id}",
                entity_type="assignment_run_approval",
            )
            _refuse("forbidden", str(exc.detail))
            continue

        if run.status != "awaiting_human":
            try:
                _refuse_not_awaiting(run)
            except HTTPException as exc:
                _refuse(
                    "already_answered" if exc.status_code == 409 else "run_not_awaiting",
                    str(exc.detail),
                )
            continue
        # The check that actually closes the race — the status above cannot
        # tell a fresh pause from one already answered.
        try:
            _refuse_already_answered(run)
        except HTTPException as exc:
            _refuse("already_answered", str(exc.detail))
            continue

        # The engine refuses a partial set, because resuming with an undecided
        # call leaves a ``tool_use`` block without its ``tool_result`` and every
        # provider rejects the next request outright. Caught here rather than
        # left to raise so one under-specified run does not fail the batch.
        pending_ids = {
            p.get("tool_call_id") for p in (run.pending_approvals or [])
        }
        answered_ids = {d.tool_call_id for d in decisions}
        if pending_ids - answered_ids:
            _refuse(
                "incomplete_decisions",
                (
                    "Every pending call on a run must be decided together; "
                    f"{len(pending_ids - answered_ids)} left undecided."
                ),
            )
            continue

        applied = [
            ApprovalDecision(
                tool_call_id=d.tool_call_id,
                decision=d.decision,
                rejection_reason=d.rejection_reason,
            )
            for d in decisions
        ]
        # The applier and the grant both raise on a set they will not accept —
        # a stale client sending ids the run no longer has, a free-text pause
        # bundled in by mistake, a duplicate. Letting that escape would abort
        # the request with a bare 4xx **after** earlier members had already
        # resumed and committed, so the caller learns nothing about the ones
        # that landed. FR-033 says a member that cannot be answered is
        # reported, not fatal; that has to hold for the applier's own
        # objections too, or the contract is only kept for the failures this
        # endpoint happened to anticipate.
        try:
            _apply_decisions_to_run(run, applied, user)
            await record_approval_decision(db, run=run, user=user, surface="assignment_batch")
        # Composed, not reimplemented: a second grant path is exactly how the
        # two surfaces would drift (FR-029, FR-038b). ``approve_always`` reaches
        # a batch since FR-032's revision, and it grants here through the very
        # same helper the run page uses, to the same identity, with the same
        # fingerprint — so an exception cannot mean one thing per surface.
            await _grant_standing_approvals(db, run, applied, user)
            await resume_assignment_run(db, run)
        except HTTPException as exc:
            detail = exc.detail
            _refuse(
                "rejected_by_applier",
                detail if isinstance(detail, str) else str(detail),
            )
            continue

        results.extend(
            BatchApprovalResult(
                run_id=run_id, tool_call_id=d.tool_call_id, status="applied"
            )
            for d in decisions
        )

    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return BatchApprovalResponse(results=results)


@router.post("/runs/{run_id}/respond", response_model=AssignmentRunResponse)
async def respond_to_human_input(
    run_id: str,
    body: RespondToHumanInputRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Respond to a assignment run that is awaiting human input.

    For company-managed runs (``is_company_managed``), only admins may respond.
    For private runs, only the owning user may respond.
    """
    run = await answer_paused_run(
        db,
        parse_path_id(run_id, not_found_detail="Run not found"),
        user,
        body,
        surface="assignment_respond",
    )
    return _run_to_response(run, user=user, parent_shown=await _parent_shown(db, run, user))


async def answer_paused_run(
    db: AsyncSession,
    run_id: uuid.UUID,
    user: User,
    body: RespondToHumanInputRequest,
    *,
    surface: str,
    answered_from: str | None = None,
) -> AssignmentRun:
    """Answer a run paused for a human, as *user*; the run after it.

    The one path every surface that answers a pause takes - the respond
    endpoint, and the Teams card of a pasted run link - so the access
    rule, the row lock, the refusals and the audit cannot drift between them.
    *surface* names the caller in the refusal trail; *answered_from* is stamped
    on the resolution entry for an answer given outside the web app.
    """
    from app.services.assignment_engine import resume_assignment_run

    # Lock the row for the rest of this transaction, so two reviewers opening
    # the same pause cannot both write a verdict. The lock only serialises
    # them — answering leaves the status at ``awaiting_human``, so it is
    # ``_refuse_already_answered`` below that turns the second one away.
    result = await db.execute(
        select(AssignmentRun)
        .where(AssignmentRun.id == run_id)
        .with_for_update()
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    try:
        require_run_access(run, user, action="respond to")
    except HTTPException:
        await record_refused_decision(
            db,
            user_id=str(user.id),
            conversation_id=run.id,
            reason="forbidden",
            surface=surface,
            summary=f"Refused an approval decision on run {run.id}",
            entity_type="assignment_run_approval",
        )
        raise

    if run.status != "awaiting_human":
        _refuse_not_awaiting(run)
    _refuse_already_answered(run)

    # Structured approval decisions — a different resolution shape from
    # the free-text answer below, handled first so the two never mix.
    if body.decisions is not None:
        _apply_decisions_to_run(run, body.decisions, user, surface=answered_from)
        await record_approval_decision(db, run=run, user=user, surface=answered_from or surface)
        await _grant_standing_approvals(db, run, body.decisions, user)
        await resume_assignment_run(db, run)
        # Decided: the Approve / Reject buttons of its phone notifications
        # stop working at once (they would be refused anyway, the pause is over).
        from app.services.push_action_tokens import void_run_tokens

        await void_run_tokens(run.id)
        await db.refresh(run)
        return run

    # A gated pause also fills ``human_input_question`` ("Approval required
    # before running: ..."), so a classic client sees something that looks
    # answerable and sends prose. Taking it would resume down the free-text
    # branch: it looks for a ``request_human_input`` call, finds none, and
    # appends a tool result whose id matches nothing while the gated
    # ``tool_use`` blocks stay unanswered — the malformed conversation every
    # provider rejects. Our own UI hides the box; the API must refuse it.
    if run.pending_approvals:
        raise HTTPException(
            status_code=400,
            detail=(
                "This run is awaiting approval decisions, not free text. "
                "Submit 'decisions' covering every pending tool call."
            ),
        )

    # Reject empty/whitespace responses: the UI keys the pending -> answered
    # transition off response presence, and the engine injects the text into
    # the resumed conversation - an empty answer would persist an ambiguous
    # "answered but blank" state (non-UI clients can send anything).
    response_text = (body.response or "").strip()
    if not response_text:
        raise HTTPException(status_code=400, detail="Response cannot be empty")

    # An answer resumes the run: refused while the owner is deactivated,
    # so the question stays pending until then. Dismissing stays possible.
    if run.assignment_id is not None:
        run_assignment = await db.get(Assignment, run.assignment_id)
        if run_assignment is not None:
            await refuse_if_owner_deactivated(db, run_assignment)

    # Save the human response + a durable resolution entry (the engine clears
    # the live fields on resume), then resume via the orchestration engine
    # (commits internally).
    run.human_input_response = response_text
    record_human_input_resolution(
        run, user, resolution="answered", response=response_text, surface=answered_from
    )
    await resume_assignment_run(db, run)

    # Re-read run to return updated state
    await db.refresh(run)
    return run


@router.get("/runs/{run_id}/action-requests", response_model=list[RunActionRequestResponse])
async def list_run_action_requests(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The open requests to act on a paused run, for the people who may answer it.

    Someone who cannot answer a run asked, from the card of its pasted link,
    those who can to act on it. Listed while the run waits for a person.
    """
    run = await db.get(AssignmentRun, parse_path_id(run_id, not_found_detail="Run not found"))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    require_run_access(run, user, action="see the requests on")
    if run.status != "awaiting_human":
        return []
    return [
        RunActionRequestResponse(
            id=str(request.id),
            requester_name=request.requester_name,
            source=request.source,
            created_at=request.created_at.isoformat(),
        )
        for request in await open_run_action_requests(db, run.id)
    ]


@router.post("/runs/{run_id}/action-requests/{request_id}/dismiss", status_code=204)
async def dismiss_run_action_request(
    run_id: str,
    request_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Close one open request to act on a run, as someone who may answer it."""
    run = await db.get(AssignmentRun, parse_path_id(run_id, not_found_detail="Run not found"))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    require_run_access(run, user, action="dismiss the requests on")
    closed = await close_run_action_requests(
        db,
        run.id,
        request_id=parse_path_id(request_id, not_found_detail="Request not found"),
        closed_by=user,
    )
    if not closed:
        raise HTTPException(status_code=404, detail="Request not found")
    await db.commit()


@router.post("/runs/{run_id}/dismiss", response_model=AssignmentRunResponse)
async def dismiss_run(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Dismiss a assignment run that is awaiting human input.

    Marks the run as completed (terminal state) without resuming execution.
    For company-managed runs (``is_company_managed``), only admins may dismiss.
    """
    # Same lock as responding, and for the same reason across the two
    # endpoints: a dismiss and a decision racing on one pause would otherwise
    # both observe ``awaiting_human`` — one completing the run while the other
    # resumes it.
    result = await db.execute(
        select(AssignmentRun)
        .where(AssignmentRun.id == parse_path_id(run_id, not_found_detail="Run not found"))
        .with_for_update()
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    require_run_access(run, user, action="dismiss")

    if run.status != "awaiting_human":
        raise HTTPException(
            status_code=400,
            detail=f"Run is not awaiting human input (status: {run.status})",
        )

    run.status = "completed"
    run.human_input_response = "[Dismissed by user]"
    record_human_input_resolution(run, user, resolution="dismissed", response=None)
    # Dismissed: nothing is left for an action request to ask for. An
    # answer closes them where every answer goes, ``resume_assignment_run``.
    await close_run_action_requests(db, run.id)
    run.result_summary = "Run dismissed by user — no action taken."
    run.completed_at = datetime.now(UTC)

    # Send notification if configured — a dismissed HITL run is still a
    # completed run and the user should be notified.
    wf_result = await db.execute(
        select(Assignment).where(Assignment.id == run.assignment_id)
    )
    assignment = wf_result.scalar_one_or_none()
    if assignment and assignment.notify_on_complete:
        run.notification_sent = True

    await db.commit()

    if assignment and assignment.notify_on_complete:
        try:
            from app.models.agent import Agent

            ag_result = await db.execute(select(Agent).where(Agent.id == run.agent_id))
            agent = ag_result.scalar_one_or_none()
            if agent:
                from app.services.notify import deliver_notification

                await deliver_notification(db, run, assignment, agent)
        except Exception as exc:
            logger.warning("Dismiss notification failed: %s", format_exception_message(exc))

    await db.refresh(run)
    return _run_to_response(run, user=user, parent_shown=await _parent_shown(db, run, user))


@router.post("/runs/{run_id}/followup", response_model=AssignmentRunResponse)
async def followup_run(
    run_id: str,
    body: FollowupRunRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Create a follow-up run based on a completed/failed/skipped run.

    Takes the parent run's result and the user's follow-up prompt, creates a
    new AssignmentRun on the same assignment/agent, and kicks off execution.
    The follow-up carries that result, so it is never more visible than the
    run it follows up: private when that run is, whatever the
    assignment's visibility has become since.
    """
    from app.services.assignment_engine import trigger_assignment as do_trigger

    # Load parent run
    result = await db.execute(
        select(AssignmentRun).where(
            AssignmentRun.id == parse_path_id(run_id, not_found_detail="Run not found")
        )
    )
    parent_run = result.scalar_one_or_none()
    if not parent_run:
        raise HTTPException(status_code=404, detail="Run not found")

    require_run_access(parent_run, user, action="follow up on")

    # Only allow follow-up on terminal runs
    if parent_run.status not in ("completed", "failed", "skipped"):
        raise HTTPException(
            status_code=400,
            detail=f"Can only follow up on completed, failed, or skipped runs (status: {parent_run.status})",
        )

    # Load assignment and agent
    wf_result = await db.execute(
        select(Assignment).where(Assignment.id == parent_run.assignment_id)
    )
    assignment = wf_result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if not assignment.enabled:
        raise HTTPException(status_code=400, detail="Assignment is disabled")
    await refuse_if_owner_deactivated(db, assignment)

    agent_result = await db.execute(
        select(Agent).where(Agent.id == parent_run.agent_id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not agent.enabled:
        raise HTTPException(status_code=400, detail="Agent is disabled")

    # NOTE: We intentionally do NOT reject when the assignment has other active
    # runs (pending / delayed / running / awaiting_human). A follow-up is tied
    # to a *specific* terminal run the user is reviewing — it is an explicit,
    # user-initiated action and must always succeed regardless of whatever else
    # the assignment happens to be doing. The active-run guard only belongs on
    # the autonomous scheduler and the generic manual trigger (which start a
    # fresh, unrelated run), not here.

    # Build follow-up payload and context
    payload = {
        "trigger_type": "followup",
        "parent_run_id": str(parent_run.id),
        "parent_result": parent_run.result_summary or parent_run.error or "",
        "followup_prompt": body.prompt,
    }
    trigger_context = {
        "type": "followup",
        "parent_run_id": str(parent_run.id),
        "parent_assignment_name": assignment.name,
        "user_id": str(user.id),
        "user_email": user.email,
        "triggered_at": datetime.now(UTC).isoformat(),
    }

    new_run_id = await do_trigger(
        assignment=assignment,
        agent=agent,
        payload=payload,
        db=db,
        trigger_context=trigger_context,
        # Follow-up runs on behalf of the user who clicked, like a manual
        # trigger — not the assignment's "run as" user.
        triggered_by_user_id=user.id,
        continues_run=parent_run,
    )
    await log_audit(
        db,
        user=user,
        action="followup",
        entity_type="assignment_run",
        entity_id=parent_run.id,
        entity_name=assignment.name,
        summary=f"Created follow-up run for assignment '{assignment.name}'",
        details={
            "parent_run_id": str(parent_run.id),
            "new_run_id": str(new_run_id),
            "agent_id": str(agent.id),
            "agent_name": agent.name,
            "assignment_id": str(assignment.id),
            "prompt_preview": (
                (body.prompt[:200] + "...") if len(body.prompt) > 200 else body.prompt
            ),
        },
        request=request,
    )

    # Return the new run
    run_result = await db.execute(
        select(AssignmentRun).where(AssignmentRun.id == new_run_id)
    )
    new_run = run_result.scalar_one()
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return _run_to_response(new_run, user=user)


# ---------------------------------------------------------------------------
# Interactive follow-up (chat) helpers
# ---------------------------------------------------------------------------

# Rendered prompts can be very large; cap the seeded task so the chat history
# stays readable and the LLM context isn't dominated by a single message.
_FOLLOWUP_TASK_CHARS = 6000
# Per-tool result preview length in the seeded execution trace.
_FOLLOWUP_TRACE_RESULT_CHARS = 400
# Safety cap on the number of tool steps included in the seeded trace.
_FOLLOWUP_TRACE_MAX_STEPS = 40


def _extract_run_task(parent_run: AssignmentRun, assignment: Assignment | None) -> str:
    """Best-effort "what was asked" text for a run.

    Uses the first user-role message of the stored conversation (the rendered
    prompt), falling back to the assignment's configured prompt.
    """
    if parent_run.messages:
        for msg in parent_run.messages:
            if (
                isinstance(msg, dict)
                and msg.get("role") == "user"
                and isinstance(msg.get("content"), str)
                and msg["content"].strip()
            ):
                return msg["content"]
    return (assignment.prompt if assignment else "") or ""


def _build_run_execution_trace(parent_run: AssignmentRun) -> str:
    """Compact, human-readable summary of the tools the run used.

    Lets the agent reason about *what it actually did* when the user asks
    troubleshooting questions in the seeded chat, without replaying raw
    tool-call structures (and their fragile ids) into the conversation history.
    """
    if not parent_run.messages:
        return ""

    # Pair tool results back to their call by id.
    results: dict[str, str] = {}
    for msg in parent_run.messages:
        if isinstance(msg, dict) and msg.get("role") == "tool":
            tcid = msg.get("tool_call_id")
            if tcid:
                results[str(tcid)] = str(msg.get("content", "") or "")

    lines: list[str] = []
    step = 0
    for msg in parent_run.messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            if step >= _FOLLOWUP_TRACE_MAX_STEPS:
                lines.append("… (trace truncated)")
                break
            step += 1
            name = tc.get("name") or "tool"
            res = results.get(str(tc.get("id", "")), "")
            res = " ".join(res.split())  # collapse whitespace for a one-liner
            if len(res) > _FOLLOWUP_TRACE_RESULT_CHARS:
                res = res[:_FOLLOWUP_TRACE_RESULT_CHARS] + "…"
            lines.append(f"{step}. `{name}`" + (f" → {res}" if res else ""))
        else:
            continue
        break

    if not lines:
        return ""
    header = f"**What this run actually did** ({step} tool call(s)):"
    return header + "\n" + "\n".join(lines)


@router.post("/runs/{run_id}/followup-chat", response_model=FollowupChatResponse)
async def followup_run_chat(
    run_id: str,
    body: FollowupChatRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _chat_gate: None = Depends(require_chat_enabled),
):
    """Open an interactive chat seeded from a terminal run.

    Creates a new web conversation routed to the run's agent, pre-seeded with
    the run's original task, result and a compact execution trace, so the user
    can continue the conversation and troubleshoot / understand what happened in
    the assignment. Returns the conversation id for the frontend to navigate to.
    """
    from app.models.chat import Conversation, Message

    # Load parent run
    result = await db.execute(
        select(AssignmentRun).where(
            AssignmentRun.id == parse_path_id(run_id, not_found_detail="Run not found")
        )
    )
    parent_run = result.scalar_one_or_none()
    if not parent_run:
        raise HTTPException(status_code=404, detail="Run not found")

    require_run_access(parent_run, user, action="follow up on")

    if parent_run.status not in ("completed", "failed", "skipped"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Can only follow up on completed, failed, or skipped runs "
                f"(status: {parent_run.status})"
            ),
        )

    agent_result = await db.execute(
        select(Agent).where(Agent.id == parent_run.agent_id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not agent.enabled:
        raise HTTPException(status_code=400, detail="Agent is disabled")

    assignment: Assignment | None = None
    if parent_run.assignment_id:
        wf_result = await db.execute(
            select(Assignment).where(Assignment.id == parent_run.assignment_id)
        )
        assignment = wf_result.scalar_one_or_none()

    assignment_name = assignment.name if assignment else (agent.name or "this run")

    # Create the conversation routed to the run's agent.
    conv = Conversation(
        user_id=user.id,
        channel="web",
        agent_id=agent.id,
        title=f"Follow-up: {assignment_name}"[:500],
    )
    db.add(conv)
    await db.flush()

    # Seed the conversation so the agent (and the user) can review what happened.
    task = _extract_run_task(parent_run, assignment)
    if len(task) > _FOLLOWUP_TASK_CHARS:
        task = task[:_FOLLOWUP_TASK_CHARS] + "\n\n… (task truncated)"
    seed_user = (
        f"I'm reviewing the assignment run **{assignment_name}** "
        f"(status: {parent_run.status}). Here is the original task you were "
        f"given for this run:\n\n{task or '(no task recorded)'}"
    )

    result_text = (
        parent_run.result_summary or parent_run.error or "(no result was produced)"
    )
    trace = _build_run_execution_trace(parent_run)
    seed_assistant = f"{result_text}\n\n---\n\n{trace}" if trace else result_text

    # PostgreSQL text columns reject null bytes (0x00). The seed content is
    # derived from arbitrary run output (the rendered prompt, tool results),
    # so strip them the same way store_message() does to avoid a 500 on insert.
    seed_user = seed_user.replace("\x00", "")
    seed_assistant = seed_assistant.replace("\x00", "")

    now = datetime.now(UTC)
    db.add(
        Message(
            conversation_id=conv.id,
            role="user",
            content=seed_user,
            channel="web",
            sequence=1,
            created_at=now,
        )
    )
    db.add(
        Message(
            conversation_id=conv.id,
            role="assistant",
            content=seed_assistant,
            channel="web",
            sequence=2,
            created_at=now,
        )
    )
    # Keep the conversation's updated_at in lockstep with its newest seeded
    # message so it sorts correctly in the conversation list (mirrors what
    # store_message() does for normal chat writes).
    conv.updated_at = now
    await db.flush()

    await log_audit(
        db,
        user=user,
        action="followup_chat",
        entity_type="assignment_run",
        entity_id=parent_run.id,
        entity_name=assignment_name,
        summary=f"Opened interactive follow-up chat for '{assignment_name}'",
        details={
            "parent_run_id": str(parent_run.id),
            "conversation_id": str(conv.id),
            "agent_id": str(agent.id),
            "agent_name": agent.name,
            "prompt_preview": (
                ((body.prompt[:200] + "...") if len(body.prompt) > 200 else body.prompt)
                if body and body.prompt
                else None
            ),
        },
        request=request,
    )
    # Commit before answering: the frontend opens this conversation as soon as it
    # gets the id, and the get_db teardown only commits after the response is sent.
    await db.commit()

    return FollowupChatResponse(conversation_id=str(conv.id), agent_id=str(agent.id))


def _trigger_config_str(value: object) -> str | None:
    """Coerce a free-form ``trigger_config`` JSON value to a clean string or None.

    ``trigger_config`` is user-controlled JSON; legacy / misconfigured rows can
    hold a non-string where a string is expected (e.g. ``cron`` of ``[]`` / ``0``,
    or a list ``provider`` / ``event``). The schedule endpoint feeds these into
    ``str | None`` response fields and into dict/set membership checks
    (``POLL_INTERVALS.get`` / ``in _PROVIDERS_WITH_ASSIGNMENT_WEBHOOKS``), so a
    single non-string row would 500 the whole endpoint — Pydantic validation, or
    ``TypeError: unhashable type`` on an unhashable key. Mirrors the defensive
    ``isinstance`` guard the scheduler already applies to the same data. Returns
    the stripped string, or None for any non-string / blank value.
    """
    # Guard the type first so a non-string short-circuits to None before any
    # ``.strip()`` (which would AttributeError on the very values this guards).
    if not isinstance(value, str):
        return None
    return value.strip() or None


@router.get("/schedule", response_model=list[AssignmentScheduleResponse])
async def list_assignment_schedule(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    fleet: Annotated[
        str | None,
        Query(
            description="A fleet id: only its home members' assignments. `none`: only the agents in no fleet (the playground).",
        ),
    ] = None,
):
    """Trigger + next-run summary for every visible, enabled assignment.

    Powers the Agentic Flow "Schedule" tab ("what will trigger, and when?").

    * ``next_run_at`` (schedule triggers) is computed from the cron expression,
      ``trigger_config.timezone`` and ``last_triggered_at`` with the same
      ``next_cron_run`` helper the background scheduler uses, so the UI can
      never drift from the tick that actually fires; ``next_run_local`` is the
      same instant in the configured zone, ``timezone_warning`` names an
      unknown zone (read as UTC).
    * ``pending_run_at`` is the earliest queued/delayed orchestration execution
      for the assignment (a run waiting on its ``delay``), read from the
      authoritative ``orchestration_executions.run_at``.

    Declared BEFORE ``GET /{assignment_id}`` so the literal ``schedule`` segment
    is not captured as an assignment id. Visibility is the list's across
    agents (``GET /assignments``): the assignments whose agent the caller can
    see, and every enabled assignment for an admin, so an admin's Schedule tab
    reflects the whole platform. ``?fleet=`` narrows it to a fleet's home
    members or to the playground: the schedule view of a fleet and of
    the agent-to-agent map.
    """
    from app.models.orchestration import OrchestrationExecution
    from app.services.assignment_engine import (
        ACTIVE_RUN_STATUSES,
        TERMINAL_RUN_STATUSES,
        count_queued_runs_by_assignment,
    )
    from app.services.event_poller import POLL_INTERVALS

    user_group_ids = [] if user.is_admin else await get_user_group_ids(db, user.id)
    # Like a disabled agent below: a paused assignment has no upcoming
    # run to announce, for an admin reading the whole platform too.
    result = await db.execute(
        select(Assignment).where(
            assignment_visible_filter(user.id, user_group_ids, is_admin=user.is_admin),
            Assignment.enabled.is_(True),
            owner_not_deactivated(),
            *([fleet_filter(Assignment.agent_id, fleet)] if fleet is not None else []),
        )
    )
    assignments = result.scalars().all()
    if not assignments:
        return []

    # Agent names/icons for display (batch) — also used to drop assignments
    # whose parent agent is missing or disabled. The scheduler, webhook
    # receiver and assignment engine all skip disabled agents, so their runs
    # never fire; surfacing them here would report upcoming / pending work that
    # can never execute.
    agent_ids = {w.agent_id for w in assignments}
    agents_result = await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
    agents_map = {a.id: a for a in agents_result.scalars().all()}
    assignments = [
        w
        for w in assignments
        if agents_map.get(w.agent_id) is not None and agents_map[w.agent_id].enabled
    ]
    if not assignments:
        return []

    assignment_ids = [w.id for w in assignments]

    # Which assignments currently have a non-terminal run (the scheduler would
    # skip them) and which delayed/pending run ids we still want execute-times
    # for — one grouped query.
    active_assignment_ids: set[uuid.UUID] = set()
    delayed_run_to_assignment: dict[str, uuid.UUID] = {}
    runs_result = await db.execute(
        select(
            AssignmentRun.id,
            AssignmentRun.assignment_id,
            AssignmentRun.status,
        ).where(
            AssignmentRun.assignment_id.in_(assignment_ids),
            AssignmentRun.status.in_(ACTIVE_RUN_STATUSES),
        )
    )
    for run_id, run_assignment_id, run_status in runs_result.all():
        active_assignment_ids.add(run_assignment_id)
        if run_status in ("pending", "delayed"):
            delayed_run_to_assignment[str(run_id)] = run_assignment_id

    # Backlog of accepted webhook deliveries waiting behind the active run.
    # Kept OUT of the query above on both counts: a queued run must
    # not mark the assignment active (it owns no execution row and no lease —
    # it is a backlog entry, not work in flight), and only its COUNT is needed,
    # so a grouped aggregate beats dragging up to ``ASSIGNMENT_QUEUE_MAX_DEPTH``
    # rows per assignment back just to tally them in Python.
    queued_counts = await count_queued_runs_by_assignment(db, assignment_ids)

    # The latest finished run of each assignment, its outcome and when it
    # finished: the row's outcome dot. One DISTINCT ON query, latest by
    # finish time (then creation, then id), so the status and the time shown
    # together belong to the same run. A terminal run that exited before
    # setting ``completed_at`` (assignment or agent unavailable at start) ended
    # where it began: its creation time stands for its finish time, so a newer
    # early failure is never ranked behind an older completed run. A non-admin
    # sees only the runs ``run_visible_filter`` lets them open, like on the run
    # list.
    run_finished_at = func.coalesce(AssignmentRun.completed_at, AssignmentRun.created_at)
    last_finished_query = (
        select(
            AssignmentRun.assignment_id,
            AssignmentRun.status,
            run_finished_at,
        )
        .where(
            AssignmentRun.assignment_id.in_(assignment_ids),
            AssignmentRun.status.in_(TERMINAL_RUN_STATUSES),
        )
        .distinct(AssignmentRun.assignment_id)
        .order_by(
            AssignmentRun.assignment_id,
            run_finished_at.desc(),
            AssignmentRun.created_at.desc(),
            AssignmentRun.id.desc(),
        )
    )
    if not user.is_admin:
        last_finished_query = last_finished_query.where(run_visible_filter(user.id))
    last_finished: dict[uuid.UUID, tuple[str, datetime | None]] = {
        run_assignment_id: (run_status, run_completed_at)
        for run_assignment_id, run_status, run_completed_at in (
            await db.execute(last_finished_query)
        ).all()
    }

    now = datetime.now(UTC)

    # Earliest pending execute-time per assignment, from the durable orchestration
    # queue. ``run_at`` is the authoritative "earliest eligible" time for a queued
    # run; it is legitimately in the PAST for a run that is due now but has not yet
    # been claimed by a worker, so we deliberately do NOT bound it by ``> now`` —
    # that would hide due-now pending runs and make the Schedule tab report no
    # pending time even though an execution is queued. ``state == "pending"``
    # already scopes this to executions the engine has not run yet.
    pending_map: dict[uuid.UUID, datetime] = {}
    if delayed_run_to_assignment:
        run_id_expr = OrchestrationExecution.payload["run_id"].astext
        orch_result = await db.execute(
            select(run_id_expr, OrchestrationExecution.run_at).where(
                OrchestrationExecution.adapter == "assignment",
                OrchestrationExecution.state == "pending",
                run_id_expr.in_(list(delayed_run_to_assignment.keys())),
            )
        )
        for run_id_text, run_at_val in orch_result.all():
            mapped_assignment_id = delayed_run_to_assignment.get(run_id_text)
            if mapped_assignment_id is None:
                continue
            current = pending_map.get(mapped_assignment_id)
            if current is None or run_at_val < current:
                pending_map[mapped_assignment_id] = run_at_val

    items: list[AssignmentScheduleResponse] = []
    for w in assignments:
        agent = agents_map.get(w.agent_id)
        config = w.trigger_config if isinstance(w.trigger_config, dict) else {}
        cron = (
            _trigger_config_str(config.get("cron"))
            if w.trigger_type == "schedule"
            else None
        )
        # The cron is read in ``trigger_config.timezone`` (UTC when absent or
        # unknown), with the scheduler's own ``next_cron_run``; the next run is
        # shown in UTC and in that zone, with the unknown-zone warning.
        schedule = (
            describe_schedule_next_run(config, schedule_base(w), now)
            if w.trigger_type == "schedule"
            else {}
        )

        webhook_url = None
        provider = None
        event = None
        poll_interval = None
        if w.trigger_type == "webhook":
            webhook_url = f"{settings.base_url}/api/hooks/assignments/{w.id}"
        elif w.trigger_type == "integration_event":
            provider = _trigger_config_str(config.get("provider"))
            event = _trigger_config_str(config.get("event"))
            integration_id = _trigger_config_str(config.get("integration_id")) or ""
            if event:
                poll_interval = POLL_INTERVALS.get(event)
            if integration_id and provider in _PROVIDERS_WITH_ASSIGNMENT_WEBHOOKS:
                webhook_url = (
                    f"{settings.base_url}/api/hooks/integrations/"
                    f"{integration_id}/assignments/{w.id}"
                )

        pending_at = pending_map.get(w.id)
        items.append(
            AssignmentScheduleResponse(
                assignment_id=str(w.id),
                assignment_name=w.name,
                agent_id=str(w.agent_id),
                agent_name=agent.name if agent else "Unknown",
                agent_icon=agent.icon if agent else "Bot",
                trigger_type=w.trigger_type,
                enabled=w.enabled,
                cron=cron,
                next_run_at=schedule.get("next_run_at"),
                timezone=schedule.get("timezone"),
                next_run_local=schedule.get("next_run_local"),
                timezone_warning=schedule.get("timezone_warning"),
                delay_seconds=parse_delay(w.delay),
                last_triggered_at=(
                    w.last_triggered_at.isoformat() if w.last_triggered_at else None
                ),
                last_run_status=last_finished[w.id][0] if w.id in last_finished else None,
                last_run_finished_at=(
                    finished_at.isoformat()
                    if w.id in last_finished
                    and (finished_at := last_finished[w.id][1]) is not None
                    else None
                ),
                has_active_run=w.id in active_assignment_ids,
                pending_run_at=pending_at.isoformat() if pending_at else None,
                queued_run_count=queued_counts.get(w.id, 0),
                provider=provider,
                event=event,
                poll_interval_seconds=poll_interval,
                webhook_url=webhook_url,
            )
        )

    # Soonest-first by next_run_at (ISO strings sort chronologically); rows with
    # no computed next run sort last.
    items.sort(key=lambda i: i.next_run_at or "9999")
    return items


@router.get("/platform-events")
async def list_platform_events(user: User = Depends(get_current_user)) -> list[dict]:
    """The platform events a ``platform_event`` trigger can name, with their filter keys.

    The trigger picker reads it; it is the same catalog the agent builder's
    ``list_platform_events`` tool reads, so the two never drift.
    """
    from app.services.platform_events import catalog

    return catalog()


@router.get("/platform-event-watches")
async def list_platform_event_watches(
    include_ended: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """The caller's one-off watches that wake a background task."""
    from app.services.platform_event_task_watches import describe, list_watches

    return [describe(w) for w in await list_watches(db, user_id=user.id, include_ended=include_ended)]


@router.post("/platform-event-watches", status_code=status.HTTP_201_CREATED)
async def create_platform_event_watch(
    body: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> dict:
    """Arm a one-off watch: the first matching event starts one background task of ``agent_id``.

    Body: ``{agent_id, task, event, expires_at, filter?, label?}``. The caller
    must be able to add an assignment to the agent (the same gate).
    """
    from app.services.platform_event_task_watches import TaskWatchError, create_watch, describe

    user_group_ids = [] if user.is_admin else await get_user_group_ids(db, user.id)
    agent = await get_user_agent(db, user.id, str(body.get("agent_id") or ""), user_group_ids, is_admin=user.is_admin)
    assert_writable_agent(agent, user, user_group_ids, action="add a watch to")
    if agent.enabled is False:
        raise HTTPException(status_code=400, detail="The agent is disabled.")
    try:
        watch = await create_watch(
            db,
            user_id=user.id,
            agent_id=agent.id,
            task=body.get("task"),
            events=body.get("event"),
            filter=body.get("filter"),
            expires_at=body.get("expires_at"),
            label=body.get("label") if isinstance(body.get("label"), str) else None,
        )
    except TaskWatchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return describe(watch)


@router.delete("/platform-event-watches/{watch_id}")
async def cancel_platform_event_watch(
    watch_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Cancel one of the caller's armed watches; a fired or expired one is returned as it is."""
    from app.services.platform_event_task_watches import cancel_watch, describe

    watch_uuid = parse_path_id(watch_id, not_found_detail="Watch not found")
    watch = await cancel_watch(db, user_id=user.id, watch_id=watch_uuid)
    if watch is None:
        raise HTTPException(status_code=404, detail="Watch not found")
    return describe(watch)


# ---------------------------------------------------------------------------
# Parameterised assignment routes – /{assignment_id}
# ---------------------------------------------------------------------------


@router.get("/{assignment_id}", response_model=AssignmentResponse)
async def get_assignment(
    assignment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Group memberships only matter for non-admins: the admin lookup path
    # bypasses visibility entirely and the response serializer short-circuits
    # on admin before consulting group tiers — skip the DB roundtrip.
    user_group_ids = [] if user.is_admin else await get_user_group_ids(db, user.id)
    assignment = await get_user_assignment(
        db, user.id, assignment_id, user_group_ids, is_admin=user.is_admin
    )
    # ``webhook_secret`` visibility depends on the parent agent's group-share
    # permission tier — load the agent only when the check is actually needed
    # (a secret is present and the viewer is not admin/owner of a personal
    # assignment).
    agent = None
    if (
        not user.is_admin
        and isinstance(assignment.trigger_config, dict)
        and "webhook_secret" in assignment.trigger_config
        and not assignment.is_company_managed
        and assignment.user_id != user.id
    ):
        agent_row = await db.execute(
            select(Agent).where(Agent.id == assignment.agent_id)
        )
        agent = agent_row.scalar_one_or_none()
    return _to_response(
        assignment,
        user=user,
        agent=agent,
        user_group_ids=user_group_ids,
        owner_paused=await _owner_paused(db, assignment, user),
    )


def _builtin_allowed_or_422(value):
    """``builtin_allowed_tools`` normalised, or a 422 naming the valid functions."""
    from app.services.builtin_allowed_tools import (
        BuiltinAllowedToolsError,
        normalize_builtin_allowed_tools,
    )

    try:
        return normalize_builtin_allowed_tools(value)
    except BuiltinAllowedToolsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _gate_fleet_or_422(db: AsyncSession, check: object) -> None:
    """422 when the gate check names a fleet that does not exist (#1400)."""
    from app.services.fleets import gate_fleet_refusal

    refusal = await gate_fleet_refusal(db, check)
    if refusal is not None:
        raise HTTPException(status_code=422, detail=refusal)


#: Fields ``update_assignment`` copies verbatim when the body sets them.
_UPDATE_LOOP_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "trigger_type",
    "trigger_config",
    "activation_prompt",
    "activation_check",
    "prompt",
    "delay",
    "tool_sources",
    "integration_bindings",
    "integration_allowed_tools",
    "builtin_allowed_tools",
    "integration_element_bindings",
    "integration_element_enforcement",
    "use_knowledge_bases",
    "read_knowledge_base_ids",
    "write_knowledge_base_ids",
    # Runner refinement: None (inherit) / [] (none) / subset all pass through
    # via setattr — no []→None normalization (unlike read KBs), because []
    # means "no runners" here.
    "runner_ids",
    "write_knowledge_base_enabled",
    "ai_label",
    "ai_label_prompt",
    "notify_on_complete",
    "show_in_digest",
    "human_in_the_loop",
    "human_in_the_loop_prompt",
    "run_as_all_users",
    "hide_runs",
    "timeout_seconds",
    "ignore_self_triggered_events",
    "retry_on_failure",
    "max_retries",
    "dry_run",
    "manual_trigger_parameters",
    "enabled",
)

#: The subset backed by a NOT NULL column. Every request field is ``X | None``,
#: so an explicit ``null`` there means "no change": written through it would be
#: a NOT NULL violation and a 500. Derived from the model so a new
#: field cannot be forgotten; nullable fields keep writing ``None`` (clear /
#: inherit).
_NON_NULLABLE_UPDATE_FIELDS: frozenset[str] = frozenset(
    field for field in _UPDATE_LOOP_FIELDS if not Assignment.__table__.c[field].nullable
)


@router.put("/{assignment_id}", response_model=AssignmentResponse)
async def update_assignment(
    assignment_id: str,
    body: UpdateAssignmentRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    user_group_ids = await get_user_group_ids(db, user.id)
    assignment = await get_user_assignment(
        db, user.id, assignment_id, user_group_ids, is_admin=user.is_admin
    )
    # Locked and re-read in place until the commit: the stored cron carried
    # forward below is read from this row, so a concurrent update must not
    # change it in between.
    await db.execute(
        select(Assignment)
        .where(Assignment.id == assignment.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    parent_agent = await db.execute(
        select(Agent).where(Agent.id == assignment.agent_id)
    )
    agent_row = parent_agent.scalar_one_or_none()
    if not agent_row:
        raise HTTPException(status_code=404, detail="Parent agent not found")
    # Consolidated gate (built-in + company-managed admin + group permission).
    # On a built-in agent the switch alone may be written on any assignment
    # (#2192), and a seeded opt-in one also takes its run-as identity: the
    # built-in gate alone is dropped, the company-managed one still applies.
    assert_writable_assignment(
        assignment,
        agent_row,
        user,
        user_group_ids,
        action="edit",
        allow_builtin=builtin_assignment_write_allowed(agent_row, assignment, body.model_fields_set),
    )
    if "tool_sources" in body.model_fields_set:
        await assert_tool_source_additions_allowed(
            db,
            user=user,
            agent=agent_row,
            value=body.tool_sources,
            baseline=assignment.tool_sources,
            user_group_ids=user_group_ids,
        )
    if "integration_bindings" in body.model_fields_set:
        await assert_integration_additions_allowed(
            db,
            user=user,
            agent=agent_row,
            value=body.integration_bindings,
            baseline=assignment.integration_bindings,
            user_group_ids=user_group_ids,
        )
    if "channel_config" in body.model_fields_set:
        await _assert_channel_override_additions(
            db,
            user=user,
            agent=agent_row,
            value=body.channel_config,
            baseline=assignment.channel_config,
            user_group_ids=user_group_ids,
        )
    if "trigger_config" in body.model_fields_set and body.trigger_config is not None:
        await assert_trigger_integration_allowed(
            db,
            user=user,
            value=body.trigger_config,
            baseline=assignment.trigger_config,
            user_group_ids=user_group_ids,
        )

    # The loop below replaces ``trigger_config`` rather than merging it, and the
    # scheduler silently skips a schedule without a cron. A new config without
    # a cron on an assignment that is and stays a schedule therefore keeps the
    # stored cron, every other key as sent. Only ``None`` leaves the stored
    # type in place (the loop skips it); any other ``trigger_type``, an empty
    # one included, is the type saved. A ``cron`` key the request does send,
    # even null or empty, is validated by the request schema.
    stored_config = assignment.trigger_config
    stored_cron = stored_config.get("cron") if isinstance(stored_config, dict) else None
    saved_type = assignment.trigger_type if body.trigger_type is None else body.trigger_type
    if (
        body.trigger_config is not None
        and "cron" not in body.trigger_config
        and assignment.trigger_type == "schedule"
        and saved_type == "schedule"
        and stored_cron is not None
    ):
        body.trigger_config = {**body.trigger_config, "cron": stored_cron}
    # A batch window as it will be saved: judged against the type the row keeps.
    if body.trigger_config is not None:
        from app.services.event_batching import window_error

        batch_error = window_error(saved_type, body.trigger_config)
        if batch_error is not None:
            raise HTTPException(status_code=422, detail=batch_error)
    # A platform_event trigger as it will be saved: a config sent alone
    # is judged against the stored type, which the request schema cannot see.
    if saved_type == "platform_event" and body.trigger_config is not None:
        from app.services.platform_events import trigger_config_error

        platform_event_error = trigger_config_error(saved_type, body.trigger_config)
        if platform_event_error is not None:
            raise HTTPException(status_code=422, detail=platform_event_error)
        from app.services.platform_events import fleet_scope_error

        fleet_error = await fleet_scope_error(saved_type, body.trigger_config, assignment.agent_id)
        if fleet_error is not None:
            raise HTTPException(status_code=422, detail=fleet_error)
    # A work_item inbox's parallel runs, judged as it will be saved:
    # a config sent alone keeps the stored type.
    if saved_type == "work_item" and body.trigger_config is not None:
        from app.services.assignment_dispatch import work_item_concurrency_error

        concurrency_error = work_item_concurrency_error(body.trigger_config)
        if concurrency_error is not None:
            raise HTTPException(status_code=422, detail=concurrency_error)
    # A provision block judged as it will be saved, refused now rather than
    # on the scheduler pass (#970).
    if body.trigger_config is not None:
        from app.services.trigger_provisioning import provision_spec_error

        provision_error = provision_spec_error(saved_type, body.trigger_config)
        if provision_error is not None:
            raise HTTPException(status_code=422, detail=provision_error)

    # Pause provenance: remember the state BEFORE the generic setattr
    # loop below so a pause/resume can be told apart from any other edit and
    # stamped with who did it. Without this the only record of a human pausing
    # an assignment was an "Updated assignment" audit row indistinguishable
    # from a rename.
    was_enabled = assignment.enabled
    # Before any mutation: ``get_db`` commits on teardown, so a refusal must
    # happen while the row is still untouched.
    if getattr(body, "enabled", None) is True and not was_enabled:
        ensure_not_emergency_stopped(assignment)
    if "activation_check" in body.model_fields_set and body.activation_check is not None:
        await _gate_fleet_or_422(db, body.activation_check)

    for field in _UPDATE_LOOP_FIELDS:
        if field not in body.model_fields_set:
            continue
        value = getattr(body, field)
        if value is None and field in _NON_NULLABLE_UPDATE_FIELDS:
            continue
        setattr(assignment, field, value)

    # A Scope(s) enforcement flag only lives next to its selection:
    # any change to either side keeps the flags of the types still selected.
    if {"integration_element_bindings", "integration_element_enforcement"} & body.model_fields_set:
        kept = normalize_scope_enforcement(
            assignment.integration_element_enforcement, assignment.integration_element_bindings
        )
        if kept != assignment.integration_element_enforcement:
            assignment.integration_element_enforcement = kept

    # wait_policy: null or {} clears the override (inherit the agent's).
    if "wait_policy" in body.model_fields_set:
        assignment.wait_policy = checked_wait_policy_override(body.wait_policy)

    # organization_context_mode: null goes back to "inherit" (#11533).
    if "organization_context_mode" in body.model_fields_set:
        assignment.organization_context_mode = body.organization_context_mode or "inherit"
    # organization_context_categories: null inherits the agent's, then the platform's (#11533).
    if "organization_context_categories" in body.model_fields_set:
        assignment.organization_context_categories = _checked_org_categories(body.organization_context_categories)

    # max_iterations accepts null as meaningful (null = inherit: agent override
    # -> platform default), so check model_fields_set to distinguish "not
    # provided" from "explicitly null". A real value is clamped to the shared
    # ceiling so a direct API call can't exceed the UI slider.
    if "max_iterations" in body.model_fields_set:
        assignment.max_iterations = (
            clamp_max_iterations(body.max_iterations)
            if body.max_iterations is not None
            else None
        )
    elif assignment.max_iterations is not None:
        # Defensively pull a legacy out-of-range value back under the ceiling
        # even when this update doesn't touch max_iterations: this column used
        # to accept arbitrary ints (no schema bound, no server-side clamp), so a
        # stale value could already exceed it (mirrors update_agent). Preserves
        # NULL (= inherit) and only writes when clamping changes the value, so
        # in-range assignments see no spurious update / version churn.
        _clamped = clamp_max_iterations(assignment.max_iterations)
        if _clamped != assignment.max_iterations:
            assignment.max_iterations = _clamped

    # Auto-generate a webhook secret when the trigger is being (re)configured
    # as a webhook without one (SEC-5). Scoped to trigger edits only so an
    # unrelated update (rename, prompt tweak) never silently flips a legacy
    # secretless assignment to signature-required and breaks its sender.
    # A trigger field sent as an explicit ``null`` was skipped by the loop
    # above, so it is not a trigger edit either.
    _trigger_type_edited = (
        "trigger_type" in body.model_fields_set and body.trigger_type is not None
    )
    _trigger_config_edited = (
        "trigger_config" in body.model_fields_set and body.trigger_config is not None
    )
    if _trigger_type_edited or _trigger_config_edited:
        assignment.trigger_config = ensure_webhook_secret(
            assignment.trigger_type, assignment.trigger_config
        )

    # Smart default: switching an assignment TO the work_item trigger
    # auto-enables the agent's ``work_items`` built-in tool (opt-out) so the
    # inbox worker can close / claim / manage its queue. Only on the transition
    # (trigger_type in this update's fields), never re-added on unrelated saves,
    # so a deliberate uncheck sticks.
    if _trigger_type_edited and assignment.trigger_type == "work_item":
        _bt_ids = list(agent_row.builtin_tool_ids or [])
        if "work_items" not in _bt_ids:
            agent_row.builtin_tool_ids = [*_bt_ids, "work_items"]

    # channel_config accepts null as meaningful (null = inherit from agent)
    if "channel_config" in body.model_fields_set:
        assignment.channel_config = body.channel_config

    # Built-in function scoping: validated against the group's
    # functions (a 422 names them), null-valued groups dropped, {} -> None.
    if "builtin_allowed_tools" in body.model_fields_set:
        assignment.builtin_allowed_tools = _builtin_allowed_or_422(
            assignment.builtin_allowed_tools
        )

    # Normalize integration_allowed_tools: strip null-valued keys, empty dict → None,
    # and split an ``openapi`` allow-list per OpenAPI integration.
    if "integration_allowed_tools" in body.model_fields_set:
        raw = assignment.integration_allowed_tools
        if isinstance(raw, dict):
            cleaned = {k: v for k, v in raw.items() if v is not None}
            assignment.integration_allowed_tools = cleaned if cleaned else None
        allowed = assignment.integration_allowed_tools
        from app.services.openapi_import import openapi_integration_ids as _openapi_ids
        from app.services.tool_scope_names import integration_allowed_or_422

        await integration_allowed_or_422(
            db,
            allowed,
            _openapi_ids(assignment.integration_bindings or agent_row.integration_bindings)
            if isinstance(allowed, dict) and "openapi" in allowed
            else None,
        )
        if isinstance(allowed, dict) and "openapi" in allowed:
            # A list under the shared ``openapi`` key is read per integration.
            from app.services.openapi_import import (
                openapi_integration_ids,
                per_integration_tool_scopes,
            )

            assignment.integration_allowed_tools = await per_integration_tool_scopes(
                db,
                allowed,
                openapi_integration_ids(
                    assignment.integration_bindings or agent_row.integration_bindings
                ),
            )

    # Handle target_knowledge_base_id separately (UUID conversion + allow clearing)
    if "target_knowledge_base_id" in body.model_fields_set:
        assignment.target_knowledge_base_id = (
            uuid.UUID(body.target_knowledge_base_id)
            if body.target_knowledge_base_id
            else None
        )

    # Normalize an empty read selection to NULL — read has no "none" state, an
    # empty list always means "inherit all agent KBs". This keeps the column
    # clean and prevents an assignment from getting stuck showing "None".
    if "read_knowledge_base_ids" in body.model_fields_set and not (
        assignment.read_knowledge_base_ids
    ):
        assignment.read_knowledge_base_ids = None

    # "Run as" identity — only meaningful for company-managed agents. The
    # assert_writable_assignment gate above already requires admin for
    # company-managed assignments, so only admins can reach this for them.
    _run_as_before = assignment.run_as_user_id
    if "run_as_user_id" in body.model_fields_set:
        assignment.run_as_user_id = await _resolve_run_as_user_id(
            db, agent_row, body.run_as_user_id
        )
    # A seeded fleet automation runs as the administrator who switches it on,
    # never the seed owner, and only with the module connected for them (#9188).
    from app.services.fleet_run_as import apply_fleet_run_as

    await apply_fleet_run_as(
        db,
        agent=agent_row,
        assignment=assignment,
        actor_id=user.id,
        was_enabled=was_enabled,
        run_as_changed=assignment.run_as_user_id != _run_as_before,
    )

    # Pause provenance. The generic loop above already wrote
    # ``enabled``; this stamps WHO/WHEN/WHY on the transition (or clears the
    # stamp on resume) so the assignment page can explain the "Paused" badge.
    # Only on an actual transition — an unrelated save that happens to carry
    # ``enabled`` unchanged must not restamp the pause with a new date.
    paused_now = was_enabled and not assignment.enabled
    resumed_now = not was_enabled and assignment.enabled
    if paused_now:
        actor = actor_display_name(user)
        pause_details = f"Paused from the UI by {actor}."
        # Off before its first run: still being set up, so its fleet reads
        # "Not set up" rather than "Paused" (#11060).
        toggle_reason = switch_off_reason(assignment, "user_toggle")
        pause_assignment(
            assignment,
            reason=toggle_reason,
            details=pause_details,
            actor_user_id=user.id,
            actor_label=actor,
        )
        # Written BEFORE the commit, like the resume row below: both audit rows
        # then land in the same transaction as the state change they describe,
        # instead of one on each side of it.
        await record_pause_audit(
            db,
            assignment=assignment,
            user=user,
            reason=toggle_reason,
            details=pause_details,
            request=request,
        )
    elif resumed_now:
        # Clears the stamp and audits the resume — ``enabled`` is already True.
        await resume_assignment(
            db, assignment=assignment, user=user, request=request
        )

    await db.flush()
    await db.refresh(assignment)
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="assignment",
        entity_id=assignment.id,
        entity_name=assignment.name,
        summary=f"Updated assignment '{assignment.name}'",
        details={
            "agent_id": str(assignment.agent_id),
            "trigger_type": assignment.trigger_type,
            "enabled": assignment.enabled,
            "delay": assignment.delay,
            "ai_label": assignment.ai_label,
            "human_in_the_loop": assignment.human_in_the_loop,
        },
        request=request,
    )
    # One commit for the update and its audit entry, before answering: the
    # get_db teardown only commits after the response is sent.
    await db.commit()
    return _to_response(
        assignment,
        user=user,
        agent=agent_row,
        user_group_ids=user_group_ids,
        owner_paused=await _owner_paused(db, assignment, user),
    )


@router.delete("/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_assignment(
    assignment_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    user_group_ids = await get_user_group_ids(db, user.id)
    assignment = await get_user_assignment(
        db, user.id, assignment_id, user_group_ids, is_admin=user.is_admin
    )

    parent_agent_del = await db.execute(
        select(Agent).where(Agent.id == assignment.agent_id)
    )
    agent_row_del = parent_agent_del.scalar_one_or_none()
    if not agent_row_del:
        raise HTTPException(status_code=404, detail="Parent agent not found")
    # Consolidated gate (built-in + company-managed admin + group permission).
    assert_writable_assignment(
        assignment, agent_row_del, user, user_group_ids, action="delete"
    )
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="assignment",
        entity_id=assignment.id,
        entity_name=assignment.name,
        summary=f"Deleted assignment '{assignment.name}'",
        details={
            "agent_id": str(assignment.agent_id),
            "trigger_type": assignment.trigger_type,
        },
        request=request,
    )
    # What was provisioned for it in its source platform goes with it.
    from app.services.trigger_provisioning import deprovision_assignment

    await deprovision_assignment(db, assignment)
    # A deleted seeded fleet health check stays deleted: its fleet opts out.
    from app.services.fleets import record_health_check_deleted

    await record_health_check_deleted(db, assignment, actor=str(user.id))
    await db.delete(assignment)
    # Same as the create: the list refetched right after the 204 must not
    # still hold the deleted row.
    await db.commit()


@router.post("/{assignment_id}/trigger", response_model=AssignmentRunResponse)
async def trigger_assignment_manual(
    assignment_id: str,
    request: Request,
    body: TriggerAssignmentRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Manually trigger a assignment execution."""
    from app.services.assignment_engine import trigger_assignment as do_trigger

    assignment = await get_user_assignment(
        db, user.id, assignment_id, is_admin=user.is_admin
    )
    agent_result = await db.execute(
        select(Agent).where(Agent.id == assignment.agent_id)
    )
    agent = agent_result.scalar_one_or_none()
    # One gate with the agent tools that start a run as their caller
    # (agent-builder ``trigger_assignment``, work-items delegation).
    refusal = manual_trigger_refusal(assignment, agent, is_admin=user.is_admin)
    if refusal is None:
        # Paused while its owner is deactivated - for an admin too,
        # although the run would execute as them.
        await refuse_if_owner_deactivated(db, assignment)
        refusal = await manual_trigger_overlap(db, assignment, agent, user.id)
    if refusal is not None:
        raise HTTPException(status_code=refusal.status_code, detail=refusal.detail)

    payload = (body.payload if body else None) or {"trigger_type": "manual"}
    trigger_context = {
        "type": "manual",
        "user_id": str(user.id),
        "user_email": user.email,
        "user_name": user.full_name,
        "triggered_at": datetime.now(UTC).isoformat(),
    }
    if body and body.payload:
        trigger_context["payload"] = body.payload
    run_scope = manual_trigger_run_scope(assignment, agent, user.id)
    run_id = await do_trigger(
        assignment=assignment,
        agent=agent,
        payload=payload,
        db=db,
        trigger_context=trigger_context,
        # Manual trigger runs on behalf of the user who clicked — their
        # integrations / model / memory — not the assignment's "run as" user.
        triggered_by_user_id=user.id,
        # The engine's lock makes the overlap check above atomic.
        skip_if_active=True,
        active_run_user_id=run_scope,
    )
    if run_id is None:
        skipped = manual_trigger_skipped(run_scope)
        raise HTTPException(status_code=skipped.status_code, detail=skipped.detail)
    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="assignment",
        entity_id=assignment.id,
        entity_name=assignment.name,
        summary=f"Manually triggered assignment '{assignment.name}'",
        details={
            "run_id": str(run_id),
            "agent_id": str(agent.id),
            "agent_name": agent.name,
            "trigger_type": "manual",
        },
        request=request,
    )

    # Return the run
    run_result = await db.execute(
        select(AssignmentRun).where(AssignmentRun.id == run_id)
    )
    run = run_result.scalar_one()
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return _run_to_response(run, user=user)


@router.get("/{assignment_id}/runs", response_model=list[AssignmentRunResponse])
async def list_assignment_runs(
    assignment_id: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    search: str | None = None,
    parent_run_id: str | None = "none",
    review: Annotated[str | None, Query(description=_REVIEW_DESCRIPTION, pattern=_REVIEW_PATTERN)] = None,
    sort: Annotated[str | None, Query(description=_RUN_SORT_DESCRIPTION, pattern=_RUN_SORT_PATTERN)] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await get_user_assignment(db, user.id, assignment_id, is_admin=user.is_admin)
    if await _hides_parent_filter(db, parent_run_id, user):
        return []

    # The assignment Activity tab counts these rows through ``GET /runs/count``
    # with this assignment and ``scope=all``: the same query, so the search and
    # the run visibility (every run for an admin, own + company-managed runs for
    # anyone else, a group-shared agent included) are one predicate for both.
    query = _filtered_runs_query(
        user,
        search=search,
        assignment_id=assignment_id,
        parent_run_id=parent_run_id,
        scope="all",
        review=review,
    )
    result = await db.execute(
        query.order_by(*_run_order(sort)).offset(offset).limit(limit)
    )
    runs = result.scalars().all()
    openable = await openable_parent_runs(
        db,
        {r.parent_run_id for r in runs if r.parent_run_id},
        user.id,
        is_admin=user.is_admin,
    )
    return [
        _run_to_response(
            r, user=user, parent_shown=parent_is_shown(r, openable, is_admin=user.is_admin)
        )
        for r in runs
    ]


@router.delete("/{assignment_id}/runs", status_code=status.HTTP_204_NO_CONTENT)
async def purge_assignment_runs(
    assignment_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete all runs for a specific assignment."""
    user_group_ids = [] if user.is_admin else await get_user_group_ids(db, user.id)
    assignment = await get_user_assignment(
        db, user.id, assignment_id, user_group_ids, is_admin=user.is_admin
    )
    # Every run goes, other users' private runs included: the tier that may
    # delete the assignment (and its runs with it) - an admin for a
    # company-managed one - never a read-only grant.
    parent_agent = await db.execute(
        select(Agent).where(Agent.id == assignment.agent_id)
    )
    agent_row = parent_agent.scalar_one_or_none()
    if not agent_row:
        raise HTTPException(status_code=404, detail="Parent agent not found")
    assert_writable_assignment(
        assignment,
        agent_row,
        user,
        user_group_ids,
        action="purge runs for",
        allow_builtin=True,
    )
    await log_audit(
        db,
        user=user,
        action="purge_runs",
        entity_type="assignment",
        entity_id=assignment.id,
        entity_name=assignment.name,
        summary=f"Purged all runs for assignment '{assignment.name}'",
        details={"agent_id": str(assignment.agent_id)},
        request=request,
    )
    # Clean up file attachments for runs being purged
    from app.services.file_cleanup import delete_file_attachments

    run_ids_result = await db.execute(
        select(AssignmentRun.id).where(AssignmentRun.assignment_id == assignment.id)
    )
    purged_run_ids = [row[0] for row in run_ids_result.all()]
    for rid in purged_run_ids:
        await delete_file_attachments(db, assignment_run_id=rid)

    # Cancel orphaned orchestration executions + background tasks for the runs.
    from app.services.orchestration_engine import cancel_executions_for_runs
    from app.services.sub_agent import mark_sub_agent_tasks_cancelled_for_runs

    await cancel_executions_for_runs(db, purged_run_ids)
    await mark_sub_agent_tasks_cancelled_for_runs(db, purged_run_ids)

    await db.execute(
        sql_delete(AssignmentRun).where(
            AssignmentRun.assignment_id == assignment.id,
        )
    )
    assignment.execution_count = 0
    await db.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_channel_override_additions(
    db: AsyncSession,
    *,
    user: User,
    agent: Agent,
    value: dict | None,
    baseline: dict | None,
    user_group_ids: list[str] | None,
) -> None:
    """An assignment's channel override replaces the agent's at run time, so an
    entry naming a bot neither the row nor the agent already names must name a
    bot the writer can see and pass the agent's own channel check
    (``validate_channel_config``). Entries already there are not re-checked,
    except for the admin-only ``bypass_whitelist``, which no entry may turn on
    for a non-admin (``assert_whitelist_bypass_not_added``).
    """
    if not value:
        return
    added = await assert_channel_bot_additions_allowed(
        db,
        user=user,
        channel_config=value,
        baseline=baseline,
        agent_bound=channel_bot_ids(agent.channel_config),
        user_group_ids=user_group_ids,
    )
    assert_whitelist_bypass_not_added(
        value, user=user, baseline=baseline, agent_config=agent.channel_config
    )
    if added:
        from app.services.agent_bindings import validate_channel_config

        await validate_channel_config(
            db,
            channel_config=deepcopy(added),
            is_company_managed=agent.is_company_managed,
            is_admin=user.is_admin,
        )


# Providers that support per-assignment webhook URLs (dedicated URL per assignment).
# These providers allow configuring as many webhooks as needed on their side,
# so each Copilot assignment can have its own dedicated endpoint.
_PROVIDERS_WITH_ASSIGNMENT_WEBHOOKS: set[str] = {"opencti", "perk", "github", "hubspot", "aikido", "airwallex"}


async def _owner_paused(db: AsyncSession, assignment: Assignment, user: User) -> bool:
    """``paused_by_owner_deactivation`` of one assignment for *user*.

    No query when *user* owns it: a signed-in account is an active one.
    """
    if assignment.user_id == user.id:
        return False
    return await is_paused_by_deactivated_owner(db, assignment)


def _to_response(
    w: Assignment,
    *,
    user: User,
    agent: Agent | None = None,
    user_group_ids: list[str] | None = None,
    owner_paused: bool = False,
) -> AssignmentResponse:
    webhook_url = None
    integration_webhook_url = None

    if w.trigger_type == "webhook":
        webhook_url = f"{settings.base_url}/api/hooks/assignments/{w.id}"
    elif w.trigger_type == "integration_event":
        config = w.trigger_config or {}
        provider = config.get("provider", "")
        integration_id = config.get("integration_id", "")
        if integration_id:
            # Generic integration-level URL (fans out to all matching assignments)
            integration_webhook_url = (
                f"{settings.base_url}/api/hooks/integrations/{integration_id}"
            )
            # Per-assignment URL — only for providers that support it
            if provider in _PROVIDERS_WITH_ASSIGNMENT_WEBHOOKS:
                webhook_url = f"{settings.base_url}/api/hooks/integrations/{integration_id}/assignments/{w.id}"

    return AssignmentResponse(
        id=str(w.id),
        agent_id=str(w.agent_id),
        user_id=str(w.user_id) if w.user_id else None,
        is_company_managed=w.is_company_managed,
        run_as_user_id=str(w.run_as_user_id) if w.run_as_user_id else None,
        run_as_all_users=bool(w.run_as_all_users),
        builtin_admin_opt_in=is_admin_opt_in_builtin_assignment(agent, w),
        hide_runs=w.hide_runs,
        name=w.name,
        description=w.description,
        trigger_type=w.trigger_type,
        trigger_config=_visible_trigger_config(w, user, agent, user_group_ids),
        event_matching=(
            ("exact" if uses_exact_event_matching(w.trigger_config) else "legacy")
            if w.trigger_type in EVENT_MATCHING_TRIGGER_TYPES
            else None
        ),
        activation_prompt=w.activation_prompt,
        activation_check=(
            w.activation_check if isinstance(getattr(w, "activation_check", None), dict) else None
        ),
        prompt=w.prompt,
        delay=w.delay,
        tool_sources=w.tool_sources,
        integration_bindings=w.integration_bindings,
        integration_allowed_tools=w.integration_allowed_tools,
        builtin_allowed_tools=getattr(w, "builtin_allowed_tools", None),
        integration_element_bindings=w.integration_element_bindings,
        integration_element_enforcement=w.integration_element_enforcement,
        channel_config=w.channel_config,
        use_knowledge_bases=w.use_knowledge_bases,
        read_knowledge_base_ids=w.read_knowledge_base_ids,
        write_knowledge_base_ids=w.write_knowledge_base_ids,
        runner_ids=w.runner_ids,
        write_knowledge_base_enabled=w.write_knowledge_base_enabled,
        target_knowledge_base_id=(
            str(w.target_knowledge_base_id) if w.target_knowledge_base_id else None
        ),
        ai_label=w.ai_label,
        ai_label_prompt=w.ai_label_prompt,
        notify_on_complete=w.notify_on_complete,
        show_in_digest=w.show_in_digest,
        human_in_the_loop=w.human_in_the_loop,
        human_in_the_loop_prompt=w.human_in_the_loop_prompt,
        max_iterations=w.max_iterations,
        wait_policy=getattr(w, "wait_policy", None) or None,
        organization_context_mode=getattr(w, "organization_context_mode", None) or "inherit",
        organization_context_categories=getattr(w, "organization_context_categories", None),
        timeout_seconds=w.timeout_seconds,
        ignore_self_triggered_events=w.ignore_self_triggered_events,
        retry_on_failure=w.retry_on_failure,
        max_retries=w.max_retries,
        # Without this mapping the response always carried the schema default
        # (False), so the UI could never see a dry-run assignment as such.
        dry_run=w.dry_run,
        manual_trigger_parameters=w.manual_trigger_parameters,
        enabled=w.enabled,
        # Pause provenance — what the UI needs to explain the "Paused"
        # badge without a second request or a join.
        paused_at=w.paused_at.isoformat() if w.paused_at else None,
        paused_reason=w.paused_reason,
        paused_by_user_id=(
            str(w.paused_by_user_id) if w.paused_by_user_id else None
        ),
        paused_by_label=w.paused_by_label,
        paused_details=w.paused_details,
        paused_by_owner_deactivation=owner_paused,
        execution_count=w.execution_count,
        events_filtered_count=(
            w.events_filtered_count if isinstance(getattr(w, "events_filtered_count", None), int) else 0
        ),
        last_triggered_at=(
            w.last_triggered_at.isoformat() if w.last_triggered_at else None
        ),
        webhook_url=webhook_url,
        integration_webhook_url=integration_webhook_url,
        created_at=w.created_at.isoformat(),
        updated_at=w.updated_at.isoformat(),
    )


async def _parent_shown(db: AsyncSession, run: AssignmentRun, user: User) -> bool:
    """Whether *user* may be told *run*'s parent (``openable_parent_runs``)."""
    if user.is_admin or not run.parent_run_id:
        return True
    openable = await openable_parent_runs(db, {run.parent_run_id}, user.id, is_admin=False)
    return parent_is_shown(run, openable, is_admin=False)


def _run_to_response(
    r: AssignmentRun,
    *,
    child_run_count: int = 0,
    user: User | None = None,
    parent_shown: bool = True,
) -> AssignmentRunResponse:
    """*parent_shown*: whether the reader may open the run's parent
    (:func:`_parent_shown`)."""
    return AssignmentRunResponse(
        id=str(r.id),
        assignment_id=str(r.assignment_id) if r.assignment_id else None,
        agent_id=str(r.agent_id),
        status=r.status,
        is_company_managed=r.is_company_managed,
        notification_sent=r.notification_sent,
        parent_run_id=str(r.parent_run_id) if r.parent_run_id and parent_shown else None,
        child_run_count=child_run_count,
        trigger_payload=without_hidden_parent(r.trigger_payload, parent_shown),
        trigger_context=r.trigger_context,
        actions_taken=r.actions_taken,
        result_summary=r.result_summary,
        error=r.error,
        pending_approvals=_visible_pending_approvals(r, user),
        approval_answered=bool(r.approval_decisions),
        failure_cause=r.failure_cause,
        failure_determination=r.failure_determination,
        stop_reason=r.stop_reason,
        integration_auth_failures=getattr(r, "integration_auth_failures", None),
        human_input_question=r.human_input_question,
        human_input_response=r.human_input_response,
        human_input_history=r.human_input_history,
        langfuse_trace_id=r.langfuse_trace_id,
        llm_trace_id=str(r.llm_trace_id) if r.llm_trace_id else None,
        total_input_tokens=r.total_input_tokens,
        total_output_tokens=r.total_output_tokens,
        started_at=r.started_at.isoformat() if r.started_at else None,
        completed_at=r.completed_at.isoformat() if r.completed_at else None,
        created_at=r.created_at.isoformat(),
    )
