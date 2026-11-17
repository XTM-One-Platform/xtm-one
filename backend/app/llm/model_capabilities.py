# SPDX-License-Identifier: Apache-2.0

"""Small, manually maintained model-parameter rules."""

from __future__ import annotations

from dataclasses import dataclass

from app.llm.model_limits import _normalize_family_key

# TODO(future-proofing): this is a static, hand-maintained prefix list and
# will drift as new model families ship. Prefer sourcing these
# rules from provider/gateway metadata where available instead of adding more
# prefixes by hand — e.g. OpenRouter exposes a `supported_parameters` array
# per model that already answers "does this model accept `temperature`?".
# A gateway-backed capability lookup (cached, with this table as fallback for
# providers with no such metadata) would make new Claude/GPT releases work
# without a code change. Tracked as a follow-up, not solved here.

DEFAULT_OUTPUT_TOKENS = "max_tokens"

# Reasoning effort levels, lowest first: Anthropic ``output_config.effort``
# (the pinned SDK's ``OutputConfigParam.effort``) and the ``reasoning_effort``
# of the OpenAI-compatible APIs (OpenAI, xAI, Mistral).
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_EFFORT_LEVELS_WITHOUT_XHIGH = ("low", "medium", "high", "max")
_EFFORT_LEVELS_WITHOUT_MAX = ("low", "medium", "high", "xhigh")


# Keep broad family rules here; add an exact prefix before them only when a
# model differs from its family behavior.
@dataclass(frozen=True)
class ModelParameterRule:
    prefixes: tuple[str, ...]
    output_tokens: str = DEFAULT_OUTPUT_TOKENS
    omit_temperature: bool = False
    adaptive_thinking: bool = False
    # Anthropic marks the model's thinking "Always on": it refuses
    # ``thinking: {"type": "disabled"}`` with HTTP 400, so ``disable_thinking``
    # sends the adaptive + lowest-effort form from the first request
    # instead of learning it from that refusal. Only read together with
    # ``adaptive_thinking``, which a rule setting it must set too.
    thinking_always_on: bool = False
    # Anthropic refuses ``disabled`` on this model and documents
    # ``thinking: {"type": "between_tools"}`` as its lowest setting: no
    # up-front thinking, and without tools a text-only answer, as ``disabled``
    # gives elsewhere. That form takes no other thinking field and no effort
    # above ``thinking_off_max_effort``, which a rule setting it must set, with
    # ``adaptive_thinking`` and without ``thinking_always_on``.
    between_tools_thinking_off: bool = False
    # Highest ``output_config.effort`` the model accepts with thinking turned
    # off (Opus 5 refuses ``disabled`` at ``xhigh`` / ``max``); a caller's
    # higher effort is lowered to it there. ``None``: no ceiling.
    thinking_off_max_effort: str | None = None
    # The model thinks by default but returns thinking blocks EMPTY unless the
    # request sets ``thinking.display`` to ``"summarized"`` (Anthropic's default
    # display is ``"omitted"`` on these models), so a reasoning pane stays
    # blank until the request opts in. A rule setting it must also set
    # ``omit_temperature``: the API refuses ``temperature`` alongside an
    # explicit ``thinking`` config. The same field is what
    # ``thinks_by_default()`` reads (thinking replay): every model that
    # hides its text by default thinks on every request by default.
    summarized_thinking_opt_in: bool = False
    # Anthropic accepts a ``system`` message inside ``messages`` on this model
    # (older models answer "role 'system' is not supported on this model"), so
    # the loop's mid-run nudges can stay where they were appended instead of
    # being merged into the top-level ``system``, which invalidated the cached
    # conversation on every nudge. Verified per model against the API.
    in_place_system_messages: bool = False
    # The effort levels the model accepts (Anthropic ``output_config.effort``,
    # OpenAI-compatible ``reasoning_effort``): a level it refuses is an HTTP
    # 400. Empty for a model that takes no effort at all; ``None`` where they
    # are not declared, and a requested level is sent as it is.
    effort_levels: tuple[str, ...] | None = None
    # OpenAI refuses a ``reasoning_effort`` other than ``none`` next to
    # function tools on ``/v1/chat/completions`` for this model (HTTP 400);
    # only ``/v1/responses`` takes both, so ``none`` is sent there.
    effort_refused_with_tools: bool = False
    # The model refuses ``reasoning_effort: "none"`` (HTTP 400
    # ``unsupported_value``): a caller's ``none`` / ``minimal`` is sent as its
    # lowest declared level instead. Read with ``effort_levels``.
    refuses_effort_none: bool = False
    # On ``/v1/chat/completions`` the model has no valid request with function
    # tools: an effort next to tools asks for ``none`` and ``none`` is refused
    # (gpt-6-astra). The native OpenAI provider sends its requests with tools
    # to ``/v1/responses`` instead (``openai_responses``), with a level the
    # model takes.
    responses_api_with_tools: bool = False
    # Anthropic refuses a ``tool_choice`` that forces a tool call (type ``any``
    # or ``tool``) on this model with HTTP 400, token counting included.
    refuses_forced_tool_choice: bool = False
    # Amazon Bedrock serves this model without structured outputs (Converse
    # ``outputConfig.textFormat``, strict tools); the Claude API has them.
    no_bedrock_structured_output: bool = False
    # Models the same provider is asked, in order, when this one declines a
    # request in a category another model may answer
    # (``ModelDeclinedError.failover_allowed``: ``cyber``, ``frontier_llm``),
    # before the rest of the failover chain. Bare Anthropic ids: only the
    # Anthropic provider raises a decline.
    decline_fallback_models: tuple[str, ...] = ()


