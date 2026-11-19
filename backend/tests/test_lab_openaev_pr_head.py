"""The OpenAEV PR-head lab: [service.lab-openaev] (xtm-one #11799, item 5).

OpenAEV's own backend (Maven reactor, Flyway migrations) and front built from a nested checkout at the PR head,
on the lab's stores, seeded through the API, the platform administrator signed in by its token cookie. These pin
the declaration, the ports, the environment the backend boots with, the build and the seed.
"""

from __future__ import annotations

import importlib.util
import io

import pytest

from tests.test_lab_seed_declarations import CHECKS, LAB, _services

SCRIPT = LAB.parent / "openaev_pr_head.py"

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_openaev_pr_head", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_service_runs_the_script_on_the_jvm_image_with_its_stores():
    service = _services()["lab-openaev"]
    module = _module()
    assert service["argv"] == ["python", ".xtm-one/lab/openaev_pr_head.py"]
    assert service["image"] == "toolchain-jvm"
    assert ".xtm-one/stores.py" in service["needs"]
    assert set(service["depends_on"]) == {"lab-opencti-search", "lab-opencti-broker"}
    assert service["port"] == module.PORT and service["ready_path"] == "/lab/ready"
    assert service["session_log"] and service["session_log"] in "seeded signed-in session for lab@filigran.io"


def test_the_ports_never_meet_another_lab():
    module = _module()
    ours = {module.PORT, module.VITE_PORT, module.BACKEND_PORT}
    assert len(ours) == 3
    others = {spec["port"] for name, spec in _services().items() if name != "lab-openaev"}
    assert not ours & others


def test_the_backend_runs_on_the_port_the_front_proxies_to():
    """openaev-front/vite.config.ts proxies /api to http://localhost:8080."""
    assert _module().BACKEND_PORT == 8080


def test_the_backend_is_awaited_on_its_unauthenticated_route():
    """/api/settings answers 401 without a session, /api/health 400 without its key: the wait ends on neither."""
    module = _module()
    assert f"/api/health?health_access_key={module.HEALTH_KEY}" == module.HEALTH_PATH
    assert module.backend_env()["OPENAEV_HEALTHCHECK_KEY"] == module.HEALTH_KEY
    assert "{HEALTH_PATH}" in SCRIPT.read_text(encoding="utf-8")


def test_the_backend_boots_on_the_lab_stores():
    module = _module()
    env = module.backend_env()
    assert env["SPRING_DATASOURCE_URL"].endswith(f":{module.lab.stores.PG_PORT}/{module.DATABASE}")
    assert (env["MINIO_ACCESS_KEY"], env["MINIO_ACCESS_SECRET"]) == (module.lab.stores.S3_USER, module.lab.stores.S3_PASSWORD)
    assert env["MINIO_PORT"] == str(module.lab.stores.S3_PORT)
    assert env["ENGINE_URL"] == "http://127.0.0.1:9200" and env["OPENAEV_RABBITMQ_PORT"] == "5672"
    assert env["OPENAEV_ADMIN_EMAIL"] == module.ADMIN_EMAIL and env["OPENAEV_ADMIN_TOKEN"] == module.ADMIN_TOKEN
    assert env["SERVER_PORT"] == str(module.BACKEND_PORT)


def test_the_build_skips_tests_and_keeps_its_repository_in_the_lab_cache(tmp_path):
    module = _module()
    argv = module.maven_argv(tmp_path)
    assert argv[0] == "mvn" and "-DskipTests" in argv
    assert argv[argv.index("-pl") + 1] == "openaev-api" and "-am" in argv
    assert f"-Dmaven.repo.local={module.lab.CACHE / 'm2'}" in argv


def test_the_sign_in_cookie_is_the_admin_token():
    """TokenAuthenticationFilter reads the openaev_token cookie (openaev.cookie-name)."""
    module = _module()
    assert module.sign_in_cookie().startswith(f"openaev_token={module.ADMIN_TOKEN};")


def test_the_sign_in_sets_a_readable_xsrf_cookie():
    """The first page's POST carries X-XSRF-TOKEN from this cookie: HttpOnly would hide it from the front."""
    cookie = _module().xsrf_cookie()
    assert cookie.startswith("XSRF-TOKEN=") and "HttpOnly" not in cookie


