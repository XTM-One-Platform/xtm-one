"""Failure-cause classification and persistence.

The point of the feature is precision, so the tests are mostly about precision:
that a cause reached from a status code is marked STRUCTURAL, that the same
cause reached from a substring is marked HEURISTIC, and that the ordering
between the two never lets a keyword overrule a structural fact. Phase 2 will
bill on the first kind and not the second, so the distinction has to hold under
test, not just under review.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.failure_causes import (
    BILLABLE_CANDIDATE_CAUSES,
    FAILURE_CAUSE_VALUES,
    TOOL_OVERFLOW_DETAIL,
    FailureCause,
    FailureContext,
    FailureDetermination,
    classify_run_failure,
)


class _StatusError(Exception):
    """Stands in for an Anthropic / OpenAI ``APIStatusError``."""

    def __init__(self, status_code: int, message: str = "provider said no"):
        super().__init__(message)
        self.status_code = status_code


# ── The taxonomy itself ──────────────────────────────────────────────────────


def test_every_cause_value_is_unique_and_snake_case():
    assert len(set(FAILURE_CAUSE_VALUES)) == len(FAILURE_CAUSE_VALUES)
    for value in FAILURE_CAUSE_VALUES:
        assert value == value.lower()
        assert " " not in value


def test_cause_values_fit_the_persisted_column():
    # ``assignment_runs.failure_cause`` / ``llm_traces.failure_cause`` are
    # VARCHAR(48). A longer member would be silently rejected at write time in
    # a failure handler, i.e. exactly where nobody is watching.
    for value in FAILURE_CAUSE_VALUES:
        assert len(value) <= 48


def test_unknown_is_never_a_billing_candidate():
    """The unattributed bucket must stay visible, never quietly charged."""
    assert FailureCause.UNKNOWN not in BILLABLE_CANDIDATE_CAUSES


def test_heuristic_only_causes_are_not_billing_candidates():
    """Causes we can only ever reach by keyword stay out of the table.

    A provider that returns a bare ``Exception`` is indistinguishable from a
    tool payload that happens to contain the word "overloaded". Those two are
    fine to *show*; they are not fine to charge on.
    """
    for cause in (
        FailureCause.PROVIDER_RATE_LIMITED,
        FailureCause.PROVIDER_OVERLOADED,
        FailureCause.PROVIDER_AUTH_REJECTED,
        FailureCause.PROVIDER_TIMEOUT,
        FailureCause.PROVIDER_CONNECTION_FAILED,
        FailureCause.MODEL_REJECTED_REQUEST,
    ):
        assert cause not in BILLABLE_CANDIDATE_CAUSES


# ── Structural: our own exception classes ────────────────────────────────────


def test_context_overflow_is_structural():
    from app.llm.base import ContextOverflowError

    exc = ContextOverflowError(
        estimated_tokens=500_000,
        context_window=200_000,
        model="claude-x",
    )
    result = classify_run_failure(exc)
    assert result.cause is FailureCause.CONTEXT_OVERFLOW
    assert result.determination is FailureDetermination.STRUCTURAL


def test_tool_dominated_overflow_keeps_its_detail():
    """The message layer branches on the detail, so it has to survive."""
    from app.llm.base import ContextOverflowError

    exc = ContextOverflowError(
        estimated_tokens=500_000,
        context_window=200_000,
        model="claude-x",
        tool_tokens=400_000,
    )
    result = classify_run_failure(exc)
    assert result.cause is FailureCause.CONTEXT_OVERFLOW
    assert result.detail == TOOL_OVERFLOW_DETAIL


def test_quota_exceeded_is_structural():
    from app.services.quota import QuotaExceededError

    exc = QuotaExceededError("agentic", limit=100, current=100, period="month")
    result = classify_run_failure(exc)
    assert result.cause is FailureCause.PLATFORM_QUOTA_EXCEEDED
    assert result.determination is FailureDetermination.STRUCTURAL


def test_exhausted_failover_chain_is_its_own_cause():
    from app.llm.router import AllProvidersFailedError

    exc = AllProvidersFailedError(
        [("anthropic", _StatusError(529)), ("openai", _StatusError(500))]
    )
    result = classify_run_failure(exc)
    assert result.cause is FailureCause.ALL_PROVIDERS_FAILED
    assert result.determination is FailureDetermination.STRUCTURAL


def test_failover_chain_ending_on_a_size_error_is_request_shaped():
    """The chain short-circuits on the first size error, so
    its LAST entry is the one that stopped it — and a size error reproduces on
    every provider, which makes it the request's problem, not the fleet's."""
    from app.llm.router import AllProvidersFailedError

    exc = AllProvidersFailedError(
        [
            ("anthropic", Exception("Error code: 529 - overloaded")),
            ("openai", Exception("Error code: 413 - request_too_large")),
        ]
    )
    assert classify_run_failure(exc).cause is FailureCause.REQUEST_TOO_LARGE


# ── Structural: HTTP status read off the SDK exception ───────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, FailureCause.MODEL_REJECTED_REQUEST),
        (401, FailureCause.PROVIDER_AUTH_REJECTED),
        (403, FailureCause.PROVIDER_AUTH_REJECTED),
        (413, FailureCause.REQUEST_TOO_LARGE),
        (429, FailureCause.PROVIDER_RATE_LIMITED),
        (500, FailureCause.PROVIDER_OVERLOADED),
        (503, FailureCause.PROVIDER_OVERLOADED),
        (504, FailureCause.PROVIDER_TIMEOUT),
        (529, FailureCause.PROVIDER_OVERLOADED),
    ],
)
def test_status_codes_classify_structurally(status, expected):
    result = classify_run_failure(_StatusError(status))
    assert result.cause is expected
    assert result.determination is FailureDetermination.STRUCTURAL
    assert str(status) in (result.detail or "")


def test_unmapped_5xx_falls_back_to_overloaded():
    result = classify_run_failure(_StatusError(507))
    assert result.cause is FailureCause.PROVIDER_OVERLOADED
    assert result.determination is FailureDetermination.STRUCTURAL


def test_httpx_style_nested_status_is_read():
    exc = Exception("gateway said no")
    exc.response = SimpleNamespace(status_code=429)
    assert classify_run_failure(exc).cause is FailureCause.PROVIDER_RATE_LIMITED


def test_botocore_style_nested_status_is_read():
    exc = Exception("bedrock said no")
    exc.response = {"ResponseMetadata": {"HTTPStatusCode": 429}}
    assert classify_run_failure(exc).cause is FailureCause.PROVIDER_RATE_LIMITED


# ── Structural: exception types ──────────────────────────────────────────────


def test_timeout_type_beats_an_empty_message():
    """``str(TimeoutError())`` is empty, so the keyword ladder saw nothing and
    the failure landed in the generic bucket. The type was there all along."""
    result = classify_run_failure(TimeoutError())
    assert result.cause is FailureCause.PROVIDER_TIMEOUT
    assert result.determination is FailureDetermination.STRUCTURAL


def test_connection_error_type_is_structural():
    result = classify_run_failure(ConnectionError())
    assert result.cause is FailureCause.PROVIDER_CONNECTION_FAILED
    assert result.determination is FailureDetermination.STRUCTURAL


# ── Heuristics, and their place in the order ─────────────────────────────────


def test_keyword_match_is_tagged_heuristic():
    result = classify_run_failure(Exception("the model is overloaded, try later"))
    assert result.cause is FailureCause.PROVIDER_OVERLOADED
    assert result.determination is FailureDetermination.HEURISTIC


def test_structural_status_wins_over_a_contradicting_message():
    """A 429 whose body happens to mention a timeout is a rate limit."""
    result = classify_run_failure(_StatusError(429, "upstream timed out"))
    assert result.cause is FailureCause.PROVIDER_RATE_LIMITED
    assert result.determination is FailureDetermination.STRUCTURAL


def test_pre_model_failure_is_not_attributed_to_a_provider():
    """The precision case the ordering exists for.

    Nothing was called, so a provider cannot be at fault — however much the
    exception's text sounds like one.
    """
    result = classify_run_failure(
        Exception("connection timed out talking to the database"),
        FailureContext(llm_resolved=False),
    )
    assert result.cause is FailureCause.FAILED_BEFORE_MODEL_CALL
    assert result.determination is FailureDetermination.STRUCTURAL


def test_llm_resolved_none_is_not_read_as_a_negative():
    """A site that does not observe the signal must not have one inferred."""
    result = classify_run_failure(Exception("overloaded"), FailureContext())
    assert result.cause is FailureCause.PROVIDER_OVERLOADED


def test_a_platform_quota_refusal_is_not_blamed_on_the_provider():
    """A re-raised copy of our OWN refusal must not read as provider weather.

    ``QuotaExceededError`` is caught structurally, but a stringified copy of it
    — crossing a process boundary, arriving through the dead-letter path — used
    to hit a bare ``"quota"`` keyword and claim the provider rate-limited us,
    pointing the reader at the wrong owner entirely.
    """
    result = classify_run_failure(Exception("agentic quota exceeded: 100/100 (month)"))
    assert result.cause is not FailureCause.PROVIDER_RATE_LIMITED


@pytest.mark.parametrize(
    "message",
    [
        "insufficient_quota: you exceeded your current quota",
        "Quota exceeded for quota metric 'Generate requests'",
    ],
)
def test_provider_quota_wording_still_reads_as_rate_limited(message):
    """Tightening the keyword must not lose the provider-side phrasings."""
    assert classify_run_failure(Exception(message)).cause is (
        FailureCause.PROVIDER_RATE_LIMITED
    )


@pytest.mark.parametrize(
    "message",
    ["request took 503 ms", "trimmed to 512 tokens", "connect to port 443 failed"],
)
def test_a_bare_number_is_not_read_as_an_http_status(message):
    """Numbers that merely look like statuses must not pollute the sample.

    Harmless for billing — everything the message ladder produces is HEURISTIC
    and barred from the phase-2 table by construction — but phase 2 is meant to
    be *decided from* this distribution, and a mislabelled row is worse there
    than an unlabelled one.
    """
    result = classify_run_failure(Exception(message))
    assert result.cause is FailureCause.UNKNOWN


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Error code: 529 - overloaded", FailureCause.PROVIDER_OVERLOADED),
        ("status code: 503", FailureCause.PROVIDER_OVERLOADED),
        ("HTTP 429 returned by the gateway", FailureCause.PROVIDER_RATE_LIMITED),
    ],
)
def test_status_like_context_is_still_read_from_the_message(message, expected):
    """The cases the tightened pattern must keep catching."""
    result = classify_run_failure(Exception(message))
    assert result.cause is expected
    assert result.determination is FailureDetermination.HEURISTIC


def test_tools_array_too_long_reads_as_a_tool_overflow():
    result = classify_run_failure(
        Exception("tools: array too long: 200 > 128")
    )
    assert result.cause is FailureCause.CONTEXT_OVERFLOW
    assert result.detail == TOOL_OVERFLOW_DETAIL
    assert result.determination is FailureDetermination.HEURISTIC


# ── The unattributed bucket ──────────────────────────────────────────────────


def test_unrecognised_failure_is_explicitly_unattributed():
    """Never guessed into a neighbouring cause — the bucket is the deliverable."""
    result = classify_run_failure(Exception("kaboom"))
    assert result.cause is FailureCause.UNKNOWN
    assert result.determination is FailureDetermination.UNATTRIBUTED
    assert result.is_attributed is False
    assert result.detail == "Exception"


def test_no_exception_and_no_context_is_unattributed():
    result = classify_run_failure(None)
    assert result.cause is FailureCause.UNKNOWN


def test_classifier_never_raises_on_a_hostile_exception():
    class _HostileError(Exception):
        def __str__(self):  # pragma: no cover - exercised through the classifier
            raise RuntimeError("no string for you")

    result = classify_run_failure(_HostileError())
    assert result.cause is FailureCause.UNKNOWN


# ── Orchestration-engine abandonment ─────────────────────────────────────────


def test_engine_never_claimed():
    result = classify_run_failure(
        None,
        FailureContext(engine_state="dead_letter", engine_attempts=0, engine_max_attempts=3),
    )
    assert result.cause is FailureCause.ENGINE_NEVER_CLAIMED
    assert result.determination is FailureDetermination.STRUCTURAL


def test_engine_attempts_exhausted():
    result = classify_run_failure(
        None,
        FailureContext(engine_state="dead_letter", engine_attempts=3, engine_max_attempts=3),
    )
    assert result.cause is FailureCause.ENGINE_ATTEMPTS_EXHAUSTED


def test_engine_deadline_exceeded():
    result = classify_run_failure(
        None,
        FailureContext(
            engine_state="dead_letter",
            engine_attempts=1,
            engine_max_attempts=3,
            engine_deadline_exceeded=True,
        ),
    )
    assert result.cause is FailureCause.ENGINE_DEADLINE_EXCEEDED


def test_never_claimed_beats_deadline_exceeded():
    """A row nothing ever picked up is a queue problem that also happened to run
    out of time — not a timeout. The more specific story wins."""
    result = classify_run_failure(
        None,
        FailureContext(
            engine_state="dead_letter",
            engine_attempts=0,
            engine_max_attempts=3,
            engine_deadline_exceeded=True,
        ),
    )
    assert result.cause is FailureCause.ENGINE_NEVER_CLAIMED


def test_engine_verdict_beats_the_last_attempt_s_exception():
    """When the engine abandoned the execution, THAT is the failure — whatever
    the last attempt happened to leave behind."""
    result = classify_run_failure(
        _StatusError(429),
        FailureContext(engine_state="dead_letter", engine_attempts=0),
    )
    assert result.cause is FailureCause.ENGINE_NEVER_CLAIMED


def test_engine_abandonment_without_bookkeeping_stays_coarse():
    """Missing ``engine_attempts`` degrades the cause but never invents one."""
    result = classify_run_failure(
        None, FailureContext(engine_state="dead_letter")
    )
    assert result.cause is FailureCause.ENGINE_ABANDONED
    assert result.determination is FailureDetermination.STRUCTURAL


def test_a_live_engine_state_classifies_nothing():
    result = classify_run_failure(
        _StatusError(429), FailureContext(engine_state="running", engine_attempts=1)
    )
    assert result.cause is FailureCause.PROVIDER_RATE_LIMITED


# ── Agent-loop terminal stop reasons ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("budget_exhausted", FailureCause.RUN_BUDGET_EXHAUSTED),
        ("cancelled", FailureCause.RUN_CANCELLED),
        ("timeout", FailureCause.PROVIDER_TIMEOUT),
    ],
)
def test_loop_stop_reasons_are_structural(stop_reason, expected):
    result = classify_run_failure(None, FailureContext(stop_reason=stop_reason))
    assert result.cause is expected
    assert result.determination is FailureDetermination.STRUCTURAL


# ── Trace metadata shape ─────────────────────────────────────────────────────


def test_trace_metadata_carries_cause_and_determination():
    payload = classify_run_failure(_StatusError(429)).as_trace_metadata()
    assert payload["failure_cause"] == "provider_rate_limited"
    assert payload["failure_determination"] == "structural"
    assert "failure_detail" in payload


# ── Persistence at the dead-letter site ──────────────────────────────────────


class _FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeDb:
    def __init__(self, scalar=None):
        self._scalar = scalar

    async def execute(self, *_a, **_k):
        return _FakeResult(self._scalar)

    async def commit(self):
        pass


@pytest.mark.asyncio
async def test_dead_letter_records_which_abandonment_it_was():
    """The engine already knew; until now the distinction died with the sweep."""
    from app.services.orchestration_adapters import AdapterContext
    from app.services.orchestration_adapters.assignment import AssignmentAdapter

    run = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        status="running",
        error=None,
        completed_at=None,
        failure_cause=None,
        failure_determination=None,
        # The adapter also credits the agentic action back here. An
        # empty context means "never metered", so that path is a no-op and
        # this test stays about the classification.
        trigger_context={},
    )
    execution = SimpleNamespace(
        payload={"run_id": str(uuid.uuid4())},
        last_error="Lease expired, last heartbeat 74 s ago, with no attempt left.",
        state="dead_letter",
        attempt_count=3,
        max_attempts=3,
        deadline_at=None,
    )

    await AssignmentAdapter().on_dead_letter(
        AdapterContext(db=_FakeDb(scalar=run), worker_id="w1"), execution
    )

    assert run.status == "failed"
    assert run.failure_cause == FailureCause.ENGINE_ATTEMPTS_EXHAUSTED.value
    assert run.failure_determination == FailureDetermination.STRUCTURAL.value


@pytest.mark.asyncio
async def test_dead_letter_leaves_terminal_runs_untouched():
    """The pre-existing terminal-state guard must still short-circuit BEFORE
    any classification is written."""
    from app.services.orchestration_adapters import AdapterContext
    from app.services.orchestration_adapters.assignment import AssignmentAdapter

    run = SimpleNamespace(
        status="completed",
        error=None,
        completed_at=None,
        failure_cause=None,
        trigger_context={},
    )
    execution = SimpleNamespace(
        payload={"run_id": str(uuid.uuid4())},
        last_error="engine gave up",
        state="dead_letter",
        attempt_count=3,
        max_attempts=3,
        deadline_at=None,
    )

    await AssignmentAdapter().on_dead_letter(
        AdapterContext(db=_FakeDb(scalar=run), worker_id="w1"), execution
    )

    assert run.status == "completed"
    assert run.failure_cause is None


# ── Persistence on the durable trace ─────────────────────────────────────────


class _FakeSpan:
    def __init__(self):
        self.updates: list[dict] = []
        self.trace_io: list[dict] = []

    def update(self, **kwargs) -> None:
        self.updates.append(kwargs)

    def set_trace_io(self, **kwargs) -> None:
        self.trace_io.append(kwargs)

    def create_event(self, **kwargs) -> None:
        pass

    def end(self) -> None:
        pass


def _end_trace_capturing(**kwargs):
    """Run ``end_trace`` with the dispatcher stubbed, returning the TraceData."""
    from unittest.mock import patch

    from app.services.langfuse_tracing import end_trace

    # ``trace_llm_call`` writes the same dict to both stashes at trace open, so
    # the fixture does too — the quota keys stand in for the creation-time
    # metadata the Trace Explorer's other panels read.
    opened_metadata = {"quota_actions_consumed": 1, "quota_reason": "charged"}
    ctx = {
        "trace": _FakeSpan(),
        "generation": None,
        "parent_ctx": None,
        "metadata": dict(opened_metadata),
        "local_trace_id": uuid.uuid4(),
        "local_source": "assignment",
        "local_name": "assignment-run:agent",
        "local_metadata": dict(opened_metadata),
    }
    with patch(
        "app.services.tracing.dispatcher.submit_trace_for_flush"
    ) as submit:
        end_trace(ctx, **kwargs)
    assert submit.call_args is not None, "expected a durable trace flush"
    return submit.call_args.args[0], ctx


def test_end_trace_persists_the_failure_cause_on_the_trace():
    trace_data, _ctx = _end_trace_capturing(
        error="Error code: 429", failure=classify_run_failure(_StatusError(429))
    )
    assert trace_data.status == "error"
    assert trace_data.failure_cause == FailureCause.PROVIDER_RATE_LIMITED.value


def test_end_trace_records_the_determination_in_metadata():
    """The determination rides on the metadata rather than in its own column —
    and it must MERGE, not replace: the creation-time metadata (here the quota
    accounting) is what the Trace Explorer's other panels read."""
    trace_data, _ctx = _end_trace_capturing(
        error="boom", failure=classify_run_failure(Exception("overloaded"))
    )
    assert trace_data.metadata["failure_determination"] == "heuristic"
    assert trace_data.metadata["failure_cause"] == "provider_overloaded"
    assert trace_data.metadata["quota_reason"] == "charged"


