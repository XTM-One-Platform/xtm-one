"""Cross-platform tokens verified and addressed by identity on internal URLs.

In the standard Docker deployments a product has one URL for its users and
another for XTM One: OpenCTI is ``http://localhost:8080`` in the browser and
``http://opencti:8080`` from the XTM One container. Its identity stays the URL
it registered with; XTM One fetches its keys, and sends OpenAEV's run
callbacks, on the internal URL captured at registration
(``{PROVIDER}_API_URL``), and every token keeps naming the identity: the
``iss`` it is trusted on, the ``aud`` it is addressed to. Identities are
compared normalized, and XTM One publishes its own at ``/xtm/auth/metadata``.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import xtm_auth as xtm_auth_api
from app.core import platform_identity, xtm_auth
from app.core.base_path import RESERVED_FIRST_SEGMENTS, BasePathMiddleware
from app.core.config import settings
from app.core.platform_identity import (
    PlatformCall,
    captured_api_url,
    platform_token_audience,
    resolve_platform_call,
)
from app.core.security import encrypt_value
from app.core.url_identity import (
    URLSettingError,
    normalize_url_identity,
    parse_url_identity_list,
    redact_url,
    same_url_identity,
)
from app.core.xtm_auth import _kid, _private_key, _public_key_raw, verify_xtm_jwt
from app.models.integration import Integration
from app.models.platform_registration import PlatformRegistration

PUBLIC = "http://localhost:8080"
INTERNAL = "http://opencti:8080"
OPENAEV_PUBLIC = "http://localhost:8081"
OPENAEV_INTERNAL = "http://openaev:8080"
XTM_ONE = "http://localhost:8090"


@pytest.fixture(autouse=True)
def _xtm_one_identity(monkeypatch):
    monkeypatch.setattr(settings, "base_url", XTM_ONE)
    monkeypatch.setattr(settings, "xtm_auth_accepted_audiences", [])


def _registration(platform_url: str = PUBLIC, api_url: str | None = None, identifier: str = "opencti"):
    return SimpleNamespace(platform_identifier=identifier, platform_url=platform_url, api_url=api_url)


class _Result:
    def __init__(self, rows: list, scalar: object = None) -> None:
        self._rows = rows
        self._scalar = scalar

    def all(self) -> list:
        return list(self._rows)

    def first(self) -> object:
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self) -> object:
        return self._scalar


class _Db:
    """A session holding *registrations*: every ``platform_registrations`` query
    answers them, any other query answers *scalar* (a user, an integration)."""

    def __init__(self, *registrations, scalar: object = None) -> None:
        self.registrations = list(registrations)
        self.scalar = scalar

    async def execute(self, statement):
        if "platform_registrations" in str(statement):
            return _Result(self.registrations)
        return _Result([], self.scalar)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _token(*, iss: str = PUBLIC, aud: object = None, omit: tuple[str, ...] = ()) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": iss,
        "sub": str(uuid.uuid4()),
        "email": "analyst@example.com",
        "aud": XTM_ONE if aud is None else aud,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "jti": str(uuid.uuid4()),
    }
    for claim in omit:
        payload.pop(claim)
    return pyjwt.encode(payload, _private_key, algorithm="EdDSA", headers={"kid": _kid})


@pytest.fixture
def fetched_keys(monkeypatch) -> list[str]:
    """Every JWKS URL a verification fetches keys from; serves this module's own key."""
    fetched: list[str] = []

    class _Client:
        def __init__(self, url: str) -> None:
            self.url = url

        def get_signing_key_from_jwt(self, _token: str):
            fetched.append(self.url)
            return SimpleNamespace(key=Ed25519PublicKey.from_public_bytes(_public_key_raw))

    monkeypatch.setattr("app.core.jwks.get_jwk_client", lambda url, **_kwargs: _Client(url))
    return fetched