def test_the_seed_writes_on_the_default_tenant(monkeypatch, tmp_path):
    module = _module()
    calls = []

    def api(method, path, body=None):
        calls.append((method, path))
        if path.endswith("endpoints/agentless"):
            return {"asset_id": f"a{len(calls)}"}
        if path.endswith("scenarios"):
            return {"scenario_id": "s1"}
        if path.endswith("exercises"):
            return {"exercise_id": "e1"}
        return {}

    monkeypatch.setattr(module, "api", api)
    monkeypatch.setattr(module, "SEEDED", tmp_path / "openaev-seeded.json")
    done = module.seed()
    assert all(path.startswith(f"/api/tenants/{module.TENANT_ID}/") for _, path in calls)
    assert len(done) == len(module.SEED_ASSETS) + 3
    assert module.STATE["scenario"] == "s1" and module.STATE["simulation"] == "e1"
    assert "seed_missing" not in module.STATE


def _http_error(module, code: int, body: bytes):
    return module.urllib.error.HTTPError("http://lab", code, "Error", {}, io.BytesIO(body))


def test_a_seed_step_is_retried_while_the_backend_warms_up(monkeypatch, tmp_path):
    module = _module()
    monkeypatch.setattr(module, "SEEDED", tmp_path / "openaev-seeded.json")
    monkeypatch.setattr(module, "SEED_RETRY_SECONDS", 0)
    answers = [module.urllib.error.URLError("refused"), _http_error(module, 503, b"starting"), {"exercise_id": "e9"}]

    def api(method, path, body=None):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(module, "api", api)
    assert module.seed_call("POST", module.tenant_path("exercises"), module.SEED_SIMULATION) == {"exercise_id": "e9"}
    assert not answers


def test_a_failed_simulation_is_named_with_the_backend_answer_and_seeded_on_the_next_start(monkeypatch, tmp_path):
    module = _module()
    monkeypatch.setattr(module, "SEEDED", tmp_path / "openaev-seeded.json")
    monkeypatch.setattr(module, "STATE", {})
    calls = []
    refuse = {"exercises": True}

    def api(method, path, body=None):
        calls.append(path)
        if path.endswith("exercises") and refuse["exercises"]:
            raise _http_error(module, 400, b'{"message":"exercise_name: must not be blank"}')
        if path.endswith("endpoints/agentless"):
            return {"asset_id": f"a{len(calls)}"}
        if path.endswith("scenarios"):
            return {"scenario_id": "s1"}
        if path.endswith("exercises"):
            return {"exercise_id": "e1"}
        return {}

    monkeypatch.setattr(module, "api", api)
    first = module.seed()
    assert len(first) == len(module.SEED_ASSETS) + 2
    assert calls.count(module.tenant_path("exercises")) == 1, "a 4xx is final, never retried"
    assert "HTTP 400" in module.STATE["seed_missing"] and "must not be blank" in module.STATE["seed_missing"]
    assert "simulation" not in module.STATE

    # The next start on the kept database seeds only the step the record lacks.
    calls.clear()
    refuse["exercises"] = False
    module.STATE.clear()
    second = module.seed()
    assert calls == [module.tenant_path("exercises")]
    assert second == [f"simulation {module.SEED_SIMULATION['exercise_name']}"]
    assert module.STATE["scenario"] == "s1" and module.STATE["simulation"] == "e1"
    assert "seed_missing" not in module.STATE


def test_the_start_refuses_a_checkout_missing_a_sub_path(monkeypatch, tmp_path):
    module = _module()
    where = tmp_path / "openaev"
    for sub_path in module.OPENAEV_SUB_PATHS:
        target = where / sub_path
        if sub_path.endswith(".xml"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("<project/>", encoding="utf-8")
        else:
            target.mkdir(parents=True)
    monkeypatch.setattr(module.lab, "CHECKOUT", tmp_path)
    monkeypatch.delenv("LAB_OPENAEV_CHECKOUT", raising=False)
    assert module.openaev_checkout() == where
    (where / "openaev-front").rmdir()
    with pytest.raises(SystemExit) as refused:
        module.openaev_checkout()
    assert "lacks openaev-front" in str(refused.value)


def test_the_declaration_comment_names_every_sub_path():
    module = _module()
    comment = " ".join(CHECKS.read_text(encoding="utf-8").split("[service.lab-openaev]")[0].splitlines()[-9:])
    for sub_path in module.OPENAEV_SUB_PATHS:
        assert f'"{sub_path}"' in comment, sub_path
