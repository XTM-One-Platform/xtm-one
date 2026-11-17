"""Test Connection reads the same row the pre-flight reads — including the circuit."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.a2a.client import probe
from app.integrations.connectivity import ProbeContext


def _target(**kw):
    base = dict(
        usability="usable",
        unusable_reason=None,
        circuit_open_until=None,
        declared_name="Triage",
        card_url="https://p.example/c.json",
        agreed_protocol_version="1.0",
        signature_state="unsigned",
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def rig(monkeypatch):
    import app.a2a.providers as providers
    from tests.a2a_chat_gate import stub_chat_gate

    state = {"targets": []}

    async def _settings(_db, _id):
        return SimpleNamespace(direction="outbound")

    async def _targets(_db, _id):
        return state["targets"]

    monkeypatch.setattr(providers, "get_settings", _settings)
    monkeypatch.setattr(providers, "outbound_targets", _targets)
    stub_chat_gate(monkeypatch)
    state["ctx"] = ProbeContext(
        provider="oauth_a2a",
        name="Partner",
        token=None,
        extra={},
        db=object(),
        integration=SimpleNamespace(id=uuid.uuid4()),
    )
    return state


class TestThePauseIsReadFromTheBreaker:
    async def test_an_expired_pause_is_not_reported(self, rig):
        # The column is cleared only by a success. Read raw, a partner that
        # blipped once stayed "paused" until a delegation happened to succeed.
        rig["targets"] = [
            _target(circuit_open_until=datetime.now(UTC) - timedelta(seconds=1))
        ]
        result = await probe.outbound_probe(rig["ctx"])
        assert result.status == "ok"

    async def test_an_open_pause_is_degraded(self, rig):
        rig["targets"] = [
            _target(circuit_open_until=datetime.now(UTC) + timedelta(seconds=60))
        ]
        result = await probe.outbound_probe(rig["ctx"])
        assert result.status == "degraded"
        assert "paused" in result.message

    async def test_one_paused_beside_one_working_is_degraded_naming_it(self, rig):
        rig["targets"] = [
            _target(declared_name="Fine"),
            _target(
                declared_name="Slow",
                circuit_open_until=datetime.now(UTC) + timedelta(seconds=60),
            ),
        ]
        result = await probe.outbound_probe(rig["ctx"])
        assert result.status == "degraded"
        assert result.message.startswith("1 of 2 reachable")
        assert "Slow (paused)" in result.message

    async def test_every_agent_unusable_is_failed(self, rig):
        rig["targets"] = [
            _target(usability="unusable", unusable_reason="card_unfetchable"),
            _target(usability="unusable", unusable_reason="card_malformed"),
        ]
        result = await probe.outbound_probe(rig["ctx"])
        assert result.status == "error"
        assert "None of 2" in result.message
