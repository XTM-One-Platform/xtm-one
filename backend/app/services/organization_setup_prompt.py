# SPDX-License-Identifier: Apache-2.0

"""The first-connection setup proposal, versioned (#9173 S3, extended by #1087).

The platform proposes a guided setup to an administrator of an XTM One
platform once per instance and per setup version:

* at install (no organization profile saved yet), and
* on the first administrator sign-in after an upgrade that carries a newer
  :data:`SETUP_VERSION` than the one the instance last answered.

The first version, ``ctem-loop-1``, takes the administrator from the
organization context to a running CTEM loop fleet (CTEM-33): organization,
modules, ecosystem, gates, preview, enable. Whatever the answer ("started",
"later", "dismissed"), it is recorded on the ``platform_settings`` singleton
with the version answered, and the proposal is never shown again for that
version, to anyone; the setup stays reachable from the command center and the
CTEM loop fleet page. Each step of the flow saves on its own
(:func:`record_step`), so leaving midway loses nothing.

Never proposed to a non-administrator, never in Copilot mode.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.platform_settings import PlatformSettings

#: The setup version this build carries. Bump it (``ctem-loop-2``...) only for
#: a release whose setup an instance that already answered must see again.
SETUP_VERSION = "ctem-loop-1"

Outcome = Literal["started", "later", "dismissed"]
OUTCOMES: tuple[Outcome, ...] = ("started", "later", "dismissed")

#: The steps of the guided flow, in order.
STEPS: tuple[str, ...] = ("organization", "modules", "ecosystem", "gates", "preview", "enable")
StepState = Literal["done", "skipped"]
STEP_STATES: tuple[StepState, ...] = ("done", "skipped")


@dataclass(frozen=True)
class SetupPromptState:
    #: True when this caller must be shown the proposal now.
    propose: bool
    #: The recorded answer, ``None`` while the current version is unanswered.
    outcome: str | None
    answered_at: datetime | None
    #: The setup version this build carries.
    version: str = SETUP_VERSION
    #: The version the instance last answered, ``None`` when it never answered.
    answered_version: str | None = None
    #: Step key -> ``done`` or ``skipped``, for the current version only.
    steps: dict[str, str] = field(default_factory=dict)


def answered_current(row: PlatformSettings, version: str = SETUP_VERSION) -> bool:
    """True when the instance already answered *version*.

    An instance that answered the unversioned proposal of #9173 (the answer
    columns set, no version) has not answered ``ctem-loop-1``: it is the
    upgrade case, proposed once.
    """
    return row.setup_prompt_version == version and row.organization_setup_prompted_at is not None


def should_propose(*, is_admin: bool, is_xtm_one: bool, row: PlatformSettings, version: str = SETUP_VERSION) -> bool:
    """The rule, kept pure so every branch is unit-tested."""
    if not (is_admin and is_xtm_one):
        return False
    return not answered_current(row, version)


def steps_of(row: PlatformSettings, version: str = SETUP_VERSION) -> dict[str, str]:
    """The recorded step states of *version*; an older version's progress reads empty."""
    progress = row.setup_prompt_progress or {}
    if not isinstance(progress, dict) or progress.get("version") != version:
        return {}
    steps = progress.get("steps") or {}
    return {k: str(v) for k, v in steps.items() if k in STEPS and v in STEP_STATES}


def state_of(*, propose: bool, row: PlatformSettings, version: str = SETUP_VERSION) -> SetupPromptState:
    current = answered_current(row, version)
    return SetupPromptState(
        propose=propose,
        outcome=row.organization_setup_prompt_outcome if current else None,
        answered_at=row.organization_setup_prompted_at if current else None,
        version=version,
        answered_version=row.setup_prompt_version
        or ("legacy" if row.organization_setup_prompted_at is not None else None),
        steps=steps_of(row, version),
    )


def record_answer(
    row: PlatformSettings,
    *,
    outcome: Outcome,
    user_id: uuid.UUID,
    now: datetime | None = None,
    version: str = SETUP_VERSION,
) -> bool:
    """Record the answer to *version* once. Returns False when it was already answered.

    The first answer to a version wins: a second administrator's tab answering
    the same proposal leaves the record of the first. The answer columns hold
    the latest version's answer; ``setup_prompt_version`` names that version.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    if answered_current(row, version):
        return False
    row.organization_setup_prompted_at = now or datetime.now(UTC)
    row.organization_setup_prompted_by_user_id = user_id
    row.organization_setup_prompt_outcome = outcome
    row.setup_prompt_version = version
    return True


def record_step(row: PlatformSettings, *, step: str, state: StepState, version: str = SETUP_VERSION) -> dict[str, str]:
    """Record one step of the flow as done or skipped; answers the steps of *version*.

    Progress of an older version is dropped: a new setup version starts clean.
    """
    if step not in STEPS:
        raise ValueError(f"unknown step {step!r}")
    if state not in STEP_STATES:
        raise ValueError(f"unknown step state {state!r}")
    steps = steps_of(row, version)
    steps[step] = state
    # A new dict, so the JSON column is seen as changed.
    row.setup_prompt_progress = {"version": version, "steps": dict(steps)}
    return steps


def reset_answer(row: PlatformSettings) -> bool:
    """Forget the instance's answer and step progress, so the setup is proposed again.

    Settings > Platform "Reset onboarding": the four answer columns and the
    step progress are cleared, so the next administrator page load is proposed
    the current version from its first step, as after a fresh install. Only the
    proposal state is touched: the organization profile, integrations, fleets,
    automations and slot bindings keep what the earlier run saved. Returns
    False when there was nothing to clear.
    """
    cleared = any(
        value is not None
        for value in (
            row.organization_setup_prompted_at,
            row.organization_setup_prompted_by_user_id,
            row.organization_setup_prompt_outcome,
            row.setup_prompt_version,
            row.setup_prompt_progress,
        )
    )
    row.organization_setup_prompted_at = None
    row.organization_setup_prompted_by_user_id = None
    row.organization_setup_prompt_outcome = None
    row.setup_prompt_version = None
    row.setup_prompt_progress = None
    return cleared


async def has_saved_profile(db: AsyncSession) -> bool:
    from app.services import organization_context as org_context

    return await org_context.current_version(db) is not None
