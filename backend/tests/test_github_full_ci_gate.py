"""merge_github_pr requires the full CI run green on the exact head when the repository declares it (#11985).

xtm-one's pull request CI skips its suites (the merge queue used to run them).
``[merge] full_ci_workflows`` in ``.xtm-one/checks.toml`` names the workflows
whose manual run must be green on the head SHA before a merge: absent, it is
dispatched and the merge refused; running or red, refused; green on an older
SHA, refused and dispatched again; green on the head, merged pinned to it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools, full_ci_gate

_HEAD = "a" * 40
_OLD = "c" * 40
_MERGED = "b" * 40
_GATE = '[merge]\nfull_ci_workflows = ["ci.yml"]\n'


def _merge_fn():
    ex = MagicMock()
    ex.register = MagicMock()
    build_tools("fake-token", ex)
    for call in ex.register.call_args_list:
        if call[0][0] == "merge_github_pr":
            return call[0][1]
    raise KeyError("merge_github_pr not registered")


def _run(
    run_id: int,
    sha: str,
    status: str = "completed",
    conclusion: str | None = "success",
    at: str = "2026-11-15T08:00:00Z",
) -> dict:
    return {
        "id": run_id,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
        "created_at": at,
        "html_url": f"https://github.com/acme/app/actions/runs/{run_id}",
    }


# -- table parsing -----------------------------------------------------------


def test_parse_gate_reads_the_workflows():
    assert full_ci_gate.parse_gate(_GATE) == (["ci.yml"], None)


def test_parse_gate_without_table_or_key_is_no_gate():
    assert full_ci_gate.parse_gate('[check.x]\nargv = ["true"]\n') == ([], None)
    assert full_ci_gate.parse_gate("[merge]\n") == ([], None)


def test_parse_gate_names_a_malformed_table():
    names, why = full_ci_gate.parse_gate('[merge]\nfull_ci_workflows = "ci.yml"\n')
    assert names == [] and "must be a list" in (why or "")
    names, why = full_ci_gate.parse_gate("[merge\n")
    assert names == [] and "not valid TOML" in (why or "")


def test_this_repository_declares_no_gate():
    from pathlib import Path

    checks = Path(__file__).resolve().parents[2] / ".xtm-one" / "checks.toml"
    if not checks.exists():
        pytest.skip("no checks.toml next to the backend")
    names, why = full_ci_gate.parse_gate(checks.read_text())
    assert why is None
    assert names == []


# -- verdict -----------------------------------------------------------------


def test_verdict_absent_when_no_run_on_the_head():
    assert full_ci_gate.run_verdict("ci.yml", [], _HEAD).state == "absent"


def test_verdict_stale_green_on_another_sha_is_absent():
    assert full_ci_gate.run_verdict("ci.yml", [_run(1, _OLD)], _HEAD).state == "absent"


def test_verdict_running():
    v = full_ci_gate.run_verdict("ci.yml", [_run(2, _HEAD, status="in_progress", conclusion=None)], _HEAD)
    assert v.state == "running" and v.run_id == 2


def test_verdict_red():
    v = full_ci_gate.run_verdict("ci.yml", [_run(3, _HEAD, conclusion="failure")], _HEAD)
    assert v.state == "red" and v.conclusion == "failure"


def test_verdict_newest_run_on_the_head_wins():
    runs = [
        _run(4, _HEAD, conclusion="failure", at="2026-11-15T08:00:00Z"),
        _run(5, _HEAD, conclusion="success", at="2026-11-15T09:00:00Z"),
    ]
    v = full_ci_gate.run_verdict("ci.yml", runs, _HEAD)
    assert v.green and v.run_id == 5


# -- merge_github_pr end to end ----------------------------------------------


class _GitHub:
    def __init__(self, gate_text: str | None, runs: list[dict]):
        self.gate_text = gate_text
        self.runs = runs
        self.puts: list[dict] = []
        self.dispatches: list[tuple[str, dict]] = []
        self.run_params: list[dict] = []
        self.dispatch_error: str | None = None

    async def handle(self, client, method, url, provider, action, **kwargs):
        if method == "put" and url.endswith("/merge"):
            self.puts.append(dict(kwargs.get("json") or {}))
            return httpx.Response(
                200, json={"sha": _MERGED, "merged": True, "message": "Pull Request successfully merged"}
            ), None
        if method == "post" and url.endswith("/dispatches"):
            if self.dispatch_error:
                return None, self.dispatch_error
            self.dispatches.append((url, dict(kwargs.get("json") or {})))
            return httpx.Response(204), None
        if method == "get" and url.endswith("/contents/.xtm-one/checks.toml"):
            if self.gate_text is None:
                return None, "Error: GitHub resource not found"
            return httpx.Response(200, text=self.gate_text), None
        if method == "get" and url.endswith("/actions/workflows/ci.yml/runs"):
            self.run_params.append(dict(kwargs.get("params") or {}))
            return httpx.Response(200, json={"workflow_runs": self.runs}), None
        if method == "get" and url.endswith("/pulls/12"):
            pr = {
                "title": "fix(x): y (#1)",
                "body": "Closes #1",
                "head": {"sha": _HEAD, "ref": "fix/y", "repo": {"full_name": "acme/app"}},
                "base": {"ref": "main", "repo": {"full_name": "acme/app"}},
                "merged": False,
            }
            return httpx.Response(200, json=pr), None
        if method == "get" and "/commits/" in url:
            commit = {
                "verification": {"verified": True, "reason": "valid"},
                "committer": {"date": "2026-11-15T09:00:00Z"},
            }
            return httpx.Response(200, json={"sha": _MERGED, "commit": commit}), None
        if url.endswith("/commits") or "/commits?" in url:
            return httpx.Response(200, json=[]), None
        return httpx.Response(200, json={}), None


async def _merge(github: _GitHub) -> str:
    with patch("app.integrations.providers.github._common.safe_request", side_effect=github.handle):
        return await _merge_fn()(repo="acme/app", number=12, merge_method="squash", commit_title="fix(x): y (#1)")


@pytest.mark.asyncio
async def test_no_full_run_dispatches_and_refuses():
    github = _GitHub(_GATE, [])

    result = await _merge(github)

    assert github.puts == []
    assert len(github.dispatches) == 1
    assert github.dispatches[0][1] == {"ref": "fix/y"}
    assert "NOT merged" in result and "dispatched now on fix/y" in result
    assert github.run_params[0]["head_sha"] == _HEAD
    assert github.run_params[0]["event"] == "workflow_dispatch"


@pytest.mark.asyncio
async def test_stale_green_on_an_older_head_dispatches_again():
    github = _GitHub(_GATE, [_run(7, _OLD)])

    result = await _merge(github)

    assert github.puts == []
    assert len(github.dispatches) == 1
    assert "NOT merged" in result


@pytest.mark.asyncio
async def test_running_full_run_refuses_without_a_second_dispatch():
    github = _GitHub(_GATE, [_run(8, _HEAD, status="in_progress", conclusion=None)])

    result = await _merge(github)

    assert github.puts == [] and github.dispatches == []
    assert "running, run 8" in result


@pytest.mark.asyncio
async def test_red_full_run_refuses_and_names_it():
    github = _GitHub(_GATE, [_run(9, _HEAD, conclusion="failure")])

    result = await _merge(github)

    assert github.puts == [] and github.dispatches == []
    assert "failure, run 9" in result and "Nothing was merged." in result


@pytest.mark.asyncio
async def test_green_full_run_merges_pinned_to_the_head_and_names_the_run():
    github = _GitHub(_GATE, [_run(10, _HEAD)])

    result = await _merge(github)

    assert len(github.puts) == 1
    assert github.puts[0]["sha"] == _HEAD
    assert "merged (squash)" in result
    assert "Full CI on head aaaaaaaaaaaa: ci.yml run 10" in result
    assert github.dispatches == []


@pytest.mark.asyncio
async def test_no_gate_declared_merges_as_before():
    github = _GitHub('[check.x]\nargv = ["true"]\n', [])

    result = await _merge(github)

    assert len(github.puts) == 1
    assert "sha" not in github.puts[0]
    assert github.run_params == [] and github.dispatches == []
    assert "merged (squash)" in result


@pytest.mark.asyncio
async def test_unreadable_rules_file_merges_as_before():
    github = _GitHub(None, [])

    result = await _merge(github)

    assert len(github.puts) == 1 and "merged (squash)" in result


# -- a red that is the base's own (#12069) ----------------------------------


def _base_run(run_id: int, conclusion: str, event: str = "push") -> dict:
    return {"id": run_id, "conclusion": conclusion, "event": event, "status": "completed"}


def test_overlap_red_on_base_when_the_newest_base_run_fails_on_the_same_job():
    state, run_id = full_ci_gate.base_overlap({"Backend 1/6"}, [(_base_run(50, "failure"), {"Backend 1/6"})])
    assert (state, run_id) == ("red_on_base", 50)


def test_overlap_own_when_the_base_fails_elsewhere():
    state, _ = full_ci_gate.base_overlap({"Backend 1/6"}, [(_base_run(50, "failure"), {"Frontend"})])
    assert state == "own"


def test_overlap_base_fixed_when_the_base_turned_green_since():
    runs = [(_base_run(52, "success"), set()), (_base_run(51, "failure"), {"Backend 1/6"})]
    assert full_ci_gate.base_overlap({"Backend 1/6"}, runs) == ("base_fixed", 52)


def test_overlap_own_without_failed_jobs_or_base_runs():
    assert full_ci_gate.base_overlap(set(), [(_base_run(50, "failure"), {"x"})])[0] == "own"
    assert full_ci_gate.base_overlap({"x"}, [])[0] == "own"
    assert full_ci_gate.base_overlap({"x"}, [(_base_run(52, "success"), set())])[0] == "own"


class _RedOnBase(_GitHub):
    """The head's full run 9 fails on Backend 1/6; main's newest push run 50 fails on it too."""

    def __init__(self, base_conclusion: str = "failure"):
        super().__init__(_GATE, [_run(9, _HEAD, conclusion="failure")])
        self.base_conclusion = base_conclusion

    async def handle(self, client, method, url, provider, action, **kwargs):
        params = kwargs.get("params") or {}
        if method == "get" and url.endswith("/actions/runs/9/jobs"):
            jobs = [{"name": "Backend 1/6", "conclusion": "failure"}, {"name": "Lint", "conclusion": "success"}]
            return httpx.Response(200, json={"jobs": jobs}), None
        if method == "get" and url.endswith("/actions/runs/50/jobs"):
            return httpx.Response(200, json={"jobs": [{"name": "Backend 1/6", "conclusion": "failure"}]}), None
        if method == "get" and url.endswith("/actions/workflows/ci.yml/runs") and params.get("branch") == "main":
            runs = [_base_run(50, self.base_conclusion), _base_run(48, "success")]
            if self.base_conclusion == "success":
                runs = [_base_run(51, "success"), {**_base_run(50, "failure")}, _base_run(48, "success")]
            return httpx.Response(200, json={"workflow_runs": runs}), None
        return await super().handle(client, method, url, provider, action, **kwargs)


async def _merge_with_park(github: _GitHub, parked: list) -> str:
    async def park(repo, base, workflow, number, head_sha):
        parked.append((repo, base, workflow, number, head_sha))
        return True

    ex = MagicMock()
    ex.register = MagicMock()
    build_tools("fake-token", ex, park_full_ci=park)
    merge = next(c[0][1] for c in ex.register.call_args_list if c[0][0] == "merge_github_pr")
    with patch("app.integrations.providers.github._common.safe_request", side_effect=github.handle):
        return await merge(repo="acme/app", number=12, merge_method="squash", commit_title="fix(x): y (#1)")


@pytest.mark.asyncio
async def test_red_on_main_too_parks_the_pull_request():
    parked: list = []

    result = await _merge_with_park(_RedOnBase(), parked)

    assert parked == [("acme/app", "main", "ci.yml", 12, _HEAD)]
    assert "red on main too (run 50 fails on Backend 1/6)" in result
    assert "parked" in result and "Nothing was merged." in result


@pytest.mark.asyncio
async def test_red_on_main_fixed_since_says_update_and_parks_nothing():
    parked: list = []

    result = await _merge_with_park(_RedOnBase(base_conclusion="success"), parked)

    assert parked == []
    assert "green again (run 51)" in result and "update_branch" in result


@pytest.mark.asyncio
async def test_own_red_parks_nothing():
    parked: list = []
    github = _GitHub(_GATE, [_run(9, _HEAD, conclusion="failure")])

    result = await _merge_with_park(github, parked)

    assert parked == [] and "Read its failed job" in result


class _Redispatch:
    def __init__(self, state: str = "open", sha: str = _HEAD):
        self.state = state
        self.sha = sha
        self.calls: list[tuple[str, str, dict]] = []

    async def handle(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url, dict(kwargs.get("json") or {})))
        if method == "get" and url.endswith("/pulls/12"):
            pr = {"state": self.state, "head": {"sha": self.sha, "ref": "fix/y"}}
            return httpx.Response(200, json=pr), None
        if method == "put" and url.endswith("/update-branch"):
            self.sha = "d" * 40
            return httpx.Response(202, json={}), None
        return httpx.Response(204), None


@pytest.mark.asyncio
async def test_redispatch_updates_the_branch_then_dispatches():
    github = _Redispatch()
    with (
        patch("app.integrations.providers.github._common.safe_request", side_effect=github.handle),
        patch("asyncio.sleep", return_value=None),
    ):
        line = await full_ci_gate.redispatch("acme/app", 12, _HEAD, "ci.yml", "fake-token")

    methods = [(m, u.rsplit("/", 1)[-1]) for m, u, _ in github.calls]
    assert ("put", "update-branch") in methods
    assert methods[-1] == ("post", "dispatches")
    assert github.calls[-1][2] == {"ref": "fix/y"}
    assert "dispatched on fix/y after update-branch" in line


@pytest.mark.asyncio
async def test_redispatch_leaves_a_moved_or_closed_pull_request_alone():
    for github in (_Redispatch(sha=_OLD), _Redispatch(state="closed")):
        with patch("app.integrations.providers.github._common.safe_request", side_effect=github.handle):
            line = await full_ci_gate.redispatch("acme/app", 12, _HEAD, "ci.yml", "fake-token")
        assert "not re-dispatched" in line
        assert all(m == "get" for m, _, _ in github.calls)


@pytest.mark.asyncio
async def test_malformed_gate_is_named_never_a_refusal():
    github = _GitHub('[merge]\nfull_ci_workflows = "ci.yml"\n', [])

    result = await _merge(github)

    assert len(github.puts) == 1
    assert "Full CI gate not checked" in result


# -- dispatch at push time (#12067) ------------------------------------------


def _open_pr(head_repo: str = "acme/app", state: str = "open") -> dict[str, Any]:
    return {
        "number": 12,
        "state": state,
        "head": {"sha": _OLD, "ref": "fix/y", "repo": {"full_name": head_repo}},
        "base": {"ref": "main", "repo": {"full_name": "acme/app"}},
    }


async def _start(github: _GitHub, pr: dict[str, Any]) -> str:
    with patch("app.integrations.providers.github._common.safe_request", side_effect=github.handle):
        return await full_ci_gate.start_on_push(None, "acme", "app", pr, {}, head_sha=_HEAD)


@pytest.mark.asyncio
async def test_push_dispatches_the_full_run_on_the_new_head() -> None:
    github = _GitHub(_GATE, [_run(7, _OLD)])

    note = await _start(github, _open_pr())

    assert len(github.dispatches) == 1 and github.dispatches[0][1] == {"ref": "fix/y"}
    assert github.run_params[0]["head_sha"] == _HEAD
    assert "Full CI dispatched on PR #12 head aaaaaaaaaaaa (ci.yml)" in note


@pytest.mark.asyncio
async def test_push_with_a_run_already_on_the_head_dispatches_nothing() -> None:
    github = _GitHub(_GATE, [_run(8, _HEAD, status="in_progress", conclusion=None)])

    assert await _start(github, _open_pr()) == ""
    assert github.dispatches == []


@pytest.mark.asyncio
async def test_push_without_a_gate_a_fork_or_a_closed_pr_dispatches_nothing() -> None:
    assert await _start(_GitHub('[check.x]\nargv = ["true"]\n', []), _open_pr()) == ""
    fork = _GitHub(_GATE, [])
    assert await _start(fork, _open_pr(head_repo="someone/app")) == ""
    closed = _GitHub(_GATE, [])
    assert await _start(closed, _open_pr(state="closed")) == ""
    assert fork.dispatches == [] and closed.dispatches == []


@pytest.mark.asyncio
async def test_push_names_a_failed_dispatch_and_never_raises() -> None:
    github = _GitHub(_GATE, [])
    github.dispatch_error = "Error: 403 Resource not accessible"

    note = await _start(github, _open_pr())

    assert "Full CI not dispatched on PR #12" in note and "403" in note
