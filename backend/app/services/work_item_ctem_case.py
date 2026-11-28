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

"""The CTEM case a work item is about, derived by the platform (#2370).

A CTEM gate (review, validation, remediation, a fleet's launch, mobilize or
re-test step) is a work item keyed on its case: ``ctem:validation:<case id>``,
``ctem:f2:retest:<case id>``. The link from the decision back to the case was
left to the agent's ``ask.links`` and was in practice absent, so the person
decided without the case in front of them. The platform defines those keys
(``builtin_agents.CTEM_*_GATE_PREFIXES``, ``case-validation.ts``), so it derives
the case itself:

* :func:`case_id_of`: the case id of an item, from ``payload.case_id`` first,
  else from its correlation key (the last segment, a UUID). Pure.
* :func:`with_case_link`: the ask a decision card carries, with "Open the case"
  as its first link and any agent link to the same case dropped. Every card
  surface (Teams, Outlook, Slack, Google Chat, the email) reads ``ask.links``.
* :func:`readable_case`: ``{id, title, state, path}`` for the item dialog when
  the reader sees the case under the case page's own rule, else ``None``: no
  link that answers 404.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.agents.builtin_agents import CTEM_F1_GATE_PREFIXES, CTEM_F2_GATE_PREFIXES

logger = logging.getLogger(__name__)

#: The work item key prefixes that name a CTEM case: every gate key of the two seeded fleets
#: (``builtin_agents.CTEM_F1_GATE_PREFIXES`` / ``CTEM_F2_GATE_PREFIXES``, the single source,
#: so a gate added there names its case here too) and every other key of those fleets.
CTEM_CASE_KEY_PREFIXES: tuple[str, ...] = tuple(
    dict.fromkeys((*CTEM_F1_GATE_PREFIXES, *CTEM_F2_GATE_PREFIXES, "ctem:f1:", "ctem:f2:"))
)

#: The ``kind`` of the derived case link in ``ask.links``: its button is worded from the catalog.
CASE_LINK_KIND = "ctem_case"

#: How long the item read waits for the case visibility check before it leaves the link out.
READ_TIMEOUT_SECONDS = 3.0


def _uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def case_id_of(correlation_key: object, payload: object = None) -> uuid.UUID | None:
    """The CTEM case *correlation_key* / *payload* name, ``None`` for any other item.

    ``payload.case_id`` wins when it is a UUID; else a key under one of
    :data:`CTEM_CASE_KEY_PREFIXES` whose last ``:`` segment is a UUID
    (``ctem:f1:state``, a fleet's own state key, names none).
    """
    if isinstance(payload, dict):
        found = _uuid(payload.get("case_id")) if payload.get("case_id") else None
        if found is not None:
            return found
    key = str(correlation_key or "")
    if not key.startswith(CTEM_CASE_KEY_PREFIXES):
        return None
    return _uuid(key.rsplit(":", 1)[-1])


def case_path(case_id: uuid.UUID) -> str:
    """The case page in the app (``/ctem/cases/:id``)."""
    return f"/ctem/cases/{case_id}"


def names_case(url: object, case_id: uuid.UUID) -> bool:
    """*url* opens the page of *case_id* (``/ctem/cases/<id>`` or the ticketing ``/xtm-one/ctem/cases/<id>``)."""
    return f"/ctem/cases/{case_id}" in str(url or "")


def with_case_link(ask: dict[str, Any], correlation_key: object, payload: object, base_url: str) -> dict[str, Any]:
    """*ask* with "Open the case" as its first link when the item names a case; *ask* unchanged otherwise.

    An agent's link to the same case (any label) is dropped, so the card never
    shows the case twice; its other links keep their order.
    """
    case_id = case_id_of(correlation_key, payload)
    if case_id is None or not base_url:
        return ask
    others = [
        link
        for link in (ask.get("links") or [])
        if not (isinstance(link, dict) and names_case(link.get("url"), case_id))
    ]
    case_link = {"label": "Open the case", "url": f"{base_url.rstrip('/')}{case_path(case_id)}", "kind": CASE_LINK_KIND}
    return {**ask, "links": [case_link, *others]}


CaseReader = Callable[[uuid.UUID, Any], Awaitable[dict[str, Any] | None]]


async def _visible(case_id: uuid.UUID, user: Any) -> dict[str, Any] | None:
    """The case as the case page renders it for *user*, ``None`` when they see none of it."""
    from fastapi import HTTPException

    from app.api.v1.ctem_cases import visible_case
    from app.core.database import async_session
    from app.services import ctem_cases_view as view_of
    from app.services.ctem_verticals import normalize_vertical

    vertical = normalize_vertical(getattr(user, "ctem_vertical", None))
    try:
        async with async_session() as db:
            shown = await visible_case(db, case_id, str(user.id), vertical, read_only=True)
    except (HTTPException, view_of.ViewRebuildingError):
        return None
    return None if shown is None else shown[1]


async def readable_case(item: dict[str, Any], user: Any, read: CaseReader | None = None) -> dict[str, Any] | None:
    """``{id, title, state, path}`` of the case *item* names when *user* may read it, else ``None``.

    The case page's own rule decides (``ctem_cases.visible_case``): a reader who
    sees none of its issues gets no link. Outside XTM One mode, for an item that
    names no case, or when the check outlasts :data:`READ_TIMEOUT_SECONDS`, ``None``.
    """
    from app.core.config import settings

    if settings.is_copilot_like:
        return None
    case_id = case_id_of(item.get("correlation_key"), item.get("payload"))
    if case_id is None:
        return None
    try:
        rendered = await asyncio.wait_for((read or _visible)(case_id, user), READ_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.info("work item %s: case %s visibility check timed out", item.get("id"), case_id)
        return None
    if not rendered:
        return None
    return {
        "id": str(case_id),
        "title": str(rendered.get("title") or ""),
        "state": rendered.get("state"),
        "path": case_path(case_id),
    }
