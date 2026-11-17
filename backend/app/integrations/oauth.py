# SPDX-License-Identifier: Apache-2.0

"""Generic OAuth 2.0 flow handler – uses per-integration credentials from DB.

No global env vars for client IDs/secrets. Each Integration row stores its
own client_id_enc / client_secret_enc, configured via the UI.
"""

import base64
import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from app.core.egress import egress_client

logger = logging.getLogger(__name__)

from app.core.config import settings
from app.core.redis import redis_client
from app.core.security import decrypt_value
from app.core.utils import format_exception_message
from app.integrations.token_endpoint import assert_token_endpoint_allowed
from app.services.mcp_oauth_discovery import generate_pkce_pair, oauth_redirect_uri
from app.services.vault import reveal_json_field

# Microsoft Graph / Azure AD providers whose /common/ OAuth endpoint must be
# rewritten to the tenant-specific /{tenant_id}/ endpoint when a tenant_id is
# configured. Single-tenant app registrations reject /common (AADSTS50194), so
# every Microsoft provider that ships its authorize/token URLs on /common must
# be listed here (microsoft, intune, defender, sentinel, entra, and purview).
_ENTRA_ID_PROVIDERS = {
    "microsoft",
    "intune",
    "defender",
    "sentinel",
    "entra",
    "purview",
}

# Providers whose OAuth token endpoint requires HTTP Basic Authentication
# (base64 of "client_id:client_secret" in the Authorization header) together
# with a JSON body.  They reject the standard credentials-in-body / form-encoded
# style used by most OAuth 2.0 servers with a 401 ``invalid_client`` error.
#
# This convention applies to BOTH the authorization-code exchange and the
# refresh-token grant, so it must be honoured everywhere a token endpoint is
# called: code exchange (this module), inline refresh (``integration_tools``),
# and scheduled refresh (``token_refresh``).
_BASIC_AUTH_TOKEN_PROVIDERS = frozenset({"notion"})

# Providers whose token endpoint takes the client credentials as HTTP Basic
# auth but the grant as a form-encoded body (DocuSign refuses a JSON body).
_BASIC_AUTH_FORM_PROVIDERS = frozenset({"docusign"})

# Providers whose authorization-code flow sends PKCE (RFC 7636, S256).
_PKCE_PROVIDERS = frozenset({"x", "openapi"})

# Synthetic lifetime used when the token endpoint omits ``expires_in``.
# Notion without token rotation (and a few other providers) issue long-lived
# access tokens with no expiry field.  Leaving ``token_expires_at`` NULL
# makes ``refresh_expiring_tokens`` re-select the row on every 60s scheduler
# tick — the docstring's "hourly safety net" was never gated, so a single
# Notion integration was refreshed every minute.  Stamping a 1-hour TTL lets
# the existing ``token_expires_at < now + 15min`` predicate wait ~45 minutes
# before the next refresh.
UNKNOWN_EXPIRY_TTL_SECONDS = 3600


#: The longest lifetime stamped on a token, whatever a provider says: a far
#: future ``exp`` or ``expires_in`` (a "never expires" sentinel) must not make
#: ``token_expires_at`` overflow after the refresh token rotated.
MAX_TOKEN_LIFETIME_SECONDS = 365 * 24 * 3600


def jwt_expiry_seconds(access_token: Any) -> int | None:
    """Seconds left before a JWT access token's ``exp`` claim, ``None`` when unreadable.

    Read without verifying the signature: the value only decides when the
    token is refreshed, never whether it is trusted, and the worst a forged
    ``exp`` can do is make the platform refresh earlier or later than it
    should. An opaque token, a token with no ``exp`` or a non-finite one gives
    ``None``; an ``exp`` already past gives zero or less (often clock skew
    between the authorization server and this platform). Never more than
    :data:`MAX_TOKEN_LIFETIME_SECONDS`.
    """
    if not isinstance(access_token, str) or access_token.count(".") != 2:
        return None
    payload = access_token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp = int(claims["exp"])
    except Exception:  # not JSON, no exp, not a number, inf / nan (OverflowError, ValueError)
        return None
    return min(exp - int(datetime.now(UTC).timestamp()), MAX_TOKEN_LIFETIME_SECONDS)


