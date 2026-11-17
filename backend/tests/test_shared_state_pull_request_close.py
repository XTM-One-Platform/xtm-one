"""A shared-state entry that tracks a pull request is resolved when it closes (#10720).

Branch leases (``dev:branch-lock:<head branch>`` with ``{"pr": N}``) stayed
active after their pull request merged whenever the merging run did not
resolve them; a watchdog resolved them by hand every hour. The pull request
closing delivery now resolves them. Unit level: the matcher (number, then
repository, else head branch), the pass over candidates, the unsigned
read-back and the webhook route.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.integrations.pull_request_events import PullRequestEvent
from app.services import shared_state_pull_requests as mod

REPO = "acme/widgets"
NOW = datetime(2026, 11, 10, 7, 0, 37, tzinfo=UTC)


def _closed(number: int = 12, *, merged: bool = True, branch: str | None = "fix/12-x", **extra) -> PullRequestEvent:
    return PullRequestEvent(
        kind="closed",
        repo=REPO,
        pr_numbers=(number,),
        head_branch=branch,
        merged=merged,
        merge_sha="c1553adbcac7" if merged else None,
        closed_at="2026-11-10T07:00:37Z",
        **extra,
    )


# -- the matcher -------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        {"pr": 12},
        {"pr": "12"},
        {"pr": "#12"},
        {"pr_number": 12},
        {"pull_request": {"number": 12}},
    ],
)
def test_every_spelling_of_the_number_is_read(value):
    assert mod.pull_request_named(value) == (12, None)


def test_a_value_naming_no_pull_request_is_none():
    for value in ({}, {"pr": None}, {"pr": True}, {"pr": 0}, {"pr": "twelve"}, [], None):
        assert mod.pull_request_named(value) is None


def test_the_repository_named_decides_and_another_repository_never_matches():
    kw = {"repo": REPO, "number": 12, "head_branch": "fix/12-x"}
    assert mod.entry_names_pull_request("k", {"pr": 12, "repository": "Acme/Widgets"}, **kw)
    assert mod.entry_names_pull_request("k", {"pull_request": {"number": 12, "repo": REPO}}, **kw)
    assert not mod.entry_names_pull_request("k", {"pr": 12, "repository": "other/repo"}, **kw)
    assert not mod.entry_names_pull_request("k", {"pr": 13, "repository": REPO}, **kw)


def test_with_no_repository_the_head_branch_decides():
    kw = {"repo": REPO, "number": 12}
    # The branch lease names its branch in its key.
    assert mod.entry_names_pull_request("dev:branch-lock:fix/12-x", {"pr": 12}, head_branch="fix/12-x", **kw)
    assert mod.entry_names_pull_request("lease", {"pr": 12, "branch": "fix/12-x"}, head_branch="fix/12-x", **kw)
    # Same number, another branch: another repository's pull request.
    assert not mod.entry_names_pull_request("dev:branch-lock:feat/9-y", {"pr": 12}, head_branch="fix/12-x", **kw)
    # A closing that names no head branch matches no entry without a repository.
    assert not mod.entry_names_pull_request("dev:branch-lock:fix/12-x", {"pr": 12}, head_branch=None, **kw)


def test_an_entry_naming_no_number_is_matched_by_its_branch_and_repository():
    # #10780: a lease taken before the pull request existed names the issue, not the PR.
    lease = {"holder": None, "repository": "XTM-One-Platform/xtm-one", "issue": 10770}
    kw = {"repo": "xtm-one-platform/xtm-one", "number": 10771}
    key = "dev:branch-lock:fix/10770-roi-compact-figures"
    assert mod.entry_names_pull_request(key, lease, head_branch="fix/10770-roi-compact-figures", **kw)
    assert mod.entry_names_pull_request(
        "lease", {**lease, "branch": "fix/10770-roi-compact-figures"}, head_branch="fix/10770-roi-compact-figures", **kw
    )
    # Another branch, another repository, no repository, or no head branch: left alone.
    assert not mod.entry_names_pull_request(key, lease, head_branch="fix/10770-other", **kw)
    assert not mod.entry_names_pull_request(
        key, {**lease, "repository": "other/repo"}, head_branch="fix/10770-roi-compact-figures", **kw
    )
    assert not mod.entry_names_pull_request(key, {"issue": 10770}, head_branch="fix/10770-roi-compact-figures", **kw)
    assert not mod.entry_names_pull_request(key, lease, head_branch=None, **kw)
    # A value naming ANOTHER pull request number is never matched by its branch.
    assert not mod.entry_names_pull_request(
        key, {**lease, "pr": 10700}, head_branch="fix/10770-roi-compact-figures", **kw
    )


def test_a_repository_scoped_key_names_its_repository() -> None:
    assert mod.repository_in_key("dev:branch-lock:OpenCTI-Platform/opencti:chore/chatbot-3.29.0") == (
        "OpenCTI-Platform/opencti"
    )
    # The old form names no repository; a branch with a slash is not one either.
    assert mod.repository_in_key("dev:branch-lock:chore/chatbot-3.29.0") is None
    assert mod.repository_in_key("dev:branch-lock:main-red-opencti") is None
    assert mod.repository_in_key("lease") is None
    assert mod.repository_in_key("") is None


def test_two_repositories_with_the_same_branch_hold_independent_leases() -> None:
    branch = "chore/chatbot-3.29.0"
    aev = f"dev:branch-lock:OpenAEV-Platform/openaev:{branch}"
    cti = f"dev:branch-lock:OpenCTI-Platform/opencti:{branch}"
    kw: dict[str, Any] = {"number": 12, "head_branch": branch}
    # Same branch, same PR number, no repository in the value: the key decides.
    assert mod.entry_names_pull_request(aev, {"pr": 12}, repo="openaev-platform/openaev", **kw)
    assert not mod.entry_names_pull_request(aev, {"pr": 12}, repo="OpenCTI-Platform/opencti", **kw)
    assert mod.entry_names_pull_request(cti, {"pr": 12}, repo="OpenCTI-Platform/opencti", **kw)
    # A lease taken before the pull request existed (no number) is scoped the same way.
    assert mod.entry_names_pull_request(aev, {"issue": 9}, repo="OpenAEV-Platform/openaev", **kw)
    assert not mod.entry_names_pull_request(cti, {"issue": 9}, repo="OpenAEV-Platform/openaev", **kw)
    # The value's repository still wins over the key's.
    assert not mod.entry_names_pull_request(
        aev, {"pr": 12, "repository": "OpenCTI-Platform/opencti"}, repo="OpenAEV-Platform/openaev", **kw
    )


def test_an_old_form_lease_is_still_honoured_by_its_branch() -> None:
    kw: dict[str, Any] = {"repo": REPO, "number": 12, "head_branch": "fix/12-x"}
    assert mod.entry_names_pull_request("dev:branch-lock:fix/12-x", {"pr": 12}, **kw)
    assert mod.entry_names_pull_request("dev:branch-lock:fix/12-x", {"pr": 12, "repository": REPO}, **kw)


def test_a_branch_is_escaped_for_the_like_pattern():
    assert mod._like_literal("fix/1_a%b\\c") == "fix/1\\_a\\%b\\\\c"


def test_the_note_names_the_pull_request_how_it_ended_and_when():
    note = mod.resolved_note(_closed(), 12, NOW)
    assert "acme/widgets#12 merged (c1553adb) at 2026-11-10T07:00:37Z" in note
    assert "closed unmerged" in mod.resolved_note(_closed(merged=False), 12, NOW)


# -- the pass ----------------------------------------------------------------------


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def harness(monkeypatch):
    state: dict = {"rows": [], "resolved": [], "reads": [], "host_reads": [], "answers": {}}

    async def candidates(db, number, head_branch=None):
        state["reads"].append(("candidates", number, head_branch))
        return list(state["rows"])

    async def resolve_entry(state_id, event, number, now):
        state["resolved"].append((state_id, event))
        return True

    async def closings(bindings, user_id):
        state["host_reads"].append((bindings[0]["repo"], bindings[0]["number"], user_id))
        answer = state["answers"].get(str(user_id))
        return [answer] if answer is not None else []

    import app.core.database as database
    from app.services import work_item_pull_requests

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(mod, "_candidates", candidates)
    monkeypatch.setattr(mod, "_resolve_entry", resolve_entry)
    monkeypatch.setattr(work_item_pull_requests, "read_pull_request_closings", closings)
    return state


def _row(key: str, value: dict, user=None):
    return (uuid.uuid4(), user or uuid.uuid4(), key, value)


async def test_a_trusted_closing_resolves_only_the_entries_of_that_pull_request(harness):
    lease = _row("dev:branch-lock:fix/12-x", {"holder": None, "pr": 12})
    record = _row("babysit", {"pr": 12, "repository": REPO})
    other_repo = _row("dev:branch-lock:fix/12-x", {"pr": 12, "repository": "other/repo"})
    other_number = _row("note", {"pr": 120, "repository": REPO})
    harness["rows"] = [lease, record, other_repo, other_number]

    assert await mod.resolve_states_of_closed_pull_request(_closed()) == 2
    assert [r[0] for r in harness["resolved"]] == [lease[0], record[0]]


async def test_closing_one_repository_leaves_the_same_branch_lease_of_another(harness: dict[str, Any]) -> None:
    branch = "chore/chatbot-3.29.0"
    ours = _row(f"dev:branch-lock:{REPO}:{branch}", {"holder": None, "pr": 12})
    theirs = _row(f"dev:branch-lock:other/repo:{branch}", {"holder": None, "pr": 12})
    old_form = _row(f"dev:branch-lock:{branch}", {"holder": None, "pr": 12, "repository": "other/repo"})
    harness["rows"] = [ours, theirs, old_form]

    assert await mod.resolve_states_of_closed_pull_request(_closed(branch=branch)) == 1
    assert [r[0] for r in harness["resolved"]] == [ours[0]]


async def test_a_closing_resolves_the_branch_lease_that_names_no_number(harness):
    lease = _row(
        "dev:branch-lock:fix/10770-roi-compact-figures",
        {"holder": None, "repository": "XTM-One-Platform/xtm-one", "issue": 10770},
    )
    other_branch = _row("dev:branch-lock:fix/10770-x", {"repository": "XTM-One-Platform/xtm-one"})
    harness["rows"] = [lease, other_branch]
    event = PullRequestEvent(
        kind="closed",
        repo="xtm-one-platform/xtm-one",
        pr_numbers=(10771,),
        head_branch="fix/10770-roi-compact-figures",
        merged=True,
        merge_sha="b4c57df9",
    )
    assert await mod.resolve_states_of_closed_pull_request(event) == 1
    assert [r[0] for r in harness["resolved"]] == [lease[0]]
    # The candidate read is given the head branch, so a value without the number is read.
    assert harness["reads"] == [("candidates", 10771, "fix/10770-roi-compact-figures")]


async def test_a_pull_request_closed_unmerged_resolves_its_entries_too(harness):
    harness["rows"] = [_row("dev:branch-lock:fix/12-x", {"pr": 12})]
    assert await mod.resolve_states_of_closed_pull_request(_closed(merged=False)) == 1


async def test_other_events_resolve_nothing(harness):
    harness["rows"] = [_row("dev:branch-lock:fix/12-x", {"pr": 12})]
    for kind in ("ci", "head", "issue_closed"):
        event = PullRequestEvent(kind=kind, repo=REPO, pr_numbers=(12,), head_branch="fix/12-x")
        assert await mod.resolve_states_of_closed_pull_request(event) == 0
    assert harness["resolved"] == []


async def test_an_unsigned_closing_resolves_only_once_the_code_host_confirms_it(harness):
    owner = uuid.uuid4()
    harness["rows"] = [_row("dev:branch-lock:fix/12-x", {"pr": 12}, user=owner)]
    hint = _closed(merged=True)

    # The code host says it is still open: nothing resolved.
    assert await mod.resolve_from_unverified_delivery(hint) == 0
    assert harness["host_reads"] == [(REPO, 12, owner)]

    confirmed = PullRequestEvent(
        kind="closed", repo=REPO, pr_numbers=(12,), merged=False, closed_at="2026-11-10T07:01:00Z"
    )
    harness["answers"] = {str(owner): confirmed}
    assert await mod.resolve_from_unverified_delivery(hint) == 1
    applied = harness["resolved"][0][1]
    # The confirmed closing wins over the delivery's claim; the branch is kept to match.
    assert (applied.merged, applied.closed_at, applied.head_branch) == (False, "2026-11-10T07:01:00Z", "fix/12-x")


async def test_an_unsigned_closing_with_no_matching_entry_reads_nothing(harness):
    harness["rows"] = [_row("dev:branch-lock:feat/9-y", {"pr": 12})]
    assert await mod.resolve_from_unverified_delivery(_closed()) == 0
    assert harness["host_reads"] == []


async def test_an_unreadable_store_resolves_nothing_and_never_raises(monkeypatch):
    import app.core.database as database

    class _Broken:
        async def __aenter__(self):
            raise RuntimeError("database down")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(database, "async_session", lambda: _Broken())
    assert await mod.resolve_states_of_closed_pull_request(_closed()) == 0
    assert await mod.resolve_from_unverified_delivery(_closed()) == 0


# -- the locked write --------------------------------------------------------------


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _WriteSession(_Session):
    def __init__(self, row):
        self.row = row
        self.committed = False

    async def execute(self, _stmt):
        return _Result(self.row)

    async def commit(self):
        self.committed = True


async def test_the_entry_is_resolved_with_its_hold_and_value_kept(monkeypatch):
    import app.core.database as database

    holder = uuid.uuid4()
    row = SimpleNamespace(
        key="dev:branch-lock:fix/12-x",
        value={"holder": "run x", "pr": 12},
        status="active",
        resolved_at=None,
        held_by_run_id=holder,
        notes=[],
    )
    session = _WriteSession(row)
    monkeypatch.setattr(database, "async_session", lambda: session)

    assert await mod._resolve_entry(uuid.uuid4(), _closed(), 12, NOW) is True
    assert (row.status, row.resolved_at, row.held_by_run_id) == ("resolved", NOW, holder)
    assert row.value == {"holder": "run x", "pr": 12}
    assert "acme/widgets#12 merged" in row.notes[-1]["note"] and session.committed


async def test_an_entry_already_resolved_or_rewritten_is_left_alone(monkeypatch):
    import app.core.database as database

    resolved = SimpleNamespace(key="k", value={"pr": 12, "repository": REPO}, status="resolved", notes=[])
    rewritten = SimpleNamespace(key="k", value={"pr": 13, "repository": REPO}, status="active", notes=[])
    for row in (resolved, rewritten):
        session = _WriteSession(row)
        monkeypatch.setattr(database, "async_session", lambda s=session: s)
        assert await mod._resolve_entry(uuid.uuid4(), _closed(), 12, NOW) is False
        assert not session.committed


# -- the route ---------------------------------------------------------------------


async def test_the_route_resolves_on_a_closing_and_reads_back_an_unsigned_one(monkeypatch):
    import app.integrations.providers.github  # noqa: F401 - registers the parser
    from app.api.v1 import webhooks
    from app.services import work_item_blocked_refs, work_item_pull_requests

    calls: list[tuple[str, PullRequestEvent]] = []

    async def record(name):
        async def fn(event):
            calls.append((name, event))
            return 0

        return fn

    monkeypatch.setattr(work_item_blocked_refs, "release_from_unverified_delivery", await record("refs_read_back"))
    monkeypatch.setattr(work_item_pull_requests, "handle_pull_request_event", await record("refs_trusted"))
    monkeypatch.setattr(mod, "resolve_from_unverified_delivery", await record("state_read_back"))
    monkeypatch.setattr(mod, "resolve_states_of_closed_pull_request", await record("state_trusted"))
    payload = {
        "repository": {"full_name": REPO},
        "pull_request": {"number": 12, "merged": True, "head": {"ref": "fix/12-x", "sha": "s"}},
    }

    monkeypatch.setattr(webhooks, "_signed_with_webhook_secret", lambda enc: False)
    await webhooks._route_pull_request_event("github", "pull_request.closed", payload, None)
    assert [c[0] for c in calls] == ["refs_read_back", "state_read_back"]
    assert calls[1][1].head_branch == "fix/12-x"

    calls.clear()
    monkeypatch.setattr(webhooks, "_signed_with_webhook_secret", lambda enc: True)
    await webhooks._route_pull_request_event("github", "pull_request.closed", payload, "enc")
    await webhooks._route_pull_request_event("github", "pull_request.synchronize", payload, "enc")
    assert [c[0] for c in calls] == ["refs_trusted", "state_trusted", "refs_trusted"], "a push resolves nothing"
