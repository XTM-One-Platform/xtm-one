"""An item about pull request N never waits on another pull request (#11507).

A UX verdict item (``dev:ux-verdict-N-comment``) or a UX review item
(``ux:<repo>-N``) belongs to one pull request. Blocked on a sibling's
reference, it wakes on the sibling's event or never: the write is refused and
the refusal names the reference expected.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.integrations.pull_request_events import PullRequestEvent
from app.services import work_item_blocked_refs as refs_mod

REPO = "xtm-one-platform/xtm-one"
VERDICT_KEY = "dev:ux-verdict-12-comment"
VERDICT_TITLE = "dev:ux-verdict-12-comment: post the UX Director verdict as one comment on XTM-One-Platform/xtm-one#12"
NOW = datetime(2026, 11, 13, 4, 0, tzinfo=UTC)


def _ref(number: int, *, kind: str = "pull_request", repo: str = REPO, **extra) -> dict:
    release_on = "deployed" if kind == "deployment" else "merged_or_closed"
    return {"repo": repo, "kind": kind, "number": number, "release_on": release_on, **extra}


def _refuse(refs, *, key=VERDICT_KEY, title=VERDICT_TITLE, status="blocked"):
    refs_mod.refuse_foreign_pull_request_ref(status, correlation_key=key, title=title, blocked_on_refs=refs)


# -- which pull request an item is about -----------------------------------------------


def test_a_verdict_item_is_about_the_pull_request_its_key_and_title_name():
    assert refs_mod.own_pull_request(VERDICT_KEY, VERDICT_TITLE) == {"repo": REPO, "name": None, "number": 12}


def test_a_ux_review_item_names_its_repository_short_name_in_the_key():
    own = refs_mod.own_pull_request("ux:xtm-one-12", "UX review: xtm-one #12 on abcdef12")
    assert own == {"repo": None, "name": "xtm-one", "number": 12}
    own = refs_mod.own_pull_request("ux:gate:xtm-one-12", "gate of XTM-One-Platform/xtm-one#12")
    assert own == {"repo": REPO, "name": "xtm-one", "number": 12}


def test_other_items_are_about_no_pull_request():
    assert refs_mod.own_pull_request("dev:gap-verdict-item-pr-ref", "Gap fix 15/25") is None
    assert refs_mod.own_pull_request(None, "XTM-One-Platform/xtm-one#12") is None


# -- the write guard --------------------------------------------------------------------


def test_a_verdict_item_for_pull_request_n_blocks_on_n():
    _refuse([_ref(12)])
    _refuse([_ref(12, kind="deployment")])


def test_a_reference_to_a_sibling_pull_request_is_refused_with_the_expected_one_named():
    with pytest.raises(ValueError) as caught:
        _refuse([_ref(13)])
    message = str(caught.value)
    assert f"{REPO}#13 is not this item's pull request" in message
    assert f"is about {REPO}#12" in message
    assert '"number": 12' in message


def test_a_deployment_of_a_sibling_is_refused_too():
    with pytest.raises(ValueError, match=f"deployment of {REPO}#13"):
        _refuse([_ref(12), _ref(13, kind="deployment")])


def test_a_ux_review_item_is_checked_on_its_short_repository_name():
    with pytest.raises(ValueError, match="is about xtm-one-platform/xtm-one#12"):
        _refuse([_ref(13)], key="ux:xtm-one-12", title="UX review: xtm-one #12 on abcdef12")


def test_another_repository_an_issue_or_a_satisfied_reference_is_left_alone():
    _refuse([_ref(13, repo="xtm-foundation/filigran-design-system")])
    _refuse([{"repo": REPO, "kind": "issue", "number": 13, "release_on": "closed"}])
    _refuse([_ref(13, state="merged", at=NOW.isoformat())])


def test_a_pull_request_the_payload_binds_is_its_own_whatever_the_key_names() -> None:
    # #2242: the key names #12, the payload binds #13, the PR that carries the change.
    payload = {"pull_requests": [{"repo": REPO, "number": 13, "state": "open"}]}
    refs_mod.refuse_foreign_pull_request_ref(
        "blocked", correlation_key=VERDICT_KEY, title=VERDICT_TITLE, blocked_on_refs=[_ref(13)], payload=payload
    )
    with pytest.raises(ValueError, match="#14 is not this item's pull request"):
        refs_mod.refuse_foreign_pull_request_ref(
            "blocked", correlation_key=VERDICT_KEY, title=VERDICT_TITLE, blocked_on_refs=[_ref(14)], payload=payload
        )


def test_an_item_not_blocked_or_about_no_pull_request_is_never_refused():
    _refuse([_ref(13)], status="in_progress")
    _refuse([_ref(13)], key="dev:gap-verdict-item-pr-ref", title="Gap fix 15/25")


# -- the release follows the item's own pull request ----------------------------------------


def _closed(number: int) -> PullRequestEvent:
    return PullRequestEvent(
        kind="closed", repo=REPO, pr_numbers=(number,), merged=True, merge_sha="a" * 40, closed_at=NOW.isoformat()
    )


def test_the_item_is_released_by_its_own_pull_request_and_not_by_a_sibling():
    refs = [refs_mod.normalize_blocked_on_refs([_ref(12)])[0]]
    after_sibling, released, _ = refs_mod.apply_reference_event(refs, _closed(13), NOW)
    assert released == [] and not refs_mod.all_satisfied(after_sibling)
    after_own, released, _ = refs_mod.apply_reference_event(after_sibling, _closed(12), NOW)
    assert released and refs_mod.all_satisfied(after_own)
