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

"""What a render may use and what it uses: the container's CPUs and memory, a process tree's memory, the CPU
priority a job beside a render starts at (:func:`niced`).

A render runs next to the platform's own worker, in one container. Two numbers
of that container bound it:

* :func:`cpu_budget` - ffmpeg and x264 size their thread pools from the CPUs
  they may be scheduled on, which in a container is the whole node: on a large
  node every encoder of a 2-CPU worker would start dozens of threads, each
  holding frames, for throughput the quota never grants;
* :func:`memory_limit` - the container's memory limit, from which the render
  process's own ceiling is derived (``render_process.memory_ceiling``).

:func:`tree_memory` measures a process and everything it started (the render
process, its Chromium, its ffmpeg encoders and decoders), the number the render
supervisor holds to that ceiling and the stage logs report. It reads ``/proc``
and answers ``None`` where there is none.
"""

from __future__ import annotations

import functools
import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

_PROC = "/proc"
_CGROUP_V2_CPU_MAX = "/sys/fs/cgroup/cpu.max"
_CGROUP_V1_QUOTA = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
_CGROUP_V1_PERIOD = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
_CGROUP_V2_MEMORY_MAX = "/sys/fs/cgroup/memory.max"
_CGROUP_V1_MEMORY_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
_CGROUP_V2_MEMORY_CURRENT = "/sys/fs/cgroup/memory.current"
_CGROUP_V1_MEMORY_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
_CGROUP_V2_MEMORY_EVENTS = "/sys/fs/cgroup/memory.events"
_CGROUP_V1_OOM_CONTROL = "/sys/fs/cgroup/memory/memory.oom_control"
_CGROUP_V2_CPU_STAT = "/sys/fs/cgroup/cpu.stat"
_CGROUP_V1_CPU_STAT = "/sys/fs/cgroup/cpu/cpu.stat"
_CGROUP_V1_CPU_USAGE = ("/sys/fs/cgroup/cpuacct/cpuacct.usage", "/sys/fs/cgroup/cpu,cpuacct/cpuacct.usage")
#: cgroup v1 writes "no limit" as a page-rounded 2^63: anything past this is none.
_NO_LIMIT = 1 << 60


def _cgroup_cpu_quota() -> float | None:
    """The CPUs the container's quota grants, or ``None`` when it sets none."""
    try:
        quota, period = Path(_CGROUP_V2_CPU_MAX).read_text().split()[:2]
        return None if quota == "max" else int(quota) / int(period)
    except (OSError, ValueError):
        pass
    try:
        quota = int(Path(_CGROUP_V1_QUOTA).read_text())
        period = int(Path(_CGROUP_V1_PERIOD).read_text())
        return quota / period if quota > 0 and period > 0 else None
    except (OSError, ValueError):
        return None


@functools.cache
def cpu_budget() -> int:
    """The CPUs this process may actually use, the container's quota included."""
    available = os.process_cpu_count() or 1
    quota = _cgroup_cpu_quota()
    if quota is not None:
        available = min(available, math.ceil(quota))
    return max(1, available)


def memory_limit() -> int | None:
    """The container's memory limit in bytes, or ``None`` when it sets none."""
    for path in (_CGROUP_V2_MEMORY_MAX, _CGROUP_V1_MEMORY_LIMIT):
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        return value if 0 < value < _NO_LIMIT else None
    return None


def memory_usage() -> int | None:
    """What the container uses now, in bytes (its cgroup's count, the page cache included); ``None`` without one."""
    for path in (_CGROUP_V2_MEMORY_CURRENT, _CGROUP_V1_MEMORY_USAGE):
        try:
            return int(Path(path).read_text().strip())
        except (OSError, ValueError):
            continue
    return None


def oom_kills() -> int | None:
    """How many processes the kernel's out-of-memory killer has killed in this container; ``None`` unknown.

    A process it kills gets SIGKILL, like one killed by hand: this count, read
    before and after, is what tells the two apart.
    """
    for path in (_CGROUP_V2_MEMORY_EVENTS, _CGROUP_V1_OOM_CONTROL):
        try:
            lines = Path(path).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            name, _, value = line.partition(" ")
            if name == "oom_kill":
                try:
                    return int(value)
                except ValueError:
                    return None
    return None


@dataclass(frozen=True)
class CpuStat:
    """The container's CPU since it started: the CPU seconds its processes used, and the scheduling periods
    of its CPU quota, those in which the quota held it back and for how long."""

    usage_seconds: float | None
    periods: int = 0
    throttled: int = 0
    throttled_seconds: float = 0.0


def _stat_fields(path: str) -> dict[str, int] | None:
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return None
    found = {}
    for line in lines:
        name, _, value = line.partition(" ")
        try:
            found[name] = int(value)
        except ValueError:
            continue
    return found


