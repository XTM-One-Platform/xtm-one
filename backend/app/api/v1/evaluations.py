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

"""Evaluation API routes – CRUD, trigger, chart, file upload."""

import asyncio
import hashlib
import hmac
import io
import json
import logging
import time
import uuid
import zipfile
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin, require_agentic_ai, require_ai_enabled
from app.api.v1.datasets import (
    MAX_JSON_MEMBER_SIZE,
    MAX_UNCOMPRESSED_SIZE,
    MAX_ZIP_SIZE,
    export_dataset,
    import_dataset_package,
)
from app.api.v1.evaluation_extractors import (
    apply_agent_extractors,
    load_agent_extractors,
    load_evaluation_agents,
    validate_agent_extractors,
    validate_existing_extractors_against_family,
)
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    apply_group_share_fields,
    apply_user_share_fields,
    can_manage,
    get_user_group_ids,
    handle_company_managed_toggle,
    hidden_rows_excluded,
    list_scope_admin_bypass,
    parse_path_id,
    parse_uuid,
    require_admin_for_company_managed,
    user_visible_filter,
    user_visible_query,
    validation_detail,
    wants_access_restriction,
)
from app.api.v1.schemas.evaluation import (
    EVALUATION_EXECUTION_MODES,
    EVALUATION_RUN_MODES,
    AddAgentsRequest,
    AgentTagsResponse,
    AlwaysAskToolsResponse,
    BaselineResponse,
    BundleReviewResponse,
    BundleReviewsRequest,
    CreateEvaluationRequest,
    EvaluationChartResponse,
    EvaluationComparisonResponse,
    EvaluationDetailResponse,
    EvaluationFamilyConflict,
    EvaluationPackage,
    EvaluationResponse,
    EvaluationRunCountResponse,
    EvaluationRunDetailResponse,
    EvaluationRunResponse,
    ExtractorSampleResultResponse,
    FamilyInfoResponse,
    ImportedAgentSettings,
    MetricCatalogEntry,
    MetricResponse,
    PackagedDependencies,
    PackagedResult,
    PackagedRunResults,
    PatchRunTagsRequest,
    PlaceboComparisonResponse,
    PlaceboEstimateRequest,
    PlaceboEstimateResponse,
    PreviewExtractorForFamilyRequest,
    PreviewExtractorRequest,
    PreviewExtractorResponse,
    PreviewExtractorSample,
    PromptGuardStateResponse,
    ReplayJudgeRequest,
    ReplayRunRequest,
    RescoreFromRunsRequest,
    RescoreFromRunsResponse,
    RescoreFromRunsResult,
    RescoreRequest,
    RescoreResponse,
    ResumeEvaluationEpisodeRequest,
    ResumeEvaluationEpisodeResponse,
    RunDiffResponse,
    RunMetricsAggregateResponse,
    RunParamsResponse,
    RunSearchRequest,
    RunSearchResponse,
    RunSearchResultItem,
    RunTagsResponse,
    SetAgentTagsRequest,
    SetBaselineRequest,
    TriggerEvaluationFromRunsRequest,
    TriggerEvaluationRequest,
    UpdateEvaluationRequest,
)
from app.api.v1.schemas.settings import AvailableModelItem
from app.core.archive_reading import ArchiveBudget, ArchiveTooLargeError
from app.core.database import get_db
from app.core.security import UnusableSecretError, timing_safe_str_eq
from app.core.utils import build_content_disposition, fit_copy_name, format_exception_message, safe_uuid
from app.llm.provider_access import ProviderRouting
from app.models.agent import Agent
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.evaluation import (
    SUBJECT_AGENT,
    SUBJECT_FLOW,
    Evaluation,
    EvaluationAgent,
    EvaluationEpisode,
    EvaluationPlaceboComparison,
    EvaluationResult,
    EvaluationResultTrace,
    EvaluationRun,
)
from app.models.evaluation_metric import EvaluationMetric
from app.models.flow import Flow
from app.models.user import User
from app.services.audit import log_audit
from app.services.evaluation.aggregation import counts_in_headlines
from app.services.evaluation.bundle_review import BundleReviewRefusedError, load_bundle_review, load_bundle_reviews
from app.services.evaluation.cross_comparison import ComparisonRefusedError, compare_evaluations
from app.services.evaluation.dataset_family_compatibility import (
    check_family_compatibility,
)
from app.services.evaluation.extraction import (
    apply_extractor,
    count_extraction_failures,
    current_limits,
    resolve_source,
    supported_output_kinds,
    validate_extractor_chain,
)
from app.services.evaluation.extraction import item_count as extraction_item_count
from app.services.evaluation.extractor_repository import as_extractor_dict
from app.services.evaluation.framing import ANOMALY_KIND_PLATFORM_ERROR
from app.services.evaluation.launch import (
    LaunchPlan,
    LaunchRefusedError,
    active_model_ids,
    add_launch_runs,
    check_dataset_families,
    check_family,
    check_launchable,
    check_models_served,
    check_runs_served,
    family_gate_error,
    plan_launch,
    subjects_hidden_from,
)
from app.services.evaluation.metric_package import (
    is_macro_key,
    is_pooled_key,
    list_families,
    metric_catalog,
    metric_entry_view,
    metric_unit,
    normalize_field_scoring_overrides,
    validate_field_scoring_overrides,
)
from app.services.evaluation.metrics import log_result_metrics
from app.services.evaluation.resume_secret import (
    RESUME_SECRET_KEY,
    resume_secret,
    served_trigger_config,
    stored_trigger_config,
    without_resume_secret,
)
from app.services.evaluation.run_mode import (
    RunMode,
    load_run_detail_mode,
    load_run_modes,
)
from app.services.evaluation.scorers.registry import (
    invalid_keys,
    judge_enabled,
)
from app.services.evaluation.settings import (
    CLEARED_BY_NULL,
    EVALUATION_SETTINGS,
    evaluation_agent_settings,
    evaluation_settings,
)
from app.services.evaluation.subjects import link_of, subject_id, subject_label
from app.services.evaluation.tags import (
    merge_effective_tags,
    strip_reserved_keys,
)
from app.services.evaluation_continuation import (
    TERMINAL_RUN_STATUSES,
    CallbackOutcome,
    ConversationContinuationService,
    is_callback_pending_for_run,
)
from app.services.evaluation_placebo import (
    imported_placebo_config,
    parse_placebo_config,
    placebo_mode_error,
)
from app.services.file_storage import objects_deleted_on_failure

logger = logging.getLogger(__name__)
router = APIRouter()


_TRACE_PREVIEW_LIMIT = 30_000
_TRACE_TRUNCATION_MARKER = "\n\n... [TRUNCATED IN TRACE] ..."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_model_config(
    model_ids: list[str] | None, disabled_model_ids: list[str] | None
) -> tuple[list[str], list[str]]:
    """Return deduplicated model IDs and a disabled subset preserving input order."""
    configured = list(dict.fromkeys(model_ids or []))
    disabled = [
        mid
        for mid in dict.fromkeys(disabled_model_ids or [])
        if mid in set(configured)
    ]
    return configured, disabled


def _reject_f1_quality_source(quality_source: str | None) -> None:
    """Reject ``quality_source == "f1"`` on write.

    Token-F1 is retired as a quality source: stored rows that carry it stay
    readable, but no new evaluation may select it.
    """
    if quality_source == "f1":
        raise HTTPException(
            status_code=422,
            detail=(
                "Token-F1 has been retired as a quality source. Use the LLM Judge "
                "(quality_source='judge') and select an evaluation family for "
                "deterministic package metrics."
            ),
        )


def _validate_family(family: str | None) -> None:
    """Reject an unknown family.

    A ``None`` family is allowed here; a family is required at trigger time. The
    rule is ``launch.family_gate_error``, shared with every launch.
    """
    error = family_gate_error(family)
    if error is not None:
        raise HTTPException(status_code=400, detail=error)


def _http_refusal(refused: LaunchRefusedError) -> HTTPException:
    """A launch refusal as the HTTP error a request gets."""
    return HTTPException(status_code=refused.status_code, detail=refused.detail)


async def _require_dataset_families_set(db: AsyncSession, evaluation) -> None:
    """Fail-closed run-time gate: every linked dataset must declare a family.

    Beside :func:`_require_runnable_family` at the rescore sites; a launch that
    runs an agent calls :func:`_require_launchable`, which holds both.
    """
    try:
        await check_dataset_families(db, evaluation)
    except LaunchRefusedError as refused:
        raise _http_refusal(refused) from None


async def _evaluation_family_conflicts(
    db: AsyncSession, evaluation
) -> list[EvaluationFamilyConflict]:
    """Re-check every dataset linked to ``evaluation`` against its family.

    The mirror of ``datasets._dataset_family_conflicts`` for the other side of the
    link: run when the evaluation's own family changes, it reports conflicts and
    never fixes them.
    """
    rows = (
        await db.execute(
            select(Dataset.id, Dataset.name, Dataset.family)
            .join(EvaluationDataset, EvaluationDataset.dataset_id == Dataset.id)
            .where(EvaluationDataset.evaluation_id == evaluation.id)
        )
    ).all()
    conflicts: list[EvaluationFamilyConflict] = []
    for dataset_id, dataset_name, dataset_family in rows:
        message = check_family_compatibility(dataset_family, evaluation.family)
        if message is not None:
            conflicts.append(
                EvaluationFamilyConflict(
                    dataset_id=str(dataset_id),
                    dataset_name=dataset_name,
                    message=message,
                )
            )
    return conflicts


def _require_runnable_family(evaluation) -> None:
    """Fail-closed run-time family gate, ``launch.check_family`` as an HTTP error."""
    try:
        check_family(evaluation)
    except LaunchRefusedError as refused:
        raise _http_refusal(refused) from None


async def _refuse_unserved_models(db: AsyncSession, evaluation, model_ids: list[str]) -> None:
    """Refuse a model no provider of the owner offers on save, as a launch refuses it.

    Given only the models a request adds: a saved one is never re-checked, so
    an evaluation whose provider went away stays editable, and its launch
    refuses the model.
    """
    try:
        await check_models_served(db, evaluation, model_ids)
    except LaunchRefusedError as refused:
        raise _http_refusal(refused) from None


def _unavailable_model_warnings(response: dict) -> list[str]:
    """One warning per model a copy or an import keeps that its new owner's providers do not offer.

    Kept, as a saved model whose provider went away is: it runs once one of
    them offers it. Read from the response's ``unavailable_model_ids``.
    """
    return [
        f"Model {mid} is kept but cannot run: no provider you can use offers it"
        for mid in response["unavailable_model_ids"]
    ]


#: Why an evaluation may not take an agent or flow its owner cannot see.
SUBJECT_NOT_VISIBLE_TO_OWNER = (
    "An evaluation runs only its owner's own agents and flows, those shared with them "
    "and company-managed ones: its owner cannot see one of these."
)


async def _refuse_hidden_subjects(
    db: AsyncSession,
    owner_id: uuid.UUID | None,
    *,
    agent_ids: Iterable[uuid.UUID] = (),
    flow_ids: Iterable[uuid.UUID] = (),
) -> None:
    """422 when an evaluation owned by *owner_id* would link an agent or flow
    outside its owner's three tiers. An ownerless evaluation is not checked."""
    if owner_id is not None and await subjects_hidden_from(db, owner_id, agent_ids=agent_ids, flow_ids=flow_ids):
        raise HTTPException(status_code=422, detail=SUBJECT_NOT_VISIBLE_TO_OWNER)


async def _require_launchable(db: AsyncSession, evaluation) -> None:
    """The checks of every launch that runs an agent: family, callback secret, datasets."""
    try:
        await check_launchable(db, evaluation)
    except LaunchRefusedError as refused:
        raise _http_refusal(refused) from None


def _truncate_trace_text(text: Any, limit: int = _TRACE_PREVIEW_LIMIT) -> Any:
    if not isinstance(text, str):
        return text
    if len(text) <= limit:
        return text
    return text[:limit] + _TRACE_TRUNCATION_MARKER


def _truncate_execution_details_for_response(execution_details: Any) -> Any:
    """Return a UI-safe copy of execution details with large trace payloads trimmed."""
    if not isinstance(execution_details, dict):
        return execution_details

    details = deepcopy(execution_details)
    traces = details.get("traces")
    if not isinstance(traces, list):
        return details

    payload_keys = (
        "tool_input",
        "tool_output",
        "system_prompt",
        "user_prompt",
        "raw_response",
    )
    for trace in traces:
        if not isinstance(trace, dict):
            continue
        for key in payload_keys:
            if key in trace:
                trace[key] = _truncate_trace_text(trace.get(key))

    return details


async def _get_user_evaluation(
    db: AsyncSession,
    user_id: uuid.UUID,
    evaluation_id: str,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
) -> Evaluation:
    """Fetch an evaluation visible to the user, or raise 404."""
    eid = parse_path_id(evaluation_id, not_found_detail="Evaluation not found")
    query = user_visible_query(
        Evaluation, user_id, user_group_ids, is_admin=is_admin
    ).where(Evaluation.id == eid)
    result = await db.execute(query)
    evaluation = cast(Evaluation | None, result.scalar_one_or_none())
    if not evaluation:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    return evaluation


def _stored_trigger_config(incoming: dict | None, stored: dict | None = None) -> dict:
    """:func:`stored_trigger_config`, an unusable resume secret refused with a 422."""
    try:
        return stored_trigger_config(incoming, stored)
    except UnusableSecretError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid resume secret: {exc}") from None


def _trigger_config_after(incoming: dict | None, stored: dict | None = None) -> dict:
    """The trigger config a write stores: *incoming* merged into *stored*.

    A key the write leaves out is kept, so a save from the page, which sends its
    schedule and on-agent-change only, keeps the callback settings set through
    the API; a key sent as ``null`` is removed. ``resume_secret: null`` reaches
    :func:`stored_trigger_config`, which removes the secret, and an empty
    ``{}`` changes nothing.
    """
    merged = {**without_resume_secret(stored), **(incoming or {})}
    return _stored_trigger_config(
        {key: value for key, value in merged.items() if value is not None or key == RESUME_SECRET_KEY},
        stored,
    )


async def _build_evaluation_response(
    db: AsyncSession,
    evaluation: Evaluation,
    *,
    caller_can_manage: bool,
    owner_routing: ProviderRouting | None = None,
) -> dict:
    """Build a full response dict for an evaluation.

    *caller_can_manage* is :func:`~app.api.v1.helpers.can_manage` for the caller,
    served as ``can_manage``. *owner_routing* is the evaluation owner's
    providers, when the caller already read them (a list serves several
    evaluations of one owner).
    """
    from app.services.evaluation_engine import unserved_model_ids

    # Agents, with their evaluation-scoped tags and baseline flag.
    agent_rows = await db.execute(
        select(
            Agent.id,
            Agent.name,
            Agent.slug,
            Agent.icon,
            EvaluationAgent.tags,
            EvaluationAgent.is_baseline,
        )
        .join(EvaluationAgent, EvaluationAgent.agent_id == Agent.id)
        .where(EvaluationAgent.evaluation_id == evaluation.id)
        .order_by(EvaluationAgent.created_at)
    )
    agents = [
        {
            "id": str(r.id),
            "name": r.name,
            "slug": r.slug,
            "icon": r.icon,
            "tags": r.tags or None,
            "is_baseline": bool(r.is_baseline),
        }
        for r in agent_rows
    ]
    # Flow subjects (#8257), beside the agents.
    flow_rows = await db.execute(
        select(Flow.id, Flow.name, EvaluationAgent.tags, EvaluationAgent.is_baseline)
        .join(EvaluationAgent, EvaluationAgent.flow_id == Flow.id)
        .where(EvaluationAgent.evaluation_id == evaluation.id)
        .order_by(EvaluationAgent.created_at)
    )
    flows = [
        {"id": str(r.id), "name": r.name, "tags": r.tags or None, "is_baseline": bool(r.is_baseline)}
        for r in flow_rows
    ]

    dataset_count_result = await db.execute(
        select(func.count()).where(
            EvaluationDataset.evaluation_id == evaluation.id
        )
    )
    dataset_count = dataset_count_result.scalar() or 0

    entry_count_result = await db.execute(
        select(func.count(DatasetEntry.id))
        .join(EvaluationDataset, EvaluationDataset.dataset_id == DatasetEntry.dataset_id)
        .where(EvaluationDataset.evaluation_id == evaluation.id)
    )
    entry_count = entry_count_result.scalar() or 0

    active_runs_result = await db.execute(
        select(func.count(EvaluationRun.id)).where(
            EvaluationRun.evaluation_id == evaluation.id,
            EvaluationRun.status.in_(["pending", "running"]),
        )
    )
    has_active_runs = (active_runs_result.scalar() or 0) > 0

    return {
        "id": str(evaluation.id),
        "user_id": str(evaluation.user_id) if evaluation.user_id else None,
        "name": evaluation.name,
        "description": evaluation.description,
        "family": evaluation.family,
        "trigger_config": served_trigger_config(evaluation.trigger_config),
        "use_agent_default_model": evaluation.use_agent_default_model,
        "model_ids": evaluation.model_ids or [],
        "disabled_model_ids": evaluation.disabled_model_ids or [],
        # Read now, never stored: a provider added or removed changes it.
        "unavailable_model_ids": await unserved_model_ids(
            db, evaluation, evaluation.model_ids or [], routing=owner_routing
        ),
        "execution_mode": evaluation.execution_mode,
        "apply_prompt_guards": bool(evaluation.apply_prompt_guards),
        "require_tool_approval": bool(evaluation.require_tool_approval),
        "run_mode": evaluation.run_mode,
        "evaluator_model": evaluation.evaluator_model,
        "evaluator_prompt": evaluation.evaluator_prompt,
        "evaluation_instructions": evaluation.evaluation_instructions,
        "performance_tolerance": evaluation.performance_tolerance,
        "sub_score_weights": evaluation.sub_score_weights,
        "quality_source": evaluation.quality_source,
        "penalty_factor": evaluation.penalty_factor,
        "timeout_seconds": evaluation.timeout_seconds,
        "judge_max_tokens": evaluation.judge_max_tokens,
        "enabled": evaluation.enabled,
        "is_company_managed": evaluation.is_company_managed,
        "can_manage": caller_can_manage,
        "shared_group_ids": evaluation.shared_group_ids,
        "shared_group_permissions": evaluation.shared_group_permissions,
        "shared_user_ids": evaluation.shared_user_ids,
        "shared_user_permissions": evaluation.shared_user_permissions,
        "access_restricted": evaluation.access_restricted,
        "custom_params": evaluation.custom_params,
        "field_scoring_overrides": evaluation.field_scoring_overrides,
        "scorer_selection": evaluation.scorer_selection,
        "reuse_default": evaluation.reuse_default,
        "overall_quality_metrics": evaluation.overall_quality_metrics,
        "overall_performance_metrics": evaluation.overall_performance_metrics,
        "placebo_config": evaluation.placebo_config,
        "tags": evaluation.tags or None,
        "agent_ids": [a["id"] for a in agents],
        "agents": agents,
        "flows": flows,
        "agent_extractors": await load_agent_extractors(db, evaluation.id) or None,
        "entry_count": entry_count,
        "dataset_count": dataset_count,
        "has_active_runs": has_active_runs,
        "last_triggered_at": (
            evaluation.last_triggered_at.isoformat()
            if evaluation.last_triggered_at
            else None
        ),
        "created_at": evaluation.created_at.isoformat(),
        "updated_at": evaluation.updated_at.isoformat(),
    }

async def _get_avg_sub_scores(
    db: AsyncSession, run: EvaluationRun
) -> tuple[dict, int, int, int]:
    """Compute average sub-scores and count reused, judge-failure and platform-error
    results.

    Returns ``(avg_sub_scores, reused_entries, judge_failure_count,
    platform_error_count)`` from a single query, so the per-run reuse indicator,
    the judge-failure indicator and the entries the platform could not
    run, left out of every headline, cost no extra DB round-trip. The averages
    count the entries the judge score counts, and a run scored with the judge off has
    none.
    """
    # The run's own snapshot, never the evaluation's current selection: the run was
    # scored with that judge on or off.
    judge_on = judge_enabled(getattr(run, "scorer_selection", None))
    results = await db.execute(
        select(
            EvaluationResult.sub_scores,
            EvaluationResult.is_reused,
            EvaluationResult.is_judge_failure,
            EvaluationResult.execution_anomaly_kind,
        ).where(EvaluationResult.run_id == run.id)
    )
    reused_entries = 0
    judge_failure_count = 0
    platform_error_count = 0
    all_scores: list = []
    for row in results:
        if row.is_reused:
            reused_entries += 1
        if row.is_judge_failure:
            judge_failure_count += 1
        if row.execution_anomaly_kind == ANOMALY_KIND_PLATFORM_ERROR:
            platform_error_count += 1
        if (
            judge_on
            and row.sub_scores
            and counts_in_headlines(row)
        ):
            all_scores.append(row.sub_scores)
    if not all_scores:
        return {}, reused_entries, judge_failure_count, platform_error_count
    keys = {k for s in all_scores if isinstance(s, dict) for k in s}
    avg: dict[str, float] = {}
    for k in keys:
        vals = [s.get(k, 0) for s in all_scores if isinstance(s, dict)]
        avg[k] = round(sum(vals) / len(vals), 1) if vals else 0
    return avg, reused_entries, judge_failure_count, platform_error_count


