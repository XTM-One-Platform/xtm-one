# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""``code_interpreter(session=true)``: one long-lived kernel per workspace.

The kernel is the code interpreter's own wrapper (the same environment scrub,
module blocklist and removed ``os`` functions) looping over cells in ONE
namespace (``code_interpreter._KERNEL_TRAILER``), so variables and imports
carry over from one call to the next. It is bound to the workspace - so to
the conversation or the run - and lives in this server process:

- one cell at a time per kernel (concurrent calls wait their turn);
- a cell past its timeout kills the kernel, which the next call reports;
- an idle kernel exits after ``CODE_INTERPRETER_KERNEL_IDLE_SECONDS``, by itself
  (its own timer) and here (reaped on the next call); one whose server process
  goes exits with its stdin;
- a process keeps at most ``CODE_INTERPRETER_KERNEL_MAX_PER_PROCESS``, the
  least recently used stopped first;
- a call that reaches another process (another replica, a restart) starts a
  new kernel and says it was restarted: the workspace row records the kernel
  last started for it (``kernel_state``) and counts the restarts.

The kernel's working directory is kept between calls: ``workspace/`` is synced
with the manifest before each cell and saved after it, as for a stateless
run; ``attachments/`` is rewritten when the call mounts files; a file the cell
writes elsewhere in it is that call's output, as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import socket
import sys
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.core.utils import format_exception_message
from app.services.sandbox_workspace import metrics
from app.services.sandbox_workspace.local import Snapshot, materialize, snapshot_after
from app.services.sandbox_workspace.paths import WORKSPACE_DIRNAME
from app.services.sandbox_workspace.run import SESSION, WorkspaceCall, commit_scan, scan_in_thread
from app.services.sandbox_workspace.session import WorkspaceSession, WorkspaceUnavailableError

logger = logging.getLogger(__name__)

KERNEL_SCRIPT = ".xtm_kernel.py"
_MARKER = "\x1eXTMK:"
_STDOUT_LIMIT = 64 * 1024 * 1024
_STRAY_LIMIT = 200_000


class KernelDiedError(RuntimeError):
    """The kernel process ended while a cell ran (a crash, out of memory)."""


#: The keys of ``kernel_state`` other modules keep there (labs, service containers, command containers): a kernel
#: start replaces the kernel's own keys and never these.
_OTHER_RECORDS = ("labs", "services", "commands", "install_fallbacks", "jobs", "reruns", "lost_runs")


async def _record_kernel(workspace: WorkspaceSession, kernel: dict, *, restarted: bool) -> None:
    """Record the kernel started for *workspace*, keeping the other records ``kernel_state`` holds.

    The workspace record's ``kernel_state`` also names the running labs, service containers and command containers;
    writing the kernel's state whole lost them at the first ``session=true`` call of a run resumed in another process,
    and the run found its lab gone.
    """
    def change(current: dict) -> dict:
        kept = {name: current[name] for name in _OTHER_RECORDS if current.get(name)}
        return {**kernel, **kept}

    await workspace.update_kernel_state(change, restarted=restarted)


def _settings() -> tuple[float, int]:
    from app.core.config import settings

    return (
        float(max(30, int(settings.code_interpreter_kernel_idle_seconds))),
        max(1, int(settings.code_interpreter_kernel_max_per_process)),
    )


@dataclass
class LocalKernel:
    key: str
    home: str
    process: Any
    token: str
    kernel_id: str
    started_at: float
    last_used: float
    snapshot: Snapshot | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=50))
    _seq: int = 0
    _stderr_task: asyncio.Task | None = None

    @property
    def alive(self) -> bool:
        return self.process.returncode is None

    @property
    def workspace_dir(self) -> str:
        return os.path.join(self.home, WORKSPACE_DIRNAME)

    def state(self) -> dict:
        return {
            "backend": "local",
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "kernel_id": self.kernel_id,
            "started_at": datetime.fromtimestamp(self.started_at, tz=UTC).isoformat(),
        }

    async def run(self, code: str, timeout: float) -> dict:
        """One cell; :class:`TimeoutError` past *timeout*, :class:`KernelDiedError` if it ended."""
        self._seq += 1
        request = json.dumps({"id": self._seq, "code": code}) + "\n"
        try:
            self.process.stdin.write(request.encode("utf-8"))
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise KernelDiedError("the kernel is not running") from exc
        stray: list[str] = []
        stray_size = 0
        marker = f"{_MARKER}{self.token}:"

        async def _read() -> dict:
            nonlocal stray_size
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    raise KernelDiedError("the kernel exited")
                text = line.decode("utf-8", errors="replace")
                if text.startswith(marker):
                    return json.loads(text[len(marker) :])
                if stray_size < _STRAY_LIMIT:
                    stray.append(text)
                    stray_size += len(text)

        result = await asyncio.wait_for(_read(), timeout)
        if stray:
            result["stdout"] = "".join(stray) + (result.get("stdout") or "")
        return result

    async def kill(self) -> None:
        if self.alive:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.process.wait(), 5)
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        shutil.rmtree(self.home, ignore_errors=True)


