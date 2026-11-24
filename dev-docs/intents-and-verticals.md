# Intents & Verticals

Developer and admin reference for the intent routing and agent binding system in XTM One.

---

## Overview

The **Intents** system is an AI capability routing layer that decouples _what needs to be done_ (an intent) from _which agent does it_ (a binding). It enables:

- External platforms (OpenCTI, OpenAEV) to declare AI capabilities they expect Copilot to provide
- Admins to bind specific agents to those capabilities
- Priority-based routing when multiple agents cover the same intent
- Vertical (business domain) scoping so the same intent resolves differently by context
- Per-user overrides that users can personalize (unless locked by an admin)

A typical intent name looks like `opencti.report.summarize` or `global.assistant` — dotted, self-namespaced strings with no strict format requirement beyond uniqueness.

---

## Core Concepts

### Intent

A globally registered AI capability identified by a unique dotted name.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `name` | string (200) | Unique dotted name, e.g. `cti.container_summary` |
| `description` | string \| null | Human-readable description of the capability |

Intents are created in two ways:
- **Auto-created** when a platform registers and declares intents it uses (source: platform)
- **Manually created** by an admin via the UI or `POST /api/v1/intents`

### Vertical

A business domain label used to scope intent bindings. Examples: `cti`, `aev`, `fraud`, `fimi`.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `name` | string (50) | Unique label, e.g. `cti` |
| `source` | `"manual"` \| `"platform"` \| `"builtin"` | How it was created |
| `platform_identifier` | string \| null | Which platform auto-created it |

Verticals are orthogonal to intents — they are assigned directly on agents (`supported_verticals`) and used as a filter on bindings. A `null` vertical on a binding means it applies universally across all verticals.

### Intent Binding

The join between an intent and an agent, optionally scoped to a vertical.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `intent_id` | UUID | FK → `intents` |
| `agent_id` | UUID | FK → `agents` |
| `vertical` | string \| null | Scope (`null` = universal) |
| `user_id` | UUID \| null | `null` = global admin default; set = personal user override |
| `is_locked` | bool | If true, prevents non-admin users from overriding |
| `priority` | int | Base routing priority (higher = preferred) |
| `priority_overrides` | `{"vertical": int}` \| null | Per-vertical priority overrides |

Unique constraint: `(intent_id, vertical, user_id, agent_id)`.

### Platform Intent Declaration

A many-to-many join tracking which platforms declared which intents. Used for UI filtering ("show only intents from OpenCTI") and to drive the intent catalog returned at registration.

---

## Priority Resolution

When multiple agents are bound to the same intent, the routing layer picks the one with the highest **effective priority**:

```
effective_priority(binding, vertical) =
    priority_overrides[vertical]   if vertical is set and present in overrides
    binding.priority               otherwise
```

In the catalog returned to platforms, agents are sorted **descending** by effective priority. The first entry is marked `is_default: true` and is the agent the platform should use by default.

Example:

```
Intent: global.assistant
  Binding A: agent=opencti-assistant, vertical=cti, priority=5
  Binding B: agent=ctem-assistant,    vertical=null, priority=4

For a platform with vertical="cti":
  A → effective_priority = 5  (is_default: true)
  B → effective_priority = 4
```

These are the seeded values on main: each module assistant outranks the universal CTEM Assistant on its own vertical, so the OpenCTI, OpenAEV and OpenCRQ embedded chats open on their module assistant and a module question takes no transfer through a hub. The CTEM Assistant stays first among the universal bindings and one transfer away from every module assistant. The hop cost of that choice is pinned per golden task by `backend/tests/test_ootb_routing_eval.py` (see `dev-docs/ootb-routing-eval.md`).

---

## Admin/User Permission Model

