"""An evaluation run measures the model it names: no call goes to another model.

On a staging instance a gateway denied the evaluated model on every call (403
``policy_denied``); the failover wrapper handed each call to another provider's
model, and the run ended ``completed`` with that model's answers scored as the
named model's. A Full Loop evaluation turn now resolves its model with no
failover chain (``get_llm_for_user(..., failover=False)``) and no budget
downgrade: the model that fails fails the entry, and a run whose every entry
failed that way ends ``failed``.

The run goes through the real ``execute_evaluation_run``, ``run_agent_chat``,
router and agent loop on PostgreSQL; only the provider instances (spies built
in place of ``build_provider``), the judge, Redis and the turn's tool and memory
loaders are stubbed.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.llm import router
from app.llm.base import BaseLLMProvider, ChatResponse
from app.models.agent import Agent
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.evaluation import Evaluation, EvaluationResult, EvaluationRun
from app.models.user import User
from app.services import evaluation_engine as ee
from app.services.evaluation.framing import ANOMALY_KIND_PLATFORM_ERROR
from tests.evaluation_helpers import seed_provider
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_EVALUATED = "claude-sonnet-4-6"
_OTHER = "gpt-4o"
_ENTRIES = 3


class _PolicyDeniedError(Exception):
    """What the gateway raised: a 403 naming the issuer's policy."""

    status_code = 403

    def __init__(self, model: str | None) -> None:
        super().__init__(f"Error code: 403 - model '{model}' is not allowed by this issuer's policy (policy_denied)")


class _SpyProvider(BaseLLMProvider):
    """One provider config's instance: records the model each call runs on, denies or answers.

    A call that names no model runs on the provider's default one, as a real provider does.
    """

    def __init__(self, config, *, denies: bool) -> None:
        self.provider_name = config.provider
        self.provider_config_id = str(config.id)
        self.default_model = (config.available_models or [None])[0]
        self.denies = denies
        self.models: list[str] = []

    async def chat(self, messages, model=None, **kwargs):
        model = model or self.default_model
        self.models.append(model)
        if self.denies:
            raise _PolicyDeniedError(model)
        return ChatResponse(content="An answer.", model=model, usage={})

    async def chat_stream(self, messages, model=None, **kwargs):
        model = model or self.default_model
        self.models.append(model)
        if self.denies:
            raise _PolicyDeniedError(model)
        yield "An answer."

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return [self.default_model]


class _FakeRedis:
    async def incr(self, key):
        return 1

    async def decr(self, key):
        return 0

    async def expire(self, key, ttl):
        return True

    async def delete(self, key):
        return 1

    async def get(self, key):
        return None

    async def set(self, *args, **kwargs):
        return True


