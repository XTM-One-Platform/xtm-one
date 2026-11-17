"""A JSON-format agent's prose answer is delivered, not a bracketed fragment of it.

``emit_bare_json`` (no ``output_schema``) and ``emit_repaired_json`` (with one) clean an
``output_format="json"`` agent's answer to JSON. When the model answered in prose, the
outermost bracketed span of the prose was repaired into JSON and delivered as the whole
answer: ``The actor is APT29 [high confidence], see the report.`` became
``["high confidence"]``. A span that is not JSON and reads as prose is now left alone, so
the caller keeps the model's text.

The repair of damaged JSON wrapped in prose must not change: consumers such as OpenAEV
parse the reply strictly and rely on it. ``_WORKING`` is the side-by-side corpus, every
entry delivered today and asserted unchanged.
"""

from __future__ import annotations

import gc
import inspect
import json
import sys
import time
from collections.abc import Callable
from types import CodeType

import pytest

from app.services import structured_output
from app.services.structured_output import (
    emit_bare_json,
    emit_repaired_json,
    extract_json_text,
    reads_as_prose,
)

_STRINGS_SCHEMA: dict = {"type": "array", "items": {"type": "string"}}
_OBJECT_SCHEMA: dict = {"type": "object"}

# ---------------------------------------------------------------------------
# Prose answers: the fragment is never delivered
# ---------------------------------------------------------------------------

_PROSE = [
    # The issue's table.
    "The actor is APT29 [high confidence], see the report.",
    "I could not find the indicators {sorry}.",
    "Use options [a] or [b]",
    "```text\nUse options [a] or [b]\n```",
    # The same families.
    "See [1] and [2].",
    "I would pick [the first option] here.",
    "I could not find them {sorry, the feed is down}.",
    "No result {see the logs} for now.",
    "```\nThe feed returned [nothing useful] today.\n```",
    'Result: {"answer": "x",} Let me know if {you} need more.',
]


@pytest.mark.parametrize("raw", _PROSE)
def test_prose_answer_is_kept_without_schema(raw: str) -> None:
    assert emit_bare_json(raw, log_context="test") is None


@pytest.mark.parametrize("raw", _PROSE)
def test_prose_answer_is_kept_with_an_array_schema(raw: str) -> None:
    assert emit_repaired_json(raw, _STRINGS_SCHEMA, log_context="test") is None


@pytest.mark.parametrize("raw", _PROSE)
def test_prose_answer_is_kept_with_an_object_schema(raw: str) -> None:
    assert emit_repaired_json(raw, _OBJECT_SCHEMA, log_context="test") is None


# ---------------------------------------------------------------------------
# Damaged JSON wrapped in prose: repaired exactly as before
# ---------------------------------------------------------------------------

_WORKING: list[tuple[str, object]] = [
    # Trailing comma after / between sentences.
    (
        'Here is the result:\n{"technique": "T1059", "confidence": "high",}',
        {"technique": "T1059", "confidence": "high"},
    ),
    (
        'Here is the result:\n{"technique": "T1059",}\nLet me know if you need more.',
        {"technique": "T1059"},
    ),
    # Single quotes, unquoted keys, Python literals, comments.
    (
        "Here is the result:\n{'technique': 'T1059', 'confidence': 'high'}",
        {"technique": "T1059", "confidence": "high"},
    ),
    (
        'Here is the result:\n{technique: "T1059", confidence: "high"}',
        {"technique": "T1059", "confidence": "high"},
    ),
    (
        'Here is the result:\n{"ok": True, "flagged": False}',
        {"ok": True, "flagged": False},
    ),
    (
        'Here is the result:\n{"technique": "T1059" // the main one\n}',
        {"technique": "T1059"},
    ),
    (
        'Here is the result:\n{"technique": "T1059" /* the main one */}',
        {"technique": "T1059"},
    ),
    # Unquoted values, single-word in an array, multi-word in an object.
    ("Techniques: [T1059, T1566]", ["T1059", "T1566"]),
    ("{name: John Smith}", {"name": "John Smith"}),
    # An unescaped quote inside a string.
    ('Here is {"a": "the "APT29" group"} done.', {"a": 'the "APT29" group'}),
    # Fenced, with prose inside or before the fence.
    (
        "```\nSure, here you go:\n{'a': 3,}\n```",
        {"a": 3},
    ),
    (
        'Here you go:\n```json\n{"a": 1, "b": [1, 2,],}\n```',
        {"a": 1, "b": [1, 2]},
    ),
    # A missing comma, a mismatched close, a truncated payload.
    ('{"a": 1 "b": 2}', {"a": 1, "b": 2}),
    ('Result: {"a": [1, 2}', {"a": [1, 2]}),
    ('Here: {"a": [1, 2', {"a": [1, 2]}),
    # Brackets inside strings (a STIX pattern), a bad escape.
    (
        'Bundle:\n{"pattern": "[file:hashes.MD5 = \'abc\']",}',
        {"pattern": "[file:hashes.MD5 = 'abc']"},
    ),
    ('{"d": "a \\& b",}', {"d": "a \\& b"}),
]


