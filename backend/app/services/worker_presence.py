# SPDX-License-Identifier: Apache-2.0

"""Which workers are alive, and how the last run of each one ended.

A lease says when its holder last renewed it, never whether the holder is
still there: a worker killed mid-render (the kernel's out-of-memory killer, a
crash) leaves its lease to run out - three minutes for a render - while nothing
draws. Every worker's reconciler therefore says it is there, in Redis, every
:data:`PUBLISH_EVERY_SECONDS` (:func:`publish`, a record living
:data:`PRESENCE_TTL_SECONDS`): its version, its host and process, its state
(``running``, ``draining`` once a stop was asked, ``stopped`` once it stopped,
with the signal that asked), and the container's memory at that moment; its
last word is kept an hour after its presence ran out.

:func:`gone` reads the holders of leases and says which are gone, with their
last record, so the reconciler takes their work back at once
(``orchestration_engine.reclaim_from_gone_owners``) instead of waiting for the
leases:

* a holder that said ``stopped`` is gone (its hand-back did not land);
* a holder on this very host whose process is no longer the one that wrote
  the record (a container restarted in place: its PID taken by another
  process, or by no process) is gone at once;
* a holder whose presence ran out while its last word is kept (``lapsed``) is
  gone: a worker of a release that never published is never taken for dead,
  and its leases expire as they always did.

:func:`stop_reason` words why a holder went, the signal and the memory at its
last word included. Redis being unreachable never takes a worker for gone.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import settings
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

_KEY = "xtm:orchestration:worker:{}"
#: The same record, kept an hour: once the presence ran out, what the worker last said (its memory, its version).
_LAST_KEY = "xtm:orchestration:worker-last:{}"
#: How often a worker says it is there.
PUBLISH_EVERY_SECONDS = 5.0
#: How long its word lasts: a worker silent this long is gone.
PRESENCE_TTL_SECONDS = 20
#: How long the last word of a worker is kept: for the next worker to say how it went.
LAST_WORD_TTL_SECONDS = 3600
#: A worker whose container used this share of its memory limit at its last word, and then went without one, was
#: most likely killed by the out-of-memory killer.
OOM_SHARE = 0.9
#: When this process came up: a worker of another release last heard after it was still running when this release
#: was up, so the rollout, not its work, ended it (:func:`replaced_by_deploy`).
_PROCESS_STARTED_AT = datetime.now(UTC)
_BOOT_ID = "/proc/sys/kernel/random/boot_id"

STATE_RUNNING = "running"
STATE_DRAINING = "draining"
STATE_STOPPED = "stopped"

_published: dict[str, float] = {}


def _boot_id() -> str | None:
    try:
        return Path(_BOOT_ID).read_text().strip() or None
    except OSError:
        return None


def identity() -> dict:
    """This process as a record names it: host, process and its start, the kernel's boot, the version."""
    from app.services.video_studio import resources

    pid = os.getpid()
    return {
        "host": socket.gethostname(),
        "pid": pid,
        "started": resources.process_start(pid),
        "boot_id": _boot_id(),
        "version": settings.app_version,
        # Comparable across hosts (unlike ``started``, in ticks since its boot): which release came up last.
        "process_started_at": _PROCESS_STARTED_AT.isoformat(),
    }


def _memory() -> dict:
    from app.services.video_studio import resources

    return {
        "memory": resources.memory_usage(),
        "memory_limit": resources.memory_limit(),
        "oom_kills": resources.oom_kills(),
    }


