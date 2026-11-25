"""No dispatch of an item whose pull request a live run holds a lease on (#10737, #11480).

Both paths: the scheduler's inbox pass (#10737) and the CI-completion
dispatch of ``work_item_pull_requests`` (#11480).

A run holding ``dev:branch-lock:<head branch>`` (``hold="take"``, value
naming the pull request) is pushing the fix of its red head. The item that
names the same pull request went back to ``open``; dispatched, its run would
read the lease, stand down and end, and the item would come back for another
metered run. The inbox pass leaves it open and unclaimed while the holder
lives; once the lease is released or its holder ended, the next pass takes it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.services.autonomous as autonomous_mod
import app.services.work_item as wi_service
from app.services import shared_state_pull_requests as leases_mod

REPO = "acme/widgets"
BRANCH = "fix/12-x"
NOW = datetime(2026, 11, 13, 3, 40, tzinfo=UTC)


def _item(item_id: str, *, number: int | None = 12, state: str = "open") -> dict:
    payload: dict = {}
    if number is not None:
        payload["pull_requests"] = [{"repo": REPO, "number": number, "head_branch": BRANCH, "state": state}]
    return {"id": item_id, "user_id": str(uuid.uuid4()), "payload": payload, "correlation_key": None}


# -- the matcher over held entries ----------------------------------------------------


def test_a_held_entry_naming_the_pull_request_is_a_lease():
    bindings = [{"repo": REPO, "number": 12, "head_branch": BRANCH}]
    holder = uuid.uuid4()
    rows = [
        (f"dev:branch-lock:{BRANCH}", {"pr": 12}, holder),
        ("babysit", {"pr": 12, "repository": REPO}, holder),
        ("other", {"pr": 12, "repository": "other/repo"}, holder),
        (f"dev:branch-lock:{BRANCH}", {"pr": 13}, holder),
        (f"dev:branch-lock:{BRANCH}", {"pr": 12}, None),
    ]
    found = leases_mod.leases_naming(rows, bindings)
    assert [key for key, _, _ in found] == [f"dev:branch-lock:{BRANCH}", "babysit"]


@pytest.fixture
def leases(monkeypatch):
    state: dict = {"rows": [], "alive": set(), "reads": 0}

    async def held_rows(user_id):
        state["reads"] += 1
        return list(state["rows"])

    async def alive(run_ids):
        return {r for r in run_ids if r in state["alive"]}

    monkeypatch.setattr(leases_mod, "_held_rows", held_rows)
    monkeypatch.setattr(leases_mod, "_alive", alive)
    return state


async def test_a_live_holder_is_answered_and_an_ended_one_is_not(leases):
    holder = uuid.uuid4()
    leases["rows"] = [(f"dev:branch-lock:{BRANCH}", {"pr": 12, "repository": REPO}, holder)]
    leases["alive"] = {holder}
    lease = await leases_mod.live_pull_request_lease(_item("a")["payload"], user_id=None)
    assert lease == {"key": f"dev:branch-lock:{BRANCH}", "run_id": str(holder), "repo": REPO, "number": 12}

    leases["alive"] = set()
    assert await leases_mod.live_pull_request_lease(_item("a")["payload"], user_id=None) is None


async def test_an_item_with_no_open_pull_request_reads_nothing(leases):
    assert await leases_mod.live_pull_request_lease(_item("a", number=None)["payload"], user_id=None) is None
    assert await leases_mod.live_pull_request_lease(_item("a", state="merged")["payload"], user_id=None) is None
    assert leases["reads"] == 0


async def test_a_failed_read_dispatches_as_before(monkeypatch):
    async def boom(user_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(leases_mod, "_held_rows", boom)
    assert await leases_mod.live_pull_request_lease(_item("a")["payload"], user_id=None) is None


# -- the inbox pass ---------------------------------------------------------------------


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _QueueSession:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, *_a, **_k):
        return self._results.pop(0)

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _db():
    agent_id = uuid.uuid4()
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        enabled=True,
        name="Inbox",
        trigger_type="work_item",
        trigger_config={},
    )
    agent = SimpleNamespace(id=agent_id, name="Agent", enabled=True, is_company_managed=False)
    return _QueueSession([_ScalarsResult([assignment]), _ScalarsResult([agent])])


@pytest.fixture
def inbox(monkeypatch, leases):
    state: dict = {"items": [], "claimed": [], "triggered": []}

    async def list_items(**kwargs):
        return [i for i in state["items"] if i["id"] not in state["claimed"]][: kwargs.get("limit", 100)]

    async def claim(work_item_id, **_k):
        state["claimed"].append(work_item_id)
        return {"claimed": True}

    async def release(*, work_item_id, **_k):
        state["claimed"].remove(work_item_id)

    async def record(*_a, **_k):
        return None

    async def trigger(**kwargs):
        state["triggered"].append(kwargs["payload"]["work_item_id"])
        return f"run-{len(state['triggered'])}"

    async def no_active(*_a, **_k):
        return False

    monkeypatch.setattr(autonomous_mod, "has_active_run", no_active)
    monkeypatch.setattr(autonomous_mod, "trigger_assignment", trigger)
    monkeypatch.setattr(wi_service, "list_dispatchable_work_items", list_items)
    monkeypatch.setattr(wi_service, "claim_work_item", claim)
    monkeypatch.setattr(wi_service, "release_work_item_claim", release)
    monkeypatch.setattr(wi_service, "record_claim_run", record)
    state["leases"] = leases
    return state


async def test_the_inbox_skips_an_item_under_a_live_lease_and_takes_the_next(inbox):
    holder = uuid.uuid4()
    inbox["leases"]["rows"] = [(f"dev:branch-lock:{BRANCH}", {"pr": 12, "repository": REPO}, holder)]
    inbox["leases"]["alive"] = {holder}
    inbox["items"] = [_item("leased"), _item("free", number=None)]

    assert await autonomous_mod._process_work_item_assignments(_db()) == 1
    # Not claimed, not dispatched: it stays open for a pass after the release.
    assert inbox["triggered"] == ["free"]
    assert inbox["claimed"] == ["free"]


async def test_once_the_lease_is_released_the_next_pass_dispatches_it_once(inbox):
    holder = uuid.uuid4()
    inbox["leases"]["rows"] = [(f"dev:branch-lock:{BRANCH}", {"pr": 12, "repository": REPO}, holder)]
    inbox["leases"]["alive"] = {holder}
    inbox["items"] = [_item("leased")]

    assert await autonomous_mod._process_work_item_assignments(_db()) == 0
    assert inbox["triggered"] == []

    inbox["leases"]["rows"] = []  # hold="release"
    assert await autonomous_mod._process_work_item_assignments(_db()) == 1
    assert inbox["triggered"] == ["leased"]


async def test_a_lease_whose_holder_ended_does_not_hold_the_item(inbox):
    holder = uuid.uuid4()
    inbox["leases"]["rows"] = [(f"dev:branch-lock:{BRANCH}", {"pr": 12, "repository": REPO}, holder)]
    inbox["items"] = [_item("leased")]

    assert await autonomous_mod._process_work_item_assignments(_db()) == 1
    assert inbox["triggered"] == ["leased"]


# -- the CI-completion dispatch (#11480) ----------------------------------------------


class _RowResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _RowSession:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, *_a, **_k):
        return _RowResult(self._row)

    async def commit(self):
        pass


@pytest.fixture
def ci_dispatch(monkeypatch, leases):
    """``work_item_pull_requests._dispatch`` with the database, the inbox and the run start replaced."""
    from app.core import database
    from app.services import assignment_engine
    from app.services import work_item_pull_requests as wipr

    state: dict = {"triggered": [], "published": [], "recorded": [], "leases": leases}
    agent = SimpleNamespace(id=uuid.uuid4())
    inbox = SimpleNamespace(name="Inbox", source=(SimpleNamespace(id=uuid.uuid4()), agent), user_id=None, writer=None)

    def install(row):
        monkeypatch.setattr(database, "async_session", lambda: _RowSession(row))

    async def owner_inbox(_db, _item):
        return inbox

    async def no_active(*_a, **_k):
        return False

    async def trigger(**kwargs):
        state["triggered"].append(kwargs["payload"]["work_item_id"])
        return "run-1"

    async def publish(item_id, **kwargs):
        state["published"].append((item_id, kwargs))

    async def record(item_id, **_k):
        state["recorded"].append(item_id)

    async def serialized(work_item_id, **_k):
        return {"id": work_item_id, "correlation_key": None}

    monkeypatch.setattr(wipr, "_owner_inbox", owner_inbox)
    monkeypatch.setattr(assignment_engine, "has_active_run", no_active)
    monkeypatch.setattr(assignment_engine, "trigger_assignment", trigger)
    monkeypatch.setattr(wi_service, "publish_transition", publish)
    monkeypatch.setattr(wi_service, "record_claim_run", record)
    monkeypatch.setattr(wi_service, "get_work_item", serialized)
    monkeypatch.setattr(wi_service, "bounded_trigger_item", lambda item: item)
    state["install"] = install
    state["dispatch"] = wipr._dispatch
    return state


def _ci_row(status: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status=status,
        payload=_item("a")["payload"],
        blocked_on=None,
        claim_notes=[],
        claimed_by_agent_id=None,
        claimed_by_run_id=None,
        claimed_at=None,
        last_activity_at=None,
    )


def _completed():
    return SimpleNamespace(
        conclusion="failure", run_url="https://ci/1", run_name="ci", repo=REPO, number=12, head_sha="a" * 40
    )


async def test_a_ci_completion_under_a_live_lease_starts_no_run(ci_dispatch):
    holder = uuid.uuid4()
    ci_dispatch["leases"]["rows"] = [(f"dev:branch-lock:{BRANCH}", {"pr": 12, "repository": REPO}, holder)]
    ci_dispatch["leases"]["alive"] = {holder}
    row = _ci_row("open")
    ci_dispatch["install"](row)

    what = await ci_dispatch["dispatch"](row.id, _completed(), NOW)

    assert what.startswith("not dispatched: run ") and f"dev:branch-lock:{BRANCH}" in what
    assert ci_dispatch["triggered"] == []
    # Neither claimed nor recorded: it stays open for the inbox pass after the release.
    assert row.status == "open" and row.claimed_by_agent_id is None
    assert ci_dispatch["recorded"] == []


async def test_a_blocked_item_under_a_live_lease_goes_back_to_the_queue_unclaimed(ci_dispatch):
    holder = uuid.uuid4()
    ci_dispatch["leases"]["rows"] = [(f"dev:branch-lock:{BRANCH}", {"pr": 12, "repository": REPO}, holder)]
    ci_dispatch["leases"]["alive"] = {holder}
    row = _ci_row("blocked")
    ci_dispatch["install"](row)

    await ci_dispatch["dispatch"](row.id, _completed(), NOW)

    assert ci_dispatch["triggered"] == []
    assert row.status == "open" and row.claimed_by_agent_id is None
    assert ci_dispatch["published"] == [(row.id, {"previous_status": "blocked"})]


async def test_a_ci_completion_with_an_ended_holder_or_no_lease_dispatches_once(ci_dispatch):
    holder = uuid.uuid4()
    ci_dispatch["leases"]["rows"] = [(f"dev:branch-lock:{BRANCH}", {"pr": 12, "repository": REPO}, holder)]
    row = _ci_row("open")
    ci_dispatch["install"](row)

    what = await ci_dispatch["dispatch"](row.id, _completed(), NOW)

    assert what == "dispatched to 'Inbox': run run-1"
    assert ci_dispatch["triggered"] == [str(row.id)]
    assert row.status == "claimed"

    ci_dispatch["leases"]["rows"] = []
    row = _ci_row("open")
    ci_dispatch["install"](row)
    assert (await ci_dispatch["dispatch"](row.id, _completed(), NOW)).startswith("dispatched")
