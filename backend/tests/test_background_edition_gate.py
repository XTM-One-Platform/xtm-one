"""Background work follows the edition, like every interactive surface.

Every request gate asks ONE licence question. The work nobody waits
on - schedules, webhook / integration-event / flow triggers, channel bots,
delegated tasks, queued runs, the worker's AI jobs - asked only the
administrator's AI switch, so a deployment that dropped to Community Edition
(its Enterprise Edition platform lost its license or was disconnected, the XTM
One license was removed) kept every automation configured while it was
Enterprise running AI and spending the included allowance.

Pinned here:

* ``agentic_gate.agentic_refusal`` means exactly ``platform.is_agentic_ai_enabled``
  (switch first, then the licence) and resolves the licence through the one
  predicate - cached per TTL, on its own session, invalidated after the commits
  that change the edition, never raising.
* The downgrade scenario, entry point by entry point, with the edition forced to
  Community Edition: nothing runs, nothing is charged, nothing is disabled, and
  a person on a channel reads the REST refusal's sentence. Enterprise runs, and
  the AI switch keeps its own answers.
* Tripwires: an execution entry point that decides with the switch alone fails
  the build; the modules allowed to are listed with their reason.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.api.v1 import platform as platform_api
from app.services import agentic_gate, ai_state
from app.services.agentic_gate import (
    AI_DISABLED_CODE,
    LICENSE_REQUIRED_CODE,
    REFUSAL_CODES,
    AgenticVerdict,
)
from app.services.failure_causes import FailureCause, FailureDetermination
from tests import app_sources

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"

COMMUNITY = {"ai_on": True, "licensed": False}
ENTERPRISE = {"ai_on": True, "licensed": True}
AI_SWITCHED_OFF = {"ai_on": False, "licensed": True}


def _pin(monkeypatch, *, ai_on: bool, licensed: bool) -> None:
    """Force the two halves of the gate, the way ``test_edition_model`` does:
    the AI switch, and the ONE licence predicate. The cached licence answer is
    dropped, as a real edition change does, so a test may switch editions.

    The switch's cache is primed with the pinned answer too: a switch read
    successfully is what the verdict calls definitive, and a refusal nothing
    was read for is deferred rather than recorded (see the transient tests).
    Like the real read, every pinned read caches its answer again: primed only
    once, the cache's five-second TTL could lapse between two engine calls of
    one test on a loaded runner, and the verdict would read the pinned switch
    as unconfirmed (transient) instead of definitive."""

    async def _ai(_db):
        ai_state.prime_cache(ai_on)
        return ai_on

    async def _licence(_db):
        return licensed

    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai)
    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _licence)
    ai_state.prime_cache(ai_on)
    agentic_gate.invalidate_cache()


def _database_down(*_a, **_k):
    raise OperationalError("SELECT 1", {}, Exception("database failover"))


def _pin_unreadable(monkeypatch, *, half: str) -> None:
    """A cold process during a database failover: ``half`` could not be read.

    ``licence``: the switch reads on, the licence read raises and was never
    read successfully in this process. ``switch``: ``ai_state.is_ai_enabled``
    failed closed (it answers ``False`` to a read error and caches nothing).
    """

    async def _ai(_db):
        if half != "switch":
            # Read successfully: cached, as ``_pin`` does.
            ai_state.prime_cache(True)
        return half != "switch"

    async def _licence(_db):
        if half == "licence":
            _database_down()
        return True

    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai)
    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _licence)
    if half != "switch":
        ai_state.prime_cache(True)
    agentic_gate.invalidate_cache()


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value

    def first(self):
        return self._value

    def all(self):
        return list(self._value or [])

    def scalars(self):
        return self


class _Session:
    """A session that answers queued rows in order and counts everything.

    ``strict`` makes any query an error: the refusal paths must be decided
    before the caller's session is read at all.
    """

    def __init__(self, rows=(), *, strict: bool = False):
        self._rows = list(rows)
        self._strict = strict
        self.executes = 0
        self.commits = 0
        self.added: list = []

    async def execute(self, *_a, **_k):
        if self._strict:
            raise AssertionError("the refused path must not query the database")
        self.executes += 1
        return _Result(self._rows.pop(0) if self._rows else None)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def flush(self, *_a, **_k):
        pass

    def in_transaction(self) -> bool:
        return False

    def add(self, obj):
        self.added.append(obj)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


# ── The gate: one meaning, one licence rule ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ai_on", "licensed", "expected"),
    [
        (True, True, None),
        (True, False, LICENSE_REQUIRED_CODE),
        # The switch is asked first, as ``deps.require_agentic_ai`` does: it is
        # the deterministic answer and the more actionable one.
        (False, True, AI_DISABLED_CODE),
        (False, False, AI_DISABLED_CODE),
    ],
)
async def test_the_gate_means_is_agentic_ai_enabled(monkeypatch, ai_on, licensed, expected):
    _pin(monkeypatch, ai_on=ai_on, licensed=licensed)
    assert await agentic_gate.agentic_refusal(object()) == expected
    assert (expected is None) is await platform_api.is_agentic_ai_enabled(object())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edition", [platform_api.EDITION_ENTERPRISE, platform_api.EDITION_COMMUNITY]
)
async def test_the_licence_half_is_the_edition_rule_not_a_second_one(monkeypatch, edition):
    """Resolved through ``is_agentic_ai_licensed`` -> ``get_deployment_edition``:
    the gate has no licence logic of its own to drift."""

    async def _edition(_db):
        return edition

    async def _ai(_db):
        return True

    monkeypatch.setattr(platform_api.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(platform_api, "get_deployment_edition", _edition)
    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai)
    expected = None if edition == platform_api.EDITION_ENTERPRISE else LICENSE_REQUIRED_CODE
    assert await agentic_gate.agentic_refusal(object()) == expected


@pytest.mark.asyncio
async def test_the_licence_is_read_once_per_ttl_on_a_session_of_its_own(monkeypatch):
    seen: list = []

    async def _licence(db):
        seen.append(db)
        return True

    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _licence)
    monkeypatch.setattr(agentic_gate, "time", SimpleNamespace(monotonic=lambda: clock.now))

    assert await agentic_gate.is_licensed() is True
    assert await agentic_gate.is_licensed() is True
    assert len(seen) == 1, "a per-message / per-tick caller must not re-read"
    # Its own session, never the caller's: the tick or the run mid-transaction
    # does not get the settings singleton and every registration loaded into it.
    assert isinstance(seen[0], AsyncSession)

    clock.now += agentic_gate.CACHE_TTL_SECONDS + 0.1
    await agentic_gate.is_licensed()
    assert len(seen) == 2, "an edition change converges within one TTL"

    agentic_gate.invalidate_cache()
    await agentic_gate.is_licensed()
    assert len(seen) == 3, "the replica serving the change converges at once"


def test_the_ttl_is_the_ai_switchs_own_window():
    """An edition change reaches every replica in the time a switch flip does."""
    assert agentic_gate.CACHE_TTL_SECONDS == ai_state.CACHE_TTL_SECONDS


@pytest.mark.asyncio
async def test_a_failed_licence_read_keeps_the_last_answer_and_fails_closed_without_one(
    monkeypatch,
):
    state = {"fail": True, "attempts": 0}

    async def _flaky(_db):
        state["attempts"] += 1
        if state["fail"]:
            raise OperationalError("SELECT 1", {}, Exception("database down"))
        return True

    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _flaky)
    monkeypatch.setattr(agentic_gate, "time", SimpleNamespace(monotonic=lambda: clock.now))

    # Nothing was ever read: refused, rather than running a model on a
    # deployment that may not be licensed.
    assert await agentic_gate.is_licensed() is False
    # Not cached: the next caller tries again.
    assert await agentic_gate.is_licensed() is False
    assert state["attempts"] == 2

    state["fail"] = False
    assert await agentic_gate.is_licensed() is True
    clock.now += agentic_gate.CACHE_TTL_SECONDS + 0.1

    # A licence does not change in a database blip: refusing here would fail
    # queued work nothing is wrong with. (An edition change does change it:
    # see test_edition_gate_followups.)
    state["fail"] = True
    assert await agentic_gate.is_licensed() is True


@pytest.mark.asyncio
async def test_a_burst_of_callers_shares_one_licence_read(monkeypatch):
    """A burst of channel messages or worker jobs after the TTL expires must
    not stampede the edition read: every caller that misses joins the read in
    progress, and a caller cancelled while waiting does not cancel it."""
    import asyncio

    reads = {"count": 0}
    release = asyncio.Event()

    async def _slow(_db):
        reads["count"] += 1
        await release.wait()
        return True

    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _slow)

    waiters = [asyncio.ensure_future(agentic_gate.is_licensed()) for _ in range(20)]
    await asyncio.sleep(0)
    waiters[0].cancel()
    release.set()
    results = await asyncio.gather(*waiters[1:])

    assert results == [True] * 19
    assert reads["count"] == 1
    assert await agentic_gate.is_licensed() is True
    assert reads["count"] == 1, "cached for the TTL once read"


@pytest.mark.asyncio
async def test_a_read_begun_before_an_edition_change_is_not_cached(monkeypatch):
    """The commit-then-invalidate order leaves one window: a read that started
    before the change and ends after it. It may carry the old edition, so it
    caches nothing, and a caller arriving after the invalidation reads anew."""
    import asyncio

    answers = [True, False]
    release = asyncio.Event()

    async def _licence(_db):
        answer = answers.pop(0)
        if answer:
            await release.wait()
        return answer

    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _licence)

    before = asyncio.ensure_future(agentic_gate.is_licensed())
    await asyncio.sleep(0)
    agentic_gate.invalidate_cache()  # e.g. the EE platform was just purged
    after = await agentic_gate.is_licensed()
    release.set()

    assert await before is True
    assert after is False
    assert agentic_gate._cache is not None and agentic_gate._cache[0] is False
    assert agentic_gate._last_known is False, "nor does it become the fallback"


@pytest.mark.asyncio
async def test_a_programming_error_is_not_read_as_unlicensed(monkeypatch):
    async def _broken(_db):
        raise RuntimeError("a bug in the resolver")

    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _broken)
    with pytest.raises(RuntimeError):
        await agentic_gate.is_licensed()


def test_every_refusal_is_a_failure_cause_of_the_same_value():
    for code in REFUSAL_CODES:
        assert FailureCause(code).value == code
    assert FailureCause.AI_DISABLED.value == AI_DISABLED_CODE
    assert FailureCause.LICENSE_REQUIRED.value == LICENSE_REQUIRED_CODE


@pytest.mark.parametrize("platform_mode", ["xtm_one", "copilot"])
def test_a_refusal_carries_the_rest_refusals_own_sentence(monkeypatch, platform_mode):
    monkeypatch.setattr(deps.settings, "platform_mode", platform_mode)
    assert agentic_gate.refusal_message(AI_DISABLED_CODE) == deps.AI_DISABLED_DETAIL
    assert agentic_gate.refusal_message(LICENSE_REQUIRED_CODE) == deps.licence_required_detail()


def test_refusal_code_recognises_the_two_refusals_and_nothing_else():
    for code in REFUSAL_CODES:
        exc = HTTPException(403, detail={"code": code, "message": "x"})
        assert agentic_gate.refusal_code(exc) == code
    for other in (
        HTTPException(403, detail="Enterprise Edition required."),
        HTTPException(403, detail={"code": "ai_provisioning_busy"}),
        HTTPException(409, detail={"message": "busy"}),
        ValueError("disabled"),
    ):
        assert agentic_gate.refusal_code(other) is None


# ── An unreadable state is not a reason to fail work ────────────────────────
#
# The failure policy refuses when the state cannot be read: nothing runs a
# model on a deployment that may not allow it. Where the refusal only skips or
# holds something back that is enough. Where it would FAIL work - a queued run,
# a queued delegation, a fresh evaluation - a database blip on a cold worker
# must not be recorded as "not licensed": the verdict says the refusal is
# transient, and those sites leave the work for the orchestration engine to
# retry.


@pytest.mark.asyncio
async def test_a_licence_that_could_not_be_read_is_a_transient_refusal(monkeypatch):
    async def _ai(_db):
        return True

    state = {"fail": True, "licensed": False}

    async def _licence(_db):
        if state["fail"]:
            _database_down()
        return state["licensed"]

    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai)
    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _licence)
    monkeypatch.setattr(agentic_gate, "time", SimpleNamespace(monotonic=lambda: clock.now))

    def _expire() -> None:
        clock.now += agentic_gate.CACHE_TTL_SECONDS + 0.1

    # Never read in this process: refused - but transient, not "not licensed".
    assert await agentic_gate.agentic_verdict(object()) == AgenticVerdict(
        LICENSE_REQUIRED_CODE, transient=True
    )
    # The yes / no answer is the same fail-closed refusal as before.
    assert await agentic_gate.agentic_refusal(object()) == LICENSE_REQUIRED_CODE

    # Read: Community Edition, a definitive answer.
    state["fail"] = False
    assert await agentic_gate.agentic_verdict(object()) == AgenticVerdict(LICENSE_REQUIRED_CODE)

    # Read again after a blip: the last answer is kept, and still unconfirmed.
    _expire()
    state["fail"] = True
    assert await agentic_gate.agentic_verdict(object()) == AgenticVerdict(
        LICENSE_REQUIRED_CODE, transient=True
    )

    # Last read licensed: a blip keeps the platform running, nothing refused.
    # (Not across an edition change: see test_edition_gate_followups.)
    state.update(fail=False, licensed=True)
    _expire()
    assert await agentic_gate.agentic_verdict(object()) == AgenticVerdict(None)
    _expire()
    state["fail"] = True
    assert await agentic_gate.agentic_verdict(object()) == AgenticVerdict(None)


class _FailingSession(_Session):
    async def execute(self, *_a, **_k):
        _database_down()


@pytest.mark.asyncio
async def test_an_ai_switch_that_could_not_be_read_is_a_transient_refusal(monkeypatch):
    """``ai_state.is_ai_enabled`` answers ``False`` to a read error and caches
    nothing, so only a cached ``False`` - an administrator's choice, read
    successfully - is definitive. The real resolver, not a stub."""
    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", AsyncMock(return_value=True))

    assert await agentic_gate.agentic_verdict(_FailingSession()) == AgenticVerdict(
        AI_DISABLED_CODE, transient=True
    )
    assert await agentic_gate.agentic_refusal(_FailingSession()) == AI_DISABLED_CODE

    # The stored override says "disabled": read, cached, definitive - and a
    # later caller inside the TTL gets the same definitive answer.
    assert await agentic_gate.agentic_verdict(_Session([True])) == AgenticVerdict(
        AI_DISABLED_CODE
    )
    assert await agentic_gate.agentic_verdict(_FailingSession()) == AgenticVerdict(
        AI_DISABLED_CODE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("half", ["licence", "switch"])
@pytest.mark.parametrize("status", ["pending", "delayed", "awaiting_human"])
async def test_a_run_whose_gate_could_not_be_read_is_left_for_the_engine_to_retry(
    monkeypatch, half, status
):
    """A cold worker during a database failover must not fail a queued run as
    "not licensed" (or "AI disabled"): nothing is wrong with the run. It is
    left exactly as it was - pending, delayed, or paused on a human - and
    handed back to the orchestration engine, uncharged."""
    from app.services import assignment_engine

    _pin_unreadable(monkeypatch, half=half)
    run, assignment, agent = _run_stubs()
    run.status = status
    session = _Session([run, assignment, agent])
    monkeypatch.setattr(assignment_engine, "async_session", lambda: session)
    quota = AsyncMock()
    monkeypatch.setattr("app.services.quota.check_agentic_quota", quota)
    monkeypatch.setattr("app.services.quota.increment_agentic_usage", quota)
    executions = AsyncMock()
    monkeypatch.setattr(assignment_engine, "record_agentic_execution", executions)

    outcome = await assignment_engine.execute_assignment_run(str(run.id))

    assert run.status == status
    assert run.error is None and run.failure_cause is None and run.completed_at is None
    assert session.commits == 0
    quota.assert_not_awaited()
    executions.assert_not_awaited()
    assert outcome == assignment_engine.RUN_DEFERRED


@pytest.mark.asyncio
async def test_the_assignment_adapter_hands_a_deferred_run_back_to_the_engine(monkeypatch):
    """Retried with a bounded backoff through the engine's own retry path,
    before the run's status is read - a paused run must not be re-paused
    waiting for a resume that already arrived."""
    from app.services import assignment_engine
    from app.services.orchestration_adapters import assignment as assignment_adapter
    from app.services.orchestration_adapters.base import AdapterContext

    monkeypatch.setattr(
        assignment_engine,
        "execute_assignment_run",
        AsyncMock(return_value=assignment_engine.RUN_DEFERRED),
    )
    monkeypatch.setattr(assignment_adapter, "_bind_memory_chain", AsyncMock(return_value=None))

    result = await assignment_adapter.AssignmentAdapter().execute(
        AdapterContext(db=_Session(strict=True), worker_id="w1"),
        SimpleNamespace(payload={"run_id": str(uuid.uuid4())}),
    )

    assert result.kind == "failed"
    assert result.retryable is True
    assert result.backoff_seconds == agentic_gate.UNKNOWN_STATE_RETRY_SECONDS
    assert result.error == agentic_gate.UNKNOWN_STATE_DETAIL


@pytest.mark.asyncio
@pytest.mark.parametrize("half", ["licence", "switch"])
async def test_a_delegation_whose_gate_could_not_be_read_stays_pending(monkeypatch, half):
    from app.services import sub_agent

    _pin_unreadable(monkeypatch, half=half)
    task = _task_row()
    session = _TaskSession([task])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    collaborators = _patch_task_collaborators(monkeypatch)

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
        assignment_run_id=str(uuid.uuid4()),
    )

    assert task.status == "pending"
    assert task.error is None and task.completed_at is None and task.started_at is None
    assert session.commits == 0
    collaborators.run_agent_chat.assert_not_awaited()
    collaborators.update_run.assert_not_awaited()
    collaborators.delegation.assert_not_awaited()
    collaborators.publish.assert_not_awaited()
    collaborators.wake.assert_not_called()
    assert outcome.deferred and not outcome.succeeded


@pytest.mark.asyncio
async def test_the_sub_agent_adapter_hands_a_deferred_task_back_to_the_engine(monkeypatch):
    from app.services.orchestration_adapters import sub_agent as sub_agent_adapter
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.sub_agent import SubAgentTaskOutcome

    monkeypatch.setattr(
        "app.services.sub_agent._execute_sub_agent_task",
        AsyncMock(
            return_value=SubAgentTaskOutcome(
                status="deferred", error=agentic_gate.UNKNOWN_STATE_DETAIL
            )
        ),
    )

    result = await sub_agent_adapter.SubAgentAdapter().execute(
        AdapterContext(db=_Session([SimpleNamespace(status="pending")]), worker_id="w1"),
        SimpleNamespace(
            payload={
                "task_id": str(uuid.uuid4()),
                "target_agent_id": str(uuid.uuid4()),
                "user_id": str(uuid.uuid4()),
                "task_description": "Summarise the new exposures",
            }
        ),
    )

    assert result.kind == "failed"
    assert result.retryable is True
    assert result.backoff_seconds == agentic_gate.UNKNOWN_STATE_RETRY_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize("half", ["licence", "switch"])
@pytest.mark.usefixtures("evaluation_enabled")
async def test_a_fresh_evaluation_whose_gate_could_not_be_read_is_retried(monkeypatch, half):
    from app.services.orchestration_adapters import evaluation as evaluation_adapter
    from app.services.orchestration_adapters.base import AdapterContext

    _pin_unreadable(monkeypatch, half=half)
    failed_runs = AsyncMock()
    monkeypatch.setattr(evaluation_adapter, "_fail_active_runs", failed_runs)
    execute = AsyncMock()
    monkeypatch.setattr("app.services.evaluation_engine.execute_evaluation", execute)

    result = await evaluation_adapter.EvaluationAdapter().execute(
        AdapterContext(db=_Session(strict=True), worker_id="w1"),
        SimpleNamespace(payload={"evaluation_id": str(uuid.uuid4())}),
    )

    failed_runs.assert_not_awaited()
    execute.assert_not_awaited()
    assert result.kind == "failed"
    assert result.retryable is True
    assert result.backoff_seconds == agentic_gate.UNKNOWN_STATE_RETRY_SECONDS


@pytest.mark.asyncio
async def test_the_reconciler_hands_a_deferral_its_backoff(monkeypatch):
    """The backoff rides the engine's ordinary retry path (``engine.fail``,
    retryable): attempts and the deadline still bound it. A failure that names
    no backoff calls ``engine.fail`` exactly as before and keeps the engine's
    exponential one."""
    from app.services import orchestration_engine, orchestration_reconciler
    from app.services.orchestration_adapters.base import AdapterResult

    fail = AsyncMock(return_value=SimpleNamespace(state="pending"))
    monkeypatch.setattr(orchestration_engine, "fail", fail)

    await orchestration_reconciler._apply_result(
        _Session(),
        uuid.uuid4(),
        "w1",
        AdapterResult.failed("not confirmed", retryable=True, backoff_seconds=30.0),
    )
    assert fail.await_args.kwargs["retryable"] is True
    assert fail.await_args.kwargs["backoff_seconds"] == 30.0

    await orchestration_reconciler._apply_result(
        _Session(), uuid.uuid4(), "w1", AdapterResult.failed("boom")
    )
    assert "backoff_seconds" not in fail.await_args.kwargs


def test_a_deferral_is_bounded_by_the_engines_attempts_and_the_runs_grace():
    """Within the default attempts a deferral tolerates about a minute of
    database trouble - and a run that then starts keeps its deadline's grace."""
    from app.core.config import settings

    retries = settings.orchestration_default_max_attempts - 1
    assert 0 < retries * agentic_gate.UNKNOWN_STATE_RETRY_SECONDS <= (
        settings.orchestration_grace_seconds
    )


