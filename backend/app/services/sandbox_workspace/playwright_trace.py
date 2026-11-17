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

"""Reading a Playwright ``trace.zip`` in one bounded answer.

Pure: no I/O. ``sandbox_playwright_trace`` (``builtin_tools/sandbox_workspace.py``)
reads the zip's bytes through the workspace session and shapes them here, so a
failed E2E run downloaded from CI (``download_github_workflow_artifact``) is
diagnosed in one call instead of free-form scripts that print the whole
network log.

A trace zip holds ``test.trace`` (the test runner's steps: ``before`` /
``after`` pairs by ``callId``, the ``error`` of the test, the attachments) and
one ``N-trace.trace`` / ``N-trace.network`` pair per browser context (the
library calls with their ``log`` lines, ``frame-snapshot``, ``screencast-frame``
and ``console`` events, and one ``resource-snapshot`` HAR entry per request).
Every event carries a time on one monotonic clock in milliseconds, so the
answer is a window on that clock: by default from ``window_ms`` before the
failing action to one second after it ended (its timeout), or around a time or
a call id the caller names. What falls outside the window is counted, never
dropped silently.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

#: Milliseconds the window reaches before the failing action (or around the
#: time named), by default and at most.
DEFAULT_WINDOW_MS = 5_000
MAX_WINDOW_MS = 600_000
#: Characters the whole answer holds by default, and at most.
DEFAULT_MAX_CHARS = 8_000
MAX_CHARS = 40_000
#: Rows a section shows at most; the rest are counted.
MAX_ACTIONS = 30
MAX_LOG_LINES = 6
MAX_NETWORK = 20
#: Milliseconds the window runs past the anchor's end: the teardown that follows a failure is not the failure.
_AFTER_MS = 250
MAX_CONSOLE = 12
MAX_URLS = 15
#: Characters one inlined JSON attachment shows, and all of them together.
ATTACHMENT_CHARS = 600
ATTACHMENTS_CHARS = 1_500
#: The largest trace zip read (a long suite's trace with screencast is a few MB).
MAX_TRACE_BYTES = 64 * 1024 * 1024
#: Classes of library calls folded into one count: request routing, not page actions.
_ROUTING_CLASSES = frozenset({"Route", "WebSocketRoute"})
_ROUTING_METHODS = frozenset({"setNetworkInterceptionPatterns", "setWebSocketInterceptionPatterns"})
_QUIET_TEST_STEPS = frozenset({"hook", "fixture", "pw:api", "test.attach"})
_REQUEST_CLASSES = frozenset({"APIRequestContext"})
#: Resource types counted, not listed, in the network section: the page's static files.
_STATIC_RESOURCES = frozenset({"script", "stylesheet", "image", "font", "media", "manifest"})
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class TraceError(ValueError):
    """The bytes are not a Playwright trace this reader understands."""


@dataclass
class Call:
    call_id: str
    start: float
    end: float | None = None
    cls: str = ""
    method: str = ""
    title: str = ""
    selector: str = ""
    error: str = ""
    stack: str = ""
    step_id: str = ""
    logs: list[str] = field(default_factory=list)
    test: bool = False


@dataclass
class Trace:
    t0: float
    calls: list[Call]
    errors: list[tuple[str, str]]
    urls: list[tuple[float, str]]
    frames: list[tuple[float, str]]
    console: list[tuple[float, str, str]]
    network: list[tuple[float, int, str, str, float, str]]
    attachments: list[tuple[str, str, str]]
    files: dict[str, bytes]
    version: str


@dataclass
class TraceAnswer:
    text: str
    frame_name: str | None = None
    frame_bytes: bytes | None = None


def _clean(text: Any) -> str:
    return _ANSI.sub("", str(text or "")).strip()


def _lines(data: bytes) -> list[dict[str, Any]]:
    out = []
    for raw in data.decode("utf-8", "replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _stack(stack: Any) -> str:
    if isinstance(stack, list) and stack and isinstance(stack[0], dict):
        top = stack[0]
        return f"{str(top.get('file', '')).rsplit('/', 1)[-1]}:{top.get('line', '?')}"
    return ""


def _path(url: str) -> str:
    parts = urlsplit(url)
    if not parts.scheme.startswith("http"):
        return url[:120]
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query[:40] + ("..." if len(parts.query) > 40 else "")
    return path[:140]


def parse(data: bytes) -> Trace:
    """Every event of a trace zip, on its one clock."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise TraceError("this is not a zip: pass the trace.zip a Playwright run wrote.") from exc
    names = archive.namelist()
    traces = sorted(n for n in names if n.endswith(".trace"))
    if not traces:
        raise TraceError(
            "this zip holds no .trace file: pass the trace.zip itself, not the artifact archive around it."
        )
    calls: dict[str, Call] = {}
    order: list[str] = []
    errors: list[tuple[str, str]] = []
    urls: list[tuple[float, str]] = []
    frames: list[tuple[float, str]] = []
    console: list[tuple[float, str, str]] = []
    network: list[tuple[float, int, str, str, float, str]] = []
    attachments: list[tuple[str, str, str]] = []
    starts: list[float] = []
    version = ""
    for name in traces:
        is_test = name.rsplit("/", 1)[-1] == "test.trace"
        for event in _lines(archive.read(name)):
            kind = event.get("type")
            if kind == "context-options":
                if isinstance(event.get("monotonicTime"), (int, float)):
                    starts.append(float(event["monotonicTime"]))
                version = version or str(event.get("playwrightVersion") or "")
            elif kind == "before":
                cid = f"{name}:{event.get('callId')}"
                params = event.get("params") or {}
                calls[cid] = Call(
                    call_id=str(event.get("callId")),
                    start=float(event.get("startTime") or 0),
                    cls=str(event.get("class") or ""),
                    method=str(event.get("method") or ""),
                    title=_clean(event.get("title")),
                    selector=str(params.get("selector") or params.get("locator") or params.get("url") or ""),
                    stack=_stack(event.get("stack")),
                    step_id=str(event.get("stepId") or ""),
                    test=is_test,
                )
                order.append(cid)
            elif kind == "after":
                call = calls.get(f"{name}:{event.get('callId')}")
                if call is not None:
                    call.end = float(event.get("endTime") or call.start)
                    err = event.get("error")
                    if isinstance(err, dict):
                        call.error = _clean(err.get("message")).split("\n\nCall log:")[0]
                for att in event.get("attachments") or []:
                    if isinstance(att, dict):
                        entry = (
                            str(att.get("name") or ""),
                            str(att.get("contentType") or ""),
                            str(att.get("path") or att.get("file") or ""),
                        )
                        if entry not in attachments:
                            attachments.append(entry)
            elif kind == "log":
                call = calls.get(f"{name}:{event.get('callId')}")
                if call is not None:
                    call.logs.append(_clean(event.get("message")))
            elif kind == "error":
                errors.append((_clean(event.get("message")), _stack(event.get("stack"))))
            elif kind == "frame-snapshot":
                snap = event.get("snapshot") or {}
                if snap.get("isMainFrame", True) and snap.get("frameUrl"):
                    urls.append((float(snap.get("timestamp") or 0), str(snap["frameUrl"])))
            elif kind == "screencast-frame":
                frames.append((float(event.get("timestamp") or 0), str(event.get("file") or "")))
            elif kind == "console":
                level = str(event.get("messageType") or "")
                where = _path(str((event.get("location") or {}).get("url") or ""))
                text = _clean(event.get("text"))
                console.append((float(event.get("time") or 0), level, f"{text} ({where})" if where else text))
    for name in sorted(n for n in names if n.endswith(".network")):
        for event in _lines(archive.read(name)):
            snap = event.get("snapshot") if event.get("type") == "resource-snapshot" else None
            if not isinstance(snap, dict):
                continue
            request = snap.get("request") or {}
            response = snap.get("response") or {}
            network.append(
                (
                    float(snap.get("_monotonicTime") or 0),
                    int(response.get("status") or 0),
                    str(request.get("method") or ""),
                    _path(str(request.get("url") or "")),
                    float(snap.get("time") or 0),
                    str(snap.get("_resourceType") or ""),
                )
            )
    files = {path: archive.read(path) for _, _, path in attachments if path in names}
    every = [c.start for c in calls.values()] + [t for t, *_ in network]
    t0 = min(starts) if starts else (min(every) if every else 0.0)
    urls.sort()
    frames.sort()
    console.sort()
    network.sort()
    return Trace(
        t0=t0,
        calls=[calls[c] for c in order],
        errors=errors,
        urls=urls,
        frames=frames,
        console=console,
        network=network,
        attachments=attachments,
        files=files,
        version=version,
    )


