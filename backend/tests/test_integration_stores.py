"""The offline integration stores of the sandbox: ``.xtm-one/stores.py``,
the ``backend-integration`` check and the ``python-dev`` image that bakes them.

Static: the stores themselves start in the image smoke test of
``sandbox-images.yml`` (``--network none``); here the wiring is checked.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.services.sandbox_workspace import checks

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def stores():
    spec = importlib.util.spec_from_file_location("xtm_stores", ROOT / ".xtm-one" / "stores.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["xtm_stores"] = module
    spec.loader.exec_module(module)
    return module


def test_every_store_is_pinned_by_sha256(stores):
    assert set(stores.ARTEFACTS) == {"postgres", "postgres-contrib", "redis", "s3"}
    for url, algorithm, digest in stores.ARTEFACTS.values():
        assert url.startswith("https://")
        assert algorithm == "sha256" and re.fullmatch(r"[0-9a-f]{64}", digest), url


def test_installed_reads_the_manifest_of_exactly_the_pins(stores, tmp_path):
    assert not stores.installed(tmp_path)
    (tmp_path / stores.MANIFEST).write_text(json.dumps(stores.pins()), encoding="utf-8")
    assert stores.installed(tmp_path)
    stale = stores.pins()
    stale["redis"]["sha256"] = "0" * 64
    (tmp_path / stores.MANIFEST).write_text(json.dumps(stale), encoding="utf-8")
    assert not stores.installed(tmp_path)


def test_an_image_baked_before_the_contrib_pin_still_serves_the_check(stores, tmp_path):
    """The check needs postgres, redis and s3 at their pins; the labs' contrib archive is optional."""
    assert set(stores.CHECK_ARTEFACTS) == set(stores.ARTEFACTS) - {"postgres-contrib"}
    older = {name: pin for name, pin in stores.pins().items() if name != "postgres-contrib"}
    (tmp_path / stores.MANIFEST).write_text(json.dumps(older), encoding="utf-8")
    assert stores.installed(tmp_path, stores.CHECK_ARTEFACTS)
    assert not stores.installed(tmp_path)
    older["postgres"] = {**older["postgres"], "sha256": "0" * 64}
    (tmp_path / stores.MANIFEST).write_text(json.dumps(older), encoding="utf-8")
    assert not stores.installed(tmp_path, stores.CHECK_ARTEFACTS)
    (tmp_path / stores.MANIFEST).write_text("[]", encoding="utf-8")
    assert not stores.installed(tmp_path, stores.CHECK_ARTEFACTS)


def test_the_check_starts_on_an_image_without_contrib_and_the_smoke_test_refuses_it(stores, tmp_path, monkeypatch):
    older = {name: pin for name, pin in stores.pins().items() if name != "postgres-contrib"}
    (tmp_path / stores.MANIFEST).write_text(json.dumps(older), encoding="utf-8")
    monkeypatch.setattr(stores, "STORES_DIR", tmp_path)
    started = []

    def fake_start_all(dest, run_dir, spawn):
        started.append(dest)
        raise SystemExit("stop here")

    monkeypatch.setattr(stores, "start_all", fake_start_all)
    with pytest.raises(SystemExit, match="stop here"):
        stores.main(["tests/test_x.py"])
    assert started == [tmp_path]
    with pytest.raises(SystemExit, match="python-dev image"):
        stores.main(["--smoke"])


def test_the_contrib_extensions_land_in_the_pgserver_tree(stores):
    prefix = "postgresql-16.14.0-x86_64-unknown-linux-gnu/"
    names = [prefix, f"{prefix}lib/libpq.so", f"{prefix}lib/pgcrypto.so", f"{prefix}share/tsearch_data/english.stop"]
    for extension in stores.CONTRIB_EXTENSIONS:
        names += [
            f"{prefix}lib/{extension}.so",
            f"{prefix}share/extension/{extension}.control",
            f"{prefix}share/extension/{extension}--1.0.sql",
            f"{prefix}share/extension/{extension}--1.0--1.1.sql",
        ]
    names.append(f"{prefix}share/tsearch_data/unaccent.rules")
    members = stores.contrib_members(names)
    assert members[f"{prefix}lib/unaccent.so"] == "lib/postgresql/unaccent.so"
    assert (
        members[f"{prefix}share/extension/unaccent--1.0--1.1.sql"]
        == "share/postgresql/extension/unaccent--1.0--1.1.sql"
    )
    assert members[f"{prefix}share/tsearch_data/unaccent.rules"] == "share/postgresql/tsearch_data/unaccent.rules"
    # Only the listed extensions: pgcrypto needs libcrypto, which the image may not carry.
    assert not any("pgcrypto" in name or "libpq" in name or "english.stop" in name for name in members)


def test_a_contrib_archive_without_an_extension_stops_the_install(stores):
    with pytest.raises(SystemExit, match=r"unaccent\.so"):
        stores.contrib_members(["pg/share/extension/unaccent.control"])


class _Body:
    def __init__(self, data: bytes):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self.data