# ── The downgrade scenario, entry point by entry point ──────────────────────


def _assignment(enabled: bool = True):
    return SimpleNamespace(id=uuid.uuid4(), name="Triage", enabled=enabled)


@pytest.mark.asyncio
async def test_a_webhook_or_event_trigger_is_refused_on_community_edition(monkeypatch):
    """``trigger_assignment`` is where the scheduler, the webhooks, the
    integration events and the manual route all arrive. Refused before any run
    exists, so nothing is charged; in a shape its callers branch on."""
    from app.services import assignment_engine

    _pin(monkeypatch, **COMMUNITY)
    quota = AsyncMock()
    monkeypatch.setattr("app.services.quota.check_agentic_quota", quota)
    monkeypatch.setattr("app.services.quota.increment_agentic_usage", quota)

    with pytest.raises(HTTPException) as caught:
        await assignment_engine.trigger_assignment(_assignment(), SimpleNamespace(enabled=True))

    assert caught.value.status_code == 403
    assert caught.value.detail == {
        "code": LICENSE_REQUIRED_CODE,
        "message": deps.licence_required_detail(),
    }
    # The contract the provider push receivers rely on to keep answering 200.
    assert agentic_gate.refusal_code(caught.value) == LICENSE_REQUIRED_CODE
    quota.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_ai_switch_keeps_its_own_trigger_refusal(monkeypatch):
    from app.services import assignment_engine

    _pin(monkeypatch, **AI_SWITCHED_OFF)
    with pytest.raises(HTTPException) as caught:
        await assignment_engine.trigger_assignment(_assignment(), SimpleNamespace(enabled=True))
    assert caught.value.detail == {
        "code": AI_DISABLED_CODE,
        "message": "AI is disabled on this deployment.",
    }


