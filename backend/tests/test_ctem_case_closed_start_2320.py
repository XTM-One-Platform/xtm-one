"""A queued ``ctem_case.opened`` run whose case is no longer open ends at zero tokens (#2320).

1. A case in any state outside ``OPEN_STATES`` (resolved, mitigated_watching, accepted_risk)
   ends the run ``skipped`` with ``case_not_open`` and its state in the summary.
2. A case still in one of ``OPEN_STATES`` runs as today.
3. Every other case event (reopened, sla_breached, validation_finished, retest_requested,
   stalled) is never checked, whatever the case state.
4. A check that cannot read the case lets the run go ahead.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.ctem_case import CLOSED_STATES, OPEN_STATES
from app.services import ctem_case_run_start as run_start
from app.services import ctem_cases_store as store


class _Db:
    def __init__(self, state: str | None) -> None:
        self.state = state

    async def execute(self, _query: Any) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: self.state)


def _context(case_id: uuid.UUID, event: str = "ctem_case.opened") -> dict[str, Any]:
    return {"type": "platform_event", "event": event, "object_type": "ctem_case", "object_id": str(case_id)}


@pytest.fixture(autouse=True)
def _no_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _into(_db: Any, _case_id: uuid.UUID, *, organization_id: Any) -> None:
        raise AssertionError("an existing case never reads the merge events")

    monkeypatch.setattr(store, "merged_into", _into)


def test_every_known_state_is_open_or_closed() -> None:
    assert not set(OPEN_STATES) & set(CLOSED_STATES)
    assert CLOSED_STATES


@pytest.mark.parametrize("state", CLOSED_STATES)
async def test_a_closed_case_is_skipped_with_its_state(state: str) -> None:
    case_id = uuid.uuid4()
    verdict = await run_start.case_start(_Db(state), case_id)  # type: ignore[arg-type]
    assert not verdict.proceed and verdict.context == {}
    assert verdict.error == run_start.CASE_NOT_OPEN
    assert str(case_id) in verdict.summary and f"state {state}" in verdict.summary
    assert "no model call" in verdict.summary


@pytest.mark.parametrize("state", OPEN_STATES)
async def test_an_open_case_runs_as_today(state: str) -> None:
    verdict = await run_start.case_start(_Db(state), uuid.uuid4())  # type: ignore[arg-type]
    assert verdict.proceed and verdict.context == {} and verdict.summary == ""


@pytest.mark.parametrize(
    "event",
    [
        "ctem_case.reopened",
        "ctem_case.sla_breached",
        "ctem_case.validation_finished",
        "ctem_case.retest_requested",
        "ctem_case.stalled",
    ],
)
def test_other_case_events_are_never_checked(event: str) -> None:
    assert run_start.case_of(_context(uuid.uuid4(), event)) is None


def _run(case_id: uuid.UUID, event: str = "ctem_case.opened") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        trigger_context=_context(case_id, event),
        status="running",
        error=None,
        result_summary=None,
        total_input_tokens=None,
        total_output_tokens=None,
        started_at=None,
        completed_at=None,
        is_company_managed=False,
    )


class _Session:
    def __init__(self, state: str | None) -> None:
        self.state = state
        self.committed = False

    async def execute(self, _query: Any) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: self.state)

    async def commit(self) -> None:
        self.committed = True


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from app.services import assignment_engine as engine

    seen: list[str] = []

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    async def _publish(_user: str, _run: str, status: str, *_a: Any, **_k: Any) -> None:
        seen.append(status)

    monkeypatch.setattr(engine, "ensure_single_writer", _noop)
    monkeypatch.setattr(engine, "_release_run_work_item", _noop)
    monkeypatch.setattr(engine, "_publish_run_status", _publish)
    return seen


@pytest.mark.parametrize("state", CLOSED_STATES)
async def test_the_engine_ends_a_closed_case_run_at_zero_tokens(state: str, published: list[str]) -> None:
    from app.services import assignment_engine as engine

    run = _run(uuid.uuid4())
    session = _Session(state)
    ended = await engine._skip_by_case_start(
        session,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        agent=SimpleNamespace(),
        assignment=SimpleNamespace(name="Case opened or reopened"),  # type: ignore[arg-type]
    )
    assert ended and session.committed and published == ["skipped"]
    assert (run.status, run.error, run.total_input_tokens, run.total_output_tokens) == (
        "skipped",
        run_start.CASE_NOT_OPEN,
        0,
        0,
    )
    assert f"state {state}" in run.result_summary


async def test_the_engine_lets_an_open_case_run_go_ahead(published: list[str]) -> None:
    from app.services import assignment_engine as engine

    run = _run(uuid.uuid4())
    session = _Session("analyzing")
    ended = await engine._skip_by_case_start(
        session,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        agent=SimpleNamespace(),
        assignment=SimpleNamespace(name="Case opened or reopened"),  # type: ignore[arg-type]
    )
    assert not ended and run.status == "running" and not session.committed and published == []


async def test_the_engine_never_checks_a_reopened_run_of_a_closed_case(published: list[str]) -> None:
    from app.services import assignment_engine as engine

    run = _run(uuid.uuid4(), "ctem_case.validation_finished")
    ended = await engine._skip_by_case_start(
        _Session("resolved"),  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        agent=SimpleNamespace(),
        assignment=SimpleNamespace(name="Case opened or reopened"),  # type: ignore[arg-type]
    )
    assert not ended and run.status == "running" and published == []


async def test_a_check_that_cannot_read_the_case_lets_the_run_go_ahead(published: list[str]) -> None:
    from app.services import assignment_engine as engine

    class _Broken(_Session):
        async def execute(self, _query: Any) -> Any:
            raise RuntimeError("database unavailable")

    run = _run(uuid.uuid4())
    ended = await engine._skip_by_case_start(
        _Broken(None),  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        agent=SimpleNamespace(),
        assignment=SimpleNamespace(name="Case opened or reopened"),  # type: ignore[arg-type]
    )
    assert not ended and run.status == "running" and published == []
