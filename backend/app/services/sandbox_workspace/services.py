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

"""Start a service a checkout declares, in an OpenSandbox container, behind a preview URL.

A checkout may declare in ``.xtm-one/checks.toml`` how it runs, as well as how
it is checked: ``[service.<name>]`` (:func:`checks.load_services`). This module
turns ``sandbox_start_service(checkout, service)`` into a container of the
service's image with the workspace copied in, the service and the services it
depends on started as background commands (their output appended to a log file
in the container), each one waited for on its port, and the port resolved to a
preview URL through the OpenSandbox server (``Sandbox.get_endpoint``; behind the
server proxy when ``OPENSANDBOX_USE_SERVER_PROXY`` is on). The container is
kept per workspace (``kernel_state["services"]``), found again by id from any
replica, and dies at its lifetime (renewed by every status or logs call) when
nothing stops it before.

Generic on purpose (invariant 18): what a service runs, on which image and
port, is the repository's declaration; no product is named here. The container
has the network policy of every sandbox (default deny, the operator's egress
allow-list) and no credential; the headers the server asks for on a preview
request are never returned to the agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shlex
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from app.core.utils import format_exception_message
from app.services.sandbox_workspace import audit, preview, remote
from app.services.sandbox_workspace.checkout import (
    RECORD_NAMES,
    CheckoutRecord,
    CheckoutRecordError,
    is_record_name,
    read_record_bytes,
    record_paths,
)
from app.services.sandbox_workspace.checks import (
    CHECKS_FILE,
    ChecksError,
    ServiceSpec,
    describe_services,
    load_services,
    service_chain,
)
from app.services.sandbox_workspace.paths import WorkspacePathError, display, is_under, normalize
from app.services.sandbox_workspace.session import WorkspaceSession

logger = logging.getLogger(__name__)

#: What the readiness wait sleeps with and reads the time from (stand-ins in tests).
Sleep = Callable[[float], Awaitable[object]]
Clock = Callable[[], float]
#: What the workspace record keeps of a started service.
Held = dict[str, Any]

#: The image key a service runs on when it names none.
DEFAULT_IMAGE_KEY = "python"
#: The container directory the services' logs are written to.
LOG_DIR = "/tmp/.xtm/services"  # noqa: S108 - the container's own directory
#: Seconds between two readiness probes, at first and at most.
_PROBE_START = 1.0
_PROBE_MAX = 5.0
#: Lines and characters a logs call returns, by default and at most.
DEFAULT_LOG_LINES = 200
MAX_LOG_LINES = 2000
MAX_LOG_CHARS = 60_000
#: Lines a filtered logs call scans per service (the newest ones) before it keeps ``lines`` matches.
MAX_LOG_SCAN_LINES = 20_000
#: Characters a ``pattern`` may hold.
MAX_LOG_PATTERN_CHARS = 500
#: Characters one log line keeps; the rest is cut and counted (a one-line JSON stack trace).
MAX_LOG_LINE_CHARS = 1_000
#: Times are masked when two log lines are compared for the repeat fold: ISO dates and times, clock
#: times and epoch stamps of 10 digits or more; any other digit (a count, a line number) keeps lines apart.
_TIMES = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?|\d{2}:\d{2}:\d{2}(?:[.,]\d+)?|\b\d{10,}\b"
)
#: Variables every service gets on top of the container's own and its declaration.
SERVICE_ENV = {
    "CI": "1",
    "NO_COLOR": "1",
    "TERM": "dumb",
    "PYTHONUNBUFFERED": "1",
    "HOME": "/tmp",  # noqa: S108 - the container's own writable directory
}


@dataclass(frozen=True)
class ResolvedService:
    """A service bound to the workspace: what starts, in which order, where and on which image."""

    checkout: str
    name: str
    #: The service and what it depends on, dependencies first.
    chain: tuple[ServiceSpec, ...]
    image: str

    @property
    def spec(self) -> ServiceSpec:
        return self.chain[-1]

    @property
    def key(self) -> str:
        return f"{self.checkout or '.'}:{self.name}"

    @property
    def lifetime(self) -> int:
        """The started service's own lifetime: its dependencies live in its container."""
        return self.spec.lifetime


