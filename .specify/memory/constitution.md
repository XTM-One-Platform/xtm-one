<!--
Sync Impact Report
==================
Version change: 2.1.0 → 2.2.0
Rationale for MINOR: one new principle added (XII. Generic Mechanisms, No
Workarounds); no existing principle is removed, renamed or redefined.
Templates and commands: unchanged. Retroactive scope: the open cases are
listed in the exception lists of `backend/tests/test_no_workarounds.py` and
removed since.

-----------------------------------------------------------------------
Sync Impact Report (2.1.0, retained)
==================
Version change: 2.0.0 → 2.1.0
Rationale for MINOR: one new principle added; no existing principle is
removed, renamed or redefined.

Added principles:
  - XI. Minimal, Reviewable Writing

Modified principles: none. Principle VII (Rationale-Documented Complexity)
is the one XI could have collided with, so XI is scoped to exclude it
explicitly: VII mandates the "why" in non-obvious code, and XI targets
restatement, duplication and padding. Where the two appear to conflict on a
given comment, VII wins and XI does not license deleting a rationale.

Added sections: none. Removed sections: none.

Templates and commands: unchanged (they read this file at runtime). The
existing spec/plan/tasks templates do not conflict with XI; they set
structure, not length.

Retroactive scope: XI is NOT applied retroactively to merged artifacts.
`specs/006-a2a-server-inbound` and `specs/009-a2a-client-outbound` were both
written before it and both exceed what XI would now allow — 009 in
particular carries long deliberation transcripts in its Clarifications
section. Rewriting a ratified record is worse than keeping a verbose one, so
they stand as history; XI binds new writing and revisions.

The 2.0.0 report below is retained rather than compressed, for the same
reason.

-----------------------------------------------------------------------
Sync Impact Report (2.0.0, retained)
==================
Version change: 1.0.0 → 2.0.0
Rationale for MAJOR: this merges two independently-ratified 1.0.0
constitutions that existed in parallel — the process-oriented document on
`main` (ratified 2026-07-23, Principles I–V) and the domain-oriented
document written on the evaluation-pipeline branch (ratified
2026-07-09, Principles I–VII). Every existing principle keeps its text but
is renumbered, which is a backward-incompatible redefinition of the
identifiers `Principle I`–`Principle V` and therefore MAJOR under the
versioning policy below.

Modified principles:
  - I. Fail-Closed by Default → II (text unchanged)
  - II. Concurrency-Safe, Multi-Replica by Design → IV (text unchanged)
  - III. CI-Enforced Quality Gates → VI (text unchanged)
  - IV. Rationale-Documented Complexity → VII (text unchanged)
  - V. Backward-Compatible, Auditable Evolution → merged into V
    (Alembic-Only, Lock-Safe Migrations), combining the branch's five
    concrete migration rules with main's fail-closed-on-unknown-state and
    CalVer/UPGRADING.md release requirements. This was the only genuine
    overlap between the two documents.

Added principles (from the evaluation-pipeline document):
  - I. Assignment-Driven Automation (NON-NEGOTIABLE)
  - III. Security by Default
  - VIII. Shared Utilities Over Duplication
  - IX. TanStack Query for Server State
  - X. Design System Compliance

Added sections:
  - Technology Stack (descriptive, explicitly non-normative — see note)
  - Development Workflow: Local Development, Quality Gates, Code
    Organization, Three-Tier Entity Visibility

Removed sections: none. Main's Security Requirements section is retained
in full; its bandit/sandbox/disclosure detail is orthogonal to the
branch's Principle III and the two are complementary rather than
duplicative.

Cross-reference fixes: "per Principle I" → "per Principle II" and "see
Principle IV" → "see Principle VII" updated to follow the renumbering.

Templates requiring updates:
  - ⚠ .specify/templates/plan-template.md — the Constitution Check gate
    enumerates the old I–V; it MUST be re-checked against the new I–X
    before the next /speckit-plan run.
  - ✅ .specify/templates/tasks-template.md — the "tests optional" caveat
    still resolves correctly (Principle III → now VI).
  - ✅ .specify/templates/spec-template.md — unchanged; operates above the
    level these principles govern.

Follow-up TODOs: existing specs/ plans pre-date this merge and were NOT
retroactively re-checked; whoever next runs /speckit-plan or
/speckit-analyze on those features should re-evaluate their gates.
-->

# XTM One Constitution

## Core Principles

### I. Assignment-Driven Automation (NON-NEGOTIABLE)

