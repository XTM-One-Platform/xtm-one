"""Regression tests for Microsoft email search query escaping."""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft._outlook import _build_email_search_query
from app.tools.base import CallableToolExecutor, ToolCall


class _StubResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _RequestRecorder:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, provider, op, **kwargs):
        self.calls.append(
            {"method": method, "url": url, "provider": provider, "op": op, **kwargs}
        )
        return _StubResponse({"value": []}), None


def test_build_email_search_query_wraps_literal_phrase():
    assert _build_email_search_query("Quarterly Sync & Review") == '"Quarterly Sync & Review"'


def test_build_email_search_query_quotes_advanced_subject_filter():
    """An AQS field restriction must be wrapped in the outer quotes Graph
    requires (with inner quotes escaped). The ``subject:`` KQL restriction
    still applies inside the quoted value."""
    query = 'subject:"Quarterly Sync & Review"'
    assert (
        _build_email_search_query(query)
        == '"subject:\\"Quarterly Sync & Review\\""'
    )


def test_build_email_search_query_quotes_from_field_address():
    """A ``from:`` filter carrying an email address must
    be wrapped in outer quotes, otherwise Graph rejects the bare
    ``from:user@domain.com`` with HTTP 400 ("character ':' is not valid")."""
    assert (
        _build_email_search_query("from:user@domain.com")
        == '"from:user@domain.com"'
    )


def test_build_email_search_query_is_idempotent_for_already_quoted_input():
    """If the caller already passes the documented Graph form (the whole value
    wrapped in outer double quotes, e.g. ``"from:alice@example.com"``), the
    builder must not double-wrap it — nesting the quotes would make Graph treat
    the value as a literal phrase and drop the ``from:`` field restriction."""
    assert (
        _build_email_search_query('"from:alice@example.com"')
        == '"from:alice@example.com"'
    )
    assert _build_email_search_query('"pizza"') == '"pizza"'
    # A leading quote that does NOT fully wrap the value is still escaped +
    # wrapped (it isn't a balanced already-quoted string).
    assert (
        _build_email_search_query('"alpha" AND "beta"')
        == '"\\"alpha\\" AND \\"beta\\""'
    )
    # A fully-quoted value carrying *escaped* interior quotes (the documented
    # Graph form for a subject filter) is recognised as already-quoted and
    # returned unchanged — not double-wrapped into a literal phrase.
    assert (
        _build_email_search_query('"subject:\\"plan\\""')
        == '"subject:\\"plan\\""'
    )


def test_build_email_search_query_is_idempotent_on_its_own_output():
    """Running the builder on its own output must be a no-op, including for a
    subject filter whose first pass introduces escaped interior quotes. Without
    the escape-aware already-quoted check this second pass double-wrapped the
    value and Graph dropped the ``subject:`` field restriction."""
    for query in (
        "from:alice@example.com",
        'subject:"A & B"',
        "from:alice AND subject:plan",
        "Quarterly Sync & Review",
    ):
        once = _build_email_search_query(query)
        assert _build_email_search_query(once) == once


def test_build_email_search_query_empty_input_returns_empty_quoted_phrase():
    """An empty / whitespace-only query is still wrapped in quotes so
    the resulting ``$search`` syntax stays valid (Graph rejects a bare
    empty ``$search``)."""
    assert _build_email_search_query("") == '""'
    assert _build_email_search_query("   ") == '""'


def test_build_email_search_query_escapes_embedded_quotes_and_backslashes():
    """Embedded ``"`` and ``\\`` must be escaped so they don't break
    out of the wrapping phrase. Both are doubled (``\\\\`` for ``\\``
    and ``\\"`` for ``"``) to match Graph's ``$search`` escaping rules."""
    assert (
        _build_email_search_query('quote " inside')
        == '"quote \\" inside"'
    )
    assert (
        _build_email_search_query("back\\slash")
        == '"back\\\\slash"'
    )


def test_build_email_search_query_wraps_boolean_operators():
    """Boolean AQS operators (AND / OR / NOT) keep working — Graph interprets
    them inside the quoted value — but the value is still wrapped in the outer
    quotes Graph requires. Forwarding them unquoted is rejected with HTTP 400."""
    for query in (
        "alpha AND beta",
        "alpha or beta",
        "alpha NOT beta",
        "(alpha OR beta) AND gamma",
    ):
        assert _build_email_search_query(query) == f'"{query}"'


def test_build_email_search_query_quotes_non_aqs_colon_strings():
    """Strings that look syntactically like ``word:`` but use a word
    Graph does NOT recognise as an AQS keyword (URLs, times, free-form
    notes) must still be wrapped as literal phrases — otherwise Graph
    would reject the unquoted ``$search`` with a 400, defeating the
    point of this helper."""
    cases = [
        "https://example.com/page",
        "https://example.com/q?a=1&b=2",
        "meet at lunch: tomorrow",
        "team: planning sync",
        "12:30 sync notes",
    ]
    for query in cases:
        result = _build_email_search_query(query)
        # The escaped string is the input itself (no embedded quote/backslash
        # in these fixtures) — assert it is wrapped on both sides.
        assert result.startswith('"') and result.endswith('"'), (
            f"non-AQS colon query {query!r} must be wrapped as a phrase; "
            f"got {result!r}"
        )
        assert result == f'"{query}"'


def test_build_email_search_query_quotes_aqs_fields_case_insensitively():
    """AQS field names are case-insensitive per Microsoft Graph; whatever the
    case, the value is wrapped in the outer quotes Graph requires (inner
    quotes escaped)."""
    assert (
        _build_email_search_query('Subject:"Quarterly"')
        == '"Subject:\\"Quarterly\\""'
    )
    assert (
        _build_email_search_query("FROM:alice@example.com")
        == '"FROM:alice@example.com"'
    )
    assert _build_email_search_query("BODY: invoice") == '"BODY: invoice"'


@pytest.mark.asyncio
async def test_search_microsoft_emails_sends_quoted_advanced_query(monkeypatch):
    """A ``from:`` query must reach Graph as a
    quoted ``$search`` value, not the bare ``from:address`` that 400s."""
    recorder = _RequestRecorder()
    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", recorder)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-1",
            name="search_microsoft_emails",
            arguments={"query": "from:alice@example.com"},
        )
    )

    assert not result.is_error
    assert recorder.calls[0]["params"]["$search"] == '"from:alice@example.com"'
