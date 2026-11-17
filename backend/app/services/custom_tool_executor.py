# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Custom tool executor – runs user-defined JavaScript or Python functions securely.

JavaScript is executed in a subprocess using Node.js. Python is executed
in a subprocess using the system Python interpreter with sandbox security
(blocked dangerous modules, restricted os functions).

Variables from the tool's input schema are injected as ``$variable_name``
in JS and ``variable_name`` in Python.

Security measures:
  - **Docker sandbox preferred**: the CUSTOM_TOOL_SANDBOX setting
    (auto|docker|subprocess, default "auto") auto-detects Docker and runs
    tools in a hardened container when available; subprocess execution is
    the fallback and logs a one-time security warning.
  - **Node.js permission model** (subprocess fallback): JavaScript tools
    run under ``node --permission`` when the installed Node supports it
    (stable since Node 24) — file system, child process, worker threads
    and native addons are denied; network stays available (``--allow-net``
    is added on Node 25+ where the permission model would otherwise deny
    it). Opt-out via ``CUSTOM_TOOL_NODE_PERMISSION=false`` for legacy
    tools that rely on direct file-system access.
  - **Python isolated mode** (subprocess fallback): Python tools run with
    ``-I -X utf8`` so ``PYTHON*`` env vars, the user site directory, and
    the script directory can never influence the interpreter.
  - **Environment sanitization**: only safe env vars are forwarded to
    subprocesses; all secrets are stripped (reuses the allow-list from
    ``code_interpreter``). ``_build_sandbox_env()`` never propagates the
    S3/MinIO TLS bypass as ``NODE_TLS_REJECT_UNAUTHORIZED``, so certificate
    verification stays enabled for outbound HTTPS calls made by custom
    JavaScript tools.
  - **Module blocklist**: database drivers, network libraries, and other
    dangerous modules are blocked inside Python sandboxes.
  - **os.environ scrubbed**: env dict is cleared inside the Python sandbox
    wrapper before user code runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import keyword
import logging
import os as _os
import re
import shutil
import sys
import tempfile
import time
import uuid
from typing import TYPE_CHECKING

from prometheus_client import Gauge

