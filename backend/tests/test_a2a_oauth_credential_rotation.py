"""#105: the A2A OAuth credential rotates its secret in place, Verify tests the
secret, and both sides can name a client id mismatch.

Doubles only (no database): the PostgreSQL behaviour of the columns is the
migration's, and the token endpoint's own path keeps its existing tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.a2a import refusals
from app.a2a.client import credential_state, credentials, registration
from app.api.v1.a2a_server import minted_before_rotation
from app.models.a2a import UNUSABLE_CREDENTIAL_NO_TOKEN, USABILITY_UNUSABLE
from tests.a2a_client_stubs import resolve_public

# ---------------------------------------------------------------- part A


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: list(self._rows))


class _MintDB:
    """Holds the integration's OAuthClient rows as the session would."""

    def __init__(self) -> None:
        self.rows: list[Any] = []
        self.deleted: list[Any] = []

    async def execute(self, _statement: object) -> _Rows:
        return _Rows(sorted(self.rows, key=lambda r: r.created_at, reverse=True))

    def add(self, row: Any) -> None:
        if getattr(row, "created_at", None) is None:
            row.created_at = datetime.now(UTC)
        self.rows.append(row)

    async def delete(self, row: Any) -> None:
        self.rows.remove(row)
        self.deleted.append(row)

    async def flush(self) -> None:
        return None


@pytest.fixture
def _mint_env(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[str]]:
    from app.integrations.providers import oauth_a2a

    async def _hash(value: bytes) -> bytes:
        return b"hash:" + value

    async def _reveal(_integration: object, _field: str) -> dict[str, Any]:
        return {}

    retired: list[str] = []

    async def _remember(_db: object, _integration: object, ids: list[str]) -> None:
        retired.extend(ids)

    monkeypatch.setattr("app.core.security.bcrypt_hashpw_async", _hash)
    monkeypatch.setattr("app.core.security.encrypt_value", lambda v: v)
    monkeypatch.setattr("app.services.vault.reveal_json_field", _reveal)
    monkeypatch.setattr(oauth_a2a, "_remember_retired_client_ids", _remember)
    return oauth_a2a, retired


def _inbound() -> Any:
    return SimpleNamespace(id=uuid.uuid4(), name="Partners", extra_config_enc=None, status="pending")


@pytest.mark.asyncio
async def test_regenerating_twice_keeps_the_client_id_and_only_the_latest_secret_matches(
    _mint_env: tuple[Any, list[str]],
) -> None:
    oauth_a2a, retired = _mint_env
    db, integration = _MintDB(), _inbound()

    first_id, first_secret = await oauth_a2a.mint_credential(db, integration)
    second_id, second_secret = await oauth_a2a.mint_credential(db, integration)
    third_id, third_secret = await oauth_a2a.mint_credential(db, integration)

    assert first_id == second_id == third_id
    assert len({first_secret, second_secret, third_secret}) == 3
    assert len(db.rows) == 1 and db.deleted == [] and retired == []
    row = db.rows[0]
    # Only the latest secret's hash is stored: a secret-only paste on the
    # partner pairs with the client id it already holds.
    assert row.client_secret_hash == f"hash:{third_secret}"
    assert row.secret_rotated_at is not None


@pytest.mark.asyncio
async def test_the_first_generate_mints_a_client_id_and_no_rotation_stamp(
    _mint_env: tuple[Any, list[str]],
) -> None:
    oauth_a2a, _ = _mint_env
    db = _MintDB()
    client_id, _ = await oauth_a2a.mint_credential(db, _inbound())
    assert client_id.startswith("a2a-")
    assert db.rows[0].secret_rotated_at is None


@pytest.mark.asyncio
async def test_the_explicit_rotation_changes_both_and_remembers_the_retired_id(
    _mint_env: tuple[Any, list[str]],
) -> None:
    oauth_a2a, retired = _mint_env
    db, integration = _MintDB(), _inbound()
    old_id, _ = await oauth_a2a.mint_credential(db, integration)
    new_id, _ = await oauth_a2a.mint_credential(db, integration, rotate_client_id=True)
    assert new_id != old_id
    assert [r.client_id for r in db.rows] == [new_id]
    assert retired == [old_id]


def test_a_bearer_minted_before_the_rotation_is_refused() -> None:
    rotated = datetime.now(UTC)
    client = SimpleNamespace(secret_rotated_at=rotated)
    before = int((rotated - timedelta(seconds=5)).timestamp())
    after = int((rotated + timedelta(seconds=5)).timestamp())
    assert minted_before_rotation({"iat": before}, client)
    assert not minted_before_rotation({"iat": after}, client)
    assert not minted_before_rotation({"iat": int(rotated.timestamp())}, client)
    assert minted_before_rotation({}, client)
    assert not minted_before_rotation({"iat": before}, SimpleNamespace(secret_rotated_at=None))


# ---------------------------------------------------------------- part B + C outbound


class _Integration(SimpleNamespace):
    __tablename__ = "integrations"


def _outbound() -> Any:
    return _Integration(
        id=uuid.uuid4(),
        name="CEO Office A2A",
        provider="oauth_a2a",
        user_id=uuid.uuid4(),
        status="connected",
        client_id_enc=None,
        client_secret_enc=None,
        extra_config_enc=None,
        token_url="https://partner.example/mcp/oauth/token",
    )


