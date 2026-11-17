"""An item deferred on an unchanged red is woken by the next CI conclusion of its pull requests (#11185).

The deferral of #11163 waits for "the next CI conclusion": the CI poll reads
such an open item, a re-run that ends with the same conclusion on the same
head counts as new (the deciding check run is read), and a red that did not
move keeps the item deferred.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.integrations.providers.github.reference_state import head_ci_decision
from app.services import work_item as wi_service
from app.services import work_item_pull_requests as wipr

REPO = "acme/widget"
SHA = "8" * 40
NOW = datetime(2026, 11, 11, 16, 40, tzinfo=UTC)
RELEASED = NOW - timedelta(minutes=30)
UNTIL = RELEASED + wi_service.RED_UNCHANGED_DEFER


def _payload(**extra) -> dict:
    payload = wipr.bind_pull_request({}, repo=REPO, number=7, head_branch="fix/x", head_sha=SHA, draft=True)
    payload["pull_requests"][0]["ci"] = {
        "head_sha": SHA,
        "runs": {"workflow:1": {"run_id": 1, "status": "completed", "conclusion": "failure"}},
        "dispatched": [f"{SHA}:failure:workflow:1"],
    }
    payload.update(extra)
    return payload


def _deferred_item(**fields) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "status": "open",
        "blocked_on": None,
        "blocked_on_refs": None,
        "needs_human": None,
        "not_before": UNTIL,
        "payload": _payload(**{wi_service.RED_DEFERRED_UNTIL_KEY: UNTIL.isoformat()}),
        "claim_notes": [],
        "last_activity_at": RELEASED,
    }
    base.update(fields)
    return SimpleNamespace(**base)


def _state(conclusion: str, *, check_id: int, completed_at: datetime) -> dict:
    return {
        "head_sha": SHA,
        "head_branch": "fix/x",
        "draft": True,
        "conclusion": conclusion,
        "run_url": f"https://x/{check_id}",
        "check_id": check_id,
        "completed_at": completed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# -- The deciding check run of a head --------------------------------------------------


def test_the_decision_names_the_last_check_run_to_decide():
    data = {
        "check_runs": [
            {
                "id": 3,
                "name": "a",
                "status": "completed",
                "conclusion": "success",
                "completed_at": "2026-11-11T16:20:00Z",
            },
            {
                "id": 9,
                "name": "b",
                "status": "completed",
                "conclusion": "success",
                "completed_at": "2026-11-11T16:50:34Z",
            },
        ]
    }
    conclusion, _, decider = head_ci_decision(data)
    assert conclusion == "success" and decider == {"check_id": 9, "completed_at": "2026-11-11T16:50:34Z"}


def test_an_undecided_head_names_no_decider():
    data = {"check_runs": [{"id": 3, "name": "a", "status": "in_progress", "conclusion": None}]}
    assert head_ci_decision(data) == (None, None, None)


# -- Which items the poll reads --------------------------------------------------------


def test_an_item_deferred_on_an_unchanged_red_is_read():
    item = _deferred_item()
    assert wipr.red_deferred(item) is True and wipr.ci_poll_candidate(item) is True
    assert wipr.red_deferral_start(item) == RELEASED


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "claimed"},
        {"blocked_on": [str(uuid.uuid4())]},
        {"payload": _payload()},  # open, no deferral: the queue takes it
        {"payload": {wi_service.RED_DEFERRED_UNTIL_KEY: UNTIL.isoformat()}},  # no pull request
    ],
)
def test_an_open_item_not_deferred_on_a_red_is_not_read(fields):
    assert wipr.ci_poll_candidate(_deferred_item(**fields)) is False


# -- The payload transition -------------------------------------------------------------


def test_a_rerun_ending_red_again_after_the_release_is_a_new_conclusion():
    state = _state("failure", check_id=40, completed_at=NOW - timedelta(minutes=10))
    payload, done = wipr.apply_polled_ci(_payload(), repo=REPO, number=7, state=state, newer_than=RELEASED)
    assert done is not None and done.conclusion == "failure"
    assert payload["pull_requests"][0]["ci"]["runs"][wipr.POLLED_RUN_KEY]["run_id"] is not None
    # The same read again owes nothing.
    _, again = wipr.apply_polled_ci(payload, repo=REPO, number=7, state=state, newer_than=RELEASED)
    assert again is None


def test_an_unchanged_red_stays_deferred_and_keeps_its_signature():
    state = _state("failure", check_id=1, completed_at=RELEASED - timedelta(minutes=20))
    before = _payload()
    payload, done = wipr.apply_polled_ci(before, repo=REPO, number=7, state=state, newer_than=RELEASED)
    assert done is None
    assert payload["pull_requests"][0]["ci"]["runs"] == before["pull_requests"][0]["ci"]["runs"]


def test_a_rerun_with_the_same_conclusion_on_a_parked_item_counts_as_new():
    first = _state("success", check_id=10, completed_at=NOW - timedelta(minutes=30))
    payload, done = wipr.apply_polled_ci(_payload(), repo=REPO, number=7, state=first)
    assert done is not None
    rerun = _state("success", check_id=11, completed_at=NOW)
    payload, again = wipr.apply_polled_ci(payload, repo=REPO, number=7, state=rerun)
    assert again is not None and again.dispatch_key != done.dispatch_key
    _, third = wipr.apply_polled_ci(payload, repo=REPO, number=7, state=rerun)
    assert third is None


def test_a_read_without_a_decider_keeps_the_previous_rule():
    state = {"head_sha": SHA, "conclusion": "failure"}
    _, done = wipr.apply_polled_ci(_payload(), repo=REPO, number=7, state=state, newer_than=RELEASED)
    assert done is None


# -- The pass ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, item):
        self.item = item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        return _Result([self.item])

    async def commit(self):
        return None


@pytest.fixture
def wired(monkeypatch):
    import app.core.database as database
    import app.integrations.reference_state as reference_state
    import app.services.work_item_blocked_refs as blocked_refs

    item = _deferred_item()
    monkeypatch.setattr(database, "async_session", lambda: _Session(item))
    dispatched: list = []

    async def fake_dispatch(item_id, done, now):
        # The real dispatch clears the deferral (work_item.clear_red_deferral).
        wi_service.clear_red_deferral(item)
        dispatched.append(done)
        return "dispatched to 'inbox': run r1"

    async def token(_db, _provider, _user_id):
        return "t"

    state = _state("success", check_id=50, completed_at=NOW - timedelta(minutes=1))

    async def reader(repo, number, _token):
        return dict(state) if (repo, number) == (REPO, 7) else None

    monkeypatch.setattr(wipr, "_dispatch", fake_dispatch)
    monkeypatch.setattr(blocked_refs, "reference_token", token)
    monkeypatch.setattr(wipr, "_ci_polled", {})
    monkeypatch.setattr(reference_state, "_PULL_REQUEST_CI_READERS", {"github": reader})
    return SimpleNamespace(item=item, dispatched=dispatched, state=state)


@pytest.mark.asyncio
async def test_a_green_wakes_the_deferred_item_in_one_pass(wired):
    counts = await wipr.poll_pull_request_ci(NOW)
    assert counts["dispatched"] == 1 and len(wired.dispatched) == 1
    assert wired.item.not_before is None
    assert wi_service.RED_DEFERRED_UNTIL_KEY not in wired.item.payload


@pytest.mark.asyncio
async def test_an_unchanged_red_leaves_the_deferred_item_alone(wired):
    wired.state.update(_state("failure", check_id=1, completed_at=RELEASED - timedelta(minutes=20)))
    counts = await wipr.poll_pull_request_ci(NOW)
    assert counts["read"] == 1 and counts["dispatched"] == 0 and wired.dispatched == []
    assert wired.item.not_before == UNTIL
