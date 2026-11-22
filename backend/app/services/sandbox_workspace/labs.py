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

"""Agentic coding labs (``AGENTIC_CODING``): one Kubernetes pod per lab, created and reaped by the platform.

A lab runs what a checkout declares as a service (``[service.<name>]`` in
``.xtm-one/checks.toml``, :func:`services.resolve`) in ONE pod of the namespace
``AGENTIC_LABS_NAMESPACE``: a ``workspace`` container that holds the checkout
(an ``emptyDir`` the workspace files are unpacked into through the pod's exec
API) and one container per service of the chain, on the pod's shared
``localhost``. Each service container waits for the checkout, then for the
ports of the services it depends on, then runs its argv in its ``cwd``; its
readiness is the pod's own readiness probe on its port (``ready_path`` when
declared). The service's preview is ``http://<pod IP>:<port>/``, admitted to
the browser tool and web_fetch through :func:`preview.register_origin` for the
lab's life, with nothing added to its requests.

The four sandbox service tools reach a lab when the flag is on and the
namespace is set (:func:`labs_enabled`); logs come from the pod log API, a
command from the pod exec API (:func:`lab_exec`), both through :mod:`kube`.

No ceiling of any kind (Sam's decision D1b, 31 Oct 2026): a lab is never
refused for a quota, counted against one, or limited in number. The pod has
resource REQUESTS so the scheduler can place it, and no limits. The only
end of a lab other than ``sandbox_stop_service`` is hygiene: a pod unused for
``AGENTIC_LABS_IDLE_HOURS`` is deleted by the hourly sweep, and Kubernetes ends
one older than ``AGENTIC_LABS_DEADLINE_HOURS`` (``activeDeadlineSeconds``);
``0`` turns either off. See ``dev-docs/reference/agentic-labs.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hashlib
import io
import logging
import re
import shlex
import tarfile
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.core.utils import format_exception_message
from app.services.sandbox_workspace import licences, model_keys, preview
from app.services.sandbox_workspace.commands import IGNORED_PATTERNS
from app.services.sandbox_workspace.kube import ExecResult, KubeClient, KubeError
from app.services.sandbox_workspace.paths import is_under, normalize
from app.services.sandbox_workspace.remote import first_leaf
from app.services.sandbox_workspace.services import (
    DEFAULT_LOG_LINES,
    MAX_LOG_CHARS,
    MAX_LOG_LINES,
    MAX_LOG_SCAN_LINES,
    SERVICE_ENV,
    ResolvedService,
    ServiceOutcome,
    compile_log_pattern,
    log_section,
    not_running,
    start_window_left,
)
from app.services.sandbox_workspace.session import WorkspaceSession

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[object]]
Clock = Callable[[], float]

FLAG = "AGENTIC_CODING"
#: Where the checkout is unpacked in every container of the pod (the services' own layout).
LAB_WORKSPACE = "/tmp/workspace"  # noqa: S108 - the pod's own volume mount
READY_MARKER = f"{LAB_WORKSPACE}/.xtm/lab-ready"
WORKSPACE_CONTAINER = "workspace"
#: Labels and annotations every lab pod carries.
MANAGED_BY = "xtm-one-agentic-labs"
LABEL_MANAGED = "app.kubernetes.io/managed-by"
LABEL_WORKSPACE = "xtm.filigran.io/workspace"
ANNOTATION_LAST_USED = "xtm.filigran.io/last-used"
ANNOTATION_SERVICE = "xtm.filigran.io/service"
#: The id of the lab's live-model key (``model_keys``), revoked when the pod goes; never the key.
ANNOTATION_MODEL_KEY = "xtm.filigran.io/model-key"
#: Seconds between two reads of the pod while it starts, at first and at most.
_POLL_START = 1.0
_POLL_MAX = 5.0
#: Lines and bytes of a container's log read while its ``ready_log`` text is awaited.
_READY_LOG_LINES = 2000
_READY_LOG_BYTES = 1024 * 1024
#: Lines of its own output a start answers with when a service's container ended before it was ready.
_EXIT_TAIL_LINES = 20
#: Blobs read at once while the checkout's archive is built: a repository checkout holds
#: thousands of small files, one object-store round trip each.
_ARCHIVE_READ_CONCURRENCY = 32
#: Attempts of the checkout copy into a lab: a stalled, refused or lost exec stream is retried once.
_COPY_ATTEMPTS = 2
#: Seconds before the second copy when the API server refused the first exec stream (a 404 on a pod it
#: has not caught up with yet, around a worker loss).
_COPY_REFUSED_PAUSE_SECONDS = 3.0
#: Tries of one blob read while the checkout is archived for a lab.
_BLOB_READ_ATTEMPTS = 2
#: Delete calls a stop makes before it reads the pod (a timeout under load is transient).
_STOP_ATTEMPTS = 2
_NAME_RE = re.compile(r"[^a-z0-9-]+")
#: Seconds a start waits for its lab's readiness (after the pod runs and holds the checkout) before it answers
#: ``starting`` with the service_id: with the scheduling and the copy, below the per-tool timeout of the agentic loop
#: (300 s), so a lab whose declared readiness outlives a tool call is never lost with its id. The lab goes on
#: starting; sandbox_service_status, sandbox_service_logs or a second start follows it. 240 s (was 180 s, #12104):
#: a lab ready a minute past 180 s took a second call; the session read and the answer fit in the 60 s left.
ANSWER_SECONDS = 240.0
#: Files of a service container's own supervisor (``_supervised``): the pid of the process it runs, and the flag a
#: restart sets before it ends that process so the supervisor runs it again instead of ending the container.
_CHILD_PID = "/tmp/.xtm-service.pid"  # noqa: S108 - the container's own file system
_RESTART_FLAG = "/tmp/.xtm-service.restart"  # noqa: S108 - the container's own file system
#: Seconds a restart gives the old process group to end on SIGTERM before its one SIGKILL, then to be gone after it,
#: and to stop answering its probe.
_RESTART_TERM_SECONDS = 30
_RESTART_KILL_SECONDS = 10
#: Shell function ``g``: whether a live (not zombie) process is in the process group ``$c``, or is ``$c`` itself (a
#: process run without ``setsid``). Read from ``/proc``: ``kill -0`` also answers for a zombie, and an orphan the
#: shell at pid 1 has not reaped yet would make a group killed outright look alive (#12198). ``stat`` after the last
#: ``) ``: state, ppid, pgrp.
_GROUP_ALIVE = (
    'g() { for s in /proc/[0-9]*/stat; do l=$(cat "$s" 2>/dev/null) || continue; l=${l##*) }; set -- $l; '
    '[ "$1" != Z ] && { [ "$3" = "$c" ] || [ "$s" = "/proc/$c/stat" ]; } && return 0; done; return 1; }'
)
_RESTART_DOWN_SECONDS = 20.0
#: The line the supervisor writes to the container's output before each rerun: a restart waits for the service's
#: ``ready_log`` after the last one, so the old run's line never counts the new run ready.
_RESTART_MARK = "[xtm-service] restarted in place"
#: How a readiness wait says it ran out of time (``_wait``, ``_wait_logged``), as opposed to a service that ended.
_NOT_READY_WITHIN = "not ready within "
#: Seconds a check run in a lab (``sandbox_run_command`` with ``service_id``) may take: below the per-tool timeout
#: of the agentic loop (300 s), so its answer is never lost; a longer check is narrowed with its args.
LAB_COMMAND_SECONDS = 270.0
#: Characters of a lab check's stdout and stderr in its answer (the end of each is kept).
LAB_COMMAND_STDOUT_CHARS = 20_000
LAB_COMMAND_STDERR_CHARS = 4_000
_LOGGED_IN_TIME = " in time"
#: Signed-in session lines a lab answer repeats at most, and the characters of one.
_MAX_SESSIONS = 12
_MAX_SESSION_CHARS = 4096
#: Hours of licence validity a lab must have left to be adopted: one that would lapse within it is replaced, so a
#: run reusing a lab never meets the Community Edition splash halfway through a review.
LICENCE_MARGIN_HOURS = 1.0


def labs_enabled() -> bool:
    """Whether the service tools start labs: ``AGENTIC_CODING`` on (copilot or dev mode only) and a namespace set."""
    from app.core.config import settings
    from app.core.feature_flags import is_feature_enabled

    return is_feature_enabled(FLAG) and bool((settings.agentic_labs_namespace or "").strip())


def _now() -> str:
    return datetime.now(UTC).isoformat()


def container_name(service: str) -> str:
    """The pod container of service *service*: a DNS-1123 label."""
    cleaned = _NAME_RE.sub("-", service.lower()).strip("-") or "service"
    return f"svc-{cleaned}"[:63].rstrip("-")


def _join(prefix: str, rel: str) -> str:
    return normalize(f"{prefix}/{rel}" if prefix and rel else (prefix or rel), allow_root=True)


def _probe(port: int) -> str:
    code = f"import socket; socket.create_connection(('127.0.0.1', {port}), 2).close()"
    return f"python -c {shlex.quote(code)}"


def service_script(resolved: ResolvedService, name: str) -> str:
    """The shell line a service container runs: wait for the checkout and its dependencies, then its argv."""
    specs = {spec.name: spec for spec in resolved.chain}
    spec = specs[name]
    where = _join(resolved.checkout, spec.cwd)
    cwd = f"{LAB_WORKSPACE}/{where}" if where else LAB_WORKSPACE
    steps = [f"until [ -f {READY_MARKER} ]; do sleep 1; done"]
    if not _own_image(resolved, spec):
        # A product image (AGENTIC_LABS_IMAGES) may hold no Python for the probe: it waits for its own
        # dependencies, as such products do when they start next to their search engine or broker.
        steps += [f"until {_probe(specs[dep].port)}; do sleep 1; done" for dep in spec.depends_on if dep in specs]
    steps += [f"cd {shlex.quote(cwd)}", _supervised(spec.argv)]
    return " && ".join(steps)


def _supervised(argv: tuple[str, ...] | list[str]) -> str:
    """The shell block that runs *argv* and runs it again when a restart asked for it (#12104).

    The process gets a process group of its own (``setsid`` when the image has it), so a restart ends what it
    started too (a build, a JVM). It exits as the process did unless :data:`_RESTART_FLAG` is set: a lab pod runs with
    ``restartPolicy: Never``, so a service that ends still ends its container. SIGTERM (the pod's deletion) reaches
    the process group. A restart keeps the container, so what it wrote (caches, build output) is kept.
    """
    line = shlex.join(argv)
    # Before a rerun the whole old group must be gone (#12198): a lab script ends at once on SIGTERM while the
    # PostgreSQL, JVM or Vite it started still hold their ports and data directories for a few seconds.
    gone = _RESTART_TERM_SECONDS + _RESTART_KILL_SECONDS
    return (
        f"{{ {_GROUP_ALIVE}; trap 'kill -TERM -$c 2>/dev/null || kill -TERM $c 2>/dev/null; exit 143' TERM INT; "
        "while :; do "
        f"rm -f {_RESTART_FLAG}; if command -v setsid >/dev/null 2>&1; then setsid {line} & else {line} & fi; "
        f"c=$!; echo $c > {_CHILD_PID}; wait $c; s=$?; [ -f {_RESTART_FLAG} ] || exit $s; "
        f"i=0; while g && [ $i -lt {gone} ]; do sleep 1; i=$((i+1)); done; echo {shlex.quote(_RESTART_MARK)}; done; }}"
    )


def restart_script() -> str:
    """The shell line run in a service container to restart its process group: flag, SIGTERM, one SIGKILL, bounded.

    The wait is on the whole process group, not its leader alone (#12198): every process the service started (a
    database, a JVM, a dev server) has ended before the supervisor runs the service again, so its ports and data
    directories are free. SIGTERM, up to ``_RESTART_TERM_SECONDS`` for the group to end, then SIGKILL once and up
    to ``_RESTART_KILL_SECONDS`` more: never a loop.

    Exit 3: no supervisor in the container (a pod created before #12104). Exit 4: no valid pid recorded. Exit 5: the
    group outlived its SIGKILL.
    """
    alive = "g"
    return (
        f"[ -f {_CHILD_PID} ] || exit 3; c=$(cat {_CHILD_PID}); {_GROUP_ALIVE}; "
        'case "$c" in ""|*[!0-9]*|0|1) exit 4;; esac; '
        f"touch {_RESTART_FLAG}; kill -TERM -$c 2>/dev/null || kill -TERM $c 2>/dev/null; i=0; "
        f"while {alive} && [ $i -lt {_RESTART_TERM_SECONDS} ]; do sleep 1; i=$((i+1)); done; "
        f"if {alive}; then kill -KILL -$c 2>/dev/null || kill -KILL $c 2>/dev/null; i=0; "
        f"while {alive} && [ $i -lt {_RESTART_KILL_SECONDS} ]; do sleep 1; i=$((i+1)); done; "
        f"if {alive}; then exit 5; fi; fi; exit 0"
    )


def _own_image(resolved: ResolvedService, spec: Any) -> str:
    """The image of *spec*'s own container when it is a dependency naming an ``AGENTIC_LABS_IMAGES`` key, else ``""``."""
    from app.core.config import settings

    if spec is resolved.spec or not spec.image:
        return ""
    return str(settings.agentic_labs_images.get(spec.image, "") or "")


def _probe_spec(port: int, ready_path: str) -> dict[str, Any]:
    check: dict[str, Any] = {"httpGet": {"path": ready_path, "port": port}} if ready_path else {"tcpSocket": {"port": port}}
    # Generous on purpose: a probe that fails only keeps the service "not ready", it never restarts it.
    return {**check, "periodSeconds": 2, "timeoutSeconds": 3, "failureThreshold": 3}


def pod_manifest(
    resolved: ResolvedService,
    *,
    name: str,
    image: str,
    workspace_id: str,
    licence_env: dict[str, str] | None = None,
    model_key_id: str = "",
) -> dict[str, Any]:
    """The pod of a lab: the workspace container and one container per service, requests and no limits.

    *licence_env* (variable to PEM, :func:`licences.issue`) reaches every container, under a
    service's own ``env`` so a declaration can still override it.
    """
    from app.core.config import settings

    licence_env = dict(licence_env or {})
    requests = {"cpu": settings.agentic_labs_cpu_request, "memory": settings.agentic_labs_memory_request}
    hardened = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    mount = [{"name": "workspace", "mountPath": LAB_WORKSPACE}]
    containers: list[dict[str, Any]] = [
        {
            "name": WORKSPACE_CONTAINER,
            "image": image,
            "command": ["sh", "-c", f"mkdir -p {LAB_WORKSPACE}/.xtm && while :; do sleep 3600; done"],
            "env": [{"name": key, "value": value} for key, value in {**SERVICE_ENV, **licence_env}.items()],
            "volumeMounts": mount,
            "resources": {"requests": requests},
            "securityContext": hardened,
        }
    ]
    for spec in resolved.chain:
        env = {**SERVICE_ENV, **licence_env, **dict(spec.env)}
        containers.append(
            {
                "name": container_name(spec.name),
                "image": _own_image(resolved, spec) or image,
                "command": ["sh", "-c", service_script(resolved, spec.name)],
                "env": [{"name": key, "value": str(value)} for key, value in env.items()],
                "ports": [{"containerPort": spec.port, "name": f"p{spec.port}"}],
                "readinessProbe": _probe_spec(spec.port, spec.ready_path),
                "volumeMounts": mount,
                "resources": {"requests": requests},
                "securityContext": hardened,
            }
        )
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "volumes": [{"name": "workspace", "emptyDir": {}}],
        "containers": containers,
    }
    deadline = int(settings.agentic_labs_deadline_hours) * 3600
    if deadline > 0:
        pod_spec["activeDeadlineSeconds"] = deadline
    annotations = {ANNOTATION_LAST_USED: _now(), ANNOTATION_SERVICE: resolved.key}
    if model_key_id:
        annotations[ANNOTATION_MODEL_KEY] = model_key_id
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "labels": {LABEL_MANAGED: MANAGED_BY, LABEL_WORKSPACE: workspace_id},
            "annotations": annotations,
        },
        "spec": pod_spec,
    }


