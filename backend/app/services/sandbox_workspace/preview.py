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

"""Preview URLs of started sandbox services: which URLs the browser tool and web_fetch may open.

``sandbox_start_service`` answers the URL the OpenSandbox server gives for a
service's port: with ``OPENSANDBOX_USE_SERVER_PROXY`` its own proxy route,
``<protocol>://<OPENSANDBOX_DOMAIN>/sandboxes/<id>/proxy/<port>/``. That server
sits on the operator's network, which the outbound URL policy refuses, and
its proxy asks for the server's API key on every request.

A preview is a capability: :func:`register` records the ``(sandbox id, port)``
pair a start produced, for the service's lifetime (Redis, renewed by status and
logs, removed by stop); :func:`allowed` admits a URL only when it is on the
configured server's origin, under the proxy route of a registered pair, with
no credential in it and no encoded dot or slash in its path. The browser tool
and web_fetch then send it with :func:`request_headers` added by the platform:
no agent ever sees or types the key, no other path of the server (its
lifecycle API, another sandbox, another port of the same sandbox) is reachable,
and every other URL is held to the outbound policy as before. Fails closed: no
Redis, no preview.

An agentic coding lab (``labs.py``, ``AGENTIC_CODING``) is a pod of its own:
its preview is ``http://<pod IP>:<port>/``, admitted by
:func:`register_origin` the same way, for that IP literal and port only (a
private unicast address; loopback, link-local and metadata addresses never),
and sent with nothing added: the server's key goes to the server's proxy only.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

if TYPE_CHECKING:
    from app.services.web_fetch import FetchResult

logger = logging.getLogger(__name__)

#: Redirect hops :func:`fetch` follows, each one held to :func:`allowed`.
MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}

_KEY_PREFIX = "sandbox_preview:"
_ORIGIN_KEY_PREFIX = "sandbox_preview_origin:"
_PROXY_PATH_RE = re.compile(r"^/sandboxes/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/proxy/([0-9]{1,5})(?:/|$)")
_ENCODED_TRAVERSAL_RE = re.compile(r"%(2e|2f|5c)", re.IGNORECASE)
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class PreviewTarget:
    sandbox_id: str
    port: int


@dataclass(frozen=True)
class OriginTarget:
    """A lab pod's service: an IP literal and a port."""

    host: str
    port: int


#: Seconds a positive answer of :func:`allowed` is kept in the process: a page
#: loads tens of modules from one preview, one Redis read covers them.
_ALLOWED_CACHE_SECONDS = 5.0
_allowed_cache: dict[PreviewTarget | OriginTarget, float] = {}


def path_of(sandbox_id: str, port: int) -> str:
    """The server proxy's path for *port* of *sandbox_id*, the prefix a service is served under."""
    return f"/sandboxes/{sandbox_id}/proxy/{port}"


def _server_origin() -> tuple[str, str, int] | None:
    from app.core.config import settings

    domain = (getattr(settings, "opensandbox_domain", "") or "").strip().rstrip("/")
    protocol = (getattr(settings, "opensandbox_protocol", "") or "http").strip().lower()
    if not domain or protocol not in _DEFAULT_PORTS:
        return None
    parsed = urlparse(f"{protocol}://{domain}")
    if not parsed.hostname:
        return None
    return protocol, parsed.hostname.lower(), parsed.port or _DEFAULT_PORTS[protocol]


def match(url: str) -> PreviewTarget | None:
    """The ``(sandbox id, port)`` *url* names on the platform's OpenSandbox proxy, or ``None``.

    Pure: whether the pair is a live preview is :func:`allowed`'s question.
    """
    origin = _server_origin()
    if origin is None:
        return None
    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        return None
    if (scheme, host, port) != origin or parsed.username is not None or parsed.password is not None:
        return None
    path = parsed.path or ""
    if _ENCODED_TRAVERSAL_RE.search(path) or "/../" in f"{path}/" or "\\" in path:
        return None
    found = _PROXY_PATH_RE.match(path)
    if found is None:
        return None
    target_port = int(found.group(2))
    if not 1 <= target_port <= 65535:
        return None
    return PreviewTarget(sandbox_id=found.group(1), port=target_port)


