# shellcheck shell=bash
# Queue for scripts/ci-local.sh runs; sourced by it (needs ROOT).
#
# Runs started from parallel sessions (one worktree per PR) would each run
# pytest -n auto, tsc and the containers at once and exhaust the machine; they
# also share one database (copilot@5432), so their integration suites would
# trample each other. Queue them: a run waits for the lock before any check.
#
# The lock is a flock(2) on ci-local.lock in the clone's git common directory,
# held by ci-local-lock.py, a direct child of the run that exits once the run
# is over (its docstring says what that means). Holding it on a shell fd
# instead would let every long-lived child that inherits the fd keep the lock.

CI_LOCAL_LOCK_HOLDER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ci-local-lock.py"

# The main checkout's .git directory, shared by every linked worktree of the
# clone. --git-common-dir resolves to it even from a linked worktree, and is
# relative (".git") when it is the checkout itself.
git_common_dir() {
  local common_dir
  common_dir="$(git -C "$ROOT" rev-parse --git-common-dir 2>/dev/null)" || return 1
  [[ -n "$common_dir" ]] || return 1
  [[ "$common_dir" != /* ]] && common_dir="$ROOT/$common_dir"
  [[ -d "$common_dir" ]] || return 1
  (cd "$common_dir" && pwd)
}

acquire_run_lock() {
  if [[ "${CI_LOCAL_NO_LOCK:-0}" == "1" ]]; then
    echo "==> CI_LOCAL_NO_LOCK=1: not waiting for other local CI runs"
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "==> WARNING: python3 not found: running without the local CI queue, other runs may overlap this one" >&2
    return 0
  fi
  local lock_file="${CI_LOCAL_LOCK_FILE:-}" common_dir reply="" started=$SECONDS
  if [[ -z "$lock_file" ]]; then
    if common_dir="$(git_common_dir)"; then
      lock_file="$common_dir/ci-local.lock"
    else
      # A shared temporary directory: one name per user, so another user
      # cannot hold the queue (the holder also refuses a symlink there).
      lock_file="${TMPDIR:-/tmp}/xtm-one-ci-local-$(id -u).lock"
    fi
  fi
  # `exec` makes the holder this shell's direct child, which is how it sees the
  # run end. fd 8 stays open for the run; the holder writes one line only.
  exec 8< <(exec python3 "$CI_LOCAL_LOCK_HOLDER" "$lock_file" "$$" "$ROOT")
  read -r reply <&8 || true
  case "$reply" in
    acquired) ;;
    "acquired after waiting")
      echo "==> Local CI lock acquired after $(( SECONDS - started ))s"
      ;;
    "unlocked "*)
      echo "==> WARNING: ${reply#unlocked }: running without the local CI queue, other runs may overlap this one" >&2
      ;;
    *)
      reply="${reply#error }"
      echo "Could not take the local CI lock $lock_file: ${reply:-its holder exited without an answer}" >&2
      echo "Set CI_LOCAL_LOCK_FILE to a writable path, or CI_LOCAL_NO_LOCK=1 to skip the wait." >&2
      exit 1
      ;;
  esac
}
