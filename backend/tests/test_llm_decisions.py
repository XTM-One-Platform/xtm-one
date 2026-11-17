"""Decision models (CLMs) on the LLM providers - ``app.llm.decisions``.

The two wire formats (TypeSafe System One, OpenAI Decisions), the reading of
an answer as external input, and which provider classes declare one.
"""

import pytest

from app.llm.decisions import (
    DECISION_TIMEOUT_SECONDS,
    DECISIONS_OPENAI,
    DECISIONS_SYSTEM_ONE,
    MAX_CHOICES,
    ChoiceDecision,
    DecisionResponseError,
    DecisionsNotSupportedError,
    build_choice_request,
    parse_choice_response,
)

_CHOICES = {"investigate": "Needs an analyst.", "noise": ""}


# ── Requests ─────────────────────────────────────────────────────────────────


def test_system_one_request_shape():
    path, body = build_choice_request(
        DECISIONS_SYSTEM_ONE,
        model="typesafe/jev-1.13",
        state="alert",
        instructions="Worth an analyst?",
        choices=_CHOICES,
    )
    assert path == "/systemone"
    assert body == {
        "model": "typesafe/jev-1.13",
        "state": "alert",
        "questions": {
            "label": {
                "type": "choice",
                "instructions": "Worth an analyst?",
                # An empty description is sent as null, as the API allows.
                "criteria": {"investigate": "Needs an analyst.", "noise": None},
            }
        },
    }


