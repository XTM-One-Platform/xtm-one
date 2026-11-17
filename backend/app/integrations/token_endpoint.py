# SPDX-License-Identifier: Apache-2.0

"""Where an OAuth token request may be sent.

Every token request - the authorization-code exchange and the scheduled and
inline refreshes, for integrations and MCP servers - POSTs the client
credentials (and a code or refresh token) to a URL a user configured. Before
any such request, and when the URL is saved, the destination is checked:

* by default, only what no token exchange could ever complete against is
  refused: a cloud instance-metadata address (``METADATA_ADDRESSES`` - no
  metadata service implements an OAuth token endpoint) and a URL no request can
  be sent to (another scheme, no host, an unreadable URL, port or host). Every other
  destination - loopback, link-local, unspecified, reserved, multicast,
  private, public - is accepted as it was before the check existed, so no
  deployment needs a configuration change;
* ``OAUTH_TOKEN_URL_STRICT`` opts into the operator-network policy of
  ``app.core.destination_policy``: private networks stay allowed, so an on-prem
  identity provider keeps working; loopback, link-local, multicast,
  unspecified, reserved and cloud-metadata addresses are refused, and so is
  any scheme but http(s). ``OAUTH_TOKEN_DEV_TRUSTED_ORIGINS`` names origins
  whose token URL may then resolve to a loopback address, for local
  development. Link-local and metadata addresses stay refused for them too.

Both are security options (``app.core.security_options``): set by their
variable, or in Settings > Access and security when the variable is unset.

An IPv4 shorthand the C resolver reads as an address (``0xa9fea9fe``) is
judged as that address, with no lookup. A name is resolved and EVERY address it
resolves to must pass (a name answering both a public and a metadata address
would otherwise pass on the public one).
The lookup goes through the A2A destination guard's resolver, off the event
loop, for the name the token request looks up (httpx's encoding of the host).
A name that does not resolve, or not within ``_DNS_TIMEOUT_SECONDS``, is
not refused here: there is no destination to judge, and the request resolves
it again itself (behind an egress proxy, the proxy does - refusing would break
every install that reaches its identity provider that way).

The check runs before the request and the request resolves again, so a DNS
answer that changes in between, or that only arrives after the bound, is not
caught; pinning the connection to the checked address is a transport-level
change (see the OpenAPI integration's guarded transport, which this
policy is meant to converge with). Redirects are not followed by the token
clients, so a checked URL cannot bounce the request elsewhere.
"""

from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlsplit

import httpx

from app.core.destination_policy import forbidden_address_reason, is_metadata_address
from app.core.destination_policy import shorthand_ipv4_address as _shorthand_address
from app.core.egress import current_route
from app.core.security_options import security_option

_DEFAULT_PORTS = {"http": 80, "https": 443}
# httpx sends a ws:// or wss:// URL as plain HTTP or HTTPS, so a token URL
# written that way exchanged tokens before the check existed. Only strict mode
# holds the scheme to http(s).
_SENDABLE_PORTS = {**_DEFAULT_PORTS, "ws": 80, "wss": 443}

# How a strict refusal names the way out: the variable, or its field.
_STRICT_OFF = "set OAUTH_TOKEN_URL_STRICT=false (or turn off its field in Settings > Access and security)"

# The bound the A2A destination guard applies to its own lookups.
_DNS_TIMEOUT_SECONDS = 10.0


class TokenEndpointRefusedError(ValueError):
    """A token URL this platform will not send credentials to."""


def _origin(scheme: str, host: str, port: int | None) -> str:
    return f"{scheme}://{host}{f':{port}' if port else ''}"


def _written_origin(scheme: str, host: str, port: int | None) -> str:
    """The origin as ``OAUTH_TOKEN_DEV_TRUSTED_ORIGINS`` takes it: an IPv6 host in brackets."""
    return _origin(scheme, f"[{host}]" if ":" in host else host, port)


async def _resolve(host: str, port: int) -> list:
    """The A2A destination guard's resolver, so both guards resolve alike.

    One seam, so a test can stand in for DNS.
    """
    from app.a2a.client.destinations import _resolve as resolve_destination

    return await resolve_destination(host, port)


def _lookup_name(host: str) -> str:
    """The name the token request itself looks up: httpx's encoding of *host*.

    httpx sends a non-ASCII host IDNA 2008-encoded (``straße`` becomes
    ``xn--strae-oqa``), while a lookup of the ``str`` would apply Python's IDNA
    2003 codec (``strasse``): two different DNS names, and the guard must judge
    the request's. A host httpx cannot encode at all (``1.2.3.999``, a symbol
    IDNA 2008 disallows) is a URL no request can be sent to.
    """
    try:
        return httpx.URL(scheme="http", host=host).raw_host.decode("ascii")
    except httpx.InvalidURL as exc:
        raise TokenEndpointRefusedError("The token URL has an invalid host.") from exc


async def _resolved_addresses(name: str, port: int) -> list[str]:
    """Every address *name* resolves to; none when it does not resolve in time.

    A name the resolver cannot even encode (an empty or over-long label raises
    ``UnicodeError``) does not resolve either: the request fails on it as it
    did before this check existed.
    """
    try:
        infos = await asyncio.wait_for(_resolve(name, port), _DNS_TIMEOUT_SECONDS)
    except (OSError, TimeoutError, UnicodeError):
        return []
    return [str(info[4][0]) for info in infos]


