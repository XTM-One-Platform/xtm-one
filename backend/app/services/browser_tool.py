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

"""Built-in browser automation tool using Playwright.

Provides agents with a headless browser they can control to navigate web
pages, extract content, fill forms, click elements, take screenshots, and
evaluate JavaScript.

Session management:
- Each (agent, conversation) pair gets its own browser context.
- Sessions are lazily created on first use and automatically closed after
  an inactivity timeout (default 5 minutes).
- All browser operations are protected by a per-session asyncio lock to
  prevent concurrent page manipulation.
- A session may instead be signed in with a browser profile
  (:func:`install_session`, opened by ``browser_profiles.agent_sessions``):
  it runs under the profile's URL policy and saves its refreshed session when
  it is released.
- A session without a profile keeps its cookies, local storage, viewport,
  emulated media features and last URL after each navigation: a session re-created for the
  same key, in this process or in another one (a run resumed after a rollout),
  starts from them, encrypted in Redis between processes.

URL policy (SSRF):
- ``browser_navigate`` validates every explicit navigation target with the
  shared :func:`app.services.web_fetch.validate_outbound_url` validator:
  http/https schemes only, and every DNS-resolved address must be globally
  routable (``ip.is_global``).  Blocked URLs return a refusal string.
- The one exception is the preview of a sandbox service an agent started
  (``sandbox_workspace/preview.py``): a URL on the platform's own
  OpenSandbox server proxy, for a registered sandbox and port only. Its
  requests carry the server's key, added by the route filter.
- As defense in depth, each browser context registers a request route
  filter that aborts requests targeting non-http(s) schemes or literal
  non-global IP hosts (e.g. the cloud metadata endpoint 169.254.169.254).
  JS-initiated navigations cannot be fully intercepted, so the explicit
  navigate gate is the primary control.
- Every redirect hop of every request the session's Chromium makes (a
  navigation, an iframe, a subresource, a fetch / XHR, a worker's request)
  is held to the same validator before it is sent (:class:`RedirectGuard`):
  Playwright runs a route handler for the first URL of a request only.
- A document the page moved to while the tool read it (a navigation race)
  is read again only when the same validator allows its URL.

Dependencies:
- ``playwright`` (async) — must be installed via ``pip install playwright``
  and browsers installed via ``playwright install chromium``.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import ipaddress
import json
import logging
import math
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from app.core.utils import format_exception_message
from app.services.sandbox_workspace import preview as sandbox_preview
from app.services.sandbox_workspace.preview import allowed as preview_allowed
from app.services.sandbox_workspace.preview import request_headers as preview_headers
from app.services.web_fetch import validate_outbound_url_async

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

# ── Session timeout ──────────────────────────────────────────────────────────

SESSION_TIMEOUT_SECONDS = 300  # 5 minutes of inactivity
MAX_PAGE_CONTENT_CHARS = 50_000
#: How long the ``snapshot`` action waits for the element its ``selector`` names.
SNAPSHOT_SELECTOR_TIMEOUT_MS = 5_000
#: Ceiling on an ``evaluate`` result. A script that returns
#: ``canvas.toDataURL()`` puts a 200 KB base64 blob into the model's context
#: for nothing it can read; the cut says so and names the tools that carry an
#: image as a file instead.
MAX_EVALUATE_RESULT_CHARS = 20_000
NAVIGATION_TIMEOUT_MS = 30_000
#: The longest a ``navigate`` may be asked to wait for its page (a dev server compiling a route on first visit).
MAX_NAVIGATION_TIMEOUT_SECONDS = 120.0
ACTION_TIMEOUT_MS = 10_000

#: Playwright defaults the agent's browser leaves out of its launch: classic scrollbars stay painted.
BROWSER_SHOWN_DEFAULT_ARGS: tuple[str, ...] = ("--hide-scrollbars",)
#: How long an ``evaluate`` script may run before it is stopped, by default and at most: a page's long task
#: (a canvas export) otherwise holds the session for the tool's whole budget.
EVALUATE_TIMEOUT_SECONDS = 30.0
MAX_EVALUATE_TIMEOUT_SECONDS = 120.0
#: How long the cookies and local storage of a session closed or lost are kept for the session re-created after it.
KEPT_STATE_SECONDS = 3600.0
#: The longest a read of a session's cookies and local storage may take.
_STATE_READ_SECONDS = 5.0
#: Where a session's kept state is shared with the other backend processes: a run resumed in another
#: process (a rollout restarting the worker) re-creates its session from it instead of an empty one at about:blank.
_SHARED_STATE_PREFIX = "browser:kept_state:"
#: This backend process, as the writer named in the states it shares: a process reading a shared state back
#: tells its own copy from one another process kept after the run went on there.
_PROCESS_ID = uuid.uuid4().hex
#: The largest kept state shared through Redis, encrypted; a larger one stays in this process only.
_MAX_SHARED_STATE_BYTES = 1_000_000
#: Redirect hops of one session checked at the same time. A check resolves the
#: hop's name on the process's default executor, which asyncpg shares for the
#: database's own DNS: a page decides how many hops it makes, so it must
#: not decide how many resolver threads run.
MAX_CONCURRENT_HOP_CHECKS = 4
#: The page size a session opens with, and the bounds of the ``viewport`` action.
DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
VIEWPORT_WIDTH_BOUNDS = (320, 3840)
VIEWPORT_HEIGHT_BOUNDS = (240, 2160)
#: The device pixel ratios the ``viewport`` action takes (``scale``): 2 captures a 1440 x 900 page at 2880 x 1800.
VIEWPORT_SCALE_BOUNDS = (1, 3)
#: The values the ``emulate_media`` action takes for each CSS media feature it emulates, by its argument name; a
#: feature not emulated answers what the browser reports by default (no reduced motion, light).
MEDIA_FEATURES: dict[str, tuple[str, ...]] = {
    "reduced_motion": ("reduce", "no-preference"),
    "color_scheme": ("light", "dark", "no-preference"),
}
#: The tallest page a ``screenshot`` of one element grows the viewport to, for the capture only: a page renders
#: what intersects its viewport (in-view effects, lazy lists), so an element taller than the viewport is captured
#: with the viewport as tall as the element, up to this height, then the viewport is restored.
MAX_ELEMENT_CAPTURE_HEIGHT = 8_192
#: The ``record`` action: how long it records by default and at most (seconds), the frame rate of the MP4 it
#: answers, and the JPEG quality of the screencast frames. Chromium paints the frames off the page's main thread,
#: but reading back and encoding each one costs the CPUs the page renders on: a recording asks for every second
#: compositor frame (60 Hz / 2 = :data:`RECORD_FPS`, never a frame the MP4 would drop) at most
#: :data:`RECORD_MAX_WIDTH` wide at JPEG 60, so the page keeps its own rate (measured at 1728x1000 on one CPU:
#: 1280 px at JPEG 70 kept 80 % of the page's rAF rate, 960 px at JPEG 60 kept 99 %). On a CPU the browser shares
#: with busy jobs no size is enough (23 % lost even at 1.5 frames per second): the browser's processes draw in the
#: worker's render lane while it records (:func:`_lift_browser`).
DEFAULT_RECORD_SECONDS = 6.0
RECORD_SECONDS_BOUNDS = (1.0, 30.0)
RECORD_FPS = 30
RECORD_MAX_WIDTH = 960
_RECORD_EVERY_NTH_FRAME = 2
_RECORD_JPEG_QUALITY = 60
#: The frames a recording keeps at most per second recorded: a page cannot paint faster than its display rate.
_RECORD_MAX_FRAMES_PER_SECOND = 120
#: How long an element capture waits for the grown page to paint (two animation frames) before it captures anyway.
_ELEMENT_PAINT_SECONDS = 2.0
#: The height a ``full_page`` capture needs: the document's scroll height, or, for an app shell that scrolls inside
#: its own container (a fixed sidebar and top bar, ``height: 100vh`` and ``overflow: auto`` below), the viewport
#: plus what the tallest such container hides. A capture stitched past the viewport paints ``position: fixed``
#: elements where the viewport was, over the content; a viewport grown to this height paints them once, at the top.
_FULL_PAGE_HEIGHT_JS = """() => {
  const vh = window.innerHeight;
  let need = Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0);
  for (const el of document.querySelectorAll('body *')) {
    if (el.scrollHeight <= el.clientHeight + 1) continue;
    if (!/(auto|scroll|overlay)/.test(getComputedStyle(el).overflowY)) continue;
    need = Math.max(need, vh + el.scrollHeight - el.clientHeight);
  }
  return Math.ceil(need);
}"""
#: Distinct console messages a session keeps since its last ``navigate`` (a repeat is folded into its first
#: occurrence and counted): the oldest distinct one goes first past it, its messages counted.
MAX_CONSOLE_MESSAGES = 200
#: The longest text of one kept console message.
MAX_CONSOLE_TEXT_CHARS = 500
#: The longest answer of the ``console`` action: the lines past it are not shown, and counted.
MAX_CONSOLE_ANSWER_CHARS = 8_000
#: The ``level`` the ``console`` action accepts: the least severe message kind it shows.
CONSOLE_LEVELS = ("error", "warning")
#: Message kinds by severity: 0 errors (an uncaught page error, ``console.error``, a failed ``console.assert``),
#: 1 warnings, 2 everything else.
_CONSOLE_SEVERITY = {"pageerror": 0, "error": 0, "assert": 0, "warning": 1}

_DATA_URL_PREFIX_RE_SOURCE = r"data:image/[a-zA-Z0-9.+-]+;base64,"

#: Playwright's rewrite of every lost in-page execution context: the page
#: navigated (or reloaded its frame) while a script ran or was dispatched.
#: Anchored after the optional ``Page.evaluate: `` API prefix so a script that
#: throws an error quoting these words is still the script's own error.
_NAVIGATION_RACE_RE = re.compile(r"^(?:\w+\.\w+: )?Execution context was destroyed")
_PLAYWRIGHT_TIMEOUT_RE = re.compile(r"^(?:\w+\.\w+: )?Timeout (\d+)ms exceeded")
#: Steps of Playwright's click call log: the click was dispatched, and it completed.
_CLICK_PERFORMING_RE = re.compile(r"^\s*- performing click action\s*$", re.MULTILINE)
_CLICK_DONE_RE = re.compile(r"^\s*- click action done\s*$", re.MULTILINE)

#: The page's text, title and URL in ONE script, so the three describe the same document.
_PAGE_CONTENT_JS = (
    "() => ({url: location.href, title: String(document.title), "
    "text: document.body ? String(document.body.innerText) : ''})"
)
_PAGE_URL_AND_TITLE_JS = "() => [location.href, String(document.title)]"
#: Where the document says it is: after a navigate, the place the next script runs.
_PAGE_HREF_JS = "() => location.href"
#: When the document was created (epoch milliseconds): tells a new document from the one a navigate left.
_DOCUMENT_ORIGIN_JS = "() => performance.timeOrigin"


class DocumentTracker:
    """Counts the documents a session's page commits and loads.

    Playwright rejects a script with the navigation race before its client has
    processed the commit of the new document: at that moment ``page.url`` still
    names the old document and ``wait_for_load_state("domcontentloaded")``
    answers at once from the old document's state. A main-frame commit followed
    by its DOMContentLoaded is what says the new document is there.
    """

    def __init__(self) -> None:
        self.committed = 0
        self._loaded = 0
        self._changed = asyncio.Event()

    def watch(self, page: Any) -> None:
        page.on("framenavigated", self._on_navigated)
        page.on("domcontentloaded", self._on_loaded)

    def _on_navigated(self, frame: Any) -> None:
        if frame.parent_frame is None:
            self.committed += 1

    def _on_loaded(self, _page: Any) -> None:
        self._loaded = self.committed
        self._changed.set()

    async def loaded_after(self, mark: int, timeout: float) -> bool:
        """Whether a document committed after *mark* reaches DOMContentLoaded within *timeout* seconds."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._loaded <= mark:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except TimeoutError:
                return self._loaded > mark
        return True

    def loaded_since(self, mark: int) -> bool:
        """Whether a document committed after *mark* already reached DOMContentLoaded."""
        return self._loaded > mark


def _read(value: Any) -> Any:
    """A Playwright property, or the method an older release exposed in its place."""
    return value() if callable(value) else value


@dataclass
class ConsoleEntry:
    """One distinct console message: its kind, its (cut) text, where it was first logged and how many times."""

    kind: str
    text: str
    where: str
    count: int = 1

    @property
    def severity(self) -> int:
        return _CONSOLE_SEVERITY.get(self.kind, 2)

    def line(self) -> str:
        repeats = f" x{self.count}" if self.count > 1 else ""
        return f"[{self.kind}]{repeats} {self.text}" + (f" ({self.where})" if self.where else "")


class ConsoleLog:
    """What a session's page logged since the last ``navigate`` action: console messages and page errors.

    A message identical to one already kept (same kind, same text) is folded into it and counted, so a page that
    logs one error on every render keeps it once. At most :data:`MAX_CONSOLE_MESSAGES` distinct messages are kept
    in the order first logged (the oldest dropped first, its messages counted), each text cut at
    :data:`MAX_CONSOLE_TEXT_CHARS`. A page's own words: the ``console`` action returns them as data.
    """

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], ConsoleEntry] = {}
        self.dropped = 0

    def watch(self, page: Any) -> None:
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)

    def clear(self) -> None:
        self.entries.clear()
        self.dropped = 0

    def _add(self, kind: str, text: str, where: str) -> None:
        cut = text if len(text) <= MAX_CONSOLE_TEXT_CHARS else text[:MAX_CONSOLE_TEXT_CHARS] + " [cut]"
        kept = self.entries.get((kind, cut))
        if kept is not None:
            kept.count += 1
            return
        if len(self.entries) >= MAX_CONSOLE_MESSAGES:
            oldest = next(iter(self.entries))
            self.dropped += self.entries.pop(oldest).count
        self.entries[(kind, cut)] = ConsoleEntry(kind, cut, where)

    def _on_console(self, message: Any) -> None:
        try:
            kind = str(_read(getattr(message, "type", "log")) or "log")
            text = str(_read(getattr(message, "text", "")) or "")
            location = _read(getattr(message, "location", None)) or {}
            url = str(location.get("url") or "") if isinstance(location, dict) else ""
            line = location.get("lineNumber") if isinstance(location, dict) else None
        except Exception as exc:  # an event Playwright shaped differently is skipped, never raised into its emitter
            logger.debug("Console message not read: %s", format_exception_message(exc))
            return
        self._add(kind, text, f"{url}:{line}" if url and line is not None else url)

    def _on_page_error(self, error: Any) -> None:
        text = getattr(error, "message", None) or str(error)
        self._add("pageerror", str(text), "")

    def describe(self, url: str, level: str | None = None) -> str:
        """The kept messages as the ``console`` action returns them.

        Errors first, then warnings, then the rest, each group in the order first logged, a repeat shown once with
        its count (``x178``). *level* (``error`` or ``warning``) keeps that kind and the more severe ones. The
        answer stops at :data:`MAX_CONSOLE_ANSWER_CHARS`: what is not shown, filtered out or dropped is counted.
        """
        if not self.entries:
            return f"No console message or page error since the last navigate (page at {url})."
        entries = list(self.entries.values())
        total = sum(e.count for e in entries)
        ceiling = {"error": 0, "warning": 1}.get(level or "", 2)
        shown = sorted((e for e in entries if e.severity <= ceiling), key=lambda e: e.severity)
        hidden = [e for e in entries if e.severity > ceiling]
        counts = (
            f"{total} message(s), {len(entries)} distinct"
            + (f"; {self.dropped} older dropped" if self.dropped else "")
            + (
                f"; {sum(e.count for e in hidden)} below level '{level}' not shown ({len(hidden)} distinct)"
                if hidden
                else ""
            )
        )
        header = (
            f"Console messages and page errors since the last navigate (page at {url}), errors first, then "
            f"warnings, then the rest, a repeat once with its count ({counts}):"
        )
        if not shown:
            return f"{header}\nNo message at level '{level}' or above."
        lines = [header]
        size = len(header)
        for index, entry in enumerate(shown):
            line = entry.line()
            if size + 1 + len(line) > MAX_CONSOLE_ANSWER_CHARS:
                rest = shown[index:]
                narrow = " (narrow with level='error' or level='warning')" if level != "error" else ""
                lines.append(
                    f"... {len(rest)} more distinct message(s), {sum(e.count for e in rest)} in all, not shown: "
                    f"the answer stops at {MAX_CONSOLE_ANSWER_CHARS:,} characters{narrow}."
                )
                break
            lines.append(line)
            size += 1 + len(line)
        return "\n".join(lines)


@dataclass(frozen=True)
class RedirectRefusal:
    """A redirect hop of the page's own navigation that the outbound URL policy refused."""

    url: str
    reason: str

    def describe(self) -> str:
        return f"Navigation blocked by URL policy: redirected to {self.url}: {self.reason}"


class RedirectGuard:
    """Holds every redirect hop of every request of one browser to the outbound URL policy.

    Playwright runs a route handler for the first URL of a request only: once
    the route filter continues it, Chromium follows the redirects itself. The
    guard sits in Chromium's own request interception (CDP ``Fetch`` on the
    browser target, so every page, frame and worker of the browser), where a
    request reached through a redirect is paused before it is sent. A first
    URL is continued at once and left to the route filter; a hop is sent only
    when the outbound URL policy an explicit navigate runs
    (``validate_outbound_url_async``) allows it, and otherwise fails without a
    request leaving the browser. Chromium keeps following the hops it allows
    and stops a chain at its own redirect limit, as without the guard.

    At most :data:`MAX_CONCURRENT_HOP_CHECKS` hops are checked at a time, and a
    check keeps its place until its resolver thread returns, even after the
    hop it served was refused for taking too long; a hop not checked within
    ``ACTION_TIMEOUT_MS`` of its pause, waiting included, is refused.

    A refused hop fails as ``Aborted``: Chromium then keeps the page on the
    document it was showing, as when the navigate check refuses a URL before
    the browser. ``BlockedByClient`` would commit an error page right after
    the navigation failed, interrupting the next one.
    """

    def __init__(self, validator: Callable[[str], Awaitable[object]] | None = None) -> None:
        #: The policy a hop is held to: the browser tool's public-only check when
        #: ``None`` (read at check time); the Video Studio passes its profile's.
        self._validator = validator
        self._cdp: Any = None
        self._main_frame_id: str | None = None
        #: Refused hops of the main frame's navigations, counted so an action can
        #: tell the ones it caused (``refusal_since``); only the latest are kept.
        self.main_frame_refusals = 0
        self._recent: deque[tuple[int, RedirectRefusal]] = deque(maxlen=8)
        self._decisions: set[asyncio.Task] = set()
        self._check_slots = asyncio.Semaphore(MAX_CONCURRENT_HOP_CHECKS)
        self._checks: set[asyncio.Task] = set()

    async def attach(self, browser: Any) -> None:
        """Pause every request of *browser* before it is sent, from now on."""
        self._cdp = cdp = await browser.new_browser_cdp_session()
        cdp.on("Fetch.requestPaused", self._on_request_paused)
        await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})

    async def watch_main_frame(self, context: Any, page: Any) -> None:
        """Remember *page*'s main frame: a Chromium page's main frame id is its target id."""
        cdp = await context.new_cdp_session(page)
        try:
            info = await cdp.send("Target.getTargetInfo")
        finally:
            await cdp.detach()
        self._main_frame_id = info["targetInfo"]["targetId"]

    def refusal_since(self, mark: int) -> RedirectRefusal | None:
        """The first main-frame hop refused after *mark* (a ``main_frame_refusals`` reading)."""
        return next((refusal for number, refusal in self._recent if number > mark), None)

    def close(self) -> None:
        for task in (*self._decisions, *self._checks):
            task.cancel()
        self._decisions.clear()

    def _on_request_paused(self, event: dict[str, Any]) -> None:
        decision = asyncio.get_running_loop().create_task(self._decide(event))
        self._decisions.add(decision)
        decision.add_done_callback(self._decisions.discard)

    async def _hop_refusal(self, url: str) -> str | None:
        """Why the policy refuses the hop *url*, or ``None`` when it allows it (fails closed)."""
        loop = asyncio.get_running_loop()
        bound = ACTION_TIMEOUT_MS / 1000
        deadline = loop.time() + bound
        try:
            await asyncio.wait_for(self._check_slots.acquire(), bound)
        except TimeoutError:
            return _not_checked_in_time()
        check = loop.create_task((self._validator or _outbound_or_preview)(url))
        self._checks.add(check)
        check.add_done_callback(self._check_finished)
        done, _pending = await asyncio.wait({check}, timeout=max(0.0, deadline - loop.time()))
        if not done:
            return _not_checked_in_time()
        if check.cancelled():
            return "its address could not be checked (the check was cancelled)"
        exc = check.exception()
        if exc is None:
            return None
        if isinstance(exc, ValueError):
            return format_exception_message(exc)
        return f"its address could not be checked ({format_exception_message(exc)})"

    def _check_finished(self, check: asyncio.Task) -> None:
        """Free the check's place once its resolver thread has returned."""
        self._checks.discard(check)
        self._check_slots.release()
        if not check.cancelled():
            check.exception()  # read here when the hop stopped waiting for it

    async def _decide(self, event: dict[str, Any]) -> None:
        request_id = event["requestId"]
        refusal: str | None = None
        recorded: tuple[int, RedirectRefusal] | None = None
        if event.get("redirectedRequestId"):
            url = str(event.get("request", {}).get("url") or "")
            try:
                refusal = await self._hop_refusal(url)
            except Exception as exc:
                refusal = f"its address could not be checked ({format_exception_message(exc)})"
            if refusal is not None:
                # Recorded before the failure is sent, so the action it fails can read it.
                recorded = self._record(event, RedirectRefusal(url, refusal))
        try:
            if refusal is None:
                await self._cdp.send("Fetch.continueRequest", {"requestId": request_id})
            else:
                await self._cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"})
        except Exception as exc:
            # The request is gone with its page or browser: the refusal did not stop anything.
            if recorded is not None and recorded in self._recent:
                self._recent.remove(recorded)
            logger.debug("Browser request %s could not be released: %s", request_id, format_exception_message(exc))

    def _record(self, event: dict[str, Any], refusal: RedirectRefusal) -> tuple[int, RedirectRefusal] | None:
        logger.debug("Browser redirect hop refused by the URL policy: %s", refusal.reason)
        if event.get("resourceType") != "Document" or event.get("frameId") != self._main_frame_id:
            return None
        self.main_frame_refusals += 1
        entry = (self.main_frame_refusals, refusal)
        self._recent.append(entry)
        return entry


