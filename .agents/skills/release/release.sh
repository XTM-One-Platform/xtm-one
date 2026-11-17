#!/usr/bin/env bash
# Cut a release: resolve commit and tags, run preflight, build the release notes,
# optionally create the GitHub release. Dry run unless --publish --yes is given.
set -uo pipefail

usage() {
  cat <<'USAGE'
Usage: release.sh [<sha>] [options]

  <sha>              Commit to release (default: origin/main, after a fetch).
  --tag TAG          Override the computed tag.
  --theme TEXT       Title becomes "XTM One <tag> - <TEXT>".
  --notes-file FILE  Publish this body instead of the generated one.
  --out DIR          Where to write release-notes.generated.md.
  --publish          Create the release (needs --yes). Default is a dry run.
  --yes              Confirms --publish. Ask the user first.
  --draft            With --publish: create a draft (the tag, the image build and
                     the MCP registry publish wait until it is published in the UI).
Exit: 0 ok, 1 a preflight check failed, 2 bad input or environment.
USAGE
}

sha_arg=""; tag_override=""; theme=""; notes_in=""; out=""
publish=0; yes=0; draft=0
while [ $# -gt 0 ]; do
  case "$1" in
    --tag)        tag_override="${2:?--tag needs a value}"; shift 2 ;;
    --theme)      theme="${2:?--theme needs a value}"; shift 2 ;;
    --notes-file) notes_in="${2:?--notes-file needs a value}"; shift 2 ;;
    --out)        out="${2:?--out needs a value}"; shift 2 ;;
    --publish)    publish=1; shift ;;
    --yes)        yes=1; shift ;;
    --draft)      draft=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    -*)           echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)            sha_arg="$1"; shift ;;
  esac
done
[ -z "$notes_in" ] || [ -f "$notes_in" ] || { echo "no such file: $notes_in" >&2; exit 2; }

# Same values as scripts/generate-release-notes.sh.
DOCKERHUB_URL="https://hub.docker.com/u/xtmone"
DOCS_URL="https://docs.xtmone.io"
RELEASE_RE='^[0-9]+\.[0-9]{6}\.[0-9]+(-lts\.[0-9]+)?$'

fail=0
check() { echo "[$1] $2"; [ "$1" = ok ] || fail=1; }

# From origin, not the cwd's gh default: in a fork clone that would target the fork.
repo=$(git remote get-url origin | sed -E 's#(\.git)?/?$##; s#.*[:/]([^/:]+/[^/]+)$#\1#')
[ -n "$repo" ] || { echo "cannot derive owner/repo from origin" >&2; exit 2; }
git fetch --quiet --force origin main --tags || { echo "git fetch failed" >&2; exit 2; }

target=$(git rev-parse --verify --quiet "${sha_arg:-origin/main}^{commit}") \
  || { echo "cannot resolve commit: ${sha_arg:-origin/main}" >&2; exit 2; }
# Nearest release-shaped ancestor tag. The glob also matches 1.261012.0-rc.1 and the
# like, so skip any candidate that is not release-shaped and ask again without it.
prev=""; excl=()
while cand=$(git describe --tags --abbrev=0 --match '[0-9]*.[0-9][0-9][0-9][0-9][0-9][0-9].*' \
    ${excl[@]+"${excl[@]}"} "$target" 2>/dev/null); do
  if [[ "$cand" =~ $RELEASE_RE ]]; then prev=$cand; break; fi
  excl+=(--exclude "$cand")
done
[ -n "$prev" ] || { echo "no previous release tag found before $target" >&2; exit 2; }

if [ -n "$tag_override" ]; then
  next="$tag_override"
else
  major=${prev%%.*}; today=$(date -u +%y%m%d)
  # Release-shaped tags only: a same-day -rc or marker tag would break the arithmetic.
  last=$(git tag -l "$major.$today.*" | grep -E "$RELEASE_RE" | sed -E 's/^[0-9]+\.[0-9]{6}\.([0-9]+)(-lts\.[0-9]+)?$/\1/' | sort -n | tail -1)
  next="$major.$today.$(( ${last:--1} + 1 ))"
