# SPDX-License-Identifier: Apache-2.0

"""OAuth 2.0 device authorization grant (RFC 8628) for integrations.

The user signs in with a short code instead of a redirect: the platform asks
the device authorization endpoint for a ``device_code`` and a ``user_code``,
the user opens the verification URL (on any device) and enters the code, and
the platform polls the token endpoint until the user approves — or denies,
or the code expires. The browser that started the flow drives the polling
(it calls :func:`poll_device_authorization` at the interval the server asked
for), so no background job holds a pending sign-in.

The device code is the secret of the pending authorization: it stays on the
server, in Redis under ``oauth_device:{flow_id}`` for the code's lifetime,
bound to the user and the integration that started the flow. The browser
only sees the flow id and the user code. Requests go through
:func:`app.integrations.oauth_token_grants.post_with_client_auth`: the
destination guard, the client authentication fallback and the client
certificate apply as they do to every token request.
"""

from __future__ import annotations

import json
import logging
import secrets
import ssl
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from app.core.redis import redis_client
from app.integrations.oauth_token_grants import (
    TokenGrantError,
    check_bearer,
    describe_failure,
    post_with_client_auth,
    response_payload,
)
from app.services.openapi_import.security import DEVICE_CODE_GRANT

logger = logging.getLogger(__name__)

_KEY = "oauth_device:{}"
#: RFC 8628 §3.5: without ``interval``, poll every 5 seconds; ``slow_down``
#: adds 5 seconds.
_DEFAULT_INTERVAL = 5
_SLOW_DOWN_STEP = 5
_MAX_INTERVAL = 60
#: A code lives at most this long here, whatever the server says.
_MAX_LIFETIME = 30 * 60
_DEFAULT_LIFETIME = 10 * 60

STATUS_PENDING = "pending"
STATUS_CONNECTED = "connected"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class DeviceAuthorization:
    """What the user needs to sign in, as shown by the integration page."""

    flow_id: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DevicePollResult:
    status: str
    #: The token response, once the user approved.
    tokens: dict[str, Any] = field(default_factory=dict)
    interval: int | None = None
    message: str | None = None


def _int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if number > 0 else default


def _http_url(value: Any) -> str | None:
    if isinstance(value, str) and value.strip().lower().startswith(("https://", "http://")):
        return value.strip()
    return None


async def start_device_authorization(
    *,
    user_id: str,
    integration_id: str,
    device_authorization_url: str,
    token_url: str,
    client_id: str,
    client_secret: str,
    scopes: str | None,
    ssl_context: ssl.SSLContext | None = None,
) -> DeviceAuthorization:
    """Ask for a user code (RFC 8628 §3.1–3.2); raises :class:`TokenGrantError`.

    The token URL and the client id the flow starts with are bound to it: the
    device code is only ever redeemed there, by that client.
    """
    data: dict[str, str] = {}
    if (scopes or "").strip():
        data["scope"] = " ".join((scopes or "").split())
    resp, _method = await post_with_client_auth(
        device_authorization_url,
        data,
        client_id=client_id,
        client_secret=client_secret,
        ssl_context=ssl_context,
        endpoint="device authorization endpoint",
    )
    if not resp.is_success:
        raise TokenGrantError(describe_failure(resp, "device authorization endpoint"))
    payload = response_payload(resp)
    device_code = payload.get("device_code")
    user_code = payload.get("user_code")
    # "verification_url" is what some providers (Google) send.
    verification_uri = _http_url(payload.get("verification_uri")) or _http_url(
        payload.get("verification_url")
    )
    if not isinstance(device_code, str) or not device_code or not isinstance(user_code, str) or not user_code:
        raise TokenGrantError("the device authorization endpoint returned no device or user code")
    if verification_uri is None:
        raise TokenGrantError("the device authorization endpoint returned no verification URL")
    lifetime = min(_int(payload.get("expires_in"), _DEFAULT_LIFETIME), _MAX_LIFETIME)
    interval = min(_int(payload.get("interval"), _DEFAULT_INTERVAL), _MAX_INTERVAL)
    flow_id = secrets.token_urlsafe(24)
    state = {
        "user_id": user_id,
        "integration_id": integration_id,
        "device_code": device_code,
        "token_url": token_url,
        "client_id": client_id,
        "interval": interval,
        "next_poll_at": time.time() + interval,
    }
    redis = await redis_client()
    await redis.set(_KEY.format(flow_id), json.dumps(state), ex=lifetime)
    return DeviceAuthorization(
        flow_id=flow_id,
        user_code=user_code[:64],
        verification_uri=verification_uri,
        verification_uri_complete=_http_url(payload.get("verification_uri_complete")),
        expires_in=lifetime,
        interval=interval,
    )


