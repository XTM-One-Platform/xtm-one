#!/usr/bin/env bash
#
# verify_migration_squash.sh — local safety manual + CI harness for the Alembic
# squash.
#
# Strategy: the squash assumes the FULLY-UPGRADED CONTRACT — every deployment is
# on the last pre-squash release (stamped at the single PRE_SQUASH_FINAL_HEAD)
# before it installs the squashed version.  So there is no legacy revision
# matrix: the only bridge case is "a DB stamped at PRE_SQUASH_FINAL_HEAD heals to
# the baseline with zero schema/row change".
#
# Design goals:
#   * prove fresh installs work from the squashed baseline;
#   * prove the resulting schema matches models and, optionally, a real oracle DB;
#   * prove disaster recovery by stamp is metadata-only (schema + row counts stable);
#   * prove a DB at the last pre-squash head auto-heals to the baseline.
#
# Usage, from anywhere in the repo:
#   backend/scripts/verify_migration_squash.sh            # run everything (CI mode)
#   backend/scripts/verify_migration_squash.sh <step>     # run ONE step by hand
#   backend/scripts/verify_migration_squash.sh cleanup    # drop the throwaway DBs
#
# Steps (run in this order when going by hand):
#   preflight          working tree has a single squashed baseline + env sanity
#                      + PRE_SQUASH_FINAL_HEAD still equals ORACLE_REF's head
#   fresh-boot         fresh empty DB boots from the squashed baseline
#   model-drift        fresh DB schema matches the models (no drift)    [needs fresh-boot]
#   oracle-diff        fresh baseline schema == real historical DB      [needs fresh-boot]
#                      (FATAL if the oracle cannot be built/reached — this is the
#                      strongest schema check and must not silently degrade)
#   disaster-recovery  manual `stamp --purge` recovery is metadata-only
#   bridge-heal        a DB at the last pre-squash head auto-heals to baseline
#
# Hand-run steps KEEP their databases so the next step can reuse them; run the
# `cleanup` command (or leave KEEP_DBS unset in a full run) to drop them.  The
# full step-by-step runbook lives in dev-docs/migration-squash-verification.md.
#
# Config:
#   PGHOST=localhost PGPORT=5432 PGUSER=copilot PGPASSWORD=copilot_dev
#   PGADMINDB=copilot
#   ORACLE_DB=              # OPTIONAL explicit oracle (e.g. a restored prod snapshot).
#                          # When UNSET (the default) the oracle is built automatically
#                          # from ORACLE_REF — never from your local dev DB.
#   ORACLE_REF=origin/main  # git ref whose fresh-install schema the baseline must match
#   KEEP_DBS=0              # (all mode) set 1 to leave throwaway DBs for inspection
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$BACKEND_DIR/.." && pwd)"
FP="$BACKEND_DIR/scripts/schema_fingerprint.py"

if [[ -x "$BACKEND_DIR/.venv/bin/python" ]]; then
  VENV_PY="$BACKEND_DIR/.venv/bin/python"
else
  VENV_PY="$(command -v python3 || command -v python)"
fi

export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-5432}"
export PGUSER="${PGUSER:-copilot}"
export PGPASSWORD="${PGPASSWORD:-copilot_dev}"
export PGADMINDB="${PGADMINDB:-copilot}"
export SECRET_KEY="${SECRET_KEY:-verify-squash-secret}"
# The harness only exercises schema/DDL, so app feature flags are irrelevant here.
# Default them to empty so a stale local .env (e.g. an unknown ENABLED_FEATURES
# flag) cannot block Settings from loading; override by exporting it yourself.
export ENABLED_FEATURES="${ENABLED_FEATURES:-}"
# The oracle is built automatically from ORACLE_REF (origin/main) unless the
# operator supplies an explicit ORACLE_DB (e.g. a restored production snapshot).
# Defaulting to a local dev DB is deliberately NOT done: dev DBs drift from
# migration-file churn and produce false diffs.
ORACLE_DB="${ORACLE_DB-}"
ORACLE_REF="${ORACLE_REF:-origin/main}"
KEEP_DBS="${KEEP_DBS:-0}"

