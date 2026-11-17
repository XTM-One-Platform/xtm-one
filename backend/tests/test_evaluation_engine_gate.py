"""The evaluation engine gate opens to the Concierge, the surfaces do not (#8257, decision B9).

``engine_accepts`` holds where the surface gate (``evaluation_enabled``) does,
and also for an evaluation the Concierge built (``concierge_built``, read on the
row) while ``CONCIERGE`` is on, whatever the mode. The surface gate is
unchanged: ``test_evaluation_feature_flag.py`` pins it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.services.evaluation import availability
from app.services.evaluation.availability import (
    DISABLED_DETAIL,
    engine_accepts,
    engine_accepts_runs,
    evaluation_enabled,
)


@pytest.fixture
def concierge(monkeypatch):
    """The Concierge on or off (flag and Settings), as the gate reads it."""
    state = SimpleNamespace(enabled=True)

    async def _active(_db) -> bool:
        return state.enabled

    monkeypatch.setattr(availability, "concierge_active", _active)
    return state


def _mode(monkeypatch, mode: str, features: str) -> None:
    monkeypatch.setattr(settings, "platform_mode", mode)
    monkeypatch.setattr(settings, "enabled_features", features)


class _Db:
    """What the gate reads: the evaluation's mark."""

    def __init__(self, built):
        self.built = built
        self.reads = 0

    async def scalar(self, _statement):
        self.reads += 1
        return self.built


@pytest.mark.parametrize(
    ("mode", "features", "concierge_on", "built", "expected"),
    [
        ("copilot", "EVALUATION", False, False, True),
        ("dev", "EVALUATION", True, False, True),
        ("xtm_one", "", True, True, True),
        ("xtm_one", "", False, True, False),
        ("xtm_one", "EVALUATION", False, False, True),
        ("xtm_one", "", True, False, False),
        ("xtm_one", "", True, None, False),
        ("copilot", "", True, True, True),
        ("copilot", "", False, True, False),
    ],
    ids=[
        "surface-on-human",
        "surface-on-flag-on-human",
        "xtm-one-concierge",
        "xtm-one-concierge-flag-off",
        "xtm-one-surface-on-human",
        "xtm-one-human",
        "xtm-one-missing",
        "copilot-flag-off-concierge",
        "copilot-both-off-concierge",
    ],
)
async def test_the_engine_gate_table(monkeypatch, concierge, mode, features, concierge_on, built, expected):
    _mode(monkeypatch, mode, features)
    concierge.enabled = concierge_on

    assert await engine_accepts(_Db(built), uuid.uuid4()) is expected  # type: ignore[arg-type]


async def test_the_surface_gate_never_follows_the_concierge(monkeypatch, concierge):
    _mode(monkeypatch, "xtm_one", "")

    assert evaluation_enabled() is False
    assert await engine_accepts(_Db(True), uuid.uuid4()) is True  # type: ignore[arg-type]
    assert evaluation_enabled() is False


@pytest.mark.parametrize("concierge_on", [True, False], ids=["flag-on", "flag-off"])
async def test_with_the_surface_on_the_row_is_not_read(monkeypatch, concierge, concierge_on):
    _mode(monkeypatch, "copilot", "EVALUATION")
    concierge.enabled = concierge_on
    db = _Db(False)

    assert await engine_accepts(db, uuid.uuid4()) is True  # type: ignore[arg-type]
    assert db.reads == 0


async def test_with_the_concierge_off_the_row_is_not_read(monkeypatch, concierge):
    _mode(monkeypatch, "xtm_one", "")
    concierge.enabled = False
    db = _Db(True)

    assert await engine_accepts(db, uuid.uuid4()) is False  # type: ignore[arg-type]
    assert await engine_accepts_runs(db, [uuid.uuid4()]) is False  # type: ignore[arg-type]
    assert db.reads == 0


