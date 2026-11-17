#!/usr/bin/env bash
set -euo pipefail

# Generate GitHub Release notes for a version from the git commit history,
# following the established XTM One release template.
#
# The notes are authored by an LLM (GitHub Copilot CLI, `copilot -p`, using
# your local Copilot auth). A deterministic scaffold is always built from the
# commits and handed to the LLM as a structural anchor; it also serves as the
# automatic fallback when the Copilot CLI is unavailable or returns off-template
# output. The final Markdown is printed to stdout; progress goes to stderr.
#
# Usage:
#   scripts/generate-release-notes.sh --version V --range RANGE [options]
#
# Required:
#   --version V      Release version, e.g. 1.260731.0
#   --range RANGE    Git revision range for the commits, e.g. 1.260729.0..HEAD
#                    (or a single commit-ish for the very first release).
#
# Options:
#   --prev TAG       Previous release tag (drives the "since" text and the
#                    compare link). Omit for the first release.
#   --repo OWNER/REPO  GitHub repository (default: XTM-One-Platform/xtm-one).
#   --dockerhub URL  Docker Hub URL for the Upgrade block.
#   --docs URL       Documentation URL for the Upgrade block.
#   --model NAME     Copilot CLI model (default: Copilot's default).
#   -h, --help       Show this help.

REPO="XTM-One-Platform/xtm-one"
DOCS_URL="https://docs.xtmone.io/"
DOCKERHUB_URL="https://hub.docker.com/u/xtmone"
VERSION=""
PREV_TAG=""
RANGE=""
MODEL=""
CHANGE_COUNT_ARG=""

usage() {
  cat <<'EOF'
Generate GitHub Release notes for a version from the git commit history,
following the established XTM One release template.

Usage:
  scripts/generate-release-notes.sh --version V --range RANGE [options]

Required:
  --version V        Release version, e.g. 1.260731.0
  --range RANGE      Git revision range for the commits, e.g. 1.260729.0..HEAD
                     (or a single commit-ish for the very first release).

Options:
  --prev TAG         Previous release tag (drives the "since" text and the
                     compare link). Omit for the first release.
  --repo OWNER/REPO  GitHub repository (default: XTM-One-Platform/xtm-one).
  --dockerhub URL    Docker Hub URL for the Upgrade block.
  --docs URL         Documentation URL for the Upgrade block.
  --change-count N   Override the change count (default: number of commits in
                     RANGE). Lets the caller keep one authoritative count.
  --model NAME       Copilot CLI model (default: Copilot's default).
  -h, --help         Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)      VERSION="${2:?Missing value for --version}"; shift 2 ;;
    --range)        RANGE="${2:?Missing value for --range}"; shift 2 ;;
    --prev)         PREV_TAG="${2:?Missing value for --prev}"; shift 2 ;;
    --repo)         REPO="${2:?Missing value for --repo}"; shift 2 ;;
    --dockerhub)    DOCKERHUB_URL="${2:?Missing value for --dockerhub}"; shift 2 ;;
    --docs)         DOCS_URL="${2:?Missing value for --docs}"; shift 2 ;;
    --change-count) CHANGE_COUNT_ARG="${2:?Missing value for --change-count}"; shift 2 ;;
    --model)        MODEL="${2:?Missing value for --model}"; shift 2 ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "$VERSION" || -z "$RANGE" ]]; then
  echo "Both --version and --range are required." >&2
  usage >&2
  exit 1
fi

PREV_LABEL="${PREV_TAG:-the previous release}"

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

# Capitalize the first character of a string (bash 3.2 compatible).
capitalize() {
  local s="$1"
  [[ -z "$s" ]] && return 0
  printf '%s%s' \
    "$(printf '%s' "${s:0:1}" | tr '[:lower:]' '[:upper:]')" \
    "${s:1}"
}

# Pretty-print a conventional-commit scope as a section header.
pretty_scope() {
  local s="$1"
  case "$s" in
    llm) echo "LLM"; return ;;
    ai) echo "AI"; return ;;
    ci) echo "CI"; return ;;
    api) echo "API"; return ;;
    ui) echo "UI"; return ;;
    sso) echo "SSO"; return ;;
    mcp) echo "MCP"; return ;;
    kb) echo "KB"; return ;;
    hitl) echo "HITL"; return ;;
    opencti) echo "OpenCTI"; return ;;
    openaev) echo "OpenAEV"; return ;;
    github) echo "GitHub"; return ;;
    deps) echo "Dependencies"; return ;;
  esac
  # Title-case words, splitting on - and _.
  local out="" word
  for word in ${s//[-_]/ }; do
    out+="$(capitalize "$word") "
  done
  echo "${out% }"
}

