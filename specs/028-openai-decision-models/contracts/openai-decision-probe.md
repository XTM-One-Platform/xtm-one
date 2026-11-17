# Contract: OpenAI decision model probe

Sent by `OpenAIProvider.list_decision_models()` once per model id of the provider's model listing (`client.models.list()`) without a remembered result ([data model](../data-model.md)), on the provider's own client. A round (the models of one listing that need probing) first sends it for a control id `xtm-probe-{uuid4 hex}` and waits for the answer; nothing is sent when every model is remembered.

## Candidates

`client.models.list()` with `timeout=5`, `max_retries=0`, within 3 seconds and the round's 8-second deadline. A failure or no answer in time: nothing is probed, nothing is listed.

A round runs only when it claims the config's round marker (FR-024, [data model](../data-model.md)); otherwise the load lists what is remembered.

## Request

`POST {base}/decisions`, SDK options `timeout=5`, `max_retries=0`, at most 8 at a time, the whole round within 8 seconds (pending probes are cancelled and unknown):

```json
{
  "model": "<model id>",
  "input": "probe",
  "questions": [
    {
      "type": "choice",
      "name": "label",
      "instructions": "Pick one.",
      "choices": [
        {"value": "a", "description": "a"},
        {"value": "a", "description": "a"}
      ]
    }
  ]
}
```

The duplicate value is deliberate: OpenAI refuses it after recognising the model and before checking credit (spec, Findings).

## Outcome

| OpenAI answer | Result | Remembered |
|---|---|---|
| Error with `code == "model_not_found"` | does not serve decisions | yes, 1 h |
| 400 with `param` starting with `questions` | serves decisions | yes, 24 h |
| 2xx | serves decisions | yes, 24 h |
| 429 | unknown, not listed; the round's other probes are cancelled or not sent, and unknown (FR-018) | no |
| Anything else: 401, 403, 5xx, other 400, timeout, connection error, past the round's deadline | unknown, not listed | no |

The control has 2 seconds. The control id must get `model_not_found` for the round's other probes to be sent. Any other answer for it (the duplicate-value refusal, a 2xx, 401, 403, 429, a timeout), or none by the deadline, ends the round there: nothing else sent, nothing listed, nothing remembered.

Observed bodies (2026-10-16):

- `{"error":{"message":"The model `gpt-4o-mini` does not exist or you do not have access to it.","type":"invalid_request_error","param":null,"code":"model_not_found"}}`
- `{"error":{"message":"Choice values must be unique within the question.","type":"invalid_request_error","param":"questions[0].choices[1].value","code":null}}`
