# Implementation Plan: Workspace Knowledge — Sources, Skills, Instructions and Conversation Search

**Branch**: `rds/workspace-docs-conversations-8d5263` (stack base) | **Date**: 2026-10-24 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/032-workspace-knowledge/spec.md`

## Summary

A workspace gains instructions, linked skills and two owned knowledge bases: Sources and Conversations. Their visibility derives from the workspace by a subquery in `user_visible_filter`, not by mirrored columns (R1). They are locked outside the workspace by the existing KB mutation choke point (R2) and never bindable (R3). One resolver decides what a turn receives (R5). It feeds `run_agent_chat`, `run_general_chat`, consult, delegate and a new `capability.workspace` flow node:
- instructions;
- sources, in full or by search (R6);
- bound KBs and skills, under the turn user's visibility (R8).

Filed DM and web conversations are indexed after they go idle, never their tool results (R9). The new conversation-search tool filters at query time, so every unfile, archive or delete path takes effect at once (R10). People file a conversation from the web history or by asking the agent (R12). Deleting a workspace deletes its two KBs by FK cascade, after cancelling their jobs (R13).

The feature ships as three stacked PRs.

## Technical Context

**Language/Version**: Python 3.14 (backend), TypeScript / React 19 (frontend)

**Primary Dependencies**: FastAPI, SQLAlchemy async, Alembic, SAQ, pgvector hybrid search; TanStack Query, Filigran design system

**Storage**: PostgreSQL. New columns on `workspaces`, `knowledge_bases` and `platform_settings`; new tables `workspace_skills` and `workspace_conversation_indexes` (data-model.md). No new S3 objects: KB documents store their content in the database.

**Testing**: pytest (unit, plus `integration` with PostgreSQL for visibility SQL, cascades and migrations); vitest and node:test (frontend); Playwright for the project page.

**Target Platform**: XTM One backend replicas, SAQ workers, web app

**Project Type**: web application (backend and frontend)

**Performance Goals**:
- Turn start adds one read of the conversation's workspace plus one resolver query set, concurrent with the existing gathers.
- The full-sources token estimate uses stored `content` lengths: one aggregate query.
- The indexing tick processes a bounded batch.
- Search adds one `source_ids` filter.

**Constraints**:
- Fail closed on visibility (II).
- No vendor or tool-name logic (XII).
- Migrations are lock-safe, single head (V).
- The sidebar order is untouched: no writes to `conversations`.

**Scale/Scope**: about 10 backend modules touched, 3 new services, 2 migrations, about 15 frontend files, 4 locale catalogs.

## Constitution Check

*GATE: passes before Phase 0; re-checked after Phase 1: unchanged.*

| Principle | Status |
|---|---|
| I Assignment-driven | Pass. Indexing is platform maintenance, like KB sync and memory maintenance, not automation of agent work. |
| II Fail-closed | Pass. The resolver returns nothing for an invisible workspace or anonymous person (R5). The flow node fails the run on an unresolvable workspace (R11). Search filters on current eligibility (R10). |
| III Security | Pass, with these provisions: <ul><li>Sources are quarantined (R6).</li><li>Titles in search links are escaped for every markdown-link component (R10).</li><li>Secrets stay redacted in the index (R9).</li><li>URL sources keep the SSRF guard (R4).</li><li>Tool results are never indexed (FR-017a).</li></ul> |
| IV Multi-replica | Pass. Lazy KB creation locks the workspace row. The tick runs under the fleet lock. Each job holds an advisory lock and re-checks under it. Jobs are deduplicated by key. |
| V Migrations | Pass. Two migrations, idempotent, `terminate_lock_holders` and `set_lock_timeout`, working downgrades, stack-numbered ids (R14). |
| VI CI gates | Pass. `ci-local.sh --no-e2e` runs per PR. Integration tests carry `pytestmark = pytest.mark.integration`. |
| VII Rationale comments | Pass. These spots carry a "why" comment: <ul><li>the R1 subquery;</li><li>the R9 "no write to conversations" rule;</li><li>the R10 query-time filter;</li><li>the R12 handshake.</li></ul> |
| VIII Shared utilities | Pass. <ul><li>Document ingestion is one service (R4).</li><li>The bindable clause and the workspace-owned clause each have one builder (R1, R3).</li><li>One turn resolver (R5).</li><li>The filing tools reuse the agent-builder handlers (R12).</li></ul> |
| IX TanStack Query | Pass. New hooks live in `hooks/api/useWorkspaceApi.ts`. |
| X Design system | Pass. DS components and tokens only. If the DS has no drawer, the slide-over follows `ConversationFilesPanel`, and the gap is filed in `fds-migration/LIBRARY-FEEDBACK.md`. |
| XI Minimal writing | Pass. Artifacts cite R, FR and US ids. |
| XII No workarounds | Pass. Nothing keys on an agent, tool name, vendor or model; the filing handshake is a generic tool result. |

**Invariant 16 (no system-prompt inflation)**: instructions and sources are user content, not platform steering (R7). **Invariant 18**: no keyword logic.

## Project Structure

### Documentation (this feature)

```text
specs/032-workspace-knowledge/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── workspace-knowledge-api.md
│   └── agent-turn.md
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks
```

### Source Code — PR1: sources, instructions, linked KBs, skills, wiring, cleanup

```text
backend/
├── alembic/versions/w6533wsknow01_workspace_knowledge.py
├── app/models/workspace.py                 # instructions, WorkspaceSkill
├── app/models/knowledge_base.py            # owner_workspace_id, workspace_role
├── app/models/platform_settings.py         # workspace_sources_context_percent
├── app/api/v1/helpers.py                   # workspace-owned clause in user_visible_filter + company_wide_visible_filter,
│                                           # bindable_knowledge_base_clause, managed_knowledge_base_error, content lock "workspace"
├── app/api/v1/users.py                     # list_user_knowledge_bases uses the clause
├── app/services/resource_exposure.py       # entity_audience for workspace-owned KBs
├── app/services/agent_bindings.py          # bindable clause in both validators
├── app/api/v1/binding_access.py            # bindable clause in refused_additions
├── app/services/flow_access.py, flow_references.py   # bindable clause in catalog and validation
├── app/services/kb_documents.py            # NEW: ingestion service (R4)
├── app/api/v1/knowledge_bases.py           # routes call kb_documents; managed_by_workspace in response
├── app/services/builtin_tools/kb.py        # _store_file_document calls kb_documents
├── app/services/workspaces.py              # ensure_workspace_kb, skill rebind, purge_workspace_knowledge in remove_workspace
├── app/services/workspace_knowledge.py     # NEW: resolve_turn_workspace_knowledge, full-sources block (R5, R6)
├── app/api/v1/workspaces.py                # instructions, /sources*, /skills, knowledge summary
├── app/api/v1/schemas/workspace.py
├── app/api/v1/settings.py                  # /settings/workspace-knowledge
├── app/services/builtin_tools/skills.py    # extra (skill_ids, access) groups (R8)
├── app/services/agent_chat.py              # turn-start resolution, session_workspace_id, prompt sections, KB and skill union, consult propagation
├── app/services/general_chat.py            # same resolver
├── app/services/sub_agent.py               # delegate propagation
├── app/services/flow_catalog.py, flow_runner.py      # capability.workspace (R11)
├── app/services/builtin_tools/workspaces.py # update_workspace accepts instructions; workspace KBs refused in rebind
└── tests/
    ├── test_workspace_knowledge_visibility.py        # compiled SQL, non-admin, read_only, config, group grant, revoked share, company-managed
    ├── test_workspace_knowledge_postgres.py          # integration: REST routes as non-admin, cascade on delete, parity
    ├── test_workspace_knowledge_turn.py              # resolver and run_agent_chat / general_chat / consult / delegate / flow wiring (spies)
    ├── test_workspace_sources_budget.py              # full vs search boundary, never partial
    └── test_kb_documents_service.py                  # KB routes unchanged through the service
