# Phase 1 Data Model

## Schema changes

One migration, three additive nullable columns (research.md D1, D2, D7).

| Table | Column | Type | Qualifies |
|---|---|---|---|
| `general_assistant_config` | `llm_provider_id` | `UUID`, nullable | `llm_model` (FR-002) |
| `platform_settings` | `routing_provider_id` | `UUID`, nullable | `routing_model` (FR-003) |
| `platform_settings` | `fallback_provider_id` | `UUID`, nullable | `fallback_model` (FR-003) |

No foreign key, by D2. No unique constraint on `LLMProviderConfig.provider` is
added — overlap stays legal (FR-010).

### Already present (no change)

`Agent.llm_provider_id` (`models/agent.py:454`),
`PlatformSettings.agent_default_provider_id` / `embedding_provider_id` /
`image_provider_id` (`models/platform_settings.py:52,60,66`).

## Invariants

- **I1**: a reference is null or a `LLMProviderConfig.id` that existed at save
  time. It may dangle later (FR-007).
- **I2**: a reference that advertises the model decides the provider config, and
  outranks the newest-config-first guess even when a differently-named provider
  also advertises the model (FR-005). A reference that does **not** advertise the
  model loses to the model owner, because calling it would 404 — the pre-existing
  auto-correct at `router.py:1428`, which this feature narrows rather than
  removes (research.md D9).
- **I3**: a null reference changes nothing about resolution (FR-006).

## Wire shapes

`AvailableModel` (`frontend/src/types/index.ts:284`) and `get_available_models`
(`router.py:2116`) already carry `provider_id` — unchanged.

`LLMProviderResponse` gains:

```
model_overlaps: list[LLMModelOverlap]  # { model, provider_ids } — D5, FR-010
```

The three settings payloads gain the matching nullable id fields, alongside the
four that already exist.
