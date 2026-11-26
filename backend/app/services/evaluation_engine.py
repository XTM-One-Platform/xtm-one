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

"""Evaluation engine - executes evaluation runs in background.

Each evaluation trigger creates an agent x model matrix of runs.
Each run iterates dataset entries, executes the agent (LLM-only or full loop),
and scores the output via an LLM judge comparing against the expected answer.
"""

import asyncio
import contextlib
import functools
import json
import logging
import math
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType, SimpleNamespace
from typing import TYPE_CHECKING, Any, Final, Protocol, Self, cast

from sqlalchemy import ColumnElement, Numeric, and_, case, delete, func, or_, select, text, tuple_, update
from sqlalchemy import cast as sql_cast  # ``cast`` is taken by typing.cast above
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.json_sql import when_json_parseable
from app.core.redaction import known_secrets_scoped
from app.core.utils import format_exception_message, safe_uuid
from app.llm.attribution import provider_config_metadata
from app.llm.base import (
    ChatMessage,
    ContextOverflowError,
    StreamUsageSink,
    count_llm_retries,
)
from app.llm.cut_answers import (
    StreamStopReason,
    answer_was_cut,
    chat_retrying_a_cut,
    cut_retry_budget,
    still_cut,
)
from app.models.agent import Agent
from app.models.agent_config_version import AgentConfigVersion
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.evaluation import (
    Evaluation,
    EvaluationEpisode,
    EvaluationPlaceboComparison,
    EvaluationResult,
    EvaluationResultTrace,
    EvaluationRun,
)
from app.models.flow import Flow
from app.models.llm_trace import LLMSpan, LLMTrace
from app.models.prompt import Prompt
from app.models.prompt_version import PromptVersion
from app.models.skill_version import SkillVersion
from app.services.element_scope import agent_run_scope
from app.services.evaluation.aggregation import (
    compute_performance_headlines,
    compute_quality_headline,
    counts_in_headlines,
)
from app.services.evaluation.extraction import record_extracted_prediction
from app.services.evaluation.extraction.pipeline import (
    failed_answer_quality,
    log_extraction_failures,
    quality_with_extraction,
    tally_extraction_failure,
)
from app.services.evaluation.extractor_repository import load_extractor_for_agent
from app.services.evaluation.fingerprint import (
    compute_input_fingerprint,
    is_result_reusable,
    load_agent_kb_state,
)
from app.services.evaluation.flow_subject import (
    FLOW_EXECUTION_MODE,
    FlowEntryOutcome,
    run_flow_for_entry,
)
from app.services.evaluation.framing import (
    ANOMALY_KIND_CONFIRMATION_SEEKING,
    ANOMALY_KIND_PLATFORM_ERROR,
    ANOMALY_KIND_TRANSIENT_ERROR,
    EXECUTION_ERROR_REASONING,
    PLATFORM_ERROR_REASONING,
    classify_execution_anomaly,
    classify_raised_entry,
    detect_confirmation_seeking,
    detect_execution_error,
    fallback_input_prompt,
)
from app.services.evaluation.metric_package import (
    RETRY_COUNT_KEY,
    ChainInputs,
    PerformanceConfig,
    QualityConfig,
    QualityConfigError,
    build_quality_config,
    metric_suite_version,
    resolve_declared_vocabulary,
    run_performance,
    to_persistable,
    unscorable_outcomes,
)
from app.services.evaluation.placebo_arms import ARM_PLACEBO, ARM_REAL
from app.services.evaluation.run_mode import FALLBACK_MODE
from app.services.evaluation.run_ownership import (
    runs_owned_by_nobody,
    runs_this_execution_may_touch,
)
from app.services.evaluation.sandbox_cases import SANDBOX_KEY, agent_sandbox_case
from app.services.evaluation.sandbox_cases import case_ref as sandbox_case_ref
from app.services.evaluation.scorers.registry import judge_enabled
from app.services.evaluation.settings import SUB_SCORE_WEIGHTS
from app.services.evaluation_continuation import (
    PARKED_EPISODE_STATUSES,
    TERMINAL_EPISODE_STATUSES,
    ConversationContinuationService,
    fail_orphaned_episodes,
)
from app.services.failure_causes import FailureClassification
from app.services.integration_credentials import normalize_acting_user_id
from app.services.prompt_versioning import DEFAULT_PERSONA_SNAPSHOT_MARKER
from app.services.run_ledger import (
    CHAIN_KEY,
    EVALUATION_CHAIN_KEY,
    LEDGER_KEY,
    NO_CONTINUATION_KEY,
    ChainLedger,
    IncompleteReason,
    RunLedger,
    assemble_chain,
    uncounted_chain,
)

logger = logging.getLogger(__name__)

# Output budget of an LLM-only evaluation answer; an answer cut at it is
# retried once with twice the room and otherwise fails the case.
_LLM_ONLY_MAX_TOKENS = 4096

# A crash-path log line must stay bounded: an evaluation matrix can carry
# hundreds of runs, and the count above it is the number that matters.
_ABANDONED_LOG_SAMPLE = 20

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.llm.provider_access import ProviderRouting
    from app.models.orchestration import OrchestrationExecution
    from app.tools.base import ToolDefinition, ToolExecutor


async def _execution_deadline(
    db: AsyncSession, evaluation_id: str, *, waves: int = 1
) -> datetime:
    """Wall-clock deadline for an evaluation's orchestration execution.

    The evaluation's own budget (``timeout_seconds`` covers the whole job)
    plus a grace margin, so the reconciler does not fall back to the generic
    10-minute ``worker_job_timeout`` and dead-letter a long evaluation.

    A missing evaluation row (deleted since the caller's lookup) gets
    ``now + orchestration_grace_seconds``, never ``None``: no deadline would
    keep ``sweep_deadlines`` from ever timing the execution out, which is
    worse than timing out too early (fail-closed).

    *waves* scales the budget for a placebo comparison: its runs are
    the evaluation's arms x trials, dispatched in waves of the concurrency cap.
    """
    from datetime import timedelta

    from app.core.config import settings

    evaluation = await db.get(Evaluation, uuid.UUID(str(evaluation_id)))
    if evaluation is None:
        return datetime.now(UTC) + timedelta(
            seconds=settings.orchestration_grace_seconds
        )
    return datetime.now(UTC) + timedelta(
        seconds=(evaluation.timeout_seconds or 3600) * max(waves, 1)
        + settings.orchestration_grace_seconds
    )


async def stage_evaluation_execution(
    db: AsyncSession, evaluation_id: str, trigger_reason: str = "manual", *, waves: int = 1
) -> "OrchestrationExecution":
    """Stage the orchestration execution for an evaluation trigger.

    Adds the execution to *db*'s own open transaction via
    ``create_execution()`` (flush only, never commits). Callers MUST fold it
    into the same transaction as their run-creation or status-flip commit:

        run = EvaluationRun(...)
        db.add(run)
        execution = await stage_evaluation_execution(db, str(evaluation.id))
        run.owner_execution_id = execution.id
        await db.commit()
        await notify_orchestration_wake()

    so a crash can never persist a run's active status without the execution
    that alone can dispatch, complete or fail it. Dead-letter handling assumes
    every active run has an execution, and there is no periodic sweep to
    catch one that does not.

    Returns the staged execution (flushed, NOT committed) so the caller can
    stamp ``EvaluationRun.owner_execution_id`` in that same transaction.
    A run's owner is then exactly as durable as its active status,
    which is what lets an abandoned execution's cleanup tell its own runs from
    ones a later attempt has taken over.
    """
    from app.services.orchestration_engine import create_execution

    deadline_at = await _execution_deadline(db, evaluation_id, waves=waves)
    return await create_execution(
        db,
        adapter="evaluation",
        payload={
            "evaluation_id": str(evaluation_id),
            "trigger_reason": trigger_reason,
        },
        deadline_at=deadline_at,
    )


async def enqueue_evaluation(
    evaluation_id: str, trigger_reason: str = "manual"
) -> None:
    """Schedule an evaluation through the durable orchestration engine.

    The reconciler claims the execution and runs ``execute_evaluation`` via
    the ``EvaluationAdapter``.

    Callers MUST NOT create ``EvaluationRun`` rows: the execution is
    committed on a session the caller cannot see, so a run created alongside
    could never be stamped with its id and would be committed active with
    ``owner_execution_id = NULL``, failable by any other execution's cleanup.
    A caller that creates runs uses ``stage_evaluation_execution``
    and folds it into its own commit.

    Opens its own short-lived session so it cannot commit or roll back a
    caller's unrelated pending changes, then commits the execution and wakes
    the reconciler.
    """
    from app.core.database import async_session
    from app.services.orchestration_engine import notify_orchestration_wake

    async with async_session() as session:
        await stage_evaluation_execution(session, evaluation_id, trigger_reason)
        await session.commit()

    await notify_orchestration_wake()


#: The rescore payload key holding each run's status before the rescore set it ``"running"``:
#: a rescore re-judges stored answers and hands the run back as it found it.
RESCORE_PREVIOUS_STATUSES = "previous_statuses"


def rescore_previous_status(payload: Mapping[str, Any], run_id: object) -> str:
    """The status *run_id* had before the rescore staged with *payload*.

    ``stage_rescore_execution`` records it for every run it stages.
    """
    return payload[RESCORE_PREVIOUS_STATUSES][str(run_id)]


async def stage_rescore_execution(
    db: AsyncSession,
    evaluation_id: str,
    run_ids: list[str],
    *,
    previous_statuses: Mapping[str, str],
) -> "OrchestrationExecution":
    """Stage the orchestration execution for a background rescore.

    Same contract as ``stage_evaluation_execution``: the caller stamps the
    returned execution onto each run as its owner and flips the runs
    to ``"running"`` in the same ``db.commit()``.

    *previous_statuses* maps each run id to its status before that flip: whatever
    happens to the rescore, the run gets it back.
    """
    from app.services.orchestration_engine import create_execution

    deadline_at = await _execution_deadline(db, evaluation_id)
    return await create_execution(
        db,
        adapter="evaluation",
        payload={
            "kind": "rescore_runs",
            "run_ids": run_ids,
            RESCORE_PREVIOUS_STATUSES: {rid: previous_statuses[rid] for rid in run_ids},
        },
        deadline_at=deadline_at,
    )


class IntegrationToolsBundle(tuple):
    """An evaluation's integration tools, built once for its entries: the pair
    ``(definitions, executor)`` it always was, and on ``context`` what the build collected
    for the agent's built-in tools (``integration_tools.PLATFORM_NAMES_CONTEXT_KEY``,
    ``ATTACK_SOURCES_CONTEXT_KEY``), which an evaluated run reads as a chat run does.

    ``bindings`` / ``acting_user_id``: what it was built from and for whom, which
    ``integration_tools.prebuilt_tools_refusal`` checks before a turn uses it."""

    context: Mapping[str, list]
    bindings: Mapping[str, str] | None
    acting_user_id: str | None

    def __new__(
        cls,
        definitions: list[ToolDefinition],
        executor: ToolExecutor,
        context: Mapping[str, list] | None = None,
        *,
        bindings: Mapping[str, str] | None = None,
        acting_user_id: str | None = None,
    ) -> Self:
        bundle = super().__new__(cls, (definitions, executor))
        bundle.context = context if context is not None else MappingProxyType({})
        bundle.bindings = bindings
        bundle.acting_user_id = acting_user_id
        return bundle


class JudgeEntryLike(Protocol):
    id: uuid.UUID
    input_prompt: str | None
    expected_answer: str
    evaluation_prompt: str | None


def _integration_providers_from_tool_defs(defs: list["ToolDefinition"]) -> set[str]:
    """Extract integration provider ids from tool definitions.

    Integration tools carry ``source='integration:<provider>'`` and a channel
    bot's carry ``source='channel_bot:<provider>:<id>'``; other tool
    sources are ignored.
    """
    providers: set[str] = set()
    for td in defs:
        source = getattr(td, "source", None)
        if isinstance(source, str) and source.startswith("channel_bot:"):
            parts = source.split(":")
            if len(parts) >= 2 and parts[1]:
                providers.add(parts[1])
            continue
        if not isinstance(source, str) or not source.startswith("integration:"):
            continue
        provider = source.split(":", 1)[1].strip()
        if provider:
            providers.add(provider)
    return providers


def _build_judge_verdict_schema() -> dict:
    """The single judge-verdict JSON Schema, derived from ``SUB_SCORE_WEIGHTS``.

    One source of truth for provider-native enforcement
    (``build_native_structured_output``), the prompt-side fallback
    (``build_schema_prompt``) and the repair-aware parse
    (``validate_and_parse``). Sub-scores live under a nested ``sub_scores``
    object built from ``SUB_SCORE_WEIGHTS``, so the schema cannot drift from
    the weighting; ``reasoning`` is a top-level string.
    """
    sub_keys = list(SUB_SCORE_WEIGHTS)
    return {
        "type": "object",
        "properties": {
            "sub_scores": {
                "type": "object",
                "properties": {
                    key: {"type": "integer", "minimum": 0, "maximum": 100}
                    for key in sub_keys
                },
                "required": list(sub_keys),
                "additionalProperties": False,
            },
            "reasoning": {"type": "string"},
        },
        "required": ["sub_scores", "reasoning"],
        "additionalProperties": False,
    }


_JUDGE_VERDICT_SCHEMA = _build_judge_verdict_schema()

JUDGE_PROMPT_SKILL_SLUG = "evaluation-judge-system-prompt"


def _extract_skill_slug(tags: list | None) -> str | None:
    """Extract the seed slug marker from a skill tags list."""
    for tag in tags or []:
        if isinstance(tag, str) and tag.startswith("slug:"):
            return tag[5:]
    return None


async def _load_default_judge_prompt_from_skill(db: AsyncSession) -> str:
    """Load the default judge prompt from the seeded built-in skill."""
    from app.models.agent_skill import AgentSkill

    result = await db.execute(
        select(AgentSkill).where(
            AgentSkill.is_company_managed.is_(True),
            AgentSkill.enabled.is_(True),
            AgentSkill.content.isnot(None),
        )
    )

    for skill in result.scalars().all():
        if _extract_skill_slug(skill.tags) == JUDGE_PROMPT_SKILL_SLUG and skill.content:
            return skill.content

    raise ValueError(
        "Default evaluation judge skill not found: "
        f"slug '{JUDGE_PROMPT_SKILL_SLUG}'. "
        "Seed built-in skills or set evaluation.evaluator_prompt explicitly."
    )


def _build_judge_user_prompt(
    input_prompt: str,
    agent_output: str,
    expected_answer: str,
    evaluation_prompt: str | None = None,
    available_tools: list[str] | None = None,
    tool_calls: list[dict] | None = None,
) -> str:
    """Build the user message for the LLM judge."""
    parts = [
        f"## INPUT\n{input_prompt}",
        f"## AGENT OUTPUT\n{agent_output}",
        f"## EXPECTED ANSWER\n{expected_answer}",
    ]

    if available_tools is not None:
        if available_tools:
            parts.append(f"## AVAILABLE TOOLS\n{', '.join(available_tools)}")
        else:
            parts.append(
                "## AVAILABLE TOOLS\nNone (no tools were available to the agent)"
            )

    if tool_calls is not None:
        if tool_calls:
            call_lines = []
            for tc in tool_calls:
                name = tc.get("tool_name", "unknown")
                dur = tc.get("duration_ms", 0)
                desc = tc.get("description", "")
                call_lines.append(f"- **{name}** ({dur}ms): {desc}")
            parts.append("## TOOL CALLS MADE\n" + "\n".join(call_lines))
        else:
            parts.append("## TOOL CALLS MADE\nNone (the agent did not use any tools)")

    if evaluation_prompt:
        parts.append(f"## EVALUATION INSTRUCTIONS\n{evaluation_prompt}")
    return "\n\n".join(parts)


# The tools the agent had when the entry ran, kept with its result so reuse,
# rescore and judge-only replay judge it against the same list.
AVAILABLE_TOOLS_KEY = "available_tools"


def _recorded_available_tools(execution_details: dict | None) -> list[str] | None:
    """The agent's tool list stored with a result, ``None`` when unknown.

    A result recorded before the list was stored has none, unless it ran in LLM
    Only, which offers no tool whatever its age.
    """
    details = execution_details or {}
    recorded = details.get(AVAILABLE_TOOLS_KEY)
    if isinstance(recorded, list):
        return [name for name in recorded if isinstance(name, str) and name]
    if details.get("execution_mode") in ("llm_only", FALLBACK_MODE):
        return []
    return None


# --- What the judge reads, the same on every path ---------------------------------


def judge_instructions(
    evaluation_instructions: str | None, dataset_instructions: str | None
) -> str | None:
    """The global instructions the judge reads: the evaluation's, else the dataset's. An
    entry's own evaluation prompt wins over both (``score_result``)."""
    return evaluation_instructions or dataset_instructions


def _agent_timeline(traces: object) -> list[dict]:
    """The agent's side of a timeline, without the judge step stored after it."""
    if not isinstance(traces, list):
        return []
    return [
        t for t in traces if isinstance(t, dict) and t.get("type") not in ("judge", "judge_skipped")
    ]


async def judge_tool_calls(
    db: AsyncSession, traces: object, trace_ids: Sequence[str]
) -> list[dict]:
    """The tool calls the judge sees: the agent timeline's tool steps, or the linked
    trace spans when there is no timeline (a timeline without a tool step means the
    agent used none)."""
    timeline = _agent_timeline(traces)
    if not timeline and trace_ids:
        return await _load_tool_calls_from_trace_ids(db, list(trace_ids))
    return [t for t in timeline if t.get("type") == "tool"]


async def scored_entry(
    db: AsyncSession, result: EvaluationResult
) -> tuple[Any | None, uuid.UUID | None]:
    """The entry *result* was scored against, its dataset snapshot's first, else the live
    one, and that entry's dataset id; ``(None, None)`` when neither exists any more."""
    from app.models.dataset_version import DatasetVersion, DatasetVersionEntry

    if result.version_entry_id is not None:
        snapshot = await db.get(DatasetVersionEntry, result.version_entry_id)
        if snapshot is not None:
            version = await db.get(DatasetVersion, snapshot.version_id)
            return snapshot, version.dataset_id if version is not None else None
    if result.entry_id is not None:
        live = await db.get(DatasetEntry, result.entry_id)
        if live is not None:
            return live, live.dataset_id
    return None, None


@dataclass(frozen=True)
class StoredJudgeInputs:
    """What the judge reads to score a stored result again."""

    entry: JudgeEntryLike | None
    instructions: str | None
    available_tools: list[str] | None
    tool_calls: list[dict]


async def stored_judge_inputs(
    db: AsyncSession,
    evaluation: Evaluation,
    result: EvaluationResult,
    resolved: tuple[Any | None, uuid.UUID | None] | None = None,
) -> StoredJudgeInputs:
    """The judge's inputs for a stored answer (a rescore, a judge-only replay or a resumed
    callback), as the fresh pass built them: the entry it was scored against, the same
    instructions, the tools the agent had and the tool calls of its stored
    timeline. *resolved* is ``scored_entry``'s answer when the caller already has it."""
    entry, dataset_id = resolved if resolved is not None else await scored_entry(db, result)
    dataset = await db.get(Dataset, dataset_id) if dataset_id is not None else None
    details = result.execution_details or {}
    trace_ids: list[str] = [str(t) for t in (details.get("trace_ids") or [])]
    if not trace_ids and not _agent_timeline(details.get("traces")):
        linked = await db.execute(
            select(EvaluationResultTrace.trace_id)
            .where(
                EvaluationResultTrace.result_id == result.id,
                EvaluationResultTrace.trace_role.in_(["agent", "auxiliary"]),
            )
            .order_by(EvaluationResultTrace.trace_order.asc())
        )
        trace_ids = [str(trace_id) for trace_id in linked.scalars().all()]
    judge_entry = (
        cast(
            JudgeEntryLike,
            SimpleNamespace(
                id=result.entry_id,
                # The fresh pass's prompt, its fallback for an empty one included.
                input_prompt=getattr(entry, "input_prompt", None)
                or fallback_input_prompt(bool(getattr(entry, "file_ids", None))),
                expected_answer=getattr(entry, "expected_answer", "") or "",
                evaluation_prompt=getattr(entry, "evaluation_prompt", None),
            ),
        )
        if entry is not None
        else None
    )
    return StoredJudgeInputs(
        entry=judge_entry,
        instructions=judge_instructions(
            evaluation.evaluation_instructions,
            getattr(dataset, "evaluation_instructions", None),
        ),
        available_tools=_recorded_available_tools(details),
        tool_calls=await judge_tool_calls(db, details.get("traces"), trace_ids),
    )


async def _run_entry_records(
    db: AsyncSession, run: EvaluationRun, evaluation: Evaluation
) -> tuple[list[Any], list[dict]]:
    """The entries a run scores and their records: its pinned snapshots, else the live
    entries. One loader for the fresh pass and a resumed callback, so both build
    the same quality config."""
    # Pinned runs score against the immutable version snapshot taken at
    # trigger time, so editing live entries never changes past results.
    # Runs with no pinned_dataset_versions read the live entries.
    pinned_versions = getattr(run, "pinned_dataset_versions", None) or None
    if pinned_versions:
        from app.services.evaluation import dataset_versioning

        pinned_rows = await dataset_versioning.load_pinned_entries(
            db, evaluation.id, pinned_versions
        )
        entries = [ve for ve, _, _, _ in pinned_rows]
        entry_records = [
            {
                "id": ve.entry_id,
                "version_entry_id": ve.id,
                "input_prompt": ve.input_prompt or fallback_input_prompt(bool(ve.file_ids)),
                "file_ids": ve.file_ids,
                "expected_answer": ve.expected_answer or "",
                "evaluation_prompt": ve.evaluation_prompt,
                "output_schema": getattr(ve, "output_schema", None),
                # The SNAPSHOT's dataset-level shape, not the live one:
                # a pinned run stays interpretable after the dataset moves on.
                "dataset_output_schema": version_schema,
                # Same for the CLASSIFICATION label vocabulary.
                "dataset_label_vocabulary": version_vocabulary,
                "dataset_instructions": instructions,
            }
            for ve, instructions, version_schema, version_vocabulary in pinned_rows
        ]
    else:
        entries_result = await db.execute(
            select(
                DatasetEntry,
                Dataset.evaluation_instructions.label("dataset_instructions"),
                Dataset.output_schema.label("dataset_output_schema"),
                Dataset.label_vocabulary.label("dataset_label_vocabulary"),
            )
            .join(EvaluationDataset, EvaluationDataset.dataset_id == DatasetEntry.dataset_id)
            .join(Dataset, Dataset.id == DatasetEntry.dataset_id)
            .where(EvaluationDataset.evaluation_id == evaluation.id)
            .order_by(EvaluationDataset.position, DatasetEntry.position)
        )
        entry_rows = entries_result.all()
        entries = [row[0] for row in entry_rows]
        entry_records = [
            {
                "id": row[0].id,
                "version_entry_id": None,
                "input_prompt": row[0].input_prompt or fallback_input_prompt(bool(row[0].file_ids)),
                "file_ids": row[0].file_ids,
                "expected_answer": row[0].expected_answer or "",
                "evaluation_prompt": row[0].evaluation_prompt,
                "output_schema": getattr(row[0], "output_schema", None),
                # An unpinned run has no snapshot, so it reads the live dataset.
                "dataset_output_schema": row[2],
                "dataset_label_vocabulary": row[3],
                "dataset_instructions": row[1],
            }
            for row in entry_rows
        ]
    return entries, entry_records


def _run_quality_config(
    run: EvaluationRun, evaluation: Evaluation, entry_records: list[dict]
) -> tuple[Any | None, Exception | None]:
    """The run's quality config and the error that stopped it, built ONCE per run from
    every entry's reference: CLASSIFICATION derives its vocabulary from all of them.
    A configuration error degrades to judge-only, never a crash or a fabricated
    score. ``(None, None)`` for an evaluation without a family."""
    family = getattr(evaluation, "family", None)
    if not family:
        return None, None
    dataset_refs = [rec.get("expected_answer") or "" for rec in entry_records]
    try:
        # Declared CLASSIFICATION vocabulary: an evaluation can link several
        # datasets, so this is the UNION of each entry's dataset vocabulary, with the
        # derived fallback for entries whose dataset declared none. Never "first found
        # wins", which drops the other datasets' declarations and can invalidate an
        # undeclared dataset's entries.
        declared_vocabulary = resolve_declared_vocabulary(
            [rec.get("dataset_label_vocabulary") for rec in entry_records],
            dataset_refs,
        )
        return build_quality_config(family, evaluation, dataset_refs, declared_vocabulary), None
    except (QualityConfigError, ValueError) as exc:
        logger.warning(
            "Quality config unavailable for run %s (family=%s): %s",
            run.id,
            family,
            exc,
        )
        return None, exc


def _execution_record(
    *,
    agent_duration_ms: int,
    agent_wall_clock_ms: int | None,
    ttft_ms: object,
    execution_mode: str | None,
    agent_traces: list[dict],
    trace_ids: list[str],
    metric_results: dict[str, dict],
    chain: ChainLedger | None,
    available_tools: list[str] | None,
    held_tools: list[str],
    judge_duration_ms: int | None = None,
    judge_trace: dict | None = None,
) -> dict:
    """The ``execution_details`` an entry's result stores: the fresh pass's, and a
    callback entry's first turn without its judge, so both keep one shape."""
    details: dict = {
        "total_duration_ms": agent_duration_ms + (judge_duration_ms or 0),
        "agent_duration_ms": agent_duration_ms,
        "agent_wall_clock_ms": agent_wall_clock_ms,
        **({"judge_duration_ms": judge_duration_ms} if judge_duration_ms is not None else {}),
        "ttft_ms": ttft_ms,
        "execution_mode": execution_mode,
        "traces": [*agent_traces, *([judge_trace] if judge_trace is not None else [])],
        "trace_ids": trace_ids,
        "metric_results": metric_results,
    }
    if chain is not None:
        details[CHAIN_KEY] = chain
    if available_tools is not None:
        details[AVAILABLE_TOOLS_KEY] = available_tools
    if held_tools:
        details["approval_required_tools"] = held_tools
    return details


def _compute_global_score(
    sub_scores: dict, weights: dict[str, float] | None = None
) -> float:
    """Compute weighted global score from sub-scores."""
    w = weights or SUB_SCORE_WEIGHTS
    total = 0.0
    for key, weight in w.items():
        total += sub_scores.get(key, 0) * weight
    return round(total, 2)


def _parse_judge_response(text: str) -> dict:
    """Parse the LLM judge response into sub-scores and reasoning.

    Runs the shared Schema-Aligned Parsing pipeline (``validate_and_parse``):
    the verdict is extracted from fences or prose, JSON slips are repaired and
    values coerced, so a complete but malformed verdict is still scored.
    Sub-scores come from the nested ``sub_scores`` object, ``reasoning`` from
    the top level.

    A complete verdict has every ``SUB_SCORE_WEIGHTS`` key int-coercible under
    ``sub_scores``. Missing or invalid keys go to ``missing_keys``, never
    filled with a neutral 50, so the caller records one explicit judge failure.
    """
    from app.services.structured_output import validate_and_parse

    data, _errors = validate_and_parse(
        text or "", _JUDGE_VERDICT_SCHEMA, allow_partial=True
    )
    if not isinstance(data, dict):
        logger.warning("Failed to parse judge response as a JSON verdict")
        return {
            "sub_scores": {k: 0 for k in SUB_SCORE_WEIGHTS},
            "reasoning": f"Failed to parse judge response: {text[:200]}",
            "parse_error": True,
            "missing_keys": list(SUB_SCORE_WEIGHTS),
        }

    raw_sub_scores = data.get("sub_scores")
    if not isinstance(raw_sub_scores, dict):
        # Wrong shape (flat keys, a non-object): every sub-score is missing, so
        # it becomes one explicit judge failure, not a fabricated all-zero score.
        raw_sub_scores = {}

    sub_scores: dict[str, int] = {}
    missing_keys: list[str] = []
    for key in SUB_SCORE_WEIGHTS:
        raw = raw_sub_scores.get(key)
        try:
            if raw is None:
                raise ValueError
            sub_scores[key] = max(0, min(100, int(raw)))
        except (ValueError, TypeError):
            sub_scores[key] = 0
            missing_keys.append(key)

    return {
        "sub_scores": sub_scores,
        "reasoning": data.get("reasoning", "") or "",
        "parse_error": False,
        "missing_keys": missing_keys,
    }


async def _get_llm_provider_and_model(
    db: AsyncSession, model_id: str, *, user_id: str | None = None
):
    """Resolve an LLM provider for a given model ID, among *user_id*'s."""
    from app.llm.router import resolve_provider_for_model

    return await resolve_provider_for_model(db, model_id, user_id=user_id)


def evaluation_owner_id(evaluation: object) -> str | None:
    """The user an evaluation's LLM calls run as: its owner, else no account."""
    owner = getattr(evaluation, "user_id", None) if evaluation is not None else None
    return str(owner) if owner else None


async def owner_model_routing(db: AsyncSession, owner_id: str | None) -> ProviderRouting:
    """The providers an evaluation's runs resolve their models among: its owner's.

    An evaluation with no owner runs with nobody acting (the worker binds no
    one), so the person bound to the current request, the admin who triggers,
    must not stand in for it.
    """
    from app.llm.provider_access import acting_for, provider_routing
    from app.llm.router import select_provider_configs

    configs = list((await db.execute(select_provider_configs())).scalars().all())
    with acting_for(owner_id):
        return await provider_routing(db, owner_id, configs)


def model_served(routing: ProviderRouting, model_id: str) -> bool:
    """Whether a run on *model_id* is answered by that model.

    A chat model a provider of *routing* owns, by its catalog or its vendor
    prefix (``owner_for``), the rule ``get_llm_for_user`` picks a model's
    provider by. Any other model is not answered by itself: a non-chat or
    reserved one is skipped for the next tier, and a chat model no provider
    owns is sent to a provider that does not serve it: the call fails, or fails
    over to another model when a fallback is configured.
    """
    from app.llm.router import is_chat_capable_model

    return is_chat_capable_model(model_id) and routing.owner_for(model_id) is not None


async def run_model_served(db: AsyncSession, model_id: str, owner_id: str | None) -> bool:
    """:func:`model_served` for one run on *owner_id*'s providers, checked before it calls its model."""
    return model_served(await owner_model_routing(db, owner_id), model_id)


async def unserved_model_ids(
    db: AsyncSession,
    evaluation: object,
    model_ids: Iterable[str],
    *,
    routing: ProviderRouting | None = None,
) -> list[str]:
    """The models of *model_ids* :func:`model_served` refuses on *evaluation*'s owner's providers.

    Once each, in order. A launch refuses them, a save refuses a new
    one and the evaluation's response flags a saved one. *routing* is
    the owner's, when the caller already read it; with no model, nothing is read.
    """
    candidates = list(dict.fromkeys(model_ids))
    if not candidates:
        return []
    if routing is None:
        routing = await owner_model_routing(db, evaluation_owner_id(evaluation))
    return [mid for mid in candidates if not model_served(routing, mid)]


def unavailable_model_message(model_ids: list[str]) -> str:
    """Why a run on *model_ids* is refused."""
    if not model_ids:
        raise ValueError("unavailable_model_message needs at least one model")
    if len(model_ids) > 1:
        return (
            f"Models {', '.join(model_ids)} are not chat models, or no provider "
            "this evaluation can use offers them"
        )
    return f"Model {model_ids[0]} is not a chat model, or no provider this evaluation can use offers it"


async def _get_evaluator_llm(
    db: AsyncSession, evaluator_model: str | None, *, user_id: str | None = None
):
    """Resolve the LLM provider for the evaluator judge.

    Returns (llm, model_id, provider_name) — provider_name is used to surface
    judge execution details in the UI. The judge runs on a provider *user_id*,
    the evaluation's owner, may use.
    """
    from app.llm.router import get_platform_default_model

    model = evaluator_model
    if not model:
        model = await get_platform_default_model(db)
        if not model:
            raise ValueError("No platform default model configured for evaluation judge")

    llm, resolved_model = await _get_llm_provider_and_model(db, model, user_id=user_id)
    provider_name = getattr(llm, "provider_name", None) or "unknown"
    return llm, resolved_model, provider_name