def match_origin(url: str) -> OriginTarget | None:
    """The lab service *url* names (``http://<private IP>:<port>/...``), or ``None``. Pure, like :func:`match`."""
    try:
        parsed = urlparse(url)
        port = parsed.port
        host = ipaddress.ip_address((parsed.hostname or "").strip("[]"))
    except ValueError:
        return None
    if (parsed.scheme or "").lower() != "http" or port is None or parsed.username is not None or parsed.password is not None:
        return None
    if not host.is_private or host.is_loopback or host.is_link_local or host.is_multicast or host.is_unspecified:
        return None
    path = parsed.path or ""
    if _ENCODED_TRAVERSAL_RE.search(path) or "/../" in f"{path}/" or "\\" in path:
        return None
    return OriginTarget(host=host.compressed, port=port)


def _key(target: PreviewTarget | OriginTarget) -> str:
    if isinstance(target, OriginTarget):
        return f"{_ORIGIN_KEY_PREFIX}{target.host}:{target.port}"
    return f"{_KEY_PREFIX}{target.sandbox_id}:{target.port}"


async def register(sandbox_id: str, port: int, ttl_seconds: int) -> None:
    """Admit the preview of *port* of *sandbox_id* for *ttl_seconds* (renewed by calling again)."""
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        await client.set(_key(PreviewTarget(sandbox_id, port)), "1", ex=max(60, int(ttl_seconds)))
    except Exception as exc:
        logger.warning("Sandbox preview %s:%s not registered: %s", sandbox_id, port, exc)


async def unregister(sandbox_id: str, port: int) -> None:
    try:
        from app.core.redis import redis_client

        _allowed_cache.pop(PreviewTarget(sandbox_id, port), None)
        client = await redis_client()
        await client.delete(_key(PreviewTarget(sandbox_id, port)))
    except Exception as exc:
        logger.info("Sandbox preview %s:%s not unregistered: %s", sandbox_id, port, exc)


# The value of a lab admission written before admissions named their pod: it names no owner, so the
# caller withdrawing it (which holds the lab's record for this ip:port) may remove it.
LEGACY_ORIGIN_OWNER = "1"

# Delete the admission only while it still names the pod withdrawing it, or the legacy value that
# names nobody (one round trip, atomic in Redis).
_DELETE_IF_OWNER = (
    "local v = redis.call('get', KEYS[1]) "
    "if v == ARGV[1] or v == ARGV[2] then return redis.call('del', KEYS[1]) else return 0 end"
)


async def register_origin(host: str, port: int, ttl_seconds: int, *, owner: str) -> None:
    """Admit the lab service at ``http://host:port/`` for *ttl_seconds* (renewed by calling again).

    The admission names its *owner* (the lab pod): an IP the cluster gives to
    another pod later is then withdrawn by that pod only, never by the old one.
    """
    try:
        from app.core.redis import redis_client

        target = OriginTarget(ipaddress.ip_address(host).compressed, int(port))
        client = await redis_client()
        await client.set(_key(target), owner, ex=max(60, int(ttl_seconds)))
    except Exception as exc:
        logger.warning("Lab preview %s:%s not registered: %s", host, port, exc)


async def unregister_origin(host: str, port: int, *, owner: str) -> None:
    """Withdraw the admission of ``http://host:port/`` when *owner* still holds it.

    An admission written before admissions named their pod (value ``LEGACY_ORIGIN_OWNER``) is
    withdrawn too, so a lab started before the upgrade does not stay admitted until its TTL.
    """
    try:
        from app.core.redis import redis_client

        target = OriginTarget(ipaddress.ip_address(host).compressed, int(port))
        _allowed_cache.pop(target, None)
        client = await redis_client()
        await client.eval(_DELETE_IF_OWNER, 1, _key(target), owner, LEGACY_ORIGIN_OWNER)
    except Exception as exc:
        logger.info("Lab preview %s:%s not unregistered: %s", host, port, exc)