@pytest.mark.asyncio
async def test_a_trigger_on_enterprise_edition_goes_past_the_gate(monkeypatch):
    from app.services import assignment_engine

    _pin(monkeypatch, **ENTERPRISE)
    # The next check after the gate: reaching it proves the gate let it through.
    with pytest.raises(ValueError, match="is disabled"):
        await assignment_engine.trigger_assignment(
            _assignment(enabled=False), SimpleNamespace(enabled=True)
        )


class _PollRedis:
    def __init__(self, values: dict | None = None):
        self.values: dict = dict(values or {})
        self.claimed: list = []
        self.released: list = []
        self.stamped: list = []

    async def get(self, key):
        return self.values.get(key)

    # The seen set: a sorted set bounded by the claim window, plus the legacy set.
    async def sismember(self, _key, _member):
        return False

    async def zremrangebyscore(self, *_a):
        return 0

    async def zadd(self, _key, mapping, nx=False):
        (member,) = mapping
        if member in self.claimed:
            return 0
        self.claimed.append(member)
        return 1

    async def zrem(self, _key, member):
        self.released.append(member)
        return 1

    async def expire(self, *_a, **_k):
        return True

    async def set(self, key, value, **_k):
        self.values[key] = value
        if key.startswith("event_poll:"):
            self.stamped.append(key)

    async def delete(self, key):
        self.values.pop(key, None)


def _poller_rows():
    agent = SimpleNamespace(id=uuid.uuid4(), enabled=True, user_id=None, integration_bindings={})
    wf = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        trigger_config={"provider": "microsoft", "event": "new_email", "integration_id": "i-1"},
    )
    integration = SimpleNamespace(id=uuid.uuid4(), access_token_enc="sealed")
    return wf, agent, integration


