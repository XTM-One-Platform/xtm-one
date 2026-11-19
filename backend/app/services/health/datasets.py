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

"""Evaluation datasets Platform Health builds from production (#8257, PRD 6.7, B15, B16).

:func:`build_dataset_from_traces` turns a target's consented production work
into an ordinary evaluation dataset, marked ``concierge_built``, owned by no
user (Platform Health has no user row) and so visible to administrators only:

* **cases to fix**: negatively rated answers and failed runs. The entry has no
  expected answer; its judge instructions carry the rating's comment and the
  answer as given, each as quarantined data, never as instructions;
* **non-regression cases**: positively rated answers (completed runs, for a
  flow, which nobody rates), the answer as given being the expected one.

Entries keep the content as it was, unredacted (B16: the opt-in notice covers
it). Every source goes through level 1's consent rule twice: in the reading
query (``consent_filter``) and right before its content is copied
(``require_content_read``, which audits the read). Each entry keeps its source
and the tool calls it recorded (``concierge_dataset_entry_sources``), which a
sandbox run of the evaluation replays (level 2's ``RecordedCall``).

A revocation removes what was derived: :func:`withdraw_entries` (registered
with level 1's ``register_revocation_handler``) deletes the entries, their
dataset-version copies and the results scored on them, and marks the runs that
lost results so no headline reads as complete.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.annotation import Annotation
from app.models.assignment import Assignment, AssignmentRun
from app.models.chat import Conversation, Message, MessageFeedback
from app.models.dataset import Dataset, DatasetEntry
from app.models.dataset_version import DatasetVersion, DatasetVersionEntry
from app.models.evaluation import EvaluationResult, EvaluationRun
from app.models.flow import Flow
from app.models.flow_run import FLOW_RUN_STATUS_COMPLETED, FLOW_RUN_STATUS_FAILED, FlowRun
from app.models.health_dataset import (
    CASE_NON_REGRESSION,
    CASE_TO_FIX,
    SOURCE_ASSIGNMENT_RUN,
    SOURCE_FLOW_RUN,
    SOURCE_MESSAGE,
    HealthDatasetEntrySource,
)
from app.services.health.consent import (
    ConsentDeniedError,
    Revocation,
    consent_filter,
    content_readable,
    in_supervision_scope,
    register_revocation_handler,
    require_content_read,
)

logger = logging.getLogger(__name__)

TargetType = Literal["agent", "assignment", "flow"]

#: The family a built dataset scores with: answers are free text.
DATASET_FAMILY = "PROSE"
MAX_ENTRIES = 200
#: An input or an answer past this is left out, never cut: a cut case is another case.
MAX_TEXT_CHARS = 32_000
#: Recorded tool calls kept per entry, in characters of JSON; past it the entry is left out.
MAX_RECORDINGS_CHARS = 1_000_000
#: What a run stopped by a revocation says.
WITHDRAWN_RUN_ERROR = "Stopped: consent was withdrawn for some of its entries."


class DatasetBuildError(Exception):
    """A build refused before anything was read; the message is generic."""


@dataclass
class DatasetBuildResult:
    dataset_id: uuid.UUID
    added: dict[str, int] = field(default_factory=lambda: {CASE_TO_FIX: 0, CASE_NON_REGRESSION: 0})
    #: Per reason, the sources left out: counts only, never their content.
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


@dataclass
class _Case:
    case_kind: str
    source_kind: str
    source_id: uuid.UUID
    input_prompt: str
    answer: str
    comment: str | None = None
    agent_id: uuid.UUID | None = None
    flow_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    rater_id: uuid.UUID | None = None
    rating_id: uuid.UUID | None = None
    trace_id: uuid.UUID | None = None
    trace_ids: list[uuid.UUID] = field(default_factory=list)
    #: A flow run's agent nodes and called flows, at every ``flow.call`` depth,
    #: whose content the run's answer and calls carry.
    node_agent_ids: list[uuid.UUID] = field(default_factory=list)
    node_flow_ids: list[uuid.UUID] = field(default_factory=list)


# ── Reading the sources ───────────────────────────────────────────────────


def _rated(negative: bool, positive: bool) -> list[str]:
    return [rating for rating, wanted in (("negative", negative), ("positive", positive)) if wanted]


def _case_kind(rating: str) -> str:
    return CASE_TO_FIX if rating == "negative" else CASE_NON_REGRESSION


async def _rated_messages(
    db: AsyncSession, agent_id: uuid.UUID, ratings: list[str], since: datetime | None, limit: int
) -> list[_Case]:
    """The agent's rated chat answers: chat ratings live in ``message_feedback``, not ``annotations``."""
    from app.services.annotations import agent_answer_clauses

    if not ratings:
        return []
    query = (
        select(MessageFeedback, Message, Conversation.user_id)
        .join(Message, Message.id == MessageFeedback.message_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            *agent_answer_clauses(agent_id),
            MessageFeedback.rating.in_(ratings),
            await consent_filter(
                db, agent_id_col=Conversation.agent_id, user_id_cols=(Conversation.user_id, MessageFeedback.user_id)
            ),
        )
        .order_by(MessageFeedback.updated_at.desc())
        .limit(limit)
    )
    if since is not None:
        query = query.where(MessageFeedback.updated_at >= since)
    cases = []
    for feedback, message, user_id in (await db.execute(query)).all():
        question = await db.scalar(
            select(Message.content)
            .where(
                Message.conversation_id == message.conversation_id,
                Message.role == "user",
                Message.sequence < message.sequence,
            )
            .order_by(Message.sequence.desc())
            .limit(1)
        )
        cases.append(
            _Case(
                case_kind=_case_kind(feedback.rating),
                source_kind=SOURCE_MESSAGE,
                source_id=message.id,
                input_prompt=question or "",
                answer=message.content or "",
                comment=feedback.comment,
                agent_id=agent_id,
                user_id=user_id,
                rater_id=feedback.user_id,
                rating_id=feedback.id,
                trace_id=message.llm_trace_id,
                trace_ids=[message.llm_trace_id] if message.llm_trace_id else [],
            )
        )
    return cases


