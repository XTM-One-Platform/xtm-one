"""A pipeline tool ref loads the integration it pins for whoever may use it.

A prepare / finalize tool ref may pin an ``integration_id``. The pin is checked
when it is WRITTEN (``integration_credentials.pipeline_refs_refusal``: a writer
may only pin an integration they can see, ``test_pipeline_ref_writes``). At run
time a person loads it only when they may use it as a direct binding
(``shadow_invisible_bindings``: visible to them, or lent by the agent), the
agent's owner included, since a pin copied with an agent names another owner's
integration; a run with no person is checked as the owner only when it is the
owner's own (``owner_run``), and keeps no pin otherwise. A withheld pin is
replaced by the person's own integration of the provider, unless each
integration of the provider is a different API. A pin naming no connected
integration falls back to per-user resolution for the provider.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.integration import Integration
from app.services import agent_chat
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
    ("user_id", "owner_run"),
    [
        pytest.param(str(OWNER_ID), False, id="the_owner"),
        pytest.param("anonymous", True, id="anonymous_owner_run"),
    ],
)
@pytest.mark.parametrize("auth_type", ["api_key", "oauth"])
async def test_the_owner_s_own_run_loads_the_pin_by_id(_build, user_id, owner_run, auth_type):
    pinned = make_integration(auth_type=auth_type)
    mine = make_integration(user_id=COLLEAGUE_ID, auth_type="api_key")

    loaded = await agent_chat._load_pipeline_tool_standalone(
        FakeSession({Integration: [pinned, mine]}), _ref(str(pinned.id)), user_id, _AGENT, owner_run=owner_run
    )

    assert loaded is not None
    assert dict(_build.await_args.args[1]) == {"slack": [str(pinned.id)]}
    assert "credential_user_id" not in _build.await_args.kwargs["context"]


@pytest.mark.parametrize("auth_type", ["api_key", "oauth"])
async def test_a_turn_with_no_person_that_is_not_the_owner_s_own_keeps_no_pin(_build, auth_type):
    pinned = make_integration(auth_type=auth_type)
    mine = make_integration(user_id=COLLEAGUE_ID, auth_type="api_key")

    loaded = await agent_chat._load_pipeline_tool_standalone(
        FakeSession({Integration: [pinned, mine]}), _ref(str(pinned.id)), "anonymous", _AGENT
    )

    # Nothing company-wide replaces it: the colleague's own is not eligible.
    assert loaded is None


@pytest.mark.parametrize("auth_type", ["api_key", "oauth"])
async def test_a_pin_someone_else_may_not_use_gives_them_their_own(_build, auth_type):
    """The owner's pin the turn's user cannot see, a personal OAuth token included, is not used."""
    pinned = make_integration(auth_type=auth_type)
    mine = make_integration(user_id=COLLEAGUE_ID, auth_type="api_key")

    loaded = await agent_chat._load_pipeline_tool_standalone(
        FakeSession({Integration: [pinned, mine]}), _ref(str(pinned.id)), str(COLLEAGUE_ID), _AGENT
    )

    assert loaded is not None
    assert dict(_build.await_args.args[1]) == {"slack": [str(mine.id)]}


async def test_a_pin_naming_no_connected_integration_falls_back_to_the_user_s_own(_build):
    gone = make_integration(auth_type="api_key", status="disconnected")
    mine = make_integration(user_id=COLLEAGUE_ID)

    await agent_chat._load_pipeline_tool_standalone(
        FakeSession({Integration: [gone, mine]}),
        _ref(str(gone.id)),
        str(COLLEAGUE_ID),
        _AGENT,
    )

    assert dict(_build.await_args.args[1]) == {"slack": [str(mine.id)]}


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
