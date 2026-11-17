#!/usr/bin/env bash
# Run the same checks as GitHub CI locally before pushing.
# Usage:
#   ./scripts/ci-local.sh
#   ./scripts/ci-local.sh --no-e2e
#   ./scripts/ci-local.sh --only backend,frontend

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN_BACKEND=1
RUN_FRONTEND=1
RUN_MIGRATIONS=1
RUN_INTEGRATION=1
RUN_DOCS=1
RUN_E2E=1

usage() {
  cat <<'EOF'
Usage: ./scripts/ci-local.sh [options]

Options:
  --no-e2e               Skip Playwright e2e suite.
  --only <csv>           Run only selected groups.
                         Groups: backend,frontend,migrations,integration,docs,e2e
  -h, --help             Show this help.

Runs from the main checkout or from a linked git worktree. The dependency
services come from the dev override (docker-compose.yml + docker-compose.dev.yml)
in the checkout's own compose project, so the dev stack running there is
reused. The tests reach them with the dev credentials (copilot / copilot_dev /
copilot_dev_secret): a .env that changes DB_PASSWORD or the S3 keys breaks
them. The e2e stack shares one compose project across checkouts, named after
the main checkout with an "-e2e" suffix, so the containers pinned by name in
docker-compose.yml are reused rather than fought over, and a dev stack is
never recreated.

Runs one at a time: a second run, from any checkout of this clone, waits until
the first one ends before it starts (each run takes all the cores and several
GB of memory, and every run shares one database).

Environment overrides (optional):
  CI_LOCAL_NO_LOCK       Set to 1 to start without waiting for another run.
  CI_LOCAL_LOCK_FILE     Lock file runs queue on (default: ci-local.lock in the
                         clone's git common directory).
  COMPOSE_PROJECT_NAME   Compose project of the dev services; the e2e stack
                         uses it with an "-e2e" suffix (default: see above).
  CONTAINER_CLI          Container engine binary (auto-detect: docker, then podman).
  SECRET_KEY             Backend test secret.
  ADMIN_EMAIL            Admin email for local platform startup/e2e login.
  ADMIN_PASSWORD         Admin password for local platform startup/e2e login.
  E2E_USER_EMAIL         E2E login email (defaults to ADMIN_EMAIL).
  E2E_USER_PASSWORD      E2E login password (defaults to ADMIN_PASSWORD).
  MKDOCS_GIT_COMMITTERS_APIKEY
                         GitHub token for the docs contributors section (defaults
                         to GITHUB_TOKEN, then `gh auth token`; with none, the docs
                         build without that section).
EOF
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

# shellcheck source=scripts/ci-local-lock.sh
source "$ROOT/scripts/ci-local-lock.sh"

resolve_compose_project() {
  if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
    COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME}-e2e"
    export COMPOSE_PROJECT_NAME
    return 0
  fi
  # Compose derives its project name from the compose file's directory, so a run
  # from a linked git worktree lands in a different project than a run from the
  # main checkout. docker-compose.yml pins `container_name` (postgres,
  # platform, ...) and those names are global to the container engine, so the
  # second project cannot create them: the run aborts with "container name is
  # already in use" before a single check executes. Pin the project to the main
  # checkout's directory name, so every worktree shares that project and adopts
  # the existing containers instead of colliding with them. The "-e2e" suffix
  # keeps it apart from the dev stack, whose services have the same names and
  # whose project, in the main checkout, is that same directory name.
  local main_root="$ROOT" common_dir
  if common_dir="$(git_common_dir)"; then
    main_root="$(cd "$common_dir/.." && pwd)"
  fi
  # Compose only accepts [a-z0-9][a-z0-9_-]* for an explicit project name, and
  # unlike a derived name it does not sanitise the value it is handed.
  COMPOSE_PROJECT_NAME="$(basename "$main_root" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -e 's/[^a-z0-9_-]/-/g' -e 's/^[^a-z0-9]*//')"
  if [[ -z "$COMPOSE_PROJECT_NAME" ]]; then
    echo "Could not derive a compose project name from '$main_root'." >&2
    echo "Set COMPOSE_PROJECT_NAME explicitly and re-run." >&2
    exit 1
  fi
  COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME}-e2e"
  export COMPOSE_PROJECT_NAME
}