DB_NEW="sqsh_verify_new"
DB_ORACLE="sqsh_verify_oracle"
DB_DISASTER="sqsh_verify_disaster"
DB_BRIDGE="sqsh_verify_bridge"
ORACLE_WT=""
WORKDIR="$(mktemp -d -t sqsh_verify.XXXXXX)"

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_GREEN=$'\033[32m'; C_RED=$'\033[31m'; C_CYAN=$'\033[36m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_GREEN=""; C_RED=""; C_CYAN=""
fi

PASS=0; FAIL=0
declare -a RESULTS=()
DROP_ON_EXIT=0   # single steps keep their DBs; only a full `all` run drops them

section() { printf '\n%s========================================================================%s\n' "$C_CYAN" "$C_RESET"; printf '%s%s%s\n' "$C_BOLD$C_CYAN" "$1" "$C_RESET"; printf '%s------------------------------------------------------------------------%s\n' "$C_CYAN" "$C_RESET"; }
info()    { printf '  %s· %s%s\n' "$C_DIM" "$1" "$C_RESET"; }
cmdlog()  { printf '  %s$ %s%s\n' "$C_DIM" "$1" "$C_RESET"; }
ok()      { PASS=$((PASS+1)); RESULTS+=("${C_GREEN}PASS${C_RESET}  $1"); printf '  %s✓ PASS%s  %s\n' "$C_GREEN" "$C_RESET" "$1"; }
bad()     { FAIL=$((FAIL+1)); RESULTS+=("${C_RED}FAIL${C_RESET}  $1"); printf '  %s✗ FAIL%s  %s\n' "$C_RED" "$C_RESET" "$1"; }

fp() { "$VENV_PY" "$FP" "$@"; }

# db_url <db> — the asyncpg URL the app + alembic env read from $DATABASE_URL.
db_url() { printf 'postgresql+asyncpg://%s:%s@%s:%s/%s' "$PGUSER" "$PGPASSWORD" "$PGHOST" "$PGPORT" "$1"; }

drop_dbs() {
  fp dropdb "$DB_NEW"      >/dev/null 2>&1 && info "dropped $DB_NEW"
  fp dropdb "$DB_ORACLE"   >/dev/null 2>&1 && info "dropped $DB_ORACLE"
  fp dropdb "$DB_DISASTER" >/dev/null 2>&1 && info "dropped $DB_DISASTER"
  fp dropdb "$DB_BRIDGE"   >/dev/null 2>&1 && info "dropped $DB_BRIDGE"
}

# remove_oracle_worktree — always tear down the throwaway origin/main checkout so
# no dangling git worktree registration is left behind (the DB itself persists).
remove_oracle_worktree() {
  [[ -n "$ORACLE_WT" ]] && git -C "$REPO_ROOT" worktree remove --force "$ORACLE_WT" >/dev/null 2>&1
  git -C "$REPO_ROOT" worktree prune >/dev/null 2>&1 || true
}

on_exit() {
  remove_oracle_worktree
  if [[ "$DROP_ON_EXIT" == "1" ]]; then
    section "Cleanup"
    if [[ "$KEEP_DBS" == "1" ]]; then
      info "KEEP_DBS=1 — leaving throwaway databases in place"
    else
      drop_dbs
    fi
  fi
  rm -rf "$WORKDIR"
}
trap on_exit EXIT

# capture_state <db> <prefix> — snapshot schema + row counts under WORKDIR/<prefix>_*.json
capture_state() {
  fp fingerprint "$1" "$WORKDIR/$2_schema.json"
  fp row-counts  "$1" "$WORKDIR/$2_rows.json"
}

# assert_unchanged <before_prefix> <after_prefix> — 0 if schema+rows identical,
# else prints the offending diffs and returns 1 (caller emits ok/bad).
assert_unchanged() {
  local before="$1" after="$2" schema_ok=0 rows_ok=0
  fp diff "$WORKDIR/${before}_schema.json" "$WORKDIR/${after}_schema.json" >/dev/null && schema_ok=1
  diff -u "$WORKDIR/${before}_rows.json" "$WORKDIR/${after}_rows.json" >/dev/null && rows_ok=1
  [[ "$schema_ok" == 1 && "$rows_ok" == 1 ]] && return 0
  [[ "$schema_ok" == 1 ]] || fp diff "$WORKDIR/${before}_schema.json" "$WORKDIR/${after}_schema.json"
  [[ "$rows_ok" == 1 ]] || diff -u "$WORKDIR/${before}_rows.json" "$WORKDIR/${after}_rows.json" || true
  return 1
}

