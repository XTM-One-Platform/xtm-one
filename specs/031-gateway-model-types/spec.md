# Feature Specification: Gateway Model Types and Decision Models

**Feature Branch**: `feat/5990-gateway-model-types`

**Created**: 2026-10-19

**Status**: In review

**Input**: User description: "The gateway provider reads the model list's `types` and serves decisions."

The Filigran AI Gateway lets one model id have several types, such as `gpt-6-luna` as chat and decision (shipped). Its model list gains `types`, the model's full set; `type` stays a string, `chat` when the model has it, otherwise the first declared. XTM One read `type` only, so a model typed chat and decision showed only in the chat pickers.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A multi-type model appears in every picker of its types (Priority: P1)

An administrator's gateway declares a model as chat and embedding. The model is offered in the chat pickers and in the embedding picker, not only in the one `type` names.

**Why this priority**: this is the gap, and the only part that needs nothing from the gateway beyond the new field.

**Independent Test**: with the gateway's listing stubbed to carry `types`, list each capability's models and confirm a model appears under every capability among its types, and under no other.

**Acceptance Scenarios**:

1. **Given** a model whose types are chat and embedding, **When** the chat and embedding pickers load, **Then** it is offered in both.
2. **Given** the same model, **When** the image, video and speech pickers load, **Then** it is offered in none.
3. **Given** a model whose types are chat and decision, **When** the chat pickers load, **Then** it is offered there exactly as a chat-only model is.

---

### User Story 2 - Older gateways and unknown types behave as today (Priority: P1)

An administrator's gateway has no `types` yet, or declares a type XTM One does not know. Every picker lists what it listed before this change.

**Why this priority**: the gateway rolls out independently of XTM One, so a regression on either side of the change is a regression for every gateway deployment.

**Independent Test**: stub a listing with `type` only, one with neither field, and one with an unknown type beside known ones; compare each picker's output with the current one.

**Acceptance Scenarios**:

1. **Given** a listing with `type` and no `types`, **When** the pickers load, **Then** each model is offered by that one type.
2. **Given** a listing with neither, **When** the pickers load, **Then** each model is offered by the platform's own classification.
3. **Given** a model whose types include one XTM One does not know, **When** the pickers load, **Then** the unknown type is ignored and the known ones are honoured.

---

### User Story 3 - Pick a gateway decision model that is also a chat model (Priority: P2)

An administrator opens the Decision Model section: the models of the gateway, or of a custom OpenAI-compatible endpoint pointed at it, whose types include decision are offered, a model that is also chat included. They pick one and page actions decide on it as spec 028 describes.

**Why this priority**: since spec 028 the gateway lists its decision models by `type`, which hides a model that is also chat; this closes that.

**Independent Test**: stub the listing with a chat-only model, a decision-only model and a chat and decision model; confirm each provider kind offers exactly the two decision ones.

**Acceptance Scenarios**:

1. **Given** a gateway whose listing types a model as decision, alone or with chat, **When** the Decision Model section loads, **Then** that model is offered with the gateway as provider, and no model without that type.
2. **Given** a model that is also a chat model, **When** it is set as the decision model, **Then** it stays offered in the chat pickers.
3. **Given** a custom OpenAI-compatible provider whose listing types models the same way, **When** the section loads, **Then** it offers the same models by the same rule.
4. **Given** a gateway whose listing types no model as decision, **When** the section loads, **Then** the gateway is offered for a typed model id.

---

### Edge Cases

- `types` is not a list, is empty, repeats a type, or carries a non-text entry: with no non-empty text entry it falls back to `type` (FR-003); repeats and other entries are skipped.
- `types` and `type` disagree: `types` is the set, so `type` plays no part while `types` is non-empty (FR-003).
- `types` holds only types XTM One does not know: the model is offered in no picker, as an unknown `type` is today (FR-004).
- A model's types change between loads: each load offers what the listing says; a decision model already set stays set and shown (028, FR-015).
- A provider the caller may not use contributes nothing to the Decision Model section and is not contacted (028, FR-007).
- The gateway listing fails: it fails as it does today, for every picker.

## Requirements *(mandatory)*

### Functional Requirements

**Reading types**

- **FR-001**: The gateway provider MUST read `types` from the gateway's model list.
- **FR-002**: A gateway model MUST be offered in the picker of every capability among its types: chat, embedding, image, video, speech.
- **FR-003**: A model whose listing carries no `types` list, or none with a non-empty text entry, MUST be typed by its `type`, and by the platform's own classification when it has neither.
- **FR-004**: A type XTM One does not know MUST be ignored, and the model's other types MUST still be honoured.
- **FR-005**: A listing that carries only `type` MUST give every picker the models it gives today.

**Decision models**

- **FR-006**: The Decision Model section MUST offer, for the gateway provider, exactly the models whose types include decision.
- **FR-007**: A custom OpenAI-compatible provider MUST list its decision models by the same rule.
- **FR-008**: Listing a gateway's decision models MUST send it no request beyond reading its model list.
- **FR-009**: A gateway whose listing types no model as decision MUST be offered for a typed model id (028, FR-009).

**Documentation**

- **FR-010**: The administration guide and the decision model feature spec (027) MUST state that the gateway's models are offered in every picker of their types, and which of them are decision models.

### Key Entities

- **Model types**: the set of capabilities the gateway declares for one model id.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Against a listing with multi-type models, every model appears in every picker of its types and in none other, bar the video and speech models the platform classes by id (Assumptions).
- **SC-002**: Against a listing with `type` only, or with neither field, every picker's output is identical to the output before this change.
- **SC-003**: A listing mixing unknown and known types loads every picker without error, with the known types honoured.
- **SC-004**: On a gateway that types a model as chat and decision, an administrator sets it as the decision model in under one minute, without typing a model id.
- **SC-005**: The Decision Model section sends the gateway one request, the model list, and offers exactly the models whose types include decision, for a gateway provider and a custom OpenAI-compatible provider alike.

## Assumptions

- User Stories 1 and 2 need only the field, which the gateway ships.
- Spec 028 (merged) owns the decision itself: OpenAI's Decisions route, the upstream header, the typed model id. Its `type` test for decision models (FR-021) is replaced here by membership in `types`, and its text is revised by replacement.
- The `/v1/decisions` route is a gateway prerequisite 028 states; an open gateway change makes it the only decision route.
- A wire per model (System One or OpenAI Decisions) is not built: 028's one route stands, and the gateway translates for System One providers.
- The decision model setting, its administrator-only rule, the typed model id and provider restrictions are unchanged.
- Name-based classification stays as it is: it still adds video and speech models to their pickers, and never yields a decision model.
- `type` is not dropped from the listing by this feature; the gateway may do so later.