def test_a_successful_trace_carries_no_cause():
    trace_data, _ctx = _end_trace_capturing(output="all good")
    assert trace_data.status == "ok"
    assert trace_data.failure_cause is None


def test_an_unclassified_failure_still_ends_the_trace():
    """Callers not yet wired to the classifier must keep working — the column
    reads NULL, which the UI shows as "no cause recorded" rather than as
    ``unknown``."""
    trace_data, _ctx = _end_trace_capturing(error="legacy failure")
    assert trace_data.status == "error"
    assert trace_data.failure_cause is None


# ── Backend / frontend drift ─────────────────────────────────────────────────

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_the_frontend_lists_exactly_the_backend_causes():
    """``frontend/src/lib/failure-causes.ts`` mirrors this enum by hand.

    A cause missing there is invisible in the Trace Explorer's filter — the one
    place anyone would go to empty the unattributed pool — and a cause present
    there but not here is a 422 on click. Neither failure announces itself, so
    the drift is caught at test time instead.
    """
    source = (
        BACKEND_ROOT.parent / "frontend/src/lib/failure-causes.ts"
    ).read_text(encoding="utf-8")

    # Every quoted string inside the FAILURE_CAUSE_GROUPS literal's `causes`
    # arrays, plus the sentinel constant referenced symbolically in the last
    # group.
    groups_block = source.split("FAILURE_CAUSE_GROUPS", 1)[1].split("] as const;", 1)[0]
    listed = {
        value
        for block in re.findall(r"causes:\s*\[(.*?)\]", groups_block, re.S)
        for value in re.findall(r'"([a-z_]+)"', block)
    }
    sentinel = re.search(
        r'UNRECORDED_FAILURE_CAUSE = "([a-z_]+)"', source
    ).group(1)
    listed.discard(sentinel)

    assert listed == set(FAILURE_CAUSE_VALUES)


def test_the_frontend_labels_every_cause_it_lists():
    """An unlabelled cause falls back to the raw enum value in the filter."""
    source = (
        BACKEND_ROOT.parent / "frontend/src/lib/failure-causes.ts"
    ).read_text(encoding="utf-8")
    labels_block = source.split("FAILURE_CAUSE_LABELS", 1)[1].split("};", 1)[0]
    labelled = set(re.findall(r"^\s{2}([a-z_]+):", labels_block, re.M))

    assert set(FAILURE_CAUSE_VALUES) <= labelled
