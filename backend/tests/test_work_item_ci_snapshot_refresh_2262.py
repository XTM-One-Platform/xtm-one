"""An item blocked on a review keeps a live CI snapshot, not the one taken when it was blocked (#2262).

Before #2262 only items parked on their pull requests' CI were polled: an item
blocked on a reference kept the pending snapshot of its block time, so its
card read "Waiting for CI" days after the checks finished.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.services import work_item_pull_requests as prs
from app.services.work_item_context import pickup_state

_REPO = "xtm-one-platform/xtm-one"


def _payload(ci: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "pull_requests": [
            {"repo": _REPO, "number": 7, "state": "open", "head_sha": "a" * 40, "ci": ci or {"head_sha": "a" * 40}}
        ]
    }


def _blocked_on_review(payload: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        status="blocked",
        blocked_on=[],
        needs_human=False,
        blocked_on_refs=[{"kind": "pull_request", "repo": _REPO, "number": 7, "release_on": "merged"}],
        payload=payload,
    )


def test_an_item_blocked_on_a_review_with_a_pending_snapshot_is_refreshed() -> None:
    item = _blocked_on_review(_payload())
    assert not prs.ci_poll_candidate(item)
    assert prs.ci_snapshot_stale(item)


def test_an_item_parked_on_its_ci_is_the_dispatching_poll_not_the_refresh() -> None:
    item = SimpleNamespace(status="blocked", blocked_on=[], needs_human=False, blocked_on_refs=[], payload=_payload())
    assert prs.ci_poll_candidate(item)
    assert not prs.ci_snapshot_stale(item)


def test_a_live_green_read_ends_the_ci_wait_without_a_dispatch_key() -> None:
    payload = prs.record_polled_ci(
        _payload(), repo=_REPO, number=7, state={"head_sha": "a" * 40, "conclusion": "success"}
    )
    assert prs.pull_requests_waiting_for_ci({"payload": payload}) == []
    binding = payload["pull_requests"][0]
    assert binding["ci"]["runs"][prs.POLLED_RUN_KEY]["conclusion"] == "success"
    assert "dispatched" not in binding["ci"]
    assert not prs.ci_snapshot_stale(_blocked_on_review(payload))


def test_a_new_head_forgets_the_old_heads_ci() -> None:
    payload = prs.record_polled_ci(_payload(), repo=_REPO, number=7, state={"head_sha": "b" * 40})
    binding = payload["pull_requests"][0]
    assert binding["head_sha"] == "b" * 40 and binding["ci"] == {"head_sha": "b" * 40}


def test_the_view_names_the_review_wait_once_ci_is_green() -> None:
    payload = prs.record_polled_ci(
        _payload(), repo=_REPO, number=7, state={"head_sha": "a" * 40, "conclusion": "success"}
    )
    item = {
        "status": "blocked",
        "owner_agent_id": "x",
        "payload": payload,
        "blocked_on_refs": [{"kind": "pull_request", "repo": _REPO, "number": 7, "release_on": "merged"}],
    }
    assert pickup_state(item, [], datetime.now(UTC)) != "waiting_for_ci"
    stale = {**item, "payload": _payload()}
    assert pickup_state(stale, [], datetime.now(UTC)) == "waiting_for_ci"
