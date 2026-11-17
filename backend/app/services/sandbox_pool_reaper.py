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

"""The OpenSandbox pool reaper: XTM One cleans up what its sandboxes leave in the cluster.

An OpenSandbox ``Pool`` lists its pods by owner and counts every one that is
not being deleted, whatever its state: a pod that ended (phase ``Failed`` or
``Succeeded``, reason ``Evicted`` after node disk pressure) or one stuck before
Running (``Init:*``, ``ContainerStatusUnknown``, ``Unknown``) still fills the
warm buffer, so the controller creates no replacement and every ``poolRef``
create waits the server's ``sandbox_create_timeout_seconds`` and answers HTTP
504 ``K8S_POD_READY_TIMEOUT`` until someone deletes the dead pods by hand
(upstream: opensandbox-group/OpenSandbox ``PoolReconciler.scalePool``, which
counts ``len(pods)`` as schedulable). The reaper deletes them, so the Pool
controller refills its buffer by itself; it also deletes the ``BatchSandbox``
objects past their ``spec.expireTime`` that nothing collected.

It runs in the worker, on the reconciler cycle
(``orchestration_adapters.sandbox_pool_reaper``): once a minute for the whole
install (a Redis window), and at once after a pooled create timed out
(:func:`request_reap`, called by ``_sandbox_selector._create_opensandbox``).
It talks to the API server with the pod's service account through the labs'
client (:mod:`app.services.sandbox_workspace.kube`), in
``OPENSANDBOX_K8S_NAMESPACE`` only. Unset namespace: off. Not in a cluster, or
no RBAC: one log line per process, then nothing. It deletes, it never creates,
scales or relaxes anything.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.metrics import get_or_create_counter

logger = logging.getLogger(__name__)

#: The label the OpenSandbox controller puts on every pod of a Pool.
POOL_LABEL = "sandbox.opensandbox.io/pool-name"
GROUP = "sandbox.opensandbox.io"
VERSION = "v1alpha1"
BATCH_SANDBOXES = "batchsandboxes"
POOLS = "pools"
#: Seconds the starvation read (:func:`pool_starved`) may take before a create goes on pooled.
STARVED_READ_TIMEOUT_SECONDS = 3.0

#: How often the whole install reaps (seconds), and how often each process asks.
INTERVAL_SECONDS = 60
ATTEMPT_INTERVAL_SECONDS = 5
_WINDOW_KEY = "sandbox_pool_reaper:window"
_REQUEST_KEY = "sandbox_pool_reaper:requested"
#: Container waiting reasons that never resolve by themselves.
_BROKEN_WAITING = frozenset({"ContainerStatusUnknown", "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"})

REAPED = get_or_create_counter(
    "xtmone_sandbox_pool_reaped_total",
    "OpenSandbox objects the pool reaper deleted, by kind (pod, batchsandbox) and reason.",
    ["kind", "reason"],
)

#: The reasons already logged as "reaper off" in this process (one line each).
_off_logged: set[str] = set()


@dataclass
class ReapReport:
    pods: dict[str, str] = field(default_factory=dict)
    batch_sandboxes: list[str] = field(default_factory=list)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_pool_pod(pod: dict[str, Any]) -> bool:
    meta = pod.get("metadata") or {}
    if (meta.get("labels") or {}).get(POOL_LABEL):
        return True
    return any(ref.get("kind") == "Pool" for ref in meta.get("ownerReferences") or [])


def _ready_since(status: dict[str, Any]) -> tuple[bool, datetime | None]:
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Ready":
            return condition.get("status") == "True", _parse_time(condition.get("lastTransitionTime"))
    return False, None


def pod_verdict(pod: dict[str, Any], *, now: datetime, stuck_after: timedelta) -> str | None:
    """Why a Pool pod must go (``evicted``, ``failed``, ``succeeded``, ``stuck``, ``broken``), or ``None``."""
    meta = pod.get("metadata") or {}
    if meta.get("deletionTimestamp") or not _is_pool_pod(pod):
        return None
    status = pod.get("status") or {}
    phase = status.get("phase") or "Unknown"
    if status.get("reason") == "Evicted":
        return "evicted"
    if phase == "Failed":
        return "failed"
    if phase == "Succeeded":
        return "succeeded"
    created = _parse_time(meta.get("creationTimestamp"))
    statuses = [*(status.get("initContainerStatuses") or []), *(status.get("containerStatuses") or [])]
    broken = any(((s.get("state") or {}).get("waiting") or {}).get("reason") in _BROKEN_WAITING for s in statuses)
    if phase == "Running":
        ready, since = _ready_since(status)
        if ready:
            return None
        since = since or created
        if since is not None and now - since > stuck_after:
            return "broken" if broken else "stuck"
        return None
    # Pending (Init:*, image pull, unschedulable) or Unknown (node lost).
    if created is not None and now - created > stuck_after:
        return "broken" if broken else "stuck"
    return None


def batch_sandbox_expired(sandbox: dict[str, Any], *, now: datetime) -> bool:
    if (sandbox.get("metadata") or {}).get("deletionTimestamp"):
        return False
    expire = _parse_time((sandbox.get("spec") or {}).get("expireTime"))
    return expire is not None and expire < now


def _log_off(reason: str, message: str, *args: object) -> None:
    if reason not in _off_logged:
        _off_logged.add(reason)
        logger.warning(message, *args)


# Works around https://github.com/opensandbox-group/OpenSandbox/issues/2650; drop once the controller fix is deployed.
async def reap(*, client: Any = None, now: datetime | None = None) -> ReapReport:
    """Delete the dead Pool pods and the expired BatchSandboxes of the sandbox namespace once.

    Raises :class:`~app.services.sandbox_workspace.kube.KubeError` when the API
    server refuses or does not answer (the caller logs it).
    """
    from app.core.config import settings
    from app.services.sandbox_workspace.kube import KubeClient, KubeConfig, KubeError

    report = ReapReport()
    namespace = settings.opensandbox_k8s_namespace.strip()
    if not namespace:
        return report
    client = client or KubeClient(KubeConfig.from_settings(namespace=namespace))
    moment = now or datetime.now(UTC)
    stuck_after = timedelta(seconds=max(60, int(settings.opensandbox_pool_stuck_seconds)))

    for pod in await client.list_pods(POOL_LABEL):
        verdict = pod_verdict(pod, now=moment, stuck_after=stuck_after)
        name = (pod.get("metadata") or {}).get("name") or ""
        if not verdict or not name:
            continue
        try:
            deleted = await client.delete_pod(name)
        except KubeError as exc:
            if exc.status in (401, 403):
                raise
            logger.warning("Sandbox pool reaper: pod %s not deleted: %s", name, exc)
            continue
        if deleted:
            report.pods[name] = verdict
            REAPED.labels(kind="pod", reason=verdict).inc()

    try:
        sandboxes = await client.list_custom(GROUP, VERSION, BATCH_SANDBOXES)
    except KubeError as exc:
        if exc.status in (401, 403, 404):
            _log_off(
                "batchsandboxes", "Sandbox pool reaper: BatchSandboxes not readable in %s, skipped: %s", namespace, exc
            )
            return report
        raise
    for sandbox in sandboxes:
        name = (sandbox.get("metadata") or {}).get("name") or ""
        if not name or not batch_sandbox_expired(sandbox, now=moment):
            continue
        try:
            if await client.delete_custom(GROUP, VERSION, BATCH_SANDBOXES, name):
                report.batch_sandboxes.append(name)
                REAPED.labels(kind="batchsandbox", reason="expired").inc()
        except KubeError as exc:
            logger.warning("Sandbox pool reaper: BatchSandbox %s not deleted: %s", name, exc)
    return report


async def reap_logged() -> None:
    """:func:`reap`, its deletions logged, a missing cluster or RBAC logged once per process."""
    from app.services.sandbox_workspace.kube import KubeError

    try:
        report = await reap()
    except KubeError as exc:
        if exc.status in (401, 403):
            _log_off(
                "rbac",
                "Sandbox pool reaper off: the service account may not manage pods in the sandbox namespace: %s",
                exc,
            )
        elif exc.status == 0 and "no Kubernetes API server" in str(exc):
            _log_off("cluster", "Sandbox pool reaper off: %s", exc)
        else:
            logger.warning("Sandbox pool reaper: the API server did not answer: %s", exc)
        return
    if report.pods:
        logger.info(
            "Sandbox pool reaper: deleted %d Pool pod(s) so their Pool replaces them: %s",
            len(report.pods),
            ", ".join(f"{name} ({why})" for name, why in sorted(report.pods.items())),
        )
    if report.batch_sandboxes:
        logger.info(
            "Sandbox pool reaper: deleted %d expired BatchSandbox(es): %s",
            len(report.batch_sandboxes),
            ", ".join(sorted(report.batch_sandboxes)),
        )


def enabled() -> bool:
    from app.core.config import settings

    return bool(settings.opensandbox_k8s_namespace.strip())


def _pod_creating(pod: dict[str, Any], *, now: datetime, stuck_after: timedelta) -> bool:
    """A live Pool pod on its way to Running (Pending, not dead, not stuck)."""
    if (pod.get("metadata") or {}).get("deletionTimestamp"):
        return False
    phase = (pod.get("status") or {}).get("phase") or "Unknown"
    return phase == "Pending" and pod_verdict(pod, now=now, stuck_after=stuck_after) is None


async def pool_starved(pool_name: str, *, client: Any = None, now: datetime | None = None) -> bool:
    """Whether a ``poolRef`` create on *pool_name* would only wait for its timeout.

    True when the Pool reports no available pod and none of its pods is on its
    way to Running (every one allocated, dead or stuck): the create then goes
    direct at once instead of waiting the server's create timeout for a 504.
    Fail-open: ``False`` when the reaper is off, the Pool is unknown, the API
    server refuses or does not answer within ``STARVED_READ_TIMEOUT_SECONDS``.
    """
    if not enabled() or not pool_name:
        return False
    from app.core.config import settings
    from app.services.sandbox_workspace.kube import KubeClient, KubeConfig

    namespace = settings.opensandbox_k8s_namespace.strip()
    moment = now or datetime.now(UTC)
    stuck_after = timedelta(seconds=max(60, int(settings.opensandbox_pool_stuck_seconds)))

    async def _read() -> bool:
        kube = client or KubeClient(KubeConfig.from_settings(namespace=namespace))
        pool = await kube.get_custom(GROUP, VERSION, POOLS, pool_name)
        if pool is None:
            return False
        try:
            available = int((pool.get("status") or {}).get("available") or 0)
        except (TypeError, ValueError):
            return False
        if available > 0:
            return False
        pods = await kube.list_pods(f"{POOL_LABEL}={pool_name}")
        return not any(_pod_creating(pod, now=moment, stuck_after=stuck_after) for pod in pods)

    try:
        return await asyncio.wait_for(_read(), timeout=STARVED_READ_TIMEOUT_SECONDS)
    except Exception as exc:  # fail-open: the read is a hint, the pooled create goes on
        logger.debug("Sandbox pool %s: starvation not read: %s", pool_name, exc)
        return False


async def request_reap() -> None:
    """Ask the next worker cycle to reap at once (a pooled create just timed out). Never raises."""
    if not enabled():
        return
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        await client.set(_REQUEST_KEY, "1", ex=INTERVAL_SECONDS * 5)
    except Exception as exc:
        logger.debug("Sandbox pool reaper: the request was not recorded: %s", exc)


async def take_turn() -> bool | None:
    """Whether this process reaps now: a pending request (taken by one process), else the minute's window.

    ``None`` when Redis does not answer: the caller then reaps on its own
    minute (fail-open; the deletions are idempotent, a pod already gone
    answers 404).
    """
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        if await client.delete(_REQUEST_KEY):
            return True
        return bool(await client.set(_WINDOW_KEY, "1", nx=True, ex=INTERVAL_SECONDS - 5))
    except Exception:
        return None
