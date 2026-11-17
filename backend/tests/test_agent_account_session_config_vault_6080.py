"""An agent account's session settings read from the integration resolve a vault reference (#6080)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.core.security import encrypt_value
from app.services import vault
from app.services.agent_accounts import pipeline
from app.services.agent_accounts.providers import get_provider


def _holder(config: dict) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), extra_config_enc=encrypt_value(json.dumps(config)))


@pytest.mark.asyncio
async def test_a_referenced_setting_is_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _resolve(value, _owner):
        return "1234567890" if value == "vault://whatsapp/app#phone" else value

    monkeypatch.setattr(vault, "resolve_secret_text", _resolve)
    holder = _holder({"phone_number_id": "vault://whatsapp/app#phone", "backend": "meta_cloud_api"})
    config = await pipeline._session_config(holder, get_provider("whatsapp"))
    assert config["phone_number_id"] == "1234567890"
    assert config["backend"] == "meta_cloud_api"
    assert "vault" not in json.dumps(config)


@pytest.mark.asyncio
async def test_a_plain_setting_is_read_as_stored() -> None:
    holder = _holder({"phone_number_id": "42", "base_url": " https://gw.example "})
    config = await pipeline._session_config(holder, get_provider("whatsapp"))
    assert config["phone_number_id"] == "42"
    assert config["base_url"] == "https://gw.example"


@pytest.mark.asyncio
async def test_an_unavailable_reference_refuses_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _refuse(_value, _owner):
        raise vault.SecretUnavailableError("not_found")

    monkeypatch.setattr(vault, "resolve_secret_text", _refuse)
    holder = _holder({"phone_number_id": "vault://whatsapp/app#phone"})
    with pytest.raises(pipeline.AccountUnavailableError, match="vault") as exc:
        await pipeline._session_config(holder, get_provider("whatsapp"))
    assert "whatsapp/app" not in str(exc.value)


@pytest.mark.asyncio
async def test_no_settings_asked_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(*_args, **_kwargs):
        raise AssertionError("read")

    monkeypatch.setattr(vault, "reveal_json_field", _boom)
    provider = SimpleNamespace(session_config_keys=())
    assert await pipeline._session_config(_holder({"a": "b"}), provider) == {}
