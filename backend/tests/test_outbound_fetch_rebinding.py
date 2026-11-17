"""The fetchers that check a URL and then send it connect to a checked address.

``validate_outbound_url`` resolves a host and judges its addresses; a client
that then connects by name resolves it AGAIN, and a short-TTL answer can pass
the check with a public address and point the connection inward (DNS
rebinding). Each fetcher below now connects through the pinned transport
(``web_fetch.guarded_client_options``): the address the socket opens to is the
one the policy judged when the connection opened.

The resolver answers each name from a queue (the check's answer first, the
connection's second); no socket is opened: the network backend records where
it would connect and stops there.
"""

from __future__ import annotations

import socket
import uuid
from types import SimpleNamespace

import httpcore
import httpx
import pytest

from app.integrations import file_delivery
from app.integrations.file_delivery import FetchError, fetch_file
from app.integrations.providers import social_media
from app.services import web_fetch
from app.services.builtin_tools.image_assets import ImageAssetError, download_public_image


class _ConnectAttemptError(Exception):
    """Raised instead of opening a socket; carries where it would connect."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(f"{host}:{port}")
        self.host = host
        self.port = port


class _Resolver:
    """Answers each name from its own queue (the last answer repeats)."""

    def __init__(self, answers: dict[str, list[str]]) -> None:
        self.answers = {host: list(queue) for host, queue in answers.items()}
        self.calls: list[str] = []

    def __call__(self, host, port, family=0, type=0, proto=0, flags=0):
        self.calls.append(host)
        queue = self.answers.get(host)
        if not queue:
            raise socket.gaierror(f"unknown host {host}")
        ip = queue.pop(0) if len(queue) > 1 else queue[0]
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port or 0))]


@pytest.fixture
def connects(monkeypatch) -> list[tuple[str, int]]:
    """Where the pinned transport would open a socket (never opened)."""
    attempts: list[tuple[str, int]] = []

    async def fake_connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        attempts.append((host, port))
        raise _ConnectAttemptError(host, port)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", fake_connect)
    # No environment proxy: every request is connected to directly.
    monkeypatch.setattr(web_fetch, "_environment_proxies", dict)
    return attempts


def _resolve(monkeypatch, answers: dict[str, list[str]]) -> _Resolver:
    resolver = _Resolver(answers)
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    return resolver


# ── fetch_image_asset ─────────────────────────────────────────────────────


async def test_an_image_download_connects_to_the_checked_address(monkeypatch, connects):
    host = f"img-{uuid.uuid4().hex[:8]}.example.test"
    _resolve(monkeypatch, {host: ["93.184.216.34"]})
    with pytest.raises(_ConnectAttemptError):
        await download_public_image(f"https://{host}/logo.png")
    assert connects == [("93.184.216.34", 443)]


async def test_an_image_download_never_connects_to_a_rebound_address(monkeypatch, connects):
    host = f"img-{uuid.uuid4().hex[:8]}.example.test"
    resolver = _resolve(monkeypatch, {host: ["93.184.216.34", "127.0.0.1"]})
    with pytest.raises(ImageAssetError) as refused:
        await download_public_image(f"https://{host}/logo.png")
    assert connects == []
    assert resolver.calls == [host, host]
    assert "127.0.0.1" not in str(refused.value)


# ── The flow HTTP node ────────────────────────────────────────────────────


def _http_runner():
    from app.services.flow_runner import FlowWalker

    runner = FlowWalker.__new__(FlowWalker)
    runner.db = None
    runner.run = SimpleNamespace(flow_id=uuid.uuid4())
    runner._renderer = None
    return runner


async def test_an_http_node_connects_to_the_checked_address(monkeypatch, connects):
    _resolve(monkeypatch, {"api.example.test": ["93.184.216.34"]})
    with pytest.raises(_ConnectAttemptError):
        await _http_runner()._exec_http_request({"method": "POST", "url": "https://api.example.test/v1", "body": "{}"})
    assert connects == [("93.184.216.34", 443)]


async def test_an_http_node_never_connects_to_a_rebound_address(monkeypatch, connects):
    resolver = _resolve(monkeypatch, {"api.example.test": ["93.184.216.34", "169.254.169.254"]})
    outcome = await _http_runner()._exec_http_request({"method": "GET", "url": "https://api.example.test/v1"})
    assert outcome.error is not None and outcome.error.startswith("Request failed")
    assert "169.254.169.254" not in outcome.error
    assert connects == []
    assert resolver.calls == ["api.example.test", "api.example.test"]


# ── fetch_file: a redirect hop that leaves the start origin ───────────────


def _start_origin_redirecting_to(location: str) -> httpx.AsyncClient:
    """The caller's client: the start origin answers with a redirect."""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": location})),
        follow_redirects=True,
    )


