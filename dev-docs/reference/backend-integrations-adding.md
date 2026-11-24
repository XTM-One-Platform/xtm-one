# Integrations: adding a provider, catalog, lifecycle and Test Connection

The step-by-step recipe for a new provider, the provider catalog and its categories, lifecycle hooks and the two Test Connection contracts; the rules live in `backend/app/integrations/AGENTS.md`.

## Provider Catalog

The backend exposes `GET /api/v1/integrations/catalog` which lists available provider types and the fields each one needs. This powers the frontend integration creation form. The catalog is defined in `backend/app/api/v1/integration_catalog.py`.

## How to Add a New Integration

### Step 1: Create the provider file

The contract is a module-level `build_tools(token, executor, **kwargs)` that registers the tool callables on the executor and returns their `ToolDefinition`s; `token` is the decrypted credential and `kwargs` carries what the funnel hands every builder (`refresh_fn`, and per provider `persist_file_fn`, `integration_id`...). A `BaseIntegration` subclass (`integrations/base.py`) is optional. Every call goes through `safe_request`, which answers `(response, None)` or `(None, error)`, the error already worded for the model (shape of `providers/greynoise.py`):

```python
# backend/app/integrations/providers/acme.py
"""Acme integration - asset lookups. Auth: API key in the ``X-Api-Key`` header."""

from __future__ import annotations

from typing import Any

from app.core.egress import egress_client
from app.integrations.http import TRANSIENT_RETRY_STATUSES, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "Acme"
_SOURCE = "integration:acme"
ACME_API = "https://api.acme.example/v1"


def build_acme_headers(api_key: str) -> dict[str, str]:
    """The one header builder, shared with the connectivity test."""
    return {"X-Api-Key": api_key, "Accept": "application/json"}


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    api_key = (token or "").strip()
    if not api_key:
        return []
    headers = build_acme_headers(api_key)

    async def get_acme_asset(asset_id: str) -> str:
        """Read one asset."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{ACME_API}/assets/{asset_id}",
                _PROVIDER,
                f"get asset {asset_id}",
                headers=headers,
                retry_statuses=TRANSIENT_RETRY_STATUSES,  # a read: idempotent
            )
        if err:
            return err
        data = resp.json() or {}
        return f"Asset {data.get('name', asset_id)}: {data.get('status', 'unknown')}"

    executor.register("get_acme_asset", get_acme_asset)
    return [
        ToolDefinition(
            name="get_acme_asset",
            description="Read one Acme asset by id: name and status.",
            input_schema={
                "type": "object",
                "properties": {"asset_id": {"type": "string"}},
                "required": ["asset_id"],
            },
            source=_SOURCE,
        ),
    ]
```

Register the builder in `PROVIDER_TOOLS` (`services/integration_tools.py`). The catalog introspects every builder with a dummy credential (`build_dummy_token()` in `integration_catalog.py`, pinned by `tests/test_dummy_token_shapes.py`): a builder that parses a compound token (`base_url||api_key`, assembled by its branch of `_get_token`) is listed there with the shape it validates.

### Step 2: Add to provider catalog

In `backend/app/api/v1/integration_catalog.py`, add to `PROVIDER_CATALOG`:

```python
PROVIDER_CATALOG = {
    ...,
    "acme": {
        "name": "Acme",
        "description": "Acme asset inventory - asset lookups",
        "logo": "/logos/acme.svg",
        "category": "integration",
        "integration_category": "exposure_management",
        "integration_types": ["asset_management"],
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "setup_guide": {"title": "Acme API Key Setup", "steps": [...], "permissions": [...]},
    },
}
```

No env vars: users paste the credential in the UI. An OAuth provider sets `auth_type: "oauth"`, `fields: ["client_id", "client_secret"]` and its `authorize_url` / `token_url` / `scopes` (the `jira` entry).

A provider with several `supported_auth_types` set up apart gives each mode its own guide in `auth_setup_guides` (`{"oauth": guide, "api_key": guide}`, the `planhat` entry): the setup guide button shows the mode chosen in the form or stored on the integration (`setupGuideFor()`, `frontend/src/lib/catalog-variants.ts`), and `setup_guide` (covering every mode) where no mode is chosen yet or a mode has none. Placeholders are filled as in `setup_guide`.

**Every catalog entry MUST set `integration_category` to one of the marketplace
taxonomy values** (they drive the faceted sidebar and category sections on
`/integrations/new` — an unknown value gets its own facet under a trailing
"Other" sidebar group, labeled with the title-cased category id and no icon):

