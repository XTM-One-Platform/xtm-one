"""Agentic coding labs (``AGENTIC_CODING``): the flag, the pod a lab is, its start / status / logs / exec / stop, the reaper.

The labs talk to a fake Kubernetes API server (:class:`_Kube`): pods in a dict,
a created pod's containers run at once, and a service container is ready once
the checkout was unpacked. No quota is ever checked or counted on this path
(Sam's decision D1b): the quota functions raise if called.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import pathlib
import tarfile
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import settings
from app.core.feature_flags import KNOWN_FEATURE_FLAGS, is_feature_enabled
from app.services.sandbox_workspace import checks, kube, labs, preview, services
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope

SERVICES = """
[service.db]
argv = ["db-server", "--port", "5432"]
port = 5432
image = "python-dev"
timeout = 30

[service.lab]
argv = ["start-lab", "--dev"]
cwd = "backend"
port = 3100
ready_path = "/api/health"
image = "python-dev"
timeout = 120
depends_on = ["db"]
env = { ENABLED_FEATURES = "*" }
"""


# -- the flag -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "features", "expected"),
    [
        ("copilot", "AGENTIC_CODING", True),
        ("dev", "AGENTIC_CODING", True),
        ("copilot", "*", True),
        ("copilot", "", False),
        ("dev", "VIDEO_STUDIO", False),
        # Never an XTM One feature, whatever the flag says.
        ("xtm_one", "AGENTIC_CODING", False),
        ("xtm_one", "*", False),
        ("ai_disabled", "AGENTIC_CODING", False),
    ],
)
def test_the_flag_is_on_only_in_a_copilot_like_mode(monkeypatch, mode, features, expected):
    assert "AGENTIC_CODING" in KNOWN_FEATURE_FLAGS
    monkeypatch.setattr(settings, "platform_mode", mode)
    monkeypatch.setattr(settings, "enabled_features", features)
    assert is_feature_enabled("AGENTIC_CODING") is expected


def test_labs_need_the_flag_and_a_namespace(monkeypatch):
    monkeypatch.setattr(settings, "platform_mode", "copilot")
    monkeypatch.setattr(settings, "enabled_features", "AGENTIC_CODING")
    monkeypatch.setattr(settings, "agentic_labs_namespace", "")
    assert not labs.labs_enabled()
    monkeypatch.setattr(settings, "agentic_labs_namespace", "agentic-labs")
    assert labs.labs_enabled()
    monkeypatch.setattr(settings, "platform_mode", "xtm_one")
    assert not labs.labs_enabled()


# -- the pod ----------------------------------------------------------------------


async def _resolved(workspace: WorkspaceSession) -> services.ResolvedService:
    await workspace.put_tree(
        [
            ("co/.xtm-one/checks.toml", SERVICES.encode()),
            ("co/backend/app.py", b"print('hi')\n"),
            ("co/backend/node_modules/x/index.js", b"//"),
            ("other/readme.md", b"not the checkout"),
        ]
    )
    return await services.resolve(workspace, checkout="co", service="lab", known_images={"python", "python-dev"})


def _workspace() -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.UUID(int=7), conversation_id=uuid.uuid4())
    return WorkspaceSession(scope, store=MemoryWorkspaceStore(), limits=Limits(quota_bytes=8 * 1024 * 1024, max_files=200))


@pytest.fixture
def lab_settings(monkeypatch):
    monkeypatch.setattr(settings, "platform_mode", "copilot")
    monkeypatch.setattr(settings, "enabled_features", "AGENTIC_CODING")
    monkeypatch.setattr(settings, "agentic_labs_namespace", "agentic-labs")
    monkeypatch.setattr(settings, "agentic_labs_idle_hours", 12)
    monkeypatch.setattr(settings, "agentic_labs_deadline_hours", 72)
    monkeypatch.setattr(settings, "opensandbox_images", {"python": "py:1", "python-dev": "registry/python-dev:2"})


async def test_the_pod_holds_the_workspace_and_one_container_per_service_with_requests_and_no_limits(lab_settings):
    resolved = await _resolved(_workspace())
    manifest = labs.pod_manifest(resolved, name="lab-1", image="registry/python-dev:2", workspace_id="w-1")
    spec = manifest["spec"]
    names = [c["name"] for c in spec["containers"]]
    assert names == ["workspace", "svc-db", "svc-lab"]
    assert spec["activeDeadlineSeconds"] == 72 * 3600 and spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False
    for container in spec["containers"]:
        assert set(container["resources"]) == {"requests"}  # D1b: scheduling only, never a ceiling
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
    lab = spec["containers"][2]
    script = lab["command"][2]
    assert script.index("lab-ready") < script.index("5432") < script.index("start-lab --dev &")
    assert "cd /tmp/workspace/co/backend" in script
    assert lab["readinessProbe"]["httpGet"] == {"path": "/api/health", "port": 3100}
    assert spec["containers"][1]["readinessProbe"]["tcpSocket"] == {"port": 5432}
    assert {"name": "ENABLED_FEATURES", "value": "*"} in lab["env"]
    assert manifest["metadata"]["labels"][labs.LABEL_MANAGED] == labs.MANAGED_BY


async def test_no_deadline_when_the_hygiene_is_turned_off(lab_settings, monkeypatch):
    monkeypatch.setattr(settings, "agentic_labs_deadline_hours", 0)
    manifest = labs.pod_manifest(await _resolved(_workspace()), name="lab-1", image="i", workspace_id="w")
    assert "activeDeadlineSeconds" not in manifest["spec"]


async def test_the_archive_holds_the_checkout_only_without_what_a_command_builds(lab_settings):
    workspace = _workspace()
    await _resolved(workspace)
    raw, count = await labs.checkout_archive(workspace, "co")
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        names = sorted(tf.getnames())
    assert names == ["co/.xtm-one/checks.toml", "co/backend/app.py"] and count == 2


async def test_the_archive_reads_its_blobs_concurrently_once_per_content(lab_settings, monkeypatch):
    workspace = _workspace()
    await workspace.put_tree([(f"co/f{i}.txt", b"same" if i % 2 else f"own {i}".encode()) for i in range(40)])
    store = workspace.store
    original = store.read_blob
    reads: list[str] = []
    running = peak = 0

    async def slow_read(workspace_id, sha256):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        reads.append(sha256)
        await asyncio.sleep(0.01)
        running -= 1
        return await original(workspace_id, sha256)

    monkeypatch.setattr(store, "read_blob", slow_read)
    raw, count = await labs.checkout_archive(workspace, "co")
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        names = tf.getnames()
        same = tf.extractfile("co/f1.txt").read()
        own = tf.extractfile("co/f2.txt").read()
    assert count == 40 and names == sorted(names) and len(names) == 40
    assert same == b"same" and own == b"own 2"
    assert len(reads) == len(set(reads)) == 21  # 20 distinct + the shared one
    assert 1 < peak <= labs._ARCHIVE_READ_CONCURRENCY


async def test_a_blob_read_is_retried_once_and_a_failure_names_the_file(lab_settings, monkeypatch):
    """#10928: never 'unhandled errors in a TaskGroup (1 sub-exception)': the file and the cause."""
    workspace = _workspace()
    await workspace.put_tree([("co/ok.txt", b"ok"), ("co/flaky.txt", b"flaky"), ("co/gone.txt", b"gone")])
    entries = await workspace.entries(refresh=True)
    flaky, gone = entries["co/flaky.txt"].sha256, entries["co/gone.txt"].sha256
    store = workspace.store
    original = store.read_blob
    failures: dict[str, int] = {}

    async def read_blob(workspace_id, sha256):
        if sha256 == gone or (sha256 == flaky and not failures.get(flaky)):
            failures[sha256] = failures.get(sha256, 0) + 1
            raise ConnectionError("store reset")
        return await original(workspace_id, sha256)

    monkeypatch.setattr(store, "read_blob", read_blob)
    with pytest.raises(labs.ArchiveReadError) as caught:
        await labs.checkout_archive(workspace, "co")
    assert str(caught.value) == "co/gone.txt could not be read from the workspace store (store reset, after 2 attempts)"
    assert failures == {flaky: 1, gone: 2}


def test_a_container_name_is_a_dns_label():
    assert labs.container_name("Web_App.v2") == "svc-web-app-v2"
    assert len(labs.container_name("x" * 200)) <= 63


# -- the runtime against a fake API server ----------------------------------------


