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

"""One classifier for "why did this agent run fail".

Failure causes were already being computed — at four different sites, for four
local purposes (pick a user-facing message, pick a retry budget, pick a
user-facing error type, decide whether the platform absorbs a failed run's
cost) — and then thrown away. Nothing classifiable reached the database, so
"what is failing on this install, and whose fault is it?" was only answerable
by reading error strings one at a time.

This module is the single place that answers it. Every failure site calls
:func:`classify_run_failure`; nobody re-derives.

Precision rule
--------------
A classification carries **how it was reached**, not only *what* it says:

* :attr:`FailureDetermination.STRUCTURAL` — read off a type, an exception
  class, an HTTP status code, or an engine state. Reproducible, and stable
  under provider wording changes.
* :attr:`FailureDetermination.HEURISTIC` — reached by matching substrings in
  ``str(exc)``. Useful for observability, and **never** trustworthy enough to
  drive a billing decision: at the scale of a real taxonomy a rule built out
  of 80 substrings rots.
* :attr:`FailureDetermination.UNATTRIBUTED` — we do not know. Always paired
  with :attr:`FailureCause.UNKNOWN`.

Phase 2 (billing on the cause) is expected to admit only ``STRUCTURAL``
classifications into its cause → bucket table, which is why the determination
is persisted next to the cause rather than derived from it: the SAME cause can
be reached either way (a 429 read off ``exc.status_code`` is structural; the
same 429 recognised by the word "rate limit" in a stringified error is not).

Nothing here changes what a failure *costs*. Phase 1 measures only.
"""

from __future__ import annotations

import importlib
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "BILLABLE_CANDIDATE_CAUSES",
    "FAILURE_CAUSE_VALUES",
    "TOOL_OVERFLOW_DETAIL",
    "UNRECORDED_FAILURE_CAUSE",
    "FailureCause",
    "FailureClassification",
    "FailureContext",
    "FailureDetermination",
    "PipelineToolFailedError",
    "classify_run_failure",
    "last_provider_failure",
]


