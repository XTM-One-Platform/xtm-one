"""Evaluation stays behind ``EVALUATION`` and a mode where AI runs.

On in both products (XTM One and Copilot, dev-docs/mode-split.md S3), never in
``ai_disabled``. Off - the default - its API answers 403 naming the
flag, its routes are not published, an agent is never handed its tools, Admin
Objects does not offer its types, and the adapter starts no new work. What it
stored is kept. See ``dev-docs/evaluation-feature-flag.md``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.main import application  # imported here: which routes are published is decided at import
from app.services.evaluation.availability import DISABLED_DETAIL, evaluation_enabled

FLAG = "EVALUATION"
EVALUATION_TOOLS = {
    "list_evaluations",
    "get_evaluation",
    "list_evaluation_runs",
    "get_evaluation_run_values",
    "trigger_evaluation",
    "get_placebo_comparison",
    "compare_evaluation_runs",
}


@pytest.fixture
def licensed(monkeypatch):
    """A valid licence, as a copilot or dev deployment has: ``LicenseGateMiddleware``
    refuses every API request without one, before the routes."""
    import time

    from app.main import LicenseGateMiddleware

    monkeypatch.setattr(LicenseGateMiddleware, "_cache_valid", True)
    monkeypatch.setattr(LicenseGateMiddleware, "_cache_ts", time.monotonic())


@pytest.fixture
def evaluation_disabled(monkeypatch):
    """Copilot mode without the flag: off whatever ``.env`` sets."""
    monkeypatch.setattr(settings, "enabled_features", "")
    monkeypatch.setattr(settings, "platform_mode", "copilot")


@pytest.mark.parametrize(
    ("mode", "features", "expected"),
    [
        ("copilot", "EVALUATION", True),
        ("dev", "EVALUATION", True),
        ("copilot", "*", True),
        ("copilot", "", False),
        ("dev", "VIDEO_STUDIO", False),
        # A generic AI capability: XTM One mode runs it too, with the flag.
        ("xtm_one", "EVALUATION", True),
        ("xtm_one", "*", True),
        ("xtm_one", "", False),
        # The mode where no model call happens never does.
        ("ai_disabled", "EVALUATION", False),
        ("ai_disabled", "*", False),
    ],
)
def test_evaluation_is_on_in_every_ai_mode_with_the_flag(monkeypatch, mode, features, expected):
    monkeypatch.setattr(settings, "platform_mode", mode)
    monkeypatch.setattr(settings, "enabled_features", features)
    assert evaluation_enabled() is expected


PERSONA_REWRITE_PATH = "/api/v1/improvement-proposals/persona"

_GATED_REQUESTS = [
    ("GET", "/api/v1/evaluations"),
    ("GET", "/api/v1/datasets"),
    # A person's persona rewrite (#1044); an agent's proposals are Platform Health's.
    ("POST", "/api/v1/improvement-proposals/persona"),
    # The signed callback answers no user, and is refused all the same.
    ("POST", "/api/v1/evaluations/resume"),
]


async def _answers(client: httpx.AsyncClient) -> dict[str, httpx.Response]:
    return {path: await client.request(method, path) for method, path in _GATED_REQUESTS}


@pytest.mark.usefixtures("licensed")
@pytest.mark.parametrize(("mode", "features"), [("copilot", ""), ("xtm_one", "")])
async def test_the_api_answers_403_naming_the_flag_before_authentication(monkeypatch, mode, features):
    monkeypatch.setattr(settings, "platform_mode", mode)
    monkeypatch.setattr(settings, "enabled_features", features)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        answers = await _answers(client)

    assert {path: r.status_code for path, r in answers.items()} == {path: 403 for _m, path in _GATED_REQUESTS}
    assert {r.json()["detail"] for r in answers.values()} == {DISABLED_DETAIL}


@pytest.mark.usefixtures("evaluation_enabled", "licensed")
async def test_with_evaluation_on_the_api_asks_who_is_calling():
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        answers = await _answers(client)

    # Past the gate: the user routes want a user; the callback, a signature over a body.
    user_routes = {path: r.status_code for path, r in answers.items() if not path.endswith("/resume")}
    assert user_routes == dict.fromkeys(user_routes, 401)
    assert answers["/api/v1/evaluations/resume"].status_code not in (401, 403)


@pytest.mark.skipif(evaluation_enabled(), reason="the schema is built at import; this asserts the flag-off one")
def test_the_routes_are_not_published_while_off():
    published = [
        path
        for path in application.openapi()["paths"]
        if path.startswith(("/api/v1/evaluations", "/api/v1/datasets")) or path == PERSONA_REWRITE_PATH
    ]
    assert published == []


def _built_tools(**kwargs) -> set[str]:
    from app.services.builtin_tools import build_evaluation_tools

    definitions, _executor = build_evaluation_tools(None, "", **kwargs)  # type: ignore[arg-type]
    return {d.name for d in definitions}


@pytest.mark.usefixtures("evaluation_disabled")
def test_off_an_agent_is_never_handed_the_evaluation_tools():
    """Neither offered to the model nor callable by name."""
    from app.services.builtin_tools import build_evaluation_tools

    definitions, executor = build_evaluation_tools(None, str(uuid.uuid4()))  # type: ignore[arg-type]

    # The ratings of the agent's work are not evaluations: that tool stays.
    assert {d.name for d in definitions} == {"get_agent_feedback"}
    assert executor.tool_names == ["get_agent_feedback"]


@pytest.mark.usefixtures("evaluation_disabled")
def test_off_the_registry_still_lists_the_evaluation_tools():
    """Skill packages and the refusal classifier read the registry: an id it drops is rejected."""
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    assert _built_tools(include_withheld=True) == EVALUATION_TOOLS | {"get_agent_feedback"}
    registry = {d["name"] for d in build_builtin_tool_catalog()["platform"]}
    assert registry >= EVALUATION_TOOLS


@pytest.mark.usefixtures("evaluation_enabled")
def test_on_an_agent_gets_every_evaluation_tool():
    assert _built_tools() == EVALUATION_TOOLS | {"get_agent_feedback"}


async def test_the_offered_catalog_follows_the_gate(monkeypatch):
    from app.api.v1.settings import get_builtin_tool_catalog

    monkeypatch.setattr(settings, "platform_mode", "copilot")
    monkeypatch.setattr(settings, "enabled_features", "")
    # ``agent_builder`` is an alias of the platform group.
    off = {d["name"] for d in (await get_builtin_tool_catalog(_user=None))["platform"]}
    monkeypatch.setattr(settings, "enabled_features", FLAG)
    on = {d["name"] for d in (await get_builtin_tool_catalog(_user=None))["platform"]}

    assert not EVALUATION_TOOLS & off
    assert on >= EVALUATION_TOOLS
    assert "get_agent_feedback" in off


def test_admin_objects_offers_the_evaluation_types_only_while_on(monkeypatch):
    from app.api.v1.admin_objects import active_entity_arms

    monkeypatch.setattr(settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(settings, "enabled_features", "")
    assert not {"dataset", "evaluation"} & set(active_entity_arms())
    monkeypatch.setattr(settings, "enabled_features", FLAG)
    assert set(active_entity_arms()) >= {"dataset", "evaluation"}
    monkeypatch.setattr(settings, "platform_mode", "ai_disabled")
    assert not {"dataset", "evaluation"} & set(active_entity_arms())
    monkeypatch.setattr(settings, "platform_mode", "dev")
    assert set(active_entity_arms()) >= {"dataset", "evaluation"}
    monkeypatch.setattr(settings, "enabled_features", "")
    assert not {"dataset", "evaluation"} & set(active_entity_arms())


# ── The adapter starts no new work ──
# Off, an execution queued before is refused: a fresh evaluation fails its pending
# runs with the reason, a rescore hands its runs back as they were.


async def _execute(db, payload: dict, execution_id: uuid.UUID):
    from app.services.orchestration_adapters import AdapterContext
    from app.services.orchestration_adapters.evaluation import EvaluationAdapter

    # What the adapter reads of a queued execution.
    queued = SimpleNamespace(id=execution_id, payload=payload, adapter_state=None)
    async with db.sessions() as session:
        return await EvaluationAdapter().execute(AdapterContext(db=session, worker_id="w1"), queued)  # type: ignore[arg-type]


@pytest.mark.integration
@pytest.mark.usefixtures("evaluation_disabled")
async def test_off_queued_work_is_refused_and_its_runs_say_why():
    import app.models  # noqa: F401 - register every mapper on Base.metadata
    from app.models.agent import Agent
    from app.models.evaluation import Evaluation, EvaluationRun
    from tests.scratch_database import ALL_TABLES, scratch_database

    fresh_execution, rescore_execution = uuid.uuid4(), uuid.uuid4()
    async with scratch_database("evaluationflag", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as s:
            agent, evaluation = Agent(name="Agent", slug=f"a-{uuid.uuid4().hex[:6]}"), Evaluation(name="Eval")
            s.add_all([agent, evaluation])
            await s.flush()

            def _run(status: str, owner: uuid.UUID) -> EvaluationRun:
                return EvaluationRun(
                    evaluation_id=evaluation.id,
                    agent_id=agent.id,
                    model_id="m",
                    trigger_reason="manual",
                    status=status,
                    owner_execution_id=owner,
                )

            pending, rescored = _run("pending", fresh_execution), _run("running", rescore_execution)
            s.add_all([pending, rescored])
            await s.commit()
            evaluation_id, pending_id, rescored_id = evaluation.id, pending.id, rescored.id

        fresh = await _execute(db, {"evaluation_id": str(evaluation_id), "trigger_reason": "schedule"}, fresh_execution)
        rescore = await _execute(
            db,
            {
                "kind": "rescore_runs",
                "run_ids": [str(rescored_id)],
                "previous_statuses": {str(rescored_id): "completed"},
            },
            rescore_execution,
        )

        async with db.sessions() as s:
            runs = {
                run.id: run
                for run in (
                    await s.execute(sa.select(EvaluationRun).where(EvaluationRun.id.in_([pending_id, rescored_id])))
                ).scalars()
            }

    assert (fresh.kind, fresh.error, fresh.retryable) == ("failed", DISABLED_DETAIL, False)
    assert (rescore.kind, rescore.error, rescore.retryable) == ("failed", DISABLED_DETAIL, False)
    assert (runs[pending_id].status, runs[pending_id].error) == ("failed", DISABLED_DETAIL)
    assert (runs[rescored_id].status, runs[rescored_id].rescore_error) == ("completed", DISABLED_DETAIL)


async def test_off_the_benchmark_is_left_as_it_is_rather_than_synced_away(monkeypatch):
    """Both seeds are full syncs: run with nothing declared they would delete the
    built-in benchmark and its runs, so while off they do not run at all."""
    from app.evaluations import seed
    from app.services import provisioning

    synced: list[str] = []

    async def _sync(name: str) -> int:
        synced.append(name)
        return 0

    monkeypatch.setattr(seed, "seed_builtin_datasets", lambda _db, platform_mode: _sync("datasets"))
    monkeypatch.setattr(seed, "seed_builtin_evaluations", lambda _db, platform_mode: _sync("evaluations"))
    monkeypatch.setattr(settings, "platform_mode", "copilot")

    monkeypatch.setattr(settings, "enabled_features", "")
    await provisioning.seed_builtin_evaluation_content("copilot")
    assert synced == []

    monkeypatch.setattr(settings, "enabled_features", FLAG)
    await provisioning.seed_builtin_evaluation_content("copilot")
    assert synced == ["datasets", "evaluations"]


async def test_off_no_scheduled_evaluation_is_triggered(monkeypatch):
    from app.services import autonomous

    triggered: list[object] = []

    async def _process(db) -> int:
        triggered.append(db)
        return 1

    monkeypatch.setattr(autonomous, "_process_scheduled_evaluations", _process)
    monkeypatch.setattr(settings, "platform_mode", "copilot")

    monkeypatch.setattr(settings, "enabled_features", "")
    assert await autonomous._run_scheduled_evaluations("db", ai_on=True) == 0
    monkeypatch.setattr(settings, "enabled_features", FLAG)
    assert await autonomous._run_scheduled_evaluations("db", ai_on=False) == 0
    assert triggered == []
    assert await autonomous._run_scheduled_evaluations("db", ai_on=True) == 1
    assert triggered == ["db"]


@pytest.mark.integration
async def test_off_an_agent_change_starts_no_evaluation(monkeypatch):
    import app.models  # noqa: F401 - register every mapper on Base.metadata
    from app.api.v1.agents import _trigger_evaluations_on_agent_change
    from app.models.agent import Agent
    from app.models.evaluation import Evaluation, EvaluationAgent
    from app.services import evaluation_engine
    from tests.scratch_database import ALL_TABLES, scratch_database

    started: list[str] = []

    async def _enqueue(evaluation_id: str, trigger_reason: str) -> None:
        started.append(evaluation_id)

    monkeypatch.setattr(evaluation_engine, "enqueue_evaluation", _enqueue)
    monkeypatch.setattr(settings, "platform_mode", "copilot")
    async with scratch_database("evaluationflagagent", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as s:
            agent = Agent(name="Agent", slug=f"a-{uuid.uuid4().hex[:6]}")
            evaluation = Evaluation(name="Eval", trigger_config={"on_agent_change": True})
            s.add_all([agent, evaluation])
            await s.flush()
            s.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
            await s.commit()
            agent_id, evaluation_id = agent.id, evaluation.id

        async with db.sessions() as s:
            monkeypatch.setattr(settings, "enabled_features", "")
            await _trigger_evaluations_on_agent_change(s, agent_id)
            off = list(started)
            monkeypatch.setattr(settings, "enabled_features", FLAG)
            await _trigger_evaluations_on_agent_change(s, agent_id)

    assert off == []
    assert started == [str(evaluation_id)]


@pytest.mark.integration
@pytest.mark.parametrize(("mode", "served"), [("copilot", True), ("dev", True), ("xtm_one", True)])
async def test_the_frontend_is_served_the_flag_only_where_evaluation_runs(monkeypatch, mode, served):
    """One rule, the backend's: the frontend reads the plain flag from ``/auth/config``."""
    from tests.scratch_database import ALL_TABLES, scratch_database

    monkeypatch.setattr(settings, "platform_mode", mode)
    monkeypatch.setattr(settings, "enabled_features", "EVALUATION,VIDEO_STUDIO")
    async with scratch_database("evaluationflagauth", tables=ALL_TABLES, extensions=("vector",)):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            features = (await client.get("/api/v1/auth/config")).json()["enabled_features"]

    assert ("EVALUATION" in features) is served
    assert "VIDEO_STUDIO" in features


