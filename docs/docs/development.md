# Development

This page covers how to set up a local development environment and contribute to XTM One.

## Prerequisites

- Docker with Docker Compose 2.24 or later, or Podman (see [Podman](#podman))
- Git
- To run the checks and tests on the host: [uv](https://docs.astral.sh/uv/getting-started/installation/), which also provides Python 3.14, and Node.js 24 (22.18 at minimum) with Yarn

## Local setup

```bash
# Clone the repository
git clone https://github.com/XTM-One-Platform/xtm-one.git && cd xtm-one

# Copy environment file
cp .env.sample .env

# Run the full stack: PostgreSQL + pgvector, Redis, Silo, platform, worker, frontend
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

`docker-compose.dev.yml` is an override of the production `docker-compose.yml`: the same stack, with the platform and the worker running your checkout's backend sources and a Vite dev server in place of the built frontend. To drop the `-f` flags, set `COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml` in `.env` (`;` as the separator on Windows); the commands below then read `docker compose ...`.

The root `Makefile` wraps these commands: `make up`, `make start`, `make logs s=platform`, `make psql`, `make down` (`make` lists them all). It runs `docker compose`, or `podman compose` when `docker` is not installed; force one with `ENGINE=podman` (or `ENGINE=docker`), add profiles with `PROFILES="tools ha"`. `make up` and `make start` first check that the stack's host ports are free: when another checkout or process holds one, they stop, name it, and print a free set of [host ports](#host-ports) for `.env`.

The frontend is at http://localhost:3100 and the platform API at http://localhost:8100. Both run with hot reload: the platform restarts on a change under `backend/app` or `backend/alembic`, the worker on a change under `backend/app`, and Vite updates the page in place. The platform migrates the database on startup.

- Run `up --build` again after a change to `backend/pyproject.toml` or `backend/uv.lock`. Frontend dependencies install on every start of the `frontend` service.
- Start it in the background with `up -d --build`, follow it with `logs -f platform worker frontend`, stop it with `stop`.
- Every `.env` setting reaches the platform and the worker. `REDIS_URL` and `S3_ENDPOINT` are the stack's own services and override `.env` (a Redis password set there is not used); `OPENSANDBOX_DOMAIN`, `BASE_URL` and `FRONTEND_URL` default to the stack's own addresses.
- Every checkout, git worktrees included, is a compose project of its own, with its own database, Redis and bucket. To run two checkouts at once, give the second one free host ports in its `.env` (see [Host ports](#host-ports)).
- Where bind mounts deliver no file change events (`podman machine`, or a Windows checkout under Docker Desktop), set `WATCHFILES_FORCE_POLLING=true` and `CHOKIDAR_USEPOLLING=true` in `.env`.
- Open a database shell with `docker compose -f docker-compose.yml -f docker-compose.dev.yml exec postgres psql -U copilot`. PostgreSQL, Redis and Silo also listen on the host for the tests.
- Every port is published on `127.0.0.1` only. To reach the stack from another device (a phone, a second laptop), set `DEV_BIND_ADDRESS=0.0.0.0` in `.env`: PostgreSQL, Redis, Silo and the admin account then answer the network with their dev credentials.

### Host ports

Each host port the stack publishes is set by a `.env` variable:

| Variable | Default | Service |
|---|---|---|
| `DEV_FRONTEND_PORT` | `3100` | Vite dev server (`FRONTEND_URL` follows it) |
| `DEV_PLATFORM_PORT` | `8100` | Platform API (`BASE_URL` follows it) |
| `DEV_POSTGRES_PORT` | `5432` | PostgreSQL |
| `DEV_REDIS_PORT` | `6380` | Redis |
| `DEV_SILO_PORT` | `9002` | Silo S3 API |
| `DEV_SILO_CONSOLE_PORT` | `9003` | Silo console |
| `DEV_REDISINSIGHT_PORT` | `5540` | RedisInsight (`tools` profile) |

A second checkout running beside the first, for example a git worktree, takes another set in its own `.env`:

```bash
DEV_FRONTEND_PORT=3110
DEV_PLATFORM_PORT=8110
DEV_POSTGRES_PORT=5442
DEV_REDIS_PORT=6390
DEV_SILO_PORT=9012
DEV_SILO_CONSOLE_PORT=9013
DEV_REDISINSIGHT_PORT=5550
```

Its frontend is then at http://localhost:3110. Tests run from the host against that stack take the matching `DATABASE_URL`, `REDIS_URL` and `S3_ENDPOINT`; `scripts/ci-local.sh` reads them from the running stack.

### Optional profiles

| Profile | What it adds |
|---|---|
| `tools` | RedisInsight on http://localhost:5540. |
| `ha` | A Redis Sentinel topology for failover testing; the header of `docker-compose.dev.yml` lists the `.env` settings that switch the backend to it. |

Enable one with `--profile <name>`, or with `COMPOSE_PROFILES=<name>` in `.env`.

### OpenSandbox

The dev stack always runs the OpenSandbox server of `docker-compose.yml` (the `opensandbox` service, which `docker-compose.yml` alone starts only with the `opensandbox` profile), which starts the sandboxes as containers through the container engine's socket. In the dev stack it sits on a network that only the platform and the worker join, so no other container reaches it, and it requires a key: `OPENSANDBOX_API_KEY` from `.env`, else `copilot-dev-opensandbox`, which the platform and the worker then send too. The platform uses it with `CUSTOM_TOOL_SANDBOX=opensandbox` and `OPENSANDBOX_API_KEY` set in `.env`. Build the sandbox images it runs once, `backend/sandbox/build-image.sh sandbox-python:0.4` and `backend/sandbox/build-image.sh sandbox-nodejs:0.4` (the script needs `jq` and the `docker` command: with Podman, install `podman-docker`). The default `OPENSANDBOX_IMAGES` names those tags, so there is nothing to set in `.env`.

### Podman

The compose files run unchanged with `podman compose`, whichever provider it uses (`docker-compose`, or `podman-compose` 1.6 or later):

```bash
podman compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

- Under `podman machine` (macOS, Windows) the VM's file share delivers no change events: set `WATCHFILES_FORCE_POLLING=true` and `CHOKIDAR_USEPOLLING=true` in `.env`, or nothing reloads.
- On macOS the VM cannot read a checkout under `~/Documents`, `~/Desktop` or `~/Downloads` (bind mounts fail with `operation not permitted`) unless you grant it access in System Settings > Privacy & Security > Files and Folders. Keeping the checkout elsewhere also works.
- Rootless Podman on Linux serves no `/var/run/docker.sock`, which the `opensandbox` service mounts, so the stack does not start without it: enable the user socket (`systemctl --user enable --now podman.socket`) and set `CONTAINER_ENGINE_SOCKET=$XDG_RUNTIME_DIR/podman/podman.sock` in `.env`.
- Bind mounts carry the `z` option, so they work on SELinux hosts (Fedora, RHEL), and image names are fully qualified (`docker.io/...`), so Podman's short-name policy never prompts.

### Python dependencies

The backend (`backend/`) and the documentation site (`docs/`) are [uv](https://docs.astral.sh/uv/) projects: `pyproject.toml` declares the dependencies and `uv.lock` pins every one of them, transitive ones included. In either directory:

```bash
uv sync                         # install the locked set into .venv
uv add <package>==<version>     # add a dependency and relock; commit pyproject.toml and uv.lock
uv run <command>                # run a command in that environment
```

CI and the Docker images install with `--locked`, which fails when `pyproject.toml` was changed without relocking.

`uv run` syncs `.venv` to the checkout's lock before it runs the command. In a worktree whose `backend/.venv` is a link to another checkout's venv, that rewrites the shared environment: run `uv run --no-sync`, or set `UV_NO_SYNC=1` in that shell.

## Project structure

```
xtm-one/
├── backend/           → Python / FastAPI (see backend/AGENTS.md)
├── frontend/          → React 19 / TypeScript (see frontend/AGENTS.md)
├── docs/              → Product documentation (MkDocs)
├── apps/              → Platform app packages
└── docker-compose.yml → Production stack
```

## Running checks

```bash
# Backend lint and type check
cd backend && uv run ruff check . && uv run mypy

# Frontend lint, type check, dead code (knip) and unit tests
cd frontend && yarn check

# Office add-in type check (an npm package of its own; its unit tests run in `yarn check` above)
cd apps/microsoft-office && npm ci && npm run check

# Backend tests
cd backend && SECRET_KEY=test uv run python -m pytest -m "not integration" -q -n auto
```

`main` takes pull requests through a merge queue. On a pull request, the `CI` workflow (`.github/workflows/ci.yml`) runs these lint, type and dead-code checks, the migration history check, the secret scan, the ROI ledger's invariants, the frontend ratchet gates and catalog tests, and the frontend build; its tests and database checks skip, as do the jobs of `E2E Tests`, whose check passes. Once the pull request enters the queue (**Merge when ready**), the queue runs the suites of both workflows on the commit `main` would move to: the pull request merged on top of `main` and of the pull requests ahead of it, since two pull requests each green on their own can break it together. It runs only what the merge group's change reaches - the test files whose imports lead to a changed module, the E2E projects whose code or specs changed, Migrations when the chain's inputs did - and the `Test selection` job's summary says what ran and why (`dev-docs/ci-test-selection.md`). A change to what every test runs under (a `conftest.py`, the dependencies, the workflow) runs everything. A failure takes the pull request out of the queue. `main` runs both whole nightly, the full pass that catches what the selection missed (a red nightly run opens an issue), and when a file their caches follow changes (the lockfiles, the Dockerfiles, the workflow itself), since only `main` can save the caches the queue restores. To run them by hand on a branch, open **Actions**, pick **CI** (or **E2E Tests**), click **Run workflow** and choose the branch, or run `gh workflow run ci.yml --ref <branch>` and `gh workflow run e2e.yml --ref <branch>`. Their checks show on the pull request, and a new run on the same branch cancels the previous one. `./scripts/ci-local.sh` runs the same suites on your machine (`CONTRIBUTING.md`).

Since the queue, not a person, pushes to `main`, the GitHub app's `commits` feed names `github-merge-queue[bot]`. On every push to `main`, the `Merge notice` workflow (`.github/workflows/merge-notice.yml`) hands the push to `XTM-One-Platform/release`, which posts each merged pull request to the team's Teams channel with its author and who merged it (who added it to the queue); the Teams webhook is a secret of that repository (its README, *Merge notice*).

The `Makefile` runs both test suites from the host:

- `make test` runs the backend unit tests as CI does, from `backend/.venv`, which it syncs with `uv sync --locked` whenever `pyproject.toml` or `uv.lock` changes (uv provides the Python of `backend/.python-version`). Narrow the run with `ARGS`, e.g. `make test ARGS="tests/test_x.py -k name"`.
- `make e2e` runs the Playwright `core` suite against the running dev stack (`make start` first), at `E2E_BASE_URL` from `frontend/.env.e2e` or `http://localhost:3100`. It logs in with `E2E_USER_EMAIL` and `E2E_USER_PASSWORD` from `frontend/.env.e2e` (copy `frontend/.env.e2e.example`). One spec: `make e2e ARGS="tests_e2e/x.spec.ts"`. `scripts/ci-local.sh --only e2e` runs the suite as CI does, on a production build of its own.

### Running tests in parallel

CI runs every backend suite under [pytest-xdist](https://pytest-xdist.readthedocs.io/) (`-n auto` locally, `-n logical` in CI), and so can you. CI also splits each suite between several jobs: with `PYTEST_SHARD=<index>/<total>` a run keeps every `<total>`-th test of the sorted test ids (`tests/sharding.py`), so a test runs in exactly one job. The jobs that run one marker's tests (`-m integration`, `-m chromium`) also set `PYTEST_MARKED_FILES=<marker>`: they collect only the test files whose source spells `mark.<marker>` (`tests/marked_files.py`), so such a test applies its marker in its own file. The real-browser tests (`-m chromium`) are the exception to xdist: they time renders and sign-in redirects, so each of their jobs runs them one at a time. What that asks of a test:

- **No test depends on another one.** The suite already runs in random order (pytest-randomly); xdist spreads it over processes too.
- **Every process collects the same test ids.** A `parametrize` value built from `uuid.uuid4()`, `time.time()` or a freshly generated key turns into a different id in each worker, and the run stops with `Different tests were collected between gw0 and gw1`. Give such a parametrization `ids=` (or `pytest.param(..., id=...)`): the value can stay random, its name cannot.
- **A PostgreSQL test works in a database of its own**, through `tests/scratch_database.py`. The schema is built once per process into a template database and every scratch database is a copy of it, so a new suite should use `scratch_database(...)` rather than a `CREATE DATABASE` + `create_all` of its own.
- **A test reading the backend's own source** (a tripwire over every module under `app/`) reads it through `tests/app_sources.py`, which parses the tree once per process instead of once per suite.
- **A module that owns a fixed resource stays on one process.** `tests/test_bootstrap.py` starts one backend on port 8099; it is marked `pytest.mark.xdist_group(...)` so that all its tests run in the same worker. The group only applies under `--dist loadgroup`, which CI and `scripts/ci-local.sh` pass: add it to a local `-n` run (`uv run python -m pytest -m integration -n auto --dist loadgroup`).

The integration suite needs PostgreSQL, Redis and Silo, and a database role allowed to create databases: `./scripts/ci-local.sh --only integration` starts them and runs it the way CI does.

`scripts/ci-local.sh` runs one at a time per clone: a run started while another is in progress, from any checkout or worktree, prints `Another local CI run is in progress` and waits for it to finish, since runs share the machine and the database on port 5432. `CI_LOCAL_NO_LOCK=1` skips the wait; `CONTRIBUTING.md` describes the queue.

The Playwright suite runs in CI as several jobs, each with a stack of its own: `E2E_SHARD=<index>/<total>` deals the spec files of a project to the jobs by the seconds each took on CI (`frontend/tests_e2e/shard-timings.json`). The nightly runs measure these seconds and publish the median of their last five measures, which every later run deals by; the committed file is only the fallback, refreshed by hand with `backend/scripts/shard_weights.py refresh --kind e2e` (`dev-docs/ci-test-selection.md`).

The suite runs in the merge queue, not on a pull request, whose `E2E (Playwright / Chromium)` check passes with its shards skipped. In the queue it runs the projects the change reaches: none for docs or unit tests alone, `core` for any other change, `a2a` and `evaluation` for their own code, specs and UI or for what every project runs on (the images, the dependencies, the database). To run it on a branch before, start it by hand: `gh workflow run e2e.yml --ref <branch>`.

### Live LLM smoke suite (opt-in)

The provider unit tests run against mocked SDK clients, so they cannot tell whether a real API accepts the requests the platform builds. `backend/tests/live/` sends those requests to the real Anthropic, Gemini and OpenRouter APIs with tiny prompts. It spends real money (cents per run), never runs in CI, and is skipped unless `LIVE_LLM_SMOKE=1` is set and the provider's key is exported as `LLM_PROVIDER_ANTHROPIC_API_KEY`, `LLM_PROVIDER_GEMINI_API_KEY` or `LLM_PROVIDER_OPENROUTER_API_KEY`:

```bash
cd backend && LIVE_LLM_SMOKE=1 SECRET_KEY=test uv run python -m pytest tests/live -v -rfEs
```

A provider whose key is not set is skipped. `backend/tests/live/README.md` lists the cases, the model overrides and how the keys are kept out of the test output.

## Migrations

Schema changes must use Alembic migrations. Never use inline SQL.

```bash
cd backend
uv run python -m alembic -c alembic/alembic.ini revision --autogenerate -m "description of change"
```

The platform auto-migrates on startup — no manual `alembic upgrade` needed in production.

### One head, always

The migration history must have exactly **one head**. Two pull requests that each add a
migration chained off the same parent fork the history the moment both land — and each one
is green on its own, because pull request CI validates the branch against `main` *as it was
when that run started*. This has happened twice on `main`, and both times it surfaced days
later as an unrelated pull request going red.

Three guards catch it:

| Guard | Where | Catches |
| --- | --- | --- |
| `tests/test_migration_integrity.py` | *Backend lint* CI job, every pull request, on GitHub's merge of the pull request into its base | A fork you created, and one created by *another* migration that landed on `main` after your branch was cut |
| The single-head check | *Migrations (Alembic)* CI job, in the merge queue on a change to a migration, a model, a module the chain loads (the migration helpers, the database setup) or the backend's dependencies | Two pull requests merged seconds apart: each is tested on top of the ones ahead of it in the queue |
| The nightly run on `main` | *Migrations (Alembic)* CI job | A fork that reached `main` anyway |

If the migration history check fails on your pull request while it passes on your machine, `main` has moved under you (`./scripts/ci-local.sh` runs it against the live tip of `main` too):

```bash
git fetch origin main && git rebase origin/main
# re-point your earliest migration's down_revision to main's new head
cd backend && uv run python -m pytest tests/test_migration_integrity.py
```

When a migration lands on `main`, every open pull request that touches
`backend/alembic/versions/` also gets an automatic comment asking for that rebase.

!!! note "Pull requests stacked on an integration branch"

    CI runs whatever branch you target, merged into that branch, not into `main`. A fork you introduced is still caught by the
    first guard above; one created by a migration that landed on `main` shows once the
    integration branch itself goes to `main`. Rebase the integration branch onto `main` to see
    it earlier.

!!! note "Repository settings"

    The guards above detect a fork; they cannot *prevent* two green pull requests from being
    merged seconds apart. The **merge queue** on `main` does: the "Merge queue" ruleset of the
    repository requires it (squash, entries tested on top of the ones ahead of them). Every
    required check must run on `merge_group` events or the queue never drains, and must report
    on the pull request too, or it never enters the queue: on a pull request `E2E Tests` skips
    its shards and its check passes, and `CI` runs its lint jobs. The checks to require are `CI verdict` (it fails
    when any job of `CI` fails, the lint jobs included, reads a skipped one as passed, and runs
    even when the run is cancelled) and `E2E (Playwright / Chromium)` - the aggregators, never a
    shard, and no workflow with a `paths` filter on `pull_request` or `merge_group`.

## Commit conventions

All commits follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```
type(scope?): description (#issue)
```

Types: `feat`, `fix`, `chore`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `revert`.

## Next step

For product documentation guidelines, see `docs/AGENTS.md`. For backend and frontend specifics, see `backend/AGENTS.md` and `frontend/AGENTS.md`.