def cpu_stat() -> CpuStat | None:
    """The container's :class:`CpuStat` (cgroup v2 ``cpu.stat``, else v1's ``cpu.stat`` and ``cpuacct.usage``);
    ``None`` where neither says."""
    fields = _stat_fields(_CGROUP_V2_CPU_STAT)
    if fields is not None and "usage_usec" in fields:
        return CpuStat(
            fields["usage_usec"] / 1e6,
            fields.get("nr_periods", 0),
            fields.get("nr_throttled", 0),
            fields.get("throttled_usec", 0) / 1e6,
        )
    fields = _stat_fields(_CGROUP_V1_CPU_STAT)
    if fields is None:
        return None
    usage = None
    for path in _CGROUP_V1_CPU_USAGE:
        try:
            usage = int(Path(path).read_text().strip()) / 1e9
            break
        except (OSError, ValueError):
            continue
    throttled = fields.get("throttled_time", 0) / 1e9
    return CpuStat(usage, fields.get("nr_periods", 0), fields.get("nr_throttled", 0), throttled)


def cpu_pressure_seconds(path: str | Path) -> float | None:
    """The seconds at least one task of a cgroup waited for a CPU since it started: its ``cpu.pressure``'s
    ``some ... total=`` (microseconds, the kernel's PSI); ``None`` where it does not say."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.startswith("some "):
            continue
        for field in line.split()[1:]:
            name, _, value = field.partition("=")
            if name == "total":
                try:
                    return int(value) / 1e6
                except ValueError:
                    return None
    return None


def tree_cpu_seconds(pid: int) -> float | None:
    """The CPU seconds *pid* and every live process it started used, each with those of its children it
    waited for (``utime``, ``stime``, ``cutime``, ``cstime``); ``None`` without ``/proc``."""
    if not os.path.isdir(_PROC):
        return None
    try:
        ticks = float(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):
        return None
    total, measured = 0, False
    for member in process_tree(pid):
        try:
            fields = Path(_PROC, str(member), "stat").read_text().rsplit(")", 1)[1].split()
            total += sum(int(value) for value in fields[11:15])
            measured = True
        except (OSError, ValueError, IndexError):
            continue
    return total / ticks if measured and ticks > 0 else None


def niced(command: list[str], nice: int) -> list[str]:
    """*command* started *nice* steps below the worker's CPU priority: ``nice -n``, which everything it starts
    inherits; *command* as it is for *nice* 0, or where the system has no ``nice``.

    The kernel shares a busy container's CPUs by runnable thread, each weighed by its priority: at 10 steps a thread
    weighs a tenth of one at the worker's, so what runs so takes the CPU the others leave.
    """
    binary = _nice_binary() if nice > 0 else None
    return [binary, "-n", str(int(nice)), *command] if binary else list(command)


def _nice_binary() -> str | None:
    return shutil.which("nice") if os.name == "posix" else None


def host_memory() -> int | None:
    """The host's physical memory in bytes, or ``None`` where the system does not say."""
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, ValueError, OSError):
        return None


def _children() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir(_PROC)
    except OSError:
        return children
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            stat = Path(_PROC, entry, "stat").read_text()
            # The command name may hold spaces and parentheses: the fields after the last ")".
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    return children


def process_tree(pid: int) -> list[int]:
    """*pid* and every live process it started, directly or not (``[pid]`` without ``/proc``)."""
    children = _children()
    found, todo = [], [pid]
    while todo:
        current = todo.pop()
        if current in found:
            continue
        found.append(current)
        todo.extend(children.get(current, ()))
    return found


def process_start(pid: int) -> int | None:
    """When *pid* started, in clock ticks since boot: what tells one process from another given its number later."""
    try:
        return int(Path(_PROC, str(pid), "stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def snapshot_tree(pid: int) -> dict[int, int | None]:
    """:func:`process_tree` with each member's start time; empty without ``/proc``."""
    if not os.path.isdir(_PROC):
        return {}
    return {member: process_start(member) for member in process_tree(pid)}


def _process_memory(pid: int) -> int | None:
    """One process's memory: its proportional share (PSS) when the kernel says it, else its RSS.

    Chromium's processes share most of their pages: summing their RSS counts
    those pages once per process and would stop a render that fits.
    """
    try:
        for line in Path(_PROC, str(pid), "smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        for line in Path(_PROC, str(pid), "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def tree_memory(pid: int, pids: list[int] | None = None) -> int | None:
    """The memory of *pid* and its descendants (or of *pids*), in bytes; ``None`` without ``/proc``."""
    total, measured = 0, False
    for member in pids if pids is not None else process_tree(pid):
        value = _process_memory(member)
        if value is not None:
            total += value
            measured = True
    return total if measured else None


@functools.cache
def _libc() -> object | None:
    try:
        import ctypes

        return ctypes.CDLL("libc.so.6")
    except OSError:
        return None


def release_freed_memory() -> bool:
    """Give back to the system the memory this process freed: ``malloc_trim(0)`` where glibc offers it.

    The C library keeps what Python and numpy free in its heaps for reuse: a render process composing a 2160p
    screen insert grew 1.6 GiB, and held it through the scenes after. ``False`` where there is no such call.
    """
    libc = _libc()
    trim = getattr(libc, "malloc_trim", None)
    if trim is None:
        return False
    return bool(trim(0))


def format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value >= 1 << 30:
        return f"{value / (1 << 30):.1f} GiB"
    return f"{value / (1 << 20):.0f} MiB"