@pytest.mark.parametrize(("raw", "expected"), _WORKING)
def test_damaged_json_is_still_repaired_without_schema(raw: str, expected: object) -> None:
    out = emit_bare_json(raw, log_context="test")
    assert out is not None
    assert json.loads(out) == expected


@pytest.mark.parametrize(("raw", "expected"), _WORKING)
def test_damaged_json_is_still_repaired_with_a_schema(raw: str, expected: object) -> None:
    schema = _OBJECT_SCHEMA if isinstance(expected, dict) else _STRINGS_SCHEMA
    out = emit_repaired_json(raw, schema, log_context="test")
    assert out is not None
    assert json.loads(out) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('Here is the result:\n["T1059", "T1566"]', ["T1059", "T1566"]),
        ("I found 3 items: [1, 2, 3] in the list.", [1, 2, 3]),
        ('{"a": 1}', {"a": 1}),
    ],
)
def test_valid_json_after_a_sentence_is_still_extracted(raw: str, expected: object) -> None:
    """Valid JSON is never second-guessed: only a span that needs repair is checked."""
    out = emit_bare_json(raw, log_context="test")
    assert out is not None
    assert json.loads(out) == expected


# ---------------------------------------------------------------------------
# Fence search and prose check are linear
# ---------------------------------------------------------------------------
#
# Linearity is asserted on growth, not on an absolute wall-clock ceiling: a fixed
# 1.0 s budget went red on a loaded xdist runner (1.07 s) with linear code. The input
# grows 4x; linear work grows about 4x, the quadratic backtracking these tests guard
# against grows about 16x. CPU time of this process (``time.process_time``), the best
# of a few repeats with the garbage collector off, so neighbouring workers and a
# collection pause do not count.

_GROWTH = 4
_LINEAR_CEILING = 8.0
_REPEATS = 3
# Below this, the clock and interpreter noise dominate the small input's time.
_TIME_FLOOR = 0.002


def _best_cpu_seconds(work: Callable[[], object]) -> float:
    best = float("inf")
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(_REPEATS):
            started = time.process_time()
            work()
            best = min(best, time.process_time() - started)
    finally:
        if was_enabled:
            gc.enable()
    return best


def _assert_linear(run: Callable[[str], object], build: Callable[[int], str], n: int) -> None:
    small, large = build(n), build(n * _GROWTH)
    small_seconds = max(_best_cpu_seconds(lambda: run(small)), _TIME_FLOOR)
    large_seconds = _best_cpu_seconds(lambda: run(large))
    assert large_seconds < _LINEAR_CEILING * small_seconds, (
        f"{_GROWTH}x the input took {large_seconds / small_seconds:.1f}x the time "
        f"({small_seconds:.4f}s -> {large_seconds:.4f}s); linear stays under {_LINEAR_CEILING}x"
    )