# run_migrations_py — invoke the real startup migration path against $DATABASE_URL.
run_migrations_py() {
  "$VENV_PY" -c "import asyncio;from app.core.migrations import run_migrations;asyncio.run(run_migrations())"
}

# build_oracle — build $DB_ORACLE from $ORACLE_REF's fresh-install path.
#
# The pre-squash migration chain is NOT standalone (its base revision is an
# ALTER TABLE, not a create-tables baseline), so `alembic upgrade head` from an
# empty DB is impossible.  ORACLE_REF's own fresh-install path is
# create_all(models) + stamp head — exactly what a fresh customer install ran —
# so we reproduce it by calling that ref's run_migrations() from a throwaway
# worktree.  This is the correct historical reference and can never be the
# operator's drifted local dev DB.  Returns 0 on success, 1 otherwise.
build_oracle() {
  cmdlog "git worktree add <tmp> $ORACLE_REF && run_migrations()  # build oracle from $ORACLE_REF"
  git -C "$REPO_ROOT" fetch --quiet origin "${ORACLE_REF#origin/}" >/dev/null 2>&1 || true
  ORACLE_WT="$WORKDIR/oracle_wt"
  if ! git -C "$REPO_ROOT" worktree add --detach "$ORACLE_WT" "$ORACLE_REF" >/dev/null 2>&1; then
    info "could not create a worktree for $ORACLE_REF"; ORACLE_WT=""; return 1
  fi
  fp dropdb "$DB_ORACLE" >/dev/null 2>&1
  if ! fp createdb "$DB_ORACLE" >/dev/null 2>&1; then
    info "could not create $DB_ORACLE"; return 1
  fi
  # Run the REF's own migration bootstrap (cwd + PYTHONPATH pin `app` to the
  # worktree so we build the ref's schema, not the current branch's baseline).
  if ( cd "$ORACLE_WT/backend" \
        && PYTHONPATH="$ORACLE_WT/backend" DATABASE_URL="$(db_url "$DB_ORACLE")" \
           "$VENV_PY" -c "import asyncio;from app.core.migrations import run_migrations;asyncio.run(run_migrations())" ) \
       >"$WORKDIR/oracle_build.log" 2>&1; then
    info "built oracle $DB_ORACLE from $ORACLE_REF"
    return 0
  fi
  info "oracle build failed — see $WORKDIR/oracle_build.log"
  tail -5 "$WORKDIR/oracle_build.log" | sed 's/^/      /'
  return 1
}

cd "$BACKEND_DIR" || exit 1
ALEMBIC=( "$VENV_PY" -m alembic -c alembic/alembic.ini )
current_rev() { "${ALEMBIC[@]}" current 2>/dev/null | grep -oE '^[a-zA-Z0-9_]+' | head -1; }
head_rev()    { "${ALEMBIC[@]}" heads   2>/dev/null | grep -oE '^[a-zA-Z0-9_]+' | head -1; }

# run_drift_check <logfile> — run `alembic check` and classify the result:
#   0 = no drift (schema matches the models)
#   1 = genuine model drift (check reported concrete upgrade operations)
#   2 = check could NOT run (DB unreachable / import / config error) — NOT a
#       schema verdict, so the caller must report it differently from real drift.
run_drift_check() {
  local logfile="$1" file ops
  if "${ALEMBIC[@]}" check >"$logfile" 2>&1; then
    return 0
  fi
  # Old Alembic without a `check` subcommand → discarded autogenerate probe.
  if grep -qi "invalid choice\|no such command\|unknown command" "$logfile"; then
    "${ALEMBIC[@]}" revision --autogenerate -m verify_squash_drift_probe >"$logfile" 2>&1 || return 2
    file="$(grep -oE 'Generating .*\.py' "$logfile" | awk '{print $2}')"
    [[ -n "$file" && -f "$file" ]] || return 2
    ops="$(awk '/def upgrade\(/ {in_upgrade=1; next} /def downgrade\(/ {in_upgrade=0} in_upgrade && /^[[:space:]]*op\./ {ops++} END {print ops+0}' "$file")"
    rm -f "$file"
    [[ "$ops" == "0" ]] && return 0 || return 1
  fi
  # `check` ran and reported concrete operations → real schema drift.
  if grep -qi "new upgrade operations detected" "$logfile"; then
    return 1
  fi
  # Anything else: could not connect / import / config error — not a drift result.
  return 2
}