async def _platform_error_counts(
    db: AsyncSession, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Per run, the entries the platform could not run, in one query."""
    if not run_ids:
        return {}
    rows = await db.execute(
        select(EvaluationResult.run_id, func.count())
        .where(
            EvaluationResult.run_id.in_(run_ids),
            EvaluationResult.execution_anomaly_kind == ANOMALY_KIND_PLATFORM_ERROR,
        )
        .group_by(EvaluationResult.run_id)
    )
    return {run_id: count for run_id, count in rows.all()}


async def _held_reasons_for(db: AsyncSession, runs: list[EvaluationRun]) -> dict:
    """``{evaluation_id: refusal code}`` for the held evaluations among *runs*'
    PENDING runs, in ONE query - none when no run is pending."""
    pending = {r.evaluation_id for r in runs if r.status == "pending" and r.evaluation_id}
    if not pending:
        return {}
    from app.services.ai_hold import held_evaluations

    return await held_evaluations(db, pending)
def _headline_detail(run: EvaluationRun, key: str, empty=None):
    """Read one persisted headline provenance field.

    Provenance is stored, never re-derived at read time: recomputing it here
    could report a composition the persisted value was not built from, which is
    worse than reporting none.
    """
    return (run.headline_details or {}).get(key) or (
        [] if empty is None else empty
    )


#: Why a placebo comparison's run is never re-run alone: the 422 of the
#: replay and trigger-from-runs routes, and a comparison run's ``replay_block_reason``.
COMPARISON_RUN_REFUSAL = "A placebo comparison's run cannot be re-run on its own: launch a new placebo comparison"
#: Nor re-scored alone: its comparison's statistics stay on the scores it settled with.
COMPARISON_RUN_RESCORE_REFUSAL = (
    "A placebo comparison's run cannot be re-scored on its own: launch a new placebo comparison"
)


async def _build_run_response(
    db: AsyncSession,
    run: EvaluationRun,
    *,
    missing_snapshots: list[str] | None = None,
    held_reasons: dict | None = None,
    run_modes: dict[uuid.UUID, RunMode] | None = None,
) -> dict:
    """Build a response dict for an evaluation run.

    *run_modes* is :func:`load_run_modes`, computed by the caller for the
    whole page. ``None`` reads the run's own column alone, which is
    exact for a run that has not started and for one recorded with its mode:
    the trigger, replay and cancel routes. The runs list and the run
    page, which serve older runs, pass it.

    *held_reasons* is :func:`_held_reasons_for`, computed by the caller for
    the whole page in one query; ``None`` reports no held state (a
    run this request just created or ended cannot be held yet).

    A given *missing_snapshots* is a pre-computed replayability result and
    skips the per-run :func:`resolve_run_snapshot_overrides` call. The runs
    list precomputes the page with :func:`batch_compute_run_replayability`,
    which keeps it at O(1) DB calls per snapshot type instead of
    O(runs × snapshot_refs).
    """
    from app.services.evaluation_engine import resolve_run_snapshot_overrides

    # The agent's, or a flow subject's (#8257).
    subject = await subject_label(db, run)

    eval_result = await db.execute(
        select(
            Evaluation.quality_source,
            Evaluation.name,
            Evaluation.tags,
        ).where(Evaluation.id == run.evaluation_id)
    )
    eval_row = eval_result.one_or_none()
    quality_source = eval_row.quality_source if eval_row else "judge"
    evaluation_name = eval_row.name if eval_row else ""
    evaluation_tags = (eval_row.tags if eval_row else None) or None

    # Effective tags merge evaluation ⊕ agent ⊕ run tags at read time and are
    # never persisted.
    agent_meta_result = await db.execute(
        select(EvaluationAgent.tags, EvaluationAgent.is_baseline).where(
            EvaluationAgent.evaluation_id == run.evaluation_id,
            link_of(run),
        )
    )
    agent_meta_row = agent_meta_result.one_or_none()
    agent_tags = (agent_meta_row.tags if agent_meta_row else None) or None
    is_baseline = bool(agent_meta_row.is_baseline) if agent_meta_row else False
    effective_tags = merge_effective_tags(evaluation_tags, agent_tags, run.tags)

    if missing_snapshots is None:
        _, missing_snapshots = await resolve_run_snapshot_overrides(db, run)
    # A placebo comparison's run is never re-run alone: the replay and
    # trigger-from-runs routes refuse it, and the response says so up front.
    in_comparison = run.placebo_comparison_id is not None
    can_replay = len(missing_snapshots) == 0 and not in_comparison
    held_reason = (
        (held_reasons or {}).get(run.evaluation_id) if run.status == "pending" else None
    )
    replay_block_reason = (
        None
        if can_replay
        else COMPARISON_RUN_REFUSAL
        if in_comparison
        else (
            "Replay impossible: missing version snapshots ("
            + ", ".join(missing_snapshots)
            + ")"
        )
    )

    # Dataset drift: the dataset was edited since this run pinned its version.
    # A run with no pinned versions never drifts.
    pinned_versions = run.pinned_dataset_versions or None
    has_dataset_drift = False
    if pinned_versions:
        pinned_ids = [parse_uuid(did, label="Dataset ID") for did in pinned_versions]
        current_rows = await db.execute(
            select(Dataset.id, Dataset.current_version).where(Dataset.id.in_(pinned_ids))
        )
        for did, current_version in current_rows.all():
            if (current_version or 0) > pinned_versions.get(str(did), 0):
                has_dataset_drift = True
                break

    (
        avg_sub_scores,
        reused_entries,
        judge_failure_count,
        platform_error_count,
    ) = await _get_avg_sub_scores(db, run)
    run_mode = run_modes.get(run.id) if run_modes else None
    if run_mode is None:
        run_mode = RunMode(run.execution_mode, 0)

    return {
        "id": str(run.id),
        "evaluation_id": str(run.evaluation_id),
        "evaluation_name": evaluation_name,
        "agent_id": str(run.agent_id) if run.agent_id else None,
        "flow_id": str(run.flow_id) if run.flow_id else None,
        "subject_kind": SUBJECT_FLOW if run.flow_id is not None else SUBJECT_AGENT,
        "agent_name": subject.name if subject else "",
        "agent_slug": subject.slug if subject else "",
        "agent_user_name": subject.owner_name if subject else None,
        "model_id": run.model_id,
        "is_default_model": run.is_default_model,
        "status": run.status,
        "trigger_reason": run.trigger_reason,
        "score": run.score,
        "speed_score": run.speed_score,
        "overall_score": run.overall_score,
        "f1_score": run.f1_score,
        # Headline metrics, passed through as persisted: `None` stays
        # `None` so the UI renders "N/A", never a measured zero.
        "quality_score": run.quality_score,
        "quality_metric_keys": _headline_detail(run, "quality_metric_keys"),
        "performance_score": run.performance_score,
        "performance_metric_keys": _headline_detail(run, "performance_metric_keys"),
        "metric_entry_counts": _headline_detail(run, "metric_entry_counts", {}),
        # What each contributing metric put into the mean. The Metrics panel
        # shows raw means in native units, which reproduce neither headline
        # (quality is penalised, performance is normalised across the
        # evaluation), so without these the two readings silently disagree.
        "quality_contributions": _headline_detail(run, "quality_contributions", {}),
        "quality_coverage": (run.headline_details or {}).get("quality_coverage"),
        "performance_contributions": _headline_detail(
            run, "performance_contributions", {}
        ),
        # Both halves of the exclusion record are served, so the run
        # page can tell a headline averaged over five metrics from one averaged
        # over five of seven.
        "quality_aggregation_excluded": _headline_detail(run, "excluded"),
        "performance_aggregation_excluded": _headline_detail(
            run, "performance_excluded"
        ),
        "quality_source": quality_source,
        "duration_seconds": run.duration_seconds,
        "avg_sub_scores": avg_sub_scores,
        "completed_entries": run.completed_entries,
        "total_entries": run.total_entries,
        "error": run.error,
        "rescore_error": run.rescore_error,
        "held_reason": held_reason,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "created_at": run.created_at.isoformat(),
        "persona_version_number": run.persona_version_number,
        "prompt_versions": run.prompt_versions or None,
        "agent_config_versions": run.agent_config_versions or None,
        "skill_versions": run.skill_versions or None,
        # Score lineage. Null on runs scored before the column existed;
        # the UI renders that as unknown, never as fine.
        "metric_suite_version": run.metric_suite_version,
        # Judge provenance. Null on runs predating the column, which
        # reads as judge-on. Served so an N/A judge headline is explicable: it
        # tells "the judge did not run" from "the judge ran and failed".
        "scorer_selection": run.scorer_selection,
        "can_replay": can_replay,
        "replay_block_reason": replay_block_reason,
        "pinned_dataset_versions": pinned_versions,
        "tags": run.tags or None,
        "effective_tags": effective_tags,
        "is_baseline": is_baseline,
        "has_dataset_drift": has_dataset_drift,
        "anomaly_count": run.anomaly_count or 0,
        "execution_mode": run_mode.execution_mode,
        "apply_prompt_guards": run.apply_prompt_guards,
        "require_tool_approval": run.require_tool_approval,
        "fallback_entries": run_mode.fallback_entries,
        "reused_entries": reused_entries,
        "judge_failure_count": judge_failure_count,
        "platform_error_count": platform_error_count,
        "arm": run.arm or "real",
        "trial_index": run.trial_index or 0,
        "placebo_target": run.placebo_target,
        "placebo_comparison_id": str(run.placebo_comparison_id) if run.placebo_comparison_id else None,
    }


def _parse_optional_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string (best effort)."""
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _exported_settings(evaluation: Evaluation) -> dict:
    """Every setting an export writes: the resume secret is never exported."""
    settings = evaluation_settings(evaluation)
    settings.update(
        trigger_config=without_resume_secret(evaluation.trigger_config),
        model_ids=evaluation.model_ids or [],
        disabled_model_ids=evaluation.disabled_model_ids or [],
        execution_mode=_runnable_execution_mode(evaluation.execution_mode),
        run_mode=_runnable_run_mode(evaluation.run_mode),
        apply_prompt_guards=bool(evaluation.apply_prompt_guards),
        require_tool_approval=bool(evaluation.require_tool_approval),
    )
    return settings


async def _build_evaluation_export_bundle(
    db: AsyncSession,
    evaluation: Evaluation,
    user: User,
    request: Request,
) -> dict:
    """Build an evaluation definition + dependency payloads for ZIP export."""
    # Embedded agents use the agent export/import format.
    from app.api.v1.agents import export_agent
    from app.services.agentschema.detect import NATIVE_FORMAT

    # 1) Agents linked to this evaluation (ordered by insertion time), with
    # their settings in it.
    links = list(
        (
            await db.execute(
                select(EvaluationAgent)
                .where(EvaluationAgent.evaluation_id == evaluation.id)
                .order_by(EvaluationAgent.created_at)
            )
        ).scalars()
    )
    if any(link.flow_id is not None for link in links):
        # A package carries agents, not flows (#8257).
        raise HTTPException(status_code=422, detail="An evaluation with a flow subject cannot be exported.")
    linked_agent_ids = [link.agent_id for link in links]

    agents_bundle: list[dict] = []
    agent_index_by_id: dict[str, int] = {}
    for idx, agent_id in enumerate(linked_agent_ids):
        # Called as the route is: ``request`` for its audit, ``format`` spelled
        # out, since a direct call would get the ``Query`` default object.
        exported_response = await export_agent(
            agent_id=str(agent_id),
            request=request,
            format=NATIVE_FORMAT,
            user=user,
            db=db,
        )
        agent_bundle = json.loads(bytes(exported_response.body).decode("utf-8"))
        agents_bundle.append(agent_bundle)
        agent_index_by_id[str(agent_id)] = idx

    # 2) Datasets linked to this evaluation.
    datasets_result = await db.execute(
        select(EvaluationDataset, Dataset)
        .join(Dataset, Dataset.id == EvaluationDataset.dataset_id)
        .where(EvaluationDataset.evaluation_id == evaluation.id)
        .order_by(EvaluationDataset.position)
    )
    dataset_rows = datasets_result.all()

    datasets_bundle: list[dict] = []
    entry_ref_by_id: dict[str, dict[str, int]] = {}
    dataset_ids_by_index: dict[int, str] = {}

    for dataset_idx, (link, dataset) in enumerate(dataset_rows):
        dataset_ids_by_index[dataset_idx] = str(dataset.id)

        entries_result = await db.execute(
            select(DatasetEntry)
            .where(DatasetEntry.dataset_id == dataset.id)
            .order_by(DatasetEntry.position)
        )
        entries = entries_result.scalars().all()

        for entry in entries:
            entry_ref_by_id[str(entry.id)] = {
                "dataset_index": dataset_idx,
                "entry_position": entry.position,
            }

        datasets_bundle.append(
            {
                "index": dataset_idx,
                "position": link.position,
                "path": f"dependencies/datasets/dataset-{dataset_idx}.zip",
            }
        )

    # 3) Runs + results for this evaluation. A placebo comparison's runs stay
    # out: the bundle carries no arm, so an import would turn its
    # control runs into the agent's own.
    runs_result = await db.execute(
        select(EvaluationRun)
        .where(EvaluationRun.evaluation_id == evaluation.id, EvaluationRun.placebo_comparison_id.is_(None))
        .order_by(EvaluationRun.created_at)
    )
    runs = runs_result.scalars().all()

    runs_definition: list[dict] = []
    run_result_files: list[dict] = []
    for run_index, run in enumerate(runs):
        results_result = await db.execute(
            select(EvaluationResult)
            .where(EvaluationResult.run_id == run.id)
            .order_by(EvaluationResult.created_at)
        )
        results = results_result.scalars().all()

        trace_rows = []
        if results:
            trace_result = await db.execute(
                select(EvaluationResultTrace)
                .where(
                    EvaluationResultTrace.result_id.in_([r.id for r in results])
                )
                .order_by(
                    EvaluationResultTrace.result_id.asc(),
                    EvaluationResultTrace.trace_order.asc(),
                )
            )
            trace_rows = trace_result.scalars().all()

        trace_ids_by_result: dict[str, list[str]] = {}
        for link in trace_rows:
            trace_ids_by_result.setdefault(str(link.result_id), []).append(
                str(link.trace_id)
            )

        result_items = []
        for result in results:
            result_items.append(
                {
                    # ``None`` for a result whose entry was deleted or is no longer
                    # in a linked dataset: it still counts toward its run's
                    # headlines, so it is exported, and imported without an entry.
                    "entry_ref": entry_ref_by_id.get(str(result.entry_id)),
                    "agent_output": result.agent_output,
                    "score": result.score,
                    "sub_scores": result.sub_scores or {},
                    "evaluator_reasoning": result.evaluator_reasoning,
                    "duration_seconds": result.duration_seconds,
                    "speed_score": result.speed_score,
                    "overall_score": result.overall_score,
                    "f1_score": result.f1_score,
                    # The result's classification, without which the package
                    # cannot be scored the way the original was:
                    # ``_compute_run_headlines`` excludes judge failures and
                    # execution anomalies, and without the flags the first
                    # recompute after an import averages them in.
                    #
                    # No version bump, unlike the dataset export: this
                    # import pins ``version`` exactly (``!= 1``), so a bump would
                    # make every deployed server reject these packages. The keys
                    # are additive both ways (an older server ignores them, a
                    # newer one defaults them).
                    "is_judge_failure": result.is_judge_failure,
                    "execution_anomaly_kind": result.execution_anomaly_kind,
                    # Provenance. Without it every imported result
                    # claims a fresh execution. It also decides what Run
                    # Duration measures: ``extract_metrics`` reads the judge
                    # time for a reused result and the total for a fresh one.
                    #
                    # ``reused_from_result_id`` is deliberately not carried.
                    # Results here are addressed by position (the dataset entry
                    # they answer within their run), so the stored UUID would
                    # resolve to nothing on the importing instance, and nothing
                    # joins on the column. NULL (the column is nullable,
                    # ``ondelete="SET NULL"``) already states "reused, source
                    # not recorded here".
                    "is_reused": bool(result.is_reused),
                    "trace_ids": trace_ids_by_result.get(str(result.id), []),
                    "execution_details": result.execution_details,
                    "created_at": result.created_at.isoformat(),
                }
            )

        run_results_path = f"evaluation_runs/run-{run_index}.json"
        runs_definition.append(
            {
                "index": run_index,
                "results_path": run_results_path,
                "agent_index": agent_index_by_id.get(str(run.agent_id)),
                "model_id": run.model_id,
                "is_default_model": run.is_default_model,
                "status": run.status,
                "trigger_reason": run.trigger_reason,
                "execution_mode": run.execution_mode,
                "apply_prompt_guards": run.apply_prompt_guards,
                "require_tool_approval": run.require_tool_approval,
                "persona_version_number": run.persona_version_number,
                "prompt_versions": run.prompt_versions,
                "agent_config_versions": run.agent_config_versions,
                "skill_versions": run.skill_versions,
                "metric_suite_version": run.metric_suite_version,
                # What its judge headline is computed from on import.
                "scorer_selection": run.scorer_selection,
                "score": run.score,
                "speed_score": run.speed_score,
                "overall_score": run.overall_score,
                "f1_score": run.f1_score,
                "duration_seconds": run.duration_seconds,
                "completed_entries": run.completed_entries,
                "total_entries": run.total_entries,
                "error": run.error,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "completed_at": run.completed_at.isoformat()
                if run.completed_at
                else None,
                "created_at": run.created_at.isoformat(),
            }
        )
        run_result_files.append(
            {
                "path": run_results_path,
                "payload": {
                    "version": 1,
                    "run_index": run_index,
                    "results": result_items,
                },
            }
        )

    definition = {
        "version": 1,
        "export_type": "evaluation_package",
        "exported_at": datetime.now(UTC).isoformat(),
        "evaluation": {
            "name": evaluation.name,
            # Every setting, the resume secret left out.
            **_exported_settings(evaluation),
            "enabled": evaluation.enabled,
            "last_triggered_at": evaluation.last_triggered_at.isoformat()
            if evaluation.last_triggered_at
            else None,
        },
        "dependencies": {
            "agents": [
                {
                    "index": idx,
                    "path": f"dependencies/agents/agent-{idx}.json",
                    # Its extractor, tags and baseline flag in this evaluation.
                    **evaluation_agent_settings(link),
                }
                for idx, link in enumerate(links)
            ],
            "datasets": datasets_bundle,
        },
        "evaluation_runs": runs_definition,
    }

    return {
        "definition": definition,
        "agents": agents_bundle,
        "dataset_ids_by_index": dataset_ids_by_index,
        "run_result_files": run_result_files,
    }


async def _resolve_imported_evaluation_name(
    db: AsyncSession,
    base_name: str,
) -> str:
    """Ensure imported evaluation name does not collide for this user.

    When a duplicate exists, append a short slug suffix to the title.
    """
    normalized = (base_name or "Imported Evaluation").strip() or "Imported Evaluation"
    # ``limit(1)``: several evaluations can already share the name.
    exists_result = await db.execute(
        select(Evaluation.id).where(
            func.lower(Evaluation.name) == normalized.lower(),
        ).limit(1)
    )
    if exists_result.scalar() is None:
        return normalized

    slug = uuid.uuid4().hex[:8]
    return fit_copy_name(normalized, Evaluation.__table__.c.name, suffix=f"-{slug}")


async def _streaming_response_to_bytes(response: StreamingResponse) -> bytes:
    """Read a StreamingResponse payload into bytes."""
    data = bytearray()
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            data.extend(chunk)
        else:  # pragma: no cover - defensive fallback
            data.extend(str(chunk).encode("utf-8"))
    return bytes(data)


# ---------------------------------------------------------------------------
# Run endpoints (must be before /{evaluation_id} to avoid route conflicts)
# ---------------------------------------------------------------------------


@router.post(
    "/resume",
    response_model=ResumeEvaluationEpisodeResponse,
    # Declared so the 410 appears in the generated schema: 401/404 are
    # conventional, but a 410 meaning "abandoned, NOT a duplicate" is this
    # endpoint's contract, and a caller reading only the schema must see it.
    responses={
        410: {
            "description": (
                "This callback's episode was already closed before the "
                "callback arrived, so the payload was NOT applied. This is not "
                "a duplicate of work already handled — nothing processed it. "
                "The run may or may not still be active: sibling episodes can "
                "keep it running, so check the run before re-running it."
            )
        }
    },
)
async def resume_evaluation_episode(
    body: ResumeEvaluationEpisodeRequest,
    request: Request,
    x_signature: str | None = Header(default=None, alias="X-Signature"),
    db: AsyncSession = Depends(get_db),
):
    """Resume an async evaluation episode from an external callback.

    No AI gate: the episode was already dispatched, so refusing its callback
    prevents no AI work; it would only abandon work in flight, which must be
    allowed to finish, and leave the episode hanging until the deadline sweep.
    """
    episode_result = await db.execute(
        select(EvaluationEpisode).where(
            EvaluationEpisode.correlation_id == body.correlation_id
        )
    )
    episode = episode_result.scalar_one_or_none()
    if not episode:
        raise HTTPException(status_code=404, detail="Evaluation episode not found")

    run_result = await db.execute(
        select(EvaluationRun, Evaluation)
        .join(Evaluation, Evaluation.id == EvaluationRun.evaluation_id)
        .where(EvaluationRun.id == episode.run_id)
    )
    row = run_result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Evaluation run not found")

    _run, evaluation = row
    try:
        secret = resume_secret(evaluation.trigger_config)
    except InvalidToken:
        # A stored value no sender can prove (another key, not a string, a vault
        # reference): refused as a wrong signature is, never a 500.
        logger.warning("Evaluation resume callback refused: the resume secret cannot be read")
        raise HTTPException(status_code=401, detail="Invalid callback signature") from None
    if not secret:
        raise HTTPException(
            status_code=400,
            detail="Resume secret is not configured for this evaluation",
        )

    raw_body = await request.body()
    expected_signature = hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    if not x_signature or not timing_safe_str_eq(x_signature, expected_signature):
        raise HTTPException(status_code=401, detail="Invalid callback signature")

    resumed_episode, outcome = await ConversationContinuationService.resume_from_callback(
        db,
        correlation_id=body.correlation_id,
        idempotency_key=body.idempotency_key,
        payload=body.callback_payload,
    )
    if not resumed_episode:
        raise HTTPException(status_code=404, detail="Evaluation episode not found")

    if outcome is CallbackOutcome.DISCARDED:
        # 410, not a 200 saying "duplicate". The episode was closed
        # before the callback arrived, so the payload is dropped and nobody
        # handled the caller's work: the opposite of a duplicate. A status code
        # rather than a body field, because a caller treating every 2xx as
        # delivered does not read the body. 410 is the house spelling for "was
        # valid, no longer is" (``auth.py`` uses it for a consumed invitation).
        # This breaks a caller treating every 2xx as success; that caller was
        # being told dropped work was accepted, so the break is intended.
        #
        # It does not say what closed the episode: stored state cannot tell a
        # repair from an ordinary entry failure. It is scoped to the
        # episode, not the run: ``timeout_waiting_episodes`` closes one episode
        # while sibling episodes keep the run going, and
        # ``_reconcile_run_status`` leaves it ``running`` while entries remain,
        # so telling the caller to re-run could restart an active run.
        raise HTTPException(
            status_code=410,
            detail=(
                f"This callback's episode ({resumed_episode.id}) was closed as "
                f"{resumed_episode.status} before the callback arrived, so the "
                "payload was not applied. This is not a duplicate of work "
                "already handled — nothing processed it."
            ),
        )

    duplicate = outcome is CallbackOutcome.DUPLICATE
    should_enqueue = not duplicate and resumed_episode.status == "resumed"
    episode_id = str(resumed_episode.id)

    # Enqueued in the same transaction as the acceptance, committed once.
    # With two commits, the gap between them is a hole both ways:
    #
    # - a poll there sees the episode ``resumed`` with no execution, so
    #   ``is_callback_pending_for_run`` is false and ``get_run`` tells the
    #   client to stop polling while the resume is about to rewrite headlines;
    # - a ``create_execution`` failure after the first commit leaves the
    #   callback accepted with no resume queued and nothing to repair it
    #   (``timeout_waiting_episodes`` only takes ``waiting_callback``,
    #   ``fail_abandoned_episode`` needs an execution).
    #
    # The Redis nudge stays after the commit: waking a worker for a
    # transaction that has not landed cannot work.
    # Imported above the branch: ``notify_orchestration_wake`` is called in a
    # second ``if should_enqueue`` block after the commit, which must not rely
    # on a name bound inside this one.
    from app.core.config import settings
    from app.services.orchestration_engine import (
        create_execution,
        notify_orchestration_wake,
    )

    if should_enqueue:
        # The resume runs as a fresh evaluation execution, so the reconciler
        # processes it with retries and recovery through the EvaluationAdapter.
        # Bounded by the evaluation's own budget plus a grace margin, so the
        # reconciler does not fall back to the generic ``worker_job_timeout``
        # (10 min) and time the resume out early.
        deadline_at = datetime.now(UTC) + timedelta(
            seconds=(evaluation.timeout_seconds or 3600)
            + settings.orchestration_grace_seconds
        )

        # No dedup_key: ``resume_from_callback`` already enforces idempotency
        # (a repeat callback returns duplicate=True and skips this branch), and
        # a stable key would short-circuit a legitimate second resume of the
        # same episode against a prior completed execution.
        #
        # ``idempotency_key`` is a generation token. A second
        # non-duplicate callback is legal on a ``"resumed"`` episode, so two
        # resume executions can be outstanding at once; the key keeps the older
        # one's dead-letter cleanup from finalizing the episode the newer
        # callback owns, which would silently drop an accepted callback.
        await create_execution(
            db,
            adapter="evaluation",
            payload={
                "kind": "resume_episode",
                "episode_id": episode_id,
                "idempotency_key": body.idempotency_key,
            },
            deadline_at=deadline_at,
        )
    await db.commit()
    if should_enqueue:
        await notify_orchestration_wake()

    return {
        "accepted": True,
        "duplicate": duplicate,
        "episode_id": str(resumed_episode.id),
        "status": resumed_episode.status,
    }


@router.get("/runs/diff", response_model=RunDiffResponse)
async def diff_run_params(
    run_a: str,
    run_b: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Diff two runs' parameters into changed vs identical.

    Registered before ``/runs/{run_id}`` so the literal ``diff`` path is not
    captured as a run id.
    """
    from app.services.evaluation import params as run_params

    rid_a = parse_uuid(run_a, label="Run ID")
    rid_b = parse_uuid(run_b, label="Run ID")
    result = await db.execute(
        select(EvaluationRun.id).where(EvaluationRun.id.in_([rid_a, rid_b]))
    )
    found = {r.id for r in result}
    if rid_a not in found or rid_b not in found:
        raise HTTPException(status_code=404, detail="Evaluation run not found")

    params_a = await run_params.get_run_params(db, rid_a)
    params_b = await run_params.get_run_params(db, rid_b)
    diff = run_params.compute_param_diff(params_a, params_b)
    return {"run_a": run_a, "run_b": run_b, **diff}


@router.get("/runs/{run_id}/params", response_model=RunParamsResponse)
async def get_run_params(
    run_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Read a run's logged experimental-condition parameters."""
    from app.services.evaluation import params as run_params

    rid = parse_path_id(run_id, not_found_detail="Evaluation run not found")
    result = await db.execute(select(EvaluationRun.id).where(EvaluationRun.id == rid))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Evaluation run not found")
    return {"run_id": run_id, "params": await run_params.get_run_params(db, rid)}


async def _load_run_or_404(db: AsyncSession, run_id: str) -> EvaluationRun:
    rid = parse_path_id(run_id, not_found_detail="Evaluation run not found")
    result = await db.execute(select(EvaluationRun).where(EvaluationRun.id == rid))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Evaluation run not found")
    return run


def _metric_half(key: str) -> str | None:
    """Which headline a metric composes, or `None` for execution telemetry.

    Membership is a package-registry fact, not a naming convention: a key
    prefix heuristic in the browser would misfile a metric named differently,
    and the client cannot tell telemetry from quality. Shared by the run
    aggregate and the per-result routes so one metric is never filed under two
    headlines on the two panels that show it.
    """
    from app.services.evaluation.metric_package.directions import (
        is_performance_key,
        is_quality_key,
    )

    if is_quality_key(key):
        return "quality"
    if is_performance_key(key):
        return "performance"
    return None


@router.get(
    "/runs/{run_id}/results/{result_id}/metrics",
    response_model=list[MetricResponse],
)
async def get_result_metrics(
    run_id: str,
    result_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List the scalar metrics logged for one result, each with how it reads on one entry."""
    rid = parse_path_id(run_id, not_found_detail="Evaluation run not found")
    resid = parse_path_id(result_id, not_found_detail="Result not found")
    rows = await db.execute(
        select(EvaluationMetric.key, EvaluationMetric.value)
        .where(
            EvaluationMetric.run_id == rid,
            EvaluationMetric.result_id == resid,
        )
        .order_by(EvaluationMetric.key)
    )
    response = []
    for k, v in rows.all():
        view = metric_entry_view(k)
        response.append(
            {
                "key": k,
                "value": v,
                "half": _metric_half(k),
                "unit": metric_unit(k),
                "shown_on_entry": view.shown,
                "entry_label": view.label,
            }
        )
    return response


@router.get(
    "/runs/{run_id}/metrics/aggregate",
    response_model=RunMetricsAggregateResponse,
)
async def get_run_metrics_aggregate(
    run_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Server-side per-metric aggregates for a run."""
    from app.services.evaluation import metrics as run_metrics

    run = await _load_run_or_404(db, run_id)
    aggregates = await run_metrics.get_run_aggregates(db, run.id)

    return {
        "run_id": str(run.id),
        "aggregates": [
            {
                "key": k,
                "half": _metric_half(k),
                "unit": metric_unit(k),
                "pooled": is_pooled_key(k),
                "macro": is_macro_key(k),
                **v,
            }
            for k, v in sorted(aggregates.items())
        ],
    }


# The warning reads one evaluation's agents; each costs a few queries.
MAX_ALWAYS_ASK_AGENT_IDS = 50


@router.get("/always-ask-tools", response_model=AlwaysAskToolsResponse)
async def get_always_ask_tools(
    agent_ids: list[str] = Query(default_factory=list, max_length=MAX_ALWAYS_ASK_AGENT_IDS),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """The given agents' tools an administrator marked "always ask", for the warning under the setting.

    With the evaluation's tool approval setting off, those tools run without
    approval. Read from each agent's configuration, without running a turn
    (``services/evaluation/always_ask.py``, which says what it cannot see).
    An unknown or malformed id is left out; more than
    ``MAX_ALWAYS_ASK_AGENT_IDS`` ids is a 422. A 503 when the marks cannot be
    read, so the card says it could not check rather than showing no warning.
    Declared before ``/{evaluation_id}``.
    """
    from app.services.evaluation import always_ask
    from app.services.hitl_whitelist import WhitelistUnavailableError

    ids = [agent_uuid for raw in agent_ids if (agent_uuid := safe_uuid(str(raw))) is not None]
    agents = (
        list((await db.execute(select(Agent).where(Agent.id.in_(ids)))).scalars()) if ids else []
    )
    try:
        report = await always_ask.always_ask_tools(db, agents)
    except WhitelistUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Could not read the tools marked "always ask".',
        ) from None
    return {"agents": report}


@router.get("/prompt-guard", response_model=PromptGuardStateResponse)
async def get_prompt_guard_state(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Whether a prompt guard is connected, for the line under the evaluation's setting.

    The guard's own test (``prompt_guards._evaluate``): chat checks every
    incoming message while one is connected, so "connected" is the whole
    condition under which an evaluation that skips the guard departs from chat.
    Declared before ``/{evaluation_id}`` so the path is not read as an id.
    """
    from app.services.integration_state import any_provider_connected
    from app.services.prompt_guards import GUARD_PROVIDER

    return {"connected": await any_provider_connected(db, GUARD_PROVIDER)}


@router.get("/runs/{run_id}/tags", response_model=RunTagsResponse)
async def get_run_tags(
    run_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Read a run's mutable tags."""
    run = await _load_run_or_404(db, run_id)
    return {"run_id": str(run.id), "tags": run.tags or {}}


async def _assert_run_tag_manage(db: AsyncSession, run: EvaluationRun, user: User) -> None:
    """Tag edits require manage rights on the parent evaluation."""
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, str(run.evaluation_id), user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(evaluation, user)


@router.patch("/runs/{run_id}/tags", response_model=RunTagsResponse)
async def patch_run_tags(
    run_id: str,
    body: PatchRunTagsRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Merge tags into a run; keys mapped to null are removed."""
    run = await _load_run_or_404(db, run_id)
    await _assert_run_tag_manage(db, run, user)
    merged = dict(run.tags or {})
    for k, v in (body.tags or {}).items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    run.tags = strip_reserved_keys(merged) or None
    await db.commit()
    return {"run_id": str(run.id), "tags": run.tags or {}}


@router.post("/runs/search", response_model=RunSearchResponse)
async def search_runs(
    body: RunSearchRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Combined server-side run search over params, metrics, scores, tags, dates.

    A row carries the run's headlines and tags, not its params or metric
    aggregates: no reader used them, and they cost two queries per row.
    The run's own endpoints serve them.
    """
    from app.services.evaluation.run_search import search_evaluation_runs

    user_group_ids = await get_user_group_ids(db, user.id)
    visible = user_visible_query(
        Evaluation, user.id, user_group_ids, is_admin=user.is_admin
    ).with_only_columns(Evaluation.id)
    visible_eval_ids = {r for (r,) in (await db.execute(visible)).all()}

    runs = await search_evaluation_runs(db, body, visible_eval_ids)
    platform_errors = await _platform_error_counts(db, [run.id for run, _, _ in runs])
    items: list[dict] = []
    for run, effective_tags, is_baseline in runs:
        items.append(
            RunSearchResultItem(
                id=str(run.id),
                evaluation_id=str(run.evaluation_id),
                agent_id=str(run.agent_id) if run.agent_id else None,
                flow_id=str(run.flow_id) if run.flow_id else None,
                model_id=run.model_id,
                status=run.status,
                score=run.score,
                speed_score=run.speed_score,
                overall_score=run.overall_score,
                f1_score=run.f1_score,
                quality_score=run.quality_score,
                performance_score=run.performance_score,
                created_at=run.created_at.isoformat() if run.created_at else "",
                tags=run.tags,
                effective_tags=effective_tags,
                is_baseline=is_baseline,
                anomaly_count=run.anomaly_count or 0,
                platform_error_count=platform_errors.get(run.id, 0),
                arm=run.arm or "real",
                placebo_target=run.placebo_target,
                placebo_comparison_id=str(run.placebo_comparison_id) if run.placebo_comparison_id else None,
            ).model_dump()
        )
    return {"runs": items, "count": len(items)}


@router.get("/runs/{run_id}", response_model=EvaluationRunDetailResponse)
async def get_run(
    run_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get a run with all its results."""
    rid = parse_path_id(run_id, not_found_detail="Evaluation run not found")
    result = await db.execute(select(EvaluationRun).where(EvaluationRun.id == rid))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Evaluation run not found")

    run_mode, fallen_back = await load_run_detail_mode(db, run)
    resp = await _build_run_response(
        db,
        run,
        held_reasons=await _held_reasons_for(db, [run]),
        run_modes={run.id: run_mode},
    )
    resp["placebo_snapshot"] = run.placebo_snapshot

    # Deferred shared performance pass: a run turns terminal
    # before the batch's coalesced Overall Performance pass commits, so a
    # terminal run can still be waiting on a `performance_score` refresh.
    # "running"/"pending" runs are covered by the frontend's status poll.
    if run.status not in ("running", "pending"):
        from app.services.evaluation_engine import is_rescore_pending_for_run

        rescore_pending = await is_rescore_pending_for_run(db, str(run.id))
        resp["rescore_pending"] = rescore_pending
        if not rescore_pending:
            # `performance_score` and every performance-derived field were
            # snapshotted from the `run` loaded by `_build_run_response`. If the
            # shared pass committed between that load and the pending check, all
            # of them are stale together, so all are rebuilt from one fresh read;
            # refreshing the score alone would pair it with old data.
            #
            # A column select, not `db.refresh(run)`: it bypasses the identity
            # map, which already holds `run` in this session.
            fresh_row = (
                await db.execute(
                    select(
                        EvaluationRun.performance_score,
                        EvaluationRun.headline_details,
                    ).where(EvaluationRun.id == run.id)
                )
            ).one_or_none()
            # A `None` row means the run was deleted since the first load:
            # keep the snapshot rather than overwrite it with nulls;
            # the client 404s on its next poll anyway.
            if fresh_row is not None:
                fresh_score, fresh_headline_details = fresh_row
                fresh_details = fresh_headline_details or {}
                resp["performance_score"] = fresh_score
                resp["performance_metric_keys"] = (
                    fresh_details.get("performance_metric_keys") or []
                )
                resp["metric_entry_counts"] = (
                    fresh_details.get("metric_entry_counts") or {}
                )
                resp["performance_contributions"] = (
                    fresh_details.get("performance_contributions") or {}
                )
                resp["performance_aggregation_excluded"] = (
                    fresh_details.get("performance_excluded") or []
                )

        # A late callback can still rewrite this run's headlines after it reads
        # terminal: ``_reconcile_run_status`` recomputes them when the
        # outside system answers, and the status poll stops at terminal.
        callback_pending = await is_callback_pending_for_run(db, str(run.id))
        resp["callback_pending"] = callback_pending
        if not callback_pending:
            # The mirror of the rescore re-read above, on the other half of the
            # headline: a resolving callback writes ``score``,
            # ``quality_score``, ``f1_score``, ``status``,
            # ``completed_entries`` and the quality side of
            # ``headline_details``, and ``_write_canonical_headlines`` skips the
            # performance pass. A callback committing between
            # ``_build_run_response`` and this check would otherwise leave the
            # client told "nothing pending" with numbers that just changed, and
            # once the flag is False nothing corrects it.
            #
            # Coupled to what ``_reconcile_run_status`` writes: a field added
            # there and not here goes stale silently. Selecting the columns and
            # the EXISTS in one statement would remove the list; it is kept
            # parallel to the block above instead.
            #
            # Columns, not the entity: a column select bypasses the identity
            # map, which still holds the pre-callback instance.
            fresh = (
                await db.execute(
                    select(
                        EvaluationRun.score,
                        EvaluationRun.quality_score,
                        EvaluationRun.f1_score,
                        EvaluationRun.status,
                        EvaluationRun.error,
                        EvaluationRun.completed_entries,
                        EvaluationRun.headline_details,
                    ).where(EvaluationRun.id == run.id)
                )
            ).one_or_none()
            if fresh is not None:
                resp["score"] = fresh.score
                resp["quality_score"] = fresh.quality_score
                resp["f1_score"] = fresh.f1_score
                resp["status"] = fresh.status
                resp["error"] = fresh.error
                resp["completed_entries"] = fresh.completed_entries
                fresh_details = fresh.headline_details or {}
                # All five keys the quality recompute writes together:
                # refreshing a subset would pair a fresh ``quality_score`` with
                # stale explanation data that looks authoritative.
                resp["quality_metric_keys"] = (
                    fresh_details.get("quality_metric_keys") or []
                )
                resp["quality_contributions"] = (
                    fresh_details.get("quality_contributions") or {}
                )
                resp["quality_coverage"] = fresh_details.get("quality_coverage")
                resp["metric_entry_counts"] = (
                    fresh_details.get("metric_entry_counts") or {}
                )
                resp["quality_aggregation_excluded"] = (
                    fresh_details.get("excluded") or []
                )

            # The result-derived aggregates too: a resolving callback
            # writes a result row (``_store_resumed_answer``, scored by
            # ``_score_resumed_answer``), and the results list below is read
            # fresh, so a stale average would not match the rows it averages.
            # Same helper, so the same formula.
            (
                resp["avg_sub_scores"],
                resp["reused_entries"],
                resp["judge_failure_count"],
                resp["platform_error_count"],
            ) = await _get_avg_sub_scores(db, run)

    results_result = await db.execute(
        select(
            EvaluationResult,
            DatasetEntry.input_prompt,
            DatasetEntry.description,
            DatasetEntry.file_ids,
            DatasetEntry.expected_answer,
        )
        .outerjoin(
            DatasetEntry,
            DatasetEntry.id == EvaluationResult.entry_id,
        )
        .where(EvaluationResult.run_id == run.id)
        .order_by(EvaluationResult.created_at)
    )
    rows = list(results_result)
    result_ids = [r.id for r, *_rest in rows]

    trace_rows = []
    if result_ids:
        trace_result = await db.execute(
            select(EvaluationResultTrace)
            .where(EvaluationResultTrace.result_id.in_(result_ids))
            .order_by(
                EvaluationResultTrace.result_id.asc(),
                EvaluationResultTrace.trace_order.asc(),
            )
        )
        trace_rows = trace_result.scalars().all()

    trace_ids_by_result: dict[uuid.UUID, list[str]] = {}
    for link in trace_rows:
        trace_ids_by_result.setdefault(link.result_id, []).append(str(link.trace_id))

    # Batch-resolve file metadata. An entry's ``file_ids`` is stored as its
    # create / update body sent it: a value that is not a UUID names no file,
    # like an unknown id, and is skipped.
    all_file_uuids = [
        parsed for _, _, _, fids, _ in rows for fid in fids or [] if (parsed := safe_uuid(fid)) is not None
    ]
    file_map: dict[str, dict] = {}
    if all_file_uuids:
        from app.models.file_attachment import FileAttachment

        fa_result = await db.execute(
            select(FileAttachment).where(FileAttachment.id.in_(all_file_uuids))
        )
        file_map = {
            str(a.id): {
                "id": str(a.id),
                "filename": a.filename,
                "size": a.size,
                "content_type": a.content_type,
            }
            for a in fa_result.scalars().all()
        }

    resp["results"] = [
        {
            "id": str(r.id),
            "run_id": str(r.run_id),
            "entry_id": str(r.entry_id) if r.entry_id is not None else None,
            "description": description,
            "input_prompt": input_prompt or "",
            "expected_answer": expected_answer or "",
            "files": [file_map[fid] for fid in (file_ids or []) if fid in file_map]
            or None,
            "agent_output": r.agent_output,
            "trace_ids": trace_ids_by_result.get(r.id, []),
            "score": r.score,
            "duration_seconds": r.duration_seconds,
            "speed_score": r.speed_score,
            "overall_score": r.overall_score,
            "f1_score": r.f1_score,
            "sub_scores": r.sub_scores or {},
            "evaluator_reasoning": r.evaluator_reasoning,
            "input_fingerprint": r.input_fingerprint,
            "is_reused": bool(r.is_reused),
            "reused_from_result_id": str(r.reused_from_result_id)
            if r.reused_from_result_id is not None
            else None,
            "fell_back_to_llm_only": r.id in fallen_back,
            "is_execution_anomaly": r.execution_anomaly_kind is not None,
            "execution_anomaly_kind": r.execution_anomaly_kind,
            "is_judge_failure": bool(r.is_judge_failure),
            "execution_details": _truncate_execution_details_for_response(
                r.execution_details
            ),
            "created_at": r.created_at.isoformat(),
        }
        for r, input_prompt, description, file_ids, expected_answer in rows
    ]

    # One in-memory pass over rows this route already selected, no query.
    # Not in ``_build_run_response``, which loads no results and runs
    # once per row in the list endpoints.
    resp["extraction_failure_count"] = sum(
        count_extraction_failures(r.execution_details) for r, *_rest in rows
    )

    return resp


@router.get("/runs/{run_id}/results/{result_id}/traces/{trace_index}/download")
async def download_run_result_trace(
    run_id: str,
    result_id: str,
    trace_index: int,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Download one full execution trace exactly as stored in DB."""
    rid = parse_path_id(run_id, not_found_detail="Evaluation run not found")

    run_row = await db.execute(select(EvaluationRun).where(EvaluationRun.id == rid))
    run = run_row.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Evaluation run not found")

    result_row = await db.execute(
        select(EvaluationResult).where(
            EvaluationResult.id == parse_path_id(result_id, not_found_detail="Evaluation result not found"),
            EvaluationResult.run_id == run.id,
        )
    )
    eval_result = result_row.scalar_one_or_none()
    if not eval_result:
        raise HTTPException(status_code=404, detail="Evaluation result not found")

    details = eval_result.execution_details or {}
    traces = details.get("traces") if isinstance(details, dict) else None
    if not isinstance(traces, list):
        raise HTTPException(status_code=404, detail="No traces found for this result")

    if trace_index < 0 or trace_index >= len(traces):
        raise HTTPException(status_code=404, detail="Trace index out of bounds")

    trace = traces[trace_index]
    if not isinstance(trace, dict):
        raise HTTPException(status_code=404, detail="Trace payload is not a JSON object")

    payload = {
        "run_id": str(run.id),
        "result_id": str(eval_result.id),
        "trace_index": trace_index,
        "trace": trace,
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"evaluation_trace_{run.id}_{eval_result.id}_{trace_index}.json"

    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/json",
        headers={"Content-Disposition": build_content_disposition(filename)},
    )


@router.post("/runs/{run_id}/results/{result_id}/replay-judge")
async def replay_judge_result(
    run_id: str,
    result_id: str,
    body: ReplayJudgeRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Replay the judge for a single result with an optional larger max_tokens."""
    rid = parse_path_id(run_id, not_found_detail="Evaluation run not found")

    run_result = await db.execute(select(EvaluationRun).where(EvaluationRun.id == rid))
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Evaluation run not found")
    if run.status != "completed":
        raise HTTPException(status_code=400, detail="Run must be completed")

    result_row = await db.execute(
        select(EvaluationResult).where(
            EvaluationResult.id == parse_path_id(result_id, not_found_detail="Evaluation result not found"),
            EvaluationResult.run_id == run.id,
        )
    )
    eval_result = result_row.scalar_one_or_none()
    if not eval_result:
        raise HTTPException(status_code=404, detail="Evaluation result not found")

    evaluation_row = await db.execute(
        select(Evaluation).where(Evaluation.id == run.evaluation_id)
    )
    evaluation = evaluation_row.scalar_one_or_none()
    if not evaluation:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    require_admin_for_company_managed(evaluation, user)

    from app.services.evaluation import metrics as run_metrics
    from app.services.evaluation_engine import (
        _compute_run_headlines,
        _ms,
        evaluation_owner_id,
        score_result,
        stored_judge_inputs,
        with_judge_pass,
    )

    # What the fresh pass gave the judge: the entry the run was scored against (its
    # dataset snapshot first), the same instructions, tools and tool calls.
    judge_inputs = await stored_judge_inputs(db, evaluation, eval_result)
    if judge_inputs.entry is None:
        raise HTTPException(status_code=404, detail="Dataset entry not found")

    judge_t0 = time.perf_counter()
    score_data = await score_result(
        db,
        eval_result.agent_output,
        judge_inputs.entry,
        evaluation.evaluator_model,
        evaluator_prompt=evaluation.evaluator_prompt,
        evaluation_instructions=judge_inputs.instructions,
        available_tools=judge_inputs.available_tools,
        tool_calls=judge_inputs.tool_calls,
        sub_score_weights=evaluation.sub_score_weights,
        judge_max_tokens=body.judge_max_tokens,
        user_id=evaluation_owner_id(evaluation),
    )
    # This pass's Judge Time and timeline step, as a rescore records them.
    judge_duration_ms = _ms(judge_t0, time.perf_counter())
    eval_result.execution_details = with_judge_pass(
        eval_result.execution_details, score_data, judge_duration_ms, evaluation.evaluator_model
    )
    await run_metrics.log_judge_seconds(
        db, result_id=eval_result.id, run_id=run.id, judge_duration_ms=judge_duration_ms
    )

    eval_result.score = score_data["score"]
    eval_result.sub_scores = score_data["sub_scores"]
    eval_result.evaluator_reasoning = score_data["evaluator_reasoning"]
    # The judge's own verdict on whether it produced a usable one.
    # Written because the headlines are recomputed below and
    # ``_compute_run_headlines`` excludes judge failures: a replay that newly
    # fails must leave the headline, one that newly succeeds must join it. The
    # anomaly flags are untouched: they classify the agent output, which a
    # replay does not re-run.
    #
    # Only an explicit ``False`` says the verdict is usable; anything else
    # excludes the result. ``score_result`` always carries the key, so absence
    # is a broken contract, and excluding is the visible way to fail.
    #
    # What absence means depends on where ``score_data`` came from:
    #
    # (a) built by ``score_result``: absence is a broken contract -> exclude.
    #     Here, and ``score_stored_result`` in ``evaluation_engine``, for a
    #     rescore and a resumed callback (spelled ``bool(..., False)``,
    #     equivalent).
    # (b) built by hand because the judge did not run (the main loop and the
    #     judge trace in ``evaluation_engine``): absence means "no judge ran",
    #     not a failure -> stay open. Excluding there would drop every result
    #     of an evaluation run without the LLM judge from its aggregates.
    # (c) supplied by the user (``import_evaluation``): absence means an
    #     older export -> stay open.
    eval_result.is_judge_failure = score_data.get("is_judge_failure") is not False

    # Serialize concurrent replays on the same result: lock the parent row
    # before the delete + MAX(trace_order) recompute, so two replays cannot
    # pick the same next_order and trip uq_eval_result_trace_order.
    await db.execute(
        select(EvaluationResult.id)
        .where(EvaluationResult.id == eval_result.id)
        .with_for_update()
    )
    await db.execute(
        delete(EvaluationResultTrace).where(
            EvaluationResultTrace.result_id == eval_result.id,
            EvaluationResultTrace.trace_role == "judge",
        )
    )
    judge_trace_id = score_data.get("judge_trace_id")
    if isinstance(judge_trace_id, str) and judge_trace_id:
        try:
            parsed_judge_trace_id = uuid.UUID(judge_trace_id)
        except ValueError:
            parsed_judge_trace_id = None
        existing_rows = await db.execute(
            select(
                func.coalesce(func.max(EvaluationResultTrace.trace_order), 0)
            ).where(EvaluationResultTrace.result_id == eval_result.id)
        )
        next_order = int(existing_rows.scalar() or 0) + 1
        if parsed_judge_trace_id is not None:
            db.add(
                EvaluationResultTrace(
                    result_id=eval_result.id,
                    trace_id=parsed_judge_trace_id,
                    trace_role="judge",
                    trace_order=next_order,
                )
            )

    # Recompose headlines at evaluation scope: Overall Performance is
    # comparative, so one replayed result shifts the basis of every sibling
    # run. Only the canonical writer sets ``run.score``; a second headline
    # formula here would diverge from it.
    await _compute_run_headlines(db, run.evaluation_id)
    await db.commit()

    return {
        "status": "ok",
        "run_id": str(run.id),
        "result_id": str(eval_result.id),
        "score": eval_result.score,
        "judge_max_tokens": body.judge_max_tokens,
        "judge_model": score_data.get("judge_model"),
        "judge_stop_reason": score_data.get("judge_stop_reason"),
        "judge_trace_id": judge_trace_id,
    }


async def _cancel_execution_unless_it_owns_siblings(
    db: AsyncSession, *, execution_id: uuid.UUID, run_id: uuid.UUID
) -> bool:
    """Cancel *execution_id*, but only when *run_id* is the only run it owns.

    ``cancel_execution`` stops an execution, ``cancel_run`` one run, and the
    two differ. A trigger stamps one execution on every agent x model
    run it creates, and ``execute_evaluation`` dispatches them all through
    ``_gather_runs_bounded``, whose ``cancel_event`` cancels the whole batch:
    cancelling that execution for one run would stop every sibling and leave
    them ``running`` with no worker until the stranded sweep fails them.

    A run-scoped execution (a single rescore, a replay) is cancelled, which
    interrupts a model call in flight rather than waiting for an entry
    boundary like the cooperative stop in ``execute_evaluation_run``.

    The count is taken under the execution's row lock, which makes it
    race-free: ``execute_evaluation`` fences on the execution ``FOR UPDATE``
    (``_fence_on_lease``) before it stamps owners onto unowned pending runs.
    The lock is taken first, in the global EXECUTION -> RUN -> EPISODE order.

    Returns True when the execution was cancelled.
    """
    from app.models.orchestration import OrchestrationExecution
    from app.services.evaluation.run_ownership import runs_this_execution_may_touch
    from app.services.orchestration_engine import cancel_execution

    locked = (
        await db.execute(
            select(OrchestrationExecution.id)
            .where(OrchestrationExecution.id == execution_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked is None:
        return False

    # Asked as "does this stop anything else", not "does it own one run": the
    # caller's idea of the owner can be stale (the repair path re-reads it).
    siblings = (
        await db.execute(
            select(func.count())
            .select_from(EvaluationRun)
            .where(
                # Strict ownership, through the one module that expresses it.
                # An unowned run is not one this execution
                # would stop, and this count is not evaluation-scoped, so the
                # harmless arm (``OR owner_execution_id IS NULL``) would count
                # every unowned run on the platform and suppress the cancel.
                runs_this_execution_may_touch(
                    execution_id, repeating_is_harmless=False
                ),
                EvaluationRun.id != run_id,
            )
        )
    ).scalar_one()
    if siblings:
        logger.info(
            "cancel_run: execution %s also owns %d other run(s); not cancelling "
            "it. Run %s stops at its next entry boundary instead.",
            execution_id,
            siblings,
            run_id,
        )
        return False

    await cancel_execution(db, execution_id=execution_id)
    return True


async def _owning_payload(db: AsyncSession, run: EvaluationRun) -> dict | None:
    """The payload of the execution that owns *run*, ``None`` without one.

    A column select, which takes no lock, so it may come before the lock order of
    ``_cancel_active_run``.
    """
    from app.models.orchestration import OrchestrationExecution

    if run.owner_execution_id is None:
        return None
    return (
        await db.execute(
            select(OrchestrationExecution.payload).where(
                OrchestrationExecution.id == run.owner_execution_id
            )
        )
    ).scalar_one_or_none()


async def _cancel_evaluation_executions(db: AsyncSession, evaluation_id: uuid.UUID) -> None:
    """Cancel every execution still working on *evaluation_id*'s runs.

    For a deletion of all of them: an execution owns runs of one evaluation only (a
    trigger, a replay or a rescore is staged for one), so none of what it stops
    survives, unlike ``cancel_run``'s single run (``_cancel_execution_unless_it_owns_siblings``).
    The executions owning an active run, and the evaluation's dispatch not yet
    claimed. Executions first, before any run is touched (the lock order of
    ``cancel_run``); the worker learns at its next heartbeat.
    """
    from app.models.orchestration import TERMINAL_EXECUTION_STATES, OrchestrationExecution
    from app.services.orchestration_engine import cancel_execution

    owners = (
        await db.execute(
            select(EvaluationRun.owner_execution_id).where(
                EvaluationRun.evaluation_id == evaluation_id,
                EvaluationRun.status.in_(("pending", "running")),
            )
        )
    ).scalars().all()
    dispatches = (
        await db.execute(
            select(OrchestrationExecution.id).where(
                OrchestrationExecution.adapter == "evaluation",
                OrchestrationExecution.payload["evaluation_id"].astext == str(evaluation_id),
                OrchestrationExecution.state.not_in(TERMINAL_EXECUTION_STATES),
            )
        )
    ).scalars().all()
    # Sorted, so two deletions lock in one order.
    for execution_id in sorted({*owners, *dispatches} - {None}, key=str):
        await cancel_execution(db, execution_id=execution_id)


@router.post("/runs/{run_id}/cancel", response_model=EvaluationRunResponse)
async def cancel_run(
    run_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a running or pending evaluation run, or the rescore running on it.

    A rescore re-judges a finished run's stored answers, so cancelling it hands the
    run back as it was: its status from before the rescore, its original
    ``completed_at`` and ``duration_seconds``, and the scores the rescore had not
    yet committed.
    """
    rid = parse_path_id(run_id, not_found_detail="Evaluation run not found")
    result = await db.execute(select(EvaluationRun).where(EvaluationRun.id == rid))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Evaluation run not found")
    if run.status not in ("pending", "running"):
        raise HTTPException(status_code=400, detail="Run is not active")
    await _cancel_active_run(db, run)
    return await _build_run_response(db, run)


async def _cancel_active_run(db: AsyncSession, run: EvaluationRun) -> None:
    """Stop an active (``pending`` / ``running``) run's work and record it; commits.

    The cancel path, shared by ``cancel_run`` and the deletions, which must
    stop what a run is doing before its row goes.
    """
    from app.services.evaluation_engine import rescore_previous_status

    # A rescore says so in its execution's payload, whatever the run's
    # timing: a run imported or failed without a ``completed_at`` is rescored too.
    # Without that execution, the engine's own marker: ``running`` again over the
    # timing of the run it re-judges.
    payload = await _owning_payload(db, run)
    rescore_in_flight = run.status == "running" and (
        payload.get("kind") == "rescore_runs"
        if payload is not None
        else run.completed_at is not None
    )

    # Stop the work, not just the row: marking the run alone lets the
    # worker execute and judge every remaining entry.
    #
    # Lock order: execution, then run, then episodes, and the execution lock is
    # taken before the run is touched at all. Mutating ``run`` first
    # leaves the session dirty, and autoflush emits ``UPDATE evaluation_runs``
    # ahead of ``cancel_execution``'s SELECT, taking the run lock first (the
    # autoflush trap). The worker holds execution and reaches for run
    # (``_fence_on_lease`` then the reclaim's ``UPDATE``, in one transaction),
    # so run-then-execution here is an ABBA deadlock. Taking the execution
    # first also means the worker learns at its next heartbeat.
    #
    # ``owner_execution_id`` is the link, not
    # ``cancel_executions_for_runs``: that matches the assignment-run payload
    # keys, which an evaluation execution does not carry.
    #
    # Two mechanisms for two granularities. When this run is the only
    # one its execution owns (a single rescore, a replay), cancelling the
    # execution is the better stop: ``extend_lease`` raises ``LeaseLostError``,
    # the heartbeat turns it into ``cancel_event``, and the fan-out
    # cancels in-flight model calls, within one heartbeat (~20s). A
    # shared execution (more than one agent x model pair) would stop every
    # sibling that way, so those runs stop cooperatively at their next entry
    # boundary in ``execute_evaluation_run``.
    # ``_cancel_execution_unless_it_owns_siblings`` tells them apart.
    if run.owner_execution_id is not None:
        await _cancel_execution_unless_it_owns_siblings(
            db, execution_id=run.owner_execution_id, run_id=run.id
        )

    if rescore_in_flight:
        if payload is not None:
            # The rescore stops at its next result, or with the execution
            # (``_rescore_was_cancelled``): the run no longer reads ``running``. It
            # opens no episode, so there is none to close. A past rescore's
            # failure stays: the scores shown are still from before it.
            run.status = rescore_previous_status(payload, run.id)
        else:
            # No execution to say what the run was: cancelled, over the original
            # timing, which the rescore never re-ran.
            run.status = "cancelled"
            run.error = "Cancelled by user"
        await db.commit()
        return

    run.status = "cancelled"
    run.error = "Cancelled by user"
    run.completed_at = datetime.now(UTC)
    started_at = run.started_at
    completed_at = run.completed_at
    if started_at is not None and completed_at is not None:
        run.duration_seconds = round(
            (completed_at - started_at).total_seconds(), 2
        )

    # Then the episodes. Left open, a late callback is still accepted and
    # scored (a judge call billed on a cancelled run), ``completed_entries``
    # climbs against a frozen score, and a ``running`` episode is stranded,
    # since the sweep only reaches ``"running"`` runs. The helper takes
    # the run lock before the episode locks, under the execution lock above.
    from app.services.evaluation_continuation import (
        close_episodes_for_cancelled_run,
    )

    await close_episodes_for_cancelled_run(db, run_id=run.id)

    # All of it in one transaction with the status write, so the row, its
    # entries and its execution agree or none of them changes.
    await db.commit()

    # The dispatch claim races this endpoint. ``owner_execution_id``
    # came from an unlocked read, and ``execute_evaluation`` claims unowned rows
    # with ``UPDATE ... WHERE status = 'pending' AND owner_execution_id IS
    # NULL``; a claim committing in between leaves the run cancelled but owned
    # by an execution never cancelled. Locking the run up front would put this
    # transaction in run-then-execution order (the ABBA deadlock above), so the
    # window is closed after the fact: the commit set ``'cancelled'`` and the
    # claim requires ``'pending'``, so one fresh read sees the final owner.
    #
    # The rescore endpoints also stamp a new owner, on terminal runs, but as a
    # later user action that sets the run back to ``"running"``; the status
    # guard below tells the two apart.
    #
    # Columns, not the entity: a column select never consults the identity
    # map, which holds the stale pre-claim instance. Selecting the entity would
    # silently read it.
    fresh = (
        await db.execute(
            select(EvaluationRun.owner_execution_id, EvaluationRun.status)
            .where(EvaluationRun.id == run.id)
        )
    ).one_or_none()
    owner, status_now = (fresh.owner_execution_id, fresh.status) if fresh else (None, None)
    # Still cancelled, or this is not the race being repaired. A
    # rescore landing between the commit and this read sets the run back to
    # ``"running"`` with a new owner; cancelling that execution would stop a
    # live rescore. The claim needs ``pending`` and a rescore writes
    # ``running``, so an owner that changed while the run is still
    # ``cancelled`` can only be a claim. The other six writers of the column
    # (``trigger_evaluation`` and its siblings here, ``autonomous``, run
    # creation in ``evaluation_engine``) stamp rows they just built, so none
    # reaches an existing cancelled run.
    if status_now == "cancelled" and owner is not None and owner != run.owner_execution_id:
        logger.info(
            "cancel_run: run %s was claimed by execution %s while it was being "
            "cancelled; cancelling that execution too",
            run.id,
            owner,
        )
        # Same sibling guard: the execution that claims an unowned pending row
        # is the evaluation-scoped dispatch one, the most likely to own a
        # whole matrix.
        await _cancel_execution_unless_it_owns_siblings(
            db, execution_id=owner, run_id=run.id
        )
        await db.commit()


@router.post("/runs/{run_id}/replay", response_model=EvaluationRunResponse)
async def replay_run(
    run_id: str,
    body: ReplayRunRequest | None = None,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Replay a failed or completed run with the same agent and model."""
    from app.services.evaluation_engine import resolve_run_snapshot_overrides

    rid = parse_path_id(run_id, not_found_detail="Evaluation run not found")
    result = await db.execute(select(EvaluationRun).where(EvaluationRun.id == rid))
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Evaluation run not found")

    evaluation_result = await db.execute(
        select(Evaluation).where(Evaluation.id == original.evaluation_id)
    )
    evaluation = evaluation_result.scalar_one_or_none()
    if not evaluation:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    _refuse_comparison_runs([original])
    await _require_launchable(db, evaluation)
    # A saved model no provider would answer is refused up front, as the Run
    # button does, instead of a run created to fail.
    try:
        await check_runs_served(
            db, evaluation, [(original.agent_id, original.model_id, original.is_default_model)]
        )
    except LaunchRefusedError as refused:
        raise _http_refusal(refused) from None

    entry_count_result = await db.execute(
        select(func.count(DatasetEntry.id))
        .join(EvaluationDataset, EvaluationDataset.dataset_id == DatasetEntry.dataset_id)
        .where(EvaluationDataset.evaluation_id == evaluation.id)
    )
    entry_count = entry_count_result.scalar() or 0
    if entry_count == 0:
        raise HTTPException(status_code=400, detail="No dataset entries found for this evaluation")

    # A replayed run inherits the original's pinned_dataset_versions below, so
    # its total_entries comes from that frozen snapshot, not the live count:
    # with the live count, completed_entries could never reach
    # total_entries and the run would stay "running" forever.
    if original.pinned_dataset_versions:
        from app.services.evaluation import dataset_versioning

        pinned_entry_count = await dataset_versioning.count_pinned_entries(
            db, evaluation.id, original.pinned_dataset_versions
        )
        if pinned_entry_count > 0:
            entry_count = pinned_entry_count

    _, missing_snapshots = await resolve_run_snapshot_overrides(db, original)
    if missing_snapshots:
        raise HTTPException(
            status_code=409,
            detail=(
                "Replay impossible: missing version snapshots ("
                + ", ".join(missing_snapshots)
                + ")"
            ),
        )

    new_run = EvaluationRun(
        evaluation_id=original.evaluation_id,
        agent_id=original.agent_id,
        flow_id=original.flow_id,
        flow_version_number=original.flow_version_number if original.flow_id else None,
        model_id=original.model_id,
        is_default_model=original.is_default_model,
        status="pending",
        trigger_reason="manual",
        total_entries=entry_count,
        persona_version_number=original.persona_version_number,
        prompt_versions=original.prompt_versions,
        agent_config_versions=original.agent_config_versions,
        skill_versions=original.skill_versions,
        pinned_dataset_versions=original.pinned_dataset_versions,
    )
    db.add(new_run)
    await db.flush()

    # Per-replay prediction-reuse override, mirroring ``trigger_evaluation``'s
    # ``reuse`` param: "Re-run full" forces a fresh execution of this
    # run without fanning out to every agent. ``inherit`` (or absent) falls
    # back to the evaluation's ``reuse_default``; ``on``/``off`` force it.
    # Persisted as a manual param so it outranks the auto ``reuse`` param.
    reuse_override = body.reuse if body else None
    if reuse_override is not None and reuse_override not in ("inherit", "on", "off"):
        raise HTTPException(
            status_code=422,
            detail="reuse must be one of: inherit, on, off",
        )
    if reuse_override in ("on", "off"):
        from app.services.evaluation import params as run_params

        await run_params.capture_params(
            db, new_run.id, {"reuse": reuse_override}, "manual"
        )

    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="evaluation",
        entity_id=str(evaluation.id),
        entity_name=evaluation.name,
        summary=f"Replayed evaluation run {run_id}",
    )

    # Stage the execution in the same transaction as the run, so a
    # crash cannot leave the run active with no execution to drive,
    # dead-letter or fail it.
    #
    # No try/except + rollback, unlike rescore: this endpoint holds no
    # ``with_for_update()`` lock across the call, so ``get_db()``'s teardown
    # rollback costs nothing.
    from app.services.evaluation_engine import stage_evaluation_execution
    from app.services.orchestration_engine import notify_orchestration_wake

    execution = await stage_evaluation_execution(db, str(evaluation.id))
    # Stamp the owner in the same transaction as the run: a run
    # committed active without its owning execution could be failed by
    # another execution's cleanup.
    new_run.owner_execution_id = execution.id
    await db.commit()
    await notify_orchestration_wake()

    # ``new_run`` copies ``original``'s version refs, which resolved above
    # (or this raised 409), so the re-resolution is skipped.
    return await _build_run_response(db, new_run, missing_snapshots=[])


@router.post(
    "/runs/{run_id}/rescore",
    response_model=RescoreResponse,
    response_model_exclude_none=True,
)
async def rescore_evaluation_run(
    run_id: str,
    # No field; strict, so a retired ``scorer_selection`` is a 422.
    body: RescoreRequest | None = None,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Re-run the evaluation's scorers on a run's stored predictions.

    The evaluated agent is not re-run: the stored predictions are rescored in
    place with the evaluation's scorer selection (there is no
    per-rescore override). A judge scorer still calls its evaluator model,
    hence the AI gate. ``409`` when the run has no stored predictions.

    Runs in the background: this validates and schedules the rescore
    and sets the run to ``"running"`` for its duration, so the run page's
    status-driven UI shows it with no rescoring-specific state.
    """
    rid = parse_path_id(run_id, not_found_detail="Evaluation run not found")
    # Lock the row before checking and setting status: otherwise two
    # concurrent rescores can both read "completed", both pass the
    # in-progress guard below, and both get scheduled.
    result = await db.execute(
        select(EvaluationRun).where(EvaluationRun.id == rid).with_for_update()
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Evaluation run not found")

    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, str(run.evaluation_id), user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(evaluation, user)
    if run.placebo_comparison_id is not None:
        raise HTTPException(status_code=422, detail=COMPARISON_RUN_RESCORE_REFUSAL)

    _require_runnable_family(evaluation)
    await _require_dataset_families_set(db, evaluation)

    if run.status in ("pending", "running"):
        raise HTTPException(
            status_code=409,
            detail="Run is already in progress",
        )

    predictions_count = (
        await db.execute(
            select(func.count())
            .select_from(EvaluationResult)
            .where(EvaluationResult.run_id == run.id)
        )
    ).scalar_one()
    if not predictions_count:
        raise HTTPException(
            status_code=409,
            detail="Run has no stored predictions to rescore",
        )

    from app.services.evaluation_engine import stage_rescore_execution
    from app.services.orchestration_engine import notify_orchestration_wake

    # Stage the execution before setting the run's status: both land
    # in one transaction, so a failure never commits a "running" run with
    # nothing to act on it. The row lock above makes the sequence atomic
    # against a concurrent cancel or rescore.
    try:
        execution = await stage_rescore_execution(
            db,
            str(evaluation.id),
            [str(run.id)],
            # What the run goes back to, whatever happens to the rescore.
            previous_statuses={str(run.id): run.status},
        )
    except Exception as exc:
        logger.error(
            "Failed to schedule rescore for run %s: %s",
            run.id,
            format_exception_message(exc),
            exc_info=True,
        )
        # A failed INSERT aborts the PostgreSQL transaction. Without this
        # rollback the FOR UPDATE lock above stays held until get_db()
        # teardown, and any use of `db` before then fails with
        # InFailedSQLTransaction instead of this 500.
        await db.rollback()
        raise HTTPException(
            status_code=500, detail="Failed to schedule rescore"
        ) from exc

    run.status = "running"
    # Take ownership in the same transaction as the status change. A
    # previous owner is superseded: when it is later abandoned, its cleanup
    # sees the run is no longer its own and leaves this rescore's result alone.
    run.owner_execution_id = execution.id

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="evaluation",
        entity_id=str(evaluation.id),
        entity_name=evaluation.name,
        summary=f"Started rescoring run {run_id}",
    )
    await db.commit()
    await notify_orchestration_wake()

    return RescoreResponse(run_id=str(run.id), status="running")


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(
    run_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a run and its results."""
    rid = parse_path_id(run_id, not_found_detail="Evaluation run not found")
    result = await db.execute(select(EvaluationRun).where(EvaluationRun.id == rid))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Evaluation run not found")

    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db,
        user.id,
        str(run.evaluation_id),
        user_group_ids,
        is_admin=user.is_admin,
    )
    require_admin_for_company_managed(evaluation, user)

    # Stop its work first, through the cancel path: deleting the row alone
    # leaves its execution calling, and billing, models for it.
    if run.status in ("pending", "running"):
        await _cancel_active_run(db, run)

    evaluation_id_of_run = run.evaluation_id
    if run.placebo_comparison_id is not None:
        # Its comparison lost a trial: its statistics would read the
        # others alone, so it ends cancelled, without results.
        comparison = await db.get(EvaluationPlaceboComparison, run.placebo_comparison_id)
        if comparison is not None:
            comparison.status = "cancelled"
            comparison.completed_at = comparison.completed_at or datetime.now(UTC)
            comparison.results = None
            comparison.warnings = [*(comparison.warnings or []), {"code": "run_deleted"}]
    await db.delete(run)
    await db.commit()

    # Overall Performance is comparative, so removing a run changes every
    # survivor's value: the rest must not stay scored against a run
    # that no longer exists.
    from app.services.evaluation_engine import _compute_run_headlines

    await _compute_run_headlines(db, evaluation_id_of_run)
    # The recompute writes the survivors' headlines. ``get_db`` would commit
    # them only after the response is sent, so a client refetching on the 204
    # could read the pre-delete values.
    await db.commit()


@router.delete("/{evaluation_id}/runs", status_code=status.HTTP_204_NO_CONTENT)
async def delete_all_runs(
    evaluation_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete all runs for an evaluation."""
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(evaluation, user)

    eid = parse_path_id(evaluation_id, not_found_detail="Evaluation not found")
    # Its work stops with its runs, in the same transaction.
    await _cancel_evaluation_executions(db, eid)
    await db.execute(delete(EvaluationRun).where(EvaluationRun.evaluation_id == eid))
    # A placebo comparison is its runs: none is left to read.
    await db.execute(delete(EvaluationPlaceboComparison).where(EvaluationPlaceboComparison.evaluation_id == eid))
    await db.commit()
    # No survivors to rescore here, but the recompute still runs so the two
    # deletion paths cannot drift.
    from app.services.evaluation_engine import _compute_run_headlines

    await _compute_run_headlines(db, eid)
    await db.commit()


# ---------------------------------------------------------------------------
# Evaluation CRUD
# ---------------------------------------------------------------------------


@router.get("/families", response_model=list[FamilyInfoResponse])
async def list_evaluation_families(
    _user: User = Depends(get_current_user),
):
    """List selectable output families for the evaluation UI.

    Any authenticated user can browse families; only admins can set one on an
    evaluation (create/update are admin-gated). Each family also names the output
    kinds an extractor may produce for it, so the extraction card offers and
    seeds only those, by the rule a save applies.
    """
    return [
        {**family, "extractor_output_kinds": sorted(supported_output_kinds(family["id"]))}
        for family in list_families()
    ]


@router.get("/metric-catalog", response_model=list[MetricCatalogEntry])
async def get_family_metric_catalog(
    family: str,
    user: User = Depends(require_admin),
):
    """Metric keys selectable for a family, before an evaluation exists.

    The creation screen offers the overall metric selection while the evaluation is
    being written; the same catalog as ``/{evaluation_id}/metric-catalog``, read from
    the family the author picked instead of a stored one. Declared before
    ``/{evaluation_id}``, which would otherwise read ``metric-catalog`` as an id.
    """
    _validate_family(family)
    return metric_catalog(family)


def _id_list(raw: str | None, label: str) -> list[uuid.UUID]:
    """Comma-separated ids, each parsed (422 for a malformed one), duplicates dropped."""
    return list(dict.fromkeys(parse_uuid(part.strip(), label=label) for part in (raw or "").split(",") if part.strip()))


@router.get("/compare", response_model=EvaluationComparisonResponse)
async def compare_evaluations_route(
    ids: str,
    run_ids: str | None = None,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Compare 1 to 4 evaluations of one family on the entries their datasets share
    (``services/evaluation/cross_comparison.py``); one evaluation compares its own runs.

    ``ids`` lists the evaluations, each visible to the caller (else 404); ``run_ids``
    optionally picks the runs compared, an evaluation none of whose runs is named
    keeping its newest completed run per subject and model. Another family, a
    family-less evaluation or a wrong count is a 422. Declared before
    ``/{evaluation_id}``, which would otherwise read ``compare`` as an id.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluations = [
        await _get_user_evaluation(db, user.id, str(eid), user_group_ids, is_admin=user.is_admin)
        for eid in _id_list(ids, "Evaluation ID")
    ]
    try:
        return await compare_evaluations(db, evaluations, _id_list(run_ids, "Run ID"))
    except ComparisonRefusedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/bundle-review", response_model=BundleReviewResponse)
async def get_bundle_review(
    result_ids: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """One STIX entry's expected bundle against 1 to 32 answers, object by object
    (``services/evaluation/bundle_review.py``).

    Every result's evaluation must be visible to the caller (else 404); answers of another
    family or scored against another expected bundle are a 422. Declared before
    ``/{evaluation_id}``.
    """
    rids = _id_list(result_ids, "Result ID")
    await _assert_results_visible(db, user, rids)
    try:
        return await load_bundle_review(db, rids)
    except LookupError:
        raise HTTPException(status_code=404, detail="Evaluation result not found") from None
    except BundleReviewRefusedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


async def _assert_results_visible(db: AsyncSession, user: User, result_ids: list[uuid.UUID]) -> None:
    """404 unless every result's evaluation is visible to *user*."""
    user_group_ids = await get_user_group_ids(db, user.id)
    owners = await db.execute(
        select(EvaluationRun.evaluation_id)
        .join(EvaluationResult, EvaluationResult.run_id == EvaluationRun.id)
        .where(EvaluationResult.id.in_(result_ids))
    )
    for evaluation_id in {row[0] for row in owners.all()}:
        await _get_user_evaluation(db, user.id, str(evaluation_id), user_group_ids, is_admin=user.is_admin)


@router.post("/bundle-reviews", response_model=list[BundleReviewResponse])
async def get_bundle_reviews(
    body: BundleReviewsRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Several entries' bundle reviews at once (``load_bundle_reviews``), one group of 1 to
    32 result ids per entry, without the report text unless ``include_text``: what a run's
    review reads over every entry (its figures, the causes of its misses, its history).
    A read: POST only to carry the groups."""
    groups = [_id_list(",".join(group), "Result ID") for group in body.groups]
    await _assert_results_visible(db, user, [rid for group in groups for rid in group])
    try:
        return await load_bundle_reviews(db, groups, include_text=body.include_text)
    except LookupError:
        raise HTTPException(status_code=404, detail="Evaluation result not found") from None
    except BundleReviewRefusedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/{evaluation_id}/metric-catalog", response_model=list[MetricCatalogEntry])
async def get_evaluation_metric_catalog(
    evaluation_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Metric keys selectable for this evaluation's family.

    Read from the package's declared metrics, not from stored
    ``evaluation_metrics`` rows: the author chooses what a future run composes
    from, so a metric must be selectable before it was ever computed.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    return metric_catalog(evaluation.family)


@router.get("/{evaluation_id}/available-models", response_model=list[AvailableModelItem])
async def list_evaluation_available_models(
    evaluation_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """The chat models this evaluation's owner may use: what a save and a launch accept.

    Not the caller's (`/settings/available-models`): an admin editing another
    user's evaluation would be offered models its save refuses (a run's model is
    decided on the owner's providers).
    """
    from app.llm.router import get_available_models, is_chat_capable_model
    from app.services.evaluation_engine import evaluation_owner_id, owner_model_routing

    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    routing = await owner_model_routing(db, evaluation_owner_id(evaluation))
    return [
        AvailableModelItem(**m)
        for m in await get_available_models(db, routing=routing)
        if is_chat_capable_model(m.get("model"))
    ]


@router.get("/agents/all")
async def list_all_agents_for_evaluations(
    search: str | None = Query(None, max_length=200),
    limit: int = Query(10, ge=1, le=50),
    exclude_ids: str | None = Query(
        None, description="Comma-separated agent IDs to exclude"
    ),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Search the agents the caller may evaluate, for the evaluation agent pickers.

    The caller's three tiers, with no admin bypass (``subjects_hidden_from``):
    their own agents, those shared with them and company-managed ones.
    Results are limited (default 10, max 50) and can be filtered by a search
    term and an exclusion list.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    query = select(
        Agent.id,
        Agent.name,
        Agent.slug,
        Agent.icon,
        Agent.is_company_managed,
        Agent.enabled,
    ).where(
        Agent.enabled.is_(True),
        hidden_rows_excluded(Agent),
        user_visible_filter(Agent, user.id, user_group_ids),
    )

    if search:
        q = f"%{search.strip()}%"
        query = query.where(
            or_(
                Agent.name.ilike(q),
                Agent.slug.ilike(q),
                Agent.description.ilike(q),
            )
        )

    if exclude_ids:
        ids_to_exclude = []
        for raw in exclude_ids.split(","):
            raw = raw.strip()
            if raw:
                ids_to_exclude.append(parse_uuid(raw, label="Exclude Agent ID"))
        if ids_to_exclude:
            query = query.where(Agent.id.notin_(ids_to_exclude))

    query = query.order_by(Agent.name).limit(limit)

    result = await db.execute(query)
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "slug": r.slug,
            "icon": r.icon,
            "is_company_managed": r.is_company_managed,
            "enabled": r.enabled,
        }
        for r in result
    ]


@router.get("", response_model=list[EvaluationResponse])
async def list_evaluations(
    agent_id: str | None = Query(None),
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List evaluations visible to the user, optionally filtered by agent.

    ``scope=all`` lists every evaluation (what a detail page links to, e.g.
    the evaluations of one agent).
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    query = user_visible_query(
        Evaluation, user.id, user_group_ids, is_admin=list_scope_admin_bypass(scope, user)
    )

    if agent_id:
        aid = parse_uuid(agent_id, label="Agent ID")
        eval_ids_subq = (
            select(EvaluationAgent.evaluation_id)
            .where(EvaluationAgent.agent_id == aid)
            .scalar_subquery()
        )
        query = query.where(Evaluation.id.in_(eval_ids_subq))

    result = await db.execute(query)
    evaluations = cast(list[Evaluation], result.scalars().all())

    from app.services.evaluation_engine import evaluation_owner_id, owner_model_routing

    # Each owner's providers read once, for the evaluations that name a model.
    routing_by_owner: dict[str | None, ProviderRouting] = {}
    responses = []
    for ev in evaluations:
        routing = None
        if ev.model_ids:
            owner_id = evaluation_owner_id(ev)
            if owner_id not in routing_by_owner:
                routing_by_owner[owner_id] = await owner_model_routing(db, owner_id)
            routing = routing_by_owner[owner_id]
        responses.append(
            await _build_evaluation_response(
                db,
                ev,
                caller_can_manage=can_manage(user, ev, user_group_ids),
                owner_routing=routing,
            )
        )
    return responses


@router.post("", response_model=EvaluationResponse, status_code=status.HTTP_201_CREATED)
async def create_evaluation(
    body: CreateEvaluationRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Create a new evaluation with optional agents and linked datasets."""
    _reject_f1_quality_source(body.quality_source)
    settings = _validated_settings(body)
    settings["trigger_config"] = _trigger_config_after(body.trigger_config)
    evaluation = Evaluation(
        user_id=user.id,
        is_company_managed=body.is_company_managed,
        name=body.name,
        enabled=body.enabled,
        **settings,
    )
    await _refuse_unserved_models(db, evaluation, evaluation.model_ids)
    await apply_group_share_fields(db, evaluation, body, partial=False)
    await apply_user_share_fields(db, evaluation, body, partial=False)
    db.add(evaluation)
    await db.flush()

    if body.agent_ids:
        agent_uuids = [parse_uuid(aid_str, label="Agent ID") for aid_str in body.agent_ids]
        await _refuse_hidden_subjects(db, user.id, agent_ids=agent_uuids)
        for aid in agent_uuids:
            db.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=aid))

    # Per-agent prediction extractors, validated against the family and
    # the linked agents before anything is written, so one bad chain leaves the
    # whole request unpersisted.
    if body.agent_extractors:
        # Read from the linked rows, not the request, so this check and the
        # update path share one notion of "known agent". The SELECT autoflushes
        # the pending rows.
        extractor_err = validate_agent_extractors(
            body.agent_extractors,
            family=body.family,
            known_agent_ids={
                str(row.agent_id)
                for row in await load_evaluation_agents(db, evaluation.id)
            },
        )
        if extractor_err:
            raise HTTPException(status_code=422, detail=extractor_err)
        await apply_agent_extractors(db, evaluation.id, body.agent_extractors)

    if body.dataset_ids:
        for pos, did_str in enumerate(body.dataset_ids):
            did = parse_uuid(did_str, label="Dataset ID")
            ds_result = await db.execute(select(Dataset).where(Dataset.id == did))
            ds = ds_result.scalar_one_or_none()
            if ds:
                db.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=did, position=pos))
        # The link route's rule, so a dataset of another family is refused
        # here too rather than linked and only reported. The SELECT
        # autoflushes the pending links; refusing leaves the request unpersisted.
        conflicts = await _evaluation_family_conflicts(db, evaluation)
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail=" ".join(f"{c.dataset_name}: {c.message}" for c in conflicts),
            )

    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="evaluation",
        entity_id=str(evaluation.id),
        entity_name=evaluation.name,
        summary=f"Created evaluation '{evaluation.name}'",
    )
    await db.commit()

    # The caller owns the row it just created: no group grant can change that,
    # so no group ids are read.
    return await _build_evaluation_response(db, evaluation, caller_can_manage=can_manage(user, evaluation))


@router.post("/{evaluation_id}/copy", response_model=EvaluationResponse, status_code=status.HTTP_201_CREATED)
async def copy_evaluation(
    evaluation_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Copy an evaluation: every setting, agent and dataset link, no trigger, run or result."""
    user_group_ids = await get_user_group_ids(db, user.id)
    source = await _get_user_evaluation(db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin)

    settings = evaluation_settings(source)
    settings.update(
        execution_mode=_runnable_execution_mode(source.execution_mode),
        run_mode=_runnable_run_mode(source.run_mode),
    )
    # Checked as a create: a value stored before its rule existed is refused,
    # naming the field, never copied.
    settings = _settings_checked_as_created(settings)
    # No trigger (FR-023): a copy starts nothing on its own until it is given one.
    settings["trigger_config"] = {}
    copy = Evaluation(
        user_id=user.id,
        is_company_managed=False,
        shared_group_ids=None,
        name=fit_copy_name(source.name, Evaluation.__table__.c.name, prefix="Copy of "),
        enabled=True,
        **settings,
    )
    db.add(copy)
    await db.flush()

    # Each agent with its extractor, tags and baseline flag in this evaluation,
    # and each flow subject, which takes no extractor.
    agent_links = await load_evaluation_agents(db, source.id)
    flow_links = list(
        (
            await db.execute(
                select(EvaluationAgent).where(
                    EvaluationAgent.evaluation_id == source.id, EvaluationAgent.flow_id.is_not(None)
                )
            )
        ).scalars()
    )
    # The copy runs as its new owner: every subject must be one they may evaluate.
    await _refuse_hidden_subjects(
        db,
        user.id,
        agent_ids=[link.agent_id for link in agent_links if link.agent_id is not None],
        flow_ids=[link.flow_id for link in flow_links if link.flow_id is not None],
    )
    for link in agent_links:
        db.add(
            EvaluationAgent(
                evaluation_id=copy.id, agent_id=link.agent_id, **evaluation_agent_settings(link)
            )
        )
    for flow_link in flow_links:
        db.add(EvaluationAgent(evaluation_id=copy.id, flow_id=flow_link.flow_id, **evaluation_agent_settings(flow_link)))

    ds_rows_result = await db.execute(
        select(EvaluationDataset)
        .where(EvaluationDataset.evaluation_id == source.id)
        .order_by(EvaluationDataset.position)
    )
    for ds_link in ds_rows_result.scalars():
        ds_result = await db.execute(select(Dataset).where(Dataset.id == ds_link.dataset_id))
        ds = ds_result.scalar_one_or_none()
        if ds:
            db.add(EvaluationDataset(evaluation_id=copy.id, dataset_id=ds_link.dataset_id, position=ds_link.position))

    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="evaluation",
        entity_id=str(copy.id),
        entity_name=copy.name,
        summary=f"Copied evaluation '{source.name}' as '{copy.name}'",
    )
    await db.commit()

    # The caller owns the row it just created: no group grant can change that,
    # so no group ids are read.
    response = await _build_evaluation_response(db, copy, caller_can_manage=can_manage(user, copy))
    response["warnings"] = _unavailable_model_warnings(response)
    return response


@router.get("/{evaluation_id}/export")
async def export_evaluation(
    evaluation_id: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Export an evaluation as a ZIP package with structured dependencies."""
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )

    package = await _build_evaluation_export_bundle(db, evaluation, user, request)
    definition = package["definition"]
    agents = package["agents"]
    dataset_ids_by_index = package["dataset_ids_by_index"]
    run_result_files = package["run_result_files"]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "evaluation.json",
            json.dumps(definition, ensure_ascii=False, indent=2),
        )

        for idx, agent_bundle in enumerate(agents):
            zf.writestr(
                f"dependencies/agents/agent-{idx}.json",
                json.dumps(agent_bundle, ensure_ascii=False, indent=2),
            )

        for dataset_index, dataset_id in dataset_ids_by_index.items():
            dataset_export_response = await export_dataset(
                dataset_id=dataset_id,
                user=user,
                db=db,
            )
            dataset_zip_bytes = await _streaming_response_to_bytes(
                dataset_export_response
            )
            zf.writestr(
                f"dependencies/datasets/dataset-{dataset_index}.zip",
                dataset_zip_bytes,
            )

        for run_file in run_result_files:
            zf.writestr(
                run_file["path"],
                json.dumps(run_file["payload"], ensure_ascii=False, indent=2),
            )

    buf.seek(0)
    safe_name = "".join(
        c.lower() if c.isalnum() or c in "-_" else "-" for c in evaluation.name
    ).strip("-") or "evaluation"

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": build_content_disposition(
                f"evaluation-{safe_name}.zip"
            )
        },
    )


#: The modes a run records: an evaluation's two, and ``skip_llm`` for a
#: model-independent agent.
_RUN_EXECUTION_MODES = (*EVALUATION_EXECUTION_MODES, "skip_llm")


def _runnable_execution_mode(stored: str | None) -> str:
    """The mode a stored ``execution_mode`` runs in, as a copy and an export write it.

    A value that is neither mode runs as LLM Only. Written as such, a copy stays
    editable and an export imports, since both refuse any other value.
    """
    return stored if stored in EVALUATION_EXECUTION_MODES else "llm_only"


def _runnable_run_mode(stored: str | None) -> str:
    """The mode a stored ``run_mode`` runs in, as a copy and an export write it.

    Any value but ``sequential`` runs in parallel. Written as such, a copy and
    an import accept it, since both refuse any other value.
    """
    return stored if stored in EVALUATION_RUN_MODES else "parallel"


def _imported_execution_mode(evaluation_data: object) -> str:
    """The mode of an imported evaluation: Full Loop when absent, a 422 for any other value."""
    mode = evaluation_data.get("execution_mode") if isinstance(evaluation_data, dict) else None
    if mode is None:
        return "full_loop"
    if mode not in EVALUATION_EXECUTION_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"execution_mode must be 'llm_only' or 'full_loop', not {str(mode)[:40]!r}",
        )
    return mode


def _validated_settings(body: CreateEvaluationRequest) -> dict:
    """Every setting of *body*, checked and normalised as create stores them.

    Create, import and copy all call it, so a package or a copied row is
    refused as a request would be (FR-114). The trigger config is left to the
    caller: create encrypts the secret it is sent, an import and a copy have none.
    """
    bad_scorers = invalid_keys(body.scorer_selection)
    if bad_scorers:
        raise HTTPException(status_code=422, detail=f"Unknown scorer(s): {', '.join(bad_scorers)}")
    _validate_family(body.family)
    override_err = validate_field_scoring_overrides(body.field_scoring_overrides)
    if override_err:
        raise HTTPException(status_code=422, detail=override_err)
    model_ids, disabled_model_ids = _normalize_model_config(body.model_ids, body.disabled_model_ids)
    settings = {field: getattr(body, field) for field in EVALUATION_SETTINGS}
    settings.update(
        model_ids=model_ids,
        disabled_model_ids=disabled_model_ids,
        field_scoring_overrides=normalize_field_scoring_overrides(body.field_scoring_overrides),
        tags=strip_reserved_keys(body.tags),
        placebo_config=body.placebo_config.model_dump(mode="json") if body.placebo_config else None,
        sub_score_weights=body.sub_score_weights.stored() if body.sub_score_weights else None,
    )
    return settings


def _imported_settings(evaluation_data: dict, execution_mode: str) -> dict:
    """Every setting of an imported evaluation, refused as create refuses it.

    Validated by create's own schema and checks (FR-114), before anything is
    imported, so a malformed value is a 422 naming the field and leaves nothing
    behind. A missing key, or ``null`` on a setting that cannot be null, takes
    create's default: an older package names fewer settings.
    """
    raw = {
        field: evaluation_data[field]
        for field in EVALUATION_SETTINGS
        if field in evaluation_data and (evaluation_data[field] is not None or field in CLEARED_BY_NULL)
    }
    if "performance_tolerance" not in raw and evaluation_data.get("speed_tolerance") is not None:
        # Older bundles name this field `speed_tolerance`, with the
        # same meaning; honour it rather than reset to 5.0.
        raw["performance_tolerance"] = evaluation_data["speed_tolerance"]
    raw.update(
        execution_mode=execution_mode,
        # An imported evaluation has no resume secret: set one to take callbacks.
        trigger_config=without_resume_secret(evaluation_data.get("trigger_config")),
        # Only a JSON ``true`` turns the guard on, and the tool approval:
        # a package without the setting imports off.
        apply_prompt_guards=evaluation_data.get("apply_prompt_guards") is True,
        require_tool_approval=evaluation_data.get("require_tool_approval") is True,
    )
    return _settings_checked_as_created(raw)


def _settings_checked_as_created(raw: dict) -> dict:
    """*raw* settings, checked and normalised by create's own schema and checks.

    Import and copy both call it, so neither stores a value a create would
    refuse: that value is a 422 naming the field. A missing key, or ``null``
    on a setting that cannot be null, takes create's default.
    """
    raw = {field: value for field, value in raw.items() if value is not None or field in CLEARED_BY_NULL}
    try:
        # The name is the caller's: a placeholder only satisfies the schema.
        body = CreateEvaluationRequest.model_validate({**raw, "name": "Checked Evaluation"})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=validation_detail(exc, root="evaluation")) from None
    settings = _validated_settings(body)
    settings["trigger_config"] = body.trigger_config or {}
    return settings