@pytest.fixture
def _outbound_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, list[Any]]]:
    resolve_public(monkeypatch)
    credentials._TOKENS.clear()
    credential_state._LAST_STAMP.clear()
    events: dict[str, list[Any]] = {"success": [], "failure": []}

    async def _success(_integration: object, **kw: Any) -> bool:
        events["success"].append(kw)
        return True

    async def _failure(_integration: object, detail: str, **_kw: Any) -> bool:
        events["failure"].append(detail)
        return True

    async def _config(_integration: object) -> dict[str, str]:
        return {"client_id": "a2a-J2Qs6old", "client_secret": "wrong"}

    monkeypatch.setattr(credential_state, "record_mint_success", _success)
    monkeypatch.setattr(credential_state, "record_mint_failure", _failure)
    monkeypatch.setattr(credentials, "_config", _config)
    yield events
    credentials._TOKENS.clear()


@pytest.mark.asyncio
async def test_verify_with_a_wrong_secret_answers_cannot_mint_naming_the_oauth_error_and_client_id(
    _outbound_env: dict[str, list[Any]],
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(401, json={"error": "invalid_client"}))
    )
    verdict = await registration.test_credential(_outbound(), client)
    assert verdict is not None
    detail = verdict.error_detail or ""
    assert verdict.usability == USABILITY_UNUSABLE
    assert verdict.unusable_reason == UNUSABLE_CREDENTIAL_NO_TOKEN
    assert "invalid_client" in detail
    assert "a2a-J2Qs6old" in detail
    # The client id is named; the secret never is.
    assert "wrong" not in detail.replace("wrong secret", "")
    assert _outbound_env["failure"] == [detail]
    assert _outbound_env["success"] == []


@pytest.mark.asyncio
async def test_verify_with_the_right_secret_is_usable_and_stamps_the_mint(
    _outbound_env: dict[str, list[Any]],
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
    )
    assert await registration.test_credential(_outbound(), client) is None
    # A manual Verify always stamps last_token_minted_at (force), whatever
    # the stamp interval.
    assert _outbound_env["success"] == [{"force": True}]


def test_a_free_text_error_from_the_partner_is_not_echoed() -> None:
    response = httpx.Response(401, json={"error": "<script>"})
    detail = credentials._token_refusal_detail(response, "a2a-x")
    assert "<script>" not in detail and "a2a-x" in detail


@pytest.mark.asyncio
async def test_a_credential_save_reads_not_tested_and_marks_the_targets_due() -> None:
    class _DB:
        def __init__(self) -> None:
            self.statements: list[Any] = []

        async def execute(self, statement: Any) -> SimpleNamespace:
            self.statements.append(statement)
            return SimpleNamespace(rowcount=1)

    integration = _outbound()
    integration.connection_test_status = "ok"
    credential_state._LAST_STAMP[str(integration.id)] = datetime.now(UTC)
    db = _DB()
    await credential_state.clear_on_credential_edit(db, integration)
    assert integration.connection_test_status == "untested"
    assert str(integration.id) not in credential_state._LAST_STAMP
    tables = [s.table.name for s in db.statements]
    assert "a2a_client_targets" in tables


# ---------------------------------------------------------------- part C inbound


class _Session:
    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.committed = False

    async def execute(self, statement: Any) -> SimpleNamespace:
        self.statements.append(statement)
        return SimpleNamespace(rowcount=1)

    async def commit(self) -> None:
        self.committed = True

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.mark.parametrize(
    ("reason", "expected"),
    [(refusals.REASON_UNKNOWN_CLIENT, "unknown_client"), (refusals.REASON_SECRET_MISMATCH, "secret_mismatch")],
)
@pytest.mark.asyncio
async def test_a_refused_request_is_recorded_with_its_reason_and_never_the_secret(reason: str, expected: str) -> None:
    session = _Session()
    integration_id = uuid.uuid4()
    assert await refusals.record_refusal(integration_id, "a2a-J2Qs6old", reason, session_factory=lambda: session)
    values = {c.name: b.value for c, b in session.statements[0]._values.items()}
    assert values["last_refused_reason"] == expected
    assert values["last_refused_client_id"] == "a2a-J2Qs6old"
    assert isinstance(values["last_refused_token_at"], datetime)
    assert not any("secret" in name and "reason" not in name for name in values)
    assert session.committed


@pytest.mark.asyncio
async def test_attribution_tells_a_current_client_id_from_a_retired_one() -> None:
    integration_id = uuid.uuid4()
    current = SimpleNamespace(client_id="a2a-new", a2a_integration_id=integration_id)
    settings_row = SimpleNamespace(integration_id=integration_id, retired_client_ids=["a2a-old"])

    class _DB:
        def __init__(self, client: object) -> None:
            self.client = client
            self.calls = 0

        async def execute(self, _statement: object) -> SimpleNamespace:
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(scalar_one_or_none=lambda: self.client)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [settings_row]))

    assert await refusals.attribute(_DB(current), "a2a-new") == (integration_id, "secret_mismatch")
    assert await refusals.attribute(_DB(None), "a2a-old") == (integration_id, "unknown_client")
    assert await refusals.attribute(_DB(None), "a2a-nobody") is None


def test_the_manager_view_carries_no_secret() -> None:
    row = SimpleNamespace(
        last_refused_token_at=datetime(2026, 11, 18, 7, 36, tzinfo=UTC),
        last_refused_client_id="a2a-J2Qs6old",
        last_refused_reason="unknown_client",
    )
    view = refusals.describe_refusal(row)
    assert view is not None
    assert set(view) == {"at", "client_id", "reason", "what_to_do"}
    assert view["what_to_do"]
    assert refusals.describe_refusal(SimpleNamespace(last_refused_token_at=None)) is None
