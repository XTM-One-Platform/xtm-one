#!/bin/sh
# -----------------------------------------------------------------------------
# Uvicorn launcher for the unified platform image (root Dockerfile).
# Centralises:
#
#   * The non-negative-integer validation helper (`_pi`), so a misconfigured
#     env var such as ``WS_PING_INTERVAL=30s`` produces a visible
#     ``WARNING:`` on stderr and falls back to the documented default
#     instead of silently being passed to uvicorn (which would crash on
#     start with an obscure ``invalid int value`` error).
#
#   * The exact list of tunables — ``WEB_WORKERS``, ``MAX_CONCURRENCY``,
#     ``WS_PING_INTERVAL``, ``WS_PING_TIMEOUT``, ``MAX_REQUESTS``,
#     ``MAX_REQUESTS_JITTER``, ``MAX_REQUESTS_DEFER_SECONDS``,
#     ``GRACEFUL_TIMEOUT`` — and the uvicorn flags or app variables they map
#     to.
#
# The listening port is the first positional argument (defaults to 4000).
#
# This must remain POSIX-shell compatible: the base image is
# ``python:3.14-slim``, whose ``/bin/sh`` is dash (no bashisms).
# -----------------------------------------------------------------------------
set -eu

_pi() {
    n=$1
    v=$2
    d=$3
    case "$v" in
        ''|*[!0-9]*)
            if [ -n "$v" ]; then
                echo "WARNING: env $n='$v' is not a non-negative integer, falling back to $d" >&2
            fi
            printf '%s' "$d"
            ;;
        *)
            printf '%s' "$v"
            ;;
    esac
}

PORT=${1:-4000}

# Default to 2 workers (instead of 1) so a single wedged event loop cannot
# make the whole pod fail liveness/readiness probes at once. Keep in sync
# with ``web_workers`` in ``backend/app/core/config.py`` (pool-overcommit
# estimator) and the ``WEB_WORKERS`` default in ``docker-compose.yml``.
WW=$(_pi WEB_WORKERS "${WEB_WORKERS:-2}" 2)
# Uvicorn's --limit-concurrency is a protocol-level *backstop*: once hit it
# replies 503 to EVERY request (health probes included), which failed the
# liveness/readiness probes during a burst. The real, health-
# exempt admission control with 429 backpressure now lives in-app
# (ConcurrencyLimitMiddleware, APP_MAX_CONCURRENCY, default 200). This backstop
# is kept well ABOVE that app limit so the in-app limiter sheds load first and
# the blunt 503 path is effectively never reached.
MC=$(_pi MAX_CONCURRENCY "${MAX_CONCURRENCY:-1000}" 1000)
WS_PI=$(_pi WS_PING_INTERVAL "${WS_PING_INTERVAL:-30}" 30)
# Default raised from 30s to 120s to match .env.sample's documented value.
# Chat WebSockets stream long-running LLM responses that can block the event
# loop for tens of seconds at a time (multi-step agent loops, large
# attachments, slow providers).  A 30s timeout was firing routinely on
# healthy connections and producing dozens of CloseCode 1011 / "keepalive
# ping timeout" errors per pod per hour. 120s is permissive enough to
# absorb agent loops while still detecting truly dead sockets.
WS_PT=$(_pi WS_PING_TIMEOUT "${WS_PING_TIMEOUT:-120}" 120)
MR=$(_pi MAX_REQUESTS "${MAX_REQUESTS:-10000}" 10000)
# Bounded graceful-shutdown drain (--timeout-graceful-shutdown). Uvicorn's
# native default is None = wait FOREVER for open connections and handler
# tasks to finish. That default is what turned MAX_REQUESTS worker recycling
# into full-pod outages in production: a worker that reaches
# its recycle limit stops accepting new connections, then waits on long-lived SSE
# streams (/api/v1/activity/stream never completes on its own) — it keeps
# ponging the multiprocess supervisor's healthcheck pipe, so it is never
# replaced, and once every sibling worker reaches its own limit (they start
# together and share the accept load evenly) NOTHING accepts on the port,
# liveness/readiness probes time out, and the kubelet restarts the pod.
# 120s lets normal in-flight requests finish while guaranteeing the drain
# always ends well before a sibling can also wedge; SSE clients
# (EventSource) reconnect automatically to a live worker.
GT=$(_pi GRACEFUL_TIMEOUT "${GRACEFUL_TIMEOUT:-120}" 120)
# The app reads the drain it is given to say how long the turns it is asked
# to stop with have left (app/core/worker_shutdown.py).
XTM_ONE_GRACEFUL_TIMEOUT=$GT
export XTM_ONE_GRACEFUL_TIMEOUT