async def test_a_redirect_hop_off_the_start_origin_connects_to_the_checked_address(monkeypatch, connects):
    _resolve(monkeypatch, {"storage.example.test": ["93.184.216.35"]})
    async with _start_origin_redirecting_to("https://storage.example.test/f.bin") as client:
        with pytest.raises(_ConnectAttemptError):
            await fetch_file(
                client, "GET", "https://api.vendor.test/export", provider="Vendor", action="download", max_bytes=1024
            )
    assert connects == [("93.184.216.35", 443)]


async def test_a_redirect_hop_off_the_start_origin_never_connects_to_a_rebound_address(monkeypatch, connects):
    resolver = _resolve(monkeypatch, {"storage.example.test": ["93.184.216.35", "10.0.0.8"]})
    async with _start_origin_redirecting_to("https://storage.example.test/f.bin") as client:
        answer = await fetch_file(
            client, "GET", "https://api.vendor.test/export", provider="Vendor", action="download", max_bytes=1024
        )
    assert isinstance(answer, FetchError)
    assert "10.0.0.8" not in str(answer)
    assert connects == []
    assert resolver.calls == ["storage.example.test", "storage.example.test"]


async def test_a_redirect_hop_keeps_the_callers_hooks(monkeypatch, connects):
    """The clean client of a hop off the start origin still runs the caller's hooks."""
    _resolve(monkeypatch, {"storage.example.test": ["93.184.216.35"]})
    seen: list[str] = []

    async def hook(request: httpx.Request) -> None:
        seen.append(str(request.url))

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": "https://storage.example.test/f.bin"})
        ),
        follow_redirects=True,
        event_hooks={"request": [hook]},
    )
    async with client:
        with pytest.raises(_ConnectAttemptError):
            await file_delivery.fetch_file(
                client, "GET", "https://api.vendor.test/export", provider="Vendor", action="download", max_bytes=1024
            )
    assert seen == ["https://api.vendor.test/export", "https://storage.example.test/f.bin"]


# ── fetch_public_media and its trusted origin ─────────────────────────────


async def test_a_media_download_never_connects_to_a_rebound_address(monkeypatch, connects):
    resolver = _resolve(monkeypatch, {"cdn.example.test": ["93.184.216.36", "127.0.0.1"]})
    answer = await social_media.fetch_public_media("https://cdn.example.test/v.mp4", source="X")
    assert isinstance(answer, FetchError)
    assert "127.0.0.1" not in str(answer)
    assert connects == []
    assert resolver.calls == ["cdn.example.test", "cdn.example.test"]


async def test_a_trusted_origin_on_the_private_network_is_connected_to(monkeypatch, connects):
    _resolve(monkeypatch, {"pds.corp.test": ["10.1.2.3"]})
    with pytest.raises(_ConnectAttemptError):
        await social_media.fetch_public_media(
            "https://pds.corp.test/xrpc/com.atproto.sync.getBlob?cid=b",
            source="Bluesky",
            trusted_origin="https://PDS.corp.test:443/",
        )
    assert connects == [("10.1.2.3", 443)]


@pytest.mark.parametrize("address", ["169.254.169.254", "127.0.0.1", "169.254.10.1"])
async def test_a_trusted_origin_still_never_reaches_metadata_loopback_or_link_local(
    monkeypatch, connects, address
):
    _resolve(monkeypatch, {"pds.corp.test": [address]})
    answer = await social_media.fetch_public_media(
        "https://pds.corp.test/xrpc/com.atproto.sync.getBlob?cid=b",
        source="Bluesky",
        trusted_origin="https://pds.corp.test",
    )
    assert isinstance(answer, FetchError)
    assert address not in str(answer)
    assert connects == []


async def test_the_trusted_origin_on_another_port_is_held_to_the_public_policy(monkeypatch, connects):
    """Only the exact origin is admitted to the private network."""
    resolver = _resolve(monkeypatch, {"pds.corp.test": ["10.1.2.3"]})
    answer = await social_media.fetch_public_media(
        "https://pds.corp.test:8443/xrpc/com.atproto.sync.getBlob?cid=b",
        source="Bluesky",
        trusted_origin="https://pds.corp.test",
    )
    assert isinstance(answer, FetchError)
    assert connects == []
    # Refused by the URL check, before any connection was attempted.
    assert resolver.calls == ["pds.corp.test"]
