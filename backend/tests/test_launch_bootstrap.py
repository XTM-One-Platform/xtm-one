"""A worker whose launch already migrated and seeded does not do it again.

uvicorn recycles a web worker after ``MAX_REQUESTS`` and starts a replacement
next to a sibling that keeps serving. The replacement used to re-run the
whole one-time startup - migrations under the advisory lock, the full
built-in content sync - and answered its share of the pod's traffic with
``503`` for its whole length (~40 s in production), inbound webhooks and
runner heartbeats included. ``start-uvicorn.sh`` now exports one
``XTM_ONE_LAUNCH_ID`` per launch, the first worker to finish the bootstrap
records it (``app/core/launch_marker.py``), and a sibling of the same launch
only sets up its own per-process state (``_adopt_launch_bootstrap``).
"""

from __future__ import annotations

import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import app.main as main_module
from app.core import launch_marker

_LAUNCH_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def launch(monkeypatch, tmp_path: Path) -> Path:
    """A launch id and a private temp directory for its marker."""
    monkeypatch.setenv(launch_marker.LAUNCH_ID_ENV, _LAUNCH_ID)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def test_no_launch_id_means_no_marker(monkeypatch, tmp_path):
    """uvicorn run without the launcher (local dev server): every worker runs
    the full startup, as before."""
    monkeypatch.delenv(launch_marker.LAUNCH_ID_ENV, raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    launch_marker.mark_launch_bootstrap_done()

    assert not launch_marker.launch_bootstrap_done()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "launch_id",
    [
        "",
        "short",
        "../../etc/passwd-0123456789",
        "a/b" * 10,
        str(uuid.UUID(_LAUNCH_ID)),
        _LAUNCH_ID.upper(),
        _LAUNCH_ID[:-1],
        _LAUNCH_ID + "0",
    ],
    ids=["blank", "short", "path", "separators", "hand-set-uuid", "uppercase", "31-chars", "33-chars"],
)
def test_a_malformed_launch_id_is_no_launch_id(monkeypatch, tmp_path, launch_id):
    """Only the launcher's 32 lowercase hex characters name a launch: the
    variable is the launcher's, and a value set by hand - which a restarted
    container would keep - must never make it look like the previous one."""
    monkeypatch.setenv(launch_marker.LAUNCH_ID_ENV, launch_id)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    launch_marker.mark_launch_bootstrap_done()

    assert not launch_marker.launch_bootstrap_done()
    assert list(tmp_path.iterdir()) == []


def test_the_marker_is_per_launch(launch, monkeypatch):
    assert not launch_marker.launch_bootstrap_done()

    launch_marker.mark_launch_bootstrap_done()
    assert launch_marker.launch_bootstrap_done()
    launch_marker.mark_launch_bootstrap_done()  # idempotent

    # A restarted container runs the launcher again: a new launch.
    monkeypatch.setenv(launch_marker.LAUNCH_ID_ENV, uuid.uuid4().hex)
    assert not launch_marker.launch_bootstrap_done()


def test_an_unwritable_temp_dir_degrades_to_the_full_startup(launch, monkeypatch, caplog):
    def _refuse(self, *args, **kwargs):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(Path, "touch", _refuse)

    launch_marker.mark_launch_bootstrap_done()

    assert not launch_marker.launch_bootstrap_done()
    assert "re-run the migrations" in caplog.text


class _Recorder:
    """Stubs every step of ``_do_all_startup_work`` and records the one-time ones."""

    def __init__(self, monkeypatch, *, adopt: bool = True) -> None:
        self.calls: list[str] = []
        self._adopt = adopt
        self.on_lock = None
        self.on_bootstrap = None

        async def _noop(*_args, **_kwargs) -> None:
            return None

        async def _migrations() -> None:
            self.calls.append("migrations")

        @asynccontextmanager
        async def _bootstrap_lock():
            self.calls.append("lock")
            if self.on_lock is not None:
                self.on_lock()
            yield

        async def _bootstrap() -> None:
            self.calls.append("bootstrap")
            if self.on_bootstrap is not None:
                self.on_bootstrap()

        async def _adopt_launch_bootstrap() -> bool:
            self.calls.append("adopt")
            return self._adopt

        class _Storage:
            client = object()

        class _Session:
            async def commit(self) -> None:
                return None

        @asynccontextmanager
        async def _session():
            yield _Session()

        monkeypatch.setattr("app.core.migrations.run_migrations", _migrations)
        monkeypatch.setattr("app.core.migrations.bootstrap_lock", _bootstrap_lock)
        monkeypatch.setattr(main_module, "_bootstrap_platform_data", _bootstrap)
        monkeypatch.setattr(main_module, "_adopt_launch_bootstrap", _adopt_launch_bootstrap)
        monkeypatch.setattr("app.core.ann_index_builder.build_ann_indexes", _noop)
        monkeypatch.setattr("app.services.tracing.dispatcher.init_tracing_dispatcher", _noop)
        monkeypatch.setattr("app.services.file_storage.file_storage", _Storage())
        monkeypatch.setattr(
            "app.services.custom_tool_executor.warn_sandbox_mode_at_startup", _noop
        )
        monkeypatch.setattr(main_module, "_autonomous_loop", _noop)
        monkeypatch.setattr(main_module, "_a2a_reaper_loop", _noop)
        monkeypatch.setattr(main_module, "_a2a_client_sweep_loop", _noop)
        monkeypatch.setattr(main_module, "_a2a_owed_cancel_loop", _noop)
        monkeypatch.setattr(main_module, "_branding_refresh_loop", _noop)
        monkeypatch.setattr(main_module, "refresh_registered_platform_origins", _noop)
        monkeypatch.setattr(
            "app.services.security_options.refresh_security_options_cache", _noop
        )
        monkeypatch.setattr(
            "app.services.security_options.run_security_options_refresh_loop", _noop
        )
        monkeypatch.setattr("app.channels.discord.start_discord_gateway", _noop)
        monkeypatch.setattr(
            "app.api.v1.platform_settings_helpers.get_platform_settings", _noop
        )
        monkeypatch.setattr("app.services.branding.refresh_branding_cache", _noop)
        monkeypatch.setattr("app.services.telemetry.init_telemetry", _noop)
        monkeypatch.setattr("app.core.database.async_session", _session)
        monkeypatch.setattr(main_module, "_refresh_openapi_branding", lambda: None)
        monkeypatch.setattr(main_module, "_refresh_index_html", lambda: None)