def failing_call(trace: Trace) -> Call | None:
    """The first call that ended in an error: a test step first (it names the locator), else a library call."""
    for want_test in (True, False):
        for call in trace.calls:
            if call.test == want_test and call.error and call.method not in ("hook", "fixture"):
                return call
    return next((c for c in trace.calls if c.error), None)


def _ms(trace: Trace, at: float) -> str:
    return f"{(at - trace.t0) / 1000:.2f}s"


class _Budget:
    def __init__(self, limit: int) -> None:
        self.left = limit
        self.cut = 0

    def take(self, out: list[str], line: str) -> bool:
        if len(line) + 1 > self.left:
            self.cut += 1
            return False
        out.append(line)
        self.left -= len(line) + 1
        return True


def summarize(
    data: bytes,
    *,
    label: str,
    around: str | float | None = None,
    window_ms: int | None = None,
    frame_at: float | None = None,
    max_chars: int | None = None,
) -> TraceAnswer:
    """The bounded answer: the failure, then actions, URLs, console, network and attachments in the window."""
    trace = parse(data)
    span = max(0, min(int(window_ms if window_ms is not None else DEFAULT_WINDOW_MS), MAX_WINDOW_MS))
    limit = max(1_000, min(int(max_chars or DEFAULT_MAX_CHARS), MAX_CHARS))
    failing = failing_call(trace)
    anchor: Call | None = None
    if isinstance(around, str) and around.strip() and not _is_number(around):
        wanted = around.strip()
        anchor = next((c for c in trace.calls if c.call_id == wanted or c.step_id == wanted), None)
        if anchor is None:
            raise TraceError(f"no call {wanted!r} in this trace: the action list names each call id.")
    if around is not None and anchor is None and _is_number(around):
        at = trace.t0 + float(around) * 1000
        lo, hi, why = at - span, at + span, f"around {float(around):.2f}s"
    else:
        anchor = anchor or failing
        if anchor is not None:
            lo, hi = anchor.start - span, (anchor.end or anchor.start) + _AFTER_MS
            why = f"{'call ' + anchor.call_id if around else 'the failing action'} and {span / 1000:g}s before it"
        else:
            last = max([c.end or c.start for c in trace.calls] + [trace.t0])
            lo, hi, why = last - span, last + _AFTER_MS, "the end of the trace (no call failed)"
    out: list[str] = []
    budget = _Budget(limit)
    tests = sum(1 for c in trace.calls if c.test)
    head = (
        f"{label}: Playwright {trace.version or '?'} trace, {len(trace.calls) - tests} browser call(s), "
        f"{tests} test step(s), {len(trace.network)} request(s), {len(trace.frames)} screencast frame(s). "
        f"Window {_ms(trace, lo)} to {_ms(trace, hi)} ({why}); times from the trace start."
    )
    budget.take(out, head)
    budget.take(out, "")
    if trace.errors or failing:
        budget.take(out, "FAILURE")
        message, where = (
            trace.errors[0] if trace.errors else (failing.error if failing else "", failing.stack if failing else "")
        )
        for line in message.split("\n\nCall log:")[0].splitlines()[:8]:
            if line.strip():
                budget.take(out, f"  {line.strip()[:300]}")
        if failing is not None:
            budget.take(
                out,
                f"  at {failing.stack or where or '?'}: {failing.title or failing.method} started {_ms(trace, failing.start)}, "
                f"ended {_ms(trace, failing.end or failing.start)} ({((failing.end or failing.start) - failing.start) / 1000:.1f}s)",
            )
        budget.take(out, "")
    _attachments(trace, out, budget)
    _actions(trace, lo, hi, out, budget)
    _urls(trace, lo, hi, out, budget)
    _console(trace, lo, hi, out, budget)
    _network(trace, lo, hi, out, budget)
    answer = TraceAnswer(text="")
    if frame_at is not None:
        at = trace.t0 + float(frame_at) * 1000
        if trace.frames:
            stamp, path = min(trace.frames, key=lambda f: abs(f[0] - at))
            try:
                answer.frame_bytes = zipfile.ZipFile(io.BytesIO(data)).read(path)
                answer.frame_name = f"frame-{(stamp - trace.t0) / 1000:.2f}s.jpeg"
            except KeyError:
                budget.take(out, f"Screencast frame {path} is named in the trace but absent from the zip.")
        else:
            budget.take(out, "No screencast frame in this trace (recorded without screenshots).")
    if budget.cut:
        out.append(f"[{budget.cut} line(s) past {limit:,} characters not shown: narrow window_ms or raise max_chars]")
    answer.text = "\n".join(out).rstrip()
    return answer


