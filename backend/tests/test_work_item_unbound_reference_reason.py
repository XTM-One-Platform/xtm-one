"""A blocked item never waits on a pull request it only names in its reason (#11045).

A reason is text that no event reads: ``WAITS FOR: filigran-ui #516`` with no
binding, no reference, no blocker and no person would stay blocked after the
merge. The write is refused with the fields that would release it.
"""

from __future__ import annotations

import pytest

from app.services import work_item_pull_requests as wipr
from app.services.work_item_blocked_refs import reason_names_reference, refuse_unbound_reference_reason


@pytest.mark.parametrize(
    ("reason", "named"),
    [
        ("WAITS FOR: filigran-ui #516 released", "filigran-ui #516"),
        ("waits for FiligranHQ/filigran-ui#516 to merge", "FiligranHQ/filigran-ui#516"),
        ("WAITS FOR: PR #10884, released by its merge", "PR #10884"),
        ("waits for the pull request 12", "pull request 12"),
        ("waits for xtm-one #10911 to deploy", "xtm-one #10911"),
    ],
)
def test_a_reason_naming_a_pull_request_is_read(reason, named):
    assert reason_names_reference(reason) == named


@pytest.mark.parametrize(
    "reason",
    [None, "", "waits for Sam's decision", "the UX round 2 verdict", "step #3 of the plan", "3 items left"],
)
def test_a_reason_naming_none_is_not_read(reason):
    assert reason_names_reference(reason) is None


def test_a_reason_alone_naming_a_pull_request_is_refused_and_names_blocked_on_refs():
    with pytest.raises(ValueError, match="blocked_on_refs"):
        refuse_unbound_reference_reason("blocked", blocked_reason="WAITS FOR: filigran-ui #516", payload={})


@pytest.mark.parametrize(
    "fields",
    [
        {"blocked_on": ["0b9f2c1e-3c3d-4a3e-9c1e-2a4b5c6d7e8f"]},
        {"needs_human": True},
        {"blocked_on_refs": [{"repo": "a/b", "kind": "pull_request", "number": 516, "release_on": "merged"}]},
        {"payload": wipr.bind_pull_request({}, repo="a/b", number=516)},
    ],
)
def test_a_reason_with_a_waker_is_accepted(fields):
    refuse_unbound_reference_reason("blocked", blocked_reason="WAITS FOR: a/b#516", **fields)


def test_a_satisfied_reference_or_a_closed_binding_is_no_waker():
    refs = [{"repo": "a/b", "kind": "pull_request", "number": 3, "release_on": "merged", "state": "merged"}]
    with pytest.raises(ValueError):
        refuse_unbound_reference_reason("blocked", blocked_reason="WAITS FOR: a/b#4", blocked_on_refs=refs)
    payload = wipr.bind_pull_request({}, repo="a/b", number=4)
    payload["pull_requests"][0]["state"] = "merged"
    with pytest.raises(ValueError):
        refuse_unbound_reference_reason("blocked", blocked_reason="WAITS FOR: a/b#4", payload=payload)


def test_only_a_blocked_status_is_checked():
    refuse_unbound_reference_reason("in_progress", blocked_reason="WAITS FOR: a/b#4")
    refuse_unbound_reference_reason("blocked", blocked_reason="waits for the UX verdict")