def _ms(start: float, end: float) -> int:
    """Convert perf_counter delta to milliseconds."""
    return round((end - start) * 1000)


def _is_count(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _add_usage(total: dict, usage: dict | None) -> None:
    """Fold one request's usage into *total*: counts added, a breakdown map
    (``prompt_tokens_details``, ...) added key by key, anything else the latest."""
    for key, value in (usage or {}).items():
        before = total.get(key)
        if isinstance(value, dict) and isinstance(before, dict):
            merged = dict(before)
            _add_usage(merged, value)
            total[key] = merged
        elif _is_count(value) and _is_count(before):
            total[key] = before + value
        else:
            total[key] = value


def _build_judge_trace(
    score_data: dict,
    judge_duration_ms: int,
    evaluator_model: str | None,
    *,
    judge_ran: bool,
    skip_reason: str | None = None,
) -> dict:
    """Build the judge step for ``execution_details['traces']``.

    A judge that ran gets the full ``judge`` trace (model, prompts, usage). A
    skipped one (an execution anomaly, or the judge scorer deselected) gets a
    ``judge_skipped`` marker, so the timeline and trace analytics never show a
    judge call that did not happen.
    """
    if not judge_ran:
        return {
            "type": "judge_skipped",
            "description": skip_reason or "Judge scoring skipped",
            "duration_ms": judge_duration_ms,
            "skipped": True,
        }
    return {
        "type": "judge",
        "description": "LLM Judge — comparing agent output against expected answer",
        "duration_ms": judge_duration_ms,
        "model": score_data.get("judge_model", evaluator_model or "platform_default"),
        "provider": score_data.get("judge_provider"),
        "stop_reason": score_data.get("judge_stop_reason"),
        "truncated": score_data.get("judge_truncated", False),
        "is_judge_failure": score_data.get("is_judge_failure", False),
        "usage": score_data.get("judge_usage") or {},
        "system_prompt": score_data.get("judge_system_prompt"),
        "user_prompt": score_data.get("judge_user_prompt"),
        "raw_response": score_data.get("judge_raw_response"),
    }


def with_judge_pass(
    execution_details: dict | None,
    score_data: dict,
    judge_duration_ms: int,
    evaluator_model: str | None,
) -> dict:
    """*execution_details* after a judge re-ran on a stored answer (a rescore or a
    replay): its Judge Time, and its timeline's judge step replaced by this pass's, so
    the verdict on display and the step that explains it agree. A new dict:
    SQLAlchemy tracks JSON by identity."""
    details = dict(execution_details or {})
    details["judge_duration_ms"] = judge_duration_ms
    details["traces"] = [
        *_agent_timeline(details.get("traces")),
        _build_judge_trace(score_data, judge_duration_ms, evaluator_model, judge_ran=True),
    ]
    return details


async def _sum_trace_durations_ms(db: AsyncSession, trace_ids: list[str]) -> int:
    """Return the sum of durations for the given trace IDs."""
    parsed_ids: list[uuid.UUID] = []
    for trace_id in trace_ids:
        try:
            parsed_ids.append(uuid.UUID(trace_id))
        except (TypeError, ValueError):
            logger.debug("Ignoring invalid trace id while summing durations: %r", trace_id)
            continue
    if not parsed_ids:
        return 0

    result = await db.execute(
        select(func.coalesce(func.sum(LLMTrace.duration_ms), 0)).where(
            LLMTrace.id.in_(parsed_ids)
        )
    )
    return int(result.scalar() or 0)


# Cache buckets as they appear in a persisted span's ``usage_details``, grouped by the
# convention the trace-level ``input`` figure was built under (see ``_normalize_usage``):
#   * Anthropic-style top-level buckets are ADDED into ``input`` -> inclusive
#   * OpenAI-style flattened detail keys are SUBTRACTED out of ``input`` -> separate
_INCLUSIVE_CACHE_KEYS = {
    "cache_read_input_tokens": "cache_read_input_tokens",
    "cache_creation_input_tokens": "cache_write_input_tokens",
}
_SEPARATE_CACHE_KEYS = {
    "input_cached_tokens": "cache_read_input_tokens",
    "input_cache_read_tokens": "cache_read_input_tokens",
    "input_cache_creation_tokens": "cache_write_input_tokens",
    "input_cache_write_tokens": "cache_write_input_tokens",
}
_REASONING_KEYS = ("output_reasoning_tokens",)


def _usage_detail_count(value: object) -> int | None:
    """Coerce a persisted usage-detail value to a non-negative count, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    return int(value)


async def _sum_agent_token_categories(
    db: AsyncSession, parsed_ids: list[uuid.UUID]
) -> tuple[dict[str, int], str | None]:
    """Sum the cache and reasoning token buckets recorded on the agent's spans.

    The trace row carries only the two headline figures; the breakdown lives in
    each span's ``usage_details``. Returns ``({bucket: count}, convention)`` with
    only the buckets some span reported, so an unreported category stays
    distinct from a measured zero.
    """
    result = await db.execute(
        select(LLMSpan.usage_details).where(LLMSpan.trace_id.in_(parsed_ids))
    )
    return _accumulate_token_categories(details for (details,) in result.all())


def _accumulate_token_categories(
    details_iter: Iterable[object],
) -> tuple[dict[str, int], str | None]:
    """Classify a stream of ``usage_details`` dicts into token category buckets.

    Shared by the persisted-span path and the in-memory timeline path so the two can
    never disagree about what a provider's cache keys mean.

    ``convention`` is ``None`` when no cache bucket was seen, and also when spans reported
    cache under BOTH conventions: the two describe opposite relationships to the same input
    figure, so picking either would silently double-count or under-count. Reasoning tokens
    are convention-independent and are always kept.
    """
    buckets: dict[str, int] = {}
    conventions: set[str] = set()

    def add(bucket: str, count: int) -> None:
        buckets[bucket] = buckets.get(bucket, 0) + count

    for details in details_iter:
        if not isinstance(details, dict):
            continue
        for source, bucket in _INCLUSIVE_CACHE_KEYS.items():
            count = _usage_detail_count(details.get(source))
            if count is not None:
                add(bucket, count)
                conventions.add("inclusive")
        for source, bucket in _SEPARATE_CACHE_KEYS.items():
            count = _usage_detail_count(details.get(source))
            if count is not None:
                add(bucket, count)
                conventions.add("separate")
        for source in _REASONING_KEYS:
            count = _usage_detail_count(details.get(source))
            if count is not None:
                add("reasoning_output_tokens", count)

    if len(conventions) > 1:
        logger.debug(
            "Agent spans reported cache tokens under mixed conventions %s; "
            "reporting the cache buckets as uncaptured",
            sorted(conventions),
        )
        buckets.pop("cache_read_input_tokens", None)
        buckets.pop("cache_write_input_tokens", None)
        return buckets, None

    return buckets, next(iter(conventions), None)


async def _sum_agent_token_usage(
    db: AsyncSession, trace_ids: list[str]
) -> dict[str, object] | None:
    """Sum token usage across the given agent trace IDs, by category.

    Returns the raw buckets the performance adapter consumes: always
    ``input_tokens`` / ``output_tokens``, plus ``cache_read_input_tokens``,
    ``cache_write_input_tokens`` and ``reasoning_output_tokens`` when reported,
    and the ``convention`` relating the cache buckets to ``input_tokens``. An
    unreported category is ABSENT, not ``0``: caching never used and caching
    not observed are different facts, and a measured zero for the second would
    make an unmeasurable saving look real.

    Returns ``None`` when no valid trace id resolves any usage. The judge trace
    is never in ``agent_trace_ids``, so cost measures the agent, not the judge.
    """
    parsed_ids: list[uuid.UUID] = []
    for trace_id in trace_ids:
        try:
            parsed_ids.append(uuid.UUID(trace_id))
        except (TypeError, ValueError):
            logger.debug("Ignoring invalid trace id while summing tokens: %r", trace_id)
            continue
    if not parsed_ids:
        return None

    result = await db.execute(
        select(
            func.coalesce(func.sum(LLMTrace.input_tokens), 0),
            func.coalesce(func.sum(LLMTrace.output_tokens), 0),
        ).where(LLMTrace.id.in_(parsed_ids))
    )
    row = result.first()
    if row is None:
        return None
    input_tokens = int(row[0] or 0)
    output_tokens = int(row[1] or 0)
    if input_tokens == 0 and output_tokens == 0:
        return None

    usage: dict[str, object] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    try:
        buckets, convention = await _sum_agent_token_categories(db, parsed_ids)
    except Exception:
        # The breakdown is an enrichment: its failure keeps the two headline figures.
        logger.debug("Failed to sum agent token categories", exc_info=True)
        return usage
    usage.update(buckets)
    if convention is not None:
        usage["convention"] = convention
    return usage


def _sum_local_token_usage(local_traces: object) -> dict[str, object] | None:
    """Sum token usage from the step timeline the run captured in-process.

    Each ``type == "llm"`` step carries the provider's raw usage dict.
    Normalizing it through ``usage_detail_buckets`` yields the keys the matching
    ``LLMSpan.usage_details`` row will hold, so both token paths agree.

    This matters for providers that report only the UNCACHED portion as
    ``input_tokens`` (Anthropic): ignoring the cache buckets reads a 150k-token
    call as 2 tokens.

    Returns ``None`` when no LLM step reported tokens, never a zero that would
    make an unobserved call look free.
    """
    if not isinstance(local_traces, list):
        return None

    from app.services.langfuse_tracing import usage_detail_buckets

    normalized: list[dict[str, int]] = []
    for step in local_traces:
        if not isinstance(step, dict) or step.get("type") != "llm":
            continue
        buckets = usage_detail_buckets(
            step.get("usage") if isinstance(step.get("usage"), dict) else None
        )
        if buckets:
            normalized.append(buckets)

    return _token_usage_from_buckets(normalized)


def _token_usage_from_buckets(normalized: list[dict[str, int]]) -> dict[str, object] | None:
    """The adapter's ``token_usage`` from span-normalised usage dicts.

    Shared by the in-memory timeline path and the chain ledger, whose
    per-model usage is already normalised span usage. ``None`` when no dict
    reported a token.
    """
    input_tokens = sum(int(b.get("input", 0)) for b in normalized)
    output_tokens = sum(int(b.get("output", 0)) for b in normalized)
    if input_tokens == 0 and output_tokens == 0:
        return None

    usage: dict[str, object] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    categories, convention = _accumulate_token_categories(normalized)
    usage.update(categories)
    if convention is not None:
        usage["convention"] = convention
    return usage


async def _resolve_agent_token_usage(
    db: AsyncSession, local_traces: object, trace_ids: list[str]
) -> dict[str, object] | None:
    """Resolve the agent's token usage, preferring the in-memory step timeline.

    The tracing dispatcher writes ``llm_traces`` and ``llm_spans`` from a
    background worker, seconds after an entry is scored, so reading the DB
    first reports ``token_usage_not_captured`` for fully measured runs.

    The in-memory timeline holds the same numbers with no flush to wait on,
    and reuse carries it forward (a reused prediction inherits its source's
    token cost, duration and retry count). The DB is the fallback for paths
    with no local timeline.
    """
    usage = _sum_local_token_usage(local_traces)
    if usage is not None:
        return usage
    if not trace_ids:
        return None
    return await _sum_agent_token_usage(db, trace_ids)


async def _build_run_price_rows(
    db: AsyncSession, model_id: str
) -> list[dict[str, object]]:
    """Resolve the platform price row for ``model_id`` in the shape the
    performance adapter consumes (per-1M keys + currency/resolved names).

    Returns an empty list when no price is known: the adapter then reports the
    cost metric ``unavailable`` rather than a fabricated zero.
    """
    from app.services.tracing.cost_calculator import resolve_model_price_any

    price = await resolve_model_price_any(db, model_id)
    if price is None:
        return []
    return [
        {
            "input_price_per_1m": price.input_price_per_1m,
            "output_price_per_1m": price.output_price_per_1m,
            "cache_read_price_per_1m": price.cache_read_price_per_1m,
            "cache_write_price_per_1m": price.cache_write_price_per_1m,
            "currency": "USD",
            "resolved_model": price.model_id,
            "resolved_provider": price.provider,
        }
    ]


async def _chain_inputs(
    db: AsyncSession,
    chain: ChainLedger,
    run_model_id: str | None,
    run_price_rows: list[dict[str, object]],
    price_rows_cache: dict[str, list[dict[str, object]]],
) -> ChainInputs:
    """The performance adapter's view of an entry's chain ledger.

    Each serving model is priced at the row its name resolves to (the trace
    sink's resolver), cached for the run. The run's own model, by name or by
    resolved row, keeps the run's price row, so an entry with no handover, or
    a lone model that resolves to nothing, is priced at the run's row as it
    would be without a chain ledger. Among several, a model with no row leaves
    the cost unavailable, never partial.
    """
    usage_by_model: list[tuple[str | None, dict[str, object]]] = []
    for part in chain["usage_by_model"]:
        usage = _token_usage_from_buckets([part["usage"]])
        if usage is not None:
            usage_by_model.append((part["model"], usage))

    run_resolved = run_price_rows[0].get("resolved_model") if run_price_rows else None
    price_rows_by_model: dict[str | None, list[dict[str, object]]] = {}
    for model, _usage in usage_by_model:
        rows: list[dict[str, object]] = []
        if model and model != run_model_id:
            if model not in price_rows_cache:
                price_rows_cache[model] = await _build_run_price_rows(db, model)
            rows = price_rows_cache[model]
        if (
            model == run_model_id
            or (rows and rows[0].get("resolved_model") == run_resolved)
            or (not rows and len(usage_by_model) == 1)
        ):
            rows = run_price_rows
        price_rows_by_model[model] = rows

    return ChainInputs(
        calls=chain["calls"],
        token_usage_by_model=usage_by_model,
        price_rows_by_model=price_rows_by_model,
        complete=chain["complete"],
        incomplete_reason=chain.get("incomplete_reason"),
    )


#: One delegated task, as the wait reads it: its status, its metadata, and
#: whether an attempt ever started it.
_TaskRow = tuple[str, dict, bool]

#: How long a task killed while it ran is given to write its ledger. The kill
#: commits ``cancelled`` first and the worker, still running, writes the ledger
#: when its turn ends; a worker that died never does, and waiting on it would
#: hold the entry to the wait's bound. Past this, its ledger is missing.
_KILLED_TASK_LEDGER_GRACE_SECONDS = 120.0


def _in_evaluation_chain(task_model):
    """Only the tasks an evaluation's chain spawned: the wait reads no other task."""
    return task_model.metadata_json[EVALUATION_CHAIN_KEY].astext == "true"


async def _load_task_rows(session: AsyncSession, task_ids: list[str]) -> dict[str, _TaskRow]:
    """Each delegated task's status and metadata, and whether it was started."""
    from app.models.sub_agent_task import SubAgentTask

    ids = [task_uuid for t in task_ids if (task_uuid := safe_uuid(t)) is not None]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(
                SubAgentTask.id, SubAgentTask.status, SubAgentTask.metadata_json, SubAgentTask.started_at
            ).where(SubAgentTask.id.in_(ids), _in_evaluation_chain(SubAgentTask))
        )
    ).all()
    return {str(r[0]): (str(r[1]), dict(r[2] or {}), r[3] is not None) for r in rows}


async def _load_continuations(session: AsyncSession, task_ids: list[str]) -> dict[str, list[str]]:
    """The continuation tasks an ``incomplete`` task spawned, keyed by the task continued."""
    from app.models.sub_agent_task import SubAgentTask

    if not task_ids:
        return {}
    continued = SubAgentTask.metadata_json["wait_continuation_of"].astext
    rows = (
        await session.execute(
            select(SubAgentTask.id, continued).where(continued.in_(task_ids), _in_evaluation_chain(SubAgentTask))
        )
    ).all()
    found: dict[str, list[str]] = {}
    for child_id, parent_id in rows:
        found.setdefault(str(parent_id), []).append(str(child_id))
    return found


async def _delegation_wait_seconds(run: EvaluationRun) -> float:
    """How long an entry may wait for delegated work.

    The smaller of the platform's wait budget for spawned tasks (the one an
    agent's own ``wait`` is held to) and the time left before the execution's
    deadline, less the grace kept for judging and recording. Read only when an
    entry spawned tasks, in a session of its own.
    """
    from app.core.config import settings
    from app.core.database import async_session
    from app.models.orchestration import OrchestrationExecution
    from app.services.wait_settings import load_platform_wait_policy

    async with async_session() as session:
        policy = await load_platform_wait_policy(session)
        deadline = None
        if getattr(run, "owner_execution_id", None) is not None:
            deadline = (
                await session.execute(
                    select(OrchestrationExecution.deadline_at).where(
                        OrchestrationExecution.id == run.owner_execution_id
                    )
                )
            ).scalar_one_or_none()
    seconds = float(policy.total_seconds)
    if deadline is not None:
        left = (deadline - datetime.now(UTC)).total_seconds()
        seconds = min(seconds, left - settings.orchestration_grace_seconds)
    return max(0.0, seconds)


async def _run_was_cancelled(session: AsyncSession, run_uuid: uuid.UUID) -> bool:
    """Whether the user cancelled *run_uuid* since this worker started it.

    A column select, like ``_stop_if_the_user_cancelled``, in the wait's own
    session: the entry's session holds this worker's stale ``running`` copy, and
    must not be committed in the middle of an entry.
    """
    status = (
        await session.execute(select(EvaluationRun.status).where(EvaluationRun.id == run_uuid))
    ).scalar_one_or_none()
    # Deleted (``None``) counts as cancelled: its deletion went through the
    # cancel path.
    return status is None or status == "cancelled"


async def _collect_task_ledgers(
    task_ids: list[str],
    *,
    bound_seconds: float,
    run_uuid: uuid.UUID | None = None,
    reason_out: dict[str, IncompleteReason] | None = None,
) -> dict[str, RunLedger | None]:
    """Wait for an entry's delegated tasks and read each one's ledger.

    Follows the tasks each finished task spawned, and the continuation an
    ``incomplete`` task spawns when it runs out of wait budget, until every task
    reached is terminal or *bound_seconds* have passed. A task still unfinished
    then, finished without a ledger, or whose ledger is ``partial`` maps to
    ``None``: the chain is incomplete and its totals are reported unavailable,
    never partial. So does an id that is not a task of an evaluation's chain: a
    spawned task's row is committed before its id is returned, so there is
    nothing to wait for. *reason_out* gets why the wait stopped early.

    A lost lease cancels the entry, and the wait with it. A user's cancel of
    *run_uuid* only marks the run, which the engine reads between entries, so
    the wait reads it at each poll and stops there, the chain left incomplete.
    Each poll reads in one short session, so the wait holds no connection
    between polls.
    """
    from app.core.database import async_session
    from app.services.sub_agent import _MAX_WAIT_CONTINUATIONS
    from app.tools.wait_policy import WAIT_POLL_INTERVAL, WAIT_TERMINAL_STATUSES

    give_up_at = time.monotonic() + bound_seconds
    ledgers: dict[str, RunLedger | None] = {}
    pending: list[str] = list(dict.fromkeys(task_ids))
    awaiting_continuation: set[str] = set()
    killed_since: dict[str, float] = {}
    while True:
        async with async_session() as session:
            rows = await _load_task_rows(session, [*pending, *sorted(awaiting_continuation)])
            still: list[str] = []
            for task_id in pending:
                row = rows.get(task_id)
                if row is None:
                    ledgers[task_id] = None
                    continue
                if row[0] not in WAIT_TERMINAL_STATUSES:
                    still.append(task_id)
                    continue
                status, metadata, started = row
                raw = metadata.get(LEDGER_KEY)
                ledger = cast("RunLedger", raw) if isinstance(raw, dict) else None
                if ledger is None and status == "cancelled" and started:
                    # Killed while it ran: the kill commits ``cancelled`` first,
                    # and the worker, still running, writes the ledger after.
                    first_seen = killed_since.setdefault(task_id, time.monotonic())
                    if time.monotonic() - first_seen < _KILLED_TASK_LEDGER_GRACE_SECONDS:
                        still.append(task_id)
                        continue
                ledgers[task_id] = None if ledger is None or ledger.get("partial") else ledger
                children = (ledger or {}).get("spawned_task_ids")
                for child in children if isinstance(children, list) else []:
                    if child not in ledgers and child not in still and child not in pending:
                        still.append(str(child))
                depth = int(metadata.get("wait_continuation_depth") or 0)
                if status == "incomplete" and depth < _MAX_WAIT_CONTINUATIONS:
                    awaiting_continuation.add(task_id)
            for task_id in sorted(awaiting_continuation):
                row = rows.get(task_id)
                if row is not None and row[1].get(NO_CONTINUATION_KEY):
                    awaiting_continuation.discard(task_id)
            if awaiting_continuation:
                continuations = await _load_continuations(session, sorted(awaiting_continuation))
                for parent, children in continuations.items():
                    awaiting_continuation.discard(parent)
                    still.extend(c for c in children if c not in ledgers and c not in still)
            pending = still
            if not pending and not awaiting_continuation:
                return ledgers
            stop: IncompleteReason | None = None
            if time.monotonic() >= give_up_at:
                stop = "wait_bound_reached"
            elif run_uuid is not None and await _run_was_cancelled(session, run_uuid):
                stop = "run_cancelled"
        if stop is not None:
            if reason_out is not None:
                reason_out["reason"] = stop
            for task_id in pending:
                ledgers[task_id] = None
            for task_id in awaiting_continuation:
                # Its continuation never appeared: the work it continues is uncounted.
                ledgers[f"{task_id}:continuation"] = None
            return ledgers
        await asyncio.sleep(WAIT_POLL_INTERVAL)


def _reused_chain(reused_details: dict | None, exec_mode: str | None) -> ChainLedger | None:
    """The chain ledger a reused entry scores from.

    A source that carries its chain keeps it. An agent-run source without one
    was counted from the agent's timeline, which misses a helper's work and
    some of the agent's own calls, so its totals are reported unavailable
    rather than ranked against chain-counted entries (as for latency).
    An ``llm_only`` source never has a chain: ``None`` keeps its step-derived
    figures.
    """
    carried = (reused_details or {}).get(CHAIN_KEY)
    if isinstance(carried, dict) and isinstance(carried.get("calls"), list):
        return cast("ChainLedger", dict(carried))
    if exec_mode in ("full_loop", "skip_llm"):
        return uncounted_chain("recorded_before_chain")
    return None


def _recover_reused_retry_count(reused_details: dict | None) -> int | None:
    """Recover the retry count recorded by the run a prediction was reused from.

    A reused entry runs no agent, so this execution's meter reads zero, which
    would persist as a measured "no retries" for a prediction that may have
    needed several. ``None`` when the source recorded none, leaving the metric
    uncaptured rather than a fabricated zero.
    """
    results = (reused_details or {}).get("metric_results")
    if not isinstance(results, dict):
        return None
    entry = results.get(RETRY_COUNT_KEY)
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if value < 0:
        return None
    return int(value)


def _recover_reused_wall_clock_ms(reused_details: dict | None) -> int | None:
    """Recover a reused entry's wall-clock latency.

    A reused entry runs no agent, so it carries the latency its source run
    measured, recorded as ``agent_wall_clock_ms``.

    ``None`` (the metric reports ``unavailable``) when there is no usable
    recorded clock. A source without one does NOT fall back to its step total:
    that is the pre-4.0.0 suite figure, not comparable with the clock, so a run
    mixing reused and fresh entries would rank them against each other, and for
    a transfer it is 0, a false perfect score. Non-finite values are
    rejected too: ``round`` raises on ``inf`` and ``nan``.
    """
    recorded = (reused_details or {}).get("agent_wall_clock_ms")
    if (
        not isinstance(recorded, (int, float))
        or isinstance(recorded, bool)
        or not math.isfinite(recorded)
    ):
        return None
    # Rounded as ``_ms`` rounds, and checked AFTER rounding, or a
    # sub-millisecond value would come back as 0.
    wall_clock_ms = round(recorded)
    return wall_clock_ms if wall_clock_ms > 0 else None


def _step_total_ms(traces: list[dict]) -> int:
    """The sum of an entry's recorded steps, a group of parallel tools counted once.

    Tools that ran together share a ``parallel_group``: the agent spent the
    longest of them, not their sum. A step outside a group is added as it is.
    """
    total = 0
    longest: dict[str, int] = {}
    for trace in traces:
        duration = int(trace.get("duration_ms") or 0)
        group = trace.get("parallel_group")
        if isinstance(group, str) and group:
            longest[group] = max(longest.get(group, 0), duration)
        else:
            total += duration
    return total + sum(longest.values())


def _recover_reused_agent_duration_ms(
    agent_duration_ms: int,
    reused_details: dict | None,
    source_duration_seconds: float | None,
) -> int:
    """Recover a reused entry's agent duration.

    The carried step timeline may lack per-step ``duration_ms`` (and no
    ``agent_trace_ids`` are carried, so there is no DB fallback). A fully
    missing timeline sums to 0, an agent that took no time; a partially
    missing one undercounts. So:

      1. The recorded total is the source's ``agent_duration_ms`` (the exact
         value it scored on), else its ``duration_seconds`` snapshot taken
         before the connection release.
      2. Return ``max(carried_sum, recorded_total)``: a no-op for a complete
         timeline, the real total for a zero or an undercount.
      3. With no recorded total the carried sum is kept (0 makes the
         aggregate exclude the entry as unknown rather than instant).
    """
    recovered = 0
    recorded = (reused_details or {}).get("agent_duration_ms")
    if recorded and int(recorded) > 0:
        recovered = int(recorded)
    elif source_duration_seconds and source_duration_seconds > 0:
        recovered = round(source_duration_seconds * 1000)
    return max(agent_duration_ms, recovered)


async def _load_tool_calls_from_trace_ids(
    db: AsyncSession,
    trace_ids: list[str],
) -> list[dict]:
    """Build judge tool-call context from native TOOL spans."""
    parsed_ids: list[uuid.UUID] = []
    for trace_id in trace_ids:
        try:
            parsed_ids.append(uuid.UUID(trace_id))
        except (TypeError, ValueError):
            logger.debug("Ignoring invalid trace id while loading tool calls: %r", trace_id)
            continue
    if not parsed_ids:
        return []

    span_rows = await db.execute(
        select(LLMSpan).where(
            # ck_llm_spans_type_valid constrains type to uppercase literals,
            # so a direct compare keeps the predicate sargable.
            LLMSpan.trace_id.in_(parsed_ids),
            LLMSpan.type == "TOOL",
        )
    )
    # Caller's trace order, then span position: sorting by raw trace_id UUIDs
    # would scramble tool-call chronology across traces.
    input_order = {tid: idx for idx, tid in enumerate(parsed_ids)}
    spans = sorted(
        span_rows.scalars().all(),
        key=lambda s: (input_order.get(s.trace_id, len(input_order)), s.position),
    )
    calls: list[dict] = []
    for span in spans:
        tool_name = span.name or "tool"
        tool_output = span.output
        calls.append(
            {
                "type": "tool",
                "tool_name": tool_name,
                "description": f"Tool call: {tool_name}",
                "duration_ms": int(span.duration_ms or 0),
                "tool_output": json.dumps(tool_output, ensure_ascii=False)
                if isinstance(tool_output, (dict, list))
                else str(tool_output or ""),
            }
        )
    return calls


def _build_versioned_prompts_fragment(
    prompt_snapshots: list[tuple[str, str | None, str]],
) -> str:
    """Build a prompt injection fragment from versioned prompt snapshots.

    Each snapshot is ``(name, description, content)``: ``name`` and
    ``description`` come from the LIVE ``Prompt`` row (a rerun picks up their
    edits), ``content`` is the immutable value pinned by
    ``PromptVersion.version_number``.

    Mirrors the live ``builtin_tools.load_agent_prompts`` layout:

        ── Attached Prompts ──
        ### {name}
        {description}            ← only if non-empty
        {content}

    so live execution and versioned replay produce the same system prompt,
    description line included.
    """
    if not prompt_snapshots:
        return ""

    parts: list[str] = ["\n── Attached Prompts ──"]
    for prompt_name, description, content in prompt_snapshots:
        header = f"\n### {prompt_name}"
        if description:
            header += f"\n{description}"
        parts.append(header)
        parts.append(content)
    return "\n".join(parts)


async def floored_max_iterations(
    db: AsyncSession,
    agent: Agent,
    evaluation_overrides: dict[str, object] | None,
) -> int | None:
    """The iteration cap the chat floor raises this run's agent to, else ``None``.

    The configured cap is read by ``configured_max_iterations``, as
    ``run_agent_chat`` reads it, so the fingerprint records the cap the loop
    gets. ``None`` when the floor leaves the cap as configured: the fingerprint
    then gains no key, and stored answers that predate the floor stay
    reusable.
    """
    from app.models.general_assistant_config import (
        DEFAULT_MAX_ITERATIONS,
        GeneralAssistantConfig,
        chat_loop_max_iterations,
        clamp_max_iterations,
        configured_max_iterations,
    )

    stored = await db.scalar(select(GeneralAssistantConfig.max_iterations).limit(1))
    platform_max_iterations = stored if stored is not None else DEFAULT_MAX_ITERATIONS
    configured = configured_max_iterations(
        agent.max_iterations, platform_max_iterations, evaluation_overrides
    )
    floored = chat_loop_max_iterations(configured)
    return floored if floored != clamp_max_iterations(int(configured)) else None


@dataclass(frozen=True)
class PinnedPrompt:
    """One attached Prompt as a run's ``prompt_versions`` snapshot pins it."""

    prompt_id: uuid.UUID
    version_number: int
    name: str
    description: str | None
    content: str


async def versioned_prompt_snapshots(
    db: AsyncSession, prompt_versions: object
) -> tuple[list[PinnedPrompt], list[str]]:
    """The Prompts a run's ``prompt_versions`` pins, and the pins that no longer resolve.

    Live name and description, pinned content: see
    ``_build_versioned_prompts_fragment``. A placebo arm replaces
    exactly these.
    """
    pinned: list[PinnedPrompt] = []
    missing: list[str] = []
    if not isinstance(prompt_versions, dict):
        return pinned, missing
    for prompt_id_raw, raw_version in prompt_versions.items():
        try:
            prompt_id = uuid.UUID(str(prompt_id_raw))
            version_number = int(raw_version)
        except (TypeError, ValueError):
            missing.append(f"prompt {prompt_id_raw} v{raw_version}")
            continue

        prompt_result = await db.execute(
            select(PromptVersion.content, Prompt.name, Prompt.description)
            .outerjoin(Prompt, Prompt.id == PromptVersion.prompt_id)
            .where(
                PromptVersion.prompt_id == prompt_id,
                PromptVersion.version_number == version_number,
            )
        )
        row = prompt_result.one_or_none()
        if row is None:
            missing.append(f"prompt {prompt_id} v{version_number}")
            continue
        pinned.append(
            PinnedPrompt(
                prompt_id=prompt_id,
                version_number=version_number,
                name=row.name or f"Prompt {prompt_id}",
                description=row.description,
                content=row.content,
            )
        )
    return pinned, missing


