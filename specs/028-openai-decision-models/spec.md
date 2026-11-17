# Feature Specification: Decision Models on OpenAI and the Filigran AI Gateway

**Feature Branch**: `feat/openai-decision-models`

**Created**: 2026-10-16

**Status**: Draft

**Input**: User description: "OpenAI provider: support OpenAI's Decisions API end to end and list the models that serve it in the Decision Model selector (Settings > AI Models)."

The Decision Model picker (`specs/027-decision-model-page-actions`) offers only the models a provider lists as decision models, and only Ollama lists any. A platform whose only decision provider is OpenAI cannot set a decision model, so its agents cannot act on the page of an embedded chat.

## Clarifications

### Session 2026-10-16

- Q: Is there a default OpenAI decision model? → A: No. OpenAI's Decisions guide names the model in every request and documents no default; the platform carries no model name.
- Q: How is "serves decisions" known, and is it charged? → A: OpenAI reports it nowhere, so each candidate model is asked once with a request OpenAI refuses after recognising the model and before billing it. It costs nothing (measured 2026-10-16, see Findings).

### Session 2026-10-19

- Q: Where do the candidate models come from? → A: From the OpenAI provider's own model listing, like the embedding, image, speech and video selectors. The provider card's model selection stays chat models only and plays no part: decision models are a modality of their own, picked in the Decision Model section.
- Q: Does the Filigran AI Gateway serve decision models, and on which endpoint? → A: Yes. Whether it is configured as a Filigran AI Gateway provider or as a custom OpenAI-compatible provider pointed at it, XTM One calls the OpenAI-standard `/v1/decisions` route, and the gateway takes the decision to its upstream.
- Q: What does the section offer for OpenAI when its check lists nothing? → A: The `Model id` field every decision provider that lists no decision model has: the administrator types the id. Once the check lists OpenAI's decision models, OpenAI is offered in the list instead.

## Findings that shape this feature

- OpenAI's model listing reports, per model, an id, an owner, a creation date and an optional shutdown date: no capability, no supported endpoint, no context window. The model pages do not list the Decisions endpoint either, even for `gpt-6-luna`, the one model the Decisions guide names.
- OpenAI's Decisions endpoint checks a request in this order, measured with curl on 2026-10-16:
  1. the request shape (an empty question list is refused for every model);
  2. the model (a model that does not serve decisions, such as `gpt-4o-mini`, is refused as `model_not_found`);
  3. the question's rules (two choices with the same value are refused);
  4. the account's credit;
  5. the decision itself, the only billed step.
- A choice question with two identical values therefore separates the two kinds of model at no cost: a decision model gets the duplicate-value refusal, any other model `model_not_found`. The order is observed, not documented.
- The Filigran AI Gateway marks each model of its listing with a type, `decision` for a decision model, which its administrator declares (the gateway never guesses it from the id). Its listing's `owned_by` is inferred from the id and reads `gateway` for decision ids that name no provider (`tev1:0.8b`, `clef-flash`). It routes decisions on `POST /v1/systemone` today, to the upstream named in `X-Target-Provider` (Ollama, OpenRouter, TypeSafe), and refuses one whose upstream neither the header nor the id names.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Pick an OpenAI decision model (Priority: P1)

An administrator has an OpenAI provider whose account can use the Decisions API. They open the Decision Model section of Settings > AI Models: `gpt-6-luna` on that OpenAI provider is offered, and OpenAI's other models are not. They pick it and save, without touching the provider card. Agents in embedded chats can now act on the host page, each step decided by OpenAI.

**Why this priority**: this is the gap. Without it OpenAI's Decisions API is unreachable from the platform.

**Independent Test**: on a platform with only an OpenAI provider, open the Decision Model section, confirm it offers only the models OpenAI recognises on its Decisions API, set one, and run a page action whose trace names that model for each decision.

**Acceptance Scenarios**:

1. **Given** an OpenAI provider, **When** the Decision Model section loads, **Then** it offers, with their provider, the models OpenAI's model listing reports that its Decisions API recognises, and none of the others.
2. **Given** that offer, **When** the administrator picks a model and saves, **Then** the setting shows that model and provider after a reload, and every page action decides with exactly that model on exactly that provider.
3. **Given** an OpenAI provider whose account can use no decision model, or that cannot be reached, **When** the section loads, **Then** OpenAI is offered for a typed model id (the `Model id` field) instead of in the list.
4. **Given** no provider that serves decisions, **When** the section loads, **Then** it says so and names the kinds of provider that serve decision models.
5. **Given** a decision model set on OpenAI, **When** the OpenAI provider is deleted, **Then** the deletion impact lists the decision model and the setting is cleared.
6. **Given** a decision model set on OpenAI, **When** OpenAI cannot be checked as the section loads, or stops reporting the model, **Then** the section still shows the model with its provider and lets the administrator change or clear it.
7. **Given** an OpenAI provider, **When** the administrator edits its models on the provider card, **Then** the card offers chat models only, as before, and its selection does not change what the Decision Model section offers.