def _is_number(value: Any) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    try:
        float(str(value))
    except ValueError:
        return False
    return True


def _actions(trace: Trace, lo: float, hi: float, out: list[str], budget: _Budget) -> None:
    library = {c.step_id for c in trace.calls if not c.test and c.step_id}
    routing = 0
    shown: list[Call] = []
    before = after = 0
    for call in trace.calls:
        end = call.end or call.start
        if call.cls in _ROUTING_CLASSES or call.method in _ROUTING_METHODS:
            routing += lo <= call.start <= hi
            continue
        # A test step that mirrors a browser call (pw:api), a hook, a fixture or an attach adds nothing
        # the library calls and the attachments section do not say: shown only when it failed.
        if call.test and not call.error and (call.method in _QUIET_TEST_STEPS or call.call_id in library):
            continue
        if end < lo:
            before += 1
        elif call.start > hi:
            after += 1
        else:
            shown.append(call)
    shown.sort(key=lambda c: c.start)
    budget.take(
        out,
        f"ACTIONS in the window ({len(shown)}; {before} before, {after} after; {routing} request-routing call(s) folded)",
    )
    for call in shown[-MAX_ACTIONS:] if len(shown) > MAX_ACTIONS else shown:
        dur = ((call.end or call.start) - call.start) / 1000
        what = f"{call.cls}.{call.method}" if call.cls and not call.test else call.method
        line = f"  {_ms(trace, call.start)} +{dur:.2f}s {call.call_id} {what}"
        if call.title and call.title != call.method:
            line += f" {call.title!r}"[:120]
        if call.selector:
            line += f" {call.selector[:160]}"
        if call.error:
            line += f" ERROR {call.error.splitlines()[0][:200]}"
        budget.take(out, line)
        logs = call.logs
        if call.cls in _REQUEST_CLASSES:
            # An API request's log is its headers: keep the request and status lines.
            logs = [line for line in logs if line[:1] == "\u2190"]
        for log in logs[-MAX_LOG_LINES:]:
            budget.take(out, f"      | {log[:200]}")
        if len(logs) > MAX_LOG_LINES:
            budget.take(out, f"      | ({len(logs) - MAX_LOG_LINES} earlier log line(s))")
    if len(shown) > MAX_ACTIONS:
        budget.take(out, f"  ({len(shown) - MAX_ACTIONS} earlier action(s) of the window not shown)")
    budget.take(out, "")


