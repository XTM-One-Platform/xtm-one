"""Sharing an OAuth integration without its connected account.

Sharing an OAuth integration asks its owner whether the people it is shared
with use the connected account (the default) or not (``Don't share my
connected account``). The opt-out is now stored (``credential_mode =
"per_user"``): the owner's account stays theirs - authorizing it again does
not lend it, the reported case - and everyone else authorizes their own on
the same integration, and every run acts with the acting person's. These
tests pin the stored choice, the one resolver, the credential row as a token
holder (it reads its integration's configuration and holds its own tokens),
the tool funnel, the refresh, the sign-in and the chat's session selection.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

import app.api.oauth_callbacks as oauth_callbacks
import app.services.integration_tools as integration_tools
import app.services.integration_user_credentials as user_credentials
import app.services.token_refresh as token_refresh
from app.core.security import decrypt_json, decrypt_value, encrypt_value
from app.models.integration import Integration
from app.models.integration_user_credential import IntegrationUserCredential
from app.services.integration_credentials import (
    OWN_ACCOUNT_NOT_CONNECTED_REASON,
    apply_oauth_share_consent,
    format_unshared_oauth_prompt_note,
    oauth_credentials_usable,
    require_oauth_credentials_usable,
    resolve_session_integration_bindings,
    unavailable_integrations_notice,
)
from app.services.integration_tools import BindingBasis, ToolBindings
from app.tools.base import ToolDefinition
from tests.integration_credential_fakes import (
    COLLEAGUE_ID,
    OWNER_ID,
    FakeSession,
    evaluate,
    make_integration,
)

THIRD_ID = uuid.uuid4()


def _per_user(**overrides) -> Integration:
    fields = {"credential_mode": "per_user", "shared_user_ids": [str(COLLEAGUE_ID), str(THIRD_ID)]}
    fields.update(overrides)
    return make_integration(**fields)


def _credential(integration: Integration, user_id=COLLEAGUE_ID, **overrides) -> IntegrationUserCredential:
    fields = {
        "id": uuid.uuid4(),
        "integration_id": integration.id,
        "user_id": user_id,
        "integration": integration,
        "access_token_enc": f"enc-token-of-{user_id}",
        "refresh_token_enc": None,
        "status": "connected",
    }
    fields.update(overrides)
    return IntegrationUserCredential(**fields)


@pytest.fixture
def _visible(monkeypatch):
    """Everyone in these tests can see the integration, unless a test says otherwise."""
    seen: dict[str, bool] = {}

    async def _visible_to(_db, _integration_id, user_id, _group_ids=None):
        return seen.get(str(user_id), True)

    monkeypatch.setattr(user_credentials, "_visible_to", _visible_to)
    return seen


# ---------------------------------------------------------------------------
# The stored choice: the reported case
# ---------------------------------------------------------------------------


async def test_reauthorizing_a_per_user_integration_never_lends_its_account_again():
    """The owner opted out and authorized again: their new token stays theirs."""
    integration = _per_user(oauth_share_acknowledged=False)

    await oauth_callbacks.acknowledge_share_on_authorize(FakeSession(), integration, str(OWNER_ID))

    assert integration.oauth_share_acknowledged is False
    assert not oauth_credentials_usable(integration, COLLEAGUE_ID)


async def test_a_shared_integration_still_lends_the_account_its_owner_authorizes():
    """``shared`` mode keeps main's behaviour: authorizing a shared integration shares it."""
    integration = make_integration(shared_user_ids=[str(COLLEAGUE_ID)])

    await oauth_callbacks.acknowledge_share_on_authorize(FakeSession(), integration, str(OWNER_ID))

    assert integration.oauth_share_acknowledged is True


async def test_the_reported_case_opting_out_then_authorizing_again_keeps_the_account_the_owner_s():
    """Shared, opted out, re-authorized: the owner's account is still theirs only."""
    integration = make_integration(shared_user_ids=[str(COLLEAGUE_ID)], oauth_share_acknowledged=True)

    apply_oauth_share_consent(integration, share_credentials=False, sharing_changed=False)
    # The opt-out keeps the owner's own token: they do not lose the connection.
    assert (integration.credential_mode, integration.oauth_share_acknowledged) == ("per_user", False)
    assert integration.access_token_enc == "enc-access-token"

    await oauth_callbacks.acknowledge_share_on_authorize(FakeSession(), integration, str(OWNER_ID))

    assert integration.oauth_share_acknowledged is False
    assert not oauth_credentials_usable(integration, COLLEAGUE_ID)


