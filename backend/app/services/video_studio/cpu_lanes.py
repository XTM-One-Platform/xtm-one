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

"""The CPU share a whole render keeps against every other job on its worker.

A final render's 8 pages got 4.0 of 8 CPUs beside 6 assignment jobs and a scene preview: only the scene previews and
the recordings' encodes ran below a render's priority (``cpu_share``), and the reconciler's shared pool -
assignment runs, sub-agent tasks, evaluations - at the render's own. A whole render's process (its pages, its
encoders, everything it starts) now draws in a *render lane* (:func:`place`), the first of these its worker can give
(:func:`lane`):

* **cgroup** - where the container's cgroup v2 is delegated to it (writable, the ``cpu`` controller offered): the
  container's processes move to a :data:`WORKER_LEAF` and a render's to a :data:`RENDER_LEAF` whose ``cpu.weight``
  is ``VIDEO_STUDIO_RENDER_CPU_WEIGHT`` against the worker leaf's :data:`DEFAULT_WEIGHT`. On a busy CPU the render's
  tree gets that share of it however many threads the rest runs. A process later started in the container
  (``docker exec``, ``kubectl exec``) joins its init's leaf - runc's fallback on cgroup v2.
* **priority** - else, where the worker may raise a process's priority (``CAP_SYS_NICE``, or an ``RLIMIT_NICE``
  allowing it): the render's process starts :func:`raised_nice` steps above the worker's, the same ratio of weights
  thread for thread (``cpu_share.weight``).
* **none** - else the render keeps the worker's priority; :attr:`Lane.reason` says why and what gives it one.

:func:`expected` says which lane a worker gives without changing anything - what its render capacity publishes
(``render_preflight.publish_worker``) and the pre-flight prices with (``cpu_share.priced``); :func:`lane` sets it up,
once, at the worker's first render. :func:`own_pressure` reads how long the render's tasks waited for a CPU.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

ROOT = "/sys/fs/cgroup"
#: The leaves the container's cgroup is split into: everything of the worker's, and its whole renders.
WORKER_LEAF = "xtm-worker"
RENDER_LEAF = "xtm-render"
#: cgroup v2's ``cpu.weight`` of a cgroup nobody weighed: the worker leaf's.
DEFAULT_WEIGHT = 100
MAX_WEIGHT = 10000
#: ``CAP_SYS_NICE``'s bit in ``/proc/self/status``'s ``CapEff``.
_CAP_SYS_NICE = 23
_STATUS = "/proc/self/status"

CGROUP = "cgroup"
PRIORITY = "priority"
NONE = "none"


def configured_weight() -> int:
    """``VIDEO_STUDIO_RENDER_CPU_WEIGHT`` within cgroup v2's bounds; ``0``: no render lane."""
    value = int(settings.video_studio_render_cpu_weight or 0)
    return 0 if value <= 0 else min(MAX_WEIGHT, max(1, value))


def raised_nice(weight: int) -> int:
    """The niceness that weighs a thread *weight* / :data:`DEFAULT_WEIGHT` times the worker's (a step is 1.25x):
    ``-10`` for 1000; ``0`` for a weight no higher than the worker's."""
    if weight <= DEFAULT_WEIGHT:
        return 0
    return -min(19, round(math.log(weight / DEFAULT_WEIGHT) / math.log(1.25)))


@dataclass(frozen=True)
class Lane:
    """Where a worker's whole renders draw (:func:`lane`): their cgroup leaf and its weight, a raised priority, or
    the worker's own priority and why."""

    kind: str
    weight: int = DEFAULT_WEIGHT
    nice: int = 0
    reason: str = ""

    def words(self) -> str:
        """What a pre-flight says of it: ``its pages draw in a render lane of their own (cgroup cpu.weight 1000
        against the 100 of the worker's other jobs)``."""
        if self.kind == CGROUP:
            return (
                f"its pages draw in a render lane of their own (cgroup cpu.weight {self.weight} against the "
                f"{DEFAULT_WEIGHT} of the worker's other jobs)"
            )
        if self.kind == PRIORITY:
            return f"its pages draw at a raised priority (nice {self.nice}, the worker's other jobs at 0)"
        return f"its pages draw at the worker's own priority, as its assignment jobs do: {self.reason}"

    def as_dict(self) -> dict:
        return {"kind": self.kind, "weight": self.weight, "nice": self.nice, "reason": self.reason}

    @classmethod
    def of(cls, value: Any) -> Lane | None:
        if not isinstance(value, dict) or value.get("kind") not in (CGROUP, PRIORITY, NONE):
            return None
        try:
            return cls(
                str(value["kind"]),
                int(value.get("weight") or DEFAULT_WEIGHT),
                int(value.get("nice") or 0),
                str(value.get("reason") or ""),
            )
        except (TypeError, ValueError):
            return None