def _patch_poller(monkeypatch, *, trigger):
    from app.services import event_poller

    redis = _PollRedis()
    monkeypatch.setattr(event_poller, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(event_poller, "decrypt_value", lambda _v: "token")

    async def _provider(_provider, _event, _token, wf, redis_, *_a, **_kw):
        # What every provider poller does before returning: advance the cursor.
        await redis_.set(f"event_cursor:{wf.id}", "after-this-poll")
        return [{"message_id": "m-1"}, {"message_id": "m-2"}]

    poll = AsyncMock(side_effect=_provider)
    monkeypatch.setattr(event_poller, "_poll_provider", poll)

    # A free slot for every event: *trigger* stands for the run it starts,
    # called the way the engine calls ``trigger_assignment`` then.
    async def _trigger_or_queue(assignment, agent, payload=None, *, db, trigger_context=None, **_kwargs):
        from app.services.assignment_engine import QueuedTriggerResult

        run_id = await trigger(
            assignment=assignment, agent=agent, payload=payload, db=db, trigger_context=trigger_context
        )
        return QueuedTriggerResult(status="ok", run_id=run_id)

    monkeypatch.setattr(event_poller, "trigger_or_queue_assignment", _trigger_or_queue)
    # Room in the queue: the rows above are answered in query order.
    monkeypatch.setattr(event_poller, "full_queue_assignment_ids", AsyncMock(return_value=set()))
    return redis, poll


@pytest.mark.asyncio
async def test_integration_events_are_not_polled_or_claimed_on_community_edition(monkeypatch):
    """The events stay at the provider, unread and unclaimed, so they are
    polled once AI is back instead of being lost behind a seen marker."""
    from app.services import event_poller

    _pin(monkeypatch, **COMMUNITY)
    wf, agent, _integration = _poller_rows()
    trigger = AsyncMock()
    redis, poll = _patch_poller(monkeypatch, trigger=trigger)

    triggered = await event_poller.poll_integration_events(_Session([[wf], [agent]]))

    assert triggered == 0
    poll.assert_not_awaited()
    trigger.assert_not_awaited()
    assert redis.claimed == [] and redis.stamped == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor_before", ["2026-09-26T10:00:00Z", None])
async def test_an_event_refused_mid_tick_is_given_back(monkeypatch, cursor_before):
    """The edition changed between the per-assignment check and the trigger:
    the claimed event is released, nothing more is claimed, the provider
    cursor the poll advanced is put back and the assignment's poll is not
    stamped - so everything the poll returned is fetched again once AI is
    back, the events already dispatched deduplicated by the seen set."""
    from app.services import event_poller

    _pin(monkeypatch, **ENTERPRISE)
    wf, agent, integration = _poller_rows()
    refusal = HTTPException(
        403, detail={"code": LICENSE_REQUIRED_CODE, "message": "Enterprise Edition required."}
    )
    redis, _poll = _patch_poller(monkeypatch, trigger=AsyncMock(side_effect=refusal))
    cursor_key = f"event_cursor:{wf.id}"
    if cursor_before is not None:
        redis.values[cursor_key] = cursor_before

    triggered = await event_poller.poll_integration_events(
        _Session([[wf], [agent], integration])
    )

    assert triggered == 0
    assert redis.claimed == ["m-1"] and redis.released == ["m-1"]
    assert redis.values.get(cursor_key) == cursor_before
    assert redis.stamped == []


@pytest.mark.asyncio
async def test_integration_events_dispatch_on_enterprise_edition(monkeypatch):
    from app.services import event_poller

    _pin(monkeypatch, **ENTERPRISE)
    wf, agent, integration = _poller_rows()
    redis, poll = _patch_poller(monkeypatch, trigger=AsyncMock(return_value="run-1"))

    triggered = await event_poller.poll_integration_events(
        _Session([[wf], [agent], integration])
    )

    assert triggered == 2
    poll.assert_awaited_once()
    assert redis.released == [] and redis.stamped == [f"event_poll:{wf.id}"]


def test_every_provider_push_receiver_skips_a_refused_delivery_and_answers_200():
    """Microsoft Graph and Slack remove a subscription after repeated
    failures, so a push receiver must swallow the licence refusal exactly as it
    swallows the AI switch's - never let it become a 403."""
    from app.api.v1 import integration_notifications
    from app.services import slack_events

    # The Slack dispatch lives in ``services/slack_events.py``.
    handlers = [
        node
        for module in (integration_notifications, slack_events)
        for node in ast.walk(ast.parse(inspect.getsource(module)))
        if isinstance(node, ast.ExceptHandler)
        and ast.unparse(node.type or ast.Constant(None)) == "HTTPException"
    ]
    assert len(handlers) == 3, "Graph, Slack and LinkedIn deliveries"
    for handler in handlers:
        body = ast.unparse(handler)
        assert "refusal_code(exc)" in body
        assert "continue" in body


def _inbound():
    from app.channels.base import ChannelType, InboundMessage

    return InboundMessage(
        channel=ChannelType.TELEGRAM,
        text="What changed on the perimeter overnight?",
        user_email=None,
        channel_user_id="12345",
        channel_thread_id="67890",
        metadata={"chat_type": "private"},
    )


def _slack_channel_message():
    """A message in a Slack channel thread, the bot not mentioned."""
    from app.channels.base import ChannelType, InboundMessage

    return InboundMessage(
        channel=ChannelType.SLACK,
        text="Did anyone look at the VPN alerts?",
        user_email=None,
        channel_user_id="U12345",
        channel_thread_id="C0123:1700000000.000100",
        metadata={"channel_type": "channel"},
    )


class _PastTheGateError(Exception):
    pass


@pytest.mark.asyncio
async def test_the_ai_switch_answers_a_channel_message_before_anything_else(monkeypatch):
    """The AI switch's reply is unchanged: every channel message is answered,
    before the bot's own rules are read and before anything is stored."""
    from app.channels import router

    _pin(monkeypatch, **AI_SWITCHED_OFF)
    monkeypatch.setattr(router, "async_session", lambda: _Session(strict=True))
    monkeypatch.setattr(router, "_get_channel_bot", AsyncMock(side_effect=_PastTheGateError))

    out = await router.handle_message(_slack_channel_message())

    assert out.text == (
        "AI is disabled on this platform, so I cannot answer right now. "
        "Please contact an administrator."
    )
    assert not getattr(out, "suppress", False)


def _patch_bot(monkeypatch, *, only_mention: bool = False, allows: bool = True) -> None:
    """The bot's listening rules; resolving the user is past the gate."""
    from app.channels import router

    monkeypatch.setattr(router, "async_session", lambda: _Session(strict=True))
    monkeypatch.setattr(
        router, "_get_channel_bot", AsyncMock(return_value=SimpleNamespace(only_mention=only_mention))
    )
    monkeypatch.setattr(router, "_bot_allows_receive", lambda _bot, _inbound: allows)
    monkeypatch.setattr(router, "_any_agent_bypasses_whitelist", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "_is_thread_active", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "_is_thread_muted", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "resolve_user_by_email", AsyncMock(side_effect=_PastTheGateError))
    monkeypatch.setattr(
        router, "resolve_user_by_channel_identity", AsyncMock(side_effect=_PastTheGateError)
    )


@pytest.mark.asyncio
async def test_a_channel_message_the_bot_listens_to_is_answered_with_the_licence_refusal(
    monkeypatch,
):
    """Slack, Teams, Telegram, Discord, WhatsApp and Google Chat all arrive at
    ``handle_message``. Answered, not dropped - a person is waiting on the bot -
    with the chat gate's own sentence, where a model would have answered: the
    routing rules run first (``test_edition_gate_followups``), and nothing is
    stored. A private chat is always listened to."""
    from app.channels import router

    _pin(monkeypatch, **COMMUNITY)
    _patch_bot(monkeypatch, only_mention=True)
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Support",
        enabled=True,
        channel_config={"telegram": {"receive_enabled": True}},
        available_channel_types=None,
    )
    user = SimpleNamespace(id=uuid.uuid4(), channel_agent_config=None)
    session = _Session([agent])
    monkeypatch.setattr(router, "async_session", lambda: session)
    monkeypatch.setattr(router, "resolve_user_by_channel_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(router, "link_channel_identity", AsyncMock())
    monkeypatch.setattr(
        router, "_find_existing_external_conversation", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(router, "_resolve_channel_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(router, "_resolve_channel_files", AsyncMock(side_effect=_PastTheGateError))

    out = await router.handle_message(_inbound())

    assert out.text == deps.chat_unavailable_detail()
    assert deps.licence_required_detail() in out.text
    assert not getattr(out, "suppress", False)
    assert session.commits == 0 and session.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", ["only_mention", "receive_whitelist", "muted_thread"])
async def test_the_licence_refusal_is_not_posted_where_the_bot_does_not_listen(
    monkeypatch, rule
):
    """A deployment that dropped to Community Edition must not answer every
    group message with the licence sentence: where the bot stays silent while
    it is licensed, it stays silent."""
    from app.channels import router

    _pin(monkeypatch, **COMMUNITY)
    _patch_bot(
        monkeypatch,
        only_mention=rule == "only_mention",
        allows=rule != "receive_whitelist",
    )
    if rule == "muted_thread":
        monkeypatch.setattr(router, "_is_thread_muted", AsyncMock(return_value=True))

    out = await router.handle_message(_slack_channel_message())

    assert out.suppress is True
    assert out.text == ""


@pytest.mark.asyncio
async def test_a_channel_message_on_enterprise_edition_goes_past_the_gate(monkeypatch):
    from app.channels import router

    _pin(monkeypatch, **ENTERPRISE)
    monkeypatch.setattr(router, "async_session", lambda: _Session())
    monkeypatch.setattr(router, "_get_channel_bot", AsyncMock(side_effect=_PastTheGateError))
    with pytest.raises(_PastTheGateError):
        await router.handle_message(_inbound())


def _run_stubs():
    run = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        assignment_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        status="pending",
        error=None,
        started_at=None,
        completed_at=None,
        failure_cause=None,
        failure_determination=None,
        is_company_managed=False,
        trigger_context={},
    )
    assignment = SimpleNamespace(
        enabled=True, name="Weekly briefing", user_id=None, is_company_managed=False
    )
    # A resumed run reads the agent's tool set to word its resume note (#2677).
    agent = SimpleNamespace(
        enabled=True,
        user_id=None,
        is_builtin=False,
        id=uuid.uuid4(),
        builtin_tool_ids=[],
        builtin_allowed_tools=None,
        tool_deny_list=[],
    )
    return run, assignment, agent


class _QuotaReached(BaseException):
    """Stops the engine where the quota would be charged."""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("edition", "cause", "error"),
    [
        (COMMUNITY, FailureCause.LICENSE_REQUIRED, "licence"),
        (AI_SWITCHED_OFF, FailureCause.AI_DISABLED, "AI is disabled on this deployment."),
    ],
)
async def test_a_queued_run_is_refused_at_its_start_and_never_charged(
    monkeypatch, edition, cause, error
):
    """A run queued or delayed before the downgrade has not started: refused
    before the quota, recorded with its cause through the classifier's
    vocabulary, and nothing else is touched - the assignment stays enabled."""
    from app.services import assignment_engine

    _pin(monkeypatch, **edition)
    run, assignment, agent = _run_stubs()
    session = _Session([run, assignment, agent])
    monkeypatch.setattr(assignment_engine, "async_session", lambda: session)
    quota = AsyncMock()
    monkeypatch.setattr("app.services.quota.check_agentic_quota", quota)
    monkeypatch.setattr("app.services.quota.increment_agentic_usage", quota)
    executions = AsyncMock()
    monkeypatch.setattr(assignment_engine, "record_agentic_execution", executions)
    publish = AsyncMock()
    monkeypatch.setattr(assignment_engine, "_publish_run_status", publish)

    await assignment_engine.execute_assignment_run(str(run.id))

    assert run.status == "failed"
    assert run.error == (deps.licence_required_detail() if error == "licence" else error)
    assert run.failure_cause == cause.value
    assert run.failure_determination == FailureDetermination.STRUCTURAL.value
    assert run.completed_at is not None
    assert session.commits == 1
    quota.assert_not_awaited()
    executions.assert_not_awaited()
    publish.assert_not_awaited()
    assert assignment.enabled is True
    assert "quota_metered" not in run.trigger_context


@pytest.mark.asyncio
async def test_a_queued_run_on_enterprise_edition_reaches_the_quota(monkeypatch):
    from app.services import assignment_engine

    _pin(monkeypatch, **ENTERPRISE)
    run, assignment, agent = _run_stubs()
    monkeypatch.setattr(
        assignment_engine, "async_session", lambda: _Session([run, assignment, agent])
    )
    monkeypatch.setattr(
        "app.services.quota.check_agentic_quota", AsyncMock(side_effect=_QuotaReached)
    )
    with pytest.raises(_QuotaReached):
        await assignment_engine.execute_assignment_run(str(run.id))
    assert run.status == "pending"


@pytest.mark.asyncio
async def test_queued_deliveries_stay_queued_while_ai_is_unavailable(monkeypatch):
    """Promoting a delivery could only fail it at run start: left queued, order
    and bounds intact, and drained once AI is available again. The gate is
    asked only when there is something to promote, so an empty queue - the
    common case, every reconciler cycle - costs exactly what it did before."""
    from app.services import assignment_dispatch, assignment_engine

    monkeypatch.setattr(assignment_engine.settings, "assignment_queue_enabled", True)
    drain_one = AsyncMock(return_value=1)
    monkeypatch.setattr(assignment_engine, "_drain_one_assignment", drain_one)
    load_states = assignment_engine._load_queue_states
    # One assignment with a queued delivery and a free slot.
    state = assignment_dispatch.QueueState(
        assignment_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        depth=1,
        oldest_at=datetime.now(UTC),
        active=0,
        cap=1,
    )
    monkeypatch.setattr(assignment_engine, "_load_queue_states", AsyncMock(return_value=[state]))
    monkeypatch.setattr(assignment_engine, "_unclaimed_assignment_executions", AsyncMock(return_value=0))

    _pin(monkeypatch, **COMMUNITY)
    monkeypatch.setattr(assignment_engine, "async_session", lambda: _Session())
    assert await assignment_engine.drain_assignment_queues() == 0
    drain_one.assert_not_awaited()

    _pin(monkeypatch, **ENTERPRISE)
    assert await assignment_engine.drain_assignment_queues() == 1
    drain_one.assert_awaited_once()

    monkeypatch.setattr(assignment_engine, "_load_queue_states", load_states)
    gate = AsyncMock(return_value=None)
    monkeypatch.setattr(agentic_gate, "agentic_refusal", gate)
    session = _Session([[]])
    monkeypatch.setattr(assignment_engine, "async_session", lambda: session)
    assert await assignment_engine.drain_assignment_queues() == 0
    # The CTEM backlog count of trim_platform_event_queues (#2570), then the queue: one read each.
    assert session.executes == 2, "the backlog count and the queue are read once each"
    gate.assert_not_awaited()


class _TaskSession(_Session):
    pass


def _task_row():
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="pending",
        error=None,
        started_at=None,
        completed_at=None,
        conversation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        metadata_json={},
    )


def _patch_task_collaborators(monkeypatch):
    from app.services import sub_agent

    collaborators = SimpleNamespace(
        run_agent_chat=AsyncMock(return_value=("done", None)),
        update_run=AsyncMock(),
        delegation=AsyncMock(),
        publish=AsyncMock(),
        wake=MagicMock(),
    )
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", collaborators.run_agent_chat)
    monkeypatch.setattr(sub_agent, "update_assignment_run", collaborators.update_run)
    monkeypatch.setattr(sub_agent, "_update_task_delegation", collaborators.delegation)
    monkeypatch.setattr(sub_agent, "publish_task_event", collaborators.publish)
    monkeypatch.setattr(sub_agent, "_schedule_parent_wake_if_needed", collaborators.wake)
    return collaborators


@pytest.mark.asyncio
async def test_a_queued_delegation_is_refused_before_its_agent_turn(monkeypatch):
    """The agent turn is what charges the quota, so a refused task never
    reaches it. Recorded like any failure before the loop: the task, its linked
    run (with the cause) and the delegation row all say why."""
    from app.services import sub_agent

    _pin(monkeypatch, **COMMUNITY)
    task = _task_row()
    # The task, then the target agent's name for the parent's wake.
    session = _TaskSession([task, "Exposure analyst"])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    collaborators = _patch_task_collaborators(monkeypatch)
    run_id = str(uuid.uuid4())

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
        assignment_run_id=run_id,
    )

    message = deps.licence_required_detail()
    assert not outcome.succeeded
    assert outcome.error == message
    assert task.status == "failed"
    assert task.error == message
    assert task.started_at is None, "refused before it started"
    collaborators.run_agent_chat.assert_not_awaited()
    collaborators.update_run.assert_awaited_once()
    assert collaborators.update_run.await_args.args[1:] == (run_id, "failed")
    assert collaborators.update_run.await_args.kwargs == {
        "error": message,
        "failure_cause": FailureCause.LICENSE_REQUIRED,
    }
    collaborators.delegation.assert_awaited_once()
    assert collaborators.delegation.await_args.args[2] == "failed"
    collaborators.wake.assert_called_once()
    # The parent reads who could not run, not the "Agent" placeholder.
    assert collaborators.wake.call_args.kwargs["agent_name"] == "Exposure analyst"


