"""Where a vault provider connected in the UI may be reached.

A provider declared in the environment (``SECRET_VAULT_<NAME>_*``) is the
operator's: its address is trusted like any other deployment setting. A
provider connected in Settings > Access and security > Secret vaults is typed by an
administrator, and the platform then sends requests to it from its own
network position: a login, a token lookup, KV listings and reads. Each host
such a provider names is held to exactly one rule:

* HashiCorp and 1Password ``ADDRESS`` and AWS ``ENDPOINT_URL``
  (:data:`ADDRESS_SETTINGS`): the operator-network policy of
  ``app.core.destination_policy``, as the OAuth token URLs are under
  ``OAUTH_TOKEN_URL_STRICT``, enforced here (below);
* Azure ``VAULT_URL``: a key vault of an Azure cloud
  (``app.services.vault.azure.KEY_VAULT_DOMAINS``), a name only Microsoft's
  DNS or the operator's own private zones answer, so it is not judged here;
* Google Cloud: no host is typed. The API (``app.services.vault.gcp.API_ROOT``)
  and the token endpoint a stored service account key is exchanged at
  (``app.services.vault.gcp.TOKEN_URI``, whatever the key's ``token_uri``
  says) are Google's own.

The identity endpoints a provider signs in at (Azure's authority host and
managed identity endpoint, AWS STS and the instance metadata service, the GKE
metadata server) come from the environment and are not judged.

The operator-network policy:

* private networks (RFC 1918, RFC 6598, unique local IPv6) and public
  addresses are reached: a vault normally lives on a private network;
* loopback, link-local, unspecified, reserved and multicast addresses are
  refused, and cloud metadata addresses always are;
* ``SECRET_VAULT_TRUSTED_ORIGINS`` names origins that may resolve to a
  loopback address, for an on-host vault. It is an environment variable only:
  an administrator session cannot widen it. Link-local and metadata addresses
  stay refused for a listed origin.

It runs:

* **on save and on a test** (:func:`assert_provider_addresses_allowed`, a 422
  in the routes: a new provider, an edit that changes an address, the draft
  test and the stored test): the address's host, the address it spells (an IPv4
  shorthand), and every address its name resolves to now. A name that does
  not resolve is accepted: the vault may not be up yet, and the connection
  decides;
* **on every connection** (:func:`client_options`, the HashiCorp and 1Password
  clients): through ``app.core.pinned_transport``, on the address actually
  connected to, with no second lookup. The clients follow no redirect. Behind an outbound proxy (``HTTP_PROXY`` / ``HTTPS_PROXY``,
  ``NO_PROXY`` applied) the proxy resolves and connects, so the address is
  judged from here (spelled or resolved) before the request leaves through
  it, as the MCP server URL policy does in its permissive mode;
* **AWS**: the SDK resolves and connects itself, so ``ENDPOINT_URL`` is judged
  again when the provider's client is built, not pinned.

A refusal answers one generic message (:data:`REFUSED_DETAIL`); the host, the
address and its class go to the log only, so a refusal is no oracle for how
internal names resolve.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import ssl
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import httpx

from app.core.destination_policy import IPAddress, forbidden_address_reason, shorthand_ipv4_address
from app.core.pinned_transport import PinnedTransport
from app.services.vault.config import VaultProviderConfig

logger = logging.getLogger(__name__)

#: The settings of each provider type that name a host the platform sends requests to, held to this policy.
ADDRESS_SETTINGS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "hashicorp": ("ADDRESS",),
        "onepassword": ("ADDRESS",),
        "aws": ("ENDPOINT_URL",),
    }
)

#: What a refused address or connection answers, whatever the reason.
REFUSED_DETAIL = (
    "This vault address is not allowed: a vault connected in the UI is not reached on a loopback, "
    "link-local, reserved or cloud metadata address. An operator can allow an on-host vault's "
    "origin with SECRET_VAULT_TRUSTED_ORIGINS."
)

_DEFAULT_PORTS = {"http": 80, "https": 443}

# A lookup on save is a courtesy (the connection decides), so it is bounded
# tightly: a slow resolver costs the save this long, at most.
_SAVE_DNS_TIMEOUT_SECONDS = 5.0


class VaultAddressRefusedError(ValueError):
    """A vault address the destination policy refuses; ``str()`` is safe to show."""


def _trusted_origins() -> list[str]:
    from app.core.config import settings

    return settings.secret_vault_trusted_origins


def address_refusal(url: httpx.URL, ip: IPAddress) -> str | None:
    """Why the policy refuses *ip* for a request to *url*, or ``None``."""
    from app.tools.mcp_destinations import origin_keys

    trusted = not origin_keys(url).isdisjoint(_trusted_origins())
    return forbidden_address_reason(ip, allow_private=True, allow_loopback=trusted)


async def _resolve(host: str, port: int) -> list:
    """The loop's resolver: one seam, so a test can stand in for DNS."""
    return await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _parse_ip(address: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return None


def _spelled_address(url: httpx.URL) -> IPAddress | None:
    """The address the host is or spells (an IPv4 shorthand), with no lookup."""
    literal = _parse_ip(url.host)
    if literal is not None:
        return literal
    shorthand = shorthand_ipv4_address(url.raw_host.decode("ascii"))
    return ipaddress.ip_address(shorthand) if shorthand else None


async def _refusal_of(url: httpx.URL) -> tuple[str, IPAddress, str] | None:
    """``(verb, address, why)`` for the first address of *url* the policy refuses, or ``None``.

    The host as spelled, else every address its name resolves to now; a name
    that does not resolve in time is not refused (the connection decides).
    """
    spelled = _spelled_address(url)
    if spelled is not None:
        verb, addresses = "is", [spelled]
    else:
        port = url.port or _DEFAULT_PORTS.get(url.scheme, 443)
        try:
            infos = await asyncio.wait_for(_resolve(url.raw_host.decode("ascii"), port), _SAVE_DNS_TIMEOUT_SECONDS)
        except (OSError, TimeoutError, UnicodeError):
            return None
        verb = "resolves to"
        addresses = [ip for info in infos if (ip := _parse_ip(str(info[4][0]))) is not None]
    for ip in addresses:
        if why := address_refusal(url, ip):
            return verb, ip, why
    return None


async def assert_address_allowed(address: str) -> None:
    """Raise :class:`VaultAddressRefusedError` when the platform may not send requests to *address*.

    Judged as httpx, which sends the requests, reads the host. An address no
    request can be sent to (not an http(s) URL with a host) is left to the
    provider's own check (``service_address``), which names the setting.
    """
    try:
        url = httpx.URL(address.strip())
    except (httpx.InvalidURL, TypeError, ValueError):
        return
    if url.scheme not in _DEFAULT_PORTS or not url.host:
        return
    refusal = await _refusal_of(url)
    if refusal is not None:
        verb, ip, why = refusal
        logger.info("Vault address refused: host %r %s %s, a %s", url.host[:200], verb, ip, why)
        raise VaultAddressRefusedError(REFUSED_DETAIL)


async def assert_provider_addresses_allowed(config: VaultProviderConfig) -> None:
    """Raise :class:`VaultAddressRefusedError` when a provider connected in the UI names an address it may not reach.

    A provider declared in the environment is the operator's and is not judged.
    """
    if not config.from_database:
        return
    for setting in ADDRESS_SETTINGS.get(config.type, ()):
        if value := config.get(setting):
            await assert_address_allowed(value)


class _VaultTransport(httpx.AsyncBaseTransport):
    """How a request of a provider connected in the UI leaves the platform.

    Directly: through a :class:`PinnedTransport`, the address connected to
    judged. Through the proxy the environment names for the URL: after the
    address is judged from here. Each transport is built on its first use.
    """

    def __init__(self, ssl_context: ssl.SSLContext) -> None:
        self._ssl_context = ssl_context
        self._pinned: PinnedTransport | None = None
        self._proxied: dict[str, httpx.AsyncHTTPTransport] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        from app.tools.mcp_destinations import environment_proxy

        proxy = environment_proxy(request.url)
        if proxy is not None:
            refusal = await _refusal_of(request.url)
            if refusal is not None:
                verb, ip, why = refusal
                logger.warning(
                    "Refused to send a vault request for %r through the outbound proxy: it %s %s, a %s",
                    request.url.host[:200],
                    verb,
                    ip,
                    why,
                )
                raise httpx.ConnectError(REFUSED_DETAIL, request=request)
            if proxy not in self._proxied:
                self._proxied[proxy] = httpx.AsyncHTTPTransport(proxy=proxy, verify=self._ssl_context, trust_env=False)
            return await self._proxied[proxy].handle_async_request(request)
        if self._pinned is None:
            self._pinned = PinnedTransport(
                address_refusal,
                refusal=REFUSED_DETAIL,
                resolve=lambda host, port: _resolve(host, port),
                ssl_context=self._ssl_context,
            )
        return await self._pinned.handle_async_request(request)

    async def aclose(self) -> None:
        for transport in (self._pinned, *self._proxied.values()):
            if transport is not None:
                await transport.aclose()


def client_options(config: VaultProviderConfig, verify: bool | ssl.SSLContext = True) -> dict[str, Any]:
    """The ``httpx.AsyncClient`` options of a provider's client to its vault.

    A provider connected in the UI gets :class:`_VaultTransport` and reads
    nothing from the environment itself (``trust_env=False``): the transport
    alone decides whether a proxy is used. A provider declared in the
    environment keeps httpx's own transport, as before the policy existed.
    """
    if not config.from_database:
        return {"verify": verify}
    ssl_context = verify if isinstance(verify, ssl.SSLContext) else httpx.create_ssl_context(trust_env=True)
    return {"transport": _VaultTransport(ssl_context), "trust_env": False}