#: What gives a worker a render lane, said with one it does not have.
REMEDY = (
    "delegate the worker container's cgroup to it (a writable /sys/fs/cgroup, cgroup v2) or grant it CAP_SYS_NICE "
    "(Compose: cap_add: [SYS_NICE]; Kubernetes: securityContext.capabilities.add: [SYS_NICE])"
)


def _cgroup_refusal() -> str | None:
    """Why the container's cgroup gives no render lane; ``None`` when it can."""
    root = Path(ROOT)
    try:
        controllers = (root / "cgroup.controllers").read_text().split()
    except OSError:
        return "the container has no cgroup v2"
    if "cpu" not in controllers:
        return "its cgroup offers no cpu controller"
    if not (os.access(root / "cgroup.subtree_control", os.W_OK) and os.access(root / "cgroup.procs", os.W_OK)):
        return "its cgroup is read-only"
    return None


def _cap_sys_nice() -> bool:
    try:
        for line in Path(_STATUS).read_text().splitlines():
            if line.startswith("CapEff:"):
                return bool(int(line.split()[1], 16) >> _CAP_SYS_NICE & 1)
    except (OSError, ValueError, IndexError):
        pass
    return False


def _may_raise(nice: int) -> bool:
    """Whether this process may start another at *nice* (negative): ``CAP_SYS_NICE``, or ``RLIMIT_NICE`` past it."""
    if os.name != "posix" or nice >= 0:
        return nice >= 0
    if _cap_sys_nice():
        return True
    try:
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_NICE)
    except (ImportError, AttributeError, OSError, ValueError):
        return False
    return soft == resource.RLIM_INFINITY or 20 - soft <= nice


def _expected(weight: int) -> Lane:
    if weight <= 0:
        return Lane(NONE, reason="VIDEO_STUDIO_RENDER_CPU_WEIGHT is 0")
    cgroup = _cgroup_refusal()
    if cgroup is None:
        return Lane(CGROUP, weight=weight)
    nice = raised_nice(weight)
    if nice < 0 and _may_raise(nice):
        return Lane(PRIORITY, weight=weight, nice=nice)
    return Lane(NONE, reason=f"{cgroup}, and the worker may not raise a priority - {REMEDY}")


_expected_lane: Lane | None = None
_lane: Lane | None = None


def expected() -> Lane:
    """The lane this worker's whole renders draw in (:func:`lane` once set up), read without changing anything."""
    global _expected_lane
    if _lane is not None:
        return _lane
    if _expected_lane is None:
        _expected_lane = _expected(configured_weight())
    return _expected_lane


def _move_all(source: Path, target: Path) -> None:
    """Every process of *source* into *target*: one written at a time, those gone meanwhile skipped."""
    for pid in (source / "cgroup.procs").read_text().split():
        try:
            (target / "cgroup.procs").write_text(pid)
        except OSError as exc:
            if exc.errno not in (errno.ESRCH, errno.EINVAL):
                raise


def _set_up_cgroup(weight: int) -> str | None:
    """The container's cgroup split into :data:`WORKER_LEAF` and :data:`RENDER_LEAF` at *weight*; why not, else.

    A cgroup that hands its ``cpu`` controller to its children holds no process itself: the container's go to the
    worker leaf first, again on ``EBUSY`` (one started meanwhile)."""
    root = Path(ROOT)
    try:
        for leaf in (WORKER_LEAF, RENDER_LEAF):
            (root / leaf).mkdir(exist_ok=True)
        for _ in range(3):
            _move_all(root, root / WORKER_LEAF)
            try:
                if "cpu" not in (root / "cgroup.subtree_control").read_text().split():
                    (root / "cgroup.subtree_control").write_text("+cpu")
                break
            except OSError as exc:
                if exc.errno != errno.EBUSY:
                    raise
        else:
            return "processes kept joining the container's own cgroup"
        (root / RENDER_LEAF / "cpu.weight").write_text(str(weight))
    except OSError as exc:
        return f"its cgroup refused the render lane ({format_exception_message(exc)})"
    return None


