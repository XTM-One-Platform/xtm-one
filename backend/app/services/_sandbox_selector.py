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

"""Sandbox probe and selection logic shared by custom_tool_executor and code_interpreter.

This module owns the process-wide state for sandbox availability probing
and exposes the functions used by both the custom-tool executor and the
code interpreter to determine which sandbox backend to use.

Keeping this logic here (rather than in ``custom_tool_executor``) avoids a
cross-service dependency: ``code_interpreter`` needs ``_probe_opensandbox_available``
and ``_build_opensandbox_config``, and it would be awkward for it to import
from ``custom_tool_executor``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import random
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

from opensandbox.config import ConnectionConfig

from app.core.metrics import get_or_create_counter, get_or_create_histogram

if TYPE_CHECKING:
    from opensandbox.models.sandboxes import NetworkPolicy

logger = logging.getLogger(__name__)

# Seconds every container create took, per image key and outcome (ok, timeout,
# sidecar, error) and whether it was bound to a Pool: the series that sizes the
# Pools' warm buffer (a timeout series on one key is an exhausted or cold Pool).
CREATE_SECONDS = get_or_create_histogram(
    "xtmone_sandbox_create_seconds",
    "Seconds one OpenSandbox container create attempt took, by image key, outcome and Pool binding.",
    ["image_key", "outcome", "pooled"],
    buckets=(0.5, 1, 2.5, 5, 10, 20, 30, 60, 90, 120, 180, 240, 300),
)
# Creates that left a Pool for a direct create (one per call): the Pool had no
# pod to give (``starved``) or its create timed out (``timeout``).
POOL_FALLBACKS = get_or_create_counter(
    "xtmone_sandbox_pool_fallback_total",
    "OpenSandbox creates that dropped their Pool for a direct create, by image key and reason.",
    ["image_key", "reason"],
)
# The image keys whose Pool is their network, not only their warm start: the
# toolchain Pools (helm-xtm-one ``toolchain: true``) label their pods
# ``app.kubernetes.io/component: toolchain-sandbox``, the label the registry
# egress policy selects. A direct create gets the server's own pod template
# (``component: sandbox``) and the namespace deny-all: no DNS, no package
# registry, so every Maven, cargo or dbt install fails offline. Their creates
# keep the poolRef: a starved or slow Pool is waited for, never bypassed.
POOL_BOUND_KEY_PREFIX = "toolchain-"


def is_pool_bound(image_key: str) -> bool:
    """Whether *image_key*'s sandboxes reach their network only through their Pool."""
    return image_key.startswith(POOL_BOUND_KEY_PREFIX)


# What a sandbox created with open egress (CODE_INTERPRETER_OPEN_EGRESS) never
# reaches: the cloud metadata endpoints, the cluster's and the cloud's internal
# names, and every non-public range (loopback, RFC 1918, CGNAT, link-local,
# IPv6 unique-local and link-local), so the platform's own services, its
# database and Redis stay out of reach. Deny rules come before the default
# allow and a deny always wins over an allow in the egress sidecar.
OPEN_EGRESS_DENY: tuple[str, ...] = (
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.azure.com",
    "*.internal",
    "*.local",
    "*.localdomain",
    "*.svc",
    "*.cluster.local",
    "localhost",
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "::1/128",
    "fc00::/7",
    "fd00:ec2::254",
    "fe80::/10",
)


def egress_policy(egress_allow_list: Iterable[str], *, open_egress: bool = False) -> NetworkPolicy:
    """The OpenSandbox network policy of a sandbox.

    Default deny with *egress_allow_list* allowed; with *open_egress*, default
    allow with :data:`OPEN_EGRESS_DENY` denied first (the allow-list adds nothing
    then, every public host being allowed).
    """
    from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule

    if open_egress:
        return NetworkPolicy(
            defaultAction="allow", egress=[NetworkRule(action="deny", target=t) for t in OPEN_EGRESS_DENY]
        )
    egress = [NetworkRule(action="allow", target=t) for t in egress_allow_list]
    return NetworkPolicy(defaultAction="deny", egress=egress or None)


