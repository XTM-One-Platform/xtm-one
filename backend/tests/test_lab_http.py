"""One raw HTTP request to a lab service from inside the lab: [check.lab-http] (gap lab-http-probe-inside-lab).

The browser tool answers a redirect as an opaqueredirect, with no status and no Location, and a check container has
no route to a lab pod; .xtm-one/lab/lab_http.py prints the answer as it came. These pin the declaration and the
answer shape against an in-process server: a 302 is reported with its Location and never followed, a 404 is an
answer (exit 0), a refused connection exits 2.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import socket
import threading
import tomllib
from collections.abc import Iterator
from types import ModuleType

import pytest

from tests.test_lab_seed_declarations import CHECKS, LAB

SCRIPT = LAB.parent / "lab_http.py"

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lab_http", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        return None

    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/sign-in?next=%2F")
            self.send_header("Set-Cookie", "a=1")
            self.send_header("Set-Cookie", "b=2")
            self.end_headers()
            return
        if self.path == "/echo":
            body = json.dumps({"cookie": self.headers.get("Cookie")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "9")
        self.end_headers()
        self.wfile.write(b"not found")


@pytest.fixture
def server() -> Iterator[str]:
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_check_is_declared_on_the_python_dev_image() -> None:
    checks = tomllib.loads(CHECKS.read_text())["check"]
    check = checks["lab-http"]
    assert check["argv"] == ["python", ".xtm-one/lab/lab_http.py"]
    assert check["image"] == "python-dev"


def test_a_redirect_is_reported_with_its_location_and_not_followed(server: str) -> None:
    answer = _module().probe(f"{server}/redirect", "get", [], None, 4000, 5.0)
    assert answer["status"] == 302
    assert answer["method"] == "GET"
    assert answer["headers"]["Location"] == "/sign-in?next=%2F"
    assert answer["headers"]["Set-Cookie"] == "a=1, b=2"


def test_headers_are_sent_and_the_body_is_cut(server: str) -> None:
    answer = _module().probe(f"{server}/echo", "GET", ["Cookie: lab=1"], None, 5, 5.0)
    assert answer["status"] == 200
    assert answer["body"] == '{"coo'
    assert answer["body_length"] > 5


def test_an_error_status_is_an_answer(server: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert _module().main(["--url", f"{server}/missing"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == 404


def test_nothing_answering_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert _module().main(["--url", f"http://127.0.0.1:{port}/", "--timeout", "2"]) == 2
    assert "nothing answered" in capsys.readouterr().err


def test_a_malformed_header_exits_2(server: str) -> None:
    assert _module().main(["--url", f"{server}/echo", "--header", "no-colon"]) == 2
