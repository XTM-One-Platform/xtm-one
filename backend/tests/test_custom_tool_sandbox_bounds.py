"""Custom tools in OpenSandbox end in seconds and name a denied egress.

A JS custom tool that does no network work must not hold the call until the
sandbox's lifetime (300 s): the script exits once the tool returned, and the
backend bounds the command below the lifetime. A fetch refused by the
sandbox's default-deny egress policy says so instead of a bare "fetch failed".
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services import custom_tool_executor as cte
from tests.virtual_time import VirtualTimeLoop


def _sandbox(*, run=None, exit_code=0, stdout="ok", stderr=""):
    result = MagicMock()
    result.exit_code = exit_code
    result.logs.stdout = [MagicMock(text=stdout)] if stdout else []
    result.logs.stderr = [MagicMock(text=stderr)] if stderr else []
    result.complete = SimpleNamespace(execution_time_in_millis=10)
    sandbox = AsyncMock()
    sandbox.__aenter__ = AsyncMock(return_value=sandbox)
    sandbox.__aexit__ = AsyncMock(return_value=False)
    sandbox.files.write_file = AsyncMock()
    sandbox.commands.run = run or AsyncMock(return_value=result)
    sandbox.get_metrics = AsyncMock(return_value=None)
    return sandbox


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "disable_custom_tools", False)
    monkeypatch.setattr("app.services.quota.enforce_combined_sandbox_quota", AsyncMock())
    monkeypatch.setattr("app.services.quota.increment_sandbox_usage", AsyncMock())


def test_the_script_exits_once_the_tool_returned():
    script = cte._build_js_script("console.log(1);", {})
    assert "__xtmWrite('', () => process.exit(process.exitCode ?? 0));" in script
    assert script.rstrip().endswith("});")
    assert "cause:" in script


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("return { lexical: { root: 1 } };", '{"lexical":{"root":1}}'),
        ("return 'plain';", "plain"),
        # A tool that prints keeps its output as it was: the returned value is
        # not appended (a $output_files object it printed stays parseable).
        ("console.log('logged'); return [1, 2];", "logged"),
        ("console.log(JSON.stringify({ $output_files: [] })); return 1;", '{"$output_files":[]}'),
        ("process.stdout.write('raw'); return 'x';", "raw"),
        ("console.log('only logged');", "only logged"),
    ],
)
def test_a_returned_value_is_the_tool_s_output(tmp_path, code, expected):
    """A tool ending with ``return`` printed nothing and answered "(no output)"."""
    path = tmp_path / "tool.js"
    path.write_text(cte._build_js_script(code, {}))
    completed = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=20)  # noqa: S603, S607
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_an_open_handle_no_longer_keeps_node_alive(tmp_path):
    # A keep-alive interval would keep Node running forever without the exit.
    script = cte._build_js_script("setInterval(() => {}, 1000); console.log('done');", {})
    path = tmp_path / "tool.js"
    path.write_text(script)
    completed = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=20)  # noqa: S603, S607
    assert completed.returncode == 0
    assert completed.stdout.strip() == "done"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_tool_s_own_exit_code_is_kept(tmp_path):
    script = cte._build_js_script("process.exitCode = 3; console.log('partial');", {})
    path = tmp_path / "tool.js"
    path.write_text(script)
    completed = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=20)  # noqa: S603, S607
    assert completed.returncode == 3


def test_the_bound_takes_off_the_time_already_spent():
    """The lifetime runs from the sandbox's creation: upload time is taken off the bound."""
    margin = cte._COMMAND_DEADLINE_MARGIN_SECONDS
    assert cte._command_deadline(300, 1000.0, 1040.0) == 300 - 40 - margin
    assert cte._command_deadline(300, 1000.0, 1000.0) == 300 - margin
    # No time left is reported as such, never rounded up to a 1 s bound.
    assert cte._command_deadline(10, 1000.0, 1100.0) < 1
    # A short timeout keeps most of itself: the margin is a tenth of it.
    assert cte._command_deadline(30, 1000.0, 1000.0) == 27
    # The default timeout leaves the agent loop's own 300 s room for the kill.
    assert cte._command_deadline(300, 1000.0, 1000.0) <= 300 - 15


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_failed_fetch_reports_its_network_cause(tmp_path):
    # Port 2: closed, and not on the fetch spec's "bad port" list (9 is).
    code = "await fetch('http://127.0.0.1:2/');"
    path = tmp_path / "tool.js"
    path.write_text(cte._build_js_script(code, {}))
    completed = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=20)  # noqa: S603, S607
    assert completed.returncode == 1
    payload = json.loads(completed.stderr.strip().splitlines()[-1])
    assert payload["error"] == "fetch failed"
    assert payload["cause"] == "ECONNREFUSED"