# Fallback section header derived from the commit type (no scope).
type_header() {
  case "$1" in
    feat) echo "Features" ;;
    fix) echo "Bug Fixes" ;;
    perf) echo "Performance" ;;
    refactor) echo "Refactoring" ;;
    docs) echo "Documentation" ;;
    test) echo "Tests" ;;
    build) echo "Build" ;;
    ci) echo "CI" ;;
    chore) echo "Chores" ;;
    revert) echo "Reverts" ;;
    style) echo "Style" ;;
    *) echo "Other" ;;
  esac
}

contains() { local n="$1"; shift; local e; for e in "$@"; do [[ "$e" == "$n" ]] && return 0; done; return 1; }

# ---------------------------------------------------------------------------
# Parse commits into parallel arrays.
# ---------------------------------------------------------------------------
declare -a C_HEADER=()   # parallel: section header per commit
declare -a C_BULLET=()   # parallel: rendered bullet per commit
declare -a SECTION_ORDER=()
HIGHLIGHTS=""
CHANGE_COUNT=0

while IFS= read -r subject; do
  [[ -z "$subject" ]] && continue
  CHANGE_COUNT=$((CHANGE_COUNT + 1))

  type="other"; scope=""; desc="$subject"
  cc_re='^([a-zA-Z]+)(\(([^)]+)\))?(!)?:[[:space:]]+(.+)$'
  if [[ "$subject" =~ $cc_re ]]; then
    type="${BASH_REMATCH[1]}"
    scope="${BASH_REMATCH[3]}"
    desc="${BASH_REMATCH[5]}"
  fi

  # Collect issue/PR refs, then strip the trailing "(#NNN)" groups.
  refs="$(printf '%s' "$subject" | grep -oE '#[0-9]+' | paste -sd',' - | sed 's/,/, /g' || true)"
  clean="$(printf '%s' "$desc" | sed -E 's/[[:space:]]*\(#[0-9]+\)//g')"
  clean="${clean%"${clean##*[![:space:]]}"}"   # rstrip
  clean="$(capitalize "$clean")"                # capitalize first char
  [[ "$clean" =~ [.!?]$ ]] || clean+="."

  bullet="- ${clean}"
  [[ -n "$refs" ]] && bullet+=" (${refs})"

  if [[ -n "$scope" ]]; then
    header="$(pretty_scope "$scope")"
  else
    header="$(type_header "$type")"
  fi
  C_HEADER+=("$header")
  C_BULLET+=("$bullet")
  contains "$header" "${SECTION_ORDER[@]:-}" || SECTION_ORDER+=("$header")

  if [[ "$type" == "feat" ]]; then
    htitle="$(pretty_scope "${scope:-}")"
    [[ -z "$htitle" ]] && htitle="Feature"
    hl="**${htitle}** - ${clean}"
    [[ -n "$refs" ]] && hl+=" (${refs})"
    HIGHLIGHTS+="${hl}"$'\n\n'
  fi
done < <(git log --no-merges --reverse --pretty=format:'%s' "$RANGE"; printf '\n')

# The caller (release.sh) may hand us the authoritative change count so its plan
# and these notes can never disagree; otherwise use the count parsed above.
[[ -n "$CHANGE_COUNT_ARG" ]] && CHANGE_COUNT="$CHANGE_COUNT_ARG"

# Emit all bullets belonging to a given section header, in commit order.
print_section_bullets() {
  local header="$1" i
  for ((i = 0; i < ${#C_HEADER[@]}; i++)); do
    [[ "${C_HEADER[$i]}" == "$header" ]] && printf '%s\n' "${C_BULLET[$i]}"
  done
}

# Order sections: primary types first, domain scopes next (alpha), low-signal
# sections last.
PRIMARY=("Features" "Bug Fixes" "Performance" "Refactoring")
LOW=("Documentation" "Tests" "Build" "CI" "Chores" "Dependencies" "Reverts" "Style" "Other")

ORDERED=()
for h in "${PRIMARY[@]}"; do
  contains "$h" "${SECTION_ORDER[@]:-}" && ORDERED+=("$h")
done
# Middle: scope/domain headers (present, not primary, not low), alphabetical.
MIDDLE=()
for h in "${SECTION_ORDER[@]:-}"; do
  [[ -z "$h" ]] && continue
  contains "$h" "${PRIMARY[@]}" && continue
  contains "$h" "${LOW[@]}" && continue
  MIDDLE+=("$h")
done
if [[ ${#MIDDLE[@]} -gt 0 ]]; then
  while IFS= read -r h; do ORDERED+=("$h"); done \
    < <(printf '%s\n' "${MIDDLE[@]}" | sort)
fi
for h in "${LOW[@]}"; do
  contains "$h" "${SECTION_ORDER[@]:-}" && ORDERED+=("$h")
done

# ---------------------------------------------------------------------------
# Renderers.
# ---------------------------------------------------------------------------

# Fixed Upgrade + changelog + sign-off block, reproduced verbatim in every
# release (the LLM is told not to alter it).
render_footer() {
  printf '### Upgrade\n\n'
  printf 'Images for `%s` are published on [Docker Hub](%s). ' "$VERSION" "$DOCKERHUB_URL"
  printf 'Pull the new tag and restart - migrations run automatically on startup. '
  printf 'Deployment guide: [docs.xtmone.io](%s).\n\n' "$DOCS_URL"
  if [[ -n "$PREV_TAG" ]]; then
    printf '**Full changelog**: [%s...%s](https://github.com/%s/compare/%s...%s)\n\n' \
      "$PREV_TAG" "$VERSION" "$REPO" "$PREV_TAG" "$VERSION"
  fi
  printf -- '- The Filigran team\n'
}

# Deterministic notes built purely from the parsed commits. Serves as the LLM
# structural anchor and the automatic fallback.
render_notes_deterministic() {
  printf '## XTM One %s\n\n' "$VERSION"
  printf '<!-- Summarize the release highlights in 1-2 sentences. --> '
  printf '**%s changes** since %s.\n\n' "$CHANGE_COUNT" "$PREV_LABEL"
  printf -- '---\n\n'

  printf '### Highlights\n\n'
  if [[ -n "$HIGHLIGHTS" ]]; then
    printf '%s' "$HIGHLIGHTS"
  else
    printf '<!-- No feat commits detected; add highlights manually. -->\n\n'
  fi
  printf -- '---\n\n'

  for h in "${ORDERED[@]}"; do
    printf '### %s\n\n' "$h"
    print_section_bullets "$h"
    printf '\n'
  done
  printf -- '---\n\n'

  render_footer
}

# LLM-authored notes via the GitHub Copilot CLI (uses local Copilot auth).
# Falls back to the deterministic notes if copilot is missing or misbehaves.
render_notes_llm() {
  if ! command -v copilot >/dev/null 2>&1; then
    echo "copilot CLI not found; falling back to deterministic notes." >&2
    render_notes_deterministic
    return
  fi

  local commits footer scaffold promptfile out
  commits="$(git log --no-merges --reverse --pretty=format:'- %s' "$RANGE")"
  footer="$(render_footer)"
  scaffold="$(render_notes_deterministic)"

  promptfile="$(mktemp)"
  cat > "$promptfile" <<EOF
You are writing the GitHub Release notes for XTM One ${VERSION}.
Output ONLY the final Markdown. No preamble, no explanations, no code fences.

Follow this exact template and house style:

1. First line: "## XTM One ${VERSION}"
2. Blank line, then a 1-2 sentence plain-English summary of the most important
   changes in this release, ending with the literal text:
   "**${CHANGE_COUNT} changes** since ${PREV_LABEL}."
   Do not change that change count or the comparison target.
3. A line with only "---".
4. "### Highlights" followed by 2 to 5 entries, each on its own line formatted
   as: **Short Title** - one clear sentence. (#refs)
   Cover only the most user-facing or important changes (features first).
   Keep the issue/PR references (the #numbers) exactly.
5. A line with only "---".
6. One "### Section" heading per logical area, each followed by "- sentence. (#refs)"
   bullets. Group by domain (e.g. Integrations, Assignments, LLM, Observability,
   Server and platform, Documentation). For a specific integration or component,
   you may prefix a bullet with its name in bold, e.g. "- **OpenAEV**: ...".
   Every commit below must appear exactly once across the sections. Preserve
   every (#refs). Rewrite terse commit subjects into clear, user-facing English.
7. A line with only "---".
8. Reproduce this Upgrade/footer block VERBATIM as the final part, unchanged:

${footer}

Conventional-commit subjects since ${PREV_LABEL} (oldest first):
${commits}

For reference only, here is a deterministic scaffold with the correct structure,
change count, refs, and footer. Improve the wording, write the summary and
highlights, and refine the section grouping - but keep all facts, all (#refs),
the change count, and the footer intact:

${scaffold}
EOF

  echo "Generating release notes with Copilot CLI..." >&2
  out="$(copilot -p "$(cat "$promptfile")" --no-color --log-level none ${MODEL:+--model "$MODEL"} 2>/dev/null || true)"
  rm -f "$promptfile"
  # Strip any accidental code-fence wrapper.
  out="$(printf '%s\n' "$out" | sed -E '/^```/d')"

  if [[ "$out" != *"## XTM One ${VERSION}"* ]]; then
    echo "Copilot output was empty or off-template; using deterministic notes." >&2
    render_notes_deterministic
    return
  fi
  printf '%s\n' "$out"
}

render_notes_llm