async def _run_startup() -> None:
    original = (main_module._startup_done, main_module._startup_settled)
    main_module._startup_done = main_module.asyncio.Event()
    main_module._startup_settled = main_module.asyncio.Event()
    try:
        await main_module._do_all_startup_work()
        assert main_module._startup_done.is_set()
        assert main_module._startup_settled.is_set()
    finally:
        main_module._startup_done, main_module._startup_settled = original


async def test_the_first_worker_of_a_launch_migrates_seeds_and_records_it(launch, monkeypatch):
    recorder = _Recorder(monkeypatch)

    await _run_startup()

    assert recorder.calls == ["migrations", "lock", "bootstrap"]
    assert launch_marker.launch_bootstrap_done()


async def test_a_recycled_worker_skips_the_migrations_and_the_seeding(launch, monkeypatch):
    launch_marker.mark_launch_bootstrap_done()
    recorder = _Recorder(monkeypatch)

    await _run_startup()

    assert recorder.calls == ["adopt"]


async def test_a_worker_booting_alongside_adopts_what_its_sibling_seeded(launch, monkeypatch):
    """Both workers of a new pod start without a marker; the one that waited
    on the bootstrap lock finds it written by the other and skips the sync."""
    recorder = _Recorder(monkeypatch)
    recorder.on_lock = launch_marker.mark_launch_bootstrap_done

    await _run_startup()

    assert recorder.calls == ["migrations", "lock", "adopt"]


async def test_a_worker_that_cannot_adopt_bootstraps_again(launch, monkeypatch):
    """The admin row the service identity names is gone: the full bootstrap
    re-creates it, under the lock. Never the migrations: a recycled worker is
    the older code once a newer version has booted next to it (a rolling
    deploy), and re-running them there fails against the newer head."""
    launch_marker.mark_launch_bootstrap_done()
    recorder = _Recorder(monkeypatch, adopt=False)

    await _run_startup()

    assert recorder.calls == ["adopt", "lock", "adopt", "bootstrap"]


async def test_a_bootstrap_that_raised_is_not_recorded(launch, monkeypatch):
    """A worker killed or failing mid-bootstrap leaves no record: the next
    worker of the launch runs the full startup, migrations included."""
    recorder = _Recorder(monkeypatch)

    def _fail() -> None:
        raise RuntimeError("seed failed")

    recorder.on_bootstrap = _fail

    with pytest.raises(RuntimeError, match="seed failed"):
        await _run_startup()

    assert not launch_marker.launch_bootstrap_done()

    recorder.on_bootstrap = None
    await _run_startup()

    assert recorder.calls == ["migrations", "lock", "bootstrap"] * 2
    assert launch_marker.launch_bootstrap_done()


