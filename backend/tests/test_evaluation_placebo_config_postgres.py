"""An evaluation's placebo configuration on every write path, on real PostgreSQL.

``dev-docs/agent-placebo-evaluation.md`` FR-1 to FR-4: create, update, copy,
export and import carry the configuration; a configuration whose targets the
execution mode cannot deliver is refused (422) - whether the conflict is in the
body or between the body and the stored row - and nothing is written. The
evaluation's settings list carries it on copy and export as every setting
(``tests/test_evaluation_settings.py``); this file pins what is placebo's own.
Last, the schema the placebo arms record into (section 7): what a run is before
any arm is set, and what removing a comparison or an evaluation leaves.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import evaluations as evaluations_api
from app.core.database import get_db
from app.models.agent import Agent
from app.models.evaluation import Evaluation, EvaluationPlaceboComparison, EvaluationRun
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

SKILL_ID = str(uuid.uuid4())
CONFIG = {
    "targets": [{"kind": "persona"}, {"kind": "skill", "skill_id": SKILL_ID}],
    "trials_per_entry": 8,
    "include_no_target": False,
}
#: What the API stores and serves for ``CONFIG``: every field, defaults included.
STORED = {
    "targets": [{"kind": "persona", "skill_id": None}, {"kind": "skill", "skill_id": SKILL_ID}],
    "include_no_target": False,
    "trials_per_entry": 8,
    "custom_text": None,
}


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


async def _user(db, *, admin: bool) -> User:
    user = User(
        email=f"user-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Admin" if admin else "Member",
        role="admin" if admin else "user",
    )
    db.add(user)
    await db.commit()
    return user


@asynccontextmanager
async def _client(sessions, user: User) -> AsyncIterator[httpx.AsyncClient]:
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
    app.dependency_overrides[deps.require_agentic_ai] = _allowed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _row(sessions, evaluation_id: str) -> Evaluation:
    async with sessions() as db:
        return (await db.execute(select(Evaluation).where(Evaluation.id == uuid.UUID(evaluation_id)))).scalar_one()


async def _count(sessions, model) -> int:
    async with sessions() as db:
        return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def test_create_stores_and_serves_the_configuration():
    async with _scratch("placebo_cfg_create") as scratch:
        async with scratch.sessions() as db:
            admin = await _user(db, admin=True)
        async with _client(scratch.sessions, admin) as client:
            response = await client.post(
                "/api/v1/evaluations",
                json={"name": "With placebo", "execution_mode": "full_loop", "placebo_config": CONFIG},
            )
            plain = await client.post("/api/v1/evaluations", json={"name": "Without"})

        assert response.status_code == 201, response.text
        assert response.json()["placebo_config"] == STORED
        assert (await _row(scratch.sessions, response.json()["id"])).placebo_config == STORED
        assert plain.status_code == 201
        assert plain.json()["placebo_config"] is None


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            {"execution_mode": "llm_only", "placebo_config": {"targets": [{"kind": "skills"}]}},
            "Placebo targets skills need the 'full_loop' execution mode",
        ),
        (
            {"execution_mode": "llm_only", "placebo_config": {"targets": [{"kind": "prompts"}]}},
            "Placebo targets prompts need the 'full_loop' execution mode: in 'llm_only'",
        ),
        ({"placebo_config": {"targets": []}}, "at least 1 item"),
        ({"placebo_config": {"targets": [{"kind": "persona"}], "trials_per_entry": 31}}, "less than or equal to 30"),
    ],
    ids=["skills-in-llm-only", "prompts-in-llm-only", "no-target", "too-many-trials"],
)
async def test_create_refuses_an_invalid_configuration_and_writes_nothing(body, message):
    async with _scratch("placebo_cfg_create_refused") as scratch:
        async with scratch.sessions() as db:
            admin = await _user(db, admin=True)
        async with _client(scratch.sessions, admin) as client:
            response = await client.post("/api/v1/evaluations", json={"name": "Refused", **body})

        assert response.status_code == 422
        assert message in json.dumps(response.json())
        assert await _count(scratch.sessions, Evaluation) == 0


async def test_update_sets_keeps_and_clears_the_configuration():
    async with _scratch("placebo_cfg_update") as scratch:
        async with scratch.sessions() as db:
            admin = await _user(db, admin=True)
        async with _client(scratch.sessions, admin) as client:
            created = (await client.post("/api/v1/evaluations", json={"name": "E", "execution_mode": "full_loop"})).json()
            url = f"/api/v1/evaluations/{created['id']}"

            set_response = await client.put(url, json={"placebo_config": CONFIG})
            kept_response = await client.put(url, json={"name": "Renamed"})
            kept_row = await _row(scratch.sessions, created["id"])
            cleared_response = await client.put(url, json={"placebo_config": None})

        assert set_response.status_code == 200, set_response.text
        assert set_response.json()["placebo_config"] == STORED
        assert kept_response.json()["placebo_config"] == STORED
        assert kept_row.placebo_config == STORED
        assert cleared_response.json()["placebo_config"] is None
        assert (await _row(scratch.sessions, created["id"])).placebo_config is None


@pytest.mark.parametrize(
    ("stored_mode", "stored_config", "body"),
    [
        # The body switches the mode under a stored configuration.
        ("full_loop", {"targets": [{"kind": "prompts"}]}, {"execution_mode": "llm_only"}),
        # The body sends a configuration the stored mode cannot deliver.
        ("llm_only", None, {"placebo_config": {"targets": [{"kind": "skills"}]}}),
        # Both in the body.
        ("full_loop", None, {"execution_mode": "llm_only", "placebo_config": {"targets": [{"kind": "skills"}]}}),
    ],
    ids=["mode-against-row", "config-against-row", "both-in-body"],
)
async def test_update_refuses_a_mode_that_cannot_deliver_the_targets_and_changes_nothing(
    stored_mode, stored_config, body
):
    async with _scratch("placebo_cfg_update_refused") as scratch:
        async with scratch.sessions() as db:
            admin = await _user(db, admin=True)
        async with _client(scratch.sessions, admin) as client:
            created = (
                await client.post(
                    "/api/v1/evaluations",
                    json={"name": "E", "execution_mode": stored_mode, "placebo_config": stored_config},
                )
            ).json()
            before = await _row(scratch.sessions, created["id"])

            response = await client.put(f"/api/v1/evaluations/{created['id']}", json=body)

        after = await _row(scratch.sessions, created["id"])
        assert response.status_code == 422
        assert "need the 'full_loop' execution mode" in json.dumps(response.json())
        assert (after.execution_mode, after.placebo_config) == (before.execution_mode, before.placebo_config)


async def test_a_stored_configuration_that_no_longer_validates_only_blocks_the_updates_touching_it():
    async with _scratch("placebo_cfg_update_stale") as scratch:
        async with scratch.sessions() as db:
            admin = await _user(db, admin=True)
        async with _client(scratch.sessions, admin) as client:
            created = (await client.post("/api/v1/evaluations", json={"name": "E", "execution_mode": "full_loop"})).json()
            async with scratch.sessions() as db:
                row = await db.get(Evaluation, uuid.UUID(created["id"]))
                # Written by no API path: a target kind this version does not know.
                row.placebo_config = {"targets": [{"kind": "tools"}]}
                await db.commit()
            url = f"/api/v1/evaluations/{created['id']}"

            # The configuration form sends the unchanged mode with every save.
            renamed = await client.put(url, json={"name": "Renamed", "execution_mode": "full_loop"})
            mode = await client.put(url, json={"execution_mode": "llm_only"})
            cleared = await client.put(url, json={"placebo_config": None})

        assert renamed.status_code == 200, renamed.text
        assert mode.status_code == 422
        assert mode.json() == {
            "detail": "The stored placebo configuration is no longer valid: send a new placebo_config or null"
        }
        assert cleared.status_code == 200
        stored = await _row(scratch.sessions, created["id"])
        assert (stored.name, stored.execution_mode, stored.placebo_config) == ("Renamed", "full_loop", None)


async def test_a_non_admin_cannot_set_a_configuration():
    async with _scratch("placebo_cfg_non_admin") as scratch:
        async with scratch.sessions() as db:
            admin = await _user(db, admin=True)
            member = await _user(db, admin=False)
        async with _client(scratch.sessions, admin) as client:
            created = (await client.post("/api/v1/evaluations", json={"name": "E", "execution_mode": "full_loop"})).json()
        async with _client(scratch.sessions, member) as client:
            create = await client.post(
                "/api/v1/evaluations",
                json={"name": "Mine", "execution_mode": "full_loop", "placebo_config": CONFIG},
            )
            update = await client.put(f"/api/v1/evaluations/{created['id']}", json={"placebo_config": CONFIG})

        assert (create.status_code, update.status_code) == (403, 403)
        assert (await _row(scratch.sessions, created["id"])).placebo_config is None
        assert await _count(scratch.sessions, Evaluation) == 1


async def test_an_export_carries_the_configuration_and_an_import_keeps_what_resolves_here():
    async with _scratch("placebo_cfg_export") as scratch:
        async with scratch.sessions() as db:
            admin = await _user(db, admin=True)
        async with _client(scratch.sessions, admin) as client:
            created = (
                await client.post(
                    "/api/v1/evaluations",
                    json={"name": "E", "execution_mode": "full_loop", "placebo_config": CONFIG},
                )
            ).json()
            exported = await client.get(f"/api/v1/evaluations/{created['id']}/export")
            imported = await client.post(
                "/api/v1/evaluations/import",
                files={"file": ("evaluation.zip", exported.content, "application/zip")},
            )

        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            definition = json.loads(archive.read("evaluation.json"))
        assert exported.status_code == 200
        assert definition["evaluation"]["placebo_config"] == STORED
        assert imported.status_code == 201, imported.text
        # The single-skill target names a skill of the exporting instance.
        assert imported.json()["placebo_config"] == STORED | {"targets": [{"kind": "persona", "skill_id": None}]}
        assert imported.json()["warnings"] == [
            f"Placebo target skill:{SKILL_ID} was dropped: it names a skill of the exporting instance"
        ]
        assert created["warnings"] == []


def _package(evaluation: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "evaluation.json",
            json.dumps(
                {
                    "version": 1,
                    "export_type": "evaluation_package",
                    "evaluation": {"name": "Imported", **evaluation},
                    "dependencies": {"agents": [{"index": 0, "path": "agents/agent-0.json"}]},
                }
            ),
        )
        archive.writestr("agents/agent-0.json", json.dumps({"name": "Never imported"}))
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("evaluation", "detail"),
    [
        ({"placebo_config": {"targets": [{"kind": "tools"}]}}, "placebo_config.targets.0.kind: Input should be 'persona', 'prompts', 'skills' or 'skill'"),
        (
            {"execution_mode": "llm_only", "placebo_config": {"targets": [{"kind": "skills"}]}},
            "evaluation: Value error, Placebo targets skills need the 'full_loop' execution mode: "
            "in 'llm_only' the model sees the persona alone",
        ),
    ],
    ids=["malformed", "mode-conflict"],
)
async def test_an_import_with_an_invalid_configuration_is_refused_before_anything_is_imported(evaluation, detail):
    async with _scratch("placebo_cfg_import_refused") as scratch:
        async with scratch.sessions() as db:
            admin = await _user(db, admin=True)
        async with _client(scratch.sessions, admin) as client:
            response = await client.post(
                "/api/v1/evaluations/import",
                files={"file": ("evaluation.zip", _package(evaluation), "application/zip")},
            )

        assert response.status_code == 422
        assert response.json() == {"detail": detail}
        assert await _count(scratch.sessions, Evaluation) == 0
        assert await _count(scratch.sessions, Agent) == 0


async def test_a_run_is_the_real_arm_and_the_first_trial_until_told_otherwise():
    async with _scratch("placebo_schema_defaults") as scratch:
        async with scratch.sessions() as db:
            admin = await _user(db, admin=True)
            agent = Agent(name="Agent", slug=f"agent-{uuid.uuid4().hex[:8]}", user_id=admin.id)
            evaluation = Evaluation(user_id=admin.id, name="E")
            db.add_all([agent, evaluation])
            await db.flush()
            run = EvaluationRun(evaluation_id=evaluation.id, agent_id=agent.id, model_id="m", trigger_reason="manual")
            db.add(run)
            await db.commit()
            run_id = run.id

        async with scratch.sessions() as db:
            stored = await db.get(EvaluationRun, run_id)

        assert (
            stored.arm, stored.trial_index, stored.placebo_target, stored.placebo_snapshot, stored.placebo_comparison_id
        ) == ("real", 0, None, None, None)


async def test_removing_the_evaluation_removes_its_comparisons_and_their_runs():
    async with _scratch("placebo_schema_cascade") as scratch:
        async with scratch.sessions() as db:
            admin = await _user(db, admin=True)
            agent = Agent(name="Agent", slug=f"agent-{uuid.uuid4().hex[:8]}", user_id=admin.id)
            kept, removed = Evaluation(user_id=admin.id, name="Kept"), Evaluation(user_id=admin.id, name="Removed")
            db.add_all([agent, kept, removed])
            await db.flush()
            comparisons = [
                EvaluationPlaceboComparison(evaluation_id=evaluation.id, seed=f"seed-{index}", created_by=admin.id)
                for index, evaluation in enumerate((kept, removed))
            ]
            db.add_all(comparisons)
            await db.flush()
            db.add_all(
                EvaluationRun(
                    evaluation_id=comparison.evaluation_id, agent_id=agent.id, model_id="m", trigger_reason="manual",
                    arm="placebo", trial_index=2, placebo_target={"kind": "persona", "skill_id": None},
                    placebo_comparison_id=comparison.id,
                )
                for comparison in comparisons
            )
            await db.commit()
            kept_id, removed_id = kept.id, removed.id

        async with scratch.sessions() as db:
            await db.delete(await db.get(Evaluation, removed_id))
            await db.commit()

        async with scratch.sessions() as db:
            left = (await db.execute(select(EvaluationPlaceboComparison.evaluation_id))).scalars().all()
            runs = (await db.execute(select(EvaluationRun.evaluation_id, EvaluationRun.arm, EvaluationRun.trial_index))).all()

        assert left == [kept_id]
        assert [tuple(run) for run in runs] == [(kept_id, "placebo", 2)]


async def test_an_evaluation_with_an_agent_exports_and_imports_back():
    # With its agent re-imported, the placebo configuration comes back whole:
    # a persona target names no instance-specific id.
    async with _scratch("placebo_cfg_export_agent") as scratch:
        async with scratch.sessions() as db:
            admin = await _user(db, admin=True)
            agent = Agent(name="Analyst", slug=f"analyst-{uuid.uuid4().hex[:8]}", user_id=admin.id, persona="You are an analyst.")
            db.add(agent)
            await db.commit()
            agent_id = str(agent.id)
        async with _client(scratch.sessions, admin) as client:
            created = (
                await client.post(
                    "/api/v1/evaluations",
                    json={
                        "name": "E",
                        "execution_mode": "full_loop",
                        "agent_ids": [agent_id],
                        "placebo_config": {"targets": [{"kind": "persona"}]},
                    },
                )
            ).json()
            exported = await client.get(f"/api/v1/evaluations/{created['id']}/export")
            imported = await client.post(
                "/api/v1/evaluations/import",
                files={"file": ("evaluation.zip", exported.content, "application/zip")},
            )

        assert exported.status_code == 200, exported.text
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            definition = json.loads(archive.read("evaluation.json"))
        assert [dep["index"] for dep in definition["dependencies"]["agents"]] == [0]
        assert imported.status_code == 201, imported.text
        assert len(imported.json()["agent_ids"]) == 1
        assert imported.json()["agent_ids"] != [agent_id]
        assert imported.json()["placebo_config"]["targets"] == [{"kind": "persona", "skill_id": None}]


async def test_an_import_drops_the_skill_target_before_checking_the_mode():
    # In llm_only only the persona can be replaced: once the skill target is
    # dropped, what is left is a valid configuration, so the package imports.
    async with _scratch("placebo_cfg_import_drop_first") as scratch:
        async with scratch.sessions() as db:
            admin = await _user(db, admin=True)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "evaluation.json",
                json.dumps(
                    {
                        "version": 1,
                        "export_type": "evaluation_package",
                        "evaluation": {
                            "name": "Imported",
                            "execution_mode": "llm_only",
                            "placebo_config": {"targets": [{"kind": "persona"}, {"kind": "skill", "skill_id": SKILL_ID}]},
                        },
                    }
                ),
            )
        async with _client(scratch.sessions, admin) as client:
            response = await client.post(
                "/api/v1/evaluations/import",
                files={"file": ("evaluation.zip", buffer.getvalue(), "application/zip")},
            )

        assert response.status_code == 201, response.text
        assert response.json()["placebo_config"]["targets"] == [{"kind": "persona", "skill_id": None}]
        assert response.json()["warnings"] == [
            f"Placebo target skill:{SKILL_ID} was dropped: it names a skill of the exporting instance"
        ]