fi
title="XTM One $next${theme:+ - $theme}"

echo "repo:     $repo"
echo "commit:   $target ($(git log -1 --format=%s "$target"))"
echo "previous: $prev"
echo "next:     $next"
[[ "$prev" != *-lts.* ]] || echo "warning:  previous tag $prev is an LTS tag; check the change list"
echo "title:    $title"
echo
echo "## Preflight"
if git merge-base --is-ancestor "$target" origin/main; then
  check ok "commit is on origin/main"
else check FAIL "commit is not an ancestor of origin/main"; fi
if [[ "$next" =~ $RELEASE_RE ]]; then
  check ok "tag matches MAJOR.YYMMDD.SEQ"
else check FAIL "tag $next does not match MAJOR.YYMMDD.SEQ"; fi
if git rev-parse --verify --quiet "refs/tags/$next" >/dev/null; then
  check FAIL "tag $next already exists"
else check ok "tag $next is free"; fi
count=$(git rev-list --count "$prev..$target")
if [ "$count" -gt 0 ]; then
  check ok "$count commits since $prev"
else check FAIL "no commits since $prev"; fi
# A release must come after every earlier one: tagging an older commit would publish it
# as the newest version, and the release would move `latest` back to it. Tags merged into
# origin/main only, and no LTS tags: a maintenance branch must not block main.
latest=$(git tag --merged origin/main -l "${prev%%.*}.*" | grep -E "$RELEASE_RE" | grep -v -- '-lts\.' | sort -V | tail -1)
if [ -n "$latest" ]; then
  if [ "$(git rev-list -n 1 "$latest")" = "$target" ] || ! git merge-base --is-ancestor "$latest" "$target"; then
    check FAIL "commit is not after the latest release tag $latest"
  else check ok "commit is after the latest release tag $latest"; fi
  if [[ "$next" =~ $RELEASE_RE ]]; then
    if [ "$next" != "$latest" ] && [ "$(printf '%s\n%s\n' "$latest" "$next" | sort -V | tail -1)" = "$next" ]; then
      check ok "tag $next is newer than $latest"
    else check FAIL "tag $next is not newer than $latest"; fi
  fi
fi

# Fail closed: anything not green blocks, including a `cancelled` run superseded by
# concurrency (re-run it). --paginate prints one count per page; sum them.
runs=$(gh api --paginate "repos/$repo/commits/$target/check-runs?per_page=100" \
  --jq '[.check_runs[] | select(.status != "completed" or (.conclusion | IN("success","skipped","neutral") | not))] | length' \
  | awk '{n += $1} END {print n + 0}') || runs=""
if [ -z "$runs" ]; then check FAIL "could not read check runs for the commit"
elif [ "$runs" = 0 ]; then check ok "all check runs passed"
else check FAIL "$runs check run(s) failed or still running"; fi
# Legacy commit statuses (not check runs). "none" when the commit has no statuses.
status=$(gh api "repos/$repo/commits/$target/status" \
  --jq 'if .total_count == 0 then "none" else .state end') || status=""
case "$status" in
  none|success) check ok "legacy commit status: $status" ;;
  "") check FAIL "could not read the commit status" ;;
  *) check FAIL "legacy commit status is $status" ;;
esac
# deploy.yml only dispatches the build, signing and deploy to XTM-One-Platform/release:
# its check runs pass at the dispatch. The main release of the commit must have passed there.
# Only dispatched runs of that repository's main count: anyone who can push a branch there
# can run release.yml from it under any run name. Read once, newest first, one
# "<sha> <status> <conclusion>" line per run: feature branch deploys share the listing,
# hence the 1,000 runs.
releases=$(gh run list --repo XTM-One-Platform/release --workflow release.yml --limit 1000 \
  --json displayTitle,status,conclusion,headBranch,event \
  --jq '.[] | select(.headBranch == "main" and .event == "workflow_dispatch" and (.displayTitle | startswith("Release refs/heads/main at "))) | "\(.displayTitle | ltrimstr("Release refs/heads/main at ")) \(.status) \(.conclusion)"') || releases="error"