def _run_prompt(run: AssignmentRun) -> str:
    """The first user turn the run's agent received: what an evaluation sends it."""
    for message in run.messages or []:
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "\n".join(
                    part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
                )
    return ""


async def _assignment_runs(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    assignment_id: uuid.UUID | None,
    ratings: list[str],
    failed: bool,
    since: datetime | None,
    limit: int,
) -> list[_Case]:
    readable = await consent_filter(db, agent_id_col=AssignmentRun.agent_id, user_id_cols=(AssignmentRun.user_id,))
    scope = [AssignmentRun.agent_id == agent_id, readable]
    if assignment_id is not None:
        scope.append(AssignmentRun.assignment_id == assignment_id)
    if since is not None:
        scope.append(AssignmentRun.created_at >= since)
    cases: list[_Case] = []
    if ratings:
        rows = (
            await db.execute(
                select(Annotation, AssignmentRun)
                .join(AssignmentRun, AssignmentRun.id == Annotation.target_id)
                .where(
                    Annotation.target_type == "assignment_run",
                    Annotation.rating.in_(ratings),
                    *scope,
                    await consent_filter(db, agent_id_col=Annotation.agent_id, user_id_cols=(Annotation.user_id,)),
                )
                .order_by(Annotation.updated_at.desc())
                .limit(limit)
            )
        ).all()
        cases.extend(
            _Case(
                case_kind=_case_kind(annotation.rating),
                source_kind=SOURCE_ASSIGNMENT_RUN,
                source_id=run.id,
                input_prompt=_run_prompt(run),
                answer=run.result_summary or "",
                comment=annotation.comment,
                agent_id=agent_id,
                user_id=run.user_id,
                rater_id=annotation.user_id,
                rating_id=annotation.id,
                trace_id=run.llm_trace_id,
                trace_ids=[run.llm_trace_id] if run.llm_trace_id else [],
            )
            for annotation, run in rows
        )
    if failed:
        runs = (
            await db.execute(
                select(AssignmentRun)
                .where(AssignmentRun.status == "failed", *scope)
                .order_by(AssignmentRun.created_at.desc())
                .limit(limit)
            )
        ).scalars()
        cases.extend(
            _Case(
                case_kind=CASE_TO_FIX,
                source_kind=SOURCE_ASSIGNMENT_RUN,
                source_id=run.id,
                input_prompt=_run_prompt(run),
                answer=run.error or "",
                agent_id=agent_id,
                user_id=run.user_id,
                trace_id=run.llm_trace_id,
                trace_ids=[run.llm_trace_id] if run.llm_trace_id else [],
            )
            for run in runs
        )
    return cases


