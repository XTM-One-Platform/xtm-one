#!/usr/bin/env bash
# Tests the `security` type/scope refusal: .githooks/commit-msg and
# pr-title-check.yml hold the same pattern, and the hook and the title check's own
# step script refuse and accept the same samples.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
hook="$root/.githooks/commit-msg"
workflow="$root/.github/workflows/pr-title-check.yml"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
failures=0

fail() { echo "FAIL $1"; failures=$((failures + 1)); }

hook_pattern="$(sed -n "s/^pattern='\(.*\)'$/\1/p" "$hook")"
title_pattern="$(sed -n "s/^ *SECURITY_SCOPE='\(.*\)'$/\1/p" "$workflow")"
[ -n "$hook_pattern" ] || fail "no pattern found in $hook"
[ -n "$title_pattern" ] || fail "no SECURITY_SCOPE found in $workflow"
[ "$hook_pattern" = "$title_pattern" ] || fail "the hook and the PR title check hold different patterns"

# The title check's step script, run as is (its `shopt`, its message).
step_script="$(awk '
  /^ +run: \|$/ { on = 1; next }
  !on { next }
  NF { match($0, /^ */); if (!ind) ind = RLENGTH; else if (RLENGTH < ind) exit }
  { print substr($0, ind + 1) }
' "$workflow")"
[ -n "$step_script" ] || fail "no run script found in $workflow"

hook_refuses() { printf '%s\n' "$1" > "$tmp/msg"; ! sh "$hook" "$tmp/msg" 2>/dev/null; }
title_refuses() {
  local out
  out="$(TITLE="$1" bash -c "$step_script" 2>&1)" && return 1
  [[ "$out" == *"'security' is not a type or a scope"* ]]
}

expect() { # expect refused|accepted <title>
  local want="$1" title="$2"
  for check in hook_refuses title_refuses; do
    if $check "$title"; then got=refused; else got=accepted; fi
    if [ "$got" = "$want" ]; then echo "ok   $check: $want: $title"; else fail "$check: $title is $got, expected $want"; fi
  done
}

expect refused "security: fix the export (#1)"
expect refused "security(api): fix the export (#1)"
expect refused "Security: fix the export (#1)"
expect refused "fix(security): restrict access (#1)"
expect refused "Fix(Security): restrict access (#1)"
expect refused "fix(security)!: restrict access (#1)"
expect refused "fix(api/security): restrict access (#1)"
expect refused "fix(auth,security): restrict access (#1)"
expect refused "fix(auth, security): restrict access (#1)"
expect refused "fix(security,auth): restrict access (#1)"
expect accepted "fix(security-audit): count dismissed findings (#1)"
expect accepted "fix(securityheaders): set the default (#1)"
expect accepted "feat(ctem): a published security advisory is a new signal (#1)"
expect accepted "fix(auth): harden the email change (#1)"
expect accepted "docs: describe the securityContext setting (#1)"

# The hook reads every line but the comments: a squash commit carries them all.
printf 'fix(auth): harden x\n\nfix(security): y\n' > "$tmp/msg"
if sh "$hook" "$tmp/msg" 2>/dev/null; then fail "hook: a body line with the scope is accepted"; else echo "ok   hook: body line refused"; fi
printf 'fix(auth): harden x\n# fix(security): y\n' > "$tmp/msg"
if sh "$hook" "$tmp/msg" 2>/dev/null; then echo "ok   hook: comment line ignored"; else fail "hook: a comment line is refused"; fi

[ "$failures" -eq 0 ] || { echo "$failures failure(s)"; exit 1; }
echo "all passed"
