"""Blame of a file range and the merge base in a comparison.

A fake ``gh_graphql`` answers the blame query; a fake ``safe_request``
answers the compare call. The tests check the clipped ranges, the window and
its continuation, the refusals and the merge base line.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.commits import _MAX_BLAME_LINES, format_blame

_GH_GRAPHQL = "app.integrations.providers.github._common.gh_graphql"
_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"


def _range(start: int, end: int, oid: str, subject: str = "feat: a change", login: str | None = "octocat") -> dict:
    return {
        "startingLine": start,
        "endingLine": end,
        "commit": {
            "oid": oid,
            "committedDate": "2026-10-31T12:00:00Z",
            "messageHeadline": subject,
            "author": {"name": "Octo Cat", "user": {"login": login} if login else None},
        },
    }


class FakeGraphQL:
    def __init__(self, ranges: list[dict] | None = None, oid: str | None = "c0ffee1234567890") -> None:
        self.ranges = ranges if ranges is not None else []
        self.oid = oid
        self.calls: list[dict] = []

    async def __call__(self, client, headers, query, variables=None, *, action="", extra_headers=None):
        self.calls.append(variables or {})
        assert "blame(path: $path)" in query
        if self.oid is None:
            return {"repository": {"object": None}}, None
        return {"repository": {"object": {"oid": self.oid, "blame": {"ranges": self.ranges}}}}, None


def _tools():
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)
    handlers = {call[0][0]: call[0][1] for call in executor.register.call_args_list}
    return handlers, {d.name: d for d in definitions}


@pytest.mark.asyncio
async def test_a_range_spanning_two_commits_answers_two_clipped_lines():
    handlers, _ = _tools()
    fake = FakeGraphQL([_range(1, 10, "aaaaaaaa11"), _range(11, 30, "bbbbbbbb22", login=None), _range(31, 40, "cccccccc33")])
    with patch(_GH_GRAPHQL, new=fake):
        result = await handlers["get_github_blame"](repo="acme/app", path="/src/a.py", ref="main", start_line=5, end_line=20)

    assert result.splitlines() == [
        "Blame of acme/app:src/a.py at c0ffee12, lines 5-20 of 40:",
        "L5-10 aaaaaaaa octocat 2026-10-31 feat: a change",
        "L11-20 bbbbbbbb Octo Cat 2026-10-31 feat: a change",
    ]
    assert fake.calls == [{"owner": "acme", "name": "app", "expression": "main", "path": "src/a.py"}]


@pytest.mark.asyncio
async def test_an_empty_ref_blames_the_default_branch_head():
    handlers, _ = _tools()
    fake = FakeGraphQL([_range(1, 3, "aaaaaaaa11")])
    with patch(_GH_GRAPHQL, new=fake):
        result = await handlers["get_github_blame"](repo="acme/app", path="a.py")

    assert fake.calls[0]["expression"] == "HEAD"
    assert result.splitlines()[0] == "Blame of acme/app:a.py at c0ffee12, lines 1-3 of 3:"


@pytest.mark.asyncio
async def test_a_window_beyond_the_cap_names_the_next_start_line():
    handlers, _ = _tools()
    total = _MAX_BLAME_LINES + 50
    fake = FakeGraphQL([_range(1, total, "aaaaaaaa11")])
    with patch(_GH_GRAPHQL, new=fake):
        result = await handlers["get_github_blame"](repo="acme/app", path="big.py")

    lines = result.splitlines()
    assert lines[0] == f"Blame of acme/app:big.py at c0ffee12, lines 1-{_MAX_BLAME_LINES} of {total}:"
    assert lines[1].startswith(f"L1-{_MAX_BLAME_LINES} aaaaaaaa")
    assert lines[-1] == (
        f"Lines {_MAX_BLAME_LINES + 1}-{total} not shown: call again with start_line={_MAX_BLAME_LINES + 1}."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fake", "kwargs", "expected"),
    [
        (FakeGraphQL(oid=None), {"ref": "nope"}, "Error: ref 'nope' is not a commit of acme/app."),
        (FakeGraphQL([]), {}, "Error: no file 'a.py' at c0ffee12 in acme/app (or it is empty or binary)."),
        (FakeGraphQL([_range(1, 3, "aaaaaaaa11")]), {"start_line": 9}, "Error: a.py has 3 lines at c0ffee12; line 9 is past its end."),
    ],
)
async def test_a_missing_ref_or_file_or_a_line_past_the_end_answers_one_error(fake, kwargs, expected):
    handlers, _ = _tools()
    with patch(_GH_GRAPHQL, new=fake):
        result = await handlers["get_github_blame"](repo="acme/app", path="a.py", **kwargs)

    assert result == expected


@pytest.mark.asyncio
async def test_bad_arguments_are_refused_before_any_request():
    handlers, _ = _tools()
    fake = FakeGraphQL([_range(1, 3, "aaaaaaaa11")])
    with patch(_GH_GRAPHQL, new=fake):
        no_path = await handlers["get_github_blame"](repo="acme/app", path=" ")
        reversed_range = await handlers["get_github_blame"](repo="acme/app", path="a.py", start_line=9, end_line=3)

    assert no_path.startswith("Error: 'path' is required")
    assert reversed_range == "Error: 'end_line' (3) is before 'start_line' (9)."
    assert fake.calls == []


def test_format_blame_truncates_a_long_subject_and_skips_ranges_outside():
    lines, last = format_blame([_range(1, 2, "aaaaaaaa11"), _range(3, 4, "bbbbbbbb22", subject="x" * 100)], 3, 3)

    assert last == 4
    assert lines == [f"L3 bbbbbbbb octocat 2026-10-31 {'x' * 71}…"]


def test_the_blame_tool_is_declared_with_its_required_arguments():
    _, definitions = _tools()

    assert definitions["get_github_blame"].input_schema["required"] == ["repo", "path"]


@pytest.mark.asyncio
async def test_compare_shows_the_merge_base():
    handlers, _ = _tools()
    response = MagicMock()
    response.json.return_value = {
        "status": "diverged",
        "ahead_by": 1,
        "behind_by": 2,
        "html_url": "https://github.com/acme/app/compare/main...feat",
        "merge_base_commit": {"sha": "0123456789abcdef0123"},
        "commits": [],
        "files": [
            {"filename": "backend/app/x.py", "status": "modified", "additions": 1, "deletions": 0},
            {"filename": "docs/new.md", "status": "renamed", "previous_filename": "docs/old.md"},
        ],
    }

    async def fake_request(client, method, url, provider, action, **kwargs):
        return response, None

    with patch(_SAFE_REQUEST, side_effect=fake_request):
        result = await handlers["compare_github_commits"](repo="acme/app", base="main", head="feat")

    assert result.content.splitlines()[2] == "Merge base: 0123456789ab"
    # The merge base is carried whole for sandbox_checkout_rebase.
    assert result.structured == {
        "merge_base": "0123456789abcdef0123",
        "status": "diverged",
        "ahead_by": 1,
        "behind_by": 2,
        # The changed files too: the rebase of a narrow checkout reads the branch's files outside it (#11853).
        "files": [
            {"filename": "backend/app/x.py", "status": "modified", "previous_filename": ""},
            {"filename": "docs/new.md", "status": "renamed", "previous_filename": "docs/old.md"},
        ],
        "files_capped": False,
    }
