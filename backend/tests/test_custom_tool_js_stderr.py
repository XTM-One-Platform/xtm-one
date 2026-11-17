"""Tests for Node.js warning filtering and sandbox env sanitisation.

Covers:
- ``_filter_node_warnings`` removes ``(node:<pid>) <Name>Warning:`` lines
  (and the ``--trace-warnings`` hint) from stderr while preserving the
  actual error payload, including non-warning ``(node:<pid>)`` output.
- ``_build_sandbox_env`` never includes ``NODE_TLS_REJECT_UNAUTHORIZED``
  regardless of the ``s3_reject_unauthorized`` setting (regression guard:
  the S3/MinIO TLS bypass used to be propagated to every sandbox subprocess,
  disabling certificate verification for custom JS tools).
- ``execute_js_tool`` surfaces clean error messages when Node.js writes
  warnings to stderr alongside the structured JSON error object.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.core.config import settings
from app.services import custom_tool_executor as cte
from app.services.code_interpreter import _build_sandbox_env
from app.services.custom_tool_executor import _filter_node_warnings


@pytest.fixture(autouse=True)
def _skip_node_permission_probe(monkeypatch):
    """Pre-populate the Node permission-model probe cache.

    The tests below monkeypatch ``asyncio.create_subprocess_exec`` with fakes
    that only implement ``communicate()``; without a cached result the
    resolver in ``execute_js_tool`` would probe through those fakes.
    """
    monkeypatch.setattr(cte, "_node_permission_flags", ())

# ---------------------------------------------------------------------------
# _filter_node_warnings
# ---------------------------------------------------------------------------


def test_filter_node_warnings_removes_single_warning():
    raw = (
        "(node:552) Warning: Setting the NODE_TLS_REJECT_UNAUTHORIZED environment "
        "variable to '0' makes TLS connections and HTTPS requests insecure by "
        "disabling certificate verification.\n"
        '{"error":"GitHub API error: 403 Forbidden"}'
    )
    result = _filter_node_warnings(raw)
    assert result == '{"error":"GitHub API error: 403 Forbidden"}'


def test_filter_node_warnings_removes_multiple_warnings():
    raw = (
        "(node:1) Warning: First warning line.\n"
        "(node:1) [DEP0025] DeprecationWarning: sys.stdin.end() is deprecated.\n"
        "actual output here"
    )
    result = _filter_node_warnings(raw)
    assert result == "actual output here"


def test_filter_node_warnings_passthrough_when_no_warnings():
    payload = '{"error":"something went wrong"}'
    assert _filter_node_warnings(payload) == payload


def test_filter_node_warnings_returns_empty_when_only_warnings():
    raw = "(node:99) Warning: Some Node.js warning with no real content."
    assert _filter_node_warnings(raw) == ""


def test_filter_node_warnings_preserves_multiline_json():
    raw = (
        "(node:123) Warning: TLS warning.\n"
        "{\n"
        '  "error": "multi-line error"\n'
        "}"
    )
    result = _filter_node_warnings(raw)
    assert "error" in result
    assert "Warning" not in result


def test_filter_node_warnings_does_not_strip_unrelated_parenthesised_text():
    """Lines with (foo) that don't match (node:<pid>) must not be stripped."""
    raw = "(TypeError) some error message"
    assert _filter_node_warnings(raw) == raw


def test_filter_node_warnings_preserves_non_warning_node_prefixed_lines():
    """A (node:<pid>) line that is not a <Name>Warning: must be preserved —
    it may be legitimate user output that happens to use the prefix.
    """
    raw = "(node:42) custom log line from user code\n" '{"error":"real error"}'
    assert _filter_node_warnings(raw) == raw


def test_filter_node_warnings_removes_trace_warnings_hint_line():
    """The companion hint Node prints after the first warning is stripped too."""
    raw = (
        "(node:552) Warning: Setting the NODE_TLS_REJECT_UNAUTHORIZED environment "
        "variable to '0' makes TLS connections and HTTPS requests insecure.\n"
        "(Use `node --trace-warnings ...` to show where the warning was created)\n"
        '{"error":"GitHub API error: 403 Forbidden"}'
    )
    result = _filter_node_warnings(raw)
    assert result == '{"error":"GitHub API error: 403 Forbidden"}'


