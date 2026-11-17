# Data Model: Workspace Knowledge

Every model column with a DDL default carries the same `server_default` (parity test, R14).

## workspaces (changed, PR1)

| Column | Type | Rule |
|---|---|---|
| `instructions` | Text, not null, server_default `''` | ≤ 8 000 characters, validated in `WorkspaceUpdate` (R7) |

## knowledge_bases (changed, PR1)

| Column | Type | Rule |
|---|---|---|
| `owner_workspace_id` | UUID, null, FK `workspaces.id` `ON DELETE CASCADE` | Set only on creation by `ensure_workspace_kb`; never updated |
| `workspace_role` | String(20), null | `sources` \| `conversations`; not null iff `owner_workspace_id` is not null (CHECK) |

Indexes: `uq_knowledge_bases_workspace_role` unique on `(owner_workspace_id, workspace_role)` WHERE `owner_workspace_id IS NOT NULL`. Lookups by workspace use it.

Row defaults on creation:
- `user_id` NULL: visibility and the manage right are the workspace's, and an owner would cascade the knowledge base away with that person while a company-managed workspace survives;
- `is_company_managed` false, sharing columns null, `is_internal` false, `is_system_managed` false (that flag means an agent's auto-created collection);
- `name` = `"<workspace name> — Sources"` / `"— Conversations"`, fitted to the column width, not renamed with the workspace (the badge carries the live name);
- chunking defaults of the platform.

Created lazily, under `SELECT … FOR UPDATE` on the workspace row (the `ensure_corpus` pattern, `services/workset.py:397`).

## workspace_skills (new, PR1)

| Column | Type | Rule |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID, FK `workspaces.id` CASCADE, not null | |
| `skill_id` | UUID, FK `agent_skills.id` CASCADE, not null | |
| `created_at` | timestamptz, server_default now() | |

Indexes: unique `(workspace_id, skill_id)`; index on `skill_id`. A link, not ownership: deleting either end removes only the row (FR-032).

## workspace_conversation_indexes (new, PR2)

| Column | Type | Rule |
|---|---|---|
| `conversation_id` | UUID PK, FK `conversations.id` CASCADE | |
| `workspace_id` | UUID, FK `workspaces.id` CASCADE, not null | Workspace the document was written into |
| `knowledge_document_id` | UUID, null, FK `knowledge_documents.id` SET NULL | The conversation's one document |
| `indexed_through_sequence` | Integer, not null | `MAX(messages.sequence)` covered |
| `status` | String(20), not null | `indexed` \| `failed` \| `skipped` (empty conversation) |
| `error` | Text, null | Logged reason, never shown verbatim to other members |
| `indexed_at` | timestamptz, not null | |

Index on `workspace_id`. A row whose `workspace_id` differs from the conversation's current one is re-indexed into the new workspace and its old document deleted (R9).

## knowledge_documents (unchanged schema)

Conversations-KB documents set `extra_metadata = {"conversation_id": "<uuid>"}` and `source_type = "conversation"`. Sources-KB documents keep the source types of the KB routes (R4).

## platform_settings (changed)

| Column | PR | Type | Default | Bounds |
|---|---|---|---|---|
| `workspace_sources_context_percent` | 1 | Integer, not null | 15 | 5–40 (R6) |
| `workspace_conversation_idle_minutes` | 2 | Integer, not null | 30 | 5–1440 (R9) |

## State transitions

**Source document**: `pending → processing → ready | error`. Uses the KB document lifecycle; `error` is retried from the workspace (FR-007 edge case).

**Conversation index**:

| From | To | Trigger |
|---|---|---|
| (none) | `indexed` | Idle and eligible |
| (none) | `skipped` | Idle and eligible, but no user content |
| `indexed` | `indexed` | New messages, then idle again (document replaced) |
| any | (row and document deleted) | No longer eligible in its workspace, by the sweep. The search filter excludes it before the sweep runs. |
| any | `failed` | Job error. Retried on the next tick after a back-off. |