@dataclass
class BrowserSession:
    """A managed headless browser session."""

    pw: Any  # playwright instance — must be stopped on close
    browser: Browser | Any  # playwright Browser (lazy-imported)
    context: BrowserContext | Any  # playwright BrowserContext (lazy-imported)
    page: Page | Any  # playwright Page (lazy-imported)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.time)
    closed: bool = False
    documents: DocumentTracker = field(default_factory=DocumentTracker)
    redirects: RedirectGuard = field(default_factory=RedirectGuard)
    console: ConsoleLog = field(default_factory=ConsoleLog)
    #: The URL check of every navigation and every page the session reads
    #: (:func:`_url_check`): a browser profile session holds the profile's
    #: policy (its declared private hosts), its redirect guard the same one;
    #: ``None`` = the outbound URL policy.
    check_url: Callable[[str], Awaitable[str]] | None = None
    #: The browser profile the session is signed in with
    #: (``browser_profiles.agent_sessions.ProfileSessionInfo``), or ``None``.
    profile: Any = None
    #: Closes a session launched elsewhere (:func:`install_session`) in place of
    #: the browser teardown; a profile session saves its refreshed session first.
    release: Callable[[], Awaitable[None]] | None = None
    #: The page's DevTools session, opened before any script runs: one opened while a script keeps the page
    #: busy never answers, so a script past its timeout is stopped through this one (:func:`_stop_script`).
    cdp: Any = None
    #: The page size the ``viewport`` action set, kept with the session's state for the one re-created after
    #: it; ``None`` = the default size.
    viewport: dict[str, int] | None = None
    #: The device pixel ratio the ``viewport`` action set (``scale``): a context option, so a change re-creates the
    #: session (:func:`browser_set_viewport`); kept with the session's state like the viewport. 1 = the default.
    scale: float = 1
    #: The media features the ``emulate_media`` action set (``reduced_motion``, ``color_scheme``), kept with the
    #: session's state like the viewport; ``None`` = none emulated.
    media: dict[str, str] | None = None
    #: The page cut from the network by the ``network`` action (``BrowserContext.set_offline``), kept with the
    #: session's state like the viewport; ``False`` = online.
    offline: bool = False
    #: The local storage of the kept state a re-created session started from, by origin, not yet checked on a page
    #: of that origin: the first navigate there writes back what did not land (:func:`_settle_resumed_page`).
    pending_storage: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    #: The session storage of the kept page a re-created session started from, by origin, not yet written to a page
    #: of that origin: Playwright's storage state carries none, so it travels with the kept page
    #: (:func:`_read_session_storage`) and the first page of that origin gets it back (:func:`_settle_resumed_page`).
    pending_session_storage: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    #: The page a session re-created from the state another process kept was on: the run was resumed there
    #: and believes it still is, so its first action of any kind opens it again (:func:`_reopen_resumed_page`)
    #: instead of running on about:blank; ``None`` once opened, after an explicit navigate, or with nothing to open.
    resume_url: str | None = None
    #: The page a navigate left and the one it answered, until the next action (:func:`_check_navigated_document`):
    #: the first ``evaluate`` after it runs on the answered document or refuses (#10013). ``None`` otherwise.
    navigated: tuple[str, str] | None = None
    #: When that navigate started, in epoch milliseconds: a document whose ``performance.timeOrigin`` is later is the
    #: navigate's own, even at the address of the page it left (an application that routed back there, #10149).
    navigated_at: float | None = None
    #: The document count when a navigate answered "Still loading" (:func:`_still_loading`): that document has not
    #: reached DOMContentLoaded yet, and the next navigate waits for it before leaving it (:func:`_finish_loading`).
    #: ``None`` when no navigate left a document loading.
    loading_mark: int | None = None


# Global session registry: key = "{agent_id}:{conversation_id}"
_sessions: dict[str, BrowserSession] = {}
# Per-key creation locks serialize the check-then-launch in
# ``_get_or_create_session``: the agent loop runs a turn's tool calls
# concurrently (asyncio.gather), so two ``browser`` calls for the same key could
# both pass the existence check and both launch Chromium — the second overwrote
# the first in ``_sessions``, orphaning the first's ~100 MB browser process.
_session_locks: dict[str, asyncio.Lock] = {}
_cleanup_task: asyncio.Task | None = None
#: The cookies and local storage of each session browsing without a profile, as its last navigation left them:
#: ``key -> (Playwright storage state, when kept)``. A session re-created for the key - its browser died, or
#: it was closed idle - starts from them, still signed in. They are credentials: besides this process's copy they are
#: shared with the other backend processes only Fernet-encrypted in Redis, for :data:`KEPT_STATE_SECONDS`
#: (:func:`_share_state`), so a run resumed in another process after a rollout keeps its session. A profile's session
#: is never kept here: its cookies stay with its policy.
_kept_states: dict[str, tuple[dict, float]] = {}
#: The page size and the last URL kept with :data:`_kept_states`, by key.
_kept_pages: dict[str, dict[str, Any]] = {}
#: The page size and pixel ratio the next launch of a key opens with, whatever was kept: set by a ``viewport`` action
#: that changes the pixel ratio, which re-creates the session (:func:`browser_set_viewport`), even with nothing kept.
_next_launch: dict[str, dict[str, Any]] = {}
#: The stamp (writer, kept_at) of the shared copy this process last wrote or started from, by this process's id and
#: key: a shared copy with another stamp was written since by another process (:func:`_newer_elsewhere`), no clock
#: compared.
_known_stamps: dict[tuple[str, str], tuple[str, float]] = {}
#: What the next action of a session says first: it was re-created, and what it was given back.
_notices: dict[str, str] = {}


#: One lock per session key taken when a browser action is called, before its first ``await`` (:func:`action_order`),
#: with the event loop it serves: a lock is bound to the loop it first waited on, so another loop gets its own.
_action_orders: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


@contextlib.asynccontextmanager
async def action_order(session_key: str):
    """Run the browser actions of *session_key* one after another, in the order the agent called them.

    The agent loop runs a turn's tool calls concurrently: a navigate and an evaluate called in the same turn both
    reached the page, and the navigate's URL check (a DNS read) let the evaluate take :attr:`BrowserSession.lock`
    first, so the script read the page the navigate was leaving while the navigate answered its URL (UX run
    cdecbc8a: ``/api/v1/health`` then ``/flows/fleets``). The lock is taken before anything awaits, and an
    ``asyncio.Lock`` serves its waiters first in, first out: each action starts once the one called before it ended.
    """
    loop = asyncio.get_running_loop()
    held = _action_orders.get(session_key)
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Lock())
        _action_orders[session_key] = held
    async with held[1]:
        yield


def _session_lock(key: str) -> asyncio.Lock:
    """Return the per-key creation lock, creating it on first use.

    Safe without its own guard: dict get/set here span no ``await``, so the
    single-threaded event loop runs this atomically.
    """
    lock = _session_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[key] = lock
    return lock

__all__ = ["_cleanup_task"]


async def _ensure_cleanup_running() -> None:
    """Start the background cleanup task if not already running."""
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        # A context of its own, not the one of the run that first browsed.
        _cleanup_task = asyncio.create_task(_cleanup_loop(), context=contextvars.Context())


async def _cleanup_loop() -> None:
    """Periodically close idle sessions."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        # A session an action holds is in use, however long that action takes.
        to_close = [
            k
            for k, s in _sessions.items()
            if not s.closed and not s.lock.locked() and (now - s.last_used) > SESSION_TIMEOUT_SECONDS
        ]
        for key in to_close:
            await _close_session(key)
        for key in [k for k, (_state, kept) in _kept_states.items() if now - kept > KEPT_STATE_SECONDS]:
            _kept_states.pop(key, None)
            _kept_pages.pop(key, None)
        # Drop creation locks that no longer guard anything (no live session
        # and no creation in flight). Without this sweep the map would retain
        # one Lock per {agent_id}:{conversation_id} key forever — including
        # keys whose launch failed and never produced a session to clean up.
        stale_locks = [
            k
            for k, lk in _session_locks.items()
            if k not in _sessions and not lk.locked()
        ]
        for key in stale_locks:
            _session_locks.pop(key, None)
        for key in [k for k, (_loop, lk) in _action_orders.items() if k not in _sessions and not lk.locked()]:
            _action_orders.pop(key, None)


def _keeps_state(session: BrowserSession) -> bool:
    """Whether *session*'s cookies and local storage are kept for the one re-created after it: one without a profile."""
    return session.profile is None and session.release is None


async def _remember_state(key: str, session: BrowserSession, *, over_newer: bool = True) -> dict | None:
    """Keep *session*'s cookies and local storage for key *key* (:data:`_kept_states`); best effort, bounded. Returns
    the state read and shared, or ``None`` when none was.

    *over_newer* ``False``: a state another process shared later is left as it is (:func:`_share_state`)."""
    if not _keeps_state(session):
        return None
    url = str(getattr(session.page, "url", "") or "")
    if not url.startswith(("http://", "https://")):
        # about:blank or an error page (a session just re-created): the state kept before names the page the
        # run was on and its sign-in; this one would replace it with neither.
        return None
    origin = _origin_of(url)
    if (
        origin in session.pending_storage or origin in _pending_session_storage(session)
    ) and not await _write_back_pending(key, session, origin):
        # A re-created session on a page whose kept storage has not landed yet: read now, the page's
        # origin would be kept signed out, and the next resume would start from it.
        return None
    try:
        state = await asyncio.wait_for(session.context.storage_state(), _STATE_READ_SECONDS)
    except Exception as exc:  # a page busy or gone: the state its last navigation left stays kept
        logger.debug("Browser session state not read: %s", format_exception_message(exc))
        return None
    if not isinstance(state, dict):
        return None
    state = await _with_page_origin(key, session, state, origin)
    removed = _storage_removed(key, state, origin)
    state = _with_kept_origins(key, state, origin)
    page: dict[str, Any] = {}
    if session.viewport:
        page["viewport"] = dict(session.viewport)
    if _scale_of(session) != 1:
        page["scale"] = _scale_of(session)
    if session.media:
        page["media"] = dict(session.media)
    if session.offline:
        page["offline"] = True
    page["url"] = url
    session_storage = await _read_session_storage(key, session, origin)
    if session_storage:
        page["session_storage"] = session_storage
    if removed:
        page["storage_removed"] = removed
    if not over_newer:
        # A session closed idle: the run may have gone on in another process since its last action.
        kept_at = session.last_used
        await _share_state(key, state, page, kept_at=kept_at, over_newer=False)
        previous = _kept_states.get(key)
        if previous is None or previous[1] <= kept_at:
            _kept_states[key] = (state, kept_at)
            _kept_pages[key] = page
        return state
    _kept_states[key] = (state, time.time())
    _kept_pages[key] = page
    await _share_state(key, state, page)
    return state


async def _write_back_pending(key: str, session: BrowserSession, origin: str) -> bool:
    """Write the kept local storage of *origin* still pending on *session* back to its page, never overwriting what
    the page holds; whether it landed. Logged with the session key when it did not."""
    try:
        if origin in session.pending_storage:
            items = session.pending_storage.get(origin) or []
            await asyncio.wait_for(session.page.evaluate(_RESTORE_LOCAL_STORAGE_JS, items), _STATE_READ_SECONDS)
            session.pending_storage.pop(origin, None)
        if origin in _pending_session_storage(session):
            items = session.pending_session_storage.get(origin) or []
            await asyncio.wait_for(session.page.evaluate(_RESTORE_SESSION_STORAGE_JS, items), _STATE_READ_SECONDS)
            session.pending_session_storage.pop(origin, None)
    except Exception as exc:
        logger.warning("Browser session %s: the kept storage of %s not written back (%s), state kept as it was",
                       key, origin, format_exception_message(exc))
        return False
    return True


_READ_SESSION_STORAGE_JS = """
() => {
  const items = [];
  for (let i = 0; i < window.sessionStorage.length; i++) {
    const name = window.sessionStorage.key(i);
    if (name !== null) items.push([name, window.sessionStorage.getItem(name) ?? ""]);
  }
  return items;
}
"""


def _pending_session_storage(session: Any) -> dict[str, list[tuple[str, str]]]:
    """The session storage still pending on *session* (none on a session installed or stubbed without it)."""
    pending = getattr(session, "pending_session_storage", None)
    return pending if isinstance(pending, dict) else {}