# drift_signal_lines <logfile> — the meaningful lines only, stripping the noisy
# pgvector operator-class warnings so a hand-runner sees the actual verdict.
drift_signal_lines() {
  grep -ivE 'UserWarning|util\.warn|setting up autogenerate|Context impl|will assume transactional|Cannot compare index|assuming equal and skipping|^[[:space:]]*util\.warn' "$1"
}

# BASELINE_REV / PRE_SQUASH_FINAL_HEAD are resolved from the working tree + the
# compatibility metadata module; every step needs them, so resolve once here.
BASELINE_REV=""
PRE_SQUASH_FINAL_HEAD=""
resolve_meta() {
  if ! "$VENV_PY" -c "import asyncpg, alembic, pgvector" >/dev/null 2>&1; then
    printf '%sMissing deps (asyncpg / alembic / pgvector) in %s%s\n' "$C_RED" "$VENV_PY" "$C_RESET"; exit 1
  fi
  # Fail fast with a friendly message if Postgres is unreachable, instead of
  # letting a raw asyncpg traceback surface deep inside a step.
  if ! fp count-tables "$PGADMINDB" >/dev/null 2>&1; then
    printf '%sPostgres not reachable at %s@%s:%s (admin db: %s)%s\n' \
      "$C_RED" "$PGUSER" "$PGHOST" "$PGPORT" "$PGADMINDB" "$C_RESET"
    printf '  Start the dev database first, e.g.:\n'
    printf '    docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres\n'
    printf '  or point PGHOST/PGPORT/PGUSER/PGPASSWORD/PGADMINDB at a running instance.\n'
    exit 1
  fi
  BASELINE_REV="$(head_rev)"
  if [[ -z "$BASELINE_REV" ]]; then
    printf '%sCould not resolve Alembic head%s\n' "$C_RED" "$C_RESET"; exit 1
  fi
  PRE_SQUASH_FINAL_HEAD="$("$VENV_PY" -c 'from app.core._pre_squash_revisions import PRE_SQUASH_FINAL_HEAD; print(PRE_SQUASH_FINAL_HEAD)' 2>/dev/null)"
}

# require_db_new — steps model-drift/oracle-diff read the DB that fresh-boot
# builds; fail loudly if it is missing so a hand-runner knows to run it first.
require_db_new() {
  if fp count-tables "$DB_NEW" >/dev/null 2>&1; then
    return 0
  fi
  bad "database $DB_NEW not found — run the 'fresh-boot' step first"
  return 1
}

