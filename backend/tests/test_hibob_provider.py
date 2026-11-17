"""Tests for HiBob integration request payloads."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from app.integrations.providers import hibob
from app.tools.base import CallableToolExecutor, ToolCall


class _FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        content: bytes = b'{"ok":true}',
        json_error: Exception | None = None,
        headers: dict[str, str] | None = None,
        text: str = "",
        status_code: int = 200,
    ):
        self._payload = payload
        self.content = content
        self._json_error = json_error
        self.headers = headers or {}
        self.text = text
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


def test_resolve_employee_by_name_avoids_unsupported_filter(monkeypatch):
    payloads: list[dict[str, Any]] = []

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        payloads.append(kwargs["json"])
        return (
            _FakeResponse(
                {
                    "employees": [
                        {
                            "id": "emp-123",
                            "displayName": "David Loiret",
                            "email": "david.loiret@example.com",
                        }
                    ]
                }
            ),
            "",
        )

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    employee_id, err = asyncio.run(hibob._resolve_employee_id({}, "David Loiret"))

    assert err == ""
    assert employee_id == "emp-123"
    assert "filters" not in payloads[0]
    assert payloads[0]["limit"] == hibob._NAME_LOOKUP_LIMIT


def test_resolve_employee_by_email_uses_supported_root_email_filter(monkeypatch):
    payloads: list[dict[str, Any]] = []

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        payloads.append(kwargs["json"])
        return (
            _FakeResponse({"employees": [{"id": "emp-456"}]}),
            "",
        )

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    employee_id, err = asyncio.run(
        hibob._resolve_employee_id({}, "alexis.zubiolo@example.com")
    )

    assert err == ""
    assert employee_id == "emp-456"
    assert payloads[0]["filters"] == [
        {
            "fieldPath": "root.email",
            "operator": "equals",
            "values": ["alexis.zubiolo@example.com"],
        }
    ]


def _make_employee_response(employees: list[dict[str, Any]]) -> Any:
    return _FakeResponse({"employees": employees})


def test_search_hibob_employees_query_email_uses_api_filter(monkeypatch):
    """When query contains '@', it should be forwarded as an email filter."""
    payloads: list[dict[str, Any]] = []

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        payloads.append(kwargs["json"])
        return (
            _make_employee_response(
                [{"id": "emp-1", "displayName": "Alice Smith", "email": "alice@example.com"}]
            ),
            "",
        )

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(id="1", name="search_hibob_employees", arguments={"query": "alice@example.com"})
        )
    )

    assert not result.is_error
    assert payloads[0]["filters"] == [
        {"fieldPath": "root.email", "operator": "equals", "values": ["alice@example.com"]}
    ]
    assert "Alice Smith" in result.content


def test_search_hibob_employees_query_name_filters_client_side(monkeypatch):
    """When query has no '@', results should be filtered client-side by display name."""
    payloads: list[dict[str, Any]] = []

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        payloads.append(kwargs["json"])
        return (
            _make_employee_response(
                [
                    {"id": "emp-1", "displayName": "Alice Smith", "email": "alice@example.com"},
                    {"id": "emp-2", "displayName": "Bob Jones", "email": "bob@example.com"},
                ]
            ),
            "",
        )

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(id="2", name="search_hibob_employees", arguments={"query": "Alice"})
        )
    )

    assert not result.is_error
    # Exactly one API call should be made (no extra round-trips)
    assert len(payloads) == 1
    # No API-level filter should be set for name queries
    assert "filters" not in payloads[0]
    assert payloads[0]["showInactive"] is False
    assert payloads[0]["humanReadable"] == "REPLACE"
    assert payloads[0]["limit"] == hibob._NAME_LOOKUP_LIMIT
    # Only the matching employee should appear in the result
    assert "Alice Smith" in result.content
    assert "Bob Jones" not in result.content


def test_search_hibob_employees_query_no_match_returns_empty_message(monkeypatch):
    """When name query matches no employees, a clear message is returned."""

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        return (
            _make_employee_response(
                [{"id": "emp-2", "displayName": "Bob Jones", "email": "bob@example.com"}]
            ),
            "",
        )

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(id="3", name="search_hibob_employees", arguments={"query": "Alice"})
        )
    )

    assert not result.is_error
    assert "No employees found" in result.content


def test_search_hibob_employees_query_name_no_match_at_limit_returns_hint(monkeypatch):
    """When a name query matches nothing and the API returned _NAME_LOOKUP_LIMIT records, hint about the cap."""

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        # Return exactly _NAME_LOOKUP_LIMIT employees — none matching "Alice"
        employees = [
            {"id": f"emp-{i}", "displayName": f"Bob Employee {i}", "email": f"bob{i}@example.com"}
            for i in range(hibob._NAME_LOOKUP_LIMIT)
        ]
        return _make_employee_response(employees), ""

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(id="4", name="search_hibob_employees", arguments={"query": "Alice"})
        )
    )

    assert not result.is_error
    # Should include the cap-hint message, not just the generic "No employees found"
    assert "first" in result.content and str(hibob._NAME_LOOKUP_LIMIT) in result.content
    assert "exact email or employee ID" in result.content


def test_search_hibob_employees_query_name_matches_despite_whitespace_noise(
    monkeypatch,
):
    """Regression for a false negative.

    Before the fix: the query was normalised via
    ``" ".join(query.strip().split()).casefold()`` (collapses whitespace
    runs) but the candidate display name was only ``.casefold()``'d.
    An upstream display name like ``"Alice   Smith"`` (multiple ASCII
    spaces) or ``"Alice\u00a0Smith"`` (non-breaking space) would fail
    to match the perfectly reasonable query ``"Alice Smith"``.

    Fix routes both sides through ``_normalize_for_name_match`` so the
    substring check operates on the same canonical form.
    """

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        return (
            _make_employee_response(
                [
                    # Multiple ASCII spaces between the first and last
                    # name — the original ``.casefold()``-only candidate
                    # path would have kept this as ``"alice   smith"``
                    # and the substring ``"alice smith"`` would have
                    # failed to match.
                    {
                        "id": "emp-1",
                        "displayName": "Alice   Smith",
                        "email": "alice@example.com",
                    },
                    # Non-breaking space between names — same trap, a
                    # different code point.  ``str.split()`` without
                    # arguments handles this correctly.
                    {
                        "id": "emp-2",
                        "displayName": "Bob\u00a0Jones",
                        "email": "bob@example.com",
                    },
                ]
            ),
            "",
        )

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)

    # Query with a single space — must match the multi-space display name.
    alice_result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="ws-1",
                name="search_hibob_employees",
                arguments={"query": "Alice Smith"},
            )
        )
    )
    assert not alice_result.is_error
    assert "Alice" in alice_result.content
    assert "Bob" not in alice_result.content

    # Query that should match the non-breaking-space display name.
    bob_result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="ws-2",
                name="search_hibob_employees",
                arguments={"query": "Bob Jones"},
            )
        )
    )
    assert not bob_result.is_error
    assert "Bob" in bob_result.content
    assert "Alice" not in bob_result.content


def test_normalize_for_name_match_helper():
    """Direct unit test on the normalisation helper so a future change
    that drops the ``.split()`` or ``.casefold()`` is caught here
    instead of only via the integration tests above.
    """
    assert hibob._normalize_for_name_match("Alice Smith") == "alice smith"
    assert hibob._normalize_for_name_match("Alice   Smith") == "alice smith"
    assert hibob._normalize_for_name_match("  Alice  \tSmith  ") == "alice smith"
    assert hibob._normalize_for_name_match("Alice\u00a0Smith") == "alice smith"
    assert hibob._normalize_for_name_match("") == ""
    assert hibob._normalize_for_name_match(None) == ""


def test_search_job_profiles_always_sends_fields(monkeypatch):
    payloads: list[dict[str, Any]] = []

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        payloads.append(kwargs["json"])
        return (_FakeResponse({"profiles": []}), "")

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("service-user||api-token", executor)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="1",
                name="search_hibob_job_profiles",
                arguments={"status": "active"},
            )
        )
    )

    assert result.content == "No job profiles found."
    assert payloads[0]["fields"] == [
        "jobProfile.id",
        "jobProfile.title",
        "jobProfile.status",
    ]
    assert payloads[0]["filters"] == {"status": "active"}


def test_resolve_employee_by_name_disambiguates_multiple_matches(monkeypatch):
    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        return (
            _FakeResponse(
                {
                    "employees": [
                        {
                            "id": "emp-1",
                            "displayName": "Alexis Zubiolo",
                            "email": "alexis.zubiolo@example.com",
                        },
                        {
                            "id": "emp-2",
                            "displayName": "Alexis Martin",
                            "email": "alexis.martin@example.com",
                        },
                    ]
                }
            ),
            "",
        )

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    employee_id, err = asyncio.run(hibob._resolve_employee_id({}, "Alexis"))

    assert employee_id == ""
    assert "Multiple employees found matching 'Alexis'" in err
    assert "Alexis Zubiolo (alexis.zubiolo@example.com) [ID: emp-1]" in err
    assert "Alexis Martin (alexis.martin@example.com) [ID: emp-2]" in err


def test_resolve_employee_handles_slash_path_root_id_keys(monkeypatch):
    """HiBob may return employee identifiers under slash-path keys
    (e.g. ``"/root/id"``) when ``humanReadable: 'REPLACE'`` is in effect or
    when requested ``fields`` are echoed back as slash-path paths instead
    of nested dicts. ``_extract_field('root.id', ...)`` must follow the same
    convention used elsewhere in the module for ``root.email`` /
    ``root.displayName`` so we don't discard otherwise-valid employees."""

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        return (
            _FakeResponse(
                {
                    "employees": [
                        {
                            "/root/id": "emp-slash-1",
                            "/root/displayName": "Alexis Zubiolo",
                            "/root/email": "alexis.zubiolo@example.com",
                        }
                    ]
                }
            ),
            "",
        )

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    name_id, name_err = asyncio.run(
        hibob._resolve_employee_id({}, "Alexis Zubiolo")
    )
    assert name_err == ""
    assert name_id == "emp-slash-1"

    email_id, email_err = asyncio.run(
        hibob._resolve_employee_id({}, "alexis.zubiolo@example.com")
    )
    assert email_err == ""
    assert email_id == "emp-slash-1"


