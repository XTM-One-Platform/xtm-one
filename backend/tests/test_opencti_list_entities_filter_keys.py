"""list_opencti_entities hints at valid keys after OpenCTI rejects a filter.

OpenCTI stays the judge of what is valid: the call is always sent, and the
filterKeysSchema is only read afterwards, to suggest close keys for the ones
OpenCTI named in its UNSUPPORTED_ERROR (BAD_USER_INPUT_ERROR since OpenCTI#19096).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

SCHEMA = {
    "data": {
        "filterKeysSchema": [
            {"entity_type": "Malware", "filters_schema": [
                {"filterKey": "objectLabel"}, {"filterKey": "createdBy"}, {"filterKey": "confidence"},
                {"filterKey": "x_opencti_detection"}, {"filterKey": "x_opencti_cvss_base_score"}]}
        ]
    }
}
LIST_OK = {"data": {"stixDomainObjects": {"edges": [], "pageInfo": {"globalCount": 0}}}}


UNKNOWN_KEYS = "Incorrect filter keys not existing in any schema definition"
INVALID_CHARS = "Incorrect filter keys containing invalid characters"


def _rejected(
    *keys: str,
    code: str = "UNSUPPORTED_ERROR",
    http_status: int = 500,
    message: str = UNKNOWN_KEYS,
) -> dict:
    return {"errors": [{
        "message": message,
        "extensions": {"code": code,
                       "data": {"http_status": http_status, "genre": "BUSINESS", "keys": list(keys)}},
    }]}


# Code and status vary independently: the hint must not read the status (FR-002).
REJECTIONS = pytest.mark.parametrize(
    ("code", "http_status"),
    [(c, s) for c in ("UNSUPPORTED_ERROR", "BAD_USER_INPUT_ERROR") for s in (500, 400)],
)


def _fg(*keys: str) -> str:
    return json.dumps(
        {"mode": "and", "filters": [{"key": k, "values": ["x"]} for k in keys], "filterGroups": []}
    )


async def _call(filters: str, *, list_answer: dict = LIST_OK, schema: dict | int = SCHEMA):
    executor = CallableToolExecutor()
    build_tools("https://octi.test||static-token", executor)
    queries: list[str] = []

    async def _fake(self, method, url, **kwargs):
        query = (kwargs.get("json") or {}).get("query", "")
        queries.append(query)
        request = httpx.Request("POST", url)
        if "FilterKeysSchema" in query:
            if isinstance(schema, int):
                return httpx.Response(schema, text="down", request=request)
            return httpx.Response(200, json=schema, request=request)
        return httpx.Response(200, json=list_answer, request=request)

    with patch.object(httpx.AsyncClient, "request", _fake):
        result = await executor.execute_tool(
            ToolCall(id="1", name="list_opencti_entities", arguments={"filters": filters})
        )
    return result.content, queries


def _schema_fetches(queries: list[str]) -> int:
    return sum("FilterKeysSchema" in q for q in queries)


@pytest.mark.asyncio
@REJECTIONS
async def test_rejected_keys_come_back_with_close_schema_keys(code, http_status):
    content, queries = await _call(_fg("created_by", "labels"), list_answer=_rejected("created_by", "labels", code=code, http_status=http_status))
    assert code in content
    assert "created_by -> createdBy" in content
    assert "Hint:" in content
    assert _schema_fetches(queries) == 1


@pytest.mark.asyncio
async def test_a_call_is_always_sent_and_the_schema_is_not_read_when_it_succeeds():
    _, queries = await _call(_fg("aliases"))
    assert any("ListSDOs" in q for q in queries)
    assert _schema_fetches(queries) == 0


@pytest.mark.asyncio
@REJECTIONS
async def test_a_failed_schema_fetch_returns_the_original_error(code, http_status):
    content, _ = await _call(_fg("labels"), list_answer=_rejected("labels", code=code, http_status=http_status), schema=500)
    assert code in content
    assert "Hint:" not in content


@pytest.mark.asyncio
async def test_other_errors_do_not_read_the_schema():
    auth = {"errors": [{"message": "You must be logged in to do this.",
                        "extensions": {"code": "AUTH_REQUIRED", "data": {"http_status": 401}}}]}
    content, queries = await _call(_fg("objectLabel"), list_answer=auth)
    assert "Hint:" not in content
    assert _schema_fetches(queries) == 0


@pytest.mark.asyncio
async def test_another_code_with_the_filter_keys_wording_gets_no_hint():
    other = _rejected("labels", code="VALIDATION_ERROR", http_status=400)
    content, queries = await _call(_fg("labels"), list_answer=other)
    assert "Hint:" not in content
    assert _schema_fetches(queries) == 0


@pytest.mark.asyncio
async def test_the_generic_new_code_with_another_message_gets_no_hint():
    other = _rejected("labels", code="BAD_USER_INPUT_ERROR", http_status=400, message="Invalid value")
    content, queries = await _call(_fg("labels"), list_answer=other)
    assert "Hint:" not in content
    assert _schema_fetches(queries) == 0


@pytest.mark.asyncio
@REJECTIONS
async def test_keys_cut_off_in_a_long_error_still_give_a_hint(code, http_status):
    many = [f"bad_{i}_xxxxxxxxxxxxxxxx" for i in range(30)] + ["created_by"]
    content, _ = await _call(_fg("created_by"), list_answer=_rejected(*many, code=code, http_status=http_status))
    hint = content.split("Hint:")[1]
    assert "bad_0_xxxxxxxxxxxxxxxx" in content.split("Hint:")[0]
    assert "created_by ->" not in hint  # cut off by the 300-char cap, so never parsed
    assert '"bad_' not in hint


@pytest.mark.asyncio
@REJECTIONS
async def test_invented_keys_that_only_contain_a_real_one_are_matched(code, http_status):
    content, _ = await _call(
        _fg("labels", "detection", "cvss_base_score"),
        list_answer=_rejected("labels", "detection", "cvss_base_score", code=code, http_status=http_status),
    )
    assert "labels -> objectLabel." in content
    assert "detection -> x_opencti_detection." in content
    assert "cvss_base_score -> x_opencti_cvss_base_score." in content


@pytest.mark.asyncio
@REJECTIONS
async def test_a_key_with_no_close_match_gets_the_generic_hint_only(code, http_status):
    content, _ = await _call(_fg("zzzz"), list_answer=_rejected("zzzz", code=code, http_status=http_status))
    assert "Hint:" in content
    assert "->" not in content.split("Hint:")[1]


@pytest.mark.asyncio
@REJECTIONS
async def test_a_rejection_without_keys_is_returned_untouched(code, http_status):
    no_keys = {"errors": [{"message": UNKNOWN_KEYS,
                           "extensions": {"code": code, "data": {"http_status": http_status}}}]}
    content, queries = await _call(_fg("labels"), list_answer=no_keys)
    assert "Hint:" not in content
    assert _schema_fetches(queries) == 0


@pytest.mark.asyncio
@REJECTIONS
async def test_a_key_with_invalid_characters_gets_the_hint(code, http_status):
    answer = _rejected("created-by", code=code, http_status=http_status, message=INVALID_CHARS)
    content, queries = await _call(_fg("created-by"), list_answer=answer)
    assert "created-by -> createdBy" in content
    assert _schema_fetches(queries) == 1


@pytest.mark.asyncio
@REJECTIONS
async def test_a_key_that_breaks_the_keys_list_is_returned_untouched(code, http_status):
    key = "name.']; ctx._source.value = true; //"
    answer = _rejected(key, code=code, http_status=http_status, message=INVALID_CHARS)
    content, queries = await _call(_fg(key), list_answer=answer)
    assert "Hint:" not in content
    assert _schema_fetches(queries) == 0


@pytest.mark.asyncio
@REJECTIONS
async def test_an_empty_schema_gives_no_hint(code, http_status):
    content, _ = await _call(_fg("labels"), list_answer=_rejected("labels", code=code, http_status=http_status),
                             schema={"data": {"filterKeysSchema": []}})
    assert code in content
    assert "Hint:" not in content


@pytest.mark.asyncio
@REJECTIONS
async def test_the_hint_does_not_send_the_agent_to_validate_nlq_filter(code, http_status):
    content, _ = await _call(_fg("labels"), list_answer=_rejected("labels", code=code, http_status=http_status))
    assert "validate_nlq_filter" not in content


@pytest.mark.asyncio
async def test_invalid_filters_json_is_still_refused_up_front():
    content, queries = await _call("{not json")
    assert content.startswith("Error:")
    assert queries == []


@pytest.mark.asyncio
@REJECTIONS
async def test_a_slow_schema_fetch_returns_the_original_error(monkeypatch, code, http_status):
    from app.integrations.providers.opencti import knowledge

    async def _never(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(knowledge, "_FILTER_HINT_BUDGET_S", 0.05)
    monkeypatch.setattr(knowledge, "_get_available_keys", _never)
    content, _ = await _call(_fg("labels"), list_answer=_rejected("labels", code=code, http_status=http_status))
    assert code in content
    assert "Hint:" not in content


def test_close_keys_break_ties_by_name():
    from app.integrations.providers.opencti.knowledge import _close_keys

    available = {"aaaaaaaaaa_b_score", "zzzzzzzzzzzzzzzzzzzz_score", "aaaaaaaaaa_a_score"}
    assert _close_keys("score", available) == [
        "aaaaaaaaaa_a_score", "aaaaaaaaaa_b_score", "zzzzzzzzzzzzzzzzzzzz_score",
    ]


def test_close_keys_drop_one_trailing_s_not_all():
    from app.integrations.providers.opencti.knowledge import _close_keys

    # "class" -> "clas" still matches; stripping every "s" would leave "cla" (< 4) and match nothing.
    assert _close_keys("class", {"xclassyyyyyyyyyyyyyyyy"}) == ["xclassyyyyyyyyyyyyyyyy"]
