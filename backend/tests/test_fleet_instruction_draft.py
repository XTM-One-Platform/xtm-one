"""AI-drafted collaboration instruction of a connected member (#9162 R5), without a database.

Pinned here: a built-in fleet, an unknown member and a home member are refused
before the model; the context names the fleet, the orchestrator and the
connected agent or fleet by name, never an id; an "improve" round quotes the
current text; the fleet default is quoted for coherence; fences and wrapping
quotes are cleaned and the text is bounded; a failed or empty answer is an
InstructionDraftError; nothing is written; the route is AI-gated.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.models.fleet import FLEET_ROLE_CONNECTED, FLEET_ROLE_MEMBER
from app.services import fleet_instruction_draft as gen
from app.services.fleets import MEMBER_INSTRUCTION_MAX_LENGTH, FleetActionRefusedError


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._value or []))


class _Db:
    """Answers the member lookup, then the agents lookup; records nothing else."""

    def __init__(self, member, agents=(), fleets=None) -> None:
        self._answers = [member, list(agents)]
        self._fleets = fleets or {}
        self.writes = 0

    async def execute(self, _statement):
        return _Result(self._answers.pop(0) if self._answers else [])

    async def get(self, _model, key):
        return self._fleets.get(key)

    def add(self, _row):  # pragma: no cover - a write would fail the test
        self.writes += 1


class _Model:
    def __init__(self, content: str = "Call it for enrichment.", fail: bool = False) -> None:
        self.content = content
        self.fail = fail
        self.messages: list = []

    async def chat(self, messages, *, model, temperature, max_tokens):
        self.messages.append(messages)
        if self.fail:
            raise RuntimeError("provider down")
        return SimpleNamespace(content=self.content, stop_reason="end_turn")


def _fleet(**fields) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "name": "Alert Desk",
        "description": "Triages alerts.",
        "is_builtin": False,
        "orchestrator_agent_id": None,
        "default_collaboration_instruction": "",
    }
    return SimpleNamespace(**{**base, **fields})


def _member(**fields) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "role": FLEET_ROLE_CONNECTED,
        "agent_id": None,
        "target_fleet_id": None,
        "collaboration_instruction": None,
    }
    return SimpleNamespace(**{**base, **fields})


async def test_a_builtin_fleet_is_refused_before_the_model() -> None:
    model = _Model()
    with pytest.raises(FleetActionRefusedError) as refused:
        await gen.draft_member_instruction(
            _Db(_member()), uuid.uuid4(), _fleet(is_builtin=True), uuid.uuid4(), brief="Ask", llm=model
        )
    assert refused.value.status_code == 403
    assert model.messages == []


async def test_an_unknown_or_home_member_is_refused_before_the_model() -> None:
    model = _Model()
    with pytest.raises(FleetActionRefusedError) as unknown:
        await gen.draft_member_instruction(_Db(None), uuid.uuid4(), _fleet(), uuid.uuid4(), brief="Ask", llm=model)
    assert unknown.value.status_code == 404
    home = _member(role=FLEET_ROLE_MEMBER, agent_id=uuid.uuid4())
    with pytest.raises(FleetActionRefusedError) as refused:
        await gen.draft_member_instruction(_Db(home), uuid.uuid4(), _fleet(), home.id, brief="Ask", llm=model)
    assert refused.value.status_code == 400
    assert model.messages == []


async def test_the_context_names_the_agent_and_orchestrator_never_an_id() -> None:
    agent_id, lead_id = uuid.uuid4(), uuid.uuid4()
    agents = [
        SimpleNamespace(id=agent_id, name="Enricher", description="Adds context to indicators."),
        SimpleNamespace(id=lead_id, name="Desk Lead", description=""),
    ]
    member = _member(agent_id=agent_id)
    fleet = _fleet(orchestrator_agent_id=lead_id, default_collaboration_instruction="Hand off to the SOC.")
    model = _Model('```\n"Call the Enricher for every new indicator."\n```')
    db = _Db(member, agents)
    draft = await gen.draft_member_instruction(
        db, uuid.uuid4(), fleet, member.id, brief="when to enrich", current_text="Call it sometimes.", llm=model
    )
    assert draft.text == "Call the Enricher for every new indicator."
    assert draft.truncated is False
    asked = model.messages[0][1].content
    assert '"Enricher"' in asked and "Adds context to indicators." in asked
    assert '"Desk Lead"' in asked
    assert "Hand off to the SOC." in asked, "the fleet default is quoted for coherence"
    assert "Call it sometimes." in asked, "an improve round keeps the current text"
    assert "when to enrich" in asked
    for raw in (agent_id, lead_id, member.id, fleet.id):
        assert str(raw) not in asked, "no id reaches the model"
    assert db.writes == 0


async def test_a_connected_fleet_is_named_as_a_fleet() -> None:
    target_id = uuid.uuid4()
    member = _member(target_fleet_id=target_id)
    db = _Db(member, fleets={target_id: SimpleNamespace(name="Threat Intel", description="Owns the intel.")})
    model = _Model()
    await gen.draft_member_instruction(db, uuid.uuid4(), _fleet(), member.id, brief="what to ask", llm=model)
    asked = model.messages[0][1].content
    assert 'The connected fleet: "Threat Intel". Owns the intel.' in asked


async def test_a_failed_or_empty_answer_is_an_error() -> None:
    member = _member(agent_id=uuid.uuid4())
    with pytest.raises(gen.InstructionDraftError):
        await gen.draft_member_instruction(
            _Db(member), uuid.uuid4(), _fleet(), member.id, brief="Ask", llm=_Model(fail=True)
        )
    with pytest.raises(gen.InstructionDraftError):
        await gen.draft_member_instruction(
            _Db(member), uuid.uuid4(), _fleet(), member.id, brief="Ask", llm=_Model("  ")
        )


def test_a_long_answer_is_bounded() -> None:
    text = gen.clean_instruction("x" * (MEMBER_INSTRUCTION_MAX_LENGTH + 50))
    assert len(text) == MEMBER_INSTRUCTION_MAX_LENGTH
    assert text.endswith("…")


def test_the_route_is_ai_gated() -> None:
    from app.api.deps import require_agentic_ai
    from app.api.v1.fleets import router

    route = next(r for r in router.routes if getattr(r, "path", "").endswith("/instruction-draft"))
    assert "POST" in route.methods
    assert any(d.call is require_agentic_ai for d in route.dependant.dependencies)