def _graph_targets(graph: dict | None, agents: dict[uuid.UUID, None], flows: dict[uuid.UUID, None]) -> None:
    """Add the agents of *graph*'s agent nodes and the flows its ``flow.call`` nodes call."""
    wanted = {"agent": ("agent_id", agents), "flow.call": ("flow_id", flows)}
    for node in (graph or {}).get("nodes") or []:
        if not isinstance(node, dict) or node.get("kind") not in wanted:
            continue
        key, found = wanted[node["kind"]]
        try:
            found[uuid.UUID(str((node.get("config") or {}).get(key)))] = None
        except ValueError:
            continue


async def _flow_run_targets(db: AsyncSession, run: FlowRun) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """The agents and called flows whose content *run*'s answer and calls may
    carry: its graph's, then those of the sub-runs its ``flow.call`` nodes
    spawned, down to the runner's own depth cap."""
    from app.models.flow_run import FLOW_CALL_MAX_DEPTH

    agents: dict[uuid.UUID, None] = {}
    flows: dict[uuid.UUID, None] = {}
    level = [run]
    for _depth in range(FLOW_CALL_MAX_DEPTH + 1):
        for each in level:
            _graph_targets(each.graph, agents, flows)
            if each is not run:
                flows[each.flow_id] = None
        level = list(
            (
                await db.execute(
                    select(FlowRun).where(
                        FlowRun.parent_run_id.in_([each.id for each in level]), FlowRun.trigger_type == "flow_call"
                    )
                )
            ).scalars()
        )
        if not level:
            break
    # The run's own flow is the case's target, checked as such.
    flows.pop(run.flow_id, None)
    return list(agents), list(flows)


async def _flow_runs(
    db: AsyncSession, flow_id: uuid.UUID, *, failed: bool, completed: bool, since: datetime | None, limit: int
) -> list[_Case]:
    from app.services.evaluation.flow_subject import flow_run_trace_ids
    from app.services.flow_runner import final_step_output

    statuses = [s for s, wanted in ((FLOW_RUN_STATUS_FAILED, failed), (FLOW_RUN_STATUS_COMPLETED, completed)) if wanted]
    if not statuses:
        return []
    query = (
        select(FlowRun)
        .where(
            FlowRun.flow_id == flow_id,
            FlowRun.status.in_(statuses),
            # Its own runs only: never a parent's sub-run or an evaluation's.
            FlowRun.trigger_type != "flow_call",
            func.coalesce(FlowRun.trigger_context["source"].as_string(), "") != "evaluation",
            await consent_filter(db, flow_id_col=FlowRun.flow_id, user_id_cols=(FlowRun.user_id,)),
        )
        .order_by(FlowRun.created_at.desc())
        .limit(limit)
    )
    if since is not None:
        query = query.where(FlowRun.created_at >= since)
    cases = []
    for run in (await db.execute(query)).scalars():
        trace_ids, _settled = await flow_run_trace_ids(db, run)
        node_agent_ids, node_flow_ids = await _flow_run_targets(db, run)
        parameters = (run.trigger_payload or {}).get("payload")
        message = (run.trigger_payload or {}).get("message")
        cases.append(
            _Case(
                case_kind=CASE_TO_FIX if run.status == FLOW_RUN_STATUS_FAILED else CASE_NON_REGRESSION,
                source_kind=SOURCE_FLOW_RUN,
                source_id=run.id,
                # What ``flow_subject.entry_parameters`` reads back into the run.
                input_prompt=json.dumps(parameters)
                if isinstance(parameters, dict) and parameters
                else str(message or ""),
                answer=(run.error or "")
                if run.status == FLOW_RUN_STATUS_FAILED
                else await final_step_output(db, run.id),
                flow_id=flow_id,
                user_id=run.user_id,
                trace_ids=[uuid.UUID(t) for t in trace_ids],
                node_agent_ids=node_agent_ids,
                node_flow_ids=node_flow_ids,
            )
        )
    return cases


