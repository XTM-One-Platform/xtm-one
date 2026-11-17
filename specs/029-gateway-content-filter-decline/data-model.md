# Data Model

No stored data. One in-flight value.

## Model decline (`ModelDeclinedError`, existing)

| Field | From | Rule |
|---|---|---|
| `model` | the request's model | as today |
| `category` | `stop_details.category` | non-empty string, else `None` |
| `explanation` | `stop_details.explanation` | non-empty string, else `None` |
| `usage` | the call's usage | `{}` when none was reported |

`failover_allowed` is true only for `cyber` / `frontier_llm`, so a `None` category never fails over (FR-003).

## Finish handling (state per call)

`finish_reason` | result
---|---
`content_filter` | raise the decline; any partial text is dropped
`tool_calls`, `length`, `stop`, other | unchanged

## Test matrix (FR → case)

Run for `OpenAICompatibleProvider` and `OpenAIProvider` (the tool-stream column is the one shared consumer, run through both).

| Case | Non-stream | `chat_stream` | `consume_openai_stream_with_tools` |
|---|---|---|---|
| category + explanation | FR-001, FR-002 | FR-001, FR-002 | FR-001, FR-002 |
| bare `content_filter` | FR-003 | FR-003 | FR-003 |
| `stop` unchanged | FR-005 | FR-005 | FR-005 |
