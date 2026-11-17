# XTM One Documentation Space

[![Slack Status](https://img.shields.io/badge/slack-3K%2B%20members-4A154B)](https://community.filigran.io)

## Introduction

This is the main directory of the XTM One Documentation space. The online version is available directly on [xtm-one-platform.github.io/xtm-one](https://xtm-one-platform.github.io/xtm-one/).

## Install and display the documentation locally

Clone the repository:

```sh
git clone https://github.com/XTM-One-Platform/xtm-one.git
cd xtm-one/docs
```

Install dependencies with [uv](https://docs.astral.sh/uv/getting-started/installation/) (the locked set of `uv.lock`, into `.venv`):

```sh
uv sync
```

Run local server with hot-reload:

```sh
uv run mkdocs serve
```

It should display the following output in the terminal:

```
> Starting server at http://localhost:8000/
```

Make sure to always build the documentation locally before submitting a pull request, and check for errors and warnings in the terminal output, as they can indicate issues with the formatting of the documentation.

```sh
uv run mkdocs build --strict
```

The `git-committers` plugin reads page contributors from the GitHub API with a token: set `MKDOCS_GIT_COMMITTERS_APIKEY` to show them (for instance `export MKDOCS_GIT_COMMITTERS_APIKEY=$(gh auth token)`; CI sets it from `GITHUB_TOKEN`). Without a token the plugin is switched off (`hooks/git_committers.py`) and the pages build without their contributors section; `MKDOCS_GIT_COMMITTERS_ENABLED=false` switches it off even with one. `./scripts/ci-local.sh --only docs` picks the token up by itself (`MKDOCS_GIT_COMMITTERS_APIKEY`, then `GITHUB_TOKEN`, then `gh auth token`) and disables the plugin, with a notice, when there is none. The red "Warning from the Material for MkDocs team" banner printed at startup is informational: it is not a strict-mode warning.

The plugin keeps the contributors it fetched in `.cache/` (git-ignored) and asks GitHub again only about the pages changed since the day it wrote that cache. A failed REST call (rate limit, 401/403, 5xx) is logged as an error naming the page and the HTTP status, a failed GraphQL call (the co-authors) as a warning, and a connection error aborts the build with a traceback: locally all three fail `--strict`, so build again once GitHub answers. In CI they never fail the Docs build check nor block the Pages deploy: both build again without the contributors section and say so in a warning. CI keeps the cache between runs under a key that changes with `mkdocs.yml`, `uv.lock` and the month, and only the strict Docs build check saves it.

## Contribute

You can contribute to the documentation by submitting a pull request on GitHub. Follow the conventions documented in [AGENTS.md](AGENTS.md).

### Caveats

The following notable issues have been identified in the past and should be checked before submitting a pull request.

#### Bullet or ordered lists not rendered properly

```md
Some text
- Item 1
- Item 2
```

Will not be rendered properly, and displayed as a paragraph.
To fix this, make sure to add a blank line before the first item in the list:

```md
Some text

- Item 1
- Item 2
```

#### Anchored links not working

When linking to a section in the same page, make sure to use the correct anchor link.
For instance:

```md
## My (super) section
```

Will be accessible with the following link: `#my-super-section` (all lowercase, spaces replaced by dashes, and special characters removed).

#### Documentation page not accessible from the sidebar

Make sure to add new documentation pages in the `mkdocs.yml` file under the `nav` section.
Then, make sure to build the documentation locally and check for errors and warnings in the terminal output.

## Deploy the documentation

The deployment relies on `mike` and is automated by the `Docs Pages` GitHub Actions workflow: every push to `main` that touches `docs/**` publishes the docs under the latest release tag (e.g. `1.260705.0`) and updates the `latest` alias, mirroring the OpenCTI and OpenAEV documentation versioning.

To manage versions manually:

### Check existing versions

```sh
mike list
```

### Deploy and update the current version

With the right version number (e.g. 1.260705.0):

```sh
mike deploy --push [version]
```

### Deploy a new stable version

With the right version number, update the `latest` alias:

```sh
mike deploy --push --update-aliases [version] latest
```
