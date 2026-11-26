# SPDX-License-Identifier: Apache-2.0

"""The platform LM Studio LLM provider, fed from an LM Studio integration.

A company-managed LM Studio integration with a server URL can create the platform's LM Studio LLM provider (``app/llm/lmstudio_provider.py``)
or adopt one it fed before; its Models tab picks the models offered
(``services/integration_llm_provider.py`` is the linking mechanism).

Unlike a provider holding an API key, the config holds no credential: the
integration stays the one place where the server's URL, its optional API token
and its relay group live. Every request the provider sends goes through
:class:`IntegrationTransport`, which reads them through the provider's
config - the integration it is fed by now (:func:`resolve_target`, kept
:data:`TARGET_TTL_SECONDS` per process and dropped on every edit of the
integration and every unlink) - and sends the request on the integration's
route: through its relay group when it has one, directly otherwise. A
provider unlinked, or whose integration is gone, personal or without a
server URL, refuses every request with a 424 the SDK does not retry.

A relay logs these requests under the integration (``source_type``
``integration``), like its console's.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from app.core.egress import EgressRoute, egress_client
from app.integrations.lifecycle import IntegrationUpdate, register_lifecycle
from app.integrations.providers import lmstudio as client
from app.models.integration import Integration
from app.services import integration_llm_provider as feeds

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

PROVIDER = "lmstudio"

#: What the config stores as its key: the provider reads the integration, never this.
STORED_CREDENTIAL = "integration"

#: The host the provider's OpenAI client is built on. The transport replaces it
#: with the integration's server URL; ``.invalid`` never resolves, so a request
#: that escaped the transport could not reach anything.
PLACEHOLDER_ROOT = "http://lmstudio.integration.invalid"

#: How long a process reuses what it read of an integration. An edit drops it
#: at once in the process that made it; other processes follow within this.
TARGET_TTL_SECONDS = 30.0

#: The status a refused request answers with: a 4xx the OpenAI SDK and
#: ``is_retryable_openai`` never retry, read as the provider's error.
REFUSED_STATUS = 424


def _refusal(integration: Integration) -> str | None:
    try:
        from app.core.security import decrypt_json

        extra = decrypt_json(integration.extra_config_enc) if integration.extra_config_enc else None
        client.normalize_base_url(extra.get(client.BASE_URL_FIELD) if isinstance(extra, dict) else None)
    except client.LMStudioError as exc:
        return exc.public_message
    except Exception:
        return client.PUBLIC_MESSAGES["no_base_url"]
    return None


def _stored_credential(integration: Integration) -> str:
    return STORED_CREDENTIAL


FEED = feeds.IntegrationFeed(
    integration_provider=PROVIDER,
    llm_provider=PROVIDER,
    label="LM Studio",
    refusal=_refusal,
    stored_credential=_stored_credential,
)


def link_refusal(integration: Integration) -> str | None:
    """Why ``integration`` may not feed the platform LM Studio provider, or ``None``."""
    return feeds.link_refusal(FEED, integration)


# ---------------------------------------------------------------------------
# Where a request goes
# ---------------------------------------------------------------------------


class TargetRefusedError(Exception):
    """The provider's integration cannot carry a request; ``message`` says why."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Target:
    integration_id: uuid.UUID
    #: The integration's relay route; ``None``: the server is called directly.
    route: EgressRoute | None
    connection: client.Connection


#: Per provider config: when it was read, and where its requests go.
_targets: dict[uuid.UUID, tuple[float, Target]] = {}

_UNLINKED = (
    "This LM Studio provider is not fed by an LM Studio integration any more. Create it again "
    "from the integration's Models tab."
)


def forget_target(integration_id: Any = None, *, config_id: Any = None) -> None:
    """Drop what this process read of ``integration_id`` or of ``config_id`` (everything when neither)."""
    if integration_id is None and config_id is None:
        _targets.clear()
        return
    if config_id is not None:
        _targets.pop(config_id, None)
    if integration_id is not None:
        for key, (_, target) in list(_targets.items()):
            if target.integration_id == integration_id:
                _targets.pop(key, None)


async def _read_target(db: AsyncSession, config_id: uuid.UUID) -> Target:
    from app.models.llm_config import LLMProviderConfig
    from app.services.relays import integration_egress_route

    config = await db.get(LLMProviderConfig, config_id)
    if config is None or config.provider != PROVIDER or config.source_integration_id is None:
        raise TargetRefusedError(_UNLINKED)
    integration = await db.get(Integration, config.source_integration_id)
    if integration is None or integration.provider != PROVIDER:
        raise TargetRefusedError(_UNLINKED)
    refusal = link_refusal(integration)
    if refusal is not None:
        raise TargetRefusedError(refusal)
    route = await integration_egress_route(db, integration)
    try:
        connection = await client.integration_connection(integration)
    except client.LMStudioError as exc:
        raise TargetRefusedError(exc.public_message) from exc
    return Target(integration_id=integration.id, route=route, connection=connection)


async def resolve_target(config_id: uuid.UUID | None) -> Target:
    """Where a request of the provider config ``config_id`` goes; raises :class:`TargetRefusedError`."""
    if config_id is None:
        raise TargetRefusedError(_UNLINKED)
    cached = _targets.get(config_id)
    if cached is not None and time.monotonic() - cached[0] < TARGET_TTL_SECONDS:
        return cached[1]
    # No lock: two requests racing past an expired entry read the rows twice.
    from app.core.database import async_session

    async with async_session() as db:
        target = await _read_target(db, config_id)
    _targets[config_id] = (time.monotonic(), target)
    return target