def _parse_ip(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return None


_LOOPBACK_NAME = "localhost"


def strict_refusal_reason(url: str) -> str | None:
    """Why the strict checks would refuse *url* as it is written, ``None`` when nothing in it does.

    Read without a lookup, for the Security Audit: the scheme, and a
    host that is an address, spells one (an IPv4 shorthand) or is
    ``localhost``; a name is judged only by the lookup a token request makes.
    What every mode refuses (a cloud metadata address, a URL no request can be
    sent to) is not reported: the strict checks change nothing for it.
    """
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in _SENDABLE_PORTS or not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            name = _lookup_name(host)
        except TokenEndpointRefusedError:
            return None
        if name == _LOOPBACK_NAME or name.endswith(f".{_LOOPBACK_NAME}"):
            ip = ipaddress.ip_address("127.0.0.1")
        elif (shorthand := _shorthand_address(name)) is not None:
            ip = ipaddress.ip_address(shorthand)
        else:
            ip = None
    if ip is not None and is_metadata_address(ip):
        return None
    if scheme not in _DEFAULT_PORTS:
        return f"a {scheme} URL"
    if ip is None:
        return None
    allow_loopback = _origin(scheme, host, port) in security_option("oauth_token_dev_trusted_origins")
    why = forbidden_address_reason(ip, allow_private=True, allow_loopback=allow_loopback)
    return f"a {why}" if why else None


async def assert_token_endpoint_allowed(url: str) -> None:
    """Raise :class:`TokenEndpointRefusedError` when *url* may not receive a token request."""
    strict = security_option("oauth_token_url_strict")
    try:
        parts = urlsplit((url or "").strip())
    except ValueError as exc:
        raise TokenEndpointRefusedError("The token URL is not a valid URL.") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in _SENDABLE_PORTS:
        # Saving the URL answers 422 with this text: it names the schemes the
        # active mode accepts, ws / wss included by default.
        schemes = "http or https" if strict else "http, https, ws or wss"
        raise TokenEndpointRefusedError(f"The token URL must be an {schemes} URL.")
    host = (parts.hostname or "").lower()
    if not host:
        raise TokenEndpointRefusedError("The token URL has no host.")
    try:
        port = parts.port
    except ValueError as exc:
        raise TokenEndpointRefusedError("The token URL has an invalid port.") from exc

    if current_route() is not None:
        # Sent through a relay group: the host is a name of the customer's
        # network, resolved (and checked) by the relay. Only what the URL
        # itself spells is judged here.
        spelled = host if _parse_ip(host) is not None else _shorthand_address(_lookup_name(host))
        if spelled is not None and is_metadata_address(_parse_ip(spelled)):
            raise TokenEndpointRefusedError(
                f"The token URL host {host} is a cloud metadata endpoint; this platform does "
                "not send OAuth credentials there."
            )
        return

    try:
        ipaddress.ip_address(host)
    except ValueError:
        name = _lookup_name(host)
        if (shorthand := _shorthand_address(name)) is not None:
            verb, addresses = "is", [shorthand]
        else:
            verb, addresses = "resolves to", await _resolved_addresses(name, port or _SENDABLE_PORTS[scheme])
    else:
        verb, addresses = "is", [host]
    ips = [ip for ip in map(_parse_ip, addresses) if ip is not None]

    def _refused(why: str, way_out: str = "") -> TokenEndpointRefusedError:
        # The class only, never the resolved address: whoever saves a token
        # URL must not learn how internal names resolve.
        return TokenEndpointRefusedError(
            f"The token URL host {host} {verb} a {why}; this platform does not "
            f"send OAuth credentials there.{way_out}"
        )

    # Refused in every mode and lifted by no setting, so it is checked before
    # any refusal that names one.
    if any(is_metadata_address(ip) for ip in ips):
        raise _refused("cloud metadata endpoint")
    if not strict:
        return
    if scheme not in _DEFAULT_PORTS:
        raise TokenEndpointRefusedError(
            f"The token URL must be an http or https URL. To accept a {scheme} URL, {_STRICT_OFF}."
        )
    allow_loopback = _origin(scheme, host, port) in security_option("oauth_token_dev_trusted_origins")
    reasons = [
        why
        for ip in ips
        if (why := forbidden_address_reason(ip, allow_private=True, allow_loopback=allow_loopback))
    ]
    if not reasons:
        return
    # A trusted origin lifts a loopback refusal only, so it is offered only
    # when loopback is all that stands in the way.
    beyond_loopback = [why for why in reasons if why != "loopback address"]
    if beyond_loopback:
        raise _refused(beyond_loopback[0], f" To accept it, {_STRICT_OFF}.")
    raise _refused(
        "loopback address",
        f" To accept it, {_STRICT_OFF}, or, for local development, add "
        f"{_written_origin(scheme, host, port)} to OAUTH_TOKEN_DEV_TRUSTED_ORIGINS "
        "(or to its field in Settings > Access and security).",
    )
