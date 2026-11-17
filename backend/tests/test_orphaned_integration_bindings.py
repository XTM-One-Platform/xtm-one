"""An agent binding whose integration the agent's owner can no longer see is
orphaned: no run of the agent uses it (#11229)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import integration_credentials as ic

KEPT = str(uuid.uuid4())
LOST = str(uuid.uuid4())


@pytest.fixture
def owner_lost_one(monkeypatch):
    seen: dict = {}

    async def fake_orphaned(db, owner_id, integration_ids, *, owner_group_ids=None):
        seen["owner"] = owner_id
        seen["ids"] = list(integration_ids)
        return {LOST}

    monkeypatch.setattr(ic, "orphaned_binding_ids", fake_orphaned)
    return seen


@pytest.mark.asyncio
async def test_orphaned_binding_is_dropped_and_reported(owner_lost_one):
    agent = SimpleNamespace(user_id=uuid.uuid4(), is_company_managed=False)
    bindings = {"jira": KEPT, "hibob": LOST}
    origin = {"jira": "direct", "hibob": "direct"}
    withheld: list[dict] = []

    dropped = await ic.drop_orphaned_bindings(None, agent, bindings, origin, withheld=withheld)

    assert dropped == ["hibob"]
    assert bindings == {"jira": KEPT}
    assert origin == {"jira": "direct"}
    assert owner_lost_one["owner"] == agent.user_id
    assert withheld == [
        {
            "binding_key": "hibob",
            "provider": "hibob",
            "integration_id": LOST,
            "reason": ic.OWNER_LOST_ACCESS_REASON,
        }
    ]


@pytest.mark.asyncio
async def test_multi_instance_key_reports_its_base_provider(owner_lost_one):
    agent = SimpleNamespace(user_id=uuid.uuid4(), is_company_managed=False)
    bindings = {"hibob#2": LOST}
    withheld: list[dict] = []

    await ic.drop_orphaned_bindings(None, agent, bindings, withheld=withheld)

    assert bindings == {}
    assert withheld[0]["provider"] == "hibob"
    assert withheld[0]["binding_key"] == "hibob#2"


@pytest.mark.asyncio
async def test_company_managed_agent_is_never_orphaned(owner_lost_one):
    agent = SimpleNamespace(user_id=uuid.uuid4(), is_company_managed=True)
    bindings = {"hibob": LOST}

    assert await ic.drop_orphaned_bindings(None, agent, bindings) == []
    assert bindings == {"hibob": LOST}
    assert "owner" not in owner_lost_one


@pytest.mark.asyncio
async def test_origins_limit_the_judgement_to_the_agents_own_bindings(owner_lost_one):
    """An assignment override (an admin may bind any row) is not the owner's
    binding: only the keys of the named origins are judged."""
    agent = SimpleNamespace(user_id=uuid.uuid4(), is_company_managed=False)
    bindings = {"github": LOST, "hibob": LOST}
    origin = {"github": "assignment_binding_override", "hibob": "agent_binding_inherited"}

    dropped = await ic.drop_orphaned_bindings(None, agent, bindings, origin, origins=("agent_binding_inherited",))

    assert dropped == ["hibob"]
    assert bindings == {"github": LOST}
    assert owner_lost_one["ids"] == [LOST]


@pytest.mark.asyncio
async def test_override_only_run_reads_no_visibility(owner_lost_one):
    agent = SimpleNamespace(user_id=uuid.uuid4(), is_company_managed=False)
    bindings = {"github": LOST}
    origin = {"github": "assignment_binding_override"}

    assert await ic.drop_orphaned_bindings(None, agent, bindings, origin, origins=("agent_binding_inherited",)) == []
    assert bindings == {"github": LOST}
    assert "owner" not in owner_lost_one


@pytest.mark.asyncio
async def test_agent_page_lists_the_orphaned_integrations(owner_lost_one):
    class _Rows:
        def all(self):
            return [SimpleNamespace(id=uuid.UUID(LOST), name="HiBob", provider="hibob")]

    class _Db:
        async def execute(self, _stmt):
            return _Rows()

    agent = SimpleNamespace(
        user_id=uuid.uuid4(), is_company_managed=False, integration_bindings={"jira": [KEPT], "hibob": [LOST]}
    )

    assert await ic.agent_orphaned_integrations(_Db(), agent) == [{"id": LOST, "name": "HiBob", "provider": "hibob"}]
    assert sorted(owner_lost_one["ids"]) == sorted([KEPT, LOST])


@pytest.mark.asyncio
async def test_company_agent_page_lists_no_orphan(owner_lost_one):
    agent = SimpleNamespace(user_id=uuid.uuid4(), is_company_managed=True, integration_bindings={"hibob": [LOST]})

    assert await ic.agent_orphaned_integrations(None, agent) == []
    assert "owner" not in owner_lost_one


@pytest.mark.asyncio
async def test_owner_less_agent_orphans_nothing():
    assert await ic.orphaned_binding_ids(None, None, [LOST]) == set()


def test_prompt_note_names_the_reason_and_escapes_the_provider():
    note = ic.format_orphaned_binding_prompt_note(
        [{"provider": 'hi"bob', "reason": ic.OWNER_LOST_ACCESS_REASON}, {"provider": "jira", "reason": "other"}]
    )
    assert "owner no longer has access" in note
    assert '"hi\\"bob"' in note
    assert "jira" not in note
    assert ic.format_orphaned_binding_prompt_note([]) == ""


def test_chat_prompt_note_carries_the_orphan_note():
    note = ic.format_unshared_oauth_prompt_note(
        [{"provider": "hibob", "reason": ic.OWNER_LOST_ACCESS_REASON}]
    )
    assert "owner no longer has access" in note
