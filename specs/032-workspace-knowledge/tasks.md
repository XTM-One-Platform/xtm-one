---

description: "Task list for workspace knowledge: sources, skills, instructions and conversation search"
---

# Tasks: Workspace Knowledge — Sources, Skills, Instructions and Conversation Search

**Input**: Design documents from `specs/032-workspace-knowledge/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: included (backend change, constitution VI). Each story's tests are written first and fail before the implementation. A test file that needs PostgreSQL carries `pytestmark = pytest.mark.integration`.

**Organization**: by user story, in the order of the PR stack (plan.md > Delivery), so each PR is a contiguous run of phases:

| PR | Phases | Stories |
|---|---|---|
| PR1 | 1–6 | US1, US2, US5, US6 (all but filing) |
| PR2 | 7–8 | US3, US6 filing |
| PR3 | 9 | US4 |

US6 (P3) is split because its turn wiring lands with PR1's resolver, while its filing needs PR2's notice.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: different file, no dependency on an unfinished task.
- Paths are from the repository root. Backend commands run from `backend/` with `uv run` (`uv run --no-sync` where `backend/.venv` links another checkout).
- Every PR title is `feat(workspaces): …`. Every commit is signed, Conventional, and ends with ``.

---

## Phase 1: Setup (PR1)

**Purpose**: a clean base for the stack.

- [X] T001 Bring the branch up to `main` (`sync_with_base_branch`). Note the current Alembic head with `cd backend && uv run alembic heads`; research R14 recorded `m6239manifest01`.
- [X] T002 Commit the spec artifacts in `specs/032-workspace-knowledge/` as `docs(workspaces): specify workspace knowledge`.

---

## Phase 2: Foundational (PR1, blocks every story)

**Purpose**: the schema, the visibility and lock rules, the ingestion service and the turn resolver that every story reads.

### Tests

- [X] T003 [P] Create `backend/tests/test_workspace_knowledge_visibility.py` (unit, compiled SQL).
  - The non-admin `user_visible_filter(KnowledgeBase, …)` contains `owner_workspace_id IS NULL AND <tiers>` OR `owner_workspace_id IN (SELECT workspaces.id … )`.
  - The admin branch is unchanged (`test_internal_kb_visibility.py` stays as it is).
  - `bindable_knowledge_base_clause()` renders `owner_workspace_id IS NULL AND is_internal IS false`.
  - `knowledge_base_content_lock` returns `"workspace"` for a row with `owner_workspace_id`.
  - Refs: R1, R2, R3.
- [X] T004 [P] Create `backend/tests/test_workspace_knowledge_postgres.py` (integration).
  - A workspace-owned KB is listed by `GET /knowledge-bases` for: the owner, a `read_only` user grant, a group grant, and everyone on a company-managed workspace.
  - It is not listed for a stranger, a user whose share was revoked (in the same test, after `PUT /workspaces/{id}`), or a user excluded by `access_restricted`.
  - `GET /knowledge-bases/{id}` answers 404 for the stranger.
  - Every KB mutation route listed in research R2 answers 409 with the generic "Managed by workspace" detail.
  - Refs: FR-002, FR-003, FR-024, spec edge case on lost access.
- [X] T005 [P] Create `backend/tests/test_kb_documents_service.py`. Text, upload, URL and attachment ingestion through `services/kb_documents.py` produce exactly what the four KB routes produced before:
  - the `KnowledgeDocument` fields;
  - `document_count` + 1;
  - one activity row;
  - one `enqueue_document_processing` call after commit (spy arguments);
  - NUL rejection;
  - the SSRF refusal on a private URL;
  - the ≥ 20-character crawl minimum.
  - Ref: R4.

### Implementation

- [X] T006 Add `instructions` to `Workspace` and a `WorkspaceSkill` model in `backend/app/models/workspace.py`. Add `owner_workspace_id`, `workspace_role`, the CHECK and `uq_knowledge_bases_workspace_role` in `backend/app/models/knowledge_base.py`. Add `workspace_sources_context_percent` in `backend/app/models/platform_settings.py`. Every DDL default gets a `server_default`. Register the new model in `backend/app/models/__init__.py`. Ref: data-model.md.
- [X] T007 Create `backend/alembic/versions/w6533wsknow01_workspace_knowledge.py` with `down_revision` = the head from T001.
  - Contents: the four column groups, the CHECK, the partial unique index, the `workspace_skills` table and its indexes, and the platform setting.
  - Make it idempotent with probes, `terminate_lock_holders` + `set_lock_timeout` from `app.core.migration_ddl`, and a working `downgrade()`.
  - Add `knowledge_bases` to `PARITY_TABLES` in `backend/tests/test_workspaces_postgres.py:636`.
  - Run `test_migration_integrity.py`, then the parity test.
  - Ref: R14.
- [X] T008 In `backend/app/api/v1/helpers.py`:
  - Declare `KnowledgeBase.visibility_parent` (the owning workspace) and apply it through `parent_visible_clause` in the non-admin branch of `user_visible_filter`. Gate on the declaration, never on `hasattr(workspace_id)`.
  - Use the same clause in `company_wide_visible_filter`.
  - Add `bindable_knowledge_base_clause()`.
  - Comment why visibility derives from the workspace rather than mirrored columns (R1, constitution VII).
- [X] T009 [P] Apply the R1 clause in `backend/app/api/v1/users.py:868` (`list_user_knowledge_bases`) and in `backend/app/services/resource_exposure.py:569` (`entity_audience`: a workspace-owned KB's audience is the workspace's). Extend `backend/tests/test_resource_exposure.py`. Refs: FR-002, R1.
- [X] T010 In `backend/app/api/v1/helpers.py`:
  - Generalise `package_origin_knowledge_base_error` and `assert_knowledge_base_not_package_origin` into `managed_knowledge_base_error` and `assert_knowledge_base_not_managed`. They refuse package origin as before and a workspace-owned row with 409 "Managed by workspace <name>. Change it from the workspace."
  - Make `knowledge_base_content_lock` return `"workspace"`.
  - Rename every call site: `api/v1/knowledge_bases.py`, `api/v1/chat.py:3089`, `services/builtin_tools/kb.py:851`, `services/builtin_tools/agent_builder.py:7107,8994,9063,9305`.
  - Makes T003 (lock part) and T004 (403 part) pass. Refs: R2, FR-003, FR-003a.
- [X] T011 [P] Apply `bindable_knowledge_base_clause()` in:
  - `backend/app/services/agent_bindings.py` (`validate_personal_agent_bindings`, `validate_company_managed_bindings`);
  - `backend/app/api/v1/binding_access.py:115` (`refused_additions`, both branches);
  - `backend/app/services/flow_access.py:95`;
  - `backend/app/services/flow_references.py:600`;
  - `backend/app/services/workspaces.py:781` (`rebind_workspace_knowledge_bases`).

  Add cases to `backend/tests/test_workspace_knowledge_visibility.py`:
  - a workspace-owned KB is refused for a personal agent, a company-managed agent, profile defaults, GA KBs, chat selection, a flow capability node and another workspace's bindings;
  - a company-managed workset corpus is refused for a company-managed agent (the adjacent defect, R3).

  Refs: FR-004, R3.
- [X] T012 Create `backend/app/services/kb_documents.py` with `add_text_document`, `add_uploaded_document`, `add_url_document` and `add_attachment_document`.
  - The bodies move out of `backend/app/api/v1/knowledge_bases.py` (724, 1796, 1859) and `backend/app/services/builtin_tools/kb.py:280` (`_store_file_document`).
  - Each function returns the flushed document. The caller commits, then calls `enqueue_document_processing`.
  - The routes and the tool become thin callers.
  - Makes T005 pass. Run `tests/test_knowledge_base*.py` unchanged. Ref: R4.
- [X] T013 In `backend/app/services/workspaces.py` add `ensure_workspace_kb(db, workspace, role)`, following the `services/workset.py:397` lock pattern.
  - It creates the row with the data-model defaults.
  - It returns the existing row on a race.
  - It never sets the KB's sharing columns.
- [X] T014 Add `managed_by_workspace: {id, name} | null` and `content_lock: "workspace"` to `KnowledgeBaseResponse` in `backend/app/api/v1/knowledge_bases.py` (`_kb_to_response`, ~3880). Load the names in one query per list. Ref: contracts/workspace-knowledge-api.md.
- [X] T015 Create `backend/app/services/workspace_knowledge.py`:
  - a `WorkspaceKnowledge` dataclass;
  - `resolve_turn_workspace_knowledge(db, *, user, workspace_id)`, which returns None for an anonymous or unknown user and for an invisible workspace, and keeps an archived workspace serving;
  - `load_conversation_workspace_id(db, conversation_id)`, guarded by `safe_uuid` and a savepoint.

  Fields filled by later stories (sources, skills, conversations KB) start empty here. Refs: R5, FR-024, FR-026.
- [X] T016 Create `backend/tests/test_workspace_knowledge_turn.py` with the resolver cases:
  - anonymous user;
  - non-uuid user;
  - invisible workspace;
  - archived workspace still serving;
  - a `read_only` grantee resolves it;
  - `db=object()` with conversation id `"conv-1"` returns None without touching `db`.

**Checkpoint**: schema, visibility, lock, binding refusal, ingestion and resolver are in place; nothing reaches a turn yet.

---

## Phase 3: User Story 1 - Attach sources the agent uses (P1, PR1) 🎯 MVP

**Goal**: sources added to a workspace ground every turn in its filed conversations (FR-005 to FR-010).

**Independent Test**: [quickstart](quickstart.md) PR1 steps 2–5.

### Tests

- [X] T017 [P] [US1] Create `backend/tests/test_workspace_sources_budget.py`.
  - The budget is `percent × resolve_effective_context_window`.
  - Sources that fit go in full mode, all of them. One token over switches to search mode with the Sources KB id and no inline text. Partial is never chosen.
  - Only `ready` documents count.
  - The full block is wrapped with `wrap_untrusted`, and the quarantine flag is set.
  - Refs: FR-008, FR-009, R6.
- [X] T018 [P] [US1] Extend `backend/tests/test_workspace_knowledge_postgres.py`:
  - `POST /workspaces/{id}/sources` answers 403 for a `read_only` grantee and 404 for a stranger, and creates exactly one Sources KB for owner, admin, `config` and `config_with_chats`, even when called twice;
  - `POST /sources/from-attachment` refuses a file outside a conversation filed in this workspace and a file the caller cannot read (404 for both), and stores the attachment's text for a manager;
  - `GET /knowledge` is readable by a `read_only` grantee, with `can_manage` false;
  - the KB document and sync-source routes follow the workspace's tier (already pinned by T004).

  Refs: FR-005, FR-006, FR-007, FR-023.
- [X] T019 [P] [US1] Extend `backend/tests/test_workspace_knowledge_turn.py`. `run_agent_chat` in a filed conversation:
  - full mode puts the source text in the system prompt (spy on `run_agent_loop`'s messages);
  - search mode adds the Sources KB id to the ids given to `build_kb_search_tool` and `build_kb_document_read_tools` (spy arguments) and never to `writable_kbs`;
  - an unfiled conversation gets neither.

  Refs: US1 scenarios 1–3.

### Implementation

- [X] T020 [US1] In `backend/app/services/workspace_knowledge.py` add `sources_for_turn(db, kb_id, context_window, percent)`. It returns the full texts with titles or the search mode (one aggregate query on `ready` documents' content length, then the texts only in full mode) and `render_full_sources_block(...)`, which wraps each source with `content_safety.wrap_untrusted`. Ref: R6.
- [X] T021 [US1] Add the `WorkspaceKnowledgeSummary`, `WorkspaceSourcesKnowledgeBase` and `WorkspaceSourceFromAttachment` schemas in `backend/app/api/v1/schemas/workspace.py`, per contracts/workspace-knowledge-api.md.
- [X] T022 [US1] Create `backend/app/api/v1/workspace_knowledge.py`, registered in `backend/app/main.py` under `/api/v1/workspaces`, with `GET /{id}/knowledge`, `POST /{id}/sources` and `POST /{id}/sources/from-attachment`.
  - Read routes go through `get_visible_workspace`; write routes through `get_manageable_entity`.
  - `ensure_workspace_kb(…, "sources")` creates the Sources KB on the first write.
  - The attachment route uses `kb_documents.add_file_document` over the attachment's bytes, writes an audit entry, commits and then enqueues.

  Makes T018 pass. Refs: FR-005, FR-007, R2.
- [X] T023 [US1] In `backend/app/services/agent_chat.py`:
  - At turn start, after `user_record` is loaded (~2083), resolve `_workspace_id = session_workspace_id or await load_conversation_workspace_id(...)` and `_ws_knowledge = await resolve_turn_workspace_knowledge(...)`.
  - Add the `session_workspace_id: uuid.UUID | None = None` parameter.
  - Reuse `_workspace_id` in the late lookup at ~3963 instead of a second query.
  - In search mode, append the Sources KB id to `kb_ids` after the session replacement (~2241), so a flow's replacement keeps it, and keep it out of `writable_kbs`.
  - In full mode, insert the block before the untrusted policy and fold it into `_prompt_quarantined` (~3541).
  - Makes T019 pass. Refs: R5, R6.
- [X] T024 [US1] Update `backend/app/api/v1/AGENTS.md` or `backend/app/services/AGENTS.md` (whichever owns KB ingestion) and `dev-docs/reference/integrations-and-knowledge-bases.md`. Cover workspace-owned KBs, the `workspace` lock, the bindable clause and `kb_documents` as the one ingestion path.

**Checkpoint**: US1 works end to end through the API.

---

## Phase 4: User Story 2 - Workspace instructions and skills (P1, PR1)

**Goal**: instructions and linked skills reach every turn in a filed conversation, under the person's visibility (FR-011 to FR-014).

**Independent Test**: [quickstart](quickstart.md) PR1 steps 1, 3, 4, 6.

### Tests

- [X] T025 [P] [US2] Extend `backend/tests/test_workspace_knowledge_postgres.py`:
  - `PUT /workspaces/{id}` with 8 001 characters of instructions answers 422, and with `null` leaves them unchanged;
  - `GET`/`PUT /skills` follow the tier rules of T018;
  - an id the caller cannot see answers 404 for that id;
  - a linked skill the caller cannot see is kept on replace;
  - `GET /skills` lists only visible skills.

  Refs: FR-012, FR-014, FR-023.
- [X] T026 [P] [US2] Extend `backend/tests/test_workspace_knowledge_turn.py`:
  - the instructions appear once in the system prompt, defused of secret markers;
  - a workspace skill visible to the turn user and not to the agent owner is in the catalogue, and `load_skill` loads it;
  - one visible to the owner only is absent;
  - unsharing it makes the next `load_skill` refuse it;
  - with `session_skill_ids` set (flow), workspace skills are still present;
  - bound KBs visible to the user are read-only in `kb_ids`, and invisible ones are absent.

  Refs: FR-011, FR-013, FR-014, R8.

### Implementation

- [X] T027 [US2] Add `instructions` (`max_length=8000`) to `WorkspaceCreate`, `WorkspaceUpdate` and `WorkspaceResponse` in `backend/app/api/v1/schemas/workspace.py`. Apply it in `update_workspace` and `create_workspace_endpoint` (`backend/app/api/v1/workspaces.py`) and in the agent-builder `update_workspace`/`create_workspace` handlers (`backend/app/services/builtin_tools/workspaces.py`).
- [X] T028 [US2] In `backend/app/services/workspaces.py` add `visible_linked_skills(...)` and `relink_workspace_skills(...)`, mirroring `visible_bound_knowledge_bases` and `rebind_workspace_knowledge_bases` (row lock, per-id visibility refusal, keep what the caller cannot see). Add `GET`/`PUT /skills` in `backend/app/api/v1/workspaces.py` with an audit entry.
- [X] T029 [US2] In `backend/app/services/builtin_tools/skills.py`, let `resolve_agent_skills` and `build_skill_tools` take `extra_groups: list[tuple[list[str], SkillAccessContext]]`. Merge them before `_assign_catalog_names`. Every `load_skill` re-resolves each group under its own access. Keep the existing signatures' behaviour when `extra_groups` is empty. Ref: R8.
- [X] T030 [US2] Fill the resolver's skills and bound-KB fields in `backend/app/services/workspace_knowledge.py`:
  - skills under `build_skill_access_context(db, user.id)`;
  - bound KBs via `visible_bound_knowledge_bases` minus workspace-owned rows.
- [X] T031 [US2] In `backend/app/services/agent_chat.py`:
  - Insert the `workspace_instructions` section after the attached prompts and skills (~2794), through `defuse_secret_markers`, and label it in `_prompt_sections`.
  - Pass the workspace skill group to the skill resolution (~2705–2760) and to the `load_skill` registration (~3925).
  - Append the bound KB ids read-only after the session replacement.
  - Makes T026 pass. Refs: R7, R8.

**Checkpoint**: US1 and US2 work together; an agent in a filed conversation has the instructions, sources, skills and bound KBs.

---

## Phase 5: User Story 5 - Deleting a workspace cleans up its knowledge (P1, PR1)

**Goal**: deleting a workspace removes its two KBs and everything in them, and nothing else (FR-031 to FR-033).

**Independent Test**: [quickstart](quickstart.md) PR1 step 9.

### Tests

- [X] T032 [P] [US5] Extend `backend/tests/test_workspace_knowledge_postgres.py`.
  - After `DELETE /workspaces/{id}`, no row remains for the two KBs in `knowledge_bases`, `knowledge_documents`, `embeddings`, `kb_sync_sources`, `kb_activity_logs` or `kb_export_packages`, and the workspace's `workspace_skills` rows are gone.
  - The linked library KB and the skill still exist.
  - `cancel_document_jobs_after_commit` received exactly the documents' ids (spy).
  - Export-package S3 objects were deleted (fake storage).
  - The audit entry carries the counts.
  - A refused delete (default workspace, held work items) changes nothing.
  - Deleting the owning user cascades the KBs too.
  - The agent-builder `delete_workspace` tool behaves the same.

  Refs: US5 scenarios 1–3.

### Implementation

- [X] T033 [US5] Add `purge_workspace_knowledge(db, workspace)` in `backend/app/services/workspaces.py`. It collects the owned KBs' document ids, calls `cancel_document_jobs_after_commit`, deletes the export-package S3 objects and the KBs, and returns the counts. Call it from `remove_workspace` after the refusals and before `db.delete`. Add the counts to `WorkspaceDeletion` and to the audit details in `backend/app/api/v1/workspaces.py:435` and `backend/app/services/builtin_tools/workspaces.py:601`. Ref: R13.
- [X] T034 [US5] Update the docstring of `delete_workspace` (`backend/app/api/v1/workspaces.py:411`) and the `WorkspaceKnowledgeBase` docstring (`backend/app/models/workspace.py:189`). Bound KBs stay untouched, owned KBs go. Every sentence must stay true to the diff.

**Checkpoint**: deletion is clean.

---

## Phase 6: User Story 6 - The same knowledge on every surface (P3, PR1 part)

**Goal**: General Assistant, consult, delegate, transfer, channel and flow turns receive what a web turn receives, under the same rules (FR-025 to FR-027).

**Independent Test**: [contracts/agent-turn.md](contracts/agent-turn.md) surface table, one turn per surface.

### Tests

- [X] T035 [P] [US6] Extend `backend/tests/test_workspace_knowledge_turn.py` with one test per surface, asserting with spies on the call site:
  - **General Assistant** in a filed conversation: instructions in the prompt, the Sources KB in `build_kb_search_tool`'s ids, the skill group passed.
  - **Consult**: the child `run_agent_chat` receives `session_workspace_id`.
  - **Delegate**: `_execute_sub_agent_task` passes the workspace of `task.conversation_id`.
  - **Transfer**: the child resolves from `conversation_id`.
  - **Channel**: an identified sender gets the knowledge; `"anonymous"` gets none.
  - **Flow**: a `capability.workspace` node passes `session_workspace_id`, and an invisible or archived workspace fails the run with `WorkspaceScopeError`.
  - **XTM One MCP**: no surface gets it.

  Refs: US6 scenarios 1–3 and 5, FR-028.

### Implementation

- [X] T036 [US6] In `backend/app/services/general_chat.py`, resolve the workspace knowledge at turn start like T023. Insert the instructions after the GA system prompt (~376) and the full-sources block. Append the Sources and bound KB ids to its `knowledge_base_ids` (~348). Pass the skill group to its skill resolution (~611–637).
- [X] T037 [US6] Pass the resolved workspace on to sub-agents:
  - **Consult**: in `backend/app/services/agent_chat.py:4736` pass `session_workspace_id=_workspace_id`.
  - **GA consult**: do the same in `backend/app/services/general_chat.py:834`.
  - **Delegate**: in `backend/app/services/sub_agent.py:2918` (`_execute_sub_agent_task`) load the workspace of `task.conversation_id` and pass it.
- [X] T038 [US6] Add the `capability.workspace` node (`config.workspace_id`):
  - define it in `backend/app/services/flow_catalog.py`;
  - in `backend/app/services/flow_runner.py`, resolve it with `resolve_workspace_scope` as the acting user (fail closed, logged reason) and pass `session_workspace_id` to wired agent nodes;
  - validate references in `backend/app/services/flow_references.py`.

  Ref: R11.
- [X] T039 [US6] Document the surfaces in `dev-docs/reference/agents-and-assignments.md` (what a turn in a filed conversation receives) and the node in the flow builder reference.

**Checkpoint (PR1 done)**:
1. Run `./scripts/ci-local.sh --no-e2e` in the background.
2. Run `/code-review high` and `/security-review`.
3. Open PR1 `feat(workspaces): give agents a workspace's instructions, sources and skills`. Its description includes `## Behaviour changes`, which names the 409 on workspace KBs, the new library entries and the adjacent binding fix, and `## Tests run`.

