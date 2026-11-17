"""The relay end-to-end checks, driven by run.sh against the stack of
docker-compose.relay-e2e.yml.

What it proves, in the order a customer would meet it:

1. a relay enrolled with a key from the platform connects out and is online;
2. a request from the platform reaches a service on a network the platform has
   no route to, through the relay, over TLS from an internal CA only the relay
   trusts;
3. the relay's own allow-list refuses anything else;
4. a Splunk integration routed through the group passes its connectivity test
   and runs a tool through the relay, its token reaching the service;
5. the same integration NOT routed fails: the platform cannot reach the service;
6. every relayed request is in the group's request log.

Integrations are an Enterprise Edition capability: on a stack started without
ENTERPRISE_LICENSE, steps 4 and 5 are skipped and say so. Stdlib only. Exits
non-zero on the first failed check.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

PLATFORM = os.environ.get("RELAY_E2E_PLATFORM", "http://localhost:4000").rstrip("/")
COMPOSE = shlex.split(os.environ["RELAY_E2E_COMPOSE"])
EMAIL = os.environ.get("ADMIN_EMAIL", "admin@filigran.io")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me-in-production")
SPLUNK = "https://onprem-splunk:8089"


def api(method: str, path: str, body: Any = None, token: str | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{PLATFORM}{path}", data=data, method=method)  # noqa: S310 - fixed e2e URL
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=90) as response:  # noqa: S310 - fixed e2e URL
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw.decode(errors="replace")


def check(condition: bool, label: str, detail: Any = None) -> None:
    if condition:
        print(f"PASS  {label}", flush=True)
        return
    print(f"FAIL  {label}", flush=True)
    if detail is not None:
        print(f"      {json.dumps(detail, default=str)[:2000]}", flush=True)
    sys.exit(1)


def wait_for(label: str, probe, timeout: float, interval: float = 2.0) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        ok, last = probe()
        if ok:
            return last
        time.sleep(interval)
    check(False, label, last)


def compose(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*COMPOSE, *args],
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> None:
    def ready() -> tuple[bool, Any]:
        try:
            status, body = api("GET", "/api/health/ready")
        except OSError as exc:
            return False, str(exc)
        return status == 200, body

    wait_for("the platform is ready", ready, timeout=300, interval=3)

    status, body = api("POST", "/api/v1/auth/login", {"email": EMAIL, "password": PASSWORD})
    check(status == 200 and body.get("access_token"), "the admin signs in", body)
    token = body["access_token"]

    # A licensed platform gates every user until an admin accepts the Terms of Use.
    status, me = api("GET", "/api/v1/auth/me", token=token)
    terms = (me or {}).get("terms_of_use") or {}
    if terms.get("required") and not terms.get("accepted"):
        status, body = api("POST", "/api/v1/auth/terms/accept", {"version": terms["version"]}, token)
        check(status == 200, "the admin accepts the Terms of Use", body)
    status, config = api("GET", "/api/v1/platform/config", token=token)
    enterprise = isinstance(config, dict) and config.get("edition") == "enterprise"

    status, group = api("POST", "/api/v1/relays/groups", {"name": "E2E on-prem", "zone": "onprem"}, token)
    check(status == 201, "a relay group is created", group)
    gid = group["id"]

    status, key = api("POST", f"/api/v1/relays/groups/{gid}/enrollment-keys", {"name": "e2e"}, token)
    check(status == 201 and str(key.get("key", "")).startswith("rlenr-"), "an enrollment key is issued", key)

    started = compose("up", "-d", "--no-build", "relay", env={"RELAY_E2E_ENROLLMENT_KEY": key["key"]})
    check(started.returncode == 0, "the relay container starts", started.stderr)

    def online() -> tuple[bool, Any]:
        status, relays = api("GET", f"/api/v1/relays/groups/{gid}/relays", token=token)
        return status == 200 and any(r.get("online") for r in relays or []), relays

    relays = wait_for("the relay enrolls and comes online", online, timeout=180)
    relay = next(r for r in relays if r.get("online"))
    check(relay["declared_destinations"] == ["onprem-splunk:8089"], "the relay declares its own list", relay)

    unreachable = compose(
        "exec", "-T", "platform", "python", "-c",
        "import socket; socket.create_connection(('onprem-splunk', 8089), 3)",
    )
    check(unreachable.returncode != 0, "the platform has no route to the on-prem service", unreachable.stdout)

    status, result = api(
        "POST", f"/api/v1/relays/groups/{gid}/test", {"url": f"{SPLUNK}/services/server/info"}, token
    )
    # The mock answers 401 without a credential: reached, through the internal CA.
    check(status == 200 and result.get("ok") and result.get("status_code") == 401,
          "a request reaches the on-prem service through the relay", result)

    status, result = api("POST", f"/api/v1/relays/groups/{gid}/test", {"url": "https://onprem-splunk:9443/"}, token)
    check(status == 200 and not result.get("ok") and result.get("error_code") == "destination_denied",
          "the relay refuses a destination outside its own list", result)

    if enterprise:
        check_routed_integration(gid, token)
    else:
        print("SKIP  the routed integration checks: Community Edition (set ENTERPRISE_LICENSE)", flush=True)

    def logged() -> tuple[bool, Any]:
        status, page = api("GET", f"/api/v1/relays/groups/{gid}/requests?limit=50", token=token)
        items = (page or {}).get("items", []) if isinstance(page, dict) else []
        expected = 5 if enterprise else 2
        return status == 200 and sum(1 for i in items if i.get("host") == "onprem-splunk") >= expected, page

    wait_for("the relayed requests are in the group's log", logged, timeout=30)
    print("relay e2e: every check passed", flush=True)


def check_routed_integration(gid: str, token: str) -> None:
    status, integration = api(
        "POST",
        "/api/v1/integrations",
        {
            "name": "E2E Splunk",
            "provider": "splunk",
            "auth_type": "api_key",
            "api_key": "e2e-splunk-token",
            "extra_config": {"base_url": SPLUNK},
            "relay_group_id": gid,
        },
        token,
    )
    check(status in (200, 201) and integration.get("relay_group_id") == gid,
          "a Splunk integration is routed through the group", integration)
    iid = integration["id"]

    status, result = api("POST", f"/api/v1/integrations/{iid}/test", None, token)
    check(status == 200 and result.get("status") == "ok", "its connectivity test passes through the relay", result)

    status, result = api(
        "POST", f"/api/v1/integrations/{iid}/test-tool", {"tool_name": "get_splunk_status", "arguments": {}}, token
    )
    check(status == 200 and "9.2.0" in json.dumps(result), "one of its tools runs through the relay", result)

    status, updated = api("PUT", f"/api/v1/integrations/{iid}", {"relay_group_id": None}, token)
    check(status == 200 and updated.get("relay_group_id") is None, "the integration is set back to direct", updated)
    status, result = api("POST", f"/api/v1/integrations/{iid}/test", None, token)
    check(status == 200 and result.get("status") != "ok", "direct from the platform, it cannot reach the service", result)


if __name__ == "__main__":
    main()