def _ignored(path: str) -> bool:
    if any(fnmatch.fnmatchcase(path, pattern) for pattern in IGNORED_PATTERNS):
        return True
    parts = path.split("/")
    dirs = [pattern for pattern in IGNORED_PATTERNS if pattern.endswith("/")]
    return any(
        fnmatch.fnmatchcase("/".join(parts[:depth]) + "/", pattern) for depth in range(1, len(parts)) for pattern in dirs
    )


async def checkout_archive(
    workspace: WorkspaceSession, checkout: str, *, since: float | None = None
) -> tuple[bytes, int]:
    """The files of *checkout* (the whole workspace for ``""``) as one tar, what a command builds left out.

    With *since* (a ``time.time()`` stamp), only the files written at or after it: what a sync puts in a running lab.
    """
    entries = await workspace.entries(refresh=True)
    record = await workspace.record(create=False) if entries else None
    if record is None:
        return _tar([]), 0
    paths = [
        path
        for path in sorted(entries)
        if is_under(path, checkout) and not _ignored(path) and (since is None or entries[path].mtime >= since)
    ]
    # One read per distinct content, _ARCHIVE_READ_CONCURRENCY at a time: in
    # sequence, a 6,000-file checkout spent about 200 s here before a lab started.
    blobs: dict[str, bytes] = {}
    gate = asyncio.Semaphore(_ARCHIVE_READ_CONCURRENCY)
    # The first path of each distinct content: the file a failed read is named by.
    first_path: dict[str, str] = {}
    for path in paths:
        first_path.setdefault(entries[path].sha256, path)

    async def read(sha256: str) -> None:
        # One more try for a blob read that failed (a store hiccup under 32 parallel reads), then the
        # failure names the file: a TaskGroup's own message ("1 sub-exception") names nothing (#10928).
        async with gate:
            for attempt in range(1, _BLOB_READ_ATTEMPTS + 1):
                try:
                    blobs[sha256] = await workspace.store.read_blob(record.id, sha256)
                    return
                except Exception as exc:
                    if attempt == _BLOB_READ_ATTEMPTS:
                        raise ArchiveReadError(
                            f"{first_path[sha256]} could not be read from the workspace store "
                            f"({format_exception_message(exc)}, after {attempt} attempts)"
                        ) from exc

    try:
        async with asyncio.TaskGroup() as reads:
            for sha256 in first_path:
                reads.create_task(read(sha256))
    except BaseExceptionGroup as group:
        raise first_leaf(group) from group
    return _tar([(path, entries[path].mtime, blobs[entries[path].sha256]) for path in paths]), len(paths)