class _Kube:
    def __init__(self) -> None:
        self.pods: dict[str, dict] = {}
        self.execs: list[tuple[str, str, list[str], bytes | None]] = []
        self.deleted: list[str] = []
        self.exec_exit = 0
        self.never_ready = False
        #: Exec calls with stdin that time out before one gets through.
        self.copy_stalls = 0
        #: Exec calls with stdin the API server refuses at the handshake (HTTP 404) before one gets through.
        self.copy_refusals = 0
        self.exec_timeouts: list[float] = []
        #: Exit code of a command run without stdin (a restart), when set; the copies keep exec_exit.
        self.command_exit: int | None = None

    async def create_pod(self, manifest):
        name = manifest["metadata"]["name"]
        pod = {
            "metadata": dict(manifest["metadata"]),
            "spec": manifest["spec"],
            "status": {
                "phase": "Running",
                "podIP": "10.42.0.9",
                "containerStatuses": [
                    {"name": c["name"], "ready": False, "state": {"running": {}}} for c in manifest["spec"]["containers"]
                ],
            },
        }
        self.pods[name] = pod
        return pod

    async def get_pod(self, name):
        return self.pods.get(name)

    async def list_pods(self, _selector):
        return list(self.pods.values())

    async def patch_annotations(self, name, annotations):
        if name not in self.pods:
            raise kube.KubeError("not found", 404)
        self.pods[name]["metadata"].setdefault("annotations", {}).update(annotations)

    async def delete_pod(self, name):
        self.deleted.append(name)
        return self.pods.pop(name, None) is not None

    async def logs(self, name, container, *, tail_lines, limit_bytes):
        if name not in self.pods:
            raise kube.KubeError("not found", 404)
        return "\n".join(f"{container} line {i}" for i in range(5))

    async def exec(self, name, container, command, *, stdin=None, timeout=600.0):
        self.execs.append((name, container, command, stdin))
        if stdin is not None:
            self.exec_timeouts.append(timeout)
            if self.copy_stalls:
                self.copy_stalls -= 1
                raise kube.KubeError(f"the command did not finish within {timeout:g} s")
            if self.copy_refusals:
                self.copy_refusals -= 1
                raise kube.KubeError("the API server refused the exec stream (HTTP 404)", handshake=404)
        if stdin is not None and not self.never_ready:
            for status in self.pods[name]["status"]["containerStatuses"]:
                status["ready"] = True
        if stdin is None and self.command_exit is not None:
            return kube.ExecResult(exit_code=self.command_exit, stdout=b"", stderr=b"")
        return kube.ExecResult(exit_code=self.exec_exit, stdout=b"ok", stderr=b"" if self.exec_exit == 0 else b"tar: broken")


class _Redis:
    def __init__(self) -> None:
        self.keys: dict[str, int] = {}
        self.values: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.keys[key] = ex
        self.values[key] = value

    async def delete(self, key):
        self.keys.pop(key, None)
        self.values.pop(key, None)

    async def eval(self, script, _numkeys, key, owner, legacy):
        assert script is preview._DELETE_IF_OWNER and legacy == preview.LEGACY_ORIGIN_OWNER
        if self.values.get(key) not in {owner, legacy}:
            return 0
        await self.delete(key)
        return 1

    async def exists(self, key):
        return int(key in self.keys)


@pytest.fixture
def redis(monkeypatch):
    fake = _Redis()

    async def _client():
        return fake

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    monkeypatch.setattr(preview, "_allowed_cache", {})
    return fake


@pytest.fixture
def no_quota(monkeypatch):
    """D1b: the lab path never checks nor counts a sandbox quota."""

    async def _forbidden(*_a, **_k):
        raise AssertionError("a lab must not touch the sandbox quota")

    monkeypatch.setattr("app.services.quota.enforce_combined_sandbox_quota", _forbidden)
    monkeypatch.setattr("app.services.quota.increment_sandbox_usage", _forbidden)


async def _no_sleep(_seconds):
    return None


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_lab_starts_unpacks_the_checkout_and_answers_a_pod_preview(redis):
    workspace = _workspace()
    resolved = await _resolved(workspace)
    fake = _Kube()
    outcome = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert outcome.ready and outcome.started == ["db", "lab"], outcome.text
    (name,) = fake.pods
    assert outcome.service_id == name and outcome.preview_url == "http://10.42.0.9:3100/"
    (pod_name, container, command, stdin) = fake.execs[0]
    assert (pod_name, container) == (name, "workspace") and "tar -xf -" in command[2] and stdin
    held = (await workspace.record(create=False)).kernel_state["labs"]["co:lab"]
    assert held["pod"] == name and held["preview_url"] == outcome.preview_url
    # The preview is admitted for the lab's idle horizon, that IP and port only, with no key added to it.
    assert redis.keys == {"sandbox_preview_origin:10.42.0.9:3100": 12 * 3600}
    assert await preview.allowed("http://10.42.0.9:3100/api/x")
    assert not await preview.allowed("http://10.42.0.9:5432/")
    assert not await preview.allowed("http://10.42.0.10:3100/")
    assert preview.request_headers("http://10.42.0.9:3100/") == {}
    # Started again: answered from the running pod, nothing created.
    again = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert again.ready and "already running" in again.text and list(fake.pods) == [name]


WEB_DEPENDENCY = """
[service.store]
argv = ["store-server"]
port = 9200
image = "python-dev"

[service.product]
argv = ["product-server"]
port = 4000
ready_path = "/health"
image = "python-dev"
depends_on = ["store"]

[service.lab]
argv = ["start-lab"]
port = 3100
ready_path = "/"
image = "python-dev"
depends_on = ["product"]
"""


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_lab_admits_each_http_service_of_its_chain_and_withdraws_them_at_stop(redis):
    # #12094: the lab OpenCTI a lab depends on (its own ready_path) opens in the browser; a store never does.
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", WEB_DEPENDENCY.encode()), ("co/app.py", b"")])
    resolved = await services.resolve(workspace, checkout="co", service="lab", known_images={"python-dev"})
    fake = _Kube()
    outcome = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert outcome.ready, outcome.text
    assert set(redis.keys) == {"sandbox_preview_origin:10.42.0.9:3100", "sandbox_preview_origin:10.42.0.9:4000"}
    assert await preview.allowed("http://10.42.0.9:4000/graphql")
    assert not await preview.allowed("http://10.42.0.9:9200/")
    assert "http://10.42.0.9:4000/" in outcome.text
    held = (await workspace.record(create=False)).kernel_state["labs"]["co:lab"]
    assert held["ports"] == [3100, 4000]
    stopped = await labs.stop_lab(workspace, outcome.service_id, client=fake)
    assert not stopped.refused, stopped.text
    assert redis.keys == {}


def test_a_lab_record_written_before_the_ports_admits_its_single_port() -> None:
    assert labs._held_ports({"port": 3100}) == [3100]
    assert labs._held_ports({"port": 3100, "ports": [3100, 4000]}) == [3100, 4000]
    assert labs._held_ports({}) == []


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_sync_copies_the_files_written_since_the_last_copy_into_the_running_lab(redis):
    # #11799: an edit made after the start reaches the lab's copy without a restart.
    workspace = _workspace()
    resolved = await _resolved(workspace)
    fake = _Kube()
    started = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert started.ready, started.text
    held = (await workspace.record(create=False)).kernel_state["labs"]["co:lab"]
    assert isinstance(held["copied_at"], float)
    # The copy is pushed back in time past the slack, so only the edit made after it is new.
    await labs._remember(workspace, "co:lab", {**held, "copied_at": held["copied_at"] - 1000})
    for path in ("co/.xtm-one/checks.toml", "co/backend/app.py"):
        entry = (await workspace.entries(refresh=True))[path]
        data = (await workspace.get_bytes(path))[1]
        await workspace.put_bytes(path, data, mtime=entry.mtime - 2000)
    # The edit lands after that (pushed-back) copy and before this test's clock: only it is new.
    await workspace.put_bytes("co/backend/app.py", b"print('edited')\n", mtime=held["copied_at"] - 500)
    fake.execs.clear()
    synced = await labs.sync_lab(resolved, workspace=workspace, client=fake)
    assert not synced.refused and "synced 1 file(s)" in synced.text and synced.service_id == started.service_id
    ((pod, container, command, stdin),) = fake.execs
    assert (pod, container) == (started.service_id, "workspace") and "tar -xf -" in command[2]
    with tarfile.open(fileobj=io.BytesIO(stdin)) as tf:
        assert tf.getnames() == ["co/backend/app.py"]
        assert tf.extractfile("co/backend/app.py").read() == b"print('edited')\n"
    # The stamp moved on: a second sync with nothing new copies nothing.
    fake.execs.clear()
    again = await labs.sync_lab(resolved, workspace=workspace, client=fake)
    assert not again.refused and "nothing" in again.text and fake.execs == []
    # The pod never changed.
    assert list(fake.pods) == [started.service_id]


def test_a_service_runs_under_a_supervisor_that_reruns_it_on_a_restart_only():
    # #12104: the process runs in its own group, its pid is written for a restart, and it is run again only when a
    # restart set the flag; otherwise the container ends with the process's own status (restartPolicy Never).
    block = labs._supervised(["java", "-jar", "app.jar"])
    assert "setsid java -jar app.jar &" in block and f"echo $c > {labs._CHILD_PID}" in block
    assert f"[ -f {labs._RESTART_FLAG} ] || exit $s" in block and "trap " in block
    restart = labs.restart_script()
    assert restart.startswith(f"[ -f {labs._CHILD_PID} ] || exit 3")
    assert restart.index(f"touch {labs._RESTART_FLAG}") < restart.index("kill -TERM -$c") < restart.index("kill -KILL")