if [ "$releases" = error ]; then check FAIL "could not read the releases of XTM-One-Platform/release"
# Any run that passed: a later run of the same commit that failed or was cancelled does
# not undo its release.
elif grep -qx "$target completed success" <<<"$releases"; then check ok "main release passed in XTM-One-Platform/release"
else
  release_run=$(grep -m 1 "^$target " <<<"$releases" | cut -d ' ' -f 2-)
  if [ -z "$release_run" ]; then check FAIL "no main release of the commit in XTM-One-Platform/release"
  else check FAIL "main release in XTM-One-Platform/release: $release_run"; fi
  # A run a newer push replaced ends cancelled, and main never goes back: it cannot
  # be re-run. Point at the commit to tag instead.
  green=$(awk '$2 == "completed" && $3 == "success" { print $1; exit }' <<<"$releases")
  [ -z "$green" ] || echo "       latest main commit released there: $green"
fi

# --- notes -----------------------------------------------------------------
generated=$(gh api "repos/$repo/releases/generate-notes" -f tag_name="$next" \
  -f previous_tag_name="$prev" -f target_commitish="$target" --jq .body) || exit 2

# bash 3.2 (macOS) has no associative arrays: one items_<id> variable per section.
declare -a deps=() breaking=() authors=() firsts=()
SECTIONS="features fixes improvements tests docs other deps"
for id in $SECTIONS; do printf -v "items_$id" '%s' ""; done
section_for() { # conventional type -> section id
  case "$1" in
    feat) echo features ;; fix) echo fixes ;; perf|refactor) echo improvements ;;
    test|ci) echo tests ;; docs) echo docs ;; *) echo other ;;
  esac
}
heading_for() {
  case "$1" in
    features) echo Features ;; fixes) echo Fixes ;; improvements) echo Improvements ;;
    tests) echo "Tests and CI" ;; docs) echo Documentation ;; deps) echo Dependencies ;;
    *) echo "Other changes" ;;
  esac
}
add_item() { local v="items_$1"; printf -v "$v" '%s%s\n' "${!v}" "$2"; }
npr=0
re_pr='^\* (.+) by @([^ ]+) in https://github\.com/[^/]+/[^/]+/pull/([0-9]+)$'
re_cc='^([a-z]+)(\(([^)]*)\))?(!)?: (.+)$'
re_ref='^(.*) \(#([0-9]+)\)$'
re_first='^\* @([^ ]+) made their first contribution'
while IFS= read -r line; do
  if [[ "$line" =~ $re_first ]]; then firsts+=("${BASH_REMATCH[1]}"); continue; fi
  [[ "$line" =~ $re_pr ]] || continue
  subject=${BASH_REMATCH[1]}; author=${BASH_REMATCH[2]}; pr=${BASH_REMATCH[3]}; npr=$((npr+1))
  if [ "$author" = "dependabot[bot]" ]; then deps+=("#$pr"); continue; fi
  case "$author" in *"[bot]") ;; *)
    case " ${authors[*]:-} " in *" $author "*) ;; *) authors+=("$author") ;; esac ;;
  esac
  type=""; scope=""; bang=""; text=$subject
  if [[ "$subject" =~ $re_cc ]]; then
    type=${BASH_REMATCH[1]}; scope=${BASH_REMATCH[3]}; bang=${BASH_REMATCH[4]}; text=${BASH_REMATCH[5]}
  fi
  # PR titles end in the issue (or PR) number: (#5766). Cite it, as earlier releases do; else the PR.
  ref=$pr
  if [[ "$text" =~ $re_ref ]]; then text=${BASH_REMATCH[1]}; ref=${BASH_REMATCH[2]}; fi
  text="$(tr '[:lower:]' '[:upper:]' <<<"${text:0:1}")${text:1}"
  case "$text" in *")") dot="" ;; *) dot="." ;; esac
  bullet="* ${scope:+**$scope**: }${text}${dot} (#$ref)"
  add_item "$(section_for "$type")" "$bullet"
  if [ -n "$bang" ] || [[ "$subject" == *BREAKING* ]]; then breaking+=("$bullet"); fi