from app.core.log_once import log_once
from app.services._sandbox_selector import _probe_opensandbox_available
from app.services._sandbox_utils import build_sandbox_security_src as _build_sandbox_security_src
from app.services.code_interpreter import _build_sandbox_env

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Effective custom-tool sandbox, exported for alerting (the
# subprocess fallback must page, not just log). Exactly one label value is 1
# at any time; alert on
# ``xtmone_custom_tool_sandbox{sandbox=~"subprocess|docker_unavailable|opensandbox_unavailable"} == 1``
# — NOT ``{sandbox!="docker"}``, which would also page when custom tools are
# intentionally disabled platform-wide (the ``disabled`` state is set to 1).
SANDBOX_GAUGE = Gauge(
    "xtmone_custom_tool_sandbox",
    "Effective custom tool sandbox (1 for the active mode, 0 otherwise)",
    ["sandbox"],
)

_SANDBOX_GAUGE_STATES = (
    "docker",
    "subprocess",
    "opensandbox",
    "docker_unavailable",
    "opensandbox_unavailable",
    "disabled",
)


def _set_sandbox_gauge(effective: str) -> None:
    """Publish the effective sandbox as a one-hot Prometheus gauge."""
    for state in _SANDBOX_GAUGE_STATES:
        SANDBOX_GAUGE.labels(sandbox=state).set(1.0 if state == effective else 0.0)


async def _docker_force_remove(container_name: str) -> None:
    """Best-effort ``docker rm -f`` for a named container.

    ``process.kill()`` on a timeout kills the ``docker run`` CLIENT, not the
    container it launched — the detached container keeps running (and ``--rm``
    only fires on exit), so an infinite-loop tool would otherwise leave a
    CPU-spinning, memory-holding zombie container alive indefinitely. Removing
    it by name reclaims those resources.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "-f",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except Exception as exc:
        logger.warning(
            "Failed to force-remove timed-out container %s: %s",
            container_name,
            exc,
        )


CUSTOM_TOOLS_DISABLED_MSG = (
    "Custom tools are disabled by the platform administrator. "
    "This function should not have been called — custom tools should "
    "not be registered when disabled."
)


def _custom_tools_disabled() -> bool:
    """The ``disable_custom_tools`` security option, read on every call.

    Settings > Access and security, or ``DISABLE_CUSTOM_TOOLS``, which wins.
    """
    from app.core.security_options import security_option

    return bool(security_option("disable_custom_tools"))


def _safe_js_identifier(key: str, used: set[str]) -> str:
    """Map an input key to a unique JS identifier (referenced as ``$<name>``).

    Mirrors :func:`_safe_python_identifier`: two keys that sanitize to the same
    token (e.g. ``a-b`` and ``a_b``) would otherwise emit duplicate
    ``const $x = ...`` declarations, and a duplicate ``const`` is a SyntaxError
    in Node that kills the entire generated script with an opaque error.
    """
    candidate = re.sub(r"[^a-zA-Z0-9_]", "_", key)
    if candidate in used:
        suffix = 2
        while f"{candidate}_{suffix}" in used:
            suffix += 1
        candidate = f"{candidate}_{suffix}"
    used.add(candidate)
    return candidate


def _egress_precheck_targets(egress_allow_list: frozenset[str] | list[str] | None) -> list[str] | None:
    """The allow-list the in-sandbox fetch pre-check may apply, or ``None`` for no pre-check.

    The pre-check only answers sooner than the sandbox's egress policy, which
    stays the enforcement: it must never refuse a host the policy lets
    through. A target it cannot read as a host name, an IP or a ``*.suffix``
    wildcard (a CIDR range, a port, a scheme) turns it off.
    """
    if egress_allow_list is None:
        return None
    targets = sorted({t.strip().lower() for t in egress_allow_list if t and t.strip()})
    for target in targets:
        host = target[2:] if target.startswith("*.") else target
        if not host or not re.fullmatch(r"[a-z0-9.\-]+|[0-9a-f:]+", host):
            return None
    return targets


# The connect bound of a fetch to an allowed host inside the sandbox.
_JS_CONNECT_BOUND_MS = 10_000


def _js_egress_guard(targets: list[str]) -> list[str]:
    """JS lines wrapping ``fetch`` so a host outside *targets* is refused at once.

    Under a default-deny egress policy such a fetch waits for undici's connect
    timeout before failing. The wrapper throws the error a refused
    fetch throws (``fetch failed``, cause code ``EGRESS_DENIED``), which the
    catch below and :func:`_explain_network_failure` turn into the same
    egress-policy message. Every target also lets its subdomains through,
    and ``*.example.com`` lets ``example.com`` through: whatever the policy's
    own matching, refusing less than it is harmless, more is not.

    A fetch to an allowed host first opens a TCP connection to its host and
    port, bounded to ``_JS_CONNECT_BOUND_MS``: a host that does not
    answer fails the fetch then (``fetch failed``, cause code
    ``UND_ERR_CONNECT_TIMEOUT``) instead of after undici's own connect
    timeout. A host:port that answered once is not probed again.
    """
    return [
        f"const __xtmEgressAllow = {json.dumps(targets)};",
        f"const __xtmConnectBoundMs = {_JS_CONNECT_BOUND_MS};",
        "if (typeof globalThis.fetch === 'function') {",
        "  const __xtmFetch = globalThis.fetch;",
        "  const __xtmNet = require('node:net');",
        "  const __xtmReachable = new Map();",
        "  const __xtmFetchError = (cause) => { const e = new TypeError('fetch failed'); e.cause = cause; return e; };",
        "  const __xtmProbe = (host, port) => {",
        "    const key = host + ':' + port;",
        "    if (!__xtmReachable.has(key)) {",
        "      const probe = new Promise((resolve, reject) => {",
        "        const socket = __xtmNet.connect({ host, port });",
        "        const timer = setTimeout(() => {",
        "          socket.destroy();",
        "          reject(__xtmFetchError({ code: 'UND_ERR_CONNECT_TIMEOUT', message: 'connect to ' + key + ' did not complete in ' + (__xtmConnectBoundMs / 1000) + ' s' }));",
        "        }, __xtmConnectBoundMs);",
        "        socket.once('connect', () => { clearTimeout(timer); socket.destroy(); resolve(); });",
        "        socket.once('error', (err) => { clearTimeout(timer); socket.destroy(); reject(__xtmFetchError(err)); });",
        "      });",
        "      __xtmReachable.set(key, probe.catch((err) => { __xtmReachable.delete(key); throw err; }));",
        "    }",
        "    return __xtmReachable.get(key);",
        "  };",
        "  const __xtmHostAllowed = (host) => {",
        "    const h = String(host || '').toLowerCase().replace(/^\\[|\\]$/g, '').replace(/\\.$/, '');",
        "    return __xtmEgressAllow.some((t) => { const b = t.startsWith('*.') ? t.slice(2) : t; return h === b || h.endsWith('.' + b); });",
        "  };",
        "  globalThis.fetch = function (input, init) {",
        "    let url = null;",
        "    try { url = new URL(typeof input === 'string' || input instanceof URL ? String(input) : input.url); } catch (e) { url = null; }",
        "    if (url && (url.protocol === 'http:' || url.protocol === 'https:') && !__xtmHostAllowed(url.hostname)) {",
        "      return Promise.reject(__xtmFetchError({ code: 'EGRESS_DENIED', message: 'host ' + url.hostname + ' is not in the egress allow-list' }));",
        "    }",
        "    if (url && (url.protocol === 'http:' || url.protocol === 'https:')) {",
        "      const host = url.hostname.replace(/^\\[|\\]$/g, '');",
        "      const port = Number(url.port) || (url.protocol === 'https:' ? 443 : 80);",
        "      return __xtmProbe(host, port).then(() => __xtmFetch.call(this, input, init));",
        "    }",
        "    return __xtmFetch.call(this, input, init);",
        "  };",
        "}",
    ]


def _build_js_script(
    js_function: str, inputs: dict, egress_allow_list: frozenset[str] | list[str] | None = None
) -> str:
    """Build a complete Node.js script by injecting input variables.

    Variables in the JS function are referenced as ``$variable_name``.
    We inject them as ``const $variable_name = <json_value>;`` at the top.

    *egress_allow_list*, given for a sandbox under a default-deny egress
    policy (OpenSandbox), adds the fetch pre-check of :func:`_js_egress_guard`;
    ``None`` (subprocess and Docker modes, no such policy) leaves fetch as is.
    """
    lines = []
    precheck = _egress_precheck_targets(egress_allow_list)
    if precheck is not None:
        lines.extend(_js_egress_guard(precheck))

    # Inject input variables as top-level constants
    used_keys: set[str] = set()
    for key, value in inputs.items():
        safe_key = _safe_js_identifier(key, used_keys)
        lines.append(f"const ${safe_key} = {json.dumps(value)};")

    # Whether the tool wrote to stdout itself (console.log included): a value
    # it returns is printed only when it did not, so the output of a
    # tool that prints, a ``$output_files`` object above all, never changes.
    lines.append("let __xtmWrote = false;")
    lines.append("const __xtmWrite = process.stdout.write.bind(process.stdout);")
    lines.append("process.stdout.write = (...args) => { __xtmWrote = true; return __xtmWrite(...args); };")

    # Wrap the user function in an async IIFE so top-level await works
    lines.append("")
    lines.append("(async () => {")
    lines.append("  try {")
    # Indent user code
    for line in js_function.split("\n"):
        lines.append(f"    {line}")
    lines.append("  } catch (err) {")
    # The network cause of a failed fetch ("fetch failed" says nothing): its
    # code lets the executor name a denied egress (_explain_network_failure).
    lines.append("    const cause = err && err.cause ? (err.cause.code || err.cause.message || '') : '';")
    lines.append("    process.stderr.write(JSON.stringify({ error: (err && err.message) || String(err), cause: String(cause) }));")
    lines.append("    process.exit(1);")
    lines.append("  }")
    # The tool has returned: exit now. A handle left open (a keep-alive socket,
    # a timer, a pending connection to a host the policy denies) would keep
    # Node alive until the sandbox's lifetime ends, the 300 s hang.
    # The tool's own process.exitCode is kept: a tool that sets it and returns
    # still reports its failure.
    # A value the tool returns is its output when it printed nothing:
    # only stdout reaches the agent, so a tool ending with ``return {...}``
    # answered "(no output)". A string goes out as is, anything else as JSON.
    lines.append("})().then((value) => {")
    lines.append("  if (value !== undefined && !__xtmWrote) {")
    lines.append("    let out;")
    lines.append("    try { out = typeof value === 'string' ? value : JSON.stringify(value); } catch (e) { out = String(value); }")
    lines.append("    if (out !== undefined) __xtmWrite(out);")
    lines.append("  }")
    lines.append("  __xtmWrite('', () => process.exit(process.exitCode ?? 0));")
    lines.append("});")

    return "\n".join(lines)


# Node's codes for a connection the network refused or could not resolve: under
# a default-deny egress policy these are what a fetch to a host outside the
# allow-list ends with.
_NETWORK_FAILURE_CODES = frozenset(
    {
        "ECONNREFUSED",
        "ECONNRESET",
        "ENOTFOUND",
        "EAI_AGAIN",
        "ETIMEDOUT",
        "EHOSTUNREACH",
        "ENETUNREACH",
        "UND_ERR_CONNECT_TIMEOUT",
        "UND_ERR_SOCKET",
        # The in-sandbox pre-check of a host outside the allow-list.
        "EGRESS_DENIED",
    }
)


def _explain_network_failure(error: str, cause: str, egress_allow_list: frozenset[str] | list[str]) -> str:
    """*error* with the sandbox's egress policy named when the network refused a call.

    A custom tool in an OpenSandbox container runs under a default-deny egress
    policy (``OPENSANDBOX_NETWORK_ALLOW``): a fetch to a host outside the
    allow-list fails with Node's bare "fetch failed". The message says so and
    what to do instead, so the agent does not retry the same call.
    """
    code = (cause or "").strip()
    if error.strip().lower() != "fetch failed" and code.upper() not in _NETWORK_FAILURE_CODES:
        return error
    allowed = ", ".join(sorted(egress_allow_list)) or "none"
    return (
        f"{error}{f' ({code})' if code else ''}: the network call was refused by the sandbox's "
        f"egress policy (default deny; hosts allowed by OPENSANDBOX_NETWORK_ALLOW: {allowed}). "
        "A custom tool cannot reach other hosts from this sandbox: call the API through an "
        "integration (an OpenAPI integration runs its calls on the backend), or ask an "
        "administrator to allow the host."
    )


# Node.js emits runtime warnings to stderr in the form:
#   (node:<pid>) Warning: <text>
#   (node:<pid>) [DEP0040] DeprecationWarning: <text>
#   (node:<pid>) ExperimentalWarning: <text>
# followed (on first emission) by a companion hint line:
#   (Use `node --trace-warnings ...` to show where the warning was created)
# These must be stripped before treating stderr as a structured error payload
# or surfacing it as tool output, so users never see raw runtime metadata.
# The pattern is deliberately anchored on the ``<Name>Warning:`` label (with
# an optional ``[CODE]`` prefix) so that legitimate user output that merely
# starts with ``(node:<pid>)`` is NOT swallowed. The trailing ``\n?`` consumes
# the line break so a warning in the middle of the text does not leave a
# blank line behind.
_NODE_WARNING_RE = re.compile(
    r"^(?:"
    r"\(node:\d+\)\s+(?:\[\w+\]\s+)?\w*Warning:\s.*"
    r"|\(Use `node --trace-[\w-]+ \.\.\.` to show where the warning was created\)"
    r")\n?",
    re.MULTILINE,
)


async def lint_tool_code(language: str, code: str) -> dict:
    """Syntax-check a Custom tool's script WITHOUT executing it.

    The authoring modal's Validate button. Both languages check the script
    the runtime actually runs, and line numbers map back by subtracting its
    preamble. Python compiles `_build_python_script` in-process (``compile``,
    never executed): the code lands at module top level after the sandbox
    header and the input bindings, where the compiler refuses a ``return`` /
    ``await`` that ``ast.parse`` accepts and a ``from __future__`` import that
    the raw snippet alone would allow. JavaScript runs ``node --check`` on
    `_build_js_script`: the runtime's async IIFE legalises top-level
    ``await``/``return`` that a bare check would reject. Returns ``{"status": "ok"}`` or
    ``{"status": "error", "message": ..., "line": ...}``; never runs the
    user's code.
    """

    if _custom_tools_disabled():
        raise RuntimeError(CUSTOM_TOOLS_DISABLED_MSG)

    if language == "python":
        preamble_lines = _build_python_script("", {}).count("\n")
        try:
            compile(_build_python_script(code, {}), "custom_tool", "exec", dont_inherit=True)
            return {"status": "ok"}
        except SyntaxError as exc:
            message = exc.msg or "Invalid Python syntax."
            if "__future__" in message:
                message = "from __future__ imports cannot be used: the code runs after the platform's own setup lines."
            return {
                "status": "error",
                "message": message,
                "line": max(1, exc.lineno - preamble_lines) if exc.lineno else None,
            }

    # JavaScript: node --check on the wrapped script, via a temp file
    # (--check has no stdin mode). Empty inputs: the $vars are undefined
    # identifiers at worst — a runtime concern, not a syntax one.
    wrapped = _build_js_script(code, {})
    preamble_lines = 3  # "", "(async () => {", "  try {"
    tmp_dir = tempfile.mkdtemp(prefix="xtm_lint_")
    try:
        script_path = _os.path.join(tmp_dir, "_tool.js")
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(wrapped)
        try:
            process = await asyncio.create_subprocess_exec(
                "node",
                "--check",
                script_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_build_sandbox_env(),
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        except FileNotFoundError:
            return {
                "status": "error",
                "message": (
                    "Node.js is not installed on the platform — the script "
                    "cannot be checked (it will also fail to run)."
                ),
            }
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
                await process.wait()
            return {"status": "error", "message": "Syntax check timed out."}
        if process.returncode == 0:
            return {"status": "ok"}
        text = _filter_node_warnings((stderr or b"").decode(errors="replace"))
        line: int | None = None
        match = re.search(r"_tool\.js:(\d+)", text)
        if match:
            line = max(1, int(match.group(1)) - preamble_lines)
        # Drop the temp-file path noise; keep the parser's own message.
        message_lines = []
        for ln in text.splitlines():
            if not ln.strip() or "_tool.js" in ln:
                continue
            # The parser's message ends where node's own stack begins.
            if ln.lstrip().startswith("at ") or ln.startswith("Node.js v"):
                break
            message_lines.append(ln)
        return {
            "status": "error",
            "message": "\n".join(message_lines[:6]) or "Invalid JavaScript syntax.",
            "line": line,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _filter_node_warnings(text: str) -> str:
    """Remove Node.js runtime warning lines from *text* and return the remainder.

    Node.js writes lines of the form ``(node:<pid>) Warning: ...`` (and the
    companion hint "(Use `node --trace-warnings ...` to show where the
    warning was created)") to stderr for
    security notices (e.g. ``NODE_TLS_REJECT_UNAUTHORIZED=0``), deprecation
    warnings, and experimental-feature warnings.  These are Node runtime
    metadata that should never appear in user-facing error messages or tool
    output.  Only ``<Name>Warning:``-labelled lines are stripped — other
    ``(node:<pid>)``-prefixed output is preserved.
    """
    return _NODE_WARNING_RE.sub("", text).strip()


async def execute_js_tool(
    js_function: str,
    inputs: dict,
    timeout: int = 300,
) -> str:
    """Execute a JavaScript function with the given inputs.

    When the local Node supports the permission model (Node 24+) and
    ``CUSTOM_TOOL_NODE_PERMISSION`` is enabled (default), the script runs
    under ``node --permission`` — file system, child processes, worker
    threads and native addons are denied while network stays available.

    Returns the stdout output of the script.
    Raises RuntimeError on timeout or execution failure.
    """

    if _custom_tools_disabled():
        raise RuntimeError(CUSTOM_TOOLS_DISABLED_MSG)

    script = _build_js_script(js_function, inputs)
    permission_flags = await _resolve_node_permission_flags()

    try:
        process = await asyncio.create_subprocess_exec(
            "node",
            *permission_flags,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_sandbox_env(),
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(input=script.encode("utf-8")), timeout=timeout)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise RuntimeError(f"Tool execution timed out after {timeout}s") from exc

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            # Strip Node.js runtime warnings before parsing/surfacing the error
            # so that notices like "NODE_TLS_REJECT_UNAUTHORIZED=0" don't
            # corrupt the structured JSON payload written by user code.
            clean_stderr = _filter_node_warnings(stderr_text)
            error_msg = clean_stderr or f"Process exited with code {process.returncode}"
            try:
                err_data = json.loads(clean_stderr)
                error_msg = err_data.get("error", error_msg)
            except (json.JSONDecodeError, AttributeError):
                pass  # keep the cleaned stderr as error_msg
            raise RuntimeError(f"Tool execution failed: {error_msg}")

        # Return stdout; if empty, return stderr info (Node.js warnings stripped)
        return stdout_text or _filter_node_warnings(stderr_text) or "(no output)"

    except FileNotFoundError as exc:
        raise RuntimeError(
            "Node.js is not installed or not in PATH. Custom tools require Node.js 18+ to execute JavaScript functions."
        ) from exc


# ---------------------------------------------------------------------------
# Docker-sandboxed execution
#
# Controlled by the CUSTOM_TOOL_SANDBOX setting (auto|docker|subprocess,
# default "auto" — see core/config.py). The legacy CUSTOM_TOOL_DOCKER=true
# env var is honoured as "docker" (deprecated) only when CUSTOM_TOOL_SANDBOX
# is not explicitly configured.
#
# When the Docker sandbox is used, custom tools run inside a disposable
# Docker container (`node:24-slim` / `python:3.14-slim`) with:
#   - No network access (--network=none)
#   - Read-only filesystem (--read-only)
#   - No capabilities (--cap-drop=ALL)
#   - Memory limit (128 MB)
#   - Timeout via both asyncio and Docker --stop-timeout
#
# In "auto" mode, Docker availability is probed lazily and cached: a positive
# result is cached for the lifetime of the process, a negative result is
# re-probed after a short interval so a daemon that comes online later is
# picked up without a restart. When unavailable, execution falls back to the
# plain subprocess sandbox and a WARNING is logged once a day for the whole
# platform (``_warn_subprocess_isolation_once``). In "docker" mode
# there is no fallback — execution fails with a clear error when Docker is
# unavailable.
# ---------------------------------------------------------------------------

_DOCKER_IMAGE = "node:24-slim"
_DOCKER_MEMORY_LIMIT = "128m"

_SANDBOX_MODES = ("auto", "docker", "subprocess", "opensandbox")
_DOCKER_PROBE_TIMEOUT = 10  # seconds for one `docker info` probe
_DOCKER_PROBE_RETRY_INTERVAL = 60.0  # seconds before re-probing after a failure

# Process-wide probe cache: True is cached forever, False is re-probed after
# _DOCKER_PROBE_RETRY_INTERVAL. The lock makes the probe single-flight so
# concurrent tool executions never spawn more than one `docker info` process.
_docker_available: bool | None = None
_docker_probe_failed_at: float = 0.0
_docker_probe_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Node.js permission model (subprocess fallback hardening)
#
# When custom tools fall back to the plain subprocess (no Docker), JavaScript
# used to run with FULL Node.js privileges: any tool could read host files,
# spawn processes, or load native addons. The Node.js permission model
# (stable since Node 24) closes that gap without a container:
#
#   --permission             deny fs read/write, child_process, worker
#                            threads, native addons, WASI, inspector
#   --allow-net (Node 25+)   the permission model started denying network in
#                            Node 25; custom tools legitimately call external
#                            APIs, so network stays allowed (matching the
#                            Python fallback, where the module blocklist does
#                            not cover the HTTP stack)
#
# The flag sets are probed once per process, newest-first: the net-allowing
# set must be tried before plain --permission, which on Node 25+ would deny
# network and break tools. The first supported combination is cached. On
# Node < 24 (no stable --permission) the probe finds no supported set and the
# fallback runs unhardened, exactly as before. Opt-out:
# CUSTOM_TOOL_NODE_PERMISSION=false.
#
# Parity note: the Docker sandbox already denies fs (--read-only) and
# process spawning (--pids-limit, no shell) — tools that work in the Docker
# sandbox keep working under the permission model. Inputs and conversation
# files are injected inline ($variables / $files with pre-resolved content),
# so well-behaved tools never touch the filesystem.
# ---------------------------------------------------------------------------

_NODE_PERMISSION_FLAG_SETS: tuple[tuple[str, ...], ...] = (
    ("--permission", "--allow-net"),  # Node 25+ (permission model denies net)
    ("--permission",),  # Node 24 (permission model does not cover net)
)
_NODE_PROBE_TIMEOUT = 10  # seconds for one `node <flags> -e ...` probe

# Process-wide probe cache: None = not probed yet, () = unsupported/disabled,
# otherwise the supported flag tuple. Single-flight via the lock.
_node_permission_flags: tuple[str, ...] | None = None
_node_permission_lock = asyncio.Lock()


async def _run_node_permission_probe(flags: tuple[str, ...]) -> bool:
    """Check whether the local ``node`` accepts *flags*. Never raises.

    The probe runs under the same sanitized environment as actual tool
    execution (``_build_sandbox_env()``): host env vars that alter Node
    startup (e.g. ``NODE_OPTIONS``) are stripped from tool subprocesses, so
    letting the probe inherit them could cache a capability result that does
    not match real execution — and the probe must not receive secrets either.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "node",
            *flags,
            "-e",
            "process.exit(0)",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=_build_sandbox_env(),
        )
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=_NODE_PROBE_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()
            returncode = -1
        return returncode == 0
    except Exception:
        return False


