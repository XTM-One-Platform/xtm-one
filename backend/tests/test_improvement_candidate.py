"""The candidate run of an applied improvement proposal (#8256), without a database.

Pinned here: a change whose baseline ran a sandbox copy, or any change measured
on an evaluation the Concierge built, is re-evaluated on a fresh sandbox copy
of the same source, never on the production agent (its writes would happen
again for real); a baseline on a production agent (a person's #1044 request)
keeps its candidate there; when no like-for-like copy can be made (the source
is gone, the baseline ran a copy of another agent, the Concierge may not
evaluate the copy), the change goes unmeasured with the reason, which the
watch's end states; the copy a candidate runs is kept until its verdict is
read. Against PostgreSQL: ``test_improvement_handlers_postgres.py``.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.models.evaluation import EvaluationRun
from app.models.improvement_proposal import STATUS_FAILED, STATUS_WATCHING
from app.services import improvement_proposals as proposals
from app.services.improvement_proposals import candidate as candidate_module
from app.services.improvement_proposals import skill as skill_module


class _Db:
    """Rows by id; records what is added."""

    def __init__(self, *rows):
        self.rows = {row.id: row for row in rows if row is not None}
        self.added: list = []

    async def get(self, _model, row_id):
        return self.rows.get(row_id)

    async def scalar(self, _query):
        return 3  # the evaluation's entries

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = uuid.uuid4()

    @asynccontextmanager
    async def begin_nested(self):
        yield


def _world(*, baseline_on: str = "copy", concierge_built: bool = True, source_kept: bool = True):
    production = SimpleNamespace(id=uuid.uuid4(), is_sandbox=False, skill_ids=[])
    baseline_copy_agent = SimpleNamespace(id=uuid.uuid4(), is_sandbox=True)
    copy_row = SimpleNamespace(
        source_type="agent",
        source_id=production.id,
        copy_agent_id=baseline_copy_agent.id,
        source_trace_ids=["11111111-1111-1111-1111-111111111111"],
        source_run_ids=None,
    )
    evaluation = SimpleNamespace(id=uuid.uuid4(), concierge_built=concierge_built)
    baseline = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=baseline_copy_agent.id if baseline_on == "copy" else production.id,
        model_id="model-a",
        is_default_model=True,
    )
    proposal = SimpleNamespace(
        id=uuid.uuid4(), evaluation_id=evaluation.id, baseline_run_id=baseline.id, author_agent_id=uuid.uuid4()
    )
    db = _Db(evaluation, baseline, baseline_copy_agent, production if source_kept else None)
    return SimpleNamespace(
        db=db, production=production, copy_row=copy_row, evaluation=evaluation, baseline=baseline, proposal=proposal
    )


@pytest.fixture
def stubs(monkeypatch):
    """The sandbox, the launch checks and the staging, recorded."""
    from app.services import agent_version_snapshots, evaluation_engine
    from app.services.concierge import evaluations
    from app.services.concierge.sandbox import copies
    from app.services.evaluation import launch

    seen = SimpleNamespace(copies=[], checked=[], refusal=None, copy_row=None, fresh_agent_id=uuid.uuid4())

    async def sandbox_copy_of(_db, *, agent_id=None, flow_id=None):
        row = seen.copy_row
        return row if row is not None and row.copy_agent_id == agent_id else None

    async def create_sandbox_copy(_db, **kwargs):
        seen.copies.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4(), copy_agent_id=seen.fresh_agent_id)

    async def subject_refusal(_db, kind, subject_id):
        assert (kind, subject_id) == ("agent", seen.fresh_agent_id)
        return seen.refusal

    async def check_launchable(_db, _evaluation):
        return None

    async def check_runs_served(_db, _evaluation, runs):
        seen.checked.extend(runs)

    async def snapshot(_db, _agent_id):
        return 1, {}, {}, {}

    async def stage_execution(_db, _evaluation_id, _reason=None):
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(copies, "sandbox_copy_of", sandbox_copy_of)
    monkeypatch.setattr(copies, "create_sandbox_copy", create_sandbox_copy)
    monkeypatch.setattr(evaluations, "evaluation_subject_refusal", subject_refusal)
    monkeypatch.setattr(launch, "check_launchable", check_launchable)
    monkeypatch.setattr(launch, "check_runs_served", check_runs_served)
    monkeypatch.setattr(agent_version_snapshots, "resolve_agent_version_snapshot", snapshot)
    monkeypatch.setattr(evaluation_engine, "stage_evaluation_execution", stage_execution)
    return seen


async def _queue(world, agent):
    queued = await candidate_module.queue_candidate(
        world.db, world.proposal, agent, noun="change", trigger_reason="improvement_proposal"
    )
    assert queued is not None
    return await queued()


def _runs(db) -> list[EvaluationRun]:
    return [row for row in db.added if isinstance(row, EvaluationRun)]


async def test_a_baseline_on_a_sandbox_copy_is_matched_by_a_fresh_copy_of_its_source(stubs):
    world = _world()
    stubs.copy_row = world.copy_row

    staged = await _queue(world, world.production)

    (run,) = _runs(world.db)
    assert staged == candidate_module.StagedCandidate(run.id)
    assert run.agent_id == stubs.fresh_agent_id
    assert run.agent_id not in (world.production.id, world.baseline.agent_id)
    assert (run.model_id, run.is_default_model) == ("model-a", True)
    (made,) = stubs.copies
    assert (made["source_type"], made["source_id"]) == ("agent", world.production.id)
    # The fresh copy replays what the baseline's copy replayed.
    assert made["source_trace_ids"] == world.copy_row.source_trace_ids
    assert str(world.proposal.id) in made["created_for"]


async def test_a_baseline_on_a_production_agent_keeps_its_candidate_there(stubs):
    """A person's #1044 request: an evaluation of their own, its baseline on the agent."""
    world = _world(baseline_on="production", concierge_built=False)

    staged = await _queue(world, world.production)

    (run,) = _runs(world.db)
    assert (staged.run_id, staged.unmeasured, run.agent_id) == (run.id, None, world.production.id)
    assert stubs.copies == []