| Action | Non-admin user | Admin |
|--------|---------------|-------|
| List intents / verticals | Yes | Yes |
| Create / update / delete intent | No | Yes |
| Create / update / delete vertical | No | Yes |
| Add binding (global, `user_id=null`) | No | Yes |
| Add binding (personal override, `user_id=self`) | Yes, unless locked | Yes |
| Set `is_locked` on binding | No | Yes |
| Delete own personal override | Yes | Yes |
| Delete any binding | No | Yes |

When a non-admin calls `PUT /intents/{id}/bind`, the backend automatically sets `user_id = current_user.id` and ignores the `is_locked` field. If the corresponding global binding has `is_locked=true`, the request is rejected with HTTP 403.

---

## API Reference

All endpoints require JWT authentication (`Authorization: Bearer <token>`). Admin-only endpoints additionally require `is_admin=true`.

### Intents

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/v1/intents` | User | List all intents |
| `GET` | `/api/v1/intents/{id}` | User | Get one intent with its bindings |
| `POST` | `/api/v1/intents` | Admin | Create a custom intent |
| `PUT` | `/api/v1/intents/{id}` | Admin | Update intent name / description |
| `DELETE` | `/api/v1/intents/{id}` | Admin | Delete intent (blocked if bindings exist) |
| `PUT` | `/api/v1/intents/{id}/bind` | User\* | Upsert a binding |
| `DELETE` | `/api/v1/intents/{id}/bind/{binding_id}` | User\* | Remove a binding |

\* Admins set global bindings (`user_id=null`); non-admins set personal overrides (`user_id=self`).

#### `GET /api/v1/intents` — query parameters

| Param | Type | Description |
|-------|------|-------------|
| `platform` | string | Filter to intents declared by this platform identifier |
| `vertical` | string | Filter to intents with at least one binding for this vertical |
| `agent_id` | UUID | Filter to intents bound to this agent |
| `status` | `"used"` \| `"unused"` | `"used"` = has bindings or platform declarations; `"unused"` = neither |

#### `POST /api/v1/intents` — request body

```json
{
  "name": "my.custom.intent",
  "description": "Optional description"
}
```

#### `PUT /api/v1/intents/{id}/bind` — request body

```json
{
  "agent_id": "<uuid>",
  "vertical": "cti",              // null for universal binding
  "priority": 5,                  // base priority (or override value if priority_override_vertical is set)
  "is_locked": false,             // admin only — ignored for non-admins
  "priority_override_vertical": "cti"  // optional — writes into priority_overrides[vertical] instead of base priority
}
```

The endpoint is an **upsert**: if a binding already exists for `(intent, vertical, user, agent)`, it updates priority / lock; otherwise it creates a new row.

### Verticals

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/v1/verticals` | User | List all verticals with binding counts |
| `POST` | `/api/v1/verticals` | Admin | Create a manual vertical |
| `PUT` | `/api/v1/verticals/{id}` | Admin | Rename vertical (cascades to bindings) |
| `DELETE` | `/api/v1/verticals/{id}` | Admin | Delete vertical (blocked if in use) |

Renaming a vertical via `PUT` cascades to all `intent_bindings` rows automatically.

---

## Platform Registration & Intent Sync

When an external platform calls `POST /register`, it can include an `intents` list and a `business_vertical`. The backend runs `_sync_platform_intents()`, which:

1. **Upserts the vertical** from `business_vertical` (source = `"platform"`)
2. **Upserts Intent rows** for each declared intent (creates if missing; updates description only if the existing row has none)
3. **Syncs `PlatformIntentDeclaration`** — adds new declarations, removes intents the platform no longer declares
4. **Builds and returns the intent catalog** — for each declared intent, queries global bindings (`user_id=null`) scoped to the platform's vertical + universal bindings, sorts by effective priority descending, marks the first as `is_default: true`

#### `RegisterRequest` shape (relevant fields)

```json
{
  "platform_identifier": "opencti",
  "platform_url": "https://demo.opencti.io",
  "business_vertical": "cti",
  "intents": [
    { "name": "opencti.report.summarize", "description": "Summarize a report" },
    { "name": "global.assistant", "description": "General-purpose assistant" }
  ]
}
```

