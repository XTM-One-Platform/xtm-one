# CI test selection

A pull request runs the static checks of `ci.yml` (about a minute). The merge queue runs the suites, but only what the merge group's change reaches: each of `ci.yml` and `e2e.yml` starts with a `select` job that calls `backend/tests/selection.py` on the group's own change (`merge_group.base_sha..sha`, every pull request batched in it) and sizes the other jobs from its answer. Measuring each group from the one ahead of it is safe because the queue merges only non-failing pull requests (the `Merge queue` ruleset's `grouping_strategy: ALLGREEN`): a pull request merges only once its own group passes, and the groups behind a failed one are rebuilt without it. Under `HEADGREEN` a group's pass would merge the pull requests ahead of it on its own change's tests alone: switch the diff to `main`'s tip first. Every other event - the nightly run on `main`, a manual run, a push to `main` - runs everything, and the nightly run opens an issue when it fails (`notify-on-failure`): it is the full pass that catches what the rules below cannot see.

The `Test selection` and `E2E selection` jobs write a table to their summary: what runs, and why.

## Backend (unit, integration, Browser tool)

A test file runs when the change reaches it through the source:

- **Imports.** A module reaches what it imports as it loads, at any depth (a package `__init__` included). An `app` function's own import counts one step, from a changed module only: followed further, the one function that imports `app.main` would put every module in every closure. A test file, or a helper of `tests/`, reaches everything it imports, at module level or in a test, and every dotted name it writes as a string (`mock.patch("app.x.y")`). Code that runs at import - a Pydantic schema, a constant, a registry - is reached the same way; a coverage map would miss it, its lines running during collection under no test.
- **The test itself** when the change adds or edits it.
- **Tree readers** whenever a `.py` file of `app/` changes, deletions included: a file that reads `app/` through `tests/app_sources.py`, a package walk or `import_module`, or that lists a directory it names `app` (`APP.rglob("*.py")`, the egress and other source tripwires).
- **Path readers.** A file outside `backend/app` selects the tests that write its name, or its area (`frontend`, `.github`, `docs`, `alembic`, a data directory of `tests/`), as a path piece. A data file of `app/` (seed data, templates) reaches the modules that name it or a directory above it.
- **Repository readers** (`git ls-files`: `tests/test_repo_text_hygiene.py`) on every change.
- **A package's Markdown** (`AGENTS.md`, `copyright.md` beside its modules) reaches only a module of that package that writes its name in a string, and the tests that name it: it documents the package, no module reads it.
- **A deleted module** selects what still names it in an import.

The whole suite runs when the change touches what every test runs under: a `conftest.py`, `pyproject.toml` / `uv.lock`, `selection.py` / `sharding.py` / `marked_files.py`, `ci.yml` or the backend-venv action. So does a data file of `app/` no module names, and a backend file the graph cannot parse. A change to `alembic/`, `app/models/` or the database setup runs the integration suite whole. The autouse fixtures of `conftest.py` import the modules whose state they reset; a test that reads that state imports the module too, so a change that breaks the reset reaches it.

The jobs read the selection through `PYTEST_SELECTED_FILES` (a list of test files; `tests/conftest.py`). A file it lists that does not exist stops the run; a job that collects nothing from it passes (a unit shard dealt only integration files). A selected suite takes its share of the shards: the unit suite one shard per ~23 minutes of one worker's tests (`tests/shard_durations.json`, four workers a job, at most eight), the integration (at most four) and Browser tool (at most five: its tests run one at a time) suites in proportion to their files. A suite the change does not reach skips, and `CI verdict` reads a skipped job as passed.

Replayed over `main`'s last 60 merges (18 Nov 2026, timed on the queue's own runs of that day), a merge group runs a median 32% of the unit suite (75th percentile 54%), 54 of the 60 end under ten minutes (median 8, against 12 to 13.5 when every job ran) - the other six change the migration chain, whose job takes about 11 - and the runner time halves. Of the queue runs that failed in the same days, every failure a group's own change caused is in its selection; the others - `main` already red, a flaky a2a spec, a group stacked on a failing one - would no longer eject a pull request that did not cause them.

## Migrations, frontend

- **Migrations (Alembic)** runs when the change touches what the chain runs: `alembic/`, the models, the dependencies (`MIGRATION_INPUTS`), or an `app` module the chain loads - what `alembic/` imports anywhere in a file (`app.core.migration_ddl` in a migration's `upgrade()`) and what those import as they load (`migration_chain_modules`, about 8% of `app`). Otherwise the chain is `main`'s, which passed.
- **Frontend (TypeScript)** (the build and the design-system guards) runs when `frontend/` changes.
- **Frontend unit tests** run `vitest related` on one shard: the tests that import a changed file of `src/` or `tests_unit/`, plus the vitest files and their helpers (`src/testing/`) that read a changed file from disk (an `fs` import, and the file's name or directory in a string literal - a comment or a test title names no path; a file that lists a directory, its area too: `src`, `tests_unit`, or outside `frontend/` `app`, `docs`): `vitest related` follows a helper to the tests that import it. A change to the dependencies, the vite or vitest config or a setup file runs every file on three shards; a change that reaches no vitest file skips the job. The whole-tree gates run on every pull request and merge group anyway (`frontend-gates`).

## E2E

- **None** when every changed file is one the stack does not carry: `dev-docs/`, unit tests, `.xtm-one/`, `scripts/`, the runner and relay sources (`E2E_INERT`). A path the list does not know runs the suite. `docs/` is in the list although the image carries it: the boot only reads it as the text of a knowledge base, as UTF-8, which the repository reader above checks. Markdown is inert too, but under `backend/app` only a package's own (`AGENTS.md`, `copyright.md`): the boot seeds personas, skills and templates from the rest.
- **core** (ten shards) for any other change.
- **a2a** and **evaluation** for their own modules and the modules those import directly, their specs, their UI (a `frontend/src` path naming the area), and what every project runs on: the images and their start script, the compose files, the dependencies, the database and its init script, the suite's harness (a file of `tests_e2e/` outside the areas' directories that is not a core spec) (`E2E_EVERY_PROJECT`). Their code reaches most of `app` through the agentic loop, so a deeper rule would run them on every change; the nightly run covers the rest.

## Running everything on a branch

`gh workflow run ci.yml --ref <branch>` and `gh workflow run e2e.yml --ref <branch>` run every job. The Video Studio ultra render (50 to 85 minutes) runs nightly, opening an issue when it fails, and by hand: `gh workflow run video-studio-ultra.yml --ref <branch>` before merging a change to the render pipeline.

## Shard weights

The nightly unit shards keep their JUnit reports (`unit-junit-*` artifacts). When one shard ends long after the others, refresh the weights:

```bash
gh run download <nightly run id> --pattern 'unit-junit-*' --dir /tmp/junit
cd backend && uv run python scripts/refresh_shard_durations.py /tmp/junit/*/*.xml
```