@dataclass
class ServiceOutcome:
    """What a service call did: its text for the tool and the facts behind it."""

    text: str
    refused: bool = False
    service_id: str = ""
    preview_url: str = ""
    ready: bool = False
    #: Not ready yet and not failed: the start answered before its readiness window ended (a lab's answer budget).
    starting: bool = False
    seconds: float = 0.0
    started: list[str] = field(default_factory=list)


def _join(prefix: str, rel: str) -> str:
    return normalize(f"{prefix}/{rel}" if prefix and rel else (prefix or rel), allow_root=True)


def log_path(name: str) -> str:
    return f"{LOG_DIR}/{name}.log"


async def _declared(session: WorkspaceSession, checkout: str) -> tuple[str, dict[str, ServiceSpec]]:
    try:
        root = normalize(checkout or "", allow_root=True)
    except WorkspacePathError as exc:
        raise ChecksError(str(exc)) from exc
    path = _join(root, CHECKS_FILE)
    try:
        _entry, raw = await session.get_bytes(path)
    except FileNotFoundError as exc:
        raise ChecksError(
            f"{display(path)} is not in the workspace: the checkout declares no service "
            f"(materialize_repository with {CHECKS_FILE.split('/')[0]} among sub_paths, or the whole tree)."
        ) from exc
    return root, load_services(raw.decode("utf-8", errors="replace"))


async def _checkout_record(session: WorkspaceSession, root: str) -> CheckoutRecord | None:
    """The ``materialize_repository`` record of the checkout at *root*, or ``None`` (absent or unreadable)."""
    try:
        _entry, data = await read_record_bytes(session, root)
    except FileNotFoundError:
        return None
    try:
        return CheckoutRecord.from_json(data, path=root)
    except CheckoutRecordError:
        return None


async def _missing_needs(session: WorkspaceSession, root: str, chain: list[ServiceSpec]) -> list[str]:
    """The paths the services of *chain* declare in ``needs`` that the checkout at *root* does not hold.

    With a checkout record, a need is held when the record's ``sub_paths``
    cover it (the whole tree when none) or the record lists it as a file: a
    narrow checkout keeps companion files outside its sub_paths so its checks
    are detected (``frontend/scripts/*.mjs``), and those files never make the
    directory above them present. Without a record (a tree the agent wrote),
    a need is held when a workspace file lies at or under it. A need the
    record does not cover is still held when it lies in a checkout of its
    own (a nested ``.xtm-checkout.json`` at it or above it, inside *root*:
    another repository materialized into this one, ``lab-chatbot/``) that
    holds a file at or under it.
    """
    wanted: list[str] = []
    for spec in chain:
        wanted.extend(need for need in spec.needs if need not in wanted)
    if not wanted:
        return []
    record = await _checkout_record(session, root)
    if record is not None:
        wanted = [need for need in wanted if not (record.covers(need) or need in record.files)]
        if not wanted:
            return []
    held = [path for path in await session.entries() if is_under(path, root)]
    if record is None:
        return [need for need in wanted if not any(is_under(path, _join(root, need)) for path in held)]
    own = set(record_paths(root))
    nested = {path for path in held if is_record_name(path) and path not in own}
    return [need for need in wanted if not _held_by_nested_checkout(root, need, held, nested)]


def _held_by_nested_checkout(root: str, need: str, held: list[str], nested: set[str]) -> bool:
    """Whether *need* lies in a nested checkout under *root* whose tree holds a file at or under it."""
    parts = need.split("/")
    if not any(
        _join(root, "/".join([*parts[:depth], name])) in nested
        for depth in range(1, len(parts) + 1)
        for name in RECORD_NAMES
    ):
        return False
    target = _join(root, need)
    return any(is_under(path, target) and path not in nested for path in held)


async def list_declared(session: WorkspaceSession, *, checkout: str) -> str:
    """The services the checkout at *checkout* declares, one line each: the answer of a start that names none.

    :class:`ChecksError` only when the checkout holds no readable ``checks.toml``.
    """
    root, specs = await _declared(session, checkout)
    where = display(_join(root, CHECKS_FILE))
    if not specs:
        return f"{where} declares no service."
    return f"{len(specs)} service(s) declared in {where} (sandbox_start_service with service=NAME starts one):\n{describe_services(specs)}"


