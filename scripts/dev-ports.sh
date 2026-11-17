#!/usr/bin/env bash
# Check that the host ports the dev stack is about to publish are free, before
# `make up` / `make start` hand over to compose.
#
# Usage: scripts/dev-ports.sh <engine> "<services>" <compose flags...>
#   <services>  the services to check, space-separated; empty checks them all
#
# The ports come from `compose config`, so .env, the DEV_*_PORT settings and
# the enabled profiles are taken into account. Ports already published by this
# checkout's own compose project are not a conflict. On a conflict it names the
# container holding the port, prints a free set of DEV_*_PORT settings for
# .env, and exits 1.

set -euo pipefail

engine="$1"
services="$2"
shift 2

# stderr stays apart: podman compose writes its provider banner there.
errors="$(mktemp)"
trap 'rm -f "$errors"' EXIT
if ! config="$("$engine" compose "$@" config 2>"$errors")"; then
  cat "$errors" >&2
  exit 1
fi

project="$(awk '/^name:/ { print $2; exit }' <<<"$config")"

# One "<service> <target> <host ip> <published>" line per published port.
# Reads both the long form `docker compose config` prints and the short
# "ip:published:target" form of podman-compose.
published="$(awk '
  /^services:/ { in_services = 1; next }
  /^[^ ]/ { in_services = 0 }
  !in_services { next }
  /^  [A-Za-z0-9_.-]+:$/ { service = $1; sub(/:$/, "", service); host_ip = ""; next }
  /^ +- mode:/ { host_ip = "" }
  /^ +host_ip:/ { host_ip = $2 }
  /^ +target:/ { target = $2 }
  /^ +published:/ { gsub(/"/, "", $2); print service, target, (host_ip == "" ? "-" : host_ip), $2 }
  /^ +- "?([0-9.]+:)?[0-9]+:[0-9]+/ {
    entry = $2; gsub(/"/, "", entry); sub(/\/.*/, "", entry)
    n = split(entry, part, ":")
    print service, part[n], (n == 3 ? part[1] : "-"), part[n - 1]
  }
' <<<"$config")"

checked="$published"
if [[ -n "$services" ]]; then
  checked="$(awk -v keep=" $services " 'index(keep, " " $1 " ")' <<<"$published")"
fi

# What this checkout's stack already publishes, e.g. "127.0.0.1:6380->6379/tcp".
own_ports=""
if [[ -n "$project" ]]; then
  own_ports="$("$engine" ps --filter "label=com.docker.compose.project=$project" --format '{{.Ports}}' 2>/dev/null || true)"
fi

# The .env setting that moves each port (docker-compose.dev.yml).
setting_for() {
  case "$1:$2" in
    frontend:3100) echo DEV_FRONTEND_PORT ;;
    platform:8100) echo DEV_PLATFORM_PORT ;;
    postgres:5432) echo DEV_POSTGRES_PORT ;;
    redis:6379) echo DEV_REDIS_PORT ;;
    minio:9000) echo DEV_SILO_PORT ;;
    minio:9001) echo DEV_SILO_CONSOLE_PORT ;;
    redisinsight:5540) echo DEV_REDISINSIGHT_PORT ;;
  esac
}

# Something accepts connections on the port.
port_in_use() {
  local host="$1"
  case "$host" in -|0.0.0.0|::|"[::]") host=127.0.0.1 ;; esac
  (exec 3<>"/dev/tcp/$host/$2") 2>/dev/null
}

conflicts=0
while read -r service target host_ip port; do
  [[ -z "$port" ]] && continue
  grep -q ":$port->" <<<"$own_ports" && continue
  port_in_use "$host_ip" "$port" || continue
  conflicts=1
  holder="$("$engine" ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | awk -v p=":$port->" 'index($0, p) { print $1; exit }' || true)"
  setting="$(setting_for "$service" "$target")"
  echo "Port $port ($service${setting:+, $setting}) is in use${holder:+ by container $holder}." >&2
done <<<"$checked"

[[ $conflicts -eq 0 ]] && exit 0

# A free set for the whole stack: every port shifted by the same offset.
for offset in 10 20 30 40 50 60 70 80 90 100; do
  free=1
  while read -r service target host_ip port; do
    [[ -z "$port" ]] && continue
    if port_in_use "$host_ip" $((port + offset)); then
      free=0
      break
    fi
  done <<<"$published"
  [[ $free -eq 1 ]] || continue
  echo "" >&2
  echo "Free ports for this checkout; add them to its .env:" >&2
  while read -r service target host_ip port; do
    setting="$(setting_for "$service" "$target")"
    [[ -n "$setting" ]] && echo "  $setting=$((port + offset))" >&2
  done <<<"$published"
  break
done
exit 1
