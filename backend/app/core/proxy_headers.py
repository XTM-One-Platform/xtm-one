# SPDX-License-Identifier: Apache-2.0

"""Which reverse proxy may speak for the client - the startup report and uvicorn's trust list.

Two settings decide it:

* ``FORWARDED_ALLOW_IPS`` - read by uvicorn, not by the application. Its
  proxy-header middleware rewrites ``scope["client"]`` and ``scope["scheme"]``
  from ``X-Forwarded-For`` / ``X-Forwarded-Proto`` only when the peer is one
  of the listed addresses, and reads the chain from its end, so a client
  cannot choose its own address behind a proxy that appends to the header.
  The recommended setting behind a reverse proxy.
* ``TRUST_PROXY_HEADERS`` - the ``trust_proxy_headers`` security option (its
  variable, or Settings > Access and security): it reads the LEFTMOST ``X-Forwarded-For``
  hop and ``X-Forwarded-Proto`` from any peer, and wins over
  ``FORWARDED_ALLOW_IPS`` when both are set. It relaxes a boundary, so it is
  reported at startup (or when it is switched on in Settings > Access and security,
  ``app.services.security_options``) and by the security audit while it is on.

:func:`trusted_proxy_entries` is uvicorn's trust list as this report and the
security audit's proxy check read it, parsed by
``app.core.undeclared_proxy.parse_forwarded_allow_ips``, the one parsing of it.

:func:`log_proxy_header_configuration` is the startup report, once per
process. Its warning about the flag advises from what the trust list in place
would leave in charge once the flag is off (:func:`trust_proxy_headers_advice`).
It also names the ``FORWARDED_ALLOW_IPS`` entries uvicorn silently ignores: an
entry that is not an IP address or network is kept as a literal string that
no peer ever equals, so a hostname or a typoed CIDR trusts nobody and
nothing says so.
"""

from __future__ import annotations

import logging

from app.core.security_options import security_option, where_to_set
from app.core.undeclared_proxy import forwarded_allow_ips, parse_forwarded_allow_ips

logger = logging.getLogger(__name__)

# uvicorn's own default when the variable is unset.
UVICORN_DEFAULT_FORWARDED_ALLOW_IPS = "127.0.0.1"

#: What the flag trusts, as each of its warnings opens (at startup, and when
#: it is switched on in Settings > Access and security).
TRUST_PROXY_HEADERS_RISK = (
    "TRUST_PROXY_HEADERS takes the client address from the leftmost X-Forwarded-For "
    "value, which the client itself writes behind a proxy that appends to the header, "
    "and it reads the headers from any peer: it suits only a proxy that overwrites "
    "X-Forwarded-For and is the only thing that reaches the platform."
)

#: The ``UPGRADING.md`` section the warnings point at.
UPGRADING_SECTION = "UPGRADING.md > 'FORWARDED_ALLOW_IPS behind a reverse proxy'"

_reported = False


def trust_proxy_headers_remediation() -> str:
    """The way off the flag: the trust list set, then checked, then the flag turned off where it is set."""
    return (
        "Set FORWARDED_ALLOW_IPS=<your proxy addresses> (an IP, a CIDR or a comma-separated "
        "list naming the proxy as this container sees it, not *) and restart, check that the "
        "access log's client_ip shows real client addresses rather than the proxy's, then "
        + where_to_set("trust_proxy_headers", "false")
        + f". See {UPGRADING_SECTION} for nginx, Traefik and Kubernetes ingress."
    )


def effective_forwarded_allow_ips() -> str:
    """The value uvicorn trusts: the variable when set (even empty), else its default.

    The platform image passes the variable to uvicorn as ``--forwarded-allow-ips``
    and uvicorn reads it natively otherwise, so the environment is the source of
    truth. An empty value trusts no peer, loopback included. Read through
    ``app.core.undeclared_proxy.forwarded_allow_ips``, the one reading of it.
    """
    return forwarded_allow_ips()


