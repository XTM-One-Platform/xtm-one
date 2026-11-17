"""The Filigran licensing PR-head lab: [service.lab-filigran-licenses] (xtm-one #12370).

The licensing application (next dev) from a nested checkout at the PR head, migrated on the lab's PostgreSQL, one
customer and the Filigran License product seeded, signed in through the app's own development provider. These pin
the declaration, the ports, the environment the app boots with and the headers the preview forwards.
"""

from __future__ import annotations

import importlib.util

import pytest

from tests.test_lab_seed_declarations import CHECKS, LAB, _services

SCRIPT = LAB.parent / "filigran_licenses_pr_head.py"

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_filigran_licenses_pr_head", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_service_runs_the_script_with_the_stores():
    service = _services()["lab-filigran-licenses"]
    module = _module()
    assert service["argv"] == ["python", ".xtm-one/lab/filigran_licenses_pr_head.py"]
    assert service["image"] == "python-dev" and not service.get("depends_on")
    assert ".xtm-one/stores.py" in service["needs"]
    assert service["port"] == module.PORT and service["ready_path"] == "/lab/ready"
    assert service["session_log"] and service["session_log"] in "seeded signed-in session for lab@filigran.io"


def test_the_ports_never_meet_another_lab():
    module = _module()
    ours = {module.PORT, module.NEXT_PORT}
    assert len(ours) == 2
    others = {spec["port"] for name, spec in _services().items() if name != "lab-filigran-licenses"}
    assert not ours & others


def test_the_app_signs_in_with_its_development_provider_only():
    """src/auth.ts: the local admin provider exists only under NODE_ENV=development with LOCAL_ADMIN_* set."""
    env = _module().app_env()
    assert env["NODE_ENV"] == "development"
    assert env["LOCAL_ADMIN_EMAIL"] and env["LOCAL_ADMIN_PASSWORD"]
    # An empty AUTH_SAML_ISSUER makes Auth.js reject its whole configuration (.env.example): never set.
    assert not any(name.startswith("AUTH_SAML_") for name in env)
    assert env["TRIAL_AUTH_TOKEN"], "src/instrumentation.ts refuses to start without it"


def test_the_app_uses_the_lab_postgres():
    module = _module()
    env = module.app_env()
    assert env["DATABASE_PORT"] == str(module.lab.stores.PG_PORT)
    assert env["DATABASE_ID"] == module.DATABASE and env["DATABASE_USER"] == "copilot"


def test_the_forward_keeps_the_browser_host():
    """Auth.js and server actions compare Host with Origin: the preview host must reach next dev."""
    module = _module()
    headers = module.upstream_headers(
        {"Host": "preview.example", "Connection": "keep-alive", "Content-Length": "9"}, b"ab"
    )
    assert headers["Host"] == "preview.example"
    assert "Connection" not in headers and headers["Content-Length"] == "2"


def test_cookie_pairs_drop_the_attributes():
    module = _module()
    pairs = module.cookie_pairs(["a=1; Path=/; HttpOnly", "authjs.csrf-token=x%7Cy; Path=/; SameSite=Lax"])
    assert pairs == "a=1; authjs.csrf-token=x%7Cy"


def test_psql_binds_values_as_variables_never_in_the_statement(monkeypatch):
    module = _module()
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, sql=kwargs["input"])
        return type("Done", (), {"stdout": ""})()

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.lab.stores, "installed", lambda dest: True)
    module.psql("SELECT id FROM \"Customer\" WHERE name = :'name'", name='x\'; DROP TABLE "Customer"; --')
    assert "DROP" not in seen["sql"] and "-c" not in seen["argv"]


def test_the_start_refuses_a_checkout_missing_a_sub_path(monkeypatch, tmp_path):
    module = _module()
    where = tmp_path / "filigran-licenses"
    for sub_path in module.SUB_PATHS:
        target = where / sub_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if "." in sub_path.rsplit("/", 1)[-1]:
            target.write_text("{}", encoding="utf-8")
        else:
            target.mkdir()
    monkeypatch.setattr(module.lab, "CHECKOUT", tmp_path)
    monkeypatch.delenv("LAB_FILIGRAN_LICENSES_CHECKOUT", raising=False)
    assert module.licenses_checkout() == where
    (where / "src").rmdir()
    with pytest.raises(SystemExit) as refused:
        module.licenses_checkout()
    assert "lacks src" in str(refused.value)


def test_the_declaration_comment_names_every_sub_path_and_the_customer():
    module = _module()
    comment = " ".join(CHECKS.read_text(encoding="utf-8").split("[service.lab-filigran-licenses]")[0].splitlines()[-8:])
    for sub_path in module.SUB_PATHS:
        assert f'"{sub_path}"' in comment, sub_path
    assert module.CUSTOMER_ID in comment
