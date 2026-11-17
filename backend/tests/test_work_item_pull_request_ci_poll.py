"""An item parked on a pull request is handed back when its CI completed with no delivery (#10692).

The GitHub verdict of a head's check runs, the pure payload transition of a
live read (a new head made by an update-branch merge included), which items
the pass reads, and the pass itself with the database, the reader and the
dispatch replaced.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.integrations.providers.github.reference_state import head_ci_verdict
from app.services import work_item_pull_requests as wipr

REPO = "acme/widget"
OLD = "c" * 40
NEW = "4" * 40
NOW = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)


def _payload(**binding) -> dict:
    payload = wipr.bind_pull_request({}, repo="Acme/Widget", number=7, head_branch="feat/x", head_sha=OLD, draft=True)
    payload["pull_requests"][0].update(binding)
    return payload


def _run(name: str, status: str = "completed", conclusion: str | None = "success", *, run_id: int = 1) -> dict:
    return {"id": run_id, "name": name, "status": status, "conclusion": conclusion, "html_url": f"https://x/{run_id}"}


# -- The verdict of a head's check runs -------------------------------------------


def test_every_check_completed_and_passing_is_success():
    data = {"total_count": 3, "check_runs": [_run("a"), _run("b", conclusion="skipped", run_id=2), _run("c", run_id=3)]}
    assert head_ci_verdict(data)[0] == "success"


def test_one_running_check_is_not_decided():
    data = {"check_runs": [_run("a"), _run("b", status="in_progress", conclusion=None, run_id=2)]}
    assert head_ci_verdict(data) == (None, None)


def test_one_failed_check_decides_failure_with_its_url():
    data = {"check_runs": [_run("a"), _run("b", conclusion="failure", run_id=2), _run("c", status="queued", run_id=3)]}
    assert head_ci_verdict(data) == ("failure", "https://x/2")


def test_a_rerun_replaces_the_run_it_repeats():
    data = {"check_runs": [_run("a", conclusion="failure", run_id=1), _run("a", conclusion="success", run_id=9)]}
    assert head_ci_verdict(data)[0] == "success"


def test_a_head_whose_required_checks_have_not_reported_is_not_decided():
    # Only the pull_request_target deploy and a draft skip reported; the base requires the CI verdict.
    data = {"check_runs": [_run("deploy"), _run("Aikido", conclusion="skipped", run_id=2)]}
    required = frozenset({"CI verdict", "Secret scan"})
    assert head_ci_verdict(data, required) == (None, None)
    data["check_runs"] += [_run("CI verdict", run_id=3), _run("Secret scan", run_id=4)]
    assert head_ci_verdict(data, required)[0] == "success"


def test_a_failed_check_the_base_does_not_require_decides_nothing_before_the_required_ones_report():
    # #11766: the required checks decide alone; without them every check does.
    data = {"check_runs": [_run("deploy", conclusion="failure")]}
    assert head_ci_verdict(data, frozenset({"CI verdict"})) == (None, None)
    assert head_ci_verdict(data)[0] == "failure"


def test_no_check_or_a_partial_page_is_not_decided():
    assert head_ci_verdict({"check_runs": []}) == (None, None)
    assert head_ci_verdict({"total_count": 150, "check_runs": [_run("a")]}) == (None, None)


# -- The payload transition of a live read -----------------------------------------


def test_a_green_new_head_from_an_update_branch_merge_owes_one_dispatch():
    state = {"head_sha": NEW, "head_branch": "feat/x", "draft": True, "conclusion": "success", "run_url": "u"}
    payload, done = wipr.apply_polled_ci(_payload(), repo=REPO, number=7, state=state)
    binding = payload["pull_requests"][0]
    assert binding["head_sha"] == NEW
    assert binding["ci"]["head_sha"] == NEW
    assert wipr.ci_pending(binding) is False
    assert done is not None and done.head_sha == NEW and done.conclusion == "success"
    # A second read of the same head and verdict dispatches nothing.
    again, second = wipr.apply_polled_ci(payload, repo=REPO, number=7, state=state)
    assert second is None
    assert again["pull_requests"][0]["ci"]["dispatched"] == binding["ci"]["dispatched"]


def test_a_head_a_delivery_already_dispatched_is_not_dispatched_twice():
    ci = {"head_sha": NEW, "runs": {}, "dispatched": [f"{NEW}:success:workflow:1"]}
    state = {"head_sha": NEW, "conclusion": "success"}
    _, done = wipr.apply_polled_ci(_payload(head_sha=NEW, ci=ci), repo=REPO, number=7, state=state)
    assert done is None


def test_a_running_ci_records_the_new_head_and_owes_nothing():
    state = {"head_sha": NEW, "conclusion": None}
    payload, done = wipr.apply_polled_ci(_payload(), repo=REPO, number=7, state=state)
    assert done is None
    assert payload["pull_requests"][0]["head_sha"] == NEW
    assert wipr.ci_pending(payload["pull_requests"][0]) is True


def test_a_red_head_owes_a_failure_dispatch():
    state = {"head_sha": NEW, "conclusion": "failure", "run_url": "https://x/2"}
    _, done = wipr.apply_polled_ci(_payload(), repo=REPO, number=7, state=state)
    assert done is not None and done.conclusion == "failure" and done.run_url == "https://x/2"


def test_a_pull_request_the_item_does_not_name_changes_nothing():
    payload, done = wipr.apply_polled_ci(
        _payload(), repo=REPO, number=8, state={"head_sha": NEW, "conclusion": "success"}
    )
    assert done is None and payload["pull_requests"][0]["head_sha"] == OLD


# -- Which items the pass reads -----------------------------------------------------


def _item(**fields) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "status": "blocked",
        "blocked_on": None,
        "blocked_on_refs": None,
        "needs_human": None,
        "payload": _payload(),
        "claim_notes": [],
    }
    base.update(fields)
    return SimpleNamespace(**base)


def test_a_draft_parked_with_a_reason_only_is_read():
    assert wipr.parked_without_release(_item()) is True


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "open"},
        {"blocked_on": [str(uuid.uuid4())]},
        {"needs_human": True},
        {"blocked_on_refs": [{"repo": REPO, "kind": "pull_request", "number": 7, "release_on": "merged_or_closed"}]},
        {"payload": _payload(state="merged")},
        {"payload": {}},
    ],
)
def test_an_item_something_else_releases_is_not_read(fields):
    assert wipr.parked_without_release(_item(**fields)) is False


def test_a_satisfied_reference_does_not_count_as_a_wait():
    refs = [{"repo": REPO, "kind": "pull_request", "number": 3, "release_on": "merged", "state": "merged"}]
    assert wipr.parked_without_release(_item(blocked_on_refs=refs)) is True


# -- The pass ------------------------------------------------------------------------


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
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        return _Result([self.item])

    async def commit(self):
        self.commits += 1


@pytest.fixture
def wired(monkeypatch):
    import app.core.database as database
    import app.integrations.reference_state as reference_state
    import app.services.work_item_blocked_refs as blocked_refs

    item = _item(last_activity_at=NOW - timedelta(hours=1))
    session = _Session(item)
    monkeypatch.setattr(database, "async_session", lambda: session)
    dispatched: list = []

    async def fake_dispatch(item_id, done, now):
        dispatched.append((item_id, done))
        return "dispatched to 'inbox': run r1"

    async def token(_db, _provider, _user_id):
        return "t"

    monkeypatch.setattr(wipr, "_dispatch", fake_dispatch)
    monkeypatch.setattr(blocked_refs, "reference_token", token)
    monkeypatch.setattr(wipr, "_ci_polled", {})
    state = {"head_sha": NEW, "head_branch": "feat/x", "draft": True, "conclusion": "success", "run_url": "u"}

    async def reader(repo, number, _token):
        return dict(state) if (repo, number) == (REPO, 7) else None

    monkeypatch.setattr(reference_state, "_PULL_REQUEST_CI_READERS", {"github": reader})
    return SimpleNamespace(item=item, dispatched=dispatched, state=state)


@pytest.mark.asyncio
async def test_the_pass_dispatches_a_green_draft_once_and_notes_it(wired):
    counts = await wipr.poll_pull_request_ci(NOW)
    assert counts["dispatched"] == 1
    assert len(wired.dispatched) == 1 and wired.dispatched[0][1].head_sha == NEW
    assert wired.item.payload["pull_requests"][0]["head_sha"] == NEW
    assert "read live, no CI delivery had come" in wired.item.claim_notes[-1]["text"]
    # The next pass, past the quiet window, finds the same head and verdict: nothing more.
    wipr._ci_polled.clear()
    counts = await wipr.poll_pull_request_ci(NOW)
    assert counts["dispatched"] == 0 and len(wired.dispatched) == 1


@pytest.mark.asyncio
async def test_the_pass_leaves_a_running_ci_blocked(wired):
    wired.state["conclusion"] = None
    counts = await wipr.poll_pull_request_ci(NOW)
    assert counts["read"] == 1 and counts["dispatched"] == 0 and wired.dispatched == []
    assert wired.item.status == "blocked"


# -- A closing the delivery never brought (#11045) ------------------------------------


@pytest.fixture
def closed_read(wired, monkeypatch):
    """The PR merged: the CI reader answers nothing, the state reader answers the closing."""
    import app.integrations.reference_state as reference_state
    from app.integrations.pull_request_events import PullRequestEvent

    async def ci_reader(_repo, _number, _token):
        return None  # closed: the CI reader answers nothing

    async def state_reader(repo, kind, number, _token):
        if (repo, kind, number) != (REPO, "pull_request", 7):
            return None
        return PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(7,), merged=True, merge_sha="m" * 40)

    applied: list = []

    async def apply(item_id, event, now):
        applied.append((item_id, event.kind))
        return ["noted", "released"]

    monkeypatch.setattr(reference_state, "_PULL_REQUEST_CI_READERS", {"github": ci_reader})
    monkeypatch.setattr(reference_state, "_READERS", {"github": state_reader})
    monkeypatch.setattr(wipr, "_apply_to_item", apply)
    return applied


@pytest.mark.asyncio
async def test_a_merge_with_no_delivery_goes_through_the_delivery_path(wired, closed_read):
    counts = await wipr.poll_pull_request_ci(NOW)
    assert closed_read == [(wired.item.id, "closed")]
    assert counts["released"] == 1 and counts["dispatched"] == 0


@pytest.mark.asyncio
async def test_an_open_pull_request_reads_no_closing(wired, monkeypatch):
    import app.integrations.reference_state as reference_state

    asked: list = []

    async def state_reader(*args):
        asked.append(args)
        return None

    monkeypatch.setattr(reference_state, "_READERS", {"github": state_reader})
    wired.state["conclusion"] = None
    await wipr.poll_pull_request_ci(NOW)
    assert asked == []  # the CI reader answered: the pull request is open


def test_a_second_closing_of_the_same_pull_request_changes_nothing():
    from app.integrations.pull_request_events import PullRequestEvent

    closed = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(7,), merged=True, merge_sha="m" * 40)
    payload, first = wipr.apply_pull_request_event(_payload(), closed, NOW)
    assert len(first) == 1 and payload["pull_requests"][0]["state"] == "merged"
    again, second = wipr.apply_pull_request_event(payload, closed, NOW + timedelta(minutes=55))
    assert second == [] and again == payload
    assert wipr.released_on_closing(again, second) is False


@pytest.mark.asyncio
async def test_an_item_read_within_the_quiet_window_is_not_read_again(wired):
    wired.state["conclusion"] = None
    await wipr.poll_pull_request_ci(NOW)
    counts = await wipr.poll_pull_request_ci(NOW)
    assert counts["read"] == 0