---

## Phase 7: User Story 3 - Search the workspace's conversations (P2, PR2)

**Goal**: filed DM and web conversations become searchable after going idle; tool results never (FR-015 to FR-021).

**Independent Test**: [quickstart](quickstart.md) PR2 steps 1–3.

### Tests

- [X] T040 [P] [US3] Create `backend/tests/test_workspace_conversation_index.py` (unit).
  - **Transcript content.** Built from user and assistant rows only. Excludes:
    - `tool_calls_json`, `metadata_json.tool_call_trace` and `role=tool`;
    - hidden wake prompts.
  - **Secrets.** Secret markers render `[secret]`.
  - **Attachments.** Attachment text is capped per attachment.
  - **Summary.** The summary call receives no tool data (spy on `llm.chat` messages).
  - **Eligibility clause.** It excludes group threads, archived conversations, unowned conversations and unfiled conversations, and includes web, Slack DM, Teams DM and `api` conversations.
  - Refs: FR-015, FR-017, FR-017a, FR-021.
- [X] T041 [P] [US3] Create `backend/tests/test_workspace_conversation_index_postgres.py` (integration).
  - **Indexing.**
    - The tick enqueues only conversations idle past the setting.
    - New messages followed by idle replace the one document, with no duplicate.
    - A move indexes into the new workspace and deletes the old document.
    - Archive, unfile, `clear_agent_chats` and user deletion make the sweep delete documents and index rows.
    - A job whose conversation became ineligible writes nothing.
    - Two concurrent jobs on one conversation produce one document.
    - No write touches `conversations.updated_at`.
  - **Gating.** AI disabled skips the step.
  - Refs: FR-016, FR-018, R9.