# ── Building ──────────────────────────────────────────────────────────────


def _interleaved(sources: list[list[_Case]]) -> list[_Case]:
    """The cases of every source, newest first within each, taken in turn, rated
    and unrated apart: a cap on the entries keeps some of each kind instead of
    filling up with the first source."""
    parts = [
        part
        for cases in sources
        for part in ([c for c in cases if c.rating_id is not None], [c for c in cases if c.rating_id is None])
        if part
    ]
    out: list[_Case] = []
    for index in range(max((len(part) for part in parts), default=0)):
        out += [part[index] for part in parts if index < len(part)]
    return out


def _judge_instructions(case: _Case) -> str | None:
    """A case to fix: what went wrong, every value quarantined as data."""
    if case.case_kind != CASE_TO_FIX:
        return None
    from app.services.content_safety import wrap_untrusted

    parts = ["This case comes from production, where the answer below was rejected."]
    if case.comment:
        parts.append("The rating's comment:\n" + wrap_untrusted(case.comment, "rating comment"))
    if case.answer:
        parts.append("The rejected answer (or the run's error):\n" + wrap_untrusted(case.answer, "rejected answer"))
    parts.append("Score the new answer on whether it fixes what was rejected.")
    return "\n\n".join(parts)


async def _target(
    db: AsyncSession, target_type: str, target_id: uuid.UUID
) -> tuple[str, uuid.UUID | None, uuid.UUID | None]:
    """The target's display name, agent and flow; refused when out of the supervision scope."""
    if target_type == "flow":
        flow = await db.get(Flow, target_id)
        if flow is None or not await in_supervision_scope(db, "flow", flow.id):
            raise DatasetBuildError("The target is not available.")
        return flow.name, None, flow.id
    agent_id = target_id
    name_suffix = ""
    if target_type == "assignment":
        assignment = await db.get(Assignment, target_id)
        if assignment is None:
            raise DatasetBuildError("The target is not available.")
        agent_id, name_suffix = assignment.agent_id, f" / {assignment.name}"
    elif target_type != "agent":
        raise DatasetBuildError("Unknown target type.")
    agent = await db.get(Agent, agent_id)
    if agent is None or not await in_supervision_scope(db, "agent", agent.id):
        raise DatasetBuildError("The target is not available.")
    return agent.name + name_suffix, agent.id, None


async def _recordings(db: AsyncSession, trace_ids: Sequence[uuid.UUID]) -> list[dict[str, Any]]:
    from app.services.health.sandbox.replay import load_recorded_calls

    return [dataclasses.asdict(call) for call in await load_recorded_calls(db, trace_ids)] if trace_ids else []