def test_a_restart_waits_for_the_whole_process_group_and_kills_it_once_never_in_a_loop() -> None:
    # #12198: the leader (a lab script) ended at once on SIGTERM while its children (PostgreSQL, a JVM) went on; the
    # restart and the supervisor both waited on the leader alone, so the rerun met the old database. Both now wait on
    # the group, the SIGKILL is sent once, and every wait is bounded.
    restart = labs.restart_script()
    assert labs._GROUP_ALIVE in restart and "kill -0" not in restart  # /proc, zombies skipped
    assert restart.count("kill -KILL") == 2  # the group, then the leader as fallback
    assert restart.count(f"$i -lt {labs._RESTART_TERM_SECONDS}") == 1
    assert restart.count(f"$i -lt {labs._RESTART_KILL_SECONDS}") == 1 and restart.endswith("exit 0")
    assert "exit 5" in restart and 'case "$c" in' in restart
    block = labs._supervised(["python", "lab.py"])
    wait_group = f"while g && [ $i -lt {labs._RESTART_TERM_SECONDS + labs._RESTART_KILL_SECONDS} ]"
    assert (
        block.index(labs._GROUP_ALIVE)
        < block.index(f"[ -f {labs._RESTART_FLAG} ] || exit $s")
        < block.index(wait_group)
    )


@pytest.mark.skipif(not __import__("shutil").which("setsid"), reason="needs setsid (util-linux)")
def test_the_restart_script_ends_a_group_whose_child_outlives_its_leader(tmp_path: pathlib.Path) -> None:
    # #12198, run for real: a leader that dies on SIGTERM and a child that ignores it. The restart answers 0 only
    # once the child is gone too (one SIGKILL), well within its bounds.
    import contextlib
    import os
    import signal
    import subprocess
    import time

    script = labs.restart_script().replace(labs._CHILD_PID, str(tmp_path / "pid"))
    script = script.replace(labs._RESTART_FLAG, str(tmp_path / "flag"))
    script = script.replace(f"$i -lt {labs._RESTART_TERM_SECONDS}", "$i -lt 2")
    group = subprocess.Popen(
        ["/bin/sh", "-c", "sh -c 'trap \"\" TERM; while :; do sleep 1; done' & wait"], start_new_session=True
    )
    try:
        (tmp_path / "pid").write_text(str(group.pid))
        time.sleep(0.5)
        started = time.monotonic()
        done = subprocess.run(["/bin/sh", "-c", script], timeout=30, check=False)  # noqa: S603 - the script under test
        assert done.returncode == 0 and time.monotonic() - started < 15
        assert (tmp_path / "flag").is_file()
        group.wait(timeout=5)
        with pytest.raises(ProcessLookupError):
            os.killpg(group.pid, 0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(group.pid, signal.SIGKILL)


def test_the_restart_script_refuses_a_pid_file_that_names_no_process_group(tmp_path: pathlib.Path) -> None:
    import subprocess

    (tmp_path / "pid").write_text("1")
    script = labs.restart_script().replace(labs._CHILD_PID, str(tmp_path / "pid"))
    assert subprocess.run(["/bin/sh", "-c", script], timeout=10, check=False).returncode == 4  # noqa: S603


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_sync_with_restart_reruns_that_service_in_its_container_and_waits_for_it(redis):
    workspace = _workspace()
    resolved = await _resolved(workspace)
    fake = _Kube()
    started = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert started.ready, started.text
    fake.execs.clear()
    clock = iter(range(0, 10_000, 5))
    outcome = await labs.sync_lab(
        resolved, workspace=workspace, client=fake, restart="db", sleep=_no_sleep, clock=lambda: float(next(clock))
    )
    assert outcome.ready and not outcome.refused, outcome.text
    assert "db restarted in its container" in outcome.text and "build caches were kept" in outcome.text
    # The sync's copy (when a file is new), then the restart, in the db container alone.
    pod, container, command, stdin = fake.execs[-1]
    assert [e[1] for e in fake.execs if e[3] is None] == ["svc-db"]
    assert (pod, container, stdin) == (started.service_id, "svc-db", None)
    assert command[:2] == ["sh", "-c"] and command[2] == labs.restart_script()
    # The same pod: nothing created, nothing deleted.
    assert list(fake.pods) == [started.service_id] and fake.deleted == []


def test_the_supervisor_marks_each_rerun_in_the_container_output() -> None:
    # #12236: the restart waits for ready_log after this mark, never on the old run's line.
    block = labs._supervised(["python", "lab.py"])
    assert labs._RESTART_MARK in block
    assert block.index(f"[ -f {labs._RESTART_FLAG} ] || exit $s") < block.index(labs._RESTART_MARK)
    assert labs._after_restart("lab up: one\n") is None
    text = f"lab up: old\n{labs._RESTART_MARK}\nmigrating\n"
    assert labs._after_restart(text) == "\nmigrating\n"


def test_a_service_states_its_restart_target_in_seconds() -> None:
    from app.services.sandbox_workspace.checks import ChecksError, load_services

    specs = load_services(
        '[service.a]\nargv = ["x"]\nport = 1\nrestart_target = 120\n[service.b]\nargv = ["y"]\nport = 2\n'
    )
    assert specs["a"].restart_target == 120 and specs["b"].restart_target == 0
    with pytest.raises(ChecksError, match="restart_target must be a positive number"):
        load_services('[service.a]\nargv = ["x"]\nport = 1\nrestart_target = 0\n')


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_restart_is_ready_only_once_the_new_run_logs_its_ready_log_and_names_its_target(redis):
    # #12236: the port answers before the restarted lab has migrated and seeded; the old run's "lab up" is still in
    # the log, so only the lines after the supervisor's mark count. The answer says whether the target was met.
    workspace = _workspace()
    text = SERVICES.replace(
        'ready_path = "/api/health"\n', 'ready_path = "/api/health"\nready_log = "lab up: "\nrestart_target = 120\n'
    )
    await workspace.put_tree([("co/.xtm-one/checks.toml", text.encode()), ("co/backend/app.py", b"print('hi')\n")])
    resolved = await services.resolve(workspace, checkout="co", service="lab", known_images={"python", "python-dev"})
    fake = _Kube()
    reads: list[str] = []

    async def logs(name, container, *, tail_lines, limit_bytes):
        reads.append(container)
        old = "[lab 40.0s] lab up: sign in\n"
        if not any(e[3] is None for e in fake.execs):
            return old
        new = "[lab 50.0s] lab up: sign in\n" if reads.count("svc-lab") >= 3 else "[lab 2.0s] migrating\n"
        return f"{old}{labs._RESTART_MARK}\n{new}"

    fake.logs = logs  # type: ignore[method-assign]
    started = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert started.ready, started.text
    reads.clear()
    clock = iter(range(0, 10_000, 10))
    outcome = await labs.sync_lab(
        resolved, workspace=workspace, client=fake, restart="lab", sleep=_no_sleep, clock=lambda: float(next(clock))
    )
    assert outcome.ready, outcome.text
    assert reads.count("svc-lab") >= 3
    assert "its restart target of 120s" in outcome.text and ("within" in outcome.text or "over" in outcome.text)
    # Only the lab's container was restarted: the database kept running.
    assert [e[1] for e in fake.execs if e[3] is None] == ["svc-lab"] and fake.deleted == []


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_restart_of_an_unknown_service_or_of_an_old_pod_is_refused_with_why(redis):
    workspace = _workspace()
    resolved = await _resolved(workspace)
    fake = _Kube()
    started = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    fake.execs.clear()
    unknown = await labs.sync_lab(resolved, workspace=workspace, client=fake, restart="web", sleep=_no_sleep)
    assert unknown.refused and "not a service of this lab (db, lab)" in unknown.text and fake.execs == []
    # A pod created before the supervisor: the restart script finds no pid file and exits 3.
    fake.command_exit = 3
    old = await labs.sync_lab(resolved, workspace=workspace, client=fake, restart="lab", sleep=_no_sleep)
    assert old.refused and "predates in-place restarts" in old.text and old.service_id == started.service_id


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_restart_of_an_exited_service_says_it_exited_without_an_exec(redis):
    # #115: an exec into a container that has exited is refused by the API server (HTTP 500 on the upgrade).
    workspace = _workspace()
    resolved = await _resolved(workspace)
    fake = _Kube()
    started = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    for status in fake.pods[started.service_id]["status"]["containerStatuses"]:
        if status["name"] == "svc-lab":
            status["ready"] = False
            status["state"] = {"terminated": {"exitCode": 1, "reason": "Error"}}
    fake.execs.clear()
    outcome = await labs.sync_lab(resolved, workspace=workspace, client=fake, restart="lab", sleep=_no_sleep)
    assert outcome.refused and outcome.service_id == started.service_id, outcome.text
    assert "exited with code 1 (Error)" in outcome.text and "cannot be run again in place" in outcome.text
    assert "sandbox_stop_service" in outcome.text and "svc-lab line 4" in outcome.text
    # The copy may run in the workspace container; nothing ran in the exited one.
    assert [e for e in fake.execs if e[1] == "svc-lab"] == []


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_restarted_service_that_does_not_answer_in_the_budget_is_answered_starting(redis):
    workspace = _workspace()
    resolved = await _resolved(workspace)
    fake = _Kube()
    started = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    fake.never_ready = True  # the sync's copy leaves the containers as they are
    for status in fake.pods[started.service_id]["status"]["containerStatuses"]:
        status["ready"] = False
    clock = iter(range(0, 10_000, 10))
    outcome = await labs.sync_lab(
        resolved,
        workspace=workspace,
        client=fake,
        restart="lab",
        sleep=_no_sleep,
        clock=lambda: float(next(clock)),
        answer_seconds=60,
    )
    assert outcome.starting and not outcome.refused and "not answering yet" in outcome.text, outcome.text


def test_the_answer_budget_leaves_room_under_the_loop_tool_timeout():
    # #12104: one start answers a lab ready within 4 minutes; the copy and the session read fit in what is left.
    assert labs.ANSWER_SECONDS == 240.0 and labs.ANSWER_SECONDS + 45 < 300


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_sync_without_a_running_lab_is_refused(redis):
    workspace = _workspace()
    resolved = await _resolved(workspace)
    fake = _Kube()
    refused = await labs.sync_lab(resolved, workspace=workspace, client=fake)
    assert refused.refused and "start it first" in refused.text and fake.execs == []


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_checkout_that_cannot_be_unpacked_deletes_the_pod(redis):
    workspace = _workspace()
    fake = _Kube()
    fake.exec_exit = 2
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    assert outcome.refused and "tar: broken" in outcome.text
    assert fake.pods == {} and await labs.find(workspace, "co:lab") is None
    # A tar that exits non-zero would fail the same way again: answered at once, never retried.
    assert len(fake.execs) == 1


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_stalled_checkout_copy_is_retried_once_within_its_own_budget(redis, monkeypatch):
    monkeypatch.setattr(settings, "agentic_labs_copy_seconds", 300)
    workspace = _workspace()
    fake = _Kube()
    fake.copy_stalls = 1
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    assert outcome.ready, outcome.text
    # Two copies on the same pod, each with the copy budget, never the start budget.
    assert len(fake.execs) == 2 and {e[0] for e in fake.execs} == {outcome.service_id}
    assert fake.exec_timeouts == [300.0, 300.0]


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_copy_that_stalls_twice_is_answered_and_the_pod_deleted(redis):
    workspace = _workspace()
    fake = _Kube()
    fake.copy_stalls = 2
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    assert outcome.refused and "could not be put in the lab" in outcome.text and "after 2 attempts" in outcome.text
    assert fake.pods == {} and await labs.find(workspace, "co:lab") is None


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_copy_refused_at_the_handshake_is_retried_after_a_pause(redis):
    # lab-start-websocket-404: a 404 on the exec upgrade around a worker loss, the pod there a moment later.
    workspace = _workspace()
    fake = _Kube()
    fake.copy_refusals = 1
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=sleep)
    assert outcome.ready, outcome.text
    assert len([e for e in fake.execs if e[3] is not None]) == 2
    assert labs._COPY_REFUSED_PAUSE_SECONDS in pauses


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_copy_refused_twice_names_the_status_and_deletes_the_pod(redis):
    workspace = _workspace()
    fake = _Kube()
    fake.copy_refusals = 2
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    assert outcome.refused and "HTTP 404" in outcome.text and "after 2 attempts" in outcome.text
    assert fake.pods == {} and await labs.find(workspace, "co:lab") is None