def test_filter_node_warnings_removes_experimental_warning():
    raw = (
        "(node:9) ExperimentalWarning: The Fetch API is an experimental feature.\n"
        "payload"
    )
    assert _filter_node_warnings(raw) == "payload"


def test_filter_node_warnings_leaves_no_blank_line_for_mid_text_warning():
    """A warning between two payload lines must not leave an empty line."""
    raw = "line one\n(node:7) Warning: mid-stream warning.\nline two"
    assert _filter_node_warnings(raw) == "line one\nline two"


# ---------------------------------------------------------------------------
# _build_sandbox_env — TLS env isolation
# ---------------------------------------------------------------------------


def test_build_sandbox_env_never_sets_node_tls_when_s3_reject_unauthorized_true(
    monkeypatch,
):
    monkeypatch.setattr(settings, "s3_reject_unauthorized", True)
    env = _build_sandbox_env()
    assert "NODE_TLS_REJECT_UNAUTHORIZED" not in env


def test_build_sandbox_env_never_sets_node_tls_when_s3_reject_unauthorized_false(
    monkeypatch,
):
    """Even when S3 TLS verification is disabled, sandbox subprocesses must not
    inherit NODE_TLS_REJECT_UNAUTHORIZED=0 — the bypass is S3-client-specific
    and must never disable TLS verification for tool HTTPS traffic.
    """
    monkeypatch.setattr(settings, "s3_reject_unauthorized", False)
    env = _build_sandbox_env()
    assert "NODE_TLS_REJECT_UNAUTHORIZED" not in env


# ---------------------------------------------------------------------------
# execute_js_tool — warning noise filtered from surfaced errors
# ---------------------------------------------------------------------------


async def test_execute_js_tool_filters_warning_from_structured_error(monkeypatch):
    """When Node.js prepends a warning line before the JSON error, the raised
    RuntimeError must contain only the clean error message.
    """
    from app.services import custom_tool_executor as cte

    warning_line = (
        "(node:552) Warning: Setting the NODE_TLS_REJECT_UNAUTHORIZED environment "
        "variable to '0' makes TLS connections and HTTPS requests insecure.\n"
    )
    json_error = json.dumps({"error": "GitHub API error: 403 Forbidden"})
    fake_stderr = (warning_line + json_error).encode()

    class _FakeProcess:
        returncode = 1

        async def communicate(self, input=None):
            return b"", fake_stderr

    async def fake_exec(*args, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr(settings, "disable_custom_tools", False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="GitHub API error: 403 Forbidden") as exc_info:
        await cte.execute_js_tool("throw new Error('x');", {})

    # The raw Node.js warning must not appear in the exception message.
    assert "NODE_TLS_REJECT_UNAUTHORIZED" not in str(exc_info.value)
    assert "Warning:" not in str(exc_info.value)


async def test_execute_js_tool_docker_filters_warning_from_structured_error(monkeypatch):
    """The Docker path applies the same warning filter on the error path."""
    from app.services import custom_tool_executor as cte

    warning_line = "(node:1) [DEP0040] DeprecationWarning: The punycode module is deprecated.\n"
    json_error = json.dumps({"error": "Container tool failed: boom"})
    fake_stderr = (warning_line + json_error).encode()

    class _FakeProcess:
        returncode = 1

        async def communicate(self, input=None):
            return b"", fake_stderr

    async def fake_exec(*args, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr(settings, "disable_custom_tools", False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="Container tool failed: boom") as exc_info:
        await cte.execute_js_tool_docker("throw new Error('x');", {})

    assert "DeprecationWarning" not in str(exc_info.value)


async def test_execute_js_tool_filters_warning_from_success_output(monkeypatch):
    """On success, Node.js warning lines must not leak into the returned output
    when stdout is empty and stderr contains only warnings.
    """
    from app.services import custom_tool_executor as cte

    fake_stderr = b"(node:1) Warning: Some Node.js warning only."

    class _FakeProcess:
        returncode = 0

        async def communicate(self, input=None):
            return b"", fake_stderr

    async def fake_exec(*args, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr(settings, "disable_custom_tools", False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await cte.execute_js_tool("/* no stdout */", {})
    assert result == "(no output)"
    assert "Warning" not in result