class PipelineToolFailedError(RuntimeError):
    """A turn whose pipeline tool refused or failed, raised by a caller that
    must not take the tool's error for the agent's answer. ``str()`` is the
    tool's message; ``error_code`` the stage that failed."""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class FailureCause(StrEnum):
    """Why an agent run ended in failure.

    Deliberately coarse. A value earns its place by being *actionable* — it
    tells a reader whose problem this is (the install's configuration, the
    provider, the request itself, the platform) — not by naming an exception.
    """

    # ── Configuration / precondition: the run could never have run ───────
    # Nothing was called; the failure is entirely inside this install's own
    # configuration and is fixed by an administrator, not by retrying.
    ASSIGNMENT_UNAVAILABLE = "assignment_unavailable"
    AGENT_UNAVAILABLE = "agent_unavailable"
    #: The run died before a model had even been resolved for it. Named after
    #: what the signal actually establishes rather than after a guessed reason:
    #: "no provider is configured" and "prompt assembly blew up" both land here
    #: and are NOT distinguishable from the exception (provider resolution
    #: raises a plain ``ValueError``). What IS certain — and is the whole point
    #: — is that no provider was reached, so none can be at fault.
    FAILED_BEFORE_MODEL_CALL = "failed_before_model_call"
    #: Refused before anything ran: an administrator switched AI off, or the
    #: deployment is not licensed for it (Community Edition; no valid license
    #: in Copilot mode). Same values as the ``agentic_gate`` refusal codes.
    AI_DISABLED = "ai_disabled"
    LICENSE_REQUIRED = "license_required"

    # ── Configuration drift: the run ran on a configuration that broke ───
    #: The agent finished, but an integration reported a resource the run
    #: uses as missing in separate runs (a Notion database deleted or no
    #: longer shared) - the assignment's configuration, not the agent.
    STALE_INTEGRATION_REFERENCE = "stale_integration_reference"

    #: A pipeline tool of the agent (its Prepare or Finalize stage) refused or
    #: failed: no input, no licence, no base URL, the tool raising. The turn's
    #: text is the tool's error, not an answer.
    PIPELINE_TOOL_FAILED = "pipeline_tool_failed"

    # ── Platform accounting ─────────────────────────────────────────────
    PLATFORM_QUOTA_EXCEEDED = "platform_quota_exceeded"
    QUOTA_BACKEND_UNAVAILABLE = "quota_backend_unavailable"

    # ── Request-shaped: reproduces on every provider ─────────────────────
    # The request itself is the problem, so failover cannot help and a retry
    # of the same payload cannot either.
    CONTEXT_OVERFLOW = "context_overflow"
    REQUEST_TOO_LARGE = "request_too_large"
    MODEL_REJECTED_REQUEST = "model_rejected_request"
    #: The model's safety classifiers declined to answer (Anthropic
    #: ``stop_reason: "refusal"``). The detail is the decline category; the
    #: router hands only some categories to another model.
    MODEL_DECLINED = "model_declined"

    # ── Provider weather ────────────────────────────────────────────────
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_OVERLOADED = "provider_overloaded"
    PROVIDER_AUTH_REJECTED = "provider_auth_rejected"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_CONNECTION_FAILED = "provider_connection_failed"
    ALL_PROVIDERS_FAILED = "all_providers_failed"

    # ── Agent loop control ──────────────────────────────────────────────
    RUN_BUDGET_EXHAUSTED = "run_budget_exhausted"
    RUN_CANCELLED = "run_cancelled"

    # ── Orchestration engine gave up on the run ─────────────────────────
    # "Abandoned" is one state (``dead_letter``) but four different stories,
    # and they point at different owners: a row nothing ever claimed is a
    # scheduling / capacity problem, a row claimed three times and failed three
    # times is about the work itself.
    ENGINE_NEVER_CLAIMED = "engine_never_claimed"
    ENGINE_ATTEMPTS_EXHAUSTED = "engine_attempts_exhausted"
    ENGINE_DEADLINE_EXCEEDED = "engine_deadline_exceeded"
    ENGINE_ABANDONED = "engine_abandoned"
    #: Its queue-wait bound passed before any worker started it: a capacity
    #: story, apart from a deadline reached while running. Its execution budget
    #: counts from its start, so this is the only deadline queued work can meet.
    DEADLINE_EXCEEDED_BEFORE_START = "deadline_exceeded_before_start"

    # ── The honest bucket ───────────────────────────────────────────────
    # Everything we cannot yet attribute. This is the value the whole
    # exercise exists to shrink: today it is invisible, and invisible means
    # nobody empties it. It is displayed, counted, and reviewed — never
    # quietly folded into a neighbouring cause because the neighbour is
    # plausible.
    UNKNOWN = "unknown"


class FailureDetermination(StrEnum):
    """How a :class:`FailureCause` was reached. See the module docstring."""

    STRUCTURAL = "structural"
    HEURISTIC = "heuristic"
    UNATTRIBUTED = "unattributed"


#: Causes that a future billing table (phase 2) may legitimately consider,
#: *provided the classification that produced them is structural*. Listed here
#: so the rule is written down in the same module as the taxonomy rather than
#: rediscovered later: everything absent from this set — and every heuristic
#: determination, whatever its cause — stays in observability. The agentic
#: gate's refusals (``AI_DISABLED``, ``LICENSE_REQUIRED``) are absent on
#: purpose: the platform declined to run, before any quota was touched, so
#: there is nothing a billing decision could be about.
BILLABLE_CANDIDATE_CAUSES: frozenset[FailureCause] = frozenset(
    {
        FailureCause.ASSIGNMENT_UNAVAILABLE,
        FailureCause.AGENT_UNAVAILABLE,
        FailureCause.FAILED_BEFORE_MODEL_CALL,
        FailureCause.STALE_INTEGRATION_REFERENCE,
        FailureCause.PLATFORM_QUOTA_EXCEEDED,
        FailureCause.QUOTA_BACKEND_UNAVAILABLE,
        FailureCause.CONTEXT_OVERFLOW,
        FailureCause.REQUEST_TOO_LARGE,
        FailureCause.ALL_PROVIDERS_FAILED,
        FailureCause.RUN_BUDGET_EXHAUSTED,
        FailureCause.RUN_CANCELLED,
        FailureCause.ENGINE_NEVER_CLAIMED,
        FailureCause.ENGINE_ATTEMPTS_EXHAUSTED,
        FailureCause.ENGINE_DEADLINE_EXCEEDED,
        FailureCause.ENGINE_ABANDONED,
        FailureCause.DEADLINE_EXCEEDED_BEFORE_START,
    }
)

