"""Tests for trafilatura log-noise suppression and URL-context injection.

Covers the _TrafilaturaFilter (level downgrade, embedded-URL redaction,
URL-context suffix), _install_trafilatura_filter, and the
_extract_with_trafilatura helper introduced to fix the noisy ERROR-level
log burst.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

import app.services.web_fetch as web_fetch_mod

# ── _TrafilaturaFilter unit tests ────────────────────────────────────────────


def _make_record(
    msg: str,
    *,
    logger_name: str = "trafilatura.core",
    level: int = logging.ERROR,
    args: tuple = (),
) -> logging.LogRecord:
    return logging.LogRecord(
        name=logger_name,
        level=level,
        pathname="",
        lineno=0,
        msg=msg,
        args=args,
        exc_info=None,
    )


class TestTrafilaturaFilterDowngrade:
    """The filter downgrades known ERROR patterns to WARNING."""

    def test_empty_html_tree_downgraded_to_warning(self):
        filt = web_fetch_mod._TrafilaturaFilter()
        record = _make_record("empty HTML tree: None")
        filt.filter(record)
        assert record.levelno == logging.WARNING
        assert record.levelname == "WARNING"

    def test_parsed_tree_length_downgraded_to_warning(self):
        filt = web_fetch_mod._TrafilaturaFilter()
        record = _make_record(
            "parsed tree length: 1, wrong data type or not valid HTML",
            logger_name="trafilatura.utils",
        )
        filt.filter(record)
        assert record.levelno == logging.WARNING
        assert record.levelname == "WARNING"

    def test_wrong_data_type_pattern_downgraded_to_warning(self):
        filt = web_fetch_mod._TrafilaturaFilter()
        record = _make_record("wrong data type", logger_name="trafilatura.utils")
        filt.filter(record)
        assert record.levelno == logging.WARNING

    def test_unrelated_error_not_downgraded(self):
        filt = web_fetch_mod._TrafilaturaFilter()
        record = _make_record("unexpected exception occurred")
        filt.filter(record)
        assert record.levelno == logging.ERROR  # unchanged

    def test_warning_record_not_affected_by_downgrade_logic(self):
        filt = web_fetch_mod._TrafilaturaFilter()
        # A WARNING with the known text is already below ERROR — no change.
        record = _make_record("empty HTML tree", level=logging.WARNING)
        filt.filter(record)
        assert record.levelno == logging.WARNING  # stays WARNING (no escalation)

    def test_filter_always_returns_true(self):
        """The filter never suppresses records; it only transforms them."""
        filt = web_fetch_mod._TrafilaturaFilter()
        for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR):
            record = _make_record("some message", level=level)
            assert filt.filter(record) is True

    def test_malformed_record_passes_through_untouched(self):
        """A record whose msg/args mismatch (getMessage raises) must not raise
        out of the filter — filters run unprotected at the logging call site,
        so an exception here would propagate into the fetch path."""
        filt = web_fetch_mod._TrafilaturaFilter()
        token = web_fetch_mod._traf_current_url.set("https://example.com/")
        try:
            record = _make_record("bad format %s %s", args=("only-one",))
            assert filt.filter(record) is True  # must not raise
            # Record is left untouched for handler-level error handling.
            assert record.levelno == logging.ERROR
            assert record.msg == "bad format %s %s"
            assert record.args == ("only-one",)
        finally:
            web_fetch_mod._traf_current_url.reset(token)


class TestTrafilaturaFilterUrlInjection:
    """The filter appends [url=…] when _traf_current_url is set."""

    def test_url_appended_when_context_set(self):
        filt = web_fetch_mod._TrafilaturaFilter()
        token = web_fetch_mod._traf_current_url.set("https://example.com/page")
        try:
            record = _make_record("empty HTML tree: None")
            filt.filter(record)
            assert "[url=https://example.com/page]" in record.msg
        finally:
            web_fetch_mod._traf_current_url.reset(token)

    def test_no_url_suffix_when_context_not_set(self):
        filt = web_fetch_mod._TrafilaturaFilter()
        token = web_fetch_mod._traf_current_url.set(None)
        try:
            record = _make_record("empty HTML tree: None")
            filt.filter(record)
            assert "[url=" not in record.msg
        finally:
            web_fetch_mod._traf_current_url.reset(token)

    def test_args_cleared_after_url_injection(self):
        """After URL injection record.args is cleared to avoid double-formatting."""
        filt = web_fetch_mod._TrafilaturaFilter()
        token = web_fetch_mod._traf_current_url.set("https://example.com/")
        try:
            record = _make_record("parsed tree length: %d", args=(1,))
            filt.filter(record)
            # args must be empty so handlers don't re-apply % formatting.
            assert record.args == ()
            assert "parsed tree length: 1" in record.msg
        finally:
            web_fetch_mod._traf_current_url.reset(token)

    def test_downgrade_and_url_combined(self):
        """Both downgrade and URL injection happen in a single filter call."""
        filt = web_fetch_mod._TrafilaturaFilter()
        token = web_fetch_mod._traf_current_url.set("https://news.example.org/article")
        try:
            record = _make_record("empty HTML tree: None")
            filt.filter(record)
            assert record.levelno == logging.WARNING
            assert "[url=https://news.example.org/article]" in record.msg
        finally:
            web_fetch_mod._traf_current_url.reset(token)

    def test_double_filter_pass_does_not_duplicate_url_suffix(self):
        """Running the filter twice on one record must not stamp [url=…] twice.

        This is the failure mode a duplicated filter installation would
        produce — the module-level singleton + Logger.addFilter dedupe
        prevent it, and the suffix guard makes it structurally impossible.
        """
        filt = web_fetch_mod._TrafilaturaFilter()
        token = web_fetch_mod._traf_current_url.set("https://example.com/")
        try:
            record = _make_record("empty HTML tree: None")
            filt.filter(record)
            filt.filter(record)
            assert record.msg.count("[url=") == 1
        finally:
            web_fetch_mod._traf_current_url.reset(token)


class TestFilterRedactsEmbeddedUrls:
    """Trafilatura logs the raw ``url=`` argument in its own messages
    (``LOGGER.error("empty HTML tree: %s", url)`` in trafilatura.core) —
    the filter must redact URLs inside the rendered message, not just the
    injected suffix.
    """

    def test_raw_url_in_message_is_redacted(self):
        filt = web_fetch_mod._TrafilaturaFilter()
        record = _make_record(
            "empty HTML tree: %s",
            args=("https://user:pass@ex.com/page?token=sekret",),
        )
        filt.filter(record)
        assert "https://ex.com/page" in record.msg
        assert "sekret" not in record.msg
        assert "user:pass" not in record.msg
        assert record.args == ()

    def test_unparseable_url_replaced_with_placeholder(self):
        # _redact_url_for_log returns None for a malformed port — the filter
        # must drop the URL entirely rather than keep a possibly-sensitive one.
        filt = web_fetch_mod._TrafilaturaFilter()
        record = _make_record(
            "too large: length 5 for URL https://ex.com:99999999/x?token=sekret"
        )
        filt.filter(record)
        assert "[redacted-url]" in record.msg
        assert "sekret" not in record.msg

    def test_message_without_urls_left_untouched(self):
        filt = web_fetch_mod._TrafilaturaFilter()
        record = _make_record("lxml parsing failed: some parser error")
        filt.filter(record)
        assert record.msg == "lxml parsing failed: some parser error"

    def test_uppercase_scheme_url_still_redacted(self):
        # RFC 3986 schemes are case-insensitive — an ``HTTPS://`` URL must
        # not slip through the in-message redaction unredacted.
        filt = web_fetch_mod._TrafilaturaFilter()
        record = _make_record(
            "empty HTML tree: HTTPS://user:pass@ex.com/page?token=sekret"
        )
        filt.filter(record)
        assert "sekret" not in record.msg
        assert "user:pass" not in record.msg
        assert "https://ex.com/page" in record.msg

    def test_second_pass_idempotent_with_redaction_and_suffix(self):
        filt = web_fetch_mod._TrafilaturaFilter()
        token = web_fetch_mod._traf_current_url.set("https://ctx.example.com/page")
        try:
            record = _make_record(
                "empty HTML tree: %s", args=("https://a.com/x?t=1",)
            )
            filt.filter(record)
            first = record.msg
            filt.filter(record)
            assert record.msg == first
            assert record.msg.count("[url=") == 1
            assert "?t=1" not in record.msg
        finally:
            web_fetch_mod._traf_current_url.reset(token)


# ── _install_trafilatura_filter idempotency ──────────────────────────────────


@pytest.fixture
def _clean_traf_loggers():
    """Remove the filter from the trafilatura loggers before and after."""

    def _cleanup():
        for name in web_fetch_mod._TRAF_LOGGER_NAMES:
            logging.getLogger(name).removeFilter(web_fetch_mod._traf_filter)

    _cleanup()
    yield
    _cleanup()


def test_install_filter_is_idempotent(_clean_traf_loggers):
    """Calling _install_trafilatura_filter repeatedly does not duplicate the filter."""
    web_fetch_mod._install_trafilatura_filter()
    web_fetch_mod._install_trafilatura_filter()
    web_fetch_mod._install_trafilatura_filter()

    traf_logger = logging.getLogger("trafilatura")
    assert traf_logger.filters.count(web_fetch_mod._traf_filter) == 1


def test_install_filter_covers_all_trafilatura_loggers(_clean_traf_loggers):
    """The filter is installed on every logger in _TRAF_LOGGER_NAMES."""
    web_fetch_mod._install_trafilatura_filter()
    for name in web_fetch_mod._TRAF_LOGGER_NAMES:
        assert web_fetch_mod._traf_filter in logging.getLogger(name).filters, (
            f"_TrafilaturaFilter not found on {name}"
        )


# ── _extract_with_trafilatura context-var lifecycle ──────────────────────────


def test_extract_sets_and_resets_url_context():
    """_traf_current_url is None before the call and after the call."""
    seen_urls: list[str | None] = []

    def fake_extract(html, **kwargs):
        seen_urls.append(web_fetch_mod._traf_current_url.get())
        return "extracted text"

    assert web_fetch_mod._traf_current_url.get() is None

    with patch.dict("sys.modules", {"trafilatura": MagicMock(extract=fake_extract)}):
        result = web_fetch_mod._extract_with_trafilatura(
            "<html><body>hi</body></html>", url="https://ex.com/"
        )

    assert result == "extracted text"
    assert seen_urls == ["https://ex.com/"]
    # ContextVar must be reset after the call.
    assert web_fetch_mod._traf_current_url.get() is None


def test_extract_resets_url_context_on_exception(caplog):
    """_traf_current_url is reset even when trafilatura.extract raises,
    and the warning log carries the caller URL and the exception text."""

    def boom(html, **kwargs):
        raise RuntimeError("trafilatura exploded")

    assert web_fetch_mod._traf_current_url.get() is None

    with (
        caplog.at_level(logging.WARNING, logger="app.services.web_fetch"),
        patch.dict("sys.modules", {"trafilatura": MagicMock(extract=boom)}),
    ):
        result = web_fetch_mod._extract_with_trafilatura(
            "<html/>", url="https://boom.example.com/"
        )

    assert result == ""  # graceful fallback
    assert web_fetch_mod._traf_current_url.get() is None  # reset even after exception
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "[url=https://boom.example.com/]" in m and "trafilatura exploded" in m
        for m in messages
    ), f"Exception-path warning must include URL and exception text; got: {messages}"


def test_extract_url_none_context_not_set():
    """When url=None the ContextVar is set to None (not some stale value)."""
    seen: list[str | None] = []

    def fake_extract(html, **kwargs):
        seen.append(web_fetch_mod._traf_current_url.get())
        return None

    with patch.dict("sys.modules", {"trafilatura": MagicMock(extract=fake_extract)}):
        web_fetch_mod._extract_with_trafilatura("<html/>")

    assert seen == [None]


def test_extract_installs_filter_lazily(_clean_traf_loggers):
    """_install_trafilatura_filter is called inside _extract_with_trafilatura."""
    with patch.dict(
        "sys.modules",
        {"trafilatura": MagicMock(extract=MagicMock(return_value="ok"))},
    ):
        web_fetch_mod._extract_with_trafilatura("<html/>", url="https://example.com/")

    traf_logger = logging.getLogger("trafilatura")
    assert web_fetch_mod._traf_filter in traf_logger.filters


# ── URL redaction for logs ───────────────────────────────────────────────────


class TestRedactUrlForLog:
    """_redact_url_for_log strips credentials and tokens, keeps host + path."""

    def test_strips_userinfo(self):
        assert (
            web_fetch_mod._redact_url_for_log("https://user:secret@example.com/page")
            == "https://example.com/page"
        )

    def test_strips_query_and_fragment(self):
        assert (
            web_fetch_mod._redact_url_for_log(
                "https://example.com/doc?token=abc123&sig=xyz#section"
            )
            == "https://example.com/doc"
        )

    def test_keeps_port(self):
        assert (
            web_fetch_mod._redact_url_for_log("http://example.com:8080/a/b")
            == "http://example.com:8080/a/b"
        )

    def test_ipv6_host_keeps_brackets(self):
        # urlparse strips the brackets from IPv6 literals; the helper must
        # re-add them so the reconstructed URL stays valid.
        assert (
            web_fetch_mod._redact_url_for_log("https://[2001:db8::1]/path?token=x")
            == "https://[2001:db8::1]/path"
        )

    def test_ipv6_host_with_port_and_userinfo(self):
        assert (
            web_fetch_mod._redact_url_for_log(
                "https://user:pass@[2001:db8::1]:8443/a?k=v#f"
            )
            == "https://[2001:db8::1]:8443/a"
        )

    def test_none_and_empty_return_none(self):
        assert web_fetch_mod._redact_url_for_log(None) is None
        assert web_fetch_mod._redact_url_for_log("") is None

    def test_malformed_port_does_not_raise(self):
        # ``urlparse(...).port`` raises ValueError for out-of-range ports;
        # the helper must swallow it and return None rather than raise on
        # a logging path.
        assert web_fetch_mod._redact_url_for_log("https://example.com:99999999/x") is None

    def test_non_http_scheme_returns_none(self):
        # ``file:///etc/passwd`` would reconstruct into the misleading
        # ``file:/etc/passwd`` and leak a non-web path into logs.
        assert web_fetch_mod._redact_url_for_log("file:///etc/passwd") is None
        assert web_fetch_mod._redact_url_for_log("ftp://host/file") is None
        assert web_fetch_mod._redact_url_for_log("javascript:alert(1)") is None

    def test_missing_host_returns_none(self):
        # ``https:///path`` (empty netloc) would reconstruct into the
        # invalid ``https:/path``.
        assert web_fetch_mod._redact_url_for_log("https:///path") is None
        assert web_fetch_mod._redact_url_for_log("https://") is None

    def test_relative_and_scheme_relative_urls_return_none(self):
        # No scheme at all — not an absolute http(s) URL.
        assert web_fetch_mod._redact_url_for_log("/just/a/path") is None
        assert web_fetch_mod._redact_url_for_log("example.com/page?t=1") is None
        assert web_fetch_mod._redact_url_for_log("//example.com/page") is None


def test_extract_stores_redacted_url_in_context():
    """The ContextVar holds the redacted URL, so no filter can leak tokens."""
    seen: list[str | None] = []

    def fake_extract(html, **kwargs):
        seen.append(web_fetch_mod._traf_current_url.get())
        return "ok"

    with patch.dict("sys.modules", {"trafilatura": MagicMock(extract=fake_extract)}):
        web_fetch_mod._extract_with_trafilatura(
            "<html/>", url="https://user:pass@ex.com/page?api_key=sekret#frag"
        )

    assert seen == ["https://ex.com/page"]


def test_extract_exception_warning_is_redacted(caplog):
    """The exception-path warning must not contain userinfo or query tokens."""

    def boom(html, **kwargs):
        raise RuntimeError("kaboom")

    with (
        caplog.at_level(logging.WARNING, logger="app.services.web_fetch"),
        patch.dict("sys.modules", {"trafilatura": MagicMock(extract=boom)}),
    ):
        web_fetch_mod._extract_with_trafilatura(
            "<html/>", url="https://user:pass@boom.example.com/x?token=sekret"
        )

    messages = [r.getMessage() for r in caplog.records]
    assert any("[url=https://boom.example.com/x]" in m for m in messages), messages
    joined = "\n".join(messages)
    assert "sekret" not in joined
    assert "user:pass" not in joined
