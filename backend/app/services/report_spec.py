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

"""``ReportSpec`` — the validated contract for agent-generated reports.

This module is the **single source of truth** for what an executive report may
contain. The agent-facing ``generate_report`` tool, the REST read endpoint and
the tests all validate through :func:`validate_spec`, so the tool, the API and
the frontend renderer can never drift on the accepted shape.

Design constraints that shaped it
---------------------------------
* **Controlled blocks, not free-form HTML.** A report is a typed ``header``, an
  ordered list of blocks each declaring one of :data:`REPORT_BLOCKS`, and an
  optional ``footer``. Every block maps to a dedicated React component with a
  fixed prop set, and every byte of layout — the hero gradient, the KPI grid,
  the chain arrows, the timeline rails, the risk badges, the print stylesheet —
  lives in ``frontend/src/components/report/report.css``, authored by a human.

  This is the whole point of the feature. A CTI brief looks like 138 lines of
  bespoke CSS, but its *structure* is highly regular: a dozen repeated
  components with data in them. So the agent supplies data and nothing else.
  Letting a template carry raw ``<style>`` instead would be far cheaper and
  fatally wrong: ``template_authoring`` is agent-facing, so a template
  that can hold CSS is a template an *agent* can write, and the report renders
  in the app's own origin. The deck-side ban on ``<style>`` would become
  cosmetic, bypassable through the template door.
* **``rich_text`` is the only HTML escape hatch**, for paragraph-level prose
  inside a block. The guard lives in ``app.services.rich_text_guard`` and is
  shared with the deck spec, so "unsafe markup" means one thing platform-wide.
  It *rejects* here (loudly, at generation time, naming the field) and DOMPurify
  strips again at render time — defence in depth, not a replacement.
* **Fail loudly on malformed input.** Models forbid unknown fields: an LLM that
  invents ``{"block": "kpi_row", "kpis": [...]}`` gets a precise error naming
  the offending key and the allowed ones, which is what makes the retry
  productive. Silently ignoring extras would drop report content on the floor.
* **Bounded.** Every collection has a cap, so one tool call can neither blow up
  the JSON column nor produce a 300-page document nobody asked for.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.services.rich_text_guard import check_rich_text

# ── Catalog constants ────────────────────────────────────────────────────────

#: Visual themes. Rendered from platform design tokens in
#: ``frontend/src/components/report/report.css``. ``light`` is the default: a
#: report's primary output is a printed / PDF-exported document, and a dark
#: surface burns a cartridge and reads worse on paper.
REPORT_THEMES: tuple[str, ...] = ("light", "dark", "filigran")

#: Block catalog. Each entry has a matching React component; adding one here
#: without adding the component renders nothing for that block, so the two lists
#: are kept in sync deliberately (see ``BLOCK_COMPONENTS`` in
#: ``frontend/src/components/report/blocks.tsx``).
REPORT_BLOCKS: tuple[str, ...] = (
    "kpi_row",
    "prose",
    "decisions",
    "chain",
    "assessment_table",
    "scenarios",
    "risk_table",
    "matrix",
    "callout",
    "timeline",
    "checklist",
    "fill_in_box",
    "annex",
    "sources",
)

#: Confidence grades on an ``assessment_table`` row. The renderer maps each to a
#: colour; ``confidence_label`` carries the analyst's own wording ("faible à
#: moyenne") so a nuanced grade is not flattened into one of three words.
REPORT_CONFIDENCE_LEVELS: tuple[str, ...] = ("high", "medium", "low")

#: Risk levels on a ``risk_table`` row or a ``matrix`` cell, mapped to the badge
#: palette. Shared between the two on purpose: a report that grades an exposure
#: table one way and a matrix cell another would read as two different scales.
REPORT_RISK_LEVELS: tuple[str, ...] = (
    "critical",
    "elevated",
    "moderate",
    "controlled",
)

#: Bounds. Generous enough for a real executive brief, small enough that a
#: runaway generation cannot produce an unusable artifact or an oversized JSON
#: column.
MAX_BLOCKS = 40
MAX_TITLE_CHARS = 300
MAX_SHORT_TEXT_CHARS = 500
MAX_LONG_TEXT_CHARS = 1200
MAX_RICH_TEXT_CHARS = 6000
MAX_TAGS = 8
MAX_KPIS = 6
MAX_DECISIONS = 6
MAX_CHAIN_STEPS = 8
MAX_ASSESSMENT_ROWS = 24
MAX_SCENARIOS = 6
MAX_RISK_ROWS = 12
MAX_TIMELINE_PHASES = 8
MAX_PHASE_ITEMS = 10
MAX_CHECKLIST_ITEMS = 30
MAX_FILL_IN_FIELDS = 12
MAX_FILL_IN_NOTES = 4
MAX_ANNEX_BULLETS = 20
MAX_TABLE_COLUMNS = 6
MAX_TABLE_ROWS = 30
MAX_SOURCES = 30
#: A matrix is a readable grid, not a spreadsheet: past roughly 6x8 it stops
#: being something an executive reads and starts being data that belongs in a
#: table (or a file).
MAX_MATRIX_COLUMNS = 6
MAX_MATRIX_ROWS = 8
#: Section numbers are labels, not integers — real briefs use "01", "R", "A.2".
MAX_NUMBER_CHARS = 8

#: A source link must be a plain web URL. ``javascript:`` / ``data:`` are the two
#: classic ways a link executes or smuggles a document, and the frontend's
#: DOMPurify profile allows only http(s)/mailto — rejecting here as well means
#: the agent is told at generation time instead of finding a silently dropped
#: reference in the rendered bibliography.
_SAFE_URL_RE = re.compile(r"^https?://[^\s<>\"']{3,500}$", re.IGNORECASE)


# ── Building blocks ──────────────────────────────────────────────────────────


class _Strict(BaseModel):
    """Base for every spec model: unknown keys are an error, not a shrug."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Title = Annotated[str, Field(min_length=1, max_length=MAX_TITLE_CHARS)]
