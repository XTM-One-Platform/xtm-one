# SPDX-License-Identifier: Apache-2.0

"""One choke point for the platform's outbound HTTP.

An integration, a remote MCP server or a flow's HTTP node may be routed
through a relay group: its requests then leave from a relay the customer runs
next to the destination, never from the platform. The route is ambient - set
around the work with ``with egress_route(route):`` - and every outbound client
in the routed trees is an :func:`egress_client`, so code that builds requests
never chooses where they go.

* :func:`egress_client` is a drop-in for ``httpx.AsyncClient``. With no route
  set it IS an ``httpx.AsyncClient`` built from the same arguments - the same
  transport, TLS settings, proxies and limits as before.
* The decision is made per request, where httpx picks a transport
  (``_transport_for_url``), after the proxy mounts the environment adds: a
  routed request goes to the relay group (``RelayTransport``) whatever
  ``HTTPS_PROXY`` says, every redirect hop included, and never falls back to a
  direct connection.
* ``tests/test_egress_funnel.py`` refuses a bare ``httpx.AsyncClient(`` in the
  routed trees.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

_REAL_ASYNC_CLIENT = httpx.AsyncClient


@dataclass(frozen=True)
class EgressRoute:
    """Where routed requests go: one relay group (or one relay of it)."""

    group_id: Any
    group_name: str | None = None
    # The group's platform-side allow-list (``None`` = no restriction); the
    # relay's own list is checked again on the relay.
    allowed_destinations: tuple[str, ...] | None = None
    # What is routed, for the request log (``integration``, ``mcp_server``,
    # ``flow``...).
    source_type: str | None = None
    source_id: Any = None
    relay_id: Any = None

    def transport(self) -> httpx.AsyncBaseTransport:
        from app.services.relay_transport import RelayTransport

        return RelayTransport(
            group_id=self.group_id,
            group_name=self.group_name,
            allowed_destinations=(
                list(self.allowed_destinations) if self.allowed_destinations is not None else None
            ),
            source_type=self.source_type,
            source_id=self.source_id,
            relay_id=self.relay_id,
        )


_ROUTE: contextvars.ContextVar[EgressRoute | None] = contextvars.ContextVar(
    "egress_route", default=None
)


def current_route() -> EgressRoute | None:
    """The route of the work in progress, ``None`` for direct egress."""
    return _ROUTE.get()


@contextlib.contextmanager
def egress_route(route: EgressRoute | None) -> Iterator[None]:
    """Route the outbound requests made inside the block (``None``: direct).

    Tasks started inside the block inherit the route (``asyncio`` copies the
    context), so a background refresh or a stream reader stays on it.
    """
    token = _ROUTE.set(route)
    try:
        yield
    finally:
        _ROUTE.reset(token)


_AMBIENT: Any = object()


class _Routing:
    """Sends a request through its route: the client's own, else the current one."""

    _egress_route: Any = _AMBIENT

    def _transport_for_url(self, url: httpx.URL) -> httpx.AsyncBaseTransport:
        route = self._egress_route
        if route is _AMBIENT:
            route = _ROUTE.get()
        if route is not None:
            return route.transport()
        return super()._transport_for_url(url)  # type: ignore[misc]


class EgressClient(_Routing, httpx.AsyncClient):
    """An ``httpx.AsyncClient`` whose requests follow the current route."""


_ROUTING_SUBCLASSES: dict[type, type] = {}


def egress_client(*args: Any, route: Any = _AMBIENT, **kwargs: Any) -> httpx.AsyncClient:
    """A drop-in for ``httpx.AsyncClient(...)`` that follows the current route.

    *route* pins the client to one route (``None``: always direct) whatever
    the context of a request says - for a client whose requests are made by
    tasks it does not control (an MCP transport). Without it, each request
    follows the route of the context it is made in.

    ``httpx.AsyncClient`` is read when called: a subclass installed in its
    place (instrumentation) keeps routing through a subclass of it, and a
    stand-in that is not a client (a unit test's fake) is called as is.
    """
    cls = httpx.AsyncClient
    if cls is _REAL_ASYNC_CLIENT:
        client: httpx.AsyncClient = EgressClient(*args, **kwargs)
    elif isinstance(cls, type) and issubclass(cls, _REAL_ASYNC_CLIENT):
        routed = _ROUTING_SUBCLASSES.get(cls)
        if routed is None:
            routed = type(f"Egress{cls.__name__}", (_Routing, cls), {})
            _ROUTING_SUBCLASSES[cls] = routed
        client = routed(*args, **kwargs)
    else:
        return cls(*args, **kwargs)
    if route is not _AMBIENT:
        client._egress_route = route  # type: ignore[attr-defined]
    return client