def _session_storage_of(page: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """The session storage items a kept page carries (:func:`_read_session_storage`), by lower-cased origin."""
    kept = page.get("session_storage")
    if not isinstance(kept, dict):
        return {}
    by_origin: dict[str, list[tuple[str, str]]] = {}
    for origin, pairs in kept.items():
        items = [
            (str(pair[0]), str(pair[1]))
            for pair in pairs or []
            if isinstance(pair, list | tuple) and len(pair) == 2
        ] if isinstance(pairs, list) else []
        if isinstance(origin, str) and _origin_of(origin) and items:
            by_origin[origin.lower()] = items
    return by_origin


async def _read_session_storage(key: str, session: BrowserSession, origin: str) -> dict[str, list[list[str]]]:
    """The session storage to keep with the page of *session*: the page's *origin* as read there, and every other
    origin the page kept before held (session storage is per tab and per origin: only the page's own can be read).

    Playwright's storage state carries cookies and local storage only, so a page state held in session storage (a
    composer draft, a selected agent) was lost at every resume in another process. A page that cannot be read keeps
    the items kept before for its origin; an empty storage is kept as empty. Names and values are never logged.
    """
    kept = _session_storage_of(_kept_pages.get(key) or {})
    try:
        read = await asyncio.wait_for(session.page.evaluate(_READ_SESSION_STORAGE_JS), _STATE_READ_SECONDS)
    except Exception as exc:
        logger.debug("Browser session %s: session storage of %s not read (%s), the kept one carried over", key,
                     origin, format_exception_message(exc))
    else:
        items = [
            (str(pair[0]), str(pair[1]))
            for pair in read or []
            if isinstance(pair, list | tuple) and len(pair) == 2
        ] if isinstance(read, list) else []
        kept.pop(origin, None)
        if items:
            kept[origin] = items
    return {o: [[name, value] for name, value in items] for o, items in kept.items()}


_READ_LOCAL_STORAGE_JS = """
() => {
  const items = [];
  for (let i = 0; i < window.localStorage.length; i++) {
    const name = window.localStorage.key(i);
    if (name !== null) items.push([name, window.localStorage.getItem(name) ?? ""]);
  }
  return items;
}
"""


async def _with_page_origin(key: str, session: BrowserSession, state: dict, origin: str) -> dict:
    """*state* with the local storage of the page's own origin when the read left it out.

    Playwright leaves out of ``storage_state()`` an origin it read empty or could not read (a frame mid-navigation):
    :func:`_with_kept_origins` takes the page's origin as read, so one such read replaced the kept sign-in token with
    nothing and the share handed the resumed run a state with 0 sites. The page itself says which it was: items read
    there are kept, an empty storage is a sign-out and stays one, a page that cannot be read keeps the items kept
    before (logged with the session key).
    """
    if any(isinstance(o, dict) and str(o.get("origin", "")).lower() == origin for o in state.get("origins") or []):
        return state
    try:
        read = await asyncio.wait_for(session.page.evaluate(_READ_LOCAL_STORAGE_JS), _STATE_READ_SECONDS)
        items = [
            {"name": str(pair[0]), "value": str(pair[1])}
            for pair in read or []
            if isinstance(pair, list | tuple) and len(pair) == 2
        ]
    except Exception as exc:
        previous = _kept_states.get(key)
        kept = next(
            (
                o
                for o in (previous[0].get("origins") or [] if previous else [])
                if isinstance(o, dict) and str(o.get("origin", "")).lower() == origin and o.get("localStorage")
            ),
            None,
        )
        if kept is None:
            return state
        logger.warning("Browser session state of %s: local storage of %s not read (%s), the kept one carried over",
                       key, origin, format_exception_message(exc))
        return {**state, "origins": [*(state.get("origins") or []), kept]}
    if not items:
        return state
    logger.warning("Browser session state of %s: local storage of %s left out of the read, %d item(s) read on the page",
                   key, origin, len(items))
    return {**state, "origins": [*(state.get("origins") or []), {"origin": origin, "localStorage": items}]}


def _storage_removed(key: str, state: dict, origin: str) -> list[str]:
    """The local storage keys of the page's *origin* the state kept before held and *state* (just read) no longer
    does, with those named before and still absent: names only, never values.

    A page that removes its own keys (a sign-out, a token cleared on a 401) is kept as read, so a resume lands where
    the page was, signed out; :func:`_restored_notice` names these keys so the run knows the restore did not lose
    them.
    """
    read = _storage_by_origin(state)
    if origin not in read and not any(
        isinstance(o, dict) and str(o.get("origin", "")).lower() == origin for o in state.get("origins") or []
    ):
        # The read left the origin out: an empty storage (all removed) or one not read; the kept page says.
        held: set[str] = set()
    else:
        held = {name for name, _value in read.get(origin, [])}
    previous = _kept_states.get(key)
    before = {name for name, _value in _storage_by_origin(previous[0]).get(origin, [])} if previous else set()
    kept_page = _kept_pages.get(key) or {}
    named = kept_page.get("storage_removed") if _origin_of(str(kept_page.get("url") or "")) == origin else None
    still = {str(n) for n in named or [] if isinstance(n, str)}
    return sorted((before | still) - held)[:_STORAGE_REMOVED_NAMES]


#: The most removed local storage key names a kept page names (:func:`_storage_removed`).
_STORAGE_REMOVED_NAMES = 10


def _with_kept_origins(key: str, state: dict, origin: str) -> dict:
    """*state* with the local storage of every origin the state kept before held and this read missed.

    Playwright reads each origin's local storage on a page of its own and leaves out an origin it read empty or could
    not read: one read missing the origin of a sign-in replaced the kept token, and the share handed the other
    processes a state with 0 sites. The page's own origin is taken as read, so a sign-out there is kept.
    """
    previous = _kept_states.get(key)
    if previous is None:
        return state
    read = {str(o.get("origin", "")).lower() for o in state.get("origins") or [] if isinstance(o, dict)}
    carried = [
        o
        for o in previous[0].get("origins") or []
        if isinstance(o, dict)
        and str(o.get("origin", "")).lower() not in read
        and str(o.get("origin", "")).lower() != origin
        and o.get("localStorage")
    ]
    if not carried:
        return state
    logger.warning("Browser session state of %s: local storage of %s not read, the kept one carried over", key,
                   ", ".join(str(o.get("origin")) for o in carried))
    return {**state, "origins": [*(state.get("origins") or []), *carried]}


def _shared_key(key: str) -> str:
    return f"{_SHARED_STATE_PREFIX}{key}"


def _origin_of(url: str) -> str:
    """``scheme://host[:port]`` of *url*, as Playwright names an origin of the storage state; ``""`` without one."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def _trimmed_state(state: dict, url: str | None) -> dict:
    """*state* with the local storage of the page's origin only: the cookies and the storage that keep the
    session signed in where it is, for a state too large to share whole."""
    origin = _origin_of(url or "")
    origins = [
        o for o in state.get("origins") or [] if isinstance(o, dict) and str(o.get("origin", "")).lower() == origin
    ]
    return {**state, "origins": origins}


def _trimmed_page(page: dict[str, Any]) -> dict[str, Any]:
    """*page* with the session storage of its own origin only, for a state too large to share whole."""
    if not isinstance(page.get("session_storage"), dict):
        return page
    origin = _origin_of(str(page.get("url") or ""))
    kept = {o: items for o, items in page["session_storage"].items() if o == origin}
    trimmed = {k: v for k, v in page.items() if k != "session_storage"}
    if kept:
        trimmed["session_storage"] = kept
    return trimmed


async def _share_state(key: str, state: dict, page: dict[str, Any], *, kept_at: float | None = None,
                       over_newer: bool = True) -> None:
    """Share the kept state of key *key* with the other backend processes: Fernet-encrypted in Redis
    (invariant 2), for :data:`KEPT_STATE_SECONDS`; best effort and bounded, this process keeps its own copy.

    A state over :data:`_MAX_SHARED_STATE_BYTES` (every site visited adds its local storage) is shared trimmed to its
    cookies and the local storage of the page's origin, never dropped whole: a dropped write left the other
    process an older copy or none, and the resumed run signed out.

    The shared copy names this process and when the state was kept. *over_newer* ``False`` (a session closed
    idle, the run maybe gone on elsewhere) leaves a copy another process kept later untouched.
    """
    try:
        from app.core.redis import redis_client
        from app.core.security import encrypt_value

        kept = time.time() if kept_at is None else kept_at
        if not over_newer:
            current = await _shared_entry(key)
            if current is not None and _newer_elsewhere(current[2], kept, key):
                logger.warning("Browser session state of %s not shared: another process kept a newer one", key)
                return
        stamp = {"writer": _PROCESS_ID, "kept_at": kept}
        token = encrypt_value(json.dumps({"state": state, **page, **stamp}))
        if len(token) > _MAX_SHARED_STATE_BYTES:
            token = encrypt_value(
                json.dumps({"state": _trimmed_state(state, page.get("url")), **_trimmed_page(page), **stamp})
            )
        if len(token) > _MAX_SHARED_STATE_BYTES:
            logger.warning("Browser session state of %s not shared: %d bytes once trimmed", key, len(token))
            return
        client = await redis_client()
        await asyncio.wait_for(client.set(_shared_key(key), token, ex=int(KEPT_STATE_SECONDS)), _STATE_READ_SECONDS)
        _known_stamps[(_PROCESS_ID, key)] = (_PROCESS_ID, float(kept))
    except Exception as exc:
        logger.warning("Browser session state of %s not shared: %s", key, format_exception_message(exc))


async def _shared_state(key: str) -> tuple[dict, dict[str, Any]] | None:
    """The state another process kept for key *key* (:func:`_share_state`), or ``None``; never raises."""
    entry = await _shared_entry(key)
    return None if entry is None else (entry[0], entry[1])


async def _shared_entry(key: str) -> tuple[dict, dict[str, Any], dict[str, Any]] | None:
    """The shared state of key *key*, its page (viewport, URL) and who kept it when (``writer``, ``kept_at``;
    empty for an older shared state), or ``None``; never raises."""
    try:
        from app.core.redis import redis_client
        from app.core.security import decrypt_json

        client = await redis_client()
        token = await asyncio.wait_for(client.get(_shared_key(key)), _STATE_READ_SECONDS)
        data = decrypt_json(token) if token else None
    except Exception as exc:
        logger.warning("Browser session state of %s not read back: %s", key, format_exception_message(exc))
        return None
    if not isinstance(data, dict) or not isinstance(data.get("state"), dict):
        return None
    page: dict[str, Any] = {}
    viewport = data.get("viewport")
    if isinstance(viewport, dict):
        with contextlib.suppress(ValueError):  # a size out of bounds: the default one
            page["viewport"] = viewport_size(viewport.get("width"), viewport.get("height"))
    scale = _kept_scale(data.get("scale"))
    if scale != 1:
        page["scale"] = scale
    media = _kept_media(data.get("media"))
    if media:
        page["media"] = media
    if data.get("offline") is True:
        page["offline"] = True
    if isinstance(data.get("url"), str):
        page["url"] = data["url"]
    removed = data.get("storage_removed")
    if isinstance(removed, list):
        names = [str(n) for n in removed if isinstance(n, str)][:_STORAGE_REMOVED_NAMES]
        if names:
            page["storage_removed"] = names
    session_storage = _session_storage_of(data)
    if session_storage:
        page["session_storage"] = {o: [[n, v] for n, v in items] for o, items in session_storage.items()}
    stamp: dict[str, Any] = {}
    if isinstance(data.get("writer"), str):
        stamp["writer"] = data["writer"]
    kept_at = data.get("kept_at")
    if isinstance(kept_at, int | float) and not isinstance(kept_at, bool):
        stamp["kept_at"] = float(kept_at)
    return data["state"], page, stamp


def _newer_elsewhere(stamp: dict[str, Any], kept_at: float, key: str | None = None) -> bool:
    """Whether a shared state, by its stamp (:func:`_shared_entry`), was kept by another process after this process
    kept its own copy (at *kept_at*): the run went on in that process since.

    With the stamp this process last wrote or started from for *key* (:data:`_known_stamps`), any other copy another
    process wrote is newer: the shared key holds one copy, so it was written after that one. The ``kept_at`` stamps
    of two processes come from two clocks: compared, a host ahead of the other took the other's newer copy for an
    older one, kept its session on the previous page and shared that page over the newer copy (#10499). The clock
    comparison stays only for a process that saw no shared copy of *key*.
    """
    writer = stamp.get("writer")
    if not isinstance(writer, str) or writer == _PROCESS_ID:
        return False
    known = _known_stamps.get((_PROCESS_ID, key)) if key is not None else None
    if known is not None:
        return (writer, float(stamp.get("kept_at") or 0.0)) != known
    return float(stamp.get("kept_at") or 0.0) > kept_at


async def _forget_shared_state(key: str) -> None:
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        await asyncio.wait_for(client.delete(_shared_key(key)), _STATE_READ_SECONDS)
    except Exception as exc:
        logger.debug("Browser session state not forgotten: %s", format_exception_message(exc))


def _alive(session: BrowserSession) -> bool:
    """Whether *session*'s browser still answers: not disconnected, its page not closed (a browser that cannot say
    is taken as answering)."""
    try:
        return session.browser.is_connected() is not False and session.page.is_closed() is not True
    except Exception:
        return True


def take_notice(session_key: str) -> str | None:
    """What the session of *session_key* says first since it was re-created, once; ``None`` otherwise."""
    return _notices.pop(session_key, None)


async def _close_session(key: str, *, forget: bool = False) -> None:
    """Close and remove a browser session; *forget*: its kept cookies and local storage go with it."""
    session = _sessions.pop(key, None)
    if forget:
        _kept_states.pop(key, None)
        _kept_pages.pop(key, None)
        _next_launch.pop(key, None)
        _known_stamps.pop((_PROCESS_ID, key), None)
        await _forget_shared_state(key)
    if session and not session.closed:
        session.closed = True
        if not forget and _alive(session):
            await _remember_state(key, session, over_newer=False)
        try:
            if session.release is not None:
                await session.release()
            else:
                session.redirects.close()
                await session.context.close()
                await session.browser.close()
                await session.pw.stop()
        except Exception:
            message = "Best-effort browser cleanup failed for session %s"
            logger.debug(message, key, exc_info=True)
        logger.debug("Browser session closed: %s", key)


#: The longest the process stop waits for the open sessions' states to be kept (:func:`keep_open_sessions`).
_KEEP_ON_STOP_SECONDS = 10.0


async def keep_open_sessions() -> int:
    """Keep and share the state of every open session without a profile, once, as the process stops.

    A rollout stops this process in the middle of a run: the run resumes in another process from the state shared at
    its last kept action. A sign-in token an application wrote after that action (a login request answered after the
    click returned) was never kept, and the resumed session came back signed out with 0 sites. Each state is read and
    shared now, stamped with the session's last action so that a copy the resumed run shared since from another
    process is never overwritten, within :data:`_KEEP_ON_STOP_SECONDS` in all; each one is logged at WARNING
    with its session key. Returns how many sessions were kept.
    """
    open_sessions = [(k, s) for k, s in list(_sessions.items()) if not s.closed and _keeps_state(s)]
    if not open_sessions:
        return 0

    async def keep(key: str, session: BrowserSession) -> bool:
        if not _alive(session):
            logger.warning("Browser session %s not kept at the process stop: its browser stopped first", key)
            return False
        state = await _remember_state(key, session, over_newer=False)
        if state is None:
            logger.warning("Browser session %s not kept at the process stop: no state read on %s", key,
                           getattr(session.page, "url", "") or "no page")
            return False
        cookies = len(state.get("cookies") or [])
        origins = len(state.get("origins") or [])
        logger.warning("Browser session %s kept at the process stop: %d cookie(s), local storage of %d site(s)", key,
                       cookies, origins)
        return True

    try:
        done = await asyncio.wait_for(
            asyncio.gather(*(keep(k, s) for k, s in open_sessions), return_exceptions=True), _KEEP_ON_STOP_SECONDS
        )
    except Exception as exc:
        logger.warning("Browser sessions not all kept at the process stop: %s", format_exception_message(exc))
        return 0
    return sum(1 for d in done if d is True)


async def install_session(session_key: str, session: BrowserSession) -> None:
    """Make *session* (launched elsewhere) the session of *session_key*, closing the one it replaces."""
    await _ensure_cleanup_running()
    await _open_cdp(session)
    async with _session_lock(session_key):
        await _close_session(session_key)
        session.documents.watch(session.page)
        session.console.watch(session.page)
        session.last_used = time.time()
        _sessions[session_key] = session
    logger.debug("Browser session installed: %s", session_key)


def session_profile(session_key: str) -> Any:
    """The browser profile the open session of *session_key* is signed in with, or ``None``."""
    session = _sessions.get(session_key)
    return session.profile if session is not None and not session.closed else None


_READ_SESSION_STORAGE_JS = """
() => {
  const items = {};
  for (let i = 0; i < sessionStorage.length; i++) {
    const key = sessionStorage.key(i);
    items[key] = sessionStorage.getItem(key);
  }
  return items;
}
"""


@dataclass(frozen=True)
class CapturedSession:
    """What an open session holds now (:func:`capture_session`): what a browser profile saves of it."""

    #: Playwright's storage state of the context (cookies and local storage by origin).
    state: dict
    #: The URL the page is on.
    url: str
    #: The page origin's ``sessionStorage`` (empty when it could not be read).
    session_storage: dict[str, str]
    #: The browser profile the session is signed in with, or ``None``.
    profile: Any = None


async def capture_session(session_key: str) -> CapturedSession | None:
    """The cookies and storage of the open session of *session_key*, read between two actions; ``None`` with none.

    Never opens a session: a session that is not open (or whose browser
    stopped) has nothing signed in to keep.
    """
    session = _sessions.get(session_key)
    if session is None or session.closed or not _alive(session):
        return None
    async with session.lock:
        session.last_used = time.time()
        try:  # IndexedDB too, as a profile sign-in saves it: an application may keep its session there
            read = session.context.storage_state(indexed_db=True)
        except TypeError:  # an older Playwright
            read = session.context.storage_state()
        state = await asyncio.wait_for(read, _STATE_READ_SECONDS)
        url = str(session.page.url or "")
        items: Any = None
        try:
            items = await asyncio.wait_for(session.page.evaluate(_READ_SESSION_STORAGE_JS), _STATE_READ_SECONDS)
        except Exception as exc:  # a page navigating or busy: its cookies and local storage still say it
            logger.debug("Session storage not read: %s", format_exception_message(exc))
    session_storage = {str(k): str(v) for k, v in items.items()} if isinstance(items, dict) else {}
    return CapturedSession(
        state=state if isinstance(state, dict) else {}, url=url, session_storage=session_storage, profile=session.profile
    )


async def _went_on_elsewhere(session_key: str, session: BrowserSession) -> bool:
    """Whether the run of *session_key* went on in another process since this one last kept *session*'s state.

    A run handed to another worker and back finds its session of this process still open, on the page it left:
    a navigate the other process made in between answered its URL, and the next action here ran on the previous
    page (#9935). The shared copy names its writer and when it was kept (:func:`_share_state`); one kept by
    another process after this process's copy means this page is stale. A session that keeps no state, or kept
    none yet here, has nothing to compare; a shared copy that cannot be read keeps the session.
    """
    if not _keeps_state(session):
        return False
    local = _kept_states.get(session_key)
    if local is None:
        return False
    shared = await _shared_entry(session_key)
    return shared is not None and _newer_elsewhere(shared[2], local[1], session_key)


async def _get_or_create_session(session_key: str) -> BrowserSession:
    """Get an existing session or create a new one."""
    await _ensure_cleanup_running()

    existing = _sessions.get(session_key)
    if (
        existing is not None
        and not existing.closed
        and _alive(existing)
        and not await _went_on_elsewhere(session_key, existing)
    ):
        existing.last_used = time.time()
        return existing

    async with _session_lock(session_key):
        # Re-check under the lock: a concurrent tool call in the same turn may
        # have created the session while we awaited the lock. Without this the
        # loser's freshly launched Chromium would be orphaned by the overwrite.
        existing = _sessions.get(session_key)
        if existing is not None and not existing.closed:
            if _alive(existing):
                if not await _went_on_elsewhere(session_key, existing):
                    existing.last_used = time.time()
                    return existing
                # The run went on in another process since this one last kept the session: this page is where
                # the run WAS (#9935). Closed without overwriting the newer copy, re-created from it.
                logger.warning("Browser session %s: another process kept a newer state since this one's, the "
                               "session is re-created from it", session_key)
                await _close_session(session_key)
                return await _launch_session(session_key)
            # Its browser died: re-created below from what its last navigation left, and said so.
            if existing.profile is not None:
                _notices[session_key] = (
                    f"The browser signed in with the browser profile '{existing.profile.name}' stopped: this is a new "
                    "session without it - navigate with the profile argument to sign in with it again."
                )
            await _close_session(session_key)
            return await _launch_session(session_key)
        # No session open for the key: closed idle (or never opened here). The run still believes it is on its page,
        # which stopped no browser: opened again before the next action, as after a resume elsewhere.
        return await _launch_session(session_key, reopen_kept_page=True)


async def _launch_session(session_key: str, *, reopen_kept_page: bool = False) -> BrowserSession:
    """Launch a new Playwright browser session (callers hold the key lock).

    The redirect guard is attached before the first page exists; a browser it
    cannot be attached to is closed rather than handed to an agent.

    A session re-created from a state another process kept opens its page again before its first action;
    *reopen_kept_page* does the same from this process's copy: a session closed idle, whose page stopped no
    browser. A browser that died in this process is not sent back to its page, which may be what stopped it.
    """
    pw: Any = None
    browser: Any = None
    redirects = RedirectGuard()
    try:
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
            # Playwright adds --hide-scrollbars to every headless launch: a screenshot then paints no
            # scrollbar, and a review cannot see one. The agent's browser keeps them classic.
            ignore_default_args=list(BROWSER_SHOWN_DEFAULT_ARGS),
        )
        await redirects.attach(browser)
        kept_state: dict | None = None
        kept_page: dict[str, Any] = {}
        local = _kept_states.get(session_key)
        # Kept by another process: the run was resumed elsewhere. Read even with a copy of this process:
        # a run that left this process and came back to it went on elsewhere in between, and this copy is
        # older than the one that process shared.
        shared = await _shared_entry(session_key)
        if local is not None and shared is not None and _newer_elsewhere(shared[2], local[1], session_key):
            logger.warning("Browser session %s re-created from the state another process kept after this one's",
                           session_key)
            local = None
        from_process = local is not None
        if local is not None:
            kept_state, kept_page = local[0], dict(_kept_pages.get(session_key) or {})
        elif shared is not None:
            kept_state, kept_page = shared[0], shared[1]
        if kept_state is not None:
            # What the resume starts from, said where the logs can be read: a state shared with no site,
            # shared then lost, or a stale copy of this process are told apart by these figures and the stamp.
            stamp = shared[2] if shared is not None and not from_process else {}
            logger.warning(
                "Browser session %s re-created from the state %s: %d cookie(s), local storage of %d site(s), "
                "kept by %s at %s, page %s",
                session_key,
                ("of this process" + (" (closed idle)" if reopen_kept_page else " (its browser stopped)"))
                if from_process
                else "shared by another process",
                len(kept_state.get("cookies") or []),
                len(kept_state.get("origins") or []),
                stamp.get("writer", _PROCESS_ID if from_process else "an unstamped writer"),
                stamp.get("kept_at", local[1] if local is not None else "an unknown time"),
                kept_page.get("url") or "unknown",
            )
        # A pixel ratio changed by the viewport action re-created the session: what it set wins over the kept page.
        opened = {**kept_page, **_next_launch.pop(session_key, {})}
        viewport = opened.get("viewport")
        scale = _kept_scale(opened.get("scale"))
        media = _kept_media(opened.get("media"))
        offline = opened.get("offline") is True
        context = await browser.new_context(
            viewport=dict(viewport or DEFAULT_VIEWPORT),
            device_scale_factor=scale,
            user_agent="FiligranCopilot/1.0 (Browser Tool)",
            storage_state=kept_state,
            offline=offline,
            **media,
        )
        await context.route("**/*", _route_filter)
        if offline:
            await context.route("**/*", _fail_offline)
        page = await context.new_page()
        await redirects.watch_main_frame(context, page)
        page.set_default_timeout(ACTION_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

        session = BrowserSession(
            pw=pw,
            browser=browser,
            context=context,
            page=page,
            redirects=redirects,
            viewport=dict(viewport) if viewport else None,
            scale=scale,
            media=media or None,
            offline=offline,
        )
        session.documents.watch(page)
        session.console.watch(page)
        await _open_cdp(session)
        _sessions[session_key] = session
        if kept_state is not None:
            session.pending_storage = _storage_by_origin(kept_state)
            session.pending_session_storage = _session_storage_of(kept_page)
            if not from_process:
                _kept_states[session_key] = (kept_state, time.time())
                _kept_pages[session_key] = dict(kept_page)
                writer, kept_at = stamp.get("writer"), stamp.get("kept_at")
                if isinstance(writer, str) and isinstance(kept_at, float):
                    # Started from this copy: only a copy written after it means the run went on elsewhere.
                    _known_stamps[(_PROCESS_ID, session_key)] = (writer, kept_at)
            # Resumed elsewhere or closed idle here: the page did not stop a browser, so it is opened
            # again; the first action would otherwise run on about:blank (SecurityError on local storage, blank
            # screenshot) while the run believes it is still on its page.
            if (not from_process or reopen_kept_page) and _origin_of(str(kept_page.get("url") or "")):
                session.resume_url = str(kept_page["url"])
            _notices[session_key] = _restored_notice(
                session_key, kept_state, kept_page, from_process, reopens=session.resume_url is not None
            )
        logger.debug("Browser session created: %s", session_key)
        return session

    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Install with: pip install playwright && playwright install chromium"
        ) from exc
    except Exception as exc:
        redirects.close()
        await _discard_launch(browser, pw)
        raise RuntimeError(f"Failed to launch browser: {exc}") from exc


#: What the notice of a re-created session says of its page (:func:`_restored_notice`), and what it says instead when
#: its first action is a navigate, which goes where it is told (:func:`browser_navigate`).
_REOPEN_NOTE = "The next action opens it again first"
_NAVIGATED_NOTE = "This navigate went to the page it was given instead"
_NOT_REOPENED_NOTE = "It could not be opened again before this action"


def _restored_notice(
    session_key: str, state: dict, page: dict[str, Any], from_process: bool, *, reopens: bool = False
) -> str:
    """What a session re-created from a kept state says first: what it was given back, and the page it was on.

    *reopens*: the page is opened again by the next action (:attr:`BrowserSession.resume_url`)."""
    cookies = len(state.get("cookies") or [])
    origins = len(state.get("origins") or [])
    restored = f"Session re-created, cookies restored ({cookies}), local storage of {origins} site(s) restored"
    session_sites = len(_session_storage_of(page))
    if session_sites:
        restored += f", session storage of {session_sites} site(s) restored"
    if not from_process:
        restored += " (the run went on in another server process)"
    viewport = page.get("viewport")
    if viewport:
        restored += f", viewport {viewport['width']} x {viewport['height']} px restored"
    scale = _kept_scale(page.get("scale"))
    if scale != 1:
        restored += f", pixel ratio {scale:g} restored"
    media = _kept_media(page.get("media"))
    if media:
        restored += f", emulated media {_media_text(media)} restored"
    if page.get("offline") is True:
        restored += ", the page still offline (network action)"
    removed = [str(n) for n in page.get("storage_removed") or [] if isinstance(n, str)]
    if removed:
        # Kept as the page left them: a sign-in key the page removed is not given back by the restore.
        restored += (
            f". The page itself removed {', '.join(removed)} from its local storage before the resume: the session "
            "resumes without them (signed out if they held the sign-in; sign in again)"
        )
    if reopens:
        signed = "" if removed else ", signed in"
        last = f" It was on {page['url']}. {_REOPEN_NOTE}{signed}."
        said = _notices.get(session_key)
        return f"{said} {restored}.{last}" if said else f"{restored}.{last}"
    # A browser that stopped in this process: its page is not opened again, it may be the one that stopped it.
    last = f" It was on {page['url']}." if page.get("url") else ""
    said = _notices.get(session_key)
    return f"{said} {restored}.{last}" if said else f"{restored}: navigate to the page to go on.{last}"


def _storage_by_origin(state: dict) -> dict[str, list[tuple[str, str]]]:
    """The local storage items of *state* (a Playwright storage state), by lower-cased origin."""
    kept: dict[str, list[tuple[str, str]]] = {}
    for entry in state.get("origins") or []:
        if not isinstance(entry, dict):
            continue
        origin = str(entry.get("origin", "")).lower()
        items = [
            (str(item["name"]), str(item["value"]))
            for item in entry.get("localStorage") or []
            if isinstance(item, dict) and "name" in item and "value" in item
        ]
        if origin and items:
            kept[origin] = items
    return kept


_RESTORE_LOCAL_STORAGE_JS = """
(items) => {
  let written = 0;
  for (const [name, value] of items) {
    if (window.localStorage.getItem(name) === null) {
      window.localStorage.setItem(name, value);
      written++;
    }
  }
  return written;
}
"""

_RESTORE_SESSION_STORAGE_JS = """
(items) => {
  let written = 0;
  for (const [name, value] of items) {
    if (window.sessionStorage.getItem(name) === null) {
      window.sessionStorage.setItem(name, value);
      written++;
    }
  }
  return written;
}
"""


async def _restore_session_storage(session: BrowserSession, origin: str) -> int:
    """Write the kept session storage of *origin* still pending on *session* to its page, never overwriting what the
    page holds; how many items were written. Raises when the page cannot be written (the items stay pending)."""
    items = _pending_session_storage(session).get(origin)
    if items is None:
        return 0
    written = int(await session.page.evaluate(_RESTORE_SESSION_STORAGE_JS, items) or 0)
    session.pending_session_storage.pop(origin, None)
    return written

#: The most times the first navigate of a re-created session opens its page again (:func:`_settle_resumed_page`).
_RESUME_REOPENS = 2


async def _settle_resumed_page(session_key: str, session: BrowserSession, url: str) -> None:
    """Make the first navigate of a session re-created from a kept state land where the run was, signed in.

    The local storage handed to the new context does not always reach the page the agent opens: the first document
    can be about:blank or one whose storage cannot be read yet, and an application signed in by a local storage
    token then shows its sign-in page. On a page of an origin the kept state holds, the items missing there are
    written back and the page is opened again; a navigate that ended on no http(s) document is opened again too, at
    most :data:`_RESUME_REOPENS` times. Each case is logged with the session key.
    """
    page = session.page
    for reopened in range(_RESUME_REOPENS + 1):
        current = str(getattr(page, "url", "") or "")
        origin = _origin_of(current)
        if not origin:
            reason = f"the navigate ended on {current or 'no document'}"
        else:
            items = session.pending_storage.get(origin)
            if items is None and origin not in _pending_session_storage(session):
                return
            try:
                written = int(await page.evaluate(_RESTORE_LOCAL_STORAGE_JS, items) or 0) if items is not None else 0
                # Session storage is read by the application as it loads too (a composer draft): written before
                # the page is opened again, it survives that reload (same tab, same origin).
                in_session = await _restore_session_storage(session, origin)
            except Exception as exc:
                reason = f"the storage of {origin} could not be read ({format_exception_message(exc)})"
            else:
                session.pending_storage.pop(origin, None)
                if not written and not in_session:
                    return
                reason = (
                    f"{written} local storage item(s) and {in_session} session storage item(s) of {origin} written back"
                )
        if reopened == _RESUME_REOPENS:
            logger.warning("Browser session %s re-created: %s, not opened again", session_key, reason)
            return
        logger.warning("Browser session %s re-created: %s, the page is opened again", session_key, reason)
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            logger.warning("Browser session %s re-created: %s not opened again: %s", session_key, url,
                           format_exception_message(exc))
            return


#: How many times the first action of a resumed session tries to open its page again (:func:`_reopen_resumed_page`).
_REOPEN_ATTEMPTS = 2


def _say_not_reopened(
    session_key: str, session: BrowserSession, reason: str, *, next_step: str = "Navigate to the page to go on."
) -> None:
    """Replace the promise of the pending notice: the page was not opened again, and where the action runs instead.

    The notice built at launch says the next action opens the page again first; an action that could not do it
    would otherwise run on about:blank (SecurityError on local storage, blank screenshot) under that promise.
    """
    said = _notices.get(session_key)
    if not said or _REOPEN_NOTE not in said:
        return
    where = str(getattr(session.page, "url", "") or "") or "no document"
    head, _sep, _tail = said.partition(_REOPEN_NOTE)
    _notices[session_key] = (
        f"{head}{_NOT_REOPENED_NOTE} ({reason}): this action ran on {where}. {next_step}"
    )


#: Seconds the reopen waits before it reads a preview admission Redis could not answer once more.
_PREVIEW_RECHECK_SECONDS = 1.0


async def _resume_url_checked(session: BrowserSession, url: str) -> str:
    """*url* as the session's URL policy admits it; a preview admission Redis could not read is read once more."""
    try:
        return await _url_check(session)(url)
    except PreviewRefusedError as exc:
        if exc.state != sandbox_preview.UNREADABLE:
            raise
    await asyncio.sleep(_PREVIEW_RECHECK_SECONDS)
    return await _url_check(session)(url)


async def _reopen_resumed_page(session_key: str, session: BrowserSession) -> bool:
    """Open again the page a session resumed from another process's state was on, before its first action.

    The re-created page starts on about:blank, where a script reading local storage raises SecurityError and a
    screenshot is blank, while the resumed run still believes it is on its page: the first action of any kind opens
    that page again, held to the session's URL policy, and settles it (:func:`_settle_resumed_page`). Once only, best
    effort: a refusal or a failure is logged with the session key and the action runs where the page is. True when
    this call opened the page again (the caller may give it time to paint, :func:`_capture_reopened_page`).
    """
    url = getattr(session, "resume_url", None)  # a session installed or stubbed without one has nothing to reopen
    if not url:
        return False
    session.resume_url = None
    try:
        safe_url = await _resume_url_checked(session, url)
    except ValueError as exc:
        reason = f"URL policy: {format_exception_message(exc)}"
        logger.warning("Browser session %s resumed: %s not opened again (%s)", session_key, url, reason)
        # A navigate is held to the same policy: promise it only when the refusal says it may pass.
        next_step = (
            "Navigate to the page to go on."
            if isinstance(exc, PreviewRefusedError) and exc.state == sandbox_preview.UNREADABLE
            else "A navigate to it is held to the same policy."
        )
        _say_not_reopened(session_key, session, reason, next_step=next_step)
        return False
    failure: str | None = None
    for _attempt in range(_REOPEN_ATTEMPTS):
        try:
            await session.page.goto(safe_url, wait_until="domcontentloaded")
        except Exception as exc:
            failure = format_exception_message(exc)
            continue
        landed = str(getattr(session.page, "url", "") or "")
        if not _origin_of(landed):  # an error page or about:blank: no http(s) document to run on
            failure = f"it ended on {landed or 'no document'}"
            continue
        failure = None
        break
    if failure is not None:
        logger.warning("Browser session %s resumed: %s not opened again: %s", session_key, url, failure)
        _say_not_reopened(session_key, session, failure)
        return False
    logger.warning("Browser session %s resumed: %s opened again before its first action", session_key, url)
    if session.pending_storage or _pending_session_storage(session):
        await _settle_resumed_page(session_key, session, safe_url)
    return True


#: The longest a screenshot that is the first action after a resume waits for the page it opened again to paint.
_REOPENED_PAINT_SECONDS = 10.0
#: The pause between two captures of a reopened page that still showed one colour only (a loading screen).
_REOPENED_PAINT_STEP_SECONDS = 1.0
#: What the notice adds when the reopened page had still not painted at the end of that wait.
_NOT_SETTLED_NOTE = "The page was opened again but had not painted after {seconds:g} s: capture again after a wait."
#: The clock and the pause of that wait: module seams so a test measures the bound in fake time, never on the
#: runner's wall clock (#9739).
_paint_clock: Callable[[], float] = time.monotonic
_paint_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


async def _capture_reopened_page(
    session_key: str, session: BrowserSession, capture: Callable[[], Awaitable[bytes]]
) -> bytes:
    """The capture of a page a resume has just opened again, once it has painted (bounded).

    A single-page application reopened by :func:`_reopen_resumed_page` shows its loading screen for a while after
    ``domcontentloaded``: captured at once, the PNG is that screen, one colour (#9712). The page is given its network
    idle, then captured until the capture is no longer blank (:func:`app.services.image_summary.looks_blank`), at most
    :data:`_REOPENED_PAINT_SECONDS`; still blank then, the last capture is returned and the notice says the page had
    not settled. Only this case waits: a screenshot of a session that was not resumed is taken as before.
    """
    from app.services.image_summary import looks_blank, summarize_image

    deadline = _paint_clock() + _REOPENED_PAINT_SECONDS
    with contextlib.suppress(Exception):  # a page that never idles (a poll, a socket) still gets the capture loop
        await session.page.wait_for_load_state("networkidle", timeout=_REOPENED_PAINT_SECONDS * 1000)
    while True:
        image = await capture()
        if not looks_blank(await summarize_image(image)):
            return image
        left = deadline - _paint_clock()
        if left <= 0:
            break
        await _paint_sleep(min(_REOPENED_PAINT_STEP_SECONDS, left))
    logger.warning("Browser session %s resumed: the reopened page had not painted after %ss", session_key,
                   _REOPENED_PAINT_SECONDS)
    note = _NOT_SETTLED_NOTE.format(seconds=_REOPENED_PAINT_SECONDS)
    said = _notices.get(session_key)
    _notices[session_key] = f"{said} {note}" if said else note
    return image


#: True once the document has loaded and its body shows text: a single-page application has mounted its root.
_RENDERED_JS = (
    "() => document.readyState === 'complete' && !!document.body && document.body.innerText.trim().length > 0"
)
#: What a script error on a page a resume has just opened again adds to the error (:func:`browser_evaluate`).
_REOPENED_SCRIPT_NOTE = "The page was opened again by the resume just before this script: run it again."


async def _await_reopened_render(session_key: str, session: BrowserSession) -> bool:
    """Wait, bounded, for a page a resume has just opened again to render before the first script runs on it.

    :func:`_reopen_resumed_page` returns at ``domcontentloaded``: a single-page application has not mounted yet, and
    a script reading ``document.querySelector('main')`` there gets null (#9793). The page is given its ``load`` event
    and its network idle, then read until :data:`_RENDERED_JS` holds, at most :data:`_REOPENED_PAINT_SECONDS` in all
    on the clock of :func:`_capture_reopened_page`. True when it rendered; False is logged and the script runs anyway.
    """
    deadline = _paint_clock() + _REOPENED_PAINT_SECONDS
    for state in ("load", "networkidle"):
        left = deadline - _paint_clock()
        if left <= 0:
            break
        with contextlib.suppress(Exception):  # a page that never idles (a poll, a socket) still gets the read loop
            await session.page.wait_for_load_state(state, timeout=left * 1000)
    while True:
        with contextlib.suppress(Exception):  # a document replaced under the read is read again
            if await session.page.evaluate(_RENDERED_JS):
                return True
        left = deadline - _paint_clock()
        if left <= 0:
            break
        await _paint_sleep(min(_REOPENED_PAINT_STEP_SECONDS, left))
    logger.warning("Browser session %s resumed: the reopened page had not rendered after %ss", session_key,
                   _REOPENED_PAINT_SECONDS)
    return False


async def _discard_launch(browser: Any, pw: Any) -> None:
    """Close what a failed launch had started, best effort."""
    for close in (getattr(browser, "close", None), getattr(pw, "stop", None)):
        if close is None:
            continue
        try:
            await close()
        except Exception as exc:
            logger.debug("Best-effort cleanup of a failed browser launch failed: %s", format_exception_message(exc))


# ── URL policy ───────────────────────────────────────────────────────────────


def _is_blocked_request_url(url: str) -> bool:
    """Cheap synchronous check used by the Playwright route filter.

    Blocks non-http(s) schemes and literal non-global IP hosts (private
    ranges, loopback, link-local/cloud metadata, shared address space).
    Hostnames are NOT DNS-resolved here — that is the job of
    :func:`validate_outbound_url` on explicit navigations; this filter is
    a lightweight defense-in-depth net for in-page requests.
    """
    try:
        parsed = urlparse(url)
        if (parsed.scheme or "").lower() not in ("http", "https"):
            return True
        host = parsed.hostname or ""
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False  # Not a literal IP — allow (validated elsewhere)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return not ip.is_global
    except Exception:
        return True


class PreviewRefusedError(ValueError):
    """A sandbox service preview URL the policy refuses, with why (:func:`sandbox_preview.admission`)."""

    def __init__(self, state: str) -> None:
        self.state = state
        super().__init__(_PREVIEW_REFUSALS[state])


#: What a refusal of a URL shaped as a sandbox service preview says instead of the outbound policy's generic
#: private-address error, so a run knows whether a navigate to it can work.
_PREVIEW_REFUSALS = {
    sandbox_preview.NOT_ADMITTED: (
        "this sandbox service preview is no longer admitted (the service was stopped, or it restarted on another "
        "address): a navigate to it is refused too; sandbox_service_status answers the current preview_url"
    ),
    sandbox_preview.UNREADABLE: (
        "the admission of this sandbox service preview could not be read just now: a navigate to it may work in a "
        "moment"
    ),
}


async def _outbound_or_preview(url: str) -> str:
    """The outbound URL policy, plus the preview of a started sandbox service."""
    state = await sandbox_preview.admission(url)
    if state == sandbox_preview.ADMITTED:
        return url
    try:
        return await validate_outbound_url_async(url)
    except ValueError:
        if state in _PREVIEW_REFUSALS:
            raise PreviewRefusedError(state) from None
        raise


async def _route_filter(route: Any) -> None:
    """Playwright route handler aborting requests to blocked URLs.

    A request to a started sandbox service's preview goes on with the
    OpenSandbox server's key added here, by the platform: the page never holds it.
    An agentic coding lab's preview (a pod IP) goes on with nothing added.
    """
    url = route.request.url
    if await preview_allowed(url):
        await route.continue_(headers={**route.request.headers, **preview_headers(url)})
    elif _is_blocked_request_url(url):
        await route.abort("blockedbyclient")
    else:
        await route.continue_()


async def _fail_offline(route: Any) -> None:
    """Route handler of an offline session: every new request fails as a cut network would fail it.

    ``BrowserContext.set_offline`` alone lets a request through on a connection already open (an HTTP keep-alive
    or HTTP/2 connection to the same origin), so a page's error state could not be provoked. Registered after
    :func:`_route_filter`, it runs first; removed when the session goes back online.
    """
    await route.abort("internetdisconnected")


# ── Browser actions ──────────────────────────────────────────────────────────


class PageNavigationError(Exception):
    """A page script could not complete across a navigation of the page."""


class PageKeptNavigatingError(PageNavigationError):
    """The page settled on no document the tool could read: it had not loaded in time, or navigated again."""


class PageBlockedByPolicyError(PageNavigationError):
    """The page navigated to a URL the outbound URL policy refuses; nothing was read from it."""


class EvaluateTimeoutError(Exception):
    """An ``evaluate`` script that ran past its timeout: stopped, the session kept."""


class ReopenedPageScriptError(Exception):
    """A script failed on the page a resume had just opened again: the error says it may be run again (#9793)."""


class StillOnPreviousPageError(PageNavigationError):
    """The first ``evaluate`` after a navigate found the page still on the document the navigate left: not run."""


class ClickTimeoutError(Exception):
    """A click timed out; the message says how far it got, and on which page."""


def _playwright_errors() -> tuple[type[Exception], type[Exception]] | None:
    """Playwright's ``Error`` and ``TimeoutError`` classes (``None`` without Playwright)."""
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    except ImportError:
        return None
    return PlaywrightError, PlaywrightTimeoutError


def _is_navigation_race(exc: BaseException) -> bool:
    errors = _playwright_errors()
    return errors is not None and isinstance(exc, errors[0]) and _NAVIGATION_RACE_RE.match(str(exc)) is not None


def _playwright_timeout_ms(exc: BaseException) -> int | None:
    """The time a Playwright action waited before its ``TimeoutError``; ``None`` for any other error."""
    errors = _playwright_errors()
    if errors is None or not isinstance(exc, errors[1]):
        return None
    match = _PLAYWRIGHT_TIMEOUT_RE.match(str(exc))
    return int(match.group(1)) if match else None


async def _await_new_document(session: BrowserSession, mark: int) -> bool:
    """Wait, bounded, for a document committed after *mark* to reach DOMContentLoaded."""
    if await session.documents.loaded_after(mark, ACTION_TIMEOUT_MS / 1000):
        return True
    logger.debug("Browser page had no new document loaded %d ms after a navigation race", ACTION_TIMEOUT_MS)
    return False


def _not_checked_in_time() -> str:
    return f"its address could not be checked within {ACTION_TIMEOUT_MS / 1000:g} s"


def _url_check(session: BrowserSession | None) -> Callable[[str], Awaitable[str]]:
    """The URL check of *session*: its own (a browser profile's policy), else the outbound URL policy."""
    if session is not None and not session.closed and session.check_url is not None:
        return session.check_url
    return _outbound_or_preview


async def _url_policy_refusal(url: str, session: BrowserSession | None = None) -> str | None:
    """Why *session*'s URL policy refuses *url*, or ``None`` when it allows it (fails closed)."""
    try:
        await asyncio.wait_for(_url_check(session)(url), ACTION_TIMEOUT_MS / 1000)
    except ValueError as exc:
        return format_exception_message(exc)
    except TimeoutError:
        return _not_checked_in_time()
    return None


async def _require_allowed(url: str, session: BrowserSession) -> None:
    refusal = await _url_policy_refusal(url, session)
    if refusal is not None:
        raise PageBlockedByPolicyError(
            f"the page navigated while it was read, to {url}, which the browser's URL policy does "
            f"not allow ({refusal}); nothing was read from the new document."
        )


async def _read_page(
    session: BrowserSession,
    read: Callable[[], Awaitable[Any]],
    reread: Callable[[], Awaitable[tuple[str, Any]]],
) -> tuple[Any, bool]:
    """Run one of the tool's OWN page reads; after a navigation race, read the new document once.

    Only for the reads this module writes (the page text, its title): they
    change nothing in the page, so a second run cannot act twice. An agent's
    script is never run again (:func:`browser_evaluate`). The new document is
    read only once it loaded within ``ACTION_TIMEOUT_MS`` and the outbound URL
    policy allows the URL the page reports; *reread* returns the URL it read
    with its result, kept only when the policy allows that one too, and is
    itself bounded by ``ACTION_TIMEOUT_MS``. Returns ``(result, navigated)``.
    """
    page = session.page
    mark = session.documents.committed
    bound = ACTION_TIMEOUT_MS / 1000
    try:
        return await read(), False
    except Exception as exc:
        if not _is_navigation_race(exc):
            raise
    if not await _await_new_document(session, mark):
        raise PageKeptNavigatingError(
            f"the page navigated while it was read, and the new document had not loaded after {bound:g} s "
            f"(page now at {page.url}); nothing was read from it. Wait for the page to settle, then read it again."
        )
    await _require_allowed(page.url, session)
    try:
        url, result = await asyncio.wait_for(reread(), bound)
    except TimeoutError as exc:
        raise PageNavigationError(
            f"the page navigated to {page.url} while it was read, and reading the new document did not "
            f"finish within {bound:g} s."
        ) from exc
    except Exception as exc:
        if _is_navigation_race(exc):
            raise PageKeptNavigatingError(
                "the page kept navigating: it navigated while it was read, and again while the new "
                f"document was read (page now at {page.url}). Wait for the page to settle, then read it again."
            ) from exc
        raise PageNavigationError(
            f"the page navigated to {page.url} while it was read; reading the new document failed: {exc}"
        ) from exc
    await _require_allowed(url, session)
    return result, True


async def _page_title(session: BrowserSession) -> str:
    """The page title; a navigation still in flight leaves it unknown, never fails the action."""
    page = session.page

    async def reread() -> tuple[str, str]:
        url, title = await page.evaluate(_PAGE_URL_AND_TITLE_JS)
        return str(url), str(title)

    try:
        title, _navigated = await _read_page(session, page.title, reread)
    except PageBlockedByPolicyError:
        return "(title unavailable: the page moved to a URL the browser's URL policy does not allow)"
    except PageKeptNavigatingError:
        return "(title unavailable: the page is still navigating)"
    except PageNavigationError:
        return "(title unavailable: the page navigated and its new title could not be read)"
    return title


async def _landed_url(session: BrowserSession) -> str:
    """The URL the document a navigate reached reports itself (``location.href``), read in the page.

    ``page.url`` is the URL the browser committed; an application that routes on its own after the load (a SPA
    guard sending the page back, a ``history.replaceState``) leaves the document elsewhere, and the next script
    reads that place. When the two disagree, the page is given until its ``load`` event and read once more, so
    the answer names where a script runs next. A page that cannot be read keeps the committed URL.
    """
    page = session.page
    committed = str(getattr(page, "url", "") or "")
    bound = ACTION_TIMEOUT_MS / 1000

    async def href() -> str:
        value = await asyncio.wait_for(page.evaluate(_PAGE_HREF_JS), bound)
        return value if isinstance(value, str) and value.startswith(("http://", "https://")) else ""

    try:
        landed = await href()
        if landed and landed != committed:
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("load", timeout=ACTION_TIMEOUT_MS)
            landed = await href()
    except Exception as exc:
        logger.debug("Browser navigate: location.href not read (%s)", format_exception_message(exc))
        return committed
    return landed or committed


def _landed_elsewhere(requested: str, landed: str) -> str:
    """The line a navigate adds when the page ended somewhere else than the URL asked, else ``""``."""
    if not landed or _same_location(requested, landed):
        return ""
    return (
        f"\nNote: not the requested {requested} (a redirect or the application's own routing); scripts run on {landed}."
    )


def _same_location(a: str, b: str) -> bool:
    """Whether two URLs name the same page: a trailing slash on the path and an empty fragment are not a difference."""

    def norm(url: str) -> tuple[str, str, str, str]:
        parts = urlparse(url)
        return parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", parts.query

    try:
        return norm(a) == norm(b)
    except ValueError:
        return a == b


def _same_path(a: str, b: str) -> bool:
    """Whether two URLs share scheme, host and path (a trailing slash aside), whatever their query and fragment.

    A page that normalises its own address after its load (``history.replaceState`` adding a default ``?vis=all``)
    is still the document a navigate to the bare path answered.
    """

    def norm(url: str) -> tuple[str, str, str]:
        parts = urlparse(url)
        return parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/"

    try:
        return norm(a) == norm(b)
    except ValueError:
        return a == b


def _keep_landed_page(session_key: str, session: BrowserSession, landed: str) -> None:
    """Make this process's kept page name where the navigate landed, even when the state read after it failed.

    :func:`_remember_state` is best effort: a page busy at that moment leaves the page kept by the PREVIOUS
    navigate, and a session re-created from it (closed idle, its browser stopped) reopened that previous route.
    """
    if not _keeps_state(session) or not landed.startswith(("http://", "https://")):
        return
    kept = _kept_pages.get(session_key)
    if kept is not None and kept.get("url") != landed:
        kept["url"] = landed


async def _share_landed_page(session_key: str, session: BrowserSession, landed: str) -> None:
    """Share the kept state under the page a navigate landed on, when the state read after it shared nothing.

    :func:`_remember_state` is best effort (a page busy, the kept local storage of the origin not written back yet):
    without this the shared copy kept naming the page of the PREVIOUS navigate, and a run resumed in another
    process reopened that page and ran its next action there, the navigate lost (#9318). The cookies and local
    storage shared are the ones kept before; only the page changes.
    """
    if not _keeps_state(session) or not landed.startswith(("http://", "https://")):
        return
    kept = _kept_states.get(session_key)
    if kept is None:
        return
    page = dict(_kept_pages.get(session_key) or {})
    page["url"] = landed
    if session.viewport:
        page["viewport"] = dict(session.viewport)
    if _scale_of(session) != 1:
        page["scale"] = _scale_of(session)
    if session.media:
        page["media"] = dict(session.media)
    if session.offline:
        page["offline"] = True
    _kept_pages[session_key] = page
    await _share_state(session_key, kept[0], page)


async def _share_kept_viewport(session_key: str, session: BrowserSession) -> None:
    """Share the viewport the ``viewport`` action set, when the state read after it shared nothing.

    :func:`_remember_state` is best effort (a page busy, the kept local storage of the origin not written back yet):
    without this the kept page kept the previous size, and the session re-created after a resume opened at
    1280 x 720 while the run believed it was still at the size it set (#10599). The cookies and local storage
    shared are the ones kept before; only the viewport changes. A session with nothing kept has nothing to share.
    """
    if not _keeps_state(session) or not session.viewport:
        return
    kept = _kept_states.get(session_key)
    if kept is None:
        return
    page = dict(_kept_pages.get(session_key) or {})
    scale = _scale_of(session)
    if page.get("viewport") == session.viewport and _kept_scale(page.get("scale")) == scale:
        return
    page["viewport"] = dict(session.viewport)
    if scale != 1:
        page["scale"] = scale
    else:
        page.pop("scale", None)
    _kept_pages[session_key] = page
    await _share_state(session_key, kept[0], page)


async def _share_kept_media(session_key: str, session: BrowserSession) -> None:
    """Share the media features the ``emulate_media`` action set, when the state read after it shared nothing.

    The same best-effort gap as :func:`_share_kept_viewport`: without it a session re-created after a resume opened
    with no reduced motion and a light scheme while the run believed it still emulated them. The cookies and local
    storage shared are the ones kept before; only the media features change.
    """
    if not _keeps_state(session):
        return
    kept = _kept_states.get(session_key)
    if kept is None:
        return
    page = dict(_kept_pages.get(session_key) or {})
    if (page.get("media") or None) == (session.media or None):
        return
    if session.media:
        page["media"] = dict(session.media)
    else:
        page.pop("media", None)
    _kept_pages[session_key] = page
    await _share_state(session_key, kept[0], page)


async def _share_kept_offline(session_key: str, session: BrowserSession) -> None:
    """Share the network state the ``network`` action set, when the state read after it shared nothing.

    The same best-effort gap as :func:`_share_kept_media`: without it a session re-created after a resume came back
    online while the run believed its page was still cut from the network.
    """
    if not _keeps_state(session):
        return
    kept = _kept_states.get(session_key)
    if kept is None:
        return
    page = dict(_kept_pages.get(session_key) or {})
    if (page.get("offline") is True) == session.offline:
        return
    if session.offline:
        page["offline"] = True
    else:
        page.pop("offline", None)
    _kept_pages[session_key] = page
    await _share_state(session_key, kept[0], page)


def navigation_timeout(asked: object) -> float:
    """The seconds a ``navigate`` waits for its page: *asked* within 1 and :data:`MAX_NAVIGATION_TIMEOUT_SECONDS`,
    else :data:`NAVIGATION_TIMEOUT_MS` in seconds."""
    default = NAVIGATION_TIMEOUT_MS / 1000
    try:
        seconds = float(asked) if asked is not None else default
    except (TypeError, ValueError):
        seconds = default
    if seconds != seconds:  # NaN
        seconds = default
    return min(MAX_NAVIGATION_TIMEOUT_SECONDS, max(1.0, seconds))


def _still_loading(session: BrowserSession, committed: int, seconds: float | None = None) -> str | None:
    """The answer of a navigate whose document committed but did not reach DOMContentLoaded in time, else ``None``.

    A page that loads hundreds of unbundled modules (a Vite dev server on its first visit) commits at once and
    reaches DOMContentLoaded long after the navigation timeout. The navigation happened and the page keeps
    loading: failing it makes the agent navigate again, which starts the load over. A timeout before any commit
    (nothing answered) stays an error.
    """
    if session.documents.committed <= committed:
        return None
    return (
        f"Navigated to: {session.page.url}\n"
        f"Still loading: the document arrived but did not finish loading within "
        f"{seconds if seconds is not None else NAVIGATION_TIMEOUT_MS / 1000:g} s;"
        " it keeps loading. Do not navigate again (that restarts the load): wait for the page with evaluate"
        " or snapshot."
    )


async def _finish_loading(session_key: str, session: BrowserSession) -> None:
    """Let the document a navigate left "Still loading" finish before the next navigate leaves it, then keep its state.

    A sign-in page opened from its URL (``/login#token=``) on a slow dev server answers "Still loading"; a second
    navigate called in the same turn ran as soon as the first answered and aborted that document before its script
    stored the token, so the session went on signed out, and the state kept at the next rollout resumed it signed
    out (cookies restored 0, the sign-in page). The wait is bounded by :data:`NAVIGATION_TIMEOUT_MS`; a document
    that still has not loaded is left, logged with the session key.
    """
    mark = session.loading_mark
    if mark is None:
        return
    session.loading_mark = None
    loaded = await session.documents.loaded_after(mark, NAVIGATION_TIMEOUT_MS / 1000)
    if not loaded:
        logger.warning(
            "Browser session %s: the document left loading by the previous navigate did not load within "
            "%g s, left as it is",
            session_key,
            NAVIGATION_TIMEOUT_MS / 1000,
        )
        return
    await _settle_after_load(session)
    logger.info(
        "Browser session %s: the document left loading by the previous navigate loaded before this one", session_key
    )
    await _remember_state(session_key, session)


#: How long after a navigate its state is kept again (:func:`_keep_after_settle`).
_SETTLED_KEEP_SECONDS = 2.0
#: The deferred keeps in flight, held so that none is collected before it ran.
_settle_keeps: set[asyncio.Task] = set()


def _keep_after_settle(session_key: str, session: BrowserSession) -> None:
    """Keep the session's state again :data:`_SETTLED_KEEP_SECONDS` after a navigate, when nothing used it since.

    A navigate keeps the state at DOMContentLoaded. A page that signs in from its URL (a ``/login#token=``
    callback) writes its token from its own script a moment later: a run handed back before any other action
    resumed from the state the navigate kept, signed out (0 sites restored). An action in between keeps the state
    itself, so the deferred keep stands down. The run handed back meanwhile keeps every open session first
    (``assignment_run_resume.hand_back``).
    """
    if not _keeps_state(session):
        return
    used = session.last_used

    async def keep() -> None:
        await asyncio.sleep(_SETTLED_KEEP_SECONDS)
        if session.closed or session.last_used != used or _sessions.get(session_key) is not session:
            return
        async with session.lock:
            if session.closed or session.last_used != used or not _alive(session):
                return
            # Stamped with the navigate: a copy the run shared since from another process stays.
            await _remember_state(session_key, session, over_newer=False)

    task = asyncio.get_running_loop().create_task(keep(), context=contextvars.Context())
    _settle_keeps.add(task)
    task.add_done_callback(_settle_keeps.discard)


#: How long the first navigate of a re-created session watches its page after the load (:func:`_hold_requested_url`).
_RESUME_SETTLE_SECONDS = 1.0


async def _page_href(session: BrowserSession) -> str:
    """``location.href`` of the page, ``""`` when it cannot be read or is no http(s) document."""
    try:
        value = await asyncio.wait_for(session.page.evaluate(_PAGE_HREF_JS), ACTION_TIMEOUT_MS / 1000)
    except Exception as exc:
        logger.debug("Browser navigate: location.href not read (%s)", format_exception_message(exc))
        return ""
    return value if isinstance(value, str) and value.startswith(("http://", "https://")) else ""


async def _settle_after_load(session: BrowserSession) -> None:
    """Give the page its ``load`` event, then :data:`_RESUME_SETTLE_SECONDS` for the routing it runs after it."""
    with contextlib.suppress(Exception):
        await session.page.wait_for_load_state("load", timeout=ACTION_TIMEOUT_MS)
    await asyncio.sleep(_RESUME_SETTLE_SECONDS)


async def _hold_requested_url(session_key: str, session: BrowserSession, url: str) -> None:
    """Make the first navigate of a re-created session end on *url*, not on the page the restore brought back.

    The local storage written back to a re-created session (:func:`_settle_resumed_page`) can carry the state an
    application routes from: after its load it moved the document back to the URL it had kept (a debounced
    ``history.replaceState`` of filters held in storage), and the navigate answered the requested URL while the next
    script ran on the previous one (#9576). The page is watched past its load; when it left *url* it is opened
    there once more, now that its storage is settled. Once only: an application that still routes away is named
    by the answer's landed URL (:func:`_landed_elsewhere`).
    """
    await _settle_after_load(session)
    moved = await _page_href(session)
    if not moved or _same_location(url, moved):
        return
    logger.warning("Browser session %s re-created: the page moved from %s to %s after its load, opened again",
                   session_key, url, moved)
    try:
        await session.page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:
        logger.warning("Browser session %s re-created: %s not opened again: %s", session_key, url,
                       format_exception_message(exc))
        return
    await _settle_after_load(session)


def _only_fragment_differs(a: str, b: str) -> bool:
    """Whether *b* is *a* with another fragment: a change the browser makes inside the open document by design."""
    try:
        return urlparse(a)._replace(fragment="") == urlparse(b)._replace(fragment="") and a != b
    except ValueError:
        return False


async def _load_if_same_document(session: BrowserSession, before: str, url: str, response: Any, mark: int) -> str:
    """Make a navigate that loaded no new document load one, or say so; the line it adds to the answer, else ``""``.

    ``page.goto`` answers no response when the browser changed the URL inside the open document instead of loading
    a new one: a fragment change, or a single-page application that intercepts the navigation and routes on its
    own. The answer then named the requested URL while the DOM, the next script and the next screenshot were still
    the previous route (#9635). Any such change is reloaded so the requested page is the document the next action
    reads. A fragment-only change is reloaded too (#10458): what the open document made of it depended on its own
    ``hashchange`` handling and on the state it already held (a signed-in ``/login#token=`` URL opened on a page
    already at ``/login`` left the next script on the signed-out page); a fresh load reads the fragment the way a
    first visit does, and the answer says so.
    """
    if response is not None or session.documents.loaded_since(mark):
        return ""
    fragment_only = _only_fragment_differs(before, url)
    logger.info("Browser navigate: %s changed the open document without loading it, reloaded", url)
    try:
        await session.page.reload(wait_until="domcontentloaded")
    except Exception as exc:
        return (
            "\nNote: the browser changed the URL inside the open document (no new load) and the reload that should"
            f" load it failed ({format_exception_message(exc)}); the page may still show the previous route."
        )
    if fragment_only:
        await _settle_fragment_reload(session)
        return "\nNote: only the fragment differed, so the page was loaded again to read it as a first visit does."
    return ""


#: How long a fragment-only reload waits for the page to go quiet after its load (:func:`_settle_fragment_reload`).
_FRAGMENT_SETTLE_SECONDS = 5.0


async def _settle_fragment_reload(session: BrowserSession) -> None:
    """Give a page reloaded for its fragment the time its own scripts take to read it (#10464).

    A page reads a callback fragment from its scripts after DOMContentLoaded: a lazily loaded route, an effect, a
    fetch, then a ``history.replaceState`` or a client-side redirect (a signed-in ``/login#token=`` URL storing its
    token and moving to ``/``). Answered at DOMContentLoaded, the navigate named the fragment URL and the next script
    could read the page before the token was stored. The page is given its ``load`` event, then until the network is
    quiet, within :data:`_FRAGMENT_SETTLE_SECONDS`; a page that never goes quiet is answered at the bound.
    """
    bound_ms = _FRAGMENT_SETTLE_SECONDS * 1000
    for state in ("load", "networkidle"):
        try:
            await session.page.wait_for_load_state(state, timeout=bound_ms)
        except Exception as exc:
            logger.debug(
                "Browser navigate: fragment reload not settled at %s (%s)", state, format_exception_message(exc)
            )
            return


#: The bound of the settle every navigate waits after its document arrived (:func:`_settle_navigation`).
_NAVIGATE_SETTLE_SECONDS = 4.0
#: The network-quiet share of that bound: an application that polls never goes quiet and is answered at it.
_NAVIGATE_IDLE_SECONDS = 1.5
#: How long ``location.href`` must stay the same before the page counts as routed.
_NAVIGATE_QUIET_SECONDS = 0.4
_NAVIGATE_POLL_SECONDS = 0.1


async def _settle_navigation(session: BrowserSession) -> bool:
    """Wait until the document a navigate reached is settled; whether it settled within the bound.

    ``page.goto`` answers at DOMContentLoaded. A single-page application routes on its own after that (a route it
    restores from storage, a ``/login#token=`` callback that stores the token after a fetch and moves to ``/``, a
    default query string written back): the answer named one URL while the next ``evaluate`` or ``click`` read the
    previous route or a half-rendered one (UX runs 544d96bd, b26cd84b, 4f7d0136). The page is given its ``load``
    event, a short network-quiet window (:data:`_NAVIGATE_IDLE_SECONDS`), then until ``location.href`` held still for
    :data:`_NAVIGATE_QUIET_SECONDS`, all within :data:`_NAVIGATE_SETTLE_SECONDS`. A page still moving at the bound
    answers ``False``: the navigate names the URL it shows then and says it was still moving.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _NAVIGATE_SETTLE_SECONDS

    def left_ms(cap: float | None = None) -> float:
        left = max(deadline - loop.time(), 0.0)
        return (min(left, cap) if cap is not None else left) * 1000

    for state, cap in (("load", None), ("networkidle", _NAVIGATE_IDLE_SECONDS)):
        timeout = left_ms(cap)
        if timeout <= 0:
            break
        try:
            await session.page.wait_for_load_state(state, timeout=timeout)
        except Exception as exc:
            logger.debug("Browser navigate: not settled at %s (%s)", state, format_exception_message(exc))
    href = await _page_href(session)
    if not href:
        return True
    still_since = loop.time()
    while loop.time() - still_since < _NAVIGATE_QUIET_SECONDS:
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(_NAVIGATE_POLL_SECONDS)
        now = await _page_href(session)
        if not now:
            return True
        if now != href:
            href, still_since = now, loop.time()
    return True


def _still_moving(settled: bool, landed: str) -> str:
    """The line a navigate adds when its page kept changing its URL past the settle bound, else ``""``."""
    if settled:
        return ""
    return (
        f"\nNote: the page was still changing its URL after {_NAVIGATE_SETTLE_SECONDS:g} s; it showed {landed} when"
        " this answered. Check location.href with evaluate before relying on the page."
    )


async def browser_navigate(session_key: str, url: str, timeout: float | None = None) -> str:
    """Navigate to a URL and return the page title + URL.

    *timeout* is how long the navigation waits for the page, in seconds (:func:`navigation_timeout`): a dev server
    that compiles a route on its first visit answers nothing for longer than the default, and a navigate that
    timed out before any document arrived restarts that wait when it is called again.

    The navigate answers once the page settled (:func:`_settle_navigation`): its ``load`` event, a short quiet
    network window and ``location.href`` held still, so the next action reads the document the answer names. The
    answer's first line is that final URL; a page that ended elsewhere than the URL asked is named by a note.

    The URL is validated against the shared outbound SSRF policy
    (http/https only, globally routable addresses) before any browser
    session is created or any navigation occurs; a redirect to a URL the
    policy refuses stops the navigation there and is refused the same way,
    naming the hop (:class:`RedirectGuard`). An open browser profile session
    checks with the profile's policy instead (:attr:`BrowserSession.check_url`).
    """
    try:
        safe_url = await _url_check(_sessions.get(session_key))(url)
    except ValueError as exc:
        return f"Navigation blocked by URL policy: {exc}"

    session = await _get_or_create_session(session_key)
    async with session.lock:
        session.last_used = time.time()
        await _finish_loading(session_key, session)
        # An explicit navigate goes where it is told; it settles a resumed session itself.
        recreated = session.resume_url is not None or bool(session.pending_storage)
        if session.resume_url is not None:
            said = _notices.get(session_key)
            if said:
                _notices[session_key] = said.replace(_REOPEN_NOTE, _NAVIGATED_NOTE)
        session.resume_url = None
        session.navigated = None  # set again below once this navigate answered
        mark = session.redirects.main_frame_refusals
        committed = session.documents.committed
        session.console.clear()
        before = str(getattr(session.page, "url", "") or "")
        session.navigated_at = time.time() * 1000
        bound = navigation_timeout(timeout)
        try:
            if timeout is None:
                response = await session.page.goto(safe_url, wait_until="domcontentloaded")
            else:
                response = await session.page.goto(safe_url, wait_until="domcontentloaded", timeout=bound * 1000)
        except Exception as exc:
            refusal = session.redirects.refusal_since(mark)
            errors = _playwright_errors()
            if errors is not None and refusal is None and isinstance(exc, errors[1]):
                loading = _still_loading(session, committed, bound)
                if loading is not None:
                    session.loading_mark = committed
                    _keep_landed_page(session_key, session, str(getattr(session.page, "url", "") or ""))
                    return loading
            if refusal is None or errors is None or not isinstance(exc, errors[0]):
                raise
            return refusal.describe()
        same_document = await _load_if_same_document(session, before, safe_url, response, committed)
        if session.pending_storage or _pending_session_storage(session):
            await _settle_resumed_page(session_key, session, safe_url)
        if recreated:
            await _hold_requested_url(session_key, session, safe_url)
        settled = await _settle_navigation(session)
        title = await _page_title(session)
        landed = await _landed_url(session)
        shared = await _remember_state(session_key, session)
        _keep_landed_page(session_key, session, landed)
        if shared is None:
            await _share_landed_page(session_key, session, landed)
        _keep_after_settle(session_key, session)
        session.navigated = (before, landed) if landed.startswith(("http://", "https://")) else None
        return (
            f"Navigated to: {landed}\nTitle: {title}"
            + _landed_elsewhere(safe_url, landed)
            + same_document
            + _still_moving(settled, landed)
        )


async def browser_get_content(session_key: str) -> str:
    """Get the text content of the current page."""
    session = await _get_or_create_session(session_key)
    async with session.lock:
        session.last_used = time.time()
        await _reopen_resumed_page(session_key, session)
        page = session.page

        async def reread() -> tuple[str, dict[str, Any]]:
            snapshot = await page.evaluate(_PAGE_CONTENT_JS)
            return str(snapshot.get("url") or ""), snapshot

        # Get text content (more useful than raw HTML for agents)
        snapshot, navigated = await _read_page(session, lambda: page.evaluate(_PAGE_CONTENT_JS), reread)
        text = str(snapshot.get("text") or "").strip()
        if len(text) > MAX_PAGE_CONTENT_CHARS:
            text = text[:MAX_PAGE_CONTENT_CHARS] + "\n\n[Content truncated]"

        note = "[The page navigated while it was read: this is the new document.]\n" if navigated else ""
        # A read-only action keeps the state too: a sign-in token written since the last action is shared.
        await _remember_state(session_key, session)
        return f"{note}URL: {snapshot.get('url')}\nTitle: {snapshot.get('title')}\n\n{text}"


def bound_snapshot(snapshot: str, *, scope: str, limit: int = MAX_PAGE_CONTENT_CHARS) -> str:
    """*snapshot* cut at a line boundary under *limit* characters, the cut said and how to narrow it. Pure.

    A catalog page answered 95,426 characters in one call: the outline is cut
    like ``get_content`` and the answer names the ``selector`` that reads one
    region of the page instead.
    """
    if len(snapshot) <= limit:
        return snapshot
    head = snapshot[:limit]
    cut = head.rfind("\n")
    if cut > 0:
        head = head[:cut]
    return (
        f"{head}\n\n[Snapshot of {scope} truncated at {len(head):,} of {len(snapshot):,} characters: "
        "pass selector (a CSS selector, e.g. 'main' or '[role=dialog]') to outline one region.]"
    )


async def browser_snapshot(session_key: str, selector: str = "") -> str:
    """Get an accessibility tree snapshot of the current page, or of the element *selector* matches first.

    Returns a structured representation of interactive elements,
    headings, and key content — much more useful for agents than
    raw HTML or plain text. The outline is bounded (:func:`bound_snapshot`).
    """
    scope = selector.strip() or "body"
    session = await _get_or_create_session(session_key)
    async with session.lock:
        session.last_used = time.time()
        await _reopen_resumed_page(session_key, session)
        # Page.accessibility was removed in Playwright 1.57 and Page.aria_snapshot
        # only exists from 1.59; Locator.aria_snapshot (1.49+) ships in every
        # release without the former, and the page-level call snapshots the body.
        target = session.page.locator(scope)
        if scope != "body":
            target = target.first
        aria_snapshot = getattr(target, "aria_snapshot", None)
        if aria_snapshot is None:
            raise RuntimeError(
                "The snapshot action needs Playwright's ARIA snapshot API "
                "(Locator.aria_snapshot, Playwright 1.49 or later), which the "
                "installed Playwright does not provide. Use get_content or "
                "screenshot instead."
            )
        # A selector that matches nothing fails within SNAPSHOT_SELECTOR_TIMEOUT_MS, never the 30 s default.
        snapshot = await (aria_snapshot(timeout=SNAPSHOT_SELECTOR_TIMEOUT_MS) if scope != "body" else aria_snapshot())
        await _remember_state(session_key, session)  # as get_content
        if not snapshot or not snapshot.strip():
            if scope != "body":
                return f"No accessibility tree available for '{scope}' on this page."
            return "No accessibility tree available for this page."
        return bound_snapshot(snapshot, scope="the page" if scope == "body" else f"'{scope}'")


async def _describe_click_timeout(page: Any, selector: str, waited_ms: int, exc: Exception) -> str:
    """How far a timed-out click got, read off Playwright's call log, and on which page.

    The log says whether the click was performed: a click that happened and
    then waited for the navigation it started must never read as a click to
    try again. Only a click the log shows was not performed is described by
    what the selector matches now. Without a log, Playwright's text is kept.
    """
    text = str(exc)
    waited = f"{waited_ms / 1000:g} s"
    if _CLICK_DONE_RE.search(text):
        return (
            f"the click on '{selector}' happened, but the navigation it started had not finished "
            f"after {waited} (page at {page.url}). Check the page before clicking again."
        )
    if "\nCall log:" not in text:
        return text
    if _CLICK_PERFORMING_RE.search(text):
        return (
            f"the time ran out while the click on '{selector}' was being performed on {page.url}, "
            f"so it may have happened. Check the page before clicking again. {text}"
        )
    try:
        matches = await page.locator(selector).count()
    except Exception as count_exc:
        logger.debug("Could not count the matches of a timed-out click: %s", format_exception_message(count_exc))
        return text
    if matches == 0:
        return f"no element matches '{selector}' on {page.url} (waited {waited})."
    return (
        f"{matches} element(s) match '{selector}' on {page.url}, but the click was not performed "
        f"within {waited}; Playwright's log says what it was waiting for. {text}"
    )


async def browser_click(session_key: str, selector: str) -> str:
    """Click an element matching the CSS selector."""
    session = await _get_or_create_session(session_key)
    async with session.lock:
        session.last_used = time.time()
        session.navigated = None  # a click may route the page itself: the next evaluate runs where it lands
        await _reopen_resumed_page(session_key, session)
        mark = session.redirects.main_frame_refusals
        try:
            await session.page.click(selector)
        except Exception as exc:
            waited_ms = _playwright_timeout_ms(exc)
            if waited_ms is None:
                raise
            raise ClickTimeoutError(await _describe_click_timeout(session.page, selector, waited_ms, exc)) from exc
        await session.page.wait_for_load_state("domcontentloaded")
        title = await _page_title(session)
        await _remember_state(session_key, session)
        result = f"Clicked '{selector}'. Current page: {session.page.url} — {title}"
        refusal = session.redirects.refusal_since(mark)
        return result if refusal is None else f"{result}\n{refusal.describe()}"


#: Reads what a typed field holds once the page had a task to re-render: the DOM value (``textContent``
#: for a contenteditable) and, on a React controlled input, the ``value`` prop React rendered it with
#: (``null`` when the element carries no such prop). A controlled input whose state did not take the
#: text renders that prop empty while the DOM still shows the text.
_TYPED_VALUE_JS = """async (el) => {
  await new Promise((resolve) => { requestAnimationFrame(() => resolve()); setTimeout(resolve, 100); });
  const dom = ('value' in el) ? el.value : el.textContent;
  let framework = null;
  for (const key of Object.keys(el)) {
    if (!key.startsWith('__reactProps$')) continue;
    const props = el[key];
    if (props && Object.prototype.hasOwnProperty.call(props, 'value')) {
      framework = props.value == null ? '' : String(props.value);
    }
    break;
  }
  return {dom: dom == null ? '' : String(dom), framework: framework};
}"""


#: Lets a field that reacts to its focus render first: two animation frames (a framework commits its
#: focus state and runs its effects within them), at most 200 ms on a page that paints no frame.
_SETTLE_JS = """() => new Promise((resolve) => {
  requestAnimationFrame(() => requestAnimationFrame(() => resolve(true)));
  setTimeout(() => resolve(false), 200);
})"""


async def _focus_and_settle(page: Any, selector: str) -> None:
    """Give *selector* the focus and let the page render what the focus changes before text arrives."""
    await page.focus(selector)
    try:
        await page.evaluate(_SETTLE_JS)
    except Exception as exc:
        logger.debug("Page not settled after focusing %s: %s", selector, format_exception_message(exc))


async def _typed_value(page: Any, selector: str) -> tuple[str, str | None] | None:
    """The DOM value and the framework value of the field *selector* names; ``None`` when unreadable."""
    try:
        read = await page.eval_on_selector(selector, _TYPED_VALUE_JS)
    except Exception as exc:
        logger.debug("Typed value of %s not read: %s", selector, format_exception_message(exc))
        return None
    if not isinstance(read, dict) or not isinstance(read.get("dom"), str):
        return None
    framework = read.get("framework")
    return read["dom"], framework if isinstance(framework, str) else None


def _value_took(read: tuple[str, str | None] | None, text: str) -> bool:
    """Whether the field holds *text* in the DOM and, when it is a controlled input, in its framework state."""
    if read is None:
        return True  # nothing to compare with: the fill is trusted as before
    dom, framework = read
    return dom == text and (framework is None or framework == text)


async def browser_type(session_key: str, selector: str, text: str) -> str:
    """Type text into an input element, focused first, and check the page's own state took it.

    The field gets the focus before the fill, as a person's click would give it: a field that renders
    another element or resets its state on focus (a tag input) otherwise receives the text on an element
    it then replaces. The value is read back; when the DOM or a React controlled input's ``value`` prop
    does not hold the text, the field is cleared and the text is typed again with real key events, and a
    value still different is named in the result rather than reported as typed.
    """
    session = await _get_or_create_session(session_key)
    async with session.lock:
        session.last_used = time.time()
        session.navigated = None
        await _reopen_resumed_page(session_key, session)
        page = session.page
        await _focus_and_settle(page, selector)
        await page.fill(selector, text)
        read = await _typed_value(page, selector)
        retyped = False
        if not _value_took(read, text):
            field = page.locator(selector).first
            await field.fill("")
            await field.press_sequentially(text, timeout=ACTION_TIMEOUT_MS)
            read = await _typed_value(page, selector)
            retyped = True
        # A page may keep what is typed (a token pasted into a field it stores): kept like a click's.
        await _remember_state(session_key, session)
        result = f"Typed into '{selector}': {text[:100]}"
        if not _value_took(read, text) and read is not None:
            dom, framework = read
            held = repr(dom[:100])
            if framework is not None and framework != dom:
                held = f"{held} in the DOM, {framework[:100]!r} in the page state"
            return (
                f"{result}\nThe field does not hold the text: it reads {held}"
                f"{' after a second try with real key events' if retyped else ''}. The page may format, mask or "
                "limit it; read the field before relying on the value."
            )
        if retyped:
            return f"{result} (typed again with real key events: the first fill did not reach the page state)"
        return result


#: Keys one press call may send: a keyboard path of a few dozen stops, never an unbounded loop.
MAX_PRESSED_KEYS = 100

#: Names the focused element after the keys: its tag, role and accessible text, so a run sees where the
#: keys landed without a second call.
_FOCUSED_JS = """() => {
  const el = document.activeElement;
  if (!el || el === document.body) return '';
  const role = el.getAttribute('role');
  const label = (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('title') || '')
    .replace(/\\s+/g, ' ').trim().slice(0, 60);
  return el.tagName.toLowerCase() + (role ? `[role=${role}]` : '') + (label ? ` '${label}'` : '');
}"""


def parse_key_sequence(key: str) -> list[str]:
    """The keys *key* names: one key ('Tab', 'Shift+Tab') or several separated by spaces, each with an
    optional repeat count ('Tab*26 Enter').

    Raises ``ValueError`` on an empty name, a repeat count that is not a positive integer, or more than
    ``MAX_PRESSED_KEYS`` keys in all.
    """
    keys: list[str] = []
    for token in key.split():
        name, repeat = token, 1
        if "*" in token[1:]:  # a bare '*' is the key itself
            name, _, count = token.rpartition("*")
            if not count.isdigit() or int(count) < 1:
                raise ValueError(f"'{token}': a repeat count is a positive integer, as in Tab*3")
            repeat = int(count)
        if len(keys) + repeat > MAX_PRESSED_KEYS:
            raise ValueError(f"at most {MAX_PRESSED_KEYS} keys per press call")
        keys.extend([name] * repeat)
    if not keys:
        raise ValueError("no key named")
    return keys


async def _settle(page: Any) -> None:
    """Let the page render what the last key changed (two frames, at most 200 ms)."""
    try:
        await page.evaluate(_SETTLE_JS)
    except Exception as exc:
        logger.debug("Page not settled after a key press: %s", format_exception_message(exc))


async def _focused(page: Any) -> str:
    """The focused element, described by ``_FOCUSED_JS``; empty when unreadable or the body holds it."""
    try:
        described = await page.evaluate(_FOCUSED_JS)
    except Exception as exc:
        logger.debug("Focused element not read: %s", format_exception_message(exc))
        return ""
    return described if isinstance(described, str) else ""


async def browser_press(session_key: str, key: str) -> str:
    """Press a keyboard key, or a sequence of keys in one call ('Tab*26 Enter').

    Each key is pressed once the page rendered what the previous one changed, so a keyboard path is
    walked at the page's own pace inside a timed window (an undo notice) instead of one tool call per
    key; the result names the element that holds the focus after the last key.
    """
    keys = parse_key_sequence(key)
    session = await _get_or_create_session(session_key)
    async with session.lock:
        session.last_used = time.time()
        session.navigated = None  # Enter may submit a form that routes the page
        await _reopen_resumed_page(session_key, session)
        page = session.page
        for name in keys:
            await page.keyboard.press(name)
            await _settle(page)
        # A sign-in submitted with Enter writes its token now: kept like a click's.
        await _remember_state(session_key, session)
        result = f"Pressed key: {key}" if len(keys) == 1 else f"Pressed {len(keys)} keys: {key}"
        focused = await _focused(page)
        return f"{result}. Focus: {focused}" if focused else result


#: Finds a viewport point the element itself receives the pointer at, or ``null``. Playwright's
#: ``hover`` aims at the centre of the bounding box, which a thin curved SVG stroke (a graph edge's hit
#: path) does not cover: the hit test lands on the layer beneath and the hover waits out its timeout.
#: Points along an SVG shape's geometry come first, then a 3 x 3 grid of the box; an element outside
#: the viewport is scrolled to its centre first, clear of a sticky header.
_HOVER_POINT_JS = """(el) => {
  const vw = window.innerWidth, vh = window.innerHeight;
  let r = el.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return null;
  if (r.top < 0 || r.left < 0 || r.bottom > vh || r.right > vw) {
    el.scrollIntoView({block: 'center', inline: 'center'});
    r = el.getBoundingClientRect();
  }
  const reaches = (x, y) => {
    if (!(x >= 0 && y >= 0 && x < vw && y < vh)) return false;
    const top = document.elementFromPoint(x, y);
    return !!top && (top === el || el.contains(top));
  };
  const points = [];
  if (typeof el.getTotalLength === 'function' && typeof el.getScreenCTM === 'function') {
    try {
      const length = el.getTotalLength(), m = el.getScreenCTM();
      if (length > 0 && m) {
        for (const f of [0.5, 0.25, 0.75, 0.1, 0.9, 0.4, 0.6, 0.05, 0.95]) {
          const p = el.getPointAtLength(length * f);
          points.push([m.a * p.x + m.c * p.y + m.e, m.b * p.x + m.d * p.y + m.f]);
        }
      }
    } catch (e) { /* not a rendered geometry: the box grid below */ }
  }
  for (const fy of [0.5, 0.25, 0.75]) {
    for (const fx of [0.5, 0.25, 0.75]) points.push([r.left + r.width * fx, r.top + r.height * fy]);
  }
  for (const [x, y] of points) if (reaches(x, y)) return {x: x, y: y};
  return null;
}"""


#: The native tooltip the pointer shows over the element: its own ``title`` or the nearest ancestor's
#: (an SVG ``<title>`` child counts), and whether the element's own text is cut. The browser draws that
#: tooltip outside the page, so no screenshot shows it: the hover answer says it instead.
_NATIVE_TOOLTIP_JS = """(el) => {
  let node = el, text = '';
  while (node && node.nodeType === 1 && !text) {
    const own = node.getAttribute && node.getAttribute('title');
    if (own && own.trim()) text = own.trim();
    else {
      const svgTitle = Array.from(node.children || []).find((c) => c.tagName && c.tagName.toLowerCase() === 'title');
      if (svgTitle && svgTitle.textContent.trim()) text = svgTitle.textContent.trim();
    }
    node = node.parentElement;
  }
  const cut = el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1;
  return {text: text, cut: !!cut && el.clientWidth > 0};
}"""

#: The longest native tooltip text the hover answer quotes.
MAX_TOOLTIP_CHARS = 300


async def _native_tooltip(page: Any, selector: str) -> str:
    """What the hover answer says of the native tooltip over *selector*; empty when there is none."""
    try:
        found = await page.locator(selector).first.evaluate(_NATIVE_TOOLTIP_JS, timeout=ACTION_TIMEOUT_MS)
    except Exception as exc:
        logger.debug("No native tooltip read for %s: %s", selector, format_exception_message(exc))
        return ""
    if not isinstance(found, dict):
        return ""
    text = found.get("text")
    if not isinstance(text, str) or not text:
        return ""
    if len(text) > MAX_TOOLTIP_CHARS:
        text = text[:MAX_TOOLTIP_CHARS] + "..."
    cut = " The element's own text is cut, so the tooltip carries the full text." if found.get("cut") else ""
    return (
        f" Native tooltip (title): {json.dumps(text, ensure_ascii=False)}; the browser draws it outside "
        f"the page, so no screenshot shows it.{cut}"
    )


async def _reachable_hover_point(page: Any, selector: str) -> tuple[float, float] | None:
    """A viewport point the first match of *selector* receives the pointer at; ``None`` when none is found."""
    try:
        point = await page.locator(selector).first.evaluate(_HOVER_POINT_JS, timeout=ACTION_TIMEOUT_MS)
    except Exception as exc:
        logger.debug("No reachable hover point for %s: %s", selector, format_exception_message(exc))
        return None
    if not isinstance(point, dict):
        return None
    x, y = point.get("x"), point.get("y")
    if not all(isinstance(v, int | float) and math.isfinite(v) for v in (x, y)):
        return None
    return float(x), float(y)


async def browser_hover(session_key: str, selector: str) -> str:
    """Move the pointer over the element matching *selector*: a tooltip, a hover state, a hover menu.

    The pointer goes to a point the element itself receives (see ``_HOVER_POINT_JS``), so the page's own
    hover handlers run on an SVG stroke whose box centre is covered; without such a point, Playwright's
    hover runs and its log says what intercepts the pointer.
    """
    session = await _get_or_create_session(session_key)
    async with session.lock:
        session.last_used = time.time()
        session.navigated = None
        await _reopen_resumed_page(session_key, session)
        point = await _reachable_hover_point(session.page, selector)
        if point is None:
            await session.page.hover(selector)
            where = ""
        else:
            await session.page.mouse.move(*point)
            where = f" at ({point[0]:.0f}, {point[1]:.0f})"
        tooltip = await _native_tooltip(session.page, selector)
        await _remember_state(session_key, session)  # as get_content
        return f"Hovering '{selector}'{where} on {session.page.url}.{tooltip}"


#: The farthest a drag moves on either axis, in CSS px, and the bounds of its intermediate pointer moves.
MAX_DRAG_DISTANCE = 4096
DRAG_STEPS_BOUNDS = (1, 100)
DEFAULT_DRAG_STEPS = 10


def drag_vector(dx: object, dy: object, steps: object) -> tuple[float, float, int]:
    """The move *dx*, *dy* in *steps* a drag asks for, or ``ValueError`` saying the bounds."""
    def distance(value: object) -> float:
        if value is None:
            return 0.0
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise ValueError("'dx' and 'dy' are numbers of CSS pixels.")
        if abs(value) > MAX_DRAG_DISTANCE:
            raise ValueError(f"'dx' and 'dy' are at most {MAX_DRAG_DISTANCE} px either way, got {value:g}.")
        return float(value)

    x, y = distance(dx), distance(dy)
    if x == 0 and y == 0:
        raise ValueError("'dx' or 'dy' must move the pointer.")
    if steps is None:
        count = DEFAULT_DRAG_STEPS
    elif isinstance(steps, bool) or not isinstance(steps, int | float) or not math.isfinite(steps) or steps % 1:
        raise ValueError("'steps' is a whole number.")
    else:
        count = int(steps)
    low, high = DRAG_STEPS_BOUNDS
    if not low <= count <= high:
        raise ValueError(f"'steps' is {low} to {high}, got {count}.")
    return x, y, count


async def _drag_start_point(page: Any, selector: str) -> tuple[float, float] | None:
    """Where a drag of *selector* presses: a point the element receives, else its box centre."""
    point = await _reachable_hover_point(page, selector)
    if point is not None:
        return point
    try:
        box = await page.locator(selector).first.bounding_box(timeout=ACTION_TIMEOUT_MS)
    except Exception as exc:
        logger.debug("No box to drag for %s: %s", selector, format_exception_message(exc))
        return None
    if not isinstance(box, dict):
        return None
    try:
        x = float(box["x"]) + float(box["width"]) / 2
        y = float(box["y"]) + float(box["height"]) / 2
    except (KeyError, TypeError, ValueError):
        return None
    return (x, y) if math.isfinite(x) and math.isfinite(y) else None


async def browser_drag(session_key: str, selector: str, dx: object, dy: object, steps: object = None) -> str:
    """Press the pointer on *selector*, move it by *dx*, *dy* CSS px in *steps* moves, release it.

    Real input events (Playwright ``mouse.down`` / ``move`` / ``up``), so a handle that calls
    ``setPointerCapture`` follows the drag: a ``PointerEvent`` dispatched from ``evaluate`` is untrusted
    and such a handle ignores it. ``ValueError`` on a move out of bounds, before the page is touched.
    """
    move_x, move_y, count = drag_vector(dx, dy, steps)
    session = await _get_or_create_session(session_key)
    async with session.lock:
        session.last_used = time.time()
        session.navigated = None
        await _reopen_resumed_page(session_key, session)
        start = await _drag_start_point(session.page, selector)
        if start is None:
            raise ValueError(f"no visible element matches '{selector}' to press on.")
        x, y = start
        end = (x + move_x, y + move_y)
        mouse = session.page.mouse
        await mouse.move(x, y)
        await mouse.down()
        try:
            await mouse.move(*end, steps=count)
        finally:
            # Never leave the button held: the next click would be read as the end of this drag.
            await mouse.up()
        await _remember_state(session_key, session)  # as get_content
        return (
            f"Dragged '{selector}' from ({x:.0f}, {y:.0f}) to ({end[0]:.0f}, {end[1]:.0f}) in {count} "
            f"move(s) on {session.page.url}."
        )


def viewport_scale(scale: object) -> float:
    """The device pixel ratio *scale* asks for (``None``: 1), or ``ValueError`` saying the bounds."""
    if scale is None:
        return 1
    low, high = VIEWPORT_SCALE_BOUNDS
    if isinstance(scale, bool) or not isinstance(scale, int | float) or not math.isfinite(scale):
        raise ValueError(f"'scale' is a number from {low} to {high}.")
    if not low <= scale <= high:
        raise ValueError(f"'scale' is {low} to {high}, got {scale:g}.")
    return float(scale)


def _kept_scale(value: object) -> float:
    """The pixel ratio a kept page names, 1 when it names none or one out of bounds."""
    try:
        return viewport_scale(value)
    except ValueError:
        return 1


def _scale_of(session: BrowserSession) -> float:
    """*session*'s pixel ratio; 1 for a session installed or stubbed without one."""
    return _kept_scale(getattr(session, "scale", 1))


def viewport_size(width: object, height: object) -> dict[str, int]:
    """The page size *width* x *height* asks for, or ``ValueError`` saying the bounds."""
    def whole(value: object) -> int:
        # A model may send 1440.0 for 1440; a bool, a fraction or a text is refused.
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value % 1:
            raise ValueError("'width' and 'height' are whole numbers of CSS pixels.")
        return int(value)

    w, h = whole(width), whole(height)
    (w_min, w_max), (h_min, h_max) = VIEWPORT_WIDTH_BOUNDS, VIEWPORT_HEIGHT_BOUNDS
    if not (w_min <= w <= w_max and h_min <= h <= h_max):
        raise ValueError(f"the viewport is {w_min} to {w_max} px wide and {h_min} to {h_max} px high, got {w} x {h}.")
    return {"width": w, "height": h}


#: The page's own size, read after two animation frames so the resize and the layout it triggers have run.
_PAGE_SIZE_JS = (
    "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame("
    "() => r([window.innerWidth, window.innerHeight]))))"
)
#: The page's own size, read at once: no animation frame needed, so a page that paints late still answers (#10037).
_PAGE_SIZE_NOW_JS = "() => [window.innerWidth, window.innerHeight]"
#: The longest the ``viewport`` action waits for the page to report the size it set (#9967, #10037).
_VIEWPORT_SETTLE_SECONDS = 10.0
#: The pause between two reads of the page's size while it still reports the old one.
_VIEWPORT_SETTLE_STEP_SECONDS = 0.1
#: The longest one size read may take: a read stuck on a busy page never holds the whole wait (#10037).
_VIEWPORT_READ_SECONDS = 0.5
#: The longest the two paint frames are waited for once the page reports the size, best effort.
_VIEWPORT_PAINT_SECONDS = 0.5
#: The reads in a row that must report the new size before the ``viewport`` action answers: a single read matched
#: while the page was still applying the size and a read a second later found the old width again (#10308).
_VIEWPORT_STABLE_READS = 3


async def _await_viewport_applied(page: Any, size: dict[str, int]) -> tuple[bool, tuple[int, int] | None]:
    """Wait, bounded, until the page's ``innerWidth`` x ``innerHeight`` equal *size*.

    ``set_viewport_size`` returns once the browser accepted the size, before the page has run its resize: an
    ``evaluate`` or a click in the next call read the old width for about 1.5 s on a busy single-page application
    (#9967). The size is read at once (:data:`_PAGE_SIZE_NOW_JS`, no animation frame: a page busy re-rendering after
    the resize may run none for seconds, #10037), each read bounded by :data:`_VIEWPORT_READ_SECONDS`, again every
    :data:`_VIEWPORT_SETTLE_STEP_SECONDS` up to :data:`_VIEWPORT_SETTLE_SECONDS` on the clock of
    :func:`_capture_reopened_page`. It matches once :data:`_VIEWPORT_STABLE_READS` reads in a row report it: one
    matching read is no proof, the page read the old width again a second later (#10308). Once it matches, two paint
    frames are waited for, best effort and bounded. Answers whether it matched and the last size read (``None`` when
    no read answered); a deadline reached on a matching read that is not yet stable counts as matched.
    """
    wanted = (size["width"], size["height"])
    deadline = _paint_clock() + _VIEWPORT_SETTLE_SECONDS
    seen: tuple[int, int] | None = None
    in_a_row = 0
    while True:
        left = deadline - _paint_clock()
        read_now: tuple[int, int] | None = None
        with contextlib.suppress(Exception):  # a document replaced under the read, a page too busy to answer
            budget = min(_VIEWPORT_READ_SECONDS, max(left, _VIEWPORT_SETTLE_STEP_SECONDS))
            read = await asyncio.wait_for(page.evaluate(_PAGE_SIZE_NOW_JS), budget)
            if isinstance(read, list | tuple) and len(read) == 2:
                read_now = (int(read[0]), int(read[1]))
        if read_now is not None:
            seen = read_now
        in_a_row = in_a_row + 1 if read_now == wanted else 0
        left = deadline - _paint_clock()
        if in_a_row >= _VIEWPORT_STABLE_READS or (in_a_row and left <= 0):
            with contextlib.suppress(Exception):  # the layout already uses the size; the frames are a courtesy
                await asyncio.wait_for(page.evaluate(_PAGE_SIZE_JS), _VIEWPORT_PAINT_SECONDS)
            return True, seen
        if left <= 0:
            return False, seen
        await _paint_sleep(min(_VIEWPORT_SETTLE_STEP_SECONDS, left))


async def _page_holds_viewport(session: BrowserSession) -> str | None:
    """Make sure the page still reports the size the ``viewport`` action set before a script reads it.

    The ``viewport`` action answered on the new size, yet the next ``evaluate`` read the old ``innerWidth`` once in a
    while (#10308). The page's size is read at once, bounded; when it differs from :attr:`BrowserSession.viewport`
    the size is applied again and waited for (:func:`_await_viewport_applied`). Answers ``None`` when the page holds
    the size (or no size was set), else a note the result carries: the script then ran on another size.
    """
    size = getattr(session, "viewport", None)
    if not size:
        return None
    wanted = (size["width"], size["height"])
    with contextlib.suppress(Exception):
        read = await asyncio.wait_for(session.page.evaluate(_PAGE_SIZE_NOW_JS), _VIEWPORT_READ_SECONDS)
        if isinstance(read, list | tuple) and len(read) == 2 and (int(read[0]), int(read[1])) == wanted:
            return None
    with contextlib.suppress(Exception):  # the browser accepted it once; a refusal leaves the wait to say so
        await session.page.set_viewport_size(dict(size))
    matched, seen = await _await_viewport_applied(session.page, size)
    if matched:
        return None
    reported = f"{seen[0]} x {seen[1]} px" if seen is not None else "no size (busy or reloading)"
    return (
        f"Note: the viewport is {wanted[0]} x {wanted[1]} px but the page reported {reported} "
        f"(window.innerWidth x innerHeight) after {_VIEWPORT_SETTLE_SECONDS:g} s; this result was read on it."
    )


async def _rescale_session(
    session_key: str, session: BrowserSession, size: dict[str, int], scale: float
) -> BrowserSession:
    """Re-create *session* at pixel ratio *scale* and size *size*: ``device_scale_factor`` is a context option.

    Its cookies, storage and page are kept (:func:`_close_session` without forgetting) and the new context opens
    with them, back on the page it was on (:attr:`BrowserSession.resume_url`); the restore notice is not said, the
    action's own answer says what changed. A profile session is refused: its sign-in lives with its policy.
    """
    if not _keeps_state(session):
        raise ValueError("'scale' cannot change on a session signed in with a browser profile; close it first.")
    _next_launch[session_key] = {
        "viewport": dict(size),
        "scale": scale,
        **({"media": dict(session.media)} if session.media else {}),
        **({"offline": True} if session.offline else {}),
    }
    url = str(getattr(session.page, "url", "") or "")
    async with _session_lock(session_key):
        await _close_session(session_key)
        fresh = await _launch_session(session_key, reopen_kept_page=True)
    _notices.pop(session_key, None)
    if fresh.resume_url is None and _origin_of(url):
        fresh.resume_url = url  # nothing kept (a page with no storage): it is still opened again
    return fresh


async def browser_set_viewport(session_key: str, width: object, height: object, scale: object = None) -> str:
    """Resize the session's page; kept until the session closes, a new session opens at 1280 x 720.

    *scale* sets the device pixel ratio (1 to 3) screenshots are taken at: a context option, so a change re-creates
    the session on its page, signed in as it was (:func:`_rescale_session`); ``None`` keeps the current one.
    Returns once the page itself reports the new size (bounded, :func:`_await_viewport_applied`), so a capture, a
    click or an ``evaluate`` in the next call runs on the resized layout.
    """
    size = viewport_size(width, height)
    ratio = None if scale is None else viewport_scale(scale)
    session = await _get_or_create_session(session_key)
    if ratio is not None and ratio != _scale_of(session):
        async with session.lock:
            session = await _rescale_session(session_key, session, size, ratio)
    async with session.lock:
        session.last_used = time.time()
        await _reopen_resumed_page(session_key, session)
        await session.page.set_viewport_size(size)
        session.viewport = dict(size)
        matched, seen = await _await_viewport_applied(session.page, size)
        if await _remember_state(session_key, session) is None:
            await _share_kept_viewport(session_key, session)
        if not matched:
            logger.warning("Browser session %s: the page reported %s after the viewport %s", session_key, seen, size)
            reported = (
                f"the page still reports {seen[0]} x {seen[1]} px (window.innerWidth x innerHeight)"
                if seen is not None
                else "the page answered no read of window.innerWidth x innerHeight (busy or reloading)"
            )
            return (
                f"Viewport set to {size['width']} x {size['height']} px on {session.page.url}, but after "
                f"{_VIEWPORT_SETTLE_SECONDS:g} s {reported}: read it again before a capture or a measure."
            )
        ratio_note = f" at pixel ratio {_scale_of(session):g}" if _scale_of(session) != 1 else ""
        return (
            f"Viewport set to {size['width']} x {size['height']} px{ratio_note} on {session.page.url}: the page reports "
            f"{seen[0]} x {seen[1]} px (window.innerWidth x innerHeight), screenshots, evaluate and the page's "
            "layout now use it."
        )


def media_features(reduced_motion: object = None, color_scheme: object = None) -> dict[str, str]:
    """The media features the ``emulate_media`` action asks for, by Playwright argument name, or ``ValueError``.

    A feature left ``None`` is not changed; at least one is required."""
    asked = {"reduced_motion": reduced_motion, "color_scheme": color_scheme}
    features: dict[str, str] = {}
    for name, value in asked.items():
        if value is None:
            continue
        allowed = MEDIA_FEATURES[name]
        if not isinstance(value, str) or value not in allowed:
            raise ValueError(f"'{name}' is one of {', '.join(allowed)}, got {value!r}.")
        features[name] = value
    if not features:
        raise ValueError("give 'reduced_motion' or 'color_scheme', or 'reset' true to stop emulating.")
    return features


def _kept_media(value: object) -> dict[str, str]:
    """The valid media features of a kept page's *value*; anything else is dropped (an older or altered copy)."""
    if not isinstance(value, dict):
        return {}
    return {
        name: value[name]
        for name, allowed in MEDIA_FEATURES.items()
        if isinstance(value.get(name), str) and value[name] in allowed
    }


def _media_text(media: dict[str, str]) -> str:
    """The emulated features as the CSS media queries they answer, e.g. ``prefers-reduced-motion: reduce``."""
    css = {"reduced_motion": "prefers-reduced-motion", "color_scheme": "prefers-color-scheme"}
    return ", ".join(f"{css[name]}: {value}" for name, value in media.items())


async def browser_emulate_media(
    session_key: str, reduced_motion: object = None, color_scheme: object = None, *, reset: bool = False
) -> str:
    """Emulate CSS media features on the session's page (``page.emulate_media``), kept until reset or close.

    A feature not given keeps what was emulated before; *reset* stops every emulation (the browser's defaults).
    Kept with the session's state like the viewport, so a navigate and a session re-created after a resume keep it.
    """
    features = {} if reset else media_features(reduced_motion, color_scheme)
    session = await _get_or_create_session(session_key)
    async with session.lock:
        session.last_used = time.time()
        await _reopen_resumed_page(session_key, session)
        if reset:
            # Playwright's "null" stops the emulation of a feature: the page answers the browser's default again.
            await session.page.emulate_media(reduced_motion="null", color_scheme="null")
            session.media = None
        else:
            await session.page.emulate_media(**features)
            session.media = {**(session.media or {}), **features}
        if await _remember_state(session_key, session) is None:
            await _share_kept_media(session_key, session)
        if not session.media:
            return f"Media emulation reset on {session.page.url}: the page answers the browser's defaults."
        return (
            f"Emulating {_media_text(session.media)} on {session.page.url}, kept until reset or close: matchMedia, "
            "CSS media queries (motion-safe, dark) and screenshots now use it."
        )


async def browser_network(session_key: str, offline: object) -> str:
    """Cut the session's pages from the network or give it back (``BrowserContext.set_offline``), kept until changed.

    Offline, ``navigator.onLine`` is false, the page gets its ``offline`` event and every new request fails with
    ``net::ERR_INTERNET_DISCONNECTED``, one sent on a reused keep-alive connection included (:func:`_fail_offline`);
    a connection already open (an events stream) is not cut by Chromium's
    emulation. Back online the page gets its ``online`` event. Kept with the session's state like the viewport, so
    a session re-created after a resume opens offline too.
    """
    if not isinstance(offline, bool):
        raise ValueError(f"'offline' is true or false, got {offline!r}.")
    session = await _get_or_create_session(session_key)
    async with session.lock:
        session.last_used = time.time()
        await _reopen_resumed_page(session_key, session)
        await session.context.set_offline(offline)
        if offline and not session.offline:
            await session.context.route("**/*", _fail_offline)
        elif not offline:
            await session.context.unroute("**/*", _fail_offline)
        session.offline = offline
        if await _remember_state(session_key, session) is None:
            await _share_kept_offline(session_key, session)
        if offline:
            return (
                f"Network offline on {session.page.url}, kept until offline=false or close: navigator.onLine is "
                "false, the 'offline' event fired, every new request fails with net::ERR_INTERNET_DISCONNECTED. A "
                "connection already open (an events stream) is not cut by the emulation: the page's own 'offline' "
                "handling decides."
            )
        return f"Network back online on {session.page.url}: the page got its 'online' event, requests go out again."


async def browser_console(session_key: str, level: str | None = None) -> str:
    """What the page logged since the last ``navigate`` action (*level*: ``error`` or ``warning`` and above)."""
    if level is not None and level not in CONSOLE_LEVELS:
        raise ValueError(f"level must be one of {', '.join(CONSOLE_LEVELS)}, not {level!r}")
    session = await _get_or_create_session(session_key)
    async with session.lock:
        session.last_used = time.time()
        await _reopen_resumed_page(session_key, session)
        await _remember_state(session_key, session)  # as get_content
        return session.console.describe(session.page.url, level)


async def browser_screenshot(session_key: str, full_page: bool = False, selector: str | None = None) -> bytes:
    """Take a screenshot of the current page and return the raw PNG bytes.

    The caller (``build_browser_tool``) persists these bytes as a
    ``FileAttachment`` so the screenshot becomes a durable conversation /
    assignment-run file and renders inline. Previously this returned a
    truncated 100-char base64 preview string, so the image was discarded and
    never reached the LLM or the user — the screenshot action was effectively
    a no-op (see #SCREENSHOT-FIX).
    """
    if selector and full_page:
        raise ValueError("a screenshot takes either 'selector' (one element) or 'full_page', not both.")
    session = await _get_or_create_session(session_key)
    async with session.lock:
        session.last_used = time.time()
        reopened = await _reopen_resumed_page(session_key, session)

        async def capture() -> bytes:
            if selector:
                return await _element_screenshot(session, selector)
            if full_page:
                return await _full_page_screenshot(session)
            return await session.page.screenshot(full_page=False)

        # A page a resume has just opened again is given time to paint; any other capture is taken at once.
        image = await (_capture_reopened_page(session_key, session, capture) if reopened else capture())
        # A screenshot is often the action after a sign-in click: the token written since is kept.
        await _remember_state(session_key, session)
        return image


async def _element_screenshot(session: BrowserSession, selector: str) -> bytes:
    """The capture of the first element *selector* matches, its whole box painted.

    Chromium captures an element past the viewport, but the page paints only what intersects the viewport: an SPA
    container taller than it came back as background where its cards are drawn on entering the view. Such an element
    is captured with the viewport grown to its height (bounded by :data:`MAX_ELEMENT_CAPTURE_HEIGHT`), given two
    animation frames to paint, and the viewport is restored whatever happens. An element that fits is captured as is.
    """
    page = session.page
    element = page.locator(selector).first
    viewport = getattr(page, "viewport_size", None) or session.viewport or DEFAULT_VIEWPORT
    # Waits for the element as the capture did (a timeout raises the same way); ``None``: not visible.
    box = await element.bounding_box()
    if not box or box.get("height", 0) <= viewport["height"]:
        # The first match, scrolled into view: the capture of that element's box only.
        return await element.screenshot()
    tall = {"width": viewport["width"], "height": min(math.ceil(box["height"]), MAX_ELEMENT_CAPTURE_HEIGHT)}
    await page.set_viewport_size(tall)
    try:
        with contextlib.suppress(Exception):  # a busy page: Playwright's own stability wait still runs
            await asyncio.wait_for(
                page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"),
                _ELEMENT_PAINT_SECONDS,
            )
        return await element.screenshot()
    finally:
        await page.set_viewport_size({"width": viewport["width"], "height": viewport["height"]})


async def _full_page_screenshot(session: BrowserSession) -> bytes:
    """The whole page, its fixed shell painted once at the top and every scroll container's content shown.

    Chromium stitches a ``full_page`` capture past the viewport without resizing it, so ``position: fixed`` and
    ``sticky`` elements (an app's sidebar and top bar) are painted where the viewport sits, over the content, and an
    app that scrolls inside its own container comes back one viewport tall. The viewport is grown to the height
    :data:`_FULL_PAGE_HEIGHT_JS` measures (bounded by :data:`MAX_ELEMENT_CAPTURE_HEIGHT`), given two animation
    frames to paint, captured, and restored whatever happens. A page that fits, or answers no measure, is captured
    as before.
    """
    page = session.page
    viewport = getattr(page, "viewport_size", None) or session.viewport or DEFAULT_VIEWPORT
    need: object = None
    with contextlib.suppress(Exception):  # a busy or reloading page: the plain capture below
        need = await asyncio.wait_for(page.evaluate(_FULL_PAGE_HEIGHT_JS), _ELEMENT_PAINT_SECONDS)
    if not isinstance(need, int | float) or isinstance(need, bool) or need <= viewport["height"]:
        return await page.screenshot(full_page=True)
    tall = {"width": viewport["width"], "height": min(math.ceil(need), MAX_ELEMENT_CAPTURE_HEIGHT)}
    await page.set_viewport_size(tall)
    try:
        with contextlib.suppress(Exception):  # a busy page: Playwright's own stability wait still runs
            await asyncio.wait_for(
                page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"),
                _ELEMENT_PAINT_SECONDS,
            )
        # ``full_page`` still covers a document the grown viewport does not hold whole (past the bound).
        return await page.screenshot(full_page=True)
    finally:
        await page.set_viewport_size({"width": viewport["width"], "height": viewport["height"]})


def record_seconds(seconds: object) -> float:
    """The duration *seconds* asks the ``record`` action for (``None``: the default), or ``ValueError``."""
    if seconds is None:
        return DEFAULT_RECORD_SECONDS
    low, high = RECORD_SECONDS_BOUNDS
    if isinstance(seconds, bool) or not isinstance(seconds, int | float) or not math.isfinite(seconds):
        raise ValueError(f"'seconds' is a number of seconds, {low:g} to {high:g}.")
    if not low <= seconds <= high:
        raise ValueError(f"a recording lasts {low:g} to {high:g} seconds, got {seconds:g}.")
    return float(seconds)


def record_frame_size(viewport: dict[str, Any]) -> tuple[int, int]:
    """The size a recording captures a *viewport* at: the viewport itself, scaled down to :data:`RECORD_MAX_WIDTH`."""
    width, height = int(viewport["width"]), int(viewport["height"])
    if width <= RECORD_MAX_WIDTH:
        return width, height
    return RECORD_MAX_WIDTH, max(1, round(height * RECORD_MAX_WIDTH / width))


@dataclass(frozen=True)
class RecordedVideo:
    """What the ``record`` action captured: the MP4 and what the page painted meanwhile."""

    mp4: bytes
    seconds: float
    #: The frames Chromium painted during the recording (a still page paints few: each one lasts until the next).
    frames: int
    #: The rate the page painted at, from the first frame to the last; 0.0 under two frames.
    painted_fps: float
    width: int
    height: int
    url: str
    #: Where the browser drew while it recorded (``cpu_lanes.Lane.words``); blank where it was not said.
    lane: str = ""


async def _lift_browser(browser: Any) -> tuple[Callable[[], None], str]:
    """Put *browser*'s processes (the browser, its renderers, its GPU process) in the worker's render lane while it
    records (``video_studio.cpu_lanes.lift``): a screencast's readback runs on the CPUs the page draws on, and the
    workers' jobs beside it would take them. Answers what puts them back and what the lane is; a browser whose
    processes cannot be read (another host, an older Chromium) is left where it is."""
    from app.services.video_studio import cpu_lanes

    def nothing() -> None:
        return None

    try:
        root = await browser.new_browser_cdp_session()
        try:
            info = await root.send("SystemInfo.getProcessInfo")
        finally:
            with contextlib.suppress(Exception):
                await root.detach()
        pids = sorted({int(entry["id"]) for entry in (info or {}).get("processInfo") or [] if entry.get("id")})
    except Exception as exc:  # the recording goes on where the browser does not say
        logger.info("Browser processes not read for a recording: %s", format_exception_message(exc))
        return nothing, ""
    if not pids:
        return nothing, ""
    found = cpu_lanes.lane()
    if found.kind == cpu_lanes.NONE:
        return nothing, found.words()
    return cpu_lanes.lift(pids), found.words()


class _RecordedFrames:
    """The screencast frames of one recording, each acknowledged at once and written to *workdir* off the loop."""

    def __init__(self, cdp: Any, workdir: str, max_frames: int) -> None:
        self._cdp = cdp
        self._dir = workdir
        self._max = max_frames
        self._loop = asyncio.get_running_loop()
        self._writes: set[asyncio.Future[None]] = set()
        self.frames: list[tuple[float, str]] = []
        self.started = 0.0

    def on_frame(self, event: dict[str, Any]) -> None:
        self._loop.create_task(self._ack(event.get("sessionId")))
        if len(self.frames) >= self._max:
            return
        metadata = event.get("metadata") or {}
        stamp = float(metadata.get("timestamp") or time.time())
        name = f"f{len(self.frames):06d}.jpg"
        self.frames.append((max(0.0, stamp - self.started), name))
        future = self._loop.run_in_executor(None, self._write, name, event.get("data") or "")
        self._writes.add(future)
        future.add_done_callback(self._writes.discard)

    def _write(self, name: str, data: str) -> None:
        import base64
        import os

        with open(os.path.join(self._dir, name), "wb") as handle:
            handle.write(base64.b64decode(data))

    async def _ack(self, session_id: Any) -> None:
        with contextlib.suppress(Exception):  # the page may be navigating
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})

    async def written(self) -> None:
        if self._writes:
            await asyncio.gather(*list(self._writes), return_exceptions=True)
        self.frames.sort()


