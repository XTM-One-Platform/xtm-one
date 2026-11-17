"""The OpenCRQ Ask Ariane lab: OpenCRQ's own front, signed in, with the Ask Ariane panel answering.

``[service.lab-opencrq-ask-ariane]`` in ``.xtm-one/checks.toml`` runs this script (standard library only) on
the ``python-dev`` image. It renders the Ask Ariane launcher and panel (``front/src/features/xtm-one``) of an
OpenCRQ checkout the agent chooses, so a ``@filigran/chatbot`` bump is reviewed in the product that embeds it.
Like the OpenAEV lab, no OpenCRQ backend runs (it needs Node 26, PostgreSQL and its edition check):

1. The OpenCRQ source is a nested checkout under this checkout, ``opencrq/`` by default
   (``LAB_OPENCRQ_CHECKOUT``): materialize_repository(OpenCRQ-Platform/opencrq, ref=<branch or main>,
   sub_paths=OPENCRQ_SUB_PATHS, path="<this checkout>/opencrq"). The front is a workspace of the root yarn
   project (it depends on ``@app/module`` and ``@app/utils`` and reads ``back/config``), so the root manifest,
   its lock and the sibling workspaces come with it.
2. Node and yarn are the lab's pinned downloads (``lab.NODE``, ``lab.YARN``: yarn 4.18.1 is the root
   ``packageManager``); ``yarn install`` at the root installs the workspaces, then Vite serves ``front`` on
   ``VITE_PORT``.
3. This script listens on ``PORT``, the preview port. ``/api/xtm-one/chat/*`` (the product's Ask Ariane proxy) is
   answered from the OpenCTI lab's fixtures (agents, three seeded conversations, a long Markdown answer streamed in
   chunks; ``slow`` holds the working state, ``error`` ends on an error frame, ``full`` reports a context over 95%
   of the window), with the context usage on the thinking and done frames, so the composer gauge renders with no
   ``window.fetch`` override. ``/trpc/*`` answers the shell's tRPC calls in tRPC's own wire formats (the JSON
   Lines of ``httpBatchStreamLink``, the JSON array of ``httpBatchLink``, the event stream of
   ``httpSubscriptionLink``) from ``PROCEDURES``: a signed-in lab admin on an initialized Enterprise Edition
   platform connected to XTM One, one tenant. Any other procedure answers an empty value (``[]`` for a list
   name, ``0`` for a count, else ``null``) and is logged once (``opencrq-ask-ariane: unstubbed ...``), so the
   pages render empty around the header. Everything else, websockets included, goes to Vite.
4. Theme: open ``/lab/theme/dark`` or ``/lab/theme/light`` (a cookie ``users.getUserTheme`` reads), then the page
   it redirects to renders in that theme.

Pins: an ``opencrq-pins.json`` file at this checkout's root (``{"@filigran/chatbot": "3.27.2"}``) or
``LAB_OPENCRQ_PINS=@filigran/chatbot@3.27.2``, which wins over the file, rewrites the front's pins before the
install (``opencrq-ask-ariane: front pins overridden: ...``); the ``lab up`` line names the chatbot version served.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab  # noqa: E402 - .xtm-one/lab/lab.py: the pinned toolchain and the process helpers
import openaev_ask_ariane as product  # noqa: E402 - the pinned toolchain unpacked, the pins reader
import opencti_ask_ariane as fixtures  # noqa: E402 - the Ask Ariane fixtures, stream and Vite tunnel

PORT = 3400
VITE_PORT = 3401
#: What the nested OpenCRQ checkout must hold: the root yarn project, the root TypeScript configs the front's
#: tsconfig extends (``../tsconfig.base.json``: without it Vite answers TSCONFIG_ERROR and the page never renders)
#: and the workspaces the front installs with. The start refuses when one of them is missing.
OPENCRQ_SUB_PATHS = (
    "package.json",
    "yarn.lock",
    ".yarnrc.yml",
    "tsconfig.base.json",
    "tsconfig.json",
    "front",
    "back",
    "module",
    "utils",
    "eslint-rules",
    "integration-tests/package.json",
)
CHAT_PREFIX = "/api/xtm-one/chat"
PINS_ENV = "LAB_OPENCRQ_PINS"
PINS_FILE = "opencrq-pins.json"
TENANT_ID = "0193a6e2-7c1d-7a3e-9b4f-5c6d7e8f9a01"
USER_ID = "0193a6e2-7c1d-7a3e-9b4f-5c6d7e8f9a02"
THEME_COOKIE = "lab_theme"
#: The unstubbed procedures already logged, so a polling page logs each one once.
UNSTUBBED: set[str] = set()

LAB_USER = {"id": USER_ID, "email": "lab@filigran.io", "name": "Lab User", "title": None}
TENANT = {"id": TENANT_ID, "name": "Lab"}


def procedure_answer(path: str, theme: str) -> object:
    """The data the tRPC procedure *path* answers for the signed-in lab admin, in *theme*."""
    answers = {
        # auth-service-router.ts: initialized, Enterprise Edition (use-enterprise-edition.ts), no XTM sign-in.
        "authStatus": {"initialized": True, "featureFlags": [], "edition": "ee", "xtmSignIn": False},
        "isLoggedIn": LAB_USER,
        "getMe": LAB_USER,
        "users.getUserTheme": theme,
        "users.getUserLocale": "en",
        "tenants.getTenants": [TENANT],
        "tenants.getTenantsWithMemberCount": [{**TENANT, "memberCount": 1}],
        "tenants.getTenant": TENANT,
        "platformCapabilities.hasPlatformAccess": True,
        "health": {"status": "ok"},
        # xtm-one-router.ts: the Ask Ariane launcher shows when configured; the url is where XTM One opens.
        "xtmOne.getSettings": {
            "configured": True,
            "url": f"http://localhost:{PORT}",
            "entitlementRefusal": None,
            "registration": "ok",
            "failingSince": None,
            "lastAnsweredAt": None,
        },
    }
    if path in answers:
        return answers[path]
    if path not in UNSTUBBED:
        UNSTUBBED.add(path)
        lab.log(f"opencrq-ask-ariane: unstubbed {path}")
    leaf = path.rsplit(".", 1)[-1]
    if leaf.startswith("count") or leaf.endswith("Count"):
        return 0
    if leaf.startswith(("list", "search")) or (leaf.startswith("get") and leaf.endswith("s")):
        return []
    return None


def trpc_response(method: str, target: str, accept: str, theme: str) -> tuple[str, bytes]:
    """The answer of a ``/trpc/*`` request: (content type, body), in the wire format the calling link reads.

    ``httpBatchStreamLink`` (queries, ``trpc-accept: application/jsonl``) reads one JSON line whose keys are
    the call indexes, each value an encoded ``[[{"result": {"data": ...}}]]`` with no async part;
    ``httpBatchLink`` (mutations) a JSON array; ``httpLink`` (``skipBatch``) one object.
    """
    parts = urllib.parse.urlsplit(target)
    paths = urllib.parse.unquote(parts.path.removeprefix("/trpc/")).split(",")
    batched = "batch" in urllib.parse.parse_qs(parts.query)
    results = [{"result": {"data": procedure_answer(path, theme)}} for path in paths]
    if not batched:
        return "application/json", json.dumps(results[0]).encode()
    if method == "GET" and "application/jsonl" in accept:
        head = {str(index): [[result]] for index, result in enumerate(results)}
        return "application/jsonl", (json.dumps(head) + "\n").encode()
    return "application/json", json.dumps(results).encode()


class Front(fixtures.Front):
    """The preview port: the chat proxy and the tRPC calls from fixtures, the rest to Vite."""

    def _theme(self) -> str:
        morsel = SimpleCookie(self.headers.get("Cookie") or "").get(THEME_COOKIE)
        return "light" if morsel is not None and morsel.value == "light" else "dark"

    def _send(self, status: int, ctype: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _subscription(self) -> None:
        """``httpSubscriptionLink``: connected, then a ping every 10 s until the page closes the stream."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b'event: connected\ndata: {"reconnectAfterInactivityMs":20000}\n\n')
        self.wfile.flush()
        while True:
            time.sleep(10)
            self.wfile.write(b"event: ping\ndata: \n\n")
            self.wfile.flush()

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/lab/theme/dark", "/lab/theme/light"):
                self._body()
                self.send_response(302)
                self.send_header("Set-Cookie", f"{THEME_COOKIE}={path.rsplit('/', 1)[-1]}; Path=/")
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path.endswith("/pending-approvals") and path.startswith(CHAT_PREFIX + "/"):
                self._body()
                self._send(200, "application/json", b"[]")
            elif path.startswith(CHAT_PREFIX + "/"):
                # The OpenCTI fixtures answer the same chatbot routes under its own prefix.
                self.path = "/chatbot" + self.path.removeprefix(CHAT_PREFIX)
                self._chatbot()
            elif path.startswith("/trpc/"):
                self._body()
                if "text/event-stream" in (self.headers.get("Accept") or ""):
                    self._subscription()
                    return
                accept = f"{self.headers.get('trpc-accept') or ''} {self.headers.get('Accept') or ''}"
                ctype, data = trpc_response(self.command, self.path, accept, self._theme())
                self._send(200, ctype, data)
            elif path.startswith("/api/"):
                self._body()
                if path not in UNSTUBBED:
                    UNSTUBBED.add(path)
                    lab.log(f"opencrq-ask-ariane: unstubbed {self.command} {path}")
                self._send(200, "application/json", b"{}")
            else:
                self._forward()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _handle


def missing_sub_paths(where: Path) -> list[str]:
    """The entries of OPENCRQ_SUB_PATHS absent from the OpenCRQ checkout at *where*, in their declared order."""
    return [sub_path for sub_path in OPENCRQ_SUB_PATHS if not (where / sub_path).exists()]


def opencrq_checkout() -> Path:
    """The nested OpenCRQ checkout; exits 1 naming every missing sub_path instead of serving a broken front."""
    where = lab.CHECKOUT / os.environ.get("LAB_OPENCRQ_CHECKOUT", "opencrq")
    missing = missing_sub_paths(where)
    if missing:
        raise SystemExit(
            f"opencrq-ask-ariane: the OpenCRQ checkout at {where} lacks {', '.join(missing)}: materialize "
            f"OpenCRQ-Platform/opencrq at the ref to review with sub_paths {list(OPENCRQ_SUB_PATHS)} under this "
            f"checkout's {where.name}/ directory (or add_paths the missing ones), then start again"
        )
    return where


def main() -> None:
    root = opencrq_checkout()
    front = root / "front"
    node, yarn, env = product.toolchain()
    overridden = fixtures.override_pins(front, product.requested_pins(lab.CHECKOUT, PINS_ENV, PINS_FILE))
    if overridden:
        env["YARN_ENABLE_IMMUTABLE_INSTALLS"] = "false"
        lab.log(f"opencrq-ask-ariane: front pins overridden: {', '.join(overridden)}")
    lab.run("yarn", [str(node), str(yarn), "install"], cwd=root, env={**os.environ, **env})
    chatbot = json.loads((front / "package.json").read_text(encoding="utf-8"))["dependencies"].get("@filigran/chatbot")
    # The fixtures' Vite tunnel and forward read the module's VITE_PORT at call time.
    fixtures.VITE_PORT = VITE_PORT
    vite = next((p for p in (front / "node_modules/vite/bin/vite.js", root / "node_modules/vite/bin/vite.js") if p.is_file()), None)
    if vite is None:
        raise SystemExit("opencrq-ask-ariane: vite is not installed under front/ or the root node_modules")
    lab.spawn(
        "opencrq-front",
        [str(node), str(vite), "--host", "127.0.0.1", "--port", str(VITE_PORT), "--strictPort"],
        cwd=front,
        env=env,
    )
    lab.wait_http(f"http://127.0.0.1:{VITE_PORT}/", 300, "opencrq-front")
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Front)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://localhost:{PORT}"
    for conversation in fixtures.CONVERSATIONS:
        lab.log(f"seeded Ask Ariane conversation: {base}/{TENANT_ID} (Ask Ariane, history: {conversation['title']})")
    lab.log(
        f"lab up: {base}/{TENANT_ID}, OpenCRQ front at @filigran/chatbot {chatbot}, signed in as lab@filigran.io; "
        f"theme at {base}/lab/theme/dark or /lab/theme/light; a message holding 'slow' holds the working state, "
        "'error' ends on an error, 'full' fills the context gauge"
    )
    while True:
        for child in lab.CHILDREN:
            if child.poll() is not None:
                raise SystemExit(f"opencrq-ask-ariane: {child.args[0]} exited with {child.returncode}")
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    finally:
        for child in lab.CHILDREN:
            if child.poll() is None:
                child.terminate()
