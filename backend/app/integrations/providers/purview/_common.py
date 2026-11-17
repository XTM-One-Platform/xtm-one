"""Microsoft Purview integration - shared clients, helpers, and formatters.

Microsoft Purview is Microsoft's unified data governance, risk, and compliance
platform. This integration spans its two product pillars, each exposed on a
different API surface:

* **Risk & Compliance** - via the Microsoft Graph ``security`` namespace
  (``https://graph.microsoft.com``): eDiscovery (Premium), the Purview Audit
  log search, Information Protection sensitivity labels, Records Management /
  Data Lifecycle retention labels, and Subject Rights Requests (GDPR/DSR).
* **Unified Data Governance** - via the Purview Data Map / Apache Atlas REST API
  (``https://{account}.purview.azure.com``, audience ``https://purview.azure.net``):
  catalog discovery (search), data assets, lineage, and the business glossary.

Two authentication modes are supported transparently to every tool (the same
Azure AD app-registration model used by the Entra ID / Defender integrations):

* **Delegated OAuth** (default) - the tools mint a per-audience token by
  redeeming the integration's refresh token for that resource's ``/.default``
  scope (the ``token_acquirer`` injected by ``integration_tools``), exactly like
  Microsoft Defender XDR. This covers both the Graph and the Purview Data Map
  audiences from a single sign-in.
* **App-only client credentials** (recommended for unattended governance /
  compliance automation) - ``token`` is the compound
  ``tenant_id||client_id||client_secret||account_name`` assembled by
  ``integration_tools._get_token``; each audience token is minted via the
  client-credentials grant and cached in-process.

The Purview ``account_name`` is only required for the Data Map (governance)
tools; the compliance tools run against Microsoft Graph and need no account.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.http import (
    DEFAULT_TIMEOUT,
    app_token_cache_key,
    bearer_auth_headers,
    format_entity_list,
    format_pagination_hint,
    get_cached_app_token,
    parse_compound_token,
    safe_request,
    set_cached_app_token,
)

logger = logging.getLogger(__name__)

PROVIDER = "Microsoft Purview"

# Microsoft Graph (Risk & Compliance pillar). Version is appended per call so
# the audit-log-search tools can target ``/beta`` (the audit query API is only
# available under beta) while everything else uses ``/v1.0``.
GRAPH_HOST = "https://graph.microsoft.com"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Purview Data Map / Apache Atlas (Unified Data Governance pillar).
PURVIEW_SCOPE = "https://purview.azure.net/.default"
# Apache Atlas + Discovery data-plane API version (GA).
MAP_API_VERSION = "2023-09-01"
# Account data-plane API version (collections live here).
ACCOUNT_API_VERSION = "2019-11-01-preview"

# Token-acquirer scope -> token callback (delegated OAuth, per audience).
TokenAcquirer = Callable[[str], Awaitable[str | None]]

_AUTH_ERROR = (
    "Error: Failed to authenticate with Microsoft Purview (Azure AD). In OAuth "
    "(sign-in) mode, re-authorize this integration - its refresh token may be "
    "missing or expired. In app-only (API key) mode, check the tenant ID, "
    "client ID, and client secret, and ensure admin consent was granted for the "
    "required Microsoft Graph application permissions (and, for Data Map tools, "
    "that the service principal holds a Purview data-plane role such as Data "
    "Curator)."
)

# Returned by Data Map (governance) tools when no Purview account name is set.
_NO_ACCOUNT_ERROR = (
    "Error: This Microsoft Purview integration has no governance account name "
    "configured, so the Data Map (data catalog) tools are unavailable. Set the "
    "Purview account name on the integration to enable catalog search, assets, "
    "lineage, and glossary tools."
)

# Compliance domains whose Microsoft Graph endpoints do NOT support app-only
# (application) permissions - only delegated (signed-in user) access. Tools in
# these domains short-circuit with an actionable message in app-only mode
# instead of surfacing a confusing 401/403 from Graph.
DELEGATED_ONLY_HINT = (
    "This Microsoft Purview capability is only available with delegated "
    "(signed-in user) access - Microsoft Graph does not expose it to app-only "
    "(API key) credentials. Re-create or re-authorize this integration using "
    "OAuth (sign-in) mode to use it."
)


async def mint_app_token(
    tenant_id: str, client_id: str, client_secret: str, scope: str
) -> str | None:
    """Mint (and cache) an app-only Azure AD token for *scope*.

    App-only tokens are cached in-process (keyed by a hash of the credentials +
    scope) until shortly before they expire, so a multi-tool agentic run does
    not mint a fresh token from Azure AD on every call (which risks throttling).
    """
    tenant_id = (tenant_id or "").strip()
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not (tenant_id and client_id and client_secret):
        return None

    cache_key = app_token_cache_key(
        "purview", tenant_id, client_id, client_secret, scope
    )
    cached = get_cached_app_token(cache_key)
    if cached:
        return cached

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    try:
        async with egress_client(timeout=15) as client:
            resp = await client.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": scope,
                },
            )
            if resp.is_success:
                data = resp.json()
                access = data.get("access_token")
                if access:
                    set_cached_app_token(cache_key, access, data.get("expires_in"))
                return access
            logger.warning(
                "Purview Azure AD token request failed (scope=%s): %s",
                scope,
                resp.text[:300],
            )
    except (httpx.HTTPError, ValueError) as exc:
        # ValueError covers a non-JSON success body (e.g. a proxy returning HTML
        # on a 200); fall through to the standard auth-error path.
        logger.warning("Purview Azure AD token request error (scope=%s): %s", scope, format_exception_message(exc))
    return None


async def get_app_graph_token(
    tenant_id: str, client_id: str, client_secret: str
) -> str | None:
    """Mint an app-only Microsoft Graph token (used by the connectivity test)."""
    return await mint_app_token(tenant_id, client_id, client_secret, GRAPH_SCOPE)


def map_base_url(account_name: str) -> str:
    """Return the Data Map base URL for a Purview *account_name*.

    Accepts either a bare account name (``mycompany``) or a full host /
    URL (``https://mycompany.purview.azure.com``) and normalises to the
    ``https://{account}.purview.azure.com`` data-plane origin.
    """
    value = (account_name or "").strip().rstrip("/")
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        host = parsed.netloc or parsed.path
        return f"https://{host}"
    if "." in value:  # already a host (e.g. mycompany.purview.azure.com)
        return f"https://{value}"
    return f"https://{value}.purview.azure.com"


class PurviewClient:
    """Dual-audience, dual-auth Microsoft Purview client.

    Every tool issues requests through :meth:`graph_request` (compliance) or
    :meth:`map_request` (governance / Data Map), so the delegated-vs-app-only
    distinction and the two audiences live in exactly one place. Errors are
    returned (never raised) as strings suitable for surfacing to the LLM.

    Three credential modes:

    * **delegated_acquirer** - runtime OAuth: a ``token_acquirer`` mints a token
      per Microsoft audience by redeeming the refresh token.
    * **app_only** - ``token`` is ``tenant_id||client_id||client_secret||account``
      and tokens are minted via the client-credentials grant.
    * **graph_direct** - ``token`` is a raw delegated Graph access token (used by
      the OAuth-connect ``validate_connection`` / ``get_user_info`` path).
    """

    def __init__(
        self,
        token: str = "",
        *,
        token_acquirer: TokenAcquirer | None = None,
        account_name: str = "",
    ) -> None:
        self._acquirer = token_acquirer
        self.tenant_id = self.client_id = self.client_secret = ""
        self.graph_access_token = ""

        if token_acquirer is not None:
            self.mode = "delegated_acquirer"
            account_from_token = ""
        elif "||" in (token or ""):
            self.mode = "app_only"
            tenant, cid, secret, account_from_token = parse_compound_token(token, 4)
            self.tenant_id = tenant.strip()
            self.client_id = cid.strip()
            self.client_secret = secret.strip()
        else:
            self.mode = "graph_direct"
            self.graph_access_token = (token or "").strip()
            account_from_token = ""

        self.account_name = (account_name or account_from_token or "").strip()

    @property
    def app_only(self) -> bool:
        return self.mode == "app_only"

    @property
    def has_account(self) -> bool:
        return bool(self.account_name)

    # -- token resolution -------------------------------------------------

    async def _graph_token(self) -> str | None:
        if self.mode == "graph_direct":
            return self.graph_access_token or None
        if self.mode == "delegated_acquirer":
            return await self._acquirer(GRAPH_SCOPE)  # type: ignore[misc]
        return await mint_app_token(
            self.tenant_id, self.client_id, self.client_secret, GRAPH_SCOPE
        )

    async def _map_token(self) -> str | None:
        if self.mode == "graph_direct":
            return None  # Data Map needs its own audience, unavailable here.
        if self.mode == "delegated_acquirer":
            return await self._acquirer(PURVIEW_SCOPE)  # type: ignore[misc]
        return await mint_app_token(
            self.tenant_id, self.client_id, self.client_secret, PURVIEW_SCOPE
        )

    # -- Microsoft Graph (compliance) -------------------------------------

    async def graph_request(
        self,
        method: str,
        path: str,
        action: str,
        *,
        version: str = "v1.0",
        extra_headers: dict[str, str] | None = None,
        expected_statuses: set[int] | None = None,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        """Execute a Microsoft Graph request. ``path`` is a ``/...`` path or URL."""
        access = await self._graph_token()
        if not access:
            return None, _AUTH_ERROR
        url = path if path.startswith("http") else f"{GRAPH_HOST}/{version}{path}"
        headers = bearer_auth_headers(
            access,
            extra={"Content-Type": "application/json", **(extra_headers or {})},
        )
        async with egress_client(timeout=DEFAULT_TIMEOUT) as client:
            return await safe_request(
                client,
                method,
                url,
                PROVIDER,
                action,
                headers=headers,
                expected_statuses=expected_statuses,
                **kwargs,
            )

    async def graph_get_json(
        self, path: str, action: str, *, version: str = "v1.0", **kwargs: Any
    ) -> tuple[dict[str, Any] | None, str | None]:
        """GET a Graph resource and decode JSON. ``({}, None)`` on empty body."""
        resp, err = await self.graph_request(
            "get", path, action, version=version, **kwargs
        )
        if err:
            return None, err
        return _decode_json(resp), None

    async def graph_paged(
        self,
        path: str,
        action: str,
        *,
        top: int,
        version: str = "v1.0",
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, str]:
        """Fetch a single page of a Graph collection.

        Returns ``(items, error, next_skiptoken)`` - the ``$skiptoken`` from the
        ``@odata.nextLink`` (empty when there are no more pages).
        """
        merged = {"$top": top, **(params or {})}
        data, err = await self.graph_get_json(
            path, action, version=version, params=merged, extra_headers=extra_headers
        )
        if err:
            return [], err, ""
        items = (data or {}).get("value", []) or []
        return items, None, next_skiptoken(data or {})

    # -- Purview Data Map (governance) ------------------------------------

    async def map_request(
        self,
        method: str,
        path: str,
        action: str,
        *,
        api_version: str = MAP_API_VERSION,
        params: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        """Execute a Data Map request against ``https://{account}.purview.azure.com``."""
        base = map_base_url(self.account_name)
        if not base:
            return None, _NO_ACCOUNT_ERROR
        access = await self._map_token()
        if not access:
            return None, _AUTH_ERROR
        url = f"{base}{path}"
        merged_params = {"api-version": api_version, **(params or {})}
        headers = bearer_auth_headers(access, extra={"Content-Type": "application/json"})
        async with egress_client(timeout=DEFAULT_TIMEOUT) as client:
            return await safe_request(
                client,
                method,
                url,
                PROVIDER,
                action,
                headers=headers,
                params=merged_params,
                expected_statuses=expected_statuses,
                **kwargs,
            )

    async def map_get_json(
        self, path: str, action: str, **kwargs: Any
    ) -> tuple[dict[str, Any] | None, str | None]:
        resp, err = await self.map_request("get", path, action, **kwargs)
        if err:
            return None, err
        return _decode_json(resp), None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _decode_json(resp: Any) -> dict[str, Any]:
    """Best-effort JSON decode of a success response to a dict (``{}`` on failure).

    A non-JSON or non-dict body (e.g. a 204, a JSON array, or a proxy / WAF HTML
    page served with a 200) degrades to ``{}`` so no tool ever raises on a
    surprising success body.
    """
    if resp is None:
        return {}
    try:
        data = resp.json()
    except (ValueError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


def safe_json(resp: Any) -> dict[str, Any]:
    """Decode an already-successful response body to a dict (``{}`` on failure)."""
    return _decode_json(resp)


def next_skiptoken(data: dict[str, Any]) -> str:
    """Extract the ``$skiptoken`` from a Graph ``@odata.nextLink``, or ``""``."""
    next_link = data.get("@odata.nextLink")
    if not next_link:
        return ""
    query = parse_qs(urlparse(next_link).query)
    token = query.get("$skiptoken") or query.get("$skipToken")
    return token[0] if token else ""


def page_hint(skiptoken: str) -> str:
    """Pagination hint string for a Graph collection (``skiptoken`` param)."""
    return format_pagination_hint(skiptoken, param_name="skiptoken")


def dt(value: str | None) -> str:
    """Trim an ISO 8601 timestamp to second precision for display."""
    return (value or "")[:19] if value else "-"


def trunc(text: str | None, max_len: int = 300) -> str:
    """Truncate *text* to *max_len* characters with an ellipsis."""
    if not text:
        return ""
    text = str(text)
    return text[:max_len] + ("..." if len(text) > max_len else "")


def odata_quote(value: str) -> str:
    """Escape a value for use inside a single-quoted OData string literal."""
    return value.replace("'", "''")


def fmt_list(
    items: list[dict[str, Any]],
    formatter: Callable[[dict[str, Any]], str],
    *,
    noun: str,
    skiptoken: str = "",
    total: int | None = None,
    empty_msg: str | None = None,
) -> str:
    """Format a collection with a header, body, and optional pagination hint."""
    body = format_entity_list(
        items, formatter, noun=noun, total=total, empty_msg=empty_msg
    )
    return body + page_hint(skiptoken)