# assert_final_head_fresh — PRE_SQUASH_FINAL_HEAD is FROZEN in
# app/core/_pre_squash_revisions.py, but ORACLE_REF (main) keeps moving until
# the squash merges.  Any migration that lands on the ref after the freeze
# means real deployments will be stamped at a NEWER head than the constant, and
# every fully-upgraded install fails closed at boot.  A schema-changing late
# migration would also fail T3's fingerprint diff — but a DATA-ONLY migration
# (backfill) changes no models, produces no schema diff, and would slip through
# every other step.  So compare the constant against the ref's actual Alembic
# head directly.  The ref's versions/ tree is materialised via `git archive`
# (cheap, no worktree) and the head computed by AST-parsing the revision graph
# — NOT alembic's ScriptDirectory, which *executes* every migration file, and
# the historical migrations import `app.*` (unimportable from a bare archive).
# Parsing handles multi-line down_revision tuples and depends_on correctly.
assert_final_head_fresh() {
  if [[ -z "$PRE_SQUASH_FINAL_HEAD" ]]; then
    bad "could not resolve PRE_SQUASH_FINAL_HEAD from app.core._pre_squash_revisions"
    return
  fi
  local ref_tree="$WORKDIR/ref_alembic" ref_heads
  mkdir -p "$ref_tree"
  git -C "$REPO_ROOT" fetch --quiet origin "${ORACLE_REF#origin/}" >/dev/null 2>&1 || true
  if ! git -C "$REPO_ROOT" archive "$ORACLE_REF" backend/alembic/versions 2>/dev/null \
       | tar -xf - -C "$ref_tree" 2>/dev/null; then
    bad "could not read backend/alembic/versions from $ORACLE_REF to check PRE_SQUASH_FINAL_HEAD freshness"
    return
  fi
  ref_heads="$("$VENV_PY" - "$ref_tree/backend/alembic/versions" 2>/dev/null <<'PY'
import ast
import pathlib
import sys

revs: set[str] = set()
parents: set[str] = set()  # down_revision + depends_on targets — never heads
for path in sorted(pathlib.Path(sys.argv[1]).glob("*.py")):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names, value = [node.target.id], node.value
        else:
            continue
        strings = [
            c.value
            for c in ast.walk(value)
            if isinstance(c, ast.Constant) and isinstance(c.value, str)
        ] if value is not None else []
        if "revision" in names and len(strings) == 1:
            revs.add(strings[0])
        elif names and set(names) <= {"down_revision", "depends_on"}:
            parents.update(strings)
print("\n".join(sorted(revs - parents)))
PY
)"
  if [[ -z "$ref_heads" ]]; then
    bad "could not resolve the Alembic head(s) of $ORACLE_REF"
  elif [[ "$ref_heads" == "$PRE_SQUASH_FINAL_HEAD" ]]; then
    ok "PRE_SQUASH_FINAL_HEAD ($PRE_SQUASH_FINAL_HEAD) is the single Alembic head of $ORACLE_REF"
  else
    bad "PRE_SQUASH_FINAL_HEAD ($PRE_SQUASH_FINAL_HEAD) != Alembic head(s) of $ORACLE_REF: $(printf '%s' "$ref_heads" | tr '\n' ' ') — a migration merged after the freeze; re-freeze the constant (and re-fold or rebase) before shipping"
  fi
}

step_preflight() {
  section "Preflight"
  info "repo root     : $REPO_ROOT"
  info "backend       : $BACKEND_DIR"
  info "python        : $VENV_PY"
  info "postgres      : $PGUSER@$PGHOST:$PGPORT (admin db: $PGADMINDB)"
  info "final head    : ${PRE_SQUASH_FINAL_HEAD:-<unresolved>}"
  info "current head  : $BASELINE_REV"

  local wt
  wt="$(find alembic/versions -maxdepth 1 -name '*.py' | wc -l | tr -d ' ')"
  if [[ "$wt" == "1" ]]; then
    ok "working tree has exactly one migration file (squashed baseline)"
  else
    bad "working tree should have exactly one migration file; found $wt"
  fi

  assert_final_head_fresh
}

step_fresh_boot() {
  section "T1  Fresh DB: squashed baseline boots from empty database"
  fp createdb "$DB_NEW" && info "created empty DB $DB_NEW (+ vector extension)"
  export DATABASE_URL="$(db_url "$DB_NEW")"
  cmdlog "alembic upgrade head"
  if "${ALEMBIC[@]}" upgrade head >"$WORKDIR/t1.log" 2>&1; then
    local cur; cur="$(current_rev)"
    if [[ "$cur" == "$BASELINE_REV" ]]; then
      ok "fresh DB reached current head $BASELINE_REV"
    else
      bad "fresh DB expected $BASELINE_REV, got ${cur:-<none>}"
    fi
  else
    bad "fresh DB upgrade failed"
    tail -5 "$WORKDIR/t1.log" | sed 's/^/    /'
  fi
}

step_model_drift() {
  section "T2  Fresh DB: schema matches models"
  export DATABASE_URL="$(db_url "$DB_NEW")"
  require_db_new || return
  cmdlog "alembic check"
  run_drift_check "$WORKDIR/t2.log"
  case $? in
    0)
      ok "model drift check produced no upgrade operations"
      ;;
    1)
      bad "model has drifted from the baseline — 'alembic check' detected operations"
      drift_signal_lines "$WORKDIR/t2.log" | grep -iE 'detected|new upgrade operations|FAILED' | sed 's/^/    /'
      ;;
    2)
      bad "could not RUN the drift check (DB/config error — not a schema verdict)"
      drift_signal_lines "$WORKDIR/t2.log" | tail -12 | sed 's/^/    /'
      ;;
  esac
}

