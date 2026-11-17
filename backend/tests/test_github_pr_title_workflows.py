"""The pull request title rule read from a repository's CI workflows.

A repository that declares no ``[pull_request]`` table in ``.xtm-one/checks.toml``
is checked against the title checks of its workflows: a shell step that tests the
title against a pattern variable (``[[ =~ ]]`` with expanded variables, or
``grep -P``), a shared ``pr-title-check`` action with its dependency exemption,
and alternative steps that run on a condition of an earlier step. A fake
``safe_request`` answers every read and the write.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.pr_title import (
    RULES_FILE,
    WORKFLOWS_DIR,
    title_error,
    workflow_title_rules,
    workflow_title_verdict,
)

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"

# A bash [[ =~ ]] check whose pattern expands another variable.
_BASH_WORKFLOW = r"""
name: Validate PR Title
on:
  pull_request:
    types: [opened, edited]
jobs:
  validate-pr-title:
    runs-on: ubuntu-latest
    steps:
      - name: Check Conventional Commit title
        env:
          TITLE: ${{ github.event.pull_request.title }}
        run: |
          set -euo pipefail
          ALLOWED_TYPES='feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert'
          # Required format: type(scope?)!?: description (#123)
          PATTERN="^(${ALLOWED_TYPES})(\([a-z0-9]+([_-][a-z0-9]+)*(\/[a-z0-9]+([_-][a-z0-9]+)*)*\))?!?: [a-z0-9].* \(#[0-9]+\)$"
          if [[ "${TITLE}" =~ ${PATTERN} ]]; then
            exit 0
          fi
          echo "Required format:"
          echo "  type(scope?): description (#123)"
          echo "Examples:"
          echo "  feat(api): add bulk export endpoint (#871)"
          exit 1
"""

# A grep -P check, the pattern in single quotes.
_GREP_WORKFLOW = r"""
name: PR conventions checks
on: pull_request_target
jobs:
  check-pr-title-convention:
    runs-on: ubuntu-latest
    steps:
      - name: Check PR title matches convention
        env:
          PR_TITLE: ${{ github.event.pull_request.title }}
        run: |
          PATTERN='^(feat|fix|chore|docs|style|refactor|perf|test|build|ci|revert)(\([a-z0-9._/-]+\))?!?: .+ \(#[0-9]+(, ?#[0-9]+)*\)$'
          if ! echo "$PR_TITLE" | grep -Pq "$PATTERN"; then
            echo "::error::PR title does not match convention: '$PR_TITLE'"
            echo "Format: 'type(scope?): description (#issue)' for example 'docs: updating a doc page (#1234)'."
            exit 1
          fi
  check-signed-commits:
    runs-on: ubuntu-latest
    steps:
      - run: echo "signed"
"""

# The shared action, always run.
_ACTION_WORKFLOW = """
name: PR conventions checks
on: pull_request
jobs:
  check-pr-title-convention:
    runs-on: ubuntu-latest
    steps:
      - name: Validate PR title and create check
        uses: SomeOrg/ci-tools/actions/pr-title-check@main
        with:
          token: ${{ secrets.GITHUB_TOKEN }}
"""

# The shared action and a no-reference alternative, each on a condition of an earlier step.
_CONDITIONAL_WORKFLOW = r"""
name: Validate PR title
on: pull_request
jobs:
  validate-pr-title:
    runs-on: ubuntu-latest
    steps:
      - name: Detect a private related issue
        id: related
        run: echo "private=false" >> "$GITHUB_OUTPUT"
      - name: Validate PR title and create check
        if: steps.related.outputs.private != 'true'
        uses: SomeOrg/ci-tools/actions/pr-title-check@main
      - name: Validate PR title for a private issue
        if: steps.related.outputs.private == 'true'
        env:
          TITLE: ${{ github.event.pull_request.title }}
        run: |
          pattern='^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([a-zA-Z0-9_.-]+\))?!?: [^#]+$'
          if [[ ! "$TITLE" =~ $pattern ]]; then
            echo "Invalid PR title. Required format: type(scope?)!?: description"
            exit 1
          fi
