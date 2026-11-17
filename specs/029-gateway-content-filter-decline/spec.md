# Feature Specification: Read the gateway's content_filter refusal as a model decline

**Feature Branch**: `029-gateway-content-filter-decline`

**Created**: 2026-10-15

**Status**: Draft

## Clarifications

### Session 2026-10-15

- Q: Is this in line with the decline failover, and is the main change on the gateway? → A: Yes, it completes it and it is the consumer half; the gateway only emits the refusal signal.
- Q: Does native OpenAI get the same reading, though it has no category? → A: Yes, in `chat` and `chat_stream` too, so no OpenAI-format path returns an empty answer; a bare filter is a decline with no failover.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A refusal through the gateway is a decline (Priority: P1)

A user asks something the model refuses, on a model served through the AI Gateway. Today the refusal arrives as an empty, successful answer. It must be treated as a decline, the way a direct Anthropic refusal is, so the user is told the model declined, not shown a blank reply.

**Why this priority**: the defect itself; every other behaviour (failover, failure causes) depends on it.

**Independent Test**: send a gateway-shaped refusal (`content_filter`, refusal text, category and explanation) to the provider and check a decline carrying that category and explanation is raised, not an empty answer returned.

**Acceptance Scenarios**:

1. **Given** a gateway answer finishing with `content_filter` and a category and explanation, **When** the call completes, **Then** a decline is raised with that category, that explanation and the call's usage.
2. **Given** the same answer delivered as a stream whose final chunk carries the category and explanation, **When** the stream ends, **Then** the same decline is raised.
3. **Given** a normal answer finishing with `stop`, **When** the call completes, **Then** the answer is returned unchanged.

---

### User Story 2 - A declined category fails over like any other (Priority: P2)

An operator relies on category failover (e.g. `cyber`, `frontier_llm` declines retried on another model) and on the recorded failure causes. A gateway decline must feed both, with no gateway-specific handling.

**Why this priority**: the value of reading the decline; P1 alone already stops the empty answer.

**Independent Test**: route a gateway decline of a failover-eligible category through the router and check the next model is tried; route a non-eligible one and check it is not.

**Acceptance Scenarios**:

1. **Given** a gateway decline in a failover-eligible category, **When** the router handles it, **Then** it fails over as an Anthropic decline in that category does.
2. **Given** a gateway decline in any other category, **When** the router handles it, **Then** it does not fail over and the failure is recorded with the category as its cause.

---

### User Story 3 - A bare content filter is a decline with no category (Priority: P3)

A provider's own filter (a Bedrock guardrail, an OpenAI or Mistral content filter) also finishes with `content_filter` but carries no category. It is a decline too, and is never handed to another model.

**Why this priority**: same fix on the same code path; narrower audience.

**Independent Test**: send a `content_filter` answer with no category or explanation and check a decline with neither is raised.

**Acceptance Scenarios**:

1. **Given** an answer finishing with `content_filter` and no category, **When** the call completes (streaming or not), **Then** a decline with no category is raised and no failover happens.

---

### Edge Cases

- A `content_filter` answer carrying partial text: a stream's text already delivered stays delivered, the decline then ends the call, and the partial answer is not added to the conversation.
- A category or explanation that is empty or not text counts as absent.
- A `content_filter` ending on a stream's final chunk after earlier content chunks: the decline still wins.
- The gateway's Bedrock refusal details are not verified live; the category may be absent there and must behave as in Story 3.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: A `content_filter` finish MUST raise a model decline, not return an answer, for non-streaming and streaming calls, including the tool-calling stream.
- **FR-002**: The decline MUST carry the category and explanation the answer names, and the usage the call consumed.
- **FR-003**: A `content_filter` finish naming no category MUST raise a decline with no category; such a decline MUST NOT fail over.
- **FR-004**: A gateway decline MUST be indistinguishable downstream from an Anthropic decline: same failover decision, same recorded failure cause.
- **FR-005**: Every other finish reason MUST behave as today.
- **FR-006**: The behaviour MUST apply to every OpenAI-format provider: the OpenAI-compatible family (gateway, OpenRouter, Mistral, Azure, others) and native OpenAI.

### Key Entities

- **Model decline**: an answer the model or a filter refused; has an optional category, an optional explanation and the usage consumed.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A gateway refusal never reaches the user as an empty successful answer: 100% of the gateway-shaped refusal tests (streaming and not) end in a decline.
- **SC-002**: A failover-eligible gateway decline is answered by the next model in the same cases an Anthropic decline is.
- **SC-003**: Zero change in answers for `stop`, `length` and `tool_calls` finishes.

## Assumptions

- The AI Gateway change that emits these fields is not merged yet; tests use its documented shape.
- Only the gateway (like Anthropic) names a category, so only its declines can fail over; any other provider's `content_filter` is a decline with no category.
- Decline failover (router, `model_capabilities`) needs no change: it acts on any decline, but until now only Anthropic's provider raised one. Its fallback ids are bare, as the gateway lists models.
- No gateway change belongs to this feature; the gateway emits the refusal, this side reads it. Merging before the gateway change is safe: a refusal the gateway does not signal stays as today, and any `content_filter` it already forwards becomes a decline.
- Showing the user a decline message is already handled for Anthropic declines and is reused.
