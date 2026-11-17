#!/usr/bin/env bash
# Fixture test for release.sh: a throwaway git repo with a bare `origin`, and a
# stub `gh` on PATH that returns canned answers. No network, nothing published.
# Run it with the bash you want to cover (macOS: /bin/bash is 3.2):
#   /bin/bash .agents/skills/release/test.sh
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd)
release="$here/release.sh"
command -v jq >/dev/null || { echo "jq is required (the gh stub runs release.sh's --jq filters)" >&2; exit 2; }
tmp=$(mktemp -d)
trap '[ -n "${tmp:-}" ] && rm -rf "$tmp"' EXIT
# Fixed by the `date` stub below, so a run crossing midnight UTC cannot disagree with it.
today=261012
passed=0; failed=0

pass() { passed=$((passed + 1)); }
fail() { failed=$((failed + 1)); echo "FAIL: $1"; }
assert_eq() { if [ "$2" = "$3" ]; then pass; else fail "$1: expected '$2', got '$3'"; fi; }
assert_has() { if grep -qF -- "$3" <<<"$2"; then pass; else fail "$1: missing '$3'"; fi; }
assert_lacks() { if grep -qF -- "$3" <<<"$2"; then fail "$1: unexpected '$3'"; else pass; fi; }

# Signing off: the fixture must not need the developer's signing key.
git_() { git -c user.name=t -c user.email=t@example.com -c commit.gpgsign=false -c tag.gpgsign=false "$@"; }

# gh stub: runs release.sh's real --jq filters (via jq) against canned JSON.
# FAKE_RUNS_JSON: one check-runs page per line; --paginate returns all, otherwise the
# first only. FAKE_STATUS_JSON: the combined-status object. FAKE_RELEASE_RUNS_JSON: the
# release repository's runs. FAKE_GH_FAIL: a word that makes the matching call exit 1
# (check-runs, "run list", generate-notes, "release create"). FAKE_GH_LOG: every call,
# one line each.
mkdir -p "$tmp/bin"
cat >"$tmp/bin/gh" <<'STUB'
#!/usr/bin/env bash
args=(); filter="."
while [ $# -gt 0 ]; do
  case "$1" in --jq) filter=$2; shift 2 ;; *) args+=("$1"); shift ;; esac
done
printf '%s\n' "${args[*]}" >>"$FAKE_GH_LOG"
[ -z "${FAKE_GH_FAIL:-}" ] || [[ "${args[*]}" != *"$FAKE_GH_FAIL"* ]] || { echo "gh stub: forced failure" >&2; exit 1; }
case "${args[*]}" in
  *check-runs*)
    if [[ "${args[*]}" == *--paginate* ]]; then printf '%s\n' "$FAKE_RUNS_JSON"
    else printf '%s\n' "$FAKE_RUNS_JSON" | head -1; fi | jq -r "$filter" ;;
  *"/status"*)       printf '%s\n' "$FAKE_STATUS_JSON" | jq -r "$filter" ;;
  "run list"*)       printf '%s\n' "$FAKE_RELEASE_RUNS_JSON" | jq -r "$filter" ;;
  *generate-notes*)  cat "$FAKE_NOTES" ;;
  "release create"*) printf '%s\n' "${args[@]}" >"$FAKE_RELEASE_LOG" ;;
  *) echo "gh stub: unexpected: ${args[*]}" >&2; exit 99 ;;
esac
STUB
# date stub: the release day is pinned, and must be asked for in UTC.
cat >"$tmp/bin/date" <<'STUB'
#!/usr/bin/env bash
if [[ "$*" == *"+%y%m%d"* ]]; then
  [[ "$*" == *-u* ]] || { echo "date stub: release day must be UTC (-u)" >&2; exit 97; }
  echo "$FAKE_TODAY"
else exec /bin/date "$@"; fi
STUB
chmod +x "$tmp/bin/gh" "$tmp/bin/date"
export PATH="$tmp/bin:$PATH" FAKE_TODAY="$today" FAKE_RELEASE_LOG="$tmp/release.log" FAKE_NOTES="$tmp/notes.md" FAKE_GH_LOG="$tmp/gh.log"

