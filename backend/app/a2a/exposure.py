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

"""Exposure authority and run-as identity.

**The rule: exposing an agent never lends the exposer's own reach.** A group
member holding the config-tier sharing permission on someone else's agent may
expose it; the identity the partner's work runs as is resolved from the agent
(its owner) or chosen deliberately by an administrator (company-managed), and
in neither case is it the person who flipped the switch.

There is no A2A-specific data scope, and adding one would mean inventing an
access mode this platform does not have. External work runs as a user and sees
what that user sees, resolved by ``run_agent_chat`` exactly as it is for chat,
``/mcp/user`` and assignments. What this module bounds is *which identity*
runs - see ``resolve_run_as``. It does not bound which tools that identity may
use: external work resolves the agent's own configuration in full, handover
peers included.

No new permission concept is introduced. Authority to expose is
``writable_agent_error()`` - the same gate that already decides who may edit
an agent or attach an assignment to it - so owner / administrator /
config-tier-grantee / company-managed distinctions are inherited rather than
re-invented. Its one read-only rule, built-ins in XTM One mode, is dropped
here and only here; see ``user_can_expose`` for why exposure is the class of
change that rule was never meant to catch.
"""

from __future__ import annotations

import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.helpers import get_user_group_ids, writable_agent_error
from app.models.a2a import A2AExposure
from app.models.agent import Agent
from app.models.user import User

#: Card tokens are 32 bytes of URL-safe randomness. They are opaque
#: identifiers, not secrets that gate access - the task surface is what
#: requires credentials - but they must be unguessable, because the
#: specification forbids any listing of exposed agents and an enumerable
#: token would reconstruct exactly that listing.
_CARD_TOKEN_BYTES = 32


async def user_can_expose(
    db: AsyncSession, user: User, agent: Agent
) -> bool:
    """Return True when *user* may change *agent*'s exposure state.

    Delegates to ``writable_agent_error`` — the SAME gate every other
    attachment surface uses (assignments, channel wiring, the agent builder).
    Exposure is another management action, not a new capability: company-managed
    agents stay administrator-only and config-tier grantees (group or per-user)
    inherit the ability.

    **Built-in agents are exposable**, under ``allow_builtin=True``. In XTM One
    mode a built-in is company-managed, so this still resolves to
    administrator-only — the flag drops the read-only rule and nothing else.
    Two reasons it is right to drop here:

    * Exposure is not part of a built-in's seeded definition. It follows
      ``integration_bindings`` (``a2a.providers``), which describes the
      deployment's own accounts rather than the agent, and which the seed pass
      leaves to administrators — the same reason every other integration is
      configurable on a built-in.
    * In XTM One the built-ins ARE the product's agents. Refusing them would
      make the feature reach only agents an operator hand-built, which is the
      opposite of what a partner integration is for.

    What DOES resync is the card's advertised surface — ``name``,
    ``description`` and ``builtin_tool_ids`` all come from the seed data — so a
    platform upgrade can change what a live card advertises. That is a
    versioned, reviewed change to the product's own agents rather than silent
    drift, and it never moves the boundary: the run-as identity is recorded on
    the exposure and the seed pass does not touch it.
    """
    group_ids = await get_user_group_ids(db, user.id)
    return (
        writable_agent_error(
            agent, user, group_ids, action="expose", allow_builtin=True
        )
        is None
    )


class RunAsRequiredError(ValueError):
    """There is no identity for external work on this agent to run as.

    Raised only when the agent has no stored owner — the fail-closed case, in
    which the binding simply produces no exposure. See :func:`resolve_run_as`.
    """


def resolve_run_as(agent: Agent) -> uuid.UUID:
    """Resolve the identity external work on *agent* executes as: its owner.

    Mirrors ``Assignment.run_as_user_id``, which is the platform's existing
    answer to "whose data does a company-managed agent see". There is no
    company-scoped access mode in this platform to reach for instead —
    interactive chat borrows the chatting user's identity, ``/mcp/user`` the
    caller's — and an external task is the one path with no invoking human to
    borrow from, which is why the identity is resolved once and recorded.

    **Always inherited, never nominated, and the signature is the guarantee.**
    This took a ``requested`` override while an expose endpoint existed to
    supply one. Nothing supplies one now — exposure follows an integration
    binding, and the flush hook that creates it has no operator to ask — so
    the parameter went with the endpoint rather than staying as a choice the
    platform appears to offer and no surface can make.

    **For a company-managed agent this is an accepted trade-off, not a good
    answer.** Its ``user_id`` is whoever seeded it — usually an administrator
    — so the partner sees that person's personal and group-shared data. The
    alternative, refusing to expose company-managed agents, would exclude
    every built-in and so most of what the feature is for. It is mitigated by
    being stated: the resolved identity is shown on the agent's Channels &
    Bots tab, notified to the people it concerns, and recorded in the audit
    trail (FR-031).
    """
    if agent.user_id is None:
        # Nothing to inherit: fail closed rather than invent an identity. The
        # agent simply does not become exposed.
        raise RunAsRequiredError("Agent has no owner to run as")
    return agent.user_id


def _mint_card_token() -> str:
    return secrets.token_urlsafe(_CARD_TOKEN_BYTES)[:64]


