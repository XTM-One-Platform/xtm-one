"""A later run of a work item opens, by id, the files an earlier run of it wrote.

Run A of item X generates file F (a generator, a metrics file); the item is
redispatched or reopened and run B starts in a scope of its own. B finds F in
its file context, by reference, so ``read_file_content`` and
``sandbox_put_file`` resolve it. Only runs of the SAME item, by the SAME
agent, for the SAME user are read: another item's run reads nothing of A.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.assignment_engine import (
    MAX_EARLIER_ITEM_RUN_FILES,
    load_earlier_item_run_files,
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _RecordingDB:
    """Answers the rows given, newest first, and records each statement."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.rows)


def _item_run(item_id: str, *, agent_id=None, user_id=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id or uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        parent_run_id=None,
        trigger_context={"type": "work_item", "work_item_id": item_id},
        trigger_payload={"trigger_type": "work_item", "work_item_id": item_id},
    )


def _file(name: str, run_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        filename=name,
        content_type="text/x-python",
        size=10,
        s3_object_name=f"assignments/{run_id}/{name}",
        source="agent_download",
        description=None,
        assignment_run_id=run_id,
        conversation_id=None,
        template_container_id=None,
    )


@pytest.mark.asyncio
async def test_run_b_of_an_item_finds_the_files_run_a_of_it_wrote():
    item_id = str(uuid.uuid4())
    run_a = _item_run(item_id)
    run_b = _item_run(item_id, agent_id=run_a.agent_id, user_id=run_a.user_id)
    newest, older = _file("metrics.json", run_a.id), _file("layout.py", run_a.id)
    db = _RecordingDB([newest, older])

    rows = await load_earlier_item_run_files(db, run_b)

    assert [r.filename for r in rows] == ["layout.py", "metrics.json"], "oldest first, as produced"
    where = str(db.statements[0].whereclause)
    for clause in (
        "assignment_runs.trigger_context",
        "assignment_runs.agent_id = ",
        "assignment_runs.user_id IS NOT DISTINCT FROM",
        "assignment_runs.id != ",
        "file_attachments.template_container_id IS NULL",
    ):
        assert clause in where, clause
    params = db.statements[0].compile().params
    assert item_id in params.values(), "scoped to the dispatched item"
    assert "work_item" in params.values()
    assert run_b.agent_id in params.values() and run_b.user_id in params.values()
    assert run_b.id in params.values(), "its own rows come from the run's own scope"
    assert MAX_EARLIER_ITEM_RUN_FILES in params.values()


@pytest.mark.asyncio
async def test_a_run_not_dispatched_for_an_item_reads_nothing():
    """A payload naming an item without the dispatcher's own record is no grant."""
    db = _RecordingDB([_file("x.py", uuid.uuid4())])
    forged = _item_run(str(uuid.uuid4()))
    forged.trigger_context = {"type": "manual", "work_item_id": forged.trigger_payload["work_item_id"]}
    mismatch = _item_run(str(uuid.uuid4()))
    mismatch.trigger_payload = {"trigger_type": "work_item", "work_item_id": str(uuid.uuid4())}

    assert await load_earlier_item_run_files(db, forged) == []
    assert await load_earlier_item_run_files(db, mismatch) == []
    assert db.statements == []


@pytest.mark.asyncio
async def test_a_failing_lookup_costs_the_files_not_the_run():
    class _Broken:
        async def execute(self, _statement):
            raise RuntimeError("pool exhausted")

    assert await load_earlier_item_run_files(_Broken(), _item_run(str(uuid.uuid4()))) == []


def test_the_engine_registers_them_by_reference_before_the_file_section():
    source = (Path(__file__).resolve().parents[1] / "app/services/assignment_engine.py").read_text()
    loop = source.index("# ── Inject file context into prompt")
    trigger_refs = source.index("trigger_file_refs = file_context.list_files()", loop)
    earlier = source.index("await load_earlier_item_run_files(db, run)", loop)
    section = source.index("file_context.to_prompt_section(", loop)
    assert trigger_refs < earlier < section, "listed in the section, never inlined on the user message"