#: Every cause as a plain string — the API's filter validation and the
#: frontend's filter list both read this so they cannot drift from the enum.
FAILURE_CAUSE_VALUES: tuple[str, ...] = tuple(cause.value for cause in FailureCause)

#: Filter sentinel that is NOT a cause: it selects FAILED rows carrying no
#: classification at all — written before this feature existed, or by a site
#: not yet wired to the classifier. The read stores pair it with the
#: failed-status predicate, without which it would also match every success. Deliberately distinct from:
#: attr:`FailureCause.UNKNOWN`: "we never looked" and "we looked and could not
#: tell" are different findings, and collapsing them would make the
#: unattributed pool look smaller than it is. Lives here rather than in the API
#: layer so the read stores and the endpoint spell it the same way.
UNRECORDED_FAILURE_CAUSE = "unrecorded"


@dataclass(frozen=True, slots=True)
class FailureContext:
    """Structural signals the failure site already holds.

    Every field is optional: a site passes what it knows and nothing more.
    ``None`` means "not observed here", which is never read as a negative —
    ``llm_resolved=None`` does not mean the LLM failed to resolve.
    """

    #: True once a model has actually been resolved for this run. False means
    #: the run died before there was anything to call, which is the cleanest
    #: "not the agent's fault" signal the platform has.
    llm_resolved: bool | None = None

    #: ``LLMProvider.last_usage`` is populated only once a call has RETURNED
    #: (and is proxied through the failover wrapper), so this reads "did any
    #: model call in this run actually succeed". Recorded for measurement;
    #: phase 3 is where it changes an outcome.
    model_call_returned: bool | None = None

    #: The agent loop's own terminal ``stop_reason``
    #: (``budget_exhausted`` / ``timeout`` / ``cancelled`` / ``max_tokens`` /
    #: ``finalize_tool``), when the site has it.
    stop_reason: str | None = None

    #: Orchestration state the engine gave up in, plus the retry bookkeeping
    #: that says WHICH abandonment it was. ``attempt_count`` is incremented on
    #: every claim, so ``0`` means nothing ever picked the execution up.
    engine_state: str | None = None
    engine_attempts: int | None = None
    engine_max_attempts: int | None = None
    engine_deadline_exceeded: bool | None = None


@dataclass(frozen=True, slots=True)
class FailureClassification:
    """A cause, how it was reached, and the free text behind it."""

    cause: FailureCause
    determination: FailureDetermination
    #: Short free-text detail — the exception type, an HTTP status, or the
    #: engine state. NOT the user-facing message and NOT the full error body
    #: (``AssignmentRun.error`` / ``llm_traces.error_message`` keep those).
    detail: str | None = None
    #: The legs of an exhausted failover chain (``FailoverLeg.as_dict``,
    #: redacted and bounded), so the trace shows why each target failed.
    legs: tuple[dict[str, Any], ...] = ()

    @property
    def is_attributed(self) -> bool:
        return self.cause is not FailureCause.UNKNOWN

    def as_trace_metadata(self) -> dict[str, Any]:
        """Shape written onto the trace root's metadata at failure time."""
        payload: dict[str, Any] = {
            "failure_cause": self.cause.value,
            "failure_determination": self.determination.value,
        }
        if self.detail:
            payload["failure_detail"] = self.detail
        if self.legs:
            payload["failure_legs"] = [dict(leg) for leg in self.legs]
        return payload


_UNATTRIBUTED = FailureClassification(
    cause=FailureCause.UNKNOWN,
    determination=FailureDetermination.UNATTRIBUTED,
)


