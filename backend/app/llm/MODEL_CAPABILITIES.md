# Model capabilities

Runtime parameter rules live in `model_capabilities.py`.
They currently control:

- `max_tokens` vs `max_completion_tokens`
- whether `temperature` is sent
- the Anthropic thinking defaults (below)
- the reasoning effort levels a model accepts: Anthropic `output_config.effort`
  and the `reasoning_effort` of OpenAI, SpaceXAI and Mistral (below)
- whether an Anthropic model takes a `system` message inside `messages`
  (`in_place_system_messages`, `supports_in_place_system_messages()`)
- the Claude Sonnet 5.5 request limits (forced `tool_choice`, Bedrock
  structured outputs, below)
- the models a declining Claude model hands a request to
  (`decline_fallback_models`, below)

The first matching rule wins. Add exact model prefixes before broad family
prefixes when a model differs from its family (`claude-sonnet-5-5` before
`claude-sonnet-5`, `claude-opus-5-5` before `claude-opus-5`). Matching is
intentionally asymmetric: output-token names use the raw model id so
gateway-prefixed ids keep `max_tokens`, while temperature checks use the
normalized id so a gateway-prefixed Claude/GPT-5 model still omits unsupported
temperature values. The normalized id of a Claude model writes its version with
dashes, so OpenRouter's `anthropic/claude-opus-4.7` takes the `claude-opus-4-7`
rule. A Claude model that keeps its temperature gets it clamped to 0–1, the
range Claude accepts, where agents allow up to 2.

## Check a model

Run from `backend/`:

```bash
python scripts/check_model_capabilities.py \
  --model openrouter:openai/gpt-5-mini \
  --json
```

Check several models by repeating `--model`:

```bash
python scripts/check_model_capabilities.py \
  --model openrouter:openai/gpt-5-mini \
  --model openrouter:openai/gpt-4o \
  --model openrouter:google/gemini-3.8-flash \
  --json
```

The script is read-only. It reports the parameters published by the provider;
update `MODEL_PARAMETER_RULES` in code only when a provider's behavior changes.
Add `--check` to fail when the published parameters contradict the static
rules; the checker follows paginated provider catalogs. For an OpenRouter
model it also fails, naming the model and the parameters produced, when the
rules send none of the output-token names in `OUTPUT_TOKEN_PARAMETERS` (derived
from the rules, so a rule with a new name is covered without editing the
script).

OpenRouter's catalog is public. Direct Anthropic and Gemini checks require a
credential:

```bash
ANTHROPIC_API_KEY=... python scripts/check_model_capabilities.py \
  --model anthropic:claude-sonnet-5

GEMINI_API_KEY=... python scripts/check_model_capabilities.py \
  --model gemini:gemini-3.8-flash
```

Use the provider's own endpoint for Ollama, Azure, Bedrock, LiteLLM, Mistral,
and custom OpenAI-compatible deployments. Gateway metadata may differ from the
underlying provider.