# Characters of the server's own reason a create failure carries.
_SERVER_DETAIL_CHARS = 400

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OPENSANDBOX_PROBE_TIMEOUT = 5  # seconds for one TCP connect probe
_OPENSANDBOX_PROBE_RETRY_INTERVAL = 60.0

# Every sandbox is created under a default-deny network policy, so the
# OpenSandbox server starts an egress sidecar before the sandbox itself. On a
# cold Docker host (a dev machine after idle) that sidecar can lose its
# startup race - "Egress sidecar container failed to start." - usually a
# transient failure a second attempt clears (the server says the same for a
# host that can never start it). Retried a bounded number of times, after a
# pause drawn between 1x and 2x the delay so calls that failed together do not
# retry together; the policy itself is never dropped (that would give the
# sandbox open egress).
_EGRESS_SIDECAR_RETRIES = 1
_EGRESS_SIDECAR_RETRY_DELAY_SECONDS = 1.5
# The server's own sentence for that failure (an HTTP 500), lowercased.
_EGRESS_SIDECAR_START_FAILURE = "egress sidecar container failed to start"

# The OpenSandbox server answers HTTP 504 (code K8S_POD_READY_TIMEOUT) when the
# sandbox pod is not Running with an IP within its own
# ``[kubernetes] sandbox_create_timeout_seconds`` (60 s by default), and deletes
# the workload. That happens on a cold pod: the Pool's warm pods are taken and
# the next one pulls a large image (the dev and toolchain images) and runs the
# execd init first. The image is on the node afterwards, so one more attempt
# usually starts in seconds. Retried once, under the identical policy.
_CREATE_TIMEOUT_RETRIES = 1
_CREATE_TIMEOUT_RETRY_DELAY_SECONDS = 2.0
_CREATE_TIMEOUT_STATUS = 504
# A caller that does not hold a tool call open while it creates (the workspace
# command container, created as its own task past the call) passes a window
# instead (``retry_timeouts_for``): a 504 is then retried until that window,
# the pause growing from the delay above to this cap, so a cold Pool that
# needs several minutes to pull a large image ends in a container rather than
# in an error after two blind attempts.
_CREATE_TIMEOUT_RETRY_DELAY_MAX_SECONDS = 15.0

# The pool state read from the OpenSandbox server (``sandbox_pool_state``):
# pages of this size, at most this many, within this many seconds in all.
_POOL_STATE_PAGE_SIZE = 200
_POOL_STATE_MAX_PAGES = 5
_POOL_STATE_TIMEOUT_SECONDS = 5.0

# Native thread pools (OpenBLAS, which numpy and scipy bundle; OpenMP; MKL)
# size themselves on the HOST's CPU count, not on the container's CPU quota:
# on a 64-core host ``import numpy`` starts 64 threads and hits the sandbox's
# process limit (``pids_limit``). Every OpenSandbox container gets these
# variables set to its own CPU allocation, so the thread count no longer
# depends on the host. The sandbox wrappers' environment scrubs keep them.
NATIVE_THREAD_ENV_KEYS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")


def _cpu_thread_count(cpu: object) -> int:
    """Whole threads for a container CPU value (``"1"``, ``"500m"``, ``"2.5"``), at least 1.

    A value that does not parse counts as one CPU: the cap then errs on the
    side of fewer threads, never more.
    """
    text = str(cpu or "").strip().lower()
    try:
        cores = float(text[:-1]) / 1000 if text.endswith("m") else float(text)
    except ValueError:
        return 1
    if not math.isfinite(cores) or cores <= 1:
        return 1
    return math.ceil(cores)


def native_thread_env(cpu: object) -> dict[str, str]:
    """The native thread-pool caps for a container with *cpu* CPUs (see ``NATIVE_THREAD_ENV_KEYS``)."""
    threads = str(_cpu_thread_count(cpu))
    return dict.fromkeys(NATIVE_THREAD_ENV_KEYS, threads)


class SandboxUnavailableError(RuntimeError):
    """The sandbox infrastructure could not start a container.

    Raised instead of the raw SDK error when the egress sidecar still fails
    after the retries, so the message the agent relays says the platform had a
    problem rather than that the user's tool is broken.
    """


