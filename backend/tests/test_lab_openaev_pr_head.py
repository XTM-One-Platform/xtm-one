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
    ours = {module.PORT, module.VITE_PORT, module.BACKEND_PORT, module.OPENCTI_STUB_PORT}
    assert len(ours) == 4
    others = {spec["port"] for name, spec in _services().items() if name != "lab-openaev"}
    assert not ours & others


def test_the_default_tenant_is_connected_to_the_opencti_stub(monkeypatch):
    """Gap lab-xtm-counterpart-stub: the backend's own counterpart lookup answers, no browser stub."""
    import json

    module = _module()
    monkeypatch.delenv("LAB_OPENAEV_OPENCTI", raising=False)
    spring = json.loads(module.backend_env()["SPRING_APPLICATION_JSON"])
    assert spring == {
        "openaev": {
            "xtm": {"opencti": {"enable": True, "url": module.OPENCTI_STUB_URL, "token": module.OPENCTI_STUB_TOKEN}}
        }
    }
    assert module.OPENCTI_STUB_URL.endswith("/") and str(module.OPENCTI_STUB_PORT) in module.OPENCTI_STUB_URL
    monkeypatch.setenv("LAB_OPENAEV_OPENCTI", "off")
    assert "SPRING_APPLICATION_JSON" not in module.backend_env()


def _lookup(module, kind_filters):
    return module.opencti_answer(
        {
            "query": "query XtmCounterparts { stixCoreObjects { edges { node { id } } } }",
            "variables": {"first": 8, "filters": kind_filters},
        }
    )


def test_the_stub_answers_the_asset_lookup_found_and_not_found():
    """The filter shape of OpenCTICounterpartProvider.variables(ASSET): a System by name, observables by value."""
    module = _module()
    names = ["lab-web-01", "lab-dc-01", "lab-mac-01"]
    filters = {
        "mode": "or",
        "filters": [],
        "filterGroups": [
            {
                "mode": "and",
                "filters": [{"key": ["entity_type"], "values": ["System"]}, {"key": ["name"], "values": names}],
            },
            {
                "mode": "and",
                "filters": [
                    {"key": ["entity_type"], "values": ["Hostname", "IPv4-Addr"]},
                    {"key": ["value"], "values": names},
                ],
            },
        ],
    }
    nodes = [e["node"] for e in _lookup(module, filters)["data"]["stixCoreObjects"]["edges"]]
    found = {n.get("name") or n.get("observable_value") for n in nodes}
    assert found == {"lab-web-01", "lab-dc-01"}
    web = next(n for n in nodes if n.get("name") == "lab-web-01")
    assert web["representative"]["main"] == "lab-web-01" and web["reports"]["pageInfo"]["globalCount"] == 3
    assert web["objectMarking"] == [{"definition": "TLP:AMBER"}] and web["modified"]


def test_the_stub_finds_an_asset_by_its_seeded_hostname():
    """OpenAEV looks an endpoint up by its hostname: each seeded asset_hostname finds its counterpart, lab-mac-01 none."""
    module = _module()
    hostnames = [asset["asset_hostname"] for asset in module.SEED_ASSETS]
    filters = {"mode": "and", "filters": [{"key": ["value"], "values": hostnames}], "filterGroups": []}
    nodes = [e["node"] for e in _lookup(module, filters)["data"]["stixCoreObjects"]["edges"]]
    assert {n["id"] for n in nodes} == {"lab-octi-system-web-01", "lab-octi-ipv4-dc-01"}
    assert all("hostname" not in n for n in nodes)
    upper = {"mode": "and", "filters": [{"key": ["value"], "values": ["LAB-WEB-01.LAB.LOCAL"]}], "filterGroups": []}
    assert [e["node"]["id"] for e in _lookup(module, upper)["data"]["stixCoreObjects"]["edges"]] == [
        "lab-octi-system-web-01"
    ]


def test_the_stub_answers_an_attack_pattern_by_its_mitre_id_and_nothing_else_empty():
    module = _module()
    filters = {
        "mode": "and",
        "filters": [
            {"key": ["entity_type"], "values": ["Attack-Pattern"]},
            {"key": ["x_mitre_id"], "values": ["T1486", "T9999"]},
        ],
        "filterGroups": [],
    }
    nodes = [e["node"] for e in _lookup(module, filters)["data"]["stixCoreObjects"]["edges"]]
    assert [n["x_mitre_id"] for n in nodes] == ["T1486"]
    assert module.opencti_answer({"query": "query { externalReferences { edges { node { url } } } }"}) == {
        "data": {"externalReferences": {"edges": []}}
    }
    assert module.opencti_answer({"query": "mutation { connectorRegister }"}) == {"data": {}}