# Every compose invocation goes through here: the explicit `-f` keeps the run
# independent of the caller's cwd, while still resolving the project directory
# (and so the platform image build context) to the checkout this script lives
# in — which is what makes a worktree run test the worktree's code. The first
# argument is a compose file, or several joined by ':' (a base and its
# overrides, in order).
compose() {
  local files file
  local -a args=()
  IFS=: read -ra files <<<"$1"
  shift
  for file in "${files[@]}"; do
    args+=(-f "$ROOT/$file")
  done
  "${CONTAINER_COMPOSE[@]}" "${args[@]}" "$@"
}

# Pinning the project makes every checkout agree on one project, but the names
# pinned by `container_name` stay global to the engine: a container some *other*
# project still owns cannot be re-created here, and compose only reports an
# opaque "container name is already in use". That is what a leftover e2e stack
# from a run predating the "-e2e" project looks like, so name the cause and the
# way out instead.
compose_up() {
  local file="$1"
  shift
  if compose "$file" up -d "$@"; then
    return 0
  fi
  cat >&2 <<EOF

Could not start the services declared in $file (compose project: ${COMPOSE_PROJECT_NAME:-derived from the checkout directory}).

If the failure above is "container name ... is already in use", another compose
project still owns that container, typically an e2e stack left by a run that
predates the "-e2e" project. Find the owner:

  $CONTAINER_CLI inspect <container-name> --format '{{index .Config.Labels "com.docker.compose.project"}}'

then drop the stale container (this does not remove named volumes; postgres/minio data stays):

  $CONTAINER_CLI rm -f <container-name>
EOF
  exit 1
}

resolve_compose_cmd() {
  local cli="$1"
  if "$cli" compose version >/dev/null 2>&1; then
    CONTAINER_COMPOSE=("$cli" compose)
    return 0
  fi
  if [[ "$cli" == "podman" ]] && command -v podman-compose >/dev/null 2>&1; then
    CONTAINER_COMPOSE=(podman-compose)
    return 0
  fi
  if [[ "$cli" == "docker" ]] && command -v docker-compose >/dev/null 2>&1; then
    CONTAINER_COMPOSE=(docker-compose)
    return 0
  fi

  echo "No compose command found for $cli." >&2
  echo "Install '$cli compose' (preferred), or set CONTAINER_CLI to another engine." >&2
  exit 1
}

