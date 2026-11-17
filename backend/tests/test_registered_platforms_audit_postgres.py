"""The security audit's registration check, against a real PostgreSQL.

``_check_registered_platforms`` names the active registrations Settings cannot
show, and it counts and lists them in SQL: an install polluted through earlier
releases may hold any number of them, each with a URL of any length, so the
audit reads a bounded amount whatever the table holds. A session double
answers whatever it is asked, so the filter, the count and the cut are checked
here, on the statements the database runs.
"""

from __future__ import annotations

import pytest

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import security_audit
from app.models.platform_registration import PlatformRegistration
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


def _registration(identifier: str, url: str, status: str = "active") -> PlatformRegistration:
    return PlatformRegistration(
        platform_identifier=identifier,
        platform_url=url,
        platform_title=identifier,
        platform_version="",
        status=status,
    )


async def _findings(registrations: list[PlatformRegistration]) -> list[security_audit.AuditFinding]:
    async with scratch_database(
        "regaudit", tables=ALL_TABLES, extensions=("vector",), refuse_other_databases=True
    ) as db:
        async with db.sessions() as session:
            session.add_all(registrations)
            await session.commit()
        findings: list[security_audit.AuditFinding] = []
        async with db.sessions() as session:
            await security_audit._check_registered_platforms(session, findings)
    return findings


async def test_the_database_counts_lists_and_cuts_the_unknown_registrations():
    long_url = "https://attacker.example/" + "a" * 5000
    findings = await _findings(
        [
            _registration("opencti", "https://opencti.example.com"),
            _registration("opengrc", "https://opengrc.example.com"),
            _registration("rogue-gone", "https://gone.example", status="unregistered"),
            *(_registration(f"rogue-{index:02d}", long_url) for index in range(14)),
        ]
    )

    [finding] = findings
    assert (finding.id, finding.severity) == ("authentication.registered_platform_unknown", "critical")
    assert "14 active platform registrations" in finding.description
    assert finding.description.count("'rogue-") == security_audit._LISTED_REGISTRATIONS
    assert f"and {14 - security_audit._LISTED_REGISTRATIONS} more" in finding.description
    assert long_url[: security_audit._LISTED_URL_CHARS - 3] + "..." in finding.description
    assert long_url not in finding.description
    for listed_elsewhere in ("opencti.example.com", "opengrc.example.com", "rogue-gone"):
        assert listed_elsewhere not in finding.description


async def test_the_database_finds_every_active_registration_listed_by_settings():
    findings = await _findings(
        [
            _registration("opencti", "https://opencti.example.com"),
            _registration("openbas", "https://openbas.example.com"),
            _registration("rogue-gone", "https://gone.example", status="unregistered"),
        ]
    )

    [finding] = findings
    assert (finding.id, finding.severity) == ("authentication.registered_platforms_listed", "pass")
    assert "(2 registration(s))" in finding.title