def _structural(cause: FailureCause, detail: str | None = None) -> FailureClassification:
    return FailureClassification(cause, FailureDetermination.STRUCTURAL, detail)


def _heuristic(cause: FailureCause, detail: str | None = None) -> FailureClassification:
    return FailureClassification(cause, FailureDetermination.HEURISTIC, detail)


# ── Structural HTTP status → cause ───────────────────────────────────────
# Read off the provider SDK exception, never off its message. Anthropic's and
# OpenAI's ``APIStatusError`` both expose ``.status_code``; ``httpx`` exposes
# ``.response.status_code``. Stringifying those and grepping the result is the
# precision leak this table exists to close.
_STATUS_CAUSES: dict[int, FailureCause] = {
    400: FailureCause.MODEL_REJECTED_REQUEST,
    401: FailureCause.PROVIDER_AUTH_REJECTED,
    403: FailureCause.PROVIDER_AUTH_REJECTED,
    404: FailureCause.MODEL_REJECTED_REQUEST,
    408: FailureCause.PROVIDER_TIMEOUT,
    413: FailureCause.REQUEST_TOO_LARGE,
    422: FailureCause.MODEL_REJECTED_REQUEST,
    429: FailureCause.PROVIDER_RATE_LIMITED,
    500: FailureCause.PROVIDER_OVERLOADED,
    502: FailureCause.PROVIDER_OVERLOADED,
    503: FailureCause.PROVIDER_OVERLOADED,
    504: FailureCause.PROVIDER_TIMEOUT,
    529: FailureCause.PROVIDER_OVERLOADED,
}

# ── Heuristic ladder ─────────────────────────────────────────────────────
# Last resort, for providers that surface a plain ``Exception`` with no status
# code at all (several self-hosted gateways, and every error that arrives as a
# bare string through an SDK we do not special-case). Anything matched here is
# tagged HEURISTIC and is therefore barred from billing by construction.
_HEURISTIC_KEYWORDS: tuple[tuple[FailureCause, tuple[str, ...]], ...] = (
    (
        FailureCause.CONTEXT_OVERFLOW,
        (
            "prompt is too long",
            "too many tokens",
            "maximum context length",
            "context_length_exceeded",
            "context window",
        ),
    ),
    (
        FailureCause.MODEL_REJECTED_REQUEST,
        ("invalid_request_error", "unsupported model"),
    ),
    (
        FailureCause.PROVIDER_RATE_LIMITED,
        (
            "rate limit",
            "rate_limit",
            "rate-limit",
            "too many requests",
            # Provider-side quota wording ONLY. A bare "quota" would claim
            # "the provider rate-limited us" for the platform refusing the run
            # itself — ``QuotaExceededError`` is caught structurally above, but
            # any re-raised or re-stringified copy of that refusal (crossing a
            # process boundary, arriving through the dead-letter path) would
            # land here and point the reader at the wrong owner, which is the
            # one thing the taxonomy exists to prevent.
            "insufficient_quota",  # OpenAI
            "quota exceeded for",  # Vertex / Gemini phrasing
            "resource exhausted",
            "resource_exhausted",
            "throttl",  # AWS Bedrock "throttling" / "throttled"
        ),
    ),
    (
        FailureCause.PROVIDER_OVERLOADED,
        (
            "overloaded",
            "service unavailable",
            "temporarily unavailable",
            "internal server error",
            "bad gateway",
        ),
    ),
    (
        FailureCause.PROVIDER_AUTH_REJECTED,
        ("unauthorized", "authentication", "invalid api key", "permission denied"),
    ),
    (
        FailureCause.PROVIDER_TIMEOUT,
        ("timeout", "timed out", "deadline exceeded"),
    ),
    (
        FailureCause.PROVIDER_CONNECTION_FAILED,
        ("connection error", "api connection", "connection reset", "connection refused"),
    ),
)

