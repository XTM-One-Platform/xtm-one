# Contract: refusal answer from the AI Gateway

Inbound shape the provider must read (the gateway's refusal signal, not merged: fixtures use this shape).

Non-streaming, on `choices[0]`:

```json
{
  "finish_reason": "content_filter",
  "message": {"role": "assistant", "content": null, "refusal": "<text>"},
  "stop_details": {"category": "cyber", "explanation": "<text>"}
}
```

Streaming: the final choice chunk carries `finish_reason: "content_filter"` and `stop_details`. `usage` is either on that same chunk or on a trailing chunk with empty `choices` (the OpenAI shape); both are read.

Reading rules:
- `finish_reason == "content_filter"` is the only trigger.
- `stop_details` may be absent, or its fields null or empty: a decline with no category.
- `message.refusal` is not read.

Outbound: `ModelDeclinedError` ([data-model.md](../data-model.md)); no API or UI shape changes.