class ArchiveReadError(RuntimeError):
    """A file of the checkout the lab copy could not read, named with its cause."""


def _tar(members: list[tuple[str, float, bytes]]) -> bytes:
    """*members* (path, mtime, bytes) as one uncompressed tar, in the given order."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for path, mtime, data in members:
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mtime = mtime
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


async def copy_checkout(client: KubeClient, name: str, archive: bytes, *, sleep: Sleep = asyncio.sleep) -> None:
    """Unpack *archive* in the workspace container of pod *name*, within its own copy budget.

    Each attempt has ``AGENTIC_LABS_COPY_SECONDS``, apart from the pod start budget. A copy that
    times out or loses its exec stream is tried once more on the same pod (tar overwrites what the
    first attempt wrote): a 165 MB checkout stalled once at 600 s, then unpacked in under 3 minutes.
    A first stream the API server refused at the handshake (HTTP 404 around a worker loss) is tried
    again after a short pause.
    A tar that exits non-zero is raised at once.
    """
    from app.core.config import settings

    budget = float(settings.agentic_labs_copy_seconds)
    unpack = f"tar -xf - -C {LAB_WORKSPACE} && mkdir -p {LAB_WORKSPACE}/.xtm && touch {READY_MARKER}"
    for attempt in range(1, _COPY_ATTEMPTS + 1):
        try:
            result = await client.exec(name, WORKSPACE_CONTAINER, ["sh", "-c", unpack], stdin=archive, timeout=budget)
        except KubeError as exc:
            if attempt == _COPY_ATTEMPTS:
                raise KubeError(f"{exc}, after {attempt} attempts", exc.status, handshake=exc.handshake) from exc
            logger.warning("lab %s: checkout copy attempt %d failed (%s), retrying", name, attempt, exc)
            if exc.handshake:
                await sleep(_COPY_REFUSED_PAUSE_SECONDS)
            continue
        if result.exit_code != 0:
            # The archive or the volume is at fault: a retry would fail the same way.
            raise KubeError(result.stderr.decode("utf-8", errors="replace")[-500:] or result.message)
        return


async def _held(workspace: WorkspaceSession) -> dict[str, dict[str, Any]]:
    # As stored now: a start and a stop of the same lab may run in two calls (two sessions, two replicas).
    return dict((await workspace.latest_kernel_state() or {}).get("labs") or {})


async def _remember(workspace: WorkspaceSession, key: str, value: dict[str, Any] | None) -> None:
    # Under the kernel state's lock: two labs of one workspace started in parallel each write their own key, and
    # neither puts back the other's older record.
    def change(state: dict) -> dict | None:
        labs = dict(state.get("labs") or {})
        if value is None:
            if labs.pop(key, None) is None:
                return None
        else:
            labs[key] = value
        return {**state, "labs": labs}

    await workspace.update_kernel_state(change)


async def find(workspace: WorkspaceSession, service_id: str) -> tuple[str, dict[str, Any]] | None:
    """The lab *service_id* names in *workspace* (its pod name or its service key), or ``None``."""
    for key, value in (await _held(workspace)).items():
        if value.get("pod") == service_id or key == service_id:
            return key, value
    return None


def _pod_ip(pod: dict[str, Any] | None) -> str:
    return str(((pod or {}).get("status") or {}).get("podIP") or "")


def _statuses(pod: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {status.get("name", ""): status for status in (pod.get("status") or {}).get("containerStatuses") or []}


def _terminated(pod: dict[str, Any], container: str) -> str:
    state = (_statuses(pod).get(container) or {}).get("state") or {}
    ended = state.get("terminated")
    if ended:
        return f"exited with code {ended.get('exitCode')} ({ended.get('reason') or 'no reason given'})"
    waiting = state.get("waiting") or {}
    if waiting.get("reason") in {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError"}:
        return f"cannot start: {waiting.get('reason')} {waiting.get('message') or ''}".strip()
    return ""


def _target_ready(pod: dict[str, Any], target: str) -> bool:
    return bool((_statuses(pod).get(target) or {}).get("ready"))


def _chain_failed(pod: dict[str, Any], resolved: ResolvedService) -> str:
    """The first service of the chain whose container ended or cannot start, as ``name why``; ``""`` if none."""
    for spec in resolved.chain:
        why = _terminated(pod, container_name(spec.name))
        if why:
            return f"{spec.name} {why}"
    return ""


def _starting_detail(pod: dict[str, Any], container: str) -> str:
    """What a not-ready container is doing, from its status: the waiting reason (an image pull shows as
    ``ContainerCreating``) and its restart count, so a silent step reads as more than ``starting``."""
    status = _statuses(pod).get(container) or {}
    reason = str(((status.get("state") or {}).get("waiting") or {}).get("reason") or "")
    restarts = int(status.get("restartCount") or 0)
    parts = [reason] if reason else []
    if restarts:
        parts.append(f"restarted {restarts}x")
    return f" ({', '.join(parts)})" if parts else ""


def service_states(pod: dict[str, Any], held: dict[str, Any]) -> str:
    """One line naming each service of the lab: ``ready``, ``starting`` (with its waiting reason and restart
    count when the container reports them) or why it ended (its exit code)."""
    containers = held.get("containers") or {}
    states = []
    for name in held.get("services") or []:
        container = containers.get(name) or container_name(name)
        ended = _terminated(pod, container)
        if ended:
            states.append(f"{name} {ended}")
        elif _target_ready(pod, container):
            states.append(f"{name} ready")
        else:
            states.append(f"{name} starting{_starting_detail(pod, container)}")
    return "services: " + ", ".join(states) if states else ""


async def _wait(
    client: KubeClient, name: str, ready: Callable[[dict[str, Any]], bool], failed: Callable[[dict[str, Any]], str],
    timeout: float, *, sleep: Sleep, clock: Clock,
) -> tuple[dict[str, Any] | None, str]:
    deadline = clock() + timeout
    delay = _POLL_START
    while True:
        pod = await client.get_pod(name)
        if pod is None:
            return None, "the pod is gone"
        if ready(pod):
            return pod, ""
        why = failed(pod)
        if why:
            return pod, why
        if clock() >= deadline:
            return pod, f"{_NOT_READY_WITHIN}{timeout:g}s"
        await sleep(min(delay, max(0.0, deadline - clock())))
        delay = min(delay * 2, _POLL_MAX)


async def _tail(client: KubeClient, name: str, container: str) -> str:
    """The last lines of *container*'s own output, as a block to append to an answer; ``""`` when none."""
    try:
        text = await client.logs(name, container, tail_lines=_EXIT_TAIL_LINES, limit_bytes=_READY_LOG_BYTES)
    except KubeError:
        return ""
    lines = text.rstrip("\n").splitlines()[-_EXIT_TAIL_LINES:]
    return f"\nIts last {len(lines)} line(s):\n" + "\n".join(lines) if lines else ""


async def _ended(client: KubeClient, name: str, pod: dict[str, Any] | None, resolved: ResolvedService) -> str:
    """The first service of the chain whose container ended or cannot start, with its own last lines; ``""`` if none.

    A pod runs with ``restartPolicy: Never``: such a container never becomes ready, so the start answers it at once
    instead of waiting out its readiness window.
    """
    for spec in resolved.chain:
        container = container_name(spec.name)
        why = _terminated(pod or {}, container)
        if why:
            return f"{spec.name} {why}{await _tail(client, name, container)}"
    return ""


async def _wait_logged(
    client: KubeClient, name: str, resolved: ResolvedService, deadline: float, *, sleep: Sleep, clock: Clock
) -> str:
    """Wait until every service of the chain that declares ``ready_log`` has logged it; ``""`` or why not.

    The pod is read again after each read that misses the text: a service of the chain that ended meanwhile is
    answered at once, with its exit and its last lines, never at the deadline.
    """
    for spec in resolved.chain:
        if not spec.ready_log:
            continue
        delay = _POLL_START
        while True:
            try:
                text = await client.logs(
                    name, container_name(spec.name), tail_lines=_READY_LOG_LINES, limit_bytes=_READY_LOG_BYTES
                )
            except KubeError:
                text = ""
            if spec.ready_log in text:
                break
            ended = await _ended(client, name, await client.get_pod(name), resolved)
            if ended:
                return ended
            if clock() >= deadline:
                return f"{spec.name} did not log {spec.ready_log!r}{_LOGGED_IN_TIME}"
            await sleep(min(delay, max(0.0, deadline - clock())))
            delay = min(delay * 2, _POLL_MAX)
    return ""


