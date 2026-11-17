# SPDX-License-Identifier: Apache-2.0

"""What a media provider's failed call says: its HTTP status, its own message, the wait it asked for.

Read off the errors the providers raise - OpenAI-shaped SDK errors
(``status_code``, ``body``, ``response.headers``), Google GenAI errors
(``code``, ``details`` with its ``google.rpc.RetryInfo``) and
``video.VideoAPIError`` (``code``, ``retry_after``) - so a video, an image or
a track refused by its provider is worded and asked again the same way; and
whether it says a quota ran out (:func:`quota_exceeded`).
"""

from __future__ import annotations

import re
from typing import Any

from app.core.utils import format_exception_message

#: Google's ``RetryInfo.retryDelay``: a duration in seconds, ``"23s"`` or ``"1.5s"``.
_GOOGLE_DELAY = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


def error_body(exc: BaseException) -> Any:
    """The JSON an API error carries: ``body`` on OpenAI-shaped errors, ``details`` on Google's."""
    body = getattr(exc, "body", None)
    return body if body is not None else getattr(exc, "details", None)


def api_error_message(exc: BaseException) -> str:
    """The message an API error carries in its body, else its text, short enough to show."""
    body = error_body(exc)
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:400]
        if isinstance(error, str) and error.strip():
            return error[:400]
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message.strip():
        return message[:400]
    return format_exception_message(exc)[:400]


def error_status(exc: BaseException) -> int | None:
    """The HTTP status a provider answered the failed call with; ``None`` when it did not answer."""
    for status in (
        getattr(exc, "status_code", None),
        getattr(exc, "code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            return status
    return None


def is_transient(code: int | None) -> bool:
    """A failure worth asking again: no HTTP answer at all, a rate limit, a server error."""
    return code is None or code == 429 or code >= 500


#: The ``google.rpc.Code`` values of a call refused for the service's own state, by number and by name, with the
#: HTTP status Google maps each to: ``DEADLINE_EXCEEDED`` 504, ``RESOURCE_EXHAUSTED`` 429, ``UNAVAILABLE`` 503.
_RPC_BUSY = {4: 504, 8: 429, 14: 503, "DEADLINE_EXCEEDED": 504, "RESOURCE_EXHAUSTED": 429, "UNAVAILABLE": 503}


def rpc_busy_status(status: Any) -> int | None:
    """The HTTP status of a ``google.rpc.Status`` (a long-running operation's ``error``) that says the service could not
    take the work now - its load, its rate limit, a deadline it ran out of - or ``None`` for any other failure. Read
    from its ``code`` (or the canonical ``status`` name a REST body carries), never its message."""
    if not isinstance(status, dict):
        return None
    for value in (status.get("code"), status.get("status")):
        if isinstance(value, int | str) and not isinstance(value, bool) and value in _RPC_BUSY:
            return _RPC_BUSY[value]
    return None


def status_retry_delay(body: Any) -> float | None:
    """The wait an error body's ``google.rpc.RetryInfo`` asks for (an API error's, or a long-running operation's
    ``error``), when it names one."""
    error = body.get("error", body) if isinstance(body, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    for detail in details if isinstance(details, list) else []:
        if isinstance(detail, dict) and str(detail.get("@type", "")).endswith("google.rpc.RetryInfo"):
            match = _GOOGLE_DELAY.match(str(detail.get("retryDelay") or ""))
            if match:
                return float(match.group(1))
    return None


def quota_exceeded(exc: BaseException) -> bool:
    """Whether the provider's error says a quota ran out, read from its structure, never its words.

    Google's ``google.rpc.QuotaFailure`` detail, an OpenAI-shaped error's ``insufficient_quota`` code or type.
    """
    return status_says_quota(error_body(exc))


def status_says_quota(body: Any) -> bool:
    """Whether an error body (an API error's, or a long-running operation's ``google.rpc.Status``) says a quota ran out:
    :func:`quota_exceeded`'s reading of its structure."""
    error = body.get("error", body) if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return False
    if "insufficient_quota" in (error.get("code"), error.get("type")):
        return True
    details = error.get("details")
    return any(
        isinstance(detail, dict) and str(detail.get("@type", "")).endswith("google.rpc.QuotaFailure")
        for detail in (details if isinstance(details, list) else [])
    )


def retry_delay(exc: BaseException) -> float | None:
    """The seconds the provider asked to wait before asking again, when it said."""
    stated = getattr(exc, "retry_after", None)
    if isinstance(stated, int | float) and not isinstance(stated, bool):
        return float(stated)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    raw = headers.get("retry-after") if headers is not None else None
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return status_retry_delay(error_body(exc))