def apply_oauth_token_expiry(target: Any, tokens: dict[str, Any]) -> None:
    """Stamp ``token_expires_at`` from a token-endpoint payload.

    Uses ``expires_in`` / ``ext_expires_in`` when the provider reports a
    positive lifetime, else the ``exp`` claim of a JWT access token
    (an authorization server issuing 10-minute JWTs without ``expires_in`` had
    them refreshed only after the synthetic hour). An ``exp`` already past is
    stamped as expiring now - floored at one second - and logged, since the
    usual cause is a skewed clock: taking it for an hour would bring that bug
    back. Otherwise stamps a synthetic 1-hour TTL so a missing expiry cannot put
    the row on the every-tick refresh sweep - for a token that can be
    refreshed. One with no refresh token (in *tokens* or on *target*) keeps no
    expiry: nothing can renew it, the sweep never selects it, and a
    synthetic hour only made a non-expiring token (a GitHub OAuth App's) read
    as expired an hour later. Never more than
    :data:`MAX_TOKEN_LIFETIME_SECONDS`, so a huge value cannot overflow.
    """
    raw = tokens.get("expires_in") or tokens.get("ext_expires_in")
    seconds: int | None = None
    if raw not in (None, ""):
        try:
            seconds = int(raw)
        except (TypeError, ValueError, OverflowError):
            seconds = None
    if seconds is not None and seconds <= 0:
        seconds = None
    if seconds is None:
        seconds = jwt_expiry_seconds(tokens.get("access_token"))
        if seconds is not None and seconds <= 0:
            logger.warning(
                "The OAuth access token issued for %s expired %ss ago when it was received; "
                "check the clock of this platform and of the authorization server",
                getattr(target, "id", "a connection"),
                -seconds,
            )
            seconds = 1
    if seconds is None:
        if not tokens.get("refresh_token") and not getattr(target, "refresh_token_enc", None):
            target.token_expires_at = None
            return
        seconds = UNKNOWN_EXPIRY_TTL_SECONDS
    seconds = min(seconds, MAX_TOKEN_LIFETIME_SECONDS)
    target.token_expires_at = datetime.now(UTC) + timedelta(seconds=seconds)


def apply_mcp_token_expiry(server: Any, tokens: dict[str, Any]) -> None:
    """:func:`apply_oauth_token_expiry` for an MCP server, which also records when its token was received.

    ``token_issued_at`` and ``token_expires_at`` give the token's lifetime, from
    which the refresh sweep schedules its renewal
    (``token_refresh.mcp_refresh_due_at``).
    """
    server.token_issued_at = datetime.now(UTC)
    apply_oauth_token_expiry(server, tokens)


def provider_uses_basic_auth_token(provider: str) -> bool:
    """Whether ``provider``'s token endpoint requires HTTP Basic auth + JSON body."""
    return provider in _BASIC_AUTH_TOKEN_PROVIDERS


def build_token_request(
    provider: str,
    client_id: str,
    client_secret: str,
    params: dict[str, str],
    *,
    json_provider: bool = False,
) -> dict[str, Any]:
    """Build the httpx ``post`` keyword arguments for an OAuth token request.

    ``params`` must contain only grant-specific fields — ``grant_type`` plus
    ``code`` / ``redirect_uri`` (authorization-code grant) or ``refresh_token``
    (refresh grant), optionally ``scope`` / ``code_verifier``.  The client
    credentials are added here according to the provider's required style:

    * **Basic-auth providers (Notion):** credentials go into the HTTP
      ``Authorization: Basic`` header and the body is sent as JSON.  Notion
      rejects credentials-in-body with ``401 invalid_client``.
    * **Basic-auth, form-body providers (DocuSign):** the same header, the
      grant form-encoded.
    * **JSON providers (Atlassian/Jira, via ``json_provider=True``):**
      credentials go into a JSON body.
    * **Default:** credentials go into a form-encoded body.

    Returns a dict suitable for ``await client.post(url, **kwargs)`` containing
    a ``headers`` entry plus either ``json`` or ``data``.
    """
    headers = {"Accept": "application/json"}
    body = dict(params)

    if provider_uses_basic_auth_token(provider) or provider in _BASIC_AUTH_FORM_PROVIDERS:
        basic = base64.b64encode(
            f"{client_id}:{client_secret}".encode()
        ).decode("ascii")
        headers["Authorization"] = f"Basic {basic}"
        if provider in _BASIC_AUTH_FORM_PROVIDERS:
            return {"data": body, "headers": headers}
        return {"json": body, "headers": headers}

    body["client_id"] = client_id
    # RFC 6749 §2.3.1: a public client (no secret) leaves the parameter out;
    # an empty one is an authentication attempt that strict servers refuse.
    if client_secret:
        body["client_secret"] = client_secret
    if json_provider:
        return {"json": body, "headers": headers}
    return {"data": body, "headers": headers}


