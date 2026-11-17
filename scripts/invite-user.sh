#!/usr/bin/env bash
#
# invite-user.sh — create a user invitation (seed/automation).
#
# Reads credentials from a .env file (repo root by default) and creates a
# single-use, email-bound invitation. The invitee opens the registration link
# to register with that email (which they can't change) and the preset role.
#
# By default the link is returned directly (send_email=false) so you can hand
# it out yourself — no email is sent. Pass --send-email to have the server
# email the link to the invitee instead (the same path the UI uses), in which
# case the link is not returned.
#
# Usage:
#   ./scripts/invite-user.sh [options] <email> [role] [expires_in_hours]
#     email              email the invitation is bound to (required)
#     role               user | admin   (default: user)
#     expires_in_hours   optional integer (server default: 168 = 7 days)
#
#   Options:
#     -s, --send-email   email the registration link to the invitee (the
#                        server sends it); the link is not returned
#     -l, --link         print the registration link directly (default)
#     -h, --help         show this help and exit
#
# .env keys (real environment variables take precedence over the file):
#   ADMIN_EMAIL        admin account email      (required unless API_KEY set)
#   ADMIN_PASSWORD     admin account password   (required unless API_KEY set)
#   API_URL            backend base URL         (optional, default http://localhost:8100)
#   API_KEY            an "fcp-" API key to use instead of email/password (optional)
#
# Override the env file location with:  ENV_FILE=/path/to/.env ./scripts/invite-user.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"

# Track temp files (curl --config holding the bearer header) so we can wipe
# them on every exit path — normal completion, ``set -e`` failure, signal,
# crash. The bearer header file is the most sensitive output we touch.
TMP_FILES=()
cleanup() {
  for f in "${TMP_FILES[@]+"${TMP_FILES[@]}"}"; do
    [[ -f "$f" ]] && rm -f -- "$f"
  done
}
trap cleanup EXIT INT TERM

usage() {
  cat >&2 <<'EOF'
Usage: invite-user.sh [options] <email> [role] [expires_in_hours]
  email              email the invitation is bound to (required)
  role               user | admin   (default: user)
  expires_in_hours   optional integer (server default: 168 = 7 days)

Options:
  -s, --send-email   email the registration link to the invitee (the server
                     sends it); the link is not returned
  -l, --link         print the registration link directly (default)
  -h, --help         show this help and exit
EOF
}

SEND_EMAIL=false
POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -s|--send-email) SEND_EMAIL=true; shift ;;
    -l|--link)       SEND_EMAIL=false; shift ;;
    -h|--help)       usage; exit 0 ;;
    --)              shift; while [[ $# -gt 0 ]]; do POSITIONAL+=("$1"); shift; done ;;
    -*)              echo "Error: unknown option '$1'" >&2; usage; exit 1 ;;
    *)               POSITIONAL+=("$1"); shift ;;
  esac
done
set -- "${POSITIONAL[@]+"${POSITIONAL[@]}"}"

EMAIL="${1:-}"
ROLE="${2:-user}"
EXPIRES="${3:-}"

if [[ -z "$EMAIL" ]]; then
  usage
  exit 1
fi
if [[ "$ROLE" != "user" && "$ROLE" != "admin" ]]; then
  echo "Error: role must be 'user' or 'admin' (got '$ROLE')" >&2
  exit 1
fi
# Shape-check ``expires_in_hours`` in bash so a typo or a float like
# ``1.5`` produces a friendly CLI error instead of a Python traceback
# from ``int(sys.argv[3])`` inside the JSON-body helper below. Server-
# side range enforcement (1..720) still applies via the
# ``/api/v1/invitations`` validator — admins who pass an out-of-range
# integer get a clean 400 with the bound; this guard only catches the
# "not even an integer" case.
if [[ -n "$EXPIRES" && ! "$EXPIRES" =~ ^[0-9]+$ ]]; then
  echo "Error: expires_in_hours must be a non-negative integer (got '$EXPIRES')" >&2
  exit 1
fi