async def publish(
    worker_id: str,
    *,
    state: str = STATE_RUNNING,
    signal: str | None = None,
    force: bool = False,
    claims: list[str] | None = None,
    extra: Callable[[], dict] | None = None,
) -> None:
    """Say *worker_id* is there, in *state*: at most every :data:`PUBLISH_EVERY_SECONDS` unless *force* or stopping.

    *claims* are the adapters whose executions it claims (``orchestration_reconciler.claimed_adapters``):
    what :func:`count` reads. *extra* gives the rest of the record when one is written (the CPUs its work
    reserves, the executions it runs, what its adapters add): never what keeps it from being written.
    """
    from app.core.redis import redis_client

    now = time.monotonic()
    if not force and state == STATE_RUNNING and now - _published.get(worker_id, -1e9) < PUBLISH_EVERY_SECONDS:
        return
    _published[worker_id] = now
    added: dict = {}
    if extra is not None:
        try:
            added = dict(extra() or {})
        except Exception as exc:
            logger.debug("Worker presence extras not read: %s", format_exception_message(exc))
    record = {
        **added,
        "worker_id": worker_id,
        **identity(),
        **_memory(),
        "state": state,
        "signal": signal,
        "at": datetime.now(UTC).isoformat(),
        **({"claims": list(claims)} if claims is not None else {}),
    }
    text = json.dumps(record)
    try:
        redis = await redis_client()
        await redis.set(_LAST_KEY.format(worker_id), text, ex=LAST_WORD_TTL_SECONDS)
        if state == STATE_STOPPED:
            await redis.delete(_KEY.format(worker_id))
        else:
            await redis.set(_KEY.format(worker_id), text, ex=PRESENCE_TTL_SECONDS)
    except Exception as exc:  # unreachable: no worker reads it as gone
        logger.debug("Worker presence not published: %s", format_exception_message(exc))


def _loads(value: object) -> dict | None:
    if not value:
        return None
    try:
        record = json.loads(value)
    except (TypeError, ValueError):
        return None
    return record if isinstance(record, dict) else None


async def records(worker_ids: Iterable[str]) -> dict[str, dict | None] | None:
    """The last record of each of *worker_ids*, ``lapsed`` when its presence ran out (``None``: it never published);
    ``None`` when Redis cannot be read."""
    from app.core.redis import redis_client

    ids = sorted(set(worker_ids))
    if not ids:
        return {}
    try:
        redis = await redis_client()
        present = await redis.mget([_KEY.format(worker_id) for worker_id in ids])
        last = await redis.mget([_LAST_KEY.format(worker_id) for worker_id in ids])
    except Exception as exc:
        logger.debug("Worker presence not read: %s", format_exception_message(exc))
        return None
    found: dict[str, dict | None] = {}
    for worker_id, now_said, last_said in zip(ids, present, last, strict=True):
        record = _loads(now_said)
        if record is None and (record := _loads(last_said)) is not None and record.get("state") != STATE_STOPPED:
            record = {**record, "lapsed": True}
        found[worker_id] = record
    return found


def claims(record: dict, adapter: str) -> bool:
    """Whether the worker of *record* claims *adapter*'s executions: a record of a release that listed none
    claims all of them."""
    listed = record.get("claims")
    return not isinstance(listed, list) or adapter in listed


def reserved_cpus(record: dict) -> int:
    """The CPUs the work of the worker of *record* reserves (a whole render's pages); ``0`` for a record of a
    release that said none."""
    try:
        return max(0, int(record.get("reserved_cpus") or 0))
    except (TypeError, ValueError):
        return 0


async def present(*, at_most: int = 256) -> list[dict] | None:
    """The record of every worker there now (its presence not run out), running or draining, at most *at_most*,
    ordered by worker; ``None`` when Redis cannot be read. Who could take a render waiting for a worker."""
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        keys = []
        async for key in redis.scan_iter(match=_KEY.format("*"), count=100):
            keys.append(key)
            if len(keys) >= at_most:
                break
        said = await redis.mget(keys) if keys else []
    except Exception as exc:
        logger.debug("Workers present not read: %s", format_exception_message(exc))
        return None
    records = [record for value in said if (record := _loads(value)) is not None and record.get("state") != STATE_STOPPED]
    return sorted(records, key=lambda record: str(record.get("worker_id") or ""))


async def count(*, at_most: int = 256, claiming: str | None = None, incoming: bool = False) -> int | None:
    """How many workers are there and running now (their presence not run out, not draining), at most *at_most* -
    of them, with *claiming*, those that claim that adapter's executions (:func:`claims`); with *incoming*,
    those of another release than this one (:func:`is_incoming`); ``None`` when Redis cannot be read. What a
    render is split across (``render_preflight``)."""
    found = await present(at_most=at_most)
    if found is None:
        return None
    return sum(
        1
        for record in found
        if record.get("state") == STATE_RUNNING
        and (claiming is None or claims(record, claiming))
        and (not incoming or is_incoming(record))
    )


def is_incoming(record: dict) -> bool:
    """Whether the worker of *record* runs another release than this one: while this worker drains, the
    release a rolling update brings in - a worker of this very release is stopped by the same rollout next. A record
    that names no version is never taken for one."""
    version = record.get("version")
    return isinstance(version, str) and bool(version) and version != settings.app_version


