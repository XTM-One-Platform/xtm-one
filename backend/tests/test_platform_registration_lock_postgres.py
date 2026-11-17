"""Concurrent register calls of one platform, against a real PostgreSQL.

``register_platform`` reads the platform's registration, decides from it what
the call may record - on the published default token, what it keeps whatever
the call sends - and writes the row back. Two calls of the same platform that
interleave let the later commit write back a row read before the earlier one
changed it: the stored license is re-encrypted on every heartbeat, so a
heartbeat that read the registration before a renewal landed writes the old
certificate over it. What is under test is the lock that serializes the two,
so the database is real: a session double serializes nothing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import helpers as helpers_api
from app.api.v1 import platform as platform_api
from app.core.config import settings
from app.core.registration_token import PUBLISHED_DEFAULT_REGISTRATION_TOKEN
from app.core.security import decrypt_value, encrypt_value
from app.models.platform_registration import PlatformRegistration
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_URL = "https://opencti.example.com"
_STORED = "the license stored before the renewal"
_RENEWED = "the renewed license"
# What the heartbeat carrying the renewal sends as its license type.
_RENEWAL = "renewal"


@pytest.fixture
def published_default(monkeypatch):
    """Everything around the registration stubbed; the license decision reduced to its outcome.

    The heartbeat carrying the renewal records it; any other keeps the license
    it read, as ``_registered_license`` does on the published default.
    """

    async def _nothing(*_args, **_kwargs) -> None:
        return None

    async def _load_ps(_db) -> SimpleNamespace:
        return SimpleNamespace(enterprise_license=None, created_at=datetime.now(UTC))

    def _registered_license(reg, sent, _sent_info):
        if sent.license_type == _RENEWAL:
            return platform_api._RegistrationLicense(reg.platform_id, True, "standard", _RENEWED)
        return platform_api._RegistrationLicense(
            reg.platform_id, reg.license_validated, reg.license_type, decrypt_value(reg.enterprise_license_pem_enc)
        )

    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    monkeypatch.setattr(settings, "enterprise_license", "")
    monkeypatch.setattr(platform_api, "enforce_rate_limit", _nothing)
    monkeypatch.setattr(platform_api, "refund_rate_limit", _nothing)
    monkeypatch.setattr(platform_api, "_load_ps", _load_ps)
    monkeypatch.setattr(platform_api, "_registered_license", _registered_license)
    monkeypatch.setattr(helpers_api, "resolve_company_managed_owner_id", _nothing)


async def test_a_heartbeat_never_writes_back_a_registration_it_read_before_a_renewal(
    published_default, monkeypatch
):
    renewal_read = asyncio.Event()
    stale_read = asyncio.Event()
    release_renewal = asyncio.Event()
    renewal_done = asyncio.Event()
    refuse = platform_api._refuse_published_default_change

    # Called right after the registration is read, inside the call's
    # transaction: the renewal holds its transaction open there until released,
    # and the other heartbeat writes nothing before the renewal has returned.
    async def _after_read(request, reg=None, platform_url=None):
        await refuse(request, reg, platform_url)
        if request.caller == "renewal":
            renewal_read.set()
            await release_renewal.wait()
        else:
            stale_read.set()
            await renewal_done.wait()

    monkeypatch.setattr(platform_api, "_refuse_published_default_change", _after_read)

    async with scratch_database(
        "platformreg", tables=ALL_TABLES, extensions=("vector",), refuse_other_databases=True
    ) as db:
        async with db.sessions() as session:
            session.add(
                PlatformRegistration(
                    platform_identifier="opencti",
                    platform_url=_URL,
                    platform_title="OpenCTI",
                    platform_version="",
                    platform_id="instance-1",
                    contract="EE",
                    license_type="standard",
                    license_validated=True,
                    enterprise_license_pem_enc=encrypt_value(_STORED),
                    status="active",
                )
            )
            await session.commit()

        async def _heartbeat(caller: str, license_type: str | None) -> None:
            async with db.sessions() as session:
                await platform_api.register_platform(
                    platform_api.RegisterRequest(
                        platform_identifier="opencti", platform_url=_URL, license_type=license_type
                    ),
                    SimpleNamespace(caller=caller),
                    session,
                )

        tasks: list[asyncio.Task] = []
        try:
            tasks.append(asyncio.create_task(_heartbeat("renewal", _RENEWAL)))
            await asyncio.wait_for(renewal_read.wait(), 30)
            tasks.append(asyncio.create_task(_heartbeat("stale", None)))
            # Unserialized, the other heartbeat reads the registration at once;
            # serialized, it cannot before the renewal commits.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(stale_read.wait(), 2)
            release_renewal.set()
            await asyncio.wait_for(tasks[0], 30)
            renewal_done.set()
            await asyncio.wait_for(tasks[1], 30)
        finally:
            release_renewal.set()
            renewal_done.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 30)

        async with db.sessions() as session:
            row = (await session.execute(select(PlatformRegistration))).scalar_one()

    assert (row.license_validated, decrypt_value(row.enterprise_license_pem_enc)) == (True, _RENEWED)