def _browsable_ports(resolved: ResolvedService) -> list[int]:
    """The ports of the lab the browser may open: the started service's, then each chain service declaring a ready_path.

    A service with a ready_path answers HTTP (a lab OpenCTI under a lab XTM One); a store without one (search,
    broker, Redis, S3, a database) is never admitted.
    """
    ports = [resolved.spec.port]
    for spec in resolved.chain:
        if spec.ready_path and spec.port not in ports:
            ports.append(spec.port)
    return ports


def _held_ports(held: dict[str, Any]) -> list[int]:
    """The ports a lab record admits: its recorded ports, or its single port for a record written before them."""
    ports = [int(port) for port in held.get("ports") or [] if int(port or 0) > 0]
    return ports or ([int(held["port"])] if int(held.get("port") or 0) > 0 else [])


def _other_previews(held: dict[str, Any]) -> list[str]:
    """One line naming the lab's other admitted HTTP services (a lab OpenCTI), when it has any."""
    pod_ip = str(held.get("pod_ip") or "")
    others = [port for port in _held_ports(held) if port != int(held.get("port") or 0)]
    if not pod_ip or not others:
        return []
    return ["Also admitted for the browser and web_fetch: " + ", ".join(f"http://{pod_ip}:{port}/" for port in others)]


async def _admit(pod_ip: str, ports: list[int], *, owner: str) -> None:
    """Admit every browsable port of the lab at *pod_ip* for the idle horizon, named for its pod."""
    for port in ports:
        await preview.register_origin(pod_ip, port, _ttl(), owner=owner)


def _ttl() -> int:
    """The preview's registration: the idle reaper's horizon, or a day when it is off."""
    from app.core.config import settings

    hours = int(settings.agentic_labs_idle_hours)
    return hours * 3600 if hours > 0 else 86400


def _describe(resolved: ResolvedService, held: dict[str, Any], *, note: str = "", seconds: float = 0.0) -> str:
    head = f"{resolved.name}: ready" + (f" ({note})" if note else f" in {seconds:.1f}s")
    lines = [
        head,
        f"service_id: {held['pod']}",
        f"preview_url: {held['preview_url']}"
        if held.get("preview_url")
        else "preview_url: not recorded yet; sandbox_service_status(service_id) answers it.",
        "Open it with the browser tool or web_fetch.",
        *_other_previews(held),
        "Logs: sandbox_service_logs(service_id) (one container per service of the lab pod). "
        "It stops at sandbox_stop_service, or after the lab's idle time.",
    ]
    if held.get("licences"):
        lines.append(str(held["licences"]) + _licence_expiry(held))
    if held.get("model"):
        lines.append(str(held["model"]))
    lines += _session_lines(held)
    return "\n".join(lines)


def _licence_expiry(held: dict[str, Any]) -> str:
    expires = str(held.get("licences_expire_at") or "")
    return f" (valid until {expires})" if expires else ""


def _licences_lapsing(held: dict[str, Any], *, now: datetime | None = None) -> str:
    """Why the licences the pod of *held* was created with no longer cover a review (``""`` while they do).

    A lab's licences are short-lived (``licences.issue``) and reach its containers as environment variables, so a lab
    kept past them (a reused lab, a run resumed the next day) shows the Community Edition splash: such a pod is
    replaced, with fresh licences, instead of adopted.
    """
    raw = str(held.get("licences_expire_at") or "")
    if not raw:
        return ""
    try:
        expires = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    if expires - (now or datetime.now(UTC)) > timedelta(hours=LICENCE_MARGIN_HOURS):
        return ""
    return f"its Enterprise Edition licences expire at {raw}"


def _session_url(url: str, preview_url: str) -> str:
    """*url* with a loopback origin replaced by the lab's preview origin (the address the browser tool reaches)."""
    if not preview_url:
        return url
    parts = urlsplit(url)
    if parts.hostname not in ("localhost", "127.0.0.1"):
        return url
    preview = urlsplit(preview_url)
    return urlunsplit((preview.scheme, preview.netloc, parts.path, parts.query, parts.fragment))


def parse_sessions(text: str, prefix: str) -> list[tuple[str, str]]:
    """The ``(label, url)`` of each line of *text* holding *prefix* then ``<label>: <url>``, the last per label."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        at = line.find(prefix)
        if at < 0:
            continue
        label, sep, url = line[at:].partition(": ")
        url = url.strip()
        if not sep or not url.startswith(("http://", "https://")) or len(url) > _MAX_SESSION_CHARS:
            continue
        found.pop(label.strip(), None)
        found[label.strip()] = url
    return list(found.items())[-_MAX_SESSIONS:]


async def _read_sessions(client: KubeClient, held: dict[str, Any]) -> list[list[str]]:
    """The signed-in sessions the lab's services logged (their ``session_log`` lines), URLs on the preview origin."""
    sessions: list[list[str]] = []
    for container, prefix in (held.get("session_logs") or {}).items():
        try:
            text = await client.logs(held["pod"], container, tail_lines=_READY_LOG_LINES, limit_bytes=_READY_LOG_BYTES)
        except KubeError:
            continue
        sessions += [
            [label, _session_url(url, str(held.get("preview_url") or ""))]
            for label, url in parse_sessions(text, prefix)
        ]
    return sessions[-_MAX_SESSIONS:]


async def _with_sessions(
    client: KubeClient, workspace: WorkspaceSession, key: str, held: dict[str, Any]
) -> dict[str, Any]:
    """*held* with its signed-in sessions, read once from the logs and kept in the record (a later tail may lose them)."""
    if held.get("sessions") or not held.get("session_logs") or not held.get("preview_url"):
        return held
    sessions = await _read_sessions(client, held)
    if not sessions:
        return held
    await _if_current(workspace, key, held["pod"], {"sessions": sessions})
    return {**held, "sessions": sessions}


def _session_lines(held: dict[str, Any]) -> list[str]:
    sessions = held.get("sessions") or []
    if not sessions:
        return []
    lines = [
        "Signed-in sessions (one browser navigate on a URL signs the tab in as that account; navigate again after a "
        "run resume finds the tab signed out):"
    ]
    return lines + [f"- {label}: {url}" for label, url in sessions]


