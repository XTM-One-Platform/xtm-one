"""The instance identity leaves the platform only on an explicit opt-in (#708).

FR-032 keeps an outbound delegation to the composed text. An operator may opt
one A2A integration in to telling the partner which instance is calling: then,
and only then, every message carries ``metadata.xtmOneInstance`` with the
Instance ID and the display name, nothing else. Captured at the wire, like the
payload boundary tests, because the question is what actually left.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app.a2a.client import credentials, delegation
from app.a2a.protocol import (
    DIALECT_JSONRPC_V03,
    DIALECT_PROTO_V03,
    DIALECT_PROTO_V10,
    INSTANCE_METADATA_KEY,
    instance_metadata,
    outbound_message,
)
from tests.a2a_client_stubs import (
    RecordingDb,
    client_for,
    integration_row,
    rpc,
    settings_row,
)

INSTANCE_ID = "6f1c2a7e-0d3b-4c55-9e1a-2b7d8c9e0f11"
PLATFORM_NAME = "XTM One Test"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from tests.a2a_chat_gate import stub_chat_gate

    stub_chat_gate(monkeypatch)
    monkeypatch.setattr(delegation, "verdict_is_stale", lambda _s: False)

    async def _headers(_i, _c):
        return {"Authorization": "Bearer partner-scoped-minted"}

    monkeypatch.setattr(delegation, "outbound_headers", _headers)

    async def _identity(_db):
        return instance_metadata(INSTANCE_ID, PLATFORM_NAME)

    monkeypatch.setattr(delegation, "_instance_identity", _identity)


@pytest.fixture
def captured(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return rpc({"id": "t", "status": {"state": "completed"}})

    monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
    return seen


def _opted(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(delegation, "sends_instance_identity", lambda _i: value)


class TestTheWire:
    @pytest.mark.asyncio
    async def test_off_by_default_the_message_carries_no_metadata(self, captured):
        # No stub of the opt-in: the real reader sees no extra config at all.
        await delegation.delegate(RecordingDb(), integration_row(), settings_row(), message="triage alert 42")
        body = json.loads(captured[0].content)
        assert "metadata" not in body["params"]["message"]
        assert set(body["params"]) <= {"message", "taskId"}
        raw = captured[0].content.decode()
        assert INSTANCE_ID not in raw and PLATFORM_NAME not in raw

    @pytest.mark.asyncio
    async def test_opted_out_sends_nothing_more(self, captured, monkeypatch):
        _opted(monkeypatch, False)
        await delegation.delegate(RecordingDb(), integration_row(), settings_row(), message="x")
        assert "metadata" not in json.loads(captured[0].content)["params"]["message"]

    @pytest.mark.asyncio
    async def test_opted_in_sends_exactly_the_instance_identity(self, captured, monkeypatch):
        _opted(monkeypatch, True)
        await delegation.delegate(RecordingDb(), integration_row(), settings_row(), message="triage alert 42")
        message = json.loads(captured[0].content)["params"]["message"]
        assert message["metadata"] == {
            INSTANCE_METADATA_KEY: {"instanceId": INSTANCE_ID, "platformName": PLATFORM_NAME}
        }
        # The text is unchanged, and the params still hold nothing else.
        assert message["parts"] == [{"text": "triage alert 42"}]
        assert set(json.loads(captured[0].content)["params"]) <= {"message", "taskId"}
        raw = captured[0].content.decode()
        for forbidden in ("Bearer", "client_secret", "conversation", "history"):
            assert forbidden not in raw, forbidden

    @pytest.mark.asyncio
    async def test_a_continuation_carries_it_too(self, captured, monkeypatch):
        _opted(monkeypatch, True)

        async def _owned(*_a, **_k):
            return True

        monkeypatch.setattr(delegation, "_may_continue", _owned)
        await delegation.delegate(
            RecordingDb(),
            integration_row(),
            settings_row(),
            message="the tenant is contoso",
            continue_task_id="task-9",
        )
        message = json.loads(captured[0].content)["params"]["message"]
        assert message["taskId"] == "task-9"
        assert message["metadata"][INSTANCE_METADATA_KEY]["instanceId"] == INSTANCE_ID

    @pytest.mark.asyncio
    async def test_an_unreadable_identity_sends_the_text_alone(self, captured, monkeypatch):
        _opted(monkeypatch, True)

        async def _none(_db):
            return None

        monkeypatch.setattr(delegation, "_instance_identity", _none)
        await delegation.delegate(RecordingDb(), integration_row(), settings_row(), message="x")
        assert "metadata" not in json.loads(captured[0].content)["params"]["message"]


class TestTheMessageShape:
    @pytest.mark.parametrize("dialect", [DIALECT_JSONRPC_V03, DIALECT_PROTO_V03, DIALECT_PROTO_V10])
    def test_every_dialect_spells_metadata_on_the_message(self, dialect):
        meta = instance_metadata(INSTANCE_ID, PLATFORM_NAME)
        message = outbound_message("x", dialect=dialect, message_id="m-1", metadata=meta)
        assert message["metadata"] == meta
        bare = outbound_message("x", dialect=dialect, message_id="m-1")
        assert "metadata" not in bare

    def test_the_identity_names_who_and_nothing_else(self):
        meta = instance_metadata(INSTANCE_ID, PLATFORM_NAME)
        assert set(meta) == {INSTANCE_METADATA_KEY}
        assert set(meta[INSTANCE_METADATA_KEY]) == {"instanceId", "platformName"}


class TestTheOptIn:
    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            (None, False),
            ({}, False),
            ({"send_instance_identity": "off"}, False),
            ({"send_instance_identity": ""}, False),
            ({"send_instance_identity": "yes"}, False),
            ({"send_instance_identity": True}, False),
            ({"send_instance_identity": "on"}, True),
            ({"send_instance_identity": " ON "}, True),
        ],
    )
    def test_only_an_explicit_on_opts_in(self, monkeypatch, stored, expected):
        monkeypatch.setattr(credentials, "_stored_config", lambda _i: stored or {})
        integration = SimpleNamespace(id="i-1", extra_config_enc="enc" if stored is not None else None)
        assert credentials.sends_instance_identity(integration) is expected

    def test_every_outbound_a2a_provider_offers_the_choice_off_first(self):
        from app.a2a.providers import A2A_PROVIDERS
        from app.api.v1.integration_catalog import PROVIDER_CATALOG

        for provider in A2A_PROVIDERS:
            entry = PROVIDER_CATALOG[provider]
            outbound = entry["variants"]["outbound"]["extra_fields"]
            assert credentials.SEND_INSTANCE_IDENTITY_FIELD in outbound, provider
            options = entry["extra_field_options"][credentials.SEND_INSTANCE_IDENTITY_FIELD]
            assert [o["value"] for o in options] == [
                credentials.SEND_INSTANCE_IDENTITY_OFF,
                credentials.SEND_INSTANCE_IDENTITY_ON,
            ], provider
            # Never on the inbound side: it concerns what this platform sends.
            assert credentials.SEND_INSTANCE_IDENTITY_FIELD not in (entry.get("extra_fields") or {})