@pytest.mark.parametrize("refusal", ["status", "closed", "os"])
async def test_exec_turns_a_refused_or_lost_stream_into_a_kube_error(monkeypatch, refusal):
    from types import SimpleNamespace

    import websockets.asyncio.client as ws_client
    from websockets.exceptions import ConnectionClosedError, InvalidStatus

    def connect(*_args, **_kwargs):
        if refusal == "status":
            raise InvalidStatus(SimpleNamespace(status_code=404))
        if refusal == "closed":
            raise ConnectionClosedError(None, None)
        raise ConnectionResetError("reset by peer")

    monkeypatch.setattr(ws_client, "connect", connect)
    config = kube.KubeConfig(api_url="http://api", namespace="labs", token_path="/nonexistent", ca_path="")
    monkeypatch.setattr(kube.KubeConfig, "token", lambda self: "t")
    client = kube.KubeClient(config)
    with pytest.raises(kube.KubeError) as raised:
        await client.exec("lab-1", "workspace", ["true"], stdin=b"x")
    if refusal == "status":
        assert raised.value.status == 0 and raised.value.handshake == 404 and "HTTP 404" in str(raised.value)
    else:
        assert raised.value.status == 0 and raised.value.handshake == 0 and "exec stream was lost" in str(raised.value)


class _ClosingStream:
    """An exec stream whose server closes it while stdin is sent, after the frames it holds (lab-start-exec-stream-lost)."""

    def __init__(self, frames: list[bytes], *, close_on_send: bool = True) -> None:
        self.frames = frames
        self.close_on_send = close_on_send
        self.sent: list[bytes] = []

    async def __aenter__(self) -> _ClosingStream:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def send(self, data: bytes) -> None:
        from websockets.exceptions import ConnectionClosedOK

        self.sent.append(data)
        if self.close_on_send:
            raise ConnectionClosedOK(None, None)

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._frames()

    async def _frames(self) -> AsyncIterator[bytes]:
        for frame in self.frames:
            yield frame


def _exec_client(monkeypatch: pytest.MonkeyPatch, stream: _ClosingStream) -> kube.KubeClient:
    import websockets.asyncio.client as ws_client

    monkeypatch.setattr(ws_client, "connect", lambda *_a, **_k: stream)
    config = kube.KubeConfig(api_url="http://api", namespace="labs", token_path="/nonexistent", ca_path="")
    monkeypatch.setattr(kube.KubeConfig, "token", lambda self: "t")
    return kube.KubeClient(config)


async def test_exec_closed_while_stdin_is_sent_still_reads_the_status(monkeypatch: pytest.MonkeyPatch) -> None:
    # tar ends at its end-of-archive blocks and the server closes the stream before the last chunk lands.
    status = json.dumps({"status": "Success"}).encode()
    stream = _ClosingStream([bytes([1]) + b"unpacked", bytes([3]) + status])
    result = await _exec_client(monkeypatch, stream).exec("lab-1", "workspace", ["tar"], stdin=b"x")
    assert result.exit_code == 0 and result.stdout == b"unpacked" and len(stream.sent) == 1


async def test_exec_closed_while_stdin_is_sent_keeps_a_failed_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    status = json.dumps(
        {"status": "Failure", "message": "exit 2", "details": {"causes": [{"reason": "ExitCode", "message": "2"}]}}
    ).encode()
    stream = _ClosingStream([bytes([2]) + b"tar: broken", bytes([3]) + status])
    result = await _exec_client(monkeypatch, stream).exec("lab-1", "workspace", ["tar"], stdin=b"x")
    assert result.exit_code == 2 and result.stderr == b"tar: broken"


