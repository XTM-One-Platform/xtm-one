"""Unit tests for the shared Bearer-header guard in ``app.integrations.http``.

Building an ``Authorization: Bearer <token>`` header from a blank or
whitespace-only token yields the value ``"Bearer "``, which httpx/h11 rejects
with an opaque ``LocalProtocolError`` that surfaces as a useless HTTP 500.
``normalize_bearer_token`` / ``bearer_auth_headers`` are the single guard that
makes that header impossible to construct, so the empty-token check can never be
silently dropped by a future refactor.
"""

from __future__ import annotations

import pytest

from app.integrations.http import (
    MISSING_TOKEN_MESSAGE,
    EmptyBearerTokenError,
    bearer_auth_headers,
    normalize_bearer_token,
)


@pytest.mark.parametrize("blank", [None, "", "   ", "\t", "\n", " \t\n "])
def test_normalize_bearer_token_rejects_blank(blank):
    """A blank/whitespace-only token must raise the typed error so the caller
    can map it to a clean 401 instead of leaking a LocalProtocolError."""
    with pytest.raises(EmptyBearerTokenError) as exc:
        normalize_bearer_token(blank)
    assert "missing or expired" in str(exc.value)
    assert str(exc.value) == MISSING_TOKEN_MESSAGE


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abc", "abc"),
        ("  abc  ", "abc"),
        ("token\n", "token"),
        ("base_url||api_key", "base_url||api_key"),
    ],
)
def test_normalize_bearer_token_strips_and_returns(raw, expected):
    assert normalize_bearer_token(raw) == expected


def test_bearer_auth_headers_builds_clean_header():
    headers = bearer_auth_headers("  my-token  ")
    assert headers == {"Authorization": "Bearer my-token"}
    # Crucially: never an illegal trailing-space-only value.
    assert headers["Authorization"] != "Bearer "


def test_bearer_auth_headers_merges_extra_headers():
    headers = bearer_auth_headers(
        "tok", extra={"Accept": "application/vnd.github+json"}
    )
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Accept"] == "application/vnd.github+json"


def test_bearer_auth_headers_extra_cannot_override_authorization():
    """The computed Authorization must always win over an Authorization key in
    ``extra`` - otherwise a caller could reintroduce the illegal ``"Bearer "``
    value the helper exists to prevent."""
    headers = bearer_auth_headers(
        "good-token", extra={"Authorization": "Bearer ", "Accept": "x"}
    )
    assert headers["Authorization"] == "Bearer good-token"
    assert headers["Accept"] == "x"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_bearer_auth_headers_rejects_blank(blank):
    with pytest.raises(EmptyBearerTokenError):
        bearer_auth_headers(blank)


def test_empty_bearer_token_error_is_value_error():
    """The error stays a plain ValueError subclass so the integrations layer
    carries no FastAPI/HTTP coupling — API callers translate it to a 401."""
    assert issubclass(EmptyBearerTokenError, ValueError)
