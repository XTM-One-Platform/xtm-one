"""A handover type map that works today keeps working.

Each test pins, on one write surface the handover rules touch, what main does
with every map that is not the fix's case, and passes unchanged on main: the
agents API (create and update), the agent builder's ``update_agent``, the
company-managed strip and the agent copy (``retain_agent_bindings``), the
agent deletion clean-up and ``PUT /settings/general-assistant``. The intended
changes (a map kept empty, an explicit map a removal would empty) are tested
next to the code that makes them.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.api.v1.helpers as api_helpers
import app.core.database as database
from app.api.v1.schemas.agent import UpdateAgentRequest
from app.api.v1.schemas.settings import GeneralAssistantConfigRequest
from app.models.agent import Agent
from app.services import agent_bindings
from tests.test_agent_builder_crud_tools import (
    _call,
    _session_factory,
    _stub_personal_bindings,
    _stub_writable_agent,
    build_agent_builder_tool,
)
from tests.test_agent_reference_cleanup import _FakeSession
from tests.test_agent_update_partial_payload import _DummyAgent, _invoke_update
from tests.test_general_assistant_tools import (
    _agent,
    _FlushReachedError,
    _Result,
    _save_general_assistant,
    _Session,
)

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
C = "cccccccc-0000-4000-8000-000000000003"


@pytest.fixture
def platform_settings(monkeypatch):
    async def _fake_platform_settings(_db):
        return SimpleNamespace(
            embedding_provider=None,
            embedding_provider_id=None,
            embedding_model=None,
            embedding_dimensions=None,
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_platform_settings,
    )


# -- Agents API: create ------------------------------------------------------


def _main_alignment(ids, type_map):
    """Unique UUID targets in order, and the entries of *type_map* for them."""
    kept: list[str] = []
    for raw in ids:
        try:
            aid = str(uuid.UUID(str(raw)))
        except ValueError:
            continue
        if aid not in kept:
            kept.append(aid)
    return kept, {k: v for k, v in (type_map or {}).items() if k in kept}


@pytest.mark.parametrize("ids", [[], [A], [A, B], [B, A, B], [A, "not-a-uuid", None]])
@pytest.mark.parametrize(
    "type_map",
    [
        None,
        {},
        {A: "consult"},
        {B: "transfer"},
        {A: "consult", B: "delegate"},
        {C: "consult"},
        {"not-a-uuid": "consult"},
        {A: "mandatory_transfer", C: "transfer"},
    ],
)
def test_creating_an_agent_aligns_its_handover_map_as_before(ids, type_map):
    cleaned_ids, types, _ = agent_bindings.sanitize_handover_config(ids, type_map, None)
    assert (cleaned_ids, types) == _main_alignment(ids, type_map)


# -- Agents API: update ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ids", "types", "body", "expected"),
    [
        ([A, B], {A: "consult"}, {"name": "Renamed"}, {A: "consult"}),
        ([A, B], None, {"name": "Renamed"}, None),
        ([A, B], {}, {"name": "Renamed"}, None),
        ([A], None, {"handover_agent_ids": [A, B]}, {}),
        ([A], None, {"handover_types": {C: "consult", "not-a-uuid": "transfer"}}, {}),
        ([A, B], {A: "consult"}, {"handover_types": {A: "transfer", B: "consult"}}, {A: "transfer", B: "consult"}),
        ([A, B], {A: "consult"}, {"handover_types": {}}, {}),
        ([A, B], {A: "consult", B: "transfer"}, {"handover_agent_ids": [B]}, {B: "transfer"}),
    ],
    ids=[
        "explicit-unrelated-save",
        "legacy-none-unrelated-save",
        "legacy-empty-unrelated-save",
        "legacy-target-added",
        "legacy-type-for-no-target",
        "explicit-replaced",
        "explicit-cleared",
        "typed-target-removed-next-to-a-typed-one",
    ],
)
async def test_updating_an_agent_keeps_how_each_target_runs(monkeypatch, ids, types, body, expected):
    agent = _DummyAgent()
    agent.handover_agent_ids = list(ids)
    agent.handover_types = types

    updated, _ = await _invoke_update(monkeypatch, UpdateAgentRequest(**body), agent=agent)

    assert updated.handover_types == expected


# -- Agent builder: update_agent -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ids", "types", "arguments", "expected"),
    [
        ([A], {}, {"handover_types": f"{C}:consult"}, {}),
        ([A], {}, {"handover_types": f"{A}:consult"}, {A: "consult"}),
        ([A, B], {A: "consult"}, {"handover_types": "clear"}, {}),
        ([A, B], {A: "consult", B: "transfer"}, {"handover_agent_ids": B}, {B: "transfer"}),
    ],
    ids=["legacy-type-for-no-target", "legacy-target-typed", "explicit-cleared", "typed-target-removed"],
)
async def test_the_agent_builder_keeps_how_each_target_runs(monkeypatch, ids, types, arguments, expected):
    agent = Agent(id=uuid.uuid4(), name="Router", handover_agent_ids=list(ids), handover_types=types)
    _stub_writable_agent(monkeypatch, agent=agent)
    _stub_personal_bindings(monkeypatch)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "update_agent", agent_id=str(agent.id), **arguments)

    assert not result.is_error, result.content
    assert agent.handover_types == expected


# -- Company-managed strip and agent copy ----------------------------------


@pytest.mark.parametrize(
    ("types", "keep", "expected_ids", "expected_types"),
    [
        ({A: "consult", B: "delegate"}, {A}, [A], {A: "consult"}),
        (None, {A}, [A], None),
        ({}, {A}, [A], None),
        ({A: "consult"}, {A, B}, [A, B], {A: "consult"}),
        ({A: "consult"}, set(), None, None),
    ],
    ids=["explicit-typed-kept", "legacy-none", "legacy-empty", "all-kept", "none-kept"],
)
def test_stripping_or_copying_an_agent_keeps_how_each_kept_target_runs(types, keep, expected_ids, expected_types):
    agent = SimpleNamespace(handover_agent_ids=[A, B], handover_types=types, handover_conditions=None)

    agent_bindings.retain_agent_bindings(agent, {"handover_agent_ids": keep})

    assert agent.handover_agent_ids == expected_ids
    assert agent.handover_types == expected_types


# -- Agent deletion ----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("ga_types", [{A: "consult"}, {}], ids=["ga-explicit", "ga-legacy"])
async def test_deleting_an_agent_keeps_how_every_other_target_runs(ga_types):
    deleted = uuid.uuid4()
    aid = str(deleted)
    explicit = SimpleNamespace(
        id=uuid.uuid4(),
        handover_agent_ids=[aid, A],
        handover_types={aid: "consult", A: "transfer"},
        handover_conditions=None,
    )
    legacy = SimpleNamespace(
        id=uuid.uuid4(),
        handover_agent_ids=[aid, A],
        handover_types={},
        handover_conditions=None,
    )
    ga = SimpleNamespace(handover_agent_ids=[aid, A], handover_types=dict(ga_types), linked_agent_id=None)

    await api_helpers.purge_agent_references(_FakeSession(agents=[explicit, legacy], ga=ga), deleted)

    assert [explicit.handover_agent_ids, legacy.handover_agent_ids, ga.handover_agent_ids] == [[A]] * 3
    assert explicit.handover_types == {A: "transfer"}
    assert legacy.handover_types == {}
    assert ga.handover_types == ga_types


# -- General Assistant settings --------------------------------------------


async def _save(config, body, *lookups):
    db = _Session(_Result(config), *(_Result(rows) for rows in lookups))
    with pytest.raises(_FlushReachedError):
        await _save_general_assistant(db, body)
    return config


@pytest.mark.asyncio
async def test_a_target_added_to_an_explicit_general_assistant_is_recorded_as_delegate(platform_settings):
    added = uuid.uuid4()
    config = SimpleNamespace(handover_agent_ids=[A], handover_types={A: "consult"}, builtin_tool_ids=[])

    await _save(
        config,
        GeneralAssistantConfigRequest(handover_agent_ids=[A, str(added)]),
        [_agent(added, "Support", company_managed=True)],
    )

    assert config.handover_types == {A: "consult", str(added): "delegate"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, {}])
async def test_a_first_general_assistant_target_is_recorded_as_delegate(platform_settings, stored):
    added = uuid.uuid4()
    config = SimpleNamespace(handover_agent_ids=[], handover_types=stored, builtin_tool_ids=[])

    await _save(
        config,
        GeneralAssistantConfigRequest(handover_agent_ids=[str(added)]),
        [_agent(added, "Support", company_managed=True)],
    )

    assert config.handover_types == {str(added): "delegate"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [{}, {A: "consult", B: "delegate"}], ids=["legacy", "explicit"])
async def test_an_unrelated_general_assistant_save_keeps_its_map(platform_settings, stored):
    config = SimpleNamespace(handover_agent_ids=[A, B], handover_types=dict(stored), builtin_tool_ids=[])

    await _save(config, GeneralAssistantConfigRequest(handover_agent_ids=[A, B]))

    assert config.handover_types == stored


@pytest.mark.asyncio
async def test_general_assistant_types_sent_for_its_targets_are_saved_as_sent(platform_settings):
    typed = {A: "consult", B: "transfer"}
    config = SimpleNamespace(handover_agent_ids=[A, B], handover_types={A: "delegate"}, builtin_tool_ids=[])

    await _save(config, GeneralAssistantConfigRequest(handover_agent_ids=[A, B], handover_types=typed))

    assert config.handover_types == typed
