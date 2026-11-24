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

"""The Platform Health identity: who a Platform Health run acts as (#8254).

A platform principal of its own, NOT a ``users`` row (PRD
``dev-docs/platform-concierge.md`` section 13). A user row could be shared
with, added to a group, picked in a user picker or linked by SSO email, and any
of these would widen Platform Health's reach past its supervision scope and
consent. The fleet's runs are company-managed assignment runs with no user, so
they already read with no account and no admin bypass
(``helpers.no_account_provenance``); this module adds which runs those are.

* A run acts as Platform Health when its agent row carries a ``health_role``,
  is company-managed, Platform Health is available, and the run has no user or
  an admin user (:func:`health_principal_for`). A non-admin chatting with a
  fleet agent gets no principal, so every Platform Health tool refuses.
* A fleet run nobody started by hand acts with no user at all: never as its
  agent's owner (``assignment_engine._acting_identity``), and the work it
  delegates neither (:data:`HEALTH_ALONE_KEY`, :func:`health_acts_alone`).
  A sandbox copy's turn keeps the principal its runner holds, and with or
  without one reaches only what Platform Health may (:func:`in_health_reach`).
* The principal is held in a context variable written by server code only
  (:func:`acting_as_health_principal`, :func:`mark_health_run`), never derived
  from a request or a model's output.
* Rows Platform Health creates carry ``user_id = NULL`` plus the creating
  level's own marker. :data:`HEALTH_PRINCIPAL_ID` names the principal in
  columns that are not a ``users`` foreign key; written into one, it fails.
* Audit entries name it ``system:concierge`` (:data:`HEALTH_ACTOR` through
  ``services.audit.log_system_audit``).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Final, Literal, get_args

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.health.availability import health_enabled

#: ``log_system_audit(actor=...)``: audit rows read ``system:concierge``.
HEALTH_ACTOR: Final = "concierge"

#: The principal's stable id. Never a ``users`` row.
HEALTH_PRINCIPAL_ID: Final[uuid.UUID] = uuid.uuid5(uuid.NAMESPACE_URL, "xtm-one:platform-concierge")

#: The three module health checkers (#101 S15, ``dev-docs/platform-health.md`` section 13.1).
#: ``module_analyst`` is the role the single S14 member held: kept so the cycles,
#: recommendations and spend rows it wrote still read as Platform Health's.
HealthRole = Literal[
    "orchestrator",
    "memory_steward",
    "quality_analyst",
    "flow_lab",
    "experimenter",
    "opencti_checker",
    "openaev_checker",
    "opencrq_checker",
    "module_analyst",
]
HEALTH_ROLES: Final[frozenset[str]] = frozenset(get_args(HealthRole))
#: The module checker role per module provider (section 13.1).
CHECKER_ROLE_BY_PROVIDER: Final[dict[str, str]] = {
    "opencti": "opencti_checker",
    "openaev": "openaev_checker",
    "opencrq": "opencrq_checker",
}
#: Every role a module checker cycle is booked under, the S14 alias included.
MODULE_CHECKER_ROLES: Final[frozenset[str]] = frozenset({*CHECKER_ROLE_BY_PROVIDER.values(), "module_analyst"})


@dataclass(frozen=True, slots=True)
class HealthPrincipal:
    """Platform Health, acting through one fleet agent.

    ``on_behalf_of_admin_id`` is the administrator whose chat or on-demand
    request started the run, ``None`` for a scheduled or signal run. It is who
    asked, never whose rights apply: Platform Health's reach is its scope and
    consent alone.
    """

    agent_id: uuid.UUID
    role: str
    on_behalf_of_admin_id: uuid.UUID | None = None


_CURRENT: ContextVar[HealthPrincipal | None] = ContextVar("concierge_principal", default=None)
# Whether a run scope is open, so a mark never outlives the run that set it.
_RUN_SCOPE_OPEN: ContextVar[bool] = ContextVar("concierge_run_scope_open", default=False)


def health_identity_id() -> uuid.UUID:
    """The principal's id (no row to read)."""
    return HEALTH_PRINCIPAL_ID


def is_health_identity(value: object) -> bool:
    """Whether *value* names Platform Health: a principal, or its id as UUID or string."""
    if isinstance(value, HealthPrincipal):
        return True
    if isinstance(value, uuid.UUID):
        return value == HEALTH_PRINCIPAL_ID
    if isinstance(value, str):
        try:
            return uuid.UUID(value) == HEALTH_PRINCIPAL_ID
        except ValueError:
            return False
    return False


def is_health_agent(agent: Any) -> bool:
    """Whether *agent*'s row makes it a member of the Platform Health fleet.

    The role alone is not enough: the fleet is company-managed, and a private
    agent never acts as the platform.
    """
    role = getattr(agent, "health_role", None)
    return role in HEALTH_ROLES and getattr(agent, "is_company_managed", False) is True


