"""Two verifications with one code yield exactly one session.

``_verify_second_factor`` re-reads the user under ``SELECT ... FOR UPDATE``
before it checks and consumes a one-time code, and ``mfa_verify`` commits
before issuing the session, so a second attempt with the same code waits for
the first to commit and then sees the code as spent. Row locks only exist in
a real database, so this runs against PostgreSQL, in an isolated temporary
database.

Both attempts meet at a rendezvous right after the lock would be taken, and
wait there for each other for up to a second: without the lock both arrive,
read the same unspent state and both succeed; with it the second is still
blocked on the row, so the first goes on alone after the timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pyotp
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1 import auth as auth_module
from app.models.user import User
from app.services import mfa
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

_RENDEZVOUS_TIMEOUT_SECS = 1.0


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    """Isolated database holding only the users table, dropped on exit."""
    async with scratch_database("copilot_mfa", tables=[User.__table__]) as db:
        yield db.sessions


async def _noop(*_a, **_kw) -> None:
    return None


def _rendezvous(monkeypatch) -> None:
    """Hold each attempt just past the lock until the other one arrives."""
    arrived = 0
    both_here = asyncio.Event()
    load_secret = auth_module._load_user_secret

    async def _meet_then_load(user):
        nonlocal arrived
        arrived += 1
        if arrived >= 2:
            both_here.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(both_here.wait(), _RENDEZVOUS_TIMEOUT_SECS)
        return await load_secret(user)

    monkeypatch.setattr(auth_module, "_load_user_secret", _meet_then_load)


@pytest.mark.parametrize("factor", ["totp", "recovery"])
async def test_two_verifications_with_one_code_issue_exactly_one_session(
    monkeypatch, factor
):
    monkeypatch.setattr(auth_module, "enforce_rate_limit", _noop)
    monkeypatch.setattr(auth_module, "log_audit", _noop)
    _rendezvous(monkeypatch)

    secret = mfa.generate_secret()
    plaintext, stored = mfa.generate_recovery_codes()
    code = (
        pyotp.TOTP(
            secret, digits=mfa.TOTP_DIGITS, interval=mfa.TOTP_PERIOD_SECONDS
        ).generate_otp(mfa.current_step())
        if factor == "totp"
        else plaintext[0]
    )

    async with _temp_database() as sessions:
        user_id = uuid.uuid4()
        async with sessions() as session:
            session.add(
                User(
                    id=user_id,
                    email="mfa.race@example.com",
                    full_name="MFA Race",
                    is_active=True,
                    mfa_enabled=True,
                    mfa_secret=mfa.encrypt_secret(secret),
                    mfa_recovery_codes=stored,
                )
            )
            await session.commit()

        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

        async def _attempt():
            async with sessions() as session:
                try:
                    return await auth_module.mfa_verify(
                        body=auth_module.MfaVerifyRequest(
                            mfa_token=mfa.create_challenge_token(user_id), code=code
                        ),
                        request=request,
                        db=session,
                    )
                except HTTPException as exc:
                    return exc

        outcomes = await asyncio.gather(_attempt(), _attempt())

        sessions_issued = [o for o in outcomes if not isinstance(o, HTTPException)]
        refusals = [o for o in outcomes if isinstance(o, HTTPException)]
        assert len(sessions_issued) == 1, f"one code must open one session: {outcomes}"
        assert sessions_issued[0].access_token
        assert [r.status_code for r in refusals] == [401]

        async with sessions() as session:
            stored_user = await session.get(User, user_id)
            if factor == "totp":
                assert stored_user.mfa_last_used_step is not None
            else:
                assert mfa.count_unused_recovery_codes(stored_user.mfa_recovery_codes) == (
                    mfa.RECOVERY_CODE_COUNT - 1
                )