def test_resolve_employee_handles_human_readable_section(monkeypatch):
    """HiBob also returns requested fields inside a ``humanReadable``
    sub-object when ``humanReadable: 'APPEND'`` is requested. The third
    lookup branch in ``_extract_field`` must still surface the ID for
    both lookup paths."""

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        return (
            _FakeResponse(
                {
                    "employees": [
                        {
                            "humanReadable": {
                                "id": "emp-hr-9",
                                "displayName": "Alexis Zubiolo",
                                "email": "alexis.zubiolo@example.com",
                            }
                        }
                    ]
                }
            ),
            "",
        )

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    name_id, name_err = asyncio.run(
        hibob._resolve_employee_id({}, "Alexis Zubiolo")
    )
    assert name_err == ""
    assert name_id == "emp-hr-9"

    email_id, email_err = asyncio.run(
        hibob._resolve_employee_id({}, "alexis.zubiolo@example.com")
    )
    assert email_err == ""
    assert email_id == "emp-hr-9"


def _run_timeoff_balance_with_response(
    monkeypatch, response: "_FakeResponse"
) -> str:
    """Wire ``response`` through the HiBob client and run
    ``get_hibob_timeoff_balance`` once, returning the tool's textual
    output.  Centralises the fake client / monkeypatch / executor
    boilerplate that every degenerate-response regression test would
    otherwise duplicate, so adding new failure-mode tests later is a
    one-liner."""

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        # Match the real ``app.integrations.http.safe_request`` contract
        # ``(httpx.Response, None)`` on success.  Older fakes in this file
        # return ``(response, "")`` for historical reasons; the provider
        # checks ``if err:`` so both shapes work in practice, but new
        # tests use the production-accurate ``None`` so any future
        # ``err is None`` assertion in the call path is exercised
        # correctly.
        return (response, None)

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("service-user||api-token", executor)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="1",
                name="get_hibob_timeoff_balance",
                arguments={"employee_id": "emp-123", "policy_type": "Holiday"},
            )
        )
    )
    return result.content


def test_get_timeoff_balance_handles_empty_response_body(monkeypatch):
    content = _run_timeoff_balance_with_response(
        monkeypatch, _FakeResponse({}, content=b"")
    )
    assert (
        "Error: HiBob API returned an empty response while fetching time-off balance"
        in content
    )


def test_get_timeoff_balance_handles_invalid_json_response(monkeypatch):
    content = _run_timeoff_balance_with_response(
        monkeypatch,
        _FakeResponse(
            {},
            content=b"not-json",
            json_error=json.JSONDecodeError("Expecting value", "not-json", 0),
        ),
    )
    assert (
        "Error: HiBob API returned an invalid response while fetching time-off balance"
        in content
    )


def test_get_timeoff_balance_handles_api_error_message_field(monkeypatch):
    """HiBob occasionally returns HTTP 2xx with a JSON error object instead of
    the expected balance schema.  The tool must surface the error message and
    not fall back to a raw JSON dump that the LLM may silently retry."""
    content = _run_timeoff_balance_with_response(
        monkeypatch,
        _FakeResponse(
            {"errorMessage": "Employee not found"},
            content=b'{"errorMessage":"Employee not found"}',
        ),
    )
    assert "Error: HiBob returned an error for employee" in content
    assert "Employee not found" in content


def test_get_timeoff_balance_handles_api_message_field(monkeypatch):
    """``message`` is the other common error-key shape in HiBob 2xx error
    bodies; it must be detected and returned as a clear error string."""
    content = _run_timeoff_balance_with_response(
        monkeypatch,
        _FakeResponse(
            {"message": "Invalid policy type"},
            content=b'{"message":"Invalid policy type"}',
        ),
    )
    assert "Error: HiBob returned an error for employee" in content
    assert "Invalid policy type" in content


def test_get_timeoff_balance_handles_api_error_string_field(monkeypatch):
    """``error`` (as a plain string) is the third error-key shape detected in
    HiBob 2xx error bodies; it must be surfaced like ``errorMessage`` and
    ``message`` so this branch cannot silently regress."""
    content = _run_timeoff_balance_with_response(
        monkeypatch,
        _FakeResponse(
            {"error": "Access denied"},
            content=b'{"error":"Access denied"}',
        ),
    )
    assert "Error: HiBob returned an error for employee" in content
    assert "Access denied" in content


def test_get_timeoff_balance_ignores_non_string_error_field(monkeypatch):
    """A structured (non-string) ``error`` value must not be flattened into
    the error message — the unrecognized payload falls through to the raw
    JSON dump so no information is lost."""
    content = _run_timeoff_balance_with_response(
        monkeypatch,
        _FakeResponse(
            {"error": {"code": 42}, "detail": "x"},
            content=b'{"error":{"code":42},"detail":"x"}',
        ),
    )
    assert "Error: HiBob returned an error for employee" not in content
    assert '"code": 42' in content


def test_get_timeoff_balance_prefers_balance_result_over_benign_message(monkeypatch):
    """A valid BalanceResult that also carries an informational ``message``
    key must be rendered as a balance, not misclassified as an API error
    (the error-key detection only applies to unrecognized payload shapes)."""
    content = _run_timeoff_balance_with_response(
        monkeypatch,
        _FakeResponse(
            {
                "totalBalanceAsOfDate": 12.5,
                "totalRoundedBalanceAsOfDate": 12,
                "pointInTime": "2026-01-01",
                "message": "Balance computed from policy snapshot",
            },
            content=b'{"totalBalanceAsOfDate":12.5}',
        ),
    )
    assert "Time-off balance for employee emp-123" in content
    assert "12" in content
    assert "Error: HiBob returned an error" not in content


def test_get_timeoff_balance_handles_empty_dict_response(monkeypatch):
    """An empty JSON object ``{}`` returned by the HiBob API must yield a
    clear terminal error instead of the uninformative ``'{}'`` string that
    would leave the agent uncertain whether to retry."""
    content = _run_timeoff_balance_with_response(
        monkeypatch,
        _FakeResponse({}, content=b"{}"),
    )
    assert "Error: HiBob returned no time-off balance data for employee" in content
    assert "policy" in content


def _run_custom_table_entries_with_response(
    monkeypatch, response: "_FakeResponse"
) -> str:
    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        return (response, None)

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("service-user||api-token", executor)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="1",
                name="get_hibob_custom_table_entries",
                arguments={"employee_id": "emp-123", "table_id": "work-history"},
            )
        )
    )
    return result.content


def test_get_custom_table_entries_handles_empty_response_body(monkeypatch):
    content = _run_custom_table_entries_with_response(
        monkeypatch, _FakeResponse({}, content=b"")
    )
    assert (
        "Error: HiBob API returned an empty response while fetching custom table work-history entries for employee emp-123."
        in content
    )


def test_get_custom_table_entries_handles_invalid_json_response(monkeypatch):
    content = _run_custom_table_entries_with_response(
        monkeypatch,
        _FakeResponse(
            {},
            content=b"not-json",
            json_error=json.JSONDecodeError("Expecting value", "not-json", 0),
        ),
    )
    assert (
        "Error: HiBob API returned an invalid response while fetching custom table work-history entries for employee emp-123."
        in content
    )


# ---------------------------------------------------------------------------
# Direct unit coverage for the shared `_safe_parse_json` helper so future
# adoption across the rest of the provider can rely on the contract that
# regression tests pin down here (empty body, invalid JSON, happy path).
# ---------------------------------------------------------------------------


def test_safe_parse_json_returns_data_on_valid_response():
    data, err = hibob._safe_parse_json(
        _FakeResponse({"foo": 1}, content=b'{"foo":1}'),
        "fetching foo",
    )
    assert err is None
    assert data == {"foo": 1}


def test_safe_parse_json_returns_error_on_empty_body():
    data, err = hibob._safe_parse_json(
        _FakeResponse({}, content=b""), "fetching bar"
    )
    assert data is None
    assert err == "Error: HiBob API returned an empty response while fetching bar."


