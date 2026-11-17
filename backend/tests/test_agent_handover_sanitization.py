import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.core.database as database
from app.api.v1.agents import _sanitize_handover_for_storage
from app.api.v1.schemas.agent import UpdateAgentRequest
from app.models.agent import Agent
from app.services import agent_bindings
from app.services.agent_bindings import sanitize_handover_config, validate_company_managed_bindings
from tests.test_agent_builder_crud_tools import (
    _call,
    _session_factory,
    _stub_personal_bindings,
    _stub_writable_agent,
    build_agent_builder_tool,
)
from tests.test_agent_update_partial_payload import _DummyAgent, _invoke_update

TYPED = "550e8400-e29b-41d4-a716-446655440000"
UNTYPED = "11111111-1111-1111-1111-111111111111"


def test_sanitize_handover_config_drops_non_uuid_targets() -> None:
    valid_id = "550e8400-e29b-41d4-a716-446655440000"
    ids, types, conditions = sanitize_handover_config(
        [valid_id, "__general_assistant__", "not-a-uuid"],
        {valid_id: "consult", "__general_assistant__": "consult"},
        {valid_id: "billing", "__general_assistant__": "should-drop"},
    )

    assert ids == [valid_id]
    assert types == {valid_id: "consult"}
    assert conditions == {valid_id: "billing"}


def test_sanitize_handover_config_deduplicates_and_prunes_orphans() -> None:
    valid_id = "550e8400-e29b-41d4-a716-446655440000"
    ids, types, conditions = sanitize_handover_config(
        [valid_id, valid_id],
        {valid_id: "mandatory_transfer", "11111111-1111-1111-1111-111111111111": "consult"},
        {valid_id: "route by billing", "11111111-1111-1111-1111-111111111111": "drop"},
    )

    assert ids == [valid_id]
    assert types == {valid_id: "mandatory_transfer"}
    assert conditions == {valid_id: "route by billing"}


def test_dropping_the_only_typed_target_keeps_the_others_delegate() -> None:
    """An explicit map emptied by the alignment would give every target left
    every handover type (the legacy mode): they keep running as delegate."""
    typed = "550e8400-e29b-41d4-a716-446655440000"
    untyped = "11111111-1111-1111-1111-111111111111"
    ids, types, _ = sanitize_handover_config(
        [untyped], {typed: "consult"}, None, types_before={typed: "consult"}
    )

    assert ids == [untyped]
    assert types == {untyped: "delegate"}
    assert _sanitize_handover_for_storage(
        [untyped], {typed: "consult"}, None, types_before={typed: "consult"}
    )[1] == {untyped: "delegate"}


@pytest.mark.parametrize("types_before", [None, {}], ids=["new-agent", "legacy-agent"])
def test_a_type_written_only_for_agents_that_are_no_target_is_dropped(types_before) -> None:
    """A new or legacy agent never ran with an explicit map: its targets keep
    every handover type, as before the removal rule."""
    target = "550e8400-e29b-41d4-a716-446655440000"
    stray = "11111111-1111-1111-1111-111111111111"
    _, types, _ = sanitize_handover_config(
        [target], {stray: "consult", "not-a-uuid": "transfer"}, None, types_before=types_before
    )
    assert types == {}


def test_clearing_the_types_of_an_explicit_agent_returns_it_to_the_legacy_mode() -> None:
    target = "550e8400-e29b-41d4-a716-446655440000"
    _, types, _ = sanitize_handover_config([target], {}, None, types_before={target: "consult"})
    assert types == {}


def test_a_legacy_map_stays_empty_when_it_is_aligned() -> None:
    target = "550e8400-e29b-41d4-a716-446655440000"
    _, types, _ = sanitize_handover_config([target], {}, None)
    assert types == {}
    assert _sanitize_handover_for_storage([target], None, None)[1] is None


# ── Regression: sanitize → validate → assign ordering on update_agent ─────


def _run_handover_sanitize_validate_assign(agent, validator):
    """Mirror the exact ordering used in ``update_agent`` so the
    validate-before-mutate invariant can be tested without spinning up
    the full FastAPI/DB stack.

    Any drift in the production code path (e.g. someone re-introducing
    the in-place mutation pattern) will fail the regression tests below
    because they import ``validate_handover_conditions`` from
    ``agents_api`` directly and exercise the same call sequence.
    """
    cleaned_ids, cleaned_types, cleaned_conditions = sanitize_handover_config(
        agent.handover_agent_ids,
        agent.handover_types,
        agent.handover_conditions,
        types_before=agent.handover_types,
    )
    allowed_ids = {str(a) for a in cleaned_ids}
    cleaned_conditions = {
        aid: cond
        for aid, cond in cleaned_conditions.items()
        if aid in allowed_ids and cleaned_types.get(aid) == "mandatory_transfer"
    } or {}

    validator(cleaned_ids, cleaned_types, cleaned_conditions)

    agent.handover_agent_ids = cleaned_ids
    agent.handover_types = cleaned_types
    agent.handover_conditions = cleaned_conditions