async def _drain_stderr(kernel: LocalKernel) -> None:
    with contextlib.suppress(Exception):
        while True:
            line = await kernel.process.stderr.readline()
            if not line:
                return
            kernel.stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())


async def start_local_kernel(key: str) -> LocalKernel:
    """A new kernel process in a directory of its own."""
    from app.services.code_interpreter import _build_sandbox_env, _build_sandbox_wrapper, _resolve_allowed_modules

    idle, _ = _settings()
    home = tempfile.mkdtemp(prefix="xtm_kernel_")
    script = os.path.join(home, KERNEL_SCRIPT)
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(_build_sandbox_wrapper(_resolve_allowed_modules(), kernel=True))
    os.makedirs(os.path.join(home, WORKSPACE_DIRNAME), exist_ok=True)
    env = _build_sandbox_env()
    env["WORKSPACE_DIR"] = os.path.abspath(os.path.join(home, WORKSPACE_DIRNAME))
    token = secrets.token_hex(16)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        script,
        token,
        str(idle),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=home,
        env=env,
        limit=_STDOUT_LIMIT,
    )
    now = time.time()
    kernel = LocalKernel(
        key=key,
        home=home,
        process=process,
        token=token,
        kernel_id=uuid.uuid4().hex,
        started_at=now,
        last_used=now,
    )
    kernel._stderr_task = asyncio.create_task(_drain_stderr(kernel), name=f"kernel-stderr:{key}")
    return kernel


class KernelPool:
    """This process's kernels, by workspace id."""

    def __init__(self, starter=start_local_kernel) -> None:
        self._kernels: dict[str, LocalKernel] = {}
        self._lock = asyncio.Lock()
        self._starter = starter

    def __len__(self) -> int:
        return len(self._kernels)

    def get(self, key: str) -> LocalKernel | None:
        return self._kernels.get(key)

    async def acquire(self, key: str, *, reset: bool) -> tuple[LocalKernel, str | None]:
        """The workspace's kernel and why it is the one: ``reused``, ``reset``, ``restarted`` or ``None`` (new here)."""
        idle, cap = _settings()
        async with self._lock:
            now = time.time()
            for other_key, other in list(self._kernels.items()):
                if other.lock.locked():
                    continue
                if other_key != key and (not other.alive or now - other.last_used > idle):
                    self._kernels.pop(other_key, None)
                    await other.kill()
            kernel = self._kernels.get(key)
            status: str | None = None
            if kernel is not None:
                if reset:
                    status = "reset"
                elif kernel.alive and now - kernel.last_used <= idle:
                    kernel.last_used = now
                    return kernel, "reused"
                else:
                    status = "restarted"
                self._kernels.pop(key, None)
                await kernel.kill()
            elif reset:
                status = "reset"
            while len(self._kernels) >= cap:
                idle_kernels = [k for k in self._kernels.values() if not k.lock.locked()]
                if not idle_kernels:
                    break
                oldest = min(idle_kernels, key=lambda k: k.last_used)
                self._kernels.pop(oldest.key, None)
                await oldest.kill()
            kernel = await self._starter(key)
            self._kernels[key] = kernel
            return kernel, status

    async def drop(self, key: str, kernel: LocalKernel | None = None) -> None:
        """Stop the workspace's kernel - only *kernel* when given, never one started since."""
        async with self._lock:
            current = self._kernels.get(key)
            if current is not None and (kernel is None or current is kernel):
                self._kernels.pop(key, None)
        target = kernel or current
        if target is not None:
            await target.kill()

    async def shutdown(self) -> None:
        async with self._lock:
            kernels = list(self._kernels.values())
            self._kernels.clear()
        for kernel in kernels:
            await kernel.kill()


