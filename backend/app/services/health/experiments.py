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

"""Platform Health experiments: draft a variant, measure it, file what it earned (#8259).

* **Draft** (Quality Analyst; Flow Lab for a flow): a variant for a finding,
  checked by the kind of its change (:class:`ExperimentKind`: level 6's text
  kinds - persona, skill content, assignment prompt - through
  ``variants.check_variant``; level 7 registers the flow kinds) and stored
  with the base it was written against.
* **Run** (Experimenter, the only launcher): the budget is read before the
  launch and the dataset and trials are sized to it; the dataset is built from
  the agent's consented production cases (level 4); two sandbox copies of the
  agent (level 2) - the current configuration and the variant - are evaluated
  on it with the same model (level 4). Nothing here writes the agent, the skill
  or the assignment.
* **Settle** (Platform Health's tick, or a read): once both arms ended, the
  comparison (quality with its 2-point noise margin, cost, latency) and the
  writes the sandbox answered with a model are recorded. A run stopped early
  gives ``incomplete``, never a verdict. The spend is the evaluation's own to
  charge (``evaluations.settle_health_evaluations``).
* **File** (Experimenter): a proposal (level 3) built from the row - the
  verdict is the measurement's, never an argument - or ``unmeasured`` with the
  reason the experiment was not measured, stated as such.

The base is re-read before the launch and before filing: a target a human
edited meanwhile marks the experiment ``stale``, and nothing is filed over it.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import func, select

from app.core.utils import format_exception_message
from app.models.health_experiment import (
    CHANGE_TYPES,
    OPEN_STATUSES,
    STATUS_COMPLETED,
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_INCOMPLETE,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_STALE,
    HealthExperiment,
)
from app.models.health_finding import (
    STATUS_DISMISSED,
    STATUS_EXPERIMENTING,
    STATUS_PROPOSED,
    STATUS_RESOLVED,
    HealthFinding,
)
from app.services.health import quality_deps as deps
from app.services.health.flow_lab_access import EXPERIMENT_COPY_PREFIX

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent import Agent

logger = logging.getLogger(__name__)

MIN_CASES = 5
DEFAULT_CASES = 20
MAX_CASES = 50
MAX_TRIALS = 3
#: Days of production the dataset is built from.
DATASET_DAYS = 30
#: Tokens per case when the agent has no measured run to estimate from.
DEFAULT_CASE_TOKENS = 20_000
#: The judge's share on top of the subject's own tokens.
JUDGE_SHARE = 0.25
#: A neutral result is still worth filing when it costs this much less per run.
COST_GAIN_PCT = 5.0

#: Why an experiment was not measured, as stored on the row and stated on its proposal.
UNMEASURED_REASONS = frozenset(
    {"budget", "no_dataset", "no_consent", "evaluation_unavailable", "budget_reached_mid_run"}
)


class ExperimentError(ValueError):
    """A request the experiment store refuses, with a message the caller may read."""


# ── Kinds ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DraftSpec:
    """What a kind makes of a draft request: the row's target, base and variant."""

    #: Level 3's target type of the proposal this change files (its ``TARGET_OF``).
    target_type: str
    #: The agent an evaluation runs, when the subject is an agent.
    subject_agent_id: uuid.UUID | None
    base_text: str
    base_version: int | None
    #: The variant as text (persona, skill, prompt), "" for a structured one.
    variant_text: str
    #: A structured variant (e.g. a flow kind's ``{"sandbox_flow_id", "graph"}``).
    variant: dict[str, Any]
    summary: str