- [X] T042 [P] [US3] Create `backend/tests/test_workspace_conversation_search.py`.
  - **Filter.** `search_workspace_conversations` passes `source_ids` = eligible documents minus the current conversation (spy on `hybrid_search_embeddings`).
  - **Immediacy.** An archived conversation is absent on the next call before any sweep.
  - **Who can search.** A `read_only` member finds another member's conversation (FR-020); a non-member's turn has no tool.
  - **Input validation.** A blank query returns a refusal message.
  - **Link escaping.** One table case per escaping variant of the title in the markdown link: `]`, `[`, `)`, `(`, a newline, a backtick, `<`, `>`, and a title that is only whitespace. The link uses the base path.
  - Refs: FR-019, FR-020, R10.

### Implementation

- [X] T043 [US3] Add `WorkspaceConversationIndex` in `backend/app/models/workspace.py` and `workspace_conversation_idle_minutes` in `backend/app/models/platform_settings.py`. Create `backend/alembic/versions/w6533wsknow02_workspace_conversation_index.py` (`down_revision = "w6533wsknow01"`, same safety recipe). Add the table to `PARITY_TABLES`. Ref: data-model.md.
- [X] T044 [US3] Create `backend/app/services/workspace_conversation_index.py`:
  - `eligible_conversation_clause()`: `web_listable_clause()` or channel `api`, plus filed, not archived and owned;
  - `build_index_text(...)` (R9 step 2–3);
  - `summarise_transcript(...)`, following `conversation_memory.generate_conversation_title`: `get_llm_for_user(db, conv.user_id)`, traced, time-bounded, output redacted;
  - `index_conversation(db, conversation_id)`: advisory lock, re-check, write or replace through `kb_documents` into `ensure_workspace_kb(…, "conversations")` with `extra_metadata.conversation_id`, upsert the index row;
  - `pick_idle_conversations(db, limit)` and `sweep_stale_conversation_documents(db)`.

  Comment why nothing is written to `conversations` (VII). Makes T040 pass.
