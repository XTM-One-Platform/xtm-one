"""The data stores of the backend's integration tests, started offline.

PostgreSQL 16 + pgvector (and the contrib extensions of CONTRIB_EXTENSIONS), a
Redis-API store and an S3 store, the three services
CI gives the ``integration`` tests (``.github/workflows/ci.yml``), started on
loopback inside one sandbox container. Standard library only.

Three uses:

``python stores.py install <dir>``
    Build time (``backend/sandbox/Dockerfile.sandbox.dev``, stage
    ``stores-fetch``): fetch each artefact below, check its digest, unpack it
    under *dir* (``/opt/stores`` in ``python-dev``) and write ``MANIFEST.json``
    with the pins. The only step that touches the network.

``python ../.xtm-one/stores.py <pytest arguments>``
    The ``backend-integration`` check of ``.xtm-one/checks.toml`` (cwd
    ``backend``): start the three stores from ``/opt/stores`` under a throwaway
    directory, export the variables CI exports, run pytest with the arguments,
    stop the stores and answer pytest's exit code. No network.

``python stores.py --smoke``
    The image smoke test (``sandbox-images.yml``, ``--network none``): start
    the three stores, check pgvector, PING and the S3 health route, stop.

The lab (``.xtm-one/lab/lab.py``) starts the same stores through
:func:`start_all`. dev-docs/sandbox-image-hardening.md names the stores as a
runtime exception of ``python-dev``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

# (url, digest algorithm, digest): an artefact whose digest differs stops the install.
# PostgreSQL 16 with pgvector 0.8 (halfvec, the HNSW indexes of the migrations need it).
PGSERVER = (
    "https://files.pythonhosted.org/packages/c4/84/6bf6fa6c7b2a30d9b8adbc3603634b0ce0c4ec14e02943cb806ed5f921a6/"
    "pixeltable_pgserver-0.7.1-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
    "sha256",
    "5577ba9112506f1eaad82ec79843fd62f9a852902acbc23fa306413aa5f28de4",
)
SILO = (
    "https://github.com/pgsty/silo/releases/download/RELEASE.2026-10-26T00-00-00Z/silo_20261026000000.0.0_linux_amd64.tar.gz",
    "sha256",
    "4119e6da79be4f1b9ec9c619f72752f2e26e7447191dba969c02e4795fed8f92",
)
DRAGONFLY = (
    "https://github.com/dragonflydb/dragonfly/releases/download/v2.0.2/dragonfly-x86_64.tar.gz",
    "sha256",
    "eb9dfe654c69ab190cb3aa329ed3599ebe19c5e9a3a437bb7bc38ef52f665c88",
)
# The pgserver build carries plpgsql and pgvector only, no contrib: a product migration that runs
# CREATE EXTENSION unaccent (OpenCRQ 0001) fails on it. The contrib modules below come from the
# PostgreSQL build of theseus-rs/postgresql-binaries at the pgserver server's own minor (16.14:
# a module of a later minor calls server symbols 16.14 lacks, hstore's DatumGetHeapTupleHeader
# under OpenAEV's V2_68 migration) and are copied into the pgserver tree at install.
PG_CONTRIB = (
    "https://github.com/theseus-rs/postgresql-binaries/releases/download/16.14.0/"
    "postgresql-16.14.0-x86_64-unknown-linux-gnu.tar.gz",
    "sha256",
    "3541705c9ca2fdbb0bfc7562b10a36aac155f18d45577d8ec6725b874ccada4a",
)
#: Contrib extensions copied from PG_CONTRIB: each needs libc alone at load.
CONTRIB_EXTENSIONS = ("unaccent", "pg_trgm", "citext", "btree_gin", "btree_gist", "hstore", "fuzzystrmatch")
ARTEFACTS = {"postgres": PGSERVER, "postgres-contrib": PG_CONTRIB, "redis": DRAGONFLY, "s3": SILO}

STORES_DIR = Path(os.environ.get("XTM_STORES", "/opt/stores"))
MANIFEST = "MANIFEST.json"
PG_PORT, REDIS_PORT, S3_PORT = 5432, 6379, 9000
S3_USER, S3_PASSWORD = "copilot", "copilot_dev_secret"

# The variables the integration job of ci.yml exports beside the stores'.
CHECK_ENV = {
    "SECRET_KEY": "ci-test-secret-key-not-for-production",
    "APP_VERSION": "0.0.0-ci",
    "ADMIN_EMAIL": "admin@test.filigran.io",
    "ADMIN_PASSWORD": "test-admin-password-123",
}

Spawn = Callable[[str, list[str], dict[str, str] | None], subprocess.Popen]


def log(message: str) -> None:
    print(f"[stores] {message}", file=sys.stderr, flush=True)


def pins() -> dict[str, dict[str, str]]:
    return {name: {"url": url, algorithm: digest} for name, (url, algorithm, digest) in ARTEFACTS.items()}


#: The artefacts the backend-integration check starts; postgres-contrib serves the product labs
#: (OpenCRQ, OpenAEV migrations) and is copied in when the image has it.
CHECK_ARTEFACTS = ("postgres", "redis", "s3")


def installed(dest: Path, names: tuple[str, ...] | None = None) -> bool:
    """Whether *dest* holds the stores of the pins above: every artefact, or those of *names*.

    Compared per artefact, so an image baked before a new artefact was pinned still serves the
    ones it holds at their exact pins.
    """
    try:
        manifest = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, dict):
        return False
    wanted = pins()
    return all(manifest.get(name) == wanted[name] for name in (names or tuple(wanted)))


def pg_bindir(dest: Path) -> Path:
    return dest / "pgserver" / "pixeltable_pgserver" / "pginstall" / "bin"


# A release host answers a transient 5xx or 429 now and then (GitHub releases: a 504 failed
# Sandbox Images run 42566778522 three times): the download is retried, a 4xx is not.
FETCH_ATTEMPTS = 4
FETCH_BACKOFF_SECONDS = 5.0


def _transient(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code >= 500 or error.code == 429
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))


def _download(url: str, sleep: Callable[[float], None] = time.sleep) -> bytes:
    """The bytes at *url*, retried with a doubling backoff on a transient error."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=300) as response:
                return response.read()
        except Exception as error:
            if attempt == FETCH_ATTEMPTS or not _transient(error):
                raise
            delay = FETCH_BACKOFF_SECONDS * 2 ** (attempt - 1)
            log(f"{url.rsplit('/', 1)[-1]}: {error}, retry {attempt}/{FETCH_ATTEMPTS - 1} in {delay:.0f}s")
            sleep(delay)
    raise AssertionError("unreachable")