async def incoming_worker(claiming: str, *, me: str | None = None) -> str | None:
    """A running worker of another release that claims *claiming*'s executions, the first by worker; ``None``
    when none runs or Redis cannot be read. What a draining worker hands its work to first."""
    found = await present() or []
    for record in found:
        worker_id = record.get("worker_id")
        if (
            worker_id
            and worker_id != me
            and record.get("state") == STATE_RUNNING
            and claims(record, claiming)
            and is_incoming(record)
        ):
            return str(worker_id)
    return None


def name_of(record: dict) -> str:
    """``worker-7f9c (version 1.4.2)``: a worker as a message names it, from its record."""
    name = str(record.get("host") or record.get("worker_id") or "a worker")
    return f"{name} (version {record['version']})" if record.get("version") else name


def _process_alive(pid: object, started: object) -> bool:
    """Whether the process *pid* on this host is the one that started at *started* (clock ticks since boot)."""
    from app.services.video_studio import resources

    if not isinstance(pid, int):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return True
    return started is None or resources.process_start(pid) == started


def is_gone(record: dict | None) -> bool:
    """Whether the worker of *record* is gone: it said it stopped, its process here is not it, or its word ran out."""
    if record is None:
        return False
    if record.get("lapsed") or record.get("state") == STATE_STOPPED:
        return True
    me = identity()
    same_host = record.get("host") == me["host"] and record.get("boot_id") and record.get("boot_id") == me["boot_id"]
    if same_host and (record.get("pid"), record.get("started")) != (me["pid"], me["started"]):
        return not _process_alive(record.get("pid"), record.get("started"))
    return False