- [X] T045 [US3] Register `index_workspace_conversation_job` in `backend/app/worker.py`. Use `_skip_because_ai_is_disabled`, key `index-conversation-{id}`, and timeout and retries like `process_document_job`. Add a tick step in `backend/app/services/autonomous.py` (~1595 pattern, gated on `_ai_on`) that runs `pick_idle_conversations`, enqueues the picks, then runs the sweep with `cancel_document_jobs_after_commit`. Makes T041 pass.
- [X] T046 [US3] Add `source_ids: list[str] | None` to `make_search_knowledge_fn` in `backend/app/services/vector.py` and forward it to `hybrid_search_embeddings` and the fallback.
- [X] T047 [US3] Create `backend/app/services/builtin_tools/workspace_knowledge.py` with `build_workspace_conversation_search_tool(user_id, workspace_id, kb_id, current_conversation_id)`. It computes `source_ids` per call and renders `[title](base-path link)` with the escaping helper. If no shared markdown-link escaper exists in `backend/app/core/` or `services/`, add one there. Register it in `backend/app/services/agent_chat.py` and `backend/app/services/general_chat.py` when the resolver returns a Conversations KB. Fill `conversations_kb_id` in the resolver. Makes T042 pass.
- [X] T048 [US3] Add the `conversations` counts to `GET /workspaces/{id}/knowledge` in `backend/app/api/v1/workspaces.py`. Extend `GET/PUT /settings/workspace-knowledge` with `conversation_idle_minutes` in `backend/app/api/v1/settings.py`. Add tier tests to `backend/tests/test_workspace_knowledge_postgres.py`.

