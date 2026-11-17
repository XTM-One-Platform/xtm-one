"""Whether a run's cited report is being written, on its writer's word.

The block used to show "Writing the cited report..." for three minutes after
every completed run, including runs that would never have one. Pinned here:
the writer's marker exists from the run's first step to the writer's return,
whatever it returned (a report, none, a cancel, a crash), and a marker that
cannot be read is said as ``unknown``, never as an activity.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services import deep_investigation_chat as dic
from app.services import investigation_report_status as status
from app.services.investigation_orchestrator import SourceOutcome
from app.services.investigation_packs import SourceDefinition, SourcePack

from .test_deep_investigation_chat import _install, _patch_querier, _StubSession


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.values[key] = value
        self.ttls[key] = ex

    async def delete(self, key):
        self.values.pop(key, None)

    async def mget(self, keys):
        return [self.values.get(k) for k in keys]


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(status, "_redis", _client)
    return fake


def _run(report=None, run_id=None):
    return SimpleNamespace(id=run_id or uuid.uuid4(), report=report)


@pytest.mark.asyncio
async def test_the_marker_says_writing_until_the_writer_clears_it(redis):
    run = _run()
    await status.mark_report_writing(run.id)
    assert redis.ttls[status.report_writing_key(run.id)] == status.REPORT_WRITING_TTL_SECONDS
    assert status.report_status(run, await status.reports_being_written([run.id])) == "writing"
    await status.clear_report_writing(run.id)
    assert status.report_status(run, await status.reports_being_written([run.id])) == "none"
    assert status.report_status(_run("# Report"), set()) == "written"


@pytest.mark.asyncio
async def test_an_unreadable_marker_claims_nothing():
    """The unit suite's seam fails, as a deployment without Redis does."""
    run = _run()
    await status.mark_report_writing(run.id)  # never raises
    await status.clear_report_writing(run.id)
    assert await status.reports_being_written([run.id]) is None
    assert status.report_status(run, None) == "unknown"
    assert await status.reports_being_written([]) == set()


@pytest.mark.asyncio
async def test_the_writer_holds_the_marker_while_it_writes_and_drops_it_after(monkeypatch, redis):
    _install(monkeypatch, SourceDefinition(name="A", url_template="https://a.test/{value}", subject_types=("unknown",)))
    _patch_querier(monkeypatch, {})
    seen_while_writing: list[set[str]] = []

    async def _write(_db, result, **_kw):
        seen_while_writing.append(await status.reports_being_written([result.investigation.id]))
        return "a report"

    monkeypatch.setattr(dic, "_write_report", _write, raising=True)
    _text, result = await dic.run_deep_investigation_chat(_StubSession(), subject="x")
    run_id = str(result.investigation.id)
    assert seen_while_writing == [{run_id}]
    assert await status.reports_being_written([run_id]) == set()


@pytest.mark.asyncio
async def test_a_cancelled_run_leaves_no_marker(monkeypatch, redis):
    _install(monkeypatch, SourceDefinition(name="A", url_template="https://a.test/{value}", subject_types=("unknown",)))
    _patch_querier(monkeypatch, {})
    cancel = asyncio.Event()
    cancel.set()
    _text, result = await dic.run_deep_investigation_chat(_StubSession(), subject="x", cancel_event=cancel)
    assert redis.values == {}
    assert result.investigation.status == "cancelled"


@pytest.mark.asyncio
async def test_a_writer_that_fails_leaves_no_marker(monkeypatch, redis):
    _install(monkeypatch, SourceDefinition(name="A", url_template="https://a.test/{value}", subject_types=("unknown",)))
    _patch_querier(monkeypatch, {})

    async def _write(*_a, **_kw):
        raise RuntimeError("model down")

    monkeypatch.setattr(dic, "_write_report", _write, raising=True)
    with pytest.raises(RuntimeError):
        await dic.run_deep_investigation_chat(_StubSession(), subject="x")
    assert redis.values == {}


@pytest.mark.asyncio
async def test_a_run_that_raises_leaves_no_marker(monkeypatch, redis):
    _install(monkeypatch, SourceDefinition(name="A", url_template="https://a.test/{value}", subject_types=("unknown",)))

    def _factory(*_args, **_kwargs):
        async def _query(_source, _subject, _query):
            raise asyncio.CancelledError

        return _query

    monkeypatch.setattr(dic, "make_dispatching_querier", _factory)
    with pytest.raises(asyncio.CancelledError):
        await dic.run_deep_investigation_chat(_StubSession(), subject="x")
    assert redis.values == {}


@pytest.mark.asyncio
async def test_the_hook_runs_once_with_the_run_id_before_any_source(monkeypatch):
    from app.services.investigation_orchestrator import DeepInvestigationOrchestrator

    queried: list[str] = []
    created: list[tuple[uuid.UUID, int]] = []

    async def _query(source, _subject, _query):
        queried.append(source.name)
        return SourceOutcome(findings_count=1)

    async def _created(run_id):
        created.append((run_id, len(queried)))

    pack = SourcePack(
        slug="p",
        label="P",
        description="",
        subject_types=("unknown",),
        sources=(SourceDefinition(name="A", url_template="https://a.test/{value}", subject_types=("unknown",)),),
        max_iterations=1,
    )
    result = await DeepInvestigationOrchestrator(_StubSession(), _query).run(
        subject_value="x", packs=[pack], on_created=_created
    )
    assert created == [(result.investigation.id, 0)]
    assert queried == ["A"]