# HTTP status codes embedded in a message, e.g. "Error code: 529". Kept
# separate from the keyword ladder because a number is a weaker signal than a
# phrase — it is only consulted once every phrase has missed, and it is still
# a heuristic: the digits may belong to a tool payload the provider echoed.
#
# The status-like prefix is required rather than optional. A bare three-digit
# match also fires on "request took 503 ms", "512 tokens" and "port 443" —
# harmless for billing (everything matched here is HEURISTIC, and therefore
# barred from the phase-2 table by construction) but not harmless for the
# distribution, which is the thing phase 2 is meant to be *decided from*.
# Mislabelled rows in the sample are worse than unlabelled ones.
_STATUS_IN_MESSAGE_RE = re.compile(
    r"(?:status(?:\s*code)?|error\s*code|http)\D{0,3}(4\d{2}|5\d{2})(?![\d,.])",
    re.IGNORECASE,
)


def exception_status_code(exc: BaseException) -> int | None:
    """HTTP status carried by a provider SDK exception, read structurally.

    Covers the three shapes the platform actually sees: an SDK error with
    ``.status_code`` (Anthropic, OpenAI, Mistral, and every SDK modelled on
    them), an ``httpx.HTTPStatusError`` with ``.response.status_code``, and a
    botocore-style ``.response["ResponseMetadata"]["HTTPStatusCode"]``.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status

    response = getattr(exc, "response", None)
    if response is not None:
        nested = getattr(response, "status_code", None)
        if isinstance(nested, int):
            return nested
        if isinstance(response, dict):
            meta = response.get("ResponseMetadata")
            if isinstance(meta, dict):
                code = meta.get("HTTPStatusCode")
                if isinstance(code, int):
                    return code
    return None


def last_provider_failure(exc: BaseException) -> tuple[BaseException, int | None]:
    """The leg that ended a failover chain, and the HTTP status it carried.

    An exhausted chain (``AllProvidersFailedError``) names its providers, not
    why the last one failed; a caller that answers a client needs that leg.
    Any other exception is its own last leg.
    """
    failures = getattr(exc, "failures", None)
    last: BaseException = exc
    if isinstance(failures, list) and failures:
        candidate = failures[-1][1] if isinstance(failures[-1], tuple) else None
        if isinstance(candidate, BaseException):
            last = candidate
    return last, exception_status_code(last)


#: Bound of the detail that lists the legs of an exhausted chain.
_LEGS_DETAIL_LIMIT = 300


def _legs_detail(legs: tuple[Any, ...]) -> str | None:
    """``label:outcome`` per leg of an exhausted chain, bounded."""
    if not legs:
        return None
    detail = "; ".join(f"{leg.label}:{leg.outcome()}" for leg in legs)
    return detail if len(detail) <= _LEGS_DETAIL_LIMIT else detail[: _LEGS_DETAIL_LIMIT - 1] + "…"


def _exception_label(exc: BaseException, status: int | None = None) -> str:
    """Short, bounded detail string — a type name plus an optional status."""
    name = type(exc).__name__
    return f"{name} ({status})" if status is not None else name


def _classify_context(ctx: FailureContext) -> FailureClassification | None:
    """Structural signals that decide the cause before the exception is read.

    Engine verdicts come first: when the orchestration engine abandoned an
    execution, *that* is the failure, whatever exception the last attempt left
    behind.
    """
    engine = _classify_engine(ctx)
    if engine is not None:
        return engine

    stop_reason = (ctx.stop_reason or "").strip().lower()
    if stop_reason == "budget_exhausted":
        return _structural(FailureCause.RUN_BUDGET_EXHAUSTED, "stop_reason=budget_exhausted")
    if stop_reason == "cancelled":
        return _structural(FailureCause.RUN_CANCELLED, "stop_reason=cancelled")
    if stop_reason == "timeout":
        return _structural(FailureCause.PROVIDER_TIMEOUT, "stop_reason=timeout")

    return None


def _classify_before_model_call(
    exc: BaseException, ctx: FailureContext
) -> FailureClassification | None:
    """"Nothing was ever called" — decisive, but only over the heuristics.

    Ranked BELOW the structural exception steps (an exception carrying a real
    status code cannot have been raised before a provider was reached, so those
    never conflict) and ABOVE the message ladder, which is where it matters: a
    pre-resolution failure whose text happens to contain the word "timeout" is
    not a provider timeout, and the ordering is what stops it being recorded as
    one.

    Only ``False`` counts. ``None`` means the site did not observe this, which
    is not a negative.
    """
    if ctx.llm_resolved is not False:
        return None
    return _structural(
        FailureCause.FAILED_BEFORE_MODEL_CALL, _exception_label(exc)
    )


def _classify_engine(ctx: FailureContext) -> FailureClassification | None:
    """Distinguish the ways the orchestration engine gives up on an execution.

    ``dead_letter`` alone only says "abandoned". The retry bookkeeping says
    which abandonment it was, and they have different owners:

    * ``attempt_count == 0`` — nothing ever claimed the row. It was scheduled
      and the workers never got to it: a capacity or scheduling problem, and
      the least defensible thing to charge anyone for.
    * ``attempt_count >= max_attempts`` — claimed repeatedly, failed every
      time. About the work, not the queue.
    * past ``deadline_at`` — claimed (or waiting on a callback) and still
      unfinished when the hard timeout arrived.

    Checked in that order: the most specific story wins. A row that was never
    claimed AND is past its deadline is a queue problem that happens to have
    also run out of time, not a timeout.
    """
    state = (ctx.engine_state or "").strip().lower()
    if state not in {"dead_letter", "abandoned"}:
        return None

    attempts = ctx.engine_attempts
    max_attempts = ctx.engine_max_attempts
    detail = f"engine_state={state} attempts={attempts}"

    if attempts is None:
        # Not fatal — ``ENGINE_ABANDONED`` below is still true, just vaguer.
        # Logged because the vagueness would otherwise be indistinguishable
        # from a genuinely unclassifiable abandonment: a caller that simply
        # forgot to thread the retry bookkeeping silently degrades every one
        # of its rows to the catch-all, and nothing says so.
        logger.debug(
            "Engine failure classified without retry bookkeeping "
            "(engine_state=%s): falling back to the catch-all cause",
            state,
        )
    elif attempts == 0:
        return _structural(FailureCause.ENGINE_NEVER_CLAIMED, detail)
    elif max_attempts is not None and attempts >= max_attempts:
        return _structural(FailureCause.ENGINE_ATTEMPTS_EXHAUSTED, detail)
    if ctx.engine_deadline_exceeded:
        return _structural(FailureCause.ENGINE_DEADLINE_EXCEEDED, detail)
    return _structural(FailureCause.ENGINE_ABANDONED, detail)


def _is_instance_of(module: str, name: str, exc: BaseException) -> bool:
    """``isinstance`` against a lazily-imported class, never raising.

    The import is deferred AND guarded because this runs inside failure
    handlers: an import chain that blows up here (a broken optional provider
    SDK is exactly how ``app.llm.router`` fails to import in practice) must
    degrade the classification, never mask the exception being recorded.
    """
    try:
        klass = getattr(importlib.import_module(module), name)
    except Exception:  # pragma: no cover - defensive
        return False
    return isinstance(klass, type) and isinstance(exc, klass)


def _classify_decline(exc: BaseException) -> FailureClassification | None:
    """A model's decline (``ModelDeclinedError``), its category as the detail."""
    if not _is_instance_of("app.llm.base", "ModelDeclinedError", exc):
        return None
    category = getattr(exc, "category", None)
    detail = category[:64] if isinstance(category, str) and category else None
    return _structural(FailureCause.MODEL_DECLINED, detail)


