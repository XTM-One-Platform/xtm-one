# SPDX-License-Identifier: Apache-2.0

"""OAuth bundles whose service redeems the authorization code (``oauth_exchange``).

Some services keep the identity provider for themselves: the platform runs the
OAuth redirect with the integration's client (Microsoft Entra ID for SaaS
Manager), but the service - not the platform - redeems the code and issues
tokens of its own, which its API alone accepts. A custom bundle with
``auth_type: "oauth"`` declares it in ``metadata.json``::

    "oauth_exchange": {
      "code_path": "/api/auth/social/filigranssooidc/",
      "code_field": "code",
      "redirect_uri_field": "callback_url",
      "access_field": "access",
      "refresh_field": "refresh",
      "refresh_cookie_contains": "refresh",
      "refresh_path": "/api/auth/token/refresh/",
      "refresh_request_field": "refresh"
    }

The callback POSTs ``{code_field: code, redirect_uri_field: <callback URL>}``
to ``{base_url}{code_path}``; a refresh POSTs ``{refresh_request_field:
<refresh token>}`` to ``{base_url}{refresh_path}``. Both answers are read at
``access_field`` / ``refresh_field`` (dot paths; the refresh token may instead
come in a cookie whose name contains ``refresh_cookie_contains``) and mapped to
the RFC 6749 shape - ``access_token``, ``refresh_token``, ``expires_in`` from
the access token's JWT ``exp`` - so the callback and both refresh paths
(``token_refresh`` and the inline refresh of ``integration_tools``) store and
schedule them as any provider's, under the same lock, cooldowns and verdicts.

``base_url`` is the integration's admin-configured ``extra_config`` value; each
URL also passes the token-endpoint policy (``assert_token_endpoint_allowed``)
before a code or a refresh token is sent to it.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import unquote

import httpx

from app.core.egress import egress_client
from app.integrations.oauth import jwt_expiry_seconds
from app.integrations.token_answer import MalformedTokenAnswerError
from app.integrations.token_endpoint import assert_token_endpoint_allowed
from app.services.vault import reveal_json_field

logger = logging.getLogger(__name__)

_REQUIRED_FIELDS = (
    "code_path",
    "code_field",
    "redirect_uri_field",
    "access_field",
    "refresh_path",
    "refresh_request_field",
)
_OPTIONAL_FIELDS = ("refresh_field", "refresh_cookie_contains")
_PATH_FIELDS = ("code_path", "refresh_path")
# One endpoint under the admin's base URL: no query, fragment, placeholder,
# whitespace or ``..`` segment.
_PATH_RE = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*")
# A token goes into a header: visible ASCII only, no space, bounded.
_TOKEN_RE = re.compile(r"[\x21-\x7e]{1,8192}")


def validate_oauth_exchange(raw: Any) -> list[str]:
    """Validate a bundle's ``oauth_exchange`` block. Empty list = valid."""
    prefix = "oauth_exchange"
    if not isinstance(raw, dict):
        return [f"{prefix} must be an object."]
    errors: list[str] = []
    unknown = sorted(str(k) for k in raw if k not in _REQUIRED_FIELDS + _OPTIONAL_FIELDS)
    if unknown:
        errors.append(f"{prefix} has unknown keys: {unknown}.")
    for field in _REQUIRED_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{prefix}.{field} must be a non-empty string.")
    for field in _OPTIONAL_FIELDS:
        value = raw.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            errors.append(f"{prefix}.{field} must be a non-empty string when set.")
    for field in _PATH_FIELDS:
        value = raw.get(field)
        # Decoded first: "%2e%2e" is a ".." segment to the service.
        if isinstance(value, str) and value.strip() and (
            not _PATH_RE.fullmatch(value) or ".." in unquote(value).split("/")
        ):
            errors.append(
                f"{prefix}.{field} must be an absolute path ('/...') without a "
                f"query, fragment, placeholder or '..' segment."
            )
    code_field, redirect_uri_field = raw.get("code_field"), raw.get("redirect_uri_field")
    if isinstance(code_field, str) and code_field.strip() and code_field == redirect_uri_field:
        # One key for both would drop the code from the request body.
        errors.append(f"{prefix}.code_field and {prefix}.redirect_uri_field must differ.")
    return errors


