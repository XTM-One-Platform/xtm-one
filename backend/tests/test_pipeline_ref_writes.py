"""A pinned pipeline integration is vetted when it is saved.

The pipeline loads a pinned ``integration_id`` by id and runs it with its
stored credentials whoever the turn's user is, as on main: nothing is withheld
at run time. Without a save-time check an agent's owner could pin a
colleague's integration id, share the agent with that colleague, and run with
their credentials.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from app.models.integration import Integration
from app.services.integration_credentials import (
    PIPELINE_REF_NOT_VISIBLE_DETAIL,
    pipeline_ref_integration_ids,
    pipeline_refs_refusal,
)
from tests.integration_credential_fakes import (
    COLLEAGUE_ID,
    OWNER_ID,
    FakeSession,
    make_integration,
)

pytestmark = pytest.mark.asyncio


def _ref(integration_id: str) -> dict:
    return {"source": "integration", "provider": "slack", "integration_id": integration_id}


def _db():
    own = make_integration(auth_type="api_key", name="Owner's")
    colleagues = make_integration(auth_type="api_key", user_id=COLLEAGUE_ID, name="Colleague's")
    return FakeSession({Integration: [own, colleagues]}), own, colleagues


async def test_the_owner_cannot_pin_a_colleague_s_integration():
    db, _own, colleagues = _db()
    refusal = await pipeline_refs_refusal(
        db, OWNER_ID, is_admin=False, refs=[_ref(str(colleagues.id))], stored=[]
    )
    assert refusal == PIPELINE_REF_NOT_VISIBLE_DETAIL


async def test_worked_on_main_own_stored_and_dangling_pins_save():
    """The writer's own integration, a pin the agent already held (placed by a
    co-editor), an id naming no integration (the pipeline falls back to scope
    resolution) and a non-integration ref all save as before."""
    db, own, colleagues = _db()
    refs = [
        _ref(str(own.id)),
        _ref(str(colleagues.id).upper()),
        _ref(str(uuid.uuid4())),
        {"source": "mcp", "integration_id": str(colleagues.id)},
        None,
    ]
    assert (
        await pipeline_refs_refusal(
            db, OWNER_ID, is_admin=False, refs=refs, stored=[_ref(str(colleagues.id))]
        )
        is None
    )


async def test_admins_pin_any_integration():
    db, _own, colleagues = _db()
    assert (
        await pipeline_refs_refusal(
            db, OWNER_ID, is_admin=True, refs=[_ref(str(colleagues.id))], stored=[]
        )
        is None
    )


def test_only_integration_refs_carry_a_pin():
    assert pipeline_ref_integration_ids(
        _ref("a"), {"source": "custom_tool", "integration_id": "b"}, {"source": "integration"}, "x"
    ) == ["a"]


def test_every_agent_write_that_sets_a_pipeline_ref_runs_the_check():
    from app.api.v1 import agents as agents_api
    from app.services.builtin_tools import agent_builder

    for route in (agents_api.create_agent, agents_api.update_agent):
        assert "require_pipeline_refs_visible(" in inspect.getsource(route)
    assert "pipeline_refs_refusal(" in inspect.getsource(agent_builder)