_pool: KernelPool | None = None


def kernel_pool() -> KernelPool:
    global _pool
    if _pool is None:
        _pool = KernelPool()
    return _pool


def _file_states(home: str, skip: set[str]) -> dict[str, tuple[int, int]]:
    states: dict[str, tuple[int, int]] = {}
    for root, dirs, names in os.walk(home):
        dirs[:] = [d for d in dirs if os.path.join(root, d) not in skip]
        for name in names:
            full = os.path.join(root, name)
            with contextlib.suppress(OSError):
                st = os.stat(full)
                states[full] = (st.st_size, st.st_mtime_ns)
    return states


async def _stateless_fallback(code: str, *, timeout: float, workspace, input_files, note: str) -> dict:
    from app.services.code_interpreter import execute_python

    result = await execute_python(code, timeout=int(timeout), input_files=input_files, workspace=workspace)
    call = result.get("workspace")
    if not isinstance(call, WorkspaceCall):
        call = WorkspaceCall()
    call.notes.append(note)
    result["workspace"] = call
    return result


async def execute_in_local_kernel(
    code: str,
    *,
    timeout: float,
    workspace: WorkspaceSession | None,
    input_files=None,
    reset: bool = False,
    pool: KernelPool | None = None,
) -> dict:
    """Run *code* in the workspace's kernel; the result has ``execute_python``'s shape."""
    from app.services.code_interpreter import (
        ATTACHMENTS_DIRNAME,
        _collect_output_files,
        _load_inputs_safely,
        _write_input_files,
    )

    if workspace is None or not workspace.available:
        reason = workspace.unavailable_reason if workspace is not None else "no conversation or run to bind it to"
        return await _stateless_fallback(
            code,
            timeout=timeout,
            workspace=workspace,
            input_files=input_files,
            note=f"session=true needs a sandbox workspace ({reason}): this call ran in a fresh process.",
        )
    try:
        record = await workspace.record(create=True)
    except WorkspaceUnavailableError as exc:
        return await _stateless_fallback(
            code,
            timeout=timeout,
            workspace=workspace,
            input_files=input_files,
            note=f"session=true needs a sandbox workspace ({exc}): this call ran in a fresh process.",
        )
    pool = pool or kernel_pool()
    key = str(record.id)
    kernel, status = await pool.acquire(key, reset=reset)
    if status is None:
        status = "restarted" if (record.kernel_state or {}).get("kernel_id") else "new"
    if status != "reused":
        metrics.kernel_started(status)
        await _record_kernel(workspace, kernel.state(), restarted=status == "restarted")
    call = WorkspaceCall(mode=SESSION, kernel=status)
    async with kernel.lock:
        kernel.last_used = time.time()
        attach_dir = os.path.join(kernel.home, ATTACHMENTS_DIRNAME)
        mounted_paths: set[str] = set()
        mounted_names: list[str] = []
        inputs = await _load_inputs_safely(input_files)
        if inputs:
            shutil.rmtree(attach_dir, ignore_errors=True)
            mounted_paths, mounted_names = _write_input_files(kernel.home, inputs)
        snapshot: Snapshot | None = None
        try:
            snapshot = await materialize(workspace, kernel.workspace_dir, existing=kernel.snapshot)
            call.mounted = True
        except Exception as exc:
            logger.warning("Could not sync the kernel workspace of %s: %s", workspace.scope.label, format_exception_message(exc))
            call.unavailable = f"its files could not be read just now ({format_exception_message(exc)})"
        skip = {kernel.workspace_dir, attach_dir}
        before = await asyncio.to_thread(_file_states, kernel.home, skip)
        lost = False
        try:
            result = await kernel.run(code, timeout)
        except TimeoutError:
            # Stopped now, removed once what it wrote is saved.
            lost = True
            with contextlib.suppress(ProcessLookupError):
                kernel.process.kill()
            logger.warning("Code interpreter kernel timed out after %ss", timeout)
            result = {
                "stdout": "",
                "stderr": "",
                "error": (
                    f"Execution timed out after {int(timeout)} seconds. The kernel was stopped: the next "
                    "session=true call starts a new one (files under workspace/ are kept, variables are not)."
                ),
                "images": [],
            }
            call.kernel_note = "The kernel was stopped by the timeout."
        except KernelDiedError:
            lost = True
            tail = "\n".join(list(kernel.stderr_tail)[-10:])
            result = {
                "stdout": "",
                "stderr": tail,
                "error": (
                    "The kernel stopped while running the cell (it crashed or ran out of memory). The next "
                    "session=true call starts a new one (files under workspace/ are kept, variables are not)."
                ),
                "images": [],
            }
            call.kernel_note = "The kernel stopped during the cell."
        result["output_files"] = await asyncio.to_thread(
            _collect_output_files,
            kernel.home,
            {os.path.join(kernel.home, KERNEL_SCRIPT), *mounted_paths},
            exclude_dirs=sorted(skip),
            since=before,
        )
        result["mounted"] = mounted_names
        if snapshot is not None:
            try:
                scanned = await scan_in_thread(kernel.workspace_dir, snapshot, max_files=workspace.limits.max_files)
                await commit_scan(
                    workspace, scanned, call, bytes_read=snapshot.bytes_read, count_source_writes=True
                )
                if call.refused:
                    # The directory holds what the manifest refused: start the
                    # next call from the manifest, as a fresh process would.
                    shutil.rmtree(kernel.workspace_dir, ignore_errors=True)
                    kernel.snapshot = None
                else:
                    kernel.snapshot = snapshot_after(snapshot, scanned, kernel.workspace_dir)
            except Exception as exc:
                logger.warning("Could not save the kernel workspace of %s: %s", workspace.scope.label, format_exception_message(exc))
                call.refused = f"Nothing it wrote under workspace/ was saved: {format_exception_message(exc)}."
                kernel.snapshot = None
        kernel.last_used = time.time()
    if lost:
        await pool.drop(key, kernel)
    metrics.call(SESSION, "unavailable" if call.unavailable else "mounted")
    result["workspace"] = call
    return result


