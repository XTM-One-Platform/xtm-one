"""An evaluation entry run on a Concierge sandbox agent copy (#8257).

A copy's turn runs as one case of level 2's interception, on the entry's own
recordings and under a case ref naming the run and the entry; any other agent
runs as before, with no case.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services.concierge import datasets
from app.services.concierge.sandbox import copies, runs
from app.services.evaluation.sandbox_cases import SandboxCaseUnavailableError, agent_sandbox_case, case_ref


def _agent(*, sandbox: bool) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), is_sandbox=sandbox)


async def test_an_agent_that_is_not_a_copy_runs_with_no_case(monkeypatch):
    async def _unexpected(*_args, **_kwargs):
        raise AssertionError("no sandbox read for a production agent")

    monkeypatch.setattr(copies, "sandbox_copy_of", _unexpected)

    async with agent_sandbox_case(
        None, _agent(sandbox=False), run_id=uuid.uuid4(), entry_id=None, user_id=None, model=None
    ) as session:
        assert session is None


async def test_a_copy_s_turn_is_a_case_on_the_entry_s_recordings(monkeypatch):
    copy, session = SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(report=None)
    run_id, entry_id = uuid.uuid4(), uuid.uuid4()
    recordings = [SimpleNamespace(tool="search_cases")]
    calls = []

    async def _copy_of(_db, *, agent_id):
        calls.append(("copy_of", agent_id))
        return copy

    async def _recordings(_db, entry_ids):
        calls.append(("recordings", list(entry_ids)))
        return {entry_id: recordings}

    @asynccontextmanager
    async def _case(_db, case_copy, **kwargs):
        calls.append(("case", case_copy, kwargs))
        yield session

    monkeypatch.setattr(copies, "sandbox_copy_of", _copy_of)
    monkeypatch.setattr(datasets, "entry_recordings", _recordings)
    monkeypatch.setattr(runs, "sandbox_case", _case)
    agent = _agent(sandbox=True)

    async with agent_sandbox_case(None, agent, run_id=run_id, entry_id=entry_id, user_id="u-1", model="gpt-x") as bound:
        assert bound is session

    assert calls == [
        ("copy_of", agent.id),
        ("recordings", [entry_id]),
        (
            "case",
            copy,
            {"case_ref": case_ref(run_id, entry_id), "recorded_calls": recordings, "user_id": "u-1", "model": "gpt-x"},
        ),
    ]
    assert case_ref(run_id, entry_id) == f"eval:{run_id}:{entry_id}"


async def test_a_copy_whose_record_is_gone_is_left_out(monkeypatch):
    async def _no_copy(_db, *, agent_id):
        return None

    monkeypatch.setattr(copies, "sandbox_copy_of", _no_copy)

    with pytest.raises(SandboxCaseUnavailableError):
        async with agent_sandbox_case(
            None, _agent(sandbox=True), run_id=uuid.uuid4(), entry_id=None, user_id=None, model=None
        ):
            pass
