"""A merged-away case id answers its survivor; a queued case run of a merged or deleted case (#2289).

1. ``get_ctem_case`` (and the other case tools) on a merged id answers the survivor id, when and
   by whom; a never-created id still answers ``No case <id>.``.
2. A unique 8+ character prefix of a merged id resolves to the merged id, so the read answers
   the survivor too.
3. A queued ``ctem_case.opened`` run starts with ``merged_into`` in its trigger context when its
   case was merged, and is skipped at zero tokens when its case was deleted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_case_run_start as run_start
from app.services import ctem_cases_store as store
from tests.test_ctem_case_tools import env  # noqa: F401 - fixture

AT = datetime(2026, 11, 25, 16, 0, tzinfo=UTC)


def _merged(case_id: uuid.UUID, survivor: uuid.UUID) -> store.MergedCase:
    return store.MergedCase(case_id=case_id, survivor_id=survivor, at=AT, actor="agent-7")


async def test_a_merged_id_answers_its_survivor(env: SimpleNamespace) -> None:  # noqa: F811
    gone = uuid.uuid4()
    env.merged = {gone: _merged(gone, env.case.id)}
    answer = await env.call("get_ctem_case", case_id=str(gone))
    assert answer == (f"Case {gone} was merged into {env.case.id} at {AT.isoformat()} by agent-7; read the survivor.")
    assert "merged into" in await env.call("update_ctem_case", case_id=str(gone), state="remediating")


async def test_a_never_created_id_is_still_no_case(env: SimpleNamespace) -> None:  # noqa: F811
    unknown = uuid.uuid4()
    assert await env.call("get_ctem_case", case_id=str(unknown)) == f"No case {unknown}."


async def test_a_prefix_of_a_merged_id_answers_its_survivor(
    env: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gone = uuid.uuid4()
    env.merged = {gone: _merged(gone, env.case.id)}

    async def _none(_db: Any, prefix: str, *, organization_id: Any, limit: int = 2) -> list[uuid.UUID]:
        return []

    monkeypatch.setattr(store, "case_ids_with_prefix", _none)
    answer = await env.call("get_ctem_case", case_id=str(gone)[:8])
    assert answer.startswith(f"Case {gone} was merged into {env.case.id}")


class _Db:
    def __init__(self, exists: bool) -> None:
        self.exists = exists

    async def execute(self, _query: Any) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: uuid.uuid4() if self.exists else None)


def _context(case_id: uuid.UUID, event: str = "ctem_case.opened") -> dict[str, Any]:
    return {"type": "platform_event", "event": event, "object_type": "ctem_case", "object_id": str(case_id)}


def test_the_check_reads_the_opened_event_only() -> None:
    case_id = uuid.uuid4()
    assert run_start.case_of(_context(case_id)) == case_id
    assert run_start.case_of(_context(case_id, "ctem_case.sla_breached")) is None
    assert run_start.case_of({"event": "ctem_case.opened", "object_type": "ctem_case", "object_id": "x"}) is None
    assert run_start.case_of(None) is None


async def test_a_live_case_starts_unchanged() -> None:
    verdict = await run_start.case_start(_Db(exists=True), uuid.uuid4())  # type: ignore[arg-type]
    assert verdict.proceed and verdict.context == {}


async def test_a_merged_case_starts_informed(monkeypatch: pytest.MonkeyPatch) -> None:
    gone, survivor = uuid.uuid4(), uuid.uuid4()

    async def _into(_db: Any, case_id: uuid.UUID, *, organization_id: Any) -> store.MergedCase | None:
        assert organization_id is None
        return _merged(case_id, survivor) if case_id == gone else None

    monkeypatch.setattr(store, "merged_into", _into)
    verdict = await run_start.case_start(_Db(exists=False), gone)  # type: ignore[arg-type]
    assert verdict.proceed
    assert verdict.context[run_start.MERGED_INTO_KEY]["case_id"] == str(survivor)
    assert verdict.context[run_start.MERGED_INTO_KEY]["actor"] == "agent-7"


async def test_a_deleted_case_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _into(_db: Any, case_id: uuid.UUID, *, organization_id: Any) -> None:
        return None

    monkeypatch.setattr(store, "merged_into", _into)
    case_id = uuid.uuid4()
    verdict = await run_start.case_start(_Db(exists=False), case_id)  # type: ignore[arg-type]
    assert not verdict.proceed and str(case_id) in verdict.summary and "no model call" in verdict.summary


async def test_the_engine_skips_a_deleted_case_run_at_zero_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import assignment_engine as engine

    case_id = uuid.uuid4()
    published: list[str] = []

    async def _start(_db: Any, _case_id: uuid.UUID) -> run_start.CaseStart:
        return run_start.CaseStart(proceed=False, context={}, summary="Skipped: gone")

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    async def _publish(_user: str, _run: str, status: str, *_a: Any, **_k: Any) -> None:
        published.append(status)

    monkeypatch.setattr(run_start, "case_start", _start)
    monkeypatch.setattr(engine, "ensure_single_writer", _noop)
    monkeypatch.setattr(engine, "_release_run_work_item", _noop)
    monkeypatch.setattr(engine, "_publish_run_status", _publish)

    class _Session:
        committed = False

        async def commit(self) -> None:
            self.committed = True

    run = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        trigger_context=_context(case_id),
        status="running",
        error=None,
        result_summary=None,
        total_input_tokens=None,
        total_output_tokens=None,
        started_at=None,
        completed_at=None,
        is_company_managed=False,
    )
    session = _Session()
    ended = await engine._skip_by_case_start(
        session,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        agent=SimpleNamespace(),
        assignment=SimpleNamespace(name="Case opened or reopened"),  # type: ignore[arg-type]
    )
    assert ended and session.committed and published == ["skipped"]
    assert (run.status, run.error, run.total_input_tokens) == ("skipped", run_start.CASE_GONE, 0)


async def test_the_engine_adds_the_survivor_to_a_merged_case_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import assignment_engine as engine

    async def _start(_db: Any, _case_id: uuid.UUID) -> run_start.CaseStart:
        return run_start.CaseStart(proceed=True, context={run_start.MERGED_INTO_KEY: {"case_id": "s"}})

    monkeypatch.setattr(run_start, "case_start", _start)
    run = SimpleNamespace(trigger_context=_context(uuid.uuid4()), status="running")
    ended = await engine._skip_by_case_start(
        SimpleNamespace(),  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        agent=SimpleNamespace(),
        assignment=SimpleNamespace(name="a"),  # type: ignore[arg-type]
    )
    assert not ended and run.status == "running"
    assert run.trigger_context[run_start.MERGED_INTO_KEY] == {"case_id": "s"}
