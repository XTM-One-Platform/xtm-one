"""The OOTB routing eval lab: [service.lab-ootb-live] and its own OpenCTI (xtm-one #9646 slice 2b).

The lab is [service.lab-live] (a live model through the Copilot's LLM proxy) plus an OpenCTI in the same pod,
each product on an image key of AGENTIC_LABS_IMAGES, registered with the lab's backend through the real
``/platform/register`` path. These pin the declaration and the seed's two OpenCTI branches.
"""

from __future__ import annotations

import types

import pytest

from app.services.sandbox_workspace import checks
from tests.test_lab_seed_declarations import CHECKS, LAB, _lab_namespace, _lab_source, _services

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not LAB.is_file(), reason="no .xtm-one next to the backend")

OPENCTI_KEYS = {"opencti", "opencti-search", "opencti-broker", "opencti-redis", "opencti-s3"}
LAB_STORE_PORTS = {5432, 6379, 9000, 8100, 9099, 3100, 9098}
# [service.lab-opencti] runs this script: an argv item of checks.toml is capped at 512 characters (#10810).
OPENCTI_START = CHECKS.parent / "lab" / "opencti-start.sh"


def _opencti_start() -> str:
    assert _services()["lab-opencti"]["argv"] == ["sh", ".xtm-one/lab/opencti-start.sh"]
    return OPENCTI_START.read_text(encoding="utf-8")


def test_the_ootb_lab_is_the_live_lab_with_an_opencti_of_its_own():
    services = _services()
    live, ootb = services["lab-live"], services["lab-ootb-live"]
    assert ootb["argv"] == live["argv"] and ootb["image"] == live["image"] == "python-dev"
    assert ootb["depends_on"] == ["lab-opencti"]
    assert {
        k: v
        for k, v in ootb["env"].items()
        if k not in ("LAB_OPENCTI", "LAB_OPENCTI_IDENTITIES", "LAB_OPENCTI_PIRS", "LAB_WORKER")
    } == live["env"]
    # The worker runs beside the backend: its reconciler starts the Dashboard Creator's delegated tasks (#11023).
    assert ootb["env"]["LAB_WORKER"] == "1"
    # The real OpenCTI answers the searches: no fixture in front of it.
    assert ootb["env"]["LAB_OPENCTI"] == "live"
    assert ootb["env"]["LAB_OPENCTI_IDENTITIES"] == ootb["env"]["LAB_OPENCTI_PIRS"] == "0"


def test_every_opencti_dependency_runs_on_an_allowed_image_key_on_its_own_port():
    specs = checks.load_services(CHECKS.read_text(encoding="utf-8"))
    # One chain image for the OpenSandbox path: refused there, accepted on a lab with the five keys.
    with pytest.raises(checks.ChecksError, match="same image"):
        checks.service_chain(specs, "lab-ootb-live")
    chain = checks.service_chain(specs, "lab-ootb-live", own_images=OPENCTI_KEYS)
    assert chain[-1].name == "lab-ootb-live" and chain[-2].name == "lab-opencti"
    deps = chain[:-1]
    assert {spec.image for spec in deps} == OPENCTI_KEYS
    ports = [spec.port for spec in deps]
    assert len(set(ports)) == len(ports) and not set(ports) & LAB_STORE_PORTS


def test_the_lab_opencti_registers_with_the_lab_backend_using_the_lab_token():
    services = _services()
    env = services["lab-opencti"]["env"]
    namespace = _lab_namespace("LAB_REGISTRATION_TOKEN", "LIVE_OPENCTI_URL")
    assert env["XTM__XTM_ONE_TOKEN"] == namespace["LAB_REGISTRATION_TOKEN"]
    # The backend's port: ``LLM_PORT, API_PORT, WEB_PORT = 9099, 8100, 3100`` in lab.py.
    assert "LLM_PORT, API_PORT, WEB_PORT = 9099, 8100, 3100" in LAB.read_text(encoding="utf-8")
    assert env["XTM__XTM_ONE_URL"] == "http://localhost:8100"
    assert env["APP__BASE_URL"] == namespace["LIVE_OPENCTI_URL"]
    assert env["REDIS__PORT"] == str(services["lab-opencti-redis"]["port"])
    assert env["MINIO__PORT"] == str(services["lab-opencti-s3"]["port"])


def test_the_product_commands_start_on_either_release_line_of_their_image():
    services = _services()
    # OpenCTI 7.x images run build/back.mjs (6.x build/back.js); pgsty/silo, the S3 store of OpenCTI's own
    # compose, names its binary silo where minio/minio names it minio.
    opencti = _opencti_start()
    assert "entry=build/back.mjs" in opencti and "entry=build/back.js;" in opencti
    s3 = services["lab-opencti-s3"]["argv"][-1]
    assert "command -v silo || command -v minio" in s3 and "127.0.0.1:9002" in s3


