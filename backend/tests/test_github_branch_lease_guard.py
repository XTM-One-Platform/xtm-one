"""A branch another live run leases is not moved by this run's GitHub write tools.

Gap branch-lease-bypassed-by-concurrent-push: the lease (``hold="take"`` on an
entry naming the repository and the branch) was honoured only at dispatch, so
an event run with no item binding pushed under another live run's lease.
"""

from __future__ import annotations

import uuid

import pytest

from app.integrations.providers.github import branch_lease as guard_mod
from app.services import shared_state_pull_requests as leases_mod
from app.services.integration_tools import _make_branch_lease_check_fn
from app.tools.base import CallableToolExecutor, ToolDefinition

REPO = "acme/widgets"
BRANCH = "fix/12-x"


# -- the matcher ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        (f"dev:branch-lock:{BRANCH}", {"pr": 12, "repository": REPO}, True),
        (f"dev:branch-lock:{REPO}:{BRANCH}", {"pr": 12}, True),
        (f"dev:branch-lock:{BRANCH}", {"repository": REPO}, True),
        ("lease", {"repository": REPO, "branch": BRANCH}, True),
        ("lease", {"pr": {"number": 12, "repo": REPO}, "head_branch": BRANCH}, True),
        (f"dev:branch-lock:{BRANCH}", {"pr": 12, "repository": "other/repo"}, False),
        (f"dev:branch-lock:{BRANCH}", {"pr": 12}, False),
        ("dev:branch-lock:fix/13-y", {"repository": REPO}, False),
        (f"dev:branch-lock:{BRANCH}", "not a dict", False),
    ],
)
def test_an_entry_names_the_branch_of_its_repository_only(key, value, expected):
    assert leases_mod.entry_names_branch(key, value, repo=REPO, branch=BRANCH) is expected


def test_the_repository_is_compared_case_insensitively():
    assert leases_mod.entry_names_branch(
        f"dev:branch-lock:{BRANCH}", {"repository": "Acme/Widgets"}, repo=REPO, branch=BRANCH
    )


# -- the live lease ---------------------------------------------------------------------


@pytest.fixture
def held(monkeypatch):
    state: dict = {"rows": [], "alive": set()}

    async def held_rows(user_id):
        return list(state["rows"])

    async def alive(run_ids):
        return {r for r in run_ids if r in state["alive"]}

    monkeypatch.setattr(leases_mod, "_held_rows", held_rows)
    monkeypatch.setattr(leases_mod, "_alive", alive)
    return state


async def test_another_live_holder_is_answered(held):
    holder = uuid.uuid4()
    held["rows"] = [(f"dev:branch-lock:{BRANCH}", {"repository": REPO, "pr": 12}, holder)]
    held["alive"] = {holder}
    lease = await leases_mod.live_branch_lease(REPO, BRANCH, user_id=None, run_id=uuid.uuid4())
    assert lease == {"key": f"dev:branch-lock:{BRANCH}", "run_id": str(holder)}


async def test_the_callers_own_lease_and_an_ended_holder_let_the_write_go(held):
    holder = uuid.uuid4()
    held["rows"] = [(f"dev:branch-lock:{BRANCH}", {"repository": REPO}, holder)]
    held["alive"] = {holder}
    assert await leases_mod.live_branch_lease(REPO, BRANCH, user_id=None, run_id=str(holder)) is None
    held["alive"] = set()
    assert await leases_mod.live_branch_lease(REPO, BRANCH, user_id=None, run_id=uuid.uuid4()) is None


async def test_a_lease_on_another_branch_or_repository_is_not_this_branch(held):
    holder = uuid.uuid4()
    held["rows"] = [
        ("dev:branch-lock:fix/13-y", {"repository": REPO}, holder),
        (f"dev:branch-lock:{BRANCH}", {"repository": "other/repo"}, holder),
    ]
    held["alive"] = {holder}
    assert await leases_mod.live_branch_lease(REPO, BRANCH, user_id=None, run_id=uuid.uuid4()) is None