@pytest.mark.parametrize(
    ("choice", "implicit", "expected"),
    [
        (False, False, ("per_user", False)),
        (None, False, ("per_user", False)),
        # An implicit consent (the agent builder's) never ends an opt-out.
        (None, True, ("per_user", False)),
        # Only the explicit choice lends the account again.
        (True, False, ("shared", True)),
    ],
)
def test_only_an_explicit_choice_ends_an_opt_out(choice, implicit, expected):
    integration = _per_user()

    apply_oauth_share_consent(
        integration, share_credentials=choice, sharing_changed=True, implicit_consent=implicit
    )

    assert (integration.credential_mode, integration.oauth_share_acknowledged) == expected
    assert integration.access_token_enc == "enc-access-token"


def test_a_share_that_asks_nothing_opts_out_and_keeps_the_owner_s_token():
    """The REST baseline used to remove an unconsented token."""
    from app.services.integration_credentials import requested_share_choice

    integration = make_integration(shared_user_ids=[str(COLLEAGUE_ID)])
    choice = requested_share_choice(None, integration, sharing_changed=True)

    apply_oauth_share_consent(integration, share_credentials=choice, sharing_changed=True, implicit_consent=False)

    assert (integration.credential_mode, integration.access_token_enc) == ("per_user", "enc-access-token")


def test_an_unshared_or_non_oauth_integration_lends_nothing_and_takes_no_opt_out():
    personal = _per_user(shared_user_ids=None)
    apply_oauth_share_consent(personal, share_credentials=False, sharing_changed=True)
    assert personal.credential_mode == "shared"

    api_key = make_integration(auth_type="api_key", shared_user_ids=[str(COLLEAGUE_ID)])
    apply_oauth_share_consent(api_key, share_credentials=False, sharing_changed=True)
    assert api_key.credential_mode == "shared"


def test_the_stored_account_is_its_holder_s_only():
    integration = _per_user(oauth_share_acknowledged=True, is_company_managed=True)

    assert oauth_credentials_usable(integration, OWNER_ID)
    assert not oauth_credentials_usable(integration, COLLEAGUE_ID)
    # No admin bypass: an admin acts with their own account like anyone else.
    with pytest.raises(HTTPException) as refused:
        require_oauth_credentials_usable(integration, COLLEAGUE_ID, is_admin=True)
    assert refused.value.status_code == 403


# ---------------------------------------------------------------------------
# The credential row as a token holder
# ---------------------------------------------------------------------------


def test_a_credential_reads_its_integration_and_holds_its_own_account():
    integration = _per_user(client_id_enc="enc-client", token_url="https://idp.test/token", scopes="read")
    credential = _credential(integration)

    assert (credential.provider, credential.auth_type, credential.name) == ("slack", "oauth", "Personal Slack")
    assert (credential.client_id_enc, credential.token_url, credential.scopes) == (
        "enc-client",
        "https://idp.test/token",
        "read",
    )
    assert credential.access_token_enc == f"enc-token-of-{COLLEAGUE_ID}"
    assert integration.access_token_enc == "enc-access-token"
    assert credential.oauth_share_acknowledged is False
    with pytest.raises(AttributeError):
        credential.token_url = "https://elsewhere.test/token"


def test_what_a_sign_in_learns_is_the_account_s_and_the_configuration_stays_the_integration_s():
    integration = _per_user(extra_config_enc=encrypt_value(json.dumps({"account_type": "page", "base_url": "https://x"})))
    credential = _credential(integration)

    extra = user_credentials.account_extra(credential)
    extra.update({"organizations": [{"id": "42"}], "organization_id": "42"})
    user_credentials.save_account_extra(credential, extra)

    assert decrypt_json(credential.account_config_enc) == {"organizations": [{"id": "42"}], "organization_id": "42"}
    assert user_credentials.account_extra(credential) == {
        "account_type": "page",
        "base_url": "https://x",
        "organizations": [{"id": "42"}],
        "organization_id": "42",
    }
    assert decrypt_json(integration.extra_config_enc) == {"account_type": "page", "base_url": "https://x"}


def test_a_holder_s_lock_and_back_off_are_its_own():
    integration = _per_user()
    credential = _credential(integration)

    assert user_credentials.holder_ref(integration) == str(integration.id)
    assert user_credentials.holder_ref(credential) == f"user-credential:{credential.id}"
    assert user_credentials.binding_integration_id(credential) == str(integration.id)
    assert user_credentials.integration_of(credential) is integration


async def test_a_holder_is_loaded_back_from_its_reference():
    integration = _per_user()
    credential = _credential(integration)
    db = FakeSession({Integration: [integration], IntegrationUserCredential: [credential]})

    assert await user_credentials.load_token_holder(db, user_credentials.holder_ref(integration)) is integration
    assert await user_credentials.load_token_holder(db, user_credentials.holder_ref(credential)) is credential
    assert await user_credentials.load_token_holder(db, "user-credential:not-a-uuid") is None


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


async def test_shared_mode_always_resolves_the_integration(_visible):
    integration = make_integration(shared_user_ids=[str(COLLEAGUE_ID)])

    assert await user_credentials.resolve_token_holder(FakeSession(), integration, COLLEAGUE_ID) is integration