#### `RegistrationResponse.intent_catalog` shape

```json
{
  "intent_catalog": [
    {
      "intent": "opencti.report.summarize",
      "description": "Summarize a report",
      "agents": [
        {
          "agent_id": "<uuid>",
          "agent_name": "OpenCTI Assistant",
          "agent_slug": "opencti-assistant",
          "agent_description": "...",
          "vertical": "cti",
          "priority": 5,
          "is_default": true,
          "is_locked": false,
          "enabled": true
        }
      ]
    }
  ]
}
```

The platform uses the catalog to know which agent to route each intent to. `is_default: true` identifies the recommended agent. Platforms should surface agents in `enabled: false` state as unavailable.

---

## Built-in Bindings (XTM One mode)

In `xtm_one` and `dev` platform modes, `seed_builtin_intent_bindings()` runs at startup and provisions a pre-defined set of intent × agent bindings from `BUILTIN_INTENT_BINDINGS` in `backend/app/agents/seed.py`.

`BUILTIN_INTENT_BINDINGS` is **not hand-maintained** — it is generated at import time by `_build_intent_bindings()`, which walks `BUILTIN_AGENTS` and reads each agent's `supported_intents` declaration (a dict mapping intent name → priority, or per-vertical `{vertical: priority}`), plus the editing-intent cross-product from `_EDITING_INTENTS` × `_EDITING_INTENT_AGENTS`. Intent descriptions come from the `_INTENT_DESCRIPTIONS` single-source-of-truth map.

The seed is **idempotent**:
- **Phase 1**: Removes any global binding (`user_id=null`) that is no longer in `BUILTIN_INTENT_BINDINGS`, then cleans up orphaned intents (no remaining bindings)
- **Phase 2**: For each entry, upserts the intent, the vertical (if specified), resolves the agent by slug, and creates the binding if missing

In `copilot` mode, the seed is skipped entirely (returns 0).

The current intent namespace (see `_INTENT_DESCRIPTIONS` in `seed.py` for the authoritative list) spans:
- **global.*** editing/assistant intents (`assistant`, `make_it_shorter`, `make_it_longer`, `fix_spelling`, `change_tone`, `summarize`, `explain`)
- **cti.*** — `nlq_search`, `container_summary`, `container_report`, `containers_digest`, `entity_activity`, `entity_forecast`, `entity_history`, `ttp_harvester`, `stix_harvester`, `stix_transformer`, `stix_consumer`
- **aev.*** — `attack_path_orchestrator`, `attack_path_additional_agent`, `detection_rules_generator`, `phishing_email_html_generator`, `phishing_landing_page_html_generator`, `message_generator`, `media_article_generator`
- **crq.*** — `control_gap_analysis`, `risk_scenario_generator`, `assessment_summary`

The `crq` vertical and the `crq.*` intents were named `grc` before the OpenCRQ rename. Alembic revision `c1crq2vertical1` moves stored rows (verticals, bindings and their priority overrides, intents with their declarations, agents' supported verticals and intents, registrations' business vertical), merging into rows already under `crq` without dropping a binding. A platform build that still sends `grc` is read under `crq` at registration and by `GET /intents/catalog` (`app/services/intent_aliases.py`, `RENAMED_VERTICALS`), so the old names are never recreated.

Bindings are scoped to the `cti` / `aev` / `crq` / `fimi` verticals (or universal) per each agent's `supported_intents`. Because the table is generated, consult `seed.py` rather than a hand-copied list here for the exact intent × agent × vertical rows.

---

## Frontend UI

### Intents page (`/intents`)

The main admin interface. Shows all registered intents as collapsible cards:

- **Intent card**: name, description, platform tags (which platforms declared it), binding count, and an "Unbound" warning badge if the intent was declared by a platform but has no agent bound to it
- **Expanded view**: bindings grouped by vertical. Within each group, agents are ordered by effective priority. If 2+ agents share the same intent+vertical, priority up/down buttons appear
- **Add Binding dialog**: pick an agent, select one or more verticals (or "All verticals"), creates one binding per selected vertical
- **Edit Binding dialog**: change the vertical scope of an existing binding

### Agent detail page — Intents tab

On any agent's detail page, the Intents section shows all intent bindings for that agent, grouped by intent name, with vertical scoping badges. Admins can add and remove bindings directly from this view.

---

## Database Schema

```
intents
  id, name (unique), description, created_at, updated_at

verticals
  id, name (unique), source, platform_identifier, created_at, updated_at

platform_intent_declarations
  id, platform_identifier (indexed), intent_id → intents(CASCADE)
  UNIQUE (platform_identifier, intent_id)

intent_bindings
  id, intent_id → intents(CASCADE), agent_id → agents(CASCADE)
  vertical, user_id → users(CASCADE), is_locked, priority, priority_overrides (JSON)
  UNIQUE (intent_id, vertical, user_id, agent_id)
```

Migrations:
- `fb72fd90e71b_add_intents_verticals_and_bindings.py` — initial schema + `supported_intents`/`supported_verticals` columns on `agents`
- `p1r2o3v4e5r6_add_priority_overrides_to_intent_bindings.py` — adds `priority_overrides` JSON column

---

## Adding a New Built-in Binding

Built-in bindings are derived from the agents, so you add them on the agent, not on a hand-edited list:

1. Add the intent to the agent's `supported_intents` in `BUILTIN_AGENTS` (`backend/app/agents/builtin_agents.py`). The value is the priority (an `int` for a universal binding) or a per-vertical map:

```python
# In the BUILTIN_AGENTS entry for your agent:
"supported_intents": {
    "my.new.intent": 0,                 # universal (vertical=None)
    "global.assistant": {"cti": 10},    # per-vertical priority
},
```

2. Add a human-readable description for the intent to `_INTENT_DESCRIPTIONS` in `backend/app/agents/seed.py`.

3. If the agent serves several intents, give the new one its own section in the agent's persona (`backend/app/agents/seed_data/agent_persona/<slug>.md`, headed `(intent: <name>)`): a platform calls the agent by slug and never passes the intent, so the agent picks its mode from the request text alone.

4. The next startup in `xtm_one` / `dev` mode regenerates `BUILTIN_INTENT_BINDINGS` (via `_build_intent_bindings()`) and creates the intent, vertical (if needed), and binding automatically. Removing the intent from `supported_intents` lets Phase 1 of the seed clean up the old binding on the next startup.

> No migration is needed for adding/removing built-in bindings — they are managed entirely at the application layer. The six shared editing intents are cross-producted onto the vertical agents via `_EDITING_INTENTS` × `_EDITING_INTENT_AGENTS`.

---

## Relevant Files

| Path | Role |
|------|------|
| `backend/app/models/intent.py` | SQLAlchemy models: `Intent`, `Vertical`, `IntentBinding`, `PlatformIntentDeclaration` |
| `backend/app/api/v1/intents.py` | CRUD routes for intents, bindings, and verticals |
| `backend/app/api/v1/platform.py` | `RegisterRequest`, `IntentCatalogEntry`, `_sync_platform_intents()` |
| `backend/app/agents/builtin_agents.py` | `BUILTIN_AGENTS[*].supported_intents` |
| `backend/app/agents/seed.py` | `_INTENT_DESCRIPTIONS`, `_build_intent_bindings()` → `BUILTIN_INTENT_BINDINGS`, `seed_builtin_intent_bindings()` |
| `frontend/src/pages/Intents.tsx` | Admin Intents management page |
| `frontend/src/pages/agent-detail/config/IntentsSection.tsx` | Intent bindings section on agent detail |
