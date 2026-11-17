"""The builder tools set and read an OAuth integration's credential mode.

``share_connected_account`` (alias ``credential_mode``) on create_integration and
update_integration is the Integrations UI's "Don't share my connected account"
switch, written through the same ``apply_oauth_share_consent``; get_integration
and list_integrations report the mode and, on a per_user row, a count of the
people who connected their own account.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.core.database as database
import app.services.integration_user_credentials as user_credentials_module
from app.services.builtin_tools import build_agent_builder_tool
from app.services.builtin_tools.agent_builder import (
    _credential_mode_line,
    _share_connected_account_argument,
)
from app.services.integration_credentials import (
    PER_USER_CREDENTIAL_MODE,
    SHARE_CONSENT_REFUSED_DETAIL,
    SHARED_CREDENTIAL_MODE,
)
from app.tools.base import ToolCall
from tests.test_agent_builder_crud_tools import (
    _admin_caller,
    _session_factory,
    _stub_manageable_gate,
)


async def _call(executor, tool_name, /, **arguments):
    return await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=tool_name, arguments=arguments)
    )


def _shared_oauth_row(owner_id, **overrides):
    values = dict(
        id=uuid.uuid4(),
        name="Acme",
        provider="acme",
        auth_type="oauth",
        status="connected",
        user_id=owner_id,
        is_company_managed=False,
        shared_group_ids=[str(uuid.uuid4())],
        shared_group_permissions={},
        shared_user_ids=[],
        access_token_enc="ENC(tok)",
        refresh_token_enc="ENC(ref)",
        token_expires_at=None,
        oauth_token_owner_id=None,
        oauth_share_acknowledged=True,
        credential_mode=SHARED_CREDENTIAL_MODE,
        client_id_enc="ENC(cid)",
        client_secret_enc="ENC(sec)",
        api_key_enc=None,
        extra_config_enc=None,
        authorize_url="https://acme.example/authorize",
        token_url="https://acme.example/token",
        uses_admin_oauth_credentials=False,
        tool_slug=None,
        is_builtin=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def no_credential_writes(monkeypatch):
    """Record the people's own-credential deletes and prunes instead of querying."""
    calls: dict[str, int] = {"delete": 0, "prune": 0}

    async def _delete(_db, _integration_id, **_kw):
        calls["delete"] += 1
        return 0

    async def _prune(_db, _integration):
        calls["prune"] += 1
        return 0

    monkeypatch.setattr(user_credentials_module, "delete_user_credentials", _delete)
    monkeypatch.setattr(user_credentials_module, "prune_unreachable_user_credentials", _prune)
    return calls


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("share", "mode", "expected"),
    [
        (None, None, None),
        ("", "", None),
        (False, None, False),
        (True, None, True),
        ("false", None, False),
        ("TRUE", None, True),
        (None, "per_user", False),
        (None, "shared", True),
        (False, "per_user", False),
    ],
)
def test_share_connected_account_argument_maps_to_the_consent(share, mode, expected):
    assert _share_connected_account_argument(share, mode) == (expected, None)


@pytest.mark.parametrize(
    ("share", "mode"),
    [("maybe", None), (None, "everyone"), (True, "per_user"), (3, None), (None, 1)],
)
def test_share_connected_account_argument_refuses_what_it_cannot_read(share, mode):
    choice, error = _share_connected_account_argument(share, mode)
    assert choice is None
    assert error


def test_credential_mode_line_counts_and_never_names():
    per_user = SimpleNamespace(auth_type="oauth", credential_mode=PER_USER_CREDENTIAL_MODE)
    assert "per_user" in _credential_mode_line(per_user, 2)
    assert "2 connected their own account" in _credential_mode_line(per_user, 2)
    shared = SimpleNamespace(auth_type="oauth", credential_mode=SHARED_CREDENTIAL_MODE)
    assert _credential_mode_line(shared).startswith("shared")
    assert _credential_mode_line(SimpleNamespace(auth_type="api_key", credential_mode="shared")) == ""


# ---------------------------------------------------------------------------
# update_integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_integration_opts_out_of_sharing_the_connected_account(
    monkeypatch, no_credential_writes
):
    owner = uuid.uuid4()
    integ = _shared_oauth_row(owner)
    _stub_manageable_gate(monkeypatch, entity=integ)
    _admin_caller(monkeypatch, is_admin=False)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(owner))
    result = await _call(
        executor, "update_integration", integration_id=str(integ.id), share_connected_account=False
    )

    assert not result.is_error, result.content
    assert integ.credential_mode == PER_USER_CREDENTIAL_MODE
    assert integ.oauth_share_acknowledged is False
    # The owner keeps their own connection.
    assert integ.access_token_enc == "ENC(tok)"
    assert "share_connected_account" in result.content
    assert "Credential mode: per_user" in result.content
    assert created[0].commits == 1
    # The people's own accounts are pruned to those who still see it.
    assert no_credential_writes["prune"] == 1


@pytest.mark.asyncio
async def test_update_integration_accepts_the_credential_mode_alias(monkeypatch, no_credential_writes):
    owner = uuid.uuid4()
    integ = _shared_oauth_row(owner)
    _stub_manageable_gate(monkeypatch, entity=integ)
    _admin_caller(monkeypatch, is_admin=False)
    monkeypatch.setattr(database, "async_session", _session_factory([]))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(owner))
    result = await _call(
        executor, "update_integration", integration_id=str(integ.id), credential_mode="per_user"
    )

    assert not result.is_error, result.content
    assert integ.credential_mode == PER_USER_CREDENTIAL_MODE


