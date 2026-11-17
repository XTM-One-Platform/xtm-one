"""The Deep Investigation bounded loop.

The parts of an agentic loop most likely to be wrong are the ones a live
network dependency makes untestable: the iteration budget, the skip path, the
circular-lead guard and the error taxonomy.  The orchestrator takes an injected
querier precisely so they can be tested, and this suite is why that indirection
exists.
"""

import asyncio
import uuid

import pytest

from app.services import investigation_state as state
from app.services.investigation_orchestrator import (
    MAX_LEAD_STEPS,
    DeepInvestigationOrchestrator,
    Evidence,
    SourceOutcome,
)
from app.services.investigation_packs import SourceDefinition, SourcePack


class _NotAnException(BaseException):
    """Something that is not an `Exception`, to prove the guard is wide enough.

    `except Exception` would miss this, and would miss `CancelledError` with
    it — which is the realistic one, since a chat turn ends that way every time
    the socket goes away mid-run.
    """


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _StubSession:
    """Emulates the two database behaviours the loop actually depends on.

    1. **Primary keys are assigned on flush** — the models default ``id`` to
       ``uuid.uuid4`` at insert time, and the loop reads ``step.id`` to record
       lead provenance.
    2. **The max-position query** issued by ``append_lead_step`` — answered
       from the steps this stub has recorded, so appended leads get contiguous
       positions instead of all landing on the same one.

    A real session is exercised by the migration / fresh-install suites; using
    one here would buy nothing for loop logic and cost a Postgres dependency.
    """

    def __init__(self):
        self.objects: list[object] = []
        self.commits = 0
        #: Snapshot of step statuses at each commit — lets a test assert what a
        #: concurrent reader would have seen partway through the run.
        self.commit_snapshots: list[list[str]] = []
        self.rollbacks = 0

    def add(self, obj):
        self.objects.append(obj)

    def _steps(self):
        return [o for o in self.objects if hasattr(o, "position")]

    async def flush(self):
        for obj in self.objects:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        await self.flush()
        self.commits += 1
        self.commit_snapshots.append([s.status for s in self._steps()])

    async def execute(self, _stmt):
        positions = [s.position for s in self._steps()]
        return _Result(max(positions) if positions else None)

    async def rollback(self):
        # A real session has one, and `_finalize` clears a half-applied flush
        # before it writes. Nothing to undo here: this stub never buffers.
        self.rollbacks += 1

    async def get(self, _model, pk):
        for obj in self.objects:
            # A run, not a step: steps carry a `position`.
            if getattr(obj, "id", None) == pk and not hasattr(obj, "position"):
                return obj
        return None

    def _run_row(self):
        rows = [o for o in self.objects if hasattr(o, "subject_value")]
        assert len(rows) == 1, f"expected exactly one run, got {len(rows)}"
        return rows[0]


def _source(name: str, url: str | None = None, types=("unknown",)) -> SourceDefinition:
    return SourceDefinition(
        name=name,
        url_template=url or f"https://{name.lower()}.test/{{value}}",
        subject_types=types,
    )


def _pack(*sources: SourceDefinition, slug="test-pack", max_iterations=10) -> SourcePack:
    return SourcePack(
        slug=slug,
        label=slug,
        description="",
        subject_types=("unknown",),
        sources=sources,
        max_iterations=max_iterations,
    )


def _install(monkeypatch, pack: SourcePack) -> None:
    """Point the pack registry at a test pack, for both lookup paths."""
    from app.services import investigation_packs as packs

    monkeypatch.setattr(packs, "BUILTIN_PACKS", {pack.slug: pack})
    monkeypatch.setattr(packs, "DEFAULT_PACK_SLUG", pack.slug)


def _querier(outcomes: dict[str, SourceOutcome], calls: list[str] | None = None):
    async def _query(source, _subject_value, url):
        if calls is not None:
            calls.append(url)
        return outcomes.get(source.name, SourceOutcome(findings_count=1))

    return _query


# ---------------------------------------------------------------------------
# Happy path and derived statuses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_source_becomes_a_step_with_a_derived_status(monkeypatch):
    _install(monkeypatch, _pack(_source("Found"), _source("Nothing"), _source("Down")))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(
        db,
        _querier(
            {
                "Found": SourceOutcome(
                    findings_count=3,
                    evidence=(Evidence(kind="url", label="u", href="u"),),
                ),
                "Nothing": SourceOutcome(findings_count=0),
                "Down": SourceOutcome(error_type="timeout", detail="no response"),
            }
        ),
    )
    result = await orch.run(subject_value="APT29")

    statuses = {s.source_name: s.status for s in result.steps}
    assert statuses == {"Found": "completed", "Nothing": "empty", "Down": "error"}
    assert result.investigation.status == "completed"
    assert result.summary.all_succeeded is False
    assert result.summary.failed == 1


@pytest.mark.asyncio
async def test_the_query_stored_on_a_step_is_the_url_actually_fetched(monkeypatch):
    _install(monkeypatch, _pack(_source("S", "https://s.test/lookup?q={value}")))
    db = _StubSession()
    calls: list[str] = []
    orch = DeepInvestigationOrchestrator(db, _querier({}, calls))
    result = await orch.run(subject_value="8.8.8.8", subject_type="unknown")

    assert calls == ["https://s.test/lookup?q=8.8.8.8"]
    assert result.steps[0].query == "https://s.test/lookup?q=8.8.8.8"


@pytest.mark.asyncio
async def test_this_slice_produces_no_confidence_at_all(monkeypatch):
    """The "no unearned metrics" invariant, asserted on the real pipeline."""
    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(db, _querier({}))
    result = await orch.run(subject_value="APT29")

    assert all(s.confidence is None for s in result.steps)
    assert result.summary.confidence is None


