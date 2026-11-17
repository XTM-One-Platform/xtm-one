# Data Model: Decision Models on OpenAI and the Filigran AI Gateway

No database change. The decision model setting is unchanged (`PlatformSettings.decision_model` / `decision_provider_id`, 027).

## Decision model check (Redis)

| Field | Value |
|---|---|
| Key | `llm:decision-model:{config_id}:{fingerprint}:{model}` |
| `config_id` | `LLMProviderConfig.id` (`provider.provider_config_id`) |
| `fingerprint` | `log_once.fingerprint(api_key, base_url)`, 12 hex |
| `model` | the model id checked, as OpenAI's model listing reports it |
| Value | `1` serves decisions, `0` does not |
| TTL | 86 400 s for `1`, 3 600 s for `0` (FR-005) |

- Written only for a definite answer of a round whose control id was refused as unknown (R1); an unknown outcome, or a round stopped at its control, writes nothing.
- A changed key or endpoint yields a new fingerprint, so older entries are never read again and expire.
- Round marker: `llm:decision-model-round:{config_id}:{fingerprint}`, `1`, set with `NX` for 10 s and deleted when the round ends (FR-024).
- OpenAI only: the gateway and custom OpenAI-compatible providers read the type their listing states and remember nothing (R9).

## Provider response (`LLMProviderResponse`)

Unchanged: its `capabilities` holds `decision` for a decision provider (R5).
