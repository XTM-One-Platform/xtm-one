# Contributing to XTM One

Thank you for reading this documentation and considering making your contribution to the project. Any contribution which helps us improve the platform is valuable and much appreciated. If it is also meaningful to you or your organisation it's all for the best.

In order to help you understand the project, where we are heading and how you can contribute, below are several resources and answers.

Do not hesitate to shoot us an [email](mailto:contact@filigran.io) or join us on our [Slack channel](https://community.filigran.io). Most of the articles below are an introduction for our [detailed documentation](https://docs.xtmone.io/).


## Why contribute?

XTM One is the open source, AI-native cockpit for threat-informed defense. It connects [OpenCTI](https://filigran.io/platform/opencti/) (cyber threat intelligence) and [OpenAEV](https://filigran.io/platform/openaev/) (adversarial exposure validation) to help security teams understand their threats, validate their defenses and operationalize Continuous Threat Exposure Management (CTEM). If you want to know more about XTM One, you can read the [detailed documentation](https://docs.xtmone.io/).

Whether you are an organisation or an individual working or studying in the field of cybersecurity and cyberdefense, or simply as an individual looking for a technical challenge, contributing to the XTM One project may represent a great opportunity for you.

* You can help grow the community and a tool focused on improving cyber threat management and therefore enhancing our ability to better protect our organisations and societies;

* You will be able to adapt the tool to your core interests and methods of work by developing features or fixing bugs you are most interested in;

* XTM One is also an interesting opportunity for developers to work on modern technologies such as a Python/FastAPI backend and a React/TypeScript frontend.


## Where is the project heading?

Our goal for future releases is two-fold:

* Of course, fix bugs and develop features which are identified as non-critical but would really add up to XTM One's power;

* On a longer term vision, we would like to continue deepening the integration between threat intelligence and exposure validation, expanding the catalog of threat management use cases and bringing AI even closer to the daily work of security teams.


## Code of Conduct

XTM One has adopted a Code of Conduct that we expect project participants to adhere to. Please read the [full text](CODE_OF_CONDUCT.md) so that you can understand which actions will and will not be tolerated.


## How can you contribute?

Any contribution is appreciated and many don't imply coding. Contributions can range from a suggestion for improving documentation, requesting a new feature, reporting a bug, to developing features or fixing bugs yourself.

For general suggestions or questions about the project or the documentation, you can open an issue on the repository. We will answer as soon as possible. If you do not wish to publish on the repository, please see the section below [**"How can you get in touch for other questions?"**](#how-can-you-get-in-touch-for-other-questions).

* **Never report a security vulnerability in an issue, a pull request or a comment: this repository is public.** Report it privately with a [GitHub security advisory](https://github.com/XTM-One-Platform/xtm-one/security/advisories/new), as described in [`SECURITY.md`](SECURITY.md).

* Just using XTM One and opening issues if everything is not working as expected will be a huge step forward. To report a bug, please refer to the [bug reporting module](https://github.com/XTM-One-Platform/xtm-one/issues/new?template=1-bug_report.md). To suggest a new feature, please fill in the feature request [form](https://github.com/XTM-One-Platform/xtm-one/issues/new?template=2-feature_request.md).

* Don't hesitate to flag us an issue with the documentation or the templates if you find them incomplete or not clear enough. You can do that either by opening a [bug report](https://github.com/XTM-One-Platform/xtm-one/issues/new?template=1-bug_report.md), a [question](https://github.com/XTM-One-Platform/xtm-one/issues/new?template=4-question.md), or by sending us a message on our [Slack channel](https://community.filigran.io).

* You can look through opened issues and help triage them (ask for more information, suggest workarounds, suggest labels, flag issues etc.)

* If you are interested in contributing to developing XTM One, please refer to the [detailed documentation](https://docs.xtmone.io/). It can be either to fix an issue which is meaningful to you, or to develop a feature requested by others.

### Issue titles and labels

All issues, commits and pull requests follow the shared Filigran title and label convention. The full taxonomy lives in [`.github/LABELS.md`](.github/LABELS.md). In short: Conventional Commits titles (`type(scope?)!?: description`, lowercase, no trailing period; preserve acronyms such as `XTM One`, `OpenCTI`, `OpenAEV`, `LLM`, `STIX`), one primary type label matching the title prefix (`feature` for `feat:`, `bug` for `fix:`, `documentation` for `docs:`), optional area labels (e.g. `integrations`, `microsoft-teams`, `llm-router`, `agents`), and no deprecated labels (`enhancement`, `feature-request`).

The GitHub **Type** (Feature / Bug / Task) and the type label follow the title prefix automatically: the [issue type sync](.github/workflows/issue-type-sync.yml) workflow sets both whenever an issue, open or closed, is opened, retitled, relabelled or retyped, and a daily sweep catches anything it missed. To change an issue's type, change its title prefix - a label or Type that contradicts the prefix is put back. An issue without a prefix takes the type label or Type it was last given, and an issue with a Type other than Feature, Bug or Task is left alone.

### Pull requests and commits

* All Pull Request titles must follow the Conventional Commits format: `type(scope?): description (#issue-number)`

  - `type` must be one of: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`
  - `scope` is optional, a lowercase noun describing the affected part of the code (e.g. `backend`, `frontend`, `integrations`, `docs`). The old `[component]` bracket prefixes are **discontinued** — use a scope instead.
  - `#issue-number` is required on the Pull Request title (it becomes the squash-merge commit) and the Pull Request must be linked to an issue. **Renovate** pull requests are exempt.

  **Example:** `feat(integrations): add OpenCTI dashboard creation (#123)`

* All commits must be signed. If you need to configure your git environment, please see the [GitHub documentation on signed commits](https://docs.github.com/en/authentication/managing-commit-signature-verification/signing-commits).

* All Pull Requests must be linked to an issue and either fix a bug, implement a feature or improve documentation.

* The Pull Request description is the [template](.github/pull_request_template.md) filled in (its comment says how).

* Documentation should be included in the Pull Request, in the `docs` folder.

* As much as possible, provide advice on how to test the feature or the bug fix you implemented in the Pull Request.

### Run CI checks locally before pushing

A pull request runs the static checks of the `CI` workflow (lint, type checks, secret scan, and the database checks when it changes a migration or a model); the test suites, the builds and `E2E Tests` run in the merge queue, once you click **Merge when ready**, and a failure there takes the pull request out of the queue. To check them before, when needed, run them on your machine with the local CI runner script below, or on GitHub from **Actions > Run workflow** on your branch.

Use the local CI runner script to execute the same families of checks as GitHub Actions before pushing:

```bash
./scripts/ci-local.sh
```

Useful variants:

```bash
# Skip e2e when you only need fast backend/frontend/migrations/docs checks
./scripts/ci-local.sh --no-e2e

# Run only selected groups
./scripts/ci-local.sh --only backend,frontend
```

Default groups covered by the script: backend lint (ruff, mypy) + unit tests (the real-browser ones too when Playwright's Chromium is installed: `cd backend && uv run python -m playwright install chromium`), every step of the frontend jobs of CI in order (typecheck of the app and the e2e specs, oxlint, knip, command manifests, i18n catalogs, unit tests, the design-system guards, the Office add-in's typecheck), migration checks (the post-merge one included), backend integration tests, docs build, and Playwright e2e. The secret scan is left to CI, which runs it on every pull request.

The platform image build does not typecheck the frontend: a `docker compose build` and the script's e2e group bundle it with `vite build` alone. Type errors are caught by the frontend group, by `cd frontend && yarn check`, or by the Frontend lint job of CI.

Runs are queued: when a run is already in progress in any checkout of the clone (main checkout or worktree), the next one prints `Another local CI run is in progress ...` and waits for it to finish. Each run takes every core and several GB of memory, and all runs share one database, so parallel runs would exhaust the machine and break each other's integration tests. The queue is a lock on `ci-local.lock` in the clone's git common directory (`CI_LOCAL_LOCK_FILE` overrides it). The lock is held by a small `python3` process (`scripts/ci-local-lock.py`), and the kernel releases it when that process exits, so a killed run leaves no stale lock. The holder exits once the run is over, even if the caller has not reaped the run's shell yet. When the run leads its own process group, as a terminal job does, the holder also waits for the checks a `kill -9` of the shell left running (pytest, yarn). A run started by a tool that keeps it in the tool's own process group does not get that wait: kill such a run with its whole group, never its shell alone. Without `python3` or `flock(2)` (Git Bash on Windows), the run prints a warning and goes on without the queue. `CI_LOCAL_NO_LOCK=1` starts without waiting.

The script runs from the main checkout or from a linked `git worktree`. It starts PostgreSQL, Redis and Silo with the dev override (`docker-compose.yml` + `docker-compose.dev.yml`) in the checkout's own compose project, so it reuses the dev stack you run there. The tests reach those services with the dev credentials, so a `.env` that changes `DB_PASSWORD` or the S3 keys breaks them. The Playwright stack (`docker-compose.yml` alone) pins container names, so every checkout shares a single compose project for it, named after the main checkout with an `-e2e` suffix: it never recreates the dev stack, whose services have the same names. `COMPOSE_PROJECT_NAME` overrides the dev project, and the e2e one becomes `<name>-e2e`.

The dev stack (`make up`, which wraps `docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build`) pins no container name: every checkout and every **linked worktree is a compose project of its own**, with its own PostgreSQL database, Redis and S3 bucket in volumes of its own. A branch carrying an unreleased migration therefore never stamps another worktree's `alembic_version`. To run two at once, give the second one its own host ports with the `DEV_*_PORT` settings of its `.env` (`make up` checks the ports first and prints a free set; see `docs/docs/development.md`); a new worktree starts from an empty database, so its first run migrates from scratch and you will need to seed whatever data you need.

### Editions and licensing

XTM One has a Community Edition (Apache License 2.0) and an Enterprise Edition (XTM One Enterprise Edition License), as described in [`LICENSE`](LICENSE). A source file that starts with the Enterprise Edition notice, or that sits in a directory holding a `copyright.md` notice (except the third-party files that notice names), is Enterprise Edition; every other file is Community Edition. Every external contribution requires the Filigran Contributor License Agreement (CLA), whether it touches Community or Enterprise Edition files: code contributed as Community Edition may later move into an Enterprise Edition file, and for the Enterprise Edition `LICENSE` requires it too. The CLA bot is coming; Filigran employees are exempt.

Before you add a file, `make edition P="path/to/file"` tells which edition it will be (`licensing/editions.yml` decides it): an EE file published as CE cannot be taken back, the other way round can. Each directory is wholly EE, wholly CE or mixed: in a mixed one, a new file is classified in the same change (an `ee` glob in `licensing/editions.yml`, or `SPDX-License-Identifier: Apache-2.0` on its first line for CE, which `make mark-ce P=<path>` writes), and so is a new directory, or the build fails. A moved or split CE file stays CE. Never write the license header by hand: `make license` writes it, and `make setup-hooks` (once) has git check each commit. CI fails when a header is missing or misplaced. Details: [`dev-docs/reference/editions.md`](dev-docs/reference/editions.md).

### How can you get in touch for other questions?

If you need support or you wish to engage a discussion about the XTM One platform, feel free to join us on our [Slack channel](https://community.filigran.io). You can also send us an [email](mailto:contact@filigran.io).
