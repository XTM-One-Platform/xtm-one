"""Saving an OAuth default keeps a stored secret the form left empty.

The Settings > Integrations > OAuth Defaults form cannot read a stored secret, and the upsert used
to replace the extra config wholesale, so re-saving the Slack OAuth default without
retyping its Signing secret wiped it - and every Slack integration on the admin app
fell back to polling. The form now sends the mask for a stored secret, and the upsert
reads it back as "keep the stored value", the rule of an integration update.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api.v1 import settings as settings_api
from app.api.v1.integrations import EXTRA_CONFIG_MASK
from app.api.v1.schemas.settings import OAuthDefaultRequest
from app.core.security import decrypt_value, encrypt_value


def _default(**extra):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="slack",
        client_id_enc=encrypt_value("client-id"),
        client_secret_enc=encrypt_value("client-secret"),
        extra_config_enc=encrypt_value(json.dumps(extra)) if extra else None,
        created_at=now,
        updated_at=now,
    )


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _Db:
    def __init__(self, row):
        self.row = row

    async def execute(self, _statement):
        return _Result(self.row)

    def add(self, row):
        self.row = row

    async def flush(self):
        if getattr(self.row, "id", None) is None:
            self.row.id = uuid.uuid4()
        now = datetime.now(UTC)
        self.row.created_at = getattr(self.row, "created_at", None) or now
        self.row.updated_at = now

    async def commit(self):
        return None


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    async def _log_audit(*_a, **_k):
        return None

    monkeypatch.setattr(settings_api, "log_audit", _log_audit)


def _save(db, extra_config):
    body = OAuthDefaultRequest(
        provider="slack", client_id="client-id", client_secret="client-secret", extra_config=extra_config
    )
    return asyncio.run(settings_api.upsert_oauth_default(body, request=None, user=SimpleNamespace(), db=db))


def _stored(row) -> dict:
    return json.loads(decrypt_value(row.extra_config_enc)) if row.extra_config_enc else {}


def test_a_stored_secret_sent_back_as_the_mask_is_kept():
    db = _Db(_default(webhook_secret="signing-secret"))
    response = _save(db, {"webhook_secret": EXTRA_CONFIG_MASK})
    assert _stored(db.row) == {"webhook_secret": "signing-secret"}
    assert response.stored_secret_keys == ["webhook_secret"]


def test_a_typed_secret_replaces_the_stored_one():
    db = _Db(_default(webhook_secret="old"))
    _save(db, {"webhook_secret": "new"})
    assert _stored(db.row) == {"webhook_secret": "new"}


def test_the_mask_with_nothing_stored_is_never_stored_as_the_secret():
    db = _Db(_default())
    response = _save(db, {"webhook_secret": EXTRA_CONFIG_MASK})
    assert db.row.extra_config_enc is None
    assert response.stored_secret_keys == []


def test_a_non_secret_field_keeps_the_literal_it_was_sent():
    db = _Db(_default(tenant_id="t-1"))
    _save(db, {"tenant_id": EXTRA_CONFIG_MASK})
    assert _stored(db.row) == {"tenant_id": EXTRA_CONFIG_MASK}


def test_the_list_names_stored_secrets_without_their_values():
    row = _default(webhook_secret="signing-secret", tenant_id="t-1", api_key="")
    response = settings_api._oauth_default_response(row)
    assert response.stored_secret_keys == ["webhook_secret"]
    assert "signing-secret" not in response.model_dump_json()


def test_an_unreadable_stored_config_names_nothing():
    row = _default()
    row.extra_config_enc = "not-a-fernet-token"
    assert settings_api._oauth_default_response(row).stored_secret_keys == []
