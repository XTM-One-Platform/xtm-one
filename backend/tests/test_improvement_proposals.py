"""The generic rules of improvement proposals (#8256), without a database.

The dedup key (the order or repetition of evidence makes no new key; a
finding's fingerprint wins), the lifecycle's transitions (a level 7 handler
cannot jump a proposal back), which flag reaches which proposal, the
registry's refusal of a handler that does not match its type, and where a
watch ends on its re-evaluation (the 2-point margin, a rollback only of the
proposal's own change). The persona type's rules: ``test_persona_improvement.py``;
against PostgreSQL: ``test_improvement_proposals_postgres.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.models.improvement_proposal import (
    OPEN_STATUSES,
    PROPOSAL_STATUSES,
    PROPOSAL_TYPES,
    STATUS_APPLYING,
    STATUS_FAILED,
    STATUS_KEPT,
    STATUS_PENDING_APPROVAL,
    STATUS_REJECTED,
    STATUS_ROLLED_BACK,
    STATUS_SHADOW,
    STATUS_WATCHING,
    TARGET_OF,
    TARGET_TYPES,
)
from app.services import improvement_proposals as proposals
from app.services.improvement_proposals import lifecycle


def test_every_type_names_its_target():
    assert set(TARGET_OF) == set(PROPOSAL_TYPES)
    assert set(TARGET_OF.values()) == set(TARGET_TYPES)
    assert set(OPEN_STATUSES) < set(PROPOSAL_STATUSES)


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def test_the_order_or_repetition_of_evidence_makes_no_new_key():
    a = [{"kind": "trace", "id": "t-1"}, {"kind": "rating", "id": "r-2"}]
    b = [{"kind": "rating", "id": "r-2"}, {"kind": "trace", "id": "t-1"}, {"kind": "trace", "id": "t-1"}]
    assert proposals.evidence_fingerprint({}, a) == proposals.evidence_fingerprint({}, b)
    assert proposals.evidence_fingerprint({}, a) != proposals.evidence_fingerprint({}, a[:1])


def test_a_findings_fingerprint_is_the_evidence_fingerprint():
    refs = [{"kind": "trace", "id": "t-1"}]
    assert proposals.evidence_fingerprint({"fingerprint": " f-1 "}, refs) == "f-1"
    assert proposals.evidence_fingerprint({"fingerprint": "  "}, refs) == proposals.evidence_fingerprint({}, refs)


def test_the_key_names_type_target_and_evidence():
    target = uuid.uuid4()
    key = proposals.dedup_key("persona", "agent", target, "f-1")
    assert len(key) == 64
    assert key != proposals.dedup_key("skill", "agent", target, "f-1")
    assert key != proposals.dedup_key("persona", "agent", uuid.uuid4(), "f-1")
    assert key != proposals.dedup_key("persona", "agent", target, "f-2")


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (STATUS_PENDING_APPROVAL, STATUS_APPLYING),
        (STATUS_APPLYING, STATUS_SHADOW),
        (STATUS_SHADOW, STATUS_WATCHING),
        (STATUS_SHADOW, STATUS_FAILED),
        (STATUS_WATCHING, STATUS_ROLLED_BACK),
    ],
)
async def test_a_handler_moves_a_proposal_along_its_lifecycle(before, after):
    proposal = SimpleNamespace(status=before, outcome_note=None, applied_at=None, settled_at=None)

    await proposals.set_proposal_status(None, proposal, after, "Why.")  # type: ignore[arg-type]

    assert (proposal.status, proposal.outcome_note) == (after, "Why.")
    assert (proposal.settled_at is not None) == (after in (STATUS_FAILED, STATUS_ROLLED_BACK))
    assert (proposal.applied_at is not None) == (after in (STATUS_SHADOW, STATUS_WATCHING))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (STATUS_REJECTED, STATUS_WATCHING),
        (STATUS_KEPT, STATUS_ROLLED_BACK),
        (STATUS_WATCHING, STATUS_SHADOW),
        (STATUS_PENDING_APPROVAL, STATUS_KEPT),
    ],
)
async def test_a_transition_the_lifecycle_does_not_have_is_refused(before, after):
    proposal = SimpleNamespace(status=before, outcome_note=None, applied_at=None, settled_at=None)

    with pytest.raises(ValueError):
        await proposals.set_proposal_status(None, proposal, after)  # type: ignore[arg-type]
    assert proposal.status == before


# ---------------------------------------------------------------------------
# Which flag reaches which proposal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("by_agent", "evaluation", "concierge", "reachable"),
    [
        (False, True, False, True),
        (False, False, True, False),
        (True, False, True, True),
        (True, True, False, False),
    ],
)
def test_a_persons_request_follows_evaluation_and_an_agents_the_concierge(
    monkeypatch, by_agent, evaluation, concierge, reachable
):
    monkeypatch.setattr("app.services.evaluation.availability.evaluation_enabled", lambda: evaluation)
    monkeypatch.setattr(lifecycle, "health_enabled", lambda: concierge)
    proposal = SimpleNamespace(author_agent_id=uuid.uuid4() if by_agent else None)

    assert proposals.surface_enabled(proposal) is reachable


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_a_handler_registers_under_its_own_type_and_target(monkeypatch):
    from app.services.improvement_proposals import registry

    # Loaded already: the real handlers never land in the temporary registry.
    monkeypatch.setattr(registry, "_loaded", True)
    monkeypatch.setattr(registry, "_HANDLERS", {})
    skill = SimpleNamespace(proposal_type="skill", target_type="skill", owns_watch=False)
    proposals.register_proposal_type("skill", skill)  # type: ignore[arg-type]

    assert proposals.handler_for("skill") is skill
    assert proposals.handler_for_target("skill") is skill
    for wrong_type, handler in [
        ("persona", skill),
        ("skill", SimpleNamespace(proposal_type="skill", target_type="agent", owns_watch=False)),
    ]:
        with pytest.raises(ValueError):
            proposals.register_proposal_type(wrong_type, handler)  # type: ignore[arg-type]


async def test_a_type_without_a_handler_is_refused_at_filing(monkeypatch):
    from app.services.improvement_proposals import registry

    monkeypatch.setattr(registry, "_loaded", True)
    monkeypatch.setattr(registry, "_HANDLERS", {})
    spec = proposals.ProposalSpec(proposal_type="new_flow", target_id=uuid.uuid4(), experiment_verdict="unmeasured")

    with pytest.raises(proposals.ProposalError) as refused:
        await proposals.file_proposal(None, spec=spec, author_agent=SimpleNamespace(id=uuid.uuid4()))  # type: ignore[arg-type]
    assert refused.value.status_code == 422


async def test_a_proposal_has_one_author():
    spec = proposals.ProposalSpec(proposal_type="skill", target_id=uuid.uuid4())
    with pytest.raises(ValueError):
        await proposals.file_proposal(None, spec=spec)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await proposals.file_proposal(
            None,  # type: ignore[arg-type]
            spec=spec,
            author_agent=SimpleNamespace(id=uuid.uuid4()),
            requested_by=SimpleNamespace(id=uuid.uuid4()),
        )


# ---------------------------------------------------------------------------
# Settling a watch on its re-evaluation
# ---------------------------------------------------------------------------


class _SettleDb:
    def __init__(self, run):
        self.run = run

    async def get(self, _model, _id):
        return self.run


class _Handler:
    proposal_type = "persona"
    target_type = "agent"
    noun = "persona rewrite"
    subject = "persona"
    owns_watch = False

    def __init__(self, *, still_applied: bool):
        self.still_applied = still_applied
        self.rolled_back: list[str] = []

    async def load_target(self, _db, _target_id, *, lock):
        return SimpleNamespace(id=uuid.uuid4())

    async def is_still_applied(self, _db, _proposal, _target):
        return self.still_applied

    async def rollback(self, _db, _proposal, _target, reason):
        self.rolled_back.append(reason)
        return 4


def _watching(**fields):
    return SimpleNamespace(
        **{
            "proposal_type": "persona",
            "status": STATUS_WATCHING,
            "target_id": uuid.uuid4(),
            "agent_id": uuid.uuid4(),
            "candidate_run_id": uuid.uuid4(),
            "verdict": None,
            "quality_metric": "score",
            "baseline_score": 70.0,
            "candidate_score": None,
            "delta": None,
            "watch_until": None,
            "outcome_note": None,
            "settled_at": None,
            "rollback_version_number": None,
            **fields,
        }
    )


@pytest.mark.parametrize(
    ("run", "still_applied", "status", "rolled_back", "note"),
    [
        (
            SimpleNamespace(status="completed", score=67.5),
            True,
            STATUS_ROLLED_BACK,
            ["Quality went -2.50 points (margin 2)"],
            "Quality went -2.50 points (margin 2): the persona was rolled back to what it was, as v4.",
        ),
        (
            SimpleNamespace(status="completed", score=60.0),
            False,
            "regression_kept",
            [],
            "Quality went -10.00 points (margin 2), but the persona was edited after the change was "
            "applied: that edit stays, nothing was rolled back.",
        ),
        (
            SimpleNamespace(status="completed", score=68.0),
            True,
            STATUS_KEPT,
            [],
            "Quality went -2.00 points (margin 2): the change stays.",
        ),
        (
            SimpleNamespace(status="failed", score=None),
            True,
            STATUS_FAILED,
            [],
            "The re-evaluation run ended failed, so there is no verdict. The change stays active; "
            "roll it back from the persona version history if needed.",
        ),
        (
            SimpleNamespace(status="completed", score=None),
            True,
            STATUS_FAILED,
            [],
            "The re-evaluation has no quality score, so there is no verdict. The change stays active.",
        ),
    ],
    ids=["regression", "regression-after-a-human-edit", "tie-inside-the-margin", "run-failed", "no-score"],
)
async def test_a_watch_ends_on_its_re_evaluation(monkeypatch, run, still_applied, status, rolled_back, note):
    from app.services.improvement_proposals import registry

    handler = _Handler(still_applied=still_applied)
    monkeypatch.setattr(registry, "_HANDLERS", {"persona": handler})
    monkeypatch.setattr(registry, "_loaded", True)
    proposal = _watching()

    result = await proposals.settle(_SettleDb(run), proposal)  # type: ignore[arg-type]

    assert (result.changed, result.status, proposal.status) == (True, status, status)
    assert (handler.rolled_back, proposal.outcome_note) == (rolled_back, note)
    assert proposal.settled_at is not None


async def test_a_running_re_evaluation_settles_nothing(monkeypatch):
    from app.services.improvement_proposals import registry

    monkeypatch.setattr(registry, "_HANDLERS", {"persona": _Handler(still_applied=True)})
    monkeypatch.setattr(registry, "_loaded", True)
    proposal = _watching()

    result = await proposals.settle(_SettleDb(SimpleNamespace(status="running")), proposal)  # type: ignore[arg-type]

    assert (result.changed, proposal.status, proposal.settled_at) == (False, STATUS_WATCHING, None)