@pytest.mark.asyncio
async def test_the_skeleton_is_committed_before_any_source_is_queried(monkeypatch):
    """The progressive skeleton must be readable before the work starts.

    Without this, a block opened at the top of a run would render empty and the
    "here is the shape of the work" promise would be a lie.
    """
    _install(monkeypatch, _pack(_source("A"), _source("B"), _source("C")))
    db = _StubSession()
    calls: list[str] = []

    async def _query(source, _subject, url):
        # At the moment the first source is queried, the skeleton must already
        # have been committed with every step pending.
        assert db.commit_snapshots, "no commit happened before the first query"
        assert db.commit_snapshots[0] == ["pending", "pending", "pending"]
        calls.append(url)
        return SourceOutcome(findings_count=1)

    await DeepInvestigationOrchestrator(db, _query).run(subject_value="APT29")
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_state_is_committed_as_the_run_progresses(monkeypatch):
    """A crash mid-run must not lose the steps already completed."""
    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()
    await DeepInvestigationOrchestrator(db, _querier({})).run(subject_value="x")
    # Skeleton + per-step active/result + close: far more than one commit.
    assert db.commits >= 5
    assert db.commit_snapshots[0] == ["pending", "pending"]


@pytest.mark.asyncio
async def test_opting_out_of_per_step_commits_leaves_the_transaction_to_the_caller(
    monkeypatch,
):
    _install(monkeypatch, _pack(_source("A")))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(db, _querier({}), commit_each_step=False)
    await orch.run(subject_value="x")
    assert db.commits == 0


# ---------------------------------------------------------------------------
# Iteration budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_exhaustion_skips_the_rest_with_a_reason(monkeypatch):
    _install(monkeypatch, _pack(*[_source(f"S{i}") for i in range(5)]))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(db, _querier({}))
    result = await orch.run(subject_value="APT29", max_iterations=2)

    statuses = [s.status for s in result.steps]
    assert statuses == ["completed", "completed", "skipped", "skipped", "skipped"]
    assert all("budget spent" in (s.status_detail or "") for s in result.steps if s.status == "skipped")
    assert "budget spent" in (result.investigation.end_reason or "")


@pytest.mark.asyncio
async def test_a_step_refused_by_the_budget_is_skipped_not_dropped(monkeypatch):
    """The step the budget stopped on must appear as skipped, not stay pending.

    This is the off-by-one the loop's cursor guards: the budget is checked
    before the cursor advances, so the refused step stays in the unreached
    slice.
    """
    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()
    result = await DeepInvestigationOrchestrator(db, _querier({})).run(subject_value="x", max_iterations=1)
    assert [s.status for s in result.steps] == ["completed", "skipped"]
    assert not any(s.status == "pending" for s in result.steps)


@pytest.mark.asyncio
async def test_budget_defaults_to_the_packs_own_ceiling(monkeypatch):
    _install(monkeypatch, _pack(*[_source(f"S{i}") for i in range(4)], max_iterations=2))
    db = _StubSession()
    result = await DeepInvestigationOrchestrator(db, _querier({})).run(subject_value="x")
    assert result.investigation.max_iterations == 2
    assert [s.status for s in result.steps].count("skipped") == 2


@pytest.mark.asyncio
async def test_each_selected_pack_brings_its_own_allowance(monkeypatch):
    """Two packs, two allowances — not the larger of the two.

    This is what the picker made reachable. Under `max`, ticking a second pack
    planned its sources and then marked them `skipped` for a spent budget,
    because the first pack's steps had already consumed the single shared
    allowance. The analyst asked for more coverage and got a run that showed
    them less, with no step saying the selection was the reason.
    """
    from app.services import investigation_packs as packs_module

    a = _pack(_source("A1"), _source("A2"), slug="pack-a", max_iterations=2)
    b = _pack(_source("B1"), _source("B2"), slug="pack-b", max_iterations=2)
    monkeypatch.setattr(packs_module, "BUILTIN_PACKS", {"pack-a": a, "pack-b": b})

    db = _StubSession()
    result = await DeepInvestigationOrchestrator(db, _querier({})).run(
        subject_value="x", source_pack_slugs=["pack-a", "pack-b"], user_id=None
    )
    assert result.investigation.max_iterations == 4
    assert [s.status for s in result.steps] == ["completed"] * 4