def test_safe_parse_json_returns_error_on_invalid_json():
    data, err = hibob._safe_parse_json(
        _FakeResponse(
            {},
            content=b"not-json",
            json_error=json.JSONDecodeError("Expecting value", "not-json", 0),
        ),
        "fetching baz",
    )
    assert data is None
    assert err == "Error: HiBob API returned an invalid response while fetching baz."


def test_update_hibob_employee_treats_304_as_up_to_date(monkeypatch):
    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_resolve_employee_id(_headers: dict, _identifier: str):
        return "emp-123", ""

    captured_kwargs: dict[str, Any] = {}

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        captured_kwargs.update(kwargs)
        fake_resp = _FakeResponse({}, status_code=304)
        return fake_resp, "Error: HiBob returned HTTP 304 while trying to update employee emp-123."

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "_resolve_employee_id", fake_resolve_employee_id)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("service-user||api-token", executor)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="u-304",
                name="update_hibob_employee",
                arguments={
                    "employee_id": "emp-123",
                    "fields_json": '{"work": {"title": "Engineer"}}',
                },
            )
        )
    )

    assert result.content == "Employee emp-123 record is already up to date."
    # Verify 304 is passed as expected_statuses so safe_request suppresses the
    # warning log (the root cause of the log storm described in the bug report).
    assert captured_kwargs.get("expected_statuses") == {304}


def test_update_hibob_employee_surfaces_non_304_error(monkeypatch):
    """A non-304 failure must NOT be swallowed as "already up to date" - the
    branch keys off the exact 304 status, so any other error response is
    returned verbatim to the caller."""

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_resolve_employee_id(_headers: dict, _identifier: str):
        return "emp-123", ""

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        fake_resp = _FakeResponse({}, status_code=500)
        return fake_resp, "Error: HiBob returned HTTP 500 while updating employee emp-123."

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "_resolve_employee_id", fake_resolve_employee_id)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("service-user||api-token", executor)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="u-500",
                name="update_hibob_employee",
                arguments={
                    "employee_id": "emp-123",
                    "fields_json": '{"work": {"title": "Engineer"}}',
                },
            )
        )
    )

    assert "HTTP 500" in result.content
    assert "already up to date" not in result.content


# ── Closed-goal handling on update_hibob_goal ───────────────────


class _ErrResp:
    """Minimal stand-in for the failed httpx.Response that the closed-goal
    detector inspects (it only reads ``status_code`` and ``text``)."""

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def test_is_closed_goal_update_error_detects_signature():
    assert hibob._is_closed_goal_update_error(
        _ErrResp(400, "Goal g-1 is closed and cannot be updated")
    )
    # Case-insensitive on the body.
    assert hibob._is_closed_goal_update_error(_ErrResp(400, "Goal is CLOSED"))
    # Not a 400 → not our case.
    assert not hibob._is_closed_goal_update_error(_ErrResp(500, "closed"))
    # 400 but unrelated → not our case.
    assert not hibob._is_closed_goal_update_error(_ErrResp(400, "missing field title"))
    # No response (network error) → not our case.
    assert not hibob._is_closed_goal_update_error(None)


def test_update_hibob_goal_closed_returns_graceful_message(monkeypatch, caplog):
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any,
        _method: str,
        _url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ):
        captured.update(kwargs)
        return (
            _ErrResp(400, "Goal g-1 is closed and cannot be updated"),
            "Error: HiBob returned HTTP 400 while trying to update goal g-1. "
            "Invalid input for field 'Goal g-1 is closed and cannot be updated': goalId",
        )

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            executor.execute_tool(
                ToolCall(
                    id="g-closed",
                    name="update_hibob_goal",
                    arguments={"goal_id": "g-1", "fields_json": '{"title": "New"}'},
                )
            )
        )

    # Graceful, actionable message — not the raw HTTP 400.
    assert "is closed and cannot be updated" in result.content
    assert "update_hibob_goal_status" in result.content
    assert "HTTP 400" not in result.content
    # The request opts into 400 inspection (and suppresses the noisy warning).
    # Passed as a set to match ``safe_request``'s ``expected_statuses: set[int]``.
    assert captured.get("expected_statuses") == {400}
    # Closed-goal is benign — its warning stays suppressed (no re-log).
    assert "HiBob update goal" not in caplog.text


def test_update_hibob_goal_generic_400_passes_through(monkeypatch, caplog):
    raw_err = (
        "Error: HiBob returned HTTP 400 while trying to update goal g-2. "
        "Invalid value for field 'dueDate'."
    )

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(*_a: Any, **_kw: Any):
        return _ErrResp(400, "Invalid value for field 'dueDate'."), raw_err

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            executor.execute_tool(
                ToolCall(
                    id="g-bad",
                    name="update_hibob_goal",
                    arguments={"goal_id": "g-2", "fields_json": '{"dueDate": "nope"}'},
                )
            )
        )

    # A non-closed 400 is surfaced unchanged (no masking of real errors).
    assert result.content == raw_err
    # ...and re-logged as a warning so genuine validation errors stay visible
    # even though ``expected_statuses={400}`` suppressed safe_request's own log.
    assert "HiBob update goal g-2 failed" in caplog.text


def test_update_hibob_goal_success(monkeypatch):
    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(*_a: Any, **_kw: Any):
        return _ErrResp(200, "{}"), None

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="g-ok",
                name="update_hibob_goal",
                arguments={"goal_id": "g-3", "fields_json": '{"title": "New"}'},
            )
        )
    )

    assert not result.is_error
    assert result.content == "Goal g-3 updated successfully."


def _owner_stub(monkeypatch):
    """Fake HiBob: every request succeeds and is recorded."""
    calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any, method: str, url: str, _provider: str, _action: str, **kw: Any
    ):
        calls.append({"method": method, "url": url, "json": kw.get("json")})
        return _FakeResponse({}), None

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)
    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    return executor, calls


def _update_goal(executor: CallableToolExecutor, fields: dict[str, Any]):
    return asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="g-owner",
                name="update_hibob_goal",
                arguments={"goal_id": "42", "fields_json": json.dumps(fields)},
            )
        )
    )


def test_update_hibob_goal_refuses_the_owner_before_any_request(monkeypatch):
    # HiBob answers 400 "Goal owner ... cannot be changed on update" (#10858).
    executor, calls = _owner_stub(monkeypatch)
    result = _update_goal(executor, {"owner": "3804", "title": "New"})

    assert "cannot be changed through the HiBob API" in result.content
    assert "HiBob UI" in result.content
    assert "Nothing was sent" in result.content
    assert calls == []


def test_update_hibob_goal_without_owner_sends_one_patch(monkeypatch):
    executor, calls = _owner_stub(monkeypatch)
    result = _update_goal(executor, {"title": "New"})
    assert result.content == "Goal 42 updated successfully."
    assert [c["method"] for c in calls] == ["patch"]
    assert calls[0]["json"] == {
        "items": [{"objectType": "goal", "fields": {"title": {"value": "New"}}}]
    }


# ── Decimal key result values (#10060) ──────────────────────────


def _kr_stub(monkeypatch, stored: dict[str, dict[str, Any]]):
    """Fake HiBob API: records every call; the search returns ``stored``."""
    calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any, method: str, url: str, _provider: str, _action: str, **kwargs: Any,
    ):
        calls.append({"method": method, "url": url, "json": kwargs.get("json")})
        if url.endswith("/key-results/search"):
            items = [
                {"fields": {"id": {"value": int(kr_id)}, **{k: {"value": v} for k, v in f.items()}}}
                for kr_id, f in stored.items()
            ]
            return _FakeResponse({"items": items}), None
        return _FakeResponse({}), None

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)
    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    return executor, calls


def _run(executor: CallableToolExecutor, name: str, arguments: dict[str, Any]):
    return asyncio.run(executor.execute_tool(ToolCall(id="kr", name=name, arguments=arguments)))


def test_update_key_results_sends_decimal_target_and_verifies_it(monkeypatch):
    executor, calls = _kr_stub(monkeypatch, {"200": {"measureType": "numeric", "target": 0.5}})
    result = _run(executor, "update_hibob_key_results", {
        "goal_id": "g-1", "key_results_json": '[{"keyResultId": 200, "target": 0.5}]',
    })

    assert result.content == "Key results updated for goal g-1."
    patch = calls[0]
    assert patch["method"] == "patch"
    assert patch["json"]["items"][0]["fields"]["target"] == {"value": 0.5}
    assert calls[1]["url"].endswith("/goals/goals/key-results/search")


def test_update_key_results_reports_truncated_decimal_target(monkeypatch):
    executor, _ = _kr_stub(monkeypatch, {"200": {"measureType": "percentage", "target": 99}})
    result = _run(executor, "update_hibob_key_results", {
        "goal_id": "g-1", "key_results_json": '[{"keyResultId": 200, "target": 99.995}]',
    })

    assert result.content.startswith("Error:")
    assert "key result 200 (percentage): target sent 99.995, HiBob stored 99" in result.content


