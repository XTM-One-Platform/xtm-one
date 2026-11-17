"""What a render's processes hold, by what they are, sampled while it draws; ``None`` without ``/proc``.

The render supervisor holds a render's whole process tree to its ceiling, counting each process's proportional
share of its pages (``resources.tree_memory``). A page's share of it (``render_preflight.PAGE_PEAK``) is what one
more page adds: its Chromium renderer, its encoder and the decoders of the footage it shows, and what its frame
server holds in the render process past what that process held before it drew. The rest is the process's base -
Chromium's browser and its helpers, the Playwright driver as it first ran, the render process at rest.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

from app.services.video_studio import resources


def _cmdline(pid: int) -> str:
    try:
        return Path("/proc", str(pid), "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def role(pid: int, me: int) -> str:
    """``render`` (this process), ``renderer`` (a page), ``encoder`` / ``decoder`` (ffmpeg), ``driver``, ``browser``."""
    if pid == me:
        return "render"
    command = _cmdline(pid)
    if "ffmpeg" in command.split(" ", 1)[0]:
        return "encoder" if "image2pipe" in command else "decoder"
    if "--type=renderer" in command:
        return "renderer"
    if "node" in command.split(" ", 1)[0] or "playwright" in command.split(" ", 1)[0]:
        return "driver"
    return "browser"


PAGE_ROLES = ("renderer", "encoder", "decoder")


class TreeSampler(threading.Thread):
    """The memory of this process's tree by role, every *interval* seconds, from :meth:`start` to :meth:`stop`.

    :attr:`page_peak`: the most one page held at once - its renderer, its ffmpeg, and what the render process and
    the driver grew by past their first sample - with :attr:`at_peak`, every role then; :attr:`base`: what the rest
    held at that moment. *phase*, when given, names what is being drawn at each sample (``insert`` or ``plain``):
    :attr:`peaks` is the page's peak in each.
    """

    def __init__(self, interval: float = 0.25, phase: Callable[[], str | None] | None = None) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self.phase = phase
        self.me = os.getpid()
        self.done = threading.Event()
        self.samples = 0
        self.page_peak: int | None = None
        self.tree_peak: int | None = None
        self.base: int | None = None
        self.at_peak: dict[str, int] = {}
        self.peaks: dict[str, int] = {}
        self.render_rest = resources.tree_memory(self.me, pids=[self.me])
        self._driver_first: int | None = None

    def sample(self) -> dict[str, int]:
        held: dict[str, int] = {}
        for pid in resources.process_tree(self.me):
            memory = resources.tree_memory(pid, pids=[pid])
            if memory is not None:
                name = role(pid, self.me)
                held[name] = held.get(name, 0) + memory
        return held

    def _take(self) -> None:
        held = self.sample()
        if not held:
            return
        self.samples += 1
        if "driver" in held and self._driver_first is None:
            self._driver_first = held["driver"]
        grown = max(0, held.get("render", 0) - (self.render_rest or 0))
        driver_grown = max(0, held.get("driver", 0) - (self._driver_first or held.get("driver", 0)))
        page = sum(held.get(name, 0) for name in PAGE_ROLES) + grown + driver_grown
        total = sum(held.values())
        self.tree_peak = max(self.tree_peak or 0, total)
        drawing = self.phase() if self.phase is not None else None
        if drawing:
            self.peaks[drawing] = max(self.peaks.get(drawing, 0), page)
        if page > (self.page_peak or 0):
            self.page_peak = page
            self.base = total - page
            self.at_peak = dict(held)

    def run(self) -> None:
        while not self.done.wait(self.interval):
            self._take()

    def stop(self) -> None:
        self.done.set()
        self.join()

    def said(self) -> str:
        parts = ", ".join(f"{name} {resources.format_bytes(n)}" for name, n in sorted(self.at_peak.items(), key=lambda i: -i[1]))
        phases = ", ".join(f"{name} {resources.format_bytes(n)}" for name, n in sorted(self.peaks.items()))
        return (
            f"page peak {resources.format_bytes(self.page_peak)}{f' ({phases})' if phases else ''} "
            f"(tree peak {resources.format_bytes(self.tree_peak)}, "
            f"base {resources.format_bytes(self.base)}, render process at rest {resources.format_bytes(self.render_rest)}; "
            f"at the peak: {parts}; {self.samples} samples)"
        )