@pytest.mark.asyncio
async def test_a_built_in_selection_resolves_without_touching_the_session(monkeypatch):
    """The chat path has no picker and always selects the recommendation.

    Resolving the install-specific packs costs two queries and needs the
    caller loaded. A selection made only of built-in slugs cannot be changed
    by either, so it must not pay for them — this path was database-free
    before those packs existed, and every stub session in this file relies on
    it still being so.
    """
    from app.services import investigation_packs as packs_module

    a = _pack(_source("A"), slug="pack-a")
    monkeypatch.setattr(packs_module, "BUILTIN_PACKS", {"pack-a": a})

    class _NoLookupSession(_StubSession):
        async def get(self, *_a, **_k):  # pragma: no cover - must not be called
            raise AssertionError("a built-in selection loaded the caller")

        async def execute(self, *_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("a built-in selection queried the database")

    result = await DeepInvestigationOrchestrator(_NoLookupSession(), _querier({})).run(
        subject_value="x", source_pack_slugs=["pack-a"], user_id=uuid.uuid4()
    )
    assert [s.status for s in result.steps] == ["completed"]


@pytest.mark.asyncio
async def test_a_lone_pack_that_asks_to_sample_still_samples(monkeypatch):
    """A ceiling below the source count is a decision, not an oversight.

    Guarded because the obvious fix for the bug above — raise the budget to
    cover the plan — would also overrule a pack that deliberately declares
    fewer iterations than it carries sources.
    """
    _install(monkeypatch, _pack(*[_source(f"S{i}") for i in range(4)], max_iterations=2))
    db = _StubSession()
    result = await DeepInvestigationOrchestrator(db, _querier({})).run(subject_value="x")
    assert result.investigation.max_iterations == 2


@pytest.mark.asyncio
async def test_a_completed_run_records_why_it_ended(monkeypatch):
    _install(monkeypatch, _pack(_source("A")))
    db = _StubSession()
    result = await DeepInvestigationOrchestrator(db, _querier({})).run(subject_value="x")
    assert result.investigation.end_reason_code == "run.all_sources_queried"


# ---------------------------------------------------------------------------
# Multi-hop leads and the circular guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lead_is_appended_queried_and_attributed(monkeypatch):
    lead = _source("Lead")
    _install(monkeypatch, _pack(_source("Seed")))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(
        db,
        _querier({"Seed": SourceOutcome(findings_count=1, leads=(lead,))}),
    )
    result = await orch.run(subject_value="APT29")

    assert [s.source_name for s in result.steps] == ["Seed", "Lead"]
    seed, followed = result.steps
    assert followed.status == "completed"
    assert followed.lead_from_step_id == seed.id
    assert followed.iteration == 2  # queried on the second iteration
    assert seed.iteration == 1


@pytest.mark.asyncio
async def test_a_planned_step_has_no_lead_provenance_but_a_lead_does(monkeypatch):
    lead = _source("Lead")
    _install(monkeypatch, _pack(_source("Seed")))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(db, _querier({"Seed": SourceOutcome(findings_count=1, leads=(lead,))}))
    result = await orch.run(subject_value="x")
    assert result.steps[0].lead_from_step_id is None
    assert result.steps[1].lead_from_step_id is not None


@pytest.mark.asyncio
async def test_two_sources_yielding_the_same_lead_queue_it_once(monkeypatch):
    lead = _source("Lead")
    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(
        db,
        _querier(
            {
                "A": SourceOutcome(findings_count=1, leads=(lead,)),
                "B": SourceOutcome(findings_count=1, leads=(lead,)),
            }
        ),
    )
    result = await orch.run(subject_value="x")
    assert [s.source_name for s in result.steps].count("Lead") == 1


@pytest.mark.asyncio
async def test_a_lead_pointing_at_an_already_planned_source_is_never_enqueued(
    monkeypatch,
):
    """No duplicate step, and no second fetch of the same URL.

    ``A`` yields a lead pointing at the same URL as the planned source ``B``.
    Dropping it at enqueue time (rather than creating a step and skipping it
    later) is what keeps the block free of a second row for one source.
    """
    shared_url = "https://shared.test/{value}"
    lead = _source("LeadToB", shared_url)
    _install(monkeypatch, _pack(_source("A"), _source("B", shared_url)))
    db = _StubSession()
    calls: list[str] = []
    orch = DeepInvestigationOrchestrator(
        db,
        _querier({"A": SourceOutcome(findings_count=1, leads=(lead,))}, calls),
    )
    result = await orch.run(subject_value="x")

    assert [s.source_name for s in result.steps] == ["A", "B"]
    assert calls.count("https://shared.test/x") == 1


@pytest.mark.asyncio
async def test_a_lead_the_budget_never_reaches_keeps_its_discovery_iteration(
    monkeypatch,
):
    """ "Leads not followed" needs to say when the lead came up."""
    lead = _source("Unreached")
    _install(monkeypatch, _pack(_source("Seed")))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(db, _querier({"Seed": SourceOutcome(findings_count=1, leads=(lead,))}))
    result = await orch.run(subject_value="x", max_iterations=1)

    unreached = result.steps[1]
    assert unreached.status == "skipped"
    assert unreached.iteration == 1
    assert unreached.detail_code == "run.budget_spent"


@pytest.mark.asyncio
async def test_a_self_referential_lead_cannot_spin_the_loop(monkeypatch):
    """A source whose lead is itself: the guard must stop it immediately."""
    selfie = _source("Loop")
    _install(monkeypatch, _pack(selfie))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(db, _querier({"Loop": SourceOutcome(findings_count=1, leads=(selfie,))}))
    result = await orch.run(subject_value="x", max_iterations=99)
    assert len(result.steps) == 1


@pytest.mark.asyncio
async def test_lead_steps_are_capped_independently_of_the_budget(monkeypatch):
    """A source reporting hundreds of leads must not flood the block.

    The budget bounds the work; this cap bounds the plan, so the block stays
    readable even though only the first few leads would ever be queried.
    """
    many = tuple(_source(f"L{i}", f"https://l{i}.test/{{value}}") for i in range(80))
    _install(monkeypatch, _pack(_source("Seed")))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(db, _querier({"Seed": SourceOutcome(findings_count=1, leads=many)}))
    result = await orch.run(subject_value="x", max_iterations=1)
    assert len(result.steps) == 1 + MAX_LEAD_STEPS


# ---------------------------------------------------------------------------
# Failure containment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_querier_that_raises_fails_only_its_own_step(monkeypatch):
    """One broken source must not abort an otherwise useful investigation."""
    _install(monkeypatch, _pack(_source("Boom"), _source("Fine")))
    db = _StubSession()

    async def _query(source, _subject, _url):
        if source.name == "Boom":
            raise RuntimeError("provider client blew up")
        return SourceOutcome(findings_count=2)

    result = await DeepInvestigationOrchestrator(db, _query).run(subject_value="x")

    boom, fine = result.steps
    assert boom.status == "error"
    assert boom.error_type == "invalid_response"
    assert "provider client blew up" in (boom.status_detail or "")
    assert fine.status == "completed"
    assert result.investigation.status == "completed"


@pytest.mark.asyncio
async def test_an_all_failing_run_never_claims_success(monkeypatch):
    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(
        db,
        _querier({name: SourceOutcome(error_type="unreachable") for name in "AB"}),
    )
    result = await orch.run(subject_value="x")
    assert result.summary.all_succeeded is False
    assert result.summary.failed == 2
    assert result.summary.confidence is None


@pytest.mark.asyncio
async def test_a_subject_no_source_covers_says_so_instead_of_looking_fine(
    monkeypatch,
):
    """A zero-step "completed" run would read as a successful investigation.

    `web-general` has no keyless endpoint for file hashes, so this is a real
    state the block has to render — with the reason, not as a silent success.
    """
    pack = SourcePack(
        slug="narrow",
        label="narrow",
        description="",
        subject_types=("unknown",),
        sources=(_source("OnlyForIps", types=("ip",)),),
    )
    _install(monkeypatch, pack)
    db = _StubSession()
    calls: list[str] = []
    result = await DeepInvestigationOrchestrator(db, _querier({}, calls)).run(
        subject_value="APT29", subject_type="unknown"
    )

    assert result.steps == []
    assert calls == []
    assert result.summary.all_succeeded is False
    assert "no source in the selected packs covers" in (result.investigation.end_reason or "")
    assert "'unknown'" in (result.investigation.end_reason or "")


@pytest.mark.asyncio
async def test_an_explicit_pack_selection_is_honoured_as_given(monkeypatch):
    _install(monkeypatch, _pack(_source("A"), slug="chosen"))
    db = _StubSession()
    result = await DeepInvestigationOrchestrator(db, _querier({})).run(subject_value="x", source_pack_slugs=["chosen"])
    assert result.investigation.source_pack_slugs == ["chosen"]


@pytest.mark.asyncio
async def test_an_unknown_pack_selection_is_refused_before_anything_is_created(
    monkeypatch,
):
    from app.services.investigation_packs import UnknownSourcePackError

    _install(monkeypatch, _pack(_source("A")))
    db = _StubSession()
    with pytest.raises(UnknownSourcePackError):
        await DeepInvestigationOrchestrator(db, _querier({})).run(subject_value="x", source_pack_slugs=["nope"])
    assert db.objects == []


# ---------------------------------------------------------------------------
# WS frames
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frames_are_emitted_in_run_order(monkeypatch):
    """planned → (active, result)* → complete, with nothing out of sequence."""
    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()
    frames: list[dict] = []

    async def _on_event(frame):
        frames.append(frame)

    await DeepInvestigationOrchestrator(db, _querier({}), on_event=_on_event).run(subject_value="x")

    assert [f["status"] for f in frames] == [
        "investigation_planned",
        "investigation_step",
        "investigation_step",
        "investigation_step",
        "investigation_step",
        "investigation_complete",
    ]
    assert [f.get("step_status") for f in frames[1:5]] == [
        "active",
        "completed",
        "active",
        "completed",
    ]


@pytest.mark.asyncio
async def test_the_planned_frame_precedes_the_first_query(monkeypatch):
    _install(monkeypatch, _pack(_source("A")))
    db = _StubSession()
    frames: list[dict] = []

    async def _on_event(frame):
        frames.append(frame)

    async def _query(_source, _subject, _url):
        assert [f["status"] for f in frames] == ["investigation_planned"]
        return SourceOutcome(findings_count=1)

    await DeepInvestigationOrchestrator(db, _query, on_event=_on_event).run(subject_value="x")


@pytest.mark.asyncio
async def test_a_frame_is_never_emitted_before_its_commit(monkeypatch):
    """The "frames are advisory, the record is authoritative" contract.

    A frame that outran its commit could reach a client which then reads the
    record and sees OLDER state than the frame described.
    """
    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()
    seen: list[tuple[str, int]] = []

    async def _on_event(frame):
        seen.append((frame["status"], db.commits))

    await DeepInvestigationOrchestrator(db, _querier({}), on_event=_on_event).run(subject_value="x")

    # Every frame was emitted after at least as many commits as the frame
    # before it, and never with zero commits behind it.
    counts = [c for _status, c in seen]
    assert counts == sorted(counts)
    assert all(c > 0 for c in counts)


@pytest.mark.asyncio
async def test_a_broken_event_sink_does_not_abort_the_run(monkeypatch):
    """A dropped socket is a cosmetic problem, not a reason to lose the run."""
    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()

    async def _on_event(_frame):
        raise RuntimeError("websocket closed")

    result = await DeepInvestigationOrchestrator(db, _querier({}), on_event=_on_event).run(subject_value="x")

    assert result.investigation.status == "completed"
    assert [s.status for s in result.steps] == ["completed", "completed"]


@pytest.mark.asyncio
async def test_a_discovered_lead_is_announced_as_its_own_frame(monkeypatch):
    """The block appends it at `position` on arrival."""
    lead = _source("Lead")
    _install(monkeypatch, _pack(_source("Seed")))
    db = _StubSession()
    frames: list[dict] = []

    async def _on_event(frame):
        frames.append(frame)

    await DeepInvestigationOrchestrator(
        db,
        _querier({"Seed": SourceOutcome(findings_count=1, leads=(lead,))}),
        on_event=_on_event,
    ).run(subject_value="x")

    pending = [f for f in frames if f["status"] == "investigation_step" and f.get("step_status") == "pending"]
    assert len(pending) == 1
    assert pending[0]["source_name"] == "Lead"
    assert pending[0]["lead_from_step_id"] is not None


@pytest.mark.asyncio
async def test_a_skipped_step_is_announced_too(monkeypatch):
    """An unreached source must not be left looking pending on the client."""
    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()
    frames: list[dict] = []

    async def _on_event(frame):
        frames.append(frame)

    await DeepInvestigationOrchestrator(db, _querier({}), on_event=_on_event).run(subject_value="x", max_iterations=1)

    skipped = [f for f in frames if f.get("step_status") == "skipped"]
    assert len(skipped) == 1
    assert "budget spent" in (skipped[0]["status_detail"] or "")


@pytest.mark.asyncio
async def test_the_revision_rises_across_frames(monkeypatch):
    """What lets a client discard a duplicate or out-of-order frame."""
    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()
    revisions: list[int] = []

    async def _on_event(frame):
        revisions.append(frame["revision"])

    await DeepInvestigationOrchestrator(db, _querier({}), on_event=_on_event).run(subject_value="x")

    assert revisions == sorted(revisions)
    assert revisions[0] < revisions[-1]


@pytest.mark.asyncio
async def test_a_run_with_no_applicable_source_still_announces_itself(monkeypatch):
    """An empty plan is a state the block renders, so it must be sent."""
    pack = SourcePack(
        slug="narrow",
        label="narrow",
        description="",
        subject_types=("unknown",),
        sources=(_source("OnlyForIps", types=("ip",)),),
    )
    _install(monkeypatch, pack)
    db = _StubSession()
    frames: list[dict] = []

    async def _on_event(frame):
        frames.append(frame)

    await DeepInvestigationOrchestrator(db, _querier({}), on_event=_on_event).run(
        subject_value="APT29", subject_type="unknown"
    )

    assert [f["status"] for f in frames] == [
        "investigation_planned",
        "investigation_complete",
    ]
    assert frames[0]["steps"] == []
    assert "no source in the selected packs covers" in (frames[1]["end_reason"] or "")


# ---------------------------------------------------------------------------
# Cancellation and guaranteed finalization (review follow-up)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_set_cancel_event_stops_the_run_between_sources(monkeypatch):
    """Checked between sources: a fetch is one awaited call, so interrupting
    mid-fetch would leave the step `active` with no result."""

    _install(monkeypatch, _pack(_source("A"), _source("B"), _source("C")))
    db = _StubSession()
    cancel = asyncio.Event()
    queried: list[str] = []

    async def _query(source, _subject, _url):
        queried.append(source.name)
        if source.name == "A":
            cancel.set()
        return SourceOutcome(findings_count=1)

    result = await DeepInvestigationOrchestrator(db, _query).run(subject_value="x", cancel_event=cancel)

    assert queried == ["A"], "no source may be queried after the cancel"
    assert result.investigation.status == "cancelled"
    assert result.investigation.end_reason_code == "run.cancelled"
    assert [s.status for s in result.steps] == ["completed", "skipped", "skipped"]


@pytest.mark.asyncio
async def test_an_unset_cancel_event_changes_nothing(monkeypatch):

    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()
    result = await DeepInvestigationOrchestrator(db, _querier({})).run(subject_value="x", cancel_event=asyncio.Event())
    assert result.investigation.status == "completed"
    assert result.summary.all_succeeded is True


@pytest.mark.asyncio
async def test_a_cancelled_task_still_finalizes_the_run(monkeypatch):
    """The stranded-run bug: `CancelledError` is a BaseException, so it used to
    propagate straight past `finish_run` and leave the row `running` — which
    the block polls every 3s for, indefinitely."""

    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()

    async def _query(_source, _subject, _url):
        raise asyncio.CancelledError

    orch = DeepInvestigationOrchestrator(db, _query)
    with pytest.raises(asyncio.CancelledError):
        await orch.run(subject_value="x")

    runs = [o for o in db.objects if hasattr(o, "subject_value")]
    assert len(runs) == 1
    assert runs[0].status == "cancelled", "a stranded run would poll for ever"
    assert "cancelled" in runs[0].end_reason
    assert runs[0].end_reason_code == "run.interrupted"
    assert runs[0].completed_at is not None


@pytest.mark.asyncio
async def test_an_unexpected_error_also_finalizes_the_run(monkeypatch):
    """A querier raising is contained per-step, but a failure in the loop's own
    bookkeeping must still close the run rather than strand it.

    Patching the FIRST checkpoint is deliberate: it fires while the skeleton is
    being written, before the loop starts, and that region used to sit outside
    the guard — `start_run` had already flipped the status, so a failure there
    stranded the row exactly like one inside the loop.
    """
    _install(monkeypatch, _pack(_source("A")))
    db = _StubSession()

    async def _explode(*_a, **_k):
        raise RuntimeError("session died")

    orch = DeepInvestigationOrchestrator(db, _querier({}))
    monkeypatch.setattr(orch, "_checkpoint_and_emit", _explode)
    with pytest.raises(RuntimeError):
        await orch.run(subject_value="x")

    run = next(o for o in db.objects if hasattr(o, "subject_value"))
    assert run.status == "aborted"


# ---------------------------------------------------------------------------
# Review follow-ups: the lead cap and the budget guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leads_dropped_at_the_plan_cap_are_reported(monkeypatch):
    """The cap bounds the PLAN, so dropped leads are counted rather than
    persisted — but they must not vanish. Reporting them in `end_reason` puts
    the gap where the block already renders it, in the footer."""
    many = tuple(_source(f"L{i}", f"https://l{i}.test/{{value}}") for i in range(80))
    _install(monkeypatch, _pack(_source("Seed")))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(db, _querier({"Seed": SourceOutcome(findings_count=1, leads=many)}))
    result = await orch.run(subject_value="x", max_iterations=1)

    assert len(result.steps) == 1 + MAX_LEAD_STEPS
    # A column now, not a suffix on the sentence: the block renders the number
    # instead of parsing it back out of prose.
    assert result.investigation.leads_dropped == 30
    assert result.investigation.end_reason_code == "run.budget_spent"
    assert result.investigation.end_reason_params == {"budget": 1}


@pytest.mark.asyncio
async def test_a_lead_already_covered_is_not_counted_as_dropped(monkeypatch):
    """Redundant is not the same as discarded: a lead pointing at a URL already
    in the plan is covered, and reporting it as dropped would invent a gap."""
    seed = _source("Seed")
    _install(monkeypatch, _pack(seed))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(db, _querier({"Seed": SourceOutcome(findings_count=1, leads=(seed, seed))}))
    result = await orch.run(subject_value="x")
    assert result.investigation.leads_dropped == 0


@pytest.mark.asyncio
async def test_one_lead_found_twice_past_the_cap_is_dropped_once(monkeypatch):
    """The count is per DISTINCT lead, not per discovery.

    The URL was only added to `enqueued` BELOW the cap, so two sources
    returning the same new lead once the cap was reached each incremented the
    counter. The footer then reported more leads lost than exist — in the one
    sentence whose entire purpose is to be an honest statement of coverage,
    which makes over-reporting the same kind of failure as under-reporting.
    """
    import app.services.investigation_orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "MAX_LEAD_STEPS", 0)
    shared = _source("Shared lead", url="https://shared.test/{value}")
    a, b = _source("A"), _source("B")
    _install(monkeypatch, _pack(a, b))

    db = _StubSession()
    result = await DeepInvestigationOrchestrator(
        db,
        _querier(
            {
                "A": SourceOutcome(findings_count=1, leads=(shared,)),
                "B": SourceOutcome(findings_count=1, leads=(shared,)),
            }
        ),
    ).run(subject_value="x")

    # The count starts life appended to `end_reason` and becomes its own
    # column further up the stack. Asserted against whichever this branch has,
    # so the test travels with the cascade rather than conflicting at every
    # merge — the property under test is the NUMBER, not where it is stored.
    dropped = getattr(result.investigation, "leads_dropped", None)
    if dropped is None:
        reason = result.investigation.end_reason or ""
        assert "1 further lead(s) dropped" in reason, (
            f"one distinct lead was lost, not two — got {reason!r}"
        )
    else:
        assert dropped == 1, f"one distinct lead was lost, not {dropped}"


