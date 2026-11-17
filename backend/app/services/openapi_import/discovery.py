# SPDX-License-Identifier: Apache-2.0

"""Authorization-server discovery documents of a spec's security schemes.

An ``openIdConnect`` scheme names only its discovery URL (``openIdConnectUrl``,
an OpenID Provider configuration), and an OpenAPI 3.2 ``oauth2`` scheme may
name its RFC 8414 metadata (``oauth2MetadataUrl``) instead of, or besides,
spelling out its flows. :func:`fetch_discovery` fetches those documents when a
spec is loaded (preview, import, refresh) so the conversion can turn them into
ordinary OAuth2 options (``security.py``). A document that cannot be fetched
or read maps to ``{"error": reason}``: its scheme is then listed as
unsupported with that reason, and the rest of the spec imports normally.

A discovery URL given as the issuer itself (without
``/.well-known/openid-configuration`` or ``/.well-known/oauth-authorization-server``)
is tried at its well-known locations too: inserted before the issuer's path
(RFC 8414, multi-tenant issuers), appended to it (OpenID Connect) and at the
root. Fetches go through the destination guard, like the spec download, and
only over HTTPS.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from app.services.openapi_import.document import SpecDocument
from app.services.openapi_import.schema import RefResolver, UnresolvedRefError
from app.services.openapi_import.security import (
    absolute_url,
    is_secure_oauth_url,
    security_scheme_definitions,
)

logger = logging.getLogger(__name__)

_MAX_DOCUMENTS = 5
_MAX_BYTES = 512 * 1024
_TIMEOUT_SECONDS = 10
_OPENID_SUFFIX = "/.well-known/openid-configuration"
_OAUTH_SUFFIX = "/.well-known/oauth-authorization-server"


def discovery_urls(document: SpecDocument, url_base: str | None) -> list[tuple[str, str]]:
    """``(absolute URL, kind)`` of every discovery document the schemes name.

    *kind* is ``"openid"`` or ``"oauth"``. Swagger 2.0 has neither.
    """
    if document.version.family == "2.0":
        return []
    resolver = RefResolver(document.data)
    found: list[tuple[str, str]] = []
    for raw in security_scheme_definitions(document.data, document.version.family).values():
        try:
            scheme = resolver.deref(raw)
        except UnresolvedRefError:
            continue
        if not isinstance(scheme, dict):
            continue
        if scheme.get("type") == "openIdConnect":
            url, kind = absolute_url(scheme.get("openIdConnectUrl"), url_base), "openid"
        elif scheme.get("type") == "oauth2":
            url, kind = absolute_url(scheme.get("oauth2MetadataUrl"), url_base), "oauth"
        else:
            continue
        if url and all(url != u for u, _ in found):
            found.append((url, kind))
    return found[:_MAX_DOCUMENTS]


def _is_metadata(data: Any) -> bool:
    return isinstance(data, dict) and bool(
        data.get("authorization_endpoint") or data.get("token_endpoint")
    )


async def _fetch_json(url: str) -> dict[str, Any]:
    from app.services.web_fetch import fetch_url_bytes

    fetched = await fetch_url_bytes(
        url,
        max_bytes=_MAX_BYTES,
        timeout=_TIMEOUT_SECONDS,
        allow_private=True,
        accept="application/json",
    )
    try:
        data = json.loads(fetched.content)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("it is not a JSON document") from None
    if not isinstance(data, dict):
        raise ValueError("it is not a JSON object")
    return data


def _error(exc: Exception) -> str:
    from app.services.web_fetch import DestinationUnresolvedError

    if isinstance(exc, httpx.HTTPStatusError):
        return f"the server answered HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError | DestinationUnresolvedError):
        # Refused, reset, filtered, unresolved: one answer, which says nothing
        # about the hosts and ports behind it.
        return "it could not be reached"
    return str(exc) or type(exc).__name__


async def _discover(url: str, kind: str) -> dict[str, Any]:
    if not is_secure_oauth_url(url):
        # The document says where codes, tokens and secrets go: read in clear
        # text, anyone on the path could rewrite it.
        return {"error": "its discovery URL does not use HTTPS"}
    candidates = [url]
    if "/.well-known/" not in urlparse(url).path:
        # An issuer URL: the three well-known shapes the MCP OAuth discovery
        # probes too (RFC 8414 path insertion, OIDC appending, the root).
        from app.services.mcp_oauth_discovery import _well_known_candidates

        suffix = _OPENID_SUFFIX if kind == "openid" else _OAUTH_SUFFIX
        candidates.extend(_well_known_candidates(url, suffix.removeprefix("/.well-known/")))
    first_error: str | None = None
    for candidate in candidates:
        try:
            data = await _fetch_json(candidate)
        except (ValueError, httpx.HTTPError) as exc:
            first_error = first_error or _error(exc)
            continue
        if _is_metadata(data):
            return data
        first_error = first_error or "it declares no authorization or token endpoint"
    return {"error": first_error or "it could not be fetched"}


async def fetch_discovery(document: SpecDocument, url_base: str | None) -> dict[str, Any]:
    """``discovery URL → document`` (or ``{"error": reason}``) for *document*'s schemes."""
    targets = discovery_urls(document, url_base)
    if not targets:
        return {}
    results = await asyncio.gather(*(_discover(url, kind) for url, kind in targets))
    return {url: result for (url, _kind), result in zip(targets, results, strict=True)}
