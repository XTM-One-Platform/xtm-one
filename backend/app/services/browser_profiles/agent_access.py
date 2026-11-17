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

"""Which browser profiles an agent's ``browser`` tool may open for one run.

The ONE rule, read by every place that builds the ``browser`` tool (agent
chat, assignments, the General Assistant):

* a profile is opened only when it is BOUND - ``Agent.browser_profile_ids``
  for an agent, ``User.ga_browser_profile_ids`` for a person's General
  Assistant. Binding is explicit: an agent never reaches every login its user
  can see;
* AND the person the run acts for may use it - the three-tier predicate
  without the admin bypass, the rule of every other use of a profile
  (``profiles.load_usable_profile``). A run that acts for nobody (an
  anonymous public chat, an ownerless company-managed run) opens none;
* a binding listed in ``browser_profile_read_only`` opens read-only: the
  tool refuses ``click``, ``type``, ``press`` and ``evaluate`` in it.

Binding writes are checked where they are written (``api/v1/agents.py``,
``api/v1/settings.py``): the writer must be able to use what they add.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import safe_uuid
from app.models.browser_profile import BrowserProfile

#: The most profiles one agent binds.
MAX_BOUND_PROFILES = 50

VIA_AGENT = "agent"
VIA_GENERAL_ASSISTANT = "general_assistant"


def normalize_profile_ids(value: Iterable[object] | None) -> list[str] | None:
    """Bound profile ids as stored: canonical UUID strings, deduplicated, in order; ``None`` when empty.

    Raises ``ValueError`` on an entry that is not a UUID, or past :data:`MAX_BOUND_PROFILES`.
    """
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        raise ValueError("browser profile ids are a list of ids")
    ids: list[str] = []
    for raw in value:
        parsed = safe_uuid(str(raw).strip()) if raw is not None else None
        if parsed is None:
            raise ValueError(f"{raw!r} is not a browser profile id")
        text = str(parsed)
        if text not in ids:
            ids.append(text)
    if len(ids) > MAX_BOUND_PROFILES:
        raise ValueError(f"An agent binds at most {MAX_BOUND_PROFILES} browser profiles.")
    return ids or None


def normalize_read_only(read_only: Iterable[object] | None, bound: Iterable[object] | None) -> list[str] | None:
    """The read-only list as stored: the ids among *read_only* that are also bound."""
    bound_ids = normalize_profile_ids(bound) or []
    wanted = set(normalize_profile_ids(read_only) or [])
    kept = [pid for pid in bound_ids if pid in wanted]
    return kept or None


@dataclass(frozen=True)
class ProfileAccess:
    """What one run's ``browser`` tool may open: the bound profiles, for the person the run acts for."""

    profile_ids: tuple[str, ...]
    read_only_ids: frozenset[str] = field(default_factory=frozenset)
    #: The person the run acts for; ``None`` = no profile opens.
    user_id: str | None = None
    #: Who browses, for the audit trail.
    agent_id: str | None = None
    agent_name: str | None = None
    via: str = VIA_AGENT

    def is_read_only(self, profile_id: object) -> bool:
        return str(profile_id) in self.read_only_ids


def _stored_ids(value: object) -> list[str]:
    """A stored id list read as a run reads it: what is not a UUID is skipped, never an error."""
    if not isinstance(value, (list, tuple)):
        return []
    ids: list[str] = []
    for raw in value:
        parsed = safe_uuid(str(raw)) if raw is not None else None
        if parsed is not None and str(parsed) not in ids:
            ids.append(str(parsed))
    return ids[:MAX_BOUND_PROFILES]


def _access(
    ids: object,
    read_only: object,
    *,
    user_id: object,
    agent_id: object = None,
    agent_name: str | None = None,
    via: str,
) -> ProfileAccess | None:
    bound = _stored_ids(ids)
    if not bound:
        return None
    person = safe_uuid(str(user_id)) if user_id else None
    return ProfileAccess(
        profile_ids=tuple(bound),
        read_only_ids=frozenset(_stored_ids(read_only)) & frozenset(bound),
        user_id=str(person) if person else None,
        agent_id=str(agent_id) if agent_id else None,
        agent_name=agent_name,
        via=via,
    )


def access_for_agent(agent: Any, *, user_id: object) -> ProfileAccess | None:
    """The profiles *agent* binds, opened for *user_id*; ``None`` when it binds none."""
    return _access(
        getattr(agent, "browser_profile_ids", None),
        getattr(agent, "browser_profile_read_only", None),
        user_id=user_id,
        agent_id=getattr(agent, "id", None),
        agent_name=getattr(agent, "name", None),
        via=VIA_AGENT,
    )


def access_for_general_assistant(profile_ids: Iterable[object] | None, *, user_id: object) -> ProfileAccess | None:
    """The profiles a person gave their General Assistant; ``None`` when none."""
    return _access(profile_ids, None, user_id=user_id, agent_name="General Assistant", via=VIA_GENERAL_ASSISTANT)


async def usable_profiles(access: ProfileAccess) -> list[BrowserProfile]:
    """The bound profiles the run's person may use, by name (a session of its own)."""
    from app.core.database import async_session
    from app.services.browser_profiles.profiles import list_usable_profiles

    if not access.user_id:
        return []
    async with async_session() as db:
        rows = await list_usable_profiles(db, uuid.UUID(access.user_id), ids=access.profile_ids)
        for row in rows:
            db.expunge(row)
    return rows


class ProfileReferenceError(LookupError):
    """A ``profile`` argument that names no profile the run may open; ``str()`` says why, safe to show."""


async def resolve_profile(access: ProfileAccess | None, reference: str) -> BrowserProfile:
    """The usable bound profile *reference* names (its id, or its exact name), or :class:`ProfileReferenceError`."""
    if access is None:
        raise ProfileReferenceError("This agent is bound to no browser profile; it browses without a sign-in.")
    if not access.user_id:
        raise ProfileReferenceError(
            "Browser profiles open only for a person: this run acts for nobody, so it browses without a sign-in."
        )
    text = (reference or "").strip()
    rows = await usable_profiles(access)
    if not rows:
        raise ProfileReferenceError(
            "None of the browser profiles bound to this agent is one the person it acts for may use."
        )
    pid = safe_uuid(text)
    matches = (
        [row for row in rows if row.id == pid]
        if pid is not None
        else [row for row in rows if row.name.strip().casefold() == text.casefold()]
    )
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(f"{row.name} ({row.id})" for row in rows)
    if len(matches) > 1:
        raise ProfileReferenceError(f"Several browser profiles are named '{text}': give its id. Profiles: {names}.")
    raise ProfileReferenceError(f"No browser profile '{text}' this agent may use. Profiles: {names}.")