async def test_a_malformed_id_is_refused_without_a_read(monkeypatch, concierge):
    _mode(monkeypatch, "xtm_one", "")
    db = _Db(True)

    assert await engine_accepts(db, "not-a-uuid") is False  # type: ignore[arg-type]
    assert await engine_accepts_runs(db, ["not-a-uuid"]) is False  # type: ignore[arg-type]
    assert db.reads == 0


def test_no_copy_export_or_import_carries_the_mark():
    from app.services.evaluation.settings import EVALUATION_NOT_SETTINGS, EVALUATION_SETTINGS

    assert "concierge_built" in EVALUATION_NOT_SETTINGS
    assert "concierge_built" not in EVALUATION_SETTINGS


async def _execute(db, payload: dict, execution_id: uuid.UUID):
    from app.services.orchestration_adapters import AdapterContext
    from app.services.orchestration_adapters.evaluation import EvaluationAdapter

    queued = SimpleNamespace(id=execution_id, payload=payload, adapter_state=None)
    async with db.sessions() as session:
        return await EvaluationAdapter().execute(AdapterContext(db=session, worker_id="w1"), queued)  # type: ignore[arg-type]


@pytest.mark.integration
async def test_in_xtm_one_mode_the_adapter_runs_a_concierge_launch_and_refuses_a_human_one(monkeypatch, concierge):
    import app.models  # noqa: F401 - register every mapper on Base.metadata
    from app.models.agent import Agent
    from app.models.evaluation import Evaluation, EvaluationRun
    from app.services import agentic_gate, evaluation_engine
    from tests.scratch_database import ALL_TABLES, scratch_database

    # The surface off (no EVALUATION): only the Concierge's own launch runs.
    _mode(monkeypatch, "xtm_one", "")
    started: list[str] = []

    async def _execute_evaluation(db, evaluation_id, trigger_reason, execution_id, **_kwargs):
        started.append(str(evaluation_id))

    async def _verdict(_db):
        return SimpleNamespace(transient=False, refusal=None)

    monkeypatch.setattr(evaluation_engine, "execute_evaluation", _execute_evaluation)
    monkeypatch.setattr(agentic_gate, "agentic_verdict", _verdict)

    concierge_execution, human_execution = uuid.uuid4(), uuid.uuid4()
    async with scratch_database("evaluationenginegate", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as s:
            agent = Agent(name="Agent", slug=f"a-{uuid.uuid4().hex[:6]}")
            by_concierge = Evaluation(name="Concierge", concierge_built=True)
            by_human = Evaluation(name="Human")
            s.add_all([agent, by_concierge, by_human])
            await s.flush()
            human_run = EvaluationRun(
                evaluation_id=by_human.id,
                agent_id=agent.id,
                model_id="m",
                trigger_reason="manual",
                status="pending",
                owner_execution_id=human_execution,
            )
            s.add(human_run)
            await s.commit()
            concierge_id, human_id, human_run_id = by_concierge.id, by_human.id, human_run.id

        ran = await _execute(db, {"evaluation_id": str(concierge_id)}, concierge_execution)
        refused = await _execute(db, {"evaluation_id": str(human_id)}, human_execution)

        async with db.sessions() as s:
            run = (await s.execute(sa.select(EvaluationRun).where(EvaluationRun.id == human_run_id))).scalar_one()

    assert ran.kind == "completed"
    assert started == [str(concierge_id)]
    assert (refused.kind, refused.error, refused.retryable) == ("failed", DISABLED_DETAIL, False)
    assert (run.status, run.error) == ("failed", DISABLED_DETAIL)


def test_the_adapter_reads_the_engine_gate_not_the_surface_gate():
    from app.services.orchestration_adapters import evaluation as adapter

    assert adapter.engine_accepts is availability.engine_accepts
    assert adapter.engine_accepts_runs is availability.engine_accepts_runs
    assert not hasattr(adapter, "evaluation_enabled")