def _stub_around_the_turn(monkeypatch) -> None:
    """Everything around the turn that needs Redis, a tool server or a memory store."""
    for target, value in {
        "app.core.redis.redis_client": AsyncMock(return_value=_FakeRedis()),
        "app.services.variable_resolver.load_variables": AsyncMock(return_value={}),
        "app.services.agent_chat.enforce_agentic_quota": AsyncMock(return_value=None),
        "app.services.builtin_tools.load_custom_and_mcp_tools": AsyncMock(return_value=None),
        "app.services.builtin_tools.build_kb_search_tool": MagicMock(return_value=None),
        "app.services.integration_tools.build_integration_tools": AsyncMock(return_value=([], None)),
        "app.services.integration_tools.build_channel_tools": AsyncMock(return_value=([], None)),
        "app.services.agent_memory.load_memory": AsyncMock(return_value={}),
        "app.services.user_memory.load_user_memory": AsyncMock(return_value={}),
        "app.services.persistent_memory.load_persistent_context": AsyncMock(return_value=""),
        # Opens its own session on the app engine, outside the scratch database.
        "app.services.builtin_tools.memory._resolve_provider_model": AsyncMock(return_value=None),
    }.items():
        monkeypatch.setattr(target, value)

    async def _judge(*_args, **_kwargs):
        return {
            "score": 80.0,
            "sub_scores": {k: 80 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "ok",
            "is_judge_failure": False,
        }

    async def _no_price_rows(_db, _model_id):
        return []

    monkeypatch.setattr(ee, "score_result", _judge)
    monkeypatch.setattr(ee, "_build_run_price_rows", _no_price_rows)


async def _run_on_a_denied_model(monkeypatch) -> tuple[EvaluationRun, list[EvaluationResult], dict[str, _SpyProvider]]:
    """One Full Loop run on a model its gateway denies, beside a provider that would answer."""
    spies: dict[str, _SpyProvider] = {}

    def _build(config, *, api_key=None):
        spy = _SpyProvider(config, denies=config.provider == "filigran_gateway")
        spies[config.provider] = spy
        return spy

    monkeypatch.setattr(router, "build_provider", _build)
    _stub_around_the_turn(monkeypatch)
    router.invalidate_provider_cache()

    async with scratch_database("evalpin", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as s:
            owner = User(id=uuid.uuid4(), email="owner@example.com", full_name="Owner", hashed_password="x", role="admin")
            s.add(owner)
            await s.flush()
            await seed_provider(s, [_EVALUATED], provider="filigran_gateway")
            await seed_provider(s, [_OTHER], provider="openai")
            agent = Agent(
                name="STIX Extractor",
                slug=f"stix-{uuid.uuid4().hex[:6]}",
                persona="You extract STIX objects.",
                user_id=owner.id,
            )
            evaluation = Evaluation(
                name="STIX Extraction", scorer_selection=["judge"], execution_mode="full_loop", user_id=owner.id
            )
            dataset = Dataset(name="ds")
            s.add_all([agent, evaluation, dataset])
            await s.flush()
            s.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=dataset.id))
            s.add_all(
                DatasetEntry(dataset_id=dataset.id, position=i, input_prompt=f"Report {i}", expected_answer="a")
                for i in range(_ENTRIES)
            )
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id=_EVALUATED,
                trigger_reason="manual",
                status="pending",
            )
            s.add(run)
            await s.commit()
            run_id = run.id

        try:
            async with db.sessions() as s:
                await ee.execute_evaluation_run(s, str(run_id))
        finally:
            router.invalidate_provider_cache()

        async with db.sessions() as s:
            stored = await s.get(EvaluationRun, run_id)
            results = list(
                (await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))).scalars()
            )
    return stored, results, spies


async def test_a_denied_model_is_never_answered_by_another_model(monkeypatch):
    run, results, spies = await _run_on_a_denied_model(monkeypatch)

    gateway = spies["filigran_gateway"]
    # Every call of the run went to the model it names, on its own provider...
    assert gateway.models == [_EVALUATED] * _ENTRIES
    # ...and none to the provider that would have answered in its place.
    assert sorted(spies) == ["filigran_gateway"]

    # The run reports what happened: no entry measured, never ``completed``.
    assert run.status == "failed"
    assert run.error == ee.NOTHING_COUNTED_ERROR
    assert len(results) == _ENTRIES
    assert [r.execution_anomaly_kind for r in results] == [ANOMALY_KIND_PLATFORM_ERROR] * _ENTRIES
    assert [r.agent_output.startswith("Error: Error code: 403") for r in results] == [True] * _ENTRIES


async def test_the_same_turn_outside_evaluation_still_fails_over(monkeypatch):
    """The pin is the evaluation's: a chat turn on the same model keeps its failover."""
    from app.services import agent_chat

    spies: dict[str, _SpyProvider] = {}

    def _build(config, *, api_key=None):
        spy = _SpyProvider(config, denies=config.provider == "filigran_gateway")
        spies[config.provider] = spy
        return spy

    monkeypatch.setattr(router, "build_provider", _build)
    _stub_around_the_turn(monkeypatch)
    router.invalidate_provider_cache()

    async with (
        scratch_database("evalpin", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        owner = User(id=uuid.uuid4(), email="owner@example.com", full_name="Owner", hashed_password="x", role="admin")
        s.add(owner)
        await s.flush()
        await seed_provider(s, [_EVALUATED], provider="filigran_gateway")
        await seed_provider(s, [_OTHER], provider="openai")
        agent = Agent(
            name="STIX Extractor",
            slug=f"stix-{uuid.uuid4().hex[:6]}",
            persona="You extract STIX objects.",
            user_id=owner.id,
            llm_model=_EVALUATED,
        )
        s.add(agent)
        await s.commit()
        try:
            output, _ = await agent_chat.run_agent_chat(
                s, str(owner.id), agent, [], "Report 0", meter_quota=False, on_event=AsyncMock()
            )
        finally:
            router.invalidate_provider_cache()

    assert set(spies["filigran_gateway"].models) == {_EVALUATED}
    assert spies["openai"].models == [_OTHER]
    assert output == "An answer."