@pytest.mark.parametrize(
    ("acting", "stored", "visible", "expected"),
    [
        (OWNER_ID, None, True, "integration"),
        (COLLEAGUE_ID, "connected", True, "credential"),
        (COLLEAGUE_ID, "error", True, "credential"),
        (COLLEAGUE_ID, "reauthorize", True, None),
        (COLLEAGUE_ID, None, True, None),
        (COLLEAGUE_ID, "connected", False, None),
        (None, "connected", True, None),
        ("anonymous", "connected", True, None),
    ],
)
async def test_per_user_mode_resolves_the_acting_person_s_own_account(_visible, acting, stored, visible, expected):
    integration = _per_user()
    credentials = [_credential(integration, status=stored)] if stored else []
    _visible[str(COLLEAGUE_ID)] = visible
    db = FakeSession({IntegrationUserCredential: credentials})

    holder = await user_credentials.resolve_token_holder(db, integration, acting)

    assert holder is {"integration": integration, "credential": credentials[0] if credentials else None, None: None}[expected]


async def test_a_token_transferred_with_its_integration_stays_its_connector_s(_visible):
    """After an owner transfer the stored token is whoever connected it."""
    integration = _per_user(oauth_token_owner_id=COLLEAGUE_ID)
    db = FakeSession({IntegrationUserCredential: []})

    assert await user_credentials.resolve_token_holder(db, integration, COLLEAGUE_ID) is integration
    assert await user_credentials.resolve_token_holder(db, integration, OWNER_ID) is None


async def test_the_rest_tooling_acts_with_the_caller_s_own_account(_visible):
    integration = _per_user()
    credential = _credential(integration)
    db = FakeSession({IntegrationUserCredential: [credential]})

    assert await user_credentials.usable_credential_holder(db, integration, COLLEAGUE_ID) is credential
    with pytest.raises(HTTPException) as refused:
        await user_credentials.usable_credential_holder(db, integration, THIRD_ID, is_admin=True)
    assert refused.value.status_code == 403
    assert "connect" in refused.value.detail.lower()


@pytest.mark.parametrize(
    ("user", "integration_kwargs", "expected"),
    [
        (SimpleNamespace(id=COLLEAGUE_ID, role="user"), {}, True),
        (SimpleNamespace(id=OWNER_ID, role="user"), {}, False),
        (SimpleNamespace(id=COLLEAGUE_ID, role="user"), {"credential_mode": "shared"}, False),
        (SimpleNamespace(id=COLLEAGUE_ID, role="user"), {"auth_type": "api_key"}, False),
        (SimpleNamespace(id=OWNER_ID, role="admin"), {}, False),
        # An owner-less company-managed one is connected first by an admin, its holder.
        (SimpleNamespace(id=COLLEAGUE_ID, role="admin"), {"user_id": None, "is_company_managed": True}, False),
        (SimpleNamespace(id=COLLEAGUE_ID, role="user"), {"user_id": None, "is_company_managed": True}, True),
    ],
)
def test_who_signs_in_for_their_own_account(user, integration_kwargs, expected):
    assert user_credentials.signs_in_own_account(user, _per_user(**integration_kwargs)) is expected


# ---------------------------------------------------------------------------
# Ending an opt-out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"share_credentials": True, "sharing_changed": False},
        # Not shared any more: personal again.
        {"share_credentials": None, "sharing_changed": True, "unshare": True},
    ],
)
async def test_lending_the_account_again_or_unsharing_disconnects_the_people_s_own_accounts(monkeypatch, change):
    removed: list[object] = []

    async def _delete(_db, integration_id, *, user_ids=None):
        removed.append(integration_id)
        return 2

    monkeypatch.setattr(user_credentials, "delete_user_credentials", _delete)
    integration = _per_user()
    if change.pop("unshare", False):
        integration.shared_user_ids = None

    await user_credentials.apply_share_choice(FakeSession(), integration, **change)

    assert integration.credential_mode == "shared"
    assert removed == [integration.id]


async def test_an_edit_that_keeps_the_opt_out_keeps_the_people_s_own_accounts(monkeypatch):
    removed: list[object] = []
    monkeypatch.setattr(user_credentials, "delete_user_credentials", AsyncMock(side_effect=removed.append))
    integration = _per_user()

    await user_credentials.apply_share_choice(FakeSession(), integration, share_credentials=False, sharing_changed=True)

    assert integration.credential_mode == "per_user"
    assert removed == []


# ---------------------------------------------------------------------------
# The tool funnel: every run acts with its acting person's account
# ---------------------------------------------------------------------------


