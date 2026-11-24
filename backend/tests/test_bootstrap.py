"""Bootstrap smoke test – verifies the platform starts cleanly on a fresh database.

This test starts the FastAPI application via uvicorn in a subprocess, waits for
it to become healthy, and then checks:

1. The health endpoint returns 200 with {"status": "ok"}.
2. The auth config endpoint returns 200 (proves routes are mounted).
3. Login with the default admin credentials succeeds (proves admin user was created,
   migrations ran, and the auth system works end-to-end).

If any of these fail, it means something is broken in the startup sequence:
database migrations, template seeding, admin user creation, or route registration.

Usage:
    uv run python -m pytest tests/test_bootstrap.py -v

Requires:
    - PostgreSQL 17 + pgvector running (defaults to the docker-compose.dev.yml stack)
    - Redis running (defaults to the docker-compose.dev.yml stack)
    - Silo (S3 object storage) running (defaults to the docker-compose.dev.yml stack)
"""

import os
import signal
import subprocess
import sys
import threading
import time

import httpx
import pytest

# One backend on a fixed port per run: under pytest-xdist every worker that
# gets a test of this module would start its own server on 8099, and all but
# one would fail to bind. The group keeps the module on one worker, which
# requires `--dist loadgroup` (CI and scripts/ci-local.sh pass it).
pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("bootstrap_server")]

BASE_URL = "http://127.0.0.1:8099"
STARTUP_TIMEOUT = 60  # seconds to wait for the server to become healthy
POLL_INTERVAL = 1  # seconds between health polls
DEFAULT_APP_VERSION = "0.0.0-dev"
#: First line faulthandler writes when SIGABRT stops a stalled server.
FAULTHANDLER_MARKER = "Fatal Python error: Aborted"
#: Seconds a stalled server gets to write its thread stacks after SIGABRT.
STACK_DUMP_GRACE = 10


def _stop_with_stacks(proc: subprocess.Popen) -> None:
    """Stop a server that never became ready so that it says where it stalled.

    The server runs with ``PYTHONFAULTHANDLER=1``: SIGABRT makes faulthandler
    write every thread's stack to its output (the pipe the fixture drains)
    before the process dies. A plain SIGTERM would let uvicorn shut down, or
    leave a process stuck in an import with nothing said, and the failure would
    name no cause. SIGKILL only when even the abort does not end it.
    """
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGABRT)
    try:
        proc.wait(timeout=STACK_DUMP_GRACE)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _startup_timeout_message(bound: bool, last_status: int | None, last_body: str, output: str) -> str:
    """The failure of a server that did not become ready, naming the phase it stopped in.

    Never bound (every poll refused) means the process stalled before uvicorn
    listened: an import or the lifespan's synchronous start. Bound but not
    ready means the background startup (migrations, seeding) did not finish;
    the last readiness answer says which step it was on.
    """
    if not bound:
        phase = (
            f"the server never accepted a connection on {BASE_URL} "
            "(stalled before uvicorn listened: an import or the lifespan start)"
        )
    else:
        phase = f"the server listened but /api/health/ready last answered {last_status}: {last_body[:500]}"
    # faulthandler's dump after SIGABRT runs to several thousand characters:
    # a plain tail of the output keeps only the stacks and loses the server
    # log that names the startup step it was on. Keep the end of each.
    log, marker, stacks = output.partition(FAULTHANDLER_MARKER)
    shown = f"{log[-4000:]}\n{marker}{stacks[:4000]}" if marker else output[-6000:]
    return (
        f"Backend did not become healthy within {STARTUP_TIMEOUT}s: {phase}.\n"
        f"Output (server log, then the thread stacks at the stop):\n{shown}"
    )


