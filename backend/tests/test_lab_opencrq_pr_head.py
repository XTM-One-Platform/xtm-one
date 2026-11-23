"""The OpenCRQ PR-head lab: [service.lab-opencrq] (xtm-one #11799, item 5).

OpenCRQ's own backend and front built from a nested checkout at the PR head, migrated and platform-seeded on the
lab's PostgreSQL, the seeded administrator signed in by a session row. These pin the declaration, the ports, the
configuration the backend boots with and the session cookie the front sends.
"""

from __future__ import annotations

import importlib.util

import pytest

from tests.test_lab_seed_declarations import CHECKS, LAB, _services

SCRIPT = LAB.parent / "opencrq_pr_head.py"

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_opencrq_pr_head", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_service_runs_the_script_with_the_stores():
    service = _services()["lab-opencrq"]
    module = _module()
    assert service["argv"] == ["python", ".xtm-one/lab/opencrq_pr_head.py"]
    assert service["image"] == "python-dev" and not service.get("depends_on")
    assert ".xtm-one/stores.py" in service["needs"]
    assert service["port"] == module.PORT and service["ready_path"] == "/lab/ready"
    assert service["session_log"] and service["session_log"] in "seeded signed-in session for lab@filigran.io"


def test_the_ports_never_meet_another_lab():
    module = _module()
    ours = {module.PORT, module.VITE_PORT, module.BACKEND_PORT, module.HEALTH_PORT}
    assert len(ours) == 4
    others = {spec["port"] for name, spec in _services().items() if name not in ("lab-opencrq", "lab-opencrq-opencti")}
    assert not ours & others


def test_the_backend_boots_on_the_lab_postgres_with_the_platform_seed():
    module = _module()
    config = module.local_config()
    assert config["postgres"]["port"] == module.lab.stores.PG_PORT
    assert config["postgres"]["database"] == module.DATABASE
    assert config["http_server"]["port"] == module.BACKEND_PORT
    assert config["dev_server"]["port"] == module.VITE_PORT
    assert config["platform_seed"]["enabled"] is True
    # development.json's file_storage names a MinIO the lab does not run: a failed HeadBucket exits the backend.
    storage = config["file_storage"]
    assert storage["endpoint"].endswith(f":{module.lab.stores.S3_PORT}")
    assert (storage["access_key"], storage["secret_key"]) == (module.lab.stores.S3_USER, module.lab.stores.S3_PASSWORD)
    assert config["platform_seed"]["admin"]["email"] == module.LAB_ADMIN_EMAIL
    # xtm_one is required by the config parse (config-schema.ts) and three keys long.
    assert set(config["xtm_one"]) == {"url", "token", "platform_url"}


def test_the_session_cookie_carries_the_backend_port():
    """back/src/trpc.ts scopeSessionCookieToPort: outside production the name ends with the backend port."""
    module = _module()
    assert module.session_cookie_name() == f"app::auth_session::{module.BACKEND_PORT}"


def test_psql_binds_values_as_variables_never_in_the_statement(monkeypatch):
    module = _module()
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, sql=kwargs["input"])
        return type("Done", (), {"stdout": ""})()

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.lab.stores, "installed", lambda dest: True)
    module.psql("SELECT id FROM users WHERE email = :'email'", email="x'; DROP TABLE users; --")
    assert "DROP" not in seen["sql"] and "-c" not in seen["argv"]
    assert "email=x'; DROP TABLE users; --" in seen["argv"]


def test_node_is_the_release_the_root_engines_ask_for():
    module = _module()
    assert "node-linux-x64-26." in module.NODE26[0] and module.NODE26[1] == "sha512"


def test_the_start_refuses_a_checkout_missing_a_sub_path(monkeypatch, tmp_path):
    module = _module()
    where = tmp_path / "opencrq"
    for sub_path in module.OPENCRQ_SUB_PATHS:
        target = where / sub_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if "." in sub_path.rsplit("/", 1)[-1]:
            target.write_text("{}", encoding="utf-8")
        else:
            target.mkdir()
    monkeypatch.setattr(module.lab, "CHECKOUT", tmp_path)
    monkeypatch.delenv("LAB_OPENCRQ_CHECKOUT", raising=False)
    assert module.opencrq_checkout() == where
    (where / "back").rmdir()
    with pytest.raises(SystemExit) as refused:
        module.opencrq_checkout()
    assert "lacks back" in str(refused.value)


