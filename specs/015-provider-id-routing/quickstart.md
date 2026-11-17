# Quickstart: validating provider-config routing

Prerequisites: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build` (platform :8100, frontend :3100), admin account, two
usable API keys for one vendor — or one vendor key plus a gateway fronting it.

## Setup: manufacture the collision

1. Settings → AI Models: add provider config **A** advertising `claude-sonnet-4-6`.
2. Add provider config **B**, created later, also advertising `claude-sonnet-4-6`.

Config B is now the newest row, so it wins every recency-resolved route today.

## US1 — a saved choice keeps routing where it was saved

US1 ships without a frontend change, so the three new references have no picker
yet. Save them over the API instead — the agent's own reference is settable
through the new-agent wizard, which already sends a composite choice:

```bash
# every administered model field lives on this one endpoint — /settings/platform
# is branding and ignores them (A_ID = config A's uuid)
curl -sX PUT localhost:8100/api/v1/settings/general-assistant \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{
    "llm_provider": "anthropic", "llm_provider_id": "'"$A_ID"'", "llm_model": "claude-sonnet-4-6",
    "agent_default_model": "claude-sonnet-4-6", "agent_default_provider_id": "'"$A_ID"'",
    "routing_model": "claude-sonnet-4-6", "routing_provider_id": "'"$A_ID"'",
    "fallback_model": "claude-sonnet-4-6", "fallback_provider_id": "'"$A_ID"'" }'
```

Omit a field to leave it alone; send `null` to clear it. Embeddings and images
already have their pickers.

1. Pick the **A** option for an agent's model, and save.
2. Send a chat turn; confirm the run's provider is A (run detail / backend log).
3. Add a third config **C** advertising the same id.
4. Send another turn; the provider is still A. Before this feature it becomes C.
5. Repeat for the platform default, routing, fallback, GA chat, embeddings, images.
6. Delete A; send a turn. It does not 500 — but note the reference does **not**
   dangle: `_cleanup_orphaned_model_references` nulls the reference and the
   provider name together, so resolution falls through to first-found,
   not to name resolution. FR-007's dangling-reference path is
   only reachable when a reference outlives its config, which the cleanup now
   prevents on all seven surfaces — it is covered by unit tests instead
   (`test_llm_router.py`, `test_llm_router_fallback_model.py`).

## US2 — the picker expresses the choice

1. Open each of the seven pickers. Each shows two options for the shared id, each
   naming its config's display name.
2. Choose one and close the picker: the trigger shows one option, not both
   concatenated.
3. Browser console shows no duplicate-key warning.

## US3 — overlap is visible

Saving config B (above) warns and names config A and `claude-sonnet-4-6`. The save
succeeds.

## Regression — deployments without a collision

With a single provider config, no saved choice changes provider, and a
hand-typed model id still resolves by prefix (SC-003, FR-006).

## Automated

```bash
cd backend && ruff check app/ && pytest tests/ -k "router or settings or general_assistant"
cd backend && pytest tests/test_migration_integrity.py
cd frontend && yarn check
```
