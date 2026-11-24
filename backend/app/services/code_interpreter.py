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

"""Code interpreter – executes Python code in a sandboxed subprocess.

Python code is executed in an isolated subprocess with strict timeout
enforcement.  Supports matplotlib / seaborn for chart generation —
figures are returned as base64-encoded PNG images.

Security measures:
  - **Process isolation**: every execution runs in a fresh subprocess.
  - **Timeout enforcement**: asyncio.wait_for() kills the process on expiry.
  - **Temporary workspace**: code runs in a disposable temp directory that
    is cleaned up after execution.
  - **Non-interactive backend**: matplotlib forced to ``Agg`` backend.
  - **Environment sanitization**: only safe env vars are passed to subprocess;
    all secrets (DATABASE_URL, SECRET_KEY, etc.) are stripped.
  - **Module blocklist**: database drivers, network libraries, and other
    dangerous modules are blocked inside the sandbox. Admins can re-enable
    specific modules (e.g. ``socket``) for this sandbox only via the
    ``CODE_INTERPRETER_EXTRA_ALLOWED_MODULES`` setting — see its security note
    in ``core/config.py`` (the import blocklist is the only network guard when
    the backend is not network-isolated).
  - **os.environ scrubbed**: env dict is cleared inside the wrapper before
    user code runs.

For production deployments, run the backend inside a Docker container to
provide additional OS-level isolation (seccomp, namespaces, cgroups).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import ntpath
import os
import re
import sys
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

from app.services._sandbox_utils import build_sandbox_security_src as _build_sandbox_security_src

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.sandbox_workspace.session import WorkspaceSession
    from app.tools.base import ToolDefinition

logger = logging.getLogger(__name__)

# Default execution timeout in seconds
DEFAULT_TIMEOUT = 30

# The longest the create stage of an OpenSandbox run may take. The server
# answers HTTP 504 for a pod not ready within its own 60 s and
# ``_create_opensandbox`` retries that once, so a create that can succeed, a
# cold pod included, has answered by then. Past it the call answers "stage
# create" instead of spending the rest of the tool's time (the agent loop's
# 300 s) waiting on a container that does not come.
CREATE_STAGE_MAX_SECONDS = 130.0
# Seconds of the call's timeout kept back so a stage that overruns is answered,
# and its container killed, before the agent loop's own per-tool timeout.
_STAGE_MARGIN_MAX_SECONDS = 15

_STAGE_HINTS = {
    "workspace": (
        "the workspace files were not ready in time. Name only the workspace files the code "
        "needs (literal paths such as workspace/data.csv) and retry."
    ),
    "create": (
        "the sandbox service did not start a container in time. This is a platform problem, "
        "not a defect of the code: retry once; if it fails again, report that the sandbox "
        "service is slow or out of capacity for the python image key."
    ),
    "run": (
        "the code did not finish within the call's time. Shorten the work, split it into "
        "several calls, or keep state between calls with session=true."
    ),
    "upload": (
        "the workspace files the code names did not reach the container in time. Name only "
        "the files the code needs, by literal path (workspace/data.csv rather than a path built "
        "at run time, which mounts the whole workspace)."
    ),
}
# A mounted workspace file at least this large gets the dedicated tools named
# when a stage overruns: rewriting a large file through code_interpreter moves
# it into the container and back, which the in-place tools never do.
LARGE_WORKSPACE_FILE_BYTES = 1_000_000
_LARGE_FILE_HINT = (
    " The call mounted large files ({files}): to change one in place use sandbox_edit_file, "
    "to find lines sandbox_grep, to read a window sandbox_read_file; none of them moves the file."
)


def _large_mounted_files(mount: Any) -> list[str]:
    """The mounted workspace files of at least ``LARGE_WORKSPACE_FILE_BYTES``, largest first (at most 3)."""
    entries = getattr(mount, "entries", None) or {}
    large = [
        (getattr(entry, "size", 0) or 0, path)
        for path, entry in entries.items()
        if (getattr(entry, "size", 0) or 0) >= LARGE_WORKSPACE_FILE_BYTES
    ]
    large.sort(key=lambda item: (-item[0], item[1]))
    return [f"workspace/{path} {size / 1_000_000:.1f} MB" for size, path in large[:3]]


def _large_file_hint(mount: Any) -> str:
    files = _large_mounted_files(mount)
    return _LARGE_FILE_HINT.format(files=", ".join(files)) if files else ""


def _stage_margin(timeout: float) -> int:
    """Seconds kept back from *timeout*: a tenth of a short one, at most the cap, at least 1."""
    return min(_STAGE_MARGIN_MAX_SECONDS, max(1, int(timeout) // 10))


def _stage_budget(timeout: float, started: float, *, cap: float | None = None) -> float:
    """Seconds the next stage may take: what is left of *timeout* since *started*, less the margin.

    At least 1 s, so a stage is always tried; at most *cap* when given.
    """
    left = timeout - (time.monotonic() - started) - _stage_margin(timeout)
    if cap is not None:
        left = min(left, cap)
    return max(1.0, left)


def _stage_timeout_result(
    stage: str, timeout: float, started: float, stages: dict[str, float], *, extra: str = ""
) -> dict:
    """The error result of a stage that did not end: the stage, the seconds each one took, what to do.

    *extra* is appended to the stage's hint (the large mounted files and the tools that edit them in place).
    """
    spent = ", ".join(f"{name} {seconds:.1f} s" for name, seconds in stages.items())
    elapsed = time.monotonic() - started
    error = (
        f"Execution stopped at stage {stage}, {elapsed:.0f} s into the call (timeout {int(timeout)} s"
        f"{'; ' + spent if spent else ''}): {_STAGE_HINTS[stage]}{extra}"
    )
    logger.warning("Code interpreter stage %s did not end: %s", stage, error)
    return {
        "stdout": "",
        "stderr": "",
        "error": error,
        # The platform, not the code, failed: the tool answers an error result.
        "platform_failure": stage == "create",
        "images": [],
        "output_files": [],
        "mounted": [],
    }


# Maximum number of chart images captured per execution
MAX_IMAGES = 5

# Files written to the sandbox working directory by user code (e.g.
# ``df.to_csv("report.csv")``, ``plt.savefig("plot.png")``,
# ``wb.save("data.xlsx")``) are captured after execution and persisted as
# conversation / assignment-run files so they survive the temp-dir cleanup.
# Bounded so a runaway script cannot exhaust memory.
MAX_OUTPUT_FILES = 10
MAX_OUTPUT_FILE_BYTES = 15 * 1024 * 1024  # 15 MB per file

# Conversation files are MOUNTED into the sandbox working directory:
# ``attachments/<name>``, plus ``attachments/index.json`` mapping file ids to
# paths. A model asked to compute over an attached 558k HTML model tried
# ``open('Filigran_Americas_Coverage_Model TD.html')`` in a sandbox that held
# nothing, and retried with other paths - every retry an iteration. The copy
# is disposable (it goes with the run) and never written back. Mounted only
# when the code names ``attachments`` (a run that does not read them costs no
# download), and bounded in count and bytes.
ATTACHMENTS_DIRNAME = "attachments"
MAX_INPUT_FILES = 50
MAX_INPUT_TOTAL_BYTES = 64 * 1024 * 1024  # 64 MB across all mounted files
#: A mounted name fits every filesystem's name limit (255 bytes of UTF-8).
_MOUNT_NAME_MAX_BYTES = 255
_MOUNT_NAME_BAD_CHARS = re.compile(r"[\\/:*?\"<>|\x00-\x1f\x7f]+")
_MOUNT_EXTENSION_RE = re.compile(r"[A-Za-z0-9_-]{1,16}")
#: Names Windows refuses as the part before the first dot (the local sandbox
#: runs wherever the backend does). ``ntpath.isreserved`` - the stdlib's own
#: list, ``CONIN$`` and the superscript ``COM\u00b9`` included - is asked too.
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)
#: Unicode categories a name drops: controls (NEL included), format marks
#: (a right-to-left override turns ``gpj.exe`` into what reads as ``exe.jpg``)
#: and the line / paragraph separators - invisible or line-breaking in every
#: listing the model and a reviewer read.
_MOUNT_NAME_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
#: The code names the mount directory as a PATH (``attachments/x``,
#: ``"attachments"``): the one case worth a download. A comment or prose that
#: merely says "attachments" does not pay for one.
_ATTACHMENTS_REFERENCE_RE = re.compile(r"attachments[/\\'\"]")


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """*text* cut to at most *max_bytes* of UTF-8, never inside a character."""
    return text.encode("utf-8")[: max(0, max_bytes)].decode("utf-8", errors="ignore")


def mount_tag(file_id: str | None) -> str:
    """The file-id part of a mount name: the WHOLE id (a UUID's 32 hex digits).

    Distinct files carry distinct ids, so two tags - and two mount names -
    never collide and nothing ever has to be renamed at mount time: the path
    a footer or a refusal names for one file on its own is the path it lands
    at. A prefix of the id could collide, and the renamed mount would then
    disagree with every path already announced. The ``file-`` form of an id
    is the same file, so it is the same tag; an id that is not a UUID gets a
    digest of itself (lower-case hex, so names differing only by case - one
    name on a case-insensitive filesystem - cannot arise).
    """
    ident = (file_id or "").strip().removeprefix("file-")
    if not ident:
        return "file"
    try:
        return uuid.UUID(ident).hex
    except ValueError:
        return hashlib.blake2b(ident.encode("utf-8"), digest_size=10).hexdigest()


def safe_mount_name(filename: str | None, file_id: str) -> str:
    """The name a conversation file is mounted under: flat, safe and unique BY CONSTRUCTION.

    ``<stem>-<file id>.<ext>`` (:func:`mount_tag`): the file-id tag makes two
    uploads named ``data.csv`` two different names, whatever the other files
    of the run (so a path can be named for one file on its own, in a footer
    or a refusal, and still be exact), and no two names differ only by case.
    The stem is NFC-normalised, stripped of path separators, control
    characters and the characters a filesystem refuses, the whole name capped
    at 255 bytes of UTF-8 (a CJK name is cut by bytes, not characters), a
    Windows reserved device name defused, and the extension kept last so
    ``pd.read_csv`` and friends still guess right.
    """
    base = os.path.basename((filename or "").replace("\\", "/").strip())
    base = unicodedata.normalize("NFC", base)
    # Non-ASCII only: an ASCII control is a separator the substitution below
    # turns into ``_``, which keeps the words of ``line\nbreak`` apart.
    base = "".join(
        ch for ch in base if ch.isascii() or unicodedata.category(ch) not in _MOUNT_NAME_DROPPED_CATEGORIES
    )
    base = _MOUNT_NAME_BAD_CHARS.sub("_", base).strip(" .")
    stem, dot, ext = base.rpartition(".")
    if not dot or not stem or not _MOUNT_EXTENSION_RE.fullmatch(ext):
        stem, ext = base, ""
    suffix = f"-{mount_tag(file_id)}" + (f".{ext.lower()}" if ext else "")
    room = _MOUNT_NAME_MAX_BYTES - len(suffix.encode("utf-8"))
    stem = stem or "file"
    if stem.split(".", 1)[0].strip().lower() in _WINDOWS_RESERVED_NAMES:
        stem = "_" + stem
    name = (_truncate_utf8(stem, room).rstrip(" .") or "file") + suffix
    if ntpath.isreserved(name):
        # Defused at the stem, so the tag and the extension always survive the cap.
        name = (_truncate_utf8("_" + stem, room).rstrip(" .") or "file") + suffix
    return name


def mount_path_hint(filename: str | None, file_id: str) -> str:
    """The path a conversation file is mounted at (relative to the working directory)."""
    return f"{ATTACHMENTS_DIRNAME}/{safe_mount_name(filename, file_id)}"


def code_interpreter_offered(tools: Iterable[ToolDefinition] | None) -> bool:
    """Whether the built-in code interpreter is on *tools* - an offered surface.

    The one test every text that names ``code_interpreter`` or its mount path
    goes through: ``offered_tools()`` inside a loop, the definitions an entry
    point is about to offer before one. The source is checked as well as the
    name, because the mount exists only for the built-in: a same-named MCP or
    custom tool replaces it on the surface and mounts nothing.
    """
    return any(
        getattr(tool, "name", None) == "code_interpreter" and getattr(tool, "source", None) == "builtin:code_interpreter"
        for tool in tools or ()
    )


#: The files a run mounts: ``(mount name, bytes)`` pairs, or a loader
#: returning them - awaited only once the run has passed every gate (the
#: sandbox reachable, the quota open, the sandbox created), so a refused run
#: downloads nothing.
SandboxInputs = list[tuple[str, bytes]]
SandboxInputsLoader = Callable[[], Awaitable[SandboxInputs]]


async def _load_inputs_safely(input_files: SandboxInputs | SandboxInputsLoader | None) -> SandboxInputs:
    """The mount's files: the list itself, or what the loader returns - awaited
    here, past the gates. A loader that fails mounts nothing (the result's
    ``mounted`` says so); it never fails the run."""
    if input_files is None:
        return []
    if not callable(input_files):
        return list(input_files)
    try:
        return list(await input_files() or [])
    except Exception:
        logger.warning("Could not load the conversation files for the sandbox mount", exc_info=True)
        return []


def code_reads_attachments(code: str) -> bool:
    """Whether *code* names the mount directory as a path - the one case worth a download."""
    return _ATTACHMENTS_REFERENCE_RE.search(code or "") is not None

# ---------------------------------------------------------------------------
# Environment sanitization
#
# Only these env var prefixes/names are forwarded to the sandbox subprocess.
# Everything else (DATABASE_URL, SECRET_KEY, ADMIN_PASSWORD, REDIS_URL,
# S3_ACCESS_KEY, S3_SECRET_KEY, etc.) is stripped.
# ---------------------------------------------------------------------------

_SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "MPLBACKEND",
        "COPILOT_MAX_IMAGES",
        "SYSTEMROOT",  # required on Windows
        "COMSPEC",  # required on Windows
    }
)

_SAFE_ENV_PREFIXES = ("PYTHON",)


def _build_sandbox_env() -> dict[str, str]:
    """Build a minimal, safe environment for sandbox subprocesses.

    Deliberately does NOT propagate the S3/MinIO TLS bypass
    (``S3_REJECT_UNAUTHORIZED=false``) as ``NODE_TLS_REJECT_UNAUTHORIZED=0``:
    that would disable certificate verification for EVERY outbound HTTPS call
    made by custom JS tools and stdio MCP servers (MITM exposure), and Node
    prepends a ``(node:<pid>) Warning:`` line to stderr that corrupts the
    structured JSON error contract of custom tools. Sandboxed tools read
    conversation files through the pre-resolved ``$files`` entries
    (``content`` / ``data_base64``), so they do not need to fetch presigned
    S3 URLs; stdio MCP servers that genuinely need the bypass can set the
    variable explicitly in their per-server env configuration.
    """
    safe = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _SAFE_ENV_KEYS or any(upper.startswith(p) for p in _SAFE_ENV_PREFIXES):
            safe[key] = value
    safe["COPILOT_MAX_IMAGES"] = str(MAX_IMAGES)
    safe["MPLBACKEND"] = "Agg"
    return safe


# ---------------------------------------------------------------------------
# Sandbox wrapper script
#
# This script is written to a temp file and executed in the subprocess.
# It patches matplotlib to capture figures, executes the user code via
# ``exec()``, and prints a JSON result on stdout.
# ---------------------------------------------------------------------------

_SANDBOX_WRAPPER_PREFIX = """\
import sys, os, io, json, base64, traceback, ast, builtins

# Force non-interactive matplotlib backend
os.environ["MPLBACKEND"] = "Agg"

# An import of a module of the workspace (a checkout) writes no __pycache__
# beside it: a bytecode cache is never a change of the checkout (#9518).
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

# ---- Scrub environment variables -------------------------------------------
# Even though the parent only passes safe env vars, belt-and-suspenders:
# keep only what the Python runtime needs, drop everything else.
_KEEP_ENV = {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
             "TMPDIR", "TMP", "TEMP", "MPLBACKEND", "COPILOT_MAX_IMAGES",
             "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED",
             "SYSTEMROOT", "COMSPEC", "WORKSPACE_DIR",
             # The native thread-pool caps (_sandbox_selector.NATIVE_THREAD_ENV_KEYS)
             "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"}
for _k in list(os.environ):
    if _k.upper() not in _KEEP_ENV:
        del os.environ[_k]

"""

# Everything after the security-setup block. The blocklist + import hook is
# spliced in between the prefix and this suffix at call time so the per-deploy
# ``code_interpreter_extra_allowed_modules`` allow-list can be applied.
_SANDBOX_WRAPPER_SUFFIX = """\

# Block dangerous os functions that allow command execution
for _fn in ("system", "popen", "exec", "execl", "execle", "execlp",
            "execlpe", "execv", "execve", "execvp", "execvpe",
            "spawnl", "spawnle", "spawnlp", "spawnlpe",
            "spawnv", "spawnve", "spawnvp", "spawnvpe"):
    if hasattr(os, _fn):
        delattr(os, _fn)

MAX_IMAGES = int(os.environ.get("COPILOT_MAX_IMAGES", "5"))

# ---- The sandbox workspace's installed packages --------------------
# Appended, never prepended: a package cannot shadow the standard library or
# what the sandbox ships. Imports still go through the module blocklist.
# Checked before every cell: a kernel outlives the install that adds them.
def _add_workspace_site():
    _ws_dir = os.environ.get("WORKSPACE_DIR", "")
    if _ws_dir:
        _ws_site = os.path.join(_ws_dir, ".venv", "site-packages")
        if os.path.isdir(_ws_site) and _ws_site not in sys.path:
            sys.path.append(_ws_site)

_images: list[str] = []

# What importing matplotlib prints (a font cache notice) is the first cell's,
# as when the capture started before it: never on the result's own stdout.
_boot_out, _boot_err = io.StringIO(), io.StringIO()
_real_out, _real_err = sys.stdout, sys.stderr
sys.stdout, sys.stderr = _boot_out, _boot_err

# ---- Patch matplotlib.pyplot.show() to capture figures ---------------------
_plt_available = False
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as _plt
    _plt_available = True

    def _capture_show(*_a, **_kw):
        for _n in _plt.get_fignums():
            if len(_images) >= MAX_IMAGES:
                break
            _buf = io.BytesIO()
            _plt.figure(_n).savefig(
                _buf, format="png", dpi=100,
                bbox_inches="tight", facecolor="white", edgecolor="none",
            )
            _buf.seek(0)
            _images.append(base64.b64encode(_buf.read()).decode("ascii"))
        _plt.close("all")

    _plt.show = _capture_show
except Exception:
    # Figure capture is best-effort: a matplotlib that cannot start here
    # (missing, or no home directory for its config) never stops the code.
    _plt_available = False
finally:
    sys.stdout, sys.stderr = _real_out, _real_err


def _run_cell(_user_code, _ns):
    # One cell: stdout / stderr captured, the last expression displayed
    # (Jupyter-style), the figures it left captured. ``_ns`` is a fresh
    # namespace for a stateless run, the kernel's own for session=true.
    global _boot_out, _boot_err
    _add_workspace_site()
    _images.clear()
    _stdout_buf = io.StringIO(_boot_out.getvalue())
    _stderr_buf = io.StringIO(_boot_err.getvalue())
    _stdout_buf.seek(0, 2)
    _stderr_buf.seek(0, 2)
    _boot_out, _boot_err = io.StringIO(), io.StringIO()
    _orig_stdout, _orig_stderr = sys.stdout, sys.stderr
    sys.stdout = _stdout_buf
    sys.stderr = _stderr_buf
    _error = None
    try:
        _tree = ast.parse(_user_code)

        # Auto-display the last expression (Jupyter-style)
        if _tree.body and isinstance(_tree.body[-1], ast.Expr):
            _last_expr = _tree.body.pop()
            _mod = ast.Module(body=_tree.body, type_ignores=[])
            _compiled_mod = compile(_mod, "<code>", "exec")

            _expr_node = ast.Expression(body=_last_expr.value)
            _compiled_expr = compile(_expr_node, "<code>", "eval")

            exec(_compiled_mod, _ns)
            _result_val = eval(_compiled_expr, _ns)

            if _result_val is not None:
                # Pretty-print DataFrames as markdown tables
                try:
                    import pandas as _pd
                    if isinstance(_result_val, _pd.DataFrame):
                        try:
                            print(_result_val.to_markdown(index=False))
                        except ImportError:
                            print(_result_val.to_string(index=False))
                    elif isinstance(_result_val, _pd.Series):
                        try:
                            print(_result_val.to_markdown())
                        except ImportError:
                            print(_result_val.to_string())
                    else:
                        print(repr(_result_val))
                except ImportError:
                    print(repr(_result_val))
        else:
            exec(compile(_user_code, "<code>", "exec"), _ns)

    except SystemExit as _exit:
        # ``sys.exit()`` ends the cell, not the interpreter: what it printed
        # is kept, and a non-zero code is reported as the cell's error.
        if _exit.code not in (None, 0):
            _error = "SystemExit: " + str(_exit.code)
    except Exception:
        _error = traceback.format_exc()

    # ---- Capture remaining matplotlib figures (not explicitly shown) -------
    if _plt_available:
        try:
            for _n in _plt.get_fignums():
                if len(_images) >= MAX_IMAGES:
                    break
                _buf = io.BytesIO()
                _plt.figure(_n).savefig(
                    _buf, format="png", dpi=100,
                    bbox_inches="tight", facecolor="white", edgecolor="none",
                )
                _buf.seek(0)
                _images.append(base64.b64encode(_buf.read()).decode("ascii"))
            _plt.close("all")
        except Exception:
            pass  # Matplotlib figure capture is best-effort — ignore cleanup failures

    sys.stdout = _orig_stdout
    sys.stderr = _orig_stderr
    return {
        "stdout": _stdout_buf.getvalue(),
        "stderr": _stderr_buf.getvalue(),
        "error": _error,
        "images": _images[:MAX_IMAGES],
    }
"""

# A stateless run: the code file named on the command line, one cell, the
# structured result on stdout.
_STATELESS_TRAILER = """\

_code_path = sys.argv[1]
with open(_code_path, "r", encoding="utf-8") as _f:
    _user_code = _f.read()
print(json.dumps(_run_cell(_user_code, {"__builtins__": __builtins__, "__name__": "__main__"})))
"""

# A session=true kernel: cells run in ONE namespace, one at a time.
# On the local sandbox they arrive one JSON line each on stdin and each result
# is one line on stdout, framed with the per-kernel token so text the code
# writes to the raw descriptor is not mistaken for it; the kernel exits after
# its idle timeout, or when its stdin closes (the server process went). In a
# container (a request directory as third argument) a cell is a
# ``<id>.json`` file announced by ``<id>.ready``, its result ``<id>.res``
# (renamed into place, never read half-written), and the files the cell
# wrote in the working directory are listed with it. The working directory is
# reset before every cell, so a ``chdir`` never moves the next cell's paths.
_KERNEL_TRAILER = """\
import queue, threading, time as _kernel_time

_kernel_token = sys.argv[1]
_kernel_idle = float(sys.argv[2])
_kernel_dir = sys.argv[3] if len(sys.argv) > 3 else ""
_kernel_home = os.getcwd()
_kernel_skip = {"workspace", "attachments", ".xtm"}
_kernel_ns = {"__builtins__": __builtins__, "__name__": "__main__"}
_kernel_in = sys.stdin
sys.stdin = io.StringIO("")


def _kernel_files():
    _found = {}
    for _root, _dirs, _names in os.walk(_kernel_home):
        if _root == _kernel_home:
            _dirs[:] = [_d for _d in _dirs if _d not in _kernel_skip]
        for _name in _names:
            _path = os.path.join(_root, _name)
            try:
                _st = os.stat(_path)
            except OSError:
                continue
            _found[os.path.relpath(_path, _kernel_home).replace(os.sep, "/")] = (_st.st_size, _st.st_mtime_ns)
    return _found


def _kernel_cell(_request):
    try:
        os.chdir(_kernel_home)
    except OSError:
        pass
    _before = _kernel_files() if _kernel_dir else None
    _result = _run_cell(_request.get("code", ""), _kernel_ns)
    _result["id"] = _request.get("id")
    if _before is not None:
        _result["changed"] = sorted(
            _rel for _rel, _state in _kernel_files().items()
            if _before.get(_rel) != _state and not _rel.startswith(os.path.basename(sys.argv[0]))
        )
    return _result


if _kernel_dir:
    os.makedirs(_kernel_dir, exist_ok=True)
    with open(os.path.join(_kernel_dir, "pid"), "w") as _f:
        _f.write(str(os.getpid()))
    _kernel_last = _kernel_time.time()
    while _kernel_time.time() - _kernel_last <= _kernel_idle:
        _ready = sorted(_n for _n in os.listdir(_kernel_dir) if _n.endswith(".ready"))
        if not _ready:
            _kernel_time.sleep(0.05)
            continue
        _rid = _ready[0][: -len(".ready")]
        _base = os.path.join(_kernel_dir, _rid)
        try:
            with open(_base + ".json", encoding="utf-8") as _f:
                _kernel_request = json.load(_f)
        except (OSError, ValueError):
            _kernel_request = None
        for _suffix in (".ready", ".json"):
            try:
                os.remove(_base + _suffix)
            except OSError:
                pass
        if _kernel_request is None:
            continue
        _kernel_result = _kernel_cell(_kernel_request)
        with open(_base + ".res.part", "w", encoding="utf-8") as _f:
            json.dump(_kernel_result, _f)
        os.replace(_base + ".res.part", _base + ".res")
        _kernel_last = _kernel_time.time()
    try:
        os.remove(os.path.join(_kernel_dir, "pid"))
    except OSError:
        pass
else:
    _kernel_out = io.open(os.dup(1), "w", encoding="utf-8", closefd=True)
    _kernel_requests = queue.Queue()

    def _kernel_reader():
        for _line in _kernel_in:
            _kernel_requests.put(_line)
        _kernel_requests.put(None)

    threading.Thread(target=_kernel_reader, daemon=True).start()
    while True:
        try:
            _kernel_line = _kernel_requests.get(timeout=_kernel_idle)
        except queue.Empty:
            break
        if _kernel_line is None:
            break
        try:
            _kernel_request = json.loads(_kernel_line)
        except ValueError:
            continue
        _kernel_result = _kernel_cell(_kernel_request)
        _kernel_out.write("\\x1eXTMK:" + _kernel_token + ":" + json.dumps(_kernel_result) + "\\n")
        _kernel_out.flush()
"""


# Tracks which allow-lists have already been warned about so the security
# warning is logged once per distinct configuration, not on every execution.
_warned_allow_lists: set[frozenset[str]] = set()


def _resolve_allowed_modules() -> frozenset[str]:
    """Return the admin-configured set of modules to re-enable in the sandbox.

    Reads the ``code_interpreter_extra_allowed_modules`` security option
    (Settings > Access and security, or ``CODE_INTERPRETER_EXTRA_ALLOWED_MODULES``, which
    wins). Logs a one-time WARNING per distinct non-empty configuration
    because re-enabling blocked modules (notably ``socket`` / the HTTP stack)
    restores network access to sandboxed code.
    """
    from app.core.security_options import security_option

    allow = security_option("code_interpreter_extra_allowed_modules")
    if allow and allow not in _warned_allow_lists:
        _warned_allow_lists.add(allow)
        logger.warning(
            "Code interpreter sandbox: re-enabling normally-blocked module(s) %s "
            "(CODE_INTERPRETER_EXTRA_ALLOWED_MODULES or Settings > Access and security). This grants sandboxed code "
            "access to those modules (e.g. 'socket' restores raw network access). "
            "Only do this when the backend runs with appropriate network isolation.",
            ", ".join(sorted(allow)),
        )
    return allow


# The HTTP stack the OpenSandbox code interpreter gets back with
# CODE_INTERPRETER_OPEN_EGRESS: its container's egress policy, not the import
# hook, is then the network boundary.
OPEN_EGRESS_MODULES: frozenset[str] = frozenset({"socket", "http", "urllib", "requests", "httpx", "aiohttp"})


def _opensandbox_allowed_modules() -> frozenset[str]:
    """The modules re-enabled in the OpenSandbox code interpreter: the admin's, plus the HTTP stack with open egress.

    Never used by the local code interpreter, which shares the backend's network.
    """
    from app.core.config import settings

    allow = _resolve_allowed_modules()
    return allow | OPEN_EGRESS_MODULES if settings.code_interpreter_open_egress else allow


def _build_sandbox_wrapper(allow: frozenset[str], *, kernel: bool = False) -> str:
    """Assemble the full sandbox wrapper script, applying *allow* to the blocklist.

    *kernel* builds the ``session=true`` kernel instead of the one-shot run:
    the same security setup and the same cell runner, a loop around it.
    """
    return (
        _SANDBOX_WRAPPER_PREFIX
        + _build_sandbox_security_src("builtins", "code interpreter sandbox", allow=allow)
        + _SANDBOX_WRAPPER_SUFFIX
        + (_KERNEL_TRAILER if kernel else _STATELESS_TRAILER)
    )


# ---------------------------------------------------------------------------
# Output-file capture
# ---------------------------------------------------------------------------


def _collect_output_files(
    tmp_dir: str,
    exclude: set[str],
    *,
    exclude_dirs: Iterable[str] = (),
    since: dict[str, tuple[int, int]] | None = None,
) -> list[dict]:
    """Collect files user code wrote into the sandbox working directory.

    Returns a list of ``{filename, data_base64, content_type, size}`` entries
    (base64-encoded so they survive the temp-dir cleanup in
    :func:`execute_python`'s ``finally`` block). The scaffolding files
    (``user_code.py`` / ``_sandbox_wrapper.py``) and the mounted attachments
    (every path in *exclude*) are excluded. Bounded by
    :data:`MAX_OUTPUT_FILES` and :data:`MAX_OUTPUT_FILE_BYTES` so a runaway
    script cannot exhaust memory. Best-effort: any per-file OS error is
    skipped rather than failing the whole execution.

    *exclude_dirs* are not walked at all: the sandbox workspace
    (``workspace/``) is the code's durable directory, not its output.
    *since* - ``(size, mtime_ns)`` per path - keeps a file it lists unchanged
    out: a kernel's directory outlives the call, and only what this call
    wrote is its output.
    """
    import base64 as _b64
    import mimetypes

    collected: list[dict] = []
    skip_dirs = {os.path.normcase(os.path.abspath(path)) for path in exclude_dirs or ()}
    try:
        for root, dirs, files in os.walk(tmp_dir):
            dirs[:] = [
                d for d in dirs if os.path.normcase(os.path.abspath(os.path.join(root, d))) not in skip_dirs
            ]
            for name in sorted(files):
                if len(collected) >= MAX_OUTPUT_FILES:
                    return collected
                full = os.path.join(root, name)
                if full in exclude:
                    continue
                if since is not None:
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    if since.get(full) == (st.st_size, st.st_mtime_ns):
                        continue
                try:
                    size = os.path.getsize(full)
                    if size <= 0 or size > MAX_OUTPUT_FILE_BYTES:
                        continue
                    with open(full, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                rel = os.path.relpath(full, tmp_dir).replace(os.sep, "/")
                ctype, _ = mimetypes.guess_type(rel)
                collected.append(
                    {
                        "filename": rel,
                        "data_base64": _b64.b64encode(data).decode("ascii"),
                        "content_type": ctype,
                        "size": len(data),
                    }
                )
    except OSError:
        return collected
    return collected


def _write_input_files(base_dir: str, input_files: list[tuple[str, bytes]] | None) -> tuple[set[str], list[str]]:
    """Write the mounted attachments under ``base_dir/attachments``.

    Returns ``(paths written, mount names written)``. The names come from
    :func:`safe_mount_name` (flat and safe); each is re-checked to stay a
    plain file name inside the directory. A per-file OS error is logged and
    skipped - a mount is a convenience, never the reason an execution fails -
    and the caller announces only the names that DID land (``mounted``).
    """
    written: set[str] = set()
    names: list[str] = []
    if not input_files:
        return written, names
    attach_dir = os.path.join(base_dir, ATTACHMENTS_DIRNAME)
    try:
        # A directory - or a link - already there was not made by this run
        # (``base_dir`` is fresh from ``mkdtemp``): refuse it rather than
        # write the conversation's files through it.
        os.mkdir(attach_dir, 0o700)
    except OSError:
        logger.warning("Could not create the sandbox attachments directory", exc_info=True)
        return written, names
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    for name, data in input_files:
        if not name or os.path.basename(name) != name or name in {".", ".."}:
            continue
        path = os.path.join(attach_dir, name)
        try:
            # O_EXCL: never through a file or a link planted under the name.
            fd = os.open(path, flags, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except OSError:
            logger.warning("Could not mount %s into the sandbox", name, exc_info=True)
            continue
        written.add(path)
        names.append(name)
    return written, names


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def execute_python(
    code: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    user_id: uuid.UUID | None = None,
    quota_user_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
    input_files: SandboxInputs | SandboxInputsLoader | None = None,
    workspace: WorkspaceSession | None = None,
    session: bool = False,
    reset: bool = False,
) -> dict:
    """Execute Python code in a sandboxed subprocess.

    Returns a dict with:
        stdout:        str        – captured standard output
        stderr:        str        – captured standard error (warnings, etc.)
        error:         str | None – traceback string if execution failed
        images:        list[str]  – base64-encoded PNG images (from matplotlib)
        output_files:  list[dict] – files the code wrote to disk, each
            ``{filename, data_base64, content_type, size}`` (see
            :func:`_collect_output_files`)
        mounted:       list[str]  – the ``attachments/`` names actually written
        workspace:     WorkspaceCall – only with *workspace*: the kernel mode,
            the mount, the bytes moved and the manifest diff

    *input_files* - ``(mount name, bytes)`` pairs, or a loader returning them
    (:data:`SandboxInputsLoader`, awaited past every gate) - are written under
    ``attachments/`` in the working directory before the code runs
    (:data:`ATTACHMENTS_DIRNAME`) and are never collected as outputs.
    Every result says which names landed (``mounted``, empty when none did).

    *workspace* is the turn's sandbox workspace: mounted under
    ``workspace/`` when the code names it (or it holds installed packages),
    and what the code changed there is saved once it ran. *session* runs the
    code in the conversation's long-lived kernel instead of a fresh process
    (``sandbox_workspace/kernel.py``); *reset* starts that kernel anew.

    *user_id*/*quota_user_id*/*db*, when provided, are forwarded to
    :func:`execute_python_opensandbox` for the combined quota check and
    per-user sandbox usage recording (spec FR-011 for *db*'s
    period-resolution role). Only meaningful on the ``opensandbox`` branch
    below — the local subprocess sandbox is out of scope for usage tracking
    (spec 002-sandbox-quota-usage-tracking Assumptions), so all three are
    silently unused on the other branch.
    """
    from app.core.config import settings
    from app.core.security_options import security_option

    if security_option("disable_code_interpreter"):
        raise RuntimeError(
            "Code interpreter is disabled by the platform administrator. "
            "This function should not have been called — the tool should "
            "not be registered when disabled."
        )

    if (getattr(settings, "custom_tool_sandbox", "auto") or "auto") == "opensandbox":
        # Only the keyword arguments in use: the delegate's signature is what
        # a test double or an older deployment of the module offers.
        extra: dict = {"input_files": input_files} if input_files else {}
        if workspace is not None:
            extra["workspace"] = workspace
        if session:
            extra.update(session=True, reset=reset)
        return await execute_python_opensandbox(
            code, timeout=timeout, user_id=user_id, quota_user_id=quota_user_id, db=db, **extra
        )

    if session:
        from app.services.sandbox_workspace.kernel import execute_in_local_kernel

        return await execute_in_local_kernel(
            code, timeout=timeout, workspace=workspace, input_files=input_files, reset=reset
        )

    import shutil

    from app.services.sandbox_workspace.paths import WORKSPACE_DIRNAME
    from app.services.sandbox_workspace.run import finish_local, mount_local

    tmp_dir = tempfile.mkdtemp(prefix="copilot_code_")
    try:
        mount = None
        if workspace is not None:
            mount = await mount_local(workspace, code, os.path.join(tmp_dir, WORKSPACE_DIRNAME))
        result = await _run_local(
            code,
            tmp_dir,
            timeout=timeout,
            input_files=input_files,
            workspace_dir=mount.workspace_dir if mount is not None and mount.snapshot is not None else None,
        )
        if mount is not None:
            result["workspace"] = await finish_local(mount)
        return result
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _run_local(
    code: str,
    tmp_dir: str,
    *,
    timeout: int,
    input_files: SandboxInputs | SandboxInputsLoader | None,
    workspace_dir: str | None,
) -> dict:
    """One stateless run in *tmp_dir*, the working directory :func:`execute_python` owns and removes."""
    code_file = os.path.join(tmp_dir, "user_code.py")
    wrapper_file = os.path.join(tmp_dir, "_sandbox_wrapper.py")

    try:
        with open(code_file, "w", encoding="utf-8") as f:
            f.write(code)
        with open(wrapper_file, "w", encoding="utf-8") as f:
            f.write(_build_sandbox_wrapper(_resolve_allowed_modules()))
        mounted, mounted_names = _write_input_files(tmp_dir, await _load_inputs_safely(input_files))

        env = _build_sandbox_env()
        # No __pycache__ written beside an imported workspace module (#9518).
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if workspace_dir is not None:
            env["WORKSPACE_DIR"] = os.path.abspath(workspace_dir)

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            wrapper_file,
            code_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmp_dir,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            logger.warning("Code interpreter timed out after %ds", timeout)
            return {
                "stdout": "",
                "stderr": "",
                "error": f"Execution timed out after {timeout} seconds. "
                "Try simplifying the code or reducing the dataset size.",
                "images": [],
                "output_files": [],
                "mounted": mounted_names,
            }

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        # Capture any files the user code wrote to the working directory
        # BEFORE the ``finally`` block deletes ``tmp_dir``. The mounted
        # attachments are inputs, not outputs; the workspace is kept on its own.
        output_files = _collect_output_files(
            tmp_dir,
            exclude={code_file, wrapper_file, *mounted},
            exclude_dirs=[workspace_dir] if workspace_dir else (),
        )

        if process.returncode != 0 and not stdout_text:
            return {
                "stdout": "",
                "stderr": stderr_text,
                "error": stderr_text or f"Process exited with code {process.returncode}",
                "images": [],
                "output_files": output_files,
                "mounted": mounted_names,
            }

        # Parse the structured JSON result from the wrapper
        try:
            result = json.loads(stdout_text)
            if not isinstance(result, dict):
                raise ValueError("Unexpected output format")
            result["output_files"] = output_files
            result["mounted"] = mounted_names
            return result
        except (json.JSONDecodeError, ValueError):
            # Fallback: wrapper failed but code may have produced raw output
            return {
                "stdout": stdout_text,
                "stderr": stderr_text,
                "error": None,
                "images": [],
                "output_files": output_files,
                "mounted": mounted_names,
            }

    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Python interpreter not found at {sys.executable}. Code interpreter requires Python 3.10+."
        ) from exc


async def execute_python_opensandbox(
    code: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    user_id: uuid.UUID | None = None,
    quota_user_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
    input_files: SandboxInputs | SandboxInputsLoader | None = None,
    workspace: WorkspaceSession | None = None,
    session: bool = False,
    reset: bool = False,
) -> dict:
    """:func:`_run_opensandbox`, with the turn's sandbox workspace.

    The workspace is brought into the container at ``/tmp/workspace`` (the
    working directory's ``workspace/``) when the code needs it, and what the
    code changed there is read back before the container goes
    (``sandbox_workspace/remote.py``). *session* runs the code in the
    workspace's kernel container instead (``kernel.execute_in_remote_kernel``).
    """
    if session:
        from app.services.sandbox_workspace.kernel import execute_in_remote_kernel

        return await execute_in_remote_kernel(
            code,
            timeout=timeout,
            workspace=workspace,
            input_files=input_files,
            reset=reset,
            user_id=user_id,
            quota_user_id=quota_user_id,
            db=db,
        )
    started = time.monotonic()
    mount = None
    if workspace is not None:
        from app.services.sandbox_workspace.run import prepare_remote

        try:
            mount = await asyncio.wait_for(prepare_remote(workspace, code), timeout=_stage_budget(timeout, started))
        except TimeoutError:
            return _stage_timeout_result("workspace", timeout, started, {"workspace": time.monotonic() - started})
    result = await _run_opensandbox(
        code,
        timeout=timeout,
        user_id=user_id,
        quota_user_id=quota_user_id,
        db=db,
        input_files=input_files,
        mount=mount,
        started=started,
    )
    if mount is not None:
        result["workspace"] = mount.finish()
    return result


async def _run_opensandbox(
    code: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    user_id: uuid.UUID | None = None,
    quota_user_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
    input_files: SandboxInputs | SandboxInputsLoader | None = None,
    mount: Any = None,
    started: float | None = None,
) -> dict:
    """Execute Python code via an OpenSandbox-managed container.

    The create stage ends within ``CREATE_STAGE_MAX_SECONDS`` and what is left
    of *timeout* (less a margin, :func:`_stage_budget`; *started* is the
    call's ``time.monotonic()`` start, now when omitted), and the run stage
    within what create and upload left: a stage that does not end answers an
    error naming it with the seconds each stage took, and its container is
    killed, before the agent loop's own per-tool timeout. Without it a
    container that never started looked like code that never ended ("timed
    out after 300s" on a bare print).

    Uploads the sandbox wrapper and user code into the container, runs the
    wrapper (which captures matplotlib figures and outputs structured JSON),
    and parses the result.  Returns the same dict shape as
    :func:`execute_python`. *input_files* are written under
    ``/tmp/attachments/`` (the directory created first, through the SDK's
    ``create_directories``) and the wrapper runs with ``/tmp`` as its working
    directory - set through ``RunCommandOpts.working_directory``, never a
    shell ``cd`` - so ``attachments/<name>`` resolves the way it does in the
    local sandbox.

    *user_id*, when provided, is used to record one unit of per-user sandbox
    usage (``quota_usage`` table, ``usage_type="sandbox"``) once execution
    reaches the sandbox's own success path — i.e. the sandbox itself ran to
    completion, regardless of whether the wrapper's JSON reports a
    user-code-level error (spec 002-sandbox-quota-usage-tracking FR-002).
    ``None`` (the default) skips usage recording entirely, e.g. for callers
    that have no user context to attribute the call to.

    *quota_user_id*, when provided, is used instead of *user_id* for the
    combined agentic+sandbox quota check and usage recording — but not for
    file ownership or structured call logging, which stay on *user_id*.
    This lets a caller with a real user to charge quota against (e.g. a
    company-managed assignment run's charged/owning user) but no personal
    file owner (``user_id=None``, by design — see
    ``assignment_engine._resolve_run_file_owner_id``) still be metered,
    instead of silently dispatching unmetered. Defaults to *user_id* when
    not given.

    *db*, when provided alongside the quota-attribution id, lets that usage
    recording resolve a per-user/group/platform-configured period (spec
    FR-011) instead of always using the monthly default — see
    :func:`app.services.quota.increment_sandbox_usage`.
    """
    from app.core.config import settings
    from app.services._sandbox_selector import (
        _create_opensandbox,
        _probe_opensandbox_available,
    )
    from app.services.sandbox_call_logging import (
        kill_sandbox_best_effort,
        log_execution_errors,
        log_sandbox_call,
    )

    effective_quota_user_id = quota_user_id if quota_user_id is not None else user_id
    if started is None:
        started = time.monotonic()
    stages: dict[str, float] = {}

    if not await _probe_opensandbox_available():
        return {
            "stdout": "",
            "stderr": "",
            "error": (
                "Code interpreter sandbox is set to 'opensandbox' but the "
                "OpenSandbox server is not reachable at the configured "
                "OPENSANDBOX_DOMAIN. Check the server address and OPENSANDBOX_API_KEY."
            ),
            "images": [],
            "output_files": [],
            "mounted": [],
        }

    # Combined agentic+sandbox quota check (spec 002-sandbox-quota-usage-tracking
    # FR-015/FR-018/FR-019). Called unconditionally, even when
    # effective_quota_user_id is None (no attributable user) — the
    # platform-wide hard cap inside still applies in that case; only the
    # per-user limit is skipped.
    from app.services.quota import (
        QUOTA_EXCEEDED_MESSAGE,
        QUOTA_UNAVAILABLE_MESSAGE,
        QuotaCheckUnavailableError,
        QuotaExceededError,
        enforce_combined_sandbox_quota,
    )

    try:
        await enforce_combined_sandbox_quota(effective_quota_user_id)
    except QuotaExceededError:
        return {
            "stdout": "",
            "stderr": "",
            "error": QUOTA_EXCEEDED_MESSAGE,
            "images": [],
            "output_files": [],
            "mounted": [],
        }
    except QuotaCheckUnavailableError:
        return {
            "stdout": "",
            "stderr": "",
            "error": QUOTA_UNAVAILABLE_MESSAGE,
            "images": [],
            "output_files": [],
            "mounted": [],
        }

    wrapper_src = _build_sandbox_wrapper(_opensandbox_allowed_modules())
    _egress_allow_list = settings.opensandbox_network_allow_list

    create_started = time.monotonic()
    try:
        create = _create_opensandbox(
            image_key="python",
            timeout_seconds=timeout,
            kind_label="code-interpreter",
            usage_type="code_interpreter",
            user_id=user_id,
            egress_allow_list=_egress_allow_list,
            open_egress=settings.code_interpreter_open_egress,
            resource=settings.sandbox_resources("python"),
            env={
                "MPLBACKEND": "Agg",
                "COPILOT_MAX_IMAGES": str(MAX_IMAGES),
                "PYTHONDONTWRITEBYTECODE": "1",
                **(mount.env if mount is not None else {}),
            },
        )
        sandbox = await asyncio.wait_for(create, timeout=_stage_budget(timeout, started, cap=CREATE_STAGE_MAX_SECONDS))
    except TimeoutError:
        stages["create"] = time.monotonic() - create_started
        # The pods of the key on the server now (fail-open, bounded by its own
        # timeout inside the call's margin): an empty warm buffer (capacity)
        # reads apart from a slow server with warm pods (#9355).
        from app.services._sandbox_selector import pool_state_words, sandbox_pool_state

        try:
            state = await asyncio.wait_for(sandbox_pool_state("python"), timeout=_stage_margin(timeout))
        except TimeoutError:
            state = None
        pool = pool_state_words("python", state)
        timed_out = _stage_timeout_result("create", timeout, started, stages, extra=f" {pool}" if pool else "")
        # The cap cancels ``_create_opensandbox`` before its own failure path
        # (``_fail``) runs, so the friction of the run is recorded here.
        from app.services.frictions import note_sandbox_failure

        note_sandbox_failure(
            "code_interpreter", f"code-interpreter sandbox (image key python) not created: {timed_out['error']}"
        )
        return timed_out
    except Exception as exc:
        return {
            "stdout": "",
            "stderr": str(exc),
            "error": f"Failed to create sandbox: {exc}",
            "platform_failure": True,
            "images": [],
            "output_files": [],
            "mounted": [],
        }
    stages["create"] = time.monotonic() - create_started
    async with sandbox:
        _sandbox_remote_script_dir = "/tmp/"  # noqa: S108
        await sandbox.files.write_file(f"{_sandbox_remote_script_dir}user_code.py", code.encode("utf-8"))
        await sandbox.files.write_file(f"{_sandbox_remote_script_dir}_sandbox_wrapper.py", wrapper_src.encode("utf-8"))

        from opensandbox.models.filesystem import DirectoryListEntry

        # The names that DID land: the caller announces those, so a failed
        # write is never reported to the model as an available path.
        mounted_names: list[str] = []
        _attach_dir = f"{_sandbox_remote_script_dir}{ATTACHMENTS_DIRNAME}"
        # The download happens HERE, past the probe, the quota and the
        # sandbox creation: a refused run never pays for it.
        _inputs = await _load_inputs_safely(input_files)
        if _inputs:
            try:
                from opensandbox.models.filesystem import WriteEntry

                await sandbox.files.create_directories([WriteEntry(path=_attach_dir, mode=755)])
                _dir_ready = True
            except Exception:
                logger.warning("Could not create %s in the OpenSandbox container", _attach_dir, exc_info=True)
                _dir_ready = False
            for _mount_name, _mount_data in _inputs if _dir_ready else []:
                if not _mount_name or "/" in _mount_name or _mount_name in {".", ".."}:
                    continue
                try:
                    await sandbox.files.write_file(f"{_attach_dir}/{_mount_name}", _mount_data)
                except Exception:
                    logger.warning("Could not mount %s into the OpenSandbox container", _mount_name, exc_info=True)
                    continue
                mounted_names.append(_mount_name)
        _mounted_paths = {f"{_attach_dir}/{name}" for name in mounted_names}
        if mount is not None:
            # Bounded like every stage: a large mount (a checkout named
            # through a built path, a file of several MB) answers "stage
            # upload" with the files that made it large, never the agent
            # loop's bare per-tool timeout.
            upload_started = time.monotonic()
            try:
                await asyncio.wait_for(mount.push(sandbox), timeout=_stage_budget(timeout, started))
            except TimeoutError:
                stages["upload"] = time.monotonic() - upload_started
                await kill_sandbox_best_effort(sandbox)
                return _stage_timeout_result("upload", timeout, started, stages, extra=_large_file_hint(mount))

        # Snapshot /tmp before running so we can detect files created by user code
        _sandbox_scaffold = frozenset(
            {f"{_sandbox_remote_script_dir}user_code.py", f"{_sandbox_remote_script_dir}_sandbox_wrapper.py"}
        ) | _mounted_paths
        try:
            _before_entries = await sandbox.files.list_directory(DirectoryListEntry(path=_sandbox_remote_script_dir))
            _tmp_before = {e.path for e in _before_entries}
        except Exception:
            _tmp_before = set(_sandbox_scaffold)

        from opensandbox.models.execd import RunCommandOpts

        stages["upload"] = time.monotonic() - create_started - stages["create"]
        run_started = time.monotonic()
        command = f"python {_sandbox_remote_script_dir}_sandbox_wrapper.py {_sandbox_remote_script_dir}user_code.py"
        try:
            async with log_execution_errors(
                usage_type="code_interpreter", user_id=user_id, egress_allow_list=_egress_allow_list
            ):
                # The working directory comes from the SDK (``cwd`` of the run
                # request), so a relative ``attachments/<name>`` - and a relative
                # output path - means the directory the collection below lists.
                result = await asyncio.wait_for(
                    sandbox.commands.run(command, opts=RunCommandOpts(working_directory=_sandbox_remote_script_dir)),
                    timeout=_stage_budget(timeout, started),
                )
        except TimeoutError:
            stages["run"] = time.monotonic() - run_started
            await kill_sandbox_best_effort(sandbox)
            return _stage_timeout_result("run", timeout, started, stages, extra=_large_file_hint(mount))
        stdout_text = "\n".join(line.text for line in result.logs.stdout).strip()
        stderr_text = "\n".join(line.text for line in result.logs.stderr).strip()

        # Collect new files written to /tmp by user code and return them as base64
        import base64 as _b64
        import mimetypes

        output_files: list[dict] = []
        try:
            _after_entries = await sandbox.files.list_directory(DirectoryListEntry(path=_sandbox_remote_script_dir))
            for _entry in sorted(_after_entries, key=lambda e: e.path):
                if len(output_files) >= MAX_OUTPUT_FILES:
                    break
                if _entry.path in _tmp_before or _entry.path in _sandbox_scaffold:
                    continue
                if _entry.entry_type == "directory":
                    continue
                if _entry.size <= 0 or _entry.size > MAX_OUTPUT_FILE_BYTES:
                    continue
                if _entry.path.endswith(".stdout") or _entry.path.endswith(".stderr"):
                    continue  # Ignore stdout and stderr files
                try:
                    _data = await sandbox.files.read_bytes(_entry.path)
                    _rel = (
                        _entry.path[len(_sandbox_remote_script_dir) :]
                        if _entry.path.startswith(_sandbox_remote_script_dir)
                        else _entry.path.lstrip("/")
                    )
                    _ctype, _ = mimetypes.guess_type(_rel)
                    output_files.append(
                        {
                            "filename": _rel,
                            "data_base64": _b64.b64encode(_data).decode("ascii"),
                            "content_type": _ctype,
                            "size": len(_data),
                        }
                    )
                except Exception:
                    logger.debug("Failed to read file: %s", _entry.path, exc_info=True)
                    continue
        except Exception:
            # File collection is best-effort — ignore errors
            logger.debug("Failed to list %s directory in sandbox", _sandbox_remote_script_dir, exc_info=True)

        # Decided and logged exactly once here, before branching into any
        # of the return paths below — never at each return statement, which
        # would double-log a single call (spec 003-sandbox-call-logging
        # FR-001, /speckit-analyze finding A1). A non-zero exit code is
        # always a sandbox_call failure (spec FR-006), even in the case
        # right below where the process still produced stdout and is
        # therefore still treated as billable usage — usage-metering and
        # observability answer different questions here (/code-review
        # finding: nonzero exit with stdout was previously misreported as
        # outcome="success").
        sandbox_call_failed = result.exit_code != 0
        await log_sandbox_call(
            usage_type="code_interpreter",
            user_id=user_id,
            outcome="failure" if sandbox_call_failed else "success",
            failure_reason="nonzero_exit" if sandbox_call_failed else None,
            sandbox=sandbox,
            execution_complete=result.complete,
            egress_allow_list=_egress_allow_list,
        )
        # What the code changed under workspace/ is read back while the
        # container is alive.
        if mount is not None:
            await mount.pull(sandbox)
        # Free the container now rather than waiting out its TTL - after
        # logging (which still needed it alive for metrics), nothing below
        # touches the sandbox again.
        await kill_sandbox_best_effort(sandbox)

        if result.exit_code != 0 and not stdout_text:
            return {
                "stdout": "",
                "stderr": stderr_text,
                "error": stderr_text or f"Sandbox exited with code {result.exit_code}",
                "images": [],
                "output_files": output_files,
                "mounted": mounted_names,
            }

        # The sandbox itself ran to completion at this point (both return
        # paths below are "successful" for usage-metering purposes, even if
        # the wrapper's own JSON later reports a user-code-level error, or
        # even if the process exited non-zero while still producing stdout)
        # — spec 002-sandbox-quota-usage-tracking FR-002. Called
        # unconditionally — increment_sandbox_usage itself skips the
        # per-user counter when effective_quota_user_id is None, but still
        # writes the global counter the platform-wide cap reads.
        from app.services.quota import increment_sandbox_usage

        await increment_sandbox_usage(effective_quota_user_id, db=db)

        try:
            data = json.loads(stdout_text)
            if not isinstance(data, dict):
                raise ValueError("Unexpected output format")
            data.setdefault("output_files", output_files)
            data["mounted"] = mounted_names
            return data
        except (json.JSONDecodeError, ValueError):
            return {
                "stdout": stdout_text,
                "stderr": stderr_text,
                "error": None,
                "images": [],
                "output_files": output_files,
                "mounted": mounted_names,
            }


# ---------------------------------------------------------------------------
# Output sanitization (defense-in-depth)
#
# Even if the primary guards (env allow-list + in-sandbox scrub) somehow
# fail, these patterns redact connection strings and credential-shaped
# values before the output is returned to the LLM / user.
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "postgresql_url",
        re.compile(
            r"postgresql(?:\+\w+)?://[^\s\"',})\]]+",
            re.IGNORECASE,
        ),
    ),
    (
        "redis_url",
        re.compile(
            r"redis(?:s)?://[^\s\"',})\]]+",
            re.IGNORECASE,
        ),
    ),
    (
        "generic_password",
        re.compile(
            r"(?:password|passwd|secret_key|api_key|access_key|secret|token)"
            r"\s*[=:]\s*['\"]?[^\s\"',})\]]{8,}",
            re.IGNORECASE,
        ),
    ),
    (
        "s3_credentials",
        re.compile(
            r"(?:AKIA[0-9A-Z]{16})",
            0,
        ),
    ),
    (
        "fernet_key",
        re.compile(
            r"[A-Za-z0-9_-]{43}=",
            0,
        ),
    ),
]


def _sanitize_output(text: str) -> str:
    """Redact patterns that look like secrets in sandbox output.

    Defense-in-depth: even if env sanitization fails, this prevents
    secrets from reaching the LLM or the user.
    """
    if not text:
        return text
    sanitized = text
    for name, pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub(f"[REDACTED:{name}]", sanitized)
    return sanitized


_NAMEERROR_STATELESS_HINT = (
    "Hint: each code_interpreter call runs in a fresh, isolated process - "
    "no variables, imports, or state from previous calls are available. "
    "Define everything this code needs (imports, helper functions, data) "
    "within this single call, or fix the name if it is simply misspelled."
)
# The same hint once the call had a sandbox workspace: files persist,
# and ``session=true`` keeps variables.
_NAMEERROR_WORKSPACE_HINT = (
    " Files under workspace/ do persist: save what a later call needs there, or call "
    "code_interpreter with session=true to keep variables and imports between calls."
)
_NAMEERROR_RESTARTED_HINT = (
    "Hint: this call ran in a NEW kernel (the previous one was gone), so variables and imports "
    "from earlier session calls are not defined: define them again in this call, or reload "
    "them from files under workspace/."
)

# Matches CPython's standard NameError phrasing on a single traceback line,
# e.g. "NameError: name 'agents' is not defined" (optionally followed by
# "Did you forget to import 'x'?"). A single-line regex avoids false positives
# where "NameError" and "is not defined" appear on unrelated lines of a long
# traceback (e.g. chained exceptions). ImportError / ModuleNotFoundError are
# deliberately NOT covered: in this sandbox they signal a blocked or missing
# module, and lost cross-call imports surface as NameError anyway.
_NAMEERROR_RE = re.compile(r"NameError:.*\bis not defined\b")


def format_text_result(result: dict) -> str:
    """Format an execution result as text for the LLM tool response.

    Images are *not* included here — they are collected separately and
    injected into the final chat response after the agentic loop completes.

    When the error text contains a ``NameError`` with "is not defined", a
    targeted reminder about per-call isolation is appended so the model
    recovers in the very next iteration rather than repeating the mistake.
    """
    parts: list[str] = []
    call = result.get("workspace")
    if call is not None:
        from app.services.sandbox_workspace.run import SESSION, format_call

        parts.append(format_call(call))

    if result.get("error"):
        error_text = _sanitize_output(result["error"])
        if _NAMEERROR_RE.search(error_text):
            if call is not None and call.mode == SESSION:
                hint = _NAMEERROR_RESTARTED_HINT if call.kernel == "restarted" else ""
            else:
                hint = _NAMEERROR_STATELESS_HINT + (_NAMEERROR_WORKSPACE_HINT if call is not None else "")
            parts.append(f"Error:\n{error_text}" + (f"\n\n{hint}" if hint else ""))
        else:
            parts.append(f"Error:\n{error_text}")

    stdout = result.get("stdout", "").strip()
    if stdout:
        parts.append(f"Output:\n{_sanitize_output(stdout)}")
    elif not result.get("error"):
        parts.append("Code executed successfully (no printed output).")

    stderr = result.get("stderr", "").strip()
    if stderr:
        parts.append(f"Warnings/Info:\n{_sanitize_output(stderr)}")

    n_images = len(result.get("images", []))
    if n_images:
        parts.append(
            f"Generated {n_images} chart image(s) — they will be displayed "
            "to the user automatically. Describe what the chart(s) show."
        )

    if call is not None and getattr(call, "hints", None):
        parts.extend(call.hints)

    return "\n\n".join(parts)


async def inject_code_images(
    response_text: str,
    images: list[str],
    *,
    db: object | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    assignment_run_id: str | None = None,
) -> str:
    """Append any generated chart images to the response markdown.

    Called after the agentic loop to ensure charts are visible to the user
    even if the LLM didn't explicitly include them in its response text.

    When a DB session plus a persistence scope (``conversation_id`` /
    ``assignment_run_id``) or an owner (``user_id``) are supplied, each chart is
    persisted as a durable ``FileAttachment`` (linked to the conversation /
    assignment run) and referenced by a stable
    ``/api/v1/chat/files/{id}/download`` URL — so charts survive page reloads
    exactly like any other conversation file. The legacy behaviour (ephemeral
    presigned ``charts/`` URL that expired after ~1h, or an inline base64 data
    URL) is kept as a no-DB fallback.
    """
    if not images:
        return response_text

    image_parts: list[str] = []
    for i, img_b64 in enumerate(images):
        url = await _store_chart_image(
            img_b64,
            i,
            db=db,
            user_id=user_id,
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
        )
        image_parts.append(f"![Figure {i + 1}]({url})")

    return response_text.rstrip() + "\n\n" + "\n\n".join(image_parts)


async def _store_chart_image(
    img_b64: str,
    index: int,
    *,
    db: object | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    assignment_run_id: str | None = None,
) -> str:
    """Persist a base64 chart image and return a URL to it.

    Preferred path (DB session + a conversation/run scope or an owner): store
    as a proper ``FileAttachment`` so the chart is a durable conversation / run
    file and return its ``/api/v1/chat/files/{id}/download`` URL. Falls back to
    the legacy presigned ``charts/`` object (then an inline base64 data URL)
    when no DB session / persistence scope is supplied or persistence fails.
    """
    import base64
    import uuid as _uuid

    raw_bytes = base64.b64decode(img_b64)
    filename = f"chart_{_uuid.uuid4().hex[:8]}.png"

    # Company-managed assignment runs have no file owner (``user_id`` is empty)
    # but still provide an ``assignment_run_id`` scope, so gate on having a DB
    # session plus any owner/scope rather than on ``user_id`` alone.
    if db is not None and (user_id or conversation_id or assignment_run_id):
        try:
            from app.services.builtin_tools._shared import _persist_generated_file

            file_att = await _persist_generated_file(
                db=db,
                user_id=user_id or "",
                filename=filename,
                raw_bytes=raw_bytes,
                content_type="image/png",
                conversation_id=conversation_id,
                assignment_run_id=assignment_run_id,
                source="agent",
            )
            return f"/api/v1/chat/files/{file_att.id}/download"
        except Exception:
            logger.warning(
                "Persisting code-interpreter chart as a FileAttachment failed; falling back to an ephemeral URL",
                exc_info=True,
            )

    try:
        import io

        from app.services.file_storage import file_storage

        await asyncio.to_thread(
            lambda: file_storage.client.put_object(
                bucket_name=file_storage._bucket,
                object_name=f"charts/{filename}",
                data=io.BytesIO(raw_bytes),
                length=len(raw_bytes),
                content_type="image/png",
            )
        )
        # Return a presigned URL for direct access
        return await file_storage.presigned_url(f"charts/{filename}")
    except Exception:
        # Fallback to inline base64
        return f"data:image/png;base64,{img_b64}"
