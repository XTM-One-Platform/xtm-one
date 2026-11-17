---
name: release
description: Cut an XTM One release - next date-based tag, release notes from the PRs since the last tag, GitHub release. Dry run by default; --publish creates it.
disable-model-invocation: true
---

# Release

`/release [<sha>] [--publish] [--draft]`

Everything mechanical lives in [release.sh](release.sh): commit and tag
resolution, preflight, note skeleton, the `gh release create` call. Run it
rather than redoing any of it by hand. `release.sh --help` lists the options.

The guard against an accidental publish is `--yes`: pass it only after the user
has said yes to this exact title and body, in this conversation.

Publishing creates the tag, which fires `deploy.yml` (its checks, then the
release build in XTM-One-Platform/release: platform images, the signed runner)
and `mcp-registry.yml` (registry entries). Treat `--publish` as
outward-facing. `--draft` defers the tag and both builds until someone
publishes it in the GitHub UI. This is separate from `scripts/release.sh`
(Copilot CLI, signed tag, draft release), which stays as it is. The tag is a
lightweight, unsigned one, as for the recent releases. `.claude/skills/release/` is
file symlinks: on Windows set `core.symlinks=true`.

## Steps

1. **Dry run** (always first, even for `--publish`):

   ```bash
   .agents/skills/release/release.sh [<sha>] --out <scratchpad>/release
   ```

   Exit 1 = a preflight check failed: report which and stop. Never work around one.
   CI is read from check runs and the legacy commit status. Anything not green blocks,
   including a `cancelled` run superseded by concurrency: re-run it first, unless it is
   Deploy (a replaced Deploy or release run is never re-run: see below). The build
   runs in XTM-One-Platform/release (`deploy.yml` only dispatches it): the commit's
   `Release refs/heads/main at <sha>` run there must have passed, so a release right
   after a merge blocks until it does. A run a newer push replaced ends `cancelled` and
   cannot be re-run: tag the commit the check names as the latest one released there.
   A commit that is not after the latest release tag blocks too.
2. **Judgement the script cannot make.** Copy `release-notes.generated.md` (rewritten
   on every run) to `release-notes.md`, read it, and write it like the last releases
   (`gh release view <prev> --json body -q .body`):
   - Pick the **theme**: the two or three biggest changes, `A, B & C`. It goes in the
     title via `--theme`.
   - Replace the type sections with area sections (`### Presentations`, `### Flows`,
     `### Agents & chat`, `### Integrations`, `### Auth, settings & security`,
     `### Developer experience`...). Reword unclear bullets; keep every `(#ref)`
     and one sentence per bullet. Merge a fix with its feature when it ships together.
   - Write one or two sentences before `**N changes**`, and `### Highlights` after the
     first `---` (3-5 bold-headed paragraphs, refs in parentheses), only if there are
     real features or fixes.
   - Read the "Review before publishing" block and open each alembic migration it
     lists. A migration is **breaking** if it needs a manual step, locks or rewrites a
     large table, or is not backward compatible with the previous release. Put it in
     `### Upgrade instructions`, and fix the "Migrations run automatically" bullet
     to say what happens. A merely added migration is not breaking. The script adds
     commits marked `!:` / `BREAKING` itself; remove "It has no new entries" if
     `UPGRADING.md` changed, and summarise its new entries as bullets.
   - Keep `### Contributors` and the `### Upgrade` footer. Bots are not thanked.
   - No mention of how the work was done, no tool attribution.
3. **Dry run result**: re-run with `--theme` and `--notes-file <your edited copy>`.
   It prints the file that would be published. Show the user the title, body and
   command. Stop unless `--publish`.
4. **`--publish`**: ask the user explicitly and wait for a yes (earlier approval does
   not carry over), then re-run the same command adding `--publish --yes` (and `--draft`
   if asked). Print the release URL.

## Tests

`/bin/bash .agents/skills/release/test.sh` runs `release.sh` against a throwaway
repo with a stub `gh` (needs `jq`).
