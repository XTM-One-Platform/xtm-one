"""Sandbox commands as durable jobs: a worker killed mid-command, the turn resumed on another worker (#11484).

The fake container and the ``api`` fixture are those of ``test_sandbox_run_command.py``: the command runs as a
real detached process, so a "worker" that stops waiting leaves it running exactly as a rollout does. A resumed
worker is a new :class:`WorkspaceSession` on the same store and scope, with nothing in memory.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

from app.services.sandbox_workspace import commands, jobs
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope
from tests import test_sandbox_run_command as run_command_tests

_resolve = run_command_tests._resolve
#: The fake OpenSandbox API of the run-command tests: a real detached process per command.
api = run_command_tests.api

_LIMITS = Limits(quota_bytes=8 * 1024 * 1024, max_files=200)


def _worker(scope: WorkspaceScope, store: MemoryWorkspaceStore) -> WorkspaceSession:
    """One worker's session on the run's workspace: a resumed worker opens a new one on the same store."""
    return WorkspaceSession(scope, store=store, limits=_LIMITS)


def _scope() -> WorkspaceScope:
    return WorkspaceScope(user_id=uuid.UUID(int=7), conversation_id=uuid.uuid4())


async def _call(workspace: WorkspaceSession, arguments: dict, sandbox_api, **kwargs) -> commands.CommandOutcome:
    """What ``sandbox_run_command`` does for a check call: resolve, then run under the call's job."""
    command = await _resolve(workspace, check=arguments["check"])
    with jobs.bound(arguments):
        return await commands.run_with_fallback(command, workspace=workspace, sandbox_api=sandbox_api, **kwargs)


def test_the_job_key_is_the_defining_arguments_only():
    base = {"checkout": "co", "check": "unit", "args": ["tests/a.py"]}
    key = jobs.job_key(base)
    assert key and len(key) == 32
    # How to wait is not part of the job; what runs is.
    assert jobs.job_key({**base, "background": True}) == key
    assert jobs.job_key({**base, "args": ["tests/b.py"]}) != key
    assert jobs.job_key({**base, "affected": True}) != key
    assert jobs.job_key({**base, "args": None, "cwd": ""}) == jobs.job_key(
        {"checkout": "co", "check": "unit", "args": []}
    )
    # A poll, a lab run or a listing starts no job of its own.
    assert jobs.job_key({**base, "run_id": "abc"}) is None
    assert jobs.job_key({**base, "service_id": "lab"}) is None
    assert jobs.job_key({"checkout": "co"}) is None
    assert jobs.job_key(None) is None


async def test_a_worker_killed_mid_command_resumes_on_the_same_job_with_one_execution(api, monkeypatch):
    """Cut while the check runs, the repeat reattaches to its run; a repeat after its end reads the saved result."""
    scope, store = _scope(), MemoryWorkspaceStore()
    first_worker = _worker(scope, store)
    script = "import time; time.sleep(1); open('built.txt', 'w').write('x'); print('job-done')"
    await first_worker.put_tree(
        [("co/.xtm-one/checks.toml", f'[check.slow]\nargv = ["python", "-c", {json.dumps(script)}]\n'.encode())]
    )
    arguments = {"checkout": "co", "check": "slow", "args": None, "argv": None, "cwd": "", "affected": False}

    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.2)
    cut = await _call(first_worker, arguments, api)
    # The worker is stopped here: the call's answer (and its run_id) never reaches the turn.
    assert cut.running and cut.run_id
    (container,) = api.containers.values()
    assert len(container.launched) == 1

    # The resumed turn, on another worker: the job is recorded, the run still held, so the repeat runs (reattaches).
    resumed = _worker(scope, store)
    job = jobs.job_of(await resumed.latest_kernel_state(), jobs.job_key(arguments))
    assert job is not None and job["run_id"] == cut.run_id and job["check"] == "slow"
    assert await jobs.landed(resumed, arguments) is False

    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    repeat = await _call(resumed, arguments, api)
    assert repeat.exit_code == 0 and repeat.run_id == cut.run_id and "job-done" in repeat.text, repeat.text
    assert len(container.launched) == 1, "the check ran once"
    assert "co/built.txt" in await resumed.entries(refresh=True)

    # Cut again after the end (the answer lost a second time): the next repeat is answered from the saved result.
    again = _worker(scope, store)
    landed = await jobs.landed(again, arguments)
    assert (
        isinstance(landed, str) and f"run {cut.run_id}" in landed and "exit code 0" in landed and "job-done" in landed
    )
    assert len(container.launched) == 1


