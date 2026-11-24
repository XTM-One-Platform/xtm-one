#!/usr/bin/env bash
#
# Keeps every AGENTS.md small: the root one loads on every agent turn, a
# directory's one whenever an agent works there. They hold pointers and the
# rules of their scope; the detail lives in dev-docs/reference/. Guards them
# against drifting back into the catalogs they were split out of.
#
# Checks:
#   (a) Each file stays under its size budget - a sign a section is
#       re-accumulating detail that belongs in a dev-docs/reference/ file.
#   (b) No issue-number citations (#1234) outside the root file -
#       a sign an entry has turned into "what changed" instead of "what it
#       does today". The root is exempt: its commit-convention block shows
#       the "(#1234)" title suffix.
#   (c) Every dev-docs/ path the files name exists, and every doc they name
#       by its bare file name (`backend-services-quota.md`) is a tracked
#       file, so a moved or renamed doc doesn't leave a dead pointer.
#
# Usage: bash scripts/check-agents-md-hygiene.sh
#
# On a partial sandbox checkout (a .xtm-checkout record, or the legacy
# .xtm-checkout.json, whose sub_paths name
# the parts of the tree it holds), a dev-docs/ pointer outside those parts is
# not judged: the checkout lacks it, CI checks the whole tree.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

fail=0

scope=()
record=""
for name in .xtm-checkout .xtm-checkout.json; do
  if [[ -f "$name" ]]; then
    record="$name"
    break
  fi
done
if [[ -n "$record" ]] && command -v python3 > /dev/null; then
  while IFS= read -r sub; do
    [[ -n "$sub" ]] && scope+=("$sub")
  done < <(python3 -c 'import json,sys
try: d = json.load(open(sys.argv[1]))
except Exception: sys.exit(0)
for s in d.get("sub_paths") or []:
    if isinstance(s, str) and s.strip("/"): print(s.strip("/"))' "$record")
fi

# Whether the checkout holds $1: always on a whole tree.
held() {
  (( ${#scope[@]} == 0 )) && return 0
  local sub
  for sub in "${scope[@]}"; do
    [[ "$1" == "$sub" || "$1" == "$sub"/* ]] && return 0
  done
  return 1
}

budget() {
  case "$1" in
    AGENTS.md) echo 12000 ;;
    *) echo 6000 ;;
  esac
}

while IFS= read -r file; do
  max="$(budget "$file")"
  size="$(wc -c < "$file" | tr -d ' ')"
  if (( size > max )); then
    echo "FAIL: $file is $size bytes (budget $max). Move the detail to dev-docs/reference/ and leave a pointer."
    fail=1
  fi

  if [[ "$file" != "AGENTS.md" ]]; then
    hits="$(grep -nE '(^|[^#[:alnum:]&])#[0-9]{2,}([^[:alnum:]]|$)' "$file" || true)"
    if [[ -n "$hits" ]]; then
      echo "FAIL: $file cites issue numbers (describe current behavior, not history):"
      echo "$hits" | sed 's/^/  /'
      fail=1
    fi
  fi

  while IFS= read -r path; do
    [[ -z "$path" ]] && continue
    if [[ ! -e "$path" ]] && held "$path"; then
      echo "FAIL: $file points to $path, which does not exist."
      fail=1
    fi
  done < <(grep -oE 'dev-docs/[A-Za-z0-9_./-]+\.md' "$file" | sort -u)

  while IFS= read -r name; do
    [[ -z "$name" ]] && continue
    if [[ -z "$(git ls-files -- "$name" "*/$name")" ]]; then
      echo "FAIL: $file names $name, which is not a tracked file."
      fail=1
    fi
  done < <(grep -oE '`[A-Za-z0-9_.-]+\.md`' "$file" | tr -d '`' | sort -u)
done < <(git ls-files -- 'AGENTS.md' '*/AGENTS.md')

if [[ "$fail" -eq 0 ]]; then
  (( ${#scope[@]} > 0 )) && echo "Partial checkout (${scope[*]}): pointers outside it not judged; CI checks the whole tree."
  echo "AGENTS.md hygiene checks passed."
fi
exit "$fail"