# -- In an OpenSandbox container ------------------------------------------------
#
# The kernel lives in a container of its own, kept for the workspace (its
# ``kernel_state`` names it) and reached by id from any server process: a
# call landing on another replica finds the same kernel. The container's TTL
# is the idle timeout, renewed on every call. Cells and results travel as files
# of ``REMOTE_KERNEL_DIR`` (``code_interpreter._KERNEL_TRAILER``'s directory
# mode); the workspace is reconciled with the manifest before each cell and
# read back after it, as for a stateless container.

REMOTE_KERNEL_DIR = "/tmp/.xtm/k"  # noqa: S108 - the container's own directory
REMOTE_KERNEL_SCRIPT = "/tmp/.xtm/kernel.py"  # noqa: S108
_POLL_START = 0.1
_POLL_MAX = 1.0


async def _remote_kernel_alive(sandbox: Any) -> bool:
    from opensandbox.models.execd import RunCommandOpts

    probe = (
        "python -c \"import os,sys; p='" + REMOTE_KERNEL_DIR + "/pid'; "
        "sys.exit(0 if os.path.exists(p) and os.kill(int(open(p).read()), 0) is None else 1)\""
    )
    try:
        result = await sandbox.commands.run(probe, opts=RunCommandOpts(working_directory="/tmp"))  # noqa: S108
    except Exception:
        return False
    return getattr(result, "exit_code", 1) == 0


async def _start_remote_kernel(sandbox: Any, *, idle: float) -> None:
    from opensandbox.models.execd import RunCommandOpts
    from opensandbox.models.filesystem import WriteEntry

    from app.services.code_interpreter import _build_sandbox_wrapper, _opensandbox_allowed_modules

    await sandbox.files.create_directories([WriteEntry(path=REMOTE_KERNEL_DIR, mode=700)])
    await sandbox.files.write_file(
        REMOTE_KERNEL_SCRIPT, _build_sandbox_wrapper(_opensandbox_allowed_modules(), kernel=True).encode("utf-8")
    )
    await sandbox.commands.run(
        f"python {REMOTE_KERNEL_SCRIPT} {secrets.token_hex(16)} {int(idle)} {REMOTE_KERNEL_DIR}",
        opts=RunCommandOpts(background=True, working_directory="/tmp"),  # noqa: S108
    )


async def _remote_result(sandbox: Any, request_id: str, timeout: float) -> dict | None:
    """The cell's result, polled with a backoff; ``None`` past *timeout*."""
    deadline = time.monotonic() + timeout
    delay = _POLL_START
    path = f"{REMOTE_KERNEL_DIR}/{request_id}.res"
    while time.monotonic() < deadline:
        try:
            return json.loads(await sandbox.files.read_bytes(path))
        except Exception:
            await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, _POLL_MAX)
    return None