async def build_dataset_from_traces(
    db: AsyncSession,
    *,
    target_type: TargetType,
    target_id: uuid.UUID,
    max_entries: int = 50,
    negative: bool = True,
    failed: bool = True,
    positive: bool = True,
    since: datetime | None = None,
    dataset_id: uuid.UUID | None = None,
) -> DatasetBuildResult:
    """Build (or extend, with *dataset_id*) a Platform Health dataset from *target*'s production work.

    *target_type* ``agent`` reads its rated answers and its failed assignment
    runs, ``assignment`` the same restricted to one assignment's runs, ``flow``
    its failed and completed runs. Newest first, at most *max_entries* new
    entries; a source already in the dataset is not added twice. Flushes, the
    caller commits. Raises :class:`DatasetBuildError` (generic) before reading
    anything when the target or the dataset is not available.
    """
    if not 1 <= max_entries <= MAX_ENTRIES:
        raise DatasetBuildError(f"max_entries must be between 1 and {MAX_ENTRIES}.")
    name, agent_id, flow_id = await _target(db, target_type, target_id)
    if dataset_id is not None:
        dataset = await db.get(Dataset, dataset_id, with_for_update=True)
        if dataset is None or not dataset.concierge_built:
            raise DatasetBuildError("The dataset is not available.")
    else:
        dataset = Dataset(
            name=f"Platform Health - {name}"[:200],
            description=f"Built by Platform Health from production on {datetime.now(UTC):%Y-%m-%d}.",
            family=DATASET_FAMILY,
            concierge_built=True,
        )
        db.add(dataset)
        await db.flush()

    ratings = _rated(negative, positive)
    sources: list[list[_Case]] = []
    if flow_id is not None:
        sources.append(
            await _flow_runs(db, flow_id, failed=failed, completed=positive, since=since, limit=max_entries * 2)
        )
    elif agent_id is not None:
        if target_type == "agent":
            sources.append(await _rated_messages(db, agent_id, ratings, since, max_entries * 2))
        sources.append(
            await _assignment_runs(
                db,
                agent_id=agent_id,
                assignment_id=target_id if target_type == "assignment" else None,
                ratings=ratings,
                failed=failed,
                since=since,
                limit=max_entries * 2,
            )
        )
    candidates = _interleaved(sources)

    present = set(
        (
            await db.execute(
                select(HealthDatasetEntrySource.source_kind, HealthDatasetEntrySource.source_id).where(
                    HealthDatasetEntrySource.dataset_id == dataset.id
                )
            )
        ).tuples()
    )
    position = await db.scalar(select(func.max(DatasetEntry.position)).where(DatasetEntry.dataset_id == dataset.id))
    position = -1 if position is None else position
    result = DatasetBuildResult(dataset_id=dataset.id)
    added = 0
    for case in candidates:
        if added >= max_entries:
            break
        key = (case.source_kind, case.source_id)
        if key in present:
            result.skip("already_in_dataset")
            continue
        present.add(key)
        if not case.input_prompt.strip():
            result.skip("no_input")
            continue
        if len(case.input_prompt) > MAX_TEXT_CHARS or len(case.answer) > MAX_TEXT_CHARS:
            result.skip("too_long")
            continue
        # A flow run's agent nodes and called flows: each one's consent too, beside the flow's.
        node_refused = False
        nodes = [{"agent_id": a} for a in case.node_agent_ids] + [{"flow_id": f} for f in case.node_flow_ids]
        for node in nodes:
            if not await content_readable(
                db, **node, user_id=case.user_id, user_ids=[case.rater_id] if case.rater_id else ()
            ):
                node_refused = True
                break
        if node_refused:
            result.skip("not_consented")
            continue
        try:
            await require_content_read(
                db,
                kind="trace",
                ref_id=case.trace_id or case.source_id,
                agent_id=case.agent_id,
                flow_id=case.flow_id,
                user_id=case.user_id,
                user_ids=[case.rater_id] if case.rater_id else (),
            )
        except ConsentDeniedError:
            result.skip("not_consented")
            continue
        recordings = await _recordings(db, case.trace_ids)
        if len(json.dumps(recordings, default=str)) > MAX_RECORDINGS_CHARS:
            result.skip("recordings_too_large")
            continue
        position += 1
        entry = DatasetEntry(
            dataset_id=dataset.id,
            position=position,
            description=f"{'Case to fix' if case.case_kind == CASE_TO_FIX else 'Non-regression'} ({case.source_kind})",
            input_prompt=case.input_prompt,
            expected_answer=case.answer if case.case_kind == CASE_NON_REGRESSION else "",
            evaluation_prompt=_judge_instructions(case),
        )
        db.add(entry)
        await db.flush()
        db.add(
            HealthDatasetEntrySource(
                entry_id=entry.id,
                dataset_id=dataset.id,
                case_kind=case.case_kind,
                source_kind=case.source_kind,
                source_id=case.source_id,
                source_trace_id=case.trace_id,
                source_rating_id=case.rating_id,
                source_user_id=case.user_id,
                source_rater_id=case.rater_id,
                source_agent_id=case.agent_id,
                source_flow_id=case.flow_id,
                source_node_agent_ids=list(case.node_agent_ids),
                source_node_flow_ids=list(case.node_flow_ids),
                recorded_calls=recordings,
            )
        )
        result.added[case.case_kind] += 1
        added += 1
    await db.flush()
    return result