# ---------------------------------------------------------------------------
# Identities, normalized
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spelling", "identity"),
    [
        ("HTTP://LocalHost:8080/", "http://localhost:8080"),
        ("https://opencti.example:443", "https://opencti.example"),
        ("http://opencti.example:80/", "http://opencti.example"),
        ("https://Host.example/Base/Path/", "https://host.example/Base/Path"),
        ("http://[::1]:8080/", "http://[::1]:8080"),
    ],
)
def test_an_identity_is_its_canonical_form(spelling, identity):
    assert normalize_url_identity(spelling) == identity


@pytest.mark.parametrize("url", ["https://alice@host.example", "https://alice:secret@host.example", "https://@host"])
def test_a_url_with_userinfo_names_no_identity(url):
    """Dropping it would make ``https://alice@host`` and ``https://bob@host`` one identity."""
    with pytest.raises(ValueError, match="userinfo"):
        normalize_url_identity(url)
    assert not same_url_identity(url, "https://host.example")
    assert captured_api_url(url, PUBLIC) is None


def test_a_configured_url_keeps_dropping_its_userinfo():
    """``BASE_URL`` and a run's callback base URL were always read that way."""
    assert xtm_auth._normalize_url("https://alice:secret@host.example/") == "https://host.example"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("http://localhost:8080", "http://localhost:8081"),
        ("http://localhost:8080", "https://localhost:8080"),
        ("http://localhost:8080", "http://localhost.evil:8080"),
        ("https://host.example/base", "https://host.example/Base"),
        ("https://host.example", "https://host.example/base"),
        ("https://host.example", "not a url"),
    ],
)
def test_two_identities_that_differ_are_never_the_same(left, right):
    assert not same_url_identity(left, right)


def test_an_accepted_audience_list_is_canonical_and_a_malformed_entry_refused():
    assert parse_url_identity_list("HTTP://XTM-One:4000/, https://a.example:443 http://xtm-one:4000", setting="X") == [
        "http://xtm-one:4000",
        "https://a.example",
    ]
    assert parse_url_identity_list("", setting="X") == []
    with pytest.raises(URLSettingError, match="X entry 'xtm-one:4000'"):
        parse_url_identity_list("xtm-one:4000", setting="X")
    with pytest.raises(URLSettingError, match="userinfo") as refused:
        parse_url_identity_list("http://admin:hunter2@xtm-one:4000", setting="X")
    assert "hunter2" not in str(refused.value)
    assert "admin" not in str(refused.value)


def test_the_setting_is_parsed_at_startup_and_empty_by_default(monkeypatch):
    from app.core.config import Settings

    monkeypatch.delenv("XTM_AUTH_ACCEPTED_AUDIENCES", raising=False)
    assert Settings(_env_file=None).xtm_auth_accepted_audiences == []
    monkeypatch.setenv("XTM_AUTH_ACCEPTED_AUDIENCES", "HTTP://XTM-One:4000/")
    assert Settings(_env_file=None).xtm_auth_accepted_audiences == ["http://xtm-one:4000"]
    monkeypatch.setenv("XTM_AUTH_ACCEPTED_AUDIENCES", "ftp://xtm-one")
    with pytest.raises(URLSettingError, match="XTM_AUTH_ACCEPTED_AUDIENCES"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "entry",
    [
        "http://admin:hunter2@xtm-one:4000",
        "admin:hunter2@xtm-one:4000",
        "https:/admin:hunter2@xtm-one",
        "https://admin:hun/ter2@xtm-one",
        "https://admin:hun?ter2@xtm-one",
        "https://admin:hun#ter2@xtm-one",
        "http://xtm-one:4000/?token=hunter2",
    ],
)
def test_a_refused_entry_never_prints_its_credential(monkeypatch, entry):
    """pydantic quotes the raw input of a ValueError; the refusal is raised
    past it, and names the entry with its credentials masked."""
    from app.core.config import Settings

    monkeypatch.setenv("XTM_AUTH_ACCEPTED_AUDIENCES", entry)
    with pytest.raises(URLSettingError) as refused:
        Settings(_env_file=None)
    assert not isinstance(refused.value, ValueError)
    assert "hunter2" not in str(refused.value)
    assert "hunter2" not in redact_url(entry)


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("https://alice:secret@host.example/x", "https://***@host.example/x"),
        ("alice:secret@host.example", "***@host.example"),
        ("https:/alice:secret@host.example", "https:/***@host.example"),
        ("https:///host.example", "https:///host.example"),
        ("https://alice:p/w@host.example", "https://***@host.example"),
        ("https://host.example/?key=secret#frag", "https://host.example/?***"),
        ("https://host.example", "https://host.example"),
    ],
)
def test_a_url_is_quoted_without_its_credentials(value, shown):
    assert redact_url(value) == shown