@pytest.fixture
def _slack_builder(monkeypatch):
    built: list[str] = []

    def _builder(token, executor, **_kwargs):
        built.append(token)
        return [
            ToolDefinition(
                name=f"slack_probe_{len(built)}",
                description="probe",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    async def _parallel_refresh(_db, rows):
        return {}, set()

    monkeypatch.setattr(integration_tools, "decrypt_value", lambda value: f"plain:{value}")
    monkeypatch.setitem(integration_tools.PROVIDER_TOOLS, "slack", _builder)
    monkeypatch.setattr(integration_tools, "_parallel_refresh_expiring_tokens", _parallel_refresh)
    return built


@pytest.mark.parametrize("source", ["chat", "assignment", "flow", "public_chat"])
async def test_each_person_s_run_acts_with_their_own_account(_visible, _slack_builder, source):
    integration = _per_user()
    colleague, third = _credential(integration), _credential(integration, user_id=THIRD_ID)
    db = FakeSession({Integration: [integration], IntegrationUserCredential: [colleague, third]})

    for acting in (OWNER_ID, COLLEAGUE_ID, THIRD_ID):
        key = "credential_user_id" if source in ("flow", "public_chat") else "user_id"
        await integration_tools.build_integration_tools(
            db, ToolBindings({"slack": [str(integration.id)]}, basis=BindingBasis.CALLER_CHECKED, reason="test"), context={"source": source, key: str(acting)}
        )

    assert _slack_builder == [
        "plain:enc-access-token",
        f"plain:enc-token-of-{COLLEAGUE_ID}",
        f"plain:enc-token-of-{THIRD_ID}",
    ]


@pytest.mark.parametrize("acting", [COLLEAGUE_ID, None])
async def test_a_person_without_their_own_account_gets_no_tools_and_is_told(_visible, _slack_builder, acting):
    """Never the holder's account for someone else - nor for a run with nobody acting."""
    integration = _per_user()
    db = FakeSession({Integration: [integration], IntegrationUserCredential: []})
    context = {"source": "assignment", "user_id": str(acting) if acting else None}

    definitions, _ = await integration_tools.build_integration_tools(
        db, ToolBindings({"slack": [str(integration.id)]}, basis=BindingBasis.CALLER_CHECKED, reason="test"), context=context
    )

    assert definitions == []
    assert _slack_builder == []
    [entry] = context["missing_integration_bindings"]
    assert entry["reason"] == OWN_ACCOUNT_NOT_CONNECTED_REASON
    assert entry["integration_id"] == str(integration.id)


async def test_a_shared_integration_still_resolves_as_bound(_visible, _slack_builder):
    """``shared`` mode keeps the bound account, whoever the run acts as."""
    integration = make_integration(shared_user_ids=[str(COLLEAGUE_ID)])
    db = FakeSession({Integration: [integration]})

    await integration_tools.build_integration_tools(
        db, ToolBindings({"slack": [str(integration.id)]}, basis=BindingBasis.CALLER_CHECKED, reason="test"), context={"source": "assignment", "user_id": str(COLLEAGUE_ID)}
    )

    assert _slack_builder == ["plain:enc-access-token"]


# ---------------------------------------------------------------------------
# The chat: session selection and the notice
# ---------------------------------------------------------------------------


async def test_the_session_selection_keeps_a_per_user_integration_for_a_connected_person():
    integration = _per_user()
    db = FakeSession({Integration: [integration], IntegrationUserCredential: [_credential(integration)]})
    refused: list[dict] = []

    bindings = await resolve_session_integration_bindings(db, COLLEAGUE_ID, [str(integration.id)], refused=refused)

    assert bindings == {"slack": [str(integration.id)]}
    assert refused == []


async def test_the_session_selection_tells_an_unconnected_person_to_connect():
    integration = _per_user()
    db = FakeSession({Integration: [integration], IntegrationUserCredential: []})
    refused: list[dict] = []

    bindings = await resolve_session_integration_bindings(db, THIRD_ID, [str(integration.id)], refused=refused)

    assert bindings == {}
    assert [entry["reason"] for entry in refused] == [OWN_ACCOUNT_NOT_CONNECTED_REASON]


def test_the_reply_and_the_model_are_told_where_to_connect():
    integration = _per_user(name="SaaS Manager")
    from app.services.integration_credentials import own_account_not_connected_binding

    entry = own_account_not_connected_binding(integration)

    [item] = unavailable_integrations_notice([entry])
    assert item == {
        "provider": "slack",
        "reason": OWN_ACCOUNT_NOT_CONNECTED_REASON,
        "name": "SaaS Manager",
        "integration_id": str(integration.id),
    }
    note = format_unshared_oauth_prompt_note([entry])
    assert '"SaaS Manager"' in note
    assert "connect their account" in note


# ---------------------------------------------------------------------------
# Refresh: a person's refresh is theirs
# ---------------------------------------------------------------------------


def _refreshable_credential() -> tuple[Integration, IntegrationUserCredential]:
    integration = _per_user(
        client_id_enc=encrypt_value("client-id"),
        token_url="https://idp.test/token",
        refresh_token_enc=encrypt_value("owner-refresh"),
    )
    credential = _credential(
        integration,
        access_token_enc=encrypt_value("member-access"),
        refresh_token_enc=encrypt_value("member-refresh"),
    )
    return integration, credential


def _http(response: httpx.Response) -> SimpleNamespace:
    return SimpleNamespace(post=AsyncMock(return_value=response))


async def test_a_person_s_refresh_presents_and_stores_their_own_tokens(monkeypatch):
    monkeypatch.setattr(token_refresh, "_token_endpoint_allowed", AsyncMock(return_value=True))
    integration, credential = _refreshable_credential()
    owner_access = integration.access_token_enc
    request = httpx.Request("POST", "https://idp.test/token")
    http = _http(httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}, request=request))

    assert await token_refresh._do_refresh_integration(credential, http) is True

    sent = http.post.await_args.kwargs
    assert "member-refresh" in json.dumps(sent, default=str)
    assert decrypt_value(credential.access_token_enc) == "new-access"
    assert decrypt_value(credential.refresh_token_enc) == "new-refresh"
    assert credential.token_expires_at is not None
    assert integration.access_token_enc == owner_access
    assert decrypt_value(integration.refresh_token_enc) == "owner-refresh"


