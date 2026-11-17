# SPDX-License-Identifier: Apache-2.0

"""Web workers of this launch that stopped without shutting down.

Two things kill one web worker without a word while its pod stays up:

- uvicorn's multiprocess supervisor SIGKILLs a worker that misses its 5 s
  health-check ping and starts a replacement, saying so only in an INFO line
  of its own plain-text output;
- the kernel's out-of-memory killer kills the worker process: the container
  survives because its main process is the supervisor, and Kubernetes reports
  ``OOMKilled`` only when the main process dies.

Either way the worker's chat turns end with no reply and no log. Every worker
of a launch records itself in the container's temp directory
(``xtm-one-workers/<launch id>/<pid>``: its process start time and the
container's ``oom_kill`` count) and removes the record when it starts a clean
shutdown. A worker that starts and finds a sibling's record whose process is
gone logs it at WARNING, with what the ``oom_kill`` count says, then removes
it. Records of an earlier launch (a restarted container) are removed unread:
Kubernetes reports that restart itself.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from app.core.launch_marker import current_launch_id
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

_REGISTRY = "xtm-one-workers"


def _registry_root() -> Path:
    return Path(tempfile.gettempdir()) / _REGISTRY


def _record_path() -> Path | None:
    launch_id = current_launch_id()
    if launch_id is None:
        return None
    return _registry_root() / launch_id / str(os.getpid())


def _process_start(pid: int) -> int | None:
    from app.services.video_studio.resources import process_start

    return process_start(pid)


def _oom_kills() -> int | None:
    from app.services.video_studio.resources import oom_kills

    return oom_kills()


def _read_record(path: Path) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _describe_death(pid: int, record: dict, oom_now: int | None) -> str:
    oom_then = record.get("oom_kills")
    if isinstance(oom_then, int) and oom_now is not None and oom_now > oom_then:
        return (
            f"web worker pid {pid} of this launch stopped without shutting down: the "
            f"kernel's out-of-memory killer killed {oom_now - oom_then} process(es) in "
            f"this container since it started (oom_kill {oom_then} -> {oom_now}), it "
            f"most likely among them"
        )
    return (
        f"web worker pid {pid} of this launch stopped without shutting down: it was "
        f"killed (SIGKILL), by uvicorn's supervisor after it missed its health-check "
        f"ping, or by hand; the container's oom_kill count did not rise"
    )


def register_worker_and_report_lost() -> list[int]:
    """Record this worker, report the siblings gone without a clean exit. Never raises.

    Returns the pids reported. Nothing is done for a process the launcher did
    not start, or where ``/proc`` cannot tell one process from another.
    """
    path = _record_path()
    own_start = _process_start(os.getpid())
    if path is None or own_start is None:
        return []
    lost: list[int] = []
    try:
        root = _registry_root()
        for entry in root.iterdir() if root.is_dir() else ():
            if entry.name != path.parent.name:
                shutil.rmtree(entry, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        oom_now = _oom_kills()
        for sibling in path.parent.iterdir():
            if sibling == path or not sibling.name.isdigit():
                continue
            pid = int(sibling.name)
            record = _read_record(sibling)
            if record.get("start") is not None and _process_start(pid) == record.get("start"):
                continue
            logger.warning(
                "%s; the chat turns it ran ended without a reply",
                _describe_death(pid, record, oom_now),
            )
            lost.append(pid)
            sibling.unlink(missing_ok=True)
        path.write_text(json.dumps({"start": own_start, "oom_kills": oom_now}), encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not record this web worker (%s)", format_exception_message(exc))
    return lost


def unregister_worker() -> None:
    """This worker starts a clean shutdown: forget its record. Never raises."""
    path = _record_path()
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not forget this web worker (%s)", format_exception_message(exc))