def substitute_tenant(url: str, tenant_id: str) -> str:
    """Replace the ``/common/`` segment with *tenant_id*, encoded as ONE path segment.

    The tenant id is configuration text: percent-encoding it (``/`` included)
    keeps it inside its segment, so it can never add path, query or fragment
    parts to the identity provider URL.
    """
    return url.replace("/common/", f"/{quote(str(tenant_id).strip(), safe='')}/")


_DOCUSIGN_HOSTS = {"production": "account.docusign.com", "demo": "account-d.docusign.com"}


def docusign_environment_url(url: str | None, extra: dict) -> str | None:
    """*url* on the DocuSign host of ``extra["environment"]`` (``demo`` or production).

    The catalog ships the production hosts; a developer (demo) account signs
    in and refreshes on ``account-d.docusign.com``. Applied to the authorize,
    code-exchange and refresh requests alike.
    """
    if not url:
        return url
    environment = str(extra.get("environment") or "production").strip().lower()
    wanted = _DOCUSIGN_HOSTS.get(environment, _DOCUSIGN_HOSTS["production"])
    for host in _DOCUSIGN_HOSTS.values():
        url = url.replace(f"://{host}/", f"://{wanted}/")
    return url


async def provider_token_url(integration, token_url: str | None) -> str | None:
    """*token_url* adjusted to the integration's configuration: the DocuSign
    environment host, a vault reference resolved (#6080). The Entra tenant
    substitution stays with its callers."""
    if integration.provider != "docusign" or not token_url or not integration.extra_config_enc:
        return token_url
    try:
        extra = await reveal_json_field(integration, "extra_config_enc")
    except Exception as exc:
        logger.warning("Failed to read the DocuSign environment: %s", format_exception_message(exc))
        return token_url
    return docusign_environment_url(token_url, extra if isinstance(extra, dict) else {})


async def _resolve_tenant_urls(integration, authorize_url: str, token_url: str | None) -> tuple[str, str | None]:
    """Replace /common/ with tenant ID for Microsoft Entra ID integrations.

    A custom bundle (``custom:``) is one when its OAuth URLs spell ``/common/``
    and it declares a ``tenant_id`` setting (SaaS Manager's Entra ID app).
    DocuSign: the hosts of the configured environment. A tenant id or an
    environment held as a vault reference is resolved (#6080).
    """
    if integration.provider == "docusign":
        return (
            await provider_token_url(integration, authorize_url) or authorize_url,
            await provider_token_url(integration, token_url),
        )
    if (
        integration.provider in _ENTRA_ID_PROVIDERS or integration.provider.startswith("custom:")
    ) and integration.extra_config_enc:
        try:
            extra = await reveal_json_field(integration, "extra_config_enc")
            tenant_id = str((extra if isinstance(extra, dict) else {}).get("tenant_id") or "").strip()
            if tenant_id:
                authorize_url = substitute_tenant(authorize_url, tenant_id)
                if token_url:
                    token_url = substitute_tenant(token_url, tenant_id)
        except Exception as exc:
            logger.warning("Failed to apply tenant_id to OAuth URLs: %s", format_exception_message(exc))
    return authorize_url, token_url