async def entry_recordings(db: AsyncSession, entry_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[Any]]:
    """Each Platform Health-built entry's recorded tool calls, as level 2's ``RecordedCall``."""
    from app.services.health.sandbox.replay import RecordedCall

    ids = list(entry_ids)
    if not ids:
        return {}
    known = {f.name for f in dataclasses.fields(RecordedCall)}
    rows = await db.execute(
        select(HealthDatasetEntrySource.entry_id, HealthDatasetEntrySource.recorded_calls).where(
            HealthDatasetEntrySource.entry_id.in_(ids)
        )
    )
    return {
        entry_id: [RecordedCall(**{k: v for k, v in call.items() if k in known}) for call in calls or []]
        for entry_id, calls in rows.all()
    }


# ── Withdrawal ────────────────────────────────────────────────────────────


async def _stop_runs_reading(db: AsyncSession, entry_ids: list[uuid.UUID]) -> None:
    """Stop the runs still to score the withdrawn entries: a run in flight would
    otherwise score them, or keep their content, after the revocation."""
    from app.models.dataset import EvaluationDataset

    dataset_ids = select(DatasetEntry.dataset_id).where(DatasetEntry.id.in_(entry_ids)).scalar_subquery()
    runs = (
        await db.execute(
            select(EvaluationRun)
            .join(EvaluationDataset, EvaluationDataset.evaluation_id == EvaluationRun.evaluation_id)
            .where(EvaluationDataset.dataset_id.in_(dataset_ids), EvaluationRun.status.in_(("pending", "running")))
            .with_for_update(of=EvaluationRun)
        )
    ).scalars()
    for run in runs:
        run.status = "cancelled"
        run.error = WITHDRAWN_RUN_ERROR
        run.completed_at = datetime.now(UTC)


async def _withdraw(db: AsyncSession, entry_ids: list[uuid.UUID]) -> int:
    if not entry_ids:
        return 0
    await _stop_runs_reading(db, entry_ids)
    version_rows = (
        await db.execute(
            select(DatasetVersionEntry.id, DatasetVersionEntry.version_id).where(
                DatasetVersionEntry.entry_id.in_(entry_ids)
            )
        )
    ).all()
    version_entry_ids = [row.id for row in version_rows]
    scored_on = or_(
        EvaluationResult.entry_id.in_(entry_ids),
        EvaluationResult.version_entry_id.in_(version_entry_ids) if version_entry_ids else False,
    )
    lost = (
        await db.execute(
            select(EvaluationResult.run_id, func.count()).where(scored_on).group_by(EvaluationResult.run_id)
        )
    ).all()
    await db.execute(delete(EvaluationResult).where(scored_on))
    if version_entry_ids:
        await db.execute(delete(DatasetVersionEntry).where(DatasetVersionEntry.id.in_(version_entry_ids)))
        # Each snapshot says how many entries it still holds.
        removed: dict[uuid.UUID, int] = {}
        for row in version_rows:
            removed[row.version_id] = removed.get(row.version_id, 0) + 1
        for version_id, count in removed.items():
            await db.execute(
                update(DatasetVersion)
                .where(DatasetVersion.id == version_id)
                .values(entry_count=func.greatest(DatasetVersion.entry_count - count, 0))
            )
    await db.execute(delete(DatasetEntry).where(DatasetEntry.id.in_(entry_ids)))
    # A run that lost results no longer measures what its headlines say: they
    # read "not measured", and why, never a complete verdict.
    for run_id, count in lost:
        run = await db.get(EvaluationRun, run_id, with_for_update=True)
        if run is None:
            continue
        details = dict(run.headline_details or {})
        details["withdrawn_entries"] = int(details.get("withdrawn_entries", 0)) + int(count)
        run.headline_details = details
        run.score = run.quality_score = run.performance_score = None
    return len(entry_ids)