def _is_egress_sidecar_failure(exc: BaseException) -> bool:
    """True for the OpenSandbox server's egress-sidecar start failure only.

    Keyed on the server's sentence ("Egress sidecar container failed to
    start."), not on the words "egress" or "start": an invalid egress rule, an
    egress misconfiguration or any request the server refused (a 4xx) is
    permanent and must fail on the first attempt.
    """
    from opensandbox.exceptions.sandbox import SandboxApiException

    if not isinstance(exc, SandboxApiException):
        return False
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and 400 <= status_code < 500:
        return False
    detail = getattr(getattr(exc, "error", None), "message", None) or ""
    return _EGRESS_SIDECAR_START_FAILURE in f"{exc} {detail}".lower()


def _is_create_timeout(exc: BaseException) -> bool:
    """True when the server (or a gateway in front of it) gave up waiting for the sandbox to be ready (HTTP 504)."""
    from opensandbox.exceptions.sandbox import SandboxApiException

    return isinstance(exc, SandboxApiException) and getattr(exc, "status_code", None) == _CREATE_TIMEOUT_STATUS


def _server_detail(exc: BaseException) -> str:
    """The OpenSandbox server's own reason for a failed create, or ``""``.

    The server answers ``{"code": ..., "message": ...}`` (at the top level or
    under FastAPI's ``detail``); the SDK keeps it in ``error.message`` when it
    parsed it, else only in ``response_body``. For a pod-ready timeout the
    message names the last pod state and the seconds waited, which tells a cold
    image pull from a Pool with no capacity left.
    """
    error = getattr(exc, "error", None)
    code = str(getattr(error, "code", "") or "")
    message = str(getattr(error, "message", "") or "")
    body = getattr(exc, "response_body", None)
    if not message and body:
        text = body.decode("utf-8", errors="replace") if isinstance(body, bytes | bytearray) else str(body)
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            inner = parsed.get("detail") if isinstance(parsed.get("detail"), dict) else parsed
            code = str(inner.get("code") or code)
            message = str(inner.get("message") or "")
        else:
            message = text.strip()
    if not message:
        return ""
    shown = f"{code}: {message}" if code and code not in {"UNEXPECTED_RESPONSE", "INTERNAL_UNKNOWN_ERROR"} else message
    return shown[:_SERVER_DETAIL_CHARS]


def _create_timeout_message(image_key: str, attempts: list[float], seconds: float, exc: BaseException) -> str:
    """What a persistent create timeout tells the agent: the stage, the figures, the server's reason, what to do."""
    each = ", ".join(f"{s:.0f} s" for s in attempts)
    detail = _server_detail(exc)
    reason = f" The server said: {detail}." if detail else ""
    return (
        f"stage create: the sandbox container for image key {image_key!r} was not ready in time "
        f"({len(attempts)} attempts of {each}, {seconds:.0f} s in all; the OpenSandbox server answered HTTP 504: "
        "the pod was not Running within its [kubernetes] sandbox_create_timeout_seconds)."
        f"{reason} The workspace had not been sent yet, so its size is not the cause: the pod could not start "
        "(a cold node pulling the image, or no capacity left in the Pool bound to this image key). This is a "
        "platform problem, not a defect of the tool: retry the call once in a minute; if it fails again, report "
        "that an administrator should raise the Pool's warm buffer (bufferMin/bufferMax) and poolMax for this "
        f"image key, or the server's sandbox_create_timeout_seconds. Details: {exc}"
    )


def _create_retry_delay(timeouts: int) -> float:
    """The pause before retry number *timeouts* (1-based) of a 504: doubling from the delay, capped, jittered."""
    base = min(_CREATE_TIMEOUT_RETRY_DELAY_MAX_SECONDS, _CREATE_TIMEOUT_RETRY_DELAY_SECONDS * 2 ** max(0, timeouts - 1))
    return base * random.uniform(1.0, 2.0)  # noqa: S311 - jitter


def _retry_timeout(timeouts: int, started: float, window: float | None) -> bool:
    """Whether the create should try again after its *timeouts*-th 504.

    Without a *window*, ``_CREATE_TIMEOUT_RETRIES`` times; with one, while the
    next pause still ends inside the window that began at *started*.
    """
    if window is None:
        return timeouts <= _CREATE_TIMEOUT_RETRIES
    pause_cap = _CREATE_TIMEOUT_RETRY_DELAY_MAX_SECONDS * 2
    return time.monotonic() - started + pause_cap < window