def test_update_key_results_reads_back_integer_target_and_title(monkeypatch):
    executor, calls = _kr_stub(monkeypatch, {"200": {"target": 5, "title": "T"}})
    result = _run(executor, "update_hibob_key_results", {
        "goal_id": "g-1", "key_results_json": '[{"keyResultId": 200, "target": 5, "title": "T"}]',
    })

    assert result.content == "Key results updated for goal g-1."
    assert len(calls) == 2
    assert calls[0]["json"]["items"][0]["fields"]["target"] == {"value": 5}
    assert "title" in calls[1]["json"]["fields"]


def test_update_key_results_reports_measure_type_hibob_kept(monkeypatch):
    executor, calls = _kr_stub(
        monkeypatch, {"17805401": {"measureType": "percentage", "target": 90}},
    )
    result = _run(executor, "update_hibob_key_results", {
        "goal_id": "g-1",
        "key_results_json": '[{"id": 17805401, "measure": "numeric", "target": 90}]',
    })

    assert result.is_error or result.content.startswith("Error:")
    assert "measureType sent numeric, HiBob stored percentage" in result.content
    assert "target sent" not in result.content
    assert "delete_hibob_key_result" in result.content
    sent = calls[0]["json"]["items"][0]["fields"]
    assert sent["measureType"] == {"value": "numeric"}


def test_update_key_results_reports_per_result_refusal(monkeypatch):
    calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any, method: str, url: str, _provider: str, _action: str, **kwargs: Any,
    ):
        calls.append({"method": method, "url": url})
        if url.endswith("/key-results/search"):
            return _FakeResponse({"items": [
                {"fields": {"id": {"value": 200}, "title": {"value": "Old"}}},
            ]}), None
        return _FakeResponse({"success": False, "goalId": 1, "results": [
            {"keyResultId": 200, "success": False, "error": "Key result is locked"},
        ]}), None

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)
    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    result = _run(executor, "update_hibob_key_results", {
        "goal_id": "g-1", "key_results_json": '[{"keyResultId": 200, "title": "New"}]',
    })

    assert result.content.startswith("Error:")
    assert "key result 200: HiBob refused the update (Key result is locked)" in result.content
    assert "title sent New, HiBob stored Old" in result.content


def test_delete_key_result_calls_the_key_result_endpoint(monkeypatch):
    executor, calls = _kr_stub(monkeypatch, {})
    result = _run(executor, "delete_hibob_key_result", {
        "goal_id": "11604150", "key_result_id": "17805401",
    })

    assert result.content == "Key result 17805401 deleted from goal 11604150."
    assert calls == [{
        "method": "delete",
        "url": f"{hibob._BASE_URL}/goals/goals/11604150/key-results/17805401",
        "json": None,
    }]


def test_update_progress_sends_decimal_current_value_and_verifies_it(monkeypatch):
    executor, calls = _kr_stub(
        monkeypatch, {"200": {"measureType": "percentage", "currentValue": 99.995}},
    )
    result = _run(executor, "update_hibob_key_results_progress", {
        "goal_id": "g-1", "progress_json": '[{"keyResultId": 200, "currentValue": 99.995}]',
    })

    assert result.content == "Key results progress updated for goal g-1 (1 KR(s))."
    assert calls[0]["json"]["keyResults"][0]["currentValue"] == 99.995
    assert len(calls) == 2


def test_update_progress_reports_truncated_current_value(monkeypatch):
    executor, _ = _kr_stub(
        monkeypatch, {"200": {"measureType": "percentage", "currentValue": 99}},
    )
    result = _run(executor, "update_hibob_key_results_progress", {
        "goal_id": "g-1", "progress_json": '[{"keyResultId": 200, "currentValue": 99.995}]',
    })

    assert result.content.startswith("Error:")
    assert "currentValue sent 99.995, HiBob stored 99" in result.content


def test_update_progress_integer_current_value_unchanged(monkeypatch):
    executor, calls = _kr_stub(monkeypatch, {})
    result = _run(executor, "update_hibob_key_results_progress", {
        "goal_id": "g-1", "progress_json": '[{"keyResultId": 200, "currentValue": 75}]',
    })

    assert result.content == "Key results progress updated for goal g-1 (1 KR(s))."
    assert len(calls) == 1


# ── get_hibob_goal ──────────────────────────────────────────────


def _goal_item(goal_id: str, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": {"value": goal_id}}
    for k, v in fields.items():
        payload[k] = {"value": v}
    return {"fields": payload}


def test_get_hibob_goal_returns_detail_and_filters_by_id(monkeypatch):
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any, _method: str, url: str, _provider: str, _action: str, **kwargs: Any
    ):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return (
            _FakeResponse(
                {
                    "items": [
                        _goal_item(
                            "g-1",
                            title="Ship v2",
                            status="onTrack",
                            owner="emp-7",
                            dueDate="2026-09-30",
                            typeId=3,
                            description="Quarter goal",
                        )
                    ]
                }
            ),
            "",
        )

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(id="g", name="get_hibob_goal", arguments={"goal_id": "g-1"})
        )
    )

    assert not result.is_error
    assert "Goal [g-1]: Ship v2" in result.content
    assert "Status: onTrack" in result.content
    assert "TypeID: 3" in result.content
    assert "Description: Quarter goal" in result.content
    # Reads via the goals search endpoint with an id-equality filter.
    assert captured["url"].endswith("/goals/goals/search")
    assert captured["json"]["filters"] == [
        {"fieldId": "id", "operator": "equals", "values": ["g-1"]}
    ]


def test_get_hibob_goal_not_found(monkeypatch):
    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(*_a: Any, **_kw: Any):
        return _FakeResponse({"items": []}), ""

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(id="g", name="get_hibob_goal", arguments={"goal_id": "g-x"})
        )
    )

    assert "Goal g-x not found" in result.content


def test_get_hibob_goal_matches_client_side_when_filter_ignored(monkeypatch):
    """If a tenant ignores the id filter and returns a page of goals, the
    correct one is still selected client-side (not the first arbitrary row)."""

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(*_a: Any, **_kw: Any):
        return (
            _FakeResponse(
                {
                    "items": [
                        _goal_item("g-1", title="First"),
                        _goal_item("g-2", title="Second"),
                    ]
                }
            ),
            "",
        )

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(id="g", name="get_hibob_goal", arguments={"goal_id": "g-2"})
        )
    )

    assert "Goal [g-2]: Second" in result.content
    assert "First" not in result.content


def test_get_hibob_goal_forces_id_when_fields_json_omits_it(monkeypatch):
    """A fields_json override that omits ``id`` must still request ``id`` —
    otherwise the client-side id match would always fail and report a spurious
    "not found" even though the server filter worked."""
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any, _method: str, url: str, _provider: str, _action: str, **kwargs: Any
    ):
        captured["json"] = kwargs.get("json")
        return (
            _FakeResponse(
                {"items": [_goal_item("g-1", title="Ship v2", status="onTrack")]}
            ),
            "",
        )

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="g",
                name="get_hibob_goal",
                arguments={"goal_id": "g-1", "fields_json": '["title", "status"]'},
            )
        )
    )

    # ``id`` is force-added to the requested fields even though it wasn't in
    # the override, so the client-side match succeeds.
    assert "id" in captured["json"]["fields"]
    assert "title" in captured["json"]["fields"]
    assert not result.is_error
    assert "Goal [g-1]: Ship v2" in result.content


# ── Custom employee field rendering (Option Grant regression) ───────────


def test_flatten_employee_fields_nested_custom_and_value_wrapper():
    emp = {
        "id": "emp-1",
        "displayName": "David Loiret",
        "work": {
            "title": "Engineer",
            "custom": {"field_1768430212695": "1,500 options"},
        },
        # Slash-path value-wrapper, human-readable preferred.
        "/equity/custom/field_999": {"value": "2000", "humanReadable": "2,000 options"},
        # Lists (historical tables) are skipped.
        "salaries": [{"amount": 1}],
    }
    flat = hibob._flatten_employee_fields(emp)
    assert flat["work.title"] == "Engineer"
    assert flat["work.custom.field_1768430212695"] == "1,500 options"
    assert flat["equity.custom.field_999"] == "2,000 options"
    # Identity roots and list values are not flattened.
    assert "displayName" not in flat
    assert "salaries" not in flat
    assert not any(k.startswith("salaries") for k in flat)


def test_coerce_scalar_falls_back_to_raw_when_human_readable_is_container():
    """A value-wrapper whose ``humanReadable`` is a dict/list (instead of a
    scalar) must fall back to the raw ``value`` rather than being dropped —
    otherwise a usable scalar is silently lost, violating the "nothing is ever
    silently dropped" contract.
    """
    # humanReadable is a list → fall back to the raw scalar value.
    assert (
        hibob._coerce_scalar({"value": "2000", "humanReadable": ["2,000 options"]})
        == "2000"
    )
    # humanReadable is a dict → same fallback.
    assert (
        hibob._coerce_scalar({"value": 50000, "humanReadable": {"raw": 50000}})
        == "50000"
    )
    # A usable scalar humanReadable is still preferred over the raw value.
    assert (
        hibob._coerce_scalar({"value": "2000", "humanReadable": "2,000 options"})
        == "2,000 options"
    )
    # Both container and no usable raw value → genuinely nothing to render.
    assert hibob._coerce_scalar({"humanReadable": ["x"], "value": {"a": 1}}) is None