@pytest.mark.integration
@pytest.mark.usefixtures("evaluation_disabled")
async def test_off_the_prompt_guard_scope_still_lists_the_evaluation_tools():
    """They never run while off; an entry that read as stale would be dropped from the scope."""
    from app.api.v1.settings import _scannable_tool_groups
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with (
        scratch_database("evaluationflagscope", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        groups = await _scannable_tool_groups(s)

    builtin = next(g for g in groups if g.key == "builtin")
    assert {t.name for t in builtin.tools} >= EVALUATION_TOOLS


def test_with_evaluation_on_the_routes_are_published():
    """The schema is built at import, so a process that starts with evaluation on publishes them."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    probe = (
        "from app.main import application; "
        "print(sorted({p.split('/')[3] for p in application.openapi()['paths'] "
        f"if p.startswith(('/api/v1/evaluations', '/api/v1/datasets')) or p == {PERSONA_REWRITE_PATH!r}}}))"
    )
    done = subprocess.run(  # noqa: S603 - this interpreter, a fixed probe
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PLATFORM_MODE": "dev", "ENABLED_FEATURES": "EVALUATION"},
        timeout=300,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "['datasets', 'evaluations', 'improvement-proposals']"


@pytest.mark.parametrize(
    ("mode", "served"), [("copilot", True), ("dev", True), ("xtm_one", True), ("ai_disabled", False)]
)
def test_the_flags_in_effect_hold_evaluation_only_where_it_runs(monkeypatch, mode, served):
    """What ``/auth/config`` serves and the startup logs print: ``*`` in ``ai_disabled`` mode is not evaluation."""
    from app.core.feature_flags import is_feature_enabled, resolve_enabled_feature_flags

    monkeypatch.setattr(settings, "platform_mode", mode)
    monkeypatch.setattr(settings, "enabled_features", "*")
    assert ("EVALUATION" in resolve_enabled_feature_flags()) is served
    assert is_feature_enabled("EVALUATION") is served
    assert "VIDEO_STUDIO" in resolve_enabled_feature_flags()
