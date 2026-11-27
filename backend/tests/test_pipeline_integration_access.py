"""A pipeline tool ref loads the integration it pins for whoever may use it.

A prepare / finalize tool ref may pin an ``integration_id``. The pin is checked
when it is WRITTEN (``integration_credentials.pipeline_refs_refusal``: a writer
may only pin an integration they can see, ``test_pipeline_ref_writes``). At run
time the agent's owner, a run with no person (the owner's own), and everyone
the agent is shared with (sharing an agent lends what it binds: maintainer
decision of 2026-09-27, #2568) load the pin by id, as bound. Anyone else (an
administrator evaluating an agent nobody shared with them, #683) loads it only
when they may use it as a direct binding (``shadow_invisible_bindings``: visible
to them, or lent by the agent), and otherwise gets their own integration of the
provider. A pin naming no connected integration falls back to per-user
resolution for the provider.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.integration import Integration
from app.services import agent_chat
from app.services.integration_tools import BindingBasis
from app.tools.base import CallableToolExecutor, ToolDefinition
from tests.integration_credential_fakes import COLLEAGUE_ID, OWNER_ID, FakeSession, make_integration

_AGENT = SimpleNamespace(id=uuid.uuid4(), name="Pipeline agent", user_id=OWNER_ID)


def _ref(integration_id: str) -> dict:
    return {
        "source": "integration",
        "provider": "slack",
        "tool_name": "slack_post",
        "integration_id": integration_id,
    }


@pytest.fixture
def _build(monkeypatch):
    definition = ToolDefinition(
        name="slack_post", description="post", input_schema={"type": "object", "properties": {}}
    )
    build = AsyncMock(return_value=([definition], CallableToolExecutor()))
    monkeypatch.setattr("app.services.integration_tools.build_integration_tools", build)
    return build


@pytest.mark.parametrize(
    "user_id",
    [
        pytest.param(str(OWNER_ID), id="the_owner"),
        pytest.param("anonymous", id="anonymous_turn"),
    ],
)
@pytest.mark.parametrize("auth_type", ["api_key", "oauth"])
async def test_the_owner_s_own_run_loads_the_pin_by_id(_build, user_id, auth_type):
    pinned = make_integration(auth_type=auth_type)
    mine = make_integration(user_id=COLLEAGUE_ID, auth_type="api_key")

    loaded = await agent_chat._load_pipeline_tool_standalone(
        FakeSession({Integration: [pinned, mine]}), _ref(str(pinned.id)), user_id, _AGENT
    )

    assert loaded is not None
    assert _build.await_args.args[1] == {"slack": [str(pinned.id)]}
    assert "credential_user_id" not in _build.await_args.kwargs["context"]


@pytest.mark.parametrize("auth_type", ["api_key", "oauth"])
async def test_a_pin_someone_else_may_not_use_gives_them_their_own(_build, auth_type):
    """The owner's pin the turn's user cannot see, a personal OAuth token included, is not used."""
    pinned = make_integration(auth_type=auth_type)
    mine = make_integration(user_id=COLLEAGUE_ID, auth_type="api_key")

    loaded = await agent_chat._load_pipeline_tool_standalone(
        FakeSession({Integration: [pinned, mine]}), _ref(str(pinned.id)), str(COLLEAGUE_ID), _AGENT
    )

    assert loaded is not None
    assert _build.await_args.args[1] == {"slack": [str(mine.id)]}


_GROUP_ID = str(uuid.uuid4())


def _shared(**grants) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), name="Shared agent", user_id=OWNER_ID, **grants)


@pytest.mark.parametrize(
    "agent",
    [
        pytest.param(_shared(shared_user_ids=[str(COLLEAGUE_ID)]), id="shared_with_the_person"),
        pytest.param(_shared(shared_group_ids=[_GROUP_ID]), id="shared_with_their_group"),
        pytest.param(_shared(is_company_managed=True), id="published_to_everyone"),
    ],
)
@pytest.mark.parametrize("auth_type", ["api_key", "oauth"])
async def test_a_person_the_agent_is_shared_with_loads_the_pin_as_bound(_build, monkeypatch, caplog, agent, auth_type):
    """Sharing an agent lends what it binds (#2568): the exact pinned integration,
    lent or not, never the person's own and never withheld."""
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[_GROUP_ID]))
    pinned = make_integration(auth_type=auth_type)
    mine = make_integration(user_id=COLLEAGUE_ID, auth_type="api_key")

    with caplog.at_level(logging.INFO, logger=agent_chat.logger.name):
        loaded = await agent_chat._load_pipeline_tool_standalone(
            FakeSession({Integration: [pinned, mine]}), _ref(str(pinned.id)), str(COLLEAGUE_ID), agent
        )

    assert loaded is not None
    bindings = _build.await_args.args[1]
    assert bindings == {"slack": [str(pinned.id)]}
    assert bindings.basis is BindingBasis.SHARED_AS_BOUND
    assert "withheld" not in caplog.text