def fetch(spec: tuple[str, str, str], sleep: Callable[[float], None] = time.sleep) -> bytes:
    """The bytes at *spec*'s URL, checked against its sha256."""
    url, algorithm, expected = spec
    if algorithm != "sha256":
        raise SystemExit(f"stores: {url}: only sha256 pins are supported, not {algorithm}")
    started = time.monotonic()
    data = _download(url, sleep)
    actual = sha256(data).hexdigest()
    if actual != expected:
        raise SystemExit(f"stores: {url} has sha256 {actual}, expected {expected}")
    log(f"fetched {url.rsplit('/', 1)[-1]} ({len(data) // 1_000_000} MB, {time.monotonic() - started:.1f}s)")
    return data


def _binary_from_tar(data: bytes, matches: Callable[[str], bool], target: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            archive.extractall(tmp, filter="data")
        binary = next((p for p in sorted(Path(tmp).rglob("*")) if p.is_file() and matches(p.name)), None)
        if binary is None:
            raise SystemExit(f"stores: no {target.name} binary in the archive")
        shutil.copyfile(binary, target)
    target.chmod(0o755)


def contrib_members(names: list[str]) -> dict[str, str]:
    """The members of the PG_CONTRIB archive to copy, mapped to their path under the pgserver install.

    Each extension of CONTRIB_EXTENSIONS: its module (lib/<ext>.so to lib/postgresql/), its control and
    SQL scripts (share/extension/ to share/postgresql/extension/), and the dictionaries it reads
    (share/tsearch_data/<ext>.rules, unaccent's). Raise when an extension's module or control is missing.
    """
    members: dict[str, str] = {}
    for name in names:
        parts = name.split("/", 1)
        if len(parts) != 2:
            continue
        relative = parts[1]
        for extension in CONTRIB_EXTENSIONS:
            if relative == f"lib/{extension}.so":
                members[name] = f"lib/postgresql/{extension}.so"
            elif relative.startswith("share/extension/") and relative.split("/")[-1].split("--")[0].removesuffix(".control") == extension:
                members[name] = f"share/postgresql/extension/{relative.split('/')[-1]}"
            elif relative == f"share/tsearch_data/{extension}.rules":
                members[name] = f"share/postgresql/tsearch_data/{extension}.rules"
    for extension in CONTRIB_EXTENSIONS:
        for required in (f"lib/postgresql/{extension}.so", f"share/postgresql/extension/{extension}.control"):
            if required not in members.values():
                raise SystemExit(f"stores: the contrib archive has no {required}")
    return members


def install_contrib(data: bytes, pginstall: Path) -> None:
    """Copy the contrib extensions of the PG_CONTRIB archive *data* into the pgserver install *pginstall*."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member, target in contrib_members(archive.getnames()).items():
            source = archive.extractfile(member)
            if source is None:
                raise SystemExit(f"stores: {member} is not a file in the contrib archive")
            path = pginstall / target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.read())
            path.chmod(0o755 if target.endswith(".so") else 0o644)


def install(dest: Path) -> Path:
    """Fetch and unpack the three stores under *dest* (the only step with network)."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(fetch(PGSERVER))) as wheel:
        wheel.extractall(dest / "pgserver")
    for tool in pg_bindir(dest).iterdir():
        tool.chmod(0o755)
    install_contrib(fetch(PG_CONTRIB), pg_bindir(dest).parent)
    (dest / "bin").mkdir(exist_ok=True)
    _binary_from_tar(fetch(DRAGONFLY), lambda name: name.startswith("dragonfly"), dest / "bin" / "dragonfly")
    _binary_from_tar(fetch(SILO), lambda name: name in ("silo", "minio"), dest / "bin" / "silo")
    (dest / MANIFEST).write_text(json.dumps(pins(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # The runner beside the stores (#1858): a check of ANY repository wraps its command in
    # `python /opt/stores/stores.py exec -- ...` (checks.STORES_RUNNER), its checkout holding no copy.
    shutil.copyfile(Path(__file__), dest / RUNNER)
    log(f"installed under {dest}")
    return dest


#: The runner's file name under the stores directory (``checks.STORES_RUNNER`` names its path).
RUNNER = "stores.py"
#: The role and database ``exec`` serves when the check sets none (the ``PG*`` variables of libpq).
EXEC_DEFAULTS = {"PGUSER": "copilot", "PGPASSWORD": "copilot", "PGDATABASE": "copilot"}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


def exec_environment(given: dict[str, str]) -> dict[str, str]:
    """The variables ``exec`` gives its command: libpq's ``PG*`` and a ``DATABASE_URL`` (#1858).

    A ``PGUSER`` / ``PGPASSWORD`` / ``PGDATABASE`` the check sets (a CI job's own credentials, ``app`` /
    ``admin`` on OpenCRQ) is kept: ``exec`` creates that role and database before the command runs.
    """
    env = {name: given.get(name) or default for name, default in EXEC_DEFAULTS.items()}
    for name in ("PGUSER", "PGDATABASE"):
        if not _IDENTIFIER.fullmatch(env[name]):
            raise SystemExit(f"stores: {name}={env[name]!r} is not a plain identifier")
    env.update(PGHOST="127.0.0.1", PGPORT=str(PG_PORT))
    env["DATABASE_URL"] = (
        f"postgresql://{env['PGUSER']}:{env['PGPASSWORD']}@127.0.0.1:{PG_PORT}/{env['PGDATABASE']}"
    )
    return env


def ensure_role(dest: Path, user: str, password: str) -> None:
    """Create login role *user* (superuser, as a CI service's POSTGRES_USER is) unless it exists."""
    exists = subprocess.run([*_psql(dest, "postgres"), "-v", f"name={user}"],
                            input="SELECT 1 FROM pg_roles WHERE rolname = :'name'",
                            capture_output=True, text=True)
    if exists.returncode == 0 and exists.stdout.strip() == "1":
        return
    # Through stdin, never -c: psql interpolates :"name" and :'secret' (quoted) only in what it reads.
    created = subprocess.run([*_psql(dest, "postgres"), "-v", f"name={user}", "-v", f"secret={password}"],
                             input="CREATE ROLE :\"name\" SUPERUSER LOGIN PASSWORD :'secret'",
                             capture_output=True, text=True)
    if created.returncode != 0:
        raise SystemExit(f"stores: CREATE ROLE {user} failed: {(created.stderr or created.stdout)[-2000:]}")


def run_exec(command: list[str], dest: Path = STORES_DIR) -> int:
    """Start postgres offline, give *command* its ``PG*`` variables, run it, stop (``stores.py exec -- ...``)."""
    if not command:
        raise SystemExit("usage: stores.py exec -- <command> [args...]")
    if not installed(dest, ("postgres",)):
        raise SystemExit(
            f"stores: {dest} holds no postgres at the pin of stores.py: this sandbox image predates the "
            "stores of the toolchain images (#1858); the check cannot start a database here."
        )
    env = exec_environment(dict(os.environ))
    reset = reset_leftovers(dest)
    if reset:
        log(reset)
    run_dir = Path(tempfile.mkdtemp(prefix=RUN_DIR_PREFIX))
    processes: list[subprocess.Popen] = []
    try:
        try:
            start_postgres(dest, run_dir, _file_spawn(run_dir, processes))
        except SystemExit:
            for store_log in sorted(run_dir.glob("*.log")):
                log(f"{store_log.name}: {store_log.read_text(encoding='utf-8', errors='replace')[-1500:]}")
            raise
        ensure_role(dest, env["PGUSER"], env["PGPASSWORD"])
        ensure_database(dest, env["PGDATABASE"])
        log(f"postgres ready for {env['PGUSER']}@{env['PGDATABASE']} on 127.0.0.1:{PG_PORT}")
        return subprocess.run(command, env={**os.environ, **env}).returncode
    finally:
        stop(processes)
        shutil.rmtree(run_dir, ignore_errors=True)


def environment() -> dict[str, str]:
    """The stores' variables, named as ci.yml's integration job names them."""
    return {
        "DATABASE_URL": f"postgresql+asyncpg://copilot:copilot_ci@127.0.0.1:{PG_PORT}/copilot",
        "REDIS_URL": f"redis://127.0.0.1:{REDIS_PORT}",
        "S3_ENDPOINT": f"127.0.0.1:{S3_PORT}",
        "S3_ACCESS_KEY": S3_USER,
        "S3_SECRET_KEY": S3_PASSWORD,
        "S3_USE_SSL": "false",
    }


def _run(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"stores: {Path(argv[0]).name} exited with {result.returncode}: {(result.stderr or result.stdout)[-2000:]}")
    return result.stdout


def _alive(process: subprocess.Popen, what: str) -> None:
    if process.poll() is not None:
        raise SystemExit(f"stores: {what} exited with {process.returncode} while starting")


def _wait_port(port: int, process: subprocess.Popen, what: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _alive(process, what)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise SystemExit(f"stores: {what} not listening on 127.0.0.1:{port} within {timeout:.0f}s")


def _psql(dest: Path, database: str) -> list[str]:
    return [str(pg_bindir(dest) / "psql"), "-h", "127.0.0.1", "-p", str(PG_PORT), "-U", "copilot", "-d", database,
            "-v", "ON_ERROR_STOP=1", "-qAt"]


def ensure_database(dest: Path, name: str) -> bool:
    """Create database *name* unless it exists; True when it was created (#12198).

    A service restarted in place (``sandbox_start_service`` sync with restart) finds the cluster and its databases
    of the previous run: a second ``CREATE DATABASE`` would end the restart with an error.
    """
    exists = subprocess.run([*_psql(dest, "postgres"), "-v", f"name={name}"],
                            input="SELECT 1 FROM pg_database WHERE datname = :'name'",
                            capture_output=True, text=True)
    if exists.returncode == 0 and exists.stdout.strip() == "1":
        return False
    _run([str(pg_bindir(dest) / "createdb"), "-h", "127.0.0.1", "-p", str(PG_PORT), "-U", "copilot", name])
    return True


def init_cluster(bindir: Path, data: Path) -> bool:
    """Run ``initdb`` on *data* unless it already holds a cluster; True when it ran (#12198).

    A cluster left by a previous run of the same container (a restart in place) is kept: ``initdb`` refuses a
    non-empty directory. Its ``postmaster.pid`` is dropped, since the postmaster that wrote it is gone (the restart
    waits for the whole process group to end). A directory without ``PG_VERSION`` is a half-written init: removed.
    """
    if (data / "PG_VERSION").is_file():
        (data / "postmaster.pid").unlink(missing_ok=True)
        log(f"postgres cluster kept from the previous run ({data}), no initdb")
        return False
    shutil.rmtree(data, ignore_errors=True)
    _run([str(bindir / "initdb"), "-D", str(data), "-U", "copilot", "-A", "trust", "-E", "UTF8", "--no-sync"])
    return True


def start_postgres(dest: Path, run_dir: Path, spawn: Spawn) -> None:
    bindir = pg_bindir(dest)
    data = run_dir / "pgdata"
    init_cluster(bindir, data)
    process = spawn(
        "postgres",
        [str(bindir / "postgres"), "-D", str(data), "-h", "127.0.0.1", "-p", str(PG_PORT), "-k", str(run_dir),
         "-c", "fsync=off", "-c", "max_connections=200", "-c", "shared_buffers=128MB"],
        None,
    )
    deadline = time.monotonic() + 60
    ready = [str(bindir / "pg_isready"), "-h", "127.0.0.1", "-p", str(PG_PORT), "-U", "copilot"]
    while subprocess.run(ready, capture_output=True).returncode != 0:
        _alive(process, "postgres")
        if time.monotonic() > deadline:
            raise SystemExit("stores: postgres not ready within 60s")
        time.sleep(0.2)
    ensure_database(dest, "copilot")
    version = _run([*_psql(dest, "copilot"), "-c", "CREATE EXTENSION IF NOT EXISTS vector; SELECT extversion FROM pg_extension WHERE extname = 'vector'"])
    log(f"postgres ready on 127.0.0.1:{PG_PORT}, pgvector {version.strip()}")


def start_redis(dest: Path, run_dir: Path, spawn: Spawn) -> None:
    (run_dir / "dragonfly").mkdir(parents=True, exist_ok=True)
    process = spawn(
        "redis",
        [str(dest / "bin" / "dragonfly"), "--bind", "127.0.0.1", "--port", str(REDIS_PORT), "--force_epoll",
         "--proactor_threads", "2", "--maxmemory", "1gb", "--dir", str(run_dir / "dragonfly"), "--dbfilename", "",
         "--default_lua_flags", "allow-undeclared-keys", "--logtostderr"],
        None,
    )
    _wait_port(REDIS_PORT, process, "redis")
    log(f"redis ready on 127.0.0.1:{REDIS_PORT}")


def start_s3(dest: Path, run_dir: Path, spawn: Spawn) -> None:
    process = spawn(
        "s3",
        [str(dest / "bin" / "silo"), "server", str(run_dir / "s3"), "--address", f"127.0.0.1:{S3_PORT}", "--quiet"],
        {"MINIO_ROOT_USER": S3_USER, "MINIO_ROOT_PASSWORD": S3_PASSWORD, "MINIO_BROWSER": "off"},
    )
    deadline = time.monotonic() + 60
    while True:
        _alive(process, "s3")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{S3_PORT}/minio/health/live", timeout=2) as response:
                if response.status == 200:
                    break
        except (OSError, urllib.error.URLError):
            pass
        if time.monotonic() > deadline:
            raise SystemExit("stores: s3 not healthy within 60s")
        time.sleep(0.2)
    log(f"s3 ready on 127.0.0.1:{S3_PORT}")


def start_all(dest: Path, run_dir: Path, spawn: Spawn) -> None:
    """Start the three stores of *dest* side by side, their data under *run_dir*; raise when one fails."""
    run_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(start, dest, run_dir, spawn) for start in (start_postgres, start_redis, start_s3)]
        for future in futures:
            future.result()


def stop(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


#: Prefix of the throwaway directory each run keeps its stores' data under.
RUN_DIR_PREFIX = "xtm-stores-"


def leftovers(dest: Path, tmp: Path, proc_root: Path = Path("/proc")) -> list[int]:
    """The pids of store processes a run of this check started that are still alive (a run lost with its caller).

    A run whose sandbox call was cut by a container or worker restart never reaches its ``finally``: its
    postgres, redis and s3 keep the ports, and the next run fails at once (address in use, database exists).
    A leftover is a process whose executable lies under *dest* AND whose arguments name a data directory
    under *tmp* with :data:`RUN_DIR_PREFIX`: the stores a lab started (its data under the lab's own root,
    a check run inside a lab included) are never touched. One command runs per container, so no live run
    of this check owns them.
    """
    prefixes = tuple({f"{dest.resolve()}/", f"{dest}/"})
    run_dirs = tuple({f"{tmp.resolve()}/{RUN_DIR_PREFIX}", f"{tmp}/{RUN_DIR_PREFIX}"})
    found: list[int] = []
    try:
        candidates = list(proc_root.iterdir())
    except OSError:
        return found
    for entry in candidates:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if argv[0].startswith(prefixes) and any(run_dir in arg for arg in argv[1:] for run_dir in run_dirs):
            found.append(int(entry.name))
    return sorted(found)


def _alive_pid(pid: int) -> bool:
    """Whether *pid* still runs: a zombie (ended, not yet reaped by a parent that may be gone) counts as ended."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return stat.rsplit(")", 1)[-1].split()[:1] != ["Z"]


def reap(pids: list[int], timeout: float = 10) -> list[int]:
    """Terminate *pids*, killing those still alive after *timeout* seconds; the pids that were signalled."""
    import signal

    signalled = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            signalled.append(pid)
        except (ProcessLookupError, PermissionError):
            continue
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(_alive_pid(pid) for pid in signalled):
        time.sleep(0.1)
    for pid in signalled:
        if _alive_pid(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    return signalled


def reset_leftovers(dest: Path, tmp: Path | None = None, proc_root: Path = Path("/proc")) -> str:
    """Tear down what a lost run left (its store processes, then its data directories); one line, ``""`` when none."""
    tmp = tmp or Path(tempfile.gettempdir())
    pids = reap(leftovers(dest, tmp, proc_root))
    stale = sorted(tmp.glob(f"{RUN_DIR_PREFIX}*"))
    for directory in stale:
        shutil.rmtree(directory, ignore_errors=True)
    if not pids and not stale:
        return ""
    return (
        f"reset what a lost run left: {len(pids)} store process(es) ended"
        f"{' (pids ' + ', '.join(str(pid) for pid in pids[:6]) + ')' if pids else ''}, "
        f"{len(stale)} data director{'y' if len(stale) == 1 else 'ies'} removed"
    )


def _file_spawn(run_dir: Path, processes: list[subprocess.Popen]) -> Spawn:
    """A spawn that writes each store's output to <run_dir>/<name>.log, out of pytest's way."""

    def spawn(name: str, argv: list[str], env: dict[str, str] | None) -> subprocess.Popen:
        with open(run_dir / f"{name}.log", "wb") as output:
            process = subprocess.Popen(argv, env={**os.environ, **(env or {})}, stdout=output, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    return spawn


def _smoke() -> None:
    bindir = pg_bindir(STORES_DIR)
    psql = [str(bindir / "psql"), "-h", "127.0.0.1", "-p", str(PG_PORT), "-U", "copilot", "-d", "copilot", "-qAt"]
    version = _run([*psql, "-c", "SELECT extversion FROM pg_extension WHERE extname = 'vector'"]).strip()
    if not version:
        raise SystemExit("stores: pgvector is not installed in the copilot database")
    available = set(_run([*psql, "-c", "SELECT name FROM pg_available_extensions"]).split())
    missing = [extension for extension in CONTRIB_EXTENSIONS if extension not in available]
    if missing:
        raise SystemExit(f"stores: contrib extensions not available: {', '.join(missing)}")
    _run([*psql, "-c", "CREATE EXTENSION IF NOT EXISTS unaccent; SELECT unaccent('Hôtel'); DROP EXTENSION unaccent"])
    with socket.create_connection(("127.0.0.1", REDIS_PORT), timeout=5) as connection:
        connection.sendall(b"*1\r\n$4\r\nPING\r\n")
        answer = connection.recv(64)
    if not answer.startswith(b"+PONG"):
        raise SystemExit(f"stores: redis answered {answer!r} to PING")
    with urllib.request.urlopen(f"http://127.0.0.1:{S3_PORT}/minio/health/live", timeout=5) as response:
        if response.status != 200:
            raise SystemExit(f"stores: s3 health answered {response.status}")
    print(f"ok: postgres with pgvector {version} and {len(CONTRIB_EXTENSIONS)} contrib extensions, redis PONG, s3 healthy, offline")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["exec"]:
        return run_exec(args[2:] if args[1:2] == ["--"] else args[1:])
    if args[:1] == ["install"]:
        if len(args) != 2:
            raise SystemExit("usage: stores.py install <dir>")
        install(Path(args[1]))
        return 0
    smoke = args[:1] == ["--smoke"]
    if not installed(STORES_DIR, None if smoke else CHECK_ARTEFACTS):
        raise SystemExit(
            f"stores: {STORES_DIR} holds no stores matching the pins of .xtm-one/stores.py. The sandbox has no "
            "network: the stores come with the python-dev image built from this checkout's "
            "backend/sandbox/Dockerfile.sandbox.dev; an older image cannot run this check "
            "(dev.version of backend/sandbox/published-images.json names the image to run)."
        )
    if not smoke and not installed(STORES_DIR):
        log("this image predates the postgres-contrib pin: the integration tests run without the contrib extensions")
    reset = reset_leftovers(STORES_DIR)
    if reset:
        log(reset)
    run_dir = Path(tempfile.mkdtemp(prefix=RUN_DIR_PREFIX))
    processes: list[subprocess.Popen] = []
    started = time.monotonic()
    try:
        try:
            start_all(STORES_DIR, run_dir, _file_spawn(run_dir, processes))
        except SystemExit:
            for store_log in sorted(run_dir.glob("*.log")):
                log(f"{store_log.name}: {store_log.read_text(encoding='utf-8', errors='replace')[-1500:]}")
            raise
        log(f"stores up in {time.monotonic() - started:.1f}s")
        if smoke:
            _smoke()
            return 0
        env = {**CHECK_ENV, **os.environ, **environment()}
        return subprocess.run([sys.executable, "-m", "pytest", *args], env=env).returncode
    finally:
        stop(processes)
        shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
