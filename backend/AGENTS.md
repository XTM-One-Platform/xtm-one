# Backend – Developer Guide

Read before changing `backend/`. The nearest `backend/app/<area>/AGENTS.md` adds the rules of its area; detail lives in `dev-docs/reference/`.

## Tech Stack

- **Python 3.14** with async/await throughout
- **FastAPI** – web framework
- **SQLAlchemy 2.0** (async) – ORM with `Mapped[]` type annotations
- **Alembic** – database migrations
- **PostgreSQL 17 + pgvector** – relational data + vector embeddings
- **Redis 8** – task queue (SAQ), pub/sub (SSE activity feed), caching
- **SAQ** – async background job queue (redis-based)
- **croniter** – cron expression parsing for assignment scheduling
- **Anthropic SDK + OpenAI SDK + Google GenAI SDK** – LLM providers (direct, no LangChain)
- **MCP SDK** – Model Context Protocol tool servers
- **Authlib + httpx** – OAuth 2.0 flows
- **Cryptography (Fernet)** – secret encryption at rest
- **PyJWT** – JWT authentication
- **Pydantic** – request/response validation

## Where to look

| Need | Read |
|---|---|
| File-by-file map, request and assignment execution flow | `dev-docs/reference/backend-directory-map.md` |
| Shared helpers (one rule, one module) | `dev-docs/reference/backend-shared-utilities.md` |
| API route prefixes | `dev-docs/reference/api-routes.md` |
| Environment variables | `dev-docs/reference/backend-configuration.md` |
| Migrations, full rules and idioms | `dev-docs/reference/migrations-safety.md` |
| Test suites, sharding and tripwires | `dev-docs/reference/backend-testing.md` |
| Full-stack feature (API → hook → page) | `dev-docs/reference/full-stack-feature.md` |

**Layering.** `app/api/` calls the services, the channels and the tools; they never import a private (underscore) helper of the API layer. Such a helper moves to a service module under a public name, and the API imports it from there (`tests/test_layering_private_api_imports.py`, a ratchet over the imports that predate it).

## Database migrations (safety) - READ before touching `alembic/` or `models/`

The platform auto-migrates on startup across several replicas, so a bad migration breaks every booting backend. Full rules, rationale and idioms: `dev-docs/reference/migrations-safety.md` (rules cited by number).

1. **Single head**: chain off the current head; after a rebase, re-point your earliest `down_revision` and rerun `make test ARGS="tests/test_migration_integrity.py"`.
2. **Idempotent**: `IF NOT EXISTS` / `IF EXISTS` guards everywhere; fresh installs run `create_all` + `stamp head`, never the body.
3. **Concurrent startup is already guarded** by the advisory lock: never add a migration lock or run DDL outside Alembic.
4. **Lock-light**: no table rewrite, no `SET NOT NULL` on a large table, no unbounded backfill, never `CREATE/DROP INDEX CONCURRENTLY` (use `create_index_in_place()`).
5. **Lock-safe**: every lock-taking DDL (FK-bearing `CREATE TABLE` included) follows the recipe - idempotency probe + early return, `terminate_lock_holders(conn, "<table>")`, `set_lock_timeout(conn)` (`app.core.migration_ddl`), then the guarded DDL; enforced by `test_lock_taking_ddl_sets_lock_timeout`.
6. **Reversible, no exceptions**: a working `downgrade()` for every migration (CI walks the last 5).

## Configuration

All settings are in `app/core/config.py` (`dev-docs/reference/backend-configuration.md`). **Integration credentials are NOT in env vars**: they are stored per integration in the DB, configured via the UI. Log an exception with `format_exc(e)`, never a raw `%s`, and never log a secret.

## Tests

Details: `dev-docs/reference/backend-testing.md`. `make test` (repo root) runs the unit suite, unsharded, from a `uv`-synced `backend/.venv`; narrow it with `ARGS="tests/test_<name>.py -k name"`.

- CI runs under pytest-xdist: deterministic `parametrize` ids, PostgreSQL tests through `tests/scratch_database.py`, source tripwires through `tests/app_sources.py`, a fixed resource keeps its module on one worker (`xdist_group`).
- A red tripwire means a rule was re-spelled outside its owning module: move the code to the owner, never patch the tripwire.
- Unit tests meet no Redis: a test of Redis-backed code fakes `redis_client`; one that needs the real one is an `integration` test.
- `tests/live/` (`live_llm`) hits the real provider APIs only with `LIVE_LLM_SMOKE=1`; run it after changing what a provider sends.

## Seeded automations

A seeded automation never ships enabled; the customer opts in (agentic runs meter the customer's quota). A new `BUILTIN_ASSIGNMENTS` entry spreads `SEEDED_FLEET_OPT_IN` (`app/agents/fleet_opt_in.py`); `tests/test_ctem_fleets_opt_in.py` fails on one enabled outside `ENABLED_BY_DEFAULT_ALLOWLIST`.

## Dead code

`tests/test_no_dead_code.py` runs vulture over `app/`: a function, class or method nothing names (in `app/`, `tests/`, `scripts/`, `alembic/`) fails the build. Vulture reads names, not strings: before deleting, check for a reader it cannot see (a computed `getattr` name, a string registry, `importlib`); then delete it and the docs that describe it. A name a test uses keeps a definition alive, so dead code with a unit test is not reported. Only a decorator of `REGISTERING_DECORATORS` (routes, validators, event listeners) keeps a definition out of the report; a method a library calls by name goes to `LIBRARY_HOOKS`, and `KNOWN_DEAD` only shrinks. A comment must not start with `# type:`: Python reads it as a type annotation and the module can no longer be parsed.

## Adding Dependencies

`uv add <package>==<version>` (dev-only tools: `uv add --dev`): it writes the exact pin to `pyproject.toml` and relocks `uv.lock`; commit both. `uv sync` installs the locked set into `backend/.venv`; CI and the images install with `--locked`, so a `pyproject.toml` edited without relocking fails them. Rebuild the Docker image after.
