"""The received window and the page token of the folder email listing.

``list_microsoft_emails`` reads one Graph page of a mail folder. A window
(``received_from`` / ``received_to``) becomes a ``receivedDateTime`` filter,
and a page past the first is reached with the token taken from Graph's
``@odata.nextLink``: its query, kept to the paging parameters, so a token can
only move through the same folder's listing and never sends the request
anywhere else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit

#: The query parameters a page token carries; any other one is dropped.
_TOKEN_PARAMS = frozenset({"$skip", "$skiptoken", "$filter", "$orderby"})


def received_instant(value: str) -> str | None:
    """*value* (ISO-8601, a date alone or a date and time) as the UTC instant Graph filters take, or None."""
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def received_filter(received_from: str = "", received_to: str = "") -> tuple[str, str | None]:
    """The ``$filter`` of a received window (empty without one), or the error that refuses it."""
    clauses: list[str] = []
    for name, value, operator in (("received_from", received_from, "ge"), ("received_to", received_to, "lt")):
        if not (value or "").strip():
            continue
        instant = received_instant(value)
        if instant is None:
            return (
                "",
                f"Error: {name} must be an ISO-8601 date or date and time (e.g. 2026-11-09T05:45:00Z), not {value!r}.",
            )
        clauses.append(f"receivedDateTime {operator} {instant}")
    return " and ".join(clauses), None


def next_page_token(next_link: object) -> str:
    """The page token of a listing's ``@odata.nextLink`` (empty when Graph has no next page)."""
    if not isinstance(next_link, str) or not next_link:
        return ""
    pairs = parse_qsl(urlsplit(next_link).query, keep_blank_values=True)
    return urlencode([(k, v) for k, v in pairs if k in _TOKEN_PARAMS], safe="$")


def page_params(page_token: str) -> dict[str, str] | None:
    """The query parameters a page token carries, or None when it is not a token of this listing."""
    pairs = parse_qsl((page_token or "").strip(), keep_blank_values=True)
    params = {k: v for k, v in pairs if k in _TOKEN_PARAMS}
    if not params.get("$skip", "").isdigit() and not params.get("$skiptoken"):
        return None
    return params


def pages_line(total: object, skip: int, shown: int, size: int) -> str:
    """How many messages the listing holds and how many pages remain after this one, or empty when unknown."""
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        return ""
    remaining = max(0, total - skip - shown)
    pages = -(-remaining // max(1, size))
    first, last = (skip + 1, skip + shown) if shown else (skip, skip)
    noun = "page remains" if pages == 1 else "pages remain"
    return f"Messages {first}-{last} of {total}; {pages} more {noun} at this count."