async def execute_in_remote_kernel(
    code: str,
    *,
    timeout: float,
    workspace: WorkspaceSession | None,
    input_files=None,
    reset: bool = False,
    user_id: uuid.UUID | None = None,
    quota_user_id: uuid.UUID | None = None,
    db: Any = None,
    sandbox_api: Any = None,
) -> dict:
    """Run *code* in the workspace's kernel container; the result has ``execute_python``'s shape.

    *sandbox_api* stands in for the ``opensandbox`` SDK's ``Sandbox`` class
    and the container factory (tests).
    """
    from datetime import timedelta

    from app.core.config import settings
    from app.services.code_interpreter import (
        ATTACHMENTS_DIRNAME,
        MAX_IMAGES,
        MAX_OUTPUT_FILE_BYTES,
        MAX_OUTPUT_FILES,
        _load_inputs_safely,
        execute_python_opensandbox,
    )
    from app.services.sandbox_workspace.paths import WORKSPACE_ENV
    from app.services.sandbox_workspace.remote import REMOTE_WORKSPACE, collect, install_tool, reconcile

    def _fallback_note(reason: str) -> str:
        return f"session=true needs a sandbox workspace ({reason}): this call ran in a fresh container."

    if workspace is None or not workspace.available:
        result = await execute_python_opensandbox(
            code, timeout=int(timeout), user_id=user_id, quota_user_id=quota_user_id, db=db, input_files=input_files, workspace=workspace
        )
        call = result.get("workspace") if isinstance(result.get("workspace"), WorkspaceCall) else WorkspaceCall()
        call.notes.append(_fallback_note(workspace.unavailable_reason if workspace is not None else "no conversation or run"))
        result["workspace"] = call
        return result

    from app.services.quota import (
        QUOTA_EXCEEDED_MESSAGE,
        QUOTA_UNAVAILABLE_MESSAGE,
        QuotaCheckUnavailableError,
        QuotaExceededError,
        enforce_combined_sandbox_quota,
        increment_sandbox_usage,
    )

    def _refused(error: str) -> dict:
        call = WorkspaceCall(mode=SESSION, notes=["The call was refused before it ran."])
        return {"stdout": "", "stderr": "", "error": error, "images": [], "output_files": [], "mounted": [], "workspace": call}

    effective_quota_user = quota_user_id if quota_user_id is not None else user_id
    try:
        await enforce_combined_sandbox_quota(effective_quota_user)
    except QuotaExceededError:
        return _refused(QUOTA_EXCEEDED_MESSAGE)
    except QuotaCheckUnavailableError:
        return _refused(QUOTA_UNAVAILABLE_MESSAGE)

    if sandbox_api is None:
        from opensandbox import Sandbox

        from app.services._sandbox_selector import _build_opensandbox_config, _create_opensandbox

        async def _connect(sandbox_id: str):
            return await Sandbox.connect(sandbox_id, connection_config=_build_opensandbox_config())

        async def _create(idle_seconds: float):
            return await _create_opensandbox(
                image_key="python",
                timeout_seconds=int(idle_seconds),
                kind_label="code-interpreter-kernel",
                usage_type="code_interpreter",
                user_id=user_id,
                egress_allow_list=settings.opensandbox_network_allow_list,
                open_egress=settings.code_interpreter_open_egress,
                resource=settings.sandbox_resources("python"),
                env={"MPLBACKEND": "Agg", "COPILOT_MAX_IMAGES": str(MAX_IMAGES), WORKSPACE_ENV: REMOTE_WORKSPACE},
            )

        sandbox_api = SimpleNamespace(connect=_connect, create=_create)

    idle, _ = _settings()
    # The container outlives the longest cell, whatever the idle timeout says.
    lifetime = max(idle, float(timeout) + 60)
    record = await workspace.record(create=True)
    state = record.kernel_state if (record.kernel_state or {}).get("backend") == "opensandbox" else None
    sandbox = None
    status: str | None = None
    if state:
        try:
            sandbox = await sandbox_api.connect(state["sandbox_id"])
        except Exception:
            sandbox = None
        if sandbox is not None and (reset or not await _remote_kernel_alive(sandbox)):
            with contextlib.suppress(Exception):
                await sandbox.kill()
            sandbox = None
            status = "reset" if reset else "restarted"
        elif sandbox is not None:
            status = "reused"
        else:
            status = "reset" if reset else "restarted"
    elif reset:
        status = "reset"
    if sandbox is None:
        status = status or "new"
        try:
            sandbox = await sandbox_api.create(lifetime)
            await _start_remote_kernel(sandbox, idle=idle)
        except Exception as exc:
            return _refused(f"Failed to create the kernel sandbox: {format_exception_message(exc)}")
        await _record_kernel(
            workspace,
            {
                "backend": "opensandbox",
                "sandbox_id": str(sandbox.id),
                "kernel_id": uuid.uuid4().hex,
                "started_at": datetime.now(UTC).isoformat(),
                "host": socket.gethostname(),
            },
            restarted=status == "restarted",
        )
        metrics.kernel_started(status)
    else:
        with contextlib.suppress(Exception):
            await sandbox.renew(timedelta(seconds=lifetime))

    call = WorkspaceCall(mode=SESSION, kernel=status)
    lost = False
    mounted_names: list[str] = []
    try:
        inputs = await _load_inputs_safely(input_files)
        if inputs:
            from opensandbox.models.filesystem import WriteEntry

            attach = f"/tmp/{ATTACHMENTS_DIRNAME}"  # noqa: S108
            with contextlib.suppress(Exception):
                await sandbox.files.create_directories([WriteEntry(path=attach, mode=755)])
            for name, data in inputs:
                if name and "/" not in name and name not in {".", ".."}:
                    with contextlib.suppress(Exception):
                        await sandbox.files.write_file(f"{attach}/{name}", data)
                        mounted_names.append(name)
        ws_state = None
        try:
            await install_tool(sandbox)
            ws_state = await reconcile(sandbox, workspace, await workspace.entries(refresh=True))
            call.mounted = True
        except Exception as exc:
            call.unavailable = f"its files could not be put in the sandbox ({format_exception_message(exc)})"
        request_id = uuid.uuid4().hex
        await sandbox.files.write_file(f"{REMOTE_KERNEL_DIR}/{request_id}.json", json.dumps({"id": request_id, "code": code}).encode("utf-8"))
        await sandbox.files.write_file(f"{REMOTE_KERNEL_DIR}/{request_id}.ready", b"")
        result = await _remote_result(sandbox, request_id, timeout)
        if result is None:
            lost = True
            call.kernel_note = "The kernel was stopped by the timeout."
            result = {
                "stdout": "",
                "stderr": "",
                "error": (
                    f"Execution timed out after {int(timeout)} seconds. The kernel was stopped: the next "
                    "session=true call starts a new one (files under workspace/ are kept, variables are not)."
                ),
                "images": [],
                "changed": [],
            }
        output_files: list[dict] = []
        import base64 as _b64
        import mimetypes

        for rel in list(result.pop("changed", None) or [])[:MAX_OUTPUT_FILES]:
            try:
                data = await sandbox.files.read_bytes(f"/tmp/{rel}")  # noqa: S108
            except Exception as exc:
                logger.debug("Kernel output %s could not be read back: %s", rel, format_exception_message(exc))
                continue
            if 0 < len(data) <= MAX_OUTPUT_FILE_BYTES:
                output_files.append(
                    {
                        "filename": rel,
                        "data_base64": _b64.b64encode(data).decode("ascii"),
                        "content_type": mimetypes.guess_type(rel)[0],
                        "size": len(data),
                    }
                )
        result["output_files"] = output_files
        result["mounted"] = mounted_names
        if ws_state is not None and not lost:
            from app.services.sandbox_workspace.manifest import QuotaExceededError as WorkspaceQuotaError

            try:
                scanned = await collect(sandbox, workspace, ws_state)
            except WorkspaceQuotaError as exc:
                metrics.quota_refused("code_interpreter")
                call.refused = str(exc)
            else:
                await commit_scan(
                    workspace, scanned, call, bytes_read=ws_state.bytes_read, count_source_writes=True
                )
        await increment_sandbox_usage(effective_quota_user, db=db)
    finally:
        if lost:
            with contextlib.suppress(Exception):
                await sandbox.kill()
        with contextlib.suppress(Exception):
            await sandbox.close()
    metrics.call(SESSION, "unavailable" if call.unavailable else "mounted")
    result["workspace"] = call
    return result