**Checkpoint**: conversations are searchable across the workspace.

---

## Phase 8: User Story 6 - File a conversation by asking the agent (P3, PR2)

**Goal**: FR-027b and FR-022 on the agent path.

**Independent Test**: [quickstart](quickstart.md) PR2 step 4.

### Tests

- [X] T049 [P] [US6] Create `backend/tests/test_workspace_filing_tools.py`.
  - **When the suite is offered.** On an identified turn with a conversation; not on an anonymous turn; not twice when `agent_builder` is present.
  - **Consent.** `file_this_conversation` into a workspace shared with others pauses for approval on a screened executor and files once approved (mid-turn or resumed); unscreened, it is refused. A private workspace files at once.
  - **Refusals.** Archived target, `read_only` target, a conversation not owned by the person, and no conversation yet (first channel message, with the "next message" wording).
  - **Creation.** `create_workspace` creates one owned by the person.
  - Refs: US6 scenario 4, FR-022, FR-027b, R12.

### Implementation

- [X] T050 [US6] Create `backend/app/services/builtin_tools/workspace_filing.py`. It builds `list_workspaces`, `create_workspace` and `file_this_conversation` over the handlers of `backend/app/services/builtin_tools/workspaces.py`, extracted into callable functions if they are closures. It installs the FR-022 consent screen (`install_filing_consent`), judged by one "reaches others" helper (company-managed, or any user or group share). Register it in `backend/app/services/agent_chat.py` and `backend/app/services/general_chat.py` per the rules of T049. Makes T049 pass.