def test_flatten_employee_fields_keeps_field_with_container_human_readable():
    """End-to-end: the flattener must surface a custom field even when its
    ``humanReadable`` is a container, using the raw value (regression for the
    silently-dropped-field edge case)."""
    emp = {
        "id": "emp-1",
        "displayName": "David Loiret",
        "equity": {
            "custom": {
                # humanReadable is a list — the raw value must still win.
                "field_999": {"value": "2000", "humanReadable": ["2,000 options"]},
            }
        },
    }
    flat = hibob._flatten_employee_fields(emp)
    assert flat["equity.custom.field_999"] == "2000"


def test_format_employee_detail_surfaces_custom_fields():
    emp = {
        "id": "emp-1",
        "displayName": "David Loiret",
        "email": "david.loiret@example.com",
        "work": {
            "title": "Engineer",
            "department": "R&D",
            "custom": {"field_1768430212695": "1,500 options"},
        },
        "equity": {"custom": {"field_999": "50000"}},
    }
    out = hibob._format_employee_detail(emp)
    assert "Title: Engineer" in out
    assert "Department: R&D" in out
    # The custom Option-Grant style fields are no longer silently dropped.
    assert "work.custom.field_1768430212695: 1,500 options" in out
    assert "equity.custom.field_999: 50000" in out


def test_format_employee_detail_surfaces_labelled_field_when_extract_empty():
    """A labelled field whose ``_extract_field`` returns "" but which
    ``_flatten_employee_fields`` resolves (e.g. a value-wrapper with only
    ``humanReadable`` — ``_extract_field`` looks for ``value``/``displayName``/
    ``name`` and misses it) must still be surfaced in the Additional fields
    dump, not silently dropped by marking the path "shown" when it was never
    rendered as a labelled line.
    """
    emp = {
        "id": "emp-1",
        "displayName": "Dana",
        "email": "dana@example.com",
        # work.title is a wrapper with ONLY humanReadable → _extract_field
        # returns "" (no value/displayName/name), but the flattener resolves it.
        "work": {"title": {"humanReadable": "Staff Engineer"}},
    }
    out = hibob._format_employee_detail(emp)
    assert "Staff Engineer" in out
    # Surfaced under its dotted path in the Additional fields section.
    assert "work.title: Staff Engineer" in out


def test_format_employee_oneliner_appends_custom_fields():
    emp = {
        "id": "e1",
        "displayName": "Dana",
        "email": "dana@example.com",
        "work": {"title": "Eng", "custom": {"field_1": "grant-X"}},
    }
    line = hibob._format_employee(emp)
    assert "Dana" in line
    assert "work.custom.field_1: grant-X" in line


def test_get_hibob_employee_requests_all_fields_for_custom_data(monkeypatch):
    """get_hibob_employee should pull every field id from metadata so custom
    fields are included by default, then surface them in the output."""
    calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_resolve(_headers: dict, _identifier: str):
        return "emp-1", ""

    async def fake_safe_request(
        _client: Any, method: str, url: str, _provider: str, _action: str, **kwargs: Any
    ):
        calls.append({"method": method, "url": url, "json": kwargs.get("json")})
        if url.endswith("/company/people/fields"):
            return (
                _FakeResponse(
                    {
                        "fields": [
                            {"id": "root.id"},
                            {"id": "work.title"},
                            {"id": "equity.custom.field_1768430212695"},
                        ]
                    }
                ),
                None,
            )
        return (
            _FakeResponse(
                {
                    "id": "emp-1",
                    "displayName": "David Loiret",
                    "email": "david@example.com",
                    "equity": {"custom": {"field_1768430212695": "1,500 options"}},
                }
            ),
            None,
        )

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "_resolve_employee_id", fake_resolve)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    result = asyncio.run(
        executor.execute_tool(
            ToolCall(
                id="e", name="get_hibob_employee", arguments={"identifier": "David"}
            )
        )
    )

    assert not result.is_error
    # Metadata was consulted and its field IDs forwarded to the read call.
    assert any(c["url"].endswith("/company/people/fields") for c in calls)
    read_call = next(c for c in calls if c["url"].endswith("/people/emp-1"))
    assert "equity.custom.field_1768430212695" in read_call["json"]["fields"]
    assert "equity.custom.field_1768430212695: 1,500 options" in result.content


# ── Reports (views shape + required format param) ───────────────────────


def _wire(monkeypatch, response: "_FakeResponse", capture: dict[str, Any]):
    @asynccontextmanager
    async def fake_client(timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any, method: str, url: str, _provider: str, _action: str, **kwargs: Any
    ):
        capture["method"] = method
        capture["url"] = url
        capture["json"] = kwargs.get("json")
        capture["params"] = kwargs.get("params")
        return response, None

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)


def _run_tool(name: str, arguments: dict[str, Any]) -> str:
    executor = CallableToolExecutor()
    hibob.build_tools("svc||tok", executor)
    result = asyncio.run(
        executor.execute_tool(ToolCall(id="t", name=name, arguments=arguments))
    )
    return result.content


def test_list_hibob_reports_parses_views_shape(monkeypatch):
    capture: dict[str, Any] = {}
    _wire(
        monkeypatch,
        _FakeResponse(
            {
                "views": [
                    {
                        "id": 12345,
                        "name": "Equity Report",
                        "type": "people",
                        "description": "Option grants",
                        "creationDate": "2025-09-09",
                        "createdBy": "admin@example.com",
                    }
                ]
            }
        ),
        capture,
    )
    content = _run_tool("list_hibob_reports", {})
    assert "[12345] Equity Report" in content
    assert "Option grants" in content
    assert "No reports found" not in content


def test_download_hibob_report_sends_required_format_param(monkeypatch):
    capture: dict[str, Any] = {}
    _wire(
        monkeypatch,
        _FakeResponse({"rows": [{"a": 1}]}, headers={"content-type": "application/json"}),
        capture,
    )
    content = _run_tool(
        "download_hibob_report", {"report_id": "12345", "report_format": "json"}
    )
    assert capture["params"] == {"format": "json", "humanReadable": "REPLACE"}
    assert '"rows"' in content


def test_download_hibob_report_defaults_to_csv(monkeypatch):
    capture: dict[str, Any] = {}
    _wire(monkeypatch, _FakeResponse({}, text="col1,col2\n1,2"), capture)
    content = _run_tool("download_hibob_report", {"report_id": "12345"})
    assert capture["params"]["format"] == "csv"
    assert "col1,col2" in content


def test_download_hibob_report_rejects_bad_format(monkeypatch):
    capture: dict[str, Any] = {}
    _wire(monkeypatch, _FakeResponse({}), capture)
    content = _run_tool(
        "download_hibob_report", {"report_id": "1", "report_format": "pdf"}
    )
    assert "invalid report_format" in content
    # No request should have been issued for an invalid format.
    assert capture == {}


# ── Positions (object-search endpoint + fields/filters body) ────────────


def test_search_hibob_positions_uses_object_search_contract(monkeypatch):
    capture: dict[str, Any] = {}
    _wire(
        monkeypatch,
        _FakeResponse(
            {
                "values": [
                    {
                        "/position/id": {"value": "p1"},
                        "/position/name": {"humanReadable": "Staff Engineer"},
                        "/position/status": {"humanReadable": "Vacant"},
                    }
                ]
            }
        ),
        capture,
    )
    content = _run_tool("search_hibob_positions", {})
    assert capture["url"].endswith("/objects/position/search")
    assert "/position/name" in capture["json"]["fields"]
    # filters is mandatory — a default non-cancelled filter is sent.
    assert capture["json"]["filters"][0]["fieldId"] == "/position/status"
    assert "[p1] Staff Engineer | Status: Vacant" in content


# ── Attendance (fields + filters contract, employee filter required) ────


def test_attendance_summary_requires_employee_ids(monkeypatch):
    capture: dict[str, Any] = {}
    _wire(monkeypatch, _FakeResponse({"items": []}), capture)
    content = _run_tool(
        "get_hibob_attendance_summary",
        {"from_date": "2025-01-01", "to_date": "2025-01-31"},
    )
    assert "employee_ids_json is required" in content
    assert capture == {}


def test_attendance_summary_sends_fields_and_filters(monkeypatch):
    capture: dict[str, Any] = {}
    _wire(
        monkeypatch,
        _FakeResponse(
            {
                "items": [
                    {
                        "objectType": "attendanceSummary",
                        "fields": {
                            "/attendanceSummary/employeeId": {"value": "123"},
                            "/attendanceSummary/hoursWorked": {"value": 168.5},
                            "/attendanceSummary/payableHours": {"value": 170},
                            "/attendanceSummary/overtimeHours": {"value": 2},
                        },
                    }
                ]
            }
        ),
        capture,
    )
    content = _run_tool(
        "get_hibob_attendance_summary",
        {
            "from_date": "2025-01-01",
            "to_date": "2025-01-31",
            "employee_ids_json": '["123"]',
        },
    )
    assert "/attendanceSummary/employeeId" in capture["json"]["fields"]
    field_ids = {f["fieldId"] for f in capture["json"]["filters"]}
    assert "/attendanceSummary/dateRange" in field_ids
    assert "/attendanceSummary/employeeId" in field_ids
    assert "Employee 123" in content
    assert "168.5" in content


