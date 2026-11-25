"""A merge queue removal on failed checks names where its failing queue run is read (#2155).

Gaps queue-eject-notice-no-run-id and dequeue-note-without-failing-run: the
wake and the surfaced note said "removed (failed_checks)" and nothing else, so
the owner listed workflow runs by hand (five listings in run 5eb976a8) to find
the merge group run that failed. Gap comment-attachments-xtm-assets-race:
parallel comments with attachments raced on the assets branch tip and the
loser answered an error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.integrations.providers.github import attachments
from app.integrations.providers.github.pr_events import parse_github_pull_request_event, queue_run_hint
from app.integrations.providers.github.reference_state import dequeued_from_timeline
from app.services import work_item_pull_requests as wipr
from app.services.work_item_blocked_refs import dequeued_note

REPO = "acme/widget"
SHA = "c" * 40
NOW = datetime(2026, 11, 25, 14, 30, tzinfo=UTC)


def _payload(reason: str) -> dict[str, Any]:
    return {
        "action": "dequeued",
        "reason": reason,
        "repository": {"full_name": "Acme/Widget"},
        "pull_request": {
            "number": 7,
            "merged": False,
            "updated_at": "2026-11-25T13:35:00Z",
            "html_url": "https://github.com/Acme/Widget/pull/7",
            "head": {"ref": "feat/x", "sha": SHA},
        },
    }


def test_a_check_failure_removal_carries_the_read_of_its_failing_queue_run() -> None:
    event = parse_github_pull_request_event("pull_request.dequeued", _payload("FAILED_CHECKS"))
    assert event is not None
    hint = event.extra["queue_run_hint"]
    assert 'list_github_workflow_runs(repo="acme/widget", event="merge_group", status="failure"' in hint
    assert 'created=">=2026-11-25"' in hint
    assert "list_github_workflow_jobs" in hint


@pytest.mark.parametrize("reason", ["MERGE_CONFLICT", "MANUAL", "BRANCH_PROTECTION_FAILURE"])
def test_a_removal_that_is_no_check_failure_carries_no_run_hint(reason: str) -> None:
    event = parse_github_pull_request_event("pull_request.dequeued", _payload(reason))
    assert event is not None
    assert "queue_run_hint" not in event.extra
    assert queue_run_hint(REPO, reason.lower(), "2026-11-25T13:35:00Z") is None


def test_the_wake_and_the_surfaced_note_quote_the_run_hint() -> None:
    event = parse_github_pull_request_event("pull_request.dequeued", _payload("FAILED_CHECKS"))
    assert event is not None
    bound = wipr.bind_pull_request({}, repo="Acme/Widget", number=7, head_branch="feat/x", head_sha=SHA)
    _, out = wipr.apply_pull_request_event(bound, event, NOW)
    (wake,) = [o for o in out if isinstance(o, wipr.CiCompleted)]
    assert wake.run_name is not None
    assert wake.run_name.startswith("merge queue: removed (failed_checks); the failing queue run: ")
    note = dequeued_note([f"{REPO}#7"], event, NOW, still_blocked=False)
    assert "(failed_checks; the failing queue run: list_github_workflow_runs(" in note


def test_a_removal_read_from_the_timeline_carries_the_run_hint_too() -> None:
    data = {
        "repository": {
            "pullRequest": {
                "state": "OPEN",
                "isInMergeQueue": False,
                "url": "https://github.com/Acme/Widget/pull/7",
                "headRefName": "feat/x",
                "headRefOid": SHA,
                "timelineItems": {"nodes": [{"createdAt": "2026-11-25T04:56:24Z", "reason": "FAILED_CHECKS"}]},
            }
        }
    }
    event = dequeued_from_timeline("Acme/Widget", 7, data)
    assert event is not None
    assert 'created=">=2026-11-25"' in event.extra["queue_run_hint"]


def test_a_moved_tip_refusal_is_told_apart_from_other_errors() -> None:
    assert attachments.tip_moved('Error: Expected branch to point to "abc" but it did not.')
    assert not attachments.tip_moved("Error: GitHub returned HTTP 403.")
    assert not attachments.tip_moved(None)


async def test_attachments_retry_on_the_new_tip_when_another_comment_moved_it(monkeypatch) -> None:
    tips = iter(["a" * 40, "b" * 40])
    sent: list[str] = []

    async def _tip(*_args: Any) -> tuple[str, None]:
        return next(tips), None

    async def _graphql(_client, _headers, _query, variables, **_kw):
        sent.append(variables["input"]["expectedHeadOid"])
        if len(sent) == 1:
            return None, 'Error: Expected branch to point to "' + "a" * 40 + '" but it did not.'
        return {"createCommitOnBranch": {"commit": {"oid": "d" * 40}}}, None

    async def _read(_ctx, _file_id, **_kw):
        return b"\x89PNG", "shot.png", "image/png"

    monkeypatch.setattr(attachments, "_assets_tip", _tip)
    monkeypatch.setattr(attachments._common, "gh_graphql", _graphql)
    monkeypatch.setattr(attachments, "read_pipe_file", _read)
    lines, err = await attachments.attach_files(object(), {}, "acme/widget", 7, ["f1"], object())
    assert err is None
    assert sent == ["a" * 40, "b" * 40]
    assert lines and ("d" * 40) in lines[0]
