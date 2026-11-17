# Services – Developer Guide

Business logic and background work; API routes stay thin and call it. Grep the service map before writing a helper: most rules have ONE owning module.

## Where to look

| Need | Read |
|---|---|
| Assignments, orchestration, flows, evaluations, panic | `dev-docs/reference/backend-services-execution.md` |
| Chat turns, turn lock, fan-out, channels | `dev-docs/reference/backend-services-chat.md` |
| Memory | `dev-docs/reference/backend-services-memory.md` |
| Built-in tools, sandboxes, MCP, skills, rendering, browser profiles | `dev-docs/reference/backend-services-tools.md` |
| Files, widget uploads, knowledge bases | `dev-docs/reference/backend-services-files-and-knowledge.md` |
| Integrations, polling, OAuth, vault, mail | `dev-docs/reference/backend-services-integrations.md` |
| Decks, reports, templates, Video Studio; Deep Investigation | `dev-docs/reference/backend-services-presentations.md`, `backend-services-investigations.md` |
| Agent config, workspaces, LLM providers, relays | `dev-docs/reference/backend-services-platform.md` |
| Audit, prompt guards, HITL, API keys | `dev-docs/reference/backend-services-safety.md` |
| Quotas, AI gate, holds, telemetry | `dev-docs/reference/backend-services-quota.md` |
| Shared helpers by name | `dev-docs/reference/backend-shared-utilities.md` |

## Rules

**One owner, never re-spelled**:
- Pausing: every `enabled = False` via `assignment_pause.pause_assignment`; every query picking assignments or flows to fire adds `owner_not_deactivated()`, a new `create_flow_run` caller the owner check (`test_flow_owner_deactivation.py`).
- Long-running work runs on `orchestration_engine` executions, never Redis delay keys or a task of its own; queue caps, keys and plans in `assignment_dispatch`.
- Background AI work asks `agentic_gate` before any work, never `ai_state.is_ai_enabled()` alone; a transient refusal defers, a definitive one holds (`ai_hold`), never fails or charges (`test_background_edition_gate.py`).
- Quota: fail-mode, refund and counter-clear rules in `backend-services-quota.md` (flush-lock + stash protocol, never a bare `DEL` of `quota:usage:` keys).
- Deletion: an agent only via `agent_deletion.delete_agent` (`test_agent_deletion.py`); a KB document's jobs only via `kb_helpers.cancel_document_jobs_after_commit()` (`test_document_job_cancel_after_commit.py`).
- Company-managed shared state and work items: every write takes a `CompanyManagedWriter` (`test_company_managed_writes.py`).
- Files: attachment, KB embedding, stored bytes, `FileContext` and webhook trigger file rules in `backend-services-files-and-knowledge.md` / `backend-services-tools.md`; stored-file edits: one find/replace path (`edit_file_content`), strict UTF-8, BOM kept.
- Sandbox workspace files, paths, quota: only `sandbox_workspace/`.
- Rendering: SVG / Chromium / outbound HTTP / mail / Teams rules in `backend-services-tools.md` (`svg_raster`, `headless_render`, `PLATFORM_USER_AGENT`, `email_html`, `teams_format`).
- Chat: turn lock, terminal-frame ordering, `linked_agent_id`, web/Slack/Teams continuation rules in `backend-services-chat.md`.
- Deck / video sharing and edits: `presentation_sharing`, `share_principals`, `presentation_comments`; workspace rules raise from `services/workspaces.py`; agent-builder writes use the `_load_writable_*` / `_load_manageable_entity_for_builder` gates (`helpers.can_manage()`).
- Browser profiles: secrets and session only via `browser_profiles.profiles`, what a run's `browser` tool opens only via `agent_access`, URLs via `browser.check_target`.
- Video Studio: a render starts only via `projects.start_render`, runs in `render_process.run_isolated`, is deleted only via `render_retention.delete` + `purge` (what keeps one: `render_retention.kept_by`); only `resources.py` reads container CPU / memory, `render_retry` judges a platform failure, `motion_design` owns motion.
- Writing `Agent.skill_ids` runs `reconcile_agent_skill_package_bindings` after the binding validators.
- Work items: what one means to its reader only via `work_item_context`; scheduled ones' rules and `action_mode` writes in `scheduled_work_items`; investigation step status only via `investigation_state.record_step_result`, markers via `build_investigation_marker`.

**Safety and security**
- Prompt-guard, external-content, HITL and URL rules: `backend-services-safety.md` (`prompt_guards.evaluate_text`, `content_safety.wrap_untrusted()` / `wrap_memory()`, `hitl_consent`, `url_guard.finalize_platform_links()`). Fix behaviour in code, never in system-prompt text.
- OAuth `reauthorize` is terminal until re-authorization; both refresh paths lock, re-read the stored row and apply only to the tokens they started from (`test_token_refresh_reauthorize_contract.py`, rule in `backend-services-integrations.md`).
- User memory writes: only `update_entries()` (never a bare `SET`); every caller passes `agent_id` when rendering it; cross-agent preferences live only in the user profile (rule in `backend-services-memory.md`).
- Whose LLM analytics a non-admin reads: only `group_managers.team_scope` (insights: `platform_insights.scope.load_scope`); a manager's team never reaches traces or sessions.
- Tools never echo secrets; telemetry carries no identities or content.

**Panic**: arm and cancel are TWO transactions, never merged; `platform_stop_open()` is the stop signal, never the raw `ai_disabled` bit; re-enable paths refuse on `is_emergency_stopped()`.

**Streaming**: `streaming` never clears accumulated `thinking_text`; answer text already shown is never retracted.

## Adding a new service

Async, `db: AsyncSession` param, `logging.getLogger(__name__)`; called from routes or services. Add its row to the matching `backend-services-*.md`.

## Tests

The tripwires above fail the build on a bypass; `test_builtin_tools_facade.py` pins the `builtin_tools` re-exports; `test_quota_global_postgres.py` runs quota on real PostgreSQL + Redis.