class ExperimentKind(Protocol):
    """How one ``change_type`` is drafted, kept current and measured.

    Level 6 registers ``persona``, ``skill`` and ``assignment_prompt``; level 7
    registers its flow types through :func:`register_experiment_kind`, so the
    run, settle and file steps below stay the same for every kind.
    """

    async def prepare_draft(
        self, db: AsyncSession, *, finding: HealthFinding, target_id: uuid.UUID, payload: dict[str, Any]
    ) -> DraftSpec:
        """Check a draft request for *finding* and read the base it changes; ``ExperimentError`` / ``VariantError``."""
        ...

    async def check_stale(self, db: AsyncSession, experiment: HealthExperiment) -> bool:
        """Whether the target no longer holds the base the variant was drafted on."""
        ...

    async def dataset_target(self, db: AsyncSession, experiment: HealthExperiment) -> tuple[str, uuid.UUID] | None:
        """``(target_type, target_id)`` level 4 builds the dataset from; ``None``: not measurable yet."""
        ...

    async def subjects(
        self, db: AsyncSession, experiment: HealthExperiment, *, created_for: str, author_agent_id: uuid.UUID | None
    ) -> tuple[deps.Subject, deps.Subject]:
        """The baseline and variant arms, as sandbox subjects (level 2): never a production row."""
        ...

    def proposal_content(self, experiment: HealthExperiment) -> dict[str, Any]:
        """``base_content`` / ``proposed_content`` / ``change`` of the proposal (level 3's ``ProposalSpec``)."""
        ...


_KINDS: dict[str, ExperimentKind] = {}


def register_experiment_kind(change_type: str, kind: ExperimentKind) -> None:
    """Make *change_type* drafted and measured through *kind* (one kind per type)."""
    if change_type not in CHANGE_TYPES:
        raise ValueError(f"Unknown change type {change_type!r}: add it to CHANGE_TYPES and the migration first.")
    _KINDS[change_type] = kind


def experiment_kind(change_type: str) -> ExperimentKind:
    kind = _KINDS.get(change_type)
    if kind is None:
        raise ExperimentError(f"change_type must be one of: {', '.join(sorted(_KINDS))}.")
    return kind


# ── Draft ────────────────────────────────────────────────────────────────────


async def draft(
    db: AsyncSession,
    *,
    finding: HealthFinding,
    change_type: str,
    target_id: uuid.UUID,
    payload: dict[str, Any],
    author_agent_id: uuid.UUID | None,
) -> tuple[HealthExperiment, bool]:
    """Store a checked variant for *finding* (the caller commits): ``(experiment, created)``.

    *payload* is the kind's: ``{"text", "summary"}`` for a text variant. An
    open experiment of the same finding, change and target is returned as is:
    one variant under measurement at a time.
    """
    if finding.status in (STATUS_DISMISSED, STATUS_RESOLVED):
        raise ExperimentError("This finding is closed: record it again if the pattern is back.")
    spec = await experiment_kind(change_type).prepare_draft(db, finding=finding, target_id=target_id, payload=payload)
    existing = await db.scalar(
        select(HealthExperiment).where(
            HealthExperiment.finding_id == finding.id,
            HealthExperiment.change_type == change_type,
            HealthExperiment.target_id == target_id,
            HealthExperiment.status.in_(OPEN_STATUSES),
        )
    )
    if existing is not None:
        return existing, False
    experiment = HealthExperiment(
        finding_id=finding.id,
        change_type=change_type,
        target_type=spec.target_type,
        target_id=target_id,
        subject_agent_id=spec.subject_agent_id,
        base_text=spec.base_text,
        base_version=spec.base_version,
        variant_text=spec.variant_text,
        variant=spec.variant,
        variant_summary=spec.summary,
        status=STATUS_DRAFT,
        author_agent_id=author_agent_id,
    )
    db.add(experiment)
    if finding.status not in (STATUS_PROPOSED,):
        finding.status = STATUS_EXPERIMENTING
    await db.flush()
    return experiment, True


# ── Sizing ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Sizing:
    cases: int
    trials: int
    case_tokens: int
    case_cost_usd: Decimal | None
    tokens: int
    cost_usd: Decimal

    def as_json(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            "trials": self.trials,
            "case_tokens": self.case_tokens,
            "case_cost_usd": str(self.case_cost_usd) if self.case_cost_usd is not None else None,
            "tokens": self.tokens,
            "cost_usd": str(self.cost_usd),
        }