async def withdraw_entries(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    flow_id: uuid.UUID | None = None,
) -> int:
    """Delete Platform Health-built entries derived from *user_id*'s content (as the
    conversation's user or the rater), or from *agent_id* / *flow_id*. In the
    caller's transaction; returns how many entries went."""
    matches = []
    if user_id is not None:
        matches += [
            HealthDatasetEntrySource.source_user_id == user_id,
            HealthDatasetEntrySource.source_rater_id == user_id,
        ]
    if agent_id is not None:
        matches.append(HealthDatasetEntrySource.source_agent_id == agent_id)
        # A flow run whose agent node it ran: its content is in the entry.
        matches.append(HealthDatasetEntrySource.source_node_agent_ids.contains([agent_id]))
    if flow_id is not None:
        matches.append(HealthDatasetEntrySource.source_flow_id == flow_id)
        # A flow run that called it: the called flow's content is in the entry.
        matches.append(HealthDatasetEntrySource.source_node_flow_ids.contains([flow_id]))
    if not matches:
        return 0
    entry_ids = list((await db.execute(select(HealthDatasetEntrySource.entry_id).where(or_(*matches)))).scalars())
    return await _withdraw(db, entry_ids)


async def withdraw_out_of_scope(db: AsyncSession) -> int:
    """Delete the entries whose source target left a narrowed supervision scope."""
    from app.services.health.settings import load_health_settings

    settings = await load_health_settings(db)
    if settings.scope_mode != "selected":
        return 0
    outside = or_(
        HealthDatasetEntrySource.source_agent_id.not_in(settings.scope_agent_ids or [uuid.UUID(int=0)]),
        HealthDatasetEntrySource.source_flow_id.not_in(settings.scope_flow_ids or [uuid.UUID(int=0)]),
        # A flow run's agent node that left the scope takes the entry with it.
        ~HealthDatasetEntrySource.source_node_agent_ids.contained_by(list(settings.scope_agent_ids)),
        ~HealthDatasetEntrySource.source_node_flow_ids.contained_by(list(settings.scope_flow_ids)),
    )
    entry_ids = list((await db.execute(select(HealthDatasetEntrySource.entry_id).where(outside))).scalars())
    return await _withdraw(db, entry_ids)


async def withdraw_for_revocation(db: AsyncSession, revocation: Revocation) -> None:
    """Level 1's revocation handler: an opt-out, a revoked opt-in or a narrowed scope."""
    if revocation.kind == "scope_narrowed":
        removed = await withdraw_out_of_scope(db)
    else:
        removed = await withdraw_entries(
            db, user_id=revocation.user_id, agent_id=revocation.agent_id, flow_id=revocation.flow_id
        )
    if removed:
        logger.info("Platform Health datasets: %d entr(y/ies) withdrawn on %s", removed, revocation.kind)


# Run on every revocation, in the revoking transaction (level 1).
register_revocation_handler(withdraw_for_revocation)
