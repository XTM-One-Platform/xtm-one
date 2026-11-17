"""A 400 or 422 from an integration (an imported OpenAPI tool included)
quotes what the provider said, redacted and bounded, as 403 and 404 do.

Before, a body in a shape the extractor did not know answered a bare
"bad request." and the agent repeated the call it could not correct.
"""

import httpx

from app.integrations.http import format_http_error

_ACTION = "execute tool 'instance_list'"


def test_400_with_a_json_body_quotes_its_message_fields() -> None:
    resp = httpx.Response(
        400,
        json={
            "type": "https://tools.ietf.org/html/rfc9110#section-15.5.1",
            "title": "One or more validation errors occurred.",
            "status": 400,
            "errors": {"pageSize": ["The field pageSize must be between 1 and 100."]},
        },
    )
    result = format_http_error(resp, "SaaS API", _ACTION)
    assert result.startswith(
        "Error: SaaS API rejected the request to execute tool 'instance_list' \u2014 bad request. "
    )
    assert "pageSize: The field pageSize must be between 1 and 100." in result


def test_400_with_a_nested_error_object_quotes_its_message() -> None:
    resp = httpx.Response(400, json={"error": {"code": 400, "message": "Invalid value for 'status'"}})
    result = format_http_error(resp, "SaaS API", _ACTION)
    assert result.endswith("bad request. Invalid value for 'status'")


def test_400_with_an_unknown_json_shape_quotes_the_body_bounded() -> None:
    resp = httpx.Response(400, json={"reason": "unknown filter", "items": ["x" * 50] * 20})
    result = format_http_error(resp, "SaaS API", _ACTION)
    assert '"reason":"unknown filter"' in result
    assert len(result) < 420


def test_400_with_a_text_body_quotes_a_bounded_excerpt() -> None:
    resp = httpx.Response(400, text="query parameter 'limit' is not an integer\n" + "y" * 2000)
    result = format_http_error(resp, "SaaS API", _ACTION)
    assert "query parameter 'limit' is not an integer" in result
    assert "y" * 301 not in result
    assert len(result) < 420


def test_400_and_422_never_echo_a_header_or_a_credential() -> None:
    token = "ghp_" + "A1b2C3d4" * 4
    resp = httpx.Response(
        400,
        headers={"Authorization": "Bearer header-secret-value", "Set-Cookie": "session=cookie-secret"},
        json={"message": f"token {token} is not allowed with Bearer abcdefgh12345678"},
    )
    result = format_http_error(resp, "SaaS API", _ACTION)
    assert token not in result
    assert "abcdefgh12345678" not in result
    assert "header-secret-value" not in result
    assert "cookie-secret" not in result
    assert "bad request. token" in result

    unprocessable = httpx.Response(422, text=f"bad key {token}")
    result = format_http_error(unprocessable, "SaaS API", _ACTION)
    assert token not in result
    assert "validation error. bad key" in result


def test_422_with_no_body_has_no_trailing_space() -> None:
    resp = httpx.Response(422, text="")
    result = format_http_error(resp, "SaaS API", _ACTION)
    assert result == "Error: SaaS API rejected the request to execute tool 'instance_list' \u2014 validation error."
