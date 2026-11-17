# Research: Decision Models on OpenAI and the Filigran AI Gateway

## R1. Recognising an OpenAI decision model

- **Decision**: the candidates are the ids of OpenAI's model listing (`client.models.list()`), as for the other modalities; per candidate, `POST /decisions` with one choice question whose two choices share one value ([probe contract](contracts/openai-decision-probe.md)). An error whose `code` is `model_not_found` → not a decision model. A 400 whose `param` starts with `questions` → decision model. A 2xx answer → decision model. A 429 → unknown, and the round ends (FR-018). Anything else → unknown (FR-004). Each round first probes `xtm-probe-{uuid4 hex}`, a model id that cannot exist, and waits for its answer; only `model_not_found` lets the other candidates be probed (FR-016).
- **Rationale**: OpenAI reports decision support nowhere (`/v1/models` has id, owner, created, shutdown date; the model pages omit Decisions). The check order measured on 2026-10-16 (spec, Findings) makes this request refused after the model check and before billing, so it is free (SC-003). Classifying on the refusal's `param` and `code`, not its message text, survives rewording. The control id turns a change of that undocumented order into an empty list instead of every listed model offered, and makes a key that cannot reach the Decisions API (401, 403, 429) cost one request per load instead of one per model. The provider card's model selection is the chat catalogue and stays out of it (FR-017).
- **Alternatives considered**: a valid minimal decision (billed, ~$0.000003); an empty `questions` list (refused before the model check, no signal); the models enabled on the provider card (mixes a chat-only selection into another modality, and a decision-only model would have to be offered to chat); listing every model unverified (offers models that fail at first use).

## R2. Remembering results

- **Decision**: Redis key `llm:decision-model:{config_id}:{fingerprint(api_key, base_url)}:{model}`, value `1` with TTL 24 h, value `0` with TTL 1 h. Only `True`/`False` are stored. One round at a time per config and scope: `llm:decision-model-round:{config_id}:{scope}`, set with `NX` for the deadline plus 2 s and deleted after the round (FR-024). A Redis error is logged at debug and the probe runs uncached and unclaimed.
- **Rationale**: the fingerprint makes a key or endpoint change start from fresh entries without any invalidation code (FR-005), and a model OpenAI starts reporting has no entry, so only it is probed; Redis shares results across replicas (IV). A "no" lasts an hour because an account whose Decisions access is gated may get `model_not_found` for every model (unmeasured): access granted later then shows within the hour, at the cost of one free round per hour. `log_once.fingerprint` already produces a 12-hex digest for this purpose.
- **Rationale for the round marker**: the route is open to every user; without it, concurrent loads would each ask about every unchecked model on the organisation's key.
- **Alternatives considered**: invalidation hooks on provider update (a second place to keep in step with every edit path); per-instance memory (per replica, and the provider cache lives ~2 min).

## R3. Building the provider for the listing

- **Decision**: `_list_models_by_capability` builds a decision listing's provider with `provider_for_config(cfg)`, and runs it within `DECISION_LISTING_TIMEOUT_SECONDS` (10 s, FR-025); the other capabilities keep their fresh `provider_cls(api_key=...)` instance. Every listing runs concurrently.
- **Rationale**: the memo is keyed by config id (R2), which only an instance built from the config carries (`provider_config_id`). `provider_for_config` is the documented path for a caller that chose the config itself, and gives the listing the same instance (identity, context windows, vault-resolved key) the decision call uses. `list_decision_models()` keeps its signature, so the capability loop stays generic.
- **Alternatives considered**: setting `provider_config_id` on the ad-hoc instance in `settings.py` (re-spells `build_provider`).

## R4. Bounding the listing

- **Decision**: probes for one provider run under `asyncio.Semaphore(8)`, each with `timeout=5` and `max_retries=0` (SDK `RequestOptions`), inside one 8-second deadline that also covers the candidate listing (at most 3 s) and the control (at most 2 s), so the probes keep at least 3 s (FR-006); the client's own read timeout is 600 s, sized for chat. Probes still pending at the deadline, or when the caller is cancelled, are cancelled and unknown, and remember nothing. The control is awaited before the other probes start (FR-016); after it, the first 429 cancels the round's other probes (FR-018), so a rate-limited account costs a handful of requests per load. `_list_models_by_capability` lists its providers concurrently (`asyncio.gather`), so several slow providers still finish within one deadline (SC-008).
- **Rationale**: OpenAI lists on the order of 120 models; at ~200 ms each, 8 at a time, a first load checks them in ~3 s, and an unresponsive OpenAI would otherwise take 120 × 5 s ÷ 8 ≈ 75 s (FR-006). What the deadline cuts off is checked on the next load. Retries would multiply a slow provider's delay, and the next load retries anyway.

## R5. A decision provider that lists nothing

- **Decision**: no change. `LLMProviderResponse.capabilities` (`router.provider_capabilities`) holds `decision` for a provider whose class speaks a decisions API, and the Decision Model section offers every such provider that lists no decision model for a typed model id (`typedProviders` in `Settings.tsx`). OpenAI leaves that field as soon as its check lists a model, and returns to it while the check lists none (FR-009).
- **Rationale**: one backend fact (`decisions_api`) already reaches the frontend; a second field would restate it (XI).