def trusted_proxy_entries(value: str) -> tuple[bool, list[str]]:
    """Whether uvicorn, given ``value``, trusts a peer beyond loopback, and the entries no peer matches.

    Mirrors ``uvicorn.middleware.proxy_headers._TrustedHosts``: the whole value
    ``*`` trusts every peer, otherwise each comma-separated entry is an IP
    address, or a network when it contains ``/`` (host bits must be zero), and
    anything else - a hostname (never resolved), a bracketed IPv6 address, a
    ``*`` inside a list, a Unix socket path - is a literal. Blank entries are
    skipped. No peer is ever a literal: a TCP peer is an IP address, and a
    Unix-socket connection has no client address at all
    (``uvicorn.protocols.utils.get_remote_addr``), which ``_TrustedHosts``
    refuses before its literal lookup. A literal only marks an
    ``X-Forwarded-For`` hop spelled the same way as a proxy, once another
    entry has trusted the peer. Parsed by
    ``app.core.undeclared_proxy.parse_forwarded_allow_ips``.
    """
    parsed = parse_forwarded_allow_ips(value)
    return parsed.trusts_remote, list(parsed.literals)


def unusable_forwarded_allow_ips(value: str) -> list[str]:
    """Entries of ``value`` that uvicorn keeps as literals, which match no peer (:func:`trusted_proxy_entries`)."""
    return trusted_proxy_entries(value)[1]


def trust_proxy_headers_advice(forwarded_allow_ips: str) -> str:
    """What the flag's warning tells an operator whose uvicorn trusts ``forwarded_allow_ips``.

    Turning the flag off hands the client address to that trust list, so the advice
    follows what it trusts: nothing yet (unset, uvicorn's default, empty), every
    peer (``*``, which reads the caller-controlled start of the chain just as the
    flag does), fewer peers than written (entries uvicorn ignores), or the peers it
    names - and only the access log's ``client_ip`` shows those are the proxy.
    """
    entries = [entry.strip() for entry in forwarded_allow_ips.split(",") if entry.strip()]
    # docker-compose always sets the variable, to uvicorn's own default.
    if entries in ([], [UVICORN_DEFAULT_FORWARDED_ALLOW_IPS]):
        return trust_proxy_headers_remediation()
    if forwarded_allow_ips == "*":
        return (
            "FORWARDED_ALLOW_IPS=* is set here but is no safer: it trusts every peer and "
            "reads the caller-controlled start of the chain as well. " + trust_proxy_headers_remediation()
        )
    if unusable_forwarded_allow_ips(forwarded_allow_ips):
        return (
            "FORWARDED_ALLOW_IPS is set here, but no peer ever matches the entries named in the "
            "next warning: correct them and restart, check that the access log's client_ip shows "
            "real client addresses rather than the proxy's, then "
            + where_to_set("trust_proxy_headers", "false")
            + "."
        )
    return (
        "FORWARDED_ALLOW_IPS is set here, and the flag takes precedence over it: check that the "
        "access log's client_ip shows real client addresses rather than the proxy's, then "
        + where_to_set("trust_proxy_headers", "false")
        + "."
    )


def log_proxy_header_configuration() -> None:
    """Report, once per process, the flag while it is on or a trust list that trusts less than it says.

    Run by ``main.lifespan`` before the startup work, so it reads the
    ``FORWARDED_ALLOW_IPS`` uvicorn read, before the provider seed's
    ``load_dotenv`` can add one from a ``.env`` file uvicorn never saw. The
    stored security options are not read yet either: a flag switched on in
    Settings > Access and security is warned about when that value is published
    (``app.services.security_options``).
    """
    global _reported
    if _reported:
        return
    _reported = True

    forwarded_allow_ips = effective_forwarded_allow_ips()
    if security_option("trust_proxy_headers"):
        logger.warning(
            "%s %s",
            TRUST_PROXY_HEADERS_RISK,
            trust_proxy_headers_advice(forwarded_allow_ips),
        )

    unusable = unusable_forwarded_allow_ips(forwarded_allow_ips)
    if unusable:
        logger.warning(
            "FORWARDED_ALLOW_IPS entries %s are not IP addresses or networks, so uvicorn "
            "trusts no peer for them: hostnames are not resolved, IPv6 addresses are written "
            "without brackets, a network needs its host bits at zero (10.42.0.0/16), and * "
            "means every peer only as the whole value.",
            ", ".join(repr(entry) for entry in unusable),
        )