---

### User Story 2 - Decisions on OpenAI give the same results as on other providers (Priority: P1)

An agent in an embedded chat acts on the host page with an OpenAI decision model. Each step asks one choice question; OpenAI answers with a choice, a probability per option and a confidence, and the tool acts on that choice exactly as it does with an Ollama model.

**Why this priority**: picking the model has no value unless the decision itself works against OpenAI's format.

**Independent Test**: with OpenAI's answers stubbed, run choice questions with a valid answer, a refusal, an answer naming an option not offered, and a question with more options than the format accepts.

**Acceptance Scenarios**:

1. **Given** a choice question, **When** it is sent to OpenAI, **Then** the request carries the content, the instructions and every option with a description, in OpenAI's format.
2. **Given** OpenAI's answer, **When** it names one of the offered options, **Then** that option is the decision, with its probability distribution and confidence.
3. **Given** OpenAI's answer, **When** the model refused, named an option that was not offered, or answered nothing for the question, **Then** no decision is made and the caller is told the model gave no usable answer.
4. **Given** a question with more options than the format accepts, **When** it is built, **Then** it is refused before anything is sent.
5. **Given** OpenAI refuses the request (no credit left, access withdrawn), **When** a page action runs, **Then** the tool reports the failure naming the decision model, and the agent says so instead of guessing.

---

### User Story 3 - Provider access and listing cost (Priority: P2)

On a platform where a provider is restricted, the Decision Model section never offers it and never contacts it. Loading the section repeatedly does not ask OpenAI about the same models again and again.

**Why this priority**: provider restrictions are an access rule a settings page must not bypass, and a page load must not turn into a stream of outbound requests.

**Independent Test**: restrict the OpenAI provider for the caller and load the section; then, unrestricted, load it twice and count the requests OpenAI receives.

**Acceptance Scenarios**:

1. **Given** a provider the caller may not use, **When** the decision models are listed, **Then** that provider contributes nothing and is not contacted.
2. **Given** a model already found to serve decisions or not, **When** the section loads again, **Then** OpenAI is not asked about that model again until the check expires (FR-005) or the provider's key changes.
3. **Given** providers that list decision models themselves (Ollama), **When** the section loads, **Then** their models are offered as before, beside the OpenAI ones.

---

### User Story 4 - Decision models through the Filigran AI Gateway (Priority: P2)

An administrator whose models go through the Filigran AI Gateway sets a decision model the gateway serves. Whether the gateway is configured as a Filigran AI Gateway provider or as a custom OpenAI-compatible provider, its decision models are offered and asked on its OpenAI-standard Decisions route.

**Why this priority**: the gateway is how a Filigran deployment reaches its models; decision models must not need a second, direct provider.

**Independent Test**: with the gateway's listing stubbed to mark one decision model among chat models, confirm the picker offers exactly that model for each of the two provider kinds, and that a decision reaches the Decisions route in OpenAI's format.

**Acceptance Scenarios**:

1. **Given** a Filigran AI Gateway provider whose listing marks a model as a decision model, **When** the Decision Model section loads, **Then** that model is offered with the gateway provider, and no model the listing marks otherwise.
2. **Given** that model set, **When** a page action runs, **Then** each decision goes to the gateway's Decisions route in OpenAI's format, naming the model's upstream when the gateway's listing reports one, as its embedding and image calls do, or the upstream its credential pins.
3. **Given** a custom OpenAI-compatible provider whose listing marks a model as a decision model, **When** the section loads, **Then** that model is offered with that provider, and its decisions go to the endpoint's Decisions route, naming no upstream.
4. **Given** either kind whose listing marks no decision model (an older gateway, an endpoint that reports no model type), **When** the section loads, **Then** the provider is offered for a typed model id.

---

### Edge Cases