async def test_a_person_s_failed_refresh_backs_off_and_marks_only_their_own(monkeypatch):
    monkeypatch.setattr(token_refresh, "_token_endpoint_allowed", AsyncMock(return_value=True))
    cooldowns: list[str] = []

    async def _cooldown(key, *_args, **_kwargs):
        cooldowns.append(key)

    monkeypatch.setattr(token_refresh, "start_refresh_cooldown", _cooldown)
    integration, credential = _refreshable_credential()
    request = httpx.Request("POST", "https://idp.test/token")

    ok = await token_refresh._do_refresh_integration(credential, _http(httpx.Response(503, text="busy", request=request)))
    assert ok is False
    assert cooldowns == [token_refresh.refresh_cooldown_key(f"user-credential:{credential.id}")]
    assert (credential.status, integration.status) == ("error", "connected")

    dead = httpx.Response(400, json={"error": "invalid_grant"}, request=request)
    assert await token_refresh._do_refresh_integration(credential, _http(dead)) is False
    assert (credential.status, integration.status) == ("reauthorize", "connected")


# ---------------------------------------------------------------------------
# Sign-in: a person's authorization lands on their own credential
# ---------------------------------------------------------------------------


async def test_a_person_s_sign_in_lands_on_their_credential_and_leaves_the_integration_alone(monkeypatch):
    integration = _per_user(scopes="read write", oauth_share_acknowledged=False)
    credential = _credential(integration, access_token_enc=None)
    monkeypatch.setattr(oauth_callbacks, "oauth_exchange_spec", AsyncMock(return_value=None))
    monkeypatch.setattr(
        oauth_callbacks,
        "exchange_code_for_tokens_with_integration",
        AsyncMock(return_value={"access_token": "member-access", "refresh_token": "member-refresh", "expires_in": 3600}),
    )
    monkeypatch.setattr(oauth_callbacks, "upsert_user_credential", AsyncMock(return_value=credential))
    monkeypatch.setattr(oauth_callbacks, "log_audit", AsyncMock())
    monkeypatch.setattr(token_refresh, "clear_refresh_cooldown", AsyncMock())
    state = {
        "integration_id": str(integration.id),
        "user_id": str(COLLEAGUE_ID),
        "provider": "slack",
        "credential": "user",
    }

    response = await oauth_callbacks._complete_integration_callback(integration, state, "code", FakeSession())

    assert response.status_code in (302, 307)
    assert decrypt_value(credential.access_token_enc) == "member-access"
    assert decrypt_value(credential.refresh_token_enc) == "member-refresh"
    assert credential.status == "connected" and credential.token_expires_at is not None
    assert integration.access_token_enc == "enc-access-token"
    assert integration.oauth_share_acknowledged is False
    assert integration.scopes == "read write"
    token_refresh.clear_refresh_cooldown.assert_awaited_once_with(
        token_refresh.refresh_cooldown_key(f"user-credential:{credential.id}")
    )


# ---------------------------------------------------------------------------
# Per-user (template) bindings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("acting", "eligible"), [(COLLEAGUE_ID, True), (THIRD_ID, False)])
def test_a_company_per_user_integration_resolves_per_user_for_whoever_connected_their_account(acting, eligible):
    """A per-user binding finds the company integration on which the person
    connected their own account; the tool build then acts with that account."""
    integration = _per_user(is_company_managed=True, shared_user_ids=None, access_restricted=False)
    rows = {Integration: [integration], IntegrationUserCredential: [_credential(integration)]}

    clause = integration_tools.template_candidate_clause(acting, [])

    assert (evaluate(clause, integration, rows) is True) is eligible


