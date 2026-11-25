"""Fair share of the worker pool across fleets, delegated children first.

``assignment_dispatch.fair_share_order`` decides the order a free slot claims
due assignment runs in; ``AssignmentAdapter.deal_claims`` hands it to the
engine's ``claim_due(deal=...)``. The simulation below drives the same function
slot release by slot release.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services import assignment_dispatch as dispatch
from app.services.assignment_dispatch import ClaimCandidate
from app.services.orchestration_adapters import assignment as assignment_adapter

_BASE = datetime(2026, 11, 23, 8, 0, tzinfo=UTC)


def _run(
    key: str,
    group: str,
    agent: str | None = None,
    *,
    at: int,
    child: bool = False,
    interactive: bool = False,
    assignment: str = "",
) -> ClaimCandidate:
    return ClaimCandidate(
        key=key,
        group=group,
        agent=agent or f"{group}-agent",
        run_at=_BASE + timedelta(seconds=at),
        child_of_live_parent=child,
        interactive=interactive,
        assignment=assignment,
    )


def _order(
    candidates: list[ClaimCandidate],
    *,
    running_group: dict[str, int] | None = None,
    running_agent: dict[str, int] | None = None,
    pool: int = 10,
    fleet_share: float = 0.5,
    agent_share: float = 0.5,
) -> list[object]:
    return dispatch.fair_share_order(
        candidates,
        running_by_group=running_group or {},
        running_by_agent=running_agent or {},
        pool=pool,
        fleet_share=fleet_share,
        agent_share=agent_share,
    )


def test_share_cap_bounds():
    assert dispatch.share_cap(10, 0.5) == 5
    assert dispatch.share_cap(10, 0.25) == 3
    assert dispatch.share_cap(1, 0.5) == 1
    assert dispatch.share_cap(10, 0) == 10
    assert dispatch.share_cap(10, 1) == 10


def test_single_fleet_keeps_oldest_first():
    runs = [_run(f"a{i}", "A", at=i) for i in range(6)]
    assert _order(list(reversed(runs))) == [f"a{i}" for i in range(6)]


def test_groups_take_turns_oldest_group_first():
    runs = [_run(f"a{i}", "A", at=i) for i in range(4)] + [_run("b0", "B", at=100)]
    assert _order(runs)[:3] == ["a0", "b0", "a1"]


def test_a_fleet_over_its_share_goes_after_other_fleets():
    runs = [_run(f"a{i}", "A", at=i) for i in range(3)] + [_run("b0", "B", at=50), _run("b1", "B", at=51)]
    order = _order(runs, running_group={"A": 5}, running_agent={"A-agent": 5})
    assert order == ["b0", "b1", "a0", "a1", "a2"]


def test_an_agent_over_its_share_goes_after_its_fleet_mates():
    runs = [_run("x0", "F", "x", at=0), _run("x1", "F", "x", at=1), _run("y0", "F", "y", at=2)]
    order = _order(runs, running_group={"F": 1}, running_agent={"x": 5}, fleet_share=1)
    assert order == ["y0", "x0", "x1"]


def test_work_conserving_when_only_one_fleet_waits():
    runs = [_run(f"a{i}", "A", at=i) for i in range(3)]
    assert _order(runs, running_group={"A": 10}, running_agent={"A-agent": 10}) == ["a0", "a1", "a2"]


def test_delegated_child_of_a_live_parent_is_claimed_first():
    backlog = [_run(f"o{i}", f"G{i}", at=i) for i in range(20)]
    child = _run("child", "A", at=500, child=True)
    assert _order([*backlog, child])[0] == "child"


def test_flood_of_fleet_a_does_not_delay_fleet_b_past_one_slot_release():
    """Acceptance: fleet A floods 50 runs; a run of fleet B created after them
    starts at the next slot release, and A never holds more than its cap while
    B has due work."""
    pool = 10
    queue = [_run(f"a{i}", "A", at=i) for i in range(50)]
    running: list[ClaimCandidate] = []
    # The pool fills with A's oldest runs first.
    for key in _order(queue, pool=pool)[:pool]:
        chosen = next(c for c in queue if c.key == key)
        queue.remove(chosen)
        running.append(chosen)
    assert len(running) == pool
    queue.append(_run("b0", "B", at=60))
    # One slot is released.
    running.pop(0)
    by_group: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    for item in running:
        by_group[item.group] = by_group.get(item.group, 0) + 1
        by_agent[item.agent] = by_agent.get(item.agent, 0) + 1
    assert _order(queue, running_group=by_group, running_agent=by_agent, pool=pool)[0] == "b0"


def test_a_fleet_holds_at_most_its_cap_while_another_has_due_work():
    pool = 10
    queue = [_run(f"a{i}", "A", at=i) for i in range(50)] + [_run(f"b{i}", "B", at=100 + i) for i in range(50)]
    running: list[ClaimCandidate] = []
    while len(running) < pool:
        by_group: dict[str, int] = {}
        by_agent: dict[str, int] = {}
        for item in running:
            by_group[item.group] = by_group.get(item.group, 0) + 1
            by_agent[item.agent] = by_agent.get(item.agent, 0) + 1
        key = _order(queue, running_group=by_group, running_agent=by_agent, pool=pool)[0]
        chosen = next(c for c in queue if c.key == key)
        queue.remove(chosen)
        running.append(chosen)
    assert sum(1 for item in running if item.group == "A") == 5
    assert sum(1 for item in running if item.group == "B") == 5


# -- #2234: a burst of event runs never starves a resume, nor another assignment ---------


def test_a_gate_answer_resume_goes_before_a_burst_of_bulk_runs_of_its_agent() -> None:
    """Acceptance: 500 due bulk runs of agent A (one assignment) and one gate-answer resume of agent A created last."""
    burst = [_run(f"e{i}", "F", "A", at=i, assignment="case-opened") for i in range(500)]
    resume = _run("gate", "F", "A", at=900, interactive=True, assignment="gate-answered")
    assert _order([*burst, resume], pool=3)[0] == "gate"


def test_an_interactive_run_goes_first_even_when_its_fleet_is_over_its_share() -> None:
    burst = [_run(f"e{i}", "F", "A", at=i, assignment="bulk") for i in range(20)]
    resume = _run("item", "F", "A", at=50, interactive=True, assignment="inbox")
    order = _order([*burst, resume], running_group={"F": 3}, running_agent={"A": 3}, pool=3)
    assert order[0] == "item"


def test_a_second_assignment_of_one_agent_is_claimed_within_one_slot_release() -> None:
    """Acceptance: 100 due runs of a first assignment and 1 of a second created last."""
    first = [_run(f"a{i}", "F", "A", at=i, assignment="one") for i in range(100)]
    second = _run("b0", "F", "A", at=500, assignment="two")
    assert _order([*first, second], pool=3)[:2] == ["a0", "b0"]


def test_a_burst_on_a_three_slot_pool_never_holds_a_gate_answer_back() -> None:
    """Acceptance: ~500 opened events on a 3-slot pool; a gate answer created mid-burst starts at the next release."""
    pool = 3
    queue = [_run(f"e{i}", "F", "A", at=i, assignment="case-opened") for i in range(500)]
    running: list[ClaimCandidate] = []
    for key in _order(queue, pool=pool)[:pool]:
        chosen = next(c for c in queue if c.key == key)
        queue.remove(chosen)
        running.append(chosen)
    queue.append(_run("gate", "F", "A", at=600, interactive=True, assignment="gate-answered"))
    running.pop(0)  # one slot is released
    assert _order(queue, running_group={"F": 2}, running_agent={"A": 2}, pool=pool)[0] == "gate"


@pytest.mark.parametrize(
    ("context", "answered", "expected"),
    [
        ({"type": "work_item"}, None, True),
        ({"type": "platform_event", "event": "work_item.status_changed"}, True, True),
        # The payload's ``answered`` as ``->>`` text: only the JSON literal true counts.
        ({"type": "platform_event", "event": "work_item.status_changed"}, "true", True),
        ({"type": "platform_event", "event": "work_item.status_changed"}, "3", False),
        ({"type": "platform_event", "event": "work_item.status_changed"}, '{"by": "x"}', False),
        ({"type": "platform_event", "event": "work_item.status_changed"}, "yes", False),
        ({"type": "platform_event", "event": "work_item.released"}, None, True),
        ({"type": "platform_event", "event": "ctem_case.opened"}, None, False),
        ({"type": "platform_event", "event": "work_item.status_changed"}, False, False),
        ({"type": "schedule"}, None, False),
        (None, None, False),
    ],
)
def test_what_makes_a_run_interactive(context: object, answered: object, expected: bool) -> None:
    assert assignment_adapter.is_interactive_run(context, answered) is expected


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def scalars(self):
        return self


class _Session:
    """Answers the four reads of ``fair_share_deal`` in order: the runs, the live parents, the running counts and
    the home fleets; then the two of ``fleet_run_budget.hold_over_budget``: the runs and the budgeted fleets."""

    def __init__(self, answers):
        self._answers = list(answers)

    async def execute(self, _statement):
        return _Result(self._answers.pop(0))


class _Execution:
    def __init__(self, run_id, at):
        self.id = run_id
        self.payload = {"run_id": str(run_id)}
        self.run_at = _BASE + timedelta(seconds=at)


@pytest.mark.asyncio
async def test_the_adapter_deals_children_first_then_one_per_fleet(monkeypatch):
    async def _pool():
        return 10

    monkeypatch.setattr(assignment_adapter, "_pool_slots", _pool)
    agent_a, agent_b, parent = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fleet_a, fleet_b = uuid.uuid4(), uuid.uuid4()
    a0, a1, b0, child = (_Execution(uuid.uuid4(), at) for at in (0, 1, 50, 90))
    runs = [
        (a0.id, agent_a, None, None, None, None),
        (a1.id, agent_a, None, None, None, None),
        (b0.id, agent_b, None, None, None, None),
        (child.id, agent_a, parent, None, None, None),
    ]
    session = _Session(
        [
            runs,
            [parent],
            [(agent_a, 1)],
            [(agent_a, fleet_a), (agent_b, fleet_b)],
            # No fleet has a run budget (#1526): nothing is held.
            runs,
            [],
        ]
    )
    ordered = await assignment_adapter.AssignmentAdapter().deal_claims(session, [a0, a1, b0, child], "w1")
    assert [e.id for e in ordered] == [child.id, a0.id, b0.id, a1.id]


@pytest.mark.asyncio
async def test_the_adapter_keeps_oldest_first_when_fair_share_is_off(monkeypatch):
    monkeypatch.setattr(dispatch.settings, "assignment_fair_share_enabled", False)
    due = [_Execution(uuid.uuid4(), at) for at in (0, 1, 2)]
    assert await assignment_adapter.fair_share_deal(_Session([]), due, "w1") == due
