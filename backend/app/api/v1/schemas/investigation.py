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

"""Pydantic schemas for the investigation API routes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class InvestigationStepResponse(BaseModel):
    """One source query, as the block renders it.

    ``step_status`` rather than ``status`` matches the streamed frame payload exactly
    — the block hydrates from REST and merges frames into the same objects, so
    the two paths share one field list (see ``services.investigations``).

    Every optional field is present-and-null rather than omitted, because the
    block distinguishes "no confidence" from "not sent yet".
    """

    step_id: str | None = None
    position: int
    step_status: str
    source_name: str
    source_pack_slug: str | None = None
    query: str | None = None
    error_type: str | None = None
    status_detail: str | None = None
    #: Localizable form of ``status_detail``; the client renders this and falls
    #: back to the free text only where no code exists.
    detail_code: str | None = None
    detail_params: dict | None = None
    findings_count: int
    confidence: float | None = None
    evidence: list[dict] = []
    #: The prose this source wrote, when it wrote any. The spans in
    #: ``evidence`` are offsets into THIS string, so a client that renders a
    #: citation inline must use the two together or place its markers by guess.
    answer: str | None = None
    iteration: int | None = None
    lead_from_step_id: str | None = None
    #: What a goal-plan hop queried when it is not the run's own subject (the
    #: IP a pivot reads, a neighbouring site), and the plan action the step
    #: serves: the pack's declared one, else the default for its kind.
    subject_value: str | None = None
    action: str | None = None


class GoalActionResponse(BaseModel):
    """One action of a run's goal plan."""

    slug: str
    #: English, and a key of the UI's catalogs.
    label: str
    description: str
    cost: int
    preconditions: list[str] = []
    effects: list[str] = []
    #: One of `investigation_packs.GOAL_ACTION_ICONS`.
    icon: str
    #: The action whose work is the run's report rather than a source query.
    produces_report: bool = False
    #: Whether a source of this run can do the action's work (its own steps,
    #: or the action that feeds it hops). False is said up front, not drawn
    #: pending until the run ends.
    servable: bool = True


class GoalPredicateResponse(BaseModel):
    slug: str
    label: str


class GoalPlanResponse(BaseModel):
    """The goal plan the block draws: every run carries one.

    A pack's declared plan as A* paths it for the run's subject type, or the
    default plan read from the kinds of source the run planned. What HOLDS is
    not sent: the client derives it from the steps, so it cannot run ahead of
    what the sources answered.
    """

    pack_slug: str | None = None
    declared: bool
    #: False when no action sequence reaches the goal from the subject's state.
    reachable: bool = True
    #: A catalog key with a ``{value}`` placeholder for the subject.
    objective: str
    predicates: list[GoalPredicateResponse] = []
    initial_state: list[str] = []
    goal_state: list[str] = []
    actions: list[GoalActionResponse] = []
    path_cost: int = 0


class ExecutiveSummaryRequestResponse(BaseModel):
    """What "Generate executive summary" sends to the conversation's agent."""

    #: The built-in deck the summary is made with (XTM One Brand Deck).
    template_name: str
    #: The instruction itself, sent as the chat message the agent acts on.
    prompt: str


class OpenCTIRecordRequestResponse(BaseModel):
    """What "Send to OpenCTI" sends: an instruction for the conversation's
    agent, which records the findings with the OpenCTI integration's tools."""

    #: The OpenCTI integration the turn needs, added to its selection.
    integration_id: str
    prompt: str


class InvestigationSummaryResponse(BaseModel):
    """Derived counts for the block's collapsed line.

    Computed server-side, not by the client: ``all_succeeded`` gates the
    "everything passed" affordance, and one rule must decide it on every
    surface (block, completion frame, report).
    """

    total: int
    by_status: dict[str, int] = {}
    all_succeeded: bool
    failed: int
    unfinished: int
    #: Null whenever no step carries a confidence — never defaulted.
    confidence: float | None = None
    #: How many steps `confidence` is the mean of. It is an unweighted mean
    #: over the steps that carry one, so a single confident step among ten
    #: that carry none reads as the run's confidence unless this is rendered
    #: with it. Nothing renders the aggregate confidence today; this ships
    #: alongside it so that nothing can.
    confidence_samples: int = 0


