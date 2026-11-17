"""A placebo comparison settled, measured and served, on real PostgreSQL (level 3).

``dev-docs/agent-placebo-evaluation.md`` FR-23 to FR-31: a comparison settles once
none of its runs is pending or running (completed, failed or cancelled), with
its statistics read from the vertical's stored metrics; a deleted run cancels
it; the routes are the admin's, and the agent tools read and start comparisons
under the same rule as every evaluation tool, tier by tier in strict mode.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.agent import Agent
from app.models.evaluation import Evaluation, EvaluationPlaceboComparison, EvaluationRun
from app.models.group import Group, GroupMember
from app.models.user import User
from app.services import evaluation_engine as ee
from app.services.builtin_tools.evaluations import build_evaluation_tools
from app.services.evaluation import placebo_runs
from app.services.evaluation.placebo_arms import ARM_PLACEBO
from app.services.evaluation.placebo_comparisons import settle_comparisons_of_runs
from app.services.evaluation.placebo_stats import VERDICT_BEATS, VERDICT_NOT_COMPUTABLE
from app.services.evaluation_placebo import LengthMeasurer
from app.tools.base import ToolCall
from tests.test_evaluation_placebo_runs_postgres import PERSONA, _client, _no_wake, _runs, _scratch, _seed

pytestmark = pytest.mark.integration

__all__ = ["_no_wake"]

PERSONA_ONLY = {"targets": [{"kind": "persona"}], "trials_per_entry": 2}


@pytest.fixture
def stubbed(monkeypatch):
    """The real persona answers every entry right, any other wrong; the judge is off."""

    async def _full_loop(_db, _agent, _model_id, _input_prompt, *_args, evaluation_overrides=None, **_kwargs):
        overrides = evaluation_overrides or {}
        right = overrides.get("persona", PERSONA) == PERSONA
        # The real skill is opened by its own name; a placebo or a removed one is not.
        real_skills = not overrides.get("skill_substitutes") and not overrides.get("skill_ids_removed")
        timeline = [{"type": "tool", "tool_name": "load_skill", "tool_input": '{"name": "Triage"}'}]
        return ("A" if right else "B"), [], None, [], timeline if real_skills else []

    async def _quarter_tokens(text: str) -> int:
        return -(-len(text) // 4)

    async def _measurer(*_args, **_kwargs) -> LengthMeasurer:
        return LengthMeasurer("tokens", _quarter_tokens)

    async def _no_price_rows(_db, _model_id):
        return []

    async def _no_tools(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ee, "_execute_agent_full_loop", _full_loop)
    monkeypatch.setattr(ee, "_build_run_price_rows", _no_price_rows)
    monkeypatch.setattr(ee, "_build_evaluation_integration_tools", _no_tools)
    monkeypatch.setattr(placebo_runs, "probe_token_measurer", _measurer)


async def _launch(scratch, seeded) -> list[EvaluationRun]:
    async with _client(scratch.sessions, seeded.admin) as client:
        response = await client.post(f"/api/v1/evaluations/{seeded.evaluation_id}/trigger", json={"placebo": True})
    assert response.status_code == 200, response.text
    return await _runs(scratch.sessions, seeded.evaluation_id)


async def _execute(scratch, runs) -> None:
    for run in runs:
        async with scratch.sessions() as db:
            await ee.execute_evaluation_run(db, str(run.id))


async def _seeded(scratch, **fields):
    return await _seed(scratch.sessions, config=PERSONA_ONLY, entries=4, scorer_selection=[], **fields)


async def test_a_finished_comparison_settles_on_read_with_its_statistics(stubbed):
    async with _scratch("placebo_l3_settle") as scratch:
        seeded = await _seeded(scratch)
        await _execute(scratch, await _launch(scratch, seeded))
        async with _client(scratch.sessions, seeded.admin) as client:
            listed = await client.get(f"/api/v1/evaluations/{seeded.evaluation_id}/placebo-comparisons")
            (comparison,) = listed.json()
            detail = await client.get(f"/api/v1/evaluations/placebo-comparisons/{comparison['id']}")

    assert listed.status_code == detail.status_code == 200, listed.text
    assert detail.json() == comparison
    assert comparison["status"] == "completed"
    assert len(comparison["runs"]) == 6
    (row,) = comparison["results"]["rows"]
    assert row["target_key"] == "persona"
    assert row["entries"] == 4
    assert (row["arms"]["real"]["trials"], row["arms"]["placebo"]["trials"], row["arms"]["no_target"]["trials"]) == (
        8, 8, 8,
    )
    assert row["vs_placebo"]["quality"]["estimate"] == 100.0
    assert row["vs_no_target"]["quality"]["estimate"] == 100.0
    assert row["placebo_vs_no_target"]["quality"]["estimate"] == 0.0
    assert row["verdict"] == VERDICT_BEATS
    assert "few_entries" in row["warnings"]


async def test_a_comparison_stays_running_while_one_of_its_runs_may_still_run(stubbed):
    async with _scratch("placebo_l3_running") as scratch:
        seeded = await _seeded(scratch)
        runs = await _launch(scratch, seeded)
        await _execute(scratch, runs[:2])
        async with _client(scratch.sessions, seeded.admin) as client:
            (comparison,) = (await client.get(f"/api/v1/evaluations/{seeded.evaluation_id}/placebo-comparisons")).json()

    assert (comparison["status"], comparison["results"], comparison["completed_at"]) == ("running", None, None)


async def test_a_failed_run_settles_its_comparison_failed_and_its_cell_without_a_verdict(stubbed):
    async with _scratch("placebo_l3_failed") as scratch:
        seeded = await _seeded(scratch)
        runs = await _launch(scratch, seeded)
        await _execute(scratch, runs)
        async with scratch.sessions() as db:
            failed = await db.get(EvaluationRun, next(run.id for run in runs if run.arm == ARM_PLACEBO))
            failed.status = "failed"
            await db.commit()
            await settle_comparisons_of_runs(db, [run.id for run in runs])
            comparison = (await db.execute(select(EvaluationPlaceboComparison))).scalar_one()

    assert comparison.status == "failed"
    (row,) = comparison.results["rows"]
    assert row["verdict"] == VERDICT_NOT_COMPUTABLE
    assert "run_incomplete" in row["warnings"]


async def test_the_comparisons_of_a_finished_batch_settle_once(stubbed):
    async with _scratch("placebo_l3_batch") as scratch:
        seeded = await _seeded(scratch)
        runs = await _launch(scratch, seeded)
        await _execute(scratch, runs)
        async with scratch.sessions() as db:
            await settle_comparisons_of_runs(db, [str(run.id) for run in runs])
            first = (await db.execute(select(EvaluationPlaceboComparison))).scalar_one()
            settled_at, results = first.completed_at, first.results
            await settle_comparisons_of_runs(db, [str(run.id) for run in runs])
            await db.refresh(first)

    assert first.status == "completed"
    assert (first.completed_at, first.results) == (settled_at, results)


async def test_deleting_a_comparison_s_run_cancels_it_and_deleting_every_run_removes_it(stubbed):
    async with _scratch("placebo_l3_delete") as scratch:
        seeded = await _seeded(scratch)
        runs = await _launch(scratch, seeded)
        await _execute(scratch, runs)
        async with _client(scratch.sessions, seeded.admin) as client:
            one = await client.delete(f"/api/v1/evaluations/runs/{runs[0].id}")
            async with scratch.sessions() as db:
                comparison = (await db.execute(select(EvaluationPlaceboComparison))).scalar_one()
            every = await client.delete(f"/api/v1/evaluations/{seeded.evaluation_id}/runs")
            async with scratch.sessions() as db:
                left = (await db.execute(select(EvaluationPlaceboComparison))).scalars().all()

    assert one.status_code == every.status_code == 204
    assert (comparison.status, comparison.results) == ("cancelled", None)
    assert comparison.warnings[-1] == {"code": "run_deleted"}
    assert left == []


async def test_only_an_admin_reads_comparisons_and_an_unknown_id_is_not_found(stubbed):
    async with _scratch("placebo_l3_routes") as scratch:
        seeded = await _seeded(scratch)
        await _launch(scratch, seeded)
        async with scratch.sessions() as db:
            member = User(email=f"m-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="M")
            db.add(member)
            await db.commit()
            comparison_id = (await db.execute(select(EvaluationPlaceboComparison.id))).scalar_one()
        async with _client(scratch.sessions, member) as client:
            listed = await client.get(f"/api/v1/evaluations/{seeded.evaluation_id}/placebo-comparisons")
            one = await client.get(f"/api/v1/evaluations/placebo-comparisons/{comparison_id}")
        async with _client(scratch.sessions, seeded.admin) as client:
            unknown = await client.get(f"/api/v1/evaluations/placebo-comparisons/{uuid.uuid4()}")
            malformed = await client.get("/api/v1/evaluations/placebo-comparisons/not-an-id")

    assert listed.status_code == one.status_code == 403
    assert unknown.status_code == malformed.status_code == 404
    assert unknown.json() == {"detail": "Placebo comparison not found"}


async def _tool(user: User, name: str, **arguments) -> str:
    _, executor = build_evaluation_tools(None, str(user.id), include_withheld=True)
    result = await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))
    return result.content


async def test_the_agent_tools_start_a_comparison_and_read_its_statistics(stubbed):
    async with _scratch("placebo_l3_tools") as scratch:
        seeded = await _seeded(scratch)
        async with scratch.sessions() as db:
            admin = await db.get(User, seeded.admin.id)
        started = json.loads(await _tool(admin, "trigger_evaluation", evaluation_id=str(seeded.evaluation_id),
                                         placebo=True))
        evaluation = json.loads(await _tool(admin, "get_evaluation", evaluation_id=str(seeded.evaluation_id)))
        await _execute(scratch, await _runs(scratch.sessions, seeded.evaluation_id))
        read = json.loads(await _tool(admin, "get_placebo_comparison", evaluation_id=str(seeded.evaluation_id)))

    assert started["count"] == 6
    assert evaluation["placebo_config"] == PERSONA_ONLY
    assert read["comparison"]["status"] == "completed"
    assert [row["verdict"] for row in read["comparison"]["results"]["rows"]] == [VERDICT_BEATS]


async def _share_world(scratch, seeded, share: str) -> User:
    """A second admin, and the evaluation shared with it as ``share`` says."""
    async with scratch.sessions() as db:
        reader = User(email=f"r-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="R", role="admin")
        group = Group(name=f"g-{uuid.uuid4().hex[:8]}")
        db.add_all([reader, group])
        await db.flush()
        db.add(GroupMember(group_id=group.id, user_id=reader.id))
        evaluation = await db.get(Evaluation, seeded.evaluation_id)
        agent = await db.get(Agent, seeded.agent_id)
        reader_id = str(reader.id)
        if share in {"read_only", "config", "revoked"}:
            for row in (evaluation, agent):
                row.shared_user_ids = [reader_id]
                row.shared_user_permissions = {reader_id: "config" if share == "config" else "read_only"}
        if share == "group":
            for row in (evaluation, agent):
                row.shared_group_ids = [str(group.id)]
                row.shared_group_permissions = {str(group.id): "read_only"}
        if share == "company":
            evaluation.is_company_managed = True
            agent.is_company_managed = True
        if share == "evaluation_only":
            evaluation.shared_user_ids = [reader_id]
            evaluation.shared_user_permissions = {reader_id: "read_only"}
        await db.commit()
        if share == "revoked":
            evaluation.shared_user_ids, evaluation.shared_user_permissions = [], {}
            await db.commit()
        return reader


@pytest.mark.parametrize(
    ("share", "sees_rows"),
    [
        ("private", None),
        ("read_only", True),
        ("config", True),
        ("group", True),
        ("revoked", None),
        ("company", True),
        ("evaluation_only", False),
    ],
    ids=["private", "read-only-share", "config-share", "group-grant", "revoked-share", "company-managed",
         "agent-not-shared"],
)
async def test_in_strict_mode_the_comparison_tool_keeps_to_the_caller_s_tiers(monkeypatch, stubbed, share, sees_rows):
    async with _scratch("placebo_l3_tiers") as scratch:
        seeded = await _seeded(scratch)
        await _execute(scratch, await _launch(scratch, seeded))
        reader = await _share_world(scratch, seeded, share)
        monkeypatch.setattr(settings, "agent_tools_strict_admin_access", True)
        content = await _tool(reader, "get_placebo_comparison", evaluation_id=str(seeded.evaluation_id))
        monkeypatch.setattr(settings, "agent_tools_strict_admin_access", False)
        bypass = json.loads(await _tool(reader, "get_placebo_comparison", evaluation_id=str(seeded.evaluation_id)))

    assert len(bypass["comparison"]["results"]["rows"]) == 1
    if sees_rows is None:
        assert content == "Error: evaluation not found."
        return
    shown = json.loads(content)["comparison"]
    assert len(shown["results"]["rows"]) == (1 if sees_rows else 0)
    assert len(shown["runs"]) == (6 if sees_rows else 0)


async def test_a_member_s_agent_cannot_read_a_comparison(stubbed):
    async with _scratch("placebo_l3_member_tool") as scratch:
        seeded = await _seeded(scratch)
        await _launch(scratch, seeded)
        async with scratch.sessions() as db:
            member = User(email=f"m-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="M")
            db.add(member)
            await db.commit()
        content = await _tool(member, "get_placebo_comparison", evaluation_id=str(seeded.evaluation_id))

    assert content == "Error: only admins can read placebo comparisons."


async def test_the_execution_settles_its_comparisons_when_its_batch_ends(stubbed):
    async with _scratch("placebo_l3_hook") as scratch:
        seeded = await _seeded(scratch)
        runs = await _launch(scratch, seeded)
        async with scratch.sessions() as db:
            await ee.execute_evaluation(
                db, str(seeded.evaluation_id), "manual", runs[0].owner_execution_id, worker_id=ee.NO_LEASE
            )
        async with scratch.sessions() as db:
            comparison = (await db.execute(select(EvaluationPlaceboComparison))).scalar_one()

    assert comparison.status == "completed"
    assert [row["verdict"] for row in comparison.results["rows"]] == [VERDICT_BEATS]


@pytest.mark.parametrize(("pinned", "loaded"), [(False, (1.0, 0.0)), (True, (None, None))], ids=["loaded", "pinned"])
async def test_a_skill_target_says_how_often_each_arm_opened_it(stubbed, pinned, loaded):
    async with _scratch("placebo_l3_skill_loads") as scratch:
        seeded = await _seeded(scratch)
        async with scratch.sessions() as db:
            evaluation = await db.get(Evaluation, seeded.evaluation_id)
            evaluation.placebo_config = {
                "targets": [{"kind": "skill", "skill_id": str(seeded.skill_id)}],
                "trials_per_entry": 1,
            }
            if pinned:
                agent = await db.get(Agent, seeded.agent_id)
                agent.skill_load_modes = {str(seeded.skill_id): "always"}
            await db.commit()
        await _execute(scratch, await _launch(scratch, seeded))
        async with _client(scratch.sessions, seeded.admin) as client:
            (comparison,) = (await client.get(f"/api/v1/evaluations/{seeded.evaluation_id}/placebo-comparisons")).json()

    (row,) = comparison["results"]["rows"]
    assert (row["arms"]["real"]["skill_loaded"], row["arms"]["placebo"]["skill_loaded"]) == loaded
    assert row["arms"]["no_target"]["skill_loaded"] is None


async def test_a_comparison_run_serves_its_arm_and_placebo_and_refuses_a_replay(stubbed):
    async with _scratch("placebo_l4_run_fields") as scratch:
        seeded = await _seeded(scratch)
        runs = await _launch(scratch, seeded)
        await _execute(scratch, runs)
        placebo = next(run for run in runs if run.arm == ARM_PLACEBO)
        async with _client(scratch.sessions, seeded.admin) as client:
            listed = (await client.get(f"/api/v1/evaluations/{seeded.evaluation_id}/runs")).json()
            detail = (await client.get(f"/api/v1/evaluations/runs/{placebo.id}")).json()

    by_arm = {(run["arm"], run["trial_index"]) for run in listed}
    assert by_arm == {(arm, trial) for arm in ("real", "placebo", "no_target") for trial in (0, 1)}
    assert {run["placebo_comparison_id"] for run in listed} == {str(placebo.placebo_comparison_id)}
    assert (detail["arm"], detail["placebo_target"]) == ("placebo", {"kind": "persona", "skill_id": None})
    assert detail["placebo_snapshot"]["elements"][0]["content"]
    assert detail["can_replay"] is False
    assert detail["replay_block_reason"] == (
        "A placebo comparison's run cannot be re-run on its own: launch a new placebo comparison"
    )


async def test_the_runs_list_leaves_a_comparison_out_on_request_and_its_runs_are_never_re_scored_alone(stubbed):
    async with _scratch("placebo_l4_list_rescore") as scratch:
        seeded = await _seeded(scratch)
        async with scratch.sessions() as db:
            plain = EvaluationRun(
                evaluation_id=seeded.evaluation_id, agent_id=seeded.agent_id, model_id="gpt-4o",
                trigger_reason="manual", status="completed",
            )
            db.add(plain)
            await db.commit()
        runs = await _launch(scratch, seeded)
        await _execute(scratch, runs)
        control = next(run for run in runs if run.arm == ARM_PLACEBO)
        url = f"/api/v1/evaluations/{seeded.evaluation_id}"
        async with _client(scratch.sessions, seeded.admin) as client:
            every = (await client.get(f"{url}/runs", params={"limit": 100})).json()
            outside = (await client.get(f"{url}/runs", params={"limit": 100, "exclude_comparisons": True})).json()
            one = await client.post(f"/api/v1/evaluations/runs/{control.id}/rescore", json={})
            bulk = await client.post(f"{url}/rescore-from-runs", json={"run_ids": [str(control.id)]})

    assert len(every) == 7
    assert [run["id"] for run in outside] == [str(plain.id)]
    assert (one.status_code, one.json()) == (
        422,
        {"detail": "A placebo comparison's run cannot be re-scored on its own: launch a new placebo comparison"},
    )
    assert bulk.status_code == 200, bulk.text
    assert [(r["run_id"], r["status"], r["error"]) for r in bulk.json()["results"]] == [
        (str(control.id), "failed", "placebo_comparison_run")
    ]