def test_port_0_names_no_identity():
    """Normalization drops a default port, and ``:0`` must not read as one."""
    with pytest.raises(ValueError, match="port 0"):
        normalize_url_identity("http://localhost:0")
    assert not same_url_identity("http://localhost:0", "http://localhost")
    assert xtm_auth._normalize_url("http://localhost:0") == "http://localhost"


# ---------------------------------------------------------------------------
# Incoming tokens: trusted on the identity, keys fetched where XTM One reaches it
# ---------------------------------------------------------------------------


async def test_keys_are_fetched_on_the_internal_url_while_the_token_names_the_public_identity(fetched_keys):
    payload = await verify_xtm_jwt(_token(iss=PUBLIC), _Db(_registration(api_url=INTERNAL)))
    assert payload is not None
    assert payload.iss == PUBLIC
    assert payload.platform_identifier == "opencti"
    assert fetched_keys == [f"{INTERNAL}/xtm/auth/jwks"]


async def test_without_an_internal_url_the_keys_are_fetched_on_the_registered_identity(fetched_keys):
    """Never on the token's own spelling of it: a caller never picks where keys come from."""
    payload = await verify_xtm_jwt(_token(iss="HTTP://LocalHost:8080/"), _Db(_registration()))
    assert payload is not None
    assert fetched_keys == [f"{PUBLIC}/xtm/auth/jwks"]


async def test_a_trailing_slash_identity_fetches_its_keys_on_a_single_slash(fetched_keys):
    """The token names its issuer ``.../``; the keys are never asked at ``//xtm/auth/jwks``."""
    payload = await verify_xtm_jwt(_token(iss=PUBLIC + "/"), _Db(_registration(platform_url=PUBLIC + "/")))
    assert payload is not None
    assert fetched_keys == [f"{PUBLIC}/xtm/auth/jwks"]


async def test_keys_fall_back_to_the_registered_identity_when_the_internal_url_fails(monkeypatch):
    """The internal URL not serving the keys never refuses a token the identity
    verifies (where the keys are fetched without an internal URL), and the
    token's own spelling is never asked."""
    from jwt import PyJWKClientError

    fetched: list[str] = []

    class _Client:
        def __init__(self, url: str) -> None:
            self.url = url

        def get_signing_key_from_jwt(self, _token: str):
            fetched.append(self.url)
            if self.url.startswith(INTERNAL):
                raise PyJWKClientError("connection refused")
            return SimpleNamespace(key=Ed25519PublicKey.from_public_bytes(_public_key_raw))

    monkeypatch.setattr("app.core.jwks.get_jwk_client", lambda url, **_kwargs: _Client(url))
    registration = _registration(platform_url="http://LocalHost:8080", api_url=INTERNAL)
    payload = await verify_xtm_jwt(_token(iss="http://localhost:8080/"), _Db(registration))
    assert payload is not None
    assert fetched == [f"{INTERNAL}/xtm/auth/jwks", "http://LocalHost:8080/xtm/auth/jwks"]


@pytest.mark.parametrize("stored", ["file:///etc/passwd", "http://alice:pw@opencti:8080", "opencti:8080"])
async def test_a_stored_internal_url_that_names_no_identity_is_never_asked(fetched_keys, stored):
    """Whatever wrote the column, urllib is never handed a non-http(s) key location."""
    payload = await verify_xtm_jwt(_token(), _Db(_registration(api_url=stored)))
    assert payload is not None
    assert fetched_keys == [f"{PUBLIC}/xtm/auth/jwks"]


