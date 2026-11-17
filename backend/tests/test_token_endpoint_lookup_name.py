"""The token endpoint guard looks up the name the token request looks up.

httpx sends a non-ASCII token URL host IDNA 2008-encoded (``straße`` becomes
``xn--strae-oqa``), while a lookup of the same host as a ``str`` goes through
Python's IDNA 2003 codec first (``strasse``): two different DNS names, so the
guard must resolve the one the request will. The same codec cannot encode a
host with an empty or over-long label at all and raises ``UnicodeEncodeError``
(a ``ValueError``), where httpx still sends the request and it fails on DNS.
Such a name does not resolve, so it is left to the request like any other name
that does not resolve, instead of escaping the guard: a 500 when the URL is
saved, and a scheduled refresh logged and retried on every tick instead of
backing off.

No test resolves a name or opens a connection: the stand-in resolver applies
the codec a real lookup of a ``str`` host applies first, then answers from a
table.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.api.v1 import helpers as api_helpers
from app.core import config
from app.core.security import encrypt_value
from app.integrations import token_endpoint
from app.integrations.token_endpoint import (
    TokenEndpointRefusedError,
    assert_token_endpoint_allowed,
)
from app.services import token_refresh

UNENCODABLE_HOSTS = ["idp..example", f"{'a' * 64}.example"]


def _resolver(answers: dict[str, str], looked_up: list[str] | None = None):
    """A lookup as ``socket.getaddrinfo`` makes it: the IDNA 2003 codec, then the table."""

    async def _fake(host, port):
        name = host.encode("idna").decode("ascii")
        if looked_up is not None:
            looked_up.append(name)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[name], port))]

    return _fake


@pytest.fixture(autouse=True)
def default_mode(monkeypatch):
    monkeypatch.setattr(config.settings, "oauth_token_url_strict", False)
    monkeypatch.setattr(config.settings, "oauth_token_dev_trusted_origins", [])
    monkeypatch.setattr(token_endpoint, "_resolve", _resolver({}))


@pytest.mark.parametrize("host", UNENCODABLE_HOSTS)
def test_the_codec_a_lookup_applies_cannot_encode_these_hosts_but_httpx_sends_them(host):
    with pytest.raises(UnicodeError):
        host.encode("idna")
    assert httpx.URL(f"https://{host}/token").raw_host == host.encode("ascii")


@pytest.mark.asyncio
async def test_a_non_ascii_host_is_judged_by_the_name_the_request_looks_up(monkeypatch):
    assert httpx.URL("https://straße.example/token").raw_host == b"xn--strae-oqa.example"
    looked_up: list[str] = []
    answers = {"strasse.example": "203.0.113.10", "xn--strae-oqa.example": "169.254.169.254"}
    monkeypatch.setattr(token_endpoint, "_resolve", _resolver(answers, looked_up))

    with pytest.raises(TokenEndpointRefusedError, match="cloud metadata endpoint"):
        await assert_token_endpoint_allowed("https://straße.example/token")
    assert looked_up == ["xn--strae-oqa.example"]


@pytest.mark.asyncio
async def test_strict_mode_judges_the_same_name(monkeypatch):
    monkeypatch.setattr(config.settings, "oauth_token_url_strict", True)
    answers = {"strasse.example": "10.0.0.7", "xn--strae-oqa.example": "127.0.0.1"}
    monkeypatch.setattr(token_endpoint, "_resolve", _resolver(answers))

    with pytest.raises(TokenEndpointRefusedError, match="loopback address"):
        await assert_token_endpoint_allowed("https://straße.example/token")


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("host", UNENCODABLE_HOSTS)
async def test_a_host_the_lookup_cannot_encode_is_left_to_the_request(monkeypatch, host, strict):
    monkeypatch.setattr(config.settings, "oauth_token_url_strict", strict)
    await assert_token_endpoint_allowed(f"https://{host}/token")


@pytest.mark.asyncio
async def test_strict_mode_still_refuses_a_ws_url_whose_host_cannot_be_encoded(monkeypatch):
    monkeypatch.setattr(config.settings, "oauth_token_url_strict", True)
    with pytest.raises(TokenEndpointRefusedError, match="must be an http or https URL"):
        await assert_token_endpoint_allowed("ws://idp..example/token")


@pytest.mark.asyncio
@pytest.mark.parametrize("host", UNENCODABLE_HOSTS)
async def test_saving_such_a_token_url_is_not_a_server_error(host):
    await api_helpers.require_allowed_token_url(f"https://{host}/token")


# -- a host httpx cannot send is a URL no request can be sent to ----------------

UNSENDABLE_HOSTS = ["1.2.3.999", "010.0.0.1", "\u2603.example"]


def _answering_public(attempted: list[str]):
    async def _fake(host, port):
        attempted.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", port))]

    return _fake


@pytest.mark.parametrize("host", UNSENDABLE_HOSTS)
def test_httpx_refuses_to_send_these_hosts(host):
    with pytest.raises(httpx.InvalidURL):
        httpx.URL(f"https://{host}/token")


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("host", UNSENDABLE_HOSTS)
async def test_a_host_httpx_cannot_send_is_refused_without_a_lookup(monkeypatch, host, strict):
    attempted: list[str] = []
    monkeypatch.setattr(config.settings, "oauth_token_url_strict", strict)
    monkeypatch.setattr(token_endpoint, "_resolve", _answering_public(attempted))
    with pytest.raises(TokenEndpointRefusedError, match="invalid host") as refused:
        await assert_token_endpoint_allowed(f"https://{host}/token")
    assert "OAUTH_TOKEN_URL_STRICT" not in str(refused.value)
    assert attempted == []


@pytest.mark.asyncio
async def test_saving_a_token_url_httpx_cannot_send_answers_422():
    with pytest.raises(HTTPException) as refused:
        await api_helpers.require_allowed_token_url("https://1.2.3.999/token")
    assert refused.value.status_code == 422


# -- an IPv4 shorthand is an address, not a name --------------------------------

METADATA_ALIASES = ["0xa9fea9fe", "2852039166", "169.254.43518"]


def _failing_lookup(error: BaseException, attempted: list[str]):
    async def _fake(host, _port):
        attempted.append(host)
        raise error

    return _fake


@pytest.mark.parametrize("alias", METADATA_ALIASES)
def test_the_request_reads_these_shorthands_as_the_metadata_address(alias):
    assert httpx.URL(f"http://{alias}/token").raw_host == alias.encode("ascii")
    assert socket.inet_aton(alias) == socket.inet_aton("169.254.169.254")


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", METADATA_ALIASES)
@pytest.mark.parametrize(
    "lookup_error",
    [socket.gaierror(socket.EAI_AGAIN, "too many DNS lookups in flight"), TimeoutError()],
)
async def test_a_shorthand_for_a_metadata_address_is_refused_whatever_the_resolver_does(
    monkeypatch, alias, lookup_error
):
    attempted: list[str] = []
    monkeypatch.setattr(token_endpoint, "_resolve", _failing_lookup(lookup_error, attempted))
    with pytest.raises(TokenEndpointRefusedError, match="cloud metadata endpoint") as refused:
        await assert_token_endpoint_allowed(f"http://{alias}/latest/meta-data")
    assert "169.254.169.254" not in str(refused.value)
    assert attempted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_a_loopback_shorthand_is_judged_as_loopback_in_both_modes(monkeypatch, strict):
    attempted: list[str] = []
    monkeypatch.setattr(config.settings, "oauth_token_url_strict", strict)
    monkeypatch.setattr(token_endpoint, "_resolve", _failing_lookup(TimeoutError(), attempted))
    if strict:
        with pytest.raises(TokenEndpointRefusedError, match="loopback address"):
            await assert_token_endpoint_allowed("http://2130706433:8080/token")
    else:
        await assert_token_endpoint_allowed("http://2130706433:8080/token")
    assert attempted == []


@pytest.mark.asyncio
async def test_the_scheduled_refresh_of_such_a_token_url_backs_off_as_it_always_did(monkeypatch):
    redis = SimpleNamespace(set=AsyncMock(), get=AsyncMock(return_value=None))
    monkeypatch.setattr(token_refresh, "redis_client", AsyncMock(return_value=redis))
    http = SimpleNamespace(post=AsyncMock(side_effect=httpx.ConnectError("Name or service not known")))
    integration = SimpleNamespace(
        id="i-1",
        name="IdP",
        provider="custom",
        client_id_enc=encrypt_value("cid"),
        client_secret_enc=None,
        refresh_token_enc=encrypt_value("rt"),
        scopes=None,
        token_url="https://idp..example/token",
        extra_config_enc=None,
        status="connected",
    )

    assert await token_refresh._do_refresh_integration(integration, http) is False
    http.post.assert_awaited_once()
    assert integration.status == "error"
    redis.set.assert_awaited_once()
    assert redis.set.await_args.args[0] == "token_refresh_cooldown:i-1"
    assert redis.set.await_args.kwargs["ex"] == token_refresh._TRANSIENT_FAIL_COOLDOWN
