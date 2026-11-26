# XTM One - Agent Instructions

Every agent session loads this file: it keeps pointers and the rules that apply everywhere. Contracts live in `dev-docs/reference/` and in each directory's `AGENTS.md`; read the one for the area you change, and put new detail there, never here.

XTM One is an open-source agentic assistant platform: **agents** (persona, tools, knowledge) run **assignments** (trigger + prompt) autonomously and chat with users on the web and external channels.

## Layout and running

- `backend/` Python 3.14 / FastAPI (`backend/AGENTS.md`), `frontend/` React 19 / TypeScript (`frontend/AGENTS.md`), `apps/` Office add-in, XTM One Desktop runner, XTM One Relay, `agent-core` (the Rust core they share).
- `dev-docs/` designs, FSM, ADRs and the `reference/` contracts; `docs/` user docs (MkDocs); `k8s/` Kubernetes examples for self-hosters (Relay, alert rules); `mcp-registry/` the MCP Registry entries of the MCP servers, published on release (`dev-docs/mcp-registry-publication.md`); `.env.sample` infrastructure config only.
- Run: `cp .env.sample .env`, then `make up` / `make start` / `make test` / `make e2e` / `make logs` / `make psql` / `make down` / `make reset` - the Makefile wraps the dev compose override and the test suites; `make` lists all targets, `make help` shows usage.

## Where the details live

| Need | Read |
|---|---|
| Entities, visibility, channel flags, model resolution | `dev-docs/reference/entities.md` |
| Assignments, triggers, handovers, output format, memory, compaction, templates | `dev-docs/reference/agents-and-assignments.md` |
| Integrations and knowledge bases | `dev-docs/reference/integrations-and-knowledge-bases.md` |
| Evaluations: data model, API, scores, judge, reuse, export | `dev-docs/reference/evaluations.md` |
| API route prefixes | `dev-docs/reference/api-routes.md` |
| Invariants, full text | `dev-docs/reference/architecture-invariants.md` |
| Shared helpers (one rule, one module) | `dev-docs/reference/backend-shared-utilities.md`, `frontend-shared-utilities.md` |
| New-feature checklist, rules 1-99 (cited by number) | `dev-docs/reference/adding-features.md` |
| Full-stack feature (API → hook → page) | `dev-docs/reference/full-stack-feature.md` |
| Migrations (safety rules, cited by number) | `dev-docs/reference/migrations-safety.md` |
| Area guides | `backend/AGENTS.md`, `frontend/AGENTS.md`, `backend/app/<area>/AGENTS.md` |
| Which files are Enterprise Edition (license headers) | `dev-docs/reference/editions.md`, `licensing/editions.yml` |
| A vulnerability found, a security fix, the `security` label | `dev-docs/reference/security-reporting.md` |
| Feature designs | `dev-docs/*.md` (`presentations.md`, `platform-mcp-servers.md`, `FSM/`, ...) |

Find the owner before writing: `rg -n "<helper or entity>" dev-docs/reference`. Cross-cutting rules (visibility, file access, OAuth sharing, base path, CSP, quotas, edition gate) have ONE module, often pinned by a tripwire test: reuse it, never re-spell it.

Entities (contracts in `entities.md`): Agent, Assignment and Assignment Run, Flow, Agent Template, Integration, Channel Bot / Channel, Agent Account (`dev-docs/reference/agent-accounts.md`), Knowledge Base, MCP Server, Custom Tool / Custom Tool Source, Agent Skill, Prompt, Variable, General Assistant, Sub-Agent Task / Delegation, Work Item / Shared State, Agent Memory Entry, Annotation / Feedback Criterion / Improvement Proposal (Persona Improvement), Group, Department, Audit Log, Public Catalog, MCP endpoints, Template Container, Presentation, Report, Video Studio (`dev-docs/video-studio.md`), Browser Profile (`dev-docs/browser-profiles.md`), XTM One Desktop / Runner, Relay Group / Relay (`dev-docs/relays.md`), Dataset / Evaluation, Intent / Vertical, Quota / Agentic Execution, LLM Trace, Investigation Pack, Orchestration Execution, Arcade, CTEM Metric, Invitation, Terms of Use.

## Visibility (three tiers)

Own (`user_id`) + shared (`shared_group_ids` / `shared_user_ids`; tiers `read_only`, `config`, `config_with_chats`) + company-managed (`is_company_managed`, read-only for non-admins). Admins see and edit everything; only library lists and pickers stay three-tier for them (`?scope=all` on detail pages). Who may edit is `helpers.can_manage()`, served as `can_manage`, read through `canManageEntity()`.

## Architecture invariants (DO NOT violate)

Numbering is stable (code cites "invariant 16"); full text in `architecture-invariants.md`.