async def build_authorize_url_for_integration(
    integration,
    user_id: str,
    *,
    scopes: str | None = None,
    implicit: bool = False,
    own_account: bool = False,
) -> str:
    """Build OAuth authorization URL using an integration's stored credentials.

    *scopes* overrides the stored ones for this request only; the stored scopes
    are what was last authorized, and only the callback updates them.

    ``implicit`` asks for the access token itself (OAuth2 implicit grant, an
    OpenAPI integration option): the provider returns it in the redirect's
    URL fragment, which only the browser sees, so the callback hands the
    browser to the frontend to post it back (``app.api.oauth_callbacks``).
    There is no code to exchange, hence no PKCE.

    ``own_account`` marks the sign-in of a person's own account on an
    integration whose people each connect theirs: the state carries
    it, so the callback stores the tokens on that person's credential.
    """
    client_id = decrypt_value(integration.client_id_enc)
    authorize_url = integration.authorize_url
    scopes = (integration.scopes if scopes is None else scopes) or ""
    redirect_uri = f"{settings.base_url}/api/oauth/callback"

    if not client_id or not authorize_url:
        raise ValueError("Integration missing client_id or authorize_url")

    # For Microsoft single-tenant apps, substitute the tenant ID
    authorize_url, _ = await _resolve_tenant_urls(integration, authorize_url, None)

    state = secrets.token_urlsafe(32)

    # Store state → {user_id, integration_id, provider} in Redis (5 min TTL)
    state_data = {
        "user_id": user_id,
        "integration_id": str(integration.id),
        "provider": integration.provider,
        **({"flow": "implicit"} if implicit else {}),
        **({"credential": "user"} if own_account else {}),
    }
    redis = await redis_client()
    await redis.set(f"oauth_state:{state}", json.dumps(state_data), ex=300)

    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "token" if implicit else "code",
        "state": state,
    }

    # Slack OAuth v2: user token scopes go in 'user_scope', not 'scope'
    # (using 'scope' tells Slack to install a bot, which we don't want for user integrations)
    if integration.provider == "slack":
        params["user_scope"] = scopes
    elif integration.provider in _ENTRA_ID_PROVIDERS:
        params["scope"] = scopes
        # Microsoft Entra: admin consent is granted tenant-wide via Entra ID.
        # Using prompt=consent would force re-consent and fail when user
        # consent is restricted in the tenant (shows "Approval required").
        # select_account lets the user pick their account without
        # re-triggering the consent flow.  The offline_access scope
        # (in the scopes list) is what provides refresh tokens — these are
        # then redeemed per-audience at runtime (e.g. Graph + Defender for
        # Endpoint, or Azure Resource Manager + Log Analytics).
        params["prompt"] = "select_account"
    elif integration.provider == "google":
        params["scope"] = scopes
        # Google requires access_type=offline + prompt=consent to return
        # a refresh token on subsequent authorizations.
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    elif integration.provider == "jira":
        params["scope"] = scopes
        # Atlassian 3LO requires audience + prompt=consent for refresh tokens.
        # The audience tells Atlassian which API the token will be used for.
        params["audience"] = "api.atlassian.com"
        params["prompt"] = "consent"
    elif integration.provider in _PKCE_PROVIDERS and not implicit:
        params["scope"] = scopes
        # X/Twitter requires OAuth 2.0 with PKCE; OpenAPI integrations send it
        # too — a generic authorization server may require it (OAuth 2.1
        # makes it mandatory), and one that does not ignores the parameters.
        # Generate code_verifier (43-128 chars, URL-safe) and derive code_challenge.
        code_verifier = secrets.token_urlsafe(64)[:96]
        code_challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode("ascii")).digest()
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"

        # Store code_verifier alongside the state data so the callback can pass it
        # to the token exchange.  We update the existing Redis key, keeping
        # everything else it carries (an own-account sign-in's marker).
        await redis.set(
            f"oauth_state:{state}",
            json.dumps({**state_data, "code_verifier": code_verifier}),
            ex=300,
        )
    else:
        params["scope"] = scopes

    # Percent-encode: scope values are space-separated and redirect_uri contains
    # "://" — a raw "&".join would only work by accident of browser auto-encoding
    # and breaks any non-browser consumer or a param containing &/+/#.
    query = urlencode({k: v for k, v in params.items() if v})
    return f"{authorize_url}?{query}"


