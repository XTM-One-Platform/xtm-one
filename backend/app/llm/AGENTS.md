# LLM Providers – Developer Guide

One provider class per vendor on its official SDK (no LangChain), the router that picks provider and model and fails over, and shared message, attachment and usage plumbing.

## Where to look

| Need | Read |
|---|---|
| Module layout, providers, credentials, speech / music / video, retries, decision models | `dev-docs/reference/backend-llm-providers.md` |
| Prompt caching, thinking off, reasoning text, thinking replay | `dev-docs/reference/backend-llm-thinking-and-caching.md` |
| Usage dict per provider, pricing, `StreamUsageSink` | `dev-docs/reference/backend-llm-token-usage.md` |
| Message builders, images and SVG, attachment budget, provider extras | `dev-docs/reference/backend-llm-messages-and-attachments.md` |
| Failover, declines, provider access, model resolution, env-seeded providers, router | `dev-docs/reference/backend-llm-routing-and-models.md` |
| Capability flags | `MODEL_CAPABILITIES.md`, `model_capabilities.py` |

## Rules

The full rules for each sub-area live in the corresponding `dev-docs/reference/backend-llm-*.md`. The binding invariants are:

- Every vendor call goes through `retry_llm_call()` with the provider's retryable predicate; a 400 the provider handles gets its own branch, never a wider retry policy.
- Map the API stop signal to `ChatResponse.stop_reason` (streams: `last_stop_reason`) on every path, a cut answer as `"max_tokens"`; compare SDK enums by value.
- Every provider returns its full usage dict (`usage` / `last_usage`): a missing token is never priced. `price_usage()` is the one pricing rule.
- Every streaming method takes `usage_sink` (`usage_sink_of(kwargs)`) and reports into it, restarted per attempt; never estimate usage from text.
- Ask for reasoning text only when `reads_reasoning(on_thinking)`, never merely because a callback is set, never on `chat()`.
- After changing a request shape (`thinking`, `output_config`, a replayed block, a new parameter), run the live smoke suite `backend/tests/live/` (`LIVE_LLM_SMOKE=1`): mocks never prove the API accepts it.
- SVG is rasterized only through `services/svg_raster.py`; an `async def` awaits the async image functions, never the sync ones.
- Every builder resolves attachments through `resolve_payload_files()` (one budget per request); the current user turn's files are never stubbed.
- A model decline raises `ModelDeclinedError`, never an answer (an OpenAI-format `content_filter` finish too, only via `declined_by_filter`); failover targets must be chat-capable (`is_chat_capable_model()`); a size error is a 413 `request_too_large` or a `REQUEST_TOO_LARGE_KEYWORDS` match (`is_request_too_large_error()`) and stops failover, except a provider's own body cap (`body_cap_error_code`: the AI Gateway's).
- Every chat resolution goes through `provider_routing()`; a vendor key comes from `usable_provider_configs()`, never a bare `select(LLMProviderConfig)` or a hand filter on `access_restricted`.
- Services resolve a model through the router (`resolve_provider_for_model()`), never by their own matching.
- A provider that serves no chat declares `only_capability` (`"embedding"`, `"speech"`, `"video"`); `CHATLESS_PROVIDERS` / `EMBEDDING_ONLY_PROVIDERS` derive from it, never list one by name. Speech, video and music resolve through `get_speech_provider()` / `get_video_provider()` / `get_music_provider()`.
- A chat model listing keeps `is_chat_capable_model()` ids only; image, video, speech, music and embedding models are listed by `list_<capability>_models`.
- What a model takes is the provider's to say (`continues_music`); a video model the provider no longer serves is recorded by `video_served.py` and dropped by `video_model_listing`: never a model-name check or a listing filtered by hand.
- An env-pinned video, speech, music or decision model is one `CapabilityRole` in `env_capability_defaults.py`, never a new block in `main.py`.
- **Env-seeded providers**: `env_models.py` is the one source of the env model catalog; read `Settings.provider_models_admin_curated` / `provider_management_env_only`, never the raw flags; mutations pass `ensure_llm_provider_management_allowed()`.

## Adding a new LLM provider

1. `{name}_provider.py` implementing `BaseLLMProvider`; an OpenAI-compatible API subclasses `OpenAICompatibleProvider`.
2. Register it in `PROVIDER_MAP` (`router.py`; `api/v1/settings.py` copies it); embeddings / images: `embed()` / `generate_image()` + the router's fallback lists; a decision endpoint sets `decisions_api`.
3. Frontend: `PROVIDER_DISPLAY_NAMES` (+ `SINGLE_CAPABILITY_PROVIDERS` for a chatless one, `frontend/src/lib/constants.ts`), `allowedProvidersByMode` (`Settings.tsx`).
4. `cd backend && uv add <sdk>==<version>`, then run the live smoke suite.

## Tests

Tripwires: `test_llm_stop_reason_max_tokens.py`, `test_anthropic_raw_event_stream.py`, `test_svg_raster_bounds.py`, `test_anthropic_thinking_replay.py`, `test_llm_provider_config_attribution.py`; usage: `test_streaming_usage_capture.py`, `test_provider_billed_usage.py`; media: `test_video_generation_providers.py`, `test_env_capability_defaults.py`.
