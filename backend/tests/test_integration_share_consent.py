"""Sharing an OAuth integration shares its connected account.

``integration_credentials.apply_oauth_share_consent`` is the one write rule:
a shared OAuth integration records its owner's consent unless the request opts
out, an edit that does not opt out never removes a token, and an integration
that stops being shared is personal again. These tests pin that rule, then
follow a share into the readers that decide who uses the token: the people it
is shared with do, anyone who cannot see it still does not.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest

import app.api.v1.helpers as helpers
import app.core.database as database
import app.services.builtin_tools as builtin_tools_module
import app.services.integration_tools as integration_tools
from app.api.v1.integrations import _requested_credential_share
from app.api.v1.schemas.integration import UpdateIntegrationRequest
from app.models.integration import Integration
from app.services.integration_credentials import (
    NOT_COMPANY_MANAGED_REASON,
    apply_oauth_share_consent,
    is_shared_integration,
    oauth_credentials_usable,
    resolve_session_integration_bindings,
)
from app.services.integration_tools import BindingBasis, ToolBindings
from app.tools.base import ToolDefinition
from tests.integration_credential_fakes import OWNER_ID, FakeSession, make_integration
from tests.test_agent_builder_crud_tools import (
    _admin_caller,
    _call,
    _session_factory,
    _stub_manageable_gate,
)
from tests.test_integration_oauth_sharing_guard import (
    _build_app,
    _connected_oauth,
)
from tests.test_integration_oauth_sharing_guard import _FakeDb as _RestDb

_GROUP = str(uuid.uuid4())
_MEMBER = uuid.uuid4()
_OUTSIDER = uuid.uuid4()
_GRANTEE = uuid.uuid4()

_SHARES = {
    "group": {"shared_group_ids": [_GROUP]},
    "user_grant": {"shared_user_ids": [str(_GRANTEE)]},
    "company_managed": {"is_company_managed": True},
}


def _row(**overrides) -> SimpleNamespace:
    fields = {
        "auth_type": "oauth",
        "is_company_managed": False,
        "shared_group_ids": None,
        "shared_user_ids": None,
        "access_token_enc": "enc-token",
        "refresh_token_enc": "enc-refresh",
        "token_expires_at": "2026-10-01T00:00:00Z",
        "client_id_enc": "enc-client",
        "status": "connected",
        "oauth_share_acknowledged": False,
        "credential_mode": "shared",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("share", sorted(_SHARES))
def test_a_share_records_the_consent_and_keeps_the_token(share):
    row = _row(**_SHARES[share])

    apply_oauth_share_consent(row, share_credentials=None, sharing_changed=True)

    assert row.oauth_share_acknowledged is True
    assert row.access_token_enc == "enc-token"
    assert row.refresh_token_enc == "enc-refresh"
    assert row.status == "connected"


@pytest.mark.parametrize("share", sorted(_SHARES))
def test_the_opt_out_keeps_the_share_and_the_owner_s_own_account(share):
    """The opt-out is stored: the owner keeps their connection, nobody
    else may use it, and everyone else authorizes with their own account."""
    row = _row(**_SHARES[share], oauth_share_acknowledged=True)

    apply_oauth_share_consent(row, share_credentials=False, sharing_changed=False)

    assert is_shared_integration(row)
    assert row.oauth_share_acknowledged is False
    assert row.credential_mode == "per_user"
    assert (row.access_token_enc, row.refresh_token_enc) == ("enc-token", "enc-refresh")
    assert row.status == "connected"


def test_the_opt_out_removes_a_token_a_previous_owner_left():
    """A token kept from before an owner transfer is nobody's to keep."""
    row = _row(**_SHARES["group"], client_id_enc=None, oauth_token_owner_id=uuid.uuid4())

    apply_oauth_share_consent(row, share_credentials=False, sharing_changed=True)

    assert row.access_token_enc is None
    assert row.oauth_token_owner_id is None
    assert row.status == "disconnected"


def test_an_edit_acknowledges_a_shared_token_nobody_consented_for():
    row = _row(**_SHARES["group"])

    apply_oauth_share_consent(row, share_credentials=None, sharing_changed=False)

    assert row.oauth_share_acknowledged is True
    assert row.access_token_enc == "enc-token"


def test_an_edit_leaves_an_opted_out_integration_opted_out():
    row = _row(**_SHARES["group"], access_token_enc=None, refresh_token_enc=None)

    apply_oauth_share_consent(row, share_credentials=None, sharing_changed=False)

    assert row.oauth_share_acknowledged is False


def test_an_explicit_share_records_the_consent_even_before_a_token_exists():
    row = _row(**_SHARES["group"], access_token_enc=None, refresh_token_enc=None)

    apply_oauth_share_consent(row, share_credentials=True, sharing_changed=False)

    assert row.oauth_share_acknowledged is True


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"oauth_share_acknowledged": True}, id="unshared"),
        pytest.param(
            {"auth_type": "api_key", "is_company_managed": True, "oauth_share_acknowledged": True},
            id="not_oauth",
        ),
    ],
)
@pytest.mark.parametrize("choice", [None, True, False])
def test_a_personal_or_non_oauth_integration_carries_no_consent(overrides, choice):
    row = _row(**overrides)

    apply_oauth_share_consent(row, share_credentials=choice, sharing_changed=True)

    assert row.oauth_share_acknowledged is False
    # Nothing is shared, so there is nothing to withhold: the token stays.
    assert row.access_token_enc == "enc-token"