async def get_live_exposure(
    db: AsyncSession, agent_id: uuid.UUID
) -> A2AExposure | None:
    result = await db.execute(
        select(A2AExposure).where(
            A2AExposure.agent_id == agent_id,
            A2AExposure.revoked_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


def _resolvable_exposures():
    """SELECT for exposures that may still be resolved for external callers.

    Two conditions, and the second is FR-007 rather than a nicety: an exposure
    stops resolving when "the user it executes as [is] deactivated or deleted —
    without falling back to any other identity".

    Deletion is already covered by the ``ON DELETE CASCADE`` on
    ``run_as_user_id``: removing the user removes the exposure. Deactivation
    needs this join, because ``users.toggle-active`` only flips ``is_active``
    and leaves the row in place — the departed-employee case, whose data must
    not stay in a partner's reach.

    An INNER JOIN, so a deactivated user's exposure disappears from the result
    rather than resolving to a row a caller then has to remember to check.

    Deliberately a filter rather than a revocation. FR-007 says "cease
    resolving", not "revoke", and the distinction is load-bearing in both
    directions: deactivation is reversible (``toggle-active`` is a toggle), so
    revoking would permanently break a partner over a temporary suspension,
    while a filter takes effect on every replica the instant the flag flips and
    heals by itself when the account is restored.

    Applies ONLY to external resolution. ``get_live_exposure`` deliberately
    omits it: the internal surface must still be able to show an operator the
    exposure that has stopped resolving, and to revoke it.
    """
    return (
        select(A2AExposure)
        .join(User, User.id == A2AExposure.run_as_user_id)
        .where(A2AExposure.revoked_at.is_(None), User.is_active.is_(True))
    )


async def get_exposure_by_token(
    db: AsyncSession, card_token: str
) -> A2AExposure | None:
    """Resolve a live exposure by card token.

    A revoked exposure's token resolves to ``None``, not to the revoked row.
    Callers must be unable to tell "revoked" from "never existed": the
    retained row exists for audit and to guarantee the token is never reissued,
    not to give a probing caller a different answer.

    An exposure whose run-as user has been deactivated resolves to ``None`` for
    the same reason and through the same uniform not-found — see
    :func:`_resolvable_exposures`.
    """
    result = await db.execute(
        _resolvable_exposures().where(A2AExposure.card_token == card_token)
    )
    return result.scalar_one_or_none()


async def resolvable_exposures_for_integration(
    db: AsyncSession, integration_id: uuid.UUID, provider: str
) -> list[A2AExposure]:
    """Every live exposure an A2A integration currently reaches.

    The integration-scoped half of :func:`_resolvable_exposures`. One
    ``oauth_a2a`` credential is valid on every agent that binds it, so
    "does this credential still address a live surface?" is a question about
    a SET of exposures rather than one — but the answer must be built from
    the same filter the task surface resolves on, or a credential could stay
    mintable after everything it addresses stopped answering.
    """
    from app.a2a.providers import agents_binding

    agent_ids = [a.id for a in await agents_binding(db, integration_id, provider)]
    if not agent_ids:
        return []
    result = await db.execute(
        _resolvable_exposures().where(A2AExposure.agent_id.in_(agent_ids))
    )
    return list(result.scalars().all())


async def get_resolvable_exposure(
    db: AsyncSession, exposure_id: uuid.UUID
) -> A2AExposure | None:
    """Resolve an exposure by id for an external caller, or ``None``.

    The token endpoint's half of :func:`_resolvable_exposures`. Minting is
    gated on exactly what the task surface resolves on, so a deactivated
    run-as user cannot be worked around by holding a token issued a minute
    earlier — the credential stops being issuable and the surface it addresses
    stops answering at the same moment.
    """
    result = await db.execute(
        _resolvable_exposures().where(A2AExposure.id == exposure_id)
    )
    return result.scalar_one_or_none()


def exposure_invalidated(exposure: A2AExposure, agent: Agent) -> bool:
    """True when *agent* changed such that its exposure must not stand.

    Checked in BOTH directions. A personally-owned agent's run-as identity IS
    its owner, so an ownership change moves it. A tier change moves it too:
    becoming company-managed detaches the identity from the owner it was
    resolved from, and ceasing to be company-managed re-attaches it to an
    owner the recorded identity may not match. Either way the identity a
    partner's work executes as would change under them, which is the thing
    revocation exists to prevent.
    """
    if agent.is_company_managed:
        # The recorded identity is not tied to this agent's stored owner —
        # nominated or inherited, it was resolved once and pinned on the
        # exposure — so only the tier change itself can invalidate it, and
        # that is watched separately (``hooks._WATCHED``).
        return False
    return exposure.run_as_user_id != agent.user_id


# There is no in-process guard against an external task performing a
# management action, and none is wanted: a partner token carries a ``scope``
# claim, and ``deps.get_current_user_ungated`` refuses ANY token carrying one,
# so such a token cannot authenticate to ``/api/v1/*`` at all - the exposure
# endpoints included. Being a property of the token rather than a list of
# forbidden verbs, it closes an endpoint added tomorrow without anyone
# extending an enumeration. Covered by ``test_a2a_oauth_client_credentials``.
# A management-capable TOOL inside the task runs in-process, so it is held by
# the partner-task scope instead (``partner_scope``): ``agent_builder``
# keeps its read half, as an anonymous caller, and no trace tool is wired.
