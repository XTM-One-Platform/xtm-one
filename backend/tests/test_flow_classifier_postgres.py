"""The Classify (CLM) node walked against a real PostgreSQL.

What a fake session would fake: the branch the walker takes on a decision, the
step journal, the error path, the provider a node resolves to among real
config rows (pinned, unpinned, restricted, granted through a group), and the
built-in TheHive triage example run verbatim.

Only the provider's network call is replaced (``decide_choice`` on the
provider class): resolution, the provider cache, the walker and the journal
are the real ones.
"""

import json
import uuid

import httpx
import openai
import pytest
from sqlalchemy import select

from app.core.security import encrypt_value
from app.llm.custom_openai_provider import CustomOpenAIProvider
from app.llm.decisions import ChoiceDecision, DecisionResponseError
from app.llm.openai_provider import OpenAIProvider
from app.llm.openrouter_provider import OpenRouterProvider
from app.llm.router import invalidate_provider_cache
from app.models.agent import Agent
from app.models.flow_run import FLOW_RUN_ACTIVE_STATUSES, FlowRun
from app.models.group import Group, GroupMember
from app.models.llm_config import LLMProviderConfig
from app.models.platform_settings import PlatformSettings
from app.services.flow_classifier import (
    AMBIGUOUS_PROVIDER_MESSAGE,
    NO_DECISION_PROVIDER_MESSAGE,
    UNLISTED_MODEL_MESSAGE,
)
from tests.agentic_gate_stub import stub_agentic_gate
from tests.test_flow_runner_postgres import (
    _edge,
    _node,
    _run_to_outcome,
    _seed,
    _steps,
    _temp_database,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    stub_agentic_gate(monkeypatch)


@pytest.fixture(autouse=True)
def _fresh_provider_cache():
    invalidate_provider_cache()
    yield
    invalidate_provider_cache()


class _Decide:
    """Stands in for the decision endpoint of every provider class it patches."""

    def __init__(self, monkeypatch, *classes):
        self.calls = []
        self.decision = ChoiceDecision(choice="yes", confidence=0.9)
        self.error = None
        # What each provider class's decision listing reports; nothing by default.
        self.listed: dict[str, list[str]] = {}
        spy = self

        async def fake_list_decision_models(self):
            return spy.listed.get(type(self).__name__, [])

        async def fake_decision_listing(self):
            return spy.listed.get(type(self).__name__, []), True

        async def fake_decide_choice(self, *, model, state, instructions, choices):
            spy.calls.append(
                {
                    "provider": type(self).__name__,
                    "config_id": self.provider_config_id,
                    "model": model,
                    "state": state,
                    "instructions": instructions,
                    "choices": choices,
                }
            )
            if spy.error is not None:
                raise spy.error
            return spy.decision

        for cls in classes or (OpenRouterProvider, OpenAIProvider):
            monkeypatch.setattr(cls, "decide_choice", fake_decide_choice)
            monkeypatch.setattr(cls, "list_decision_models", fake_list_decision_models)
            monkeypatch.setattr(cls, "decision_listing", fake_decision_listing)


def _provider(provider="openrouter", models=None, **extra):
    return LLMProviderConfig(
        id=uuid.uuid4(),
        provider=provider,
        api_key_enc=encrypt_value("sk-test"),
        available_models=models or [],
        **extra,
    )


def _classify_graph(**config):
    node_config = {
        "model": "typesafe/jev-1.13",
        "input": "{{t1.payload.title}}",
        "question": "Relevant?",
        "cases": {"yes": "Needs an analyst", "no": "Noise"},
        "min_confidence": 0.6,
        **config,
    }
    return {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("c1", "condition.classify", **node_config),
            _node("s_yes", "util.stop", result="yes"),
            _node("s_no", "util.stop", result="no"),
            _node("s_unc", "util.stop", result="uncertain"),
            _node("s_err", "util.stop", result="error"),
        ],
        "edges": [
            _edge("t1", "c1"),
            _edge("c1", "s_yes", sh="case:yes", kind="condition_branch"),
            _edge("c1", "s_no", sh="case:no", kind="condition_branch"),
            _edge("c1", "s_unc", sh="default", kind="condition_branch"),
            _edge("c1", "s_err", sh="error", kind="error"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


async def _walk(session, graph, payload=None):
    user, flow = await _seed(session, graph=graph)
    run, outcome = await _run_to_outcome(
        session, user, flow, payload=payload or {"title": "Mimikatz on DC01"}
    )
    steps = await _steps(session, run)
    return user, run, outcome, steps


# ── Branch selection and journal ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_confident_label_takes_its_branch_and_is_journaled(monkeypatch):
    decide = _Decide(monkeypatch)
    decide.decision = ChoiceDecision(
        choice="yes", confidence=0.9, probabilities={"yes": 0.9, "no": 0.1}
    )
    async with _temp_database() as sessions, sessions() as session:
        config = _provider(models=["typesafe/jev-1.13"])
        session.add(config)
        await session.commit()
        _, _, outcome, steps = await _walk(session, _classify_graph())

        assert outcome.kind == "completed"
        assert [s.node_id for s in steps] == ["t1", "c1", "s_yes"]
        c1 = next(s for s in steps if s.node_id == "c1")
        assert c1.output == {
            "label": "yes",
            "confidence": 0.9,
            "probabilities": {"yes": 0.9, "no": 0.1},
        }
        assert decide.calls == [
            {
                "provider": "OpenRouterProvider",
                "config_id": str(config.id),
                "model": "typesafe/jev-1.13",
                "state": "Mimikatz on DC01",
                "instructions": "Relevant?",
                "choices": {"yes": "Needs an analyst", "no": "Noise"},
            }
        ]


@pytest.mark.asyncio
async def test_a_label_under_the_floor_takes_uncertain(monkeypatch):
    decide = _Decide(monkeypatch)
    decide.decision = ChoiceDecision(choice="no", confidence=0.4)
    async with _temp_database() as sessions, sessions() as session:
        session.add(_provider())
        await session.commit()
        _, _, outcome, steps = await _walk(session, _classify_graph())

        assert outcome.kind == "completed"
        assert [s.node_id for s in steps] == ["t1", "c1", "s_unc"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            DecisionResponseError("The model refused to classify this content."),
            "The model refused to classify this content.",
        ),
        (
            openai.InternalServerError(
                "upstream said: key sk-or-secret is over quota",
                response=httpx.Response(
                    503, request=httpx.Request("POST", "https://openrouter.ai/api/v1/systemone")
                ),
                body=None,
            ),
            "The decision model call failed (HTTP 503).",
        ),
        (
            httpx.ConnectError("connection refused to 10.0.0.1"),
            "The decision model could not be reached.",
        ),
    ],
    ids=["refusal", "http-error", "transport-error"],
)
async def test_no_decision_takes_the_error_path_with_a_generic_message(
    monkeypatch, error, message
):
    decide = _Decide(monkeypatch)
    decide.error = error
    async with _temp_database() as sessions, sessions() as session:
        session.add(_provider())
        await session.commit()
        _, _, outcome, steps = await _walk(session, _classify_graph())

        assert outcome.kind == "completed", "the error edge absorbs the failure"
        assert [s.node_id for s in steps] == ["t1", "c1", "s_err"]
        c1 = next(s for s in steps if s.node_id == "c1")
        assert c1.status == "failed"
        assert c1.error == message


@pytest.mark.asyncio
async def test_without_an_error_route_a_failed_classification_fails_the_run(monkeypatch):
    """Fail closed: content a filter could not classify is never dropped silently."""
    decide = _Decide(monkeypatch)
    decide.error = DecisionResponseError("The model answered with a label that was not offered.")
    graph = _classify_graph()
    graph["edges"] = [e for e in graph["edges"] if e["source_handle"] != "error"]
    async with _temp_database() as sessions, sessions() as session:
        session.add(_provider())
        await session.commit()
        _, run, outcome, steps = await _walk(session, graph)

        assert outcome.kind == "failed"
        assert run.status == "failed"
        assert [s.node_id for s in steps] == ["t1", "c1"]


# ── Provider resolution against real config rows ─────────────────────────────


@pytest.mark.asyncio
async def test_unpinned_prefers_a_provider_that_lists_the_model(monkeypatch):
    decide = _Decide(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        other = _provider(models=["anthropic/claude-sonnet-5"])
        listing = _provider(models=["typesafe/jev-1.13"])
        session.add_all([_provider("anthropic"), other, listing])
        await session.commit()
        await _walk(session, _classify_graph())

        assert [c["config_id"] for c in decide.calls] == [str(listing.id)]


@pytest.mark.asyncio
async def test_unpinned_skips_a_provider_restricted_to_others(monkeypatch):
    decide = _Decide(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        reserved = _provider(
            models=["typesafe/jev-1.13"],
            access_restricted=True,
            shared_user_ids=[str(uuid.uuid4())],
        )
        open_openai = _provider("openai", models=["gpt-6-luna"])
        session.add_all([reserved, open_openai])
        await session.commit()
        await _walk(session, _classify_graph())

        assert [(c["provider"], c["config_id"]) for c in decide.calls] == [
            ("OpenAIProvider", str(open_openai.id))
        ]


@pytest.mark.asyncio
async def test_unpinned_uses_the_only_decision_provider_even_unlisted(monkeypatch):
    decide = _Decide(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        only = _provider(models=["anthropic/claude-sonnet-5"])
        session.add_all([_provider("anthropic"), only])
        await session.commit()
        await _walk(session, _classify_graph())

        assert [c["config_id"] for c in decide.calls] == [str(only.id)]


@pytest.mark.asyncio
async def test_unpinned_skips_a_provider_whose_whole_listing_omits_the_model(monkeypatch):
    """OpenAI as the default must not receive a Jev call its whole listing omits."""
    decide = _Decide(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        openrouter = _provider("openrouter")
        session.add_all([_provider("openai", is_default=True), openrouter])
        await session.commit()
        _, _, _, steps = await _walk(session, _classify_graph())

        assert next(s for s in steps if s.node_id == "c1").error is None
        assert [c["config_id"] for c in decide.calls] == [str(openrouter.id)]


@pytest.mark.asyncio
async def test_unpinned_asks_for_a_pin_rather_than_guess_between_providers(monkeypatch):
    decide = _Decide(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        session.add_all([_provider("openrouter", is_default=True), _provider("openrouter")])
        await session.commit()
        _, _, _, steps = await _walk(session, _classify_graph())

        c1 = next(s for s in steps if s.node_id == "c1")
        assert c1.error == AMBIGUOUS_PROVIDER_MESSAGE
        assert decide.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_model", ["typesafe/jev-1.13", "another/model"], ids=["same-model", "other-model"])
async def test_unpinned_takes_the_provider_the_platform_decision_model_names(monkeypatch, platform_model):
    # OpenRouter lists no decision model: with a second decision provider, the
    # administrator's Decision Model setting is what names it for its model.
    decide = _Decide(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        named = _provider("openrouter")
        session.add_all([_provider("openrouter", is_default=True), named])
        session.add(PlatformSettings(decision_model=platform_model, decision_provider_id=named.id))
        await session.commit()
        _, _, _, steps = await _walk(session, _classify_graph())

        c1 = next(s for s in steps if s.node_id == "c1")
        if platform_model == "typesafe/jev-1.13":
            assert c1.error is None
            assert [c["config_id"] for c in decide.calls] == [str(named.id)]
        else:
            assert c1.error == AMBIGUOUS_PROVIDER_MESSAGE
            assert decide.calls == []


@pytest.mark.asyncio
async def test_a_card_listing_the_model_outranks_the_platform_decision_model(monkeypatch):
    # Granted-first card order keeps a team's traffic on its own provider.
    decide = _Decide(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        carded = _provider("openrouter", models=["typesafe/jev-1.13"])
        platform = _provider("openrouter", is_default=True)
        session.add_all([carded, platform])
        session.add(PlatformSettings(decision_model="typesafe/jev-1.13", decision_provider_id=platform.id))
        await session.commit()
        await _walk(session, _classify_graph())

        assert [c["config_id"] for c in decide.calls] == [str(carded.id)]


@pytest.mark.asyncio
async def test_an_endpoint_declaring_no_decision_model_leaves_openrouter_the_only_candidate(monkeypatch):
    decide = _Decide(monkeypatch, OpenRouterProvider, CustomOpenAIProvider)
    async with _temp_database() as sessions, sessions() as session:
        openrouter = _provider("openrouter")
        session.add_all([openrouter, _provider("custom_openai", is_default=True)])
        await session.commit()
        _, _, _, steps = await _walk(session, _classify_graph())

        assert next(s for s in steps if s.node_id == "c1").error is None
        assert [c["config_id"] for c in decide.calls] == [str(openrouter.id)]


@pytest.mark.asyncio
async def test_a_lone_endpoint_declaring_no_decision_model_is_not_called(monkeypatch):
    decide = _Decide(monkeypatch, CustomOpenAIProvider)
    async with _temp_database() as sessions, sessions() as session:
        session.add(_provider("custom_openai"))
        await session.commit()
        _, _, _, steps = await _walk(session, _classify_graph())

        assert next(s for s in steps if s.node_id == "c1").error == UNLISTED_MODEL_MESSAGE
        assert decide.calls == []


@pytest.mark.asyncio
async def test_unpinned_takes_the_only_provider_whose_decision_listing_has_the_model(monkeypatch):
    decide = _Decide(monkeypatch)
    decide.listed = {"OpenRouterProvider": ["typesafe/jev-1.13"]}
    async with _temp_database() as sessions, sessions() as session:
        openrouter = _provider("openrouter")
        session.add_all([_provider("openai", is_default=True), openrouter])
        await session.commit()
        _, _, _, steps = await _walk(session, _classify_graph())

        assert next(s for s in steps if s.node_id == "c1").error is None
        assert [c["config_id"] for c in decide.calls] == [str(openrouter.id)]


@pytest.mark.asyncio
async def test_unpinned_with_no_decision_provider_fails_with_the_setup_hint(monkeypatch):
    decide = _Decide(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        session.add(_provider("anthropic"))
        await session.commit()
        _, _, _, steps = await _walk(session, _classify_graph())

        c1 = next(s for s in steps if s.node_id == "c1")
        assert c1.error == NO_DECISION_PROVIDER_MESSAGE
        assert decide.calls == []


@pytest.mark.asyncio
async def test_a_pinned_provider_restricted_to_others_is_refused_not_swapped(monkeypatch):
    decide = _Decide(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        reserved = _provider(access_restricted=True, shared_user_ids=[str(uuid.uuid4())])
        session.add_all([reserved, _provider()])
        await session.commit()
        _, _, _, steps = await _walk(
            session, _classify_graph(provider=str(reserved.id))
        )

        c1 = next(s for s in steps if s.node_id == "c1")
        assert c1.error == "The LLM provider picked on this node is not available to you."
        assert decide.calls == []


@pytest.mark.asyncio
async def test_a_pinned_provider_granted_through_a_group_is_used(monkeypatch):
    decide = _Decide(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _seed(session, graph=_classify_graph())
        group = Group(id=uuid.uuid4(), name=f"SOC {uuid.uuid4().hex[:6]}")
        session.add(group)
        await session.flush()
        session.add(GroupMember(group_id=group.id, user_id=user.id))
        granted = _provider(access_restricted=True, shared_group_ids=[str(group.id)])
        session.add(granted)
        await session.commit()
        flow.graph["nodes"][1]["config"]["provider"] = str(granted.id)
        _, outcome = await _run_to_outcome(
            session, user, flow, payload={"title": "Mimikatz on DC01"}
        )

        assert outcome.kind == "completed"
        assert [c["config_id"] for c in decide.calls] == [str(granted.id)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pin", "message"),
    [
        ("anthropic", "The LLM provider picked on this node does not serve decision models. Pick an OpenRouter, OpenAI, Ollama, Filigran AI Gateway or custom OpenAI-compatible provider."),
        ("missing", "The LLM provider picked on this node no longer exists."),
    ],
)
async def test_a_pinned_provider_that_cannot_serve_is_refused(monkeypatch, pin, message):
    decide = _Decide(monkeypatch)
    async with _temp_database() as sessions, sessions() as session:
        anthropic = _provider("anthropic")
        session.add_all([anthropic, _provider()])
        await session.commit()
        pinned = str(anthropic.id) if pin == "anthropic" else str(uuid.uuid4())
        _, _, _, steps = await _walk(session, _classify_graph(provider=pinned))

        c1 = next(s for s in steps if s.node_id == "c1")
        assert c1.error == message
        assert decide.calls == []


# ── The built-in example pair, run verbatim ──────────────────────────────────

_ALERT = {"_id": "~4242", "title": "Mimikatz on DC01"}


async def _event_type(session):
    from app.models.flow_event import FlowEventType
    from app.services.flow_examples import EXAMPLE_EVENT_TYPES

    spec = EXAMPLE_EVENT_TYPES["thehive_alert"]
    event_type = FlowEventType(user_id=None, name=spec["name"], fields=spec["fields"])
    session.add(event_type)
    await session.flush()
    return str(event_type.id)


async def _triage_flow(session, monkeypatch):
    from app.services.flow_examples import example_clm_alert_triage_graph

    prompts = []

    async def fake_run_agent_chat(db, user_id, agent, history, user_message, **kw):
        prompts.append(user_message)
        return "Promoted to case #12.", None

    import app.services.agent_chat as agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", fake_run_agent_chat)

    session.add(_provider(models=["typesafe/jev-1.13"]))
    await session.commit()
    event_type_id = await _event_type(session)
    user, flow = await _seed(session, graph=example_clm_alert_triage_graph(event_type_id))
    agent = Agent(id=uuid.uuid4(), user_id=user.id, name="TheHive Case Manager")
    session.add(agent)
    await session.commit()
    ag = next(n for n in flow.graph["nodes"] if n["id"] == "ag")
    ag["config"]["agent_id"] = str(agent.id)
    return user, flow, prompts


def _event_payload(alert=_ALERT):
    """What an emission of the intake hands the triage run's trigger."""
    return {
        "fields": {
            "alert_id": alert["_id"],
            "title": alert["title"],
            "alert": json.dumps(alert),
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        ChoiceDecision(choice="investigate", confidence=0.95),
        # Unsure, whatever the label: the agent decides, nothing is dropped.
        ChoiceDecision(choice="noise", confidence=0.5),
    ],
    ids=["investigate", "uncertain-noise"],
)
async def test_the_triage_sends_relevant_and_uncertain_alerts_to_the_agent(
    monkeypatch, decision
):
    decide = _Decide(monkeypatch)
    decide.decision = decision
    async with _temp_database() as sessions, sessions() as session:
        user, flow, prompts = await _triage_flow(session, monkeypatch)
        run, outcome = await _run_to_outcome(
            session, user, flow, payload=_event_payload()
        )

        assert outcome.kind == "completed"
        assert [s.node_id for s in await _steps(session, run)] == ["t1", "clm", "ag"]
        assert len(prompts) == 1
        assert "Alert ~4242" in prompts[0]
        assert f"CLM label: {decision.choice}" in prompts[0]
        assert len(decide.calls) == 1
        assert decide.calls[0]["choices"].keys() == {"investigate", "noise"}
        assert json.loads(decide.calls[0]["state"]) == _ALERT


@pytest.mark.asyncio
async def test_the_triage_flags_an_unclassified_alert_to_a_person(monkeypatch):
    decide = _Decide(monkeypatch)
    decide.error = DecisionResponseError("The model refused to classify this content.")
    async with _temp_database() as sessions, sessions() as session:
        user, flow, prompts = await _triage_flow(session, monkeypatch)
        run, outcome = await _run_to_outcome(
            session, user, flow, payload=_event_payload()
        )

        assert outcome.kind == "completed"
        assert [s.node_id for s in await _steps(session, run)] == ["t1", "clm", "failed"]
        assert prompts == [], "no agent run for an alert the CLM could not read"


@pytest.mark.asyncio
async def test_the_intake_starts_one_triage_run_per_alert_even_while_one_is_active():
    """The fan-out: webhook-triggered runs are one at a time per flow, event
    listener runs are not, so each alert gets its own concurrent triage run."""
    from app.models.flow import Flow
    from app.services.flow_examples import (
        example_clm_alert_intake_graph,
        example_clm_alert_triage_graph,
    )

    async with _temp_database() as sessions, sessions() as session:
        event_type_id = await _event_type(session)
        user, intake = await _seed(
            session, graph=example_clm_alert_intake_graph(event_type_id)
        )
        triage = Flow(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Triage",
            status="active",
            graph=example_clm_alert_triage_graph(event_type_id),
        )
        session.add(triage)
        await session.commit()

        for alert_id in ("~1", "~2"):
            run, outcome = await _run_to_outcome(
                session,
                user,
                intake,
                payload={"object": {"_id": alert_id, "title": f"Alert {alert_id}"}},
            )
            assert outcome.kind == "completed"
            emit = next(s for s in await _steps(session, run) if s.node_id == "emit")
            assert emit.output["listeners_started"] == 1

        triage_runs = (
            (
                await session.execute(
                    select(FlowRun)
                    .where(FlowRun.flow_id == triage.id)
                    .order_by(FlowRun.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert [r.trigger_type for r in triage_runs] == ["event", "event"]
        assert all(r.status in FLOW_RUN_ACTIVE_STATUSES for r in triage_runs), (
            "the second alert's run starts while the first is still active"
        )
        assert [r.trigger_payload["fields"]["alert_id"] for r in triage_runs] == [
            "~1",
            "~2",
        ]
