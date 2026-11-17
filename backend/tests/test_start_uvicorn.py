import os
import re
import subprocess
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[1] / "start-uvicorn.sh"
COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def _run_launcher(
    tmp_path: Path,
    *,
    web_workers: str | None = None,
    max_requests: str | None = None,
    max_requests_jitter: str | None = None,
    max_concurrency: str | None = None,
    ws_ping_timeout: str | None = None,
    graceful_timeout: str | None = None,
    max_requests_defer: str | None = None,
    forwarded_allow_ips: str | None = None,
    launch_id: str | None = None,
    inherited_env: dict[str, str] | None = None,
) -> tuple[list[str], str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    args_file = tmp_path / "args.txt"
    fake_uvicorn = fake_bin / "uvicorn"
    fake_uvicorn.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$UVICORN_ARGS_FILE"\n'
        'printf "%s" "${XTM_ONE_LAUNCH_ID:-}" > "$UVICORN_ARGS_FILE.launch"\n'
        'env | grep "^XTM_ONE_" > "$UVICORN_ARGS_FILE.env" || true\n',
        encoding="utf-8",
    )
    fake_uvicorn.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "UVICORN_ARGS_FILE": str(args_file),
        **(inherited_env or {}),
    }
    if inherited_env is None:
        for name in _RECYCLE_ENV:
            env.pop(name, None)
    if launch_id is None:
        env.pop("XTM_ONE_LAUNCH_ID", None)
    else:
        env["XTM_ONE_LAUNCH_ID"] = launch_id
    if max_requests is None:
        env.pop("MAX_REQUESTS", None)
    else:
        env["MAX_REQUESTS"] = max_requests
    if max_requests_jitter is None:
        env.pop("MAX_REQUESTS_JITTER", None)
    else:
        env["MAX_REQUESTS_JITTER"] = max_requests_jitter
    if graceful_timeout is None:
        env.pop("GRACEFUL_TIMEOUT", None)
    else:
        env["GRACEFUL_TIMEOUT"] = graceful_timeout
    if max_requests_defer is None:
        env.pop("MAX_REQUESTS_DEFER_SECONDS", None)
    else:
        env["MAX_REQUESTS_DEFER_SECONDS"] = max_requests_defer
    if max_concurrency is None:
        env.pop("MAX_CONCURRENCY", None)
    else:
        env["MAX_CONCURRENCY"] = max_concurrency
    if ws_ping_timeout is None:
        env.pop("WS_PING_TIMEOUT", None)
    else:
        env["WS_PING_TIMEOUT"] = ws_ping_timeout
    if web_workers is None:
        env.pop("WEB_WORKERS", None)
    else:
        env["WEB_WORKERS"] = web_workers
    if forwarded_allow_ips is None:
        env.pop("FORWARDED_ALLOW_IPS", None)
    else:
        env["FORWARDED_ALLOW_IPS"] = forwarded_allow_ips

    proc = subprocess.run(  # noqa: S603
        ["/bin/sh", str(LAUNCHER), "4000"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return args_file.read_text(encoding="utf-8").splitlines(), proc.stderr


_RECYCLE_ENV = (
    "XTM_ONE_RECYCLE_REQUESTS",
    "XTM_ONE_RECYCLE_JITTER",
    "XTM_ONE_RECYCLE_DEFER_SECONDS",
)


def _app_env(tmp_path: Path) -> dict[str, str]:
    """The ``XTM_ONE_*`` variables uvicorn, and so every worker, received."""
    lines = (tmp_path / "args.txt.env").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def _get_flag(tokens: list[str], flag: str) -> str | None:
    for i, tok in enumerate(tokens):
        if tok == flag and i + 1 < len(tokens):
            return tokens[i + 1]
    return None


def _has_flag(tokens: list[str], flag: str) -> bool:
    return flag in tokens


def test_start_uvicorn_hands_the_worker_recycle_to_the_app(tmp_path: Path) -> None:
    """uvicorn's own recycle cut every chat turn of a worker that reached its
    quota once GRACEFUL_TIMEOUT passed, the pod staying up: the app
    recycles instead, once no agent turn runs in the worker."""
    args, _ = _run_launcher(tmp_path, max_requests=None)
    assert not _has_flag(args, "--limit-max-requests")
    assert not _has_flag(args, "--limit-max-requests-jitter")
    env = _app_env(tmp_path)
    assert env["XTM_ONE_RECYCLE_REQUESTS"] == "10000"
    assert env["XTM_ONE_RECYCLE_DEFER_SECONDS"] == "14400"


def test_start_uvicorn_defaults_web_workers_to_2(tmp_path: Path) -> None:
    args, _ = _run_launcher(tmp_path)
    assert _get_flag(args, "--workers") == "2"


def test_start_uvicorn_invalid_web_workers_falls_back_and_warns(tmp_path: Path) -> None:
    args, stderr = _run_launcher(tmp_path, web_workers="many")
    assert "WARNING" in stderr
    assert _get_flag(args, "--workers") == "2"


def test_web_workers_default_is_consistent_everywhere() -> None:
    """The WEB_WORKERS default lives in three places that MUST stay in sync:

    1. ``start-uvicorn.sh`` (what uvicorn actually runs with),
    2. ``Settings.web_workers`` (feeds the pool-overcommit estimator in
       ``startup_checks._validate_pool_config`` — a drift makes the
       connection-budget check undercount live workers),
    3. ``docker-compose.yml`` (the shipped stack pins its own default — a
       drift silently reverts the launcher default for compose users).
    """
    from app.core.config import Settings

    launcher = LAUNCHER.read_text(encoding="utf-8")
    match = re.search(
        r'_pi WEB_WORKERS "\$\{WEB_WORKERS:-(\d+)\}" (\d+)', launcher
    )
    assert match is not None, "WEB_WORKERS default not found in start-uvicorn.sh"
    assert match.group(1) == match.group(2), (
        "start-uvicorn.sh: env default and _pi fallback for WEB_WORKERS differ"
    )
    launcher_default = int(match.group(1))

    settings_default = Settings.model_fields["web_workers"].default
    assert settings_default == launcher_default, (
        f"Settings.web_workers default ({settings_default}) drifted from the "
        f"start-uvicorn.sh default ({launcher_default}); the pool-overcommit "
        "estimator would miscount live workers"
    )

    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    compose_match = re.search(r"WEB_WORKERS: \$\{WEB_WORKERS:-(\d+)\}", compose)
    assert compose_match is not None, (
        "WEB_WORKERS default not found in docker-compose.yml"
    )
    assert int(compose_match.group(1)) == launcher_default, (
        f"docker-compose.yml WEB_WORKERS default ({compose_match.group(1)}) "
        f"drifted from the start-uvicorn.sh default ({launcher_default})"
    )


def test_start_uvicorn_respects_max_requests_zero_opt_out(tmp_path: Path) -> None:
    args, _ = _run_launcher(tmp_path, max_requests="0")
    assert not _has_flag(args, "--limit-max-requests")
    assert not _has_flag(args, "--limit-max-requests-jitter")
    assert not set(_RECYCLE_ENV) & _app_env(tmp_path).keys()


def test_start_uvicorn_max_requests_zero_drops_an_inherited_recycle(tmp_path: Path) -> None:
    # A container restarted with MAX_REQUESTS=0 keeps whatever its environment
    # held: only the launcher decides what the app recycles.
    _run_launcher(
        tmp_path,
        max_requests="0",
        inherited_env={"XTM_ONE_RECYCLE_REQUESTS": "10000"},
    )
    assert "XTM_ONE_RECYCLE_REQUESTS" not in _app_env(tmp_path)


def test_start_uvicorn_defaults_recycle_jitter_to_half_the_limit(tmp_path: Path) -> None:
    # Workers boot together and count requests at the same rate, so without
    # jitter they all reach the recycle limit within the same drain window
    # and the pod stops accepting connections entirely (probe-failure
    # restarts under load). Default jitter = MAX_REQUESTS / 2.
    _run_launcher(tmp_path, max_requests="10000")
    assert _app_env(tmp_path)["XTM_ONE_RECYCLE_JITTER"] == "5000"


def test_start_uvicorn_custom_recycle_jitter(tmp_path: Path) -> None:
    _run_launcher(tmp_path, max_requests="10000", max_requests_jitter="123")
    assert _app_env(tmp_path)["XTM_ONE_RECYCLE_JITTER"] == "123"


def test_start_uvicorn_invalid_recycle_jitter_falls_back_and_warns(
    tmp_path: Path,
) -> None:
    _, stderr = _run_launcher(
        tmp_path, max_requests="8000", max_requests_jitter="lots"
    )
    assert "WARNING" in stderr
    assert _app_env(tmp_path)["XTM_ONE_RECYCLE_JITTER"] == "4000"


def test_start_uvicorn_custom_recycle_deferral(tmp_path: Path) -> None:
    _run_launcher(tmp_path, max_requests_defer="600")
    assert _app_env(tmp_path)["XTM_ONE_RECYCLE_DEFER_SECONDS"] == "600"


def test_start_uvicorn_invalid_recycle_deferral_falls_back_and_warns(tmp_path: Path) -> None:
    _, stderr = _run_launcher(tmp_path, max_requests_defer="4h")
    assert "WARNING" in stderr
    assert _app_env(tmp_path)["XTM_ONE_RECYCLE_DEFER_SECONDS"] == "14400"


def test_start_uvicorn_defaults_graceful_shutdown_to_120(tmp_path: Path) -> None:
    # Uvicorn's native default (None = wait forever for open connections)
    # turned MAX_REQUESTS worker recycling into full-pod probe outages:
    # long-lived SSE connections never complete, so a recycling worker
    # wedged in the drain forever without ever accepting again.
    args, _ = _run_launcher(tmp_path)
    assert _get_flag(args, "--timeout-graceful-shutdown") == "120"


def test_start_uvicorn_custom_graceful_shutdown(tmp_path: Path) -> None:
    args, _ = _run_launcher(tmp_path, graceful_timeout="30")
    assert _get_flag(args, "--timeout-graceful-shutdown") == "30"


def test_start_uvicorn_tells_the_app_its_graceful_timeout(tmp_path: Path) -> None:
    # A worker asked to stop says how long its running turns have left.
    _run_launcher(tmp_path, graceful_timeout="2m")
    assert _app_env(tmp_path)["XTM_ONE_GRACEFUL_TIMEOUT"] == "120"


def test_start_uvicorn_invalid_graceful_shutdown_falls_back_and_warns(
    tmp_path: Path,
) -> None:
    args, stderr = _run_launcher(tmp_path, graceful_timeout="2m")
    assert "WARNING" in stderr
    assert _get_flag(args, "--timeout-graceful-shutdown") == "120"


def test_start_uvicorn_custom_max_requests(tmp_path: Path) -> None:
    _run_launcher(tmp_path, max_requests="5000")
    assert _app_env(tmp_path)["XTM_ONE_RECYCLE_REQUESTS"] == "5000"


def test_start_uvicorn_invalid_max_requests_falls_back_and_warns(tmp_path: Path) -> None:
    _, stderr = _run_launcher(tmp_path, max_requests="not-a-number")
    assert "WARNING" in stderr
    assert _app_env(tmp_path)["XTM_ONE_RECYCLE_REQUESTS"] == "10000"


def test_start_uvicorn_defaults_limit_concurrency_backstop_to_1000(tmp_path: Path) -> None:
    # Protocol-level backstop kept high (above the in-app APP_MAX_CONCURRENCY
    # limiter) so uvicorn's blunt 503 path is effectively never reached.
    args, _ = _run_launcher(tmp_path)
    assert _get_flag(args, "--limit-concurrency") == "1000"


def test_start_uvicorn_custom_max_concurrency(tmp_path: Path) -> None:
    args, _ = _run_launcher(tmp_path, max_concurrency="3000")
    assert _get_flag(args, "--limit-concurrency") == "3000"


def test_start_uvicorn_invalid_max_concurrency_falls_back_and_warns(tmp_path: Path) -> None:
    args, stderr = _run_launcher(tmp_path, max_concurrency="lots")
    assert "WARNING" in stderr
    assert _get_flag(args, "--limit-concurrency") == "1000"


def test_start_uvicorn_defaults_ws_ping_timeout_to_120(tmp_path: Path) -> None:
    # Default raised from 30s to 120s so chat WebSockets don't get torn
    # down (CloseCode 1011 / "keepalive ping timeout") while the agent
    # loop blocks the event loop on long LLM streams. Matches the
    # documented value in .env.sample.
    args, _ = _run_launcher(tmp_path)
    assert _get_flag(args, "--ws-ping-timeout") == "120"


def test_start_uvicorn_custom_ws_ping_timeout(tmp_path: Path) -> None:
    args, _ = _run_launcher(tmp_path, ws_ping_timeout="60")
    assert _get_flag(args, "--ws-ping-timeout") == "60"


def test_start_uvicorn_invalid_ws_ping_timeout_falls_back_and_warns(tmp_path: Path) -> None:
    args, stderr = _run_launcher(tmp_path, ws_ping_timeout="30s")
    assert "WARNING" in stderr
    assert _get_flag(args, "--ws-ping-timeout") == "120"


def test_start_uvicorn_defaults_ws_ping_interval_to_30(tmp_path: Path) -> None:
    args, _ = _run_launcher(tmp_path)
    assert _get_flag(args, "--ws-ping-interval") == "30"


def test_start_uvicorn_passes_forwarded_allow_ips(tmp_path: Path) -> None:
    args, _ = _run_launcher(tmp_path, forwarded_allow_ips="10.42.0.0/16")
    assert _get_flag(args, "--forwarded-allow-ips") == "10.42.0.0/16"


def test_start_uvicorn_omits_forwarded_allow_ips_when_unset(tmp_path: Path) -> None:
    args, _ = _run_launcher(tmp_path)
    assert not _has_flag(args, "--forwarded-allow-ips")


def test_start_uvicorn_omits_forwarded_allow_ips_when_empty(tmp_path: Path) -> None:
    # The shape docker-compose.yml produces when .env leaves it unset: passing
    # an empty value would override uvicorn's own 127.0.0.1 default with one
    # that trusts nothing by name.
    args, _ = _run_launcher(tmp_path, forwarded_allow_ips="")
    assert not _has_flag(args, "--forwarded-allow-ips")


def _launch_id(tmp_path: Path) -> str:
    return (tmp_path / "args.txt.launch").read_text(encoding="utf-8")


def test_start_uvicorn_exports_a_launch_id_every_worker_inherits(tmp_path: Path) -> None:
    """A worker uvicorn starts to replace a recycled one skips the migrations
    and seeding its launch applied (app/core/launch_marker.py) - it can only
    know its launch through the environment the supervisor hands down."""
    from app.core.launch_marker import _LAUNCH_ID_PATTERN

    _run_launcher(tmp_path)
    launch_id = _launch_id(tmp_path)

    assert re.fullmatch(r"[0-9a-f]{32}", launch_id), launch_id
    assert _LAUNCH_ID_PATTERN.fullmatch(launch_id)


def test_start_uvicorn_mints_a_fresh_launch_id_on_every_run(tmp_path: Path) -> None:
    """A restarted container keeps its environment - an inherited or hand-set
    id must never make a new launch look like the previous one."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    _run_launcher(first, launch_id="0123456789abcdef0123456789abcdef")
    _run_launcher(second, launch_id="0123456789abcdef0123456789abcdef")

    assert _launch_id(first) != "0123456789abcdef0123456789abcdef"
    assert _launch_id(first) != _launch_id(second)


def test_compose_does_not_default_forwarded_allow_ips_to_empty() -> None:
    """Uvicorn reads this variable natively, and an empty value trusts nobody
    — not even the loopback proxy its own default covers."""
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "FORWARDED_ALLOW_IPS: ${FORWARDED_ALLOW_IPS:-127.0.0.1}" in compose