@pytest.mark.parametrize(
    ("payload", "sharing_changed", "acknowledged", "expected"),
    [
        pytest.param({}, False, False, None, id="omitted_edit"),
        pytest.param({}, True, False, False, id="omitted_share_strips_as_on_main"),
        pytest.param({}, True, True, None, id="omitted_share_keeps_a_consent"),
        pytest.param({"keep_oauth_credentials_on_share": None}, True, False, False, id="null_share"),
        pytest.param({"keep_oauth_credentials_on_share": True}, True, False, True, id="true"),
        pytest.param({"keep_oauth_credentials_on_share": False}, False, True, False, id="false_opts_out"),
    ],
)
def test_only_an_explicit_true_shares_the_account(payload, sharing_changed, acknowledged, expected):
    """An omitted choice keeps the baseline meaning."""
    row = SimpleNamespace(oauth_share_acknowledged=acknowledged)
    assert (
        _requested_credential_share(
            UpdateIntegrationRequest(**payload), row, sharing_changed=sharing_changed
        )
        is expected
    )


def test_without_implicit_consent_an_edit_that_makes_no_choice_changes_nothing():
    row = _row(shared_group_ids=["g"])
    apply_oauth_share_consent(
        row, share_credentials=None, sharing_changed=False, implicit_consent=False
    )
    assert row.oauth_share_acknowledged is False
    assert row.access_token_enc == "enc-token"


# ---------------------------------------------------------------------------
# Readers: the chat session override, with the real visibility clause
# ---------------------------------------------------------------------------


@pytest.fixture
def _memberships(monkeypatch):
    """Group memberships stubbed; the visibility clause itself is the real one."""

    async def _group_ids(_db, user_id):
        return [_GROUP] if str(user_id) == str(_MEMBER) else []

    monkeypatch.setattr(helpers, "get_user_group_ids", _group_ids)


def _slack(share: str, *, share_credentials: bool | None = None) -> Integration:
    """The owner's Slack integration, shared as *share* through the write rule."""
    fields = {"user_id": OWNER_ID, "shared_user_ids": None, "access_restricted": False}
    fields.update(_SHARES[share])
    integration = make_integration(**fields)
    apply_oauth_share_consent(
        integration, share_credentials=share_credentials, sharing_changed=True
    )
    return integration


@pytest.mark.parametrize(
    ("share", "acting", "resolved"),
    [
        pytest.param("group", _MEMBER, True, id="group_member"),
        pytest.param("group", _OUTSIDER, False, id="group_outsider"),
        pytest.param("user_grant", _GRANTEE, True, id="grantee"),
        pytest.param("user_grant", _OUTSIDER, False, id="user_grant_outsider"),
        pytest.param("company_managed", _OUTSIDER, True, id="company_managed_anyone"),
    ],
)
async def test_the_people_it_is_shared_with_use_the_token_and_nobody_else(
    _memberships, share, acting, resolved
):
    integration = _slack(share)
    refused: list[dict] = []

    bindings = await resolve_session_integration_bindings(
        FakeSession({Integration: [integration]}),
        str(acting),
        [str(integration.id)],
        refused=refused,
    )

    assert bindings == ({"slack": [str(integration.id)]} if resolved else {})
    # Someone who cannot see the integration is not even told it exists.
    assert refused == []


async def test_a_restricted_company_integration_stays_out_of_reach_of_its_non_audience(
    _memberships,
):
    integration = make_integration(
        user_id=OWNER_ID,
        is_company_managed=True,
        access_restricted=True,
        shared_group_ids=[_GROUP],
        shared_user_ids=None,
    )
    apply_oauth_share_consent(integration, share_credentials=None, sharing_changed=True)
    db = FakeSession({Integration: [integration]})

    assert await resolve_session_integration_bindings(db, str(_MEMBER), [str(integration.id)])
    assert await resolve_session_integration_bindings(db, str(_OUTSIDER), [str(integration.id)]) == {}


async def test_a_company_managed_agent_never_resolves_a_personal_session_integration(
    _memberships,
):
    """#11866: a person's own integration named in a chat selection never reaches a
    company-managed agent; the refusal is said, never silent."""
    own = make_integration(user_id=_MEMBER, is_company_managed=False)
    company = _slack("company_managed")
    db = FakeSession({Integration: [own, company]})
    refused: list[dict] = []

    bindings = await resolve_session_integration_bindings(
        db,
        str(_MEMBER),
        [str(own.id), str(company.id)],
        refused=refused,
        company_managed_only=True,
    )

    assert bindings == {"slack": [str(company.id)]}
    assert [(entry["integration_id"], entry["reason"]) for entry in refused] == [
        (str(own.id), NOT_COMPANY_MANAGED_REASON)
    ]
    # The same selection on a personal agent keeps the person's own integration.
    assert str(own.id) in str(await resolve_session_integration_bindings(db, str(_MEMBER), [str(own.id)]))