async def build_authorize_url_for_mcp_server(mcp_server, user_id: str) -> str:
    """Build OAuth authorization URL using an MCP server's stored credentials.

    Two MCP-specific additions on top of the generic authorization-code flow:

    * **PKCE (S256)** — added whenever the client has no secret, which is every
      dynamically registered client and any hand-configured public one. OAuth
      2.1 (and therefore MCP) makes it mandatory there, and it is the only thing
      binding the authorization code to this platform. A confidential client
      keeps the exact request shape it had before, so no existing configuration
      changes behaviour.
    * **``resource`` (RFC 8707)** — sent when the server's protected-resource
      metadata gave us a canonical identifier, so the issued token is
      audience-bound to this MCP server and cannot be replayed against another.

    The PKCE verifier travels in the Redis state blob, which the callback reads
    back to complete the exchange.
    """
    client_id = decrypt_value(mcp_server.client_id_enc)
    authorize_url = mcp_server.authorize_url
    scopes = mcp_server.scopes or ""
    redirect_uri = oauth_redirect_uri()

    if not client_id or not authorize_url:
        raise ValueError("MCP server missing client_id or authorize_url")

    state = secrets.token_urlsafe(32)

    state_data: dict[str, str] = {
        "user_id": user_id,
        "entity_type": "mcp_server",
        "entity_id": str(mcp_server.id),
    }

    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }

    # Public client (no secret): PKCE is what authenticates the token request.
    if not mcp_server.client_secret_enc:
        code_verifier, code_challenge = generate_pkce_pair()
        state_data["code_verifier"] = code_verifier
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"

    if mcp_server.oauth_resource:
        params["resource"] = mcp_server.oauth_resource

    # Store state → {user_id, entity_type, entity_id, …} in Redis (5 min TTL)
    redis = await redis_client()
    await redis.set(f"oauth_state:{state}", json.dumps(state_data), ex=300)

    if scopes:
        params["scope"] = scopes

    # Request offline access for refresh tokens (generic OAuth 2.0 best practice)
    params["access_type"] = "offline"

    query = urlencode({k: v for k, v in params.items() if v})
    return f"{authorize_url}?{query}"


async def exchange_code_for_tokens_with_mcp_server(
    mcp_server,
    code: str,
    *,
    code_verifier: str | None = None,
) -> dict:
    """Exchange authorization code using an MCP server's own client credentials.

    ``client_secret`` is omitted entirely when the client has none. Sending an
    empty one is a client-authentication *attempt*, which an OAuth 2.1 server
    that issued a public client rejects with ``invalid_client`` — so a
    dynamically registered client could never complete the exchange.
    """
    from app.services.vault import reveal_field

    client_id = decrypt_value(mcp_server.client_id_enc)
    # A vault reference is read where the secret is used, like an integration's.
    client_secret = await reveal_field(mcp_server, "client_secret_enc") or ""
    token_url = mcp_server.token_url
    redirect_uri = oauth_redirect_uri()

    if not client_id or not token_url:
        raise ValueError("MCP server missing client_id or token_url")

    payload: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    if code_verifier:
        payload["code_verifier"] = code_verifier
    if mcp_server.oauth_resource:
        payload["resource"] = mcp_server.oauth_resource

    await assert_token_endpoint_allowed(token_url)
    async with egress_client() as client:
        response = await client.post(
            token_url,
            data=payload,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()


async def get_state_data(state: str) -> dict | None:
    """Retrieve and delete state data from Redis, in one atomic step.

    ``GETDEL`` makes the state single-use: of two requests carrying it, only
    one gets the data.
    """
    redis = await redis_client()
    data = await redis.getdel(f"oauth_state:{state}")
    if data:
        return json.loads(data)
    return None


async def exchange_code_for_tokens_with_integration(
    integration,
    code: str,
    *,
    code_verifier: str | None = None,
) -> dict:
    """Exchange authorization code using the integration's own client credentials.

    Parameters
    ----------
    code_verifier:
        PKCE code verifier — required for X/Twitter OAuth 2.0 with PKCE.
        Retrieved from Redis during the callback and forwarded here.
    """
    from app.services.vault import reveal_field

    client_id = decrypt_value(integration.client_id_enc)
    client_secret = await reveal_field(integration, "client_secret_enc") or ""
    token_url = integration.token_url
    redirect_uri = f"{settings.base_url}/api/oauth/callback"

    if not client_id or not token_url:
        raise ValueError("Integration missing client_id or token_url")

    # For Microsoft single-tenant apps, substitute the tenant ID
    _, token_url = await _resolve_tenant_urls(integration, "", token_url)

    params: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }

    # X/Twitter PKCE: include code_verifier in the token exchange
    if code_verifier:
        params["code_verifier"] = code_verifier

    # build_token_request() applies the provider-specific auth style — e.g.
    # Notion needs HTTP Basic auth + a JSON body (it rejects credentials-in-body
    # with 401 invalid_client), while most providers use a form-encoded body.
    request_kwargs = build_token_request(
        integration.provider, client_id, client_secret, params
    )

    await assert_token_endpoint_allowed(token_url)
    client_options: dict[str, Any] = {}
    if integration.provider == "openapi":
        # A user-supplied token URL: pinned connection, client certificate.
        from app.integrations.providers.openapi import token_client_options

        client_options = token_client_options(integration)
    async with egress_client(**client_options) as client:
        response = await client.post(token_url, **request_kwargs)
        response.raise_for_status()
        return response.json()
