# Contract: Settings API Changes

## `GET / PUT /api/v1/settings/general-assistant`

Two fields are added to `GeneralAssistantConfigRequest` and `GeneralAssistantConfigResponse`, with the same semantics as `routing_model` / `routing_provider_id`:

| Field | Type | PUT rule |
|---|---|---|
| `decision_model` | `string \| null` | `""` clears both fields. Any other value needs `decision_provider_id` in the same body, or the call gets a 422. |
| `decision_provider_id` | `string (uuid) \| null` | It must name an existing provider config (`_GA_PROVIDER_ID_FIELDS`) whose provider class declares a `decisions_api`, or the call gets a 422. |

Neither field is ever in `env_managed_fields` (FR-004). Writing either one is admin-only, as for the rest of this route.

## `GET /api/v1/settings/available-decision-models`

It returns `list[AvailableModelItem]`, the same shape as `/available-embedding-models`, built by `_models_for_capability(db, "decision", user)` from each provider's `list_decision_models()`: Ollama's decision models, the models of an OpenAI provider's listing that its Decisions API recognises, and the models a Filigran AI Gateway or custom OpenAI-compatible provider types as `decision` (`specs/028-openai-decision-models`). With AI off it returns an empty list.

## Provider deletion impact

`LLMProviderDeletionImpact.decision_model: ProviderDeletionRoute` is added next to `routing_model`. It is listed by the deletion preview, counted in the deletion fingerprint and audit through `DeletionImpact.routes()`, and cleared by `_cleanup_orphaned_model_references`.

## Gateway models in the capability pickers

A Filigran AI Gateway model appears in `/available-embedding-models`, `/available-image-models`, `/available-video-models` and `/available-speech-models` for every type the gateway lists for it, not for its primary type alone (`specs/031-gateway-model-types/contracts/gateway-model-list.md`).
