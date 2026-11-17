"""Exposure data is rendered as presence, never by value (#5686).

Two halves:

* the table of ``app.integrations.credential_exposure``: which field names
  are secrets (snake / camel / kebab case, whole parts only), presence, the
  BIN + last four card mask, Luhn, and the value-shape scrub of free text;
* the tripwire: every provider tool that formats exposure data (SOCRadar,
  Flashpoint, Intel 471, Cybersixgill, Recorded Future identity) is fed a
  record carrying a password, a cookie, a token and a card number, through
  the tool itself with ``safe_request`` / the HTTP client faked, and none of
  the values may reach the tool result.

Every secret below is an obviously fake value; the card number is the
well-known Visa test number.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.integrations import credential_exposure as ce
from app.integrations.credential_exposure import (
    CAPTURED,
    NOT_CAPTURED,
    REDACTED,
    is_card_number_key,
    is_secret_key,
    luhn_valid,
    mask_card_number,
    presence,
    render_exposed_value,
    scrub_exposure_text,
)
from app.tools.base import CallableToolExecutor, ToolCall

_PASSWORD = "Fake-Passw0rd-xyz"
_COOKIE = "fake-cookie-value-0123"
_TOKEN_VALUE = "fake-token-value-4567"
_CARD = "4111111111111111"
_CARD_MASKED = "411111******1111"
_LEAKED = (_PASSWORD, _COOKIE, _TOKEN_VALUE, _CARD)


# ---------------------------------------------------------------------------
# Field names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "Password",
        "user_password",
        "passwordHash",
        "passwd",
        "pwd",
        "pass",
        "passphrase",
        "secret",
        "clientSecret",
        "token",
        "authToken",
        "access-token",
        "cookie",
        "sessionCookie",
        "cookies",
        "session",
        "sessionId",
        "sessionid",
        "PHPSESSID",
        "JSESSIONID",
        "sid",
        "connect.sid",
        "jwt",
        "Authorization",
        "Proxy-Authorization",
        "set-cookie",
        "seedPhrase",
        "ccv",
        "credential_details",
        "credentials",
        "cvv",
        "CVV",
        "cvv2",
        "cvc",
        "pin",
        "pinCode",
        "pan",
        "PAN",
        "ssn",
        "otp",
        "private_key",
        "privateKey",
        "apiKey",
        "api_key",
        "accessKey",
        "securityCode",
        "card_number",
        "cardNumber",
        "CardNumber",
        "ccNumber",
        "creditCard",
    ],
)
def test_secret_field_names(key):
    assert is_secret_key(key) is True


@pytest.mark.parametrize(
    "key",
    [
        "username",
        "email",
        "domain",
        "url",
        "passport",
        "spinner",
        "bypassCount",
        "tokenizer",
        "keyboard",
        "key",
        "card",
        "cardBrand",
        "bin",
        "expired",
        "status",
        "source",
        "discoveryDate",
        "malwareFamily",
    ],
)
def test_non_secret_field_names(key):
    assert is_secret_key(key) is False


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("card_number", True),
        ("cardNumber", True),
        ("ccNumber", True),
        ("creditCard", True),
        ("pan", True),
        ("cvv", False),
        ("password", False),
        ("cardBrand", False),
    ],
)
def test_card_number_field_names(key, expected):
    assert is_card_number_key(key) is expected


# ---------------------------------------------------------------------------
# Presence, card mask, Luhn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (_PASSWORD, CAPTURED),
        (True, CAPTURED),
        ("true", CAPTURED),
        (123, CAPTURED),
        ({"user": "a", "password": "b"}, CAPTURED),
        (["x"], CAPTURED),
        (False, NOT_CAPTURED),
        ("false", NOT_CAPTURED),
        ("", NOT_CAPTURED),
        ("   ", NOT_CAPTURED),
        (None, NOT_CAPTURED),
        (0, NOT_CAPTURED),
        ([], NOT_CAPTURED),
        ({}, NOT_CAPTURED),
    ],
)
def test_presence(value, expected):
    assert presence(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (_CARD, _CARD_MASKED),
        ("4111 1111 1111 1111", _CARD_MASKED),
        ("4111-1111-1111-1111", _CARD_MASKED),
        ("378282246310005", "378282*****0005"),
        ("4111111111111111111", "411111*********1111"),
        ("411111******1111", "411111**1111"),
        ("1234", CAPTURED),
        ("", NOT_CAPTURED),
        (None, NOT_CAPTURED),
    ],
)
def test_mask_card_number(value, expected):
    assert mask_card_number(value) == expected


@pytest.mark.parametrize(
    ("digits", "expected"),
    [
        (_CARD, True),
        ("378282246310005", True),
        ("5555555555554444", True),
        ("4111111111111112", False),
        ("41111111111111a1", False),
        ("", False),
    ],
)
def test_luhn(digits, expected):
    assert luhn_valid(digits) is expected


def test_render_exposed_value_secret_fields_never_by_value():
    assert render_exposed_value("password", _PASSWORD) == CAPTURED
    assert render_exposed_value("sessionCookie", "") == NOT_CAPTURED
    assert render_exposed_value("cvv", False) == NOT_CAPTURED
    assert render_exposed_value("cardNumber", _CARD) == _CARD_MASKED
    assert render_exposed_value("credentials", [{"password": _PASSWORD}]) == CAPTURED
    assert render_exposed_value("domain", "acme.example") == "acme.example"
    assert render_exposed_value("urls", ["a.example", "b.example"]) == "a.example, b.example"


# ---------------------------------------------------------------------------
# Value-shape scrub
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # account:password pairs keep the account.
        ("user:pass leaked for acme.com", f"user:{REDACTED} leaked for acme.com"),
        (f"alice@acme.example:{_PASSWORD} leaked", f"alice@acme.example:{REDACTED} leaked"),
        (f"jdoe.admin:{_PASSWORD}", f"jdoe.admin:{REDACTED}"),
        # Stealer-log line: URL, then account:password.
        (
            f"https://login.acme.example/:carol@acme.example:{_PASSWORD}",
            f"https://login.acme.example/:carol@acme.example:{REDACTED}",
        ),
        # URL userinfo.
        (
            f"https://bob:{_PASSWORD}@login.acme.example/path",
            f"https://bob:{REDACTED}@login.acme.example/path",
        ),
        (
            f"ftp://svc:{_PASSWORD}@files.acme.example",
            f"ftp://svc:{REDACTED}@files.acme.example",
        ),
        ("https://bob:p@ss@login.acme.example/x?u=a@b", f"https://bob:{REDACTED}@login.acme.example/x?u=a@b"),
        # Secret-named assignments, any quoting.
        (f"password={_PASSWORD}; other=1", f"password={REDACTED} other=1"),
        (f"Password: {_PASSWORD}", f"Password: {REDACTED}"),
        ('{"password": "my pass phrase", "user": "dave"}', f'{{"password": "{REDACTED}", "user": "dave"}}'),
        (f"cookie: sessionid={_COOKIE}; path=/", f"cookie: {REDACTED}"),
        (f"https://host.example/cb?token={_TOKEN_VALUE}&x=1", f"https://host.example/cb?token={REDACTED}"),
        (f"note:password={_PASSWORD}", f"note:{REDACTED}"),
        (f"card_number={_CARD}", f"card_number={_CARD_MASKED}"),
        # A secret runs to the end of its token: a delimiter inside it never
        # ends the mask.
        ("jdoe:Pa$$;w0rd leaked", f"jdoe:{REDACTED} leaked"),
        ("jdoe:Pa$$,w0rd|x) leaked", f"jdoe:{REDACTED} leaked"),
        ("jdoe.admin:1234.5678", f"jdoe.admin:{REDACTED}"),
        ("password=Ab&12cd", f"password={REDACTED}"),
        ("token=abc}def]ghi", f"token={REDACTED}"),
        ("jwt=abc.def.ghi", f"jwt={REDACTED}"),
        ("cookie sessionid=a1b2c3d4e5 leaked", f"cookie sessionid={REDACTED} leaked"),
        # A cookie / authorization header is masked to the end of its line.
        ("cookie: a_sid=X1; b_sid=Y2", f"cookie: {REDACTED}"),
        ("Set-Cookie: sid=X1; HttpOnly; Path=/\nHost: acme.example", f"Set-Cookie: {REDACTED}\nHost: acme.example"),
        ("Authorization: Bearer x;y", f"Authorization: {REDACTED}"),
        ("proxy-authorization=Basic a b c\r\nnext", f"proxy-authorization={REDACTED}\r\nnext"),
        # Quoted values: an escaped or other quote inside does not end them.
        ('{"password": "Hun\\"ter2", "user": "dave"}', f'{{"password": "{REDACTED}", "user": "dave"}}'),
        ('{"password": "it\'s-secret", "user": "dave"}', f'{{"password": "{REDACTED}", "user": "dave"}}'),
        ("{'password': 'say \"hi\"', 'user': 'dave'}", f"{{'password': '{REDACTED}', 'user': 'dave'}}"),
        (
            'payload {\\"password\\": \\"my pass\\", \\"user\\": \\"dave\\"}',
            f'payload {{\\"password\\": \\"{REDACTED}\\", \\"user\\": \\"dave\\"}}',
        ),
        ('password: "never closed; on this line\nnext', f'password: "{REDACTED}\nnext'),
        # Several secrets on one line, a secret at the end, an empty value.
        ("user=jdoe password=a;b token=c&d end", f"user=jdoe password={REDACTED} token={REDACTED} end"),
        ("10:30,user:pass", f"10:30,user:{REDACTED}"),
        (f"leaked password={_PASSWORD}", f"leaked password={REDACTED}"),
        ("leaked password=", "leaked password="),
        ('password="" end', 'password="" end'),
        # The combo-list shape: email|password.
        ("user@example.com|Hunter2;x", f"user@example.com|{REDACTED}"),
        ("https://site.example|jdoe@acme.example|Hunter2!", f"https://site.example|jdoe@acme.example|{REDACTED}"),
        (f"card_number={_CARD};", f"card_number={_CARD_MASKED}"),
        # Luhn-valid card numbers, grouped or not.
        (f"card {_CARD} exp 12/27", f"card {_CARD_MASKED} exp 12/27"),
        ("card 4111 1111 1111 1111 seen", f"card {_CARD_MASKED} seen"),
        # Shapes that are not secrets stay as they are.
        ("id 4111111111111112 not luhn", "id 4111111111111112 not luhn"),
        ("Date: 2026-07-02 08:00", "Date: 2026-07-02 08:00"),
        ("host acme.example:8443", "host acme.example:8443"),
        ("hosts acme.example:8443, b.example:443/path.", "hosts acme.example:8443, b.example:443/path."),
        ("a|b and site.example|x", "a|b and site.example|x"),
        ("session://host", "session://host"),
        ("ip 2001:db8::1", "ip 2001:db8::1"),
        ("Suspicious lookalike domain detected.", "Suspicious lookalike domain detected."),
        ("https://acme.example/login", "https://acme.example/login"),
        ("", ""),
        (None, ""),
    ],
)
def test_scrub_exposure_text(text, expected):
    assert scrub_exposure_text(text) == expected


def test_scrub_exposure_text_runs_the_shared_token_shapes():
    # Built from parts so no JWT-shaped literal sits in the source for the secret scan.
    jwt = ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiJmYWtlIn0", "ZmFrZS1zaWduYXR1cmU"])
    assert scrub_exposure_text(f"session {jwt}") == "session <redacted-jwt>"


@pytest.mark.parametrize(
    "hostile",
    [
        "a:" * 20_000,
        "x=" * 20_000,
        "1-" * 20_000,
        "password=" * 5_000,
        "[redacted]" * 4_000,
        "a@" * 20_000,
        'password=\\"a ' * 2_000,
        'password="a ' * 2_000,
        "a.b:1 " * 2_000,
        "a@b|" * 4_000,
    ],
)
def test_scrub_exposure_text_is_bounded(hostile):
    started = time.monotonic()
    out = scrub_exposure_text(hostile)
    assert len(out) <= ce.MAX_SCRUB_CHARS
    assert time.monotonic() - started < 2.0


# ---------------------------------------------------------------------------
# Tripwire: every provider that formats exposure data goes through the rule
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, payload: Any):
        self._payload = payload
        self.status_code = 200
        self.is_success = True
        self.content = b"x"
        self.text = ""
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        return self._payload


def _fake_safe_request(payload: Any):
    async def _call(_client, _method, _url, _provider, _action, **_kwargs):
        return _Response(payload), None

    return _call


def _leaked_record() -> dict[str, Any]:
    return {
        "password": _PASSWORD,
        "sessionCookie": _COOKIE,
        "authToken": _TOKEN_VALUE,
        "cardNumber": _CARD,
    }


async def _socradar(monkeypatch) -> str:
    from app.integrations.providers import socradar

    finding = {
        "alarmId": 1,
        "status": "OPEN",
        "contentPreview": f"jdoe@acme.example:{_PASSWORD} cookie={_COOKIE}",
        **_leaked_record(),
    }
    monkeypatch.setattr(
        socradar,
        "safe_request",
        _fake_safe_request({"is_success": True, "data": {"data": [finding]}}),
    )
    executor = CallableToolExecutor()
    socradar.build_tools(token="https://platform.socradar.com/api||1||fake-key", executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="t", name="search_socradar_dark_web", arguments={"source": "botnet_data"})
    )
    return result.content


async def _flashpoint(monkeypatch) -> str:
    from app.integrations.providers import flashpoint

    hit = {
        "_source": {
            "email": "jdoe@acme.example",
            "fpid": "fp-1",
            "affected_url": f"https://jdoe:{_PASSWORD}@portal.acme.example",
            "password": _PASSWORD,
            "breach": {"title": "fake breach"},
        }
    }
    monkeypatch.setattr(
        flashpoint, "safe_request", _fake_safe_request({"hits": {"total": 1, "hits": [hit]}})
    )
    executor = CallableToolExecutor()
    flashpoint.build_tools(token="https://api.flashpoint.io||fake-token", executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="t", name="search_flashpoint_credentials", arguments={})
    )
    return result.content


async def _intel471(monkeypatch) -> str:
    from app.integrations.providers.intel471 import _common
    from app.integrations.providers.intel471 import build_tools as intel471_tools

    payload = {
        "credentialTotalCount": 1,
        "credentials": [
            {"uid": "c1", "data": {"accessedUrl": f"https://jdoe:{_PASSWORD}@vpn.acme.example"}},
        ],
    }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, *_args, **_kwargs):
            return _Response(payload)

    monkeypatch.setattr(_common.httpx, "AsyncClient", lambda *a, **k: _Client())
    executor = CallableToolExecutor()
    intel471_tools("analyst@acme.example||fake-key", executor)
    result = await executor.execute_tool(
        ToolCall(id="t", name="search_intel471_credentials", arguments={})
    )
    return result.content


async def _cybersixgill(monkeypatch) -> str:
    from app.integrations.providers import cybersixgill

    items = {
        "content": {
            "items": [
                {
                    "_source": {
                        "title": f"combo jdoe@acme.example:{_PASSWORD}",
                        "creator": "actor",
                        "site": "forum",
                        "content": f"password={_PASSWORD} cookie: {_COOKIE} card {_CARD}",
                    }
                }
            ]
        }
    }
    monkeypatch.setattr(cybersixgill, "safe_request", _fake_safe_request(items))

    async def _mint(*_args):
        return "fake-bearer", 1500

    monkeypatch.setattr(cybersixgill, "_request_access_token", _mint)
    monkeypatch.setattr(cybersixgill, "get_cached_app_token", lambda _key: None)
    monkeypatch.setattr(cybersixgill, "set_cached_app_token", lambda *_args: None)
    executor = CallableToolExecutor()
    cybersixgill.build_tools(
        token="https://api.cybersixgill.com||fake-client||||fake-secret", executor=executor
    )
    result = await executor.execute_tool(
        ToolCall(id="t", name="get_cybersixgill_alert_content", arguments={"alert_id": "a1"})
    )
    return result.content


async def _recorded_future_identity(monkeypatch) -> str:
    from app.integrations.providers import recorded_future
    from app.integrations.providers.recorded_future import management

    payload = {
        "identities": [
            {
                "identity": {"subjects": ["jdoe@acme.example"]},
                "credentials": [
                    {
                        "exposed_secret": {
                            "type": "clear",
                            "clear_text_value": _PASSWORD,
                            "details": {"type": "clear", "clear_text_hint": _PASSWORD},
                        },
                        "authorization_service": {"domain": "portal.acme.example"},
                        "cookies": [_COOKIE],
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(management, "safe_request", _fake_safe_request(payload))
    executor = CallableToolExecutor()
    recorded_future.build_tools(token="https://api.recordedfuture.com||fake-token", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="t",
            name="lookup_rf_identity_exposures",
            arguments={"subjects": "jdoe@acme.example"},
        )
    )
    return result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [_socradar, _flashpoint, _intel471, _cybersixgill, _recorded_future_identity],
    ids=["socradar", "flashpoint", "intel471", "cybersixgill", "recorded_future_identity"],
)
async def test_exposure_tools_never_return_a_leaked_secret(monkeypatch, call):
    content = await call(monkeypatch)
    assert content
    assert "jdoe" in content  # the account stays visible
    for secret in _LEAKED:
        assert secret not in content