# Read a key from $ENV_FILE unless it is already set in the environment.
#
# Strips surrounding single/double quotes on quoted values, and strips an
# inline ``<space>#`` comment on unquoted values (mirrors how python-dotenv /
# pydantic-settings parse ``.env`` files).  A bare ``#`` mid-value without a
# preceding space is preserved as data — passwords containing ``#`` are not
# truncated.  An unbalanced opening quote returns the literal post-key=
# slice (no silent stripping that could swallow real password material).
read_env() {
  local key="$1" line="" value="" rest=""
  if [[ -n "${!key:-}" ]]; then
    printf '%s' "${!key}"
    return 0
  fi
  [[ -f "$ENV_FILE" ]] || return 0
  # `|| true` so a no-match grep doesn't trip `set -o pipefail`.
  line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null | tail -n1 || true)"
  [[ -n "$line" ]] || return 0
  # Drop leading whitespace then the ``key=`` prefix.
  value="${line#"${line%%[![:space:]]*}"}"
  value="${value#${key}=}"
  if [[ "$value" == \"* ]]; then
    rest="${value#\"}"
    if [[ "$rest" == *\"* ]]; then
      value="${rest%%\"*}"
    fi
  elif [[ "$value" == \'* ]]; then
    rest="${value#\'}"
    if [[ "$rest" == *\'* ]]; then
      value="${rest%%\'*}"
    fi
  else
    # Strip an inline ``<space>#`` comment.  ``${var%% *}`` would also drop
    # trailing whitespace on a comment-free value, so prefer the explicit
    # space-hash delimiter instead and trim trailing whitespace separately.
    if [[ "$value" == *" #"* ]]; then
      value="${value%% #*}"
    fi
    value="${value%"${value##*[![:space:]]}"}"
  fi
  printf '%s' "$value"
}

API_URL="$(read_env API_URL)"; API_URL="${API_URL:-http://localhost:8100}"
API_URL="${API_URL%/}"
API_KEY="$(read_env API_KEY)"

command -v curl >/dev/null  || { echo "Error: curl is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "Error: python3 is required" >&2; exit 1; }

# --- Obtain a bearer token ---------------------------------------------------
# Secrets handling principle for the rest of the script:
#   * Never let a credential reach python3 / curl as a command-line argument
#     (``ps -ef`` would surface it for the duration of the syscall).
#   * Pass credentials to python via the environment (``env -i`` controlled
#     scope) and then pipe the JSON body to curl over stdin via
#     ``--data-binary @-``.
#   * Keep the bearer token out of curl's argv as well: write the headers
#     into a ``mktemp``-created config file (``chmod 600``) consumed via
#     ``curl --config <file>`` and remove it on exit.
if [[ -n "$API_KEY" ]]; then
  BEARER="$API_KEY"
else
  ADMIN_EMAIL="$(read_env ADMIN_EMAIL)"
  ADMIN_PASSWORD="$(read_env ADMIN_PASSWORD)"
  if [[ -z "$ADMIN_EMAIL" || -z "$ADMIN_PASSWORD" ]]; then
    echo "Error: ADMIN_EMAIL and ADMIN_PASSWORD must be set (in $ENV_FILE or the environment)," >&2
    echo "       or provide API_KEY instead." >&2
    exit 1
  fi
  # Build the JSON body without ever passing the password as argv. The
  # python helper reads the credentials from the environment and writes the
  # body to stdout, which is then piped to curl on stdin.
  login_resp="$(
    ADMIN_EMAIL="$ADMIN_EMAIL" ADMIN_PASSWORD="$ADMIN_PASSWORD" python3 -c '
import json, os, sys
sys.stdout.write(json.dumps({
    "email": os.environ["ADMIN_EMAIL"],
    "password": os.environ["ADMIN_PASSWORD"],
}))
' | curl -s -w $'\n%{http_code}' -X POST "$API_URL/api/v1/auth/login" \
        -H 'Content-Type: application/json' --data-binary @-
  )"
  login_code="$(printf '%s' "$login_resp" | tail -n1)"
  login_json="$(printf '%s' "$login_resp" | sed '$d')"
  if [[ "$login_code" != "200" ]]; then
    echo "Error: login failed (HTTP $login_code): $login_json" >&2
    exit 1
  fi
  # An account with two-factor enabled gets a 200 carrying
  # ``mfa_required`` and a NULL ``access_token`` — the session is only issued
  # after a code is verified. There is nowhere to prompt for one here, so fail
  # with the fix rather than sending "Bearer None" and surfacing a baffling 401
  # three steps later.
  if printf '%s' "$login_json" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("mfa_required") else 1)'; then
    echo "Error: '$ADMIN_EMAIL' has two-factor authentication enabled, which this script cannot complete." >&2
    echo "       Set API_KEY to an 'fcp-' API key for that account instead (Profile -> API Keys)." >&2
    exit 1
  fi
  BEARER="$(printf '%s' "$login_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
fi

# --- Create the invitation ---------------------------------------------------
# The non-secret request fields (email / role / expires / send_email flag)
# are still passed via argv to keep the python helper readable — none of
# them are credentials.
invite_body="$(python3 -c '
import json, sys
body = {"email": sys.argv[1], "role": sys.argv[2], "send_email": sys.argv[4] == "true"}
if sys.argv[3]:
    body["expires_in_hours"] = int(sys.argv[3])
print(json.dumps(body))
' "$EMAIL" "$ROLE" "$EXPIRES" "$SEND_EMAIL")"

# Write the Authorization header into a chmod-600 curl config file so the
# bearer never appears in this curl's argv (``-H "Authorization: Bearer
# $BEARER"`` would have leaked it via ``ps -ef``).
header_cfg="$(mktemp)"
TMP_FILES+=("$header_cfg")
chmod 600 "$header_cfg"
{
  printf 'header = "Authorization: Bearer %s"\n' "$BEARER"
  printf 'header = "Content-Type: application/json"\n'
} > "$header_cfg"

resp="$(printf '%s' "$invite_body" | curl -s -w $'\n%{http_code}' -X POST "$API_URL/api/v1/invitations" \
  --config "$header_cfg" \
  --data-binary @-)"
code="$(printf '%s' "$resp" | tail -n1)"
json="$(printf '%s' "$resp" | sed '$d')"

if [[ "$code" != "201" ]]; then
  echo "Error: creating invitation failed (HTTP $code): $json" >&2
  exit 1
fi

# --- Output ------------------------------------------------------------------
printf '%s' "$json" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("Invitation created:")
print("  email:      %s" % d["email"])
print("  role:       %s" % d["role"])
print("  expires_at: %s" % d["expires_at"])
link = d.get("link")
if link:
    print("  link:       %s" % link)
    print()
    print(link)
elif d.get("email_sent"):
    print("  email_sent: yes (link emailed to %s)" % d["email"])
else:
    print("  email_sent: no")
    print()
    # The invitation was created server-side (HTTP 201 reached this branch)
    # but the SMTP delivery failed AFTER the token was written to Redis
    # (auth error, refused message, network blip while talking to the SMTP
    # server, etc.).  When SMTP is *unset* the server short-circuits with a
    # 503 before writing the token, so this branch never fires in that case.
    #
    # The pending invitation now exists in Redis and the raw token is gone
    # (only returned once at creation time).  A naive retry will 409 because
    # the exists-check on the server finds the still-pending invite —
    # recovery requires revoking the pending invite first, then re-issuing.
    print(
        "Warning: the invitation was created server-side but the SMTP exchange"
    )
    print(
        "         refused the message (auth / network / refused recipient)."
    )
    print(
        "         The link is NOT printable here — the raw token is only"
    )
    print(
        "         returned once at creation."
    )
    print()
    print(
        "Recovery:"
    )
    print(
        "  1. Revoke the pending invitation (admin UI, or"
    )
    print(
        "     DELETE /api/v1/invitations/{id} with id=%s)." % d["id"]
    )
    print(
        "  2. Fix the SMTP failure (or run again without --send-email to"
    )
    print(
        "     return the link directly), then re-issue the invitation."
    )
    sys.exit(1)
'