def _observe_create(image_key: str, outcome: str, pooled: bool, seconds: float) -> None:
    CREATE_SECONDS.labels(image_key=image_key, outcome=outcome, pooled="true" if pooled else "false").observe(
        max(0.0, seconds)
    )


# ---------------------------------------------------------------------------
# Pool state
# ---------------------------------------------------------------------------


def _pod_phase(info: object) -> str:
    """``ready``, ``pulling`` (a pending pod pulling its image), ``creating`` (any other pending state) or ``other``."""
    status = getattr(info, "status", None)
    state = str(getattr(status, "state", "") or "").lower()
    if state == "running":
        return "ready"
    if state in {"pending", "creating", "allocating", "starting"}:
        words = f"{getattr(status, 'reason', '') or ''} {getattr(status, 'message', '') or ''}".lower()
        return "pulling" if "pull" in words else "creating"
    return "other"


def _pod_image_key(info: object, keys_by_image: dict[str, str]) -> str:
    image = getattr(getattr(info, "image", None), "image", None)
    return keys_by_image.get(str(image or ""), "other")


async def sandbox_pool_state(image_key: str | None = None, *, manager: object = None) -> dict[str, dict[str, int]] | None:
    """The OpenSandbox pods per image key: ``{key: {"ready", "creating", "pulling", "other"}}``.

    Read from the server's sandbox list (``SandboxManager.list_sandbox_infos``),
    an image mapped back to its key through ``OPENSANDBOX_IMAGES`` (a pod of
    another image counts under ``"other"``). *image_key* narrows the answer to
    that key (zero counts when it has no pod). Fail-open: ``None`` when the
    server is not configured, not reachable or does not answer within
    ``_POOL_STATE_TIMEOUT_SECONDS``; never raises. *manager* stands in for the
    SDK's manager in tests.
    """
    from app.core.config import settings

    if manager is None and not await _probe_opensandbox_available():
        return None
    keys_by_image = {image: key for key, image in settings.opensandbox_images.items()}

    async def _read(mgr) -> list:
        from opensandbox.models.sandboxes import SandboxFilter

        infos: list = []
        for page in range(1, _POOL_STATE_MAX_PAGES + 1):
            paged = await mgr.list_sandbox_infos(SandboxFilter(page=page, page_size=_POOL_STATE_PAGE_SIZE))
            infos.extend(getattr(paged, "sandbox_infos", None) or [])
            if not getattr(getattr(paged, "pagination", None), "has_next_page", False):
                break
        return infos

    owned = None
    try:
        if manager is None:
            from opensandbox.manager import SandboxManager

            owned = await SandboxManager.create(connection_config=_build_opensandbox_config())
        infos = await asyncio.wait_for(_read(manager or owned), timeout=_POOL_STATE_TIMEOUT_SECONDS)
    except Exception as exc:  # fail-open: the state is a hint, never a failure of the caller
        logger.info("OpenSandbox pool state not read: %s", exc)
        return None
    finally:
        if owned is not None:
            with contextlib.suppress(Exception):
                await owned.close()
    state: dict[str, dict[str, int]] = {}
    if image_key is not None:
        state[image_key] = {"ready": 0, "creating": 0, "pulling": 0, "other": 0}
    for info in infos:
        key = _pod_image_key(info, keys_by_image)
        if image_key is not None and key != image_key:
            continue
        counts = state.setdefault(key, {"ready": 0, "creating": 0, "pulling": 0, "other": 0})
        counts[_pod_phase(info)] += 1
    return dict(sorted(state.items()))