"""


class _GitHub:
    """No checks.toml; *workflows* maps a file name of .github/workflows to its text
    (``None``: the folder cannot be listed). Records every call."""

    def __init__(self, workflows: dict[str, str] | None) -> None:
        self.workflows = workflows
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url))
        if url.endswith(f"/contents/{RULES_FILE}"):
            return None, "Error: GitHub API error (404): Not Found"
        if url.endswith(f"/contents/{WORKFLOWS_DIR}"):
            if self.workflows is None:
                return None, "Error: GitHub API error (404): Not Found"
            listing = [{"type": "file", "name": n, "path": f"{WORKFLOWS_DIR}/{n}"} for n in self.workflows]
            return httpx.Response(200, json=listing), None
        if f"/contents/{WORKFLOWS_DIR}/" in url:
            return httpx.Response(200, text=(self.workflows or {})[url.rsplit("/", 1)[1]]), None
        if method == "post" and url.endswith("/pulls"):
            pr = {"number": 12, "html_url": "https://github.com/acme/app/pull/12", "head": {"ref": "f", "sha": "abc"}}
            return httpx.Response(201, json=pr), None
        if method == "patch":
            return httpx.Response(200, json={"state": "open"}), None
        if method == "get":  # the closing-keyword guard and the overlap read: nothing to say
            return None, "Error: GitHub API error (404): Not Found"
        raise AssertionError(f"unexpected {method} {url}")

    def writes(self) -> list[tuple[str, str]]:
        return [(m, u) for m, u in self.calls if m in ("post", "patch")]

    def workflow_reads(self) -> list[str]:
        return [u.rsplit("/", 1)[1] for m, u in self.calls if f"/contents/{WORKFLOWS_DIR}/" in u]


def _tools():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return {c[0][0]: c[0][1] for c in executor.register.call_args_list}


async def _create(fake: _GitHub, title: str) -> str:
    with patch(_SAFE_REQUEST, new=fake):
        return await _tools()["create_github_pr"](repo="acme/app", title=title, head="f", base="main")


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", [_BASH_WORKFLOW, _GREP_WORKFLOW, _ACTION_WORKFLOW])
async def test_a_title_without_the_issue_suffix_is_refused_before_any_write(workflow):
    fake = _GitHub({"validate-pr-title.yml": workflow})
    result = await _create(fake, "fix(chat): keep the agent label plain")
    assert result.startswith("Error: the pull request title does not follow acme/app's title rule (")
    assert ".github/workflows/validate-pr-title.yml, job " in result
    assert "nothing was written" in result
    assert "Rule: type(scope?)" in result
    assert fake.writes() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", [_BASH_WORKFLOW, _GREP_WORKFLOW, _ACTION_WORKFLOW])
async def test_a_matching_title_is_created(workflow):
    fake = _GitHub({"validate-pr-title.yml": workflow})
    result = await _create(fake, "fix(chat): keep the agent label plain (#529)")
    assert result == "PR #12 created: https://github.com/acme/app/pull/12"
    assert fake.writes() == [("post", "https://api.github.com/repos/acme/app/pulls")]


def test_the_bash_rule_expands_its_variables_and_quotes_the_workflow_wording():
    [(rule, condition)] = workflow_title_rules(_BASH_WORKFLOW, "w.yml")
    assert condition == ""
    assert rule.rule == "type(scope?): description (#123)"
    assert rule.example == "feat(api): add bulk export endpoint (#871)"
    assert title_error("feat(ui/chatbot): a fix (#1)", rule, "x") is None
    assert title_error("feat(ui): Upper case (#1)", rule, "x") is not None
    assert title_error("feat(ui): two refs (#1, #2)", rule, "x") is not None


def test_the_grep_rule_takes_several_references_and_skips_the_other_jobs():
    [(rule, _)] = workflow_title_rules(_GREP_WORKFLOW, "w.yml")
    assert title_error("fix(api): a bug (#1, #2)", rule, "x") is None
    assert title_error("fix(api): a bug", rule, "x") is not None
    assert rule.example == "docs: updating a doc page (#1234)"


def test_the_shared_action_exempts_dependency_updates():
    [(rule, _)] = workflow_title_rules(_ACTION_WORKFLOW, "w.yml")
    assert title_error("chore(deps): update a library", rule, "x") is None
    assert title_error("chore: update a library", rule, "x") is not None
    assert "SomeOrg/ci-tools/actions/pr-title-check" in rule.source


@pytest.mark.asyncio
async def test_conditional_alternatives_pass_either_form_and_name_the_condition():
    fake = _GitHub({"validate-pr-title.yml": _CONDITIONAL_WORKFLOW})
    assert await _create(fake, "fix(api): a bug (#3)") == "PR #12 created: https://github.com/acme/app/pull/12"

    fake = _GitHub({"validate-pr-title.yml": _CONDITIONAL_WORKFLOW})
    first, note = (await _create(fake, "fix(api): a private bug")).split("\n", 1)
    assert first == "PR #12 created: https://github.com/acme/app/pull/12"
    assert note.startswith("Title checked: it breaks type(scope?)!?: description (#123)")
    assert "steps.related.outputs.private == 'true'" in note

    fake = _GitHub({"validate-pr-title.yml": _CONDITIONAL_WORKFLOW})
    assert (await _create(fake, "Fix a bug")).startswith("Error:") and fake.writes() == []


@pytest.mark.asyncio
async def test_only_workflows_named_for_a_title_or_a_convention_are_read():
    fake = _GitHub({"ci.yml": _BASH_WORKFLOW, "pr-check-conventions.yml": _GREP_WORKFLOW})
    result = await _create(fake, "fix(api): a bug")
    assert result.startswith("Error:")
    assert fake.workflow_reads() == ["pr-check-conventions.yml"]


@pytest.mark.asyncio
async def test_a_repository_without_a_title_check_says_title_not_checked():
    fake = _GitHub({"title-labels.yml": "jobs:\n  a:\n    steps:\n      - run: echo hi\n"})
    first, note = (await _create(fake, "Anything goes")).split("\n", 1)
    assert first == "PR #12 created: https://github.com/acme/app/pull/12"
    assert note == "Title not checked: acme/app declares no title rule and no title check was found in its workflows."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workflows", [None, {"pr-title.yml": "jobs: [unclosed"}, {"pr-title.yml": "::not yaml::\n\t- ["}]
)
async def test_an_unreadable_workflow_never_blocks(workflows):
    fake = _GitHub(workflows)
    result = await _create(fake, "Anything goes")
    assert result.startswith("PR #12 created: https://github.com/acme/app/pull/12")
    assert len(fake.writes()) == 1


@pytest.mark.asyncio
async def test_update_checks_a_new_title_against_the_workflows():
    fake = _GitHub({"validate-pr-title.yml": _ACTION_WORKFLOW})
    with patch(_SAFE_REQUEST, new=fake):
        refused = await _tools()["update_github_pr"](repo="acme/app", number=12, title="fix: no suffix")
        updated = await _tools()["update_github_pr"](repo="acme/app", number=12, title="fix: a suffix (#12)")
    assert refused.startswith("Error:")
    assert updated.startswith("PR #12 updated")
    assert fake.writes() == [("patch", "https://api.github.com/repos/acme/app/pulls/12")]


def test_no_rule_list_is_silent_and_an_empty_one_is_named():
    assert workflow_title_verdict("x", None, "acme/app") == (None, None)
    refusal, note = workflow_title_verdict("x", [], "acme/app")
    assert refusal is None and note and note.startswith("Title not checked:")
