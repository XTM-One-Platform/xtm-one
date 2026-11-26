# SPDX-License-Identifier: Apache-2.0

"""Evaluation models – first-class multi-agent benchmark system.

An Evaluation defines a benchmark that tests multiple agents across multiple
LLM models using linked Datasets.  Each trigger event creates an agent x model
matrix of EvaluationRun records.  Each run iterates dataset entries, executes
the agent, and scores the output via an LLM judge.

Entity hierarchy:
    Evaluation
      ├── EvaluationAgent (join table → Agent, or → Flow: the evaluation's subjects)
      ├── EvaluationDataset (join table → Dataset → DatasetEntry)
      └── EvaluationRun (one per agent × model, one per flow)
              └── EvaluationResult (one per dataset entry)

A subject is an agent or a flow (#8257): exactly one of ``agent_id`` and
``flow_id`` is set on a link and on a run (``ck_*_one_subject``). A flow runs
once per launch on ``MODEL_INDEPENDENT``, its models being per node.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSON, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Evaluation(Base):
    """Top-level evaluation entity – a multi-agent benchmark."""

    __tablename__ = "evaluations"
    __table_args__ = (
        CheckConstraint(
            "timeout_seconds >= 60", name="ck_evaluations_timeout_seconds_min"
        ),
        CheckConstraint(
            "reuse_default IN ('off', 'on')",
            name="ck_evaluations_reuse_default",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Three-tier visibility
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)
    shared_group_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    # Per-group permission tier, the same shape as the other shared entities.
    # NULL keeps a grant at read_only, ``get_group_permission``'s default.
    shared_group_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Per-user sharing + company-managed narrowing, shaped like the group
    # columns and keyed by User UUID; see ``app.models.agent`` for the rationale
    # and ``app.api.v1.helpers.user_visible_filter`` for enforcement.
    shared_user_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_user_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    access_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    # Display
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set only on a built-in evaluation, by the startup seed (app/evaluations/seed.py),
    # which matches its rows by it; NULL on every user-created evaluation.
    slug: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)

    # The output family that selects the package's metrics. NULL: the
    # evaluation stays viewable but cannot run until one is set.
    family: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Trigger configuration
    # { "schedule": "0 0 * * 1", "on_agent_change": true }
    trigger_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # When True, each agent is also tested with its own configured model
    use_agent_default_model: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    # Additional models to test – each agent is tested with each model
    # e.g. ["claude-sonnet-5", "gpt-4o"]
    model_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # Disabled additional models kept in config but excluded from execution.
    disabled_model_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )

    # "llm_only" (the persona and the entry alone) or "full_loop" (the agent as
    # configured, the default); any other stored value runs as LLM Only.
    execution_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="full_loop"
    )

    # A Full Loop run checks prompts and answers with the prompt guard, as chat
    # does. Off by default: its semantic and ``llm:`` rules cost model
    # calls on every entry, and only the author knows if the guard is under test.
    apply_prompt_guards: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    # A Full Loop run holds tool calls that need approval, as chat does.
    # Off by default: nobody approves during a run, so a held call would score
    # the gate, not the agent.
    require_tool_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    # "parallel" (all runs at once) or "sequential" (one run at a time)
    run_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="parallel")

    # LLM model used as judge (defaults to platform default if null)
    evaluator_model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # The judge's response budget on every run, so long verdicts are not
    # cut; 256..131072, enforced by the schema.
    judge_max_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=8192, server_default=sa.text("8192")
    )

    # Custom judge system prompt (defaults to built-in prompt if null)
    evaluator_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Global evaluation instructions passed to the LLM judge for every entry.
    # Per-entry evaluation_prompt overrides this when set.
    evaluation_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Overall Performance's shared victory zone, in % of the best value:
    # runs inside it tie at 100, the rest scale linearly to 0 at the worst. Every
    # performance metric uses it; quality metrics, absolute, never do. The server
    # default covers a replica still writing only ``speed_tolerance`` during a
    # rolling deploy.
    performance_tolerance: Mapped[float] = mapped_column(
        Float, nullable=False, default=5.0, server_default=sa.text("5.0")
    )

    # Retired; ignored on write and served by nothing. The
    # column keeps its stored values.
    quality_weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)

    # Per-criterion quality weights (JSON dict, keys match SUB_SCORE_WEIGHTS).
    sub_score_weights: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Quality source: only "judge". "f1" is retired: create and update refuse it,
    # but a stored row, or its import or copy, can still hold it.
    quality_source: Mapped[str] = mapped_column(
        String(10), nullable=False, default="judge"
    )

    # Consistency penalty factor (k) for the penalized mean formula:
    # run_score = mean - k * std_dev.  Higher k penalizes inconsistency more.
    penalty_factor: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)

    # Maximum wall-clock time for the whole evaluation job; the SAQ worker kills
    # the job past it. Default 3600s (1 hour), minimum 60s.
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)

    # User-defined params logged as source=evaluation on every run.
    custom_params: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # Key/value tags every run of the evaluation inherits at read time;
    # reserved keys are dropped (``services/evaluation/tags.py``). NULL = none.
    tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=None)

    # Structured-JSON per-field scoring, keyed by JSON Pointer:
    #   {"field_modes": {"/severity": "exact"}, "field_weights": {"/summary": 2.0}}
    # Scoring intent, so on the evaluation: two evaluations of one dataset can
    # weigh a field differently. An unset field is derived from the schema.
    field_scoring_overrides: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # The judge runs when this is NULL or holds "judge" (``[]`` turns it off).
    # "judge" is the only key; any other is refused on write.
    scorer_selection: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # 'off' | 'on'; a trigger may also pass 'inherit'. The application default is
    # 'on'; the server default stays 'off', as changing it takes a migration.
    reuse_default: Mapped[str] = mapped_column(
        String(20), nullable=False, default="on", server_default="off"
    )

    # The metric keys composing Overall Quality / Performance. NULL
    # inherits every available metric; [] selects none, so the headline is N/A.
    # The update writes NULL as a value, never as "no change".
    overall_quality_metrics: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    overall_performance_metrics: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Placebo-controlled method: ``PlaceboConfig`` in
    # services/evaluation_placebo.py - targets, trials per entry, no-target arm,
    # optional custom text. NULL = the evaluation runs without placebo arms.
    placebo_config: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # Built by Platform Health (#8257), which has no user row: the mark
    # the evaluation engine gate reads to run it whatever the mode, while
    # Platform Health is available. Set only by server code; never copied, exported or
    # imported.
    concierge_built: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.false()
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


SUBJECT_AGENT = "agent"
SUBJECT_FLOW = "flow"


class _Subject:
    """An agent or a flow (#8257), read off ``agent_id`` / ``flow_id``.

    Plain properties: the columns are declared on each model, never here, or
    the declarative mixin would map them a second time.
    """

    agent_id: uuid.UUID | None
    flow_id: uuid.UUID | None

    @property
    def subject_kind(self) -> str:
        return SUBJECT_FLOW if self.flow_id is not None else SUBJECT_AGENT

    @property
    def subject_id(self) -> uuid.UUID:
        subject = self.flow_id if self.flow_id is not None else self.agent_id
        if subject is None:  # ck_*_one_subject forbids it on a stored row
            raise LookupError("an evaluation subject needs an agent_id or a flow_id")
        return subject


class EvaluationAgent(_Subject, Base):
    """Join table linking an evaluation to its subjects: agents, and flows (#8257)."""

    __tablename__ = "evaluation_agents"
    __table_args__ = (
        UniqueConstraint("evaluation_id", "agent_id", name="uq_eval_agent"),
        Index(
            "uq_eval_flow",
            "evaluation_id",
            "flow_id",
            unique=True,
            postgresql_where=sa.text("flow_id IS NOT NULL"),
        ),
        CheckConstraint("num_nonnulls(agent_id, flow_id) = 1", name="ck_evaluation_agents_one_subject"),
        # At most one baseline agent per evaluation (#2606).
        Index(
            "uq_eval_agent_baseline",
            "evaluation_id",
            unique=True,
            postgresql_where=sa.text("is_baseline"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    flow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flows.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Tags every run of this agent in the evaluation inherits, as
    # ``Evaluation.tags``.
    tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=None)
    # The evaluation's baseline agent; at most one (``uq_eval_agent_baseline``).
    is_baseline: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.false()
    )
    # How this agent's raw output maps onto the shape the family scores;
    # NULL scores the raw output.
    output_extractor: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

class EvaluationRun(_Subject, Base):
    """A single execution of one agent with one model, or of one flow, against the full dataset."""

    __tablename__ = "evaluation_runs"
    __table_args__ = (
        CheckConstraint("num_nonnulls(agent_id, flow_id) = 1", name="ck_evaluation_runs_one_subject"),
        # The stranded-run sweep's scan, every few seconds (#3597). Partial
        # on its predicate, so it holds only runs in flight; ``owner_execution_id``
        # rides along for the join to ``orchestration_executions``.
        Index(
            "ix_evaluation_runs_stranded_candidates",
            "id",
            "owner_execution_id",
            postgresql_where=sa.text("status = 'running' AND completed_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    flow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flows.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # The flow version this run executed (#8257), as ``persona_version_number``
    # for an agent; NULL on an agent's run.
    flow_version_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The LLM model used for this run (overrides agent model)
    model_id: Mapped[str] = mapped_column(String(100), nullable=False)

    # Whether model_id is the agent's own configured model
    is_default_model: Mapped[bool] = mapped_column(Boolean, default=False)

    # pending → running → completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    # The execution that last made this run pending or running, written
    # in the same transaction as that status. Cleanup and pending-run
    # selection match on it, so a dead-lettered attempt never fails a run a later
    # execution took over. Not a foreign key: executions are never pruned, and no
    # ON DELETE may erase which one produced a result. A NULL owner is handled by
    # ``services/evaluation/run_ownership.py``.
    owner_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # What triggered this evaluation
    trigger_reason: Mapped[str] = mapped_column(String(50), nullable=False)

    # Average score across all entries (0-100), set on completion
    score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Retired: speed score from execution duration; kept for historical runs.
    speed_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Retired: harmonic mean of score and speed_score; kept for historical runs.
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Retired: token-level F1 (0-100), the mean of its entries' F1 when it was
    # scored; kept for historical runs, never written or cleared by any scoring or
    # headline pass.
    f1_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Overall Quality (absolute) and Overall Performance (comparative within the
    # evaluation), 0-100, higher is better. NULL is "not measured", apart
    # from a measured 0.
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    performance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Provenance for the two headlines above:
    #   {"quality_metric_keys": [...], "performance_metric_keys": [...],
    #    "metric_entry_counts": {key: n}, "excluded": [{key, reason}]}
    # Stored, not derived on read: Overall Performance depends on the run set, so
    # a reader recomputing it would disagree. The entry counts tell a partial
    # aggregate from a full one.
    headline_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # The metric suite version that produced the scores, so a changed
    # metric definition is told apart from a regression. NULL on older runs,
    # never backfilled: that lineage was not observed.
    metric_suite_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # The scorers the stored scores came from, stamped wherever entries
    # are scored, rescores included. Not read from the evaluation, which can
    # change: turning its judge on would read a past judge-off run, whose entries
    # store 0.0, as a headline of 0. NULL (an older run) reads as judge-on, which
    # it was.
    scorer_selection: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # The mode this run executed in, ``llm_only``, ``full_loop`` or
    # ``skip_llm``, stamped at start. Not the ``execution_mode`` run parameter,
    # which a custom one overrides. NULL: read from the results
    # (``services/evaluation/run_mode.py``).
    execution_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Whether this run used the prompt guard, stamped at start; False
    # outside Full Loop. NULL on an older run, which checked prompts but not
    # answers: neither True nor False fits, so never backfill it.
    apply_prompt_guards: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Whether this run held tool calls for approval, stamped at start;
    # False outside Full Loop. NULL on an older run, which ran each agent's gate
    # as configured: neither True nor False fits every agent.
    require_tool_approval: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Raw execution duration in seconds
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Progress tracking
    completed_entries: Mapped[int] = mapped_column(Integer, default=0)
    total_entries: Mapped[int] = mapped_column(Integer, default=0)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Why the last rescore failed, cleared by the next success. Apart from
    # ``error``: a rescore leaves the run's status and its reason alone.
    rescore_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Persona version active at the time this run was created
    persona_version_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Prompt versions active on the agent at run creation time.
    # Format: {"<prompt_uuid>": <version_number>}
    prompt_versions: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # Agent config-field versions active on the agent at run creation time.
    # Format: {"guardrails": 3, "temperature": 2, ...}
    agent_config_versions: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        default=None,
    )

    # Skill versions active on the agent at run creation time.
    # Format: {"<skill_uuid>": <version_number>}
    # A run that loads a skill mid-flight through ``load_skill`` resolves the body
    # pinned here, not the live (possibly re-fetched) one, or a replay of the
    # same run silently grades a different prompt.
    skill_versions: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        default=None,
    )

    # {dataset_id: version_number} pinned at trigger time. NULL: an older run,
    # rendered from the live entries.
    pinned_dataset_versions: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Key/value tags, editable per run; merged over the agent's and the
    # evaluation's at read time (``services/evaluation/tags.py``). NULL = none.
    tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=None)

    # The results with an ``execution_anomaly_kind``, counted so they
    # stay visible.
    anomaly_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )

    # Placebo-controlled comparison. ``real`` is the agent as
    # configured - every run outside a comparison; ``placebo`` replaces
    # ``placebo_target`` by the neutral text of ``placebo_snapshot``
    # (``PlaceboBuild.snapshot()``); ``no_target`` removes it. Each trial of an
    # arm is one run (``trial_index``, 0 outside a comparison); the runs of one
    # comparison share ``placebo_comparison_id``.
    arm: Mapped[str] = mapped_column(
        String(20), nullable=False, default="real", server_default="real"
    )
    trial_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    placebo_target: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    placebo_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    # No foreign key, as ``owner_execution_id``: a run's table never needs the
    # comparisons' (scratch schemas built from a table list stay valid), and
    # both go with their evaluation (CASCADE).
    placebo_comparison_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class EvaluationResult(Base):
    """Scored output for one dataset entry within a run."""

    __tablename__ = "evaluation_results"
    __table_args__ = (
        # Deleting a result or a snapshot entry sets these keys NULL on the rows that
        # point at it: the indexes find them instead of scanning the table.
        Index(
            "ix_evaluation_results_reused_from_result_id",
            "reused_from_result_id",
            postgresql_where=sa.text("reused_from_result_id IS NOT NULL"),
        ),
        Index(
            "ix_evaluation_results_version_entry_id",
            "version_entry_id",
            postgresql_where=sa.text("version_entry_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dataset_entries.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # The agent's full response
    agent_output: Mapped[str] = mapped_column(Text, nullable=False)

    # Global score 0-100 (weighted average of sub-scores)
    score: Mapped[float] = mapped_column(Float, nullable=False)

    # Sub-scores JSON:
    # {
    #   "tool_selection_quality": 85,
    #   "response_completeness": 90,
    #   "response_accuracy": 88,
    #   "instruction_adherence": 92,
    #   "coherence": 95
    # }
    sub_scores: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # LLM judge explanation
    evaluator_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Agent execution time in seconds (excludes judge)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Retired: speed score across the run's entries; kept for historical rows.
    speed_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Retired: harmonic mean of score and speed_score; kept for historical rows.
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Retired: token-level F1 (0-100); kept for historical rows, never
    # written, and cleared only when a resumed callback replaces the answer it scored.
    f1_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Durations, the agent's timeline, each metric's result, the chain ledger and
    # the extracted answer (``dev-docs/reference/evaluations.md`` §10, Stored results).
    # ``traces`` stays while traces move to EvaluationResultTrace links.
    execution_details: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # The snapshot entry this result was scored against, rendered instead of the
    # live entry.
    version_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dataset_version_entries.id", ondelete="SET NULL"),
        nullable=True,
    )

    # SHA-256 of the reuse-relevant inputs, indexed for reuse lookup.
    input_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    # This prediction was reused from a prior run, not re-executed.
    is_reused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    reused_from_result_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluation_results.id", ondelete="SET NULL"),
        nullable=True,
    )

    # The entry's anomaly, NULL when none; every kind is kept out of reuse
    # (``ANOMALY_KIND_*`` in ``services/evaluation/framing.py``).
    # "transient_error", an error surfacing as the output: not judged,
    # left out of every run aggregate. "platform_error", a run the platform could
    # not make: the same, and shown as left out in the headlines' coverage.
    # "confirmation_seeking", the agent asking instead of finishing: judged and
    # counted.
    execution_anomaly_kind: Mapped[str | None] = mapped_column(
        String(24), nullable=True
    )

    # The judge gave no usable, complete verdict: unparsable, truncated
    # or missing a sub-score. Excluded from every run aggregate, never scored
    # with a made-up value.
    is_judge_failure: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.text("false")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class EvaluationPlaceboComparison(Base):
    """One placebo launch of an evaluation and its statistics.

    Its runs carry ``placebo_comparison_id``. ``config`` is the placebo
    configuration the launch ran with, frozen; ``results`` holds, per agent x
    model x target and outcome, the effect, its bootstrap interval, the raw and
    Holm-adjusted p-values and the verdict; ``warnings`` what limits them.
    """

    __tablename__ = "evaluation_placebo_comparisons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # pending → running → completed | failed | cancelled
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    seed: Mapped[str] = mapped_column(String(64), nullable=False)
    corpus_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    # The administrator who launched it: a bare id, no foreign key to ``users``.
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    results: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    warnings: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class EvaluationResultTrace(Base):
    """Link table between an evaluation result and one or more native traces."""

    __tablename__ = "evaluation_result_traces"
    __table_args__ = (
        UniqueConstraint("result_id", "trace_id", name="uq_eval_result_trace_pair"),
        UniqueConstraint(
            "result_id",
            "trace_order",
            name="uq_eval_result_trace_order",
        ),
        CheckConstraint(
            "trace_role IN ('agent', 'judge', 'auxiliary')",
            name="ck_eval_result_trace_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluation_results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Local trace id written to ``llm_traces`` asynchronously — no FK to
    # avoid a race between the synchronous commit and the async dispatcher
    # (same convention as Message.llm_trace_id / AssignmentRun.llm_trace_id).
    trace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    trace_role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="agent", server_default="agent"
    )
    trace_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class EvaluationEpisode(Base):
    """One run/entry pair's execution episode, for async and resumable runs."""

    __tablename__ = "evaluation_episodes"
    __table_args__ = (
        UniqueConstraint("run_id", "entry_id", name="uq_eval_episode_run_entry"),
        UniqueConstraint("correlation_id", name="uq_eval_episode_correlation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dataset_entries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Correlates an external callback to the episode.
    correlation_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)

    # pending | running | waiting_callback | resumed | completed | failed | timed_out
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)

    # Latest idempotency key accepted for callback resume.
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # The keys accepted before it, so a replay of an earlier callback is refused.
    superseded_idempotency_keys: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )

    waiting_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    callback_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    resumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # First user-visible answer and final answer after full continuation.
    first_turn_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the first turn produced before pausing, ``version_entry_id`` and
    # a fresh result's ``execution_details`` minus judge and quality, which the
    # resume builds the result from. NULL on an older paused episode.
    first_turn_details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    final_output: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Normalized timeline.
    steps: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    participants: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    callback_payload: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    @property
    def accepted_idempotency_keys(self) -> list[str]:
        """Every callback key this episode accepted, the latest last."""
        latest = [self.idempotency_key] if self.idempotency_key else []
        return [*(self.superseded_idempotency_keys or []), *latest]