def _opener(answers):
    calls = []

    def urlopen(url, timeout):
        calls.append(url)
        answer = answers[len(calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return _Body(answer)

    return urlopen, calls


def _http_error(stores, code):
    return stores.urllib.error.HTTPError("https://example.test/a.tar.gz", code, "error", None, None)


def test_fetch_retries_a_transient_server_error_with_backoff(stores, monkeypatch):
    data = b"artefact"
    spec = ("https://example.test/a.tar.gz", "sha256", stores.sha256(data).hexdigest())
    urlopen, calls = _opener([_http_error(stores, 504), stores.urllib.error.URLError("reset"), data])
    monkeypatch.setattr(stores.urllib.request, "urlopen", urlopen)
    slept = []
    assert stores.fetch(spec, sleep=slept.append) == data
    assert len(calls) == 3
    assert slept == [stores.FETCH_BACKOFF_SECONDS, stores.FETCH_BACKOFF_SECONDS * 2]


def test_fetch_gives_up_after_the_last_attempt(stores, monkeypatch):
    spec = ("https://example.test/a.tar.gz", "sha256", "0" * 64)
    urlopen, calls = _opener([_http_error(stores, 503)] * stores.FETCH_ATTEMPTS)
    monkeypatch.setattr(stores.urllib.request, "urlopen", urlopen)
    with pytest.raises(stores.urllib.error.HTTPError):
        stores.fetch(spec, sleep=lambda _delay: None)
    assert len(calls) == stores.FETCH_ATTEMPTS


def test_fetch_does_not_retry_a_client_error(stores, monkeypatch):
    spec = ("https://example.test/a.tar.gz", "sha256", "0" * 64)
    urlopen, calls = _opener([_http_error(stores, 404)])
    monkeypatch.setattr(stores.urllib.request, "urlopen", urlopen)
    with pytest.raises(stores.urllib.error.HTTPError):
        stores.fetch(spec, sleep=lambda _delay: None)
    assert len(calls) == 1


def test_without_the_stores_the_check_says_the_image_is_too_old(stores, tmp_path, monkeypatch):
    monkeypatch.setattr(stores, "STORES_DIR", tmp_path)
    with pytest.raises(SystemExit) as exc:
        stores.main(["tests/test_x.py"])
    assert "python-dev image" in str(exc.value)


def test_the_check_exports_what_ci_exports_to_the_integration_tests(stores):
    """Every store variable of ci.yml's integration job, same names, loopback."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = ci.split("- name: Run integration tests", 1)[1].split("run:", 1)[0]
    ci_env = dict(re.findall(r'^\s+([A-Z0-9_]+): "([^"]*)"', job, re.M))
    env = {**stores.CHECK_ENV, **stores.environment()}
    for name in (
        "DATABASE_URL",
        "REDIS_URL",
        "S3_ENDPOINT",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "S3_USE_SSL",
        "SECRET_KEY",
        "ADMIN_EMAIL",
        "ADMIN_PASSWORD",
    ):
        assert name in env, name
        assert env[name] == ci_env[name].replace("localhost", "127.0.0.1"), name


def test_the_backend_integration_check_runs_the_stores_then_the_integration_tests():
    specs = checks.load_checks((ROOT / checks.CHECKS_FILE).read_text(encoding="utf-8"))
    spec = specs["backend-integration"]
    assert spec.image == "python-dev" and spec.cwd == "backend" and spec.report == "junit"
    assert list(spec.argv[:4]) == ["python", "../.xtm-one/stores.py", "-m", "integration"]
    assert (ROOT / spec.cwd / spec.argv[1]).resolve() == ROOT / ".xtm-one" / "stores.py"


def test_python_dev_bakes_the_stores_from_a_digest_pinned_fetch_stage():
    text = (ROOT / "backend" / "sandbox" / "Dockerfile.sandbox.dev").read_text(encoding="utf-8")
    fetch = text.split(" AS stores-fetch", 1)
    assert len(fetch) == 2 and "@sha256:" in fetch[0].rsplit("FROM ", 1)[1]
    assert "COPY .xtm-one/stores.py /tmp/stores.py" in fetch[1]
    assert "python /tmp/stores.py install /opt/stores" in fetch[1]
    python_dev = text.split(" AS python-dev", 1)[1].split(" AS node-builder", 1)[0]
    assert "COPY --from=stores-fetch /opt/stores /opt/stores" in python_dev
    # Copied before the USER switch: root owns the stores, sandboxuser runs them.
    assert python_dev.index("COPY --from=stores-fetch") < python_dev.index("USER sandboxuser")


def test_the_image_smoke_test_starts_every_store_offline():
    workflow = (ROOT / ".github" / "workflows" / "sandbox-images.yml").read_text(encoding="utf-8")
    assert '".xtm-one/stores.py"' in workflow.split("jobs:", 1)[0]
    step = workflow.split("- name: Assert the integration stores start offline", 1)[1].split("- name:", 1)[0]
    assert "--network none" in step and "stores.py --smoke" in step


def test_the_hardening_doc_names_the_stores_exception():
    doc = (ROOT / "dev-docs" / "sandbox-image-hardening.md").read_text(encoding="utf-8")
    assert "/opt/stores" in doc


def test_the_lab_takes_its_stores_from_the_shared_module():
    lab = (ROOT / ".xtm-one" / "lab" / "lab.py").read_text(encoding="utf-8")
    assert "import stores" in lab and "stores.start_all(" in lab
    assert "PGSERVER" not in lab and "DRAGONFLY" not in lab and "SILO" not in lab


def test_a_lost_runs_stores_are_reset_before_the_next_run_and_a_labs_are_kept(stores, tmp_path):
    """A run cut by a restart left its stores alive: the next run ends them and removes its data; a lab's stay."""
    import subprocess

    dest = tmp_path / "opt" / "stores"
    (dest / "bin").mkdir(parents=True)
    # A store process keeps its own executable as argv[0] (postgres, dragonfly and silo do): a link to Python here.
    server = dest / "bin" / "store-server"
    server.symlink_to(sys.executable)
    sleeper = ["-c", "import time; time.sleep(60)"]
    tmp = tmp_path / "tmp"
    lost_dir = tmp / f"{stores.RUN_DIR_PREFIX}lost"
    lost_dir.mkdir(parents=True)
    lab_root = tmp_path / "lab"
    lab_root.mkdir()
    leftover = subprocess.Popen([str(server), *sleeper, str(lost_dir / "pgdata")])  # noqa: S603 - test-owned paths
    lab = subprocess.Popen([str(server), *sleeper, str(lab_root / "pgdata")])  # noqa: S603 - test-owned paths
    try:
        assert stores.leftovers(dest, tmp) == [leftover.pid]
        line = stores.reset_leftovers(dest, tmp)
        assert "1 store process(es) ended" in line and "1 data directory removed" in line, line
        assert leftover.wait(timeout=15) is not None
        assert lab.poll() is None
        assert not lost_dir.exists()
        assert stores.reset_leftovers(dest, tmp) == ""
    finally:
        for process in (leftover, lab):
            process.kill()
            process.wait()


def test_the_check_resets_leftovers_before_it_starts_the_stores(stores, tmp_path, monkeypatch):
    (tmp_path / stores.MANIFEST).write_text(json.dumps(stores.pins()), encoding="utf-8")
    monkeypatch.setattr(stores, "STORES_DIR", tmp_path)
    order = []
    monkeypatch.setattr(stores, "reset_leftovers", lambda dest: order.append(("reset", dest)) or "")

    def fake_start_all(dest, run_dir, spawn):
        order.append(("start", dest))
        raise SystemExit("stop here")

    monkeypatch.setattr(stores, "start_all", fake_start_all)
    with pytest.raises(SystemExit, match="stop here"):
        stores.main(["tests/test_x.py"])
    assert order == [("reset", tmp_path), ("start", tmp_path)]


def _recorder(calls: list[list[str]]) -> Callable[[list[str]], str]:
    def run(argv: list[str]) -> str:
        calls.append(argv)
        return ""

    return run


def test_a_cluster_left_by_a_previous_run_is_kept_without_initdb(
    stores: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #12198: a service restarted in place met its own pgdata and died at initdb.
    calls: list[list[str]] = []
    monkeypatch.setattr(stores, "_run", _recorder(calls))
    data = tmp_path / "pgdata"
    data.mkdir()
    (data / "PG_VERSION").write_text("17\n")
    (data / "postmaster.pid").write_text("4242\n")
    assert stores.init_cluster(tmp_path / "bin", data) is False
    assert calls == [] and (data / "PG_VERSION").is_file() and not (data / "postmaster.pid").exists()


def test_an_empty_or_half_written_data_directory_is_initialised(
    stores: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(stores, "_run", _recorder(calls))
    data = tmp_path / "pgdata"
    data.mkdir()
    (data / "base").mkdir()  # no PG_VERSION: an init cut halfway
    assert stores.init_cluster(tmp_path / "bin", data) is True
    assert calls and calls[0][0].endswith("initdb") and not (data / "base").exists()


def test_a_database_is_created_only_when_missing(stores: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[list[str]] = []
    monkeypatch.setattr(stores, "_run", _recorder(created))
    answers = iter(["1\n", ""])
    monkeypatch.setattr(
        stores.subprocess,
        "run",
        lambda argv, **kwargs: stores.subprocess.CompletedProcess(argv, 0, stdout=next(answers), stderr=""),
    )
    assert stores.ensure_database(tmp_path, "openaev") is False and created == []
    assert stores.ensure_database(tmp_path, "openaev") is True
    assert created[0][0].endswith("createdb") and created[0][-1] == "openaev"


def test_the_pr_head_labs_create_their_database_idempotently() -> None:
    for script in ("openaev_pr_head.py", "opencrq_pr_head.py"):
        text = (ROOT / ".xtm-one" / "lab" / script).read_text(encoding="utf-8")
        assert "stores.ensure_database(" in text and "CREATE DATABASE" not in text
        assert 'lab.run("createdb"' not in text