async def test_exec_closed_with_no_status_frame_is_still_a_lost_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _ClosingStream([bytes([1]) + b"partial"])
    with pytest.raises(kube.KubeError) as raised:
        await _exec_client(monkeypatch, stream).exec("lab-1", "workspace", ["tar"], stdin=b"x")
    assert "exec stream was lost" in str(raised.value) and raised.value.handshake == 0


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_lab_that_never_answers_says_where_to_read_its_logs(redis, monkeypatch):
    workspace = _workspace()
    fake = _Kube()
    fake.never_ready = True
    clock = iter(range(0, 10_000, 50))
    outcome = await labs.start_lab(
        await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep, clock=lambda: float(next(clock)),
        answer_seconds=float("inf"),
    )
    assert not outcome.ready and not outcome.starting and "not ready after" in outcome.text
    assert "sandbox_service_logs" in outcome.text
    assert outcome.service_id in fake.pods  # kept for its logs
    # Its whole window is over: a later start replaces it, never follows it.
    assert (await labs.find(workspace, "co:lab"))[1]["timed_out"] is True


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_lab_whose_readiness_outlives_the_answer_budget_is_answered_starting_with_its_id(redis):
    # dev:sandbox-start-service-background: an OpenCTI lab declares 900 s of readiness, past the 300 s tool timeout.
    # The start answers within its budget with the service_id, the services' states and how to follow it.
    workspace = _workspace()
    fake = _Kube()
    fake.never_ready = True
    clock = iter(range(0, 10_000, 10))
    outcome = await labs.start_lab(
        await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep, clock=lambda: float(next(clock)),
        answer_seconds=60,
    )
    assert outcome.starting and not outcome.ready and not outcome.refused, outcome.text
    assert outcome.service_id in fake.pods and outcome.seconds < 150
    assert outcome.text.startswith("lab: starting (not ready after")
    assert f"service_id: {outcome.service_id}" in outcome.text
    assert "services: db starting, lab starting" in outcome.text and "sandbox_service_logs" in outcome.text
    assert outcome.preview_url == "http://10.42.0.9:3100/"
    # The logs are read while it starts.
    logs = await labs.lab_logs(workspace, outcome.service_id, service="lab", client=fake)
    assert "svc-lab line 4" in logs.text
    # A second start follows the same pod (never a second lab) and answers ready once it is.
    for status in fake.pods[outcome.service_id]["status"]["containerStatuses"]:
        status["ready"] = True
    again = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    assert again.ready and again.service_id == outcome.service_id and list(fake.pods) == [outcome.service_id]


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_second_start_of_a_lab_still_starting_follows_it_and_creates_no_pod(redis):
    workspace = _workspace()
    fake = _Kube()
    fake.never_ready = True
    first = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep, background=True)
    assert first.starting and first.service_id in fake.pods, first.text
    clock = iter(range(0, 10_000, 10))
    again = await labs.start_lab(
        await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep, clock=lambda: float(next(clock)),
        answer_seconds=30,
    )
    assert again.starting and again.service_id == first.service_id and "already starting" in again.text, again.text
    assert list(fake.pods) == [first.service_id] and fake.deleted == []


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_background_start_answers_once_the_checkout_is_in_without_waiting_for_readiness(redis):
    workspace = _workspace()
    fake = _Kube()
    fake.never_ready = True
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep, background=True)
    assert outcome.starting and outcome.service_id in fake.pods, outcome.text
    held = (await labs.find(workspace, "co:lab"))[1]
    assert held["files"] == 2 and "ready_seconds" not in held
    status = await labs.lab_status(workspace, outcome.service_id, client=fake)
    assert "services: db starting, lab starting" in status.text


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_the_status_names_a_service_that_exited_with_its_code(redis):
    workspace = _workspace()
    fake = _Kube()
    fake.never_ready = True
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep, background=True)
    for status in fake.pods[outcome.service_id]["status"]["containerStatuses"]:
        if status["name"] == "svc-db":
            status["ready"] = True
        if status["name"] == "svc-lab":
            status["state"] = {"terminated": {"exitCode": 3, "reason": "Error"}}
    status = await labs.lab_status(workspace, outcome.service_id, client=fake)
    assert "services: db ready, lab exited with code 3 (Error)" in status.text
    # An ended service is never followed: the next start replaces the lab.
    fake.never_ready = False
    again = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    assert again.ready and again.service_id != outcome.service_id and outcome.service_id in fake.deleted


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_the_status_names_why_a_starting_service_waits_and_its_restarts(redis):
    workspace = _workspace()
    fake = _Kube()
    fake.never_ready = True
    outcome = await labs.start_lab(
        await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep, background=True
    )
    for status in fake.pods[outcome.service_id]["status"]["containerStatuses"]:
        if status["name"] == "svc-db":
            status["state"] = {"waiting": {"reason": "ContainerCreating"}}
        if status["name"] == "svc-lab":
            status["restartCount"] = 2
    status = await labs.lab_status(workspace, outcome.service_id, client=fake)
    assert "services: db starting (ContainerCreating), lab starting (restarted 2x)" in status.text, status.text


