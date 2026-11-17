"""A take refused once measured (GPU GL, ``fps_fallback: refuse``) keeps its capture, events and step notes, so its
reader says the same capture line a ready take says: the frames kept each second and what its canvases copied."""

from __future__ import annotations

import ast
import inspect

from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import canvas_alone, capture_rate, runners


def _refusal_mark() -> ast.Call:
    """The ``jobs.mark`` call of the branch that refuses a measured take."""
    tree = ast.parse(inspect.getsource(runners))
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "capture_rate.refusal" in ast.unparse(node.test):
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and ast.unparse(call.func) == "jobs.mark":
                    return call
    raise AssertionError("no jobs.mark under the capture_rate.refusal branch")


def test_the_refused_take_keeps_its_capture_events_and_notes():
    keywords = {k.arg: ast.unparse(k.value) for k in _refusal_mark().keywords}
    assert keywords["status"] == "JOB_FAILED"
    assert "captured" in keywords["capture"]
    assert "result.events" in keywords["events"]
    assert "notes" in keywords["error"] and "refused" in keywords["error"]


def test_the_failed_take_reader_says_its_rates_with_its_canvases():
    source = inspect.getsource(tool)
    at = source.index("Capture up to the failure")
    said = source[at - 600 : at]
    assert "rates_words(captured)" in said
    assert "take_capture_words(captured)" in said


def test_the_capture_line_of_a_refused_take_is_built_from_its_kept_capture():
    captured = {"rates": [25, 13, 20], "gl": {"gpu": True}, "fps": 30}
    line = "; ".join(s for s in (capture_rate.rates_words(captured), canvas_alone.take_capture_words(captured)) if s)
    assert line.startswith("frames kept each second: 25 13 20")
    assert "canvases drawn with GPU GL" in line
