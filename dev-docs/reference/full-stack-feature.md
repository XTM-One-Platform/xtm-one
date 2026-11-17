# Full-stack feature

The end-to-end path when a feature touches both the API and the frontend. Each step names the pattern doc that holds the rules; this file holds the order, not the rules.

## Steps

1. **Model** (if a new entity): `backend/app/models/<name>.py`, UUID `id`, `user_id` FK, `created_at` UTC, full visibility column set for a shareable entity. Register in `app/models/__init__.py`. Add a row to `backend-models-catalog.md`. Pattern: `backend-models-howto.md`.

2. **Migration**: `make migrate ARGS="revision --autogenerate -m \"add <table> table\""`, harden the output per `migrations-safety.md`, then `make migrate ARGS="upgrade head"`. Run `make test ARGS="tests/test_migration_integrity.py"`.

3. **API route**: `backend/app/api/v1/<area>.py`. Auth dependency (`get_current_user` / `require_admin`), Pydantic schemas for every field, named status codes (`HTTP_201_CREATED` on create, `HTTP_204_NO_CONTENT` on delete). Resolve entities through the visibility helpers (`user_visible_query()`, `get_manageable_entity()`); never a hand-written `or_(user_id, is_company_managed)`. Commit before answering (`await db.commit()` after the write and its `log_audit()` row, before the return). Pattern: `backend-api-patterns.md`.

4. **Frontend type**: `frontend/src/types/index.ts`. One interface per API response / request body, mirroring the Pydantic schemas. The name is the one the hook imports; do not duplicate a type the hook file already defines.

5. **Frontend hook**: `frontend/src/hooks/api/use<Area>Api.ts`. Queries through `useQuery` with a `queryKey` (lists: `entityListKey(["<area>"], scope)` from `@/lib/entity-list-scope`); mutations through `useMutation` with `onSuccess: () => qc.invalidateQueries({ queryKey: [...] })`. Pattern: `frontend/AGENTS.md` > "How to Add an API Hook".

6. **Frontend page**: `frontend/src/pages/<Area>.tsx`. Render through the shared components (`components/ui/`); server state only through the hook (invariant 5). For a new top-level page: `nav-model.ts` (`leaf()` or `aiLeaf()`, two levels max) + a `ResourceHub` for group hubs. Pattern: `frontend-pages.md`.

7. **Commands of the search field** (`>` prefix, if the feature adds a user action): `frontend/src/pages/<Area>.commands.ts` (or the page's own file). `defineCommands([...])`, one entry per action, `when` for gating, `navigate` for the target. Pattern: `frontend-command-palette.md`.

8. **Permissions**: the page reads `canManageEntity()` from `@/lib/entity-permissions` (not `@/lib/utils`). The route serves `can_manage`; the UI gates on it. A new manage tier or a new entity joins `CompanyManagedEntity` in `backend/app/api/v1/helpers.py`.

9. **Tests**: `make test ARGS="tests/test_<area>.py"` (backend, run as a non-admin for a shareable entity — rule 19), `cd frontend && yarn check` (unit + type check), `make e2e` (Playwright, against the running stack).

## Cross-cutting

- **Visibility** is three-tier everywhere (own / shared / company-managed). The backend serves `can_manage`; the frontend reads it through `canManageEntity()`. Never re-spell the predicate.
- **The one-owner rule** applies across the stack: a helper that belongs to the backend does not get a frontend mirror, and vice versa. If a rule is "one module", it is one module in each tree.
- **The `adding-features.md` checklist** (rules 1-99) is the backend half of this path; it is cited by number in the area `AGENTS.md` files. Read it before step 3.