| Domain | Valid `integration_category` values |
|--------|-------------------------------------|
| Cybersecurity | `threat_intelligence`, `siem_analytics`, `detection_response`, `exposure_management`, `adversarial_validation`, `identity_access`, `grc` |
| AI & Research | `ai_models`, `search_news` |
| Business & Productivity | `communication`, `social_media`, `business_operations`, `hr` |
| Engineering & Operations | `development`, `automation`, `observability` |

Adding a brand-new category requires updating `INTEGRATION_CATEGORY_LABELS`,
`INTEGRATION_CATEGORY_ICONS`, and `INTEGRATION_CATEGORY_GROUPS` in
`frontend/src/lib/constants.ts` in the same PR.

An integration built on an open-source project (its engine, rule format or rule set, as Nova is on NOVA) credits it with `setup_guide.open_source`: `name` (the project's own, not the integration's), `url`, optional `author` and `license`, copied from the project's repository and licence file. `SetupGuideOpenSource` accepts only an absolute `https://` URL with an ASCII host and no userinfo (`safe_link_url()` in `api/v1/schemas/integration.py`), and the setup guide dialog appends "Built on {project}, an open-source project by {author}." to its subtitle. Imported bundles cannot set the credit, and their console `url` is kept only when `safe_link_url()` accepts it as http(s).

### Lifecycle hooks (`lifecycle.py`)

`register_lifecycle(provider, on_connect=, on_disconnect=, on_update=)`: `on_connect` runs when the integration lands connected, `on_disconnect` when it is deleted, `on_update` after every `PUT /integrations/{id}` once the edit is flushed, handed an `IntegrationUpdate` (the actor, the request, the route's `AfterCommit` for a cache drop). `on_connect` / `on_disconnect` are best-effort and never raise; `on_update` keeps state in step with the integration, so its failure fails the edit (409, nothing committed). Nova seeds its rule source on connect; OpenRouter copies a replaced API key into the LLM provider it feeds on update (`services/openrouter_llm_provider.py`, registered by the import in `api/v1/integrations.py`).

### Step 3: Use in an assignment

An agent's `integration_bindings` (likewise a flow node's) name the integrations it uses. At run time `build_integration_tools()` (`services/integration_tools.py`) loads the bound rows, resolves each credential with `_get_token()` (decrypted, an OAuth token refreshed inline when it is about to expire, the acting person's own credential in `per_user` mode) and calls the provider's `PROVIDER_TOOLS` builder, so its tools join the agentic loop of chats and assignment runs.

## Connectivity Testing (`Test Connection`)

`POST /integrations/{id}/test` answers "does this integration actually work?".
There are two contracts, and picking the wrong one produces a green banner that
verified nothing.

**Default — an authenticated HTTP call.** Add the provider to the `checks`
table in `app/api/v1/integration_testing.py` as a `(url, headers)` pair (or a
dedicated branch when the credential is not a bearer token). The status code is
the verdict. Right for every integration whose health *is* "can we reach their
API with this credential" — nearly all of them.

**Probe — internal state.** Some integrations have no endpoint to call and no
credential to validate: their health lives in our own database or runtime
(synced rules, an installed engine, a local dataset). Those register a probe
with `app/integrations/connectivity.py` instead of growing an `elif` in the
central helper:

```python
# backend/app/integrations/providers/nova.py
from app.integrations.connectivity import ProbeContext, ProbeResult, register_connectivity

async def check_connectivity(ctx: ProbeContext) -> ProbeResult:
    ...  # ctx.db and ctx.integration are populated by the test endpoint
    return ProbeResult.degraded("12 active rules, keyword tier only — …")

register_connectivity("nova", check_connectivity)
```

A registered probe **owns the entire verdict**: it runs before credential
resolution, before the `checks` table, and before the `"appears configured"`
fallback. That is what makes credential-less providers testable — without it
the endpoint stops at `"No credentials stored for this integration"` and the
probe is never reached.

Three outcomes, no more: `ProbeResult.ok` / `.degraded` (wire status `info` —
the frontend's blue "works, with caveats" banner) / `.failed`. A probe must be
read-only and cheap (it runs on a button press), must never echo a credential
into its message, and cannot hang or raise — the dispatcher enforces a timeout
and captures exceptions so the endpoint's never-raises contract holds.

An `ok` or `info` verdict self-heals a stale `not_connected` status for
credential-less integrations.

The test endpoint only requires that the caller can *see* the integration, so
a probe must not quote text kept for the people who fix it - a stored sync
error - unless `ctx.caller_can_manage` is true (set by the endpoint from
`helpers.can_manage`; `False` when the caller is unknown). Anyone else gets a
fixed sentence: Nova serves its last sync error through
`nova_rule_sync.served_sync_error()`, the helper its admin-only rule source
routes use too.
