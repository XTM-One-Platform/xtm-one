"""Status and claim events from the work item writers outside the service's own.

``work_item.publish_transition`` is the one helper the requeue, the dispatch
release, the dependency reopen, the scheduled dispatcher and the pull request
workflow call after their commit: it reads the row that landed and raises the
same ``work_item.*`` events ``update_work_item`` / ``claim_work_item`` raise.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import work_item as wi

AGENT = uuid.uuid4()
OTHER = uuid.uuid4()


def _item(**extra) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "correlation_key": "dev:x",
        "title": "Do x",
        "status": "open",
        "priority": "high",
        "owner_agent_id": OTHER,
        "created_by_agent_id": AGENT,
        "claimed_by_agent_id": None,
        "claimed_by_run_id": None,
        "user_id": None,
        "is_company_managed": True,
        "workspace_id": None,
    }
    base.update(extra)
    return SimpleNamespace(**base)


@pytest.fixture
def landed(monkeypatch):
    """The row ``publish_transition`` reads back, and the events it publishes."""
    import app.core.database as database

    state = SimpleNamespace(row=None, published=[])

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, _model, _id):
            return state.row

    async def publish(events):
        state.published.extend(name for name, _data in events)
        state.data = [data for _name, data in events]

    monkeypatch.setattr(database, "async_session", _Session)
    monkeypatch.setattr(wi, "_publish", publish)
    return state


@pytest.mark.asyncio
async def test_requeue_raises_status_changed_and_the_note(landed):
    landed.row = _item(status="open")
    await wi.publish_transition(landed.row.id, previous_status="blocked", note="put back in the queue")
    assert landed.published == ["work_item.note_added", "work_item.status_changed"]


@pytest.mark.asyncio
async def test_a_terminal_status_also_raises_completed(landed):
    landed.row = _item(status="done")
    await wi.publish_transition(landed.row.id, previous_status="claimed")
    assert landed.published == ["work_item.status_changed", "work_item.completed"]


@pytest.mark.asyncio
async def test_a_claim_raises_claimed_and_status_changed(landed):
    landed.row = _item(status="claimed", claimed_by_agent_id=OTHER)
    await wi.publish_transition(landed.row.id, previous_status="open", claimed_by_agent_id=OTHER)
    assert landed.published == ["work_item.claimed", "work_item.status_changed"]


@pytest.mark.asyncio
async def test_a_scheduled_run_names_itself_as_the_writer(landed):
    # A scheduled item settled by its own run: the events name that run's agent,
    # so the agent's assignment ignoring its own events is not woken by them.
    run_id = uuid.uuid4()
    landed.row = _item(status="done")
    await wi.publish_transition(landed.row.id, previous_status="in_progress", actor_agent_id=OTHER, actor_run_id=run_id)
    assert landed.published == ["work_item.status_changed", "work_item.completed"]
    assert {d["actor_agent_id"] for d in landed.data} == {str(OTHER)}
    assert {d["actor_run_id"] for d in landed.data} == {str(run_id)}


@pytest.mark.asyncio
async def test_a_persons_answer_marks_its_status_change_answered(landed):
    # A decision listener hears the answer whatever the item's key, never another release.
    landed.row = _item(status="open", correlation_key="ctem:f1:version-read:abc")
    await wi.publish_transition(landed.row.id, previous_status="blocked", note="ANSWERED: yes", answered=True)
    assert landed.published == ["work_item.note_added", "work_item.status_changed"]
    by_name = dict(zip(landed.published, landed.data, strict=True))
    assert by_name["work_item.status_changed"]["answered"] is True
    assert "answered" not in by_name["work_item.note_added"]


@pytest.mark.asyncio
async def test_a_release_is_not_an_answer(landed):
    landed.row = _item(status="open")
    await wi.publish_transition(landed.row.id, previous_status="blocked")
    assert "answered" not in landed.data[0]


@pytest.mark.asyncio
async def test_the_scheduled_wrapper_forwards_the_writer(monkeypatch):
    from app.services import scheduled_work_item_runner as runner

    seen = {}

    async def publish_transition(work_item_id, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(wi, "publish_transition", publish_transition)
    run_id = uuid.uuid4()
    await runner.publish_transition(uuid.uuid4(), previous_status="claimed", actor_agent_id=OTHER, actor_run_id=run_id)
    assert seen == {"previous_status": "claimed", "actor_agent_id": OTHER, "actor_run_id": run_id}


class _Rows:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def all(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class _SettleDb:
    """The stale scheduled items first, then the run row each one names."""

    def __init__(self, items, runs):
        self.answers = [_Rows(items), *(_Rows(r) for r in runs)]

    async def execute(self, _stmt):
        return self.answers.pop(0)

    async def commit(self):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", [uuid.uuid4(), None], ids=["run-row-gone", "no-run-id"])
async def test_a_settlement_without_its_run_names_no_writer(monkeypatch, run_id):
    # The run is gone: the scheduler alone settles the item, so an assignment of
    # the owner agent that ignores its own events still hears about it.
    from datetime import UTC, datetime

    from app.services import scheduled_work_item_runner as runner

    async def block(db, item, run, *, code, reason, now):
        item.status = "blocked"

    monkeypatch.setattr(runner, "_block", block)
    item = _item(status="in_progress", run_id=run_id)
    runs = [None] if run_id else []
    settled = await runner._settle_from_runs(_SettleDb([item], runs), datetime.now(UTC))
    assert settled == [(item.id, "in_progress", None, None)]


@pytest.mark.asyncio
async def test_a_settlement_by_its_ended_run_names_that_run(monkeypatch):
    from datetime import UTC, datetime

    from app.services import scheduled_work_item_runner as runner

    run = SimpleNamespace(id=uuid.uuid4(), status="completed", result_summary="Done.")
    item = _item(status="in_progress", run_id=run.id)
    settled = await runner._settle_from_runs(_SettleDb([item], [run]), datetime.now(UTC))
    assert settled == [(item.id, "in_progress", OTHER, run.id)] and item.status == "done"


@pytest.mark.asyncio
async def test_a_platform_only_write_names_no_writer(landed):
    landed.row = _item(status="blocked")
    await wi.publish_transition(landed.row.id, previous_status="open")
    assert {d["actor_agent_id"] for d in landed.data} == {None}


@pytest.mark.asyncio
async def test_no_change_raises_nothing(landed):
    landed.row = _item(status="open")
    await wi.publish_transition(landed.row.id, previous_status="open")
    assert landed.published == []


@pytest.mark.asyncio
async def test_a_missing_item_or_id_raises_nothing(landed):
    await wi.publish_transition(uuid.uuid4(), previous_status="claimed")
    await wi.publish_transition(None, previous_status="claimed")
    await wi.publish_transition("not-a-uuid", previous_status="claimed")
    assert landed.published == []


@pytest.mark.asyncio
async def test_publish_transition_never_raises(monkeypatch):
    import app.core.database as database

    class _Broken:
        async def __aenter__(self):
            raise RuntimeError("database gone")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(database, "async_session", _Broken)
    await wi.publish_transition(uuid.uuid4(), previous_status="claimed")
