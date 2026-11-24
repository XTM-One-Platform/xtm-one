"""``stores = true``: a check of any repository whose CI runs a postgres service gets the offline postgres (#1858).

The runner (``.xtm-one/stores.py exec``) starts PostgreSQL in the check's own container and exports libpq's
``PG*`` variables; the declaration, the wrap, the detection and the runner's environment are checked here.
The runner itself starts postgres in the toolchain image smoke test of ``sandbox-images.yml`` (``--network none``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.services.sandbox_workspace import checks, commands, detect

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def stores() -> ModuleType:
    spec = importlib.util.spec_from_file_location("xtm_stores_exec", ROOT / ".xtm-one" / "stores.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["xtm_stores_exec"] = module
    spec.loader.exec_module(module)
    return module


def test_stores_is_read_from_the_declaration_and_defaults_to_false() -> None:
    specs = checks.load_checks('[check.a]\nargv = ["x"]\n[check.b]\nargv = ["x"]\nstores = true\n')
    assert specs["a"].stores is False and specs["b"].stores is True
    with pytest.raises(checks.ChecksError, match="stores must be true or false"):
        checks.load_checks('[check.a]\nargv = ["x"]\nstores = "yes"\n')


def test_a_stores_check_runs_through_the_runner_with_its_args() -> None:
    spec = checks.load_checks('[check.a]\nargv = ["yarn", "test"]\ndefault_args = ["--all"]\nstores = true\n')["a"]
    assert checks.command_argv(spec) == (*checks.STORES_RUNNER, "yarn", "test", "--all")
    assert checks.command_argv(spec, ["x.test.ts"]) == (*checks.STORES_RUNNER, "yarn", "test", "x.test.ts")
    assert "offline postgres" in checks.describe_checks({"a": spec})


def test_a_stores_and_git_repo_check_runs_the_runner_inside_the_throwaway_repository() -> None:
    spec = checks.load_checks(
        '[check.a]\nargv = ["yarn", "db:check"]\ncwd = "module"\nstores = true\ngit_repo = true\n'
    )["a"]
    argv = checks.command_argv(spec)
    assert argv[:5] == ("sh", "-c", checks.GIT_REPO_SHELL, "xtm-git", "..")
    assert argv[5:] == (*checks.STORES_RUNNER, "yarn", "db:check")


def test_the_runner_path_is_where_the_install_copies_it(stores: ModuleType) -> None:
    assert checks.STORES_RUNNER[1] == f"{stores.STORES_DIR}/{stores.RUNNER}"


@pytest.mark.parametrize(
    ("image", "postgres"),
    [
        ("postgres:16", True),
        ("pgvector/pgvector:0.8.7-pg17", True),
        ("docker.io/library/postgres@sha256:abc", True),
        ("timescale/timescaledb:latest-pg16", True),
        ("postgis/postgis:16-3.4", True),
        ("redis:7", False),
        ("opensearchproject/opensearch:2", False),
        ("my-postgres-exporter:1", False),
    ],
)
def test_a_postgres_service_image_is_recognised(image: str, postgres: bool) -> None:
    assert detect.is_postgres_image(image) is postgres


WORKFLOW = r"""
name: ci
on: [push]
jobs:
  integration:
    runs-on: ubuntu-26.04
    services:
      postgres:
        image: pgvector/pgvector:0.8.7-pg17
    steps:
      - run: yarn install --immutable
      - run: cd integration-tests && yarn test --shard=1/7
        env:
          PGUSER: app
          PGPASSWORD: admin
  lint:
    runs-on: ubuntu-26.04
    steps:
      - run: yarn lint
"""


def test_a_check_of_a_job_with_a_postgres_service_is_detected_with_stores() -> None:
    files = {".github/workflows/ci.yml": WORKFLOW}
    listing = [*files, "package.json", "integration-tests/package.json"]
    picked = detect.candidates(listing)
    found = detect.detect(listing, {p: files[p].encode() for p in picked if p in files}, images={"toolchain-node"})
    unit = found.checks["integration-tests:node-unit"]
    assert unit.stores is True
    assert dict(unit.env)["PGUSER"] == "app"
    # The install of that job needs no database; a job without the service gets none.
    assert found.checks["node-install"].stores is False
    assert found.checks["node-lint"].stores is False


def test_the_runner_keeps_the_checks_credentials_and_sets_the_rest(stores: ModuleType) -> None:
    env = stores.exec_environment({"PGUSER": "app", "PGPASSWORD": "admin"})
    assert env["PGHOST"] == "127.0.0.1" and env["PGPORT"] == "5432"
    assert (env["PGUSER"], env["PGPASSWORD"], env["PGDATABASE"]) == ("app", "admin", "copilot")
    assert env["DATABASE_URL"] == "postgresql://app:admin@127.0.0.1:5432/copilot"
    assert stores.exec_environment({})["PGUSER"] == "copilot"
    with pytest.raises(SystemExit, match="not a plain identifier"):
        stores.exec_environment({"PGUSER": 'a"; DROP'})


def test_the_runner_refuses_an_image_without_postgres(stores: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="holds no postgres"):
        stores.run_exec(["true"], tmp_path)
    with pytest.raises(SystemExit, match="usage"):
        stores.run_exec([], tmp_path)


def test_every_toolchain_image_bakes_the_stores_and_the_smoke_test_runs_the_runner_offline() -> None:
    text = (ROOT / "backend" / "sandbox" / "Dockerfile.sandbox.toolchain").read_text(encoding="utf-8")
    fetch = text.split(" AS stores-fetch", 1)
    assert len(fetch) == 2 and "@sha256:" in fetch[0].rsplit("FROM ", 1)[1]
    assert "python /tmp/stores.py install /opt/stores" in fetch[1]
    base = text.split(" AS toolchain-base", 1)[1].split(" AS toolchain-", 1)[0]
    assert "COPY --from=stores-fetch /opt/stores /opt/stores" in base
    workflow = (ROOT / ".github" / "workflows" / "sandbox-images.yml").read_text(encoding="utf-8")
    step = workflow.split("- name: Assert the stores runner serves a check offline", 1)[1].split("- name:", 1)[0]
    assert "--network none" in step and "/opt/stores/stores.py exec --" in step


def test_commands_set_ci_as_github_actions_does() -> None:
    assert commands.SAFE_ENV["CI"] == "true"