def _urls(trace: Trace, lo: float, hi: float, out: list[str], budget: _Budget) -> None:
    timeline: list[tuple[float, str]] = []
    for at, url in trace.urls:
        if not timeline or timeline[-1][1] != url:
            timeline.append((at, url))
    budget.take(out, f"PAGE URL per snapshot ({len(timeline)} change(s) over the whole trace; * = in the window)")
    for at, url in timeline[-MAX_URLS:]:
        budget.take(out, f"  {'*' if lo <= at <= hi else ' '} {_ms(trace, at)} {url[:200]}")
    if len(timeline) > MAX_URLS:
        budget.take(out, f"  ({len(timeline) - MAX_URLS} earlier change(s))")
    budget.take(out, "")


def _console(trace: Trace, lo: float, hi: float, out: list[str], budget: _Budget) -> None:
    inside = [c for c in trace.console if lo <= c[0] <= hi]
    loud = [c for c in trace.console if c[1] in ("error", "warning")]
    if not trace.console:
        return
    budget.take(out, f"CONSOLE in the window ({len(inside)}; {len(loud)} error/warning over the whole trace)")
    for at, level, text in inside[-MAX_CONSOLE:]:
        budget.take(out, f"  {_ms(trace, at)} {level} {text[:240]}")
    budget.take(out, "")


