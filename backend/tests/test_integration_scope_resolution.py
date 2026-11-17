"""Regression coverage for ``resolve_integration_by_scope`` ownership preference.

Under the always-set-owner invariant company-managed integrations keep
a non-null ``user_id`` owner, so ``user_id is not None`` no longer distinguishes
a user's PRIVATE integration from a shared (company-managed) one. The resolver
must prefer genuinely private rows (``not is_company_managed``) before falling
back to the shared ones — otherwise a shared integration can be auto-picked
ahead of the user's private connection and bind the wrong credentials.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.integration_tools import resolve_integration_by_scope


def _integ(*, is_company_managed: bool, user_id=None):
    # ``scope_config=None`` keeps the resolver on the unscoped auto-pick path.
    return SimpleNamespace(
        id=uuid.uuid4(),
        scope_config=None,
        user_id=user_id if user_id is not None else uuid.uuid4(),
        is_company_managed=is_company_managed,
    )


@pytest.mark.asyncio
async def test_resolve_prefers_private_over_company_managed():
    """A private integration wins even when a company-managed one (also
    non-null ``user_id`` now) is listed first."""
    company = _integ(is_company_managed=True)
    private = _integ(is_company_managed=False)

    result = await resolve_integration_by_scope(
        None, "opencti", [company, private], None, acting_user_id=private.user_id
    )
    assert result is private


@pytest.mark.asyncio
async def test_resolve_falls_back_to_company_managed_when_no_private():
    """With only company-managed candidates, the shared one is the fallback."""
    company1 = _integ(is_company_managed=True)
    company2 = _integ(is_company_managed=True)

    result = await resolve_integration_by_scope(
        None, "opencti", [company1, company2], None, acting_user_id=None
    )
    assert result is company1


@pytest.mark.asyncio
async def test_resolve_single_candidate_short_circuits():
    """A single company-wide candidate is returned, even with no acting user."""
    company = _integ(is_company_managed=True)
    result = await resolve_integration_by_scope(
        None, "opencti", [company], None, acting_user_id=None
    )
    assert result is company