step_oracle_diff() {
  section "T3  Fresh baseline == real historical/oracle DB"
  export DATABASE_URL="$(db_url "$DB_NEW")"
  require_db_new || return
  # Apply runtime extras before taking the baseline fingerprint.
  if "$VENV_PY" -c "import asyncio;from app.core.migrations import _ensure_schema_extras;asyncio.run(_ensure_schema_extras())" >/dev/null 2>&1; then
    info "runtime schema extras applied on $DB_NEW"
  else
    info "runtime schema extras could not be applied directly; continuing so diff reveals gaps"
  fi
  fp fingerprint "$DB_NEW" "$WORKDIR/fp_new.json" && info "fingerprinted fresh baseline"

  # Resolve the oracle: an explicit ORACLE_DB (e.g. a restored prod snapshot) is
  # used as-is; otherwise it is built fresh from ORACLE_REF (origin/main) so the
  # comparison can never silently fall back to the operator's drifted dev DB.
  # No oracle → FAIL, not skip: this is the strongest schema check in the
  # harness, and a run that silently degraded to "everything else passed"
  # would still print RESULT: PASSED — exactly the false attestation the
  # harness exists to prevent.
  local oracle_db="$ORACLE_DB"
  if [[ -z "$oracle_db" ]]; then
    if build_oracle; then
      oracle_db="$DB_ORACLE"
    else
      bad "could not build oracle from $ORACLE_REF — the oracle comparison is mandatory"
      return
    fi
  elif ! fp count-tables "$oracle_db" >/dev/null 2>&1; then
    bad "explicit oracle DB '$oracle_db' not reachable — the oracle comparison is mandatory"
    return
  else
    info "using explicit oracle DB $oracle_db"
  fi

  fp fingerprint "$oracle_db" "$WORKDIR/fp_oracle.json" && info "fingerprinted oracle $oracle_db"
  if fp diff "$WORKDIR/fp_new.json" "$WORKDIR/fp_oracle.json"; then
    ok "fresh baseline schema equals oracle schema"
  else
    bad "fresh baseline schema differs from oracle schema"
  fi
}

step_disaster_recovery() {
  section "T4  Disaster recovery: manual stamp --purge is metadata-only"
  # Documents the UPGRADING.md fallback: an operator running Alembic by hand whose
  # DB is stamped at ANY deleted revision recovers with `stamp --purge <baseline>`.
  # (The startup auto-heal would instead REFUSE an unrecognised marker like this —
  # T5 covers the one revision it DOES heal.)
  fp createdb "$DB_DISASTER" >/dev/null && info "created DB $DB_DISASTER"
  export DATABASE_URL="$(db_url "$DB_DISASTER")"
  "${ALEMBIC[@]}" upgrade head >/dev/null 2>&1
  capture_state "$DB_DISASTER" disaster_before
  fp set-version "$DB_DISASTER" deadbeefc0de && info "stamped fake deleted revision deadbeefc0de"
  cmdlog "alembic upgrade head  # expected to fail (deleted revision)"
  if "${ALEMBIC[@]}" upgrade head >"$WORKDIR/disaster.log" 2>&1; then
    bad "deleted-revision failure did not reproduce; upgrade unexpectedly succeeded"
  else
    info "deleted-revision failure reproduced"
    cmdlog "alembic stamp --purge $BASELINE_REV  # manual recovery"
    if "${ALEMBIC[@]}" stamp --purge "$BASELINE_REV" >/dev/null 2>&1; then
      capture_state "$DB_DISASTER" disaster_after
      if assert_unchanged disaster_before disaster_after; then
        ok "manual stamp --purge recovered with zero schema/row change"
      else
        bad "manual stamp --purge changed schema or row counts"
      fi
    else
      bad "manual stamp --purge recovery failed"
    fi
  fi
}