def lane() -> Lane:
    """The lane this worker's whole renders draw in, set up once (:func:`expected` says which it will be): a cgroup
    leaf that cannot be made falls back to a raised priority, then to none, said once in the worker's log."""
    global _lane
    if _lane is not None:
        return _lane
    found = expected()
    if found.kind == CGROUP and (refused := _set_up_cgroup(found.weight)) is not None:
        nice = raised_nice(found.weight)
        found = (
            Lane(PRIORITY, weight=found.weight, nice=nice)
            if nice < 0 and _may_raise(nice)
            else Lane(NONE, reason=f"{refused}, and the worker may not raise a priority - {REMEDY}")
        )
    _lane = found
    logger.info("Video Studio render lane: %s", found.words())
    return found


def place(pid: int) -> bool:
    """Put the whole render's process *pid* in this worker's render lane before it starts its pages: in its
    cgroup leaf, or at its raised priority; whether it was. What it starts after inherits it."""
    found = lane()
    try:
        if found.kind == CGROUP:
            Path(ROOT, RENDER_LEAF, "cgroup.procs").write_text(str(int(pid)))
            return True
        if found.kind == PRIORITY:
            os.setpriority(os.PRIO_PROCESS, int(pid), found.nice)
            return True
    except (OSError, AttributeError) as exc:
        logger.info("Video render process %s not put in the render lane: %s", pid, format_exception_message(exc))
    return False


def _threads(pid: int) -> list[int]:
    """The threads of the running process *pid* (its own id alone where ``/proc`` does not list them)."""
    try:
        return [int(name) for name in os.listdir(f"/proc/{int(pid)}/task")]
    except (OSError, ValueError):
        return [int(pid)]


def lift(pids: list[int]) -> Callable[[], None]:
    """Put the RUNNING processes *pids* (a browser and its renderers) in this worker's render lane, the way
    :func:`place` does for a render about to start; answer what puts them back.

    A process already running holds threads of its own: the cgroup lane moves each process whole (``cgroup.procs``)
    and moves it back to :data:`WORKER_LEAF`; the priority lane raises every thread (``setpriority`` on Linux sets
    one thread) and gives each its niceness back. A process gone meanwhile is skipped; nothing is raised."""
    found = lane()
    moved: list[tuple[int, int]] = []
    for pid in pids:
        try:
            if found.kind == CGROUP:
                Path(ROOT, RENDER_LEAF, "cgroup.procs").write_text(str(int(pid)))
                moved.append((int(pid), 0))
            elif found.kind == PRIORITY:
                for tid in _threads(pid):
                    before = os.getpriority(os.PRIO_PROCESS, tid)
                    os.setpriority(os.PRIO_PROCESS, tid, found.nice)
                    moved.append((tid, before))
        except (OSError, AttributeError) as exc:
            logger.info("Process %s not lifted to the render lane: %s", pid, format_exception_message(exc))

    def restore() -> None:
        for ident, before in moved:
            with contextlib.suppress(OSError, AttributeError):
                if found.kind == CGROUP:
                    Path(ROOT, WORKER_LEAF, "cgroup.procs").write_text(str(ident))
                else:
                    os.setpriority(os.PRIO_PROCESS, ident, before)

    return restore


def own_pressure_file() -> tuple[Path, bool] | None:
    """``(this process's cgroup's cpu.pressure, whether that cgroup is the render lane)``; ``None`` without one."""
    try:
        line = next(
            (entry for entry in Path("/proc/self/cgroup").read_text().splitlines() if entry.startswith("0::")), None
        )
    except OSError:
        return None
    if line is None:
        return None
    relative = line[3:].strip().strip("/")
    path = Path(ROOT, relative, "cpu.pressure") if relative else Path(ROOT, "cpu.pressure")
    if not path.exists():
        path = Path(ROOT, "cpu.pressure")
        if not path.exists():
            return None
    return path, relative.endswith(RENDER_LEAF)


def own_pressure() -> tuple[float, bool] | None:
    """``(the seconds a task of this process's cgroup waited for a CPU, whether that cgroup is the render lane)``
    (``resources.cpu_pressure_seconds``); ``None`` where the kernel does not say."""
    from app.services.video_studio import resources

    found = own_pressure_file()
    if found is None:
        return None
    seconds = resources.cpu_pressure_seconds(found[0])
    return (seconds, found[1]) if seconds is not None else None
