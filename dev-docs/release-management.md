# Release management

XTM One release management covers tagging, immutable image builds, release notes, and communication.

This document records the initial release policy before open sourcing. It is intended for maintainers and release operators, not end-user documentation.

## Version formats

Stable releases use:

```text
MAJOR.YYMMDD.SEQ
```

Example:

```text
1.260520.1
```

Rolling development builds use:

```text
MAJOR.YYMMDD.0-dev.<sha>
```

Example:

```text
1.260520.0-dev.a91bc2d
```

`MAJOR` is incremented manually for product-level breaking changes. `YYMMDD` is the UTC release or build date. `SEQ` is the same-day release counter. The short SHA identifies the exact commit used for rolling development builds.

## Release flow

1. A release maintainer creates a local signed release tag with the release helper script.
2. The tag is pushed to GitHub.
3. GitHub Actions builds the immutable Docker images from the tag and publishes them, together with the `latest` tag.
4. A GitHub Release is published from the tag with curated release notes.
5. Communication is handled separately.

## Tag creation

Release tags are created locally by authorized release maintainers.

```bash
scripts/create-release-tag.sh --dry-run
scripts/create-release-tag.sh --major 1 --push
scripts/create-release-tag.sh --major 1 --ref <commit-sha> --push
```

GitHub Actions does not create release tags. This avoids requiring a workflow with write permissions to create tags and keeps release initiation under explicit human control.

Use `--ref` to create a release tag on a specific commit. The target commit must be after the latest stable release tag for the selected major version.

## Immutable release builds

Pushing a `MAJOR.YYMMDD.SEQ` release tag triggers the Deploy workflow (`.github/workflows/deploy.yml`): it checks the commit, then dispatches the build to `release.yml` in the private XTM-One-Platform/release repository, which holds the Docker Hub and signing secrets.

Release tag builds publish immutable versioned images such as:

```text
xtmone/platform:1.260520.1
xtmone/worker:1.260520.1
```

Pushing a release tag only builds and publishes images. It does not deploy any environment; environment deployments run from the `main` branch.

## Where the release runs

This repository needs no release secret (the old ones are deleted once a release has gone through XTM-One-Platform/release). `deploy.yml` runs the checks (lint, frontend check, fresh-database bootstrap) on a push to `main` or a tag, then the `release` job dispatches `release.yml` in XTM-One-Platform/release with the ref and the commit, through a GitHub App whose client ID and key are secrets of the `deploy-trigger` environment (`RELEASE_APP_CLIENT_ID`, `RELEASE_APP_PRIVATE_KEY`), which only `main` and release tags may use. A tag ruleset on `*.*.*` keeps release tags to the release managers: a tag's own Deploy workflow runs in that environment. That repository checks that the commit is on the ref, builds and signs the runner and relay, pushes the images and deploys through AWX; its runs are listed there, under `Actions` > `Release`, and the Deploy run links to its own.

What the release builds from this repository stays here, next to the code it ships: `apps/runner/scripts/release-compile.sh` and `release-stage.sh` for the runner, `apps/relay/scripts/release-build.sh` and `release-sign.sh` for the relay. The backend tests hold their archive names to the tables the platform serves, and the release runs them from the commit it builds.

To deploy a feature branch to staging, run the Deploy workflow from `main` (`Actions` > `Deploy` > `Run workflow`) and name the branch in the `branch` input; `xtmone_config` and `chart_ref` are passed to the instance. The branch is not checked again here (its own CI does), and it must include this workflow's `release-*.sh` scripts: merge `main` into a branch older than them. The release builds the branch into filigran's private image repositories, unsigned, and deploys `https://featone-<name>.one.staging.filigran.io`.

A pull request can do the same from its description: checking its "Deploy this branch to staging" box (from the pull request template) deploys the branch, and every push deploys it again while the box stays checked, aborting the deployment of the previous push if it is still building or deploying (`feature-branch-deploy.yml`, which dispatches this same feature branch deploy). Under the box, the description holds the deploy's optional inputs, read from the current description on every deploy: an ```` ```env staging ```` block of `KEY=value` lines (`xtmone_config`; the value is the rest of the line, verbatim; `#` comments and blank lines are skipped) and a `Staging chart: <repository>:<version>` line (`chart_ref`, empty for the latest release; a value starting with `#`, such as the template's example, counts as empty). An edit of either deploys again while the box stays checked. Invalid inputs fail the run on the pull request's checks: a line that is not `KEY=value` with an env variable name, a comment or blank (named by its line number), a key set twice, a chart that is not `<repository>:<version>`, two blocks or two chart lines. Both are shown in the Actions run and in AWX: no secrets. No env key is refused here: the instance runs the branch's own code, which can read or set any env anyway, and a dispatch of Deploy or of the release by hand skips this workflow, so a key check belongs in the release's `resolve-target.sh`. A pull request from a fork is never deployed.

## Latest tag

The `latest` tag is assigned automatically by `docker/metadata-action` when a release tag is built, so the release build publishes both the immutable versioned image and `latest` from the same build. There is no separate promotion step.

## Release notes

Each release is published as a GitHub Release with curated release notes, grouped into highlights followed by per-area changes.

Run `scripts/release.sh` to create the release tag, push it, and open a draft GitHub Release whose notes are generated from the Git history (via `scripts/generate-release-notes.sh`) following the established release template. Each phase can be toggled independently, and `--dry-run` previews everything without making changes:

```bash
scripts/release.sh --dry-run
scripts/release.sh --major 1
scripts/release.sh --notes-only
```

Generated release notes are drafted on the GitHub Release, not committed to the repository.

An agent can run the release instead with the `release` skill (`.agents/skills/release/`, `/release`). It creates the tag with `gh release create` on the latest `origin/main` commit or a given one, checks CI first, including that the commit's main release passed in XTM-One-Platform/release, and is a dry run unless `--publish --yes` is passed. It does not use the signed-tag script above: the tag it creates is lightweight and unsigned, like those of the recent releases. That is acceptable for the tag-triggered `deploy.yml` and `mcp-registry.yml`: they only check that the tag name matches `MAJOR.YYMMDD.SEQ`, never its signature or type. Its fixture test and `shellcheck` run on pull requests through `.github/workflows/release-skill-ci.yml`, on Ubuntu and on macOS (bash 3.2).

When a release requires operator action on existing deployments (for example a database migration change), document the exact steps in [`UPGRADING.md`](../UPGRADING.md) and surface them in the release notes under an **Upgrade instructions** heading. The single-baseline Alembic squash is the current example: existing deployments must re-stamp `alembic_version` to the baseline revision before deploying.

## Communication

Communication is a separate phase after the release is published.

The communication workflow is not automated yet. The expected channels should be confirmed before implementation:

- GitHub Release
- public documentation
- Slack
- Teams
- internal email, if required

## Permissions

Release-related actions should be protected and limited to authorized maintainers.

The production GitHub environment should define the required reviewers for release-publication workflows.

## Deferred topics

The following topics are intentionally deferred:

- SBOM generation
- Docker image signing
- SLSA provenance
- registry retention policy
- LTS automation
- release communication automation
