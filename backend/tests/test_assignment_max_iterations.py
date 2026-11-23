"""Assignment ``max_iterations`` precedence.

The resolution order is *most specific scope wins*: the assignment's own
override, then the agent's override, then the platform default
(``GeneralAssistantConfig.max_iterations``). A set assignment value therefore
OVERRIDES the agent. ``None`` at a level means "inherit from the next-broader
scope". This locks the contract independently of the (DB-bound) engine run.
"""

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.api.v1 import assignments as assignments_api
from app.api.v1.schemas.assignment import UpdateAssignmentRequest
from app.models.general_assistant_config import MAX_ITERATIONS_CEILING
from app.services import assignment_engine
from app.services.assignment_engine import (
    _MIN_ASSIGNMENT_TIMEOUT_SECONDS,
    _MIN_SECS_PER_ITERATION,
    _UNSET_TIMEOUT_FALLBACK_SECONDS,
    _apply_iteration_timeout_floor,
    resolve_assignment_tool_timeout,
    resolve_effective_max_iterations,
    resolve_run_max_iterations,
)


def test_assignment_override_wins_over_agent_and_platform():
    # The assignment is the most specific scope: its value overrides the agent.
    assert resolve_effective_max_iterations(40, 20, 25) == 40


def test_agent_override_used_when_assignment_inherits():
    # Assignment NULL -> inherit the agent's override (not the platform default).
    assert resolve_effective_max_iterations(None, 20, 25) == 20


def test_platform_default_when_both_inherit():
    # Both NULL -> platform default.
    assert resolve_effective_max_iterations(None, None, 25) == 25


def test_assignment_value_wins_even_when_lower_than_agent():
    # Precedence is by scope, not by magnitude: a lower assignment value still
    # overrides a higher agent value (the user explicitly narrowed this run).
    assert resolve_effective_max_iterations(5, 80, 25) == 5


def test_resolver_clamps_out_of_range_assignment_value_at_runtime():
    # A legacy/out-of-band assignment value above the ceiling is capped at
    # runtime so the agentic loop honours the safety ceiling (the stored value
    # is left untouched and corrected on its next save).
    assert resolve_effective_max_iterations(5000, None, 25) == MAX_ITERATIONS_CEILING


def test_resolver_clamps_out_of_range_agent_value_at_runtime():
    # The agent fallback is likewise capped when the assignment inherits.
    assert resolve_effective_max_iterations(None, 9999, 25) == MAX_ITERATIONS_CEILING


def test_resolver_floors_zero_platform_default_to_one():
    # A stored platform default of 0 (legacy / direct DB edit) must floor to 1,
    # not silently jump to the Python default. The engine now feeds a stored 0
    # through verbatim (explicit ``is None`` check, no falsy ``or``), so the
    # clamp here is the last line of defense.
    assert resolve_effective_max_iterations(None, None, 0) == 1


def test_assignment_tool_timeout_only_applies_to_high_iteration_runs():
    # Keep existing loop defaults for normal iteration budgets, including the
    # exact gate boundary (30 iterations = no cap; the cap starts above it).
    assert resolve_assignment_tool_timeout(900, 25) is None
    assert resolve_assignment_tool_timeout(1800, 30) is None
    # Above the gate the cap is a share of the budget: 1800 / 5 = 360 -> 300.
    assert resolve_assignment_tool_timeout(1800, 31) == 300
    # At or below the gate a run keeps the loop default per tool call (a
    # tighter cap on every standard run was evaluated and rejected -
    # the root cause there was memory-driven prompt bloat, fixed by the memory
    # caps).
    assert resolve_assignment_tool_timeout(600, 25) is None


@pytest.mark.parametrize("stored_timeout", [None, 300, 600])
def test_a_run_at_the_platform_default_keeps_the_loop_default_per_tool(stored_timeout):
    # Contract: a default-configured assignment never gets its tools capped
    # below the loop default, which would break legitimate long tool calls
    # (code interpreter, large MCP/KB operations) the 300s loop default
    # deliberately allows. Either the default sits at or below the gate, or
    # its iteration floor leaves one tool call the whole loop default.
    from app.models.general_assistant_config import DEFAULT_MAX_ITERATIONS
    from app.tools.agent_loop import TOOL_EXECUTION_TIMEOUT_SECONDS

    budget = _apply_iteration_timeout_floor(
        stored_timeout or _UNSET_TIMEOUT_FALLBACK_SECONDS,
        DEFAULT_MAX_ITERATIONS,
        "run-id",
        stored_timeout,
    )
    cap = resolve_assignment_tool_timeout(budget, DEFAULT_MAX_ITERATIONS)
    assert cap is None or cap >= TOOL_EXECUTION_TIMEOUT_SECONDS


