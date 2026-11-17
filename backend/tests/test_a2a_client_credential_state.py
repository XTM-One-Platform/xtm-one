"""The outbound A2A integration's status follows its partner credential (#12346).

A token endpoint that answers 401 moves the integration to ``error`` with the
reason and the time; the owner and the administrators are told once per
transition; a later mint restores ``connected``; ``get_integration`` shows the
state without any secret; the refusals name the repair.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.a2a.client import credential_state, credentials
from app.a2a.client.credentials import CredentialError
from tests.a2a_client_stubs import resolve_public


class _Integration(SimpleNamespace):
    """An integration row stand-in the module treats as persisted."""

    __tablename__ = "integrations"


def _integration(**kw):
    fields = {
        "id": uuid.uuid4(),
        "name": "Partner CTEM",
        "provider": "oauth_a2a",
        "user_id": uuid.uuid4(),
        "status": "connected",
        "client_id_enc": None,
        "client_secret_enc": None,
        "extra_config_enc": None,
        "token_url": "https://partner.example/token",
    }
    fields.update(kw)
    return _Integration(**fields)


class _DB:
    """Answers each UPDATE with the next rowcount and records what ran."""

    def __init__(self, rowcounts, admins=()):
        self.rowcounts = list(rowcounts)
        self.admins = list(admins)
        self.statements = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement):
        self.statements.append(statement)
        if str(statement).lstrip().upper().startswith("SELECT"):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.admins))
        return SimpleNamespace(rowcount=self.rowcounts.pop(0) if self.rowcounts else 0)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _factory(db):
    return lambda: db


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    resolve_public(monkeypatch)
    credentials._TOKENS.clear()
    credential_state._LAST_STAMP.clear()
    sent = []

    async def _notify(db, user_id, **kw):
        sent.append((user_id, kw))

    monkeypatch.setattr("app.services.notify.notify_web_user", _notify)
    return sent


def _values(statement) -> dict:
    return {col.name: bind.value for col, bind in statement._values.items()}


class TestMintFailure:
    @pytest.mark.asyncio
    async def test_a_rejected_credential_sets_error_with_reason_and_time(self, _fresh):
        integration = _integration()
        admin = str(uuid.uuid4())
        db = _DB([1, 1], admins=[admin])
        moved = await credential_state.record_mint_failure(
            integration, "Partner token endpoint returned HTTP 401.", session_factory=_factory(db)
        )
        assert moved is True
        stamp = _values(db.statements[0])
        assert stamp["credential_error_code"] == "credential_rejected"
        assert stamp["credential_error_detail"] == "Partner token endpoint returned HTTP 401."
        assert isinstance(stamp["credential_error_at"], datetime)
        assert _values(db.statements[1]) == {"status": "error"}
        assert db.committed
        told = {user for user, _ in _fresh}
        assert told == {str(integration.user_id), admin}
        _, kw = _fresh[0]
        assert "client id and secret" in kw["summary"]
        assert kw["extra_data"]["integration_id"] == str(integration.id)

    @pytest.mark.asyncio
    async def test_a_second_failure_keeps_error_and_notifies_nobody(self, _fresh):
        # The status UPDATE matches no row: the integration is already in error.
        db = _DB([1, 0], admins=[str(uuid.uuid4())])
        moved = await credential_state.record_mint_failure(
            _integration(status="error"), "Registration is unusable.", session_factory=_factory(db)
        )
        assert moved is False
        assert _fresh == []
        assert db.committed

    @pytest.mark.asyncio
    async def test_an_inbound_or_foreign_row_is_left_alone(self, _fresh):
        # The settings UPDATE filters on the outbound direction: 0 rows.
        db = _DB([0])
        assert not await credential_state.record_mint_failure(
            _integration(), "x", session_factory=_factory(db)
        )
        assert len(db.statements) == 1
        assert db.rolled_back and not db.committed
        assert _fresh == []

    @pytest.mark.asyncio
    async def test_a_stand_in_is_never_written(self, _fresh):
        db = _DB([1, 1])
        stand_in = SimpleNamespace(id=uuid.uuid4(), name="n", status="connected")
        assert not await credential_state.record_mint_failure(stand_in, "x", session_factory=_factory(db))
        assert db.statements == []

    @pytest.mark.asyncio
    async def test_it_never_raises(self, _fresh):
        def _broken():
            raise RuntimeError("database down")

        assert not await credential_state.record_mint_failure(_integration(), "x", session_factory=_broken)


class TestMintSuccess:
    @pytest.mark.asyncio
    async def test_a_mint_restores_connected_and_clears_the_error(self):
        db = _DB([1, 1])
        assert await credential_state.record_mint_success(
            _integration(status="error"), session_factory=_factory(db)
        )
        stamp = _values(db.statements[0])
        assert isinstance(stamp["last_token_minted_at"], datetime)
        assert stamp["credential_error_code"] is None
        assert _values(db.statements[1]) == {"status": "connected"}
        assert db.committed

    @pytest.mark.asyncio
    async def test_a_busy_partner_is_stamped_at_most_every_interval(self):
        integration = _integration()
        first = _DB([1, 0])
        assert await credential_state.record_mint_success(integration, session_factory=_factory(first))
        second = _DB([1, 0])
        assert not await credential_state.record_mint_success(integration, session_factory=_factory(second))
        assert second.statements == []
        old = datetime.now(UTC) - credential_state.MINT_STAMP_INTERVAL - timedelta(seconds=1)
        credential_state._LAST_STAMP[str(integration.id)] = old
        third = _DB([1, 0])
        assert await credential_state.record_mint_success(integration, session_factory=_factory(third))

    @pytest.mark.asyncio
    async def test_an_integration_in_error_is_restored_whatever_the_interval(self):
        integration = _integration()
        credential_state._LAST_STAMP[str(integration.id)] = datetime.now(UTC)
        integration.status = "error"
        db = _DB([1, 1])
        assert await credential_state.record_mint_success(integration, session_factory=_factory(db))
        assert _values(db.statements[1]) == {"status": "connected"}


class TestOutboundHeaders:
    @pytest.mark.asyncio
    async def test_a_real_mint_records_success_a_cached_token_does_not(self, monkeypatch):
        calls = []

        async def _success(integration, **kw):
            calls.append(integration)
            return True

        async def _config(_integration):
            return {"client_id": "cid", "client_secret": "csec"}

        monkeypatch.setattr(credential_state, "record_mint_success", _success)
        monkeypatch.setattr(credentials, "_config", _config)
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
            )
        )
        integration = _integration()
        await credentials.outbound_headers(integration, client)
        await credentials.outbound_headers(integration, client)
        assert calls == [integration]

    @pytest.mark.asyncio
    async def test_a_401_raises_and_records_no_success(self, monkeypatch):
        calls = []

        async def _success(integration, **kw):
            calls.append(integration)

        async def _config(_integration):
            return {"client_id": "cid", "client_secret": "csec"}

        monkeypatch.setattr(credential_state, "record_mint_success", _success)
        monkeypatch.setattr(credentials, "_config", _config)
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(401)))
        with pytest.raises(CredentialError, match="HTTP 401"):
            await credentials.outbound_headers(_integration(), client)
        assert calls == []


class TestDescribe:
    def test_outbound_in_error_shows_the_last_error_and_no_secret(self):
        at = datetime(2026, 11, 17, 7, 0, tzinfo=UTC)
        settings = SimpleNamespace(
            is_outbound=True,
            last_token_minted_at=None,
            credential_error_code="credential_rejected",
            credential_error_detail="Partner token endpoint returned HTTP 401.",
            credential_error_at=at,
        )
        state = credential_state.describe(settings, _integration(status="error", client_secret_enc="ENC"))
        assert state == {
            "usable": False,
            "last_token_minted_at": None,
            "last_error": {
                "code": "credential_rejected",
                "detail": "Partner token endpoint returned HTTP 401.",
                "at": at.isoformat(),
            },
        }
        assert "ENC" not in repr(state)

    def test_outbound_healthy(self):
        minted = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
        settings = SimpleNamespace(
            is_outbound=True,
            last_token_minted_at=minted,
            credential_error_code=None,
            credential_error_detail=None,
            credential_error_at=None,
        )
        assert credential_state.describe(settings, _integration()) == {
            "usable": True,
            "last_token_minted_at": minted.isoformat(),
            "last_error": None,
        }

    def test_inbound_and_missing_rows_have_no_state(self):
        assert credential_state.describe(SimpleNamespace(is_outbound=False), _integration()) is None
        assert credential_state.describe(None, _integration()) is None


def test_the_repair_hint_names_the_integration_and_the_fix():
    hint = credential_state.repair_hint(_integration(name="Partner CTEM"))
    assert "administrator must enter a valid client id and secret" in hint
    assert "'Partner CTEM'" in hint


@pytest.mark.asyncio
async def test_the_preflight_refusal_names_the_repair(monkeypatch):
    from app.a2a.client import delegation
    from app.models.a2a import UNUSABLE_CREDENTIAL_NO_TOKEN, USABILITY_UNUSABLE

    async def _enabled(_db):
        return True

    monkeypatch.setattr("app.api.v1.platform.is_chat_enabled", _enabled)
    target = SimpleNamespace(usability=USABILITY_UNUSABLE, unusable_reason=UNUSABLE_CREDENTIAL_NO_TOKEN)
    with pytest.raises(delegation.PreflightRefusedError) as refused:
        await delegation.preflight(None, _integration(name="Partner CTEM"), target)
    assert "client id and secret on integration 'Partner CTEM'" in str(refused.value.detail)
