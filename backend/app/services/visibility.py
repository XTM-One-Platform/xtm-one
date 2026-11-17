# SPDX-License-Identifier: Apache-2.0

"""The one visibility convention of the list endpoints (dev:visibility-filters-consistency).

Every list page of the frontend shows the same four segments, in this order:
Mine | Shared | Company | All. The list endpoints read the segment from one
query parameter, ``vis``, with one value set:

* ``mine``: owned by the caller, not published to the company;
* ``shared``: visible to the caller, neither the caller's nor the company's
  (shared directly or through a group, an account since deleted included);
* ``company``: company-managed, published to the whole company;
* ``all``: the union of the three, no narrowing.

The parameters the endpoints read before are kept as [DEPRECATED] aliases:
``visibility`` (presentations, videos), ``ownership`` (Operations feeds,
files) and ``scope`` (background tasks). ``vis`` wins when both are given.
The aliases' own values (``company_managed``, ``own``, ``everyone``) are
mapped onto the segments; an endpoint keeps answering its old values the way
it did, this module only names the convention new callers use.

Every paginated list also answers how many rows each segment holds under its
other filters, at ``GET <list>/segment-counts`` (:func:`segment_counts`), so
the control shows a number on each segment like the client-filtered lists do.

The frontend twin is ``frontend/src/lib/visibility.ts``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal, get_args

from fastapi import HTTPException
from pydantic import BaseModel

Visibility = Literal["mine", "shared", "company", "all"]

#: The segments in display order.
VISIBILITY_VALUES: tuple[Visibility, ...] = get_args(Visibility)

#: The query parameter that names the segment.
VISIBILITY_PARAM = "vis"

#: [DEPRECATED] query parameters read as aliases of ``vis`` when it is absent.
DEPRECATED_VISIBILITY_PARAMS: tuple[str, ...] = ("visibility", "ownership", "scope")

#: [DEPRECATED] values the aliases carried, mapped onto the segments.
_ALIAS_VALUES: dict[str, Visibility] = {
    "company_managed": "company",
    "own": "mine",
    "everyone": "all",
}


def to_visibility(raw: str | None) -> Visibility | None:
    """The segment *raw* names, or ``None`` when it is empty.

    An unknown value is a 422, never a silently dropped filter: ``?vis=min``
    must not read as "everything the caller can see".
    """
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if value in VISIBILITY_VALUES:
        return value  # type: ignore[return-value]
    if value in _ALIAS_VALUES:
        return _ALIAS_VALUES[value]
    raise HTTPException(
        status_code=422,
        detail=f"Unknown visibility `{raw}`. Use one of: {', '.join(VISIBILITY_VALUES)}.",
    )


def resolve_visibility(vis: str | None, *aliases: str | None, default: Visibility = "all") -> Visibility:
    """The segment a request asks for: ``vis`` first, then the [DEPRECATED] aliases in order, else *default*.

    The endpoint default stays ``all`` (the API contract before this
    convention); the frontend lands on ``mine`` by sending ``vis=mine``.
    """
    for raw in (vis, *aliases):
        value = to_visibility(raw)
        if value is not None:
            return value
    return default


class SegmentCounts(BaseModel):
    """How many rows each segment of a list holds under the list's other filters."""

    mine: int
    shared: int
    company: int
    all: int


async def segment_counts(count: Callable[[Visibility], Awaitable[int]]) -> SegmentCounts:
    """The four segment counts of a list, *count* answering one segment under the list's other filters.

    The ``GET <list>/segment-counts`` endpoints share this one rule: each
    segment is counted with the filter the list applies for it, never derived
    from the others, so a list whose ``all`` is not the union of the three
    (background tasks for an admin) still reads what the tab would show. The
    counts run one after the other: they share the request's session.
    """
    values = {segment: await count(segment) for segment in VISIBILITY_VALUES}
    return SegmentCounts(**values)