def test_assignment_tool_timeout_skips_disabled_run_timeouts():
    # ``None`` here is the engine-normalized "per-run timeout disabled" form
    # (a stored ``<= 0`` becomes ``None`` before the floor is applied) — no
    # wall-clock budget means no derived cap, regardless of iterations.
    assert resolve_assignment_tool_timeout(None, 40) is None
    # Defensive-only: a normalized budget is never ``<= 0``, but the guard
    # keeps a misused call site from deriving a bogus floor cap.
    assert resolve_assignment_tool_timeout(0, 40) is None
    assert resolve_assignment_tool_timeout(-1, 40) is None


def test_assignment_tool_timeout_composes_with_effective_budget_pipeline():
    # The engine contract: the stored value is normalized (None ->
    # _UNSET_TIMEOUT_FALLBACK_SECONDS, <= 0 -> disabled/None) and
    # iteration-floored BEFORE the per-tool cap is derived. An unset
    # attribute at 40 iterations gets the 300s fallback raised to the
    # 40 x 20s = 800s iteration floor, then a 160s per-tool cap
    # (800 / 5). A disabled
    # timeout (normalized ``None``) never derives a cap.
    effective = _apply_iteration_timeout_floor(
        _UNSET_TIMEOUT_FALLBACK_SECONDS, 40, "run-id", None
    )
    assert effective == 40 * _MIN_SECS_PER_ITERATION
    assert resolve_assignment_tool_timeout(effective, 40) == 160
    assert resolve_assignment_tool_timeout(None, 40) is None


def test_assignment_tool_timeout_budget_derivation_for_high_iteration_runs():
    # One tool call may take a fifth of the run budget: 1000s -> 200s.
    assert resolve_assignment_tool_timeout(1000, 40) == 200
    # The iteration count no longer divides the budget.
    assert resolve_assignment_tool_timeout(1000, 100) == 200


def test_assignment_tool_timeout_is_bounded():
    # Floor at 60s and cap at the loop default 300s.
    assert resolve_assignment_tool_timeout(200, 40) == 60
    assert resolve_assignment_tool_timeout(20_000, 40) == 300


def test_the_30_to_31_iteration_transition_is_explicit():
    """The gate is deliberate: at 30 iterations the loop default (300 s) is
    untouched; from 31 one tool call may take a fifth of the run budget. With
    the default timeout the 31-iteration budget is 620 s, so a tool call gets
    124 s and no single call can eat half the run."""
    budget_30 = _apply_iteration_timeout_floor(_UNSET_TIMEOUT_FALLBACK_SECONDS, 30, "run-id", 300)
    budget_31 = _apply_iteration_timeout_floor(_UNSET_TIMEOUT_FALLBACK_SECONDS, 31, "run-id", 300)
    assert resolve_assignment_tool_timeout(budget_30, 30) is None
    assert budget_31 == 620
    assert resolve_assignment_tool_timeout(budget_31, 31) == 124


@pytest.mark.parametrize("stored_timeout", [None, 600, 900, 1800, 3600])
def test_a_bigger_iteration_budget_never_shrinks_the_per_tool_cap(stored_timeout):
    """With the default 300 s timeout, raising an assignment above 30
    iterations cut every tool call from the 300 s loop default to the 30 s
    floor, because the iteration floor makes budget / iterations >= 20 s; with
    a 3600 s timeout the cap even fell from 116 s to 36 s between 31 and 100
    iterations. Above the gate the cap now only grows with the iteration
    budget, whatever the stored timeout, and 100 iterations get the full loop
    default."""
    from app.services.assignment_engine import _TOOL_CAP_MIN_ITERATIONS
    from app.tools.agent_loop import TOOL_EXECUTION_TIMEOUT_SECONDS

    caps = [
        resolve_assignment_tool_timeout(
            _apply_iteration_timeout_floor(
                stored_timeout or _UNSET_TIMEOUT_FALLBACK_SECONDS,
                max_iterations,
                "run-id",
                stored_timeout,
            ),
            max_iterations,
        )
        for max_iterations in range(_TOOL_CAP_MIN_ITERATIONS + 1, MAX_ITERATIONS_CEILING + 1)
    ]
    assert caps == sorted(caps)
    assert min(caps) >= 124
    assert caps[-1] == TOOL_EXECUTION_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# update_assignment: server-side ceiling clamp (mirrors update_agent).
