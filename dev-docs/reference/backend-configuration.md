# Backend configuration

Infrastructure environment variables read by `backend/app/core/config.py`. A new `.env.sample` variable is also documented in `docs/docs/` (CI-enforced).

## Environment Variables

All config is in `app/core/config.py`. Key vars:
- `DATABASE_URL` – PostgreSQL async connection string
- `REDIS_URL` – Redis connection string
- `SECRET_KEY` – Used for JWT + Fernet key derivation (MUST be strong in production)
- `BASE_URL` – Backend URL for OAuth callbacks
- `FRONTEND_URL` – Frontend URL for redirects
- `BASE_PATH` – URL path prefix when a reverse proxy serves the platform under `https://host/xtm-one/`; empty = the root. `BASE_URL` / `FRONTEND_URL` must carry the same prefix. Unreserved ASCII segments only; a prefix under a root namespace or a public file name is refused (`RESERVED_FIRST_SEGMENTS`, pinned against `application.routes` + `frontend/public` by `tests/test_base_path.py`); one that is also a platform page (`SPA_FIRST_SEGMENTS`, pinned against `frontend/src/App.tsx`) is refused in `BASE_PATH_MODE=auto` and accepted once the proxy is declared (`forwarded` / `stripped`). See `app/core/base_path.py`: `BasePathMiddleware` normalises both proxy shapes (an `X-Forwarded-Prefix` header naming the prefix marks a stripped request, else the path decides), a path-keyed middleware reads `route_path(scope)`, a redirect or an HTML-embedded URL goes through `with_base_path()`; the MCP OAuth well-known documents answer the RFC 8414 / 9728 path-inserted forms too.
- `CONTENT_SECURITY_POLICY_MODE` - `report-only` (default: `Content-Security-Policy-Report-Only` on every HTML document, nothing blocked, violations reported to `/api/v1/csp-report`), `enforce` (the same policy, enforced) or `off`; a typo fails startup. `CONTENT_SECURITY_POLICY_FRAME_ANCESTORS` adds origins allowed to frame the platform once enforced. See `app/core/csp.py`.
- `BRAND_ASSETS_EXTRA_DIRS` - comma-separated folders of more brand asset packs, read after the bundled `app/agents/seed_data/brand_assets` (`template_asset_seed.brand_asset_roots()`). For a deployment that adds packs from a folder it mounts (local development in Copilot mode can point it at a checkout of the packs); Filigran's Copilot reads its private logo libraries through the brand asset source instead (`services/brand_asset_source.py`, Settings > Platform).
- `ENABLED_FEATURES` – comma-separated feature flag ids to enable (`*` = all). Unset/empty = none enabled (fail-closed). See `app/core/feature_flags.py`.
- `LLM_PROVIDER_{PROVIDER}_API_KEY` / `LLM_PROVIDER_{PROVIDER}_MODELS` – env-seeded LLM providers (upserted on every boot, `is_from_env`). `LLM_PROVIDER_MODELS_EDITABLE` (default `false`) decides whether the `*_MODELS` list is pinned (overwrites the catalog on every boot, UI editor hidden) or an admin-curated baseline (delta-merged on every boot). Unset `LLM_PROVIDER_FILIGRAN_GATEWAY_MODELS` on an install with no other chat provider: the seed lists the gateway's chat models instead. Single source of truth: `app/llm/env_models.py` — see `dev-docs/reference/backend-llm-routing-and-models.md` > "Env-seeded providers".

**Integration credentials are NOT in env vars** — they are stored per-integration in the DB, configured via the UI.