async def resolve_run_snapshot_overrides(
    db: AsyncSession,
    run: EvaluationRun,
) -> tuple[dict[str, object], list[str]]:
    """Resolve versioned run snapshots into runtime overrides.

    Returns a tuple ``(overrides, missing)`` where:
    - ``overrides`` can contain ``persona``, ``guardrails``, ``temperature``,
      ``reasoning_effort``, ``max_iterations``, and ``prompt_fragment``.
    - ``missing`` lists missing snapshot references that make deterministic replay
      impossible.

    ``run.persona_version_number == DEFAULT_PERSONA_SNAPSHOT_MARKER`` means
    the run was created with no persona configured; replay must keep that
    explicit default state (``overrides["persona"] = ""``) rather than
    drifting to the agent's current persona.
    """
    overrides: dict[str, object] = {}
    missing: list[str] = []

    if run.persona_version_number == DEFAULT_PERSONA_SNAPSHOT_MARKER:
        overrides["persona"] = ""
    elif run.persona_version_number is not None:
        persona_result = await db.execute(
            select(PromptVersion.content).where(
                PromptVersion.agent_id == run.agent_id,
                PromptVersion.version_number == run.persona_version_number,
            )
        )
        persona_content = persona_result.scalar_one_or_none()
        if persona_content is None:
            missing.append(f"persona v{run.persona_version_number}")
        else:
            overrides["persona"] = persona_content

    config_versions = run.agent_config_versions or {}
    if isinstance(config_versions, dict):
        for field_name, raw_version in config_versions.items():
            # ``None`` marks a field unset when the run was created: the
            # override keeps ``None`` so ``run_agent_chat`` takes the default
            # path instead of the agent's current value.
            if raw_version is None:
                overrides[field_name] = None
                continue
            try:
                version_number = int(raw_version)
            except (TypeError, ValueError):
                missing.append(f"{field_name} v{raw_version}")
                continue

            cfg_result = await db.execute(
                select(AgentConfigVersion).where(
                    AgentConfigVersion.agent_id == run.agent_id,
                    AgentConfigVersion.field_name == field_name,
                    AgentConfigVersion.version_number == version_number,
                )
            )
            cfg_version = cfg_result.scalar_one_or_none()
            if cfg_version is None:
                missing.append(f"{field_name} v{version_number}")
                continue
            overrides[field_name] = cfg_version.value_json

    pinned_prompts, missing_prompts = await versioned_prompt_snapshots(db, run.prompt_versions)
    missing.extend(missing_prompts)
    if pinned_prompts:
        overrides["prompt_fragment"] = _build_versioned_prompts_fragment(
            [(prompt.name, prompt.description, prompt.content) for prompt in pinned_prompts]
        )

    # Skill pins pass through as ``{skill_id: version}``, not resolved bodies:
    # under progressive disclosure a body enters the conversation
    # only when the model calls ``load_skill``, so
    # ``builtin_tools.skills.resolve_agent_skills`` resolves the pin at load
    # time. Resolving here would inline every body.
    skill_versions = run.skill_versions or {}
    if isinstance(skill_versions, dict) and skill_versions:
        resolved_pins: dict[str, int] = {}
        for skill_id_raw, raw_version in skill_versions.items():
            try:
                skill_id = uuid.UUID(str(skill_id_raw))
                version_number = int(raw_version)
            except (TypeError, ValueError):
                missing.append(f"skill {skill_id_raw} v{raw_version}")
                continue

            exists = await db.execute(
                select(SkillVersion.id).where(
                    SkillVersion.skill_id == skill_id,
                    SkillVersion.version_number == version_number,
                )
            )
            if exists.scalar_one_or_none() is None:
                missing.append(f"skill {skill_id} v{version_number}")
                continue
            resolved_pins[str(skill_id)] = version_number

        if resolved_pins:
            overrides["skill_versions"] = resolved_pins

    return overrides, missing


async def _find_reusable_prediction(
    db: AsyncSession,
    evaluation_id: uuid.UUID,
    current_run_id: uuid.UUID,
    fingerprint: str,
) -> EvaluationResult | None:
    """Return a reusable stored prediction matching ``fingerprint``.

    Searches prior *completed* runs of the same evaluation for a non-reused,
    non-anomaly result with the same input fingerprint, then validates it is
    reusable (completed, non-empty, error-free, not an execution anomaly).
    Returns the most recent match or None.

    Rows written before the anomaly detector existed carry no anomaly kind (no
    backfill), so the DB filter alone
    would reuse a stored confirmation-seeking output instead of giving the
    agent a fresh attempt. Each candidate is therefore re-checked with
    :func:`detect_confirmation_seeking`, whatever its persisted flag.
    """
    result = await db.execute(
        select(EvaluationResult, EvaluationRun.status)
        .join(EvaluationRun, EvaluationRun.id == EvaluationResult.run_id)
        .where(
            EvaluationRun.evaluation_id == evaluation_id,
            EvaluationRun.id != current_run_id,
            EvaluationRun.status == "completed",
            EvaluationResult.input_fingerprint == fingerprint,
            EvaluationResult.is_reused.is_(False),
            # An execution anomaly is never a reuse source; excluded DB-side,
            # with is_result_reusable() below as the belt.
            EvaluationResult.execution_anomaly_kind.is_(None),
            # Nor is an LLM Only fallback's answer, whose fingerprint says
            # ``full_loop``. Excluded here so fallbacks do not fill
            # the five-row window; a row PostgreSQL cannot parse is fetched
            # and refused by is_result_reusable().
            when_json_parseable(
                EvaluationResult.execution_details,
                EvaluationResult.execution_details["execution_mode"]
                .as_string()
                .is_distinct_from(FALLBACK_MODE),
                otherwise=True,
            ),
        )
        .order_by(EvaluationResult.created_at.desc())
        .limit(5)
    )
    for res, run_status in result.all():
        # Belt for unflagged rows (see docstring): an output the detector reads
        # as confirmation-seeking or a transient execution error is a
        # non-deliverable whatever the stored kind and the execution mode.
        candidate_anomaly = (
            res.execution_anomaly_kind is not None
            or detect_confirmation_seeking(res.agent_output)
            or detect_execution_error(res.agent_output)
        )
        if is_result_reusable(
            res.agent_output,
            res.execution_details,
            run_status,
            is_execution_anomaly=candidate_anomaly,
        ):
            return res
    return None


async def batch_compute_run_replayability(
    db: AsyncSession,
    runs: list[EvaluationRun],
) -> dict[uuid.UUID, list[str]]:
    """Batch-compute the ``missing_snapshots`` list per run.

    Equivalent to the second value of :func:`resolve_run_snapshot_overrides`
    per run, but with at most four existence queries (persona, agent config,
    prompt, skill) whatever the number of runs, so the
    ``GET /evaluations/{id}/runs`` listing avoids O(runs x snapshot_refs)
    round trips.

    Returns ``{run_id: missing_list}`` with ``missing_list`` empty when
    every referenced snapshot still exists (i.e. ``can_replay == True``).
    """
    if not runs:
        return {}

    persona_lookups: set[tuple[uuid.UUID, int]] = set()
    config_lookups: set[tuple[uuid.UUID, str, int]] = set()
    prompt_lookups: set[tuple[uuid.UUID, int]] = set()
    skill_lookups: set[tuple[uuid.UUID, int]] = set()

    # Per-run parsed references; invalid values are kept verbatim so the
    # "missing" list reads like the per-run resolver's.
    parsed_per_run: list[
        tuple[
            EvaluationRun,
            list[tuple[str, int]],
            list[tuple[uuid.UUID, int, str]],
            list[tuple[uuid.UUID, int]],
            list[str],
        ]
    ] = []

    for run in runs:
        config_ok: list[tuple[str, int]] = []
        prompt_ok: list[tuple[uuid.UUID, int, str]] = []  # (uuid, version, raw_id_repr)
        skill_ok: list[tuple[uuid.UUID, int]] = []
        invalid: list[str] = []

        if (
            run.persona_version_number is not None
            and run.persona_version_number != DEFAULT_PERSONA_SNAPSHOT_MARKER
        ):
            persona_lookups.add((run.agent_id, run.persona_version_number))

        cfg = run.agent_config_versions or {}
        if isinstance(cfg, dict):
            for field_name, raw_v in cfg.items():
                if raw_v is None:
                    continue
                try:
                    version_number = int(raw_v)
                except (TypeError, ValueError):
                    invalid.append(f"{field_name} v{raw_v}")
                    continue
                config_lookups.add((run.agent_id, field_name, version_number))
                config_ok.append((field_name, version_number))

        prom = run.prompt_versions or {}
        if isinstance(prom, dict):
            for prompt_id_raw, raw_v in prom.items():
                try:
                    parsed_prompt_id = uuid.UUID(str(prompt_id_raw))
                    version_number = int(raw_v)
                except (TypeError, ValueError):
                    invalid.append(f"prompt {prompt_id_raw} v{raw_v}")
                    continue
                prompt_lookups.add((parsed_prompt_id, version_number))
                prompt_ok.append((parsed_prompt_id, version_number, str(parsed_prompt_id)))

        skl = run.skill_versions or {}
        if isinstance(skl, dict):
            for skill_id_raw, raw_v in skl.items():
                try:
                    parsed_skill_id = uuid.UUID(str(skill_id_raw))
                    version_number = int(raw_v)
                except (TypeError, ValueError):
                    invalid.append(f"skill {skill_id_raw} v{raw_v}")
                    continue
                skill_lookups.add((parsed_skill_id, version_number))
                skill_ok.append((parsed_skill_id, version_number))

        parsed_per_run.append((run, config_ok, prompt_ok, skill_ok, invalid))

    # Each query filters on the EXACT ``(agent_id, [field_name,]
    # version_number)`` tuples (composite IN), not ``agent_id IN (...)``: with
    # unlimited history (``prompt_version_retention_count=0``, the default)
    # that would load every version of those agents. The result size follows
    # the page's references, not the version history.
    existing_personas: set[tuple[uuid.UUID, int]] = set()
    if persona_lookups:
        rows = await db.execute(
            select(PromptVersion.agent_id, PromptVersion.version_number).where(
                tuple_(PromptVersion.agent_id, PromptVersion.version_number).in_(
                    list(persona_lookups)
                ),
            )
        )
        existing_personas = {
            (a, int(v)) for a, v in rows.all() if a is not None and v is not None
        }

    existing_configs: set[tuple[uuid.UUID, str, int]] = set()
    if config_lookups:
        rows = await db.execute(
            select(
                AgentConfigVersion.agent_id,
                AgentConfigVersion.field_name,
                AgentConfigVersion.version_number,
            ).where(
                tuple_(
                    AgentConfigVersion.agent_id,
                    AgentConfigVersion.field_name,
                    AgentConfigVersion.version_number,
                ).in_(list(config_lookups))
            )
        )
        existing_configs = {
            (a, f, int(v))
            for a, f, v in rows.all()
            if a is not None and f is not None and v is not None
        }

    existing_prompts: set[tuple[uuid.UUID, int]] = set()
    if prompt_lookups:
        rows = await db.execute(
            select(PromptVersion.prompt_id, PromptVersion.version_number).where(
                tuple_(PromptVersion.prompt_id, PromptVersion.version_number).in_(
                    list(prompt_lookups)
                ),
            )
        )
        existing_prompts = {
            (p, int(v)) for p, v in rows.all() if p is not None and v is not None
        }

    existing_skills: set[tuple[uuid.UUID, int]] = set()
    if skill_lookups:
        rows = await db.execute(
            select(SkillVersion.skill_id, SkillVersion.version_number).where(
                tuple_(SkillVersion.skill_id, SkillVersion.version_number).in_(
                    list(skill_lookups)
                ),
            )
        )
        existing_skills = {
            (s, int(v)) for s, v in rows.all() if s is not None and v is not None
        }

    result: dict[uuid.UUID, list[str]] = {}
    for run, config_ok, prompt_ok, skill_ok, invalid in parsed_per_run:
        missing: list[str] = []
        if (
            run.persona_version_number is not None
            and run.persona_version_number != DEFAULT_PERSONA_SNAPSHOT_MARKER
            and (run.agent_id, run.persona_version_number) not in existing_personas
        ):
            missing.append(f"persona v{run.persona_version_number}")
        for field_name, version_number in config_ok:
            if (run.agent_id, field_name, version_number) not in existing_configs:
                missing.append(f"{field_name} v{version_number}")
        for parsed_prompt_id, version_number, _raw_repr in prompt_ok:
            if (parsed_prompt_id, version_number) not in existing_prompts:
                missing.append(f"prompt {parsed_prompt_id} v{version_number}")
        for parsed_skill_id, version_number in skill_ok:
            if (parsed_skill_id, version_number) not in existing_skills:
                missing.append(f"skill {parsed_skill_id} v{version_number}")
        # Invalid entries last, in the per-run resolver's order.
        missing.extend(invalid)
        result[run.id] = missing

    return result


async def _execute_agent_llm_only(
    db: AsyncSession,
    agent: Agent,
    model_id: str,
    input_prompt: str,
    file_refs: list | None = None,
    evaluation_overrides: dict[str, object] | None = None,
    user_id: str | None = None,
) -> tuple[str, list[str], int | None, list[dict]]:
    """Execute agent in LLM-only mode.

    Returns ``(output, trace_ids, ttft_ms, local_traces)``. ``local_traces``
    is the in-process timeline: the trace rows behind ``trace_ids`` are
    flushed asynchronously and may not be queryable yet. ``user_id`` is the
    evaluation's owner, whose providers the model runs on.
    """
    llm, _ = await _get_llm_provider_and_model(db, model_id, user_id=user_id)

    overrides = evaluation_overrides if isinstance(evaluation_overrides, dict) else {}
    has_persona_override = "persona" in overrides
    override_persona = overrides.get("persona") if has_persona_override else None
    if has_persona_override:
        system_message = (
            override_persona
            if isinstance(override_persona, str) and override_persona.strip()
            else "You are a helpful assistant."
        )
    else:
        system_message = agent.persona or "You are a helpful assistant."

    override_temperature = overrides.get("temperature")
    resolved_temperature = (
        float(override_temperature)
        if isinstance(override_temperature, (int, float))
        else (
            float(agent.temperature)
            if agent.temperature is not None
            else 0.7
        )
    )

    # llm_only is always an evaluation: tell the model no human will answer
    # follow-up questions.
    from app.services.evaluation.framing import build_autonomous_system_suffix

    system_message = f"{system_message}\n\n{build_autonomous_system_suffix()}"

    messages = [
        ChatMessage(role="system", content=system_message),
        ChatMessage(role="user", content=input_prompt, files=file_refs or None),
    ]

    from app.services.langfuse_tracing import (
        end_trace as _lf_end,
    )
    from app.services.langfuse_tracing import (
        get_local_trace_id,
        reset_current_trace_ctx,
        set_current_trace_ctx,
    )
    from app.services.langfuse_tracing import (
        trace_llm_call as _lf_trace,
    )

    trace_ctx = await _lf_trace(
        db,
        name="evaluation-llm-only",
        user_id=str(agent.id),
        source="evaluation",
        model=model_id,
        input_data={"agent": agent.name, "message": input_prompt},
        agent_id=str(agent.id),
        agent_name=agent.name,
        agent_slug=agent.slug,
        metadata={
            "execution_mode": "llm_only",
            "llm_provider": getattr(llm, "provider_name", None),
            **provider_config_metadata(llm),
        },
    )

    # What each request reported: every one is billed, a cut one too. The
    # streamed call's comes from its own sink: the provider instance is
    # cached and shared, so its ``last_usage`` could be another entry's.
    usages: list[dict] = []
    stream_usage = StreamUsageSink()
    answered = 0

    async def _chat(max_tokens: int):
        nonlocal answered
        response = await llm.chat(
            messages=messages,
            model=model_id,
            temperature=resolved_temperature,
            max_tokens=max_tokens,
        )
        answered += 1
        if response.usage:
            usages.append(response.usage)
        return response

    def _usage() -> dict | None:
        # One request's usage goes to the trace as it came; a retry's is
        # added to it.
        if len(usages) < 2:
            return usages[0] if usages else None
        total: dict = {}
        for usage in usages:
            _add_usage(total, usage)
        return total

    def _still_cut() -> RuntimeError:
        # A cut answer is not scored as the agent's answer: the case fails as
        # it does when the provider call fails.
        error = still_cut("LLM-only evaluation")
        _lf_end(trace_ctx, error=str(error), usage=_usage())
        return error

    # Streamed to measure the real time to first token.
    t0 = time.perf_counter()
    ttft_ms: int | None = None
    chunks: list[str] = []
    # Current while the model answers, like the judge trace.
    _trace_token = set_current_trace_ctx(trace_ctx) if trace_ctx else None
    stream_stop = StreamStopReason(llm)
    try:
        async for chunk in llm.chat_stream(
            messages=messages,
            model=model_id,
            temperature=resolved_temperature,
            max_tokens=_LLM_ONLY_MAX_TOKENS,
            usage_sink=stream_usage,
        ):
            if ttft_ms is None:
                ttft_ms = _ms(t0, time.perf_counter())
            chunks.append(chunk)
    except ContextOverflowError:
        raise
    except Exception:
        # Non-streaming fallback. A stream that failed after reporting usage
        # was billed for it (Anthropic reports the input at
        # ``message_start``), so it counts before the fallback's requests.
        if partial := stream_usage.take():
            usages.append(partial)
        try:
            response, cut = await chat_retrying_a_cut(
                llm, model_id, _LLM_ONLY_MAX_TOKENS, _chat, label="Evaluation (LLM-only)"
            )
        except BaseException as exc:
            if answered or usages:
                # The fallback failed or was cancelled after a billed
                # request: the failed stream's, or the first of a cut answer.
                _lf_end(trace_ctx, error=format_exception_message(exc), usage=_usage())
            raise
        if cut:
            raise _still_cut() from None
        duration_ms = _ms(t0, time.perf_counter())
        _lf_end(trace_ctx, output=response.content, usage=_usage(), model=response.model)
        local_trace_id = get_local_trace_id(trace_ctx)
        trace_ids = [str(local_trace_id)] if local_trace_id else []
        local_traces = [
            {
                "type": "llm",
                "description": "LLM call - direct response",
                "duration_ms": duration_ms,
                "outcome": "success",
                "model": model_id,
                **({"usage": usage} if (usage := _usage()) else {}),
            }
        ]
        return _delivered_answer(agent, response.content or ""), trace_ids, None, local_traces
    else:
        output = "".join(chunks)
        streamed = stream_usage.take()
        if streamed:
            usages.insert(0, streamed)
        if stream_stop.was_cut():
            # Still answering: the retry runs while the trace is current too.
            budget = cut_retry_budget(
                llm, model_id, _LLM_ONLY_MAX_TOKENS, label="Evaluation (LLM-only)"
            )
            if budget is None:
                raise _still_cut()
            try:
                retry = await _chat(budget)
            except BaseException as exc:
                # The cut stream was still billed: its failure is recorded.
                _lf_end(trace_ctx, error=format_exception_message(exc), usage=_usage())
                raise
            if answer_was_cut(retry):
                raise _still_cut()
            output = retry.content or ""
    finally:
        if _trace_token is not None:
            with contextlib.suppress(Exception):
                reset_current_trace_ctx(_trace_token)

    duration_ms = _ms(t0, time.perf_counter())
    _lf_end(trace_ctx, output=output, usage=_usage(), model=model_id)

    local_trace_id = get_local_trace_id(trace_ctx)
    trace_ids = [str(local_trace_id)] if local_trace_id else []
    # The timeline step carries the call's usage, which the engine reads before
    # the stored trace, written seconds late (``_resolve_agent_token_usage``).
    local_traces = [
        {
            "type": "llm",
            "description": "LLM call - direct response",
            "duration_ms": duration_ms,
            "outcome": "success",
            "model": model_id,
            **({"usage": usage} if (usage := _usage()) else {}),
        }
    ]
    return _delivered_answer(agent, output), trace_ids, ttft_ms, local_traces


def _delivered_answer(agent: Agent, text: str) -> str:
    """What the agent delivers from the model's text, as a chat turn would.

    An agent declared as a JSON producer has its answer coerced to bare JSON in Full Loop
    (``agent_chat``); without the same step here, one agent scored 100 in Full Loop and
    0 in LLM Only on the same fenced answer. A cut answer never reaches this: LLM Only
    raises on one.
    """
    from app.services.output_format import effective_output_contract
    from app.services.structured_output import deliver_json_answer

    output_format, output_schema = effective_output_contract(
        agent.output_format, agent.output_schema, None
    )
    return deliver_json_answer(
        text, output_format, output_schema, log_context=f"agent {agent.id} (LLM Only)"
    )


@known_secrets_scoped()
async def _execute_agent_full_loop(
    db: AsyncSession,
    agent: Agent,
    model_id: str,
    input_prompt: str,
    file_refs: list | None = None,
    integration_tools_bundle: IntegrationToolsBundle | None = None,
    evaluation: Evaluation | None = None,
    *,
    evaluation_overrides: dict[str, object] | None = None,
    allow_handover: bool = True,
    delegation_depth: int = 0,
    outcome_out: dict | None = None,
    apply_prompt_guards: bool = False,
    require_tool_approval: bool = False,
    approvals_out: list | None = None,
) -> tuple[str, list[str], int | None, list[str] | None, list[dict]]:
    """Execute agent with full agentic loop via run_agent_chat in evaluation mode.

    Returns ``(output, trace_ids, ttft_ms, tool_names, local_traces)``.
    ``tool_names`` is sorted and de-duplicated, ``None`` when the turn did not
    report it (an early return): ``[]`` would read as an agent with no tools.
    ``local_traces`` is the in-memory timeline ``run_agent_chat`` collects in
    evaluation mode: the trace rows behind ``trace_ids`` are flushed in a
    background task and may not be queryable yet when the entry is scored.

    Raises ``PipelineToolFailedError`` when the agent's pipeline tool refused or
    failed: the turn's text is then the tool's error, which a chat shows as the
    answer but an evaluation must not score as one.
    """
    from app.services.agent_chat import pipeline_tool_failure, run_agent_chat

    eval_result: dict[str, object] = {}
    loop_outcome: dict[str, object] = {}
    turn_error: dict[str, object] = {}

    eval_user_id = (
        str(evaluation.user_id)
        if evaluation and getattr(evaluation, "user_id", None)
        else "anonymous"
    )
    # An evaluation with no owning user reads with the company-managed tier.
    from app.api.v1.helpers import OWNERLESS_EVALUATION, keeping_company_managed_tier

    missing = object()
    scope = (
        keeping_company_managed_tier(OWNERLESS_EVALUATION)
        if evaluation is not None and getattr(evaluation, "user_id", missing) is None
        else contextlib.nullcontext()
    )

    # Time to first token runs from the agent call, as the latency does:
    # the turn's setup counts, and it never exceeds the latency.
    called_at = time.perf_counter()
    try:
        with scope:
            output, _ = await run_agent_chat(
                db,
                eval_user_id,
                agent,
                [],
                input_prompt,
                allow_handover=allow_handover,
                files=file_refs,
                delegation_depth=delegation_depth,
                model_id_override=model_id,
                evaluation_mode=True,
                # The run's own value, stamped at its start.
                evaluation_prompt_guards=apply_prompt_guards,
                # Whether tool calls that need approval are held; the
                # proposals of the calls held land in ``approvals_out``.
                evaluation_tool_approval=require_tool_approval,
                approvals_out=approvals_out,
                evaluation_overrides=evaluation_overrides,
                eval_result_out=eval_result,
                # The run's ledger: what its trace recorded, and its
                # in-process helpers' — the source of the entry's chain totals.
                loop_outcome_out=loop_outcome,
                prebuilt_integration_tools=integration_tools_bundle,
                # Resolve fallbacks (and the missing-integration notice) as
                # ``_build_evaluation_integration_tools`` resolved the bundle,
                # or the agent is told a provider it holds tools for is not
                # connected.
                company_automation=bool(getattr(agent, "is_company_managed", False)),
                error_out=turn_error,
            )
    finally:
        # On every exit, a raise included: a failed run still spent what its
        # ledger holds, and still spawned the tasks it lists.
        if outcome_out is not None:
            outcome_out.update(loop_outcome)

    if (pipeline_failure := pipeline_tool_failure(turn_error)) is not None:
        raise pipeline_failure

    trace_id = eval_result.get("llm_trace_id")
    tool_names = eval_result.get("tool_names")
    answer_started_at = eval_result.get("answer_started_at")
    local_traces = eval_result.get("traces")
    typed_trace_ids = [trace_id] if isinstance(trace_id, str) and trace_id else []
    typed_tool_names = (
        sorted({name for name in tool_names if isinstance(name, str) and name})
        if isinstance(tool_names, list)
        else None
    )
    typed_ttft = (
        round((answer_started_at - called_at) * 1000)
        if isinstance(answer_started_at, (int, float))
        and not isinstance(answer_started_at, bool)
        and answer_started_at >= called_at
        else None
    )
    typed_local_traces = [
        t for t in local_traces if isinstance(t, dict)
    ] if isinstance(local_traces, list) else []
    return output or "", typed_trace_ids, typed_ttft, typed_tool_names, typed_local_traces


async def _load_file_refs(db: AsyncSession, file_ids: list[str] | None) -> list:
    """Load FileRef objects for dataset entry files.

    Returns a list of :class:`FileRef` objects that providers will
    resolve natively (images, PDFs) or via text extraction fallback.
    """
    if not file_ids:
        return []

    from app.llm.base import FileRef
    from app.models.file_attachment import FileAttachment

    refs = []
    for fid in file_ids:
        try:
            result = await db.execute(
                select(FileAttachment).where(FileAttachment.id == uuid.UUID(fid))
            )
            att = result.scalar_one_or_none()
            if att:
                refs.append(FileRef.from_attachment(att))
        except Exception:
            logger.warning("Failed to load file %s for evaluation", fid)
    return refs


async def _build_evaluation_integration_tools(
    db: AsyncSession,
    agent: Agent,
    evaluation: Evaluation | None = None,
) -> IntegrationToolsBundle | None:
    """Build and filter integration tools for evaluation mode, once per run.

    The bindings are resolved for the acting user (the evaluation owner, else
    the agent owner) by ``resolve_agent_integration_bindings``, the resolution
    every chat turn runs: a person who is not the agent's owner never gets an
    integration they may not use. ``run_agent_chat`` refuses a bundle
    its own resolution would not give (``prebuilt_tools_refusal``).
    """
    from app.services.builtin_tools import filter_integration_tools
    from app.services.integration_tools import build_integration_tools, resolve_agent_integration_bindings

    # Some providers (``platform_managed``) need an acting user to resolve
    # credentials: the evaluation owner, else the agent owner so ownerless
    # evaluations still resolve bindings (as assignment runs:
    # ``run.user_id or agent.user_id``).
    acting_user_id: str | None = None
    if evaluation and getattr(evaluation, "user_id", None):
        acting_user_id = str(evaluation.user_id)
    elif getattr(agent, "user_id", None):
        acting_user_id = str(agent.user_id)

    evaluation_id = str(evaluation.id) if evaluation and getattr(evaluation, "id", None) else None

    # An unparseable acting user id degrades to no acting user (as in
    # ``shadow_invisible_bindings``) instead of crashing tool setup.
    if acting_user_id and normalize_acting_user_id(acting_user_id) is None:
        logger.warning(
            "Evaluation integration tools: unparseable acting user id %r "
            "(evaluation=%s, agent=%s) — degrading to no acting user "
            "context",
            acting_user_id,
            evaluation_id,
            agent.name,
        )
        acting_user_id = None

    # An evaluation is non-interactive: a company automation when its agent is
    # company-managed.
    resolved = await resolve_agent_integration_bindings(
        db,
        agent,
        acting_user_id,
        company_automation=bool(getattr(agent, "is_company_managed", False)),
    )
    effective_bindings = resolved.bindings

    if not effective_bindings:
        logger.info(
            "Evaluation integration tools: no effective bindings "
            "(evaluation=%s, agent=%s)",
            evaluation_id,
            agent.name,
        )
        return None

    bound_providers = sorted({k.split("#", 1)[0] for k in effective_bindings})

    from app.services.email_signature_settings import SIGNATURE_CONTEXT_KEY
    from app.services.integration_live_writes import LIVE_WRITES_CONTEXT_KEY, live_writes_overrides
    from app.services.integration_read_only import (
        READ_ONLY_CONTEXT_KEY,
        read_only_integration_ids,
    )
    from app.services.integration_tools import ATTACK_SOURCES_CONTEXT_KEY, PLATFORM_NAMES_CONTEXT_KEY

    # What the build collects for the agent's built-in tools, as a chat run's build does.
    collected: dict[str, list] = {PLATFORM_NAMES_CONTEXT_KEY: [], ATTACK_SOURCES_CONTEXT_KEY: []}
    integ_defs, integ_exec = await build_integration_tools(
        db,
        resolved.tool_bindings,
        context={
            **collected,
            "source": "evaluation",
            "agent": agent.name,
            "agent_id": str(agent.id),
            "user_id": acting_user_id,
            "binding_origin_map": resolved.origins,
            # Same boundaries as the agent's chat runs.
            "element_scope": agent_run_scope(
                getattr(agent, "integration_element_bindings", None),
                getattr(agent, "integration_element_enforcement", None),
            ).enforced,
            READ_ONLY_CONTEXT_KEY: read_only_integration_ids(agent, effective_bindings),
            SIGNATURE_CONTEXT_KEY: getattr(agent, "integration_signatures", None),
            LIVE_WRITES_CONTEXT_KEY: live_writes_overrides(agent, effective_bindings),
        },
    )
    if not integ_defs:
        logger.warning(
            "Evaluation integration tools: build returned 0 tools "
            "(evaluation=%s, agent=%s, acting_user_id=%s, bound_providers=%s)",
            evaluation_id,
            agent.name,
            acting_user_id,
            bound_providers,
        )
        return None

    loaded_providers = sorted(_integration_providers_from_tool_defs(integ_defs))
    filtered_defs = filter_integration_tools(
        integ_defs,
        agent.integration_allowed_tools or {},
    )
    if not filtered_defs:
        logger.warning(
            "Evaluation integration tools: all tools filtered out by "
            "integration_allowed_tools (evaluation=%s, agent=%s, "
            "loaded_providers=%s)",
            evaluation_id,
            agent.name,
            loaded_providers,
        )
        return None

    final_providers = sorted(_integration_providers_from_tool_defs(filtered_defs))
    dropped_providers = sorted(set(loaded_providers) - set(final_providers))
    logger.info(
        "Evaluation integration tools ready "
        "(evaluation=%s, agent=%s, acting_user_id=%s, bound_providers=%s, "
        "loaded_providers=%s, final_providers=%s, dropped_providers=%s, "
        "tool_count_before=%d, tool_count_after=%d)",
        evaluation_id,
        agent.name,
        acting_user_id,
        bound_providers,
        loaded_providers,
        final_providers,
        dropped_providers,
        len(integ_defs),
        len(filtered_defs),
    )

    return IntegrationToolsBundle(
        filtered_defs, integ_exec, collected, bindings=dict(effective_bindings), acting_user_id=acting_user_id
    )


