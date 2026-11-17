"""Pull request review threads: list, reply, resolve.

A fake ``gh_graphql`` answers each query or mutation by its operation and
records the variables: the tests check the listing, its filter and counts,
each action and the refusals that send nothing.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.providers.github import build_tools, review_threads

_GH_GRAPHQL = "app.integrations.providers.github._common.gh_graphql"


def _thread(tid: str, resolved: bool, path: str = "src/a.py", line: int | None = 12, comments: int = 1) -> dict:
    return {
        "id": tid,
        "isResolved": resolved,
        "isOutdated": False,
        "path": path,
        "line": line,
        "originalLine": 10,
        "resolvedBy": {"login": "octocat"} if resolved else None,
        "first": {"totalCount": comments, "nodes": [{"author": {"login": "copilot"}, "body": "Use a set\nhere.", "url": "u"}]},
        "last": {"nodes": [{"author": {"login": "SamuelHassine"}}]},
    }


class FakeGraphQL:
    def __init__(
        self,
        threads: list[dict] | None = None,
        total: int | None = None,
        *,
        pending: list[dict] | None = None,
        reply_state: str = "SUBMITTED",
        drafts: int = 1,
        fail_on_page: int | None = None,
    ) -> None:
        self.drafts = drafts
        self.fail_on_page = fail_on_page
        self.threads = threads if threads is not None else []
        self.total = total
        self.pending = pending or []
        self.reply_state = reply_state
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, client, headers, query, variables=None, *, action="", extra_headers=None):
        self.calls.append((query, variables or {}))
        if "addPullRequestReviewThreadReply" in query:
            review = {"id": "PRR_9", "state": self.reply_state, "comments": {"totalCount": self.drafts}}
            comment = {"url": "https://github.com/c/1", "state": self.reply_state, "pullRequestReview": review}
            return {"addPullRequestReviewThreadReply": {"comment": comment}}, None
        if "unresolveReviewThread" in query:
            return {"unresolveReviewThread": {"thread": {"id": variables["thread"], "isResolved": False}}}, None
        if "resolveReviewThread" in query:
            return {"resolveReviewThread": {"thread": {"id": variables["thread"], "isResolved": True}}}, None
        if "reviewThreads" in query:
            # Pages like GitHub: ``first`` threads after the cursor, which is
            # the index of the next thread.
            start = int(variables.get("after") or 0)
            size = int(variables.get("first") or 100)
            page = start // size + 1
            if self.fail_on_page == page:
                return None, "Error: GitHub GraphQL answered 502 (read the review threads)."
            end = start + size
            total = self.total if self.total is not None else len(self.threads)
            more = end < len(self.threads)
            pull = {
                "state": "OPEN",
                "pending": {"nodes": self.pending},
                "reviewThreads": {
                    "totalCount": total,
                    "pageInfo": {"hasNextPage": more, "endCursor": str(end) if more else None},
                    "nodes": self.threads[start:end],
                },
            }
            return {"repository": {"pullRequest": pull}}, None
        raise AssertionError(f"unexpected query {query[:60]}")


def _tools() -> tuple[dict[str, Any], dict[str, Any]]:
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)
    handlers = {call[0][0]: call[0][1] for call in executor.register.call_args_list}
    return handlers, {d.name: d for d in definitions}


@pytest.mark.asyncio
async def test_the_listing_shows_unresolved_threads_and_both_counts():
    handlers, _ = _tools()
    fake = FakeGraphQL([_thread("PRRT_1", False, comments=3), _thread("PRRT_2", True, path="b.py")])
    with patch(_GH_GRAPHQL, new=fake):
        result = await handlers["list_github_pr_review_threads"](repo="acme/app", number=7)

    lines = result.splitlines()
    assert lines[0] == "Review threads of acme/app#7 (OPEN): 1 unresolved, 1 resolved."
    assert lines[1] == "- PRRT_1 src/a.py:12: copilot: Use a set here. [3 comments, last by SamuelHassine]"
    assert "PRRT_2" not in result
    assert fake.calls[0][1] == {"owner": "acme", "name": "app", "number": 7, "first": 100, "after": None}
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_the_listing_filters_resolved_and_all_and_says_when_it_is_partial():
    handlers, _ = _tools()
    fake = FakeGraphQL([_thread("PRRT_1", False), _thread("PRRT_2", True, line=None)], total=140)
    with patch(_GH_GRAPHQL, new=fake):
        resolved = await handlers["list_github_pr_review_threads"](repo="acme/app", number=7, state="resolved")
        everything = await handlers["list_github_pr_review_threads"](repo="acme/app", number=7, state="all")

    # An outdated line falls back to the original line.
    assert "- PRRT_2 src/a.py:10 (resolved by octocat): copilot" in resolved and "PRRT_1" not in resolved
    assert "PRRT_1" in everything and "PRRT_2" in everything
    # GitHub counts 140 threads but ends the pages after 2: the answer is partial.
    assert everything.splitlines()[0] == (
        "Review threads of acme/app#7 (OPEN): PARTIAL, 2 of 140 threads read: 1 unresolved, 1 resolved among them."
    )
    assert "Paging stopped early (GitHub ended the pages early): 138 thread(s) were not read" in everything
    assert "Do not merge on this answer." in everything


def _many(count: int, unresolved_at: tuple[int, ...] = ()) -> list[dict[str, Any]]:
    return [_thread(f"PRRT_{i}", i not in unresolved_at) for i in range(count)]


@pytest.mark.asyncio
async def test_the_listing_pages_through_every_thread_and_finds_unresolved_ones_on_page_two() -> None:
    handlers, _ = _tools()
    fake = FakeGraphQL(_many(198, unresolved_at=(150, 160, 170, 197)))
    with patch(_GH_GRAPHQL, new=fake):
        result = await handlers["list_github_pr_review_threads"](repo="acme/app", number=7)

    lines = result.splitlines()
    assert lines[0] == "Review threads of acme/app#7 (OPEN): 4 unresolved, 194 resolved."
    assert [line.split()[1] for line in lines[1:]] == ["PRRT_150", "PRRT_160", "PRRT_170", "PRRT_197"]
    assert "No unresolved thread" not in result and "PARTIAL" not in result
    assert [call[1]["after"] for call in fake.calls] == [None, "100"]


@pytest.mark.asyncio
async def test_a_failed_second_page_is_partial_and_never_claims_zero() -> None:
    handlers, _ = _tools()
    fake = FakeGraphQL(_many(198, unresolved_at=(150,)), fail_on_page=2)
    with patch(_GH_GRAPHQL, new=fake):
        result = await handlers["list_github_pr_review_threads"](repo="acme/app", number=7)

    lines = result.splitlines()
    assert lines[0] == (
        "Review threads of acme/app#7 (OPEN): PARTIAL, 100 of 198 threads read: 0 unresolved, 100 resolved among them."
    )
    assert lines[1].startswith("Paging stopped early (page 2 failed: Error: GitHub GraphQL answered 502")
    assert "98 thread(s) were not read" in lines[1] and "unresolved threads may remain" in lines[1]
    assert lines[-1] == "No unresolved thread among the 100 read."
    assert "No unresolved thread." not in result


@pytest.mark.asyncio
async def test_the_page_cap_is_said(monkeypatch: pytest.MonkeyPatch) -> None:
    handlers, _ = _tools()
    monkeypatch.setattr(review_threads, "_MAX_PAGES", 2)
    fake = FakeGraphQL(_many(250))
    with patch(_GH_GRAPHQL, new=fake):
        result = await handlers["list_github_pr_review_threads"](repo="acme/app", number=7)

    assert len(fake.calls) == 2
    assert "PARTIAL, 200 of 250 threads read" in result
    assert "Paging stopped early (the cap of 2 pages of 100 was reached): 50 thread(s) were not read" in result
    assert result.endswith("No unresolved thread among the 200 read.")


@pytest.mark.asyncio
async def test_no_unresolved_thread_is_said():
    handlers, _ = _tools()
    with patch(_GH_GRAPHQL, new=FakeGraphQL([_thread("PRRT_2", True)])):
        result = await handlers["list_github_pr_review_threads"](repo="acme/app", number=7)

    assert result.endswith("No unresolved thread.")


@pytest.mark.asyncio
async def test_reply_and_resolve_posts_the_reply_then_resolves():
    handlers, _ = _tools()
    fake = FakeGraphQL()
    with patch(_GH_GRAPHQL, new=fake):
        result = await handlers["manage_github_pr_review_thread"](
            repo="acme/app", number=7, thread_id="PRRT_1", action="reply_and_resolve", body="Fixed in abc123."
        )

    assert result == "Review thread PRRT_1 of acme/app#7: replied (https://github.com/c/1), then resolved."
    assert [vars_ for _, vars_ in fake.calls] == [{"thread": "PRRT_1", "body": "Fixed in abc123."}, {"thread": "PRRT_1"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "said"), [("resolve", "resolved"), ("unresolve", "unresolved")])
async def test_resolve_and_unresolve_read_the_state_back(action, said):
    handlers, _ = _tools()
    fake = FakeGraphQL()
    with patch(_GH_GRAPHQL, new=fake):
        result = await handlers["manage_github_pr_review_thread"](repo="acme/app", number=7, thread_id="PRRT_1", action=action)

    assert result == f"Review thread PRRT_1 of acme/app#7: {said}."
    assert len(fake.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("drafts", [1, 3])
async def test_a_reply_that_joins_a_pending_review_is_an_error_and_the_review_is_never_submitted(drafts):
    handlers, _ = _tools()
    fake = FakeGraphQL(reply_state="PENDING", drafts=drafts)
    with patch(_GH_GRAPHQL, new=fake):
        result = await handlers["manage_github_pr_review_thread"](
            repo="acme/app", number=7, thread_id="PRRT_1", action="reply_and_resolve", body="Fixed in abc123."
        )

    assert result.startswith("Error: the reply to review thread PRRT_1 joined the pending review PRR_9 ")
    assert f"({drafts} unpublished comment(s) in it, this reply included)" in result
    assert "nothing was resolved" in result and "was not submitted" in result
    # One call: the reply. No submission (it would publish the whole review), no resolve.
    assert [vars_ for _, vars_ in fake.calls] == [{"thread": "PRRT_1", "body": "Fixed in abc123."}]


@pytest.mark.asyncio
async def test_a_submitted_reply_sends_no_review_submission():
    handlers, _ = _tools()
    fake = FakeGraphQL()
    with patch(_GH_GRAPHQL, new=fake):
        await handlers["manage_github_pr_review_thread"](
            repo="acme/app", number=7, thread_id="PRRT_1", action="reply", body="Done."
        )

    assert not any("submitPullRequestReview" in query for query, _ in fake.calls)


@pytest.mark.asyncio
async def test_the_listing_names_a_pending_review_of_the_account():
    handlers, _ = _tools()
    pending = [{"id": "PRR_9", "author": {"login": "SamuelHassine"}, "comments": {"totalCount": 2}}]
    with patch(_GH_GRAPHQL, new=FakeGraphQL([_thread("PRRT_1", False)], pending=pending)):
        result = await handlers["list_github_pr_review_threads"](repo="acme/app", number=7)

    assert result.splitlines()[1] == (
        "Pending review PRR_9 of SamuelHassine holds 2 unpublished comment(s): "
        "nobody else sees them until it is submitted."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"thread_id": "PRRT_1", "action": "reply", "body": "  "}, "'body' is required for reply"),
        ({"thread_id": "PRRT_1", "action": "close"}, "'action' must be one of"),
        ({"thread_id": "", "action": "resolve"}, "'thread_id' is required"),
    ],
)
async def test_refusals_send_nothing(kwargs, message):
    handlers, _ = _tools()
    fake = FakeGraphQL()
    with patch(_GH_GRAPHQL, new=fake):
        result = await handlers["manage_github_pr_review_thread"](repo="acme/app", number=7, **kwargs)

    assert result.startswith("Error: ") and message in result
    assert fake.calls == []


class SlowReplies(FakeGraphQL):
    """Replies take a moment; records the most replies in flight at once."""

    def __init__(self) -> None:
        super().__init__()
        self.in_flight = 0
        self.peak = 0

    async def __call__(self, client, headers, query, variables=None, *, action="", extra_headers=None):
        if "addPullRequestReviewThreadReply" not in query:
            return await super().__call__(client, headers, query, variables, action=action)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            return await super().__call__(client, headers, query, variables, action=action)
        finally:
            self.in_flight -= 1


@pytest.mark.asyncio
async def test_replies_to_one_pull_request_in_one_round_are_posted_one_after_another():
    handlers, _ = _tools()
    fake = SlowReplies()
    manage = handlers["manage_github_pr_review_thread"]
    with patch(_GH_GRAPHQL, new=fake):
        results = await asyncio.gather(*[
            manage(repo=repo, number=7, thread_id=f"PRRT_{i}", action="reply_and_resolve", body=f"Fixed {i}.")
            for i, repo in enumerate(["acme/app", "Acme/App", "acme/app", "ACME/app"])
        ])

    assert fake.peak == 1
    assert all(result.endswith("then resolved.") for result in results)
    assert sorted(v["body"] for _, v in fake.calls if "body" in v) == [f"Fixed {i}." for i in range(4)]
    assert review_threads._REPLY_LOCKS == {}


@pytest.mark.asyncio
async def test_replies_to_two_pull_requests_are_not_held_by_each_other():
    handlers, _ = _tools()
    fake = SlowReplies()
    manage = handlers["manage_github_pr_review_thread"]
    with patch(_GH_GRAPHQL, new=fake):
        await asyncio.gather(
            manage(repo="acme/app", number=7, thread_id="PRRT_1", action="reply", body="A."),
            manage(repo="acme/app", number=8, thread_id="PRRT_2", action="reply", body="B."),
        )

    assert fake.peak == 2
    assert review_threads._REPLY_LOCKS == {}


@pytest.mark.asyncio
async def test_a_failed_reply_releases_the_pull_request():
    handlers, _ = _tools()

    async def boom(*_args, **_kwargs):
        raise RuntimeError("network")

    with patch(_GH_GRAPHQL, new=boom), pytest.raises(RuntimeError):
        await handlers["manage_github_pr_review_thread"](
            repo="acme/app", number=7, thread_id="PRRT_1", action="reply", body="A."
        )

    assert review_threads._REPLY_LOCKS == {}


def test_the_two_tools_are_declared():
    _, definitions = _tools()
    assert definitions["list_github_pr_review_threads"].input_schema["required"] == ["repo", "number"]
    assert definitions["manage_github_pr_review_thread"].input_schema["properties"]["action"]["enum"] == [
        "reply", "resolve", "unresolve", "reply_and_resolve",
    ]
