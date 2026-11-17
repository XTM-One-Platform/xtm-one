"""The open pull requests a push left behind or conflicting: a zero-token push gate (#10622).

A fake ``gh_graphql`` answers the search and the re-reads; the tests check
the structured list an ``activation_check`` reads, the search qualifiers,
the re-read of a merge state GitHub is still computing and the refusals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.providers.github import build_tools, stale_prs
from app.services.assignment_activation_check import condition_met, structured_result

_GH_GRAPHQL = "app.integrations.providers.github._common.gh_graphql"


def _node(
    number: int,
    merge: str = "CLEAN",
    mergeable: str = "MERGEABLE",
    state: str = "OPEN",
    draft: bool = False,
    head: str = "",
) -> dict:
    return {
        "number": number,
        "title": f"fix(x): change {number} (#1)",
        "state": state,
        "isDraft": draft,
        "mergeable": mergeable,
        "mergeStateStatus": merge,
        "headRefName": head or f"fix/{number}-x",
        "baseRefName": "main",
    }


class FakeGraphQL:
    def __init__(self, search: list[dict], rereads: list[dict[int, dict]] | None = None, error: str | None = None):
        self.search = search
        self.rereads = list(rereads or [])
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, client, headers, query, variables=None, *, action="", extra_headers=None):
        self.calls.append((query, dict(variables or {})))
        if self.error:
            return None, self.error
        if "search(" in query:
            return {"search": {"issueCount": len(self.search), "nodes": self.search}}, None
        answer = self.rereads.pop(0) if self.rereads else {}
        return {"repository": {f"pr{n}": node for n, node in answer.items()}}, None


def _tool():
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)
    handlers = {call[0][0]: call[0][1] for call in executor.register.call_args_list}
    return handlers["list_github_prs_needing_update"], {d.name: d for d in definitions}


@pytest.mark.asyncio
async def test_a_push_that_leaves_nothing_behind_answers_an_empty_structured_list():
    tool, _ = _tool()
    fake = FakeGraphQL([_node(1), _node(2, merge="BLOCKED"), _node(3, merge="UNSTABLE")])
    with patch(_GH_GRAPHQL, new=fake):
        result = await tool(repo="acme/app", base="main", author="@me")

    data, has = structured_result(result)
    assert has and data == []
    assert not condition_met(data, "non_empty")
    assert "No open pull request of acme/app needs an update" in result.content
    assert fake.calls[0][1]["q"] == "repo:acme/app is:pr is:open base:main author:@me"


@pytest.mark.asyncio
async def test_behind_dirty_and_a_review_blocked_conflict_need_an_update():
    tool, _ = _tool()
    fake = FakeGraphQL(
        [
            _node(4, merge="BEHIND"),
            _node(5, merge="DIRTY", mergeable="CONFLICTING"),
            _node(6, merge="BLOCKED", mergeable="CONFLICTING"),
            _node(7),
        ]
    )
    with patch(_GH_GRAPHQL, new=fake):
        result = await tool(repo="acme/app")

    data, _ = structured_result(result)
    assert [(e["number"], e["reason"]) for e in data] == [(4, "behind"), (5, "conflicting"), (6, "conflicting")]
    assert condition_met(data, "non_empty")
    assert result.content.startswith("3 open pull request(s) of acme/app need an update")
    assert fake.calls[0][1]["q"] == "repo:acme/app is:pr is:open"


@pytest.mark.asyncio
async def test_a_merge_state_github_is_computing_is_read_again(monkeypatch):
    monkeypatch.setattr(stale_prs.pr_states, "_UNKNOWN_BACKOFF", (0.0, 0.0))
    tool, _ = _tool()
    fake = FakeGraphQL(
        [_node(8, merge="UNKNOWN", mergeable="UNKNOWN"), _node(9, merge="UNKNOWN", mergeable="UNKNOWN")],
        rereads=[{8: _node(8), 9: _node(9, merge="UNKNOWN", mergeable="UNKNOWN")}, {9: _node(9, merge="BEHIND")}],
    )
    with patch(_GH_GRAPHQL, new=fake):
        result = await tool(repo="acme/app")

    data, _ = structured_result(result)
    assert [(e["number"], e["reason"]) for e in data] == [(9, "behind")]
    assert len(fake.calls) == 3


@pytest.mark.asyncio
async def test_a_merge_state_still_unknown_counts_as_needing_an_update(monkeypatch):
    """No missed wake: a pull request GitHub never settled wakes the listener."""
    monkeypatch.setattr(stale_prs.pr_states, "_UNKNOWN_BACKOFF", (0.0,))
    tool, _ = _tool()
    unknown = _node(10, merge="UNKNOWN", mergeable="UNKNOWN")
    fake = FakeGraphQL([unknown], rereads=[{10: unknown}])
    with patch(_GH_GRAPHQL, new=fake):
        result = await tool(repo="acme/app")

    data, _ = structured_result(result)
    assert [(e["number"], e["reason"]) for e in data] == [(10, "unknown")]


@pytest.mark.asyncio
async def test_a_search_error_is_answered_as_an_error_so_the_gate_lets_the_run_proceed():
    tool, _ = _tool()
    with patch(_GH_GRAPHQL, new=FakeGraphQL([], error="Error: 502 Bad Gateway")):
        result = await tool(repo="acme/app")
    assert result == "Error: 502 Bad Gateway"


@pytest.mark.asyncio
async def test_bad_arguments_send_nothing():
    tool, _ = _tool()
    fake = FakeGraphQL([])
    with patch(_GH_GRAPHQL, new=fake):
        assert (await tool(repo="nope")).startswith("Error")
        assert (await tool(repo="acme/app", author="{{login}}")).startswith("Error")
    assert fake.calls == []


def _parked_drafts() -> list[dict]:
    """A person's parked experiments: drafts on innovation branches, conflicting for weeks."""
    return [
        _node(n, merge="DIRTY", mergeable="CONFLICTING", draft=True, head=f"feat/innovation-{n}")
        for n in (5272, 5273, 5277, 5295)
    ]