async def score_result(
    db: AsyncSession,
    agent_output: str,
    entry: JudgeEntryLike,
    evaluator_model: str | None,
    evaluator_prompt: str | None = None,
    evaluation_instructions: str | None = None,
    available_tools: list[str] | None = None,
    tool_calls: list[dict] | None = None,
    sub_score_weights: dict[str, float] | None = None,
    judge_max_tokens: int = 8192,
    user_id: str | None = None,
) -> dict:
    """Score an agent output using the LLM judge.

    ``user_id`` is the evaluation's owner, whose providers the judge runs on.
    ``available_tools`` is the agent's tool list: ``[]`` when it had none, ``None``
    when that is not known, which the judge then grades from the calls alone.
    """
    llm, model, provider = await _get_evaluator_llm(
        db, evaluator_model, user_id=user_id
    )

    effective_instructions = entry.evaluation_prompt or evaluation_instructions

    user_prompt = _build_judge_user_prompt(
        input_prompt=entry.input_prompt or "",
        agent_output=agent_output,
        expected_answer=entry.expected_answer,
        evaluation_prompt=effective_instructions,
        available_tools=available_tools,
        tool_calls=tool_calls,
    )

    from app.services.structured_output import (
        build_native_structured_output,
        build_schema_prompt,
    )

    # One schema drives both native enforcement and the prompt-side fallback.
    structured_cfg = build_native_structured_output(llm, _JUDGE_VERDICT_SCHEMA)

    system_prompt = (
        evaluator_prompt
        if evaluator_prompt
        else await _load_default_judge_prompt_from_skill(db)
    )
    # Describe the verdict shape in the prompt ONLY when the provider cannot
    # enforce it natively: no prompt inflation (invariant 16).
    if structured_cfg is None:
        system_prompt = (
            system_prompt
            + "\n\n"
            + build_schema_prompt(_JUDGE_VERDICT_SCHEMA, name="judge_verdict")
        )
    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=user_prompt),
    ]

    from app.services.langfuse_tracing import (
        end_trace as _lf_end_judge,
    )
    from app.services.langfuse_tracing import (
        get_local_trace_id,
        reset_current_trace_ctx,
        set_current_trace_ctx,
    )
    from app.services.langfuse_tracing import (
        trace_llm_call as _lf_trace_judge,
    )

    judge_metadata: dict[str, object] = {
        "entry_id": str(entry.id),
        "judge_max_tokens": judge_max_tokens,
        "llm_provider": provider,
        **provider_config_metadata(llm),
    }
    judge_trace_ctx = await _lf_trace_judge(
        db,
        name="evaluation-judge",
        user_id="system",
        source="evaluation",
        model=model,
        input_data={
            "agent_output": agent_output[:500],
            "expected": entry.expected_answer[:500],
        },
        metadata=judge_metadata,
    )

    judge_chat_kwargs: dict = {
        "messages": messages,
        "model": model,
        "temperature": 0.0,
        "max_tokens": judge_max_tokens,
    }
    if structured_cfg is not None:
        judge_chat_kwargs["structured_output"] = structured_cfg
    # With the judge trace current, a failover re-points it at the config
    # that actually answered.
    _judge_token = set_current_trace_ctx(judge_trace_ctx) if judge_trace_ctx else None
    try:
        try:
            response = await llm.chat(**judge_chat_kwargs)
        except TypeError as te:
            # A provider whose chat() lacks **kwargs may reject the optional
            # structured_output hint by name: strip it and retry once. Any
            # other TypeError propagates.
            if (
                "structured_output" not in judge_chat_kwargs
                or "structured_output" not in str(te)
            ):
                raise
            judge_chat_kwargs.pop("structured_output", None)
            # Native enforcement is lost on the retry: add the prompt-side
            # schema, as a non-native provider gets, so the retry is no weaker,
            # even with a custom evaluator prompt (no seed skill block then).
            fallback_prompt = (
                system_prompt
                + "\n\n"
                + build_schema_prompt(_JUDGE_VERDICT_SCHEMA, name="judge_verdict")
            )
            judge_chat_kwargs["messages"] = [
                ChatMessage(role="system", content=fallback_prompt),
                ChatMessage(role="user", content=user_prompt),
            ]
            response = await llm.chat(**judge_chat_kwargs)
    finally:
        if _judge_token is not None:
            with contextlib.suppress(Exception):
                reset_current_trace_ctx(_judge_token)

    raw_response = response.content or ""
    judge_stop_reason = response.stop_reason
    judge_usage = response.usage or {}
    judge_truncated = answer_was_cut(response)
    parsed = _parse_judge_response(raw_response)

    # A tool-less agent's tool_selection_quality is computed, not judged, and
    # set BEFORE the completeness check so omitting it is no failure.
    # Only when the agent is known to have had no tools: an unknown list is no
    # reason for a perfect grade.
    missing_keys = set(parsed.get("missing_keys") or [])
    if available_tools == []:
        parsed["sub_scores"]["tool_selection_quality"] = 100
        missing_keys.discard("tool_selection_quality")

    # A verdict is usable only if it parsed, was not truncated and holds every
    # sub-score. Anything else is one explicit judge failure: no fabricated
    # 0/50, no retry. Judge failures are excluded from run aggregates like
    # execution anomalies (by the caller and _compute_run_headlines).
    is_judge_failure = (
        bool(parsed.get("parse_error")) or judge_truncated or bool(missing_keys)
    )

    if is_judge_failure:
        reason_bits: list[str] = []
        if parsed.get("parse_error"):
            reason_bits.append("could not be parsed as a complete verdict")
        if judge_truncated:
            reason_bits.append(
                "was truncated at the response budget (max_tokens); "
                "raise the evaluation's judge response budget"
            )
        if missing_keys:
            reason_bits.append(
                "was missing required sub-score(s): " + ", ".join(sorted(missing_keys))
            )
        parsed["reasoning"] = (
            "Judge failure: the judge verdict "
            + "; ".join(reason_bits)
            + ". This result is excluded from aggregate scores.\n\n"
            + f"Raw judge preview: {raw_response[:300]}"
        )

    global_score = _compute_global_score(parsed["sub_scores"], sub_score_weights)

    _lf_end_judge(
        judge_trace_ctx,
        output=parsed.get("reasoning", "")[:1000],
        usage=judge_usage,
        model=model,
        error="judge_failure" if is_judge_failure else None,
    )
    judge_trace_id = get_local_trace_id(judge_trace_ctx)

    return {
        "score": global_score,
        "sub_scores": parsed["sub_scores"],
        "evaluator_reasoning": parsed["reasoning"],
        "judge_stop_reason": judge_stop_reason,
        "judge_usage": judge_usage,
        "judge_truncated": judge_truncated,
        "is_judge_failure": is_judge_failure,
        "judge_model": model,
        "judge_provider": provider,
        "judge_trace_id": str(judge_trace_id) if judge_trace_id else None,
        "judge_system_prompt": system_prompt,
        "judge_user_prompt": user_prompt,
        "judge_raw_response": raw_response,
        "judge_max_tokens": judge_max_tokens,
    }


def judge_failure_from_exception(exc: BaseException, evaluator_model: str | None) -> dict:
    """The score data of a judge call that raised.

    The judge's error is not the agent's: the entry keeps the agent's answer,
    its anomaly kind and its quality metrics, and is recorded as a judge failure,
    left out of the aggregates like an unparsed verdict, so a rescore or a
    judge-only replay can score it later.
    """
    return {
        "score": 0,
        "sub_scores": {k: 0 for k in SUB_SCORE_WEIGHTS},
        "evaluator_reasoning": (
            "Judge failure: the judge call raised "
            f"({format_exception_message(exc)[:300]}). "
            "This result is excluded from aggregate scores."
        ),
        "is_judge_failure": True,
        "judge_model": evaluator_model or "platform_default",
    }


async def _evaluation_run_cancelled_out_of_band(db: AsyncSession, run_id: uuid.UUID) -> bool:
    """Whether a cancel landed on this run while it was executing.

    Read from the row, which the emergency stop and the cancel endpoint write.
    Never from the execution's ``cancel_event``: it also fires on lease loss,
    while the row is still ``running`` and a replacement owns it (see
    :func:`execute_evaluation`). Read without flushing: the session holds
    pending entry state at the end of a run.
    """
    # Best effort: a probe that cannot be read must not fail a run that just
    # finished scoring.
    try:
        with db.no_autoflush:
            status = (
                await db.execute(
                    select(EvaluationRun.status).where(EvaluationRun.id == run_id)
                )
            ).scalar_one_or_none()
    except Exception:
        logger.debug("evaluation run %s: cancel probe failed", run_id, exc_info=True)
        return False
    return status == "cancelled"


def _rescore_ownership(execution_id: uuid.UUID | None) -> list[ColumnElement[bool]]:
    """The rescore's hold on its run: owned by its execution, when it names one.

    A cancel hands the run back at once, so a new rescore can take it while the
    cancelled one is still at work; without this the old one would finish the new
    one's run.
    """
    if execution_id is None:
        return []
    return [runs_this_execution_may_touch(execution_id, repeating_is_harmless=False)]


async def _rescore_was_cancelled(
    db: AsyncSession, run_uuid, run_id: str, execution_id: uuid.UUID | None = None
) -> bool:
    """True when the run being rescored has been cancelled or deleted.

    The rescore owns the run only while it reads ``"running"`` under its own
    execution: a cancel hands it back its previous status, the emergency
    stop writes ``"cancelled"``, and a later rescore takes it over.

    Not ``_stop_if_the_user_cancelled``: ``rescore_run`` holds ONE transaction
    across every result (the first commit is after the loop), so that probe's
    commit would persist a half-judged rescore. This rolls back instead,
    discarding the partial work of a run the user stopped. There is nothing to
    close: a rescore opens no episodes.

    Needed because a BULK rescore stages one execution for every selected run,
    so ``cancel_run`` sees siblings and rightly leaves that execution running.
    Without this probe the rescore keeps calling, and billing, the judge for a
    cancelled run.

    A column select, so the identity map cannot answer it with the worker's own
    stale ``"running"``.
    """
    status = (
        await db.execute(
            select(EvaluationRun.status).where(
                EvaluationRun.id == run_uuid, *_rescore_ownership(execution_id)
            )
        )
    ).scalar_one_or_none()
    if status == "running":
        return False
    logger.info(
        "Rescore of run %s stopped: it was cancelled while re-judging", run_id
    )
    await db.rollback()
    return True


#: The error of a run none of whose entries counts in a headline.
NOTHING_COUNTED_ERROR = (
    "No entry was scored: each one failed, could not be graded by the judge, or "
    "was left out of every score. Open an entry for its reason."
)


async def _nothing_counted(db: AsyncSession, run_uuid: uuid.UUID) -> bool:
    """True when *run_uuid* holds results and none of them counts in a headline.

    Read from the stored results, the headlines' own input, so a result the loop
    did not store itself counts too: an entry whose callback resolved while the
    loop ran. A run with no result is not judged here.
    """
    from app.services.evaluation.aggregation import result_counts_in_headlines

    total, counted = (
        await db.execute(
            select(
                func.count(EvaluationResult.id),
                func.count(EvaluationResult.id).filter(result_counts_in_headlines()),
            ).where(EvaluationResult.run_id == run_uuid)
        )
    ).one()
    return total > 0 and counted == 0


#: The orchestration execution a run is executed under and the worker holding its lease, ``(execution_id,
#: worker_id)``: what fences a run's writes (:func:`_lock_run_before_episode_writes`). ``None`` for a caller that
#: holds no lease.
RunLease = tuple[uuid.UUID, str]


async def _lock_run_before_episode_writes(
    db: AsyncSession, run_uuid, *, lease: RunLease | None = None
) -> None:
    """Take the RUN lock before this entry writes its episode, under the lease fence when there is one.

    With *lease*, the EXECUTION row is locked first and checked to still be
    this worker's (:func:`_fence_on_lease`), held with the run lock to the
    end-of-entry commit: a worker whose evaluation was taken back (its stop
    said, its presence lapsed in a stall) writes no entry beside the worker
    that resumes it, and leaves on ``LeaseLostError``. The same order as the
    pre-dispatch reclaim, execution then run.

    The lock order is EXECUTION -> RUN -> EPISODE. Without this lock the entry
    loop takes them implicitly the other way: ``finalize_episode`` and
    ``append_step`` flush, locking the EPISODE row, and the run is written only
    at the end-of-entry commit. ``close_episodes_for_cancelled_run`` takes RUN
    then EPISODE, so a cancel landing in that window deadlocks against the
    worker. The sweep avoids it with ``skip_locked``; the cancel endpoint
    cannot (a skipped row is an entry left open on a run nothing revisits).

    Taken HERE, after the judge call and before the first episode write: the
    hold is DB-only and ends at the commit just below. Locking at the top of
    the iteration would hold the run across both model calls and block
    cancellation for minutes.
    """
    if lease is not None:
        await _fence_on_lease(db, lease[0], lease[1], f"evaluation run {run_uuid}: entry write")
    await db.execute(
        select(EvaluationRun.id).where(EvaluationRun.id == run_uuid).with_for_update()
    )


async def _lease_still_ours(db: AsyncSession, lease: RunLease | None) -> bool:
    """Whether the execution's lease is still this worker's: read, not locked, before an entry starts."""
    if lease is None:
        return True
    from app.models.orchestration import OrchestrationExecution

    owner = (
        await db.execute(
            select(OrchestrationExecution.lease_owner).where(OrchestrationExecution.id == lease[0])
        )
    ).scalar_one_or_none()
    return owner == lease[1]


async def _stop_if_the_user_cancelled(db: AsyncSession, run_uuid, run_id: str, where: str) -> bool:
    """True when *run_uuid* now reads ``cancelled``, or is gone, after closing its episodes.

    The per-run stop. The execution-level ``cancel_event``
    cannot stop one run: a trigger stamps ONE execution across the agent x
    model matrix, so firing it stops every sibling. A run sharing its
    execution stops by reading its own status instead.

    A COLUMN select, so the identity map cannot answer it: sessions use
    ``expire_on_commit=False``, so ``run.status`` in memory is the worker's
    last write, ``"running"``, whatever the endpoint committed since.

    Re-closes the episodes on the way out. ``cancel_run`` closed them, but the
    entry in flight runs to its end, and ``finalize_episode`` has no terminal
    guard: a callback-backed entry can put its episode back to
    ``waiting_callback`` after the endpoint closed it, and a late callback
    would be judged (and billed) on a cancelled run. Re-closing keeps the exit
    consistent whoever won the race. Idempotent: only non-terminal episodes are
    touched.

    Takes no lock itself; ``close_episodes_for_cancelled_run`` takes run then
    episodes, the global order, and the worker holds no execution lock here.

    PRECONDITION: the caller holds no unflushed ``run`` state. The
    close flushes the whole session and this commits it, so anything dirty
    rides along. Both call sites follow a commit, but nothing enforces it. A
    flush writes only changed columns, so an unflushed ``completed_entries``
    is merely persisted early; an assignment to ``run.status`` before one of
    these probes would put ``"running"`` back over ``"cancelled"`` and undo
    the cancel.
    """
    status = (
        await db.execute(
            select(EvaluationRun.status).where(EvaluationRun.id == run_uuid)
        )
    ).scalar_one_or_none()
    # A deleted run (``None``) stops too: its deletion went through the
    # cancel path, and nothing it does now has a row to land on.
    if status is not None and status != "cancelled":
        return False

    logger.info("Evaluation run %s was cancelled; stopping %s", run_id, where)
    from app.services.evaluation_continuation import close_episodes_for_cancelled_run

    await close_episodes_for_cancelled_run(db, run_id=run_uuid)
    await db.commit()
    return True


async def _pause_for_the_stop(
    db: AsyncSession, run_uuid: uuid.UUID, run_id: str, where: str, lease: RunLease | None
) -> None:
    """Put a run its stopping worker pauses back to ``pending``, at an entry boundary.

    Conditional on ``"running"``, so a cancel committed meanwhile stands, and
    under the lease fence, so a worker whose evaluation was already taken back
    never flips the run its new worker is running. The entries it finished
    stay; the next worker resumes from them (:func:`_settled_entries`). Never
    raises: a pause that cannot be written (a lost lease, a database error)
    leaves the run ``running``, which the next attempt resumes on the stop's
    word (``_resume_runs_cut_by_a_stop``) - failing it here would lose its
    entries. Same precondition as :func:`_stop_if_the_user_cancelled`:
    nothing of the run left unflushed.
    """
    try:
        if lease is not None:
            await _fence_on_lease(db, lease[0], lease[1], f"evaluation run {run_id}: pause")
        paused = (
            await db.execute(
                update(EvaluationRun)
                .where(EvaluationRun.id == run_uuid, EvaluationRun.status == "running")
                .values(status="pending")
                .returning(EvaluationRun.id)
            )
        ).first()
        await db.commit()
    except Exception as exc:
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.warning(
            "Evaluation run %s not paused %s, left running for the next worker to resume: %s",
            run_id,
            where,
            format_exception_message(exc),
        )
        return
    if paused is not None:
        logger.warning(
            "Evaluation run %s paused %s: its worker is stopping, it resumes on the next worker",
            run_id,
            where,
        )


#: The episode states a resumed run leaves as they are: the entry finished (closed), or waits on its callback.
_SETTLED_EPISODE_STATUSES = (*TERMINAL_EPISODE_STATUSES, *PARKED_EPISODE_STATUSES)


def _has_parked_episode() -> ColumnElement[bool]:
    """Whether the ``EvaluationRun`` row in the query has a parked callback episode: its callbacks drive it.

    The one test the stop hand-back, the resume and the abandoned-run repair share, so they agree on which runs
    a worker stop leaves alone.
    """
    return (
        select(EvaluationEpisode.id)
        .where(
            EvaluationEpisode.run_id == EvaluationRun.id,
            EvaluationEpisode.status.in_(PARKED_EPISODE_STATUSES),
        )
        .exists()
    )


async def _settled_entries(db: AsyncSession, run_uuid: uuid.UUID) -> set[uuid.UUID]:
    """The entries of a run that a resume skips: closed, parked on a callback, or with a result.

    A fresh run has none. A run resumed after its worker stopped (paused at an
    entry boundary, or put back by ``_resume_runs_cut_by_a_stop``) keeps every
    entry it finished: an entry closes its episode and stores its result in
    one commit, and a callback entry that failed or timed out closes its
    episode with no result. Only the entry the stop cut is left: its episode
    ``running`` or ``pending``, deleted by the reclaim, or none.
    """
    with_result = select(EvaluationResult.entry_id).where(EvaluationResult.run_id == run_uuid)
    settled = select(EvaluationEpisode.entry_id).where(
        EvaluationEpisode.run_id == run_uuid,
        EvaluationEpisode.status.in_(_SETTLED_EPISODE_STATUSES),
    )
    rows = await db.execute(with_result.union(settled))
    return set(rows.scalars().all())