async def test_the_internal_url_is_not_an_identity(fetched_keys):
    assert await verify_xtm_jwt(_token(iss=INTERNAL), _Db(_registration(api_url=INTERNAL))) is None
    assert fetched_keys == []


@pytest.mark.parametrize(
    "issuer",
    [
        "http://evil.example:8080",
        "http://localhost:8081",
        "https://localhost:8080",
        "http://localhost:8080/other",
        "http://evil@localhost:8080",
    ],
)
async def test_an_issuer_that_is_no_registered_identity_is_refused(fetched_keys, issuer):
    assert await verify_xtm_jwt(_token(iss=issuer), _Db(_registration(api_url=INTERNAL))) is None
    assert fetched_keys == []


@pytest.mark.parametrize("issuer", ["http://localhost:8080/", "HTTP://LOCALHOST:8080", "http://LocalHost:8080//"])
async def test_an_issuer_spelled_otherwise_is_the_registered_identity(fetched_keys, issuer):
    assert await verify_xtm_jwt(_token(iss=issuer), _Db(_registration())) is not None


async def test_an_exact_spelling_wins_over_a_normalized_one(fetched_keys):
    db = _Db(
        _registration(platform_url="http://LOCALHOST:8080", api_url="http://other:8080", identifier="openbas"),
        _registration(platform_url=PUBLIC, api_url=INTERNAL),
    )
    payload = await verify_xtm_jwt(_token(iss=PUBLIC), db)
    assert payload is not None and payload.platform_identifier == "opencti"
    assert fetched_keys == [f"{INTERNAL}/xtm/auth/jwks"]


# ---------------------------------------------------------------------------
# Incoming tokens: the audience, normalized, and XTM_AUTH_ACCEPTED_AUDIENCES
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "audience",
    ["http://localhost:8090/", "HTTP://LocalHost:8090", ["http://other.example", "http://localhost:8090/"]],
)
async def test_an_audience_spelled_otherwise_is_this_platform(fetched_keys, audience):
    assert await verify_xtm_jwt(_token(aud=audience), _Db(_registration())) is not None


@pytest.mark.parametrize(
    "audience",
    [
        "http://localhost:8091",
        "https://localhost:8090",
        "http://localhost:8090/other",
        "http://xtm-one:4000",
        "http://someone@localhost:8090",
        [],
        ["http://xtm-one:4000"],
        42,
    ],
)
async def test_an_audience_that_is_not_this_platform_is_refused(fetched_keys, audience):
    assert await verify_xtm_jwt(_token(aud=audience), _Db(_registration())) is None


async def test_a_token_without_an_audience_is_refused(fetched_keys):
    assert await verify_xtm_jwt(_token(omit=("aud",)), _Db(_registration())) is None


async def test_an_audience_on_port_0_is_not_the_default_port(fetched_keys, monkeypatch):
    monkeypatch.setattr(settings, "base_url", "http://xtm.example")
    db = _Db(_registration())
    assert await verify_xtm_jwt(_token(aud="http://xtm.example"), db) is not None
    assert await verify_xtm_jwt(_token(aud="http://xtm.example:0"), db) is None
    assert await verify_xtm_jwt(_token(aud="http://someone@xtm.example"), db) is None


async def test_an_internal_audience_is_accepted_only_once_configured(fetched_keys, monkeypatch, caplog):
    db = _Db(_registration())
    with caplog.at_level(logging.WARNING, logger="app.core.xtm_auth"):
        assert await verify_xtm_jwt(_token(aud="http://xtm-one:4000"), db) is None
    assert any("audience mismatch" in record.getMessage() for record in caplog.records)

    monkeypatch.setattr(settings, "xtm_auth_accepted_audiences", ["http://xtm-one:4000"])
    assert await verify_xtm_jwt(_token(aud="http://xtm-one:4000"), db) is not None
    assert await verify_xtm_jwt(_token(aud="http://XTM-One:4000/"), db) is not None
    assert await verify_xtm_jwt(_token(aud=XTM_ONE), db) is not None
    assert await verify_xtm_jwt(_token(aud="http://other:4000"), db) is None