def test_the_backend_runs_on_the_port_the_front_proxies_to():
    """openaev-front/vite.config.ts proxies /api to http://localhost:8080."""
    assert _module().BACKEND_PORT == 8080


def test_the_backend_is_awaited_on_its_unauthenticated_route():
    """/api/settings answers 401 without a session, /api/health 400 without its key: the wait ends on neither."""
    module = _module()
    assert f"/api/health?health_access_key={module.HEALTH_KEY}" == module.HEALTH_PATH
    assert module.backend_env()["OPENAEV_HEALTHCHECK_KEY"] == module.HEALTH_KEY
    assert "{HEALTH_PATH}" in SCRIPT.read_text(encoding="utf-8")


def test_the_lab_turns_every_preview_feature_on():
    """A PR behind an OpenAEV preview flag renders its screens in the lab, not the flag-off fallback."""
    module = _module()
    assert module.backend_env()["OPENAEV_ENABLED_DEV_FEATURES"] == module.PREVIEW_FEATURES == "*"


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

    monkeypatch.setattr(module, "STATE", {})
    monkeypatch.setattr(module, "api", _seed_api(calls))
    monkeypatch.setattr(module, "SEEDED", tmp_path / "openaev-seeded.json")
    done = module.seed()
    writes = [path for method, path, _ in calls if method == "POST"]
    assert all(path.startswith(f"/api/tenants/{module.TENANT_ID}/") for path in writes)
    assert len(done) == len(module.SEED_ASSETS) + 8
    assert module.STATE["scenario"] == "s1" and module.STATE["simulation"] == "e1"
    assert "seed_missing" not in module.STATE


def _seed_api(calls):
    def api(method, path, body=None):
        calls.append((method, path, body))
        if path == "/api/settings":
            return {"platform_version": "3.261123.0"}
        if path.endswith("endpoints/agentless"):
            return {"asset_id": f"a{len(calls)}"}
        if path.endswith("endpoints/register"):
            return {"asset_id": "agent-1"}
        if path.endswith("scenarios"):
            return {"scenario_id": "s1"}
        if path.endswith("exercises"):
            return {"exercise_id": "e1"}
        if path.endswith("/payloads"):
            return {"payload_id": "p1"}
        if path.endswith("injector_contracts/search"):
            return {"content": [{"injector_contract_id": "c1"}]}
        if path.endswith("/atomic-testings"):
            return {"inject_id": "i1"}
        return {}

    return api


def test_the_seed_launches_an_atomic_testing_on_a_registered_agent(monkeypatch, tmp_path):
    """Gap lab-openaev-no-running-atomic-testing-seed: the running and pending states render with no hand SQL."""
    module = _module()
    calls = []
    monkeypatch.setattr(module, "STATE", {})
    monkeypatch.setattr(module, "api", _seed_api(calls))
    monkeypatch.setattr(module, "SEEDED", tmp_path / "openaev-seeded.json")
    module.seed()
    bodies = {path.rsplit(f"/api/tenants/{module.TENANT_ID}/", 1)[-1]: body for _, path, body in calls}
    # The agent registers with the backend's own version: any other gets an upgrade job whose installer is absent.
    assert bodies["endpoints/register"]["endpoint_agent_version"] == "3.261123.0"
    assert bodies["atomic-testings"]["inject_injector_contract"] == "c1"
    assert bodies["atomic-testings"]["inject_assets"] == ["agent-1"]
    assert ("POST", module.tenant_path("atomic-testings/i1/launch"), None) in calls
    assert module.STATE["atomic_testing"] == "i1" and "seed_missing" not in module.STATE


def test_a_missing_platform_version_names_the_agent_step_and_skips_what_depends_on_it(monkeypatch, tmp_path):
    module = _module()
    calls = []
    inner = _seed_api(calls)
    monkeypatch.setattr(module, "STATE", {})
    monkeypatch.setattr(module, "api", lambda m, p, b=None: {} if p == "/api/settings" else inner(m, p, b))
    monkeypatch.setattr(module, "SEEDED", tmp_path / "openaev-seeded.json")
    module.seed()
    missing = module.STATE["seed_missing"]
    assert "agent lab-agent-01" in missing and "platform_version" in missing
    assert "atomic testing" in missing and "launch of" in missing
    assert "agent" not in module.STATE


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
    inner = _seed_api([])

    def api(method, path, body=None):
        if method == "POST":
            calls.append(path)
        if path.endswith("exercises") and refuse["exercises"]:
            raise _http_error(module, 400, b'{"message":"exercise_name: must not be blank"}')
        return inner(method, path, body)

    monkeypatch.setattr(module, "api", api)
    first = module.seed()
    assert len(first) == len(module.SEED_ASSETS) + 7
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