async def _resolve_node_permission_flags() -> tuple[str, ...]:
    """Return the Node permission-model flags to use for JS subprocess tools.

    Empty tuple when the feature is disabled (CUSTOM_TOOL_NODE_PERMISSION=
    false) or the installed Node does not support the permission model.
    The probe runs once per process (single-flight) and the result is
    cached; the disabled check is evaluated on every call so tests and
    runtime reconfiguration behave predictably.
    """
    global _node_permission_flags
    from app.core.security_options import security_option

    if not security_option("custom_tool_node_permission"):
        return ()
    if _node_permission_flags is not None:
        return _node_permission_flags

    async with _node_permission_lock:
        if _node_permission_flags is not None:
            return _node_permission_flags
        for flags in _NODE_PERMISSION_FLAG_SETS:
            if await _run_node_permission_probe(flags):
                _node_permission_flags = flags
                logger.info(
                    "Custom tool subprocess fallback: Node.js permission "
                    "model active (%s) — file system, child processes, "
                    "workers and native addons are denied for JavaScript "
                    "tools; network stays allowed.",
                    " ".join(flags),
                )
                break
        else:
            _node_permission_flags = ()
            logger.info(
                "Node.js permission model is not supported by the local "
                "node binary; JavaScript custom tools in the subprocess "
                "fallback run without it (Node 24+ required)."
            )
        return _node_permission_flags


def _legacy_docker_env_enabled() -> bool:
    """Whether the deprecated CUSTOM_TOOL_DOCKER env var requests Docker."""
    return _os.environ.get("CUSTOM_TOOL_DOCKER", "").lower() in ("true", "1", "yes")


@functools.cache
def _warn_legacy_env_once() -> None:
    """Log a one-time deprecation warning for CUSTOM_TOOL_DOCKER."""
    logger.warning(
        "CUSTOM_TOOL_DOCKER is deprecated; use CUSTOM_TOOL_SANDBOX=docker "
        "instead. Treating it as CUSTOM_TOOL_SANDBOX=docker."
    )


async def node_permission_active() -> bool:
    """Whether the JS subprocess fallback runs under the permission model.

    Public wrapper for callers outside this module (the security audit)
    so they do not need to import the private resolver. Never raises
    (except for task cancellation).
    """
    return bool(await _resolve_node_permission_flags())


