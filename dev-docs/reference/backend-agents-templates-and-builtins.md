# Agents area: templates, built-ins, triggers and key files

The agent model at a glance, the template payload, built-in agents and assignments, trigger config shapes and key files of `backend/app/agents/`; the rules live in `backend/app/agents/AGENTS.md`. The template catalog, assignment execution and trigger semantics are in `dev-docs/reference/agents-and-assignments.md`.

## Architecture

```
Agent (user-configured or company-managed)
├── persona (system prompt: who the agent is)
├── is_company_managed (shared with all users if true)
├── integration_bindings (which service accounts to use)
├── mcp_server_ids (which tool servers to connect)
├── knowledge_base_ids (which RAG sources to search — all readable)
├── knowledge_base_permissions ({kb_id: "read"|"write"} — "write" adds store_knowledge for that KB)
├── handover_agent_ids (agents this agent can hand off to)
├── memory_short_term_enabled / memory_medium_term_enabled / memory_persistent_enabled (3-tier memory)
├── llm_provider / llm_model (optional override for this agent)
├── web_enabled / web_token (public web chat embed)
└── assignments[]
    ├── trigger (schedule / webhook / integration_event / manual)
    ├── prompt (what the AI should do)
    ├── delay (optional: "10m", "1h")
    ├── human_in_the_loop (require approval before actions)
    └── tool_sources (override MCP servers for this assignment)
```

The model an agent runs on is resolved as described in `dev-docs/reference/entities.md` > "Multi-Model Support".

## Agent Templates

The template catalog (the general-purpose templates and the `cti-*` ones), the platform modes each ships in (`platform_targets`), `default_skills` and manual inputs: `dev-docs/reference/agents-and-assignments.md` > "Agent Templates". The code source is `BUILTIN_TEMPLATES` in `backend/app/agents/templates.py`, which appends `CTI_TEMPLATES` from `backend/app/agents/cti_templates.py` (each CTI template is built from `seed_data/cti_templates/<slug>/`: `persona.md`, `assignments.json` and one prompt file per assignment).

### Adding a New Template

**Via the API (recommended):**

```bash
POST /api/v1/agents/templates
{
  "slug": "my-agent",
  "name": "My Agent",
  "description": "Does something useful.",
  "icon": "Sparkles",
  "category": "productivity",
  "required_integrations": ["google"],
  "recommended_mcp": ["some-mcp"],
  "default_skills": ["some-skill-slug"],
  "default_persona": "You are a helpful assistant that...",
  "default_assignments": [
    {
      "name": "My Automation",
      "trigger_type": "webhook",
      "trigger_config": {"path": "my-hook"},
      "prompt": "When data arrives: {{trigger.payload}}. Analyze and act.",
      "delay": "5m"
    }
  ]
}
```

Requires admin privileges; the template is available to all users at once. The full field set is `CreateTemplateRequest` (`backend/app/api/v1/schemas/agent.py`); the routes are in `backend/app/api/v1/agent_templates.py`.

**Via seed data (built-in templates):** add the entry to `BUILTIN_TEMPLATES` in `templates.py` with its `platform_targets` (no targets means XTM One and dev, `seed.seed_matches_platform`); a CTI template is a `_TEMPLATE_META` entry in `cti_templates.py` plus its `seed_data/cti_templates/<slug>/` directory. The next boot inserts it.

## Built-in agents and their slugs

Built-in agents (`BUILTIN_AGENTS` in `builtin_agents.py`, including the per-stage CTEM analysts it appends) are upserted by slug on every boot (`seed.seed_builtin_agents`), each in the modes its `platform_targets` name (`seed_matches_platform`, the rule of the skill and template seeds too; XTM One and dev without any). An agent listed in `FEATURE_GATED_BUILTIN_AGENTS` is seeded only while its feature flag is on; off, it is left out (a copy seeded earlier is removed as stale) along with the handovers and persona bullets the other built-ins give it (`seed._seeded_builtin_agents`).

`DEFAULT_ASSISTANT_SLUGS` names the assistants a person reaches without choosing an agent: the CTEM Assistant (the General Assistant in XTM One mode), the XTM One assistant, and every agent bound to `global.assistant` on a vertical (what the embedded chat of OpenCTI, OpenAEV and OpenCRQ talks to). Each carries `DEFAULT_ASSISTANT_MEDIA_TOOL_IDS` (files, reports, decks, videos, template lookup), and the craft skills of those tools (`presentation-design`, `video-production`, `report-design`) bind to these slugs and to the General Assistant (`bind_to_general_assistant`). A new vertical assistant joins the tuple; `tests/test_default_assistant_media.py` fails when one is missing. A built-in skill named in `FEATURE_GATED_BUILTIN_SKILLS` is seeded only while its flag is on (`video-production` behind `VIDEO_STUDIO`); off, the skill seed's full sync removes it (`seed._seeded_builtin_skills`).