@pytest.fixture(scope="module")
def backend_server():
    """Start the backend in a subprocess and wait for it to become healthy."""
    env = {
        **os.environ,
        "DATABASE_URL": os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://copilot:copilot_dev@localhost:5432/copilot",
        ),
        "REDIS_URL": os.environ.get("REDIS_URL", "redis://localhost:6380"),
        "S3_ENDPOINT": os.environ.get("S3_ENDPOINT", "localhost:9002"),
        "SECRET_KEY": "test-bootstrap-secret-key-not-for-production",
        "ADMIN_EMAIL": "admin@test.filigran.io",
        "ADMIN_PASSWORD": "test-admin-password-123",
        "DEBUG": "false",
        # Pinned, not inherited: `test_auth_config_endpoint` asserts the
        # fail-closed default of `enabled_features == []`. Without this the
        # assertion really reads "no developer has a feature flag in their
        # environment", and any flag in a local `.env` turns the suite red.
        "ENABLED_FEATURES": "",
        "PLATFORM_MODE": "xtm_one",
        "APP_VERSION": os.environ.get("APP_VERSION", DEFAULT_APP_VERSION),
        # A server that never becomes ready is stopped with SIGABRT
        # (`_stop_with_stacks`): faulthandler then writes where it stalled.
        "PYTHONFAULTHANDLER": "1",
    }

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:application",
            "--host",
            "127.0.0.1",
            "--port",
            "8099",
            "--log-level",
            "info",
        ],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Test fixture launches a fixed trusted uvicorn argv without a shell.

    # Continuously drain the server's stdout on a background thread. The server
    # logs to stdout with a *blocking* StreamHandler (app/core/logging.py); if
    # nobody reads the pipe, a fresh-DB boot plus per-request access logging
    # eventually fills the ~64KB OS pipe buffer, the blocking write() freezes
    # the event loop, and every request then times out. Draining keeps the
    # buffer empty so the server never stalls, and preserves the captured output
    # for the failure messages below.
    output_chunks: list[bytes] = []

    def _drain_stdout() -> None:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, b""):
            output_chunks.append(line)

    drain_thread = threading.Thread(target=_drain_stdout, daemon=True)
    drain_thread.start()

    def _captured_output() -> str:
        return b"".join(output_chunks).decode(errors="replace")

    # Poll the READINESS endpoint until the server is fully started.  Since
    # the startup-gate fix the liveness endpoint /api/health
    # returns 200 as soon as uvicorn accepts connections — while migrations
    # and seeding still run in a background task and every non-health route
    # answers 503.  Readiness flips to 200 only once that background startup
    # has completed, which is the state these bootstrap tests need.
    healthy = False
    bound = False
    last_status: int | None = None
    last_body = ""
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{BASE_URL}/api/health/ready", timeout=2)
            bound = True
            last_status, last_body = resp.status_code, resp.text
            if resp.status_code == 200:
                healthy = True
                break
        except httpx.ConnectError:
            pass  # server not listening yet; retry
        except httpx.ReadTimeout:
            bound = True  # listening, the answer is late; retry

        # Check if process died
        if proc.poll() is not None:
            drain_thread.join(timeout=5)
            pytest.fail(
                f"Backend process exited with code {proc.returncode} before becoming healthy.\n"
                f"Output:\n{_captured_output()[-3000:]}"
            )

        time.sleep(POLL_INTERVAL)

    if not healthy:
        _stop_with_stacks(proc)
        drain_thread.join(timeout=5)
        pytest.fail(_startup_timeout_message(bound, last_status, last_body, _captured_output()))

    yield proc

    # Teardown: stop the server
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    drain_thread.join(timeout=5)


def test_health_endpoint(backend_server):
    """Health endpoint returns 200 with status ok without exposing version."""
    resp = httpx.get(f"{BASE_URL}/api/health", timeout=5)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" not in data


def test_ready_health_endpoint_does_not_expose_version(backend_server):
    """Readiness endpoint does not expose runtime version publicly."""
    resp = httpx.get(f"{BASE_URL}/api/health/ready", timeout=5)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" not in data


def test_auth_config_endpoint(backend_server):
    """Auth config endpoint is reachable (routes are mounted correctly)."""
    resp = httpx.get(f"{BASE_URL}/api/v1/auth/config", timeout=5)
    assert resp.status_code == 200
    data = resp.json()
    assert "registration_enabled" in data or isinstance(data, dict)
    # Feature flags default to an empty list (fail-closed) when
    # ENABLED_FEATURES is unset — see app/core/feature_flags.py.
    assert data["enabled_features"] == []


def test_admin_login(backend_server):
    """Admin user was created and can log in."""
    resp = httpx.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={
            "email": "admin@test.filigran.io",
            "password": "test-admin-password-123",
        },
        timeout=5,
    )
    assert resp.status_code == 200, f"Admin login failed: {resp.text}"
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_agents_requires_auth(backend_server):
    """Protected endpoints return 401 without a token."""
    resp = httpx.get(f"{BASE_URL}/api/v1/agents", timeout=5)
    assert resp.status_code in (401, 403)


def test_agents_list_with_auth(backend_server):
    """Authenticated request to agents list returns 200."""
    # Login first
    login_resp = httpx.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={
            "email": "admin@test.filigran.io",
            "password": "test-admin-password-123",
        },
        timeout=5,
    )
    token = login_resp.json()["access_token"]

    resp = httpx.get(
        f"{BASE_URL}/api/v1/agents",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_templates_seeded(backend_server):
    """Agent templates endpoint works; xtm_one mode seeds exactly the CTI templates.

    The general-purpose templates ship in Copilot mode only; XTM One mode ships
    the CTI pack, whose default skills the boot seeded too, so every
    one of them resolves for the admin.
    """
    from app.agents.cti_templates import CTI_TEMPLATES

    # Login
    login_resp = httpx.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={
            "email": "admin@test.filigran.io",
            "password": "test-admin-password-123",
        },
        timeout=5,
    )
    token = login_resp.json()["access_token"]

    resp = httpx.get(
        f"{BASE_URL}/api/v1/agents/templates",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    assert resp.status_code == 200
    templates = resp.json()
    assert isinstance(templates, list)
    assert sorted(t["slug"] for t in templates) == sorted(
        t["slug"] for t in CTI_TEMPLATES
    ), f"Expected exactly the CTI templates in xtm_one mode, got {[t['slug'] for t in templates]}"
    for template in templates:
        assert template["default_skills"], template["slug"]
        assert len(template["default_skill_ids"]) == len(template["default_skills"]), (
            template["slug"],
            template["default_skills"],
            template["default_skill_ids"],
        )
