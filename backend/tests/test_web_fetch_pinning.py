"""Connections are pinned to addresses the outbound policy admits (DNS rebinding)."""

from __future__ import annotations

import socket
from typing import Any

import httpcore
import httpx
import pytest

from app.services import web_fetch
from app.services.web_fetch import GuardedTransport, guarded_client_options

#: The environment's proxies as httpx reads them: everything through the
#: proxy, but ``NO_PROXY=.internal.test``.
_PROXIED = {"all://": "http://proxy.corp.test:3128", "all://*.internal.test": None}
_REFUSED = "does not call"


class _Resolver:
    """Answers each resolution from a queue (the last answer repeats)."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    def __call__(self, host, port, family=0, type=0, proto=0, flags=0):
        self.calls.append(host)
        ip = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port or 0))]


class _ConnectAttemptError(Exception):
    """Raised instead of opening a real socket; carries where it would connect."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(f"{host}:{port}")
        self.host = host
        self.port = port


@pytest.fixture
def no_sockets(monkeypatch):
    async def fake_connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        raise _ConnectAttemptError(host, port)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", fake_connect)
    monkeypatch.setattr(web_fetch, "_environment_proxies", dict)


async def _get(url: str, *, allow_private: bool) -> Any:
    async with httpx.AsyncClient(transport=GuardedTransport(allow_private=allow_private)) as client:
        return await client.get(url)


async def test_the_connection_goes_to_the_checked_address(monkeypatch, no_sockets):
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", _Resolver("93.184.216.34"))
    with pytest.raises(_ConnectAttemptError) as connected:
        await _get("https://api.example.test/v1/pets", allow_private=False)
    assert (connected.value.host, connected.value.port) == ("93.184.216.34", 443)


async def test_a_rebound_answer_is_refused_at_connect_time(monkeypatch, no_sockets):
    """The URL check sees a public address; the connection-time answer is loopback."""
    resolver = _Resolver("93.184.216.34", "127.0.0.1")
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", resolver)
    web_fetch.validate_outbound_url("https://rebind.example.test/", allow_private=True)
    with pytest.raises(httpx.ConnectError, match=_REFUSED) as refused:
        await _get("https://rebind.example.test/", allow_private=True)
    # The refusal never says where the name resolved.
    assert "127.0.0.1" not in str(refused.value)
    assert resolver.calls == ["rebind.example.test", "rebind.example.test"]


@pytest.mark.parametrize(
    ("ip", "allow_private", "admitted"),
    [
        ("10.0.0.7", True, True),
        ("100.64.3.4", True, True),
        ("10.0.0.7", False, False),
        ("169.254.169.254", True, False),
        ("100.100.100.200", True, False),
        ("::1", True, False),
        ("fd00:ec2::254", True, False),
        # Google Cloud's IPv6 metadata server sits in the ULA block the
        # private policy admits: refused by the shared destination policy.
        ("fd20:ce::254", True, False),
        ("::ffff:169.254.169.254", True, False),
    ],
)
async def test_the_policy_applies_to_every_connection(monkeypatch, no_sockets, ip, allow_private, admitted):
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", _Resolver(ip))
    if admitted:
        with pytest.raises(_ConnectAttemptError) as connected:
            await _get("http://internal.example.test:8080/", allow_private=allow_private)
        assert connected.value.host == ip
    else:
        with pytest.raises(httpx.ConnectError, match=_REFUSED):
            await _get("http://internal.example.test:8080/", allow_private=allow_private)


def test_the_url_check_under_the_private_policy_never_names_the_address(monkeypatch):
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", _Resolver("fd20:ce::254"))
    with pytest.raises(web_fetch.DestinationRefusedError) as refused:
        web_fetch.validate_outbound_url("http://metadata.corp.test/", allow_private=True)
    assert "metadata.corp.test" in str(refused.value)
    assert "fd20" not in str(refused.value)


async def test_one_disallowed_answer_refuses_the_host(monkeypatch, no_sockets):
    def resolver(host, port, family=0, type=0, proto=0, flags=0):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", resolver)
    with pytest.raises(httpx.ConnectError, match=_REFUSED):
        await _get("https://mixed.example.test/", allow_private=True)


