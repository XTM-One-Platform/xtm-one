"""Secret variables referenced from an integration credential.

``{{var_id.<uuid>}}`` in an integration ``api_key`` / ``client_secret`` is
stored as written and resolved where the credential is used
(``vault.reveal_field``), with the integration owner's visibility; the write
paths refuse a reference to an unknown, non-secret or invisible variable.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.redaction import known_secrets_scope, redact_known_secrets
from app.core.security import encrypt_value
from app.services import vault
from app.services.declarative_http_executor import _resolve_auth
from app.services.vault import variable_refs
from app.services.vault.errors import VAULT_UNAVAILABLE_DETAIL, SecretUnavailableError
from app.services.vault.failures import SecretOwner

_VAULTED_VAR = str(uuid.UUID(int=0xA1))
_PLAIN_VAR = str(uuid.UUID(int=0xB2))
_OWNER = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _integration(**columns: Any) -> SimpleNamespace:
    row = SimpleNamespace(id="11111111-1111-1111-1111-111111111111", user_id=_OWNER, **columns)
    row.__tablename__ = "integrations"
    return row


class _Recorder:
    def __init__(self) -> None:
        self.recorded: list[tuple[SecretOwner, str, str]] = []
        self.cleared: list[SecretOwner] = []

    async def record(self, owner, provider, reason, reference) -> None:
        self.recorded.append((owner, provider, reason))

    async def clear(self, owner) -> None:
        self.cleared.append(owner)


@pytest.fixture
def failures(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(variable_refs, "record_failure", recorder.record)
    monkeypatch.setattr(variable_refs, "clear_failure", recorder.clear)
    return recorder


def _variables(monkeypatch, rows: dict[str, Any], seen_owner: list[object] | None = None) -> None:
    """The owner-visible variables the resolver reads, and their values."""

    async def _visible(variable_ids, owner_id):
        if seen_owner is not None:
            seen_owner.append(owner_id)
        return {vid: rows[vid] for vid in variable_ids if vid in rows}

    async def _value(variable):
        return variable.plain

    monkeypatch.setattr(variable_refs, "_visible_variables", _visible)
    monkeypatch.setattr(variable_refs, "_secret_value", _value)


def _variable(plain: str, *, is_secret: bool = True) -> SimpleNamespace:
    return SimpleNamespace(is_secret=is_secret, value_enc="enc" if plain else None, plain=plain)


def test_placeholders_are_found_in_order_once_each_and_only_in_strings():
    text = f"users API-Key {{{{var_id.{_VAULTED_VAR.upper()}}}}} {{{{var_id.{_VAULTED_VAR}}}}} {{{{var_id.{_PLAIN_VAR}}}}}"
    assert variable_refs.variable_ids_in(text) == [_VAULTED_VAR, _PLAIN_VAR]
    assert variable_refs.variable_ids_in("plain-key") == []
    assert variable_refs.variable_ids_in(None) == []
    assert not variable_refs.holds_variable_reference("{{var_id.not-a-uuid}}")


async def test_a_reference_is_resolved_at_use_with_the_owner_visibility_and_redacted(monkeypatch, failures):
    seen: list[object] = []
    _variables(monkeypatch, {_VAULTED_VAR: _variable("pk_live_resolved")}, seen)
    row = _integration(api_key_enc=encrypt_value(f"users API-Key {{{{var_id.{_VAULTED_VAR}}}}}"))

    with known_secrets_scope("run-1"):
        token = await vault.reveal_field(row, "api_key_enc")
        assert token == "users API-Key pk_live_resolved"
        # What a run stores, traces and logs holds the marker, as for a variable.
        assert "pk_live_resolved" not in json.dumps(redact_known_secrets({"header": token}))

    assert seen == [_OWNER]
    assert failures.cleared and not failures.recorded


async def test_the_declarative_executor_sends_the_resolved_value_in_the_declared_header(monkeypatch, failures):
    _variables(monkeypatch, {_VAULTED_VAR: _variable("pk_live_resolved")})
    row = _integration(api_key_enc=encrypt_value(f"users API-Key {{{{var_id.{_VAULTED_VAR}}}}}"))

    with known_secrets_scope("run-2"):
        token = await vault.reveal_field(row, "api_key_enc")
        parts = _resolve_auth({"type": "header", "header": "Authorization", "prefix": ""}, token)

    assert parts.headers == {"Authorization": "users API-Key pk_live_resolved"}


async def test_a_plain_key_is_untouched_and_reads_no_variable(monkeypatch, failures):
    async def _never(*_args):
        raise AssertionError("a plain key must not read a variable")

    monkeypatch.setattr(variable_refs, "_visible_variables", _never)
    row = _integration(api_key_enc=encrypt_value("plain-key {{not.a.reference}}"), client_secret_enc=None)
    assert await vault.reveal_field(row, "api_key_enc") == "plain-key {{not.a.reference}}"
    assert await vault.reveal_field(row, "client_secret_enc") is None
    assert not failures.recorded and not failures.cleared


async def test_another_entity_never_resolves_a_placeholder(monkeypatch, failures):
    async def _never(*_args):
        raise AssertionError("only an integration credential reads a variable")

    monkeypatch.setattr(variable_refs, "_visible_variables", _never)
    bot = SimpleNamespace(id="b", api_key_enc=encrypt_value(f"{{{{var_id.{_VAULTED_VAR}}}}}"))
    bot.__tablename__ = "channel_bots"
    assert await vault.reveal_field(bot, "api_key_enc") == f"{{{{var_id.{_VAULTED_VAR}}}}}"


@pytest.mark.parametrize(
    "rows",
    [
        {},  # deleted, or no longer visible to the owner
        {_VAULTED_VAR: _variable("value", is_secret=False)},  # no longer secret
        {_VAULTED_VAR: _variable("")},  # empty
    ],
    ids=["missing-or-invisible", "not-secret", "empty"],
)
async def test_an_unusable_variable_fails_generically_and_is_recorded(monkeypatch, failures, rows):
    _variables(monkeypatch, rows)
    row = _integration(api_key_enc=encrypt_value(f"{{{{var_id.{_VAULTED_VAR}}}}}"))

    with pytest.raises(SecretUnavailableError) as exc:
        await vault.reveal_field(row, "api_key_enc")

    assert str(exc.value) == VAULT_UNAVAILABLE_DETAIL
    assert exc.value.reason == "variable_unavailable"
    assert failures.recorded == [
        (SecretOwner(kind="integration", id=row.id, field="api_key"), "variable", "variable_unavailable")
    ]


async def test_the_status_names_the_referenced_variables_never_a_value():
    from app.api.v1.schemas.vault_references import vault_status

    row = _integration(
        api_key_enc=encrypt_value(f"users API-Key {{{{var_id.{_VAULTED_VAR}}}}}"),
        client_secret_enc=encrypt_value("plain"),
    )
    status = vault_status(row)
    assert status is not None
    assert status.variables == {"api_key": [_VAULTED_VAR]}
    assert status.references == {}
    assert vault_status(_integration(api_key_enc=encrypt_value("plain"), client_secret_enc=None)) is None


# --- Write paths -------------------------------------------------------------


def _flags(monkeypatch, by_viewer: dict[object, dict[str, bool]]) -> None:
    async def _visible(_db, variable_ids, viewer):
        flags = by_viewer.get(viewer, {})
        return {vid: flags[vid] for vid in variable_ids if vid in flags}

    monkeypatch.setattr(variable_refs, "visible_secret_flags", _visible)


_CALLER = uuid.UUID("33333333-3333-3333-3333-333333333333")


async def test_a_visible_secret_variable_is_accepted(monkeypatch):
    _flags(monkeypatch, {_CALLER: {_VAULTED_VAR: True}, _OWNER: {_VAULTED_VAR: True}})
    fields = {"api_key": f"users API-Key {{{{var_id.{_VAULTED_VAR}}}}}", "client_secret": None}
    assert await vault.variable_reference_problem(None, fields, viewer_ids=[_CALLER, _OWNER]) is None


async def test_a_plain_key_needs_no_lookup(monkeypatch):
    async def _never(*_args):
        raise AssertionError("no placeholder, no lookup")

    monkeypatch.setattr(variable_refs, "visible_secret_flags", _never)
    assert await vault.variable_reference_problem(None, {"api_key": "plain"}, viewer_ids=[_CALLER]) is None


async def test_an_unknown_variable_is_refused(monkeypatch):
    _flags(monkeypatch, {_CALLER: {}})
    problem = await vault.variable_reference_problem(
        None, {"api_key": f"{{{{var_id.{_VAULTED_VAR}}}}}"}, viewer_ids=[_CALLER]
    )
    assert problem is not None
    assert problem.startswith("api_key references the variable " + _VAULTED_VAR)
    assert "does not exist or is not visible" in problem


async def test_a_non_secret_variable_is_refused(monkeypatch):
    _flags(monkeypatch, {_CALLER: {_PLAIN_VAR: False}})
    problem = await vault.variable_reference_problem(
        None, {"client_secret": f"{{{{var_id.{_PLAIN_VAR}}}}}"}, viewer_ids=[_CALLER]
    )
    assert problem is not None
    assert problem.startswith("client_secret references the variable " + _PLAIN_VAR)
    assert "not a secret variable" in problem


async def test_a_variable_the_owner_cannot_see_is_refused(monkeypatch):
    # The caller sees it, the integration's owner (who resolves it) does not.
    _flags(monkeypatch, {_CALLER: {_VAULTED_VAR: True}, _OWNER: {}})
    problem = await vault.variable_reference_problem(
        None, {"api_key": f"{{{{var_id.{_VAULTED_VAR}}}}}"}, viewer_ids=[_CALLER, _OWNER]
    )
    assert problem is not None and "not visible" in problem


async def test_the_rest_guard_answers_400_naming_the_field(monkeypatch):
    from fastapi import HTTPException

    from app.api.v1.schemas import vault_references

    _flags(monkeypatch, {_CALLER: {}})
    with pytest.raises(HTTPException) as exc:
        await vault_references.require_secret_variable_references(
            None, {"api_key": f"{{{{var_id.{_VAULTED_VAR}}}}}"}, viewer_ids=[_CALLER]
        )
    assert exc.value.status_code == 400
    assert str(exc.value.detail).startswith("api_key references the variable")
