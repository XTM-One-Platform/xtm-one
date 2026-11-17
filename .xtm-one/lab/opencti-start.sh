#!/bin/sh
# Starts the lab's OpenCTI ([service.lab-opencti] in .xtm-one/checks.toml) on the OpenCTI image.
# A product image gets no harness port wait (labs.py service_script) and OpenCTI checks its
# dependencies once then exits, so this script waits for them first:
# - the port of each depends_on service (search 9200, broker 5672, Redis 6380, S3 9002);
# - the search engine's cluster at yellow: a fresh OpenSearch answers on 9200 before it can create
#   indices, and OpenCTI's first-start initialisation ("[INIT] New platform detected") then exits
#   with code 1 and no error line;
# - a write to Redis: Redis accepts connections while it still loads, and the lock client fails.
# OpenCTI 7.x refuses to initialise the admin without app:encryption_key (32 bytes, base64): one is
# generated per lab start, so no key is ever written in the repository.
# Node is not exec'd: its exit code is logged ("lab-opencti: node exited with code N") with
# --trace-uncaught, so a failed start names its cause in sandbox_service_logs.
set -u

wait_for() {
  label="$1"
  shift
  n=0
  until node -e "$@" >/dev/null 2>&1; do
    n=$((n + 1))
    if [ $((n % 15)) -eq 0 ]; then echo "lab-opencti: still waiting for $label" >&2; fi
    sleep 2
  done
  echo "lab-opencti: $label ready" >&2
}

for p in 9200 5672 6380 9002; do
  wait_for "port $p" 'require("net").connect(+process.argv[1], "127.0.0.1").on("connect", () => process.exit(0)).on("error", () => process.exit(1))' "$p"
done

wait_for "search cluster yellow" 'require("http").get("http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=10s", (r) => process.exit(r.statusCode === 200 ? 0 : 1)).on("error", () => process.exit(1))'

wait_for "redis write" '
const s = require("net").connect(6380, "127.0.0.1");
s.on("error", () => process.exit(1));
s.on("data", (d) => process.exit(d.toString().startsWith("+OK") ? 0 : 1));
s.write("*3\r\n$3\r\nSET\r\n$14\r\nlab-opencti:up\r\n$1\r\n1\r\n");
setTimeout(() => process.exit(1), 3000);
'

cd /opt/opencti || exit 1
APP__ENCRYPTION_KEY="$(node -e 'process.stdout.write(require("crypto").randomBytes(32).toString("base64"))')"
export APP__ENCRYPTION_KEY
# OpenCTI 7.x ships build/back.mjs, 6.x build/back.js.
if [ -f build/back.mjs ]; then entry=build/back.mjs; else entry=build/back.js; fi
node --trace-uncaught "$entry"
code=$?
echo "lab-opencti: node exited with code $code" >&2
exit "$code"
