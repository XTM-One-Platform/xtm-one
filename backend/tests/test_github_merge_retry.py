"""merge_github_pr reads the PR back and retries after a merge answer of unknown outcome.

GitHub's merge endpoint can answer a gateway 502/503/504, or "merge already in
progress", and land the merge anyway, or leave the PR open. The tool reads the
PR back before any new attempt, stops as soon as it reads merged, pins every
retry to the head read before the first attempt, and gives up after a bounded
number of attempts with the final state named. A definite refusal (405 not
mergeable, 409 head modified) is never retried.

A 405 "Base branch was modified" means the base moved seconds before the PUT:
the tool reads the PR until GitHub has recomputed its mergeability, retries
once pinned to the head when it reads clean (or blocked by a rule the caller
may bypass), and points at update_branch only when it reads behind or dirty.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools

_HEAD = "a" * 40
_MERGED_SHA = "b" * 40


def _fn():
    ex = MagicMock()
    ex.register = MagicMock()
    build_tools("fake-token", ex)
    for call in ex.register.call_args_list:
        if call[0][0] == "merge_github_pr":
            return call[0][1]
    raise KeyError("merge_github_pr not registered")


def _resp(payload, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _pr(merged: bool = False, state: str | None = None, head: str = _HEAD) -> dict:
    pr: dict = {
        "title": "fix(x): y (#1)",
        "body": "Closes #1",
        "head": {"sha": head},
        "merged": merged,
        "merge_commit_sha": _MERGED_SHA if merged else None,
    }
    if state is not None:
        pr["mergeable"] = None if state == "unknown" else state != "dirty"
        pr["mergeable_state"] = state
    return pr


class _GitHub:
    """A scripted GitHub: merge PUT answers in order, PR reads say merged when told.

    *states* scripts the merge state of the PR reads made after the first one
    (the pre-merge read), one per read, the last repeated.
    """

    def __init__(
        self,
        merge_answers: list[tuple[int, dict]],
        merged_after_put: int | None = None,
        states: list[str] | None = None,
        heads: list[str] | None = None,
        verification: dict | None = None,
    ):
        self.verification = verification
        self.commit_reads: list[str] = []
        self.merge_answers = list(merge_answers)
        self.merged_after_put = merged_after_put
        self.states = list(states or [])
        self.heads = list(heads or [])
        self.puts: list[dict] = []
        self.reads = 0

    async def handle(self, client, method, url, provider, action, **kwargs):
        if method == "put" and url.endswith("/merge"):
            self.puts.append(dict(kwargs.get("json") or {}))
            status, body = self.merge_answers.pop(0)
            resp = _resp(body, status)
            if status >= 400:
                return resp, f"Error: GitHub API error ({status}): {body.get('message', '')}"
            return resp, None
        if method == "get" and url.endswith("/pulls/12"):
            self.reads += 1
            merged = self.merged_after_put is not None and len(self.puts) >= self.merged_after_put
            index = self.reads - 2
            state = self.states[min(index, len(self.states) - 1)] if self.states and index >= 0 else None
            head = self.heads[min(index, len(self.heads) - 1)] if self.heads and index >= 0 else _HEAD
            return _resp(_pr(merged=merged, state=state, head=head)), None
        if url.endswith("/commits") or "/commits?" in url:
            return _resp([]), None
        if method == "get" and "/commits/" in url:
            self.commit_reads.append(url.rsplit("/", 1)[-1])
            if self.verification is None:
                return None, "Error: GitHub API error (502): Bad Gateway"
            commit = {"verification": self.verification, "committer": {"date": "2026-11-05T12:00:00Z"}}
            return _resp({"sha": url.rsplit("/", 1)[-1], "commit": commit}), None
        return _resp({}), None


async def _run(github: _GitHub, **kwargs) -> str:
    with (
        patch("app.integrations.providers.github._common.safe_request", side_effect=github.handle),
        patch("app.integrations.providers.github.pulls._MERGE_RETRY_DELAYS", (0.0, 0.0)),
        patch("app.integrations.providers.github.pulls._BASE_MODIFIED_POLL_DELAYS", (0.0, 0.0, 0.0)),
    ):
        return await _fn()(
            repo="acme/app", number=12, merge_method="squash", commit_title="fix(x): y (#1)", **kwargs
        )


@pytest.mark.asyncio
async def test_a_502_whose_merge_landed_is_answered_merged_without_a_second_put():
    github = _GitHub([(502, {"message": "Bad Gateway"})], merged_after_put=1)

    result = await _run(github)

    assert len(github.puts) == 1
    assert "merged (squash)" in result
    assert _MERGED_SHA[:12] in result
    assert "read-back shows the PR merged" in result
    assert not result.startswith("Error")


@pytest.mark.asyncio
async def test_a_502_then_success_retries_pinned_to_the_head_read_before_the_merge():
    github = _GitHub(
        [
            (502, {"message": "Bad Gateway"}),
            (200, {"merged": True, "message": "Pull Request successfully merged"}),
        ]
    )

    result = await _run(github)

    assert len(github.puts) == 2
    assert "sha" not in github.puts[0]
    assert github.puts[1]["sha"] == _HEAD
    assert "merged (squash) (attempt 2)" in result


@pytest.mark.asyncio
async def test_merge_already_in_progress_is_read_back_and_retried():
    github = _GitHub(
        [
            (405, {"message": "Merge already in progress"}),
            (200, {"merged": True, "message": "Pull Request successfully merged"}),
        ]
    )

    result = await _run(github)

    assert len(github.puts) == 2
    assert "attempt 2" in result


@pytest.mark.asyncio
async def test_gives_up_after_three_attempts_naming_the_final_state():
    github = _GitHub([(502, {"message": "Bad Gateway"})] * 3)

    result = await _run(github)

    assert len(github.puts) == 3
    assert result.startswith("Error: PR #12 in acme/app is NOT merged after 3 attempt(s)")
    assert "HTTP 502" in result
    # One read-back after each unknown answer.
    assert github.reads >= 1 + 3


@pytest.mark.asyncio
async def test_a_definite_refusal_is_never_retried():
    github = _GitHub([(405, {"message": "Pull Request is not mergeable"})])

    result = await _run(github)

    assert len(github.puts) == 1
    assert "update_branch" in result


@pytest.mark.asyncio
async def test_a_head_moved_refusal_on_the_retry_stops_there():
    github = _GitHub(
        [
            (502, {"message": "Bad Gateway"}),
            (409, {"message": "Head branch was modified. Review and try the merge again."}),
        ]
    )

    result = await _run(github)

    assert len(github.puts) == 2
    assert github.puts[1]["sha"] == _HEAD
    assert "merged (squash)" not in result


_BASE_MODIFIED = (405, {"message": "Base branch was modified. Review and try the merge again."})
_MERGED_OK = (200, {"merged": True, "message": "Pull Request successfully merged"})


@pytest.mark.asyncio
async def test_base_modified_waits_for_mergeability_then_retries_pinned_to_the_head():
    github = _GitHub([_BASE_MODIFIED, _MERGED_OK], states=["unknown", "clean"])

    result = await _run(github)

    assert len(github.puts) == 2
    assert "sha" not in github.puts[0]
    assert github.puts[1]["sha"] == _HEAD
    assert "merged (squash) (attempt 2" in result
    assert "update_branch" not in result


@pytest.mark.asyncio
async def test_base_modified_blocked_by_a_review_rule_is_retried():
    github = _GitHub([_BASE_MODIFIED, _MERGED_OK], states=["blocked"])

    result = await _run(github)

    assert len(github.puts) == 2
    assert "merged (squash)" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["behind", "dirty"])
async def test_base_modified_behind_or_dirty_points_at_update_branch_without_a_retry(state):
    github = _GitHub([_BASE_MODIFIED], states=["unknown", state])

    result = await _run(github)

    assert len(github.puts) == 1
    assert result.startswith("Error: Cannot merge PR #12")
    assert f"reads '{state}'" in result
    assert "update_branch" in result


@pytest.mark.asyncio
async def test_base_modified_still_unknown_after_the_polls_is_not_retried():
    github = _GitHub([_BASE_MODIFIED], states=["unknown"])

    result = await _run(github)

    assert len(github.puts) == 1
    assert "NOT merged" in result
    assert "'unknown'" in result
    assert "No branch update is needed for a clean PR" in result


@pytest.mark.asyncio
async def test_base_modified_with_a_failing_check_is_not_retried():
    github = _GitHub([_BASE_MODIFIED], states=["unstable"])

    result = await _run(github)

    assert len(github.puts) == 1
    assert "'unstable'" in result


@pytest.mark.asyncio
async def test_base_modified_and_the_head_moved_stops_without_a_retry():
    github = _GitHub([_BASE_MODIFIED], states=["clean"], heads=["c" * 40])

    result = await _run(github)

    assert len(github.puts) == 1
    assert "head moved" in result
    assert "merged (squash)" not in result


@pytest.mark.asyncio
async def test_base_modified_whose_merge_landed_meanwhile_is_answered_merged():
    github = _GitHub([_BASE_MODIFIED], merged_after_put=1, states=["unknown"])

    result = await _run(github)

    assert len(github.puts) == 1
    assert _MERGED_SHA[:12] in result
    assert not result.startswith("Error")


@pytest.mark.asyncio
async def test_base_modified_twice_is_reported_not_retried_a_third_time():
    github = _GitHub([_BASE_MODIFIED, _BASE_MODIFIED], states=["clean"])

    result = await _run(github)

    assert len(github.puts) == 2
    assert result.startswith("Error")
    assert "merged (squash)" not in result
    assert github.commit_reads == []


# The merged answer is a receipt: merge SHA, signature verdict and date, no read-back.

_SIGNED = {"verified": True, "reason": "valid"}


@pytest.mark.asyncio
async def test_a_merge_names_the_merge_sha_and_its_signature():
    answer = {"sha": _MERGED_SHA, "merged": True, "message": "Pull Request successfully merged"}
    github = _GitHub([(200, answer)], verification=_SIGNED)

    result = await _run(github)

    assert github.commit_reads == [_MERGED_SHA]
    assert f"Merge commit {_MERGED_SHA}, signed (valid), committed 2026-11-05T12:00:00Z." in result
    assert "successfully merged." in result and ".." not in result


@pytest.mark.asyncio
async def test_an_unsigned_merge_commit_is_said_not_verified():
    unsigned = {"verified": False, "reason": "unsigned"}
    github = _GitHub([(200, {"sha": _MERGED_SHA, "merged": True, "message": "ok"})], verification=unsigned)

    result = await _run(github)

    assert "NOT verified (unsigned)" in result


@pytest.mark.asyncio
async def test_a_failed_commit_read_is_said_never_guessed():
    github = _GitHub([(200, {"sha": _MERGED_SHA, "merged": True, "message": "ok"})], verification=None)

    result = await _run(github)

    assert not result.startswith("Error")
    assert f"Merge commit {_MERGED_SHA}; signature not read (commit read failed)." in result
    assert "signed (" not in result


@pytest.mark.asyncio
async def test_a_merge_answer_without_sha_points_at_the_read_back():
    github = _GitHub([(200, {"merged": True, "message": "ok"})], verification=_SIGNED)

    result = await _run(github)

    assert github.commit_reads == []
    assert "Merge commit SHA not returned by GitHub" in result


@pytest.mark.asyncio
async def test_a_502_whose_merge_landed_carries_the_receipt_of_the_read_back_sha():
    github = _GitHub([(502, {"message": "Bad Gateway"})], merged_after_put=1, verification=_SIGNED)

    result = await _run(github)

    assert github.commit_reads == [_MERGED_SHA]
    assert "signed (valid)" in result