OptionalTitle = Annotated[str | None, Field(default=None, max_length=MAX_TITLE_CHARS)]
ShortText = Annotated[str, Field(min_length=1, max_length=MAX_SHORT_TEXT_CHARS)]
OptionalShortText = Annotated[
    str | None, Field(default=None, max_length=MAX_SHORT_TEXT_CHARS)
]
LongText = Annotated[str, Field(min_length=1, max_length=MAX_LONG_TEXT_CHARS)]
OptionalLongText = Annotated[
    str | None, Field(default=None, max_length=MAX_LONG_TEXT_CHARS)
]
RichText = Annotated[str | None, Field(default=None, max_length=MAX_RICH_TEXT_CHARS)]


def _check_url(value: str | None) -> str | None:
    """Reject anything that is not a plain http(s) URL."""
    if value is None:
        return None
    if not _SAFE_URL_RE.match(value):
        raise ValueError(
            "url must be a plain http(s) web address (javascript:, data: and "
            "relative links are refused). Omit it to list a source without a link."
        )
    return value


class Verdict(_Strict):
    """The analytic bottom line, rendered in the report header."""

    #: Short grade shown large ("MENACE ÉLEVÉE", "RISQUE MAÎTRISÉ").
    grade: Title
    #: Optional caption above the grade; the renderer supplies a default.
    label: OptionalTitle
    rich_text: RichText

    _validate_rich_text = field_validator("rich_text")(check_rich_text)


class ReportHeader(_Strict):
    """The report's masthead: what it is, as of when, for whom, and the verdict."""

    title: Title
    #: Small uppercase kicker above the title ("Cyber Threat Intelligence").
    eyebrow: OptionalTitle
    subtitle: OptionalShortText
    #: Framing chips — as-of date, classification, audience, confidence.
    tags: Annotated[list[ShortText], Field(default_factory=list, max_length=MAX_TAGS)]
    verdict: Verdict | None = None


