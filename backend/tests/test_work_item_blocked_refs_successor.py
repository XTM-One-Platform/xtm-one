"""A ``pull_request`` reference that carries ``head_branch`` follows its successors (#11187).

A changesets release workflow closes its release pull request and opens a new
number on the same head branch at every merge to main: an item blocked on the
release stays blocked on the successor and releases at its merge, instead of
being surfaced "can never release" once per regeneration.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.integrations.providers.github.reference_state import branch_closings, closing_event
from app.integrations.pull_request_events import PullRequestEvent
from app.services.work_item_blocked_refs import (
    BlockedRefError,
    all_satisfied,
    apply_reference_event,
    followed_number,
    normalize_blocked_on_refs,
    ref_label,
)

NOW = datetime(2026, 11, 11, 17, 30, tzinfo=UTC)
REPO = "xtm-foundation/filigran-design-system"
BRANCH = "changeset-release/main"


def _closed(number: int, *, merged: bool, branch: str | None = BRANCH, repo: str = REPO) -> PullRequestEvent:
    return PullRequestEvent(
        kind="closed",
        repo=repo,
        pr_numbers=(number,),
        head_branch=branch,
        merged=merged,
        merge_sha="a" * 40 if merged else None,
    )


def _ref(number: int = 306) -> list[dict]:
    return normalize_blocked_on_refs(
        [
            {
                "repo": "XTM-Foundation/filigran-design-system",
                "number": number,
                "release_on": "merged",
                "head_branch": BRANCH,
            }
        ]
    )


def test_head_branch_is_kept_on_a_pull_request_reference():
    (ref,) = _ref()
    assert ref == {"repo": REPO, "kind": "pull_request", "number": 306, "release_on": "merged", "head_branch": BRANCH}
    assert ref_label(ref) == f"{REPO}#306 (or its successor on {BRANCH})"


def test_the_tool_s_branch_field_names_the_head_branch_of_a_pull_request():
    (ref,) = normalize_blocked_on_refs([{"repo": REPO, "number": 306, "release_on": "merged", "branch": BRANCH}])
    assert ref["head_branch"] == BRANCH
    (issue,) = normalize_blocked_on_refs([{"repo": REPO, "kind": "issue", "number": 3, "branch": BRANCH}])
    assert "head_branch" not in issue


@pytest.mark.parametrize(
    "entry",
    [
        {"repo": REPO, "kind": "issue", "number": 1, "head_branch": BRANCH},
        {"repo": REPO, "number": 1, "head_branch": "bad branch"},
    ],
)
def test_head_branch_is_refused_off_a_pull_request_or_malformed(entry):
    with pytest.raises(BlockedRefError):
        normalize_blocked_on_refs([entry])


def test_superseded_numbers_survive_a_rewrite():
    (ref,) = normalize_blocked_on_refs(
        [{"repo": REPO, "number": 306, "release_on": "merged", "head_branch": BRANCH, "superseded": [306, 0, "x"]}]
    )
    assert ref["superseded"] == [306]


def test_the_release_pull_request_closed_unmerged_keeps_the_item_waiting():
    out, released, refused = apply_reference_event(_ref(), _closed(306, merged=False), NOW)
    assert refused == []
    assert released == [f"{REPO}#306 closed unmerged, waiting for its successor on {BRANCH}"]
    assert out[0]["superseded"] == [306]
    assert not all_satisfied(out)


def test_the_successor_merged_releases_the_item():
    out, _, _ = apply_reference_event(_ref(), _closed(306, merged=False), NOW)
    out, released, refused = apply_reference_event(out, _closed(308, merged=True), NOW)
    assert refused == []
    assert released == [f"{REPO}#306 (or its successor on {BRANCH}): successor #308 merged"]
    assert out[0]["merged_number"] == 308
    assert all_satisfied(out)


def test_a_chain_of_regenerations_is_followed_until_the_merge():
    out = _ref(305)
    for number in (305, 306):
        out, _, refused = apply_reference_event(out, _closed(number, merged=False), NOW)
        assert refused == []
    assert out[0]["superseded"] == [305, 306]
    out, _, _ = apply_reference_event(out, _closed(308, merged=True), NOW)
    assert all_satisfied(out)
    # The rewrite of the satisfied reference keeps what the platform recorded.
    (again,) = normalize_blocked_on_refs(out)
    assert again["state"] == "merged" and again["merged_number"] == 308


def test_the_named_pull_request_merged_releases_it_directly():
    out, released, _ = apply_reference_event(_ref(), _closed(306, merged=True), NOW)
    assert released == [f"{REPO}#306 (or its successor on {BRANCH}) merged"]
    assert "merged_number" not in out[0]
    assert all_satisfied(out)


@pytest.mark.parametrize(
    "event",
    [
        _closed(300, merged=True),  # an older number of the branch
        _closed(308, merged=True, branch="feat/other"),  # another branch
        _closed(308, merged=True, branch=None),  # a delivery that does not name its branch
        _closed(308, merged=True, repo="other/repo"),  # another repository
    ],
)
def test_only_a_later_pull_request_of_the_same_branch_and_repository_is_followed(event):
    out, released, refused = apply_reference_event(_ref(), event, NOW)
    assert (released, refused) == ([], [])
    assert not all_satisfied(out)


def test_a_superseded_number_seen_again_is_ignored():
    out, _, _ = apply_reference_event(_ref(), _closed(306, merged=False), NOW)
    assert followed_number(out[0], _closed(306, merged=False)) is None
    _, released, refused = apply_reference_event(out, _closed(306, merged=False), NOW)
    assert (released, refused) == ([], [])


def test_without_head_branch_a_superseded_pull_request_still_surfaces_the_item():
    refs = normalize_blocked_on_refs([{"repo": REPO, "number": 306, "release_on": "merged"}])
    _, released, refused = apply_reference_event(refs, _closed(306, merged=False), NOW)
    assert released == []
    assert refused == [f"{REPO}#306 closed, release_on merged"]
    # And a successor on the branch is not followed.
    _, released, _ = apply_reference_event(refs, _closed(308, merged=True), NOW)
    assert released == []


def test_merged_or_closed_on_a_branch_takes_the_first_closing():
    refs = normalize_blocked_on_refs([{"repo": REPO, "number": 306, "head_branch": BRANCH}])
    out, released, _ = apply_reference_event(refs, _closed(306, merged=False), NOW)
    assert released == [f"{REPO}#306 (or its successor on {BRANCH}) closed"]
    assert all_satisfied(out)


# -- GitHub ----------------------------------------------------------------------


def _pull(number: int, *, merged: bool, state: str = "closed", ref: str = BRANCH) -> dict:
    return {
        "number": number,
        "state": state,
        "merged_at": "2026-11-11T17:20:00Z" if merged else None,
        "closed_at": "2026-11-11T17:20:00Z" if state == "closed" else None,
        "merge_commit_sha": "b" * 40,
        "head": {"ref": ref},
    }


def test_a_read_closing_names_its_head_branch():
    event = closing_event(REPO, "pull_request", 308, _pull(308, merged=True))
    assert event is not None and event.head_branch == BRANCH and event.merged


def test_the_branch_closings_are_the_closed_pull_requests_of_that_branch_oldest_first():
    data = [
        _pull(308, merged=True),
        _pull(309, merged=False, state="open"),
        _pull(306, merged=False),
        _pull(307, merged=True, ref="feat/other"),
        "junk",
    ]
    events = branch_closings(REPO, BRANCH, data)
    assert [(e.pr_numbers, e.merged) for e in events] == [((306,), False), ((308,), True)]
    assert branch_closings(REPO, BRANCH, {"message": "Not Found"}) == []