## R6. The decision call on OpenAI

- **Decision**: no change. `build_choice_request` / `parse_choice_response` / `decide_choice` already cover OpenAI's shape, refusal, unknown label and the 255-label cap, tested in `tests/test_llm_decisions.py` (`test_openai_request_shape`, `test_an_openai_answer_is_read`, `test_anything_but_a_decision_among_the_labels_is_refused`, `test_a_request_needs_one_to_255_labels`, `test_openai_sends_the_decisions_request_on_its_own_client`). FR-013 is the base `decision_context_window` default: the card's `context_windows` entry for that model id when the model is also enabled there, else `None`, which `act_on_page` reads as its fixed page size (`MAX_STATE_PAGE_CHARS`).

## R7. OpenAI-compatible subclasses

- **Decision**: no `PROVIDER_MAP` class inherits `OpenAIProvider`, so none inherits its probe. Of the `OpenAICompatibleProvider` subclasses, `FiligranGatewayProvider` and `CustomOpenAIProvider` declare `decisions_api = DECISIONS_OPENAI` (R9); Azure OpenAI, LiteLLM, Mistral and xAI declare none (FR-002). `test_only_openai_openrouter_and_ollama_declare_a_decisions_api` becomes the map of all five decision providers.

## R8. A set decision model stays visible

- **Decision**: no change. `PlatformModelSetting` shows the current choice, with Configure to change or clear it, whenever a model is set, whatever the list holds (FR-015).
- **Rationale**: an OpenAI listing can be empty or partial for a moment (OpenAI unreachable, FR-004; deadline, FR-006), and a setting that vanished then could not be cleared.

## R9. Decision models through the Filigran AI Gateway

- **Decision**: both ways of configuring the gateway speak OpenAI Decisions on its `/v1/decisions` route (clarification 2026-10-19): `FiligranGatewayProvider` and `CustomOpenAIProvider` post `/decisions` on their `.../v1` base. Both list the models whose `/v1/models` entry's `types` (else `type`) include `decision` ([gateway contract](contracts/gateway-decisions.md)): the gateway reads the field it already sets (`_models_with_meta`), the custom provider reads its raw entries (`_list_model_entries`). Neither probes nor remembers: the listing states the type.
- **Rationale**: the gateway declares a decision model in its own configuration and never guesses it from the id, so its listing is authoritative and costs one request the other sections already make. A custom OpenAI-compatible endpoint that is not the gateway reports no type, lists nothing, and falls back to the typed model id; since its class claims decisions, every custom OpenAI-compatible provider is offered for one (FR-002's exception). XTM One's client-side classifier for gateways without types never yields `decision`, as the gateway's own rule says. One format for both ways of configuring the gateway keeps one gateway contract.
- **Alternatives considered**: the OpenAI probe for custom endpoints (relies on OpenAI's undocumented check order, which another server need not share); System One for a Filigran AI Gateway provider (two gateway routes to keep equivalent).
- **Listing failure**: both listings raise, as the gateway's other listings do; the settings listing logs it and the provider contributes nothing to that load.

## R10. Naming the upstream on a gateway decision

- **Decision**: `BaseLLMProvider._decision_headers(model)` returns `{}`; `decide_choice` sends what it returns with the request (`decisions.decide_choice(..., headers=)` → the SDK's `options["headers"]`). `FiligranGatewayProvider` returns `_target_headers(model)`, the `X-Target-Provider` its embedding, image, video and speech calls already send (FR-022).
- **Rationale**: the hook mirrors `_video_headers` / `_speech_headers`; the decision call stays one generic function.
- **Upstream not in the listing**: the gateway infers `owned_by` from the id, so a decision id that names no provider reads `gateway` and `_target_headers` sends nothing. An upstream pinned in the gateway credential (`<url>||ollama`) is set on every request by `_GatewayAuth.auth_flow`, after the per-call headers, so it names the upstream for every request of that provider config, chat, embeddings and images included: the pin belongs on a gateway provider config used only for decisions. Without a pin, the gateway must resolve the upstream from its declarations (spec, Assumptions).
- **Cost**: `_target_headers()` reads the owners through `_models_with_meta()`, one `/v1/models` request per cached provider instance (`provider_for_config`, about two minutes), accepted.

## R11. An unpinned Classify node among several decision providers

- **Decision**: when several usable decision providers exist and none has the model in its chat catalogue, `resolve_decision_provider` asks each one's `list_decision_models()` concurrently, each within `DECISION_LISTING_TIMEOUT_SECONDS`, and takes the only one listing the model; none or several still ask for a pin.
- **Rationale**: every custom OpenAI-compatible config is a decision provider (FR-020), so a platform with one Ollama decision provider and a vLLM endpoint has two candidates where only Ollama serves the model. The listings are asked only in that case, so the single-provider and catalogue paths cost nothing more.