frontend/                                            # nothing in PR1 beyond types needed by tests
docs/docs/, dev-docs/reference/{entities,integrations-and-knowledge-bases,api-routes}.md
```

### Source Code — PR2: conversation indexing, search, filing suite

```text
backend/
├── alembic/versions/w6533wsknow02_workspace_conversation_index.py
├── app/models/workspace.py                 # WorkspaceConversationIndex
├── app/models/platform_settings.py         # workspace_conversation_idle_minutes
├── app/services/workspace_conversation_index.py   # NEW: eligibility clause, tick step, job, sweep (R9)
├── app/services/autonomous.py              # tick step
├── app/worker.py                           # index_workspace_conversation_job
├── app/services/vector.py                  # source_ids filter
├── app/services/builtin_tools/workspace_knowledge.py   # NEW: search_workspace_conversations (R10)
├── app/services/builtin_tools/workspace_filing.py      # NEW: suite over the agent-builder handlers (R12)
├── app/services/agent_chat.py, general_chat.py         # register the two
├── app/api/v1/workspaces.py                # /conversation-index
└── tests/
    ├── test_workspace_conversation_index.py          # transcript excludes tool data and hidden prompts, secrets redacted, group threads excluded
    ├── test_workspace_conversation_index_postgres.py # integration: idle pick, replace, move, archive, user cascade, sweep
    ├── test_workspace_conversation_search.py         # source_ids filter, current excluded, link escaping table
    └── test_workspace_filing_tools.py                # handshake, refusals, first channel message, no duplicate with agent_builder
