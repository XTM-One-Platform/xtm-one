# Feature Specification: Route by provider config, not by model name

**Feature Branch**: `fix/3337-provider-id-routing`

**Created**: 2026-09-07

**Status**: Draft

**Input**: "a model id is not identity, so two providers offering the same model route by coincidence"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A saved model choice keeps routing where it was saved (Priority: P1)

An administrator runs two provider configurations that advertise the same model id — a Filigran gateway fronting Anthropic beside a direct Anthropic key, or a staging key beside production. They pick a model for an agent, for the platform default, for routing, for the fallback, for embeddings or for images. Every later call for that choice reaches the provider configuration they picked, and keeps reaching it after another configuration is added, recreated or reordered.

**Why this priority**: this is the defect. Today the served configuration is the newest-created row advertising the id, so credentials, billing, the audit trail and whether a governed route's policy applies at all change under an unrelated admin action.

**Independent Test**: create two configurations advertising one model id, save a choice against the older one, create a third configuration advertising the same id, and confirm the call still goes to the saved one.

**Acceptance Scenarios**:

1. **Given** two configurations advertise `claude-sonnet-4-6` and a choice was saved against the older one, **When** a call is made for that choice, **Then** the older configuration's credentials are used.
2. **Given** a saved choice, **When** a newer configuration advertising the same id is created, **Then** the call target does not change.
3. **Given** a choice saved before this feature (no configuration reference stored), **When** a call is made, **Then** it resolves exactly as it does today.
4. **Given** a saved reference to a configuration that has since been deleted, **When** a call is made, **Then** resolution falls back to the stored provider name and the dangling reference is logged.

---

### User Story 2 - The picker can express which provider you meant (Priority: P2)

With one model id advertised by two providers, each model picker offers one option per offering provider, each naming its provider, and the closed picker reads back the single provider that was chosen.

**Why this priority**: without it US1 is unreachable through the UI — the picker cannot express the distinction it now persists. Also fixes the visible symptom: duplicate React keys make the trigger read `claude-sonnet-4-6 (Anthropic)claude-sonnet-4-6 (Filigran AI Gateway)`.

**Independent Test**: with two configurations advertising one id, open each picker and confirm two distinctly-labelled options and a single-provider read-back.

**Acceptance Scenarios**:

1. **Given** two configurations advertise one model id, **When** a picker is opened, **Then** it lists one option per provider, each labelled with its provider name.
2. **Given** an option was chosen, **When** the picker is closed, **Then** the trigger shows that one option only.

---

### User Story 3 - Overlap is visible when it is created (Priority: P3)

Saving a provider configuration whose advertised models overlap another configuration's warns the administrator and names the other configuration.

**Why this priority**: turns a silently ambiguous deployment into a known one. Save still succeeds — overlap is legitimate.

**Independent Test**: save a configuration advertising a model id another configuration already advertises and confirm the warning names the other configuration.

**Acceptance Scenarios**:

1. **Given** a configuration advertises `claude-sonnet-4-6`, **When** another is saved advertising the same id, **Then** the save succeeds and warns, naming the first configuration and the overlapping ids.

---

### Edge Cases

- A stored configuration reference no longer exists → US1 scenario 4.
- A stored reference exists but no longer advertises the stored model id (models refreshed) → the model owner wins, because calling a config that does not offer the model would 404 (research.md D9, data-model.md I2).
- A model id typed by hand rather than picked, or reached by prefix inference before a refresh → no reference is stored, and today's resolution applies (US1 scenario 3).
- An environment-locked field → unchanged; the environment value keeps precedence and the field stays read-only.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Saving a model choice MUST persist the provider configuration reference under which that choice was offered, on all seven administered surfaces: agent, General Assistant chat, platform default, routing, fallback, embeddings, images.
- **FR-002**: The General Assistant's chat model MUST have somewhere to store that reference.
- **FR-003**: The routing model and the fallback model MUST each have somewhere to store that reference.
- **FR-004**: A saved choice MUST record the configuration the administrator selected, not a configuration re-derived from the model id at save time.
- **FR-005**: Resolution MUST use the stored configuration reference when one is present.
- **FR-006**: Resolution MUST behave exactly as it does today when no reference is stored, so existing agents and settings keep working without migration of their values.
- **FR-007**: A stored reference that no longer resolves MUST fall back to the stored provider name and log the dangling reference, matching the behaviour already established for the agent-level override.
- **FR-008**: Each of the seven administered model pickers named in FR-001 MUST offer one option per provider advertising a model id, and MUST render exactly one option as the selected value.
- **FR-009**: Each option MUST name the provider configuration it belongs to, distinguishably when one provider has two configurations.
- **FR-010**: Saving a provider configuration whose advertised models overlap another's MUST warn in the AI Models settings surface, naming the other configuration, and MUST NOT block the save.

### Key Entities

- **Provider configuration**: one credentialed way to reach a provider; several may exist per provider and several may advertise one model id. Identity is the row, not the provider name.
- **Model offering**: a model id as advertised by one provider configuration. Two offerings can share a model id.
- **Saved model choice**: a persisted model id plus, from now on, the offering's provider configuration reference.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With two configurations advertising one model id, 100% of calls for a saved choice reach the configuration that was chosen, across configuration creation, deletion and recreation of the other.
- **SC-002**: An administrator can tell which provider a saved model choice uses by reading the picker, without consulting configuration creation order.
- **SC-003**: Deployments with no duplicated model id see no change in which provider serves any existing choice.
- **SC-004**: An administrator who creates an overlapping configuration learns of the overlap at save time rather than from a mis-billed or mis-governed call.

## Assumptions

- Four of the seven surfaces already store a reference — agent, platform default, embeddings, images. FR-002 and FR-003 cover
  the three that do not; FR-001 governs all seven, because none of the pickers persists the admin's actual selection today.
- Overlap between configurations is legitimate and stays supported; the fix disambiguates it rather than forbidding it.
- FR-004 governs a choice made in a picker. A direct API payload that sends a provider name with no id keeps today's
  save-time pin to the newest config of that name (research.md D8); it is not a selection, so it is not FR-004's subject.
- Choices saved before this feature keep their current resolution rather than being backfilled — a backfill would have to guess, which is the defect.
- Namespacing the gateway's model ids is out of scope: a name distinguishes kinds of route, only a row reference distinguishes instances.
- Per-user model preference (`preferred_llm_model`) is out of scope; it is a user convenience, not an administered route.
- The existing model-id prefix table (`router.py:426`) stays as the no-reference fallback and is not extended. It maps names to provider *kinds*, so it does not disambiguate two configurations of one kind — OpenRouter (`anthropic/…`) and Bedrock (`anthropic.…`) miss each other by a separator coincidence, and two OpenRouter configurations still collide. No provider is special-cased by this feature.