MODEL_PARAMETER_RULES = (
    # OpenAI. The Instant / chat snapshots refuse every effort but the
    # ``medium`` they run at, so none is sent; dotted GPT-5 generations come
    # before the broad ``gpt-5`` prefix whose base and 5.1 models stop at
    # ``high``.
    ModelParameterRule(
        prefixes=(
            "gpt-5-chat",
            "gpt-5.1-chat",
            "gpt-5.2-chat",
            "gpt-5.3-chat",
            "gpt-5.4-chat",
            "gpt-5.5-chat",
            "gpt-5.6-chat",
            "gpt-chat",
        ),
        output_tokens="max_completion_tokens",
        omit_temperature=True,
        effort_levels=(),
    ),
    ModelParameterRule(
        prefixes=("gpt-5.6",),
        output_tokens="max_completion_tokens",
        omit_temperature=True,
        effort_levels=EFFORT_LEVELS,
    ),
    ModelParameterRule(
        prefixes=("gpt-5.2", "gpt-5.3", "gpt-5.4", "gpt-5.5"),
        output_tokens="max_completion_tokens",
        omit_temperature=True,
        effort_levels=_EFFORT_LEVELS_WITHOUT_MAX,
    ),
    ModelParameterRule(
        prefixes=("o1", "o3", "o4", "gpt-5"),
        output_tokens="max_completion_tokens",
        omit_temperature=True,
        effort_levels=("low", "medium", "high"),
    ),
    # gpt-6-astra answers ``reasoning_effort: "none"`` with HTTP 400
    # unsupported_value, tools or not: a caller's ``none`` becomes ``low``.
    # Its chat completions also refuse any other effort next to function
    # tools (the tools refusal of #11571), so a request with tools goes
    # through the Responses API (#12205). It refuses ``temperature`` on both
    # endpoints ("Unsupported parameter", #12216), as the o-series does.
    ModelParameterRule(
        prefixes=("gpt-6-astra",),
        output_tokens="max_completion_tokens",
        omit_temperature=True,
        effort_levels=EFFORT_LEVELS,
        refuses_effort_none=True,
        responses_api_with_tools=True,
    ),
    ModelParameterRule(
        prefixes=("gpt-6",),
        output_tokens="max_completion_tokens",
        effort_levels=EFFORT_LEVELS,
        effort_refused_with_tools=True,
    ),
    ModelParameterRule(
        prefixes=("chatgpt-",),
        output_tokens="max_completion_tokens",
        effort_levels=(),
    ),
    ModelParameterRule(prefixes=("gpt-4", "gpt-3.5"), effort_levels=()),
    ModelParameterRule(prefixes=("gpt-oss",), effort_levels=("low", "medium", "high")),
    # xAI: only these Grok models take ``reasoning_effort``; the others reason
    # without one and refuse it.
    ModelParameterRule(
        prefixes=("grok-4.7", "grok-4.6", "grok-4.20-multi-agent"),
        effort_levels=_EFFORT_LEVELS_WITHOUT_MAX,
    ),
    ModelParameterRule(prefixes=("grok-4.5",), effort_levels=("low", "medium", "high")),
    ModelParameterRule(prefixes=("grok-3-mini",), effort_levels=("low", "high")),
    ModelParameterRule(prefixes=("grok-",), effort_levels=()),
    # Mistral: reasoning is on (``high``) or off (``none``, the default) on
    # the models that adjust it; the rest take no ``reasoning_effort``.
    ModelParameterRule(
        prefixes=(
            "mistral-small-latest",
            "mistral-small-2603",
            "mistral-small-4",
            "mistral-medium-latest",
            "mistral-medium-2604",
            "mistral-medium-3-5",
            "mistral-medium-3.5",
        ),
        effort_levels=("high",),
    ),
    ModelParameterRule(
        prefixes=(
            "mistral-",
            "ministral-",
            "codestral",
            "devstral",
            "magistral",
            "pixtral",
            "voxtral",
            "open-mistral",
            "open-mixtral",
            "open-codestral",
            "labs-",
        ),
        effort_levels=(),
    ),
    # Claude 5.x: the 5.5 rules come before the 5.0 prefixes they share, since
    # each 5.5 model refuses a thinking-off form its 5.0 namesake accepts.
    # A declined request goes to the previous Sonnet, then to Opus 4.8, the
    # model Anthropic routes cyber declines of Opus 5.x and Fable 5.x to.
    ModelParameterRule(
        prefixes=("claude-sonnet-5-5",),
        omit_temperature=True,
        adaptive_thinking=True,
        between_tools_thinking_off=True,
        thinking_off_max_effort="high",
        summarized_thinking_opt_in=True,
        in_place_system_messages=True,
        effort_levels=EFFORT_LEVELS,
        refuses_forced_tool_choice=True,
        no_bedrock_structured_output=True,
        decline_fallback_models=("claude-sonnet-5", "claude-opus-4-8"),
    ),
    ModelParameterRule(
        prefixes=("claude-opus-5-5", "claude-fable-5"),
        omit_temperature=True,
        adaptive_thinking=True,
        thinking_always_on=True,
        summarized_thinking_opt_in=True,
        in_place_system_messages=True,
        effort_levels=EFFORT_LEVELS,
        decline_fallback_models=("claude-opus-4-8",),
    ),
    ModelParameterRule(
        prefixes=("claude-opus-5",),
        omit_temperature=True,
        adaptive_thinking=True,
        thinking_off_max_effort="high",
        summarized_thinking_opt_in=True,
        in_place_system_messages=True,
        effort_levels=EFFORT_LEVELS,
        decline_fallback_models=("claude-opus-4-8",),
    ),
    ModelParameterRule(
        prefixes=("claude-sonnet-5",),
        omit_temperature=True,
        adaptive_thinking=True,
        summarized_thinking_opt_in=True,
        in_place_system_messages=True,
        effort_levels=EFFORT_LEVELS,
        decline_fallback_models=("claude-opus-4-8",),
    ),
    # Haiku 5 refuses a custom temperature like the rest of 5.x (HTTP 400, checked
    # on 5.5; Haiku 4.x takes one). Temperature only: its effort levels and thinking
    # forms are not verified, so the rest stays as for an unlisted model.
    ModelParameterRule(
        prefixes=("claude-haiku-5",),
        omit_temperature=True,
    ),
    # Omitted display too, but these do not think unless the request turns
    # thinking on: opting into a display would turn it on, which changes cost
    # and behaviour, not what is shown. Opus 4.8 takes an in-place system
    # message, Opus 4.7 refuses one.
    ModelParameterRule(
        prefixes=("claude-opus-4-8",),
        omit_temperature=True,
        adaptive_thinking=True,
        in_place_system_messages=True,
        effort_levels=EFFORT_LEVELS,
    ),
    ModelParameterRule(
        prefixes=("claude-opus-4-7",),
        omit_temperature=True,
        adaptive_thinking=True,
        effort_levels=EFFORT_LEVELS,
    ),
    ModelParameterRule(
        prefixes=("claude-mythos-preview",),
        omit_temperature=True,
        adaptive_thinking=True,
        thinking_always_on=True,
        summarized_thinking_opt_in=True,
        effort_levels=_EFFORT_LEVELS_WITHOUT_XHIGH,
    ),
    ModelParameterRule(
        prefixes=("claude-mythos",),
        omit_temperature=True,
        adaptive_thinking=True,
        thinking_always_on=True,
        summarized_thinking_opt_in=True,
        effort_levels=EFFORT_LEVELS,
    ),
    # Effort only: these keep thinking off unless asked and take a custom
    # temperature while it is off. Before the broad 4.x / 3.x prefixes below.
    ModelParameterRule(
        prefixes=("claude-opus-4-6", "claude-sonnet-4-6"),
        effort_levels=_EFFORT_LEVELS_WITHOUT_XHIGH,
    ),
    ModelParameterRule(
        prefixes=("claude-opus-4-5",),
        effort_levels=("low", "medium", "high"),
    ),
    ModelParameterRule(
        prefixes=(
            "claude-sonnet-4",
            "claude-haiku-4",
            "claude-opus-4",
            "claude-3",
        ),
        effort_levels=(),
    ),
)