```

### Source Code — PR3: project page, drawer, library and pickers

```text
frontend/src/
├── App.tsx                                          # /workspaces/:id route (lazyWithRetry, <Page>)
├── pages/WorkspaceProject.tsx                       # NEW: composer entry, conversations, knowledge panel
├── components/workspaces/knowledge/                 # NEW: KnowledgePanel, InstructionsCard, SourcesCard, SkillsCard, LinkedKbCard, ConversationIndexCard
├── components/workspaces/WorkspaceKnowledgeDrawer.tsx   # NEW: same panel, opened from the chat header
├── components/knowledge-base/                       # extracted from DocumentsTab: upload dropzone, paste dialog, document list (reused)
├── pages/Chat.tsx                                   # drawer state and overflow item, ?w= on new conversation
├── pages/chat/ChatMessages.tsx, ConversationFilesPanel.tsx   # "Add to workspace" through a context
├── components/chat/FileConversationDialog.tsx, pages/chat/useSidebarWorkspaces.ts   # FR-022 confirm via reachesOthers
├── pages/chat/WorkspaceGroups.tsx                   # group header links to the project page
├── pages/KnowledgeBases.tsx, KnowledgeBaseDetail.tsx    # badge, read-only, link to workspace
├── KB pickers (agent, profile, settings, workspace dialog, flows)  # filter managed_by_workspace
├── pages/settings/…                                 # workspace-knowledge settings card
├── hooks/api/useWorkspaceApi.ts, types/index.ts
└── i18n/lang/{en,fr,es,ja}.json
tests: *.test.tsx per new component, a Playwright journey for quickstart PR1 steps 1–3
```

**Structure Decision**: the existing `backend/` and `frontend/` web-application layout. New logic goes into three new services and two new tool builders; everything else extends the modules that already own each rule.

## Delivery

| PR | Content | Depends on | Stories |
|---|---|---|---|
| 1 | Sources, instructions, linked KBs, skills, turn wiring on every surface, flow node, deletion, admin setting | `main` | US1, US2, US5, US6 (except filing) |
| 2 | Conversation indexing and search, filing suite and handshake, idle setting | PR1 | US3, US6-4 |
| 3 | Project page, drawer, library badge, picker filtering, Add to workspace, filing confirm, settings UI | PR2 | US4 |

Each level is green on its own before the next opens. A fix goes into the lowest level concerned and is carried up.

## Complexity Tracking

| Choice | Why needed | Simpler alternative rejected because |
|---|---|---|
| Subquery in `user_visible_filter` for one model | FR-002 requires visibility to follow every sharing change | Mirrored columns miss per-user shares and `access_restricted` (R1) |
| Query-time `source_ids` filter plus a reconciling sweep | About thirty paths unfile, archive or delete conversations, including DB cascades | A hook per path would miss cascades and future paths (R9) |
| A `workspace_filing` suite offered without configuration | FR-027b: any person may ask any agent | Requiring `agent_builder` hands every agent the whole configuration surface (R12) |