def test_failed_handover_validation_does_not_mutate_agent_in_place() -> None:
    """If ``validate_handover_conditions`` raises, the in-memory ``Agent``
    instance MUST keep its original handover attributes — the previous
    in-place-mutation pattern left the model with sanitised values even
    after validation rejected the payload (DB was rolled back, but the
    in-memory model was still observable to subsequent handler logic).
    """
    valid_id = "550e8400-e29b-41d4-a716-446655440000"
    original_ids = [valid_id, "__general_assistant__"]
    original_types = {valid_id: "mandatory_transfer", "__general_assistant__": "consult"}
    original_conditions = {valid_id: "duplicate", "__general_assistant__": "drop"}

    agent = SimpleNamespace(
        handover_agent_ids=list(original_ids),
        handover_types=dict(original_types),
        handover_conditions=dict(original_conditions),
    )

    def _failing_validator(_ids, _types, _conditions):
        raise HTTPException(
            status_code=400,
            detail="Conditions duplicated across mandatory_transfer targets",
        )

    with pytest.raises(HTTPException) as exc_info:
        _run_handover_sanitize_validate_assign(agent, _failing_validator)

    assert exc_info.value.status_code == 400

    # ── Invariant: agent attributes are EXACTLY as they were on entry ──
    assert agent.handover_agent_ids == original_ids, (
        "validation failure must NOT mutate handover_agent_ids in place"
    )
    assert agent.handover_types == original_types, (
        "validation failure must NOT mutate handover_types in place"
    )
    assert agent.handover_conditions == original_conditions, (
        "validation failure must NOT mutate handover_conditions in place"
    )


def test_successful_handover_validation_commits_sanitized_values() -> None:
    """The happy path of the same flow: validation succeeds, so the
    sanitised values get committed to the agent.  Pins the other side
    of the invariant — sanitisation must still actually take effect on
    successful validation.
    """
    valid_id = "550e8400-e29b-41d4-a716-446655440000"
    agent = SimpleNamespace(
        handover_agent_ids=[valid_id, "__general_assistant__"],
        handover_types={valid_id: "consult", "__general_assistant__": "consult"},
        handover_conditions={valid_id: "drop", "__general_assistant__": "drop"},
    )

    validator_calls: list[tuple] = []

    def _passing_validator(ids, types, conditions):
        validator_calls.append((list(ids), dict(types), dict(conditions)))

    _run_handover_sanitize_validate_assign(agent, _passing_validator)

    # ``__general_assistant__`` is dropped, mandatory-transfer pruning
    # also drops ``valid_id``'s condition (type was ``consult`` not
    # ``mandatory_transfer``), so the agent ends up with the surviving
    # UUID and an empty conditions map.
    assert agent.handover_agent_ids == [valid_id]
    assert agent.handover_types == {valid_id: "consult"}
    assert agent.handover_conditions == {}

    # And the validator saw the exact same sanitised values, NOT the raw
    # input — i.e. validation runs on the about-to-be-assigned shape.
    assert len(validator_calls) == 1
    seen_ids, seen_types, seen_conditions = validator_calls[0]
    assert seen_ids == [valid_id]
    assert seen_types == {valid_id: "consult"}
    assert seen_conditions == {}


def test_real_validate_function_imported_from_agent_bindings() -> None:
    """Sanity-check that the production validator is importable under
    the expected name — protects the regression tests above against a
    silent rename or move of ``validate_handover_conditions`` (which
    would let the in-place-mutation pattern sneak back in unflagged).
    """
    assert callable(getattr(agent_bindings, "validate_handover_conditions", None))
    assert callable(getattr(agent_bindings, "sanitize_handover_config", None))


def test_sanitize_handover_for_storage_preserves_nullable_shapes() -> None:
    valid_id = "550e8400-e29b-41d4-a716-446655440000"
    ids, types, conditions = _sanitize_handover_for_storage(
        [valid_id, "__general_assistant__"],
        {valid_id: "consult", "__general_assistant__": "consult"},
        {valid_id: "route", "__general_assistant__": "drop"},
    )
    assert ids == [valid_id]
    assert types == {valid_id: "consult"}
    assert conditions == {valid_id: "route"}

    empty_ids, empty_types, empty_conditions = _sanitize_handover_for_storage(
        None, None, None
    )
    assert empty_ids is None
    assert empty_types is None
    assert empty_conditions is None