# Every name build_openai_generation_params() can give the output-token bound,
# derived from the rules so a rule introducing a new name needs no second list.
OUTPUT_TOKEN_PARAMETERS = frozenset(
    {DEFAULT_OUTPUT_TOKENS, *(rule.output_tokens for rule in MODEL_PARAMETER_RULES)}
)


def _normalized_rule_key(model: str) -> str:
    key = _normalize_family_key(model)
    # OpenRouter writes a Claude version with a dot (``anthropic/claude-opus-4.7``)
    # where Anthropic, Bedrock and Vertex write a dash.
    return key.replace(".", "-") if key.startswith("claude-") else key


def _find_rule(model: str | None, *, normalize: bool) -> ModelParameterRule | None:
    if not model:
        return None
    model_key = _normalized_rule_key(model) if normalize else model.lower()
    for rule in MODEL_PARAMETER_RULES:
        if any(model_key.startswith(prefix) for prefix in rule.prefixes):
            return rule
    return None


def model_parameter_rule(model: str | None) -> ModelParameterRule | None:
    """Return the first raw model-id parameter rule matching *model*.

    Matching is intentionally asymmetric: output-token names are resolved from the
    raw model id so a gateway-prefixed route like ``openai/gpt-5-mini`` keeps
    ``max_tokens``; temperature is resolved from the normalized id so the same
    gateway-prefixed Claude/GPT-5 model still omits a parameter the underlying
    provider rejects with a 400.
    """
    return _find_rule(model, normalize=False)


