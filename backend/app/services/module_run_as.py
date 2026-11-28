# SPDX-License-Identifier: Apache-2.0

"""Who XTM One acts as in OpenCTI, OpenAEV and OpenCRQ (#2650).

Access to a module is always per user: that user's connection, markings and
rights. Every place XTM One provisions or reads in a module in the background
(a signal source, a fleet's automations, an organization sync) asks for that
user explicitly: the caller by default, any active user holding a connected
integration of the module when an administrator picks one. Nothing falls back
to another identity, and a missing choice is refused naming the modules to
choose for.

This module holds the shared rules: the candidates of the "Act in <module> as"
dialog, the check of a picked user, and the refusal of a missing choice. Other
integrations never go through it: they keep their configured connection.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: The modules whose access is per user, in a stable order.
MODULES: tuple[str, ...] = ("opencti", "openaev", "opencrq")

_LABELS = {"opencti": "OpenCTI", "openaev": "OpenAEV", "opencrq": "OpenCRQ"}

#: Why a candidate cannot be picked (``reason`` of :class:`Candidate`).
NO_INTEGRATION = "no_integration"


class RunAsRefusedError(Exception):
    """A run-as choice that cannot be used: ``status`` is the HTTP status, the text says why."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Candidate:
    """One user of the dialog: selectable, or listed disabled with its reason."""

    id: uuid.UUID
    name: str
    email: str
    is_self: bool
    selectable: bool
    reason: str | None = None


def module_label(provider: str) -> str:
    return _LABELS.get(provider, provider)


def is_module(provider: Any) -> bool:
    return str(provider or "") in MODULES


def missing_choice_refusal(modules: list[str]) -> str:
    """The 400 line of a call that did not say who to act as in *modules*."""
    names = ", ".join(module_label(m) for m in modules)
    return (
        f"Choose who XTM One acts as in {names}: pass run_as_user_id "
        "(yourself, or, as an administrator, an active user with the module connected)."
    )


async def resolve(db: AsyncSession, *, actor: Any, raw: Any, providers: list[str]) -> uuid.UUID:
    """The run-as of a write that acts in *providers*: the explicit *raw* choice, checked.

    Refused with :class:`RunAsRefusedError`: 400 when *raw* is missing and a
    module is needed (no silent default to the caller), 403 when a person who
    is not an administrator picks someone else, 422 for an unknown or inactive
    user, 400 when the user has no connected integration of a module.
    """
    from app.models.user import User
    from app.services.fleet_run_as import missing_providers, missing_refusal

    modules = [p for p in MODULES if p in set(providers)]
    if raw in (None, ""):
        raise RunAsRefusedError(400, missing_choice_refusal(modules or list(MODULES)))
    try:
        chosen = raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
    except ValueError:
        raise RunAsRefusedError(422, "run_as_user_id is not a user id") from None
    if chosen != actor.id and not getattr(actor, "is_admin", False):
        raise RunAsRefusedError(403, "Only an administrator can pick another user for XTM One to act as.")
    user = await db.get(User, chosen)
    if user is None or not getattr(user, "is_active", False):
        raise RunAsRefusedError(422, "run_as_user_id does not reference an active user")
    missing = await missing_providers(db, chosen, modules)
    if missing:
        raise RunAsRefusedError(400, missing_refusal(missing))
    return chosen


async def integration_refusal(db: AsyncSession, raw: Any, integration_id: Any) -> str | None:
    """Why *raw* cannot read *integration_id* as the run-as of a background pass, or ``None`` (#2739).

    The same rules as :func:`resolve`, held to one integration: an active user
    for whom that integration resolves (``template_candidate_filter``) and is
    connected (``connected_for_clause``). The text starts as the reasons
    ``ctem_platform_sources.reason_code`` reads (``run_as_inactive``,
    ``run_as_no_integration``), so a source shows it in the reader's language.
    """
    from app.models.integration import Integration
    from app.models.user import User
    from app.services.fleet_run_as import missing_refusal
    from app.services.integration_tools import template_candidate_filter
    from app.services.integration_user_credentials import connected_for_clause

    try:
        user_id = raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
        integration_uuid = integration_id if isinstance(integration_id, uuid.UUID) else uuid.UUID(str(integration_id))
    except ValueError:
        return "the run-as user is not a user id"
    user = await db.get(User, user_id)
    if user is None or not getattr(user, "is_active", False):
        return "the run-as user is deactivated or no longer exists"
    row = (
        await db.execute(
            select(Integration.provider).where(
                Integration.id == integration_uuid,
                await template_candidate_filter(db, user_id, company_automation=True),
                connected_for_clause(user_id),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        integration = await db.get(Integration, integration_uuid)
        return missing_refusal([str(getattr(integration, "provider", "") or "opencrq")])
    return None


async def candidates(db: AsyncSession, *, actor: Any, provider: str) -> list[Candidate]:
    """The users of the "Act in <module> as" dialog, the caller first.

    An administrator sees every active user: those with a connected integration
    of *provider* are selectable, the others disabled with ``no_integration``.
    Anyone else sees themself only.
    """
    from app.models.user import User
    from app.services.fleet_run_as import missing_providers

    if not is_module(provider):
        raise RunAsRefusedError(422, f"{provider} is not a module XTM One acts in per user")
    if getattr(actor, "is_admin", False):
        rows = (
            (await db.execute(select(User).where(User.is_active.is_(True)).order_by(User.full_name, User.email)))
            .scalars()
            .all()
        )
    else:
        rows = [actor]
    out: list[Candidate] = []
    for user in rows:
        missing = await missing_providers(db, user.id, [provider])
        out.append(
            Candidate(
                id=user.id,
                name=str(getattr(user, "full_name", "") or getattr(user, "email", "")),
                email=str(getattr(user, "email", "") or ""),
                is_self=user.id == actor.id,
                selectable=not missing,
                reason=NO_INTEGRATION if missing else None,
            )
        )
    out.sort(key=lambda c: (not c.is_self, not c.selectable, c.name.lower()))
    return out