def _network(trace: Trace, lo: float, hi: float, out: list[str], budget: _Budget) -> None:
    window = [n for n in trace.network if lo <= n[0] <= hi]
    inside = [n for n in window if n[5] not in _STATIC_RESOURCES or n[1] == 0 or n[1] >= 400]
    static = len(window) - len(inside)
    before = sum(1 for n in trace.network if n[0] < lo)
    after = sum(1 for n in trace.network if n[0] > hi)
    failed = [n for n in trace.network if n[1] == 0 or n[1] >= 400]
    budget.take(
        out,
        f"NETWORK in the window ({len(window)}, {static} static file(s) counted only; {before} before, {after} after; "
        f"{len(failed)} failed or unanswered over the whole trace)",
    )
    for at, status, method, path, dur, _kind in inside[-MAX_NETWORK:]:
        budget.take(out, f"  {_ms(trace, at)} {status or '---'} {method} {path} {dur:.0f}ms")
    if len(inside) > MAX_NETWORK:
        budget.take(out, f"  ({len(inside) - MAX_NETWORK} earlier request(s) of the window not shown)")
    outside = [n for n in failed if not lo <= n[0] <= hi]
    if outside:
        budget.take(out, "  failed outside the window:")
        for at, status, method, path, dur, _kind in outside[-5:]:
            budget.take(out, f"  {_ms(trace, at)} {status or '---'} {method} {path} {dur:.0f}ms")
    budget.take(out, "")


def _attachments(trace: Trace, out: list[str], budget: _Budget) -> None:
    if not trace.attachments:
        return
    budget.take(out, f"ATTACHMENTS ({len(trace.attachments)})")
    room = ATTACHMENTS_CHARS
    for name, kind, path in trace.attachments:
        body = trace.files.get(path)
        size = f"{len(body):,} B" if body is not None else "not in the zip"
        budget.take(out, f"  {name} ({kind or '?'}, {size})")
        if body is None or "json" not in kind or room <= 0:
            continue
        try:
            text = json.dumps(json.loads(body), separators=(",", ":"), ensure_ascii=False)
        except ValueError:
            text = body.decode("utf-8", "replace")
        cap = min(ATTACHMENT_CHARS, room)
        shown = text[:cap] + (f"... ({len(text) - cap:,} more characters)" if len(text) > cap else "")
        room -= cap
        budget.take(out, f"    {shown}")
