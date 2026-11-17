#!/usr/bin/env bash
#
# Rebase the local dev database onto main's migration head.
#
# WHY
#   The dev database can end up on a *feature branch's* migration — e.g.
#   `d5e8mfa02786` from `feat/2786-totp-mfa`, which chains AFTER main's head
#   `c5g9tou20001`. Any other branch whose migration also chains off
#   `c5g9tou20001` then cannot run: alembic refuses to walk to a revision that
#   is not in the checked-out branch's chain.
#
#   This script unwinds the foreign branch's migration (running ITS OWN
#   `downgrade()`, borrowed from that branch — never hand-written reverse DDL),
#   leaving the database on main's head, then applies the current branch's
#   migrations.
#
# WHAT IT COSTS
#   Whatever the unwound migration added. For the TOTP migration that is five
#   columns on `users` (mfa_enabled, mfa_secret, mfa_confirmed_at,
#   mfa_last_used_step, mfa_recovery_codes). Everything else — agents,
#   assignments, knowledge bases, skills — is untouched.
#
#   Switching back to that branch re-applies its migration on startup (bodies
#   are idempotent by house rule), so this is reversible.
#
# USAGE
#   ./scripts/rebase-dev-db-onto-main.sh [FOREIGN_REVISION] [MAIN_HEAD]
#
set -euo pipefail

FOREIGN_REV="${1:-d5e8mfa02786}"
MAIN_HEAD="${2:-c5g9tou20001}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/backend"

PY="${PY:-$ROOT/backend/.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

# Config comes from the same .env the app uses.
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
[ -f "$ENV_FILE" ] || { echo "No .env at $ENV_FILE — set ENV_FILE=/path/to/.env" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a

echo "==> database : ${DATABASE_URL%%\?*}"
CURRENT=$("$PY" -m alembic -c alembic/alembic.ini current 2>/dev/null | tail -1 || true)
echo "==> current  : ${CURRENT:-unknown}"

# --- 1. Back up. Non-negotiable: the next step drops columns. ---------------
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="/tmp/dev-db-before-rebase-$STAMP.dump"
echo "==> backing up to $BACKUP"
if command -v pg_dump >/dev/null 2>&1; then
  pg_dump "$(printf '%s' "$DATABASE_URL" | sed 's#postgresql+asyncpg://#postgresql://#')" \
    -Fc -f "$BACKUP"
else
  echo "    pg_dump not on PATH — run it inside your postgres container first." >&2
  exit 1
fi

# --- 2. Borrow the foreign branch's migration so alembic can reverse it. ----
BRANCH="${FOREIGN_BRANCH:-feat/2786-totp-mfa}"
FILE=$(git -C "$ROOT" show "$BRANCH" --name-only --format= | grep 'alembic/versions' | head -1)
[ -n "$FILE" ] || { echo "Could not find the migration on $BRANCH" >&2; exit 1; }
BORROWED="alembic/versions/$(basename "$FILE")"
echo "==> borrowing $(basename "$FILE") from $BRANCH"
git -C "$ROOT" show "$BRANCH:$FILE" > "$BORROWED"
# Always put the tree back, even if alembic fails below.
trap 'rm -f "$ROOT/backend/$BORROWED"; echo "==> removed the borrowed migration"' EXIT

# --- 3. Unwind it, landing on main's head. ---------------------------------
echo "==> downgrading $FOREIGN_REV -> $MAIN_HEAD"
"$PY" -m alembic -c alembic/alembic.ini downgrade "$MAIN_HEAD"

# --- 4. Drop the borrowed file BEFORE upgrading, so there is a single head. -
rm -f "$BORROWED"
trap - EXIT

# --- 5. Apply this branch's migrations. ------------------------------------
echo "==> upgrading to this branch's head"
"$PY" -m alembic -c alembic/alembic.ini upgrade head
"$PY" -m alembic -c alembic/alembic.ini current | tail -1

echo
echo "Done. Backup kept at $BACKUP"
echo "Restore with: pg_restore -d <url> --clean --if-exists '$BACKUP'"