- OpenAI cannot be reached or answers with a server error: the models whose check failed are not offered, the failure is not remembered (the next load asks again), and the rest of the list still loads. When the model listing itself fails or does not answer in time, OpenAI contributes nothing to that load (FR-006).
- OpenAI rate-limits the checks: the round stops at the first rate-limit refusal (FR-018), so a rate-limited account sends a handful of requests per load, not one per model; the rest are checked on later loads.
- OpenAI reports more models than can be checked within the deadline (FR-006): the unchecked ones are unknown and checked on the next load, so the first loads may offer fewer models until every model has a remembered result.
- The account has no credit left: the check still works, since it stops before billing, so the model is offered; page actions then fail naming the decision model (User Story 2, scenario 5).
- OpenAI changes its check order so the probe reaches the decision step: the probe is a minimal question and costs a few millionths of a dollar, and a decision answer also means the model serves decisions.
- OpenAI checks the question's rules before the model: the made-up model id is refused for the question's rules instead of as unknown, so the round stops there, listing and remembering nothing (FR-016) instead of offering every listed model.
- The key's account has no access to the Decisions API, or the key is refused: when OpenAI refuses the made-up model id for that reason, each load sends that one request and lists nothing from this provider (FR-016), which is offered for a typed model id instead (FR-009); when it answers every model as unknown instead, each model is found not to serve decisions, which is remembered for one hour (FR-005), so access granted later shows within the hour.
- OpenAI stops reporting a model set as the decision model, or cannot be checked when the section loads: the model stays set and shown, and can be cleared (FR-015); the provider's deletion still clears it.
- An Azure OpenAI, LiteLLM, Mistral or xAI provider is configured: it is not a decision provider, contributes nothing to the list, and saving a decision model on it is refused.
- A custom OpenAI-compatible endpoint that serves no Decisions route (a vLLM server, say) marks no model as a decision model, so it lists none; like every custom OpenAI-compatible provider it is still offered for a typed model id (FR-009, FR-020), and a model typed there fails each page action, naming the decision model (User Story 2, scenario 5).
- A gateway decision model whose id names no provider is listed with `owned_by` `gateway`: XTM One names no upstream unless the gateway credential pins one (`<url>||ollama`), and the decision reaches its upstream only if the gateway resolves it from its own declarations (Assumptions). A pin applies to every call of that provider config, chat included, so it belongs on a gateway provider config used only for decisions.
- The gateway's or the custom endpoint's model listing fails: it contributes nothing to that load, as in the other model sections.
- A gateway older than its model types lists every model without one: nothing is marked as a decision model, so the gateway is offered for a typed model id.
- A decision model already set on a provider that lists decision models is shown and used unchanged.

## Requirements *(mandatory)*

### Functional Requirements

**Which providers serve decisions**

- **FR-001**: The OpenAI provider MUST be a decision provider speaking OpenAI's Decisions API format.
- **FR-002**: A provider type whose service documents no decisions endpoint MUST NOT be a decision provider: Azure OpenAI, LiteLLM, Mistral and xAI stay non-decision providers; adding one is a separate change once its service documents the endpoint. A custom OpenAI-compatible endpoint is the one exception (FR-020): its service is whatever the administrator points it at.
- **FR-019**: The Filigran AI Gateway provider MUST be a decision provider speaking OpenAI's Decisions format.
- **FR-020**: A custom OpenAI-compatible provider MUST be a decision provider speaking OpenAI's Decisions format.

**Listing OpenAI decision models**

- **FR-003**: The OpenAI provider MUST offer as decision models exactly the models its model listing reports that OpenAI recognises on its Decisions endpoint. The platform MUST NOT carry model names to decide this.
- **FR-004**: The check MUST be one request OpenAI refuses after recognising the model and before billing it: a model refused as unknown does not serve decisions, a model refused for the request's own content does. Any other outcome (unreachable, server error, refused key) leaves the model unchecked: it is not offered and not remembered.
- **FR-005**: A check's result MUST be remembered per provider and model, 24 hours when the model serves decisions and 1 hour when it does not, and forgotten when the provider's key or endpoint changes.
- **FR-006**: A provider's decision model listing, the call reading its model listing included, MUST end within 8 seconds, running at most 8 checks at a time; a model not checked by then is unknown (FR-004). The model listing and the made-up model id's check MUST leave the other checks at least 3 seconds.
- **FR-007**: The decision model list MUST include only providers the caller may use; a provider the caller may not use MUST NOT be contacted.
- **FR-016**: Each round of checks MUST first check one model id that cannot exist, and MUST check the round's other models only when that id is refused as unknown.
- **FR-018**: A rate-limit refusal MUST end the round, its other checks cancelled or not sent and unknown.
- **FR-024**: One round of checks MUST run at a time per provider and key; a load meanwhile lists what is remembered.
- **FR-025**: Every provider's decision listing MUST end within 10 seconds, for the Decision Model section and for a flow's Classify node alike.

**Listing gateway and custom OpenAI-compatible decision models**

