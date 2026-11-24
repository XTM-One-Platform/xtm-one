"""An agent run never moves a done or cancelled work item back to work.

A run dispatched for an item another run completed meanwhile (a release racing
the merge run, a slice item its creator closed seconds after filing it) starts
by marking it ``in_progress``: that reopened finished work and re-routed its
completion to the fleet_impact targets at the next close. The write is now
refused for an agent run; ``status="open"`` (a deliberate reopening) and a
person's write still go through.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import app.services.work_item as wi_service
from tests.test_work_item_completion_live_holder import AGENT, _item, world  # noqa: F401


async def _no_cycle(*_a: object, **_kw: object) -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("finished", ["done", "cancelled"])
@pytest.mark.parametrize("status", ["in_progress", "claimed", "blocked"])
async def test_an_agent_run_cannot_put_a_finished_item_back_to_work(
    world: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    finished: str,
    status: str,
) -> None:
    item = _item(status=finished, result="merged", claimed_by_run_id=None)
    session = world(item, None)
    monkeypatch.setattr("app.services.work_item_wait_cycles.refuse_wait_cycle", _no_cycle)

    with pytest.raises(ValueError, match=f"already {finished}"):
        await wi_service.update_work_item(
            str(item.id), user_id=str(item.user_id), status=status,
            blocked_on_refs=[{"repo": "o/r", "kind": "pull_request", "number": 7}] if status == "blocked" else None,
            writer=AGENT, writer_run_ids=(uuid.uuid4(),),
        )
    assert item.status == finished and item.result == "merged"
    assert not session.committed


@pytest.mark.asyncio
async def test_a_note_on_a_finished_item_still_lands(world):  # noqa: F811
    item = _item(status="done", result="merged", claimed_by_run_id=None)
    session = world(item, None)

    await wi_service.update_work_item(
        str(item.id), user_id=str(item.user_id), note="read back: merged",
        writer=AGENT, writer_run_ids=(uuid.uuid4(),),
    )
    assert item.status == "done"
    assert item.description.startswith("read back: merged")
    assert session.committed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "run_ids"),
    [("open", (uuid.uuid4(),)), ("in_progress", None)],
    ids=["deliberate-reopen", "a-person"],
)
async def test_a_deliberate_reopen_or_a_person_still_reopens(world, status, run_ids):  # noqa: F811
    item = _item(status="done", result="merged", claimed_by_run_id=None)
    world(item, None)

    await wi_service.update_work_item(
        str(item.id), user_id=str(item.user_id), status=status,
        writer=AGENT, writer_run_ids=run_ids,
    )
    assert item.status == status