@known_secrets_scoped("run_id")
async def execute_evaluation_run(
    db: AsyncSession,
    run_id: str,
    *,
    stopping: "asyncio.Event | None" = None,
    lease: RunLease | None = None,
) -> None:
    """Execute a single evaluation run (one agent + one model).

    A run cancelled out of band keeps its ``cancelled`` status — the
    completion path never rewrites it as ``completed``. A stop of the whole
    execution (emergency stop, lease loss) interrupts the run from outside,
    through the fan-out (:func:`execute_evaluation`).

    *stopping* set (its worker is stopping: a deploy, a restart): a run not
    started yet stays ``pending``, and a running one pauses at its next entry
    boundary, ``pending`` again (:func:`_pause_for_the_stop`). A run that
    finished entries before resumes from them: they are skipped and counted
    in its totals (:func:`_settled_entries`).

    *lease* (``(execution_id, worker_id)``): each entry starts only while the
    execution's lease is this worker's, and writes under its fence
    (:func:`_lock_run_before_episode_writes`). Once the evaluation was taken
    back, the run leaves at the next check without writing: its new worker
    resumes it.
    """
    # Bound before the handlers below name it; lazy, as elsewhere here: the
    # engine imports the adapter registry.
    from app.services.orchestration_engine import LeaseLostError

    run_uuid = uuid.UUID(run_id)
    if lease is not None:
        # The start is a write too: a worker whose evaluation was taken back
        # (before its heartbeat noticed) must not flip a queued run to
        # ``running``, which its new worker would then skip. Execution
        # before run, the lock order; held to the commit of the transition.
        try:
            await _fence_on_lease(db, lease[0], lease[1], f"evaluation run {run_id}: start")
        except LeaseLostError:
            await db.rollback()
            logger.warning("Evaluation run %s not started: its evaluation was taken back", run_id)
            return
    result = await db.execute(
        # LOCKED, so the ``status != "pending"`` guard and the transition after
        # it are one decision. Unlocked, a cancel committing between
        # them would put a CANCELLED run back into ``"running"``, and the
        # user's stop would do nothing for it.
        #
        # Held only to the commit just below, before any model call; run-only,
        # so the run-then-episode order downstream is unaffected.
        select(EvaluationRun)
        .where(EvaluationRun.id == run_uuid)
        .with_for_update()
    )
    run = result.scalar_one_or_none()
    if not run:
        logger.warning(
            "Evaluation run %s not found when worker tried to execute it — skipping",
            run_id,
        )
        return
    if run.status != "pending":
        # Claimed or settled between enqueue and execution: logged so a run
        # that vanished is debuggable. Likely another worker ran it, or
        # its own execution's reclaim of abandoned runs failed it.
        logger.warning(
            "Evaluation run %s skipped — status is '%s' (expected 'pending'). "
            "It was already executed by another worker, or settled before "
            "execution.",
            run_id,
            run.status,
        )
        return
    if stopping is not None and stopping.is_set():
        # Its worker is stopping: not started here, left ``pending`` for the
        # next one. The rollback releases the row lock.
        await db.rollback()
        logger.info("Evaluation run %s not started: its worker is stopping", run_id)
        return

    # A run its stopping worker left (paused, or put back by the next
    # attempt's reclaim) already started: it resumes, and keeps its first start.
    resumed = run.started_at is not None
    run.status = "running"
    run.started_at = run.started_at or datetime.now(UTC)
    await db.commit()
    logger.info(
        "Evaluation run %s started (agent=%s model=%s)",
        run_id,
        run.agent_id,
        run.model_id,
    )

    try:
        # A flow subject (#8257) runs in place of the agent; every agent-only
        # step below is skipped for it.
        flow: Flow | None = None
        agent: Agent | None = None
        if run.flow_id is not None:
            flow = await db.get(Flow, run.flow_id)
            subject_missing = flow is None
        else:
            agent_result = await db.execute(select(Agent).where(Agent.id == run.agent_id))
            agent = agent_result.scalar_one_or_none()
            subject_missing = agent is None
        if subject_missing:
            run.status = "failed"
            run.error = "Flow not found" if run.flow_id is not None else "Agent not found"
            run.completed_at = datetime.now(UTC)
            await db.commit()
            return
        if flow is not None:
            # Written with the run's settings (``stamped`` below), before
            # entry 1: the launch's stamp of a run cut in its setup is not
            # one its entries ran.
            stamped_version = run.flow_version_number if resumed and run.execution_mode is not None else None
            if stamped_version is not None and stamped_version != flow.version_number:
                # Edited while the run was paused: its next entries would run
                # another flow than the ones it finished, under one label.
                run.status = "failed"
                run.error = (
                    f"The flow changed while the run was paused (v{stamped_version} → "
                    f"v{flow.version_number}) — re-run to retry"
                )
                run.completed_at = datetime.now(UTC)
                await db.commit()
                return
            # The version this run executes, which the reuse fingerprint reads:
            # a run reads the flow as it is when it starts, not at launch.
            run.flow_version_number = flow.version_number

        # Per-agent prediction extractor, resolved ONCE per run: it
        # cannot change mid-run, and a per-entry query would be an N+1.
        agent_output_extractor = (
            await load_extractor_for_agent(db, run.evaluation_id, run.agent_id)
            if agent is not None
            else None
        )
        extraction_failure_reasons: dict[str, int] = {}
        scoring_errors = 0
        # Per failure cause, the entries the platform could not run.
        platform_errors: dict[str, int] = {}

        eval_result = await db.execute(
            select(Evaluation).where(Evaluation.id == run.evaluation_id)
        )
        evaluation = eval_result.scalar_one_or_none()
        if not evaluation:
            run.status = "failed"
            run.error = "Evaluation not found"
            run.completed_at = datetime.now(UTC)
            await db.commit()
            return
        # Its entries act as the evaluation's owner: checked when the run
        # starts or resumes, as at launch.
        from app.services.evaluation.launch import owner_refusal

        if (refused := await owner_refusal(db, evaluation)) is not None:
            run.status = "failed"
            run.error = refused
            run.completed_at = datetime.now(UTC)
            await db.commit()
            return

        # A run on a saved model is answered by that model or not at all:
        # the resolver would fall back to another provider's model, and the run
        # would report that model's scores under this one. The agent-default run
        # runs on whatever the agent resolves to, and a model-independent agent
        # calls no model.
        if (
            agent is not None
            and not run.is_default_model
            and not getattr(agent, "skip_llm", False)
            and not await run_model_served(db, run.model_id, evaluation_owner_id(evaluation))
        ):
            run.status = "failed"
            run.error = unavailable_model_message([run.model_id])
            run.completed_at = datetime.now(UTC)
            await db.commit()
            return

        # A flow subject has no persona, prompt or skill snapshot; a placebo arm
        # or a callback is an agent's (#8257).
        flow_refusal = None
        if flow is not None and run.placebo_comparison_id is not None:
            flow_refusal = "A placebo comparison applies to agents only."
        elif flow is not None and (getattr(evaluation, "trigger_config", None) or {}).get("await_callback"):
            flow_refusal = "Callback mode applies to agents only."
        if flow_refusal is not None:
            run.status = "failed"
            run.error = flow_refusal
            run.completed_at = datetime.now(UTC)
            await db.commit()
            return
        evaluation_overrides, missing_snapshots = (
            await resolve_run_snapshot_overrides(db, run) if agent is not None else ({}, [])
        )
        if missing_snapshots:
            run.status = "failed"
            run.error = (
                "Replay snapshot(s) missing; run is no longer reproducible: "
                + ", ".join(missing_snapshots)
            )
            run.completed_at = datetime.now(UTC)
            await db.commit()
            return

        # A placebo comparison's control run: its placebo overlays the
        # snapshot overrides. Built in the run model's tokens on the first start
        # and stored with the run, so a later attempt or replay runs the same text.
        if run.placebo_comparison_id is not None and run.arm != ARM_REAL:
            from app.services.evaluation.placebo_runs import (
                PlaceboUnavailableError,
                build_placebo_snapshot,
                control_arm_overrides,
                sibling_snapshot,
            )
            from app.services.evaluation_placebo import PlaceboLeakError

            try:
                comparison = await db.get(EvaluationPlaceboComparison, run.placebo_comparison_id)
                if comparison is None:
                    raise PlaceboUnavailableError("Its placebo comparison no longer exists")
                if run.arm == ARM_PLACEBO and not run.placebo_snapshot:
                    run.placebo_snapshot = await sibling_snapshot(db, run) or await build_placebo_snapshot(
                        db,
                        run,
                        agent,
                        comparison,
                        evaluation_overrides,
                        user_id=evaluation_owner_id(evaluation),
                    )
                evaluation_overrides.update(control_arm_overrides(run, agent, run.placebo_snapshot))
            except (PlaceboUnavailableError, PlaceboLeakError) as placebo_err:
                run.status = "failed"
                run.error = str(placebo_err)
                run.completed_at = datetime.now(UTC)
                await db.commit()
                return

        entries, entry_records = await _run_entry_records(db, run, evaluation)

        result_objects: list[EvaluationResult] = []
        anomaly_flags: list[bool] = []
        # Whether each result this loop stored counts in a headline, read when it is
        # built (a failed entry's rollback expires the results stored before it):
        # one that does settles the run's outcome with no query.
        counted_flags: list[bool] = []
        total_agent_ms = 0
        run_uuid = run.id
        # Resumed after its worker stopped: the entries it finished are
        # skipped, and their results counted in the totals written at the end
        # as if this loop had stored them.
        settled_entries = await _settled_entries(db, run_uuid) if resumed else set()
        if settled_entries:
            # The columns the totals read, not the rows: a result's
            # ``execution_details`` is large and the run would hold every one.
            prior_results = (
                await db.execute(
                    select(
                        EvaluationResult.execution_anomaly_kind,
                        EvaluationResult.is_judge_failure,
                        EvaluationResult.duration_seconds,
                    ).where(EvaluationResult.run_id == run_uuid)
                )
            ).all()
            anomaly_flags.extend(r.execution_anomaly_kind is not None for r in prior_results)
            counted_flags.extend(counts_in_headlines(r) for r in prior_results)
            # A callback run's agent time is its first turns' alone: a
            # result its callback stored is not counted, as on a run never
            # resumed.
            if not ((getattr(evaluation, "trigger_config", None) or {}).get("await_callback")):
                total_agent_ms += round(sum(r.duration_seconds or 0 for r in prior_results) * 1000)
            logger.warning(
                "Evaluation run %s resumes after its worker stopped: %d of %d entries "
                "already finished",
                run_id,
                len(settled_entries),
                len(entries),
            )
        model_id = run.model_id
        evaluation_instructions = evaluation.evaluation_instructions
        evaluator_model = evaluation.evaluator_model
        evaluator_prompt = evaluation.evaluator_prompt
        sub_score_weights = evaluation.sub_score_weights
        # Every value but ``full_loop`` runs as LLM Only, so a stored value
        # that is neither mode resolves to what it runs.
        execution_mode_default = (
            "full_loop" if evaluation.execution_mode == "full_loop" else "llm_only"
        )
        # skip_llm agents run their deterministic pipeline in evaluations too:
        # no chat model is invoked, and ``exec_mode="skip_llm"`` keeps
        # their outputs out of anomaly detection.
        agent_is_skip_llm = bool(getattr(agent, "skip_llm", False))
        owner_id = evaluation_owner_id(evaluation)
        # The mode every entry of this run executes and records in (#4676).
        run_execution_mode = (
            FLOW_EXECUTION_MODE
            if flow is not None
            else "skip_llm"
            if agent_is_skip_llm
            else execution_mode_default
        )
        # A resumed run carries on under the settings it was stamped with when
        # it started, never an edit made to the evaluation meanwhile: its
        # entries are scored alike, under the label it shows. Stamped is not
        # started: ``started_at`` commits before the stamps (written with the
        # lineage below, before the first entry), so a run cut during its setup
        # has none and reads the evaluation as a fresh run does.
        stamped = resumed and run.execution_mode is not None
        if stamped:
            run_execution_mode = run.execution_mode
        # Judge gating reads ``judge_enabled`` on the RAW selection so an
        # explicit judge-off is honoured.
        judge_selected = judge_enabled(
            run.scorer_selection if stamped else getattr(evaluation, "scorer_selection", None)
        )
        # Judge provenance, stamped next to the gate it records. On the
        # run, so a later edit to the evaluation cannot claim a past run was
        # judged: the headline pass reads this snapshot, never the parent.
        #
        # Copied, not aliased: a shared list would let an in-place edit to the
        # evaluation rewrite the snapshot too.
        if not stamped:
            run.scorer_selection = _snapshot_selection(
                getattr(evaluation, "scorer_selection", None)
            )
        # What the run's label reads, for the same reason: the
        # evaluation's mode can change. Committed with the lineage stamps
        # below, before the first entry. Written once: a run its stopping
        # worker returned to pending resumes under it (``stamped`` above); a
        # replay or re-run is a new run, an episode resume runs no agent.
        run.execution_mode = run_execution_mode
        # Whether the run checks its prompts and answers with the prompt guard,
        # stamped beside the mode for the same reason. ``False`` when
        # the run is not Full Loop: LLM Only and the skip_llm pipeline never
        # reach a guard check, so off is what such a run applied.
        run_apply_prompt_guards = run_execution_mode == "full_loop" and bool(
            run.apply_prompt_guards if stamped else evaluation.apply_prompt_guards
        )
        run.apply_prompt_guards = run_apply_prompt_guards
        # Whether the run holds tool calls that need approval, stamped
        # the same way. ``False`` outside Full Loop: LLM Only calls no tool, and
        # a pipeline turn returns before the gate is installed.
        run_require_tool_approval = run_execution_mode == "full_loop" and bool(
            run.require_tool_approval if stamped else evaluation.require_tool_approval
        )
        run.require_tool_approval = run_require_tool_approval
        # The contract the delivery step reads: an answer delivered under another one is
        # not reused.
        from app.services.output_format import effective_output_contract

        run_output_format, run_output_schema = (
            effective_output_contract(agent.output_format, agent.output_schema, None)
            if agent is not None
            else (None, None)
        )
        # Deterministic quality comes from the metric_package seam. The
        # family config is built ONCE per run (CLASSIFICATION derives its
        # vocabulary from every dataset reference); a configuration error
        # degrades to judge-only, never a crash or a fabricated score.
        family = getattr(evaluation, "family", None)
        # A run-level configuration error leaves every entry out with its reason,
        # so the run page says why instead of a bare N/A.
        quality_config, quality_config_error = _run_quality_config(run, evaluation, entry_records)
        # PERFORMANCE metrics come from the same seam. Config and price
        # table are resolved ONCE per run (one ``model_id`` per run). A missing
        # price yields an ``unavailable`` cost, never a fabricated zero.
        performance_config = PerformanceConfig()
        performance_price_rows = await _build_run_price_rows(db, run.model_id)
        # Price rows per serving model, for entries whose chain spans several.
        chain_price_rows: dict[str, list[dict[str, object]]] = {}
        # Score lineage, stamped HERE where the suite computes the
        # entries, not in the aggregation pass, which re-aggregates stored metrics
        # without the suite. A rescore re-invokes it and stamps its own.
        # Read from the package's own declaration so it cannot drift from the
        # code that produced the scores.
        run.metric_suite_version = metric_suite_version()
        trigger_config = getattr(evaluation, "trigger_config", None) or {}
        await_callback = bool(trigger_config.get("await_callback"))
        callback_timeout = int(
            trigger_config.get("callback_timeout_seconds")
            or getattr(evaluation, "timeout_seconds", 60)
            or 60
        )
        agent_id_str = str(run.flow_id or run.agent_id)
        integration_tools_bundle: IntegrationToolsBundle | None = None

        # Capture the experimental condition. Auto params come from the run,
        # agent and evaluation; evaluation ``custom_params`` are logged as
        # ``source=evaluation``. Manual params are captured at trigger time and
        # win the upsert (manual > evaluation > auto). A failure never fails
        # the run.
        #
        # In a SAVEPOINT: the stamps above (`scorer_selection`,
        # `metric_suite_version`) are pending, and a plain `rollback()` would
        # discard them, leaving NULL, which reads as judge-on, on the judge-off
        # run the column protects. Params are diagnostic; lineage is not.
        try:
            from app.services.evaluation import params as run_params

            async with db.begin_nested():
                await run_params.capture_run_params(
                    db,
                    run,
                    agent,
                    evaluation,
                    execution_mode=execution_mode_default,
                )
        except Exception as param_err:  # pragma: no cover - defensive
            # The savepoint rolled back the partial write. NOT `db.rollback()`:
            # the lineage stamps are committed below either way.
            logger.warning(
                "Failed to capture run params for run %s: %s", run_uuid, param_err
            )
        await db.commit()

        # Prediction reuse: the per-trigger ``reuse`` param (manual, set at
        # trigger time), else the evaluation's ``reuse_default``. Await-callback
        # runs never reuse (their first turn is continued externally).
        reuse_enabled = False
        kb_state: list = []
        try:
            from app.services.evaluation import params as run_params

            param_rows = await run_params.get_run_params(db, run_uuid)
            reuse_val = next(
                (p["value"] for p in param_rows if p["key"] == "reuse"), None
            )
            reuse_effective = (
                reuse_val
                if reuse_val in ("on", "off")
                else getattr(evaluation, "reuse_default", "off")
            )
            # A placebo comparison's runs never reuse: every arm and
            # trial is an execution of its own, or the statistics compare copies.
            reuse_enabled = (
                (reuse_effective == "on")
                and not await_callback
                and run.placebo_comparison_id is None
            )
        except Exception as reuse_err:  # pragma: no cover - defensive
            logger.warning(
                "Failed to resolve reuse for run %s: %s", run_uuid, reuse_err
            )
            reuse_enabled = False

        # The KB state is captured once per run, so a KB change invalidates
        # ``full_loop`` reuse for every entry alike. Captured reuse on or off:
        # every result's fingerprint says what its answer was produced
        # from, so a later run with reuse on can reuse it.
        if run_execution_mode == "full_loop":
            try:
                kb_state = await load_agent_kb_state(db, agent)
            except Exception as kb_err:  # pragma: no cover - defensive
                logger.warning(
                    "Failed to load the knowledge-base state for run %s: %s",
                    run_uuid,
                    kb_err,
                )
                reuse_enabled = False

        if execution_mode_default == "full_loop" and agent is not None:
            try:
                integration_tools_bundle = await _build_evaluation_integration_tools(
                    db,
                    agent,
                    evaluation=evaluation,
                )
            except Exception as bundle_err:
                logger.warning(
                    "Failed to prebuild evaluation integration tools for run %s: %s",
                    run_uuid,
                    format_exception_message(bundle_err),
                )
                integration_tools_bundle = None

        # Part of the reuse fingerprint: an answer stored without the
        # floor ran under a lower cap. Read whether or not this run reuses: the
        # fingerprint is written on every result, for later runs to match.
        run_floored_max_iterations = (
            await floored_max_iterations(db, agent, evaluation_overrides)
            if run_execution_mode == "full_loop"
            else None
        )

        # A control run's answer is never the agent's own: its arm, trial
        # and placebo are part of what produced it, so no later reuse-on run
        # takes it for a real one.
        placebo_fingerprint = (
            {"arm": run.arm, "trial": run.trial_index, "placebo": run.placebo_snapshot}
            if run.placebo_comparison_id is not None and run.arm != ARM_REAL
            else None
        )

        # A flow subject's runs share the evaluation's timeout (#8257).
        flow_deadline = time.monotonic() + float(evaluation.timeout_seconds or 3600)
        for entry_idx, _entry in enumerate(entries, 1):
            entry_record = entry_records[entry_idx - 1]
            entry_id = entry_record["id"]
            # Finished before the stop: skipped first, with no query.
            if entry_id in settled_entries:
                continue
            # COOPERATIVE CANCELLATION, per run. ``cancel_run`` cancels
            # the execution only when this run is its only one; for a shared
            # execution the stop arrives here (see
            # ``_stop_if_the_user_cancelled``).
            #
            # COST: the stop lands at an entry BOUNDARY, so the entry in flight
            # finishes and is paid for: bounded by one entry, not the rest of
            # the dataset. Return, not ``break``: the block after this loop
            # writes ``status = "completed"``, which would report a cancelled
            # run as a success.
            if await _stop_if_the_user_cancelled(
                db, run_uuid, run_id, f"at entry {entry_idx} of {len(entries)}"
            ):
                return
            # Its worker is stopping: paused here, between two entries, rather
            # than cut in the middle of one when the stop's grace runs out. Not
            # a callback-backed run: its parked entries are driven by their
            # callbacks, and it stays as it was.
            if stopping is not None and stopping.is_set() and not await_callback:
                await _pause_for_the_stop(
                    db, run_uuid, run_id, f"at entry {entry_idx} of {len(entries)}", lease
                )
                return
            # Taken back meanwhile (its worker said it stopped, or its presence
            # lapsed in a stall): the new worker runs it now, so no entry starts here.
            if not await _lease_still_ours(db, lease):
                await db.rollback()
                logger.warning(
                    "Evaluation run %s: its evaluation was taken back, stopping at entry %d of %d",
                    run_id,
                    entry_idx,
                    len(entries),
                )
                return

            input_prompt = entry_record["input_prompt"]
            file_ids = entry_record["file_ids"]
            expected_answer = entry_record["expected_answer"]
            episode = None
            try:
                episode = await ConversationContinuationService.start_episode(
                    db,
                    run_id=run_uuid,
                    entry_id=entry_id,
                    correlation_id=f"eval:{run_uuid}:{entry_id}",
                )
                file_refs = await _load_file_refs(db, file_ids)
                exec_mode = run_execution_mode

                # With reuse on and a valid matching prior prediction, the agent
                # does not run and the stored output and traces are reused;
                # scorers ALWAYS re-run below.
                input_fingerprint = compute_input_fingerprint(
                    agent_id=run.agent_id,
                    flow_id=run.flow_id,
                    flow_version=run.flow_version_number if flow is not None else None,
                    model_id=model_id,
                    execution_mode=exec_mode,
                    entry_input=input_prompt,
                    entry_file_ids=file_ids,
                    persona_version=run.persona_version_number,
                    prompt_versions=run.prompt_versions,
                    agent_config_versions=run.agent_config_versions,
                    kb_state=kb_state,
                    floored_max_iterations=run_floored_max_iterations,
                    apply_prompt_guards=run_apply_prompt_guards,
                    require_tool_approval=run_require_tool_approval,
                    output_format=run_output_format,
                    output_schema=run_output_schema,
                    placebo=placebo_fingerprint,
                )
                reused_from_id = None
                reused_output = None
                reused_details: dict | None = None
                reused_trace_ids: list[str] = []
                reused_source_duration_seconds: float | None = None
                if reuse_enabled:
                    source = await _find_reusable_prediction(
                        db, evaluation.id, run_uuid, input_fingerprint
                    )
                    if source is not None:
                        reused_from_id = source.id
                        reused_output = source.agent_output
                        reused_details = dict(source.execution_details or {})
                        # Read NOW, before ``release_db_connection_before_loop``
                        # expires the row (a later read would refresh on a
                        # released connection). The last-resort duration of a
                        # timeline without per-step durations.
                        reused_source_duration_seconds = source.duration_seconds

                # Hand the pooled connection back before the agent runs, which
                # can take minutes: the reads above opened a transaction that
                # would pin one connection per in-flight run across the agent
                # loop and judge call. The writes below re-acquire.
                from app.core.database import release_db_connection_before_loop

                await release_db_connection_before_loop(db)

                # LLM Only offers no tool; the other paths set the agent's list.
                available_tools: list[str] | None = []
                # The agent run's outcome, its ledger included.
                agent_outcome: dict[str, object] = {}
                # The proposals of the calls this entry's chain held.
                entry_approvals: list = []
                # A flow subject's run on this entry, and a sandbox copy's case
                # report (#8257).
                flow_outcome: FlowEntryOutcome | None = None
                sandbox_report: dict | None = None
                # Meter the retries the provider layer absorbs during THIS
                # entry: invisible above ``retry_llm_call``, they would leave
                # the retry metric uncaptured rather than a measured zero.
                with count_llm_retries() as _retry_meter:
                    # Latency is elapsed time to the answer: timed around
                    # the agent call, not rebuilt from its steps afterwards.
                    _agent_clock_t0 = time.perf_counter()
                    if reused_from_id is not None:
                        agent_output = reused_output or ""
                        # Carry the agent-side timeline forward so
                        # duration and rescore work. The source's ``judge`` and
                        # ``judge_skipped`` steps are scoring-side and the judge
                        # re-runs below, so they are dropped.
                        agent_local_traces = _agent_timeline(
                            (reused_details or {}).get("traces")
                        )
                        agent_trace_ids = []
                        # The source's spans, for a timeline-less source.
                        reused_trace_ids = [
                            str(t) for t in ((reused_details or {}).get("trace_ids") or [])
                        ]
                        _agent_ttft = (reused_details or {}).get("ttft_ms")
                        exec_mode = (reused_details or {}).get("execution_mode") or exec_mode
                        # The source's list, never ``[]``: an empty list grades
                        # tool selection 100 unjudged.
                        available_tools = _recorded_available_tools(reused_details)
                        # The source's sandbox case report: its answer rested on
                        # the writes it counts (#8257).
                        sandbox_report = (reused_details or {}).get(SANDBOX_KEY)
                    elif flow is not None:
                        flow_outcome = await run_flow_for_entry(
                            db,
                            flow,
                            evaluation_run_id=run_uuid,
                            entry_id=entry_id,
                            input_prompt=input_prompt,
                            owner_id=evaluation.user_id,
                            # What is left of the evaluation's timeout, never a
                            # full one per entry: the execution's deadline is one.
                            timeout_seconds=max(flow_deadline - time.monotonic(), 1.0),
                            is_cancelled=lambda: _evaluation_run_cancelled_out_of_band(db, run_uuid),
                            case_ref=sandbox_case_ref(run_uuid, entry_id),
                        )
                        sandbox_report = flow_outcome.sandbox
                        agent_output = flow_outcome.output
                        agent_trace_ids = flow_outcome.trace_ids
                        agent_local_traces = flow_outcome.timeline
                        _agent_ttft = None
                        # Unknown, not none: the judge grades the calls it sees.
                        available_tools = None
                        agent_outcome[LEDGER_KEY] = flow_outcome.ledger
                    elif exec_mode in ("full_loop", "skip_llm"):
                        # A failure fails the entry with its error, score 0. No
                        # LLM Only fallback: an answer from the persona
                        # alone measures nothing about the agent. A
                        # model-independent agent takes the same call:
                        # ``run_agent_chat`` runs its deterministic pipeline
                        # through the skip_llm fast path, with no chat model.
                        # A sandbox copy's turn runs as one case of level 2's
                        # interception, on the entry's recordings (#8257).
                        async with agent_sandbox_case(
                            db, agent, run_id=run_uuid, entry_id=entry_id, user_id=owner_id, model=model_id
                        ) as sandbox_session:
                            (
                                agent_output,
                                agent_trace_ids,
                                _agent_ttft,
                                available_tools,
                                agent_local_traces,
                            ) = await _execute_agent_full_loop(
                                db,
                                agent,
                                model_id,
                                input_prompt,
                                file_refs or None,
                                integration_tools_bundle=integration_tools_bundle,
                                evaluation=evaluation,
                                evaluation_overrides=evaluation_overrides,
                                outcome_out=agent_outcome,
                                apply_prompt_guards=run_apply_prompt_guards,
                                require_tool_approval=run_require_tool_approval,
                                approvals_out=entry_approvals,
                            )
                        if sandbox_session is not None:
                            sandbox_report = sandbox_session.report.to_dict()
                    else:
                        agent_output, agent_trace_ids, _agent_ttft, agent_local_traces = (
                            await _execute_agent_llm_only(
                                db,
                                agent,
                                model_id,
                                input_prompt,
                                file_refs or None,
                                evaluation_overrides=evaluation_overrides,
                                user_id=owner_id,
                            )
                        )

                # What the latency metric reads: elapsed time to the
                # answer, not ``agent_duration_ms`` below. That sum of recorded
                # steps misses the gaps, a blocking park, and the whole turn when
                # the timeline is empty (a transfer reads 0 ms, a perfect
                # latency). ``duration_seconds`` and the run totals still use it.
                agent_wall_clock_ms: int | None = _ms(
                    _agent_clock_t0, time.perf_counter()
                )
                if flow_outcome is not None:
                    # The flow run's own clock, not the time spent polling it.
                    agent_wall_clock_ms = flow_outcome.wall_clock_ms
                # Delegated work the agent spawned (#4675): waited for after the
                # clock and before the judge, so latency stays the time to the
                # answer and the entry is recorded once that work is done.
                delegated_ledgers: dict[str, RunLedger | None] = {}
                wait_stopped: dict[str, IncompleteReason] = {}
                _spawned = (agent_outcome.get(LEDGER_KEY) or {}).get("spawned_task_ids") or []
                if reused_from_id is None and _spawned:
                    delegated_ledgers = await _collect_task_ledgers(
                        list(_spawned),
                        bound_seconds=await _delegation_wait_seconds(run),
                        run_uuid=run_uuid,
                        reason_out=wait_stopped,
                    )

                # A cancel that landed DURING the agent call ends the run here:
                # the callback handling and the judge call below are new model
                # work nobody wants after an emergency stop.
                if await _evaluation_run_cancelled_out_of_band(db, run.id):
                    break

                # Prefer the in-memory timeline for durations: the trace rows are
                # flushed in a background task and may not be queryable yet.
                agent_duration_ms = _step_total_ms(agent_local_traces)
                if agent_duration_ms == 0 and agent_trace_ids:
                    agent_duration_ms = await _sum_trace_durations_ms(
                        db, agent_trace_ids
                    )
                if reused_from_id is not None:
                    # No ``agent_trace_ids`` on reuse, so a timeline missing
                    # durations would read 0 or an undercount: see
                    # ``_recover_reused_agent_duration_ms``.
                    agent_duration_ms = _recover_reused_agent_duration_ms(
                        agent_duration_ms,
                        reused_details,
                        reused_source_duration_seconds,
                    )
                    # No agent ran, so the clock above timed nothing.
                    agent_wall_clock_ms = _recover_reused_wall_clock_ms(reused_details)
                logger.info(
                    "Entry %s: mode=%s, trace_ids=%d, agent_duration_ms=%d",
                    entry_id,
                    exec_mode,
                    len(agent_trace_ids),
                    agent_duration_ms,
                )

                # The entry's chain ledger: the agent run's own calls and
                # every delegated task's; a reused entry's comes from its source
                # (``_reused_chain``). ``llm_only`` keeps the step-derived figures:
                # its one call has no ledger.
                entry_chain: ChainLedger | None = None
                own_ledger = agent_outcome.get(LEDGER_KEY)
                if reused_from_id is not None:
                    entry_chain = _reused_chain(reused_details, exec_mode)
                elif exec_mode == FLOW_EXECUTION_MODE:
                    # The flow run's spans (#8257); traces still being written
                    # leave its totals unavailable, never partial.
                    entry_chain = (
                        assemble_chain(own_ledger, {})
                        if isinstance(own_ledger, dict)
                        else uncounted_chain("ledger_missing")
                    )
                elif exec_mode in ("full_loop", "skip_llm") and isinstance(own_ledger, dict):
                    entry_chain = assemble_chain(
                        own_ledger,
                        delegated_ledgers,
                        entry_retries=_retry_meter["retries"],
                        stop_reason=wait_stopped.get("reason"),
                    )

                performance_results: dict[str, dict] = {}
                # PERFORMANCE metrics from the telemetry this entry
                # captured, the judge excluded. Incomplete telemetry degrades a
                # metric to ``skipped``/``unavailable``, never fails the run.
                # Keys are namespaced ``performance.*`` so they never collide
                # with the ``json.*``/``stix.*`` quality keys.
                try:
                    # In-memory timeline first, for the same flush race.
                    perf_token_usage = await _resolve_agent_token_usage(
                        db, agent_local_traces, agent_trace_ids
                    )
                    perf_chain = (
                        await _chain_inputs(
                            db, entry_chain, model_id, performance_price_rows, chain_price_rows
                        )
                        if entry_chain is not None
                        else None
                    )
                    perf_outcomes = run_performance(
                        execution_details={
                            "agent_wall_clock_ms": agent_wall_clock_ms,
                            "ttft_ms": _agent_ttft,
                            "traces": agent_local_traces,
                        },
                        token_usage=perf_token_usage,
                        retry_count=(
                            _recover_reused_retry_count(reused_details)
                            if reused_from_id is not None
                            else entry_chain["retries"]
                            if entry_chain is not None
                            else _retry_meter["retries"]
                        ),
                        price_rows=performance_price_rows,
                        config=performance_config,
                        chain=perf_chain,
                    )
                    performance_results = to_persistable(perf_outcomes)
                except Exception as perf_err:  # pragma: no cover - defensive
                    # Best-effort telemetry: never fails the run or discards
                    # the quality metrics.
                    logger.warning(
                        "Performance scoring skipped (run %s, entry %s): %s",
                        run_uuid,
                        entry_id,
                        perf_err,
                    )

                # The tools whose calls were held, from the proposals
                # the turn reported, never its text. A reused answer keeps its
                # source's, recorded under the same setting.
                if reused_details is not None:
                    held_tools = list(reused_details.get("approval_required_tools") or [])
                else:
                    held_tools = sorted(
                        {
                            str(p["tool_name"])
                            for p in entry_approvals
                            if isinstance(p, dict) and p.get("tool_name")
                        }
                    )

                if await_callback and episode:
                    # RUN before EPISODE here too. This branch parks the
                    # entry and ``continue``s, so it never reaches the lock taken
                    # after the judge, and it commits its OWN transaction: the
                    # appends and ``pause_for_callback`` take the episode lock,
                    # then ``completed_entries`` takes the run lock at the commit.
                    # The agent call is done, so the hold is DB-only.
                    await _lock_run_before_episode_writes(db, run_uuid, lease=lease)
                    if agent_trace_ids:
                        for trace_id in agent_trace_ids:
                            await ConversationContinuationService.append_step(
                                db,
                                episode=episode,
                                step={
                                    "type": "trace",
                                    "duration_ms": None,
                                    "description": f"Linked trace {trace_id}",
                                },
                            )
                    else:
                        await ConversationContinuationService.append_step(
                            db,
                            episode=episode,
                            step={
                                "type": "trace",
                                "duration_ms": None,
                                "description": "No linked traces were produced for this step",
                            },
                        )
                    episode.first_turn_output = agent_output
                    # What a fresh result would store, but the judge and quality, which
                    # read the final answer: the resume builds the result from it and
                    # scores it as a rescore would.
                    episode.first_turn_details = {
                        "version_entry_id": (
                            str(entry_record["version_entry_id"])
                            if entry_record.get("version_entry_id")
                            else None
                        ),
                        "execution_details": _execution_record(
                            agent_duration_ms=agent_duration_ms,
                            agent_wall_clock_ms=agent_wall_clock_ms,
                            ttft_ms=_agent_ttft,
                            execution_mode=exec_mode,
                            agent_traces=agent_local_traces,
                            trace_ids=agent_trace_ids,
                            metric_results=dict(performance_results),
                            chain=entry_chain,
                            available_tools=available_tools,
                            held_tools=held_tools,
                        ),
                    }
                    await ConversationContinuationService.pause_for_callback(
                        db,
                        episode=episode,
                        timeout_seconds=callback_timeout,
                    )

                    completed_result = await db.execute(
                        select(func.count(EvaluationEpisode.id)).where(
                            EvaluationEpisode.run_id == run_uuid,
                            EvaluationEpisode.status.in_(["completed", "failed", "timed_out"]),
                        )
                    )
                    run.completed_entries = completed_result.scalar() or 0
                    await db.commit()
                    continue

                # The in-memory timeline, for the same flush race; the rescore and
                # the replay read it back stored, through the same rules.
                tool_call_traces = await judge_tool_calls(
                    db, agent_local_traces, agent_trace_ids or reused_trace_ids
                )
                effective_global_instructions = judge_instructions(
                    evaluation_instructions, entry_record.get("dataset_instructions")
                )
                # A plain namespace: no ORM lazy-load refresh after a commit or
                # rollback in async code.
                score_entry = cast(
                    JudgeEntryLike,
                    SimpleNamespace(
                    id=entry_id,
                    input_prompt=input_prompt,
                    expected_answer=expected_answer,
                    evaluation_prompt=entry_record["evaluation_prompt"],
                    ),
                )
                # Release again before the judge call: the writes since the
                # last release may have reopened a transaction.
                await release_db_connection_before_loop(db)

                # Anomaly detection reads the OUTPUT TEXT, fresh or reused, so
                # the persisted kind always means "the stored output is an
                # anomaly". Reuse already excludes anomalies; re-checking is a
                # free backstop for rows that predate the detector. skip_llm
                # outputs are not LLM-authored and are excluded via exec_mode. A
                # transient provider failure surfaced as the output
                # (overload/429/5xx) is flagged too. Flag only: the agent
                # is never re-run or re-prompted.
                anomaly_subtype, anomaly_reason = classify_execution_anomaly(
                    agent_output, exec_mode
                )

                judge_t0 = time.perf_counter()
                if anomaly_subtype == ANOMALY_KIND_TRANSIENT_ERROR:
                    # No judge call (saves budget): a zeroed anomaly result,
                    # excluded from aggregates.
                    score_data = {
                        "score": 0,
                        "sub_scores": {k: 0 for k in SUB_SCORE_WEIGHTS},
                        "evaluator_reasoning": anomaly_reason,
                    }
                elif judge_selected:
                    # Confirmation-seeking entries are judged too: they
                    # count toward the aggregates like any other entry. Only a
                    # transient execution error skips the judge.
                    # A judge call that raises is the judge's failure, never the
                    # agent's: caught here, apart from the agent's run.
                    try:
                        score_data = await score_result(
                            db,
                            agent_output,
                            score_entry,
                            evaluator_model,
                            evaluator_prompt=evaluator_prompt,
                            evaluation_instructions=effective_global_instructions,
                            available_tools=available_tools,
                            tool_calls=tool_call_traces,
                            sub_score_weights=sub_score_weights,
                            judge_max_tokens=evaluation.judge_max_tokens,
                            user_id=owner_id,
                        )
                    except Exception as judge_exc:
                        logger.warning(
                            "Judge call failed (run %s, entry %s): %s",
                            run_uuid,
                            entry_id,
                            format_exception_message(judge_exc),
                        )
                        score_data = judge_failure_from_exception(judge_exc, evaluator_model)
                else:
                    score_data = {
                        "score": 0,
                        "sub_scores": {k: 0 for k in SUB_SCORE_WEIGHTS},
                        "evaluator_reasoning": "LLM judge scorer not selected for this evaluation.",
                    }
                judge_t1 = time.perf_counter()
                judge_ran = anomaly_subtype != ANOMALY_KIND_TRANSIENT_ERROR and judge_selected
                # No Judge Time for a judge that did not run: a skipped one would
                # record about 0 s, which a rescore, judging nothing, never writes.
                judge_duration_ms = _ms(judge_t0, judge_t1) if judge_ran else None

                # RUN before EPISODE for the rest of this entry: covers
                # every episode write below and the run update at the commit.
                # See the helper for why here and not higher.
                await _lock_run_before_episode_writes(db, run_uuid, lease=lease)

                judge_trace_id = score_data.get("judge_trace_id")
                linked_traces: list[tuple[str, str]] = []
                linked_traces.extend((trace_id, "agent") for trace_id in agent_trace_ids)
                if isinstance(judge_trace_id, str) and judge_trace_id:
                    linked_traces.append((judge_trace_id, "judge"))

                if episode:
                    if linked_traces:
                        for trace_id, role in linked_traces:
                            await ConversationContinuationService.append_step(
                                db,
                                episode=episode,
                                step={
                                    "type": role,
                                    "duration_ms": None,
                                    "description": f"Linked trace {trace_id}",
                                },
                            )
                    else:
                        await ConversationContinuationService.append_step(
                            db,
                            episode=episode,
                            step={
                                "type": "trace",
                                "duration_ms": None,
                                "description": "No linked traces were produced for this entry",
                            },
                        )
                    await ConversationContinuationService.finalize_episode(
                        db,
                        episode=episode,
                        first_turn_output=agent_output,
                        final_output=agent_output,
                        participants=[agent_id_str],
                    )

                total_agent_ms += agent_duration_ms

                # Most specific wins: the entry's shape, then the dataset's
                # (snapshotted for pinned runs). No evaluation-level tier: a shape
                # belongs to the ground truth, not to an experiment reusing it.
                effective_output_schema = (
                    entry_record.get("output_schema")
                    or entry_record.get("dataset_output_schema")
                )
                metric_results: dict[str, dict] = {}
                scored_extraction = None
                if quality_config is not None:
                    try:
                        # One seam for both paths: extraction when the
                        # agent has an extractor, then the family's metrics.
                        # The persisted list is a SUPERSET of this one
                        # (``[*agent_local_traces, judge_trace]``), so a rescore
                        # gets a different list. They agree because every
                        # trace-reading source filters on ``type == "tool"``; a
                        # source reading traces element-wise would silently break
                        # parity. Pinned by
                        # ``test_tool_source_parity_survives_the_judge_trace``.
                        scored = quality_with_extraction(
                            extractor=agent_output_extractor,
                            family=family,
                            reference=expected_answer,
                            agent_output=agent_output,
                            execution_details={"traces": agent_local_traces},
                            output_schema=effective_output_schema,
                            config=quality_config,
                            agent_id=agent_id_str,
                        )
                    except QualityConfigError as exc:
                        # A malformed ground-truth reference is a dataset
                        # configuration error, never a score: the entry is left out
                        # with the reason, the judge still scores it.
                        logger.warning(
                            "Quality scoring skipped (run %s, entry %s): %s",
                            run_uuid,
                            entry_id,
                            exc,
                        )
                        metric_results = _left_out_quality(family, exc)
                    except Exception as exc:
                        logger.exception(
                            "Quality scoring failed (run %s, entry %s)", run_uuid, entry_id
                        )
                        metric_results = _failed_quality(family, exc)
                        scoring_errors += 1
                    else:
                        metric_results = scored.metric_results
                        scored_extraction = scored.extracted
                        tally_extraction_failure(
                            metric_results, extraction_failure_reasons
                        )
                elif quality_config_error is not None:
                    metric_results = _left_out_quality(family, quality_config_error)

                # The agent's performance metrics, measured before the callback park.
                metric_results.update(performance_results)

                # Traces are linked via EvaluationResultTrace, but reuse
                # and rescore read ``execution_details['traces']``, so the
                # in-memory timeline (agent side + judge trace) is kept too.
                # A judge failure: unparsed, truncated or incomplete
                # verdict, excluded from aggregates like an execution anomaly.
                judge_failure = bool(score_data.get("is_judge_failure", False))
                if anomaly_subtype == ANOMALY_KIND_TRANSIENT_ERROR:
                    # The classifier's own reason, so a transient provider
                    # error is not labelled confirmation-seeking.
                    judge_skip_reason = f"Judge skipped: {anomaly_reason}"
                elif not judge_selected:
                    judge_skip_reason = "Judge skipped: judge scorer not selected for this evaluation"
                else:
                    judge_skip_reason = None
                judge_trace = _build_judge_trace(
                    score_data,
                    judge_duration_ms or 0,
                    evaluator_model,
                    judge_ran=judge_ran,
                    skip_reason=judge_skip_reason,
                )
                execution_details = _execution_record(
                    agent_duration_ms=agent_duration_ms,
                    agent_wall_clock_ms=agent_wall_clock_ms,
                    ttft_ms=_agent_ttft,
                    execution_mode=exec_mode,
                    agent_traces=agent_local_traces,
                    trace_ids=agent_trace_ids,
                    metric_results=metric_results,
                    chain=entry_chain,
                    available_tools=available_tools,
                    held_tools=held_tools,
                    judge_duration_ms=judge_duration_ms,
                    judge_trace=judge_trace,
                )
                # What was actually scored, when that is not the raw output.
                record_extracted_prediction(execution_details, scored_extraction)
                if sandbox_report is not None:
                    execution_details[SANDBOX_KEY] = sandbox_report

                eval_result_obj = EvaluationResult(
                    id=uuid.uuid4(),
                    run_id=run_uuid,
                    entry_id=entry_id,
                    version_entry_id=entry_record.get("version_entry_id"),
                    agent_output=agent_output,
                    score=score_data["score"],
                    sub_scores=score_data["sub_scores"],
                    evaluator_reasoning=score_data["evaluator_reasoning"],
                    duration_seconds=round(agent_duration_ms / 1000, 2),
                    execution_details=execution_details,
                    input_fingerprint=input_fingerprint,
                    is_reused=reused_from_id is not None,
                    reused_from_result_id=reused_from_id,
                    execution_anomaly_kind=anomaly_subtype,
                    is_judge_failure=judge_failure,
                )
                db.add(eval_result_obj)

                for trace_order, (trace_id, role) in enumerate(linked_traces, start=1):
                    try:
                        parsed_trace_id = uuid.UUID(trace_id)
                    except Exception:
                        logger.warning(
                            "Skipping invalid linked trace id on evaluation result %s: %r",
                            eval_result_obj.id,
                            trace_id,
                        )
                        continue
                    db.add(
                        EvaluationResultTrace(
                            result_id=eval_result_obj.id,
                            trace_id=parsed_trace_id,
                            trace_role=role,
                            trace_order=trace_order,
                        )
                    )
                result_objects.append(eval_result_obj)
                anomaly_flags.append(anomaly_subtype is not None)
                counted_flags.append(counts_in_headlines(eval_result_obj))

                # Per-result metrics, after a flush assigns the result id. A
                # logging failure never fails the run.
                try:
                    await db.flush()
                    from app.services.evaluation import metrics as run_metrics

                    await run_metrics.log_result_metrics(
                        db,
                        result_id=eval_result_obj.id,
                        run_id=run_uuid,
                        execution_details=execution_details,
                        is_reused=reused_from_id is not None,
                    )
                except Exception as metric_err:  # pragma: no cover - defensive
                    logger.warning(
                        "Failed to log metrics for entry %s: %s", entry_id, metric_err
                    )

            except Exception as e:
                if isinstance(e, LeaseLostError):
                    # Not an entry that failed: the evaluation is another
                    # worker's now. Nothing of this entry is written.
                    raise
                if isinstance(e, ContextOverflowError):
                    logger.info(
                        "Entry %s failed — context overflow: ~%s tokens vs %s limit",
                        entry_id,
                        e.estimated_tokens,
                        e.context_window,
                    )
                    error_output = (
                        f"Context overflow: input (~{e.estimated_tokens:,} tokens) "
                        f"exceeds model context window "
                        f"({e.context_window:,} tokens for {e.model})."
                    )
                else:
                    logger.warning("Failed to evaluate entry %s: %s", entry_id, format_exception_message(e))
                    error_output = f"Error: {format_exception_message(e)[:500]}"
                # Whose fault the raise is decides how every headline treats the
                # entry: the agent's scores 0, ours and the provider's
                # leave it out.
                raised_kind, raised_cause = classify_raised_entry(e)
                if raised_kind == ANOMALY_KIND_PLATFORM_ERROR:
                    cause = raised_cause.cause.value
                    platform_errors[cause] = platform_errors.get(cause, 0) + 1
                await db.rollback()
                # Reload what the rollback expired, or the next iteration
                # lazy-loads in async context ("greenlet_spawn has not been
                # called").
                await db.refresh(run)
                await db.refresh(agent if agent is not None else flow)
                await db.refresh(evaluation)
                try:
                    # Same order on the failure path: it also writes an episode
                    # before the end-of-entry commit.
                    await _lock_run_before_episode_writes(db, run_uuid, lease=lease)
                    episode = await ConversationContinuationService.start_episode(
                        db,
                        run_id=run_uuid,
                        entry_id=entry_id,
                        correlation_id=f"eval:{run_uuid}:{entry_id}",
                    )
                    await ConversationContinuationService.finalize_episode(
                        db,
                        episode=episode,
                        first_turn_output=None,
                        final_output=None,
                        participants=[agent_id_str],
                        error=str(e),
                    )
                    raised_details = _raised_entry_details(
                        raised_kind,
                        raised_cause,
                        error_output,
                        family=family,
                        quality_config=quality_config,
                        quality_config_error=quality_config_error,
                        reference=entry_record["expected_answer"],
                        output_schema=(
                            entry_record.get("output_schema")
                            or entry_record.get("dataset_output_schema")
                        ),
                        performance_config=performance_config,
                        price_rows=performance_price_rows,
                    )
                    eval_result_obj = EvaluationResult(
                        run_id=run_uuid,
                        entry_id=entry_id,
                        version_entry_id=entry_record.get("version_entry_id"),
                        agent_output=error_output,
                        score=0,
                        sub_scores={k: 0 for k in SUB_SCORE_WEIGHTS},
                        evaluator_reasoning=_RAISED_REASONING.get(
                            raised_kind, f"Execution failed: {error_output}"
                        ),
                        duration_seconds=0,
                        execution_details=raised_details,
                        execution_anomaly_kind=raised_kind,
                    )
                    db.add(eval_result_obj)
                    result_objects.append(eval_result_obj)
                    anomaly_flags.append(raised_kind is not None)
                    counted_flags.append(counts_in_headlines(eval_result_obj))
                except Exception:
                    logger.warning(
                        "Could not store error result for entry %s", entry_id
                    )
                else:
                    # As on the success path: a logging failure never fails the run.
                    try:
                        await db.flush()
                        from app.services.evaluation import metrics as run_metrics

                        await run_metrics.log_result_metrics(
                            db,
                            result_id=eval_result_obj.id,
                            run_id=run_uuid,
                            execution_details=raised_details,
                        )
                    except Exception as metric_err:  # pragma: no cover - defensive
                        logger.warning(
                            "Failed to log metrics for entry %s: %s", entry_id, metric_err
                        )

            if await_callback:
                completed_result = await db.execute(
                    select(func.count(EvaluationEpisode.id)).where(
                        EvaluationEpisode.run_id == run_uuid,
                        EvaluationEpisode.status.in_(["completed", "failed", "timed_out"]),
                    )
                )
                run.completed_entries = completed_result.scalar() or 0
            else:
                run.completed_entries = entry_idx
            await db.commit()

        # AFTER the loop too: the in-loop probe runs at the TOP of each
        # iteration, so it never sees a cancel landing during the LAST entry.
        # Both writes below would undo the cancel (``"completed"`` or
        # ``"running"`` over the committed ``"cancelled"``).
        #
        # NOT airtight, on purpose: a cancel committing between this probe and
        # the write below still loses. The window holds no model call, and the
        # run reported ``completed`` did complete, so nothing unearned is
        # billed. Closing it needs a conditional completion write, more than
        # the residue justifies.
        if await _stop_if_the_user_cancelled(db, run_uuid, run_id, "before finalising"):
            return

        if await_callback and run.total_entries and run.completed_entries < run.total_entries:
            # Returns before the extraction tally: its counts are per invocation,
            # so logging here would report a fraction of the run as the whole. A
            # callback-backed run therefore logs no extraction line until it
            # completes (maybe never); the run detail's
            # ``extraction_failure_count`` stays accurate.
            run.status = "running"
            await db.commit()
            return

        # Mark the run complete. The headlines are NOT computed here but by
        # ``_compute_run_headlines`` below: one implementation of each headline,
        # so a judge-off run never shows a fabricated judge 0.0.
        run.anomaly_count = sum(anomaly_flags)
        if await _evaluation_run_cancelled_out_of_band(db, run.id):
            # An emergency stop or operator cancel ended the run while it was
            # scoring: ``completed`` would report stopped work as finished, so
            # the cancel's status stands and only the timing is filled in.
            run.status = "cancelled"
            run.error = run.error or "Cancelled while running."
        elif not any(counted_flags) and await _nothing_counted(db, run_uuid):
            # Every entry raised on our side or the provider's, could not be
            # graded, or was otherwise left out: the headlines read N/A,
            # so ``completed`` would report a measurement the run did not make. A
            # callback-backed run with a failed episode ends ``failed`` too.
            run.status = "failed"
            run.error = NOTHING_COUNTED_ERROR
        else:
            run.status = "completed"
            # Clear any error a previous attempt left. The lease is
            # time-based, so a worker that stalled past it keeps executing while
            # the abandoned-run reclaim stamps an error; this last write makes
            # the final state truthful.
            run.error = None
        run.completed_at = datetime.now(UTC)

        # One line per run, ids and counts only: otherwise an extractor
        # failing on every entry leaves no server-side signal.
        log_extraction_failures(
            evaluation_id=run.evaluation_id,
            run_id=run.id,
            agent_id=run.agent_id,
            reason_counts=extraction_failure_reasons,
        )
        _log_scoring_errors(run, scoring_errors)
        _log_platform_errors(run, platform_errors)

        # Agent time only: no judge, DB or overhead.
        run.duration_seconds = round(total_agent_ms / 1000, 2)

        await db.commit()

        # Score this run's headlines now, with the batch-end function. Overall
        # Performance is comparative: rescored across every completed sibling
        # (the advisory lock in `_recompute_performance_headlines` makes
        # concurrent completions safe), N/A until a second run exists.
        # Best-effort: a headline failure must not fail a run whose entries
        # all scored; the batch-end pass recomputes it anyway.
        try:
            await _compute_run_headlines(
                db, run.evaluation_id, batch_run_ids=[str(run.id)]
            )
            await db.commit()
        except Exception as headline_err:  # pragma: no cover - defensive
            await db.rollback()
            logger.warning(
                "Headline scoring deferred to batch end for run %s: %s",
                run_id,
                headline_err,
            )

    except LeaseLostError:
        # Taken back mid-entry: the worker that resumes the run owns it, its
        # status included. Nothing is written here.
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.warning(
            "Evaluation run %s: its evaluation was taken back during an entry; "
            "nothing written, its new worker redoes the entry",
            run_id,
        )
    except Exception as e:
        if isinstance(e, ContextOverflowError):
            logger.warning(
                "Evaluation run %s hit context overflow: ~%s tokens vs %s limit",
                run_id,
                e.estimated_tokens,
                e.context_window,
            )
            error_msg = (
                f"Context overflow: input (~{e.estimated_tokens:,} tokens) "
                f"exceeds model context window "
                f"({e.context_window:,} tokens for {e.model})."
            )
        else:
            logger.exception("Evaluation run %s failed: %s", run_id, format_exception_message(e))
            error_msg = format_exception_message(e)[:500]
        await db.rollback()
        try:
            await db.refresh(run)
            # A CANCELLED run keeps its label: the write below would
            # replace it with ``"failed"`` and an incidental error. A run keeps
            # working after a cooperative cancel, so it can raise here (an
            # execution-level cancel raises ``CancelledError``, which
            # ``except Exception`` does not catch).
            #
            # Episodes are NOT re-closed here, on purpose: reaching this
            # handler means finalising failed (every episode already terminal)
            # or the per-entry commit failed, where one more write is no
            # remedy. The residue is one non-terminal episode on a cancelled
            # run, which the sweep does not reach.
            if run.status == "cancelled":
                logger.info(
                    "Evaluation run %s raised after it was cancelled; keeping "
                    "the cancelled status rather than reporting: %s",
                    run_id,
                    error_msg,
                )
                return
            run.status = "failed"
            run.error = error_msg
            run.completed_at = datetime.now(UTC)
            if run.started_at and run.completed_at:
                run.duration_seconds = round(
                    (run.completed_at - run.started_at).total_seconds(), 2
                )
            await db.commit()
        except Exception:
            logger.warning("Could not update failed run status for %s", run_id)