async def poll_device_authorization(
    flow_id: str,
    *,
    user_id: str,
    integration_id: str,
    token_url: str,
    client_id: str,
    client_secret: str,
    ssl_context: ssl.SSLContext | None = None,
) -> DevicePollResult:
    """Ask the token endpoint once whether the user approved (RFC 8628 §3.4–3.5).

    A flow started by another user or for another integration reads as
    expired. So does one whose integration's token URL or client id changed
    since it started: the device code (and the client credentials) are only
    ever sent where the flow began. Polling faster than the server's interval
    answers ``pending`` without contacting it.
    """
    redis = await redis_client()
    key = _KEY.format(flow_id)
    raw = await redis.get(key)
    state = json.loads(raw) if raw else None
    if (
        not isinstance(state, dict)
        or state.get("user_id") != user_id
        or state.get("integration_id") != integration_id
    ):
        return DevicePollResult(STATUS_EXPIRED, message="The sign-in code expired. Start again.")
    if state.get("token_url") != token_url or state.get("client_id") != client_id:
        await redis.delete(key)
        return DevicePollResult(
            STATUS_EXPIRED,
            message="The integration's sign-in settings changed since this code was issued. Start again.",
        )
    interval = _int(state.get("interval"), _DEFAULT_INTERVAL)
    if time.time() < float(state.get("next_poll_at") or 0):
        return DevicePollResult(STATUS_PENDING, interval=interval)

    try:
        resp, _method = await post_with_client_auth(
            token_url,
            {"grant_type": DEVICE_CODE_GRANT, "device_code": str(state.get("device_code") or "")},
            client_id=client_id,
            client_secret=client_secret,
            ssl_context=ssl_context,
        )
    except TokenGrantError as exc:
        return await _keep_polling(redis, key, state, interval, message=str(exc))

    payload = response_payload(resp)
    if resp.is_success:
        await redis.delete(key)
        token = payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            return DevicePollResult(STATUS_FAILED, message="The token endpoint returned no access token.")
        try:
            check_bearer(payload.get("token_type"))
        except TokenGrantError as exc:
            return DevicePollResult(STATUS_FAILED, message=f"{str(exc)[:1].upper()}{str(exc)[1:]}.")
        return DevicePollResult(STATUS_CONNECTED, tokens=payload)

    error = payload.get("error")
    if error == "authorization_pending":
        return await _keep_polling(redis, key, state, interval)
    if error == "slow_down":
        return await _keep_polling(redis, key, state, min(interval + _SLOW_DOWN_STEP, _MAX_INTERVAL))
    await redis.delete(key)
    if error == "access_denied":
        return DevicePollResult(STATUS_DENIED, message="The sign-in was declined.")
    if error == "expired_token":
        return DevicePollResult(STATUS_EXPIRED, message="The sign-in code expired. Start again.")
    detail = describe_failure(resp)
    return DevicePollResult(STATUS_FAILED, message=f"{detail[:1].upper()}{detail[1:]}.")


async def _keep_polling(
    redis: Any, key: str, state: dict[str, Any], interval: int, *, message: str | None = None
) -> DevicePollResult:
    state = {**state, "interval": interval, "next_poll_at": time.time() + interval}
    ttl = await redis.ttl(key)
    if isinstance(ttl, int) and ttl > 0:
        await redis.set(key, json.dumps(state), ex=ttl)
    return DevicePollResult(STATUS_PENDING, interval=interval, message=message)


async def cancel_device_authorization(flow_id: str, *, user_id: str) -> None:
    """Forget a pending flow (the user closed the dialog)."""
    redis = await redis_client()
    key = _KEY.format(flow_id)
    raw = await redis.get(key)
    state = json.loads(raw) if raw else None
    if isinstance(state, dict) and state.get("user_id") == user_id:
        await redis.delete(key)
