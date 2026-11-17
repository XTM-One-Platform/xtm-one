"""Cross-platform tokens between products reached on internal URLs: the
deployments whose URLs are all public keep their behaviour, and the lookup
stays bounded.

The split-horizon contract itself is pinned by ``test_platform_token_identity.py``;
this file pins what a deployment with public URLs everywhere (the SaaS shape)
must keep, the IPv6 and trailing-slash spellings, and the SQL the trust lookup
sends.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.core import platform_identity, xtm_auth
from app.core.config import Settings, settings
from app.core.url_identity import URLSettingError
from app.core.xtm_auth import (
    _kid,
    _private_key,
    _public_key_raw,
    accepted_audiences,
    verify_xtm_jwt,
    xtm_issuer,
)

_PUBLIC = "http://localhost:8080"


def _db(platform_url: str = _PUBLIC, api_url: str | None = None, *, registered: bool = True):
    result = MagicMock()
    result.all.return_value = (
        [SimpleNamespace(platform_identifier="opencti", platform_url=platform_url, api_url=api_url)]
        if registered
        else []
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _token(*, iss: str = _PUBLIC, aud: object = None) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": iss,
        "sub": str(uuid.uuid4()),
        "email": "analyst@example.com",
        "aud": xtm_issuer() if aud is None else aud,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "jti": str(uuid.uuid4()),
    }
    return pyjwt.encode(payload, _private_key, algorithm="EdDSA", headers={"kid": _kid})


async def _verify(token: str, db) -> tuple[object, MagicMock]:
    signing_key = MagicMock()
    signing_key.key = Ed25519PublicKey.from_public_bytes(_public_key_raw)
    with patch("app.core.xtm_auth._get_jwk_client") as get_client:
        get_client.return_value.get_signing_key_from_jwt.return_value = signing_key
        return await verify_xtm_jwt(token, db), get_client


@pytest.mark.asyncio
async def test_public_urls_everywhere_fetch_the_keys_once_where_they_always_were():
    # The SaaS shape: no *_API_URL override, so no internal URL is recorded.
    result, get_client = await _verify(_token(), _db())
    assert result is not None
    get_client.assert_called_once_with(_PUBLIC)


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        ("http://[::1]:4000/", "http://[::1]:4000"),
        ("http://[::1]:80", "http://[::1]"),
        ("HTTPS://[FD00::1]:443/xtm", "https://[fd00::1]/xtm"),
    ],
)
def test_ipv6_literals_keep_their_brackets(url, normalized):
    assert xtm_auth._normalize_url(url) == normalized
    assert "http://[::1]:80" in platform_identity._stored_spellings("http://[::1]")


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", ["http://[::1]:4000", "http://[::1]:4000/"])
async def test_an_ipv6_registration_is_trusted_and_reached(registered):
    result, get_client = await _verify(_token(iss="http://[::1]:4000"), _db(platform_url=registered))
    assert result is not None
    get_client.assert_called_once_with("http://[::1]:4000")


@pytest.mark.asyncio
async def test_the_registration_lookup_filters_in_sql_and_strips_every_trailing_slash():
    from sqlalchemy.dialects import postgresql

    db = _db(platform_url="https://example.com///")
    result, get_client = await _verify(_token(iss="https://example.com/"), db)
    assert result is not None
    get_client.assert_called_once_with("https://example.com")
    statement = db.execute.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    key = "rtrim(lower(trim(platform_registrations.platform_url)), '/') IN ("
    assert key in sql
    spellings = sql.split(key, 1)[1].split(")", 1)[0]
    assert sorted(s.strip() for s in spellings.split(",")) == [
        "'https://example.com'",
        "'https://example.com:443'",
    ]
    assert "platform_registrations.status = 'active'" in sql
    assert "ORDER BY platform_registrations.last_heartbeat_at DESC NULLS LAST" in sql


@pytest.mark.asyncio
async def test_an_unregistered_issuer_is_refused_whatever_its_keys():
    result, get_client = await _verify(_token(), _db(registered=False))
    assert result is None
    get_client.assert_not_called()


@pytest.mark.asyncio
async def test_a_configured_internal_url_is_accepted_as_audience(monkeypatch):
    monkeypatch.setattr(settings, "xtm_auth_accepted_audiences", ["http://xtm-one:4000"])
    assert accepted_audiences() == {xtm_issuer(), "http://xtm-one:4000"}
    result, _ = await _verify(_token(aud="http://xtm-one:4000/"), _db())
    assert result is not None


def test_the_accepted_audiences_setting_is_parsed_and_validated():
    parsed = Settings(
        _env_file=None,
        xtm_auth_accepted_audiences=" http://xtm-one:4000 , ,HTTP://XTM-One:4000/,https://ai.internal/",
    )
    assert parsed.xtm_auth_accepted_audiences == ["http://xtm-one:4000", "https://ai.internal"]
    for bad in (
        "xtm-one:4000",
        "ftp://xtm-one",
        "http://xtm-one:4000/?a=1",
        "http://xtm-one:4000/#top",
        "http://xtm-one:notaport",
        "http://xtm-one:99999",
        "http://xtm-one:0",
        "http://admin@xtm-one:4000",
    ):
        with pytest.raises(URLSettingError, match="XTM_AUTH_ACCEPTED_AUDIENCES"):
            Settings(_env_file=None, xtm_auth_accepted_audiences=bad)


def _openaev_reached_internally(monkeypatch) -> dict[str, object]:
    """OpenAEV registered as ``_PUBLIC`` and reached on ``http://openaev:8080``;
    returns what the next OpenAEV call sends."""
    rows = [SimpleNamespace(platform_identifier="openaev", platform_url=_PUBLIC, api_url="http://openaev:8080")]
    user = MagicMock(email="operator@example.com")

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, _statement):
            return MagicMock(scalar_one_or_none=lambda: user, all=lambda: rows)

    sent: dict[str, object] = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def _record(self, url, headers):
            sent["url"] = url
            sent["aud"] = pyjwt.decode(
                headers["Authorization"].removeprefix("Bearer "),
                options={"verify_signature": False},
            )["aud"]

        async def get(self, url, headers):
            self._record(url, headers)
            return MagicMock(status_code=200, json=lambda: {"autonomous_run_status": "RUNNING"})

        async def post(self, url, json, headers):
            self._record(url, headers)
            sent["body"] = json
            return MagicMock(status_code=200)

    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())
    monkeypatch.setattr("httpx.AsyncClient", _Client)
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("report", "extra", "status"),
    [
        ("_report_run_failed", {"error": "worker lost"}, "FAILED"),
        ("_report_run_completed", {"reason": "objective achieved"}, "COMPLETED"),
        ("_report_run_planned", {"reason": "plan ready"}, "PLANNED"),
    ],
)
async def test_the_worker_run_status_is_addressed_to_openaev_and_sent_internally(
    monkeypatch, report, extra, status
):
    from app.services.orchestration_adapters import autonomous

    sent = _openaev_reached_internally(monkeypatch)
    await getattr(autonomous, report)(
        base_url=_PUBLIC, openaev_run_id="run-1", user_id=str(uuid.uuid4()), **extra
    )
    assert sent["url"] == "http://openaev:8080/api/autonomous-runs/run-1/status"
    assert sent["aud"] == _PUBLIC
    assert sent["body"]["status"] == status


def test_the_accepted_audiences_are_computed_once_per_settings_value(monkeypatch):
    monkeypatch.setattr(settings, "xtm_auth_accepted_audiences", ["http://xtm-one:4000"])
    xtm_auth._accepted_audiences.cache_clear()
    accepted_audiences()
    accepted_audiences()
    assert xtm_auth._accepted_audiences.cache_info().misses == 1
    monkeypatch.setattr(settings, "xtm_auth_accepted_audiences", [])
    assert accepted_audiences() == {xtm_issuer()}
