"""The ``platform_url`` a platform registers with names an http(s) identity.

``POST /api/v1/platform/register`` stores ``platform_url`` as the platform's
identity (the ``iss`` of the tokens it signs, #4880) and every surface links to
it. The body model refuses anything else, so a ``javascript:``, ``data:`` or
relative value gets a 422 and never reaches a row or the handler. The token is
checked first, before the body is read (``_bounded_register_body``): an
unauthenticated caller gets the 401 whatever its body. Rows stored before the
check are guarded on read (``frontend/src/lib/safe-url.ts``), not migrated.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.api.v1 import platform as platform_api
from app.api.v1.platform import PLATFORM_URL_INVALID_DETAIL, PLATFORM_URL_MAX_LENGTH, RegisterRequest
from app.core.config import settings

SECRET = "test-registration-token"
_HOST = "https://opencti.example.com"
_TOO_LONG = f"{_HOST}/{'a' * (PLATFORM_URL_MAX_LENGTH - len(_HOST))}"

HOSTILE_PLATFORM_URLS = [
    pytest.param("javascript:alert(1)", id="javascript"),
    pytest.param("JaVaScRiPt:alert(1)", id="javascript-mixed-case"),
    pytest.param(" javascript:alert(1)", id="javascript-leading-space"),
    pytest.param("data:text/html,<script>alert(1)</script>", id="data"),
    pytest.param("vbscript:msgbox(1)", id="vbscript"),
    pytest.param("file:///etc/passwd", id="file"),
    pytest.param("/dashboard", id="root-relative"),
    pytest.param("dashboard/settings", id="relative"),
    pytest.param("//evil.example", id="protocol-relative"),
    pytest.param("https://", id="no-host"),
    pytest.param("https://opencti.example.com@evil.example", id="userinfo"),
    pytest.param(f"{_HOST}/?next=x", id="query"),
    pytest.param(f"{_HOST}/#x", id="fragment"),
    pytest.param(f"{_HOST}/\x00x", id="nul"),
    pytest.param(f"{_HOST}/\x1b[0m", id="control-character"),
    pytest.param(f"{_HOST}/\x7f", id="delete-character"),
    pytest.param("https://open cti.example.com", id="space-in-host"),
    pytest.param("https:\\\\evil.example\\phish", id="backslashes"),
    pytest.param(f"{_HOST}\\@evil.example", id="backslash-before-userinfo"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param(_TOO_LONG, id="over-the-length-limit"),
    pytest.param(None, id="null"),
    pytest.param(42, id="not-a-string"),
]


class _NoDB:
    """A refused body must be refused before anything is read or written."""

    def __getattr__(self, name: str):
        raise AssertionError(f"a refused registration touched the database ({name})")


@pytest.fixture
def client(monkeypatch) -> TestClient:
    app = FastAPI()
    app.include_router(platform_api.router, prefix="/api/v1/platform")
    app.dependency_overrides[get_db] = lambda: _NoDB()
    return TestClient(app)


def _register(client: TestClient, platform_url: object, *, token: str | None = SECRET):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(
        "/api/v1/platform/register",
        headers=headers,
        json={"platform_identifier": "opencti", "platform_url": platform_url},
    )


def _platform_url_errors(response) -> list[dict]:
    return [error for error in response.json()["detail"] if error["loc"] == ["body", "platform_url"]]


@pytest.mark.parametrize("platform_url", HOSTILE_PLATFORM_URLS)
def test_an_authenticated_registration_without_an_http_identity_is_a_422(client, monkeypatch, platform_url):
    monkeypatch.setattr(settings, "platform_registration_token", SECRET)

    response = _register(client, platform_url)

    assert response.status_code == 422
    assert len(response.json()["detail"]) == 1
    assert len(_platform_url_errors(response)) == 1


@pytest.mark.parametrize(
    ("platform_url", "error_type"),
    [
        ("javascript:alert(1)", "value_error"),
        ("", "string_too_short"),
        (_TOO_LONG, "string_too_long"),
    ],
)
def test_the_422_names_the_rule_not_a_server_error(client, monkeypatch, platform_url, error_type):
    monkeypatch.setattr(settings, "platform_registration_token", SECRET)

    [error] = _platform_url_errors(_register(client, platform_url))

    assert error["type"] == error_type
    if error_type == "value_error":
        assert error["msg"] == f"Value error, {PLATFORM_URL_INVALID_DETAIL}"


def test_a_missing_platform_url_is_a_422(client, monkeypatch):
    monkeypatch.setattr(settings, "platform_registration_token", SECRET)

    response = client.post(
        "/api/v1/platform/register",
        headers={"Authorization": f"Bearer {SECRET}"},
        json={"platform_identifier": "opencti"},
    )

    assert response.status_code == 422
    assert [error["type"] for error in _platform_url_errors(response)] == ["missing"]


def test_an_unauthenticated_hostile_body_is_refused_before_it_is_read(client, monkeypatch):
    # The token check precedes the body: an unauthenticated caller learns
    # nothing of the rule, and its body is never parsed nor stored.
    monkeypatch.setattr(settings, "platform_registration_token", SECRET)

    response = _register(client, "javascript:alert(1)", token=None)

    assert (response.status_code, response.json()["detail"]) == (401, "Missing registration token")


@pytest.mark.parametrize(
    "platform_url",
    [
        "https://opencti.example.com",
        "http://localhost:4000/",
        "HTTPS://OpenCTI.example.com:8443/opencti",
        "https://[2001:db8::1]:4000",
        f"{_HOST}/{'a' * (PLATFORM_URL_MAX_LENGTH - len(_HOST) - 1)}",
    ],
)
def test_an_http_identity_passes_the_body_check(platform_url):
    # The body model the route validates with (``_bounded_register_body``),
    # read as the route reads it: from the raw JSON.
    raw = json.dumps({"platform_identifier": "opencti", "platform_url": platform_url})

    assert RegisterRequest.model_validate_json(raw).platform_url == platform_url


def test_the_platform_url_is_kept_as_sent():
    body = RegisterRequest(platform_identifier="opencti", platform_url="HTTPS://OpenCTI.example.com/")

    assert body.platform_url == "HTTPS://OpenCTI.example.com/"
