"""The Nova connectivity test serves the sync error per caller.

Runs the real test route, the real manage rule and the real Nova probe, as a
non-admin user and as an admin, with the integration lookup stubbed and the
probe's two queries answered by a stub session. The real-database run is in
``test_nova_sync_error_readers_postgres.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.integrations.providers.nova  # noqa: F401 - registers the Nova probe
from app.api.v1 import integrations as integrations_mod
from app.models.integration import Integration

_HIDDEN = ("payroll-repo", "git.internal.example", "hidden-marker")
_RAW = (
    "Client error '401 Unauthorized' for url 'https://git.internal.example/acme/payroll-repo/contents' (hidden-marker)"
)
_REPO = "Nova-Hunting/nova-rules"


class _ProbeSession:
    """Answers the probe: the rule source first, then the active rule count."""

    def __init__(self, source) -> None:
        self._answers = [
            SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: source)),
            SimpleNamespace(scalar_one=lambda: 12),
        ]

    async def execute(self, _statement):
        return self._answers.pop(0)

    async def commit(self) -> None:
        # The route stores the test's outcome (connection_test_status, #11837)
        # and nothing else; _test checks that is all a failed test changed.
        self.commits = getattr(self, "commits", 0) + 1


def _nova() -> Integration:
    return Integration(
        id=uuid.uuid4(),
        provider="nova",
        auth_type="none",
        name="Nova",
        status="connected",
        is_company_managed=True,
    )


def _failed_source(error: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        repo=_REPO,
        last_sync_status="error",
        last_sync_error=error,
        sync_failure_count=3,
    )


def _caller(role: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), role=role, is_admin=role == "admin")


async def _test(monkeypatch, caller, error: str | None = _RAW) -> dict:
    nova = _nova()
    monkeypatch.setattr(integrations_mod, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(integrations_mod, "get_user_integration", AsyncMock(return_value=nova))
    result = await integrations_mod.test_integration(str(nova.id), user=caller, db=_ProbeSession(_failed_source(error)))
    # A failed test changes nothing on the integration but its stored outcome.
    assert nova.connection_test_status == "error"
    assert (nova.status, nova.name, nova.is_company_managed) == ("connected", "Nova", True)
    return result


@pytest.mark.asyncio
async def test_an_admin_is_served_the_sync_error_as_before(monkeypatch):
    assert await _test(monkeypatch, _caller("admin")) == {
        "status": "error",
        "message": f"Last sync of {_REPO} failed (3 consecutive): {_RAW}",
    }


@pytest.mark.asyncio
async def test_an_admin_is_served_unknown_error_as_before(monkeypatch):
    result = await _test(monkeypatch, _caller("admin"), error=None)
    assert result["message"] == f"Last sync of {_REPO} failed (3 consecutive): unknown error"


@pytest.mark.asyncio
async def test_a_user_who_cannot_manage_the_integration_is_served_a_sentence(monkeypatch):
    result = await _test(monkeypatch, _caller("user"))

    assert result["status"] == "error"
    assert result["message"].startswith(f"Last sync of {_REPO} failed (3 consecutive): ")
    assert not any(marker in result["message"] for marker in _HIDDEN), result["message"]
    assert "manage this integration" in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, "   ", "\n"])
async def test_a_reader_is_served_the_sentence_whatever_the_stored_error(monkeypatch, error):
    result = await _test(monkeypatch, _caller("user"), error=error)

    assert result["message"].endswith("manage this integration."), result["message"]


@pytest.mark.asyncio
async def test_an_admin_is_served_a_blank_stored_error_as_before(monkeypatch):
    result = await _test(monkeypatch, _caller("admin"), error="   ")
    assert result["message"] == f"Last sync of {_REPO} failed (3 consecutive): "
