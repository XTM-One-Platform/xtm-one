"""Shared fakes of the Concierge experiment tests (#8259): spies standing in for
levels 1 to 4 at ``quality_deps``, the one seam, and a seeded agent with a
finding."""

from __future__ import annotations

import uuid

from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.user import User
from app.services.health import experiments, quality_deps
from app.services.health.findings import Evidence, record_finding

PERSONA = "You answer billing questions about {{user.name}}'s invoices."
VARIANT = "You answer billing questions about {{user.name}}'s invoices. Quote the invoice number."
SKILL = "Look the invoice up before answering."
PROMPT = "Summarize {{trigger.body}} for the billing team."


class Deps:
    """Spies standing in for levels 1 to 4."""

    def __init__(self, monkeypatch) -> None:
        self.calls: dict[str, list] = {}
        self.budget = quality_deps.Budget(tokens=None, cost_usd=None, refusal=None)
        self.entries = 12
        self.compared = quality_deps.RunsCompared(
            finished=True,
            incomplete=False,
            quality_metric="score",
            baseline=60.0,
            candidate=71.5,
            delta=11.5,
            verdict="progression",
            cost_usd={"baseline": 0.40, "variant": 0.42},
            latency_s={"baseline": 12.0, "variant": 12.5},
        )
        self.dataset_id = uuid.uuid4()
        self.evaluation_id = uuid.uuid4()
        self.proposal_id = uuid.uuid4()
        for name in (
            "budget_left",
            "create_agent_copy",
            "simulated_writes",
            "build_dataset",
            "launch",
            "compare",
            "file_proposal",
        ):
            monkeypatch.setattr(quality_deps, name, getattr(self, name))

    def _seen(self, call: str, /, **kwargs) -> None:
        self.calls.setdefault(call, []).append(kwargs)

    async def budget_left(self, _db, cycle_id):
        self._seen("budget_left", cycle_id=cycle_id)
        return self.budget

    async def create_agent_copy(self, _db, **kwargs):
        self._seen("create_agent_copy", **kwargs)
        return uuid.uuid4(), uuid.uuid4()

    async def simulated_writes(self, _db, *, copy_id, since):
        self._seen("simulated_writes", copy_id=copy_id)
        return {"case-1": 2, "case-2": 1}

    async def build_dataset(self, _db, **kwargs):
        self._seen("build_dataset", **kwargs)
        return quality_deps.DatasetBuilt(dataset_id=self.dataset_id, entries=self.entries)

    async def launch(self, _db, **kwargs):
        self._seen("launch", **kwargs)
        return self.evaluation_id, [uuid.uuid4()], [uuid.uuid4()]

    async def compare(self, _db, **kwargs):
        self._seen("compare", **kwargs)
        return self.compared

    async def file_proposal(self, _db, *, author_agent, **spec):
        self._seen("file_proposal", author_agent=author_agent, **spec)
        return self.proposal_id, True


async def seed(db):
    owner = User(id=uuid.uuid4(), email=f"ce-{uuid.uuid4().hex[:8]}@example.com", full_name="CE", hashed_password="x")
    db.add(owner)
    await db.flush()
    skill = AgentSkill(name="Invoices", user_id=owner.id, content=SKILL, description="Invoice lookups")
    db.add(skill)
    await db.flush()
    agent = Agent(id=uuid.uuid4(), name="Billing agent", user_id=owner.id, persona=PERSONA, skill_ids=[str(skill.id)])
    experimenter = Agent(id=uuid.uuid4(), name="Experimenter", user_id=owner.id, persona="x")
    db.add_all([agent, experimenter])
    await db.flush()
    assignment = Assignment(
        agent_id=agent.id, user_id=owner.id, name="Daily", trigger_type="manual", prompt=PROMPT
    )
    db.add(assignment)
    recorded = await record_finding(
        db,
        target_type="agent",
        target_id=agent.id,
        pattern_kind="negative_feedback",
        pattern_key="",
        hypothesis="Answers omit the invoice number.",
        suggested_experiment="Ask for the invoice number in the persona.",
        evidence=Evidence(rating_ids=(str(uuid.uuid4()),), counts={"negative": 9}),
        content_evidence=True,
        author_agent_id=None,
    )
    await db.flush()
    return agent, skill, assignment, experimenter, recorded.finding


async def draft_variant(db, agent, finding, change_type="persona", target_id=None, text=VARIANT):
    experiment, created = await experiments.draft(
        db,
        finding=finding,
        change_type=change_type,
        target_id=target_id or agent.id,
        payload={"text": text, "summary": "Quote the invoice number, as the negative ratings ask."},
        author_agent_id=None,
    )
    return experiment, created


async def run_experiment(db, agent, experiment, **overrides):
    params = {
        "content_readable": True,
        "signals": {"tokens_per_run": 2_000, "cost_per_run": 0.02},
        "requested_cases": 20,
        "author_agent_id": None,
        # An experiment runs inside a cycle; level 1 is stubbed, any id stands for one.
        "cycle_id": uuid.uuid4(),
    }
    del agent  # the kind reads the experiment's own subject
    return await experiments.run(db, experiment=experiment, **{**params, **overrides})
