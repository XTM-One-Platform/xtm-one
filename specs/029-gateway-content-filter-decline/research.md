# Research: gateway content_filter decline

## R1 - How `stop_details` reaches the provider

**Decision**: read it from the choice as a dict or an object.
**Rationale**: probed with openai 2.38: the choice, in a response and in a stream chunk, exposes the unknown `stop_details` field as a plain `dict`. Anthropic's `_declined` uses `getattr`, which returns `None` on a dict, so it cannot be reused unchanged.
**Rejected**: typing the SDK models; the field is not part of the OpenAI schema.

## R2 - One builder or a second copy

**Decision**: move `_declined` to `base.py` as `declined_error`, used by both providers.
**Rationale**: the category/explanation reading is the rule to keep in one place (Principle VIII).
**Rejected**: a parallel helper in `openai_compat.py`; the two would drift.

## R3 - What needs no change

**Decision**: router, `failure_causes` and the proxy stay as they are.
**Rationale**: the router keys failover on `ModelDeclinedError.failover_allowed`, `failure_causes` on the exception type, and the proxy already answers a decline as `content_filter` (`llm_proxy.py`), so the error alone is enough. A decline is not retried: `is_retryable_openai` only matches transient API errors.

## R4 - Where the decline is raised in a stream

**Decision**: after the stream loop, not on the finish chunk.
**Rationale**: usage arrives in a trailing chunk after the finish chunk; raising early would drop it, and a decline is billed. Anthropic's streams raise at the end for the same reason.

## R5 - Native OpenAI

**Decision**: `OpenAIProvider.chat` and `chat_stream` read `content_filter` too.
**Rationale**: `OpenAIProvider` is not a subclass of the compat provider but shares its tool-stream consumer; covering only that path would decline in one mode and return empty in the others. It names no category, so it gains no failover, only the end of the empty answer.

## R6 - Out of scope

OpenAI's native refusal (`message.refusal` with `finish_reason: "stop"`) is not a `content_filter` finish; not covered here.