async def test_a_run_that_ended_while_no_worker_waited_is_collected_once_by_the_repeat(api, monkeypatch):
    """The process exits while the turn is cut: the repeat collects that run, never a second execution."""
    scope, store = _scope(), MemoryWorkspaceStore()
    first_worker = _worker(scope, store)
    script = "import time; time.sleep(0.4); print('ended-alone')"
    await first_worker.put_tree(
        [("co/.xtm-one/checks.toml", f'[check.slow]\nargv = ["python", "-c", {json.dumps(script)}]\n'.encode())]
    )
    arguments = {"checkout": "co", "check": "slow"}
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.1)
    cut = await _call(first_worker, arguments, api)
    assert cut.running and cut.run_id
    await asyncio.sleep(1.2)  # the command ends with nobody waiting

    resumed = _worker(scope, store)
    # Ended but not collected: still held, so the repeat runs and collects it.
    assert await jobs.landed(resumed, arguments) is False
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    repeat = await _call(resumed, arguments, api)
    assert repeat.exit_code == 0 and repeat.run_id == cut.run_id and "ended-alone" in repeat.text, repeat.text
    (container,) = api.containers.values()
    assert len(container.launched) == 1


async def test_the_result_is_saved_before_the_held_run_is_cleared(api, monkeypatch):
    """A worker lost between the clear and the save left no record and no result: the save now comes first."""
    workspace = _worker(_scope(), MemoryWorkspaceStore())
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.quick]\nargv = ["python", "-c", "print(1)"]\n')])
    order: list[str] = []
    save, remember = commands._save_run, commands._remember_run

    async def saving(*a, **k):
        order.append("save")
        return await save(*a, **k)

    async def remembering(ws, image, run):
        order.append("clear" if run is None else "hold")
        return await remember(ws, image, run)

    monkeypatch.setattr(commands, "_save_run", saving)
    monkeypatch.setattr(commands, "_remember_run", remembering)
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    done = await _call(workspace, {"checkout": "co", "check": "quick"}, api)
    assert done.exit_code == 0, done.text
    assert order == ["hold", "save", "clear"], order


async def test_a_saved_result_older_than_an_edit_of_the_checkout_lets_the_repeat_run(api, monkeypatch):
    """A resumed attempt edited the tree after the run ended: the repeat runs on the new tree, never the stale result (#11764)."""
    import time

    scope, store = _scope(), MemoryWorkspaceStore()
    workspace = _worker(scope, store)
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.quick]\nargv = ["python", "-c", "print(1)"]\n')])
    arguments = {"checkout": "co", "check": "quick"}
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    done = await _call(workspace, arguments, api)
    assert done.exit_code == 0, done.text

    resumed = _worker(scope, store)
    assert isinstance(await jobs.landed(resumed, arguments), str), "unchanged tree: the saved result answers"
    # A file outside the checkout changes nothing.
    await resumed.put_bytes("other/notes.txt", b"x", mtime=time.time() + 60)
    assert isinstance(await jobs.landed(resumed, arguments), str)
    # An edit of the checkout after the run ended: the repeat runs.
    await resumed.put_bytes("co/app.py", b"print(2)\n", mtime=time.time() + 60)
    assert await jobs.landed(_worker(scope, store), arguments) is False


async def test_no_job_or_a_lost_run_lets_the_repeat_run(api, monkeypatch):
    workspace = _worker(_scope(), MemoryWorkspaceStore())
    arguments = {"checkout": "co", "check": "slow"}
    assert await jobs.landed(workspace, arguments) is False
    assert await jobs.landed(workspace, {**arguments, "run_id": "x", "stop": True}) is None
    await workspace.put_tree(
        [("co/.xtm-one/checks.toml", b'[check.slow]\nargv = ["python", "-c", "import time; time.sleep(5)"]\n')]
    )
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.2)
    cut = await _call(workspace, arguments, api)
    assert cut.running
    for container in list(api.containers.values()):
        await container.kill()  # the container went with a restart: nothing saved
    await commands._remember_run(workspace, "python", None)
    assert await jobs.landed(workspace, arguments) is False


