"""An item never waits on the merge of its own red pull request (#9418).

A red head merges only once the item's owner pushes a fix. Parked on that
merge, the item has nothing left to wake it: the CI of the head already
decided. Three guards: the CI record stamps when a head turned red, the
inbox finalizer puts such an item back in the queue instead of parking it,
``update_work_item`` / ``create_work_item`` refuse the wait, and the scheduler
releases an item left that way for :data:`RED_SELF_BLOCK_SECONDS`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.integrations.pull_request_events import PullRequestEvent
from app.services import work_item_blocked_refs as refs_mod
from app.services.work_item_pull_requests import apply_pull_request_event, red_pull_requests

REPO = "owner/name"
NOW = datetime(2026, 11, 6, 23, 0, tzinfo=UTC)


def _binding(conclusion: str | None, *, head="abc", ci_head="abc", state="open", failed_at=None) -> dict:
    runs = {} if conclusion is None else {"ci": {"status": "completed", "conclusion": conclusion}}
    ci: dict = {"head_sha": ci_head, "runs": runs}
    if failed_at:
        ci["failed_at"] = failed_at
    return {"repo": REPO, "number": 12, "state": state, "head_sha": head, "ci": ci}


def _ref(**extra) -> dict:
    return {"repo": REPO, "kind": "pull_request", "number": 12, "release_on": "merged_or_closed", **extra}


# -- which pull requests are red ------------------------------------------------------


def test_a_failed_head_is_red_and_a_green_or_running_one_is_not():
    assert [b["number"] for b in red_pull_requests({"pull_requests": [_binding("failure")]})] == [12]
    assert red_pull_requests({"pull_requests": [_binding("success")]}) == []
    assert red_pull_requests({"pull_requests": [_binding(None)]}) == []


def test_a_failure_of_an_older_head_or_a_closed_pull_request_is_not_red():
    assert red_pull_requests({"pull_requests": [_binding("failure", head="new", ci_head="old")]}) == []
    assert red_pull_requests({"pull_requests": [_binding("failure", state="merged")]}) == []
    assert red_pull_requests(None) == []


def test_the_ci_record_stamps_when_the_head_turned_red_and_clears_it_on_green():
    payload = {"pull_requests": [{"repo": REPO, "number": 12, "head_sha": "abc"}]}
    red = PullRequestEvent(
        kind="ci", repo=REPO, pr_numbers=(12,), head_sha="abc", run_key="ci", status="completed", conclusion="failure"
    )
    out, _ = apply_pull_request_event(payload, red, NOW)
    assert red_pull_requests(out)[0]["failed_at"] == NOW.isoformat()
    # A second red delivery keeps the first time.
    out, _ = apply_pull_request_event(out, red, NOW + timedelta(minutes=5))
    assert red_pull_requests(out)[0]["failed_at"] == NOW.isoformat()
    green = PullRequestEvent(
        kind="ci", repo=REPO, pr_numbers=(12,), head_sha="abc", run_key="ci", status="completed", conclusion="success"
    )
    out, _ = apply_pull_request_event(out, green, NOW + timedelta(minutes=9))
    assert red_pull_requests(out) == []
    assert "failed_at" not in out["pull_requests"][0]["ci"]


# -- the write guard ------------------------------------------------------------------


def test_blocking_on_the_merge_of_an_own_red_pull_request_is_refused():
    payload = {"pull_requests": [_binding("failure")]}
    with pytest.raises(ValueError, match="red on its head: the item would wait forever"):
        refs_mod.refuse_blocked_on_red_pull_request("blocked", blocked_on_refs=[_ref()], payload=payload)


def test_the_guard_lets_through_a_green_head_another_waker_or_a_merged_deployment():
    red = {"pull_requests": [_binding("failure")]}
    refs_mod.refuse_blocked_on_red_pull_request(
        "blocked", blocked_on_refs=[_ref()], payload={"pull_requests": [_binding("success")]}
    )
    refs_mod.refuse_blocked_on_red_pull_request(
        "blocked", blocked_on_refs=[_ref()], payload=red, blocked_on=[str(uuid.uuid4())]
    )
    merged = {"repo": REPO, "kind": "deployment", "number": 12, "release_on": "deployed", "merge_sha": "def"}
    refs_mod.refuse_blocked_on_red_pull_request("blocked", blocked_on_refs=[merged], payload=red)
    other = _ref(number=13)
    refs_mod.refuse_blocked_on_red_pull_request("blocked", blocked_on_refs=[other], payload=red)
    refs_mod.refuse_blocked_on_red_pull_request("in_progress", blocked_on_refs=[_ref()], payload=red)


def test_a_deployment_not_merged_yet_waits_on_the_red_merge_too():
    red = {"pull_requests": [_binding("failure")]}
    pending = {"repo": REPO, "kind": "deployment", "number": 12, "release_on": "deployed"}
    with pytest.raises(ValueError):
        refs_mod.refuse_blocked_on_red_pull_request("blocked", blocked_on_refs=[pending], payload=red)


# -- the release after 30 minutes -----------------------------------------------------


def test_the_release_is_due_only_after_the_red_window():
    stuck = refs_mod._own_red_refs([_ref()], {"pull_requests": [_binding("failure", failed_at=NOW.isoformat())]})
    assert stuck
    window = timedelta(seconds=refs_mod.RED_SELF_BLOCK_SECONDS)
    assert not refs_mod.red_self_block_due(stuck, None, NOW + window - timedelta(minutes=1))
    assert refs_mod.red_self_block_due(stuck, None, NOW + window)


def test_a_failure_recorded_before_the_stamp_counts_from_the_last_activity():
    stuck = refs_mod._own_red_refs([_ref()], {"pull_requests": [_binding("failure")]})
    window = timedelta(seconds=refs_mod.RED_SELF_BLOCK_SECONDS)
    assert refs_mod.red_self_block_due(stuck, NOW - window, NOW)
    assert not refs_mod.red_self_block_due(stuck, NOW, NOW)
    assert not refs_mod.red_self_block_due([], NOW - window, NOW)


class _Rows:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


class _Session:
    def __init__(self, items):
        self.items = items
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        return _Rows(self.items)

    async def commit(self):
        self.committed = True


def _blocked_item(**overrides):
    base = dict(
        id=uuid.uuid4(),
        status="blocked",
        blocked_on=None,
        blocked_on_refs=[_ref()],
        blocked_reason="waits for the merge",
        needs_human=None,
        payload={"pull_requests": [_binding("failure", failed_at=(NOW - timedelta(hours=3)).isoformat())]},
        description="brief",
        claim_notes=None,
        claimed_by_agent_id=uuid.uuid4(),
        claimed_by_run_id=uuid.uuid4(),
        claimed_at=NOW - timedelta(hours=3),
        last_activity_at=NOW - timedelta(hours=3),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def release(monkeypatch):
    published: list = []

    async def _surfaced(item_id, note):
        published.append((item_id, note))

    monkeypatch.setattr(refs_mod, "_publish_surfaced", _surfaced)

    async def _run(items):
        import app.core.database as database

        session = _Session(items)
        monkeypatch.setattr(database, "async_session", lambda: session)
        count = await refs_mod.release_red_self_blocks(NOW)
        return count, session

    _run.published = published
    return _run


async def test_an_item_stuck_on_its_own_red_pull_request_goes_back_to_the_queue(release):
    item = _blocked_item()
    count, session = await release([item])
    assert count == 1 and session.committed
    assert item.status == "open"
    assert item.blocked_on_refs is None and item.blocked_reason is None
    assert item.claimed_by_agent_id is None and item.claimed_by_run_id is None and item.claimed_at is None
    assert item.description.startswith("released: owner/name#12 red on its head for more than 30 minutes")
    assert item.description.endswith("brief")
    assert release.published == [(item.id, item.claim_notes[-1]["text"])]


async def test_an_item_with_another_waker_stays_blocked(release):
    on_items = _blocked_item(blocked_on=[str(uuid.uuid4())])
    other_ref = _blocked_item(blocked_on_refs=[_ref(), _ref(number=13)])
    recent = _blocked_item(
        payload={"pull_requests": [_binding("failure", failed_at=(NOW - timedelta(minutes=5)).isoformat())]}
    )
    green = _blocked_item(payload={"pull_requests": [_binding("success")]})
    count, _ = await release([on_items, other_ref, recent, green])
    assert count == 0
    assert {i.status for i in (on_items, other_ref, recent, green)} == {"blocked"}
