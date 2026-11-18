"""The polling fallback of the reference release.

A repository whose webhook is not installed sends no ``pull_request`` closed
or ``issues`` closed delivery, and a reference that closed before the item
was blocked on it sends none any more. The scheduler reads the open
references of the items quiet for a while through the provider's reader and
applies a closing exactly as the delivery would have. Unit level: the GitHub
answer turned into the event, the pass over candidates (one read per
reference, an open or unreadable reference keeps the item blocked, a user
with no credential reads nothing, the per-item quiet window).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.integrations import reference_state
from app.integrations.providers.github.reference_state import closing_event
from app.integrations.pull_request_events import PullRequestEvent
from app.services import work_item_blocked_refs as refs_mod

REPO = "xtm-one-platform/xtm-one"
NOW = datetime(2026, 11, 4, 1, 0, tzinfo=UTC)


# -- the GitHub answer -------------------------------------------------------------


def test_a_merged_pull_request_is_a_merged_closing():
    event = closing_event(
        REPO,
        "pull_request",
        12,
        {"state": "closed", "merged": True, "merge_commit_sha": "abc", "html_url": "https://x/12"},
    )
    assert event == PullRequestEvent(
        kind="closed", repo=REPO, pr_numbers=(12,), merged=True, merge_sha="abc", url="https://x/12"
    )


def test_a_pull_request_closed_unmerged_is_a_plain_closing():
    event = closing_event(REPO, "pull_request", 12, {"state": "closed", "merged": False, "merge_commit_sha": "x"})
    assert event is not None and event.kind == "closed" and not event.merged and event.merge_sha is None


def test_an_open_reference_is_no_closing():
    assert closing_event(REPO, "pull_request", 12, {"state": "open"}) is None
    assert closing_event(REPO, "issue", 12, {"state": "open"}) is None
    assert closing_event(REPO, "issue", 12, "not json") is None


def test_a_closed_issue_is_an_issue_closing_and_a_pull_request_is_not_an_issue():
    event = closing_event(REPO, "issue", 8, {"state": "closed", "state_reason": "duplicate"})
    assert event is not None and (event.kind, event.pr_numbers) == ("issue_closed", (8,))
    assert event.extra == {"state_reason": "duplicate"}
    assert closing_event(REPO, "issue", 9, {"state": "closed", "pull_request": {"url": "x"}}) is None


def test_the_github_answer_carries_the_merge_or_close_time():
    merged = closing_event(
        REPO,
        "pull_request",
        12,
        {"state": "closed", "merged": True, "merged_at": "2026-11-04T00:59:04Z", "closed_at": "2026-11-04T00:59:05Z"},
    )
    assert merged is not None and merged.closed_at == "2026-11-04T00:59:04Z"
    closed = closing_event(REPO, "pull_request", 13, {"state": "closed", "closed_at": "2026-11-04T00:10:00Z"})
    assert closed is not None and closed.closed_at == "2026-11-04T00:10:00Z"
    issue = closing_event(REPO, "issue", 8, {"state": "closed", "closed_at": "2026-11-04T00:30:00Z"})
    assert issue is not None and issue.closed_at == "2026-11-04T00:30:00Z"


def test_github_registers_its_reader():
    import app.integrations.providers.github  # noqa: F401 - registers the reader

    assert "github" in reference_state.reference_state_readers()


# -- the pass ----------------------------------------------------------------------


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def harness(monkeypatch):
    """Candidates, tokens and a reader under test control; ``applied`` records the releases."""
    state: dict = {"candidates": [], "tokens": {}, "answers": {}, "reads": [], "applied": []}

    async def candidates(db, now):
        return list(state["candidates"])

    async def token(db, provider, user_id):
        return state["tokens"].get(str(user_id))

    async def reader(repo, kind, number, tok):
        state["reads"].append((repo, kind, number, tok))
        answer = state["answers"].get((repo, kind, number))
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def apply(item_id, event, now):
        state["applied"].append((item_id, event))
        return "released"

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(refs_mod, "_poll_candidates", candidates)
    monkeypatch.setattr(refs_mod, "reference_token", token)
    monkeypatch.setattr(refs_mod, "_apply_to_item", apply)
    monkeypatch.setattr(reference_state, "_READERS", {"github": reader})
    monkeypatch.setattr(refs_mod, "_last_polled", {})
    monkeypatch.setattr(refs_mod, "_budget_skipped", set())
    return state


def _ref(number: int, **extra) -> dict:
    return {"repo": REPO, "kind": "pull_request", "number": number, "release_on": "merged_or_closed", **extra}


async def test_a_reference_found_closed_releases_its_item(harness):
    user, item = uuid.uuid4(), uuid.uuid4()
    merged = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(300,), merged=True)
    harness["candidates"] = [(item, user, [_ref(300)])]
    harness["tokens"] = {str(user): "tok"}
    harness["answers"] = {(REPO, "pull_request", 300): merged}

    out = await refs_mod.poll_blocked_on_refs(NOW)

    assert out == {"read": 1, "released": 1, "noted": 0}
    assert harness["applied"] == [(item, merged)]
    assert harness["reads"] == [(REPO, "pull_request", 300, "tok")]


async def test_an_open_or_unreadable_reference_keeps_the_item_blocked(harness):
    user = uuid.uuid4()
    harness["candidates"] = [(uuid.uuid4(), user, [_ref(301)]), (uuid.uuid4(), user, [_ref(302)])]
    harness["tokens"] = {str(user): "tok"}
    harness["answers"] = {(REPO, "pull_request", 302): RuntimeError("GitHub down")}

    out = await refs_mod.poll_blocked_on_refs(NOW)

    assert out == {"read": 2, "released": 0, "noted": 0, "read_failed": 1}
    assert harness["applied"] == []


async def test_an_unreadable_reference_is_handed_to_its_item_note(harness, monkeypatch):
    user, failing = uuid.uuid4(), uuid.uuid4()
    harness["candidates"] = [(uuid.uuid4(), user, [_ref(301)]), (failing, user, [_ref(302)])]
    harness["tokens"] = {str(user): "tok"}
    harness["answers"] = {(REPO, "pull_request", 302): RuntimeError("GitHub down")}
    handed: list = []

    async def note(unread, now):
        handed.append(unread)
        return 1

    monkeypatch.setattr(refs_mod, "note_unread_references", note)
    out = await refs_mod.poll_blocked_on_refs(NOW)

    assert out["read_failed"] == 1 and out["failure_noted"] == 1 and "_unread" not in out
    assert list(handed[0]) == [failing] and "GitHub down" in handed[0][failing][0]


async def test_satisfied_references_are_not_read_and_one_read_serves_every_item(harness):
    user = uuid.uuid4()
    merged = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(303,), merged=True)
    first, second = uuid.uuid4(), uuid.uuid4()
    harness["candidates"] = [
        (first, user, [_ref(299, state="merged"), _ref(303)]),
        (second, user, [_ref(303)]),
    ]
    harness["tokens"] = {str(user): "tok"}
    harness["answers"] = {(REPO, "pull_request", 303): merged}

    out = await refs_mod.poll_blocked_on_refs(NOW)

    assert harness["reads"] == [(REPO, "pull_request", 303, "tok")]
    assert out["released"] == 2
    assert [a[0] for a in harness["applied"]] == [first, second]


async def test_an_owner_with_no_credential_reads_nothing(harness):
    harness["candidates"] = [(uuid.uuid4(), uuid.uuid4(), [_ref(304)])]

    out = await refs_mod.poll_blocked_on_refs(NOW)

    assert out == {"read": 0, "released": 0, "noted": 0}
    assert harness["reads"] == []


async def test_an_item_is_read_again_only_after_the_quiet_window(harness, monkeypatch):
    user, item = uuid.uuid4(), uuid.uuid4()
    harness["candidates"] = [(item, user, [_ref(305)])]
    harness["tokens"] = {str(user): "tok"}
    clock = {"t": 1000.0}
    monkeypatch.setattr(refs_mod.time, "monotonic", lambda: clock["t"])

    await refs_mod.poll_blocked_on_refs(NOW)
    clock["t"] += 60
    await refs_mod.poll_blocked_on_refs(NOW)
    assert len(harness["reads"]) == 1

    clock["t"] += refs_mod.POLL_QUIET_SECONDS
    await refs_mod.poll_blocked_on_refs(NOW)
    assert len(harness["reads"]) == 2


async def test_an_item_the_budget_skipped_is_not_stamped_and_goes_first(harness, monkeypatch):
    user = uuid.uuid4()
    first, starved = uuid.uuid4(), uuid.uuid4()
    harness["candidates"] = [(first, user, [_ref(400)]), (starved, user, [_ref(401)])]
    harness["tokens"] = {str(user): "tok"}
    monkeypatch.setattr(refs_mod, "POLL_MAX_READS", 1)

    out = await refs_mod.poll_blocked_on_refs(NOW)

    assert out["read"] == 1 and out["skipped_by_budget"] == 1
    assert first in refs_mod._last_polled and starved not in refs_mod._last_polled
    assert starved in refs_mod._budget_skipped

    # The next pass reads the starved item first, even though the query lists it last.
    refs_mod._last_polled.clear()
    await refs_mod.poll_blocked_on_refs(NOW)

    assert harness["reads"][-1] == (REPO, "pull_request", 401, "tok")
    assert starved in refs_mod._last_polled and starved not in refs_mod._budget_skipped


async def test_no_reader_registered_reads_nothing(harness, monkeypatch):
    monkeypatch.setattr(reference_state, "_READERS", {})
    harness["candidates"] = [(uuid.uuid4(), uuid.uuid4(), [_ref(306)])]
    assert await refs_mod.poll_blocked_on_refs(NOW) == {"read": 0, "released": 0, "noted": 0}


def test_open_refs_keeps_the_unsatisfied_ones():
    assert refs_mod.open_refs([_ref(1, state="merged"), _ref(2), "junk"]) == [_ref(2)]
    assert refs_mod.open_refs(None) == []