async def test_without_a_launch_every_worker_runs_the_full_startup(monkeypatch, tmp_path):
    monkeypatch.delenv(launch_marker.LAUNCH_ID_ENV, raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    recorder = _Recorder(monkeypatch)

    await _run_startup()
    await _run_startup()

    assert recorder.calls == ["migrations", "lock", "bootstrap"] * 2


class _AdminLookup:
    def __init__(self, admin_id: uuid.UUID | None) -> None:
        self._admin_id = admin_id

    @asynccontextmanager
    async def __call__(self):
        admin_id = self._admin_id

        class _Result:
            def scalar_one_or_none(self):
                return admin_id

        class _Session:
            async def execute(self, _statement):
                return _Result()

        yield _Session()


@pytest.fixture
def process_state(monkeypatch) -> list[str]:
    """Record the process-state steps of the bootstrap an adopting worker redoes."""
    from app.llm import identity

    steps: list[str] = []

    async def _is_ai_enabled_strict(_db) -> bool:
        steps.append("ai_state")
        return "ai_off" not in steps

    monkeypatch.setattr("app.services.ai_state.is_ai_enabled_strict", _is_ai_enabled_strict)
    monkeypatch.setattr(main_module, "_load_dotenv_files", lambda: steps.append("dotenv"))
    monkeypatch.setattr(identity, "_service_identity", None)
    return steps


async def test_adopting_sets_up_the_process_state_the_bootstrap_leaves(monkeypatch, process_state):
    """The bootstrap's rows are shared, its process state is not: the ``.env``
    keys, the resolved AI state, and the identity the Filigran AI Gateway
    signs user-less LLM calls with."""
    from app.llm import identity

    admin_id = uuid.uuid4()
    monkeypatch.setattr("app.core.database.async_session", _AdminLookup(admin_id))

    assert await main_module._adopt_launch_bootstrap() is True

    assert process_state == ["ai_state", "dotenv"]
    resolved = identity.resolve_llm_caller()
    assert resolved.user_id == admin_id
    assert resolved.email == main_module.settings.admin_email


async def test_adopting_with_ai_off_leaves_the_env_files_unloaded(monkeypatch, process_state):
    """The bootstrap only loads the ``.env`` files through the provider seed,
    which runs with AI on: a worker adopting an AI-off launch must not see
    keys its siblings do not."""
    process_state.append("ai_off")
    monkeypatch.setattr("app.core.database.async_session", _AdminLookup(uuid.uuid4()))

    assert await main_module._adopt_launch_bootstrap() is True

    assert process_state == ["ai_off", "ai_state"]


async def test_adopting_without_an_admin_row_asks_for_the_bootstrap(monkeypatch, process_state):
    from app.llm import identity

    monkeypatch.setattr("app.core.database.async_session", _AdminLookup(None))

    assert await main_module._adopt_launch_bootstrap() is False
    assert identity._service_identity is None


def test_adopting_redoes_every_process_state_step_of_the_bootstrap():
    """The bootstrap's process-state steps - the ``.env`` keys the provider
    seed loads into ``os.environ`` (later read by ``env_models_for``), the AI
    state the boot primes, the service identity - are redone by a worker that
    adopts it; one of them moving out of the bootstrap is reported here."""
    import inspect

    from app.services import provisioning

    bootstrap = inspect.getsource(main_module._bootstrap_platform_data)
    adopt = inspect.getsource(main_module._adopt_launch_bootstrap)
    assert "_load_dotenv_files()" in inspect.getsource(main_module._seed_llm_providers_from_env)
    assert "_seed_llm_providers_from_env()" in inspect.getsource(provisioning.provision_env_providers)
    for step in ("is_ai_enabled_strict(db)", "set_service_identity("):
        assert step in bootstrap
    for step in ("_load_dotenv_files()", "is_ai_enabled_strict(db)", "set_service_identity("):
        assert step in adopt


def _connection_lost():
    import asyncpg
    from sqlalchemy import exc as sa_exc

    lost = asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed in the middle of operation")
    return sa_exc.DBAPIError("SELECT 1", {}, lost, connection_invalidated=True)


async def test_a_failover_during_the_bootstrap_is_waited_out(launch, monkeypatch):
    """A PostgreSQL switchover closed the bootstrap's connections mid-run: the
    startup bootstraps again instead of staying unready, and does not re-run
    the migrations that already succeeded (#10368)."""
    from app.core import db_availability

    monkeypatch.setattr(db_availability, "_STARTUP_FIRST_RETRY_SECONDS", 0.0)
    recorder = _Recorder(monkeypatch)
    failures = [_connection_lost()]
    retrying: list[str | None] = []

    def _lose_the_connection_once() -> None:
        retrying.append(main_module._startup_retrying)
        if failures:
            raise failures.pop()

    recorder.on_bootstrap = _lose_the_connection_once

    await _run_startup()

    assert recorder.calls == ["migrations", "lock", "bootstrap", "lock", "bootstrap"]
    assert launch_marker.launch_bootstrap_done()
    # The readiness probe names what startup is waiting out, until it is over,
    # by its SQLSTATE: it is unauthenticated, the message names hosts.
    assert retrying == [None, "database unavailable (08003)"]
    assert main_module._startup_retrying is None


async def test_a_failover_during_the_migrations_is_waited_out(launch, monkeypatch):
    from app.core import db_availability

    monkeypatch.setattr(db_availability, "_STARTUP_FIRST_RETRY_SECONDS", 0.0)
    recorder = _Recorder(monkeypatch)
    failures = [RuntimeError("Alembic migration failed")]
    failures[0].__cause__ = _connection_lost()

    async def _migrations() -> None:
        recorder.calls.append("migrations")
        if failures:
            raise failures.pop()

    monkeypatch.setattr("app.core.migrations.run_migrations", _migrations)
    retrying: list[str | None] = []
    recorder.on_bootstrap = lambda: retrying.append(main_module._startup_retrying)

    await _run_startup()

    assert recorder.calls == ["migrations", "migrations", "lock", "bootstrap"]
    # The migrations' outage is over: the bootstrap does not show it as current.
    assert retrying == [None]