async def browser_record(session_key: str, seconds: object = None) -> RecordedVideo:
    """Record the session's viewport for *seconds* and answer it as an H.264 MP4 at :data:`RECORD_FPS`.

    Chromium's DevTools screencast (``Page.startScreencast``) hands over each frame the compositor paints, already
    JPEG-encoded by the browser process: the page's main thread does no capture work, so its animations run at
    their own rate while it records (a ``screenshot`` mid-animation stalls them). The frames go to disk off the
    event loop and the platform's ffmpeg (``video_studio.encoder``) encodes them, each frame held until the next.
    ``ValueError`` for a duration out of bounds; ``RuntimeError`` when the page painted nothing or ffmpeg is missing.
    """
    import tempfile

    from app.services.video_studio import encoder

    duration = record_seconds(seconds)
    if not encoder.encoder_available():
        raise RuntimeError("recording needs ffmpeg, which is not installed where this browser runs.")
    session = await _get_or_create_session(session_key)
    async with session.lock:
        session.last_used = time.time()
        await _reopen_resumed_page(session_key, session)
        page = session.page
        viewport = getattr(page, "viewport_size", None) or session.viewport or DEFAULT_VIEWPORT
        width, height = record_frame_size(viewport)
        # A DevTools session of its own: the session's one stops runaway scripts and keeps no frame listener.
        cdp = await session.context.new_cdp_session(page)
        with tempfile.TemporaryDirectory(prefix="browser-record-") as workdir:
            frames = _RecordedFrames(cdp, workdir, max_frames=math.ceil(duration * _RECORD_MAX_FRAMES_PER_SECOND))
            cdp.on("Page.screencastFrame", frames.on_frame)
            frames.started = time.time()
            restore, lane = await _lift_browser(session.browser)
            try:
                await cdp.send(
                    "Page.startScreencast",
                    {
                        "format": "jpeg",
                        "quality": _RECORD_JPEG_QUALITY,
                        "maxWidth": width,
                        "maxHeight": height,
                        "everyNthFrame": _RECORD_EVERY_NTH_FRAME,
                    },
                )
                await asyncio.sleep(duration)
            finally:
                with contextlib.suppress(Exception):  # the page may already be gone
                    await cdp.send("Page.stopScreencast")
                with contextlib.suppress(Exception):
                    await cdp.detach()
                restore()
            await frames.written()
            if not frames.frames:
                raise RuntimeError(f"the page painted no frame in {duration:g} s of recording ({page.url}).")
            out = f"{workdir}/recording.mp4"
            await encoder.frames_to_mezzanine(
                frames.frames,
                end_time=duration,
                out_path=out,
                fps=RECORD_FPS,
                crf=23,
                preset="veryfast",
                workdir=workdir,
            )
            with open(out, "rb") as handle:
                mp4 = handle.read()
        span = frames.frames[-1][0] - frames.frames[0][0]
        painted = (len(frames.frames) - 1) / span if len(frames.frames) > 1 and span > 0 else 0.0
        return RecordedVideo(
            mp4=mp4,
            seconds=duration,
            frames=len(frames.frames),
            painted_fps=painted,
            width=width,
            height=height,
            url=page.url,
            lane=lane,
        )