def test_a_tenant_is_selected_by_its_membership_order(monkeypatch):
    """Community Edition shows a user's first tenant by id: the user joins it and leaves the ones sorted before it."""
    module = _module()
    calls = []

    def psql(sql, database=module.DATABASE, **params):
        calls.append((sql, params))
        return "bbbe7688-581e-4f4d-af65-19085e0c7916" if sql.startswith("SELECT id FROM tenants") else ""

    monkeypatch.setattr(module, "psql", psql)
    user = "f4ad0db5-4c66-4e5f-8817-7f4dc3b67162"
    assert module.select_tenant("tarnwick industries", user) == "bbbe7688-581e-4f4d-af65-19085e0c7916"
    (lookup, lookup_params), (write, write_params) = calls
    assert "lower(name) = lower(:'name')" in lookup and lookup_params == {"name": "tarnwick industries"}
    assert "INSERT INTO tenant_users" in write and "ON CONFLICT DO NOTHING" in write
    assert "DELETE FROM tenant_users WHERE user_id = :'user'::uuid AND tenant_id < :'tenant'::uuid" in write
    assert write_params == {"tenant": "bbbe7688-581e-4f4d-af65-19085e0c7916", "user": user}
    assert "tarnwick" not in write.lower()


def test_an_unknown_tenant_writes_nothing(monkeypatch):
    module = _module()
    calls = []
    monkeypatch.setattr(module, "psql", lambda sql, **params: calls.append(sql) or "")
    assert module.select_tenant("Initech", "f4ad0db5-4c66-4e5f-8817-7f4dc3b67162") is None
    assert len(calls) == 1 and calls[0].startswith("SELECT id FROM tenants")


def test_every_feature_flag_is_on_by_default(monkeypatch):
    """A screen behind a development flag renders in the lab; the env narrows them, 'none' turns them off."""
    module = _module()
    monkeypatch.delenv("LAB_OPENCRQ_FEATURE_FLAGS", raising=False)
    assert module.local_config()["feature_flags"] == ["*"]
    monkeypatch.setenv("LAB_OPENCRQ_FEATURE_FLAGS", " enterprise-edition-status , technique-coverage-changes ")
    assert module.local_config()["feature_flags"] == ["enterprise-edition-status", "technique-coverage-changes"]
    assert module.feature_flags("none") == [] and module.feature_flags("") == ["*"]


def test_a_switch_opens_a_local_next_path_only():
    module = _module()
    assert module.next_path("next=/abc/assets?tab=2") == "/abc/assets?tab=2"
    assert module.next_path("") == "/"
    for unsafe in ("next=//evil.example/", "next=https://evil.example/", "next=/\\evil.example"):
        assert module.next_path(unsafe) == "/", unsafe


def test_the_language_is_the_users_locale(monkeypatch):
    module = _module()
    calls = []
    monkeypatch.setattr(module, "psql", lambda sql, **params: calls.append((sql, params)) or "")
    user = "f4ad0db5-4c66-4e5f-8817-7f4dc3b67162"
    assert module.set_locale("fr", user) is True
    assert calls[-1] == ("UPDATE users SET locale = :'locale' WHERE id = :'user'::uuid", {"locale": "fr", "user": user})
    assert module.set_locale("default", user) is True
    assert calls[-1] == ("UPDATE users SET locale = NULL WHERE id = :'user'::uuid", {"user": user})
    assert module.set_locale("de", user) is False and len(calls) == 2


def test_opencti_is_off_by_default(monkeypatch):
    """No connectors.opencti: the default lab renders every OpenCTI surface unavailable, as before."""
    module = _module()
    monkeypatch.delenv("LAB_OPENCRQ_OPENCTI", raising=False)
    config = module.local_config()
    assert "connectors" not in config and "xtm" not in config
    assert module.opencti_connectors("off") == {} and module.opencti_connectors("") == {}


def test_the_opencti_variant_connects_the_fresh_start_tenant_to_the_lab_opencti(monkeypatch):
    module = _module()
    service = _services()["lab-opencrq-opencti"]
    assert service["argv"] == _services()["lab-opencrq"]["argv"]
    assert service["depends_on"] == ["lab-opencti"]
    assert service["env"] == {"LAB_OPENCRQ_OPENCTI": "live"}
    monkeypatch.setenv("LAB_OPENCRQ_OPENCTI", "live")
    config = module.local_config()
    # config-schema.ts keys each connector by tenant UUID: the platform seed's Tenant_FreshStart.
    assert config["connectors"] == {
        "opencti": {
            module.FRESH_START_TENANT: {"url": module.lab.LIVE_OPENCTI_URL, "token": module.lab.CTEM_OPENCTI_TOKEN}
        }
    }
    assert config["xtm"] == {"opencti_url": module.lab.LIVE_OPENCTI_URL}
    lab_opencti = _services()["lab-opencti"]
    assert lab_opencti["env"]["APP__ADMIN__TOKEN"] == module.lab.CTEM_OPENCTI_TOKEN
    assert module.lab.LIVE_OPENCTI_URL.endswith(f":{lab_opencti['port']}")


def test_the_declaration_comment_names_every_sub_path():
    module = _module()
    comment = " ".join(CHECKS.read_text(encoding="utf-8").split("[service.lab-opencrq]")[0].splitlines()[-8:])
    for sub_path in module.OPENCRQ_SUB_PATHS:
        assert f'"{sub_path}"' in comment, sub_path