def _classify_own_exception(exc: BaseException) -> FailureClassification | None:
    """Our own exception classes — the most structural signal available."""
    if isinstance(exc, PipelineToolFailedError):
        return _structural(FailureCause.PIPELINE_TOOL_FAILED, exc.error_code)

    if _is_instance_of(
        "app.integrations.stale_references", "StaleIntegrationReferenceError", exc
    ):
        references = getattr(exc, "references", ()) or ()
        detail = ", ".join(
            sorted({f"{ref.provider}:{ref.resource_type}" for ref in references})
        )
        return _structural(
            FailureCause.STALE_INTEGRATION_REFERENCE, detail[:120] or None
        )

    if _is_instance_of("app.llm.base", "ContextOverflowError", exc):
        detail = (
            TOOL_OVERFLOW_DETAIL
            if getattr(exc, "is_tool_dominated", False)
            else "context_overflow"
        )
        return _structural(FailureCause.CONTEXT_OVERFLOW, detail)

    if _is_instance_of("app.services.quota", "QuotaExceededError", exc):
        return _structural(FailureCause.PLATFORM_QUOTA_EXCEEDED, "QuotaExceededError")

    if declined := _classify_decline(exc):
        return declined

    try:
        from app.llm.router import AllProvidersFailedError, is_request_too_large_error
    except Exception:  # pragma: no cover - defensive
        return None

    if isinstance(exc, AllProvidersFailedError):
        # A chain that ENDED on a size error is a request-shaped failure that
        # merely happened to be discovered late (the chain short-circuits on
        # the first size error, so the last entry is the one that stopped it).
        # Same rule ``is_request_too_large_error`` already applies for the
        # user-facing message, so the two can never disagree.
        legs = tuple(getattr(exc, "legs", ()) or ())
        leg_dicts = tuple(leg.as_dict() for leg in legs)
        if is_request_too_large_error(exc):
            return FailureClassification(
                FailureCause.REQUEST_TOO_LARGE,
                FailureDetermination.STRUCTURAL,
                "AllProvidersFailedError",
                leg_dicts,
            )
        failures = getattr(exc, "failures", None) or []
        last = failures[-1][1] if failures else None
        # A chain stopped by a decline it may not hand on, or whose last model
        # declined too, failed on the decline: its detail is the categories.
        if last is not None and _classify_decline(last) is not None:
            categories = sorted({leg.category for leg in legs if leg.kind == "declined" and leg.category})
            last_category = getattr(last, "category", None)
            if not categories and isinstance(last_category, str) and last_category:
                categories = [last_category]
            return FailureClassification(
                FailureCause.MODEL_DECLINED,
                FailureDetermination.STRUCTURAL,
                ",".join(categories)[:_LEGS_DETAIL_LIMIT] or None,
                leg_dicts,
            )
        # Every model refused the input (an image too large): heuristic, the
        # input is told from a refused parameter by the refusal's wording.
        if getattr(exc, "input_rejected", None):
            return FailureClassification(
                FailureCause.MODEL_REJECTED_REQUEST,
                FailureDetermination.HEURISTIC,
                _legs_detail(legs),
                leg_dicts,
            )
        return FailureClassification(
            FailureCause.ALL_PROVIDERS_FAILED,
            FailureDetermination.STRUCTURAL,
            _legs_detail(legs) or f"providers={len(failures)}",
            leg_dicts,
        )

    return None