@pytest.mark.asyncio
async def test_a_delegation_on_enterprise_edition_runs_its_agent_turn(monkeypatch):
    from app.services import sub_agent

    _pin(monkeypatch, **ENTERPRISE)
    task = _task_row()
    # The task, then the agent lookup; the loop is stubbed.
    session = _TaskSession([task, None])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    _patch_task_collaborators(monkeypatch)
    monkeypatch.setattr(
        "app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[])
    )

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
    )
    # Past the gate: marked running, then stopped by the (absent) agent.
    assert task.started_at is not None
    assert "not found or not accessible" in (outcome.error or "")


@pytest.mark.asyncio
async def test_a_paused_run_answered_while_ai_is_unavailable_is_refused_at_its_resume(
    monkeypatch,
):
    """The respond route stays ungated (an existing run is work in
    flight), so the answer is accepted and the resumed run meets the run-start
    gate. The engine holds it (``test_ai_unavailable_hold``); once its
    hold is over - or with no engine to hold it - it is refused like a queued
    run, with its cause, uncharged."""
    from app.api.v1 import assignments
    from app.services import assignment_engine

    assert "require_agentic_ai" not in inspect.getsource(assignments.respond_to_human_input)

    _pin(monkeypatch, **COMMUNITY)
    run, assignment, agent = _run_stubs()
    run.status = "awaiting_human"
    monkeypatch.setattr(
        assignment_engine, "async_session", lambda: _Session([run, assignment, agent])
    )
    quota = AsyncMock()
    monkeypatch.setattr("app.services.quota.check_agentic_quota", quota)

    assert await assignment_engine.execute_assignment_run(str(run.id)) is None
    assert run.status == "failed"
    assert run.failure_cause == FailureCause.LICENSE_REQUIRED.value
    quota.assert_not_awaited()


# ── The stale-reference auto-pause never counts a refusal ───────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "status", "cause"),
    [
        ("community", "failed", FailureCause.LICENSE_REQUIRED.value),
        ("ai_switched_off", "failed", FailureCause.AI_DISABLED.value),
        ("unreadable", "pending", None),
    ],
)
async def test_a_refused_or_deferred_run_never_reaches_the_stale_reference_tracking(
    monkeypatch, scenario, status, cause
):
    """The platform pauses an assignment after consecutive scheduled runs failed on a
    stale integration reference, and tells its owner. A scheduled run refused
    at its start never ran the agent: it keeps its own cause, and the tracking
    behind the notice, the pause and its audit is never reached. A deferred
    run is left exactly as it was."""
    from app.services import assignment_engine

    if scenario == "unreadable":
        _pin_unreadable(monkeypatch, half="licence")
    else:
        _pin(monkeypatch, **(COMMUNITY if scenario == "community" else AI_SWITCHED_OFF))
    run, assignment, agent = _run_stubs()
    run.trigger_context = {"type": "schedule"}
    monkeypatch.setattr(
        assignment_engine, "async_session", lambda: _Session([run, assignment, agent])
    )
    track = AsyncMock()
    monkeypatch.setattr(assignment_engine, "_track_stale_reference_failure", track)

    await assignment_engine.execute_assignment_run(str(run.id))

    assert run.status == status
    assert run.failure_cause == cause
    track.assert_not_awaited()
    assert assignment.enabled is True


@pytest.mark.parametrize("refusal", [FailureCause.LICENSE_REQUIRED, FailureCause.AI_DISABLED])
def test_a_refused_scheduled_run_never_advances_the_stale_reference_streak(refusal):
    """A refusal is a scheduled run that failed for another reason: never
    counted as a stale-reference failure, so refusals alone can never pause
    an assignment, and one between stale failures ends the count - the rule
    the auto-pause documents for every other cause - rather than extending it."""
    from app.services.assignment_engine import _stale_reference_streak, _StreakRun

    def _failed(cause, trigger="schedule"):
        return _StreakRun(
            id=uuid.uuid4(),
            status="failed",
            failure_cause=cause.value,
            trigger_type=trigger,
            gate_skipped=False,
            created_at=None,
        )

    stale = FailureCause.STALE_INTEGRATION_REFERENCE
    refusals = [_failed(refusal), _failed(refusal, "schedule_fanout"), _failed(refusal)]
    assert _stale_reference_streak(refusals, since=None) == 0
    interrupted = [_failed(stale), _failed(refusal), _failed(stale), _failed(stale)]
    assert _stale_reference_streak(interrupted, since=None) == 1


@pytest.mark.asyncio
async def test_the_stale_reference_streak_never_reads_a_deferred_run():
    """A deferral leaves the run pending, delayed or paused on a human; the
    scan behind the streak and the episode notice reads terminal runs only."""
    from sqlalchemy.dialects import postgresql

    from app.services.assignment_engine import _load_streak_runs

    captured: list = []

    class _Db:
        async def execute(self, stmt):
            captured.append(stmt)
            return _Result([])

    await _load_streak_runs(_Db(), uuid.uuid4(), limit=5)
    (stmt,) = captured
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "assignment_runs.status IN ('completed', 'failed')" in sql.split("WHERE", 1)[1]


@pytest.mark.asyncio
async def test_update_assignment_run_stamps_a_known_cause():
    from app.services import sub_agent

    run = SimpleNamespace(
        status="running",
        completed_at=None,
        error=None,
        result_summary=None,
        failure_cause=None,
        failure_determination=None,
    )
    await sub_agent.update_assignment_run(
        _Session([run]),
        str(uuid.uuid4()),
        "failed",
        error="refused",
        failure_cause=FailureCause.LICENSE_REQUIRED,
    )
    assert run.failure_cause == "license_required"
    assert run.failure_determination == FailureDetermination.STRUCTURAL.value


def _wake_rows():
    task = SimpleNamespace(
        id=uuid.uuid4(),
        status="completed",
        conversation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        metadata_json={},
        result="Three new exposures, all on the VPN edge.",
        error=None,
    )
    conversation = SimpleNamespace(
        id=task.conversation_id,
        user_id=task.user_id,
        agent_id=uuid.uuid4(),
        channel="web",
        channel_thread_id=None,
        tool_overrides_json=None,
    )
    return task, conversation


def _patch_wake(monkeypatch):
    from app.services import parent_wake

    wake = SimpleNamespace(
        store=AsyncMock(),
        acquire=AsyncMock(return_value=(None, True)),
        publish=AsyncMock(),
    )
    monkeypatch.setattr("app.channels.router.store_message", wake.store)
    monkeypatch.setattr("app.services.chat_turn_guard.acquire_or_fold", wake.acquire)
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", wake.publish)
    monkeypatch.setattr(parent_wake, "load_result_file_refs", AsyncMock(return_value=[]))
    return wake


@pytest.mark.asyncio
async def test_a_wake_starts_no_turn_on_community_edition_and_loses_nothing(monkeypatch):
    """A wake is a new agent turn nobody asked for, so it does not start. Its
    text is persisted first - hidden, where a wake requeued behind a busy
    conversation lives too - so the next turn, once AI is back, reads the
    specialist's result."""
    from app.services import parent_wake

    _pin(monkeypatch, **COMMUNITY)
    task, conversation = _wake_rows()
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _Session([task, conversation])
    )
    wake = _patch_wake(monkeypatch)

    await parent_wake._wake_parent_conversation(
        task_id=str(task.id),
        conversation_id=str(conversation.id),
        user_id=str(task.user_id),
        agent_name="Exposure analyst",
        terminal_status="completed",
    )

    assert wake.store.await_count == 2
    stored = wake.store.await_args_list[0].kwargs
    assert stored["role"] == "user"
    assert task.result in stored["content"]
    assert '"hidden": true' in stored["metadata_json"]
    wake.acquire.assert_not_awaited()
    # No turn, so the parent does not wake: the task is delivered as a task
    # whose parent does not wake is - the completion notice, which carries
    # its files (test_edition_gate_followups).
    notice = wake.store.await_args_list[1].kwargs
    assert notice["role"] == "assistant"
    assert "Background task completed" in notice["content"]


@pytest.mark.asyncio
async def test_a_partner_wake_on_community_edition_keeps_its_payload(monkeypatch):
    from app.services import parent_wake

    _pin(monkeypatch, **COMMUNITY)
    _task, conversation = _wake_rows()
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session([conversation]))
    wake = _patch_wake(monkeypatch)

    await parent_wake._wake_conversation_with(
        conversation_id=str(conversation.id),
        user_id=str(conversation.user_id),
        wake_text="[Platform] The partner answered.",
    )

    assert wake.store.await_args.kwargs["content"] == "[Platform] The partner answered."
    wake.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_wake_on_enterprise_edition_acquires_its_turn(monkeypatch):
    from app.services import parent_wake

    _pin(monkeypatch, **ENTERPRISE)
    task, conversation = _wake_rows()
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _Session([task, conversation])
    )
    wake = _patch_wake(monkeypatch)

    await parent_wake._wake_parent_conversation(
        task_id=str(task.id),
        conversation_id=str(conversation.id),
        user_id=str(task.user_id),
        agent_name="Exposure analyst",
        terminal_status="completed",
    )

    wake.store.assert_awaited_once()
    # Folded into the in-flight turn (the stubbed answer): past the gate.
    wake.acquire.assert_awaited_once()