def describe_js_subprocess_privileges(node_hardened: bool) -> str:
    """One-line description of the JS privilege level in the subprocess fallback.

    Single source of truth for the startup/per-execution security notice and
    the security-audit finding, so the two surfaces can never drift: when the
    Node.js permission model is active the fallback is meaningfully hardened
    (fs / child processes / workers / addons denied), so neither surface may
    keep claiming
    "full Node.js privileges" once that is no longer true.
    """
    if node_hardened:
        return (
            "JavaScript tools run under the Node.js permission model "
            "(file system, child processes, worker threads and native "
            "addons denied; network allowed)"
        )
    return "JavaScript tools run with full Node.js privileges (environment sanitization only)"


def _subprocess_isolation_msg(node_hardened: bool) -> str:
    """Build the subprocess-isolation security notice."""
    js_clause = describe_js_subprocess_privileges(node_hardened)
    return (
        "SECURITY: custom tools are executing in a plain subprocess WITHOUT "
        f"the hardened Docker container sandbox. {js_clause}; Python "
        "tools run in isolated mode (-I: PYTHON* env vars, user site "
        "directory and script-dir/cwd sys.path entries ignored) with a "
        "best-effort sandbox (blocked-module import hook, removed os exec "
        "functions) that is defence-in-depth, not a container boundary. "
        "Install Docker on the host (CUSTOM_TOOL_SANDBOX=auto will pick it "
        "up automatically) or set CUSTOM_TOOL_SANDBOX=docker to enforce the "
        "hardened sandbox."
    )


# The Security Audit (``tools.custom_tool_sandbox``) and the
# ``xtmone_custom_tool_sandbox`` gauge report the fallback continuously; the
# log line is its reminder, once a day for the whole platform.
_SUBPROCESS_NOTICE_INTERVAL_SECONDS = 24 * 60 * 60


def _log_subprocess_fallback_breadcrumb() -> None:
    """One-line DEBUG breadcrumb for the executions the notice is not said for."""
    logger.debug(
        "Custom tool executing in the plain subprocess fallback (security notice already logged; "
        "the Security Audit reports it)."
    )


async def _warn_subprocess_isolation_once() -> None:
    """Log a prominent WARNING when running without isolation, once a day for the whole platform.

    WARNING (not ERROR) severity: the subprocess fallback is a *supported*
    operating mode — ``auto`` deliberately falls back when Docker is absent
    (e.g. Kubernetes without Docker-in-Docker) and ``subprocess`` requests it
    explicitly, so an unresolvable ERROR would pollute error-level monitoring
    on every boot. A real misconfiguration — an explicit
    ``docker`` mode without Docker — is logged at ERROR by
    :func:`warn_sandbox_mode_at_startup` instead.

    Every call - the startup check of each process and each tool execution in
    the fallback - goes through :func:`log_once`, keyed by the sandbox mode and
    the Node.js permission model the notice describes: the uvicorn workers,
    their ``MAX_REQUESTS`` recycles, the replicas and the worker say it once a
    day between them, and again after an upgrade or once either setting
    changes. A call it is not said for logs a single short DEBUG
    breadcrumb instead of the multi-line notice, so operators can still
    correlate individual executions with the fallback at DEBUG level. The Node
    probe runs before the notice takes its turn, so a caller cancelled in the
    probe never uses it up, and of concurrent callers one takes the turn.
    """
    node_flags = await _resolve_node_permission_flags()
    said = await log_once(
        logger,
        logging.WARNING,
        f"custom_tool_subprocess_sandbox:{_resolve_sandbox_mode()}:{'node_permission' if node_flags else 'plain'}",
        "%s",
        _subprocess_isolation_msg(bool(node_flags)),
        interval=_SUBPROCESS_NOTICE_INTERVAL_SECONDS,
    )
    if not said:
        _log_subprocess_fallback_breadcrumb()


def _resolve_sandbox_mode() -> str:
    """Resolve the configured sandbox mode (auto|docker|subprocess|opensandbox).

    The legacy ``CUSTOM_TOOL_DOCKER=true`` env var is honoured as ``docker``
    only when ``CUSTOM_TOOL_SANDBOX`` was not provided at all; an explicitly
    configured mode — including an explicit ``auto`` — always wins, so an
    operator can opt back into auto-detection without unsetting the legacy
    env var.
    """
    from app.core.config import settings

    mode = (getattr(settings, "custom_tool_sandbox", "auto") or "auto").strip().lower()
    if mode not in _SANDBOX_MODES:
        mode = "auto"

    explicitly_configured = "custom_tool_sandbox" in settings.model_fields_set
    if mode == "auto" and not explicitly_configured and _legacy_docker_env_enabled():
        _warn_legacy_env_once()
        mode = "docker"

    return mode


async def _run_docker_probe() -> bool:
    """Run a single ``docker info`` probe. Never raises."""
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "info",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=_DOCKER_PROBE_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()
            returncode = -1
        return returncode == 0
    except Exception:
        return False


async def _probe_docker_available() -> bool:
    """Probe Docker availability (cached, single-flight).

    Runs ``docker info`` with a short timeout: it verifies both that the CLI
    is installed and that the daemon is reachable. The sandbox images
    (node:24-slim / python:3.14-slim) are pulled on demand by the daemon on
    first ``docker run`` — the pull happens daemon-side, so ``--network=none``
    does not prevent it. Any failure counts as "unavailable"; the probe never
    raises.

    Caching: a positive result is cached for the process lifetime; a negative
    result is re-probed after ``_DOCKER_PROBE_RETRY_INTERVAL`` so a Docker
    daemon that comes online after startup upgrades ``auto`` mode to the
    hardened sandbox (and unblocks ``docker`` mode) without a restart. An
    ``asyncio.Lock`` makes the probe single-flight under concurrent callers.
    """
    global _docker_available, _docker_probe_failed_at

    def _cached() -> bool | None:
        if _docker_available is True:
            return True
        if _docker_available is False and (time.monotonic() - _docker_probe_failed_at < _DOCKER_PROBE_RETRY_INTERVAL):
            return False
        return None

    cached = _cached()
    if cached is not None:
        return cached

    async with _docker_probe_lock:
        # Re-check after acquiring: a concurrent caller may have just probed.
        cached = _cached()
        if cached is not None:
            return cached

        was_unavailable = _docker_available is False
        available = await _run_docker_probe()
        _docker_available = available
        if not available:
            _docker_probe_failed_at = time.monotonic()
            # INFO on the first failure of the process, DEBUG on every
            # repeat: negative results are re-probed every
            # _DOCKER_PROBE_RETRY_INTERVAL, so a permanently Docker-less host
            # would otherwise repeat this line forever under steady
            # custom-tool traffic. The prior cached state is the latch — no
            # extra global needed (a positive result is cached for the
            # process lifetime, so the pre-probe state here is only ever
            # None on the first probe or False on a re-probe).
            logger.log(
                logging.DEBUG if was_unavailable else logging.INFO,
                "Docker is not available for custom tool sandboxing (`docker info` probe failed).",
            )
        return available


async def _select_sandbox() -> str:
    """Return the effective sandbox backend for the next tool execution.

    Returns one of ``"opensandbox"``, ``"docker"``, or ``"subprocess"``.
    Raises :exc:`RuntimeError` when a required sandbox is explicitly configured
    but not available.
    """
    mode = _resolve_sandbox_mode()

    if mode == "opensandbox":
        if not await _probe_opensandbox_available():
            raise RuntimeError(
                "Custom tool sandbox is set to 'opensandbox' but the OpenSandbox "
                "server is not reachable at the configured OPENSANDBOX_DOMAIN. "
                "Check the server address and OPENSANDBOX_API_KEY."
            )
        return "opensandbox"

    if mode == "docker":
        if not await _probe_docker_available():
            raise RuntimeError(
                "Custom tool sandbox is set to 'docker' but Docker is not "
                "available on the host (the `docker info` probe failed). "
                "Install/start Docker, or set CUSTOM_TOOL_SANDBOX=auto to "
                "allow subprocess fallback."
            )
        return "docker"

    if mode == "subprocess":
        await _warn_subprocess_isolation_once()
        return "subprocess"

    # auto: prefer Docker when available, otherwise warn and fall back.
    if await _probe_docker_available():
        return "docker"
    await _warn_subprocess_isolation_once()
    return "subprocess"


async def get_effective_sandbox() -> str:
    """Return the sandbox custom tools will effectively use.

    One of:
      - ``"docker"``             — hardened Docker sandbox
      - ``"subprocess"``         — plain subprocess (no container isolation)
      - ``"docker_unavailable"`` — mode is ``docker`` but Docker is missing
                                   (tool executions will fail)
      - ``"opensandbox"``              — OpenSandbox-managed container
      - ``"opensandbox_unavailable"``  — mode is ``opensandbox`` but server unreachable

    Used by the security audit and the startup check; never raises.  Also
    refreshes the ``xtmone_custom_tool_sandbox`` Prometheus gauge so
    monitoring can alert on the subprocess fallback.
    """
    mode = _resolve_sandbox_mode()
    if mode == "subprocess":
        effective = "subprocess"
    else:
        if mode == "opensandbox":
            available = await _probe_opensandbox_available()
            effective = "opensandbox" if available else "opensandbox_unavailable"
        else:
            available = await _probe_docker_available()
            if mode == "docker":
                effective = "docker" if available else "docker_unavailable"
            else:
                effective = "docker" if available else "subprocess"
    _set_sandbox_gauge(effective)
    return effective