def _classify_request_too_large(exc: BaseException) -> FailureClassification | None:
    """A raw 413 the failover wrapper re-raised as non-failoverable.

    A 413 with a real ``status_code`` never reaches here: it is already
    structural by then. What is left is ``is_request_too_large_error``'s
    keyword match, a HEURISTIC determination, consulted right after the
    structural steps because the same predicate drives failover.
    """
    try:
        from app.llm.router import is_request_too_large_error
    except Exception:  # pragma: no cover - defensive
        return None
    if is_request_too_large_error(exc):
        return _heuristic(FailureCause.REQUEST_TOO_LARGE, _exception_label(exc))
    return None


def _classify_input_rejection(exc: BaseException) -> FailureClassification | None:
    """One model refusing the input, direct (400) or behind the gateway (its 502).

    Ahead of the status step, which reads the gateway's 502 as an overload.
    """
    try:
        from app.llm.router import input_rejection
    except Exception:  # pragma: no cover - defensive
        return None
    if input_rejection(exc) is None:
        return None
    return _heuristic(FailureCause.MODEL_REJECTED_REQUEST, _exception_label(exc, exception_status_code(exc)))


def _classify_by_status(exc: BaseException) -> FailureClassification | None:
    status = exception_status_code(exc)
    if status is None:
        return None
    cause = _STATUS_CAUSES.get(status)
    if cause is None:
        cause = (
            FailureCause.PROVIDER_OVERLOADED
            if 500 <= status < 600
            else FailureCause.MODEL_REJECTED_REQUEST
            if 400 <= status < 500
            else None
        )
    if cause is None:
        return None
    return _structural(cause, _exception_label(exc, status))