async def start_lab(
    resolved: ResolvedService,
    *,
    workspace: WorkspaceSession,
    client: KubeClient | None = None,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
    background: bool = False,
    answer_seconds: float = ANSWER_SECONDS,
) -> ServiceOutcome:
    """Start *resolved* as a lab pod; the outcome, never an exception. No quota is checked or counted (D1b).

    The readiness is awaited for *answer_seconds* at most (none with *background*): a lab still starting past it,
    with no service ended, is answered ``starting`` with its service_id, and a later start of the same declaration
    follows that lab instead of creating another one.
    """
    from app.core.config import settings

    budget = 0.0 if background else max(0.0, float(answer_seconds))

    try:
        client = client or KubeClient()
    except KubeError as exc:
        return ServiceOutcome(text=f"Error: {exc}", refused=True)
    target = container_name(resolved.name)
    following: dict[str, Any] | None = None
    replaced = ""
    # Adoption and stop of one lab hold the same lock (across API replicas): a stop waits for the adoption to answer,
    # then finds the complete record and cleans it up; it never runs between the read of the pod and the answer.
    async with workspace.lock(_lock_name(resolved.key)):
        held = (await _held(workspace)).get(resolved.key)
        if held and held.get("pod"):
            try:
                pod = await client.get_pod(held["pod"])
            except KubeError as exc:
                return ServiceOutcome(text=f"Error: the lab could not be read: {exc}", refused=True)
            pod_ip = _pod_ip(pod)
            # The pod serves the declaration it was created from: any change (image, port, argv, cwd, env...)
            # replaces it.
            fingerprint = _fingerprint(resolved)
            lapsing = _licences_lapsing(held)
            if (
                _live(pod)
                and pod_ip
                and _same_declaration(held, resolved, fingerprint)
                and not lapsing
                and (_statuses(pod or {}).get(target) or {}).get("ready")
            ):
                # A start that timed out keeps a partial record (no pod IP yet): a pod ready since is adopted.
                if (
                    held.get("pod_ip") != pod_ip
                    or not held.get("preview_url")
                    or "fingerprint" not in held
                    or "ports" not in held
                ):
                    held = {
                        **held,
                        "ports": _browsable_ports(resolved),
                        "pod_ip": pod_ip,
                        "preview_url": f"http://{pod_ip}:{resolved.spec.port}/",
                        "fingerprint": fingerprint,
                    }
                    await _remember(workspace, resolved.key, held)
                await _touch(client, held["pod"])
                await _admit(pod_ip, _browsable_ports(resolved), owner=held["pod"])
                adopted = held
            elif (
                _live(pod) and _same_declaration(held, resolved, fingerprint) and "files" in held
                and "ready_seconds" not in held and not held.get("timed_out") and not _chain_failed(pod or {}, resolved)
                and start_window_left(held, _window(resolved)) > 0
            ):
                # A lab still starting (its checkout copied, nothing ended, its window not over): followed, never
                # replaced, so a start repeated after a 'starting' answer or a cut call never creates a second pod.
                following = held
                adopted = None
            else:
                if lapsing and _live(pod):
                    replaced = f"a new lab: the previous one ({held['pod']}) was replaced because {lapsing}"
                await _drop_pod(client, held, delete=pod is not None)
                await _remember(workspace, resolved.key, None)
                adopted = None
        else:
            adopted = None
    if adopted is not None:
        return await _answer_adopted(
            resolved, adopted, workspace=workspace, client=client, sleep=sleep, clock=clock, budget=budget
        )
    if following is not None:
        await _touch(client, following["pod"])
        return await _await_ready(
            resolved, following, workspace=workspace, client=client, window=start_window_left(following, _window(resolved)),
            budget=budget, started=clock(), sleep=sleep, clock=clock, note="already starting",
        )

    image = settings.opensandbox_images.get(resolved.image, "")
    if not image:
        return ServiceOutcome(text=f"Error: image key {resolved.image!r} names no image.", refused=True)
    record = await workspace.record(create=True)
    name = f"lab-{uuid.uuid4().hex[:12]}"
    started = clock()
    # Fresh short-lived licences per lab; one the server cannot issue is named, never fatal.
    issued = await licences.issue()
    # A live model only for a declaration that opts in (LAB_MODEL = "live"): a key of the LLM proxy, never a
    # provider credential; a key that cannot be minted leaves the stub model and is named.
    minted = await model_keys.mint(resolved, pod=name)
    try:
        await client.create_pod(
            pod_manifest(
                resolved, name=name, image=image, workspace_id=str(record.id) if record else "",
                licence_env={**issued.env, **minted.env}, model_key_id=minted.key_id,
            )
        )
    except KubeError as exc:
        await model_keys.revoke(minted.key_id)
        return ServiceOutcome(text=f"Error: the lab pod could not be created: {exc}", refused=True)
    outcome = ServiceOutcome(text="", service_id=name)
    held = {
        "pod": name,
        "image": resolved.image,
        "port": resolved.spec.port,
        "ports": _browsable_ports(resolved),
        "services": [spec.name for spec in resolved.chain],
        "fingerprint": _fingerprint(resolved),
        "containers": {spec.name: container_name(spec.name) for spec in resolved.chain},
        "started_at": _now(),
        # The variables set and those missing, never a PEM.
        "licences": issued.summary(),
        # When they lapse: a later start replaces the pod before that rather than adopt it (_licences_lapsing).
        "licences_expire_at": (
            (datetime.now(UTC) + timedelta(hours=issued.hours)).isoformat() if issued.env and issued.hours else ""
        ),
        # Where each service logs its signed-in session URLs, read once the lab is ready (_with_sessions).
        "session_logs": {container_name(spec.name): spec.session_log for spec in resolved.chain if spec.session_log},
        # The key's id and prefix, never the key.
        "model": minted.summary(),
        "model_key_id": minted.key_id,
    }
    await _remember(workspace, resolved.key, held)
    start_timeout = float(settings.agentic_labs_start_seconds)

    def running(pod: dict[str, Any]) -> bool:
        # The key says it: ``running`` may be an empty object (no ``startedAt`` yet).
        return "running" in ((_statuses(pod).get(WORKSPACE_CONTAINER) or {}).get("state") or {})

    pod, why = await _wait(
        client, name, running, lambda p: _terminated(p, WORKSPACE_CONTAINER), start_timeout, sleep=sleep, clock=clock
    )
    if why:
        outcome.text = _failed(resolved, name, WORKSPACE_CONTAINER, why, clock() - started, held)
        return outcome
    copied_at = time.time()
    try:
        archive, count = await checkout_archive(workspace, resolved.checkout)
        await copy_checkout(client, name, archive, sleep=sleep)
    except Exception as exc:
        cause = format_exception_message(first_leaf(exc))
        outcome.text = f"Error: the checkout could not be put in the lab ({cause})."
        outcome.refused = True
        await _delete_quietly(client, name)
        await model_keys.revoke(minted.key_id)
        await _remember(workspace, resolved.key, None)
        outcome.service_id = ""
        return outcome
    # The checkout is in: from now on a later start of this declaration follows this pod while it starts.
    held = {**held, "files": count, "copied_at": copied_at}
    await _remember(workspace, resolved.key, held)
    # The answer budget counts from the call: what the scheduling and the copy took is no longer the readiness's.
    left = 0.0 if background else max(1.0, budget - (clock() - started))
    return await _await_ready(
        resolved, held, workspace=workspace, client=client, window=float(sum(spec.timeout for spec in resolved.chain)),
        budget=left, started=started, sleep=sleep, clock=clock, note=replaced,
    )


#: Seconds before the last copy a sync also sends: a file written while that copy was read is sent again.
_SYNC_SLACK_SECONDS = 2.0


async def sync_lab(
    resolved: ResolvedService,
    *,
    workspace: WorkspaceSession,
    client: KubeClient | None = None,
    restart: str = "",
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
    answer_seconds: float = ANSWER_SECONDS,
) -> ServiceOutcome:
    """Copy into the running lab of *resolved* the checkout files written since its last copy; the outcome.

    The services keep running: a dev server that watches its files reloads them. With *restart* (a service of the
    lab), that service's process is then run again in its own container (#12104): the pod, the other services and
    what the container wrote (build caches, build output) are kept, and the call waits for it to answer again.
    A file removed from the workspace stays in the lab.
    """
    try:
        client = client or KubeClient()
    except KubeError as exc:
        return ServiceOutcome(text=f"Error: {exc}", refused=True)
    async with workspace.lock(_lock_name(resolved.key)):
        held = (await _held(workspace)).get(resolved.key)
        if not held or not held.get("pod") or "files" not in held:
            return ServiceOutcome(
                text=f"Error: no lab of {resolved.key} holds a copy of the checkout yet: start it first "
                "(sandbox_start_service without sync), then sync the edits made after.",
                refused=True,
            )
        names = list(held.get("services") or [])
        if restart and restart not in names:
            return ServiceOutcome(
                text=f"Error: restart names {restart!r}, not a service of this lab ({', '.join(names) or 'none'}).",
                refused=True,
            )
        try:
            pod = await client.get_pod(held["pod"])
        except KubeError as exc:
            return ServiceOutcome(text=f"Error: the lab could not be read: {exc}", refused=True)
        if not _live(pod):
            return ServiceOutcome(
                text=f"Error: the lab {held['pod']} is not running: start it again (the start copies the checkout).",
                refused=True,
            )
        # A record of a lab started before the stamp existed: its start time bounds what it holds.
        since = held.get("copied_at")
        if not isinstance(since, (int, float)):
            started_at = str(held.get("started_at") or "")
            try:
                since = datetime.fromisoformat(started_at).timestamp()
            except ValueError:
                since = 0.0
        copied_at = time.time()
        try:
            archive, count = await checkout_archive(
                workspace, resolved.checkout, since=float(since) - _SYNC_SLACK_SECONDS
            )
            if count:
                await copy_checkout(client, held["pod"], archive, sleep=sleep)
        except Exception as exc:
            cause = format_exception_message(first_leaf(exc))
            return ServiceOutcome(text=f"Error: the edits could not be put in the lab ({cause}).", refused=True)
        await _remember(workspace, resolved.key, {**held, "copied_at": copied_at})
    await _touch(client, held["pod"])
    what = (
        f"{count} file(s) written since the last copy" if count else "nothing: no file was written since the last copy"
    )
    synced = f"{resolved.name}: synced {what} into the lab {held['pod']}."
    if restart:
        return await _restart_service(
            resolved,
            held,
            restart,
            client=client,
            synced=synced,
            sleep=sleep,
            clock=clock,
            budget=answer_seconds,
            pod=pod,
        )
    return ServiceOutcome(
        text=(
            f"{synced} The services keep running: one that watches its files reloads them; restart=NAME runs "
            "another again in its container, its build caches kept."
        ),
        service_id=held["pod"],
        preview_url=held.get("preview_url", ""),
        ready=bool(held.get("ready_seconds") is not None or held.get("preview_url")),
    )


def _after_restart(text: str) -> str | None:
    """The part of a container's output written by the run after the last in-place restart; None without a mark."""
    at = text.rfind(_RESTART_MARK)
    return None if at < 0 else text[at + len(_RESTART_MARK) :]


async def _wait_restart_logged(
    client: KubeClient,
    name: str,
    container: str,
    ready_log: str,
    deadline: float,
    *,
    sleep: Sleep,
    clock: Clock,
) -> str:
    """Wait for *ready_log* in what the restarted run wrote (after the supervisor's mark); ``""`` or why not.

    The old run's own ``lab up`` line is still in the container log: without this wait a restart counted the lab
    ready while the new run still migrated or seeded. A pod whose supervisor writes no mark (created before it) is
    not waited on: its port readiness stands, as before.
    """
    delay = _POLL_START
    while True:
        try:
            text = await client.logs(name, container, tail_lines=_READY_LOG_LINES, limit_bytes=_READY_LOG_BYTES)
        except KubeError:
            text = ""
        after = _after_restart(text)
        if after is None or ready_log in after:
            return ""
        pod = await client.get_pod(name)
        ended = _terminated(pod or {}, container)
        if pod is None or ended:
            return ended or "the pod is gone"
        if clock() >= deadline:
            return f"{_NOT_READY_WITHIN}the budget (no {ready_log!r} logged since the restart)"
        await sleep(min(delay, max(0.0, deadline - clock())))
        delay = min(delay * 2, _POLL_MAX)


