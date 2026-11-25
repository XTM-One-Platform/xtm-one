"""Trigger provisioning: the receiver secret of a platform-managed integration and
the provision block checked at save time (services/trigger_provisioning.py, #970)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.security import decrypt_json, encrypt_value
from app.services import opencti_trigger_provisioning as prov
from app.services.trigger_provisioning import provision_spec_error, receiver_secret

PIR_CONFIG: dict[str, Any] = {
    "provider": "opencti",
    "event": "create",
    "provision": {
        "fleet": "Threat response",
        "purpose": "PIR flags",
        "mode": "pir_playbook",
        "organization_context": ["pir_ids"],
    },
}


def _integration(auth_type: str, extra: dict[str, Any] | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="opencti",
        auth_type=auth_type,
        extra_config_enc=encrypt_value(json.dumps(extra)) if extra is not None else None,
    )


async def test_a_platform_managed_integration_gets_a_stored_secret_once() -> None:
    integration = _integration("platform_managed", {"base_url": "https://octi.example"})
    first = await receiver_secret(integration)
    assert first and len(first) >= 32
    stored = decrypt_json(integration.extra_config_enc)
    assert stored == {"base_url": "https://octi.example", "webhook_secret": first}
    # The next pass reads the stored secret, never a new one.
    assert await receiver_secret(integration) == first


async def test_a_platform_managed_integration_without_config_gets_one_too() -> None:
    integration = _integration("platform_managed", None)
    secret = await receiver_secret(integration)
    assert secret
    assert decrypt_json(integration.extra_config_enc) == {"webhook_secret": secret}


async def test_an_admin_secret_is_kept_and_a_static_key_integration_gets_none() -> None:
    managed = _integration("platform_managed", {"webhook_secret": "admin-set"})
    assert await receiver_secret(managed) == "admin-set"
    static = _integration("api_key", {"base_url": "https://octi.example"})
    before = static.extra_config_enc
    assert await receiver_secret(static) is None
    assert static.extra_config_enc == before


def test_the_object_form_of_organization_context_is_refused_with_the_accepted_form() -> None:
    wrong = {**PIR_CONFIG, "provision": {**PIR_CONFIG["provision"], "organization_context": {"pir_ids": ["p1"]}}}
    error = provision_spec_error("integration_event", wrong)
    assert error is not None
    assert error.startswith("Invalid trigger_config: provision.organization_context must be a list")
    assert '["pir_ids"]' in error
    with pytest.raises(prov.ProvisionSpecError, match="profile field names"):
        prov.read_provision_spec(wrong)


def test_a_usable_block_and_other_triggers_pass_the_save_check() -> None:
    assert provision_spec_error("integration_event", PIR_CONFIG) is None
    assert provision_spec_error("integration_event", {"provider": "opencti", "event": "create"}) is None
    assert provision_spec_error("schedule", {"cron": "0 9 * * *"}) is None
    assert provision_spec_error("integration_event", {"provider": "microsoft", "event": "new_email"}) is None


def test_a_wrong_openaev_block_is_refused_at_save_time() -> None:
    error = provision_spec_error("integration_event", {"provider": "openaev", "provision": {"fleet": "F"}})
    assert error is not None and "provision.fleet and provision.purpose are required" in error


def test_a_refusal_is_a_sentence_the_save_path_can_follow() -> None:
    """#2195: callers append "Nothing was saved."; the refusal ends with its own full stop, once."""
    error = provision_spec_error("integration_event", {"provider": "openaev", "provision": {"fleet": "F"}})
    assert error == "Invalid trigger_config: provision.fleet and provision.purpose are required."
    assert not error.endswith("..")
