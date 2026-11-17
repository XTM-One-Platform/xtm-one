"""Sandbox services: the ``[service.*]`` declaration, the start / status / logs / stop runtime, the tools.

The runtime is tested against a fake container: its files live in a dict, a
background command is recorded and marks its port open when the declared argv
says so, the readiness probe answers from the open ports, and
``get_endpoint`` answers the URL an OpenSandbox server behind its proxy gives.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.sandbox_workspace import checks, preview, remote, services
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope

SERVICES = """
[check.lint]
argv = ["ruff", "check"]

[service.db]
argv = ["db-server", "--port", "5432"]
port = 5432
image = "python-dev"
timeout = 30

[service.lab]
description = "the product from the checkout"
argv = ["start-lab"]
cwd = "backend"
port = 3100
ready_path = "/api/health"
image = "python-dev"
timeout = 99999
lifetime = 600
depends_on = ["db"]
env = { ENABLED_FEATURES = "*", PORT = 3100 }
"""


# -- the declaration -----------------------------------------------------------


def test_load_services_reads_every_field_and_bounds_the_timeouts():
    specs = checks.load_services(SERVICES)
    assert list(specs) == ["db", "lab"]
    lab = specs["lab"]
    assert lab.argv == ("start-lab",) and lab.cwd == "backend" and lab.port == 3100 and lab.ready_path == "/api/health"
    assert lab.timeout == checks.SERVICE_MAX_TIMEOUT and lab.lifetime == 600 and lab.depends_on == ("db",)
    assert dict(lab.env) == {"ENABLED_FEATURES": "*", "PORT": "3100"}
    assert specs["db"].lifetime == checks.SERVICE_DEFAULT_LIFETIME and specs["db"].ready_path == ""
    assert "- lab (the product from the checkout): port 3100, after db" in checks.describe_services(specs)
    # The checks of the same file are read as before.
    assert list(checks.load_checks(SERVICES)) == ["lint"]


def test_a_service_argv_item_may_hold_a_long_script_and_the_bound_is_stated():
    script = "echo " + "x" * 1995
    specs = checks.load_services(f'[service.a]\nargv = ["sh", "-c", "{script}"]\nport = 1\n')
    assert len(specs["a"].argv[2]) == 2000
    too_long = "x" * (checks.SERVICE_MAX_ARG_CHARS + 1)
    with pytest.raises(checks.ChecksError) as refused:
        checks.load_services(f'[service.a]\nargv = ["sh", "-c", "{too_long}"]\nport = 1\n')
    text = str(refused.value)
    assert f"items of {checks.SERVICE_MAX_ARG_CHARS} characters" in text
    assert f"the longest {checks.SERVICE_MAX_ARG_CHARS + 1} characters" in text and "script.sh" in text
    # A check keeps its own bound.
    with pytest.raises(checks.ChecksError, match=f"items of {checks.MAX_ARG_CHARS} characters"):
        checks.load_checks(f'[check.a]\nargv = ["{"x" * (checks.MAX_ARG_CHARS + 1)}"]\n')


async def test_a_start_without_a_service_lists_the_declared_services():
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", SERVICES.encode())])
    listing = await services.list_declared(workspace, checkout="co")
    assert listing.startswith("2 service(s) declared in") and not listing.startswith("Error")
    assert "- db: port 5432" in listing and "- lab (the product from the checkout): port 3100, after db" in listing
    await workspace.put_tree([("bare/.xtm-one/checks.toml", b'[check.a]\nargv = ["x"]\n')])
    assert (await services.list_declared(workspace, checkout="bare")).endswith("declares no service.")


def test_a_file_without_services_declares_none():
    assert checks.load_services('[check.a]\nargv = ["x"]\n') == {}
    assert checks.describe_services({}) == "(none)"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[service.a]\nport = 1\n", "needs argv"),
        ("[service.a]\nargv = ['x']\n", "needs port"),
        ("[service.a]\nargv = ['x']\nport = 70000\n", "needs port"),
        ("[service.a]\nargv = ['x']\nport = 1\nready_path = 'health'\n", "ready_path must be a path"),
        ("[service.a]\nargv = ['x']\nport = 1\nready_log = \"a\\nb\"\n", "ready_log must be one line"),
        ("[service.a]\nargv = ['x']\nport = 1\nready_log = 1\n", "ready_log must be one line"),
        ("[service.a]\nargv = ['x']\nport = 1\nsession_log = \"a\\nb\"\n", "session_log must be one line"),
        ("[service.a]\nargv = ['x']\nport = 1\ncwd = '../x'\n", "must stay inside the checkout"),
        ("[service.a]\nargv = ['x']\nport = 1\ntimeout = 0\n", "timeout must be a positive"),
        ("[service.a]\nargv = ['x']\nport = 1\nenv = { lower = 'x' }\n", "upper-case"),
        ("[service.a]\nargv = ['x']\nport = 1\nenv = 'x'\n", "env must be a table"),
        ("[service.a]\nargv = ['x']\nport = 1\ndepends_on = ['b']\n", "depends on 'b', which is not declared"),
    ],
)
def test_a_bad_service_is_refused_with_the_reason(text, message):
    with pytest.raises(checks.ChecksError, match=message):
        checks.load_services(text)


def test_the_chain_puts_dependencies_first_and_refuses_cycles_mixed_images_and_shared_ports():
    specs = checks.load_services(SERVICES)
    assert [s.name for s in checks.service_chain(specs, "lab")] == ["db", "lab"]
    cyclic = checks.load_services("[service.a]\nargv=['x']\nport=1\ndepends_on=['b']\n[service.b]\nargv=['x']\nport=2\ndepends_on=['a']\n")
    with pytest.raises(checks.ChecksError, match="cycle"):
        checks.service_chain(cyclic, "a")
    mixed = checks.load_services("[service.a]\nargv=['x']\nport=1\nimage='p'\n[service.b]\nargv=['x']\nport=2\nimage='n'\ndepends_on=['a']\n")
    with pytest.raises(checks.ChecksError, match="same image"):
        checks.service_chain(mixed, "b")
    shared = checks.load_services("[service.a]\nargv=['x']\nport=1\n[service.b]\nargv=['x']\nport=1\ndepends_on=['a']\n")
    with pytest.raises(checks.ChecksError, match="different ports"):
        checks.service_chain(shared, "b")


# -- the runtime -----------------------------------------------------------------


class _Container:
    def __init__(self, image: str, opens: dict[str, int]) -> None:
        self.id = f"sbx-{uuid.uuid4().hex[:8]}"
        self.image = image
        self.files = self
        self.commands = self
        self.store: dict[str, bytes] = {}
        self.open_ports: set[int] = set()
        self.launched: list[tuple[str, object]] = []
        self.killed = False
        self.renewed = 0
        self._opens = opens

    async def create_directories(self, entries) -> None:
        return None

    async def write_file(self, path: str, data: bytes) -> None:
        self.store[path] = data

    async def read_bytes(self, path: str) -> bytes:
        if path not in self.store:
            raise FileNotFoundError(path)
        return self.store[path]

    async def run(self, command: str, opts=None):
        if getattr(opts, "background", False):
            self.launched.append((command, opts))
            for marker, port in self._opens.items():
                if command.startswith(marker):
                    self.open_ports.add(port)
                    log = command.rsplit(">> ", 1)[1].split(" ", 1)[0]
                    self.store[log] = f"{marker} listening on {port}\n".encode()
            return SimpleNamespace(exit_code=0)
        port = next((p for p in self.open_ports if f"{p}" in command), None)
        return SimpleNamespace(exit_code=0 if port is not None else 1)

    async def get_endpoint(self, port: int):
        if port not in self.open_ports:
            raise RuntimeError("port not published")
        return SimpleNamespace(endpoint=f"10.1.2.3:{port}", headers={"OPEN-SANDBOX-API-KEY": "k"})

    async def renew(self, _timeout) -> None:
        self.renewed += 1

    async def kill(self) -> None:
        self.killed = True

    async def close(self) -> None:
        return None


class _Redis:
    """The few Redis calls the preview registry makes, in a dict."""

    def __init__(self) -> None:
        self.keys: dict[str, int] = {}

    async def set(self, key, _value, ex=None):
        self.keys[key] = ex

    async def delete(self, key):
        self.keys.pop(key, None)

    async def exists(self, key):
        return int(key in self.keys)


@pytest.fixture
def redis(monkeypatch):
    fake = _Redis()

    async def _client():
        return fake

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    monkeypatch.setattr(preview, "_allowed_cache", {})
    return fake


@pytest.fixture
def api(monkeypatch, redis):
    monkeypatch.setattr(settings, "opensandbox_protocol", "https")
    monkeypatch.setattr(settings, "opensandbox_domain", "sandbox.example:8090")
    monkeypatch.setattr(settings, "opensandbox_api_key", "server-key")

    async def _nothing(*_a, **_k):
        return None

    monkeypatch.setattr("app.services.quota.enforce_combined_sandbox_quota", _nothing)
    monkeypatch.setattr("app.services.quota.increment_sandbox_usage", _nothing)
    monkeypatch.setattr(remote, "install_tool", _nothing)

    async def _reconcile(*_a, **_k):
        return None

    monkeypatch.setattr(remote, "reconcile", _reconcile)
    containers: dict[str, _Container] = {}
    opens = {"db-server": 5432, "start-lab": 3100}

    async def create(image: str, _lifetime):
        container = _Container(image, opens)
        containers[container.id] = container
        return container

    async def connect(sandbox_id: str):
        container = containers.get(sandbox_id)
        if container is None or container.killed:
            raise RuntimeError("no such sandbox")
        return container

    return SimpleNamespace(create=create, connect=connect, containers=containers, opens=opens)


def _workspace() -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.UUID(int=7), conversation_id=uuid.uuid4())
    return WorkspaceSession(scope, store=MemoryWorkspaceStore(), limits=Limits(quota_bytes=8 * 1024 * 1024, max_files=200))


async def _no_sleep(_seconds):
    return None


async def _resolved(workspace, service="lab"):
    await workspace.put_tree([("co/.xtm-one/checks.toml", SERVICES.encode())])
    return await services.resolve(workspace, checkout="co", service=service, known_images={"python", "python-dev"})


async def test_a_service_starts_after_its_dependencies_and_answers_a_preview_url(api, redis):
    workspace = _workspace()
    resolved = await _resolved(workspace)
    assert [s.name for s in resolved.chain] == ["db", "lab"] and resolved.image == "python-dev" and resolved.key == "co:lab"
    outcome = await services.start_service(resolved, workspace=workspace, sandbox_api=api, sleep=_no_sleep)
    assert outcome.ready and outcome.started == ["db", "lab"]
    (container,) = api.containers.values()
    assert outcome.service_id == container.id
    assert outcome.preview_url == f"https://sandbox.example:8090/sandboxes/{container.id}/proxy/3100/"
    assert f"preview_url: {outcome.preview_url}" in outcome.text and "OPEN-SANDBOX-API-KEY" not in outcome.text
    (db_cmd, _db_opts), (_lab_cmd, lab_opts) = container.launched
    assert db_cmd.startswith("db-server --port 5432 >> /tmp/.xtm/services/db.log 2>&1")
    assert lab_opts.working_directory == f"{remote.REMOTE_WORKSPACE}/co/backend"
    assert lab_opts.envs["ENABLED_FEATURES"] == "*" and lab_opts.envs["CI"] == "1"
    held = (await workspace.record(create=False)).kernel_state["services"]["co:lab"]
    assert held["sandbox_id"] == container.id and held["preview_url"] == outcome.preview_url and held["lifetime"] == 600
    # The preview is admitted for the service's lifetime, and only it.
    assert redis.keys == {f"sandbox_preview:{container.id}:3100": 600}
    assert await preview.allowed(outcome.preview_url + "agents?tab=1")
    assert not await preview.allowed(f"https://sandbox.example:8090/sandboxes/{container.id}/proxy/5432/")
    await services.stop_service(workspace, outcome.service_id, sandbox_api=api)
    assert redis.keys == {} and not await preview.allowed(outcome.preview_url)


async def test_a_running_service_is_answered_again_and_a_dead_one_restarted(api):
    workspace = _workspace()
    first = await services.start_service(await _resolved(workspace), workspace=workspace, sandbox_api=api, sleep=_no_sleep)
    again = await services.start_service(await _resolved(workspace), workspace=workspace, sandbox_api=api, sleep=_no_sleep)
    assert again.service_id == first.service_id and "already running" in again.text and len(api.containers) == 1
    await api.containers[first.service_id].kill()
    third = await services.start_service(await _resolved(workspace), workspace=workspace, sandbox_api=api, sleep=_no_sleep)
    assert third.ready and third.service_id != first.service_id and len(api.containers) == 2


async def test_a_service_that_never_answers_is_reported_with_where_to_read_its_logs(api):
    api.opens.pop("start-lab")
    workspace = _workspace()
    resolved = await _resolved(workspace)
    ticks = iter(range(0, 100000, 400))
    outcome = await services.start_service(
        resolved, workspace=workspace, sandbox_api=api, sleep=_no_sleep, clock=lambda: float(next(ticks))
    )
    assert not outcome.ready and outcome.started == ["db", "lab"]
    assert "lab did not answer on port 3100" in outcome.text and "sandbox_service_logs" in outcome.text
    # Kept so its logs can be read; the stop discards it.
    (container,) = api.containers.values()
    assert not container.killed
    logs = await services.service_logs(workspace, outcome.service_id, sandbox_api=api)
    assert "== db (last 200 lines" in logs.text and "db-server listening on 5432" in logs.text and "== lab: no output yet ==" in logs.text
    stopped = await services.stop_service(workspace, outcome.service_id, sandbox_api=api)
    assert "stopped" in stopped.text and container.killed
    assert "co:lab" not in (await workspace.record(create=False)).kernel_state["services"]


async def _resolved_with_ready_log(workspace):
    text = SERVICES.replace('ready_path = "/api/health"\n', 'ready_path = "/api/health"\nready_log = "lab up: "\n')
    await workspace.put_tree([("co/.xtm-one/checks.toml", text.encode())])
    return await services.resolve(workspace, checkout="co", service="lab", known_images={"python", "python-dev"})


async def test_a_service_with_ready_log_is_ready_only_once_its_output_holds_it(api):
    # The lab answers on its port, then seeds and prints its URLs: the start waits for its "lab up" line, so the
    # first logs read after the answer holds the seed lines (dev:lab-start-log-flush).
    workspace = _workspace()
    resolved = await _resolved_with_ready_log(workspace)
    assert resolved.spec.ready_log == "lab up: " and resolved.chain[0].ready_log == ""
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 3:
            (container,) = api.containers.values()
            container.store[services.log_path("lab")] += (
                b"[lab 43.0s] seeded signed-in session (administrator): http://localhost:3100/x\n"
                b"[lab 43.1s] lab up: sign in as lab@filigran.io\n"
            )

    outcome = await services.start_service(resolved, workspace=workspace, sandbox_api=api, sleep=sleep)
    assert outcome.ready and len(slept) == 3, outcome.text
    logs = await services.service_logs(workspace, outcome.service_id, service="lab", sandbox_api=api)
    assert "seeded signed-in session (administrator)" in logs.text and "lab up: " in logs.text


async def test_a_service_that_never_logs_its_ready_log_is_not_ready_and_says_so(api):
    workspace = _workspace()
    resolved = await _resolved_with_ready_log(workspace)
    ticks = iter(range(0, 100000, 400))
    outcome = await services.start_service(
        resolved, workspace=workspace, sandbox_api=api, sleep=_no_sleep, clock=lambda: float(next(ticks))
    )
    assert not outcome.ready and "and log 'lab up: '" in outcome.text and "sandbox_service_logs" in outcome.text


async def test_a_second_start_of_a_service_still_seeding_waits_for_its_ready_log(api):
    # The first start's answer was lost (a run resume): its record has no ready_seconds. The second start must not
    # answer "already running" while the service still seeds; it waits for "lab up" as a fresh start does.
    workspace = _workspace()
    resolved = await _resolved_with_ready_log(workspace)
    ticks = iter(range(0, 100000, 400))
    first = await services.start_service(
        resolved, workspace=workspace, sandbox_api=api, sleep=_no_sleep, clock=lambda: float(next(ticks))
    )
    assert not first.ready
    assert "ready_seconds" not in (await workspace.record(create=False)).kernel_state["services"]["co:lab"]
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            api.containers[first.service_id].store[services.log_path("lab")] += (
                b"[lab 43.0s] seeded signed-in session (administrator): http://localhost:3100/x\n"
                b"[lab 43.1s] lab up: sign in as lab@filigran.io\n"
            )

    again = await services.start_service(resolved, workspace=workspace, sandbox_api=api, sleep=sleep)
    assert again.ready and again.service_id == first.service_id and "already running" in again.text, again.text
    assert len(slept) == 2 and len(api.containers) == 1
    logs = await services.service_logs(workspace, again.service_id, service="lab", sandbox_api=api)
    assert "seeded signed-in session (administrator)" in logs.text


def test_start_window_left_counts_from_the_recorded_start():
    now = datetime.now(UTC)
    assert 49 < services.start_window_left({"started_at": (now - timedelta(seconds=10)).isoformat()}, 60) <= 50
    assert services.start_window_left({"started_at": (now - timedelta(seconds=90)).isoformat()}, 60) == 0.0
    assert services.start_window_left({}, 60) == 0.0 and services.start_window_left({"started_at": "x"}, 60) == 0.0


async def test_status_logs_and_stop_find_the_service_by_id(api):
    workspace = _workspace()
    started = await services.start_service(await _resolved(workspace), workspace=workspace, sandbox_api=api, sleep=_no_sleep)
    status = await services.service_status(workspace, started.service_id, sandbox_api=api)
    assert status.ready and "answering on port 3100" in status.text and started.preview_url in status.text
    assert api.containers[started.service_id].renewed >= 1
    one = await services.service_logs(workspace, started.service_id, service="lab", lines=5, sandbox_api=api)
    assert "start-lab listening on 3100" in one.text and "== db" not in one.text
    bad = await services.service_logs(workspace, started.service_id, service="nope", sandbox_api=api)
    assert bad.refused
    missing = await services.service_status(workspace, "sbx-unknown", sandbox_api=api)
    assert not missing.refused and missing.text == "sbx-unknown: not running (stopped or never started in this workspace)."
    await services.stop_service(workspace, started.service_id, sandbox_api=api)
    # The read-back after a stop is a normal state, never an error (#10589).
    after = await services.service_status(workspace, started.service_id, sandbox_api=api)
    assert not after.refused and "not running" in after.text and not after.text.startswith("Error")
    again = await services.stop_service(workspace, started.service_id, sandbox_api=api)
    assert again.refused


@pytest.mark.parametrize("service_id", ["", "   ", "sbx one"])
async def test_status_of_an_empty_or_malformed_service_id_stays_a_refusal(api, service_id):
    outcome = await services.service_status(_workspace(), service_id, sandbox_api=api)
    assert outcome.refused and outcome.text.startswith("Error:")


async def test_an_undeclared_service_or_unknown_image_is_refused_with_the_declared_list(api):
    workspace = _workspace()
    with pytest.raises(checks.ChecksError, match="declares no service"):
        await services.resolve(workspace, checkout="co", service="lab", known_images={"python"})
    await workspace.put_tree([("co/.xtm-one/checks.toml", SERVICES.encode())])
    with pytest.raises(checks.ChecksError, match=r"'zzz' is not declared.*\n- db: port 5432"):
        await services.resolve(workspace, checkout="co", service="zzz", known_images={"python-dev"})
    with pytest.raises(checks.ChecksError, match="image key 'python-dev', which OPENSANDBOX_IMAGES does not configure"):
        await services.resolve(workspace, checkout="co", service="lab", known_images={"python"})


NEEDS = """
[service.db]
argv = ["db-server", "--port", "5432"]
port = 5432
image = "python-dev"
needs = ["db/schema.sql"]