def pool_state_words(image_key: str, state: dict[str, dict[str, int]] | None) -> str:
    """One sentence on *image_key*'s pods for an agent, or ``""`` when the state was not read.

    A key bound to a k8s Pool (``OPENSANDBOX_K8S_POOL_NAMES``) is never counted:
    the server lists a pooled sandbox with image ``unknown`` (its BatchSandbox has
    a ``poolRef`` and no pod template) and never lists the Pool's warm pods, so a
    count would read 0 whatever the Pool holds (#9383).
    """
    if state is None:
        return ""
    from app.core.config import settings

    pool_name = settings.opensandbox_k8s_pool_names.get(image_key)
    if pool_name:
        return (
            f"Image key {image_key!r} runs from the Pool {pool_name!r}, whose warm pods the "
            "OpenSandbox server does not list: the pod count is not observable from the platform."
        )
    counts = state.get(image_key) or {"ready": 0, "creating": 0, "pulling": 0}
    return (
        f"Sandbox pods for image key {image_key!r} on the server now: {counts['ready']} running, "
        f"{counts['creating']} creating, {counts['pulling']} pulling the image."
    )


# ---------------------------------------------------------------------------
# Process-wide probe state
# ---------------------------------------------------------------------------
_opensandbox_available: bool | None = None
_opensandbox_probe_failed_at: float = 0.0
_opensandbox_probe_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# OpenSandbox probe
# ---------------------------------------------------------------------------


async def _probe_opensandbox_available() -> bool:
    """Probe OpenSandbox server reachability via a TCP connect to the configured domain.

    Caching mirrors the Docker probe: a positive result is cached for the
    process lifetime; a negative result is re-probed after
    ``_OPENSANDBOX_PROBE_RETRY_INTERVAL``.  Never raises.
    """
    global _opensandbox_available, _opensandbox_probe_failed_at

    from app.core.config import settings

    domain = (settings.opensandbox_domain or "").strip()
    if not domain:
        return False

    def _cached() -> bool | None:
        if _opensandbox_available is True:
            return True
        if _opensandbox_available is False and (
            time.monotonic() - _opensandbox_probe_failed_at
            < _OPENSANDBOX_PROBE_RETRY_INTERVAL
        ):
            return False
        return None

    cached = _cached()
    if cached is not None:
        return cached

    async with _opensandbox_probe_lock:
        cached = _cached()
        if cached is not None:
            return cached

        if ":" in domain:
            host, port_str = domain.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                _opensandbox_available = False
                _opensandbox_probe_failed_at = time.monotonic()
                return False
        else:
            host = domain
            port = 443 if (settings.opensandbox_protocol or "http") == "https" else 80

        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=_OPENSANDBOX_PROBE_TIMEOUT,
            )
            writer.close()
            await writer.wait_closed()
            available = True
        except Exception:
            available = False

        _opensandbox_available = available
        if not available:
            _opensandbox_probe_failed_at = time.monotonic()
            logger.info(
                "OpenSandbox server is not reachable at %s (TCP connect probe failed).",
                domain,
            )
        return available


# ---------------------------------------------------------------------------
# OpenSandbox connection config
# ---------------------------------------------------------------------------


def _build_opensandbox_config() -> ConnectionConfig:
    """Build a :class:`~opensandbox.config.ConnectionConfig` from settings."""
    from datetime import timedelta

    from app.core.config import settings

    return ConnectionConfig(
        domain=settings.opensandbox_domain,
        protocol=settings.opensandbox_protocol,
        api_key=settings.opensandbox_api_key,
        request_timeout=timedelta(minutes=5),
        use_server_proxy=settings.opensandbox_use_server_proxy,
    )


# ---------------------------------------------------------------------------
# OpenSandbox container creation (shared by code_interpreter and
# custom_tool_executor's three Sandbox.create() call sites)
# ---------------------------------------------------------------------------