class KpiItem(_Strict):
    """One figure in a ``kpi_row``."""

    #: The number itself, pre-formatted by the agent ("≈ 74–75k", "> 21,6k").
    value: ShortText
    label: ShortText
    #: Caveat line under the figure — where a count comes from, why it varies.
    note: OptionalShortText


class Alert(_Strict):
    """A called-out caveat inside a ``prose`` block."""

    label: OptionalTitle
    rich_text: Annotated[str, Field(min_length=1, max_length=MAX_RICH_TEXT_CHARS)]

    _validate_rich_text = field_validator("rich_text")(check_rich_text)


class DecisionItem(_Strict):
    """One decision asked of the reader — or one mechanism, driver or finding.

    ``confidence`` is here because the block is also how a brief lays out a set of
    *analytic* items (the coercion mechanisms of a threat dossier, say), and each
    of those carries its own confidence — "high on the power to inspect, variable
    on its application". That is a judgement about the item, not decoration.
    """

    title: Title
    rich_text: RichText
    text: OptionalLongText
    #: Overline such as "DECISION 01"; the renderer numbers them when omitted.
    label: OptionalTitle
    confidence: Literal["high", "medium", "low"] | None = None
    #: The analyst's own wording, when the three grades are too coarse.
    confidence_label: OptionalShortText

    _validate_rich_text = field_validator("rich_text")(check_rich_text)


class ChainStep(_Strict):
    """One stage of an attack / process chain."""

    title: Title
    text: OptionalLongText


class AssessmentRow(_Strict):
    """One judgement, what kind of evidence backs it, and how much confidence."""

    statement: LongText
    confidence: Literal["high", "medium", "low"]
    #: The analyst's own wording, when the three grades are too coarse.
    confidence_label: OptionalShortText
    #: What KIND of evidence this is — "public decision", "local report", "legal
    #: framework", "allegation", "official attribution". Deliberately a free
    #: string and not an enum: the taxonomy is analyst vocabulary and differs per
    #: shop, so an enum would refuse the first brief that says "vendor advisory".
    nature: OptionalShortText
    #: The assessment itself, when the shop separates the claim from its appraisal
    #: ("credible and consistent with the context, but single-sourced").
    appraisal: OptionalLongText


class ScenarioItem(_Strict):
    """One way the risk materialises."""

    title: Title
    text: OptionalLongText
    #: What it costs if it happens.
    impact: OptionalShortText


class RiskRow(_Strict):
    """One exposure situation, its level and the posture it calls for."""

    situation: LongText
    level: Literal["critical", "elevated", "moderate", "controlled"]
    posture: OptionalLongText
    #: Display wording for the badge ("FAIBLE DIRECT"); defaults to the level.
    level_label: OptionalShortText


class TimelinePhase(_Strict):
    """One phase of a plan: a deadline and what happens before it."""

    #: The deadline itself ("0-4 hours", "Within 30 days").
    label: Title
    #: One-line summary of the phase's intent.
    headline: OptionalShortText
    items: Annotated[
        list[LongText], Field(default_factory=list, max_length=MAX_PHASE_ITEMS)
    ]


class FillInField(_Strict):
    """One row of a form the reader completes.

    ``value`` left unset renders a blank rule to write on — that is the point of
    the block, so it is optional rather than required.
    """

    label: Title
    value: OptionalShortText


class BlockTable(_Strict):
    """A simple headed table, used inside an ``annex``.

    Rows are validated to match the header width so the rendered table can never
    come out ragged — a mismatch is an error the agent can fix, not a visual bug
    the reader has to interpret.
    """

    headers: Annotated[
        list[ShortText], Field(min_length=1, max_length=MAX_TABLE_COLUMNS)
    ]
    rows: Annotated[
        list[Annotated[list[str], Field(max_length=MAX_TABLE_COLUMNS)]],
        Field(min_length=1, max_length=MAX_TABLE_ROWS),
    ]

    @field_validator("rows")
    @classmethod
    def _rows_match_headers(
        cls, rows: list[list[str]], info: object
    ) -> list[list[str]]:
        headers = getattr(info, "data", {}).get("headers")
        if not headers:
            return rows
        width = len(headers)
        for index, row in enumerate(rows):
            if len(row) != width:
                raise ValueError(
                    f"row {index + 1} has {len(row)} cell(s) but the table has "
                    f"{width} header(s) — every row must have exactly one cell "
                    "per header (use an empty string for a blank cell)."
                )
            for cell in row:
                if len(cell) > MAX_LONG_TEXT_CHARS:
                    raise ValueError(
                        f"row {index + 1} has a cell longer than "
                        f"{MAX_LONG_TEXT_CHARS} characters."
                    )
        return rows


