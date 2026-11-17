"""Create a fleet from a prompt, without a database.

What is pinned here: the model's answer is found fenced, bare or in prose;
member keys are slugified and every reference follows; a new member is reduced
to a minimal bundle (no invented tool, model or integration); an empty, too
long or cut prompt or answer is refused, never parsed; the generator previews
through ``apply_fleet_spec`` as a dry run and writes nothing. The PostgreSQL
round trip is ``test_fleet_spec_generation_postgres.py``.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import fleet_spec_generation as gen
from app.services.fleet_spec import FLEET_SPEC_API_VERSION, FleetSpecError, FleetSpecPlan


def _answer() -> dict:
    return {
        "metadata": {"name": "Alert Desk"},
        "spec": {
            "orchestrator": "Triage Lead",
            "members": [
                {"key": "Triage Lead", "handovers": [{"to": "Analyst", "type": "delegate"}]},
                {
                    "key": "Analyst",
                    "parent": "Triage Lead",
                    "agent": {"name": "Analyst", "persona": "You are the analyst.", "builtin_tool_ids": ["x"]},
                },
                {"key": "reporter", "parent": "Triage Lead", "agent": {"ref": {"slug": "reporter", "extra": 1}}},
            ],
        },
    }


def test_the_spec_is_found_fenced_bare_or_in_prose() -> None:
    body = json.dumps({"kind": "Fleet"})
    assert gen.extract_spec(body) == {"kind": "Fleet"}
    assert gen.extract_spec(f"```json\n{body}\n```") == {"kind": "Fleet"}
    assert gen.extract_spec(f"Here it is: {body} - enjoy") == {"kind": "Fleet"}
    with pytest.raises(FleetSpecError):
        gen.extract_spec("")
    with pytest.raises(FleetSpecError):
        gen.extract_spec("[1, 2]")


def test_keys_are_slugified_and_every_reference_follows() -> None:
    answer = _answer()
    spec, notes = gen.normalise_generated_spec(answer)
    assert spec["apiVersion"] == FLEET_SPEC_API_VERSION and spec["kind"] == "Fleet"
    assert spec["metadata"]["slug"] == "alert-desk"
    keys = [m["key"] for m in spec["spec"]["members"]]
    assert keys == ["triage-lead", "analyst", "reporter"]
    assert spec["spec"]["orchestrator"] == "triage-lead"
    assert spec["spec"]["members"][0]["handovers"] == [{"to": "analyst", "type": "delegate"}]
    assert spec["spec"]["members"][1]["parent"] == "triage-lead"
    assert any("Triage Lead" in n for n in notes)
    assert answer["spec"]["members"][0]["key"] == "Triage Lead", "the input is not mutated"


def test_a_new_member_is_a_minimal_bundle_and_a_ref_stays_a_ref() -> None:
    spec, notes = gen.normalise_generated_spec(_answer())
    lead, analyst, reporter = spec["spec"]["members"]
    assert analyst["agent"] == {
        "version": 3,
        "export_type": "agent",
        "agent": {"name": "Analyst", "persona": "You are the analyst."},
    }
    assert any("builtin_tool_ids left out" in n for n in notes)
    assert lead["agent"]["agent"]["name"] == "triage-lead", "a member without a bundle is named after its key"
    assert reporter["agent"] == {"ref": {"slug": "reporter"}}


def test_the_messages_carry_the_prompt_the_context_and_the_spec_to_refine() -> None:
    messages = gen.build_messages("an alert desk", {"agents": []}, {"kind": "Fleet"})
    assert messages[0].role == "system" and FLEET_SPEC_API_VERSION in messages[0].content
    assert "an alert desk" in messages[1].content and "Refine this spec" in messages[1].content


class _Model:
    def __init__(self, content: str, stop_reason: str = "end_turn") -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.calls: list[int] = []

    async def chat(self, messages, *, model, temperature, max_tokens):
        self.calls.append(max_tokens)
        return SimpleNamespace(content=self.content, stop_reason=self.stop_reason)


@pytest.fixture
def no_database(monkeypatch):
    applied: list[dict] = []

    async def _context(_db, _user, _groups):
        return {"agents": [], "fleets": [], "workspaces": []}

    async def _apply(_db, _user, spec, _groups, *, dry_run, workspace_id=None):
        applied.append({"spec": spec, "dry_run": dry_run, "workspace_id": workspace_id})
        plan = FleetSpecPlan(dry_run=dry_run)
        plan.add("fleet", spec["metadata"]["slug"], "create")
        return plan

    monkeypatch.setattr(gen, "generation_context", _context)
    monkeypatch.setattr(gen, "apply_fleet_spec", _apply)
    return applied


async def test_the_generator_previews_as_a_dry_run(no_database) -> None:
    model = _Model("```json\n" + json.dumps(_answer()) + "\n```")
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    out = await gen.generate_fleet_spec(object(), user, [], "an alert desk", llm=model, model="m")
    assert no_database == [{"spec": out.spec, "dry_run": True, "workspace_id": None}]
    assert out.plan.dry_run and not out.plan.applied
    assert out.as_dict()["plan"]["changes"][0] == {
        "object": "fleet",
        "name": "alert-desk",
        "action": "create",
        "detail": "",
    }
    assert model.calls == [gen.GENERATE_MAX_TOKENS]


@pytest.mark.parametrize("prompt", ["", "   ", "x" * (gen.PROMPT_MAX_CHARS + 1)])
async def test_an_empty_or_too_long_prompt_is_refused_before_the_model(no_database, prompt: str) -> None:
    model = _Model("{}")
    with pytest.raises(FleetSpecError):
        await gen.generate_fleet_spec(object(), SimpleNamespace(id=uuid.uuid4()), [], prompt, llm=model)
    assert model.calls == [] and no_database == []


async def test_a_cut_or_invalid_answer_is_refused_and_nothing_is_previewed(no_database, monkeypatch) -> None:
    monkeypatch.setattr("app.llm.cut_answers.retry_output_budget", lambda *_a, **_k: 0)
    cut = _Model(json.dumps(_answer()), stop_reason="max_tokens")
    with pytest.raises(FleetSpecError, match="cut"):
        await gen.generate_fleet_spec(object(), SimpleNamespace(id=uuid.uuid4()), [], "a desk", llm=cut)
    broken = _Model(json.dumps({"metadata": {"name": "X"}, "spec": {"orchestrator": "ghost", "members": []}}))
    with pytest.raises(FleetSpecError, match="orchestrator"):
        await gen.generate_fleet_spec(object(), SimpleNamespace(id=uuid.uuid4()), [], "a desk", llm=broken)
    assert no_database == []


def test_the_route_is_gated_by_ai_enabled_and_the_licence() -> None:
    from app.api.deps import require_agentic_ai
    from app.api.v1.fleets import router

    route = next(r for r in router.routes if getattr(r, "path", "").endswith("/generate"))
    assert "POST" in route.methods
    assert any(d.call is require_agentic_ai for d in route.dependant.dependencies)