def format_evaluate_result(result: object) -> str:
    """The text of an ``evaluate`` result, bounded.

    Cut at :data:`MAX_EVALUATE_RESULT_CHARS` with a note that says how much
    was dropped. A result that is (or carries) a base64 image data URL is
    named for what it is, and the note points at the tools that hand an
    image over as a FILE - ``fetch_image_asset`` for a picture on the web,
    ``screenshot`` for what the page shows -     because a blob of base64 is
    context the model cannot read and the loop then has to prune.
    """
    text = str(result)
    if len(text) <= MAX_EVALUATE_RESULT_CHARS:
        return f"JavaScript result:\n{text}"
    dropped = len(text) - MAX_EVALUATE_RESULT_CHARS
    is_image_blob = re.search(_DATA_URL_PREFIX_RE_SOURCE, text[: min(len(text), 4096)]) is not None
    reason = (
        "The result is a base64 image data URL: that is not readable text. To use "
        "a picture, fetch it as a file with fetch_image_asset(url=...) or capture "
        "the page with the screenshot action - both hand you a file_id."
        if is_image_blob
        else "Return less from the script (select the elements or fields you need)."
    )
    return (
        f"JavaScript result:\n{text[:MAX_EVALUATE_RESULT_CHARS]}\n\n"
        f"[Result truncated: {dropped:,} more characters were dropped (limit "
        f"{MAX_EVALUATE_RESULT_CHARS:,}). {reason}]"
    )


