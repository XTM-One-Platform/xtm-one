# Contract: Settings API changes

## `GET /settings/available-decision-models`

Shape unchanged (`list[AvailableModelItem]`, 027). Content: for each provider the caller may use, the models its `list_decision_models()` reports. An OpenAI config contributes the models of its model listing that the probe recognises ([probe](openai-decision-probe.md)), whatever its card enables; a Filigran AI Gateway or custom OpenAI-compatible config contributes the models its listing types as `decision` ([gateway](gateway-decisions.md)); Ollama's listing is unchanged. A provider the caller may not use is not built and not contacted (FR-007).

## `GET /settings/llm-providers` (and every `LLMProviderResponse`)

Unchanged. Its `capabilities` holds `decision` for a decision provider; the Decision Model section offers such a provider for a typed model id when it lists no decision model, and an OpenAI config leaves that field once its check lists one (FR-009). A decision model already set is shown, and can be changed or cleared, whatever the list holds (FR-015).

## `PUT /settings/general-assistant`

Unchanged: `decision_model` with `decision_provider_id`; refused with 422 when the provider serves no decision models (FR-010).