done <<<"$generated"
if [ ${#deps[@]} -gt 0 ]; then
  list=$(IFS=,; echo "${deps[*]}"); list=${list//,/, }
  add_item deps "* Update dependencies: ${#deps[@]} automated bump(s). ($list)"
fi

[ -n "$out" ] || out=$(mktemp -d); mkdir -p "$out" || exit 2
body="$out/release-notes.generated.md"
# Never regenerate over the file that will be published.
if [ -n "$notes_in" ] && [ -e "$body" ] && [ "$notes_in" -ef "$body" ]; then
  echo "--notes-file must not be $body (it is overwritten on every run); copy and edit it" >&2; exit 2
fi
if git diff --quiet "$prev" "$target" -- UPGRADING.md; then
  upgrading_note=" It has no new entries in this release."
else upgrading_note=""; fi
{
  echo "## XTM One $next"; echo
  echo "**$npr change$([ "$npr" -eq 1 ] || echo s)** since $prev."; echo; echo "---"; echo
  for id in $SECTIONS; do
    v="items_$id"; [ -n "${!v}" ] || continue
    echo "### $(heading_for "$id")"; echo; printf '%s' "${!v}"; echo
  done
  echo "### Upgrade instructions"; echo
  echo "Read [UPGRADING.md](https://github.com/$repo/blob/$next/UPGRADING.md) before upgrading.$upgrading_note In short:"; echo
  if [ ${#breaking[@]} -gt 0 ]; then printf '%s\n' "${breaking[@]}"; fi
  echo "* **Migrations** run automatically on startup. No manual step is needed."; echo
  if [ ${#authors[@]} -gt 0 ]; then
    names=""; n=${#authors[@]}
    for i in "${!authors[@]}"; do
      a="@${authors[$i]}"; case " ${firsts[*]:-} " in *" ${authors[$i]} "*) a="$a (first contribution)" ;; esac
      if [ "$i" -eq 0 ]; then names=$a; elif [ "$i" -eq $((n-1)) ]; then names="$names and $a"; else names="$names, $a"; fi
    done
    echo "### Contributors"; echo; echo "Thanks to $names for this release."; echo
  fi
  echo "---"; echo
  echo "### Upgrade"; echo
  echo "Images for \`$next\` are published on [Docker Hub]($DOCKERHUB_URL). Pull the new tag and restart - migrations run automatically on startup."; echo
  echo "Deployment guide: [docs.xtmone.io]($DOCS_URL)"; echo
  echo "**Full changelog:** [$prev...$next](https://github.com/$repo/compare/$prev...$next)"; echo
  echo "- The Filigran team"
} > "$body"
publish_body=${notes_in:-$body}

echo
echo "## Review before publishing (not in the notes unless you add them)"
echo "alembic migrations since $prev (A added, M changed, D removed, R renamed; read each one: an added one is"
echo "breaking only if it needs a manual step or locks a table):"
git diff --name-status "$prev" "$target" -- backend/alembic/versions | sed 's/^/  /'
echo "UPGRADING.md:"; git diff --stat "$prev" "$target" -- UPGRADING.md | sed 's/^/  /'
echo "env/deploy diff:"; git diff --stat "$prev" "$target" -- .env.sample docker-compose.yml k8s | sed 's/^/  /'

cmd=(gh release create "$next" --repo "$repo" --target "$target" --title "$title" --notes-file "$publish_body")
[ "$draft" -eq 0 ] || cmd+=(--draft)
echo
echo "## Release notes ($publish_body)"; echo; cat "$publish_body"
echo
echo "## Command"; printf '%q ' "${cmd[@]}"; echo

if [ "$publish" -eq 0 ]; then echo; echo "Dry run: nothing created."; exit "$fail"; fi
[ "$fail" -eq 0 ] || { echo "Refusing to publish: preflight failed." >&2; exit 1; }
[ "$yes" -eq 1 ] || { echo "Refusing to publish without --yes." >&2; exit 2; }
"${cmd[@]}" || exit 2
[ "$draft" -eq 1 ] || echo "Tag pushed: gh run list --workflow deploy.yml --limit 1 (checks, then images in XTM-One-Platform/release), --workflow mcp-registry.yml --limit 1"