@pytest.mark.parametrize(
    ("error", "cause", "names_policy"),
    [
        ("fetch failed", "ECONNREFUSED", True),
        ("fetch failed", "", True),
        ("getaddrinfo ENOTFOUND example.com", "ENOTFOUND", True),
        ("Cannot read properties of undefined", "", False),
        ("bad input", "", False),
    ],
)
def test_a_denied_egress_is_named(error, cause, names_policy):
    message = cte._explain_network_failure(error, cause, frozenset({"api.example.com"}))
    assert ("egress policy" in message) is names_policy
    if names_policy:
        assert "api.example.com" in message
        assert "integration" in message
    else:
        assert message == error


async def test_opensandbox_js_failure_names_the_egress_policy(monkeypatch):
    monkeypatch.setattr(settings, "opensandbox_network_allow", "")
    stderr = json.dumps({"error": "fetch failed", "cause": "ECONNREFUSED"})
    sandbox = _sandbox(exit_code=1, stdout="", stderr=stderr)
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        with pytest.raises(RuntimeError) as excinfo:
            await cte.execute_js_tool_opensandbox("await fetch('https://cms.example.com');", {})
    assert "egress policy" in str(excinfo.value)
    assert "OPENSANDBOX_NETWORK_ALLOW: none" in str(excinfo.value)


@pytest.mark.parametrize("runner", ["execute_js_tool_opensandbox", "execute_python_tool_opensandbox"])
async def test_a_command_that_never_ends_is_bounded_below_the_lifetime(monkeypatch, runner):
    monkeypatch.setattr(cte, "_COMMAND_DEADLINE_MARGIN_SECONDS", 0)

    async def _never(*_a, **_k):
        await asyncio.sleep(3600)

    sandbox = _sandbox(run=AsyncMock(side_effect=_never))
    killed = AsyncMock()
    monkeypatch.setattr("app.services.sandbox_call_logging.kill_sandbox_best_effort", killed)
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        # 2 s: setup takes a few milliseconds, so about 1.99 s is left for the run
        # stage (under 1 s left, the command is not started at all).
        with pytest.raises(RuntimeError, match="timed out after 1s") as excinfo:
            await getattr(cte, runner)("x", {}, timeout=2)
    killed.assert_awaited()
    assert "stage run" in str(excinfo.value)
    assert "egress policy" in str(excinfo.value)


async def _hang(*_a, **_k):
    await asyncio.sleep(3600)


async def test_no_command_is_run_once_the_lifetime_is_spent(monkeypatch):
    """Setup used the whole lifetime: the command is not started, the sandbox is killed."""
    sandbox = _sandbox()
    killed = AsyncMock()
    monkeypatch.setattr("app.services.sandbox_call_logging.kill_sandbox_best_effort", killed)
    timeout = 300
    with pytest.raises(RuntimeError, match="stage setup") as excinfo:
        await cte._run_bounded(sandbox, "node x.js", timeout, cte.time.monotonic() - timeout)
    sandbox.commands.run.assert_not_called()
    sandbox.commands.run.assert_not_awaited()
    killed.assert_awaited_once_with(sandbox)
    assert "nothing was run" in str(excinfo.value)