async def _interrupted_script_message(session: BrowserSession, mark: int) -> str:
    """What an agent is told when the page navigated before its script returned."""
    loaded = await _await_new_document(session, mark)
    url = session.page.url
    refusal = await _url_policy_refusal(url, session)
    if refusal is not None:
        return (
            f"the page navigated before the script returned, to {url}, which the browser's URL policy "
            f"does not allow ({refusal}). The script was not run again."
        )
    where = f"now at {url}" if loaded else f"at {url}, not loaded after {ACTION_TIMEOUT_MS / 1000:g} s"
    return (
        f"the page navigated before the script returned ({where}), so its result was lost. The script "
        "was not run again: it may have acted before the page changed (a click, a submit, a request). "
        "Run it again only if it just reads the page."
    )


def evaluate_timeout(asked: object) -> float:
    """The seconds an ``evaluate`` script may run: *asked* within 1 and :data:`MAX_EVALUATE_TIMEOUT_SECONDS`, else
    :data:`EVALUATE_TIMEOUT_SECONDS`."""
    try:
        seconds = float(asked) if asked is not None else EVALUATE_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        seconds = EVALUATE_TIMEOUT_SECONDS
    return min(MAX_EVALUATE_TIMEOUT_SECONDS, max(1.0, seconds))


def _dropped(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()


async def _open_cdp(session: BrowserSession) -> None:
    """Open *session*'s DevTools session (:attr:`BrowserSession.cdp`); best effort, a script then runs unstoppable."""
    if session.cdp is not None:
        return
    try:
        session.cdp = await asyncio.wait_for(session.context.new_cdp_session(session.page), _STATE_READ_SECONDS)
    except Exception as exc:
        logger.debug("Browser session's DevTools session not opened: %s", format_exception_message(exc))


async def _stop_script(session: BrowserSession, waited: float) -> str:
    """Stop the script the page still runs after *waited* seconds (Chromium's ``Runtime.terminateExecution``, which
    interrupts a busy page) and say where the page stands."""
    stopped = False
    try:
        if session.cdp is not None:
            await asyncio.wait_for(session.cdp.send("Runtime.terminateExecution"), _STATE_READ_SECONDS)
            stopped = True
    except Exception as exc:
        logger.debug("A timed-out script was not stopped: %s", format_exception_message(exc))
    state = (
        "it was stopped and the page answers again: return less, or a promise that resolves sooner"
        if stopped
        else "it could not be stopped and may still keep the page busy: navigate to load it again"
    )
    return (
        f"the script did not return within {waited:g} s (page at {session.page.url}); {state}. Give evaluate a "
        f"longer timeout (up to {MAX_EVALUATE_TIMEOUT_SECONDS:g} s) for a script that needs it."
    )


#: How long the first ``evaluate`` after a navigate waits for a page still on the document the navigate left
#: (:func:`_check_navigated_document`), and the pause between two reads of ``location.href``.
_NAVIGATED_DOCUMENT_SECONDS = 5.0
_NAVIGATED_DOCUMENT_STEP_SECONDS = 0.25
#: The clock and the pause of that wait: module seams so a test measures the bound in fake time.
_navigated_clock: Callable[[], float] = time.monotonic
_navigated_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


async def _check_navigated_document(session: BrowserSession) -> None:
    """Make the first ``evaluate`` after a navigate run on the document the navigate answered, or refuse it (#10013).

    A navigate answered "Navigated to: B" and the script run just after it read the page it had left: its relative
    fetches went to the previous origin, its DOM reads were the previous route's. When ``location.href`` still names
    the page the navigate left (and not the one it answered), the page is given
    :data:`_NAVIGATED_DOCUMENT_SECONDS` to get there; still on it, the script is not run and
    :class:`StillOnPreviousPageError` names both pages. A page somewhere else again (an application that routed on
    after the navigate) or one that cannot be read runs the script, as before. A page on the answered path whose only
    difference is a query or a fragment it added itself (a default filter written with ``history.replaceState``) is
    the answered document and runs the script, even when that address equals the page the navigate left. A page at
    the address it left whose document was created after the navigate started (``performance.timeOrigin``) is the
    navigate's own document that the application routed back there (a sign-in URL landing on ``/``, #10149) and runs
    the script too. A page on another origin than the one the navigate answered is never that document, whatever
    page the navigate left (#10491): a session re-created after a resume left ``about:blank``, and a page back on the
    origin it was on before the resume ran the script there. Once per navigate.
    """
    navigated, session.navigated = session.navigated, None
    started, session.navigated_at = session.navigated_at, None
    if navigated is None:
        return
    left, answered = navigated
    if _same_location(left, answered):
        return
    deadline = _navigated_clock() + _NAVIGATED_DOCUMENT_SECONDS
    while True:
        href = await _page_href(session)
        if not href or _same_path(href, answered):
            return
        other_origin = _origin_of(href) != _origin_of(answered)
        if not other_origin and not _same_location(href, left):
            return
        if not other_origin and await _document_created_since(session, started):
            return
        if _navigated_clock() >= deadline:
            break
        await _navigated_sleep(_NAVIGATED_DOCUMENT_STEP_SECONDS)
    logger.warning("Browser evaluate: the page is still on %s after a navigate answered %s, script not run",
                   href, answered)
    raise StillOnPreviousPageError(
        f"the page is still on {href}, not on {answered} that the navigate answered, after"
        f" {_NAVIGATED_DOCUMENT_SECONDS:g} s: the script was not run. Navigate to {answered} again, then evaluate."
    )


async def _document_created_since(session: BrowserSession, started: float | None) -> bool:
    """Whether the page's document was created at or after *started* (epoch ms): ``False`` when either is unknown."""
    if started is None:
        return False
    try:
        origin = await asyncio.wait_for(session.page.evaluate(_DOCUMENT_ORIGIN_JS), _STATE_READ_SECONDS)
    except Exception as exc:
        logger.debug("Browser evaluate: performance.timeOrigin not read (%s)", format_exception_message(exc))
        return False
    return isinstance(origin, int | float) and not isinstance(origin, bool) and origin >= started


async def browser_evaluate(session_key: str, expression: str, timeout: float | None = None) -> str:
    """Evaluate a JavaScript expression and return the result.

    The agent's script runs once. Playwright's navigation race cannot tell a
    script the page navigated under from one that navigated itself (a click, a
    submit, ``location.href = ...``) or had already acted, so it is never run
    again: the error says so and where the page landed, once it loaded. It
    runs for *timeout* seconds at most (:func:`evaluate_timeout`): past
    it the script is stopped and :class:`EvaluateTimeoutError` says where the
    page stands, the session kept as it was.
    """
    bound = evaluate_timeout(timeout)
    session = await _get_or_create_session(session_key)
    async with session.lock:
        session.last_used = time.time()
        reopened = await _reopen_resumed_page(session_key, session)
        if reopened:
            await _await_reopened_render(session_key, session)
        await _check_navigated_document(session)
        size_note = await _page_holds_viewport(session)
        mark = session.documents.committed
        running = asyncio.ensure_future(session.page.evaluate(expression))
        try:
            done, _pending = await asyncio.wait({running}, timeout=bound)
            if not done:
                # Left to end with the script it stopped: its error is the stop's, read and dropped.
                running.add_done_callback(_dropped)
                raise EvaluateTimeoutError(await _stop_script(session, bound))
            result = running.result()
        except EvaluateTimeoutError:
            raise
        except Exception as exc:
            if not _is_navigation_race(exc):
                if reopened:
                    raise ReopenedPageScriptError(f"{format_exception_message(exc)}. {_REOPENED_SCRIPT_NOTE}") from exc
                raise
            message = await _interrupted_script_message(session, mark)
            # The script navigated (a sign-in submitted, location set): what it wrote before is kept.
            await _remember_state(session_key, session)
            raise PageNavigationError(message) from exc
        finally:
            session.last_used = time.time()
        # A script may sign in (a token written to localStorage, a cookie set): kept as a navigation's is,
        # so a run resumed in another process comes back signed in.
        await _remember_state(session_key, session)
        answer = format_evaluate_result(result)
        return f"{size_note}\n{answer}" if size_note else answer


#: The largest response the ``download`` action keeps (it crosses the DevTools channel as base64).
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024

#: Seconds a ``download`` waits for the response.
DOWNLOAD_TIMEOUT_SECONDS = 60.0

#: The methods a ``download`` may send: a GET, or a POST for a file an application serves from a POST route only
#: (an export that takes its options as a JSON body).
DOWNLOAD_METHODS = ("GET", "POST")

#: The page-side fetch of a ``download``: the page's own origin, cookies and route filter (the preview key is added
#: there), the headers *headers* evaluates to (``{}`` when none), the method and the request body (a JSON text,
#: ``Content-Type: application/json`` unless the headers name one), the response read whole and handed back as
#: base64. ``__HEADERS__`` is replaced by the agent's expression, run in the page like an ``evaluate`` script; the
#: method and the body travel as arguments, never spliced into the script.
_DOWNLOAD_JS = """
async ([url, limit, method, body]) => {
  const headers = Object.assign({}, (await (async () => (__HEADERS__))()) || {});
  const init = {credentials: 'include', headers, method: method || 'GET'};
  if (body !== null && body !== undefined) {
    if (!Object.keys(headers).some((name) => name.toLowerCase() === 'content-type')) {
      headers['Content-Type'] = 'application/json';
    }
    init.body = body;
  }
  const response = await fetch(url, init);
  const type = response.headers.get('content-type') || '';
  const declared = Number(response.headers.get('content-length') || 0);
  if (!response.ok) return {status: response.status, type, size: declared, data: null};
  if (declared > limit) return {status: response.status, type, size: declared, data: null, tooLarge: true};
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.length > limit) return {status: response.status, type, size: bytes.length, data: null, tooLarge: true};
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return {status: response.status, type, size: bytes.length, data: btoa(binary)};
}
"""


@dataclass
class DownloadedFile:
    """A response the page fetched for the ``download`` action."""

    url: str
    content_type: str
    body: bytes


def download_script(headers_expression: str | None) -> str:
    """The page script of a ``download``, with *headers_expression* (a JavaScript expression evaluating to a headers
    object, awaited) or ``{}``."""
    expression = (headers_expression or "").strip() or "{}"
    return _DOWNLOAD_JS.replace("__HEADERS__", expression)


async def browser_download(
    session_key: str,
    url: str,
    headers_expression: str | None = None,
    *,
    method: str = "GET",
    body: str | None = None,
) -> DownloadedFile:
    """Fetch *url* from the current page and return the response body.

    The request is the page's own ``fetch``: the session's cookies, the route filter (a sandbox service preview gets
    its key there, the page never holds it) and the headers *headers_expression* evaluates to in the page (a bearer
    token read from ``localStorage``) go with it, and the bytes come back to the platform, never to the model. A
    POST (*method*, one of :data:`DOWNLOAD_METHODS`) sends *body*, a JSON text. *url* is resolved against the page
    and held to the session's URL policy first. Raises :class:`ValueError` on an unknown method, a body without a
    POST, a refused URL, an HTTP error, an empty or oversized response; :class:`EvaluateTimeoutError` past
    :data:`DOWNLOAD_TIMEOUT_SECONDS`.
    """
    import base64
    from urllib.parse import urljoin

    method = (method or "GET").upper()
    if method not in DOWNLOAD_METHODS:
        raise ValueError(f"method {method!r} is not one of {', '.join(DOWNLOAD_METHODS)}")
    if body is not None and method != "POST":
        raise ValueError("a request body is sent with method POST only")
    session = await _get_or_create_session(session_key)
    async with session.lock:
        session.last_used = time.time()
        await _reopen_resumed_page(session_key, session)
        base = str(getattr(session.page, "url", "") or "")
        target = urljoin(base, url) if base.startswith(("http://", "https://")) else url
        refusal = await _url_policy_refusal(target, session)
        if refusal is not None:
            raise ValueError(f"the browser's URL policy refuses {target}: {refusal}")
        running = asyncio.ensure_future(
            session.page.evaluate(download_script(headers_expression), [target, MAX_DOWNLOAD_BYTES, method, body])
        )
        try:
            done, _pending = await asyncio.wait({running}, timeout=DOWNLOAD_TIMEOUT_SECONDS)
            if not done:
                running.add_done_callback(_dropped)
                raise EvaluateTimeoutError(await _stop_script(session, DOWNLOAD_TIMEOUT_SECONDS))
            answer = running.result()
        finally:
            session.last_used = time.time()
    if not isinstance(answer, dict):
        raise ValueError(f"the page answered no response for {target}")
    status = answer.get("status")
    size = int(answer.get("size") or 0)
    if answer.get("tooLarge"):
        raise ValueError(
            f"{target} answered {size:,} bytes, over the {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB a download keeps"
        )
    data = answer.get("data")
    if data is None:
        raise ValueError(f"{target} answered HTTP {status}: nothing was kept")
    body = base64.b64decode(data)
    if not body:
        raise ValueError(f"{target} answered HTTP {status} with an empty body: nothing was kept")
    return DownloadedFile(url=target, content_type=str(answer.get("type") or ""), body=body)


async def browser_close(session_key: str) -> str:
    """Close the browser session; the next one starts signed out."""
    await _close_session(session_key, forget=True)
    return "Browser session closed."