@pytest.mark.asyncio
async def test_an_explicit_zero_budget_queries_nothing(monkeypatch):
    """`or` treated 0 as unset and substituted the pack's ceiling of ten."""
    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()
    calls: list[str] = []
    result = await DeepInvestigationOrchestrator(db, _querier({}, calls)).run(subject_value="x", max_iterations=0)
    assert calls == [], "a zero budget must query no source at all"
    assert result.investigation.max_iterations == 0
    assert [s.status for s in result.steps] == ["skipped", "skipped"]
    assert "budget spent (0)" in (result.investigation.end_reason or "")
    assert result.investigation.end_reason_code == "run.budget_spent"
    assert result.investigation.end_reason_params == {"budget": 0}

# A run always reaches a terminal state
#
# Not tidiness. The block polls on `status` and stops only when it is terminal,
# so a run stranded in `running` makes every future open of that conversation
# issue a request every few seconds, per mounted block, forever — one crashed
# run becomes permanent background load. Per-source failures are already
# contained; what these cover is everything OUTSIDE the querier.
# ---------------------------------------------------------------------------


def _exploding_querier(exc: BaseException):
    async def _query(_source, _subject_value, _url):
        raise exc

    return _query


@pytest.mark.asyncio
async def test_a_crash_outside_the_querier_closes_the_run_as_aborted(monkeypatch):
    """The querier is wrapped per-source, so a crash there fails one step. This
    is the other case: something raising in the loop itself — a failing
    checkpoint commit is the realistic one — which used to leave the row at
    `running` with `completed_at` NULL and nothing to ever write it."""
    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()

    commits = {"n": 0}
    original_commit = db.commit

    async def _commit():
        commits["n"] += 1
        if commits["n"] == 3:  # mid-run, after the skeleton is durable
            raise RuntimeError("the database went away")
        await original_commit()

    db.commit = _commit
    orch = DeepInvestigationOrchestrator(db, _querier({}))

    with pytest.raises(RuntimeError, match="the database went away"):
        await orch.run(subject_value="APT29")

    run = db._run_row()
    assert run.status == "aborted", "a crashed run must not stay pollable forever"
    assert run.completed_at is not None
    assert "unrecoverable" in (run.end_reason or "")
    assert db.rollbacks == 1, "the failed flush must be cleared before writing"