async def _restart_service(
    resolved: ResolvedService,
    held: dict[str, Any],
    service: str,
    *,
    client: KubeClient,
    synced: str,
    sleep: Sleep,
    clock: Clock,
    budget: float,
    pod: dict[str, Any] | None = None,
) -> ServiceOutcome:
    """Run *service*'s process again in its container of the lab *held*, then wait for it to answer (#12104).

    A service whose container has already exited cannot be run again in place: the pod runs with
    ``restartPolicy: Never``, so the container is gone for good and the exec stream into it is refused by the API
    server (an HTTP 500 on the WebSocket upgrade). That case is answered before any exec, with the exit and the
    container's last lines, and the way out: stop the lab and start it again (#115).
    """
    name = held["pod"]
    container = (held.get("containers") or {}).get(service) or container_name(service)
    exited = _terminated(pod or {}, container)
    if exited:
        return ServiceOutcome(
            text=(
                f"Error: {synced} {service} was not restarted: its container {exited}, and a lab container that "
                "has exited cannot be run again in place. Read why with sandbox_service_logs(service_id, "
                f"service={service!r}), then sandbox_stop_service and sandbox_start_service again (the start "
                f"copies the checkout).{await _tail(client, name, container)}"
            ),
            refused=True,
            service_id=name,
        )
    started = clock()
    result = await client.exec(
        name, container, ["sh", "-c", restart_script()], timeout=_RESTART_TERM_SECONDS + _RESTART_KILL_SECONDS + 30.0
    )
    if result.exit_code == 3:
        return ServiceOutcome(
            text=(
                f"Error: {synced} {service} was not restarted: its container predates in-place restarts. Stop the lab "
                "and start it again once; the next pods restart a service in place."
            ),
            refused=True,
            service_id=name,
        )
    if result.exit_code != 0:
        detail = (result.stderr or b"").decode("utf-8", errors="replace")[-300:] or result.message
        return ServiceOutcome(
            text=f"Error: {synced} {service} could not be restarted: {detail}", refused=True, service_id=name
        )
    # The old process ended; its readiness probe needs a few periods to say so: wait for that, then for the new one.
    down_until = clock() + _RESTART_DOWN_SECONDS
    while clock() < down_until:
        pod = await client.get_pod(name)
        if pod is None or not _target_ready(pod, container) or _terminated(pod, container):
            break
        await sleep(_POLL_START)
    pod, why = await _wait(
        client,
        name,
        lambda p: _target_ready(p, container),
        lambda p: _chain_failed(p, resolved),
        max(1.0, budget - (clock() - started)),
        sleep=sleep,
        clock=clock,
    )
    spec = next((s for s in resolved.chain if s.name == service), None)
    if not why and spec is not None and spec.ready_log:
        why = await _wait_restart_logged(
            client, name, container, spec.ready_log, started + budget, sleep=sleep, clock=clock
        )
    seconds = clock() - started
    target = spec.restart_target if spec is not None else 0
    verdict = ""
    if target and not why:
        met = "within" if seconds <= target else "over"
        verdict = f" Ready in {seconds:.0f}s, {met} its restart target of {target}s."
    elif target and seconds > target:
        verdict = f" Already over its restart target of {target}s."
    if not why:
        return ServiceOutcome(
            text=(
                f"{synced} {service} restarted in its container and answering again after {seconds:.1f}s; the pod, "
                f"the other services and the container's build caches were kept.{verdict}"
            ),
            service_id=name,
            preview_url=held.get("preview_url", ""),
            ready=True,
            seconds=seconds,
        )
    if why.startswith(_NOT_READY_WITHIN) and pod is not None and not _chain_failed(pod, resolved):
        return ServiceOutcome(
            text=(
                f"{synced} {service} restarted in its container, not answering yet after {seconds:.1f}s.{verdict} "
                "Follow it with sandbox_service_status(service_id) and sandbox_service_logs(service_id, service=...)."
            ),
            service_id=name,
            preview_url=held.get("preview_url", ""),
            starting=True,
            seconds=seconds,
        )
    ended = await _ended(client, name, pod, resolved) if pod is not None else ""
    return ServiceOutcome(
        text=f"Error: {synced} {service} was restarted and did not come back: {ended or why}.",
        refused=True,
        service_id=name,
        seconds=seconds,
    )


def _window(resolved: ResolvedService) -> float:
    """Seconds a lab has from its record (``started_at``) to be ready: its scheduling, then its chain's timeouts."""
    from app.core.config import settings

    return float(settings.agentic_labs_start_seconds) + float(sum(spec.timeout for spec in resolved.chain))


async def _if_current(workspace: WorkspaceSession, key: str, pod: str, changes: dict[str, Any]) -> None:
    """Merge *changes* into the record of *key* only while it still names *pod* (a stop or a replacement wins)."""

    def change(state: dict[str, Any]) -> dict[str, Any] | None:
        labs = dict(state.get("labs") or {})
        current = labs.get(key)
        if not current or current.get("pod") != pod:
            return None
        labs[key] = {**current, **changes}
        return {**state, "labs": labs}

    await workspace.update_kernel_state(change)


async def _await_ready(
    resolved: ResolvedService, held: dict[str, Any], *, workspace: WorkspaceSession, client: KubeClient,
    window: float, budget: float | None, started: float, sleep: Sleep, clock: Clock, note: str = "",
) -> ServiceOutcome:
    """Wait for the lab *held* names to be ready (its target container, then the declared ``ready_log`` texts).

    *window* is what is left of its readiness window; *budget* (``None``: no cap) the seconds this call may wait.
    Past the budget, with nothing ended, the lab is answered ``starting``; past the window, ``not ready``.
    """
    name = held["pod"]
    target = container_name(resolved.name)
    names = [spec.name for spec in resolved.chain]
    capped = budget is not None and budget < window
    wait = min(window, budget) if capped and budget is not None else window
    deadline = clock() + wait
    pod, why = await _wait(
        client, name, lambda p: _target_ready(p, target), lambda p: _chain_failed(p, resolved), wait,
        sleep=sleep, clock=clock,
    )
    timed_out = why.startswith(_NOT_READY_WITHIN)
    if why and pod is not None:
        ended = await _ended(client, name, pod, resolved)
        why = ended or why
        timed_out = timed_out and not ended
    if not why and pod is not None:
        # A port that answers is not always a service that is done: what it logs after (a seed and its URLs) is
        # awaited when declared, so the first sandbox_service_logs read holds it.
        why = await _wait_logged(client, name, resolved, deadline, sleep=sleep, clock=clock)
        timed_out = why.endswith(_LOGGED_IN_TIME)
    seconds = clock() - started
    if why and timed_out and capped:
        return await _answer_starting(
            resolved, held, pod, seconds=seconds, left=max(0.0, window - wait), note=note, client=client
        )
    pod_ip = _pod_ip(pod)
    if why or not pod_ip:
        phase = ((pod or {}).get("status") or {}).get("phase") or "unknown"
        if timed_out:
            # Its whole window is over: a later start replaces it rather than follows it.
            await _if_current(workspace, resolved.key, name, {"timed_out": True})
        return ServiceOutcome(
            text=_failed(
                resolved, name, target, why or f"is ready but the pod has no IP yet (phase {phase})", seconds, held
            ),
            service_id=name,
            seconds=seconds,
            started=names,
        )
    held = {**held, "pod_ip": pod_ip, "preview_url": f"http://{pod_ip}:{resolved.spec.port}/",
            "ready_seconds": round(seconds, 1)}
    await _remember(workspace, resolved.key, held)
    await _admit(pod_ip, _browsable_ports(resolved), owner=name)
    held = await _with_sessions(client, workspace, resolved.key, held)
    return ServiceOutcome(
        text=_describe(resolved, held, note=f"{note}, ready after {seconds:.1f}s more" if note else "", seconds=seconds),
        service_id=name,
        preview_url=held["preview_url"],
        ready=True,
        seconds=seconds,
        started=names,
    )


async def _answer_starting(
    resolved: ResolvedService, held: dict[str, Any], pod: dict[str, Any] | None, *, seconds: float, left: float,
    note: str, client: KubeClient,
) -> ServiceOutcome:
    """The answer of a start whose budget ended before its lab was ready, nothing having ended: its id and how to follow it."""
    name = held["pod"]
    if pod is None:
        with contextlib.suppress(KubeError):
            pod = await client.get_pod(name)
    pod_ip = _pod_ip(pod)
    url = f"http://{pod_ip}:{resolved.spec.port}/" if pod_ip else ""
    if url:
        # Admitted now, so the browser tool can poll it while it starts.
        await _admit(pod_ip, _browsable_ports(resolved), owner=name)
    head = f"{resolved.name}: starting ({note + '; ' if note else ''}not ready after {seconds:.1f}s"
    head += f", its readiness window runs {left:.0f}s more)" if left else ")"
    lines = [head, f"service_id: {name}"]
    lines.append(f"preview_url: {url} (serves once ready)" if url else "preview_url: not known yet (the pod has no IP yet).")
    states = service_states(pod or {}, held)
    if states:
        lines.append(states)
    lines.append(
        "Follow it: sandbox_service_status(service_id) names each service's state, sandbox_service_logs(service_id, "
        "service=NAME) reads its output now, and sandbox_start_service with the same arguments waits for it again "
        "(never a second lab)."
    )
    return ServiceOutcome(
        text="\n".join(lines), service_id=name, preview_url=url, starting=True, seconds=seconds,
        started=[spec.name for spec in resolved.chain],
    )