async def warn_sandbox_mode_at_startup() -> None:
    """Log the effective custom-tool sandbox mode at process startup.

    Should be called once from each process that executes custom tools —
    the platform startup task (``_do_all_startup_work`` in ``main.py``) and
    the SAQ worker startup hook (``startup`` in ``worker.py``) — after the
    event loop is running.  It:

    * Probes Docker availability eagerly, caching the result so the first
      tool execution does not pay the probe cost.
    * Logs the security notice at **WARNING** level when the sandbox falls
      back to a plain subprocess under ``auto`` or ``subprocess`` mode — the
      fallback is a supported operating mode (e.g. Kubernetes without
      Docker-in-Docker), so an unresolvable ERROR would pollute error-level
      monitoring on every boot. The notice is said once a day
      for the whole platform, so the per-execution calls leave a DEBUG
      breadcrumb (no repeated log flood).
    * Logs at **ERROR** level only when ``CUSTOM_TOOL_SANDBOX=docker`` is
      explicitly requested but Docker is unreachable — a real
      misconfiguration: the config demands a boundary that cannot be
      provided, and tool executions will fail.
    * Logs at INFO level when the Docker sandbox is confirmed ready.
    * Publishes the effective sandbox to the ``xtmone_custom_tool_sandbox``
      Prometheus gauge so production alerting can page on the subprocess
      fallback instead of relying on log lines.
    * Probes the Node.js permission model when falling back to a plain
      subprocess, so JavaScript tools get fs/child-process/worker/addon
      denial even without Docker (Node 24+).
    * Is a no-op when custom tools are disabled platform-wide (gauge set
      to ``disabled``).

    Never raises — startup must not fail due to this check.  The only
    exception allowed to propagate is ``asyncio.CancelledError``, so task
    cancellation during startup teardown is never swallowed.
    """

    if _custom_tools_disabled():
        logger.debug("Custom tools are disabled platform-wide; sandbox check skipped.")
        _set_sandbox_gauge("disabled")
        return

    try:
        effective = await get_effective_sandbox()
    except asyncio.CancelledError:
        # Cancellation must keep propagating (startup teardown / shutdown).
        # ``CancelledError`` is a ``BaseException``, so the ``except
        # Exception`` below would not catch it anyway; this clause makes the
        # intent explicit and survives future edits (mirrors
        # ``release_db_connection_before_loop`` in core/database.py).
        raise
    except Exception:
        logger.warning(
            "Custom tool sandbox probe failed at startup; sandbox mode unknown.",
            exc_info=True,
        )
        return

    if effective == "docker":
        logger.info(
            "Custom tool sandbox: Docker (hardened container, no network, "
            "read-only filesystem, dropped capabilities, memory cap)."
        )
    elif effective == "docker_unavailable":
        logger.error(
            "SECURITY: CUSTOM_TOOL_SANDBOX=docker but Docker is not reachable "
            "(`docker info` probe failed). Custom tool executions will fail "
            "until Docker is available on the host. Install/start Docker or "
            "set CUSTOM_TOOL_SANDBOX=auto to allow subprocess fallback."
        )
    elif effective == "opensandbox":
        logger.info(
            "Custom tool sandbox: OpenSandbox (Micro VM with hardened container, no network, "
            "read-only filesystem, dropped capabilities, memory cap)."
        )
    elif effective == "opensandbox_unavailable":
        logger.error(
            "SECURITY: CUSTOM_TOOL_SANDBOX=opensandbox but OpenSandbox is not reachable "
            "(`opensandbox-server` probe failed). Custom tool executions will fail "
            "until OpenSandbox is available on the host. Install/start OpenSandbox or "
            "set CUSTOM_TOOL_SANDBOX=auto to allow subprocess fallback."
        )
    else:
        # subprocess fallback — a supported operating mode (auto without
        # Docker, or explicit subprocess). Emit the security notice now, at
        # startup and at WARNING severity, unless another process said it
        # today. The notice resolves the Node.js permission model first, so
        # the JS clause reflects the actual privilege level of the fallback.
        await _warn_subprocess_isolation_once()


async def execute_js_tool_docker(
    js_function: str,
    inputs: dict,
    timeout: int = 300,
) -> str:
    """Execute a JavaScript function inside a Docker container.

    The container is created, run, and removed automatically.
    Returns the stdout output of the script.
    Raises RuntimeError on timeout, Docker failure, or execution failure.
    """

    if _custom_tools_disabled():
        raise RuntimeError(CUSTOM_TOOLS_DISABLED_MSG)

    script = _build_js_script(js_function, inputs)

    container_name = f"xtm-tool-js-{uuid.uuid4().hex}"
    docker_args = [
        "docker",
        "run",
        "--rm",  # Remove container after exit
        "-i",  # Keep stdin open for piping
        f"--name={container_name}",  # Named so a timeout can force-remove it
        "--network=none",  # No network access
        "--read-only",  # Read-only root filesystem
        "--cap-drop=ALL",  # Drop all Linux capabilities
        f"--memory={_DOCKER_MEMORY_LIMIT}",
        # memory-swap == memory disables swap, making the memory cap a hard
        # limit (-1 would mean UNLIMITED swap on top of the cap).
        f"--memory-swap={_DOCKER_MEMORY_LIMIT}",
        f"--stop-timeout={timeout}",
        "--pids-limit=50",  # Limit process count
        _DOCKER_IMAGE,
        "node",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *docker_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=script.encode("utf-8")),
                timeout=timeout + 5,  # Extra buffer for Docker overhead
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            await _docker_force_remove(container_name)
            raise RuntimeError(f"Docker tool execution timed out after {timeout}s") from exc

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            # Strip Node.js runtime warnings before parsing/surfacing the error.
            clean_stderr = _filter_node_warnings(stderr_text)
            error_msg = clean_stderr or f"Container exited with code {process.returncode}"
            try:
                err_data = json.loads(clean_stderr)
                error_msg = err_data.get("error", error_msg)
            except (json.JSONDecodeError, AttributeError):
                # keep the cleaned stderr as error_msg
                logger.debug("Could not parse structured sandbox error from stderr")
            raise RuntimeError(f"Tool execution failed: {error_msg}")

        return stdout_text or _filter_node_warnings(stderr_text) or "(no output)"

    except FileNotFoundError as exc:
        raise RuntimeError(
            "Docker is not installed or not in PATH. Docker sandboxing requires Docker to be available on the host."
        ) from exc


async def execute_js_tool_auto(
    js_function: str,
    inputs: dict,
    timeout: int = 300,
    *,
    use_docker: bool | None = None,
    user_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
) -> str:
    """Execute a JavaScript function using the configured sandbox mode.

    When ``use_docker`` is True, runs in Docker. When False, uses subprocess.
    When None, resolves the CUSTOM_TOOL_SANDBOX setting
    (auto|docker|subprocess|opensandbox, legacy CUSTOM_TOOL_DOCKER=true is
    honoured as docker when the setting is unset).

    *user_id*/*db*, when provided, are forwarded to the ``opensandbox``
    branch only for per-user sandbox usage recording (spec
    002-sandbox-quota-usage-tracking, FR-011 for *db*'s period-resolution
    role) — mirrors ``execute_tool_auto``.
    """
    sandbox = await _select_sandbox() if use_docker is None else ("docker" if use_docker else "subprocess")
    if sandbox == "opensandbox":
        return await execute_js_tool_opensandbox(js_function, inputs, timeout, user_id=user_id, db=db)
    if sandbox == "docker":
        return await execute_js_tool_docker(js_function, inputs, timeout)
    return await execute_js_tool(js_function, inputs, timeout)


# ---------------------------------------------------------------------------
# Python execution
# ---------------------------------------------------------------------------

# Tracks which allow-lists have already been warned about so the security
# warning is logged once per distinct configuration, not on every execution.
_ct_warned_allow_lists: set[frozenset[str]] = set()


def _resolve_custom_tool_allowed_modules() -> frozenset[str]:
    """Return the admin-configured set of modules to re-enable in the custom tool sandbox.

    Reads the ``custom_tool_extra_allowed_modules`` security option (Settings >
    Security, or ``CUSTOM_TOOL_EXTRA_ALLOWED_MODULES``, which wins). Logs a one-time WARNING
    per distinct non-empty configuration because re-enabling blocked modules
    (notably ``requests`` / the HTTP stack) restores network access to sandboxed
    code running in the subprocess fallback.
    """
    from app.core.security_options import security_option

    allow = security_option("custom_tool_extra_allowed_modules")
    if allow and allow not in _ct_warned_allow_lists:
        _ct_warned_allow_lists.add(allow)
        logger.warning(
            "Custom tool Python sandbox: re-enabling normally-blocked module(s) %s "
            "(CUSTOM_TOOL_EXTRA_ALLOWED_MODULES or Settings > Access and security). This grants sandboxed code "
            "access to those modules (e.g. 'requests' restores HTTP network access). "
            "Only do this when the backend runs with appropriate network isolation "
            "or when using the Docker sandbox (CUSTOM_TOOL_SANDBOX=docker).",
            ", ".join(sorted(allow)),
        )
    return allow


