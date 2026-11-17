# Contract: LLM judge

> **Frozen design record of the 1780 delivery (2026-10-30). Not maintained**: the code has moved on since. The current contract is `dev-docs/reference/evaluations.md`; the design is `dev-docs/evaluations.md`.

What the judge receives, the verdict it must return, and how a bad verdict is handled. The code is `score_result` and its helpers in `backend/app/services/evaluation_engine.py`.

## When it runs

| Case | Judge call | Result | Trace step |
|---|---|---|---|
| judge selected (`scorer_selection` NULL, or a list containing `"judge"`) | yes | verdict below | `judge` |
| judge off (`scorer_selection` without `"judge"`, e.g. `[]`) | no | `score` 0, sub-scores 0, reasoning "LLM judge scorer not selected for this evaluation." | `judge_skipped` |
| transient-error anomaly (FR-036) | no | `score` 0, sub-scores 0, reasoning = the anomaly reason | `judge_skipped` |

- A confirmation-seeking entry is judged normally.
- Judge Quality reads N/A for a judge-off run (FR-076).
- `run.scorer_selection` snapshots the selection the run was scored with.

## Call

| Setting | Value | FR |
|---|---|---|
| Model | `evaluations.evaluator_model`, else the platform default, resolved among the evaluation owner's providers | FR-059 |
| System prompt | `evaluator_prompt`, else the content of the company-managed, enabled skill tagged `slug:evaluation-judge-system-prompt` (seed `backend/app/agents/seed_data/skill_content/evaluation-judge-system-prompt.md`); an error when neither exists | FR-059 |
| Temperature | `0.0` | FR-059 |
| `max_tokens` | `evaluations.judge_max_tokens` (default 8192, range 256–131,072); judge-only replay uses the request's value (default 2048) | FR-059, FR-103 |
| Verdict enforcement | native structured output when the provider supports it, else the schema described at the end of the system prompt. A provider that rejects `structured_output` with a `TypeError` gets one retry without it, with the prompt-side schema. | FR-060 |

## User prompt

Sections in this order, separated by blank lines:

| Section | Content | Present when |
|---|---|---|
| `## INPUT` | the entry's input prompt | always |
| `## AGENT OUTPUT` | the agent's final answer, not the extracted one | always |
| `## EXPECTED ANSWER` | the entry's expected answer | always |
| `## AVAILABLE TOOLS` | comma-separated names, or "None (no tools were available to the agent)" | `available_tools` is not None |
| `## TOOL CALLS MADE` | one `- **name** (Nms): description` per call, or "None (the agent did not use any tools)" | `tool_calls` is not None |
| `## EVALUATION INSTRUCTIONS` | the entry's `evaluation_prompt`, else the evaluation's `evaluation_instructions`, else the dataset's | any of them is set |

`available_tools` depends on the path:

| Path | `available_tools` |
|---|---|
| fresh Full Loop or `skip_llm` entry | the agent's tools, stored in `execution_details.available_tools` |
| fresh LLM Only entry | `[]`, stored the same way |
| reused entry, rescore, judge-only replay | the list stored with the result (the source's, for a reused entry); `[]` for an LLM Only result stored without one; else None |
| resumed callback | None |

Departures from FR-057 are listed under the spec's [Known gaps](../spec.md#known-gaps).

## Verdict schema

```
{ "sub_scores": { "tool_selection_quality": int 0–100,
                  "response_completeness":  int 0–100,
                  "response_accuracy":      int 0–100,
                  "instruction_adherence":  int 0–100,
                  "coherence":              int 0–100 },   // all required, no other key
  "reasoning":  string }                                   // both required, no other key
```

`_build_judge_verdict_schema()` builds it from `SUB_SCORE_WEIGHTS`. That one schema drives native enforcement, the prompt-side fallback and parsing.

## Repair and failure (FR-060)

- **Parse**: `validate_and_parse(text, schema, allow_partial=True)` (`backend/app/services/structured_output.py`) takes the verdict out of fences or prose, repairs JSON slips and coerces types.
- **Sub-scores**: each is coerced to an int and clamped to 0–100. A missing or non-numeric one becomes 0 and is listed as missing. A response that is not an object, or whose `sub_scores` is not an object, has every sub-score missing.
- **No tools**: when `available_tools` is `[]`, `tool_selection_quality` is set to 100 and is not missing (FR-059). When it is None (not known), the judge grades it like any other sub-score.
- **Judge failure**: `is_judge_failure` = parse error, OR truncation (`answer_was_cut`), OR any missing sub-score. There is no retry and no substitute score.
  - The reasoning becomes "Judge failure: the judge verdict <causes>. This result is excluded from aggregate scores." followed by a 300-character preview of the raw response.
  - The entry is left out of every headline (FR-075).
- **Judge-only replay**: the result is a failure unless the judge reported `is_judge_failure: false`.

## Score (FR-058)

`score = round(Σ sub_scores[k] × w[k], 2)` over the keys of `w`, where `w` is `evaluations.sub_score_weights`, else 0.20 each. It is stored even for a judge failure.

## Outputs

`score_result` returns:

```
{ score, sub_scores, evaluator_reasoning, is_judge_failure, judge_truncated, judge_stop_reason,
  judge_usage, judge_model, judge_provider, judge_trace_id,
  judge_system_prompt, judge_user_prompt, judge_raw_response, judge_max_tokens }
```

| Stored where | What |
|---|---|
| Result columns | `score`, `sub_scores`, `evaluator_reasoning`, `is_judge_failure` |
| `execution_details.traces` | one `judge` step `{type: "judge", description, duration_ms, model, provider, stop_reason, truncated, is_judge_failure, usage, system_prompt, user_prompt, raw_response}` |
| `evaluation_result_traces` | the judge's LLM trace, with role `judge` |

- Judge time is recorded as `judge_seconds` (FR-072).
- Judge tokens never count in performance metrics (FR-068).
