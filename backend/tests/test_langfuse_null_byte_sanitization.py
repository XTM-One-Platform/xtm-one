"""Tests for null-byte sanitization and diagnostic logging filter.

Covers:
- ``strip_null_bytes`` (shared helper in ``core/utils.py``): recursive removal
  of ``\\u0000`` from nested payloads, copy-on-write for the clean case, and
  single-source-of-truth identity across the local PG sink and the Langfuse
  emitters.
- ``_sanitize_for_langfuse``: combined media + null-byte sanitization pipeline.
- ``end_trace`` / ``end_span`` / retry observer: null bytes stripped from the
  final output, error status messages, and retry event status messages.
- ``_LangfuseBadRequestFilter``: enriches Langfuse SDK "Bad request" error
  records with a diagnostic hint so on-call engineers have immediate direction.
- ``_install_langfuse_bad_request_filter``: idempotent filter installation.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from app.services.langfuse_tracing import (
    _install_langfuse_bad_request_filter,
    _LangfuseBadRequestFilter,
    _sanitize_for_langfuse,
    _strip_null_bytes,
    end_span,
    end_trace,
    make_retry_attempt_observer,
)

# ---------------------------------------------------------------------------
# _strip_null_bytes
# ---------------------------------------------------------------------------


class TestStripNullBytes:
    def test_plain_string_without_null(self):
        assert _strip_null_bytes("hello world") == "hello world"

    def test_plain_string_with_null(self):
        assert _strip_null_bytes("hel\u0000lo") == "hello"

    def test_string_only_null(self):
        assert _strip_null_bytes("\u0000\u0000") == ""

    def test_empty_string(self):
        assert _strip_null_bytes("") == ""

    def test_none_passthrough(self):
        assert _strip_null_bytes(None) is None

    def test_int_passthrough(self):
        assert _strip_null_bytes(42) == 42

    def test_bool_passthrough(self):
        assert _strip_null_bytes(True) is True

    def test_flat_dict(self):
        result = _strip_null_bytes({"key": "val\u0000ue", "other": "clean"})
        assert result == {"key": "value", "other": "clean"}

    def test_dict_with_null_in_key(self):
        result = _strip_null_bytes({"k\u0000ey": "value"})
        assert result == {"key": "value"}

    def test_nested_dict(self):
        data = {"outer": {"inner": "text\u0000with\u0000nulls"}}
        result = _strip_null_bytes(data)
        assert result == {"outer": {"inner": "textwithnulls"}}

    def test_list_of_strings(self):
        result = _strip_null_bytes(["a\u0000b", "c\u0000d"])
        assert result == ["ab", "cd"]

    def test_list_within_dict(self):
        data = {"messages": ["hello\u0000", "world\u0000"]}
        result = _strip_null_bytes(data)
        assert result == {"messages": ["hello", "world"]}

    def test_tuple_treated_as_list(self):
        result = _strip_null_bytes(("a\u0000", "b"))
        assert result == ["a", "b"]

    def test_deeply_nested_structure(self):
        """GitHub GraphQL responses can have deeply nested objects."""
        data = {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": [
                            {"title": "Issue\u0000 title", "body": "Body\u0000\u0000text"},
                            {"title": "Clean title", "body": "Clean body"},
                        ]
                    }
                }
            }
        }
        result = _strip_null_bytes(data)
        assert result["data"]["repository"]["issues"]["nodes"][0] == {
            "title": "Issue title",
            "body": "Bodytext",
        }
        assert result["data"]["repository"]["issues"]["nodes"][1] == {
            "title": "Clean title",
            "body": "Clean body",
        }

    def test_no_copy_when_no_null_bytes(self):
        """Identity check: a string without null bytes is returned as the same
        object (no unnecessary copy)."""
        s = "clean string"
        assert _strip_null_bytes(s) is s

    def test_no_copy_for_clean_containers(self):
        """Copy-on-write: clean dicts and lists are returned as the same
        object — no allocations on the hot clean-case path."""
        data = {"messages": [{"role": "user", "content": "clean"}], "count": 2}
        assert _strip_null_bytes(data) is data
        items = ["a", "b", {"k": "v"}]
        assert _strip_null_bytes(items) is items

    def test_dirty_containers_are_rebuilt(self):
        """A container with a null byte anywhere is rebuilt (original intact)."""
        data = {"outer": ["clean", "dir\u0000ty"]}
        result = _strip_null_bytes(data)
        assert result is not data
        assert result == {"outer": ["clean", "dirty"]}
        assert data["outer"][1] == "dir\u0000ty"  # input not mutated

    def test_non_string_dict_keys_stay_hashable(self):
        """Non-string dict keys pass through unchanged — sanitization must
        never turn a hashable key (e.g. a tuple) into an unhashable list."""
        data = {1: "a\u0000b", (2, 3): "x", "k": "clean"}
        result = _strip_null_bytes(data)
        assert result == {1: "ab", (2, 3): "x", "k": "clean"}
        assert (2, 3) in result

    def test_late_change_preserves_prefix(self):
        """Copy-on-write with the first change deep in the container keeps
        the already-verified prefix intact."""
        data = ["a", "b", "c", "d\u0000"]
        result = _strip_null_bytes(data)
        assert result == ["a", "b", "c", "d"]
        nested = {"first": "clean", "second": "also clean", "third": "no\u0000pe"}
        assert _strip_null_bytes(nested) == {
            "first": "clean",
            "second": "also clean",
            "third": "nope",
        }

    def test_single_source_of_truth(self):
        """The local PG sink and the Langfuse emitters share one helper."""
        from app.core.utils import strip_null_bytes
        from app.services.tracing import local_sink

        assert _strip_null_bytes is strip_null_bytes
        assert local_sink._strip_null_bytes is strip_null_bytes


# ---------------------------------------------------------------------------
# _sanitize_for_langfuse
# ---------------------------------------------------------------------------


class TestSanitizeForLangfuse:
    def test_strips_null_bytes_from_string(self):
        assert _sanitize_for_langfuse("text\u0000with\u0000nulls") == "textwithnulls"

    def test_strips_null_bytes_from_nested_dict(self):
        data = {"role": "assistant", "content": "Hello\u0000 world"}
        result = _sanitize_for_langfuse(data)
        assert result["content"] == "Hello world"

    def test_strips_base64_image(self):
        data = "data:image/png;base64,iVBORw0KGgo="
        result = _sanitize_for_langfuse(data)
        assert result == "[base64 image]"

    def test_strips_image_url_content_part(self):
        data = {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}}
        result = _sanitize_for_langfuse(data)
        assert result == {"type": "text", "text": "[image]"}

    def test_combined_media_and_null_bytes(self):
        """A message list with both image_url parts and null bytes in text."""
        data = [
            {"role": "user", "content": "Hello\u0000 world"},
            {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
        ]
        result = _sanitize_for_langfuse(data)
        assert result[0]["content"] == "Hello world"
        assert result[1] == {"type": "text", "text": "[image]"}

    def test_null_bytes_in_non_content_dict_values(self):
        """Non-content/non-image_url dict values containing null bytes are
        sanitized by _strip_null_bytes applied after _strip_media_content."""
        data = {
            "role": "tool",
            "tool_call_id": "call_abc\u0000def",
            "name": "search\u0000",
            "content": "result",
        }
        result = _sanitize_for_langfuse(data)
        assert result["tool_call_id"] == "call_abcdef"
        assert result["name"] == "search"
        assert result["content"] == "result"

    def test_none_returns_none(self):
        assert _sanitize_for_langfuse(None) is None

    def test_empty_list(self):
        assert _sanitize_for_langfuse([]) == []

    def test_passthrough_clean_data(self):
        """Clean data passes through unchanged (aside from possible container copy)."""
        data = {"role": "assistant", "content": "No special chars here."}
        result = _sanitize_for_langfuse(data)
        assert result == data


# ---------------------------------------------------------------------------
# Emission-point coverage: end_trace / end_span / retry observer
# ---------------------------------------------------------------------------


class _FakeSpan:
    """Records the calls Langfuse SDK spans receive from the tracer."""

    def __init__(self) -> None:
        self.trace_id = "trace-1"
        self.id = "span-1"
        self.updates: list[dict] = []
        self.trace_io: list[dict] = []
        self.events: list[dict] = []
        self.ended = False

    def update(self, **kwargs) -> None:
        self.updates.append(kwargs)

    def set_trace_io(self, **kwargs) -> None:
        self.trace_io.append(kwargs)

    def create_event(self, **kwargs) -> None:
        self.events.append(kwargs)

    def end(self) -> None:
        self.ended = True


class TestEndTraceSanitization:
    """The final output is the assistant answer, which routinely quotes tool
    results verbatim — the exact null-byte vector."""

    def test_top_level_output_sanitized(self):
        span = _FakeSpan()
        ctx = {"trace": span, "generation": None, "parent_ctx": None}
        with patch("app.services.tracing.dispatcher.submit_trace_for_flush"):
            end_trace(ctx, output="result: iss\u0000ue body")
        assert span.trace_io, "expected set_trace_io to be called"
        assert span.trace_io[0]["output"] == "result: issue body"
        assert span.ended is True

    def test_sub_agent_output_sanitized(self):
        span = _FakeSpan()
        ctx = {"trace": span, "generation": None, "parent_ctx": {"trace": None}}
        with patch("app.services.tracing.dispatcher.submit_trace_for_flush"):
            end_trace(ctx, output={"content": "x\u0000y"})
        outputs = [u["output"] for u in span.updates if "output" in u]
        assert outputs == [{"content": "xy"}]

    def test_error_sanitized(self):
        span = _FakeSpan()
        ctx = {"trace": span, "generation": None, "parent_ctx": None}
        with patch("app.services.tracing.dispatcher.submit_trace_for_flush"):
            end_trace(ctx, error="boom\u0000!")
        assert span.trace_io[0]["output"] == "ERROR: boom!"
        status_messages = [
            u["status_message"] for u in span.updates if "status_message" in u
        ]
        assert status_messages == ["boom!"]


class TestEndSpanErrorSanitization:
    def test_error_status_message_sanitized(self):
        """Exception text can embed HTTP response bodies (tool results) that
        carry null bytes — the status message must be stripped too."""
        span = _FakeSpan()
        ctx = {"trace": span, "generation": None, "parent_ctx": None}
        end_span(ctx, error="HTTP 502: bad\u0000gateway body")
        status_messages = [
            u["status_message"] for u in span.updates if "status_message" in u
        ]
        assert status_messages == ["HTTP 502: badgateway body"]


class TestRetryObserverSanitization:
    def test_retry_event_status_message_sanitized(self):
        span = _FakeSpan()
        observer = make_retry_attempt_observer({"trace": span})
        assert observer is not None
        observer(0, Exception("overloaded\u0000 response"), 1.5)
        assert span.events, "expected create_event to be called"
        assert span.events[0]["status_message"] == "Exception: overloaded response"


# ---------------------------------------------------------------------------
# _LangfuseBadRequestFilter
# ---------------------------------------------------------------------------


class TestLangfuseBadRequestFilter:
    def _make_record(self, msg: str, level: int = logging.ERROR) -> logging.LogRecord:
        record = logging.LogRecord(
            name="langfuse",
            level=level,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )
        return record

    def test_passes_all_records(self):
        """The filter never suppresses records — it always returns True."""
        filt = _LangfuseBadRequestFilter()
        record = self._make_record("Some SDK message", level=logging.DEBUG)
        assert filt.filter(record) is True

    def test_non_error_bad_request_not_enriched(self):
        """Only ERROR-level records with 'Bad request' are enriched."""
        filt = _LangfuseBadRequestFilter()
        record = self._make_record("Bad request occurred", level=logging.WARNING)
        filt.filter(record)
        # Should not be enriched (wrong log level)
        assert "Hint:" not in record.getMessage()

    def test_error_without_bad_request_not_enriched(self):
        """An ERROR-level record without 'Bad request' is not enriched."""
        filt = _LangfuseBadRequestFilter()
        record = self._make_record("Something else went wrong", level=logging.ERROR)
        filt.filter(record)
        assert "Hint:" not in record.getMessage()

    def test_error_with_bad_request_is_enriched(self):
        """An ERROR-level record containing 'Bad request' gets the diagnostic hint."""
        filt = _LangfuseBadRequestFilter()
        msg = (
            "API errors occurred: Bad request. Please check your request for "
            "any missing or incorrect parameters. Refer to our API docs: "
            "https://api.reference.langfuse.com for details."
        )
        record = self._make_record(msg, level=logging.ERROR)
        filt.filter(record)
        enriched = record.getMessage()
        assert "Bad request" in enriched
        assert "Hint:" in enriched
        assert "null bytes" in enriched

    def test_enriched_record_args_cleared(self):
        """After enrichment, ``record.args`` must be empty so the already-
        formatted ``record.msg`` is not re-expanded by the log handler."""
        filt = _LangfuseBadRequestFilter()
        record = logging.LogRecord(
            name="langfuse",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="API errors occurred: Bad request %s",
            args=("extra",),
            exc_info=None,
        )
        filt.filter(record)
        assert record.args == ()
        # getMessage should not raise and should contain the original message
        assert "Bad request" in record.getMessage()

    def test_critical_level_also_enriched(self):
        """CRITICAL is higher than ERROR — it should also be enriched."""
        filt = _LangfuseBadRequestFilter()
        record = self._make_record("Bad request from SDK", level=logging.CRITICAL)
        filt.filter(record)
        assert "Hint:" in record.getMessage()


# ---------------------------------------------------------------------------
# _install_langfuse_bad_request_filter (idempotency)
# ---------------------------------------------------------------------------


class TestInstallLangfuseBadRequestFilter:
    def test_filter_installed_on_langfuse_logger(self):
        """The filter must be present on the 'langfuse' SDK logger after install."""
        sdk_logger = logging.getLogger("langfuse")
        # Ensure at least one is installed (module already calls this at load time)
        _install_langfuse_bad_request_filter()
        installed = [f for f in sdk_logger.filters if isinstance(f, _LangfuseBadRequestFilter)]
        assert len(installed) >= 1, "Expected _LangfuseBadRequestFilter on 'langfuse' logger"

    def test_idempotent_install(self):
        """Calling install multiple times must not add duplicate filters."""
        sdk_logger = logging.getLogger("langfuse")
        # Start from a clean state for this test
        sdk_logger.filters = [f for f in sdk_logger.filters if not isinstance(f, _LangfuseBadRequestFilter)]

        _install_langfuse_bad_request_filter()
        _install_langfuse_bad_request_filter()
        _install_langfuse_bad_request_filter()

        installed = [f for f in sdk_logger.filters if isinstance(f, _LangfuseBadRequestFilter)]
        assert len(installed) == 1, "Expected exactly one _LangfuseBadRequestFilter after repeated installs"

    def test_filter_is_active_at_module_load_time(self):
        """The filter must already be installed when the module is imported
        (module-level call in langfuse_tracing.py), so it catches SDK errors
        that occur before any Langfuse client is initialized."""
        sdk_logger = logging.getLogger("langfuse")
        installed = [f for f in sdk_logger.filters if isinstance(f, _LangfuseBadRequestFilter)]
        assert installed, (
            "_LangfuseBadRequestFilter must be installed at module load time "
            "so it intercepts early SDK errors"
        )