@pytest.mark.asyncio
async def test_update_integration_drops_a_previous_owner_token_on_opt_out(
    monkeypatch, no_credential_writes
):
    """The UI switch's rule: a token a previous owner left is nobody's to keep."""
    owner = uuid.uuid4()
    integ = _shared_oauth_row(owner, oauth_token_owner_id=uuid.uuid4())
    _stub_manageable_gate(monkeypatch, entity=integ)
    _admin_caller(monkeypatch, is_admin=True)
    monkeypatch.setattr(database, "async_session", _session_factory([]))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(owner))
    result = await _call(
        executor, "update_integration", integration_id=str(integ.id), share_connected_account=False
    )

    assert not result.is_error, result.content
    assert integ.access_token_enc is None
    assert integ.oauth_token_owner_id is None


@pytest.mark.asyncio
async def test_update_integration_refuses_sharing_an_account_that_is_not_the_callers(
    monkeypatch, no_credential_writes
):
    owner = uuid.uuid4()
    editor = uuid.uuid4()
    integ = _shared_oauth_row(
        owner, oauth_share_acknowledged=False, credential_mode=PER_USER_CREDENTIAL_MODE
    )
    _stub_manageable_gate(monkeypatch, entity=integ)
    _admin_caller(monkeypatch, is_admin=False)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(editor))
    result = await _call(
        executor, "update_integration", integration_id=str(integ.id), share_connected_account=True
    )

    assert SHARE_CONSENT_REFUSED_DETAIL in result.content
    assert integ.credential_mode == PER_USER_CREDENTIAL_MODE
    assert integ.oauth_share_acknowledged is False
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_integration_shares_the_account_again_for_its_owner(
    monkeypatch, no_credential_writes
):
    owner = uuid.uuid4()
    integ = _shared_oauth_row(
        owner, oauth_share_acknowledged=False, credential_mode=PER_USER_CREDENTIAL_MODE
    )
    _stub_manageable_gate(monkeypatch, entity=integ)
    _admin_caller(monkeypatch, is_admin=False)
    monkeypatch.setattr(database, "async_session", _session_factory([]))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(owner))
    result = await _call(
        executor, "update_integration", integration_id=str(integ.id), share_connected_account=True
    )

    assert not result.is_error, result.content
    assert integ.credential_mode == SHARED_CREDENTIAL_MODE
    assert integ.oauth_share_acknowledged is True
    # Lending it again disconnects everyone's own account.
    assert no_credential_writes["delete"] == 1


@pytest.mark.asyncio
async def test_update_integration_without_the_choice_keeps_todays_behaviour(
    monkeypatch, no_credential_writes
):
    owner = uuid.uuid4()
    integ = _shared_oauth_row(
        owner, oauth_share_acknowledged=False, credential_mode=PER_USER_CREDENTIAL_MODE
    )
    _stub_manageable_gate(monkeypatch, entity=integ)
    _admin_caller(monkeypatch, is_admin=False)
    monkeypatch.setattr(database, "async_session", _session_factory([]))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(owner))
    result = await _call(executor, "update_integration", integration_id=str(integ.id), name="Renamed")

    assert not result.is_error, result.content
    # An implicit consent never ends an opt-out.
    assert integ.credential_mode == PER_USER_CREDENTIAL_MODE
    assert integ.name == "Renamed"


@pytest.mark.asyncio
async def test_update_integration_says_when_the_choice_cannot_apply(monkeypatch, no_credential_writes):
    owner = uuid.uuid4()
    integ = _shared_oauth_row(owner, shared_group_ids=[], oauth_share_acknowledged=False)
    _stub_manageable_gate(monkeypatch, entity=integ)
    _admin_caller(monkeypatch, is_admin=False)
    monkeypatch.setattr(database, "async_session", _session_factory([]))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(owner))
    result = await _call(
        executor, "update_integration", integration_id=str(integ.id), share_connected_account=False
    )

    assert "not shared" in result.content
    assert integ.credential_mode == SHARED_CREDENTIAL_MODE


@pytest.mark.asyncio
async def test_update_integration_refuses_an_unreadable_choice_before_any_read(monkeypatch):
    def _no_session():
        raise AssertionError("no session may open")

    monkeypatch.setattr(database, "async_session", _no_session)
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_integration",
        integration_id=str(uuid.uuid4()),
        share_connected_account=True,
        credential_mode="per_user",
    )

    assert "disagree" in result.content


# ---------------------------------------------------------------------------
# get_integration / list_integrations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_integration_reports_the_mode_and_a_count(monkeypatch):
    owner = uuid.uuid4()
    integ = _shared_oauth_row(
        owner,
        shared_group_ids=[],
        oauth_share_acknowledged=False,
        credential_mode=PER_USER_CREDENTIAL_MODE,
        description=None,
        tags=[],
    )

    async def _groups(_db, _uid):
        return []

    async def _counts(_db, ids):
        return {str(i): 3 for i in ids}

    import app.api.v1.helpers as api_helpers

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _groups)
    monkeypatch.setattr(user_credentials_module, "connected_user_counts", _counts)
    monkeypatch.setattr(
        database, "async_session", _session_factory([], execute_results=[integ])
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(owner))
    result = await _call(executor, "get_integration", integration_id=str(integ.id))

    assert "Credential mode: per_user" in result.content
    assert "3 connected their own account" in result.content


def test_the_tool_schemas_carry_the_choice():
    tools, _ = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    by_name = {t.name: t for t in tools}
    for name in ("create_integration", "update_integration"):
        props = by_name[name].input_schema["properties"]
        assert props["share_connected_account"]["type"] == "boolean"
        assert props["credential_mode"]["enum"] == ["shared", "per_user"]
        assert "Don't share my connected account" in by_name[name].description
        # The run-time rule: per-user protects chats, not the owner's runs.
        assert "assignments and flows" in by_name[name].description

