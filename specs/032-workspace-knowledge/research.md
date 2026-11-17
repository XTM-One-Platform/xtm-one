# Research: Workspace Knowledge

Decisions for [spec.md](spec.md). Code references are as of 2026-10-24 (`main` at `e0bc09312`).

## R1. Ownership and visibility of the two workspace knowledge bases

**Decision**: `knowledge_bases` gains `owner_workspace_id` (FK `workspaces.id`, `ON DELETE CASCADE`) and `workspace_role` (`sources` | `conversations`), unique per `(owner_workspace_id, workspace_role)`. A row with `owner_workspace_id` set is visible to a non-admin exactly when its workspace is: the non-admin branch of `user_visible_filter` (`api/v1/helpers.py:2922`) becomes `(owner_workspace_id IS NULL AND <tiers>) OR owner_workspace_id IN (SELECT id FROM workspaces WHERE user_visible_filter(Workspace))`. The KB's own sharing columns stay empty and are never read for such a row. `is_internal` stays false so the library lists them (FR-002). The admin branch is unchanged: admins see every workspace, so they see its knowledge bases (`test_internal_kb_visibility.py` keeps passing).

**Rationale**: `assignment_visible_filter` (`helpers.py:3173`) is the precedent, and it deliberately does not mirror its parent's columns because a mirror misses per-user shares and `access_restricted`. A subquery follows every sharing change at once (FR-002) with no sync code.

**Alternatives**: mirroring the workspace's visibility columns onto the KB (rejected, above); `is_internal` (rejected by FR-002).

The same rule must hold in the three other places that re-derive KB visibility: `company_wide_visible_filter` (`helpers.py:3046`), `users.list_user_knowledge_bases` (`api/v1/users.py:868`) and `resource_exposure.entity_audience` (`services/resource_exposure.py:569`). Each reads the one declaration, `KnowledgeBase.visibility_parent`, through `helpers.visibility_parent` / `parent_visible_clause`, rather than spelling it.

## R2. The collection belongs to the workspace, its content to the workspace's managers

**Decision**: a new content-lock reason `workspace`, returned by `knowledge_base_content_lock` (`helpers.py:397`) for a row with `owner_workspace_id`. Two rules follow from it.

- **The collection**: settings, sharing, embeddings and deletion are refused to everyone, admins included, with 403 "Managed by workspace *name*. Change it from the workspace." (`assert_knowledge_base_not_workspace_managed` on `PUT /{kb}`, `DELETE /{kb}` and `DELETE /{kb}/embeddings/dimension/{d}`, and in the agent-builder KB update and delete tools).
- **The content**: documents and sync sources go through the ordinary KB routes. The manage right is the workspace's (`can_manage(user, workspace)`), in `knowledge_base_write_refusal` and `_assert_kb_manageable`. An agent's write grant (no user) is refused, and `content_locked_knowledge_base_ids` strips the knowledge base from every agent write scope.

**Rationale**: reusing the KB routes for content keeps formats, limits, the SSRF guard and the whole sync-source lifecycle (about 400 lines of create, update, trigger and force-resync) identical by construction. A second set of workspace routes would duplicate them. The first design refused all KB routes and wrote through workspace routes; it was dropped for that duplication.

**Known narrower surface**: the agent-builder tools load a knowledge base through `can_manage_entity`, so a workspace's managers pass, and `add_kb_document` writes as the caller. `delete_kb_document` still runs the content gate without a user, so it refuses a workspace's Sources: a conservative refusal, never a leak. People remove a source from the workspace panel.

## R3. Never offered for binding

**Decision**: one clause, `bindable_knowledge_base_clause()` = `owner_workspace_id IS NULL AND is_internal IS FALSE`. It applies in:
- `agent_bindings.validate_personal_agent_bindings` and `validate_company_managed_bindings`;
- `binding_access.refused_additions` (profile defaults, General Assistant KBs, chat selection);
- `flow_access.catalog_sources` / `flow_references._allowed_ids`;
- `workspaces.rebind_workspace_knowledge_bases`.

`KnowledgeBaseResponse` gains `managed_by_workspace: {id, name} | null`. The frontend pickers filter on it; the library shows it.