class SourceItem(_Strict):
    """One reference in a ``sources`` block."""

    label: Title
    url: Annotated[str | None, Field(default=None, max_length=500)]
    note: OptionalShortText
    #: Citation handle ("S01", "S04 · FIELD REPORT") so body text can point at
    #: this entry. Free-form: the renderer displays it, nothing resolves it.
    ref: OptionalShortText

    _validate_url = field_validator("url")(_check_url)


class MatrixCell(_Strict):
    """One graded intersection of a ``matrix``."""

    label: ShortText
    level: Literal["critical", "elevated", "moderate", "controlled"]


class MatrixRow(_Strict):
    """One row of a ``matrix``: its axis label plus one cell per column."""

    label: Title
    cells: Annotated[
        list[MatrixCell], Field(min_length=1, max_length=MAX_MATRIX_COLUMNS)
    ]


# ── Report blocks ────────────────────────────────────────────────────────────


class _BlockBase(_Strict):
    """Fields every block shares."""

    #: Section heading. Optional so a block can continue the previous section
    #: without repeating its title.
    title: OptionalTitle
    #: Section number shown beside the heading ("01", "R", "A.2"). A *string*,
    #: not an integer: real briefs number an annex "R" or "A.2", and an int would
    #: refuse both. Optional, and the renderer never invents one — a report that
    #: does not number its sections must not acquire numbering by accident.
    #: Also what the derived table of contents cites.
    number: Annotated[str | None, Field(default=None, max_length=MAX_NUMBER_CHARS)]


class KpiRowBlock(_BlockBase):
    block: Literal["kpi_row"]
    items: Annotated[list[KpiItem], Field(min_length=1, max_length=MAX_KPIS)]


class ProseBlock(_BlockBase):
    block: Literal["prose"]
    #: Opening paragraph, rendered larger than the body.
    lead: RichText
    rich_text: RichText
    alert: Alert | None = None

    _validate_lead = field_validator("lead")(check_rich_text)
    _validate_rich_text = field_validator("rich_text")(check_rich_text)


class DecisionsBlock(_BlockBase):
    block: Literal["decisions"]
    items: Annotated[list[DecisionItem], Field(min_length=1, max_length=MAX_DECISIONS)]


class ChainBlock(_BlockBase):
    block: Literal["chain"]
    steps: Annotated[list[ChainStep], Field(min_length=1, max_length=MAX_CHAIN_STEPS)]
    #: Caveat under the chain — what it does and does not claim.
    note: OptionalLongText


class AssessmentTableBlock(_BlockBase):
    block: Literal["assessment_table"]
    rows: Annotated[
        list[AssessmentRow], Field(min_length=1, max_length=MAX_ASSESSMENT_ROWS)
    ]


class ScenariosBlock(_BlockBase):
    block: Literal["scenarios"]
    items: Annotated[list[ScenarioItem], Field(min_length=1, max_length=MAX_SCENARIOS)]


class RiskTableBlock(_BlockBase):
    block: Literal["risk_table"]
    rows: Annotated[list[RiskRow], Field(min_length=1, max_length=MAX_RISK_ROWS)]


