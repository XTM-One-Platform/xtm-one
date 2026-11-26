# Models – Developer Guide

Read before changing `backend/app/models/`: the SQLAlchemy 2.0 ORM models, one file per entity family, registered in `app/models/__init__.py`. Migration rules are in `backend/AGENTS.md`; detail lives in `dev-docs/reference/`.

## Where to look

| Need | Read |
|---|---|
| Every model: table, file, purpose, per-model constraints | `dev-docs/reference/backend-models-catalog.md` |
| Step-by-step new model, company-managed pattern, conventions, `Agent` columns (full text) | `dev-docs/reference/backend-models-howto.md` |
| Entity contracts, three-tier visibility, sharing tiers | `dev-docs/reference/entities.md` |
| Migrations: full rules and idioms | `dev-docs/reference/migrations-safety.md` |
| Shareable entity checklist (rule 19) | `dev-docs/reference/adding-features.md` |

## Rules

The full conventions and per-model constraints live in `dev-docs/reference/backend-models-catalog.md` and `backend-models-howto.md`. The binding rules are:

- **Registry**: every model file gets one `import app.models.<file>` line in `app/models/__init__.py`; the app, the SAQ worker and Alembic all import models from there.
- **Migration**: never commit an `--autogenerate` output as is; harden it per `dev-docs/reference/migrations-safety.md` and run `tests/test_migration_integrity.py`.
- **Shareable entity**: declare the full set (`is_company_managed`, `shared_group_ids`, `shared_group_permissions`, `shared_user_ids`, `shared_user_permissions`, `access_restricted`), ideally by inheriting `VisibilityColumns` (`app/models/visibility.py`), never a partial set, and add the model to the `CompanyManagedEntity` union in `app/api/v1/helpers.py`; test it as a non-admin (`tests/test_visibility_helpers.py`).
- **Company-managed** = `is_company_managed = True`, which alone decides visibility, editable by admins only; `user_id` always keeps the owner (`resolve_company_managed_owner_id` for seeded rows).
- **Primary key**: a UUID `id`, auto-generated (`default=uuid.uuid4`).
- **Ownership**: a shareable or company-managed model has a nullable `user_id` FK (NULL only on legacy or system-managed rows); a strictly per-user record declares it `nullable=False`.
- **Timestamps**: `DateTime(timezone=True)` with UTC defaults (`datetime.now(UTC)`).
- **Typing**: `Mapped[]` annotations with `mapped_column` (SQLAlchemy 2.0 style).
- **Secrets**: Fernet-encrypted, in a column ending in `_enc` (e.g. `access_token_enc`).
- **JSON**: `postgresql.JSON`; vectors: `pgvector.sqlalchemy.Vector()`, dimension-agnostic (the embedding dimension is configured).
- **Platform-wide state** lives on `PlatformSettings`: Terms of Use acceptance (never per user), agent default model, routing / fallback model, embedding / image defaults, MCP endpoint and AI-adoption config, the XTM Hub signing key version (`xtm_hub_key_version`, `core/xtm_hub_key.py`); `GeneralAssistantConfig` keeps the General Assistant's own chat model (`llm_model`), system prompt, and tool / KB / integration bindings.

## Adding a model

1. Create `app/models/<name>.py` (template in `backend-models-howto.md`): UUID `id`, `user_id` FK `ON DELETE CASCADE` with `index=True`, `created_at` UTC.
2. Add its import to `app/models/__init__.py`.
3. `make migrate ARGS="revision --autogenerate -m \"add <table> table\""`, harden the output, then `make migrate ARGS="upgrade head"`.
4. Shareable? Full visibility column set + `CompanyManagedEntity` + non-admin tests.
5. Add a row to `dev-docs/reference/backend-models-catalog.md`.

## Adding a column to Agent

`app/models/agent_copy.py` decides which `Agent` and `Assignment` columns travel on a copy (duplicate, export / import bundle, catalog clone). Put every new column in one bucket of each manifest; `tests/test_agent_copy_manifest.py` fails until you do. Copying is the default; the skip lists hold identities, secrets, sharing grants, activation state and references that only resolve on the source instance.