def _build_python_sandbox_header(allow: frozenset[str] | None = None) -> str:
    """Assemble the Python sandbox header, applying *allow* to the blocklist.

    The returned string is the prefix prepended to every custom Python tool script.
    It scrubs env vars, installs the import blocker, and strips dangerous os
    functions.  *allow* lets the admin opt selected blocked modules back in
    (e.g. ``requests``, ``urllib``) without weakening the shared blocklist or
    the code interpreter sandbox.
    """
    return (
        """\
import builtins as _builtins
import os as _os

# ---- Scrub environment variables -------------------------------------------
_KEEP_ENV = {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
             "TMPDIR", "TMP", "TEMP", "PYTHONIOENCODING",
             "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED",
             "SYSTEMROOT", "COMSPEC",
             # The native thread-pool caps (_sandbox_selector.NATIVE_THREAD_ENV_KEYS)
             "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"}
for _k in list(_os.environ):
    if _k.upper() not in _KEEP_ENV:
        del _os.environ[_k]

"""
        + _build_sandbox_security_src("_builtins", "custom tool sandbox", allow=allow)
        + """\

for _fn in ("system", "popen", "exec", "execl", "execle", "execlp",
            "execlpe", "execv", "execve", "execvp", "execvpe",
            "spawnl", "spawnle", "spawnlp", "spawnlpe",
            "spawnv", "spawnve", "spawnvp", "spawnvpe"):
    if hasattr(_os, _fn):
        delattr(_os, _fn)
"""
    )


def _safe_python_identifier(key: str, used: set[str]) -> str:
    """Map an arbitrary input key to a unique, valid Python identifier.

    Sanitization alone is not enough: keys can start with a digit, collapse
    to an empty string, collide after replacement, or be Python keywords —
    each of which would either be a ``SyntaxError`` (killing the WHOLE
    generated script, including the ``__xtm_tool_inputs__`` binding) or
    silently overwrite a previously bound variable.
    """
    candidate = re.sub(r"[^a-zA-Z0-9_]", "_", key)
    if not candidate or candidate[0].isdigit():
        candidate = f"_{candidate}"
    if keyword.iskeyword(candidate):
        candidate = f"{candidate}_"
    if candidate in used:
        suffix = 2
        while f"{candidate}_{suffix}" in used:
            suffix += 1
        candidate = f"{candidate}_{suffix}"
    used.add(candidate)
    return candidate


def _build_python_script(py_function: str, inputs: dict) -> str:
    """Build a complete Python script by injecting input variables.

    The full inputs dict is exposed as ``__xtm_tool_inputs__`` (the source
    of truth) and each input is also bound as a plain ``variable_name`` at
    the top, after the sandbox security header. Values are decoded from an
    embedded JSON literal rather than spliced as raw ``json.dumps`` output,
    because JSON ``true`` / ``false`` / ``null`` are not valid Python
    literals.
    """
    lines = [_build_python_sandbox_header(_resolve_custom_tool_allowed_modules()), ""]
    lines.append("import json as _xtm_json")
    lines.append(f"__xtm_tool_inputs__ = _xtm_json.loads({json.dumps(json.dumps(inputs))})")

    used_keys: set[str] = {"_xtm_json", "__xtm_tool_inputs__"}
    for key in inputs:
        safe_key = _safe_python_identifier(key, used_keys)
        lines.append(f"{safe_key} = __xtm_tool_inputs__[{json.dumps(key)}]")

    lines.append("")
    lines.append(py_function)

    return "\n".join(lines)


async def execute_python_tool(
    py_function: str,
    inputs: dict,
    timeout: int = 300,
) -> str:
    """Execute a Python function with the given inputs.

    Returns the stdout output of the script.
    Raises RuntimeError on timeout or execution failure.
    """

    if _custom_tools_disabled():
        raise RuntimeError(CUSTOM_TOOLS_DISABLED_MSG)

    script = _build_python_script(py_function, inputs)
    tmp_dir = tempfile.mkdtemp(prefix="copilot_tool_py_")
    script_file = _os.path.join(tmp_dir, "_tool.py")

    try:
        with open(script_file, "w", encoding="utf-8") as f:
            f.write(script)

        # -I (isolated mode) implies -E -s -P: PYTHON* env vars are ignored,
        # the user site directory is excluded, and neither the script
        # directory nor the CWD is prepended to sys.path — the sanitized env
        # from _build_sandbox_env() can then never influence interpreter
        # startup. -X utf8 pins I/O encoding since -E drops PYTHONIOENCODING.
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-X",
            "utf8",
            script_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmp_dir,
            env=_build_sandbox_env(),
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise RuntimeError(f"Tool execution timed out after {timeout}s") from exc

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            error_msg = stderr_text or f"Process exited with code {process.returncode}"
            raise RuntimeError(f"Tool execution failed: {error_msg}")

        return stdout_text or stderr_text or "(no output)"

    except FileNotFoundError as exc:
        raise RuntimeError(f"Python interpreter not found at {sys.executable}.") from exc
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)


_PYTHON_DOCKER_IMAGE = "python:3.14-slim"


async def execute_python_tool_docker(
    py_function: str,
    inputs: dict,
    timeout: int = 300,
) -> str:
    """Execute a Python function inside a Docker container."""

    if _custom_tools_disabled():
        raise RuntimeError(CUSTOM_TOOLS_DISABLED_MSG)

    script = _build_python_script(py_function, inputs)

    container_name = f"xtm-tool-py-{uuid.uuid4().hex}"
    docker_args = [
        "docker",
        "run",
        "--rm",
        "-i",  # Keep stdin open for piping
        f"--name={container_name}",  # Named so a timeout can force-remove it
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        f"--memory={_DOCKER_MEMORY_LIMIT}",
        # memory-swap == memory disables swap (see execute_js_tool_docker).
        f"--memory-swap={_DOCKER_MEMORY_LIMIT}",
        f"--stop-timeout={timeout}",
        "--pids-limit=50",
        _PYTHON_DOCKER_IMAGE,
        "python",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *docker_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=script.encode("utf-8")),
                timeout=timeout + 5,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            await _docker_force_remove(container_name)
            raise RuntimeError(f"Docker tool execution timed out after {timeout}s") from exc

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            error_msg = stderr_text or f"Container exited with code {process.returncode}"
            raise RuntimeError(f"Tool execution failed: {error_msg}")

        return stdout_text or stderr_text or "(no output)"

    except FileNotFoundError as exc:
        raise RuntimeError("Docker is not installed or not in PATH.") from exc


# ---------------------------------------------------------------------------
# OpenSandbox execution
# ---------------------------------------------------------------------------

_SANDBOX_REMOTE_DIR = "/tmp"  # noqa: S108

# Seconds of the tool's timeout kept back from the executor's own deadline.
# The tool's timeout is also the agent loop's per-tool timeout (300 s by
# default) and the sandbox's lifetime: every stage (create, upload, run) ends
# within ``timeout - margin`` of the call's start, so the executor answers
# with a stage-named message, and the kill and the logging that follow still
# fit, before the loop's own "timed out after 300s" (5 s was eaten by
# the quota check, an unbounded create or upload, and the kill).
_COMMAND_DEADLINE_MARGIN_SECONDS = 20


def _command_deadline(timeout: float, lifetime_started: float, now: float) -> float:
    """Seconds a stage may take: the budget left, less the margin, unclamped.

    The result is zero or negative once the lifetime is spent: the caller
    decides whether the stage may still start. The margin is a tenth of a
    short timeout (at least 1 s), so a tool with a small timeout keeps most
    of it.
    """
    return timeout - (now - lifetime_started) - _deadline_margin(timeout)


