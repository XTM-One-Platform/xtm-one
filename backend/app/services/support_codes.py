# SPDX-License-Identifier: Apache-2.0

"""Support codes: one entry point for every grant a Filigran code can carry.

A support code is verified offline against the license trust anchor and burned
in ``support_code_redemptions`` (``agent_failure_pool.verify_support_code`` /
``burn_support_code``). Two kinds exist:

* ``agent_failure_pool_recharge`` - every agent failure pool starts full again
  (``agent_failure_pool.apply_pool_recharge``);
* ``platform_quota_reset`` - the platform-wide usage counters of the
  current windows start again from zero (``quota.reset_platform_quota_usage``).
  This is what a proof of concept needs once it has used the license's
  allowance before the evaluation ends: the counters the ceilings are enforced
  against belong to the license, so no admin action resets them, and a new
  license is far more than the POC needs. The ceilings themselves are
  untouched.

``verify_redeemable_code`` checks a pasted code against every kind and
``apply_support_code`` applies the one it carries (``redeem_support_code`` does
both). The code is burned in the transaction that applies it, so a grant that
fails - a busy flush, a database error - leaves it unused.

Claims of a reset code
----------------------

``quota_types`` and ``periods`` say which counters it resets. Both are
optional; an absent (or ``null``) claim means the default:

* ``quota_types``: ``agentic`` and ``sandbox`` - the two counters an agentic
  ceiling adds up (``check_agentic_quota``). ``llm_proxy`` resets the LLM
  proxy's counter, capped only in Copilot mode.
* ``periods``: ``daily``, ``monthly`` and ``yearly`` - the CURRENT window of
  each. Past windows are history and stay as they are.

A claim that is present must be a non-empty list of known names. Anything else
is refused before the code is burned: a signed code is Filigran's, not
necessarily sane, and guessing what a malformed scope meant would reset
counters nobody asked to reset.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.agent_failure_pool import (
    PLATFORM_QUOTA_RESET_KIND,
    POOL_RECHARGE_KIND,
    SupportCode,
    SupportCodeError,
    apply_pool_recharge,
    burn_support_code,
    verify_support_code,
)
from app.services.quota import (
    PLATFORM_RESETTABLE_PERIODS,
    PLATFORM_RESETTABLE_QUOTA_TYPES,
    reset_platform_quota_usage,
)

#: Every kind ``redeem_support_code`` applies.
SUPPORTED_KINDS = (POOL_RECHARGE_KIND, PLATFORM_QUOTA_RESET_KIND)

#: What a reset code resets when it names no quota type / no period.
DEFAULT_RESET_QUOTA_TYPES = ("agentic", "sandbox")
DEFAULT_RESET_PERIODS = PLATFORM_RESETTABLE_PERIODS

_SCOPE_REFUSAL = (
    "This support code carries a reset scope this version of the platform "
    "cannot apply. Ask Filigran support for a new code."
)


def _scope_claim(
    claims: object, name: str, allowed: Sequence[str], default: Sequence[str]
) -> tuple[str, ...]:
    """One scope claim of a reset code, in *allowed*'s order; refused unless
    absent or a non-empty list of names from *allowed*."""
    raw = claims.get(name) if isinstance(claims, dict) else None
    if raw is None:
        return tuple(default)
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(item, str) and item in allowed for item in raw)
    ):
        raise SupportCodeError(_SCOPE_REFUSAL)
    named = set(raw)
    return tuple(item for item in allowed if item in named)


def platform_quota_reset_scope(
    verified: SupportCode,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(quota_types, periods)`` a verified reset code resets; raises
    ``SupportCodeError`` for a malformed scope."""
    claims = dict(verified.claims)
    return (
        _scope_claim(
            claims,
            "quota_types",
            PLATFORM_RESETTABLE_QUOTA_TYPES,
            DEFAULT_RESET_QUOTA_TYPES,
        ),
        _scope_claim(
            claims, "periods", PLATFORM_RESETTABLE_PERIODS, DEFAULT_RESET_PERIODS
        ),
    )


async def apply_platform_quota_reset(
    db: AsyncSession,
    verified: SupportCode,
    redeemed_by_user_id: uuid.UUID | None,
) -> dict:
    """Burn the verified reset code *verified* and reset the counters it names,
    in one transaction. Raises ``SupportCodeError`` for a malformed scope or a
    replay, ``QuotaClearBusyError`` when the counters are being saved - both
    before anything changes, so the code stays unused."""
    quota_types, periods = platform_quota_reset_scope(verified)
    await burn_support_code(db, verified, redeemed_by_user_id)
    # Commits the redemption with the reset, or neither.
    counters = await reset_platform_quota_usage(db, quota_types, periods)
    return {
        "code_id": verified.code_id,
        "note": verified.note,
        "quota_types": list(quota_types),
        "periods": list(periods),
        "counters": counters,
    }


def verify_redeemable_code(code: str) -> SupportCode:
    """Verify *code* against every kind this platform applies, and the scope of
    a reset code; raises ``SupportCodeError`` (a message meant for the admin).

    Nothing is read from or written to the database: whether the code was used
    already is ``burn_support_code``'s to say, in the transaction that applies
    it.
    """
    from app.enterprise.xtm_ca import XTM_CA_PEM

    verified = verify_support_code(
        code,
        XTM_CA_PEM,
        settings.platform_id or None,
        expected_kind=SUPPORTED_KINDS,
    )
    if verified.kind == PLATFORM_QUOTA_RESET_KIND:
        platform_quota_reset_scope(verified)
    return verified


def describe_grant(verified: SupportCode) -> str:
    """What a verified code grants, in one line for the audit trail."""
    if verified.kind == PLATFORM_QUOTA_RESET_KIND:
        quota_types, periods = platform_quota_reset_scope(verified)
        return (
            "Reset the platform-wide usage counters "
            f"({', '.join(quota_types)}; current {', '.join(periods)} windows) "
            f"with support code {verified.code_id}"
        )
    return f"Recharged all agent failure pools with support code {verified.code_id}"


async def apply_support_code(
    db: AsyncSession,
    verified: SupportCode,
    redeemed_by_user_id: uuid.UUID | None,
) -> dict:
    """Burn *verified* and apply what it grants, committing *db* - so anything
    the caller added to the session (its audit row) lands with the grant, or
    not at all. The result carries the code's ``kind`` next to what the grant
    reports. Raises ``SupportCodeError`` on a replay, ``QuotaClearBusyError``
    when the counters are being saved."""
    if verified.kind == PLATFORM_QUOTA_RESET_KIND:
        result = await apply_platform_quota_reset(db, verified, redeemed_by_user_id)
    else:
        result = await apply_pool_recharge(db, verified, redeemed_by_user_id)
    return {"kind": verified.kind, **result}


async def redeem_support_code(
    db: AsyncSession,
    code: str,
    redeemed_by_user_id: uuid.UUID | None,
) -> dict:
    """``verify_redeemable_code`` then ``apply_support_code``."""
    return await apply_support_code(
        db, verify_redeemable_code(code), redeemed_by_user_id
    )


__all__ = [
    "DEFAULT_RESET_PERIODS",
    "DEFAULT_RESET_QUOTA_TYPES",
    "SUPPORTED_KINDS",
    "apply_platform_quota_reset",
    "apply_support_code",
    "describe_grant",
    "platform_quota_reset_scope",
    "redeem_support_code",
    "verify_redeemable_code",
]