def health_principal_for(agent: Any, user: Any | None) -> HealthPrincipal | None:
    """The principal a run of *agent* for *user* acts as, or ``None``.

    ``None`` while the flag is off, for an agent outside the fleet, and for a
    run whose user is not an administrator.
    """
    if not health_enabled() or not is_health_agent(agent):
        return None
    if user is None:
        return HealthPrincipal(agent_id=agent.id, role=agent.health_role)
    if getattr(user, "role", None) != "admin" or getattr(user, "is_active", True) is False:
        return None
    return HealthPrincipal(agent_id=agent.id, role=agent.health_role, on_behalf_of_admin_id=user.id)


@contextlib.contextmanager
def acting_as_health_principal(principal: HealthPrincipal) -> Iterator[None]:
    """Scope in which the current work acts as *principal*."""
    if not isinstance(principal, HealthPrincipal):
        raise TypeError("acting_as_health_principal needs a HealthPrincipal")
    token = _CURRENT.set(principal)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_health_principal() -> HealthPrincipal | None:
    """The principal the current work acts as, or ``None``.

    Re-checks the flag: switching it off stops a run in flight from acting
    as Platform Health at its next check.
    """
    principal = _CURRENT.get()
    return principal if principal is not None and health_enabled() else None


def in_health_run() -> bool:
    return current_health_principal() is not None


def health_acts_alone() -> bool:
    """Whether the current work acts as Platform Health with nobody behind it: a
    fleet run nobody started by hand, and the work it delegates. Such work
    acts with no user at all, never as its agents' owner (PRD 13)."""
    principal = current_health_principal()
    return principal is not None and principal.on_behalf_of_admin_id is None


def in_health_reach() -> bool:
    """Whether the current work reaches only what Platform Health may: a Platform Health
    run or turn, or a turn intercepted by the sandbox (a copy's turn, a sandbox
    flow's agent node, whoever started it - an evaluation case included).

    There no agent tool takes the admin bypass and no read without a consent
    rule serves another agent (PRD 9): a copy runs as its source's owner, often
    an administrator, but its runs are Platform Health's.
    """
    return in_health_run() or in_sandbox_turn()


def in_sandbox_turn() -> bool:
    """Whether the current work is intercepted by a sandbox case (not a dry run)."""
    from app.services.health.sandbox.session import current_sandbox_session

    session = current_sandbox_session()
    return session is not None and session.mode == "sandbox"


#: Set by the server on a task Platform Health delegated with no user
#: (:func:`health_acts_alone` at spawn): its run and its turn act with no
#: user. The task's ``user_id``, which the row needs, is its owner of record only.
HEALTH_ALONE_KEY: Final = "concierge_alone"


async def health_delegation_owner_id(db: AsyncSession, agent: Any) -> uuid.UUID | None:
    """The ``users`` row a delegation by *agent*'s Platform Health run with no user is recorded under, else ``None``.

    ``sub_agent_tasks.user_id`` needs one: the delegating fleet agent's owner
    (invariant 9), else the company-managed owner. Only as the record's owner
    (its notices, its result files): the task is marked
    :data:`HEALTH_ALONE_KEY`, so its turn and its reach are no user's.
    """
    if not health_acts_alone() or not is_health_agent(agent):
        return None
    if getattr(agent, "user_id", None) is not None:
        return agent.user_id
    from app.api.v1.helpers import resolve_company_managed_owner_id

    return await resolve_company_managed_owner_id(db)


@contextlib.contextmanager
def health_run_scope() -> Iterator[None]:
    """A run scope: nothing acts as Platform Health until :func:`mark_health_run`
    marks it, and the mark ends with the scope."""
    principal_token = _CURRENT.set(None)
    scope_token = _RUN_SCOPE_OPEN.set(True)
    try:
        yield
    finally:
        _RUN_SCOPE_OPEN.reset(scope_token)
        _CURRENT.reset(principal_token)


async def mark_health_run(db: AsyncSession, agent: Any, user_id: uuid.UUID | None) -> HealthPrincipal | None:
    """Mark the run in scope as Platform Health's when *agent* and *user_id* make it one.

    Called by the assignment engine beside ``mark_company_managed_run``. Outside
    a :func:`health_run_scope` it marks nothing.
    """
    if not _RUN_SCOPE_OPEN.get():
        return None
    user = None
    if user_id is not None and is_health_agent(agent):
        from app.models.user import User

        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None:
            _CURRENT.set(None)
            return None
    principal = health_principal_for(agent, user)
    _CURRENT.set(principal)
    return principal


async def set_health_role(db: AsyncSession, agent: Any, role: str | None) -> None:
    """Give *agent* its fleet role, or take it away. The seed's call alone.

    Refuses a role outside :data:`HEALTH_ROLES` and an agent that is not
    company-managed. Flushes, does not commit.
    """
    if role is not None:
        if role not in HEALTH_ROLES:
            raise ValueError(f"not a Platform Health role: {role!r}")
        if getattr(agent, "is_company_managed", False) is not True:
            raise ValueError("a Platform Health fleet agent must be company-managed")
    agent.health_role = role
    await db.flush()