async def test_a_failed_read_lets_the_write_go(monkeypatch):
    async def boom(user_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(leases_mod, "_held_rows", boom)
    assert await leases_mod.live_branch_lease(REPO, BRANCH, user_id=None, run_id=uuid.uuid4()) is None


async def test_no_branch_reads_nothing(monkeypatch):
    async def never(user_id):
        raise AssertionError("read")

    monkeypatch.setattr(leases_mod, "_held_rows", never)
    assert await leases_mod.live_branch_lease(REPO, "", user_id=None, run_id=None) is None


# -- the callback ---------------------------------------------------------------------------


def test_no_run_no_callback():
    assert _make_branch_lease_check_fn({"user_id": "u"}) is None


async def test_the_callback_answers_the_refusal(held):
    holder = uuid.uuid4()
    held["rows"] = [(f"dev:branch-lock:{BRANCH}", {"repository": REPO}, holder)]
    held["alive"] = {holder}
    check = _make_branch_lease_check_fn({"run_id": str(uuid.uuid4()), "user_id": None})
    refusal = await check(REPO, BRANCH)
    assert refusal and "Nothing written" in refusal and str(holder) in refusal
    own = _make_branch_lease_check_fn({"run_id": str(holder), "user_id": None})
    assert await own(REPO, BRANCH) is None


# -- the tool guard ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "kwargs", "expected"),
    [
        ("create_github_commit", {"repo": REPO, "branch": BRANCH}, BRANCH),
        ("create_github_commit", {"repo": REPO, "branch": ""}, None),
        ("merge_github_branch", {"repo": REPO, "branch": BRANCH}, BRANCH),
        ("rebase_github_branch", {"repo": REPO, "branch": BRANCH}, BRANCH),
        ("manage_github_ref", {"repo": REPO, "action": "update_branch", "branch": BRANCH}, BRANCH),
        ("manage_github_ref", {"repo": REPO, "action": "create_tag", "branch": BRANCH}, None),
        ("get_github_file", {"repo": REPO, "branch": BRANCH}, None),
    ],
)
def test_the_branch_a_call_moves(tool, kwargs, expected):
    assert guard_mod.branch_written(tool, kwargs) == expected


def _executor_with(names: list[str], calls: list[str]) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    executor = CallableToolExecutor()
    definitions = []
    for name in names:

        async def handler(*args, _name=name, **kwargs):
            calls.append(_name)
            return f"{_name} written"

        executor.register(name, handler)
        definitions.append(ToolDefinition(name=name, description=name))
    return definitions, executor


async def test_a_leased_branch_is_refused_and_another_one_is_written():
    calls: list[str] = []
    definitions, executor = _executor_with(["create_github_commit", "get_github_file"], calls)

    async def check(repo, branch):
        return "Nothing written: leased" if branch == BRANCH else None

    kept = guard_mod.apply_branch_lease(definitions, executor, check)
    assert kept == definitions
    commit = executor.handler("create_github_commit")
    assert await commit(repo=REPO, message="m", branch=BRANCH) == "Nothing written: leased"
    assert await commit(repo=REPO, message="m", branch="fix/13-y") == "create_github_commit written"
    read = executor.handler("get_github_file")
    assert await read(repo=REPO, path="a", branch=BRANCH) == "get_github_file written"
    assert calls == ["create_github_commit", "get_github_file"]


async def test_a_failing_check_never_blocks_the_write():
    calls: list[str] = []
    definitions, executor = _executor_with(["merge_github_branch"], calls)

    async def check(repo, branch):
        raise RuntimeError("boom")

    guard_mod.apply_branch_lease(definitions, executor, check)
    merge = executor.handler("merge_github_branch")
    assert await merge(repo=REPO, branch=BRANCH) == "merge_github_branch written"