@pytest.mark.asyncio
async def test_exclude_drafts_answers_empty_when_only_parked_drafts_conflict():
    tool, _ = _tool()
    # GitHub applies draft:false itself; the fake answers the drafts anyway to prove the local filter.
    fake = FakeGraphQL([*_parked_drafts(), _node(1)])
    with patch(_GH_GRAPHQL, new=fake):
        result = await tool(repo="acme/app", author="@me", exclude_drafts=True)

    data, has = structured_result(result)
    assert has and data == []
    assert not condition_met(data, "non_empty")
    assert fake.calls[0][1]["q"] == "repo:acme/app is:pr is:open author:@me draft:false"
    assert "4 left out by the exclusions" in result.content


@pytest.mark.asyncio
async def test_a_ready_pull_request_behind_or_dirty_is_still_listed_with_the_exclusions():
    tool, _ = _tool()
    fake = FakeGraphQL(
        [*_parked_drafts(), _node(11, merge="BEHIND"), _node(12, merge="DIRTY", mergeable="CONFLICTING")]
    )
    with patch(_GH_GRAPHQL, new=fake):
        result = await tool(repo="acme/app", exclude_drafts="true", exclude_head_prefix="feat/innovation-")

    data, _ = structured_result(result)
    assert [(e["number"], e["reason"]) for e in data] == [(11, "behind"), (12, "conflicting")]
    assert condition_met(data, "non_empty")


@pytest.mark.asyncio
async def test_exclude_head_prefix_alone_keeps_other_drafts():
    tool, _ = _tool()
    other_draft = _node(13, merge="BEHIND", draft=True)
    fake = FakeGraphQL([*_parked_drafts(), other_draft])
    with patch(_GH_GRAPHQL, new=fake):
        result = await tool(repo="acme/app", exclude_head_prefix=["feat/innovation-", "wip/"])

    data, _ = structured_result(result)
    assert [(e["number"], e["draft"]) for e in data] == [(13, True)]
    assert "draft:false" not in fake.calls[0][1]["q"]


@pytest.mark.asyncio
async def test_an_excluded_pull_request_is_never_read_again(monkeypatch):
    monkeypatch.setattr(stale_prs.pr_states, "_UNKNOWN_BACKOFF", (0.0,))
    tool, _ = _tool()
    fake = FakeGraphQL([_node(14, merge="UNKNOWN", mergeable="UNKNOWN", head="feat/innovation-x")])
    with patch(_GH_GRAPHQL, new=fake):
        result = await tool(repo="acme/app", exclude_head_prefix="feat/innovation-")

    data, _ = structured_result(result)
    assert data == []
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_an_invalid_exclude_drafts_sends_nothing():
    tool, _ = _tool()
    fake = FakeGraphQL([])
    with patch(_GH_GRAPHQL, new=fake):
        assert (await tool(repo="acme/app", exclude_drafts="maybe")).startswith("Error: exclude_drafts")
    assert fake.calls == []


def test_argument_parsing_accepts_rendered_template_text():
    assert stale_prs.as_bool("True") is True
    assert stale_prs.as_bool("") is False
    assert stale_prs.as_bool(None) is False
    assert stale_prs.head_prefixes("a/, b/ ,") == ["a/", "b/"]
    assert stale_prs.head_prefixes(["a/", ""]) == ["a/"]
    assert stale_prs.head_prefixes(None) == []