def test_attendance_daily_sends_date_range_filters(monkeypatch):
    capture: dict[str, Any] = {}
    _wire(
        monkeypatch,
        _FakeResponse(
            {
                "items": [
                    {
                        "objectType": "dailyBreakdown",
                        "fields": {
                            "/dailyBreakdown/employeeId": {"value": "123"},
                            "/dailyBreakdown/date": {"value": "2025-01-15"},
                            "/dailyBreakdown/hoursWorked": {"value": 8.5},
                            "/dailyBreakdown/isApproved": {"value": True},
                        },
                    }
                ]
            }
        ),
        capture,
    )
    content = _run_tool(
        "get_hibob_attendance_daily",
        {
            "from_date": "2025-01-01",
            "to_date": "2025-01-31",
            "employee_ids_json": '["123"]',
        },
    )
    operators = {(f["fieldId"], f["operator"]) for f in capture["json"]["filters"]}
    assert ("/dailyBreakdown/date", "from") in operators
    assert ("/dailyBreakdown/date", "to") in operators
    assert ("/dailyBreakdown/employeeId", "equals") in operators
    assert "2025-01-15" in content


# ── Time-off balance (policy required + BalanceResult shape) ────────────


def test_timeoff_balance_requires_policy_type(monkeypatch):
    capture: dict[str, Any] = {}
    _wire(monkeypatch, _FakeResponse({}), capture)
    content = _run_tool(
        "get_hibob_timeoff_balance",
        {"employee_id": "emp-123", "policy_type": ""},
    )
    assert "policy_type is required" in content
    assert capture == {}


def test_timeoff_balance_omitted_policy_type_returns_friendly_error(monkeypatch):
    """When the LLM omits ``policy_type`` entirely (not just an empty string),
    the tool must still return the friendly "policy_type is required" message.

    The executor (``CallableToolExecutor``) only filters kwargs by signature and
    does not enforce the JSON-schema ``required`` list, so a required positional
    parameter would raise ``TypeError`` and surface a generic exception string.
    Keeping a default routes the omitted call through the friendly check.
    """
    capture: dict[str, Any] = {}
    _wire(monkeypatch, _FakeResponse({}), capture)
    content = _run_tool(
        "get_hibob_timeoff_balance",
        {"employee_id": "emp-123"},  # policy_type omitted entirely
    )
    assert "policy_type is required" in content
    assert "TypeError" not in content
    assert "positional argument" not in content
    assert capture == {}


def test_timeoff_balance_renders_balance_result(monkeypatch):
    capture: dict[str, Any] = {}
    _wire(
        monkeypatch,
        _FakeResponse(
            {
                "employeeId": "emp-123",
                "totalBalanceAsOfDate": 12.5,
                "totalRoundedBalanceAsOfDate": 12,
                "pointInTime": "2025-01-01",
            }
        ),
        capture,
    )
    content = _run_tool(
        "get_hibob_timeoff_balance",
        {"employee_id": "emp-123", "policy_type": "Holiday"},
    )
    assert capture["params"]["policyType"] == "Holiday"
    assert "emp-123" in content
    assert "Holiday" in content
    assert "12" in content


# ---------------------------------------------------------------------------
# Tasks - requestedFor must be surfaced, correct field names read,
# native task_status + client-side filters supported.
# ---------------------------------------------------------------------------


def test_list_hibob_tasks_surfaces_requested_for_and_correct_fields(monkeypatch):
    capture: dict[str, Any] = {}
    _wire(
        monkeypatch,
        _FakeResponse(
            {
                "tasks": [
                    {
                        "id": 32261122,
                        "title": "Termination - Organize laptop shipment",
                        "requestedFor": {
                            "id": "rf-mike",
                            "firstName": "Mike",
                            "surname": "Mammele",
                            "email": "mike.mammele@example.com",
                            "displayName": "Mike Mammele",
                        },
                        "owner": {
                            "id": "ow-remy",
                            "displayName": "Remy Angel",
                            "email": "remy.angel@example.com",
                        },
                        "workflow": "Offboarding",
                        "due": "2026-05-30",
                        "status": "Open",
                        "linkInBob": "https://app.hibob.com/task/32261122",
                        # Bogus values under the WRONG field names the old code
                        # probed - they must never surface in the output.
                        "dueDate": "1999-01-01",
                        "employeeId": "WRONG-EMP-ID",
                    }
                ]
            }
        ),
        capture,
    )
    content = _run_tool("list_hibob_tasks", {})
    assert capture["url"].endswith("/tasks")
    assert "[32261122] Termination - Organize laptop shipment" in content
    assert "Requested for: Mike Mammele" in content
    assert "Assignee: Remy Angel" in content
    assert "Workflow: Offboarding" in content
    assert "Due: 2026-05-30" in content
    assert "Status: Open" in content
    assert "https://app.hibob.com/task/32261122" in content
    # The old code read non-existent fields (employeeId / dueDate).
    assert "1999-01-01" not in content
    assert "WRONG-EMP-ID" not in content


def test_list_hibob_tasks_filters_by_requested_for(monkeypatch):
    capture: dict[str, Any] = {}
    _wire(
        monkeypatch,
        _FakeResponse(
            {
                "tasks": [
                    {
                        "id": 1,
                        "title": "Remove access",
                        "requestedFor": {
                            "displayName": "Mike Mammele",
                            "email": "mike.mammele@example.com",
                        },
                        "owner": {"displayName": "Remy Angel"},
                        "status": "Open",
                    },
                    {
                        "id": 2,
                        "title": "Order welcome kit",
                        "requestedFor": {
                            "displayName": "Alice Smith",
                            "email": "alice.smith@example.com",
                        },
                        "owner": {"displayName": "Remy Angel"},
                        "status": "Open",
                    },
                ]
            }
        ),
        capture,
    )
    content = _run_tool(
        "list_hibob_tasks", {"requested_for": "mike.mammele@example.com"}
    )
    assert "Remove access" in content
    assert "Order welcome kit" not in content
    assert "Filtered to 1 of 2" in content


def test_get_hibob_employee_tasks_sends_status_and_surfaces_requested_for(monkeypatch):
    capture: dict[str, Any] = {}
    _wire(
        monkeypatch,
        _FakeResponse(
            {
                "tasks": [
                    {
                        "id": 99,
                        "title": "Termination - Organize laptop shipment",
                        "requestedFor": {"displayName": "Mike Mammele"},
                        "owner": {"displayName": "Remy Angel"},
                        "workflow": "Offboarding",
                        "due": "2026-05-30",
                        "status": "Closed",
                        "completionDate": "2026-05-31",
                    }
                ]
            }
        ),
        capture,
    )
    content = _run_tool(
        "get_hibob_employee_tasks",
        {"employee_id": "3332883804594373380", "task_status": "closed"},
    )
    assert capture["params"] == {"task_status": "closed"}
    assert capture["url"].endswith("/tasks/people/3332883804594373380")
    assert "Requested for: Mike Mammele" in content
    assert "Completed: 2026-05-31" in content


def test_get_hibob_employee_tasks_filters_by_requested_for(monkeypatch):
    capture: dict[str, Any] = {}
    _wire(
        monkeypatch,
        _FakeResponse(
            {
                "tasks": [
                    {
                        "id": 1,
                        "title": "Mike task",
                        "requestedFor": {"displayName": "Mike Mammele"},
                        "status": "Closed",
                    },
                    {
                        "id": 2,
                        "title": "Someone else task",
                        "requestedFor": {"displayName": "Bob Stone"},
                        "status": "Closed",
                    },
                ]
            }
        ),
        capture,
    )
    content = _run_tool(
        "get_hibob_employee_tasks",
        {"employee_id": "3332883804594373380", "requested_for": "Mike"},
    )
    assert "Mike task" in content
    assert "Someone else task" not in content
    assert "Filtered to 1 of 2" in content


def test_get_hibob_employee_tasks_rejects_invalid_status(monkeypatch):
    capture: dict[str, Any] = {}
    _wire(monkeypatch, _FakeResponse({"tasks": []}), capture)
    content = _run_tool(
        "get_hibob_employee_tasks",
        {"employee_id": "3332883804594373380", "task_status": "pending"},
    )
    assert "task_status must be 'open', 'closed', 'all', or empty" in content
    # Validation happens before any HTTP call is made.
    assert capture == {}