def test_the_extra_audiences_are_logged_at_startup(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.xtm_auth"):
        xtm_auth.log_accepted_audiences()
    assert caplog.records == []
    monkeypatch.setattr(settings, "xtm_auth_accepted_audiences", ["http://xtm-one:4000"])
    with caplog.at_level(logging.WARNING, logger="app.core.xtm_auth"):
        xtm_auth.log_accepted_audiences()
    assert "http://xtm-one:4000" in caplog.records[-1].getMessage()


# ---------------------------------------------------------------------------
# Outgoing tokens: addressed to the registered identity
# ---------------------------------------------------------------------------


async def test_a_token_sent_to_the_internal_url_is_addressed_to_the_identity():
    db = _Db(_registration(OPENAEV_PUBLIC, OPENAEV_INTERNAL, "openaev"))
    assert await platform_token_audience(db, uuid.uuid4(), OPENAEV_INTERNAL) == OPENAEV_PUBLIC


async def test_a_token_sent_to_the_identity_is_addressed_as_before():
    db = _Db(_registration(OPENAEV_PUBLIC + "/", None, "openaev"))
    assert await platform_token_audience(db, uuid.uuid4(), OPENAEV_PUBLIC) == OPENAEV_PUBLIC


@pytest.mark.parametrize(
    "db",
    [
        _Db(),
        _Db(_registration(OPENAEV_PUBLIC, OPENAEV_INTERNAL, "openaev")),
    ],
    ids=["no-registration", "edited-base-url"],
)
async def test_a_url_the_registration_does_not_vouch_for_never_receives_an_identity_token(db):
    """An integration's ``base_url`` can be edited; a token addressed to the
    real platform must not follow it to another host."""
    assert await platform_token_audience(db, uuid.uuid4(), "http://elsewhere:8080") == "http://elsewhere:8080"


def _platform_integration(base_url: str) -> Integration:
    return Integration(
        id=uuid.uuid4(),
        provider="openaev",
        name="OpenAEV",
        auth_type="platform_managed",
        extra_config_enc=encrypt_value(json.dumps({"base_url": base_url})),
        is_company_managed=True,
        status="connected",
    )


async def test_the_platform_tools_call_the_internal_url_with_a_token_addressed_to_the_identity(monkeypatch):
    from app.services import integration_tools

    audiences: list[str] = []

    async def _mint(_db, _user_id, audience_url):
        audiences.append(audience_url)
        return "signed"

    monkeypatch.setattr(integration_tools, "_generate_platform_jwt", _mint)
    integ = _platform_integration(OPENAEV_INTERNAL)
    db = _Db(_registration(OPENAEV_PUBLIC, OPENAEV_INTERNAL, "openaev"))

    token = await integration_tools._get_token(db, str(integ.id), "openaev", user_id=str(uuid.uuid4()), integ=integ)

    assert token == f"{OPENAEV_INTERNAL}||signed"
    assert audiences == [OPENAEV_PUBLIC]


async def test_a_mid_run_re_mint_keeps_the_identity_audience(monkeypatch):
    from app.services import integration_tools

    audiences: list[str] = []

    async def _mint(_db, _user_id, audience_url):
        audiences.append(audience_url)
        return "signed"

    integ = _platform_integration(OPENAEV_INTERNAL)
    monkeypatch.setattr(integration_tools, "_generate_platform_jwt", _mint)
    monkeypatch.setattr(
        "app.core.database.async_session",
        lambda: _Db(_registration(OPENAEV_PUBLIC, OPENAEV_INTERNAL, "openaev"), scalar=integ),
    )

    refresh = integration_tools._make_platform_jwt_refresh_fn(str(integ.id), str(uuid.uuid4()))
    assert await refresh() == "signed"
    assert audiences == [OPENAEV_PUBLIC]


# ---------------------------------------------------------------------------
# OpenAEV autonomous run callbacks: to the internal URL, addressed to the identity
# ---------------------------------------------------------------------------


async def test_a_callback_to_a_registered_identity_goes_to_its_internal_url():
    db = _Db(_registration(OPENAEV_PUBLIC, OPENAEV_INTERNAL, "openaev"))
    assert await resolve_platform_call(db, OPENAEV_PUBLIC + "/") == PlatformCall(OPENAEV_INTERNAL, OPENAEV_PUBLIC)


async def test_a_callback_is_addressed_to_the_identity_as_registered():
    """A platform comparing ``aud`` exactly knows itself by the URL it registered with."""
    db = _Db(_registration("http://LocalHost:8081", None, "openaev"))
    assert await resolve_platform_call(db, OPENAEV_PUBLIC) == PlatformCall(OPENAEV_PUBLIC, "http://LocalHost:8081")


async def test_a_callback_to_an_internal_url_is_addressed_to_its_identity():
    db = _Db(_registration(OPENAEV_PUBLIC, OPENAEV_INTERNAL, "openaev"))
    assert await resolve_platform_call(db, OPENAEV_INTERNAL) == PlatformCall(OPENAEV_INTERNAL, OPENAEV_PUBLIC)


@pytest.mark.parametrize(
    ("db", "url"),
    [
        (_Db(_registration(OPENAEV_PUBLIC, None, "openaev")), OPENAEV_PUBLIC),
        (_Db(_registration(OPENAEV_PUBLIC, OPENAEV_INTERNAL, "openaev")), "https://aev.example"),
        (_Db(), OPENAEV_PUBLIC),
    ],
    ids=["no-internal-url", "unregistered-url", "no-registration"],
)
async def test_any_other_callback_is_sent_and_addressed_as_before(db, url):
    assert await resolve_platform_call(db, url) == PlatformCall(url, url)


async def test_an_unreadable_registration_leaves_the_callback_as_given():
    class _Broken:
        async def execute(self, _statement):
            raise RuntimeError("database unavailable")

    assert await resolve_platform_call(_Broken(), OPENAEV_PUBLIC) == PlatformCall(OPENAEV_PUBLIC, OPENAEV_PUBLIC)


@pytest.fixture
def callbacks(monkeypatch):
    """Every run callback XTM One sends OpenAEV, as ``(method, url, aud)``."""
    import httpx

    sent: list[tuple[str, str, str]] = []
    user = SimpleNamespace(email="operator@example.com")
    monkeypatch.setattr(
        "app.core.database.async_session",
        lambda: _Db(_registration(OPENAEV_PUBLIC, OPENAEV_INTERNAL, "openaev"), scalar=user),
    )

    def _audience(headers: dict) -> str:
        token = headers["Authorization"].removeprefix("Bearer ")
        return pyjwt.decode(token, options={"verify_signature": False})["aud"]

    class _Response:
        status_code = 200

        def json(self):
            return {"autonomous_run_status": "RUNNING"}

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url, headers=None):
            sent.append(("GET", url, _audience(headers)))
            return _Response()

        async def post(self, url, headers=None, json=None):
            sent.append(("POST", url, _audience(headers)))
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return sent