def omit_temperature(model: str | None) -> bool:
    """Return True when *model* does not accept a custom temperature."""
    rule = _find_rule(model, normalize=True)
    return rule.omit_temperature if rule else False


def defaults_to_adaptive_thinking(model: str | None) -> bool:
    """Return True when the model enables adaptive thinking by default."""
    rule = _find_rule(model, normalize=True)
    return rule.adaptive_thinking if rule else False


def thinking_always_on(model: str | None) -> bool:
    """Return True when *model* refuses ``thinking: {"type": "disabled"}``."""
    rule = _find_rule(model, normalize=True)
    return rule.thinking_always_on if rule else False


def between_tools_thinking_off(model: str | None) -> bool:
    """Return True when *model* turns thinking off with ``between_tools``."""
    rule = _find_rule(model, normalize=True)
    return rule.between_tools_thinking_off if rule else False


def thinking_off_max_effort(model: str | None) -> str | None:
    """Return the highest effort *model* accepts with thinking turned off."""
    rule = _find_rule(model, normalize=True)
    return rule.thinking_off_max_effort if rule else None


def supported_effort(model: str | None, effort: str | None) -> str | None:
    """Return the reasoning effort to send *model* for *effort*.

    *effort* itself where the model's levels are not declared or include it;
    otherwise the nearest declared level below it, and ``None`` - send no
    effort - when there is none, as on a model that takes no effort at all.
    """
    if not effort:
        return None
    rule = _find_rule(model, normalize=True)
    if rule is None or rule.effort_levels is None or effort in rule.effort_levels:
        return effort
    if effort not in EFFORT_LEVELS:
        return None
    lower = EFFORT_LEVELS[: EFFORT_LEVELS.index(effort)]
    return next(
        (level for level in reversed(lower) if level in rule.effort_levels), None
    )


def takes_no_effort(model: str | None) -> bool:
    """Return True when *model* is declared to refuse every reasoning effort."""
    rule = _find_rule(model, normalize=True)
    return rule is not None and rule.effort_levels == ()


def refuses_effort_with_tools(model: str | None) -> bool:
    """Return True when *model* refuses a reasoning effort on a request with tools."""
    rule = _find_rule(model, normalize=True)
    return rule.effort_refused_with_tools if rule else False


