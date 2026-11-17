# Quickstart: Decision Models on OpenAI and the Filigran AI Gateway

## Automated

```bash
cd backend && CUSTOM_TOOL_SANDBOX=auto SECRET_KEY=test-secret-key-at-least-32-bytes-long uv run pytest tests/test_openai_decision_models.py tests/test_gateway_decision_models.py tests/test_decision_model_settings.py tests/test_settings_llm_providers.py tests/test_llm_decisions.py tests/test_no_workarounds.py
```

Expected: all pass. They cover the probe outcomes of [the probe contract](contracts/openai-decision-probe.md), the Redis memo ([data model](data-model.md)), the picker listing OpenAI models and skipping a restricted provider ([settings API](contracts/settings-api.md)).

## Live (an OpenAI key with Decisions API access)

1. Settings → AI Models: add an OpenAI provider whose account can use the Decisions API; leave its card's chat models as they are.
2. Decision Model → Configure: `gpt-6-luna` on that provider is offered, and no chat-only model such as `gpt-4o-mini` (US1-1); the provider card still offers chat models only (US1-7). Pick it, save, reload: it is still set (US1-2).
3. With the `app.llm.openai_provider` logger at DEBUG, reload the page again within the hour: no probe round is logged for the provider (SC-004).
4. Replace the provider's key with one whose account has no Decisions access and reload: the DEBUG log shows either one round stopped at its control or one round finding no decision model, the section offers OpenAI for a typed model id (US1-3), and still shows `gpt-6-luna` as the set decision model, which Configure can clear (US1-6).
5. Restrict the provider to a group the admin is not in, sign in as a non-admin outside it: the provider is neither offered nor contacted (US3-1).
6. With `gpt-6-luna` set, ask Ariane in an embedded OpenCTI chat to open a report: the run's trace shows `page_decision` spans on `gpt-6-luna` (US1, Independent Test).
7. Check the OpenAI usage dashboard: steps 2–4 add no usage (SC-003).

## Live (a Filigran AI Gateway with a decision model declared)

1. In the gateway, declare a decision model (`[models] "tev1:0.8b" = "decision"`, upstream Ollama) and allow it for XTM One's caller.
2. Settings → AI Models: add the gateway as a **Filigran AI Gateway** provider. Decision Model → Configure: `tev1:0.8b` on the gateway is offered, and no chat model of the gateway (US4-1).
3. Set it and run a page action: the gateway's call log shows `POST /v1/decisions` (US4-2). `tev1:0.8b` names no provider, so its upstream comes from the gateway's declaration, or from a credential pinned to it (`<url>||ollama`), which adds `X-Target-Provider: ollama` to every call of that provider config: add a second gateway provider for decisions only rather than pinning the one that serves chat.
4. Add the same gateway as a **custom OpenAI-compatible** provider (`token||<gateway>/v1`): its decision models are offered under that provider, and a page action on one reaches the gateway's `POST /v1/decisions` without `X-Target-Provider` (US4-3).

Steps 3 and 4 need the gateway prerequisites of [the gateway contract](contracts/gateway-decisions.md); until then the calls leave XTM One as described and the gateway refuses them.