**Checkpoint (PR2 done)**: run CI, the reviews and the PR as in Phase 6. Open `feat(workspaces): search a workspace's conversations and file a conversation by asking the agent`, stacked on PR1.

---

## Phase 9: User Story 4 - Project page and in-conversation knowledge drawer (P2, PR3)

**Goal**: everything above is reachable from a project page and from a drawer in any filed conversation (FR-029, FR-030, FR-022 in the UI).

**Independent Test**: [quickstart](quickstart.md) PR1 steps 1–7, done from the UI as `owner` and as `reader`.

### Tests

- [X] T051 [P] [US4] Create `frontend/src/components/workspaces/knowledge/KnowledgePanel.test.tsx` (vitest and Testing Library, `renderWithProviders`, `vi.mock("@/lib/api")`):
  - a manager sees add and remove controls for sources, skills and KBs, and an editable instructions field;
  - a `read_only` member sees the same items without any control;
  - source statuses render;
  - the conversation index counts render.
- [X] T052 [P] [US4] Create `frontend/src/components/chat/FileConversationDialog.test.tsx`: filing into a workspace that reaches others asks for confirmation first; a private one files at once. Extend `frontend/src/pages/chat/WorkspaceGroups.test.tsx` for the same rule on drag and drop and on the menu.
- [X] T053 [P] [US4] Create `frontend/src/pages/KnowledgeBases.test.tsx` cases: a workspace-owned KB shows the badge and links to its workspace; its detail page is read-only; the agent KB picker, `WorkspaceKnowledgeBaseDialog` and profile pickers do not offer it.