def _heard_at(record: dict) -> datetime | None:
    """When the worker of *record* said it (its ``at``), timezone-aware; ``None`` when absent or unreadable."""
    try:
        at = datetime.fromisoformat(str(record["at"]))
    except (KeyError, TypeError, ValueError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


#: The key :func:`gone` adds to the record of a worker a deploy replaced (``orchestration_engine.REPLACED_BY_DEPLOY_KEY``).
REPLACED_BY_DEPLOY = "replaced_by_deploy"


def replaced_by_deploy(record: dict | None) -> bool:
    """Whether the worker of *record*, gone without a word, was killed by the deploy that replaced it.

    The version alone proves nothing (an old worker may have crashed before the
    rollout), so every one of these must hold: its last record says ``running``
    from a release that is not this one; it was last heard at or after this
    process came up (:data:`_PROCESS_STARTED_AT`: in a rolling deploy the new
    worker is up before the old one is killed, while an old worker that died
    before the rollout was last heard earlier); its memory telemetry is there
    and below :data:`OOM_SHARE` of its limit; and, on this very container, no
    out-of-memory kill counted since. Then the orchestrator's kill deadline
    came before its hand-back: a stop outside the work. Anything less (same
    release, no telemetry, an earlier last word, a reclaimer started after the
    kill) stays a loss without a word and spends its cap.
    """
    if not record or record.get("state") in (STATE_DRAINING, STATE_STOPPED):
        return False
    version = record.get("version")
    if not isinstance(version, str) or not version or version == settings.app_version:
        return False
    heard = _heard_at(record)
    if heard is None or heard < _PROCESS_STARTED_AT:
        return False
    share = _share(record)
    if share is None or share >= OOM_SHARE:
        return False
    from app.services.video_studio import resources

    here = record.get("host") == identity()["host"] and record.get("boot_id") == identity()["boot_id"]
    kills, now_kills = record.get("oom_kills"), resources.oom_kills() if here else None
    return not (isinstance(kills, int) and isinstance(now_kills, int) and now_kills > kills)


def _started_at(record: dict) -> datetime | None:
    said = record.get("process_started_at")
    if not isinstance(said, str):
        return None
    try:
        moment = datetime.fromisoformat(said)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def newest_release(found: Iterable[dict] | None) -> str | None:
    """The release a rolling update brings in, read from the workers there now (:func:`present`), this one included.

    The release of the running worker whose process came up last (``process_started_at``); a record of a release
    that did not say when its process started is older than any that did. This worker counts with its own start.
    ``None`` when no record names a version. Never this worker's own release alone: during a rollout a worker of
    the outgoing release reclaiming the work of another must still read the kill as the rollout's.
    """
    floor = datetime.min.replace(tzinfo=UTC)
    best: tuple[datetime, str] | None = (_PROCESS_STARTED_AT, settings.app_version) if settings.app_version else None
    for record in found or ():
        version = record.get("version")
        if record.get("state") != STATE_RUNNING or not isinstance(version, str) or not version:
            continue
        started = _started_at(record) or floor
        if best is None or started > best[0]:
            best = (started, version)
    return best[1] if best else None


def outgoing(found: Iterable[dict] | None, adapter: str) -> bool:
    """Whether this worker runs a release a rollout is replacing: a running worker of the newest release
    (:func:`newest_release`), another than this one, claims *adapter*'s executions. Such a worker is killed by the
    same rollout next, so it does not claim a resumable execution: the incoming release does."""
    records_now = list(found or ())
    newest = newest_release(records_now)
    if not newest or newest == settings.app_version:
        return False
    return any(
        record.get("state") == STATE_RUNNING and record.get("version") == newest and claims(record, adapter)
        for record in records_now
    )


def killed_by_rollout(record: dict | None, newest: str | None) -> bool:
    """Whether a worker gone without a stop request was killed by the rollout that brings in *newest*.

    Its last record says ``running`` from another release than the newest one there now, with its memory telemetry
    under :data:`OOM_SHARE` of its limit and no out-of-memory count up (:func:`out_of_memory_sign`). Read against the
    newest release, not the reclaimer's own: a worker of the outgoing release that takes the work back, or one of
    the new release that came up after the kill, reads it the same. A worker of the newest release gone without a
    word (a crash on a stable build) is never one.
    """
    if not isinstance(record, dict) or record.get("state") in (STATE_DRAINING, STATE_STOPPED):
        return False
    version = record.get("version")
    if not isinstance(version, str) or not version or not newest or version == newest:
        return False
    if _share(record) is None:
        return False
    return not out_of_memory_sign(record)


async def gone(owners: Iterable[str], *, me: str) -> dict[str, dict]:
    """The holders among *owners* that are gone (:func:`is_gone`), with their last record; ``{}`` when unknown.

    The record of one a deploy replaced (:func:`replaced_by_deploy`, or :func:`killed_by_rollout` against the
    newest release there now) says so (:data:`REPLACED_BY_DEPLOY`), for its work to be taken back as from a
    worker that stopped on its word: a rollout's kill never counts as a loss of the work.
    """
    wanted = [owner for owner in set(owners) if owner and owner != me]
    found = await records(wanted)
    if not found:
        return {}
    lost = {owner: record for owner, record in found.items() if record is not None and is_gone(record)}
    if not lost:
        return {}
    newest = newest_release(await present() or [])
    return {
        owner: {**record, REPLACED_BY_DEPLOY: True}
        if replaced_by_deploy(record) or killed_by_rollout(record, newest)
        else record
        for owner, record in lost.items()
    }


def resumed_after(previous_version: object) -> str:
    """``after a deploy (1.4.1 -> 1.4.2)`` or ``after its worker restarted (version 1.4.2)``: what a stop was, told by
    the worker that resumes the work, from the version of the one that stopped."""
    mine = settings.app_version
    if not isinstance(previous_version, str) or not previous_version:
        return "after its worker stopped"
    if previous_version != mine:
        return f"after a deploy ({previous_version} -> {mine})"
    return f"after its worker restarted (version {mine})"


def _share(record: dict) -> float | None:
    used, limit = record.get("memory"), record.get("memory_limit")
    if isinstance(used, int) and isinstance(limit, int) and limit > 0:
        return used / limit
    return None


def memory_words(record: dict) -> str:
    """``3.9 GiB of its 4.0 GiB limit (97%)``, or what of it is known; empty when nothing is."""
    from app.services.video_studio.resources import format_bytes

    used, limit = record.get("memory"), record.get("memory_limit")
    if not isinstance(used, int):
        return ""
    if not isinstance(limit, int) or limit <= 0:
        return f"{format_bytes(used)} (no memory limit)"
    return f"{format_bytes(used)} of its {format_bytes(limit)} limit ({round(100 * used / limit)}%)"


def out_of_memory_sign(record: dict | None) -> bool:
    """Whether the last word of a worker gone without a stop request says the kernel's out-of-memory killer took it.

    The same reading as :func:`stop_reason`: the container's out-of-memory count went up since its last record
    (readable on its own host only), or its last memory stood at :data:`OOM_SHARE` of its limit or more. A worker
    that said it was stopping, or that a deploy replaced, shows none.
    """
    if not isinstance(record, dict) or record.get("state") in (STATE_STOPPED, STATE_DRAINING):
        return False
    if record.get(REPLACED_BY_DEPLOY):
        return False
    share = _share(record)
    if share is not None and share >= OOM_SHARE:
        return True
    from app.services.video_studio import resources

    here = record.get("host") == identity()["host"] and record.get("boot_id") == identity()["boot_id"]
    kills, now_kills = record.get("oom_kills"), resources.oom_kills() if here else None
    return isinstance(kills, int) and isinstance(now_kills, int) and now_kills > kills


def rollout_kill(record: dict | None) -> bool:
    """Whether a worker gone without a stop request was most likely killed by the rollout that replaced its release.

    Its last record says ``running`` from a release that is not this one, and shows no sign of an out-of-memory
    kill (:func:`out_of_memory_sign`: its memory under :data:`OOM_SHARE` of its limit, no out-of-memory count
    up). Wider than :func:`replaced_by_deploy`, which also needs it heard after this process came up: during
    back-to-back deploys the worker taking it back is often itself of a later release than the one that killed
    it. An older release gone silent during a rollout is the rollout's kill, not its work's; a crash of the work
    on the live release stays a loss of the work (same release: never a rollout kill).
    """
    if not isinstance(record, dict) or record.get("state") in (STATE_STOPPED, STATE_DRAINING):
        return False
    version = record.get("version")
    if not isinstance(version, str) or not version or version == settings.app_version:
        return False
    return not out_of_memory_sign(record)


def stop_reason(record: dict | None, *, now: datetime | None = None) -> str:
    """Why a lease holder went, in words: the stop it was asked for, or the signal and the memory of its last word.

    A worker asked to stop says so (``SIGTERM``). One that went without a word
    was killed by a signal no process can catch - SIGKILL, which the kernel's
    out-of-memory killer sends - or crashed: its last memory says which is
    likelier, and the container's out-of-memory count when this host can read it.
    """
    if not record:
        return "its worker went without a word"
    host = f" on {record['host']}" if record.get("host") else ""
    version = f" (version {record['version']})" if record.get("version") else ""
    worker = f"its worker{host}{version}"
    signal = record.get("signal") or "a stop request"
    if record.get("state") == STATE_STOPPED:
        return f"{worker} was stopped ({signal}) and did not hand it back"
    if record.get("state") == STATE_DRAINING:
        return (
            f"{worker} was asked to stop ({signal}) and was killed before it handed it back: the orchestrator's kill "
            "deadline came before WORKER_SHUTDOWN_GRACE_PERIOD ended"
        )
    heard = ""
    at = record.get("at")
    if isinstance(at, str):
        try:
            then = datetime.fromisoformat(at)
            seconds = max(0, int(((now or datetime.now(UTC)) - then).total_seconds()))
            heard = f", last heard {seconds} s ago"
        except ValueError:
            heard = ""
    if record.get(REPLACED_BY_DEPLOY):
        return (
            f"{worker} went without a stop request{heard}: replaced by a deploy "
            f"({record.get('version')} -> {settings.app_version}) and killed before it handed it back"
        )
    memory = memory_words(record)
    used = f", using {memory} then" if memory else ""
    share = _share(record)
    from app.services.video_studio import resources

    # The container's count is only this one's: a worker of another host counted in another container.
    here = record.get("host") == identity()["host"] and record.get("boot_id") == identity()["boot_id"]
    kills, now_kills = record.get("oom_kills"), resources.oom_kills() if here else None
    if isinstance(kills, int) and isinstance(now_kills, int) and now_kills > kills:
        cause = f"killed by the kernel's out-of-memory killer (SIGKILL; the container's out-of-memory kills went from {kills} to {now_kills})"
    elif share is not None and share >= OOM_SHARE:
        cause = "most likely killed by the kernel's out-of-memory killer (SIGKILL, which no process can catch)"
    elif rollout_kill(record):
        cause = (
            f"most likely killed by the rollout that replaced its release ({record.get('version')} -> "
            f"{settings.app_version}; SIGKILL, with no out-of-memory sign)"
        )
    else:
        cause = "killed by a signal no process can catch (SIGKILL) or crashed"
    return f"{worker} went without a stop request{heard}{used}: {cause}"
