# Data Model: Decision Model and Host-Page Snapshot Tool

## Persisted: `platform_settings` (two new columns)

| Column | Type | Null | Meaning |
|---|---|---|---|
| `decision_model` | `String(100)` | yes | Decision model id used by `act_on_page`. NULL means the tool is off (FR-003). |
| `decision_provider_id` | `UUID` | yes | Provider config that owns `decision_model` (FR-002). There is no FK, for the reason given on `fallback_provider_id`. |

Both columns are written only through `PUT /settings/general-assistant` by an admin, and cleared together when their provider is deleted or no longer offers the model (FR-004). Migration: one `ADD COLUMN IF NOT EXISTS` pair, templated on `p1panic0001_add_emergency_stop_token.py`, `down_revision` set to the head at implementation time (`c2cti5696002` today).

## Transient (Redis, per conversation)

| Key | Value | TTL | Written by |
|---|---|---|---|
| `chat:page_action:pending:{conv}` | the waiting `action_id` | 60 s, refreshed while waiting | the tool, before each frame |
| `chat:page_action:{conv}` | list of answer JSON | 60 s | `/chat/messages/approve` |

## In-memory, one tool run

- **Snapshot**: `url`, `title`, `text` (the lines of [snapshot format](contracts/page-action-frame.md#snapshot-text)), the set of refs in `text`, and a hash of `text`. It replaces the previous snapshot after each answer.
- **Options**: `click:eN` / `open:eN` / `type:eN` keys built from the snapshot, at most 16 per question ([decision request](contracts/decision-output.md)).
- **Step**: `action`, `target` (the name the snapshot gives the ref, or the path), `ok`. A navigate step is `ok` only when the page's URL then starts with the path.
- **Outcome**: `finished | unsure | stopped_before_submit | cannot_reach | unverified | budget_reached | failed`, the steps, the final `url`/`title` and `truncated` ([tool result](contracts/act-on-page-tool.md#result)).

## State transitions of a run

```text
start ─► [path?] navigate ─► snapshot ─► reached? ─┬─ done (≥ 0.5) ───────► finished
                                ▲                  ├─ done (< 0.5) ───────► unsure
                                │                  └─ not yet ─► decide ─┬─ impossible ──────► cannot_reach
                                │                                        ├─ key not offered ─► failed
                                │                                        ├─ click, commits ──► stopped_before_submit
                                │                ┌───────────────────────┴─ click|open|type
                                │                └─► wait ───────────────┬─ answer ─► snapshot
                                │                                        └─ timeout ─► failed
                                └──── 8 actions or 240 s deadline reached ────────► budget_reached
```

A repeated action on an unchanged snapshot hash ends as `unverified` when the page performed it the first time, `cannot_reach` otherwise (research R6).