Three thinking flags ride on the same rules and are applied in
`anthropic_provider.py`: `adaptive_thinking` (the model thinks by default, so
`disable_thinking` sends `thinking: {"type": "disabled"}`),
`thinking_always_on` (the model refuses that form, so `disable_thinking`
sends `thinking: {"type": "adaptive"}` with the lowest `output_config.effort`
from the first request; see
`dev-docs/reference/backend-llm-thinking-and-caching.md` > "Turning thinking off for
utility calls") and `summarized_thinking_opt_in` (the model hides its thinking text by
default, so a streaming call with a reasoning consumer asks for
`display: "summarized"`; see `dev-docs/reference/backend-llm-thinking-and-caching.md` >
"Streaming reasoning text").
Claude Mythos, Opus 5.5 and Fable 5 / 5.1 set all three. Two more shape the
thinking-off request: `between_tools_thinking_off` (Sonnet 5.5, whose lowest
setting is `thinking: {"type": "between_tools"}`) and `thinking_off_max_effort`
(the highest effort that form is accepted at: `high` for Sonnet 5.5 and
Opus 5). A rule that sets `between_tools_thinking_off` must also set
`adaptive_thinking` and `thinking_off_max_effort`, and not
`thinking_always_on`
(`test_every_between_tools_model_takes_the_thinking_off_rule`). A rule that
sets `thinking_always_on` must also
set `adaptive_thinking`, the only path that reads it, and `omit_temperature`
(`test_every_always_on_thinking_model_takes_the_thinking_off_rule`). A rule
that sets `summarized_thinking_opt_in` must also set `omit_temperature`: the
API refuses `temperature` next to an explicit `thinking` config
(`test_every_display_opt_in_model_omits_temperature`). The same field answers
`thinks_by_default()` - the model thinks on a request that sends no `thinking`
config - which gates the thinking replay (`dev-docs/reference/backend-llm-thinking-and-caching.md` > "Replaying
thinking blocks within a tool round"). The replayed blocks are the provider's own: its
overflow check counts them and sheds what would not fit, and they are
deliberately not registered with the agent loop's provider-extras hook, so no
capability flag here changes what the loop's context gates measure.

`effort_levels` lists the effort values a model accepts (`EFFORT_LEVELS`:
`low` to `max`): Claude's `output_config.effort`, and the `reasoning_effort`
the OpenAI-compatible providers send. A level the model does not take is a
400, which the router answers by failing over, so `supported_effort()` lowers
the requested level to the nearest one the model lists (`xhigh` -> `high` on
Opus / Sonnet 4.6, `max` -> `high` on Opus 4.5) and drops it on a model that
takes no effort at all (empty tuple: Sonnet / Haiku 4.5 and older). `None` - a
rule that does not declare its levels, or no rule - sends the requested level
unchanged.

On the OpenAI-compatible providers the effort goes through
`openai_compat.normalize_openai_reasoning_effort()`, which applies the same
rule (`takes_no_effort()` for the empty tuple) and turns a remaining `max`
into `xhigh` outside `MAX_EFFORT_NATIVE_PREFIXES` (gpt-6, gpt-5.6). A value
outside `EFFORT_LEVELS` (a proxy client's `minimal` or `none`) passes through.
The LLM proxy's `/v1/chat/completions` applies it to a client's
`reasoning_effort` too (`supported_effort()` on its Anthropic bridge): a
platform level the model refuses is lowered or dropped, not answered with a 400.

`effort_refused_with_tools` marks a model whose chat completions refuse any
effort but `none` next to function tools (gpt-6: only `/v1/responses` takes
both). With tools, `normalize_openai_reasoning_effort(..., has_tools=True)`
sends it `none` in place of any requested level, or of none at all (the
model's default effort is refused too), on the providers and the LLM proxy
pass-through alike.

`refuses_effort_none` marks a model that answers `reasoning_effort: "none"`
with HTTP 400 `unsupported_value` (gpt-6-astra, with or without tools). A
caller's `none` or `minimal` is sent as the model's lowest declared level
(`lowest_effort()`); no effort set still sends none at all. A 400 that names
`reasoning_effort` all the same (a model the table does not know yet, or
whose values moved), or the AI Gateway's 502 carrying such a provider 400
(XTM-One-Platform/ai-gateway#348), is retried once on the same leg by
`openai_compat.create_with_effort_retry()`: with the value the error lists
nearest below the one sent, `none` when the error asks for it, else without
the parameter, logged once per model and value at warning. A failover leg
never inherits the primary's effort (`_FALLBACK_EXCLUDED_KWARGS` in the
router): it maps its own.

`responses_api_with_tools` marks a model that has no valid chat-completions
request with function tools: its tools refusal asks for `reasoning_effort:
"none"` and it refuses `none` (gpt-6-astra). The native OpenAI provider
(`OpenAIProvider.chat` and `chat_stream_with_tools`) sends such a request,
with its tools, to the Responses API (`/v1/responses`, module
`openai_responses`) with `reasoning.effort` set to the level
`normalize_openai_reasoning_effort()` maps, the model's lowest declared level
(`low`) for a caller's `none`, `minimal` or no effort. Streaming (text and
reasoning summary deltas), tool calls, usage (in the chat keys, cached and
reasoning tokens included), structured output (`text.format`) and the
`content_filter` decline are mapped as on the chat path; a request without
tools stays on chat completions. Everywhere else (an OpenAI-compatible
provider, a model the table does not flag yet), the two refusals in a row
end the leg on one HTTP 400, code `function_tools_unsupported`, message
"function tools unsupported on chat completions for <model>", marked
`retryable: false`: two calls at most, never the raw second 400.

Models sent through the Responses API with tools, on the native OpenAI
provider: `gpt-6-astra` (dated ids included).

`gpt-6-astra` also sets `omit_temperature`: it answers `temperature` with
HTTP 400 "Unsupported parameter" on chat completions and the Responses API
alike, so neither request carries it (`build_openai_generation_params`,
`openai_responses.build_responses_params`). The other `gpt-6` models set it
too: they refuse any `temperature` but the default 1 ("Unsupported value"). A 400 that names `temperature`
all the same (a model the table does not flag yet) is retried once on the
same leg without it by `openai_compat.create_without_refused_temperature()`,
on chat completions (inside `create_with_effort_retry()`) and the Responses
API, logged once per model at warning; the gateway's 502 carrying that 400
is retried the same way. A leg that still fails on a refused
parameter is named `HTTP 400 <param> not supported` in the chain error.

Declared today:

| Models | Levels |
|--------|--------|
| gpt-6-astra | `low` to `max`; `none` refused (sent as `low`), tools or not |
| other gpt-6 | `low` to `max`; `none` with tools |
| gpt-5.6 | `low` to `max` |
| gpt-5.2 to gpt-5.5 | `low` to `xhigh` |
| gpt-5, gpt-5.1, o1 / o3 / o4, gpt-oss | `low` to `high` |
| gpt-4.x, gpt-3.5, `chatgpt-`, the gpt-5.x `-chat` snapshots | none |
| grok-4.7, grok-4.6, grok-4.20-multi-agent | `low` to `xhigh` |
| grok-4.5 | `low` to `high` |
| grok-3-mini | `low`, `high` |
| every other `grok-` model | none (it reasons without one) |
| Mistral Small 4, Medium 3.5 | `high` (reasoning on); below it, none (reasoning off) |
| every other Mistral model | none |

Mistral answers a reasoning request with a `content` list of `thinking` and
`text` chunks instead of a string; `openai_compat.openai_content_parts()`
splits it, so the reasoning reaches `on_thinking` and the answer stays clean.

Two more fields are set on Claude Sonnet 5.5 alone.
`refuses_forced_tool_choice` (`refuses_forced_tool_choice()`): the model
refuses a `tool_choice` of type `any` or `tool` with HTTP 400, so the LLM
proxy sends a client's forced tool call as `auto`; the platform's own
requests never force one. `no_bedrock_structured_output`
(`supports_bedrock_structured_output()`): Bedrock serves the model without
structured outputs, so the Bedrock provider sends no `outputConfig` and the
prompt-based schema enforcement carries the schema alone.

`decline_fallback_models` names the models the same provider is asked, in
order, when the model declines a request in a category another model may answer
(`cyber`, `frontier_llm`): Sonnet 5.5 -> Sonnet 5, then Opus 4.8; Sonnet 5,
Opus 5 / 5.5 and Fable 5.x -> Opus 4.8. A model that declares some gets the
failover wrapper even with no other provider configured
(`dev-docs/reference/backend-llm-routing-and-models.md` > "Key Design Decisions").

## Model-name checks outside these rules

A new per-model request parameter goes on the rule. The model-id checks that
live elsewhere today, each for a reason of its own:

- `openai_compat.MAX_EFFORT_NATIVE_PREFIXES` (above): where `max` is sent as is.
- `gemini_provider._build_thinking_config`: Gemini 3 takes `thinking_level`,
  older families `thinking_budget`.
- `openai_provider.embed`: only `text-embedding-3` models take `dimensions`.
- `model_limits.py`: context windows and output caps per model family.
- `router.py`: which provider a bare model id belongs to
  (`_MODEL_PROVIDER_PREFIXES`) and which models cannot chat
  (`_NON_CHAT_MODEL_MARKERS`, the bundled catalog,
  `_LEGACY_COMPLETION_ONLY_MODELS`); `gateway_provider._classify_model` is the
  gateway's own chat / embedding / image fallback.

## Known limitation / future work

`MODEL_PARAMETER_RULES` is a static, hand-maintained prefix list. It will keep
needing a manual update for every new model family (e.g. the next Claude
generation) and can silently go stale between releases. A more future-proof
approach would resolve capabilities dynamically from gateway/provider
metadata instead of code prefixes — OpenRouter, for example, already reports
a `supported_parameters` array per model in its `/models` catalog, which is
exactly what `omit_temperature` / `output_tokens` are trying to hand-encode
here. A follow-up could have the gateway resolve and cache this per model
(falling back to `MODEL_PARAMETER_RULES` for providers with no such
metadata) so new models work without a code change. Not implemented yet —
noting it here so it isn't lost.
