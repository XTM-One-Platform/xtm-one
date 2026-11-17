# Contracts

No new endpoint. Three existing contracts change, additively.

## 1. `GET|PUT /api/v1/settings/general-assistant`

All three new fields live on this one contract, not on `/settings/platform` —
that endpoint handles branding and platform parameters and ignores model fields
entirely. `GeneralAssistantConfigRequest` already carries the platform-wide
model settings (`agent_default_*`, `embedding_*`, `image_*`, `routing_model`,
`fallback_model`), so the references join it:

- `llm_provider_id`, beside `llm_provider` / `llm_model`
- `routing_provider_id`, beside `routing_model`
- `fallback_provider_id`, beside `fallback_model`

**Null semantics** (as implemented, keyed on `model_fields_set`): omitting a
field leaves the stored value untouched; sending it as `null` clears it. That
distinction is how a client unpins a provider — an omitted field cannot.

`llm_provider_id` is env-locked with `llm_provider` / `llm_model`, and each
routing/fallback reference with its model (`settings.py:242,254,256`), so a PUT
naming a locked field is refused.

## 2. `GET|POST|PUT /api/v1/settings/llm-providers` — `LLMProviderResponse`

Adds computed, read-only `model_overlaps` (data-model.md). Empty list when no
model id is advertised by more than one config.

## Unchanged

`get_available_models` already returns `provider_id` per entry
(`router.py:2116`); `get_llm_for_user` already accepts `provider_id_override`
(`router.py:1150`). Neither signature changes.