def test_behind_a_proxy_direct_connections_stay_pinned(monkeypatch):
    monkeypatch.setattr(web_fetch, "_environment_proxies", lambda: dict(_PROXIED))
    options = guarded_client_options(allow_private=True)
    assert isinstance(options["transport"], GuardedTransport)
    assert len(options["event_hooks"]["request"]) == 1
    # The proxied pattern goes through the proxy; the NO_PROXY one falls back
    # to the pinned transport (``None``).
    assert options["mounts"]["all://*.internal.test"] is None
    assert isinstance(options["mounts"]["all://"], httpx.AsyncHTTPTransport)
    monkeypatch.setattr(web_fetch, "_environment_proxies", dict)
    assert set(guarded_client_options(allow_private=True)) == {"transport"}


async def test_a_no_proxy_host_is_connected_to_pinned(monkeypatch, no_sockets):
    """NO_PROXY sends the request direct: it must still go to a checked address."""
    monkeypatch.setattr(web_fetch, "_environment_proxies", lambda: dict(_PROXIED))
    resolver = _Resolver("10.0.0.9", "127.0.0.1")
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", resolver)
    async with httpx.AsyncClient(**guarded_client_options(allow_private=True)) as client:
        # The hook checks 10.0.0.9; the connection re-resolves to loopback.
        with pytest.raises(httpx.ConnectError, match=_REFUSED):
            await client.get("https://api.internal.test/v1/pets")
    assert resolver.calls == ["api.internal.test", "api.internal.test"]


def _resolve_by_host(answers: dict[str, str]):
    def resolver(host, port, family=0, type=0, proto=0, flags=0):
        ip = answers[host]
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

    return resolver


async def _through_a_proxy(
    url: str,
    handler,
    sent: list[httpx.Request],
    *,
    allow_private: bool,
    private_origins: tuple[str, ...] = (),
    **request: Any,
) -> None:
    """POST *url* with the client a proxied environment gets; *sent* is what went out."""

    def record(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return handler(req)

    options = guarded_client_options(allow_private=allow_private, private_origins=private_origins)
    # A mock transport stands for the proxy: whatever reaches it would be sent.
    options["mounts"] = {
        pattern: None if transport is None else httpx.MockTransport(record)
        for pattern, transport in options["mounts"].items()
    }
    async with httpx.AsyncClient(**options) as client:
        await client.post(url, data={"grant_type": "client_credentials"}, **request)


@pytest.mark.parametrize(
    ("ip", "allow_private", "expected"),
    [
        ("169.254.169.254", True, "idp.example.test"),
        ("127.0.0.1", True, "idp.example.test"),
        # The public policy's refusal names neither the host nor its address.
        ("10.0.0.5", False, "non-routable address$"),
    ],
)
async def test_behind_a_proxy_a_refused_url_is_never_sent(monkeypatch, ip, allow_private, expected):
    monkeypatch.setattr(web_fetch, "_environment_proxies", lambda: dict(_PROXIED))
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", _resolve_by_host({"idp.example.test": ip}))
    sent: list[httpx.Request] = []
    with pytest.raises(httpx.ConnectError, match=expected) as refused:
        await _through_a_proxy(
            "https://idp.example.test/oauth/token",
            lambda req: httpx.Response(200),
            sent,
            allow_private=allow_private,
        )
    assert ip not in str(refused.value)
    assert sent == []


async def test_behind_a_proxy_an_admitted_url_is_sent(monkeypatch):
    monkeypatch.setattr(web_fetch, "_environment_proxies", lambda: dict(_PROXIED))
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", _resolve_by_host({"idp.corp.test": "10.0.0.5"}))
    sent: list[httpx.Request] = []
    await _through_a_proxy(
        "https://idp.corp.test/oauth/token", lambda req: httpx.Response(200), sent, allow_private=True
    )
    assert [str(r.url) for r in sent] == ["https://idp.corp.test/oauth/token"]


async def test_behind_a_proxy_every_redirect_hop_is_checked(monkeypatch):
    monkeypatch.setattr(web_fetch, "_environment_proxies", lambda: dict(_PROXIED))
    monkeypatch.setattr(
        web_fetch.socket,
        "getaddrinfo",
        _resolve_by_host({"idp.example.test": "93.184.216.34", "metadata.test": "169.254.169.254"}),
    )
    sent: list[httpx.Request] = []

    def redirect(req: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"Location": "http://metadata.test/latest/"})

    with pytest.raises(httpx.ConnectError, match=r"metadata\.test"):
        await _through_a_proxy(
            "https://idp.example.test/oauth/token", redirect, sent, allow_private=True, follow_redirects=True
        )
    assert [r.url.host for r in sent] == ["idp.example.test"]