All automation MUST be implemented as **Assignments on Agents** — never as
ad-hoc background jobs, standalone scripts, or hard-coded steps. The
agentic loop (LLM + tools) is the single execution model for all agent
work.

**Rationale**: A unified execution model ensures observability,
auditability, and consistent behaviour across all automation regardless of
trigger type. Every escape hatch added here becomes a code path with no
run record, no tool tracking, and no audit trail.

### II. Fail-Closed by Default

When a security-, quota-, or integrity-relevant check cannot be completed
confidently — the verification backend is unreachable, a configuration
value is unrecognized, prior system state is not understood, credentials
cannot be read, or a signature cannot be verified — the system MUST refuse
or deny the operation rather than silently proceed in an unmetered,
unvalidated, or unrestricted state. This applies at minimum to: quota and
rate-limit enforcement when Redis/Postgres is unreachable
(`quota_fail_mode`, `rate_limit_fail_mode`), `Settings` field validators
rejecting unrecognized enum-like values at startup instead of defaulting
silently, unrecognized database migration state refusing to boot rather
than guessing, unreadable channel-bot credentials, webhook signature
verification, and tool/allow-list gating (only explicitly permitted tools
or egress targets run; everything else is denied by default).

**Rationale**: A fail-open default turns an infrastructure hiccup or a
config typo into a silent security or cost-control bypass — exactly the
failure mode these mechanisms exist to prevent. This pattern recurs
independently across quota enforcement, migrations, credential handling,
webhooks, and tool execution, which is why it is a project-wide principle
rather than a one-off implementation choice in any single module.

### III. Security by Default

- All secrets MUST be **Fernet-encrypted** before database storage. No
  integration credentials in environment variables — everything lives in
  the DB, configured via the UI.
- All API endpoints MUST require **JWT authentication**, except explicitly
  public ones (health checks, public catalog reads, JWKS).
- Prompt-injection defenses (`prompt_guard.py`) MUST be applied to every
  user-facing LLM path. System prompt inflation is banned — behavioural
  fixes MUST prefer deterministic code (post-processing, validation,
  tool-result grounding) over adding prompt text.

**Rationale**: The platform stores credentials for external services and
executes AI with tool access, so a single unguarded surface can compromise
entire downstream environments. See also Security Requirements below,
which covers the static-analysis and sandbox side of the same concern.

### IV. Concurrency-Safe, Multi-Replica by Design

Any code path that runs identically across multiple backend
replicas/workers — scheduled jobs, startup migrations, usage-counter
flushes — MUST be safe under concurrent execution by multiple instances.
Use a Postgres advisory lock (or equivalent serialization primitive) so
only one replica performs a given cycle of work, and make retries/re-runs
idempotent so a skipped or interrupted cycle never loses or double-applies
state.

**Rationale**: This service is deployed as N concurrent replicas/workers by
default, not a single instance. Code that assumes single-instance execution
(e.g. a naive "run this job on a timer") silently breaks in production the
moment the deployment scales past one replica or one worker process. This
project has already paid for that lesson at least twice (quota-usage flush,
migration bootstrap) and both fixes converged on the same advisory-lock +
idempotent-retry shape — new scheduled or startup-time work MUST follow it
rather than reinvent single-instance-only logic.

### V. Alembic-Only, Lock-Safe Migrations (NON-NEGOTIABLE)

Every schema or state change MUST ship as an Alembic migration — never
inline SQL, never a `create_all`-only schema change. Each migration MUST
be:

1. **Single-head** — never open a second Alembic head.
2. **Idempotent** — safe to re-run (`IF NOT EXISTS` / `IF EXISTS` guards),
   because fresh installs stamp while existing installs execute the body.
3. **Lock-light** — no table rewrites, no unbounded in-transaction
   backfills, no non-`CONCURRENTLY` index builds on large or hot tables.
4. **Lock-safe** — `terminate_lock_holders()` + `set_lock_timeout()`
   before any lock-taking DDL (enforced by CI).
5. **Reversible** — a working `downgrade()` for every migration.

Unrecognized prior migration state MUST fail closed — refuse to boot
rather than guess (Principle II) — instead of attempting a silent
best-effort upgrade. Releases follow the project's CalVer tagging
(`major.YYMMDD.patch`, per `cliff.toml`) with Conventional-Commit-derived
changelogs; breaking or upgrade-relevant changes MUST document a
precondition/failure/rollback path, as `UPGRADING.md` already models for
the Alembic history squash.