1. All automation is **Assignments on Agents**, never ad-hoc endpoints.
2. Secrets are **Fernet-encrypted** in the DB (exception: assignment `webhook_secret`, redacted everywhere - SEC-5).
3. Every API endpoint requires **JWT auth**, except public and webhook ones.
4. **No integration credentials in env vars** - all in the DB via the UI.
5. Frontend server state uses **TanStack Query**.
6. **`@filigran/design-system` is the single source of truth for the UI**: its components, tokens and `<Icon name>`, its consumer skill wins over local rules (`frontend-design-system.md`).
7. Assignments run through the **agentic loop** (LLM + tools), never hard-coded steps.
8. SSO users link by email; SAML group mapping syncs on every login.
9. **Company-managed** = the `is_company_managed` flag, which alone decides visibility; the row keeps a real owner, the creator or, for a row created without a user, `resolve_company_managed_owner_id` (`tests/test_company_managed_owner_resolver.py`); legacy rows may still have none; going private cleans other users' references.
10. **No native checkboxes** - the `Switch` component.
11. **Custom tools** run in the hardened Docker sandbox, with a timeout-enforced subprocess fallback.
12. **`disable_chat` agents** are hidden from `/v1/models` and the profile, greyed out in the chat picker.
13. **Migrations**: Alembic only, single head, idempotent, lock-safe (`terminate_lock_holders()` + `set_lock_timeout()`), never a second lock.
14. **Reasoning** shows under the status bubble and both vanish when the answer streams; answer text shown is never taken back.
15. The **app version** is injected at build time (`APP_VERSION`), never hardcoded.
16. **No system prompt inflation**: fix behaviour in code (post-processing, validation, tool-result grounding).
17. **A license is a quota, not a feature switch**: two editions, one gate `is_agentic_ai_licensed()` (`agentic_gate` for background work).
18. **No workarounds**: fix the cause with a generic mechanism; no vendor, tool-name or keyword logic in a generic layer (`tests/test_no_workarounds.py`).

## Working here

- Conventions: the area's `AGENTS.md` (`backend/`, `frontend/`, then the nearest directory's), then `dev-docs/reference/`. No hardcoded colors; templates are DB-backed.
- New model: import it in `backend/app/models/__init__.py` + an Alembic migration; a shareable entity takes the full visibility column set, joins `CompanyManagedEntity` and is tested as a non-admin (rule 19).
- New routes register in `backend/app/main.py`; types go to `frontend/src/types/index.ts`, hooks to `frontend/src/hooks/api/`.
- A new `.env.sample` variable is documented in `docs/docs/` (CI-enforced); update the user docs and `dev-docs/` a change affects.
- **The repository is public: never write a vulnerability down in a public place** (issue, PR, review comment, commit, test name, `security` label - retired). Tell the user, then use a private GitHub security advisory (`SECURITY.md`); a hardening with nothing exploitable is a normal, neutrally worded issue/PR.
- Text files are UTF-8 without BOM, with LF line endings (`.gitattributes`; pinned by `backend/tests/test_repo_text_hygiene.py`). On Windows, write files with `pwsh` 7, never Windows PowerShell 5.1 `Set-Content` / `Out-File` (it reads UTF-8 as cp1252 and adds a BOM).
- Agentic is EE: before creating a file or a directory, `make edition P=<path>`; on `to classify`, classify it in the same commit: agentic, a glob in `licensing/editions.yml`; otherwise `make mark-ce P=<path>` (CE cannot become EE later), and a moved or split CE file stays CE. Never write the header by hand: `make license` (rule 100).
- Python is managed by uv (`backend/` and `docs/` are uv projects: `pyproject.toml` + `uv.lock`); add a dependency with `uv add`, never pip. `uv run` syncs `.venv` to the checkout's lock: where `backend/.venv` links to another checkout's venv, use `uv run --no-sync` (`make test` / `make migrate` then skip the sync).
- Before committing: `make test` (backend unit suite; narrow with `ARGS="tests/test_<name>.py"`), `cd backend && uv run ruff check . && uv run mypy`, `cd frontend && yarn check`. A pull request runs the lint jobs of CI; the merge queue runs the test suites and E2E on what the change reaches (`dev-docs/ci-test-selection.md`); to check them before, when needed, `./scripts/ci-local.sh` runs the full CI suite locally (one run at a time per clone: run it in the background and let it wait, never `CI_LOCAL_NO_LOCK=1` or its steps by hand; `--help` for options).
- Pull request body: `.github/pull_request_template.md` filled in (its comment says how).
- Keep this file small: one line per entry, no history, no issue narrative. The contract goes to `dev-docs/reference/`.

<!-- filigran-conventions:start -->
## Commit, PR & issue conventions

All commits, pull requests and issues in this repository follow the
[Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)
specification with a GitHub issue reference:

```
type(scope?)!?: description (#issue)
```

- Types: `feat`, `fix`, `chore`, `docs`, `style`, `refactor`, `perf`, `test`,
  `build`, `ci`, `revert`.
- The description starts with a lowercase letter and has no trailing period;
  preserve acronyms and proper nouns.
- The old `[backend]` / `[frontend]` bracket prefixes are discontinued — use a
  Conventional Commits scope instead.
- Pull request titles **must** end with the related issue reference, e.g.
  `(#1234)`, and every pull request must be linked to an issue.
- Sign your commits.

When generating commit messages, PR titles or issue titles, always follow this
convention. See [`.github/LABELS.md`](.github/LABELS.md) for the full title and
label taxonomy.
<!-- filigran-conventions:end -->


<!-- filigran-model-policy:start -->
## GitHub Copilot model usage

To keep token consumption under control, pick the model that matches the task:

- **Opus 4.6** — reserve for complex work: deep reasoning, large refactors,
  architecture design, tricky debugging. It is significantly more
  token-expensive, so it is not the daily driver.
- **Sonnet / Gemini / GPT** — default for everyday tasks: autocomplete, small
  fixes, quick questions, code explanations.

We have a limited token budget — being mindful of the model you pick makes a
real difference at scale. Think of Opus as a specialist you call in when you
really need it.
<!-- filigran-model-policy:end -->

<!-- filigran-design-system:begin (managed by install-skills) -->
## Filigran Design System — usage

When building or changing UI that uses `@filigran/design-system`, follow the
shipped consumer skill and the canonical context it routes to:

> `@filigran/design-system/skills/consumer/design-system-usage/SKILL.md`

Read it before adding a component, styling with tokens, migrating a MUI
element, rendering an icon, or laying out the application shell.
<!-- filigran-design-system:end -->
