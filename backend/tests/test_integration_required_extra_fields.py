"""A write that would leave an integration unable to work is refused (#9898, S2).

The catalog's ``extra_field_required_when`` names, per provider, the extra-config
keys its credential cannot be built without (a WhatsApp integration on the Cloud
API backend needs ``phone_number_id``; FortiSIEM needs ``base_url``). The REST
create / update routes and the agent builder tools refuse a write that would
newly leave one missing, the key named, and nothing is written.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import integrations as integrations_api
from app.api.v1.integration_catalog import (
    PROVIDER_CATALOG,
    missing_required_extra_fields,
    missing_required_extra_fields_message,
    newly_missing_required_extra_fields,
)
from app.api.v1.integrations import required_extra_refusal
from app.core.security import encrypt_value

GATEWAY = {"backend": "linked_device_gateway", "gateway_vendor": "whapi", "base_url": "https://gate.test"}
CLOUD = {"backend": "meta_cloud_api", "phone_number_id": "123456"}


def _integration(provider: str, extra: dict) -> SimpleNamespace:
    return SimpleNamespace(provider=provider, extra_config_enc=encrypt_value(json.dumps(extra)))


def test_the_catalog_declares_the_required_keys_of_whatsapp_and_fortisiem():
    assert PROVIDER_CATALOG["whatsapp"]["extra_field_required_when"] == {
        "phone_number_id": {"backend": ["", "meta_cloud_api"]}
    }
    assert PROVIDER_CATALOG["fortisiem"]["extra_field_required_when"] == {"base_url": {}}


def test_every_declared_rule_names_extra_fields_of_its_provider():
    for provider, entry in PROVIDER_CATALOG.items():
        rules = entry.get("extra_field_required_when") or {}
        fields = set(entry.get("extra_fields") or {})
        for field, condition in rules.items():
            assert field in fields, (provider, field)
            assert set(condition or {}) <= fields, (provider, condition)


@pytest.mark.parametrize(
    ("extra", "missing"),
    [
        (GATEWAY, []),
        (CLOUD, []),
        ({"backend": "meta_cloud_api"}, ["phone_number_id"]),
        ({"backend": "meta_cloud_api", "phone_number_id": "  "}, ["phone_number_id"]),
        # No backend is the Cloud API, as the credential builder reads it.
        ({"gateway_vendor": "whapi"}, ["phone_number_id"]),
        ({}, ["phone_number_id"]),
    ],
)
def test_whatsapp_needs_the_phone_number_id_on_the_cloud_api_backend_only(extra, missing):
    assert missing_required_extra_fields("whatsapp", extra) == missing


def test_an_unconditional_rule_requires_the_key_always():
    assert missing_required_extra_fields("fortisiem", {"username": "api"}) == ["base_url"]
    assert missing_required_extra_fields("fortisiem", {"base_url": "https://f.test"}) == []


def test_a_provider_without_rules_requires_nothing():
    assert missing_required_extra_fields("pagerduty", {}) == []
    assert missing_required_extra_fields("no-such-provider", None) == []


def test_dropping_the_gateway_backend_is_refused_with_the_key_named():
    # The 7 Nov incident: the gateway integration lost ``backend`` and resolved
    # as a Cloud API one without a phone number id.
    integration = _integration("whatsapp", GATEWAY)
    merged = {key: value for key, value in GATEWAY.items() if key != "backend"}

    problem = required_extra_refusal(integration, merged)

    assert problem is not None
    assert "'phone_number_id'" in problem
    assert "nothing was saved" in problem


def test_removing_a_required_key_is_refused():
    integration = _integration("whatsapp", CLOUD)

    assert required_extra_refusal(integration, {"backend": "meta_cloud_api"}) is not None


def test_switching_to_the_gateway_needs_nothing_more():
    integration = _integration("whatsapp", CLOUD)

    assert required_extra_refusal(integration, {**CLOUD, **GATEWAY}) is None


def test_a_key_already_missing_before_the_edit_is_not_held_against_it():
    # An integration saved before the rule existed stays editable.
    integration = _integration("whatsapp", {"backend": "meta_cloud_api"})

    assert required_extra_refusal(integration, {"backend": "meta_cloud_api", "waba_id": "9"}) is None
    assert newly_missing_required_extra_fields("whatsapp", {}, {}) == []


def test_the_refusal_message_names_every_missing_key():
    message = missing_required_extra_fields_message("whatsapp", ["a", "b"])

    assert "'a', 'b'" in message
    assert message.startswith("The whatsapp integration needs")


def test_the_rest_update_refuses_before_writing():
    integration = _integration("whatsapp", GATEWAY)
    stored = integration.extra_config_enc

    with pytest.raises(HTTPException) as exc:
        integrations_api._refuse_newly_missing_required_extra(integration, {"base_url": "https://gate.test"})

    assert exc.value.status_code == 400
    assert "phone_number_id" in exc.value.detail
    assert integration.extra_config_enc == stored
