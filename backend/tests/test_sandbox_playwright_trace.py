"""``sandbox_playwright_trace``: a Playwright trace.zip read in one bounded answer (``playwright_trace.py``)."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from app.services.sandbox_workspace import playwright_trace as pt


def _jsonl(events: list[dict]) -> bytes:
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode()


def _fixture_zip(*, network: int = 40, with_frame: bool = True) -> bytes:
    """A small trace in the layout Playwright 1.4x-1.6x writes: test.trace + one context pair."""
    t0 = 1000.0
    test = [
        {
            "version": 9,
            "type": "context-options",
            "origin": "testRunner",
            "monotonicTime": t0,
            "playwrightVersion": "1.63.0",
        },
        {
            "type": "before",
            "callId": "hook@1",
            "stepId": "hook@1",
            "startTime": t0 + 1,
            "class": "Test",
            "method": "hook",
            "title": "Before Hooks",
        },
        {"type": "after", "callId": "hook@1", "endTime": t0 + 5},
        {
            "type": "before",
            "callId": "pw:api@2",
            "stepId": "pw:api@2",
            "startTime": t0 + 10,
            "class": "Test",
            "method": "pw:api",
            "title": "Navigate",
        },
        {"type": "after", "callId": "pw:api@2", "endTime": t0 + 200},
        {
            "type": "before",
            "callId": "expect@9",
            "stepId": "expect@9",
            "startTime": t0 + 20_000,
            "class": "Test",
            "method": "expect",
            "title": 'Expect "toBeVisible"',
            "params": {"locator": "getByText('Outbound')"},
            "stack": [{"file": "/ci/tests/a.spec.ts", "line": 42, "column": 7}],
        },
        {
            "type": "after",
            "callId": "expect@9",
            "endTime": t0 + 35_000,
            "error": {
                "message": "Error: \u001b[2mexpect(locator).toBeVisible()\u001b[22m failed\n\nCall log:\n  - waiting"
            },
        },
        {
            "type": "error",
            "message": "Error: expect(locator).toBeVisible() failed\n\nLocator: getByText('Outbound')\nTimeout: 15000ms\n\nCall log:\n - x",
            "stack": [{"file": "/ci/tests/a.spec.ts", "line": 42}],
        },
        {
            "type": "before",
            "callId": "test.attach@10",
            "stepId": "test.attach@10",
            "startTime": t0 + 35_100,
            "class": "Test",
            "method": "test.attach",
            "title": "Attach",
        },
        {
            "type": "after",
            "callId": "test.attach@10",
            "endTime": t0 + 35_200,
            "attachments": [{"name": "browser-log.json", "contentType": "application/json", "file": "attachments/abc"}],
        },
    ]
    ctx = [
        {
            "version": 9,
            "type": "context-options",
            "origin": "library",
            "monotonicTime": t0 + 2,
            "playwrightVersion": "1.63.0",
        },
        {
            "type": "before",
            "callId": "call@1",
            "startTime": t0 + 11,
            "class": "Frame",
            "method": "goto",
            "params": {"url": "http://localhost:4000/agents/1"},
            "stepId": "pw:api@2",
        },
        {
            "type": "log",
            "callId": "call@1",
            "time": t0 + 12,
            "message": 'navigating to "http://localhost:4000/agents/1"',
        },
        {"type": "after", "callId": "call@1", "endTime": t0 + 190},
        {
            "type": "frame-snapshot",
            "snapshot": {"callId": "call@1", "frameUrl": "about:blank", "timestamp": t0 + 11, "isMainFrame": True},
        },
        {
            "type": "frame-snapshot",
            "snapshot": {
                "callId": "call@1",
                "frameUrl": "http://localhost:4000/agents/1",
                "timestamp": t0 + 190,
                "isMainFrame": True,
            },
        },
        {
            "type": "before",
            "callId": "call@5",
            "startTime": t0 + 19_000,
            "class": "Route",
            "method": "continue",
            "params": {},
            "stepId": "pw:api@5",
        },
        {"type": "after", "callId": "call@5", "endTime": t0 + 19_001},
        {
            "type": "before",
            "callId": "call@6",
            "startTime": t0 + 19_500,
            "class": "Frame",
            "method": "click",
            "params": {"selector": "[data-slot=binding] >> nth=-1"},
            "stepId": "pw:api@6",
        },
        {"type": "after", "callId": "call@6", "endTime": t0 + 19_600},
        {
            "type": "frame-snapshot",
            "snapshot": {
                "callId": "call@6",
                "frameUrl": "http://localhost:4000/agents/1?tab=a2a",
                "timestamp": t0 + 19_600,
                "isMainFrame": True,
            },
        },
        {
            "type": "before",
            "callId": "call@7",
            "startTime": t0 + 20_001,
            "class": "Frame",
            "method": "expect",
            "params": {"selector": 'internal:text="Outbound"i'},
            "stepId": "expect@9",
        },
        {"type": "log", "callId": "call@7", "time": t0 + 20_002, "message": "waiting for getByText('Outbound')"},
        {"type": "after", "callId": "call@7", "endTime": t0 + 35_000, "error": {"message": "Expect failed"}},
        {
            "type": "console",
            "messageType": "error",
            "text": "Failed to load resource: 503",
            "location": {"url": "http://localhost:4000/api/v1/a2a/client/9"},
            "time": t0 + 19_700,
        },
        {"type": "screencast-frame", "file": "screencast/p-1.jpeg", "timestamp": t0 + 19_650},
    ]
    net = []
    for i in range(network):
        at = t0 + 100 + i * 10 if i < network - 3 else t0 + 19_550 + i
        status = 503 if i == network - 2 else 200
        net.append(
            {
                "type": "resource-snapshot",
                "snapshot": {
                    "_monotonicTime": at,
                    "time": 12.5,
                    "request": {
                        "method": "GET",
                        "url": f"http://localhost:4000/api/v1/item/{i}?token=secret-long-value-{'x' * 60}",
                    },
                    "response": {"status": status},
                },
            }
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("test.trace", _jsonl(test))
        z.writestr("0-trace.trace", _jsonl(ctx))
        z.writestr("0-trace.network", _jsonl(net))
        z.writestr("attachments/abc", json.dumps({"pending": [{"method": "GET", "url": "/api/v1/activity/stream"}]}))
        if with_frame:
            z.writestr("screencast/p-1.jpeg", b"\xff\xd8jpeg")
    return buf.getvalue()


def test_the_default_window_is_the_failing_action_and_its_timeout():
    answer = pt.summarize(_fixture_zip(), label="work/trace.zip")
    text = answer.text
    assert text.startswith("work/trace.zip: Playwright 1.63.0 trace, ")
    assert "Window 15.00s to 35.25s (the failing action and 5s before it)" in text
    assert text.index("ATTACHMENTS") < text.index("ACTIONS")
    assert "FAILURE" in text and "Locator: getByText('Outbound')" in text
    assert 'at a.spec.ts:42: Expect "toBeVisible" started 20.00s, ended 35.00s (15.0s)' in text
    assert "\x1b[" not in text
    # the click and the failing expect are in the window, with their call logs; navigation is counted before it
    assert "Frame.click [data-slot=binding] >> nth=-1" in text
    assert "| waiting for getByText('Outbound')" in text
    assert "navigating to" not in text
    assert "1 request-routing call(s) folded" in text and "Route.continue" not in text
    # page URL timeline, console error, network window and the failure outside it
    assert "* 19.60s http://localhost:4000/agents/1?tab=a2a" in text
    assert "Failed to load resource: 503 (/api/v1/a2a/client/9)" in text
    assert (
        "NETWORK in the window (3, 0 static file(s) counted only; 37 before, 0 after; 1 failed or unanswered over the whole trace)"
        in text
    )
    assert "503 GET /api/v1/item/38?" in text and "/api/v1/item/5?" not in text
    assert "..." in text  # long query strings are cut
    # attachments: the JSON one inlined
    assert "browser-log.json (application/json" in text and '"/api/v1/activity/stream"' in text
    assert len(text) < pt.DEFAULT_MAX_CHARS


def test_around_a_time_or_a_call_moves_the_window():
    early = pt.summarize(_fixture_zip(), label="t.zip", around="0.15", window_ms=100)
    assert "Window 0.05s to 0.25s (around 0.15s)" in early.text
    assert "Frame.goto" in early.text and "Frame.click" not in early.text
    by_call = pt.summarize(_fixture_zip(), label="t.zip", around="call@6", window_ms=0)
    assert "(call call@6 and 0s before it)" in by_call.text
    with pytest.raises(pt.TraceError, match="no call 'call@404'"):
        pt.summarize(_fixture_zip(), label="t.zip", around="call@404")


def test_frame_at_returns_the_nearest_screencast_frame():
    answer = pt.summarize(_fixture_zip(), label="t.zip", frame_at=19.5)
    assert answer.frame_bytes == b"\xff\xd8jpeg" and answer.frame_name == "frame-19.65s.jpeg"
    none = pt.summarize(_fixture_zip(with_frame=False), label="t.zip", frame_at=19.0)
    assert none.frame_bytes is None and "absent from the zip" in none.text


def test_the_answer_is_bounded_and_counts_what_it_cut():
    answer = pt.summarize(_fixture_zip(network=400), label="t.zip", window_ms=600_000, max_chars=1_000)
    assert len(answer.text) <= 1_200
    assert "line(s) past 1,000 characters not shown: narrow window_ms or raise max_chars" in answer.text


def test_not_a_trace_is_refused_with_the_reason():
    with pytest.raises(pt.TraceError, match="not a zip"):
        pt.summarize(b"plain text", label="x")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("trace.zip", b"nested")
    with pytest.raises(pt.TraceError, match=r"no \.trace file"):
        pt.summarize(buf.getvalue(), label="x")


_REAL = os.environ.get("PLAYWRIGHT_TRACE_SAMPLE")


@pytest.mark.skipif(not _REAL, reason="PLAYWRIGHT_TRACE_SAMPLE names a real trace.zip to measure (manual)")
def test_a_real_ci_trace_answers_under_the_default_cap():  # pragma: no cover - manual measure
    answer = pt.summarize(Path(_REAL).read_bytes(), label=Path(_REAL).name)
    assert len(answer.text) < pt.DEFAULT_MAX_CHARS and "FAILURE" in answer.text