def _imported_agent_settings(agent_deps: list[dict], family: str | None) -> list[dict]:
    """Each embedded agent's settings in the evaluation, in *agent_deps* order.

    Checked before anything is imported, as an update checks them: the shape by
    ``ImportedAgentSettings``, the extractor against the family. Only the first
    agent marked baseline is, as an evaluation has at most one.
    """
    links: list[ImportedAgentSettings] = []
    for position, dep in enumerate(agent_deps):
        try:
            links.append(ImportedAgentSettings.model_validate(dep))
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=f"agents[{position}]: {validation_detail(exc, root="evaluation")}"
            ) from None
    extractors = {
        str(position): link.output_extractor
        for position, link in enumerate(links)
        if link.output_extractor is not None
    }
    extractor_err = validate_agent_extractors(
        extractors, family=family, known_agent_ids=set(extractors)
    )
    if extractor_err:
        raise HTTPException(status_code=422, detail=extractor_err)
    settings: list[dict] = []
    baseline_taken = False
    for link in links:
        is_baseline = link.is_baseline and not baseline_taken
        baseline_taken = baseline_taken or is_baseline
        settings.append(
            {
                "tags": strip_reserved_keys(link.tags),
                "is_baseline": is_baseline,
                "output_extractor": as_extractor_dict(link.output_extractor),
            }
        )
    return settings