@pytest.mark.asyncio
async def test_a_base_exception_that_is_not_a_cancellation_still_closes_the_run(
    monkeypatch,
):
    """`except Exception` is not wide enough, and a `CancelledError` clause on
    its own does not make it wide enough either: a worker killed by a signal
    handler that raises, or any other `BaseException`, strands the run just the
    same. This is what makes the guard `BaseException` rather than a pair of
    narrower clauses."""
    _install(monkeypatch, _pack(_source("A")))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(db, _exploding_querier(_NotAnException()))

    with pytest.raises(_NotAnException):
        await orch.run(subject_value="APT29")

    assert db._run_row().status == "aborted"


@pytest.mark.asyncio
async def test_the_original_exception_is_never_replaced_by_the_cleanup(monkeypatch):
    """The close is best-effort by design: it runs on a path where something has
    already gone wrong, possibly the database itself. A failure here must not
    hide the exception that explains what happened."""
    _install(monkeypatch, _pack(_source("A")))
    db = _StubSession()

    async def _no_rollback():
        raise RuntimeError("rollback failed too")

    db.rollback = _no_rollback
    orch = DeepInvestigationOrchestrator(
        db, _exploding_querier(_NotAnException())
    )

    with pytest.raises(_NotAnException):  # not RuntimeError
        await orch.run(subject_value="APT29")


