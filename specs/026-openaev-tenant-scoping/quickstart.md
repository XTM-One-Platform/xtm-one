# Quickstart: Validate OpenAEV Tenant Scoping

## Automated

```bash
cd backend
pytest tests/test_openaev_tenant_scoping.py tests/test_openaev_package_tools.py
ruff check app/
```

Expected: pass. The new tests cover each state in [data-model.md](data-model.md) and every row of [contracts/path-scoping.md](contracts/path-scoping.md), assert one settings fetch per tool set (SC-003), exercise a read and a write in every tool module (SC-005), and fail if a new direct HTTP call appears outside the scoped helpers.

## Live (multi-tenant OpenAEV)

Prerequisite: OpenAEV with `default_tenant_id` in `GET /api/settings`, integration bound in the dev stack (`docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build`).

1. Run the `factory-scenario-creator` agent: create scenario → add inject → launch.
   Expected: no `TENANT_WRITE_SCOPE`; requests hit `/api/tenants/{id}/...` (OpenAEV access log).
2. Call one tool per group (assets, teams, access, taxonomy, content, testing, injects).
   Also exercise `fulltextsearch`, `vulnerabilities/external-id`, `catalog-connector`, `connector-instances`, `injectors|collectors|executors`, and `manage_openaev_profile` (must stay on `/api/me*`).
   Expected: no 404 from a missing twin; any 404 is a gap in R2 of [research.md](research.md).
3. Single-tenant or restricted token: repeat step 1 reads.
   Expected: identical paths to before; on a forced failure, a `TENANT_WRITE_SCOPE` rejection carries the lookup hint.
