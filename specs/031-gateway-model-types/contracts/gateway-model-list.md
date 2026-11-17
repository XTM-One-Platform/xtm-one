# Contract: the gateway's model list

`GET {base}/v1/models`, the call every picker already makes. Entry fields read:

| Field | Type | Use |
|---|---|---|
| `id` | text | The model id. |
| `owned_by` | text, optional | The upstream provider; `gateway` when absent. |
| `type` | text, optional | The primary type (`chat` if the model has it, else the first). |
| `types` | list of text, optional | Every type, in declared order. |

## Resolving a model's types (FR-001, FR-003, FR-004)

| Entry | Types |
|---|---|
| `types` holds at least one non-empty text entry | Those entries, duplicates dropped, order kept |
| `types` absent, not a list, empty or without a non-empty text entry; `type` is non-empty text | `[type]` |
| Neither | `[classification of id]` (never `decision`) |

A `type` that is not non-empty text counts as absent, so the model is classified by its id.

## What each picker lists (FR-002, FR-005, FR-006)

| Picker | Provider method | A model is listed when |
|---|---|---|
| Chat | `list_models`, `list_models_grouped`, `list_models_and_offered` (chat part) | `chat` in types |
| Embedding | `list_embedding_models` | `embedding` in types |
| Image | `list_image_models` | `image` in types |
| Video | `list_video_models` | `video` in types, or the platform classes the id as video |
| Speech | `list_speech_models` | `speech` in types, or the platform classes the id as speech |
| Decision | `list_decision_models` | `decision` in types |

`list_models_and_offered` still returns every id for its second list. Lists are sorted, as today.

A custom OpenAI-compatible provider reads the same two fields of its raw `/models` entries for its decision listing only (FR-007), through the same function (`declared_model_types`). It has no classification fallback, since classification never yields `decision`.

## Examples

| Entry | Chat | Embedding | Decision |
|---|---|---|---|
| `type: chat`, no `types` | yes | no | no |
| `type: chat`, `types: [chat, decision]` | yes | no | yes |
| `type: decision`, no `types` | no | no | yes |
| `type: chat`, `types: [chat, embedding]` | yes | yes | no |
| `types: [rerank]` | no | no | no |
| no `type`, no `types`, id `text-embedding-3-small` | no | yes | no |

A `types: [video]` model is listed by the video picker only, a `types: [speech]` model by the speech picker only.
