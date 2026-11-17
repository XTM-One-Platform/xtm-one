"""Tests for dangling agent-id reference cleanup.

Covers ``app.api.v1.helpers.purge_agent_references`` - the runtime path called
from both the REST ``DELETE /agents/{id}`` endpoint and the agent-builder
``delete_agent`` tool. It removes a deleted agent's id from every JSON /
scalar agent-id column that has no database-level foreign key (handover
edges, default / linked / exposed-agent pointers), so deletion never leaves a
ghost node in the topology graph.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.dml import Update

import app.api.v1.helpers as api_helpers

# ---------------------------------------------------------------------------
# purge_agent_references (runtime path)
# ---------------------------------------------------------------------------


class _PurgeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        value = self._value
        if value is None:
            return []
        return list(value) if isinstance(value, (list, tuple)) else [value]


def _from_name(statement) -> str | None:
    try:
        froms = statement.get_final_froms()
    except Exception:
        return None
    return getattr(froms[0], "name", None) if froms else None


class _FakeSession:
    """Routes purge's SELECTs to fixture rows and counts the scalar null UPDATE."""

    def __init__(self, *, agents=None, ga=None, ps=None, users=None):
        self._agents = agents or []
        self._ga = ga
        self._ps = ps
        self._users = users or []
        self.scalar_update_count = 0

    async def execute(self, statement):
        # Core UPDATE (the ``default_agent_id = NULL`` sweep) is the only DML;
        # everything else purge issues is an ORM SELECT.
        if isinstance(statement, Update):
            self.scalar_update_count += 1
            return _PurgeResult(None)
        name = _from_name(statement)
        return _PurgeResult(
            {
                "agents": self._agents,
                "general_assistant_config": self._ga,
                "platform_settings": self._ps,
                "users": self._users,
            }.get(name)
        )


@pytest.mark.asyncio
async def test_purge_agent_references_scrubs_every_surface():
    deleted = uuid.uuid4()
    aid = str(deleted)
    keep = str(uuid.uuid4())

    other_agent = SimpleNamespace(
        id=uuid.uuid4(),
        handover_agent_ids=[aid, keep],
        handover_types={aid: "consult", keep: "delegate"},
        handover_conditions={aid: "when X happens"},
    )
    # The agent being deleted is returned by the contains() pre-filter but must
    # be skipped (its row is removed by the FK delete anyway).
    self_agent = SimpleNamespace(
        id=deleted,
        handover_agent_ids=[aid],
        handover_types={aid: "delegate"},
        handover_conditions=None,
    )
    ga = SimpleNamespace(
        handover_agent_ids=[aid],
        handover_types={aid: "transfer"},
        linked_agent_id=deleted,
        linked_agent_configured=True,
    )
    ps = SimpleNamespace(
        mcp_exposed_agent_ids=[aid, keep],
        # Vertical-aware CTEM analyst defaults: stale stage + briefing overrides.
        ctem_stage_agent_ids={"cyber": {"scoping": aid, "validation": keep}},
        ctem_briefing_agent_ids={"cyber": aid, "fimi": keep},
    )
    user = SimpleNamespace(
        mcp_exposed_agent_ids=[aid],
        channel_agent_config={"slack": aid, "teams": keep},
        # Starred agents: the deleted one plus a survivor.
        favorite_agent_ids=[aid, keep],
        # Per-user CTEM overrides whose only entries point at the deleted agent.
        ctem_stage_agent_ids={"fimi": {"prioritization": aid}},
        ctem_briefing_agent_ids={"fimi": aid},
    )
    session = _FakeSession(
        agents=[other_agent, self_agent], ga=ga, ps=ps, users=[user]
    )

    await api_helpers.purge_agent_references(session, deleted)

    # Other agent: the deleted id is gone from all three handover structures.
    assert other_agent.handover_agent_ids == [keep]
    assert other_agent.handover_types == {keep: "delegate"}
    assert other_agent.handover_conditions == {}

    # The agent being deleted is left untouched (skipped).
    assert self_agent.handover_agent_ids == [aid]

    # General Assistant config: handover scrubbed + linked pointer nulled.
    assert ga.handover_agent_ids == []
    assert ga.handover_types == {}
    assert ga.linked_agent_id is None
    # Nobody chose "no agent": the next boot links the default again.
    assert ga.linked_agent_configured is False

    # Platform settings: stale exposed agent removed, valid one preserved.
    assert ps.mcp_exposed_agent_ids == [keep]
    # CTEM platform defaults: the deleted agent is dropped from the stage bucket
    # (valid one kept) and from the briefing map; emptied containers collapse away.
    assert ps.ctem_stage_agent_ids == {"cyber": {"validation": keep}}
    assert ps.ctem_briefing_agent_ids == {"fimi": keep}

    # User: exposed list emptied (kept as []) + channel mapping pruned.
    assert user.mcp_exposed_agent_ids == []
    assert user.channel_agent_config == {"teams": keep}
    # Favorites: the deleted agent is unstarred, the survivor keeps its place.
    assert user.favorite_agent_ids == [keep]
    # CTEM per-user overrides: their only entries pointed at the deleted agent,
    # so both maps collapse to None -> the user reverts to the platform/seeded
    # default (the "value disappears -> back to default" contract).
    assert user.ctem_stage_agent_ids is None
    assert user.ctem_briefing_agent_ids is None

    # The bare ``default_agent_id`` pointer is nulled via a Core UPDATE.
    assert session.scalar_update_count == 1


