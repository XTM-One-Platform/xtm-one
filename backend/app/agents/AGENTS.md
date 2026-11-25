# Agents – Developer Guide

Read before changing `backend/app/agents/`: agent templates, built-in agents and built-in assignments, and the seed that provisions them on every boot. Assignment and trigger contracts are in `dev-docs/reference/agents-and-assignments.md`.

## Where to look

| Need | Read |
|---|---|
| Agent model tree, template payload, built-in seeding, trigger config shapes, key files | `dev-docs/reference/backend-agents-templates-and-builtins.md` |
| Assignments, triggers, handovers, output format, memory, compaction, template contract | `dev-docs/reference/agents-and-assignments.md` |
| Built-in template seed data (CTI ones: `cti_templates.py`) | `backend/app/agents/templates.py` |
| Built-in agents and assignments (each in its `platform_targets` modes; XTM One and dev without any) | `backend/app/agents/builtin_agents.py` |
| Seeding at startup (templates, agents, assignments) | `backend/app/agents/seed.py` |
| Assignment execution engine | `backend/app/services/assignment_engine.py` |
| Cron trigger detection | `backend/app/services/autonomous.py` |
| Durable execution, delays, leases | `backend/app/services/orchestration_engine.py`, `orchestration_reconciler.py` |
| ORM models | `backend/app/models/agent.py`, `agent_template.py`, `assignment.py` |

## Rules

- Templates are **stored in the database** (`agent_templates`), not hardcoded; admins edit any template, create new ones and disable built-ins via the UI or API.
- `BUILTIN_TEMPLATES` in `templates.py` is the one source of built-in templates: every startup fully syncs them (insert, update, delete removed slugs).
- Built-in agents (`BUILTIN_AGENTS` in `builtin_agents.py`) are upserted by slug on every boot by `seed.seed_builtin_agents`, each in the modes its `platform_targets` name (`seed_matches_platform`; XTM One and dev without any).
- The seed adopts a row on a built-in slug only when it is company-managed. A person's own agent on that slug is never adopted: it moves to a free slug (`generate_unique_agent_slug`), loses `is_builtin`, and the built-in is created. One an earlier boot adopted also loses what the seed attached to it for that slug (`_detach_seed_rows`): its seeded assignments, global built-in intent bindings, built-in KB / skill / integration bindings, the built-in's `tags` and `supported_intents`.
- The stale sweep of retired built-in slugs deletes only company-managed rows; a private one goes back to its owner as an ordinary agent.
- Every default assistant (a new vertical one too) is in `DEFAULT_ASSISTANT_SLUGS`: media tools + craft skills (`tests/test_default_assistant_media.py`); a flag-gated built-in skill only via `FEATURE_GATED_BUILTIN_SKILLS`.
- `BUILTIN_ASSIGNMENTS` are real rows upserted by `seed.seed_builtin_assignments` (agent slug + name). The seed is the source of truth: it re-applies `enabled` on every boot (except a pause a person stamped, `is_paused_by_person`, a fleet pause or an emergency stop) and their definition is read-only. Whoever may manage the agent may write `enabled` alone on any of them (`BUILTIN_ASSIGNMENT_SWITCH_FIELDS`, `builtin_assignment_write_allowed`, REST and `update_assignment` alike).
- The one exception is `admin_opt_in: True`: created disabled (pause reason `created_disabled`), never re-touched by the seed; an admin may write only `ADMIN_OPT_IN_WRITABLE_FIELDS` (`enabled`, `run_as_user_id`), gated by `is_admin_opt_in_builtin_assignment`, served as `AssignmentResponse.builtin_admin_opt_in`.
- Execution: trigger -> `trigger_assignment()` creates the `AssignmentRun` and its `orchestration_executions` row (`run_at` carries any delay; never a Redis delay key) -> the worker's orchestration reconciler claims it and runs the agentic loop under a lease (`dev-docs/reference/backend-directory-map.md` > "How Data Flows").
- Slugs and delays go through `app/core/utils.py` (`slugify`, `validate_slug`, `parse_delay`); shared DB lookups, the company-managed toggle and the MCP runtime builder through `app/api/v1/helpers.py`.

## Adding a template or a built-in

- **Template at runtime**: `POST /api/v1/agents/templates` (admin, `CreateTemplateRequest`); available to all users at once.
- **Built-in template**: add it to `BUILTIN_TEMPLATES` in `templates.py` with its `platform_targets`; the next boot inserts it.
- **Built-in agent or assignment**: add it to `BUILTIN_AGENTS` / `BUILTIN_ASSIGNMENTS` in `builtin_agents.py`; `admin_opt_in: True` only for an automation that writes shared data under an identity only an administrator may choose.