_RUN = {"openaev_run_id": "run-1", "user_id": str(uuid.uuid4()), "callback_base_url": OPENAEV_PUBLIC}


async def test_every_run_callback_goes_to_the_internal_url_addressed_to_the_identity(callbacks):
    from app.services import autonomous_pentest as ap
    from app.services.orchestration_adapters import autonomous as adapter

    assert await ap._read_openaev_run_status(_RUN) == "RUNNING"
    await ap._read_openaev_run_scope(_RUN)
    await ap._read_openaev_run_directives(_RUN)
    await ap._post_autonomous_event(_RUN, {"type": "STATUS"})
    await ap._post_openaev_run_status(_RUN, status="RUNNING", title="Run started", content="")
    await adapter._post_run_status(
        base_url=OPENAEV_PUBLIC, openaev_run_id="run-1", user_id=_RUN["user_id"], body={"status": "FAILED"}
    )

    base = f"{OPENAEV_INTERNAL}/api/autonomous-runs/run-1"
    assert callbacks == [
        ("GET", base, OPENAEV_PUBLIC),
        ("GET", f"{base}/scope", OPENAEV_PUBLIC),
        ("GET", f"{base}/directives", OPENAEV_PUBLIC),
        ("POST", f"{base}/events", OPENAEV_PUBLIC),
        ("POST", f"{base}/status", OPENAEV_PUBLIC),
        ("POST", f"{base}/status", OPENAEV_PUBLIC),
    ]


