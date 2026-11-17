# Contract: Workspace Knowledge REST API

Prefix `/api/v1/workspaces/{workspace_id}`, JWT auth, `require_agentic_ai_licensed` like the existing workspace routes.

- **Read**: `get_visible_workspace`, which answers 404 when the workspace is not visible.
- **Write**: `get_manageable_entity(..., Workspace)`, which answers 404 when the workspace is not visible and 403 below the config tier (FR-023).
- **Validation**: bodies validate in their Pydantic models, so an invalid input is a 422.
- **Errors**: details are generic; the precise reason goes to the logs only.
- **Commit**: every write commits before responding and writes an audit entry.

## Workspace (changed)

`PUT /api/v1/workspaces/{id}`: `WorkspaceUpdate.instructions: str | None`, `max_length=8000`.

`WorkspaceResponse` gains `instructions: str`.

## Sources (PR1)

The Sources content is written through the existing knowledge-base routes on the Sources KB (`/api/v1/knowledge-bases/{kb_id}/documents*`, `/sync-sources*`, `/documents/reprocess`), under the workspace's tier (research R2). The workspace adds three routes.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/knowledge` | — | `WorkspaceKnowledgeSummary` |
| POST | `/sources` | — | `{knowledge_base_id}`: the Sources KB, created on first call (config tier) |
| POST | `/sources/from-attachment` | `{file_attachment_id}` | `{knowledge_base_id, document_id}`, 201 |

`WorkspaceKnowledgeSummary` holds:
- `instructions`;
- `can_manage`;
- `sources_knowledge_base_id: str | null`;
- `sources: {count, ready, pending, failed}`;
- `skills: int`;
- `knowledge_bases: int` (bound, visible to the caller);
- `conversations: {indexed, pending}` (PR2).

`from-attachment` checks that:
- the caller may read the file (`file_access.load_readable_file`);
- the file belongs to a conversation filed in this workspace.

## Linked knowledge bases (existing, behaviour change)

`GET/PUT /knowledge-bases` are unchanged in shape. Workspace-owned and internal KBs are refused with 404 per id (`bindable_knowledge_base_clause`, R3).

## Skills (PR1)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/skills` | — | `list[{skill_id, name, description}]`, only the skills the caller can see |
| PUT | `/skills` | `{skill_ids: list[str] (≤ 100)}` (replace) | same list |

- An added id the caller cannot see is refused with 404 for that id.
- A linked skill the caller cannot see is kept, never unlinked by a list that could not name it (same rule as `rebind_workspace_knowledge_bases`).

## Conversation index (PR2)

`GET /knowledge` carries `conversations: {indexed, pending}` (counts only): eligible conversations with a current document, and those waiting.

## Knowledge bases (changed)

- `KnowledgeBaseResponse.managed_by_workspace: {id, name} | null`. `name` is set only when the caller can see the workspace, which is always the case for a non-admin who can see the KB (R1).
- `content_lock` may be `"workspace"`.
- `can_manage` is false for a workspace-owned KB; the workspace's `can_manage` says who writes its content.
- `PUT /{kb}`, `DELETE /{kb}` and the embeddings-dimension delete answer 403 `Managed by workspace '<name>'. Change it from the workspace.` for a workspace-owned KB, to everyone (R2).
- Document and sync-source routes accept it for the workspace's managers and answer 403 to anyone else (R2).

## Settings (admin)

`GET/PUT /api/v1/settings/workspace-knowledge` returns `{sources_context_percent (5-40), conversation_idle_minutes (5-1440)}`. The PUT is admin-only and logs a from→to audit diff, following the observability settings pattern (`api/v1/settings.py:5609`). The second field arrives in PR2.