def refuses_effort_none(model: str | None) -> bool:
    """Return True when *model* refuses ``reasoning_effort: "none"``."""
    rule = _find_rule(model, normalize=True)
    return rule.refuses_effort_none if rule else False


def uses_responses_api_with_tools(model: str | None) -> bool:
    """Return True when a request of *model* with function tools goes to ``/v1/responses``."""
    rule = _find_rule(model, normalize=True)
    return rule.responses_api_with_tools if rule else False


def lowest_effort(model: str | None) -> str | None:
    """Return the lowest effort level *model* declares, ``None`` when it declares none."""
    rule = _find_rule(model, normalize=True)
    if rule is None or not rule.effort_levels:
        return None
    return next((level for level in EFFORT_LEVELS if level in rule.effort_levels), None)


def capped_effort(effort: str, ceiling: str | None) -> str:
    """Return *effort*, lowered to *ceiling* when it is a higher level."""
    if (
        ceiling is None
        or effort not in EFFORT_LEVELS
        or EFFORT_LEVELS.index(effort) <= EFFORT_LEVELS.index(ceiling)
    ):
        return effort
    return ceiling


def requires_summarized_thinking_opt_in(model: str | None) -> bool:
    """Return True when *model* streams its thinking text only on request."""
    rule = _find_rule(model, normalize=True)
    return rule.summarized_thinking_opt_in if rule else False


def supports_in_place_system_messages(model: str | None) -> bool:
    """Return True when *model* accepts a ``system`` message inside ``messages``."""
    rule = _find_rule(model, normalize=True)
    return rule.in_place_system_messages if rule else False


def refuses_forced_tool_choice(model: str | None) -> bool:
    """Return True when *model* refuses a ``tool_choice`` that forces a tool call."""
    rule = _find_rule(model, normalize=True)
    return rule.refuses_forced_tool_choice if rule else False


def is_claude_model(model: str | None) -> bool:
    """Return True when *model* is an Anthropic Claude model, whatever route
    names it (``claude-...``, Bedrock ``us.anthropic.claude-...``, a gateway's
    ``anthropic/claude-...``)."""
    return model is not None and _normalize_family_key(model).startswith("claude-")


def is_gemini_model(model: str | None) -> bool:
    """Return True when *model* is a Google Gemini model, whatever route names it."""
    return model is not None and _normalize_family_key(model).startswith("gemini-")


def supports_bedrock_structured_output(model: str | None) -> bool:
    """Return False when Amazon Bedrock serves *model* without structured outputs."""
    rule = _find_rule(model, normalize=True)
    return not (rule and rule.no_bedrock_structured_output)


def decline_fallback_models(model: str | None) -> tuple[str, ...]:
    """Return the models to ask, in order, when *model* declines a request."""
    rule = _find_rule(model, normalize=True)
    return rule.decline_fallback_models if rule else ()


def thinks_by_default(model: str | None) -> bool:
    """Return True when *model* runs adaptive thinking on a request that sends
    no ``thinking`` config (Claude Sonnet 5, Opus 5 / 5.5, Fable 5 / 5.1,
    Mythos), so every tool round it answers carries thinking blocks.

    Narrower than :func:`defaults_to_adaptive_thinking`, which also covers
    Opus 4.7 / 4.8: those accept adaptive thinking but only think when asked.
    """
    return requires_summarized_thinking_opt_in(model)


def build_openai_generation_params(
    model: str | None,
    *,
    temperature: float | None,
    max_tokens: int,
) -> dict[str, float | int]:
    """Build OpenAI-compatible generation parameters from static rules.

    Matching is deliberately asymmetric:
    * output-token name is resolved from the raw model id, so gateway-prefixed ids
      such as ``openai/gpt-5-mini`` keep ``max_tokens``;
    * temperature is resolved from the normalized id, so gateway-prefixed Claude and
      GPT-5 routes still omit unsupported temperature values instead of hitting a
      provider 400.
    """
    output_rule = model_parameter_rule(model)
    temperature_rule = _find_rule(model, normalize=True)
    params: dict[str, float | int] = {
        (output_rule.output_tokens if output_rule else DEFAULT_OUTPUT_TOKENS): max_tokens,
    }
    if temperature is not None and not (
        temperature_rule and temperature_rule.omit_temperature
    ):
        # Claude takes 0..1 where agents allow up to 2; the direct Anthropic
        # provider clamps the same way (_normalize_temperature).
        params["temperature"] = (
            min(max(temperature, 0.0), 1.0) if is_claude_model(model) else temperature
        )
    return params