async def _resolved_with_ready_log(workspace: WorkspaceSession) -> services.ResolvedService:
    text = SERVICES.replace('ready_path = "/api/health"\n', 'ready_path = "/api/health"\nready_log = "lab up: "\n')
    await workspace.put_tree([("co/.xtm-one/checks.toml", text.encode()), ("co/backend/app.py", b"print('hi')\n")])
    return await services.resolve(workspace, checkout="co", service="lab", known_images={"python", "python-dev"})


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_lab_with_ready_log_is_ready_only_once_its_container_log_holds_it(redis):
    # Ready on its port, the lab still seeds and prints its URLs: the start waits for "lab up" in the container log
    # of the service that declares it (dev:lab-start-log-flush).
    workspace = _workspace()
    fake = _Kube()
    reads: list[str] = []

    async def logs(name, container, *, tail_lines, limit_bytes):
        reads.append(container)
        if container == "svc-lab" and reads.count("svc-lab") >= 3:
            return "[lab 43.0s] seeded signed-in session (administrator): http://x\n[lab 43.1s] lab up: sign in\n"
        return "[lab 29.1s] frontend ready\n"

    fake.logs = logs  # type: ignore[method-assign]
    outcome = await labs.start_lab(await _resolved_with_ready_log(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    assert outcome.ready, outcome.text
    # Only the service that declares it is read; the database declares none.
    assert reads == ["svc-lab", "svc-lab", "svc-lab"]


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_lab_that_never_logs_its_ready_log_is_not_ready_and_kept_for_its_logs(redis):
    workspace = _workspace()
    fake = _Kube()
    clock = iter(range(0, 100_000, 50))
    outcome = await labs.start_lab(
        await _resolved_with_ready_log(workspace), workspace=workspace, client=fake, sleep=_no_sleep,
        clock=lambda: float(next(clock)), answer_seconds=float("inf"),
    )
    assert not outcome.ready and "did not log 'lab up: '" in outcome.text and "sandbox_service_logs" in outcome.text
    assert outcome.service_id in fake.pods


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_lab_that_exits_while_its_ready_log_is_awaited_is_answered_at_once_with_its_exit_and_tail(redis):
    # Ready on its port, the lab script then dies (a seed refused): the start answers the exit, its code and the last
    # lines of that container's own output at the next poll, not at the end of its readiness window
    # (dev:lab-start-answer-on-exit).
    workspace = _workspace()
    fake = _Kube()
    reads: list[tuple[str, int]] = []
    ticks = iter(range(100_000))

    async def logs(name, container, *, tail_lines, limit_bytes):
        reads.append((container, tail_lines))
        if len(reads) == 2:
            for status in fake.pods[name]["status"]["containerStatuses"]:
                if status["name"] == "svc-lab":
                    status["state"] = {"terminated": {"exitCode": 1, "reason": "Error"}}
        if container == "svc-db":
            return "db line\n"
        return "\n".join(f"[lab {i}.0s] step {i}" for i in range(30)) + "\nlab: seed refused\n"

    fake.logs = logs  # type: ignore[method-assign]
    outcome = await labs.start_lab(
        await _resolved_with_ready_log(workspace), workspace=workspace, client=fake, sleep=_no_sleep,
        clock=lambda: float(next(ticks)),
    )
    assert not outcome.ready and "lab exited with code 1 (Error)" in outcome.text, outcome.text
    assert "did not log" not in outcome.text
    # Its own last 20 lines close the answer; the database's are not read.
    lines = outcome.text.splitlines()
    assert lines[-1] == "lab: seed refused" and "Its last 20 line(s):" in lines and "db line" not in outcome.text
    assert reads == [("svc-lab", 2000), ("svc-lab", 2000), ("svc-lab", 20)]
    assert outcome.seconds < 30  # far inside the 300 s readiness window of the declaration
    assert outcome.service_id in fake.pods  # kept for its logs


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_lab_that_exits_before_its_port_answers_carries_its_last_lines(redis):
    workspace = _workspace()
    fake = _Kube()
    fake.never_ready = True
    real_create = fake.create_pod

    async def create_pod(manifest):
        pod = await real_create(manifest)
        for status in pod["status"]["containerStatuses"]:
            if status["name"] == "svc-lab":
                status["state"] = {"terminated": {"exitCode": 2, "reason": "Error"}}
        return pod

    fake.create_pod = create_pod  # type: ignore[method-assign]
    # Only the service container ended: the workspace container runs and the checkout is unpacked.
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    assert not outcome.ready and "lab exited with code 2 (Error)" in outcome.text, outcome.text
    assert outcome.text.splitlines()[-1] == "svc-lab line 4"


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_second_start_of_a_lab_still_seeding_waits_for_its_ready_log(redis):
    # The first start's answer was lost (a run resume) while the lab seeded: its pod is ready on its port, its record
    # has no ready_seconds. The second start answers only once "lab up" is in the log, never "ready" before it.
    workspace = _workspace()
    resolved = await _resolved_with_ready_log(workspace)
    fake = _Kube()
    fake.never_ready = True
    clock = iter(range(0, 100_000, 50))
    first = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep, clock=lambda: float(next(clock)))
    assert not first.ready
    for status in fake.pods[first.service_id]["status"]["containerStatuses"]:
        status["ready"] = True
    reads: list[str] = []

    async def logs(name, container, *, tail_lines, limit_bytes):
        reads.append(container)
        if len(reads) >= 3:
            return "[lab 43.0s] seeded signed-in session (administrator): http://x\n[lab 43.1s] lab up: sign in\n"
        return "[lab 29.1s] frontend ready\n"

    fake.logs = logs  # type: ignore[method-assign]
    again = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert again.ready and "already running, its start completed" in again.text, again.text
    assert again.service_id == first.service_id and reads == ["svc-lab", "svc-lab", "svc-lab"]
    held = (await workspace.record(create=False)).kernel_state["labs"]["co:lab"]
    assert "ready_seconds" in held
    # Once its start completed, a third start answers at once, without reading the log.
    third = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert third.ready and third.text.splitlines()[0].endswith("(already running)") and len(reads) == 3


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_second_start_of_a_lab_that_never_logs_its_ready_log_is_not_ready(redis):
    workspace = _workspace()
    resolved = await _resolved_with_ready_log(workspace)
    fake = _Kube()
    fake.never_ready = True
    clock = iter(range(0, 100_000, 50))
    first = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep, clock=lambda: float(next(clock)))
    for status in fake.pods[first.service_id]["status"]["containerStatuses"]:
        status["ready"] = True
    again = await labs.start_lab(
        resolved, workspace=workspace, client=fake, sleep=_no_sleep, clock=lambda: float(next(clock)),
        answer_seconds=float("inf"),
    )
    assert not again.ready and "did not log 'lab up: '" in again.text and again.service_id == first.service_id
    assert first.service_id in fake.pods  # kept for its logs


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_lab_ready_after_a_timed_out_start_is_adopted_not_a_key_error(redis):
    workspace = _workspace()
    resolved = await _resolved(workspace)
    fake = _Kube()
    fake.never_ready = True
    clock = iter(range(0, 10_000, 50))
    first = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep, clock=lambda: float(next(clock)))
    assert not first.ready
    held = (await workspace.record(create=False)).kernel_state["labs"]["co:lab"]
    assert "pod_ip" not in held and "preview_url" not in held  # the partial record a timed-out start keeps
    # The pod becomes ready later: the next start adopts it from its live status.
    for status in fake.pods[first.service_id]["status"]["containerStatuses"]:
        status["ready"] = True
    again = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert again.ready and "already running" in again.text, again.text
    assert again.service_id == first.service_id and again.preview_url == "http://10.42.0.9:3100/"
    held = (await workspace.record(create=False)).kernel_state["labs"]["co:lab"]
    assert held["pod_ip"] == "10.42.0.9" and held["preview_url"] == again.preview_url
    assert redis.keys == {"sandbox_preview_origin:10.42.0.9:3100": 12 * 3600}


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_partial_lab_from_another_declaration_is_replaced_not_adopted(redis):
    workspace = _workspace()
    resolved = await _resolved(workspace)
    fake = _Kube()
    fake.never_ready = True
    clock = iter(range(0, 10_000, 50))
    first = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep, clock=lambda: float(next(clock)))
    for status in fake.pods[first.service_id]["status"]["containerStatuses"]:
        status["ready"] = True
    record = await workspace.record(create=False)
    record.kernel_state["labs"]["co:lab"]["fingerprint"] = "older"  # argv, cwd, env... changed since that start
    fake.never_ready = False
    again = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert again.ready and "already running" not in again.text
    assert first.service_id in fake.deleted and again.service_id != first.service_id


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_lab_stopped_while_it_is_adopted_leaves_no_record(redis):
    workspace = _workspace()
    resolved = await _resolved(workspace)
    fake = _Kube()
    fake.never_ready = True
    clock = iter(range(0, 10_000, 50))
    first = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep, clock=lambda: float(next(clock)))
    for status in fake.pods[first.service_id]["status"]["containerStatuses"]:
        status["ready"] = True
    reads = fake.get_pod
    stops: list[asyncio.Task] = []

    async def _stopped_during_the_read(name):
        pod = await reads(name)
        if not stops:
            # A concurrent sandbox_stop_service: it waits for the adoption's lock, never runs inside it.
            stops.append(asyncio.create_task(labs.stop_lab(workspace, first.service_id, client=fake)))
            for _ in range(5):
                await asyncio.sleep(0)
            assert not stops[0].done()
        return pod

    fake.get_pod = _stopped_during_the_read
    again = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert again.ready and again.service_id == first.service_id, again.text
    stopped = await stops[0]
    assert "stopped" in stopped.text and first.service_id in fake.deleted
    # The stop found the record the adoption completed: neither the record nor the preview outlive it.
    assert await labs.find(workspace, "co:lab") is None
    assert redis.keys == {}


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_terminating_pod_is_replaced_not_adopted(redis):
    workspace = _workspace()
    resolved = await _resolved(workspace)
    fake = _Kube()
    fake.never_ready = True
    clock = iter(range(0, 10_000, 50))
    first = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep, clock=lambda: float(next(clock)))
    for status in fake.pods[first.service_id]["status"]["containerStatuses"]:
        status["ready"] = True
    fake.pods[first.service_id]["metadata"]["deletionTimestamp"] = "2026-11-01T13:00:00Z"
    fake.never_ready = False
    again = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert again.ready and "already running" not in again.text
    assert first.service_id in fake.deleted and again.service_id != first.service_id


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_an_api_error_while_adopting_keeps_the_lab(redis):
    workspace = _workspace()
    resolved = await _resolved(workspace)
    fake = _Kube()
    first = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert first.ready

    async def _unreachable(_name):
        raise kube.KubeError("the API server did not answer", 503)

    fake.get_pod = _unreachable
    again = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert again.refused and "could not be read" in again.text
    # A transient error proves nothing: the record and the preview stay, no pod is deleted.
    assert (await labs.find(workspace, "co:lab"))[1]["pod"] == first.service_id
    assert redis.keys == {"sandbox_preview_origin:10.42.0.9:3100": 12 * 3600} and fake.deleted == []


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_replaced_lab_withdraws_its_old_preview(redis):
    workspace = _workspace()
    resolved = await _resolved(workspace)
    fake = _Kube()
    first = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert first.ready and redis.keys == {"sandbox_preview_origin:10.42.0.9:3100": 12 * 3600}
    record = await workspace.record(create=False)
    record.kernel_state["labs"]["co:lab"]["fingerprint"] = "older"
    record.kernel_state["labs"]["co:lab"]["pod_ip"] = "10.42.0.7"  # the old pod's own address
    record.kernel_state["labs"]["co:lab"]["preview_url"] = "http://10.42.0.7:3100/"
    redis.keys["sandbox_preview_origin:10.42.0.7:3100"] = 60
    redis.values["sandbox_preview_origin:10.42.0.7:3100"] = first.service_id
    again = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert again.ready and first.service_id in fake.deleted
    assert "sandbox_preview_origin:10.42.0.7:3100" not in redis.keys


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_lab_recorded_before_the_fingerprint_survives_the_upgrade(redis):
    workspace = _workspace()
    resolved = await _resolved(workspace)
    fake = _Kube()
    first = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    record = await workspace.record(create=False)
    del record.kernel_state["labs"]["co:lab"]["fingerprint"]  # written by the previous version
    again = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert again.ready and "already running" in again.text and again.service_id == first.service_id
    assert fake.deleted == []
    held = (await workspace.record(create=False)).kernel_state["labs"]["co:lab"]
    assert held["fingerprint"] == labs._fingerprint(resolved)
    # A legacy record from another declaration (here another port) is still replaced.
    del held["fingerprint"]
    held["port"] = 9999
    replaced = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    assert replaced.ready and first.service_id in fake.deleted and replaced.service_id != first.service_id


@pytest.mark.usefixtures("lab_settings")
async def test_a_dropped_pod_withdraws_only_the_admission_it_owns(redis):
    fake = _Kube()
    await preview.register_origin("10.42.0.7", 3100, 600, owner="lab-a")
    # The cluster gave the IP to another lab since: its admission now names that pod.
    await preview.register_origin("10.42.0.7", 3100, 600, owner="lab-b")
    await labs._drop_pod(fake, {"pod": "lab-a", "pod_ip": "10.42.0.7", "port": 3100}, delete=False)
    assert redis.values == {"sandbox_preview_origin:10.42.0.7:3100": "lab-b"}
    await labs._drop_pod(fake, {"pod": "lab-b", "pod_ip": "10.42.0.7", "port": 3100}, delete=False)
    assert redis.keys == {}


