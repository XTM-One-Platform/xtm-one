#!/usr/bin/env bash
# The relay end-to-end run: a platform on one network, a Splunk
# stand-in on another the platform cannot reach, and a relay between them
# (docker-compose.relay-e2e.yml). relay_e2e.py makes the checks.
#
#   apps/relay/e2e/run.sh                   # images already built and tagged
#   RELAY_E2E_BUILD=1 apps/relay/e2e/run.sh # build platform + relay first
#   RELAY_E2E_KEEP=1 ...                    # leave the stack up afterwards
#
# Needs docker compose, openssl and python3 on the host.
set -euo pipefail

cd "$(dirname "$0")/../../.."

# docker-compose.yml reads these from the shell; a developer's point at
# services on the host, which the containers cannot reach.
unset REDIS_URL SECRET_KEY

certs="${RELAY_E2E_CERTS:-$(mktemp -d)}"
export RELAY_E2E_CERTS="$certs"
compose=(docker compose -f docker-compose.yml -f docker-compose.relay-e2e.yml)
export RELAY_E2E_COMPOSE="${compose[*]}"

# An internal CA only the relay trusts, and the service's certificate.
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -sha256 \
  -subj "/CN=XTM One relay e2e CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout "$certs/ca-key.pem" -out "$certs/ca.pem" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -sha256 -subj "/CN=onprem-splunk" \
  -keyout "$certs/server-key.pem" -out "$certs/server.csr" 2>/dev/null
printf '%s\n' \
  "subjectAltName=DNS:onprem-splunk" \
  "basicConstraints=CA:FALSE" \
  "keyUsage=critical,digitalSignature,keyEncipherment" \
  "extendedKeyUsage=serverAuth" > "$certs/server.ext"
openssl x509 -req -sha256 -days 2 -in "$certs/server.csr" \
  -CA "$certs/ca.pem" -CAkey "$certs/ca-key.pem" -CAcreateserial \
  -extfile "$certs/server.ext" -out "$certs/server.pem" 2>/dev/null
# The relay runs as uid 65532 and reads the CA through a bind mount.
chmod 755 "$certs"
chmod 644 "$certs"/*.pem

finish() {
  status=$?
  if [ "$status" -ne 0 ]; then
    "${compose[@]}" logs --no-color --tail 200 platform relay relay-net onprem-splunk || true
  fi
  if [ "${RELAY_E2E_KEEP:-0}" != "1" ]; then
    "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap finish EXIT

if [ "${RELAY_E2E_BUILD:-0}" = "1" ]; then
  "${compose[@]}" build platform relay
fi

"${compose[@]}" up -d --no-build minio postgres redis platform onprem-splunk relay-net
python3 apps/relay/e2e/relay_e2e.py