@pytest.mark.asyncio
async def test_a_caller_owned_transaction_is_left_alone(monkeypatch):
    """`commit_each_step=False` means the caller owns the transaction. Rolling
    it back and committing our own row would destroy work they have not
    finished — unwinding is theirs to do."""
    _install(monkeypatch, _pack(_source("A")))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(db, _querier({}), commit_each_step=False)

    async def _boom(_frame):
        raise RuntimeError("boom")

    orch._checkpoint_and_emit = _boom

    with pytest.raises(RuntimeError):
        await orch.run(subject_value="APT29")

    assert db.rollbacks == 0
    assert db.commits == 0


@pytest.mark.asyncio
async def test_a_run_that_already_finished_is_not_reopened(monkeypatch):
    """If the loop completed and the failure came afterwards, the run is already
    `completed`. Overwriting that with `aborted` would turn a successful
    investigation into a failed one in the UI."""
    _install(monkeypatch, _pack(_source("A")))
    db = _StubSession()

    orch = DeepInvestigationOrchestrator(db, _querier({}))
    # Fail on the very last thing the run does, after `finish_run`.
    original = orch._checkpoint_and_emit

    async def _checkpoint_and_emit(frame):
        await original(frame)
        if frame.get("status") == "investigation_complete":
            raise RuntimeError("failed after the run was closed")

    orch._checkpoint_and_emit = _checkpoint_and_emit

    with pytest.raises(RuntimeError):
        await orch.run(subject_value="APT29")

    assert db._run_row().status == "completed", (
        "a finished run must not be rewritten as aborted by a later failure"
    )


def test_run_and_the_loop_it_wraps_take_the_same_arguments():
    """`run` exists only to wrap `_run` in the terminal-state guarantee, so the
    two signatures must stay identical.

    This is not hypothetical tidiness. A parameter added to `_run` and not to
    `run` is a `TypeError` the linter catches; the dangerous direction is the
    other one — a parameter `run` accepts and silently drops on the way down,
    which no linter sees and which surfaces as a feature that quietly does
    nothing. A merge that carries the pair across branches is exactly where
    that happens.
    """
    import inspect

    outer = inspect.signature(DeepInvestigationOrchestrator.run)
    inner = inspect.signature(DeepInvestigationOrchestrator._run)
    assert list(outer.parameters) == list(inner.parameters)

    # ...and every one of them is actually forwarded.
    source = inspect.getsource(DeepInvestigationOrchestrator.run)
    call = source[source.index("await self._run("):]
    for name in list(inner.parameters)[1:]:  # skip `self`
        assert f"{name}={name}," in call, (
            f"`run` accepts {name!r} but does not pass it on — it would be "
            "silently dropped"
        )