page() { printf '{"check_runs":[{"status":"completed","conclusion":"success"},{"status":"completed","conclusion":"%s"}]}' "$1"; }
runs_ok=$(page success)
status_none='{"total_count":0,"state":"pending"}'
# release_run <sha> <status> <conclusion> [<branch of the release repository>]: one run;
# release_runs, the same arguments: a listing of that run alone.
release_run() { printf '{"displayTitle":"Release refs/heads/main at %s","status":"%s","conclusion":"%s","headBranch":"%s","event":"workflow_dispatch"}' "$1" "$2" "$3" "${4:-main}"; }
release_runs() { printf '[%s]' "$(release_run "$@")"; }

# generate-notes body: 8 PRs, 2 by Dependabot, 1 by another bot, 1 breaking,
# 1 first-time contributor, 1 title carrying its issue number as "(#N)".
cat >"$tmp/notes-full.md" <<'EOF'
## What's Changed
* feat(access): admin-issued API tokens (#179) by @alice in https://github.com/o/r/pull/181
* fix(usage): narrow the table (#162) by @bob in https://github.com/o/r/pull/165
* feat(api)!: drop the legacy header by @alice in https://github.com/o/r/pull/190
* test: de-flake the capacity tests by @alice in https://github.com/o/r/pull/192
* ci: bump taiki-e/install-action by @dependabot[bot] in https://github.com/o/r/pull/172
* build(deps): bump jsonwebtoken by @dependabot[bot] in https://github.com/o/r/pull/176
* chore: tidy by @github-actions[bot] in https://github.com/o/r/pull/177
* update the readme by @alice in https://github.com/o/r/pull/178
## New Contributors
* @bob made their first contribution in https://github.com/o/r/pull/165

**Full Changelog**: https://github.com/o/r/compare/1.260101.0...next
EOF
cat >"$tmp/notes-bots.md" <<'EOF'
## What's Changed
* build(deps): bump jsonwebtoken by @dependabot[bot] in https://github.com/o/r/pull/176
EOF

# Fresh repo: c0 (tag 1.260101.0) <- c1 (tag nightly) <- c2 <- c3 = origin/main.
# The previous tag must be 1.260101.0: `nightly` and the rc/marker tags on c2 are no releases.
setup() {
  [ -n "${tmp:-}" ] && rm -rf "$tmp/remote" "$tmp/work" "$tmp/out"
  mkdir -p "$tmp/remote/o"
  git init -q --bare -b main "$tmp/remote/o/r.git"
  git init -q -b main "$tmp/work"
  cd "$tmp/work" || exit 2
  git remote add origin "$tmp/remote/o/r.git"
  mkdir -p backend/alembic/versions
  echo 'create table a();' >backend/alembic/versions/a1.py; echo 'v1' >UPGRADING.md
  git add -A; git_ commit -q -m c0; git tag 1.260101.0
  echo 1 >f; git add -A; git_ commit -q -m c1; git tag nightly
  echo 2 >f; echo 'alter table a;' >>backend/alembic/versions/a1.py; git add -A; git_ commit -q -m c2
  git tag 1.260901.0-rc.1; git tag 1.260901.x   # ancestors of the head, but not releases
  echo 3 >f
  git add -A; git_ commit -q -m c3
  git push -q origin main
  cp "$tmp/notes-full.md" "$FAKE_NOTES"
  rm -f "$FAKE_RELEASE_LOG" "$FAKE_GH_LOG"
  export FAKE_RUNS_JSON="$runs_ok" FAKE_STATUS_JSON="$status_none"
  FAKE_RELEASE_RUNS_JSON=$(release_runs "$(git rev-parse HEAD)" completed success); export FAKE_RELEASE_RUNS_JSON
}

OUT=""; RC=0
run() { OUT=$("$BASH" "$release" --out "$tmp/out" "$@" 2>&1); RC=$?; }
gen() { cat "$tmp/out/release-notes.generated.md"; }

echo "dry run: previous tag, next tag, notes"
setup; run --theme "A theme"
assert_eq "dry run exit" 0 "$RC"
assert_has "previous tag" "$OUT" "previous: 1.260101.0"
assert_has "next tag" "$OUT" "next:     1.$today.0"
assert_has "title" "$OUT" "title:    XTM One 1.$today.0 - A theme"
assert_has "dry run says so" "$OUT" "Dry run: nothing created."
assert_eq "nothing published" no "$([ -e "$FAKE_RELEASE_LOG" ] && echo yes || echo no)"
assert_has "migration review" "$OUT" "backend/alembic/versions/a1.py"
notes=$(gen)
assert_has "count" "$notes" "**8 changes** since 1.260101.0."
assert_has "scope bold, capitalised, issue number cited" "$notes" "* **access**: Admin-issued API tokens. (#179)"
assert_has "fix bullet" "$notes" "* **usage**: Narrow the table. (#162)"
assert_has "untyped title" "$notes" "* Update the readme. (#178)"
assert_lacks "PR number not cited when the title has an issue" "$notes" "(#181)"
assert_has "dependabot collapsed" "$notes" "* Update dependencies: 2 automated bump(s). (#172, #176)"
assert_has "upgrade instructions" "$notes" "### Upgrade instructions"
assert_has "breaking bullet" "$notes" "* **api**: Drop the legacy header. (#190)"
assert_has "UPGRADING.md unchanged" "$notes" "It has no new entries in this release. In short:"
assert_has "migrations bullet" "$notes" "* **Migrations** run automatically on startup."
assert_has "upgrade footer" "$notes" "- The Filigran team"
assert_has "contributors and first contribution" "$notes" "Thanks to @alice and @bob (first contribution) for this release."
assert_lacks "bots not thanked" "$notes" "github-actions"
assert_has "changelog link" "$notes" "[1.260101.0...1.$today.0](https://github.com/o/r/compare/1.260101.0...1.$today.0)"
features=$(grep -n '^### Features' <<<"$notes" | cut -d: -f1); fixes=$(grep -n '^### Fixes' <<<"$notes" | cut -d: -f1)
if [ -n "$features" ] && [ -n "$fixes" ] && [ "$features" -lt "$fixes" ]; then pass; else fail "Features before Fixes"; fi

echo "dependabot-only release: no Contributors section"
setup; cp "$tmp/notes-bots.md" "$FAKE_NOTES"; run
assert_lacks "no contributors" "$(gen)" "### Contributors"
assert_has "still one change" "$(gen)" "**1 change** since"

echo "next tag: SEQ from release-shaped tags only"
setup
git_ commit -q --allow-empty -m side
git tag "1.$today.0"; git tag "1.$today.1-lts.1"; git tag "1.$today.9-rc.1"; git tag "1.$today.x"
git reset -q --hard origin/main
run
assert_has "SEQ skips rc/marker tags, counts lts" "$OUT" "next:     1.$today.2"
assert_eq "exit" 0 "$RC"

echo "preflight fails closed"
setup; run --tag 1.260101.0
assert_eq "existing tag exit" 1 "$RC"; assert_has "existing tag" "$OUT" "[FAIL] tag 1.260101.0 already exists"
setup; run --tag not-a-tag
assert_eq "bad tag exit" 1 "$RC"; assert_has "bad tag" "$OUT" "[FAIL] tag not-a-tag does not match"
setup; git switch -q -c side; git_ commit -q --allow-empty -m off-main; run "$(git rev-parse HEAD)"
assert_eq "off-main exit" 1 "$RC"; assert_has "off-main" "$OUT" "[FAIL] commit is not an ancestor of origin/main"
setup; run "$(git rev-parse 1.260101.0)"
assert_eq "no commits exit" 1 "$RC"; assert_has "no commits" "$OUT" "[FAIL] no commits since"
setup; git tag 1.260201.0 HEAD; run "$(git rev-parse HEAD~1)"
assert_eq "older than latest exit" 1 "$RC"; assert_has "older than latest" "$OUT" "[FAIL] commit is not after the latest release tag 1.260201.0"
setup; run
assert_has "newer than latest" "$OUT" "[ok] commit is after the latest release tag 1.260101.0"
setup; run --tag 1.250101.0
assert_eq "tag older than latest exit" 1 "$RC"; assert_has "tag older than latest" "$OUT" "[FAIL] tag 1.250101.0 is not newer than 1.260101.0"
setup; git switch -q -c lts; git_ commit -q --allow-empty -m lts-fix; git tag 1.270101.0-lts.1; git switch -q main; run
assert_eq "lts tag on another branch does not block" 0 "$RC"
for c in cancelled timed_out failure; do
  setup; FAKE_RUNS_JSON=$(page "$c") run
  assert_eq "$c run exit" 1 "$RC"; assert_has "$c run" "$OUT" "[FAIL] 1 check run(s) failed or still running"
done
setup; FAKE_RUNS_JSON='{"check_runs":[{"status":"in_progress","conclusion":null}]}' run
assert_eq "in-progress run exit" 1 "$RC"; assert_has "in-progress run" "$OUT" "[FAIL] 1 check run(s)"
for c in skipped neutral; do
  setup; FAKE_RUNS_JSON=$(page "$c") run
  assert_eq "$c run exit" 0 "$RC"; assert_has "$c run" "$OUT" "[ok] all check runs passed"
done
setup; FAKE_RUNS_JSON=$runs_ok$'\n'$(page cancelled) run
assert_eq "failing run on page 2 exit" 1 "$RC"; assert_has "failing run on page 2" "$OUT" "[FAIL] 1 check run(s)"
setup; run
assert_has "no legacy statuses" "$OUT" "[ok] legacy commit status: none"
setup; FAKE_STATUS_JSON='{"total_count":2,"state":"pending"}' run
assert_eq "pending status exit" 1 "$RC"; assert_has "pending status" "$OUT" "[FAIL] legacy commit status is pending"
setup; FAKE_STATUS_JSON='{"total_count":1,"state":"failure"}' run
assert_eq "failed status exit" 1 "$RC"; assert_has "failed status" "$OUT" "[FAIL] legacy commit status is failure"
setup; FAKE_STATUS_JSON='{"total_count":1,"state":"success"}' run
assert_eq "green status exit" 0 "$RC"; assert_has "green status" "$OUT" "[ok] legacy commit status: success"

echo "publish"
setup; run --publish
assert_eq "no --yes exit" 2 "$RC"; assert_has "no --yes" "$OUT" "Refusing to publish without --yes."
assert_eq "no --yes publishes nothing" no "$([ -e "$FAKE_RELEASE_LOG" ] && echo yes || echo no)"
setup; FAKE_RUNS_JSON=$(page failure) run --publish --yes
assert_eq "failed preflight exit" 1 "$RC"; assert_has "failed preflight" "$OUT" "Refusing to publish: preflight failed."
assert_eq "failed preflight publishes nothing" no "$([ -e "$FAKE_RELEASE_LOG" ] && echo yes || echo no)"
setup; run --theme T --publish --yes --draft
assert_eq "publish exit" 0 "$RC"
log=$(cat "$FAKE_RELEASE_LOG")
assert_has "create" "$log" "create"; assert_has "tag" "$log" "1.$today.0"
assert_has "repo from origin" "$log" "o/r"; assert_has "draft" "$log" "--draft"
assert_has "title" "$log" "XTM One 1.$today.0 - T"
assert_has "notes file" "$log" "$tmp/out/release-notes.generated.md"

echo "UPGRADING.md changed: the no-new-entries note goes"
setup; echo v2 >UPGRADING.md; git add -A; git_ commit -q -m upgrading; git push -q origin main
FAKE_RELEASE_RUNS_JSON=$(release_runs "$(git rev-parse HEAD)" completed success) run
assert_eq "exit" 0 "$RC"
assert_has "link kept" "$(gen)" "Read [UPGRADING.md](https://github.com/o/r/blob/1.$today.0/UPGRADING.md) before upgrading. In short:"
assert_lacks "note dropped" "$(gen)" "It has no new entries"

echo "gh failures"
setup; FAKE_GH_FAIL=check-runs run
assert_eq "check-runs failure exit" 1 "$RC"; assert_has "check-runs failure" "$OUT" "[FAIL] could not read check runs"
setup; FAKE_GH_FAIL=generate-notes run
assert_eq "generate-notes failure exit" 2 "$RC"
assert_eq "generate-notes failure writes no notes" no "$([ -e "$tmp/out/release-notes.generated.md" ] && echo yes || echo no)"
setup; FAKE_GH_FAIL="release create" run --publish --yes
assert_eq "release create failure exit" 2 "$RC"
assert_lacks "no tag pushed message" "$OUT" "Tag pushed"

echo "release repository run"
setup; run
assert_has "main release passed" "$OUT" "[ok] main release passed in XTM-One-Platform/release"
setup; FAKE_RELEASE_RUNS_JSON=$(release_runs "$(git rev-parse HEAD)" completed failure) run
assert_eq "failed main release exit" 1 "$RC"; assert_has "failed main release" "$OUT" "[FAIL] main release in XTM-One-Platform/release: completed failure"
setup; FAKE_RELEASE_RUNS_JSON=$(release_runs "$(git rev-parse HEAD)" in_progress null) run
assert_eq "running main release exit" 1 "$RC"; assert_has "running main release" "$OUT" "in_progress"
setup; FAKE_RELEASE_RUNS_JSON=$(release_runs "$(git rev-parse HEAD~1)" completed success) run
assert_eq "other commit's release exit" 1 "$RC"; assert_has "no main release" "$OUT" "[FAIL] no main release of the commit"
assert_has "points at the released commit" "$OUT" "latest main commit released there: $(git rev-parse HEAD~1)"
setup; FAKE_RELEASE_RUNS_JSON=$(release_runs "$(git rev-parse HEAD)" completed success my-branch) run
assert_eq "run from another branch exit" 1 "$RC"; assert_has "run from another branch" "$OUT" "[FAIL] no main release of the commit"
assert_lacks "run from another branch not named" "$OUT" "latest main commit released there"
# Newest first: a later run of the released commit failed or was cancelled.
setup; head=$(git rev-parse HEAD)
FAKE_RELEASE_RUNS_JSON="[$(release_run "$head" completed failure),$(release_run "$head" completed cancelled),$(release_run "$head" completed success)]" run
assert_eq "released then failed exit" 0 "$RC"; assert_has "released then failed" "$OUT" "[ok] main release passed in XTM-One-Platform/release"
# The newest run of the commit is the one reported; the latest released commit is
# another's, and the listing is read once, 1,000 runs deep.
setup; head=$(git rev-parse HEAD); older=$(git rev-parse HEAD~1)
FAKE_RELEASE_RUNS_JSON="[$(release_run "$head" in_progress null),$(release_run "$head" completed failure),$(release_run "$older" completed success)]" run
assert_eq "no passed run exit" 1 "$RC"; assert_has "newest run reported" "$OUT" "[FAIL] main release in XTM-One-Platform/release: in_progress null"
assert_has "released commit named" "$OUT" "latest main commit released there: $older"
assert_eq "one listing" 1 "$(grep -c '^run list' "$FAKE_GH_LOG")"
assert_has "1,000 runs" "$(grep '^run list' "$FAKE_GH_LOG")" "--limit 1000"
setup; FAKE_GH_FAIL="run list" run
assert_eq "run list failure exit" 1 "$RC"; assert_has "run list failure" "$OUT" "[FAIL] could not read the releases"

echo "--notes-file"
setup; run --notes-file "$tmp/out/release-notes.generated.md"
assert_eq "generated file refused exit" 2 "$RC"
setup; run; printf '## Hand edited\n' >"$tmp/out/release-notes.md"
run --notes-file "$tmp/out/release-notes.md"
assert_has "prints the published file" "$OUT" "## Release notes ($tmp/out/release-notes.md)"
assert_has "prints its content" "$OUT" "## Hand edited"
assert_eq "edited copy survives a re-run" "## Hand edited" "$(cat "$tmp/out/release-notes.md")"
run --notes-file "$tmp/out/release-notes.md" --publish --yes
assert_has "publishes the edited file" "$(cat "$FAKE_RELEASE_LOG")" "$tmp/out/release-notes.md"

echo
echo "$passed passed, $failed failed (bash $BASH_VERSION)"
[ "$failed" -eq 0 ]
