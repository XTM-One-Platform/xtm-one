# SPDX-License-Identifier: Apache-2.0

"""Shared Microsoft Graph pagination helpers for API routes."""

from __future__ import annotations

import logging
from typing import Any, Protocol


class _AsyncHttpGetClient(Protocol):
    """Minimal async HTTP client contract the paginator depends on.

    Captures just the ``get`` method :func:`paginate_graph_collection`
    calls, so production callers (``httpx.AsyncClient``) and the test
    fakes both satisfy it structurally — restoring editor / type-checker
    assistance without coupling this module to httpx (it only ever needs
    ``get``).
    """

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = ...,
        params: Any = ...,
    ) -> Any:
        """Perform an async HTTP GET and return the response object."""


def _extract_error_detail(resp: Any) -> str:
    """Best-effort human-readable detail from a Graph error response.

    Prefers the OData ``error.message`` field, falling back to the raw
    (truncated) body when the payload isn't valid JSON (e.g. an HTML
    body from a reverse proxy / gateway) or when ``error.message`` is
    absent / null.  Always returns a ``str`` so downstream string
    operations (logging, :func:`_is_top_not_allowed_error`) never have
    to deal with a non-string value.
    """
    try:
        message = resp.json().get("error", {}).get("message")
    except Exception:
        return resp.text[:300]
    if message is None:
        return resp.text[:300]
    return str(message)


def _is_top_not_allowed_error(detail: str | None) -> bool:
    """Return ``True`` when a Graph 400 indicates ``$top`` is unsupported.

    Some Graph endpoints (notably ``/teams/{id}/channels``) reject the
    ``$top`` OData query option with HTTP 400 ``Query option 'Top' is
    not allowed``.  Matching the message lets the helper
    transparently retry without ``$top`` instead of returning an empty
    collection and silently breaking the caller.

    Coerces ``detail`` to ``str`` before matching so a ``None`` (or any
    non-string value that slips through) can't raise on ``.lower()``.
    """
    return "'top' is not allowed" in str(detail or "").lower()


async def paginate_graph_collection(
    client: _AsyncHttpGetClient,
    *,
    initial_url: str,
    headers: dict[str, str],
    select_fields: str,
    top: int | None,
    extra_params: dict[str, str | int] | None = None,
    max_pages: int = 50,
    logger: logging.Logger,
    error_prefix: str,
    truncate_prefix: str,
) -> list[dict[str, Any]]:
    """Return ``value`` items from a Graph collection, following ``@odata.nextLink``.

    Defensive against the ``$top``-unsupported endpoints:
    if the first page is rejected with HTTP 400 ``Query option 'Top' is
    not allowed`` and we did send ``$top``, the request is retried once
    without ``$top`` so the listing succeeds instead of degrading to an
    empty result.  Callers that already know the endpoint rejects
    ``$top`` should still pass ``top=None`` to avoid the wasted first
    request — the retry is a safety net, not the primary path.
    """
    items: list[dict[str, Any]] = []
    next_link: str | None = initial_url
    first_page_params: dict[str, str | int] = {"$select": select_fields}
    if top is not None:
        first_page_params["$top"] = top
    if extra_params:
        first_page_params.update(extra_params)
    pages = 0

    while next_link and pages < max_pages:
        params = first_page_params if pages == 0 else None
        resp = await client.get(next_link, headers=headers, params=params)
        if resp.status_code >= 400:
            detail = _extract_error_detail(resp)
            # Self-heal the ``$top``-not-allowed 400 by retrying the
            # first page without ``$top`` — covers /teams/{id}/channels
            # and any other endpoint that rejects the option, regardless
            # of caller.
            if (
                pages == 0
                and resp.status_code == 400
                and params is not None
                and "$top" in params
                and _is_top_not_allowed_error(detail)
            ):
                logger.info(
                    "%s: endpoint rejected $top; retrying without it",
                    truncate_prefix,
                )
                # Rebuild as a fresh dict (the original first-page params
                # were already sent) so the retry omits $top.
                first_page_params = {
                    k: v for k, v in first_page_params.items() if k != "$top"
                }
                resp = await client.get(
                    next_link, headers=headers, params=first_page_params
                )
                if resp.status_code >= 400:
                    detail = _extract_error_detail(resp)
                    logger.warning(
                        "%s (HTTP %s): %s",
                        error_prefix,
                        resp.status_code,
                        detail,
                    )
                    return []
            else:
                logger.warning(
                    "%s (HTTP %s): %s", error_prefix, resp.status_code, detail
                )
                return []
        data = resp.json()
        items.extend(data.get("value", []))
        next_link = data.get("@odata.nextLink")
        pages += 1

    if next_link:
        logger.warning("%s truncated after %s pages", truncate_prefix, pages)

    return items