@pytest.mark.asyncio
async def test_company_managed_validation_tolerates_invalid_handover_ids() -> None:
    class MockQueryResult:
        def all(self):
            return []

    class MockDatabase:
        def __init__(self):
            self.execute_calls = 0

        async def execute(self, _query):
            self.execute_calls += 1
            return MockQueryResult()

    db = MockDatabase()
    await validate_company_managed_bindings(
        db,
        mcp_server_ids=None,
        knowledge_base_ids=None,
        integration_bindings=None,
        handover_agent_ids=[
            "__general_assistant__",
            "not-a-uuid",
            "550e8400-e29b-41d4-a716-446655440000",
        ],
    )
    # Only the valid UUID should survive sanitization, so validation performs
    # exactly one DB check and completes without raising.
    assert db.execute_calls == 1


@pytest.mark.asyncio
async def test_making_an_agent_company_managed_keeps_its_explicit_handover_map_explicit() -> None:
    """Stripping the personal target that held the only type must not give the
    company-managed targets left every handover type."""
    company = "550e8400-e29b-41d4-a716-446655440000"
    personal = "11111111-1111-1111-1111-111111111111"

    class _Rows:
        def all(self):
            return [(company,)]

    class _Db:
        async def execute(self, _query):
            return _Rows()

    agent = SimpleNamespace(
        mcp_server_ids=None,
        knowledge_base_ids=None,
        integration_bindings=None,
        handover_agent_ids=[company, personal],
        handover_types={personal: "consult"},
        handover_conditions=None,
    )
    await agent_bindings.strip_personal_bindings(_Db(), agent)

    assert agent.handover_agent_ids == [company]
    assert agent.handover_types == {company: "delegate"}


# -- The removal rule on the write paths ---------------------------
# An agent that ran with an explicit map keeps its targets delegate when a
# write would empty the map; the pins of every other map are in
# ``test_handover_no_regression.py``.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_ids", "body"),
    [
        ([TYPED, UNTYPED], {"handover_agent_ids": [UNTYPED]}),
        ([TYPED, UNTYPED], {"handover_agent_ids": [UNTYPED], "handover_types": {TYPED: "consult"}}),
        # A stored type for an agent no longer listed: the map runs explicit.
        ([UNTYPED], {"name": "Renamed"}),
    ],
    ids=["typed-target-removed", "removed-with-its-stale-type-sent-back", "unrelated-save"],
)
async def test_the_agents_api_keeps_an_explicit_agent_explicit(monkeypatch, stored_ids, body) -> None:
    agent = _DummyAgent()
    agent.handover_agent_ids = list(stored_ids)
    agent.handover_types = {TYPED: "consult"}

    updated, _ = await _invoke_update(monkeypatch, UpdateAgentRequest(**body), agent=agent)

    assert updated.handover_agent_ids == [UNTYPED]
    assert updated.handover_types == {UNTYPED: "delegate"}


@pytest.mark.asyncio
async def test_the_agent_builder_keeps_an_explicit_agent_explicit(monkeypatch) -> None:
    agent = Agent(
        id=uuid.uuid4(),
        name="Router",
        handover_agent_ids=[TYPED, UNTYPED],
        handover_types={TYPED: "consult"},
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    _stub_personal_bindings(monkeypatch)
    monkeypatch.setattr(database, "async_session", _session_factory([]))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "update_agent", agent_id=str(agent.id), handover_agent_ids=UNTYPED)

    assert not result.is_error, result.content
    assert agent.handover_types == {UNTYPED: "delegate"}


def test_a_copy_that_drops_the_only_typed_target_keeps_the_others_delegate() -> None:
    """The agent copy keeps the targets its owner may bind through the same
    helper as the company-managed strip: the copy runs them as the original."""
    agent = SimpleNamespace(
        handover_agent_ids=[TYPED, UNTYPED],
        handover_types={TYPED: "consult"},
        handover_conditions=None,
    )

    dropped = agent_bindings.retain_agent_bindings(agent, {"handover_agent_ids": {UNTYPED}})

    assert dropped == {"handover_agent_ids": 1}
    assert agent.handover_agent_ids == [UNTYPED]
    assert agent.handover_types == {UNTYPED: "delegate"}