@pytest.mark.parametrize("runner", ["execute_js_tool_opensandbox", "execute_python_tool_opensandbox"])
async def test_a_create_that_never_answers_is_bounded(monkeypatch, runner):
    """The create stage is bounded too, and its message names it."""
    monkeypatch.setattr(cte, "_COMMAND_DEADLINE_MARGIN_SECONDS", 0)
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=_hang)
        started = asyncio.get_running_loop().time()
        with pytest.raises(RuntimeError, match="stage create") as excinfo:
            await getattr(cte, runner)("x", {}, timeout=1)
    assert asyncio.get_running_loop().time() - started < 5
    assert "Failed to create sandbox" not in str(excinfo.value)
    assert "platform problem" in str(excinfo.value)


@pytest.mark.parametrize("runner", ["execute_js_tool_opensandbox", "execute_python_tool_opensandbox"])
async def test_an_upload_that_never_answers_is_bounded(monkeypatch, runner):
    """The script upload is bounded, the sandbox killed, the stage named."""
    monkeypatch.setattr(cte, "_COMMAND_DEADLINE_MARGIN_SECONDS", 0)
    sandbox = _sandbox()
    sandbox.files.write_file = AsyncMock(side_effect=_hang)
    killed = AsyncMock()
    monkeypatch.setattr("app.services.sandbox_call_logging.kill_sandbox_best_effort", killed)
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        with pytest.raises(RuntimeError, match="stage upload"):
            await getattr(cte, runner)("x", {}, timeout=1)
    killed.assert_awaited()
    sandbox.commands.run.assert_not_awaited()


def test_a_kill_after_a_stage_timeout_fits_in_the_margin(monkeypatch):
    """A short timeout is not overrun by a kill the server does not answer.

    On a virtual-time loop, with the executor's clock read from it: the run
    stage's deadline and the kill's timeout fall at exact instants, so a
    stall of a loaded runner cannot push the call past the bound.
    """
    sandbox = _sandbox(run=AsyncMock(side_effect=_hang))

    async def _slow_kill(_sandbox):
        await asyncio.sleep(3600)

    monkeypatch.setattr("app.services.sandbox_call_logging.kill_sandbox_best_effort", _slow_kill)
    monkeypatch.setattr(cte, "time", SimpleNamespace(monotonic=lambda: asyncio.get_running_loop().time()))

    async def _call() -> float:
        loop = asyncio.get_running_loop()
        with patch("opensandbox.Sandbox") as mocksandbox:
            mocksandbox.create = AsyncMock(return_value=sandbox)
            started = loop.time()
            with pytest.raises(RuntimeError, match="stage run"):
                await cte.execute_js_tool_opensandbox("x", {}, timeout=3)
        return loop.time() - started

    assert asyncio.run(_call(), loop_factory=VirtualTimeLoop) <= 3.5


async def test_a_kill_that_never_answers_does_not_hold_the_call(monkeypatch):
    """The best-effort kill gives up instead of holding the finished call."""
    from app.services import sandbox_call_logging as scl

    monkeypatch.setattr(scl, "_KILL_TIMEOUT_SECONDS", 0.05)
    sandbox = AsyncMock()
    sandbox.kill = AsyncMock(side_effect=_hang)
    await asyncio.wait_for(scl.kill_sandbox_best_effort(sandbox), timeout=2)
    sandbox.kill.assert_awaited()


async def test_a_returned_value_reaches_the_agent_through_opensandbox(monkeypatch):
    """The executor returns what the script printed: the returned JSON, not "(no output)"."""
    sandbox = _sandbox(stdout='{"lexical":{}}')
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        out = await cte.execute_js_tool_opensandbox("return { lexical: {} };", {})
    assert out == '{"lexical":{}}'
    script = sandbox.files.write_file.await_args.args[1].decode()
    assert "JSON.stringify(value)" in script


# --- Per-stage durations on the tool span, a denied egress refused at once ---