- **FR-021**: The Filigran AI Gateway and custom OpenAI-compatible providers MUST offer as decision models exactly the models whose declared types include `decision` (the listing's `types`, else its `type`), and none when no model declares it; the platform MUST NOT guess the type from a model id.
- **FR-022**: A decision asked through the Filigran AI Gateway provider MUST name the model's upstream provider when the gateway's listing reports one, the way the gateway's other non-chat calls do; an upstream pinned in the gateway credential names it for every call of that provider config, chat included.

**Decision Model section**

- **FR-008**: The section MUST offer the listed decision models of every decision provider together, each with its provider.
- **FR-009**: A decision provider that lists no decision model MUST be offered for a typed model id, and with no decision provider configured the section MUST say so, naming the kinds that serve decision models.
- **FR-010**: Saving a decision model on a provider that is not a decision provider MUST be refused with a message saying that provider serves no decision models.
- **FR-015**: A decision model that is set MUST be shown with its provider, and can be changed or cleared, whether or not any model is listed.
- **FR-017**: The provider card's model selection MUST stay chat models only and MUST NOT affect which decision models are offered.

**Decisions on OpenAI**

- **FR-011**: A choice question sent to an OpenAI decision model MUST carry the content, the instructions and every option with a description in OpenAI's format, and MUST be refused before sending when it has more options than the format accepts.
- **FR-012**: An OpenAI answer MUST be accepted only when it names one of the offered options; a refusal, an unknown option or a missing answer MUST be reported as no usable answer.
- **FR-013**: The context window used to fit a question to an OpenAI decision model MUST be the per-model window set for that model id on the provider card when there is one (the card sets windows for its chat models only), and unknown otherwise, since OpenAI's API reports none; with an unknown window the page action keeps its default page size (027).

**Documentation**

- **FR-014**: The administration guide's decision model section and the decision model feature spec (027) MUST state which providers offer decision models, how OpenAI's are found and that the check costs nothing, and that both ways of configuring the gateway use its Decisions route.
- **FR-023**: Every message naming the kinds of provider that serve decision models MUST name the Filigran AI Gateway and custom OpenAI-compatible endpoints beside Ollama, OpenRouter and OpenAI.

### Key Entities

- **Decision provider**: a configured provider whose service answers typed decision questions, in one of the wire formats the platform speaks (System One, OpenAI Decisions).
- **Decision model check**: whether one model a provider reports serves decisions; kept per provider and model for a limited time.
- **Decision model setting**: the platform-wide decision model and the provider serving it (027, FR-002).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On a platform whose only decision provider is OpenAI, an administrator sets a decision model in under one minute, without editing the provider card.
- **SC-002**: Once every reported model has been checked, the picker offers every OpenAI model that serves decisions and none that does not, with zero model names written into the platform (the no-workarounds check passes).
- **SC-003**: Listing decision models adds nothing to the OpenAI bill.
- **SC-004**: A second load of the section within one hour, with an unchanged provider key, sends no request to OpenAI about an already-checked model.
- **SC-005**: A provider the caller may not use is contacted zero times while the section loads.
- **SC-006**: Every decision on OpenAI either names an offered option or is reported as no usable answer.
- **SC-007**: Platforms that already pick a listed decision model see no change in how it is picked or used.
- **SC-008**: With OpenAI accepting connections but never answering, the Decision Model section finishes loading within 10 seconds.
- **SC-009**: With a gateway listing that marks one decision model among chat models, the picker offers exactly that model, for a Filigran AI Gateway provider and for a custom OpenAI-compatible provider alike, and a decision on it is sent to `/v1/decisions` in OpenAI's format; that it reaches the upstream is verifiable once the gateway serves the route (Assumptions).

## Assumptions

- The check order measured on 2026-10-16 holds; the edge cases above bound the cost and the listing if it changes.
- OpenAI's model listing includes the models its Decisions API serves (`gpt-6-luna` is listed as a chat model).
- OpenRouter's listing of decision models is out of scope: this feature covers OpenAI and the Filigran AI Gateway.
- Gateway prerequisites, outside this feature: the gateway serves `POST /v1/decisions` in OpenAI's format (it routes `POST /v1/systemone` today), and resolves a declared decision model's upstream itself when no `X-Target-Provider` names it (it refuses such a call today). Until then, a Filigran AI Gateway provider reaches a decision model only with its upstream pinned in the credential, and a custom OpenAI-compatible provider not at all.
- The decision model setting stays administrator-only (027, FR-004).
- OpenAI's choice question accepts at least as many options as a page action sends per step (16).
- A model the account loses access to answers as unknown and stops being offered at the next check; one already set as the decision model stays set until changed.
- An unpinned flow Classify node takes a provider listing the model on its card, else the one the platform's Decision Model setting names for that same model, else the only candidate; with several, the one whose decision listing reports the model while every listing answered in full (FR-025), or with none the only provider that cannot list its decision models (OpenRouter). A whole listing without the model rules its provider out, so a gateway or custom endpoint declaring no decision model is no candidate, as before they served decisions. With none, several, or a failed or partial listing (named in the message) it asks for the provider to be pinned. A found provider is remembered for a minute, anything else for 15 seconds, and concurrent executions share one sweep.