async def test_a_concierge_evaluation_never_runs_a_production_agent(stubs):
    world = _world(baseline_on="production", concierge_built=True)

    staged = await _queue(world, world.production)

    assert staged.run_id is None
    assert "no sandbox copy" in staged.unmeasured
    assert (_runs(world.db), stubs.copies, stubs.checked) == ([], [], [])


async def test_a_gone_source_leaves_the_change_unmeasured(stubs):
    world = _world(source_kept=False)
    stubs.copy_row = world.copy_row

    staged = await _queue(world, None)

    assert staged.run_id is None
    assert "is gone" in staged.unmeasured
    assert (_runs(world.db), stubs.copies, stubs.checked) == ([], [], [])


async def test_a_baseline_on_a_copy_of_another_agent_leaves_the_change_unmeasured(stubs):
    world = _world()
    stubs.copy_row = world.copy_row
    other = SimpleNamespace(id=uuid.uuid4(), is_sandbox=False)

    staged = await _queue(world, other)

    assert staged.run_id is None
    assert "another agent" in staged.unmeasured
    assert _runs(world.db) == []


async def test_a_copy_the_concierge_may_not_evaluate_leaves_the_change_unmeasured(stubs):
    world = _world()
    stubs.copy_row = world.copy_row
    stubs.refusal = "the source is outside the supervision scope"

    staged = await _queue(world, world.production)

    assert staged.run_id is None
    assert "outside the supervision scope" in staged.unmeasured
    assert _runs(world.db) == []


async def test_a_gone_agent_after_a_production_baseline_is_refused(stubs):
    world = _world(baseline_on="production", concierge_built=False)

    with pytest.raises(proposals.ProposalError) as caught:
        await candidate_module.queue_candidate(world.db, world.proposal, None, noun="change", trigger_reason="x")

    assert caught.value.status_code == 409


# ---------------------------------------------------------------------------
# The skill handler's measured agent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sandboxed", "uses_skill", "outcome"),
    [(True, True, "source"), (True, False, None), (False, True, "source"), (False, False, 409)],
    ids=["copy-uses", "copy-dropped-skill", "production-uses", "production-dropped-skill"],
)
async def test_the_measured_agent_of_a_skill_change(monkeypatch, sandboxed, uses_skill, outcome):
    skill = SimpleNamespace(id=uuid.uuid4())
    source = SimpleNamespace(id=uuid.uuid4(), skill_ids=[str(skill.id)] if uses_skill else [])

    async def baseline_of(_db, _proposal, _noun):
        return SimpleNamespace(agent_id=uuid.uuid4())

    async def baseline_subject(_db, _proposal, _baseline):
        return candidate_module.BaselineSubject(agent=source, copy=None, sandboxed=sandboxed)

    monkeypatch.setattr(skill_module, "baseline_of", baseline_of)
    monkeypatch.setattr(skill_module, "baseline_subject", baseline_subject)

    if outcome == 409:
        with pytest.raises(proposals.ProposalError) as caught:
            await skill_module._measured_agent(None, SimpleNamespace(), skill)
        assert caught.value.status_code == 409
    else:
        measured = await skill_module._measured_agent(None, SimpleNamespace(), skill)
        assert measured is (source if outcome == "source" else None)


# ---------------------------------------------------------------------------
# The unmeasured reason, at the end of the watch; the copy's retention
# ---------------------------------------------------------------------------


class _Handler:
    proposal_type = "skill"
    target_type = "skill"
    noun = "skill change"
    subject = "skill"
    owns_watch = False

    async def load_target(self, _db, _target_id, *, lock):
        return SimpleNamespace(id=uuid.uuid4())


async def test_the_end_of_an_unmeasured_watch_states_why(monkeypatch):
    from app.services.improvement_proposals import registry

    monkeypatch.setattr(registry, "_HANDLERS", {"skill": _Handler()})
    monkeypatch.setattr(registry, "_WATCHERS", {})
    monkeypatch.setattr(registry, "_loaded", True)
    note = f"{proposals.lifecycle.UNMEASURED_PREFIX}the agent this change was measured on is gone."
    proposal = SimpleNamespace(
        proposal_type="skill",
        status=STATUS_WATCHING,
        target_id=uuid.uuid4(),
        candidate_run_id=None,
        verdict=None,
        delta=None,
        watch_until=None,
        outcome_note=note,
        settled_at=None,
    )

    result = await proposals.settle(None, proposal)  # type: ignore[arg-type]

    assert (result.changed, proposal.status) == (True, STATUS_FAILED)
    assert proposal.outcome_note == (
        f"Nothing measured this change during its watch, so there is no verdict. {note} "
        "The change stays active, unmeasured."
    )


def test_the_copy_a_candidate_runs_is_kept_until_its_verdict_is_read():
    from app.services.concierge.sandbox import copies

    assert candidate_module.copies_awaiting_a_verdict in copies._reference_checks
