"""The xtm-one lab: the product started from a checkout, for a look at a change.

Run by ``[service.lab]`` in ``.xtm-one/checks.toml`` (``sandbox_start_service``)
on the ``python-dev`` image, whose ``/opt/venv`` already holds the backend's
locked dependencies. Standard library only. The stores come from the image's
``/opt/stores`` (``.xtm-one/stores.py``); on an older image without
them they are fetched as before. The frontend toolchain is fetched from the
hosts a lab may reach (npm and yarn), each artefact pinned by version and
digest. Then it starts:

    PostgreSQL 16 + pgvector  127.0.0.1:5432   (the ``pixeltable-pgserver`` wheel's build)
    Dragonfly (Redis API)     127.0.0.1:6379
    Silo (S3 API)             127.0.0.1:9000
    stub model (OpenAI API)   127.0.0.1:9099   (a fixed answer, no network)
    backend (uvicorn)         127.0.0.1:8100   (migrations and seeded admin at startup)
    frontend (vite dev)       0.0.0.0:3100     (the preview; proxies /api to the backend)

The frontend starts only once the backend answers ``/api/health/ready``, so
the preview is up only when sign-in works. Once it is up, the lab signs in as
its administrator and creates one row of each entity kind the API lets it
create (``seed_entities``), so every detail page has a URL to open; the URLs
are logged and written to /tmp/lab/seeded.json. A row the deployment refuses
(a feature flag off, a mode condition) is logged and skipped, never fatal.
Everything lives under /tmp/lab and dies with the lab. Never a deployment: a
throwaway instance for one change.
dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import io
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stores  # noqa: E402 - .xtm-one/stores.py, beside this directory

ROOT = Path("/tmp/lab")
CACHE = ROOT / "cache"
CHECKOUT = Path(__file__).resolve().parents[2]
T0 = time.monotonic()

# (url, algorithm, digest): a download whose digest differs stops the lab.
# The stores' pins live in .xtm-one/stores.py.
NODE = (
    "https://registry.npmjs.org/node-linux-x64/-/node-linux-x64-24.22.2.tgz",
    "sha512",
    "R5qxeQXhLZLFRzZ0Nh+Ux/dVgIVVbhwCine7eXN2CUaehslzemv24CgzfNb0U+NGOlSOVvlSpabGkiu8lg3oNg==",
)
# The yarn release frontend/package.json pins (packageManager).
YARN = (
    "https://registry.npmjs.org/@yarnpkg/cli-dist/-/cli-dist-4.18.1.tgz",
    "sha512",
    "EH+A4TxAtBWXKE0DGQxW9Bo9crXHLJ73mfxjTQJuv8XonNWWTpei8BQ5JjczHJNU9GROTycwMFn8iOvRIZXUcA==",
)
# The pnpm release XTM-Foundation/filigran-design-system pins (root engines.pnpm), fetched only when
# LAB_CHATBOT names a checkout of that pnpm monorepo without a built @filigran/chatbot (#11175).
PNPM = (
    "https://registry.npmjs.org/pnpm/-/pnpm-10.10.0.tgz",
    "sha512",
    "1hXbJG/nDyXc/qbY1z3ueCziPiJF48T2+Igkn7VoFJMYY33Kc8LFyO8qTKDVZX+5VnGIv6tH9WbR7mzph4FcOQ==",
)

PG_PORT, REDIS_PORT, S3_PORT = stores.PG_PORT, stores.REDIS_PORT, stores.S3_PORT
LLM_PORT, API_PORT, WEB_PORT = 9099, 8100, 3100
CHILDREN: list[subprocess.Popen] = []


def log(message: str) -> None:
    print(f"[lab {time.monotonic() - T0:6.1f}s] {message}", flush=True)


def fetch(spec: tuple[str, str, str]) -> bytes:
    """The bytes at *spec*'s URL, checked against its digest."""
    url, algorithm, expected = spec
    started = time.monotonic()
    with urllib.request.urlopen(url, timeout=300) as response:
        data = response.read()
    digest = hashlib.new(algorithm, data)
    actual = base64.b64encode(digest.digest()).decode() if algorithm == "sha512" else digest.hexdigest()
    if actual != expected:
        raise SystemExit(f"lab: {url} has {algorithm} {actual}, expected {expected}")
    log(f"fetched {url.rsplit('/', 1)[-1]} ({len(data) // 1_000_000} MB, {time.monotonic() - started:.1f}s, {algorithm} {actual})")
    return data


def pump(name: str, stream) -> None:
    for line in iter(stream.readline, b""):
        if b"/api/health/ready" in line:  # the lab's own readiness polls
            continue
        sys.stdout.write(f"[{name}] {line.decode('utf-8', 'replace')}")
        sys.stdout.flush()


def spawn(
    name: str, argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, daemon: bool = True
) -> subprocess.Popen:
    """Start *argv* with its output prefixed by *name*; a daemon is watched: the lab stops when it exits."""
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    threading.Thread(target=pump, args=(name, process.stdout), daemon=True).start()
    if daemon:
        CHILDREN.append(process)
    log(f"started {name} (pid {process.pid})")
    return process


def run(name: str, argv: list[str], **kwargs) -> None:
    process = spawn(name, argv, daemon=False, **kwargs)
    if process.wait() != 0:
        raise SystemExit(f"lab: {name} exited with {process.returncode}")


def startup_failure(body: bytes) -> str:
    """The ``startup_error`` a 503 readiness answer names, empty while the backend is only starting.

    The backend keeps its process alive after a failed startup (``_run_startup_tasks``) and answers
    503 with the error until it is replaced: a lab would otherwise wait out its whole timeout.
    """
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return ""
    error = payload.get("startup_error") if isinstance(payload, dict) else None
    return str(error).strip() if error else ""


def wait_http(url: str, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    reported = 0.0
    while time.monotonic() < deadline:
        answer = "no answer"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status < 500:
                    log(f"{what} ready")
                    return
        except urllib.error.HTTPError as exc:
            body = exc.read()[:2000]
            failure = startup_failure(body)
            if failure:
                # Fail fast: a failed startup never recovers in place.
                raise SystemExit(f"lab: {what} startup failed: {failure[:500]}") from None
            answer = f"{exc.code} {body[:300].decode('utf-8', 'replace')}"
        except Exception as exc:  # noqa: BLE001 - not up yet
            answer = type(exc).__name__
        if time.monotonic() - reported >= 15:
            reported = time.monotonic()
            log(f"waiting for {what}: {answer}")
        for child in CHILDREN:
            if child.poll() is not None:
                raise SystemExit(f"lab: a process exited ({child.args[0]}, code {child.returncode}) while waiting for {what}")
        time.sleep(1)
    raise SystemExit(f"lab: {what} not ready within {timeout:.0f}s")


HOP_BY_HOP_HEADERS = ("transfer-encoding", "connection", "content-length")


def has_body(command: str, status: int) -> bool:
    """Whether an HTTP/1.1 answer carries a body (RFC 9112 section 6.3).

    The answers to a HEAD, a 1xx, a 204 and a 304 end at their headers: a chunked terminator
    written after them is read by the browser as the start of the next answer on the keep-alive
    connection, which Vite's 304 on a cached module turns into ERR_INVALID_HTTP_RESPONSE on the
    next lazy chunk (every lazy route falls in the error boundary).
    """
    return command.upper() != "HEAD" and status >= 200 and status not in (204, 304)


def relay_response(handler: http.server.BaseHTTPRequestHandler, response, header=None) -> None:
    """Write an upstream ``http.client`` answer back through a preview Front, streamed.

    The status and the end-to-end headers go as they came (``header(key, value)`` may rewrite a
    value), the body is re-chunked as it arrives so a streamed answer is never buffered, and an
    answer with no body (``has_body``) ends at its headers.
    """
    handler.send_response(response.status, response.reason)
    for key, value in response.getheaders():
        if key.lower() not in HOP_BY_HOP_HEADERS:
            handler.send_header(key, header(key, value) if header else value)
    if not has_body(handler.command, response.status):
        handler.end_headers()
        handler.wfile.flush()
        return
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    while True:
        chunk = response.read1(65536)
        if not chunk:
            break
        handler.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        handler.wfile.flush()
    handler.wfile.write(b"0\r\n\r\n")
    handler.wfile.flush()


def start_stores() -> None:
    """PostgreSQL + pgvector, Dragonfly and Silo on loopback, from the image's /opt/stores when it has them."""
    dest = stores.STORES_DIR
    if stores.installed(dest):
        log(f"stores from the image ({dest}), no download")
    else:
        dest = CACHE / "stores"
        if not stores.installed(dest):
            stores.install(dest)
    stores.start_all(dest, ROOT, lambda name, argv, env: spawn(name, argv, env=env))


#: The longest a scripted turn is held open: under the agent loop's 120 s stream idle watchdog
#: (``agent_loop._STREAM_IDLE_TIMEOUT_SECS``), which would otherwise end the turn with an error.
LAB_SCRIPT_MAX_HOLD_SECONDS = 110


#: The pause ``lab:wait`` and ``lab:cut-wait`` ask for when no seconds are given, and the longest.
LAB_WAIT_DEFAULT_SECONDS = 60
LAB_WAIT_MAX_SECONDS = 300


def parse_lab_script(text: object) -> dict | None:
    """The stream script a chat message asks the stub model for, or ``None`` for an ordinary message.

    ``lab:resuming [seconds] [resume/of] [cause]`` (defaults ``90 2/100 stop``): the turn is
    announced as one the platform resumed by itself after a cut (a ``turn_resuming`` frame, as
    ``cut_turn_resume.resuming_frame`` writes it) and held open ``seconds`` before it answers, so
    the live resuming states render. The word ``cut`` anywhere after it (``lab:resuming 20 1/100 stop cut``)
    also stores the rows of a real cut and resume (``seed_resumed_reply.py``): an interrupted reply
    before the frame, then ``resumed_from`` on the held answer, so the cut-turn card goes resuming,
    then "resumed by itself".

    ``lab:wait [seconds] [reason words]`` (defaults ``60 Lab wait``): the stub calls the real ``wait``
    tool with that duration and reason, on the lab's running seeded run (``wait_call_chunks``).

    ``lab:cut-wait [seconds]`` (default ``60``): the stub answers, then ``seed_cut_wait.py`` leaves a
    ``wait`` round cut by a worker stop in the conversation's checkpoint and queues the turn for its
    automatic resume, which finishes the pause through ``_continue_cut_wait``.

    ``lab:fleet-pause`` / ``lab:fleet-raise``: in a fleet "Modify with AI" chat, the stub calls the real
    ``set_fleet_automations(action="pause")`` without ``confirm`` (the "Not applied" summary) or the real
    ``update_fleet`` one autonomy level up (the raise refusal) on the bound fleet (``fleet_call_chunks``).

    ``lab:call <tool_name> [json object]`` (arguments default ``{}``): the stub calls that tool by name,
    even when the turn's schemas do not offer it (a withheld or deferred tool: the executor answers its
    own refusal), then answers the turn's next model call with the tool's result (``called_tool_answer``).
    A name or arguments it cannot read give ``{"script": "call", "error": ...}``: nothing is called.

    ``lab:echo`` then a new line and any text: the stub answers that text verbatim (everything after the
    first line; with nothing there, what follows ``lab:echo`` on the first line), streamed in small chunks
    like ``lab:stream`` (``echo_chunks``), so a pasted Markdown test renders as the assistant answer.

    ``lab:restart-mid-turn [hold seconds] [resumed seconds]`` (defaults ``5 0``, at most ``60 240``): the stub
    starts answering and calls one tool, then holds the turn's next model call while lab.py stops the backend
    with SIGTERM, as a rollout does, and starts it again ``hold`` seconds after it exited (``restart_backend``);
    the restarted process resumes the cut turn and the stub answers it (``restart_step``). With ``resumed
    seconds`` above 0 the resumed turn works like a long one: it calls one tool first, then streams its answer
    over that many seconds, naming the steers folded into it, so its live progress and a steer sent during the
    resume can be watched (``restart_resumed_chunks``).

    Lab only: the stub model is the lab's, no backend route exists.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    first, *words = text.strip().split()
    first = first.lower()
    if first == "lab:call":
        return parse_lab_call(text.strip().split(maxsplit=2)[1:])
    if first in ("lab:fleet-pause", "lab:fleet-raise"):
        return {"script": first.removeprefix("lab:")}
    if first == "lab:stream":
        return parse_lab_stream(words)
    if first == "lab:echo":
        return parse_lab_echo(text)
    if first == "lab:restart-mid-turn":
        script = {"script": "restart-mid-turn", "hold": LAB_RESTART_DEFAULT_HOLD_SECONDS, "resumed": 0}
        numbers = [int(word) for word in words[:2] if word.isdigit()]
        if numbers:
            script["hold"] = min(numbers[0], LAB_RESTART_MAX_HOLD_SECONDS)
        if len(numbers) > 1:
            script["resumed"] = min(numbers[1], LAB_RESTART_MAX_RESUMED_SECONDS)
        return script
    if first in ("lab:wait", "lab:cut-wait"):
        script = {"script": first.removeprefix("lab:"), "seconds": LAB_WAIT_DEFAULT_SECONDS}
        if words and words[0].isdigit():
            script["seconds"] = max(1, min(int(words.pop(0)), LAB_WAIT_MAX_SECONDS))
        if first == "lab:wait":
            script["reason"] = " ".join(words) or "Lab wait"
        return script
    if first != "lab:resuming":
        return None
    script = {"script": "resuming", "seconds": 90, "resume": 2, "of": 100, "cause": "stop", "cut": False}
    for word in words:
        if word.lower() == "cut":
            script["cut"] = True
        elif word.isdigit():
            script["seconds"] = min(int(word), LAB_SCRIPT_MAX_HOLD_SECONDS)
        elif "/" in word and all(part.isdigit() and int(part) > 0 for part in word.split("/", 1)):
            resume, of = (int(part) for part in word.split("/", 1))
            script["resume"], script["of"] = min(resume, of), of
        elif word.isalpha():
            script["cause"] = word.lower()
    return script


#: ``lab:stream [bursts] [gap_ms] [per_burst]`` defaults and caps: a long Markdown answer sent in bursts of
#: ``per_burst`` small chunks written back to back, ``gap_ms`` apart, as a provider's network clumps do.
LAB_STREAM_DEFAULTS = {"bursts": 120, "gap_ms": 60, "per_burst": 4}
LAB_STREAM_MAX_BURSTS = 2000
LAB_STREAM_MAX_GAP_MS = 2000


def parse_lab_stream(words: list[str]) -> dict:
    """The ``lab:stream`` script of *words*: up to three integers (bursts, gap in ms, chunks per burst)."""
    script = {"script": "stream", **LAB_STREAM_DEFAULTS}
    numbers = [int(word) for word in words if word.isdigit()][:3]
    for key, value in zip(("bursts", "gap_ms", "per_burst"), numbers):
        script[key] = value
    script["bursts"] = max(1, min(script["bursts"], LAB_STREAM_MAX_BURSTS))
    script["gap_ms"] = max(0, min(script["gap_ms"], LAB_STREAM_MAX_GAP_MS))
    script["per_burst"] = max(1, min(script["per_burst"], 50))
    return script


#: The Markdown ``lab:stream`` repeats: prose, a table, a Python and a JSON code block, a list.
LAB_STREAM_BODY = (
    "## Findings\n\nThe answer streams in bursts so the reveal cadence of the chat can be measured. "
    "Each paragraph carries **bold**, `inline code` and a [link](https://filigran.io).\n\n"
    "| Indicator | Type | Score | First seen | Source |\n|---|---|---|---|---|\n"
    "| 198.51.100.7 | ipv4 | 82 | 2026-11-01 | lab |\n| evil.example | domain | 64 | 2026-11-03 | lab |\n"
    "| 5f4dcc3b5aa765d61d8327deb882cf99 | md5 | 91 | 2026-11-05 | lab |\n\n"
    "```python\ndef score(indicator: dict) -> int:\n    weights = {\"ipv4\": 2, \"domain\": 3, \"md5\": 5}\n"
    "    return min(100, weights.get(indicator[\"type\"], 1) * indicator[\"hits\"])\n```\n\n"
    "```json\n{\"indicator\": \"198.51.100.7\", \"score\": 82, \"labels\": [\"c2\", \"lab\"]}\n```\n\n"
    "- First point with a nested list:\n  - nested item one\n  - nested item two\n- Second point.\n\n"
)


def stream_chunks(script: dict) -> list[str]:
    """The text pieces of a ``lab:stream`` answer: bursts x per_burst pieces of 3 to 9 characters."""
    total = script["bursts"] * script["per_burst"]
    body = LAB_STREAM_BODY * (1 + total * 6 // len(LAB_STREAM_BODY))
    pieces, at = [], 0
    for index in range(total):
        size = 3 + (index * 7) % 7
        pieces.append(body[at:at + size])
        at += size
    return pieces


#: ``lab:echo``: the chunks written per burst, the silence between bursts, and the longest text echoed.
LAB_ECHO_PER_BURST = 4
LAB_ECHO_GAP_MS = 20
LAB_ECHO_MAX_CHARS = 60_000


def parse_lab_echo(text: str) -> dict:
    """The ``lab:echo`` script of the message *text*: the text after its first line, verbatim.

    With nothing after the first line, the words after ``lab:echo`` on that line; with nothing at all,
    ``{"script": "echo", "error": ...}`` and the stub answers the usage line.
    """
    head, _, rest = text.strip("\n").partition("\n")
    echoed = rest if rest.strip() else head.strip()[len("lab:echo"):].strip()
    if not echoed.strip():
        return {"script": "echo", "error": "nothing to echo: write lab:echo, a new line, then the text"}
    return {"script": "echo", "text": echoed[:LAB_ECHO_MAX_CHARS]}


def echo_chunks(text: str) -> list[str]:
    """The pieces of a ``lab:echo`` answer: *text* cut in pieces of 3 to 9 characters, joined back verbatim."""
    pieces, at, index = [], 0, 0
    while at < len(text):
        size = 3 + (index * 7) % 7
        pieces.append(text[at:at + size])
        at, index = at + size, index + 1
    return pieces


#: The drain the lab backend gets on a stop (``--timeout-graceful-shutdown`` and ``XTM_ONE_GRACEFUL_TIMEOUT``, as
#: ``backend/start-uvicorn.sh`` sets both): a turn still running past it is cut by the backend's own drain.
LAB_BACKEND_GRACE_SECONDS = 15
#: ``lab:restart-mid-turn``: how long the backend stays stopped before it is started again, by default and at most.
LAB_RESTART_DEFAULT_HOLD_SECONDS = 5
LAB_RESTART_MAX_HOLD_SECONDS = 60
#: ``lab:restart-mid-turn``: how long the resumed turn streams its answer at most (one piece a second keeps the
#: agent loop's stream idle watchdog fed).
LAB_RESTART_MAX_RESUMED_SECONDS = 240
#: The id of the tool call the resumed turn makes first when it is held (``resumed seconds`` above 0).
LAB_RESTART_RESUMED_CALL_ID = "call_lab_restart_resumed"
#: The id of the tool call the turn makes before the stop, and the tools it calls (the first one offered).
LAB_RESTART_CALL_ID = "call_lab_restart_mid_turn"
LAB_RESTART_TOOLS = ("search_memory", "search_knowledge")
#: How the resume note a resumed turn reads starts (``turn_checkpoint.resume_note``).
LAB_RESUMED_NOTE_PREFIX = "[Resumed:"

#: The backend child and how many times ``lab:restart-mid-turn`` replaced it.
BACKEND: dict = {"process": None, "generation": 0}
#: One restart at a time, when the last one stopped and brought the backend back (UTC clock), and whether its cut
#: turn is still to be answered: a resumed call that carries no resume note is answered, never stopped again.
_RESTART: dict = {"lock": threading.Lock(), "stopped_at": "", "back_at": "", "pending": False}
#: Set once the lab logged ``lab up`` (every row seeded): a restart before it would stop the backend mid-seed, and
#: the lab's watch then stops the whole lab.
LAB_UP = threading.Event()
#: The answer of a ``lab:restart-mid-turn`` turn sent before ``lab up``: nothing is stopped.
RESTART_BEFORE_UP_TEXT = "Lab script: the lab is still starting, nothing was restarted; send it again after lab up."


def restart_refusal() -> str | None:
    """The answer of a ``lab:restart-mid-turn`` turn that must not restart anything, else ``None``.

    Before ``lab up`` the backend may still be seeding: stopping it then lost the whole lab (it exited 1).
    """
    if LAB_UP.is_set():
        return None
    log("lab script restart-mid-turn: the lab is still starting, nothing stopped")
    return RESTART_BEFORE_UP_TEXT


def backend_argv() -> list[str]:
    """The backend's command: ``lab_asgi`` is ``app.main:application`` with the lab's GitHub repository answered
    in front of it, with a bounded drain on a stop."""
    return [sys.executable, "-m", "uvicorn", "lab_asgi:application", "--app-dir", str(CHECKOUT / ".xtm-one" / "lab"),
            "--host", "127.0.0.1", "--port", str(API_PORT),
            "--timeout-graceful-shutdown", str(LAB_BACKEND_GRACE_SECONDS)]


def backend_spawn_env(generation: int) -> dict[str, str]:
    """The backend's environment; a restarted one (*generation* above 0) reports a build of its own.

    A rollout's new pod runs another build, so ``cut_turn_resume.sweep_cut_turns`` resumes a turn a stop cut at
    once instead of deferring it ``SAME_BUILD_DEFER_SECONDS`` for a process of another build.
    """
    env = {**backend_env(), "XTM_ONE_GRACEFUL_TIMEOUT": str(LAB_BACKEND_GRACE_SECONDS)}
    if generation:
        env["APP_VERSION"] = f"0.0.0-lab-restart.{generation}"
    return env


def utc_clock() -> str:
    return time.strftime("%H:%M:%SZ", time.gmtime())


def message_text(message: object) -> str:
    """The text of one OpenAI chat message, its parts joined ("" when none)."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def restart_step(request: dict, pending: bool = False) -> str:
    """The step of a ``lab:restart-mid-turn`` turn *request* is: ``start``, ``stop`` or ``resumed``.

    ``resumed``: the request carries the platform's resume note (the restarted process resumed the cut turn), or
    a restart's cut turn is still to be answered (*pending*), so a resume never stops the backend twice.
    ``stop``: its last message answers the scripted tool call (the turn's next model call). ``start``: else.
    """
    messages = [m for m in request.get("messages") or [] if isinstance(m, dict)]
    if pending or any(message_text(m).startswith(LAB_RESUMED_NOTE_PREFIX) for m in messages):
        return "resumed"
    if messages and messages[-1].get("role") == "tool" and messages[-1].get("tool_call_id") == LAB_RESTART_CALL_ID:
        return "stop"
    return "start"


def restart_start_chunks(offered: set[str]) -> list[dict]:
    """The stream chunks of the turn's first answer: a few text pieces, then one read-only tool call."""
    name = next((tool for tool in LAB_RESTART_TOOLS if tool in offered), LAB_RESTART_TOOLS[0])
    call = {"index": 0, "id": LAB_RESTART_CALL_ID, "type": "function",
            "function": {"name": name, "arguments": json.dumps({"query": "lab restart mid-turn"})}}
    pieces = ("Lab script: ", "the answer ", "starts, ", "one tool ", "is called, ", "then the backend ", "stops.")
    return [
        *({"choices": [{"index": 0, "delta": {"role": "assistant", "content": piece}, "finish_reason": None}]}
          for piece in pieces),
        {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [call]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
    ]


def start_backend_restart(hold: int) -> threading.Event | None:
    """Start ``restart_backend`` on a thread; the event it sets once the old backend exited, ``None`` when one runs."""
    if not _RESTART["lock"].acquire(blocking=False):
        log("lab script restart-mid-turn: a restart is already running, nothing stopped")
        return None
    exited = threading.Event()
    threading.Thread(target=restart_backend, args=(hold, exited), daemon=True).start()
    return exited


def restart_backend(hold: int, exited: threading.Event) -> None:
    """Stop the backend child with SIGTERM, as a rollout does, then start it again *hold* seconds after it exited.

    The child leaves ``CHILDREN`` first, so the lab's watch does not stop the lab when it exits. Its own drain
    (``LAB_BACKEND_GRACE_SECONDS``) cuts the running turn, stores the interrupted reply and queues the turn; the
    restarted process (a build of its own, ``backend_spawn_env``) resumes it. Runs with ``_RESTART['lock']`` held.
    """
    try:
        old = BACKEND["process"]
        if old is None or old.poll() is not None:
            log("lab script restart-mid-turn: no running backend to stop")
            return
        if old in CHILDREN:
            CHILDREN.remove(old)
        log(f"lab script restart-mid-turn: SIGTERM to the backend (pid {old.pid}) at {utc_clock()}")
        old.send_signal(signal.SIGTERM)
        try:
            old.wait(timeout=LAB_BACKEND_GRACE_SECONDS + 30)
        except subprocess.TimeoutExpired:
            old.kill()
            old.wait()
        _RESTART["stopped_at"], _RESTART["back_at"], _RESTART["pending"] = utc_clock(), "", True
        log(f"lab script restart-mid-turn: backend stopped at {_RESTART['stopped_at']} (exit {old.returncode}), "
            f"started again in {hold} s")
        exited.set()
        time.sleep(hold)
        BACKEND["generation"] += 1
        BACKEND["process"] = spawn("backend", backend_argv(), cwd=CHECKOUT / "backend",
                                   env=backend_spawn_env(BACKEND["generation"]))
        wait_http(f"http://127.0.0.1:{API_PORT}/api/health/ready", 600, "restarted backend")
        _RESTART["back_at"] = _RESTART["back_at"] or utc_clock()
        log(f"lab script restart-mid-turn: backend back at {_RESTART['back_at']}")
    except SystemExit as exc:
        log(f"lab script restart-mid-turn: the backend did not come back: {exc.code}")
    finally:
        exited.set()
        _RESTART["lock"].release()


def restart_script(request: dict, pending: bool = False) -> dict | None:
    """The ``lab:restart-mid-turn`` script a model call of that turn runs, with the steers folded into it.

    Read from the last user message, else, while a restart's cut turn is still to be answered (*pending*), from
    the latest user message that asked for it: a steer sent during the resume is a user message of its own after
    it. A later turn of the conversation (nothing pending) never reads an older script. ``steers`` lists the texts
    of the user messages after the script's, the resume note and the script repeated left out.
    """
    users = [m for m in request.get("messages") or [] if isinstance(m, dict)]
    found = None
    for index in range(len(users) - 1, -1, -1):
        if users[index].get("role") != "user":
            continue
        script = parse_lab_script(message_text(users[index]))
        if script and script["script"] == "restart-mid-turn":
            found = index, script
            break
        if script:  # a later lab script of another turn: not this one
            return None
    if found is None:
        return None
    index, script = found
    if pending:
        # The resume may append the person's message again after its note: start at the script's first copy of
        # this turn, so the steers sent between the two are still read.
        for earlier in range(index - 1, -1, -1):
            if users[earlier].get("role") != "user":
                continue
            again = parse_lab_script(message_text(users[earlier]))
            if again and again["script"] == "restart-mid-turn":
                index = earlier
            elif again:
                break
    after = users[index + 1:]
    steers = [
        message_text(m).strip() for m in after
        if m.get("role") == "user" and message_text(m).strip()
        and not message_text(m).startswith(LAB_RESUMED_NOTE_PREFIX)
        and not parse_lab_script(message_text(m))
    ]
    if steers and not pending:
        return None  # a later message of an ordinary turn
    return {**script, "steers": steers}


def restart_resumed_step(request: dict) -> str:
    """A held resumed turn's step: ``call`` (its first call: one tool) or ``answer`` (after the tool's result)."""
    messages = [m for m in request.get("messages") or [] if isinstance(m, dict)]
    if any(m.get("role") == "tool" and m.get("tool_call_id") == LAB_RESTART_RESUMED_CALL_ID for m in messages):
        return "answer"
    return "call"


def restart_resumed_call(offered: set[str]) -> tuple[str, dict]:
    """The read-only tool the held resumed turn calls first, and its arguments."""
    name = next((tool for tool in LAB_RESTART_TOOLS if tool in offered), LAB_RESTART_TOOLS[0])
    return name, {"query": "lab resumed turn"}


def restart_resumed_pieces(script: dict) -> list[str]:
    """The pieces of a held resumed turn's answer, one streamed a second over ``resumed`` seconds."""
    steers = script.get("steers") or []
    named = "; ".join(f'"{steer[:80]}"' for steer in steers) if steers else "none"
    pieces = [f"Lab script: the resumed turn works for {script['resumed']} s. Steers folded in: {named}. "]
    pieces += [f"Step {second} of {script['resumed']}. " for second in range(1, script["resumed"])]
    pieces.append(restart_resumed_answer())
    return pieces


def restart_resumed_answer() -> str:
    """The answer to the resumed turn of ``lab:restart-mid-turn``, and its log line."""
    stopped = _RESTART["stopped_at"] or "an unknown time"
    back = _RESTART["back_at"] = _RESTART["back_at"] or utc_clock()  # the resumed call proves it is back
    _RESTART["pending"] = False
    log(f"lab script restart-mid-turn: backend stopped at {stopped}, back at {back}, resumed turn answered")
    return (f"Lab script: the backend stopped at {stopped} and was back at {back}; "
            "the restarted process resumed this turn and answered it.")


#: The longest tool result ``lab:call`` quotes in its answer, in characters.
LAB_CALL_RESULT_MAX_CHARS = 2000


def parse_lab_call(words: list[str]) -> dict:
    """The ``lab:call`` script of *words* (the tool name, then the JSON arguments text, if any)."""
    import re

    usage = "write lab:call <tool_name> [json object]"
    if not words or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", words[0]):
        return {"script": "call", "error": f"no tool name: {usage}"}
    arguments: object = {}
    rest = words[1].strip() if len(words) > 1 else ""
    if rest.startswith("{"):
        # The leading JSON object only: an assignment run's rendered prompt appends its
        # trigger block ("--- Trigger Information ---") after the script line (#11304).
        try:
            arguments, _end = json.JSONDecoder().raw_decode(rest)
        except ValueError:
            arguments = None
    elif rest and not rest.startswith("---"):
        arguments = None
    if rest:
        if not isinstance(arguments, dict):
            return {"script": "call", "error": f"the arguments are not a JSON object: {usage}"}
    return {"script": "call", "tool": words[0], "arguments": arguments}


def unstreamed_script(request: dict) -> dict | None:
    """The script a NON-streamed call runs: ``lab:call`` alone, and only on a call that offers tools.

    An assignment run's agent loop has no live stream, so its model calls are plain ``chat.completion``
    calls (#11304): ``lab:call`` there calls the tool and the run stores the assistant tool call and the
    tool's result, the agentic steps of its run page. The other scripts hold or burst a live stream and
    stay streamed-only, and a side call that offers no tool (a title, an activation gate) answers as before.
    """
    if not request.get("tools"):
        return None
    script = parse_lab_script(last_user_text(request))
    return script if script and script["script"] == "call" else None


def called_tool_completion(name: str, arguments: dict) -> dict:
    """The non-streamed ``chat.completion`` of one tool call *name* with *arguments* (``unstreamed_script``)."""
    call = {"id": f"call_lab_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}
    return {
        "id": "lab", "object": "chat.completion", "model": "lab-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                     "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def called_tool_answer(request: dict) -> str:
    """The answer to the model call after a ``lab:call``: the last tool message's content, quoted on one line."""
    for message in reversed(request.get("messages") or []):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        result = " ".join(str(content or "").split())
        if len(result) > LAB_CALL_RESULT_MAX_CHARS:
            result = result[:LAB_CALL_RESULT_MAX_CHARS] + " [cut]"
        return f"Lab script: the tool answered: {result or '(empty result)'}"
    return "Lab script: no tool result reached this call."


def last_user_text(request: dict) -> str:
    """The text of the last user message of an OpenAI chat request ("" when none)."""
    for message in reversed(request.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            return content if isinstance(content, str) else ""
    return ""


def redis_command(*args: str) -> object:
    """One command to the lab's Dragonfly over RESP (standard library only); the decoded reply."""
    import socket

    payload = f"*{len(args)}\r\n".encode() + b"".join(
        f"${len(arg.encode())}\r\n".encode() + arg.encode() + b"\r\n" for arg in args
    )
    with socket.create_connection(("127.0.0.1", REDIS_PORT), timeout=5) as connection:
        connection.sendall(payload)
        reader = connection.makefile("rb")

        def read() -> object:
            line = reader.readline().rstrip(b"\r\n")
            kind, rest = line[:1], line[1:].decode()
            if kind in (b"+", b":"):
                return int(rest) if kind == b":" else rest
            if kind == b"-":
                raise RuntimeError(f"redis: {rest}")
            if kind == b"$":
                return None if rest == "-1" else reader.read(int(rest) + 2)[:-2].decode()
            if kind == b"*":
                return None if rest == "-1" else [read() for _ in range(int(rest))]
            raise RuntimeError(f"redis: unexpected reply {line!r}")

        return read()


#: Turn tokens already scripted: a second model call of the same turn (a retry, a tool step) is not held again.
_SCRIPTED_TURNS: set[str] = set()


def announce_resuming(script: dict, before_publish=None) -> list[str]:
    """Publish and journal a ``turn_resuming`` frame on every chat turn in flight; the conversations reached.

    The frame travels like the platform's own (``chat_turn_stream``): on the conversation's turn
    channel, stamped with the turn's token and a sequence number, so every socket of the
    conversation (the one running the turn included) shows it; and in the turn's journal
    (``resuming`` on its state hash), so a reload or another device is told it too.
    ``before_publish(conversation, frame)`` runs first, once per turn (``store_cut_reply``): the
    frame makes the page read the thread back, so a row it stores is in that read.
    """
    from datetime import UTC, datetime

    reached = []
    for key in redis_command("KEYS", "chat:processing:*") or []:
        token = redis_command("GET", key)
        if not token or token in _SCRIPTED_TURNS:
            continue
        _SCRIPTED_TURNS.add(token)
        conversation = key.removeprefix("chat:processing:")
        frame = {
            "type": "turn_resuming", "cause": script["cause"], "resume": script["resume"], "of": script["of"],
            "cut_at": datetime.now(UTC).isoformat(),
        }
        if before_publish is not None:
            before_publish(conversation, frame)
        stamped = {**frame, "origin": "lab-stub-model", "turn": token, "seq": 1_000_000}
        redis_command("HSET", f"chat:turn:state:{conversation}:{token}", "resuming", json.dumps(frame))
        redis_command("PUBLISH", f"chat:turn:{conversation}", json.dumps([stamped]))
        reached.append(conversation)
    return reached


def last_role(request: dict) -> str:
    """The role of the last non-system message of an OpenAI chat request ("" when none).

    A scripted ``wait`` is called on the model call that answers the person's message only: the next
    call of the turn ends on the tool's result and gets the plain answer, so the script never loops.
    System messages are skipped: the agent loop appends its turn notes (background task rules, the
    wait primitive's notice, the tool groups list) after the person's message, so the real first call
    of a chat turn ends on ``system``, not ``user``.
    """
    for message in reversed(request.get("messages") or []):
        if isinstance(message, dict) and message.get("role") != "system":
            return str(message.get("role") or "")
    return ""


def offered_tools(request: dict) -> set[str]:
    """The names of the tools an OpenAI chat request offers the model."""
    return {
        str((tool.get("function") or {}).get("name"))
        for tool in request.get("tools") or []
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    }


def lab_running_run(path: Path | None = None) -> str | None:
    """The seeded fleet run that stays ``running`` (the last one ``seed_fleet_runs.py`` writes), or ``None``.

    ``lab:wait`` watches it, so the wait card lists one item whose state never ends the pause.
    """
    try:
        ids = json.loads((path or ROOT / "fleet_runs.json").read_text())
    except (OSError, ValueError):
        return None
    return str(ids[-1]) if isinstance(ids, list) and ids else None


def lab_run_status(run_id: str) -> str | None:
    """The status of assignment run *run_id*, read as the lab administrator, or ``None`` when unreadable."""
    try:
        login = api("POST", "/auth/login", body={
            "email": os.environ.get("ADMIN_EMAIL", ""), "password": os.environ.get("ADMIN_PASSWORD", ""),
        })
        token = login.get("access_token") if isinstance(login, dict) else None
        detail = api("GET", f"/assignments/runs/{run_id}", token) if token else None
    except Exception:  # noqa: BLE001 - an unreadable run is not watched
        return None
    return str(detail.get("status") or "") or None if isinstance(detail, dict) else None


def lab_watched_run(running: str | None, status) -> str | None:
    """*running* (``lab_running_run``) while *status* reads it ``running``, else ``None``.

    The seeded run has no orchestration execution: the stale-run sweep force-fails it after 900 s, and a
    ``wait`` on an ended run answers "No wait needed" before it announces the pause, so the chat draws
    no wait card. An ended or unreadable run is not watched: the pause runs on its timer.
    """
    return running if running and status(running) == "running" else None


def wait_answer(request: dict) -> tuple[str, bool]:
    """The answer to the model call after the ``lab:wait`` call, and whether the wait paused (drew its card)."""
    for message in reversed(request.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "tool":
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            result = " ".join(str(content or "").split())
            paused = not result.startswith(("No wait needed", "Cannot wait", "Invalid duration"))
            return f"Lab script: the wait answered: {result[:LAB_CALL_RESULT_MAX_CHARS] or '(empty result)'}", paused
    return "Lab script: no wait result reached this call.", False


#: The id of the ``wait`` call ``lab:wait`` streams.
LAB_WAIT_CALL_ID = "call_lab_wait"


def wait_call_chunks(script: dict, watched: str | None) -> list[dict]:
    """The stream chunks of one ``wait`` tool call: *script*'s seconds and reason, *watched* in ``until``."""
    arguments = {"duration": str(script["seconds"]), "reason": script["reason"]}
    if watched:
        arguments["until"] = watched
    call = {"index": 0, "id": LAB_WAIT_CALL_ID, "type": "function",
            "function": {"name": "wait", "arguments": json.dumps(arguments)}}
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [call]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
    ]


#: The autonomy levels from the most supervised to the least (``fleet_autonomy.AUTONOMY_LEVELS``).
LAB_AUTONOMY_LEVELS = ("full_gates", "medium", "low", "autonomous")


def bound_fleet(request: dict) -> tuple[str, str] | None:
    """``(fleet_id, autonomy level)`` of the fleet a chat request is bound to, or ``None``.

    Read from the turn's ``[Fleet]`` system block (``fleet_studio.format_fleet_studio_prompt``):
    ``fleet_id: <uuid> - workspace ..., autonomy level <level>.``
    """
    import re

    for message in request.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        if not isinstance(content, str) or "[Fleet]" not in content:
            continue
        found = re.search(r"fleet_id: ([0-9a-fA-F-]{36}) .*?autonomy level ([a-z_]+)\.", content)
        if found:
            return found.group(1), found.group(2)
    return None


def fleet_call(script: dict, fleet: tuple[str, str]) -> tuple[str, dict] | None:
    """The fleet tool and arguments a fleet script calls on *fleet*, or ``None`` when it cannot apply.

    ``fleet-pause``: ``set_fleet_automations`` pause without ``confirm`` (the tool answers the count,
    applies nothing). ``fleet-raise``: ``update_fleet`` one level above the fleet's (the tool refuses);
    a fleet already ``autonomous`` has no level above, so nothing is called.
    """
    fleet_id, level = fleet
    if script["script"] == "fleet-pause":
        return "set_fleet_automations", {"fleet_id": fleet_id, "action": "pause"}
    position = LAB_AUTONOMY_LEVELS.index(level) if level in LAB_AUTONOMY_LEVELS else 0
    if position + 1 >= len(LAB_AUTONOMY_LEVELS):
        return None
    return "update_fleet", {"fleet_id": fleet_id, "autonomy_level": LAB_AUTONOMY_LEVELS[position + 1]}


def fleet_call_chunks(name: str, arguments: dict) -> list[dict]:
    """The stream chunks of one fleet tool call *name* with *arguments*."""
    call = {"index": 0, "id": f"call_lab_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [call]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
    ]


#: How long ``lab:cut-wait`` waits for its turn to store its answer before it journals the cut wait.
LAB_CUT_WAIT_SETTLE_SECONDS = 60

#: The id of the cut ``wait`` call ``seed_cut_wait.py`` journals (its ``CALL_ID``).
LAB_CUT_WAIT_CALL_ID = "call_lab_cut_wait"

#: How long the stub holds its answer to the resumed turn's first model call, after the continued
#: pause: without it the answer follows ``wait_continued`` within milliseconds and the chat's
#: analyzing state is never seen (xtm-one #9337). Far under ``LAB_SCRIPT_MAX_HOLD_SECONDS``.
LAB_CUT_WAIT_ANALYZE_HOLD_SECONDS = 5


def answers_cut_wait(request: dict) -> bool:
    """Whether *request* is the resumed turn's model call right after the continued ``lab:cut-wait`` pause.

    The resumed request (captured in a lab from main 6f53a051, xtm-one #9337) replays the cut round
    (the ``wait`` call ``LAB_CUT_WAIT_CALL_ID`` and its result), then the platform's resume note (an
    assistant message without tool calls), then the person's message again, and is NOT streamed: its
    last tool call is the cut wait and its last tool result answers it.
    """
    last_call, last_result = None, None
    for message in request.get("messages") or []:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or ():
            if isinstance(call, dict):
                last_call = call.get("id")
        if message.get("role") == "tool":
            last_result = message.get("tool_call_id")
    return last_call == LAB_CUT_WAIT_CALL_ID and last_result == LAB_CUT_WAIT_CALL_ID


def turns_in_flight() -> list[str]:
    """The conversations whose chat turn holds its processing lock now."""
    return [key.removeprefix("chat:processing:") for key in redis_command("KEYS", "chat:processing:*") or []]


def journal_cut_wait(conversation: str, seconds: int) -> None:
    """Once *conversation*'s turn stored its answer, leave a cut ``wait`` round and queue the turn's resume.

    ``seed_cut_wait.py`` writes them with the backend's interpreter: the turn checkpoint of the
    conversation ends on a ``wait`` a worker stop cut, and the cut turn is queued like a stopping
    worker queues it, so the next sweep resumes it and ``_continue_cut_wait`` finishes the pause.
    """
    deadline = time.monotonic() + LAB_CUT_WAIT_SETTLE_SECONDS
    while redis_command("GET", f"chat:processing:{conversation}") and time.monotonic() < deadline:
        time.sleep(0.5)
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_cut_wait.py"), conversation, str(seconds)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    outcome = process.stdout.strip() if process.returncode == 0 else stderr_summary(process.stderr)
    log(f"lab script cut-wait: {conversation} {seconds} s journaled, {outcome}")


def run_seed_resumed_reply(*args: str, timeout: int = 60) -> tuple[bool, str]:
    """Run ``seed_resumed_reply.py`` with the backend's interpreter; whether it succeeded, and its output."""
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_resumed_reply.py"), *args],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=timeout,
    )
    ok = process.returncode == 0
    return ok, process.stdout.strip() if ok else stderr_summary(process.stderr)


def store_cut_reply(script: dict, cuts: dict):
    """The ``before_publish`` of a ``lab:resuming ... cut`` turn: store its interrupted reply first.

    The reply is the one a cut turn leaves (``seed_resumed_reply.py interrupted``); its sequence
    is kept in *cuts* by conversation, so ``mark_resumed_answer`` patches the answer stored after it.
    """

    def store(conversation: str, frame: dict) -> None:
        ok, out = run_seed_resumed_reply("interrupted", conversation, script["cause"])
        if ok and out.isdigit():
            cuts[conversation] = (int(out), frame["cut_at"])
        log(f"lab script resuming cut: {conversation} interrupted reply {'stored at ' + out if ok else 'not stored: ' + out}")

    return store


def mark_resumed_answer(conversation: str, script: dict, after: int, cut_at: str) -> None:
    """Put ``resumed_from`` on the turn's answer once stored (``seed_resumed_reply.py resumed``).

    Started when the hold starts, so the script polls before the answer lands and the page's
    end-of-turn read gets the marker.
    """
    ok, out = run_seed_resumed_reply(
        "resumed", conversation, str(after), script["cause"], str(script["resume"]), str(script["of"]), cut_at,
        timeout=script["seconds"] + 240,
    )
    log(f"lab script resuming cut: {conversation} answer {'marked resumed: ' + out if ok else 'not marked: ' + out}")


#: The first words of the fleet documentation drafter's system prompt
#: (``app.services.fleet_doc_generation._SYSTEM``): a request carrying them asks for a draft.
LAB_DOC_DRAFT_SYSTEM = "You write the documentation of an agent fleet"

#: A fleet whose description carries this marker gets a refusal: the 422 of the draft route.
LAB_DOC_DRAFT_REFUSE = "lab:draft-refuse"

#: A fleet whose description carries ``lab:draft-hold [seconds]`` gets its draft after that many seconds
#: (default 60, at most 110), so a Regenerate started by a person stays generating (#10859).
LAB_DOC_DRAFT_HOLD = "lab:draft-hold"
LAB_DOC_DRAFT_HOLD_DEFAULT_SECONDS = 60
LAB_DOC_DRAFT_HOLD_MAX_SECONDS = 110


def doc_draft_hold_seconds(description: object) -> int:
    """The seconds a fleet description's ``lab:draft-hold [seconds]`` marker holds a draft; 0 without it."""
    found = re.search(rf"{re.escape(LAB_DOC_DRAFT_HOLD)}(?:\s+(\d+))?", str(description or ""))
    if not found:
        return 0
    seconds = int(found.group(1)) if found.group(1) else LAB_DOC_DRAFT_HOLD_DEFAULT_SECONDS
    return max(1, min(seconds, LAB_DOC_DRAFT_HOLD_MAX_SECONDS))

#: Drafts answered since the lab started: each one is numbered, so a regenerated section changes.
_DOC_DRAFTS = iter(range(1, 1_000_000_000))


def doc_draft_answer(request: dict, number: int) -> str | None:
    """The stub's answer to a fleet documentation draft request, or ``None`` for any other request.

    The drafter (``fleet_doc_generation.build_messages``) sends its system prompt, then a user
    message opening on ``Sections to write:`` with one ``- key: guide`` line per section asked
    (every section on Generate documentation, one on Regenerate section), then ``The fleet:`` and its
    context as JSON. The answer is one JSON object with each asked key, numbered by *number* so a
    regenerate shows a new text; a fleet description carrying ``LAB_DOC_DRAFT_REFUSE`` gets a
    sentence instead, which the route answers 422; one carrying ``LAB_DOC_DRAFT_HOLD`` gets its draft
    after the hold (``doc_draft_hold_seconds``; the stub serves each request on its own thread).
    Lab only: a production model never sees it.
    """
    messages = [m for m in request.get("messages") or [] if isinstance(m, dict)]
    system = next((m.get("content") for m in messages if m.get("role") == "system"), None)
    if not isinstance(system, str) or not system.startswith(LAB_DOC_DRAFT_SYSTEM):
        return None
    user = last_user_text(request)
    if not user.startswith("Sections to write:"):
        return None
    head, _, rest = user.partition("\n\nThe fleet:\n")
    wanted = [line[2:].split(":", 1)[0].strip() for line in head.splitlines()[1:] if line.startswith("- ")]
    try:
        context = json.JSONDecoder().raw_decode(rest)[0] or {}
        fleet = context.get("fleet") or {}
    except (ValueError, AttributeError):
        context, fleet = {}, {}
    if LAB_DOC_DRAFT_REFUSE in str(fleet.get("description") or ""):
        return "Lab stub model: this fleet asked for a refusal, so no documentation is drafted."
    name = str(fleet.get("name") or "this fleet")
    hold = doc_draft_hold_seconds(fleet.get("description"))
    if hold:
        log(f"lab doc draft: {name} holds its draft {hold} s ({LAB_DOC_DRAFT_HOLD})")
        time.sleep(hold)
    sections = {
        key: f"Lab draft {number}: the {key.replace('_', ' ')} of {name}, written by the lab's stub model."
        for key in wanted if key
    }
    if "Structured parts to write" not in user:
        return json.dumps(sections)
    # A whole draft also maps the structured parts (#9768 S1); the generator adds the computed ones.
    return json.dumps({
        "sections": sections,
        "structured": {
            "purpose_line": f"Lab draft {number}: {name} turns events into reviewed work.",
            "outcome_line": "Every event reviewed by a person before anything changes.",
            "steps": doc_draft_steps(doc_draft_member_names(context), number),
            "gates": [{"action": "Approve the proposal", "module": "platform", "approver": "the fleet owner"}],
            "objects": [{"module": "platform", "objects": ["work items"]}],
        },
    })


def doc_draft_member_names(context: object) -> list[str]:
    """The member names of a drafter's fleet context, the orchestrator first; generic names when it has none."""
    members = context.get("members") if isinstance(context, dict) else None
    rows = [m for m in members or [] if isinstance(m, dict) and str(m.get("name") or "").strip()]
    rows.sort(key=lambda m: not m.get("orchestrator"))
    names = [str(m["name"]).strip() for m in rows]
    return names or ["the orchestrator", "the analyst", "the reviewer", "the operator"]


def doc_draft_steps(names: list[str], number: int) -> list[dict]:
    """Seven "How it works" steps as the drafter's STRUCTURED_GUIDE asks them (#10327), from *names*.

    Each detail is markdown of 25 to 40 words with the acting members in bold, one body carries "- "
    bullets and one step is a gate, so the About page shows the markdown bodies and folds after five
    steps on any seeded fleet with no fetch override. The last body carries *number*, so a regenerate
    shows a new text. Lab only: a production model never sees it.
    """
    a, b, c, d = (names[i % len(names)] for i in range(4))
    return [
        {"title": "Work arrives at the orchestrator", "detail": (
            f"**{a}** picks up each new event or request, reads what it is about and who sent it, "
            "then decides which member of the fleet should act on it first.")},
        {"title": "The orchestrator routes the work", "detail": (
            f"**{a}** hands the request over to **{b}** with the context it needs. Nothing changes yet: "
            "the handover only records who owns the next move and why.")},
        {"title": "The facts are gathered", "detail": (
            f"**{b}** collects what the decision needs before anything is proposed:\n\n"
            "- the related work items and their history\n"
            f"- the latest notes from **{c}**\n"
            "- what the connected integrations report")},
        {"title": "A proposal is drafted", "detail": (
            f"**{c}** turns the facts into one proposal: what should change, where, and the reason for it. "
            "The proposal is written as a *work item* so every member can read it.")},
        {"title": "A person approves the proposal", "gate": True, "detail": (
            f"A person reviews the proposal from **{c}** before anything changes. They approve it, ask for "
            f"changes or reject it, and **{a}** records the decision on the work item.")},
        {"title": "The approved change is applied", "detail": (
            f"**{d}** applies the approved change through the fleet's integrations, then reads it back to "
            "confirm it landed as proposed and writes the result on the work item.")},
        {"title": "The outcome is reported", "detail": (
            f"**{a}** closes the loop: it tells the requester what changed, links the work item and keeps "
            f"the outcome so the next similar request starts from it (lab draft {number}).")},
    ]


#: The first words of the organization profile drafter's system prompt
#: (``app.services.organization_context_ai_draft._SYSTEM``): a request carrying them asks for proposals.
LAB_ORG_AI_DRAFT_SYSTEM = "You draft an organization profile for a security platform"

#: The profile ``seed_org_ai_draft_stub`` saves when none is: the proposed name then renders as a conflict
#: and the proposed sector, equal to this one, under "Unchanged (1)".
LAB_ORG_PROFILE = {"name": "Lab sample organization", "sector": "Financial services"}
#: The tech stack ``LAB_CTEM_KEV_TAILORED=on`` saves with ``LAB_ORG_PROFILE``: ``lab_asgi`` narrows the cyber KEV
#: count to it (``ctem_sample.KEV_TAILORED_COUNT``, the first three labels as ``kev_tailored_products``).
LAB_ORG_TECH_STACK = (
    {"vendor": "Microsoft", "product": "Exchange Server", "cpe": ""},
    {"vendor": "Fortinet", "product": "FortiOS", "cpe": ""},
    {"vendor": "Citrix", "product": "NetScaler ADC", "cpe": ""},
    {"vendor": "Atlassian", "product": "Confluence Data Center", "cpe": ""},
)
#: The profile ``LAB_ORG_CONTEXT=complete`` saves (#83): every field a CTEM stage reads
#: (``ctem_context_scoring.CONTEXT_FIELDS``) is filled, so the posture answers ``context.state`` ``complete``.
#: The PIR is the first of ``lab_asgi.LAB_OPENCTI_PIRS``, so the picker names it.
LAB_ORG_COMPLETE_PROFILE = {
    **LAB_ORG_PROFILE,
    "regions": ["FR", "US"],
    "crown_jewels": [{"name": "Payments platform", "business_service": "Card payments", "criticality": "critical"}],
    "tech_stack": [dict(entry) for entry in LAB_ORG_TECH_STACK],
    "ai_systems": [{"name": "Customer support assistant", "kind": "assistant", "exposure": "public"}],
    "pir_ids": ["5b1e0c3a-7d2f-4e61-9a10-000000000001"],
    "risk_appetite": {"approval_threshold": 50000},
    "ticketing": {"integration": "jira", "project": "SEC"},
}

#: ``LAB_CTEM_STALE`` values: ``on`` stores the CTEM briefing, stage assessments and cyber posture past their windows
#: (``seed_ctem_actions.py``, ``lab_asgi.stale_ctem_posture``) and the stub completes a regeneration (#12381).
LAB_CTEM_STALE_STATES = ("off", "on")
#: How long the stub holds a CTEM artifact answer by default, so the generating state shows before the run completes;
#: ``LAB_CTEM_REGEN_SECONDS`` (0 to 600) overrides it for a longer look at the regenerating state.
LAB_CTEM_ARTIFACT_HOLD_SECONDS = 8


def ctem_artifact_hold_seconds() -> int:
    """``LAB_CTEM_REGEN_SECONDS`` when it is a whole number from 0 to 600, else ``LAB_CTEM_ARTIFACT_HOLD_SECONDS``."""
    raw = os.environ.get("LAB_CTEM_REGEN_SECONDS", "").strip()
    return int(raw) if raw.isdigit() and int(raw) <= 600 else LAB_CTEM_ARTIFACT_HOLD_SECONDS


#: The analyst name in a CTEM stage persona (``builtin_agents._stage_persona``) to its stage key.
LAB_CTEM_STAGE_NAMES = {
    "Scoping": "scoping", "Discovery": "discovery", "Prioritization": "prioritization",
    "Validation": "validation", "Mobilization": "remediation",
}


def ctem_artifact_answer(request: dict) -> str | None:
    """The stub's answer to a CTEM briefing or stage analyst run under ``LAB_CTEM_STALE=on``, else ``None``.

    The analysts' personas open on ``You are the Exposure Briefing Analyst`` and ``You are the CTEM <Stage>
    Analyst``; their context carries ``Active CTEM vertical: <vertical>``. The answer is one JSON object in the
    analyst's output schema, authored for that vertical, so the run completes with an artifact the briefing and
    assessment GETs answer fresh: a regeneration completes in the lab (#12381). Every other lab is unchanged.
    """
    import re

    if lab_switch("LAB_CTEM_STALE", LAB_CTEM_STALE_STATES) != "on":
        return None
    texts: list[str] = []
    for message in request.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
        if isinstance(content, str):
            texts.append(content)
    text = "\n".join(texts)
    found = re.search(r"Active CTEM vertical: (\w+)", text)
    vertical = found.group(1) if found else "cyber"
    coverage = {"opencti": True, "openaev": True}
    if "You are the Exposure Briefing Analyst" in text:
        return json.dumps({
            "vertical": vertical, "headline": "Regenerated by the lab's stub model",
            "summary": "The lab's stub model regenerated this briefing.", "posture_trend": "stable",
            "coverage": coverage, "key_findings": [], "recommended_focus": "Review the regenerated briefing",
            "next_steps": [{"action": "Review the regenerated briefing", "rationale": "Lab stub", "priority": "low"}],
        })
    found = re.search(r"You are the CTEM (\w+) Analyst", text)
    stage = LAB_CTEM_STAGE_NAMES.get(found.group(1)) if found else None
    if stage is None:
        return None
    return json.dumps({
        "stage": stage, "vertical": vertical, "headline": f"Regenerated {stage} assessment",
        "assessment": "The lab's stub model regenerated this assessment.", "score_read": "fair",
        "key_metrics": [{"label": "Regenerated", "value": "lab", "trend": "flat"}],
        "recommended_action": f"Review the regenerated {stage} assessment", "coverage": coverage,
    })


def org_ai_draft_answer(request: dict) -> str | None:
    """The stub's answer to an organization profile AI draft request, or ``None`` for any other request.

    The drafter (``organization_context_ai_draft.build_messages``) sends its system prompt, then a user
    message opening on ``Source names you may cite:`` and carrying the current profile as JSON. The answer
    covers every review state of Draft with AI, citing ``description`` (else the first source given):
    the name (a conflict when the profile has one), the sector equal to the current one (answered unchanged,
    the dialog's "Unchanged (1)"), regions (high), size (medium), an inferred hourly rate (``check``), a tech
    stack entry (low) and a suggested PIR; every other field is left to ``not_supported``. Lab only: a production model never sees it.
    """
    messages = [m for m in request.get("messages") or [] if isinstance(m, dict)]
    system = next((m.get("content") for m in messages if m.get("role") == "system"), None)
    if not isinstance(system, str) or not system.startswith(LAB_ORG_AI_DRAFT_SYSTEM):
        return None
    user = last_user_text(request)
    head, _, _ = user.partition("\n")
    if not head.startswith("Source names you may cite:"):
        return None
    sources = [s.strip() for s in head.split(":", 1)[1].split(",") if s.strip()]
    if not sources:
        return "Lab stub model: no source was given, so nothing is proposed."
    source = "description" if "description" in sources else sources[0]
    _, _, rest = user.partition("Current profile (do not repeat an identical value):\n")
    try:
        current = json.JSONDecoder().raw_decode(rest)[0] if rest else {}
    except ValueError:
        current = {}
    current = current if isinstance(current, dict) else {}
    sector = str(current.get("sector") or LAB_ORG_PROFILE["sector"])
    proposals = [
        {"field": "name", "value": "Lab sample organization SA", "confidence": "high",
         "basis": "'Lab sample organization SA, a lab company'"},
        {"field": "sector", "value": sector, "confidence": "high", "basis": f"'{sector}'"},
        {"field": "regions", "value": ["FR", "DE"], "confidence": "high", "basis": "'offices in Paris and Berlin'"},
        {"field": "size", "value": "251-1000", "confidence": "medium", "basis": "'about 400 people'"},
        {"field": "loaded_hourly_rate", "value": 95, "confidence": "medium", "check": True,
         "basis": "inferred from the sector and the regions, not read in a source"},
        {"field": "tech_stack", "value": [{"vendor": "Microsoft", "product": "Exchange Online", "cpe": ""}],
         "confidence": "low", "basis": "'our mail runs in the cloud'"},
    ]
    return json.dumps({
        "proposals": [{**p, "source": source, "check": p.get("check", False)} for p in proposals],
        "not_supported": [],
        "suggested_pirs": ["Ransomware targeting financial services in Europe"],
    })


class StubModel(http.server.BaseHTTPRequestHandler):
    """An OpenAI-compatible model that answers one fixed sentence: the lab renders chat without a provider.

    A message ``lab:resuming ...`` scripts the turn instead (``parse_lab_script``): the stub announces
    it as resumed by the platform and holds it open before answering. ``lab:wait ...`` calls the real
    ``wait`` tool; ``lab:cut-wait ...`` answers, then journals a cut wait the platform resumes;
    ``lab:call <tool> [json]`` calls that tool by name and quotes its result; ``lab:echo`` streams back
    the text after its first line. A fleet
    documentation draft request gets a sections JSON (``doc_draft_answer``).
    """

    def log_message(self, *args) -> None:  # quiet
        return

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, chunks: list[dict]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps({'id': 'lab', 'object': 'chat.completion.chunk', 'model': 'lab-model', **chunk})}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def _stream_bursts(self, script: dict) -> None:
        """A ``lab:stream`` or ``lab:echo`` answer: each burst's chunks written and flushed together, then
        ``gap_ms`` of silence."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if script["script"] == "echo":
            pieces = echo_chunks(script["text"])
            script = {**script, "per_burst": LAB_ECHO_PER_BURST, "gap_ms": LAB_ECHO_GAP_MS,
                      "bursts": -(-len(pieces) // LAB_ECHO_PER_BURST)}
        else:
            pieces = stream_chunks(script)
        log(f"lab script {script['script']}: {script['bursts']} bursts of {script['per_burst']}, "
            f"{script['gap_ms']} ms apart, {sum(map(len, pieces))} characters")
        frame = {"id": "lab", "object": "chat.completion.chunk", "model": "lab-model"}
        for burst in range(script["bursts"]):
            batch = pieces[burst * script["per_burst"]:(burst + 1) * script["per_burst"]]
            data = "".join(
                f"data: {json.dumps({**frame, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': piece}, 'finish_reason': None}]})}\n\n"
                for piece in batch
            )
            self.wfile.write(data.encode())
            self.wfile.flush()
            time.sleep(script["gap_ms"] / 1000)
        done = {**frame, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        self.wfile.write(f"data: {json.dumps(done)}\n\ndata: [DONE]\n\n".encode())

    def _resumed_long_turn(self, request: dict, script: dict) -> None:
        """A held resumed turn of ``lab:restart-mid-turn``: one tool call first, then an answer a piece a second."""
        if restart_resumed_step(request) == "call":
            name, arguments = restart_resumed_call(offered_tools(request))
            log(f"lab script restart-mid-turn: the resumed turn calls {name}, then works {script['resumed']} s")
            completion = called_tool_completion(name, arguments)
            completion["choices"][0]["message"]["tool_calls"][0]["id"] = LAB_RESTART_RESUMED_CALL_ID
            if request.get("stream"):
                call = {"index": 0, **completion["choices"][0]["message"]["tool_calls"][0]}
                self._stream([
                    {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [call]}, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                ])
            else:
                self._send(completion)
            return
        pieces = restart_resumed_pieces(script)
        log(f"lab script restart-mid-turn: the resumed turn answers over {script['resumed']} s, "
            f"{len(script['steers'])} steer(s) folded in")
        if not request.get("stream"):
            time.sleep(script["resumed"])
            self._send({
                "id": "lab", "object": "chat.completion", "model": "lab-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(pieces)},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        frame = {"id": "lab", "object": "chat.completion.chunk", "model": "lab-model"}
        for piece in pieces:
            delta = {"role": "assistant", "content": piece}
            self.wfile.write(f"data: {json.dumps({**frame, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]})}\n\n".encode())
            self.wfile.flush()
            time.sleep(1)
        done = {**frame, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        self.wfile.write(f"data: {json.dumps(done)}\n\ndata: [DONE]\n\n".encode())

    def _hold_for_restart(self, script: dict) -> None:
        """Hold the turn's model call after the scripted tool call while the backend is stopped (``restart_backend``).

        The call stays open until the stopping backend's drain cuts the turn and the process exits; the answer
        written then reaches nobody. A restart already running answers at once.
        """
        refusal = restart_refusal()
        if refusal is not None:
            text = refusal
        else:
            exited = start_backend_restart(script["hold"])
            if exited is not None:
                exited.wait(LAB_BACKEND_GRACE_SECONDS + 60)
            text = "Lab script: the backend did not stop, so this turn answers without a restart."
        try:
            self._stream([
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ])
        except OSError:  # the stopped backend closed the call: expected
            pass

    def do_GET(self) -> None:
        self._send({"object": "list", "data": [{"id": "lab-model", "object": "model", "owned_by": "lab"}]})

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        text = "This is the lab's stub model: it answers this sentence to every message."
        script = parse_lab_script(last_user_text(request)) if request.get("stream") else unstreamed_script(request)
        drafted = doc_draft_answer(request, next(_DOC_DRAFTS))
        kind = "doc draft"
        if drafted is None:
            drafted, kind = org_ai_draft_answer(request), "org ai-draft"
        if drafted is None and LAB_UP.is_set():
            # After lab up: a boot fan-out run answers the plain sentence, so the stale seed stays the latest artifact.
            drafted, kind = ctem_artifact_answer(request), "ctem artifact"
            if drafted is not None:
                time.sleep(ctem_artifact_hold_seconds())
        if drafted is not None:
            log(f"lab {kind}: answered {len(drafted)} characters")
            text, script = drafted, None
        elif answers_cut_wait(request) and (parse_lab_script(last_user_text(request)) or {}).get("script") == "cut-wait":
            # The resumed turn finished the cut pause. Its call is not streamed and ends on the
            # person's message, appended again after the resume note: hold, so the analyzing state shows.
            log(f"lab script cut-wait: resumed turn answered after {LAB_CUT_WAIT_ANALYZE_HOLD_SECONDS} s")
            time.sleep(LAB_CUT_WAIT_ANALYZE_HOLD_SECONDS)
            text = ("Lab script: the resumed turn finished the cut wait, then held this answer "
                    f"{LAB_CUT_WAIT_ANALYZE_HOLD_SECONDS} s so the analyzing state shows.")
            script = None
        restart = restart_script(request, _RESTART["pending"]) if drafted is None else None
        if restart:
            # Read on every call of the turn, streamed or not: the resumed turn's call may not be streamed.
            script = None
            step = restart_step(request, _RESTART["pending"])
            if step == "resumed" and restart["resumed"]:
                self._resumed_long_turn(request, restart)
                return
            if step == "resumed":
                text = restart_resumed_answer()
            elif step == "stop":
                self._hold_for_restart(restart)
                return
            elif request.get("stream"):
                log("lab script restart-mid-turn: the answer starts with one tool call")
                self._stream(restart_start_chunks(offered_tools(request)))
                return
        if script and script["script"] == "call" and last_role(request) != "user":
            # The turn's next model call, after the scripted call's result: quote that result.
            text, script = called_tool_answer(request), None
        if script and script["script"] == "wait" and last_role(request) != "user":
            # The turn's next model call, after the wait's result: quote it, and say whether the pause ran.
            # The card itself was drawn at the call (the platform's wait_start), logged there.
            text, paused = wait_answer(request)
            log(f"lab script wait: {'pause ended' if paused else 'no pause: the tool answered at once, no card'}")
            script = None
        if script and script["script"] in ("cut-wait", "fleet-pause", "fleet-raise") and last_role(request) != "user":
            # The turn's next model call, after the scripted call's result: the plain answer.
            script = None
        if script and script["script"] == "echo" and "error" in script:
            text, script = f"Lab script: {script['error']}.", None
        if script and script["script"] in ("stream", "echo"):
            self._stream_bursts(script)
            return
        if script and script["script"] == "wait":
            if "wait" in offered_tools(request):
                watched = lab_watched_run(lab_running_run(), lab_run_status)
                log(f"lab script wait: {script['seconds']} s, reason {script['reason']!r}, until {watched or 'nothing'}")
                self._stream(wait_call_chunks(script, watched))
                # The agent loop runs the call as soon as the stream ends and emits wait_start once its grant
                # is settled: the card draws now, labelled with the reason alone and a countdown.
                log(f"lab script wait: card drawn ({script['reason']!r}, {script['seconds']} s countdown)")
                return
            text = "Lab script: this turn was not offered the wait tool, so nothing was waited."
        elif script and script["script"] in ("fleet-pause", "fleet-raise"):
            fleet = bound_fleet(request)
            call = fleet_call(script, fleet) if fleet else None
            if call:
                # Called even when a deferred tool group hides its schema: the executor runs the platform's own.
                offered = "offered" if call[0] in offered_tools(request) else "not in the offered schemas"
                log(f"seeded fleet chat cards: {script['script']} calls {call[0]} {json.dumps(call[1])} ({offered})")
                self._stream(fleet_call_chunks(*call))
                return
            text = ("Lab script: this chat is not bound to a fleet, or the fleet is already autonomous, "
                    "so no fleet tool was called.")
        elif script and script["script"] == "call":
            if "error" in script:
                text = f"Lab script: nothing was called, {script['error']}."
            else:
                # Called even when the schemas do not offer it: the executor answers a withheld tool's refusal.
                offered = "offered" if script["tool"] in offered_tools(request) else "not in the offered schemas"
                log(f"lab script call: {script['tool']} {json.dumps(script['arguments'])} ({offered})")
                if request.get("stream"):
                    self._stream(fleet_call_chunks(script["tool"], script["arguments"]))
                else:
                    self._send(called_tool_completion(script["tool"], script["arguments"]))
                return
        elif script and script["script"] == "cut-wait":
            try:
                conversations = turns_in_flight()
            except Exception as exc:  # noqa: BLE001 - a script that cannot reach redis answers normally
                log(f"lab script cut-wait: not journaled ({exc})")
                conversations = []
            for conversation in conversations:
                threading.Thread(target=journal_cut_wait, args=(conversation, script["seconds"]), daemon=True).start()
            text = (f"Lab script: once this answer is stored, a {script['seconds']} s wait cut by a worker stop is "
                    "journaled and the turn is queued for its automatic resume.")
        elif script:
            cuts: dict = {}
            try:
                reached = announce_resuming(script, store_cut_reply(script, cuts) if script.get("cut") else None)
            except Exception as exc:  # noqa: BLE001 - a script that cannot reach redis answers normally
                log(f"lab script {script['script']}: not announced ({exc})")
                reached = []
            for conversation, (after, cut_at) in cuts.items():
                threading.Thread(
                    target=mark_resumed_answer, args=(conversation, script, after, cut_at), daemon=True,
                ).start()
            if reached:
                log(f"lab script resuming: {', '.join(reached)} held {script['seconds']} s")
                time.sleep(script["seconds"])
                text = (f"Lab script: this turn was announced as resumed ({script['resume']} of {script['of']}, "
                        f"cause {script['cause']}) and held {script['seconds']} s.")
        if request.get("stream"):
            self._stream([
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ])
            return
        self._send({
            "id": "lab", "object": "chat.completion", "model": "lab-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


#: The chat scripts the stub model answers (``parse_lab_script``), named in its start log.
LAB_CHAT_SCRIPTS = (
    "lab:resuming", "lab:wait", "lab:cut-wait", "lab:fleet-pause", "lab:fleet-raise",
    "lab:stream [bursts] [gap_ms] [per_burst]",
    "lab:echo + new line + text",
    "lab:call <tool_name> [json args]",
    "lab:restart-mid-turn [hold seconds] [resumed seconds]",
)


def start_model() -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", LLM_PORT), StubModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"stub model on 127.0.0.1:{LLM_PORT}, chat scripts: {', '.join(LAB_CHAT_SCRIPTS)}")


def live_model() -> tuple[str, str, str] | None:
    """``(key, base URL, model names)`` of the platform's LLM proxy when the declaration opts in, else ``None``.

    ``LAB_MODEL = "live"`` in the service's env asks for it; the platform then hands the pod a short-lived key of
    its own LLM proxy (``LAB_MODEL_API_KEY``, ``LAB_MODEL_BASE_URL``, ``LAB_MODEL_NAMES``, minted per lab by
    ``services/sandbox_workspace/model_keys.py``). No provider credential is ever in the pod. Opted in without a
    key (the platform not configured): the stub model, said in the log.
    """
    if os.environ.get("LAB_MODEL", "").strip().lower() != "live":
        return None
    key, url = os.environ.get("LAB_MODEL_API_KEY", ""), os.environ.get("LAB_MODEL_BASE_URL", "").rstrip("/")
    names = os.environ.get("LAB_MODEL_NAMES", "").strip()
    if not (key and url and names):
        log("live model: asked (LAB_MODEL=live) but the platform handed no key, URL or model names; stub model")
        return None
    return key, url, names


def model_env() -> dict[str, str]:
    """The backend's custom OpenAI provider: the live proxy when :func:`live_model` gives one, else the stub."""
    live = live_model()
    if live is None:
        return {
            "LLM_PROVIDER_CUSTOM_OPENAI_API_KEY": f"none||http://127.0.0.1:{LLM_PORT}/v1",
            "LLM_PROVIDER_CUSTOM_OPENAI_MODELS": "lab-model",
        }
    key, url, names = live
    log(f"live model: the platform's LLM proxy at {url}, models {names}")
    return {"LLM_PROVIDER_CUSTOM_OPENAI_API_KEY": f"{key}||{url}", "LLM_PROVIDER_CUSTOM_OPENAI_MODELS": names}


def backend_env() -> dict[str, str]:
    return {
        **stores.environment(),
        "S3_BUCKET": "copilot-files",
        "BASE_URL": f"http://localhost:{WEB_PORT}",
        "FRONTEND_URL": f"http://localhost:{WEB_PORT}",
        **model_env(),
        "TELEMETRY_ENABLED": "false",
        "LOG_FORMAT": "text",
        # The seed writes ~1,000 info lines at every start; a lab's reader needs warnings and errors.
        "LOG_LEVEL": "warning",
        "DB_POOL_SIZE": "5",
        "DB_MAX_OVERFLOW": "5",
        # With the lab's Enterprise Edition licence the Terms of Use gate applies and refuses
        # every API call (403) until an administrator accepts: a throwaway instance records
        # no consent, so the gate is off unless the declaration's env turns it on.
        "TERMS_OF_USE_ENABLED": os.environ.get("TERMS_OF_USE_ENABLED", "false"),
        # The headless Chromium of the presentation export loads the deck's render
        # page from the lab's own Vite server, by the IPv4 loopback Vite listens on
        # (``localhost`` may resolve to ::1, where nothing answers).
        "PRESENTATION_RENDER_BASE_URL": f"http://127.0.0.1:{WEB_PORT}",
        # A token of the lab's own, never the published default, so ``seed_suite_registrations``
        # runs the whole registration path (the default withholds the licence and skips the intent sync).
        "PLATFORM_REGISTRATION_TOKEN": LAB_REGISTRATION_TOKEN,
        # The GitHub integration ``seed_github_integration`` creates with this id: ``lab_asgi`` answers
        # its repository elements itself, so its Webhooks card lists a repository with nothing sent to GitHub.
        "LAB_GITHUB_INTEGRATION_ID": LAB_GITHUB_INTEGRATION_ID,
        # The Microsoft integration of the saveable agent account: ``lab_asgi`` answers its pickers.
        "LAB_AGENT_ACCOUNT_INTEGRATION_ID": LAB_AGENT_ACCOUNT_INTEGRATION_ID,
        # ``lab_asgi`` serves ``/office/`` from the task pane ``build_office_addin`` writes (``LAB_OFFICE_ADDIN``).
        **({"LAB_OFFICE_ADDIN_DIST": str(OFFICE_ADDIN_DIST)} if office_addin_enabled() else {}),
        # The Slack channel bot ``seed_channel_bot`` creates with this token: ``lab_asgi`` answers its channel list.
        "LAB_SLACK_BOT_TOKEN": LAB_SLACK_BOT_TOKEN,
        # The Teams channel bot ``seed_teams_channel_bot`` creates with this client id: ``lab_asgi`` answers its
        # channel list, so the Teams ChannelPickers list channels with nothing sent to Microsoft.
        "LAB_TEAMS_BOT_CLIENT_ID": str(LAB_TEAMS_BOT_BODY["client_id"]),
        # The OpenRouter and LM Studio integrations ``seed_model_catalog_integrations`` creates with this key and this
        # server URL: ``lab_asgi`` answers their model catalogs, so both Models tabs list rows with no outbound call.
        "LAB_OPENROUTER_API_KEY": LAB_OPENROUTER_API_KEY,
        # Its management key: ``lab_asgi`` answers its credits and 30 days of activity, so the Usage tab renders.
        "LAB_OPENROUTER_MANAGEMENT_KEY": LAB_OPENROUTER_MANAGEMENT_KEY,
        "LAB_LMSTUDIO_BASE_URL": LAB_LMSTUDIO_BASE_URL,
        # ``lab_asgi`` makes the trigger provisioning pass a no-op: nothing answers OpenCTI or OpenAEV here,
        # so the states ``seed_provisioning_states`` writes stay as written instead of turning into errors.
        "LAB_FREEZE_TRIGGER_PROVISIONING": "1",
        # ``lab_asgi`` answers the OpenCTI Organization search from its fixture (``lab:error`` reaches the real 409);
        # a declaration's env sets ``0`` to leave every search to the real caller.
        "LAB_OPENCTI_IDENTITIES": os.environ.get("LAB_OPENCTI_IDENTITIES", "1"),
        # ``lab_asgi`` answers the PIR picker's list from its fixture (a saved ``lab:pirs-error`` id answers
        # ``unreadable``); a declaration's env sets ``0`` to leave the list to the real OpenCTI caller.
        "LAB_OPENCTI_PIRS": os.environ.get("LAB_OPENCTI_PIRS", "1"),
        # ``lab_asgi`` refuses Draft with AI with the route's 429 when the description starts ``lab:quota``.
        "LAB_ORG_AI_DRAFT": os.environ.get("LAB_ORG_AI_DRAFT", "1"),
        # ``lab_asgi`` answers Check against OpenCRQ with a deterministic plan (Apply writes nothing).
        "LAB_OPENCRQ_SYNC": os.environ.get("LAB_OPENCRQ_SYNC", "1"),
        # ``lab_asgi`` answers the organization import (preview, apply, sync runs) from its fixture for a platform
        # the real path answers "not connected" (#65); a live OpenCTI keeps its real answers.
        "LAB_ORG_IMPORT": os.environ.get("LAB_ORG_IMPORT", "1"),
        # ``lab_asgi`` answers the Nova engine status as available, so the Engine models card renders (no model).
        "LAB_NOVA_ENGINE": os.environ.get("LAB_NOVA_ENGINE", "1"),
        # With it, an empty Nova semantic override is answered as ignored, so the second calibration line renders.
        "LAB_NOVA_OVERRIDE_IGNORED": os.environ.get("LAB_NOVA_OVERRIDE_IGNORED", "1"),
        # ``LAB_STALE_BUILD=on``: ``lab_asgi`` reports another frontend build than the page's on ``/auth/config`` (#63).
        "LAB_STALE_BUILD_SERVER_ID": stale_build_ids()[1],
        # ``lab_asgi`` keeps the seeded today row of the CTEM posture history over the live (empty) collection.
        "LAB_CTEM_POSTURE_HISTORY": "1" if seeds_ctem_posture_history() else "0",
        # The stale-embedding knowledge bases ``seed_kb_stale_embeddings`` writes stay stale: the background
        # remediation sweeper would re-embed them away; ``LAB_KB_STALE=0`` leaves the platform default.
        **({"EMBEDDING_STALE_AUTO_REMEDIATION_ENABLED": "false"} if seeds_kb_stale_embeddings() else {}),
        # The internal AI catalog (/catalog/*): off by default in production, on in every lab so its list and
        # detail pages render on the rows ``seed_public_catalog`` publishes; a declaration's env sets ``false``.
        "AI_CATALOG_ENABLED": lab_ai_catalog_env(),
        # A platform cost ceiling, so the non-admin peer meets the blocked-limit states of the Budget tab and
        # the fleet Run defaults on real rows (``seed_budget_ceiling``); a declaration's env sets ``0`` for none.
        "AGENT_LOOP_MAX_COST_USD": os.environ.get("AGENT_LOOP_MAX_COST_USD", LAB_BUDGET_CEILING_USD),
        # The decisions read switch (off by default in production): on in every lab, so ``GET /decisions/waiting``
        # answers the open decision ``seed_open_decision`` writes; a declaration's env sets ``false``.
        "DECISIONS_READ_ENABLED": os.environ.get("DECISIONS_READ_ENABLED", "true"),
        **edition_env(),
        **langfuse_env(),
        **smtp_env(),
    }


def runs_worker() -> bool:
    """Whether this lab runs the SAQ worker beside the backend (``LAB_WORKER = "1"`` in the declaration's env).

    The worker's startup starts the orchestration reconciler (``backend/app/worker.py``), which claims every
    durable execution: a delegated background task (``spawn_background_task``), an assignment run, an
    evaluation. Without it such a task stays queued for the lab's whole life (#11023). Opt-in: the default labs
    keep their seeded runs as written, and the worker's model calls cost a live lab's quota.
    """
    return os.environ.get("LAB_WORKER", "").strip().lower() in ("1", "true", "yes")


def worker_argv() -> list[str]:
    """The worker as ``docker-compose.dev.yml`` runs it (``saq app.worker.settings``), from the image's venv."""
    return [sys.executable, "-m", "saq", "app.worker.settings"]


def seeds_ctem_posture_history() -> bool:
    """Whether this lab records the 90 days of CTEM posture history: XTM One labs outside the CTEM sample ones."""
    return (
        os.environ.get("PLATFORM_MODE", "") in ("xtm_one", "ai_disabled")
        and os.environ.get("LAB_CTEM_SAMPLE") != "1"
        and lab_switch("LAB_CTEM_SAMPLE_STATE", LAB_CTEM_SAMPLE_STATES) == "off"
    )


#: ``LAB_EDITION`` values: ``community`` starts the backend with no Enterprise Edition licence.
LAB_EDITIONS = ("enterprise", "community")
#: ``LAB_LICENSE_SOURCE`` values: ``database`` stores the licence the platform hands the lab in the database
#: (``PUT /settings/platform``) instead of handing it to the backend as ``ENTERPRISE_LICENSE``.
LAB_LICENSE_SOURCES = ("env", "database")
#: ``LAB_DEFAULT_MODEL`` values: ``none`` clears the platform's default model once the rows are seeded.
LAB_DEFAULT_MODELS = ("seeded", "none")
#: ``LAB_AI`` values: ``off`` switches the platform's runtime AI toggle off once every row is seeded.
LAB_AI_STATES = ("on", "off")
#: ``LAB_PEER_GROUP_AI`` values: ``off`` puts the peer in a group whose AI is off once every row is seeded.
LAB_PEER_GROUP_AI_STATES = ("on", "off")
#: ``LAB_CTEM_SAMPLE_STATE`` values: ``on`` renders the CTEM sample state (no product, no briefing run).
LAB_CTEM_SAMPLE_STATES = ("off", "on")
#: ``LAB_ORG_SETUP_PROMPT`` values: ``on`` leaves the organization profile unsaved, so the first-login setup
#: proposal (#9173 S3, ``GET /organization-context/setup-prompt``) answers ``propose: true`` to the administrator.
LAB_ORG_SETUP_PROMPT_STATES = ("off", "on")
#: ``LAB_ORG_CONTEXT`` values (#83): ``default`` keeps the lab profile and logs nothing; ``missing`` saves no
#: profile and answers the first-login setup proposal ``dismissed`` (no dialog over the screens); ``partial`` keeps
#: the lab profile (name and sector); ``complete`` saves ``LAB_ORG_COMPLETE_PROFILE``. Each but ``default`` is
#: read back as ``context.state`` of ``GET /ctem-metrics/posture`` (``seed_org_context``).
LAB_ORG_CONTEXT_STATES = ("default", "missing", "partial", "complete")
#: ``LAB_ORG_SYNCED`` values (#1345): ``on`` saves the profile with rows a platform sync wrote next to typed rows
#: (``seed_org_synced``), so the Source column, locked rows, bulk Set and the synced-removal dialog render natively.
LAB_ORG_SYNCED_STATES = ("off", "on")
#: The rows ``LAB_ORG_SYNCED=on`` saves, per section: the platform whose disabled sync writes them (a source of
#: ``organization_context_import.SECTION_SOURCES``) and the rows, synced ones carrying an ``external_id``; a row
#: without one is typed. Each synced row gets the ``sync_id`` of its section's sync at seed time.
LAB_ORG_SYNCED_ROWS = {
    "crown_jewels": ("opencrq", (
        {"name": "Payments platform", "business_service": "Card payments", "criticality": "critical",
         "external_id": "lab-opencrq-asset-1"},
        {"name": "Customer data lake", "criticality": "high", "external_id": "lab-opencrq-asset-2"},
        {"name": "Internal wiki", "criticality": "low"},
    )),
    "tech_stack": ("openaev", (
        {"vendor": "Microsoft", "product": "Exchange Server", "external_id": "lab-openaev-software-1"},
        {"vendor": "Fortinet", "product": "FortiOS", "external_id": "lab-openaev-software-2"},
        {"vendor": "Atlassian", "product": "Confluence Data Center"},
    )),
    "ai_systems": ("openaev", (
        {"name": "Customer support assistant", "kind": "assistant", "exposure": "public",
         "external_id": "lab-openaev-ai-1"},
        {"name": "Code review agent", "kind": "agent", "exposure": "internal"},
    )),
}
#: ``LAB_PERF_THREAD`` values: ``on`` seeds a 200-message conversation and a short one for the administrator
#: (``seed_perf_thread.py``), so long-thread scroll, open and switch timings need no hand-edited seed.
LAB_PERF_THREAD_STATES = ("off", "on")
#: ``LAB_STALE_BUILD`` values: ``on`` serves the page one frontend build and reports another on ``/auth/config``,
#: so the "A new version is available" notice (``useStaleBuildNotice``) renders natively (#63).
LAB_STALE_BUILD_STATES = ("off", "on")
#: The build the Vite dev server writes in ``<meta name="build-id">`` under ``LAB_STALE_BUILD=on`` (``vite.config.ts``).
LAB_STALE_PAGE_BUILD_ID = "1ab0000000000001"
#: The other build ``lab_asgi`` answers as ``build_id`` of ``GET /api/v1/auth/config`` under the same switch.
LAB_STALE_SERVER_BUILD_ID = "1ab0000000000002"
#: ``LAB_CTEM_FINDINGS`` values: ``on`` answers the CTEM findings the rule-based Recommended actions act on.
LAB_CTEM_FINDINGS_STATES = ("off", "on")
#: The OpenAEV expectation types ``LAB_CTEM_FINDINGS=on`` fails (``ctem_sample.FINDINGS_EXPECTATION_BUCKETS``).
LAB_CTEM_FINDINGS_TYPES = ("PREVENTION", "DETECTION", "VULNERABILITY", "HUMAN_RESPONSE")
#: ``LAB_CTEM_ACTIONS`` values: ``on`` stores a ready assessment per stage and a ready briefing (``seed_ctem_actions.py``).
LAB_CTEM_ACTIONS_STATES = ("off", "on")
#: The CTEM stages ``LAB_CTEM_ACTIONS=on`` stores an assessment for (``seed_ctem_actions.STAGE_ACTIONS``).
LAB_CTEM_ACTIONS_STAGES = ("scoping", "discovery", "prioritization", "validation", "remediation")
#: ``LAB_CTEM_KEV_TAILORED`` values: ``on`` saves ``LAB_ORG_TECH_STACK`` with the profile, so the cyber posture
#: answers ``kev_tailored_count`` and ``kev_tailored_products`` (``lab_asgi.tailored_ctem_posture``).
LAB_CTEM_KEV_TAILORED_STATES = ("off", "on")
#: ``LAB_CTEM_FLEET_OPTIN`` values: ``mixed`` seeds one CTEM fleet on, one partially on, one paused, the others
#: not set up, and a non-zero run estimate on the one that is on; ``off`` switches every CTEM fleet automation off
#: and reads it back (every CTEM fleet not set up, the invitation; #9703).
LAB_CTEM_FLEET_OPTIN_STATES = ("mixed", "off")
#: ``LAB_AI_ADOPTION_SCALE`` values: ``large`` seeds AI Adoption with figures of 1,000 and more
#: (``seed_ai_adoption_large.py``), so the K compaction renders on real answers; the default lab keeps its small data.
LAB_AI_ADOPTION_SCALES = ("small", "large")
#: ``LAB_FIGURES_SCALE`` values: ``large`` seeds three fleets with quota estimates of 1,000 and more, 1,000 and more
#: fleet runs, and home counts of 1,000 and more (``seed_large_figures.py``, #10734), so the K compaction of the
#: fleet About tiles and the Copilot home KPI cards renders on real answers; the default lab keeps its small data.
LAB_FIGURES_SCALES = ("small", "large")
#: ``LAB_CHAT_LAYOUT`` values: ``on`` seeds the chat-layout rows (``seed_chat_layout.py``, #10910): 10 agents
#: with distinct icons, 10 workspaces, 44 conversations over Today to older, and a 32-turn Markdown thread.
LAB_CHAT_LAYOUT_STATES = ("off", "on")
#: ``LAB_CTEM_SCREENS`` values: ``on`` answers five scored CTEM stages, a live funnel and the CTEM Cases source
#: read from ``ctem_sample`` (``lab_asgi``), and runs the real recompute, so 3 cases and more exist (#11713).
LAB_CTEM_SCREENS_STATES = ("off", "on")
#: ``LAB_CTEM_ACTIVITY`` values: ``on`` seeds every source of the CTEM deterministic activity feed, with no run.
LAB_CTEM_ACTIVITY_STATES = ("off", "on")
#: The registration ``LAB_CTEM_ACTIVITY=on`` leaves silent (``seed_ctem_activity.py``): "stopped responding".
LAB_CTEM_ACTIVITY_SILENT = "opencrq"
#: The group ``LAB_PEER_GROUP_AI=off`` creates: lab-only, holds the peer alone.
LAB_AI_OFF_GROUP = "Lab group with AI off"
#: ``LAB_LANGFUSE`` values: ``stored`` saves lab-only Langfuse keys through the API (the stored-keys state of
#: Settings > Usage and quotas > Observability), ``env`` hands them to the backend as ``LANGFUSE_*`` (the
#: env-managed lock), ``off`` leaves Langfuse not configured.
LAB_LANGFUSE_STATES = ("stored", "env", "off")
#: The lab's Langfuse keys: never valid on any Langfuse.
LAB_LANGFUSE_PUBLIC_KEY = "pk-lf-lab-only-never-used-outside-the-lab"
LAB_LANGFUSE_SECRET_KEY = "sk-lf-lab-only-never-used-outside-the-lab"  # noqa: S105 - lab-only value
#: Its host: the discard port of the loopback, so a tracing client the backend builds is refused at once and
#: nothing leaves the lab (the client then waits its cooldown before trying again).
LAB_LANGFUSE_HOST = "http://127.0.0.1:9"
#: ``LAB_SMTP`` values: ``off`` leaves SMTP unconfigured (every lab), ``on`` hands the backend lab-only ``SMTP_*``
#: values, so ``is_email_configured()`` answers true and the screens that need an SMTP server render (#1542).
LAB_SMTP_STATES = ("off", "on")
#: The lab's SMTP server: the discard port of the loopback, so a send is refused at connect, the caller's failure
#: path runs and no mail leaves the lab.
LAB_SMTP_HOSTNAME = "127.0.0.1"
LAB_SMTP_PORT = "9"
LAB_SMTP_FROM = "lab-only-sender@example.invalid"


def lab_switch(name: str, values: tuple[str, ...]) -> str:
    """The value of one lab switch, its first value when unset; an unknown value is logged and ignored."""
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return values[0]
    if value not in values:
        log(f"{name}={value!r} ignored: one of {', '.join(values)}")
        return values[0]
    return value


def langfuse_env() -> dict[str, str]:
    """``LAB_LANGFUSE=env``: the lab's Langfuse keys as ``LANGFUSE_*``, so the backend's boot seeds the row from them.

    ``main._seed_langfuse_from_env`` writes the configuration at every start when both keys are set, and
    ``GET /settings/langfuse`` answers ``is_from_env`` true: the Observability card renders its env lock on a
    real answer. Every other value leaves the backend's environment as it is. Lab-only.
    """
    if lab_switch("LAB_LANGFUSE", LAB_LANGFUSE_STATES) != "env":
        return {}
    return {
        "LANGFUSE_PUBLIC_KEY": LAB_LANGFUSE_PUBLIC_KEY,
        "LANGFUSE_SECRET_KEY": LAB_LANGFUSE_SECRET_KEY,
        "LANGFUSE_HOST": LAB_LANGFUSE_HOST,
    }


def smtp_env() -> dict[str, str]:
    """``LAB_SMTP=on``: lab-only ``SMTP_*`` values, so the backend answers SMTP configured (#1542).

    The host is the loopback's discard port: ``is_email_configured()`` answers true, the Platform email
    integration and the send buttons are offered, and a send is refused at connect (no mail leaves the lab).
    ``off`` (the default) leaves the backend's environment as it is. Lab-only.
    """
    if lab_switch("LAB_SMTP", LAB_SMTP_STATES) != "on":
        return {}
    return {
        "SMTP_HOSTNAME": LAB_SMTP_HOSTNAME,
        "SMTP_PORT": LAB_SMTP_PORT,
        "SMTP_FROM": LAB_SMTP_FROM,
        "SMTP_STARTTLS": "false",
        "SMTP_USE_TLS": "false",
    }


def edition_env() -> dict[str, str]:
    """``LAB_EDITION=community``: the licence the platform hands the lab is blanked for the backend.

    The XTM One edition is Enterprise when a valid Filigran licence is installed or a connected
    platform is Enterprise Edition (``resolve_deployment_edition``). A fresh lab database holds no
    licence and the seeded Suite registrations carry none, so with ``ENTERPRISE_LICENSE`` empty the
    edition resolves to community: the home's first-visit splash (``CtemTrialSplash``) renders on
    real data. Lab-only: nothing in the product changes.
    """
    if lab_switch("LAB_EDITION", LAB_EDITIONS) == "community" or stores_database_licence():
        return {"ENTERPRISE_LICENSE": ""}
    return {}


def stores_database_licence() -> bool:
    """``LAB_LICENSE_SOURCE=database`` on an Enterprise lab: the licence goes to the database, not the env."""
    return (
        lab_switch("LAB_LICENSE_SOURCE", LAB_LICENSE_SOURCES) == "database"
        and lab_switch("LAB_EDITION", LAB_EDITIONS) == "enterprise"
    )


def seed_database_licence(token: str, base: str) -> dict[str, str]:
    """``LAB_LICENSE_SOURCE=database``: install the lab's licence in the database, read it back; raises on a refusal.

    The backend starts with ``ENTERPRISE_LICENSE`` blanked (:func:`edition_env`); the PEM the platform hands the
    lab process is saved through ``PUT /settings/platform`` (``enterprise_license``, the Update License path of
    Settings > Platform), then ``/platform/config`` must answer ``xtm_license.license_source`` ``database`` and
    ``GET /settings/platform`` ``has_db_license`` true and ``has_env_license`` false: the Enterprise License card
    renders Update License and Remove License on real data, and Remove reaches the no-licence card. Runs first,
    before every seed an Enterprise Edition gate would refuse. Lab-only: nothing in the product changes.
    """
    pem = os.environ.get("ENTERPRISE_LICENSE", "").strip()
    if not pem:
        raise RuntimeError("the platform handed this lab no ENTERPRISE_LICENSE to store in the database")
    api("PUT", "/settings/platform", token, {"enterprise_license": pem})
    config = api("GET", "/platform/config")
    xtm = config.get("xtm_license") if isinstance(config, dict) else None
    source = xtm.get("license_source") if isinstance(xtm, dict) else None
    if source != "database":
        raise RuntimeError(f"/platform/config answers license_source {source!r}, not 'database'")
    platform = api("GET", "/settings/platform", token)
    if not isinstance(platform, dict) or not platform.get("has_db_license") or platform.get("has_env_license"):
        raise RuntimeError("GET /settings/platform does not answer has_db_license true and has_env_license false")
    return {"id": "database", "url": f"{base}/settings/platform"}


#: The lab backend's registration token: throwaway, valid only inside the lab.
LAB_REGISTRATION_TOKEN = "lab-only-registration-token-never-used-outside-the-lab"  # noqa: S105 - lab-only value
#: The id of the lab's GitHub integration, chosen before the backend starts (``backend_env``).
LAB_GITHUB_INTEGRATION_ID = str(uuid.uuid4())
#: Its token: lab-only, never valid on GitHub; the lab's backend never sends it anywhere the seed reads.
LAB_GITHUB_TOKEN = "lab-only-github-token-never-used-outside-the-lab"  # noqa: S105 - lab-only value
#: The id of the Microsoft integration ``seed_agent_account_activity`` binds to the saveable agent account, chosen
#: before the backend starts (``backend_env``): ``lab_asgi`` answers that integration's identity, conversation and
#: people pickers itself, so the account's Edit dialog saves with nothing sent to Microsoft.
LAB_AGENT_ACCOUNT_INTEGRATION_ID = str(uuid.uuid4())
#: The token of the Slack channel bot ``seed_channel_bot`` creates: ``lab_asgi`` answers that bot's channel list.
LAB_SLACK_BOT_TOKEN = "xoxb-lab-only-slack-token-never-used-outside-the-lab"  # noqa: S105 - lab-only value
#: The credentials of the Microsoft Teams channel bot ``seed_teams_channel_bot`` creates: lab-only, never sent to
#: Microsoft (the channels page lists bots without an outbound call; ``lab_asgi`` answers its channel list by this
#: client id). Teams carries ``client_id`` and an extra field, so its Edit credentials form renders the
#: keep-current-values notes Slack never shows (#9822).
LAB_TEAMS_BOT_BODY: dict[str, object] = {
    "provider": "teams_bot",
    "client_id": "lab-only-teams-client-id",
    "client_secret": "lab-only-teams-client-secret",
    "extra_config": {"tenant_id": "lab-only-teams-tenant-id"},
}
#: The API key of the OpenRouter integration ``seed_model_catalog_integrations`` creates: lab-only, never sent to
#: OpenRouter (``lab_asgi`` answers its key info, catalogs and model endpoints).
LAB_OPENROUTER_API_KEY = "sk-or-lab-only-key-never-used-outside-the-lab"
#: Its management key (``extra_config.management_key``): lab-only too, ``lab_asgi`` answers its credits and activity.
LAB_OPENROUTER_MANAGEMENT_KEY = "sk-or-lab-only-management-key-never-used-outside-the-lab"
#: The server URL of the LM Studio integration it creates: a ``.invalid`` host nothing resolves, whose model listing
#: ``lab_asgi`` answers.
LAB_LMSTUDIO_BASE_URL = "http://lmstudio.lab.invalid:1234"


def has_renderer() -> bool:
    """Whether the image carries Playwright's headless Chromium (python-dev)."""
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    return browsers.is_dir() and any(browsers.glob("chromium*"))


def api(method: str, path: str, token: str | None = None, body: dict | None = None) -> dict | list:
    """One JSON call to the lab's backend; an HTTP error is raised with its status and detail.

    *token* is sent as the Bearer: a user's access token, or the registration token.
    """
    request = urllib.request.Request(
        f"http://127.0.0.1:{API_PORT}/api/v1{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path}: {exc.code} {exc.read()[:200].decode('utf-8', 'replace')}") from exc


#: (entity, frontend route, how to create it). Each creator gets the token and the rows
#: created so far and answers the new row's id; it may raise, the entity is then skipped.
def _created(answer: dict | list) -> str:
    if not isinstance(answer, dict) or not answer.get("id"):
        raise RuntimeError(f"no id in the answer: {str(answer)[:120]}")
    return str(answer["id"])


def _first(token: str, path: str) -> str:
    rows = api("GET", path, token)
    rows = rows.get("items", []) if isinstance(rows, dict) else rows
    if not rows:
        raise RuntimeError(f"GET {path}: no row")
    return str(rows[0]["id"])


def _builtin_pack(token: str, _: dict) -> str:
    builtin = api("GET", "/investigation-packs/builtin", token)
    if not builtin:
        raise RuntimeError("no built-in investigation pack to copy")
    return _created(api("POST", f"/investigation-packs/from-builtin/{builtin[0]['slug']}", token))


#: The seeded fleet: an orchestrator over three members, the first member
#: handing back to the orchestrator, so the Graph tab draws a tree and a reciprocal edge.
FLEET_MEMBERS = ("Researcher", "Analyst", "Writer")
#: The seeded agents by name (fleet members, orchestrator, handover pair), for ``seed_handovers``.
LAB_AGENTS: dict[str, str] = {}


def _seed_agent(token: str, name: str, handovers: dict[str, str] | None = None) -> str:
    handovers = handovers or {}
    return _created(api("POST", "/agents", token, {
        "name": f"{SEED_NAME} {name}", "description": "Seeded by the lab.",
        "persona": f"You are the lab's {name.lower()}.",
        "handover_agent_ids": list(handovers), "handover_types": handovers,
    }))


def _seed_fleet(token: str, _: dict) -> str:
    """One fleet: an orchestrator delegating to three members, the first consulting it back."""
    members = [_seed_agent(token, name) for name in FLEET_MEMBERS]
    orchestrator = _seed_agent(token, "Orchestrator", {member: "delegate" for member in members})
    LAB_AGENTS.update(zip(FLEET_MEMBERS, members, strict=True), Orchestrator=orchestrator)
    api("PUT", f"/agents/{members[0]}", token, {
        "handover_agent_ids": [orchestrator], "handover_types": {orchestrator: "consult"},
    })
    fleet = _created(api("POST", "/fleets", token, {
        "name": f"{SEED_NAME} fleet", "description": "Seeded by the lab: one orchestrator, three members.",
    }))
    root = _created(api("POST", f"/fleets/{fleet}/members", token, {
        "agent_id": orchestrator, "role_label": "Orchestrator",
    }))
    api("PUT", f"/fleets/{fleet}", token, {"orchestrator_agent_id": orchestrator})
    for name, member in zip(FLEET_MEMBERS, members, strict=True):
        api("POST", f"/fleets/{fleet}/members", token, {
            "agent_id": member, "parent_member_id": root, "role_label": name,
        })
    return fleet


SEED_NAME = "Lab sample"
SEEDS: list[tuple[str, str, object]] = [
    ("agent", "/agents/{id}", lambda token, _: _first(token, "/agents")),
    ("custom tool", "/tools/{id}", lambda token, _: _created(api("POST", "/custom-tools", token, {
        "name": f"{SEED_NAME} tool", "description": "Seeded by the lab.",
        "js_function": "async function run(input) { return { ok: true }; }",
    }))),
    ("template", "/templates/{id}", lambda token, _: _created(api("POST", "/template-containers", token, {
        "name": f"{SEED_NAME} template", "description": "Seeded by the lab.",
    }))),
    ("investigation pack", "/investigation-packs/{id}", _builtin_pack),
    ("relay group", "/connections/relays/{id}", lambda token, _: _created(api("POST", "/relays/groups", token, {
        "name": f"{SEED_NAME} relay group", "description": "Seeded by the lab.",
    }))),
    ("dataset", "/evaluations/datasets/{id}", lambda token, _: _created(api("POST", "/datasets", token, {
        "name": f"{SEED_NAME} dataset", "family": "PROSE",
        "entries": [{"input_prompt": "Name the lab's model.", "expected_answer": "lab-model"}],
    }))),
    ("evaluation", "/evaluations/{id}", lambda token, rows: _created(api("POST", "/evaluations", token, {
        "name": f"{SEED_NAME} evaluation", "family": "PROSE",
        "dataset_ids": [rows["dataset"]] if "dataset" in rows else None,
    }))),
    ("fleet", "/fleets/{id}", _seed_fleet),
]
#: Pages with no row to create: a fixed URL, listed with the seeded ones.
STATIC_PAGES = {
    "run comparison": ("/evaluations/{id}/compare", "evaluation"),
}


#: Themes ``LAB_THEME`` accepts: the values of the user's ``theme`` preference.
LAB_THEMES = ("light", "dark")


def seed_theme(token: str, who: str = "administrator") -> str:
    """Store ``LAB_THEME`` as the theme preference of one seeded user; answer the theme its sessions open in.

    The app paints the theme of the signed-in user's preference (``/me``) and writes it over
    the browser's ``copilot-theme`` cache, so a light capture needs the preference, not a
    forced ``data-mode`` attribute (which leaves the shell half switched). The preference is
    per user: ``seed_entities`` calls it for the administrator and ``seed_peer`` for the peer,
    each before its signed-in link is printed, so both identities open in ``LAB_THEME``.
    """
    theme = os.environ.get("LAB_THEME", "").strip().lower()
    if theme not in LAB_THEMES:
        if theme and who == "administrator":
            log(f"LAB_THEME={theme!r} ignored: one of {', '.join(LAB_THEMES)}")
        return "dark"
    try:
        api("PUT", "/settings/preferences", token, {"theme": theme})
    except Exception as exc:  # noqa: BLE001 - the seed never stops the lab
        log(f"theme not set for the {who}: {exc}")
        return "dark"
    log(f"theme: {theme} (the {who}'s preference)")
    return theme


#: The lab's second account: a non-admin peer, so a list shows what another owner shared with
#: you ("Shared by") and what you may only read (Read-only) on real rows, with no fetch override.
#: Lab-only, like the administrator's ``ADMIN_PASSWORD``: valid nowhere but inside the lab.
PEER_EMAIL = "lab-peer@filigran.io"
PEER_PASSWORD = "lab-only-peer-password-1"
PEER_NAME = "Lab peer"
#: The lab's two-factor account: a non-admin with TOTP enrolled, so User detail renders the
#: Two-factor card enabled (Reset two-factor) on a real row (#10159). Lab-only, never printed.
MFA_EMAIL = "lab-mfa@filigran.io"
MFA_PASSWORD = "lab-only-mfa-password-1"
MFA_NAME = "Lab two-factor user"
#: The platform cost ceiling of the lab (``AGENT_LOOP_MAX_COST_USD`` in ``backend_env``), in USD.
LAB_BUDGET_CEILING_USD = "5"


def signed_in_url(base: str, token: str) -> str:
    """The preview URL that lands signed in: the SSO callback form ``Login.tsx`` consumes.

    ``consumeSsoCallback`` stores the fragment's ``token`` and scrubs it from the address bar, so one
    browser ``navigate`` on it signs the session in (kept across calls by the browser tool's kept
    state) instead of typing the form. Lab-only tokens of a throwaway instance, like its passwords.
    """
    return f"{base}/login#token={token}"


def log_session(label: str, row: dict[str, str]) -> None:
    """Log one ``seeded signed-in session (...)`` line the moment its sign-in answers (#89).

    ``log_seeded`` logs it again at the end of the seed, but a long seed (every CTEM module) keeps that line minutes
    away: logged here, the session is in the start log within seconds of the preview coming up. ``parse_sessions``
    keeps the last URL per label, the same token, so the repeat changes nothing.
    """
    log(f"seeded {label}: {row['url']}")


def _share_read_only(token: str, workspace: str, user_id: str) -> None:
    api("PUT", f"/workspaces/{workspace}", token, {
        "shared_user_ids": [user_id], "shared_user_permissions": {user_id: "read_only"},
    })


def seed_peer(token: str, base: str) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Create the peer and the rows two owners need; answer the seeded and the skipped rows.

    The peer owns a workspace shared read-only with the administrator (who sees "Shared by"),
    a work item and a shared-state entry; the administrator owns a workspace shared read-only
    with the peer (who sees "Shared by" and Read-only: an administrator manages every entity,
    so Read-only renders for a non-admin viewer only). Every step may be refused: logged, skipped.
    """
    seeded: dict[str, dict[str, str]] = {}
    skipped: dict[str, str] = {}
    try:
        peer = _created(api("POST", "/users", token, {
            "email": PEER_EMAIL, "full_name": PEER_NAME, "password": PEER_PASSWORD, "role": "user",
        }))
        login = api("POST", "/auth/login", body={"email": PEER_EMAIL, "password": PEER_PASSWORD})
        peer_token = login.get("access_token") if isinstance(login, dict) else None
        if not peer_token:
            raise RuntimeError("the peer's sign-in answered no access token")
        seed_theme(peer_token, "peer")
        admin = _created(api("GET", "/auth/me", token))
    except Exception as exc:  # noqa: BLE001 - the seed never stops the lab
        return seeded, {"peer user": str(exc)[:200]}
    seeded["peer user"] = {"id": peer, "url": f"{base}/users-and-groups/users/{peer}"}
    seeded["signed-in session (peer)"] = {"id": PEER_EMAIL, "url": signed_in_url(base, peer_token)}
    log_session("signed-in session (peer)", seeded["signed-in session (peer)"])
    rows: list[tuple[str, str, object]] = [
        # The administrator opens /tasks/workspaces: "Shared by Lab peer" on this row.
        ("workspace shared by the peer", "/tasks/workspaces/{id}", lambda: _shared_workspace(peer_token, admin, "peer")),
        ("workspace shared with the peer", "/tasks/workspaces/{id}", lambda: _shared_workspace(token, peer, "administrator")),
        ("peer work item", "/tasks/work-items", lambda: _created(api("POST", "/work-items", peer_token, {
            "title": f"{SEED_NAME} work item of the peer", "description": "Seeded by the lab, owned by the peer.",
        }))),
        ("peer shared state", "/operations/shared-state", lambda: _created(api("POST", "/shared-state", peer_token, {
            "key": "lab-peer-sample", "label": f"{SEED_NAME} state of the peer", "value": {"seeded_by": "lab"},
        }))),
    ]
    for entity, route, create in rows:
        try:
            row = create()  # type: ignore[operator]
            seeded[entity] = {"id": row, "url": base + route.format(id=row)}
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped[entity] = str(exc)[:200]
    shared = seeded.get("workspace shared with the peer", {}).get("id")
    if shared:
        try:
            seeded.update(seed_workspace_work_item(token, base, shared))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["workspace work item"] = str(exc)[:200]
    else:
        skipped["workspace work item"] = "no workspace shared with the peer to file it in"
    try:
        seeded.update(seed_budget_ceiling(peer_token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["budget ceiling"] = str(exc)[:200]
    try:
        seeded.update(seed_peer_general_assistant(peer_token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["peer General Assistant tools"] = str(exc)[:200]
    return seeded, skipped


def seed_peer_general_assistant(peer_token: str, base: str) -> dict[str, dict[str, str]]:
    """The peer keeps the General Assistant as its chat agent, so /profile renders its tools card (#10210).

    In XTM One mode every new account gets the CTEM Assistant as ``default_agent_id``, and Profile
    hides the General Assistant Tools card ("Reset to defaults") behind a custom default agent. The
    peer clears it through the real ``PUT /settings/preferences`` (an empty value clears it), read
    back on ``GET /auth/me``; the administrator keeps the platform's default. May raise.
    """
    api("PUT", "/settings/preferences", peer_token, {"default_agent_id": ""})
    me = api("GET", "/auth/me", peer_token)
    if not isinstance(me, dict) or me.get("default_agent_id"):
        raise RuntimeError("the peer's default agent is still set after the clear")
    return {"peer General Assistant tools": {"id": PEER_EMAIL, "url": f"{base}/profile"}}


def totp_code(secret: str, at: float | None = None) -> str:
    """The RFC 6238 code of a base32 *secret* (SHA1, 6 digits, 30 s): the backend's parameters."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", int((time.time() if at is None else at) // 30)), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def seed_mfa_user(token: str, base: str) -> dict[str, dict[str, str]]:
    """One non-admin user with two-factor enrolled through the real enrollment (#10159).

    ``POST /auth/mfa/setup`` as that user mints the secret, ``POST /auth/mfa/enable`` confirms it
    with a code computed here, and ``GET /users/{id}`` is read back as the administrator with
    ``mfa_enabled`` true, so User detail renders the Two-factor card enabled with Reset two-factor
    and no fetch override. The secret and the recovery codes stay in this function, never logged.
    Logged ``seeded two-factor user: <url>``. May raise: the caller logs and skips.
    """
    user = _created(api("POST", "/users", token, {
        "email": MFA_EMAIL, "full_name": MFA_NAME, "password": MFA_PASSWORD, "role": "user",
    }))
    login = api("POST", "/auth/login", body={"email": MFA_EMAIL, "password": MFA_PASSWORD})
    user_token = login.get("access_token") if isinstance(login, dict) else None
    if not user_token:
        raise RuntimeError("the two-factor user's sign-in answered no access token")
    setup = api("POST", "/auth/mfa/setup", user_token)
    secret = setup.get("secret") if isinstance(setup, dict) else None
    if not secret:
        raise RuntimeError("POST /auth/mfa/setup answered no secret")
    api("POST", "/auth/mfa/enable", user_token, {"code": totp_code(secret)})
    row = api("GET", f"/users/{user}", token)
    if not isinstance(row, dict) or row.get("mfa_enabled") is not True:
        raise RuntimeError("GET /users/{id} does not answer mfa_enabled true after the enrollment")
    return {"two-factor user": {"id": user, "url": f"{base}/users-and-groups/users/{user}"}}


#: The lab's account nobody's assistant can change a fleet for: its General Assistant override carries no
#: Platform tool and no agent it sees carries the fleet functions (#10281). Lab-only, never printed.
FLEET_AI_BLOCKED_EMAIL = "lab-fleet-ai-blocked@filigran.io"
FLEET_AI_BLOCKED_PASSWORD = "lab-only-fleet-ai-blocked-password-1"
FLEET_AI_BLOCKED_NAME = "Lab user without fleet tools"
#: The ids that carry the fleet functions: the platform group and its former ids (``builtin_tool_aliases``).
FLEET_TOOL_IDS = frozenset({"platform", "agent_builder", "fleets"})


def seed_fleet_chat_cards(token: str, base: str, fleet: str) -> dict[str, dict[str, str]]:
    """Two "Modify with AI" conversations of *fleet* whose last answer renders a card (#10281).

    The lab model calls no tool, so ``seed_fleet_chat_cards.py`` writes them through the backend's own
    model: the pause summary of ``set_fleet_automations`` (the confirm card) and the autonomy raise
    refusal of ``update_fleet`` (the refusal card), both bound with ``fleet_id``. Each is read back on
    ``GET /chat/conversations/{id}/messages`` with its ``tool_call_trace``. Logged ``seeded fleet chat
    card confirm: <url>`` and ``... raise_refused: <url>``, the panel open on the conversation. May raise.
    """
    if not fleet:
        fleet = _first(token, "/fleets")
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "fleet_chat_cards.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_fleet_chat_cards.py"), admin, fleet, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_fleet_chat_cards.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    seeded: dict[str, dict[str, str]] = {}
    tools = {"confirm": "set_fleet_automations", "raise_refused": "update_fleet"}
    for kind, conversation in json.loads(out.read_text()).items():
        messages = api("GET", f"/chat/conversations/{conversation}/messages", token)
        rows = messages.get("messages") if isinstance(messages, dict) else None
        last = rows[-1] if isinstance(rows, list) and rows else {}
        trace = last.get("tool_call_trace") or []
        if last.get("role") != "assistant" or not trace or trace[-1].get("name") != tools[kind]:
            raise RuntimeError(f"conversation {conversation} ({kind}): the last answer carries no {tools[kind]} trace")
        seeded[f"fleet chat card {kind}"] = {
            "id": conversation, "url": f"{base}/fleets/{fleet}?ai=1&c={conversation}",
        }
    return seeded


def seed_perf_thread(token: str, base: str) -> dict[str, dict[str, str]]:
    """``LAB_PERF_THREAD=on``: "Perf 200-message thread" and "Perf short thread" of the administrator.

    No API writes stored history, so ``seed_perf_thread.py`` writes them through the backend's own model.
    Each is read back on ``GET /chat/conversations/{id}/messages?limit=500`` (200 and 2 messages). Logged
    ``perf thread: {kind: id}``; the conversations are listed on ``/chat``. May raise.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "perf_thread.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_perf_thread.py"), admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_perf_thread.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    ids: dict[str, str] = json.loads(out.read_text())
    expected = {"long": 200, "short": 2}
    seeded: dict[str, dict[str, str]] = {}
    for kind, conversation in ids.items():
        messages = api("GET", f"/chat/conversations/{conversation}/messages?limit=500", token)
        rows = messages.get("messages") if isinstance(messages, dict) else None
        count = len(rows) if isinstance(rows, list) else 0
        if count < expected[kind]:
            raise RuntimeError(f"conversation {conversation} ({kind}): {count} messages read back, not {expected[kind]}")
        seeded[f"perf thread {kind}"] = {"id": conversation, "url": f"{base}/chat"}
    log(f"perf thread: {ids}")
    return seeded


def seed_fleet_ai_blocked_user(token: str, base: str) -> dict[str, dict[str, str]]:
    """A non-admin who owns a fleet and whom no assistant can change it for (#10281).

    Its General Assistant override (``PUT /settings/preferences``, ``ga_builtin_tool_ids``) carries no
    Platform tool, and ``GET /agents`` as that user is read back: an enabled chat agent carrying the
    platform group or a former id of it would offer the panel, so the seed raises naming it. The fleet
    page then renders "Modify with AI" disabled with its reason line and the blocked rail, with no fetch
    override. Logged ``seeded fleet AI blocked user: <signed-in url>`` and ``seeded fleet AI blocked
    fleet: <url>``. May raise: the caller logs and skips.
    """
    user = _created(api("POST", "/users", token, {
        "email": FLEET_AI_BLOCKED_EMAIL, "full_name": FLEET_AI_BLOCKED_NAME, "password": FLEET_AI_BLOCKED_PASSWORD,
        "role": "user",
    }))
    login = api("POST", "/auth/login", body={"email": FLEET_AI_BLOCKED_EMAIL, "password": FLEET_AI_BLOCKED_PASSWORD})
    user_token = login.get("access_token") if isinstance(login, dict) else None
    if not user_token:
        raise RuntimeError("the fleet AI blocked user's sign-in answered no access token")
    api("PUT", "/settings/preferences", user_token, {"default_agent_id": "", "ga_builtin_tool_ids": ["web_fetch"]})
    me = api("GET", "/auth/me", user_token)
    if not isinstance(me, dict) or FLEET_TOOL_IDS & set(me.get("ga_builtin_tool_ids") or ["platform"]):
        raise RuntimeError("the user's General Assistant override still carries the Platform tool")
    agents = api("GET", "/agents", user_token)
    agents = agents.get("items", []) if isinstance(agents, dict) else agents
    carrying = sorted(
        a.get("name", "?") for a in agents or []
        if a.get("enabled") and not a.get("disable_chat") and FLEET_TOOL_IDS & set(a.get("builtin_tool_ids") or [])
    )
    if carrying:
        raise RuntimeError(f"agents the user sees carry the fleet functions: {', '.join(carrying)}")
    fleet = _created(api("POST", "/fleets", user_token, {
        "name": f"{SEED_NAME} fleet without AI", "description": "Seeded by the lab: nobody can change it with AI.",
    }))
    return {
        "fleet AI blocked user": {"id": user, "url": signed_in_url(base, user_token)},
        "fleet AI blocked fleet": {"id": fleet, "url": f"{base}/fleets/{fleet}"},
    }


def seed_workspace_work_item(token: str, base: str, workspace: str) -> dict[str, dict[str, str]]:
    """One open work item the administrator files in its workspace shared with the peer (#9796).

    Its ``context.visibility.reason`` is ``workspace``, so Operations > Work items renders the outline
    workspace chip (folder icon and the workspace name) on a real row. The title stays short: a title
    that wraps the chip strip makes OpsChipStrip hide the chip below 2560 px (#9804). Acceptance: the
    chip is visible at 1440 px (dark and light); at 1024 px the title column leaves no room for it and
    OpsChipStrip hides it by priority (data-ops-hidden=true), the designed behaviour, not a seed defect.
    A create answering another workspace (the field dropped) is refused: logged, skipped.
    """
    row = api("POST", "/work-items", token, {
        "title": f"{SEED_NAME} workspace item",
        "description": "Seeded by the lab, filed in a workspace: everyone who reads it sees the item.",
        "workspace_id": workspace,
    })
    item = _created(row)
    if not isinstance(row, dict) or row.get("workspace_id") != workspace:
        raise RuntimeError(f"the work item was not filed in workspace {workspace}: {str(row)[:120]}")
    return {"workspace work item": {"id": item, "url": f"{base}/tasks/work-items"}}


def seed_budget_ceiling(peer_token: str, base: str) -> dict[str, dict[str, str]]:
    """A fleet the peer owns and edits, under a platform cost ceiling the peer may not exceed.

    ``backend_env`` sets ``AGENT_LOOP_MAX_COST_USD``; ``GET /agents/budget-policy`` read as the peer must
    answer ``can_exceed_ceiling`` false and that ceiling, else the row is refused (logged, skipped). Opened
    with the peer's signed-in session, typing a Max cost above the ceiling in the fleet settings Run
    defaults (or an agent's Budget tab) renders the administrator-only refusal with no query override.
    """
    policy = api("GET", "/agents/budget-policy", peer_token)
    if not isinstance(policy, dict) or policy.get("can_exceed_ceiling") is not False:
        raise RuntimeError(f"the peer may exceed the ceiling: {str(policy)[:120]}")
    ceiling = (policy.get("ceilings") or {}).get("max_cost_usd")
    if not ceiling:
        raise RuntimeError("no platform max_cost_usd ceiling: AGENT_LOOP_MAX_COST_USD is unset or 0")
    fleet = _created(api("POST", "/fleets", peer_token, {
        "name": f"{SEED_NAME} fleet under a budget ceiling",
        "description": f"Seeded by the lab: owned by the peer, under a platform cost ceiling of {ceiling} USD.",
    }))
    return {"budget ceiling": {"id": fleet, "url": f"{base}/fleets/{fleet}"}}


#: The large fleet: an orchestrator over a lead and four members, the lead itself the parent of
#: two of them, so the Graph tab draws a dense tree with one nested level (seven nodes).
LARGE_FLEET_MEMBERS: dict[str, str | None] = {
    "Lead": None, "Scout": None, "Auditor": None, "Planner": "Lead", "Reviewer": "Lead", "Archivist": None,
}


def seed_large_fleet(token: str, base: str) -> dict[str, dict[str, str]]:
    """One fleet of seven agents with a nested parent, so the Graph tab renders a dense fleet on real rows.

    The orchestrator delegates to the members it parents, the lead to the two members under it.
    May raise: the caller logs and skips.
    """
    names = list(LARGE_FLEET_MEMBERS)
    agents = {name: _seed_agent(token, f"large {name.lower()}") for name in names}
    for name in names:
        children = [agents[child] for child, parent in LARGE_FLEET_MEMBERS.items() if parent == name]
        if children:
            api("PUT", f"/agents/{agents[name]}", token, {
                "handover_agent_ids": children, "handover_types": dict.fromkeys(children, "delegate"),
            })
    top = [agents[name] for name, parent in LARGE_FLEET_MEMBERS.items() if parent is None]
    orchestrator = _seed_agent(token, "large orchestrator", dict.fromkeys(top, "delegate"))
    LAB_AGENTS["large lead"] = agents["Lead"]  # bound to the A2A liaisons (seed_a2a_liaisons, #85)
    fleet = _created(api("POST", "/fleets", token, {
        "name": f"{SEED_NAME} large fleet",
        "description": "Seeded by the lab: one orchestrator, six members, two of them under a lead.",
    }))
    rows = {"Orchestrator": _created(api("POST", f"/fleets/{fleet}/members", token, {
        "agent_id": orchestrator, "role_label": "Orchestrator",
    }))}
    api("PUT", f"/fleets/{fleet}", token, {"orchestrator_agent_id": orchestrator})
    # Parents first: the dict lists the lead before the members under it.
    for name, parent in LARGE_FLEET_MEMBERS.items():
        rows[name] = _created(api("POST", f"/fleets/{fleet}/members", token, {
            "agent_id": agents[name], "parent_member_id": rows[parent or "Orchestrator"], "role_label": name,
        }))
    return {"large fleet": {"id": fleet, "url": f"{base}/fleets/{fleet}"}}


#: The marker ``app.services.fleet_doc_on_create`` sets while a fleet's first documentation is generated.
LAB_DOC_GENERATING_PREFIX = "fleet_doc:generating:"
#: The marker's lifetime in the backend (``GENERATING_TTL_SECONDS``); the lab refreshes it well before.
LAB_DOC_GENERATING_TTL_SECONDS = 300
LAB_DOC_GENERATING_REFRESH_SECONDS = 60
#: How long the creation-time generation (refused by the stub) may take to end and clear its marker.
LAB_DOC_GENERATING_SETTLE_SECONDS = 20


def _hold_doc_generating(fleet: str) -> None:
    """Keep the fleet's generating marker alive for the lab's lifetime (a daemon thread)."""
    marker = f"{LAB_DOC_GENERATING_PREFIX}{fleet}"
    while True:
        try:
            redis_command("SET", marker, "1", "EX", str(LAB_DOC_GENERATING_TTL_SECONDS))
        except Exception as exc:  # noqa: BLE001 - a missed refresh is retried; the marker outlives it
            log(f"fleet documentation generating: marker not refreshed: {exc}")
        time.sleep(LAB_DOC_GENERATING_REFRESH_SECONDS)


#: The three ``LAB_FIGURES_SCALE=large`` fleets: the About quota tile reads about, a floor, a range.
LARGE_FIGURES_FLEETS = ("figures about", "figures floor", "figures range")


def seed_large_figures(token: str, base: str) -> dict[str, dict[str, str]]:
    """Fleet and home figures of 1,000 and more under ``LAB_FIGURES_SCALE=large`` (#10734).

    Three fleets, each with one member and one automation switched on (so About shows its KPI row,
    not the setup row); ``seed_large_figures.py`` stores their quota lines (about 1,240, 1,180 or
    more, 1,050 to 2,400), 1,240 runs of the first fleet over 28 days, and 1,000 and more agents,
    conversations and documents of the administrator. Read back from ``/fleets/<id>/documentation``,
    ``/fleets/<id>/health`` and ``/activity/stats``. Logged ``seeded large fleet and home figures:
    <url>``. May raise: the caller logs and skips.
    """
    fleets: list[str] = []
    members: list[str] = []
    for name in LARGE_FIGURES_FLEETS:
        member = _seed_agent(token, f"{name} member")
        _created(api("POST", "/assignments", token, {
            "agent_id": member, "name": f"{SEED_NAME} {name} automation", "description": "Seeded by the lab.",
            "prompt": "Triage the new threat reports.", "trigger_type": "manual", "trigger_config": {},
            "enabled": True,
        }))
        fleet = _created(api("POST", "/fleets", token, {
            "name": f"{SEED_NAME} {name}", "description": "Seeded by the lab: figures of 1,000 and more.",
        }))
        _created(api("POST", f"/fleets/{fleet}/members", token, {"agent_id": member, "role_label": "Member"}))
        fleets.append(fleet)
        members.append(member)
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "large_figures.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_large_figures.py"), admin, *fleets, members[0],
         str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=300,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_large_figures.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    for fleet in fleets:
        quota = (api("GET", f"/fleets/{fleet}/documentation", token).get("structured") or {}).get("quota") or []
        if sum(int(q.get("runs_max") or 0) for q in quota) < 1000:
            raise RuntimeError(f"GET /fleets/{fleet}/documentation: quota {str(quota)[:300]}")
    runs = (api("GET", f"/fleets/{fleets[0]}/health", token).get("run_stats") or {}).get("runs") or 0
    if runs < 1000:
        raise RuntimeError(f"GET /fleets/{fleets[0]}/health: {runs} runs in 30 days")
    stats = api("GET", "/activity/stats", token)
    figures = {
        "agents": (stats.get("agents") or {}).get("active") or 0,
        "conversations": stats.get("conversation_count") or 0,
        "runs this month": (stats.get("runs") or {}).get("this_month") or 0,
        "documents": stats.get("document_count") or 0,
    }
    if min(figures.values()) < 1000:
        raise RuntimeError(f"GET /activity/stats: {figures}")
    url = f"{base}/fleets/{fleets[0]}"
    log(f"seeded large fleet and home figures: {url}")
    return {
        f"large figures fleet {name.split()[-1]}": {"id": fleet, "url": f"{base}/fleets/{fleet}"}
        for name, fleet in zip(LARGE_FIGURES_FLEETS, fleets, strict=True)
    } | {"large home figures": {"id": "activity-stats", "url": f"{base}/"}}


def seed_doc_generating_fleet(token: str, base: str) -> dict[str, dict[str, str]]:
    """A fleet at version 0 whose first documentation stays generating, so About shows its notice (#10345).

    The stub model saves a new fleet's version 1 in under a second, so the creation-time generation
    (#10316) is never seen. This fleet's description carries ``LAB_DOC_DRAFT_REFUSE``: the stub refuses
    the draft, the generation saves nothing and clears its marker. The lab then sets the marker itself
    and refreshes it every minute (``_hold_doc_generating``), so ``GET /fleets/<id>/documentation``
    answers ``generating`` with version 0 for the lab's lifetime, read back before the row is logged as
    ``seeded fleet documentation generating: <url>``. May raise: the caller logs and skips.
    """
    fleet = _created(api("POST", "/fleets", token, {
        "name": f"{SEED_NAME} fleet with its documentation generating",
        "description": f"Seeded by the lab: its first documentation stays generating ({LAB_DOC_DRAFT_REFUSE}).",
    }))
    marker = f"{LAB_DOC_GENERATING_PREFIX}{fleet}"
    deadline = time.monotonic() + LAB_DOC_GENERATING_SETTLE_SECONDS
    while redis_command("EXISTS", marker) and time.monotonic() < deadline:
        time.sleep(0.5)
    redis_command("SET", marker, "1", "EX", str(LAB_DOC_GENERATING_TTL_SECONDS))
    doc = api("GET", f"/fleets/{fleet}/documentation", token)
    if not isinstance(doc, dict) or doc.get("version") or doc.get("generating") is not True:
        raise RuntimeError(f"the documentation does not answer generating at version 0: {str(doc)[:160]}")
    threading.Thread(target=_hold_doc_generating, args=(fleet,), daemon=True).start()
    return {"fleet documentation generating": {"id": fleet, "url": f"{base}/fleets/{fleet}"}}


def seed_doc_hold_fleet(token: str, base: str) -> dict[str, dict[str, str]]:
    """A documented fleet whose next draft is held, so a Regenerate from About stays generating (#10859).

    The stub answers a draft at once, so a person's Regenerate lands in about 100 ms and the poll that
    lands a new version, the spinner across navigation and the second-tab case are never seen. The fleet
    is created without the marker, its first documentation (#10316) is waited for (version 1), then its
    description gets ``LAB_DOC_DRAFT_HOLD``: every later draft waits the default hold before it is
    answered (``doc_draft_answer``). Found again by name on a restart. Logged ``seeded fleet
    documentation hold: <url>``. May raise: the caller logs and skips.
    """
    name = f"{SEED_NAME} fleet whose documentation regenerates slowly"
    rows = api("GET", "/fleets", token)
    rows = rows.get("items", []) if isinstance(rows, dict) else rows
    found = next((row for row in rows if row.get("name") == name), None)
    fleet = str(found["id"]) if found else _created(api("POST", "/fleets", token, {
        "name": name,
        "description": "Seeded by the lab: its documentation regenerates slowly.",
    }))
    deadline = time.monotonic() + LAB_DOC_GENERATING_SETTLE_SECONDS
    doc = api("GET", f"/fleets/{fleet}/documentation", token)
    while not (isinstance(doc, dict) and doc.get("version")) and time.monotonic() < deadline:
        time.sleep(0.5)
        doc = api("GET", f"/fleets/{fleet}/documentation", token)
    if not isinstance(doc, dict) or not doc.get("version"):
        raise RuntimeError(f"the first documentation was not saved: {str(doc)[:160]}")
    description = (
        "Seeded by the lab: its documentation regenerates slowly "
        f"({LAB_DOC_DRAFT_HOLD} {LAB_DOC_DRAFT_HOLD_DEFAULT_SECONDS})."
    )
    api("PUT", f"/fleets/{fleet}", token, {"description": description})
    row = api("GET", f"/fleets/{fleet}", token)
    if not isinstance(row, dict) or doc_draft_hold_seconds(row.get("description")) != LAB_DOC_DRAFT_HOLD_DEFAULT_SECONDS:
        raise RuntimeError(f"the fleet description carries no draft hold: {str(row)[:160]}")
    url = f"{base}/fleets/{fleet}"
    log(f"seeded fleet documentation hold: {url}")
    return {"fleet documentation hold": {"id": fleet, "url": url}}


#: The instructions position ``seed_instructions_position_fleet`` sets (``fleet_autonomy.INSTRUCTIONS_POSITIONS``).
LAB_INSTRUCTIONS_POSITION = "medium_low"


def seed_instructions_position_fleet(token: str, base: str) -> dict[str, dict[str, str]]:
    """A custom fleet that gates nothing and carries an instructions position (#10168, #10439, #10457).

    Its status reads "Between Medium and Low, by instruction" on the Fleets list card and the About
    chip, with no hand ``PUT`` by the reviewer. The fleet stores an empty approval gate catalog
    (``PUT /fleets/<id>/approval-gates`` with no gate, so a documentation draft never adds one) and
    the position through ``PUT /fleets/<id>/instructions-position`` with ``announce`` false (an
    initial reading: no member prompt changes). Read back from ``GET /fleets/<id>/approval-gates``:
    an empty effective catalog and the position. Found again by name on a restart. Logged
    ``seeded fleet instructions position: <url>``. May raise: the caller logs and skips.
    """
    name = f"{SEED_NAME} fleet that gates nothing, positioned by instruction"
    rows = api("GET", "/fleets", token)
    rows = rows.get("items", []) if isinstance(rows, dict) else rows
    found = next((row for row in rows if row.get("name") == name), None)
    fleet = str(found["id"]) if found else _created(api("POST", "/fleets", token, {
        "name": name,
        "description": "Seeded by the lab: no gated action, its instructions put it between Medium and Low.",
    }))
    api("PUT", f"/fleets/{fleet}/approval-gates", token, {"gates": []})
    api("PUT", f"/fleets/{fleet}/instructions-position", token, {
        "position": LAB_INSTRUCTIONS_POSITION, "announce": False,
    })
    row = api("GET", f"/fleets/{fleet}/approval-gates", token)
    if (
        not isinstance(row, dict)
        or row.get("effective_catalog")
        or row.get("instructions_position") != LAB_INSTRUCTIONS_POSITION
    ):
        raise RuntimeError(f"the fleet gates an action or has no instructions position: {str(row)[:200]}")
    return {"fleet instructions position": {"id": fleet, "url": f"{base}/fleets/{fleet}"}}


#: The memory entries ``seed_agent_memory`` imports: (type, source, subject, content, days ago, scoped to the
#: administrator). Every importable type, ``chat`` and ``assignment`` sources, one ``state`` chain on one
#: subject (the two older entries land superseded) and one entry over 250 characters, so Agent > Memory pages,
#: filters and wraps on real rows (#11001).
AGENT_MEMORY_SUBJECT = "ticket:LAB-42"
AGENT_MEMORY_LONG = (
    "The analyst team triages phishing reports in three passes: the first pass reads the headers and the "
    "sending infrastructure, the second pass opens the attachments in the sandbox and records every "
    "indicator it extracts, and the third pass links the report to an existing campaign or opens a new one "
    "with the evidence attached, so a reviewer can follow every decision back to its source."
)
AGENT_MEMORY_ENTRIES: tuple[tuple[str, str, str | None, str, int, bool], ...] = (
    ("fact", "chat", None, "The SOC works from Paris and Singapore, handing over at 09:00 UTC.", 30, False),
    ("fact", "assignment", None, "Weekly threat digests are due every Monday before 08:00 UTC.", 29, False),
    ("fact", "chat", None, "The lab OpenCTI instance holds the sample intrusion sets.", 28, True),
    ("fact", "assignment", None, AGENT_MEMORY_LONG, 27, False),
    ("fact", "chat", None, "Critical vulnerabilities are escalated to the on-call engineer within one hour.", 26, False),
    ("fact", "assignment", None, "The phishing mailbox is read every fifteen minutes.", 25, False),
    ("learned", "chat", None, "Summaries land better with the decision first and the evidence after.", 24, True),
    ("learned", "assignment", None, "Indicator lists over fifty rows go in an attached file, not in the message.", 23, False),
    ("learned", "chat", None, "A report without a confidence level is sent back to its author.", 22, False),
    ("learned", "assignment", None, "The digest run is faster when it reads the last week's notes first.", 21, False),
    ("learned", "chat", None, "Duplicate alerts from the same sensor are grouped before triage.", 20, False),
    ("preference", "chat", None, "The administrator prefers tables over bullet lists for comparisons.", 19, True),
    ("preference", "chat", None, "Times are written in UTC with the time zone named.", 18, False),
    ("preference", "assignment", None, "Reports are written in English, short sentences.", 17, False),
    ("preference", "chat", None, "Links to the source are placed at the end of each line.", 16, True),
    ("style", "chat", None, "Lead with the answer, then the reasoning.", 15, False),
    ("style", "assignment", None, "No exclamation marks and no hype words.", 14, False),
    ("style", "chat", None, "Bold lead-ins on each bullet.", 13, False),
    ("style", "chat", None, "Code and queries go in fenced blocks.", 12, True),
    ("state", "assignment", AGENT_MEMORY_SUBJECT, "LAB-42 is open: the phishing campaign is being triaged.", 11, False),
    ("state", "assignment", AGENT_MEMORY_SUBJECT, "LAB-42 is waiting for the sandbox report.", 10, False),
    ("state", "chat", AGENT_MEMORY_SUBJECT, "LAB-42 is closed: the campaign is linked to its intrusion set.", 9, False),
    ("state", "assignment", "digest:weekly", "The weekly digest of last Monday was sent to the team.", 8, False),
    ("state", "chat", "sensor:edge-01", "Sensor edge-01 is back online after maintenance.", 7, True),
    ("consolidated", "assignment", None, "The team triages phishing in three passes and links reports to campaigns.", 6, False),
    ("fact", "chat", None, "The lab GitHub integration is read-only for the triage member.", 5, False),
    ("learned", "assignment", None, "Retries of a failed digest run are spaced by ten minutes.", 4, False),
    ("preference", "chat", None, "Weekend escalations go to the duty phone first.", 3, False),
    ("fact", "assignment", None, "Every assignment run writes its outcome to the work item.", 2, False),
    ("learned", "chat", None, "Questions about scope are answered from the organization profile.", 1, True),
)


def agent_memory_entries(admin: str, now: float) -> list[dict]:
    """``AGENT_MEMORY_ENTRIES`` as the import body of ``POST /agents/<id>/memory``, dated back from *now*."""
    out: list[dict] = []
    for memory_type, source, subject, content, days, scoped in AGENT_MEMORY_ENTRIES:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - days * 86400))
        entry = {"content": content, "memory_type": memory_type, "source": source, "created_at": stamp}
        if subject:
            entry["subject_key"] = subject
        if scoped:
            entry["user_id"] = admin
        out.append(entry)
    return out


def seed_agent_memory(token: str, base: str) -> dict[str, dict[str, str]]:
    """One agent whose Memory tab pages on real rows (#11001): ``AGENT_MEMORY_ENTRIES`` imported.

    The entries go through ``POST /agents/<id>/memory`` (the import route, no model call): every
    importable type, both sources, a ``state`` chain, a long entry, agent-level and administrator-scoped
    rows. Read back on ``GET /agents/<id>/memory?limit=1``: ``total_count`` is the number imported.
    Logged ``seeded agent memory: <url>`` with the other rows. May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    agent = _seed_agent(token, "memory keeper")
    entries = agent_memory_entries(admin, time.time())
    answer = api("POST", f"/agents/{agent}/memory", token, {"entries": entries})
    if not isinstance(answer, dict) or answer.get("imported") != len(entries):
        raise RuntimeError(f"the import did not store every entry: {str(answer)[:200]}")
    listed = api("GET", f"/agents/{agent}/memory?limit=1", token)
    if not isinstance(listed, dict) or listed.get("total_count") != len(entries):
        raise RuntimeError(f"stored, but the Memory list counts {str(listed)[:160]}")
    return {"agent memory": {"id": agent, "url": f"{base}/agents/{agent}/memory"}}


def seed_fleet_gate_review(token: str, base: str, github: str) -> dict[str, dict[str, str]]:
    """A documented fleet with no gate catalog, so the Approval tab offers Review actions with rows (#11001).

    The strip offers "Review actions" (``FleetGateReview``) only when the effective catalog is empty, and
    every new custom fleet acts on its computed catalog (``approval_pins`` ``{}``), so any member with a
    write tool gives it one. This fleet has one member bound with write access to the lab GitHub
    integration *github* (its write tools are the proposals), its first documentation is waited for, an
    empty catalog is stored (``PUT /fleets/<id>/approval-gates``, so the documentation's gate is not
    read) and ``seed_fleet_gate_review.py`` sets ``approval_pins`` back to ``NULL`` through the backend's
    models: the legacy state no route writes, where the computed catalog stays off. Read back from
    ``GET /fleets/<id>/approval-gates``: an empty effective catalog, ``computed_catalog_on`` false and
    proposals. Found again by name on a restart. Logged ``seeded fleet gate review: <url>`` with the other
    rows. May raise: the caller logs and skips.
    """
    name = f"{SEED_NAME} fleet with actions to review"
    rows = api("GET", "/fleets", token)
    rows = rows.get("items", []) if isinstance(rows, dict) else rows
    found = next((row for row in rows if row.get("name") == name), None)
    if found:
        fleet = str(found["id"])
    else:
        member = _seed_agent(token, "gate review writer")
        api("PUT", f"/agents/{member}", token, {"integration_bindings": {"github": [github]}})
        fleet = _created(api("POST", "/fleets", token, {
            "name": name,
            "description": "Seeded by the lab: documented, no approval gate catalog, actions to review.",
        }))
        _created(api("POST", f"/fleets/{fleet}/members", token, {"agent_id": member, "role_label": "Writer"}))
    deadline = time.monotonic() + LAB_DOC_GENERATING_SETTLE_SECONDS
    doc = api("GET", f"/fleets/{fleet}/documentation", token)
    while not (isinstance(doc, dict) and doc.get("version")) and time.monotonic() < deadline:
        time.sleep(0.5)
        doc = api("GET", f"/fleets/{fleet}/documentation", token)
    if not isinstance(doc, dict) or not doc.get("version"):
        raise RuntimeError(f"the first documentation was not saved: {str(doc)[:160]}")
    api("PUT", f"/fleets/{fleet}/approval-gates", token, {"gates": []})
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_fleet_gate_review.py"), fleet],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_fleet_gate_review.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    row = api("GET", f"/fleets/{fleet}/approval-gates", token)
    if (
        not isinstance(row, dict)
        or row.get("effective_catalog")
        or row.get("computed_catalog_on")
        or not row.get("proposals")
    ):
        raise RuntimeError(f"the fleet has a catalog or nothing to review: {str(row)[:200]}")
    return {"fleet gate review": {"id": fleet, "url": f"{base}/fleets/{fleet}/approval"}}


#: The connected members of "Lab sample fleet": playground agents linked to it (role ``connected``), the
#: first with its own collaboration instruction and context inclusion (an override of the fleet
#: defaults), the others inheriting them, so the Members view lists inherited and overridden rows
#: and its bulk bar has rows to select.
CONNECTED_MEMBERS = ("Liaison", "Hunter", "Reporter", "Reviewer")
CONNECTED_OVERRIDE = {
    "collaboration_instruction": "Answer the fleet in one paragraph; cite the source of every fact.",
    "context_inclusion": "summary",
}
#: The connected agents by name, for ``seed_reader_fleet``.
CONNECTED_AGENTS: dict[str, str] = {}


def seed_connected_members(token: str, base: str, fleet: str) -> dict[str, dict[str, str]]:
    """Connect ``CONNECTED_MEMBERS`` to *fleet*, the first with ``CONNECTED_OVERRIDE``; log the count.

    Each agent is created in the playground and linked with ``POST /fleets/<id>/members`` and
    ``role: connected``. Logged ``seeded connected members: <n>``. May raise: the caller logs and skips.
    """
    for index, name in enumerate(CONNECTED_MEMBERS):
        agent = _seed_agent(token, f"connected {name.lower()}")
        CONNECTED_AGENTS[name] = agent
        _created(api("POST", f"/fleets/{fleet}/members", token, {
            "role": "connected", "agent_id": agent, **(CONNECTED_OVERRIDE if index == 0 else {}),
        }))
    log(f"seeded connected members: {len(CONNECTED_MEMBERS)}")
    return {"fleet members view": {"id": fleet, "url": f"{base}/fleets/{fleet}/members"}}


#: The company fleet member presets: one setting the three member settings with a ``list`` scope
#: (its organizations filled by ``seed_fleet_member_presets``), one with a name over 100 characters
#: (106; a preset name is at most 120).
FLEET_MEMBER_PRESETS: tuple[tuple[str, dict], ...] = (
    (f"{SEED_NAME} preset: summary, cited answers, two organizations", {
        "context_inclusion": "summary",
        "collaboration_instruction": "Answer the fleet in three bullets; cite the source of every fact.",
        "organization_scope": {"mode": "list"},
    }),
    (
        f"{SEED_NAME} preset for the connected agents that answer the European subsidiaries' security "
        "operations team",
        {"context_inclusion": "full"},
    ),
)


def seed_fleet_member_presets(token: str, base: str) -> dict[str, dict[str, str]]:
    """``FLEET_MEMBER_PRESETS`` as company presets, so the preset picker lists 5 rows (#9428).

    The ``list`` scope names the two active tracked organizations ``seed_organizations`` wrote
    (``organizations.json``: the first and the last of ``TRACKED`` in ``seed_organizations.py``,
    the middle one archived, which the scope refuses). Logged ``seeded fleet member presets: <n>``.
    May raise: the caller logs and skips.
    """
    tracked = json.loads((ROOT / "organizations.json").read_text())["tracked"]
    scope = {"mode": "list", "organization_ids": [tracked[0], tracked[-1]]}
    ids = []
    for name, values in FLEET_MEMBER_PRESETS:
        if values.get("organization_scope", {}).get("mode") == "list":
            values = {**values, "organization_scope": scope}
        ids.append(_created(api("POST", "/fleet-member-presets", token, {"name": name, "values": values})))
    log(f"seeded fleet member presets: {len(ids)}")
    return {"fleet member presets": {"id": ",".join(ids), "url": f"{base}/api/v1/fleet-member-presets"}}


def seed_reader_fleet(token: str, base: str, fleet: str, peer: str) -> dict[str, dict[str, str]]:
    """Share *fleet* ("Lab sample fleet") read-only with the peer, its agents too (#9428).

    The peer reads the fleet and the names of its rows but manages none: its Members view renders
    the lock note, with no "Apply a preset" and no selection bar. The agents are shared so the
    rows carry names (``seed_locked_fleet`` keeps the unshared-agents state). Open it with the
    peer's signed-in session. Logged ``seeded fleet read-only for the peer: <url>``. May raise.
    """
    read_only = {"shared_user_ids": [peer], "shared_user_permissions": {peer: "read_only"}}
    for agent in (*LAB_AGENTS.values(), *CONNECTED_AGENTS.values()):
        api("PUT", f"/agents/{agent}", token, read_only)
    api("PUT", f"/fleets/{fleet}", token, read_only)
    return {"fleet read-only for the peer": {"id": fleet, "url": f"{base}/fleets/{fleet}/members"}}


#: The source changes ``seed_fork_source_changes`` makes on "Lab sample fleet" after its forks.
FORK_SOURCE_CHANGES = {
    "role_label": "Lead analyst",
    "collaboration_instruction": "Answer the fleet in three bullets.",
}


def _fork(token: str, fleet: str, name: str) -> str:
    answer = api("POST", f"/fleets/{fleet}/fork", token, {"name": f"{SEED_NAME} {name}", "members": "copy"})
    return _created(answer.get("fleet") if isinstance(answer, dict) else answer)


def seed_fork_source_changes(
    token: str, base: str, fleet: str, builtin: str | None
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Three forks whose source changed, so the fork source-changes notice renders on real rows.

    *fleet* ("Lab sample fleet", with its connected members) is forked twice; then, through
    the API, one member's role label and one connected agent's instruction change and another
    connected agent is removed. *builtin* (the built-in fleet, or None) is forked once. The
    API refuses both other writes, so ``seed_fork_source_changes.py`` changes the built-in
    source and drops the kept structure of the second fork (an older fork: diff not
    available) on the lab's database. Each fork is read back from ``GET
    /fleets/<id>/source-changes``; one that does not answer changed is skipped. Never raises.
    """
    seeded: dict[str, dict[str, str]] = {}
    skipped: dict[str, str] = {}
    forks: dict[str, str] = {}
    try:
        forks["custom"] = _fork(token, fleet, "fork of the fleet")
        forks["older"] = _fork(token, fleet, "older fork of the fleet")
        rows = api("GET", f"/fleets/{fleet}/members", token)
        homes = [r for r in rows if r.get("role") == "member" and not r.get("is_orchestrator")]
        connected = [r for r in rows if r.get("role") == "connected"]
        if not homes or len(connected) < 2:
            raise RuntimeError(f"{len(homes)} member(s), {len(connected)} connected agent(s): 1 and 2 are needed")
        api("PATCH", f"/fleets/{fleet}/members/{homes[0]['id']}", token,
            {"role_label": FORK_SOURCE_CHANGES["role_label"]})
        api("PATCH", f"/fleets/{fleet}/members/{connected[-2]['id']}", token,
            {"collaboration_instruction": FORK_SOURCE_CHANGES["collaboration_instruction"]})
        api("DELETE", f"/fleets/{fleet}/members/{connected[-1]['id']}", token)
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["fork with source changes (custom)"] = str(exc)[:600]
        forks.clear()
    if builtin:
        try:
            forks["built-in"] = _fork(token, builtin, "fork of the built-in fleet")
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["fork with source changes (built-in)"] = str(exc)[:600]
    else:
        skipped["fork with source changes (built-in)"] = "no built-in fleet row"
    if "built-in" in forks or "older" in forks:
        out = ROOT / "fork_source_changes.json"
        process = subprocess.run(
            [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_fork_source_changes.py"),
             builtin if "built-in" in forks else "", forks.get("older", ""), str(out)],
            cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
        )
        if process.returncode != 0:
            why = f"seed_fork_source_changes.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
            for kind in ("built-in", "older"):
                if forks.pop(kind, None):
                    skipped[f"fork with source changes ({kind})"] = why
    for kind in ("built-in", "custom", "older"):
        if kind not in forks:
            continue
        entity = f"fork with source changes ({kind})"
        try:
            found = api("GET", f"/fleets/{forks[kind]}/source-changes", token)
            if not found.get("changed") or found.get("diff_available") is (kind == "older"):
                raise RuntimeError(f"source-changes answered {str(found)[:200]}")
            seeded[entity] = {"id": forks[kind], "url": f"{base}/fleets/{forks[kind]}"}
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped[entity] = str(exc)[:600]
    return seeded, skipped


#: The name of the archived fork of "Lab sample fleet" ``seed_archived_fleet`` writes.
ARCHIVED_FORK = "archived fork of the fleet"


def seed_archived_fleet(token: str, base: str, fleet: str) -> dict[str, dict[str, str]]:
    """One archived fleet, so the archived states render on real rows (no hand ``PUT``).

    *fleet* ("Lab sample fleet") is forked (``POST /fleets/<id>/fork``, members copied: the fork
    has its orchestrator and its own workspace), then archived (``PUT /fleets/<id>``
    ``is_archived`` true) and read back from ``GET /fleets/<id>``. The fleet page renders the
    Archived badge, the "(archived)" workspace chip and read-only members. The default lists
    stay unchanged: ``GET /fleets`` hides it unless ``include_archived=true``, which is also how
    a restarted lab finds it again by name. Logged ``seeded archived fleet: <url>``. May raise:
    the caller logs and skips.
    """
    name = f"{SEED_NAME} {ARCHIVED_FORK}"
    rows = api("GET", "/fleets?include_archived=true", token)
    found = next((f for f in rows if f.get("name") == name), None)
    archived = str(found["id"]) if found else _fork(token, fleet, ARCHIVED_FORK)
    api("PUT", f"/fleets/{archived}", token, {"is_archived": True})
    row = api("GET", f"/fleets/{archived}", token)
    if not isinstance(row, dict) or row.get("is_archived") is not True:
        raise RuntimeError(f"the fork was not archived: {str(row)[:200]}")
    return {"archived fleet": {"id": archived, "url": f"{base}/fleets/{archived}"}}


def seed_locked_fleet(token: str, base: str, peer: str) -> dict[str, dict[str, str]]:
    """One fleet the peer reads while its members stay private: the fleet page's locked state on real rows.

    The administrator owns the fleet and its two agents and shares the fleet alone, read-only, with
    the peer: ``GET /fleets/<id>/members`` answers the peer the rows with empty names, so the Graph
    tab renders "The members of this fleet are not shared with you". Open it with the peer's
    signed-in session (an administrator sees every agent). May raise: the caller logs and skips.
    """
    member = _seed_agent(token, "locked member")
    orchestrator = _seed_agent(token, "locked orchestrator", {member: "delegate"})
    fleet = _created(api("POST", "/fleets", token, {
        "name": f"{SEED_NAME} locked fleet",
        "description": "Seeded by the lab: shared with the peer, its agents are not.",
    }))
    root = _created(api("POST", f"/fleets/{fleet}/members", token, {
        "agent_id": orchestrator, "role_label": "Orchestrator",
    }))
    api("PUT", f"/fleets/{fleet}", token, {"orchestrator_agent_id": orchestrator})
    api("POST", f"/fleets/{fleet}/members", token, {
        "agent_id": member, "parent_member_id": root, "role_label": "Member",
    })
    api("PUT", f"/fleets/{fleet}", token, {
        "shared_user_ids": [peer], "shared_user_permissions": {peer: "read_only"},
    })
    return {"locked fleet": {"id": fleet, "url": f"{base}/fleets/{fleet}"}}


def seed_peer_not_set_up_fleet(token: str, base: str, peer: str) -> dict[str, dict[str, str]]:
    """One fleet the peer reads with its members, Not set up, and cannot manage (#9896).

    The administrator owns the fleet and its two agents, the orchestrator delegating to the member
    (a configured handover, so the fleet draws on the Fleets view of the agent-to-agent map), and
    shares the fleet and both agents read-only with the peer. No automation is created, so the
    fleet reads Not set up; a managers-only action on it (Set up) is seen as a reader. Read back
    from ``GET /fleets``: ``can_manage`` true for the administrator, false for the peer. Open it
    with the peer's signed-in session. Logged ``seeded peer read-only not set up fleet: <url>``.
    May raise: the caller logs and skips.
    """
    read_only = {"shared_user_ids": [peer], "shared_user_permissions": {peer: "read_only"}}
    member = _seed_agent(token, "not set up member")
    orchestrator = _seed_agent(token, "not set up orchestrator", {member: "delegate"})
    fleet = _created(api("POST", "/fleets", token, {
        "name": f"{SEED_NAME} fleet not set up, read-only for the peer",
        "description": "Seeded by the lab: no automation, shared read-only with the peer, its agents too.",
    }))
    root = _created(api("POST", f"/fleets/{fleet}/members", token, {
        "agent_id": orchestrator, "role_label": "Orchestrator",
    }))
    api("PUT", f"/fleets/{fleet}", token, {"orchestrator_agent_id": orchestrator})
    api("POST", f"/fleets/{fleet}/members", token, {
        "agent_id": member, "parent_member_id": root, "role_label": "Member",
    })
    for agent in (orchestrator, member):
        api("PUT", f"/agents/{agent}", token, read_only)
    api("PUT", f"/fleets/{fleet}", token, read_only)
    login = api("POST", "/auth/login", body={"email": PEER_EMAIL, "password": PEER_PASSWORD})
    peer_token = login.get("access_token") if isinstance(login, dict) else None
    if not peer_token:
        raise RuntimeError("the peer's sign-in answered no access token")
    for who, session, expected in (("administrator", token, True), ("peer", peer_token, False)):
        rows = api("GET", "/fleets", session)
        rows = rows.get("items", []) if isinstance(rows, dict) else rows
        found = next((row for row in rows if str(row.get("id")) == fleet), None)
        if found is None or bool(found.get("can_manage")) is not expected:
            raise RuntimeError(f"GET /fleets for the {who}: {str(found)[:200]}, can_manage {expected} expected")
    return {"peer read-only not set up fleet": {"id": fleet, "url": f"{base}/fleets/{fleet}"}}


def seed_prerequisites_unreadable_fleet(token: str, base: str, peer: str) -> dict[str, dict[str, str]]:
    """One fleet the peer reads whose About > Prerequisites holds lines not shared with the peer (#10258).

    The administrator owns the fleet, its two agents, a GitHub integration (lab-only token, never
    called) and a knowledge base, and shares the fleet and both agents read-only with the peer, not
    the integration nor the knowledge base: the member binds both, so ``GET
    /fleets/<id>/documentation`` answers the peer ``live_prerequisites`` lines in state
    ``unreadable`` ("Not shared with you", "Needed by" the member, no Fix). No agent account: creating
    one needs a signed-in delegated integration (a real provider call). Read back as the peer: at
    least one ``unreadable`` line naming the member, none of them ``can_fix``. Open it with the
    peer's signed-in session. Logged ``seeded fleet prerequisites unreadable for the peer: <url>``.
    May raise: the caller logs and skips.
    """
    read_only = {"shared_user_ids": [peer], "shared_user_permissions": {peer: "read_only"}}
    integration = _created(api("POST", "/integrations", token, {
        "name": f"{SEED_NAME} GitHub not shared with the peer", "provider": "github", "auth_type": "api_key",
        "description": "Seeded by the lab: bound by a fleet member, never shared with the peer.",
        "api_key": "lab-only-github-token",
    }))
    knowledge = _created(api("POST", "/knowledge-bases", token, {
        "name": f"{SEED_NAME} knowledge base not shared with the peer",
        "description": "Seeded by the lab: read by a fleet member, never shared with the peer.",
    }))
    member = _seed_agent(token, "prerequisites member")
    api("PUT", f"/agents/{member}", token, {
        "integration_bindings": {"github": [integration]}, "knowledge_base_ids": [knowledge],
    })
    orchestrator = _seed_agent(token, "prerequisites orchestrator", {member: "delegate"})
    fleet = _created(api("POST", "/fleets", token, {
        "name": f"{SEED_NAME} fleet with prerequisites not shared with the peer",
        "description": "Seeded by the lab: a member binds an integration and a knowledge base the peer cannot read.",
    }))
    root = _created(api("POST", f"/fleets/{fleet}/members", token, {
        "agent_id": orchestrator, "role_label": "Orchestrator",
    }))
    api("PUT", f"/fleets/{fleet}", token, {"orchestrator_agent_id": orchestrator})
    api("POST", f"/fleets/{fleet}/members", token, {
        "agent_id": member, "parent_member_id": root, "role_label": "Member",
    })
    for agent in (orchestrator, member):
        api("PUT", f"/agents/{agent}", token, read_only)
    api("PUT", f"/fleets/{fleet}", token, read_only)
    login = api("POST", "/auth/login", body={"email": PEER_EMAIL, "password": PEER_PASSWORD})
    peer_token = login.get("access_token") if isinstance(login, dict) else None
    if not peer_token:
        raise RuntimeError("the peer's sign-in answered no access token")
    lines = api("GET", f"/fleets/{fleet}/documentation", peer_token).get("live_prerequisites") or []
    unreadable = [line for line in lines if line.get("state") == "unreadable" and member in line.get("member_ids", [])]
    if not unreadable or any(line.get("can_fix") for line in unreadable):
        raise RuntimeError(f"GET /fleets/<id>/documentation for the peer: {str(lines)[:300]}, an unreadable line expected")
    return {"fleet prerequisites unreadable for the peer": {"id": fleet, "url": f"{base}/fleets/{fleet}"}}


def _manual_fleet(token: str, name: str, read_only: dict) -> str:
    """An orchestrator over one member and a fleet over them, the three shared read-only with the peer."""
    member = _seed_agent(token, f"{name} member")
    orchestrator = _seed_agent(token, f"{name} orchestrator", {member: "delegate"})
    fleet = _created(api("POST", "/fleets", token, {
        "name": f"{SEED_NAME} fleet with {name}",
        "description": "Seeded by the lab: its About page lists manual prerequisites a fleet manager confirms.",
    }))
    root = _created(api("POST", f"/fleets/{fleet}/members", token, {"agent_id": orchestrator, "role_label": "Orchestrator"}))
    api("PUT", f"/fleets/{fleet}", token, {"orchestrator_agent_id": orchestrator})
    api("POST", f"/fleets/{fleet}/members", token, {"agent_id": member, "parent_member_id": root, "role_label": "Member"})
    for agent in (orchestrator, member):
        api("PUT", f"/agents/{agent}", token, read_only)
    api("PUT", f"/fleets/{fleet}", token, read_only)
    return fleet


def seed_fleet_manual_prerequisites(token: str, base: str, peer: str) -> dict[str, dict[str, str]]:
    """Two fleets whose About page lists documented ``manual`` prerequisites in each state (#10509).

    No lab fleet documented a manual line that survives ``covered_by_configuration``, so the To
    confirm, confirmed (Undo), expired and reader states of About > Prerequisites (#10469) had no
    row. ``live_prerequisites`` keeps two manual lines per fleet, so ``seed_fleet_manual_prerequisites.py``
    writes, through the product's writers, on the first fleet one line confirmed a few days ago by
    the administrator and one unconfirmed (one-line Confirm in the band), on the second one line
    confirmed over 90 days ago (expired) and one unconfirmed ("2 to confirm"). Both fleets and
    their agents are shared read-only with the peer, who reads "A fleet manager confirms this"
    with no button. Read back from ``GET /fleets/<id>/documentation`` as both identities. Logged
    ``seeded fleet manual prerequisites: <url>`` and ``seeded fleet manual prerequisites to
    confirm: <url>``. May raise: the caller logs and skips.
    """
    read_only = {"shared_user_ids": [peer], "shared_user_permissions": {peer: "read_only"}}
    fleets = [_manual_fleet(token, name, read_only) for name in ("manual prerequisites", "manual prerequisites to confirm")]
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "fleet_manual_prerequisites.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_fleet_manual_prerequisites.py"), admin, *fleets,
         str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_fleet_manual_prerequisites.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    login = api("POST", "/auth/login", body={"email": PEER_EMAIL, "password": PEER_PASSWORD})
    peer_token = login.get("access_token") if isinstance(login, dict) else None
    if not peer_token:
        raise RuntimeError("the peer's sign-in answered no access token")
    # The first fleet: one current confirmation and one line to confirm; the second: the expired
    # confirmation is back to confirm (who and when kept), beside the unconfirmed line.
    for fleet, expected in zip(fleets, (["met", "to_confirm"], ["to_confirm", "to_confirm"]), strict=True):
        for who, reader, manager in (("administrator", token, True), ("peer", peer_token, False)):
            lines = api("GET", f"/fleets/{fleet}/documentation", reader).get("live_prerequisites") or []
            manual = [line for line in lines if line.get("kind") == "manual"]
            states = sorted(line.get("state") for line in manual)
            confirmed = [line for line in manual if line.get("confirmed_at")]
            if (states != expected or len(confirmed) != 1
                    or any(bool(line.get("can_confirm")) != manager for line in manual)):
                raise RuntimeError(f"GET /fleets/{fleet}/documentation as the {who}: manual lines {str(manual)[:300]}")
    first, second = fleets
    return {
        "fleet manual prerequisites": {"id": first, "url": f"{base}/fleets/{first}"},
        "fleet manual prerequisites to confirm": {"id": second, "url": f"{base}/fleets/{second}"},
    }


def seed_peer_managed_fleet_enable(base: str, opencti: str) -> dict[str, dict[str, str]]:
    """One fleet the peer owns and manages, its two automations off: Enable fleet for a non-admin.

    Every other fleet the peer manages has no automation, so ``POST /fleets/{id}/enable`` answered
    the peer 403 or nothing to enable and the non-admin state of the Enable fleet dialog (Run as
    as a labelled read-only value) had no row to render on. Signed in as the peer: two agents, the
    orchestrator delegating to the member, a fleet over them, and two automations created off, a
    weekly schedule and an event of the registered OpenCTI integration (*opencti*, a module every
    lab user holds). Read back: the peer's dry run (no ``run_as_user_id``: the caller) answers
    ``changed`` 2, ``ready`` and no ``missing``. Open it with the peer's signed-in session. Logged
    ``seeded peer-managed fleet to enable: <url>``. May raise: the caller logs and skips.
    """
    login = api("POST", "/auth/login", body={"email": PEER_EMAIL, "password": PEER_PASSWORD})
    peer_token = login.get("access_token") if isinstance(login, dict) else None
    if not peer_token:
        raise RuntimeError("the peer's sign-in answered no access token")
    events = api("GET", "/integrations/events", peer_token)
    listed = events.get("opencti") if isinstance(events, dict) else None
    if not listed:
        raise RuntimeError("GET /integrations/events lists the peer no opencti event")
    member = _seed_agent(peer_token, "peer enable member")
    orchestrator = _seed_agent(peer_token, "peer enable orchestrator", {member: "delegate"})
    fleet = _created(api("POST", "/fleets", peer_token, {
        "name": f"{SEED_NAME} peer-managed fleet to enable",
        "description": "Seeded by the lab: owned and managed by the peer, its two automations off.",
    }))
    root = _created(api("POST", f"/fleets/{fleet}/members", peer_token, {
        "agent_id": orchestrator, "role_label": "Orchestrator",
    }))
    api("PUT", f"/fleets/{fleet}", peer_token, {"orchestrator_agent_id": orchestrator})
    api("POST", f"/fleets/{fleet}/members", peer_token, {
        "agent_id": member, "parent_member_id": root, "role_label": "Member",
    })
    automations = (
        (orchestrator, "peer weekly digest", "schedule", {"cron": "0 8 * * 1", "timezone": "UTC"}),
        (member, "peer OpenCTI event", "integration_event",
         {"provider": "opencti", "event": listed[0]["event"], "integration_id": opencti}),
    )
    for agent, name, kind, config in automations:
        _created(api("POST", "/assignments", peer_token, {
            "agent_id": agent, "name": f"{SEED_NAME} {name}", "description": "Seeded by the lab.",
            "prompt": "Summarise what started you.", "trigger_type": kind, "trigger_config": config,
            "enabled": False,
        }))
    plan = api("POST", f"/fleets/{fleet}/enable", peer_token, {"dry_run": True})
    if (not isinstance(plan, dict) or plan.get("changed") != len(automations)
            or not plan.get("ready") or plan.get("missing")):
        raise RuntimeError(f"the peer's enable dry run answered {str(plan)[:200]}")
    return {"peer-managed fleet to enable": {"id": fleet, "url": f"{base}/fleets/{fleet}"}}


#: The lab-only run-as user of Enable fleet who misses OpenCRQ (#9970); never signs in.
NO_CRQ_EMAIL = "lab-no-opencrq@filigran.io"
NO_CRQ_PASSWORD = "lab-only-no-opencrq-password-1"
NO_CRQ_NAME = "Lab user without OpenCRQ"
#: The group the registered OpenCRQ integration stays granted to: every other non-admin user.
LAB_CRQ_GROUP = "Lab OpenCRQ users"


def seed_fleet_enable_missing_module(token: str, base: str, opencrq: str) -> dict[str, dict[str, str]]:
    """A fleet whose Enable fleet dry run, run as a lab-only user, reports OpenCRQ missing (#9970).

    Every lab user holds the three modules, so the missing-modules state of the Enable fleet dialog
    (alert line, Confirm disabled, #9769 S4b) had no row to render on. The user ``NO_CRQ_EMAIL`` is
    created; the registered OpenCRQ integration (*opencrq*) is narrowed with ``access_restricted``
    to the group ``LAB_CRQ_GROUP``, which holds every other non-admin user, so nobody else loses it
    (an administrator sees every integration). Read back: the first fleet whose ``POST
    /fleets/{id}/enable`` dry run as that user answers ``missing`` non-empty. Logged ``seeded fleet
    enable missing module: <url> (run as <email>)``. May raise: the caller logs and skips.
    """
    user = _created(api("POST", "/users", token, {
        "email": NO_CRQ_EMAIL, "full_name": NO_CRQ_NAME, "password": NO_CRQ_PASSWORD, "role": "user",
    }))
    users = api("GET", "/users", token)
    others = [str(u["id"]) for u in users if u.get("role") != "admin" and str(u["id"]) != user]
    group = _created(api("POST", "/groups", token, {"name": LAB_CRQ_GROUP}))
    if others:
        api("POST", f"/groups/{group}/members", token, {"user_ids": others})
    api("PUT", f"/integrations/{opencrq}", token, {
        "access_restricted": True, "shared_group_ids": [group], "shared_group_permissions": {group: "read_only"},
    })
    fleets = api("GET", "/fleets", token)
    fleets = fleets.get("items", []) if isinstance(fleets, dict) else fleets
    for fleet in fleets:
        try:
            plan = api("POST", f"/fleets/{fleet['id']}/enable", token, {"run_as_user_id": user, "dry_run": True})
        except RuntimeError:
            continue  # nothing off, built-in read-only or not manageable: not this one
        if isinstance(plan, dict) and plan.get("missing"):
            url = f"{base}/fleets/{fleet['id']}"
            return {"fleet enable missing module": {"id": str(fleet["id"]), "url": f"{url} (run as {NO_CRQ_EMAIL})"}}
    raise RuntimeError(f"no fleet's enable dry run as {NO_CRQ_EMAIL} reports a missing module ({len(fleets)} fleet(s))")


def _shared_workspace(owner_token: str, grantee: str, owner: str) -> str:
    workspace = _created(api("POST", "/workspaces", owner_token, {
        "name": f"{SEED_NAME} workspace of the {owner}",
        "description": f"Seeded by the lab: owned by the {owner}, shared read-only.",
    }))
    _share_read_only(owner_token, workspace, grantee)
    return workspace


def seed_script_env() -> dict[str, str]:
    """The environment of a script the lab starts by path with the backend's interpreter.

    A script started by path has its own directory on ``sys.path``, not the working directory,
    and the image installs the backend's dependencies without the project itself, so ``app``
    is importable only with ``backend/`` on ``PYTHONPATH`` (uvicorn gets it from ``-m``).
    """
    env = {**os.environ, **backend_env()}
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(CHECKOUT / "backend"), env.get("PYTHONPATH", "")) if p)
    return env


def stderr_summary(stderr: str, limit: int = 500) -> str:
    """The exception line first, then the end of the traceback: the log names the error."""
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    if not lines:
        return "no output"
    tail = " / ".join(lines[:-1])[-(limit - min(len(lines[-1]), limit // 2)):]
    return f"{lines[-1][: limit // 2]} (after: {tail})" if tail else lines[-1][:limit]


def seed_profile_memory(token: str, agent: str, base: str) -> dict[str, dict[str, str]]:
    """The administrator's memory: Profile > Memory renders Purge All and two Clear buttons (#10210).

    No API writes a user's memory: ``seed_profile_memory.py`` writes one cross-agent profile statement
    and one memory of *agent* scoped to the administrator through the backend's own service code, with
    its interpreter and environment. Read back on ``GET /settings/me/memory``: the profile's
    ``medium_term`` is set and *agent* is listed. May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "profile_memory.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_profile_memory.py"), admin, agent, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_profile_memory.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    memory = api("GET", "/settings/me/memory", token)
    profile = memory.get("user_profile") if isinstance(memory, dict) else None
    agents = memory.get("agents") if isinstance(memory, dict) else None
    if not isinstance(profile, dict) or not profile.get("medium_term"):
        raise RuntimeError("stored, but GET /settings/me/memory answers no cross-agent profile")
    if not isinstance(agents, list) or agent not in {row.get("agent_id") for row in agents if isinstance(row, dict)}:
        raise RuntimeError(f"stored, but GET /settings/me/memory does not list agent {agent}")
    return {"profile memory": {"id": admin, "url": f"{base}/profile/memory"}}


#: ``LAB_XTM_MCP``: ``1`` (default) turns the platform tools MCP server on, ``0`` leaves the platform default (off).
LAB_XTM_MCP_STATES = ("1", "0")


def seed_xtm_mcp(token: str, base: str) -> dict[str, dict[str, str]]:
    """The platform tools MCP server on, so /profile/mcp renders its card (#10210); none under ``LAB_XTM_MCP=0``.

    Off by default on a platform (an admin turns it on), and Profile hides the card while it is off.
    Turned on through the real ``PUT /settings/general-assistant`` (``xtm_mcp_enabled``), read back on
    ``GET /settings/general-assistant``. It is a security control of the audit page: ``0`` keeps it
    off for a review of that page. May raise: the caller logs and skips.
    """
    if lab_switch("LAB_XTM_MCP", LAB_XTM_MCP_STATES) == "0":
        return {}
    api("PUT", "/settings/general-assistant", token, {"xtm_mcp_enabled": True})
    config = api("GET", "/settings/general-assistant", token)
    if not isinstance(config, dict) or config.get("xtm_mcp_enabled") is not True:
        raise RuntimeError("GET /settings/general-assistant answers xtm_mcp_enabled off after the change")
    return {"Platform Tools MCP Server": {"id": "xtm_mcp_enabled", "url": f"{base}/profile/mcp"}}


#: ``LAB_PLATFORM_HEALTH``: ``off`` (default) leaves Platform Health off, as on a new platform; ``on`` turns it on
#: (#101 S15d-3), so the module round of the registered modules runs in ``[service.lab-module-health]``.
LAB_PLATFORM_HEALTH_STATES = ("off", "on")


def seed_platform_health(token: str, base: str) -> dict[str, dict[str, str]]:
    """Platform Health on under ``LAB_PLATFORM_HEALTH=on`` (#101 S15d-3); nothing otherwise.

    Turned on through the real ``GET`` then ``PUT /health/settings`` (every other setting kept as read, the
    module checks included), read back on ``GET /health/settings``. May raise: the caller logs and skips.
    """
    if lab_switch("LAB_PLATFORM_HEALTH", LAB_PLATFORM_HEALTH_STATES) != "on":
        return {}
    settings = api("GET", "/health/settings", token)
    if not isinstance(settings, dict):
        raise RuntimeError("GET /health/settings answered no settings object")
    api("PUT", "/health/settings", token, {**settings, "enabled": True})
    after = api("GET", "/health/settings", token)
    if not isinstance(after, dict) or after.get("enabled") is not True:
        raise RuntimeError("GET /health/settings answers Platform Health off after the change")
    return {"Platform Health": {"id": "enabled", "url": f"{base}/health"}}


def seed_health_findings(token: str, base: str) -> dict[str, dict[str, str]]:
    """Open, dismissed and resolved Platform Health findings on real rows, XTM One mode only (#1303).

    A check opens a finding on its second failing pass and the Module Analyst needs a live model, so a fresh lab
    shows 0 open findings everywhere: ``seed_health_findings.py`` writes them through the engine's own writer.
    Read back on ``GET /health/overview`` (open counts above 0) and ``GET /health/findings`` (every open id
    listed). Logged ``seeded health findings: <url>``. May raise: the caller logs and skips.
    """
    if os.environ.get("PLATFORM_MODE", "") != "xtm_one":
        return {}
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "health_findings.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_health_findings.py"), admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_health_findings.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    written = json.loads(out.read_text())
    overview = api("GET", "/health/overview", token)
    cards = overview.get("modules") if isinstance(overview, dict) else None
    if not sum(card.get("critical", 0) + card.get("warning", 0) + card.get("info", 0) for card in cards or []):
        raise RuntimeError("GET /health/overview answers 0 open findings after the seed")
    listed = api("GET", "/health/findings?limit=200", token)
    shown = {f.get("id") for f in (listed.get("items") if isinstance(listed, dict) else None) or []}
    if not written["open"] or set(written["open"]) - shown:
        raise RuntimeError(f"GET /health/findings lists {len(shown)} of the {len(written['open'])} seeded open findings")
    url = f"{base}/health"
    return {
        "health findings": {"id": ",".join(written["open"]), "url": url},
        "health findings dismissed": {"id": ",".join(written["dismissed"]), "url": url},
        "health findings resolved": {"id": ",".join(written["resolved"]), "url": url},
    }


#: The agent whose welcome screen shows suggestion chips: ``GET /chat/suggestions`` answers an agent's own
#: ``web_suggested_prompts`` and nothing else, so with no agent carrying some the chatbot's welcome screen
#: never renders its chips. The third is long enough for the design system's Chip to cut it at 250 px.
#: ``lab-chatbot-host.tsx`` opens on this slug when its URL names no ``agent``.
LAB_SUGGESTIONS_AGENT_SLUG = "lab-chat-suggestions"
LAB_CHAT_SUGGESTIONS = [
    "Summarize the latest threat report",
    "List the open vulnerabilities on my assets",
    "Draft an executive briefing on the ransomware campaigns that targeted our sector this quarter",
]


def seed_chat_suggestions(token: str, base: str) -> dict[str, dict[str, str]]:
    """One chat agent with three suggested actions, so the chat welcome screen renders its chips (no fetch override).

    Created through the real ``POST /agents`` (slug ``LAB_SUGGESTIONS_AGENT_SLUG``), its prompts set through
    ``PUT /agents/{id}`` (the create body carries none), read back on ``GET /chat/suggestions``. The lab-only
    chatbot host opens on it; ``/chat?agent=<id>`` shows it in the app. May raise: the caller logs and skips.
    """
    agent = _created(api("POST", "/agents", token, {
        "name": f"{SEED_NAME} chat suggestions", "slug": LAB_SUGGESTIONS_AGENT_SLUG,
        "description": "Seeded by the lab: its welcome screen shows suggestion chips.",
        "persona": "You are the lab's chat agent.",
    }))
    api("PUT", f"/agents/{agent}", token, {"web_suggested_prompts": LAB_CHAT_SUGGESTIONS})
    answered = api("GET", f"/chat/suggestions?agent_slug={LAB_SUGGESTIONS_AGENT_SLUG}", token)
    if answered != LAB_CHAT_SUGGESTIONS:
        raise RuntimeError(f"stored, but GET /chat/suggestions answers {str(answered)[:200]}")
    log(f"seeded chat suggestions: {LAB_SUGGESTIONS_AGENT_SLUG}")
    return {"chat suggestions": {"id": agent, "url": f"{base}/chat?agent={agent}"}}


#: The deck ``seed_presentation`` imports in Copilot mode: a cover, a bullet slide and a table slide on the
#: Filigran Brand Deck (slug ``filigran-brand``, seeded in Copilot mode only), so the presentation export gate
#: (PDF and PPTX) runs in a lab with no model. A bare PresentationSpec, the shape an agent's deck has.
LAB_PRESENTATION_TITLE = f"{SEED_NAME} deck"
LAB_PRESENTATION_TEMPLATE = {"slug": "filigran-brand", "name": "Filigran Brand Deck"}
LAB_PRESENTATION_SPEC = {
    "title": LAB_PRESENTATION_TITLE,
    "theme": "dark",
    "aspect_ratio": "16:9",
    "slides": [
        {"layout": "title", "title": LAB_PRESENTATION_TITLE, "subtitle": "Seeded by the lab for the export gate"},
        {"layout": "content", "title": "What the lab checks", "blocks": [{"type": "list", "items": [
            "The deck opens in the viewer",
            "The PDF export answers a file",
            "The PPTX export answers a file",
        ]}]},
        {"layout": "content", "title": "Export formats", "blocks": [{
            "type": "table",
            "headers": ["Format", "Action", "Body"],
            "rows": [
                ["PDF", "Export", "format pdf"],
                ["PPTX", "Export", "format pptx"],
            ],
        }]},
    ],
}


def seed_presentation(token: str, base: str) -> dict[str, dict[str, str]]:
    """Import one deck on the Filigran Brand Deck through ``POST /presentations/import``; answer its row.

    The library's own import route (the one "Import" on the presentations page calls), with the
    package envelope naming the template, so no chat and no model are involved. Found again by
    title on a restart (``GET /presentations?search=``): idempotent. The import must match the
    Brand Deck (``import_report.template`` ``matched``), else it raises: a deck on another template
    would pass the export gate on the wrong design. Logged ``seeded presentation: <base>/presentations/<id>``.
    May raise: the caller logs and skips.
    """
    found = api("GET", "/presentations?" + urllib.parse.urlencode({"search": LAB_PRESENTATION_TITLE, "mine": "true"}), token)
    rows = found.get("items", []) if isinstance(found, dict) else []
    existing = next((row for row in rows if isinstance(row, dict) and row.get("title") == LAB_PRESENTATION_TITLE), None)
    if existing is not None:
        deck = str(existing["id"])
    else:
        package = json.dumps({
            "export_type": "presentation", "version": 1, "source_platform": "lab",
            "presentation": {"spec": LAB_PRESENTATION_SPEC, "template": LAB_PRESENTATION_TEMPLATE, "assets": []},
        }).encode()
        boundary = f"lab-{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"lab.presentation.json\"\r\n"
            "Content-Type: application/json\r\n\r\n"
        ).encode() + package + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{API_PORT}/api/v1/presentations/import", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                answer = json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"POST /presentations/import: {exc.code} {exc.read()[:200].decode('utf-8', 'replace')}") from exc
        report = answer.get("import_report") if isinstance(answer, dict) else None
        if not isinstance(report, dict) or report.get("template") != "matched":
            raise RuntimeError(f"POST /presentations/import did not match the Filigran Brand Deck: {str(report)[:200]}")
        deck = _created(answer)
    # The speaker view and its clicker control (ClickerControls) on the same deck (#11252).
    return {
        "presentation": {"id": deck, "url": f"{base}/presentations/{deck}"},
        "deck": {"id": deck, "url": f"{base}/presentations/{deck}/speaker"},
    }


def seed_frictions(token: str, agent: str, base: str) -> dict[str, dict[str, str]]:
    """Two runs of *agent* with a friction ledger, so the run page's Frictions card renders on real rows.

    No API records a friction for a run: ``seed_frictions.py`` writes them through the backend's
    own service code, with its interpreter and environment. The disposed friction names a work
    item created here through the API. May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    item = _created(api("POST", "/work-items", token, {
        "title": f"{SEED_NAME} friction fix", "description": "Seeded by the lab: the fix a seeded friction names.",
    }))
    out = ROOT / "frictions.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_frictions.py"), agent, admin, item, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_frictions.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    runs = json.loads(out.read_text())
    return {
        "run with frictions": {"id": runs["settled"], "url": f"{base}/operations/executions/{runs['settled']}"},
        "run with a live friction ledger": {"id": runs["live"], "url": f"{base}/operations/executions/{runs['live']}"},
    }


def seed_relay_requests(token: str, group: str, base: str) -> dict[str, dict[str, str]]:
    """Two relays and 120 request log rows on the sample relay group, so its Activity tab renders real rows (#10763).

    No API writes a relay or a ``relay_request_logs`` row: ``seed_relay_requests.py`` writes them
    through the backend's own models, metadata only (no header, no body, no usable key). Read back
    on ``GET /relays/groups/{id}/requests``; logged ``seeded relay requests: <url>``. May raise.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "relay_requests.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_relay_requests.py"), group, admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_relay_requests.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    written = json.loads(out.read_text())
    page = api("GET", f"/relays/groups/{group}/requests?limit=1", token)
    total = page.get("total") if isinstance(page, dict) else None
    if total != written["requests"]:
        raise RuntimeError(f"GET /relays/groups/{group}/requests answers total {total}, {written['requests']} written")
    url = f"{base}/connections/relays/{group}/activity"
    log(f"seeded relay requests: {url}")
    return {
        "relay group activity": {"id": group, "url": url},
        "relay with a long name": {"id": written["relays"][0], "url": f"{base}/connections/relays/{group}"},
    }


#: The two skills the sample placebo comparison targets one at a time (``kind: skill``).
LAB_PLACEBO_SKILLS = ("lab-sample-placebo-triage-skill", "lab-sample-placebo-reporting-skill")


def seed_placebo_comparisons(token: str, evaluation: str, agent: str, base: str) -> dict[str, dict[str, str]]:
    """Three settled placebo comparisons on the sample evaluation, so its Placebo comparison card renders.

    No API writes a settled comparison (a launch runs real models): ``seed_placebo_comparisons.py``
    writes them through the backend's own model, with two skills created here through the API as
    the single-skill targets. Read back on ``GET /evaluations/{id}/placebo-comparisons``; logged
    ``seeded placebo comparisons (3): <url>``. May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    skills = [
        _created(api("POST", "/skills", token, {
            "name": name, "description": "Seeded by the lab: a placebo comparison target.",
            "content": f"---\nname: {name}\ndescription: Seeded by the lab.\n---\n\n# Lab sample\n\nAnswer in one line.\n",
        }))
        for name in LAB_PLACEBO_SKILLS
    ]
    out = ROOT / "placebo_comparisons.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_placebo_comparisons.py"), evaluation, admin, agent,
         *skills, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_placebo_comparisons.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    written = json.loads(out.read_text())
    listed = api("GET", f"/evaluations/{evaluation}/placebo-comparisons?limit=10", token)
    shown = [row.get("id") for row in listed] if isinstance(listed, list) else []
    if not set(written) <= set(shown):
        raise RuntimeError(f"GET /evaluations/{evaluation}/placebo-comparisons lists {len(shown)}, {len(written)} written")
    url = f"{base}/evaluations/{evaluation}"
    log(f"seeded placebo comparisons ({len(written)}): {url}")
    return {"placebo comparisons": {"id": evaluation, "url": url}}


def seed_evaluation_runs(token: str, evaluation: str, agent: str, dataset: str, base: str) -> dict[str, dict[str, str]]:
    """A completed and a running run on the sample evaluation, so the run detail opens on real rows.

    No API finishes a run without a real model: ``seed_evaluation_runs.py`` writes them through the
    backend's own model. Read back on ``GET /evaluations/runs/{id}`` and the completed one on
    ``POST /evaluations/runs/search``; logged ``seeded evaluation runs: <url>`` once per run. May
    raise: the caller logs and skips.
    """
    out = ROOT / "evaluation_runs.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_evaluation_runs.py"), evaluation, agent, dataset,
         str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_evaluation_runs.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    completed, running = json.loads(out.read_text())
    for run, status in ((completed, "completed"), (running, "running")):
        shown = api("GET", f"/evaluations/runs/{run}", token)
        if not isinstance(shown, dict) or shown.get("status") != status:
            raise RuntimeError(f"GET /evaluations/runs/{run} does not answer a {status} run")
    found = api("POST", "/evaluations/runs/search", token, {"evaluation_id": evaluation, "limit": 10})
    if completed not in {row.get("id") for row in (found or {}).get("runs", [])}:
        raise RuntimeError("POST /evaluations/runs/search does not find the completed run")
    seeded = {}
    for name, run in (("evaluation run completed", completed), ("evaluation run running", running)):
        url = f"{base}/evaluations/runs/{run}"
        log(f"seeded evaluation runs: {url}")
        seeded[name] = {"id": run, "url": url}
    return seeded


def seed_roi_records(token: str, base: str) -> dict[str, dict[str, str]]:
    """Four reviewed and unreviewed runs and a done work item with an ROI record (issue #9394).

    So the run page's outcome review row, the runs list's Unreviewed filter and the work item
    dialog render on real rows. A lab-owned agent and assignment carry the runs, so their Activity
    tabs list them. No API writes a ``roi_run_records`` row: ``seed_roi_records.py`` writes them
    through the backend's own models, with its interpreter and environment. Logged as
    ``seeded ROI review run (<outcome>)`` and ``seeded ROI review work item``. The seed also writes
    ten weeks of back-dated runs, logged as ``seeded ROI trend weeks: N``, so the Value page's
    weekly trend draws (#10067). May raise: the caller logs and skips.
    """
    agent = _seed_agent(token, "ROI analyst")
    assignment = _created(api("POST", "/assignments", token, {
        "agent_id": agent, "name": f"{SEED_NAME} ROI triage", "description": "Seeded by the lab.",
        "prompt": "Triage the new threat reports.", "trigger_type": "manual", "trigger_config": {},
    }))
    admin = _created(api("GET", "/auth/me", token))
    item = _created(api("POST", "/work-items", token, {
        "title": f"{SEED_NAME} ROI review item", "description": "Seeded by the lab: a done item with an ROI record.",
    }))
    api("POST", f"/work-items/{item}/complete", token, {"result": "Seeded by the lab: one report triaged."})
    out = ROOT / "roi_records.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_roi_records.py"), agent, assignment, admin, item,
         str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_roi_records.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    rows = json.loads(out.read_text())
    # Back-dated weekly history, so the Value page's trend draws with no override (#10067).
    log(f"seeded ROI trend weeks: {rows.get('trend_weeks', 0)}")
    seeded = {
        f"ROI review run ({outcome})": {"id": run, "url": f"{base}/operations/executions/{run}"}
        for outcome, run in rows["runs"].items()
    }
    seeded["ROI review work item"] = {"id": item, "url": f"{base}/tasks/work-items/{item}"}
    seeded["ROI review agent"] = {"id": agent, "url": f"{base}/agents/{agent}"}
    seeded["ROI review assignment"] = {"id": assignment, "url": f"{base}/agents/assignments/{assignment}"}
    return seeded


#: The person with a name over 40 characters the ROI chat lines are attributed to (#10696).
ROI_CHAT_LONG_USER = ("lab-roi-chat-long-name@filigran.io", "Lab ROI chat author with a deliberately long display name")
#: The agent with a name over 40 characters the ROI chat lines are attributed to (#10696).
ROI_CHAT_LONG_AGENT = "ROI chat writer with a deliberately long agent name for truncation"


def seed_roi_chat_records(
    token: str, base: str, review_agent: str, peer_id: str,
) -> dict[str, dict[str, str]]:
    """"From chat, estimated" lines, so the ROI chat card and its Adjust dialog render on real rows (#10696).

    No API writes a ``roi_chat_records`` row: ``seed_roi_chat_records.py`` writes eight through the
    backend's models (every kind, counted and waiting lines, two corrected lines passing 1,000 hours,
    a 100+ character title), attributed to *review_agent* (the ROI review agent), to the first member
    of "Lab sample fleet" and to a long-named agent; owned by the administrator, the peer *peer_id*
    and a long-named person. Each line carries its agent's home fleet (``fleet_backfilled`` false), as
    the platform's writer stamps it: the lab runs no backfill, so the Lab sample fleet lines are stamped
    at seed or never (dev:harness-gaps lab-seed-roi-chat-records-fleet-stamp); none stamped raises.
    ``GET /roi/chat`` is read back. Logged as ``seeded ROI chat records (N): <base>/value?scope=organization``
    and ``seeded fleet chat stamped: <n>``. May raise: the caller logs and skips.
    """
    fleet_agent = LAB_AGENTS.get(FLEET_MEMBERS[0])
    if not fleet_agent:
        raise RuntimeError("no Lab sample fleet member: the fleet seed did not run")
    email, name = ROI_CHAT_LONG_USER
    long_user = _created(api("POST", "/users", token, {
        "email": email, "full_name": name, "password": ANALYTICS_PASSWORD, "role": "user",
    }))
    ids = {
        "admin": _created(api("GET", "/auth/me", token)), "peer": peer_id, "long_user": long_user,
        "review_agent": review_agent, "fleet_agent": fleet_agent,
        "long_agent": _seed_agent(token, ROI_CHAT_LONG_AGENT),
    }
    ids_file, out = ROOT / "roi_chat_ids.json", ROOT / "roi_chat_records.json"
    ids_file.write_text(json.dumps(ids))
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_roi_chat_records.py"), str(ids_file), str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_roi_chat_records.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    written = json.loads(out.read_text())
    records, stamped = written["records"], written.get("fleet_stamped") or 0
    if not stamped:
        raise RuntimeError("seed_roi_chat_records.py stamped no line with a home fleet")
    series = api("GET", "/roi/chat?window=30d", token)
    total = series.get("total_lines") if isinstance(series, dict) else None
    if total is None or total < len(records):
        raise RuntimeError(f"GET /roi/chat answered {total} lines of {len(records)} seeded")
    url = f"{base}/tasks/value?scope=organization"
    log(f"seeded ROI chat records ({len(records)}): {url}")
    log(f"seeded fleet chat stamped: {stamped}")
    return {"ROI chat records": {"id": ",".join(records.values()), "url": url}}


#: The unit "Lab sample fleet" declares (a built-in catalog unit, counted once per run).
ROI_FLEET_UNIT = "threat_report_triaged"
#: The two ``LAB_FIGURES_SCALE=large`` ROI fleets: Value figures in K and M, then a negative net value.
LARGE_ROI_FLEETS = ("large ROI fleet", "negative ROI fleet")


def seed_large_roi(token: str, base: str) -> dict[str, dict[str, str]]:
    """Two fleets whose ROI figures and quota estimate reach 1,000 and more (#10749).

    Each fleet has one member with one automation switched on and declares the Lab sample fleet's
    unit; ``seed_large_roi.py`` writes their ``roi_run_records`` rows, the negative fleet's LLM spend
    and the quota lines of both. Read back from ``GET /roi?scope=fleet`` (runs and units of 1,000
    and more, hours saved of 1,000 and more and a value of 1M and more on the first, a negative net
    value on the second) and ``/fleets/<id>/documentation``. Logged ``seeded large ROI fleet:
    <base>/flows/fleets/<id>``. The other seeded fleets keep their figures. May raise: the caller
    logs and skips.
    """
    fleets: list[str] = []
    members: list[str] = []
    for name in LARGE_ROI_FLEETS:
        member = _seed_agent(token, f"{name} member")
        _created(api("POST", "/assignments", token, {
            "agent_id": member, "name": f"{SEED_NAME} {name} triage", "description": "Seeded by the lab.",
            "prompt": "Triage the new threat reports.", "trigger_type": "manual", "trigger_config": {},
            "enabled": True,
        }))
        fleet = _created(api("POST", "/fleets", token, {
            "name": f"{SEED_NAME} {name}", "description": "Seeded by the lab: ROI figures of 1,000 and more.",
        }))
        _created(api("POST", f"/fleets/{fleet}/members", token, {"agent_id": member, "role_label": "Member"}))
        api("PUT", f"/roi/declarations?subject_type=fleet&subject_id={fleet}", token, {
            "unit": ROI_FLEET_UNIT, "count_mode": "per_run", "units_per_count": 1,
        })
        fleets.append(fleet)
        members.append(member)
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "large_roi.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_large_roi.py"), admin, fleets[0], members[0],
         fleets[1], members[1], str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=300,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_large_roi.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    for fleet in fleets:
        quota = (api("GET", f"/fleets/{fleet}/documentation", token).get("structured") or {}).get("quota") or []
        if sum(int(q.get("runs_max") or 0) for q in quota) < 1000:
            raise RuntimeError(f"GET /fleets/{fleet}/documentation: quota {str(quota)[:300]}")
    large, negative = (
        api("GET", f"/roi?window=30d&currency=USD&scope=fleet&id={fleet}", token).get("organization") or {}
        for fleet in fleets
    )
    if min(large.get("runs") or 0, large.get("units") or 0, large.get("hours_saved") or 0) < 1000 or (
        large.get("value") or 0
    ) < 1_000_000:
        raise RuntimeError(f"GET /roi large fleet: {str(large)[:300]}")
    if (negative.get("runs") or 0) < 1000 or (negative.get("net_value") or 0) > -1_000_000:
        raise RuntimeError(f"GET /roi negative fleet: {str(negative)[:300]}")
    log(f"seeded large ROI fleet: {base}/flows/fleets/{fleets[0]}")
    return {
        name: {"id": fleet, "url": f"{base}/flows/fleets/{fleet}"}
        for name, fleet in zip(LARGE_ROI_FLEETS, fleets, strict=True)
    }


def seed_roi_fleet(token: str, base: str, fleet: str) -> dict[str, dict[str, str]]:
    """*fleet* ("Lab sample fleet") declares an ROI unit and has counted runs, so ROI has a fleet row (#9734).

    ``PUT /roi/declarations?subject_type=fleet`` declares the unit; a lab assignment of the fleet's
    orchestrator carries the runs, and ``seed_roi_fleet.py`` writes their ``roi_run_records`` rows
    with the fleet through the backend's own models (no API writes one). ``GET /roi?window=30d`` then
    answers the fleet in ``fleets``. Logged as ``seeded ROI fleet: <base>/observability/outcomes``. May
    raise: the caller logs and skips.
    """
    orchestrator = LAB_AGENTS.get("Orchestrator")
    if not orchestrator:
        raise RuntimeError("no Orchestrator agent: the fleet seed did not run")
    api("PUT", f"/roi/declarations?subject_type=fleet&subject_id={fleet}", token, {
        "unit": ROI_FLEET_UNIT, "count_mode": "per_run", "units_per_count": 1,
    })
    assignment = _created(api("POST", "/assignments", token, {
        "agent_id": orchestrator, "name": f"{SEED_NAME} fleet ROI triage", "description": "Seeded by the lab.",
        "prompt": "Triage the new threat reports with the fleet.", "trigger_type": "manual", "trigger_config": {},
    }))
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "roi_fleet.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_roi_fleet.py"), orchestrator, assignment, admin,
         fleet, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_roi_fleet.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    json.loads(out.read_text())
    return {"ROI fleet": {"id": fleet, "url": f"{base}/observability/outcomes"}}


def seed_roi_under_k(token: str, base: str, peer: str) -> dict[str, dict[str, str]]:
    """One agent and one fleet backed by fewer than k people, so the peer reads his own ROI share (#69).

    The administrator owns an agent, a lab assignment of it and a fleet with the agent as its only
    member declaring the unit ``threat_report_triaged`` (``per_run``); both are shared read-only with
    the peer. ``seed_roi_under_k.py`` writes four counted runs through the backend's models (no API
    writes a record): two attributed to the administrator, two to the peer, so two people stay under
    k. Read back as the peer on ``GET /roi?scope=agent`` and ``scope=fleet``: ``figures.suppressed``
    has ``own_share_only`` and ``figures.totals.runs`` is the peer's own 2. Logged ``seeded ROI under k
    own share (agent|fleet): <url>``. May raise: the caller logs and skips.
    """
    read_only = {"shared_user_ids": [peer], "shared_user_permissions": {peer: "read_only"}}
    agent = _seed_agent(token, "ROI under k")
    assignment = _created(api("POST", "/assignments", token, {
        "agent_id": agent, "name": f"{SEED_NAME} ROI under k triage", "description": "Seeded by the lab.",
        "prompt": "Triage the new threat reports.", "trigger_type": "manual", "trigger_config": {},
    }))
    fleet = _created(api("POST", "/fleets", token, {
        "name": f"{SEED_NAME} ROI under k fleet",
        "description": "Seeded by the lab: ROI runs of two people, fewer than k, shared read-only with the peer.",
    }))
    _created(api("POST", f"/fleets/{fleet}/members", token, {"agent_id": agent, "role_label": "Member"}))
    api("PUT", f"/roi/declarations?subject_type=fleet&subject_id={fleet}", token, {
        "unit": ROI_FLEET_UNIT, "count_mode": "per_run", "units_per_count": 1,
    })
    api("PUT", f"/agents/{agent}", token, read_only)
    api("PUT", f"/fleets/{fleet}", token, read_only)
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "roi_under_k.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_roi_under_k.py"), agent, assignment, admin, peer,
         fleet, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_roi_under_k.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    own = len(json.loads(out.read_text())["runs"]["peer"])
    login = api("POST", "/auth/login", body={"email": PEER_EMAIL, "password": PEER_PASSWORD})
    peer_token = login.get("access_token") if isinstance(login, dict) else None
    if not peer_token:
        raise RuntimeError("the peer's sign-in answered no access token")
    seeded: dict[str, dict[str, str]] = {}
    for scope, subject, url in (("agent", agent, f"{base}/agents/{agent}"), ("fleet", fleet, f"{base}/flows/fleets/{fleet}")):
        answer = api("GET", f"/roi?window=30d&currency=USD&scope={scope}&id={subject}", peer_token)
        figures = answer.get("figures") if isinstance(answer, dict) else None
        suppressed = (figures or {}).get("suppressed") or {}
        runs = ((figures or {}).get("totals") or {}).get("runs")
        if not suppressed.get("own_share_only") or runs != own:
            raise RuntimeError(f"GET /roi?scope={scope} as the peer: suppressed {suppressed}, runs {runs}, {own} expected")
        log(f"seeded ROI under k own share ({scope}): {url}")
        seeded[f"ROI under k own share ({scope})"] = {"id": subject, "url": url}
    return seeded


def seed_roi_chat_only(token: str, base: str) -> dict[str, dict[str, str]]:
    """Chat-only ROI value in every rolling window: a ``no_agent`` remainder and an agent and fleet with no run.

    The administrator owns an agent and a fleet with the agent as its only member, both with no run.
    ``seed_roi_chat_only.py`` writes four counted ``roi_chat_records`` lines through the backend's
    models (no API writes one), all inside the 7-day window: two with no agent, two of the agent
    stamped with its fleet. Read back on ``GET /roi?window=7d&scope=platform``: ``figures.by_agent``
    carries a ``no_agent`` row with hours above 0; and on ``scope=agent|fleet``: ``figures.totals``
    has runs 0 and deliverables 2. Logged ``seeded ROI chat-only (organization|agent|fleet): <url>``.
    May raise: the caller logs and skips (dev:harness-gaps lab-roi-chat-only-window-seed).
    """
    agent = _seed_agent(token, "ROI chat only")
    fleet = _created(api("POST", "/fleets", token, {
        "name": f"{SEED_NAME} ROI chat-only fleet",
        "description": "Seeded by the lab: value from chat deliverables only, no run.",
    }))
    _created(api("POST", f"/fleets/{fleet}/members", token, {"agent_id": agent, "role_label": "Member"}))
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "roi_chat_only.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_roi_chat_only.py"), admin, agent, fleet, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_roi_chat_only.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    records = json.loads(out.read_text())["records"]
    if len(records) != 4:
        raise RuntimeError(f"seed_roi_chat_only.py wrote {len(records)} lines of 4")
    answer = api("GET", "/roi?window=7d&currency=USD&scope=platform", token)
    rows = ((answer.get("figures") if isinstance(answer, dict) else None) or {}).get("by_agent") or []
    remainder = next((r for r in rows if isinstance(r, dict) and r.get("key") == "no_agent"), None)
    if not remainder or not remainder.get("hours"):
        raise RuntimeError(f"GET /roi?window=7d: no no_agent row with hours in By agent ({remainder})")
    url = f"{base}/tasks/value?scope=organization&window=7d"
    log(f"seeded ROI chat-only (organization): {url}")
    seeded = {"ROI chat-only no_agent remainder": {"id": "no_agent", "url": url}}
    for scope, subject, url in (("agent", agent, f"{base}/agents/{agent}"), ("fleet", fleet, f"{base}/flows/fleets/{fleet}")):
        answer = api("GET", f"/roi?window=7d&currency=USD&scope={scope}&id={subject}", token)
        totals = (((answer.get("figures") if isinstance(answer, dict) else None) or {}).get("totals")) or {}
        if totals.get("runs") != 0 or totals.get("deliverables") != 2:
            raise RuntimeError(f"GET /roi?scope={scope}: totals {totals}, runs 0 and deliverables 2 expected")
        log(f"seeded ROI chat-only ({scope}): {url}")
        seeded[f"ROI chat-only ({scope})"] = {"id": subject, "url": url}
    return seeded


#: Seeded background tasks by status (``seed_background_tasks.py`` ``TASKS``).
LAB_BACKGROUND_TASKS = {"running": 3, "pending": 1, "completed": 2, "failed": 1}


def seed_background_tasks(token: str, base: str) -> dict[str, dict[str, str]]:
    """Running, pending, completed and failed background tasks, the open ones with a live execution.

    The administrator owns a parent agent that delegated to a target agent. ``seed_background_tasks.py
    seed`` writes the ``sub_agent_tasks`` rows through the backend's models (no API creates one without
    a model call): 3 running (two with long titles), 1 pending, 2 completed and 1 failed; each open task
    gets a ``sub_agent`` execution parked ``pending`` 30 days ahead, so no worker runs it and the
    orphan recovery leaves it. Read back on ``GET /chat/sub-agent-tasks?agent_id=<parent>``: the counts
    by status, and every open task with ``execution_state`` ``pending``. The chat notice shows a
    completion of the last 60 seconds: ``seed_background_tasks.py restamp <json>`` before a capture.
    Logged ``seeded background tasks (running, pending, completed, failed): <url>``.
    May raise: the caller logs and skips (dev:harness-gaps lab-seed-running-background-tasks).
    """
    parent = _seed_agent(token, "Background parent")
    target = _seed_agent(token, "Background worker")
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "background_tasks.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_background_tasks.py"), "seed", admin, parent, target,
         str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_background_tasks.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    tasks = json.loads(out.read_text())["tasks"]
    answer = api("GET", f"/chat/sub-agent-tasks?agent_id={parent}&limit=50", token)
    listed = [t for t in answer if isinstance(t, dict) and t.get("task_id") in set(tasks.values())] if isinstance(answer, list) else []
    counts = {status: sum(1 for t in listed if t.get("status") == status) for status in LAB_BACKGROUND_TASKS}
    if counts != LAB_BACKGROUND_TASKS:
        raise RuntimeError(f"GET /chat/sub-agent-tasks: {counts} listed, {LAB_BACKGROUND_TASKS} expected")
    dead = [t["task_id"] for t in listed if t.get("status") in ("pending", "running") and t.get("execution_state") != "pending"]
    if dead:
        raise RuntimeError(f"GET /chat/sub-agent-tasks: open task(s) with no live execution: {dead}")
    url = f"{base}/chat?agent={parent}"
    log(f"seeded background tasks (running, pending, completed, failed): {url} ({base}/operations/background-tasks)")
    return {"background tasks": {"id": parent, "url": url}}


def seed_builtin_tools_notice(token: str, base: str) -> dict[str, dict[str, str]]:
    """One built-in tools release notice in the administrator's bell (kind ``builtin_tools_outside_allowlist``).

    The platform writes it at the first boot of a build whose catalog gained a function in a group an
    agent's allowlist scopes; a lab boots one build and never sees it. ``seed_builtin_tools_notice.py
    seed`` limits a lab agent to the first function of one built-in group and writes the notice for
    the rest of the group through ``builtin_tool_release_notice`` (``agents_left_out``, ``notice``) and
    ``notify_web_user``. Read back on ``GET /notifications``: an entry of that kind routed to the
    agent's Tooling section, where each function is added or dismissed (#12245).
    Logged ``seeded built-in tools notice (<group>, <n> left out): <url>``.
    May raise: the caller logs and skips (dev:harness-gaps lab-seed-builtin-tools-notice).
    """
    agent = _seed_agent(token, "Scoped tools")
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "builtin_tools_notice.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_builtin_tools_notice.py"), "seed", admin, agent,
         str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_builtin_tools_notice.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    seeded = json.loads(out.read_text())
    feed = api("GET", "/notifications?limit=50", token)
    items = feed.get("items", []) if isinstance(feed, dict) else []
    entry = next(
        (i for i in items if i.get("kind") == "builtin_tools_outside_allowlist"
         and str(i.get("route", "")).startswith(f"/agents/{agent}/config/tooling")),
        None,
    )
    if entry is None:
        raise RuntimeError("GET /notifications: no builtin_tools_outside_allowlist entry for the scoped agent")
    url = f"{base}{entry['route']}"
    log(f"seeded built-in tools notice ({seeded['group']}, {len(seeded['left_out'])} left out): {url}")
    return {"built-in tools notice": {"id": agent, "url": url}}


#: The group ``seed_group_manager`` creates: lab-only, managed by the peer.
LAB_MANAGED_GROUP = "Lab managed group"


def seed_group_manager(token: str, base: str, peer: str) -> dict[str, dict[str, str]]:
    """The group "Lab managed group" with the peer (*peer*) as its manager and member (#9734).

    So the peer's /analytics renders the group-manager view and its subtitle names the group, with
    no hand-made group during a review. Logged as ``seeded group manager (peer): <base>/users-and-groups/groups/<id>``.
    May raise: the caller logs and skips.
    """
    group = _created(api("POST", "/groups", token, {"name": LAB_MANAGED_GROUP}))
    api("POST", f"/groups/{group}/members", token, {"user_ids": [peer]})
    api("POST", f"/groups/{group}/managers", token, {"user_ids": [peer]})
    return {"group manager (peer)": {"id": group, "url": f"{base}/users-and-groups/groups/{group}"}}


#: The LLM Analytics seed: agents, users and groups past one page (10) of every breakdown table.
ANALYTICS_AGENTS = 20
ANALYTICS_USERS = 15
ANALYTICS_GROUPS = ("Lab analysts", "Lab engineers", "Lab responders")
#: Lab-only, like ``PEER_PASSWORD``: these users never sign in, they own the seeded traces.
ANALYTICS_PASSWORD = "lab-only-analytics-password-1"


def seed_analytics_traces(token: str, base: str) -> dict[str, dict[str, str]]:
    """Thirty days of LLM traces and their daily rollups, so /analytics renders on real rows.

    Twenty lab agents, fifteen users spread over three groups (created through the API), then
    ``seed_analytics_traces.py`` writes the traces and runs the platform's daily-stats backfill
    through the backend's own models: no API writes an ``llm_traces`` row. Logged as
    ``seeded analytics traces: <base>/observability``. May raise: the caller logs and skips.
    """
    agents = [_seed_agent(token, f"analytics agent {n:02d}") for n in range(1, ANALYTICS_AGENTS + 1)]
    users = [
        _created(api("POST", "/users", token, {
            "email": f"lab-analytics-{n:02d}@filigran.io", "full_name": f"Lab analytics user {n:02d}",
            "password": ANALYTICS_PASSWORD, "role": "user",
        }))
        for n in range(1, ANALYTICS_USERS + 1)
    ]
    for g, name in enumerate(ANALYTICS_GROUPS):
        group = _created(api("POST", "/groups", token, {"name": name}))
        api("POST", f"/groups/{group}/members", token, {"user_ids": users[g::len(ANALYTICS_GROUPS)]})
    ids = ROOT / "analytics_ids.json"
    ids.write_text(json.dumps({"agents": agents, "users": users}))
    out = ROOT / "analytics_traces.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_analytics_traces.py"), str(ids), str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=300,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_analytics_traces.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    counts = json.loads(out.read_text())
    return {"analytics traces": {"id": str(counts["traces"]), "url": f"{base}/observability"}}


#: The production-shaped membership ``LAB_FIGURES_SCALE=large`` gives one analytics user (#87): as many groups as
#: a large SAML directory hands one person; every third name is a bare id, as a directory without display names sends.
ANALYTICS_WIDE_MEMBER_GROUPS = 90
#: The analytics user who holds them: ``lab-analytics-01``, who owns traces, so the Users breakdown lists them.
ANALYTICS_WIDE_MEMBER_EMAIL = "lab-analytics-01@filigran.io"


def analytics_wide_group_names(count: int = ANALYTICS_WIDE_MEMBER_GROUPS) -> list[str]:
    """The group names of the wide member (#87): every third one a bare UUID (``POST /groups`` strips spaces)."""
    return [
        f"3f1c2a8e-0000-4000-8000-{index:012d}" if index % 3 == 0 else f"Lab directory group {index:02d}"
        for index in range(count)
    ]


def seed_analytics_wide_member(token: str, base: str) -> dict[str, dict[str, str]]:
    """One analytics user in :data:`ANALYTICS_WIDE_MEMBER_GROUPS` groups, under ``LAB_FIGURES_SCALE=large`` (#87).

    A person in about ninety SAML groups crashed /analytics in production only: no lab row carried that shape.
    Creates the groups of :func:`analytics_wide_group_names` through ``POST /groups`` and adds
    :data:`ANALYTICS_WIDE_MEMBER_EMAIL` (seeded by :func:`seed_analytics_traces`, read from
    ``analytics_ids.json``) to each. Read back: ``GET /traces/analytics/cost-by-user`` names that user with at
    least that many ``group_names``. Logged ``seeded analytics wide member (<n> groups): <base>/observability?
    breakdown=users``. May raise: the caller logs and skips.
    """
    ids = json.loads((ROOT / "analytics_ids.json").read_text())
    user = ids["users"][0]
    names = analytics_wide_group_names()
    for name in names:
        group = _created(api("POST", "/groups", token, {"name": name}))
        api("POST", f"/groups/{group}/members", token, {"user_ids": [user]})
    answer = api("GET", "/traces/analytics/cost-by-user?period=30d", token)
    items = answer.get("items", []) if isinstance(answer, dict) else []
    row = next((item for item in items if item.get("user_email") == ANALYTICS_WIDE_MEMBER_EMAIL), None)
    held = len(row.get("group_names") or []) if row else 0
    if held < len(names):
        raise RuntimeError(f"GET /traces/analytics/cost-by-user names {ANALYTICS_WIDE_MEMBER_EMAIL} in {held} groups")
    url = f"{base}/observability?breakdown=users"
    log(f"seeded analytics wide member ({held} groups): {url}")
    return {"analytics wide member": {"id": user, "url": url}}


def seed_chat_layout(token: str, base: str, peer_id: str | None) -> dict[str, dict[str, str]]:
    """The chat-layout rows under ``LAB_CHAT_LAYOUT=on`` (#10910), for the chat-layout UX rounds.

    ``seed_chat_layout.py`` writes the agents, workspaces, conversations and the 32-turn thread
    through the backend's own models; the thread is read back through ``/chat/conversations``.
    Logged ``seeded chat layout: <base>/chat?c=<id>``. May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "chat_layout.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_chat_layout.py"), admin, peer_id or "", str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=180,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_chat_layout.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    rows = json.loads(out.read_text())
    thread = rows["thread"]
    page = api("GET", f"/chat/conversations/{thread}/messages?limit=20", token)
    if not isinstance(page, dict) or len(page.get("messages") or []) < 20 or not page.get("has_more"):
        raise RuntimeError(f"GET /chat/conversations/{thread}/messages: {str(page)[:300]}")
    url = f"{base}/chat?c={thread}"
    log(f"seeded chat layout: {url}")
    cards = {}
    if rows.get("coverage"):
        cards["chat layout markdown coverage"] = {"id": rows["coverage"], "url": f"{base}/chat?c={rows['coverage']}"}
        log(f"seeded chat layout markdown coverage: {base}/chat?c={rows['coverage']}")
    for key, label in (("Lab chat answer with a work-item card", "chat layout work-item card"),
                       ("Lab chat resumed answer with a work-item card", "chat layout resumed work-item card")):
        if rows.get(key):
            cards[label] = {"id": rows[key], "url": f"{base}/chat?c={rows[key]}"}
            log(f"seeded {label}: {base}/chat?c={rows[key]}")
    return {
        **cards,
        "chat layout thread": {"id": thread, "url": url},
        "chat layout agents": {"id": ",".join(rows["agents"]), "url": f"{base}/agents"},
        "chat layout workspaces": {"id": ",".join(rows["workspaces"]), "url": f"{base}/chat"},
    }


def seed_chat_sidebar(token: str, base: str) -> dict[str, dict[str, str]]:
    """Agent-tagged sidebar conversations over Today, Yesterday and the previous 7 days, on every lab (#11257).

    ``seed_chat_sidebar.py`` writes the agents (emoji, Lucide, default and empty icons) and the
    conversations through the backend's own models; every title is read back on ``GET /chat/conversations``
    with its ``agent_name``. Logged ``seeded chat sidebar conversations: <base>/chat``. May raise: the
    caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "chat_sidebar.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_chat_sidebar.py"), admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_chat_sidebar.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    rows = json.loads(out.read_text())
    listed = api("GET", "/chat/conversations?limit=200", token)
    named = {c.get("title"): c.get("agent_name") for c in listed} if isinstance(listed, list) else {}
    missing = [title for title in rows["titles"] if not named.get(title)]
    if missing:
        raise RuntimeError(f"GET /chat/conversations: {len(missing)} seeded conversation(s) not listed with an agent")
    url = f"{base}/chat"
    log(f"seeded chat sidebar conversations: {url}")
    return {"chat sidebar conversations": {"id": ",".join(rows["agents"]), "url": url}}


def seed_feedback_comments(base: str) -> dict[str, dict[str, str]]:
    """Commented feedback on one analytics agent, so its Observability table renders on real rows (#9494),
    and scored feedback criteria, so its Feedback criteria table renders with data (#9860).

    Reads the agents and users ``seed_analytics_traces`` wrote to ``analytics_ids.json``; no API
    writes another user's ``message_feedback`` row, so ``seed_feedback_comments.py`` writes the
    conversations, messages and feedback through the backend's own models, with its interpreter
    and environment. Logged ``seeded feedback comments: <base>/agents/<id>/observability`` and
    ``seeded feedback criteria: <base>/agents/<id>/feedback``. May raise: the caller logs and skips.
    """
    ids = ROOT / "analytics_ids.json"
    out = ROOT / "feedback_comments.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_feedback_comments.py"), str(ids), str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_feedback_comments.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    rows = json.loads(out.read_text())
    return {
        "feedback comments": {"id": rows["agent"], "url": f"{base}/agents/{rows['agent']}/observability"},
        "feedback criteria": {"id": rows["agent"], "url": f"{base}/agents/{rows['agent']}/feedback"},
    }


def seed_trace_sessions(base: str, peer_id: str | None) -> dict[str, dict[str, str]]:
    """Fourteen conversations with their traces, so /trace-sessions renders on real rows (#9742).

    Reads the agents and users ``seed_analytics_traces`` wrote to ``analytics_ids.json``; no API
    writes an ``llm_traces`` row, so ``seed_trace_sessions.py`` writes the conversations, the traces
    (each with a ``conversation_id``) and their spans through the backend's own models, with its
    interpreter and environment; one session belongs to the peer when it was seeded. Logged
    ``seeded trace sessions: <base>/observability/sessions``. May raise: the caller logs and skips.
    """
    ids = ROOT / "analytics_ids.json"
    out = ROOT / "trace_sessions.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_trace_sessions.py"), str(ids), peer_id or "",
         str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=180,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_trace_sessions.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    counts = json.loads(out.read_text())
    return {"trace sessions": {"id": str(counts["sessions"]), "url": f"{base}/observability/sessions"}}


def seed_operations(token: str, agent: str, base: str) -> dict[str, dict[str, str]]:
    """Five background tasks and four worksets of the administrator, so the two Operations lists render.

    The worksets: active, resolved, the seeded agent's implicit chat corpus (Rolling, one item
    evicted) and a sealed one (#73); the active workset's storage knowledge base, whose
    ``/knowledge/<id>`` renders the "workset storage" state (#169, #265).

    No API creates a background task or a workset row: ``seed_operations.py`` writes them through
    the backend's own models and services, with its interpreter and environment. May raise: the
    caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "operations.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_operations.py"), agent, admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_operations.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    rows = json.loads(out.read_text())
    return {
        "background tasks (5)": {"id": ",".join(rows["tasks"]), "url": f"{base}/operations/background-tasks"},
        "worksets (active, resolved, rolling, sealed)": {
            "id": ",".join(rows["worksets"]), "url": f"{base}/operations/worksets",
        },
        "workset storage knowledge base": {
            "id": rows["storage"][0], "url": f"{base}/knowledge/{rows['storage'][0]}",
        },
    }


def seed_stale_reasons(token: str, base: str) -> dict[str, dict[str, str]]:
    """One work item per ``stale_reason``, so ``/tasks/work-items?view=stale`` renders every reason (#9905).

    Through the API a lab only produces ``past_due``: ``seed_stale_reasons.py`` writes the four items
    through the backend's ``WorkItem`` model with aged timestamps, with its interpreter and
    environment. Each one is read back from ``GET /work-items?stale_only=true`` with its reason; a
    reason missing raises. Logged ``seeded stale reasons: <url>``. May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "stale_reasons.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_stale_reasons.py"), admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_stale_reasons.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    ids = json.loads(out.read_text())
    listed = api("GET", "/work-items?stale_only=true&correlation_key_prefix=lab-stale-reasons&limit=50", token)
    reasons = {str(row.get("id")): row.get("stale_reason") for row in listed if isinstance(row, dict)}
    wrong = [f"{reason}: {reasons.get(item)!r}" for reason, item in ids.items() if reasons.get(item) != reason]
    if wrong:
        raise RuntimeError(f"stale reasons not read back: {', '.join(wrong)}")
    return {"stale reasons": {"id": ",".join(f"{r}={i}" for r, i in ids.items()),
                              "url": f"{base}/tasks/work-items?view=stale"}}


def seed_open_decision(token: str, base: str) -> dict[str, dict[str, str]]:
    """One work item waiting for the administrator's decision, so the decisions inbox renders a real row.

    Created through the API with ``needs_human`` and a complete ask, no owner agent (no inbox claims
    it): the dual write opens its ``decisions`` row. Read back from ``GET /decisions/waiting``, which
    the lab turns on with ``DECISIONS_READ_ENABLED`` (``backend_env``): a switch off, or the item
    missing from the inbox, raises. Logged ``seeded open decision: <url>``. May raise: the caller
    logs and skips.
    """
    item = _created(api("POST", "/work-items", token, {
        "title": f"{SEED_NAME} decision waiting for you",
        "description": "Seeded by the lab: a work item parked for a person's decision.",
        "correlation_key": "lab-open-decision",
        "dedupe": True,
        "dedupe_by": "key",
        "needs_human": True,
        "ask": {
            "question": "Ship the seeded change to every lab?",
            "context": "Seeded by the lab so the decisions inbox shows one open row.\nAnswering it changes nothing.",
            "options": [
                {"key": "A", "label": "Ship it", "effect": "The item reopens with this answer."},
                {"key": "B", "label": "Hold it", "effect": "The item reopens with this answer."},
            ],
            "recommendation": "A",
        },
    }))
    inbox = api("GET", "/decisions/waiting?limit=200", token)
    if not isinstance(inbox, dict) or inbox.get("enabled") is not True:
        reason = inbox.get("reason") if isinstance(inbox, dict) else str(inbox)[:120]
        raise RuntimeError(f"the decisions inbox is off: {reason}")
    ids = {str(row.get("id")) for row in inbox.get("items") or [] if isinstance(row, dict)}
    if f"work_item:{item}" not in ids:
        raise RuntimeError(f"the open decision of work item {item} is not in GET /decisions/waiting")
    return {"open decision": {"id": f"work_item:{item}", "url": f"{base}/tasks/work-items/{item}"}}


def seed_no_reason_blocked(token: str, base: str, fleet: str | None) -> dict[str, dict[str, str]]:
    """Blocked work items with no reason, so the work items list renders its "No reason" chip (#10422).

    The API refuses a blocked item with no why: ``seed_no_reason_blocked.py`` writes them through the
    backend's ``WorkItem`` model, one unscoped and, given *fleet* (the locked fleet), one filed in its
    workspace with no owner. Each one is read back from ``GET /work-items`` with ``blocked_why.missing``
    true, the fleet one under ``?fleet=`` too; a row missing raises. Logged ``seeded no-reason blocked
    work items: <url>`` (and ``... in the fleet: <url>``). May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "no_reason_blocked.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_no_reason_blocked.py"), admin, fleet or "", str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_no_reason_blocked.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    ids = json.loads(out.read_text())
    query = "/work-items?status=blocked&correlation_key_prefix=lab-no-reason-blocked&vis=all&limit=50"
    scopes = {"operations": query}
    if "fleet" in ids:
        scopes["fleet"] = f"{query}&fleet={fleet}"
    wrong = []
    for slot, path in scopes.items():
        listed = api("GET", path, token)
        missing = {str(row.get("id")): (row.get("blocked_why") or {}).get("missing") for row in listed if isinstance(row, dict)}
        if missing.get(ids[slot]) is not True:
            wrong.append(f"{slot}: {missing.get(ids[slot])!r}")
    if wrong:
        raise RuntimeError(f"no-reason blocked items not read back: {', '.join(wrong)}")
    seeded = {"no-reason blocked work items": {"id": ids["operations"],
                                              "url": f"{base}/tasks/work-items?vis=all"}}
    if "fleet" in ids:
        seeded["no-reason blocked work items in the fleet"] = {
            "id": ids["fleet"], "url": f"{base}/fleets/{fleet}/operations/work-items"}
    return seeded


def seed_scheduled_one_offs(token: str, base: str) -> dict[str, dict[str, str]]:
    """An upcoming and a missed scheduled one-off, so the Reschedule dialog opens on real rows (#11252).

    No REST route creates a scheduled one-off (the ``schedule_work_item`` agent tool does), so
    ``seed_scheduled_one_offs.py`` writes two through the backend's ``WorkItem`` model, owned by the
    administrator and by the first member of "Lab sample fleet". Each one is read back from
    ``GET /work-items/scheduled`` under its state (``upcoming``, ``missed``); a row missing raises.
    Logged ``seeded scheduled one-off: <url>`` and ``seeded scheduled one-off (missed): <url>``.
    May raise: the caller logs and skips.
    """
    agent = LAB_AGENTS.get(FLEET_MEMBERS[0])
    if not agent:
        raise RuntimeError("no Lab sample fleet member: the fleet seed did not run")
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "scheduled_one_offs.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_scheduled_one_offs.py"), admin, agent, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_scheduled_one_offs.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    ids = json.loads(out.read_text())
    wrong = []
    for state in ("upcoming", "missed"):
        listed = api("GET", f"/work-items/scheduled?state={state}&limit=100", token)
        found = {str(row.get("id")) for row in listed if isinstance(row, dict)} if isinstance(listed, list) else set()
        if ids[state] not in found:
            wrong.append(state)
    if wrong:
        raise RuntimeError(f"scheduled one-offs not read back: {', '.join(wrong)}")
    # One item opens as its own path over the Scheduled view (#775): /tasks/work-items/<id>?view=scheduled.
    url = f"{base}/tasks/work-items"
    return {
        "scheduled one-off": {"id": ids["upcoming"], "url": f"{url}/{ids['upcoming']}?view=scheduled"},
        "scheduled one-off (missed)": {"id": ids["missed"], "url": f"{url}/{ids['missed']}?view=scheduled"},
    }


def seeds_kb_stale_embeddings() -> bool:
    """Whether this lab seeds the stale-embedding knowledge bases: on unless ``LAB_KB_STALE=0`` (#10251)."""
    return os.environ.get("LAB_KB_STALE", "1").strip() != "0"


def seed_kb_stale_embeddings(token: str, base: str) -> dict[str, dict[str, str]]:
    """Knowledge bases with stale stored embeddings, so the stale notice and its debug table render (#10251).

    No API writes a stale embedding: ``seed_kb_stale_embeddings.py`` writes the rows through the
    backend's models, with its interpreter and environment. They are read back from
    ``GET /knowledge-bases`` (at least 12 with ``stale_chunk_count > 0``, the package-origin one
    flagged); a short count raises. Logged ``seeded stale embeddings: <base>/knowledge``.
    ``LAB_KB_STALE=0`` skips it. May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "kb_stale_embeddings.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_kb_stale_embeddings.py"), admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_kb_stale_embeddings.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    rows = json.loads(out.read_text())
    seeded = set(rows["knowledge_bases"])
    listed = api("GET", "/knowledge-bases", token)
    stale = {str(kb.get("id")): kb for kb in listed
             if isinstance(kb, dict) and str(kb.get("id")) in seeded and (kb.get("stale_chunk_count") or 0) > 0}
    if len(stale) < 12:
        raise RuntimeError(f"GET /knowledge-bases answered {len(stale)} of {len(seeded)} seeded knowledge bases stale")
    if not (stale.get(rows["package_origin"]) or {}).get("is_package_origin"):
        raise RuntimeError("the package-origin knowledge base is not read back stale and package-origin")
    log(f"seeded stale embeddings: {len(stale)} knowledge bases")
    return {"stale embeddings": {"id": ",".join(sorted(stale)), "url": f"{base}/knowledge"}}


def seed_concierge_signals(token: str, agent: str, base: str) -> dict[str, dict[str, str]]:
    """Six crossed Concierge signals and a few cycles, so Concierge > Signals and Cycles render on real rows.

    No API writes a signal (the signal pass reads real traces): ``seed_concierge_signals.py`` writes
    them through the backend's models, with its interpreter and environment, on *agent*, a lab flow
    and a manual assignment created here. Each signal is read back from ``GET /concierge/signals``.
    Logged ``seeded concierge signals (N): <base>/concierge/signals``. May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    assignment = _created(api("POST", "/assignments", token, {
        "agent_id": agent, "name": f"{SEED_NAME} Concierge target", "description": "Seeded by the lab.",
        "prompt": "Triage the new threat reports.", "trigger_type": "manual", "trigger_config": {},
    }))
    out = ROOT / "concierge_signals.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_concierge_signals.py"), agent, assignment, admin,
         str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_concierge_signals.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    rows = json.loads(out.read_text())
    listed = api("GET", "/concierge/signals?active_only=true", token)
    read = {str(row.get("target_id")) + ":" + str(row.get("kind")) for row in listed if isinstance(row, dict)}
    if len(read) < len(rows["signals"]):
        raise RuntimeError(f"GET /concierge/signals answered {len(read)} of {len(rows['signals'])} seeded signals")
    return {
        f"concierge signals ({len(rows['signals'])})": {"id": ",".join(rows["signals"]),
                                                       "url": f"{base}/concierge/signals"},
        f"concierge cycles ({len(rows['cycles'])} new)": {"id": ",".join(rows["cycles"]),
                                                         "url": f"{base}/concierge/cycles"},
        "concierge flow target": {"id": rows["flow"], "url": f"{base}/flows/library/{rows['flow']}"},
        "concierge assignment target": {"id": assignment, "url": f"{base}/agents/assignments/{assignment}"},
    }


def seed_concierge_improvements(token: str, agent: str, flow: str, assignment: str,
                                base: str) -> dict[str, dict[str, str]]:
    """About 30 improvement proposals, so the Improvements inbox and the Concierge cards render real rows.

    No API files a proposal without a measured experiment: ``seed_concierge_improvements.py`` writes
    them through the backend's models on *agent*, *assignment*, a second manual assignment created
    here, the lab Concierge *flow* and the skills and flow it creates (#10401). The total is read back
    from ``GET /concierge/improvements``. Logged ``seeded concierge improvements (N):
    <base>/concierge?tab=improvements``. May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    second = _created(api("POST", "/assignments", token, {
        "agent_id": agent, "name": f"{SEED_NAME} Concierge second target", "description": "Seeded by the lab.",
        "prompt": "Sweep the feeds every night.", "trigger_type": "manual", "trigger_config": {},
    }))
    out = ROOT / "concierge_improvements.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_concierge_improvements.py"), agent, assignment,
         second, flow, admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_concierge_improvements.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    rows = json.loads(out.read_text())
    listed = api("GET", "/concierge/improvements?limit=200", token)
    total = int(listed.get("total") or 0) if isinstance(listed, dict) else 0
    if total != len(listed.get("items") or []) or total < len(rows["proposals"]):
        raise RuntimeError(f"GET /concierge/improvements answered {total} of {len(rows['proposals'])} seeded proposals")
    return {
        f"concierge improvements ({total})": {"id": ",".join(rows["proposals"]),
                                              "url": f"{base}/concierge?tab=improvements"},
    }


def seed_organizations(base: str) -> dict[str, dict[str, str]]:
    """A named primary organization and three tracked ones, so the organization scopes render (#9389).

    One tracked organization has a long name, one is archived, one has monitoring on. No API
    switches monitoring on: ``seed_organizations.py`` writes the rows through the backend's own
    services, with its interpreter and environment. Logged ``seeded organizations (primary and 3
    tracked): <base>/settings/organization``. May raise: the caller logs and skips.
    """
    out = ROOT / "organizations.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_organizations.py"), str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_organizations.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    rows = json.loads(out.read_text())
    ids = ",".join([rows["primary"], *rows["tracked"]])
    return {
        f"organizations (primary and {len(rows['tracked'])} tracked)": {
            "id": ids, "url": f"{base}/settings/organization",
        },
    }


#: The AI Adoption low-activity users (``seed_ai_adoption_low_activity.py`` LOW_ACTIVITY_USERS, same order).
LOW_ACTIVITY_USERS: tuple[tuple[str, str], ...] = (
    ("lab-low-activity-01@filigran.io", "Lab low-activity user 01"),
    ("lab-low-activity-02@filigran.io", "Lab low-activity user 02"),
    ("lab-low-activity-03@filigran.io", "Lab low-activity user 03"),
    (
        "lab.low.activity.long.name@ai-adoption-lowest-activity-truncation-check.lab.filigran.io",
        "Lab low-activity user with a deliberately long display name to check row truncation in Gaps",
    ),
)


def seed_ai_adoption_low_activity(token: str, base: str) -> dict[str, dict[str, str]]:
    """Four users active below the threshold, so AI Adoption > Gaps > Lowest Activity renders rows.

    Creates the users through the API (one with a long name and email), then
    ``seed_ai_adoption_low_activity.py`` writes one platform ``platform_daily`` row (1 to 7
    messages) per user through the backend's own models, under ``ACTIVITY_THRESHOLD`` (10).
    ``GET /ai-adoption/gaps?days=30`` is read back and must list every one of them in
    ``low_activity``. Logged
    ``seeded ai adoption low-activity users: <base>/ai-adoption/gaps``. May raise: the caller logs and skips.
    """
    for email, name in LOW_ACTIVITY_USERS:
        api("POST", "/users", token, {
            "email": email, "full_name": name, "password": ANALYTICS_PASSWORD, "role": "user",
        })
    out = ROOT / "ai_adoption_low_activity.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_ai_adoption_low_activity.py"), str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_ai_adoption_low_activity.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    gaps = api("GET", "/ai-adoption/gaps?days=30", token)
    wanted = {email for email, _name in LOW_ACTIVITY_USERS}
    rows = gaps.get("low_activity") if isinstance(gaps, dict) else None
    found = len(wanted & {str(r.get("email") or "") for r in rows or [] if isinstance(r, dict)})
    if found < len(wanted):
        raise RuntimeError(f"GET /ai-adoption/gaps answered {found} of {len(wanted)} low-activity users")
    return {
        "ai adoption low-activity users": {"id": f"{found} low-activity", "url": f"{base}/ai-adoption/gaps"},
    }


def seed_ai_adoption_large(token: str, base: str) -> dict[str, dict[str, str]]:
    """``LAB_AI_ADOPTION_SCALE=large``: AI Adoption figures of 1,000 and more, read back through the API.

    ``seed_ai_adoption_large.py`` writes, through the backend's own models, a pool of over 5,000 users,
    over 1,000 active users and over 1,000 API keys with requests in the thousands.
    ``/ai-adoption/overview``, ``/ai-adoption/gaps`` and ``/activity/stats`` are read back and must
    answer those figures. Logged ``seeded ai adoption large figures: <base>/ai-adoption``. May raise:
    the caller logs and skips.
    """
    out = ROOT / "ai_adoption_large.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_ai_adoption_large.py"), str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=240,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_ai_adoption_large.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    rows = json.loads(out.read_text())
    overview = api("GET", "/ai-adoption/overview?days=30", token)
    gaps = api("GET", "/ai-adoption/gaps?days=30", token)
    stats = api("GET", "/activity/stats?scope=platform", token)
    figures = {
        "registered users": int((stats or {}).get("user_count") or 0),
        "gaps pool": int((gaps or {}).get("total_users") or 0),
        "gaps active": int(((gaps or {}).get("summary") or {}).get("active_count") or 0),
        "agentic active users": int((overview or {}).get("active_user_count") or 0),
        "api calls": sum(int(u.get("value") or 0) for u in (stats or {}).get("top_api_key_users") or []),
    }
    floors = {"registered users": 5000, "gaps pool": 5000}
    short = [f"{name} {value}" for name, value in figures.items() if value < floors.get(name, 1000)]
    if short:
        raise RuntimeError(f"the AI Adoption figures read back under their floor: {', '.join(short)}")
    url = f"{base}/ai-adoption"
    log(f"seeded ai adoption large figures: {url}")
    return {
        "ai adoption large figures": {
            "id": f"{rows.get('pool_users', 0)} users,{figures['agentic active users']} agentic",
            "url": url,
        },
    }


def seed_agent_account_activity(token: str, base: str) -> dict[str, dict[str, str]]:
    """A Teams agent account with a week of activity, one with none and a mailbox, so the account page renders on real rows.

    Creating an account through the API needs a signed-in delegated integration (a real provider
    call) and no API writes a decision: ``seed_agent_account_activity.py`` writes the three accounts
    (two Microsoft Teams, one Outlook mailbox, owned by the administrator, disabled, no integration)
    and the active and mailbox ones' decisions through the backend's own models, with its interpreter
    and environment. Logged ``seeded agent account activity: <base>/profile/agent-accounts/<id>``,
    ``seeded agent account replies: <base>/profile/agent-accounts/<id>/sent`` (the active account's
    30 messages sent, every status), ``seeded agent account (no activity): ...`` and
    ``seeded agent account activity (mailbox): ...``.

    A fourth account, ``seeded agent account (saveable): ...``, carries an agent and the lab Microsoft
    integration created here with :data:`LAB_AGENT_ACCOUNT_INTEGRATION_ID` (a lab-only OAuth app, never
    signed in): ``lab_asgi`` answers that integration's identity, conversation and people pickers, so its
    Edit dialog (General, Rules, Replies, Schedule) saves end to end with no fetch override. When either
    is refused the three others are still written and the start log says why the saveable one is not.
    May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    saveable: list[str] = []
    try:
        integration = _created(api("POST", "/integrations", token, {
            "id": LAB_AGENT_ACCOUNT_INTEGRATION_ID, "provider": "microsoft",
            "name": f"{SEED_NAME} agent account Microsoft",
            "description": "Seeded by the lab: a lab-only OAuth app, never signed in; lab_asgi answers its pickers.",
            "auth_type": "oauth", "client_id": "lab-only-agent-account-microsoft-client-id",
            "client_secret": "lab-only-agent-account-microsoft-client-secret",
        }))
        saveable = [_seed_agent(token, "Agent account responder"), integration]
    except RuntimeError as exc:
        log(f"agent account (saveable) not seeded: {str(exc)[:300]}")
    out = ROOT / "agent_accounts.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_agent_account_activity.py"), admin, str(out),
         *saveable],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_agent_account_activity.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    rows = json.loads(out.read_text())
    seeded = {
        "agent account activity": {"id": rows["active"], "url": f"{base}/profile/agent-accounts/{rows['active']}"},
        "agent account replies": {"id": rows["active"], "url": f"{base}/profile/agent-accounts/{rows['active']}/sent"},
        "agent account (no activity)": {"id": rows["empty"], "url": f"{base}/profile/agent-accounts/{rows['empty']}"},
        "agent account activity (mailbox)": {
            "id": rows["mailbox"],
            "url": f"{base}/profile/agent-accounts/{rows['mailbox']}",
        },
    }
    if "saveable" in rows:
        seeded["agent account (saveable)"] = {
            "id": rows["saveable"],
            "url": f"{base}/profile/agent-accounts/{rows['saveable']}",
        }
    return seeded


def seed_tracked_ai_draft(base: str) -> dict[str, dict[str, str]]:
    """A pending tracked-organizations AI draft with every review row state (#9761).

    ``seed_tracked_ai_draft.py`` stores one ``new_tracked`` proposal through the backend's own
    ``store_tracked_draft``, with its interpreter and environment: four pending rows (high with two
    OpenCTI matches and the exact one preset, medium, low with no match, a long name), one conflict row
    (a tracked organization of ``organizations.json``) and a failed batch of two names. The stub model
    answers no tracked draft, so the dialog alone never renders the pending states. Logged ``seeded
    tracked org ai-draft: <base>/settings/organization/tracked/ai-draft/<id>``. May raise.
    """
    out = ROOT / "tracked_ai_draft.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_tracked_ai_draft.py"),
         str(ROOT / "organizations.json"), str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_tracked_ai_draft.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    proposal = json.loads(out.read_text())["proposal"]
    return {"tracked org ai-draft": {"id": proposal, "url": f"{base}/settings/organization/tracked/ai-draft/{proposal}"}}


#: The Google OAuth row whose stored token lacks a requested scope (#9813): a lab-only app.
LAB_OAUTH_MISSING_SCOPES_BODY: dict[str, str] = {
    "provider": "google", "auth_type": "oauth",
    "description": "Seeded by the lab: a lab-only token granted every requested scope but one.",
    "client_id": "lab-only-google-client-id", "client_secret": "lab-only-google-client-secret",
}


#: The knowledge base the sync sources are seeded on (created by ``seed_prerequisites_unreadable_fleet``).
KB_SYNC_SOURCES_KB = f"{SEED_NAME} knowledge base not shared with the peer"
#: One connected integration per provider a sync source needs: lab-only credentials, never sent anywhere.
KB_SYNC_SOURCE_INTEGRATIONS: dict[str, dict[str, object]] = {
    "drata": {"auth_type": "api_key", "api_key": "lab-only-drata-key", "extra_config": {"region": "us"}},
    "github": {"auth_type": "api_key", "api_key": "lab-only-github-token"},
    "microsoft": {"auth_type": "oauth", "client_id": "lab-only-kb-microsoft-client-id",
                  "client_secret": "lab-only-kb-microsoft-client-secret"},
    "google": {"auth_type": "oauth", "client_id": "lab-only-kb-google-client-id",
               "client_secret": "lab-only-kb-google-client-secret"},
}
#: One source per type, with the provider it binds (``None``: no integration) and its config.
KB_SYNC_SOURCES: list[tuple[str, str | None, dict[str, str]]] = [
    ("website", None, {"url": "https://docs.example.com", "max_pages": "5", "max_depth": "1"}),
    ("microsoft-drive", "microsoft", {"folder_path": "/Lab", "recursive": "true"}),
    ("google-drive", "google", {"folder_id": "root"}),
    ("github", "github", {"repo": "lab-sample/lab-repository", "branch": "main", "path": "docs/"}),
    ("drata", "drata", {"statuses": "ACTIVE,APPROVED"}),
]


def seed_kb_sync_sources(token: str, base: str) -> dict[str, dict[str, str]]:
    """Five knowledge base sync sources, one per type, and a Nova rule source with a last sync (#11143).

    Through the real API: one integration per provider of :data:`KB_SYNC_SOURCE_INTEGRATIONS`
    (Drata and GitHub land connected with a lab-only key; Microsoft and Google are lab-only OAuth
    apps) and a company-managed Nova integration (``auth_type`` ``none``: connected, its
    ``on_connect`` hook creates the rule source). Then ``seed_kb_sync_sources.py`` (backend
    interpreter and environment) stores a lab-only token on the two OAuth rows and a manual
    schedule and a last sync on the Nova source: no API does either without the provider round-trip.
    Then the five :data:`KB_SYNC_SOURCES` (``manual``: nothing ever syncs) on :data:`KB_SYNC_SOURCES_KB`
    (created when absent). Read back: five sources listed, the Nova source listed with a last sync.
    Logged ``seeded KB sync sources: <base>/knowledge/<id>/sources`` and
    ``seeded Nova rule source: <base>/integrations/<id>/rules``, and, when ``GET /nova-rules/engine-status``
    answers ``engine_available`` (``LAB_NOVA_ENGINE``, ``lab_asgi``), ``seeded Nova engine status: <same url>``.
    No outbound call. May raise.
    """
    integrations = {
        provider: _created(api("POST", "/integrations", token, {
            "provider": provider, "name": f"{SEED_NAME} KB sources {provider}",
            "description": "Seeded by the lab: lab-only credentials, nothing reaches the provider.", **body,
        }))
        for provider, body in KB_SYNC_SOURCE_INTEGRATIONS.items()
    }
    nova = _created(api("POST", "/integrations", token, {
        "provider": "nova", "name": f"{SEED_NAME} Nova", "auth_type": "none", "is_company_managed": True,
        "description": "Seeded by the lab: its rule source is manual and never pulls from GitHub.",
    }))
    out = ROOT / "kb_sync_sources.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_kb_sync_sources.py"), nova, str(out),
         integrations["microsoft"], integrations["google"]],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_kb_sync_sources.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    listed = api("GET", "/knowledge-bases", token)
    knowledge = next((str(kb["id"]) for kb in (listed if isinstance(listed, list) else [])
                      if isinstance(kb, dict) and kb.get("name") == KB_SYNC_SOURCES_KB), None)
    if knowledge is None:
        knowledge = _created(api("POST", "/knowledge-bases", token, {
            "name": KB_SYNC_SOURCES_KB, "description": "Seeded by the lab: one sync source of each type.",
        }))
    for source_type, provider, config in KB_SYNC_SOURCES:
        api("POST", f"/knowledge-bases/{knowledge}/sync-sources", token, {
            "source_type": source_type, "config": config, "sync_interval": "manual",
            "integration_id": integrations[provider] if provider else None,
        })
    sources = api("GET", f"/knowledge-bases/{knowledge}/sync-sources", token)
    types = sorted(s.get("source_type") for s in (sources if isinstance(sources, list) else []) if isinstance(s, dict))
    if types != sorted(source_type for source_type, _, _ in KB_SYNC_SOURCES):
        raise RuntimeError(f"created, but GET /knowledge-bases/{knowledge}/sync-sources lists {types}")
    rule_sources = api("GET", "/nova-rules/sources", token)
    rule = next((s for s in (rule_sources if isinstance(rule_sources, list) else [])
                 if isinstance(s, dict) and s.get("last_synced_at")), None)
    if rule is None:
        raise RuntimeError(f"stored, but GET /nova-rules/sources answers {str(rule_sources)[:200]}")
    try:
        engine = api("GET", "/nova-rules/engine-status", token)
    except Exception:  # noqa: BLE001 - the sources are seeded; the card line is only logged when it renders
        engine = None
    available = isinstance(engine, dict) and engine.get("engine_available") is True
    calibration = engine.get("semantic_calibration") if available else None
    uncalibrated = (isinstance(calibration, dict) and bool(calibration.get("configured_model"))
                    and calibration.get("calibrated") is False)
    return {
        "KB sync sources": {"id": knowledge, "url": f"{base}/knowledge/{knowledge}/sources"},
        "Nova rule source": {"id": nova, "url": f"{base}/integrations/{nova}/rules"},
        **({"Nova engine status": {"id": nova, "url": f"{base}/integrations/{nova}/rules"}} if available else {}),
        **({"Nova semantic calibration line": {"id": nova, "url": f"{base}/integrations/{nova}/rules"}}
           if uncalibrated else {}),
    }


#: The knowledge base the activity log is seeded on, created when absent.
KB_ACTIVITY_KB = f"{SEED_NAME} knowledge base activity"


def seed_kb_activity(token: str, base: str) -> dict[str, dict[str, str]]:
    """A knowledge base whose Activity tab lists every event kind on more than one page (#61).

    Only a sync, an upload, an embedding pass or a reprocess writes an activity row:
    ``seed_kb_activity.py`` (backend interpreter and environment) writes them through the backend's
    model on :data:`KB_ACTIVITY_KB` (created through the API when absent). Read back: the total from
    ``GET /knowledge-bases/<id>/activity/count`` over one page (50), and each of the ``sync``,
    ``documents`` and ``embeddings`` event types listed. Logged
    ``seeded KB activity: <base>/knowledge/<id>?tab=activity``. May raise: the caller logs and skips.
    """
    listed = api("GET", "/knowledge-bases", token)
    knowledge = next((str(kb["id"]) for kb in (listed if isinstance(listed, list) else [])
                      if isinstance(kb, dict) and kb.get("name") == KB_ACTIVITY_KB), None)
    if knowledge is None:
        knowledge = _created(api("POST", "/knowledge-bases", token, {
            "name": KB_ACTIVITY_KB, "description": "Seeded by the lab: every activity event kind.",
        }))
    out = ROOT / "kb_activity.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_kb_activity.py"), knowledge, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_kb_activity.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    counted = api("GET", f"/knowledge-bases/{knowledge}/activity/count", token)
    total = counted.get("count") if isinstance(counted, dict) else counted
    if not isinstance(total, int) or total <= 50:
        raise RuntimeError(f"stored, but GET /knowledge-bases/{knowledge}/activity/count answers {str(counted)[:200]}")
    for event_type in ("sync", "documents", "embeddings"):
        rows = api("GET", f"/knowledge-bases/{knowledge}/activity?event_type={event_type}&limit=1", token)
        if not rows:
            raise RuntimeError(f"stored, but no {event_type} row is listed on knowledge base {knowledge}")
    url = f"{base}/knowledge/{knowledge}?tab=activity"
    log(f"seeded KB activity: {url}")
    return {"KB activity": {"id": knowledge, "url": url}}


#: The second administrator who owns a sync, with an address long enough to wrap in the Runs as column (#65).
ORG_SYNC_OWNER_EMAIL = "lab-organization-sync-owner-with-a-long-address@lab-sample-subsidiary.filigran.io"
#: Passes ``check_password_policy`` (a letter and a digit): a password it refuses skips the whole seed (#1368).
ORG_SYNC_OWNER_PASSWORD = "lab-only-password-sync-owner-1"


def seed_org_syncs(token: str, base: str) -> dict[str, dict[str, str]]:
    """Organization syncs of every state and an import preview that reads (#65).

    The administrator saves a daily OpenCTI assets sync; a second administrator with a long address
    (:data:`ORG_SYNC_OWNER_EMAIL`) saves a weekly OpenAEV asset groups sync, is then demoted, and a run now
    pauses that sync with the real reason ("no longer an administrator"). ``LAB_ORG_IMPORT=1`` makes the
    preview read the fixture of ``lab_asgi``. Read back: ``GET /organization-context/syncs`` lists both, one
    paused, and ``POST /organization-context/import/preview`` answers rows with no error. Logged
    ``seeded organization syncs: <base>/settings/organization``. May raise: the caller logs and skips.
    """
    def saved(rows: object) -> list[dict]:
        return [r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)]

    existing = saved(api("GET", "/organization-context/syncs", token))
    if not any(r.get("section") == "assets" and r.get("source") == "opencti" for r in existing):
        api("POST", "/organization-context/syncs", token, {
            "section": "assets", "source": "opencti", "batch_type": "host", "frequency": "daily",
            "hour": 6, "minute": 30, "timezone": "Europe/Paris", "filters": {"search": "lab"},
        })
    if not any(r.get("owner_email") == ORG_SYNC_OWNER_EMAIL for r in existing):
        owner = _created(api("POST", "/users", token, {
            "email": ORG_SYNC_OWNER_EMAIL, "full_name": "Lab sync owner", "password": ORG_SYNC_OWNER_PASSWORD,
            "role": "admin",
        }))
        login = api("POST", "/auth/login", body={"email": ORG_SYNC_OWNER_EMAIL, "password": ORG_SYNC_OWNER_PASSWORD})
        owner_token = login.get("access_token") if isinstance(login, dict) else None
        if not owner_token:
            raise RuntimeError("the sync owner's sign-in answered no access token")
        sync = _created(api("POST", "/organization-context/syncs", owner_token, {
            "section": "asset_groups", "source": "openaev", "frequency": "weekly", "weekday": 0,
            "hour": 7, "minute": 0, "timezone": "UTC",
        }))
        api("PUT", f"/users/{owner}/role", token, {"role": "user"})
        api("POST", f"/organization-context/syncs/{sync}/run", token)
    rows = saved(api("GET", "/organization-context/syncs", token))
    if len(rows) < 2 or not any(r.get("paused_reason") for r in rows):
        raise RuntimeError(f"stored, but GET /organization-context/syncs answers {str(rows)[:200]}")
    preview = api("POST", "/organization-context/import/preview", token, {
        "section": "assets", "source": "opencti", "filters": {}, "batch_type": "",
    })
    plan = preview.get("plan") if isinstance(preview, dict) else None
    if not isinstance(plan, dict) or plan.get("error") or not plan.get("read"):
        raise RuntimeError(f"the import preview answers {str(preview)[:200]}")
    url = f"{base}/settings/organization"
    log(f"seeded organization syncs: {url}")
    return {"organization syncs": {"id": f"{len(rows)} syncs, {plan.get('read')} rows previewed", "url": url}}


#: The custom integration provider every lab imports (#67): a ``filigran-integration-v1`` bundle with an
#: API key setting, a select setting and no connectivity probe, so its card and its New integration form
#: render without calling anything.
LAB_CUSTOM_PROVIDER_METADATA = {
    "format": "filigran-integration-v1",
    "version": "1.0.0",
    "name": "Lab sample custom integration",
    "slug": "lab-sample-custom-integration",
    "provider": "lab-sample-custom-integration",
    "description": "Seeded by the lab: a custom provider imported from a bundle, with a long description "
                   "so the catalog card wraps its text over several lines.",
    "author": "XTM One lab",
    "auth_type": "api_key",
    "exported_at": "2026-11-18T00:00:00Z",
    "settings": [
        {"key": "api_key", "label": "API key", "type": "secret", "required": True,
         "placeholder": "lab-only key"},
        {"key": "region", "label": "Region", "type": "select", "required": False,
         "options": [{"value": "eu", "label": "Europe"}, {"value": "us", "label": "United States"}]},
    ],
    "supports_polling": True,
    "supports_webhook": False,
    "test_connection": {"mode": "none"},
}
LAB_CUSTOM_PROVIDER = "custom:" + LAB_CUSTOM_PROVIDER_METADATA["provider"]


def lab_custom_provider_bundle() -> bytes:
    """The bundle ZIP of :data:`LAB_CUSTOM_PROVIDER_METADATA`: ``metadata.json`` and its ``checksum.sha256``."""
    metadata = json.dumps(LAB_CUSTOM_PROVIDER_METADATA, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("metadata.json", metadata)
        archive.writestr("checksum.sha256", hashlib.sha256(metadata).hexdigest())
    return buffer.getvalue()


def seed_custom_integration_provider(token: str, base: str) -> dict[str, dict[str, str]]:
    """One custom integration provider in the catalog (#67).

    Imports :func:`lab_custom_provider_bundle` through ``POST /integrations/admin/import`` (the route the
    Import bundle dialog calls) unless the catalog already lists :data:`LAB_CUSTOM_PROVIDER` (a
    re-import of the same version answers 409). Read back: ``GET /integrations/catalog`` lists it with
    ``is_custom``. Logged ``seeded custom integration provider: <base>/integrations/new?provider=<key>``.
    May raise: the caller logs and skips.
    """
    def listed() -> dict | None:
        rows = api("GET", "/integrations/catalog", token)
        return next((r for r in (rows if isinstance(rows, list) else [])
                     if isinstance(r, dict) and r.get("provider") == LAB_CUSTOM_PROVIDER), None)

    if listed() is None:
        boundary = f"lab-{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"bundle\"; filename=\"lab-custom.zip\"\r\n"
            "Content-Type: application/zip\r\n\r\n"
        ).encode() + lab_custom_provider_bundle() + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{API_PORT}/api/v1/integrations/admin/import", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"POST /integrations/admin/import: {exc.code} {exc.read()[:200].decode('utf-8', 'replace')}") from exc
    row = listed()
    if row is None or not row.get("is_custom"):
        raise RuntimeError(f"imported, but GET /integrations/catalog lists {LAB_CUSTOM_PROVIDER} as {str(row)[:200]}")
    url = f"{base}/integrations/new?" + urllib.parse.urlencode({"provider": LAB_CUSTOM_PROVIDER})
    log(f"seeded custom integration provider: {url}")
    return {"custom integration provider": {"id": LAB_CUSTOM_PROVIDER, "url": url}}


def seed_a2a_partner(token: str, base: str) -> dict[str, dict[str, str]]:
    """One inbound OAuth A2A partner integration with three partner runs (#71).

    ``POST /integrations`` creates the ``oauth_a2a`` integration (``a2a_direction`` inbound), ``POST
    /integrations/{id}/a2a-credential`` mints its partner registration, and a seeded agent binds it.
    ``seed_a2a_partner_runs.py`` writes a completed, a failed and a running run of that agent through
    the backend's model, each stamped with the partner's client id and ``a2a_provider`` (no API
    creates an A2A run with a given registration). Read back: ``GET /assignments/runs/all`` narrowed
    by ``a2a_integration_id`` lists the three. Logged ``seeded A2A integration with partner runs:
    <base>/integrations/<id>/activity``. May raise: the caller logs and skips.
    """
    integration = _created(api("POST", "/integrations", token, {
        "provider": "oauth_a2a", "name": f"{SEED_NAME} A2A partner", "auth_type": "none",
        "a2a_direction": "inbound", "description": "Seeded by the lab: an inbound A2A partner, nothing calls it.",
    }))
    credential = api("POST", f"/integrations/{integration}/a2a-credential", token)
    client_id = credential.get("client_id") if isinstance(credential, dict) else None
    if not client_id:
        raise RuntimeError(f"POST /integrations/{integration}/a2a-credential answered no client id")
    agent = _seed_agent(token, "A2A partner desk")
    try:
        api("PUT", f"/agents/{agent}", token, {"integration_bindings": {"oauth_a2a": [integration]}})
    except RuntimeError as exc:  # the runs still list without the binding
        log(f"A2A partner: binding refused, runs seeded unbound: {exc}")
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "a2a_partner_runs.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_a2a_partner_runs.py"), agent, admin, client_id,
         str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_a2a_partner_runs.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    seeded_runs = set(json.loads(out.read_text()))
    listed = api("GET", f"/assignments/runs/all?a2a_integration_id={integration}&limit=50", token)
    shown = {str(r.get("id")) for r in (listed if isinstance(listed, list) else []) if isinstance(r, dict)}
    if not seeded_runs <= shown:
        raise RuntimeError(f"GET /assignments/runs/all?a2a_integration_id= lists {len(shown & seeded_runs)} of {len(seeded_runs)} runs")
    url = f"{base}/integrations/{integration}/activity"
    log(f"seeded A2A integration with partner runs: {url}")
    return {"A2A partner integration": {"id": integration, "url": url}}


#: The agents the A2A liaisons bind, by their ``LAB_AGENTS`` name: a member drawn on the data flow fleet
#: and one on the large fleet, so both Graph tabs and ``/flows/map`` draw partner nodes (#85).
A2A_LIAISON_AGENTS = ("flow orchestrator", "large lead")
#: The outbound registration's partner agent cards, on a reserved host nothing resolves (RFC 2606).
A2A_LIAISON_CARD_URLS = (
    "https://partner.invalid/agents/triage/.well-known/agent-card.json",
    "https://partner.invalid/agents/enrichment/.well-known/agent-card.json",
)


def seed_a2a_liaisons(token: str, base: str) -> dict[str, dict[str, str]]:
    """An inbound and an outbound A2A liaison on agents the fleet graphs draw (#85).

    ``POST /integrations`` creates an inbound ``oauth_a2a`` integration (its partner credential minted)
    and an outbound one whose ``card_urls`` name :data:`A2A_LIAISON_CARD_URLS` (two client targets,
    written by ``provision_settings``; the host is reserved, so no card is ever fetched and nothing is
    delegated). Both are bound to every :data:`A2A_LIAISON_AGENTS` agent the fleet seeds created.
    Read back: ``GET /a2a/liaisons`` answers, for each bound agent, an inbound edge into it and an
    outbound edge to each card. Logged ``seeded A2A liaisons: <base>/flows/map?vis=all``. May raise:
    the caller logs and skips.
    """
    agents = [LAB_AGENTS[name] for name in A2A_LIAISON_AGENTS if name in LAB_AGENTS]
    if not agents:
        raise RuntimeError(f"none of {', '.join(A2A_LIAISON_AGENTS)} was seeded")
    inbound = _created(api("POST", "/integrations", token, {
        "provider": "oauth_a2a", "name": f"{SEED_NAME} A2A intake partner", "auth_type": "none",
        "a2a_direction": "inbound", "description": "Seeded by the lab: an inbound A2A liaison, nothing calls it.",
    }))
    api("POST", f"/integrations/{inbound}/a2a-credential", token)
    outbound = _created(api("POST", "/integrations", token, {
        "provider": "oauth_a2a", "name": f"{SEED_NAME} A2A delegate partner", "auth_type": "none",
        "a2a_direction": "outbound",
        "description": "Seeded by the lab: an outbound A2A liaison on a reserved host, nothing is called.",
        "extra_config": {
            "card_urls": "\n".join(A2A_LIAISON_CARD_URLS), "token_url": "https://partner.invalid/mcp/oauth/token",
            "client_id": "lab-only-a2a-client-id", "client_secret": "lab-only-a2a-client-secret",
        },
    }))
    for agent in agents:
        api("PUT", f"/agents/{agent}", token, {"integration_bindings": {"oauth_a2a": [inbound, outbound]}})
    liaisons = api("GET", "/a2a/liaisons", token)
    edges = liaisons.get("edges", []) if isinstance(liaisons, dict) else []
    for agent in agents:
        into = [e for e in edges if e.get("target_id") == agent and e.get("direction") == "inbound"]
        out = [e for e in edges if e.get("source_id") == agent and e.get("direction") == "outbound"]
        if not into or len(out) < len(A2A_LIAISON_CARD_URLS):
            raise RuntimeError(f"GET /a2a/liaisons draws {len(into)} inbound and {len(out)} outbound edges for {agent}")
    url = f"{base}/flows/map?vis=all"
    log(f"seeded A2A liaisons: {url}")
    return {"A2A liaisons": {"id": outbound, "url": url}}


def seed_oauth_missing_scopes(token: str, base: str) -> dict[str, dict[str, str]]:
    """A connected Google OAuth integration whose token was granted all but one requested scope (#9813).

    Created through the real ``POST /integrations`` (``configured``, lab-only app), then
    ``seed_oauth_missing_scopes.py`` stores a lab-only token and the partial grant through the
    backend's own model, with its interpreter and environment: no API stores a token without the
    provider round-trip. Read back on ``GET /integrations`` (``missing_scopes`` non-empty). Logged
    ``seeded OAuth missing scopes: <base>/integrations/<id>``. No outbound call. May raise.
    """
    integration = _created(api("POST", "/integrations", token, {
        **LAB_OAUTH_MISSING_SCOPES_BODY, "name": f"{SEED_NAME} Google missing scope",
    }))
    out = ROOT / "oauth_missing_scopes.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_oauth_missing_scopes.py"), integration, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_oauth_missing_scopes.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    rows = api("GET", "/integrations", token)
    row = next((r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict) and r.get("id") == integration), None)
    if not row or not row.get("missing_scopes"):
        raise RuntimeError(f"stored, but GET /integrations answers missing_scopes {row.get('missing_scopes') if row else 'unlisted'}")
    return {"OAuth missing scopes": {"id": integration, "url": f"{base}/integrations/{integration}"}}


#: The lab's MCP servers (#9999), by the label the start log prints: every one disabled, so no
#: agent run, test or refresh contacts it; the remote URLs are on ``.lab.invalid`` (never
#: resolved) and every value is lab-only. ``POST /mcp-servers`` bodies, the name added on create.
LAB_MCP_SERVER_BODIES: dict = {
    "stdio": {
        "transport": "stdio", "command": "npx",
        "args": ["-y", "@lab-only/mcp-server-sample", "--read-only"],
        "env": {"LAB_SAMPLE_TOKEN": "lab-only-stdio-env-value"},
        "tags": ["lab"], "enabled": False,
    },
    "secret headers": {
        "transport": "streamable_http", "url": "https://mcp-headers.lab.invalid/mcp",
        "auth_mode": "headers", "headers": {"Authorization": "Bearer lab-only-mcp-header-token"},
        "tags": ["lab"], "enabled": False,
    },
    "OAuth connected": {
        "transport": "streamable_http", "url": "https://mcp-oauth.lab.invalid/mcp", "auth_mode": "oauth",
        "client_id": "lab-only-mcp-client-id", "client_secret": "lab-only-mcp-client-secret",
        "scopes": "read", "authorize_url": "https://mcp-oauth.lab.invalid/authorize",
        "token_url": "https://mcp-oauth.lab.invalid/token",
        "tags": ["lab"], "enabled": False,
    },
}


def seed_mcp_servers(token: str, base: str) -> dict[str, dict[str, str]]:
    """Three disabled MCP servers: stdio with args and env, secret headers, OAuth connected (#9999).

    Created through the real ``POST /mcp-servers`` (:data:`LAB_MCP_SERVER_BODIES`), then
    ``seed_mcp_oauth_connected.py`` stores a lab-only token on the OAuth one through the backend's
    own model, with its interpreter and environment: no API stores a token without the provider
    round-trip. Read back on ``GET /mcp-servers/<id>`` (``oauth_status`` ``connected``), so the
    Configuration tab's token-drop warning renders with no fetch override. Logged
    ``seeded MCP server (<label>): <base>/mcp-servers/<id>``. No outbound call. May raise.
    """
    seeded: dict[str, dict[str, str]] = {}
    for label, body in LAB_MCP_SERVER_BODIES.items():
        server = _created(api("POST", "/mcp-servers", token, {**body, "name": f"{SEED_NAME} MCP {label}"}))
        seeded[f"MCP server ({label})"] = {"id": server, "url": f"{base}/mcp-servers/{server}"}
    oauth = seeded["MCP server (OAuth connected)"]["id"]
    out = ROOT / "mcp_oauth_connected.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_mcp_oauth_connected.py"), oauth, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_mcp_oauth_connected.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    row = api("GET", f"/mcp-servers/{oauth}", token)
    if not isinstance(row, dict) or row.get("oauth_status") != "connected":
        raise RuntimeError(f"stored, but GET /mcp-servers/{oauth} answers oauth_status "
                           f"{row.get('oauth_status') if isinstance(row, dict) else row!r}")
    return seeded


#: The lab's stored-credential secret vault (#10557): a HashiCorp AppRole provider kept in the
#: database, so Settings > Access and security > Secret vaults lists one row with its credential
#: set and status ``not_checked``. Its address is on ``.lab.invalid`` (never resolved, so the
#: address policy lets it through without loosening it and nothing is contacted); every value is
#: lab-only. The ``POST /settings/secret-vaults`` body.
LAB_SECRET_VAULT_BODY: dict = {
    "name": "lab_vault", "display_name": f"{SEED_NAME} vault", "type": "hashicorp",
    "settings": {"address": "https://vault.lab.invalid:8200", "auth_method": "approle",
                 "role_id": "lab-only-vault-role-id"},
    "credentials": {"secret_id": "lab-only-vault-secret-id"},
    "enabled": True,
}


def seed_secret_vault(token: str, base: str) -> dict[str, dict[str, str]]:
    """One stored-credential secret vault provider (#10557), created once, read back.

    Created through the real ``POST /settings/secret-vaults`` (:data:`LAB_SECRET_VAULT_BODY`)
    unless ``GET /settings/secret-vaults`` already lists its name (idempotent across restarts).
    Read back from that list: source ``database``, the ``secret_id`` credential set and no value
    served. Logged ``seeded secret vault: <base>/settings/security/vaults``. No outbound call.
    May raise.
    """
    name = LAB_SECRET_VAULT_BODY["name"]

    def listed() -> dict | None:
        answer = api("GET", "/settings/secret-vaults", token)
        providers = answer.get("providers", []) if isinstance(answer, dict) else []
        return next((p for p in providers if isinstance(p, dict) and p.get("name") == name), None)

    if listed() is None:
        _created(api("POST", "/settings/secret-vaults", token, LAB_SECRET_VAULT_BODY))
    row = listed()
    if row is None or row.get("source") != "database" or not (row.get("credentials_set") or {}).get("secret_id"):
        raise RuntimeError(f"stored, but GET /settings/secret-vaults answers {str(row)[:200]}")
    if "lab-only-vault-secret-id" in json.dumps(row):
        raise RuntimeError("GET /settings/secret-vaults serves the stored credential")
    return {"secret vault": {"id": str(row.get("id")), "url": f"{base}/settings/security/vaults"}}


#: The lists ``seed_shared_resources`` fills, by the label the start log prints: (entity kind of
#: ``GET /exposure/<segment>/summary``, create path, frontend list route, ``POST`` body without the share).
LAB_SHARED_RESOURCES: dict = {
    "variable": ("variables", "/variables", "/variables", {
        "name": "LAB_SHARED_SAMPLE", "value": "lab-only-shared-value", "is_secret": False,
        "description": "Seeded by the lab, shared read-only with the lab peer.",
    }),
    "MCP server": ("mcp-servers", "/mcp-servers", "/mcp-servers", {
        "name": f"{SEED_NAME} MCP shared", "transport": "stdio", "command": "npx",
        "args": ["-y", "@lab-only/mcp-server-sample"], "tags": ["lab"], "enabled": False,
    }),
    "custom tool": ("custom-tools", "/custom-tools", "/tools/custom", {
        "name": f"{SEED_NAME} shared tool", "description": "Seeded by the lab, shared read-only with the lab peer.",
        "js_function": "async function run(input) { return { ok: true }; }",
    }),
}


def seed_shared_resources(token: str, base: str, peer_id: str) -> dict[str, dict[str, str]]:
    """One variable, one disabled MCP server and one custom tool, each shared read-only with the peer.

    XTM One mode turns custom variables and custom tools off by default, so the administrator first
    stores ``disable_custom_variables`` and ``disable_custom_tools`` false through the real
    ``PUT /settings/security-options`` (the lab's backend sets neither variable, so neither is
    locked). Each row is created through its real ``POST`` with ``shared_user_ids``, then read back
    on ``GET /exposure/<resources>/summary`` (``ways`` at least 1), so the ``sm`` sharing badge
    renders on /variables, /mcp-servers and /tools/custom with no fetch override. Logged
    ``seeded shared resources: <urls>``. Lab-only values, no outbound call. May raise.
    """
    api("PUT", "/settings/security-options", token, {
        "values": {"disable_custom_variables": False, "disable_custom_tools": False},
    })
    share = {"shared_user_ids": [peer_id], "shared_user_permissions": {peer_id: "read_only"}}
    seeded: dict[str, dict[str, str]] = {}
    for label, (segment, create, route, body) in LAB_SHARED_RESOURCES.items():
        row = _created(api("POST", create, token, {**body, **share}))
        summary = api("GET", f"/exposure/{segment}/summary", token)
        entry = (summary.get("summaries") or {}).get(row) if isinstance(summary, dict) else None
        ways = entry.get("ways", 0) if isinstance(entry, dict) else 0
        if ways < 1:
            raise RuntimeError(f"{label} {row} stored, but GET /exposure/{segment}/summary answers ways {ways}")
        seeded[f"shared {label}"] = {"id": row, "url": base + route}
    log("seeded shared resources: " + " ".join(row["url"] for row in seeded.values()))
    return seeded


def lab_ai_catalog_env() -> str:
    """``AI_CATALOG_ENABLED`` for the lab's backend: ``true`` unless the declaration's env sets another value."""
    return os.environ.get("AI_CATALOG_ENABLED", "true").strip().lower() or "true"


#: The rows ``seed_public_catalog`` publishes, by the label the start log prints: (segment of
#: ``GET /public/catalog/<segment>``, create path, update path prefix, frontend list route, ``POST`` body).
#: Every row carries tags, so each list renders its tag chips and the tag filter has values.
LAB_PUBLIC_CATALOG: dict = {
    "agent": ("agents", "/agents", "/agents", "/catalog/agents", {
        "name": f"{SEED_NAME} catalog agent", "description": "Seeded by the lab, published to the AI catalog.",
        "persona": "You are the lab's catalog sample.", "tags": ["lab", "triage"],
    }),
    "skill": ("skills", "/skills", "/skills", "/catalog/skills", {
        "name": "lab-sample-catalog-skill", "description": "Seeded by the lab, published to the AI catalog.",
        "content": "---\nname: lab-sample-catalog-skill\ndescription: Seeded by the lab.\n---\n\n# Lab sample\n\nAnswer in one line.\n",
        "tags": ["lab", "writing"],
    }),
    "prompt": ("prompts", "/prompts", "/prompts", "/catalog/prompts", {
        "name": f"{SEED_NAME} catalog prompt", "description": "Seeded by the lab, published to the AI catalog.",
        "content": "Summarise the incident in three bullets.", "tags": ["lab", "reporting"],
    }),
    "MCP server": ("mcp-servers", "/mcp-servers", "/mcp-servers", "/catalog/mcp-servers", {
        "name": f"{SEED_NAME} catalog MCP", "transport": "stdio", "command": "npx",
        "args": ["-y", "@lab-only/mcp-server-sample"], "tags": ["lab", "enrichment"], "enabled": False,
    }),
    "custom tool": ("custom-tools", "/custom-tools", "/custom-tools", "/catalog/tools", {
        "name": f"{SEED_NAME} catalog tool", "description": "Seeded by the lab, published to the AI catalog.",
        "js_function": "async function run(input) { return { ok: true }; }", "tags": ["lab", "automation"],
    }),
}


def seed_public_catalog(token: str, base: str) -> dict[str, dict[str, str]]:
    """One agent, skill, prompt, MCP server and custom tool published to the AI catalog, each with tags.

    Each row of :data:`LAB_PUBLIC_CATALOG` is created through its real ``POST``, published with the real
    ``PUT <path>/<id>`` ``{"is_public_catalog": true}`` (the admin's own row, so no company-managed
    gate), then read back on ``GET /public/catalog/<segment>`` (the id listed, tags kept), so the five
    /catalog list pages render cards with tags with no fetch override. Needs ``AI_CATALOG_ENABLED``
    (the router is mounted only then). Logged ``seeded public catalog: <urls>``. No outbound call.
    May raise.
    """
    seeded: dict[str, dict[str, str]] = {}
    for label, (segment, create, update, route, body) in LAB_PUBLIC_CATALOG.items():
        row = _created(api("POST", create, token, body))
        api("PUT", f"{update}/{row}", token, {"is_public_catalog": True})
        listed = api("GET", f"/public/catalog/{segment}?limit=100", token)
        items = listed.get("items", []) if isinstance(listed, dict) else []
        mine = next((item for item in items if str(item.get("id")) == row), None)
        if mine is None:
            raise RuntimeError(f"{label} {row} published, but GET /public/catalog/{segment} does not list it")
        if not mine.get("tags"):
            raise RuntimeError(f"{label} {row} listed on GET /public/catalog/{segment} with no tags")
        seeded[f"catalog {label}"] = {"id": row, "url": f"{base}{route}/{row}"}
    log("seeded public catalog: " + " ".join(base + entry[3] for entry in LAB_PUBLIC_CATALOG.values()))
    return seeded


#: A small OpenAPI 3 document whose security schemes cover every auth kind the import offers
#: (API key in a header, HTTP bearer, the four OAuth2 flows) and one it refuses (HTTP digest).
#: Its OAuth endpoints are HTTPS on ``.lab.invalid``: nothing fetches them, a sign-in is never run.
LAB_OPENAPI_SPEC: dict = {
    "openapi": "3.0.3",
    "info": {"title": "Lab sample auth kinds", "version": "1.0.0"},
    "servers": [{"url": "https://api.lab.invalid/v1"}],
    "paths": {
        "/items": {"get": {"operationId": "listItems", "summary": "List the items",
                            "responses": {"200": {"description": "The items"}}}},
        "/items/{itemId}": {"get": {
            "operationId": "getItem", "summary": "Read one item",
            "parameters": [{"name": "itemId", "in": "path", "required": True, "schema": {"type": "string"}}],
            "responses": {"200": {"description": "The item"}},
        }},
    },
    "components": {"securitySchemes": {
        "ApiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
        "BearerToken": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        "OAuth": {"type": "oauth2", "flows": {
            "clientCredentials": {"tokenUrl": "https://auth.lab.invalid/token", "scopes": {"items:read": "Read items"}},
            "password": {"tokenUrl": "https://auth.lab.invalid/token", "scopes": {"items:read": "Read items"}},
            "authorizationCode": {"authorizationUrl": "https://auth.lab.invalid/authorize",
                                  "tokenUrl": "https://auth.lab.invalid/token", "scopes": {"items:read": "Read items"}},
            "implicit": {"authorizationUrl": "https://auth.lab.invalid/authorize", "scopes": {"items:read": "Read items"}},
        }},
        "DigestAuth": {"type": "http", "scheme": "digest"},
    }},
    "security": [{"ApiKeyHeader": []}],
}
#: The option kinds ``POST /integrations/openapi/preview`` must list for the document above.
LAB_OPENAPI_KINDS = ("api_key", "bearer", "oauth2_client_credentials", "oauth2_password",
                     "oauth2_authorization_code", "oauth2_implicit", "unsupported")
LAB_OPENAPI_PORT = 9098


def lab_private_address() -> str:
    """The container's own non-loopback address: the backend's spec fetch refuses loopback hosts."""
    import socket

    candidates: list[str] = []
    try:
        candidates += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))  # no packet is sent: it only picks the outgoing address
            candidates.append(probe.getsockname()[0])
    except OSError:
        pass
    for address in candidates:
        if not address.startswith("127.") and address != "0.0.0.0":
            return address
    raise RuntimeError("no non-loopback address in this container: the spec fetch refuses loopback")


class OpenAPISpecFile(http.server.BaseHTTPRequestHandler):
    """Answers ``GET /openapi.json`` with :data:`LAB_OPENAPI_SPEC`, 404 for anything else."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        if self.path.split("?", 1)[0] != "/openapi.json":
            self.send_error(404)
            return
        body = json.dumps(LAB_OPENAPI_SPEC).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


def seed_openapi_spec(token: str, base: str) -> dict[str, dict[str, str]]:
    """A served OpenAPI document with every auth kind, and one OpenAPI integration created from it.

    The document is served on the container's private address (``lab_private_address``), read back
    through ``POST /integrations/openapi/preview`` (every kind of :data:`LAB_OPENAPI_KINDS` listed),
    then imported with ``POST /integrations`` (provider ``openapi``, the API key option, a lab-only
    key). Logged ``seeded OpenAPI spec: <url>`` and ``seeded OpenAPI integration:
    <base>/integrations/<id>/config``. No call leaves the container. May raise.
    """
    address = lab_private_address()
    server = http.server.ThreadingHTTPServer((address, LAB_OPENAPI_PORT), OpenAPISpecFile)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://{address}:{LAB_OPENAPI_PORT}/openapi.json"
    preview = api("POST", "/integrations/openapi/preview", token, {"url": url})
    kinds = {o.get("kind") for o in (preview.get("auth_options") or []) if isinstance(o, dict)} if isinstance(preview, dict) else set()
    missing = [kind for kind in LAB_OPENAPI_KINDS if kind not in kinds]
    if missing:
        raise RuntimeError(f"served at {url}, but the preview lists no {', '.join(missing)} option")
    seeded = {"OpenAPI spec": {"id": "", "url": url}}
    integration = _created(api("POST", "/integrations", token, {
        "provider": "openapi", "name": f"{SEED_NAME} OpenAPI auth kinds", "api_key": "lab-only-openapi-key",
        "extra_config": {"base_url": "https://api.lab.invalid/v1"},
        "openapi": {"url": url, "auth_option": "ApiKeyHeader"},
    }))
    seeded["OpenAPI integration"] = {"id": integration, "url": f"{base}/integrations/{integration}/config"}
    return seeded


def seed_model_catalog_integrations(token: str, base: str, relay_group: str | None) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """One OpenRouter and one LM Studio integration whose Models tab lists a stubbed catalog; never raises.

    OpenRouter: ``POST /integrations`` with :data:`LAB_OPENROUTER_API_KEY` (``api_key``, connected) and
    :data:`LAB_OPENROUTER_MANAGEMENT_KEY` (``extra_config.management_key``), whose overview (credits) and 30 days of
    activity are read back and logged ``seeded OpenRouter usage activity: <base>/integrations/<id>/usage``; the platform
    OpenRouter provider is linked to it and its five keys are read back, logged
    ``seeded OpenRouter keys: <base>/integrations/<id>/keys``. LM Studio:
    ``auth_type`` ``none``, :data:`LAB_LMSTUDIO_BASE_URL` and the seeded relay group (the console refuses a server
    with no route). ``lab_asgi`` answers both catalogs from its fixtures (two or more authors, text and embedding
    models, loaded and not loaded), so nothing leaves the lab. Each is read back through its console route and logged
    ``seeded model catalog integration (<provider>): <base>/integrations/<id>/models``, or ``not seeded ...: <why>``.
    """
    seeded: dict[str, dict[str, str]] = {}
    skipped: dict[str, str] = {}
    entity = "model catalog integration (openrouter)"
    try:
        integration = _created(api("POST", "/integrations", token, {
            "provider": "openrouter", "name": f"{SEED_NAME} OpenRouter", "is_company_managed": True,
            "description": "Seeded by the lab: a lab-only key, nothing reaches OpenRouter.",
            "auth_type": "api_key", "api_key": LAB_OPENROUTER_API_KEY,
            "extra_config": {"management_key": LAB_OPENROUTER_MANAGEMENT_KEY},
        }))
        models = {kind: api("GET", f"/integrations/{integration}/openrouter/models?kind={kind}", token)
                  for kind in ("chat", "embeddings")}
        counts = {kind: len(a.get("models") or []) if isinstance(a, dict) else 0 for kind, a in models.items()}
        if not all(counts.values()):
            raise RuntimeError(f"created, but its catalog answers {counts}")
        seeded[entity] = {"id": integration, "url": f"{base}/integrations/{integration}/models"}
        entity = "OpenRouter usage activity"
        overview = api("GET", f"/integrations/{integration}/openrouter/overview", token)
        activity = api("GET", f"/integrations/{integration}/openrouter/activity", token)
        rows = (activity.get("rows") or []) if isinstance(activity, dict) else []
        if not isinstance(overview, dict) or not overview.get("credits") or not rows:
            raise RuntimeError(f"created, but its credits answer {str(overview)[:120]} and its activity {len(rows)} rows")
        seeded[entity] = {"id": integration, "url": f"{base}/integrations/{integration}/usage"}
        entity = "OpenRouter keys"
        # The platform OpenRouter provider fed from this integration: the integration's key then reads in use by
        # both (``in_use`` [integration, llm_provider]). A refusal (provider type gated off) leaves it in use by one.
        try:
            api("POST", f"/integrations/{integration}/openrouter/llm-provider", token, {
                "available_models": ["openai/gpt-lab-mini"], "default_model": "openai/gpt-lab-mini",
            })
        except Exception as exc:  # noqa: BLE001 - the keys still render, one use fewer
            log(f"OpenRouter keys: no platform provider linked ({str(exc)[:200]})")
        listing = api("GET", f"/integrations/{integration}/openrouter/keys", token)
        keys = (listing.get("keys") or []) if isinstance(listing, dict) else []
        if len(keys) < 5:
            raise RuntimeError(f"created, but its keys answer {str(listing)[:160]}")
        seeded[entity] = {"id": integration, "url": f"{base}/integrations/{integration}/keys"}
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped[entity] = str(exc)[:600]
    entity = "model catalog integration (lmstudio)"
    if not relay_group:
        skipped[entity] = "no relay group row: the LM Studio console refuses a server with no route"
        return seeded, skipped
    try:
        integration = _created(api("POST", "/integrations", token, {
            "provider": "lmstudio", "name": f"{SEED_NAME} LM Studio", "is_company_managed": True,
            "description": "Seeded by the lab: a .invalid server, nothing reaches LM Studio.",
            "auth_type": "none", "extra_config": {"base_url": LAB_LMSTUDIO_BASE_URL},
            "relay_group_id": relay_group,
        }))
        listing = api("GET", f"/integrations/{integration}/lmstudio/models", token)
        rows = (listing.get("models") or []) if isinstance(listing, dict) else []
        if not rows:
            raise RuntimeError(f"created, but its model listing answers {str(listing)[:120]}")
        seeded[entity] = {"id": integration, "url": f"{base}/integrations/{integration}/models"}
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped[entity] = str(exc)[:600]
    return seeded, skipped


def seed_handovers(token: str, base: str) -> dict[str, dict[str, str]]:
    """Five handover delegations of the administrator along seeded edges, so the live edges render.

    Two more agents, Triage and Responder, hand over to each other outside the fleet; the fleet's
    orchestrator delegates to its members. No API creates an ``agent_delegations`` row:
    ``seed_handovers.py`` writes them through the backend's own model, with its interpreter and
    environment, times relative to now (the recent rows stay live about 5 to 10 minutes). May
    raise: the caller logs and skips.
    """
    if "Orchestrator" not in LAB_AGENTS:
        raise RuntimeError("no fleet agents")
    responder = _seed_agent(token, "Responder")
    triage = _seed_agent(token, "Triage", {responder: "delegate"})
    api("PUT", f"/agents/{responder}", token, {
        "handover_agent_ids": [triage], "handover_types": {triage: "consult"},
    })
    LAB_AGENTS.update(Triage=triage, Responder=responder)
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "handovers.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_handovers.py"), admin,
         json.dumps(LAB_AGENTS), str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_handovers.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    rows = json.loads(out.read_text())
    return {f"handovers ({len(rows)})": {"id": ",".join(rows), "url": f"{base}/flows/map?vis=all"}}


def seed_fleet_run_estimate(token: str, base: str, fleet: str, opencti: str) -> dict[str, dict[str, str]]:
    """A run estimate on *fleet* ("Lab sample fleet") with a figure and a per-assignment error (#9555).

    One ``integration_event`` assignment on the fleet's first member (``Researcher``), bound to the
    registered OpenCTI integration *opencti*; ``seed_run_estimate.py`` writes its ``provision`` block
    (``organizations: tracked``) and links the monitored tracked organization to a fixture identity
    whose count ``lab_asgi`` answers, with the backend's interpreter and environment. The estimate
    is read back: the linked organization answers ``expected_runs_per_month`` above 0, the unlinked
    one an assignment entry with ``error``. Logged ``seeded fleet run estimate: <url>``. May raise.
    """
    member = LAB_AGENTS.get(FLEET_MEMBERS[0])
    if not member:
        raise RuntimeError("no fleet member agent")
    events = api("GET", "/integrations/events", token)
    listed = events.get("opencti") if isinstance(events, dict) else None
    if not listed:
        raise RuntimeError("GET /integrations/events lists no opencti event")
    assignment = _created(api("POST", "/assignments", token, {
        "agent_id": member, "name": f"{SEED_NAME} run estimate",
        "description": "Seeded by the lab: serves the tracked organizations.", "prompt": "Summarise the event.",
        "trigger_type": "integration_event",
        "trigger_config": {"provider": "opencti", "event": listed[0]["event"], "integration_id": opencti},
    }))
    out = ROOT / "run_estimate.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_run_estimate.py"), assignment,
         str(ROOT / "organizations.json"), str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_run_estimate.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    rows = json.loads(out.read_text())
    query = f"organization_ids={rows['linked']}&organization_ids={rows['unlinked']}"
    estimate = api("GET", f"/fleets/{fleet}/run-estimate?{query}", token)
    by_id = {o.get("organization_id"): o for o in (estimate.get("organizations") or [])} if isinstance(estimate, dict) else {}
    linked, unlinked = by_id.get(rows["linked"]) or {}, by_id.get(rows["unlinked"]) or {}
    if not (linked.get("expected_runs_per_month") or 0) > 0:
        raise RuntimeError(f"the linked organization reads no figure: {str(linked)[:300]}")
    if not any(a.get("error") for a in unlinked.get("assignments") or []):
        raise RuntimeError(f"the unlinked organization reads no assignment error: {str(unlinked)[:300]}")
    return {"fleet run estimate": {"id": assignment, "url": f"{base}/fleets/{fleet}/members"}}


#: Every opt-in state the default lab's CTEM fleets render (#9689): one missing stops the start.
CTEM_FLEET_OPTIN_REQUIRED = ("on", "partial", "paused", "stale", "failing", "not_set_up")
#: The name of the fork that reads Not set up when every built-in CTEM fleet has another state.
CTEM_NOT_SET_UP_FORK = "CTEM fleet not set up"


def _ctem_not_set_up_fork(token: str, source: str) -> str:
    """A fork of the CTEM fleet *source*, found by name on a restarted lab: a CTEM fleet with every
    automation switched off (``fleet_fork``: copies are created disabled), so it reads Not set up."""
    name = f"{SEED_NAME} {CTEM_NOT_SET_UP_FORK}"
    found = next((f for f in api("GET", "/fleets", token) if f.get("name") == name), None)
    return str(found["id"]) if found else _fork(token, source, CTEM_NOT_SET_UP_FORK)


def seed_ctem_fleet_optin(token: str, base: str) -> dict[str, dict[str, str]]:
    """``LAB_CTEM_FLEET_OPTIN=mixed``: the Fleet activity card of ``/`` renders On, Partially on, Paused, Stale, Failing, Not set up.

    Every seeded fleet automation ships disabled and no API opts a built-in fleet in for a lab:
    ``seed_ctem_fleet_optin.py`` writes the states through the backend's models and pause helpers,
    with its interpreter and environment (one fleet on with a provision block serving the monitored
    tracked organization, one partially on, one paused with ``user_toggle``, one on with a stale work
    item, one on with a failed run, #9670). With the five built-in CTEM fleets none is left not set
    up, so one is forked (:func:`_ctem_not_set_up_fork`, every copied automation switched off, #9689).
    The ON fleet's run estimate is read back above 0 and the stale and failing fleets' ``/health``
    status; a state of :data:`CTEM_FLEET_OPTIN_REQUIRED` missing raises. Logged ``seeded CTEM fleet
    opt-in states: <url>``. May raise: the caller stops the start (#9689).
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "ctem_fleet_optin.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_ctem_fleet_optin.py"), admin,
         str(ROOT / "organizations.json"), str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_ctem_fleet_optin.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    states = json.loads(out.read_text())
    if not any(row["state"] == "not_set_up" for row in states.values()):
        source = sorted(states.items())[0][1]["id"]
        states["lab-fork"] = {"id": _ctem_not_set_up_fork(token, source), "state": "not_set_up"}
    # A health state carried by another fleet (``also``: four built-in CTEM fleets, #1048) counts too.
    seen = {row["state"] for row in states.values()} | {s for row in states.values() for s in row.get("also") or []}
    missing = [s for s in CTEM_FLEET_OPTIN_REQUIRED if s not in seen]
    if missing:
        raise RuntimeError(f"CTEM fleet opt-in states missing: {', '.join(missing)} ({str(states)[:300]})")
    on = next((row for row in states.values() if row["state"] == "on"), None)
    if on is None:
        raise RuntimeError(f"no fleet seeded on: {str(states)[:300]}")
    estimate = api("GET", f"/fleets/{on['id']}/run-estimate", token)
    runs = estimate.get("expected_runs_per_month") if isinstance(estimate, dict) else None
    if not (runs or 0) > 0:
        raise RuntimeError(f"the fleet seeded on reads no run estimate: {str(estimate)[:300]}")
    for row in states.values():
        health_state = next((s for s in (row["state"], *(row.get("also") or [])) if s in ("stale", "failing")), None)
        if health_state is not None:
            health = api("GET", f"/fleets/{row['id']}/health", token)
            status = health.get("status") if isinstance(health, dict) else None
            if status != health_state:
                raise RuntimeError(f"the fleet seeded {health_state} reads health {status!r}")
    ids = ",".join(f"{slug}={row['state']}" for slug, row in sorted(states.items()))
    return {"CTEM fleet opt-in states": {"id": ids, "url": f"{base}/"}}


def seed_ctem_fleet_roi(token: str, base: str) -> dict[str, dict[str, str]]:
    """The CTEM fleet seeded On carries ROI run records of the last 7 days (#9831).

    So its command center tile on ``/`` renders the review rate and non-zero Units, Hours saved and
    Net value (7 d) on real rows, with no ``GET /roi?window=7d`` fetch override. Reuses
    ``seed_roi_records.py`` with the fleet id and no work item: four runs of a lab agent, one record
    each (unreviewed, accepted, modified, rejected), every record carrying ``fleet_id``. The fleet
    is the one ``seed_ctem_fleet_optin`` read back On (``ctem_fleet_optin.json``). Logged
    ``seeded CTEM fleet ROI records: <base>/``. May raise: the caller logs and skips.
    """
    states = json.loads((ROOT / "ctem_fleet_optin.json").read_text())
    on = next((row for row in states.values() if row["state"] == "on"), None)
    if on is None:
        raise RuntimeError(f"no CTEM fleet seeded on: {str(states)[:300]}")
    agent = _seed_agent(token, "CTEM fleet ROI analyst")
    assignment = _created(api("POST", "/assignments", token, {
        "agent_id": agent, "name": f"{SEED_NAME} CTEM fleet ROI triage", "description": "Seeded by the lab.",
        "prompt": "Triage the new threat reports for the CTEM fleet.", "trigger_type": "manual",
        "trigger_config": {},
    }))
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "ctem_fleet_roi.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_roi_records.py"), agent, assignment, admin, "-",
         str(out), str(on["id"])],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_roi_records.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    json.loads(out.read_text())
    report = api("GET", "/roi?window=7d", token)
    fleets = report.get("fleets") if isinstance(report, dict) else None
    row = next((f for f in fleets or [] if str(f.get("id")) == str(on["id"])), None)
    if row is None or not (row.get("units") or 0) > 0:
        raise RuntimeError(f"GET /roi?window=7d answers no figures for the fleet seeded on: {str(row)[:300]}")
    return {"CTEM fleet ROI records": {"id": str(on["id"]), "url": f"{base}/"}}


def seed_ctem_fleets_all_off(token: str, base: str) -> dict[str, dict[str, str]]:
    """``LAB_CTEM_FLEET_OPTIN=off``: every CTEM fleet not set up, the Fleet activity invitation of ``/`` (#9703).

    ``seed_ctem_fleet_optin.py --all-off`` switches off, with an expected reason (Not set up, never
    Paused), every fleet automation of the built-in CTEM fleets, with the backend's interpreter and
    environment. Each fleet is read back: the script's ``enabled`` count is 0 and ``GET
    /fleets/{id}/health`` answers ``automations.enabled`` 0 when it carries the counts (#9566). Logged
    ``seeded CTEM fleets all off: <url>``. May raise: the caller stops the start.
    """
    out = ROOT / "ctem_fleets_all_off.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_ctem_fleet_optin.py"), "--all-off", str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_ctem_fleet_optin.py --all-off exited with {process.returncode}: {stderr_summary(process.stderr)}")
    fleets = json.loads(out.read_text())
    if not fleets:
        raise RuntimeError("no built-in CTEM fleet to switch off")
    for slug, row in fleets.items():
        if row["enabled"]:
            raise RuntimeError(f"{slug} keeps {row['enabled']} automation(s) enabled")
        health = api("GET", f"/fleets/{row['id']}/health", token)
        counts = health.get("automations") if isinstance(health, dict) else None
        if isinstance(counts, dict) and counts.get("enabled"):
            raise RuntimeError(f"{slug} reads health automations {counts}")
    return {"CTEM fleets all off": {"id": ",".join(sorted(fleets)), "url": f"{base}/"}}


def seed_fleet_runs(token: str, base: str) -> dict[str, dict[str, str]]:
    """Fourteen runs on the seeded fleet's members, one per trigger source and outcome (XTM One mode).

    So the CTEM Fleet timeline on ``/`` and ``GET /fleets/{id}/activity`` answer runs started by an
    integration event (four providers), a webhook, a work item, a chat handover, a chain, a schedule
    and by hand, and runs that failed, were interrupted, cancelled or are running, with no fetch
    override. No API creates a run with a given trigger: ``seed_fleet_runs.py`` writes them through
    the backend's own model, with its interpreter and environment. May raise: the caller logs and skips.
    """
    if "Orchestrator" not in LAB_AGENTS:
        raise RuntimeError("no fleet agents")
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "fleet_runs.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_fleet_runs.py"), admin,
         json.dumps(LAB_AGENTS), str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_fleet_runs.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    rows = json.loads(out.read_text())
    return {"fleet runs by trigger": {"id": ",".join(rows), "url": f"{base}/"}}


def seed_ctem_fleet_event_runs(token: str, base: str) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """One event-driven run with its trace on each built-in CTEM fleet (XTM One mode, #10598).

    ``seed_ctem_fleet_event_runs.py`` writes, through the backend's own model with its interpreter and
    environment, one completed orchestrator run per fleet, started by the event its own assignment
    subscribes to (provider and event read from the assignment, never invented), its consults of the
    members as agentic steps and one child run per consulted member. Each run is read back with
    ``GET /assignments/runs/{id}`` (trigger, completed, more than one step) and in the fleet's activity.
    Answers ``seeded CTEM fleet event run: <fleet>`` entries (the run page) and ``not seeded`` reasons.
    May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "ctem_fleet_event_runs.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_ctem_fleet_event_runs.py"), admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_ctem_fleet_event_runs.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    answer = json.loads(out.read_text())
    seeded: dict[str, dict[str, str]] = {}
    skipped = {f"CTEM fleet event run: {name}": why for name, why in answer.get("skipped", {}).items()}
    for fleet in answer.get("seeded", []):
        label = f"CTEM fleet event run: {fleet['name']}"
        run = api("GET", f"/assignments/runs/{fleet['run']}", token)
        context = run.get("trigger_context") or {}
        steps = run.get("agentic_steps") or []
        activity = api("GET", f"/fleets/{fleet['id']}/activity", token)
        listed = json.dumps(activity) if activity is not None else ""
        gaps = [
            why for why, bad in (
                (f"trigger {context.get('type')}", context.get("type") != fleet["trigger"]["type"]),
                (f"status {run.get('status')}", run.get("status") != "completed"),
                (f"{len(steps)} agentic step(s)", len(steps) < 2),
                ("not in GET /fleets/{id}/activity", fleet["run"] not in listed),
            ) if bad
        ]
        if gaps:
            skipped[label] = f"run {fleet['run']} read back with {', '.join(gaps)}"
            continue
        seeded[label] = {"id": fleet["run"], "url": f"{base}/operations/executions/{fleet['run']}"}
    return seeded, skipped


def seed_video_findings(token: str, base: str) -> dict[str, dict[str, str]]:
    """One video whose Renders tab lists every finding kind, so a finding's row renders without a fetch override.

    The video is created through the API; the lab has no render worker and no API writes a finished render:
    ``seed_video_findings.py`` writes failed whole renders (at most 3 kinds each, so every finding row shows) and a
    scene preview through the backend's own model, with its interpreter and environment. One seeded entry per
    render names its kinds, so the start log prints one line per kind group. May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    video = _created(api("POST", "/video-studio/projects", token, {"spec": {
        "title": f"{SEED_NAME} video render findings", "freestyle": True,
        "scenes": [{"id": "open", "type": "title", "title": "Seeded by the lab"}],
    }}))
    out = ROOT / "video_findings.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_video_findings.py"), video, admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_video_findings.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    written = json.loads(out.read_text())
    url = f"{base}/videos/{video}"
    seeded = {"video render findings": {"id": video, "url": url}}
    for row in written.get("renders") or []:
        seeded[f"video render findings {', '.join(row['kinds'])}"] = {"id": row["id"], "url": url}
    return seeded


def seed_video_render_gate(token: str, base: str, one: bool = False) -> dict[str, dict[str, str]]:
    """A second video whose only whole render left open ``fail`` findings the quality gate refuses on, so clicking
    Render opens the refused-render dialog (``video.editor.render.gate``) on real rows.

    The gate reads the latest whole render of the revision alone, so the findings video above refuses on its last
    kind group only: this one carries one ``fail`` per kind of ``LAB_VIDEO_GATE_KINDS`` (comma-separated), every
    ``PICTURE_KINDS`` kind when unset, written by ``seed_video_findings.py --gate``. With *one*, a third video whose
    render carries the first of those kinds alone (``--gate-one``), so the dialog's reason renders its singular form
    ("1 finding") on a real row. May raise: the caller logs and skips.
    """
    name = "video render gate (one finding)" if one else "video render gate"
    admin = _created(api("GET", "/auth/me", token))
    video = _created(api("POST", "/video-studio/projects", token, {"spec": {
        "title": f"{SEED_NAME} {name}", "freestyle": True,
        "scenes": [{"id": "open", "type": "title", "title": "Seeded by the lab"}],
    }}))
    out = ROOT / ("video_render_gate_one.json" if one else "video_render_gate.json")
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_video_findings.py"), video, admin, str(out),
         "--gate-one" if one else "--gate", os.environ.get("LAB_VIDEO_GATE_KINDS", "")],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_video_findings.py {process.args[5]} exited with {process.returncode}: "
                           f"{stderr_summary(process.stderr)}")
    gate = json.loads(out.read_text())["gate"]
    url = f"{base}/videos/{video}"
    # The start log prints the video, then the kinds its render refuses on (one line, beside the render id).
    return {
        name: {"id": video, "url": url},
        f"{name} {', '.join(gate['kinds'])}": {"id": gate["id"], "url": url},
    }


def seed_video_media(token: str, base: str) -> dict[str, dict[str, str]]:
    """The Media tab of Video Studio on real rows: 64 recordings and AI shots, a third used by a video (#10330).

    The lab has no recorder and no video model, and no API writes a finished recording or shot:
    ``seed_video_media.py`` writes them, and the free-style video naming every third one, through the backend's own
    models with its interpreter and environment. A restart finds them again. May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "video_media.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_video_media.py"), admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_video_media.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    written = json.loads(out.read_text())
    if not written.get("media"):
        raise RuntimeError("seed_video_media.py wrote no media")
    return {"video media library": {"id": written.get("video") or "", "url": f"{base}/videos/media"}}


def seed_builtin_fleet(base: str) -> dict[str, dict[str, str]]:
    """One built-in fleet (``is_builtin``, company-managed), so the Built-in badge and read-only states render.

    The API keeps built-in fleets read-only and creates none: ``seed_builtin_fleet.py`` writes one
    through the backend's own built-in fleet seed, with its interpreter and environment. May raise:
    the caller logs and skips.
    """
    out = ROOT / "builtin_fleet.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_builtin_fleet.py"),
         os.environ.get("PLATFORM_MODE", ""), str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_builtin_fleet.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    fleet = json.loads(out.read_text())
    return {"built-in fleet": {"id": fleet["id"], "url": f"{base}/fleets/{fleet['id']}"}}


#: The Suite platforms a registration adds (``integration_catalog.REGISTRATION_ONLY_PROVIDERS``),
#: each at a URL on the reserved ``.invalid`` TLD: nothing answers it, nothing leaves the lab.
SUITE_PLATFORMS: dict[str, str] = {
    "opencti": "OpenCTI",
    "openaev": "OpenAEV",
    "opencrq": "OpenCRQ",
}

#: The intents each Suite platform declares at registration (``RegisterRequest.intents``), synced by the real
#: ``_sync_platform_intents`` into ``PlatformIntentDeclaration`` rows, so ``/intents`` shows the Platform column,
#: the Platform filter values and the Unbound usage state on real rows. ``global.summarize`` is declared by all
#: three; ``LAB_UNBOUND_INTENT`` is declared by OpenCTI and bound to no agent (Unbound).
LAB_UNBOUND_INTENT = "cti.lab_sample_unbound"
SUITE_INTENTS: dict[str, list[dict[str, str]]] = {
    "opencti": [
        {"name": "global.summarize", "description": "Summarize content"},
        {"name": "cti.container_summary", "description": "Summarize an OpenCTI container (report, grouping, case)"},
        {"name": "cti.nlq_search", "description": "Generate an OpenCTI filter from a natural language query"},
        {"name": LAB_UNBOUND_INTENT, "description": "Lab sample: declared by OpenCTI, bound to no agent"},
    ],
    "openaev": [
        {"name": "global.summarize", "description": "Summarize content"},
        {"name": "aev.message_generator", "description": "Generate messages for adversary emulation exercises"},
        {"name": "aev.detection_rules_generator", "description": "Generate detection rules from malware behavior"},
    ],
    "opencrq": [
        {"name": "global.summarize", "description": "Summarize content"},
        {"name": "grc.assessment_summary", "description": "Summarize a compliance assessment and its requirement answers"},
    ],
}


def seed_platform_intents_check(token: str, base: str) -> dict[str, str]:
    """Read ``GET /intents`` back after the Suite registrations declared ``SUITE_INTENTS``. May raise.

    Raises unless at least one intent carries a platform and ``LAB_UNBOUND_INTENT`` reads Unbound (declared,
    no binding). Logged ``seeded platform intents: <base>/intents``.
    """
    rows = api("GET", "/intents", token)
    rows = rows.get("items", []) if isinstance(rows, dict) else rows
    platforms = sorted({p for r in rows for p in (r.get("platforms") or [])})
    if not platforms:
        raise RuntimeError("GET /intents lists no intent declared by a platform")
    unbound = [r["name"] for r in rows if r.get("platforms") and not r.get("bindings")]
    if LAB_UNBOUND_INTENT not in unbound:
        raise RuntimeError(f"{LAB_UNBOUND_INTENT} is not Unbound in GET /intents (unbound: {unbound[:5]})")
    return {"id": ",".join(platforms), "url": f"{base}/intents"}


#: The lab's own OpenCTI ([service.lab-ootb-live]): ``LAB_OPENCTI = "live"`` in the declaration's env.
LIVE_OPENCTI_URL = "http://localhost:4000"
#: Seconds ``seed_live_opencti`` waits for the lab's OpenCTI to register itself: its manager fires 30 s after
#: boot, before this backend answers, then every 5 minutes (``XTM_ONE_SCHEDULE_TIME``), so one tick plus margin.
LIVE_OPENCTI_REGISTER_SECONDS = 420
#: The path the OpenCTI of ``[service.lab-opencti-ctem]`` serves under (``APP__BASE_PATH``): the Vite server of
#: ``[service.lab-ctem-opencti]`` proxies it (``VITE_OPENCTI_URL``), so the browser opens it on the preview origin (#12058).
CTEM_OPENCTI_PATH = "/opencti"
#: The admin token of the lab's OpenCTI (``APP__ADMIN__TOKEN`` of ``[service.lab-opencti-ctem]``): lab-only.
CTEM_OPENCTI_TOKEN = "00000000-0000-4000-8000-000000000001"  # noqa: S105 - lab-only value
#: The Software observables ``seed_ctem_opencti`` writes: the first two products of ``LAB_ORG_TECH_STACK``.
CTEM_OPENCTI_SOFTWARE = (("Microsoft", "Exchange Server"), ("Fortinet", "FortiOS"))
#: (name, CVSS base score, CISA KEV, EPSS, the Software index it is related to by ``has`` or None).
#: Known 10, critical (CVSS >= 9) 4, KEV 3; relevant (the stack's) 5, of which exploitable (KEV or EPSS > 0.5) 3.
CTEM_OPENCTI_VULNERABILITIES = (
    ("CVE-2026-90001", 9.8, True, 0.97, 0),
    ("CVE-2026-90002", 8.1, True, 0.42, 1),
    ("CVE-2026-90003", 7.5, False, 0.91, 0),
    ("CVE-2026-90004", 6.5, False, 0.02, 1),
    ("CVE-2026-90005", 5.3, False, 0.01, 0),
    ("CVE-2026-90006", 9.1, True, 0.30, None),
    ("CVE-2026-90007", 9.9, False, 0.05, None),
    ("CVE-2026-90008", 4.3, False, 0.01, None),
    ("CVE-2026-90009", 9.0, False, 0.20, None),
    ("CVE-2026-90010", 3.1, False, 0.00, None),
)
#: ``LAB_CTEM_OPENCTI`` values: ``on`` seeds the lab's own OpenCTI and collects the CTEM posture live from it (#12058).
LAB_CTEM_OPENCTI_STATES = ("off", "on")
#: The Prioritization KPI labels (``ctem_verticals.STAGE_FACTS``) ``seed_ctem_opencti`` reads back, by expected count.
CTEM_OPENCTI_KPI_FACTS = {"Known-exploited (KEV)": "kev", "Critical CVEs": "critical"}


def ctem_opencti_expected() -> dict[str, int]:
    """The counts ``CTEM_OPENCTI_VULNERABILITIES`` gives each CTEM figure: what the funnel and the KPIs must read."""
    rows = CTEM_OPENCTI_VULNERABILITIES
    relevant = [r for r in rows if r[4] is not None]
    return {
        "known": len(rows),
        "critical": sum(1 for r in rows if r[1] >= 9),
        "kev": sum(1 for r in rows if r[2]),
        "relevant": len(relevant),
        "exploitable": sum(1 for r in relevant if r[2] or r[3] > 0.5),
    }


def ctem_opencti_graphql(query: str, variables: dict) -> dict:
    """One GraphQL call to the lab's own OpenCTI as its admin; an error answer is raised."""
    request = urllib.request.Request(
        f"{LIVE_OPENCTI_URL}{CTEM_OPENCTI_PATH}/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {CTEM_OPENCTI_TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        answer = json.loads(response.read() or b"null")
    if not isinstance(answer, dict) or answer.get("errors"):
        raise RuntimeError(f"OpenCTI answered {str(answer)[:300]}")
    return answer.get("data") or {}


def seed_ctem_opencti(token: str, base: str) -> dict[str, dict[str, str]]:
    """``LAB_CTEM_OPENCTI=on`` (#12058): vulnerabilities in the lab's OpenCTI, then the live posture read back. May raise.

    Writes ``CTEM_OPENCTI_SOFTWARE`` and ``CTEM_OPENCTI_VULNERABILITIES`` with their ``has`` relations through
    OpenCTI's own API (OpenCTI upserts them, so a restart writes nothing twice), forces a posture rebuild
    (``POST /ctem-metrics/sync``) and checks the funnel and the KPI counts against ``ctem_opencti_expected``,
    the tailored layers carrying their OpenCTI filters. Logged ``seeded CTEM OpenCTI vulnerabilities (N)`` and
    ``seeded CTEM funnel live from OpenCTI``.
    """
    software = []
    for vendor, name in CTEM_OPENCTI_SOFTWARE:
        data = ctem_opencti_graphql(
            "mutation($input: SoftwareAddInput) { stixCyberObservableAdd(type: \"Software\", Software: $input) { id } }",
            {"input": {"name": name, "vendor": vendor}},
        )
        software.append(data["stixCyberObservableAdd"]["id"])
    for name, cvss, kev, epss, related in CTEM_OPENCTI_VULNERABILITIES:
        data = ctem_opencti_graphql(
            "mutation($input: VulnerabilityAddInput!) { vulnerabilityAdd(input: $input) { id } }",
            {"input": {"name": name, "x_opencti_cvss_base_score": cvss, "x_opencti_cisa_kev": kev, "x_opencti_epss_score": epss}},
        )
        if related is not None:
            ctem_opencti_graphql(
                "mutation($input: StixCoreRelationshipAddInput!) { stixCoreRelationshipAdd(input: $input) { id } }",
                {"input": {"fromId": software[related], "toId": data["vulnerabilityAdd"]["id"], "relationship_type": "has"}},
            )
    api("POST", "/ctem-metrics/sync?vertical=cyber", token)
    posture = api("GET", "/ctem-metrics/posture?vertical=cyber", token)
    expected = ctem_opencti_expected()
    prio = next((s for s in posture.get("stages") or [] if s.get("key") == "prioritization"), {}) if isinstance(posture, dict) else {}
    funnel = ((prio.get("viz") or {}).get("funnel") or {}).get("layers") or []
    layers = {layer.get("key"): layer for layer in funnel}
    for key in ("known", "relevant", "exploitable"):
        layer = layers.get(key) or {}
        if layer.get("count") != expected[key]:
            raise RuntimeError(f"the funnel {key} layer reads {layer.get('count')!r}, OpenCTI holds {expected[key]}")
        if key != "known" and not layer.get("filters"):
            raise RuntimeError(f"the funnel {key} layer carries no OpenCTI filter")
    # The KEV and critical KPIs read OpenCTI's own counts, no LARGE_FACTS overlay (#12220: it read 42 / 1240).
    kpis = {fact.get("label"): fact.get("value") for fact in prio.get("facts") or []}
    for label, key in CTEM_OPENCTI_KPI_FACTS.items():
        if kpis.get(label) != expected[key]:
            raise RuntimeError(f"the {label} KPI reads {kpis.get(label)!r}, OpenCTI holds {expected[key]}")
    vulns = len(CTEM_OPENCTI_VULNERABILITIES)
    return {
        f"CTEM OpenCTI vulnerabilities ({vulns})": {"id": ",".join(r[0] for r in CTEM_OPENCTI_VULNERABILITIES[:3]),
                                                   "url": f"{base}{CTEM_OPENCTI_PATH}/dashboard/arsenal/vulnerabilities"},
        "CTEM funnel live from OpenCTI": {"id": ",".join(f"{k}={expected[k]}" for k in expected),
                                          "url": f"{base}/ctem/prioritization"},
    }


def live_opencti() -> bool:
    """Whether this lab runs an OpenCTI of its own (``LAB_OPENCTI = "live"``), registered through the real path."""
    return os.environ.get("LAB_OPENCTI", "").strip().lower() == "live"


def seed_live_opencti(token: str, base: str, *, wait: float = LIVE_OPENCTI_REGISTER_SECONDS) -> dict[str, str]:
    """The company-managed OpenCTI integration the lab's own OpenCTI registered; raises when none appears in time.

    The OpenCTI of ``[service.lab-ootb-live]`` holds the lab's registration token (``XTM__XTM_ONE_TOKEN``) and
    posts ``/platform/register`` itself, so the row, its intents and its per-user tokens are the product's own,
    nothing written by the seed. Logged ``seeded live OpenCTI integration: <url>``.
    """
    deadline = time.monotonic() + wait
    while True:
        rows = api("GET", "/integrations", token)
        rows = rows.get("items", []) if isinstance(rows, dict) else rows
        row = next((r for r in rows if r.get("provider") == "opencti" and r.get("is_company_managed")), None)
        if row is not None:
            return {"id": str(row["id"]), "url": f"{base}/integrations/{row['id']}"}
        if time.monotonic() > deadline:
            raise RuntimeError(f"the lab's OpenCTI ({LIVE_OPENCTI_URL}) did not register within {wait:.0f}s")
        time.sleep(5)


def seed_suite_registrations(token: str, base: str) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """One registered integration per Suite platform, and one ``integration_event`` assignment on each.

    In XTM One mode these providers are registration-only: no form creates them. Each goes through
    the real ``POST /platform/register`` with the lab's registration token and a ``platform_url`` on
    ``.invalid``, no credential (a provisioner may record an error state). One agent then holds one
    ``integration_event`` assignment per provider, bound to its integration, on the first event
    ``GET /integrations/events`` lists; a provider with none is named, not seeded. Never raises.
    """
    seeded: dict[str, dict[str, str]] = {}
    skipped: dict[str, str] = {}
    registered: dict[str, str] = {}
    for provider, title in SUITE_PLATFORMS.items():
        entity = f"registered {title} integration"
        if provider == "opencti" and live_opencti():
            # The lab's own OpenCTI registers itself (its XTM One registration manager): no .invalid row beside it.
            skipped[entity] = "registered by the lab's own OpenCTI (LAB_OPENCTI=live): see seed_live_opencti"
            continue
        if provider in ctem_sample_unregistered():
            skipped[entity] = (
                "left unregistered by LAB_CTEM_SAMPLE_STATE or LAB_CTEM_MODULES: OpenCRQ absent"
                if provider == "opencrq"
                else "left unregistered by LAB_CTEM_SAMPLE: the CTEM blind spot of [service.lab-ctem]"
            )
            continue
        try:
            api("POST", "/platform/register", LAB_REGISTRATION_TOKEN, {
                "platform_identifier": provider, "platform_url": f"https://{provider}.lab.invalid",
                "platform_title": f"{SEED_NAME} {title}", "platform_version": "lab",
                "intents": SUITE_INTENTS[provider],
            })
            rows = api("GET", "/integrations", token)
            rows = rows.get("items", []) if isinstance(rows, dict) else rows
            row = next((r for r in rows if r.get("provider") == provider and r.get("is_company_managed")), None)
            if row is None:
                raise RuntimeError(f"registered, but GET /integrations lists no company-managed {provider} row")
            registered[provider] = str(row["id"])
            seeded[entity] = {"id": registered[provider], "url": f"{base}/integrations/{registered[provider]}"}
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped[entity] = str(exc)[:600]
    if not registered:
        return seeded, skipped
    try:
        events = api("GET", "/integrations/events", token)
        agent = _seed_agent(token, "Suite listener")
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["Suite integration_event assignments"] = str(exc)[:600]
        return seeded, skipped
    for provider, integration in registered.items():
        entity = f"{SUITE_PLATFORMS[provider]} integration_event assignment"
        listed = events.get(provider) if isinstance(events, dict) else None
        if not listed:
            skipped[entity] = f"GET /integrations/events lists no {provider} event"
            continue
        try:
            assignment = _created(api("POST", "/assignments", token, {
                "agent_id": agent, "name": f"{SEED_NAME} {SUITE_PLATFORMS[provider]} event",
                "description": "Seeded by the lab.", "prompt": "Summarise the event.",
                "trigger_type": "integration_event",
                "trigger_config": {"provider": provider, "event": listed[0]["event"], "integration_id": integration},
            }))
            seeded[entity] = {"id": assignment, "url": f"{base}/agents/assignments/{assignment}"}
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped[entity] = str(exc)[:600]
    return seeded, skipped


def ctem_sample_unregistered() -> tuple[str, ...]:
    """The Suite platforms a CTEM lab switch leaves unregistered.

    ``LAB_CTEM_SAMPLE=1``: OpenAEV, a primary CTEM source (the blind spot of ``[service.lab-ctem]``).
    ``LAB_CTEM_SAMPLE_STATE=on``: OpenCRQ, whose ``present`` is registration-driven; OpenCTI and OpenAEV stay
    registered so ``/`` opens the command center, not the welcome landing, and answer no fact (``lab_asgi``).
    """
    if lab_switch("LAB_CTEM_SAMPLE_STATE", LAB_CTEM_SAMPLE_STATES) == "on":
        return ("opencrq",)
    if os.environ.get("LAB_CTEM_MODULES", "").strip().lower() in ("cti", "cti-aev", "cti-unscored", "none"):
        # ``LAB_CTEM_MODULES`` (#11998, #12220): OpenCRQ's ``present`` is registration-driven, absent in these states.
        return ("opencrq",)
    return ("openaev",) if os.environ.get("LAB_CTEM_SAMPLE") == "1" else ()


def seed_ctem_sample(token: str, base: str) -> dict[str, dict[str, str]]:
    """Three recorded CTEM posture days of the administrator before today (``[service.lab-ctem]``).

    ``lab_asgi`` answers today's collection from ``ctem_sample``; no API writes a past snapshot, so
    ``seed_ctem_history.py`` writes the earlier days through the backend's own model and scoring,
    with its interpreter and environment. May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "ctem_history.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_ctem_history.py"), admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_ctem_history.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    rows = json.loads(out.read_text())
    return {"CTEM coverage and history": {"id": ",".join(rows), "url": f"{base}/"}}


def seed_ctem_screens(token: str, base: str) -> dict[str, dict[str, str]]:
    """The CTEM cases of ``[service.lab-ctem-screens]`` (``LAB_CTEM_SCREENS=on``, #11713), read back. May raise.

    ``lab_asgi`` answers the Cases source read with ``ctem_sample.SCREENS_ISSUES``; this runs the real recompute
    (``POST /ctem-cases/recompute``), which groups them and writes the cases, then reads the list back. A restart
    recomputes into the same cases (stable keys); a 429 (one recompute per minute) keeps the cases already there.

    Since #1529 the recompute opens a case only through the scope gate, so the scope comes first
    (:func:`seed_ctem_screens_scope`): without it every issue is a watchlisted signal and the list is empty.
    """
    seed_ctem_screens_scope(token)
    try:
        answer = api("POST", "/ctem-cases/recompute?vertical=cyber", token)
    except RuntimeError as exc:
        if ": 429 " not in str(exc):
            raise
        answer = {"applied": True}
    if not isinstance(answer, dict) or not answer.get("applied"):
        raise RuntimeError(f"the CTEM cases recompute answered {str(answer)[:300]}")
    listed = api("GET", "/ctem-cases?vertical=cyber&limit=50", token)
    cases = listed.get("cases") if isinstance(listed, dict) else None
    if not cases or len(cases) < 3:
        raise RuntimeError(f"the CTEM cases list answered {str(listed)[:300]}")
    posture = api("GET", "/ctem-metrics/posture?vertical=cyber", token)
    stages = posture.get("stages") if isinstance(posture, dict) else None
    scored = [s["key"] for s in stages or [] if s.get("score")]
    if len(scored) != 5:
        raise RuntimeError(f"the CTEM posture scored {scored}")
    return {
        f"CTEM cases ({len(cases)})": {"id": ",".join(c["id"] for c in cases), "url": f"{base}/ctem/cases"},
        "CTEM stages scored and funnel": {"id": ",".join(scored), "url": f"{base}/ctem/prioritization"},
    }


def seed_ctem_gates(token: str, base: str) -> dict[str, dict[str, str]]:
    """The gate decision records of the first CTEM case (#1628), read back. May raise.

    The recompute writes no gate work item, decision row or ``gate`` event, and no API writes a decision in a
    chosen state: ``seed_ctem_gates.py`` writes them through the backend's own models (one gate answered,
    waiting, expired, cancelled and passed automatically), with its interpreter and environment. Read back on
    ``GET /ctem-cases/{id}``: its ``gates`` carry at least the answered, waiting and passed records. A restart
    finds the gate items by their keys and writes nothing.
    """
    admin = _created(api("GET", "/auth/me", token))
    listed = api("GET", "/ctem-cases?vertical=cyber&limit=50", token)
    cases = listed.get("cases") if isinstance(listed, dict) else None
    if not cases:
        raise RuntimeError(f"no CTEM case to seed the gates on: {str(listed)[:300]}")
    case_id = str(cases[0]["id"])
    out = ROOT / "ctem_gates.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_ctem_gates.py"), admin, case_id, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_ctem_gates.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    case = api("GET", f"/ctem-cases/{case_id}", token)
    statuses = {g.get("key"): g.get("status") for g in (case.get("gates") or [])} if isinstance(case, dict) else {}
    wanted = {"answered", "waiting", "passed_automatically"}
    if not wanted <= set(statuses.values()):
        raise RuntimeError(f"the CTEM case gates read back {statuses}")
    return {f"CTEM case gates ({len(statuses)})": {"id": case_id, "url": f"{base}/ctem/cases/{case_id}"}}


def seed_ctem_screens_scope(token: str) -> None:
    """Declare the scope the screens issues land in (#1529): tech stack products and the assets running them.

    Adds ``ctem_sample.SCREENS_TECH_STACK`` to the saved profile (the products already there are kept) and
    writes ``ctem_sample.SCREENS_ASSETS`` through the asset records (``source`` ``lab``, stable external ids,
    so a restart updates nothing). Read back: the gate resolves each issue to a ``very_high`` asset and opens
    its case. ``LAB_ORG_CONTEXT=missing`` declares nothing: that lab renders the screens with no profile. May raise.
    """
    if lab_switch("LAB_ORG_CONTEXT", LAB_ORG_CONTEXT_STATES) == "missing":
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ctem_sample  # beside this file, standard library only

    current = api("GET", "/organization-context", token)
    profile = dict(current.get("profile") or {}) if isinstance(current, dict) else {}
    stack = list(profile.get("tech_stack") or [])
    have = {str(entry.get("product", "")).lower() for entry in stack}
    missing = [dict(entry) for entry in ctem_sample.SCREENS_TECH_STACK if entry["product"].lower() not in have]
    if missing or not profile.get("name"):
        profile = {**LAB_ORG_PROFILE, **profile, "tech_stack": stack + missing}
        body = {"profile": profile, "source": "form"}
        if isinstance(current, dict) and current.get("version") is not None:
            body["based_on_version"] = current["version"]
        api("PUT", "/organization-context", token, body)
    api("POST", "/organization-context/assets/records/batch", token, {
        "rows": [dict(row) for row in ctem_sample.SCREENS_ASSETS],
    })


def seed_video_ready_render(token: str, base: str) -> dict[str, dict[str, str]]:
    """One video whose current render is ready (#11713), found again on a restart. May raise: the start stops.

    The lab has no render worker and no API writes a finished render: ``seed_video_ready_render.py`` stores the
    committed MP4 and poster of ``fixtures/`` (no ffmpeg needed: ``python-dev`` has none) and writes the video, the
    render and its files through the backend's own models, with its interpreter and environment.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "video_ready_render.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_video_ready_render.py"), admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=180,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_video_ready_render.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    written = json.loads(out.read_text())
    video = api("GET", f"/video-studio/projects/{written['video']}", token)
    ready = video.get("ready_render") if isinstance(video, dict) else None
    if not isinstance(ready, dict) or str(ready.get("id") or "") != written["render"]:
        raise RuntimeError(f"the video read back without its ready render: {str(video)[:300]}")
    url = f"{base}/videos/{written['video']}"
    return {"video ready render": {"id": written["render"], "url": url}}


def seed_report(token: str, base: str) -> dict[str, dict[str, str]]:
    """One generated report with every block kind, found again on a restart. May raise.

    No API creates a report and the lab's stub model cannot call ``generate_report``: ``seed_report.py`` writes it
    through the platform's own ``save_report`` (the tool's validation), with the backend's interpreter and
    environment. Read back on ``GET /reports/{id}``.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "report.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_report.py"), admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_report.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    written = json.loads(out.read_text())
    report = api("GET", f"/reports/{written['report']}", token)
    if not isinstance(report, dict) or str(report.get("id") or "") != written["report"]:
        raise RuntimeError(f"the report read back without its id: {str(report)[:300]}")
    return {"report": {"id": written["report"], "url": f"{base}/reports/{written['report']}"}}


def seed_ctem_posture_history(token: str, base: str) -> dict[str, str]:
    """Ninety days of recorded CTEM posture of the administrator before today. May raise.

    No API writes a past snapshot, so ``seed_ctem_posture_history.py`` writes them through the
    backend's own model and scoring, with its interpreter and environment: every trend surface
    (the stage History card and its 30 / 90 days switch, the command center trend and deltas) has
    a rising, a falling, a flat, an old-only and a single-day stage with no fetch override.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "ctem_posture_history.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_ctem_posture_history.py"), admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_ctem_posture_history.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    days = json.loads(out.read_text())
    return {"id": f"{len(days)} recorded days", "url": f"{base}/ctem/discovery"}


def seeds_ctem_large_facts() -> bool:
    """Whether ``lab_asgi`` lays ``ctem_sample.LARGE_FACTS`` over the cyber facts (its ``large_ctem_facts`` rule)."""
    # The same list as ``ctem_sample.CTEM_SAMPLE_SWITCHES`` (lab_asgi's), pinned by test: this script imports no sample.
    switches = (
        "LAB_CTEM_FINDINGS", "LAB_CTEM_ACTIONS", "LAB_CTEM_ACTIVITY", "LAB_CTEM_SAMPLE_STATE", "LAB_CTEM_SCREENS",
        "LAB_CTEM_OPENCTI",
    )
    if os.environ.get("LAB_CTEM_MODULES", "").strip():
        return False
    return seeds_ctem_posture_history() and not any(
        os.environ.get(name, "").strip().lower() == "on" for name in switches
    )


#: The company dashboard of figures of 1,000 and more (``seed_dashboard_figures``): a value tile with its change and
#: previous value, a small tile and an Exposure console, on static data. A static number keeps no ``subject`` or
#: ``span`` (``closed_static_data``), so the change sentence of a platform tile does not draw here.
LAB_DASHBOARD_FIGURES_TITLE = "Lab sample figures"
LAB_DASHBOARD_FIGURES: dict = {
    "title": LAB_DASHBOARD_FIGURES_TITLE,
    "description": "Figures of 1,000 and more beside small ones, for the compact-number review (621K, exact on hover).",
    "dataSources": {
        "findings": {"type": "static", "options": {"shape": "number", "data": {
            "value": 620965, "change": 1240, "previous": 619725, "period": "interval",
        }}},
        "kev": {"type": "static", "options": {"shape": "number", "data": {"value": 42, "change": -3, "previous": 45}}},
        "outcomes": {"type": "static", "options": {"shape": "outcomes", "data": {"items": [
            {"label": "Prevention", "success": 620965, "failed": 1240, "pending": 42},
            {"label": "Detection", "success": 15400, "failed": 2310, "pending": 999},
            {"label": "Vulnerability", "success": 999, "failed": 1240, "pending": 0},
            {"label": "Human response", "success": 42, "failed": 7, "pending": 3},
        ]}}},
    },
    "visualizations": {
        "findings_tile": {"type": "xtm.singlevalue", "title": "Findings", "dataSources": {"primary": "findings"}},
        "kev_tile": {"type": "xtm.singlevalue", "title": "Known-exploited (KEV)", "dataSources": {"primary": "kev"}},
        "console": {"type": "xtm.exposureconsole", "title": "Validation outcomes", "dataSources": {"primary": "outcomes"}},
    },
    "layout": {
        "tabs": {"items": [{"layoutId": "main", "label": "Figures"}]},
        "layoutDefinitions": {"main": {"structure": [
            {"item": "findings_tile", "position": {"x": 0, "y": 0, "w": 6, "h": 6}},
            {"item": "kev_tile", "position": {"x": 6, "y": 0, "w": 3, "h": 4}},
            {"item": "console", "position": {"x": 0, "y": 6, "w": 12, "h": 10}},
        ]}},
    },
}


def seed_dashboard_figures(token: str, base: str) -> dict[str, str]:
    """The company dashboard :data:`LAB_DASHBOARD_FIGURES`, written through the routes the studio calls. May raise."""
    created = api("POST", "/custom-dashboards", token, {"title": LAB_DASHBOARD_FIGURES_TITLE})
    dashboard_id = _created(created)
    revision = int(created.get("revision") or 1) if isinstance(created, dict) else 1
    api("PUT", f"/custom-dashboards/{dashboard_id}/definition", token,
        {"revision": revision, "definition": LAB_DASHBOARD_FIGURES})
    api("PUT", f"/custom-dashboards/{dashboard_id}/sharing", token, {"is_company_managed": True})
    return {"id": dashboard_id, "url": f"{base}/dashboards/{dashboard_id}"}


#: The dashboard the studio rail of the Copilot lab opens on (``seed_dashboard_studio``): the figures dashboard
#: under its own title, so a review of StudioRail or DashboardStudioRail has widgets to focus.
LAB_DASHBOARD_STUDIO_TITLE = "Lab dashboard studio"
#: The built-in tool DashboardStudioRail asks for (``useToolAgents("custom_dashboards")``).
LAB_DASHBOARD_TOOL_ID = "custom_dashboards"


def seed_dashboard_studio(token: str, base: str) -> dict[str, dict[str, str]]:
    """One agent carrying the Custom dashboards tool and one dashboard with widgets; answer both rows.

    Without an agent carrying ``custom_dashboards`` (and with the General Assistant not offered), the
    studio rail draws "No agent carries the dashboard tools yet" and never its composer or focus chip.
    The agent is company-managed and read back: a tool list that does not carry the tool raises.
    Logged ``seeded dashboard studio: <base>/dashboards/<id>/studio``. May raise: the caller logs and skips.
    """
    agent = _created(api("POST", "/agents", token, {
        "name": f"{SEED_NAME} dashboard builder", "is_company_managed": True,
        "description": "Seeded by the lab: carries the Custom dashboards tool, so the dashboard studio rail draws.",
        "persona": "You are the lab's dashboard builder.",
        "builtin_tool_ids": [LAB_DASHBOARD_TOOL_ID],
    }))
    row = api("GET", f"/agents/{agent}", token)
    tools = (row.get("builtin_tool_ids") or []) if isinstance(row, dict) else []
    if LAB_DASHBOARD_TOOL_ID not in tools:
        raise RuntimeError(f"agent created, but its built-in tools read {tools}")
    created = api("POST", "/custom-dashboards", token, {"title": LAB_DASHBOARD_STUDIO_TITLE})
    dashboard_id = _created(created)
    revision = int(created.get("revision") or 1) if isinstance(created, dict) else 1
    api("PUT", f"/custom-dashboards/{dashboard_id}/definition", token,
        {"revision": revision, "definition": {**LAB_DASHBOARD_FIGURES, "title": LAB_DASHBOARD_STUDIO_TITLE}})
    return {
        "dashboard studio agent": {"id": agent, "url": f"{base}/agents/{agent}/config/tooling"},
        "dashboard studio": {"id": dashboard_id, "url": f"{base}/dashboards/{dashboard_id}/studio"},
    }


def ctem_sample_state_gaps(posture: object, briefing: object) -> list[str]:
    """What keeps ``/`` from the CTEM sample state, read on the posture and briefing answers; empty when none."""
    gaps: list[str] = []
    if not isinstance(posture, dict) or not isinstance(posture.get("stages"), list):
        gaps.append(f"the posture answers no stages: {str(posture)[:200]}")
    else:
        if posture.get("posture_score") is not None:
            gaps.append(f"posture_score is {posture['posture_score']!r}, not null")
        products = posture.get("products") or {}
        present = sorted(p for p, row in products.items() if isinstance(row, dict) and row.get("present"))
        if present:
            gaps.append(f"products present: {', '.join(present)}")
        covered = [str(s.get("key")) for s in posture["stages"] if isinstance(s, dict) and s.get("covered")]
        if covered:
            gaps.append(f"stages covered: {', '.join(covered)}")
    status = briefing.get("status") if isinstance(briefing, dict) else briefing
    generating = briefing.get("generating") if isinstance(briefing, dict) else None
    if status != "pending" or generating:
        gaps.append(f"the briefing answers status {status!r}, generating {generating!r}")
    return gaps


def seed_ctem_sample_state(token: str, base: str) -> dict[str, str]:
    """``LAB_CTEM_SAMPLE_STATE=on``: ``/`` renders the CTEM sample state on real answers. May raise.

    ``lab_asgi`` answers the posture collection with no product and no fact, and OpenCRQ is left
    unregistered, so ``/ctem-metrics/posture`` answers ``posture_score`` null, no product present and
    no stage covered: the KPI cards and stage cards carry Sample, ``/ctem/:stage`` its header badge.
    The scheduler's CTEM fan-out (``schedule_fanout``) leaves one pending run per user at boot, which
    makes the briefing answer ``generating``; those active runs are deleted (the administrator's
    ``DELETE /assignments/runs/{id}``, as ``seed_ctem_no_model_lock``), then the state is read back:
    the briefing answers ``pending`` (the card's "generate yours, or wait for the monthly run"), and
    with no posture score the command center does not auto-generate it. Lab-only.
    """
    gaps: list[str] = []
    for _attempt in range(4):
        runs = api("GET", "/assignments/runs/all?limit=500", token)
        for run in runs if isinstance(runs, list) else []:
            context = run.get("trigger_context") or {}
            if run.get("status") in ("pending", "delayed", "running") and context.get("ctem_vertical"):
                api("DELETE", f"/assignments/runs/{run['id']}", token)
        gaps = ctem_sample_state_gaps(api("GET", "/ctem-metrics/posture", token), api("GET", "/ctem-metrics/briefing", token))
        if not gaps:
            return {"id": "ctem-sample-state", "url": f"{base}/"}
        time.sleep(5)
    raise RuntimeError(f"the CTEM sample state is not reached: {'; '.join(gaps)}")


def ctem_findings_gaps(posture: object, briefing: object) -> list[str]:
    """What keeps the rule-based Recommended actions from rendering, read on the posture and briefing answers."""
    if not isinstance(posture, dict) or not isinstance(posture.get("stages"), list):
        return [f"the posture answers no stages: {str(posture)[:200]}"]
    gaps: list[str] = []
    products = posture.get("products") or {}
    for product in ("opencti", "openaev"):
        if not (products.get(product) or {}).get("present"):
            gaps.append(f"{product} is not present")
    stages = {str(s.get("key")): s for s in posture["stages"] if isinstance(s, dict)}
    buckets = ((stages.get("validation") or {}).get("viz") or {}).get("expectation_buckets") or {}
    unfailed = [t for t in LAB_CTEM_FINDINGS_TYPES
                if not (buckets.get(t) or {}).get("FAILED", 0) + (buckets.get(t) or {}).get("PARTIAL", 0)]
    if unfailed:
        gaps.append(f"no failed expectation on {', '.join(unfailed)}")
    if not any(not s.get("covered") and s.get("products_present") for s in stages.values()):
        gaps.append("no stage is connected with no data")
    headline = posture.get("headline") or {}
    if not (headline.get("kev_count") or 0) > 0:
        gaps.append(f"headline kev_count is {headline.get('kev_count')!r}")
    coverage = headline.get("detection_coverage_pct")
    if coverage is None or coverage >= 60:
        gaps.append(f"headline detection_coverage_pct is {coverage!r}, not under 60")
    status = briefing.get("status") if isinstance(briefing, dict) else briefing
    if status in ("ready", "generating"):
        gaps.append(f"the briefing answers {status!r}: its next steps replace the rule-based rows")
    return gaps


def seed_ctem_findings(token: str, base: str) -> dict[str, str]:
    """``LAB_CTEM_FINDINGS=on``: ``/dashboard`` renders the rule-based Recommended actions on real answers. May raise.

    ``lab_asgi`` answers the cyber posture collection with ``ctem_sample.findings_facts`` (OpenCTI and OpenAEV
    present). The declarations also set ``LAB_AI=off``, so the briefing answers ``ai_disabled`` and its next steps
    never replace the rows; the boot fan-out's active CTEM runs are deleted first (as ``seed_ctem_sample_state``).
    The posture and the briefing are then read back: failed expectations on the four types, a connected stage
    with no data, a KEV count and a detection coverage under 60. Lab-only.
    """
    gaps: list[str] = []
    for _attempt in range(4):
        runs = api("GET", "/assignments/runs/all?limit=500", token)
        for run in runs if isinstance(runs, list) else []:
            context = run.get("trigger_context") or {}
            if run.get("status") in ("pending", "delayed", "running") and context.get("ctem_vertical"):
                api("DELETE", f"/assignments/runs/{run['id']}", token)
        gaps = ctem_findings_gaps(api("GET", "/ctem-metrics/posture", token), api("GET", "/ctem-metrics/briefing", token))
        if not gaps:
            return {"id": "ctem-findings", "url": f"{base}/dashboard"}
        time.sleep(5)
    raise RuntimeError(f"the CTEM findings are not reached: {'; '.join(gaps)}")


def ctem_actions_gaps(assessments: dict[str, object], briefing: object, posture: object) -> list[str]:
    """What keeps the Act now queue from its briefing, stage and module rows; empty when none."""
    gaps: list[str] = []
    for stage in LAB_CTEM_ACTIONS_STAGES:
        answer = assessments.get(stage)
        status = answer.get("status") if isinstance(answer, dict) else answer
        action = ((answer.get("assessment") if isinstance(answer, dict) else None) or {}).get("recommended_action")
        if status != "ready" or not action:
            gaps.append(f"the {stage} assessment answers status {status!r}, recommended_action {action!r}")
    status = briefing.get("status") if isinstance(briefing, dict) else briefing
    steps = ((briefing.get("briefing") if isinstance(briefing, dict) else None) or {}).get("next_steps") or []
    if status != "ready" or len(steps) < 3:
        gaps.append(f"the briefing answers status {status!r} with {len(steps)} next steps")
    headline = (posture.get("headline") if isinstance(posture, dict) else None) or {}
    if not (headline.get("kev_count") or 0) > 0:
        gaps.append(f"headline kev_count is {headline.get('kev_count')!r}")
    coverage = headline.get("detection_coverage_pct")
    if coverage is None or coverage >= 60:
        gaps.append(f"headline detection_coverage_pct is {coverage!r}, not under 60")
    return gaps


def seed_ctem_actions(token: str, base: str) -> dict[str, str]:
    """``LAB_CTEM_ACTIONS=on``: ``/`` renders the Act now queue on stored artifacts. May raise.

    ``seed_ctem_actions.py`` stores, with the backend's interpreter and environment, one completed run per
    stage analyst (a ``recommended_action`` each) and one of the briefing analyst (three ``next_steps``, one
    repeating a stage action); ``lab_asgi`` answers the cyber posture with the findings (KEVs, detection
    coverage under 60). The boot fan-out's active CTEM runs are deleted (an active run answers
    ``generating`` over the stored one), then every assessment, the briefing and the posture are read
    back. Logged ``seeded CTEM stage assessments: <url>``. Lab-only, no model call.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "ctem_actions.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_ctem_actions.py"), admin, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_ctem_actions.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    gaps: list[str] = []
    for _attempt in range(4):
        runs = api("GET", "/assignments/runs/all?limit=500", token)
        for run in runs if isinstance(runs, list) else []:
            context = run.get("trigger_context") or {}
            if run.get("status") in ("pending", "delayed", "running") and context.get("ctem_vertical"):
                api("DELETE", f"/assignments/runs/{run['id']}", token)
        assessments = {s: api("GET", f"/ctem-metrics/stages/{s}/assessment", token) for s in LAB_CTEM_ACTIONS_STAGES}
        gaps = ctem_actions_gaps(assessments, api("GET", "/ctem-metrics/briefing", token),
                                 api("GET", "/ctem-metrics/posture", token))
        if not gaps:
            return {"id": ",".join(json.loads(out.read_text())), "url": f"{base}/"}
        time.sleep(5)
    raise RuntimeError(f"the CTEM stage assessments are not ready: {'; '.join(gaps)}")


def seed_ctem_generic_briefing(token: str, base: str) -> dict[str, str]:
    """``LAB_CTEM_ACTIONS=on`` with ``LAB_ORG_SETUP_PROMPT=on``: ``/`` renders "This briefing is generic". May raise.

    The briefing is ready (``seed_ctem_actions``) and no organization profile version is saved, which is the
    state ``[data-testid=ctem-briefing-generic]`` renders on, with no cache override. Logged ``seeded CTEM generic
    briefing: <base>/``. Raises when a profile is saved or the briefing is not ready.
    """
    current = api("GET", "/organization-context", token)
    version = current.get("version") if isinstance(current, dict) else current
    briefing = api("GET", "/ctem-metrics/briefing", token)
    status = briefing.get("status") if isinstance(briefing, dict) else briefing
    if version is not None or status != "ready":
        raise RuntimeError(f"the profile answers version {version!r} and the briefing status {status!r}")
    return {"id": "briefing ready, no profile", "url": f"{base}/"}


def seed_ctem_kev_tailored(token: str, base: str) -> dict[str, str]:
    """``LAB_CTEM_KEV_TAILORED=on``: the Known-exploited figure of ``/`` reads the tailored count. May raise.

    The saved profile carries ``LAB_ORG_TECH_STACK`` (``seed_org_ai_draft_stub``) and ``lab_asgi`` narrows the
    cyber KEV count to it, so ``[data-testid=ctem-stats-generic]`` is absent. Logged ``seeded CTEM tailored KEV:
    <base>/``. Raises when the posture headline carries no tailored count or no product.
    """
    posture = api("GET", "/ctem-metrics/posture", token)
    headline = (posture.get("headline") if isinstance(posture, dict) else None) or {}
    count, products = headline.get("kev_tailored_count"), headline.get("kev_tailored_products")
    if not isinstance(count, int) or not products:
        raise RuntimeError(f"headline kev_tailored_count is {count!r}, kev_tailored_products {products!r}")
    return {"id": f"{count} tailored KEVs on {', '.join(products)}", "url": f"{base}/"}


def _utc(value: object) -> float | None:
    """*value*, an ISO-8601 time, as a POSIX timestamp; ``None`` when it is not one."""
    from datetime import UTC, datetime

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()


def ctem_activity_gaps(posture: object, registrations: object, runs: object) -> list[str]:
    """What keeps a source of the deterministic activity feed from rendering; empty when none."""
    gaps: list[str] = []
    if not isinstance(posture, dict) or not isinstance(posture.get("stages"), list):
        return [f"the posture answers no stages: {str(posture)[:200]}"]
    if not posture.get("generated_at"):
        gaps.append("the posture answers no generated_at")
    products = posture.get("products") or {}
    for product in ("opencti", "openaev"):
        if not (products.get(product) or {}).get("last_sync"):
            gaps.append(f"{product} has no last_sync")
    stages = {str(s.get("key")): s for s in posture["stages"] if isinstance(s, dict)}
    buckets = ((stages.get("validation") or {}).get("viz") or {}).get("expectation_buckets") or {}
    for result in ("SUCCESS", "FAILED", "PARTIAL"):
        if not sum((b or {}).get(result, 0) for b in buckets.values()):
            gaps.append(f"no {result} expectation in the validation buckets")
    rows = registrations if isinstance(registrations, list) else []
    if not any(r.get("registered_at") for r in rows):
        gaps.append("no registration with registered_at")
    # #9677: the validation row is dated as in deterministicActivity (OpenAEV's last_sync, else generated_at);
    # a "registered" row at or after it pushes it out of the 4-row card of the 1440 grid.
    validated = _utc((products.get("openaev") or {}).get("last_sync")) or _utc(posture.get("generated_at"))
    if validated is not None:
        for r in rows:
            at = _utc(r.get("registered_at"))
            if at is not None and at >= validated:
                name = r.get("platform_identifier") or r.get("id") or "a platform"
                gaps.append(f"{name} registered_at is not older than the validation row")
    if not any(not r.get("healthy") and r.get("last_heartbeat_at") for r in rows):
        gaps.append("no unhealthy registration with a last_heartbeat_at")
    if not isinstance(runs, list) or runs:
        gaps.append(f"{len(runs) if isinstance(runs, list) else runs!r} assignment run(s) listed")
    return gaps


def seed_ctem_activity(token: str, base: str) -> dict[str, str]:
    """``LAB_CTEM_ACTIVITY=on``: every source of the deterministic activity feed, and no run. May raise.

    ``lab_asgi`` answers the cyber collection with the findings (validation buckets with SUCCESS, FAILED and
    PARTIAL) and a ``last_sync`` per connected product; ``seed_ctem_activity.py`` backdates the heartbeat of
    ``LAB_CTEM_ACTIVITY_SILENT`` (no API writes an older one). Every assignment run is then deleted (the
    administrator's ``DELETE /assignments/runs/{id}``), so the Live activity card shows the feed, and the
    posture, the registrations and the runs are read back; a run landing after the delete is caught by the
    next attempt. Runs after every other seed. Lab-only.
    """
    out = ROOT / "ctem_activity.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_ctem_activity.py"), LAB_CTEM_ACTIVITY_SILENT,
         str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(f"seed_ctem_activity.py exited with {process.returncode}: {stderr_summary(process.stderr)}")
    gaps: list[str] = []
    for _attempt in range(4):
        runs = api("GET", "/assignments/runs/all?limit=500", token)
        for run in runs if isinstance(runs, list) else []:
            api("DELETE", f"/assignments/runs/{run['id']}", token)
        gaps = ctem_activity_gaps(
            api("GET", "/ctem-metrics/posture", token), api("GET", "/platform/registrations", token),
            api("GET", "/assignments/runs/all?limit=500", token),
        )
        if not gaps:
            return {"id": "ctem-activity-feed", "url": f"{base}/dashboard"}
        time.sleep(5)
    raise RuntimeError(f"the CTEM activity feed is not reached: {'; '.join(gaps)}")


def seed_provisioning_states(
    token: str, base: str, integrations: dict[str, str],
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """One ``integration_event`` assignment per kind of the Trigger card's provisioning line.

    *integrations* maps ``opencti`` / ``openaev`` to the registered integration
    (``seed_suite_registrations``). Each assignment is created through ``POST /assignments`` on the
    first event of its provider; the API keeps the provisioning state server-owned and never writes
    ``last_triggered_at``, so ``seed_provisioning.py`` writes them through the backend's own model,
    with its interpreter and environment. Logged as ``seeded provisioning <kind>``. Never raises.
    """
    seeded: dict[str, dict[str, str]] = {}
    skipped: dict[str, str] = {}
    try:
        events = api("GET", "/integrations/events", token)
        agent = _seed_agent(token, "Provisioning listener")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import seed_provisioning  # beside this file, standard library only at import

        created: dict[str, str] = {}
        for kind, provider in seed_provisioning.KINDS.items():
            listed = events.get(provider) if isinstance(events, dict) else None
            if provider not in integrations or not listed:
                skipped[f"provisioning {kind}"] = f"no registered {provider} integration or event"
                continue
            created[kind] = _created(api("POST", "/assignments", token, {
                "agent_id": agent, "name": f"{SEED_NAME} provisioning {kind}",
                "description": "Seeded by the lab.", "prompt": "Summarise the event.",
                "trigger_type": "integration_event",
                "trigger_config": {"provider": provider, "event": listed[0]["event"],
                                   "integration_id": integrations[provider]},
            }))
        if not created:
            return seeded, skipped
        out = ROOT / "provisioning.json"
        process = subprocess.run(
            [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_provisioning.py"), json.dumps(created),
             str(out)],
            cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
        )
        if process.returncode != 0:
            raise RuntimeError(
                f"seed_provisioning.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
            )
        json.loads(out.read_text())
        for kind, assignment in created.items():
            seeded[f"provisioning {kind}"] = {"id": assignment, "url": f"{base}/agents/assignments/{assignment}"}
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["provisioning states"] = str(exc)[:600]
    return seeded, skipped


def seed_github_integration(token: str, base: str) -> dict[str, dict[str, str]]:
    """One GitHub integration, connected, so its Webhooks card renders Managed Repository Webhooks.

    The card shows webhook management only on a ``connected`` integration of a provider that
    supports it: an API-key integration created with a key lands connected (``POST /integrations``).
    Its key is a lab-only stub; ``GET /github-webhooks`` answers its empty managed list from the row,
    and ``lab_asgi`` answers its repository elements with one lab repository, so the Add Webhook form
    lists a repository and the event toggles. May raise: the caller logs and skips.
    """
    integration = _created(api("POST", "/integrations", token, {
        "id": LAB_GITHUB_INTEGRATION_ID, "provider": "github", "name": f"{SEED_NAME} GitHub",
        "description": "Seeded by the lab: a lab-only token, nothing reaches GitHub.",
        "auth_type": "api_key", "api_key": LAB_GITHUB_TOKEN,
    }))
    status = api("GET", f"/integrations/{integration}", token)
    if not isinstance(status, dict) or status.get("status") != "connected":
        raise RuntimeError(f"created, but its status is {status.get('status') if isinstance(status, dict) else status!r}")
    api("GET", f"/integrations/{integration}/github-webhooks", token)
    elements = api("GET", f"/integrations/{integration}/elements?element_type=repository", token)
    if not isinstance(elements, dict) or not elements.get("elements"):
        raise RuntimeError(f"its repository elements are empty: {str(elements)[:120]}")
    return {"GitHub integration": {"id": integration, "url": f"{base}/integrations/{integration}/config"}}


def seed_channel_bot(token: str, base: str) -> dict[str, dict[str, str]]:
    """One company-managed Slack channel bot, connected, so Channels & Routing renders its per-channel editor.

    Settings > General Assistant > Channels & Routing (and an agent's Config tab) render the full row of a
    channel (Receive, Publish, Allow private, Only mention, the ChannelPickers) only for a ``connected`` bot:
    ``POST /channel-bots`` with a token lands connected. The token is a lab-only stub; ``lab_asgi`` answers
    the bot's ``GET /channel-bots/<id>/channels`` with three lab channels, nothing reaches Slack. May raise:
    the caller logs and skips.
    """
    bot = _created(api("POST", "/channel-bots", token, {
        "provider": "slack_bot", "name": f"{SEED_NAME} Slack bot", "api_key": LAB_SLACK_BOT_TOKEN,
    }))
    rows = api("GET", "/channel-bots", token)
    row = next((r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict) and r.get("id") == bot), None)
    if not row or row.get("status") != "connected":
        raise RuntimeError(f"created, but its status is {row.get('status') if row else 'unlisted'}")
    channels = api("GET", f"/channel-bots/{bot}/channels", token)
    if not isinstance(channels, dict) or not channels.get("channels"):
        raise RuntimeError(f"its channel list is empty: {str(channels)[:120]}")
    return {"connected channel bot": {"id": bot, "url": f"{base}/settings/ai/assistant"}}


def seed_teams_channel_bot(token: str, base: str) -> dict[str, dict[str, str]]:
    """One company-managed Microsoft Teams channel bot, connected, for the Edit credentials form of Channels.

    Settings > Integrations > Channels renders the edit-mode notes ("Leave fields blank to keep current
    values.") only for a provider whose fields carry ``client_id``; the Slack bot has none. ``POST
    /channel-bots`` with a client id lands connected; the body is :data:`LAB_TEAMS_BOT_BODY` (lab-only values,
    a tenant in ``extra_config``). Read back connected with a client id. May raise: the caller logs and skips.
    """
    bot = _created(api("POST", "/channel-bots", token, {**LAB_TEAMS_BOT_BODY, "name": f"{SEED_NAME} Teams bot"}))
    rows = api("GET", "/channel-bots", token)
    row = next((r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict) and r.get("id") == bot), None)
    if not row or row.get("status") != "connected" or not row.get("has_client_id"):
        raise RuntimeError(f"created, but it reads {({k: row.get(k) for k in ('status', 'has_client_id')} if row else 'unlisted')}")
    return {"connected Teams bot": {"id": bot, "url": f"{base}/settings/integrations/channels"}}


#: The channels of the agent ``seed_channel_assignment`` binds: Slack and Teams, keyed by ``channel_config`` key,
#: each to the seeded bot of that provider (the ``seeded`` entry named here).
LAB_CHANNEL_ASSIGNMENT_BOTS = {"slack": "connected channel bot", "teams": "connected Teams bot"}


def seed_channel_assignment(token: str, base: str, seeded: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """One assignment whose agent receives and publishes on Slack and Teams, for Assignment > Channels & Bots.

    The tab renders its per-channel cards and ChannelPickers only from the parent agent's ``channel_config``;
    every other seeded agent has none. The agent binds each bot of :data:`LAB_CHANNEL_ASSIGNMENT_BOTS` already in
    *seeded* (Slack is required, Teams joins when seeded), receive and publish enabled on the first channel of the
    bot's list (``lab_asgi`` answers it, nothing reaches Slack or Microsoft). The agent is read back with both
    directions on. Logged ``seeded channel assignment: <url>``. May raise: the caller logs and skips.
    """
    if LAB_CHANNEL_ASSIGNMENT_BOTS["slack"] not in seeded:
        raise RuntimeError("no connected Slack channel bot was seeded")
    config: dict[str, dict] = {}
    for channel, entity in LAB_CHANNEL_ASSIGNMENT_BOTS.items():
        if entity not in seeded:
            continue
        bot = seeded[entity]["id"]
        listed = api("GET", f"/channel-bots/{bot}/channels", token)
        rows = listed.get("channels") if isinstance(listed, dict) else None
        if not rows:
            raise RuntimeError(f"the {channel} bot lists no channel: {str(listed)[:120]}")
        first = {"id": rows[0]["id"], "name": rows[0]["name"]}
        config[channel] = {
            "channel_bot_id": bot, "receive_enabled": True, "publish_enabled": True,
            "receive_channels": [first], "publish_channels": [first],
        }
    agent = _created(api("POST", "/agents", token, {
        "name": f"{SEED_NAME} Channel listener", "description": "Seeded by the lab.",
        "persona": "You are the lab's channel listener.", "channel_config": config,
    }))
    stored = api("GET", f"/agents/{agent}", token)
    read = (stored.get("channel_config") if isinstance(stored, dict) else None) or {}
    for channel in config:
        entry = read.get(channel) or {}
        if not (entry.get("receive_enabled") and entry.get("publish_enabled")):
            raise RuntimeError(f"its agent reads {channel} {entry or 'absent'}")
    assignment = _created(api("POST", "/assignments", token, {
        "agent_id": agent, "name": f"{SEED_NAME} Channel digest", "description": "Seeded by the lab.",
        "prompt": "Post the digest of the channel.", "trigger_type": "manual", "trigger_config": {},
    }))
    return {f"channel assignment ({', '.join(sorted(config))} receive and publish)": {
        "id": assignment, "url": f"{base}/agents/assignments/{assignment}",
    }}


#: The handovers of the config showcase agent, one per type: (target name, handover type).
CONFIG_SHOWCASE_HANDOVERS = (
    ("Showcase advisor", "consult"), ("Showcase worker", "delegate"), ("Showcase escalation", "mandatory_transfer"),
)
#: Its coordination access: every company-managed agent reads its rows (narrowed to none listed), it alone writes.
CONFIG_SHOWCASE_ACCESS = {"read": "agents", "write": "self", "read_agent_ids": [], "write_agent_ids": []}


def seed_agent_config_showcase(token: str, base: str) -> dict[str, dict[str, str]]:
    """One company-managed agent whose Configuration tab renders every editable card (#10902).

    "Lab sample config showcase": the ``coordination`` built-in tool with :data:`CONFIG_SHOWCASE_ACCESS`,
    so Built-in Tools > Edit renders the Coordination access panel (``ToolingSection.tsx``: a company-managed
    agent carrying a coordination tool); three handovers of :data:`CONFIG_SHOWCASE_HANDOVERS`, one per type,
    to company-managed playground agents (a company-managed agent hands over to company-managed agents only,
    and a playground agent is in no fleet, so no exposure rule applies); and a company-managed Microsoft
    integration (a lab-only OAuth app, never authorized, nothing reaches Microsoft) bound, so Email
    signatures renders its row. Read back: the three handover types, the stored coordination access and one
    email-signature binding. Logged ``seeded agent config showcase: <base>/agents/<id>/config/tooling``.
    May raise: the caller logs and skips.
    """
    targets = {
        _created(api("POST", "/agents", token, {
            "name": f"{SEED_NAME} {name}", "description": "Seeded by the lab: a handover target of the config showcase.",
            "persona": f"You are the lab's {name.lower()}.", "is_company_managed": True,
        })): kind
        for name, kind in CONFIG_SHOWCASE_HANDOVERS
    }
    mail = _created(api("POST", "/integrations", token, {
        "provider": "microsoft", "name": f"{SEED_NAME} showcase Microsoft", "is_company_managed": True,
        "description": "Seeded by the lab: a lab-only OAuth app, never authorized, nothing reaches Microsoft.",
        "auth_type": "oauth", "client_id": "lab-only-showcase-microsoft-client-id",
        "client_secret": "lab-only-showcase-microsoft-client-secret",
    }))
    agent = _created(api("POST", "/agents", token, {
        "name": f"{SEED_NAME} config showcase", "is_company_managed": True,
        "description": "Seeded by the lab: every editable card of the Configuration tab.",
        "persona": "You are the lab's configuration showcase.",
        "builtin_tool_ids": ["coordination"], "coordination_access": CONFIG_SHOWCASE_ACCESS,
        "handover_agent_ids": list(targets), "handover_types": targets,
        "integration_bindings": {"microsoft": [mail]},
    }))
    row = api("GET", f"/agents/{agent}", token)
    row = row if isinstance(row, dict) else {}
    kinds = sorted((row.get("handover_types") or {}).values())
    if kinds != sorted(kind for _, kind in CONFIG_SHOWCASE_HANDOVERS):
        raise RuntimeError(f"created, but its handover types read {kinds}")
    access = row.get("coordination_access") or {}
    if (access.get("read"), access.get("write")) != ("agents", "self"):
        raise RuntimeError(f"created, but its coordination access reads {access or None}")
    signatures = api("GET", f"/agents/{agent}/email-signatures", token)
    if not isinstance(signatures, dict) or len(signatures.get("bindings") or []) != 1:
        raise RuntimeError(f"created, but its email signatures answer {str(signatures)[:160]}")
    return {"agent config showcase": {"id": agent, "url": f"{base}/agents/{agent}/config/tooling"}}


#: The event mechanisms the Configuration tab renders as siblings: one polling-only provider (Gmail,
#: the ``google`` provider's ``new_email``) and one hybrid provider (DocuSign, Connect webhook or poll).
#: Each row carries lab-only stub credentials: the Google row stays ``configured`` (never authorized,
#: so the poller holds no token), the DocuSign row is a JWT Grant row whose "RSA key" is not a key, so
#: minting a token fails locally and nothing reaches DocuSign.
LAB_EVENT_INTEGRATIONS: dict[str, dict] = {
    "Gmail": {
        "event": "new_email",
        "body": {
            "provider": "google", "auth_type": "oauth",
            "description": "Seeded by the lab: polling-only events, lab-only OAuth app, never authorized.",
            "client_id": "lab-only-google-client-id", "client_secret": "lab-only-google-client-secret",
        },
    },
    "DocuSign": {
        "event": "envelope_completed",
        "body": {
            "provider": "docusign", "auth_type": "api_key",
            "description": "Seeded by the lab: hybrid events, lab-only JWT Grant stub, nothing reaches DocuSign.",
            "api_key": "lab-only-docusign-rsa-key-not-a-key",
            "extra_config": {
                "environment": "demo", "integration_key": "lab-only-docusign-integration-key",
                "user_id": "00000000-0000-4000-8000-00000000d0c5",
                "connect_hmac_key": "lab-only-docusign-connect-hmac-key",
            },
        },
    },
}


def seed_event_integrations(token: str, base: str) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """One Gmail (polling) and one DocuSign (hybrid) integration, each with one ``integration_event`` assignment.

    Their Configuration tabs (``/integrations/<id>/config``) render the Event Triggers card with the
    polling or hybrid badges and the Webhooks card shown or hidden, side by side for a review. Created
    through the real ``POST /integrations`` with the stub credentials of ``LAB_EVENT_INTEGRATIONS``;
    one agent holds both assignments, each bound to its integration, and is bound to both integrations
    (``integration_bindings``, read back): the Trigger card lists only the agent's bound integrations,
    so Edit reaches the integration picker, New email and its Sender filter (#11180). Logged
    ``seeded email event trigger: <base>/assignments/<id>`` for the Gmail one. Never raises.
    """
    seeded: dict[str, dict[str, str]] = {}
    skipped: dict[str, str] = {}
    created: dict[str, str] = {}
    for title, spec in LAB_EVENT_INTEGRATIONS.items():
        try:
            created[title] = _created(api("POST", "/integrations", token, {
                **spec["body"], "name": f"{SEED_NAME} {title}",
            }))
            seeded[f"{title} integration"] = {"id": created[title], "url": f"{base}/integrations/{created[title]}/config"}
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped[f"{title} integration"] = str(exc)[:600]
    if not created:
        return seeded, skipped
    try:
        agent = _seed_agent(token, "Event listener")
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["Gmail and DocuSign integration_event assignments"] = str(exc)[:600]
        return seeded, skipped
    bindings: dict[str, list[str]] = {}
    for title, integration in created.items():
        bindings.setdefault(LAB_EVENT_INTEGRATIONS[title]["body"]["provider"], []).append(integration)
    try:
        api("PUT", f"/agents/{agent}", token, {"integration_bindings": bindings})
        stored = api("GET", f"/agents/{agent}", token)
        bound = (stored.get("integration_bindings") or {}) if isinstance(stored, dict) else {}
        if bound != bindings:
            raise RuntimeError(f"bound, but GET /agents/{agent} answers integration_bindings {str(bound)[:200]}")
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["email event trigger"] = str(exc)[:600]
    for title, integration in created.items():
        spec = LAB_EVENT_INTEGRATIONS[title]
        entity = f"{title} integration_event assignment"
        try:
            assignment = _created(api("POST", "/assignments", token, {
                "agent_id": agent, "name": f"{SEED_NAME} {title} event",
                "description": "Seeded by the lab.", "prompt": "Summarise the event.",
                "trigger_type": "integration_event",
                "trigger_config": {
                    "provider": spec["body"]["provider"], "event": spec["event"], "integration_id": integration,
                },
            }))
            seeded[entity] = {"id": assignment, "url": f"{base}/agents/assignments/{assignment}"}
            if spec["event"] == "new_email" and "email event trigger" not in skipped:
                seeded["email event trigger"] = seeded[entity]
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped[entity] = str(exc)[:600]
    return seeded, skipped


#: The platform events of the data flow fleet's dense source: more than the four chips the
#: Data flow view shows before "+N more".
FLOW_STATES_PLATFORM_EVENTS = (
    "work_item.claimed", "work_item.claim_released", "work_item.completed",
    "work_item.note_added", "work_item.released", "work_item.status_changed",
)


def seed_fleet_flow_states(token: str, base: str, github: str) -> dict[str, dict[str, str]]:
    """One fleet whose Data flow holds every greyed and dense state, on real rows (#9899).

    "Lab sample data flow fleet": an orchestrator over two members. The watcher's only start is an
    ``integration_event`` on a Gmail integration never authorized (``not connected``: "Cannot start");
    the triage member has two sources, that Gmail one and a platform event source of
    :data:`FLOW_STATES_PLATFORM_EVENTS` ("1 of 2 sources not connected", "+N more"), and a read-only
    binding on the connected lab GitHub integration *github* ("Reads only", a dotted ``read`` edge);
    the orchestrator holds one disabled schedule. ``GET /fleets/<id>/flow`` is read back and every
    state checked. Logged ``seeded fleet data flow states: <url>``. May raise: the caller logs and skips.
    """
    gmail = LAB_EVENT_INTEGRATIONS["Gmail"]
    integration = _created(api("POST", "/integrations", token, {
        **gmail["body"], "name": f"{SEED_NAME} data flow Gmail",
        "description": "Seeded by the lab: never authorized, so the data flow shows it not connected.",
    }))
    watcher = _seed_agent(token, "flow watcher")
    triage = _seed_agent(token, "flow triage")
    api("PUT", f"/agents/{triage}", token, {
        "integration_bindings": {"github": [github]}, "integration_read_only": {"github": [github]},
    })
    orchestrator = _seed_agent(token, "flow orchestrator", dict.fromkeys((watcher, triage), "delegate"))
    LAB_AGENTS["flow orchestrator"] = orchestrator  # bound to the A2A liaisons (seed_a2a_liaisons, #85)
    fleet = _created(api("POST", "/fleets", token, {
        "name": f"{SEED_NAME} data flow fleet",
        "description": "Seeded by the lab: a source not connected, reads only, a dense source, a disabled trigger.",
    }))
    root = _created(api("POST", f"/fleets/{fleet}/members", token, {
        "agent_id": orchestrator, "role_label": "Orchestrator",
    }))
    api("PUT", f"/fleets/{fleet}", token, {"orchestrator_agent_id": orchestrator})
    for label, member in (("Watcher", watcher), ("Triage", triage)):
        api("POST", f"/fleets/{fleet}/members", token, {
            "agent_id": member, "parent_member_id": root, "role_label": label,
        })
    triggers = (
        (watcher, "new mail", "integration_event", {
            "provider": gmail["body"]["provider"], "event": gmail["event"], "integration_id": integration,
        }, True),
        (triage, "new mail", "integration_event", {
            "provider": gmail["body"]["provider"], "event": gmail["event"], "integration_id": integration,
        }, True),
        (triage, "work item changes", "platform_event", {"event": list(FLOW_STATES_PLATFORM_EVENTS)}, True),
        (orchestrator, "weekly digest", "schedule", {"cron": "0 8 * * 1", "timezone": "UTC"}, False),
    )
    for agent, name, kind, config, enabled in triggers:
        _created(api("POST", "/assignments", token, {
            "agent_id": agent, "name": f"{SEED_NAME} {name}", "description": "Seeded by the lab.",
            "prompt": "Summarise what started you.", "trigger_type": kind, "trigger_config": config,
            "enabled": enabled,
        }))
    flow = api("GET", f"/fleets/{fleet}/flow", token)
    nodes = flow.get("nodes", []) if isinstance(flow, dict) else []
    edges = flow.get("edges", []) if isinstance(flow, dict) else []
    missing = [state for state, present in (
        ("a source not connected", any(n.get("lane") == "source" and n.get("reason") == "not connected" for n in nodes)),
        ("a source of more than 4 events", any(n.get("lane") == "source" and len(n.get("events") or []) > 4 for n in nodes)),
        ("a read-only output", any(e.get("kind") == "read" for e in edges)),
        # A disabled assignment the create marked paused reads back "paused" (fleet_flow.REASON_PAUSED).
        ("a disabled trigger", any(
            e.get("kind") == "trigger" and e.get("reason") in ("disabled", "paused") for e in edges
        )),
        ("named integration nodes", all(n.get("name") for n in nodes if n.get("kind") == "integration")),
    ) if not present]
    if missing:
        raise RuntimeError(f"its flow is missing {', '.join(missing)}")
    # The fleet page's Data flow tab (#9980), a path route since #10203; old ?view= links redirect there.
    return {"fleet data flow states": {"id": fleet, "url": f"{base}/fleets/{fleet}/data-flow"}}


#: The corporate fleet's members: (name, role label). The first is the orchestrator.
CORPORATE_FLEET_AGENTS = (
    ("corporate assistant", "Orchestrator"), ("corporate mail desk", "Mail"),
    ("corporate communications", "Communications"), ("corporate developer", "Developer"),
    ("corporate design review", "Design review"), ("corporate sales operations", "Sales operations"),
    ("corporate security watch", "Security watch"),
)
#: The integration event sources: (title, catalog provider, how it is created, events by member index).
#: Microsoft and Slack are OAuth rows never authorized (no poller holds a token); GitHub and HubSpot
#: are API-key rows with a lab-only key whose events arrive by webhook only, and nothing delivers one.
CORPORATE_EVENT_SOURCES = (
    ("Microsoft", "microsoft", {
        "auth_type": "oauth", "client_id": "lab-only-microsoft-client-id",
        "client_secret": "lab-only-microsoft-client-secret",
    }, (
        (1, "new_email"), (1, "email_flag_changed"), (1, "calendar_event_changed"), (1, "todo_task_changed"),
        (0, "new_email"), (0, "new_teams_chat_message"), (0, "calendar_event_changed"),
        (2, "new_teams_message"), (2, "new_teams_reply"), (2, "new_teams_chat_message"),
        (4, "new_teams_message"), (5, "new_email"),
    )),
    ("Slack", "slack", {
        "auth_type": "oauth", "client_id": "lab-only-slack-client-id", "client_secret": "lab-only-slack-client-secret",
    }, ((2, "new_slack_message"), (0, "new_slack_message"), (5, "new_slack_message"), (6, "new_slack_message"))),
    ("GitHub", "github", {"auth_type": "api_key", "api_key": "lab-only-corporate-github-token"}, (
        (3, "pull_request"), (3, "pull_request_review"), (3, "pull_request_review_thread"), (3, "check_run"),
        (3, "workflow_run"), (3, "issues"), (3, "issue_comment"), (3, "push"), (4, "pull_request"),
        (6, "dependabot_alert"), (6, "code_scanning_alert"), (0, "release"),
    )),
    ("HubSpot", "hubspot", {"auth_type": "api_key", "api_key": "lab-only-corporate-hubspot-key"}, (
        (5, "entity_created"), (5, "entity_updated"), (5, "entity_deleted"), (0, "entity_updated"),
    )),
)
#: Platform event triggers: (member index, events). Each filters on a correlation key no item carries.
CORPORATE_PLATFORM_TRIGGERS = (
    (0, ("work_item.completed",)), (0, ("work_item.needs_human",)), (0, ("run.failed", "run.orphaned")),
    (0, ("run.completed_with_undisposed_frictions",)), (0, ("platform.release.deployed",)),
    (3, ("work_item.status_changed",)), (3, ("run.friction_recorded",)), (3, ("background_task.failed",)),
    (1, ("agent_account.owner_sent",)), (2, ("work_item.note_added",)), (4, ("work_item.claimed",)),
    (6, ("work_item.released", "work_item.release_exhausted")),
)
#: The correlation key the platform event triggers filter on: no work item carries it.
CORPORATE_NEVER_KEY = "lab-corporate-fleet-never"
#: Schedules: (member index, cron). Every one fires on 29 February only (next in 2028).
CORPORATE_SCHEDULES = tuple(
    (member, f"{minute} {hour} 29 2 *")
    for member, minute, hour in (
        (0, 0, 7), (0, 30, 17), (1, 0, 8), (1, 0, 18), (2, 0, 9), (3, 0, 6), (4, 0, 10), (5, 0, 11), (6, 0, 5), (6, 0, 13),
    )
)
#: Work item inboxes: (member index, correlation prefix no item carries).
CORPORATE_INBOXES = tuple(
    (member, f"lab-corporate-{slug}:") for member, slug in (
        (0, "office"), (1, "mail"), (2, "comms"), (3, "dev"), (4, "ux"), (6, "security"),
    )
)
#: Output-only integrations (API-key rows, a lab-only key): (provider, member index, read only).
CORPORATE_OUTPUTS = (
    ("planhat", 5, True), ("metabase", 5, True), ("zendesk", 5, False), ("hibob", 0, True), ("ashby", 0, True),
    ("pagerduty", 6, False), ("servicenow", 6, False), ("splunk", 6, True), ("crowdstrike", 6, True),
    ("sentinelone", 6, True), ("virustotal", 6, True), ("shodan", 6, True), ("greynoise", 6, True),
    ("urlscan", 6, True), ("abuseipdb", 6, True), ("misp", 6, False), ("recorded_future", 6, True),
    ("wiz", 6, True), ("tenable", 6, True), ("gitlab", 3, False), ("youtrack", 3, False), ("mattermost", 2, False),
    ("feedly", 2, True), ("newsapi", 2, True), ("onepassword", 3, True), ("securityscorecard", 6, True),
)


def seed_corporate_flow_fleet(token: str, base: str) -> dict[str, dict[str, str]]:
    """One corporate-sized fleet whose Data flow has the density of a real office fleet (#10009).

    "Lab sample corporate fleet": :data:`CORPORATE_FLEET_AGENTS` (an orchestrator over six members,
    four handovers), 60 enabled assignments across seven source families (the four integrations of
    :data:`CORPORATE_EVENT_SOURCES`, platform events, schedules, work item inboxes) and about thirty
    output integrations (:data:`CORPORATE_OUTPUTS` plus the event integrations, some read-only, and the
    work items node). Nothing can start a run: the OAuth sources are never authorized, the API-key
    sources receive no delivery, the schedules fire on 29 February only, the platform event triggers
    filter on :data:`CORPORATE_NEVER_KEY` and the inboxes on prefixes no item carries.
    ``GET /fleets/<id>/flow`` is read back: at least 7 sources, 55 trigger edges and 25 outputs.
    Logged ``seeded corporate data flow fleet: <url>``. May raise: the caller logs and skips.
    """
    integrations: dict[str, str] = {}
    for title, provider, body, _ in CORPORATE_EVENT_SOURCES:
        integrations[provider] = _created(api("POST", "/integrations", token, {
            **body, "provider": provider, "name": f"{SEED_NAME} corporate {title}",
            "description": "Seeded by the lab: a lab-only credential, nothing reaches the provider.",
        }))
    outputs: list[tuple[str, str, int, bool]] = []
    for provider, member, read_only in CORPORATE_OUTPUTS:
        try:
            outputs.append((provider, _created(api("POST", "/integrations", token, {
                "provider": provider, "auth_type": "api_key", "api_key": f"lab-only-corporate-{provider}-key",
                "name": f"{SEED_NAME} corporate {provider}",
                "description": "Seeded by the lab: a lab-only key, nothing reaches the provider.",
            })), member, read_only))
        except RuntimeError:
            continue  # a provider this build refuses: the read-back counts what was created
    agents = [_seed_agent(token, name) for name, _ in CORPORATE_FLEET_AGENTS]
    bindings: list[tuple[dict[str, list[str]], dict[str, list[str]]]] = [({}, {}) for _ in agents]
    event_bound = (("microsoft", 1, False), ("microsoft", 0, False), ("slack", 2, False), ("github", 3, False),
                   ("github", 4, True), ("hubspot", 5, False))
    for provider, member, read_only in event_bound:
        bound, reads = bindings[member]
        bound.setdefault(provider, []).append(integrations[provider])
        if read_only:
            reads.setdefault(provider, []).append(integrations[provider])
    for provider, integration, member, read_only in outputs:
        bound, reads = bindings[member]
        bound.setdefault(provider, []).append(integration)
        if read_only:
            reads.setdefault(provider, []).append(integration)
    orchestrator, mail, comms, developer, design = agents[:5]
    handovers = {
        orchestrator: {mail: "delegate", comms: "delegate", developer: "delegate"},
        developer: {design: "consult"},
    }
    for index, agent in enumerate(agents):
        bound, reads = bindings[index]
        types = handovers.get(agent, {})
        api("PUT", f"/agents/{agent}", token, {
            "integration_bindings": bound, "integration_read_only": reads,
            "builtin_tool_ids": ["coordination"] if index in (0, 3, 6) else [],
            "handover_agent_ids": list(types), "handover_types": types,
        })
    fleet = _created(api("POST", "/fleets", token, {
        "name": f"{SEED_NAME} corporate fleet",
        "description": "Seeded by the lab: a corporate-sized data flow, nothing in it can start a run.",
    }))
    root = _created(api("POST", f"/fleets/{fleet}/members", token, {
        "agent_id": orchestrator, "role_label": CORPORATE_FLEET_AGENTS[0][1],
    }))
    api("PUT", f"/fleets/{fleet}", token, {"orchestrator_agent_id": orchestrator})
    for (_, label), member in zip(CORPORATE_FLEET_AGENTS[1:], agents[1:], strict=True):
        api("POST", f"/fleets/{fleet}/members", token, {
            "agent_id": member, "parent_member_id": root, "role_label": label,
        })
    triggers: list[tuple[int, str, str, dict]] = []
    for title, provider, _, events in CORPORATE_EVENT_SOURCES:
        triggers.extend(
            (member, f"{title} {event}", "integration_event",
             {"provider": provider, "event": event, "integration_id": integrations[provider]})
            for member, event in events
        )
    triggers.extend(
        (member, f"on {' or '.join(events)}", "platform_event",
         {"event": list(events), "filter": {"correlation_key": CORPORATE_NEVER_KEY}})
        for member, events in CORPORATE_PLATFORM_TRIGGERS
    )
    triggers.extend(
        (member, f"schedule {cron}", "schedule", {"cron": cron, "timezone": "UTC"})
        for member, cron in CORPORATE_SCHEDULES
    )
    triggers.extend(
        (member, f"inbox {prefix}", "work_item", {"correlation_prefix": prefix})
        for member, prefix in CORPORATE_INBOXES
    )
    for member, name, kind, config in triggers:
        _created(api("POST", "/assignments", token, {
            "agent_id": agents[member], "name": f"{SEED_NAME} corporate {name}", "description": "Seeded by the lab.",
            "prompt": "Summarise what started you.", "trigger_type": kind, "trigger_config": config,
            "enabled": True,
        }))
    flow = api("GET", f"/fleets/{fleet}/flow", token)
    nodes = flow.get("nodes", []) if isinstance(flow, dict) else []
    edges = flow.get("edges", []) if isinstance(flow, dict) else []
    counts = {
        "sources": (sum(n.get("lane") == "source" for n in nodes), 7),
        "triggers": (sum(e.get("kind") == "trigger" for e in edges), 55),
        "outputs": (sum(n.get("lane") == "output" for n in nodes), 25),
    }
    short = [f"{name} {got} of {wanted}" for name, (got, wanted) in counts.items() if got < wanted]
    if short:
        raise RuntimeError(f"its flow is short: {', '.join(short)}")
    return {"corporate data flow fleet": {"id": fleet, "url": f"{base}/fleets/{fleet}/data-flow"}}


def seed_awaiting_human_runs(token: str, base: str, corporate: str) -> dict[str, dict[str, str]]:
    """Runs paused for a person on the corporate fleet and on a built-in fleet (#10221).

    Five ``awaiting_human`` runs per fleet (``seed_awaiting_human_runs.py``): two pending approvals on
    two integrations, a long question, a company-managed run with one approval, one already decided
    (``approval_answered``, left out of the pending panels) and one paused 30 hours ago. No API pauses
    a run without a model turn, so the helper writes them through the backend's own model, with its
    interpreter and environment; nothing resumes them while the lab lives. Logged ``seeded
    awaiting-human runs: <url>/approval`` (the first fleet), then one line per other fleet.
    May raise: the caller logs and skips.
    """
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "awaiting_human_runs.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_awaiting_human_runs.py"), admin, corporate or "",
         str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_awaiting_human_runs.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    fleets = json.loads(out.read_text())
    seeded: dict[str, dict[str, str]] = {}
    for index, fleet in enumerate(fleets):
        label = "awaiting-human runs" if index == 0 else f"awaiting-human runs ({fleet['name']})"
        listed = api("GET", f"/assignments/runs/all?status=awaiting_human&fleet={fleet['id']}&limit=50", token)
        got = {row.get("id"): row for row in listed} if isinstance(listed, list) else {}
        missing = [case for case, run in fleet["runs"].items() if run not in got]
        if missing:
            raise RuntimeError(f"{fleet['name']}: runs/all?status=awaiting_human misses {', '.join(missing)}")
        answered = {case for case, run in fleet["runs"].items() if got[run].get("approval_answered")}
        if answered != {"decided"}:
            raise RuntimeError(f"{fleet['name']}: approval_answered on {sorted(answered)}, expected ['decided']")
        seeded[label] = {
            "id": ",".join(fleet["runs"].values()), "url": f"{base}/fleets/{fleet['id']}/approval",
        }
    return seeded


def seed_fleet_documentation(token: str, base: str, fleets: dict[str, str]) -> dict[str, dict[str, str]]:
    """A generated documentation on each of *fleets* (label to id), so their About page draws the showcase (#10179).

    Custom fleets ship without documentation: ``GET /fleets/<id>/documentation`` answers ``structured``
    null and the About page has no hero, KPI row or member cards. Each fleet is regenerated whole
    through ``POST /fleets/<id>/documentation/regenerate`` (the one write path; the lab's stub model
    answers the drafter, ``doc_draft_answer``), then read back with ``structured`` non-null. The
    documentation is stored at seed time: opening the page calls no model. Logged ``seeded corporate
    fleet documentation: <url>`` per fleet (the fleet path opens on About), and ``seeded fleet doc markdown
    steps: <url>`` when "How it works" holds more than five steps, each with a markdown body (#10327:
    the bodies and the "Show all N steps" fold, ``doc_draft_steps``). May raise: the caller logs and skips.
    """
    seeded: dict[str, dict[str, str]] = {}
    for label, fleet in fleets.items():
        api("POST", f"/fleets/{fleet}/documentation/regenerate", token, {})
        doc = api("GET", f"/fleets/{fleet}/documentation", token)
        if not isinstance(doc, dict) or not doc.get("structured"):
            raise RuntimeError(f"{label}: GET /fleets/{fleet}/documentation answered no structured parts")
        url = f"{base}/fleets/{fleet}"
        log(f"seeded corporate fleet documentation: {url}")
        steps = [s for s in doc["structured"].get("steps") or [] if isinstance(s, dict)]
        if len(steps) > 5 and all(str(s.get("detail") or "").strip() for s in steps):
            log(f"seeded fleet doc markdown steps: {url}")
        seeded[f"{label} documentation"] = {"id": fleet, "url": url}
    return seeded


def seed_fleet_doc_findings(token: str, base: str, fleet: str) -> dict[str, dict[str, str]]:
    """Four documentation findings and a version flagged for regeneration on *fleet* (#10335).

    The stub model returns no findings and a seed regenerate never loses a block, so
    ``seed_fleet_doc_findings.py`` writes two more versions through the backend's own writer: an older
    one with a ``part_derived`` note, the current one with a ``block_lost`` note and the findings. Read
    back on ``GET /fleets/<id>/documentation`` (the four findings) and ``.../documentation/history``
    (the current version ``needs_regenerate``). Logged ``seeded fleet doc findings: <url>`` and
    ``seeded fleet doc flagged version: <url>``. May raise: the caller logs and skips.
    """
    if not fleet:
        raise RuntimeError("the corporate fleet was not seeded")
    admin = _created(api("GET", "/auth/me", token))
    out = ROOT / "fleet_doc_findings.json"
    process = subprocess.run(
        [sys.executable, str(CHECKOUT / ".xtm-one" / "lab" / "seed_fleet_doc_findings.py"), admin, fleet, str(out)],
        cwd=CHECKOUT / "backend", env=seed_script_env(), capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"seed_fleet_doc_findings.py exited with {process.returncode}: {stderr_summary(process.stderr)}"
        )
    written = json.loads(out.read_text())
    doc = api("GET", f"/fleets/{fleet}/documentation", token)
    shown = {f.get("id") for f in (doc.get("findings") if isinstance(doc, dict) else None) or []}
    if not written["findings"] or set(written["findings"]) - shown:
        raise RuntimeError(f"GET /fleets/{fleet}/documentation shows {len(shown)} of {len(written['findings'])} findings")
    history = api("GET", f"/fleets/{fleet}/documentation/history", token)
    versions = history.get("versions") if isinstance(history, dict) else None
    current = next((v for v in versions or [] if v.get("current")), {})
    if current.get("version") != written["version"] or not current.get("needs_regenerate"):
        raise RuntimeError(f"version {written['version']} is not the current version flagged needs_regenerate")
    url = f"{base}/fleets/{fleet}"
    return {
        "fleet doc findings": {"id": ",".join(written["findings"]), "url": url},
        "fleet doc flagged version": {"id": str(written["version"]), "url": url},
    }


def seed_entities() -> None:
    """Sign in as the administrator and create one row per entity kind; log and write the URLs."""
    base = f"http://localhost:{WEB_PORT}"
    seeded: dict[str, dict[str, str]] = {}
    skipped: dict[str, str] = {}
    try:
        login = api("POST", "/auth/login", body={
            "email": os.environ.get("ADMIN_EMAIL", ""), "password": os.environ.get("ADMIN_PASSWORD", ""),
        })
        token = login.get("access_token") if isinstance(login, dict) else None
        if not token:
            raise RuntimeError("sign-in answered no access token")
    except Exception as exc:  # noqa: BLE001 - the seed never stops the lab
        log(f"seed skipped: {exc}")
        return
    seeded["signed-in session (administrator)"] = {
        "id": os.environ.get("ADMIN_EMAIL", ""), "url": signed_in_url(base, token),
    }
    log_session("signed-in session (administrator)", seeded["signed-in session (administrator)"])
    # First: every Enterprise Edition row below needs the licence the backend started without.
    if stores_database_licence():
        try:
            seeded["database licence"] = seed_database_licence(token, base)
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["database licence"] = str(exc)[:600]
    theme = seed_theme(token)
    ids: dict[str, str] = {}
    for entity, route, create in SEEDS:
        try:
            ids[entity] = create(token, ids)  # type: ignore[operator]
            seeded[entity] = {"id": ids[entity], "url": base + route.format(id=ids[entity])}
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped[entity] = str(exc)[:200]
    for entity, (route, parent) in STATIC_PAGES.items():
        if parent in ids:
            seeded[entity] = {"id": ids[parent], "url": base + route.format(id=ids[parent])}
        else:
            skipped[entity] = f"no {parent} row"
    if "agent" in ids:
        try:
            seeded.update(seed_frictions(token, ids["agent"], base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["run with frictions"] = str(exc)[:600]
    else:
        skipped["run with frictions"] = "no agent row"
    if "relay group" in ids:
        try:
            seeded.update(seed_relay_requests(token, ids["relay group"], base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["relay group activity"] = str(exc)[:600]
    else:
        skipped["relay group activity"] = "no relay group row"
    if "evaluation" in ids and "agent" in ids:
        try:
            seeded.update(seed_placebo_comparisons(token, ids["evaluation"], ids["agent"], base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["placebo comparisons"] = str(exc)[:600]
    else:
        skipped["placebo comparisons"] = "no evaluation or agent row"
    if "evaluation" in ids and "agent" in ids and "dataset" in ids:
        try:
            seeded.update(seed_evaluation_runs(token, ids["evaluation"], ids["agent"], ids["dataset"], base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["evaluation runs"] = str(exc)[:600]
    else:
        skipped["evaluation runs"] = "no evaluation, agent or dataset row"
    if "agent" in ids:
        try:
            seeded.update(seed_profile_memory(token, ids["agent"], base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["profile memory"] = str(exc)[:600]
    else:
        skipped["profile memory"] = "no agent row"
    try:
        seeded.update(seed_xtm_mcp(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["Platform Tools MCP Server"] = str(exc)[:600]
    try:
        seeded.update(seed_platform_health(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["Platform Health"] = str(exc)[:600]
    try:
        seeded.update(seed_health_findings(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["health findings"] = str(exc)[:600]
    try:
        seeded.update(seed_chat_suggestions(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["chat suggestions"] = str(exc)[:600]
    if "agent" in ids:
        try:
            seeded.update(seed_operations(token, ids["agent"], base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["background tasks and worksets"] = str(exc)[:600]
    else:
        skipped["background tasks and worksets"] = "no agent row"
    try:
        seeded.update(seed_stale_reasons(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["stale reasons"] = str(exc)[:600]
    try:
        seeded.update(seed_open_decision(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["open decision"] = str(exc)[:600]
    if seeds_kb_stale_embeddings():
        try:
            seeded.update(seed_kb_stale_embeddings(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["stale embeddings"] = str(exc)[:600]
    if "agent" in ids:
        try:
            seeded.update(seed_concierge_signals(token, ids["agent"], base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["concierge signals"] = str(exc)[:600]
    else:
        skipped["concierge signals"] = "no agent row"
    if "concierge flow target" in seeded and "concierge assignment target" in seeded:
        try:
            seeded.update(seed_concierge_improvements(
                token, ids["agent"], seeded["concierge flow target"]["id"],
                seeded["concierge assignment target"]["id"], base,
            ))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["concierge improvements"] = str(exc)[:600]
    else:
        skipped["concierge improvements"] = "no concierge targets"
    try:
        seeded.update(seed_roi_records(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["ROI review runs"] = str(exc)[:600]
    try:
        seeded.update(seed_analytics_traces(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["analytics traces"] = str(exc)[:600]
    if "analytics traces" not in skipped:
        try:
            seeded.update(seed_feedback_comments(base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["feedback comments"] = skipped["feedback criteria"] = str(exc)[:600]
    else:
        skipped["feedback comments"] = "no analytics agents: the comments go on Lab sample analytics agent 12"
        skipped["feedback criteria"] = "no analytics agents: the criteria are scored on Lab sample analytics agent 12"
    try:
        seeded.update(seed_ai_adoption_low_activity(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["ai adoption low-activity users"] = str(exc)[:600]
    if lab_switch("LAB_AI_ADOPTION_SCALE", LAB_AI_ADOPTION_SCALES) == "large":
        try:
            seeded.update(seed_ai_adoption_large(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["ai adoption large figures"] = str(exc)[:600]
    if lab_switch("LAB_FIGURES_SCALE", LAB_FIGURES_SCALES) == "large":
        try:
            seeded.update(seed_large_figures(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["large fleet and home figures"] = str(exc)[:600]
        try:
            seeded.update(seed_large_roi(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["large ROI fleets"] = str(exc)[:600]
        if "analytics traces" in skipped:
            skipped["analytics wide member"] = "no analytics users: the groups go on lab-analytics-01"
        else:
            try:
                seeded.update(seed_analytics_wide_member(token, base))
            except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
                skipped["analytics wide member"] = str(exc)[:600]
    try:
        seeded.update(seed_handovers(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["handovers"] = str(exc)[:600]
    try:
        seeded.update(seed_organizations(base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["organizations"] = str(exc)[:600]
    if "organizations" not in skipped:
        try:
            seeded.update(seed_fleet_member_presets(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["fleet member presets"] = str(exc)[:600]
    else:
        skipped["fleet member presets"] = "no organizations: the list scope names two tracked ones"
    try:
        seeded.update(seed_agent_account_activity(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["agent account activity"] = str(exc)[:600]
    try:
        seeded.update(seed_builtin_fleet(base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["built-in fleet"] = str(exc)[:600]
    try:
        seeded.update(seed_large_fleet(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["large fleet"] = str(exc)[:600]
    try:
        seeded.update(seed_doc_generating_fleet(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["fleet documentation generating"] = str(exc)[:600]
    try:
        seeded.update(seed_doc_hold_fleet(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["fleet documentation hold"] = str(exc)[:600]
    try:
        seeded.update(seed_instructions_position_fleet(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["fleet instructions position"] = str(exc)[:600]
    try:
        seeded.update(seed_agent_memory(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["agent memory"] = str(exc)[:600]
    if "fleet" in ids:
        try:
            seeded.update(seed_connected_members(token, base, ids["fleet"]))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["connected members"] = str(exc)[:600]
    else:
        skipped["connected members"] = "no fleet row"
    if "connected members" not in skipped and "fleet" in ids:
        builtin = seeded.get("built-in fleet", {}).get("id")
        fork_seeded, fork_skipped = seed_fork_source_changes(token, base, ids["fleet"], builtin)
        seeded.update(fork_seeded)
        skipped.update(fork_skipped)
    else:
        skipped["fork with source changes"] = "no fleet row with connected members"
    if "fleet" in ids:
        try:
            seeded.update(seed_archived_fleet(token, base, ids["fleet"]))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["archived fleet"] = str(exc)[:600]
    else:
        skipped["archived fleet"] = "no fleet row"
    try:
        seeded.update(seed_video_findings(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["video render findings"] = str(exc)[:600]
    try:
        seeded.update(seed_video_render_gate(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["video render gate"] = str(exc)[:600]
    try:
        seeded.update(seed_video_render_gate(token, base, one=True))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["video render gate (one finding)"] = str(exc)[:600]
    try:
        seeded.update(seed_video_media(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["video media library"] = str(exc)[:600]
    if lab_switch("LAB_EDITION", LAB_EDITIONS) != "community":
        try:
            seeded.update(seed_video_ready_render(token, base))
        except Exception as exc:  # noqa: BLE001 - a UX round reviews this row: it stops the start (#11713 retest)
            raise SystemExit(f"lab: video ready render not seeded: {str(exc)[:600]}") from None
    else:
        skipped["video ready render"] = "LAB_EDITION=community: the Video Studio is Enterprise Edition"
    if lab_switch("LAB_EDITION", LAB_EDITIONS) != "community":
        try:
            seeded.update(seed_report(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["report"] = str(exc)[:600]
    else:
        skipped["report"] = "LAB_EDITION=community: reports are Enterprise Edition"
    try:
        seeded.update(seed_github_integration(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["GitHub integration"] = str(exc)[:600]
    try:
        seeded.update(seed_channel_bot(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["connected channel bot"] = str(exc)[:600]
    try:
        seeded.update(seed_teams_channel_bot(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["connected Teams bot"] = str(exc)[:600]
    try:
        seeded.update(seed_channel_assignment(token, base, seeded))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["channel assignment"] = str(exc)[:600]
    try:
        seeded.update(seed_agent_config_showcase(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["agent config showcase"] = str(exc)[:600]
    try:
        seeded.update(seed_oauth_missing_scopes(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["OAuth missing scopes"] = str(exc)[:600]
    try:
        seeded.update(seed_mcp_servers(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["MCP servers"] = str(exc)[:600]
    try:
        seeded.update(seed_secret_vault(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["secret vault"] = str(exc)[:600]
    try:
        seeded.update(seed_openapi_spec(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["OpenAPI spec"] = str(exc)[:600]
    catalog_seeded, catalog_skipped = seed_model_catalog_integrations(token, base, ids.get("relay group"))
    seeded.update(catalog_seeded)
    skipped.update(catalog_skipped)
    event_seeded, event_skipped = seed_event_integrations(token, base)
    seeded.update(event_seeded)
    skipped.update(event_skipped)
    if "GitHub integration" in seeded:
        try:
            seeded.update(seed_fleet_flow_states(token, base, seeded["GitHub integration"]["id"]))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["fleet data flow states"] = str(exc)[:600]
    else:
        skipped["fleet data flow states"] = "no GitHub integration: the read-only output is a binding on it"
    if "GitHub integration" in seeded:
        try:
            seeded.update(seed_fleet_gate_review(token, base, seeded["GitHub integration"]["id"]))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["fleet gate review"] = str(exc)[:600]
    else:
        skipped["fleet gate review"] = "no GitHub integration: its write tools are the proposals"
    try:
        seeded.update(seed_corporate_flow_fleet(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["corporate data flow fleet"] = str(exc)[:600]
    try:
        seeded.update(seed_awaiting_human_runs(
            token, base, (seeded.get("corporate data flow fleet") or {}).get("id", ""),
        ))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["awaiting-human runs"] = str(exc)[:600]
    # The corporate fleet's enabled automations are the pause summary's count; the first fleet without it.
    try:
        seeded.update(seed_fleet_chat_cards(
            token, base, (seeded.get("corporate data flow fleet") or {}).get("id", ""),
        ))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["fleet chat cards"] = str(exc)[:600]
    documented = {
        label: fleet
        for label, fleet in (
            ("corporate fleet", (seeded.get("corporate data flow fleet") or {}).get("id")),
            ("sample fleet", ids.get("fleet")),
        )
        if fleet
    }
    try:
        seeded.update(seed_fleet_documentation(token, base, documented))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["fleet documentation"] = str(exc)[:600]
    # After the documentation: the findings and the flagged version are two more versions of it.
    if "corporate fleet documentation" in seeded:
        try:
            seeded.update(seed_fleet_doc_findings(token, base, seeded["corporate fleet documentation"]["id"]))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["fleet doc findings"] = str(exc)[:600]
    else:
        skipped["fleet doc findings"] = "the corporate fleet documentation was not seeded"
    suite_seeded, suite_skipped = seed_suite_registrations(token, base)
    seeded.update(suite_seeded)
    skipped.update(suite_skipped)
    if live_opencti():
        try:
            seeded["live OpenCTI integration"] = seed_live_opencti(token, base)
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["live OpenCTI integration"] = str(exc)[:600]
    try:
        seeded["platform intents"] = seed_platform_intents_check(token, base)
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["platform intents"] = str(exc)[:600]
    suite = {
        provider: suite_seeded[f"registered {title} integration"]["id"]
        for provider, title in SUITE_PLATFORMS.items()
        if f"registered {title} integration" in suite_seeded
    }
    provisioning_seeded, provisioning_skipped = seed_provisioning_states(token, base, suite)
    seeded.update(provisioning_seeded)
    skipped.update(provisioning_skipped)
    try:
        seeded["opencti identities stub"] = seed_opencti_identities_stub(token, base)
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["opencti identities stub"] = str(exc)[:600]
    try:
        seeded["opencti pirs stub"] = seed_opencti_pirs_stub(token, base)
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["opencti pirs stub"] = str(exc)[:600]
    try:
        seeded["org ai-draft stub"] = seed_org_ai_draft_stub(token, base)
        # The stub's sector equals the saved one: the review renders "Unchanged (1)", collapsed.
        seeded["org ai-draft Unchanged (1)"] = seeded["org ai-draft stub"]
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["org ai-draft stub"] = str(exc)[:600]
    if lab_switch("LAB_ORG_SETUP_PROMPT", LAB_ORG_SETUP_PROMPT_STATES) == "on":
        try:
            seeded["org setup prompt"] = seed_org_setup_prompt(token, base)
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["org setup prompt"] = str(exc)[:600]
    if lab_switch("LAB_ORG_SYNCED", LAB_ORG_SYNCED_STATES) == "on":
        # After seed_org_ai_draft_stub: the lab profile is kept, the three sections are replaced.
        try:
            seeded["org synced rows"] = seed_org_synced(token, base)
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["org synced rows"] = str(exc)[:600]
    try:
        seeded["OpenCRQ sync stub"] = seed_opencrq_sync_stub(token, base)
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["OpenCRQ sync stub"] = str(exc)[:600]
    if "organizations" not in skipped:
        try:
            seeded.update(seed_tracked_ai_draft(base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["tracked org ai-draft"] = str(exc)[:600]
    else:
        skipped["tracked org ai-draft"] = "no tracked organizations: the conflict row names one"
    if "fleet" in ids and "organizations" not in skipped and "opencti" in suite:
        try:
            seeded.update(seed_fleet_run_estimate(token, base, ids["fleet"], suite["opencti"]))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["fleet run estimate"] = str(exc)[:600]
    else:
        skipped["fleet run estimate"] = "no fleet, no tracked organizations or no registered OpenCTI integration"
    peer_seeded, peer_skipped = seed_peer(token, base)
    seeded.update(peer_seeded)
    skipped.update(peer_skipped)
    if "peer user" not in peer_seeded:
        skipped["shared resources"] = "no peer user to share the variable, MCP server and custom tool with"
    elif os.environ.get("LAB_SHARED_RESOURCES", "1") == "0":
        skipped["shared resources"] = "LAB_SHARED_RESOURCES=0: the platform's defaults are left as they are"
    else:
        try:
            seeded.update(seed_shared_resources(token, base, peer_seeded["peer user"]["id"]))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["shared resources"] = str(exc)[:600]
    if lab_ai_catalog_env() != "true":
        skipped["public catalog"] = "AI_CATALOG_ENABLED is not true: the /catalog pages and their API are off"
    else:
        try:
            seeded.update(seed_public_catalog(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["public catalog"] = str(exc)[:600]
    try:
        seeded.update(seed_mfa_user(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["two-factor user"] = str(exc)[:600]
    if os.environ.get("PLATFORM_MODE", "") in ("xtm_one", "ai_disabled"):
        # The pre-packaged assistants (CTEM, XTM One) carry the Platform tool and every user sees them.
        skipped["fleet AI blocked user"] = "the built-in assistants carry the fleet tools in XTM One mode: use [service.lab-dev]"
    else:
        try:
            seeded.update(seed_fleet_ai_blocked_user(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["fleet AI blocked user"] = str(exc)[:600]
    home_seeded, home_skipped = seed_home_states(token, base)
    seeded.update(home_seeded)
    skipped.update(home_skipped)
    peer_id = peer_seeded.get("peer user", {}).get("id")
    try:
        seeded.update(seed_chat_sidebar(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["chat sidebar conversations"] = str(exc)[:600]
    if lab_switch("LAB_CHAT_LAYOUT", LAB_CHAT_LAYOUT_STATES) == "on":
        try:
            seeded.update(seed_chat_layout(token, base, peer_id))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["chat layout"] = str(exc)[:600]
    if lab_switch("LAB_PERF_THREAD", LAB_PERF_THREAD_STATES) == "on":
        try:
            seeded.update(seed_perf_thread(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["perf thread"] = str(exc)[:600]
    if "analytics traces" not in skipped:
        try:
            seeded.update(seed_trace_sessions(base, peer_id))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["trace sessions"] = str(exc)[:600]
    else:
        skipped["trace sessions"] = "no analytics agents: the sessions run on the analytics agents"
    if "peer user" in peer_seeded:
        try:
            seeded.update(seed_locked_fleet(token, base, peer_seeded["peer user"]["id"]))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["locked fleet"] = str(exc)[:600]
    else:
        skipped["locked fleet"] = "no peer user: the locked state renders for a viewer the agents are not shared with"
    try:
        seeded.update(seed_no_reason_blocked(token, base, seeded.get("locked fleet", {}).get("id")))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["no-reason blocked work items"] = str(exc)[:600]
    try:
        seeded.update(seed_scheduled_one_offs(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["scheduled one-off"] = str(exc)[:600]
    if peer_id:
        try:
            seeded.update(seed_peer_not_set_up_fleet(token, base, peer_id))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["peer read-only not set up fleet"] = str(exc)[:600]
    else:
        skipped["peer read-only not set up fleet"] = "no peer user: the reader state renders for a non-manager"
    if peer_id:
        try:
            seeded.update(seed_prerequisites_unreadable_fleet(token, base, peer_id))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["fleet prerequisites unreadable for the peer"] = str(exc)[:600]
    else:
        skipped["fleet prerequisites unreadable for the peer"] = "no peer user: the state renders for a reader"
    if peer_id:
        try:
            seeded.update(seed_fleet_manual_prerequisites(token, base, peer_id))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["fleet manual prerequisites"] = str(exc)[:600]
    else:
        skipped["fleet manual prerequisites"] = "no peer user: the reader state renders for a non-manager"
    try:
        seeded.update(seed_kb_sync_sources(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["KB sync sources"] = str(exc)[:600]
    try:
        seeded.update(seed_kb_activity(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["KB activity"] = str(exc)[:600]
    try:
        seeded.update(seed_org_syncs(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["organization syncs"] = str(exc)[:600]
    try:
        seeded.update(seed_custom_integration_provider(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["custom integration provider"] = str(exc)[:600]
    try:
        seeded.update(seed_a2a_partner(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["A2A partner integration"] = str(exc)[:600]
    try:
        seeded.update(seed_a2a_liaisons(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["A2A liaisons"] = str(exc)[:600]
    # Before the AI switches (Enable fleet is refused while AI is off).
    if peer_id and "opencti" in suite:
        try:
            seeded.update(seed_peer_managed_fleet_enable(base, suite["opencti"]))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["peer-managed fleet to enable"] = str(exc)[:600]
    else:
        skipped["peer-managed fleet to enable"] = "no peer user or no registered OpenCTI integration"
    if peer_id and "fleet" in ids and "connected members" not in skipped:
        try:
            seeded.update(seed_reader_fleet(token, base, ids["fleet"], peer_id))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["fleet read-only for the peer"] = str(exc)[:600]
    else:
        skipped["fleet read-only for the peer"] = "no peer user or no fleet with connected members"
    if peer_id:
        try:
            seeded.update(seed_group_manager(token, base, peer_id))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["group manager (peer)"] = str(exc)[:600]
    else:
        skipped["group manager (peer)"] = "no peer user"
    if "fleet" in ids:
        try:
            seeded.update(seed_roi_fleet(token, base, ids["fleet"]))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["ROI fleet"] = str(exc)[:600]
    else:
        skipped["ROI fleet"] = "no fleet row"
    if peer_id:
        try:
            seeded.update(seed_roi_under_k(token, base, peer_id))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["ROI under k own share"] = str(exc)[:600]
    else:
        skipped["ROI under k own share"] = "no peer user"
    try:
        seeded.update(seed_roi_chat_only(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["ROI chat-only"] = str(exc)[:600]
    try:
        seeded.update(seed_background_tasks(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["background tasks"] = str(exc)[:600]
    try:
        seeded.update(seed_builtin_tools_notice(token, base))
    except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
        skipped["built-in tools notice"] = str(exc)[:600]
    if peer_id and "ROI review agent" in seeded:
        try:
            seeded.update(seed_roi_chat_records(token, base, seeded["ROI review agent"]["id"], peer_id))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["ROI chat records"] = str(exc)[:600]
    else:
        skipped["ROI chat records"] = "no peer user or no ROI review agent"
    mode = os.environ.get("PLATFORM_MODE", "")
    if mode == "copilot":
        try:
            seeded.update(seed_presentation(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["presentation"] = str(exc)[:600]
    else:
        skipped["presentation"] = "Copilot mode only (the Filigran Brand Deck): use [service.lab-copilot]"
    if mode == "copilot":
        try:
            seeded.update(seed_dashboard_studio(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["dashboard studio"] = str(exc)[:600]
    else:
        skipped["dashboard studio"] = "Copilot mode only: use [service.lab-copilot]"
    if mode == "xtm_one":
        try:
            seeded.update(seed_fleet_runs(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["fleet runs by trigger"] = str(exc)[:600]
        try:
            event_seeded, event_skipped = seed_ctem_fleet_event_runs(token, base)
            seeded.update(event_seeded)
            skipped.update(event_skipped)
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["CTEM fleet event run"] = str(exc)[:600]
    else:
        skipped["fleet runs by trigger"] = "XTM One mode only (the CTEM Fleet timeline): use [service.lab]"
        skipped["CTEM fleet event run"] = "XTM One mode only (the built-in CTEM fleets): use [service.lab]"
    if mode == "xtm_one" and lab_switch("LAB_CTEM_FLEET_OPTIN", LAB_CTEM_FLEET_OPTIN_STATES) == "mixed":
        if lab_switch("LAB_EDITION", LAB_EDITIONS) == "community":
            # The fleet API is Enterprise Edition: GET /fleets answers 403 in a community lab (#9716).
            skipped["CTEM fleet opt-in states"] = "LAB_EDITION=community: fleets are Enterprise Edition"
        elif "organizations" in skipped or "opencti" not in suite:
            skipped["CTEM fleet opt-in states"] = "no tracked organizations or no registered OpenCTI integration"
        else:
            try:
                seeded.update(seed_ctem_fleet_optin(token, base))
            except Exception as exc:  # noqa: BLE001 - every opt-in state is required (#9689)
                raise SystemExit(f"lab: CTEM fleet opt-in states not seeded: {str(exc)[:600]}") from None
            try:
                seeded.update(seed_ctem_fleet_roi(token, base))
            except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
                skipped["CTEM fleet ROI records"] = str(exc)[:600]
    elif mode == "xtm_one":
        try:
            seeded.update(seed_ctem_fleets_all_off(token, base))
        except Exception as exc:  # noqa: BLE001 - the all-off state is what this lab is for (#9703)
            raise SystemExit(f"lab: CTEM fleets all off not seeded: {str(exc)[:600]}") from None
    if mode == "xtm_one" and os.environ.get("LAB_CTEM_SAMPLE") == "1":
        try:
            seeded.update(seed_ctem_sample(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["CTEM coverage and history"] = str(exc)[:600]
    if mode == "xtm_one" and lab_switch("LAB_CTEM_SAMPLE_STATE", LAB_CTEM_SAMPLE_STATES) == "on":
        try:
            seeded["CTEM sample state"] = seed_ctem_sample_state(token, base)
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["CTEM sample state"] = str(exc)[:600]
    if mode == "xtm_one" and lab_switch("LAB_CTEM_OPENCTI", LAB_CTEM_OPENCTI_STATES) == "on":
        # After seed_live_opencti (the registered row) and seed_org_ai_draft_stub (the tech stack).
        try:
            seeded.update(seed_ctem_opencti(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["CTEM OpenCTI vulnerabilities"] = str(exc)[:600]
    if mode == "xtm_one" and lab_switch("LAB_CTEM_SCREENS", LAB_CTEM_SCREENS_STATES) == "on":
        try:
            seeded.update(seed_ctem_screens(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["CTEM cases and scored stages"] = str(exc)[:600]
        try:
            # After seed_ctem_screens (the cases): the case page's gate records (#1628).
            seeded.update(seed_ctem_gates(token, base))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["CTEM case gates"] = str(exc)[:600]
    if mode == "xtm_one" and lab_switch("LAB_ORG_CONTEXT", LAB_ORG_CONTEXT_STATES) != "default":
        # After seed_org_ai_draft_stub (the lab profile, or none under missing).
        try:
            state = lab_switch("LAB_ORG_CONTEXT", LAB_ORG_CONTEXT_STATES)
            seeded[f"org context {state}"] = seed_org_context(token, base)
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["org context"] = str(exc)[:600]
    if seeds_ctem_posture_history():
        try:
            seeded["CTEM posture history"] = seed_ctem_posture_history(token, base)
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["CTEM posture history"] = str(exc)[:600]
    elif mode in ("xtm_one", "ai_disabled"):
        skipped["CTEM posture history"] = "the CTEM sample labs record their own history"
    if seeds_ctem_large_facts():
        # Answered by lab_asgi (large_ctem_facts) on every read: nothing is written, the URL is logged.
        seeded["CTEM large facts"] = {"id": "ctem_sample.LARGE_FACTS", "url": f"{base}/ctem/discovery"}
    if mode == "xtm_one":
        try:
            seeded["dashboard figures"] = seed_dashboard_figures(token, base)
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["dashboard figures"] = str(exc)[:600]
    if mode in ("xtm_one", "ai_disabled"):
        for stage in ("scoping", "discovery", "prioritization", "validation", "remediation"):
            seeded[f"CTEM stage {stage}"] = {"id": stage, "url": f"{base}/ctem/{stage}"}
    else:
        skipped["CTEM stage"] = "XTM One mode only (/ctem/:stage redirects elsewhere): use [service.lab]"
    langfuse_seeded, langfuse_skipped = seed_langfuse_states(token, base)
    seeded.update(langfuse_seeded)
    skipped.update(langfuse_skipped)
    # After every seed that creates a user (each stays granted OpenCRQ), before the AI switches
    # (Enable fleet is refused while AI is off).
    if "opencrq" in suite:
        try:
            seeded.update(seed_fleet_enable_missing_module(token, base, suite["opencrq"]))
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["fleet enable missing module"] = str(exc)[:600]
    else:
        skipped["fleet enable missing module"] = "no registered OpenCRQ integration to narrow"
    # Last: an AI switch turned off earlier would refuse the agent, fleet and run rows above.
    lock_seeded, lock_skipped = seed_ai_lock_states(token, base, peer_id)
    seeded.update(lock_seeded)
    skipped.update(lock_skipped)
    # After the AI switch: the findings are read back with the briefing locked.
    if mode == "xtm_one" and lab_switch("LAB_CTEM_FINDINGS", LAB_CTEM_FINDINGS_STATES) == "on":
        try:
            seeded["CTEM findings"] = seed_ctem_findings(token, base)
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["CTEM findings"] = str(exc)[:600]
    # After the AI switches (this lab keeps AI on): the stored artifacts are read back as ready.
    if mode == "xtm_one" and lab_switch("LAB_CTEM_ACTIONS", LAB_CTEM_ACTIONS_STATES) == "on":
        try:
            seeded["CTEM stage assessments"] = seed_ctem_actions(token, base)
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["CTEM stage assessments"] = str(exc)[:600]
        # With the setup prompt on, no profile is saved: the ready briefing renders "This briefing is generic".
        if lab_switch("LAB_ORG_SETUP_PROMPT", LAB_ORG_SETUP_PROMPT_STATES) == "on":
            try:
                seeded["CTEM generic briefing"] = seed_ctem_generic_briefing(token, base)
            except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
                skipped["CTEM generic briefing"] = str(exc)[:600]
        if lab_switch("LAB_CTEM_KEV_TAILORED", LAB_CTEM_KEV_TAILORED_STATES) == "on":
            try:
                seeded["CTEM tailored KEV"] = seed_ctem_kev_tailored(token, base)
            except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
                skipped["CTEM tailored KEV"] = str(exc)[:600]
    if lab_switch("LAB_STALE_BUILD", LAB_STALE_BUILD_STATES) == "on":
        try:
            seeded["stale build notice"] = seed_stale_build_notice(token, base)
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["stale build notice"] = str(exc)[:600]
    # Last: every run any seed above created is deleted, so the Live activity card shows the feed.
    if mode == "xtm_one" and lab_switch("LAB_CTEM_ACTIVITY", LAB_CTEM_ACTIVITY_STATES) == "on":
        try:
            seeded["CTEM activity feed"] = seed_ctem_activity(token, base)
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["CTEM activity feed"] = str(exc)[:600]
    (ROOT / "seeded.json").write_text(
        json.dumps({"mode": mode, "theme": theme, "seeded": seeded, "skipped": skipped}, indent=2)
    )
    log_seeded(seeded, skipped)


def log_seeded(seeded: dict[str, dict[str, str]], skipped: dict[str, str]) -> None:
    """One ``seeded <entity>: <url>`` line per seeded row, then one ``not seeded <entity>: <why>`` per skipped one."""
    for entity, row in seeded.items():
        log(f"seeded {entity}: {row['url']}")
    for entity, why in skipped.items():
        log(f"not seeded {entity}: {why}")


#: Written once the seed of the database under ``ROOT / "pgdata"`` ended: the fingerprint of what seeded it
#: (``seed_fingerprint``). A restart in place that finds it equal keeps that database and its seed (#12253).
SEED_MARKER = "seed.sha256"
#: The environment that changes what the seed writes: a lab started with other switches seeds again.
SEED_ENV_PREFIXES = ("LAB_", "ADMIN_", "PLATFORM_MODE", "ENABLED_FEATURES")


def seed_fingerprint(lab_dir: Path | None = None, env: dict[str, str] | None = None) -> str:
    """The sha256 of every script under ``.xtm-one/lab`` and of the seed's environment switches.

    A restart in place after a sync that changed ``lab.py`` or a ``seed_*.py`` changes it, so that restart seeds a
    fresh database again; a restart for a backend or frontend change keeps it (the backend migrates the kept one).
    """
    folder = lab_dir or CHECKOUT / ".xtm-one" / "lab"
    environ = os.environ if env is None else env
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*.py")):
        digest.update(path.relative_to(folder).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    for key in sorted(k for k in environ if k.startswith(SEED_ENV_PREFIXES)):
        digest.update(f"{key}={environ[key]}\0".encode())
    return digest.hexdigest()


def kept_seed(root: Path, fingerprint: str) -> bool:
    """Whether *root* holds a database whose whole seed ended with *fingerprint* (#12253).

    True: the restart in place keeps ``pgdata`` and ``seeded.json`` and skips the seed. False (a first start, a seed
    cut half-way, a changed lab script or switch): the marker and ``pgdata`` are dropped, so the seed writes into a
    fresh database and never adds a second copy of a row (#12236).
    """
    marker, data = root / SEED_MARKER, root / "pgdata"
    try:
        stored = marker.read_text(encoding="utf-8").strip()
    except OSError:
        stored = ""
    if stored == fingerprint and (data / "PG_VERSION").is_file() and (root / "seeded.json").is_file():
        return True
    marker.unlink(missing_ok=True)
    shutil.rmtree(data, ignore_errors=True)
    return False


def seed_lab(kept: bool, fingerprint: str) -> None:
    """The whole seed on a fresh database, then its marker; on a kept one only ``restore_seed`` (#12253)."""
    if kept:
        log("seed kept from the previous run (same database, same lab scripts): restoring its in-memory rows only")
        restore_seed()
        return
    seed_entities()
    (ROOT / SEED_MARKER).write_text(fingerprint, encoding="utf-8")


#: The sign-ins whose seeded session URL a restart renews: ``seeded.json`` row to (email, password).
def _session_logins() -> dict[str, tuple[str, str]]:
    return {
        "signed-in session (administrator)": (os.environ.get("ADMIN_EMAIL", ""), os.environ.get("ADMIN_PASSWORD", "")),
        "signed-in session (peer)": (PEER_EMAIL, PEER_PASSWORD),
    }


def restore_seed() -> None:
    """A restart in place on the database the last run seeded: what that database does not hold, then its lines.

    The rows live in PostgreSQL, kept; Dragonfly keeps nothing across a run (no snapshot), so the seed's Redis rows
    are written again: the generating marker of the fleet whose documentation stays generating and the administrator's
    profile memory. The session URLs are signed in again, then every ``seeded`` and ``not seeded`` line of
    ``seeded.json`` is logged as the first run logged it, ``seeded signed-in session`` included.
    """
    base = f"http://localhost:{WEB_PORT}"
    record = json.loads((ROOT / "seeded.json").read_text())
    seeded: dict[str, dict[str, str]] = record.get("seeded") or {}
    skipped: dict[str, str] = record.get("skipped") or {}
    token = None
    for entity, (email, password) in _session_logins().items():
        if entity not in seeded:
            continue
        try:
            login = api("POST", "/auth/login", body={"email": email, "password": password})
            fresh = login.get("access_token") if isinstance(login, dict) else None
        except Exception as exc:  # noqa: BLE001 - the restore never stops the lab
            log(f"seed restore: {entity} not signed in again: {exc}")
            continue
        if fresh:
            seeded[entity]["url"] = signed_in_url(base, fresh)
            token = token or (fresh if email == os.environ.get("ADMIN_EMAIL", "") else None)
    generating = (seeded.get("fleet documentation generating") or {}).get("id")
    if generating:
        try:
            redis_command("SET", f"{LAB_DOC_GENERATING_PREFIX}{generating}", "1", "EX", str(LAB_DOC_GENERATING_TTL_SECONDS))
            threading.Thread(target=_hold_doc_generating, args=(generating,), daemon=True).start()
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["fleet documentation generating"] = str(exc)[:600]
            seeded.pop("fleet documentation generating", None)
    agent = (seeded.get("agent") or {}).get("id")
    if "profile memory" in seeded and agent and token:
        try:
            seed_profile_memory(token, agent, base)
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["profile memory"] = str(exc)[:600]
            seeded.pop("profile memory", None)
    (ROOT / "seeded.json").write_text(json.dumps({**record, "seeded": seeded, "skipped": skipped}, indent=2))
    log_seeded(seeded, skipped)


#: The searches ``seed_opencti_identities_stub`` probes: one with rows, one with none.
OPENCTI_IDENTITIES_PROBES = ("acme", "zzz")


def seed_opencti_identities_stub(token: str, base: str) -> dict[str, str]:
    """Check the OpenCTI Organization search ``lab_asgi`` answers; the Settings > Organization page.

    ``acme`` must list rows and ``zzz`` none (``LAB_OPENCTI_ORGANIZATIONS``); ``lab:error`` is left to the real
    caller and its 409. Logged ``seeded opencti identities stub: <url>``. Raises when the flag is off or a probe
    answers otherwise: the caller logs it as not seeded.
    """
    if os.environ.get("LAB_OPENCTI_IDENTITIES", "1") != "1":
        raise RuntimeError("LAB_OPENCTI_IDENTITIES is not 1: the search goes to the real OpenCTI caller")
    rows, none = (api("GET", f"/organizations/opencti-identities?search={term}", token) for term in OPENCTI_IDENTITIES_PROBES)
    if not isinstance(rows, list) or not rows or none != []:
        raise RuntimeError(f"the search answered {len(rows) if isinstance(rows, list) else rows!r} rows for acme, {none!r} for zzz")
    return {"id": f"{len(rows)} rows for acme", "url": f"{base}/settings/organization"}


def seed_opencti_pirs_stub(token: str, base: str) -> dict[str, str]:
    """Check the PIR list ``lab_asgi`` answers; the PIR picker of Settings > Organization > Profile.

    ``GET /organization-context/pirs`` must answer the fixture (``LAB_OPENCTI_PIRS``, nine PIRs) with
    ``unavailable`` null. Nothing is saved: a profile saved with an id outside the fixture renders the
    missing state, one saved with ``lab:pirs-error`` the unreadable one. Logged ``seeded opencti pirs stub:
    <url>``. Raises when the flag is off or the list answers otherwise: the caller logs it as not seeded.
    """
    if os.environ.get("LAB_OPENCTI_PIRS", "1") != "1":
        raise RuntimeError("LAB_OPENCTI_PIRS is not 1: the PIR list goes to the real OpenCTI caller")
    answer = api("GET", "/organization-context/pirs", token)
    pirs = answer.get("pirs") if isinstance(answer, dict) else None
    if not pirs or answer.get("unavailable") is not None:
        raise RuntimeError(f"the PIR list answered {answer!r}")
    return {"id": f"{len(pirs)} PIRs", "url": f"{base}/settings/organization"}


def seed_org_ai_draft_stub(token: str, base: str) -> dict[str, str]:
    """Ready Settings > Organization > Draft > With AI: a saved profile, so the proposed name is a conflict.

    Saves ``LAB_ORG_PROFILE`` when no version is saved (an existing profile is kept); the stub model answers
    the draft (``org_ai_draft_answer``) and ``lab_asgi`` the ``lab:quota`` 429. Logged
    ``seeded org ai-draft stub: <url>``. Raises when the 429 switch is off: the caller logs it as not seeded.
    """
    if os.environ.get("LAB_ORG_AI_DRAFT", "1") != "1":
        raise RuntimeError("LAB_ORG_AI_DRAFT is not 1: lab:quota reaches the real quota gate")
    current = api("GET", "/organization-context", token)
    version = current.get("version") if isinstance(current, dict) else None
    # LAB_ORG_SETUP_PROMPT=on keeps the profile unsaved: the first-login setup proposal must be answered propose true.
    # LAB_ORG_CONTEXT=missing keeps it unsaved too: the CTEM screens render the missing context state (#83).
    if (
        version is None
        and lab_switch("LAB_ORG_SETUP_PROMPT", LAB_ORG_SETUP_PROMPT_STATES) == "off"
        and lab_switch("LAB_ORG_CONTEXT", LAB_ORG_CONTEXT_STATES) != "missing"
    ):
        profile = dict(LAB_ORG_PROFILE)
        # LAB_CTEM_KEV_TAILORED=on: a tech stack, so the command center's KEV figure is the tailored count.
        # LAB_CTEM_OPENCTI=on: the same stack, which the lab's OpenCTI holds as Software (the funnel's Relevant layer).
        if "on" in (
            lab_switch("LAB_CTEM_KEV_TAILORED", LAB_CTEM_KEV_TAILORED_STATES),
            lab_switch("LAB_CTEM_OPENCTI", LAB_CTEM_OPENCTI_STATES),
        ):
            profile["tech_stack"] = [dict(entry) for entry in LAB_ORG_TECH_STACK]
        saved = api("PUT", "/organization-context", token, {"profile": profile, "source": "form"})
        version = saved.get("version") if isinstance(saved, dict) else None
    return {"id": f"profile version {version}", "url": f"{base}/settings/organization"}


def seed_org_context(token: str, base: str) -> dict[str, str]:
    """``LAB_ORG_CONTEXT`` other than ``default``: the CTEM organization context state, read back (#83). May raise.

    ``missing``: no profile version is saved (``seed_org_ai_draft_stub`` skips its PUT) and the setup proposal is
    answered ``dismissed``, since ``DELETE /organization-context`` does not exist (405). ``complete``: saves
    ``LAB_ORG_COMPLETE_PROFILE`` as the next version. ``partial``: the lab profile as saved. Read back:
    ``context.state`` of ``GET /ctem-metrics/posture?vertical=cyber`` equals the switch. Logged ``seeded org
    context <state>: <base>/ctem/scoping``. Raises on another state: the caller logs it as not seeded.
    """
    state = lab_switch("LAB_ORG_CONTEXT", LAB_ORG_CONTEXT_STATES)
    if state == "default":
        raise RuntimeError("LAB_ORG_CONTEXT is default: the lab profile is kept as is")
    if state == "missing":
        api("POST", "/organization-context/setup-prompt", token, {"outcome": "dismissed"})
    elif state == "complete":
        api("PUT", "/organization-context", token, {"profile": dict(LAB_ORG_COMPLETE_PROFILE), "source": "form"})
    posture = api("GET", "/ctem-metrics/posture?vertical=cyber", token)
    context = (posture.get("context") if isinstance(posture, dict) else None) or {}
    if context.get("state") != state:
        raise RuntimeError(f"the posture answers context {str(context)[:300]}, expected the state {state!r}")
    missing = context.get("missing_fields") or []
    return {"id": f"{state}, {len(missing)} field(s) missing", "url": f"{base}/ctem/scoping"}


def stale_build_ids() -> tuple[str, str]:
    """``(page build, server build)`` under ``LAB_STALE_BUILD=on`` (#63); ``("", "")`` otherwise.

    The Vite dev server writes the first in ``<meta name="build-id">`` (``VITE_LAB_PAGE_BUILD_ID``, dev server
    only) and ``lab_asgi`` answers the second as ``build_id`` of ``GET /api/v1/auth/config``
    (``LAB_STALE_BUILD_SERVER_ID``): two known, different builds, so ``isStaleBuild`` holds and the page offers
    a reload. Off, neither is written: the dev page has no build and the notice never shows, as before.
    """
    if lab_switch("LAB_STALE_BUILD", LAB_STALE_BUILD_STATES) != "on":
        return "", ""
    return LAB_STALE_PAGE_BUILD_ID, LAB_STALE_SERVER_BUILD_ID


def seed_stale_build_notice(token: str, base: str) -> dict[str, str]:
    """``LAB_STALE_BUILD=on``: the stale build notice renders on the preview with no override (#63).

    Read back: the preview's ``index.html`` carries the page build in ``<meta name="build-id">`` and
    ``GET /auth/config`` answers the server build, two different ids. Logged ``seeded stale build notice: <base>/``
    (the notice shows on load and again when the tab regains focus). May raise: the caller logs and skips.
    """
    page_build, server_build = stale_build_ids()
    with urllib.request.urlopen(f"http://127.0.0.1:{WEB_PORT}/", timeout=60) as response:
        html = response.read().decode("utf-8", "replace")
    if f'<meta name="build-id" content="{page_build}"' not in html:
        raise RuntimeError(f"the preview's index.html carries no <meta name=\"build-id\" content=\"{page_build}\">")
    config = api("GET", "/auth/config", token)
    answered = config.get("build_id") if isinstance(config, dict) else None
    if answered != server_build or answered == page_build:
        raise RuntimeError(f"GET /auth/config answers build_id {answered!r}, expected {server_build!r}")
    return {"id": f"page {page_build}, server {server_build}", "url": f"{base}/"}


def seed_org_synced(token: str, base: str) -> dict[str, str]:
    """``LAB_ORG_SYNCED=on``: profile rows a platform sync wrote, next to typed rows, on the real API (#1345).

    Saves one disabled sync per section of :data:`LAB_ORG_SYNCED_ROWS` (kept when one exists for that section and
    source), then the next profile version: the saved profile with those sections replaced by the rows, each synced
    row carrying ``source`` and ``sync_id`` of its section's sync. Read back: ``GET /organization-context`` lists,
    per section, every synced row with its ``sync_id``. Logged ``seeded org synced rows: <base>/settings/organization``.
    May raise: the caller logs it as not seeded.
    """
    existing = api("GET", "/organization-context/syncs", token)
    syncs = {
        (r.get("section"), r.get("source")): str(r.get("id"))
        for r in (existing if isinstance(existing, list) else [])
        if isinstance(r, dict) and r.get("id")
    }
    current = api("GET", "/organization-context", token)
    profile = dict((current.get("profile") if isinstance(current, dict) else None) or LAB_ORG_PROFILE)
    expected: dict[str, set[tuple[str, str]]] = {}
    for section, (source, rows) in LAB_ORG_SYNCED_ROWS.items():
        sync = syncs.get((section, source)) or _created(api("POST", "/organization-context/syncs", token, {
            "section": section, "source": source, "frequency": "daily", "hour": 6, "minute": 0,
            "timezone": "UTC", "enabled": False,
        }))
        profile[section] = [
            {**row, "source": source, "sync_id": sync} if row.get("external_id") else dict(row) for row in rows
        ]
        expected[section] = {(str(row["external_id"]), sync) for row in rows if row.get("external_id")}
    saved = api("PUT", "/organization-context", token, {"profile": profile, "source": "form"})
    version = saved.get("version") if isinstance(saved, dict) else None
    read = api("GET", "/organization-context", token)
    stored = (read.get("profile") if isinstance(read, dict) else None) or {}
    for section, wanted in expected.items():
        got = {
            (str(r.get("external_id")), str(r.get("sync_id")))
            for r in stored.get(section) or []
            if isinstance(r, dict) and r.get("external_id")
        }
        if not wanted <= got:
            raise RuntimeError(f"saved, but GET /organization-context answers {section} {sorted(got)[:5]}")
    url = f"{base}/settings/organization"
    log(f"seeded org synced rows: {url}")
    total = sum(len(rows) for rows in expected.values())
    return {"id": f"version {version}, {total} synced rows in {len(expected)} sections", "url": url}


def seed_org_setup_prompt(token: str, base: str) -> dict[str, str]:
    """``LAB_ORG_SETUP_PROMPT=on``: the first-login organization setup dialog (#9173 S3) on the real API.

    No profile version is saved under the switch (``seed_org_ai_draft_stub``), so ``GET
    /organization-context/setup-prompt`` must answer ``propose: true`` to the administrator: the dialog then
    renders on sign-in with no ``window.fetch`` override, and Dismiss, Later and Set up now record their outcome
    once. Logged ``seeded org setup prompt: <base>/``. Raises when the answer is not ``propose: true``.
    """
    answer = api("GET", "/organization-context/setup-prompt", token)
    if not isinstance(answer, dict) or answer.get("propose") is not True:
        raise RuntimeError(f"the setup proposal answered {answer!r}")
    return {"id": "propose true", "url": f"{base}/"}


#: The plan kinds Check against OpenCRQ must answer one row each in a lab (``lab_asgi.stub_opencrq_sync``);
#: ``pushed_to_opencrq`` stays empty since #1591 (nothing is written to OpenCRQ).
OPENCRQ_SYNC_PLAN_KINDS = ("linked", "pulled_from_opencrq", "ambiguous", "not_in_opencrq")


def seed_opencrq_sync_stub(token: str, base: str) -> dict[str, str]:
    """Check the plan ``lab_asgi`` answers for Check against OpenCRQ of Settings > Organization > Profile.

    A dry run of ``POST /organization-context/opencrq-sync`` must answer one row of each plan kind, ``errors`` and
    ``version`` included, so the plan panel renders Apply and Close. Nothing is saved (a dry run writes nothing).
    Logged ``seeded OpenCRQ sync stub: <url>``. Raises when the flag is off or the plan answers otherwise: the
    caller logs it as not seeded.
    """
    if os.environ.get("LAB_OPENCRQ_SYNC", "1") != "1":
        raise RuntimeError("LAB_OPENCRQ_SYNC is not 1: the sync goes to the real OpenCRQ caller (409)")
    answer = api("POST", "/organization-context/opencrq-sync", token, {"apply": False})
    plan = answer.get("plan") if isinstance(answer, dict) else None
    if not isinstance(plan, dict) or any(len(plan.get(kind) or []) != 1 for kind in OPENCRQ_SYNC_PLAN_KINDS):
        raise RuntimeError(f"the OpenCRQ sync answered {str(answer)[:300]}")
    if plan.get("pushed_to_opencrq"):
        raise RuntimeError(f"the OpenCRQ sync answered a push {str(answer)[:300]}")
    if answer.get("applied") is not False or answer.get("errors") != [] or "version" not in answer:
        raise RuntimeError(f"the OpenCRQ sync answered {str(answer)[:300]}")
    return {"id": f"{len(OPENCRQ_SYNC_PLAN_KINDS)} plan rows", "url": f"{base}/settings/organization"}


def seed_home_states(token: str, base: str) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """The XTM One home states the two lab switches open, read back from the API; never raises.

    ``LAB_EDITION=community``: ``/platform/config`` must answer edition ``community`` and the
    administrator's ``ctem_splash_seen`` must be false (a fresh user's default), so the home renders
    ``CtemTrialSplash`` over the registered Suite integrations. ``LAB_DEFAULT_MODEL=none``: the
    default model is cleared through ``PUT /settings/general-assistant`` (an empty value clears it)
    and read back as null, so the home's notices carry "No default model configured".
    """
    seeded: dict[str, dict[str, str]] = {}
    skipped: dict[str, str] = {}
    if lab_switch("LAB_EDITION", LAB_EDITIONS) == "community":
        try:
            edition = api("GET", "/platform/config").get("edition")  # type: ignore[union-attr]
            me = api("GET", "/auth/me", token)
            if edition != "community":
                raise RuntimeError(f"/platform/config answers edition {edition!r}, not 'community'")
            if not isinstance(me, dict) or me.get("ctem_splash_seen"):
                raise RuntimeError("the administrator's ctem_splash_seen is not false")
            seeded["CE splash"] = {"id": "community", "url": f"{base}/"}
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["CE splash"] = str(exc)[:600]
    if lab_switch("LAB_DEFAULT_MODEL", LAB_DEFAULT_MODELS) == "none":
        try:
            api("PUT", "/settings/general-assistant", token, {"agent_default_model": ""})
            config = api("GET", "/settings/general-assistant", token)
            if not isinstance(config, dict) or config.get("agent_default_model"):
                raise RuntimeError("the default model is still set after the clear")
            seeded["model-less state"] = {"id": "no-default-model", "url": f"{base}/"}
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["model-less state"] = str(exc)[:600]
        else:
            try:
                seeded["CTEM no-model lock"] = seed_ctem_no_model_lock(token, base)
            except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
                skipped["CTEM no-model lock"] = str(exc)[:600]
    return seeded, skipped


#: The CTEM stages whose assessment GET answers the AI lock (``/ctem-metrics/stages/{stage}/assessment``).
CTEM_LOCK_STAGES = ("scoping", "discovery", "prioritization", "validation")


def seed_ctem_no_model_lock(token: str, base: str) -> dict[str, str]:
    """``LAB_DEFAULT_MODEL=none``: the CTEM briefing and stage assessments answer ``no_model``. May raise.

    The seeded analysts carry no model of their own, so with the default model cleared
    ``_ai_lock_status`` answers ``no_model``, but only when no run is active: the scheduler's
    CTEM fan-out (``schedule_fanout``) creates one run per user at boot, and with no model they
    stay ``pending``, so the GETs answer ``generating`` and ``AiLockedFooter`` never shows the
    no-model reason. Those active runs are deleted (the administrator's ``DELETE
    /assignments/runs/{id}``), then the lock is read back; a fan-out landing after the delete is
    caught by the next attempt. Lab-only: nothing in the product changes.
    """
    statuses: dict[str, object] = {}
    for _attempt in range(4):
        runs = api("GET", "/assignments/runs/all?limit=500", token)
        for run in runs if isinstance(runs, list) else []:
            context = run.get("trigger_context") or {}
            if run.get("status") in ("pending", "delayed", "running") and context.get("ctem_vertical"):
                api("DELETE", f"/assignments/runs/{run['id']}", token)
        briefing = api("GET", "/ctem-metrics/briefing", token)
        statuses = {"briefing": briefing.get("status") if isinstance(briefing, dict) else briefing}
        for stage in CTEM_LOCK_STAGES:
            answer = api("GET", f"/ctem-metrics/stages/{stage}/assessment", token)
            statuses[stage] = answer.get("status") if isinstance(answer, dict) else answer
        if all(value == "no_model" for value in statuses.values()):
            return {"id": "no_model", "url": f"{base}/"}
        time.sleep(5)
    raise RuntimeError(f"the CTEM lock is not no_model after the clear: {statuses}")


def seed_ai_lock_states(
    token: str, base: str, peer_id: str | None,
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """The two AI lock states the edition and model switches do not open, read back; never raises.

    ``AiLockedFooter`` has four reasons: no licence (``LAB_EDITION=community``), no default model
    (``LAB_DEFAULT_MODEL=none``), the platform's AI off and a group with AI off. ``LAB_AI=off``
    switches the runtime AI toggle off through ``PUT /settings/platform`` (``ai_enabled: false``,
    the administrator's own switch) and reads ``ai_enabled`` false back on ``/platform/config``.
    ``LAB_PEER_GROUP_AI=off`` creates a group, switches its AI off and adds the peer, then signs the
    peer in and reads ``ai_disabled_by_group`` true on its ``/auth/me``: the peer's session shows
    the group footer. Both run after every other row, which an AI switch turned off would refuse.
    """
    seeded: dict[str, dict[str, str]] = {}
    skipped: dict[str, str] = {}
    if lab_switch("LAB_PEER_GROUP_AI", LAB_PEER_GROUP_AI_STATES) == "off":
        try:
            if not peer_id:
                raise RuntimeError("no peer user: the group holds lab-peer")
            group = _created(api("POST", "/groups", token, {"name": LAB_AI_OFF_GROUP}))
            api("PUT", f"/groups/{group}", token, {"ai_enabled": False})
            api("POST", f"/groups/{group}/members", token, {"user_ids": [peer_id]})
            login = api("POST", "/auth/login", body={"email": PEER_EMAIL, "password": PEER_PASSWORD})
            peer_token = login.get("access_token") if isinstance(login, dict) else None
            if not peer_token:
                raise RuntimeError("the peer's sign-in answered no access token")
            me = api("GET", "/auth/me", peer_token)
            if not isinstance(me, dict) or not me.get("ai_disabled_by_group"):
                raise RuntimeError("the peer's /auth/me does not answer ai_disabled_by_group true")
            seeded["group with AI off"] = {"id": group, "url": f"{base}/users-and-groups/groups/{group}"}
            seeded["signed-in session (peer, group AI off)"] = {
                "id": PEER_EMAIL, "url": signed_in_url(base, peer_token),
            }
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["group with AI off"] = str(exc)[:600]
    if lab_switch("LAB_AI", LAB_AI_STATES) == "off":
        try:
            api("PUT", "/settings/platform", token, {"ai_enabled": False})
            config = api("GET", "/platform/config")
            if not isinstance(config, dict) or config.get("ai_enabled") is not False:
                raise RuntimeError("/platform/config does not answer ai_enabled false after the switch")
            seeded["platform AI off"] = {"id": "ai-disabled", "url": f"{base}/"}
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["platform AI off"] = str(exc)[:600]
    return seeded, skipped


def seed_langfuse_states(token: str, base: str) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """The Langfuse states of Settings > Usage and quotas > Observability, read back; never raises.

    ``LAB_LANGFUSE=stored`` (the default) saves the lab's keys, enabled, through ``PUT /settings/langfuse`` and
    reads ``has_public_key`` and ``has_secret_key`` true back: the card's Configured badge and the dialog's
    "Stored: type to replace" placeholders render. ``LAB_LANGFUSE=env`` saves nothing (the API refuses an
    env-managed edit) and reads ``is_from_env`` true and both keys back: the env lock renders. ``off``: nothing.
    No connection test is run: the host is the loopback's discard port and nothing reaches a Langfuse.
    """
    seeded: dict[str, dict[str, str]] = {}
    skipped: dict[str, str] = {}
    state = lab_switch("LAB_LANGFUSE", LAB_LANGFUSE_STATES)
    url = f"{base}/settings/usage/observability"
    if state == "stored":
        try:
            api("PUT", "/settings/langfuse", token, {
                "enabled": True, "host": LAB_LANGFUSE_HOST,
                "public_key": LAB_LANGFUSE_PUBLIC_KEY, "secret_key": LAB_LANGFUSE_SECRET_KEY,
            })
            config = api("GET", "/settings/langfuse", token)
            if not isinstance(config, dict) or not (config.get("has_public_key") and config.get("has_secret_key")):
                raise RuntimeError(f"GET /settings/langfuse does not answer both keys stored: {str(config)[:200]}")
            seeded["Langfuse stored keys"] = {"id": "stored", "url": url}
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["Langfuse stored keys"] = str(exc)[:600]
    elif state == "env":
        try:
            config = api("GET", "/settings/langfuse", token)
            if not isinstance(config, dict) or not config.get("is_from_env"):
                raise RuntimeError(f"GET /settings/langfuse does not answer is_from_env true: {str(config)[:200]}")
            if not (config.get("has_public_key") and config.get("has_secret_key")):
                raise RuntimeError("the boot did not seed the keys from LANGFUSE_*")
            seeded["Langfuse env-managed"] = {"id": "env", "url": url}
        except Exception as exc:  # noqa: BLE001 - a refused row is skipped, not fatal
            skipped["Langfuse env-managed"] = str(exc)[:600]
    return seeded, skipped


YARN_ENV = {"YARN_ENABLE_TELEMETRY": "0", "YARN_ENABLE_PROGRESS_BARS": "0", "YARN_ENABLE_GLOBAL_CACHE": "false"}


#: The lock file's digest once its install succeeded: a restart in place with the same lock skips ``yarn install``.
INSTALLED_LOCK = CACHE / "frontend-installed.lock.sha256"


def lock_digest(lock: Path) -> str:
    return hashlib.sha256(lock.read_bytes()).hexdigest() if lock.is_file() else ""


def install_frontend(node: Path, yarn: Path) -> None:
    frontend = CHECKOUT / "frontend"
    digest = lock_digest(frontend / "yarn.lock")
    if digest and (frontend / "node_modules").is_dir() and INSTALLED_LOCK.is_file() and INSTALLED_LOCK.read_text() == digest:
        log("frontend install kept from the previous run (yarn.lock unchanged)")
        return
    run("yarn", [str(node), str(yarn), "install", "--immutable", "--mode=skip-build"], cwd=frontend,
        env={**YARN_ENV, "YARN_CACHE_FOLDER": str(CACHE / "yarn-cache")})
    if digest:
        INSTALLED_LOCK.write_text(digest)


#: The chatbot package inside a filigran-ui checkout.
CHATBOT_PACKAGE = Path("packages") / "filigran-chatbot"
CHATBOT_NAME = "@filigran/chatbot"


def chatbot_source(value: str) -> tuple[Path, Path] | None:
    """``LAB_CHATBOT`` read: (the checkout it names, the ``@filigran/chatbot`` package in it), or None unset.

    The value is a path relative to this checkout: a filigran-ui checkout (``packages/filigran-chatbot``
    is used) or the package directory itself, built or not. Anything else stops the lab with the reason.
    """
    value = value.strip().strip("/")
    if not value:
        return None
    root = (CHECKOUT / value).resolve()
    if not root.is_relative_to(CHECKOUT.resolve()) or not root.is_dir():
        raise SystemExit(
            f"lab: LAB_CHATBOT={value!r} is not a directory of the checkout; materialize filigran-ui with "
            f"materialize_repository(path=\"<this checkout>/{value}\") first"
        )
    for package in (root / CHATBOT_PACKAGE, root):
        manifest = package / "package.json"
        if manifest.is_file() and json.loads(manifest.read_text(encoding="utf-8")).get("name") == CHATBOT_NAME:
            return root, package
    raise SystemExit(f"lab: LAB_CHATBOT={value!r} holds no {CHATBOT_NAME} package (nor {CHATBOT_PACKAGE}/)")


def chatbot_origin(root: Path, package: Path) -> str:
    """What the log names: the package version, the repository and commit of its checkout record."""
    version = json.loads((package / "package.json").read_text(encoding="utf-8")).get("version", "?")
    where = str(root.relative_to(CHECKOUT.resolve())) if root.is_relative_to(CHECKOUT.resolve()) else str(root)
    origin = f"{CHATBOT_NAME} {version} from {where}"
    record = root / ".xtm-checkout.json"
    if record.is_file():
        data = json.loads(record.read_text(encoding="utf-8"))
        ref = f" ({data['ref']})" if data.get("ref") else ""
        origin += f", {data.get('repository', '?')}@{str(data.get('commit', '?'))[:12]}{ref}"
    return origin


def semver_tuple(version: str) -> tuple[int, int, int, int] | None:
    """``1.5.0`` -> (1, 5, 0, 1); a pre-release (``1.5.0-rc.1``) sorts below its release (last item 0)."""
    match = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version.strip())
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    return int(major), int(minor or 0), int(patch or 0), 0 if pre else 1


def semver_satisfies(version: str, spec: str) -> bool | None:
    """Whether ``version`` is in the npm range ``spec`` (``^``, ``~``, comparators, ``x``, ``||``, ``*``).

    None when either side is a syntax this reader does not know (a tag, a URL, a hyphen range): the caller
    then lets it through rather than stop a lab on a range it cannot judge.
    """
    have = semver_tuple(version)
    if have is None:
        return None
    verdicts = []
    for alternative in spec.split("||"):
        comparators = alternative.split()
        if not comparators or comparators in (["*"], ["x"]):
            return True
        ok = True
        for comparator in comparators:
            match = re.fullmatch(r"(\^|~|>=|<=|>|<|=)?v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(-[0-9A-Za-z.-]+)?", comparator)
            if not match:
                return None
            op, *parts, pre = match.groups()
            wild = [p is None or p in "xX*" for p in parts]
            nums = [0 if w else int(p) for p, w in zip(parts, wild, strict=True)]
            low = (*nums, 0 if pre else 1)
            if op in (None, "=") and any(wild):
                op, low = "~" if not wild[1] else "^", (*nums, 0)
            if op in ("^", "~"):
                if op == "~" and not wild[1]:
                    high = (nums[0], nums[1] + 1, 0, 0)
                elif nums[0] or wild[1] or op == "~":
                    high = (nums[0] + 1, 0, 0, 0)
                elif nums[1] or wild[2]:
                    high = (0, nums[1] + 1, 0, 0)
                else:
                    high = (0, 0, nums[2] + 1, 0)
                ok = ok and low <= have < high
            elif op in (None, "="):
                ok = ok and have == low
            else:
                ok = ok and {">=": have >= low, "<=": have <= low, ">": have > low, "<": have < low}[op]
        verdicts.append(ok)
    return any(verdicts)


def workspace_peer(root: Path, peer: str, spec: str) -> Path | None:
    """The package of ``peer`` the chatbot's own monorepo holds under ``packages/``, when its version is in ``spec``."""
    for manifest_path in sorted((root / "packages").glob("*/package.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("name") == peer and semver_satisfies(manifest.get("version", ""), spec):
            return manifest_path.parent
    return None


def check_chatbot_peers(package: Path, root: Path | None = None) -> list[Path]:
    """Stop the lab when a peer the chatbot names is installed in a version its range does not take.

    The package replaces the pinned one but the peers stay the frontend's: a chatbot built against a newer
    ``@filigran/design-system`` served over an older one rendered /chat without its composer and no error
    (#11054). A peer the frontend does not install, or a range this reader cannot judge, is let through.
    A peer the chatbot's own monorepo holds in that range (a filigran-design-system checkout carries
    ``packages/filigran-design-system``) is returned instead: ``serve_workspace_peer`` puts it over the
    frontend's pin after the build, so a chatbot whose peer range moved ahead of xtm-one's pin is still served.
    """
    manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    modules = CHECKOUT / "frontend" / "node_modules"
    served: list[Path] = []
    for peer, spec in sorted((manifest.get("peerDependencies") or {}).items()):
        installed = modules / peer / "package.json"
        if not installed.is_file():
            continue
        version = json.loads(installed.read_text(encoding="utf-8")).get("version", "")
        if semver_satisfies(version, spec) is False:
            own = workspace_peer(root, peer, spec) if root is not None and root != package else None
            if own is not None:
                served.append(own)
                continue
            raise SystemExit(
                f"chatbot: {CHATBOT_NAME} {manifest.get('version', '?')} needs {peer} {spec}, checkout pins {version}: "
                f"use an xtm-one checkout whose frontend pins a {peer} in that range (a branch that bumps it), "
                f"or a chatbot checkout whose packages/ holds a {peer} in it (materialize its package too)"
            )
    return served


def serve_workspace_peer(package: Path) -> None:
    """Put a peer built in the chatbot's monorepo (its design system) over the one the frontend pins.

    Its ``package.json`` and ``dist/`` replace the installed ones, as ``link_chatbot`` does for the chatbot;
    its own dependencies stay the frontend's (the pinned release's). The log names both versions.
    """
    manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    name, version = manifest.get("name", "?"), manifest.get("version", "?")
    if not (package / "dist").is_dir():
        raise SystemExit(f"lab: {name} {version} in {package} has no dist/ after the chatbot build: build it first")
    target = CHECKOUT / "frontend" / "node_modules" / name
    installed = target / "package.json"
    pinned = json.loads(installed.read_text(encoding="utf-8")).get("version", "?") if installed.is_file() else "none"
    shutil.rmtree(target / "dist", ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(package / "package.json", target / "package.json")
    shutil.copytree(package / "dist", target / "dist")
    where = str(package.relative_to(CHECKOUT.resolve())) if package.is_relative_to(CHECKOUT.resolve()) else str(package)
    log(f"chatbot: serving peer {name} {version} from {where} over the pinned {pinned}")


#: What a filigran-design-system checkout needs for the pnpm build of the chatbot: the workspace and its lock,
#: the design system the chatbot builds against (``workspace:*``, built by the root ``prepare``) and the
#: ``llms*.txt`` that ``prepare`` copies into its dist/.
PNPM_MONOREPO_FILES = (
    "package.json",
    "pnpm-workspace.yaml",
    "pnpm-lock.yaml",
    "llms.txt",
    "llms-full.txt",
    "packages/filigran-design-system/package.json",
)


def fetch_pnpm(node: Path) -> Path:
    """The pnpm CLI of ``PNPM``, fetched and unpacked once into the cache."""
    cli = CACHE / "pnpm" / "package" / "bin" / "pnpm.cjs"
    if not cli.is_file():
        with tarfile.open(fileobj=io.BytesIO(fetch(PNPM)), mode="r:gz") as archive:
            archive.extractall(CACHE / "pnpm", filter="data")
    return cli


def build_chatbot_pnpm(node: Path, root: Path) -> None:
    """Build ``@filigran/chatbot`` in a pnpm monorepo checkout (XTM-Foundation/filigran-design-system, #11175).

    ``pnpm install --frozen-lockfile`` (the root ``prepare`` builds the design system the chatbot's
    ``workspace:*`` dependency resolves to), then ``pnpm --filter @filigran/chatbot run build``. A file the
    build needs that the checkout left out, or a root ``engines.pnpm`` the lab's pnpm does not satisfy,
    stops the lab with the reason before anything is fetched.
    """
    missing = [name for name in PNPM_MONOREPO_FILES if not (root / name).is_file()]
    if missing:
        raise SystemExit(
            f"lab: LAB_CHATBOT is a pnpm monorepo without {', '.join(missing)}; materialize it with sub_paths "
            f"[\"{CHATBOT_PACKAGE.as_posix()}\", \"packages/filigran-design-system\", \"package.json\", "
            "\"pnpm-workspace.yaml\", \"pnpm-lock.yaml\", \"llms.txt\", \"llms-full.txt\"]"
        )
    version = PNPM[0].rsplit("pnpm-", 1)[-1].removesuffix(".tgz")
    wanted = (json.loads((root / "package.json").read_text(encoding="utf-8")).get("engines") or {}).get("pnpm")
    if wanted and semver_satisfies(version, wanted) is False:
        raise SystemExit(
            f"lab: LAB_CHATBOT's root pins pnpm {wanted}, the lab ships pnpm {version}: update PNPM in lab.py"
        )
    pnpm = fetch_pnpm(node)
    env = {"PATH": f"{node.parent}:{os.environ.get('PATH', '')}", "npm_config_store_dir": str(CACHE / "pnpm-store"),
           "npm_config_update_notifier": "false"}
    run("chatbot-install", [str(node), str(pnpm), "install", "--frozen-lockfile"], cwd=root, env=env)
    run("chatbot-build", [str(node), str(pnpm), "--filter", CHATBOT_NAME, "run", "build"], cwd=root, env=env)


def link_chatbot(node: Path, yarn: Path) -> None:
    """Serve the ``@filigran/chatbot`` of ``LAB_CHATBOT`` instead of the version frontend/package.json pins.

    The package is built when it has no ``dist/`` (a filigran-ui checkout: its monorepo installed, then
    ``yarn workspace @filigran/chatbot run build``; a filigran-design-system checkout, which holds
    ``pnpm-workspace.yaml``: ``build_chatbot_pnpm``), then its ``package.json`` and ``dist/`` replace the ones in
    ``frontend/node_modules``: every entry (the panel, ``/markdown``, ``/workspaces``, ``/styles.css``)
    follows its exports, React and the peers stay the frontend's, and no vite.config.ts alias is written.
    """
    source = chatbot_source(os.environ.get("LAB_CHATBOT", ""))
    if source is None:
        return
    root, package = source
    peers = check_chatbot_peers(package, root)
    if not (package / "dist" / "index.js").is_file():
        if package == root:
            raise SystemExit(f"lab: LAB_CHATBOT names a {CHATBOT_NAME} package without dist/; name its monorepo checkout")
    if not (package / "dist" / "index.js").is_file() and (root / "pnpm-workspace.yaml").is_file():
        build_chatbot_pnpm(node, root)
    elif not (package / "dist" / "index.js").is_file():
        # node-modules as filigran-ui's .yarnrc.yml says, also when a narrow checkout left that file out.
        # Never immutable: a throwaway build of a branch head, whose lockfile may lag its manifests or the lab's
        # Yarn (4.18 rewrites a 4.13 lockfile), and Yarn turns immutable installs on by itself when CI is set.
        env = {**YARN_ENV, "YARN_CACHE_FOLDER": str(CACHE / "yarn-cache-chatbot"), "YARN_NODE_LINKER": "node-modules",
               "YARN_ENABLE_IMMUTABLE_INSTALLS": "false"}
        run("chatbot-install", [str(node), str(yarn), "install", "--mode=skip-build"], cwd=root, env=env)
        run("chatbot-build", [str(node), str(yarn), "workspace", CHATBOT_NAME, "run", "build"], cwd=root,
            env={**env, "PATH": f"{node.parent}:{os.environ.get('PATH', '')}"})
    if not (package / "dist" / "index.js").is_file():
        raise SystemExit(f"lab: the {CHATBOT_NAME} build wrote no dist/index.js in {package}")
    target = CHECKOUT / "frontend" / "node_modules" / "@filigran" / "chatbot"
    shutil.rmtree(target / "dist", ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(package / "package.json", target / "package.json")
    shutil.copytree(package / "dist", target / "dist")
    for peer in peers:
        serve_workspace_peer(peer)
    # Vite's pre-bundle of the pinned versions must not be served instead.
    shutil.rmtree(CHECKOUT / "frontend" / "node_modules" / ".vite", ignore_errors=True)
    # The mode decides the accent the panel paints (Copilot blue, XTM One purple): the log names it (#11300).
    mode = os.environ.get("PLATFORM_MODE", "")
    log(f"chatbot: serving {chatbot_origin(root, package)}" + (f", in {mode} mode" if mode else ""))


#: The lab-only page that renders the served panel in the display its URL names (#11319): XTM One mounts
#: the panel inline only, so a chatbot change to the floating window, the docked sidebar or the full screen
#: is reviewed there. Its sources live here and are copied into the lab's ``frontend/`` only.
CHATBOT_HOST_DIR = Path(".xtm-one") / "lab" / "chatbot-host"
CHATBOT_HOST_FILES = (("lab-chatbot.html", Path("lab-chatbot.html")),
                      ("lab-chatbot-host.tsx", Path("src") / "lab-chatbot-host.tsx"),
                      ("lab-chatbot-params.ts", Path("src") / "lab-chatbot-params.ts"))
CHATBOT_HOST_DISPLAYS = ("floating", "sidebar", "fullscreen", "inline")
#: The review states the page reads beside ``mode`` (#11360), as ``LAB_REVIEW_PARAMS`` of lab-chatbot-params.ts.
CHATBOT_HOST_PARAMS = ("historyPending=1", "welcome=placeholder", "conversation=ID", "holdHistory=1",
                       "waiting=package", "waiting=package-nourl", "quota=1",
                       "quotaState=ok|warning|danger|unlimited|global|legacy")


def install_chatbot_host() -> None:
    """Put the display host page into the lab's ``frontend/``, where Vite serves it at ``/lab-chatbot.html``.

    Runs after ``link_chatbot`` when ``LAB_CHATBOT`` is set; the repository's ``frontend/`` never holds it.
    """
    for name, target in CHATBOT_HOST_FILES:
        destination = CHECKOUT / "frontend" / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CHECKOUT / CHATBOT_HOST_DIR / name, destination)


def chatbot_host_urls(base: str, token: str) -> list[str]:
    """One signed-in URL per display: the ``#token=`` fragment the page's first script stores and scrubs."""
    return [f"{base}/lab-chatbot.html?mode={display}#token={token}" for display in CHATBOT_HOST_DISPLAYS]


def log_chatbot_host_urls() -> None:
    """Log ``chatbot display <mode>: <url>`` per display, signed in as the administrator; never stops the lab."""
    try:
        login = api("POST", "/auth/login", body={
            "email": os.environ.get("ADMIN_EMAIL", ""), "password": os.environ.get("ADMIN_PASSWORD", ""),
        })
        token = login.get("access_token") if isinstance(login, dict) else None
        if not token:
            raise RuntimeError("sign-in answered no access token")
    except Exception as exc:  # noqa: BLE001 - the host page never stops the lab
        log(f"chatbot display host skipped: {exc}")
        return
    for display, url in zip(CHATBOT_HOST_DISPLAYS, chatbot_host_urls(f"http://localhost:{WEB_PORT}", token), strict=True):
        log(f"chatbot display {display}: {url}")
    log(f"chatbot display params (add to the query): {' '.join(CHATBOT_HOST_PARAMS)}")


#: ``LAB_OFFICE_ADDIN = "on"`` (#11109): the Office add-in task pane of ``apps/microsoft-office``, built by the
#: lab and served at ``/office/`` of the preview by ``lab_asgi`` (``LAB_OFFICE_ADDIN_DIST``), signed in.
OFFICE_ADDIN_APP = Path("apps") / "microsoft-office"
OFFICE_ADDIN_DIST = ROOT / "office-addin"
#: The Office.js tag the lab's page drops: outside Office the task pane makes no Microsoft call, and
#: ``useOffice`` answers ready with the Word host when ``Office`` is undefined.
OFFICE_JS_TAG = re.compile(r"\s*<script[^>]*appsforoffice\.microsoft\.com[^>]*>\s*</script>")
#: Run before the add-in's module: the ``#token=...&theme=...`` of the logged URL becomes the add-in's
#: session and theme (the keys ``src/lib/api.ts`` and ``src/hooks/useTheme.ts`` read), then leaves the bar.
OFFICE_ADDIN_SESSION = """(function () {
  var params = new URLSearchParams(window.location.hash.slice(1));
  var token = params.get("token");
  var theme = params.get("theme");
  if (token) {
    localStorage.setItem("filigran_copilot_token", token);
    localStorage.setItem("copilot_token", token);
    localStorage.removeItem("filigran_copilot_url");
  }
  if (theme === "light" || theme === "dark") localStorage.setItem("filigran_copilot_theme", theme);
  if (token || theme) history.replaceState(null, "", window.location.pathname + window.location.search);
})();
"""


def office_addin_enabled() -> bool:
    return os.environ.get("LAB_OFFICE_ADDIN", "").strip().lower() in ("on", "1", "true")


def prepare_office_addin_page(dist: Path) -> bool:
    """Drop Office.js from the built task pane and put the lab's session script before its module."""
    page = dist / "index.html"
    if not page.is_file():
        log(f"office add-in skipped: the build wrote no {page}")
        return False
    html = OFFICE_JS_TAG.sub("", page.read_text(encoding="utf-8"))
    (dist / "lab-session.js").write_text(OFFICE_ADDIN_SESSION, encoding="utf-8")
    # First in <head>: before the module script Vite writes there, whatever the build's order.
    html = re.sub(r"<head[^>]*>", lambda m: m.group(0) + '\n    <script src="/office/lab-session.js"></script>', html, count=1)
    page.write_text(html, encoding="utf-8")
    return True


def build_office_addin(node: Path) -> bool:
    """Build the task pane with the frontend's toolchain into ``OFFICE_ADDIN_DIST``; never stops the lab.

    Runs after ``link_chatbot``: the add-in resolves its packages from ``frontend/node_modules`` (a
    ``node_modules`` link in the lab's copy of ``apps/microsoft-office``), so it renders the same
    ``@filigran/chatbot`` as ``/chat``, a ``LAB_CHATBOT`` branch included. ``NODE_ENV=production`` gives
    the ``/office/`` base its ``vite.config.ts`` sets for the hosted build.
    """
    if not office_addin_enabled():
        return False
    app = CHECKOUT / OFFICE_ADDIN_APP
    if not (app / "index.html").is_file():
        log(f"office add-in skipped: the checkout has no {OFFICE_ADDIN_APP}/ (materialize it with the sub_paths)")
        return False
    modules = app / "node_modules"
    if not modules.exists():
        modules.symlink_to(CHECKOUT / "frontend" / "node_modules", target_is_directory=True)
    vite = CHECKOUT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
    try:
        run("office-addin-build", [str(node), str(vite), "build", "--outDir", str(OFFICE_ADDIN_DIST), "--emptyOutDir"],
            cwd=app, env={"NODE_ENV": "production", "PATH": f"{node.parent}:{os.environ.get('PATH', '')}"})
    except SystemExit as exc:
        log(f"office add-in skipped: {exc.code}")
        return False
    return prepare_office_addin_page(OFFICE_ADDIN_DIST)


def office_addin_url(base: str, token: str, theme: str) -> str:
    """The task pane URL that opens signed in, in *theme*: the fragment ``OFFICE_ADDIN_SESSION`` consumes."""
    return f"{base}/office/#token={token}&theme={theme}"


def log_office_addin_url() -> None:
    """Log ``office add-in: <url>``, signed in as the administrator, in ``LAB_THEME`` (dark by default)."""
    try:
        login = api("POST", "/auth/login", body={
            "email": os.environ.get("ADMIN_EMAIL", ""), "password": os.environ.get("ADMIN_PASSWORD", ""),
        })
        token = login.get("access_token") if isinstance(login, dict) else None
        if not token:
            raise RuntimeError("sign-in answered no access token")
    except Exception as exc:  # noqa: BLE001 - the add-in never stops the lab
        log(f"office add-in skipped: {exc}")
        return
    theme = os.environ.get("LAB_THEME", "").strip().lower()
    log(f"office add-in: {office_addin_url(f'http://localhost:{WEB_PORT}', token, theme if theme in LAB_THEMES else 'dark')}")


def main() -> None:
    missing = [part for part in ("backend", "frontend") if not (CHECKOUT / part).is_dir()]
    if missing:
        raise SystemExit(
            f"lab: the checkout has no {' or '.join(missing)}/; materialize the repository with at least "
            "the sub_paths .xtm-one, backend and frontend"
        )
    ROOT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))

    # The frontend toolchain is fetched and installed while the stores and the backend start.
    toolchain: dict[str, Path] = {}
    # Why the frontend side stopped: a SystemExit raised in a thread is otherwise dropped without a word.
    side_failure: list[str] = []

    def frontend_side() -> None:
        try:
            install_side()
        except SystemExit as exc:
            side_failure.append(str(exc.code))
            raise

    def install_side() -> None:
        node = CACHE / "node" / "package" / "bin" / "node"
        yarn = CACHE / "yarn" / "package" / "bin" / "yarn.js"
        # A restart in place (#12236) finds the toolchain its last run unpacked: no second download.
        for spec, target, done in ((NODE, "node", node), (YARN, "yarn", yarn)):
            if done.is_file():
                log(f"{target} kept from the previous run")
                continue
            with tarfile.open(fileobj=io.BytesIO(fetch(spec)), mode="r:gz") as archive:
                archive.extractall(CACHE / target, filter="data")
        node.chmod(0o755)
        install_frontend(node, yarn)
        link_chatbot(node, yarn)
        if os.environ.get("LAB_CHATBOT", "").strip():
            install_chatbot_host()
        if build_office_addin(node):
            toolchain["office_addin"] = OFFICE_ADDIN_DIST
        toolchain["node"] = node

    side = threading.Thread(target=frontend_side, daemon=True)
    side.start()

    start_model()
    # Before the stores: a database whose seed is not kept is dropped here, so PostgreSQL starts on a fresh one.
    fingerprint = seed_fingerprint()
    kept = kept_seed(ROOT, fingerprint)
    start_stores()
    if has_renderer():
        log("renderer: headless Chromium found, presentation export (PDF, PPTX) available")
    else:
        log("renderer: no headless Chromium in this image (older python-dev): presentation export answers 503")

    # Kept in BACKEND: ``lab:restart-mid-turn`` replaces it (``restart_backend``).
    BACKEND["process"] = spawn("backend", backend_argv(), cwd=CHECKOUT / "backend", env=backend_spawn_env(0))
    wait_http(f"http://127.0.0.1:{API_PORT}/api/health/ready", 600, "backend")
    if runs_worker():
        # After the backend's readiness: the backend migrates, the worker waits for the schema head.
        spawn("worker", worker_argv(), cwd=CHECKOUT / "backend", env=backend_env())
    side.join()
    if "node" not in toolchain:
        raise SystemExit(side_failure[0] if side_failure else "lab: the frontend toolchain did not install")
    spawn(
        "frontend",
        [str(toolchain["node"]), "node_modules/vite/bin/vite.js", "--host", "0.0.0.0", "--port", str(WEB_PORT), "--strictPort"],
        cwd=CHECKOUT / "frontend",
        env={
            "VITE_API_URL": f"http://127.0.0.1:{API_PORT}",
            # LAB_CTEM_OPENCTI=on: /opencti of the preview is the lab's own OpenCTI (#12058).
            **({"VITE_OPENCTI_URL": LIVE_OPENCTI_URL, "VITE_OPENCTI_TOKEN": CTEM_OPENCTI_TOKEN}
               if lab_switch("LAB_CTEM_OPENCTI", LAB_CTEM_OPENCTI_STATES) == "on" else {}),
            # LAB_STALE_BUILD=on: the dev server writes this build in <meta name="build-id"> (#63).
            **({"VITE_LAB_PAGE_BUILD_ID": stale_build_ids()[0]} if stale_build_ids()[0] else {}),
            "PATH": f"{toolchain['node'].parent}:{os.environ.get('PATH', '')}",
        },
    )
    wait_http(f"http://127.0.0.1:{WEB_PORT}/", 120, "frontend")
    seed_lab(kept, fingerprint)
    if "office_addin" in toolchain:
        log_office_addin_url()
    if os.environ.get("LAB_CHATBOT", "").strip():
        log_chatbot_host_urls()
    LAB_UP.set()
    log(f"lab up: sign in as {os.environ.get('ADMIN_EMAIL', '')} (password in [service.lab] env)"
        f" or as the non-admin {PEER_EMAIL} ({PEER_PASSWORD})")
    while True:
        for child in CHILDREN:
            if child.poll() is not None:
                raise SystemExit(f"lab: {child.args[0]} exited with {child.returncode}")
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    finally:
        # The children are waited for before pgdata goes (#12198): a restart in place reruns this script in the same
        # container, and a PostgreSQL still writing into a half-removed pgdata would leave a broken cluster behind.
        stores.stop(CHILDREN)
        # pgdata is kept (#12253): the next run keeps it with its seed when ``seed.sha256`` matches (``kept_seed``),
        # and drops it before its stores start otherwise, so a seed never writes a second copy of a row (#12236).