@pytest.mark.usefixtures("lab_settings")
async def test_an_admission_written_before_owners_is_withdrawn_at_stop(redis):
    fake = _Kube()
    # Written by the previous version: the value names no pod.
    redis.keys["sandbox_preview_origin:10.42.0.7:3100"] = 60
    redis.values["sandbox_preview_origin:10.42.0.7:3100"] = preview.LEGACY_ORIGIN_OWNER
    await labs._drop_pod(fake, {"pod": "lab-a", "pod_ip": "10.42.0.7", "port": 3100}, delete=False)
    assert redis.keys == {}


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_stop_by_an_old_pod_name_leaves_its_replacement_running(redis, monkeypatch):
    workspace = _workspace()
    fake = _Kube()
    first = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    key, value = await labs.find(workspace, first.service_id)
    stale = (key, dict(value))
    # A changed declaration replaced the lab after the stop resolved the old pod name.
    record = await workspace.record(create=False)
    record.kernel_state["labs"]["co:lab"]["pod"] = "lab-replacement"

    async def _stale_find(_workspace, _service_id):
        return stale

    real_find = labs.find
    monkeypatch.setattr(labs, "find", _stale_find)
    refused = await labs.stop_lab(workspace, first.service_id, client=fake)
    assert refused.refused and fake.deleted == []
    held = (await workspace.record(create=False)).kernel_state["labs"]["co:lab"]
    assert held["pod"] == "lab-replacement" and redis.keys
    monkeypatch.setattr(labs, "find", real_find)
    # Stopped by its service key, the current pod is the one stopped.
    stopped = await labs.stop_lab(workspace, "co:lab", client=fake)
    assert not stopped.refused and stopped.service_id == "lab-replacement"


def test_the_fingerprint_covers_every_field_of_the_declaration():
    spec = checks.ServiceSpec(name="lab", argv=("yarn", "dev"), port=3100)
    base = services.ResolvedService(checkout="co", name="lab", chain=(spec,), image="nodejs-dev")
    for changed in (
        dataclasses.replace(spec, argv=("yarn", "start")),
        dataclasses.replace(spec, cwd="frontend"),
        dataclasses.replace(spec, ready_path="/health"),
        dataclasses.replace(spec, env=(("A", "1"),)),
    ):
        assert labs._fingerprint(dataclasses.replace(base, chain=(changed,))) != labs._fingerprint(base)
    assert labs._fingerprint(dataclasses.replace(base)) == labs._fingerprint(base)


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_ready_pod_without_an_ip_names_its_phase(redis):
    workspace = _workspace()
    fake = _Kube()
    created = fake.create_pod

    async def _no_ip(manifest):
        pod = await created(manifest)
        pod["status"].pop("podIP")
        pod["status"]["phase"] = "Pending"
        return pod

    fake.create_pod = _no_ip
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    assert not outcome.ready and "no IP yet (phase Pending)" in outcome.text, outcome.text
    # Started again while it still has no IP: replaced by a new pod, never a KeyError.
    again = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    assert outcome.service_id in fake.deleted and again.service_id != outcome.service_id


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_status_logs_exec_and_stop_find_the_lab_by_id(redis):
    workspace = _workspace()
    fake = _Kube()
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    status = await labs.lab_status(workspace, outcome.service_id, client=fake)
    assert status.ready and "answering on port 3100" in status.text
    logs = await labs.lab_logs(workspace, outcome.service_id, client=fake)
    assert "== db" in logs.text and "== lab" in logs.text and "svc-lab line 4" in logs.text
    refused = await labs.lab_logs(workspace, outcome.service_id, service="nope", client=fake)
    assert refused.refused
    only = await labs.lab_logs(workspace, outcome.service_id, service="lab", pattern="LINE [34]", ignore_case=True, client=fake)
    assert only.text.splitlines()[1:] == ["svc-lab line 3", "svc-lab line 4"] and "2 of 5 lines" in only.text
    invalid = await labs.lab_logs(workspace, outcome.service_id, pattern="[", client=fake)
    assert invalid.refused and "not a valid regular expression" in invalid.text
    result = await labs.lab_exec(workspace, outcome.service_id, ["git", "status"], client=fake)
    assert result.exit_code == 0 and fake.execs[-1][1:3] == ("workspace", ["git", "status"])
    stopped = await labs.stop_lab(workspace, outcome.service_id, client=fake)
    assert "stopped" in stopped.text and fake.pods == {} and redis.keys == {}
    # The read-back after a stop answers a normal not-running state, never an error (#10589).
    after = await labs.lab_status(workspace, outcome.service_id, client=fake)
    assert not after.refused and after.text == f"{outcome.service_id}: not running (stopped or never started in this workspace)."


def test_lab_command_line_runs_in_the_lab_copy_with_the_check_env():
    assert labs.lab_command_line("co", ("python", "x.py", "--task", "a b")) == [
        "sh", "-c", "cd /tmp/workspace/co && exec python x.py --task 'a b'",
    ]
    assert labs.lab_command_line("", ("true",), (("K", "v w"),)) == ["sh", "-c", "cd /tmp/workspace && exec env 'K=v w' true"]


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_run_check_in_lab_execs_in_the_workspace_container(redis):
    workspace = _workspace()
    fake = _Kube()
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    ran = await labs.run_check_in_lab(
        workspace, outcome.service_id, checkout="co", label="lab-routing-eval",
        argv=("python", "eval.py"), cwd="co", client=fake,
    )
    assert not ran.refused and ran.ready, ran.text
    assert ran.text.startswith("lab-routing-eval: exit 0 in ") and f"in lab {outcome.service_id} (co:lab)" in ran.text
    assert fake.execs[-1][1:3] == ("workspace", ["sh", "-c", "cd /tmp/workspace/co && exec python eval.py"])
    # Another checkout's check never runs in this lab.
    other = await labs.run_check_in_lab(
        workspace, outcome.service_id, checkout="other", label="x", argv=("true",), cwd="other", client=fake
    )
    assert other.refused and "runs checkout 'co'" in other.text
    unknown = await labs.run_check_in_lab(workspace, "lab-nope", checkout="co", label="x", argv=("true",), cwd="co", client=fake)
    assert unknown.refused and "no lab 'lab-nope'" in unknown.text
    # A failing check answers its exit code, not a refusal.
    fake.exec_exit = 3
    failed = await labs.run_check_in_lab(
        workspace, outcome.service_id, checkout="co", label="x", argv=("false",), cwd="co", client=fake
    )
    assert not failed.refused and not failed.ready and "x: exit 3 in " in failed.text


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_run_check_in_lab_keeps_the_lab_when_the_exec_upgrade_is_refused(redis):
    """A 404 on the exec upgrade is not a pod gone: the lab record stays, only a REST 404 drops it."""
    workspace = _workspace()
    fake = _Kube()
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)

    async def refused(*_args, **_kwargs):
        raise kube.KubeError("the API server refused the exec stream (HTTP 404)", handshake=404)

    fake.exec = refused
    ran = await labs.run_check_in_lab(
        workspace, outcome.service_id, checkout="co", label="x", argv=("true",), cwd="co", client=fake
    )
    assert ran.refused and "gone" not in ran.text and "HTTP 404" in ran.text
    assert await labs.find(workspace, "co:lab") is not None


def test_bounded_keeps_the_end():
    assert labs._bounded(b"abcdef", 3) == "[3 earlier characters cut]\ndef"
    assert labs._bounded(b"abc", 3) == "abc"


class _FlakyDeletes(_Kube):
    """An API server whose first *failures* deletes get no answer, and that still lands them when *lands* is set."""

    def __init__(self, failures: int, *, lands: bool = False) -> None:
        super().__init__()
        self.failures = failures
        self.lands = lands

    async def delete_pod(self, name):
        if self.failures:
            self.failures -= 1
            if self.lands:
                self.pods.get(name, {}).setdefault("metadata", {})["deletionTimestamp"] = "2026-11-10T00:00:00Z"
            raise kube.KubeError("DELETE pods: no answer from the API server (ReadTimeout)")
        return await super().delete_pod(name)


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_stop_whose_delete_times_out_once_is_retried_and_answers_stopped(redis):
    workspace = _workspace()
    fake = _FlakyDeletes(1)
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    stopped = await labs.stop_lab(workspace, outcome.service_id, client=fake)
    assert not stopped.refused and "stopped" in stopped.text and fake.pods == {}
    assert await labs.find(workspace, outcome.service_id) is None


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_stop_that_cannot_delete_answers_its_error_and_keeps_the_lab_findable(redis):
    workspace = _workspace()
    fake = _FlakyDeletes(5)
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    failed = await labs.stop_lab(workspace, outcome.service_id, client=fake)
    assert failed.refused and "was not stopped" in failed.text and "ReadTimeout" in failed.text
    assert "still Running" in failed.text, failed.text
    # Never an empty success: the record stays, so status and a second stop still reach the pod.
    assert await labs.find(workspace, outcome.service_id) is not None and redis.keys
    fake.failures = 0
    again = await labs.stop_lab(workspace, outcome.service_id, client=fake)
    assert not again.refused and fake.pods == {} and redis.keys == {}


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_a_stop_whose_unanswered_delete_landed_counts_as_stopping(redis):
    workspace = _workspace()
    fake = _FlakyDeletes(5, lands=True)
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    stopped = await labs.stop_lab(workspace, outcome.service_id, client=fake)
    assert not stopped.refused and "terminating" in stopped.text
    assert await labs.find(workspace, outcome.service_id) is None and redis.keys == {}


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_two_labs_of_one_checkout_stopped_in_parallel_both_answer_stopped(redis):
    workspace = _workspace()
    fake = _FlakyDeletes(1)
    resolved = await _resolved(workspace)
    light = dataclasses.replace(resolved, name="lab-light")
    first = await labs.start_lab(resolved, workspace=workspace, client=fake, sleep=_no_sleep)
    # Its target container (svc-lab-light) is none of the pod's: it never turns ready, so its readiness
    # window runs on a test clock rather than for 150 real seconds of polling.
    clock = iter(range(0, 10_000, 10))
    second = await labs.start_lab(light, workspace=workspace, client=fake, sleep=_no_sleep, clock=lambda: float(next(clock)))
    assert first.service_id != second.service_id
    answers = await asyncio.gather(
        labs.stop_lab(workspace, first.service_id, client=fake),
        labs.stop_lab(workspace, second.service_id, client=fake),
    )
    assert all(not answer.refused and "stopped" in answer.text for answer in answers), [a.text for a in answers]
    assert fake.pods == {}
    for service_id in (first.service_id, second.service_id):
        after = await labs.lab_status(workspace, service_id, client=fake)
        assert not after.refused and "not running" in after.text