async def oauth_exchange_spec(db: Any, provider: str) -> dict[str, Any] | None:
    """The ``oauth_exchange`` block of a custom provider's bundle, when it declares one.

    A persisted block that no longer validates (edited in the database) is
    ignored with a warning: the standard code exchange then runs and fails at
    the service, which reports it as any failed authorization.
    """
    if not provider.startswith("custom:"):
        return None
    from sqlalchemy import select

    from app.models.custom_integration_definition import CustomIntegrationDefinition

    result = await db.execute(
        select(CustomIntegrationDefinition).where(
            CustomIntegrationDefinition.provider_key == provider
        )
    )
    definition = result.scalar_one_or_none()
    metadata = definition.metadata_json if definition is not None else None
    raw = metadata.get("oauth_exchange") if isinstance(metadata, dict) else None
    if raw is None:
        return None
    errors = validate_oauth_exchange(raw)
    if errors:
        logger.warning(
            "Ignoring the invalid oauth_exchange of custom provider %r: %s",
            provider,
            "; ".join(errors),
        )
        return None
    return raw


async def service_base_url(integration: Any) -> str:
    """The integration's ``extra_config`` ``base_url``, a vault reference resolved (#6080), or ``ValueError``."""
    try:
        extra = await reveal_json_field(integration, "extra_config_enc") or {}
    except Exception:  # corrupt or re-keyed (Fernet InvalidToken), not JSON, a vault that did not answer
        extra = {}
    raw = extra.get("base_url") if isinstance(extra, dict) else None
    base_url = raw.strip().rstrip("/") if isinstance(raw, str) else ""
    if not base_url:
        raise ValueError("the integration has no base_url to exchange the code with")
    return base_url


def _lookup(payload: Any, dotted: str) -> Any:
    value = payload
    for part in dotted.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def read_service_token_answer(spec: dict[str, Any], resp: httpx.Response) -> dict[str, Any]:
    """The service's 2xx token answer in the RFC 6749 shape.

    Raises ``MalformedTokenAnswerError`` - the error every refresh path
    already backs off on - when it carries no usable token.
    """
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise MalformedTokenAnswerError("the service's token answer is not a JSON object")
    access = _lookup(payload, spec["access_field"])
    if not isinstance(access, str) or not _TOKEN_RE.fullmatch(access):
        raise MalformedTokenAnswerError("the service answered without a usable access token")
    tokens: dict[str, Any] = {"access_token": access}

    refresh = _lookup(payload, spec["refresh_field"]) if spec.get("refresh_field") else None
    if refresh is None and spec.get("refresh_cookie_contains"):
        # drf-auth-kit with USE_AUTH_COOKIE sends the refresh token as a cookie.
        needle = spec["refresh_cookie_contains"]
        refresh = next(
            (value for name, value in resp.cookies.items() if needle in name),
            None,
        )
    if refresh is not None:
        if not isinstance(refresh, str) or not _TOKEN_RE.fullmatch(refresh):
            raise MalformedTokenAnswerError("the service's refresh token is not usable")
        tokens["refresh_token"] = refresh

    seconds = jwt_expiry_seconds(access)
    if seconds is not None:
        if seconds <= 0:
            # ``apply_oauth_token_expiry`` would stamp it as expiring now and
            # the row would read connected with a token the service refuses:
            # an answer that is no refresh, backed off like any other.
            raise MalformedTokenAnswerError("the service answered with an expired access token")
        tokens["expires_in"] = seconds
    return tokens


def refresh_request(
    spec: dict[str, Any], base_url: str, refresh_token: str
) -> tuple[str, dict[str, Any]]:
    """The URL and ``httpx`` ``post`` keyword arguments of a service refresh."""
    return (
        base_url + spec["refresh_path"],
        {
            "json": {spec["refresh_request_field"]: refresh_token},
            "headers": {"Accept": "application/json"},
        },
    )


async def redeem_code(
    spec: dict[str, Any], integration: Any, code: str, *, redirect_uri: str
) -> dict[str, Any]:
    """Hand the authorization code to the service; its tokens in the RFC 6749 shape.

    Raises on a refused URL, a non-2xx answer or an answer without a token:
    the callback reports any of them as a failed exchange. No PKCE verifier is
    forwarded: the platform generates none for ``custom:`` providers. Adding
    PKCE to custom bundles needs a field naming it here, or the service
    answers ``invalid_grant``.
    """
    url = await service_base_url(integration) + spec["code_path"]
    await assert_token_endpoint_allowed(url)
    async with egress_client(timeout=30) as client:
        resp = await client.post(
            url,
            json={spec["code_field"]: code, spec["redirect_uri_field"]: redirect_uri},
            headers={"Accept": "application/json"},
        )
    resp.raise_for_status()
    return read_service_token_answer(spec, resp)