def _lab_image_service_refusal(specs: dict[str, ServiceSpec], service: str, image: str) -> str:
    """Why *service*, on the ``AGENTIC_LABS_IMAGES`` key *image*, cannot be started itself, and what can (#1371).

    A lab runs the started service on the workspace image and only a dependency on a product image of its own,
    so the refusal names the declared services that depend on *service* rather than an image setting to change.
    """
    dependants = sorted(name for name, spec in specs.items() if service in spec.depends_on)
    start = (
        f"Start a service that depends on it instead: {', '.join(dependants)}."
        if dependants
        else "No declared service depends on it: declare one on a workspace image with depends_on naming it."
    )
    return (
        f"service {service!r} runs on the product image key {image!r} (AGENTIC_LABS_IMAGES): a lab runs it only as a "
        f"dependency in a container of its own, never as the started service. {start} Nothing was started."
    )


async def resolve(
    session: WorkspaceSession,
    *,
    checkout: str,
    service: str,
    known_images: set[str] | frozenset[str],
    lab_images: set[str] | frozenset[str] = frozenset(),
) -> ResolvedService:
    """The service a call asks for, bound to *session*; :class:`ChecksError` when it cannot start.

    *lab_images* (an agentic lab only, ``AGENTIC_LABS_IMAGES``) are the image keys a dependency may run on in a
    container of its own: an OpenCTI, a search engine or a broker next to the checkout's own service.
    """
    root, specs = await _declared(session, checkout)
    if not service:
        raise ChecksError(f"service names a service declared in {CHECKS_FILE} of the checkout. Declared:\n{describe_services(specs)}")
    if service not in specs:
        raise ChecksError(f"{service!r} is not declared in {display(_join(root, CHECKS_FILE))}. Declared:\n{describe_services(specs)}")
    chain = service_chain(specs, service, own_images=frozenset(lab_images))
    image = chain[-1].image or DEFAULT_IMAGE_KEY
    if image not in known_images and image in lab_images:
        raise ChecksError(_lab_image_service_refusal(specs, service, image))
    if image not in known_images:
        raise ChecksError(
            f"service {service!r} runs on image key {image!r}, which OPENSANDBOX_IMAGES does not configure "
            f"(configured: {', '.join(sorted(known_images)) or 'none'})."
        )
    missing = await _missing_needs(session, root, chain)
    if missing:
        listed = ", ".join(missing)
        adds = ", ".join(f'"{need}"' for need in missing)
        raise ChecksError(
            f"service {service!r} needs {listed} in the checkout, which {display(root)} does not hold "
            f"(a narrow checkout keeps only its sub_paths). Nothing was started. Grow the checkout first: "
            f'materialize_repository(path="{root}", add_paths=[{adds}]) with the same provider and repository, '
            "then start the service again."
        )
    return ResolvedService(checkout=root, name=service, chain=tuple(chain), image=image)


def _default_api(user_id: uuid.UUID | None) -> Any:
    from opensandbox import Sandbox

    from app.core.config import settings
    from app.services._sandbox_selector import _build_opensandbox_config, _create_opensandbox
    from app.services.sandbox_workspace.paths import WORKSPACE_ENV
    from app.services.sandbox_workspace.registry_mirror import command_egress, command_env

    async def _connect(sandbox_id: str) -> Any:
        return await Sandbox.connect(sandbox_id, connection_config=_build_opensandbox_config())

    async def _create(image_key: str, lifetime: float) -> Any:
        return await _create_opensandbox(
            image_key=image_key,
            timeout_seconds=int(lifetime),
            kind_label="sandbox-service",
            # Counted and logged as the checks are: one kind of command container.
            usage_type="sandbox_command",
            user_id=user_id,
            # Package installs reach the read-only registry mirror only, when one is set.
            egress_allow_list=command_egress(),
            resource=settings.sandbox_resources(image_key),
            env={**command_env(), WORKSPACE_ENV: remote.REMOTE_WORKSPACE},
        )

    return SimpleNamespace(connect=_connect, create=_create)


async def _held(workspace: WorkspaceSession) -> dict[str, Held]:
    record = await workspace.record(create=False)
    if record is None or not record.kernel_state:
        return {}
    return dict((record.kernel_state or {}).get("services") or {})


async def _remember(workspace: WorkspaceSession, key: str, value: Held | None) -> None:
    # Read and written under the kernel state's lock, on the state as stored now: a service started in parallel
    # (or a lab, a command container) never loses its record to this write.
    def change(state: dict) -> dict | None:
        services = dict(state.get("services") or {})
        if value is None:
            if services.pop(key, None) is None:
                return None
        else:
            services[key] = value
        return {**state, "services": services}

    await workspace.update_kernel_state(change)