### Implementation

- [X] T054 [US4] Add the types `WorkspaceSource`, `WorkspaceSkillRef`, `WorkspaceKnowledgeSummary` and `ConversationIndexStatus`, plus `Workspace.instructions` and `KnowledgeBase.managed_by_workspace` and `content_lock` (adding `"workspace"`), in `frontend/src/types/index.ts`. Add the hooks `useWorkspace(id)`, `useWorkspaceSources`, `useAddWorkspaceSource*`, `useDeleteWorkspaceSource`, `useReprocessWorkspaceSource`, `useWorkspaceSyncSources*`, `useWorkspaceSkills` / `useSetWorkspaceSkills` and `useWorkspaceConversationIndex` in `frontend/src/hooks/api/useWorkspaceApi.ts`. Sources poll while any source is pending.
- [X] T055 [US4] Extract the upload dropzone, paste-text dialog (with the DS `Textarea`) and document list from `frontend/src/pages/knowledge-base-detail/DocumentsTab.tsx` into `frontend/src/components/knowledge-base/`. `DocumentsTab` uses them unchanged. *Done differently: the knowledge-base tabs are reused whole, in a `Manage sources` dialog, so nothing was extracted.*
- [X] T056 [US4] Create `frontend/src/components/workspaces/knowledge/`:
  - `KnowledgePanel.tsx`;
  - `InstructionsCard.tsx`;
  - `SourcesCard.tsx` (upload, paste, URL, sync via the `SourcesTab` sync forms, statuses, retry, remove);
  - `LinkedKnowledgeBasesCard.tsx` (reusing the `WorkspaceKnowledgeBaseDialog` logic);
  - `SkillsCard.tsx` (`MultiSelect` over `useSkills()`);
  - `ConversationIndexCard.tsx`.

  Gate controls on `canManage`, from `can_manage`. Use DS components and tokens only, and `EmptyState`. Makes T051 pass.
- [X] T057 [US4] Create `frontend/src/pages/WorkspaceProject.tsx` and its route `/workspaces/:workspaceId` in `frontend/src/App.tsx`. Use `lazyWithRetry` and `<Page>`, and replace the `/workspaces` legacy redirect only for the id form. The page has a header (name, badges, actions menu), a composer entry that opens `/chat?w=<id>`, the workspace's conversations, and the `KnowledgePanel` on the right. Link to it from the `WorkspaceGroupSection` header and its "Open" item (`frontend/src/pages/chat/WorkspaceGroups.tsx`) and from the cards in `frontend/src/pages/operations/WorkspacesTab.tsx`.
- [X] T058 [US4] In `frontend/src/pages/Chat.tsx`: *Done differently: the project page creates the conversation filed in the workspace and opens `/chat?c=`, so `Chat.tsx` needs no `?w=`.*
  - Honour `?w=` on the auto-create paths (~1040, ~1982).
  - Add `workspace_id` to the optimistic seed in `frontend/src/hooks/api/useChatApi.ts:519`.
  - Add the overflow item "Workspace knowledge", shown when `activeConv?.workspace_id`. It opens `frontend/src/components/workspaces/WorkspaceKnowledgeDrawer.tsx` (the `ConversationFilesPanel` slide-over pattern, or the DS drawer if one exists). Close the drawer on conversation change (~2642).
  - If the DS lacks a drawer, add an entry to `fds-migration/LIBRARY-FEEDBACK.md`.