def test_openai_request_shape():
    path, body = build_choice_request(
        DECISIONS_OPENAI,
        model="gpt-6-luna",
        state="alert",
        instructions="Worth an analyst?",
        choices=_CHOICES,
    )
    assert path == "/decisions"
    assert body == {
        "model": "gpt-6-luna",
        "input": "alert",
        "questions": [
            {
                "type": "choice",
                "name": "label",
                "instructions": "Worth an analyst?",
                # The description is required there: the label stands in.
                "choices": [
                    {"value": "investigate", "description": "Needs an analyst."},
                    {"value": "noise", "description": "noise"},
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "choices",
    [{}, {f"l{i}": "" for i in range(MAX_CHOICES + 1)}],
    ids=["no-label", "too-many-labels"],
)
def test_a_request_needs_one_to_255_labels(choices):
    with pytest.raises(ValueError):
        build_choice_request(
            DECISIONS_SYSTEM_ONE, model="m", state="s", instructions="q", choices=choices
        )


def test_an_unknown_api_is_refused():
    with pytest.raises(DecisionsNotSupportedError):
        build_choice_request(
            "chat", model="m", state="s", instructions="q", choices=_CHOICES
        )


# ── Responses ────────────────────────────────────────────────────────────────

_SYSTEM_ONE_ANSWER = {
    "model": "jev-1.13.0",
    "answers": {
        "label": {
            "type": "choice",
            "choice": "investigate",
            "probabilities": {"investigate": 0.9, "noise": 0.1},
            "confidence": 0.85,
        }
    },
    "usage": {"input_tokens": 250, "output_tokens": 40},
}

_OPENAI_ANSWER = {
    "answers": [
        {
            "type": "choice",
            "name": "label",
            "choice": "investigate",
            "probabilities": [
                {"value": "investigate", "probability": 0.9},
                {"value": "noise", "probability": 0.1},
            ],
            "confidence": 0.85,
        }
    ],
}


def test_a_system_one_answer_is_read():
    assert parse_choice_response(
        DECISIONS_SYSTEM_ONE, _SYSTEM_ONE_ANSWER, _CHOICES
    ) == ChoiceDecision(
        choice="investigate",
        confidence=0.85,
        probabilities={"investigate": 0.9, "noise": 0.1},
        model="jev-1.13.0",
        usage={"input_tokens": 250, "output_tokens": 40},
    )


def test_an_openai_answer_is_read():
    assert parse_choice_response(
        DECISIONS_OPENAI, _OPENAI_ANSWER, _CHOICES
    ) == ChoiceDecision(
        choice="investigate",
        confidence=0.85,
        probabilities={"investigate": 0.9, "noise": 0.1},
    )


def test_a_workers_ai_result_envelope_is_unwrapped():
    wrapped = {"result": _SYSTEM_ONE_ANSWER, "success": True}
    decision = parse_choice_response(DECISIONS_SYSTEM_ONE, wrapped, _CHOICES)
    assert decision.choice == "investigate"
    assert decision.confidence == 0.85


def test_a_missing_confidence_falls_back_to_the_label_probability():
    payload = {
        "answers": {
            "label": {
                "type": "choice",
                "choice": "noise",
                "probabilities": {"investigate": 0.3, "noise": 0.7},
            }
        }
    }
    decision = parse_choice_response(DECISIONS_SYSTEM_ONE, payload, _CHOICES)
    assert decision.confidence == 0.7


def test_probabilities_keep_only_declared_labels_with_unit_values():
    payload = {
        "answers": {
            "label": {
                "type": "choice",
                "choice": "noise",
                "probabilities": {
                    "noise": 0.6,
                    "investigate": 1.7,  # out of range
                    "injected": 0.4,  # not offered
                },
                "confidence": True,  # not a number
            }
        }
    }
    decision = parse_choice_response(DECISIONS_SYSTEM_ONE, payload, _CHOICES)
    assert decision.probabilities == {"noise": 0.6}
    assert decision.confidence == 0.6


@pytest.mark.parametrize(
    ("api", "payload"),
    [
        (DECISIONS_SYSTEM_ONE, None),
        (DECISIONS_SYSTEM_ONE, []),
        (DECISIONS_SYSTEM_ONE, {}),
        (DECISIONS_SYSTEM_ONE, {"answers": {}}),
        (DECISIONS_SYSTEM_ONE, {"answers": {"other": {"choice": "noise"}}}),
        (DECISIONS_SYSTEM_ONE, {"answers": {"label": {"choice": "maybe"}}}),
        (DECISIONS_SYSTEM_ONE, {"answers": {"label": {"choice": None}}}),
        (DECISIONS_SYSTEM_ONE, {"answers": {"label": {"type": "refusal"}}}),
        (DECISIONS_OPENAI, {"answers": {"label": {"choice": "noise"}}}),
        (DECISIONS_OPENAI, {"answers": [{"name": "other", "choice": "noise"}]}),
        (DECISIONS_OPENAI, {"answers": [{"name": "label", "type": "refusal"}]}),
        (DECISIONS_OPENAI, {"answers": [{"name": "label", "choice": "maybe"}]}),
    ],
    ids=[
        "null",
        "list",
        "empty",
        "no-answer",
        "other-question",
        "unknown-label",
        "null-label",
        "system-one-refusal",
        "openai-wrong-shape",
        "openai-other-question",
        "openai-refusal",
        "openai-unknown-label",
    ],
)
def test_anything_but_a_decision_among_the_labels_is_refused(api, payload):
    with pytest.raises(DecisionResponseError):
        parse_choice_response(api, payload, _CHOICES)


# ── Providers ────────────────────────────────────────────────────────────────


def test_the_decision_providers_and_the_format_each_speaks():
    from app.llm.router import PROVIDER_MAP

    declared = {
        name: cls.decisions_api
        for name, cls in PROVIDER_MAP.items()
        if cls.decisions_api is not None
    }
    assert declared == {
        "openai": DECISIONS_OPENAI,
        "custom_openai": DECISIONS_OPENAI,
        "filigran_gateway": DECISIONS_OPENAI,
        # LM Studio serves OpenAI's /v1/decisions and types its decision models.
        "lmstudio": DECISIONS_OPENAI,
        "openrouter": DECISIONS_SYSTEM_ONE,
        "ollama": DECISIONS_SYSTEM_ONE,
    }


class _SpyClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def post(self, path, *, cast_to, body, options):
        self.calls.append(
            {"path": path, "cast_to": cast_to, "body": body, "options": options}
        )
        return self.payload


@pytest.mark.asyncio
async def test_openrouter_sends_the_system_one_request_on_its_own_client():
    from app.llm.openrouter_provider import OpenRouterProvider

    provider = OpenRouterProvider(api_key="sk-or-test")
    spy = _SpyClient(_SYSTEM_ONE_ANSWER)
    provider.client = spy
    decision = await provider.decide_choice(
        model="typesafe/jev-1.13",
        state="alert",
        instructions="Worth an analyst?",
        choices=_CHOICES,
    )
    assert decision.choice == "investigate"
    assert len(spy.calls) == 1
    assert spy.calls[0]["path"] == "/systemone"
    assert spy.calls[0]["cast_to"] is object
    assert spy.calls[0]["body"]["model"] == "typesafe/jev-1.13"
    # Its own short timeout, not the client's chat-sized one.
    assert spy.calls[0]["options"] == {"timeout": DECISION_TIMEOUT_SECONDS}


@pytest.mark.asyncio
async def test_openai_sends_the_decisions_request_on_its_own_client():
    from app.llm.openai_provider import OpenAIProvider

    provider = OpenAIProvider(api_key="sk-test")
    spy = _SpyClient(_OPENAI_ANSWER)
    provider.client = spy
    decision = await provider.decide_choice(
        model="gpt-6-luna", state="alert", instructions="q", choices=_CHOICES
    )
    assert decision.choice == "investigate"
    assert [c["path"] for c in spy.calls] == ["/decisions"]


@pytest.mark.asyncio
async def test_an_openai_decision_model_window_is_the_one_set_for_it():
    # OpenAI reports no context window: only the administrator's per-model
    # window applies, else the window is unknown.
    from app.llm.openai_provider import OpenAIProvider

    provider = OpenAIProvider(api_key="sk-test")
    provider.context_windows = {"gpt-6-luna": 1_050_000}

    assert await provider.decision_context_window("gpt-6-luna") == 1_050_000
    assert await provider.decision_context_window("gpt-other") is None


@pytest.mark.asyncio
async def test_a_provider_without_a_decisions_api_refuses():
    from app.llm.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(api_key="sk-ant-test")
    with pytest.raises(DecisionsNotSupportedError):
        await provider.decide_choice(
            model="claude-sonnet-5", state="s", instructions="q", choices=_CHOICES
        )


def test_the_openai_probe_deadline_fits_in_the_listing_timeout():
    from app.llm.decisions import DECISION_LISTING_TIMEOUT_SECONDS
    from app.llm.openai_provider import DECISION_PROBE_DEADLINE_SECONDS

    assert DECISION_PROBE_DEADLINE_SECONDS < DECISION_LISTING_TIMEOUT_SECONDS


def test_only_openai_compatible_endpoints_count_when_they_list_a_decision_model():
    from app.llm.router import PROVIDER_MAP

    assert {name for name, cls in PROVIDER_MAP.items() if cls.decisions_only_when_listed} == {
        "filigran_gateway",
        "custom_openai",
        "lmstudio",
    }


def test_every_decision_provider_but_openrouter_lists_its_decision_models():
    from app.llm.router import PROVIDER_MAP

    assert {name for name, cls in PROVIDER_MAP.items() if cls.decisions_api and not cls.lists_decision_models} == {
        "openrouter"
    }