#
# The per-assignment ``max_iterations`` column historically accepted arbitrary
# ints (no schema bound, no server-side clamp), so legacy rows can hold values
# above ``MAX_ITERATIONS_CEILING``. ``update_assignment`` now clamps an explicit
# value AND defensively re-clamps a stored out-of-range value even on an
# unrelated update, while preserving ``None`` (= inherit) and never rewriting an
# in-range value (no spurious version churn). These drive the real route with a
# stubbed serialiser, mirroring ``test_agent_update_partial_payload``.
# ---------------------------------------------------------------------------


class _AgentResult:
    def __init__(self, agent):
        self._agent = agent

    def scalar_one_or_none(self):
        return self._agent


class _FakeDB:
    """Minimal async session: the parent-agent lookup is the only query
    ``update_assignment`` runs for these payloads (no trigger / run-as / KB
    edits, so no other ``execute`` is reached)."""

    def __init__(self, agent):
        self._agent = agent

    async def execute(self, _stmt):
        return _AgentResult(self._agent)

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None


def _fake_assignment(max_iterations):
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Assignment",
        max_iterations=max_iterations,
        trigger_type="manual",
        trigger_config={},
        is_company_managed=False,
        # Read when the route builds the audit ``details`` dict.
        enabled=True,
        delay=None,
        ai_label=False,
        human_in_the_loop=False,
        # Read by the run-as handling of the update route (#9188).
        run_as_user_id=None,
    )