def _flow(*kinds: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Nightly triage",
        version_number=3,
        graph={"nodes": [{"id": f"n{i}", "kind": kind, "config": {}} for i, kind in enumerate(kinds)]},
        user_id=uuid.uuid4(),
        is_company_managed=False,
        shared_group_ids=None,
        shared_user_ids=None,
        access_restricted=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("edition", [COMMUNITY, AI_SWITCHED_OFF])
async def test_an_ai_flow_trigger_is_held_back_with_the_reason(monkeypatch, edition):
    from app.services import flow_triggers

    _pin(monkeypatch, **edition)
    own = _Session()
    monkeypatch.setattr(flow_triggers, "async_session", lambda: own)

    held = await flow_triggers.hold_back_if_ai_required(
        _Session([None, None]), _flow("trigger.schedule", "agent"), trigger_type="schedule"
    )

    assert held is True
    (run,) = own.added
    assert run.status == "skipped"
    if edition is COMMUNITY:
        assert run.result_summary.endswith(deps.licence_required_detail())
        assert "No step was executed" in run.result_summary
    else:
        # The AI switch's reason, unchanged.
        assert run.result_summary == flow_triggers._AI_HELD_BACK_REASON


@pytest.mark.asyncio
async def test_a_deterministic_flow_still_runs_on_community_edition(monkeypatch):
    """FR-014, unchanged by the edition: no AI step, nothing to hold back."""
    from app.services import flow_triggers

    _pin(monkeypatch, **COMMUNITY)
    held = await flow_triggers.hold_back_if_ai_required(
        None, _flow("trigger.schedule", "action.http_request"), trigger_type="schedule"
    )
    assert held is False


@pytest.mark.asyncio
async def test_a_flow_on_enterprise_edition_is_not_held_back(monkeypatch):
    from app.services import flow_triggers

    _pin(monkeypatch, **ENTERPRISE)
    held = await flow_triggers.hold_back_if_ai_required(
        None, _flow("trigger.schedule", "agent"), trigger_type="schedule"
    )
    assert held is False


@pytest.mark.asyncio
async def test_the_hold_back_record_is_rewritten_when_the_reason_changes(monkeypatch):
    """One record per held-back stretch - but a stretch recorded as "AI is
    disabled" that goes on because the licence went says something else now."""
    from app.services import flow_triggers

    _pin(monkeypatch, **COMMUNITY)
    reason = flow_triggers._held_back_reason(LICENSE_REQUIRED_CODE)
    flow = _flow("trigger.schedule", "agent")

    same = _Session()
    monkeypatch.setattr(flow_triggers, "async_session", lambda: same)
    latest = SimpleNamespace(status="skipped", result_summary=reason)
    assert await flow_triggers.hold_back_if_ai_required(
        _Session([latest, None]), flow, trigger_type="schedule"
    )
    assert same.added == [], "already recorded for this reason"

    other = _Session()
    monkeypatch.setattr(flow_triggers, "async_session", lambda: other)
    latest = SimpleNamespace(status="skipped", result_summary=flow_triggers._AI_HELD_BACK_REASON)
    assert await flow_triggers.hold_back_if_ai_required(
        _Session([latest, None]), flow, trigger_type="schedule"
    )
    assert [r.result_summary for r in other.added] == [reason]


@pytest.mark.asyncio
async def test_the_per_user_fan_out_is_refused_and_leaves_its_window_open(monkeypatch):
    from datetime import UTC, datetime

    from app.services import autonomous

    _pin(monkeypatch, **COMMUNITY)
    trigger = AsyncMock()
    monkeypatch.setattr("app.services.assignment_engine.trigger_assignment", trigger)

    fanned = await autonomous._fanout_assignment_to_users(
        _Session(strict=True), _assignment(), SimpleNamespace(), datetime.now(UTC), "0 8 * * 1"
    )

    assert fanned is None
    trigger.assert_not_awaited()
    # ``None`` is "not now", never "done for this window": the caller leaves
    # ``last_triggered_at`` unstamped, as the tick does for every schedule
    # while AI is unavailable.
    source = inspect.getsource(autonomous._process_scheduled_assignments)
    branch = source[source.index("_fanout_assignment_to_users(") :]
    assert branch.index("if fanned is None:") < branch.index("assignment.last_triggered_at = now")


@pytest.mark.asyncio
async def test_a_fan_out_refused_midway_stops_and_leaves_its_window_open(monkeypatch):
    """The edition changed while the fan-out ran: the users left are not
    tried one by one, and the window stays open - the users already served
    are deduplicated when it reruns."""
    from datetime import UTC, datetime

    from app.services import autonomous

    _pin(monkeypatch, **ENTERPRISE)
    refusal = HTTPException(
        403, detail={"code": LICENSE_REQUIRED_CODE, "message": "Enterprise Edition required."}
    )
    trigger = AsyncMock(side_effect=["run-1", refusal, "run-3"])
    monkeypatch.setattr("app.services.assignment_engine.trigger_assignment", trigger)
    users = [(uuid.uuid4(), None) for _ in range(3)]

    fanned = await autonomous._fanout_assignment_to_users(
        _Session([users, []]), _assignment(), SimpleNamespace(), datetime.now(UTC), "0 8 * * 1"
    )

    assert fanned is None
    assert trigger.await_count == 2


@pytest.mark.asyncio
async def test_a_fan_out_skips_a_user_whose_group_has_ai_off_and_serves_the_rest(monkeypatch):
    """A refusal for ONE person's group concerns that user only: the fan-out
    goes on, where a platform refusal stops it."""
    from datetime import UTC, datetime

    from app.services import autonomous

    _pin(monkeypatch, **ENTERPRISE)
    personal = HTTPException(
        403, detail={"code": AI_DISABLED_CODE, "message": "off for your group", "scope": "group"}
    )
    trigger = AsyncMock(side_effect=["run-1", personal, "run-3"])
    monkeypatch.setattr("app.services.assignment_engine.trigger_assignment", trigger)
    users = [(uuid.uuid4(), None) for _ in range(3)]

    fanned = await autonomous._fanout_assignment_to_users(
        _Session([users, []]), _assignment(), SimpleNamespace(), datetime.now(UTC), "0 8 * * 1"
    )

    assert fanned == 2
    assert trigger.await_count == 3


def test_the_scheduled_sweep_goes_past_a_personal_refusal_and_stops_on_a_platform_one():
    """The sweep's ``break`` assumes everything left would be refused too,
    which holds for the platform switch and the licence only."""
    from app.services import autonomous

    source = inspect.getsource(autonomous._process_scheduled_assignments)
    handler = source[source.index("refusal = refusal_code(exc)") :]
    assert handler.index("if refusal_is_personal(exc):") < handler.index("continue") < handler.index("break")


def test_the_event_poller_skips_an_owner_whose_group_has_ai_off_without_polling():
    from app.services import event_poller

    source = inspect.getsource(event_poller.poll_integration_events)
    loop = source[source.index("for wf in assignments:") :]
    assert loop.index("group_verdict(db, owner)") < loop.index("try:")


@pytest.mark.asyncio
async def test_a_queued_worker_job_is_skipped_and_stays_pending(monkeypatch):
    from app import worker

    _pin(monkeypatch, **COMMUNITY)
    assert await worker._skip_because_ai_is_disabled("document processing", "doc-1") is True
    _pin(monkeypatch, **AI_SWITCHED_OFF)
    assert await worker._skip_because_ai_is_disabled("document processing", "doc-1") is True
    _pin(monkeypatch, **ENTERPRISE)
    assert await worker._skip_because_ai_is_disabled("document processing", "doc-1") is False


_WORKSET_JOBS = [
    ("workset_auto_pass_job", {"document_id": "doc-1"}),
    ("workset_cluster_job", {"workset_id": "ws-1"}),
]


def _patch_workset_work(monkeypatch):
    from app import worker

    enqueue = AsyncMock()
    monkeypatch.setattr(worker, "queue", SimpleNamespace(enqueue=enqueue))
    work = AsyncMock()
    monkeypatch.setattr("app.services.workset_processing.run_auto_pass", work)
    monkeypatch.setattr("app.services.workset_clustering.cluster_workset", work)
    return enqueue, work


@pytest.mark.asyncio
@pytest.mark.parametrize(("job", "kwargs"), _WORKSET_JOBS)
@pytest.mark.parametrize("state", [COMMUNITY, AI_SWITCHED_OFF], ids=["community", "ai_off"])
async def test_a_refused_one_shot_workset_job_asks_again_later(monkeypatch, job, kwargs, state):
    """Nothing but this job comes back for its subject (no sweep recovers a
    workset item): returning would strand it, so a refused job schedules
    itself again and runs once AI is available."""
    import time

    from app import worker

    _pin(monkeypatch, **state)
    enqueue, work = _patch_workset_work(monkeypatch)

    await getattr(worker, job)({}, **kwargs)

    work.assert_not_awaited()
    (queued,), _ = enqueue.await_args
    assert queued.function == job and queued.kwargs == kwargs
    assert queued.scheduled >= int(time.time()) + worker._AI_UNAVAILABLE_RETRY_SECONDS - 5
    # The job's own timeout, not SAQ's 10 s default: the rescheduled job does
    # the same work as the original.
    assert queued.timeout == worker.app_settings.worker_job_timeout


@pytest.mark.asyncio
async def test_clustering_is_queued_with_the_worker_job_timeout(monkeypatch):
    """SAQ's own default is 10 s, and a clustering pass over a large corpus
    (up to `_MAX_VECTORS` vectors, compared pairwise) outlasts it: a timed-out
    pass leaves the corpus unclustered for good."""
    from app import worker
    from app.services.workset_clustering import enqueue_clustering

    enqueue, _work = _patch_workset_work(monkeypatch)

    assert await enqueue_clustering(uuid.uuid4()) is True
    (queued,), _ = enqueue.await_args
    assert queued.function == "workset_cluster_job"
    assert queued.timeout == worker.app_settings.worker_job_timeout
    assert queued.timeout > 10


@pytest.mark.asyncio
async def test_a_refused_auto_pass_that_cannot_be_put_back_fails_its_items(monkeypatch):
    """A row may only wait on a scheduled retry (``workset_processing``'s one
    postcondition): when the refused job cannot put itself back on the queue,
    the items behind its document are failed - visibly - instead of stranded
    ``pending`` with nothing coming back for them. A clustering job that
    cannot be rescheduled only loses a ranking pass, and is left at that."""
    from app import worker

    _pin(monkeypatch, **COMMUNITY)
    enqueue, work = _patch_workset_work(monkeypatch)
    enqueue.side_effect = ConnectionError("queue unavailable")
    fail_unscheduled = AsyncMock(return_value=1)
    monkeypatch.setattr(
        "app.services.workset_processing.fail_items_left_unscheduled", fail_unscheduled
    )

    await worker.workset_auto_pass_job({}, document_id="doc-1")
    fail_unscheduled.assert_awaited_once_with("doc-1")
    work.assert_not_awaited()

    fail_unscheduled.reset_mock()
    await worker.workset_cluster_job({}, workset_id="ws-1")
    fail_unscheduled.assert_not_awaited()

    enqueue.side_effect = None
    await worker.workset_auto_pass_job({}, document_id="doc-1")
    fail_unscheduled.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refused_auto_pass_whose_reschedule_hangs_still_fails_its_items(
    monkeypatch,
):
    """The queue's connection has no socket timeout: a reschedule that hangs
    must come back as "not scheduled" and fail the items, not outlast the job -
    cancelled at its timeout - and leave them ``pending``."""
    import asyncio

    from app import worker
    from app.services import kb_helpers

    _pin(monkeypatch, **COMMUNITY)
    enqueue, work = _patch_workset_work(monkeypatch)

    async def _hang(job):
        await asyncio.sleep(3600)

    enqueue.side_effect = _hang
    monkeypatch.setattr(kb_helpers, "_ENQUEUE_TIMEOUT_SECONDS", 0.05)
    fail_unscheduled = AsyncMock(return_value=1)
    monkeypatch.setattr(
        "app.services.workset_processing.fail_items_left_unscheduled", fail_unscheduled
    )

    await asyncio.wait_for(worker.workset_auto_pass_job({}, document_id="doc-1"), 5)
    fail_unscheduled.assert_awaited_once_with("doc-1")
    work.assert_not_awaited()


@pytest.mark.asyncio
async def test_items_left_without_a_scheduled_retry_are_failed_terminally(monkeypatch):
    from app.services import workset_processing

    workset = SimpleNamespace(id=uuid.uuid4(), processing="auto")
    item = SimpleNamespace(id=uuid.uuid4(), summary=None, priority_score=None)
    session = _Session([[(item, workset)]])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    failed = AsyncMock()
    monkeypatch.setattr("app.services.workset.fail_item", failed)

    assert await workset_processing.fail_items_left_unscheduled(str(uuid.uuid4())) == 1
    failed.assert_awaited_once()
    assert failed.await_args.args[1:] == (workset, item.id)
    assert failed.await_args.kwargs["max_attempts"] == workset_processing._TERMINAL_ATTEMPTS
    assert session.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("job", "kwargs"), _WORKSET_JOBS)
