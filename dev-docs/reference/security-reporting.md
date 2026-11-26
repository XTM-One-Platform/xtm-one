# Reporting a security problem (public repository)

The repository is public. Anything posted in it (issue, comment, pull request, commit message, test name, code comment, CI log) is readable, indexed and mailed to watchers the moment it is posted, and deleting it afterwards does not recall the notifications. So **a vulnerability never goes to a public place before its fix is released**: it goes to a private GitHub security advisory (GHSA). The user-facing policy is `SECURITY.md`.

## The rule

| What you found | Where it goes |
|---|---|
| A vulnerability: access-control or visibility bypass, data or owner-field exposure, identity or auth flaw, injection, SSRF, sandbox escape, leaked secret, anything an outsider could abuse | A private advisory: `https://github.com/XTM-One-Platform/xtm-one/security/advisories/new`. The fix is developed in the advisory's temporary private fork, never in a public branch or pull request, and is merged from there (see "Fixing in the private fork") |
| A plain hardening task with nothing exploitable in it (a stricter default, a missing guard on an unreachable path, a dependency bump) | A normal issue and pull request, worded neutrally (what to harden, not how to abuse it), with an area label |
| Not sure | Treat it as a vulnerability |

Never, for a vulnerability: an issue, a discussion, a pull request (draft or not), a pull request or issue comment, a commit message, a branch name, a test name that names the attack, a screenshot or a log with a payload. Never the `security` label, on an issue or a pull request: on a public repository the label is a one-search list of unfixed weaknesses. It is retired (`.github/LABELS.md` §5). A `security` scope (`fix(security): ...`) is refused for the same reason.

A vulnerability found in the code of the product the user is working on, by an agent or a person, follows the same rule: the finding goes to the user first, in the private conversation, then to the advisory.

## What enforces it

- **Issue chooser** (`.github/ISSUE_TEMPLATE/config.yml`): blank issues are off for readers and triagers (people with write access still see a "Maintainers only" blank option), and the first link is the advisory form. The bug template warns in its first lines. (`gh issue create --title ... --body ...` and the API bypass the chooser: only the rule and the guard cover them.)
- **`security-label-guard.yml`**: the `security` label put on an issue or a pull request is removed at once, and the guard adds nothing that names the item - no comment, no summary, nothing in the log (the step reads the event file, not a step `env:`, which Actions prints, and fails with a generic message), and every run looks the same (fixed name, the job always runs). It cannot hide the labeling itself: the item's timeline and the repository's public issue events keep it (and a failed removal is the guard's only red run), so a labeled item counts as publicly flagged and is handled as in [When a report was posted publicly](#when-a-report-was-posted-publicly). Spotting and moving the report stays with whoever triages the item.
- **`security` is neither a type nor a scope**: `pr-title-check.yml` refuses a pull request title such as `fix(security): ...`, `security: ...` or `fix(auth,security): ...` (the title becomes the squash commit on `main`), and `.githooks/commit-msg` refuses the same on any line of a commit message (enabled by `make setup-hooks`); `.github/scripts/security-scope.test.sh` holds the two patterns together. A hardening takes the scope of the area it hardens.
- **`issue-type-sync.yml`** no longer adds the `security` label (it used to, from the title and from Aikido findings).
- **Private vulnerability reporting** is enabled on the repository (Settings > Code security): it is what makes the advisory form available to every reporter.
- **Pull request template**: a checkbox that the change discloses no unpatched vulnerability.

## Fixing in the private fork

An advisory's temporary private fork is invisible to integrations: **CI does not run there, and the branch rules and merge queue of `main` are not enforced when the advisory is merged**. So:

1. Develop the fix on a branch of the fork, through a pull request inside the advisory, reviewed there.
2. Before merging, run the full suite on that branch locally: `./scripts/ci-local.sh` (it stands in for the CI that cannot see the fork).
3. A repository admin merges it from the advisory ("Merge pull request(s)"; every open pull request of the fork is merged at once, so none may conflict). The commit lands on the public `main` at that moment: its message is neutral (what it hardens, not how to abuse it), and a release follows right away.
4. Publish the advisory once the release is out (the waiting period of `SECURITY.md`).

## When a report was posted publicly

1. Do not discuss it further in the thread. Open a private advisory with the same facts (an advisory can be opened by a maintainer on the reporter's behalf: `SECURITY.md` process).
2. Remove the exploitable detail: edit the issue or comment, then delete the previous revisions from its edit history ("edited" menu > Delete revision: the history is public otherwise), or have a repository admin delete the issue (Delete issue, in the issue's sidebar; only admins and organization owners can, and the organization allows it). Treat everything posted as already disclosed: the email notifications and search caches may hold it. If it exposes a secret, rotate the secret.
3. Close the thread with a neutral pointer ("handled privately"). Credit the reporter in the published advisory.
4. If the report was a hardening task after all, reword it neutrally and keep it.

**When the fix is already public too** (a public pull request, a merged commit), the private fork protects nothing any more: the race is with the people who read it. Keep the fix in its public pull request, get it reviewed and merged first, cut a release, then publish the advisory (and its CVE) right after, without the waiting period, so self-hosters learn they must upgrade. Clean the public issue once the release is out.

## Reporting from an agent session

An agent (Claude Code, Copilot, a pi worker, an automation) that spots a vulnerability, in review, while fixing something else or from a scanner:

1. Stops writing it down in any public place: no `gh issue create`, no `gh pr create`, no PR review comment, no commit with the details.
2. Tells the user in the conversation: where, what an attacker gains, how to reproduce, and a suggested fix, kept local.
3. Lets the user file the advisory, or files a **draft** advisory only when the user asked it to (`gh api -X POST repos/XTM-One-Platform/xtm-one/security-advisories`, which stays private until published), and never publishes one.
4. Works on the fix in the advisory's private fork, or leaves it in the working tree uncommitted when no advisory exists yet, until the user says where it goes.

A fix that is only a hardening (rule above) keeps the usual flow, with a neutral title, body and test names.

## Settings behind this page

Repository-level settings are not in git, so they are checked by hand when the repository is set up: private vulnerability reporting enabled; the `security` label deleted from the repository's label list (`gh label delete security`: `gh issue create --label security` then fails); a repository ruleset on the default branch whose negated `commit_message_pattern` refuses a `security` scope in any commit message line (the squash commit carries the pull request's commit messages); the scanner's GitHub integration (Aikido) set to open no issues on this repository, or to open them without the vulnerability's details.