def _importable_run_status(status: object) -> str:
    """Map an exported run status onto one an import may legally create.

    An allowlist, not a denylist: the trust boundary for an uploaded
    bundle. Three cases:

    * **Absent** (missing, ``None``, empty): never recorded, so ``"completed"``,
      the default for such packages.
    * **Present but not a string**: structurally invalid, so it fails closed;
      ``"completed"`` would make a successful-looking run out of garbage.
    * **Active or unrecognised**: ``"failed"``. Importing an in-flight run
      cannot revive the execution that drove it, and an unknown value would be
      a 500 (``DataError`` on the ``String(20)`` column) or a status the state
      machine never produces.
    """
    if status is None or status == "":
        return "completed"
    if not isinstance(status, str):
        return "failed"
    return status if status in TERMINAL_RUN_STATUSES else "failed"


#: An evaluation package's limits, a dataset package's: its upload, and what
#: its members expand to once uncompressed, which the import holds while it
#: checks the package whole. Each nested dataset package is held to them too.
_MAX_PACKAGE_BYTES = MAX_ZIP_SIZE
_MAX_PACKAGE_UNCOMPRESSED_BYTES = MAX_UNCOMPRESSED_SIZE
#: What ``evaluation.json`` or an agent bundle may expand to: each is parsed whole, holding
#: the GIL while it is, and a native agent bundle is imported up to 10 MB on its own.
_MAX_PACKAGE_JSON_MEMBER_BYTES = 32 * 1024 * 1024  # 32 MB
#: What a run's results may expand to: the one member that carries traces.
_MAX_PACKAGE_RESULTS_MEMBER_BYTES = MAX_JSON_MEMBER_SIZE