# ── The declared params are the emitted params ──────────────────────────────
#
# `DETAIL_CODE_PARAMS` is only a single source of truth if both ends are held
# to it. The block's messages are checked against it in `test_investigation_
# api.py`; these check the other end, by RUNNING each terminal path and reading
# what it actually wrote. A declaration nobody emits, or an emission nobody
# declared, fails here.


def _declared(code: str) -> frozenset[str]:
    from app.models.investigation import DETAIL_CODE_PARAMS

    return DETAIL_CODE_PARAMS[code]


@pytest.mark.asyncio
async def test_a_run_that_queried_everything_says_so_with_no_params(monkeypatch):
    _install(monkeypatch, _pack(_source("A")))
    db = _StubSession()
    result = await DeepInvestigationOrchestrator(db, _querier({})).run(subject_value="x")
    inv = result.investigation
    assert inv.end_reason_code == "run.all_sources_queried"
    assert set(inv.end_reason_params or {}) == _declared("run.all_sources_queried")


@pytest.mark.asyncio
async def test_a_spent_budget_reports_the_budget_it_spent(monkeypatch):
    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()
    result = await DeepInvestigationOrchestrator(db, _querier({})).run(
        subject_value="x", max_iterations=1
    )
    inv = result.investigation
    assert inv.end_reason_code == "run.budget_spent"
    assert set(inv.end_reason_params or {}) == _declared("run.budget_spent")
    assert inv.end_reason_params == {"budget": 1}


@pytest.mark.asyncio
async def test_a_subject_no_pack_covers_reports_its_type(monkeypatch):
    """The only other code with a placeholder, and it had no test at all —
    which is how `{subject_type}` could have been renamed on either side
    without anything noticing."""
    _install(monkeypatch, _pack(_source("A", types=("ip",))))
    db = _StubSession()
    result = await DeepInvestigationOrchestrator(db, _querier({})).run(
        subject_value="x", subject_type="file_hash"
    )
    inv = result.investigation
    assert inv.end_reason_code == "run.no_covering_source"
    assert set(inv.end_reason_params or {}) == _declared("run.no_covering_source")
    assert inv.end_reason_params == {"subject_type": "file_hash"}


@pytest.mark.asyncio
async def test_a_cancelled_run_reports_the_cancellation_with_no_params(monkeypatch):
    _install(monkeypatch, _pack(_source("A"), _source("B")))
    db = _StubSession()
    cancel = asyncio.Event()
    cancel.set()
    result = await DeepInvestigationOrchestrator(db, _querier({})).run(
        subject_value="x", cancel_event=cancel
    )
    inv = result.investigation
    assert inv.end_reason_code == "run.cancelled"
    assert set(inv.end_reason_params or {}) == _declared("run.cancelled")


@pytest.mark.asyncio
async def test_an_interrupted_run_reports_the_interruption_with_no_params(monkeypatch):
    _install(monkeypatch, _pack(_source("A")))
    db = _StubSession()
    orch = DeepInvestigationOrchestrator(db, _querier({}))

    async def _boom(_frame):
        raise RuntimeError("session died")

    orch._checkpoint_and_emit = _boom
    with pytest.raises(RuntimeError):
        await orch.run(subject_value="x")

    inv = db._run_row()
    assert inv.end_reason_code == "run.interrupted"
    assert set(inv.end_reason_params or {}) == _declared("run.interrupted")


@pytest.mark.asyncio
async def test_an_interrupted_run_keeps_the_leads_it_dropped(monkeypatch):
    """A count the run genuinely accumulated must survive the close.

    `finish_run` defaulted `leads_dropped` to 0, so every caller that did not
    pass it — the finalizer included — erased it. A run that dropped leads and
    then hit a failing checkpoint reported "0 leads dropped": a coverage claim
    the run cannot support, from the block whose reason for existing is never
    to make one.
    """
    import app.services.investigation_orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "MAX_LEAD_STEPS", 0)
    lead = _source("Unreachable lead", url="https://lead.test/{value}")
    # Two sources, so there is a checkpoint AFTER the lead is dropped and
    # BEFORE the run closes — which is the window the finalizer runs in.
    _install(monkeypatch, _pack(_source("A"), _source("B")))

    db = _StubSession()
    orch = DeepInvestigationOrchestrator(
        db, _querier({"A": SourceOutcome(findings_count=1, leads=(lead,))})
    )
    original = orch._checkpoint_and_emit
    seen = {"n": 0}

    async def _fail_after_the_lead(frame):
        await original(frame)
        seen["n"] += 1
        # planned, A active, A result, [lead dropped], B active <- here.
        if seen["n"] >= 4:
            raise RuntimeError("session died")

    orch._checkpoint_and_emit = _fail_after_the_lead

    with pytest.raises(RuntimeError):
        await orch.run(subject_value="x")

    inv = db._run_row()
    assert inv.status == "aborted"
    assert inv.leads_dropped == 1, (
        f"the run dropped a lead and the close erased it (got {inv.leads_dropped})"
    )

# `_StubSession` gains `rollback` / `get` further up the stack, where
# `_RecoverableSession` is folded into it. Bind whichever class this branch
# has, so these tests travel with the cascade instead of breaking at each
# merge.
_CloseableSession = globals().get("_RecoverableSession", _StubSession)

# ── A second cancellation must not cut the close short ──────────────────────
#
# `_finalize` awaits four times, and it is called FROM the handler for a
# cancellation that has already been delivered. A second delivery — a worker
# shutting down, a socket teardown cancelling the turn twice — interrupted the
# close partway, escaped `except Exception` (which does not catch
# `CancelledError`), replaced the original exception and left the run
# `running`. That is the round-1 bug, reappearing on the path its own
# docstring calls the likeliest.


class _SlowRollbackSession(_CloseableSession):
    """Rolls back slowly, so a cancellation lands while the close is awaiting."""

    def __init__(self, delay: float = 0.05):
        super().__init__()
        self._delay = delay
        self.committed = 0

    async def rollback(self):
        self.rollbacks += 1
        await asyncio.sleep(self._delay)

    async def commit(self):
        self.committed += 1
        await super().commit()


