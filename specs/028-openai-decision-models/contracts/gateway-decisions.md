# Contract: Decision models through the Filigran AI Gateway

## Listing

`GET {base}/v1/models` (the call every other section makes). A model is a decision model exactly when its entry's `types` include `decision`, or, with no `types`, its `type` is `decision` (FR-021; spec 031). An entry declaring neither is never one: XTM One's fallback classifier for older gateways yields `chat`, `embedding`, `image`, `video`, `speech` or `music`, never `decision`.

| Provider kind | Reads | Lists |
|---|---|---|
| `filigran_gateway` | `_models_with_meta()` (id, `owned_by`, `types`) | ids whose types include `decision`, sorted |
| `custom_openai` | `_list_model_entries()` (raw entries) | ids whose entry's `types` (else `type`) include `decision`, sorted |

A failed listing raises; the settings listing logs it and the provider contributes nothing to that load. Nothing is remembered: the listing is the answer.

## Decision

Both provider kinds: `POST {base}/v1/decisions` (their SDK base ends in `/v1`, the path is `/decisions`), OpenAI Decisions body (`build_choice_request(DECISIONS_OPENAI, ...)`), answer read by `parse_choice_response(DECISIONS_OPENAI, ...)`; timeouts and refusals as for every decision (`DECISION_TIMEOUT_SECONDS`, `DecisionResponseError`).

| Provider kind | `X-Target-Provider` |
|---|---|
| `filigran_gateway` | the model's `owned_by` from the listing (`_target_headers`), omitted when it reads `gateway`, when the gateway does not list the model (remembered for the instance, so not fetched again) or when the listing fails (the decision is still sent); an upstream pinned in the credential (`<url>\|\|<provider>`) overrides it on every request of that config, chat included (`_GatewayAuth.auth_flow` sets it last), so it belongs on a config used only for decisions |
| `custom_openai` | none |

## Gateway prerequisites (outside XTM One)

- `POST /v1/decisions` in OpenAI's format: the gateway routes `POST /v1/systemone` today.
- A decision with no `X-Target-Provider` resolved from the gateway's own decision model declaration: today `select_provider` falls back to the model id, which names no provider for decision ids, and the call is refused.
