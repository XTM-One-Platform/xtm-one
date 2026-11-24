"""add_github_comment(dedupe_first_line=true): a comment whose first line is already on the
issue or pull request is not posted again, and no prior comment body reaches the caller."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.issues import already_posted, first_line

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
API = "https://api.github.com/repos/acme/app"


def _comment(body: str, n: int = 1) -> dict:
    return {
        "body": body,
        "user": {"login": "SamuelHassine"},
        "created_at": "2026-11-09T08:00:00Z",
        "html_url": f"https://github.com/acme/app/pull/7#issuecomment-{n}",
    }


class FakeGitHub:
    def __init__(self, pages: list[list[dict]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, str, dict]] = []

    def urls(self, method: str) -> list[str]:
        return [url for verb, url, _ in self.calls if verb == method]

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "get" and url.startswith(f"{API}/issues/7/comments?"):
            page = int(url.rsplit("page=", 1)[1])
            return httpx.Response(200, json=self.pages[page - 1] if page <= len(self.pages) else []), None
        if method == "post" and url == f"{API}/issues/7/comments":
            return httpx.Response(201, json={"html_url": "https://github.com/acme/app/pull/7#new"}), None
        raise AssertionError(f"unexpected {method.upper()} {url}")


def _tool():
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)
    handler = next(c[0][1] for c in executor.register.call_args_list if c[0][0] == "add_github_comment")
    return handler, {d.name: d for d in definitions}


def test_the_first_line_skips_blank_lines_and_strips():
    assert first_line("\n\n  **UX verdict, round 2**  \nbody") == "**UX verdict, round 2**"
    assert first_line("") == ""
    assert first_line(None) == ""


def test_the_schema_declares_the_option_as_optional():
    _, definitions = _tool()
    schema = definitions["add_github_comment"].input_schema
    assert schema["properties"]["dedupe_first_line"]["type"] == "boolean"
    assert "dedupe_first_line" not in schema["required"]


@pytest.mark.asyncio
async def test_a_comment_already_posted_is_named_and_not_posted_again():
    comment, _ = _tool()
    long_body = "**Verdict R2**\nlong body " + "x" * 5000
    fake = FakeGitHub([[_comment("Other"), _comment(long_body + "  \r\n", 2)]])
    with patch(_SAFE_REQUEST, new=fake):
        result = await comment(repo="acme/app", issue_number=7, body=long_body, dedupe_first_line=True)

    assert result.startswith("Not posted: comment by SamuelHassine on 2026-11-09T08:00:00Z")
    assert result.endswith("https://github.com/acme/app/pull/7#issuecomment-2")
    assert "long body" not in result
    assert fake.urls("post") == []


def test_a_shared_first_line_alone_is_not_a_duplicate() -> None:
    header = "UX-LOOP round=3 verdict=APPROVE new=0 repeat=0 ds_gap=0 self_check=missing"
    assert not already_posted(f"{header}\n\nVerdict on head aaaa1111.", f"{header}\n\nVerdict on head bbbb2222.")
    assert not already_posted(f"{header}\n\nBody.", f"{header}\n\nBody, longer.")
    assert already_posted(f"{header}\r\n\r\nBody.  \n", f"\n{header}\n\nBody.")
    # The attachment lines add_github_comment appends after the body.
    assert already_posted(f"{header}\n\nBody.\n\n![shot](https://x/shot.png)", f"{header}\n\nBody.")
    assert not already_posted("Other\n\nBody.", f"{header}\n\nBody.")
    assert not already_posted("anything", "")


@pytest.mark.asyncio
async def test_a_verdict_for_a_new_head_is_posted_under_the_same_header() -> None:
    comment, _ = _tool()
    header = "UX-LOOP round=3 verdict=APPROVE new=0 repeat=0 ds_gap=0 self_check=missing"
    fake = FakeGitHub([[_comment(f"{header}\n\nRound 3 on head aaaa1111.", 1)]])
    with patch(_SAFE_REQUEST, new=fake):
        result = await comment(
            repo="acme/app",
            issue_number=7,
            body=f"{header}\n\nRound 3 carried to head bbbb2222.",
            dedupe_first_line=True,
        )

    assert result == "Comment added: https://github.com/acme/app/pull/7#new"
    assert fake.urls("post") == [f"{API}/issues/7/comments"]


@pytest.mark.asyncio
async def test_no_match_posts_as_before():
    comment, _ = _tool()
    fake = FakeGitHub([[_comment("**Verdict R1**")]])
    with patch(_SAFE_REQUEST, new=fake):
        result = await comment(repo="acme/app", issue_number=7, body="**Verdict R2**", dedupe_first_line=True)

    assert result == "Comment added: https://github.com/acme/app/pull/7#new"
    assert fake.urls("post") == [f"{API}/issues/7/comments"]


@pytest.mark.asyncio
async def test_the_match_is_found_on_a_later_page():
    comment, _ = _tool()
    fake = FakeGitHub([[_comment(f"c{i}", i) for i in range(100)], [_comment("Verdict", 101)]])
    with patch(_SAFE_REQUEST, new=fake):
        result = await comment(repo="acme/app", issue_number=7, body="Verdict", dedupe_first_line=True)

    assert result.endswith("#issuecomment-101")
    assert len(fake.urls("get")) == 2
    assert fake.urls("post") == []


@pytest.mark.asyncio
async def test_without_the_option_no_comment_is_read():
    comment, _ = _tool()
    fake = FakeGitHub([[_comment("Verdict")]])
    with patch(_SAFE_REQUEST, new=fake):
        result = await comment(repo="acme/app", issue_number=7, body="Verdict")

    assert result.startswith("Comment added")
    assert fake.urls("get") == []


@pytest.mark.asyncio
async def test_a_failed_read_posts_nothing():
    comment, _ = _tool()

    async def failing(client, method, url, provider, action, **kwargs):
        if method == "get":
            return None, "Error: GitHub API error (502): Bad Gateway"
        raise AssertionError("posted after a failed read")

    with patch(_SAFE_REQUEST, new=failing):
        result = await comment(repo="acme/app", issue_number=7, body="Verdict", dedupe_first_line=True)

    assert result == "Error: GitHub API error (502): Bad Gateway"
