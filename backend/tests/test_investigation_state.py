"""Invariants of the Deep Investigation step-state machine.

These tests exist because the failure they guard against is silent: a step that
failed but renders as a success looks exactly like a step that succeeded, and
an analyst sourcing a decision on the report has no way to tell.  The prior art
that motivated this feature shipped precisely that bug — 3 of 8 source calls failed
and every step still displayed as complete, next to a hardcoded 94% confidence.

No database: the module's invariants are pure state logic, so a stub session is
enough and the suite stays in the fast (non-postgres) tier.  Persistence itself
is exercised by the migration integrity suite and, later, by the orchestrator's
own tests.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.investigation import (
    MAX_END_REASON_LENGTH,
    MAX_STATUS_DETAIL_LENGTH,
    MAX_SUBJECT_LENGTH,
    Investigation,
    InvestigationStep,
)
from app.services.investigation_state import (
    InvalidStepTransitionError,
    PlannedStep,
    append_lead_step,
    create_investigation,
    duration_seconds,
    finish_run,
    mark_step_active,
    mark_step_skipped,
    plan_steps,
    record_step_error,
    record_step_result,
    start_run,
    summarize,
)


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _StubSession:
    """Minimal async-session stand-in: records adds, no-ops the flush.

    ``execute`` answers the single query the module makes (the last position of
    a run, for ``append_lead_step``) with whatever ``last_position`` says.
    """

    def __init__(self, last_position=None):
        self.added: list[object] = []
        self.last_position = last_position
        self.flushes = 0

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1

    async def execute(self, _stmt):
        return _Result(self.last_position)


def _run(**kwargs) -> Investigation:
    return Investigation(
        subject_type=kwargs.pop("subject_type", "threat_actor"),
        subject_value=kwargs.pop("subject_value", "APT29"),
        source_pack_slugs=kwargs.pop("source_pack_slugs", ["cti-threat-actor"]),
        status=kwargs.pop("status", "running"),
        revision=kwargs.pop("revision", 1),
        iterations_used=kwargs.pop("iterations_used", 0),
        max_iterations=kwargs.pop("max_iterations", 10),
        **kwargs,
    )


def _step(status: str = "pending", **kwargs) -> InvestigationStep:
    return InvestigationStep(
        position=kwargs.pop("position", 0),
        source_name=kwargs.pop("source_name", "VirusTotal"),
        status=status,
        findings_count=kwargs.pop("findings_count", 0),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Invariant 1 — a failure never becomes a success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_type_forces_error_status_even_with_findings():
    """An error_type wins over everything the source still managed to return.

    A partially-readable rate-limited response that also errored must not be
    upgraded to `completed` just because `findings_count` was non-zero.
    """
    db, run, step = _StubSession(), _run(), _step()
    status = await record_step_result(db, run, step, findings_count=7, error_type="timeout")
    assert status == "error"
    assert step.status == "error"


@pytest.mark.asyncio
async def test_zero_findings_is_empty_not_completed():
    """`empty` is a distinct outcome: the source answered, and found nothing."""
    db, run, step = _StubSession(), _run(), _step()
    status = await record_step_result(db, run, step, findings_count=0)
    assert status == "empty"
    assert step.status != "completed"


@pytest.mark.asyncio
async def test_partial_result_is_degraded():
    db, run, step = _StubSession(), _run(), _step()
    status = await record_step_result(db, run, step, findings_count=3, partial=True)
    assert status == "degraded"


@pytest.mark.asyncio
async def test_findings_produce_completed():
    db, run, step = _StubSession(), _run(), _step()
    status = await record_step_result(
        db,
        run,
        step,
        findings_count=4,
        evidence=[{"kind": "url", "label": "https://example.test/a", "href": "https://example.test/a"}],
    )
    assert status == "completed"
    assert step.evidence == [
        {"kind": "url", "label": "https://example.test/a", "href": "https://example.test/a"}
    ]


@pytest.mark.asyncio
async def test_record_step_result_has_no_status_parameter():
    """The API must not let a caller assert a status directly.

    This is the structural half of invariant 1: statuses are derived, so there
    is no keyword through which an orchestrator bug (or a future refactor)
    could hand in `completed` for a source that failed.
    """
    db, run, step = _StubSession(), _run(), _step()
    with pytest.raises(TypeError):
        await record_step_result(db, run, step, findings_count=0, status="completed")


@pytest.mark.asyncio
async def test_record_step_error_goes_through_the_same_derivation():
    db, run, step = _StubSession(), _run(), _step()
    await record_step_error(db, run, step, error_type="auth_error", detail="401")
    assert step.status == "error"
    assert step.error_type == "auth_error"
    assert step.status_detail == "401"


@pytest.mark.asyncio
async def test_unknown_error_type_is_rejected():
    """Error types are the telemetry taxonomy, not free text.

    A typo'd error type would silently break the block's error grouping and the
    `investigation.source_error` metrics that share the vocabulary.
    """
    db, run, step = _StubSession(), _run(), _step()
    with pytest.raises(ValueError, match="unknown error_type"):
        await record_step_result(db, run, step, findings_count=0, error_type="rate-limit")


@pytest.mark.asyncio
async def test_terminal_step_cannot_be_rewritten():
    db, run = _StubSession(), _run()
    step = _step()
    await record_step_result(db, run, step, findings_count=2)
    with pytest.raises(InvalidStepTransitionError):
        await record_step_result(db, run, step, findings_count=0)


@pytest.mark.asyncio
async def test_terminal_step_cannot_be_reactivated():
    db, run = _StubSession(), _run()
    step = _step()
    await record_step_error(db, run, step, error_type="unreachable")
    with pytest.raises(InvalidStepTransitionError):
        await mark_step_active(db, run, step, query="retry")


@pytest.mark.asyncio
async def test_terminal_step_cannot_be_skipped():
    db, run = _StubSession(), _run()
    step = _step()
    await record_step_result(db, run, step, findings_count=1)
    with pytest.raises(InvalidStepTransitionError):
        await mark_step_skipped(db, run, step, reason="budget spent")


# ---------------------------------------------------------------------------
# Invariant 2 — no unearned confidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_is_dropped_on_empty():
    """Confidence in nothing is not a number the block may show."""
    db, run, step = _StubSession(), _run(), _step()
    await record_step_result(db, run, step, findings_count=0, confidence=0.9)
    assert step.status == "empty"
    assert step.confidence is None


@pytest.mark.asyncio
async def test_confidence_is_dropped_on_error():
    db, run, step = _StubSession(), _run(), _step()
    await record_step_result(db, run, step, findings_count=0, confidence=0.9, error_type="timeout")
    assert step.confidence is None


@pytest.mark.asyncio
async def test_confidence_is_kept_on_completed_and_degraded():
    db, run = _StubSession(), _run()
    completed = _step()
    await record_step_result(db, run, completed, findings_count=2, confidence=0.8)
    assert completed.confidence == pytest.approx(0.8)

    degraded = _step(position=1)
    await record_step_result(db, run, degraded, findings_count=1, confidence=0.4, partial=True)
    assert degraded.confidence == pytest.approx(0.4)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [-0.1, 1.1, float("nan"), float("inf")])
async def test_out_of_range_and_non_finite_confidence_rejected(bad):
    """NaN would pass a naive range check and then render as "NaN"."""
    db, run, step = _StubSession(), _run(), _step()
    with pytest.raises(ValueError, match="confidence"):
        await record_step_result(db, run, step, findings_count=1, confidence=bad)


def test_summary_confidence_is_none_when_no_step_carries_one():
    steps = [_step("empty"), _step("error", position=1)]
    assert summarize(steps).confidence is None


def test_summary_confidence_averages_only_present_values():
    steps = [
        _step("completed", confidence=0.6, findings_count=1),
        _step("completed", position=1, confidence=1.0, findings_count=1),
        _step("empty", position=2),
    ]
    assert summarize(steps).confidence == pytest.approx(0.8)


def test_summary_says_how_many_steps_the_confidence_is_a_mean_of():
    """The mean is unweighted and only over steps that carry one, so 0.9 can
    be one step out of eleven. The count travels with it so a reader is told
    rather than left to assume the run scored it."""
    steps = [
        _step("completed", confidence=0.9, findings_count=1),
        *(_step("empty", position=i) for i in range(1, 11)),
    ]
    summary = summarize(steps)
    assert summary.confidence == pytest.approx(0.9)
    assert summary.confidence_samples == 1, "a mean of one must not read as a mean of eleven"
    assert summary.total == 11


def test_summary_reports_no_confidence_samples_when_no_step_carries_one():
    assert summarize([_step("empty"), _step("error", position=1)]).confidence_samples == 0


# ---------------------------------------------------------------------------
# Invariant 3 — no derived duration
# ---------------------------------------------------------------------------


def test_duration_is_none_until_the_run_finished():
    run = _run()
    assert duration_seconds(run) is None
    run.started_at = datetime.now(UTC)
    assert duration_seconds(run) is None


def test_duration_is_measured_wall_clock():
    run = _run()
    run.started_at = datetime(2026, 9, 3, 10, 0, 0, tzinfo=UTC)
    run.completed_at = run.started_at + timedelta(seconds=247)
    assert duration_seconds(run) == pytest.approx(247.0)


# ---------------------------------------------------------------------------
# Aggregate summary — "all passed" must be unreachable while anything is not
# ---------------------------------------------------------------------------


def test_all_succeeded_only_when_every_step_completed():
    steps = [
        _step("completed", findings_count=1),
        _step("completed", position=1, findings_count=1),
    ]
    assert summarize(steps).all_succeeded is True


@pytest.mark.parametrize("other", ["empty", "degraded", "error", "skipped", "pending", "active"])
def test_one_non_success_step_blocks_all_succeeded(other):
    steps = [_step("completed", findings_count=1), _step(other, position=1)]
    assert summarize(steps).all_succeeded is False


def test_all_succeeded_is_false_for_an_empty_run():
    """A run with no steps has not "passed everything" — it did nothing."""
    assert summarize([]).all_succeeded is False


def test_summary_counts_and_derived_properties():
    steps = [
        _step("completed", findings_count=1),
        _step("error", position=1),
        _step("error", position=2),
        _step("skipped", position=3),
        _step("pending", position=4),
        _step("active", position=5),
    ]
    summary = summarize(steps)
    assert summary.total == 6
    assert summary.by_status["error"] == 2
    assert summary.failed == 2
    assert summary.unfinished == 2


# ---------------------------------------------------------------------------
# Skeleton, leads and run lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_investigation_starts_in_planning_with_no_timing():
    db = _StubSession()
    run = await create_investigation(
        db,
        subject_type="cve",
        subject_value="CVE-2024-1234",
        source_pack_slugs=["cti-vulnerability"],
    )
    assert run.status == "planning"
    assert run.started_at is None and run.completed_at is None
    assert run.source_pack_slugs == ["cti-vulnerability"]


@pytest.mark.asyncio
async def test_plan_steps_assigns_contiguous_positions_in_order():
    """Ordering is server-assigned so out-of-order frames cannot reorder it."""
    db, run = _StubSession(), _run()
    steps = await plan_steps(
        db,
        run,
        [
            PlannedStep("VirusTotal", "cti-ioc-basic"),
            PlannedStep("AbuseIPDB", "cti-ioc-basic"),
            PlannedStep("Shodan", "cti-ioc-basic"),
        ],
    )
    assert [s.position for s in steps] == [0, 1, 2]
    assert [s.source_name for s in steps] == ["VirusTotal", "AbuseIPDB", "Shodan"]
    assert all(s.status == "pending" for s in steps)


@pytest.mark.asyncio
async def test_planned_steps_carry_no_iteration_but_leads_do():
    """This is what distinguishes "planned up front" from "found as a lead"."""
    db, run = _StubSession(), _run()
    (planned,) = await plan_steps(db, run, [PlannedStep("MITRE ATT&CK")])
    assert planned.iteration is None

    db.last_position = 0
    lead = await append_lead_step(db, run, source_name="OTX AlienVault", iteration=3)
    assert lead.position == 1
    assert lead.iteration == 3


@pytest.mark.asyncio
async def test_first_lead_on_an_unplanned_run_starts_at_position_zero():
    db, run = _StubSession(last_position=None), _run()
    lead = await append_lead_step(db, run, source_name="Malpedia")
    assert lead.position == 0


@pytest.mark.asyncio
async def test_mark_step_active_records_the_query_actually_issued():
    db, run, step = _StubSession(), _run(), _step()
    await mark_step_active(db, run, step, query="APT29 site:attack.mitre.org", iteration=2)
    assert step.status == "active"
    assert step.query == "APT29 site:attack.mitre.org"
    assert step.started_at is not None
    assert run.iterations_used == 2


@pytest.mark.asyncio
async def test_iterations_used_never_goes_backwards():
    """Parallel source queries report iterations out of order."""
    db, run = _StubSession(), _run()
    await mark_step_active(db, run, _step(), iteration=5)
    await mark_step_active(db, run, _step(position=1), iteration=2)
    assert run.iterations_used == 5


@pytest.mark.asyncio
async def test_skip_reason_is_persisted_for_the_report():
    db, run, step = _StubSession(), _run(), _step()
    await mark_step_skipped(db, run, step, reason="iteration budget spent")
    assert step.status == "skipped"
    assert step.status_detail == "iteration budget spent"


@pytest.mark.asyncio
async def test_start_and_finish_run_stamp_wall_clock():
    db, run = _StubSession(), _run(status="planning")
    await start_run(db, run)
    assert run.status == "running" and run.started_at is not None
    await finish_run(db, run, status="completed", end_reason="no new leads")
    assert run.status == "completed"
    assert run.completed_at is not None
    assert run.end_reason == "no new leads"


@pytest.mark.asyncio
async def test_finish_run_rejects_a_non_terminal_status():
    db, run = _StubSession(), _run()
    with pytest.raises(ValueError, match="invalid terminal run status"):
        await finish_run(db, run, status="running")


@pytest.mark.asyncio
async def test_cancelling_leaves_unfinished_steps_as_they_were():
    """A cancelled run showing two never-started sources is the truth."""
    db, run = _StubSession(), _run()
    steps = await plan_steps(db, run, [PlannedStep("A"), PlannedStep("B"), PlannedStep("C")])
    await record_step_result(db, run, steps[0], findings_count=2)
    await finish_run(db, run, status="cancelled", end_reason="user stopped the run")
    summary = summarize(steps)
    assert summary.unfinished == 2
    assert summary.all_succeeded is False


@pytest.mark.asyncio
async def test_every_state_write_bumps_the_revision():
    """The client discards a frame older than the revision it already holds."""
    db, run = _StubSession(), _run(revision=1)
    (step,) = await plan_steps(db, run, [PlannedStep("VirusTotal")])
    after_plan = run.revision
    await mark_step_active(db, run, step)
    after_active = run.revision
    await record_step_result(db, run, step, findings_count=1)
    assert 1 < after_plan < after_active < run.revision


# ---------------------------------------------------------------------------
# Free text meets bounded columns
#
# Every one of these strings is built from something outside our control — an
# exception message, a skip reason, a subject someone typed. The columns are
# bounded. Truncating at each call site with a repeated literal is correct
# exactly until somebody prefixes the string, and the failure is then a
# `DataError` at flush time, on the error path, which is the worst possible
# place to discover it. So the clamp lives here, against limits read off the
# columns themselves.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_over_long_subject_is_capped_not_rejected_by_postgres():
    """The cap belongs at the boundary, not in one caller. Every entry point —
    the chat turn and the flow-triggered `assignment_run_id` path — reaches
    `create_investigation`, and a caller-side cap only protects its own
    caller."""
    db = _StubSession()
    run = await create_investigation(
        db,
        subject_type="unknown",
        subject_value="A" * (MAX_SUBJECT_LENGTH + 250),
        source_pack_slugs=["web-general"],
    )
    assert len(run.subject_value) == MAX_SUBJECT_LENGTH


@pytest.mark.asyncio
async def test_the_subject_is_stripped_and_an_empty_one_is_refused():
    db = _StubSession()
    run = await create_investigation(
        db,
        subject_type="unknown",
        subject_value="  8.8.8.8\n",
        source_pack_slugs=["web-general"],
    )
    assert run.subject_value == "8.8.8.8"

    for blank in ("", "   ", "\n\t "):
        with pytest.raises(ValueError, match="needs a subject"):
            await create_investigation(
                db,
                subject_type="unknown",
                subject_value=blank,
                source_pack_slugs=["web-general"],
            )


@pytest.mark.asyncio
async def test_an_over_long_status_detail_is_clamped_on_every_writer():
    """`record_step_result` and `mark_step_skipped` both write this column."""
    long = "x" * (MAX_STATUS_DETAIL_LENGTH + 500)

    db, run, step = _StubSession(), _run(), _step()
    await record_step_result(
        db, run, step, findings_count=0, error_type="timeout", detail=long
    )
    assert len(step.status_detail) == MAX_STATUS_DETAIL_LENGTH

    db, run, step = _StubSession(), _run(), _step()
    await mark_step_skipped(db, run, step, reason=long)
    assert len(step.status_detail) == MAX_STATUS_DETAIL_LENGTH


@pytest.mark.asyncio
async def test_an_over_long_end_reason_is_clamped():
    """The abort path builds this one out of an exception message, so it is the
    likeliest to be long and the least affordable to lose."""
    db, run = _StubSession(), _run()
    await finish_run(
        db, run, status="aborted", end_reason="y" * (MAX_END_REASON_LENGTH + 400)
    )
    assert len(run.end_reason) == MAX_END_REASON_LENGTH


@pytest.mark.asyncio
async def test_a_short_value_is_left_exactly_as_it_is():
    """The clamp must not normalise, pad or otherwise touch a value that fits —
    a truncation that fires early would silently corrupt the common case."""
    db, run, step = _StubSession(), _run(), _step()
    await record_step_result(
        db, run, step, findings_count=0, error_type="timeout", detail="no response"
    )
    assert step.status_detail == "no response"

    db, run, step = _StubSession(), _run(), _step()
    await record_step_result(db, run, step, findings_count=2)
    assert step.status_detail is None, "None must survive as None, not become ''"


def test_the_limits_are_read_off_the_columns_they_protect():
    """The point of the constants: a column widened in a migration moves the
    clamp with it, and one narrowed cannot leave a stale larger literal
    behind."""
    assert Investigation.__table__.c.subject_value.type.length == MAX_SUBJECT_LENGTH
    assert Investigation.__table__.c.end_reason.type.length == MAX_END_REASON_LENGTH
    assert (
        InvestigationStep.__table__.c.status_detail.type.length
        == MAX_STATUS_DETAIL_LENGTH
    )