**Adjacent defect**: `validate_company_managed_bindings` and `refused_additions(company_only=True)` skip `is_internal` today, so a company-managed workset corpus can be bound to a company-managed agent. The clause fixes it. The PR reports it as an unrelated fix.

## R4. One document-ingestion service

**Decision**: the add-document logic inlined in `api/v1/knowledge_bases.py` (text 1796, upload 1859, URL crawl 724) moves into `services/kb_documents.py`, which these routes call. So do `store_knowledge`'s file path (`builtin_tools/kb.py:280`), the workspace "add from a conversation attachment" route and the conversation index (PR2). The service validates, inserts, counts and logs activity. The caller commits, then enqueues. A refused URL fetch answers a generic "Could not fetch that URL.", and the reason goes to the log (a behaviour change on the KB crawl route, which echoed the exception). The crawl now records an activity entry like the text and upload routes.

**Rationale**: constitution VIII; FR-006.

## R5. Resolving a turn's workspace knowledge

**Decision**: `services/workspace_knowledge.py` holds `resolve_turn_workspace_knowledge(db, *, user, workspace_id) -> WorkspaceKnowledge | None`, the one place the decision is made. It returns `None` (fail closed, FR-024/FR-026) for:
- an anonymous or unknown user;
- a workspace the user cannot see (`resolve_workspace_scope` semantics, except that an archived workspace still serves).

Otherwise it returns:
- the instructions;
- the source mode with either the full source texts or the Sources KB id (R6);
- the bound KB ids visible to the user (`visible_bound_knowledge_bases`, `services/workspaces.py:742`, minus workspace-owned rows);
- the linked skill ids;
- the Conversations KB id.

**Where the workspace id comes from**:
- **Web, channel and transfer turns**: the conversation's `workspace_id`, read once at turn start. The read is guarded by `safe_uuid(conversation_id)` and a savepoint, because smoke tests pass `db=object()` with a non-UUID conversation id (`tests/test_agent_chat_smoke.py:654`).
- **Consult**: the parent's resolved id, through a new `session_workspace_id` parameter of `run_agent_chat`. A consulted run has no conversation (`agent_chat.py:4736`).
- **Delegate**: `_execute_sub_agent_task` (`services/sub_agent.py:2918`) reads `task.conversation_id`'s workspace and passes `session_workspace_id`.
- **Flow**: a new `capability.workspace` node (R11).
- **General Assistant**: `run_general_chat` calls the same resolver with its `conversation_id`.

**Turn wiring**: the coordination tools' own lookup at `agent_chat.py:3963` stays separate on purpose. It serves the shared-state, work-item and workset scopes without a visibility re-check (a thread already filed keeps its scope), while the knowledge resolution re-checks the person's visibility on every turn. A conversation filed mid-turn by a tool gets the knowledge from its next turn.

## R6. Sources: full or searched

**Decision**: let `budget = share × effective context window`. The window comes from `resolve_effective_context_window`, as at `tools/agent_loop.py:8785`. `share` is a platform setting, default 15 %, bounds 5–40 %.
- **Full mode**: when the summed `estimate_tokens` (`services/prompt_budget.py:51`) of all `ready` Sources documents fits the budget, they are inlined in one block. The block is wrapped with `content_safety.wrap_untrusted` and folded into `_prompt_quarantined` so the untrusted-content policy applies. It goes before the security anchor.
- **Search mode**: the Sources KB id joins the turn's read-only KB ids, so `search_knowledge` and the document-read tools reach it. Citations use the existing `_render_search_results` headers.
- Never a partial set (FR-008).

**Rationale**:
- The agent loop already reserves 25 % of the window for the system prompt, tools and generation (`agent_loop.py:286-299`). 15 % keeps sources inside that reserve with room for the rest.
- All-or-nothing is predictable: an agent never silently answers from a subset it believes complete.

## R7. Instructions

**Decision**: `workspaces.instructions` is Text, not null, `server_default ''`, at most 8 000 characters, validated in `WorkspaceUpdate` (422, never 500). It is passed through `defuse_secret_markers` and inserted after the attached-prompts and skills block (`agent_chat.py:~2794`; `general_chat.py` after the GA prompt) as section `workspace_instructions`.