def _deadline_margin(timeout: float) -> int:
    """The seconds kept back from *timeout*: the margin, a tenth of a short timeout, at least 1."""
    return min(_COMMAND_DEADLINE_MARGIN_SECONDS, max(1, int(timeout) // 10))


class _StageTimeoutError(RuntimeError):
    """A sandbox stage outlived the call's budget; the message names the stage."""


class _StageClock:
    """Per-stage durations of one OpenSandbox custom tool call.

    Each stage (quota, create, upload, run, cleanup) is timed from the end of
    the previous one; :meth:`publish` puts the figures on the tool span the
    call runs under (``custom_tool.<stage>_ms``, ``custom_tool.total_ms``,
    ``custom_tool.outcome``), so a retest reads where the time went from the
    trace instead of the end-to-end span duration alone.
    """

    def __init__(self, started: float) -> None:
        self.started = started
        self._mark = started
        self.stages: dict[str, float] = {}

    def lap(self, stage: str) -> None:
        now = time.monotonic()
        self.stages[stage] = self.stages.get(stage, 0.0) + (now - self._mark)
        self._mark = now

    def summary(self) -> str:
        """The stages timed so far, as ``quota 0.0s, create 1.2s``, for an error text."""
        return ", ".join(f"{name} {seconds:.1f}s" for name, seconds in self.stages.items())

    def publish(self, outcome: str) -> None:
        """Annotate the current tool span; outside a tool call it does nothing, never raises."""
        fields = {f"custom_tool.{name}_ms": f"{seconds * 1000.0:.0f}" for name, seconds in self.stages.items()}
        fields["custom_tool.total_ms"] = f"{(time.monotonic() - self.started) * 1000.0:.0f}"
        fields["custom_tool.outcome"] = outcome
        with contextlib.suppress(Exception):
            from app.services.langfuse_tracing import annotate_current_tool_span

            annotate_current_tool_span(fields)


# A create stage longer than this is named in the error text of a failed call:
# the reader of the tool result sees that the platform, not the tool,
# spent the time, without opening the trace.
_SLOW_CREATE_SECONDS = 10.0


def _with_slow_create(exc: RuntimeError, clock: _StageClock) -> RuntimeError | None:
    """*exc* with the stage figures appended when the create stage was slow, else ``None``."""
    create = clock.stages.get("create", 0.0)
    if create <= _SLOW_CREATE_SECONDS:
        return None
    note = (
        f" [stages: {clock.summary()}. Creating the sandbox took {create:.0f}s (over "
        f"{_SLOW_CREATE_SECONDS:.0f}s): the sandbox service was slow to start a container, "
        "not the tool's code.]"
    )
    try:
        return type(exc)(f"{exc}{note}")
    except TypeError:  # a subclass with its own constructor: the text matters, not the type
        return RuntimeError(f"{exc}{note}")


@contextlib.asynccontextmanager
async def _timed_call(started: float):
    """A :class:`_StageClock` whose figures reach the tool span however the call ends.

    A failed call whose create stage was slow names it in its error.
    """
    clock = _StageClock(started)
    try:
        yield clock
    except _StageTimeoutError as exc:
        clock.publish("stage_timeout")
        slow = _with_slow_create(exc, clock)
        if slow is not None:
            raise slow from exc
        raise
    except RuntimeError as exc:
        clock.publish("error")
        slow = _with_slow_create(exc, clock)
        if slow is not None:
            raise slow from exc
        raise
    except BaseException:
        clock.publish("error")
        raise
    clock.publish("ok")


_STAGE_HINTS = {
    "create": (
        "the sandbox service did not start a container in time. This is a platform "
        "problem, not a defect of the tool: retry once; if it fails again, report that "
        "the sandbox service is slow or unavailable."
    ),
    "upload": (
        "the sandbox did not accept the tool's script in time. This is a platform "
        "problem, not a defect of the tool: retry once."
    ),
}


def _run_stage_hint(egress_allow_list: frozenset[str] | list[str]) -> str:
    allowed = ", ".join(sorted(egress_allow_list)) or "none"
    return (
        "the command did not report its end. The tool's code may wait on something that "
        "never settles: an unawaited promise, a timer, or a network call the sandbox's "
        "egress policy holds (default deny; hosts allowed by OPENSANDBOX_NETWORK_ALLOW: "
        f"{allowed}). Shorten the work or call the API through an integration (an OpenAPI "
        "integration runs its calls on the backend)."
    )


async def _bounded_stage(
    awaitable,
    stage: str,
    timeout: int,
    lifetime_started: float,
    *,
    sandbox=None,
    hint: str = "",
    deadline: float | None = None,
):
    """Await one sandbox stage within what is left of the call's budget.

    The budget (*timeout* less ``_COMMAND_DEADLINE_MARGIN_SECONDS``) runs from
    the call's start (*lifetime_started*, a ``time.monotonic()`` reading), so
    each stage only gets the time the previous ones left. On expiry the
    sandbox, when there is one, is killed and :class:`_StageTimeoutError`
    names the stage and what it means. *deadline*, when given, is the bound
    the caller already computed (the run stage); otherwise the create and
    upload stages get at least 1 s.
    """
    from app.services.sandbox_call_logging import kill_sandbox_best_effort

    if deadline is None:
        deadline = max(1.0, _command_deadline(timeout, lifetime_started, time.monotonic()))
    try:
        return await asyncio.wait_for(awaitable, timeout=deadline)
    except TimeoutError as exc:
        if sandbox is not None:
            # The kill fits in the margin, so a short timeout is not overrun
            # by cleanup; the sandbox's TTL is the backstop.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    kill_sandbox_best_effort(sandbox), timeout=max(1, _deadline_margin(timeout))
                )
        elapsed = time.monotonic() - lifetime_started
        raise _StageTimeoutError(
            f"Tool execution timed out after {int(deadline)}s (stage {stage}, {elapsed:.0f}s into the "
            f"call, timeout {timeout}s): {hint or _STAGE_HINTS.get(stage, '')}"
        ) from exc


async def _run_bounded(
    sandbox,
    command: str,
    timeout: int,
    lifetime_started: float,
    egress_allow_list: frozenset[str] | list[str] = (),
):
    """``sandbox.commands.run(command)`` within what is left of the call's budget.

    Without a bound, a command that never reports its end (a process kept
    alive by an open handle, a stream that does not close) holds the call
    until the sandbox's lifetime expires. The call ends before, with a
    message naming the stage and the egress policy, and the sandbox is killed.

    When creating the sandbox and uploading the script left less than a
    second, the command is not started at all: it would outlive the sandbox
    and end on a transport error instead of a stage-named message.
    """
    from app.services.sandbox_call_logging import kill_sandbox_best_effort

    remaining = _command_deadline(timeout, lifetime_started, time.monotonic())
    if remaining < 1:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                kill_sandbox_best_effort(sandbox), timeout=max(1, _deadline_margin(timeout))
            )
        elapsed = time.monotonic() - lifetime_started
        raise _StageTimeoutError(
            f"Tool execution timed out (stage setup, {elapsed:.0f}s into the call, timeout "
            f"{timeout}s): creating the sandbox and uploading the script used the tool's time, "
            "nothing was run. Retry once; if it happens again, the sandbox service is slow."
        )
    return await _bounded_stage(
        sandbox.commands.run(command),
        "run",
        timeout,
        lifetime_started,
        sandbox=sandbox,
        hint=_run_stage_hint(egress_allow_list),
        deadline=remaining,
    )


async def execute_js_tool_opensandbox(
    js_function: str,
    inputs: dict,
    timeout: int = 300,
    *,
    user_id: uuid.UUID | None = None,
    quota_user_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
) -> str:
    """Execute a JavaScript function inside an OpenSandbox-managed container.

    Uploads the generated script as a temp file, runs it with ``node``,
    and returns stdout.  Raises :exc:`RuntimeError` on failure.

    *user_id*, when provided, records one unit of per-user sandbox usage
    (``quota_usage`` table, ``usage_type="sandbox"``) once execution reaches
    this function's single success path (spec 002-sandbox-quota-usage-tracking
    FR-002). ``None`` (the default) skips usage recording entirely. *db*,
    when provided alongside *user_id*, lets that recording resolve a
    per-user/group/platform-configured period (spec FR-011) instead of the
    monthly default.

    *quota_user_id*, when provided, is used instead of *user_id* for the
    combined agentic+sandbox quota check and usage recording only — file
    ownership and structured call logging stay on *user_id*. Lets a caller
    with a real user to charge quota against but no personal file owner
    (e.g. a company-managed assignment run) still be metered. Defaults to
    *user_id* when not given.
    """
    if _custom_tools_disabled():
        raise RuntimeError(CUSTOM_TOOLS_DISABLED_MSG)

    # The call's budget (and, conservatively, the sandbox's lifetime) counts
    # from here: the quota check, the create and the upload all spend it.
    # Every stage is timed and the durations reach the tool span.
    lifetime_started = time.monotonic()
    async with _timed_call(lifetime_started) as clock:
        return await _run_js_tool_opensandbox(
            clock,
            js_function,
            inputs,
            timeout,
            user_id=user_id,
            quota_user_id=quota_user_id,
            db=db,
        )