#: What :func:`admission` answers of a URL.
ADMITTED = "admitted"
NOT_ADMITTED = "not admitted"
UNREADABLE = "unreadable"
NOT_A_PREVIEW = "not a preview"


async def admission(url: str) -> str:
    """Whether *url* is a registered preview, telling apart why it is not.

    :data:`NOT_A_PREVIEW` for a URL no preview shape matches, :data:`ADMITTED` for a
    live registration, :data:`NOT_ADMITTED` when the registration is gone (the
    service stopped, or a lab restarted on another address), :data:`UNREADABLE`
    when Redis could not be read. Only :data:`ADMITTED` admits the URL.
    """
    target: PreviewTarget | OriginTarget | None = match(url) or match_origin(url)
    if target is None:
        return NOT_A_PREVIEW
    now = time.monotonic()
    if _allowed_cache.get(target, 0.0) > now:
        return ADMITTED
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        live = bool(await client.exists(_key(target)))
    except Exception:
        return UNREADABLE
    if live:
        if len(_allowed_cache) > 1024:
            _allowed_cache.clear()
        _allowed_cache[target] = now + _ALLOWED_CACHE_SECONDS
    else:
        _allowed_cache.pop(target, None)
    return ADMITTED if live else NOT_ADMITTED


async def allowed(url: str) -> bool:
    """Whether *url* is a registered preview: the OpenSandbox proxy's, or a lab pod's (fails closed)."""
    return await admission(url) == ADMITTED


def request_headers(url: str | None = None) -> dict[str, str]:
    """What the platform adds to a preview request: the server's API key, never shown to an agent.

    With *url*, only to a URL on the server's proxy (:func:`match`): a lab pod's
    preview gets nothing.
    """
    from app.core.config import settings

    if url is not None and match(url) is None:
        return {}
    key = (getattr(settings, "opensandbox_api_key", "") or "").strip()
    return {"OPEN-SANDBOX-API-KEY": key} if key else {}


async def fetch(url: str, *, max_chars: int, timeout: float = 30.0, max_bytes: int = 20 * 1024 * 1024) -> FetchResult:
    """``web_fetch`` for a preview: the page read with the server's key, every redirect held to :func:`allowed`.

    Raises ``ValueError`` when a hop leaves the registered previews, ``httpx.HTTPError`` as
    :func:`app.services.web_fetch.fetch_and_extract` does.
    """
    import httpx

    from app.core.parse_offload import run_parse
    from app.services.web_fetch import USER_AGENT, FetchResult, extract_from_html, is_html_media_type

    async with httpx.AsyncClient(
        follow_redirects=False, trust_env=False, timeout=timeout, headers={"User-Agent": USER_AGENT}
    ) as client:
        target = url
        for _hop in range(MAX_REDIRECTS + 1):
            if not await allowed(target):
                raise ValueError(f"{target} is not the preview of a running sandbox service")
            async with client.stream("GET", target, headers=request_headers(target)) as resp:
                if resp.status_code in _REDIRECT_STATUSES and "location" in resp.headers:
                    target = urljoin(str(resp.url), resp.headers["location"])
                    continue
                resp.raise_for_status()
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) >= max_bytes:
                        break
                content_type = resp.headers.get("content-type", "")
                text = bytes(body).decode(resp.encoding or "utf-8", errors="replace")
                final = str(resp.url)
                status = resp.status_code
                kept = {name.lower(): value for name, value in resp.headers.items()}
            break
        else:
            raise ValueError(f"Too many redirects (more than {MAX_REDIRECTS}) while fetching: {url}")
    title = None
    if is_html_media_type(content_type):
        text, title = await run_parse(lambda: extract_from_html(text, url=final))
    truncated = len(text) > max_chars
    text = text[:max_chars]
    return FetchResult(
        url=final,
        title=title,
        text=text,
        content_type=content_type,
        status_code=status,
        char_count=len(text),
        truncated=truncated,
        body=bytes(body),
        headers=kept,
        body_capped=len(body) >= max_bytes,
    )
