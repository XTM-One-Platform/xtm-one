"""Every delegation attempt is recorded, and attributed to the right partner.

FR-040, FR-041, FR-042, FR-044 / SC-013. The property that matters is not
"delegations are logged" but that attribution keys on what was recorded at
SUBMISSION time. Two registrations may be bound to one agent, and deriving
attribution from an agent's current bindings would make each report the
other's traffic.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.a2a.client import delegation
from app.a2a.client.delegation import CallingRun
from tests.a2a_client_stubs import (
    RecordingDb,
    client_for,
    integration_row,
    rpc,
    settings_row,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from tests.a2a_chat_gate import stub_chat_gate

    stub_chat_gate(monkeypatch)
    monkeypatch.setattr(delegation, "verdict_is_stale", lambda _s: False)

    async def _headers(_i, _c):
        return {"Authorization": "Bearer minted"}

    monkeypatch.setattr(delegation, "outbound_headers", _headers)


def _calling():
    return CallingRun(agent_id=uuid.uuid4(), user_id=uuid.uuid4(), run_ref=uuid.uuid4())


class TestEveryAttemptIsRecorded:
    @pytest.mark.asyncio
    async def test_a_success_is_recorded_with_its_partner(self, monkeypatch):
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            client_for(lambda _r: rpc({"id": "t", "status": {"state": "completed"}})),
        )
        db, integration, calling = RecordingDb(), integration_row(), _calling()
        await delegation.delegate(
            db, integration, settings_row(), message="x", calling=calling
        )
        assert len(db.added) == 1
        row = db.added[0]
        # FR-042: the partner is recorded HERE, at submission, not inferred
        # from the agent's bindings later.
        assert row.integration_id == integration.id
        assert row.agent_id == calling.agent_id
        assert row.user_id == calling.user_id
        assert row.run_ref == calling.run_ref

    @pytest.mark.asyncio
    async def test_a_preflight_refusal_is_recorded_too(self, monkeypatch):
        # "We refused to send this" is a fact an operator investigating a
        # silent agent needs as much as a failure at the partner.
        monkeypatch.setattr(httpx, "AsyncClient", client_for(lambda _r: rpc({})))
        db = RecordingDb()
        await delegation.delegate(
            db,
            integration_row(),
            settings_row(usability="unusable", unusable_reason="signature_invalid"),
            message="x",
            calling=_calling(),
        )
        assert len(db.added) == 1
        assert db.added[0].outcome == "refused_preflight"

    @pytest.mark.asyncio
    async def test_a_failure_is_recorded_with_its_kind(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        db = RecordingDb()
        await delegation.delegate(
            db, integration_row(), settings_row(), message="x", calling=_calling()
        )
        assert db.added[0].outcome == "unreachable"
        assert db.added[0].failure_kind == "transient"


class TestWhatIsRecorded:
    @pytest.mark.asyncio
    async def test_the_version_and_provenance_of_this_exchange(self, monkeypatch):
        # Recorded rather than looked up later: the registration's agreed
        # version may since have renegotiated, and its signature state may
        # have changed, but THIS delegation ran under these.
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            client_for(lambda _r: rpc({"id": "t", "status": {"state": "completed"}})),
        )
        db = RecordingDb()
        await delegation.delegate(
            db,
            integration_row(),
            settings_row(signature_state="verified"),
            message="x",
            calling=_calling(),
        )
        assert db.added[0].protocol_version == "1.0"
        assert db.added[0].signature_state == "verified"

    @pytest.mark.asyncio
    async def test_no_payload_is_stored(self, monkeypatch):
        # Copying the message and result here would create a second store of
        # partner-exchanged content with its own retention question.
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            client_for(
                lambda _r: rpc(
                    {
                        "id": "t",
                        "status": {"state": "completed"},
                        "artifacts": [{"parts": [{"text": "sensitive verdict"}]}],
                    }
                )
            ),
        )
        db = RecordingDb()
        await delegation.delegate(
            db, integration_row(), settings_row(), message="secret instruction", calling=_calling()
        )
        row = db.added[0]
        for value in vars(row).values():
            if isinstance(value, str):
                assert "secret instruction" not in value
                assert "sensitive verdict" not in value


class TestBookkeepingIsBestEffort:
    @pytest.mark.asyncio
    async def test_a_recording_failure_does_not_fail_the_delegation(self, monkeypatch):
        # A delegation that succeeded must not surface to the calling agent as
        # broken because a row could not be written.
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            client_for(lambda _r: rpc({"id": "t", "status": {"state": "completed"}})),
        )
        result = await delegation.delegate(
            RecordingDb(fail=True),
            integration_row(),
            settings_row(),
            message="x",
            calling=_calling(),
        )
        assert result.succeeded

    @pytest.mark.asyncio
    async def test_no_calling_context_records_nothing_rather_than_guessing(
        self, monkeypatch
    ):
        # Attribution must never be invented. A delegation with no caller
        # recorded is a gap someone can see, where a guessed one is a lie
        # nobody can.
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            client_for(lambda _r: rpc({"id": "t", "status": {"state": "completed"}})),
        )
        db = RecordingDb()
        await delegation.delegate(db, integration_row(), settings_row(), message="x")
        assert db.added == []


class TestTwoPartnersOnOneAgent:
    @pytest.mark.asyncio
    async def test_each_records_against_its_own_registration(self, monkeypatch):
        # SC-013's measurement, and the exact case that made FR-042 explicit.
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            client_for(lambda _r: rpc({"id": "t", "status": {"state": "completed"}})),
        )
        db = RecordingDb()
        agent_id, user_id = uuid.uuid4(), uuid.uuid4()
        first, second = integration_row(), integration_row("entra_a2a")
        for integration in (first, second):
            await delegation.delegate(
                db,
                integration,
                settings_row(),
                message="x",
                calling=CallingRun(agent_id=agent_id, user_id=user_id),
            )
        recorded = [r.integration_id for r in db.added]
        assert recorded == [first.id, second.id]
        assert len(set(recorded)) == 2