[service.lab]
argv = ["start-lab"]
port = 3100
image = "python-dev"
depends_on = ["db"]
needs = [".xtm-one/lab/", "backend", "frontend", "backend"]
"""


def test_needs_is_read_cleaned_and_bounded():
    specs = checks.load_services(NEEDS)
    assert specs["lab"].needs == (".xtm-one/lab", "backend", "frontend") and specs["db"].needs == ("db/schema.sql",)
    assert checks.load_services(SERVICES)["lab"].needs == ()
    for bad, match in (
        ('needs = "backend"', "needs must be a list of paths"),
        ("needs = [3]", "needs must be a list of paths"),
        ('needs = ["../outside"]', "must be a relative path inside the checkout"),
        ('needs = ["/etc"]', "must be a relative path inside the checkout"),
        ('needs = ["."]', "names the checkout itself"),
        (f"needs = {[f'p{i}' for i in range(checks.SERVICE_MAX_NEEDS + 1)]!r}".replace("'", '"'), "at most"),
    ):
        with pytest.raises(checks.ChecksError, match=match):
            checks.load_services(f'[service.x]\nargv = ["x"]\nport = 1\n{bad}\n')


async def test_a_service_whose_checkout_lacks_a_needed_path_is_refused_before_any_container(api):
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", NEEDS.encode()), ("co/frontend/package.json", b"{}")])
    with pytest.raises(checks.ChecksError) as refused:
        await services.resolve(workspace, checkout="co", service="lab", known_images={"python-dev"})
    text = str(refused.value)
    # The dependency's needs come first, then the service's own; a path the checkout holds is not named.
    assert "needs db/schema.sql, .xtm-one/lab, backend in the checkout" in text and "frontend" not in text
    assert 'materialize_repository(path="co", add_paths=["db/schema.sql", ".xtm-one/lab", "backend"])' in text
    assert "Nothing was started" in text and api.containers == {}
    # A path held by a file below it, or as the file itself, satisfies the need; a sibling prefix does not.
    await workspace.put_tree(
        [
            ("co/db/schema.sql", b"--"),
            ("co/.xtm-one/lab/lab.py", b"#"),
            ("co/backendish/x.py", b"#"),
            ("other/backend/app.py", b"#"),
        ]
    )
    with pytest.raises(checks.ChecksError, match=r'add_paths=\["backend"\]'):
        await services.resolve(workspace, checkout="co", service="lab", known_images={"python-dev"})
    await workspace.put_tree([("co/backend/app/main.py", b"#")])
    resolved = await services.resolve(workspace, checkout="co", service="lab", known_images={"python-dev"})
    assert [s.name for s in resolved.chain] == ["db", "lab"]


async def test_a_narrow_checkout_record_decides_needs_not_its_companion_files(api):
    # materialize_repository keeps companion files outside sub_paths (frontend/scripts/*.mjs) so the
    # checks are detected; they must not make frontend look held (#9862).
    from app.services.sandbox_workspace.checkout import RECORD_NAME, build_record

    workspace = _workspace()
    held = [
        (".xtm-one/checks.toml", NEEDS.encode()),
        (".xtm-one/lab/lab.py", b"#"),
        ("db/schema.sql", b"--"),
        ("backend/app/main.py", b"#"),
        ("frontend/scripts/node-test-files.mjs", b"//"),
    ]
    record = build_record(
        provider="github",
        repository="o/r",
        ref="",
        commit="abc",
        path="co",
        sub_paths=[".xtm-one", "backend"],
        files=held,
    )
    await workspace.put_tree([*((f"co/{p}", d) for p, d in held), (f"co/{RECORD_NAME}", record.to_json())])
    with pytest.raises(checks.ChecksError) as refused:
        await services.resolve(workspace, checkout="co", service="lab", known_images={"python-dev"})
    # db/schema.sql is a recorded file (a companion), frontend only has companions under it.
    assert 'add_paths=["frontend"])' in str(refused.value) and api.containers == {}
    grown = build_record(
        provider="github",
        repository="o/r",
        ref="",
        commit="abc",
        path="co",
        sub_paths=[".xtm-one", "backend", "frontend"],
        files=held,
    )
    await workspace.put_tree([(f"co/{RECORD_NAME}", grown.to_json())])
    resolved = await services.resolve(workspace, checkout="co", service="lab", known_images={"python-dev"})
    assert [s.name for s in resolved.chain] == ["db", "lab"]
    # A whole-tree record (no sub_paths) holds every need.
    whole = build_record(provider="github", repository="o/r", ref="", commit="abc", path="co", sub_paths=[], files=held)
    await workspace.put_tree([(f"co/{RECORD_NAME}", whole.to_json())])
    await services.resolve(workspace, checkout="co", service="lab", known_images={"python-dev"})


async def test_a_need_held_by_a_nested_checkout_starts_with_no_add_paths(api):
    # materialize_repository of another repository into <checkout>/lab-chatbot writes its own record:
    # the outer narrow record never covers it, the nested checkout holds it (lab-chatbot recipe).
    from app.services.sandbox_workspace.checkout import RECORD_NAME, build_record

    toml = '[service.lab]\nargv = ["start-lab"]\nport = 3100\nimage = "python-dev"\nneeds = ["backend", "lab-chatbot", "vendor/pkg"]\n'
    outer = [(".xtm-one/checks.toml", toml.encode()), ("backend/app/main.py", b"#")]
    record = build_record(
        provider="github",
        repository="o/r",
        ref="",
        commit="abc",
        path="co",
        sub_paths=[".xtm-one", "backend"],
        files=outer,
    )
    inner = build_record(
        provider="github", repository="o/ui", ref="", commit="def", path="co/lab-chatbot", sub_paths=[], files=[]
    )
    workspace = _workspace()
    await workspace.put_tree(
        [
            *((f"co/{p}", d) for p, d in outer),
            (f"co/{RECORD_NAME}", record.to_json()),
            (f"co/lab-chatbot/{RECORD_NAME}", inner.to_json()),
            ("co/vendor/pkg/index.js", b"//"),
        ]
    )
    with pytest.raises(checks.ChecksError) as refused:
        await services.resolve(workspace, checkout="co", service="lab", known_images={"python-dev"})
    # A nested record alone holds nothing; a plain file outside the record and any nested checkout neither.
    assert 'add_paths=["lab-chatbot", "vendor/pkg"])' in str(refused.value) and api.containers == {}
    await workspace.put_tree([("co/lab-chatbot/packages/filigran-chatbot/package.json", b"{}")])
    with pytest.raises(checks.ChecksError, match=r'add_paths=\["vendor/pkg"\]'):
        await services.resolve(workspace, checkout="co", service="lab", known_images={"python-dev"})
    await workspace.put_tree([(f"co/vendor/{RECORD_NAME}", inner.to_json())])
    resolved = await services.resolve(workspace, checkout="co", service="lab", known_images={"python-dev"})
    assert [s.name for s in resolved.chain] == ["lab"]


# -- the preview URL policy -------------------------------------------------------


@pytest.fixture
def server(monkeypatch, redis):
    monkeypatch.setattr(settings, "opensandbox_protocol", "http")
    monkeypatch.setattr(settings, "opensandbox_domain", "copilot-opensandbox:8090")
    monkeypatch.setattr(settings, "opensandbox_api_key", "server-key")
    return redis


@pytest.mark.parametrize(
    "url",
    [
        "http://copilot-opensandbox:8090/sandboxes/sbx-1/proxy/3100",
        "http://copilot-opensandbox:8090/sandboxes/sbx-1/proxy/3100/",
        "http://COPILOT-OPENSANDBOX:8090/sandboxes/sbx-1/proxy/3100/assets/x.js?v=2",
    ],
)
def test_a_url_on_the_server_proxy_names_its_sandbox_and_port(server, url):
    assert preview.match(url) == preview.PreviewTarget("sbx-1", 3100)


@pytest.mark.parametrize(
    "url",
    [
        "https://copilot-opensandbox:8090/sandboxes/sbx-1/proxy/3100/",  # another scheme
        "http://copilot-opensandbox:8091/sandboxes/sbx-1/proxy/3100/",  # another port
        "http://evil.example/sandboxes/sbx-1/proxy/3100/",  # another host
        "http://user:pw@copilot-opensandbox:8090/sandboxes/sbx-1/proxy/3100/",  # credentials
        "http://copilot-opensandbox:8090/sandboxes",  # the lifecycle API
        "http://copilot-opensandbox:8090/sandboxes/sbx-1",
        "http://copilot-opensandbox:8090/sandboxes/sbx-1/proxy/3100/..%2f..%2fsandboxes",  # encoded traversal
        "http://copilot-opensandbox:8090/sandboxes/sbx-1/proxy/3100/../../sbx-2/proxy/1/",
        "http://copilot-opensandbox:8090/sandboxes/sbx-1/proxy/99999/",
        "file:///etc/passwd",
    ],
)
def test_anything_else_is_not_a_preview(server, url):
    assert preview.match(url) is None


async def test_only_a_registered_preview_is_allowed_and_the_key_is_added_by_the_platform(server):
    url = "http://copilot-opensandbox:8090/sandboxes/sbx-1/proxy/3100/"
    assert not await preview.allowed(url)
    await preview.register("sbx-1", 3100, 600)
    assert await preview.allowed(url) and await preview.allowed(url + "api/health")
    assert not await preview.allowed("http://copilot-opensandbox:8090/sandboxes/sbx-1/proxy/3101/")
    assert preview.request_headers() == {"OPEN-SANDBOX-API-KEY": "server-key"}
    await preview.unregister("sbx-1", 3100)
    assert not await preview.allowed(url)


async def test_a_preview_check_fails_closed_without_redis(server, monkeypatch):
    async def _down():
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.core.redis.redis_client", _down)
    assert not await preview.allowed("http://copilot-opensandbox:8090/sandboxes/sbx-1/proxy/3100/")


async def test_the_browser_route_filter_adds_the_key_to_a_preview_only(server, monkeypatch):
    from app.services import browser_tool

    await preview.register("sbx-1", 3100, 600)

    class _Route:
        def __init__(self, url):
            self.request = SimpleNamespace(url=url, headers={"accept": "*/*"})
            self.continued = None
            self.aborted = False

        async def continue_(self, headers=None):
            self.continued = headers

        async def abort(self, _reason):
            self.aborted = True

    live = _Route("http://copilot-opensandbox:8090/sandboxes/sbx-1/proxy/3100/src/main.tsx")
    await browser_tool._route_filter(live)
    assert live.continued == {"accept": "*/*", "OPEN-SANDBOX-API-KEY": "server-key"}
    public = _Route("https://example.com/")
    await browser_tool._route_filter(public)
    assert public.continued is None and not public.aborted
    private = _Route("http://10.0.0.5/")
    await browser_tool._route_filter(private)
    assert private.aborted

    async def refuse(url, **_k):
        raise ValueError(f"{url} refused")

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", refuse)
    check = browser_tool._url_check(None)
    assert await check(live.request.url) == live.request.url
    with pytest.raises(ValueError, match="refused"):
        await check("http://copilot-opensandbox:8090/sandboxes/sbx-1/proxy/3101/")


def test_the_log_tail_is_bounded():
    text = "\n".join(f"line {i}" for i in range(5000))
    assert services.tail(text, 3) == "line 4997\nline 4998\nline 4999"
    big = services.tail("x" * (services.MAX_LOG_CHARS * 2), 1)
    assert big.startswith("[... cut ...]") and len(big) <= services.MAX_LOG_CHARS + 20


def test_a_log_pattern_keeps_only_the_matching_lines_and_counts_them():
    text = "\n".join(
        ["dragonfly banner"] * 50 + ["[lab 40s] seeded 3 organizations", "yarn step", "[lab 41s] not seeded: kb sync 404"]
    )
    regex = services.compile_log_pattern("seeded|NOT SEEDED", ignore_case=True)
    block = services.log_section("lab", "container svc-lab", text, 200, regex)
    head, *body = block.splitlines()
    assert "2 of 53 lines" in head and "seeded|NOT SEEDED" in head
    assert body == ["[lab 40s] seeded 3 organizations", "[lab 41s] not seeded: kb sync 404"]
    last = services.log_section("lab", "x", text, 1, regex)
    assert "last 1 shown" in last and last.endswith("not seeded: kb sync 404") and "seeded 3" not in last
    unfiltered = services.log_section("lab", "x", text, 2, None)
    assert unfiltered.endswith("yarn step\n[lab 41s] not seeded: kb sync 404")


def test_a_log_pattern_that_matches_nothing_says_so():
    regex = services.compile_log_pattern("seeded")
    block = services.log_section("lab", "x", "one\ntwo\nSEEDED", 200, regex)
    assert block == "== lab: 0 of 3 lines of x match 'seeded' =="


def test_an_invalid_or_oversized_log_pattern_is_refused_with_its_reason():
    assert services.compile_log_pattern("") is None
    bad = services.compile_log_pattern("seeded(")
    assert isinstance(bad, str) and "not a valid regular expression" in bad and "missing )" in bad
    big = services.compile_log_pattern("a" * (services.MAX_LOG_PATTERN_CHARS + 1))
    assert isinstance(big, str) and f"at most {services.MAX_LOG_PATTERN_CHARS}" in big


async def test_service_logs_filters_by_pattern_and_refuses_an_invalid_one(api):
    workspace = _workspace()
    started = await services.start_service(await _resolved(workspace), workspace=workspace, sandbox_api=api, sleep=_no_sleep)
    hit = await services.service_logs(workspace, started.service_id, service="lab", pattern="listening", sandbox_api=api)
    assert "start-lab listening on 3100" in hit.text and "match 'listening'" in hit.text
    bad = await services.service_logs(workspace, started.service_id, pattern="(", sandbox_api=api)
    assert bad.refused and "not a valid regular expression" in bad.text


def test_the_service_tools_are_offered_on_the_opensandbox_backend_only(monkeypatch):
    from app.services.builtin_tools.media import build_code_interpreter_tool

    names = {"sandbox_start_service", "sandbox_service_status", "sandbox_service_logs", "sandbox_stop_service"}
    monkeypatch.setattr(settings, "code_interpreter_workspace_enabled", True)
    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    monkeypatch.setattr(settings, "custom_tool_sandbox", "subprocess")
    without, _ = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    assert not names & {d.name for d in without}
    monkeypatch.setattr(settings, "custom_tool_sandbox", "opensandbox")
    definitions, _ = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    offered = {d.name: d for d in definitions}
    assert names <= set(offered)
    # Without service the tool lists the declared services (#11799): only the checkout is required.
    assert offered["sandbox_start_service"].input_schema["required"] == ["checkout"]
    assert "sync" in offered["sandbox_start_service"].input_schema["properties"]
    # A repeat adopts the held service: a resumed turn runs it again, even after two cuts (#9243).
    assert offered["sandbox_start_service"].replay_safe and offered["sandbox_start_service"].cut_safe
    assert not offered["sandbox_stop_service"].cut_safe