class MatrixBlock(_BlockBase):
    """A double-entry grid: one graded cell per row × column intersection.

    The shape a risk matrix actually has, and the one thing ``risk_table`` cannot
    express — a table grades one situation per row, a matrix grades every
    combination of two variables (travel context × device profile, likelihood ×
    impact, asset class × control maturity). Flattening a 4×4 matrix into a table
    loses 12 of its 16 judgements.

    Rows are validated to match the column count so the rendered grid can never
    come out ragged — a mismatch is an error the agent can fix, not a visual bug
    the reader has to interpret.
    """

    block: Literal["matrix"]
    #: Top-left corner label, naming what the two axes are ("Profile / device").
    axis_label: OptionalTitle
    #: Column headers — the horizontal axis.
    column_labels: Annotated[
        list[ShortText], Field(min_length=1, max_length=MAX_MATRIX_COLUMNS)
    ]
    rows: Annotated[list[MatrixRow], Field(min_length=1, max_length=MAX_MATRIX_ROWS)]
    #: What the grading means, or what it deliberately does not claim.
    note: OptionalLongText

    @field_validator("rows")
    @classmethod
    def _rows_match_columns(cls, rows: list[MatrixRow], info: object) -> list[MatrixRow]:
        headers = getattr(info, "data", {}).get("column_labels")
        if not headers:
            return rows
        width = len(headers)
        for index, row in enumerate(rows):
            if len(row.cells) != width:
                raise ValueError(
                    f"row {index + 1} ('{row.label}') has {len(row.cells)} cell(s) "
                    f"but the matrix has {width} column(s) — every row needs "
                    "exactly one cell per column label."
                )
        return rows


class CalloutBlock(_BlockBase):
    block: Literal["callout"]
    rich_text: RichText
    #: Headline figure on the right ("4/5"), with its caption underneath.
    score: OptionalShortText
    score_label: OptionalShortText

    _validate_rich_text = field_validator("rich_text")(check_rich_text)


class TimelineBlock(_BlockBase):
    block: Literal["timeline"]
    phases: Annotated[
        list[TimelinePhase], Field(min_length=1, max_length=MAX_TIMELINE_PHASES)
    ]


class ChecklistBlock(_BlockBase):
    block: Literal["checklist"]
    items: Annotated[list[LongText], Field(min_length=1, max_length=MAX_CHECKLIST_ITEMS)]
    #: Print in one or two columns. Two is the default — a 10-question list in a
    #: single column wastes half the page in the printed brief.
    columns: Literal[1, 2] = 2


class FillInBoxBlock(_BlockBase):
    block: Literal["fill_in_box"]
    fields: Annotated[
        list[FillInField], Field(min_length=1, max_length=MAX_FILL_IN_FIELDS)
    ]
    #: Status chips under the fields ("Status: NOT ASSESSED", "Due: 72 H").
    notes: Annotated[
        list[ShortText], Field(default_factory=list, max_length=MAX_FILL_IN_NOTES)
    ]


class AnnexBlock(_BlockBase):
    block: Literal["annex"]
    #: The clickable summary line of the collapsible section.
    summary: Title
    items: Annotated[
        list[LongText], Field(default_factory=list, max_length=MAX_ANNEX_BULLETS)
    ]
    rich_text: RichText
    table: BlockTable | None = None
    note: OptionalLongText
    #: Start expanded on screen. Default closed; every annex prints open (the
    #: print variant of ``ReportView``, which the PDF export renders).
    open: bool = False

    _validate_rich_text = field_validator("rich_text")(check_rich_text)


class SourcesBlock(_BlockBase):
    block: Literal["sources"]
    intro: OptionalLongText
    items: Annotated[list[SourceItem], Field(min_length=1, max_length=MAX_SOURCES)]
    #: Closing note on method and limits.
    limits: RichText
    open: bool = True

    _validate_limits = field_validator("limits")(check_rich_text)


ReportBlock = Annotated[
    Union[  # noqa: UP007 - Pydantic needs the explicit Union for the discriminator
        KpiRowBlock,
        ProseBlock,
        DecisionsBlock,
        ChainBlock,
        AssessmentTableBlock,
        ScenariosBlock,
        RiskTableBlock,
        MatrixBlock,
        CalloutBlock,
        TimelineBlock,
        ChecklistBlock,
        FillInBoxBlock,
        AnnexBlock,
        SourcesBlock,
    ],
    Field(discriminator="block"),
]


