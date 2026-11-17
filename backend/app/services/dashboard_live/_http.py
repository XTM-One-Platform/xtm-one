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

"""The one outbound request of the live data layer: bounded, JSON only, no redirects.

The destination is the integration's base URL plus a fixed path chosen by the
per-source module; the connection goes through the guarded transport (the
connected address is checked, metadata and link-local refused; private
networks allowed, as an integration may live on one), or the integration's
relay when it has one. The raw body is read as a stream and refused past
:data:`MAX_BODY_BYTES` - compression is refused too, so the bound is on what
crosses the wire.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from . import LiveError

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 2 * 1024 * 1024
TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def _client() -> httpx.AsyncClient:
    from app.core.egress import egress_client
    from app.services.web_fetch import guarded_client_options

    return egress_client(
        timeout=TIMEOUT, follow_redirects=False, trust_env=False, **guarded_client_options(allow_private=True)
    )


async def request_json(
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    json_body: Any = None,
    params: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    form: dict[str, str] | None = None,
    what: str = "platform",
) -> Any:
    """Send one request and return its JSON answer, or raise :class:`LiveError`.

    *json_body* is sent as JSON; *files* (and *form*) as a multipart upload -
    the publishing of a dashboard to its platform.
    """
    send_headers = {**headers, "Accept": "application/json", "Accept-Encoding": "identity"}
    if json_body is not None:
        send_headers["Content-Type"] = "application/json"
    body: dict[str, Any] = (
        {"files": files, "data": form}
        if files is not None
        else {"content": json.dumps(json_body).encode("utf-8") if json_body is not None else None}
    )
    try:
        async with _client() as client, client.stream(
            method,
            url,
            headers=send_headers,
            params=params,
            **body,
        ) as response:
            if response.status_code in (401, 403):
                logger.info("Dashboard live data: %s refused the viewer (%s)", what, response.status_code)
                raise LiveError("access_denied")
            if response.status_code != 200:
                logger.warning("Dashboard live data: %s answered HTTP %s", what, response.status_code)
                raise LiveError("upstream_error")
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type != "application/json":
                logger.warning("Dashboard live data: %s answered %r, not JSON", what, content_type[:60])
                raise LiveError("upstream_error")
            encoding = response.headers.get("content-encoding", "identity").strip().lower()
            if encoding not in ("", "identity"):
                logger.warning("Dashboard live data: %s compressed its answer (%r)", what, encoding[:30])
                raise LiveError("upstream_error")
            chunks: list[bytes] = []
            size = 0
            # Compression was refused above, so these are the bytes on the wire.
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    logger.warning("Dashboard live data: %s answered more than %s bytes", what, MAX_BODY_BYTES)
                    raise LiveError("upstream_error")
                chunks.append(chunk)
    except LiveError:
        raise
    except httpx.TimeoutException:
        raise LiveError("timeout") from None
    except httpx.HTTPError as exc:
        logger.warning("Dashboard live data: %s unreachable: %r", what, str(exc)[:200])
        raise LiveError("upstream_error") from None
    try:
        return json.loads(b"".join(chunks))
    except ValueError:
        logger.warning("Dashboard live data: %s answered unreadable JSON", what)
        raise LiveError("upstream_error") from None
