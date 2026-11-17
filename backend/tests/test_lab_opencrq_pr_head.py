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
    others = {spec["port"] for name, spec in _services().items() if name != "lab-opencrq"}
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


def test_the_declaration_comment_names_every_sub_path():
    module = _module()
    comment = " ".join(CHECKS.read_text(encoding="utf-8").split("[service.lab-opencrq]")[0].splitlines()[-8:])
    for sub_path in module.OPENCRQ_SUB_PATHS:
        assert f'"{sub_path}"' in comment, sub_path
