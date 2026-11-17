# API – Developer Guide

The FastAPI routers (`v1/`, SSO and OAuth callbacks; channel webhooks live in `app/channels/`) and their shared helpers (`v1/helpers.py`, `deps.py`).

## Where to look

| Need | Read |
|---|---|
| Route prefixes | `dev-docs/reference/api-routes.md` |
| Routers: file, purpose, auth, endpoints (the main ones) | `dev-docs/reference/backend-api-routes-detail.md` |
| Patterns in full: commits, visibility, admin reach, ids, files, company-managed toggle, continuing runs | `dev-docs/reference/backend-api-patterns.md` |
| Security switches and Security Audit checks | `dev-docs/reference/backend-api-security-controls.md` |
| New-feature checklist (rules cited by number) | `dev-docs/reference/adding-features.md` |

## Rules

- **Auth**: every non-public route takes `Depends(get_current_user)` or `Depends(require_admin)`; Pydantic models for every schema.
- **Commit before answering**: a write route calls `await db.commit()` after the write and its audit row, before it returns; an outbound step that must undo the write stays before it (`tests/test_write_routes_commit_before_response.py`, `tests/test_account_writes_commit_before_response.py`).
- **After-commit side effects** (a cache drop above all) run right after that commit or through `after_commit: AfterCommit = Depends(run_after_commit, scope="function")` (`deps.py`), always function-scoped.
- **Status codes**: `status.HTTP_201_CREATED` on create, `status.HTTP_204_NO_CONTENT` on delete; numeric codes in `HTTPException`.
- **Visibility**: lists use `user_visible_query()`, counts `user_visible_filter()`, with `get_user_group_ids()`; never a hand-written `or_(user_id, is_company_managed)`.
- **Invisible = unknown**: an entity the caller cannot see gets the unknown id's 404, never a 403; 403 only for one they see but may not manage (`get_manageable_entity()`).
- **Admin reach**: library lists and pickers stay three-tier for admins; detail routes pass `is_admin=user.is_admin`; entity lists and run feeds take `scope: EntityListScope` (`list_scope_admin_bypass()`). An admin reading other users' private rows by role alone is an audited `break_glass` oversight read, committed before serving.
- **Assignments** are visible exactly when their agent is: `assignment_visible_filter()`, never the mirrored `Assignment.user_id` / `is_company_managed`. Agent tools go through `find_user_assignment(..., is_admin=agent_tool_admin_bypass(caller))` and `manual_trigger_refusal()` / `manual_trigger_overlap()`, never a copy (`tests/test_manual_trigger_gate.py`).
- **Run access**: `require_run_access(run, user, *, action)`.
- **Continuing runs** (follow-up, chain, retry, delegation child) go through `trigger_assignment(continues_run=...)`: `continuing_run_is_company_managed()` (explicit `ceiling`) and `can_open_continued_run()`; a refusal is reported to the owner alone, never dropped (`tests/test_continuing_run_visibility.py`).
- **Company-managed**: toggle only via `handle_company_managed_toggle()` / `require_admin_for_company_managed()` / `toggle_company_managed_list_field()` (admin-only; going private cleans other users' references).
- **Stored files**: load through `load_readable_file()` / `load_readable_chat_image()` (`services/file_access.py`); every refusal is the same 404, never a 403.
- **Client ids**: never a bare `uuid.UUID(...)`. The row a route acts on: `parse_path_id()` / `parse_conversation_id()` (unknown-id 404); list or body filters: `parse_uuid()` (422); `safe_uuid()` only where the route has its own answer (`tests/test_malformed_path_id_routes.py`, `tests/test_malformed_conversation_id.py`).
- **Literal paths before `/{id}`** in a router, or the id route swallows them.
- **Agent copies**: duplicate, export and import take their fields from `app/models/agent_copy.py`; `budget_config` above the platform ceiling is admin-only (`assert_budget_within_ceiling()` on create/update, `lower_budget_to_ceiling()` on duplicate/import).
- **OAuth authorize** only for whom the callback stores a token: `can_authorize_integration()`.
- **Admin OAuth app deliveries** (provider webhooks, LinkedIn notifications) are handed over only by `_admin_app_integrations_by_assignment` in `webhooks.py`; never re-spell it.
- **MCP endpoints** read their body through `v1/mcp_transport.py`.
- **Layering**: services, channels and tools never import a private `_helper` of this layer (`backend/AGENTS.md`).

## Adding an endpoint

1. Add the route to the area's `v1/` module (a new router registers in `app/main.py`); auth dependency, Pydantic schemas, named status codes.
2. Resolve entities through the helpers above (visibility, manage tier, ids, files); detail routes pass `is_admin`, new entity lists take `scope`.
3. Audit the write (`log_audit()`), commit before returning, side effects after the commit.
4. Document it in `backend-api-routes-detail.md` (and `api-routes.md` for a new prefix); frontend types and hooks per root `AGENTS.md`.

## Adding a security switch or check

A switch that makes the platform safer gets one `SecurityControl` in `SECURITY_CONTROLS` (`app/core/security_controls.py`), or a `NOT_CONTROLS` entry with its reason. A finding has a literal, stable `id="<category>.<name>"` (dismissals are keyed on it), a category from `SECURITY_CATEGORIES`, `control=` when it is about a control, follows the severity rubric and reports its pass. Full contract: `dev-docs/reference/backend-api-security-controls.md`; enforced by `tests/test_security_controls.py` and `tests/test_security_audit_finding_ids.py`.

## Tests

- A shareable entity's endpoints are exercised as a NON-ADMIN: `is_admin=True` short-circuits the visibility predicate (rule 19, `tests/test_visibility_helpers.py`).
- The tripwires named above fail the build on a route that breaks its rule; an exemption names its reason in the test.
