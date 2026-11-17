"""A platform re-registration keeps the extra-config keys an admin set."""

import json

from app.api.v1.platform import _keep_admin_extra_fields
from app.core.security import encrypt_value

REGISTERED = json.dumps({"base_url": "https://new.test"})


def _stored(**extra) -> str:
    return encrypt_value(json.dumps(extra))


def test_the_openaev_tenant_survives_a_re_registration():
    stored = _stored(base_url="https://old.test", tenant_id="t-b", stray="x")
    merged = json.loads(_keep_admin_extra_fields("openaev", stored, REGISTERED))
    # The registration's own keys win; only the editable one carries over.
    assert merged == {"base_url": "https://new.test", "tenant_id": "t-b"}


def test_the_webhook_secret_survives_a_re_registration():
    """Without it no OpenAEV or OpenCTI event could be provisioned after the next heartbeat."""
    for provider in ("openaev", "opencti"):
        stored = _stored(base_url="https://old.test", webhook_secret="s3cret")
        merged = json.loads(_keep_admin_extra_fields(provider, stored, REGISTERED))
        assert merged == {"base_url": "https://new.test", "webhook_secret": "s3cret"}, provider


def test_opencti_live_writes_survives_a_re_registration():
    """An admin's Live writes choice is not reset to off by the next heartbeat."""
    stored = _stored(base_url="https://old.test", live_writes="on", stray="x")
    merged = json.loads(_keep_admin_extra_fields("opencti", stored, REGISTERED))
    assert merged == {"base_url": "https://new.test", "live_writes": "on"}


def test_a_provider_without_editable_fields_takes_the_registration_as_is():
    stored = _stored(base_url="https://old.test", tenant_id="t-b", webhook_secret="s")
    assert _keep_admin_extra_fields("opencrq", stored, REGISTERED) == REGISTERED


def test_nothing_stored_or_unreadable_takes_the_registration_as_is():
    assert _keep_admin_extra_fields("openaev", None, REGISTERED) == REGISTERED
    assert _keep_admin_extra_fields("openaev", "not-a-fernet-token", REGISTERED) == REGISTERED