async def test_a_call_the_api_server_never_answers_is_a_readable_kube_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    client = _client(handler, tmp_path)
    with pytest.raises(kube.KubeError) as raised:
        await client.delete_pod("lab-1")
    assert "no answer from the API server (ReadTimeout)" in str(raised.value) and raised.value.status == 0
    with pytest.raises(kube.KubeError, match="ReadTimeout"):
        await client.patch_annotations("lab-1", {"a": "b"})


@pytest.mark.usefixtures("lab_settings", "no_quota")
async def test_status_answers_env_presence_per_container_never_a_value(redis):
    workspace = _workspace()
    fake = _Kube()
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    pod = fake.pods[outcome.service_id]
    pod["spec"]["containers"][0]["env"] += [
        {"name": "PROBE_SECRET", "value": "s3cr3t-value"},
        {"name": "PROBE_EMPTY", "value": ""},
    ]
    status = await labs.lab_status(
        workspace, outcome.service_id, env=["PROBE_SECRET", "PROBE_EMPTY", "PROBE_ABSENT"], client=fake
    )
    first = next(line for line in status.text.splitlines() if line.startswith("env in workspace:"))
    assert "PROBE_SECRET set" in first and "PROBE_EMPTY set (empty)" in first and "PROBE_ABSENT missing" in first
    assert "s3cr3t-value" not in status.text
    assert sum(line.startswith("env in ") for line in status.text.splitlines()) == len(pod["spec"]["containers"])
    plain = await labs.lab_status(workspace, outcome.service_id, client=fake)
    assert "env in " not in plain.text


async def test_the_reaper_deletes_ended_and_idle_labs_only(lab_settings, redis):
    fake = _Kube()
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)

    def pod(name, phase, idle_hours):
        fake.pods[name] = {
            "metadata": {"name": name, "annotations": {labs.ANNOTATION_LAST_USED: (now - timedelta(hours=idle_hours)).isoformat()}},
            "spec": {"containers": [{"ports": [{"containerPort": 3100}]}]},
            "status": {"phase": phase, "podIP": "10.42.0.1"},
        }

    pod("busy", "Running", 1)
    pod("idle", "Running", 13)
    pod("done", "Succeeded", 0)
    assert sorted(await labs.reap(client=fake, now=now)) == ["done", "idle"]
    assert list(fake.pods) == ["busy"]


async def test_the_idle_reaper_can_be_turned_off(lab_settings, monkeypatch):
    monkeypatch.setattr(settings, "agentic_labs_idle_hours", 0)
    fake = _Kube()
    fake.pods["old"] = {
        "metadata": {"name": "old", "annotations": {labs.ANNOTATION_LAST_USED: "2020-01-01T00:00:00+00:00"}},
        "status": {"phase": "Running"},
    }
    assert await labs.reap(client=fake) == []


async def test_the_reaper_does_nothing_when_labs_are_off(monkeypatch):
    monkeypatch.setattr(settings, "enabled_features", "")
    assert await labs.reap(client=SimpleNamespace()) == []


# -- the preview policy for a lab pod ----------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:3100/",  # loopback
        "http://169.254.169.254:80/latest/meta-data",  # metadata
        "http://8.8.8.8:3100/",  # public
        "https://10.42.0.9:3100/",  # another scheme
        "http://10.42.0.9/",  # no explicit port
        "http://u:p@10.42.0.9:3100/",
        "http://10.42.0.9:3100/a/..%2f..%2fb",
        "http://lab.internal:3100/",  # a name, not an IP literal
    ],
)
def test_only_a_private_ip_literal_with_a_port_names_a_lab(url):
    assert preview.match_origin(url) is None


def test_a_lab_url_names_its_ip_and_port():
    assert preview.match_origin("http://10.42.0.9:3100/assets/x.js") == preview.OriginTarget("10.42.0.9", 3100)


async def test_a_lab_preview_fails_closed_without_redis(monkeypatch):
    async def _down():
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.core.redis.redis_client", _down)
    monkeypatch.setattr(preview, "_allowed_cache", {})
    assert not await preview.allowed("http://10.42.0.9:3100/")


# -- the API client --------------------------------------------------------------


def _client(handler, tmp_path) -> kube.KubeClient:
    token = tmp_path / "token"
    token.write_text("sa-token\n")
    config = kube.KubeConfig(api_url="https://k8s.example:6443", namespace="agentic-labs", token_path=str(token), ca_path="")
    return kube.KubeClient(config, transport=httpx.MockTransport(handler))


async def test_the_client_calls_the_namespace_with_the_token(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers["authorization"]))
        if request.method == "GET" and request.url.path.endswith("/pods/gone"):
            return httpx.Response(404, json={"message": "pods \"gone\" not found"})
        if request.method == "POST":
            return httpx.Response(201, json={"metadata": {"name": "lab-1"}})
        return httpx.Response(200, json={"items": [{"metadata": {"name": "lab-1"}}]})

    client = _client(handler, tmp_path)
    assert (await client.create_pod({"metadata": {"name": "lab-1"}}))["metadata"]["name"] == "lab-1"
    assert await client.get_pod("gone") is None
    assert len(await client.list_pods("a=b")) == 1
    assert {path for _m, path, _a in seen} == {"/api/v1/namespaces/agentic-labs/pods", "/api/v1/namespaces/agentic-labs/pods/gone"}
    assert {auth for _m, _p, auth in seen} == {"Bearer sa-token"}


async def test_a_refusal_carries_the_status_and_never_the_token(tmp_path):
    def handler(_request):
        return httpx.Response(403, json={"message": "pods is forbidden"})

    with pytest.raises(kube.KubeError) as raised:
        await _client(handler, tmp_path).create_pod({})
    assert raised.value.status == 403 and "forbidden" in str(raised.value) and "sa-token" not in str(raised.value)


def test_the_exec_url_and_exit_codes(tmp_path):
    client = _client(lambda _r: httpx.Response(200), tmp_path)
    url = client.exec_url("lab-1", "workspace", ["sh", "-c", "echo hi"], stdin=True)
    assert url.startswith("wss://k8s.example:6443/api/v1/namespaces/agentic-labs/pods/lab-1/exec?")
    assert "command=sh&command=-c&command=echo+hi" in url and "stdin=true" in url
    assert kube.exit_code_of({"status": "Success"}) == 0
    assert kube.exit_code_of({"status": "Failure", "details": {"causes": [{"reason": "ExitCode", "message": "3"}]}}) == 3
    assert kube.exit_code_of({"status": "Failure"}) == 1


def test_no_api_server_is_said_plainly(monkeypatch):
    monkeypatch.setattr(settings, "agentic_labs_api_url", "")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    with pytest.raises(kube.KubeError, match="AGENTIC_LABS_API_URL"):
        kube.KubeConfig.from_settings()


# -- the tools ---------------------------------------------------------------------


def test_the_service_tools_are_offered_when_labs_are_on_even_without_opensandbox(monkeypatch, lab_settings):
    from app.services.builtin_tools.media import build_code_interpreter_tool

    names = {"sandbox_start_service", "sandbox_service_status", "sandbox_service_logs", "sandbox_stop_service"}
    monkeypatch.setattr(settings, "code_interpreter_workspace_enabled", True)
    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    monkeypatch.setattr(settings, "custom_tool_sandbox", "subprocess")
    definitions, _ = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    offered = {d.name for d in definitions}
    assert names <= offered and "sandbox_run_command" not in offered
    start = next(d for d in definitions if d.name == "sandbox_start_service")
    assert start.input_schema["properties"]["background"]["type"] == "boolean"
    assert start.input_schema["properties"]["restart"]["type"] == "string" and "restart=NAME" in start.description
    assert start.input_schema["required"] == ["checkout"] and "'starting'" in start.description
    monkeypatch.setattr(settings, "enabled_features", "")
    without, _ = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    assert not names & {d.name for d in without}
