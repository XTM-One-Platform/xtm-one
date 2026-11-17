"""An integration update merges ``extra_config`` key by key (#9898).

An update that sets one key never drops the others: the WhatsApp integration
on the linked-device gateway lost its ``backend`` selector to an update of one
key, resolved as a Cloud API integration without a phone number id, and every
tool of it disappeared from every turn with no error.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.api.v1 import integrations as integrations_api
from app.api.v1.integrations import (
    EXTRA_CONFIG_MASK,
    merge_extra_config_values,
    merged_extra_config,
)
from app.core.security import encrypt_value


def _integration(provider: str, extra: dict) -> SimpleNamespace:
    return SimpleNamespace(provider=provider, extra_config_enc=encrypt_value(json.dumps(extra)))


@pytest.fixture(autouse=True)
def _no_custom_schema(monkeypatch):
    async def _none(_db, _provider):
        return frozenset()

    monkeypatch.setattr(integrations_api, "_custom_secret_config_keys", _none)


GATEWAY = {
    "backend": "linked_device_gateway",
    "gateway_vendor": "evolution",
    "base_url": "https://gateway.test",
    "allowed_senders": "+33100000000",
}


async def test_setting_one_key_on_a_gateway_whatsapp_integration_keeps_every_other_key():
    merged = await merged_extra_config(None, _integration("whatsapp", GATEWAY), {"allowed_senders": "+33199999999"})

    assert merged == {**GATEWAY, "allowed_senders": "+33199999999"}
    assert merged["backend"] == "linked_device_gateway"


async def test_setting_one_key_on_another_provider_keeps_every_other_key():
    stored = {"region": "eu", "from_email": "ops@example.test"}

    merged = await merged_extra_config(None, _integration("pagerduty", stored), {"region": "us"})

    assert merged == {"region": "us", "from_email": "ops@example.test"}


async def test_a_key_sent_as_null_is_removed_and_only_that_key():
    merged = await merged_extra_config(None, _integration("whatsapp", GATEWAY), {"allowed_senders": None})

    assert "allowed_senders" not in merged
    assert merged["backend"] == "linked_device_gateway"
    assert merged["base_url"] == "https://gateway.test"


async def test_a_secret_sent_back_as_the_mask_keeps_its_stored_value():
    stored = {"base_url": "https://x.test", "webhook_secret": "s3cret"}

    merged = await merged_extra_config(
        None, _integration("pagerduty", stored), {"webhook_secret": EXTRA_CONFIG_MASK, "base_url": "https://y.test"}
    )

    assert merged == {"base_url": "https://y.test", "webhook_secret": "s3cret"}


async def test_a_mask_for_a_secret_never_stored_stores_nothing():
    merged = await merged_extra_config(None, _integration("pagerduty", {}), {"api_token": EXTRA_CONFIG_MASK})

    assert merged == {}


def test_a_nested_object_is_merged_one_level_down_and_null_removes_inside_it():
    stored = {"options": {"a": 1, "b": 2}, "keep": True}

    merged = merge_extra_config_values(stored, {"options": {"b": None, "c": 3}})

    assert merged == {"options": {"a": 1, "c": 3}, "keep": True}


def test_a_list_or_a_scalar_replaces_the_stored_value():
    stored = {"urls": ["https://a.test"], "mode": "x"}

    merged = merge_extra_config_values(stored, {"urls": ["https://b.test"], "mode": "y"})

    assert merged == {"urls": ["https://b.test"], "mode": "y"}


def test_the_stored_config_is_never_mutated():
    stored = {"options": {"a": 1}}

    merge_extra_config_values(stored, {"options": {"a": 2}})

    assert stored == {"options": {"a": 1}}


async def test_an_unreadable_stored_config_is_merged_from_nothing():
    integration = SimpleNamespace(provider="pagerduty", extra_config_enc="not-a-ciphertext")

    assert await merged_extra_config(None, integration, {"region": "eu"}) == {"region": "eu"}