def test_needs_update_ignores_closed_pull_requests():
    assert stale_prs.needs_update(_node(1, merge="BEHIND", state="MERGED")) is None
    assert stale_prs.needs_update(_node(1, merge="BEHIND")) == "behind"
    assert stale_prs.needs_update(_node(1)) is None


_GATE = "pipeline / ✅ Pipeline Gate"


def _with_head(node: dict, minutes_ago: float, contexts: list[dict], more: bool = False) -> dict:
    when = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    rollup = {"contexts": {"pageInfo": {"hasNextPage": more}, "nodes": contexts}}
    node["commits"] = {"nodes": [{"commit": {"committedDate": when, "statusCheckRollup": rollup}}]}
    return node


def _required(contexts: frozenset[str]):
    calls: list[str] = []

    async def read(client, headers, owner, name, branch):
        calls.append(branch)
        return contexts

    return read, calls


@pytest.mark.asyncio
async def test_an_old_head_without_a_required_context_of_the_base_is_listed(monkeypatch):
    """A non-strict required check added after the push leaves the PR blocked, never behind."""
    read, calls = _required(frozenset({_GATE}))
    monkeypatch.setattr(stale_prs.checks, "read_required_contexts", read)
    tool, _ = _tool()
    no_checks = _with_head(_node(11, merge="BLOCKED"), 600, [])
    other_checks = _with_head(_node(12, merge="BLOCKED"), 45, [{"__typename": "CheckRun", "name": "lint"}])
    with patch(_GH_GRAPHQL, new=FakeGraphQL([no_checks, other_checks])):
        result = await tool(repo="acme/app")

    data, _ = structured_result(result)
    assert [(e["number"], e["reason"], e["missing"]) for e in data] == [
        (11, "missing_required_contexts", [_GATE]),
        (12, "missing_required_contexts", [_GATE]),
    ]
    assert condition_met(data, "non_empty")
    assert f"#11 | missing_required_contexts ({_GATE})" in result.content
    assert calls == ["main"]


@pytest.mark.asyncio
async def test_a_head_that_reported_the_required_context_is_not_listed(monkeypatch):
    read, _ = _required(frozenset({_GATE, "legacy/status"}))
    monkeypatch.setattr(stale_prs.checks, "read_required_contexts", read)
    tool, _ = _tool()
    reported = [
        {"__typename": "CheckRun", "name": _GATE},
        {"__typename": "StatusContext", "context": "legacy/status"},
    ]
    with patch(_GH_GRAPHQL, new=FakeGraphQL([_with_head(_node(13, merge="CLEAN"), 600, reported)])):
        result = await tool(repo="acme/app")

    data, _ = structured_result(result)
    assert data == []


@pytest.mark.asyncio
async def test_a_fresh_push_or_an_unread_context_page_is_never_judged_missing(monkeypatch):
    read, calls = _required(frozenset({_GATE}))
    monkeypatch.setattr(stale_prs.checks, "read_required_contexts", read)
    tool, _ = _tool()
    fresh = _with_head(_node(14, merge="BLOCKED"), 5, [])
    truncated = _with_head(_node(15, merge="BLOCKED"), 600, [], more=True)
    with patch(_GH_GRAPHQL, new=FakeGraphQL([fresh, truncated])):
        result = await tool(repo="acme/app")

    data, _ = structured_result(result)
    assert data == []
    assert calls == []


@pytest.mark.asyncio
async def test_a_pull_request_already_behind_keeps_its_reason_and_reads_no_rule(monkeypatch):
    read, calls = _required(frozenset({_GATE}))
    monkeypatch.setattr(stale_prs.checks, "read_required_contexts", read)
    tool, _ = _tool()
    with patch(_GH_GRAPHQL, new=FakeGraphQL([_with_head(_node(16, merge="BEHIND"), 600, [])])):
        result = await tool(repo="acme/app")

    data, _ = structured_result(result)
    assert [(e["number"], e["reason"]) for e in data] == [(16, "behind")]
    assert "missing" not in data[0]
    assert calls == []


def test_the_definition_is_a_read_with_repo_required():
    _, definitions = _tool()
    definition = definitions["list_github_prs_needing_update"]
    assert definition.input_schema["required"] == ["repo"]
    assert definition.input_schema["properties"]["exclude_drafts"]["type"] == "boolean"
    assert "exclude_head_prefix" in definition.input_schema["properties"]
    assert "activation_check" in definition.description