@pytest.mark.asyncio
async def test_deleting_the_only_typed_target_keeps_the_other_targets_delegate():
    """A type map whose only entry is the deleted agent's must not turn into the
    legacy mode, which would give every target left every handover type:
    they keep running as delegate, for agents and the General Assistant alike."""
    deleted = uuid.uuid4()
    aid = str(deleted)
    keep = str(uuid.uuid4())

    other_agent = SimpleNamespace(
        id=uuid.uuid4(),
        handover_agent_ids=[aid, keep],
        handover_types={aid: "consult"},
        handover_conditions=None,
    )
    legacy_agent = SimpleNamespace(
        id=uuid.uuid4(),
        handover_agent_ids=[aid, keep],
        handover_types={},
        handover_conditions=None,
    )
    ga = SimpleNamespace(
        handover_agent_ids=[aid, keep],
        handover_types={aid: "transfer"},
        linked_agent_id=None,
        # An administrator's "no agent": deleting another agent keeps it.
        linked_agent_configured=True,
    )
    session = _FakeSession(agents=[other_agent, legacy_agent], ga=ga)

    await api_helpers.purge_agent_references(session, deleted)

    assert other_agent.handover_agent_ids == [keep]
    assert other_agent.handover_types == {keep: "delegate"}
    assert legacy_agent.handover_agent_ids == [keep]
    assert legacy_agent.handover_types == {}
    assert ga.handover_agent_ids == [keep]
    assert ga.handover_types == {keep: "delegate"}
    assert ga.linked_agent_id is None
    assert ga.linked_agent_configured is True


@pytest.mark.asyncio
async def test_purge_agent_references_noop_without_targets():
    """No GA/platform/user rows and no referencing agents - still issues the
    scalar null-out UPDATE but mutates nothing."""
    deleted = uuid.uuid4()
    session = _FakeSession(agents=[], ga=None, ps=None, users=[])
    await api_helpers.purge_agent_references(session, deleted)
    assert session.scalar_update_count == 1


def test_prune_ctem_agent_overrides_strips_whitespace():
    """Override ids are trimmed on read/write, so pruning must compare on the
    stripped form - a stored id with stray whitespace must still be removed
    (otherwise a deleted analyst would leave a dangling, unresolvable id)."""
    aid = "11111111-1111-1111-1111-111111111111"
    keep = "22222222-2222-2222-2222-222222222222"
    stage_map = {
        "cyber": {"scoping": f"  {aid}  ", "validation": keep},
        "fimi": {"prioritization": f"{aid}\n"},  # only stale -> bucket drops
    }
    brief_map = {"cyber": f" {aid} ", "fimi": keep}

    new_stage, new_brief, changed = api_helpers._prune_ctem_agent_overrides(
        stage_map, brief_map, aid
    )

    assert changed is True
    # Padded stale id removed; the kept id (and its bucket) survive; the
    # all-stale fimi bucket collapses away.
    assert new_stage == {"cyber": {"validation": keep}}
    assert new_brief == {"fimi": keep}


def test_prune_ctem_agent_overrides_legacy_flat_and_empty():
    """Legacy flat ``{stage: id}`` entries are matched (stripped) too, and maps
    that end up empty collapse to ``None``."""
    aid = "33333333-3333-3333-3333-333333333333"
    new_stage, new_brief, changed = api_helpers._prune_ctem_agent_overrides(
        {"scoping": f"  {aid}"}, {"cyber": aid}, aid
    )
    assert changed is True
    assert new_stage is None
    assert new_brief is None

    # Nothing to remove -> unchanged, no false positive.
    s2, b2, changed2 = api_helpers._prune_ctem_agent_overrides(
        {"cyber": {"scoping": "other"}}, None, aid
    )
    assert changed2 is False
    assert s2 == {"cyber": {"scoping": "other"}}
    assert b2 is None