One baseline on every default entry point: each slug of `DEFAULT_ASSISTANT_SLUGS` also carries `DEFAULT_ASSISTANT_PLATFORM_TOOL_IDS` (`agent_builder` and `fleets`, so agents, assignments, skills, knowledge bases and fleets are built in place and `get_fleet_health` is read without a transfer), the skills of `DEFAULT_ASSISTANT_BASELINE_SKILL_SLUGS` (the fleet skills, until then an orchestrator's alone, plus the media craft skills; each also `bind_to_general_assistant`) and the knowledge bases of `DEFAULT_ASSISTANT_BASELINE_KB_SLUGS`; `DEFAULT_ASSISTANT_MODULE_SKILL_SLUGS` adds an assistant's own skills (`cti-orchestrator` on the CTI assistants), written onto the skill entries at import. `tests/test_default_assistant_baseline.py` pins all of it. The `xtm-one-documentation` knowledge base is seeded from the documentation the image ships (`bundled_docs: true`; the Dockerfile copies `docs/` to `/app/xtm_one_docs`, `XTM_ONE_DOCS_DIR` overrides, a source checkout reads its own `docs/`; `seed.bundled_docs_dir`), every Markdown file under its relative path, re-seeded at every boot when a file changed: the repository is private, so a `github_public` source of it never synced.

Module documentation knowledge bases (#12343): `opencti-documentation` and `openaev-documentation` sync `docs/` of the product repository anonymously (`OpenAEV-Platform/docs` is archived, so the OpenAEV source points at `OpenAEV-Platform/openaev` path `docs`); `opencrq-documentation` is seeded from `seed_data/knowledge_base/opencrq-documentation/` because `OpenCRQ-Platform/opencrq` is private. Each is bound to its module assistant and to `ctem-assistant`; refresh the OpenCRQ copy from `OpenCRQ-Platform/opencrq` `docs/` when it changes.

A new built-in is created `is_builtin`, company-managed, with `user_id` set to the company-managed owner (`helpers.resolve_company_managed_owner_id`: the `ADMIN_EMAIL` user, else the oldest admin, else the oldest user); an adopted row with no owner gets that owner. A row already on the slug is adopted only when it is company-managed. A person's own agent holding the slug (created before that built-in shipped, or adopted by an earlier boot and still private) is never adopted: the seed moves it to a free slug (`generate_unique_agent_slug`, logged), clears `is_builtin`, and creates the built-in. The stale sweep of retired built-in slugs likewise deletes only company-managed rows and hands a private one back as an ordinary agent. The persona and the tracked config fields of a built-in keep their version history (`agent_config_versioning`): a built-in the seed creates gets the "Initial version" of each, and each one a boot changes is versioned with no author and the commit message "Updated by the built-in agent seed (release <APP_VERSION>)"; a value the seed leaves unchanged writes nothing, so a boot without a content change records no version, and the boot's `bootstrap_lock` keeps several replicas from recording the same change twice.

### Mechanics and customer layer (#914)

A built-in agent has two layers (`app.services.builtin_customer_layer`).

- **Mechanics**, product-owned and locked: persona core, name and card, members and roles, handovers, gates and their defaults, assignments, built-in tool groups, deny list, guardrails, config. `seed._sync_agent_fields` rewrites them on every boot. `PUT /agents/{id}` refuses a change to any of them in XTM One mode with `403 "Built-in agents cannot be modified: <fields> is locked by the product ..."`; a field re-sent with its stored value (a whole form) is not a change.
- **Customer layer**, customer-owned and never written by the seed: integration bindings and their scoping (`INTEGRATION_LAYER_FIELDS`, diffed by `_is_integration_binding_change`), MCP servers and their tool scoping, knowledge bases and their read or write grant, skills and their load modes, custom tools, the model (`MODEL_LAYER_FIELDS`), `customer_notes`, `enabled`, `approve_tool_calls`, `hide_activity_from_non_admins` (`CUSTOMER_LAYER_FIELDS`). Whoever may manage the built-in writes it (an administrator: built-ins are company-managed).
- **Organization notes** (`agents.customer_notes`, at most `CUSTOMER_NOTES_MAX_CHARS`): appended after the seeded persona under `CUSTOMER_NOTES_HEADING` by `persona_with_customer_notes`, in chat (`agent_chat`) and in runs (`assignment_helpers`); never replaces the core. Refused 422 on an agent that is not built-in.
- **What the seed keeps** (`agents.customer_fields`, migration `c914builtincustlayer1`): the customer-layer fields a person set. The seed would otherwise write three of them: the model (`seed_keeps_model` skips it once pinned; clearing every model field hands it back to the release) and the template-bound providers with their read-only set (`SEEDED_DEFAULT_FIELDS`, `seed_keeps_field`: the seeded default stays until a person sets one). Tripwire: `tests/test_builtin_customer_layer.py`.
- A fork (`dev-docs/fleet-fork.md`) stays the way to change mechanics.

### Capability slots (#914 slice 2)

Each role label of a fleet member declares the capabilities it can use (`app.services.fleet_capability_slots.ROLE_SLOTS`: Mobilization takes ticketing, notification, EDR/XDR and SIEM (a fix applied through a bound integration under `ctem.production_change`, #2061), Validation EDR/XDR, scanner and EASM, Triage and Relevance vulnerability intelligence and asset inventory, Hunt, Detections and Gap analysis SIEM and EDR/XDR, Discovery scanner, EASM and asset inventory; Relevance, Triage, Validation, Hunt and Mobilization also take the optional Search engine capability, catalog type `search` (Brave Search, Google Search), whose members, `builtin_agents.CTEM_WEB_RESEARCH_AGENT_SLUGS`, get the `ctem-web-research` skill and the deployment's built-in web search). Slots are read from the role label at request time, so a fleet instantiated before a capability is added shows it on upgrade with no migration. A capability is a set of catalog `integration_types` (`CAPABILITIES`), never a provider name, so any provider typed that way fills it; a catalog entry flagged `capability_slots: False` (Filigran AI, an extraction model rather than a data source) keeps its types for the catalog browser and is never a slot candidate (#1180). `GET /fleets/{id}/capability-slots` answers each home member's slots as `filled`, `empty` (a connected integration the reader sees can be bound) or `unavailable` (none connected); a company-managed agent is offered company-managed integrations only. `POST /fleets/{id}/members/{member_id}/slots/{capability}` with `{integration_id}` binds a connected integration in one click and `DELETE .../slots/{capability}/{integration_id}` unbinds it: both write the member AGENT's `integration_bindings` under the agent's manage rule (company-managed: administrators), never the fleet, so a built-in fleet's write lock does not apply and the seed keeps the binding (customer layer). Licence-gated, no model call, audited on the agent. Tripwire: `tests/test_fleet_capability_slots.py`.

### Learned skill (#914 slice 3)

Each built-in agent owns one customer skill, "<agent> learned for <organization>" (`app.services.builtin_learned_skill`): a company-managed `AgentSkill` tagged `learned` and `learned-for-agent:<agent id>`, with no `slug:` and no `builtin` tag, so the seed never rewrites or deletes it. It is created empty on the first lesson, bound to the agent and pinned `always` through `skill_ids` / `skill_load_modes`, both recorded in `customer_fields` so the seed keeps the binding. The agent writes it with the tool `record_learned_lesson` (`services/builtin_tools/learned_skill.py`, offered to built-in agents in XTM One mode, in chat turns and assignment runs): one `## ` section per topic, `append`, `revise` or `remove`, bounded (`SECTION_MAX_CHARS` per section, `LEARNED_SKILL_MAX_CHARS` per skill). Every answer carries `size`, `max_chars` and `near_cap` (from 90 % of the cap) so the agent consolidates before the wall; a lesson that would pass the cap raises `LessonCapError`, whose refusal names the current size, the characters needed, the room left and the sections largest first, with the way to make room (`revise` or `remove`, then retry), and leaves a `tool_error` friction on the run so the owner sees a full skill (#2697). Every change is a `SkillVersion` whose commit message names the run; an administrator views, edits or clears it on the skill page, reverts a lesson with the skill rollback, and freezes it with the tag `learned-frozen` (the tool then refuses). A change of the organization profile never goes there: it goes through `propose_organization_context_change`. Tripwire: `tests/test_builtin_learned_skill.py`.

Next slice of #914: the "Locked by the product" / "Yours" split, the slots and the learned skill on the fleet and agent pages (UI, UX gate). Gate pins and added gates on built-in fleets (CTEM-34) are stored in this layer.

## Built-in knowledge bases (XTM One)

`BUILTIN_KNOWLEDGE_BASES` in `builtin_agents.py` are company-managed knowledge bases upserted by slug on every boot (`seed.seed_builtin_knowledge_bases`) and bound to the built-in agents in `bind_to_agent_slugs`. A KB with `knowledge_base_seed` loads the files of `seed_data/knowledge_base/<folder>/` as documents, matched by file name: a new file is added, a changed one is replaced and re-processed, and a file removed from the folder stays in installed KBs, so retiring a document means replacing its content under the same name. `chunk_size` and `chunk_overlap` are applied when the KB is created, before its documents are processed, and re-applied on each boot; changing them later does not re-chunk unchanged documents. Third-party content in a seed folder is described in a `seed_data/NOTICE-*.md` file (source, terms, changes).

## Built-in assignments (XTM One)

`BUILTIN_ASSIGNMENTS` in `builtin_agents.py` are real rows on built-in agents, upserted on every boot by `seed.seed_builtin_assignments` (agent slug + name; seeded names no longer listed are removed). The seed is the source of truth: it re-applies `enabled` on each boot, and admins cannot edit them.

The one exception is `admin_opt_in: True`, for an automation that writes shared data under an identity only an administrator may choose (the hourly indicator decay re-check). The seed creates it disabled (pause reason `created_disabled`) and never touches its switch or run-as identity again. An administrator may write `ADMIN_OPT_IN_WRITABLE_FIELDS` (`enabled`, `run_as_user_id`) on it and nothing else; the update route drops the built-in gate only for that field set (`is_admin_opt_in_builtin_assignment`). It is served as `AssignmentResponse.builtin_admin_opt_in`, which the assignment page reads to offer the switch and the run-as picker.

## Assignment execution and triggers

What happens when an assignment fires, the trigger types and delays: `dev-docs/reference/agents-and-assignments.md` > "Assignment Execution Flow", "Trigger Types" and "Delay Support"; the code path (`trigger_assignment` -> `orchestration_executions` row with `run_at` -> worker reconciler): `dev-docs/reference/backend-services-execution.md` > "Assignment Execution Flow".

`trigger_config` shapes:

```json
{"trigger_type": "schedule", "trigger_config": {"cron": "*/15 * * * *"}}
{"trigger_type": "webhook", "trigger_config": {"path": "github-issues", "events": ["issues.closed"]}}
{"trigger_type": "integration_event", "trigger_config": {"provider": "microsoft", "event": "new_email"}}
{"trigger_type": "manual", "trigger_config": {}}
```

## Key Files

| File | Purpose |
|------|---------|
| `backend/app/models/agent.py` | Agent ORM model |
| `backend/app/models/agent_template.py` | AgentTemplate ORM model (DB-backed) |
| `backend/app/models/assignment.py` | Assignment + AssignmentRun ORM models |
| `backend/app/agents/templates.py` | Built-in agent templates (`BUILTIN_TEMPLATES`) |
| `backend/app/agents/cti_templates.py` | CTI templates (`CTI_TEMPLATES`), built from `seed_data/cti_templates/` |
| `backend/app/agents/builtin_agents.py` | Built-in agents, assignments, integrations, knowledge bases and skills (XTM One) |
| `backend/app/agents/seed.py` | Startup seed: templates, built-in agents, assignments, knowledge bases, skills, integrations, intent bindings |
| `backend/app/services/assignment_engine.py` | Assignment triggering and execution |
| `backend/app/services/orchestration_engine.py`, `orchestration_reconciler.py` | Durable execution: delays (`run_at`), leases, deadlines, retries |
| `backend/app/services/autonomous.py` | Background scheduler: cron and integration-event trigger detection, housekeeping |
| `backend/app/core/utils.py` | Shared helpers (slugify, validate_slug, parse_delay) |
| `backend/app/api/v1/helpers.py` | Shared DB lookups + company-managed toggle + MCP runtime builder |
| `backend/app/api/v1/agents.py` | Agent API endpoints |
| `backend/app/api/v1/agent_templates.py` | Agent template API endpoints |
| `backend/app/api/v1/assignments.py` | Assignment API endpoints |
| `backend/app/api/v1/webhooks.py` | Webhook receiver endpoint |
| `frontend/src/pages/Agents.tsx` | Agent list UI |
| `frontend/src/pages/AgentDetail.tsx` | Agent config and assignments (sections in `pages/agent-detail/`) |
| `frontend/src/pages/AgentFlow.tsx` | Handover topology graph visualization |
| `frontend/src/pages/NewAgentMode.tsx` | Create an agent: wizard (`NewAgentWizard.tsx`), template or scratch (`NewAgent.tsx`), import (`NewAgentImport.tsx`) |