def _probe_command(spec: ServiceSpec) -> str:
    """A command that exits 0 once *spec* answers on its port (HTTP below 500 when it has a ready_path)."""
    if spec.ready_path:
        url = f"http://127.0.0.1:{spec.port}{spec.ready_path}"
        code = (
            "import sys,urllib.request,urllib.error\n"
            "try:\n"
            f" urllib.request.urlopen({url!r}, timeout=3)\n"
            "except urllib.error.HTTPError as e:\n"
            " sys.exit(0 if e.code < 500 else 1)\n"
            "except Exception:\n"
            " sys.exit(1)\n"
        )
    else:
        code = (
            "import socket,sys\n"
            "try:\n"
            f" socket.create_connection(('127.0.0.1', {spec.port}), 3).close()\n"
            "except Exception:\n"
            " sys.exit(1)\n"
        )
    # ``python``: every sandbox image runs the workspace helper with it (remote.py).
    return f"python -c {shlex.quote(code)}"


async def _answers(sandbox: Any, spec: ServiceSpec) -> bool:
    from opensandbox.models.execd import RunCommandOpts

    try:
        result = await sandbox.commands.run(
            _probe_command(spec), opts=RunCommandOpts(working_directory="/tmp", timeout=timedelta(seconds=10))  # noqa: S108
        )
    except Exception:
        return False
    return getattr(result, "exit_code", 1) == 0


async def _logged(sandbox: Any, spec: ServiceSpec) -> bool:
    """Whether *spec*'s output holds its ``ready_log`` text (always, when it declares none)."""
    if not spec.ready_log:
        return True
    try:
        raw = await sandbox.files.read_bytes(log_path(spec.name))
    except Exception:
        return False
    return spec.ready_log.encode("utf-8") in raw


def start_window_left(held: Held, window: float) -> float:
    """Seconds left of the *window* a start recorded in *held* (``started_at``) had to become ready, ``0.0`` past it.

    A record without ``ready_seconds`` is a start that never answered ready: while its window lasts, it may still be
    starting (its call cut by a run resume), so a second start waits what is left of it rather than answering early.
    """
    try:
        started = datetime.fromisoformat(str(held["started_at"]))
    except (KeyError, ValueError):
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0.0, window - (datetime.now(UTC) - started).total_seconds())


async def _wait_ready(
    sandbox: Any, spec: ServiceSpec, *, sleep: Sleep = asyncio.sleep, clock: Clock = time.monotonic,
    timeout: float | None = None,
) -> bool:
    """Whether *spec* answers on its port, and has logged its ``ready_log`` when it declares one, within its timeout.

    *timeout* replaces the declared one (``0`` probes once).
    """
    deadline = clock() + (spec.timeout if timeout is None else timeout)
    delay = _PROBE_START
    while True:
        if await _answers(sandbox, spec) and await _logged(sandbox, spec):
            return True
        if clock() >= deadline:
            return False
        await sleep(min(delay, max(0.0, deadline - clock())))
        delay = min(delay * 2, _PROBE_MAX)


async def _launch(sandbox: Any, resolved: ResolvedService, spec: ServiceSpec) -> None:
    from opensandbox.models.execd import RunCommandOpts

    from app.services.sandbox_workspace.registry_mirror import command_env, write_maven_settings

    await write_maven_settings(sandbox)
    where = _join(resolved.checkout, spec.cwd)
    cwd = f"{remote.REMOTE_WORKSPACE}/{where}" if where else remote.REMOTE_WORKSPACE
    # The registry mirror's variables on the process itself: a pooled container does not pass the create's env on.
    env = {**command_env(), **SERVICE_ENV, **dict(spec.env)}
    command = f"{shlex.join(spec.argv)} >> {shlex.quote(log_path(spec.name))} 2>&1"
    await sandbox.commands.run(command, opts=RunCommandOpts(background=True, working_directory=cwd, envs=env))


async def preview_url(sandbox: Any, port: int) -> str:
    """The server proxy URL of *port* of *sandbox*, once the server resolves an endpoint for it.

    ``get_endpoint`` raises while the port is not published; the URL itself is the
    server's proxy route on the configured origin (``OPENSANDBOX_PROTOCOL`` and
    ``OPENSANDBOX_DOMAIN``), the one :mod:`preview` admits, never the endpoint's
    pod address (unreachable from the platform's browser) nor its headers.
    """
    from app.core.config import settings

    await sandbox.get_endpoint(port)
    domain = (settings.opensandbox_domain or "").strip().rstrip("/")
    return f"{settings.opensandbox_protocol or 'http'}://{domain}{preview.path_of(str(sandbox.id), port)}/"