parse_only() {
  local csv="$1"
  RUN_BACKEND=0
  RUN_FRONTEND=0
  RUN_MIGRATIONS=0
  RUN_INTEGRATION=0
  RUN_DOCS=0
  RUN_E2E=0

  local selected=0
  IFS=',' read -r -a parts <<<"$csv"
  for part in "${parts[@]}"; do
    # Trim surrounding whitespace so "--only backend, frontend" works.
    part="${part#"${part%%[![:space:]]*}"}"
    part="${part%"${part##*[![:space:]]}"}"
    [[ -z "$part" ]] && continue
    case "${part}" in
      backend) RUN_BACKEND=1 ;;
      frontend) RUN_FRONTEND=1 ;;
      migrations) RUN_MIGRATIONS=1 ;;
      integration) RUN_INTEGRATION=1 ;;
      docs) RUN_DOCS=1 ;;
      e2e) RUN_E2E=1 ;;
      *)
        echo "Unknown group in --only: ${part}" >&2
        exit 1
        ;;
    esac
    selected=1
  done

  if [[ "$selected" -eq 0 ]]; then
    echo "--only requires at least one group (backend,frontend,migrations,integration,docs,e2e)" >&2
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-e2e)
      RUN_E2E=0
      shift
      ;;
    --only)
      if [[ $# -lt 2 ]]; then
        echo "--only requires a csv value" >&2
        exit 1
      fi
      parse_only "$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

acquire_run_lock

if [[ $RUN_MIGRATIONS -eq 1 || $RUN_INTEGRATION -eq 1 || $RUN_E2E -eq 1 ]]; then
  if [[ -n "${CONTAINER_CLI:-}" ]]; then
    require_cmd "$CONTAINER_CLI"
  elif command -v docker >/dev/null 2>&1; then
    CONTAINER_CLI="docker"
  elif command -v podman >/dev/null 2>&1; then
    CONTAINER_CLI="podman"
  else
    echo "Missing required container engine: docker or podman" >&2
    exit 1
  fi
  resolve_compose_cmd "$CONTAINER_CLI"
  DEV_COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-}"
  resolve_compose_project
fi
if [[ $RUN_BACKEND -eq 1 || $RUN_MIGRATIONS -eq 1 || $RUN_INTEGRATION -eq 1 || $RUN_DOCS -eq 1 ]]; then
  require_cmd uv
fi
if [[ $RUN_FRONTEND -eq 1 || $RUN_E2E -eq 1 ]]; then
  require_cmd corepack
fi
if [[ $RUN_FRONTEND -eq 1 ]]; then
  require_cmd npm
fi
if [[ $RUN_E2E -eq 1 ]]; then
  require_cmd curl
fi

SECRET_KEY="${SECRET_KEY:-ci-test-secret-key-not-for-production}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@test.filigran.io}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-test-admin-password-123}"
E2E_USER_EMAIL="${E2E_USER_EMAIL:-$ADMIN_EMAIL}"
E2E_USER_PASSWORD="${E2E_USER_PASSWORD:-$ADMIN_PASSWORD}"

BACKEND_PY="$ROOT/backend/.venv/bin/python"

export SECRET_KEY ADMIN_EMAIL ADMIN_PASSWORD

# The uv values of the workflow-level `env:` in ci.yml (pypi.org can stop
# answering for several minutes): change them together.
export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-30}"
export UV_HTTP_RETRIES="${UV_HTTP_RETRIES:-5}"

# Syncs the uv project of the current directory (its .venv) to its uv.lock the
# way the CI install steps do: up to 3 times, a minute apart.
uv_sync() {
  local attempt
  for attempt in 1 2 3; do
    uv sync --locked && return 0
    echo "    uv sync failed (attempt $attempt of 3)" >&2
    if [[ "$attempt" -lt 3 ]]; then sleep 60; fi
  done
  return 1
}

# The docs group has a uv project of its own (docs/, synced in the docs block
# below); the backend one is only required by the backend / migrations /
# integration groups.
if [[ $RUN_BACKEND -eq 1 || $RUN_MIGRATIONS -eq 1 || $RUN_INTEGRATION -eq 1 ]]; then
  echo "==> Backend test deps"
  (cd "$ROOT/backend" && uv_sync)
fi

if [[ $RUN_BACKEND -eq 1 ]]; then
  echo "==> Backend lint (ruff)"
  (cd "$ROOT/backend" && "$BACKEND_PY" -m ruff check .)

  # Scoped to app/services/evaluation/extraction by [tool.mypy], as in CI.
  echo "==> Backend type check (mypy)"
  (cd "$ROOT/backend" && "$BACKEND_PY" -m mypy)

  echo "==> Backend unit tests"
  (cd "$ROOT/backend" && SECRET_KEY="$SECRET_KEY" "$BACKEND_PY" -m pytest -m "not integration" -n auto --dist loadgroup -v --tb=short)
fi

# Runs every `run:` command of the Frontend lint, Frontend (TypeScript) and
# Office add-in lint jobs of ci.yml, in the same order, so this group fails exactly when those jobs would: a step added there
# is added here too.
if [[ $RUN_FRONTEND -eq 1 ]]; then
  echo "==> Frontend install"
  (cd "$ROOT/frontend" && corepack enable && yarn install --immutable)

  echo "==> Frontend typecheck (app + e2e specs)"
  (cd "$ROOT/frontend" && yarn typecheck && yarn typecheck -p tests_e2e)

  echo "==> Frontend lint (oxlint)"
  (cd "$ROOT/frontend" && yarn lint)

  echo "==> Frontend dead code (knip)"
  (cd "$ROOT/frontend" && yarn knip)

  echo "==> Frontend command manifests"
  (cd "$ROOT/frontend" && yarn validate:commands)

  echo "==> Frontend i18n catalogs"
  (cd "$ROOT/frontend" && yarn validate:i18n)

  echo "==> Frontend unit tests"
  (cd "$ROOT/frontend" && yarn test:unit)

  echo "==> Frontend design-system guards"
  (cd "$ROOT/frontend" && yarn build && node scripts/fds-migration/verify-classes.mjs && node scripts/fds-migration/count-legacy.mjs --check)

  echo "==> Office add-in install + typecheck"
  (cd "$ROOT/apps/microsoft-office" && npm ci && npm run check)