async def test_an_install_cut_by_a_worker_restart_is_answered_from_the_container_after_the_resume(api):
    """The installed tree is the container's: a resumed worker's install of unchanged lockfiles is a cache hit."""
    scope, store = _scope(), MemoryWorkspaceStore()
    first_worker = _worker(scope, store)
    script = "import pathlib; p = pathlib.Path('count'); p.write_text(str(int(p.read_text()) + 1 if p.exists() else 1))"
    toml = f'[check.install]\nargv = ["python", "-c", {json.dumps(script)}]\n'.encode()
    await first_worker.put_tree([("co/.xtm-one/checks.toml", toml), ("co/requirements.txt", b"a==1\n")])
    first = await _call(first_worker, {"checkout": "co", "check": "install"}, api)
    assert first.exit_code == 0 and "cached" not in first.text, first.text

    resumed = _worker(scope, store)
    second = await _call(resumed, {"checkout": "co", "check": "install"}, api)
    assert second.exit_code == 0 and "cached: the same lockfiles were installed by run " in second.text, second.text
    (container,) = api.containers.values()
    assert len(container.launched) == 1


def test_the_job_ledger_is_bounded():
    state: dict = {}

    class _Workspace:
        async def update_kernel_state(self, change):
            nonlocal state
            state = change(state) or state

    async def fill():
        for i in range(jobs.MAX_JOBS + 5):
            with jobs.bound({"checkout": "co", "check": f"c{i}"}):
                await jobs.record(_Workspace(), f"r{i}", f"c{i}")

    asyncio.run(fill())
    assert len(state[jobs.JOBS]) == jobs.MAX_JOBS


async def test_an_install_run_first_is_not_recorded_as_the_job_of_the_check():
    state: dict = {}

    class _Workspace:
        async def update_kernel_state(self, change):
            nonlocal state
            state = change(state) or state

    with jobs.bound({"checkout": "co", "check": "unit"}):
        await jobs.record(_Workspace(), "install-run", "install")
        assert jobs.JOBS not in state
        await jobs.record(_Workspace(), "unit-run", "unit")
    assert jobs.job_of(state, jobs.job_key({"checkout": "co", "check": "unit"}))["run_id"] == "unit-run"
    # Outside a bound call nothing is recorded.
    await jobs.record(_Workspace(), "other", "unit")
    assert len(state[jobs.JOBS]) == 1


def test_saving_a_run_drops_the_oldest_run_directories_beyond_the_kept_count():
    """Saved results never fill the file limit: the oldest run directories go, prepush records stay."""
    entries = {}
    for i in range(5):
        for name in ("run.json", "stdout.txt", "stderr.txt"):
            entries[f".xtm/runs/r{i}/{name}"] = SimpleNamespace(mtime=float(i))
    entries[".xtm/runs/prepush/co.json"] = SimpleNamespace(mtime=0.0)
    entries["co/app.py"] = SimpleNamespace(mtime=0.0)

    stale = commands._stale_run_files(entries, ".xtm/runs/new", keep=3)
    # The new run plus the 2 newest kept (r4, r3); r0..r2 dropped whole.
    assert sorted({p.split("/")[2] for p in stale}) == ["r0", "r1", "r2"]
    assert len(stale) == 9
    assert ".xtm/runs/prepush/co.json" not in stale and "co/app.py" not in stale
    # Under the kept count nothing goes; the run being saved is never counted twice.
    assert commands._stale_run_files(entries, ".xtm/runs/r4", keep=10) == []


async def test_a_saved_run_leaves_at_most_the_kept_count_of_run_directories(monkeypatch):
    workspace = _worker(_scope(), MemoryWorkspaceStore())
    monkeypatch.setattr(commands, "MAX_KEPT_RUNS", 3)
    command = SimpleNamespace(
        check="unit", argv=["true"], cwd="co", image="img", timeout=60, report=None, report_path=None
    )
    for i in range(5):
        saved = await commands._save_run(
            workspace,
            f".xtm/runs/r{i}",
            command=command,
            stdout="ok",
            stderr="",
            exit_code=0,
            timed_out=False,
            seconds=1.0,
            failure="",
            container="c",
        )
        assert saved
    runs = {p.split("/")[2] for p in await workspace.entries(refresh=True) if p.startswith(".xtm/runs/")}
    assert len(runs) == 3 and "r4" in runs, runs
