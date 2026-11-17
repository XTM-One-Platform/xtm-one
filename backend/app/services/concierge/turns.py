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

"""Who an agent turn acts as, for the Concierge (#8261; level 1 left this wiring to level 8).

Level 1 marks an assignment run of a fleet agent as the Concierge
(``identity.mark_concierge_run``, beside ``mark_company_managed_run``). A chat
turn, and the turn of an agent another one delegated to or consulted, start in
``agent_chat.run_agent_chat`` instead; :func:`concierge_turn_scoped` wraps it so
each turn acts as exactly what its own agent and user make it:

* a fleet agent's turn for no user or an administrator acts as the Concierge,
  with **that agent's** role (a specialist the orchestrator delegated to never
  inherits the orchestrator's role);
* any other turn - a person's agent consulted by a Concierge run, a fleet agent
  chatting with a non-administrator - acts as no principal, so the Concierge
  tools act as the person or refuse;
* except a sandbox turn (a copy's, or any turn inside a sandbox case or a
  sandbox flow's burst), which keeps the principal its runner holds: the
  copy runs as its source's owner, but its run is the Concierge's, so the
  admin bypass stays off and no consent-free read serves it
  (``identity.in_concierge_reach`` holds there with no principal too).

The scope ends with the turn, so nothing leaks into the caller's.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from app.services.concierge.identity import concierge_run_scope, in_sandbox_turn, mark_concierge_run


def concierge_turn_scoped[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Run ``fn(db, user_id, agent, ...)`` as the principal its agent and user make it."""

    @functools.wraps(fn)
    async def scoped(*args: P.args, **kwargs: P.kwargs) -> R:
        db, user_id, agent = _turn_args(args, kwargs)
        if _sandbox_turn(agent):
            # Kept as the runner holds it: a copy's own row never makes it the Concierge's agent.
            return await fn(*args, **kwargs)
        uid = _uuid(user_id)
        with concierge_run_scope():
            # An id that names no user is not "no user": such a turn never acts as the Concierge.
            if uid is not None or not user_id:
                await mark_concierge_run(db, agent, uid)
            return await fn(*args, **kwargs)

    return scoped


def _sandbox_turn(agent: Any) -> bool:
    from app.services.concierge.sandbox.copies import is_sandbox_copy

    return is_sandbox_copy(agent) or in_sandbox_turn()


def _turn_args(args: tuple, kwargs: dict) -> tuple[Any, Any, Any]:
    names = ("db", "user_id", "agent")
    values = list(args[:3]) + [kwargs.get(name) for name in names[len(args[:3]) :]]
    return values[0], values[1], values[2]


def _uuid(value: Any):
    from app.core.utils import safe_uuid

    return safe_uuid(value) if value else None
