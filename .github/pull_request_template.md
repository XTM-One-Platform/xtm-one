<!--
Title: `type(scope): description (#issue)`, Conventional Commits (CONTRIBUTING.md).
Every pull request is linked to an issue.
Fill in the issue number and every section, and keep every checkbox line.
Leave the Staging box unchecked unless the change needs a check on staging:
while it is checked, feature-branch-deploy.yml deploys the branch on every push.
It deploys branches of this repository only, never a fork's.
From the CLI, copy this file to a scratch file, fill it in, and pass that copy to
`gh pr create --body-file`: `--body` skips the template, and passing this file
itself posts it unfilled.
-->

Closes #

## What and why

## How it was tested

## Checklist

- [ ] The title follows `type(scope): description (#issue)` and the issue is linked
- [ ] All my commits are signed
- [ ] New files and directories have the right edition: agentic is Enterprise Edition (`make edition P=<path>`)
- [ ] External contributor: I signed the Contributor License Agreement, or will when the CLA bot asks; it is required for every external contribution (`CONTRIBUTING.md`)
- [ ] This change discloses no unpatched vulnerability: its title, body, commits and tests name no flaw that is not yet published (`SECURITY.md`)
- [ ] Tests cover the change, and the user docs (`docs/`) and `dev-docs/` it affects are updated

## Staging

- [ ] Deploy this branch to staging (`https://featone-<name>.one.staging.filigran.io`), again on every push while checked; a fork's branch is never deployed

<!--
Optional, read on every deploy (an edit of either deploys again while the box
is checked), and shown in the Actions run and in AWX: no secrets.
- The block: extra env for the platform and worker, one KEY=value per line,
  the value being the rest of the line, verbatim; # comments allowed.
- The chart line: the xtm-suite chart as <repository>:<version>, written in
  place of the # example; empty, or left as the # example, for the latest
  release.
-->
Env. variables:
```env staging
# LOG_LEVEL=debug
```

Staging chart: # filigran/xtm-suite:0.261009.4