**Rationale**: Multi-replica auto-migrate-on-startup means one bad
migration stalls every booting backend, and the 2026-07-05 outage proved
there is no such thing as a "cold" table under a rolling deploy — any
long-lived transaction that has merely *read* the table blocks the ALTER.
`UPGRADING.md` records a real incident where this discipline mattered, and
the migrations CI job (idempotent re-run + downgrade round-trip) keeps it
enforced mechanically rather than relying on reviewers to catch a
regression by eye.

### VI. CI-Enforced Quality Gates

Lint (`ruff check`, including its enabled `flake8-bandit` security rules)
and frontend type-checking/lint (`tsc --noEmit`, `oxlint`) MUST pass in CI
and are blocking, not advisory. Backend changes MUST be covered by tests
that run in CI (unit tests by default; the `integration` pytest marker for
behavior touching real Postgres/Redis/MinIO) — tests are the norm here, not
an optional nice-to-have, even though generic task templates may mark them
optional for lighter-weight or frontend-only work. Database migrations MUST
be covered by the migration CI job's idempotent-re-run and
downgrade-round-trip checks.

**Rationale**: The CI pipeline already enforces exactly this (`ci.yml`
lint/typecheck/oxlint steps, a dedicated `integration` job against real
infra, a dedicated `migrations` job walking the full Alembic chain) — this
principle names an existing, non-negotiable gate rather than introducing a
new one. Treating backend tests as routinely optional would contradict how
this repository already ships changes.

### VII. Rationale-Documented Complexity

Code in security-sensitive, concurrency-sensitive, or otherwise non-obvious
areas (fail-closed logic, locking/idempotency, migration edge cases, legacy
data-shape handling) MUST carry comments that explain *why* — the
constraint, prior incident, or invariant being protected — not comments
that restate *what* the code does. Simple, self-evidently correct
code (plain CRUD models, straightforward data classes) does not need this
treatment; forcing rationale comments onto trivial code just adds noise.

**Rationale**: This is exactly how the codebase already writes its densest,
most-load-bearing modules (quota enforcement, migration bootstrap, ORM
models with non-obvious nullability decisions) — and exactly where it is
conspicuously absent (bare, near-comment-free simple model files) causes no
harm, because there's no hidden constraint to lose. The principle targets
the "why", not blanket comment-density quotas.

### VIII. Shared Utilities Over Duplication

- Backend: use `api/v1/helpers.py` (`user_visible_query`,
  `handle_company_managed_toggle`), `builtin_tools/`, `output_format.py`,
  `prompt_guard.py`, and `core/utils.py`.
- Frontend: use `lib/constants.ts`, `lib/utils.ts`, and the shared hooks
  and components (`useEntityFilter`, `useTabParam`, `useDebounce`,
  `RunCard`, `EntityConfigCard`).
- New constants, formatters, or UI patterns MUST be added to the shared
  layer — never redefined locally.

**Rationale**: Across 490+ backend test files and 60+ frontend pages,
a locally redefined copy of a shared helper does not stay in sync; the
drift surfaces later as a silent regression in whichever copy was not
updated.

### IX. TanStack Query for Server State

All server data in the frontend MUST use **TanStack Query** — no
`useState` for API data, no manual caching, no Redux/Zustand for server
state. Refetch intervals come from `constants.ts`
(`REFETCH_INTERVAL_SLOW` / `_STANDARD` / `_FAST`).

**Rationale**: Consistent cache invalidation, request deduplication, and
background refetching across 50+ entity types requires a single, proven
state manager rather than per-page bespoke logic.

### X. Design System Compliance

- **Dark + light theme** via CSS token variables (`data-mode` attribute,
  no `.dark` class). Surfaces MUST use tokens (`bg-foreground/N`,
  `border-foreground/N`, `bg-popover`) — never hardcoded colours.
- **Filigran identity**: Geologica (headings) + IBM Plex Sans (body), cyan
  accent (`hsl(197, 100%, 53%)`), glassmorphism panels.
- **No native checkboxes** — always the `Switch` component.
- **No native `title` attributes** — always the `Tip` tooltip component.
- Shared UI components MUST be reused rather than re-implemented.

**Rationale**: A single design language across 60+ pages keeps the product
familiar and reduces cognitive load; hardcoded colours in particular are
the recurring cause of light-mode breakage, which is invisible to anyone
developing in dark mode.

### XI. Minimal, Reviewable Writing

Prose in this repository — specifications, plans, task lists, developer
docs, commit bodies, PR descriptions and code comments — MUST be written to
be read in full by a reviewer, not skimmed. Concretely:

- **One fact, one place.** A decision recorded once MUST be cited by
  identifier elsewhere (`FR-012`, `T031`, an ADR number), never restated in
  its own words. Two statements of one rule drift, and the drift surfaces
  as a contradiction nobody notices.
- **One requirement, one obligation, one sentence.** A requirement that
  needs a second sentence to state what it obliges is two requirements.
- **Record the decision, not the deliberation.** A clarification or design
  note states what was chosen and the single reason it beat the strongest
  alternative. Rejected options are named, not argued through.
- **Revise by replacement.** A correction MUST replace the text it
  invalidates. Appending a correction beside the wrong statement leaves a
  document that says both things.
- **No ceremony.** No restating the question inside the answer, no
  summarising a section that was just read, no "it is worth noting", no
  hedging that adds length without changing meaning.

This principle does **not** override Principle VII: a comment explaining
*why* non-obvious code is the way it is stays, and MUST NOT be deleted in
the name of brevity. VII says keep the "why"; XI says cut everything that
is not the "why". They are the same instruction from opposite sides.

It applies to this document too.

**Rationale**: reviewer attention is the binding constraint on quality
here, not authoring effort. A document long enough to be skimmed converts
its guarantees into decoration — which is precisely the failure this
project has already paid for: the A2A inbound review (2026-08-19) found
three requirements shipped as functions with no call site, each surrounded
by enough prose to read as assurance, and the surrounding prose is part of
why nobody checked. Length also hides contradictions, because a reader who
has stopped reading closely cannot notice that page four disagrees with
page one. Shorter documents are not a stylistic preference; they are the
only ones whose claims get verified.

### XII. Generic Mechanisms, No Workarounds (NON-NEGOTIABLE)

XTM One is a generic platform. A behaviour MUST be fixed at its cause with a
mechanism every agent, integration, language and model shares. In the
generic layers (the agent loop, tool base classes, built-in tools, memory,
chat and assignment orchestration, prompt assembly):

- No vendor, integration or domain logic: what an integration needs is
  declared by the integration (catalog entry, provider adapter,
  `ToolDefinition`) and read generically.
- No keyword or regex list reading natural language to decide behaviour:
  ask for a structured answer, use a fact the platform holds, or let the
  model's decision stand.
- No tool behaviour decided by a tool's name or verb: the tool declares it.
- No re-reading of text the platform wrote: pass the fact as data.
- No code, constant or prompt text for one run, one customer or one
  integration.

`backend/tests/test_no_workarounds.py` enforces the first two; its exception
lists only shrink. Full text: architecture invariant 18.

**Rationale**: a patch shaped like the incident that revealed it fixes that
incident in one language for one vendor and fails silently on the next. The
cross-agent memory is the case that made this a principle: its first
fix routed "PR / review / hold" phrases and called GitHub from inside the
user memory.

## Security Requirements

- `flake8-bandit` security rules stay enabled in the backend ruff
  configuration; do not silence a bandit finding without a code comment
  explaining why it is a false positive in context (see Principle VII).
- Sandbox execution surfaces (OpenSandbox, Docker, subprocess custom tools,
  the built-in code interpreter) default to deny: network egress is denied
  unless explicitly allow-listed, and module/import blocklists are treated
  as defense-in-depth, not as a complete security boundary on their own —
  document that distinction wherever a new sandbox surface is added.
  Similarly, MCP/agent tool exposure defaults to an explicit allow-list.
- Security vulnerabilities are handled per `SECURITY.md`: private
  disclosure via GitHub Security Advisories only, no public issues/PRs for
  unpatched vulnerabilities.
- Secrets, credentials, and tokens are never logged; a credential that
  cannot be read or verified is treated as absent (Principle II), never as
  "assume valid and continue."

## Technology Stack

This section is **descriptive, not normative**: it records the stack the
principles above are written against, so a reader can tell whether a rule
still applies. Routine version bumps and dependency changes do NOT require
a constitutional amendment — only a change that invalidates a principle
does.