**Invariant 16**: this is user-authored content the person asked for, like attached prompts. It is not the platform steering behaviour through prompt text.

## R8. Skills for the turn user

**Decision**: a new table `workspace_skills` (workspace FK CASCADE, skill FK CASCADE, unique pair). `resolve_agent_skills` and `build_skill_tools` (`services/builtin_tools/skills.py:189,506`) accept extra `(skill_ids, access)` groups. Workspace skills resolve under `build_skill_access_context(db, user_id)`, the turn user, while the agent's own skills keep the agent owner's access. The groups merge before `_assign_catalog_names`, so names stay unique and one `load_skill` serves both. Every `load_skill` re-runs the resolution, so an unshared skill stops loading at once. A session replacement of the agent's skills (flows) does not drop the workspace group. Linking validates each id against the manager's own visibility, like `rebind_workspace_knowledge_bases`.

## R9. Conversation indexing

**Eligible conversation**: `workspace_id` set, `is_archived` false, `user_id` not null, and the thread is its owner's own. That means the web-listable clause (`services/channel_continuation.py:127`: web, Slack/Teams DMs) or the `api` channel. Group threads hold other people's messages and are excluded (spec FR-015).

**State**: a table `workspace_conversation_indexes`, one row per conversation:
- `conversation_id` (PK, FK CASCADE), `workspace_id` (FK CASCADE), `knowledge_document_id` (FK SET NULL);
- `indexed_through_sequence`, `indexed_at`, `status`, `error`.

Nothing is written to `conversations`, because any write bumps `updated_at` and reorders the sidebar (`models/chat.py:241`).

**Trigger**: a step of the autonomous tick (`services/autonomous.py:1423`, fleet-wide Redis lock, every minute), gated on `_ai_on`. It selects a bounded batch of eligible conversations whose last message (`MAX(messages.created_at)`, indexed) is older than the idle delay (platform setting, default 30 min, bounds 5–1440). A conversation is picked when it has no index row, or its row is behind `MAX(sequence)`, or its row names another workspace. Each pick enqueues the saq job `index_workspace_conversation_job`, keyed `index-conversation-{id}`. There are no saq crons today (`worker.py:816`); the tick is the house pattern.

**Job**:
1. Take `pg_advisory_xact_lock(hashtext('wsconv:'||id))` and re-check eligibility; if the conversation is no longer eligible, write nothing.
2. Build the transcript from `role in (user, assistant)` rows, skipping hidden wake prompts (`metadata_json.hidden`). Never read `tool_calls_json`, `metadata_json.tool_call_trace` or `role=tool` (FR-017a). Content goes through `redact_known_secrets_for_display` (FR-021).
3. Append attachment text from `metadata_json.attachments` via `extract_attachment_text_async`, each attachment capped.
4. Generate a summary following `generate_conversation_title` (`services/conversation_memory.py:2131`): `get_llm_for_user(db, conv.user_id)`, traced, time-bounded, output redacted. The summary is not charged to quota, like titles and compaction.
5. Write or replace the conversation's one document in the Conversations KB (summary first, then transcript) through `kb_documents`, with `extra_metadata = {conversation_id}`. Delete its embeddings, set it pending, and enqueue processing after commit.

**Removal**: the search filters at query time (R10), so unfiling, moving, archiving, deleting and cascades take effect on the next search whatever path made them (FR-018). Storage follows: the same tick step deletes documents of Conversations KBs whose conversation is no longer eligible in that workspace or has no index row. It also cancels their processing jobs.

**Rationale**: about thirty code paths change filing or delete conversations (REST, platform, builtin tool, archive endpoints, user cascade, agent-chat clear). A query-time filter plus a reconciling sweep covers all of them without a hook in each.

**Spec correction**: the REST "delete" of a conversation archives it (`api/v1/chat.py:2849`). The spec's "deleted" means archived or deleted.

## R10. Conversation search tool