def _span_fields(monkeypatch) -> dict[str, str]:
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        "app.services.langfuse_tracing.annotate_current_tool_span", lambda fields: seen.update(fields)
    )
    return seen


async def test_the_stage_durations_reach_the_tool_span(monkeypatch):
    seen = _span_fields(monkeypatch)
    sandbox = _sandbox(stdout="done")
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        assert await cte.execute_js_tool_opensandbox("return 'done';", {}) == "done"
    for stage in ("quota", "create", "upload", "run", "cleanup", "total"):
        assert int(seen[f"custom_tool.{stage}_ms"]) >= 0
    assert seen["custom_tool.outcome"] == "ok"


async def test_the_stage_durations_reach_the_span_on_a_failure(monkeypatch):
    seen = _span_fields(monkeypatch)
    stderr = json.dumps({"error": "fetch failed", "cause": "EGRESS_DENIED"})
    sandbox = _sandbox(exit_code=1, stdout="", stderr=stderr)
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        with pytest.raises(RuntimeError, match="egress policy"):
            await cte.execute_js_tool_opensandbox("await fetch('https://cms.example.com');", {})
    assert seen["custom_tool.outcome"] == "error"
    assert "custom_tool.run_ms" in seen
    assert "custom_tool.cleanup_ms" in seen


async def test_a_create_failure_still_reports_its_stages(monkeypatch):
    seen = _span_fields(monkeypatch)
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=ValueError("boom"))
        with pytest.raises(RuntimeError, match="Failed to create sandbox"):
            await cte.execute_js_tool_opensandbox("return 1;", {})
    assert "custom_tool.create_ms" in seen
    assert "custom_tool.upload_ms" not in seen
    assert seen["custom_tool.outcome"] == "error"


async def test_the_opensandbox_script_carries_the_egress_precheck(monkeypatch):
    monkeypatch.setattr(settings, "opensandbox_network_allow", "api.example.com")
    sandbox = _sandbox()
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await cte.execute_js_tool_opensandbox("return 1;", {})
    script = sandbox.files.write_file.await_args.args[1].decode()
    assert 'const __xtmEgressAllow = ["api.example.com"];' in script


def test_the_subprocess_script_has_no_egress_precheck():
    # No egress policy outside OpenSandbox: fetch is left as it is.
    assert "__xtmEgressAllow" not in cte._build_js_script("return 1;", {})


@pytest.mark.parametrize(
    ("allow", "expected"),
    [
        ([], []),
        (["API.example.com ", "*.corp.example"], ["*.corp.example", "api.example.com"]),
        (["10.0.0.1"], ["10.0.0.1"]),
        # A target the pre-check cannot read turns it off: never refuse more than the policy.
        (["10.0.0.0/8"], None),
        (["api.example.com:8443"], None),
        (["https://api.example.com"], None),
    ],
)
def test_the_precheck_targets(allow, expected):
    assert cte._egress_precheck_targets(frozenset(allow)) == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize(
    ("allow", "url", "denied"),
    [
        ([], "https://staging.example.com/api", True),
        (["api.example.com"], "https://staging.example.com/api", True),
        (["api.example.com"], "https://api.example.com.evil.test/", True),
        # Allowed hosts reach the network (a closed local port here): no pre-check refusal.
        (["127.0.0.1"], "http://127.0.0.1:2/", False),
        (["*.0.0.1"], "http://127.0.0.1:2/", False),
    ],
)
def test_a_denied_host_is_refused_without_a_network_call(tmp_path, allow, url, denied):
    path = tmp_path / "tool.js"
    path.write_text(cte._build_js_script(f"await fetch({json.dumps(url)});", {}, egress_allow_list=frozenset(allow)))
    started = time.monotonic()
    completed = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=20)  # noqa: S603, S607
    assert completed.returncode == 1
    payload = json.loads(completed.stderr.strip().splitlines()[-1])
    assert payload["error"] == "fetch failed"
    if denied:
        assert payload["cause"] == "EGRESS_DENIED"
        assert time.monotonic() - started < 5
        message = cte._explain_network_failure(payload["error"], payload["cause"], frozenset(allow))
        assert "egress policy" in message
    else:
        assert payload["cause"] == "ECONNREFUSED"