async def test_a_workset_job_runs_on_enterprise_edition(monkeypatch, job, kwargs):
    from app import worker

    _pin(monkeypatch, **ENTERPRISE)
    enqueue, work = _patch_workset_work(monkeypatch)

    await getattr(worker, job)({}, **kwargs)

    work.assert_awaited_once()
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("evaluation_enabled")
async def test_a_queued_evaluation_is_refused_with_the_reason_on_its_runs(monkeypatch):
    """With holding turned off (``AI_UNAVAILABLE_HOLD_MAX_SECONDS=0``) - the
    held case is ``test_ai_unavailable_hold``."""
    from app.services.orchestration_adapters import evaluation as evaluation_adapter
    from app.services.orchestration_adapters.base import AdapterContext

    _pin(monkeypatch, **COMMUNITY)
    monkeypatch.setattr(
        "app.core.config.settings.ai_unavailable_hold_max_seconds", 0
    )
    failed_runs = AsyncMock()
    monkeypatch.setattr(evaluation_adapter, "_fail_active_runs", failed_runs)
    execute = AsyncMock()
    monkeypatch.setattr("app.services.evaluation_engine.execute_evaluation", execute)
    evaluation_id = str(uuid.uuid4())

    execution = SimpleNamespace(id=uuid.uuid4(), payload={"evaluation_id": evaluation_id})

    result = await evaluation_adapter.EvaluationAdapter().execute(
        AdapterContext(db=_Session(strict=True), worker_id="w1"), execution
    )

    assert result.kind == "failed"
    assert result.retryable is False
    assert result.error == deps.licence_required_detail()
    # The pending batch only: a running episode waiting on its callback is in
    # flight and resumes through ``resume_episode``, untouched.
    failed_runs.assert_awaited_once_with(
        evaluation_id, execution.id, deps.licence_required_detail(), statuses=("pending",)
    )
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_extension_endpoint_is_refused_on_community_edition(monkeypatch):
    """Browser extension tasks run an agent on demand: the route takes the chat
    gate, whose licence half refuses Community Edition with the chat sentence."""
    from app.api.v1 import extension

    source = inspect.getsource(extension.execute_extension_task)
    assert "Depends(require_chat_enabled)" in source

    _pin(monkeypatch, **COMMUNITY)
    monkeypatch.setattr(platform_api, "get_deployment_edition", AsyncMock(return_value="community"))
    with pytest.raises(HTTPException) as caught:
        await deps.require_chat_enabled(object())
    assert caught.value.status_code == 403
    assert caught.value.detail == deps.chat_unavailable_detail()


# ── The scheduler tick ──────────────────────────────────────────────────────


_TICK_STEPS_GATED = {
    "scheduled_assignments": "app.services.autonomous._process_scheduled_assignments",
    "work_item_assignments": "app.services.autonomous._process_work_item_assignments",
    "integration_events": "app.services.event_poller.poll_integration_events",
    "kb_syncs": "app.services.kb_sync_scheduler.process_scheduled_kb_syncs",
    "nova_syncs": "app.services.nova_rule_sync.process_scheduled_nova_syncs",
    "stuck_documents": "app.services.kb_helpers.recover_stuck_documents",
    "failed_documents": "app.services.kb_helpers.recover_failed_documents",
    "stale_documents": "app.services.kb_helpers.recover_stale_documents",
    "scheduled_evaluations": "app.services.autonomous._process_scheduled_evaluations",
}
_TICK_STEPS_OPEN = {
    "scheduled_flows": "app.services.flow_triggers.process_scheduled_flows",
    "work_item_flows": "app.services.flow_triggers.process_work_item_flows",
    # The run re-checks the edition and AI gate and blocks with the reason.
    "scheduled_work_items": (
        "app.services.scheduled_work_item_runner.dispatch_due_scheduled_work_items"
    ),
    "flow_events": "app.services.flow_triggers.poll_flow_integration_events",
    # Housekeeping, not AI work: a deleted config's chunk references are
    # cleared on every edition.
    "embedding_provider_references": (
        "app.services.llm_provider_deletion.clear_deleted_embedding_provider_references"
    ),
}
_TICK_HOUSEKEEPING = (
    "app.services.token_refresh.refresh_expiring_tokens",
    "app.services.kb_sync_scheduler.reset_stale_syncs",
    "app.services.sub_agent.recover_orphaned_sub_agent_tasks",
    "app.services.autonomous._recover_stale_orchestration_executions",
    "app.services.autonomous._cleanup_stale_assignment_runs",
    "app.services.autonomous._cleanup_expired_kb_export_packages",
    "app.services.autonomous._run_trace_storage_maintenance",
    "app.services.telemetry.export_telemetry_if_due",
)


def _stub_tick(monkeypatch, session) -> dict:
    """Every step of the tick replaced by a spy, so the gate is what decides."""
    from app.services import autonomous

    monkeypatch.setattr(autonomous, "async_session", lambda: session)
    monkeypatch.setattr(autonomous.settings, "model_prices_git_sync_enabled", False)
    for counter in (
        "_apikey_stats_tick_counter",
        "_quota_flush_tick_counter",
        "_teams_subscription_tick_counter",
    ):
        monkeypatch.setattr(autonomous, counter, 0)
    # The wall-clock jobs: none due unless a test says so (no Redis here).
    monkeypatch.setattr("app.services.wall_clock_schedule.claim", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.wall_clock_schedule.complete", AsyncMock())
    monkeypatch.setattr("app.services.wall_clock_schedule.backlog_pending", AsyncMock(return_value=False))
    monkeypatch.setattr(autonomous, "_dispatch_refusal_logged", None)
    monkeypatch.setattr(
        "app.services.kb_helpers.resolve_embedding_recovery_state",
        AsyncMock(return_value=True),
    )
    spies = {}
    for name, path in {**_TICK_STEPS_GATED, **_TICK_STEPS_OPEN}.items():
        spies[name] = AsyncMock(return_value=0)
        monkeypatch.setattr(path, spies[name])
    for path in _TICK_HOUSEKEEPING:
        monkeypatch.setattr(path, AsyncMock(return_value=0))
    return spies


@pytest.mark.asyncio
async def test_the_scheduler_tick_dispatches_no_ai_work_on_community_edition(
    monkeypatch, caplog
):
    from app.services import autonomous

    _pin(monkeypatch, **COMMUNITY)
    spies = _stub_tick(monkeypatch, _Session([4]))

    with caplog.at_level("INFO", logger="app.services.autonomous"):
        await autonomous._run_autonomous_tick_inner()
        await autonomous._run_autonomous_tick_inner()

    for name in _TICK_STEPS_GATED:
        spies[name].assert_not_awaited()
    # Deterministic flows keep running (FR-014); an AI flow is held back per
    # flow, inside the trigger paths. The housekeeping sweep runs too.
    for name in _TICK_STEPS_OPEN:
        assert spies[name].await_count == 2, name
    notices = [r for r in caplog.records if "not licensed" in r.getMessage()]
    assert len(notices) == 1, "logged once per off-period, not per tick"
    assert "4 enabled scheduled assignment(s)" in notices[0].getMessage()


@pytest.mark.asyncio
async def test_the_scheduler_tick_names_a_changed_reason_again(monkeypatch, caplog):
    from app.services import autonomous

    _pin(monkeypatch, **AI_SWITCHED_OFF)
    _stub_tick(monkeypatch, _Session([2, 2]))
    with caplog.at_level("INFO", logger="app.services.autonomous"):
        await autonomous._run_autonomous_tick_inner()
        _pin(monkeypatch, **COMMUNITY)
        agentic_gate.invalidate_cache()
        await autonomous._run_autonomous_tick_inner()
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("AI is disabled: no AI scheduled work") for m in messages)
    assert any(m.startswith("AI is not licensed on this deployment") for m in messages)


@pytest.mark.asyncio
async def test_the_scheduler_tick_dispatches_on_enterprise_edition(monkeypatch):
    from app.services import autonomous

    _pin(monkeypatch, **ENTERPRISE)
    spies = _stub_tick(monkeypatch, _Session())
    await autonomous._run_autonomous_tick_inner()
    for name in (
        "scheduled_assignments",
        "work_item_assignments",
        "integration_events",
        "embedding_provider_references",
    ):
        spies[name].assert_awaited_once()


async def _tick_runs_the_ai_adoption_sync(monkeypatch, platform_mode: str) -> bool:
    """One tick on Enterprise Edition with the hourly sync due."""
    from app.core.config import settings as app_settings
    from app.services import autonomous

    _pin(monkeypatch, **ENTERPRISE)
    _stub_tick(monkeypatch, _Session())
    monkeypatch.setattr(app_settings, "platform_mode", platform_mode)

    from app.services import wall_clock_schedule

    async def _claim(job: wall_clock_schedule.Job, **_kw: object) -> object:
        return object() if job is wall_clock_schedule.AI_METRICS_SYNC else None

    monkeypatch.setattr(wall_clock_schedule, "claim", _claim)
    collect = AsyncMock(return_value={})
    monkeypatch.setattr("app.services.ai_metrics_collector.collect_all_metrics", collect)
    await autonomous._run_autonomous_tick_inner()
    return collect.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_mode", ["copilot", "dev", "xtm_one"])
async def test_the_ai_adoption_sync_runs_in_every_agentic_mode(monkeypatch, platform_mode):
    """AI adoption is generic AI (dev-docs/mode-split.md, S2): XTM One mode too."""
    assert await _tick_runs_the_ai_adoption_sync(monkeypatch, platform_mode)