**Decision**: `search_workspace_conversations(query, limit)` is built when the turn has workspace knowledge with a Conversations KB. It calls `make_search_knowledge_fn` (`services/vector.py:673`), extended with a `source_ids` filter that `hybrid_search_embeddings` already accepts (`services/hybrid_search.py:61`). The filter holds the ids of documents whose conversation is currently eligible in this workspace, minus the current conversation (FR-019).

Each result carries the conversation title and a link to `/chat?c=<id>`, built with the base-path helper. The title is user content, so it is escaped for markdown-link label, URL, delimiters and newlines, with a test table per variant. The tool name differs from `search_knowledge` to avoid a clash.

## R11. Flow node

**Decision**: a new capability node `capability.workspace` (`config.workspace_id`). It is resolved with `resolve_workspace_scope` (`services/workspaces.py:285`, fail closed) as the flow's acting user, and passed to wired agent nodes as `session_workspace_id`. Listed in the node catalog while `FLOW_BUILDER` is on.

**Rationale**: the existing workspace references on the shared-state, work-items and workset nodes scope those tools. Overloading them to also inject knowledge would change what those nodes mean.

## R12. Filing from a channel or by asking the agent (FR-027a, FR-027b)

**Decision**:
- **From the web history**: Slack/Teams DMs already appear in the own-scope history (`web_listable_clause`) and REST PATCH files them. Nothing to add.
- **By asking the agent**: a new built-in suite `workspace_filing`, offered on every turn with an identified user and a conversation. It holds three tools: `list_workspaces`, `create_workspace` and `file_this_conversation`. Their handlers are the agent-builder ones (`services/builtin_tools/workspaces.py`: same rules, manage tier, not archived, own conversations only). When the agent already has `agent_builder`, the suite is not added twice.
- **FR-022 consent**: when the target workspace reaches others (`reachesOthers` semantics), the call pauses on the existing approval card: a consent screen on the turn's composite executor (`require_consent`), independent of the agent's approval settings, so the person approves the exact call. *Revised after the security review*: a first version took an `acknowledged_shared` argument, which the model sets and content the agent reads could set. Where no card can be shown, the move tool refuses such a target (`consent_screened`).
- **First channel message**: the first message of a new external thread has no conversation row yet (`channels/router.py:3761`), so filing works from the second message. The tool says so instead of failing silently.

**Rationale**: filing your own conversation is a person's right, not an agent capability someone must configure. Requiring `agent_builder` would hand the whole configuration surface to every agent a person wants to file from.

## R13. Deleting a workspace

**Decision**:
- `remove_workspace` (`services/workspaces.py:717`) calls `purge_workspace_knowledge(db, workspace)` before `db.delete`. That function collects the owned KBs' document ids and calls `cancel_document_jobs_after_commit`. It cancels conversation-index jobs and deletes export-package S3 objects (the KB delete route leaves those today). It then deletes the KBs; documents, embeddings, sync sources, activity and export rows cascade (`models/knowledge_base.py:186,314,351,462`, `models/embedding.py:97`). The counts go to the audit entry.
- The FK is `CASCADE`, not `RESTRICT` like worksets: KB children all cascade in the database, so a user deletion cascading through `workspaces` leaves no orphan. Only its saq jobs are not cancelled; the document job already ends on a missing document.
- An in-flight sync writing into a deleted KB fails on its FK and writes nothing.

## R14. Migrations and tests that guard the shape

- Two migrations: PR1 (`workspaces.instructions`, `knowledge_bases.owner_workspace_id/workspace_role`, `workspace_skills`, platform settings) and PR2 (`workspace_conversation_indexes`, idle-delay setting). Revision ids carry the stack base `w6533`, re-pointed on `main`'s head at implementation (head today `m6239manifest01`). Each is idempotent, uses `terminate_lock_holders` and `set_lock_timeout` (`core/migration_ddl.py`), and has a working downgrade.
- `knowledge_bases` joins `PARITY_TABLES` in `tests/test_workspaces_postgres.py:636`. The new FKs to `workspaces` are checked there automatically.
- `test_visibility_helpers.py` (union and shared-group tables) and `test_internal_kb_visibility.py` (admin SQL unchanged) must stay green. A new compiled-SQL test pins the non-admin workspace clause.