def size_to_budget(
    budget: deps.Budget,
    *,
    requested_cases: int,
    trials: int,
    case_tokens: float | None,
    case_cost_usd: float | None,
) -> Sizing:
    """The cases and trials the budget affords: two arms, each case *trials* times, plus the judge.

    Pure (unit-tested). ``cases`` below ``MIN_CASES`` means: do not launch. A
    cost cap with no measured cost per run to size against launches nothing.
    """
    trials = max(1, min(int(trials or 1), MAX_TRIALS))
    requested = max(1, min(int(requested_cases or DEFAULT_CASES), MAX_CASES))
    per_case_tokens = max(1, math.ceil((case_tokens or DEFAULT_CASE_TOKENS) * 2 * trials * (1 + JUDGE_SHARE)))
    per_case_cost = (
        Decimal(str(case_cost_usd)) * Decimal(2 * trials) * Decimal(str(1 + JUDGE_SHARE))
        if case_cost_usd is not None
        else None
    )
    cases = requested
    if budget.tokens is not None:
        cases = min(cases, max(0, budget.tokens) // per_case_tokens)
    if budget.cost_usd is not None:
        if per_case_cost is None:
            cases = 0
        elif per_case_cost:
            cases = min(cases, int(max(Decimal(0), budget.cost_usd) // per_case_cost))
    cost = per_case_cost * cases if per_case_cost is not None else Decimal(0)
    return Sizing(
        cases=int(cases),
        trials=trials,
        case_tokens=per_case_tokens,
        case_cost_usd=per_case_cost,
        tokens=int(cases) * per_case_tokens,
        cost_usd=cost,
    )


# ── Run ──────────────────────────────────────────────────────────────────────


def _skip(experiment: HealthExperiment, reason: str) -> HealthExperiment:
    experiment.status = STATUS_SKIPPED
    experiment.status_reason = reason
    experiment.completed_at = datetime.now(UTC)
    return experiment


async def run(
    db: AsyncSession,
    *,
    experiment: HealthExperiment,
    content_readable: bool,
    signals: dict[str, Any],
    requested_cases: int,
    author_agent_id: uuid.UUID | None,
    cycle_id: uuid.UUID | None = None,
) -> HealthExperiment:
    """Launch *experiment* (a draft) or say why it is not launched (the caller commits;
    level 4 commits the launch itself).

    *signals* are the target's current window (``diagnosis.summarize_window``):
    its tokens and cost per run size the experiment (defaults without them).
    *cycle_id* is the Platform Health cycle the spend is reserved on and charged to:
    without one nothing is launched, since nothing could book its spend.
    """
    if experiment.status != STATUS_DRAFT:
        raise ExperimentError(f"This experiment is {experiment.status}: only a draft is launched.")
    if cycle_id is None:
        raise ExperimentError("An experiment runs inside a Concierge cycle, which books what it spends.")
    kind = experiment_kind(experiment.change_type)
    if await kind.check_stale(db, experiment):
        experiment.status = STATUS_STALE
        await db.flush()
        return experiment
    if not content_readable:
        # The dataset is the target's conversations: no opt-in, no measurement.
        return _skip(experiment, "no_consent")
    target = await kind.dataset_target(db, experiment)
    if target is None:
        return _skip(experiment, "evaluation_unavailable")

    budget = await deps.budget_left(db, cycle_id)
    if budget.refusal:
        return _skip(experiment, "budget")
    sizing = size_to_budget(
        budget,
        requested_cases=requested_cases,
        trials=deps.MAX_TRIALS,
        case_tokens=signals.get("tokens_per_run"),
        case_cost_usd=signals.get("cost_per_run"),
    )
    experiment.sizing = sizing.as_json()
    if sizing.cases < MIN_CASES:
        return _skip(experiment, "budget")

    built = await deps.build_dataset(
        db,
        target_type=target[0],
        target_id=target[1],
        since=datetime.now(UTC) - timedelta(days=DATASET_DAYS),
        max_entries=sizing.cases,
    )
    if built.dataset_id is None or built.entries < MIN_CASES:
        return _skip(experiment, "no_dataset")

    try:
        baseline, variant = await kind.subjects(
            db, experiment, created_for=f"{EXPERIMENT_COPY_PREFIX}{experiment.id}", author_agent_id=author_agent_id
        )
        # The evaluation reserves the sizing on the cycle and holds it until it
        # charges what its runs spent (``evaluations.settle_health_evaluations``).
        evaluation_id, baseline_runs, variant_runs = await deps.launch(
            db,
            dataset_id=built.dataset_id,
            name=f"Concierge experiment {experiment.id}",
            baseline=baseline,
            variant=variant,
            cycle_id=cycle_id,
            tokens=sizing.tokens,
            cost_usd=sizing.cost_usd,
        )
    except deps.BudgetUnavailableError:
        experiment.dataset_id = built.dataset_id
        return _skip(experiment, "budget")
    except Exception as exc:
        # Refused or failed, nothing measures and nothing was reserved; the reason is logged.
        log = logger.info if isinstance(exc, deps.EvaluationUnavailableError) else logger.warning
        log("Platform Health experiment %s not launched: %s", experiment.id, format_exception_message(exc))
        experiment.dataset_id = built.dataset_id
        return _skip(experiment, "evaluation_unavailable")
    experiment.dataset_id = built.dataset_id
    experiment.evaluation_id = evaluation_id
    experiment.baseline_copy_id = baseline.copy_id
    experiment.variant_copy_id = variant.copy_id
    experiment.baseline_run_ids = list(baseline_runs)
    experiment.variant_run_ids = list(variant_runs)
    experiment.sizing = {**experiment.sizing, "dataset_entries": built.entries}
    experiment.status = STATUS_RUNNING
    experiment.launched_at = datetime.now(UTC)
    await db.flush()
    return experiment


# ── Settle ───────────────────────────────────────────────────────────────────


async def settle(db: AsyncSession, experiment: HealthExperiment) -> HealthExperiment:
    """Record the measurement of a running experiment whose arms ended (the caller commits)."""
    if experiment.status != STATUS_RUNNING:
        return experiment
    try:
        compared = await deps.compare(
            db, baseline_run_ids=list(experiment.baseline_run_ids), variant_run_ids=list(experiment.variant_run_ids)
        )
    except Exception as exc:
        logger.warning(
            "Platform Health experiment %s comparison failed: %s", experiment.id, format_exception_message(exc)
        )
        return experiment
    if not compared.finished:
        return experiment

    simulated: dict[str, Any] = {"baseline": 0, "variant": 0, "per_case": {}}
    for arm, copy_id in (("baseline", experiment.baseline_copy_id), ("variant", experiment.variant_copy_id)):
        if copy_id is None:
            continue
        per_case = await deps.simulated_writes(db, copy_id=copy_id, since=experiment.launched_at)
        simulated[arm] = sum(per_case.values())
        simulated["per_case"][arm] = per_case
    simulated["total"] = simulated["baseline"] + simulated["variant"]

    experiment.result = {
        "quality_metric": compared.quality_metric,
        "baseline": compared.baseline,
        "candidate": compared.candidate,
        "delta": compared.delta,
        "verdict": None if compared.incomplete else compared.verdict,
        "cost_usd": compared.cost_usd,
        "latency_s": compared.latency_s,
        "simulated_writes": simulated,
        "base_chars": len(experiment.base_text or ""),
        "variant_chars": len(experiment.variant_text or ""),
    }
    if compared.incomplete or compared.verdict is None:
        experiment.status, experiment.status_reason = _NO_VERDICT.get(
            compared.reason, (STATUS_FAILED, "evaluation_unavailable")
        )
    else:
        experiment.status = STATUS_COMPLETED
    experiment.completed_at = datetime.now(UTC)
    await db.flush()
    return experiment


#: What an experiment with no verdict records, by level 4's reason; any other
#: reason (a failed arm, an unmeasured metric) is ``failed`` / ``evaluation_unavailable``.
_NO_VERDICT: dict[str | None, tuple[str, str]] = {
    "budget": (STATUS_INCOMPLETE, "budget_reached_mid_run"),
    "consent": (STATUS_INCOMPLETE, "no_consent"),
}


async def settle_running_experiments(db: AsyncSession, *, limit: int = 50) -> int:
    """Settle every running experiment whose arms ended, whoever reads it next; the number settled.

    Platform Health's tick: an experiment outlives the run that launched it. Commits.
    """
    running = (
        (
            await db.execute(
                select(HealthExperiment)
                .where(HealthExperiment.status == STATUS_RUNNING)
                .order_by(HealthExperiment.launched_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    settled = 0
    for experiment in running:
        await settle(db, experiment)
        settled += experiment.status != STATUS_RUNNING
        await db.commit()
    return settled


# ── File ─────────────────────────────────────────────────────────────────────


def filing_verdict(experiment: HealthExperiment) -> tuple[str | None, str | None]:
    """``(experiment_verdict, refusal)`` for filing *experiment* (pure: unit-tested).

    A completed experiment files its measured verdict when it earned it:
    ``progression``, or ``neutral`` with a shorter text or a lower cost per run;
    a regression or a neutral result with no gain is refused. An experiment that
    was not measured files ``unmeasured``, stating why; a stale or running one
    files nothing.
    """
    status = experiment.status
    if status == STATUS_COMPLETED:
        result = experiment.result or {}
        verdict = result.get("verdict")
        if verdict == "progression":
            return "progression", None
        if verdict == "neutral":
            # A structured variant (a flow graph) has no text to be shorter by.
            variant_chars = result.get("variant_chars") or 0
            shorter = 0 < variant_chars < (result.get("base_chars") or 0)
            cost = result.get("cost_usd") or {}
            base_cost, variant_cost = cost.get("baseline"), cost.get("variant")
            cheaper = (
                isinstance(base_cost, int | float)
                and isinstance(variant_cost, int | float)
                and base_cost > 0
                and 100.0 * (base_cost - variant_cost) / base_cost >= COST_GAIN_PCT
            )
            if shorter or cheaper:
                return "neutral", None
            return None, "The variant measured no better, shorter or cheaper than the current text: nothing to propose."
        return None, "The variant measured a regression: nothing to propose."
    if status in (STATUS_SKIPPED, STATUS_INCOMPLETE, STATUS_FAILED):
        if (experiment.status_reason or "") in UNMEASURED_REASONS:
            return "unmeasured", None
        return None, "This experiment ended without a measurement or a stated reason."
    if status == STATUS_STALE:
        return None, "The target changed after the variant was drafted: draft it again on the current text."
    return None, f"This experiment is {status}: wait for its measurement."


async def propose(
    db: AsyncSession,
    *,
    experiment: HealthExperiment,
    finding: HealthFinding,
    author_agent: Agent,
    content_readable: bool = True,
    evidence: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, bool]:
    """File the proposal *experiment* earned (the caller commits): ``(proposal id, created)``.

    A finding drawn from conversations files only while they are still
    readable (*content_readable*: the target's opt-in); *evidence* is its refs
    as the consent rows allow them now (the finding's own by default).
    """
    if experiment.proposal_id is not None:
        return experiment.proposal_id, False
    verdict, refusal = filing_verdict(experiment)
    if refusal:
        raise ExperimentError(refusal)
    if finding.content_evidence and not content_readable:
        raise ExperimentError(
            "This finding was drawn from conversations the Concierge may no longer read: nothing is filed."
        )
    kind = experiment_kind(experiment.change_type)
    if await kind.check_stale(db, experiment):
        experiment.status = STATUS_STALE
        await db.flush()
        raise ExperimentError("The target changed after the variant was drafted: draft it again on the current text.")

    result = experiment.result or {}
    evidence = evidence if evidence is not None else (finding.evidence or {})
    evidence_refs = [
        *({"kind": "trace", "id": i} for i in evidence.get("trace_ids") or []),
        *({"kind": "rating", "id": i} for i in evidence.get("rating_ids") or []),
        *({"kind": "assignment_run", "id": i} for i in evidence.get("run_ids") or []),
    ]
    experiment_refs = [
        *({"kind": "evaluation_run", "id": str(i)} for i in experiment.baseline_run_ids or []),
        *({"kind": "evaluation_run", "id": str(i)} for i in experiment.variant_run_ids or []),
        *(
            {"kind": "sandbox_copy", "id": str(i)}
            for i in (experiment.baseline_copy_id, experiment.variant_copy_id)
            if i is not None
        ),
    ]
    measured = verdict != "unmeasured"
    proposal_id, created = await deps.file_proposal(
        db,
        author_agent=author_agent,
        proposal_type=experiment.change_type,
        target_id=experiment.target_id,
        **kind.proposal_content(experiment),
        summary=experiment.variant_summary,
        finding={
            "fingerprint": finding.fingerprint,
            "finding_id": str(finding.id),
            "pattern_kind": finding.pattern_kind,
            "pattern_key": finding.pattern_key,
            "hypothesis": finding.hypothesis,
            "counts": evidence.get("counts") or {},
        },
        evidence_refs=evidence_refs,
        experiment_refs=experiment_refs,
        experiment={
            "experiment_id": str(experiment.id),
            "status": experiment.status,
            "unmeasured_reason": None if measured else experiment.status_reason,
            "quality_metric": result.get("quality_metric"),
            "baseline": result.get("baseline"),
            "candidate": result.get("candidate"),
            "delta": result.get("delta"),
            "cost_usd": result.get("cost_usd"),
            "latency_s": result.get("latency_s"),
            "simulated_writes": (result.get("simulated_writes") or {}).get("total", 0) if measured else None,
            "cases": (experiment.sizing or {}).get("dataset_entries"),
            "trials": (experiment.sizing or {}).get("trials"),
        },
        experiment_verdict=verdict,
        evaluation_id=experiment.evaluation_id if measured else None,
        baseline_run_id=(experiment.baseline_run_ids or [None])[0] if measured else None,
        quality_metric=result.get("quality_metric") or "score",
        baseline_score=result.get("baseline") if measured else None,
        base_version_number=experiment.base_version,
    )
    experiment.proposal_id = proposal_id
    finding.status = STATUS_PROPOSED
    finding.proposal_id = proposal_id
    await db.flush()
    return proposal_id, created


def experiment_payload(experiment: HealthExperiment, *, content: bool = True) -> dict[str, Any]:
    """One experiment as the tools serve it. The variant text is Platform Health's own; no source content.

    Without *content* access its summary, written from the finding's evidence,
    is redacted as the finding's hypothesis is (``access.redact_finding``).
    """
    payload = {
        "id": str(experiment.id),
        "finding_id": str(experiment.finding_id),
        "change_type": experiment.change_type,
        "target_type": experiment.target_type,
        "target_id": str(experiment.target_id),
        "status": experiment.status,
        "status_reason": experiment.status_reason,
        "variant_summary": experiment.variant_summary,
        # A structured variant's refs; its body (a flow graph) is read where it lives.
        "variant": {k: v for k, v in (experiment.variant or {}).items() if k != "graph"},
        "base_chars": len(experiment.base_text or ""),
        "variant_chars": len(experiment.variant_text or ""),
        "sizing": {k: v for k, v in (experiment.sizing or {}).items() if k != "reservation"},
        "result": experiment.result or None,
        "evaluation_id": str(experiment.evaluation_id) if experiment.evaluation_id else None,
        "proposal_id": str(experiment.proposal_id) if experiment.proposal_id else None,
        "created_at": experiment.created_at.isoformat() if experiment.created_at else None,
        "completed_at": experiment.completed_at.isoformat() if experiment.completed_at else None,
    }
    if not content:
        payload["variant_summary"] = ""
        payload["redacted"] = True
    return payload


async def status_counts(
    db: AsyncSession,
    *,
    since: datetime,
    until: datetime | None = None,
    target: tuple[str, uuid.UUID] | None = None,
) -> dict[str, int]:
    """Experiments created in ``[since, until)`` by status, for a cycle's digest (level 8).

    Every status is counted as itself: ``skipped`` and ``incomplete`` are never
    folded into ``completed``.
    """
    until = until or datetime.now(UTC)
    query = select(HealthExperiment.status, func.count()).where(
        HealthExperiment.created_at >= since, HealthExperiment.created_at < until
    )
    if target is not None:
        query = query.where(HealthExperiment.target_type == target[0], HealthExperiment.target_id == target[1])
    rows = (await db.execute(query.group_by(HealthExperiment.status))).all()
    return {status: int(count) for status, count in rows}
