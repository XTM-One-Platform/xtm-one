# SPDX-License-Identifier: Apache-2.0

"""Decision models (CLMs) — typed classification calls to an LLM provider.

A *classifier language model* (CLM) such as TypeSafe's Jev or Cloudflare's
Clef does not write text: it reads a piece of content, answers typed questions
about it, and returns a probability per allowed answer. That makes it a cheap,
fast and repeatable filter in front of an LLM — sort a SIEM's alerts, keep the
ones that need an analyst, and only those reach an agent.

CLMs are served by the LLM providers the platform already configures, on an
endpoint of their own next to chat completions. Two wire formats exist today,
and each provider class declares the one it speaks
(``BaseLLMProvider.decisions_api``):

* ``system_one`` — TypeSafe's System One API (``POST {base}/systemone``):
  Jev natively, Clef (advertised as Jev-API compatible), OpenRouter.
* ``openai_decisions`` — OpenAI's Decisions API (``POST {base}/decisions``).

Only the *choice* question is used: the caller declares the labels, the model
returns the most probable one with its probability distribution and a
confidence. A yes/no filter is a choice between two labels.

The answer is external input: it is validated against the labels the caller
declared, and anything else raises :class:`DecisionResponseError` rather than
being read as a decision.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.redis import redis_client
from app.core.utils import format_exception_message

if TYPE_CHECKING:
    from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

DECISIONS_SYSTEM_ONE = "system_one"
DECISIONS_OPENAI = "openai_decisions"

DECISIONS_APIS: frozenset[str] = frozenset({DECISIONS_SYSTEM_ONE, DECISIONS_OPENAI})

# The one question every request carries; the answer is read back by this key.
_QUESTION_NAME = "label"

# Both APIs cap a choice question at 255 options.
MAX_CHOICES = 255

# A decision takes milliseconds; the provider client's own read timeout is
# sized for long chat generations and would hold a flow step for minutes.
DECISION_TIMEOUT_SECONDS = 30.0


class DecisionsNotSupportedError(Exception):
    """The provider does not serve decision models."""


class DecisionResponseError(Exception):
    """The provider answered, but not with a decision over the declared labels."""


@dataclass(frozen=True)
class ChoiceDecision:
    """The model's answer to one choice question."""

    choice: str
    # 0..1, derived by the provider from the distribution. When the provider
    # sends none, the chosen label's own probability stands in.
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


def _unit_float(value: Any) -> float | None:
    """A finite float in [0, 1], or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        return None
    return number


def build_choice_request(
    api: str,
    *,
    model: str,
    state: str,
    instructions: str,
    choices: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    """``(path, body)`` of a single-choice request in *api*'s wire format."""
    if not choices:
        raise ValueError("A choice question needs at least one label.")
    if len(choices) > MAX_CHOICES:
        raise ValueError(f"A choice question takes at most {MAX_CHOICES} labels.")
    if api == DECISIONS_SYSTEM_ONE:
        return "/systemone", {
            "model": model,
            "state": state,
            "questions": {
                _QUESTION_NAME: {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": {
                        label: (description or None)
                        for label, description in choices.items()
                    },
                }
            },
        }
    if api == DECISIONS_OPENAI:
        return "/decisions", {
            "model": model,
            "input": state,
            "questions": [
                {
                    "type": "choice",
                    "name": _QUESTION_NAME,
                    "instructions": instructions,
                    "choices": [
                        # The description is required there; the label
                        # itself is the best description a caller left empty.
                        {"value": label, "description": description or label}
                        for label, description in choices.items()
                    ],
                }
            ],
        }
    raise DecisionsNotSupportedError(f"Unknown decisions API `{api}`.")


def _answer(api: str, payload: Any) -> dict[str, Any]:
    """The raw answer to our question, located in *api*'s response shape."""
    if not isinstance(payload, dict):
        raise DecisionResponseError("The response is not a JSON object.")
    # Cloudflare Workers AI wraps every model's output in a `result` envelope.
    if "answers" not in payload and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    answers = payload.get("answers")
    if api == DECISIONS_SYSTEM_ONE:
        answer = answers.get(_QUESTION_NAME) if isinstance(answers, dict) else None
    else:
        answer = next(
            (
                a
                for a in (answers if isinstance(answers, list) else [])
                if isinstance(a, dict) and a.get("name") == _QUESTION_NAME
            ),
            None,
        )
    if not isinstance(answer, dict):
        raise DecisionResponseError("The response carries no answer to the question.")
    if answer.get("type") == "refusal":
        raise DecisionResponseError("The model refused to classify this content.")
    return answer


def _probabilities(raw: Any, labels: dict[str, str]) -> dict[str, float]:
    """``{label: probability}`` for the declared labels, from either shape."""
    pairs: list[tuple[Any, Any]] = []
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, list):
        pairs = [
            (item.get("value"), item.get("probability"))
            for item in raw
            if isinstance(item, dict)
        ]
    result: dict[str, float] = {}
    for label, probability in pairs:
        number = _unit_float(probability)
        if isinstance(label, str) and label in labels and number is not None:
            result[label] = number
    return result


