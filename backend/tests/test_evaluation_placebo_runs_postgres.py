"""A placebo comparison launched and run on the evaluation vertical, on real PostgreSQL (level 2).

``dev-docs/agent-placebo-evaluation.md`` FR-11 to FR-21: the trigger creates one
run per arm x trial through the launch every trigger uses, interleaved and
owned by one execution; what refuses it writes nothing; each arm shows the
model its own persona, Prompts and skills; the judge reads the same input
whatever the arm; no comparison run reuses an answer; comparison runs stay out
of Overall Performance's basis and the chart; and a comparison's run is never
re-run alone.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections import Counter
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import evaluations as evaluations_api
from app.core.database import get_db
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.evaluation import (
    Evaluation,
    EvaluationAgent,
    EvaluationPlaceboComparison,
    EvaluationResult,
    EvaluationRun,
)
from app.models.prompt import Prompt
from app.models.user import User
from app.services import evaluation_engine as ee
from app.services.evaluation import placebo_launch, placebo_runs
from app.services.evaluation.placebo_arms import ARM_NO_TARGET, ARM_PLACEBO, ARM_REAL
from app.services.evaluation.resume_secret import stored_trigger_config
from app.services.evaluation_placebo import LengthMeasurer
from tests.evaluation_helpers import seed_provider
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

PERSONA = "You are a threat intelligence analyst who reads reports and writes a synthesis."
MODEL = "gpt-4o"


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


async def _seed(sessions, *, config: dict | None, entries: int = 2, **evaluation_fields) -> SimpleNamespace:
    async with sessions() as db:
        admin = User(
            email=f"admin-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="Admin", role="admin"
        )
        db.add(admin)
        await db.flush()
        await seed_provider(db, [MODEL])
        prompt = Prompt(user_id=admin.id, name="Style guide", description="How answers read.", content="Write short paragraphs.")
        skill = AgentSkill(user_id=admin.id, name="Triage", description="Sort the alerts.", content="Sort by severity first.")
        db.add_all([prompt, skill])
        await db.flush()
        agent_fields = evaluation_fields.pop("agent_fields", {})
        agent = Agent(
            name="Analyst",
            slug=f"analyst-{uuid.uuid4().hex[:8]}",
            user_id=admin.id,
            persona=PERSONA,
            prompt_ids=[str(prompt.id)],
            skill_ids=[str(skill.id)],
            **agent_fields,
        )
        dataset = Dataset(user_id=admin.id, name="D", family="PROSE")
        evaluation = Evaluation(
            user_id=admin.id,
            name="E",
            family="PROSE",
            execution_mode="full_loop",
            use_agent_default_model=False,
            model_ids=[MODEL],
            reuse_default="on",
            placebo_config=config,
            **evaluation_fields,
        )
        db.add_all([agent, dataset, evaluation])
        await db.flush()
        db.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
        db.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=dataset.id))
        for position in range(entries):
            db.add(
                DatasetEntry(
                    dataset_id=dataset.id, position=position, input_prompt=f"Question {position}", expected_answer="A"
                )
            )
        await db.commit()
        return SimpleNamespace(
            admin=admin, agent_id=agent.id, evaluation_id=evaluation.id, skill_id=skill.id, prompt_id=prompt.id
        )


@asynccontextmanager
async def _client(sessions, user: User):
    app = FastAPI()
    app.include_router(evaluations_api.router, prefix="/api/v1/evaluations")

    async def _get_db():
        async with sessions() as session:
            yield session

    async def _allowed() -> None:
        return None

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[deps.require_ai_enabled] = _allowed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture(autouse=True)
def _no_wake(monkeypatch):
    from app.services import orchestration_engine

    async def _wake() -> None:
        return None

    monkeypatch.setattr(orchestration_engine, "notify_orchestration_wake", _wake)


async def _count(sessions, model) -> int:
    async with sessions() as db:
        return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def _runs(sessions, evaluation_id) -> list[EvaluationRun]:
    async with sessions() as db:
        return list(
            (
                await db.execute(
                    select(EvaluationRun)
                    .where(EvaluationRun.evaluation_id == evaluation_id)
                    .order_by(EvaluationRun.created_at)
                )
            ).scalars()
        )


ALL_TARGETS = {"targets": [{"kind": "persona"}, {"kind": "prompts"}, {"kind": "skills"}], "trials_per_entry": 2}


async def test_a_placebo_trigger_creates_one_run_per_arm_and_trial_owned_by_one_execution():
    async with _scratch("placebo_l2_trigger") as scratch:
        seeded = await _seed(scratch.sessions, config=ALL_TARGETS)
        async with _client(scratch.sessions, seeded.admin) as client:
            response = await client.post(f"/api/v1/evaluations/{seeded.evaluation_id}/trigger", json={"placebo": True})

        runs = await _runs(scratch.sessions, seeded.evaluation_id)
        async with scratch.sessions() as db:
            comparison = (await db.execute(select(EvaluationPlaceboComparison))).scalar_one()
            from app.services.evaluation import params as run_params

            reuse = {
                p["value"] for run in runs for p in await run_params.get_run_params(db, run.id) if p["key"] == "reuse"
            }

    assert response.status_code == 200, response.text
    # (real + 3 targets x (placebo + no target)) x 2 trials.
    assert len(response.json()) == len(runs) == 14
    assert Counter(run.arm for run in runs) == {ARM_REAL: 2, ARM_PLACEBO: 6, ARM_NO_TARGET: 6}
    assert [run.trial_index for run in runs] == [0] * 7 + [1] * 7
    assert {run.placebo_comparison_id for run in runs} == {comparison.id}
    assert len({run.owner_execution_id for run in runs}) == 1
    assert {run.status for run in runs} == {"pending"}
    assert reuse == {"off"}
    assert comparison.status == "running"
    assert comparison.config == ALL_TARGETS | {"include_no_target": True, "custom_text": None, "targets": [
        {"kind": "persona", "skill_id": None},
        {"kind": "prompts", "skill_id": None},
        {"kind": "skills", "skill_id": None},
    ]}


@pytest.mark.parametrize(
    ("config", "fields", "detail"),
    [
        (None, {}, "This evaluation has no placebo configuration"),
        (
            {"targets": [{"kind": "persona"}], "trials_per_entry": 2},
            {"trigger_config": stored_trigger_config({"await_callback": True, "resume_secret": "a-resume-secret"})},
            "A placebo comparison cannot wait for external callbacks",
        ),
        (
            {"targets": [{"kind": "skill", "skill_id": str(uuid.uuid4())}]},
            {},
            "No agent of this evaluation has any of the placebo targets",
        ),
        (
            {"targets": [{"kind": "persona"}], "custom_text": "Notes from an analyst about reports."},
            {},
            "No neutral placebo for the 'persona' target",
        ),
    ],
    ids=["no-configuration", "callback-mode", "no-arm", "leak"],
)
async def test_a_refused_placebo_trigger_writes_nothing(config, fields, detail):
    async with _scratch("placebo_l2_refused") as scratch:
        seeded = await _seed(scratch.sessions, config=config, **fields)
        async with _client(scratch.sessions, seeded.admin) as client:
            response = await client.post(f"/api/v1/evaluations/{seeded.evaluation_id}/trigger", json={"placebo": True})

        assert response.status_code in (400, 422), response.text
        assert detail in response.json()["detail"]
        assert await _count(scratch.sessions, EvaluationRun) == 0
        assert await _count(scratch.sessions, EvaluationPlaceboComparison) == 0


async def test_a_comparison_over_the_execution_cap_is_refused(monkeypatch):
    monkeypatch.setattr(placebo_launch, "MAX_COMPARISON_EXECUTIONS", 10)
    async with _scratch("placebo_l2_cap") as scratch:
        seeded = await _seed(scratch.sessions, config=ALL_TARGETS)
        async with _client(scratch.sessions, seeded.admin) as client:
            response = await client.post(f"/api/v1/evaluations/{seeded.evaluation_id}/trigger", json={"placebo": True})

        assert response.status_code == 422
        assert response.json()["detail"] == (
            "This comparison would run 28 agent executions (14 runs x 2 entries); the limit is 10"
        )
        assert await _count(scratch.sessions, EvaluationRun) == 0


async def test_unapproved_tools_need_a_confirmation_and_run_once_confirmed():
    async with _scratch("placebo_l2_tools") as scratch:
        seeded = await _seed(
            scratch.sessions,
            config={"targets": [{"kind": "persona"}], "trials_per_entry": 1},
            agent_fields={"mcp_server_ids": [str(uuid.uuid4())]},
        )
        async with _client(scratch.sessions, seeded.admin) as client:
            url = f"/api/v1/evaluations/{seeded.evaluation_id}/trigger"
            refused = await client.post(url, json={"placebo": True})
            confirmed = await client.post(url, json={"placebo": True, "confirm_write_tools": True})

    assert refused.status_code == 422
    assert refused.json()["detail"] == (
        "This placebo comparison repeats, for every arm and trial, the calls of tools this evaluation runs "
        "without approval (Analyst: 1 MCP server(s)). Send confirm_write_tools to launch it."
    )
    assert confirmed.status_code == 200
    assert len(confirmed.json()) == 3


async def test_the_estimate_counts_what_a_trigger_would_run_and_writes_nothing():
    async with _scratch("placebo_l2_estimate") as scratch:
        seeded = await _seed(scratch.sessions, config=ALL_TARGETS)
        async with _client(scratch.sessions, seeded.admin) as client:
            response = await client.post(f"/api/v1/evaluations/{seeded.evaluation_id}/placebo/estimate", json={})

        body = response.json()
        assert response.status_code == 200, response.text
        assert (body["runs"], body["agent_executions"], body["judge_calls"]) == (14, 28, 28)
        assert body["estimated_tokens"] is None
        assert body["refusals"] == []
        assert body["probe_calls"] > 0
        assert await _count(scratch.sessions, EvaluationRun) == 0


async def test_the_estimate_resolves_models_as_the_trigger_does():
    async with _scratch("placebo_l2_estimate_models") as scratch:
        seeded = await _seed(scratch.sessions, config=ALL_TARGETS)
        async with _client(scratch.sessions, seeded.admin) as client:
            url = f"/api/v1/evaluations/{seeded.evaluation_id}"
            twice = await client.post(f"{url}/placebo/estimate", json={"model_ids": [MODEL, MODEL]})
            unknown = await client.post(f"{url}/placebo/estimate", json={"model_ids": [MODEL, "no-such-model"]})
            unknown_trigger = await client.post(
                f"{url}/trigger", json={"placebo": True, "model_ids": [MODEL, "no-such-model"]}
            )

    assert twice.status_code == 200, twice.text
    assert twice.json()["runs"] == 14
    assert unknown.status_code == unknown_trigger.status_code == 400
    assert unknown.json() == unknown_trigger.json()


async def test_the_trigger_launches_with_the_seed_the_estimate_checked():
    async with _scratch("placebo_l2_estimate_seed") as scratch:
        seeded = await _seed(scratch.sessions, config=ALL_TARGETS)
        async with _client(scratch.sessions, seeded.admin) as client:
            url = f"/api/v1/evaluations/{seeded.evaluation_id}"
            seed = (await client.post(f"{url}/placebo/estimate", json={})).json()["seed"]
            invalid = await client.post(f"{url}/trigger", json={"placebo": True, "placebo_seed": "not a seed"})
            launched = await client.post(f"{url}/trigger", json={"placebo": True, "placebo_seed": seed})
        async with scratch.sessions() as db:
            comparison = (await db.execute(select(EvaluationPlaceboComparison))).scalar_one()

    assert invalid.status_code == 422
    assert launched.status_code == 200, launched.text
    assert comparison.seed == seed


async def test_a_comparison_lists_each_warning_as_one_object_with_a_code():
    missing_skill = str(uuid.uuid4())
    async with _scratch("placebo_l2_warnings") as scratch:
        seeded = await _seed(
            scratch.sessions,
            config={"targets": [{"kind": "persona"}, {"kind": "skill", "skill_id": missing_skill}]},
            run_mode="sequential",
        )
        async with _client(scratch.sessions, seeded.admin) as client:
            response = await client.post(f"/api/v1/evaluations/{seeded.evaluation_id}/trigger", json={"placebo": True})
        async with scratch.sessions() as db:
            comparison = (await db.execute(select(EvaluationPlaceboComparison))).scalar_one()

    assert response.status_code == 200, response.text
    assert comparison.warnings == [
        {"code": "sequential_run_mode"},
        {
            "code": "target_skipped",
            "agent_id": str(seeded.agent_id),
            "agent_name": "Analyst",
            "target": f"skill:{missing_skill}",
            "reason": "the skill is not bound to the agent, or the agent cannot load it",
        },
    ]


async def test_an_export_leaves_a_comparison_s_runs_out():
    async with _scratch("placebo_l2_export") as scratch:
        seeded = await _seed(scratch.sessions, config={"targets": [{"kind": "persona"}], "trials_per_entry": 1})
        async with scratch.sessions() as db:
            comparison = EvaluationPlaceboComparison(evaluation_id=seeded.evaluation_id, seed="s")
            db.add(comparison)
            await db.flush()
            common = {
                "evaluation_id": seeded.evaluation_id, "agent_id": seeded.agent_id, "model_id": MODEL,
                "trigger_reason": "manual", "status": "completed",
            }
            plain = EvaluationRun(**common)
            db.add(plain)
            db.add(EvaluationRun(**common, arm=ARM_NO_TARGET, placebo_comparison_id=comparison.id))
            await db.commit()
            plain_created = plain.created_at.isoformat()
        async with _client(scratch.sessions, seeded.admin) as client:
            exported = await client.get(f"/api/v1/evaluations/{seeded.evaluation_id}/export")

    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        definition = json.loads(archive.read("evaluation.json"))
    assert [run["created_at"] for run in definition["evaluation_runs"]] == [plain_created]


async def test_a_placebo_matches_its_original_as_the_model_reads_it_with_variables_resolved():
    from app.models.variable import Variable

    async with _scratch("placebo_l2_variables") as scratch:
        seeded = await _seed(scratch.sessions, config={"targets": [{"kind": "persona"}]})
        async with scratch.sessions() as db:
            variable = Variable(user_id=seeded.admin.id, name="ORG", value="the security operations centre of a bank")
            db.add(variable)
            await db.flush()
            agent = await db.get(Agent, seeded.agent_id)
            agent.persona = f"You work for {{{{var_id.{variable.id}}}}}."
            await db.commit()
            sources = await placebo_runs.load_placebo_target_sources(db, agent, user_id=str(seeded.admin.id))

    assert sources.persona == "You work for the security operations centre of a bank."


def _stub_runs(monkeypatch) -> SimpleNamespace:
    calls = SimpleNamespace(agent=[], judge=[], probes=0)

    async def _full_loop(_db, agent, _model_id, input_prompt, *_args, evaluation_overrides=None, **_kwargs):
        calls.agent.append((input_prompt, dict(evaluation_overrides or {})))
        return "The same answer.", [], None, [], []

    async def _judge(_db, agent_output, entry, *args, **kwargs):
        # What the judge reads: the answer, the entry, the tools and calls, the instructions.
        calls.judge.append(
            (
                agent_output,
                getattr(entry, "input_prompt", None),
                getattr(entry, "expected_answer", None),
                args,
                sorted(kwargs.items(), key=lambda item: item[0]),
            )
        )
        return {"score": 70.0, "sub_scores": {k: 70 for k in ee.SUB_SCORE_WEIGHTS}, "evaluator_reasoning": "ok", "is_judge_failure": False}

    async def _quarter_tokens(text: str) -> int:
        return -(-len(text) // 4)

    async def _measurer(*_args, usage=None, **_kwargs) -> LengthMeasurer:
        calls.probes += 1

        async def _measure(text: str) -> int:
            usage.calls += 1
            return await _quarter_tokens(text)

        return LengthMeasurer("tokens", _measure)

    async def _no_price_rows(_db, _model_id):
        return []

    async def _no_tools(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ee, "_execute_agent_full_loop", _full_loop)
    monkeypatch.setattr(ee, "score_result", _judge)
    monkeypatch.setattr(ee, "_build_run_price_rows", _no_price_rows)
    monkeypatch.setattr(ee, "_build_evaluation_integration_tools", _no_tools)
    monkeypatch.setattr(placebo_runs, "probe_token_measurer", _measurer)
    return calls


async def _trigger_and_run(scratch, seeded) -> list[EvaluationRun]:
    async with _client(scratch.sessions, seeded.admin) as client:
        response = await client.post(f"/api/v1/evaluations/{seeded.evaluation_id}/trigger", json={"placebo": True})
    assert response.status_code == 200, response.text
    for run in await _runs(scratch.sessions, seeded.evaluation_id):
        async with scratch.sessions() as db:
            await ee.execute_evaluation_run(db, str(run.id))
    return await _runs(scratch.sessions, seeded.evaluation_id)


async def test_each_arm_runs_every_entry_once_per_trial_with_its_own_target_and_no_reuse(monkeypatch):
    calls = _stub_runs(monkeypatch)
    async with _scratch("placebo_l2_run") as scratch:
        seeded = await _seed(scratch.sessions, config=ALL_TARGETS)
        runs = await _trigger_and_run(scratch, seeded)
        async with scratch.sessions() as db:
            results = list((await db.execute(select(EvaluationResult))).scalars())

    assert {run.status for run in runs} == {"completed"}, [run.error for run in runs]
    # Every run executes both entries once: 14 runs x 2 entries, none reused.
    assert len(calls.agent) == len(results) == 28
    assert {result.is_reused for result in results} == {False}
    # The placebo is probed once per target, then shared by the other trial.
    assert calls.probes == 3

    by_run = {run.id: run for run in runs}
    overrides_by_arm: dict[tuple[str, str | None], list[dict]] = {}
    for run, (_, overrides) in zip(
        [r for r in runs for _ in range(2)], calls.agent, strict=True
    ):
        key = (run.arm, (run.placebo_target or {}).get("kind"))
        overrides_by_arm.setdefault(key, []).append(overrides)
    assert set(by_run) == {run.id for run in runs}

    real = overrides_by_arm[(ARM_REAL, None)]
    assert all("skill_substitutes" not in o and "skill_ids_removed" not in o for o in real)
    assert all(o.get("persona", PERSONA) == PERSONA for o in real)
    placebo_persona = overrides_by_arm[(ARM_PLACEBO, "persona")]
    assert {o["persona"] for o in placebo_persona} != {PERSONA}
    assert len({o["persona"] for o in placebo_persona}) == 1
    assert {o["persona"] for o in overrides_by_arm[(ARM_NO_TARGET, "persona")]} == {""}
    assert {o["prompt_fragment"] for o in overrides_by_arm[(ARM_NO_TARGET, "prompts")]} == {""}
    placebo_prompts = overrides_by_arm[(ARM_PLACEBO, "prompts")]
    assert all("Style guide" not in o["prompt_fragment"] and "Attached Prompts" in o["prompt_fragment"] for o in placebo_prompts)
    assert {tuple(o["skill_ids_removed"]) for o in overrides_by_arm[(ARM_NO_TARGET, "skills")]} == {(str(seeded.skill_id),)}
    substitutes = [o["skill_substitutes"][str(seeded.skill_id)] for o in overrides_by_arm[(ARM_PLACEBO, "skills")]]
    assert {s.name for s in substitutes} != {"Triage"} and len({s.content for s in substitutes}) == 1

    placebo_runs_done = [run for run in runs if run.arm == ARM_PLACEBO]
    assert all(run.placebo_snapshot and run.placebo_snapshot["unit"] == "tokens" for run in placebo_runs_done)
    # The probes a placebo cost are stored with it, and its sibling trial shares them.
    assert {run.placebo_snapshot["probe_usage"]["calls"] > 0 for run in placebo_runs_done} == {True}


async def test_the_judge_reads_the_same_input_whatever_the_arm(monkeypatch):
    calls = _stub_runs(monkeypatch)
    async with _scratch("placebo_l2_judge") as scratch:
        seeded = await _seed(scratch.sessions, config={"targets": [{"kind": "persona"}], "trials_per_entry": 1})
        await _trigger_and_run(scratch, seeded)

    judged = {repr(call) for call in calls.judge}
    # Three arms x two entries, but only two distinct judge inputs: one per entry.
    assert len(calls.judge) == 6
    assert len(judged) == 2


async def test_comparison_runs_stay_out_of_the_performance_basis(monkeypatch):
    _stub_runs(monkeypatch)
    basis: list[list] = []

    async def _headlines(_db, _evaluation, run_ids):
        basis.append(sorted(run_ids))
        return {}

    monkeypatch.setattr(ee, "compute_performance_headlines", _headlines)
    async with _scratch("placebo_l2_basis") as scratch:
        seeded = await _seed(scratch.sessions, config={"targets": [{"kind": "persona"}], "trials_per_entry": 1})
        async with scratch.sessions() as db:
            plain = EvaluationRun(
                evaluation_id=seeded.evaluation_id, agent_id=seeded.agent_id, model_id=MODEL,
                trigger_reason="manual", status="completed",
            )
            db.add(plain)
            await db.commit()
            plain_id = plain.id
        await _trigger_and_run(scratch, seeded)

    assert basis
    assert {tuple(run_ids) for run_ids in basis} == {(plain_id,)}


async def test_a_comparison_run_is_never_re_run_alone(monkeypatch):
    _stub_runs(monkeypatch)
    async with _scratch("placebo_l2_replay") as scratch:
        seeded = await _seed(scratch.sessions, config={"targets": [{"kind": "persona"}], "trials_per_entry": 1})
        runs = await _trigger_and_run(scratch, seeded)
        control = next(run for run in runs if run.arm == ARM_PLACEBO)
        async with _client(scratch.sessions, seeded.admin) as client:
            replay = await client.post(f"/api/v1/evaluations/runs/{control.id}/replay", json={})
            rerun = await client.post(
                f"/api/v1/evaluations/{seeded.evaluation_id}/trigger-from-runs", json={"run_ids": [str(control.id)]}
            )

        detail = "A placebo comparison's run cannot be re-run on its own: launch a new placebo comparison"
        assert (replay.status_code, replay.json()) == (422, {"detail": detail})
        assert (rerun.status_code, rerun.json()) == (422, {"detail": detail})
        assert await _count(scratch.sessions, EvaluationRun) == len(runs)


async def test_comparison_runs_stay_out_of_the_chart():
    async with _scratch("placebo_l2_chart") as scratch:
        seeded = await _seed(scratch.sessions, config={"targets": [{"kind": "persona"}], "trials_per_entry": 1})
        async with scratch.sessions() as db:
            comparison = EvaluationPlaceboComparison(evaluation_id=seeded.evaluation_id, seed="s")
            db.add(comparison)
            await db.flush()
            common = {
                "evaluation_id": seeded.evaluation_id, "agent_id": seeded.agent_id, "model_id": MODEL,
                "trigger_reason": "manual", "status": "completed",
            }
            db.add(EvaluationRun(**common, quality_score=50.0))
            db.add(EvaluationRun(**common, quality_score=99.0, arm=ARM_PLACEBO, placebo_comparison_id=comparison.id))
            await db.commit()
        async with _client(scratch.sessions, seeded.admin) as client:
            response = await client.get(f"/api/v1/evaluations/{seeded.evaluation_id}/chart", params={"range": "all"})

    assert response.status_code == 200, response.text
    plotted = [value for point in response.json()["trends"]["quality"] for value in point["scores"].values()]
    assert plotted == [50.0]