async def _answer_adopted(
    resolved: ResolvedService, held: dict[str, Any], *, workspace: WorkspaceSession, client: KubeClient,
    sleep: Sleep, clock: Clock, budget: float | None = None,
) -> ServiceOutcome:
    """The answer to a start that found its lab running: at once when a start of it completed, else once it is done.

    A record without ``ready_seconds`` is a start that never answered ready (cut by a run resume, or timed out):
    its pod's port may answer while the service still seeds, so the declared ``ready_log`` texts are awaited as a
    fresh start awaits them, for what is left of that start's window (at least one read), and the first
    ``sandbox_service_logs`` read holds them. The wait runs outside the adoption lock, so a stop is never held
    behind it.
    """
    if "ready_seconds" not in held and any(spec.ready_log for spec in resolved.chain):
        from app.core.config import settings

        window = float(settings.agentic_labs_start_seconds) + float(sum(spec.timeout for spec in resolved.chain))
        started = clock()
        left = start_window_left(held, window)
        capped = budget is not None and budget < left
        deadline = started + (min(left, budget) if capped and budget is not None else left)
        why = await _wait_logged(client, held["pod"], resolved, deadline, sleep=sleep, clock=clock)
        seconds = clock() - started
        if why and capped and why.endswith(_LOGGED_IN_TIME):
            return await _answer_starting(
                resolved, held, None, seconds=seconds, left=max(0.0, left - seconds), note="already running",
                client=client,
            )
        if why:
            return ServiceOutcome(
                text=_failed(resolved, held["pod"], container_name(resolved.name), why, seconds, held),
                service_id=held["pod"],
            )
        async with workspace.lock(_lock_name(resolved.key)):
            current = (await _held(workspace)).get(resolved.key)
            if not current or current.get("pod") != held["pod"]:
                return ServiceOutcome(
                    text=f"{resolved.name}: not ready: the lab was stopped while its start was awaited.",
                    refused=True,
                )
            # The adoption's own address fills what the stored record lacks (a write it raced with).
            held = {**held, **current, "ready_seconds": round(seconds, 1)}
            await _remember(workspace, resolved.key, held)
        note = f"already running, its start completed after {seconds:.1f}s more"
    else:
        note = "already running"
    held = await _with_sessions(client, workspace, resolved.key, held)
    return ServiceOutcome(
        text=_describe(resolved, held, note=note),
        service_id=held["pod"],
        preview_url=held.get("preview_url", ""),
        ready=True,
    )


def _failed(
    resolved: ResolvedService, pod: str, container: str, why: str, seconds: float, held: dict[str, Any] | None = None
) -> str:
    # A why may carry the ended container's last lines after its first line: they close the answer.
    why, _, tail = why.partition("\n")
    text = (
        f"{resolved.name}: not ready after {seconds:.1f}s: {container} {why}.\n"
        f"service_id: {pod}\n"
        "Read its output with sandbox_service_logs(service_id); stop it with sandbox_stop_service(service_id)."
    )
    text += f"\n{held['licences']}" if held and held.get("licences") else ""
    text += f"\n{held['model']}" if held and held.get("model") else ""
    return text + (f"\n{tail}" if tail else "")


async def _touch(client: KubeClient, name: str) -> None:
    try:
        await client.patch_annotations(name, {ANNOTATION_LAST_USED: _now()})
    except KubeError:
        return


async def _delete_quietly(client: KubeClient, name: str) -> None:
    try:
        await client.delete_pod(name)
    except KubeError:
        return


async def _drop_pod(client: KubeClient, held: dict[str, Any], *, delete: bool = True) -> None:
    """Delete a lab's pod and withdraw its preview, so an IP the cluster reuses is never left admitted.

    The admission is withdrawn only while it names this pod: when the cluster gave the IP to another lab since,
    that lab's preview stays.
    """
    if delete:
        await _delete_quietly(client, held["pod"])
    await model_keys.revoke(str(held.get("model_key_id") or ""))
    if held.get("pod_ip"):
        for port in _held_ports(held):
            await preview.unregister_origin(held["pod_ip"], port, owner=held["pod"])


def _fingerprint(resolved: ResolvedService) -> str:
    """A digest of everything the pod was built from: the image key and every service of the chain, field by field."""
    return hashlib.sha256(repr((resolved.image, resolved.chain)).encode("utf-8")).hexdigest()[:16]


def _same_declaration(held: dict[str, Any], resolved: ResolvedService, fingerprint: str) -> bool:
    """Whether the recorded pod serves *resolved*'s declaration.

    A record written before the fingerprint existed is matched on what it does record (the image key, the port and
    the service chain) and gets the fingerprint at its adoption, so a running lab survives the upgrade.
    """
    if "fingerprint" in held:
        return bool(held["fingerprint"] == fingerprint)
    return (
        held.get("image") == resolved.image
        and held.get("port") == resolved.spec.port
        and held.get("services") == [spec.name for spec in resolved.chain]
    )


def _lock_name(key: str) -> str:
    return f"lab:{key}"


def _live(pod: dict[str, Any] | None) -> bool:
    """A pod that exists, is not being deleted and runs: a terminating pod is gone, never adopted."""
    if pod is None or (pod.get("metadata") or {}).get("deletionTimestamp"):
        return False
    return (pod.get("status") or {}).get("phase") == "Running"


def env_presence(pod: dict[str, Any], names: list[str]) -> list[str]:
    """One line per container of *pod*: each of *names* ``set``, ``set (empty)`` or ``missing``, never a value.

    Read from the pod spec (what the containers were created with), so a variable a
    service exports from its own script is not seen.
    """
    wanted = [name.strip() for name in names if name and name.strip()]
    lines: list[str] = []
    for container in (pod.get("spec") or {}).get("containers") or []:
        declared = {item.get("name"): item.get("value") or item.get("valueFrom") for item in container.get("env") or []}
        states = []
        for name in wanted:
            if name not in declared:
                states.append(f"{name} missing")
            else:
                states.append(f"{name} set" if declared[name] else f"{name} set (empty)")
        lines.append(f"env in {container.get('name', '?')}: " + ", ".join(states))
    return lines


async def lab_status(
    workspace: WorkspaceSession, service_id: str, *, env: list[str] | None = None, client: KubeClient | None = None
) -> ServiceOutcome:
    found = await find(workspace, service_id)
    if found is None:
        return not_running(service_id)
    key, held = found
    client = client or KubeClient()
    pod = await client.get_pod(held["pod"])
    if pod is None:
        await _remember(workspace, key, None)
        return ServiceOutcome(text=f"{key}: gone (its pod ended).")
    target = held.get("containers", {}).get(key.rsplit(":", 1)[-1]) or ""
    ready = bool((_statuses(pod).get(target) or {}).get("ready"))
    await _touch(client, held["pod"])
    if held.get("pod_ip"):
        await _admit(held["pod_ip"], _held_ports(held), owner=held["pod"])
    lines = [
        f"{key}: {'answering' if ready else 'not answering'} on port {held.get('port')}",
        f"service_id: {held['pod']}",
        f"phase: {(pod.get('status') or {}).get('phase', '?')}",
        f"started_at: {held.get('started_at', '?')}",
    ]
    states = service_states(pod, held)
    if states:
        lines.append(states)
    if held.get("preview_url"):
        lines.append(f"preview_url: {held['preview_url']}")
    if held.get("licences"):
        lines.append(str(held["licences"]) + _licence_expiry(held))
    if ready:
        lines += _session_lines(await _with_sessions(client, workspace, key, held))
    if env:
        lines += env_presence(pod, env)
    return ServiceOutcome(text="\n".join(lines), service_id=held["pod"], preview_url=held.get("preview_url", ""), ready=ready)


async def lab_logs(
    workspace: WorkspaceSession, service_id: str, *, service: str = "", lines: int = DEFAULT_LOG_LINES,
    pattern: str = "", ignore_case: bool = False, client: KubeClient | None = None,
) -> ServiceOutcome:
    regex = compile_log_pattern(pattern, ignore_case=ignore_case)
    if isinstance(regex, str):
        return ServiceOutcome(text=regex, refused=True)
    found = await find(workspace, service_id)
    if found is None:
        return ServiceOutcome(text=f"Error: no lab {service_id!r} is running for this workspace.", refused=True)
    key, held = found
    names = list(held.get("services") or [])
    if service and service not in names:
        return ServiceOutcome(text=f"Error: {service!r} is not one of {', '.join(names) or 'its services'}.", refused=True)
    lines = max(1, min(int(lines or DEFAULT_LOG_LINES), MAX_LOG_LINES))
    # A filtered call reads the newest scan window and keeps the matches; an unfiltered one reads the tail alone.
    read_lines, read_bytes = (MAX_LOG_SCAN_LINES, MAX_LOG_CHARS * 40) if regex else (lines, MAX_LOG_CHARS * 4)
    client = client or KubeClient()
    parts = []
    for name in [service] if service else names:
        container = held.get("containers", {}).get(name) or container_name(name)
        try:
            text = await client.logs(held["pod"], container, tail_lines=read_lines, limit_bytes=read_bytes)
        except KubeError as exc:
            if exc.status == 404:
                await _remember(workspace, key, None)
                return ServiceOutcome(text=f"{key}: gone (its pod ended).")
            parts.append(f"== {name}: no output ({exc}) ==")
            continue
        parts.append(log_section(name, f"container {container}", text, lines, regex))
    await _touch(client, held["pod"])
    return ServiceOutcome(text="\n".join(parts), service_id=held["pod"])


