"""The egress funnel: direct by default, the relay group when routed."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.core import egress
from app.core.egress import EgressRoute, current_route, egress_client, egress_route
from app.services.web_fetch import validate_outbound_url


def _recorder(name: str, calls: list[tuple[str, str]], status: int = 200, headers=None):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((name, str(request.url)))
        return httpx.Response(status, headers=headers or {}, text=name)

    return httpx.MockTransport(handler)


@pytest.fixture
def relay_calls(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(EgressRoute, "transport", lambda self: _recorder("relay", calls))
    return calls


ROUTE = EgressRoute(group_id="g-1", group_name="Paris DC")


async def test_with_no_route_the_client_is_the_plain_client_it_replaces():
    calls: list[tuple[str, str]] = []
    async with egress_client(transport=_recorder("direct", calls)) as client:
        assert isinstance(client, httpx.AsyncClient)
        response = await client.get("https://api.example.com/v1")
    assert response.text == "direct"
    assert calls == [("direct", "https://api.example.com/v1")]


async def test_a_routed_request_goes_to_the_relay_group_never_direct(relay_calls):
    direct: list[tuple[str, str]] = []
    async with egress_client(transport=_recorder("direct", direct)) as client:
        with egress_route(ROUTE):
            response = await client.get("https://splunk.corp.local:8089/services/search")
        after = await client.get("https://api.example.com/")
    assert response.text == "relay"
    assert relay_calls == [("relay", "https://splunk.corp.local:8089/services/search")]
    # The route is per request: outside the block the same client is direct.
    assert direct == [("direct", "https://api.example.com/")]
    assert after.text == "direct"


async def test_a_configured_proxy_never_takes_a_routed_request(relay_calls):
    async with egress_client(proxy="http://127.0.0.1:9") as client:
        with egress_route(ROUTE):
            response = await client.get("https://tanium.corp.local/api")
        with pytest.raises(httpx.ConnectError):
            await client.get("https://tanium.corp.local/api")
    assert response.text == "relay"
    assert relay_calls == [("relay", "https://tanium.corp.local/api")]


async def test_every_redirect_hop_of_a_routed_request_goes_through_the_relay(monkeypatch):
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://other.corp.local/end"})
        return httpx.Response(200, text="end")

    monkeypatch.setattr(EgressRoute, "transport", lambda self: httpx.MockTransport(handler))
    async with egress_client(follow_redirects=True) as client:
        with egress_route(ROUTE):
            response = await client.get("https://splunk.corp.local/start")
    assert response.text == "end"
    assert hops == ["https://splunk.corp.local/start", "https://other.corp.local/end"]


async def test_tasks_started_inside_a_route_inherit_it():
    async def inner():
        return current_route()

    with egress_route(ROUTE):
        assert await asyncio.create_task(inner()) is ROUTE
    assert current_route() is None
    with egress_route(ROUTE), egress_route(None):
        assert current_route() is None


def test_a_stand_in_for_the_client_is_honoured(monkeypatch):
    class _Fake:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(httpx, "AsyncClient", _Fake)
    client = egress_client(timeout=5)
    assert isinstance(client, _Fake)
    assert client.kwargs == {"timeout": 5}


async def test_a_client_subclass_installed_in_place_keeps_routing(monkeypatch, relay_calls):
    class _Instrumented(egress._REAL_ASYNC_CLIENT):
        pass

    monkeypatch.setattr(httpx, "AsyncClient", _Instrumented)
    async with egress_client() as client:
        assert isinstance(client, _Instrumented)
        with egress_route(ROUTE):
            response = await client.get("https://splunk.corp.local/")
    assert response.text == "relay"


def test_the_route_builds_a_relay_transport_with_its_group():
    from app.services.relay_transport import RelayTransport

    route = EgressRoute(
        group_id="g-2",
        group_name="Lyon",
        allowed_destinations=("splunk.corp.local:8089",),
        source_type="integration",
        source_id="i-1",
    )
    transport = route.transport()
    assert isinstance(transport, RelayTransport)
    assert transport.group_id == "g-2"
    assert transport.allowed_destinations == ["splunk.corp.local:8089"]
    assert transport.source_type == "integration"


def test_a_routed_url_may_be_private_but_never_carries_credentials():
    with pytest.raises(ValueError):
        validate_outbound_url("http://10.20.0.5:8089/services")
    with egress_route(ROUTE):
        assert validate_outbound_url("http://10.20.0.5:8089/services") == "http://10.20.0.5:8089/services"
        assert validate_outbound_url("https://splunk.corp.local/x?y=1") == "https://splunk.corp.local/x?y=1"
        with pytest.raises(ValueError, match="credentials"):
            validate_outbound_url("https://admin:secret@splunk.corp.local/")
        with pytest.raises(ValueError, match="scheme"):
            validate_outbound_url("file:///etc/passwd")
