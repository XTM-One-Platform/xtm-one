# SPDX-License-Identifier: Apache-2.0

"""The first-login organization setup proposal (#9173, S3).

The first time an administrator signs in to an XTM One platform whose
organization profile was never saved, the platform proposes the setup once:
"Set up now" opens Settings > Organization in edit mode with the Draft menu,
"Later" and "Dismiss" close it. Whatever the answer, it is recorded on the
``platform_settings`` singleton and the proposal is never shown again on this
instance, to anyone; the setup stays reachable from Settings > Organization.

Never proposed to a non-administrator, never in Copilot mode, never once a
profile version exists (an instance already set up has nothing to propose).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.platform_settings import PlatformSettings

Outcome = Literal["started", "later", "dismissed"]
OUTCOMES: tuple[Outcome, ...] = ("started", "later", "dismissed")


@dataclass(frozen=True)
class SetupPromptState:
    #: True when this caller must be shown the proposal now.
    propose: bool
    #: The recorded answer, ``None`` while unanswered.
    outcome: str | None
    answered_at: datetime | None


def should_propose(*, is_admin: bool, is_xtm_one: bool, has_profile: bool, row: PlatformSettings) -> bool:
    """The rule, kept pure so every branch is unit-tested."""
    if is_admin and is_xtm_one and not has_profile:
        return row.organization_setup_prompted_at is None
    return False


def state_of(*, propose: bool, row: PlatformSettings) -> SetupPromptState:
    return SetupPromptState(
        propose=propose,
        outcome=row.organization_setup_prompt_outcome,
        answered_at=row.organization_setup_prompted_at,
    )


def record_answer(row: PlatformSettings, *, outcome: Outcome, user_id: uuid.UUID, now: datetime | None = None) -> bool:
    """Record the answer once. Returns False when the instance had already answered.

    The first answer wins: a second administrator's tab answering the same
    proposal leaves the record of the first.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    if row.organization_setup_prompted_at is not None:
        return False
    row.organization_setup_prompted_at = now or datetime.now(UTC)
    row.organization_setup_prompted_by_user_id = user_id
    row.organization_setup_prompt_outcome = outcome
    return True


async def has_saved_profile(db: AsyncSession) -> bool:
    from app.services import organization_context as org_context

    return await org_context.current_version(db) is not None