async def _create_opensandbox(
    *,
    image_key: str,
    pool_key: str | None = None,  # unused by any call site today; kept for a future decoupled-pool-naming deployment (see docstring)
    timeout_seconds: int,
    kind_label: str,
    usage_type: str,
    user_id,
    egress_allow_list,
    resource: dict[str, str],
    env: dict[str, str] | None = None,
    retry_timeouts_for: float | None = None,
    open_egress: bool = False,
):
    """Create an OpenSandbox container for *image_key* (``"python"``/``"nodejs"``).

    Resolves the container image from ``OPENSANDBOX_IMAGES[image_key]``. If
    *image_key* isn't in that mapping (e.g. a typo'd key with no default),
    logs and raises ``RuntimeError`` before ever calling ``Sandbox.create``.
    The k8s Pool binding (``OPENSANDBOX_K8S_POOL_NAMES``, when configured)
    is looked up by *pool_key* if given, else falls back to *image_key* —
    the common case is the same identifier for both, so callers only need
    to pass *pool_key* if a deployment ever wants to decouple pool naming
    from image naming.

    On any creation failure — missing image included — logs the error
    (*kind_label* identifies which sandbox kind failed, e.g.
    ``"code-interpreter"``/``"JS"``/``"Python"``, for a human-readable log
    line) and records it via ``log_sandbox_create_failure``, then re-raises.
    Callers decide how to surface the failure (an error-dict shape vs. a
    ``RuntimeError``) without duplicating this logging.

    An egress-sidecar start failure is retried ``_EGRESS_SIDECAR_RETRIES``
    times before it counts as a failure, and a create timeout (HTTP 504, the
    pod not ready in the server's time) ``_CREATE_TIMEOUT_RETRIES`` times; if
    either persists it is raised as :class:`SandboxUnavailableError`, whose
    message says the platform (not the tool) failed, and for a timeout the
    stage, the attempts and the seconds spent.

    *open_egress* creates it under :func:`egress_policy`'s default-allow policy
    (the code interpreter with ``CODE_INTERPRETER_OPEN_EGRESS``) instead of the
    default-deny one.

    *retry_timeouts_for* (seconds) is for a caller whose create does not hold
    a tool call open (the workspace command container, created past the
    call): a 504 is then retried, with a growing pause, until that window
    ends, instead of ``_CREATE_TIMEOUT_RETRIES`` times.

    A broken Pool never blocks the call: when the Pool has no pod to give
    (``sandbox_pool_reaper.pool_starved``) or a ``poolRef`` create times out,
    the call drops the ``poolRef`` ONCE and creates the sandbox directly from
    the same image, resources, environment and default-deny network policy
    (the server's own pod template), while the reaper heals the Pool. Nothing
    else changes and the policy is never relaxed.
    """
    from datetime import timedelta

    from opensandbox import Sandbox
    from opensandbox.exceptions.sandbox import SandboxException

    from app.core.config import settings
    from app.core.utils import format_exception_message
    from app.services.sandbox_call_logging import log_sandbox_create_failure

    async def _fail(exc: Exception, log_message: str, *log_args) -> None:
        from app.services.frictions import note_sandbox_failure

        logger.error(log_message, *log_args, exc_info=True)
        await log_sandbox_create_failure(
            usage_type=usage_type, user_id=user_id, exc=exc, egress_allow_list=egress_allow_list
        )
        # A friction of the run that asked for the container (no scope bound: nothing).
        note_sandbox_failure(
            usage_type, f"{kind_label} sandbox (image key {image_key}) not created: {format_exception_message(exc)}"
        )

    image = settings.opensandbox_images.get(image_key)
    # The caller's own variables win over the thread caps.
    env = {**native_thread_env(resource.get("cpu")), **(env or {})}
    network_policy = egress_policy(egress_allow_list, open_egress=open_egress)
    pool_name = settings.opensandbox_k8s_pool_names.get(pool_key or image_key)

    if image is None:
        exc = RuntimeError(
            f"No OpenSandbox image configured for key {image_key!r} — check OPENSANDBOX_IMAGES."
        )
        await _fail(
            exc,
            "Cannot create %s sandbox: no image configured for key %r (OPENSANDBOX_IMAGES): %s",
            kind_label,
            image_key,
            exc,
        )
        raise exc

    attempt = 0
    timeouts = 0
    started = time.monotonic()
    timed_out: list[float] = []
    pooled = bool(pool_name)
    # A toolchain Pool is the only route to its network: its pods carry the toolchain label the
    # registry egress policy selects, a direct pod gets the server's template and the namespace
    # deny-all (no DNS, no package registry). Such a create never drops its poolRef.
    pool_bound = pooled and is_pool_bound(image_key)
    fell_back = False
    if pooled:
        from app.services.sandbox_pool_reaper import pool_starved, request_reap

        if not pool_bound and await pool_starved(pool_name or ""):
            fell_back, pooled = True, False
            POOL_FALLBACKS.labels(image_key=image_key, reason="starved").inc()
            await request_reap()
            logger.warning(
                "OpenSandbox Pool %s has no pod to give for %s sandbox (image=%s); creating it directly",
                pool_name,
                kind_label,
                image,
            )
    while True:
        attempt_started = time.monotonic()
        try:
            created = await Sandbox.create(
                image=image,
                connection_config=_build_opensandbox_config(),
                timeout=timedelta(seconds=timeout_seconds),
                env=env,
                network_policy=network_policy,
                resource=resource,
                extensions={"poolRef": pool_name} if pooled and pool_name else None,
            )
            _observe_create(image_key, "ok", pooled, time.monotonic() - attempt_started)
            return created
        except SandboxException as exc:
            if _is_create_timeout(exc):
                timed_out.append(time.monotonic() - attempt_started)
                _observe_create(image_key, "timeout", pooled, timed_out[-1])
                timeouts += 1
                if pooled:
                    # A Pool that holds dead pods never refills: the reaper deletes them now,
                    # and this call goes on without the Pool (once, same image and policy).
                    await request_reap()
                    if not fell_back and not pool_bound:
                        fell_back, pooled = True, False
                        POOL_FALLBACKS.labels(image_key=image_key, reason="timeout").inc()
                        logger.warning(
                            "OpenSandbox Pool %s timed out creating %s sandbox (image=%s) after %.1f s; "
                            "creating it directly: %s",
                            pool_name,
                            kind_label,
                            image,
                            timed_out[-1],
                            _server_detail(exc),
                        )
                        continue
                if _retry_timeout(timeouts, started, retry_timeouts_for):
                    logger.warning(
                        "OpenSandbox timed out creating %s sandbox (image=%s) after %.1f s; retrying (%d, %s): %s %s",
                        kind_label,
                        image,
                        time.monotonic() - started,
                        timeouts,
                        f"window {retry_timeouts_for:.0f} s" if retry_timeouts_for else f"of {_CREATE_TIMEOUT_RETRIES}",
                        format_exception_message(exc),
                        _server_detail(exc),
                    )
                    await asyncio.sleep(_create_retry_delay(timeouts))
                    continue
                await _fail(
                    exc, "OpenSandbox timed out creating %s sandbox (image=%s): %s", kind_label, image, exc
                )
                raise SandboxUnavailableError(
                    _create_timeout_message(image_key, timed_out, time.monotonic() - started, exc)
                ) from exc
            sidecar = _is_egress_sidecar_failure(exc)
            _observe_create(image_key, "sidecar" if sidecar else "error", pooled, time.monotonic() - attempt_started)
            if sidecar and attempt < _EGRESS_SIDECAR_RETRIES:
                attempt += 1
                logger.warning(
                    "OpenSandbox egress sidecar failed to start for %s sandbox (image=%s); "
                    "retrying (%d/%d): %s",
                    kind_label,
                    image,
                    attempt,
                    _EGRESS_SIDECAR_RETRIES,
                    format_exception_message(exc),
                )
                delay = _EGRESS_SIDECAR_RETRY_DELAY_SECONDS * random.uniform(1.0, 2.0)  # noqa: S311 - jitter
                await asyncio.sleep(delay)
                continue
            await _fail(
                exc, "OpenSandbox failed to create %s sandbox (image=%s): %s", kind_label, image, exc
            )
            if sidecar:
                raise SandboxUnavailableError(
                    "the sandbox infrastructure could not start its network-isolation "
                    f"(egress) container after {attempt + 1} attempts. This is a platform "
                    "problem, not a defect of the tool, and it is usually transient: retry "
                    "the call once; if it fails again, report that the sandbox service is "
                    "unavailable and an administrator should check the OpenSandbox server. "
                    f"Details: {exc}"
                ) from exc
            raise
        except Exception as exc:
            _observe_create(image_key, "error", pooled, time.monotonic() - attempt_started)
            await _fail(
                exc, "Unexpected error creating %s sandbox (image=%s): %s", kind_label, image, exc
            )
            raise
