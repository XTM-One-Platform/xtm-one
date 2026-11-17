# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""What the Microsoft agent-account providers share: Microsoft Graph, as the account.

Teams and Outlook reach the platform the same way - one delegated Graph
token, the same error statuses, the same change-notification subscriptions
and the same directory - and differ only in the resources they read and post
to. Everything Graph-wide lives here once; each provider module keeps its own
resources.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from app.core.egress import egress_route
from app.integrations.providers.graph_path import _validate_path_segment
from app.services.agent_accounts.events import AccountIdentity, PersonOption
from app.services.agent_accounts.providers import (
    AccountProvider,
    CreatedSubscription,
    ExistingSubscription,
    InboundNotification,
    ProviderError,
    ProviderSession,
)

logger = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
#: Subscriptions read back when looking for ones a sync created but never recorded.
MAX_LISTED_SUBSCRIPTIONS = 500
_CACHE_TTL_SECONDS = 600
_CACHE_MAX = 2000


class _TTLCache:
    """A small in-process cache of names the platform rarely changes."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._data.get(key)
        if hit is None or hit[0] < time.monotonic():
            return None
        return hit[1]

    def put(self, key: str, value: Any) -> None:
        if len(self._data) >= _CACHE_MAX:
            for stale in sorted(self._data, key=lambda k: self._data[k][0])[: _CACHE_MAX // 4]:
                self._data.pop(stale, None)
        self._data[key] = (time.monotonic() + _CACHE_TTL_SECONDS, value)


def _segment(value: str, label: str) -> str:
    if error := _validate_path_segment(value, label):
        raise ProviderError(error.removeprefix("Error: "))
    return value


def _graph_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _parse_time(value: Any) -> datetime:
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


#: What a Graph status says can be done (``providers.ERROR_KINDS``); a 5xx is ``unavailable``.
_KIND_BY_STATUS = {401: "reauthorize", 403: "refused", 404: "not_found", 410: "not_found", 429: "throttled"}


def _graph_error(resp: Any) -> ProviderError:
    try:
        error = resp.json().get("error") or {}
        message = error.get("message") or error.get("code")
    except Exception:
        message = None
    status = resp.status_code
    kind = _KIND_BY_STATUS.get(status) or ("unavailable" if status >= 500 else "error")
    return ProviderError(f"Microsoft Graph answered {status}" + (f": {message}" if message else ""), kind=kind)


class MicrosoftGraphProvider(AccountProvider):
    """An account reached through its delegated Microsoft Graph token."""

    integration_provider = "microsoft"
    #: Headers the subscriptions are created with (an id format their notifications name).
    subscription_headers: ClassVar[dict[str, str]] = {}

    # -- HTTP ---------------------------------------------------------------

    async def _call(
        self,
        session: ProviderSession,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
        allow_missing: bool = False,
    ) -> dict | None:
        """One Graph call as the account; ``None`` for a 404 when *allow_missing*."""
        kwargs: dict[str, Any] = {}
        if params:
            kwargs["params"] = params
        if payload is not None:
            kwargs["json"] = payload
        if headers:
            kwargs["headers"] = {**session.headers, **headers}
        try:
            with egress_route(session.route):
                async with session.client() as client:
                    resp = await client.request(method, url, **kwargs)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Microsoft Graph could not be reached: {type(exc).__name__}", kind="unavailable"
            ) from None
        if resp.status_code == 404 and allow_missing:
            return None
        if resp.status_code >= 400:
            raise _graph_error(resp)
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError("Microsoft Graph answered with a body that is not JSON.") from None
        return data if isinstance(data, dict) else {}

    async def _collect(
        self,
        session: ProviderSession,
        url: str,
        params: dict[str, str] | None,
        limit: int,
        *,
        headers: dict[str, str] | None = None,
    ) -> list[dict]:
        """A Graph collection across ``@odata.nextLink`` pages, at most *limit* items."""
        items: list[dict] = []
        next_url: str | None = url
        next_params = params
        while next_url and len(items) < limit:
            page = await self._call(session, "GET", next_url, params=next_params, headers=headers) or {}
            items.extend(v for v in page.get("value") or [] if isinstance(v, dict))
            link = page.get("@odata.nextLink")
            # Only Graph's own paging link: the token goes nowhere else.
            next_url = link if isinstance(link, str) and link.startswith(f"{GRAPH}/") else None
            next_params = None
        return items[:limit]

    # -- identity -----------------------------------------------------------

    async def identify(self, session: ProviderSession) -> AccountIdentity:
        me = await self._call(
            session, "GET", f"{GRAPH}/me", params={"$select": "id,displayName,mail,userPrincipalName"}
        ) or {}
        user_id = str(me.get("id") or "")
        if not user_id:
            raise ProviderError("Microsoft Graph did not say who the integration is signed in as.")
        return AccountIdentity(
            external_id=user_id,
            display_name=str(me.get("displayName") or me.get("userPrincipalName") or user_id),
            email=me.get("mail") or me.get("userPrincipalName"),
        )

    # -- push ---------------------------------------------------------------

    async def create_subscription(
        self,
        session: ProviderSession,
        resource: str,
        *,
        notification_url: str,
        lifecycle_url: str,
        client_state: str,
        change_type: str = "created",
    ) -> CreatedSubscription:
        if not notification_url.startswith("https://"):
            raise ProviderError("Microsoft Graph only notifies an HTTPS address; the platform's BASE_URL is not one.")
        expires = datetime.now(UTC) + timedelta(minutes=self.subscription_lifetime_minutes)
        created = await self._call(
            session,
            "POST",
            f"{GRAPH}/subscriptions",
            payload={
                "changeType": change_type,
                "notificationUrl": notification_url,
                # Graph refuses a subscription longer than an hour without it.
                "lifecycleNotificationUrl": lifecycle_url,
                "resource": resource,
                "includeResourceData": False,
                "expirationDateTime": _graph_time(expires),
                "clientState": client_state,
            },
            headers=dict(self.subscription_headers) or None,
        ) or {}
        external_id = created.get("id")
        if not isinstance(external_id, str) or not external_id:
            raise ProviderError("Microsoft Graph created no subscription.")
        return CreatedSubscription(external_id=external_id, expires_at=_parse_time(created.get("expirationDateTime")))

    async def renew_subscription(self, session: ProviderSession, external_id: str) -> datetime | None:
        expires = datetime.now(UTC) + timedelta(minutes=self.subscription_lifetime_minutes)
        renewed = await self._call(
            session,
            "PATCH",
            f"{GRAPH}/subscriptions/{_segment(external_id, 'subscription id')}",
            payload={"expirationDateTime": _graph_time(expires)},
            allow_missing=True,
        )
        if renewed is None:
            return None
        return _parse_time(renewed.get("expirationDateTime")) if renewed else expires

    async def delete_subscription(self, session: ProviderSession, external_id: str) -> bool:
        try:
            await self._call(
                session,
                "DELETE",
                f"{GRAPH}/subscriptions/{_segment(external_id, 'subscription id')}",
                allow_missing=True,
            )
            return True
        except ProviderError as exc:
            logger.warning("Could not delete Graph subscription %s: %s", external_id, exc)
            return False

    async def list_subscriptions(self, session: ProviderSession) -> list[ExistingSubscription]:
        items = await self._collect(session, f"{GRAPH}/subscriptions", None, MAX_LISTED_SUBSCRIPTIONS)
        return [
            ExistingSubscription(
                external_id=str(item["id"]),
                resource=str(item.get("resource") or ""),
                notification_url=str(item.get("notificationUrl") or ""),
            )
            for item in items
            if isinstance(item.get("id"), str) and item["id"]
        ]

    def handshake(self, query: dict[str, str]) -> str | None:
        token = query.get("validationToken")
        return token if isinstance(token, str) and token else None

    def parse_notifications(self, body: bytes) -> list[InboundNotification]:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        entries = data.get("value")
        if not isinstance(entries, list):
            raise ValueError("no value list")
        out: list[InboundNotification] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("an entry is not an object")
            sub_id = entry.get("subscriptionId")
            state = entry.get("clientState")
            if not isinstance(sub_id, str) or not isinstance(state, str):
                continue
            lifecycle = entry.get("lifecycleEvent")
            out.append(
                InboundNotification(
                    subscription_id=sub_id,
                    client_state=state,
                    lifecycle_event=lifecycle if isinstance(lifecycle, str) else None,
                    payload={"resource": entry.get("resource"), "changeType": entry.get("changeType")},
                )
            )
        return out

    # -- editor -------------------------------------------------------------

    async def search_people(self, session: ProviderSession, query: str) -> list[PersonOption]:
        needle = re.sub(r"[\"\\]", "", query or "").strip()[:100]
        if len(needle) < 2:
            return []
        users = await self._call(
            session,
            "GET",
            f"{GRAPH}/users",
            params={
                "$search": f'"displayName:{needle}" OR "mail:{needle}"',
                "$select": "id,displayName,mail,userPrincipalName",
                "$top": "20",
            },
            headers={"ConsistencyLevel": "eventual"},
        ) or {}
        return [
            PersonOption(
                id=str(u.get("id")),
                name=str(u.get("displayName") or u.get("mail") or u.get("id")),
                email=u.get("mail") or u.get("userPrincipalName"),
            )
            for u in users.get("value") or []
            if isinstance(u, dict) and u.get("id")
        ]