class InvestigationResponse(BaseModel):
    """A whole run, sufficient to render the block with no frames at all."""

    id: str
    subject_type: str
    subject_value: str
    source_pack_slugs: list[str] = []
    status: str
    end_reason: str | None = None
    end_reason_code: str | None = None
    end_reason_params: dict | None = None
    #: Leads found but never planned, because the plan cap was reached.
    leads_dropped: int = 0
    max_iterations: int
    iterations_used: int
    revision: int
    #: Measured wall clock, or null while the run is unfinished. Never derived
    #: from the step count.
    duration_seconds: float | None = None
    conversation_id: str | None = None
    assignment_run_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    #: The synthesised report across every source. Null is a real
    #: outcome, not a gap: nothing was retained to stand on, no model was
    #: available, or the draft kept citing excerpts that were not supplied —
    #: and nothing ungrounded is published.
    report: str | None = None
    #: The numbered passages the report ACTUALLY cites, in the order it cites
    #: them. Only the cited ones: listing every passage offered would present
    #: material the report never used as though it backed it.
    #:
    #: `label` and `href` are UNTREATED third-party text — a fetched page's
    #: `<title>`, an MCP server's citation — stored as they arrived. Any
    #: renderer must put them through `investigation_report._safe_label` and
    #: `_safe_href` first; both are hostile in the markdown sense (a title
    #: closes its link, an address closes its own `(...)` slot) and neither is
    #: made safe by storing it. `investigation_report` is the single escaping
    #: boundary, deliberately: treating at rest would mangle the reference the
    #: reader needs while still leaving the next renderer to guess.
    report_sources: list[dict] = []
    #: How many sentences assert something, and how many of those carry no
    #: marker. Disclosed rather than enforced — see `investigation_report`.
    report_claims: int | None = None
    report_uncited_claims: int | None = None
    #: Set when this run continued an earlier one whose budget ran out.
    continues_investigation_id: str | None = None
    goal_plan: GoalPlanResponse | None = None
    #: Null until the run has a report to summarise.
    executive_summary: ExecutiveSummaryRequestResponse | None = None
    #: Whether an OpenCTI registered with this platform is available to the
    #: reader, so "Send to OpenCTI" is offered only when it can work.
    opencti_available: bool = False
    #: "Send to OpenCTI", once there is a report and an OpenCTI to record it in.
    opencti_record: OpenCTIRecordRequestResponse | None = None
    #: Where the cited report stands: ``written``, ``writing`` (its writer
    #: said so and has not returned), ``none`` (no report is coming) or
    #: ``unknown`` (the writer's marker could not be read).
    report_status: Literal["written", "writing", "none", "unknown"] = "unknown"
    steps: list[InvestigationStepResponse] = []
    #: True when the read was capped at ``MAX_STEPS``, so the steps above are a
    #: sample. A sample cannot prove every step succeeded, which is why
    #: ``summary.all_succeeded`` is forced false alongside it.
    steps_truncated: bool = False
    summary: InvestigationSummaryResponse


class PackChoiceModel(BaseModel):
    """One value of a pack option."""

    value: str
    label: str
    description: str


class PackOptionModel(BaseModel):
    """A choice a pack offers before a run, rendered as the server sends it:
    the picker names no pack. Sent back under ``source_pack_options``."""

    key: str
    label: str
    description: str
    default: str
    choices: list[PackChoiceModel]


class SourcePackOption(BaseModel):
    """One pack as the picker renders it.

    ``applicable_source_count`` is the field that keeps the picker honest, and
    the reason this is not just a list of pack names: a pack's *affinity* can
    cover a subject type while none of its **sources** apply to it. The
    built-in ``web-general`` declares every subject type but has no source for
    a file hash, so it would otherwise be offered as "recommended" for a hash
    and then query nothing. The picker shows the count and disables a pack that
    would contribute no source.
    """

    slug: str
    label: str
    description: str
    subject_types: list[str] = []
    #: Whether the recommendation covers this subject type.
    recommended: bool = False
    #: Sources in this pack that apply to the classified subject type.
    applicable_source_count: int
    #: Sources in the pack overall, for context on how narrow the match is.
    total_source_count: int
    #: Names of the applicable sources, so the picker can say WHAT it will query.
    applicable_source_names: list[str] = []
    max_iterations: int
    #: Choices this pack offers for the classified subject type.
    options: list[PackOptionModel] = []
    #: Whose the pack is, from its row: ``builtin`` (code), ``mine``,
    #: ``company`` (company-managed) or ``shared`` (someone else's).
    origin: Literal["builtin", "mine", "company", "shared"] = "builtin"
    #: The owner's name for a ``shared`` pack, else None.
    owner_name: str | None = None
    #: The hosts the pack's free HTTP sources (URLs a user typed) send this
    #: subject to, built and parsed as the run's fetch does.
    destinations: list[str] = []