def _rewrite(url: httpx.URL, base_url: str) -> httpx.URL:
    """``url`` (on the placeholder host) on the server: its path under the server's root."""
    base = httpx.URL(base_url)
    # Raw: an escaped character of the path (``%2F``) and the query stay as sent.
    return base.copy_with(raw_path=base.raw_path.split(b"?", 1)[0].rstrip(b"/") + url.raw_path)


_PLACEHOLDER_HOST = httpx.URL(PLACEHOLDER_ROOT).host
#: Headers the transport sets itself: the server's host, and its token.
_REPLACED_HEADERS = frozenset({"host", "authorization"})


class _ClosingStream(httpx.AsyncByteStream):
    """The body of a direct answer; closing it closes the one-request client that read it."""

    def __init__(self, response: httpx.Response, owner: httpx.AsyncClient) -> None:
        self._response = response
        self._owner = owner

    async def __aiter__(self):  # type: ignore[override]
        # The raw stream, still encoded: the provider's client decodes it.
        stream = self._response.stream
        if not isinstance(stream, httpx.AsyncByteStream):
            raise TypeError("a direct LM Studio answer is read asynchronously")
        async for chunk in stream:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self._response.aclose()
        finally:
            await self._owner.aclose()


async def _send_direct(request: httpx.Request) -> httpx.Response:
    """Send ``request`` from the platform, as any direct call of an integration.

    A client per request, closed with the answer's body: no pool outlives the
    event loop that opened it, and the environment's proxies (``NO_PROXY``
    included) apply as to every other direct call.
    """
    direct = egress_client(route=None, follow_redirects=False)
    try:
        response = await direct.send(request, stream=True)
    except BaseException:
        await direct.aclose()
        raise
    return httpx.Response(
        response.status_code,
        headers=response.headers.raw,
        stream=_ClosingStream(response, direct),
        extensions=response.extensions,
        request=request,
    )


class IntegrationTransport(httpx.AsyncBaseTransport):
    """Sends the provider's requests to its integration's server, on the integration's route."""

    def __init__(self, config_id: uuid.UUID | None) -> None:
        self.config_id = config_id

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != _PLACEHOLDER_HOST:
            # Only the provider's own client sends here; anything else is a bug.
            return _refused(request, "This request was not addressed to the LM Studio provider.")
        try:
            target = await resolve_target(self.config_id)
        except TargetRefusedError as exc:
            return _refused(request, exc.message)
        headers = [(k, v) for k, v in request.headers.multi_items() if k.lower() not in _REPLACED_HEADERS]
        if target.connection.token:
            headers.append(("Authorization", f"Bearer {target.connection.token}"))
        routed = httpx.Request(
            request.method,
            _rewrite(request.url, target.connection.base_url),
            headers=headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        if target.route is None:
            return await _send_direct(routed)
        return await target.route.transport().handle_async_request(routed)


def _refused(request: httpx.Request, message: str) -> httpx.Response:
    # A stream, like any answer a transport gives: the caller may read it raw.
    body = json.dumps({"error": {"message": message, "type": "lmstudio_unavailable"}}).encode()
    return httpx.Response(
        REFUSED_STATUS,
        stream=httpx.ByteStream(body),
        headers={"content-type": "application/json"},
        request=request,
    )


def integration_http_client(config_id: uuid.UUID | None, *, timeout: httpx.Timeout) -> httpx.AsyncClient:
    """The HTTP client of the provider config ``config_id`` (``None``: one not saved, which refuses everything).

    Its one transport sends every request on the integration's route
    (``route=None`` keeps the ambient egress route out of it, and
    ``trust_env=False`` keeps an environment proxy from taking the request,
    still addressed to the placeholder host, before the transport sees it).
    """
    return egress_client(
        route=None,
        transport=IntegrationTransport(config_id),
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def _on_integration_update(
    db: AsyncSession, integration: Integration, update: IntegrationUpdate
) -> None:
    """Follow an edit of the integration: what the provider reads, and who may feed it.

    The server URL, the token and the relay group are read again on the next
    request; an integration that is no longer company-managed stops feeding
    its configs (whoever may now edit it would decide where the platform's
    requests go).
    """
    integration_id = integration.id
    forget_target(integration_id)

    async def _forget_committed() -> None:
        # Read before the commit, the old row could be cached again.
        forget_target(integration_id)

    update.after_commit.add(_forget_committed)
    configs = await feeds.linked_configs(db, integration_id)
    if not configs:
        return
    if not integration.is_company_managed:
        for config in configs:
            config.source_integration_id = None
        logger.warning(
            "LM Studio integration %s is no longer company-managed: unlinked %d LLM provider config(s)",
            integration.id,
            len(configs),
        )
        update.after_commit.add(feeds.drop_provider_cache)


async def _on_integration_removed(db: AsyncSession, integration: Integration) -> None:
    """The integration is deleted: its configs are unlinked by the database (``SET NULL``)."""
    forget_target(integration.id)


register_lifecycle(PROVIDER, on_update=_on_integration_update, on_disconnect=_on_integration_removed)