step_bridge_heal() {
  section "T5  Fully-upgraded bridge: last pre-squash head auto-heals to baseline"
  if [[ -z "$PRE_SQUASH_FINAL_HEAD" ]]; then
    bad "could not resolve PRE_SQUASH_FINAL_HEAD from app.core._pre_squash_revisions"
    return
  fi
  info "last pre-squash head: $PRE_SQUASH_FINAL_HEAD"
  fp createdb "$DB_BRIDGE" >/dev/null && info "created DB $DB_BRIDGE"
  export DATABASE_URL="$(db_url "$DB_BRIDGE")"
  # Build a genuine fully-upgraded state through the real startup path
  # (create_all + stamp head + runtime schema extras), then rewind the version
  # marker to simulate an old deployment stamped at the last pre-squash head.
  # Both snapshots go through run_migrations(), so schema extras are on each side
  # and the only expected change is the alembic_version pointer.
  cmdlog "run_migrations()  # build fully-upgraded state"
  if ! run_migrations_py >"$WORKDIR/bridge_build.log" 2>&1; then
    bad "could not build the fully-upgraded bridge state"
    tail -8 "$WORKDIR/bridge_build.log" | sed 's/^/    /'
    return
  fi
  capture_state "$DB_BRIDGE" bridge_before
  fp set-version "$DB_BRIDGE" "$PRE_SQUASH_FINAL_HEAD" \
    && info "rewound version marker to $PRE_SQUASH_FINAL_HEAD (deleted revision)"
  cmdlog "run_migrations()  # auto-heal path"
  if run_migrations_py >"$WORKDIR/bridge_heal.log" 2>&1; then
    local cur; cur="$(current_rev)"
    capture_state "$DB_BRIDGE" bridge_after
    if [[ "$cur" == "$BASELINE_REV" ]] && assert_unchanged bridge_before bridge_after; then
      ok "last pre-squash head auto-healed to baseline $BASELINE_REV (metadata-only)"
    else
      bad "bridge heal landed on ${cur:-<none>} (expected $BASELINE_REV) or changed schema/rows"
    fi
  else
    bad "run_migrations() failed to auto-heal a DB at the last pre-squash head"
    tail -8 "$WORKDIR/bridge_heal.log" | sed 's/^/    /'
  fi
}

step_summary() {
  section "Summary"
  for r in "${RESULTS[@]}"; do printf '  %s\n' "$r"; done
  printf '\n  %s%d passed%s, %s%d failed%s\n' \
    "$C_GREEN" "$PASS" "$C_RESET" "$C_RED" "$FAIL" "$C_RESET"
  if [[ "$FAIL" -gt 0 ]]; then
    printf '\n  %sRESULT: FAILED%s\n' "$C_BOLD$C_RED" "$C_RESET"
    return 1
  fi
  printf '\n  %sRESULT: PASSED%s\n' "$C_BOLD$C_GREEN" "$C_RESET"
  return 0
}

usage() {
  # Print the header comment block (everything between the shebang and the
  # first non-comment line), stripped of its leading '# ' markers.  Absolute
  # path via SCRIPT_DIR: the script cd's into BACKEND_DIR long before this
  # runs, so a relative ${BASH_SOURCE[0]} would no longer resolve.
  sed -n '2,/^set -/p' "$SCRIPT_DIR/${BASH_SOURCE[0]##*/}" | sed '$d' | sed 's/^#\{1,\} \{0,1\}//'
}

STEP="${1:-all}"
case "$STEP" in
  all)
    [[ "$KEEP_DBS" == "1" ]] || DROP_ON_EXIT=1
    resolve_meta
    step_preflight
    step_fresh_boot
    step_model_drift
    step_oracle_diff
    step_disaster_recovery
    step_bridge_heal
    step_summary
    ;;
  preflight)          resolve_meta; step_preflight;          step_summary ;;
  fresh-boot)         resolve_meta; step_fresh_boot;         step_summary ;;
  model-drift)        resolve_meta; step_model_drift;        step_summary ;;
  oracle-diff)        resolve_meta; step_oracle_diff;        step_summary ;;
  disaster-recovery)  resolve_meta; step_disaster_recovery;  step_summary ;;
  bridge-heal)        resolve_meta; step_bridge_heal;        step_summary ;;
  cleanup)
    section "Cleanup"
    drop_dbs
    info "done"
    ;;
  -h|--help|help) usage ;;
  *) printf '%sUnknown step: %s%s\n\n' "$C_RED" "$STEP" "$C_RESET"; usage; exit 2 ;;
esac