@pytest.mark.asyncio
async def test_the_ai_adoption_sync_never_calls_a_vendor_in_ai_disabled_mode(monkeypatch):
    """Even with AI switched back on, ``ai_disabled`` makes no vendor call."""
    assert not await _tick_runs_the_ai_adoption_sync(monkeypatch, "ai_disabled")


# ── Tripwires ───────────────────────────────────────────────────────────────


#: Every module allowed to decide with the AI switch ALONE, and why. None of
#: them starts background work: an execution entry point asks the background
#: gate (``agentic_gate.agentic_refusal``), which is the switch AND the
#: licence. A new reader must be classified here - or, more likely, switched to
#: the gate.
_TOGGLE_ONLY_READERS: dict[str, str] = {
    "services/agentic_gate.py": "the background gate itself: the switch is its first half",
    "api/deps.py": (
        "the request gates: require_ai_enabled is the switch by contract, for routes "
        "that carry their own licence gate; require_agentic_ai and require_chat_enabled "
        "ask it first only to name the reason, then the licence"
    ),
    "api/v1/platform.py": (
        "defines the combined is_agentic_ai_enabled / is_chat_enabled the request gates use"
    ),
    "api/v1/settings.py": (
        "the model-discovery read, which answers empty while AI is off - a UI state, "
        "not an execution"
    ),
    "api/v1/flow_runs.py": (
        "the manual run: the route's require_agentic_ai_licensed already answered the "
        "licence, this asks per flow whether the graph needs AI"
    ),
    "api/v1/mcp_endpoint.py": "asks the switch first to state the reason, then is_chat_licensed",
    "api/v1/llm_proxy.py": "the same two-reason refusal as the MCP agent endpoint",
    "api/v1/a2a_server.py": "asks the switch first, then require_chat_enabled for the licence",
    "api/v1/public_chat.py": "asks the switch first, then is_chat_enabled for the licence",
    "api/v1/ctem_metrics.py": "asks the switch first, then is_agentic_ai_licensed",
    "api/v1/mcp_platform.py": (
        "the platform MCP servers are a Community Edition feature serving product APIs "
        "(no model runs here), refused only with AI switched off (FR-029)"
    ),
    "api/v1/mcp_xtm.py": (
        "XTM One's own management and observability API over MCP, no agent runs "
        "here; asks the switch first (FR-029), then is_agentic_ai_licensed, "
        "then its admin toggle"
    ),
    "api/v1/video_studio.py": (
        "the editor preview speaks a draft's narrations only with AI on - the switch at "
        "request entry, as require_ai_enabled gates the render, clip and music routes; "
        "Video Studio carries no licence gate"
    ),
    "services/telemetry.py": "reports the switch's state, gates nothing",
    "services/health/agent_checks.py": (
        "the health engine's agent checks read counts and statuses, call no model and "
        "start nothing; with AI off agents do not run, so they record nothing"
    ),
}

#: Modules that start or continue background AI work. They must never decide
#: with the switch alone.
_BACKGROUND_EXECUTION_MODULES = frozenset(
    {
        "channels/router.py",
        "services/assignment_engine.py",
        "services/autonomous.py",
        "services/flow_triggers.py",
        "services/flow_runner.py",
        "services/event_poller.py",
        "services/sub_agent.py",
        "services/parent_wake.py",
        "services/evaluation_engine.py",
        "services/orchestration_adapters/assignment.py",
        "services/orchestration_adapters/sub_agent.py",
        "services/orchestration_adapters/flow.py",
        "services/orchestration_adapters/evaluation.py",
        "services/orchestration_adapters/autonomous.py",
        "worker.py",
        "api/v1/webhooks.py",
        "api/v1/integration_notifications.py",
        "api/v1/extension.py",
    }
)


def _reads_the_switch_alone(tree: ast.AST) -> bool:
    """``from ... ai_state import is_ai_enabled`` or ``<ai_state>.is_ai_enabled``,
    read off the AST: a mention in prose is not a read."""
    for node in app_sources.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").endswith("ai_state")
            and any(alias.name == "is_ai_enabled" for alias in node.names)
        ):
            return True
        if isinstance(node, ast.Attribute) and node.attr == "is_ai_enabled":
            return True
    return False


def test_the_tripwire_reads_the_code_not_the_prose():
    assert _reads_the_switch_alone(ast.parse("from app.services.ai_state import is_ai_enabled"))
    assert _reads_the_switch_alone(
        ast.parse("from app.services import ai_state\nawait ai_state.is_ai_enabled(db)")
    )
    assert _reads_the_switch_alone(ast.parse("x = mod.is_ai_enabled"))
    for clean in (
        "from app.services.ai_state import AI_DISABLED_CODE",
        "from app.services.ai_state import is_ai_enabled_strict",
        "# only ai_state.is_ai_enabled() used to be asked here",
        "'''ask is_ai_enabled() for the switch'''",
        "from app.services.agentic_gate import agentic_refusal",
    ):
        assert not _reads_the_switch_alone(ast.parse(clean)), clean


def test_no_execution_entry_point_decides_with_the_ai_switch_alone():
    """Background work asks the combined gate."""
    readers: set[str] = set()
    scanned = 0
    for path in app_sources.python_files(APP):
        if path.name == "ai_state.py":
            continue
        scanned += 1
        tree = app_sources.tree(path)
        if _reads_the_switch_alone(tree):
            readers.add(path.relative_to(APP).as_posix())

    # Never vacuous: a moved tree must fail loudly, not pass over nothing.
    assert scanned > 100, scanned
    offenders = readers & _BACKGROUND_EXECUTION_MODULES
    assert not offenders, (
        f"execution entry points deciding with the AI switch alone: {sorted(offenders)} "
        "- ask app.services.agentic_gate.agentic_refusal instead"
    )
    assert readers == set(_TOGGLE_ONLY_READERS), (
        f"unclassified: {sorted(readers - set(_TOGGLE_ONLY_READERS))}; "
        f"stale: {sorted(set(_TOGGLE_ONLY_READERS) - readers)}"
    )
    assert not set(_TOGGLE_ONLY_READERS) & _BACKGROUND_EXECUTION_MODULES
    for module, reason in _TOGGLE_ONLY_READERS.items():
        assert len(reason) > 30, f"{module}: give a real reason"


def _source(dotted: str) -> str:
    import importlib

    module_name, _, qualname = dotted.partition(":")
    obj = importlib.import_module(module_name)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return inspect.getsource(obj)


@pytest.mark.parametrize(
    "entry_point",
    [
        "app.channels.router:handle_message",
        "app.services.assignment_engine:_refuse_if_ai_disabled",
        "app.services.assignment_engine:execute_assignment_run",
        "app.services.assignment_engine:drain_assignment_queues",
        "app.services.autonomous:_run_autonomous_tick_inner",
        "app.services.autonomous:_fanout_assignment_to_users",
        "app.services.flow_triggers:hold_back_if_ai_required",
        "app.services.sub_agent:_execute_sub_agent_task",
        "app.services.parent_wake:_wake_refused",
        "app.services.event_poller:poll_integration_events",
        "app.services.orchestration_adapters.evaluation:EvaluationAdapter.execute",
        "app.services.scheduled_work_item_runner:_run_time_refusal",
        "app.worker:_skip_because_ai_is_disabled",
    ],
)
def test_every_background_entry_point_asks_the_combined_gate(entry_point):
    source = _source(entry_point)
    assert "agentic_refusal(" in source or "agentic_verdict(" in source, entry_point
    assert "is_ai_enabled" not in source, entry_point


@pytest.mark.parametrize(
    ("entry_point", "refusal_site"),
    [
        ("app.services.assignment_engine:execute_assignment_run", None),
        (
            "app.services.sub_agent:_execute_sub_agent_task",
            "app.services.sub_agent:_refuse_task_before_turn",
        ),
        ("app.services.orchestration_adapters.evaluation:EvaluationAdapter.execute", None),
    ],
)
def test_every_entry_point_that_fails_work_defers_a_transient_refusal(
    entry_point, refusal_site
):
    """The sites that would FAIL work on a refusal ask the verdict and act on
    a transient refusal before they record anything: a database blip on a cold
    worker is not "not licensed". A site that hands a definitive refusal to a
    helper (``refusal_site``) calls it only after that check."""
    source = _source(entry_point)
    assert "agentic_verdict(" in source, entry_point
    transient = source.index("if verdict.transient:")
    if refusal_site is None:
        refusal_source = source
        refused = source.index("expired_message(")
    else:
        refusal_source = _source(refusal_site)
        refused = source.index(refusal_site.rpartition(":")[2] + "(")
    # And a definitive refusal is held before anything is failed.
    held = min(
        refusal_source.index(token)
        for token in ("if hold:", "and hold:", "hold_budget_left(execution)")
        if token in refusal_source
    )
    failed = refusal_source.index("expired_message(")
    assert transient < refused and held < failed, entry_point


def test_both_assignment_trigger_paths_and_the_chain_share_the_gate():
    assert "_refuse_if_ai_disabled(assignment)" in _source(
        "app.services.assignment_engine:trigger_assignment"
    )
    assert "_refuse_if_ai_disabled(assignment)" in _source(
        "app.services.assignment_engine:trigger_or_queue_assignment"
    )
    # The run start (the verdict: a transient refusal is deferred), and the
    # next assignment of a chain once a run finished.
    source = _source("app.services.assignment_engine:execute_assignment_run")
    assert source.count("await agentic_verdict(db)") == 1
    assert source.count("await agentic_refusal(db)") == 1
    # Every wake - a task's, a partner's, a follow-up - runs through one turn,
    # which asks after persisting the wake's text and before taking the turn.
    turn = _source("app.services.parent_wake:_run_parent_wake_turn")
    asked = turn.index("await _wake_refused(db")
    assert turn.index("await store_message(") < asked < turn.index("acquire_or_fold(")


@pytest.mark.parametrize(
    "handler",
    [
        # The license installed or removed.
        "app.api.v1.settings:update_general_assistant_config",
        "app.api.v1.settings:update_platform_settings_endpoint",
        # A platform registering (EE connected, or re-registering without its
        # license), unregistering or purged.
        "app.api.v1.platform:register_platform",
        "app.api.v1.platform:unregister_platform",
        "app.api.v1.platform:purge_registration",
    ],
)
def test_every_edition_change_invalidates_the_cache_after_its_commit(handler):
    source = _source(handler)
    invalidation = source.index("agentic_gate.invalidate_cache()")
    assert "await db.commit()" in source[:invalidation], (
        f"{handler}: invalidate AFTER the commit, or a concurrent reader re-caches "
        "the old edition for a whole TTL"
    )