# The prose check is pure Python, so its linearity is asserted on WORK, not on time:
# the number of Python lines it executes, counted through ``sys.monitoring`` on the
# code of ``structured_output``. The count is deterministic: a loaded runner cannot
# move it (``arrays-closed-as-objects`` went 8.4x on 11 ms of CPU time on a shared
# xdist worker with linear code). Its C-level helpers (``str.find`` to the end of a
# comment, ``str.startswith``, a word's ``fullmatch``) only scan forward from the
# current index, which the counted loop then moves past.
_SCANNER_CODE: tuple[CodeType, ...] = tuple(
    obj.__code__
    for obj in vars(structured_output).values()
    if inspect.isfunction(obj) and obj.__module__ == structured_output.__name__
)


def _executed_lines(run: Callable[[str], object], arg: str) -> int:
    """Python lines of ``structured_output`` that ``run(arg)`` executes."""
    monitoring = sys.monitoring
    tool = next(t for t in range(6) if monitoring.get_tool(t) is None)
    monitoring.use_tool_id(tool, "prose-check-linearity")
    count = 0

    def on_line(code: CodeType, line: int) -> None:
        nonlocal count
        count += 1

    try:
        monitoring.register_callback(tool, monitoring.events.LINE, on_line)
        for code in _SCANNER_CODE:
            monitoring.set_local_events(tool, code, monitoring.events.LINE)
        run(arg)
    finally:
        for code in _SCANNER_CODE:
            monitoring.set_local_events(tool, code, monitoring.events.NO_EVENTS)
        monitoring.register_callback(tool, monitoring.events.LINE, None)
        monitoring.free_tool_id(tool)
    return count


def _assert_linear_work(run: Callable[[str], object], build: Callable[[int], str], n: int) -> None:
    small = _executed_lines(run, build(n))
    large = _executed_lines(run, build(n * _GROWTH))
    assert small > 0, "no line of structured_output was counted"
    assert large < _LINEAR_CEILING * small, (
        f"{_GROWTH}x the input executed {large / small:.1f}x the lines "
        f"({small} -> {large}); linear stays under {_LINEAR_CEILING}x"
    )


def test_unclosed_fence_followed_by_whitespace_is_linear() -> None:
    """``\\s*`` then a lazy group backtracked over every whitespace prefix: 40,000
    spaces took 3.3 s, doubling them quadrupled it. 400,000 now take milliseconds."""
    raw = "```" + " " * 400_000 + "x"
    assert extract_json_text(raw) == raw.strip()
    _assert_linear(extract_json_text, lambda n: "```" + " " * n + "x", 100_000)


@pytest.mark.parametrize(
    ("build", "n"),
    [
        # Quotes that never close, each followed by a word.
        (lambda n: "[" + '" a ' * n + "]", 2_500),
        # Quotes inside a comment line, each followed by a comment.
        (lambda n: '"' + '"#' * n + "\nx", 2_500),
        # Deep nesting closed by the wrong kind of bracket.
        (lambda n: "{" * n + "]" * n, 2_500),
        (lambda n: "[" * n + "}" * n, 2_500),
        # Many words, each looking past a long run of whitespace.
        (lambda n: "[" + ("a," + " " * 50) * n + "]", 500),
    ],
    ids=[
        "unclosed-quotes",
        "quotes-in-comment",
        "objects-closed-as-arrays",
        "arrays-closed-as-objects",
        "whitespace-runs",
    ],
)
def test_prose_check_is_linear(build: Callable[[int], str], n: int) -> None:
    _assert_linear_work(reads_as_prose, build, n)


def test_line_count_catches_quadratic_work() -> None:
    """The counter itself: a scan that restarts from the start at every character
    (the backtracking these tests guard against) fails the linear assertion."""

    def quadratic(span: str) -> bool:
        return any(reads_as_prose(span[:i]) for i in range(0, len(span), 50))

    with pytest.raises(AssertionError, match="linear stays under"):
        _assert_linear_work(quadratic, lambda n: "[" + "a, " * n + "]", 500)