- [X] T059 [US4] Add "Add to workspace" through a new context provided only by `Chat.tsx`, so other hosts show nothing: *Partly: from the `Conversation files` panel; the attachment chips of the transcript are not done.*
  - on attachment chips in `frontend/src/pages/chat/ChatMessages.tsx` (`FileChip`/`AttachmentChips`);
  - on `FileRow` in `frontend/src/pages/chat/ConversationFilesPanel.tsx`.

  Show it to managers of the conversation's workspace. It calls `POST /sources/from-attachment`.
- [X] T060 [US4] Before filing into a workspace for which `reachesOthers` holds, ask with `useConfirm` (FR-022) in `frontend/src/components/chat/FileConversationDialog.tsx` (`choose`) and `frontend/src/pages/chat/useSidebarWorkspaces.ts` (`fileConversation`, with the confirm node rendered by `ConversationSidebar.tsx`). Makes T052 pass.
- [X] T061 [US4] In `frontend/src/pages/KnowledgeBases.tsx` (card ~48 and list ~96) and `frontend/src/pages/KnowledgeBaseDetail.tsx` (~150, ~192), add the "Managed by workspace X" badge, linking to the project page, and read-only mode (`canManage` false, delete and sources hidden). Filter `managed_by_workspace` out of every picker that consumes `useKnowledgeBases()` for binding (list in research R3). Makes T053 pass.
- [X] T062 [US4] Add a "Workspace knowledge" settings card in the admin settings page (sources share %, idle minutes) with `useWorkspaceKnowledgeSettings`. Add the `capability.workspace` node's config editor if the flow builder does not render it from the catalog schema.
- [X] T063 [US4] Add every new key to `frontend/src/i18n/lang/{en,fr,es,ja}.json` with real translations. Add commands to the `.commands.ts` of the new page. Run `cd frontend && yarn check`.
- [X] T064 [US4] Add a Playwright journey in `frontend/tests_e2e/workspace-knowledge.spec.ts`: create a workspace, add an instruction and a text source, start a conversation from the project page, open the drawer, and see the source as ready.

**Checkpoint (PR3 done)**: take real screenshots of each new control, then run CI, the reviews and the PR. Open `feat(workspaces): add the workspace project page and knowledge drawer`, stacked on PR2.

---

## Phase 10: Polish & Cross-Cutting Concerns

- [X] T065 [P] Document the feature for users in `docs/docs/` (the workspaces page: instructions, sources, skills, conversation search, who sees what, deletion) and the two settings in `docs/docs/administration/settings.md`.
- [X] T066 [P] Update `dev-docs/reference/entities.md` (Workspace, the workspace-owned Knowledge Base) and `dev-docs/reference/api-routes.md` (new routes).
- [ ] T067 Run [quickstart.md](quickstart.md) end to end on a local stack, PR by PR, and record the results in each PR's `## Tests run`.
- [ ] T068 Re-read `spec.md`, `plan.md` and every PR description against the diff, and replace any sentence that is no longer true (constitution XI, AGENTS.md § 6).

---

## Dependencies & Execution Order

- **Phase 1 → Phase 2.** Phase 2 blocks every story.
- **US1 (Phase 3)** needs only Phase 2.
- **US2 (Phase 4)** needs Phase 2. It touches `agent_chat.py` after T023, so it runs after US1 in practice.
- **US5 (Phase 5)** needs Phase 2 only, and can run beside US1 and US2.
- **US6 wiring (Phase 6)** needs T023, T031 and T020.
- **PR2 (Phases 7–8)** needs PR1 merged or stacked. T047 needs T046 and T044.
- **PR3 (Phase 9)** needs PR2's API. T056 needs T054 and T055. T057 and T058 need T056.
- **Polish** comes last.

### Within each story

Tests first and failing, then models, services, routes, and turn wiring.

### Parallel opportunities

- **Phase 2**: T003, T004 and T005 together. T009 and T011 beside T010 and T012 (different files).
- **US1**: T017, T018 and T019 together.
- **US2**: T025 and T026 together.
- **US5**: T032 beside US1 and US2 work.
- **US3**: T040, T041 and T042 together.
- **US4**: T051, T052 and T053 together. T055 beside T054.
- **Polish**: T065 and T066.

## Parallel Example: User Story 3

```text
Task: "T040 transcript and eligibility unit tests in backend/tests/test_workspace_conversation_index.py"
Task: "T041 indexing lifecycle integration tests in backend/tests/test_workspace_conversation_index_postgres.py"
Task: "T042 search filter and link-escaping tests in backend/tests/test_workspace_conversation_search.py"
```

## Implementation Strategy

- **MVP**: Phases 1–3 (US1). The agents of a workspace use its sources, which is the core of the request.
- **PR1**: add US2, US5 and the US6 wiring. PR1 is the first reviewable unit, green on its own.
- **PR2 and PR3** follow, each green on its own before the next opens.
- **Fixes during review**: a fix goes into the lowest PR concerned and is carried up the stack at once.