async def lab_exec(
    workspace: WorkspaceSession, service_id: str, argv: list[str], *, timeout: float = 600.0,
    client: KubeClient | None = None,
) -> ExecResult:
    """Run *argv* in the lab's workspace container, in the checkout (the pod's exec API)."""
    found = await find(workspace, service_id)
    if found is None:
        raise KubeError(f"no lab {service_id!r} is running for this workspace")
    _key, held = found
    client = client or KubeClient()
    await _touch(client, held["pod"])
    return await client.exec(held["pod"], WORKSPACE_CONTAINER, list(argv), timeout=timeout)


def lab_command_line(cwd: str, argv: tuple[str, ...] | list[str], env: tuple[tuple[str, str], ...] = ()) -> list[str]:
    """The exec argv that runs *argv* in the lab's copy of the workspace at *cwd* (workspace-relative), with *env*."""
    where = f"{LAB_WORKSPACE}/{cwd}" if cwd else LAB_WORKSPACE
    assigned = [f"{key}={value}" for key, value in env]
    line = f"cd {shlex.quote(where)} && exec " + shlex.join(["env", *assigned, *argv] if assigned else list(argv))
    return ["sh", "-c", line]


def _bounded(data: bytes, limit: int) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    return f"[{len(text) - limit} earlier characters cut]\n" + text[-limit:]


async def run_check_in_lab(
    workspace: WorkspaceSession,
    service_id: str,
    *,
    checkout: str,
    label: str,
    argv: tuple[str, ...],
    cwd: str,
    env: tuple[tuple[str, str], ...] = (),
    timeout: float = LAB_COMMAND_SECONDS,
    client: KubeClient | None = None,
    clock: Clock = time.monotonic,
) -> ServiceOutcome:
    """Run a resolved check in the lab *service_id*'s workspace container; the answer text, never an exception.

    The command runs in the lab's own copy of the checkout (the files as they were when the lab started), next to
    the lab's services on the pod's ``localhost``: what a check container cannot reach (it has no route to a lab
    pod). Only a lab started from the same checkout runs it.
    """
    found = await find(workspace, service_id)
    if found is None:
        return ServiceOutcome(
            text=f"Error: no lab {service_id!r} is running for this workspace; sandbox_start_service starts one.",
            refused=True,
        )
    key, held = found
    lab_checkout = key.rsplit(":", 1)[0]
    if lab_checkout != (checkout or "."):
        return ServiceOutcome(
            text=f"Error: lab {held['pod']} runs checkout {lab_checkout!r}, not {checkout or '.'!r}: run the check "
            "with that checkout, or start the service from this one.",
            refused=True,
        )
    budget = max(1.0, min(float(timeout), LAB_COMMAND_SECONDS))
    try:
        client = client or KubeClient()
        await _touch(client, held["pod"])
        started = clock()
        result = await client.exec(held["pod"], WORKSPACE_CONTAINER, lab_command_line(cwd, argv, env), timeout=budget)
    except KubeError as exc:
        if exc.status == 404:
            await _remember(workspace, key, None)
            return ServiceOutcome(text=f"{key}: gone (its pod ended); nothing ran.", refused=True)
        return ServiceOutcome(text=f"Error: {label} could not run in lab {held['pod']}: {exc}", refused=True)
    seconds = clock() - started
    lines = [
        f"{label}: exit {result.exit_code} in {seconds:.1f} s, in lab {held['pod']} ({key}), "
        f"cwd {cwd or '.'} of the lab's copy of the checkout (the files as of the lab's start).",
    ]
    if result.message and result.exit_code != 0:
        lines.append(f"status: {result.message}")
    lines.append("== stdout ==")
    lines.append(_bounded(result.stdout, LAB_COMMAND_STDOUT_CHARS) or "(empty)")
    if result.stderr:
        lines.append("== stderr ==")
        lines.append(_bounded(result.stderr, LAB_COMMAND_STDERR_CHARS))
    return ServiceOutcome(text="\n".join(lines), service_id=held["pod"], ready=result.exit_code == 0)


async def stop_lab(workspace: WorkspaceSession, service_id: str, *, client: KubeClient | None = None) -> ServiceOutcome:
    found = await find(workspace, service_id)
    if found is None:
        return ServiceOutcome(text=f"Error: no lab {service_id!r} is running for this workspace.", refused=True)
    key = found[0]
    # Asked by pod name: stop that pod only, never a replacement recorded under the key meanwhile.
    requested_pod = found[1].get("pod") if service_id != key else None
    client = client or KubeClient()
    # The lock of start_lab's adoption: the record read here is the one an adoption in flight completed.
    async with workspace.lock(_lock_name(key)):
        held = (await _held(workspace)).get(key)
        if not held or not held.get("pod") or (requested_pod is not None and held["pod"] != requested_pod):
            return ServiceOutcome(text=f"Error: no lab {service_id!r} is running for this workspace.", refused=True)
        note, failure = await _delete_for_stop(client, held["pod"])
        if failure:
            # The pod may still run: keep its record, so status, logs and a second stop still find it.
            return ServiceOutcome(
                text=f"Error: {key} was not stopped (service_id {held['pod']}): {failure}. Call sandbox_stop_service again.",
                service_id=held["pod"],
                refused=True,
            )
        await _drop_pod(client, held, delete=False)
        await _remember(workspace, key, None)
    return ServiceOutcome(text=f"{key}: {note} (service_id {held['pod']}).", service_id=held["pod"])


async def _delete_for_stop(client: KubeClient, pod_name: str) -> tuple[str, str]:
    """Delete *pod_name* for a stop, retried once: ``(note, "")`` when it is gone or going, ``("", why)`` when it may run.

    A delete the API server did not answer (a timeout, a reset under load) may still have landed, so the pod is
    read before the stop is called failed: absent or terminating, the stop holds.
    """
    error: KubeError | None = None
    for _attempt in range(_STOP_ATTEMPTS):
        try:
            return ("stopped" if await client.delete_pod(pod_name) else "already gone"), ""
        except KubeError as exc:
            error = exc
    try:
        pod = await client.get_pod(pod_name)
    except KubeError as exc:
        return "", f"the API server answered {error}; its state could not be read ({exc})"
    if pod is None:
        return "already gone", ""
    if (pod.get("metadata") or {}).get("deletionTimestamp"):
        return "stopping (its pod is terminating)", ""
    return "", f"the API server answered {error}; its pod is still {(pod.get('status') or {}).get('phase', 'present')}"


def _parse(moment: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(moment)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def reap(*, client: KubeClient | None = None, now: datetime | None = None) -> list[str]:
    """Hygiene, never a limit: delete the lab pods that ended, and those unused for ``AGENTIC_LABS_IDLE_HOURS``."""
    from app.core.config import settings

    if not labs_enabled():
        return []
    client = client or KubeClient()
    moment = now or datetime.now(UTC)
    idle_hours = int(settings.agentic_labs_idle_hours)
    deleted = []
    for pod in await client.list_pods(f"{LABEL_MANAGED}={MANAGED_BY}"):
        meta = pod.get("metadata") or {}
        name = meta.get("name") or ""
        phase = (pod.get("status") or {}).get("phase")
        last = _parse((meta.get("annotations") or {}).get(ANNOTATION_LAST_USED, ""))
        idle = idle_hours > 0 and last is not None and moment - last > timedelta(hours=idle_hours)
        if name and (phase in {"Succeeded", "Failed"} or idle):
            try:
                await client.delete_pod(name)
            except KubeError:
                continue
            deleted.append(name)
            await model_keys.revoke((meta.get("annotations") or {}).get(ANNOTATION_MODEL_KEY, ""))
            pod_ip = (pod.get("status") or {}).get("podIP") or ""
            for container in (pod.get("spec") or {}).get("containers") or []:
                for port in container.get("ports") or []:
                    if pod_ip and port.get("containerPort"):
                        await preview.unregister_origin(pod_ip, int(port["containerPort"]), owner=name)
    return deleted


async def reap_logged() -> None:
    """:func:`reap` from the hourly sweep: what it deleted is logged, an API server that does not answer too."""
    try:
        deleted = await reap()
    except KubeError as exc:
        logger.warning("Agentic labs sweep: the API server did not answer: %s", exc)
        return
    if deleted:
        logger.info("Agentic labs sweep: deleted %d lab pod(s): %s", len(deleted), ", ".join(deleted))