# ---------------------------------------------------------------------------
# Registration captures the internal URL from XTM One's own environment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "captured"),
    [
        ("", None),
        ("  http://opencti:8080/ ", "http://opencti:8080"),
        ("HTTPS://Platform.example:443/", None),
        ("opencti:8080", None),
        ("file:///etc/passwd", None),
        ("http://alice:pw@opencti:8080", None),
        ("http://opencti:0", None),
        ("http://opencti:8080/?x=1", None),
    ],
)
def test_the_registration_records_an_internal_url_only_when_it_is_another_http_url(override, captured):
    assert captured_api_url(override, "https://platform.example") == captured


def test_an_unusable_internal_url_is_warned_about_once(monkeypatch, caplog):
    monkeypatch.setattr(platform_identity, "_warned_unusable_api_urls", set())
    with caplog.at_level(logging.WARNING, logger="app.core.platform_identity"):
        captured_api_url("opencti:8080", PUBLIC)
        captured_api_url("opencti:8080", PUBLIC)
        captured_api_url("http://alice:hunter2@opencti:8080", PUBLIC)
    assert len(caplog.records) == 2
    assert "hunter2" not in caplog.text


class _RegistrationSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    async def execute(self, _statement):
        return _Result([], None)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def refresh(self, _obj: object) -> None:
        return None


@pytest.mark.parametrize(("override", "api_url"), [("http://opencti:8080", "http://opencti:8080"), (None, None)])
def test_registering_keeps_the_identity_and_records_where_xtm_one_reaches_it(monkeypatch, override, api_url):
    from app.api.v1 import helpers as helpers_api
    from app.api.v1 import platform
    from app.core.security import decrypt_value
    from app.enterprise.licensing import clear_license_cache

    async def _nothing(*_args, **_kwargs):
        return None

    async def _load_ps(_db):
        return SimpleNamespace(enterprise_license=None, created_at=datetime.now(UTC) - timedelta(days=30))

    for var in ("OPENCTI_API_URL", "OPENAEV_API_URL", "OPENCRQ_API_URL", "OPENGRC_API_URL"):
        monkeypatch.delenv(var, raising=False)
    if override:
        monkeypatch.setenv("OPENCTI_API_URL", override)
    monkeypatch.setattr(settings, "platform_registration_token", "test-registration-token")
    monkeypatch.setattr(settings, "enterprise_license", "")
    monkeypatch.setattr(platform, "_load_ps", _load_ps)
    monkeypatch.setattr(platform, "_sync_platform_intents", _nothing)
    monkeypatch.setattr(helpers_api, "resolve_company_managed_owner_id", _nothing)
    clear_license_cache("xtm")

    session = _RegistrationSession()
    app = FastAPI()
    app.include_router(platform.router, prefix="/api/v1/platform")
    app.dependency_overrides[platform.get_db] = lambda: session
    response = TestClient(app).post(
        "/api/v1/platform/register",
        json={"platform_identifier": "opencti", "platform_url": PUBLIC},
        headers={"Authorization": "Bearer test-registration-token"},
    )
    clear_license_cache("xtm")

    assert response.status_code == 200, response.text
    (registration,) = [row for row in session.added if isinstance(row, PlatformRegistration)]
    (integration,) = [row for row in session.added if isinstance(row, Integration)]
    assert registration.platform_url == PUBLIC
    assert registration.api_url == api_url
    assert json.loads(decrypt_value(integration.extra_config_enc))["base_url"] == (override or PUBLIC)