| Layer | Technology |
|-------|-----------|
| Backend runtime | Python 3.14, async/await throughout |
| Web framework | FastAPI + Pydantic schemas |
| ORM | SQLAlchemy 2.0 (async), `Mapped[]` annotations |
| Database | PostgreSQL 17 + pgvector |
| Cache / queue | Redis 8 + SAQ |
| LLM providers | Anthropic, OpenAI, Google GenAI, OpenRouter, Ollama, Mistral, Azure OpenAI, Bedrock, LiteLLM — direct SDKs, no LangChain |
| Tool protocol | MCP (Model Context Protocol) |
| Auth | PyJWT + Fernet + Authlib |
| Frontend | React 19 (functional components only), TypeScript strict |
| Build / styling | Vite, Tailwind CSS, Radix UI + shadcn/ui pattern |
| Server state | TanStack Query (Principle IX) |
| Routing / icons | React Router DOM, Lucide React |
| Testing | pytest (`asyncio_mode=auto`) + Playwright |
| Linting | Ruff (backend), oxlint (frontend) |
| Packaging | Docker unified image (port 4000), Compose / `k8s/` |
| Object storage | MinIO (S3-compatible) |
| Package manager | Yarn (Corepack) |

## Development Workflow

### Contribution rules

- PR titles MUST follow Conventional Commits
  (`type(scope): description (#issue)`) — enforced by CI
  (`pr-title-check.yml`); intermediate commits on a feature branch are not
  held to this format (they are squash-merged), but the final PR title is.
  Every PR MUST link to an issue. Types: `feat`, `fix`, `chore`, `docs`,
  `style`, `refactor`, `perf`, `test`, `build`, `ci`, `revert`. The
  description starts lowercase with no trailing period; acronyms and
  proper nouns are preserved (`XTM One`, `OpenCTI`, `LLM`, `STIX`).
- Commits MUST be signed — enforced by CI (`check-signed-commits.yml`).
- Changes MUST be reviewed by the relevant `CODEOWNERS` for the directories
  touched (`/backend/`, `/frontend/`, `/apps/`, `/docs/`, `/scripts/`, or
  the default owners for anything else).
- Contributors are expected to run the same checks CI runs
  (`scripts/ci-local.sh` per `CONTRIBUTING.md`) before pushing, so CI
  failures are the exception, not the primary feedback loop.

### Local development

```bash
cp .env.sample .env  # first time only
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build  # deps, migrations, platform + worker + frontend with hot reload
```

### Quality gates (MUST pass before merge)

| Gate | Command |
|------|---------|
| Backend lint | `cd backend && ruff check app/` |
| Frontend lint + types | `cd frontend && yarn check` |
| Backend tests | `cd backend && pytest tests/` |
| Frontend E2E | `cd frontend && yarn test:e2e` |
| Migration integrity | `pytest tests/test_migration_integrity.py` |

### Code organization

- One API hook file per domain (`useAgentApi.ts`, `useAssignmentApi.ts`).
- One model file per entity in `backend/app/models/`.
- One Alembic migration per change — never batch unrelated schema changes.
- Feature branches named with the issue number: `###-feature-name`.

### Three-tier entity visibility

All user-facing entities follow **own + group-shared + company-managed**.
Backend queries MUST use `user_visible_query()` / `user_visible_filter()`,
and a model reached through either helper MUST declare the full visibility
column set. Frontend permission checks MUST use `canManageEntity()` from
`lib/utils.ts`.

## Governance

This constitution is the authoritative source for architectural decisions
and development standards in XTM One. It supersedes ad-hoc convention and
informal practice where the two conflict.

**Amendment process**: amendments happen via a normal reviewed PR that
edits this file, following the same CODEOWNERS / signed-commit /
Conventional-Commit-title requirements as any other change. At least one
maintainer MUST approve the amendment, and the version below MUST be
updated in the same PR.

**Versioning policy** (semantic versioning of this document, independent of
the project's own CalVer release tags): MAJOR for backward-incompatible
principle removals or redefinitions; MINOR for a new principle or a
materially expanded section; PATCH for clarifications and wording fixes
that don't change what is required.

**Compliance review**: `/speckit-plan` MUST evaluate its Constitution Check
gate against the Core Principles above for every feature. Principle XI is
reviewed at PR time rather than by a tool: a reviewer who finds restated
facts, argued-through alternatives, or corrections appended beside the text
they invalidate MUST ask for the cut. A plan that
cannot satisfy a gate MUST document the violation and justification in that
plan's Complexity Tracking table rather than silently ignoring the gate.
CI enforces migration integrity, lint gates, and type checking.

**Relationship to `AGENTS.md`**: the `AGENTS.md` files (repository root,
`backend/`, `frontend/`, and subdirectory-level) carry the detailed runtime
and implementation guidance. They elaborate on these principles and MUST
NOT contradict them; where they do, this document wins and the `AGENTS.md`
is the file to fix.

**Version**: 2.2.0 | **Ratified**: 2026-07-23 | **Last Amended**: 2026-10-03