def test_get_hibob_employee_tasks_all_status_returns_all_without_param(monkeypatch):
    # HiBob's task_status enum is open/closed only and omitting it returns
    # everything, so "all" must be accepted and honoured by NOT sending the
    # param (rather than rejected, or forwarded as a non-enum value).
    capture: dict[str, Any] = {}
    _wire(
        monkeypatch,
        _FakeResponse(
            {
                "tasks": [
                    {
                        "id": 1,
                        "title": "Open step",
                        "requestedFor": {"displayName": "Mike Mammele"},
                        "status": "Open",
                    },
                    {
                        "id": 2,
                        "title": "Closed step",
                        "requestedFor": {"displayName": "Mike Mammele"},
                        "status": "Closed",
                    },
                ]
            }
        ),
        capture,
    )
    content = _run_tool(
        "get_hibob_employee_tasks",
        {"employee_id": "3332883804594373380", "task_status": "all"},
    )
    assert capture["params"] == {}
    assert "task_status" not in (capture["params"] or {})
    assert "Open step" in content
    assert "Closed step" in content


def test_get_hibob_employee_tasks_resolves_employee_name(monkeypatch):
    calls: list[tuple[str, str]] = []

    @asynccontextmanager
    async def fake_client(_timeout: int = 30):
        yield object()

    async def fake_safe_request(
        _client: Any, method: str, url: str, _provider: str, _action: str, **kwargs: Any
    ):
        calls.append((method, url))
        if url.endswith("/people/search"):
            return (
                _FakeResponse(
                    {
                        "employees": [
                            {
                                "id": "emp-remy-7",
                                "firstName": "Remy",
                                "surname": "Angel",
                                "email": "remy.angel@example.com",
                            }
                        ]
                    }
                ),
                None,
            )
        return (
            _FakeResponse(
                {
                    "tasks": [
                        {
                            "id": 7,
                            "title": "Remove access",
                            "requestedFor": {"displayName": "Mike Mammele"},
                            "owner": {"displayName": "Remy Angel"},
                            "status": "Closed",
                        }
                    ]
                }
            ),
            None,
        )

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)

    content = _run_tool("get_hibob_employee_tasks", {"employee_id": "Remy Angel"})
    task_calls = [u for (_m, u) in calls if "/tasks/people/" in u]
    assert task_calls and task_calls[0].endswith("/tasks/people/emp-remy-7")
    assert "Requested for: Mike Mammele" in content


def test_fmt_task_person_falls_back_to_bare_id_or_email_when_no_name():
    # A truncated TaskEmployee (all name fields optional per the HiBob schema)
    # must surface its bare id/email, not a useless "Unknown (id=...)".
    assert hibob._fmt_task_person({"id": "rf-mike"}) == "id=rf-mike"
    assert (
        hibob._fmt_task_person({"email": "mike@example.com"})
        == "mike@example.com"
    )
    assert (
        hibob._fmt_task_person({"id": "rf-mike", "email": "mike@example.com"})
        == "mike@example.com, id=rf-mike"
    )
    # A name still wins and keeps the parenthesised identifiers.
    assert (
        hibob._fmt_task_person({"displayName": "Mike Mammele", "id": "rf-mike"})
        == "Mike Mammele (id=rf-mike)"
    )
    # Truly empty / non-dict inputs keep their existing behaviour.
    assert hibob._fmt_task_person({}) == "Unknown"
    assert hibob._fmt_task_person(None) == "N/A"
    assert hibob._fmt_task_person("emp-123") == "emp-123"


# ── Employee tables: list columns resolved to names ──────────────────────


def _route(monkeypatch, routes: dict[str, Any], calls: list[tuple[str, Any]]):
    """Answer each request by the first route whose key ends the URL."""

    @asynccontextmanager
    async def fake_client(timeout: int = 30):
        yield object()

    async def fake_safe_request(_client: Any, method: str, url: str, _provider: str, _action: str, **kwargs: Any):
        calls.append((url, kwargs.get("params")))
        for suffix, payload in routes.items():
            if url.endswith(suffix):
                if isinstance(payload, str):
                    return None, payload
                return _FakeResponse(payload), None
        return None, f"Error: unexpected {url}"

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)


_FIELDS = [
    {"id": "work.title", "type": "list", "typeData": {"listId": "title"}},
    {"id": "work.department", "type": "list", "typeData": {"listId": "department"}},
    {"id": "work.site", "type": "text"},
]


def test_employee_table_resolves_list_columns_to_names(monkeypatch):
    calls: list[tuple[str, Any]] = []
    _route(
        monkeypatch,
        {
            "/people/1001/work": {
                "values": [
                    {"id": 1, "title": "501", "department": "601", "site": "Site A"},
                    {"id": 2, "title": "502", "department": "699", "site": "Site A"},
                ]
            },
            "/company/people/fields": _FIELDS,
            "/company/named-lists/title": {
                "name": "title",
                "items": [
                    {"id": 501, "name": "Role One"},
                    {"id": 1, "name": "Group", "children": [{"id": 502, "name": "Role Two"}]},
                ],
            },
            "/company/named-lists/department": {
                "name": "department",
                "items": [{"id": 601, "name": "Team X"}],
            },
        },
        calls,
    )
    content = _run_tool("get_hibob_employee_table", {"employee_id": "1001", "table_name": "work"})
    assert "title: Role One (id 501)" in content
    assert "title: Role Two (id 502)" in content
    assert "department: Team X (id 601)" in content
    # An id the list does not hold stays raw; a text column is untouched.
    assert "department: 699" in content
    assert "site: Site A" in content
    # One metadata read and one read per list, archived items included.
    list_reads = [c for c in calls if "/named-lists/" in c[0]]
    assert len(list_reads) == 2
    assert all(params == {"includeArchived": "true"} for _, params in list_reads)


def test_employee_table_keeps_raw_ids_when_metadata_fails(monkeypatch):
    calls: list[tuple[str, Any]] = []
    _route(
        monkeypatch,
        {
            "/people/1001/work": {"values": [{"id": 1, "title": "501"}]},
            "/company/people/fields": "Error: HiBob API returned 403",
        },
        calls,
    )
    content = _run_tool("get_hibob_employee_table", {"employee_id": "1001", "table_name": "work"})
    assert "title: 501" in content
    assert not any("/named-lists/" in url for url, _ in calls)


def test_list_ids_by_column_prefers_the_table_category():
    fields = [
        {"id": "other.title", "type": "list", "typeData": {"listId": "other"}},
        {"id": "work.title", "type": "list", "typeData": {"listId": "title"}},
        {"id": "work.note", "type": "text", "typeData": {"listId": "ignored"}},
    ]
    assert hibob._list_ids_by_column(fields, "work", {"title", "note"}) == {"title": "title"}


# ── Who's out: one employee over a range ─────────────────────────────────


_OUTS = {
    "outs": [
        {
            "employeeId": "1001",
            "employeeDisplayName": "Person A",
            "policyTypeDisplayName": "Holiday",
            "startDate": "2026-02-01",
            "endDate": "2026-02-05",
            "status": "approved",
        },
        {
            "employeeId": "1002",
            "employeeDisplayName": "Person B",
            "policyTypeDisplayName": "Holiday",
            "startDate": "2026-03-01",
            "endDate": "2026-03-02",
            "status": "approved",
        },
    ]
}


def test_whos_out_filters_one_employee(monkeypatch):
    calls: list[tuple[str, Any]] = []
    _route(monkeypatch, {"/timeoff/whosout": _OUTS}, calls)
    content = _run_tool(
        "get_hibob_whos_out",
        {"from_date": "2025-12-01", "to_date": "2026-11-30", "employee": "1001"},
    )
    assert "Person A" in content
    assert "Person B" not in content
    assert calls[0][1] == {"from": "2025-12-01", "to": "2026-11-30"}


def test_whos_out_employee_without_time_off(monkeypatch):
    calls: list[tuple[str, Any]] = []
    _route(monkeypatch, {"/timeoff/whosout": _OUTS}, calls)
    content = _run_tool(
        "get_hibob_whos_out",
        {"from_date": "2025-12-01", "to_date": "2026-11-30", "employee": "1003"},
    )
    assert content == "No time off for employee 1003 from 2025-12-01 to 2026-11-30."


def test_whos_out_employee_range_is_checked_before_any_request(monkeypatch):
    calls: list[tuple[str, Any]] = []
    _route(monkeypatch, {"/timeoff/whosout": _OUTS}, calls)
    too_long = _run_tool(
        "get_hibob_whos_out",
        {"from_date": "2024-01-01", "to_date": "2026-06-01", "employee": "1001"},
    )
    assert "at most 24 months" in too_long
    missing = _run_tool("get_hibob_whos_out", {"employee": "1001"})
    assert "required" in missing
    reversed_range = _run_tool(
        "get_hibob_whos_out",
        {"from_date": "2026-06-01", "to_date": "2026-01-01", "employee": "1001"},
    )
    assert "before from_date" in reversed_range
    assert calls == []


def test_whos_out_without_employee_is_unchanged(monkeypatch):
    calls: list[tuple[str, Any]] = []
    _route(monkeypatch, {"/timeoff/whosout": _OUTS}, calls)
    content = _run_tool("get_hibob_whos_out", {"from_date": "2026-01-01", "to_date": "2026-12-31"})
    assert "Person A" in content and "Person B" in content


