"""A green pull request waiting on its review is parked, not put back in the queue (#2213).

The inbox run's end put a ready, green item back in the queue after the green
hand-off while its pull request waited on REVIEW_REQUIRED: the next run read
the missing review and stood down (xtm-one #418, item 703c7f5a). The live read
now carries ``review_decision`` and the settlement parks the item on the merge;
the review event (or the merge) is its next wake.
"""

from __future__ import annotations

import pytest

import app.services.work_item as wi_service
from app.integrations.providers.github import reference_state as rs
from tests.test_work_item_double_dispatch_12040 import _item, settle  # noqa: F401  (fixture)
from tests.test_work_item_merge_queue_wake_404 import REPO, _fake_github


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["REVIEW_REQUIRED", "CHANGES_REQUESTED"])
async def test_a_green_head_waiting_on_its_review_is_parked_not_requeued(settle, decision) -> None:  # noqa: F811
    settle.live({"head_sha": "abc", "conclusion": "success", "draft": False, "review_decision": decision})
    item = _item()
    out = await settle(item)
    assert out == "parked"
    assert item.status == "blocked"
    assert [(r["number"], r["release_on"]) for r in item.blocked_on_refs] == [(12, "merged_or_closed")]
    assert not any("back in the queue" in n["text"] for n in item.claim_notes)


@pytest.mark.asyncio
async def test_an_approved_green_head_still_goes_back_to_the_queue(settle) -> None:  # noqa: F811
    settle.live({"head_sha": "abc", "conclusion": "success", "draft": False, "review_decision": "APPROVED"})
    item = _item()
    assert await settle(item) == "deferred"
    assert item.status == "open"


def test_awaiting_review_reads_only_the_blocking_decisions() -> None:
    assert wi_service._awaiting_review({"review_decision": "REVIEW_REQUIRED"})
    assert wi_service._awaiting_review({"review_decision": "CHANGES_REQUESTED"})
    for read in ({"review_decision": "APPROVED"}, {}, None, "REVIEW_REQUIRED"):
        assert not wi_service._awaiting_review(read)


@pytest.mark.asyncio
async def test_the_live_read_carries_the_review_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _fake_github(monkeypatch, draft=False, queued=False)

    async def fake_gates(*_a: object, **_k: object) -> tuple[bool, str | None]:
        return False, "REVIEW_REQUIRED"

    monkeypatch.setattr(rs, "_merge_gates", fake_gates)
    state = await rs.read_pull_request_ci(REPO, 7, "token")
    assert state is not None and state["review_decision"] == "REVIEW_REQUIRED"
    assert "merge_queue" not in state
    assert asked == []


@pytest.mark.asyncio
async def test_the_merge_gates_query_asks_for_the_review_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _fake_github(monkeypatch, draft=False, queued=False)
    await rs.read_pull_request_ci(REPO, 7, "token")
    assert asked and "reviewDecision" in asked[0]
