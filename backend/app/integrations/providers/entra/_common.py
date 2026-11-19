"""Microsoft Entra ID integration - shared client, helpers, and formatters.

Microsoft Entra ID (formerly Azure Active Directory) is Microsoft's identity
and access management (IAM) platform. This integration drives the whole
identity lifecycle and identity-security loop through the Microsoft Graph API
(``https://graph.microsoft.com/v1.0``):

* Users - directory lifecycle (list/get/create/update/delete), offboarding
  (disable, revoke sessions, reset password), manager + group membership +
  license management.
* Groups - security / Microsoft 365 groups, membership and ownership.
* Directory roles (RBAC) - who holds privileged roles, assign / remove roles
  (the privileged-access audit + governance surface).
* Applications & service principals - app registrations, enterprise apps,
  credential expiry hygiene, OAuth2 consent grants.
* Devices - registered / joined devices.
* Sign-in & directory audit logs - the identity investigation trail.
* Identity Protection - risky users, risk detections, confirm-compromised /
  dismiss remediation (Entra ID P2).
* Organization, domains, license SKUs, conditional access policies, and the
  directory recycle bin (deleted items).

Two authentication modes are supported transparently to every tool:

* **Delegated OAuth** (default) - ``token`` is a Microsoft Graph access token
  obtained via the standard OAuth flow; the integration's refresh token is
  redeemed on 401 via the injected ``refresh_fn`` (same pattern as Intune /
  Microsoft).
* **App-only client credentials** (recommended for unattended IAM automation)
  - ``token`` is the compound ``tenant_id||client_id||client_secret`` assembled
  by ``integration_tools._get_token``; a short-lived Graph token is minted via
  the client-credentials grant and cached in-process.

The single Graph audience makes the mode selection trivial: a compound token
(contains ``||``) means app-only, otherwise delegated.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.http import (
    DEFAULT_TIMEOUT,
    AutoRefreshClient,
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

PROVIDER = "Microsoft Entra"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Well-known id of the single password authentication method on every user -
# the target for the password-reset action (see Microsoft Graph docs).
PASSWORD_METHOD_ID = "28c10230-6103-485e-b985-444c60001490"  # noqa: S105

_AUTH_ERROR = (
    "Error: Failed to authenticate with Microsoft Entra (Azure AD). In OAuth "
    "(sign-in) mode, re-authorize this integration - its refresh token may be "
    "missing or expired. In app-only (API key) mode, check the tenant ID, "
    "client ID, and client secret, and ensure admin consent was granted for "
    "the required Microsoft Graph application permissions."
)

RefreshFn = Callable[[], Awaitable[str | None]]


# ---------------------------------------------------------------------------
# App-only (client-credentials) Graph token
# ---------------------------------------------------------------------------


async def get_app_token(
    tenant_id: str, client_id: str, client_secret: str
) -> str | None:
    """Mint (and cache) an app-only Microsoft Graph access token.

    App-only tokens are cached in-process (keyed by a hash of the credentials)
    until shortly before they expire, so a multi-tool agentic run does not mint
    a fresh token from Azure AD on every single call (which risks throttling).
    """
    cache_key = app_token_cache_key(
        "entra", tenant_id, client_id, client_secret, GRAPH_SCOPE
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
                    "scope": GRAPH_SCOPE,
                },
            )
            if resp.is_success:
                data = resp.json()
                access = data.get("access_token")
                if access:
                    set_cached_app_token(cache_key, access, data.get("expires_in"))
                return access
            logger.warning(
                "Entra Azure AD token request failed: %s", resp.text[:300]
            )
    except (httpx.HTTPError, ValueError) as exc:
        # ValueError covers a non-JSON success body from resp.json() (e.g. a
        # proxy returning HTML on a 200); fall through to the standard
        # auth-error path instead of crashing the tool / connection test.
        logger.warning("Entra Azure AD token request error: %s", format_exception_message(exc))
    return None


# ---------------------------------------------------------------------------
# Dual-auth Graph client
# ---------------------------------------------------------------------------


class EntraClient:
    """A small Microsoft Graph client that handles both auth modes.

    Every tool issues requests through :meth:`request` / :meth:`get_json` /
    :meth:`paged`, so the delegated-vs-app-only distinction lives in exactly
    one place. Errors are returned (never raised) as strings suitable for
    surfacing directly to the LLM.
    """

    def __init__(self, token: str, refresh_fn: RefreshFn | None = None) -> None:
        self.refresh_fn = refresh_fn
        self.app_only = "||" in token
        if self.app_only:
            tenant_id, client_id, client_secret = parse_compound_token(token, 3)
            self.tenant_id = tenant_id.strip()
            self.client_id = client_id.strip()
            self.client_secret = client_secret.strip()
            self.access_token = ""
        else:
            self.access_token = (token or "").strip()
            self.tenant_id = self.client_id = self.client_secret = ""

    async def request(
        self,
        method: str,
        path: str,
        action: str,
        *,
        extra_headers: dict[str, str] | None = None,
        expected_statuses: set[int] | None = None,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        """Execute a Graph request. ``path`` may be a ``/...`` path or a full URL.

        Returns ``(response, None)`` on success and ``(None, error_string)`` on
        failure - except when ``expected_statuses`` is supplied, in which case a
        non-2xx response in that set is returned as ``(response, error_string)``
        so the caller can branch on ``resp.status_code``.
        """
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        base_extra = {"Content-Type": "application/json", **(extra_headers or {})}

        if self.app_only:
            access = await get_app_token(
                self.tenant_id, self.client_id, self.client_secret
            )
            if not access:
                return None, _AUTH_ERROR
            req_headers = bearer_auth_headers(access, extra=base_extra)
            async with egress_client(timeout=DEFAULT_TIMEOUT) as client:
                return await safe_request(
                    client,
                    method,
                    url,
                    PROVIDER,
                    action,
                    headers=req_headers,
                    expected_statuses=expected_statuses,
                    **kwargs,
                )

        if not self.access_token:
            return None, _AUTH_ERROR
        # Delegated: bake the headers into the AutoRefreshClient so a 401/403
        # token refresh mutates this very dict; we then copy the refreshed
        # access token back so later calls reuse it.
        req_headers = bearer_auth_headers(self.access_token, extra=base_extra)
        async with AutoRefreshClient(req_headers, self.refresh_fn) as client:
            resp, err = await safe_request(
                client,
                method,
                url,
                PROVIDER,
                action,
                expected_statuses=expected_statuses,
                **kwargs,
            )
        new_auth = req_headers.get("Authorization", "")
        if new_auth.startswith("Bearer "):
            self.access_token = new_auth[len("Bearer ") :]
        return resp, err

    async def get_json(
        self, path: str, action: str, **kwargs: Any
    ) -> tuple[dict[str, Any] | None, str | None]:
        """GET and decode JSON. Returns ``(data, None)`` or ``(None, error)``.

        A genuinely empty success body decodes to ``({}, None)``. A non-empty
        body that is not valid JSON (e.g. a proxy / WAF HTML login page served
        with a 200) is surfaced as an error rather than masqueraded as an empty
        result, so tools and the connection check fail actionably instead of
        silently behaving as if the tenant has no data.
        """
        resp, err = await self.request("get", path, action, **kwargs)
        if err:
            return None, err
        try:
            return resp.json(), None  # type: ignore[union-attr]
        except (ValueError, AttributeError):
            content = getattr(resp, "content", b"") or b""
            if content.strip():
                return None, (
                    f"Error: Microsoft Entra returned a non-JSON response while "
                    f"trying to {action} (often a proxy / WAF login page or an "
                    f"outage rather than valid data)."
                )
            return {}, None

    async def paged(
        self,
        path: str,
        action: str,
        *,
        top: int,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, str]:
        """Fetch a single page of a Graph collection.

        Returns ``(items, error, next_skiptoken)``. ``next_skiptoken`` is the
        opaque ``$skiptoken`` from ``@odata.nextLink`` (empty when there are no
        more pages), surfaced so the caller can offer pagination.
        """
        merged = {"$top": top, **(params or {})}
        data, err = await self.get_json(
            path, action, params=merged, extra_headers=extra_headers
        )
        if err:
            return [], err, ""
        items = (data or {}).get("value", []) or []
        return items, None, next_skiptoken(data or {})


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def safe_json(resp: Any) -> dict[str, Any]:
    """Best-effort decode of a response body to a dict (``{}`` on failure).

    Write/confirm actions call this on an already-successful response to read
    back the created/updated object. A non-JSON or empty body (e.g. a 204, or a
    proxy / WAF page) must never raise out of a tool, so a decode failure - or a
    non-dict payload - degrades to ``{}`` and the caller falls back to its
    ``.get(..., default)`` values. Mirrors the hardening in :meth:`EntraClient.get_json`.
    """
    if resp is None:
        return {}
    try:
        data = resp.json()
    except (ValueError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


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
    """Escape a value for use inside a single-quoted OData string literal.

    A literal single quote is escaped by doubling it. Values such as display
    names or UPNs can legitimately contain an apostrophe (e.g. ``O'Brien``),
    which would otherwise terminate the literal early and break the ``$filter``
    (or allow filter injection).
    """
    return value.replace("'", "''")


_QUALIFIED_SEARCH = re.compile(r"^[A-Za-z][A-Za-z0-9_]*:")


def _quote_search_term(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _search_expression(search: str, search_fields: tuple[str, ...]) -> str:
    term = search.strip()
    if not search_fields or _QUALIFIED_SEARCH.match(term):
        return _quote_search_term(term)
    return " OR ".join(_quote_search_term(f"{field}:{term}") for field in search_fields)


def build_search_params(
    *,
    search: str = "",
    odata_filter: str = "",
    select: str = "",
    orderby: str = "",
    skiptoken: str = "",
    search_fields: tuple[str, ...] = (),
) -> tuple[dict[str, Any], dict[str, str]]:
    """Build the ``params`` + ``extra_headers`` for a Graph list/search call.

    ``$search`` (and ``$count``) require the ``ConsistencyLevel: eventual``
    header (advanced query). Graph only accepts ``"property:value"`` clauses
    in ``$search`` on directory objects, so a free-text term (no
    ``property:`` prefix) is expanded into one clause per ``search_fields``
    property joined by ``OR``; a term the caller already qualified
    (``displayName:jane``) is kept as one clause. Returns
    ``(params, extra_headers)``.
    """
    params: dict[str, Any] = {}
    extra_headers: dict[str, str] = {}
    if search:
        # Graph $search uses the "field:term" or quoted-phrase syntax; default
        # to the broad displayName + mail search a human would expect. Escape
        # embedded backslashes/quotes before the outer quote-wrap so a term
        # containing either stays a syntactically valid quoted value (an
        # unescaped " or \ otherwise makes Graph return HTTP 400).
        params["$search"] = _search_expression(search, search_fields)
        params["$count"] = "true"
        extra_headers["ConsistencyLevel"] = "eventual"
    if odata_filter:
        params["$filter"] = odata_filter
    if select:
        params["$select"] = select
    if orderby:
        params["$orderby"] = orderby
    if skiptoken:
        params["$skiptoken"] = skiptoken
    return params, extra_headers


def fmt_list(
    items: list[dict[str, Any]],
    formatter: Callable[[dict[str, Any]], str],
    *,
    noun: str,
    skiptoken: str = "",
    empty_msg: str | None = None,
) -> str:
    """Format a Graph collection with a header, body, and pagination hint."""
    body = format_entity_list(items, formatter, noun=noun, empty_msg=empty_msg)
    return body + page_hint(skiptoken)
