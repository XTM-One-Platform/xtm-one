# Functional Schema

Schema: the orchestration tables (`orchestration_executions`, etc.) are created
by the consolidated Alembic baseline
(`backend/alembic/versions/d046bcdf345f_baseline_schema.py`). The earlier
per-feature migrations were squashed into that single baseline.

## `orchestration_executions`

One row per orchestrated job instance.

| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid PK | |
| `adapter` | str(50) | Discriminator: `assignment` / `evaluation` / `sub_agent`. |
| `state` | str(20) | `pending` / `running` / `completed` / `failed` / `dead_letter` / `cancelled` (CHECK). |
| `adapter_state` | str(50) null | Adapter sub-state (e.g. `awaiting_human`, `awaiting_callback`). |
| `payload` | JSONB | Adapter data (`run_id`, `evaluation_id`, `trigger_reason`, `file_ids`, …). |
| `run_at` | timestamptz | Earliest eligible time. Replaces `delayed_run:*` + SAQ scheduled set. |
| `deadline_at` | timestamptz null | Hard timeout; past it only `dead_letter`/`cancelled` are legal. |
| `lease_owner` | str(100) null | Reconciler worker id holding the row. |
| `lease_expires_at` | timestamptz null | Lease validity; heartbeated at `lease/3`. |
| `attempt_count` / `max_attempts` | int | Retry bookkeeping (CHECK ≥0 / ≥1). |
| `last_error` | text null | |
| `dedup_key` | str(255) unique null | Idempotent creation (e.g. `assignment_run:<run_id>`). |
| `created_at` / `updated_at` | timestamptz | |

Indexes: partial `(adapter, run_at) WHERE state='pending'` (hot claim), `(state)`,
partial `(lease_expires_at) WHERE state='running'` (reclaim), `(deadline_at)`,
unique `(dedup_key)`.

## `orchestration_outbox_events`

At-least-once events the reconciler drives (resume-from-callback, future
chaining).

| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid PK | |
| `execution_id` | uuid FK → executions (CASCADE) | |
| `event_type` | str(50) | e.g. `resume`. |
| `dedup_key` | str(255) | |
| `payload` | JSONB | |
| `status` | str(20) | `pending` / `processed` / `dead_letter` (CHECK). |
| `available_at` | timestamptz | Backoff / visibility gate. |
| `attempts` / `max_attempts` | int | |
| `last_error` | text null | |
| `created_at` / `processed_at` | timestamptz | |

Unique `(execution_id, event_type, dedup_key)`; partial `(available_at) WHERE
status='pending'`.