def test_the_lab_opencti_waits_for_the_port_of_every_dependency_before_it_starts():
    services = _services()
    # The harness waits for no dependency of a product image, and OpenCTI 7 exits on a broker not yet up.
    opencti = _opencti_start()
    waits = opencti.split("cd /opt/opencti")[0]
    ports = waits.split("for p in ")[1].split("; do")[0].split()
    for dep in services["lab-opencti"]["depends_on"]:
        assert str(services[dep]["port"]) in ports, dep
    # A fresh OpenSearch and Redis answer on their port before they serve: the first-start init exits 1.
    assert "_cluster/health?wait_for_status=yellow" in waits and "SET" in waits
    assert opencti.index("process.exit(0)") < opencti.index("build/back.mjs")


def test_the_lab_opencti_starts_with_an_encryption_key_generated_per_lab_and_never_committed():
    services = _services()
    # OpenCTI 7.x fails its admin init without app:encryption_key ("missing or invalid") and exits 1.
    opencti = _opencti_start()
    assert "export APP__ENCRYPTION_KEY" in opencti and "randomBytes(32)" in opencti
    assert opencti.index("APP__ENCRYPTION_KEY") < opencti.index("build/back.mjs")
    assert "APP__ENCRYPTION_KEY" not in services["lab-opencti"]["env"]


def test_the_lab_opencti_redis_never_snapshots_to_the_read_only_checkout():
    services = _services()
    # Redis saves RDB snapshots in its working directory, the checkout, where it cannot write: after the first
    # failed save it refuses every write (MISCONF), so OpenCTI's manager locks fail and the XTM One
    # registration manager never runs its retry tick.
    # An empty argv item is refused by the declaration, so the empty --save goes through sh.
    command = services["lab-opencti-redis"]["argv"][-1]
    assert "--save '' " in command and "--appendonly no" in command
    assert command.startswith("exec redis-server --port 6380 ")


def _clock(*steps: float):
    values = iter(steps)
    return types.SimpleNamespace(monotonic=lambda: next(values), sleep=lambda _s: None)


def test_seed_live_opencti_waits_for_the_real_registration_and_answers_its_row():
    answers = iter([[], {"items": [{"id": "x", "provider": "opencti", "is_company_managed": True}]}])
    ns = _lab_namespace(
        "LIVE_OPENCTI_URL",
        "LIVE_OPENCTI_REGISTER_SECONDS",
        "seed_live_opencti",
        api=lambda *a, **k: next(answers),
        time=_clock(0, 1, 2),
    )
    assert ns["seed_live_opencti"]("t", "http://localhost:3100") == {
        "id": "x",
        "url": "http://localhost:3100/integrations/x",
    }
    ns = _lab_namespace(
        "LIVE_OPENCTI_URL",
        "LIVE_OPENCTI_REGISTER_SECONDS",
        "seed_live_opencti",
        api=lambda *a, **k: [],
        time=_clock(0, 10_000),
    )
    with pytest.raises(RuntimeError, match="did not register"):
        ns["seed_live_opencti"]("t", "http://localhost:3100")


def test_the_seed_skips_the_invalid_opencti_row_only_when_the_lab_has_its_own():
    ns = _lab_namespace("live_opencti", env={"LAB_OPENCTI": "live"})
    assert ns["live_opencti"]() is True
    assert _lab_namespace("live_opencti")["live_opencti"]() is False
    source = _lab_source("seed_suite_registrations")
    assert "if provider == 'opencti' and live_opencti():" in source
    seed = _lab_source("seed_entities")
    assert seed.index("seed_suite_registrations(token, base)") < seed.index("seed_live_opencti(token, base)")


def test_a_lab_runs_the_worker_only_when_its_declaration_asks():
    # The worker's startup starts the orchestration reconciler: a delegated background task starts (#11023).
    assert _lab_namespace("runs_worker", env={"LAB_WORKER": "1"})["runs_worker"]() is True
    assert _lab_namespace("runs_worker")["runs_worker"]() is False
    argv = _lab_namespace("worker_argv", sys=types.SimpleNamespace(executable="/opt/venv/bin/python"))["worker_argv"]()
    assert argv == ["/opt/venv/bin/python", "-m", "saq", "app.worker.settings"]
    main = _lab_source("main")
    # After the backend's readiness (it migrates; the worker waits for the schema head), with the backend's env.
    assert main.index("/api/health/ready") < main.index("if runs_worker():")
    assert "spawn('worker', worker_argv(), cwd=CHECKOUT / 'backend', env=backend_env())" in main
