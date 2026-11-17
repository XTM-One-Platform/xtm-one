#!/usr/bin/env bash
#
# Fail if an environment variable that is *newly added* to .env.sample is not
# documented in the XTM One docs (docs/docs/).
#
# Only variables added on this branch (relative to the merge-base with the
# target branch) are checked, so the large set of pre-existing, intentionally
# undocumented tuning knobs never trips the build. A genuinely internal new
# variable that must not appear in user docs can be listed in
# .github/env-doc-allowlist.txt.
#
# Usage:  BASE_REF=main bash scripts/check-new-env-vars-documented.sh
#         (BASE_REF is optional; without it the base is HEAD^ - in CI, the
#         first parent of GitHub's merge of the pull request, its base.)

set -euo pipefail

ENV_FILE=".env.sample"
DOCS_DIR="docs/docs"
ALLOWLIST=".github/env-doc-allowlist.txt"

# --- Extract the set of env var names from a .env file on stdin ---------------
# Matches both active (VAR=...) and commented (# VAR=...) declarations.
extract_vars() {
  grep -oE '^#? *[A-Z][A-Z0-9_]{2,}=' | sed -E 's/^#? *//; s/=$//' | sort -u
}

# --- Resolve the base revision to diff against --------------------------------
base_rev=""
if [[ -n "${BASE_REF:-}" ]]; then
  git fetch --quiet origin "$BASE_REF" 2>/dev/null || true
  base_rev="$(git merge-base HEAD "origin/$BASE_REF" 2>/dev/null || git rev-parse --verify -q "origin/$BASE_REF" 2>/dev/null || true)"
fi
if [[ -z "$base_rev" ]]; then
  base_rev="$(git rev-parse --verify -q HEAD^ 2>/dev/null || true)"
fi
if [[ -z "$base_rev" ]]; then
  echo "No base revision to compare against; nothing to check."
  exit 0
fi

# --- Compute newly added var names (current minus base) -----------------------
current_vars="$(extract_vars < "$ENV_FILE")"
base_vars="$(git show "$base_rev:$ENV_FILE" 2>/dev/null | extract_vars || true)"
new_vars="$(comm -23 <(printf '%s\n' "$current_vars") <(printf '%s\n' "$base_vars") | sed '/^$/d')"

if [[ -z "$new_vars" ]]; then
  echo "No new environment variables added to $ENV_FILE."
  exit 0
fi

# --- Load the allowlist of intentionally undocumented vars --------------------
allow=""
if [[ -f "$ALLOWLIST" ]]; then
  allow="$(grep -oE '^[A-Z][A-Z0-9_]+' "$ALLOWLIST" | sort -u || true)"
fi

# --- Verify each new var is documented ----------------------------------------
undocumented=()
while IFS= read -r var; do
  [[ -z "$var" ]] && continue
  if [[ -n "$allow" ]] && printf '%s\n' "$allow" | grep -qx "$var"; then
    continue
  fi
  if grep -rqw "$var" "$DOCS_DIR"; then
    continue
  fi
  undocumented+=("$var")
done <<< "$new_vars"

if [[ ${#undocumented[@]} -gt 0 ]]; then
  echo "::error::New environment variable(s) in $ENV_FILE are not documented under $DOCS_DIR/:"
  for v in "${undocumented[@]}"; do
    echo "  - $v"
  done
  cat <<MSG

Document each variable in the deployment docs (for example
docs/docs/deployment/configuration.md). If a variable is intentionally
internal and must not appear in the user-facing docs, add it to $ALLOWLIST.
MSG
  exit 1
fi

echo "All newly added environment variables are documented."