set -- \
    --host 0.0.0.0 \
    --port "$PORT" \
    --workers "$WW" \
    --timeout-keep-alive 65 \
    --timeout-graceful-shutdown "$GT" \
    --ws-ping-interval "$WS_PI" \
    --ws-ping-timeout "$WS_PT" \
    --limit-concurrency "$MC"

# The MAX_REQUESTS recycle is the app's, not uvicorn's: uvicorn's
# ``--limit-max-requests`` stops a worker the moment it reaches its quota and
# cuts every chat turn running in it once GRACEFUL_TIMEOUT has passed, while
# the pod stays up. The app recycles at the first moment no agent turn runs in
# the worker, at most MAX_REQUESTS_DEFER_SECONDS after its quota, by sending
# itself SIGTERM - uvicorn's own shutdown, then the supervisor's replacement
# (app/core/worker_recycle.py). ``MAX_REQUESTS=0`` = never recycle: nothing is
# handed over.
if [ "$MR" -gt 0 ]; then
    # Per-worker recycle jitter: each worker adds random(0..jitter) to its
    # quota, so workers that booted together and count requests at the same
    # rate do NOT all reach it within the same drain window. Without it, every
    # worker recycles near-simultaneously and the pod stops accepting
    # connections entirely for the duration of the drain. Default: half the
    # recycle limit.
    MRJ=$(_pi MAX_REQUESTS_JITTER "${MAX_REQUESTS_JITTER:-$((MR / 2))}" "$((MR / 2))")
    MRD=$(_pi MAX_REQUESTS_DEFER_SECONDS "${MAX_REQUESTS_DEFER_SECONDS:-14400}" 14400)
    XTM_ONE_RECYCLE_REQUESTS=$MR
    XTM_ONE_RECYCLE_JITTER=$MRJ
    XTM_ONE_RECYCLE_DEFER_SECONDS=$MRD
    export XTM_ONE_RECYCLE_REQUESTS XTM_ONE_RECYCLE_JITTER XTM_ONE_RECYCLE_DEFER_SECONDS
else
    unset XTM_ONE_RECYCLE_REQUESTS XTM_ONE_RECYCLE_JITTER XTM_ONE_RECYCLE_DEFER_SECONDS
fi

# One id per launch, inherited by every worker the supervisor starts - the
# replacement of a worker recycled after MAX_REQUESTS included - so that one
# skips the migrations and the seeding its launch already applied instead of
# answering 503 for their whole length (app/core/launch_marker.py). Always
# fresh: a restarted container is a new launch, whatever its environment says.
XTM_ONE_LAUNCH_ID=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
export XTM_ONE_LAUNCH_ID

if [ -n "${FORWARDED_ALLOW_IPS:-}" ]; then
    # Which peers may speak for the client. Uvicorn trusts only 127.0.0.1 by
    # default, so a proxy in another pod is ignored and the app sees http://
    # from the proxy's address. Naming it corrects scope["scheme"] and
    # scope["client"] for every reader at once.
    set -- "$@" --forwarded-allow-ips "$FORWARDED_ALLOW_IPS"
fi

exec uvicorn app.main:application "$@"
