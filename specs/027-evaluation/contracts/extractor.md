# Contract: Output extractor

> **Frozen design record of the 1780 delivery (2026-10-30). Not maintained**: the code has moved on since. The current contract is `dev-docs/reference/evaluations.md`; the design is `dev-docs/evaluations.md`.

The declarative chain that turns an agent's run into the prediction quality metrics score (FR-045, FR-046). The code is `backend/app/services/evaluation/extraction/`.

## Definition

- Stored on `evaluation_agents.output_extractor` (JSON). There is at most one per (evaluation, agent).
- NULL means the raw final answer is scored.
- Every level is strict: an unknown key is refused.

```
{ "source":      { "kind": "agent_output" | "tool_output" | "tool_input", "tool_name"?: str },
  "operations":  [ { "op": str, "path"?: [str] } ],     // 1 to extraction_max_operations
  "output_kind": "object" | "array" | "label_set" }
```

| Source | Reads | `tool_name` |
|---|---|---|
| `agent_output` | the final answer | refused |
| `tool_output`, `tool_input` | the output or input of the **last successful** call to `tool_name`, in timeline order. Success means `outcome` is `success`, `succeeded` or `ok`; a missing outcome is not success. | required, non-blank, ≤ 128 chars |

| Operation | Accepts → produces | `path` |
|---|---|---|
| `find_json` | text → text: the first fence holding JSON, else the outermost object or array; never repaired | |
| `parse_json` | text → object or array | |
| `select_path` | object → whatever is at the path | required |
| `object_keys` | object → array | |
| `object_values` | object → array | |
| `collect_keys` | array → array | required |
| `flatten` | array → array | |
| `dedupe_sort` | array → label_set | |

**Static validation**, on create, update and add agent (422):

- The source yields text. The chain folds kinds operation by operation, and the last kind must equal `output_kind`.
- `path` is a non-empty list of non-empty strings, at most `extraction_max_path_segments` long.
- Family support:

  | Family | Allowed `output_kind` |
  |---|---|
  | `CLASSIFICATION` | `label_set` |
  | `STRUCTURED_JSON` | `object`, `array` |
  | any other family, or none | refused ("not supported in this version") |

A family change that would orphan an extractor is refused (FR-021).

## Limits

Settings in `backend/app/core/config.py`, read on every call, each a positive int:

| Setting | Default |
|---|---|
| `extraction_max_operations` | 8 |
| `extraction_max_path_segments` | 16 |
| `extraction_max_source_bytes` | 1 MiB |
| `extraction_max_sample_bytes` | 256 KiB |
| `extraction_max_parse_depth` | 32 |
| `extraction_max_values` | 2,000 |
| `extraction_max_value_chars` | 200 |

## Pipeline (`pipeline.py`)

1. Check that `output_kind` still fits the family.
2. Resolve the source.
3. Apply the operations.
4. Canonicalise the result:
   - an object or array becomes JSON with sorted keys and compact separators;
   - a `label_set` is validated, de-duplicated and sorted by code point.
5. Call `run_quality` with the canonical string. The expected answer is never transformed (FR-045).

## Failure (FR-047)

| Reason | Raised when |
|---|---|
| `source_not_present` | the agent gave no answer, or no successful call to the tool was recorded, or that call recorded no input or output |
| `payload_unparseable` | `find_json` found no object or array, or `parse_json` met invalid JSON, a non-container, or nesting deeper than the limit |
| `selection_empty` | a path matched nothing |
| `type_mismatch` | an operation met a kind it does not accept |
| `limit_exceeded` | the source, the value count or a value's length passed a limit |
| `label_invalid` | a `label_set` member is not a usable label |
| `family_incompatible` | `output_kind` no longer fits the evaluation's family |

On failure, `run_quality` is not called and `metric_results` holds exactly one entry:

```
{ "extraction.failed": { "status": "invalid", "value": 0,
    "error": { reason, message, agent_id, operation_index, operation, path, expected_kind, actual_kind } } }
```

- `message` never contains payload content.
- An `invalid` result writes no `evaluation_metrics` row, so the entry contributes nothing to quality.
- The run detail returns `extraction_failure_count`.

## Success (FR-048)

`execution_details.extracted_prediction = {prediction: <canonical string>, output_kind, item_count | null}`. A rescore whose extraction fails, or that has no extractor, removes it.

## Preview

- `POST /evaluations/{id}/extractors/preview`. It is read-only and runs the same validation as a save.

  ```
  request:  { agent_id, extractor, result_id | sample: { agent_output?, traces? } }   // exactly one of result_id, sample
  response: { status: "extracted", prediction, output_kind, item_count }
          | { status: "failed", prediction: null, output_kind, failure: { reason, message, operation_index, operation, path, expected_kind, actual_kind } }
  ```

  - The agent must be linked to the evaluation (404).
  - A `sample` larger than `extraction_max_sample_bytes` is refused (422), its `traces` included.
  - `traces` is a list of stored trace steps, read like a stored result's: a `tool_output` or `tool_input` source takes the last step with `type: "tool"`, its `tool_name` and a successful `outcome`. It is the only way to preview a tool source without a stored result. The extractor card sends a pasted sample for a tool source as one such step, `{type: "tool", tool_name, outcome: "success", tool_output | tool_input}`, and for `agent_output` as `agent_output` (`components/evaluation/extractor-preview.ts`).
- `POST /evaluations/extractors/preview`, for an evaluation still being created: no evaluation, linked agent or stored result exists yet, so the request names the family and the sample is the only material.

  ```
  request:  { family, extractor, sample: { agent_output?, traces? } }
  response: as above
  ```

  - An unknown family is refused (400); the extractor is checked against the family (422) and the sample bounded as above. The extraction is the evaluation route's own.
- `GET /evaluations/{id}/extractors/sample-results?agent_id&limit` returns `[{result_id, created_at, label}]` (never the output). Default `limit` is 20, maximum 100.