def _penalized_mean(values: list[float], k: float = 0.5) -> float:
    """Penalized weighted mean: mean - k * std_dev.

    Rewards high average *and* consistency. A single low outlier
    pulls the score down proportionally to k, making the metric
    meaningful even with < 10 data points (unlike P90).
    Result is clamped to [0, 100].
    """
    if not values:
        return 0.0
    n = len(values)
    mean = sum(values) / n
    if n == 1:
        return round(mean, 2)
    variance = sum((v - mean) ** 2 for v in values) / n
    std = math.sqrt(variance)
    return round(max(0.0, min(100.0, mean - k * std)), 2)


def _snapshot_selection(selection: list[str] | None) -> list[str] | None:
    """Copy a scorer selection for storage on a run.

    An alias of the evaluation's list would let an in-place edit to the
    evaluation rewrite the run's recorded provenance.

    ``None`` stays ``None``: it means "not recorded" (judge-on, per
    ``judge_enabled``) while ``[]`` means judge-off.
    """
    return list(selection) if selection is not None else None


async def _compute_run_headlines(
    db: AsyncSession,
    evaluation_id: uuid.UUID,
    batch_run_ids: list[str] | None = None,
    *,
    require_completed: bool = True,
    skip_performance_recompute: bool = False,
    commit: bool = True,
) -> None:
    """Recompute every run-level headline for an evaluation.

    With *batch_run_ids* (after ``execute_evaluation``) only those runs are
    recomputed; with ``None`` (recalculate endpoint) every completed run is.

    *require_completed* (default) loads only ``"completed"`` runs, for
    callers that just flipped the run there. ``rescore_run``, its retry
    headline repair in ``_rescore_run_with_own_session`` and the reconcile
    path in ``evaluation_continuation`` pass ``False``. For ``rescore_run``,
    the flip shares the score commit but is conditional, so a concurrent
    cancel can leave the run ``"cancelled"``, and the headlines
    belong to the scores, committed regardless. A rescore only reaches this
    for a run with scored predictions (the ``no_predictions`` guard ran
    first).

    That flag gates the WRITE, not only the basis: this function
    writes every run it loads, so ``False`` makes a still-``"running"`` run
    writable. The reconcile path wants that for a run it is about to
    terminalise; keep it in mind when reasoning about the terminal guard in
    ``_reconcile_run_status``.

    *skip_performance_recompute* leaves the Overall Performance pass
    to the caller: ``execute_rescore_runs`` runs it once after every run's own
    pass, since each per-run pass would share the same basis. Every caller that
    passes ``require_completed=False`` skips it, so the basis is always the
    evaluation's completed runs. The run-level aggregates below are still
    written.

    What it writes, at run level and independently:

    - ``score``         the LLM judge headline, a penalised mean of gradeable
                        entry scores.
    - ``quality_score`` Overall Quality, composed from the agent family's
                        deterministic quality metrics by
                        ``services/evaluation/aggregation.py``. It reads the
                        ``evaluation_metrics`` table, so a test fixture needs
                        ``evaluations`` + ``evaluation_runs`` +
                        ``evaluation_results`` + ``evaluation_metrics``.

    A headline not measured is ``None``, never ``0.0``: ``0.0`` is a real
    result, the worst one.
    """
    from collections import defaultdict

    # Refused: ``_recompute_performance_headlines`` takes a
    # TRANSACTION-scoped ``pg_advisory_xact_lock``. With ``commit=False`` it
    # would be held until the CALLER commits, serialising every other pass on
    # the evaluation behind someone else's transaction.
    if not commit and not skip_performance_recompute:
        raise ValueError(
            "_compute_run_headlines(commit=False) requires "
            "skip_performance_recompute=True: the performance pass takes a "
            "transaction-scoped advisory lock that would outlive this call"
        )

    # ── Load config from the Evaluation row ────────────────────────
    eval_row = await db.get(Evaluation, evaluation_id)
    k = eval_row.penalty_factor if eval_row else 0.5
    # The judge gate is NOT read from `eval_row`: its `scorer_selection`
    # is mutable, and since `evaluation_results.score` is NOT NULL a judge-off
    # entry stores 0.0, so re-enabling the judge would turn every past judge-off
    # run into a headline of 0. Each run carries its own snapshot (loop below).

    # ── Load runs (scoped to batch or all completed) ─────────────────
    if batch_run_ids:
        batch_uuids = [uuid.UUID(rid) for rid in batch_run_ids]
        # Scoped by ``evaluation_id`` too: everything below scores the
        # runs against THIS evaluation's config and writes the result, so a
        # foreign run id would be rescored with the wrong config. The query,
        # not the callers, guarantees it.
        batch_filters = [
            EvaluationRun.id.in_(batch_uuids),
            EvaluationRun.evaluation_id == evaluation_id,
        ]
        if require_completed:
            batch_filters.append(EvaluationRun.status == "completed")
        run_result = await db.execute(select(EvaluationRun).where(*batch_filters))
    else:
        run_result = await db.execute(
            select(EvaluationRun).where(
                EvaluationRun.evaluation_id == evaluation_id,
                EvaluationRun.status == "completed",
            )
        )
    runs = run_result.scalars().all()
    if not runs:
        return
    run_ids = [r.id for r in runs]

    # ── Load all results for these runs ──────────────────────────────
    res_result = await db.execute(
        select(EvaluationResult).where(EvaluationResult.run_id.in_(run_ids))
    )
    all_results = res_result.scalars().all()
    # NOT an early return on zero results: the loop still writes an
    # (empty) `headline_details`, which tells a run with nothing measurable
    # from one never computed. Without it, a stranded run recovered by the
    # orchestration adapter would be re-selected for recovery on every future
    # interruption.

    # ── Run-level aggregates ──────────────────────────────────────────
    by_run: dict[uuid.UUID, list[EvaluationResult]] = defaultdict(list)
    for r in all_results:
        by_run[r.run_id].append(r)

    for run in runs:
        all_entries = by_run.get(run.id, [])
        # Judge gate from the run's own snapshot. `judge_enabled` maps
        # NULL to True: a run predating the column could not be judge-off.
        judge_on = judge_enabled(getattr(run, "scorer_selection", None))
        # Exclude anomalies and judge failures from the aggregates; a
        # confirmation-seeking entry counts like any other, so the
        # predicate includes "no anomaly" OR "confirmation-seeking", never
        # "not transient_error".
        # Anomalies are counted separately so they stay visible.
        entries = [e for e in all_entries if counts_in_headlines(e)]
        scores = [e.score for e in entries if e.score is not None]
        # Run-level `speed_score` / `overall_score` are not written, nor the
        # retired `f1_score`: recomputing it would move a historical run's F1
        # with today's exclusions and penalty factor.
        # None (N/A) when nothing gradeable remains, never a fabricated 0.
        # None too when the judge never ran: a judge-off
        # entry stores 0.0 in the NOT NULL `score` column.
        run.score = _penalized_mean(scores, k) if (judge_on and scores) else None
        run.anomaly_count = sum(1 for e in all_entries if e.execution_anomaly_kind is not None)
        # Overall Quality: `None` when nothing contributed, never 0.0. Its
        # coverage counts an entry our failure left out, so the headline says
        # it was computed over fewer entries, and why.
        quality_headline = await compute_quality_headline(
            db,
            run.id,
            eval_row,
            [
                e.execution_details.get("metric_results")
                if isinstance(e.execution_details, dict)
                else None
                for e in [
                    *entries,
                    *(
                        e
                        for e in all_entries
                        if e.execution_anomaly_kind == ANOMALY_KIND_PLATFORM_ERROR
                    ),
                ]
            ],
        )
        run.quality_score = quality_headline.value
        # Provenance is persisted with the value, never re-derived downstream.
        # Merged, not replaced, so the performance half survives.
        details = dict(run.headline_details or {})
        details["metric_entry_counts"] = _entry_counts_beside(
            details, "performance_metric_keys", quality_headline.entry_counts
        )
        details["quality_metric_keys"] = quality_headline.contributing_keys
        details["excluded"] = [
            {"key": e.key, "reason": e.reason} for e in quality_headline.excluded
        ]
        details["quality_contributions"] = quality_headline.contributions
        details["quality_coverage"] = (
            quality_headline.coverage.as_detail() if quality_headline.coverage else None
        )
        run.headline_details = details

    # With skip_performance_recompute ``execute_rescore_runs`` runs
    # this pass once for the batch.
    if not skip_performance_recompute:
        await _recompute_performance_headlines(db, evaluation_id, eval_row)

    # commit=False: ``_reconcile_run_status`` runs inside
    # ``fail_orphaned_episodes``' transaction, which folds the episode repair
    # and the run failure together so a crash cannot apply one without the
    # other. Flush, not commit: the writes are then IN the caller's
    # transaction (visible to its reads, rolled back with it), not merely
    # pending ORM changes.
    if commit:
        await db.commit()
    else:
        await db.flush()


def _entry_counts_beside(details: dict, other_keys: str, counts: dict[str, int]) -> dict[str, int]:
    """``metric_entry_counts`` after one headline pass: *counts*, this half's
    contributors, beside the other half's own, read from its contributing keys
    (*other_keys*). A metric that stopped contributing loses its count:
    the counts describe the headlines as they stand, not every past pass. A half
    with no keys keeps no counts: each pass writes its keys with its counts, so
    it has none to keep."""
    kept = set(details.get(other_keys) or [])
    previous = details.get("metric_entry_counts") or {}
    return {**{key: n for key, n in previous.items() if key in kept}, **counts}


