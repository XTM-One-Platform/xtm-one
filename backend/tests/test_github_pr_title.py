"""The pull request title rule checked before create_github_pr / update_github_pr.

A repository declares its title rule in ``.xtm-one/checks.toml``
(``[pull_request] title_pattern``). A fake ``safe_request`` answers the rule
read and the write: the tests check that a title breaking the rule is refused
with nothing written, that a matching title and a repository without a rule
are written as before, that a malformed rule is named and never blocks, and
that xtm-one's own rule is the pattern its title workflow enforces.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.pr_title import ISSUE, RULES_FILE, parse_title_rule, title_error

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"

_RULE = """
[check.unit]
argv = ["pytest"]

[pull_request]
title_pattern = '^(feat|fix)(\\([a-z0-9-]+\\))?: [a-z0-9].* \\(#[0-9]+\\)$'
title_rule = "type(scope?): description (#123)"
title_example = "fix: handle missing auth token (#456)"
"""

_ISSUE_RULE = """
[issue]
title_pattern = '^(feat|fix)(\\([a-z0-9-]+\\))?: [a-z0-9].*$'
title_rule = "type(scope?): description"
title_example = "fix(auth): handle a missing token"
"""


class _GitHub:
    """Answer the rule read with *rules* (``None``: 404) and record every call."""

    def __init__(self, rules: str | None) -> None:
        self.rules = rules
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith(f"/contents/{RULES_FILE}"):
            if self.rules is None:
                return None, "Error: GitHub API error (404): Not Found"
            return httpx.Response(200, text=self.rules), None
        if method == "post" and url.endswith("/issues"):
            return httpx.Response(201, json={"number": 5, "html_url": "https://github.com/acme/app/issues/5"}), None
        if method == "patch" and "/issues/" in url:
            return httpx.Response(200, json={"state": "open", "html_url": "https://github.com/acme/app/issues/5"}), None
        if method == "post" and url.endswith("/pulls"):
            pr = {"number": 12, "html_url": "https://github.com/acme/app/pull/12", "head": {"ref": "f", "sha": "abc"}}
            return httpx.Response(201, json=pr), None
        if method == "patch":
            return httpx.Response(200, json={"state": "open"}), None
        raise AssertionError(f"unexpected {method} {url}")

    def writes(self) -> list[tuple[str, str]]:
        return [(m, u) for m, u, _ in self.calls if m in ("post", "patch")]


def _tools():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return {c[0][0]: c[0][1] for c in executor.register.call_args_list}


@pytest.mark.asyncio
async def test_a_title_breaking_the_rule_is_refused_with_nothing_written():
    fake = _GitHub(_RULE)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](repo="acme/app", title="Add a thing", head="f", base="main")

    assert result.startswith("Error: the pull request title does not follow acme/app's title rule")
    assert "nothing was written" in result
    assert "Rule: type(scope?): description (#123)" in result
    assert "Example: fix: handle missing auth token (#456)" in result
    assert fake.writes() == []
    # The rule is read on the branch the pull request targets, raw.
    _, url, kwargs = fake.calls[0]
    assert url.endswith("/repos/acme/app/contents/.xtm-one/checks.toml")
    assert kwargs["params"] == {"ref": "main"}
    assert kwargs["headers"]["Accept"] == "application/vnd.github.raw+json"


@pytest.mark.asyncio
async def test_a_missing_issue_suffix_is_refused():
    fake = _GitHub(_RULE)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](repo="acme/app", title="fix(auth): handle x", head="f", base="main")
    assert result.startswith("Error:") and fake.writes() == []


@pytest.mark.asyncio
async def test_a_matching_title_is_created_as_before():
    fake = _GitHub(_RULE)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](
            repo="acme/app", title="fix(auth): handle a missing token (#7)", head="f", base="main"
        )
    assert result == "PR #12 created: https://github.com/acme/app/pull/12"
    assert fake.writes() == [("post", "https://api.github.com/repos/acme/app/pulls")]


@pytest.mark.asyncio
@pytest.mark.parametrize("rules", [None, "[check.unit]\nargv = [\"pytest\"]\n", "[pull_request]\nlabels = 1\n"])
async def test_a_repository_without_a_rule_is_written_unchecked(rules):
    fake = _GitHub(rules)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](repo="acme/app", title="Anything goes", head="f", base="main")
    assert result == "PR #12 created: https://github.com/acme/app/pull/12"
    assert len(fake.writes()) == 1


@pytest.mark.asyncio
async def test_a_malformed_rule_is_named_and_never_blocks():
    fake = _GitHub("[pull_request]\ntitle_pattern = '(unclosed'\n")
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](repo="acme/app", title="Anything", head="f", base="main")
    first, note = result.split("\n", 1)
    assert first == "PR #12 created: https://github.com/acme/app/pull/12"
    assert note.startswith("Title not checked: .xtm-one/checks.toml: [pull_request] title_pattern is not a valid")


@pytest.mark.asyncio
async def test_update_checks_a_new_title_on_the_default_branch_when_no_base_is_given():
    fake = _GitHub(_RULE)
    with patch(_SAFE_REQUEST, new=fake):
        refused = await _tools()["update_github_pr"](repo="acme/app", number=12, title="Bad title")
    assert refused.startswith("Error:") and fake.writes() == []
    assert fake.calls[0][2]["params"] is None

    fake = _GitHub(_RULE)
    with patch(_SAFE_REQUEST, new=fake):
        updated = await _tools()["update_github_pr"](repo="acme/app", number=12, title="feat: a good one (#12)")
    assert updated == "PR #12 updated [open] in acme/app."
    assert fake.writes() == [("patch", "https://api.github.com/repos/acme/app/pulls/12")]


@pytest.mark.asyncio
async def test_update_without_a_title_reads_no_rule():
    fake = _GitHub(_RULE)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["update_github_pr"](repo="acme/app", number=12, state="closed")
    assert result == "PR #12 updated [open] in acme/app."
    assert [u for _, u, _ in fake.calls] == ["https://api.github.com/repos/acme/app/pulls/12"]


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("not toml [", "is not valid TOML"),
        ("pull_request = 1\n", "pull_request must be a table"),
        ("[pull_request]\ntitle_pattern = ''\n", "must be a non-empty string"),
        (f"[pull_request]\ntitle_pattern = '{'a' * 1001}'\n", "at most 1000 characters"),
    ],
)
def test_a_malformed_table_is_described(text, why):
    rule, note = parse_title_rule(text)
    assert rule is None and why in (note or "")


def test_the_rule_falls_back_to_the_pattern_when_it_has_no_wording():
    rule, _ = parse_title_rule("[pull_request]\ntitle_pattern = '^x$'\n")
    assert rule is not None
    error = title_error("y", rule, "acme/app") or ""
    assert "Rule: ^x$" in error and "Example" not in error


@pytest.mark.asyncio
async def test_an_issue_title_breaking_the_issue_rule_is_refused_with_nothing_written():
    fake = _GitHub(_ISSUE_RULE)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_issue"](repo="acme/app", title="Live triggers never deliver")

    assert result.startswith("Error: the issue title does not follow acme/app's title rule")
    assert "[issue]" in result and "nothing was written" in result
    assert "Rule: type(scope?): description" in result
    assert "Example: fix(auth): handle a missing token" in result
    assert fake.writes() == []
    # The issue rule is read on the default branch.
    assert fake.calls[0][2]["params"] is None


@pytest.mark.asyncio
async def test_a_matching_issue_title_is_created_as_before():
    fake = _GitHub(_ISSUE_RULE)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_issue"](repo="acme/app", title="fix(notifiers): deliver to a webhook")
    assert result == "Issue #5 created: https://github.com/acme/app/issues/5"
    assert fake.writes() == [("post", "https://api.github.com/repos/acme/app/issues")]


@pytest.mark.asyncio
@pytest.mark.parametrize("rules", [None, _RULE, "[issue]\nlabels = 1\n"])
async def test_a_repository_without_an_issue_rule_creates_issues_unchecked(rules):
    # The pull request rule alone never applies to an issue.
    fake = _GitHub(rules)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_issue"](repo="acme/app", title="Anything goes")
    assert result == "Issue #5 created: https://github.com/acme/app/issues/5"
    assert len(fake.writes()) == 1


@pytest.mark.asyncio
async def test_a_malformed_issue_rule_is_named_and_never_blocks():
    fake = _GitHub("[issue]\ntitle_pattern = '(unclosed'\n")
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_issue"](repo="acme/app", title="Anything")
    first, note = result.split("\n", 1)
    assert first == "Issue #5 created: https://github.com/acme/app/issues/5"
    assert note.startswith("Title not checked: .xtm-one/checks.toml: [issue] title_pattern is not a valid")


@pytest.mark.asyncio
async def test_an_issue_retitle_is_checked_and_an_update_without_title_reads_no_rule():
    fake = _GitHub(_ISSUE_RULE)
    with patch(_SAFE_REQUEST, new=fake):
        refused = await _tools()["update_github_issue"](repo="acme/app", number=5, title="Bad title")
    assert refused.startswith("Error: the issue title") and fake.writes() == []

    fake = _GitHub(_ISSUE_RULE)
    with patch(_SAFE_REQUEST, new=fake):
        updated = await _tools()["update_github_issue"](repo="acme/app", number=5, title="feat(x): a good one")
    assert updated == "Issue #5 updated [open]: https://github.com/acme/app/issues/5"
    assert fake.writes() == [("patch", "https://api.github.com/repos/acme/app/issues/5")]

    fake = _GitHub(_ISSUE_RULE)
    with patch(_SAFE_REQUEST, new=fake):
        await _tools()["update_github_issue"](repo="acme/app", number=5, state="closed")
    assert [u for _, u, _ in fake.calls] == ["https://api.github.com/repos/acme/app/issues/5"]


def _workflow_pattern(text: str) -> str:
    types = re.search(r"^\s*ALLOWED_TYPES='([^']+)'", text, re.M)
    pattern = re.search(r'^\s*PATTERN="([^"]+)"', text, re.M)
    assert types and pattern, "pr-title-check.yml no longer declares ALLOWED_TYPES and PATTERN"
    return pattern.group(1).replace("${ALLOWED_TYPES}", types.group(1))


def test_xtm_ones_rule_is_the_pattern_its_title_workflow_enforces():
    root = Path(__file__).resolve().parents[2]
    workflow = root / ".github" / "workflows" / "pr-title-check.yml"
    checks = root / RULES_FILE
    if not workflow.is_file() or not checks.is_file():
        pytest.skip("no pr-title-check.yml or checks.toml next to the backend")
    declared = tomllib.loads(checks.read_text(encoding="utf-8"))["pull_request"]["title_pattern"]
    assert declared == _workflow_pattern(workflow.read_text(encoding="utf-8"))
    rule, note = parse_title_rule(checks.read_text(encoding="utf-8"))
    assert note is None and rule is not None
    assert title_error("feat(github): check a title (#7243)", rule, "x") is None
    assert title_error("feat(github): check a title", rule, "x") is not None
    assert title_error("[WIP] feat: x (#1)", rule, "x") is not None


def test_xtm_ones_issue_rule_is_the_pr_form_without_the_reference():
    checks = Path(__file__).resolve().parents[2] / RULES_FILE
    if not checks.is_file():
        pytest.skip("no checks.toml next to the backend")
    rule, note = parse_title_rule(checks.read_text(encoding="utf-8"), ISSUE)
    assert note is None and rule is not None
    assert title_error("feat(github): check an issue title", rule, "x", ISSUE) is None
    assert title_error("fix(video-studio/render): a race", rule, "x", ISSUE) is None
    assert title_error("feat(github): check an issue title (#9260)", rule, "x", ISSUE) is not None
    assert title_error("Check an issue title", rule, "x", ISSUE) is not None
    assert title_error("feat(github): Check a title", rule, "x", ISSUE) is not None
