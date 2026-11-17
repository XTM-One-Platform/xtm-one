"""Logging conventions guard (see dev-docs/logging-assessment.md).

Two contracts are pinned here:

1. ``format_exception_message()`` — the shared helper introduced by the
   logging assessment — must never return an empty string, because log
   lines like ``"Failed to refresh token for X (notion): "`` (empty
   ``str(exc)`` for ``asyncio.TimeoutError``, ``CancelledError`` and
   several httpx transport errors) carry zero diagnostic value in
   production.

2. No *silent* broad exception swallow may be added to ``backend/app``:
   an ``except Exception:`` / ``except BaseException:`` / bare ``except:``
   handler whose entire body is ``pass`` hides real failures from
   operators.  Narrow handlers (``except ValueError: pass`` for parse
   fallbacks) are fine and are not flagged.  Known legitimate sites are
   allowlisted explicitly below.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from app.core.utils import format_exception_message
from tests import app_sources

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# ---------------------------------------------------------------------------
# 1. format_exception_message()
# ---------------------------------------------------------------------------


def test_non_empty_message_is_preserved_verbatim():
    assert format_exception_message(ValueError("boom")) == "boom"


def test_empty_message_falls_back_to_class_name():
    # These are the exact production offenders: str() of them is "".
    assert format_exception_message(TimeoutError()) == "TimeoutError"
    assert format_exception_message(asyncio.CancelledError()) == "CancelledError"
    assert format_exception_message(Exception()) == "Exception"


def test_whitespace_only_message_falls_back_to_class_name():
    assert format_exception_message(RuntimeError("   ")) == "RuntimeError"


def test_empty_message_with_cause_includes_the_root_reason():
    try:
        try:
            raise ConnectionError("connection reset by peer")
        except ConnectionError as root:
            raise TimeoutError() from root
    except TimeoutError as exc:
        msg = format_exception_message(exc)
    assert msg == "TimeoutError (caused by connection reset by peer)"


def test_raise_from_none_suppresses_the_context():
    try:
        try:
            raise KeyError("k")
        except KeyError:
            raise TimeoutError() from None  # explicit context suppression
    except TimeoutError as exc:
        assert format_exception_message(exc) == "TimeoutError"


def test_empty_cause_message_uses_the_cause_class_name():
    try:
        try:
            raise OSError()  # str(OSError()) == "" - empty like the outer exc
        except OSError as root:
            raise TimeoutError() from root
    except TimeoutError as exc:
        msg = format_exception_message(exc)
    # Both messages are empty, so both fall back to their class names.
    assert msg == "TimeoutError (caused by OSError)"


def test_broken_str_never_propagates_from_the_helper():
    """Unlike lazy %s formatting inside the logging handler (where a
    raising ``__str__`` is contained by ``Handler.handleError``), the
    helper runs at the call site - it must swallow a broken ``__str__``
    instead of crashing the except block that was logging it."""

    class BrokenStrError(Exception):
        def __str__(self) -> str:  # pragma: no cover - the raise IS the test
            raise RuntimeError("__str__ exploded")

    assert format_exception_message(BrokenStrError()) == "BrokenStrError"


def test_never_returns_empty_string_for_common_exception_types():
    samples: list[BaseException] = [
        ValueError(),
        KeyError(),
        OSError(),
        TimeoutError(),
        asyncio.CancelledError(),
        StopAsyncIteration(),
    ]
    for exc in samples:
        assert format_exception_message(exc).strip(), (
            f"format_exception_message({type(exc).__name__}) returned an "
            f"empty string - the whole point of the helper is that this "
            f"can never happen"
        )


def test_format_network_error_fallback_is_never_empty():
    """The shared integration-error formatter must route unknown exception
    types through the helper so tool results / warning logs never end in
    a bare ``: ``."""
    from app.integrations.http import format_network_error

    msg = format_network_error(RuntimeError(), "GitHub", "list repos")
    assert msg.rstrip() == msg or not msg.endswith(": ")
    assert "RuntimeError" in msg


# ---------------------------------------------------------------------------
# 2. No silent broad exception swallows in backend/app
# ---------------------------------------------------------------------------

# Known, intentionally-silent sites (reviewed during the logging
# assessment). Each entry is a file path relative to app/ (POSIX form);
# every broad silent handler in that file is exempted. If you legitimately
# need to add one, prefer logging at debug level instead; only extend this
# list with a strong reason.
_ALLOWED_SILENT_SWALLOWS: set[str] = {
    # Matplotlib figure capture inside the sandboxed code-interpreter
    # preamble: best-effort rendering cleanup, runs inside user code.
    "services/code_interpreter.py",
    # Custom-tool sandbox stderr JSON probing (owned by a parallel
    # workstream - see dev-docs/logging-assessment.md).
    "services/custom_tool_executor.py",
}


def _is_broad_handler(handler: ast.ExceptHandler) -> bool:
    """True for ``except:``, ``except Exception:``, ``except BaseException:``
    (including tuple forms that contain one of those)."""

    def _names(node: ast.expr | None) -> list[str]:
        if node is None:
            return ["<bare>"]
        if isinstance(node, ast.Tuple):
            return [n for elt in node.elts for n in _names(elt)]
        if isinstance(node, ast.Name):
            return [node.id]
        if isinstance(node, ast.Attribute):
            return [node.attr]
        return []

    broad = {"<bare>", "Exception", "BaseException"}
    return any(name in broad for name in _names(handler.type))


def _silent_swallows(path: Path) -> list[int]:
    tree = app_sources.tree(path)
    lines: list[int] = []
    for node in app_sources.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _is_broad_handler(node):
            continue
        body = [stmt for stmt in node.body if not isinstance(stmt, ast.Pass)]
        # A handler whose body is nothing but ``pass`` (docstring-only
        # bodies count as silent too).
        if not body or all(
            isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
            for stmt in body
        ):
            lines.append(node.lineno)
    return lines


def test_no_silent_broad_exception_swallows_in_app():
    offenders: list[str] = []
    for path in app_sources.python_files(APP_DIR):
        rel = path.relative_to(APP_DIR).as_posix()
        if rel in _ALLOWED_SILENT_SWALLOWS:
            continue
        for lineno in _silent_swallows(path):
            offenders.append(f"app/{rel}:{lineno}")
    assert not offenders, (
        "Broad exception handler(s) that silently swallow the error were "
        "introduced:\n  " + "\n  ".join(offenders) + "\n"
        "An `except Exception: pass` hides real failures from operators. "
        "Either narrow the exception type (parse fallbacks etc.), or log "
        "the failure - at least at debug level - using "
        "format_exception_message() from app.core.utils for the reason. "
        "If the site is genuinely legitimate, add it to "
        "_ALLOWED_SILENT_SWALLOWS in this test with a justification "
        "comment."
    )
