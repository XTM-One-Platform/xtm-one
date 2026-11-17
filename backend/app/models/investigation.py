# SPDX-License-Identifier: Apache-2.0

"""Deep Investigation run + per-step state.

An ``Investigation`` is the persisted record of one Deep Investigation run, and
``InvestigationStep`` is one source query inside it.  The pair exists so a run
is **legible while it runs**: the live investigation block in the chat renders
from these rows, and the final report's ``## Sources queried`` section is
generated from the same rows, so the two cannot disagree.

Why the state lives in the database, not in the loop
----------------------------------------------------
The central constraint is that step state is derived from the
orchestrator and never parsed from model prose.  Persisting it here is what
makes that constraint hold in practice:

* **A status is a fact about the run**, written by the orchestrator when it
  queries a source — not a claim the LLM made about its own progress.
* **The block survives a reload.**  Streamed frames are advisory
  accelerations of state already committed here; a page refresh mid-run, a
  late join or an unavailable bridge all still render the correct block from
  these rows alone.
* **One dataset, two renderings.**  The live block and the report section read
  the same rows, so a source that timed out cannot appear as a success in one
  view and a failure in the other.

Why a failed step cannot be stored as a success
-----------------------------------------------
The prior art this issue reacts to (a public GOAP research demo) marked every
step ``Complete`` in green even when 3 of its 8 model calls had failed, and
reported a hardcoded ``94%`` confidence next to an empty answer pane.  That is
the default outcome when the display is not wired to execution state.  Hence
the seven-state vocabulary below distinguishes *queried and found* from
*queried and empty*, *partial*, *failed* and *never reached* — and
``confidence`` is nullable on purpose: a value that cannot be derived from
findings is absent, never defaulted.

Nothing here holds findings content.  A step carries counts, evidence URLs and
provenance; the prose belongs to the report artifact.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# ---------------------------------------------------------------------------
# Status vocabularies
#
# Declared here, next to the columns that store them, so the orchestrator, the
# state service, the API serializers and the tests all read one definition.
# Stored as plain strings rather than a PG enum: adding a state must not
# require an ``ALTER TYPE`` on a hot table during a rolling deploy, and the
# allowed set is enforced in the service layer (``investigation_state``).
# ---------------------------------------------------------------------------

#: A run's lifecycle.
INVESTIGATION_STATUSES = (
    "planning",  # source packs resolved, skeleton being written
    "running",  # loop in progress
    "completed",  # loop finished on its own terms
    "cancelled",  # user stopped it
    "aborted",  # timeout / unrecoverable error
)

#: One step's state.  Ordered from "not started" to "will not happen".
STEP_STATUSES = (
    "pending",  # planned, not started
    "active",  # being queried right now
    "completed",  # queried, findings extracted
    "empty",  # queried successfully, nothing found — NOT a success
    "degraded",  # partial result (rate-limited, truncated, auth-limited)
    "error",  # timeout, auth failure, unreachable, schema failure
    "skipped",  # never reached (budget spent, circular lead)
)

#: States a step can never leave.  Guards the "a failure never becomes a
#: success" invariant in ``investigation_state``.
TERMINAL_STEP_STATUSES = ("completed", "empty", "degraded", "error", "skipped")

#: The only state that may render as a success.  ``empty`` is deliberately
#: excluded: a source that answered "nothing found" is a neutral outcome the
#: analyst must be able to tell apart from a source that produced findings.
SUCCESS_STEP_STATUSES = ("completed",)

#: Machine-readable reasons, so the UI can render them in the reader's
#: language instead of the English sentence the backend happened to write.
#:
#: A code is paired with ``*_params`` (a JSON dict) that fills its
#: placeholders. The free-text ``status_detail`` / ``end_reason`` columns stay
#: as the FALLBACK for the one family that cannot be localized: a message
#: lifted from a third-party exception (``str(httpx.ConnectError)``). A code
#: therefore wins wherever one exists, and the raw text degrades to today's
#: behaviour where none can.
DETAIL_CODES: tuple[str, ...] = (
    # Run-level reasons. A step the run never reached carries the run's reason,
    # because "why was this source not queried" and "why did the run end" are
    # the same question from two directions.
    "run.all_sources_queried",
    "run.budget_spent",  # {budget}
    "run.no_covering_source",  # {subject_type}
    "run.cancelled",
    "run.interrupted",
    # Per-source outcomes.
    "source.timed_out",  # {seconds}
    "source.http_status",  # {status}
    "source.free_http_refused",  # {}
    "source.free_http_capped",  # {}
    "source.truncated",  # {max_chars}
    "source.thin_response",  # {chars}
    "source.querier_error",  # the raw message stays in the text fallback
    # Knowledge-base outcomes.
    "source.kb_provider_unreachable",
    "source.kb_search_failed",
    "source.kb_capped",  # {top_k}
    "source.kb_unavailable",
    "source.kb_below_floor",  # {floor, examined}
    # MCP outcomes. First-party every one of them: these say what is wrong
    # with the PACK or the SERVER RECORD, not what a third party replied, so
    # unlike `source.querier_error` they are translatable and carry params.
    "source.mcp_bad_server_id",  # {server_id}
    "source.mcp_server_missing",  # {server_id}
    "source.mcp_server_disabled",  # {server}
    "source.mcp_tool_missing",  # {server, tool}
    "source.mcp_argument_ambiguous",  # {tool}
    "source.mcp_tool_error",  # the tool's own message stays in the fallback
    # Site fingerprint and passive-DNS outcomes (disinformation
    # infrastructure pack).
    "source.fingerprint_matched",  # {matched, tested}
    "source.fingerprint_capped",  # {max_bytes}
    "source.vt_unavailable",
    "source.passive_dns_filtered",  # {kept, fetched} + optional {shown}
    "source.passive_dns_filtered_more",  # {kept, fetched} + optional {shown}
    "source.passive_dns_more",  # {limit}
    "source.passive_dns_seeds",  # {resolved, seeds, ips, shared, pivoted} + optional {set_aside, not_read, failed}
    "source.opencti_unavailable",
    "source.opencti_known",  # {known, checked}
    "source.opencti_none_known",  # {checked}
)

#: Codes whose text CANNOT be translated, because it was lifted from something
#: a third party wrote — an exception message, a tool's own error string.
#:
#: They still carry a code: it classifies the failure for metrics and for
#: filtering even when the words cannot be localized. What they must not have
#: is a translated message in the block, because that message would REPLACE the
#: only diagnostic the case has. Declared here rather than listed in the test
#: that enforces it, so adding one is a decision made where the code is.
UNTRANSLATABLE_DETAIL_CODES: tuple[str, ...] = (
    "source.querier_error",
    "source.kb_provider_unreachable",
    "source.kb_search_failed",
    "source.mcp_tool_error",
)

#: The parameter names each code ships, as the single source of truth.
#:
#: This exists because the guard it replaces did not work. That guard read the
#: two service modules as TEXT, matching `detail_code="..."` followed by
#: `detail_params={...}` on the next line — which sees two of the nine coded
#: messages. The five `run.*` codes assign to a local (`reason_params = {...}`)
#: that no regex over the file can follow, and two more are written as
#: conditional expressions. The rest were skipped by an `if code in emitted`,
#: silently, so renaming `{budget}` left the suite green and the footer
#: rendering `Iteration budget spent ({budget})` verbatim in four languages.
#:
#: Declared here rather than in the test so the block's messages, the emission
#: sites and the expectation all point at one list — and so adding a code to
#: DETAIL_CODES without saying what it carries is a failure rather than an
#: omission.
DETAIL_CODE_PARAMS: dict[str, frozenset[str]] = {
    "run.all_sources_queried": frozenset(),
    "run.budget_spent": frozenset({"budget"}),
    "run.no_covering_source": frozenset({"subject_type"}),
    "run.cancelled": frozenset(),
    "run.interrupted": frozenset(),
    "source.timed_out": frozenset({"seconds"}),
    "source.http_status": frozenset({"status"}),
    "source.free_http_refused": frozenset(),
    "source.free_http_capped": frozenset(),
    "source.truncated": frozenset({"max_chars"}),
    "source.thin_response": frozenset({"chars"}),
    "source.querier_error": frozenset(),
    # Knowledge-base and MCP outcomes.
    "source.kb_provider_unreachable": frozenset(),
    "source.kb_search_failed": frozenset(),
    "source.kb_capped": frozenset({"top_k"}),
    "source.kb_unavailable": frozenset(),
    "source.kb_below_floor": frozenset({"floor", "examined"}),
    "source.mcp_bad_server_id": frozenset({"server_id"}),
    "source.mcp_server_missing": frozenset({"server_id"}),
    "source.mcp_server_disabled": frozenset({"server"}),
    "source.mcp_tool_missing": frozenset({"server", "tool"}),
    "source.mcp_argument_ambiguous": frozenset({"tool"}),
    "source.mcp_tool_error": frozenset(),
    "source.fingerprint_matched": frozenset({"matched", "tested"}),
    "source.fingerprint_capped": frozenset({"max_bytes"}),
    "source.vt_unavailable": frozenset(),
    "source.passive_dns_filtered": frozenset({"kept", "fetched"}),
    "source.passive_dns_filtered_more": frozenset({"kept", "fetched"}),
    "source.passive_dns_more": frozenset({"limit"}),
    "source.passive_dns_seeds": frozenset({"resolved", "seeds", "ips", "shared", "pivoted"}),
    "source.opencti_unavailable": frozenset(),
    "source.opencti_known": frozenset({"known", "checked"}),
    "source.opencti_none_known": frozenset({"checked"}),
}

#: Counts a code carries only when they are not zero, each said by a clause of
#: its own in the block (`DETAIL_CLAUSES`): one step can set seeds aside AND be
#: stopped by a rate limit, and a code per combination would hide all but one.
DETAIL_CODE_OPTIONAL_PARAMS: dict[str, frozenset[str]] = {
    "source.passive_dns_seeds": frozenset({"set_aside", "not_read", "failed"}),
    # Matches past `PASSIVE_DNS_MAX_EVIDENCE` are counted, not cited.
    "source.passive_dns_filtered": frozenset({"shown"}),
    "source.passive_dns_filtered_more": frozenset({"shown"}),
}

#: Why a step failed.  Mirrors the ``investigation.source_error`` telemetry
#: taxonomy so the block, the report and the metrics all name a
#: failure the same way.
STEP_ERROR_TYPES = (
    "timeout",
    "rate_limited",
    "not_found",
    "auth_error",
    "unreachable",
    "invalid_response",
)

#: What a piece of evidence IS, which decides how the block renders it.
#:
#: The distinction is not cosmetic. A ``url`` has somewhere to go and renders
#: as a link; a ``document`` names a knowledge-base file the reader may not be
#: able to open at all (it may live in a KB shared with the agent and not with
#: them), and a ``tool_result`` names the MCP server and tool that answered.
#: Rendering the last two as links would promise a destination that does not
#: exist — the same class of overstatement the step vocabulary exists to
#: prevent.
EVIDENCE_KINDS = ("url", "document", "tool_result")

#: The evidence types that carry a resolvable address. Anything else renders
#: as text, whatever ``href`` happens to hold.
LINKABLE_EVIDENCE_KINDS = ("url",)


class Investigation(Base):
    """One Deep Investigation run, with its planned and executed steps."""

    __tablename__ = "investigations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Owner. NULL only if the user is later deleted (SET NULL), matching
    # ``Report`` / ``Presentation``: the record survives its author and the API
    # then only serves it to admins.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Chat context the run was started from. NULL for a flow-triggered run,
    # which has no conversation to render the block in.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Assignment run that triggered the investigation (NULL in plain chat), so
    # a scheduled run's record is not orphaned — same rationale as
    # ``Report.assignment_run_id``.
    assignment_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assignment_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # The run whose unqueried sources this one picked up, when an analyst chose
    # to continue past a spent budget.
    #
    # A continuation is a NEW run rather than a resumed one, and the reason is
    # in the state machine: ``skipped`` is terminal, which is what enforces "a
    # failure never becomes a success". The reason a reader feels, though, is
    # citation numbering — a report's source key is numbered per run, so
    # mutating a run whose report someone already read would renumber the
    # sources under them.
    #
    # SET NULL: losing the parent costs the child only the link. Its steps,
    # findings and report stand on their own, and a chain that lost its head is
    # still a valid run.
    continues_investigation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # The CTI subject under investigation. ``subject_type`` is the LLM
    # classification step's output (threat_actor, ip, domain, url, hash, cve,
    # malware, campaign, unknown) — stored so the block can show what the
    # system *thought* the subject was, which is the first thing an analyst
    # questions when the sources look wrong.
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_value: Mapped[str] = mapped_column(String(500), nullable=False)

    # Slugs of the source packs the user confirmed in the picker. A list, not a
    # FK: packs are platform configuration objects that can be edited or
    # disabled after a run, and the record must keep saying which packs it
    # actually used.
    source_pack_slugs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # The version of each built pack (`pack:<uuid>`, the pack builder) the run
    # used, by slug: a built pack changes after a run, and the record keeps
    # saying which definition it ran. None for a run on built-in packs only.
    source_pack_versions: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    # The plan a built pack's run followed (`goal_plan_to_json`), so the block
    # draws it without reading a pack that may have changed or gone since.
    # None when the plan comes from code (`GOAL_PLANS`).
    declared_plan: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="planning", server_default="planning")

    # Why the run ended, surfaced in the block's footer so an early finish
    # never reads as a complete one. ``end_reason_code`` + ``end_reason_params``
    # are the localizable form; ``end_reason`` is the free-text fallback for a
    # reason that has no code (see ``DETAIL_CODES``).
    end_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    end_reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    end_reason_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Leads found but never planned, because the plan cap was reached. A count
    # rather than steps: the cap exists to bound the plan. Persisted rather
    # than concatenated into ``end_reason`` — which is where it started —
    # because a number the block wants to render should not have to be parsed
    # back out of a sentence.
    leads_dropped: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )

    # Iteration budget. ``max_iterations`` is resolved per run from the source
    # pack / run config (default: 10).
    max_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default=sa.text("10"))

    # --- The synthesised report ---
    #
    # NULL means no report, and that is a real outcome rather than a gap: the
    # run retained nothing a report could stand on, no model was available, or
    # the draft kept citing excerpts that were not supplied. In each case the
    # derived summary and the step list remain — both true — and nothing is
    # published whose sources cannot be checked.
    report: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The numbered passages the report ACTUALLY cites, as
    #: ``[{n, source_name, label, quote, href?}]``. Only the cited ones:
    #: listing every passage offered would present material the report never
    #: used as though it backed it.
    #:
    #: `label` and `href` are UNTREATED third-party text — a fetched page's
    #: `<title>`, an MCP server's citation — stored as they arrived. Any
    #: renderer must put them through `investigation_report._safe_label` and
    #: `_safe_href` first; both are hostile in the markdown sense (a title
    #: closes its link, an address closes its own `(...)` slot) and neither is
    #: made safe by storing it. `investigation_report` is the single escaping
    #: boundary, deliberately: treating at rest would mangle the reference the
    #: reader needs while still leaving the next renderer to guess.
    report_sources: Mapped[list | None] = mapped_column(JSON, nullable=True)
    #: Sentences that assert something, and how many of them carry no marker.
    #:
    #: Stored rather than enforced. Prose needs connective sentences, and a
    #: rule demanding a citation on every one pushes a model to attach sources
    #: to sentences they do not support — buying the look of rigour with real
    #: misattribution. Counting keeps the fact visible instead.
    report_claims: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_uncited_claims: Mapped[int | None] = mapped_column(Integer, nullable=True)
    iterations_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))

    # Bumped on every state write. The frontend uses it exactly like
    # ``Report.revision``: to tell "same run, new step states" from "a
    # different run", and to drop a frame that arrives after a fresher read.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=sa.text("1"))

    # Wall-clock timing. ``completed_at - started_at`` is the ONLY source of a
    # displayed duration: never a value derived from the step count, which is
    # how the prior art came to show "25s" for a four-minute run.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ``server_default`` sits alongside the Python default so the migrated
    # schema and the ``create_all`` one are identical (NOT NULL DEFAULT
    # now()). Without it the two paths disagreed on nullability, and the list
    # endpoints order by ``created_at``.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=sa.text("now()"),
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=sa.text("now()"),
        nullable=False,
    )


class InvestigationStep(Base):
    """One source query inside a run — the unit the live block renders."""

    __tablename__ = "investigation_steps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # CASCADE, unlike the run's own nullable FKs: a step has no meaning without
    # its run, and an orphaned step could not be rendered or attributed.
    # No single-column index: ``ix_investigation_steps_run_position`` leads
    # with this column and serves every lookup by run.
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Display order within the run. Assigned server-side so step identity and
    # ordering never depend on frame arrival order (resilience
    # requirement: out-of-order or duplicate frames must not corrupt the block).
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    # Which pack this step came from, and the human-facing source name shown in
    # the block. Both denormalized so rendering never needs the pack registry —
    # a pack disabled after the run must not blank out its own history.
    source_pack_slug: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # The query actually issued, once it is known. NULL while the step is still
    # part of the pre-rendered skeleton: the analyst must be able to see that a
    # planned step was never actually queried.
    query: Mapped[str | None] = mapped_column(Text, nullable=True)

    # How to address this step's source again, as a self-describing dict —
    # ``{"kind": "mcp_tool", "server_id": …, "tool": …}``.
    #
    # ``source_name`` is for the reader and is NOT an address: an MCP source is
    # named ``"{server_name} · {tool}"``, so matching it back would break on a
    # rename and be ambiguous between two servers offering the same tool. This
    # is what lets a step skipped for a spent budget be queried later instead
    # of only re-planned from scratch.
    #
    # Ids only, never credentials. A continuation re-resolves every id against
    # the caller's visibility at the moment they ask, so this is a pointer and
    # never a grant: a corpus they have since lost access to is refused and
    # disclosed rather than queried off a stale reference.
    #
    # Nullable because every step written before this column existed has none,
    # and a step whose source cannot be addressed is simply not offered as
    # continuable — the honest answer, not a reconstruction guess.
    source_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")

    # Set only on ``error`` / ``degraded``, from ``STEP_ERROR_TYPES``.
    error_type: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Operator-facing detail for a non-success terminal state: the skip reason
    # for ``skipped``, or the failure detail for ``error`` / ``degraded``.
    # Never a stack trace — this string is rendered in the block.
    #
    # ``detail_code`` + ``detail_params`` are the localizable form and win when
    # present; ``status_detail`` remains the fallback for the untranslatable
    # case (a third-party exception message).
    status_detail: Mapped[str | None] = mapped_column(String(300), nullable=True)
    detail_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    detail_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    findings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))

    # NULLABLE ON PURPOSE, and the reason this column has a comment at all: a
    # confidence that cannot be derived from findings is absent, so the block
    # renders no confidence rather than a default. There is no server_default.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # What the step CITES, as a list of ``{kind, label, href}`` objects.
    #
    # Widened from a bare list of URLs because a URL cannot name every
    # kind of source: a knowledge-base hit is a DOCUMENT, and an MCP tool
    # result is a tool call — neither has an address a reader could open, and
    # forcing them into a URL field would have produced citations that look
    # like links and are not.
    #
    # ``kind`` is one of ``EVIDENCE_KINDS``; ``label`` is what the block shows
    # and is always present; ``href`` is set only when there is genuinely
    # somewhere to go. The block renders a label with no href as plain text
    # rather than a dead link, which is the same honesty rule the step statuses
    # follow.
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: The prose this source produced, when it produced any.
    #:
    #: Only a source that WRITES has one — a provider web search answers in
    #: sentences and attaches its citations to spans of them. A fetched page or
    #: a corpus passage does not: those cite a document, and their evidence
    #: carries the quote instead.
    #:
    #: Stored because the spans in ``evidence`` are offsets into it. Kept apart
    #: from ``evidence`` rather than duplicated into every citation, since a
    #: dozen citations into one answer would otherwise store that answer a
    #: dozen times.
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Loop iteration that produced the step. NULL for a skeleton step planned
    # before the loop started, which is what distinguishes "planned up front"
    # from "discovered as a lead" in the block.
    iteration: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Multi-hop provenance: the step whose findings produced this lead. SET
    # NULL so pruning a step never cascades away the steps it led to.
    lead_from_step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigation_steps.id", ondelete="SET NULL"),
        nullable=True,
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # See ``Investigation.created_at`` for why ``server_default`` is here.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=sa.text("now()"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=sa.text("now()"),
        nullable=False,
    )

    __table_args__ = (
        # The block always reads a whole run in display order.
        sa.Index("ix_investigation_steps_run_position", "investigation_id", "position"),
    )


# ---------------------------------------------------------------------------
# Column-derived limits
#
# Read off the columns rather than repeated as literals next to each
# truncation. A `str(exc)[:300]` beside a `String(300)` column is correct only
# until somebody prefixes that string — and the failure is a `DataError` at
# flush time, on the error path, which is the worst place to discover it.
# Deriving them here means the clamp and the constraint cannot drift apart.
# ---------------------------------------------------------------------------

#: A run that has stopped for good. `finish_run` accepts exactly these.
TERMINAL_INVESTIGATION_STATUSES = ("completed", "cancelled", "aborted")

MAX_SUBJECT_LENGTH: int = Investigation.__table__.c.subject_value.type.length
MAX_END_REASON_LENGTH: int = Investigation.__table__.c.end_reason.type.length
MAX_STATUS_DETAIL_LENGTH: int = InvestigationStep.__table__.c.status_detail.type.length