fi

if [[ $RUN_MIGRATIONS -eq 1 || $RUN_INTEGRATION -eq 1 ]]; then
  echo "==> Start backend dependency services (postgres, redis, minio) via $CONTAINER_CLI (project: ${DEV_COMPOSE_PROJECT_NAME:-derived from the checkout directory})"
  # The dev override pins no container name: start its services in the
  # project the dev stack uses in this checkout, so the run reuses the dev
  # stack running there (and its host ports). The subshell
  # keeps the e2e stack's project for the rest of the run.
  # shellcheck disable=SC2030
  (
    if [[ -n "$DEV_COMPOSE_PROJECT_NAME" ]]; then
      export COMPOSE_PROJECT_NAME="$DEV_COMPOSE_PROJECT_NAME"
    else
      unset COMPOSE_PROJECT_NAME
    fi
    compose_up docker-compose.yml:docker-compose.dev.yml postgres redis minio
  ) || exit 1

  # The host ports the dev services publish: DEV_POSTGRES_PORT, DEV_REDIS_PORT
  # and DEV_SILO_PORT in .env move them off their defaults.
  dev_host_port() {
    local address
    # shellcheck disable=SC2030,SC2031
    address="$(
      if [[ -n "$DEV_COMPOSE_PROJECT_NAME" ]]; then
        export COMPOSE_PROJECT_NAME="$DEV_COMPOSE_PROJECT_NAME"
      else
        unset COMPOSE_PROJECT_NAME
      fi
      compose docker-compose.yml:docker-compose.dev.yml port "$1" "$2"
    )" || return 1
    echo "${address##*:}"
  }
  DEV_POSTGRES_HOST_PORT="$(dev_host_port postgres 5432)"
  DEV_REDIS_HOST_PORT="$(dev_host_port redis 6379)"
  DEV_SILO_HOST_PORT="$(dev_host_port minio 9000)"
fi

if [[ $RUN_MIGRATIONS -eq 1 ]]; then
  echo "==> Migration integrity tests"
  (cd "$ROOT/backend" && "$BACKEND_PY" -m pytest tests/test_migration_integrity.py -v --tb=short)

  # The post-merge check (scripts/migration-post-merge-check.sh): a
  # local branch is not merged into main the way a pull request's checkout is
  # on GitHub, so the suite above only proves the history is sound on THIS
  # branch; the script
  # re-runs it against the tree a merge into the live tip of main would produce,
  # so a migration that landed on main after this branch was cut shows up here
  # rather than at merge time. It fetches main; offline, it checks the local
  # origin/main.
  echo "==> Migration integrity against the live tip of main"
  (
    cd "$ROOT"
    # The merge is built from committed trees only.
    if [ -n "$(git status --porcelain -- backend/alembic/versions/)" ]; then
      echo "    WARNING: uncommitted changes under backend/alembic/versions/ are not part of this check - commit them to include them."
    fi
    PATH="$ROOT/backend/.venv/bin:$PATH" scripts/migration-post-merge-check.sh
  )

  echo "==> Fresh install migration test"
  (
    cd "$ROOT/backend"
    DATABASE_URL="postgresql+asyncpg://copilot:copilot_dev@localhost:$DEV_POSTGRES_HOST_PORT/copilot" \
    SECRET_KEY="$SECRET_KEY" \
    "$BACKEND_PY" -m pytest tests/test_fresh_install.py -v --tb=long
  )
fi