# ---------------------------------------------------------------------------
# XTM One's own identity: GET /xtm/auth/metadata
# ---------------------------------------------------------------------------


def _auth_app(base_path: str = "") -> TestClient:
    app = FastAPI()
    app.include_router(xtm_auth_api.router, prefix="/xtm/auth")
    if base_path:
        app.add_middleware(BasePathMiddleware, base_path=base_path)
    return TestClient(app)


def test_the_metadata_names_the_issuer_and_audience_of_the_tokens(monkeypatch):
    monkeypatch.setattr(settings, "base_url", "HTTP://LocalHost:8090/")
    response = _auth_app().get("/xtm/auth/metadata")
    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("public")
    assert response.json() == {
        "issuer": XTM_ONE,
        "audience": XTM_ONE,
        "jwks_uri": f"{XTM_ONE}/xtm/auth/jwks",
        "token_signing_alg_values_supported": ["EdDSA"],
    }
    token = xtm_auth.issue_xtm_jwt(uuid.uuid4(), "analyst@example.com", PUBLIC)
    assert pyjwt.decode(token, options={"verify_signature": False})["iss"] == response.json()["issuer"]


@pytest.mark.parametrize("request_path", ["/xtm/auth/metadata", "/xtm-one/xtm/auth/metadata"])
def test_the_metadata_is_served_under_a_url_path_prefix(monkeypatch, request_path):
    """Behind a proxy that forwards ``/xtm-one`` or strips it, the document is
    served, and its URLs carry the prefix ``BASE_URL`` carries."""
    monkeypatch.setattr(settings, "base_url", "https://intranet.example/xtm-one")
    client = _auth_app("/xtm-one")
    response = client.get(request_path)
    assert response.status_code == 200
    body = response.json()
    assert body["issuer"] == body["audience"] == "https://intranet.example/xtm-one"
    assert body["jwks_uri"] == "https://intranet.example/xtm-one/xtm/auth/jwks"
    assert client.get(request_path.replace("/metadata", "/jwks")).status_code == 200


def test_the_platform_serves_the_metadata_next_to_its_keys():
    from app.main import application

    paths = {getattr(route, "path", "") for route in application.routes}
    assert {"/xtm/auth/jwks", "/xtm/auth/metadata"} <= paths
    assert "xtm" in RESERVED_FIRST_SEGMENTS


# ---------------------------------------------------------------------------
# The Security Audit reports what XTM_AUTH_ACCEPTED_AUDIENCES accepts
# ---------------------------------------------------------------------------


async def test_the_audit_reports_the_accepted_audiences(monkeypatch):
    from app.api.v1.security_audit import _check_platform_token_audiences
    from app.core.security_controls import control_statuses

    findings: list = []
    await _check_platform_token_audiences(findings)
    assert [(f.id, f.severity) for f in findings] == [("authentication.platform_token_audiences_identity_only", "pass")]
    statuses = {status.id: status for status in control_statuses(None, None)}
    assert statuses["authentication.platform_token_audiences"].state == "enforced"

    monkeypatch.setattr(settings, "xtm_auth_accepted_audiences", ["http://xtm-one:4000"])
    findings = []
    await _check_platform_token_audiences(findings)
    assert [(f.id, f.severity) for f in findings] == [("authentication.platform_token_audiences_extra", "info")]
    assert "http://xtm-one:4000" in findings[0].description
    statuses = {status.id: status for status in control_statuses(None, None)}
    assert statuses["authentication.platform_token_audiences"].state == "off"
