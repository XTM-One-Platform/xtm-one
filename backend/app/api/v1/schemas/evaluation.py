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

"""Pydantic schemas for evaluation endpoints."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictBool, model_validator

# ---------------------------------------------------------------------------
# Output extraction
# ---------------------------------------------------------------------------
#
# Discriminated unions rather than a bare ``dict``: an unknown ``op`` or ``kind``
# becomes a 422 naming the field, instead of a key Pydantic silently drops and the
# engine then cannot find. Pydantic covers the SHAPE; whether the chain *connects*
# is ``validate_extractor_chain`` in services/evaluation/extraction/spec.py, which
# the routes call in the same place they call ``validate_field_scoring_overrides``.


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentOutputSource(_Strict):
    kind: Literal["agent_output"]


class ToolOutputSource(_Strict):
    kind: Literal["tool_output"]
    tool_name: str = Field(min_length=1, max_length=128)


class ToolInputSource(_Strict):
    kind: Literal["tool_input"]
    tool_name: str = Field(min_length=1, max_length=128)


SourceSpec = Annotated[
    AgentOutputSource | ToolOutputSource | ToolInputSource,
    Field(discriminator="kind"),
]


class FindJsonOp(_Strict):
    op: Literal["find_json"]


class ParseJsonOp(_Strict):
    op: Literal["parse_json"]


class SelectPathOp(_Strict):
    op: Literal["select_path"]
    path: list[str] = Field(min_length=1)


class ObjectKeysOp(_Strict):
    op: Literal["object_keys"]


class ObjectValuesOp(_Strict):
    op: Literal["object_values"]


class CollectKeysOp(_Strict):
    op: Literal["collect_keys"]
    path: list[str] = Field(min_length=1)


class FlattenOp(_Strict):
    op: Literal["flatten"]


class DedupeSortOp(_Strict):
    op: Literal["dedupe_sort"]


OperationSpec = Annotated[
    FindJsonOp
    | ParseJsonOp
    | SelectPathOp
    | ObjectKeysOp
    | ObjectValuesOp
    | CollectKeysOp
    | FlattenOp
    | DedupeSortOp,
    Field(discriminator="op"),
]


class ExtractorSpec(_Strict):
    source: SourceSpec
    operations: list[OperationSpec] = Field(min_length=1)
    output_kind: Literal["object", "array", "label_set"]


class ImportedAgentSettings(BaseModel):
    """An embedded agent's settings in an imported evaluation.

    Checked as an update checks them: a malformed value is a 422 naming it,
    never a server error. Only a JSON ``true`` marks the baseline.
    """

    tags: dict | None = None
    output_extractor: ExtractorSpec | None = None
    is_baseline: StrictBool = False


class PreviewExtractorSample(_Strict):
    """Material an author pastes in, when no stored result exists yet."""

    agent_output: str | None = None
    traces: list[dict] | None = None


class PreviewExtractorRequest(_Strict):
    agent_id: str
    extractor: ExtractorSpec
    # Exactly one of the two; enforced in the route so the message names both.
    result_id: str | None = None
    sample: PreviewExtractorSample | None = None


class PreviewExtractorForFamilyRequest(_Strict):
    """A preview before the evaluation exists: the creation screen has its
    family, the extractor and a pasted sample, and no evaluation, agent or result."""

    family: str = Field(max_length=32)
    extractor: ExtractorSpec
    sample: PreviewExtractorSample


class ExtractionFailureResponse(BaseModel):
    """Bounded metadata only, never a fragment of the material."""

    reason: str
    message: str
    operation_index: int | None = None
    operation: str | None = None
    path: str | None = None
    expected_kind: str | None = None
    actual_kind: str | None = None


class PreviewExtractorResponse(BaseModel):
    status: Literal["extracted", "failed"]
    prediction: str | None = None
    output_kind: str | None = None
    item_count: int | None = None
    failure: ExtractionFailureResponse | None = None


class ExtractorSampleResultResponse(BaseModel):
    """A stored result an author may preview against. What names its entry, no output."""

    result_id: str
    created_at: datetime
    # The entry's description, else its 0-based position; both null for an entry since
    # deleted.
    description: str | None = None
    position: int | None = None


from app.api.v1.schemas.column_limits import column_length_validator, lone_surrogate_validator
from app.models.evaluation import Evaluation, EvaluationResult, EvaluationRun
from app.services.evaluation.settings import PENALTY_FACTOR_MAX, SubScoreWeights
from app.services.evaluation_placebo import PlaceboConfig, placebo_mode_error

_EVALUATION_COLUMN_FIELDS = ("name", "execution_mode", "run_mode", "evaluator_model", "quality_source")

#: The modes an evaluation runs its agents in. Create, update and import refuse
#: any other string.
EvaluationExecutionMode = Literal["llm_only", "full_loop"]
EVALUATION_EXECUTION_MODES: tuple[str, ...] = ("llm_only", "full_loop")

#: How an evaluation launches its runs: all at once or one at a time. Create,
#: update and import refuse any other string, which would run in parallel.
EvaluationRunMode = Literal["parallel", "sequential"]
EVALUATION_RUN_MODES: tuple[str, ...] = ("parallel", "sequential")

# Cap shared by both bulk-selection endpoints. ``trigger-from-runs`` and
# ``rescore-from-runs`` build an ``IN (...)`` over the whole list, and the rescore
# also locks every matching row ``FOR UPDATE``. Both are admin- and AI-gated and the
# Activity tab selects at most 20, but an unbounded list would let one request hold
# row locks and spend model quota far beyond intent. One constant so the two
# endpoints cannot drift apart.
MAX_BULK_RUN_IDS = 200


def _retired_quality_weight() -> Any:
    """``quality_weight`` weighed the overall score that was retired, and nothing scores
    with it. A create or update may still send it, as the pages of earlier versions do
    on every save, and it is ignored: never applied, never dumped."""
    return Field(
        None,
        exclude=True,
        description="Retired: accepted and ignored, nothing scores with it.",
        json_schema_extra={"deprecated": True},
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class CreateEvaluationRequest(BaseModel):
    name: str
    description: str | None = None
    # Output family driving package metric selection. Optional here (import
    # and copy may omit it) but required before a run.
    family: str | None = Field(None, max_length=32)
    trigger_config: dict | None = None
    use_agent_default_model: bool = True
    model_ids: list[str] = []
    disabled_model_ids: list[str] = []
    execution_mode: EvaluationExecutionMode = Field(
        "full_loop",
        description=(
            "full_loop (the default) runs the agent as it is configured: its handovers, tools, "
            "integrations and knowledge bases, with their side effects. llm_only sends only the "
            "agent's persona and the entry to the model, with none of them."
        ),
    )
    apply_prompt_guards: bool = Field(
        False,
        description=(
            "Whether a full_loop run checks the entries' prompts and the agent's answers with "
            "the platform's prompt guard, as chat does. Off by default."
        ),
    )
    require_tool_approval: bool = Field(
        False,
        description=(
            "Whether a full_loop run holds tool calls that need approval, as chat does. "
            "Off by default: nobody can approve during an evaluation."
        ),
    )
    run_mode: EvaluationRunMode = "parallel"
    evaluator_model: str | None = None
    evaluator_prompt: str | None = None
    evaluation_instructions: str | None = None
    performance_tolerance: float = Field(5.0, ge=0.0)
    quality_weight: float | None = _retired_quality_weight()
    sub_score_weights: SubScoreWeights | None = None
    quality_source: str = "judge"
    # The scoring card's bounds: a negative factor would reward inconsistency.
    penalty_factor: float = Field(0.5, ge=0.0, le=PENALTY_FACTOR_MAX)
    timeout_seconds: int = Field(3600, ge=60)
    # Per-evaluation judge response budget.
    judge_max_tokens: int = Field(8192, ge=256, le=131072)
    enabled: bool = True
    is_company_managed: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    agent_ids: list[str] | None = None
    dataset_ids: list[str] | None = None
    custom_params: dict | None = None
    # Per-field JSON scoring intent, keyed by JSON Pointer:
    #   {"field_modes": {"/severity": "exact"}, "field_weights": {"/summary": 2.0}}
    field_scoring_overrides: dict | None = None
    scorer_selection: list[str] | None = None
    # The column's CHECK: anything else would fail the insert.
    reuse_default: Literal["on", "off"] = "on"
    # Per-agent prediction extractors, keyed by agent id. Absent = leave
    # unchanged; an agent mapped to null clears its extractor.
    agent_extractors: dict[str, ExtractorSpec | None] | None = None
    # Cascading tags.
    tags: dict | None = None
    # Headline composition, tri-state on the wire: `null` inherits every
    # metric the family measures, `[]` composes from none (the headline reads N/A),
    # a subset refines. Never normalised on serialisation: a `[]` collapsed to
    # `null` re-adds every metric the author removed.
    overall_quality_metrics: list[str] | None = None
    overall_performance_metrics: list[str] | None = None
    # The placebo-controlled method; null runs without placebo arms.
    placebo_config: PlaceboConfig | None = None


    _fit_columns = column_length_validator(Evaluation, *_EVALUATION_COLUMN_FIELDS)

    @model_validator(mode="after")
    def _placebo_runs_in_its_execution_mode(self) -> "CreateEvaluationRequest":
        error = placebo_mode_error(self.placebo_config, self.execution_mode)
        if error:
            raise ValueError(error)
        return self


class UpdateEvaluationRequest(BaseModel):
    """A partial update: an omitted field is kept, ``null`` clears a nullable
    setting (not ``family`` nor ``scorer_selection``, which it leaves unchanged).
    ``trigger_config`` is merged into the stored one: a key left out is
    kept, a key sent as ``null`` removed, and ``{}`` changes nothing."""

    name: str | None = None
    description: str | None = None
    # Output family; an unknown value is rejected.
    family: str | None = Field(None, max_length=32)
    trigger_config: dict | None = None
    use_agent_default_model: bool | None = None
    model_ids: list[str] | None = None
    disabled_model_ids: list[str] | None = None
    execution_mode: EvaluationExecutionMode | None = None
    apply_prompt_guards: bool | None = None
    require_tool_approval: bool | None = None
    run_mode: EvaluationRunMode | None = None
    evaluator_model: str | None = None
    evaluator_prompt: str | None = None
    evaluation_instructions: str | None = None
    performance_tolerance: float | None = Field(None, ge=0.0)
    quality_weight: float | None = _retired_quality_weight()
    sub_score_weights: SubScoreWeights | None = None
    quality_source: str | None = None
    penalty_factor: float | None = Field(None, ge=0.0, le=PENALTY_FACTOR_MAX)
    timeout_seconds: int | None = Field(None, ge=60)
    judge_max_tokens: int | None = Field(None, ge=256, le=131072)
    enabled: bool | None = None
    is_company_managed: bool | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    custom_params: dict | None = None
    # Per-field JSON scoring intent, keyed by JSON Pointer:
    #   {"field_modes": {"/severity": "exact"}, "field_weights": {"/summary": 2.0}}
    field_scoring_overrides: dict | None = None
    scorer_selection: list[str] | None = None
    reuse_default: Literal["on", "off"] | None = None
    agent_extractors: dict[str, ExtractorSpec | None] | None = None
    # Cascading tags.
    tags: dict | None = None
    # Headline composition, tri-state as on create: `null` inherits,
    # `[]` composes from none, a subset refines. Never normalised.
    overall_quality_metrics: list[str] | None = None
    overall_performance_metrics: list[str] | None = None
    # The placebo-controlled method: `null` removes it. Its fit with the
    # execution mode is checked by the route, on the row as the update leaves it.
    placebo_config: PlaceboConfig | None = None


    _fit_columns = column_length_validator(Evaluation, *_EVALUATION_COLUMN_FIELDS)


class AlwaysAskAgent(BaseModel):
    """One selected agent and the tools it carries that are marked "always ask"."""

    agent_id: str
    agent_name: str
    tools: list[str]


class AlwaysAskToolsResponse(BaseModel):
    """The selected agents carrying a tool an administrator marked "always ask"."""

    agents: list[AlwaysAskAgent]


class PromptGuardStateResponse(BaseModel):
    """Whether a prompt guard is connected on the platform."""

    connected: bool


class EvaluationAgentSummary(BaseModel):
    id: str
    name: str
    slug: str | None = None
    icon: str | None = None
    # Cascading tags and the agent-scoped baseline.
    tags: dict | None = None
    is_baseline: bool = False

    model_config = {"from_attributes": True}


class EvaluationFlowSummary(BaseModel):
    """A flow subject of the evaluation (#8257), linked by Platform Health."""

    id: str
    name: str
    tags: dict | None = None
    is_baseline: bool = False


class EvaluationFamilyConflict(BaseModel):
    """A linked dataset whose family no longer matches the evaluation after an update
    changed the evaluation's family."""

    dataset_id: str
    dataset_name: str
    message: str


class EvaluationResponse(BaseModel):
    id: str
    user_id: str | None = None
    name: str
    description: str | None = None
    family: str | None = None
    trigger_config: dict = {}
    use_agent_default_model: bool = True
    model_ids: list[str] = []
    disabled_model_ids: list[str] = []
    # The models of `model_ids` no provider of the owner offers: a
    # launch refuses them. Read with the response, never stored.
    unavailable_model_ids: list[str] = []
    # Served as stored: an older row may hold a value that is
    # neither mode, and it runs as LLM Only.
    execution_mode: str = "full_loop"
    apply_prompt_guards: bool = False
    require_tool_approval: bool = False
    run_mode: str = "parallel"
    evaluator_model: str | None = None
    evaluator_prompt: str | None = None
    evaluation_instructions: str | None = None
    performance_tolerance: float = 5.0
    sub_score_weights: dict[str, float] | None = None
    quality_source: str = "judge"
    penalty_factor: float = 0.5
    timeout_seconds: int = 3600
    judge_max_tokens: int = 8192
    enabled: bool = True
    is_company_managed: bool = False
    # helpers.can_manage() for the caller: the UI's edit gate.
    can_manage: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    agent_ids: list[str] = []
    agents: list[EvaluationAgentSummary] = []
    # Flow subjects (#8257): never in ``agent_ids`` or ``agents``.
    flows: list[EvaluationFlowSummary] = []
    entry_count: int = 0
    dataset_count: int = 0
    has_active_runs: bool = False
    last_triggered_at: str | None = None
    created_at: str
    updated_at: str
    custom_params: dict | None = None
    # Per-field JSON scoring intent, keyed by JSON Pointer:
    #   {"field_modes": {"/severity": "exact"}, "field_weights": {"/summary": 2.0}}
    field_scoring_overrides: dict | None = None
    scorer_selection: list[str] | None = None
    reuse_default: str = "off"
    agent_extractors: dict[str, dict] | None = None
    # Cascading tags.
    tags: dict | None = None

    # Headline composition, tri-state: `null` inherits, `[]` composes
    # from none, a subset refines. Never normalised: a `[]` served as `null`
    # would re-add every metric the author removed.
    overall_quality_metrics: list[str] | None = None
    overall_performance_metrics: list[str] | None = None
    # Set only when an update's family change conflicts with an already-linked
    # dataset: reported, never auto-resolved.
    family_conflicts: list[EvaluationFamilyConflict] | None = None
    # The placebo-controlled method.
    placebo_config: dict | None = None
    # What an import could not carry over (a placebo target naming a skill of
    # the exporting instance), and what an import or a copy carried over
    # unusable (a model no provider of its new owner offers); empty on every
    # other answer.
    warnings: list[str] = []

    model_config = {"from_attributes": True}


class EvaluationDetailResponse(EvaluationResponse):
    pass


# ---------------------------------------------------------------------------
# Evaluation Run
# ---------------------------------------------------------------------------


class EvaluationRunResponse(BaseModel):
    id: str
    evaluation_id: str
    evaluation_name: str = ""
    # The subject (#8257): an agent (``agent_id``) or a flow (``flow_id``).
    # ``agent_name``, ``agent_slug`` and ``agent_user_name`` name either one.
    agent_id: str | None = None
    flow_id: str | None = None
    subject_kind: str = "agent"
    agent_name: str = ""
    agent_slug: str = ""
    agent_user_name: str | None = None
    model_id: str
    is_default_model: bool = False
    status: str
    trigger_reason: str
    score: float | None = None
    # Retired: `speed_score` and `overall_score` are `null` on new runs and
    # kept for historical ones. `speed_score` is a display fallback only, used when
    # `performance_score` is N/A; no run shows both.
    speed_score: float | None = None
    overall_score: float | None = None
    f1_score: float | None = None
    # Headline metrics. `None` (JSON `null`, shown "N/A") when nothing
    # contributed, never coerced to `0`: "not measured" and "measured zero" are
    # different answers.
    #
    # `quality_score` is absolute: composed from the family's deterministic quality
    # metrics, it reads the same whether the run is alone or one of twenty.
    # `performance_score` is COMPARATIVE: normalised against the evaluation's other
    # runs, it means something only within that evaluation, which every surface
    # showing it must say.
    #
    # `*_metric_keys` name what composed each value and `metric_entry_counts` each
    # metric's contributing entries, so a partial aggregate reads as one.
    quality_score: float | None = None
    quality_metric_keys: list[str] = []
    performance_score: float | None = None
    performance_metric_keys: list[str] = []
    metric_entry_counts: dict[str, int] = {}
    # What each contributing metric put into each headline's mean. Not the raw
    # per-metric mean the aggregate endpoint reports: quality subtracts a
    # consistency penalty and performance is normalised across the evaluation, so
    # the two differ by design and the panel needs both.
    quality_contributions: dict[str, float] = {}
    performance_contributions: dict[str, float] = {}
    # The entries Overall Quality considers, those it covers, and per reason how many it
    # leaves out: `{entries, counted, left_out: {reason: count}}`. `None` on a run
    # scored before it was recorded, or with no quality headline.
    quality_coverage: dict | None = None
    # Metrics that could not contribute, and why. Two different origins share one list:
    #
    #   * `not_applicable` / `failed` are classified from the metric package's status
    #     vocabulary (`metric_package.MetricStatus`) and describe the run carrying them —
    #     no re-run can recover the first, one might recover the second. Quality emits
    #     them against an explicit selection; performance also against the default
    #     one, for a metric some runs lack.
    #   * `missing_from_other_runs` (performance only): this run measured the
    #     metric, but Overall Performance counts only the metrics every run has.
    #   * `not_a_default_contributor` comes from the default-selection logic and
    #     means the opposite: the metric SCORED but composes the headline only when
    #     selected explicitly. Never emitted when a selection exists.
    #
    # The vocabulary is OPEN: a consumer that meets an unrecognised reason renders it
    # neutrally, never as the nearest known one. Never a rewrite of the author's selection.
    #
    # Both halves are declared: a `response_model` silently drops a field it omits,
    # which once happened to `quality_aggregation_excluded`.
    quality_aggregation_excluded: list[dict] = []
    performance_aggregation_excluded: list[dict] = []
    quality_source: str = "judge"
    duration_seconds: float | None = None
    avg_sub_scores: dict = {}
    completed_entries: int = 0
    total_entries: int = 0
    error: str | None = None
    # Why the last rescore failed; the run's own status and error are left as they were.
    rescore_error: str | None = None
    # Set while the run is pending because its evaluation is HELD: agentic AI
    # is unavailable on this platform - ``license_required`` or
    # ``ai_disabled``. Not an error.
    held_reason: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str
    persona_version_number: int | None = None
    prompt_versions: dict[str, int] | None = None
    agent_config_versions: dict[str, int | None] | None = None
    # `{skill_id: version_number}`, matching its siblings above. A skill with no
    # snapshotted version is absent rather than null — there is nothing to pin,
    # and `load_skill` falls back to live content — so the value is never `None`.
    skill_versions: dict[str, int] | None = None
    # Score lineage and judge provenance. `null` on runs older
    # than their column, and meaningful: the first reads "scored by an unknown
    # suite", the second "the judge was not yet suppressible, so this run was
    # judged". Never defaulted: a value would assert a lineage nobody observed.
    metric_suite_version: str | None = None
    scorer_selection: list[str] | None = None
    can_replay: bool = True
    replay_block_reason: str | None = None
    pinned_dataset_versions: dict[str, int] | None = None
    tags: dict | None = None
    # Cascading tags: read-time merge of evaluation ⊕ agent ⊕ run tags,
    # plus the agent-derived baseline flag.
    effective_tags: dict = {}
    is_baseline: bool = False
    has_dataset_drift: bool = False
    # Execution anomalies of the autonomous evaluation mode.
    anomaly_count: int = 0
    # The mode this run executed in, as it recorded it: ``llm_only``,
    # ``full_loop``, ``skip_llm``, or None when nothing recorded one. Never the
    # evaluation's current setting, which can change after the run.
    execution_mode: str | None = None
    # Whether this run checked its prompts and answers with the prompt guard,
    # as it recorded it; None for a run recorded before the setting.
    apply_prompt_guards: bool | None = None
    # Whether this run held tool calls that need approval, as it
    # recorded it; None for a run recorded before the setting.
    require_tool_approval: bool | None = None
    # Entries that fell back to LLM Only; only older runs have any.
    fallback_entries: int = 0
    # Results whose prediction was reused from a prior run, not re-executed.
    reused_entries: int = 0
    # Results whose judge verdict was unusable, so excluded from aggregates.
    judge_failure_count: int = 0
    # Entries the platform could not run, left out of every headline.
    platform_error_count: int = 0
    # A rescore batch marks every named run terminal BEFORE the one shared Overall
    # Performance pass for the batch commits, so a run can briefly read "completed"
    # with a stale `performance_score`. `True` while an in-flight
    # `rescore_runs` execution still names this run. Computed by the run-detail
    # route only (the list and aggregate serializers would pay a query per row);
    # always `False` elsewhere.
    rescore_pending: bool = False
    #: A parked episode's callback can still land and rewrite the headlines, so
    #: the run-detail poll keeps running while the run reads terminal.
    #:
    #: Two bounds, both needed. While the episode is parked,
    #: ``timeout_waiting_episodes`` terminalises it once ``callback_deadline``
    #: passes. Once a callback is accepted that deadline is cleared, and the bound
    #: becomes the non-terminal ``resume_episode`` execution the endpoint enqueues
    #: in the same transaction, which the engine ends by completing,
    #: dead-lettering or blowing its deadline.
    #:
    #: The predicate requires the deadline to EXIST (the sweep ignores a row
    #: without one) and deliberately does NOT compare it to now:
    #: ``resume_from_callback`` accepts any non-terminal episode without reading
    #: the deadline, so between expiry and the sweep a callback is still taken,
    #: and a flag already false would stop the poll just before it lands.
    #:
    #: False does not mean nothing can change. An episode left ``resumed`` by a
    #: callback with no output (awaiting continuation) has no deadline and no
    #: execution in flight, so this reads false while a later callback is still
    #: legal. No bounded signal exists for that state; see
    #: ``is_callback_pending_for_run`` for the trade.
    #:
    #: A deadline-only check would read false for the whole resume-processing
    #: window, exactly when the headlines are about to change.
    callback_pending: bool = False
    # A placebo comparison's run: its arm, its trial, the target a
    # control arm replaces or removes, and its comparison; ``real``, 0 and
    # null on every other run.
    arm: str = "real"
    trial_index: int = 0
    placebo_target: dict | None = None
    placebo_comparison_id: str | None = None

    model_config = {"from_attributes": True}


class EvaluationRunCountResponse(BaseModel):
    """Total number of runs for an evaluation (drives Activity pagination)."""

    count: int


# ---------------------------------------------------------------------------
# Evaluation Result
# ---------------------------------------------------------------------------


class EvaluationResultResponse(BaseModel):
    id: str
    run_id: str
    entry_id: str | None = None
    description: str | None = None
    input_prompt: str = ""
    expected_answer: str = ""
    files: list[dict] | None = None
    agent_output: str
    trace_ids: list[str] = []
    execution_details: dict | None = None
    score: float
    duration_seconds: float | None = None
    speed_score: float | None = None
    overall_score: float | None = None
    f1_score: float | None = None
    sub_scores: dict = {}
    evaluator_reasoning: str | None = None
    created_at: str
    input_fingerprint: str | None = None
    is_reused: bool = False
    reused_from_result_id: str | None = None
    # Answered in LLM Only after its Full Loop attempt failed, or reused from such
    # an answer; only older results can be (``run_mode.py``).
    fell_back_to_llm_only: bool = False
    # Execution anomaly of the autonomous evaluation mode: whether
    # ``execution_anomaly_kind`` is set.
    is_execution_anomaly: bool = False
    # Anomaly subtype: "transient_error" and "platform_error" (excluded from
    # aggregates, judge skipped) or "confirmation_seeking" (judged and counted like a
    # normal entry). NULL for a non-anomalous result.
    execution_anomaly_kind: str | None = None
    # The judge could not produce a usable, complete verdict.
    is_judge_failure: bool = False

    model_config = {"from_attributes": True}


class EvaluationRunDetailResponse(EvaluationRunResponse):
    results: list[EvaluationResultResponse] = []
    # Entries whose extractor could not produce a prediction. On the
    # detail response only: the list endpoint builds each row without loading
    # results, so deriving it there would cost a query per run.
    extraction_failure_count: int = 0
    # The placebo a placebo arm ran with (FR-32); null on every other run.
    placebo_snapshot: dict | None = None


# ---------------------------------------------------------------------------
# Chart Data
# ---------------------------------------------------------------------------


class ChartDataPoint(BaseModel):
    triggered_at: str
    trigger_reason: str
    # Keyed by "agent/model". A series is ABSENT from a point when that run had
    # no value for the headline, never present as `0`: hence a sparse dict.
    scores: dict[str, float] = {}


class CorrelationPoint(BaseModel):
    """One agent/model pair on a two-headline correlation view.

    Both axis values come from the SAME run, the pair's latest completed one.
    A quality value from one run plotted against a performance value from
    another is a pair that never existed.
    """

    series: str
    run_id: str
    agent_id: str | None = None
    flow_id: str | None = None
    model_id: str
    x: float
    y: float


class EvaluationChartResponse(BaseModel):
    """Everything the evaluation summary plots.

    Three trend series and three correlation datasets, served together so the
    three headlines are never read over different windows.
    """

    # Echoed back so the UI can state the window it shows: a chart empty only
    # because of the filter must not read as an evaluation never run.
    range: str
    # Keys: "quality", "performance", "judge".
    trends: dict[str, list[ChartDataPoint]] = {}
    # Keys: "quality_performance", "judge_performance", "quality_judge".
    # Deliberately NOT filtered by `range`: a correlation point is each pair's
    # latest run, so a window would silently drop pairs rather than shorten a
    # history.
    correlations: dict[str, list[CorrelationPoint]] = {}
    # Pairs dropped from each view for want of one axis. Per view, because a pair
    # can be plottable on one correlation and not another, and a view that
    # silently shortens reads as fewer runs rather than fewer measurements.
    correlations_omitted: dict[str, int] = {}
    # Completed runs inside the window, model-independent ones included. `0` with a
    # non-empty evaluation distinguishes "filtered out" from "never run".
    runs_in_range: int = 0
    total_runs: int = 0


# ---------------------------------------------------------------------------
# Agent Binding
# ---------------------------------------------------------------------------


class AddAgentsRequest(BaseModel):
    agent_ids: list[str]


class TriggerEvaluationRequest(BaseModel):
    model_ids: list[str] | None = None
    include_default: bool = False
    params: dict | None = None
    tags: dict | None = None
    reuse: str | None = None  # inherit | on | off
    # A placebo comparison instead of one run per agent x model: its
    # arms x trials, from the evaluation's placebo configuration. Its runs
    # never reuse, whatever ``reuse`` says.
    placebo: bool = False
    # Required when the comparison repeats calls to tools the evaluation runs
    # unapproved (FR-14): the refusal lists them.
    confirm_write_tools: bool = False
    # The seed a placebo estimate returned: the launch builds the placebos it
    # checked. A fresh one when absent.
    placebo_seed: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class PlaceboEstimateRequest(BaseModel):
    """What a placebo comparison would run on these models (``TriggerEvaluationRequest``'s)."""

    model_ids: list[str] | None = None
    include_default: bool = False


class PlaceboEstimateResponse(BaseModel):
    """FR-12: the size of a placebo comparison, and what would refuse or hold it."""

    #: Sent back as the trigger's ``placebo_seed`` to launch what was estimated.
    seed: str
    runs: int
    agent_executions: int
    #: One judge call per execution when the judge is on, else 0.
    judge_calls: int
    #: At most this many token probes to build the placebos.
    probe_calls: int
    #: Agent tokens, from each agent x model's latest measured run; null while one was never measured.
    estimated_tokens: int | None = None
    execution_cap: int
    #: Agent name -> tool sources whose calls the comparison repeats unapproved.
    write_tool_sources: dict[str, list[str]] = {}
    #: The targets an agent cannot take: ``{agent_id, agent_name, target, reason}``.
    skipped: list[dict] = []
    warnings: list[str] = []
    #: Why the comparison would be refused; empty when it can launch.
    refusals: list[str] = []


class PlaceboComparisonRun(BaseModel):
    id: str
    agent_id: str
    model_id: str
    arm: str
    trial_index: int
    placebo_target: dict | None = None
    status: str


class PlaceboComparisonResponse(BaseModel):
    """A placebo comparison: its runs and, once settled, its statistics (FR-23 to FR-29)."""

    id: str
    evaluation_id: str
    #: ``running`` until no run is pending or running; then ``completed``, ``failed`` or ``cancelled``.
    status: str
    seed: str
    corpus_version: str | None = None
    #: The ``placebo_config`` the comparison ran with.
    config: dict | None = None
    #: Objects with a ``code``.
    warnings: list[dict] = []
    #: ``placebo_stats.comparison_statistics``; ``None`` until settled.
    results: dict | None = None
    created_by: str | None = None
    created_at: str
    completed_at: str | None = None
    runs: list[PlaceboComparisonRun] = []


class TriggerEvaluationFromRunsRequest(BaseModel):
    run_ids: list[str] = Field(default_factory=list, max_length=MAX_BULK_RUN_IDS)


class ReplayRunRequest(BaseModel):
    reuse: str | None = None  # inherit | on | off


class ReplayJudgeRequest(BaseModel):
    judge_max_tokens: int = Field(2048, ge=256, le=131072)


class ResumeEvaluationEpisodeRequest(BaseModel):
    # Lengths match the ``EvaluationEpisode`` columns, so an over-long value is a
    # 422 rather than a DataError 500 at flush.
    correlation_id: str = Field(..., min_length=1, max_length=120)
    # Non-blank: both the duplicate-callback key and the generation token
    # a dead-lettered resume's repair matches on. ``resume_from_callback`` treats
    # an empty stored key as ABSENT, so a blank key never registers as a
    # duplicate: every repeat would open a new generation carrying ``""``, and the
    # older execution's repair would match the newer one and finalize its episode.
    idempotency_key: str = Field(..., min_length=1, max_length=120)
    callback_payload: dict | None = None


class ResumeEvaluationEpisodeResponse(BaseModel):
    accepted: bool
    duplicate: bool = False
    episode_id: str
    status: str


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------


#
# The shape of an evaluation package, checked before the import writes
# anything: a value of the wrong type, out of its column's range or too long for
# it is a 400 naming the field, never a server error. An unknown key is ignored
# and a missing one takes its default, so older packages still import. The
# values the import reads leniently by rule stay ``object``: the run's status,
# mode and settings, a result's classification, provenance and execution details.

#: An index, a position or a count, as an ``Integer`` column holds it.
PackageInt = Annotated[int, Field(ge=0, le=2**31 - 1)]


class PackagedEntryRef(BaseModel):
    """The entry a result answers: its dataset's index in the package and its position."""

    dataset_index: PackageInt
    entry_position: PackageInt


class PackagedResult(BaseModel):
    entry_ref: PackagedEntryRef | None = None
    agent_output: str | None = None
    score: FiniteFloat | None = None
    sub_scores: dict | None = None
    evaluator_reasoning: str | None = None
    duration_seconds: FiniteFloat | None = None
    speed_score: FiniteFloat | None = None
    overall_score: FiniteFloat | None = None
    f1_score: FiniteFloat | None = None
    is_judge_failure: object = False
    execution_anomaly_kind: str | None = None
    is_reused: object = None
    execution_details: object = None
    created_at: str | None = None

    _fit_columns = column_length_validator(EvaluationResult, "execution_anomaly_kind")
    _no_lone_surrogates = lone_surrogate_validator(EvaluationResult)


class PackagedRunResults(BaseModel):
    """``evaluation_runs/run-<i>.json``."""

    results: list[PackagedResult] | None = None


class PackagedRun(BaseModel):
    #: The ``index`` of its agent in ``dependencies.agents``; skipped when it names none.
    agent_index: PackageInt | None = None
    results_path: str | None = None
    #: Older packages carry the results inline.
    results: list[PackagedResult] | None = None
    model_id: str | None = None
    is_default_model: object = False
    status: object = None
    trigger_reason: str | None = None
    execution_mode: object = None
    apply_prompt_guards: object = None
    require_tool_approval: object = None
    scorer_selection: list[str] | None = None
    metric_suite_version: str | None = None
    persona_version_number: PackageInt | None = None
    prompt_versions: dict | None = None
    agent_config_versions: dict | None = None
    skill_versions: dict | None = None
    score: FiniteFloat | None = None
    speed_score: FiniteFloat | None = None
    overall_score: FiniteFloat | None = None
    f1_score: FiniteFloat | None = None
    duration_seconds: FiniteFloat | None = None
    completed_entries: PackageInt | None = None
    total_entries: PackageInt | None = None
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str | None = None

    _fit_columns = column_length_validator(EvaluationRun, "model_id", "trigger_reason", "metric_suite_version")
    _no_lone_surrogates = lone_surrogate_validator(EvaluationRun)


class PackagedAgent(BaseModel):
    """An embedded agent: its bundle, and its settings in the evaluation (``ImportedAgentSettings``)."""

    model_config = ConfigDict(extra="allow")

    index: PackageInt = 0
    path: str | None = None


class PackagedDataset(BaseModel):
    index: PackageInt = 0
    path: str | None = None
    #: Its position among the evaluation's datasets; its index when absent.
    position: PackageInt | None = None


class PackagedDependencies(BaseModel):
    agents: list[PackagedAgent] | None = None
    datasets: list[PackagedDataset] | None = None

    @model_validator(mode="after")
    def _one_per_index_and_position(self) -> "PackagedDependencies":
        # A run names its agent and a result its dataset by index, and a dataset's
        # position orders the evaluation's datasets: shared, one of each pair would
        # take the other's runs, results or place.
        datasets = self.datasets or []
        for label, values in (
            ("agents: index", [agent.index for agent in self.agents or []]),
            ("datasets: index", [dataset.index for dataset in datasets]),
            (
                "datasets: position",
                [dataset.index if dataset.position is None else dataset.position for dataset in datasets],
            ),
        ):
            for value in values:
                if values.count(value) > 1:
                    raise ValueError(f"{label} {value} is used twice")
        return self


class PackagedEvaluation(BaseModel):
    """The evaluation. Its settings are checked as create checks them (``_imported_settings``)."""

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    last_triggered_at: str | None = None

    _fit_columns = column_length_validator(Evaluation, "name", stripped=True)


class EvaluationPackage(BaseModel):
    """``evaluation.json``."""

    version: int | None = None
    export_type: object = None
    evaluation: PackagedEvaluation | None = None
    dependencies: PackagedDependencies | None = None
    evaluation_runs: list[PackagedRun] | None = None
    #: The name older packages give ``evaluation_runs``.
    runs: list[PackagedRun] | None = None


# ---------------------------------------------------------------------------
# Dataset versions, run params, metrics, rescoring and run tags
# ---------------------------------------------------------------------------


class RunParamResponse(BaseModel):
    key: str
    value: str | None = None
    source: str

    model_config = {"from_attributes": True}


class RunParamsResponse(BaseModel):
    run_id: str
    params: list[RunParamResponse] = []


class RunParamDiffEntry(BaseModel):
    key: str
    value_a: str | None = None
    value_b: str | None = None
    source_a: str | None = None
    source_b: str | None = None


class RunDiffResponse(BaseModel):
    run_a: str
    run_b: str
    changed: list[RunParamDiffEntry] = []
    identical: list[RunParamDiffEntry] = []


class MetricResponse(BaseModel):
    key: str
    value: float | None = None
    # Which headline this metric composes, or `None` for execution telemetry that
    # composes neither. Declared because a `response_model` silently drops what
    # it omits, and the dataset entry's Metrics panel groups by it.
    half: Literal["quality", "performance"] | None = None
    # The unit the package declares for a performance metric (`milliseconds`,
    # `count`, `tokens`, `currency`, `ratio`), or `None` for a quality score and
    # the platform's own keys. The run page formats a value by it rather than
    # guessing from the key.
    unit: str | None = None
    # Whether this one entry's value means anything on its own, and the name it takes
    # there, as the package declares: the entry's Metrics strip follows them.
    shown_on_entry: bool = True
    entry_label: str | None = None

    model_config = {"from_attributes": True}


class MetricAggregate(BaseModel):
    key: str
    # As on `MetricResponse`; the run page groups its Metrics panel by it.
    half: Literal["quality", "performance"] | None = None
    # As on `MetricResponse`.
    unit: str | None = None
    # Whether the run's value of this metric pools its entries' counts: it then
    # differs from the entries' mean shown beside it, and the panel says so.
    pooled: bool = False
    # Whether the run's value is the macro F1 of the classes summed over the run:
    # it differs from the entries' mean too.
    macro: bool = False
    mean: float | None = None
    p50: float | None = None
    p95: float | None = None
    min: float | None = None
    max: float | None = None
    count: int = 0


class RunMetricsAggregateResponse(BaseModel):
    run_id: str
    aggregates: list[MetricAggregate] = []


class FamilyInfoResponse(BaseModel):
    """One output family for the evaluation family selector."""

    id: str
    label: str
    description: str
    metric_keys: list[str] = []
    structured_output: bool = False
    # The output kinds an extractor may produce for this family; empty when the
    # family takes no extraction in this version.
    extractor_output_kinds: list[str] = []


class MetricCatalogEntry(BaseModel):
    """One selectable metric for the headline composition selector.

    ``direction`` is ``None`` when the package declares none. Such a performance
    metric is NOT selectable: an unoriented value cannot be normalised, so
    offering it would let an author compose a headline that silently drops it.

    Every field the catalogue emits MUST be declared here: a response model drops
    what it does not declare, and the selector tests ``default_contributor`` with
    ``!== false``, so an absent field makes every metric a default contributor.
    """

    key: str
    label: str
    kind: Literal["quality", "performance"]
    direction: str | None = None
    # True for a metric the catalogue does not classify.
    default_contributor: bool = True
    # The declared unit of a performance metric, `None` for a quality score.
    unit: str | None = None


class RescoreRequest(_Strict):
    """A single rescore takes no field: it applies the evaluation's scorers.

    Strict, so a body still carrying the retired per-rescore ``scorer_selection``
    is refused (422) rather than silently rescored with another selection.
    """


class RescoreResponse(BaseModel):
    run_id: str
    # A rescore runs in the background: the request only schedules it.
    status: Literal["running"] = "running"


class RescoreFromRunsRequest(_Strict):
    """Bulk re-score selected runs, reusing their stored predictions."""

    run_ids: list[str] = Field(default_factory=list, max_length=MAX_BULK_RUN_IDS)


class RescoreFromRunsResult(BaseModel):
    run_id: str
    # "running": validated synchronously (the run exists and has stored
    # predictions) and scheduled in the background.
    # "failed": rejected synchronously (unknown run id, no predictions); never
    # reached the background job.
    status: Literal["running", "failed"]
    error: str | None = None


class RescoreFromRunsResponse(BaseModel):
    results: list[RescoreFromRunsResult]


class PatchRunTagsRequest(BaseModel):
    """Merge tags; keys mapped to null are removed (PATCH)."""

    tags: dict


class RunTagsResponse(BaseModel):
    run_id: str
    tags: dict = {}


# ---------------------------------------------------------------------------
# Cascading tags: agent-scoped baseline + agent tags
# ---------------------------------------------------------------------------


class SetBaselineRequest(BaseModel):
    """Designate (or clear, when ``agent_id`` is null) the baseline agent."""

    agent_id: str | None = None


class BaselineResponse(BaseModel):
    evaluation_id: str
    baseline_agent_id: str | None = None


class SetAgentTagsRequest(BaseModel):
    """Replace an agent's evaluation-scoped tag set (PUT)."""

    tags: dict


class AgentTagsResponse(BaseModel):
    evaluation_id: str
    agent_id: str
    tags: dict = {}
    is_baseline: bool = False


class RunSearchMetricFilter(BaseModel):
    key: str
    agg: str = "mean"  # mean | p50 | p95 | min | max
    op: str  # lt | lte | gt | gte | eq
    value: float


class RunSearchScoreFilter(BaseModel):
    # A new filter uses the two headline fields. `overall_score` and
    # `speed_score` read their legacy columns so saved filters on historical runs
    # keep working; they are `null` on runs scored since.
    field: Literal[
        "score",
        "speed_score",
        "overall_score",
        "f1_score",
        "quality_score",
        "performance_score",
    ] = "quality_score"
    op: str
    value: float


class RunSearchRequest(BaseModel):
    evaluation_id: str | None = None
    params: dict[str, str] | None = None  # exact key=value matches
    metrics: list[RunSearchMetricFilter] | None = None
    scores: list[RunSearchScoreFilter] | None = None
    tags: dict | None = None  # key or key=value (matched against effective tags)
    baseline_only: bool = False  # only runs whose agent is the evaluation baseline
    dataset_version: int | None = None
    created_after: str | None = None
    created_before: str | None = None
    limit: int = Field(100, ge=1, le=500)


class RunSearchResultItem(BaseModel):
    id: str
    evaluation_id: str
    agent_id: str | None = None
    flow_id: str | None = None
    model_id: str
    status: str
    score: float | None = None
    speed_score: float | None = None
    overall_score: float | None = None
    f1_score: float | None = None
    # Headline metrics. `None` = N/A, never `0`.
    quality_score: float | None = None
    performance_score: float | None = None
    created_at: str
    tags: dict | None = None
    # Cascading tags.
    effective_tags: dict = {}
    is_baseline: bool = False
    # Execution anomalies of the autonomous evaluation mode.
    anomaly_count: int = 0
    # Entries the platform could not run, left out of every headline.
    platform_error_count: int = 0
    # A placebo comparison's run: its arm and comparison, so a trial is not read
    # as an ordinary run; ``real`` and null on every other run.
    arm: str = "real"
    placebo_target: dict | None = None
    placebo_comparison_id: str | None = None


class RunSearchResponse(BaseModel):
    runs: list[RunSearchResultItem] = []
    count: int = 0


# --- Evaluation comparison and bundle review (#12282) ------------------------------------


class ComparedMetric(BaseModel):
    key: str
    label: str
    direction: str | None = None


class ComparedSubject(BaseModel):
    id: str
    kind: str
    name: str
    is_baseline: bool = False
    builtin_tools: list[str] = []
    custom_tool_count: int = 0
    mcp_server_count: int = 0
    skills: list[str] = []
    max_iterations: int | None = None
    temperature: float | None = None
    model: str | None = None


class ComparedDataset(BaseModel):
    id: str
    name: str
    entry_count: int
    instructions_digest: str | None = None
    description: str | None = None


class ComparedRun(BaseModel):
    id: str
    evaluation_id: str
    subject_id: str | None = None
    subject_name: str | None = None
    model_id: str
    is_default_model: bool = False
    created_at: str | None = None
    trigger_reason: str | None = None
    metric_suite_version: str | None = None
    execution_mode: str | None = None
    quality_score: float | None = None
    completed_entries: int = 0
    total_entries: int = 0
    completed_at: str | None = None
    #: The judge's run score; None when the run's judge was off.
    judge_score: float | None = None
    #: Runs with one key ran one setup (subject version, prompts, skills, settings, mode).
    setup_key: str | None = None
    setup: dict[str, Any] = {}
    rescore_error: str | None = None


class ComparedEvaluation(BaseModel):
    id: str
    name: str
    created_at: str | None = None
    family: str | None = None
    execution_mode: str | None = None
    timeout_seconds: int | None = None
    judge_on: bool = True
    evaluator_model: str | None = None
    judge_weights: dict[str, float] | None = None
    judge_instructions_digest: str | None = None
    subjects: list[ComparedSubject] = []
    datasets: list[ComparedDataset] = []
    runs: list[ComparedRun] = []
    run_choices: list[ComparedRun] = []


class ComparedEntryMember(BaseModel):
    evaluation_id: str
    entry_id: str | None = None
    position: int = 0
    has_file: bool = False


class ComparedEntry(BaseModel):
    key: str
    label: str
    members: list[ComparedEntryMember]


class ComparedCell(BaseModel):
    """One run's result on one entry: the pooled metrics' ``[numerator, denominator]``."""

    run_id: str
    entry_key: str
    result_id: str
    counted: bool
    anomaly: str | None = None
    metrics: dict[str, list[float]] = {}
    cost: float | None = None
    seconds: float | None = None
    #: ``[tp, fp, fn]`` of the metrics that report them (counted results only).
    counts: dict[str, list[float]] = {}
    #: The scorer could not read the answer.
    unreadable: bool = False
    judge_score: float | None = None


class EvaluationComparisonResponse(BaseModel):
    family: str
    #: The metric suite this platform scores with now.
    metric_suite_version: str | None = None
    metrics: list[ComparedMetric]
    evaluations: list[ComparedEvaluation]
    entries: list[ComparedEntry]
    cells: list[ComparedCell]


class BundleReviewEntry(BaseModel):
    label: str
    #: The entry's reference key, as ``GET /compare`` names its entries.
    key: str | None = None
    position: int = 0
    entry_count: int | None = None
    dataset_name: str | None = None
    dataset_description: str | None = None
    pages: list[str] = []
    file_name: str | None = None
    text_source: str | None = None


class BundleReviewAnswer(BaseModel):
    key: str
    result_id: str
    run_id: str
    evaluation_id: str
    evaluation_name: str
    model_id: str
    subject_id: str | None = None
    subject_name: str | None = None
    created_at: str | None = None
    judge_score: float | None = None
    judge_reasoning: str | None = None
    #: The suite the answer's run was scored by.
    metric_suite_version: str | None = None
    anomaly: str | None = None
    metrics: dict[str, float | None] = {}
    counts: dict[str, list[float]] = {}
    unreadable: bool = False
    cost: float | None = None
    seconds: float | None = None


class BundleReviewResponse(BaseModel):
    """An entry's expected STIX bundle against answers; shapes in ``services/evaluation/bundle_review.py``."""

    entry: BundleReviewEntry
    #: The suite this review reads the answers with (the platform's current one).
    metric_suite_version: str | None = None
    answers: list[BundleReviewAnswer]
    objects: list[dict[str, Any]]
    groups: list[int]
    sources: dict[str, list[int]]
    relationships: list[list[Any]]
    unreadable: list[str] = []


class BundleReviewsRequest(BaseModel):
    """Several entries' reviews at once: one group of result ids per entry."""

    model_config = ConfigDict(extra="forbid")

    groups: list[list[str]] = Field(min_length=1, max_length=200)
    include_text: bool = False