async def start_service(
    resolved: ResolvedService,
    *,
    workspace: WorkspaceSession,
    user_id: uuid.UUID | None = None,
    quota_user_id: uuid.UUID | None = None,
    db: Any = None,
    sandbox_api: Any = None,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> ServiceOutcome:
    """Start *resolved* in a container of its own; the outcome, never an exception.

    *sandbox_api* stands in for the SDK (``connect(id)``, ``create(image_key, lifetime)``), *sleep* and
    *clock* for the readiness wait's, in tests.
    """
    from app.services.quota import (
        QUOTA_EXCEEDED_MESSAGE,
        QUOTA_UNAVAILABLE_MESSAGE,
        QuotaCheckUnavailableError,
        QuotaExceededError,
        enforce_combined_sandbox_quota,
        increment_sandbox_usage,
    )
    from app.services.sandbox_workspace.commands import IGNORED_PATTERNS

    held = (await _held(workspace)).get(resolved.key)
    api = sandbox_api if sandbox_api is not None else _default_api(user_id)
    if held and held.get("sandbox_id"):
        try:
            sandbox = await api.connect(held["sandbox_id"])
        except Exception:
            await _remember(workspace, resolved.key, None)
        else:
            try:
                # A start that never answered ready (cut by a run resume) left no ``ready_seconds``: its service may
                # still start or seed, so it gets the readiness wait of a fresh start (port and ``ready_log``), for
                # what is left of that start's window, before it is answered or replaced.
                if "ready_seconds" in held:
                    ready = await _answers(sandbox, resolved.spec)
                else:
                    window = float(sum(spec.timeout for spec in resolved.chain))
                    left = start_window_left(held, window)
                    ready = await _wait_ready(sandbox, resolved.spec, sleep=sleep, clock=clock, timeout=left)
                if ready:
                    await sandbox.renew(timedelta(seconds=resolved.lifetime))
                    await preview.register(held["sandbox_id"], resolved.spec.port, resolved.lifetime)
                    url = held.get("preview_url") or await preview_url(sandbox, resolved.spec.port)
                    return ServiceOutcome(
                        text=_describe(resolved, held["sandbox_id"], url, ready=True, seconds=0.0, note="already running"),
                        service_id=held["sandbox_id"],
                        preview_url=url,
                        ready=True,
                    )
                with contextlib.suppress(Exception):
                    await sandbox.kill()
                await _remember(workspace, resolved.key, None)
            finally:
                with contextlib.suppress(Exception):
                    await sandbox.close()

    effective_quota_user = quota_user_id if quota_user_id is not None else user_id
    try:
        await enforce_combined_sandbox_quota(effective_quota_user)
    except QuotaExceededError:
        return ServiceOutcome(text=QUOTA_EXCEEDED_MESSAGE, refused=True)
    except QuotaCheckUnavailableError:
        return ServiceOutcome(text=QUOTA_UNAVAILABLE_MESSAGE, refused=True)

    started = clock()
    try:
        sandbox = await api.create(resolved.image, float(resolved.lifetime))
    except Exception as exc:
        return ServiceOutcome(text=f"Error: the service container could not be created: {format_exception_message(exc)}", refused=True)
    service_id = str(sandbox.id)
    outcome = ServiceOutcome(text="", service_id=service_id)
    keep = False
    try:
        try:
            from opensandbox.models.filesystem import WriteEntry

            await remote.install_tool(sandbox)
            await remote.reconcile(sandbox, workspace, await workspace.entries(refresh=True), ignore=list(IGNORED_PATTERNS))
            await sandbox.files.create_directories([WriteEntry(path=LOG_DIR, mode=755)])
        except Exception as exc:
            outcome.text = f"Error: the workspace could not be put in the service container ({format_exception_message(exc)})."
            outcome.refused = True
            outcome.service_id = ""
            return outcome
        await _remember(
            workspace,
            resolved.key,
            {
                "sandbox_id": service_id,
                "image": resolved.image,
                "port": resolved.spec.port,
                "services": [spec.name for spec in resolved.chain],
                "lifetime": resolved.lifetime,
                "started_at": datetime.now(UTC).isoformat(),
            },
        )
        keep = True
        for spec in resolved.chain:
            try:
                await _launch(sandbox, resolved, spec)
            except Exception as exc:
                outcome.text = _failed(resolved, service_id, spec, f"could not be started ({format_exception_message(exc)})", clock() - started)
                return outcome
            outcome.started.append(spec.name)
            if not await _wait_ready(sandbox, spec, sleep=sleep, clock=clock):
                outcome.seconds = clock() - started
                logged = f" and log {spec.ready_log!r}" if spec.ready_log else ""
                outcome.text = _failed(
                    resolved, service_id, spec, f"did not answer on port {spec.port}{logged} within {spec.timeout}s", outcome.seconds
                )
                return outcome
        outcome.seconds = clock() - started
        outcome.ready = True
        try:
            outcome.preview_url = await preview_url(sandbox, resolved.spec.port)
        except Exception as exc:
            outcome.text = (
                _describe(resolved, service_id, "", ready=True, seconds=outcome.seconds)
                + f"\nThe OpenSandbox server gave no endpoint for port {resolved.spec.port}: {format_exception_message(exc)}"
            )
            return outcome
        await preview.register(service_id, resolved.spec.port, resolved.lifetime)
        held = (await _held(workspace)).get(resolved.key) or {}
        await _remember(workspace, resolved.key, {**held, "preview_url": outcome.preview_url, "ready_seconds": round(outcome.seconds, 1)})
        outcome.text = _describe(resolved, service_id, outcome.preview_url, ready=True, seconds=outcome.seconds)
        await increment_sandbox_usage(effective_quota_user, db=db)
        return outcome
    finally:
        if not keep:
            with contextlib.suppress(Exception):
                await sandbox.kill()
        with contextlib.suppress(Exception):
            await sandbox.close()


def _describe(resolved: ResolvedService, service_id: str, url: str, *, ready: bool, seconds: float, note: str = "") -> str:
    head = f"{resolved.name}: {'ready' if ready else 'not ready'}" + (f" ({note})" if note else f" in {seconds:.1f}s")
    lines = [head, f"service_id: {service_id}"]
    if url:
        lines.append(f"preview_url: {url}")
        lines.append("Open it with the browser tool or web_fetch; the platform adds what the sandbox server asks for.")
    lines.append(
        "Logs: sandbox_service_logs(service_id) ("
        + ", ".join(log_path(spec.name) for spec in resolved.chain)
        + f" in the container). It stops at sandbox_stop_service or {resolved.lifetime}s after the last call on it."
    )
    return "\n".join(lines)


def _failed(resolved: ResolvedService, service_id: str, spec: ServiceSpec, why: str, seconds: float) -> str:
    return (
        f"{resolved.name}: not ready after {seconds:.1f}s: {spec.name} {why}.\n"
        f"service_id: {service_id}\n"
        f"Read its output with sandbox_service_logs(service_id, service={spec.name!r}); stop it with sandbox_stop_service(service_id)."
    )


async def _find(workspace: WorkspaceSession, service_id: str) -> tuple[str, Held] | None:
    for key, value in (await _held(workspace)).items():
        if value.get("sandbox_id") == service_id or key == service_id:
            return key, value
    return None


def not_running(service_id: str) -> ServiceOutcome:
    """The status answer for a service_id the workspace holds no record of (a service or a lab).

    A service the run stopped a moment earlier has no record any more (a stop drops
    it), so the read-back after a stop lands here: a normal state, not an error, so
    it is never counted as a failed tool call. An empty or malformed id (blank, or
    with whitespace in it) is still a refusal.
    """
    if not service_id.strip() or any(char.isspace() for char in service_id):
        return ServiceOutcome(
            text=f"Error: {service_id!r} is not a service_id; pass the one sandbox_start_service answered.",
            refused=True,
        )
    return ServiceOutcome(text=f"{service_id}: not running (stopped or never started in this workspace).")


async def service_status(
    workspace: WorkspaceSession, service_id: str, *, sandbox_api: Any = None, user_id: uuid.UUID | None = None
) -> ServiceOutcome:
    found = await _find(workspace, service_id)
    if found is None:
        return not_running(service_id)
    key, held = found
    api = sandbox_api if sandbox_api is not None else _default_api(user_id)
    try:
        sandbox = await api.connect(held["sandbox_id"])
    except Exception as exc:
        await _remember(workspace, key, None)
        return ServiceOutcome(text=f"{key}: gone (its container ended: {format_exception_message(exc)}).")
    try:
        spec = ServiceSpec(name=key, argv=("-",), port=int(held.get("port") or 0))
        ready = await _answers(sandbox, spec)
        lifetime = int(held.get("lifetime") or 0)
        if lifetime:
            await sandbox.renew(timedelta(seconds=lifetime))
            if held.get("preview_url"):
                await preview.register(held["sandbox_id"], spec.port, lifetime)
        lines = [
            f"{key}: {'answering' if ready else 'not answering'} on port {spec.port}",
            f"service_id: {held['sandbox_id']}",
            f"started_at: {held.get('started_at', '?')}",
        ]
        if held.get("preview_url"):
            lines.append(f"preview_url: {held['preview_url']}")
        if held.get("ready_seconds") is not None:
            lines.append(f"ready after: {held['ready_seconds']}s")
        return ServiceOutcome(text="\n".join(lines), service_id=held["sandbox_id"], preview_url=held.get("preview_url", ""), ready=ready)
    finally:
        with contextlib.suppress(Exception):
            await sandbox.close()


def tail(text: str, lines: int) -> str:
    """The last *lines* lines of *text*, at most :data:`MAX_LOG_CHARS` characters."""
    kept = text.splitlines()[-lines:]
    out = "\n".join(kept)
    if len(out) > MAX_LOG_CHARS:
        out = "[... cut ...]\n" + out[-MAX_LOG_CHARS:]
    return out


def compile_log_pattern(pattern: str, *, ignore_case: bool = False) -> re.Pattern[str] | str | None:
    """The compiled *pattern* of a logs call, ``None`` without one, or the refusal text of an invalid one."""
    if not pattern:
        return None
    if len(pattern) > MAX_LOG_PATTERN_CHARS:
        return f"Error: pattern is {len(pattern)} characters, at most {MAX_LOG_PATTERN_CHARS}."
    try:
        return re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return f"Error: pattern {pattern!r} is not a valid regular expression: {exc}."


def cut_line(line: str) -> str:
    """*line* cut to :data:`MAX_LOG_LINE_CHARS` characters, the characters left out counted."""
    if len(line) <= MAX_LOG_LINE_CHARS:
        return line
    return f"{line[:MAX_LOG_LINE_CHARS]} [+{len(line) - MAX_LOG_LINE_CHARS} chars]"


def fold_repeats(lines: list[str]) -> list[tuple[str, int]]:
    """*lines* with each repeated line kept once, at its newest place, with its count.

    Two lines repeat when they are equal once their times are masked (:data:`_TIMES`), so a recurring
    error stamped with a new time each round reads once with its count.
    """
    counts: dict[str, int] = {}
    for line in lines:
        key = _TIMES.sub("T", line)
        counts[key] = counts.get(key, 0) + 1
    seen: dict[str, int] = {}
    kept: list[tuple[str, int]] = []
    for line in lines:
        key = _TIMES.sub("T", line)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == counts[key]:
            kept.append((line, counts[key]))
    return kept


def _shown(lines: list[str]) -> tuple[list[str], int]:
    """*lines* folded (:func:`fold_repeats`), each one cut (:func:`cut_line`) then followed by ``(xN)``
    when it repeats; answers them and how many lines were folded away."""
    folded = fold_repeats(lines)
    shown = [f"{cut_line(line)} (x{count})" if count > 1 else cut_line(line) for line, count in folded]
    return shown, len(lines) - len(folded)


def _folded_note(dropped: int) -> str:
    return f", {dropped} repeated line(s) folded" if dropped else ""


def log_section(name: str, source: str, text: str, lines: int, regex: re.Pattern[str] | None) -> str:
    """One service's block of a logs answer: its last *lines* lines, or its last *lines* lines matching *regex*.

    A filtered block scans the newest :data:`MAX_LOG_SCAN_LINES` lines and its heading names how many
    matched out of how many were scanned, so a caller reads a lab's few relevant lines without its banners.
    Repeated lines are folded into their newest occurrence with a ``(xN)`` count and each line keeps at
    most :data:`MAX_LOG_LINE_CHARS` characters, so a recurring one-line stack trace cannot fill the answer.
    """
    if regex is None:
        shown, dropped = _shown(text.splitlines()[-MAX_LOG_SCAN_LINES:])
        return f"== {name} (last {lines} lines of {source}{_folded_note(dropped)}) ==\n" + tail("\n".join(shown), lines)
    scanned = text.splitlines()[-MAX_LOG_SCAN_LINES:]
    matched = [line for line in scanned if regex.search(line)]
    if not matched:
        return f"== {name}: 0 of {len(scanned)} lines of {source} match {regex.pattern!r} =="
    shown, dropped = _shown(matched)
    kept = min(lines, len(shown))
    head = (
        f"== {name} ({len(matched)} of {len(scanned)} lines of {source} match {regex.pattern!r}{_folded_note(dropped)}"
    )
    head += f", last {kept} shown) ==" if kept < len(shown) else ") =="
    return head + "\n" + tail("\n".join(shown), lines)


async def service_logs(
    workspace: WorkspaceSession,
    service_id: str,
    *,
    service: str = "",
    lines: int = DEFAULT_LOG_LINES,
    pattern: str = "",
    ignore_case: bool = False,
    sandbox_api: Any = None,
    user_id: uuid.UUID | None = None,
) -> ServiceOutcome:
    regex = compile_log_pattern(pattern, ignore_case=ignore_case)
    if isinstance(regex, str):
        return ServiceOutcome(text=regex, refused=True)
    found = await _find(workspace, service_id)
    if found is None:
        return ServiceOutcome(text=f"Error: no service {service_id!r} is running for this workspace.", refused=True)
    key, held = found
    names = list(held.get("services") or [])
    if service and service not in names:
        return ServiceOutcome(text=f"Error: {service!r} is not one of {', '.join(names) or 'its services'}.", refused=True)
    lines = max(1, min(int(lines or DEFAULT_LOG_LINES), MAX_LOG_LINES))
    api = sandbox_api if sandbox_api is not None else _default_api(user_id)
    try:
        sandbox = await api.connect(held["sandbox_id"])
    except Exception as exc:
        await _remember(workspace, key, None)
        return ServiceOutcome(text=f"{key}: gone (its container ended: {format_exception_message(exc)}).")
    try:
        parts = []
        for name in [service] if service else names:
            try:
                raw = await sandbox.files.read_bytes(log_path(name))
            except Exception:
                parts.append(f"== {name}: no output yet ==")
                continue
            parts.append(log_section(name, log_path(name), raw.decode("utf-8", errors="replace"), lines, regex))
        return ServiceOutcome(text="\n".join(parts), service_id=held["sandbox_id"])
    finally:
        with contextlib.suppress(Exception):
            await sandbox.close()


async def stop_service(
    workspace: WorkspaceSession, service_id: str, *, sandbox_api: Any = None, user_id: uuid.UUID | None = None
) -> ServiceOutcome:
    found = await _find(workspace, service_id)
    if found is None:
        return ServiceOutcome(text=f"Error: no service {service_id!r} is running for this workspace.", refused=True)
    key, held = found
    api = sandbox_api if sandbox_api is not None else _default_api(user_id)
    note = "stopped"
    try:
        sandbox = await api.connect(held["sandbox_id"])
    except Exception:
        note = "already gone"
    else:
        try:
            await sandbox.kill()
        except Exception as exc:
            note = f"stop requested; the container reported {format_exception_message(exc)} (it ends at its lifetime anyway)"
        finally:
            with contextlib.suppress(Exception):
                await sandbox.close()
    await preview.unregister(held["sandbox_id"], int(held.get("port") or 0))
    await _remember(workspace, key, None)
    return ServiceOutcome(text=f"{key}: {note} (service_id {held['sandbox_id']}).", service_id=held["sandbox_id"])


async def audit_service(
    action: str, *, user_id: uuid.UUID | None, workspace_id: uuid.UUID | None, outcome: ServiceOutcome, name: str = ""
) -> None:
    await audit.record(
        "sandbox_service",
        user_id=user_id,
        workspace_id=workspace_id,
        summary=f"{action} sandbox service {name or outcome.service_id}" + (f": ready in {outcome.seconds:.1f}s" if outcome.ready and outcome.seconds else ""),
        details={"action": action, "service": name, "service_id": outcome.service_id, "ready": outcome.ready, "seconds": round(outcome.seconds, 1)},
    )