async def _recompute_performance_headlines(
    db: AsyncSession,
    evaluation_id: uuid.UUID,
    eval_row: Evaluation | None,
) -> None:
    """Rescore Overall Performance for **every** completed run in the evaluation.

    Ignores the caller's batch scope: performance is comparative, so
    the run set changes every sibling's value. Scoring a batch against itself
    gives two runs of one evaluation incomparable numbers, and a lone run the
    top of the scale for having no one to lose to.

    A transaction-scoped advisory lock keyed on the evaluation serialises the
    pass: two batches completing at once would each read a different run set,
    and the last writer would leave runs scored against a stale basis.

    The lock makes this POSTGRES-ONLY: ``pg_advisory_xact_lock`` raises on any
    other dialect. Unguarded on purpose: every evaluation test runs on a real
    PostgreSQL database.
    """
    # `hashtext` keeps the key in int8, stable across sessions. Taken in the
    # caller's transaction, so it releases on commit or rollback.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"evaluation_headlines:{evaluation_id}"},
    )

    # A placebo comparison's runs stay out: its trials of three variants
    # would move every other run's performance, and they rank no configuration.
    basis = (
        (
            await db.execute(
                select(EvaluationRun).where(
                    EvaluationRun.evaluation_id == evaluation_id,
                    EvaluationRun.status == "completed",
                    EvaluationRun.placebo_comparison_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not basis:
        return

    headlines = await compute_performance_headlines(
        db, eval_row, [r.id for r in basis]
    )
    for run in basis:
        headline = headlines.get(run.id)
        # `None` when the metric set could not be compared — N/A, never 0.0.
        run.performance_score = headline.value if headline else None
        details = dict(run.headline_details or {})
        details["performance_metric_keys"] = (
            headline.contributing_keys if headline else []
        )
        details["metric_entry_counts"] = _entry_counts_beside(
            details, "quality_metric_keys", headline.entry_counts if headline else {}
        )
        details["performance_excluded"] = [
            {"key": e.key, "reason": e.reason, "run_ids": [str(r) for r in e.run_ids]}
            for e in (headline.excluded if headline else [])
        ]
        # Rewritten wholesale on every recompute: contributions mean something
        # only relative to the run set in effect.
        details["performance_contributions"] = (
            headline.contributions if headline else {}
        )
        run.headline_details = details


async def _resolve_dataset_output_schema(
    db: AsyncSession,
    entry: object,
    memo: dict,
) -> dict | None:
    """The dataset-level answer shape behind one result's entry.

    A snapshot entry reads the schema frozen with it; a live entry reads the
    live dataset. Reading the live dataset for a pinned result would silently
    re-describe a completed run under a shape it was never scored against.
    """
    from app.models.dataset_version import DatasetVersion

    version_id = getattr(entry, "version_id", None)
    if version_id is not None:
        if version_id not in memo:
            version = await db.get(DatasetVersion, version_id)
            memo[version_id] = getattr(version, "output_schema", None)
        return memo[version_id]

    dataset_id = getattr(entry, "dataset_id", None)
    if dataset_id is not None:
        if dataset_id not in memo:
            dataset = await db.get(Dataset, dataset_id)
            memo[dataset_id] = getattr(dataset, "output_schema", None)
        return memo[dataset_id]

    return None


def _left_out_quality(family: str, exc: Exception) -> dict[str, dict]:
    """The family's metrics for an entry whose expected answer or scoring settings are
    unusable: left out, with the reason."""
    return to_persistable(
        unscorable_outcomes(family, "configuration_error", str(exc))
    )


def _failed_quality(family: str, exc: Exception) -> dict[str, dict]:
    """The family's metrics for an entry whose scoring raised unexpectedly: left out, so
    the answer and the judge verdict stay. The error's own text, which can hold
    internals, stays in the server log."""
    return to_persistable(
        unscorable_outcomes(
            family, "scoring_error", f"Quality scoring raised {type(exc).__name__}; see the server log."
        )
    )


def _log_scoring_errors(run: EvaluationRun, count: int) -> None:
    """One error per run whose quality scoring raised: a scorer broken on every
    entry must not pass for a run with N/A quality."""
    if count:
        logger.error(
            "Quality scoring raised on %d entries of run %s (evaluation %s)",
            count,
            run.id,
            run.evaluation_id,
        )


def _log_platform_errors(run: EvaluationRun, causes: Mapping[str, int]) -> None:
    """One error per run whose entries the platform could not run, with their
    causes: they leave every headline, so a platform bug removing entries from many runs
    must show in the server log, not only on the run page."""
    if causes:
        logger.error(
            "The platform could not run %d entries of run %s (evaluation %s): %s",
            sum(causes.values()),
            run.id,
            run.evaluation_id,
            ", ".join(f"{cause}={count}" for cause, count in sorted(causes.items())),
        )


#: The ``execution_details`` key of an entry whose agent run raised: the failure
#: cause its treatment was decided from. A rescore keeps that treatment.
AGENT_RUN_FAILURE_KEY = "agent_run_failure"

#: The judge's reasoning on an entry left out because its run raised.
_RAISED_REASONING = {
    ANOMALY_KIND_TRANSIENT_ERROR: EXECUTION_ERROR_REASONING,
    ANOMALY_KIND_PLATFORM_ERROR: PLATFORM_ERROR_REASONING,
}


def _raised_entry_details(
    kind: str | None,
    cause: FailureClassification,
    message: str,
    *,
    family: str | None,
    quality_config: QualityConfig | None,
    quality_config_error: Exception | None,
    reference: object,
    output_schema: Mapping | None,
    performance_config: PerformanceConfig,
    price_rows: list[dict[str, object]],
) -> dict:
    """The ``execution_details`` of an entry whose agent run raised.

    One rule for the three headlines, by *kind* (``classify_raised_entry``):

    - the agent's (``None``): the judge's 0 counts; every quality metric scores 0,
      counted as the expected answer would; the call that raised is a failed call in the
      completion rate, and nothing else is measured;
    - ours (``ANOMALY_KIND_PLATFORM_ERROR``): an anomaly, out of every aggregate, with the
      quality metrics left out under ``platform_error`` so Overall Quality says why;
    - the provider's (``ANOMALY_KIND_TRANSIENT_ERROR``): an anomaly, out of every
      aggregate, like a transient error surfaced as the output.
    """
    metric_results: dict[str, dict] = {}
    if kind is None:
        metric_results = _raised_entry_quality(
            family, quality_config, quality_config_error, reference, output_schema, message
        )
        metric_results.update(
            to_persistable(
                run_performance(
                    execution_details={
                        "traces": [
                            {"type": "model", "outcome": "error", "error_type": cause.cause.value}
                        ]
                    },
                    token_usage=None,
                    retry_count=None,
                    price_rows=price_rows,
                    config=performance_config,
                )
            )
        )
    elif kind == ANOMALY_KIND_PLATFORM_ERROR and family:
        metric_results = to_persistable(unscorable_outcomes(family, "platform_error", message))
    return {"metric_results": metric_results, AGENT_RUN_FAILURE_KEY: cause.as_trace_metadata()}


def _raised_entry_quality(
    family: str | None,
    quality_config: QualityConfig | None,
    quality_config_error: Exception | None,
    reference: object,
    output_schema: Mapping | None,
    message: str,
) -> dict[str, dict]:
    """The quality metrics of an entry the agent failed by raising: 0, counted as
    the expected answer would; left out when the configuration cannot score it."""
    if family and quality_config is not None:
        try:
            return failed_answer_quality(
                family=family,
                reference=reference,
                output_schema=output_schema,
                config=quality_config,
                reason="agent_run_failed",
                message=message,
            )
        except QualityConfigError as exc:
            return _left_out_quality(family, exc)
        except Exception as exc:
            # As on an answer: our scoring raising leaves quality out.
            logger.warning("Quality scoring raised on a failed entry: %s", exc, exc_info=True)
            return _failed_quality(family, exc)
    if family and quality_config_error is not None:
        return _left_out_quality(family, quality_config_error)
    return {}


@dataclass
class StoredScoring:
    """What scoring a stored answer needs from its run, built once per rescore or resumed
    callback."""

    run: EvaluationRun
    evaluation: Evaluation
    judge_selected: bool
    quality_config: Any | None
    quality_config_error: Exception | None
    extractor: Any
    dataset_schemas: dict[uuid.UUID, dict | None] = field(default_factory=dict)
    extraction_failure_reasons: dict[str, int] = field(default_factory=dict)
    scoring_errors: int = 0


async def score_stored_result(
    db: AsyncSession, scoring: StoredScoring, r: EvaluationResult, *, is_reused: bool
) -> None:
    """Score one stored answer with the selected scorers: its anomaly re-classified, the
    judge, the package's quality and its metric rows. A rescore (*is_reused*: the agent
    did not run again) and a resumed callback (the agent's first turn ran for it) share
    it, so a stored answer scores as a fresh one. No commit."""
    run, evaluation = scoring.run, scoring.evaluation
    family = getattr(evaluation, "family", None)
    # Re-classify from the stored output, so rows that predate the
    # detector and transient provider failures surfaced as the output
    # are excluded like fresh ones, not re-judged to a real 0.
    # skip_llm is never flagged. Flag only: the agent is never re-run.
    r_subtype = r.execution_anomaly_kind
    r_anomaly_reason: str | None = None
    # An entry whose agent run raised has no answer: its treatment stands, and
    # only the quality of one the agent failed is recomputed, below.
    raised = (r.execution_details or {}).get(AGENT_RUN_FAILURE_KEY)
    if raised is None and r_subtype is None:
        r_exec_mode = (r.execution_details or {}).get(
            "execution_mode", "full_loop"
        )
        r_subtype, r_anomaly_reason = classify_execution_anomaly(
            r.agent_output, r_exec_mode
        )
        if r_subtype is not None:
            r.execution_anomaly_kind = r_subtype
            if r_subtype == ANOMALY_KIND_TRANSIENT_ERROR:
                r.score = 0
                r.sub_scores = {k: 0 for k in SUB_SCORE_WEIGHTS}
                r.evaluator_reasoning = r_anomaly_reason
                # An anomaly skips the judge, so it is never a judge
                # failure: clear a stale flag, as the fresh path never sets it.
                r.is_judge_failure = False
    # Transient-error entries stay zeroed, unjudged and excluded. Confirmation-seeking
    # and non-anomaly rows go on to the recompute below. A resumed answer is new: like a
    # fresh one, it is left unjudged but still gets its quality and metric rows.
    unjudged_anomaly = r_subtype is not None and r_subtype != ANOMALY_KIND_CONFIRMATION_SEEKING
    if unjudged_anomaly and is_reused:
        return
    # The entry the result was scored against: snapshot first, else live.
    resolved = await scored_entry(db, r)
    entry = resolved[0]

    expected_answer = getattr(entry, "expected_answer", "") or ""
    entry_schema = getattr(entry, "output_schema", None)
    dataset_schema = await _resolve_dataset_output_schema(db, entry, scoring.dataset_schemas)

    judge_duration_ms: int | None = None
    # An entry deleted since keeps its verdict: no question to judge against, as
    # the replay's 404.
    if scoring.judge_selected and raised is None and entry is not None and not unjudged_anomaly:
        # What the fresh pass gave the judge, the dataset's instructions included.
        judge_inputs = await stored_judge_inputs(db, evaluation, r, resolved)
        judge_t0 = time.perf_counter()
        score_data = await score_result(
            db,
            r.agent_output,
            # Set whenever *entry* is.
            cast(JudgeEntryLike, judge_inputs.entry),
            evaluation.evaluator_model,
            evaluator_prompt=evaluation.evaluator_prompt,
            evaluation_instructions=judge_inputs.instructions,
            available_tools=judge_inputs.available_tools,
            tool_calls=judge_inputs.tool_calls,
            sub_score_weights=evaluation.sub_score_weights,
            judge_max_tokens=evaluation.judge_max_tokens,
            # A re-score judges on the owner's providers, as the run did.
            user_id=evaluation_owner_id(evaluation),
        )
        judge_duration_ms = _ms(judge_t0, time.perf_counter())
        # Its latency replaces the stored one, here, not in the quality
        # block, which an evaluation without a quality config never enters.
        r.execution_details = with_judge_pass(
            r.execution_details, score_data, judge_duration_ms, evaluation.evaluator_model
        )
        if not is_reused:
            # Not a reuse: the agent ran for this answer, so its total is the fresh
            # one's, agent plus judge.
            r.execution_details["total_duration_ms"] = (
                r.execution_details.get("agent_duration_ms") or 0
            ) + judge_duration_ms
        r.score = score_data.get("score", 0)
        r.sub_scores = score_data.get("sub_scores", {})
        r.evaluator_reasoning = score_data.get("evaluator_reasoning")
        r.is_judge_failure = bool(score_data.get("is_judge_failure", False))
    elif not is_reused:
        # A new answer the judge skips gets the fresh pass's ``judge_skipped`` step,
        # so its timeline never reads as unjudged by accident.
        skip_reason = (
            f"Judge skipped: {r_anomaly_reason}"
            if unjudged_anomaly and r_anomaly_reason
            else None
            if scoring.judge_selected
            else "Judge skipped: judge scorer not selected for this evaluation"
        )
        if skip_reason is not None:
            details = dict(r.execution_details or {})
            details["traces"] = [
                *_agent_timeline(details.get("traces")),
                _build_judge_trace({}, 0, evaluation.evaluator_model, judge_ran=False, skip_reason=skip_reason),
            ]
            r.execution_details = details

    # The retired Token-F1 is neither recomputed nor cleared here: a
    # rescore keeps the answer it was computed on, and a run scored before the metric
    # package keeps the F1 its pages show. Deterministic quality is recomputed from the
    # package: identical inputs and family give a byte-identical ``metric_results``.

    # Same precedence as the fresh path, or an unchanged prediction would
    # score differently.
    effective_schema = entry_schema or dataset_schema
    if raised is not None:
        quality_results, extracted = (
            _raised_entry_quality(
                family,
                scoring.quality_config,
                scoring.quality_config_error,
                expected_answer,
                effective_schema,
                r.agent_output,
            ),
            None,
        )
    elif scoring.quality_config is not None:
        try:
            # Same seam as the fresh path, reading only material
            # persisted with this result.
            scored = quality_with_extraction(
                extractor=scoring.extractor,
                family=family,
                reference=expected_answer,
                agent_output=r.agent_output,
                execution_details=r.execution_details,
                output_schema=effective_schema,
                config=scoring.quality_config,
                agent_id=str(run.flow_id or run.agent_id),
            )
            quality_results, extracted = scored.metric_results, scored.extracted
        except QualityConfigError as exc:
            logger.warning(
                "Quality rescore skipped (run %s, result %s): %s",
                run.id,
                r.id,
                exc,
            )
            # Replaced, never kept: the previous pass's values would pass for this
            # one's.
            quality_results, extracted = _left_out_quality(family, exc), None
        except Exception as exc:
            logger.exception("Quality rescore failed (run %s, result %s)", run.id, r.id)
            # Never the whole rescore for one answer.
            quality_results, extracted = _failed_quality(family, exc), None
            scoring.scoring_errors += 1
    elif scoring.quality_config_error is not None:
        quality_results, extracted = _left_out_quality(family, scoring.quality_config_error), None
    else:
        quality_results = None
    if quality_results is not None:
        new_details = dict(r.execution_details or {})
        # Rewritten, and CLEARED when this rescore produced none: a value
        # from the previous extractor would pass for the source of the
        # fresh score.
        record_extracted_prediction(new_details, extracted)
        # The agent is not re-run, so the stored ``performance.*``
        # metrics still describe this run: only quality keys
        # are replaced.
        preserved_perf = {
            k: v
            for k, v in (new_details.get("metric_results") or {}).items()
            if str(k).startswith("performance.")
        }
        tally_extraction_failure(quality_results, scoring.extraction_failure_reasons)
        new_details["metric_results"] = {**quality_results, **preserved_perf}
        r.execution_details = new_details

    # Outside the quality block and its ``except``: the judge timing changed
    # whatever happened to quality, and ``judge_seconds`` is read from these
    # rows, not from ``execution_details``.
    #
    # ``is_reused`` for a rescore: the agent was not re-run, so Run Duration is the
    # judge time alone. A resumed answer's agent ran for it: agent plus judge.
    #
    # A deselected judge writes no duration rather than the stored one,
    # which would mint timings from an earlier pass. Existing rows are left
    # alone: the score on display still comes from that earlier pass, and
    # clearing them would leave it without provenance.
    metrics_details = r.execution_details or {}
    if judge_duration_ms is None:
        metrics_details = {
            k: v for k, v in metrics_details.items() if k != "judge_duration_ms"
        }

    from app.services.evaluation import metrics as run_metrics

    await run_metrics.log_result_metrics(
        db,
        result_id=r.id,
        run_id=run.id,
        execution_details=metrics_details,
        is_reused=is_reused,
        replace=True,
    )


async def stored_scoring_for_run(
    db: AsyncSession, run: EvaluationRun, *, judge_selected: bool
) -> StoredScoring:
    """What scoring a run's stored answers needs, built as the fresh pass built it: the
    quality config from every entry the run scores, so CLASSIFICATION derives the same
    vocabulary, and the agent's extractor. A rescore and a resumed callback both start
    from it."""
    evaluation = await db.get(Evaluation, run.evaluation_id)
    if evaluation is None:
        raise ValueError("evaluation_not_found")
    # Every entry only for a family: CLASSIFICATION derives its vocabulary from all of
    # them. Without one there is no quality to configure, and no reason to read them.
    quality_config, quality_config_error = None, None
    if getattr(evaluation, "family", None):
        _entries, entry_records = await _run_entry_records(db, run, evaluation)
        quality_config, quality_config_error = _run_quality_config(run, evaluation, entry_records)
    return StoredScoring(
        run=run,
        evaluation=evaluation,
        judge_selected=judge_selected,
        quality_config=quality_config,
        quality_config_error=quality_config_error,
        # A flow subject (#8257) takes no extractor: ``agent_id == None`` would
        # match every flow link.
        extractor=(
            await load_extractor_for_agent(db, run.evaluation_id, run.agent_id)
            if run.agent_id is not None
            else None
        ),
    )


async def rescore_run(
    db: AsyncSession,
    run_id: str,
    *,
    previous_status: str,
    execution_id: uuid.UUID | None = None,
) -> int:
    """Re-run the selected scorers on a run's stored predictions.

    The evaluated agent is not re-run — stored ``agent_output`` predictions are
    scored as-is, though a judge scorer still calls its evaluator model.
    Returns the number of results rescored. Raises ``ValueError`` with a
    machine-readable code when the run/evaluation is missing or has no stored
    predictions to rescore.

    On success it ALSO gives the run back *previous_status*, the status it had
    before the rescore, in the same transaction as the scores: the status is a
    retry's only idempotence key, so it must never read ``"running"`` once the
    new scores are durable, or an interrupted retry re-judges, and pays for, a
    finished run. A failure leaves the status to the caller, since nothing
    committed.

    The run must read ``"running"``, as the rescore endpoints leave it, or this
    raises ``ValueError("run_not_running")``: nothing else starts a rescore,
    and one started on a finished run would stop at its first result as if
    cancelled. *execution_id*, the rescore's own execution, makes every check and
    write ownership-scoped: a rescore superseded by a later one stops rather than
    write over it.

    The Overall Performance pass is left to ``execute_rescore_runs``, which runs
    it once for the batch (see ``_compute_run_headlines``).
    """
    run = await db.get(EvaluationRun, uuid.UUID(run_id))
    if run is None:
        raise ValueError("run_not_found")
    if run.status != "running":
        raise ValueError("run_not_running")
    evaluation = await db.get(Evaluation, run.evaluation_id)
    if evaluation is None:
        raise ValueError("evaluation_not_found")

    # Judge gating on the evaluation's RAW selection so a judge-off evaluation
    # is honoured.
    effective_selection = getattr(evaluation, "scorer_selection", None)
    judge_selected = judge_enabled(effective_selection)

    res_result = await db.execute(
        select(EvaluationResult).where(EvaluationResult.run_id == run.id)
    )
    results = list(res_result.scalars().all())
    if not results:
        raise ValueError("no_predictions")

    # Judge provenance: a rescore changes what produced the run's
    # numbers, so a rescore that ENABLES the judge must not leave a snapshot
    # saying "off" (N/A for scores that exist). Stamped after the guards (a
    # rescore that raises changes no score) and from the same resolved value
    # as the gate, so the record cannot drift from what was applied.
    run.scorer_selection = _snapshot_selection(effective_selection)

    # Built as the fresh pass and a resumed callback build it: the quality
    # config from every entry the run scores, not only those with a result, so a
    # partial run (cancelled, failed, timed out) derives the same CLASSIFICATION
    # vocabulary when rescored.
    scoring = await stored_scoring_for_run(db, run, judge_selected=judge_selected)

    for r in results:
        # Cancellation stops a rescore too, checked before each
        # billable judge call. ``0``, not a partial count: the helper's
        # rollback discarded every result judged so far.
        if await _rescore_was_cancelled(db, run.id, str(run.id), execution_id):
            return 0
        await score_stored_result(db, scoring, r, is_reused=True)

    # The suite computed these scores (``quality_with_extraction`` above), so the
    # run records its version, as a fresh run does.
    run.metric_suite_version = metric_suite_version()

    # Terminal in the SAME transaction as the scores: the status is
    # the batch's only idempotence key, so scores are durable IFF the run is
    # terminal. Otherwise an interruption (timeout, shutdown, crash, lost
    # lease) strands a finished rescore as ``"running"`` and the retry
    # re-judges it, re-charging the judge. An interruption before this commit
    # leaves nothing written, and the retry rightly redoes it.
    #
    # The run gets back the status it had: a rescore re-judges stored
    # answers, it neither finishes a cancelled run nor repairs a failed one. Its
    # error stands with it; only a past rescore's failure is cleared.
    #
    # A conditional UPDATE, not ``run.status = ...``: Postgres checks
    # ``status == "running"`` at UPDATE time, so a concurrent cancel, which hands
    # the run back itself, still wins. On 0
    # rows the scores still commit.
    await db.execute(
        update(EvaluationRun)
        .where(
            EvaluationRun.id == run.id,
            EvaluationRun.status == "running",
            *_rescore_ownership(execution_id),
        )
        .values(**_rescore_outcome(previous_status, None))
    )
    await db.commit()

    # The single-run scope applies to the judge and quality headlines, which
    # are absolute. Overall Performance rescores the whole evaluation:
    # a run normalised against itself is both fastest and slowest.
    #
    # require_completed=False: a concurrent cancel may have won the UPDATE
    # above, and the headlines belong to the scores, committed either way.
    #
    # Best-effort, like the fresh-run path: the scores are committed, so a
    # headline failure must not make a successful rescore look failed. A
    # manual "Recalculate" can repair the headlines.
    try:
        await _compute_run_headlines(
            db,
            run.evaluation_id,
            batch_run_ids=[str(run.id)],
            require_completed=False,
            skip_performance_recompute=True,
        )
        await db.commit()
    except Exception as headline_err:  # pragma: no cover - defensive
        await db.rollback()
        logger.warning(
            "Headline scoring failed after rescoring run %s: %s",
            run.id,
            headline_err,
        )
    log_extraction_failures(
        evaluation_id=run.evaluation_id,
        run_id=run.id,
        agent_id=run.agent_id,
        reason_counts=scoring.extraction_failure_reasons,
    )
    _log_scoring_errors(run, scoring.scoring_errors)
    return len(results)


def _rescore_outcome(previous_status: str, error: str | None) -> dict[str, object]:
    """The run's columns once its rescore ended, successfully or with *error*.

    It gets back its previous status, the failure kept in ``rescore_error``.
    """
    return {"status": previous_status, "rescore_error": None if error is None else error[:500]}


async def _rescore_run_with_own_session(
    run_id: str,
    *,
    previous_status: str,
    execution_id: uuid.UUID | None = None,
) -> None:
    """Rescore one run with its own session, handing the run back its status.

    The run gets back *previous_status*, the status it had before the rescore,
    whatever the outcome: a failure is recorded in ``rescore_error``,
    never as the run's own. *execution_id* scopes every write to the rescore's
    own run (``_rescore_ownership``). Nothing is reported back: the batch's
    performance pass is gated on ``_batch_has_a_completed_run``, which asks the
    database.

    Never raises, for ``_gather_runs_bounded``: every outcome (success, a ``ValueError`` code, an unexpected
    exception such as a failing judge call) becomes the run's terminal
    status, so one bad run never takes the batch down. That covers the whole
    body: a failing finalize UPDATE or commit is logged and gets one
    recovery attempt on a fresh session.

    A run no longer ``"running"`` when its turn comes is SKIPPED:
    the batch's durable idempotence key. It covers an engine retry after a
    lost lease (a finished run must not be re-judged and re-charged) and a
    cancel landing while the run waited in the pool (the judge is never
    called for it).
    """
    from app.core.database import async_session

    try:
        async with async_session() as session:
            run = await session.get(EvaluationRun, uuid.UUID(run_id))
            if run is None:
                # Deleted since validation: nothing to rescore.
                return
            if run.status != "running" or (
                execution_id is not None and run.owner_execution_id != execution_id
            ):
                logger.info(
                    "Skipping rescore of run %s: status is %s, not 'running' under "
                    "this rescore (already finished, cancelled while queued, or "
                    "taken over by a later rescore).",
                    run_id,
                    run.status,
                )
                if run.status == "completed":
                    # Repair headlines a previous attempt may have left stale.
                    # The headline pass runs after ``rescore_run``'s
                    # commit, so a stop there leaves the run terminal with NEW
                    # results and OLD headlines, and the retry rightly skips it.
                    #
                    # Not folded into the score transaction: a headline glitch
                    # would then roll back the scores and report a successful
                    # rescore ``failed``. Repairing on the retry costs
                    # nothing on a first attempt and needs no durable marker.
                    #
                    # Idempotent and judge-free: a recompute from stored scores,
                    # the same trade as the shared performance pass.
                    try:
                        await _compute_run_headlines(
                            session,
                            run.evaluation_id,
                            batch_run_ids=[str(run.id)],
                            require_completed=False,
                            skip_performance_recompute=True,
                        )
                        await session.commit()
                    except Exception as headline_err:
                        # Best-effort, as in ``rescore_run``. ``CancelledError``
                        # is a BaseException and still escapes.
                        await session.rollback()
                        logger.warning(
                            "Headline repair failed for already-completed run "
                            "%s: %s",
                            run_id,
                            headline_err,
                        )
                return
            try:
                await rescore_run(
                    session,
                    run_id,
                    previous_status=previous_status,
                    execution_id=execution_id,
                )
            except ValueError as exc:
                await session.rollback()
                error = str(exc)
            except Exception as exc:  # isolate one run's failure from the batch
                # rescore_run commits once, at the end: roll back the partial
                # changes before flipping to "failed", so a rescore that raises
                # changes no score.
                await session.rollback()
                logger.error(
                    "Rescore failed for run %s: %s",
                    run_id,
                    format_exception_message(exc),
                    exc_info=True,
                )
                error = format_exception_message(exc)
            else:
                error = None

            # Finalize. On success ``rescore_run`` already handed the run back
            # with the scores, so the guard matches no row; the error
            # paths rolled back and still need theirs written: the
            # previous status, the failure kept apart in ``rescore_error``.
            #
            # One atomic conditional UPDATE, not get/assign/commit:
            # Postgres checks ``status == "running"`` against the row at UPDATE
            # time (waiting on the row lock of a cancel mid-commit), so a
            # concurrent cancel wins however it interleaves. 0 rows means
            # handed back, cancelled or deleted: nothing to flip.
            await session.execute(
                update(EvaluationRun)
                .where(
                    EvaluationRun.id == uuid.UUID(run_id),
                    EvaluationRun.status == "running",
                    *_rescore_ownership(execution_id),
                )
                .values(**_rescore_outcome(previous_status, error))
            )
            await session.commit()
    except Exception as exc:  # pragma: no cover - defensive
        # A finalize failure (the UPDATE or commit, e.g. a DB blip) would
        # otherwise be absorbed by ``_gather_runs_bounded`` and leave the run
        # "running" with nothing logged. Log it, then try once on a
        # FRESH session (the one above may be what broke) to mark it "failed".
        # There is no sweep behind this and the adapter's
        # ``_fail_rescore_runs`` touches only rows still ``"running"``:
        # if the recovery fails too, nothing repairs the run.
        logger.error(
            "Failed to finalize rescore status for run %s: %s",
            run_id,
            format_exception_message(exc),
            exc_info=True,
        )
        with contextlib.suppress(Exception):
            async with async_session() as recovery_session:
                # Conditional UPDATE as above: a get/assign/commit
                # would overwrite a concurrent cancel with "failed".
                await recovery_session.execute(
                    update(EvaluationRun)
                    .where(
                        EvaluationRun.id == uuid.UUID(run_id),
                        EvaluationRun.status == "running",
                        *_rescore_ownership(execution_id),
                    )
                    .values(
                        **_rescore_outcome(
                            previous_status,
                            f"Failed to finalize rescore: {format_exception_message(exc)}",
                        )
                    )
                )
                await recovery_session.commit()


async def execute_rescore_runs(
    run_ids: list[str],
    cancel_event: "asyncio.Event | None" = None,
    *,
    previous_statuses: Mapping[str, str],
    execution_id: uuid.UUID | None = None,
) -> None:
    """Rescore a batch of runs in the background (bulk/single Re-score).

    Shaped like ``execute_evaluation`` (one session per run, bounded
    concurrency) but runs ``rescore_run`` via
    ``_rescore_run_with_own_session``: the agent is never re-run. Every
    named run is already ``"running"`` (the caller flips it before
    enqueuing); a run that is not is skipped, which makes an engine retry
    of the batch idempotent. *previous_statuses* is each run's status before
    that flip, which it gets back, and *execution_id* the batch's own execution,
    which owns them.
    """
    from app.core.config import settings

    if not run_ids:
        return

    cap = settings.evaluation_max_concurrent_runs

    async def _runner(rid: str) -> None:
        # Its runs are rescored concurrently; the shared pass below recomputes
        # Overall Performance once for the batch.
        await _rescore_run_with_own_session(
            rid,
            previous_status=previous_statuses[rid],
            execution_id=execution_id,
        )

    await _gather_runs_bounded(run_ids, cap, _runner, cancel_event=cancel_event)

    # Same boundary guard as ``execute_evaluation``. The pass below
    # rewrites whole-evaluation performance under an advisory lock, so an
    # abandoned worker would silently overwrite the replacement's aggregate.
    _stop_if_no_longer_ours(
        cancel_event, f"execute_rescore_runs: {len(run_ids)} run(s)"
    )

    # One Overall Performance pass for the batch, after every run's
    # own headlines landed, and only when some run is ``"completed"``:
    # an all-failed or all-cancelled batch has nothing to normalise and must
    # not cost a whole-evaluation write under the advisory lock.
    #
    # Checked against the database, not an in-memory "this call did work"
    # flag: a retry after a lost lease finds every run completed by the
    # PRIOR attempt and skips them all, while that attempt's own pass may
    # never have committed. The pass is a pure recompute, so repeating it on
    # a retry is harmless.
    if await _batch_has_a_completed_run(run_ids):
        # Raced like ``execute_evaluation``'s headline pass: under
        # the advisory lock an abandoned worker would not collide visibly
        # with the replacement, the later one would silently win.
        await _run_unless_stopped(
            _recompute_shared_performance_basis(run_ids),
            cancel_event,
            f"execute_rescore_runs: {len(run_ids)} run(s)",
        )


async def _batch_has_a_completed_run(run_ids: list[str]) -> bool:
    """True if any run named in *run_ids* currently reads ``"completed"``.

    A cheap existence check gating ``_recompute_shared_performance_basis``
    on the database (see its call site in ``execute_rescore_runs``).
    """
    from app.core.database import async_session

    async with async_session() as session:
        return bool(
            (
                await session.execute(
                    select(EvaluationRun.id)
                    .where(
                        EvaluationRun.id.in_(
                            [uuid.UUID(rid) for rid in run_ids]
                        ),
                        EvaluationRun.status == "completed",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            is not None
        )


class AbandonedWorkRepairError(Exception):
    """The pre-dispatch reclaim/repair failed.

    Distinct from a generic batch failure so the adapter marks it RETRYABLE.
    It is raised before anything is dispatched and before the reclaim commits
    (reclaim and episode repair share one transaction), so a retry redoes
    both atomically.

    The generic branch is non-retryable and calls ``_fail_active_runs``,
    which terminalises runs but NOT episodes: a transient repair fault would
    leave a failed run with a ``"running"`` episode nobody moves, and a
    non-retryable execution never reaches ``on_dead_letter``.
    """


class SharedPerformanceRecomputeError(Exception):
    """The batch's coalesced Overall Performance pass failed.

    Distinct from a generic batch failure: when it can raise, every run in
    the batch is terminal, so retrying the ``rescore_runs`` payload re-judges
    nothing and only retries this recompute. ``EvaluationAdapter.execute``
    classifies it as retryable; any other batch exception stays
    non-retryable, to avoid double-scoring a run left ``"running"``.
    """


async def _recompute_shared_performance_basis(run_ids: list[str]) -> None:
    """Coalesced Overall Performance pass for one rescore batch.

    Called only when some run of the batch is ``"completed"`` (see the gate
    in ``execute_rescore_runs``). Every named run is terminal by now, so the
    basis is the evaluation's ``"completed"`` runs, which holds the batch's
    completed ones and never its ``failed`` or ``cancelled`` ones.

    A failure is NOT swallowed: this is the batch's only performance
    write, and swallowing it would report a ``completed`` batch whose
    headlines never landed. It is logged here with the ``evaluation_id`` and
    re-raised as ``SharedPerformanceRecomputeError``, which the adapter
    retries. Safe because every run is already terminal, so the adapter's
    ``_fail_rescore_runs`` (``"running"`` rows only) touches nothing.

    A rescore batch is scoped to one evaluation (``stage_rescore_execution``),
    so one lookup resolves it.
    """
    from app.core.database import async_session

    async with async_session() as session:
        evaluation_id = (
            await session.execute(
                select(EvaluationRun.evaluation_id).where(
                    EvaluationRun.id.in_([uuid.UUID(rid) for rid in run_ids])
                ).limit(1)
            )
        ).scalar_one_or_none()
        if evaluation_id is None:
            # Every named run was deleted since validation.
            return
        eval_row = await session.get(Evaluation, evaluation_id)
        if eval_row is None:
            # The evaluation was deleted during a long batch: without
            # this, `eval_row=None` would reach the recompute and the `except`
            # below would log "nothing to do" as an error.
            return
        try:
            await _recompute_performance_headlines(session, evaluation_id, eval_row)
            await session.commit()
        except Exception as exc:
            # Logged, then re-raised as retryable (see the docstring).
            await session.rollback()
            logger.error(
                "Shared performance-headline recompute failed for evaluation "
                "%s: %s",
                evaluation_id,
                format_exception_message(exc),
                exc_info=True,
            )
            raise SharedPerformanceRecomputeError(str(exc)) from exc


async def is_rescore_pending_for_run(db: AsyncSession, run_id: str) -> bool:
    """True while an in-flight ``rescore_runs`` batch still names *run_id*.

    A run turns terminal BEFORE the batch's shared performance pass commits
    (that pass's basis reads only ``"completed"`` runs), and the run-detail
    poll (``useEvaluationRun``) stops when ``status`` leaves
    ``"running"``/``"pending"``, so it could show the pre-rescore
    ``performance_score`` for the rest of that pass.

    The ``rescore_runs`` :class:`~app.models.orchestration.OrchestrationExecution`
    spans both halves: the adapter awaits ``execute_rescore_runs`` (every
    finalize AND the shared pass) before completing, so the execution stays
    non-terminal until the pass has landed. Called only for a run already
    terminal (see ``get_run``); the status poll covers the others.
    """
    from sqlalchemy import cast as sql_cast
    from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB

    from app.models.orchestration import (
        NON_TERMINAL_EXECUTION_STATES,
        OrchestrationExecution,
    )

    result = await db.execute(
        select(OrchestrationExecution.id)
        .where(
            OrchestrationExecution.adapter == "evaluation",
            # Matches ``ix_orchestration_exec_rescore_run_ids``'s
            # ``postgresql_where``: the planner cannot prove an
            # equivalent ``state.not_in(...)`` satisfies a partial index on
            # ``state IN (...)``, and would scan a table that never shrinks.
            OrchestrationExecution.state.in_(NON_TERMINAL_EXECUTION_STATES),
            OrchestrationExecution.payload["kind"].astext == "rescore_runs",
            # The JSONB bind carries the list itself: a pre-serialized
            # ``json.dumps([...])`` would be serialized again into a JSON
            # string, and ``@>`` would never match.
            #
            # ``.op("->")("run_ids")``, NOT the ``["run_ids"]`` subscript:
            # SQLAlchemy renders the subscript as PostgreSQL's
            # ``payload['run_ids']``, a different parse-tree node than the
            # ``payload -> 'run_ids'`` the GIN index
            # ``ix_orchestration_exec_rescore_run_ids`` is built on, and
            # expression indexes match structurally. ``EXPLAIN`` on PG17: the
            # subscript filters ``@>`` row by row, ``->`` gets an
            # ``Index Cond``.
            OrchestrationExecution.payload.op("->")("run_ids").op("@>")(
                sql_cast([str(run_id)], PG_JSONB)
            ),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _execute_run_with_own_session(
    run_id: str, *, stopping: "asyncio.Event | None" = None, lease: RunLease | None = None
) -> None:
    """Execute a single run with its own DB session (for parallel execution)."""
    from app.core.database import async_session

    async with async_session() as session:
        await execute_evaluation_run(session, run_id, stopping=stopping, lease=lease)


async def _runs_left_unfinished(db: AsyncSession, run_ids: list[str]) -> bool:
    """Whether any of *run_ids* is left for the next worker: ``pending`` (paused by a stop, or never
    started), or ``running`` with no parked callback (its pause could not be written).

    Asked only of a stopping worker. Unreadable (a database blip) reads as
    yes: handed back, the next attempt finds what each run is, whereas a
    raise here would fail the evaluation and every run it paused.
    """
    parked = _has_parked_episode()
    try:
        left = await db.execute(
            select(EvaluationRun.id)
            .where(
                EvaluationRun.id.in_([uuid.UUID(r) for r in run_ids]),
                or_(
                    EvaluationRun.status == "pending",
                    and_(
                        EvaluationRun.status == "running",
                        EvaluationRun.completed_at.is_(None),
                        ~parked,
                    ),
                ),
            )
            .limit(1)
        )
        found = left.first() is not None
        # Read on the caller's session: release its connection again.
        await db.commit()
    except Exception as exc:
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.warning(
            "Evaluation runs left unfinished not read while stopping, handed back: %s",
            format_exception_message(exc),
        )
        return True
    return found


async def _run_unless_stopped(
    coro, cancel_event: "asyncio.Event | None", what: str
):
    """Run *coro*, abandoning it if the lease moves while it is in flight.

    ``_stop_if_no_longer_ours`` checks once, on entry, which is not enough for
    the batch-end aggregate work: the heartbeat fires at most every
    ``lease_seconds // 3``, and a headline recompute over a large evaluation
    can outlast that.

    Racing bounds the exposure to the event's latency, not the write's
    length. The replacement starts only after the lease EXPIRES (a full
    ``lease_seconds``), so aborting within one heartbeat interval keeps the
    two writers from overlapping into last-writer-wins headlines.

    NOT promised: headlines are recomputed, not accumulated, so an aborted
    pass can leave them partly updated; any later pass repairs them. Run and
    result rows are not touched here.
    """
    if cancel_event is None:
        return await coro

    # Preflight, like the fan-out: the checks before this are
    # awaits, so the event may already be set, and scheduling first would
    # start a write after the stop was known.
    if cancel_event.is_set():
        coro.close()
        logger.info(
            "%s: the lease moved before the batch-end aggregate write began",
            what,
        )
        raise asyncio.CancelledError()

    task = asyncio.ensure_future(coro)
    waiter = asyncio.ensure_future(cancel_event.wait())
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        stopped = not task.done()
        # Same re-check as ``_gather_runs_bounded``: ``stopped``
        # samples one instant, so an event firing as the write finished
        # would read as "completed while ours".
        if not stopped and cancel_event.is_set():
            stopped = True
    finally:
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter
        # Cancel and drain on EVERY exit, as in ``_gather_runs_bounded``:
        # ``asyncio.wait`` does not propagate the caller's cancellation, so
        # the write would otherwise keep running DETACHED and committing.
        if not task.done():
            task.cancel()
            # WE cancelled it, so its outcome is ours to discard, an error
            # included, which must not replace the cancellation being unwound.
            with contextlib.suppress(BaseException):
                await task
        elif not task.cancelled():
            # It finished on its own. Not awaited here: re-raising its error
            # inside ``finally`` would replace a cancellation being unwound,
            # and the adapter would treat a deadline or shutdown as a batch
            # failure. Nor suppressed: a failed shared performance pass must
            # not read as a successful batch. ``.exception()`` marks it
            # retrieved; ``task.result()`` below still raises it.
            task.exception()

    if stopped:
        logger.info(
            "%s: abandoning the batch-end aggregate write in flight, this "
            "execution is no longer ours",
            what,
        )
        raise asyncio.CancelledError()
    return task.result()


#: Passed as ``worker_id`` by a test that holds no lease. Explicit so skipping
#: the fence is ASKED for, never the result of leaving an argument off.
NO_LEASE: Final = "__no_lease__"


async def _fence_on_lease(
    db: AsyncSession,
    execution_id: uuid.UUID,
    worker_id: str,
    what: str,
) -> None:
    """Refuse to commit this transaction's writes unless the lease is still ours.

    ``_stop_if_no_longer_ours`` samples an in-memory event; this reads the
    execution row ``FOR UPDATE`` **inside the caller's transaction**, so the
    lease check and the writes it protects commit or roll back together. A
    check just before ``commit()`` is not a fence: the heartbeat can fire in
    between.

    Only usable where the writes share ``db``'s transaction (pre-dispatch
    reclaim, episode repair, ownership claim). Not the batch-end aggregates:
    ``_compute_run_headlines`` commits internally, and ``commit=False`` would
    hold the performance advisory lock across the evaluation.

    ``worker_id=NO_LEASE`` skips the fence for a caller that holds no lease.

    Raises ``LeaseLostError``, not ``CancelledError``: the fence fires
    when the cancel event is NOT set, so a bare ``CancelledError`` would be
    classified as a shutdown and vanish silently in ``_on_done``.
    ``LeaseLostError`` reaches the handler ``_do_execute`` already has.
    """
    if worker_id == NO_LEASE:
        return
    from app.models.orchestration import OrchestrationExecution

    row = (
        await db.execute(
            select(OrchestrationExecution.lease_owner)
            .where(OrchestrationExecution.id == execution_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row != worker_id:
        logger.info(
            "%s: refusing to commit, the lease is now %r and we are %r",
            what,
            row,
            worker_id,
        )
        # Imported here, not at module scope: orchestration_engine imports the
        # adapter registry, so a top-level import is a cycle.
        from app.services.orchestration_engine import LeaseLostError

        raise LeaseLostError(f"lease lost before {what}")


def _stop_if_no_longer_ours(
    cancel_event: "asyncio.Event | None", what: str
) -> None:
    """Refuse to author batch-end aggregates once the lease has moved.

    The fan-out's cancellation covers only the wait on runners. What follows
    (headline recompute, shared performance basis) mutates EVALUATION-scoped
    aggregates, and nothing there re-reads the execution ``FOR UPDATE`` with
    a lease predicate as ``engine.complete``/``fail`` do. Without this guard an
    abandoned worker overwrites headlines the replacement is recomputing.

    Raises ``CancelledError``, like ``_gather_runs_bounded``, so
    ``EvaluationAdapter.execute`` converts both stops the same way.
    """
    if cancel_event is not None and cancel_event.is_set():
        logger.info(
            "%s: stopping before the batch-end aggregate write, this "
            "execution is no longer ours",
            what,
        )
        raise asyncio.CancelledError()


async def _gather_runs_bounded(
    run_ids: list[str],
    max_concurrent: int,
    runner: "Callable[[str], Awaitable[None]]",
    *,
    cancel_event: "asyncio.Event | None" = None,
) -> None:
    """Run *runner* over *run_ids* concurrently, capped at *max_concurrent*.

    The whole agent x model matrix at once overwhelms provider rate limits:
    the 429/529 retries back off *inside* each call (up to 5, 2s to 60s, see
    ``llm.base.retry_llm_call``), inflating run durations into thousands of
    seconds. A bounded pool keeps the provider busy without
    thrashing.

    A non-positive *max_concurrent*, or a batch at or under the cap, runs
    everything at once. Exceptions are swallowed per run
    (``return_exceptions``) so one failing run never cancels the batch.

    *cancel_event*, when fired, CANCELS the batch: queued runs never
    start, and runs in flight are interrupted wherever they are, a provider
    call included.
    """
    if max_concurrent and max_concurrent > 0 and len(run_ids) > max_concurrent:
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _bounded(rid: str) -> None:
            async with semaphore:
                await runner(rid)

        coros = [_bounded(rid) for rid in run_ids]
    else:
        coros = [runner(rid) for rid in run_ids]

    if cancel_event is not None and cancel_event.is_set():
        # Checked BEFORE scheduling, defensively: ``asyncio.wait``
        # happens to return on a set waiter before the children run, but
        # "queued runs never start after cancellation" should not rest on
        # loop ordering.
        for coro in coros:
            coro.close()  # never awaited, so close them rather than leak warnings
        raise asyncio.CancelledError()

    batch = asyncio.gather(*coros, return_exceptions=True)
    if cancel_event is None:
        await batch
        return

    # Cancel the OUTER gather, never the children: with
    # ``return_exceptions=True`` a child cancelled alone becomes a
    # CancelledError *result* and the gather completes normally, swallowing
    # the stop. Cancelling the gather reaches every child.
    waiter = asyncio.create_task(cancel_event.wait())
    stopped = False
    try:
        await asyncio.wait(
            {batch, waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        stopped = not batch.done()
    finally:
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter
        # Cancel and drain on EVERY exit, not only when our event fired:
        # ``asyncio.wait`` does not propagate the caller's
        # cancellation, so unwinding through here (the deadline in
        # ``_do_execute``, a shutdown) would leave the runners DETACHED,
        # calling the judge and committing after the execution timed out.
        if not batch.done():
            batch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await batch

    # Re-check after a completed batch: ``stopped`` samples the
    # instant ``asyncio.wait`` returned, so an event firing as the last runner
    # finished would let the caller write aggregates on a lease it lost. The
    # batch's committed work stays; nothing further may be written.
    if not stopped and cancel_event.is_set():
        stopped = True

    if stopped:
        # PROPAGATE: a normal return would run the batch-end headline
        # work and let ``_do_execute`` write ``completed`` on a row the
        # replacement owns. ``EvaluationAdapter.execute`` turns this into
        # ``LeaseLostError``.
        raise asyncio.CancelledError()


#: What a run its worker abandoned says: the attempt's worker went without a word (a crash, an out-of-memory kill).
ABANDONED_RUN_ERROR = "Abandoned by a worker that did not survive its attempt — re-run to retry"
#: How many resumes of one run after a worker stop in a row may find it with no new result before it is failed
#: instead: an entry longer than the time between two rollouts is cut and redone at each one, paid each time.
MAX_STOP_RESUMES_WITHOUT_PROGRESS = 3
#: A cut counts as one without progress only when the entry it cut had run this long: a run taken from worker to
#: worker within one rolling restart, a few seconds on each, did not stall. Work time, never queue or hold time.
STOP_RESUME_MIN_WORK_SECONDS = 300
#: The execution payload key recording the resumes after a stop: ``{"runs": {run_id: {results, stalled}},
#: "consumed": <the stop marker last resumed from>}``.
STOP_RESUMES_PAYLOAD_KEY = "stop_resumes"
#: The execution payload key the evaluation adapter's hand-back writes, ``{"at": <ISO time>}``: a stop marker that
#: survives the claims, retries and AI holds before the reclaim resumes from it.
HANDED_BACK_PAYLOAD_KEY = "evaluation_handed_back"


def _aware_time(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _parsed_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _aware_time(parsed)


def pending_stop_cut(payload: Mapping[str, Any] | None) -> datetime | None:
    """When the last stop this execution has not resumed from happened (a hand-back, or a take-back on the
    worker's word), else ``None``.

    Kept in the payload, not the attempt's state, so a retry or an AI hold before the resume does not lose it;
    marked consumed by the reclaim that resumes from it, so a later crash is not taken for a stop.
    """
    from app.services.orchestration_engine import LEASE_EXPIRY_PAYLOAD_KEY, stopped_on_its_word

    payload = payload or {}
    marks: list[datetime] = []
    handed = payload.get(HANDED_BACK_PAYLOAD_KEY)
    if isinstance(handed, dict) and (at := _parsed_time(handed.get("at"))) is not None:
        marks.append(at)
    record = payload.get(LEASE_EXPIRY_PAYLOAD_KEY)
    if (
        isinstance(record, dict)
        and stopped_on_its_word(record.get("owner_record"))
        and (at := _parsed_time(record.get("at"))) is not None
    ):
        marks.append(at)
    if not marks:
        return None
    latest = max(marks)
    resumes = payload.get(STOP_RESUMES_PAYLOAD_KEY)
    consumed = _parsed_time(resumes.get("consumed")) if isinstance(resumes, dict) else None
    return None if consumed is not None and consumed >= latest else latest


def _work_stopped_at(payload: Mapping[str, Any], marker: datetime) -> datetime:
    """When the stop at *marker* (:func:`pending_stop_cut`) ended the work of the entry it cut.

    A hand-back is written by the stopping worker, at the stop. A take-back is written later, by the worker that
    took the row back, after the restart or the lease wait: the work ended at the gone worker's last word.
    """
    from app.services.orchestration_engine import LEASE_EXPIRY_PAYLOAD_KEY

    record = payload.get(LEASE_EXPIRY_PAYLOAD_KEY)
    if isinstance(record, dict) and _parsed_time(record.get("at")) == marker:
        owner = record.get("owner_record")
        said = _parsed_time(owner.get("at")) if isinstance(owner, dict) else None
        if said is not None:
            return min(said, marker)
    return marker


def _abandoned_values(error: str, now: datetime) -> dict[str, Any]:
    """What a run failed by the pre-dispatch reclaim is written with: *error*, ended *now*, its duration."""
    return {
        "status": "failed",
        "error": error[:500],
        "completed_at": now,
        "duration_seconds": case(
            (EvaluationRun.started_at.is_(None), None),
            else_=func.round(
                sql_cast(func.extract("epoch", now - EvaluationRun.started_at), Numeric),
                2,
            ),
        ),
    }


def _stalled_resumes(
    record: Mapping[str, Any],
    cut: list[uuid.UUID],
    results: Mapping[uuid.UUID, int],
    worked: Mapping[uuid.UUID, float],
) -> tuple[dict[str, Any], list[uuid.UUID]]:
    """The runs' resume records after one more resume of *cut*, and the runs past the no-progress bound.

    Per run: its first resume is the baseline, counted as none; then a new
    result since its last resume resets its count; none, with the entry the
    stop cut *worked* on for at least :data:`STOP_RESUME_MIN_WORK_SECONDS`,
    adds one; none in less (a hop within one rolling restart) leaves it. A
    run past the bound is dropped from the records, so a later resume of it
    starts afresh. Progress is measured between two resumes, so a run with
    results before its first stop and one with none are refused on the same
    cut.
    """
    runs = dict(record) if isinstance(record, Mapping) else {}
    refused: list[uuid.UUID] = []
    for run_id in cut:
        previous = runs.get(str(run_id))
        count = results.get(run_id, 0)
        if not isinstance(previous, dict):
            runs[str(run_id)] = {"results": count, "stalled": 0}
            continue
        before, stalled = previous.get("results"), previous.get("stalled")
        before = before if isinstance(before, int) else 0
        stalled = stalled if isinstance(stalled, int) else 0
        if count > before:
            stalled = 0
        elif worked.get(run_id, 0.0) >= STOP_RESUME_MIN_WORK_SECONDS:
            stalled += 1
        if stalled >= MAX_STOP_RESUMES_WITHOUT_PROGRESS:
            refused.append(run_id)
            runs.pop(str(run_id), None)
            continue
        runs[str(run_id)] = {"results": count, "stalled": stalled}
    return runs, refused


async def _resume_runs_cut_by_a_stop(
    db: AsyncSession,
    eid: uuid.UUID,
    execution_id: uuid.UUID,
    parked: ColumnElement[bool],
    evaluation_id: str,
) -> list[uuid.UUID]:
    """Put the runs a stopping worker left ``"running"`` back to ``pending``; return the ones past the stall bound.

    Inside the reclaim's transaction, under its fence (execution, then runs, then episodes): the marker is
    consumed in the same commit as the runs. Only the cut entry's open episode is deleted, so it is redone
    while every finished one is skipped. Runs with a parked callback episode are left alone: their callbacks
    still drive them.
    """
    from app.models.orchestration import OrchestrationExecution

    now = datetime.now(UTC)
    execution = await db.get(OrchestrationExecution, execution_id)
    payload = dict((execution.payload if execution is not None else None) or {})
    marker = pending_stop_cut(payload)
    record = payload.get(STOP_RESUMES_PAYLOAD_KEY)
    record = dict(record) if isinstance(record, dict) else {}

    cut = list(
        (
            await db.execute(
                select(EvaluationRun.id).where(
                    EvaluationRun.evaluation_id == eid,
                    runs_this_execution_may_touch(execution_id, repeating_is_harmless=False),
                    EvaluationRun.status == "running",
                    # As in the failing UPDATE: a finished run ``"running"``
                    # again for a rescore is the rescore's.
                    EvaluationRun.completed_at.is_(None),
                    ~parked,
                )
            )
        )
        .scalars()
        .all()
    )
    refused: list[uuid.UUID] = []
    if cut and execution is not None:
        counts = dict(
            (
                await db.execute(
                    select(EvaluationResult.run_id, func.count(EvaluationResult.id))
                    .where(EvaluationResult.run_id.in_(cut))
                    .group_by(EvaluationResult.run_id)
                )
            )
            .tuples()
            .all()
        )
        # How long the entry each stop cut had run: from its open episode's start to the stop, never the
        # restart or lease wait before the take-back.
        stopped_at = _work_stopped_at(payload, marker) if marker is not None else now
        started = dict(
            (
                await db.execute(
                    select(EvaluationEpisode.run_id, func.min(EvaluationEpisode.created_at))
                    .where(
                        EvaluationEpisode.run_id.in_(cut),
                        EvaluationEpisode.status.notin_(_SETTLED_EPISODE_STATUSES),
                    )
                    .group_by(EvaluationEpisode.run_id)
                )
            )
            .tuples()
            .all()
        )
        worked = {
            run_id: max(0.0, (stopped_at - _aware_time(at)).total_seconds()) for run_id, at in started.items()
        }
        record["runs"], refused = _stalled_resumes(record.get("runs") or {}, cut, counts, worked)
    if execution is not None and (cut or marker is not None):
        if marker is not None:
            record["consumed"] = marker.isoformat()
        execution.payload = {**payload, STOP_RESUMES_PAYLOAD_KEY: record}
    if refused:
        logger.warning(
            "execute_evaluation: execution %s fails %d run(s) of evaluation %s cut by %d worker stops in a row "
            "with no entry finished between them: %s",
            execution_id,
            len(refused),
            evaluation_id,
            MAX_STOP_RESUMES_WITHOUT_PROGRESS + 1,
            [str(r) for r in refused[:_ABANDONED_LOG_SAMPLE]],
        )
    to_resume = [run_id for run_id in cut if run_id not in refused]
    if not to_resume:
        return refused
    resumed = list(
        (
            await db.execute(
                update(EvaluationRun)
                .where(
                    EvaluationRun.id.in_(to_resume),
                    EvaluationRun.status == "running",
                    EvaluationRun.completed_at.is_(None),
                )
                .values(status="pending")
                .returning(EvaluationRun.id)
            )
        )
        .scalars()
        .all()
    )
    if resumed:
        await db.execute(
            delete(EvaluationEpisode).where(
                EvaluationEpisode.run_id.in_(resumed),
                EvaluationEpisode.status.notin_(_SETTLED_EPISODE_STATUSES),
            )
        )
        logger.warning(
            "execute_evaluation: execution %s resumes %d run(s) its stopping worker "
            "left running for evaluation %s, from their finished entries; first %d: %s",
            execution_id,
            len(resumed),
            evaluation_id,
            min(len(resumed), _ABANDONED_LOG_SAMPLE),
            [str(r) for r in resumed[:_ABANDONED_LOG_SAMPLE]],
        )
    return refused


#: What a run its resumes past :data:`MAX_STOP_RESUMES_WITHOUT_PROGRESS` fail with.
STALLED_RUN_ERROR = (
    f"Cut by {MAX_STOP_RESUMES_WITHOUT_PROGRESS + 1} worker stops in a row before it finished an entry "
    "— re-run to retry"
)


async def execute_evaluation(
    db: AsyncSession,
    evaluation_id: str,
    trigger_reason: str,
    execution_id: uuid.UUID,
    cancel_event: "asyncio.Event | None" = None,
    *,
    worker_id: str,
    stopping: "asyncio.Event | None" = None,
    resume_stopped_runs: bool = False,
) -> bool:
    """Execute all pending runs this execution owns (parallel or sequential).

    Returns ``True`` when it was handed back: *stopping* (its worker is
    stopping: a deploy, a restart) was set and each run in flight paused at
    an entry boundary, ``pending`` again, with nothing written at batch end.
    The caller hands the execution back for the next worker to resume.

    *resume_stopped_runs*: this attempt follows a take-back from a worker that
    said it was stopping (or that a deploy replaced). The runs it left
    ``"running"`` resume from their finished entries instead of being failed
    as abandoned: the entry it was cut on is the only work redone.

    ``execution_id`` is the orchestration execution driving this call. Runs are
    selected by ownership, not just by evaluation: two launches of one
    evaluation can overlap (manual trigger, scheduled sweep, replay), and
    adopting every pending run would let launch A run B's runs; if A then
    died, its ownership-scoped cleanup would skip them, leaving them
    "running" forever with no periodic sweep to catch them. Owning,
    running and failing the same set closes that.

    Runs predating ``owner_execution_id`` are *claimed* into this execution
    before dispatch, so two launches cannot both pick up the same one.

    A retry also terminalises the runs a previous attempt of this execution
    abandoned: dispatch selects ``"pending"``, so a run left
    ``"running"`` by a killed worker is invisible to it, and nothing else
    cleans it up once the retry succeeds.

    Owns *db*'s transaction: the claim commits, and ``execute_evaluation_run``
    commits per run. The caller must hold no uncommitted work on this
    session: the claim must be durable before dispatch.

    ``cancel_event`` stops the batch: fired when the lease is lost and
    by the fast cancel watcher (emergency stop, operator cancel). The
    fan-out interrupts the runs in flight and raises, so nothing is written
    after the stop. It is not handed to each run: a run reads only its own
    row for a cancel, since the event also fires while the row is still
    ``running`` and the replacement owns it.
    """
    eid = uuid.UUID(evaluation_id)

    eval_row = await db.get(Evaluation, eid)
    sequential = eval_row is not None and getattr(eval_row, "run_mode", "parallel") == "sequential"

    # ORDER IS LOAD-BEARING: reclaim BEFORE the claim. The claim
    # stamps this execution's id onto unowned rows, which would make a freshly
    # adopted row look like one a previous attempt abandoned. The claim is
    # ``pending``-scoped, but the reclaim must not depend on that.
    #
    # Nothing below may be written by a worker that lost the work:
    # the reclaim FAILS runs, the repair terminalises episodes and the claim
    # stamps ownership, all under the replacement's ``execution_id``. A RETRY
    # waits on row locks the previous attempt may hold, which is when this
    # phase can outlast a lease.
    _stop_if_no_longer_ours(cancel_event, f"execute_evaluation: evaluation {eid}")

    # The lease lock comes BEFORE the reclaim's UPDATE. Run row locks
    # first, then the execution's, is the reverse of ``engine.fail`` ->
    # ``on_dead_letter`` -> ``_fail_active_runs``: two overlapping attempts
    # would deadlock, and the aborted reclaim would land in the adapter's
    # non-retryable handler and fail the runs.
    #
    # Fencing first is also stronger: ``FOR UPDATE`` is held to commit, so
    # the lease cannot move under us at all.
    #
    # Trade-off: the heartbeat's ``extend_lease`` waits on the phase below.
    # Acceptable: with one lock order, a second attempt waits on the
    # execution lock before holding any run row.

    # TERMINALISE the runs a previous attempt of THIS execution abandoned.
    # A worker killed mid-batch (SIGKILL past the 40s stop grace,
    # OOM, host loss) leaves its runs "running". The retry of the SAME
    # execution dispatches only "pending" runs and then succeeds, so no
    # `_fail_active_runs` call site fires; since the cleanup is
    # ownership-scoped, this is the only thing that can.
    #
    # Failed, not re-run: a half-finished run already committed partial
    # results, which a re-run would double-count unless deleted, a
    # destructive step in a crash path. The re-run action leaves the judge
    # spend to a human.
    #
    # A conditional UPDATE, evaluated at write time, so a run that finished
    # or was cancelled meanwhile is left alone.
    #
    # PARKED RUNS ARE NOT ABANDONED. A callback-backed run stays
    # "running" while its episodes wait for a callback, and already has an
    # owner: ``waiting_callback`` is driven by the callback and, on expiry, by
    # ``timeout_waiting_episodes`` (the adapter's ``periodic``); ``resumed``
    # by a ``resume_episode`` execution. Failing it would kill healthy work.
    #
    # These two statuses suffice: ``process_resume_episode`` keeps the episode
    # ``resumed`` until the transaction that flips it to ``"running"`` and
    # finalizes it, so the flip is never seen; other sessions see ``resumed`` ->
    # terminal. Not "has a non-terminal episode": an abandoned run also
    # leaves a ``running`` episode, the entry it died on.
    #
    # That entry is repaired per EPISODE after this UPDATE, so a
    # parked sibling and an orphan can coexist on one run without either
    # being lost.
    #
    # KNOWN RESIDUAL: a stalled attempt looks identical to a dead one.
    # ``reclaim_expired_leases`` is time-based, and a worker stalled
    # past the lease (GC pause, blocked event loop, DB blip) keeps executing.
    # No attempt counter tells them apart: the stalled attempt IS the previous
    # attempt. Cooperative cancellation only narrows this: it fires at
    # the next heartbeat, which a blocked event loop cannot run either.
    #
    # Fencing the writer is DELIBERATELY NOT DONE: it would reject the stalled
    # attempt's completion and leave a finished run "failed". The reclaim
    # infers the attempt died; its completion proves it did not, and proof
    # must win, through ``execute_evaluation_run``'s completion write, which
    # must stay unconditional (a conditional one would leave a finished run
    # "failed"). The FINAL state is right in every ordering:
    #   * the stalled attempt completes afterwards -> "completed", ``error``
    #     cleared;
    #   * it dies instead                          -> stays "failed";
    #   * it completes first                       -> the ``status == running``
    #     predicate no longer matches.
    # What remains is a transient "failed" during the overlap, better than a
    # run stuck forever on every deploy, and better than fencing's permanent
    # wrong answer.
    #
    # The EPISODE repair below has the same residual and resolution:
    # a stalled attempt calls ``finalize_episode``, which writes its outcome
    # unconditionally (``start_episode`` does not flip a ``"failed"`` row
    # back), so the real result lands last.
    #
    # SECOND KNOWN RESIDUAL: an entry whose episode never materialised.
    # ``start_episode`` only flushes, so a worker dying right after
    # the run's ``"running"`` commit leaves no episode row. Both repairs work
    # on episode rows: on a run with a parked sibling the run is skipped, the
    # repair finds nothing, and ``_reconcile_run_status`` stays short of
    # ``total_entries`` forever. Closing it needs entry-level reconciliation
    # or durable episode creation on the per-entry hot path.
    parked = _has_parked_episode()
    abandoned_now = datetime.now(UTC)
    # The WHOLE reclaim, query and repair sit inside the retryable boundary:
    # outside it, a transient fault takes the generic non-retryable
    # path, which fails runs WITHOUT their episodes.
    # Imported before the try (the ``except`` needs the name), lazily to
    # avoid the import cycle.
    from app.services.orchestration_engine import LeaseLostError

    try:
        await _fence_on_lease(
            db, execution_id, worker_id, f"execute_evaluation: evaluation {eid}"
        )
        stalled: list[uuid.UUID] = []
        if resume_stopped_runs:
            # Its previous attempt's worker said it was stopping (a rollout, a
            # node drain): the work did not kill it. The runs it owned go back
            # to ``pending`` and resume from their finished entries, BEFORE the
            # failing UPDATE below, which then reaches only the unowned rows.
            # Owned rows only: an unowned one may be another release's. A run
            # past its bound of resumes with no progress is failed with the reason.
            refused = await _resume_runs_cut_by_a_stop(
                db, eid, execution_id, parked, evaluation_id
            )
            if refused:
                stalled = list(
                    (
                        await db.execute(
                            update(EvaluationRun)
                            .where(
                                EvaluationRun.id.in_(refused),
                                EvaluationRun.status == "running",
                                EvaluationRun.completed_at.is_(None),
                            )
                            .values(**_abandoned_values(STALLED_RUN_ERROR, abandoned_now))
                            .returning(EvaluationRun.id)
                        )
                    )
                    .scalars()
                    .all()
                )
        abandoned = (
            await db.execute(
                update(EvaluationRun)
                .where(
                    EvaluationRun.evaluation_id == eid,
                    # Unowned rows are in scope (see ``run_ownership``): this
                    # only writes ``status = "failed"``.
                    runs_this_execution_may_touch(
                        execution_id, repeating_is_harmless=True
                    ),
                    EvaluationRun.status == "running",
                    # Load-bearing, as in both ``_fail_*`` siblings: a
                    # finished run ``"running"`` again for a rescore must not be
                    # failed, nor its ``completed_at`` / ``duration_seconds``
                    # (the ORIGINAL execution's) overwritten.
                    EvaluationRun.completed_at.is_(None),
                    ~parked,
                )
                .values(**_abandoned_values(ABANDONED_RUN_ERROR, abandoned_now))
                .returning(EvaluationRun.id)
            )
        ).scalars().all()
        if abandoned:
            logger.warning(
                "execute_evaluation: execution %s failed %d run(s) abandoned by a "
                "previous attempt of itself for evaluation %s; first %d: %s",
                execution_id,
                len(abandoned),
                evaluation_id,
                min(len(abandoned), _ABANDONED_LOG_SAMPLE),
                [str(r) for r in abandoned[:_ABANDONED_LOG_SAMPLE]],
            )

        # Repair the EPISODES the same attempt abandoned. The reclaim
        # skips a run with a parked episode, but the entry loop ``continue``s
        # past a paused entry, so that run can also hold the entry the worker
        # died on, stuck ``"running"``; ``_reconcile_run_status`` would then
        # return early forever. Terminalising the orphan leaves the parked
        # sibling alone, and reconciliation finishes the run once its callback
        # resolves. Evaluated under a row lock, so an episode a resume is
        # finalizing is re-read as terminal and skipped.
        #
        # SAME TRANSACTION as the reclaim: its UPDATE holds the locks
        # of every run it failed, so no rescore can take one in between, and a
        # crash cannot leave a run failed with its episode ``"running"``.
        #
        # Two sets: runs still active are re-validated against this
        # execution's ownership under the lock (another launch may have
        # claimed an unowned one); runs just reclaimed are held by this
        # transaction and need no check, which matters since the reclaim
        # covers unowned rows.
        still_active = (
            await db.execute(
                select(EvaluationRun.id).where(
                    EvaluationRun.evaluation_id == eid,
                    # Same NULL arm as the reclaim: an unowned run both
                    # ``"running"`` and parked is not in ``abandoned``, and strict
                    # ownership would strand its orphan. ``fail_orphaned_episodes``
                    # re-validates under the lock and confines the NULL arm to
                    # ``"running"``.
                    runs_this_execution_may_touch(
                        execution_id, repeating_is_harmless=True
                    ),
                    EvaluationRun.status.in_(["pending", "running"]),
                    # As in the reclaim: a rescore's run must not be
                    # reconciled over the original ``completed_at`` /
                    # ``duration_seconds``.
                    EvaluationRun.completed_at.is_(None),
                )
            )
        ).scalars().all()
        if still_active or abandoned or stalled:
            repaired = 0
            if stalled:
                # The entry a stall cut says what its run says, not a crash.
                repaired += await fail_orphaned_episodes(
                    db,
                    owned_run_ids=[],
                    reclaimed_run_ids=stalled,
                    execution_id=execution_id,
                    error=STALLED_RUN_ERROR,
                )
            if still_active or abandoned:
                repaired += await fail_orphaned_episodes(
                    db,
                    owned_run_ids=list(still_active),
                    reclaimed_run_ids=list(abandoned),
                    execution_id=execution_id,
                    error=ABANDONED_RUN_ERROR,
                )
            # One commit for reclaim and repair, under the fence taken at the
            # TOP of this transaction: it cannot land after the lease moved and
            # fail runs the replacement is running.
            await db.commit()
            if repaired:
                logger.warning(
                    "execute_evaluation: execution %s terminalised orphaned "
                    "episode(s) on %d run(s) for evaluation %s",
                    execution_id,
                    repaired,
                    evaluation_id,
                )
    except LeaseLostError:
        # The fence, not a repair failure: as ``AbandonedWorkRepairError`` the
        # adapter would retry a batch that now belongs to someone else.
        await db.rollback()
        raise
    except Exception as exc:
        # The rollback undoes the reclaim too; a retry redoes both.
        await db.rollback()
        raise AbandonedWorkRepairError(
            f"Failed to repair work abandoned by a previous attempt of "
            f"execution {execution_id}: {format_exception_message(exc)}"
        ) from exc

    # CLAIM the unowned pending runs instead of selecting ``owner IS NULL``:
    # two overlapping launches would BOTH match it, and dispatch is
    # not idempotent (the judge re-charged, two writers on one row). The
    # conditional UPDATE is exclusive: the loser's ``owner_execution_id IS
    # NULL`` matches nothing.
    # A NEW transaction, so the fence is retaken, before the UPDATE for the
    # lock order above.
    await _fence_on_lease(
        db, execution_id, worker_id, f"execute_evaluation: evaluation {eid}"
    )
    claimed = (
        await db.execute(
            update(EvaluationRun)
            .where(
                EvaluationRun.evaluation_id == eid,
                EvaluationRun.status == "pending",
                runs_owned_by_nobody(),
            )
            .values(owner_execution_id=execution_id)
            .returning(EvaluationRun.id)
        )
    ).scalars().all()
    if claimed:
        await db.commit()
        logger.info(
            "execute_evaluation: execution %s claimed %d unowned pending run(s) "
            "for evaluation %s",
            execution_id,
            len(claimed),
            evaluation_id,
        )

    # Every dispatchable run now carries this execution's id — no NULL arm here.
    # In creation order: a placebo comparison creates its arms interleaved
    # trial by trial, and the bounded pool starts them in list order.
    result = await db.execute(
        select(EvaluationRun)
        .where(
            EvaluationRun.evaluation_id == eid,
            EvaluationRun.status == "pending",
            runs_this_execution_may_touch(execution_id, repeating_is_harmless=False),
        )
        .order_by(EvaluationRun.created_at, EvaluationRun.id)
    )
    runs = result.scalars().all()
    run_ids = [str(r.id) for r in runs]

    from app.core.config import settings

    cap = settings.evaluation_max_concurrent_runs
    eval_name = getattr(eval_row, "name", None) or "unknown"
    job_timeout = getattr(eval_row, "timeout_seconds", None)
    job_timeout_display = f"{job_timeout}s" if job_timeout else "none"
    started = time.perf_counter()
    logger.info(
        "execute_evaluation start: evaluation=%s ('%s') trigger=%s runs=%d "
        "mode=%s concurrency_cap=%d job_timeout=%s",
        evaluation_id,
        eval_name,
        trigger_reason,
        len(run_ids),
        "sequential" if sequential else "parallel",
        cap,
        job_timeout_display,
    )
    if not run_ids:
        logger.info(
            "execute_evaluation: evaluation %s has no pending runs to execute",
            evaluation_id,
        )
        # Resumed after a stop that cut the batch-end pass itself: its runs
        # all finished, and their headlines and placebo settlement are still
        # this execution's to write, below.
        if not resume_stopped_runs:
            return False
    # Runs beyond the cap stay ``pending`` while QUEUED (safe from the
    # reclaim, which touches ``"running"`` rows only). The queue depth bounds
    # wall-clock time and is the first thing to know about a slow or partly
    # pending evaluation.
    if not sequential and cap and 0 < cap < len(run_ids):
        logger.warning(
            "execute_evaluation: evaluation %s has %d runs but concurrency cap "
            "is %d — %d run(s) will be queued behind the semaphore until slots "
            "free up. Raise EVALUATION_MAX_CONCURRENT_RUNS to widen the pool.",
            evaluation_id,
            len(run_ids),
            cap,
            len(run_ids) - cap,
        )

    # Release the caller's connection before the long phase below: runs use
    # their own sessions and the scoring pass a fresh one. From the
    # reconciler, ``db`` is the reconciler session, and an open transaction
    # would pin a pooled connection for the whole batch. ``expire_on_commit``
    # is False, so the values read stay usable.
    await db.commit()

    # Sequential dispatch is the SAME fan-out with a cap of one: never
    # two model loads at once, and a stop interrupts the run in flight and
    # raises, rather than waiting for a boundary or returning normally.
    if run_ids:
        await _gather_runs_bounded(
            run_ids,
            1 if sequential else cap,
            functools.partial(
                _execute_run_with_own_session,
                stopping=stopping,
                lease=None if worker_id == NO_LEASE else (execution_id, worker_id),
            ),
            cancel_event=cancel_event,
        )

    # The batch finished, which is NOT still owning it: the lease can move
    # between the last run's commit and this write.
    _stop_if_no_longer_ours(cancel_event, f"execute_evaluation: evaluation {eid}")

    # Its worker is stopping and a run paused at an entry boundary (or never
    # started, or its pause failed and left it ``running``): handed back
    # whole, nothing written at batch end, so the next attempt resumes it. The headlines
    # and the placebo settlement are the batch's, computed once its runs
    # finish on the next worker.
    if run_ids and stopping is not None and stopping.is_set() and await _runs_left_unfinished(db, run_ids):
        logger.warning(
            "execute_evaluation: evaluation %s handed back by its stopping worker "
            "after %ss, its unfinished runs paused at an entry boundary",
            evaluation_id,
            round(time.perf_counter() - started, 2),
        )
        return True

    # A batch resumed after a stop is also every run this execution finished
    # before it: a stopping worker writes nothing at batch end, so the
    # headlines and placebo settlement of the runs it completed are this
    # batch's to compute.
    batch_run_ids = run_ids
    if resume_stopped_runs:
        owned = await db.execute(
            select(EvaluationRun.id).where(
                EvaluationRun.evaluation_id == eid,
                runs_this_execution_may_touch(execution_id, repeating_is_harmless=False),
            )
        )
        batch_run_ids = sorted({*run_ids, *(str(r) for r in owned.scalars().all())})
        await db.commit()

    # The batch scope applies to the judge and quality headlines; Overall
    # Performance rescores the whole evaluation, so a second batch never
    # leaves the first scored against a different run set.
    from app.core.database import async_session

    async with async_session() as session:
        # Raced, not just pre-checked: the lease can move WHILE this
        # evaluation-scoped write runs.
        await _run_unless_stopped(
            _compute_run_headlines(session, eid, batch_run_ids=batch_run_ids),
            cancel_event,
            f"execute_evaluation: evaluation {eid}",
        )
        # The placebo comparisons whose runs this batch finished. Best
        # effort: a read settles any this misses.
        try:
            from app.services.evaluation.placebo_comparisons import settle_comparisons_of_runs

            await settle_comparisons_of_runs(session, batch_run_ids)
        except Exception as settle_err:
            await session.rollback()
            logger.warning(
                "execute_evaluation: settling the placebo comparisons of evaluation %s failed: %s",
                eid,
                format_exception_message(settle_err),
            )

        # "N completed / M failed / K pending" in the worker logs, so a
        # batch's outcome needs no DB query.
        breakdown_rows = await session.execute(
            select(EvaluationRun.status, func.count(EvaluationRun.id))
            .where(EvaluationRun.id.in_([uuid.UUID(r) for r in run_ids]))
            .group_by(EvaluationRun.status)
        )
        breakdown = {status: count for status, count in breakdown_rows.all()}

    elapsed = round(time.perf_counter() - started, 2)
    logger.info(
        "execute_evaluation done: evaluation=%s ('%s') runs=%d elapsed=%ss "
        "breakdown=%s",
        evaluation_id,
        eval_name,
        len(run_ids),
        elapsed,
        breakdown,
    )
    still_pending = breakdown.get("pending", 0)
    if still_pending:
        logger.warning(
            "execute_evaluation: evaluation %s finished with %d run(s) still "
            "'pending' — the batch drained without executing them. Possible "
            "causes: a run was settled out from under the worker (executed by "
            "another worker, or cancelled while queued), a worker "
            "interruption/timeout, or a lost enqueue.",
            evaluation_id,
            still_pending,
        )
    return False