# ---------------------------------------------------------------------------
# Audit follow-ups
# ---------------------------------------------------------------------------


async def test_the_openapi_connectivity_probe_tests_the_tester_s_own_account(monkeypatch):
    """The probe builds the tools as the person who clicked Test: on an
    integration shared without its account, theirs - never nobody's."""
    from app.integrations.connectivity import ProbeContext
    from app.integrations.providers import openapi as openapi_provider

    integration = _per_user(provider="openapi")
    seen: dict[str, object] = {}

    async def _spec(_db, integration_id, **_kwargs):
        seen["spec_for"] = str(integration_id)
        return SimpleNamespace(active_tools=["t"], auth_option=None)

    async def _build(_db, bindings, context=None):
        seen["context"] = context
        return [], SimpleNamespace(tool_names=set())

    monkeypatch.setattr(openapi_provider, "get_spec", _spec)
    monkeypatch.setattr(openapi_provider, "effective_tools", lambda tools, _option: tools)
    monkeypatch.setattr(
        openapi_provider, "pick_probe_tool", lambda _tools: {"name": "probe", "execution": {"method": "GET", "path": "/"}}
    )
    monkeypatch.setattr("app.services.integration_tools.build_integration_tools", _build)
    ctx = ProbeContext(
        provider="openapi", name="SaaS Manager", token=None, extra={}, db=FakeSession(),
        integration=integration, acting_user_id=str(COLLEAGUE_ID),
    )

    await openapi_provider.check_connectivity(ctx)

    assert seen["spec_for"] == str(integration.id)
    assert seen["context"]["credential_user_id"] == str(COLLEAGUE_ID)


@pytest.mark.parametrize(("visible", "expected"), [(True, True), (False, False)])
async def test_an_own_account_is_offered_only_where_the_person_sees_the_integration(_visible, visible, expected):
    """An admin reaching an integration through the admin bypass is not offered
    an own-account sign-in the callback and the runs would refuse."""
    _visible[str(COLLEAGUE_ID)] = visible
    user = SimpleNamespace(id=COLLEAGUE_ID, role="admin")

    assert await user_credentials.may_connect_own_account(FakeSession(), user, _per_user()) is expected


def test_a_transfer_of_an_integration_shared_without_its_account_drops_the_previous_owner_s_token():
    """Its account is its owner's only: the new owner connects their own."""
    from app.services.integration_credentials import drop_previous_owner_token

    integration = _per_user(oauth_token_owner_id=OWNER_ID, user_id=COLLEAGUE_ID, client_id_enc="enc-client")

    assert drop_previous_owner_token(integration) is True
    assert integration.access_token_enc is None and integration.oauth_token_owner_id is None
    assert integration.status == "configured"
    assert user_credentials.credential_holder_id(integration) == COLLEAGUE_ID
    assert drop_previous_owner_token(_per_user()) is False


@pytest.mark.parametrize(
    ("status", "access", "eligible"),
    [("connected", "enc", True), ("error", "enc", True), ("reauthorize", "enc", False), ("connected", None, False)],
)
def test_a_template_candidate_needs_a_usable_own_account(status, access, eligible):
    integration = _per_user(is_company_managed=True, shared_user_ids=None, access_restricted=False)
    credential = _credential(integration, status=status, access_token_enc=access, refresh_token_enc=None)
    rows = {Integration: [integration], IntegrationUserCredential: [credential]}

    clause = integration_tools.template_candidate_clause(COLLEAGUE_ID, [])

    assert (evaluate(clause, integration, rows) is True) is eligible
    assert user_credentials.credential_connected(credential) is eligible


def test_the_merged_extra_config_is_computed_once_per_stored_pair():
    integration = _per_user(extra_config_enc=encrypt_value(json.dumps({"base_url": "https://x"})))
    credential = _credential(integration, account_config_enc=encrypt_value(json.dumps({"cloud_id": "c"})))

    first = credential.extra_config_enc
    assert credential.extra_config_enc is first
    assert decrypt_json(first) == {"base_url": "https://x", "cloud_id": "c"}
    credential.account_config_enc = encrypt_value(json.dumps({"cloud_id": "d"}))
    assert decrypt_json(credential.extra_config_enc)["cloud_id"] == "d"