class SubjectPlanResponse(BaseModel):
    """What the picker needs after the user submits a subject.

    Classification is server-side because the classifier is server-side —
    a deterministic regex in ``investigation_packs``. Duplicating it in
    TypeScript would create two answers to "what kind of subject is this?",
    and the one that picks the sources must be the one shown to the user.
    """

    subject: str
    subject_type: str
    #: How the run will name the subject: a list's summary (`list_summary`),
    #: else the subject itself.
    subject_summary: str = ""
    packs: list[SourcePackOption] = []
    #: True when no selectable pack contributes a single applicable source —
    #: the picker says so instead of offering a run that cannot query anything.
    has_applicable_source: bool


class ContinuationStepOption(BaseModel):
    """One source a previous run planned and never queried."""

    step_id: str
    source_name: str
    source_pack_slug: str | None = None
    #: Position in the parent's plan, so the block can offer the remainder in
    #: the order the analyst already saw rather than re-sorted.
    position: int
    #: What kind of source it is (``http`` / ``knowledge_base`` / ``mcp_tool``
    #: / ``web_search``). Shown because "no external source was reached" is a
    #: different gap from "no corpus was searched", and the kind is what says
    #: which one this would close.
    kind: str


class ContinuationCoverage(BaseModel):
    """What the parent run reached — counted, never judged.

    Structured rather than a sentence, and deliberately so: this codebase
    renders reasons from codes plus params so the reader sees them in their own
    language (see ``DETAIL_CODES``). A backend-composed English assessment
    would be the one part of the block that could not be translated.

    It is also why there is no model call here. A hallucinated "not worth
    continuing" would close an investigation nobody decided to close — the same
    reasoning that made ``classify_subject`` a regex instead of a prompt.
    """

    queried: int
    unqueried: int
    findings: int
    #: Sources that answered "nothing found". Neutral, not failure — and the
    #: outcome that most often makes continuing worthwhile.
    empty: int
    failed: int
    packs_reached: list[str] = []
    #: Packs the plan named and the budget never touched at all. The
    #: structural gap worth surfacing: a verdict resting only on the sources
    #: that happened to come first is a different claim from one that
    #: consulted every kind the plan named.
    packs_untouched: list[str] = []


class ContinuationOfferResponse(BaseModel):
    """The enriched block shown when a run stopped short."""

    investigation_id: str
    subject_value: str
    subject_type: str
    #: Whether anything can be continued right now.
    available: bool
    #: Why not, when not: ``chain_budget`` / ``chain_depth`` /
    #: ``nothing_left`` / ``nothing_addressable``. A code, for the same
    #: localization reason as ``ContinuationCoverage``.
    blocked_reason: str | None = None
    #: Queries this chain has already spent, summed across every run in it.
    #: The "rappel du coût actuel" — cumulative, because a per-run figure
    #: would understate what continuing has already cost.
    spent: int
    #: How many runs deep the chain is. 1 for an original run.
    chain_depth: int
    #: What the chain may still spend, after the cumulative cap.
    remaining_allowance: int
    #: The cap itself, so the block can show spend against it rather than a
    #: bare remainder.
    chain_max: int
    #: Human label per pack slug. Sent rather than mapped client-side: the
    #: first version kept that table in the panel and it drifted immediately,
    #: rendering `web-research` as its raw slug next to a labelled pack.
    pack_labels: dict[str, str] = {}
    steps: list[ContinuationStepOption] = []
    #: Sources that cannot be offered, as ``[name, reason]`` — reason being
    #: ``no_address`` / ``unreadable_address`` / ``no_longer_permitted``.
    #: Named rather than dropped: a remainder that silently shrinks is exactly
    #: the coverage gap this design exists to prevent.
    withheld: list[list[str]] = []
    coverage: ContinuationCoverage


class ContinuationRequest(BaseModel):
    """Which of the remaining sources to spend on."""

    step_ids: list[str] = Field(min_length=1)


class ContinuationStartedResponse(BaseModel):
    """The child run a continuation produced."""

    investigation_id: str
    continues_investigation_id: str
    #: Sources actually queued, after the chain cap truncated the selection.
    queued: int
    #: Selected ids that could not be run — stale, or withheld between
    #: rendering the block and clicking it. Reported rather than raised: "these
    #: ran, that one did not" beats a failed request that ran nothing.
    rejected_step_ids: list[str] = []
    #: The assistant text the run produced, so a caller outside a chat turn
    #: still gets the one-liner rather than only an id.
    message: str = ""