def parse_choice_response(
    api: str, payload: Any, choices: dict[str, str]
) -> ChoiceDecision:
    """Read *payload* as the answer to a choice question over *choices*.

    Raises :class:`DecisionResponseError` for anything that is not a decision
    among the declared labels — a refusal, a missing answer, an unknown label.
    """
    answer = _answer(api, payload)
    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in choices:
        raise DecisionResponseError("The model answered with a label that was not offered.")
    probabilities = _probabilities(answer.get("probabilities"), choices)
    confidence = _unit_float(answer.get("confidence"))
    if confidence is None:
        confidence = probabilities.get(choice, 0.0)
    usage = payload.get("usage") if isinstance(payload, dict) else None
    model = payload.get("model") if isinstance(payload, dict) else None
    return ChoiceDecision(
        choice=choice,
        confidence=confidence,
        probabilities=probabilities,
        model=model if isinstance(model, str) else None,
        usage=usage if isinstance(usage, dict) else {},
    )


async def decide_choice(
    client: AsyncOpenAI,
    api: str,
    *,
    model: str,
    state: str,
    instructions: str,
    choices: dict[str, str],
    headers: Mapping[str, str] | None = None,
) -> ChoiceDecision:
    """Send one choice question through *client* (the provider's own SDK client).

    The provider's client carries its base URL, credentials and retry policy,
    so a decision call goes exactly where the provider's chat calls go; only
    the timeout is the decision's own (:data:`DECISION_TIMEOUT_SECONDS`), and
    *headers* the provider's own for this model (an upstream to pin).
    """
    path, body = build_choice_request(
        api, model=model, state=state, instructions=instructions, choices=choices
    )
    options: dict[str, Any] = {"timeout": DECISION_TIMEOUT_SECONDS}
    if headers:
        options["headers"] = dict(headers)
    payload = await client.post(path, cast_to=object, body=body, options=options)
    return parse_choice_response(api, payload, choices)


# Whether a model serves decisions, when the provider tells only when asked
# (OpenAI). A "no" lasts an hour, so access granted to a gated account shows soon.
DECISION_SUPPORT_TTL_SECONDS = {True: 86_400, False: 3_600}

# A settings page or a flow step waits on a provider's decision listing; past
# this it lists nothing for that provider.
DECISION_LISTING_TIMEOUT_SECONDS = 10.0

# A settings page waits on these; a Redis that does not answer is no memo.
_SUPPORT_REDIS_TIMEOUT_SECONDS = 2.0


def _support_key(config_id: str, scope: str, model: str) -> str:
    return f"llm:decision-model:{config_id}:{scope}:{model}"


async def recall_decision_support(
    config_id: str | None, scope: str, models: Iterable[str]
) -> dict[str, bool]:
    """``{model: serves decisions}`` for the *models* checked recently; the others are absent."""
    models = list(models)
    if config_id is None or not models:
        return {}
    try:
        async with asyncio.timeout(_SUPPORT_REDIS_TIMEOUT_SECONDS):
            client = await redis_client()
            values = await client.mget([_support_key(config_id, scope, m) for m in models])
    except Exception as exc:
        logger.debug("Decision model checks not recalled: %s", format_exception_message(exc))
        return {}
    recalled: dict[str, bool] = {}
    for model, value in zip(models, values, strict=True):
        if isinstance(value, bytes):
            value = value.decode("ascii", "replace")
        if value in ("0", "1"):
            recalled[model] = value == "1"
    return recalled


_RELEASE_IF_OWNER_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def _round_key(config_id: str, scope: str) -> str:
    return f"llm:decision-model-round:{config_id}:{scope}"


async def claim_decision_round(config_id: str | None, scope: str, ttl_seconds: int) -> str | None:
    """The token that ends this caller's round, ``None`` while another round runs.

    One round per config and scope at a time: concurrent page loads would
    otherwise each ask about every unchecked model. A Redis that does not
    answer lets the round run (the answers are free), with an empty token.
    """
    if config_id is None:
        return ""
    token = uuid.uuid4().hex
    try:
        async with asyncio.timeout(_SUPPORT_REDIS_TIMEOUT_SECONDS):
            client = await redis_client()
            won = await client.set(_round_key(config_id, scope), token, nx=True, ex=ttl_seconds)
            return token if won else None
    except Exception as exc:
        logger.debug("Decision model round not claimed: %s", format_exception_message(exc))
        return ""


async def release_decision_round(config_id: str | None, scope: str, token: str) -> None:
    """End the round *token* claimed, never a later one its expiry let start; the expiry ends it otherwise."""
    if config_id is None or not token:
        return
    try:
        async with asyncio.timeout(_SUPPORT_REDIS_TIMEOUT_SECONDS):
            client = await redis_client()
            await client.eval(_RELEASE_IF_OWNER_LUA, 1, _round_key(config_id, scope), token)
    except Exception as exc:
        logger.debug("Decision model round not released: %s", format_exception_message(exc))


async def remember_decision_support(
    config_id: str | None, scope: str, results: Mapping[str, bool]
) -> None:
    """Remember *results* (:data:`DECISION_SUPPORT_TTL_SECONDS`); a Redis failure only loses them."""
    if config_id is None or not results:
        return
    try:
        async with asyncio.timeout(_SUPPORT_REDIS_TIMEOUT_SECONDS):
            client = await redis_client()
            async with client.pipeline(transaction=False) as pipe:
                for model, serves in results.items():
                    pipe.set(
                        _support_key(config_id, scope, model),
                        "1" if serves else "0",
                        ex=DECISION_SUPPORT_TTL_SECONDS[serves],
                    )
                await pipe.execute()
    except Exception as exc:
        logger.debug("Decision model checks not remembered: %s", format_exception_message(exc))
