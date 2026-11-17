"""A work item wakes up when the CI of its pull request ends, without a database.

The GitHub parser, the pure payload transitions of
``services/work_item_pull_requests.py`` (binding, CI tracking, dedupe, new
head, closed), the per-item handling with the database and the dispatch
replaced (a live holder gets a note, an idle item is dispatched, a merge
completes an item that asks for it) and the pickup state the views read.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.core.database as database
from app.integrations.providers.github.pr_events import parse_github_pull_request_event
from app.integrations.pull_request_events import (
    PullRequestEvent,
    parse_pull_request_event,
    register_pull_request_event_parser,
)
from app.services import work_item_context as ctx
from app.services import work_item_pull_requests as wipr

NOW = datetime(2026, 10, 31, 21, 0, tzinfo=UTC)
REPO = "acme/widget"
SHA = "a" * 40


def _ci(name: str, status: str, conclusion: str | None = None, *, run_id: int = 1, sha: str = SHA, number: int = 7):
    return PullRequestEvent(
        kind="ci",
        repo=REPO,
        pr_numbers=(number,),
        head_branch="feat/x",
        head_sha=sha,
        run_key=f"workflow:{name}",
        run_id=run_id,
        run_name=name,
        status=status,
        conclusion=conclusion,
        run_url=f"https://example.test/runs/{run_id}",
    )


def _bound(**extra) -> dict:
    payload = wipr.bind_pull_request({}, repo="Acme/Widget", number=7, head_branch="feat/x", head_sha=SHA)
    payload.update(extra)
    return payload


# -- The GitHub parser ---------------------------------------------------------------


def _repo_payload() -> dict:
    return {"repository": {"full_name": "Acme/Widget", "html_url": "https://github.com/Acme/Widget"}}


def test_github_workflow_run_completed_is_a_ci_event():
    payload = {
        **_repo_payload(),
        "workflow_run": {
            "id": 41,
            "workflow_id": 9,
            "name": "CI",
            "head_branch": "feat/x",
            "head_sha": SHA,
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://github.com/Acme/Widget/actions/runs/41",
            "pull_requests": [{"number": 7}],
        },
    }
    event = parse_github_pull_request_event("workflow_run.completed", payload)
    assert event is not None
    assert (event.kind, event.repo, event.pr_numbers, event.run_key) == ("ci", REPO, (7,), "workflow:9")
    assert (event.status, event.conclusion, event.run_id) == ("completed", "success", 41)


def test_github_actions_check_suites_are_left_to_workflow_run():
    suite = {"id": 3, "app": {"slug": "github-actions"}, "head_sha": SHA, "status": "completed"}
    assert parse_github_pull_request_event("check_suite.completed", {**_repo_payload(), "check_suite": suite}) is None
    other = {**suite, "app": {"slug": "ci-app", "id": 5, "name": "CI app"}, "conclusion": "failure"}
    event = parse_github_pull_request_event("check_suite.completed", {**_repo_payload(), "check_suite": other})
    assert event is not None
    assert (event.run_key, event.conclusion) == ("check_suite:5", "failure")
    assert event.run_url == f"https://github.com/Acme/Widget/commit/{SHA}/checks"
    # An app's suite can complete before the head's workflows exist: advisory (#11486).
    assert event.advisory is True


def test_github_workflow_run_carries_its_attempt():
    run = {"id": 41, "workflow_id": 9, "name": "CI", "head_sha": SHA, "status": "completed", "conclusion": "failure"}
    event = parse_github_pull_request_event(
        "workflow_run.completed", {**_repo_payload(), "workflow_run": {**run, "run_attempt": 2}}
    )
    assert event is not None and event.run_attempt == 2


def test_github_pull_request_closed_and_synchronize():
    pull = {"number": 7, "merged": True, "merge_commit_sha": "m" * 40, "html_url": "https://x", "head": {"ref": "feat/x", "sha": SHA}}
    closed = parse_github_pull_request_event("pull_request.closed", {**_repo_payload(), "pull_request": pull})
    assert closed is not None
    assert (closed.kind, closed.merged, closed.merge_sha) == ("closed", True, "m" * 40)
    pushed = parse_github_pull_request_event("pull_request.synchronize", {**_repo_payload(), "pull_request": pull})
    assert pushed is not None and pushed.kind == "head"
    assert parse_github_pull_request_event("pull_request.opened", {**_repo_payload(), "pull_request": pull}) is None
    assert parse_github_pull_request_event("issues.opened", _repo_payload()) is None


def test_github_draft_toggles_are_head_events_that_carry_the_draft_flag():
    pull = {"number": 7, "draft": False, "head": {"ref": "feat/x", "sha": SHA}}
    ready = parse_github_pull_request_event("pull_request.ready_for_review", {**_repo_payload(), "pull_request": pull})
    assert ready is not None and (ready.kind, ready.draft) == ("head", False)
    drafted = parse_github_pull_request_event(
        "pull_request.converted_to_draft", {**_repo_payload(), "pull_request": {**pull, "draft": True}}
    )
    assert drafted is not None and (drafted.kind, drafted.draft) == ("head", True)
    # A closing says nothing about the draft; a payload without the flag neither.
    closed = parse_github_pull_request_event("pull_request.closed", {**_repo_payload(), "pull_request": pull})
    assert closed is not None and closed.draft is None
    bare = {k: v for k, v in pull.items() if k != "draft"}
    pushed = parse_github_pull_request_event("pull_request.synchronize", {**_repo_payload(), "pull_request": bare})
    assert pushed is not None and pushed.draft is None


def test_the_binding_records_the_draft_and_a_head_event_keeps_it_current():
    payload = wipr.bind_pull_request({}, repo=REPO, number=7, head_sha=SHA, draft=True)
    assert payload["pull_requests"][0]["draft"] is True
    # Not said: the binding keeps what it knew.
    payload = wipr.bind_pull_request(payload, repo=REPO, number=7, url="https://x/7")
    assert payload["pull_requests"][0]["draft"] is True
    ready = PullRequestEvent(kind="head", repo=REPO, pr_numbers=(7,), head_sha=SHA, draft=False)
    payload, _ = wipr.apply_pull_request_event(payload, ready, NOW)
    assert payload["pull_requests"][0]["draft"] is False
    silent = PullRequestEvent(kind="head", repo=REPO, pr_numbers=(7,), head_sha="b" * 40)
    payload, _ = wipr.apply_pull_request_event(payload, silent, NOW)
    assert payload["pull_requests"][0]["draft"] is False


def test_a_parser_that_raises_never_fails_the_delivery():
    def broken(_key, _payload):
        raise KeyError("boom")

    register_pull_request_event_parser("broken-host", broken)
    assert parse_pull_request_event("broken-host", "workflow_run.completed", {}) is None
    assert parse_pull_request_event("no-such-host", "workflow_run.completed", {}) is None


# -- The binding ---------------------------------------------------------------------


def test_bind_pull_request_normalises_and_updates_in_place():
    payload = wipr.bind_pull_request({"keep": 1}, repo="Acme/Widget", number=7)
    payload = wipr.bind_pull_request(payload, repo="acme/widget", number=7, url="https://x/7")
    assert payload["keep"] == 1
    assert payload["pull_requests"] == [{"repo": REPO, "number": 7, "state": "open", "url": "https://x/7"}]
    with pytest.raises(ValueError):
        wipr.bind_pull_request({}, repo="not-a-repo", number=7)
    full = {"pull_requests": [{"repo": REPO, "number": n} for n in range(1, wipr.MAX_PULL_REQUESTS + 1)]}
    with pytest.raises(ValueError):
        wipr.bind_pull_request(full, repo=REPO, number=999)


def test_bind_pull_request_at_the_cap_evicts_the_pull_request_closed_first():
    cap = wipr.MAX_PULL_REQUESTS
    full = {
        "pull_requests": [
            {"repo": REPO, "number": n, "state": "merged", "closed_at": f"2026-11-0{n % 9 + 1}T00:00:00+00:00"}
            for n in range(1, cap + 1)
        ]
    }
    payload = wipr.bind_pull_request(full, repo=REPO, number=999)
    numbers = [b["number"] for b in payload["pull_requests"]]
    # n=9 has the earliest closed_at (2026-11-01): it leaves, the order of the rest is kept.
    assert len(numbers) == cap
    assert 9 not in numbers
    assert numbers[-1] == 999
    assert numbers[:-1] == [n for n in range(1, cap + 1) if n != 9]
    assert payload["pull_requests"][-1]["state"] == "open"
    assert len(full["pull_requests"]) == cap  # the caller's payload is untouched


def test_bind_pull_request_evicts_an_unstamped_closed_binding_before_a_stamped_one_and_never_an_open_one():
    cap = wipr.MAX_PULL_REQUESTS
    entries = [{"repo": REPO, "number": n, "state": "open"} for n in range(1, cap - 1)]
    entries.append({"repo": REPO, "number": 100, "state": "merged", "closed_at": "2026-11-01T00:00:00+00:00"})
    entries.append({"repo": REPO, "number": 101, "state": "closed"})
    payload = wipr.bind_pull_request({"pull_requests": entries}, repo=REPO, number=999)
    numbers = [b["number"] for b in payload["pull_requests"]]
    assert 101 not in numbers
    assert 100 in numbers
    assert all(n in numbers for n in range(1, cap - 1))
    # The next one takes the last finished slot; then only open ones are left and it refuses.
    payload = wipr.bind_pull_request(payload, repo=REPO, number=1000)
    assert 100 not in [b["number"] for b in payload["pull_requests"]]
    with pytest.raises(ValueError, match="open pull requests"):
        wipr.bind_pull_request(payload, repo=REPO, number=1001)


def test_bind_pull_request_updates_a_named_pull_request_at_the_cap_without_evicting():
    full = {
        "pull_requests": [{"repo": REPO, "number": n, "state": "merged"} for n in range(1, wipr.MAX_PULL_REQUESTS + 1)]
    }
    payload = wipr.bind_pull_request(full, repo=REPO, number=3, url="https://x/3")
    assert [b["number"] for b in payload["pull_requests"]] == list(range(1, wipr.MAX_PULL_REQUESTS + 1))


# -- CI tracking and dedupe -----------------------------------------------------------


def test_github_a_pull_request_target_run_is_advisory():
    run = {"id": 41, "workflow_id": 9, "name": "Feature branch deploy", "head_sha": SHA, "status": "completed"}
    target = parse_github_pull_request_event(
        "workflow_run.completed",
        {**_repo_payload(), "workflow_run": {**run, "event": "pull_request_target", "conclusion": "success"}},
    )
    own = parse_github_pull_request_event(
        "workflow_run.completed",
        {**_repo_payload(), "workflow_run": {**run, "event": "pull_request", "conclusion": "success"}},
    )
    assert target is not None and target.advisory is True
    assert own is not None and own.advisory is False


def _advisory(conclusion: str = "success", *, run_id: int = 5) -> PullRequestEvent:
    event = _ci("Feature branch deploy", "completed", conclusion, run_id=run_id)
    return PullRequestEvent(**{**event.__dict__, "advisory": True})


def test_a_head_whose_only_completed_run_is_advisory_is_not_green():
    payload, outcomes = wipr.apply_pull_request_event(_bound(), _advisory(), NOW)
    assert outcomes == []
    # A skipped app check beside it (a draft skip) does not make it green either.
    skip = _ci("Aikido", "completed", "skipped", run_id=6)
    skip = PullRequestEvent(**{**skip.__dict__, "run_key": "check_suite:aikido"})
    payload, outcomes = wipr.apply_pull_request_event(payload, skip, NOW)
    assert outcomes == []
    # The head's own CI completes green: now it is.
    payload, outcomes = wipr.apply_pull_request_event(payload, _ci("CI", "completed", "success", run_id=7), NOW)
    assert [o.conclusion for o in outcomes] == ["success"]


def test_an_advisory_run_that_fails_still_decides_failure():
    _, outcomes = wipr.apply_pull_request_event(_bound(), _advisory("failure"), NOW)
    assert [o.conclusion for o in outcomes] == ["failure"]


def test_ci_completes_only_when_every_known_run_completed():
    payload, out = wipr.apply_pull_request_event(_bound(), _ci("lint", "in_progress"), NOW)
    assert out == []
    payload, out = wipr.apply_pull_request_event(payload, _ci("tests", "in_progress", run_id=2), NOW)
    payload, out = wipr.apply_pull_request_event(payload, _ci("lint", "completed", "success"), NOW)
    assert out == []
    payload, out = wipr.apply_pull_request_event(payload, _ci("tests", "completed", "success", run_id=2), NOW)
    assert len(out) == 1
    done = out[0]
    assert isinstance(done, wipr.CiCompleted)
    assert (done.conclusion, done.number, done.head_sha) == ("success", 7, SHA)


def test_a_failed_run_decides_at_once_and_names_its_run():
    payload, _ = wipr.apply_pull_request_event(_bound(), _ci("tests", "in_progress", run_id=2), NOW)
    payload, out = wipr.apply_pull_request_event(payload, _ci("lint", "completed", "failure", run_id=3), NOW)
    assert len(out) == 1
    assert out[0].conclusion == "failure"
    assert out[0].run_url == "https://example.test/runs/3"


def test_a_repeated_delivery_dispatches_once():
    payload, out = wipr.apply_pull_request_event(_bound(), _ci("lint", "completed", "success"), NOW)
    assert len(out) == 1
    payload, again = wipr.apply_pull_request_event(payload, _ci("lint", "completed", "success"), NOW)
    assert again == []


def test_an_older_run_and_a_late_delivery_for_an_old_head_are_ignored():
    payload, _ = wipr.apply_pull_request_event(_bound(), _ci("lint", "in_progress", run_id=10), NOW)
    payload, out = wipr.apply_pull_request_event(payload, _ci("lint", "completed", "failure", run_id=9), NOW)
    assert out == []
    payload, out = wipr.apply_pull_request_event(payload, _ci("lint", "completed", "failure", run_id=11, sha="b" * 40), NOW)
    assert out == []


def test_a_new_head_forgets_the_ci_of_the_old_one():
    payload, _ = wipr.apply_pull_request_event(_bound(), _ci("lint", "completed", "failure"), NOW)
    head = PullRequestEvent(kind="head", repo=REPO, pr_numbers=(7,), head_branch="feat/x", head_sha="c" * 40)
    payload, out = wipr.apply_pull_request_event(payload, head, NOW)
    assert out == []
    binding = wipr.pull_request_bindings(payload)[0]
    assert binding["head_sha"] == "c" * 40
    assert binding["ci"] == {"head_sha": "c" * 40}
    payload, out = wipr.apply_pull_request_event(payload, _ci("lint", "completed", "success", run_id=5, sha="c" * 40), NOW)
    assert [o.conclusion for o in out] == ["success"]


def test_an_event_for_another_pull_request_changes_nothing():
    payload = _bound()
    other = _ci("lint", "completed", "success", number=8)
    other = PullRequestEvent(**{**other.__dict__, "head_branch": "feat/other"})
    assert wipr.apply_pull_request_event(payload, other, NOW) == (payload, [])


def test_closed_marks_the_binding_and_says_whether_every_pull_request_merged():
    closed = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(7,), merged=True, merge_sha="m" * 40)
    payload, out = wipr.apply_pull_request_event(_bound(), closed, NOW)
    assert wipr.pull_request_bindings(payload)[0]["state"] == "merged"
    assert len(out) == 1 and out[0].all_merged is True
    two = wipr.bind_pull_request(_bound(), repo=REPO, number=8)
    _, out = wipr.apply_pull_request_event(two, closed, NOW)
    assert out[0].all_merged is False


# -- One wake per CI event (#11486) ---------------------------------------------------


def _attempt(event: PullRequestEvent, attempt: int) -> PullRequestEvent:
    return PullRequestEvent(**{**event.__dict__, "run_attempt": attempt})


def test_a_duplicate_delivery_is_dropped_and_counted():
    payload, out = wipr.apply_pull_request_event(_bound(), _ci("lint", "completed", "failure"), NOW)
    assert [o.conclusion for o in out] == ["failure"]
    payload, again = wipr.apply_pull_request_event(payload, _ci("lint", "completed", "failure"), NOW)
    assert again == []
    assert wipr.pull_request_bindings(payload)[0]["ci"]["dropped"] == 1


def test_a_second_workflow_failing_or_cancelled_on_a_red_head_does_not_wake_again():
    payload, out = wipr.apply_pull_request_event(_bound(), _ci("lint", "completed", "cancelled"), NOW)
    assert [o.conclusion for o in out] == ["failure"]
    payload, out = wipr.apply_pull_request_event(payload, _ci("tests", "in_progress", run_id=2), NOW)
    assert out == []
    payload, out = wipr.apply_pull_request_event(payload, _ci("tests", "completed", "failure", run_id=2), NOW)
    assert out == []


def test_an_app_check_suite_before_the_workflow_run_concluded_is_not_green():
    suite = PullRequestEvent(
        **{**_ci("Aikido", "completed", "success", run_id=6).__dict__, "run_key": "check_suite:9", "advisory": True}
    )
    payload, out = wipr.apply_pull_request_event(_bound(), suite, NOW)
    assert out == []
    payload, out = wipr.apply_pull_request_event(payload, _ci("CI", "in_progress", run_id=7), NOW)
    assert out == []
    # A completed delivery that names no conclusion yet is not concluded either.
    payload, out = wipr.apply_pull_request_event(payload, _ci("CI", "completed", None, run_id=7), NOW)
    assert out == []
    payload, out = wipr.apply_pull_request_event(payload, _ci("CI", "completed", "success", run_id=7), NOW)
    assert [o.conclusion for o in out] == ["success"]


def test_a_rerun_is_a_new_key_and_an_older_attempt_is_ignored():
    payload, out = wipr.apply_pull_request_event(_bound(), _attempt(_ci("lint", "completed", "failure"), 1), NOW)
    assert len(out) == 1
    payload, out = wipr.apply_pull_request_event(payload, _attempt(_ci("lint", "in_progress"), 2), NOW)
    assert out == []
    # The re-run fails again: a new event, one wake.
    payload, out = wipr.apply_pull_request_event(payload, _attempt(_ci("lint", "completed", "failure"), 2), NOW)
    assert [o.conclusion for o in out] == ["failure"] and out[0].dispatch_key.endswith("|workflow:lint#1.2")
    # A late delivery of attempt 1 changes nothing.
    payload, out = wipr.apply_pull_request_event(payload, _attempt(_ci("lint", "completed", "failure"), 1), NOW)
    assert out == []
    payload, out = wipr.apply_pull_request_event(payload, _attempt(_ci("lint", "completed", "failure"), 2), NOW)
    assert out == []
    # A third attempt that ends green wakes it once.
    payload, out = wipr.apply_pull_request_event(payload, _attempt(_ci("lint", "completed", "success"), 3), NOW)
    assert [o.conclusion for o in out] == ["success"]


# -- One item, with the database and the dispatch replaced ---------------------------


class _Result:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class _Session:
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        return _Result(self.row)

    async def commit(self):
        return None


def _row(status: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        payload=payload,
        claim_notes=[],
        claimed_by_run_id=uuid.uuid4(),
        last_activity_at=None,
        result=None,
    )


@pytest.fixture
def world(monkeypatch):
    state: dict = {"dispatched": [], "alive": True}

    def install(row):
        monkeypatch.setattr(database, "async_session", lambda: _Session(row))

    async def holder_alive(_db, _item):
        return state["alive"]

    async def dispatch(item_id, done, _now):
        state["dispatched"].append((item_id, done.conclusion))
        return "dispatched to 'Inbox': run 1"

    monkeypatch.setattr(wipr, "_holder_alive", holder_alive)
    monkeypatch.setattr(wipr, "_dispatch", dispatch)
    state["install"] = install
    return state


async def test_a_live_holder_gets_a_note_not_a_second_run(world):
    row = _row("in_progress", _bound())
    world["install"](row)
    actions = await wipr._apply_to_item(row.id, _ci("lint", "completed", "success"), NOW)
    assert actions == ["noted"]
    assert world["dispatched"] == []
    assert row.claim_notes and row.claim_notes[-1]["text"].startswith("CI completed: success on acme/widget#7")


async def test_an_open_item_is_dispatched_with_the_conclusion(world):
    row = _row("open", _bound())
    world["install"](row)
    actions = await wipr._apply_to_item(row.id, _ci("lint", "completed", "failure"), NOW)
    assert actions == ["dispatched"]
    assert world["dispatched"] == [(row.id, "failure")]
    assert "dispatched to 'Inbox'" in row.claim_notes[-1]["text"]


async def test_a_held_item_whose_run_ended_is_dispatched(world):
    world["alive"] = False
    row = _row("in_progress", _bound())
    world["install"](row)
    assert await wipr._apply_to_item(row.id, _ci("lint", "completed", "success"), NOW) == ["dispatched"]


async def test_a_merge_completes_an_item_that_asks_for_it(world):
    closed = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(7,), merged=True, merge_sha="m" * 40)
    row = _row("in_progress", _bound(complete_on_merge=True))
    world["install"](row)
    assert await wipr._apply_to_item(row.id, closed, NOW) == ["completed"]
    assert row.status == "done"
    assert "m" * 40 in row.result

    row = _row("open", _bound())
    world["install"](row)
    assert await wipr._apply_to_item(row.id, closed, NOW) == ["noted"]
    assert row.status == "open"
    assert "merged at" in row.claim_notes[-1]["text"]


def _parked(payload: dict) -> SimpleNamespace:
    row = _row("blocked", payload)
    row.blocked_on = None
    row.blocked_on_refs = None
    row.needs_human = None
    row.blocked_reason = "WAITS FOR: CI on acme/widget#7, then its merge"
    row.description = "brief"
    row.claimed_by_agent_id = uuid.uuid4()
    row.claimed_at = NOW
    return row


@pytest.fixture
def transitions(monkeypatch):
    from app.services import work_item as work_item_service

    seen: list = []

    async def publish(item_id, **kwargs):
        seen.append((item_id, kwargs))

    monkeypatch.setattr(work_item_service, "publish_transition", publish)
    return seen


async def test_an_item_parked_on_its_pull_request_is_released_when_it_merges(world, transitions):
    closed = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(7,), merged=True, merge_sha="m" * 40)
    row = _parked(_bound())
    world["install"](row)
    assert await wipr._apply_to_item(row.id, closed, NOW) == ["noted", "released"]
    assert row.status == "open"
    assert row.blocked_reason is None and row.claimed_by_run_id is None and row.claimed_by_agent_id is None
    assert row.description.startswith("released: acme/widget#7 merged")
    assert row.description.endswith("brief")
    assert transitions and transitions[0][1]["previous_status"] == "blocked"


async def test_a_parked_item_with_another_open_pull_request_stays_blocked(world, transitions):
    closed = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(7,), merged=True, merge_sha="m" * 40)
    row = _parked(wipr.bind_pull_request(_bound(), repo=REPO, number=8))
    world["install"](row)
    assert await wipr._apply_to_item(row.id, closed, NOW) == ["noted"]
    assert row.status == "blocked"
    assert transitions == []


async def test_an_item_with_a_waker_is_not_released_by_the_closing(world, transitions):
    closed = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(7,), merged=False)
    for field, value in (
        ("blocked_on", [str(uuid.uuid4())]),
        ("needs_human", True),
        ("blocked_on_refs", [{"kind": "issue", "repo": REPO, "number": 3, "release_on": "closed"}]),
    ):
        row = _parked(_bound())
        setattr(row, field, value)
        world["install"](row)
        assert await wipr._apply_to_item(row.id, closed, NOW) == ["noted"], field
        assert row.status == "blocked", field
    assert transitions == []


def test_released_on_closing_wants_a_closing_and_no_open_pull_request():
    closed = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(7,), merged=False)
    payload, out = wipr.apply_pull_request_event(_bound(), closed, NOW)
    assert wipr.released_on_closing(payload, out) is True
    assert "closed without merge" in wipr.closing_release_note(out, NOW)
    payload, out = wipr.apply_pull_request_event(_bound(), _ci("lint", "completed", "success"), NOW)
    assert wipr.released_on_closing(payload, out) is False


async def test_an_item_blocked_on_other_items_gets_a_note_not_a_dispatch(world):
    row = _row("blocked", _bound())
    row.blocked_on = [str(uuid.uuid4())]
    world["install"](row)
    assert await wipr._apply_to_item(row.id, _ci("lint", "completed", "success"), NOW) == ["noted"]
    assert world["dispatched"] == []
    assert row.status == "blocked"
    assert "stays blocked until the items it waits for are done" in row.claim_notes[-1]["text"]


async def test_a_finished_item_is_left_alone(world):
    row = _row("done", _bound())
    world["install"](row)
    assert await wipr._apply_to_item(row.id, _ci("lint", "completed", "success"), NOW) == []


async def test_handle_never_raises(monkeypatch):
    async def broken(_db, _event):
        raise RuntimeError("database down")

    monkeypatch.setattr(database, "async_session", lambda: _Session(None))
    monkeypatch.setattr(wipr, "_candidate_ids", broken)
    assert await wipr.handle_pull_request_event(_ci("lint", "completed", "success")) == {
        "items": 0,
        "actions": [],
        "released": {},
    }


# -- What the views say -------------------------------------------------------------


def _item(status: str, payload: dict) -> dict:
    return {"id": str(uuid.uuid4()), "status": status, "owner_agent_id": str(uuid.uuid4()), "payload": payload, "scheduled": None}


def test_an_item_whose_pull_request_ci_runs_waits_for_ci_not_for_a_pickup():
    running, _ = wipr.apply_pull_request_event(_bound(), _ci("lint", "in_progress"), NOW)
    for status in ("open", "blocked"):
        assert ctx.pickup_state(_item(status, running), [], NOW) == "waiting_for_ci"
    assert wipr.waiting_for_ci(_item("open", running)) == {"repo": REPO, "number": 7, "url": None}
    assert "waiting_for_ci" in ctx.PICKUP_STATES

    finished, _ = wipr.apply_pull_request_event(running, _ci("lint", "completed", "success"), NOW)
    assert ctx.pickup_state(_item("blocked", finished), [], NOW) == "blocked"
    assert ctx.pickup_state(_item("in_progress", running), [], NOW) == "in_progress"
    assert ctx.pickup_state(_item("open", {}), [], NOW) == "manual"
    # Nothing is dispatched to an item with no owner: it says so.
    assert ctx.pickup_state({**_item("open", running), "owner_agent_id": None}, [], NOW) == "unassigned"


def test_every_open_pull_request_whose_ci_runs_is_listed_in_payload_order():
    payload = _bound()  # #7, its head bound: its CI completes below
    payload = wipr.bind_pull_request(payload, repo=REPO, number=8, url="https://example.test/pr/8")
    payload = wipr.bind_pull_request(payload, repo="acme/other", number=9)
    payload = wipr.bind_pull_request(payload, repo="acme/other", number=10)
    payload["pull_requests"][2]["state"] = "merged"
    payload, _ = wipr.apply_pull_request_event(payload, _ci("lint", "completed", "success"), NOW)
    item = _item("open", payload)
    # #7 finished, #9 merged; #8 and #10 have no CI yet: they wait for their first run.
    assert wipr.pull_requests_waiting_for_ci(item) == [
        {"repo": REPO, "number": 8, "url": "https://example.test/pr/8"},
        {"repo": "acme/other", "number": 10, "url": None},
    ]
    assert wipr.waiting_for_ci(item) == {"repo": REPO, "number": 8, "url": "https://example.test/pr/8"}
    assert wipr.pull_requests_waiting_for_ci(_item("open", {})) == []
    # A payload written by hand that names one pull request twice counts it once.
    twice = {"pull_requests": [{"repo": "Acme/Widget", "number": 8}, {"repo": "acme/widget", "number": "8"}]}
    assert wipr.pull_requests_waiting_for_ci(_item("open", twice)) == [{"repo": REPO, "number": 8, "url": None}]
    # The first entry wins, as in the chips: a later open duplicate of a closed PR does not wait.
    closed_first = {"pull_requests": [{"repo": REPO, "number": 8, "state": "closed"}, {"repo": REPO, "number": 8}]}
    assert wipr.pull_requests_waiting_for_ci(_item("open", closed_first)) == []


def test_an_item_blocked_on_other_items_reads_so_even_while_its_ci_runs():
    running, _ = wipr.apply_pull_request_event(_bound(), _ci("lint", "in_progress"), NOW)
    blocker = str(uuid.uuid4())
    assert ctx.pickup_state({**_item("blocked", running), "blocked_on": [blocker]}, [], NOW) == "blocked_on_items"
    # Plain blocked with running CI still waits for CI; blocked_on alone without CI reads blocked_on_items.
    assert ctx.pickup_state(_item("blocked", running), [], NOW) == "waiting_for_ci"
    assert ctx.pickup_state({**_item("blocked", {}), "blocked_on": [blocker]}, [], NOW) == "blocked_on_items"