# ── Private origins: an operator-configured host on a public-only client ────


@pytest.mark.parametrize(
    ("url", "ip", "admitted"),
    [
        ("https://pds.corp.test/xrpc", "10.1.2.3", True),
        ("https://PDS.corp.test:443/xrpc", "10.1.2.3", True),
        # The operator-network policy still refuses these.
        ("https://pds.corp.test/xrpc", "169.254.169.254", False),
        ("https://pds.corp.test/xrpc", "127.0.0.1", False),
        ("https://pds.corp.test/xrpc", "2002:a9fe:a9fe::1", False),
        # Another port, scheme or host is not the origin: public only.
        ("https://pds.corp.test:8443/xrpc", "10.1.2.3", False),
        ("http://pds.corp.test/xrpc", "10.1.2.3", False),
        ("https://other.corp.test/xrpc", "10.1.2.3", False),
    ],
)
async def test_a_private_origin_is_held_to_the_operator_network_policy(monkeypatch, no_sockets, url, ip, admitted):
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", _Resolver(ip))
    options = guarded_client_options(allow_private=False, private_origins=("https://pds.corp.test",))
    async with httpx.AsyncClient(**options) as client:
        if admitted:
            with pytest.raises(_ConnectAttemptError) as connected:
                await client.get(url)
            assert connected.value.host == ip
        else:
            with pytest.raises(httpx.ConnectError, match=_REFUSED):
                await client.get(url)


def test_an_unparsable_private_origin_admits_nothing():
    assert web_fetch._private_hosts(["https://pds.corp.test:notaport", "", "ftp://pds.corp.test", "/x"]) == frozenset()
    assert web_fetch._private_hosts(["https://PDS.corp.test:443/"]) == frozenset({("pds.corp.test", 443)})


def test_the_callers_hooks_come_first_and_the_proxy_check_is_appended(monkeypatch):
    async def mine(request: httpx.Request) -> None:
        return None

    async def after(response: httpx.Response) -> None:
        return None

    hooks = {"request": [mine], "response": [after]}
    monkeypatch.setattr(web_fetch, "_environment_proxies", dict)
    assert guarded_client_options(allow_private=False, event_hooks=hooks)["event_hooks"] == {
        "request": [mine],
        "response": [after],
    }
    monkeypatch.setattr(web_fetch, "_environment_proxies", lambda: dict(_PROXIED))
    proxied = guarded_client_options(allow_private=False, event_hooks=hooks)["event_hooks"]
    assert proxied["response"] == [after]
    assert proxied["request"][0] is mine
    assert len(proxied["request"]) == 2
    # The caller's mapping is not modified.
    assert hooks == {"request": [mine], "response": [after]}


@pytest.mark.parametrize(
    ("url", "ip", "sent_count"),
    [
        ("https://pds.corp.test/xrpc", "10.1.2.3", 1),
        ("https://pds.corp.test/xrpc", "169.254.169.254", 0),
        ("https://other.corp.test/xrpc", "10.1.2.3", 0),
    ],
)
async def test_behind_a_proxy_a_private_origin_is_checked_under_the_operator_policy(
    monkeypatch, url, ip, sent_count
):
    monkeypatch.setattr(web_fetch, "_environment_proxies", lambda: dict(_PROXIED))
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", _Resolver(ip))
    sent: list[httpx.Request] = []
    origins = ("https://pds.corp.test",)
    if sent_count:
        await _through_a_proxy(url, lambda req: httpx.Response(200), sent, allow_private=False, private_origins=origins)
    else:
        with pytest.raises(httpx.ConnectError):
            await _through_a_proxy(
                url, lambda req: httpx.Response(200), sent, allow_private=False, private_origins=origins
            )
    assert len(sent) == sent_count