# --- A 10 s connect bound for allowed hosts, a slow create named in the error ---


def test_the_opensandbox_script_bounds_the_connect_of_an_allowed_host():
    script = cte._build_js_script("return 1;", {}, egress_allow_list=frozenset({"api.example.com"}))
    assert f"const __xtmConnectBoundMs = {cte._JS_CONNECT_BOUND_MS};" in script
    assert cte._JS_CONNECT_BOUND_MS == 10_000
    assert "UND_ERR_CONNECT_TIMEOUT" in script
    # Outside OpenSandbox no bound is added.
    assert "__xtmConnectBoundMs" not in cte._build_js_script("return 1;", {})


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_an_allowed_host_that_never_answers_ends_on_the_connect_bound(tmp_path, monkeypatch):
    # 10.255.255.1 is not routed: a connect there hangs until a timeout.
    monkeypatch.setattr(cte, "_JS_CONNECT_BOUND_MS", 500)
    path = tmp_path / "tool.js"
    path.write_text(
        cte._build_js_script(
            "await fetch('http://10.255.255.1:81/');", {}, egress_allow_list=frozenset({"10.255.255.1"})
        )
    )
    started = time.monotonic()
    completed = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=20)  # noqa: S603, S607
    payload = json.loads(completed.stderr.strip().splitlines()[-1])
    assert payload["error"] == "fetch failed"
    if payload["cause"] == "UND_ERR_CONNECT_TIMEOUT":
        assert time.monotonic() - started < 5
    else:  # a host network that refuses the route at once still ends at once
        assert payload["cause"] in {"EHOSTUNREACH", "ENETUNREACH", "ECONNREFUSED"}


def _slow_create(seconds: float):
    """A frozen monotonic clock that jumps *seconds* while the sandbox is created.

    Frozen, not ``real() + offset``: wall time spent on a loaded runner would
    otherwise be added to the jump and round the reported stage (96.1s for 96.0s).
    """
    offset = {"value": 0.0}
    base = 1000.0

    async def create(*args, **kwargs):
        offset["value"] += seconds
        return create.sandbox

    return create, offset, (lambda: base + offset["value"])


async def test_a_failure_after_a_slow_create_names_the_create_stage(monkeypatch):
    create, _offset, clock = _slow_create(96.0)
    create.sandbox = _sandbox(exit_code=1, stdout="", stderr=json.dumps({"error": "boom", "cause": ""}))
    # The executor's clock only: the event loop keeps the real one.
    monkeypatch.setattr(cte, "time", SimpleNamespace(monotonic=clock))
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=create)
        with pytest.raises(RuntimeError) as raised:
            await cte.execute_js_tool_opensandbox("throw new Error('boom');", {})
    text = str(raised.value)
    assert "Tool execution failed: boom" in text
    assert "create 96.0s" in text
    assert "sandbox service was slow" in text


async def test_a_failure_after_a_fast_create_is_left_as_it_was():
    sandbox = _sandbox(exit_code=1, stdout="", stderr=json.dumps({"error": "boom", "cause": ""}))
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        with pytest.raises(RuntimeError) as raised:
            await cte.execute_js_tool_opensandbox("throw new Error('boom');", {})
    assert str(raised.value) == "Tool execution failed: boom"


def test_a_slow_create_keeps_the_error_type():
    clock = cte._StageClock(0.0)
    clock.stages = {"quota": 0.8, "create": 12.0}
    slow = cte._with_slow_create(cte._StageTimeoutError("timed out"), clock)
    assert isinstance(slow, cte._StageTimeoutError)
    assert "quota 0.8s, create 12.0s" in str(slow)
    clock.stages["create"] = 9.0
    assert cte._with_slow_create(RuntimeError("x"), clock) is None