async def test_an_opted_out_share_gives_nobody_a_token(_memberships):
    integration = _slack("group", share_credentials=False)

    bindings = await resolve_session_integration_bindings(
        FakeSession({Integration: [integration]}), str(_MEMBER), [str(integration.id)]
    )

    assert bindings == {}
    assert not oauth_credentials_usable(integration, _MEMBER)


# ---------------------------------------------------------------------------
# Readers: a share made through the API reaches the tool funnel
# ---------------------------------------------------------------------------


@pytest.fixture
def _slack_probe(monkeypatch):
    """A stand-in slack builder recording the token each binding resolved to."""
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

    async def _parallel_refresh(_db, _rows):
        return {}, set()

    monkeypatch.setattr(integration_tools, "decrypt_value", lambda value: f"plain:{value}")
    monkeypatch.setitem(integration_tools.PROVIDER_TOOLS, "slack", _builder)
    monkeypatch.setattr(integration_tools, "_parallel_refresh_expiring_tokens", _parallel_refresh)
    return built


async def _member_run(integration: Integration) -> dict:
    context = {"source": "assignment", "user_id": str(_MEMBER)}
    await integration_tools.build_integration_tools(
        FakeSession({Integration: [integration]}),
        ToolBindings({"slack": [str(integration.id)]}, basis=BindingBasis.CALLER_CHECKED, reason="test"),
        context=context,
    )
    return context


async def test_a_group_share_made_through_the_api_lets_the_group_use_the_token(
    monkeypatch, _slack_probe
):
    """A bound credential is used as bound, shared or not; the share is
    what lets the group's own chat turns and selections use the token."""
    owner = SimpleNamespace(
        id=OWNER_ID,
        full_name="Owner",
        role="user",
        is_admin=False,
        oauth_share_dismissed_integration_ids=None,
    )
    rest_db = _RestDb()
    integration = _connected_oauth(owner.id, provider="slack", name="Team Slack")
    rest_db.integrations[str(integration.id)] = integration

    before = await _member_run(integration)
    assert _slack_probe == ["plain:secret-token"]
    assert not before["missing_integration_bindings"]
    assert not oauth_credentials_usable(integration, _MEMBER)

    app = _build_app(rest_db, owner, monkeypatch, group_ids=[])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            f"/api/v1/integrations/{integration.id}",
            json={
                "id": str(integration.id),
                "shared_group_ids": [_GROUP],
                "keep_oauth_credentials_on_share": True,
            },
        )
    assert resp.status_code == 200, resp.text

    after = await _member_run(integration)
    assert _slack_probe == ["plain:secret-token", "plain:secret-token"]
    assert not after["missing_integration_bindings"]
    assert oauth_credentials_usable(integration, _MEMBER)


# ---------------------------------------------------------------------------
# The agent-builder integration tools follow the same rule
# ---------------------------------------------------------------------------


async def test_the_builder_creates_a_company_managed_oauth_integration_that_shares(
    monkeypatch,
):
    import app.api.v1.integration_catalog as integration_catalog_module

    monkeypatch.setattr(
        integration_catalog_module,
        "PROVIDER_CATALOG",
        {"acme": {"auth_type": "oauth", "supported_auth_types": ["oauth"]}},
        raising=False,
    )
    _admin_caller(monkeypatch)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = builtin_tools_module.build_agent_builder_tool(
        SimpleNamespace(), str(uuid.uuid4())
    )
    result = await _call(
        executor,
        "create_integration",
        provider="acme",
        name="Acme",
        client_id="client-id",
        is_company_managed=True,
    )

    assert not result.is_error, result.content
    (integration,) = created[0].added
    assert integration.is_company_managed is True
    assert integration.oauth_share_acknowledged is True


@pytest.mark.parametrize(
    ("token", "acknowledged"),
    [
        pytest.param("ENC(tok)", True, id="holding_a_token"),
        pytest.param(None, False, id="opted_out"),
    ],
)
async def test_a_builder_edit_never_removes_a_shared_token(monkeypatch, token, acknowledged):
    caller = uuid.uuid4()
    integration = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=caller,
        name="Acme",
        provider="acme",
        auth_type="oauth",
        status="connected" if token else "disconnected",
        is_company_managed=False,
        shared_group_ids=[_GROUP],
        shared_user_ids=None,
        access_token_enc=token,
        refresh_token_enc=token,
        oauth_share_acknowledged=False,
    )
    _stub_manageable_gate(monkeypatch, entity=integration)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = builtin_tools_module.build_agent_builder_tool(SimpleNamespace(), str(caller))
    result = await _call(
        executor, "update_integration", integration_id=str(integration.id), name="Acme (renamed)"
    )

    assert not result.is_error, result.content
    assert integration.name == "Acme (renamed)"
    assert integration.access_token_enc == token
    assert integration.oauth_share_acknowledged is acknowledged
    assert created[0].commits == 1