@pytest.mark.parametrize(
    ("provider", "implicit"),
    [
        pytest.param("openapi", False, id="openapi_pkce"),
        pytest.param("x", False, id="x_pkce"),
        pytest.param("openapi", True, id="openapi_implicit"),
        pytest.param("notion", False, id="no_pkce"),
    ],
)
async def test_an_own_account_sign_in_keeps_its_marker_in_the_oauth_state(monkeypatch, provider, implicit):
    """The callback stores a person's tokens on their own credential only when
    the state says so: the PKCE rewrite of the state must keep the marker
    (an OpenAPI member's sign-in otherwise came back as the integration's own
    authorization, refused 404)."""
    import app.integrations.oauth as oauth

    stored: dict[str, str] = {}

    class _Redis:
        async def set(self, key, value, ex=None):
            stored[key] = value

    async def _client():
        return _Redis()

    monkeypatch.setattr(oauth, "redis_client", _client)
    integration = _per_user(
        provider=provider,
        client_id_enc=encrypt_value("client"),
        authorize_url="https://saas.example/oauth/authorize",
        scopes="read",
    )

    await oauth.build_authorize_url_for_integration(
        integration, str(COLLEAGUE_ID), implicit=implicit, own_account=True
    )

    (state,) = (json.loads(v) for v in stored.values())
    assert state["credential"] == user_credentials.USER_CREDENTIAL_STATE
    assert state["user_id"] == str(COLLEAGUE_ID)
    assert ("code_verifier" in state) is (provider in {"openapi", "x"} and not implicit)


# ---------------------------------------------------------------------------
# Review follow-ups
# ---------------------------------------------------------------------------


def _owner_page_integration() -> Integration:
    """A Facebook page-mode integration whose owner's sign-in found their page."""
    return _per_user(
        provider="facebook",
        extra_config_enc=encrypt_value(
            json.dumps(
                {
                    "account_type": "page",
                    "pages": [{"id": "owner-page"}],
                    "page_id": "owner-page",
                    "page_name": "Owner's page",
                    "page_access_token": "owner-page-token",
                }
            )
        ),
    )


def test_a_person_s_credential_never_reads_the_owner_s_account_keys():
    """A member whose sign-in found no page (``/me/accounts`` failed) has none:
    never the owner's page or its token, which would post as the owner."""
    integration = _owner_page_integration()
    credential = _credential(integration)

    assert user_credentials.account_extra(credential) == {"account_type": "page"}
    user_credentials.save_account_extra(credential, user_credentials.account_extra(credential))

    assert credential.account_config_enc is None
    assert user_credentials.account_extra(credential) == {"account_type": "page"}
    assert decrypt_json(integration.extra_config_enc)["page_access_token"] == "owner-page-token"


def test_a_person_s_account_keys_are_theirs_even_when_they_match_the_owner_s():
    integration = _owner_page_integration()
    credential = _credential(integration)

    extra = user_credentials.account_extra(credential)
    extra.update({"pages": [{"id": "owner-page"}], "page_id": "owner-page", "page_access_token": "member-page-token"})
    user_credentials.save_account_extra(credential, extra)

    assert decrypt_json(credential.account_config_enc) == {
        "pages": [{"id": "owner-page"}],
        "page_id": "owner-page",
        "page_access_token": "member-page-token",
    }


def test_every_key_a_sign_in_learns_is_declared_as_an_account_key():
    """Tripwire: a key the OAuth callback writes about the signed-in account is
    in a provider's ``account_config_keys``, or a member would read the owner's."""
    import inspect
    import re

    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    written = set(re.findall(r'extra\["(\w+)"\] =', inspect.getsource(oauth_callbacks)))
    declared = {key for entry in PROVIDER_CATALOG.values() for key in entry.get("account_config_keys", ())}

    assert written
    assert written <= declared, sorted(written - declared)


def test_a_per_user_template_pick_goes_by_the_acting_person_s_status():
    """Tripwire: a query that applies ``template_candidate_filter`` (which admits
    a company-managed ``per_user`` row for a member with their own account)
    gates on ``connected_for_clause``, never the row's own status, or the
    member is refused while its holder is disconnected."""
    import ast
    from pathlib import Path

    def _row_status_gate(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "status"
            and any(isinstance(c, ast.Constant) and c.value == "connected" for c in node.comparators)
        )

    app_dir = Path(user_credentials.__file__).resolve().parents[1]
    offenders, scanned = [], 0
    for path in sorted(app_dir.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "template_candidate_filter(" not in source:
            continue
        for func in ast.walk(ast.parse(source)):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef) or func.name == "template_candidate_filter":
                continue
            calls = [n for n in ast.walk(func) if isinstance(n, ast.Call)]
            if not any(getattr(c.func, "id", getattr(c.func, "attr", None)) == "template_candidate_filter" for c in calls):
                continue
            scanned += 1
            for call in calls:
                if getattr(call.func, "attr", None) == "where" and any(_row_status_gate(a) for a in call.args):
                    offenders.append(f"{path.relative_to(app_dir)}:{call.lineno} {func.name}")

    assert scanned
    assert not offenders, offenders


