# SPDX-License-Identifier: Apache-2.0

"""Shared JWKS client cache — one ``PyJWKClient`` per JWKS URL.

Extracted from ``xtm_auth`` when A2A Entra federation became the second
consumer of JWKS-based verification. ``PyJWKClient`` already does the hard
parts — fetch, TTL cache of the keyset, ``kid`` resolution, force-refresh on
an unknown ``kid`` — but it caches *keys*, not itself: a client constructed
per request would re-fetch the keyset every time. This module owns the
process-wide instance cache, and the thread offload for the one blocking
step (the client fetches with synchronous urllib, which would stall the
event loop mid-authentication).

The cache key is the exact URL string, deliberately un-normalized: two
spellings of one endpoint cost one redundant client, while a normalization
that merged two genuinely different URLs would verify one issuer's tokens
against another's keys. ``test_trailing_slash_not_normalized`` pins this.
"""

from __future__ import annotations

import asyncio

from jwt import PyJWK, PyJWKClient

#: How long a fetched keyset stays fresh. Issuers rotate keys rarely and
#: announce them ahead of use, and PyJWKClient force-refreshes on an unknown
#: ``kid`` anyway, so a stale-but-valid keyset costs nothing.
JWKS_CACHE_TTL_SECONDS = 3600

_clients: dict[str, PyJWKClient] = {}


def get_jwk_client(jwks_url: str, *, timeout: int = 30) -> PyJWKClient:
    """Return the process-wide ``PyJWKClient`` for *jwks_url*.

    ``timeout`` only matters on the call that constructs the client — later
    callers share the existing instance whatever they pass. The two current
    consumers never share a URL (XTM platform JWKS vs. Entra tenant keys),
    so each surface gets the timeout it asked for.
    """
    if jwks_url not in _clients:
        _clients[jwks_url] = PyJWKClient(
            jwks_url,
            cache_jwk_set=True,
            lifespan=JWKS_CACHE_TTL_SECONDS,
            timeout=timeout,
        )
    return _clients[jwks_url]


async def resolve_signing_key(jwks_url: str, token: str, *, timeout: int = 30) -> PyJWK:
    """Resolve *token*'s signing key from *jwks_url*, off the event loop.

    Raises ``jwt.PyJWKClientError`` when the endpoint is unreachable or the
    token's ``kid`` is not in the keyset — the caller decides what a
    resolution failure means on its surface.
    """
    client = get_jwk_client(jwks_url, timeout=timeout)
    return await asyncio.to_thread(client.get_signing_key_from_jwt, token)