def test_the_history_writes_finished_simulations_once_with_bound_values(monkeypatch, tmp_path):
    """Gap lab-openaev-no-history-seed: past finished simulations with scored expectations, no hand SQL."""
    module = _module()
    calls = []
    monkeypatch.setattr(module, "STATE", {})
    monkeypatch.setattr(module, "SEEDED", tmp_path / "openaev-seeded.json")
    monkeypatch.setattr(module, "psql", lambda sql, **params: calls.append((sql, params)) or "10")
    assert module.seed_history() is True
    sql, params = calls[0]
    assert params == {"scenario": module.SEED_SCENARIO["scenario_name"], "weeks": str(module.HISTORY_WEEKS)}
    assert module.SEED_SCENARIO["scenario_name"] not in sql, "values are bound as psql variables, never spliced"
    assert "'FINISHED'" in sql and "injects_expectations" in sql and "scenarios_exercises" in sql
    for kind in ("PREVENTION", "DETECTION", "VULNERABILITY"):
        assert f"'{kind}'" in sql
    # The next start on the kept database writes nothing again.
    assert module.seed_history() is False
    assert len(calls) == 1


def test_a_failed_history_is_named_and_tried_again_on_the_next_start(monkeypatch, tmp_path):
    module = _module()
    monkeypatch.setattr(module, "STATE", {"seed_missing": "asset lab-web-01 (HTTP 400)"})
    monkeypatch.setattr(module, "SEEDED", tmp_path / "openaev-seeded.json")

    def fail(sql, **params):
        raise module.subprocess.CalledProcessError(3, ["psql"], stderr="ERROR:  no scenario named Lab")

    monkeypatch.setattr(module, "psql", fail)
    assert module.seed_history() is False
    missing = module.STATE["seed_missing"]
    assert missing.startswith("asset lab-web-01 (HTTP 400); ")
    assert module.HISTORY_STEP in missing and "no scenario named" in missing
    monkeypatch.setattr(module, "psql", lambda sql, **params: "10")
    assert module.seed_history() is True


def test_the_start_writes_the_history_after_the_api_seed():
    source = SCRIPT.read_text()
    main = source[source.index("def main()") :]
    assert main.index("seed()") < main.index("seed_history()")


def test_the_seed_waits_for_the_admin_token_the_start_up_runner_writes(monkeypatch):
    """/api/health answers before InitAdminCommandLineRunner wrote the admin token: 401 until then (gap
    lab-openaev-seed-401), so the seed waits for GET /api/me to authenticate."""
    module = _module()
    monkeypatch.setattr(module, "ADMIN_TOKEN_POLL_SECONDS", 0)
    monkeypatch.setattr(module.lab, "log", lambda line: None)
    answers = [_http_error(module, 401, b""), module.urllib.error.URLError("refused"), _http_error(module, 503, b""),
               {"user_email": module.ADMIN_EMAIL}]
    calls = []

    def api(method, path, body=None):
        calls.append((method, path))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(module, "api", api)
    assert module.wait_for_admin_token(timeout=60) is True
    assert not answers and set(calls) == {("GET", "/api/me")}


def test_the_admin_token_wait_stops_on_a_final_answer_or_the_deadline(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "ADMIN_TOKEN_POLL_SECONDS", 0)
    logged = []
    monkeypatch.setattr(module.lab, "log", logged.append)

    def forbidden(method, path, body=None):
        raise _http_error(module, 403, b"")

    monkeypatch.setattr(module, "api", forbidden)
    assert module.wait_for_admin_token(timeout=60) is False
    assert "HTTP 403" in logged[-1]

    def unauthorized(method, path, body=None):
        raise _http_error(module, 401, b"")

    monkeypatch.setattr(module, "api", unauthorized)
    assert module.wait_for_admin_token(timeout=0) is False
    assert "did not authenticate" in logged[-1]


def test_the_start_waits_for_the_admin_token_before_the_seed():
    text = SCRIPT.read_text(encoding="utf-8")
    main = text.split("def main() -> None:", 1)[1]
    assert main.index("wait_for_admin_token()") < main.index("for item in seed():")


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