def _package_part[PartT: BaseModel](model: type[PartT], data: object, source: str) -> PartT:
    """*data*, read from the package's *source* file, checked against its shape.

    A 400 naming the file and each field at fault, before anything is written.
    """
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail=f"Invalid {source}: not a JSON object")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {source}: {validation_detail(exc, root="evaluation")}") from None


@router.post("/import", response_model=EvaluationResponse, status_code=status.HTTP_201_CREATED)
async def import_evaluation(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Import an evaluation ZIP package with its agents, datasets and runs.

    In one transaction: the package is read and checked whole before
    anything is written, and a failure after that rolls back everything the
    import wrote, its agents and datasets included, and deletes the files its
    datasets stored. A package that fails halfway leaves nothing behind. The
    datasets are imported first, so the shared lock an agent import takes on the
    provider configs (``create_agent_from_bundle``) is not held during their
    uploads; it is held until the commit, which a provider deletion waits for.
    """
    raw = await file.read(_MAX_PACKAGE_BYTES + 1)
    if len(raw) > _MAX_PACKAGE_BYTES:
        raise HTTPException(
            status_code=413, detail=f"The package exceeds {_MAX_PACKAGE_BYTES // 2**20} MB"
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as err:
        raise HTTPException(status_code=400, detail="Invalid ZIP file") from err
    # What the members declare: an early refusal, not a bound (whoever built the
    # archive sets it). The bound is ``budget``, spent on what each member really
    # expands to.
    if sum(member.file_size for member in zf.infolist()) > _MAX_PACKAGE_UNCOMPRESSED_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"The package exceeds {_MAX_PACKAGE_UNCOMPRESSED_BYTES // 2**20} MB uncompressed",
        )
    budget = ArchiveBudget(_MAX_PACKAGE_UNCOMPRESSED_BYTES)

    async def _read_member(name: str, *, max_member: int | None = None) -> bytes:
        """*name*'s bytes, decompressed off the event loop within the package's budget,
        and within *max_member* for a JSON member."""
        try:
            return await asyncio.to_thread(budget.read, zf, name, max_member=max_member)
        except ArchiveTooLargeError as err:
            raise HTTPException(
                status_code=413,
                detail=f"The package is too large once uncompressed: {err}",
            ) from err
        except zipfile.BadZipFile as err:
            raise HTTPException(status_code=400, detail="Invalid ZIP file") from err

    normalized_members: dict[str, str] = {
        name.lstrip("./"): name for name in zf.namelist()
    }

    def _resolve_zip_member(path: str | None) -> str | None:
        """Resolve a zip member path, tolerating a wrapping folder.

        Accepts an exact match, a ``./``-prefixed variant, and a suffix match
        when the archive is wrapped in a top-level directory.
        """
        if not path:
            return None
        normalized_path = path.lstrip("./")
        if normalized_path in normalized_members:
            return normalized_members[normalized_path]

        suffix = f"/{normalized_path}"
        matches = [
            real_name
            for norm_name, real_name in normalized_members.items()
            if norm_name.endswith(suffix)
        ]
        if not matches:
            return None
        # Prefer the shallowest path when multiple matches exist.
        return sorted(matches, key=lambda p: p.count("/"))[0]

    definition_path = _resolve_zip_member("evaluation.json")
    if not definition_path:
        raise HTTPException(
            status_code=400,
            detail="ZIP missing evaluation.json",
        )

    definition_raw = await _read_member(definition_path, max_member=_MAX_PACKAGE_JSON_MEMBER_BYTES)
    try:
        definition = await asyncio.to_thread(json.loads, definition_raw)
    except Exception as err:
        raise HTTPException(
            status_code=400,
            detail="Invalid evaluation.json",
        ) from err

    package = _package_part(EvaluationPackage, definition, "evaluation.json")
    if package.version != 1:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported export version: {package.version}",
        )
    if package.export_type != "evaluation_package":
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported export type: "
                f"{package.export_type}"
            ),
        )
    # The settings are checked before anything is written.
    # Absent, the mode is Full Loop.
    evaluation_data = (
        package.evaluation.model_dump(exclude_unset=True) if package.evaluation else {}
    )
    imported_execution_mode = _imported_execution_mode(evaluation_data)
    # A ``skill`` placebo target names a skill of the exporting instance: the
    # agents' skills are recreated under new ids, so it is dropped, and said -
    # before the settings are checked, so a configuration valid without it is.
    # A malformed one is left as sent, for the settings check to name.
    dropped_placebo_targets: list[str] = []
    try:
        packaged_placebo = parse_placebo_config(evaluation_data.get("placebo_config"))
    except ValidationError:
        packaged_placebo = None
    if packaged_placebo is not None:
        kept_placebo, dropped_placebo_targets = imported_placebo_config(packaged_placebo)
        evaluation_data["placebo_config"] = kept_placebo.model_dump(mode="json") if kept_placebo else None
    imported_settings = _imported_settings(evaluation_data, imported_execution_mode)

    dependencies = package.dependencies or PackagedDependencies()
    agent_deps = sorted(dependencies.agents or [], key=lambda d: d.index)
    agent_settings = _imported_agent_settings(
        [d.model_dump() for d in agent_deps], imported_settings["family"]
    )
    # Each embedded agent bundle is read and created as the agent import does it.
    from app.api.v1.agent_import import create_agent_from_bundle
    from app.api.v1.schemas.agent import ImportAgentRequest
    from app.services.agentschema.detect import NATIVE_FORMAT

    agent_bundles: list[dict] = []
    for agent_dep in agent_deps:
        resolved_agent_path = _resolve_zip_member(agent_dep.path)
        if not resolved_agent_path:
            raise HTTPException(
                status_code=400,
                detail=f"Missing embedded agent bundle: {agent_dep.path}",
            )
        agent_bundle_raw = await _read_member(resolved_agent_path, max_member=_MAX_PACKAGE_JSON_MEMBER_BYTES)
        try:
            agent_bundle = await asyncio.to_thread(json.loads, agent_bundle_raw)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid embedded agent bundle JSON: {agent_dep.path}",
            ) from exc

        try:
            agent_bundles.append(ImportAgentRequest(**agent_bundle).model_dump())
        except Exception as exc:  # pragma: no cover - defensive parsing
            raise HTTPException(
                status_code=400,
                detail=f"Invalid embedded agent bundle: {exc}",
            ) from exc

    # Embedded datasets use the dataset import format, read when imported.
    dataset_deps = sorted(dependencies.datasets or [], key=lambda d: d.index)
    dataset_paths: list[str] = []
    for dataset_dep in dataset_deps:
        resolved_dataset_path = _resolve_zip_member(dataset_dep.path)
        if not resolved_dataset_path:
            raise HTTPException(
                status_code=400,
                detail=f"Missing embedded dataset bundle: {dataset_dep.path}",
            )
        dataset_paths.append(resolved_dataset_path)

    run_definitions = (
        package.evaluation_runs if package.evaluation_runs is not None else package.runs or []
    )
    run_results: list[list[PackagedResult]] = []
    for run_bundle in run_definitions:
        result_items: list[PackagedResult] | None = None
        resolved_results_path = _resolve_zip_member(run_bundle.results_path)
        if resolved_results_path:
            run_results_raw = await _read_member(
                resolved_results_path, max_member=_MAX_PACKAGE_RESULTS_MEMBER_BYTES
            )
            try:
                run_results_payload = await asyncio.to_thread(json.loads, run_results_raw)
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid {run_bundle.results_path}",
                ) from exc
            result_items = _package_part(
                PackagedRunResults, run_results_payload, str(run_bundle.results_path)
            ).results
        # Older packages carry the results inline.
        run_results.append(result_items or run_bundle.results or [])

    # The entry positions the results name, keyed like ``entry_id_by_ref`` below by
    # the dataset dependency's ``index`` (the export's enumerate order), across
    # every run: a tie on one of them loses which entry the result answered, and
    # is refused.
    positions_in_use: dict[int, set[int]] = {}
    for result_items in run_results:
        for result_bundle in result_items:
            if result_bundle.entry_ref is not None:
                positions_in_use.setdefault(result_bundle.entry_ref.dataset_index, set()).add(
                    result_bundle.entry_ref.entry_position
                )

    async with objects_deleted_on_failure(db) as stored_objects:
        imported_dataset_ids_by_index: dict[int, uuid.UUID] = {}
        entry_id_by_ref: dict[tuple[int, int], uuid.UUID] = {}
        for dataset_dep, dataset_path in zip(dataset_deps, dataset_paths, strict=True):
            try:
                imported = await import_dataset_package(
                    db,
                    await _read_member(dataset_path),
                    user,
                    stored_objects,
                    positions_in_use=positions_in_use.get(dataset_dep.index, set()),
                    # One budget for the whole import: a nested package spends the evaluation's.
                    budget=budget,
                )
            except HTTPException as exc:
                # Named like the evaluation's own files: a package holds several.
                raise HTTPException(
                    status_code=exc.status_code, detail=f"{dataset_dep.path}: {exc.detail}"
                ) from exc
            imported_dataset_ids_by_index[dataset_dep.index] = imported.dataset.id
            for position, entry_id in imported.entry_ids_by_position.items():
                entry_id_by_ref[(dataset_dep.index, position)] = entry_id

        # A run names its agent by the agent's ``index`` in the package.
        imported_agent_ids_by_index: dict[int, uuid.UUID] = {}
        for agent_dep, agent_bundle in zip(agent_deps, agent_bundles, strict=True):
            imported_agent = await create_agent_from_bundle(
                db,
                user=user,
                bundle=agent_bundle,
                request=request,
                source_format=NATIVE_FORMAT,
            )
            imported_agent_ids_by_index[agent_dep.index] = imported_agent.agent.id

        # The imported evaluation is private, owned by the current user.
        resolved_name = await _resolve_imported_evaluation_name(
            db,
            evaluation_data.get("name") or "Imported Evaluation",
        )

        evaluation = Evaluation(
            user_id=user.id,
            is_company_managed=False,
            shared_group_ids=None,
            name=resolved_name,
            **imported_settings,
            # Automatic runs paused: the package carries the source
            # instance's schedule, which must not fire here until an admin says so.
            enabled=False,
            last_triggered_at=_parse_optional_datetime(
                evaluation_data.get("last_triggered_at")
            ),
        )
        db.add(evaluation)
        await db.flush()

        # Imported agents are bound in the bundle's order, each with its settings.
        for agent_dep, settings in zip(agent_deps, agent_settings, strict=True):
            db.add(
                EvaluationAgent(
                    evaluation_id=evaluation.id,
                    agent_id=imported_agent_ids_by_index[agent_dep.index],
                    **settings,
                )
            )

        # Imported datasets keep their declared positions.
        for dataset_dep in dataset_deps:
            db.add(
                EvaluationDataset(
                    evaluation_id=evaluation.id,
                    dataset_id=imported_dataset_ids_by_index[dataset_dep.index],
                    position=(
                        dataset_dep.index if dataset_dep.position is None else dataset_dep.position
                    ),
                )
            )

        for run_index, (run_bundle, result_items) in enumerate(
            zip(run_definitions, run_results, strict=True)
        ):
            run_agent_id = imported_agent_ids_by_index.get(run_bundle.agent_index)
            if run_agent_id is None:
                continue

            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=run_agent_id,
                model_id=run_bundle.model_id or "unknown-model",
                is_default_model=bool(run_bundle.is_default_model),
                # An active status becomes "failed" on import. No
                # execution stands behind an imported run, so a "pending" or
                # "running" row would stay active with nothing to dispatch, finish
                # or clean it up, and its NULL owner would make it look
                # like an unowned row awaiting a claim.
                status=_importable_run_status(run_bundle.status),
                trigger_reason=run_bundle.trigger_reason or "import",
                # The mode the run recorded; anything else is left unset,
                # and the run's label is then read from its results.
                execution_mode=(
                    run_bundle.execution_mode
                    if run_bundle.execution_mode in _RUN_EXECUTION_MODES
                    else None
                ),
                # What the run applied; an absent value, or one that is not
                # a JSON boolean, leaves it unknown.
                apply_prompt_guards=(
                    run_bundle.apply_prompt_guards
                    if isinstance(run_bundle.apply_prompt_guards, bool)
                    else None
                ),
                require_tool_approval=(
                    run_bundle.require_tool_approval
                    if isinstance(run_bundle.require_tool_approval, bool)
                    else None
                ),
                # The scorers the run was scored with, which its judge
                # headline is computed from below. Absent (an older package), the
                # judge counts as on, as for a run recorded before the column.
                scorer_selection=run_bundle.scorer_selection,
                metric_suite_version=run_bundle.metric_suite_version,
                persona_version_number=run_bundle.persona_version_number,
                prompt_versions=run_bundle.prompt_versions,
                agent_config_versions=run_bundle.agent_config_versions,
                skill_versions=run_bundle.skill_versions,
                score=run_bundle.score,
                speed_score=run_bundle.speed_score,
                overall_score=run_bundle.overall_score,
                f1_score=run_bundle.f1_score,
                duration_seconds=run_bundle.duration_seconds,
                completed_entries=run_bundle.completed_entries or 0,
                total_entries=run_bundle.total_entries or 0,
                error=run_bundle.error,
                started_at=_parse_optional_datetime(run_bundle.started_at),
                completed_at=_parse_optional_datetime(run_bundle.completed_at),
                created_at=_parse_optional_datetime(run_bundle.created_at)
                or datetime.now(UTC),
            )
            db.add(run)
            await db.flush()

            # Kept so the metric rows below can be written once the results have
            # ids, with the provenance that decides what Run Duration means for
            # each.
            imported_results: list[tuple[EvaluationResult, bool]] = []

            for result_index, result_bundle in enumerate(result_items):
                # A result with a null ``entry_ref`` counts toward its run's headlines
                # as on the source, where its entry was deleted or unlinked.
                # A ref naming no entry of the package was never exported: it is a
                # 400, never guessed into an entry-less result.
                entry_ref = result_bundle.entry_ref
                mapped_entry_id = None
                if entry_ref is not None:
                    mapped_entry_id = entry_id_by_ref.get(
                        (entry_ref.dataset_index, entry_ref.entry_position)
                    )
                    if mapped_entry_id is None:
                        source, field = (
                            (run_bundle.results_path, f"results.{result_index}.entry_ref")
                            if run_bundle.results_path
                            else (
                                "evaluation.json",
                                f"evaluation_runs.{run_index}.results.{result_index}.entry_ref",
                            )
                        )
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"Invalid {source}: {field}: names no entry of the package "
                                f"(dataset {entry_ref.dataset_index}, "
                                f"position {entry_ref.entry_position})"
                            ),
                        )

                if result_bundle.score is None:
                    continue

                # Read once and used on the row and on the metric call below, so
                # the two cannot disagree. Optional: a package without
                # provenance defaults to a fresh execution, which is what such a
                # package claimed anyway.
                #
                # ``is True``, not ``bool(...)``: ``bool("false")`` is True, so a
                # string would mark an executed result as reused and record its Run
                # Duration as the judge time alone, claiming no agent work happened.
                # The classification flags keep ``bool(...)``: for them a malformed
                # truthy value excludes the result from the headline, the safe
                # direction.
                result_was_reused = result_bundle.is_reused is True

                result = EvaluationResult(
                    run_id=run.id,
                    entry_id=mapped_entry_id,
                    agent_output=result_bundle.agent_output or "",
                    score=result_bundle.score,
                    sub_scores=result_bundle.sub_scores or {},
                    evaluator_reasoning=result_bundle.evaluator_reasoning,
                    duration_seconds=result_bundle.duration_seconds,
                    speed_score=result_bundle.speed_score,
                    overall_score=result_bundle.overall_score,
                    f1_score=result_bundle.f1_score,
                    # Optional: a package without these imports every
                    # result as valid. An import reproduces what was exported, so
                    # nothing is re-derived from ``agent_output``: that could
                    # recover anomalies but never ``is_judge_failure``, and would
                    # let this instance's detector overrule the exporting one.
                    is_judge_failure=bool(result_bundle.is_judge_failure),
                    execution_anomaly_kind=result_bundle.execution_anomaly_kind,
                    is_reused=result_was_reused,
                    execution_details=result_bundle.execution_details,
                    created_at=_parse_optional_datetime(result_bundle.created_at)
                    or datetime.now(UTC),
                )
                db.add(result)
                imported_results.append((result, result_was_reused))

            # The metric rows, derived from the package's
            # ``execution_details.metric_results``: without them the per-result
            # metrics endpoint and ``get_run_aggregates`` read a blank for an
            # imported evaluation. Derived rather than exported as rows, which
            # still reproduces: ``extract_metrics`` copies every ``scored`` entry
            # and the two durations verbatim, so there is no second source of
            # truth. ``is_reused`` is passed, not defaulted: it selects the judge
            # time over the total for Run Duration.
            #
            # In a savepoint, because ``try``/``except`` alone does not hold:
            # derived rows must never cost the package, but Postgres aborts the
            # whole transaction on a failed statement, and the ``log_audit`` and
            # ``commit`` below would roll the import back. It is reachable: a key
            # longer than ``String(100)`` is rejected on flush, and
            # ``_upsert_metric``'s SELECT autoflushes part-way through. The results
            # are flushed outside the savepoint, so they survive.
            #
            # One savepoint per run, not per result: a rejected row costs the whole
            # run its metrics. Per-result failures are refused before the write
            # (``extract_metrics`` for a non-finite value, ``_is_storable_key`` for
            # a key the table cannot hold). A savepoint per result would cross
            # PostgreSQL's 64 cached subtransactions on any import of a few hundred
            # results. This ``except`` is for the failure nobody enumerated.
            if imported_results:
                await db.flush()
                try:
                    async with db.begin_nested():
                        for imported_result, imported_was_reused in imported_results:
                            await log_result_metrics(
                                db,
                                result_id=imported_result.id,
                                run_id=run.id,
                                execution_details=imported_result.execution_details,
                                is_reused=imported_was_reused,
                            )
                        # Inside the savepoint, so a rejected row fails here and is
                        # undone by it, not at the outer commit.
                        await db.flush()
                except Exception as metric_err:
                    logger.warning(
                        "Imported run %s: failed to write metric rows: %s",
                        run.id,
                        metric_err,
                    )

        await log_audit(
            db,
            user=user,
            action="import",
            entity_type="evaluation",
            entity_id=str(evaluation.id),
            entity_name=evaluation.name,
            summary=f"Imported evaluation '{evaluation.name}'",
            details={
                "agent_count": len(imported_agent_ids_by_index),
                "dataset_count": len(imported_dataset_ids_by_index),
                "run_count": len(run_definitions),
                "placebo_targets_dropped": dropped_placebo_targets,
            },
            request=request,
        )

        # The headlines of every imported run (FR-116), from its results and
        # metric rows, as a run's completion computes them: the package carries
        # none. Not committed by the passes, so the commit below is the import's
        # only one and nothing is durable before it. The performance pass's
        # advisory lock is held until then, on an evaluation no one else sees.
        from app.services.evaluation_engine import (
            _compute_run_headlines,
            _recompute_performance_headlines,
        )

        await _compute_run_headlines(
            db, evaluation.id, commit=False, skip_performance_recompute=True
        )
        await _recompute_performance_headlines(db, evaluation.id, evaluation)
        await db.commit()

    # The caller owns the row it just created: no group grant can change that,
    # so no group ids are read.
    response = await _build_evaluation_response(db, evaluation, caller_can_manage=can_manage(user, evaluation))
    response["warnings"] = [
        *(
            f"Placebo target {key} was dropped: it names a skill of the exporting instance"
            for key in dropped_placebo_targets
        ),
        *_unavailable_model_warnings(response),
    ]
    return response


@router.get("/{evaluation_id}", response_model=EvaluationDetailResponse)
async def get_evaluation(
    evaluation_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get evaluation detail with agents and linked datasets."""
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    return await _build_evaluation_response(
        db, evaluation, caller_can_manage=can_manage(user, evaluation, user_group_ids)
    )


# The settings ``update_evaluation`` applies apart from its loop, each with its
# own rule: merged, validated or normalised before they are stored.
_UPDATED_APART: frozenset[str] = frozenset(
    {
        "trigger_config",
        "overall_quality_metrics",
        "overall_performance_metrics",
        "field_scoring_overrides",
        "evaluation_instructions",
        "tags",
        "placebo_config",
        "sub_score_weights",
    }
)


@router.put("/{evaluation_id}", response_model=EvaluationResponse)
async def update_evaluation(
    evaluation_id: str,
    body: UpdateEvaluationRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Update an evaluation."""
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(evaluation, user)

    # What this request changes: a field it sends, ``null`` included for a
    # nullable setting, which an explicit ``null`` clears (the judge model back
    # to the platform default, every field override removed). Omitted is
    # unchanged; ``null`` on a non-nullable field too.
    changed = {
        field
        for field in body.model_fields_set
        if getattr(body, field) is not None or field in CLEARED_BY_NULL
    }

    if "scorer_selection" in changed:
        bad_scorers = invalid_keys(body.scorer_selection)
        if bad_scorers:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown scorer(s): {', '.join(bad_scorers)}",
            )

    # An unknown family is refused; the post-update family is what the
    # extractors are checked against.
    effective_family = body.family if "family" in changed else evaluation.family
    if "family" in changed:
        _validate_family(body.family)
    if body.quality_source is not None:
        _reject_f1_quality_source(body.quality_source)

    # What the stored headlines were composed with: the tolerance band, the
    # spread penalty, and the family, which words the reason a metric is left out.
    # The Configuration tab sends them on every save, so "sent" is not "changed".
    composition_before = (evaluation.performance_tolerance, evaluation.penalty_factor, evaluation.family)
    execution_mode_before = evaluation.execution_mode
    model_ids_before = set(evaluation.model_ids or [])

    # Every setting from the one list, so a new one cannot be skipped
    # here: those below are applied apart, each with its own rule.
    for field in (
        "name",
        "enabled",
        *(field for field in EVALUATION_SETTINGS if field not in _UPDATED_APART),
    ):
        if field in changed:
            setattr(evaluation, field, getattr(body, field))
    if body.trigger_config is not None:
        evaluation.trigger_config = _trigger_config_after(body.trigger_config, evaluation.trigger_config)
    await apply_group_share_fields(
        db, evaluation, body, partial=True, null_keeps_stored=True
    )

    # Headline composition: null reverts a headline to inherit-all, so
    # an explicit null is applied, as for every nullable setting.
    selection_changes: dict[str, dict] = {}
    for field in ("overall_quality_metrics", "overall_performance_metrics"):
        if field not in changed:
            continue
        before = getattr(evaluation, field, None)
        after = getattr(body, field)
        if before != after:
            selection_changes[field] = {"from": before, "to": after}
        setattr(evaluation, field, after)

    # Per-field scoring intent: refuse a contradictory configuration here rather
    # than at run time, where it would surface as a failed run instead of a failed save.
    if "field_scoring_overrides" in changed:
        override_err = validate_field_scoring_overrides(body.field_scoring_overrides)
        if override_err:
            raise HTTPException(status_code=422, detail=override_err)
        evaluation.field_scoring_overrides = normalize_field_scoring_overrides(
            body.field_scoring_overrides
        )

    # Per-agent prediction extractors. A family change re-checks every
    # stored extractor: refusing here beats an orphaned extractor failing every
    # entry of the next run.
    linked_rows = await load_evaluation_agents(db, evaluation.id)
    submitted = body.agent_extractors or {}
    if "family" in changed:
        # Only rows this request does not replace are checked against the new
        # family, so changing the family and fixing the extractors in one update
        # is accepted.
        family_err = validate_existing_extractors_against_family(
            [row for row in linked_rows if str(row.agent_id) not in submitted],
            body.family,
        )
        if family_err:
            raise HTTPException(status_code=422, detail=family_err)
    if body.agent_extractors is not None:
        extractor_err = validate_agent_extractors(
            body.agent_extractors,
            family=effective_family,
            known_agent_ids={str(row.agent_id) for row in linked_rows},
        )
        if extractor_err:
            raise HTTPException(status_code=422, detail=extractor_err)
        await apply_agent_extractors(db, evaluation.id, body.agent_extractors)

    # An empty string clears evaluation_instructions, as null does.
    if "evaluation_instructions" in changed:
        evaluation.evaluation_instructions = body.evaluation_instructions or None

    # Cascading evaluation-level tags: reserved keys stripped; an empty
    # payload or null clears them.
    if "tags" in changed:
        evaluation.tags = strip_reserved_keys(body.tags)

    # The judge's weights, stored as sent: `null` brings back 0.20 each.
    if "sub_score_weights" in changed:
        evaluation.sub_score_weights = body.sub_score_weights.stored() if body.sub_score_weights else None

    # The placebo method: `null` removes it. Its targets are checked
    # against the mode on the row as this update leaves it, when the update
    # sends a configuration or changes the mode: the Configuration tab sends
    # the mode on every save, and an unrelated edit never depends on a stored
    # configuration still validating.
    if "placebo_config" in changed:
        evaluation.placebo_config = (
            body.placebo_config.model_dump(mode="json") if body.placebo_config else None
        )
    if "placebo_config" in changed or evaluation.execution_mode != execution_mode_before:
        try:
            stored_placebo = parse_placebo_config(evaluation.placebo_config)
        except ValidationError:
            raise HTTPException(
                status_code=422,
                detail="The stored placebo configuration is no longer valid: send a new placebo_config or null",
            ) from None
        placebo_error = placebo_mode_error(stored_placebo, evaluation.execution_mode)
        if placebo_error:
            raise HTTPException(status_code=422, detail=placebo_error)

    # Disabled model IDs stay a subset of the configured models.
    if body.model_ids is not None or body.disabled_model_ids is not None:
        model_ids, disabled_model_ids = _normalize_model_config(
            evaluation.model_ids, evaluation.disabled_model_ids
        )
        evaluation.model_ids = model_ids
        evaluation.disabled_model_ids = disabled_model_ids

    # Agents never bind an evaluation by id: there is no reference to sweep when
    # it goes private.
    await handle_company_managed_toggle(
        db,
        user,
        evaluation,
        body.is_company_managed,
        agent_field=None,
        keep_grants=wants_access_restriction(body, evaluation),
    )

    # Per-user sharing, after the company-managed toggle, since
    # ``access_restricted`` is only meaningful relative to it.
    await apply_user_share_fields(db, evaluation, body, partial=True)

    # The models this request adds, on the owner's providers as the update
    # leaves the row.
    await _refuse_unserved_models(
        db, evaluation, [mid for mid in evaluation.model_ids or [] if mid not in model_ids_before]
    )

    # A family change is re-checked against every linked dataset.
    # Conflicts are reported, never resolved: the links are left untouched, as
    # on the dataset side.
    family_conflicts = (
        await _evaluation_family_conflicts(db, evaluation)
        if "family" in changed
        else None
    )

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="evaluation",
        entity_id=str(evaluation.id),
        entity_name=evaluation.name,
        summary=f"Updated evaluation '{evaluation.name}'",
        # A selection change rewrites every headline this evaluation reported,
        # with no run re-executed; without the prior value on record, a score
        # that moved between two readings looks like a defect.
        details={"headline_selection": selection_changes} if selection_changes else None,
    )

    # Re-derive when the composition changes: the tolerance band, the spread
    # penalty (the judge's score and both headlines average with it), the family,
    # or a selection. Each is read-time only, so this recomposes stored values and
    # never re-executes an agent. A save that resends the same values changes
    # nothing to recompute. The retired quality weight composes nothing.
    if (
        composition_before != (evaluation.performance_tolerance, evaluation.penalty_factor, evaluation.family)
        or selection_changes
    ):
        from app.services.evaluation_engine import _compute_run_headlines

        await _compute_run_headlines(db, evaluation.id)

    await db.commit()

    resp = await _build_evaluation_response(
        db, evaluation, caller_can_manage=can_manage(user, evaluation, user_group_ids)
    )
    if family_conflicts:
        resp["family_conflicts"] = family_conflicts
    return resp


@router.post("/{evaluation_id}/recalculate-headlines")
async def recalculate_headline_scores(
    evaluation_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Recompose every run's headlines from stored per-metric values.

    Read-time configuration only: the tolerance band, the spread penalty and
    the headline metric selection. No agent is re-executed and no metric is re-measured.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    from app.services.evaluation_engine import _compute_run_headlines

    await _compute_run_headlines(db, evaluation.id)
    await db.commit()
    return {"status": "ok"}


@router.delete("/{evaluation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_evaluation(
    evaluation_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete an evaluation (cascades to agents, entries, runs, results)."""
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(evaluation, user)

    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="evaluation",
        entity_id=str(evaluation.id),
        entity_name=evaluation.name,
        summary=f"Deleted evaluation '{evaluation.name}'",
    )
    # Its work stops with it, in the same transaction.
    await _cancel_evaluation_executions(db, evaluation.id)
    await db.delete(evaluation)
    await db.commit()


# ---------------------------------------------------------------------------
# Output extraction
# ---------------------------------------------------------------------------


@router.post(
    "/{evaluation_id}/extractors/preview", response_model=PreviewExtractorResponse
)
async def preview_extractor(
    evaluation_id: str,
    body: PreviewExtractorRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Show what an extractor extracts, without executing anything.

    Read-only: no run, no result, no audit row, no column written. It calls the
    same ``apply_extractor`` as the run path, so preview and run cannot drift.

    Every authorization failure is a 404, not a 403, so the endpoint cannot probe
    for another tenant's evaluation, agent or result.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(evaluation, user)

    if (body.result_id is None) == (body.sample is None):
        raise HTTPException(
            status_code=422,
            detail="supply exactly one of 'result_id' or 'sample'",
        )

    agent_uuid = parse_uuid(body.agent_id, label="Agent ID")
    linked = await db.execute(
        select(EvaluationAgent.id).where(
            EvaluationAgent.evaluation_id == evaluation.id,
            EvaluationAgent.agent_id == agent_uuid,
        )
    )
    if linked.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Agent not found on this evaluation")

    extractor = body.extractor.model_dump(exclude_none=True)

    # Same validation as a save, family check included, so the author learns now
    # what a run would tell them later.
    problem = validate_extractor_chain(extractor, family=evaluation.family)
    if problem:
        raise HTTPException(status_code=422, detail=problem)

    if body.result_id is not None:
        result_uuid = parse_uuid(body.result_id, label="Result ID")
        # Resolved by JOIN, never by id alone, or this endpoint would read any
        # result in the database.
        row = await db.execute(
            select(EvaluationResult.agent_output, EvaluationResult.execution_details)
            .join(EvaluationRun, EvaluationRun.id == EvaluationResult.run_id)
            .where(
                EvaluationResult.id == result_uuid,
                EvaluationRun.evaluation_id == evaluation.id,
                EvaluationRun.agent_id == agent_uuid,
            )
        )
        found = row.one_or_none()
        if found is None:
            raise HTTPException(
                status_code=404, detail="Result not found for this evaluation and agent"
            )
        agent_output, execution_details = found.agent_output, found.execution_details
    else:  # the sample: exactly one of the two is set, checked above
        agent_output, execution_details = _sample_material(body.sample)

    return _preview_outcome(extractor, agent_output, execution_details)


@router.post("/extractors/preview", response_model=PreviewExtractorResponse)
async def preview_extractor_for_family(
    body: PreviewExtractorForFamilyRequest,
    user: User = Depends(require_admin),
):
    """Show what an extractor extracts from a pasted sample, before the evaluation
    exists.

    The creation screen sets each agent's extractor while the evaluation is being
    written, so there is no evaluation, linked agent or stored result to read: the
    family the author picked, the extractor and the sample are the whole input, and
    the checks and the extraction are ``preview_extractor``'s own.
    """
    _validate_family(body.family)
    extractor = body.extractor.model_dump(exclude_none=True)
    problem = validate_extractor_chain(extractor, family=body.family)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    agent_output, execution_details = _sample_material(body.sample)
    return _preview_outcome(extractor, agent_output, execution_details)


def _sample_material(sample: PreviewExtractorSample) -> tuple[str, dict]:
    """The answer and execution details a pasted sample stands for, within the bound.

    The whole sample is weighed, not just the answer: a tool_output extractor reads
    its payload from ``traces``, which would otherwise escape the bound. Serialised by
    pydantic, not ``json.dumps``: ``traces`` is an untyped ``list[dict]``, and a value
    the stdlib encoder refuses would be a 500.
    """
    max_sample_bytes = current_limits().max_sample_bytes
    if len(sample.model_dump_json(exclude_none=True).encode("utf-8")) > max_sample_bytes:
        raise HTTPException(
            status_code=422,
            detail=f"the pasted sample exceeds {max_sample_bytes} bytes",
        )
    return sample.agent_output or "", {"traces": sample.traces or []}


def _preview_outcome(extractor: dict, agent_output: str | None, execution_details: object) -> dict:
    """What ``extractor`` extracts from one answer: the run path's own extraction."""
    source_text, failure = resolve_source(
        dict(extractor.get("source") or {}), agent_output, execution_details
    )
    if failure is None:
        outcome = apply_extractor(extractor, source_text or "")
        failure = outcome.failure
        if failure is None:
            prediction = outcome.prediction or ""
            return {
                "status": "extracted",
                "prediction": prediction,
                "output_kind": extractor.get("output_kind"),
                "item_count": extraction_item_count(prediction),
            }

    return {
        "status": "failed",
        "prediction": None,
        "output_kind": extractor.get("output_kind"),
        "failure": {
            key: value
            for key, value in failure.as_detail().items()
            if key != "agent_id"
        },
    }


@router.get(
    "/{evaluation_id}/extractors/sample-results",
    response_model=list[ExtractorSampleResultResponse],
)
async def list_extractor_sample_results(
    evaluation_id: str,
    agent_id: str,
    limit: int = 20,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Recent stored results an author may preview an extractor against.

    This is how an author learns a result id for the preview. Scoped by the
    preview's own JOIN, so it never enumerates another tenant's results, and it
    returns labels only, never ``agent_output``.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(evaluation, user)

    agent_uuid = parse_uuid(agent_id, label="Agent ID")
    linked = await db.execute(
        select(EvaluationAgent.id).where(
            EvaluationAgent.evaluation_id == evaluation.id,
            EvaluationAgent.agent_id == agent_uuid,
        )
    )
    if linked.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Agent not found on this evaluation")

    rows = await db.execute(
        select(
            EvaluationResult.id,
            EvaluationResult.created_at,
            DatasetEntry.position,
            DatasetEntry.description,
        )
        .join(EvaluationRun, EvaluationRun.id == EvaluationResult.run_id)
        .outerjoin(DatasetEntry, DatasetEntry.id == EvaluationResult.entry_id)
        .where(
            EvaluationRun.evaluation_id == evaluation.id,
            EvaluationRun.agent_id == agent_uuid,
        )
        .order_by(EvaluationResult.created_at.desc())
        .limit(max(1, min(limit, 100)))
    )
    return [
        {
            "result_id": str(row.id),
            "created_at": row.created_at,
            # What names the entry, not a sentence: the reader's language is the
            # frontend's.
            "description": row.description or None,
            "position": row.position,
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Agent Binding
# ---------------------------------------------------------------------------


@router.post("/{evaluation_id}/agents", response_model=EvaluationResponse)
async def add_agents(
    evaluation_id: str,
    body: AddAgentsRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Add agents to an evaluation."""
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(evaluation, user)

    agent_uuids = [parse_uuid(aid_str, label="Agent ID") for aid_str in body.agent_ids]
    await _refuse_hidden_subjects(db, evaluation.user_id, agent_ids=agent_uuids)
    for aid in agent_uuids:
        existing = await db.execute(
            select(EvaluationAgent).where(
                EvaluationAgent.evaluation_id == evaluation.id,
                EvaluationAgent.agent_id == aid,
            )
        )
        if not existing.scalar_one_or_none():
            db.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=aid))

    await db.commit()
    return await _build_evaluation_response(
        db, evaluation, caller_can_manage=can_manage(user, evaluation, user_group_ids)
    )


@router.delete(
    "/{evaluation_id}/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_agent(
    evaluation_id: str,
    agent_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Remove an agent from an evaluation."""
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(evaluation, user)

    aid = safe_uuid(agent_id)
    if aid is None:
        return
    await db.execute(
        delete(EvaluationAgent).where(
            EvaluationAgent.evaluation_id == evaluation.id,
            EvaluationAgent.agent_id == aid,
        )
    )
    await db.commit()


async def _get_linked_agent_or_404(
    db: AsyncSession, evaluation_id: uuid.UUID, agent_id: uuid.UUID
) -> EvaluationAgent:
    """Load the ``EvaluationAgent`` link or raise 400 if the agent is not linked."""
    link = (
        await db.execute(
            select(EvaluationAgent).where(
                EvaluationAgent.evaluation_id == evaluation_id,
                EvaluationAgent.agent_id == agent_id,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(
            status_code=400, detail="Agent is not linked to this evaluation"
        )
    return link


@router.put("/{evaluation_id}/baseline", response_model=BaselineResponse)
async def set_baseline(
    evaluation_id: str,
    body: SetBaselineRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Designate the evaluation's baseline agent.

    Atomically clears any prior baseline within the evaluation and sets the
    target; ``agent_id=null`` clears the baseline. The ``uq_eval_agent_baseline``
    partial index enforces at most one baseline per evaluation.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(evaluation, user)

    target_id: uuid.UUID | None = None
    if body.agent_id is not None:
        target_id = parse_uuid(body.agent_id, label="Agent ID")
        await _get_linked_agent_or_404(db, evaluation.id, target_id)

    # Clear any existing baseline first so the partial unique index never sees
    # two baseline rows during the re-point.
    await db.execute(
        update(EvaluationAgent)
        .where(
            EvaluationAgent.evaluation_id == evaluation.id,
            EvaluationAgent.is_baseline.is_(True),
        )
        .values(is_baseline=False)
    )
    if target_id is not None:
        await db.execute(
            update(EvaluationAgent)
            .where(
                EvaluationAgent.evaluation_id == evaluation.id,
                EvaluationAgent.agent_id == target_id,
            )
            .values(is_baseline=True)
        )

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="evaluation",
        entity_id=str(evaluation.id),
        entity_name=evaluation.name,
        summary=(
            f"Set baseline agent to {target_id}"
            if target_id is not None
            else "Cleared baseline agent"
        ),
    )
    await db.commit()
    return {
        "evaluation_id": str(evaluation.id),
        "baseline_agent_id": str(target_id) if target_id is not None else None,
    }


@router.put(
    "/{evaluation_id}/agents/{agent_id}/tags", response_model=AgentTagsResponse
)
async def set_agent_tags(
    evaluation_id: str,
    agent_id: str,
    body: SetAgentTagsRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Replace an agent's evaluation-scoped tag set."""
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(evaluation, user)
    aid = parse_path_id(agent_id, not_found_detail="Agent is not linked to this evaluation")
    link = await _get_linked_agent_or_404(db, evaluation.id, aid)
    link.tags = strip_reserved_keys(body.tags)
    await db.commit()
    return {
        "evaluation_id": str(evaluation.id),
        "agent_id": str(aid),
        "tags": link.tags or {},
        "is_baseline": bool(link.is_baseline),
    }


# ---------------------------------------------------------------------------
# Evaluation Runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ModelSelection:
    """The models a manual launch runs, as the trigger and the placebo estimate resolve them."""

    model_ids: list[str]
    use_default: bool
    #: The owner's provider routing, read when an extra model had to be checked.
    routing: object | None
    #: Selected platform models the evaluation does not list yet: the trigger adds them.
    new_model_ids: list[str]


async def _launch_model_selection(
    db: AsyncSession, evaluation: Evaluation, model_ids: list[str] | None, *, include_default: bool
) -> _ModelSelection:
    """The models of a manual launch: the enabled ones, or the selection with its extra models checked.

    A selected model that is neither enabled nor an available platform model is
    a 400. With a selection and no ``include_default``, the agent's default
    model is not used even if ``use_agent_default_model`` is on.
    """
    enabled_model_ids = active_model_ids(evaluation)
    if model_ids is None:
        return _ModelSelection(
            model_ids=list(enabled_model_ids),
            use_default=bool(evaluation.use_agent_default_model),
            routing=None,
            new_model_ids=[],
        )
    selected = list(dict.fromkeys(model_ids))
    active_set = set(enabled_model_ids)
    extra = [mid for mid in selected if mid not in active_set]
    routing = None
    new_model_ids: list[str] = []
    if extra:
        # The runs resolve their models among the owner's providers.
        from app.llm.router import get_available_models
        from app.services.evaluation_engine import evaluation_owner_id, owner_model_routing

        routing = await owner_model_routing(db, evaluation_owner_id(evaluation))
        available_model_ids = {
            str(item["model"]) for item in await get_available_models(db, routing=routing) if item.get("model")
        }
        invalid_extra = [mid for mid in extra if mid not in available_model_ids]
        if invalid_extra:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Selected model_ids must be enabled additional models or available platform models: "
                    + ", ".join(invalid_extra)
                ),
            )
        configured = set(evaluation.model_ids or [])
        new_model_ids = [mid for mid in extra if mid not in configured]
    return _ModelSelection(
        model_ids=selected,
        use_default=bool(evaluation.use_agent_default_model and include_default),
        routing=routing,
        new_model_ids=new_model_ids,
    )


def _refuse_comparison_runs(runs: list[EvaluationRun] | tuple[EvaluationRun, ...]) -> None:
    """A placebo comparison's run is never re-run on its own (FR-20): 422.

    Alone it compares nothing, and copied without its arm it would run the
    real agent under a control run's name.
    """
    if any(run.placebo_comparison_id is not None for run in runs):
        raise HTTPException(status_code=422, detail=COMPARISON_RUN_REFUSAL)


def _launch_concurrency(evaluation: Evaluation) -> int:
    """Runs one execution dispatches at once: what a placebo comparison's deadline scales by."""
    from app.core.config import settings as app_settings

    return 1 if evaluation.run_mode == "sequential" else app_settings.evaluation_max_concurrent_runs


def _write_tools_refusal(sources: dict[str, list[str]]) -> str:
    listed = "; ".join(f"{agent}: {', '.join(found)}" for agent, found in sorted(sources.items()))
    return (
        "This placebo comparison repeats, for every arm and trial, the calls of tools this "
        f"evaluation runs without approval ({listed}). Send confirm_write_tools to launch it."
    )


async def _placebo_launch_or_refusal(
    db: AsyncSession, evaluation: Evaluation, plan: LaunchPlan, body: TriggerEvaluationRequest
):
    """The placebo launch of a trigger, or its refusal as a 422 (FR-10, FR-13, FR-14, FR-20)."""
    from app.services.evaluation.placebo_launch import plan_placebo_launch

    # The estimate's seed when the dialog sends it back: the launch then builds
    # the placebos the estimate checked.
    placebo = await plan_placebo_launch(db, evaluation, plan, seed=body.placebo_seed or uuid.uuid4().hex)
    if placebo.refusals:
        raise HTTPException(status_code=422, detail="; ".join(placebo.refusals))
    if placebo.write_tool_sources and not body.confirm_write_tools:
        raise HTTPException(status_code=422, detail=_write_tools_refusal(placebo.write_tool_sources))
    return placebo


@router.post("/{evaluation_id}/trigger", response_model=list[EvaluationRunResponse])
async def trigger_evaluation(
    evaluation_id: str,
    body: TriggerEvaluationRequest | None = None,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Manually trigger an evaluation. Creates agent x model matrix of runs."""
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    await _require_launchable(db, evaluation)

    selection = await _launch_model_selection(
        db,
        evaluation,
        body.model_ids if body else None,
        include_default=bool(body and body.include_default),
    )
    if selection.new_model_ids:
        # Newly added models are enabled; the disabled list is unchanged.
        evaluation.model_ids = [*dict.fromkeys(evaluation.model_ids or []), *selection.new_model_ids]

    # The same plan and runs as the schedule: agents x models, a
    # model-independent agent collapsed to one run, a saved model no provider
    # would answer refused up front, the full version snapshot.
    try:
        plan = await plan_launch(
            db,
            evaluation,
            model_ids=selection.model_ids,
            use_default=selection.use_default,
            routing=selection.routing,
        )
    except LaunchRefusedError as refused:
        raise _http_refusal(refused) from None
    # A placebo comparison: its arms x trials instead of one run per
    # agent x model, refused here as a whole before anything is written.
    placebo = await _placebo_launch_or_refusal(db, evaluation, plan, body) if body and body.placebo else None
    # Reserved keys stripped, as on every other tag write path.
    trigger_tags = strip_reserved_keys(body.tags if body else None)
    # Stage the execution in the same transaction as the runs, and own
    # them from creation: a run committed active without its owning
    # execution could be failed by another execution's cleanup.
    from app.services.evaluation_engine import stage_evaluation_execution
    from app.services.orchestration_engine import notify_orchestration_wake

    execution = await stage_evaluation_execution(
        db, str(evaluation.id), waves=placebo.waves(_launch_concurrency(evaluation)) if placebo else 1
    )
    comparison = None
    if placebo is not None:
        from app.services.evaluation.placebo_launch import new_comparison

        comparison = new_comparison(evaluation, placebo, created_by=user.id)
        db.add(comparison)
        await db.flush()
    runs = await add_launch_runs(
        db,
        evaluation,
        plan,
        trigger_reason="manual",
        owner_execution_id=execution.id,
        tags=trigger_tags or None,
        comparison=(comparison, placebo.plan.runs) if comparison is not None and placebo is not None else None,
    )

    evaluation.last_triggered_at = datetime.now(UTC)
    await db.flush()

    # Manual trigger params outrank same-key auto/evaluation params, resolved at
    # upsert time. A flat key/value object, or 422.
    manual_params = body.params if body else None
    if manual_params is not None and (
        not isinstance(manual_params, dict)
        or any(isinstance(v, (dict, list)) for v in manual_params.values())
    ):
        raise HTTPException(
            status_code=422,
            detail="params must be a flat key/value object",
        )
    if manual_params:
        from app.services.evaluation import params as run_params

        await run_params.capture_params_for_runs(
            db, [r.id for r in runs], manual_params, "manual"
        )

    # Per-trigger prediction-reuse override. ``inherit`` (or absent) falls back
    # to the evaluation's ``reuse_default``; ``on``/``off`` force it. Persisted
    # as a manual param so it outranks the auto ``reuse`` param. Any other
    # value is a 422.
    reuse_override = body.reuse if body else None
    if reuse_override is not None and reuse_override not in ("inherit", "on", "off"):
        raise HTTPException(
            status_code=422,
            detail="reuse must be one of: inherit, on, off",
        )
    if reuse_override in ("on", "off"):
        # ``capture_params_for_runs`` is a bulk UPSERT, so this is safe
        # when ``manual_params`` already named "reuse" for the same runs: an
        # equal-precedence "manual" re-capture overwrites (``should_overwrite``),
        # so the explicit override wins.
        from app.services.evaluation import params as run_params

        await run_params.capture_params_for_runs(
            db, [r.id for r in runs], {"reuse": reuse_override}, "manual"
        )
    if comparison is not None:
        # Recorded as the runs execute: a comparison's runs never reuse.
        from app.services.evaluation import params as run_params

        await run_params.capture_params_for_runs(db, [r.id for r in runs], {"reuse": "off"}, "manual")

    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="evaluation",
        entity_id=str(evaluation.id),
        entity_name=evaluation.name,
        summary=(
            f"Triggered a placebo comparison of {len(runs)} evaluation runs"
            if comparison is not None
            else f"Triggered {len(runs)} evaluation runs"
        ),
    )
    await db.commit()
    await notify_orchestration_wake()

    # Fresh runs cannot miss snapshots: their version numbers come from
    # ``snapshots_by_agent``, resolved above against existing rows. Passing
    # ``missing_snapshots=[]`` skips the per-run
    # ``resolve_run_snapshot_overrides()``, keeping the response at O(runs)
    # DB calls.
    return [
        await _build_run_response(db, r, missing_snapshots=[]) for r in runs
    ]


@router.post("/{evaluation_id}/placebo/estimate", response_model=PlaceboEstimateResponse)
async def estimate_placebo_comparison(
    evaluation_id: str,
    body: PlaceboEstimateRequest | None = None,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """What a placebo comparison would run now, and what would refuse it (FR-12). Writes nothing.

    The models resolve as the trigger resolves them (``_launch_model_selection``),
    without adding an extra model to the evaluation. The trigger takes the
    returned ``seed`` back to build the placebos checked here.
    """
    from app.services.evaluation.placebo_arms import MAX_COMPARISON_EXECUTIONS
    from app.services.evaluation.placebo_launch import estimate_tokens, plan_placebo_launch

    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    selection = await _launch_model_selection(
        db, evaluation, body.model_ids if body else None, include_default=bool(body and body.include_default)
    )
    try:
        plan = await plan_launch(
            db,
            evaluation,
            model_ids=selection.model_ids,
            use_default=selection.use_default,
            routing=selection.routing,
        )
    except LaunchRefusedError as refused:
        raise _http_refusal(refused) from None
    placebo = await plan_placebo_launch(db, evaluation, plan, seed=uuid.uuid4().hex)
    return PlaceboEstimateResponse(
        seed=placebo.seed,
        runs=len(placebo.plan.runs),
        agent_executions=placebo.executions,
        judge_calls=placebo.executions if judge_enabled(evaluation.scorer_selection) else 0,
        probe_calls=placebo.probe_calls,
        estimated_tokens=await estimate_tokens(db, evaluation, placebo.plan.runs, placebo.entries),
        execution_cap=MAX_COMPARISON_EXECUTIONS,
        write_tool_sources=placebo.write_tool_sources,
        skipped=placebo.plan.skipped,
        warnings=placebo.warnings,
        refusals=placebo.refusals,
    )


@router.get("/{evaluation_id}/placebo-comparisons", response_model=list[PlaceboComparisonResponse])
async def list_placebo_comparisons(
    evaluation_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """An evaluation's placebo comparisons, newest first, each with its runs and results.

    A comparison whose runs have all finished is settled first, so a stopped
    worker never leaves one running.
    """
    from app.services.evaluation.placebo_comparisons import comparison_responses, settle_finished_comparisons

    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    await settle_finished_comparisons(db, evaluation.id)
    comparisons = (
        await db.execute(
            select(EvaluationPlaceboComparison)
            .where(EvaluationPlaceboComparison.evaluation_id == evaluation.id)
            .order_by(EvaluationPlaceboComparison.created_at.desc(), EvaluationPlaceboComparison.id)
            .limit(limit)
            .offset(offset)
        )
    ).scalars()
    return await comparison_responses(db, list(comparisons))


@router.get("/placebo-comparisons/{comparison_id}", response_model=PlaceboComparisonResponse)
async def get_placebo_comparison(
    comparison_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """One placebo comparison: its runs, warnings and statistics.

    The comparison of an evaluation the caller cannot see answers as an unknown id.
    """
    from app.services.evaluation.placebo_comparisons import comparison_responses, settle_placebo_comparisons

    not_found = HTTPException(status_code=404, detail="Placebo comparison not found")
    cid = parse_path_id(comparison_id, not_found_detail="Placebo comparison not found")
    comparison = await db.get(EvaluationPlaceboComparison, cid)
    if comparison is None:
        raise not_found
    user_group_ids = await get_user_group_ids(db, user.id)
    try:
        await _get_user_evaluation(
            db, user.id, str(comparison.evaluation_id), user_group_ids, is_admin=user.is_admin
        )
    except HTTPException as err:
        if err.status_code == 404:
            raise not_found from err
        raise
    await settle_placebo_comparisons(db, [cid])
    await db.refresh(comparison)
    (response,) = await comparison_responses(db, [comparison])
    return response


@router.post("/{evaluation_id}/trigger-from-runs", response_model=list[EvaluationRunResponse])
async def trigger_evaluation_from_runs(
    evaluation_id: str,
    body: TriggerEvaluationFromRunsRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Trigger fresh runs from selected baseline runs, unconditionally.

    The bulk "Re-run full": each selected run gets a fresh one with its own agent,
    model, dataset pin and persona, prompt, config and skill versions, exactly as the
    single-run action (``replay_run``) replays one. ``409`` when a run's
    version snapshot is gone, as there: the run would no longer be replayed.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    await _require_launchable(db, evaluation)

    # Canonicalize the request IDs up front, so a case- or format-only
    # difference (an uppercase UUID, braces) neither slips past the dedup nor
    # reports a spurious "missing" against the lowercase ``str(run.id)`` keys.
    # ``parse_uuid`` raises 422 on a malformed entry, which validates input.
    raw_run_ids = body.run_ids or []
    run_ids = list(
        dict.fromkeys(
            str(parse_uuid(rid, label="Run ID")) for rid in raw_run_ids
        )
    )
    if not run_ids:
        raise HTTPException(status_code=400, detail="No baseline runs selected")

    # The canonical strings always parse.
    baseline_uuids = [uuid.UUID(rid) for rid in run_ids]
    baseline_result = await db.execute(
        select(EvaluationRun).where(
            EvaluationRun.evaluation_id == evaluation.id,
            EvaluationRun.id.in_(baseline_uuids),
        )
    )
    baseline_runs = baseline_result.scalars().all()
    baseline_by_id = {str(run.id): run for run in baseline_runs}

    missing = [rid for rid in run_ids if rid not in baseline_by_id]
    if missing:
        raise HTTPException(
            status_code=404,
            detail="Baseline run(s) not found in this evaluation: " + ", ".join(missing),
        )
    _refuse_comparison_runs(baseline_runs)

    # Every saved model no provider would answer, refused up front as the Run
    # button does.
    try:
        await check_runs_served(
            db,
            evaluation,
            [(run.agent_id, run.model_id, run.is_default_model) for run in baseline_runs],
        )
    except LaunchRefusedError as refused:
        raise _http_refusal(refused) from None

    entry_count_result = await db.execute(
        select(func.count(DatasetEntry.id))
        .join(EvaluationDataset, EvaluationDataset.dataset_id == DatasetEntry.dataset_id)
        .where(EvaluationDataset.evaluation_id == evaluation.id)
    )
    entry_count = entry_count_result.scalar() or 0
    if entry_count == 0:
        raise HTTPException(status_code=400, detail="No dataset entries")

    runs: list[EvaluationRun] = []

    # Per-baseline pinned entry counts: each new run inherits
    # its baseline's pinned_dataset_versions, whose entry count can differ from
    # the live one; with the live count, ``completed_entries`` might never
    # reach ``total_entries`` and the run would stay "running". Memoized by
    # pin, since baselines often share one snapshot.
    _pinned_entry_counts: dict[tuple[tuple[str, int], ...], int] = {}

    async def _total_entries_for(baseline: EvaluationRun) -> int:
        pinned = baseline.pinned_dataset_versions
        if not pinned:
            return entry_count
        key = tuple(sorted(pinned.items()))
        if key not in _pinned_entry_counts:
            from app.services.evaluation import dataset_versioning

            count = await dataset_versioning.count_pinned_entries(
                db, evaluation.id, pinned
            )
            _pinned_entry_counts[key] = count if count > 0 else entry_count
        return _pinned_entry_counts[key]

    # Replayed with their own versions, so each must still resolve, as
    # ``replay_run`` checks: one batched lookup per snapshot kind, whatever the
    # number of runs. Every refusal is named at once.
    from app.services.evaluation_engine import batch_compute_run_replayability

    missing_snapshots = await batch_compute_run_replayability(
        db, [baseline_by_id[rid] for rid in run_ids]
    )
    missing_by_run = [
        f"{rid} ({', '.join(missing)})"
        for rid in run_ids
        if (missing := missing_snapshots.get(baseline_by_id[rid].id))
    ]
    if missing_by_run:
        raise HTTPException(
            status_code=409,
            detail="Replay impossible: missing version snapshots for run(s) "
            + "; ".join(missing_by_run),
        )

    # Every selected baseline gets a fresh run: a bulk "Re-run full"
    # is a deliberate forced recompute, like the single-run action.
    #
    # Each new run inherits its own baseline's ``pinned_dataset_versions``,
    # not a fresh pin: re-pinning would score a changed dataset
    # differently from the baseline it re-runs, and diverge from
    # ``replay_run``, which copies the pin too.
    for run_id in run_ids:
        baseline = baseline_by_id[run_id]
        run = EvaluationRun(
            evaluation_id=evaluation.id,
            agent_id=baseline.agent_id,
            flow_id=baseline.flow_id,
            flow_version_number=baseline.flow_version_number if baseline.flow_id else None,
            model_id=baseline.model_id,
            is_default_model=baseline.is_default_model,
            status="pending",
            trigger_reason="manual",
            total_entries=await _total_entries_for(baseline),
            persona_version_number=baseline.persona_version_number,
            prompt_versions=baseline.prompt_versions,
            agent_config_versions=baseline.agent_config_versions,
            skill_versions=baseline.skill_versions,
            pinned_dataset_versions=baseline.pinned_dataset_versions,
        )
        db.add(run)
        runs.append(run)

    evaluation.last_triggered_at = datetime.now(UTC)
    await db.flush()

    # Force a fresh prediction for every run this creates, whatever the
    # evaluation's ``reuse_default``: a manual param outranks the auto
    # ``reuse`` param, as in ``trigger_evaluation``.
    from app.services.evaluation import params as run_params

    await run_params.capture_params_for_runs(
        db, [r.id for r in runs], {"reuse": "off"}, "manual"
    )

    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="evaluation",
        entity_id=str(evaluation.id),
        entity_name=evaluation.name,
        summary=f"Triggered {len(runs)} runs from selected baselines",
    )

    # Stage the execution in the same transaction as the runs, as in
    # the replay endpoint.
    from app.services.evaluation_engine import stage_evaluation_execution
    from app.services.orchestration_engine import notify_orchestration_wake

    execution = await stage_evaluation_execution(db, str(evaluation.id))
    # Stamp the owner in the same transaction as the runs: a run
    # committed active without its owning execution could be failed by
    # another execution's cleanup.
    for run in runs:
        run.owner_execution_id = execution.id
    await db.commit()
    await notify_orchestration_wake()

    # Same fast path as ``trigger_evaluation``: fresh runs have resolved
    # snapshots, so ``missing_snapshots=[]`` skips the per-run lookup.
    return [
        await _build_run_response(db, r, missing_snapshots=[]) for r in runs
    ]


@router.post(
    "/{evaluation_id}/rescore-from-runs",
    response_model=RescoreFromRunsResponse,
    response_model_exclude_none=True,
)
async def rescore_evaluation_from_runs(
    evaluation_id: str,
    body: RescoreFromRunsRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Bulk re-score selected runs, reusing each run's stored predictions.

    Unlike ``trigger_evaluation_from_runs``, an unknown or foreign run ID is
    reported per entry (``status: "failed", error: "run_not_found"``) rather
    than failing the whole batch with a 404: this endpoint is per-run
    resilient.

    Runs in the background: only ``run_not_found``,
    ``no_predictions`` and ``run_in_progress`` (already ``pending`` or
    ``running``) are detected synchronously. Every other selected run is set
    to ``"running"`` and scheduled as one background batch, so the Activity
    tab's status-driven polling shows progress. The agent is never re-run.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    evaluation = await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(evaluation, user)

    _require_runnable_family(evaluation)
    await _require_dataset_families_set(db, evaluation)

    # Canonicalize + dedupe as trigger_evaluation_from_runs does: a case- or
    # format-only duplicate collapses to one result entry.
    raw_run_ids = body.run_ids or []
    run_ids = list(
        dict.fromkeys(
            str(parse_uuid(rid, label="Run ID")) for rid in raw_run_ids
        )
    )
    if not run_ids:
        raise HTTPException(status_code=400, detail="No runs selected")

    run_uuids = [uuid.UUID(rid) for rid in run_ids]
    # Lock the selected rows before checking and setting status, or
    # two overlapping bulk requests can both see a run "completed" and both
    # schedule it. Ordered by id, so overlapping batches take the locks in the
    # same order and cannot deadlock.
    runs_result = await db.execute(
        select(EvaluationRun)
        .where(
            EvaluationRun.evaluation_id == evaluation.id,
            EvaluationRun.id.in_(run_uuids),
        )
        .order_by(EvaluationRun.id)
        .with_for_update()
    )
    runs_by_id = {str(run.id): run for run in runs_result.scalars().all()}

    # Predictions per run, so "no stored predictions" is rejected
    # synchronously, as in the single-run endpoint; the rescoring itself runs
    # in the background.
    counts_result = await db.execute(
        select(EvaluationResult.run_id, func.count(EvaluationResult.id))
        .where(EvaluationResult.run_id.in_(run_uuids))
        .group_by(EvaluationResult.run_id)
    )
    predictions_by_run = {str(rid): count for rid, count in counts_result.all()}

    results: list[RescoreFromRunsResult] = []
    queued_run_ids: list[str] = []
    failed_count = 0
    for run_id in run_ids:
        if run_id not in runs_by_id:
            results.append(
                RescoreFromRunsResult(
                    run_id=run_id, status="failed", error="run_not_found"
                )
            )
            failed_count += 1
            continue
        if runs_by_id[run_id].placebo_comparison_id is not None:
            # A placebo comparison's run is never re-scored alone.
            results.append(
                RescoreFromRunsResult(
                    run_id=run_id, status="failed", error="placebo_comparison_run"
                )
            )
            failed_count += 1
            continue
        if runs_by_id[run_id].status in ("pending", "running"):
            results.append(
                RescoreFromRunsResult(
                    run_id=run_id, status="failed", error="run_in_progress"
                )
            )
            failed_count += 1
            continue
        if not predictions_by_run.get(run_id):
            results.append(
                RescoreFromRunsResult(
                    run_id=run_id, status="failed", error="no_predictions"
                )
            )
            failed_count += 1
            continue
        # The status is set only once the execution is staged, below;
        # queued_run_ids is a candidate list here.
        queued_run_ids.append(run_id)
        results.append(RescoreFromRunsResult(run_id=run_id, status="running"))

    if queued_run_ids:
        from app.services.evaluation_engine import stage_rescore_execution
        from app.services.orchestration_engine import notify_orchestration_wake

        # Stage the execution before setting any run's status: both
        # commit together, so a failure never commits a "running" run with
        # nothing to act on it. The row locks above make this atomic against
        # a concurrent cancel or rescore.
        try:
            execution = await stage_rescore_execution(
                db,
                str(evaluation.id),
                queued_run_ids,
                # What each run goes back to, whatever happens to the rescore.
                previous_statuses={rid: runs_by_id[rid].status for rid in queued_run_ids},
            )
        except Exception as exc:
            logger.error(
                "Failed to schedule bulk rescore for runs %s: %s",
                queued_run_ids,
                format_exception_message(exc),
                exc_info=True,
            )
            # As in rescore_evaluation_run: release the row locks and the
            # aborted transaction now, not at get_db() teardown.
            await db.rollback()
            raise HTTPException(
                status_code=500, detail="Failed to schedule rescore"
            ) from exc

        for run_id in queued_run_ids:
            runs_by_id[run_id].status = "running"
            # Ownership goes with the status, as in
            # rescore_evaluation_run: an earlier, abandoned owner cannot fail
            # these runs once this rescore lands.
            runs_by_id[run_id].owner_execution_id = execution.id

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="evaluation",
        entity_name=evaluation.name,
        entity_id=str(evaluation.id),
        summary=(
            f"Started rescoring {len(queued_run_ids)} of {len(run_ids)} selected "
            f"runs ({failed_count} failed immediately)"
        ),
    )
    await db.commit()
    if queued_run_ids:
        await notify_orchestration_wake()

    return RescoreFromRunsResponse(results=results)


# ---------------------------------------------------------------------------
# HuggingFace import (creates a new Dataset and links it to the evaluation)
# ---------------------------------------------------------------------------


@router.get("/{evaluation_id}/runs/count", response_model=EvaluationRunCountResponse)
async def count_runs(
    evaluation_id: str,
    agent_id: str | None = Query(None),
    model_id: str | None = Query(None),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return the total number of runs for an evaluation.

    Accepts the same ``agent_id`` / ``model_id`` filters as the list endpoint,
    so the Activity table shows a stable page count rather than one estimated
    from the current page.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )

    eid = parse_path_id(evaluation_id, not_found_detail="Evaluation not found")
    query = select(func.count(EvaluationRun.id)).where(
        EvaluationRun.evaluation_id == eid
    )
    if agent_id:
        aid = parse_uuid(agent_id, label="Agent ID")
        query = query.where(EvaluationRun.agent_id == aid)
    if model_id:
        query = query.where(EvaluationRun.model_id == model_id)

    result = await db.execute(query)
    return EvaluationRunCountResponse(count=result.scalar_one())


@router.get("/{evaluation_id}/runs", response_model=list[EvaluationRunResponse])
async def list_runs(
    evaluation_id: str,
    agent_id: str | None = Query(None),
    model_id: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    exclude_comparisons: bool = Query(False),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List runs for an evaluation with pagination.

    ``exclude_comparisons`` leaves a placebo comparison's runs out: the
    Overview ranks configurations, and a comparison's trials would fill its page.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )

    eid = parse_path_id(evaluation_id, not_found_detail="Evaluation not found")
    query = (
        select(EvaluationRun)
        .where(EvaluationRun.evaluation_id == eid)
        .order_by(EvaluationRun.created_at.desc())
    )

    if agent_id:
        aid = parse_uuid(agent_id, label="Agent ID")
        query = query.where(EvaluationRun.agent_id == aid)
    if model_id:
        query = query.where(EvaluationRun.model_id == model_id)
    if exclude_comparisons:
        query = query.where(EvaluationRun.placebo_comparison_id.is_(None))

    query = query.limit(limit).offset(offset)

    result = await db.execute(query)
    runs = list(result.scalars().all())

    # Replayability for the whole page in 3 queries (one per snapshot type),
    # not ``resolve_run_snapshot_overrides`` per run (N+1 at 100 per page).
    from app.services.evaluation_engine import batch_compute_run_replayability

    missing_by_run = await batch_compute_run_replayability(db, runs)
    held_reasons = await _held_reasons_for(db, runs)
    run_modes = await load_run_modes(db, runs)
    return [
        await _build_run_response(
            db,
            r,
            missing_snapshots=missing_by_run.get(r.id, []),
            held_reasons=held_reasons,
            run_modes=run_modes,
        )
        for r in runs
    ]


# ---------------------------------------------------------------------------
# Chart Data
# ---------------------------------------------------------------------------


# Trend windows. Three trend charts read this endpoint, so it is
# bounded: `90d` is the default rather than `all`.
_CHART_RANGES: dict[str, int | None] = {"30d": 30, "90d": 90, "all": None}
_DEFAULT_CHART_RANGE = "90d"

# The three trend series and the three correlation views, declared once so the
# payload keys cannot drift from what the UI reads.
_TREND_SERIES: tuple[tuple[str, str], ...] = (
    ("quality", "quality_score"),
    ("performance", "performance_score"),
    ("judge", "score"),
)
_CORRELATIONS: tuple[tuple[str, str, str], ...] = (
    ("quality_performance", "quality_score", "performance_score"),
    ("judge_performance", "score", "performance_score"),
    ("quality_judge", "quality_score", "score"),
)


@router.get("/{evaluation_id}/chart", response_model=EvaluationChartResponse)
async def get_chart_data(
    evaluation_id: str,
    # `Annotated` rather than a `Query(...)` default, so the Python default is a
    # real string when the tests call the endpoint directly, without FastAPI's
    # DI.
    range: Annotated[
        str,
        Query(description="Trend window: 30d, 90d or all. Does not affect correlations."),
    ] = _DEFAULT_CHART_RANGE,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Six series for the evaluation summary: three trends + three correlations.

    The three headlines are decoupled, so each gets its own trend, but they
    share one window: read over different periods they would invite a false
    comparison.

    Correlation views are not windowed: a point is an agent/model pair's latest
    completed run, so a window would drop whole pairs rather than shorten a
    history.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    await _get_user_evaluation(
        db, user.id, evaluation_id, user_group_ids, is_admin=user.is_admin
    )
    if range not in _CHART_RANGES:
        raise HTTPException(
            status_code=400,
            detail=f"range must be one of {', '.join(_CHART_RANGES)}",
        )

    eid = parse_path_id(evaluation_id, not_found_detail="Evaluation not found")
    result = await db.execute(
        select(EvaluationRun)
        .where(
            EvaluationRun.evaluation_id == eid,
            EvaluationRun.status == "completed",
            # No `score.isnot(None)` filter: `score` is the LLM judge
            # headline, and requiring it would empty the chart of every
            # judge-off evaluation. A run with no value for a series is absent
            # from it, which tells "not measured" from zero.
            #
            # Model-independent (skip_llm) runs are included: the chart
            # plots per-headline measurements of an agent, and such a run is
            # measured on Overall Quality and Overall Performance like any
            # other. Excluding it could name a lower-scoring run as the leader.
            #
            # A placebo comparison's runs are not: they are trials of
            # control variants, not the agent's configurations over time.
            EvaluationRun.placebo_comparison_id.is_(None),
        )
        .order_by(EvaluationRun.created_at)
    )
    runs = list(result.scalars().all())

    days = _CHART_RANGES[range]
    if days is None:
        windowed = runs
    else:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        windowed = [
            r
            for r in runs
            if r.created_at is not None
            and (
                r.created_at
                if r.created_at.tzinfo
                else r.created_at.replace(tzinfo=UTC)
            )
            >= cutoff
        ]

    # Keyed by subject, agent or flow (#8257).
    subject_cache: dict[uuid.UUID | None, str] = {}

    async def series_key(run: EvaluationRun) -> str:
        key = subject_id(run)
        if key not in subject_cache:
            label = await subject_label(db, run)
            subject_cache[key] = (label.name or label.slug) if label else str(key)
        return f"{subject_cache[key]}/{run.model_id}"

    # --- Trends: one time series per headline, over the SAME window ---------
    trends: dict[str, list[dict]] = {name: [] for name, _ in _TREND_SERIES}
    points_by_series: dict[str, dict[str, dict]] = {
        name: {} for name, _ in _TREND_SERIES
    }
    for run in windowed:
        key = await series_key(run)
        ts_key = run.created_at.strftime("%Y-%m-%dT%H:%M")
        for name, column in _TREND_SERIES:
            value = getattr(run, column, None)
            if value is None:
                # Absent, never `0`: an unmeasured headline must not plot at
                # the bottom of the axis.
                continue
            point = points_by_series[name].setdefault(
                ts_key,
                {
                    "triggered_at": run.created_at.isoformat(),
                    "trigger_reason": run.trigger_reason,
                    "scores": {},
                },
            )
            point["scores"][key] = value
    for name, _ in _TREND_SERIES:
        trends[name] = list(points_by_series[name].values())

    # --- Correlations: one point per agent/model pair, from ONE run --------
    # Both axis values come from the same run; taking each from whichever run
    # had it would plot a pair that never existed.
    latest_by_pair: dict[tuple[uuid.UUID | None, str], EvaluationRun] = {}
    for run in runs:
        latest_by_pair[(subject_id(run), run.model_id)] = run

    correlations: dict[str, list[dict]] = {name: [] for name, _, _ in _CORRELATIONS}
    # Counted per view: a pair can be plottable on one correlation and not on
    # another.
    omitted: dict[str, int] = {name: 0 for name, _, _ in _CORRELATIONS}
    for run in latest_by_pair.values():
        key = await series_key(run)
        for name, x_col, y_col in _CORRELATIONS:
            x = getattr(run, x_col, None)
            y = getattr(run, y_col, None)
            if x is None or y is None:
                # A pair missing either axis is omitted rather than pinned to an
                # edge: a point at x=0 asserts a measurement that was not made.
                omitted[name] += 1
                continue
            correlations[name].append(
                {
                    "series": key,
                    "run_id": str(run.id),
                    "agent_id": str(run.agent_id) if run.agent_id else None,
                    "flow_id": str(run.flow_id) if run.flow_id else None,
                    "model_id": run.model_id,
                    "x": x,
                    "y": y,
                }
            )

    return {
        "range": range,
        "trends": trends,
        "correlations": correlations,
        "correlations_omitted": omitted,
        "runs_in_range": len(windowed),
        "total_runs": len(runs),
    }