if [[ $RUN_INTEGRATION -eq 1 ]]; then
  echo "==> Backend integration tests"
  (
    cd "$ROOT/backend"
    DATABASE_URL="postgresql+asyncpg://copilot:copilot_dev@localhost:$DEV_POSTGRES_HOST_PORT/copilot" \
    REDIS_URL="redis://localhost:$DEV_REDIS_HOST_PORT" \
    S3_ENDPOINT="localhost:$DEV_SILO_HOST_PORT" \
    S3_ACCESS_KEY="copilot" \
    S3_SECRET_KEY="copilot_dev_secret" \
    S3_USE_SSL="false" \
    SECRET_KEY="$SECRET_KEY" \
    APP_VERSION="0.0.0-ci-local" \
    ADMIN_EMAIL="$ADMIN_EMAIL" \
    ADMIN_PASSWORD="$ADMIN_PASSWORD" \
    PYTEST_MARKED_FILES=integration \
    "$BACKEND_PY" -m pytest -m integration -n auto --dist loadgroup -v --tb=long
  )
fi

if [[ $RUN_DOCS -eq 1 ]]; then
  echo "==> Docs build"
  (
    cd "$ROOT/docs"
    uv_sync
    # git-committers reads page contributors from the GitHub API with a token.
    # Use the token CI would have (or the gh CLI's); with none, build without
    # that one plugin (docs/hooks/git_committers.py would switch it off too),
    # never drop --strict.
    if [[ -z "${MKDOCS_GIT_COMMITTERS_APIKEY:-}" ]]; then
      if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        MKDOCS_GIT_COMMITTERS_APIKEY="$GITHUB_TOKEN"
      elif command -v gh >/dev/null 2>&1; then
        MKDOCS_GIT_COMMITTERS_APIKEY="$(gh auth token --hostname github.com 2>/dev/null || true)"
      fi
    fi
    if [[ -n "${MKDOCS_GIT_COMMITTERS_APIKEY:-}" ]]; then
      export MKDOCS_GIT_COMMITTERS_APIKEY
    else
      echo "    (no GitHub token in MKDOCS_GIT_COMMITTERS_APIKEY, GITHUB_TOKEN or 'gh auth token' - building without the git-committers plugin, so the pages have no contributors section)"
      export MKDOCS_GIT_COMMITTERS_ENABLED=false
    fi
    .venv/bin/mkdocs build --strict
  )
fi

E2E_STACK_STARTED=0

cleanup_e2e_stack() {
  if [[ "$E2E_STACK_STARTED" -eq 1 ]]; then
    echo "==> E2E: cleanup"
    compose docker-compose.yml down -v || true
    E2E_STACK_STARTED=0
  fi
}

if [[ $RUN_E2E -eq 1 ]]; then
  # shellcheck disable=SC2031
  echo "==> E2E: build and start platform stack via $CONTAINER_CLI (project: $COMPOSE_PROJECT_NAME)"
  # Tear the stack down even when the readiness loop or Playwright fails
  # (set -e aborts the script) so a failed run never leaves containers and
  # volumes behind.
  trap cleanup_e2e_stack EXIT
  E2E_STACK_STARTED=1
  compose docker-compose.yml build platform
  # ADMIN_EMAIL / ADMIN_PASSWORD are already exported above, so compose picks
  # them up without an assignment prefix — which, in front of a function call,
  # would leak into the rest of the script instead of scoping to the call.
  compose_up docker-compose.yml minio postgres redis platform

  echo "==> E2E: wait for readiness"
  for i in $(seq 1 40); do
    if curl -sf http://localhost:4000/api/health/ready >/dev/null; then
      break
    fi
    if [[ "$i" -eq 40 ]]; then
      echo "Platform did not become ready in time" >&2
      compose docker-compose.yml logs platform || true
      exit 1
    fi
    sleep 5
  done

  echo "==> E2E: install frontend deps + playwright chromium"
  (cd "$ROOT/frontend" && corepack enable && yarn install --immutable && yarn playwright install --with-deps chromium)

  echo "==> E2E: run Playwright"
  (
    cd "$ROOT/frontend"
    E2E_BASE_URL="http://localhost:4000" \
    E2E_USER_EMAIL="$E2E_USER_EMAIL" \
    E2E_USER_PASSWORD="$E2E_USER_PASSWORD" \
    yarn test:e2e
  )

  cleanup_e2e_stack
  trap - EXIT
fi

echo "All selected CI checks passed locally."