def _goal(goal_id: int, owner: str, cycle: int = 1) -> dict[str, Any]:
    return {
        "fields": {
            "id": {"value": goal_id},
            "title": {"value": f"Goal {goal_id}"},
            "status": {"value": "onTrack"},
            "progress": {"value": 40},
            "owner": {"value": owner},
            "cycleId": {"value": cycle},
        }
    }


def _goal_pages(monkeypatch: Any, pages: list[dict[str, Any]], krs: dict[str, Any], bodies: list[Any]) -> None:
    """Answer goal searches page by page (by cursor) and KR searches by goal id."""

    @asynccontextmanager
    async def fake_client(timeout: int = 30) -> Any:
        yield object()

    async def fake_safe_request(
        _client: Any, method: str, url: str, _provider: str, _action: str, **kwargs: Any
    ) -> tuple[Any, str | None]:
        body = kwargs.get("json") or {}
        bodies.append((url, json.loads(json.dumps(body))))
        if url.endswith("/key-results/search"):
            goal_id = body["filters"][0]["values"][0]
            return _FakeResponse({"items": krs.get(goal_id, [])}), None
        if url.endswith("/goals/goals/search"):
            index = int(body.get("cursor") or 0)
            return _FakeResponse(pages[index]), None
        if url.endswith("/people/search"):
            return _FakeResponse({"employees": [{"id": "1002", "email": "b@example.com"}]}), None
        return None, f"Error: unexpected {url}"

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)


_GOAL_PAGES = [
    {"items": [_goal(1, "1001"), _goal(2, "1002")], "responseMetadata": {"nextCursor": "1"}},
    {"items": [_goal(3, "1002", cycle=2), _goal(4, "1003")], "responseMetadata": {"nextCursor": None}},
]


def test_goals_owner_filter_walks_every_page(monkeypatch: Any) -> None:
    bodies: list[Any] = []
    _goal_pages(monkeypatch, _GOAL_PAGES, {}, bodies)
    content = _run_tool("search_hibob_goals", {"owner": "1002"})
    assert "Found 2 goal(s)" in content
    assert "[2] Goal 2" in content and "[3] Goal 3" in content
    assert "Goal 1 " not in content and "Goal 4" not in content
    assert "Progress: 40%" in content
    goal_calls = [b for url, b in bodies if url.endswith("/goals/goals/search")]
    assert len(goal_calls) == 2
    assert goal_calls[1]["cursor"] == "1"
    assert "progress" in goal_calls[0]["fields"]


def test_goals_owner_by_email_is_resolved(monkeypatch: Any) -> None:
    bodies: list[Any] = []
    _goal_pages(monkeypatch, _GOAL_PAGES, {}, bodies)
    content = _run_tool("search_hibob_goals", {"owner": "b@example.com", "cycle_id": "2"})
    assert "Found 1 goal(s)" in content
    assert "[3] Goal 3" in content


def test_goals_without_client_filter_reads_one_page(monkeypatch: Any) -> None:
    bodies: list[Any] = []
    _goal_pages(monkeypatch, _GOAL_PAGES, {}, bodies)
    content = _run_tool("search_hibob_goals", {})
    assert "Found 2 goal(s)" in content
    assert len([1 for url, _ in bodies if url.endswith("/goals/goals/search")]) == 1


def test_goals_include_key_results(monkeypatch: Any) -> None:
    bodies: list[Any] = []
    krs = {
        "2": [
            {
                "fields": {
                    "id": {"value": 21},
                    "title": {"value": "KR A"},
                    "measureType": {"value": "number"},
                    "startValue": {"value": 0},
                    "target": {"value": 10},
                    "currentValue": {"value": 5},
                    "progress": {"value": 50},
                }
            }
        ]
    }
    _goal_pages(monkeypatch, _GOAL_PAGES, krs, bodies)
    content = _run_tool("search_hibob_goals", {"owner": "1002", "include_key_results": True})
    assert "KR [21] KR A | Measure: number | Start: 0 | Target: 10 | Current: 5 | Progress: 50%" in content
    assert content.count("No key results.") == 1
    kr_calls = [b for url, b in bodies if url.endswith("/key-results/search")]
    assert [c["filters"][0]["values"] for c in kr_calls] == [["2"], ["3"]]


def test_withheld_field_ids_names_requested_fields_not_returned() -> None:
    emp = {
        "id": "1",
        "work": {"title": "Engineer", "manager": None},
        "/payroll/salary": {"value": None},
        "people_analytics": {"flight_risk": {"value": "low"}},
    }
    requested = [
        "root.id",
        "work.title",
        "work.manager",
        "payroll.salary",
        "people_analytics.flight_risk",
        "people_analytics.low_performance",
    ]
    assert hibob._withheld_field_ids(emp, requested) == [
        "work.manager",
        "payroll.salary",
        "people_analytics.low_performance",
    ]


def test_get_employee_lists_withheld_fields(monkeypatch: Any) -> None:
    calls: list[tuple[str, Any]] = []
    _route(
        monkeypatch,
        {
            "/company/people/fields": [{"id": "work.title"}, {"id": "people_analytics.low_performance"}],
            "/people/1001": {"id": "1001", "displayName": "Person A", "work": {"title": "Engineer"}},
        },
        calls,
    )
    content = _run_tool("get_hibob_employee", {"identifier": "1001"})
    assert "Title: Engineer" in content
    assert "Requested fields not returned (1)" in content
    assert "people_analytics.low_performance" in content


def test_snapshot_range_defaults_to_the_last_twelve_months() -> None:
    assert hibob._snapshot_range("", "2026-06-30") == ("2025-06-30", "2026-06-30")
    assert hibob._snapshot_range("2026-01-01", "2026-03-01") == ("2026-01-01", "2026-03-01")


def test_timeoff_by_policy_totals_requests_and_days() -> None:
    lines = hibob._timeoff_by_policy_lines(
        [
            {"policyTypeDisplayName": "Holiday", "startDate": "2026-02-01", "endDate": "2026-02-05"},
            {"policyTypeDisplayName": "Holiday", "startDate": "2026-03-01", "endDate": "2026-03-01"},
            {"policyTypeDisplayName": "Sick", "startDate": "bad", "endDate": "2026-03-01"},
        ],
        "2026-01-01",
        "2026-12-31",
    )
    assert "  Holiday: 2 request(s), 6 day(s)" in lines
    assert "  Sick: 1 request(s), 0 day(s)" in lines
    assert hibob._timeoff_by_policy_lines([], "2026-01-01", "2026-12-31") == [
        "No time off from 2026-01-01 to 2026-12-31."
    ]


def test_performance_snapshot_gathers_every_section(monkeypatch: Any) -> None:
    calls: list[tuple[str, Any]] = []
    _route(
        monkeypatch,
        {
            "/company/people/fields": [{"id": "work.title"}],
            "/people/1001/work": {"values": [{"id": 1, "site": "Site A"}]},
            "/people/1001/lifecycle": {"values": [{"id": 2, "status": "employed"}]},
            # Before /people/1001, which also ends the documents URL.
            "/docs/people/1001": {"documents": [{"id": 9, "name": "Doc A"}]},
            "/people/1001": {"id": "1001", "displayName": "Person A", "work": {"title": "Engineer"}},
            "/timeoff/whosout": _OUTS,
            "/goals/goals/search": {"items": [_goal(7, "1001")], "responseMetadata": {"nextCursor": None}},
            "/key-results/search": {"items": []},
        },
        calls,
    )
    content = _run_tool(
        "get_hibob_performance_snapshot",
        {"employee": "1001", "from_date": "2026-01-01", "to_date": "2026-12-31"},
    )
    assert content.startswith("Performance snapshot of employee 1001")
    for heading in (
        "## Profile",
        "## Work history",
        "## Lifecycle history",
        "## Time off 2026-01-01 to 2026-12-31",
        "## Goals and key results",
        "## Documents",
        "## Performance reviews",
    ):
        assert heading in content
    assert "Title: Engineer" in content
    assert "Holiday: 1 request(s), 5 day(s)" in content
    assert "Person B" not in content
    assert "[7] Goal 7" in content
    assert "Doc A" in content
    assert "Bob MCP" in content


def test_performance_snapshot_checks_the_range_first(monkeypatch: Any) -> None:
    calls: list[tuple[str, Any]] = []
    _route(monkeypatch, {}, calls)
    content = _run_tool(
        "get_hibob_performance_snapshot",
        {"employee": "1001", "from_date": "2026-06-01", "to_date": "2026-01-01"},
    )
    assert "before from_date" in content
    assert calls == []


def test_performance_snapshot_skips_optional_sections(monkeypatch: Any) -> None:
    calls: list[tuple[str, Any]] = []
    _route(
        monkeypatch,
        {
            "/company/people/fields": [],
            "/people/1001/work": {"values": []},
            "/people/1001/lifecycle": {"values": []},
            "/people/1001": {"id": "1001"},
            "/timeoff/whosout": {"outs": []},
        },
        calls,
    )
    content = _run_tool(
        "get_hibob_performance_snapshot",
        {"employee": "1001", "include_goals": False, "include_documents": False},
    )
    assert "## Goals" not in content and "## Documents" not in content
    assert "No time off from" in content
    assert not any(url.endswith("/goals/goals/search") for url, _ in calls)
