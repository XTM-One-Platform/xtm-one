"""The preview Fronts of the product labs relay an upstream answer without breaking keep-alive.

Every ``*_pr_head.py`` / ``opencti_ask_ariane.py`` Front forwards the browser's requests to the
product's dev server (Vite, next dev) and re-chunks the answer. A 304 (Vite's answer for a cached
module), a 204 and a HEAD answer carry no body: a chunked terminator written after them was read
by the browser as the start of the next answer on the same connection, so the next lazy chunk
failed with ERR_INVALID_HTTP_RESPONSE and the page fell in its error boundary. lab.py starts the
stores when imported, so its functions are compiled one by one from its syntax tree (as
``test_lab_echo_script.py`` does). dev-docs/reference/agentic-labs.md, "The product labs".
"""

from __future__ import annotations

import ast
import http.client
import http.server
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")

NAMES = ("HOP_BY_HOP_HEADERS", "has_body", "relay_response")
BODY = b"export default 1;\n" * 64


def _lab_namespace() -> dict:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in NAMES)
        or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in NAMES for t in node.targets))
    ]
    namespace: dict = {"http": http}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace


@pytest.mark.parametrize(
    ("command", "status", "expected"),
    [
        ("GET", 200, True),
        ("POST", 201, True),
        ("GET", 404, True),
        ("GET", 304, False),
        ("GET", 204, False),
        ("GET", 101, False),
        ("HEAD", 200, False),
        ("head", 200, False),
    ],
)
def test_has_body_follows_rfc_9112(command, status, expected):
    assert _lab_namespace()["has_body"](command, status) is expected


class _Upstream(http.server.BaseHTTPRequestHandler):
    """Vite in small: a 304 to a conditional request, a 204 to /empty, the module otherwise."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.headers.get("If-None-Match"):
            self.send_response(304)
            self.send_header("ETag", '"w1"')
            self.end_headers()
        elif self.path == "/empty":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Content-Length", str(len(BODY)))
            self.send_header("X-Upstream", "vite")
            self.end_headers()
            self.wfile.write(BODY)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()

    def log_message(self, *args):
        pass


def _serve(handler) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def front():
    relay = _lab_namespace()["relay_response"]
    upstream = _serve(_Upstream)

    class Front(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _forward(self):
            connection = http.client.HTTPConnection("127.0.0.1", upstream.server_address[1], timeout=10)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
            connection.request(self.command, self.path, headers=headers)
            relay(self, connection.getresponse(), lambda key, value: value.upper() if key == "X-Upstream" else value)
            connection.close()

        do_GET = do_HEAD = _forward  # noqa: N815 - http.server's names

        def log_message(self, *args):
            pass

    server = _serve(Front)
    yield server.server_address[1]
    server.shutdown()
    upstream.shutdown()


def test_a_bodyless_answer_leaves_the_next_answer_on_the_connection_intact(front):
    browser = http.client.HTTPConnection("127.0.0.1", front, timeout=10)
    browser.request("GET", "/src/actions/Exercise.ts", headers={"If-None-Match": '"w1"'})
    cached = browser.getresponse()
    assert cached.status == 304 and cached.read() == b""
    assert cached.getheader("Transfer-Encoding") is None and cached.getheader("ETag") == '"w1"'
    browser.request("GET", "/empty")
    empty = browser.getresponse()
    assert empty.status == 204 and empty.read() == b""
    browser.request("HEAD", "/src/components/Breadcrumbs.tsx")
    head = browser.getresponse()
    assert head.status == 200 and head.read() == b""
    browser.request("GET", "/src/components/Breadcrumbs.tsx")
    module = browser.getresponse()
    assert module.status == 200
    assert module.getheader("Transfer-Encoding") == "chunked"
    assert module.getheader("X-Upstream") == "VITE"
    assert module.read() == BODY
    browser.close()