async def _invoke_update_assignment(monkeypatch, assignment, body):
    agent = SimpleNamespace(
        id=assignment.agent_id, is_company_managed=False, name="Agent"
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")

    async def _groups(_db, _uid):
        return []

    async def _get_assignment(_db, _uid, _aid, _gids, **_kwargs):
        return assignment

    def _assert_writable(*_args, **_kwargs):
        return None

    async def _audit(*_args, **_kwargs):
        return None

    def _to_response(w, **_kwargs):
        return {"id": str(w.id), "max_iterations": w.max_iterations}

    monkeypatch.setattr(assignments_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(assignments_api, "get_user_assignment", _get_assignment)
    monkeypatch.setattr(
        assignments_api, "assert_writable_assignment", _assert_writable
    )
    monkeypatch.setattr(assignments_api, "log_audit", _audit)
    monkeypatch.setattr(assignments_api, "_to_response", _to_response)
    monkeypatch.setattr(assignments_api, "_owner_paused", AsyncMock(return_value=False))

    request = Request(
        {"type": "http", "method": "PUT", "path": "/x", "headers": []}
    )
    return await assignments_api.update_assignment(
        str(assignment.id), body, request, user, _FakeDB(agent)
    )


@pytest.mark.asyncio
async def test_update_assignment_clamps_explicit_over_ceiling(monkeypatch):
    """An explicit max_iterations above the ceiling is clamped to it on save."""
    assignment = _fake_assignment(15)
    await _invoke_update_assignment(
        monkeypatch, assignment, UpdateAssignmentRequest(max_iterations=5000)
    )
    assert assignment.max_iterations == MAX_ITERATIONS_CEILING


@pytest.mark.asyncio
async def test_update_assignment_clamps_legacy_out_of_range_on_unrelated_update(
    monkeypatch,
):
    """A legacy out-of-range value is pulled back under the ceiling even when the
    update doesn't touch max_iterations (mirrors ``update_agent``)."""
    assignment = _fake_assignment(2000)  # legacy, never clamped server-side
    await _invoke_update_assignment(
        monkeypatch, assignment, UpdateAssignmentRequest(name="Renamed")
    )
    assert assignment.max_iterations == MAX_ITERATIONS_CEILING


@pytest.mark.asyncio
async def test_update_assignment_leaves_inherit_and_in_range_untouched(monkeypatch):
    """NULL (= inherit) stays NULL and an in-range value is not rewritten by the
    defensive clamp on an unrelated update (no spurious change / version churn)."""
    inherit = _fake_assignment(None)
    await _invoke_update_assignment(
        monkeypatch, inherit, UpdateAssignmentRequest(name="Renamed")
    )
    assert inherit.max_iterations is None

    in_range = _fake_assignment(50)
    await _invoke_update_assignment(
        monkeypatch, in_range, UpdateAssignmentRequest(name="Renamed")
    )
    assert in_range.max_iterations == 50


# ---------------------------------------------------------------------------
# _apply_iteration_timeout_floor: effective timeout scaling
#
# The effective wall-clock timeout must be at least
# effective_max_iterations × _MIN_SECS_PER_ITERATION so that an assignment
# with a high iteration budget (e.g. max_iterations=50) but the default 300 s
# timeout_seconds doesn't expire before exhausting its iteration budget.
# ---------------------------------------------------------------------------


def test_floor_raises_low_timeout_to_iteration_minimum():
    """A 300 s timeout with max_iterations=50 must be raised to the floor."""
    floor_value = 50 * _MIN_SECS_PER_ITERATION  # e.g. 1000 s
    result = _apply_iteration_timeout_floor(300.0, 50, "run-1", 300)
    assert result == float(floor_value)


def test_floor_raises_low_timeout_incident_scenario():
    """A reported run: max_iterations=50, timeout=300."""
    result = _apply_iteration_timeout_floor(300.0, 50, "ac12ad95", 300)
    assert result == float(50 * _MIN_SECS_PER_ITERATION)
    assert result > 300.0


def test_floor_raises_low_timeout_30_iterations_scenario():
    """Regression: max_iterations=30 with timeout=300."""
    result = _apply_iteration_timeout_floor(300.0, 30, "run-30", 300)
    assert result == float(30 * _MIN_SECS_PER_ITERATION)
    assert result > 300.0


def test_floor_raises_low_timeout_100_iterations_scenario():
    """Regression: max_iterations=100 with timeout=300."""
    result = _apply_iteration_timeout_floor(300.0, 100, "run-100", 300)
    assert result == float(100 * _MIN_SECS_PER_ITERATION)
    assert result > 300.0


def test_floor_does_not_reduce_generous_explicit_timeout():
    """An operator-configured timeout already above the floor is never reduced."""
    # 3600 s explicit timeout with max_iterations=25 → floor is 25×20=500 s < 3600 s
    result = _apply_iteration_timeout_floor(3600.0, 25, "run-3", 3600)
    assert result == 3600.0


def test_floor_preserves_none_timeout_disabled():
    """When timeout is None (disabled), the floor is not applied."""
    result = _apply_iteration_timeout_floor(None, 50, "run-4", 0)
    assert result is None


def test_floor_returns_float_for_int_input():
    """Callers pass ints (e.g. the unset-row fallback) — the enabled case
    must always return a float per the ``float | None`` annotation, including
    the above-the-floor pass-through branch."""
    result = _apply_iteration_timeout_floor(3600, 25, "run-int", 3600)
    assert isinstance(result, float)
    assert result == 3600.0
    raised = _apply_iteration_timeout_floor(300, 50, "run-int-2", 300)
    assert isinstance(raised, float)


def test_floor_exact_match_is_not_raised():
    """A timeout exactly equal to the floor is not changed."""
    exact = float(max(_MIN_ASSIGNMENT_TIMEOUT_SECONDS, 25 * _MIN_SECS_PER_ITERATION))
    result = _apply_iteration_timeout_floor(exact, 25, "run-5", int(exact))
    assert result == exact


def test_floor_default_iterations_with_default_timeout_raises():
    """Default max_iterations=25 with default timeout=300 is raised to floor."""
    floor_value = max(
        _MIN_ASSIGNMENT_TIMEOUT_SECONDS, 25 * _MIN_SECS_PER_ITERATION
    )
    result = _apply_iteration_timeout_floor(300.0, 25, "run-6", 300)
    assert result == float(floor_value)
    assert result > 300.0


def test_floor_uses_static_minimum_for_low_iteration_runs():
    """max_iterations=20 must floor to at least 600s (not the old 400s)."""
    result = _apply_iteration_timeout_floor(300.0, 20, "run-2319", 300)
    assert result == float(_MIN_ASSIGNMENT_TIMEOUT_SECONDS)
    assert result > float(20 * _MIN_SECS_PER_ITERATION)


def test_floor_raises_timeout_logs_info(caplog):
    """An INFO log is emitted when the floor auto-raises the effective timeout."""
    import logging

    with caplog.at_level(logging.INFO, logger="app.services.assignment_engine"):
        _apply_iteration_timeout_floor(300.0, 50, "run-log", 300)

    assert any("effective timeout raised" in r.message for r in caplog.records)
    assert any("run-log" in r.message for r in caplog.records)


def test_floor_no_log_when_already_sufficient(caplog):
    """No log is emitted when the configured timeout already exceeds the floor."""
    import logging

    with caplog.at_level(logging.INFO, logger="app.services.assignment_engine"):
        _apply_iteration_timeout_floor(9999.0, 50, "run-nolog", 9999)

    assert not any("effective timeout raised" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# resolve_run_max_iterations: shared assignment→agent→platform resolution with
# the GeneralAssistantConfig lookup, used by the loop setup AND the deadline
# floor call sites so the two bounds can never drift.
# ---------------------------------------------------------------------------


def _fake_db_with_platform_max_iterations(value):
    return SimpleNamespace(
        execute=_make_execute(value),
    )


def _make_execute(value):
    async def _execute(_stmt, _params=None):
        return SimpleNamespace(scalar_one_or_none=lambda: value)

    return _execute


@pytest.mark.asyncio
async def test_resolve_run_max_iterations_uses_ga_row():
    db = _fake_db_with_platform_max_iterations(40)
    assignment = SimpleNamespace(max_iterations=None)
    agent = SimpleNamespace(max_iterations=None)
    assert await resolve_run_max_iterations(db, assignment, agent) == 40


@pytest.mark.asyncio
async def test_resolve_run_max_iterations_missing_ga_row_uses_code_default():
    from app.models.general_assistant_config import DEFAULT_MAX_ITERATIONS

    db = _fake_db_with_platform_max_iterations(None)
    assignment = SimpleNamespace(max_iterations=None)
    agent = SimpleNamespace(max_iterations=None)
    assert (
        await resolve_run_max_iterations(db, assignment, agent)
        == DEFAULT_MAX_ITERATIONS
    )


@pytest.mark.asyncio
async def test_resolve_run_max_iterations_assignment_override_wins():
    db = _fake_db_with_platform_max_iterations(25)
    assignment = SimpleNamespace(max_iterations=50)
    agent = SimpleNamespace(max_iterations=10)
    assert await resolve_run_max_iterations(db, assignment, agent) == 50


# ---------------------------------------------------------------------------
# Source contracts (the repo's established pattern for the engine's giant
# function bodies, mirroring test_sub_agent_budget_inheritance.py): the
# iteration floor must ALSO be applied to the orchestration ``deadline_at``
# computations — without it the reconciler's deadline-derived task budget
# (``_execution_timeout``) and ``sweep_deadlines`` would still kill a
# high-``max_iterations`` run at ``timeout_seconds`` + grace, defeating the
# ``asyncio.wait_for`` floor entirely.
# ---------------------------------------------------------------------------


def test_trigger_assignment_deadline_applies_iteration_floor():
    # ``trigger_assignment`` delegates the deadline computation to
    # ``_schedule_run_execution``, which the delivery-queue drain also
    # calls — so a de-queued delivery gets the same floor as an immediate
    # trigger. Assert on the shared helper, and pin the delegation so the
    # contract can't be bypassed by re-inlining a floor-less copy.
    assert (
        "_schedule_run_execution("
        in inspect.getsource(assignment_engine.trigger_assignment)
    )
    source = inspect.getsource(assignment_engine._schedule_run_execution)
    assert "_apply_iteration_timeout_floor(" in source
    assert "resolve_run_max_iterations(" in source


def test_retry_path_deadline_applies_iteration_floor():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    retry_segment = source.split("# --- Retry on failure ---", 1)[1]
    assert "_apply_iteration_timeout_floor(" in retry_segment
    assert "resolve_run_max_iterations(" in retry_segment
    # Disabled timeout (<= 0) must still carry no deadline on the retry.
    assert "retry_deadline, retry_budget = None, {}" in retry_segment


def test_agentic_loop_wait_for_applies_iteration_floor():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    loop_segment = source.split("_loop_started = time.monotonic()", 1)[0]
    assert "_apply_iteration_timeout_floor(" in loop_segment


def test_unset_timeout_fallback_is_shared_across_all_bounds():
    """A ``None`` ``timeout_seconds`` (unflushed in-memory row) must fall back
    to the SAME value at the agentic loop's ``asyncio.wait_for`` bound and at
    both orchestration ``deadline_at`` sites (trigger + retry). The loop used
    to fall back to 900 s while the deadlines used 300 s, so a None row could
    be dead-lettered by the reconciler at ~300 s + grace while the inner loop
    still believed it had 900 s — the exact bound drift this fix eliminates."""
    assert assignment_engine._UNSET_TIMEOUT_FALLBACK_SECONDS == 300

    run_source = inspect.getsource(assignment_engine.execute_assignment_run)
    loop_segment = run_source.split("_loop_started = time.monotonic()", 1)[0]
    assert "_ts or _UNSET_TIMEOUT_FALLBACK_SECONDS" in loop_segment
    assert "_ts or 900" not in loop_segment

    retry_segment = run_source.split("# --- Retry on failure ---", 1)[1]
    assert "retry_ts = _UNSET_TIMEOUT_FALLBACK_SECONDS" in retry_segment

    # The trigger-side deadline lives in the shared ``_schedule_run_execution``
    # helper — same code for an immediate trigger and for a queued
    # delivery promoted later by the reconciler.
    trigger_source = inspect.getsource(assignment_engine._schedule_run_execution)
    assert "timeout_seconds = _UNSET_TIMEOUT_FALLBACK_SECONDS" in trigger_source


# ---------------------------------------------------------------------------
# Template default_assignments now wire through timeout_seconds / max_iterations
# ("Learn writing style from sent emails" timed out at 400s because
# the template-based assignment creation path was ignoring these fields and
# every template-born assignment fell back to the 300s column default).
# ---------------------------------------------------------------------------


def test_email_style_learning_template_has_generous_timeout():
    """The 'Learn writing style from sent emails' template assignment must carry
    an explicit timeout_seconds that is larger than the default 300 s. The
    nightly style-learning run fetches up to 30 sent emails from Microsoft Graph
    which can burn >90 s in 429 back-off retries alone."""
    from app.agents.templates import BUILTIN_TEMPLATES

    mail_desk = next(t for t in BUILTIN_TEMPLATES if t["slug"] == "email-triage")
    style_assignment = next(
        a
        for a in mail_desk["default_assignments"]
        if a["name"] == "Learn writing style from sent emails"
    )
    assert "timeout_seconds" in style_assignment, (
        "Missing timeout_seconds on 'Learn writing style from sent emails' template "
        "assignment — without it the assignment falls back to the 300 s default "
        "which is too tight for paginated Microsoft Graph calls with 429 retries."
    )
    assert style_assignment["timeout_seconds"] > 300, (
        f"timeout_seconds={style_assignment['timeout_seconds']} is not larger than "
        "the default 300 s and will not help with the Microsoft Graph throttling "
        "timeout."
    )


def test_create_agent_template_path_wires_timeout_and_max_iterations():
    """The template-based assignment creation block in agents.py must read
    'timeout_seconds' and 'max_iterations' from the template's default_assignments
    entry (they were silently ignored, leaving every template-born
    assignment at the 300 s / NULL defaults)."""
    from app.api.v1 import agents as agents_api

    # Use inspect.getsource to pin the contract without requiring a full DB.
    source = inspect.getsource(agents_api.create_agent)
    # Find the template-based assignment creation block by a unique anchor.
    assert "create_default_assignments" in source
    # Verify both new fields are now wired.
    assert 'wf_data.get("timeout_seconds"' in source, (
        "agents.create_agent does not pass 'timeout_seconds' from template "
        "default_assignments data to the Assignment constructor."
    )
    assert 'wf_data.get("max_iterations")' in source, (
        "agents.create_agent does not pass 'max_iterations' from template "
        "default_assignments data to the Assignment constructor."
    )