class ReportSpec(_Strict):
    """A complete, renderable report."""

    title: Title
    theme: Literal["light", "dark", "filigran"] = "light"
    header: ReportHeader
    blocks: Annotated[list[ReportBlock], Field(min_length=1, max_length=MAX_BLOCKS)]
    #: Single line at the foot of every page ("… · TLP:CLEAR · printable").
    footer: OptionalShortText


# ── Validation entry point ───────────────────────────────────────────────────


class ReportSpecError(ValueError):
    """A ``ReportSpec`` payload that failed validation.

    Carries an agent-readable, field-addressed message: the tool returns it
    verbatim as a tool error so the model can fix the exact block that is wrong
    instead of regenerating the whole report blindly.
    """


def _format_errors(exc: ValidationError) -> str:
    """Render Pydantic errors as short, addressable lines for an LLM.

    ``blocks.3.rows.1.confidence: Input should be 'high', 'medium' or 'low'``
    beats a JSON dump of Pydantic's error objects: the location is the path the
    agent used to build the payload, so it can patch that one field.
    """
    lines: list[str] = []
    for err in exc.errors():
        parts = [str(part) for part in err["loc"]]
        # Pydantic inserts the resolved union member into the path for a
        # discriminated union (``blocks.2.kpi_row.items``), which reads like a
        # nested field called "kpi_row". Move it out into an explicit
        # ``(block=…)`` note so the remaining path is the one the agent built.
        block_note = ""
        if len(parts) >= 3 and parts[0] == "blocks" and parts[2] in REPORT_BLOCKS:
            block_note = f" (block={parts[2]})"
            parts = parts[:2] + parts[3:]
        loc = ".".join(parts) or "(root)"
        msg = err.get("msg", "invalid value")
        lines.append(f"  - {loc}{block_note}: {msg}")
        if len(lines) >= 15:
            lines.append(f"  - … and {len(exc.errors()) - 15} more error(s)")
            break
    return "\n".join(lines)


def validate_spec(payload: object) -> dict:
    """Validate a raw spec payload and return its normalized JSON-safe dict.

    Raises :class:`ReportSpecError` with a multi-line, field-addressed message on
    any problem. The returned dict is what gets persisted in ``Report.spec`` —
    never the raw agent input.
    """
    if not isinstance(payload, dict):
        raise ReportSpecError(
            "The report spec must be a JSON object with a 'title', a 'header' "
            "and a 'blocks' array."
        )
    try:
        spec = ReportSpec.model_validate(payload)
    except ValidationError as exc:
        raise ReportSpecError(
            "The report spec is invalid and was NOT saved. Fix these field(s) "
            "and call the tool again:\n"
            f"{_format_errors(exc)}\n"
            f"Allowed blocks: {', '.join(REPORT_BLOCKS)}. Each block accepts a "
            "fixed set of keys — see the tool description."
        ) from exc
    return spec.model_dump(mode="json", exclude_none=True)


def block_catalog() -> dict[str, list[str]]:
    """Map each block id to the field names it accepts.

    Used to build the tool description (and the tool's error hints) from the
    models themselves, so the prompt can never document a field the validator
    rejects.
    """
    models: dict[str, type[BaseModel]] = {
        "kpi_row": KpiRowBlock,
        "prose": ProseBlock,
        "decisions": DecisionsBlock,
        "chain": ChainBlock,
        "assessment_table": AssessmentTableBlock,
        "scenarios": ScenariosBlock,
        "risk_table": RiskTableBlock,
        "matrix": MatrixBlock,
        "callout": CalloutBlock,
        "timeline": TimelineBlock,
        "checklist": ChecklistBlock,
        "fill_in_box": FillInBoxBlock,
        "annex": AnnexBlock,
        "sources": SourcesBlock,
    }
    return {
        block: [name for name in model.model_fields if name != "block"]
        for block, model in models.items()
    }