def _classify_by_type(exc: BaseException) -> FailureClassification | None:
    """Standard-library / transport exception types, matched on the class."""
    if isinstance(exc, TimeoutError):
        return _structural(FailureCause.PROVIDER_TIMEOUT, _exception_label(exc))
    if isinstance(exc, ConnectionError):
        return _structural(FailureCause.PROVIDER_CONNECTION_FAILED, _exception_label(exc))
    try:
        import httpx
    except Exception:  # pragma: no cover - httpx is a hard dependency
        return None
    if isinstance(exc, httpx.TimeoutException):
        return _structural(FailureCause.PROVIDER_TIMEOUT, _exception_label(exc))
    if isinstance(exc, httpx.TransportError):
        return _structural(FailureCause.PROVIDER_CONNECTION_FAILED, _exception_label(exc))
    return None


#: Detail written for an overflow the agent's TOOL SCHEMAS caused, rather than
#: its conversation. Same token, different fix (unbind tools / change model vs.
#: shorten the conversation), so the message layer branches on it.
TOOL_OVERFLOW_DETAIL = "tool_overflow"


def _classify_by_message(exc: BaseException) -> FailureClassification | None:
    message = str(exc).lower()
    if not message:
        return None
    # Tool-dominated overflow arriving as a provider validation error rather
    # than as our own ``ContextOverflowError`` — the model refuses the *tools*
    # array, not the conversation. Needs both terms, so it cannot ride the
    # single-keyword ladder below.
    if "array too long" in message and "tools" in message:
        return _heuristic(FailureCause.CONTEXT_OVERFLOW, TOOL_OVERFLOW_DETAIL)
    for cause, keywords in _HEURISTIC_KEYWORDS:
        if any(keyword in message for keyword in keywords):
            return _heuristic(cause, _exception_label(exc))
    match = _STATUS_IN_MESSAGE_RE.search(message)
    if match:
        status = int(match.group(1))
        cause = _STATUS_CAUSES.get(status)
        if cause is None and 500 <= status < 600:
            cause = FailureCause.PROVIDER_OVERLOADED
        if cause is not None:
            return _heuristic(cause, f"{_exception_label(exc)} status~{status}")
    return None


def classify_run_failure(
    exc: BaseException | None,
    ctx: FailureContext | None = None,
) -> FailureClassification:
    """Classify why a run failed.

    Never raises: a failure handler that cannot classify must still record the
    failure, so every internal step is defensive and the fallback is an
    explicit :attr:`FailureCause.UNKNOWN` rather than a plausible guess.

    Order is precision-first — context signals the site *observed*, then our
    own exception classes, then a status code read off the SDK exception, then
    exception types, and only then the message. The first four are structural;
    the last is not, and is tagged accordingly. One exception: a refused input
    is read off the refusal's wording ahead of the status, a HEURISTIC.
    """
    context = ctx or FailureContext()

    try:
        from_context = _classify_context(context)
    except Exception:  # pragma: no cover - defensive
        from_context = None
    if from_context is not None:
        return from_context

    if exc is None:
        return _UNATTRIBUTED

    for step in (
        _classify_own_exception,
        _classify_input_rejection,
        _classify_by_status,
        _classify_by_type,
        # Structural, and deliberately ranked above every heuristic below it —
        # see the function's docstring.
        lambda e: _classify_before_model_call(e, context),
        _classify_request_too_large,
        _classify_by_message,
    ):
        try:
            result = step(exc)
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "Failure classification step %s raised; skipping it",
                getattr(step, "__name__", repr(step)),
                exc_info=True,
            )
            continue
        if result is not None:
            return result

    return FailureClassification(
        cause=FailureCause.UNKNOWN,
        determination=FailureDetermination.UNATTRIBUTED,
        detail=_exception_label(exc),
    )