@pytest.mark.asyncio
async def test_a_second_cancellation_does_not_stop_the_close(monkeypatch):
    _install(monkeypatch, _pack(_source("A")))
    db = _SlowRollbackSession()
    orch = DeepInvestigationOrchestrator(db, _querier({}))

    async def _hang(_frame):
        await asyncio.sleep(3600)

    orch._checkpoint_and_emit = _hang

    task = asyncio.create_task(orch.run(subject_value="APT29"))
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()  # again, while `_finalize` is mid-rollback

    with pytest.raises(asyncio.CancelledError):
        await task

    # The shield lets the close finish after the task itself has gone.
    await asyncio.sleep(0.2)
    run = db._run_row()
    assert run.status == "cancelled", (
        f"the close was cut short and the run stayed {run.status!r}"
    )
    assert run.completed_at is not None


@pytest.mark.asyncio
async def test_a_cancellation_inside_the_close_never_replaces_the_original(
    monkeypatch,
):
    """`_finalize` catching only `Exception` let a `CancelledError` raised
    inside it escape and become what the caller saw."""
    _install(monkeypatch, _pack(_source("A")))

    class _Hostile(_CloseableSession):
        async def rollback(self):
            self.rollbacks += 1
            raise asyncio.CancelledError

    db = _Hostile()
    orch = DeepInvestigationOrchestrator(db, _exploding_querier(_NotAnException()))

    with pytest.raises(_NotAnException):
        await orch.run(subject_value="APT29")

    assert db.rollbacks == 1, "the close was attempted"


@pytest.mark.asyncio
async def test_finalize_itself_never_raises() -> None:
    """Its own contract, tested directly rather than through the shield.

    `_finalize_shielded` suppresses what escapes, so a narrow guard inside
    `_finalize` looks harmless from the outside — the wrapper catches it
    either way. It is not harmless: `_finalize` is called directly (here, and
    by anything that reaches for it later), and the two guards exist for
    different reasons. The wrapper stops the SHIELD's cancellation replacing
    an aborting exception; this one stops a failure INSIDE the close escaping
    at all.
    """
    class _Hostile(_CloseableSession):
        async def rollback(self):
            self.rollbacks += 1
            raise asyncio.CancelledError

    orch = DeepInvestigationOrchestrator(_Hostile(), _querier({}))
    orch._run_id = uuid.uuid4()

    # No `pytest.raises`: returning is the assertion.
    await orch._finalize("aborted", "something went wrong")
    assert orch.db.rollbacks == 1


@pytest.mark.asyncio
async def test_lead_positions_stay_contiguous_without_asking_the_database(
    monkeypatch,
):
    """`append_lead_step` now takes the position the loop already knows.

    The loop is the only thing appending to this run, so passing the number is
    correct — but "correct" here means the positions stay contiguous and in
    discovery order, which is what the block orders by. A test that only
    counted queries would pass on an off-by-one.
    """
    seed = _source("Seed")
    leads = tuple(_source(f"Lead{i}", url=f"https://l{i}.test/{{value}}") for i in range(3))
    _install(monkeypatch, _pack(seed, _source("Other")))

    db = _StubSession()
    queries = {"n": 0}
    original_execute = db.execute

    async def _counting_execute(stmt):
        queries["n"] += 1
        return await original_execute(stmt)

    db.execute = _counting_execute

    result = await DeepInvestigationOrchestrator(
        db, _querier({"Seed": SourceOutcome(findings_count=1, leads=leads)})
    ).run(subject_value="x")

    positions = [s.position for s in result.steps]
    assert positions == list(range(len(positions))), (
        f"positions must be contiguous from 0, got {positions}"
    )
    assert [s.source_name for s in result.steps][:2] == ["Seed", "Other"], (
        "planned steps keep their order"
    )
    assert queries["n"] == 0, (
        "the loop knows the next position; it must not ask the database for it"
    )


@pytest.mark.asyncio
async def test_a_caller_without_a_position_still_gets_one(monkeypatch):
    """The lookup path stays, for anything appending out of band."""
    from app.services.investigation_state import append_lead_step

    db = _StubSession()
    run = await state.create_investigation(
        db, subject_type="unknown", subject_value="x", source_pack_slugs=["p"]
    )
    await state.plan_steps(db, run, [state.PlannedStep(source_name="A", source_pack_slug="p")])
    lead = await append_lead_step(db, run, source_name="L", source_pack_slug="p")
    assert lead.position == 1


@pytest.mark.asyncio
async def test_the_close_finishes_before_the_caller_unwinds(monkeypatch):
    """The close must be DONE when `run` re-raises, not merely started.

    `shield` alone protects the inner coroutine but lets the outer await raise
    at once, so `_finalize` ran on as a task nobody awaited. Two things follow,
    both on the path this wrapper exists to protect: the caller disposes its
    session while the orphan is mid-await on it, so the write hits a closed
    session and the run stays `running`; and an `AsyncSession` is not safe for
    concurrent use, so the orphan and the caller's teardown interleave on one
    connection.

    The round-2 test missed this because it slept before asserting, which gave
    the orphan time to finish against a stub session that was still alive. This
    one asserts at the moment the caller unwinds and marks the session closed
    immediately after.
    """
    _install(monkeypatch, _pack(_source("A")))

    class _SlowSession(_CloseableSession):
        def __init__(self):
            super().__init__()
            self.open = True
            self.touched_after_close = False

        async def rollback(self):
            self.rollbacks += 1
            await asyncio.sleep(0.05)

        async def commit(self):
            if not self.open:
                self.touched_after_close = True
            await super().commit()

    db = _SlowSession()
    orch = DeepInvestigationOrchestrator(db, _querier({}))

    async def _hang(_frame):
        await asyncio.sleep(3600)

    orch._checkpoint_and_emit = _hang

    task = asyncio.create_task(orch.run(subject_value="APT29"))
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()          # again, while the close is awaiting
    await asyncio.sleep(0.01)
    task.cancel()          # and again

    with pytest.raises(asyncio.CancelledError):
        await task

    # The caller has unwound. No sleep: the close must already be complete.
    db.open = False
    run = db._run_row()
    assert run.status == "cancelled", (
        f"the close had not finished when the caller unwound (status {run.status!r})"
    )
    assert run.completed_at is not None

    # And nothing may touch the session afterwards.
    await asyncio.sleep(0.2)
    assert db.touched_after_close is False, (
        "a detached close raced the caller's session teardown"
    )
