"""A blocked work item always says why, and a pull request or issue releases it.

Unit level: the reference normaliser, the refusal of a blocked status with no
why, the derived ``blocked_why`` the views show, the GitHub parser of an
issue closed, and the pure matcher over the three cases met in the fleet
(a prerequisite pull request merged, the item's own pull request closed and
replaced, an issue closed as a duplicate) plus a pull request closed unmerged
that a reference wanted merged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.integrations.providers.github.pr_events import parse_github_pull_request_event
from app.integrations.pull_request_events import PullRequestEvent
from app.services.work_item_blocked_refs import (
    RELEASE_GRACE_SECONDS,
    WHY_FIELDS_MESSAGE,
    BlockedRefError,
    all_satisfied,
    apply_reference_event,
    blocked_why,
    bound_pull_request_wait,
    due_deferral,
    normalize_blocked_on_refs,
    record_closing_on_payload,
    refuse_blocked_without_why,
    refuse_needs_human_without_ask,
    release_not_before,
    release_note,
    still_waiting_note,
    unmet_note,
)

NOW = datetime(2026, 11, 3, 23, 30, tzinfo=UTC)
REPO = "xtm-one-platform/xtm-one"


def _pr_closed(number: int, *, merged: bool) -> PullRequestEvent:
    return PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(number,), merged=merged)


def _issue_closed(number: int) -> PullRequestEvent:
    return PullRequestEvent(kind="issue_closed", repo=REPO, pr_numbers=(number,))


# -- normaliser -------------------------------------------------------------------


def test_references_are_normalised_with_their_defaults():
    refs = normalize_blocked_on_refs(
        [
            {"repo": "XTM-One-Platform/xtm-one", "number": 12},
            {"repo": "XTM-One-Platform/xtm-one", "kind": "issue", "number": "#8"},
            {"repo": "XTM-One-Platform/xtm-one", "number": 12, "release_on": "merged"},
        ]
    )
    assert refs == [
        {"repo": REPO, "kind": "pull_request", "number": 12, "release_on": "merged_or_closed"},
        {"repo": REPO, "kind": "issue", "number": 8, "release_on": "closed"},
    ]


@pytest.mark.parametrize(
    "entry",
    [
        {"repo": "xtm-one", "number": 1},
        {"repo": "o/n", "number": 0},
        {"repo": "o/n", "number": 1, "kind": "commit"},
        {"repo": "o/n", "number": 1, "release_on": "approved"},
        {"repo": "o/n", "number": 1, "kind": "issue", "release_on": "merged"},
        "o/n#1",
    ],
)
def test_a_reference_the_platform_cannot_match_is_refused(entry):
    with pytest.raises(BlockedRefError):
        normalize_blocked_on_refs([entry])


# -- the refusal and the views ------------------------------------------------------


_OWNER = "9c1142f6-d1e1-434a-9099-1c63e3286156"
_ASK = {
    "question": "Merge it?",
    "options": [{"key": "A", "label": "merge"}, {"key": "B", "label": "wait"}],
    "recommendation": "A",
}


def test_a_blocked_status_with_no_why_is_refused_naming_the_three_options() -> None:
    with pytest.raises(ValueError) as exc:
        refuse_blocked_without_why("blocked")
    for field in ("owner_agent_id", "blocked_on", "blocked_on_refs", "needs_human", "ask", "not_before"):
        assert field in str(exc.value)
    assert str(exc.value).startswith(WHY_FIELDS_MESSAGE)
    assert "no owner_agent_id" in str(exc.value) and "nothing was written" in str(exc.value)


@pytest.mark.parametrize(
    "fields",
    [
        {"blocked_on": ["a"]},
        {"blocked_on_refs": [{"repo": REPO, "number": 1}]},
        {"needs_human": True, "ask": _ASK},
    ],
)
def test_an_owner_and_one_real_blocker_is_enough(fields: dict[str, Any]) -> None:
    refuse_blocked_without_why("blocked", owner_agent_id=_OWNER, **fields)


@pytest.mark.parametrize(
    ("fields", "missing"),
    [
        # Free text is context, never the blocker (#762).
        ({"owner_agent_id": _OWNER, "blocked_reason": "waits for the partner retest"}, "no blocker"),
        # Never ownerless, whatever it waits for.
        ({"blocked_on": ["a"]}, "no owner_agent_id"),
        # A person's decision carries its card: a question, two options, a recommendation.
        ({"owner_agent_id": _OWNER, "needs_human": True}, "without an ask card"),
        ({"owner_agent_id": _OWNER, "needs_human": True, "ask": {"question": "Merge it?"}}, "without an ask card"),
        (
            {"owner_agent_id": _OWNER, "needs_human": True, "ask": {**_ASK, "recommendation": None}},
            "without an ask card",
        ),
    ],
)
def test_blocked_is_black_or_white(fields: dict[str, Any], missing: str) -> None:
    with pytest.raises(ValueError, match=missing):
        refuse_blocked_without_why("blocked", **fields)


_BOUND_DRAFT = {"pull_requests": [{"repo": REPO, "number": 12, "state": "open", "draft": True}]}


def test_an_open_bound_pull_request_is_a_blocker_its_ci_releases() -> None:
    # The inbox finalizer parks a draft between two slices on its own pull
    # request (#10692): its CI completion hands it back, so it is not free text.
    refuse_blocked_without_why(
        "blocked", owner_agent_id=_OWNER, blocked_reason="WAITS FOR: draft CI", payload=_BOUND_DRAFT
    )


def test_a_closed_bound_pull_request_releases_nothing() -> None:
    closed = {"pull_requests": [{"repo": REPO, "number": 12, "state": "closed"}]}
    with pytest.raises(ValueError, match="no blocker"):
        refuse_blocked_without_why("blocked", owner_agent_id=_OWNER, blocked_reason="waits", payload=closed)
    with pytest.raises(ValueError, match="no owner_agent_id"):
        refuse_blocked_without_why("blocked", payload=_BOUND_DRAFT)


def test_only_a_blocked_status_needs_a_why():
    refuse_blocked_without_why("in_progress")
    refuse_blocked_without_why("open", blocked_reason="deferred")


def test_needs_human_needs_its_ask_card() -> None:
    refuse_needs_human_without_ask(None, None)
    refuse_needs_human_without_ask(True, _ASK)
    with pytest.raises(ValueError, match="needs_human needs an ask card"):
        refuse_needs_human_without_ask(True, None)
    with pytest.raises(ValueError, match="at least 2"):
        refuse_needs_human_without_ask(
            True, {"question": "Merge it?", "options": [{"key": "A"}], "recommendation": "A"}
        )


def test_the_views_show_what_a_blocked_item_waits_for():
    item = SimpleNamespace(
        status="blocked",
        blocked_on=None,
        blocked_on_refs=[{"repo": REPO, "kind": "pull_request", "number": 12, "release_on": "merged"}],
        blocked_reason="the UX verdict",
        needs_human=None,
        action_mode=None,
    )
    assert blocked_why(item) == {
        "text": f"Waits for: {REPO}#12 (merged); the UX verdict",
        "missing": False,
    }
    assert blocked_why({"status": "blocked", "blocked_on": ["a", "b"]})["text"] == "Waits for: 2 work item(s)"
    assert blocked_why({"status": "blocked", "needs_human": True})["text"] == "Waits for: a person (needs_human)"


def test_a_draft_parked_on_its_own_pull_request_shows_what_releases_it() -> None:
    # The inbox finalizer's park of a draft: a reason, no ref, an open bound pull request (#1617).
    item = {
        "status": "blocked",
        "blocked_reason": "WAITS FOR: o/r#12 (draft) to finish its review gate",
        "payload": {"pull_requests": [{"repo": "o/r", "number": 12, "state": "open", "draft": True}]},
    }
    assert bound_pull_request_wait(item) == "the CI completion or closing of o/r#12 (draft), released by itself"
    why = blocked_why(item)
    assert why is not None
    assert why["text"].startswith(
        "Waits for: the CI completion or closing of o/r#12 (draft), released by itself; WAITS FOR"
    )
    # A closed binding, another blocker or an item not blocked: nothing to say.
    closed = {**item, "payload": {"pull_requests": [{"repo": "o/r", "number": 12, "state": "closed"}]}}
    assert bound_pull_request_wait(closed) is None
    assert bound_pull_request_wait({**item, "blocked_on": ["x"]}) is None
    assert bound_pull_request_wait({**item, "needs_human": True}) is None
    ref = {"repo": REPO, "kind": "pull_request", "number": 9, "release_on": "merged"}
    assert bound_pull_request_wait({**item, "blocked_on_refs": [ref]}) is None
    assert bound_pull_request_wait({**item, "status": "open"}) is None


def test_a_legacy_blocked_item_with_no_reason_is_flagged_missing():
    assert blocked_why({"status": "blocked"}) == {"text": "No reason given", "missing": True}
    assert blocked_why({"status": "open"}) is None
    # A scheduled item's blocked state has its own view.
    assert blocked_why({"status": "blocked", "action_mode": "exact"}) is None


# -- the GitHub parser ---------------------------------------------------------------


def test_github_issue_closed_is_an_issue_closed_event():
    event = parse_github_pull_request_event(
        "issues.closed",
        {
            "action": "closed",
            "repository": {"full_name": "XTM-One-Platform/xtm-one"},
            "issue": {"number": 8, "html_url": "https://github.com/x/8", "state_reason": "not_planned"},
        },
    )
    assert event is not None
    assert (event.kind, event.repo, event.pr_numbers) == ("issue_closed", REPO, (8,))


def test_a_pull_request_seen_as_an_issue_is_not_an_issue_event():
    event = parse_github_pull_request_event(
        "issues.closed",
        {
            "action": "closed",
            "repository": {"full_name": "XTM-One-Platform/xtm-one"},
            "issue": {"number": 9, "pull_request": {"url": "x"}},
        },
    )
    assert event is None


# -- the matcher: the three cases of the fleet ------------------------------------------


def test_a_prerequisite_pull_request_merged_releases_the_item():
    refs = normalize_blocked_on_refs([{"repo": REPO, "number": 100, "release_on": "merged"}])
    out, released, refused = apply_reference_event(refs, _pr_closed(100, merged=True), NOW)
    assert released == [f"{REPO}#100 merged"] and refused == []
    assert all_satisfied(out)


def test_its_own_pull_request_closed_and_replaced_releases_the_item():
    refs = normalize_blocked_on_refs([{"repo": REPO, "number": 101}])  # merged_or_closed
    out, released, _ = apply_reference_event(refs, _pr_closed(101, merged=False), NOW)
    assert released == [f"{REPO}#101 closed"]
    assert all_satisfied(out)


def test_an_issue_closed_as_duplicate_releases_the_item():
    refs = normalize_blocked_on_refs([{"repo": REPO, "kind": "issue", "number": 102}])
    out, released, _ = apply_reference_event(refs, _issue_closed(102), NOW)
    assert released == [f"{REPO} issue #102 closed"]
    assert all_satisfied(out)


def test_a_pull_request_closed_unmerged_keeps_a_merged_reference_waiting():
    refs = normalize_blocked_on_refs([{"repo": REPO, "number": 103, "release_on": "merged"}])
    out, released, refused = apply_reference_event(refs, _pr_closed(103, merged=False), NOW)
    assert released == []
    assert refused == [f"{REPO}#103 closed, release_on merged"]
    assert not all_satisfied(out)


def test_a_superseded_pull_request_note_names_the_closure_and_asks_for_a_re_point():
    note = unmet_note([f"{REPO}#103 closed, release_on merged"], [], "2026-11-03T23:10:00Z", NOW)
    assert note == (
        f"surfaced: {REPO}#103 closed, release_on merged at 2026-11-03T23:10:00Z, so it can never release "
        "this item; re-point blocked_on_refs (the successor pull request) or complete it; "
        "surfaced at 2026-11-03T23:30:00Z"
    )


def test_a_superseded_pull_request_note_lists_what_is_still_pending():
    note = unmet_note([f"{REPO}#103 closed, release_on merged"], [f"{REPO}#104 (merged)"], None, NOW)
    assert f"release_on merged, so it can never release this item; still pending: {REPO}#104 (merged);" in note


def test_a_deployment_reference_whose_pull_request_closed_unmerged_is_refused():
    refs = normalize_blocked_on_refs([{"repo": REPO, "kind": "deployment", "number": 109, "release_on": "deployed"}])
    out, released, refused = apply_reference_event(refs, _pr_closed(109, merged=False), NOW)
    assert released == []
    assert refused == [f"deployment of {REPO}#109: {REPO}#109 closed unmerged"]
    assert not all_satisfied(out)


def test_one_reference_of_two_leaves_the_item_waiting():
    refs = normalize_blocked_on_refs([{"repo": REPO, "number": 104}, {"repo": REPO, "number": 105}])
    out, released, _ = apply_reference_event(refs, _pr_closed(104, merged=True), NOW)
    assert released == [f"{REPO}#104 merged"]
    assert not all_satisfied(out)
    out, released, _ = apply_reference_event(out, _pr_closed(105, merged=True), NOW)
    assert all_satisfied(out)


def test_an_issue_number_never_matches_a_pull_request_reference():
    refs = normalize_blocked_on_refs([{"repo": REPO, "number": 106}])
    out, released, refused = apply_reference_event(refs, _issue_closed(106), NOW)
    assert (released, refused) == ([], [])
    assert not all_satisfied(out)


def test_another_repository_never_matches():
    refs = normalize_blocked_on_refs([{"repo": "other/repo", "number": 107}])
    _, released, _ = apply_reference_event(refs, _pr_closed(107, merged=True), NOW)
    assert released == []


def test_ci_and_head_events_are_not_reference_events():
    refs = normalize_blocked_on_refs([{"repo": REPO, "number": 108}])
    for kind in ("ci", "head"):
        _, released, refused = apply_reference_event(
            refs, PullRequestEvent(kind=kind, repo=REPO, pr_numbers=(108,)), NOW
        )
        assert (released, refused) == ([], [])


# -- the payload binding records the closing the release applied --------------------


def test_a_release_marks_the_payload_pull_request_merged_so_the_run_end_does_not_park_it_again():
    from app.services.work_item import open_pull_requests

    payload = {"pull_requests": [{"repo": REPO, "number": 3788, "state": "open"}], "other": 1}
    event = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(3788,), merged=True, merge_sha="abc123")
    out = record_closing_on_payload(payload, event, NOW)
    assert out is not None
    binding = out["pull_requests"][0]
    assert (binding["state"], binding["merge_sha"]) == ("merged", "abc123")
    assert out["other"] == 1
    assert payload["pull_requests"][0]["state"] == "open"  # pure: the input is untouched
    assert open_pull_requests(SimpleNamespace(payload=out)) == []


def test_a_release_closed_unmerged_marks_the_binding_closed():
    payload = {"pull_requests": [{"repo": REPO, "number": 120}]}
    out = record_closing_on_payload(payload, _pr_closed(120, merged=False), NOW)
    assert out is not None and out["pull_requests"][0]["state"] == "closed"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"pull_requests": [{"repo": REPO, "number": 121}]},
        {"pull_requests": [{"repo": "other/repo", "number": 122}]},
    ],
)
def test_a_payload_not_naming_the_closed_pull_request_is_left_alone(payload):
    assert record_closing_on_payload(payload, _pr_closed(122, merged=True), NOW) is None


def test_an_issue_closing_never_touches_the_pull_request_bindings():
    payload = {"pull_requests": [{"repo": REPO, "number": 123}]}
    assert record_closing_on_payload(payload, _issue_closed(123), NOW) is None


# -- the notes: the host's merge time, then the release time -----------------

RELEASED_AT = datetime(2026, 11, 4, 1, 22, 19, tzinfo=UTC)


def test_the_release_note_carries_the_merge_time_and_the_release_time():
    note = release_note([f"{REPO}#8306 merged"], "2026-11-04T00:59:04Z", RELEASED_AT)
    assert note == f"released: {REPO}#8306 merged at 2026-11-04T00:59:04Z; released at 2026-11-04T01:22:19Z"


def test_the_release_note_states_one_time_when_both_are_the_same_second():
    note = release_note([f"{REPO}#8306 merged"], "2026-11-04T01:22:19+00:00", RELEASED_AT)
    assert note == f"released: {REPO}#8306 merged at 2026-11-04T01:22:19Z"


@pytest.mark.parametrize("closed_at", [None, "", "not a time"])
def test_without_a_host_time_the_release_time_is_never_the_merge_time(closed_at):
    note = release_note([f"{REPO} issue #8 closed"], closed_at, RELEASED_AT)
    assert note == f"released: {REPO} issue #8 closed; released at 2026-11-04T01:22:19Z"


def test_a_release_leaves_the_merging_run_a_grace_before_the_inbox_takes_the_item():
    at = release_not_before(None, RELEASED_AT)
    assert at == RELEASED_AT + timedelta(seconds=RELEASE_GRACE_SECONDS)
    assert due_deferral(at, RELEASED_AT) == "; the inbox takes it from its not_before (2026-11-04T01:23:04Z)"


def test_a_release_keeps_a_later_not_before_of_the_item():
    later = RELEASED_AT + timedelta(hours=2)
    assert release_not_before(later, RELEASED_AT) == later
    assert release_not_before(later.replace(tzinfo=None), RELEASED_AT) == later


def test_a_release_replaces_a_past_not_before_with_the_grace():
    past = RELEASED_AT - timedelta(days=1)
    assert release_not_before(past, RELEASED_AT) == RELEASED_AT + timedelta(seconds=RELEASE_GRACE_SECONDS)


def test_the_still_waiting_note_carries_the_merge_time():
    assert (
        still_waiting_note([f"{REPO}#8306 merged"], "2026-11-04T00:59:04Z", RELEASED_AT)
        == f"{REPO}#8306 merged at 2026-11-04T00:59:04Z (seen 2026-11-04T01:22:19Z); still waiting"
    )
    assert (
        still_waiting_note([f"{REPO}#8306 merged"], None, RELEASED_AT)
        == f"{REPO}#8306 merged (seen 2026-11-04T01:22:19Z); still waiting"
    )


def test_github_pull_request_closed_carries_merged_at_or_closed_at():
    def parse(pull: dict):
        return parse_github_pull_request_event(
            "pull_request.closed",
            {"action": "closed", "repository": {"full_name": "XTM-One-Platform/xtm-one"}, "pull_request": pull},
        )

    merged = parse(
        {"number": 1, "merged": True, "merged_at": "2026-11-04T00:59:04Z", "closed_at": "2026-11-04T00:59:05Z"}
    )
    assert merged is not None and merged.closed_at == "2026-11-04T00:59:04Z"
    closed = parse({"number": 2, "merged": False, "merged_at": None, "closed_at": "2026-11-04T00:10:00Z"})
    assert closed is not None and closed.closed_at == "2026-11-04T00:10:00Z"


def test_github_issue_closed_carries_closed_at():
    event = parse_github_pull_request_event(
        "issues.closed",
        {
            "action": "closed",
            "repository": {"full_name": "XTM-One-Platform/xtm-one"},
            "issue": {"number": 8, "closed_at": "2026-11-04T00:30:00Z"},
        },
    )
    assert event is not None and event.closed_at == "2026-11-04T00:30:00Z"