async def _run_js_tool_opensandbox(
    clock: _StageClock,
    js_function: str,
    inputs: dict,
    timeout: int,
    *,
    user_id: uuid.UUID | None,
    quota_user_id: uuid.UUID | None,
    db: AsyncSession | None,
) -> str:
    """The stages of :func:`execute_js_tool_opensandbox`, each timed on *clock*."""
    from app.core.config import settings
    from app.services._sandbox_selector import _create_opensandbox
    from app.services.sandbox_call_logging import (
        kill_sandbox_best_effort,
        log_execution_errors,
        log_sandbox_call,
    )

    lifetime_started = clock.started
    effective_quota_user_id = quota_user_id if quota_user_id is not None else user_id
    # Combined agentic+sandbox quota check (spec 002-sandbox-quota-usage-tracking
    # FR-015/FR-018/FR-019). Raises RuntimeError (not a dict return, unlike
    # code_interpreter.py's execute_python_opensandbox) since this function
    # returns str — CallableToolExecutor.execute_tool (app/tools/base.py)
    # catches it generically into a normal ToolResult, never an unhandled
    # crash. Called unconditionally, even when effective_quota_user_id is
    # None (no attributable user) — the platform-wide hard cap inside still
    # applies in that case; only the per-user limit is skipped.
    from app.services.quota import (
        QUOTA_EXCEEDED_MESSAGE,
        QUOTA_UNAVAILABLE_MESSAGE,
        QuotaCheckUnavailableError,
        QuotaExceededError,
        enforce_combined_sandbox_quota,
    )

    try:
        await enforce_combined_sandbox_quota(effective_quota_user_id)
    except QuotaExceededError as exc:
        raise RuntimeError(QUOTA_EXCEEDED_MESSAGE) from exc
    except QuotaCheckUnavailableError as exc:
        raise RuntimeError(QUOTA_UNAVAILABLE_MESSAGE) from exc
    finally:
        clock.lap("quota")

    _egress_allow_list = settings.opensandbox_network_allow_list
    # The fetch pre-check answers a host outside the allow-list at once
    # instead of after the connect timeout; the policy still enforces.
    script = _build_js_script(js_function, inputs, egress_allow_list=_egress_allow_list)

    try:
        sandbox = await _bounded_stage(
            _create_opensandbox(
                image_key="nodejs",
                timeout_seconds=timeout,
                kind_label="JS",
                usage_type="custom_tool_js",
                user_id=user_id,
                egress_allow_list=_egress_allow_list,
                resource=settings.sandbox_resources("nodejs"),
            ),
            "create",
            timeout,
            lifetime_started,
        )
    except _StageTimeoutError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Failed to create sandbox: {exc}") from exc
    finally:
        clock.lap("create")
    async with sandbox:
        remote_script = f"{_SANDBOX_REMOTE_DIR}/_xtm_tool.js"
        try:
            await _bounded_stage(
                sandbox.files.write_file(remote_script, script.encode("utf-8")),
                "upload",
                timeout,
                lifetime_started,
                sandbox=sandbox,
            )
        finally:
            clock.lap("upload")

        try:
            async with log_execution_errors(
                usage_type="custom_tool_js", user_id=user_id, egress_allow_list=_egress_allow_list
            ):
                result = await _run_bounded(
                    sandbox, f"node {remote_script}", timeout, lifetime_started, _egress_allow_list
                )
                stdout_text = "\n".join(line.text for line in result.logs.stdout).strip()
                stderr_text = "\n".join(line.text for line in result.logs.stderr).strip()
        finally:
            clock.lap("run")

        if result.exit_code is not None and result.exit_code != 0:
            error_msg = stderr_text or f"Sandbox exited with code {result.exit_code}"
            try:
                err_data = json.loads(stderr_text)
                error_msg = _explain_network_failure(
                    str(err_data.get("error", error_msg)),
                    str(err_data.get("cause") or ""),
                    _egress_allow_list,
                )
            except (json.JSONDecodeError, AttributeError):
                # keep the raw stderr as error_msg
                logger.debug("Could not parse structured sandbox error from stderr")
            await log_sandbox_call(
                usage_type="custom_tool_js",
                user_id=user_id,
                outcome="failure",
                failure_reason="nonzero_exit",
                sandbox=sandbox,
                execution_complete=result.complete,
                egress_allow_list=_egress_allow_list,
            )
            await kill_sandbox_best_effort(sandbox)
            clock.lap("cleanup")
            raise RuntimeError(f"Tool execution failed: {error_msg}")

        from app.services.quota import increment_sandbox_usage

        await increment_sandbox_usage(effective_quota_user_id, db=db)

        await log_sandbox_call(
            usage_type="custom_tool_js",
            user_id=user_id,
            outcome="success",
            sandbox=sandbox,
            execution_complete=result.complete,
            egress_allow_list=_egress_allow_list,
        )
        await kill_sandbox_best_effort(sandbox)
        clock.lap("cleanup")

        return stdout_text or stderr_text or "(no output)"


async def execute_python_tool_opensandbox(
    py_function: str,
    inputs: dict,
    timeout: int = 300,
    *,
    user_id: uuid.UUID | None = None,
    quota_user_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
) -> str:
    """Execute a Python function inside an OpenSandbox-managed container.

    Uploads the generated script as a temp file, runs it with ``python``,
    and returns stdout.  Raises :exc:`RuntimeError` on failure.

    *user_id*, when provided, records one unit of per-user sandbox usage
    (``quota_usage`` table, ``usage_type="sandbox"``) once execution reaches
    this function's single success path (spec 002-sandbox-quota-usage-tracking
    FR-002). ``None`` (the default) skips usage recording entirely. *db*,
    when provided alongside *user_id*, lets that recording resolve a
    per-user/group/platform-configured period (spec FR-011) instead of the
    monthly default.

    *quota_user_id*, when provided, is used instead of *user_id* for the
    combined agentic+sandbox quota check and usage recording only — file
    ownership and structured call logging stay on *user_id*. Lets a caller
    with a real user to charge quota against but no personal file owner
    (e.g. a company-managed assignment run) still be metered. Defaults to
    *user_id* when not given.
    """
    from app.core.config import settings

    if _custom_tools_disabled():
        raise RuntimeError(CUSTOM_TOOLS_DISABLED_MSG)

    from app.services._sandbox_selector import _create_opensandbox
    from app.services.sandbox_call_logging import (
        kill_sandbox_best_effort,
        log_execution_errors,
        log_sandbox_call,
    )
    # Combined agentic+sandbox quota check — see the identical block (and its
    # rationale comment) in execute_js_tool_opensandbox above. Called
    # unconditionally — see that same block's comment on why.
    # The call's budget (and, conservatively, the sandbox's lifetime) counts
    # from here: the quota check, the create and the upload all spend it.
    lifetime_started = time.monotonic()
    effective_quota_user_id = quota_user_id if quota_user_id is not None else user_id
    from app.services.quota import (
        QUOTA_EXCEEDED_MESSAGE,
        QUOTA_UNAVAILABLE_MESSAGE,
        QuotaCheckUnavailableError,
        QuotaExceededError,
        enforce_combined_sandbox_quota,
    )

    try:
        await enforce_combined_sandbox_quota(effective_quota_user_id)
    except QuotaExceededError as exc:
        raise RuntimeError(QUOTA_EXCEEDED_MESSAGE) from exc
    except QuotaCheckUnavailableError as exc:
        raise RuntimeError(QUOTA_UNAVAILABLE_MESSAGE) from exc

    script = _build_python_script(py_function, inputs)
    _egress_allow_list = settings.opensandbox_network_allow_list

    try:
        sandbox = await _bounded_stage(
            _create_opensandbox(
                image_key="python",
                timeout_seconds=timeout,
                kind_label="Python",
                usage_type="custom_tool_python",
                user_id=user_id,
                egress_allow_list=_egress_allow_list,
                resource=settings.sandbox_resources("python"),
            ),
            "create",
            timeout,
            lifetime_started,
        )
    except _StageTimeoutError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Failed to create sandbox: {exc}") from exc
    async with sandbox:
        remote_script = f"{_SANDBOX_REMOTE_DIR}/_xtm_tool.py"
        await _bounded_stage(
            sandbox.files.write_file(remote_script, script.encode("utf-8")),
            "upload",
            timeout,
            lifetime_started,
            sandbox=sandbox,
        )

        async with log_execution_errors(
            usage_type="custom_tool_python", user_id=user_id, egress_allow_list=_egress_allow_list
        ):
            result = await _run_bounded(
                sandbox, f"python {remote_script}", timeout, lifetime_started, _egress_allow_list
            )
            stdout_text = "\n".join(line.text for line in result.logs.stdout).strip()
            stderr_text = "\n".join(line.text for line in result.logs.stderr).strip()

        if result.exit_code is not None and result.exit_code != 0:
            error_msg = stderr_text or f"Sandbox exited with code {result.exit_code}"
            await log_sandbox_call(
                usage_type="custom_tool_python",
                user_id=user_id,
                outcome="failure",
                failure_reason="nonzero_exit",
                sandbox=sandbox,
                execution_complete=result.complete,
                egress_allow_list=_egress_allow_list,
            )
            await kill_sandbox_best_effort(sandbox)
            raise RuntimeError(f"Tool execution failed: {error_msg}")

        from app.services.quota import increment_sandbox_usage

        await increment_sandbox_usage(effective_quota_user_id, db=db)

        await log_sandbox_call(
            usage_type="custom_tool_python",
            user_id=user_id,
            outcome="success",
            sandbox=sandbox,
            execution_complete=result.complete,
            egress_allow_list=_egress_allow_list,
        )
        await kill_sandbox_best_effort(sandbox)

        return stdout_text or stderr_text or "(no output)"


# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------


async def execute_tool_auto(
    function_code: str,
    inputs: dict,
    language: str = "javascript",
    timeout: int = 300,
    *,
    use_docker: bool | None = None,
    user_id: uuid.UUID | None = None,
    quota_user_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
) -> str:
    """Execute a custom tool in the appropriate language sandbox.

    When ``use_docker`` is None, resolves the CUSTOM_TOOL_SANDBOX setting
    (auto|docker|subprocess|opensandbox, legacy CUSTOM_TOOL_DOCKER=true
    honoured as docker when the setting is unset).

    *user_id*/*quota_user_id*/*db*, when provided, are forwarded to the
    ``opensandbox`` branches only for the combined quota check and per-user
    sandbox usage recording (spec 002-sandbox-quota-usage-tracking, FR-011
    for *db*'s period-resolution role) — the docker/subprocess branches are
    out of scope for usage tracking, so all three are silently unused there.

    Every run receives ``dry_run`` (``$dry_run`` in JavaScript), ``False``
    unless the caller - a dry-run test case - sets it, so a tool can check it
    unconditionally (``services/custom_tool_tests.py``). A tool whose own
    input schema declares ``dry_run`` keeps the value it was given.
    """
    inputs = {"dry_run": False, **inputs}
    sandbox = await _select_sandbox() if use_docker is None else ("docker" if use_docker else "subprocess")
    if language == "python":
        if sandbox == "opensandbox":
            return await execute_python_tool_opensandbox(
                function_code, inputs, timeout, user_id=user_id, quota_user_id=quota_user_id, db=db
            )
        if sandbox == "docker":
            return await execute_python_tool_docker(function_code, inputs, timeout)
        return await execute_python_tool(function_code, inputs, timeout)

    if sandbox == "opensandbox":
        return await execute_js_tool_opensandbox(
            function_code, inputs, timeout, user_id=user_id, quota_user_id=quota_user_id, db=db
        )
    if sandbox == "docker":
        return await execute_js_tool_docker(function_code, inputs, timeout)
    return await execute_js_tool(function_code, inputs, timeout)