async def test_an_access_restricted_company_agent_is_not_shared_by_publication(_build, monkeypatch):
    """``access_restricted`` withdraws the everyone tier: only its grants share it."""
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[]))
    pinned = make_integration(auth_type="oauth")
    mine = make_integration(user_id=COLLEAGUE_ID, auth_type="api_key")
    agent = _shared(is_company_managed=True, access_restricted=True)

    await agent_chat._load_pipeline_tool_standalone(
        FakeSession({Integration: [pinned, mine]}), _ref(str(pinned.id)), str(COLLEAGUE_ID), agent
    )

    assert _build.await_args.args[1] == {"slack": [str(mine.id)]}


async def test_the_owner_s_pin_keeps_the_owner_run_basis_with_no_person(_build):
    pinned = make_integration(auth_type="oauth")

    await agent_chat._load_pipeline_tool_standalone(
        FakeSession({Integration: [pinned]}),
        _ref(str(pinned.id)),
        "anonymous",
        _shared(shared_user_ids=[str(COLLEAGUE_ID)]),
    )

    assert _build.await_args.args[1].basis is BindingBasis.OWNER_RUN


async def test_a_pin_naming_no_connected_integration_falls_back_to_the_user_s_own(_build):
    gone = make_integration(auth_type="api_key", status="disconnected")
    mine = make_integration(user_id=COLLEAGUE_ID)

    await agent_chat._load_pipeline_tool_standalone(
        FakeSession({Integration: [gone, mine]}),
        _ref(str(gone.id)),
        str(COLLEAGUE_ID),
        _AGENT,
    )

    assert _build.await_args.args[1] == {"slack": [str(mine.id)]}


async def test_a_pin_naming_nothing_with_nothing_of_the_user_s_own_builds_nothing(_build):
    loaded = await agent_chat._load_pipeline_tool_standalone(
        FakeSession({Integration: []}), _ref(str(uuid.uuid4())), str(COLLEAGUE_ID), _AGENT
    )

    assert loaded is None
    _build.assert_not_awaited()


async def test_a_withheld_openapi_pin_never_falls_back_to_another_api(_build):
    """Each OpenAPI integration is a different API: the person's own is no stand-in."""
    from app.services.openapi_import.service import OPENAPI_PROVIDER

    pinned = make_integration(auth_type="api_key", provider=OPENAPI_PROVIDER)
    mine = make_integration(user_id=COLLEAGUE_ID, auth_type="api_key", provider=OPENAPI_PROVIDER)
    ref = {**_ref(str(pinned.id)), "provider": OPENAPI_PROVIDER}

    loaded = await agent_chat._load_pipeline_tool_standalone(
        FakeSession({Integration: [pinned, mine]}), ref, str(COLLEAGUE_ID), _AGENT
    )

    assert loaded is None
    _build.assert_not_awaited()