@pytest.mark.parametrize(
    ("acting", "holder_kwargs", "expected"),
    [
        pytest.param(COLLEAGUE_ID, {}, True, id="member_may_connect"),
        pytest.param(COLLEAGUE_ID, {"oauth_token_owner_id": COLLEAGUE_ID}, False, id="member_became_the_holder"),
        pytest.param(COLLEAGUE_ID, {"credential_mode": "shared"}, False, id="account_lent_again"),
        pytest.param(OWNER_ID, {}, False, id="the_holder_itself"),
    ],
)
async def test_an_own_account_callback_rechecks_who_may_connect(_visible, acting, holder_kwargs, expected):
    """The rule ``/oauth/start`` applied is applied again when the sign-in comes
    back: the holder or the share may have changed within the state's TTL."""
    from app.models.user import User

    integration = _per_user(**holder_kwargs)
    users = [User(id=OWNER_ID, email="o@example.com", role="user"), User(id=COLLEAGUE_ID, email="c@example.com", role="user")]
    session = FakeSession({Integration: [integration], User: users})

    if expected:
        assert await oauth_callbacks._own_account_integration(session, str(integration.id), str(acting)) is integration
    else:
        with pytest.raises(HTTPException) as refused:
            await oauth_callbacks._own_account_integration(session, str(integration.id), str(acting))
        assert refused.value.status_code == 404


@pytest.mark.parametrize(
    ("status", "acting", "own_status", "expected"),
    [
        pytest.param("configured", COLLEAGUE_ID, "connected", True, id="member_connected_holder_not"),
        pytest.param("configured", COLLEAGUE_ID, "reauthorize", False, id="member_must_reconnect"),
        pytest.param("configured", THIRD_ID, "connected", False, id="someone_else_s_account"),
        pytest.param("configured", OWNER_ID, None, False, id="holder_not_connected"),
        pytest.param("connected", OWNER_ID, None, True, id="holder_connected"),
        pytest.param("configured", None, "connected", False, id="no_acting_person"),
    ],
)
def test_a_reader_goes_by_the_acting_person_s_own_status(status, acting, own_status, expected):
    """A member who connected their own account is served while the
    integration's own status is not ``connected``."""
    integration = _per_user(status=status, access_token_enc=None)
    rows = {Integration: [integration], IntegrationUserCredential: []}
    if own_status is not None:
        rows[IntegrationUserCredential].append(_credential(integration, status=own_status))

    clause = user_credentials.connected_for_clause(acting)

    assert (evaluate(clause, integration, rows) is True) is expected


def test_a_shared_integration_reads_its_own_status_only():
    integration = make_integration(status="configured")
    rows = {Integration: [integration], IntegrationUserCredential: [_credential(integration)]}

    assert evaluate(user_credentials.connected_for_clause(COLLEAGUE_ID), integration, rows) is not True


@pytest.mark.parametrize(
    ("holder", "expected"),
    [
        pytest.param(make_integration(status="connected"), True, id="integration_connected"),
        pytest.param(make_integration(status="configured"), False, id="integration_configured"),
        pytest.param(_credential(_per_user(status="configured")), True, id="own_account_connected"),
        pytest.param(_credential(_per_user(), status="reauthorize"), False, id="own_account_to_reconnect"),
        pytest.param(None, False, id="none"),
    ],
)
def test_a_holder_s_status_is_its_own(holder, expected):
    assert user_credentials.holder_connected(holder) is expected


@pytest.mark.parametrize(
    ("own", "status", "has_token"),
    [
        pytest.param(None, "configured", False, id="not_connected_yet"),
        pytest.param({}, "connected", True, id="connected"),
        pytest.param({"status": "reauthorize"}, "reauthorize", True, id="to_reconnect"),
        pytest.param({"access_token_enc": None}, "configured", False, id="no_tokens_left"),
    ],
)
def test_a_member_sees_their_own_account_never_the_holder_s(own, status, has_token):
    """The integration page's status, token and scopes are the member's own
    account's: the holder's connected token is not reported."""
    from datetime import UTC, datetime

    from app.api.v1 import integrations as integrations_api

    now = datetime.now(UTC)
    integration = _per_user(
        auth_type="oauth", status="connected", access_token_enc="enc-owner", scopes="read write",
        created_at=now, updated_at=now,
    )
    credential = _credential(integration, **own) if own is not None else None
    member = integrations_api._to_response(
        integration,
        can_manage=False,
        can_authorize=False,
        uses_own_credential=True,
        my_credential=integrations_api._credential_status(credential) if credential is not None else None,
        own_account_status=integrations_api._own_account_status(credential),
    )
    holder = integrations_api._to_response(integration, can_manage=True, can_authorize=True)

    assert (member.status, member.has_access_token, member.scopes, member.required_scopes) == (status, has_token, None, None)
    assert (holder.status, holder.has_access_token, holder.scopes) == ("connected", True, "read write")
