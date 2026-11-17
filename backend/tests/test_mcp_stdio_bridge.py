"""The stdio bridge of the Claude Desktop bundle speaks 2026-07-28 too.

``app/services/mcp_packages/data/proxy/index.js`` forwards each line of stdin
as one POST. Run with node against a local HTTP server, it must:

- mirror ``MCP-Protocol-Version`` (from ``_meta``), ``Mcp-Method`` and, for a
  ``tools/call``, ``Mcp-Name`` (Base64 sentinel for a non-ASCII name) into
  headers, which a 2026-07-28 server checks against the body;
- relay an SSE answer (``subscriptions/listen``) event by event, as each
  arrives, skipping an event that is not JSON, and turn a stream that ends
  without answering its request into a transport error;
- relay a JSON-RPC error the server answered with a non-2xx status as is, so a
  client reads ``UnsupportedProtocolVersionError`` and retries.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "app" / "services" / "mcp_packages" / "data" / "proxy" / "index.js"
META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}


def _node() -> str:
    node = shutil.which("node")
    if not node:
        if os.environ.get("CI"):
            pytest.fail("node is not installed on the CI runner; the Backend job must set it up")
        pytest.skip("node is not installed")
    return node


class _Server(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *_args):
        return

    def do_POST(self):
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Server.seen.append(
            {
                "method": message.get("method"),
                "version": self.headers.get("MCP-Protocol-Version"),
                "mcp_method": self.headers.get("Mcp-Method"),
                "mcp_name": self.headers.get("Mcp-Name"),
            }
        )
        if message["method"] == "subscriptions/listen":
            body = (
                'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/subscriptions/acknowledged",'
                '"params":{"notifications":{}}}\n\n'
                f'event: message\ndata: {{"jsonrpc":"2.0","id":{message["id"]},"result":{{"resultType":"complete"}}}}\n\n'
            ).encode()
            self._answer(200, "text/event-stream", body)
        elif message["method"] == "tools/list":
            error = {"code": -32022, "message": "Unsupported protocol version", "data": {"supported": ["2025-11-25"]}}
            self._answer(400, "application/json", json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": error}).encode())
        else:
            self._answer(200, "application/json", json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}).encode())

    def _answer(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _serving(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_bridge_speaks_the_2026_07_28_transport():
    node = _node()
    _Server.seen = []
    with _serving(_Server) as server:
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "réponse", "arguments": {}, "_meta": META}},
            {"jsonrpc": "2.0", "id": 2, "method": "subscriptions/listen", "params": {"_meta": META}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {"_meta": META}},
        ]
        completed = subprocess.run(  # noqa: S603 - node runs the bridge shipped in the repository
            [node, str(BRIDGE)],
            input="".join(json.dumps(line) + "\n" for line in lines),
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "XTM_MCP_URL": f"http://127.0.0.1:{server.server_port}/mcp/xtm"},
        )

    assert completed.returncode == 0, completed.stderr
    by_method = {entry["method"]: entry for entry in _Server.seen}
    call = by_method["tools/call"]
    assert call["version"] == "2026-07-28"
    assert call["mcp_method"] == "tools/call"
    # "réponse" is not plain ASCII: sent in the Base64 sentinel form.
    assert call["mcp_name"] == "=?base64?csOpcG9uc2U=?="
    assert by_method["subscriptions/listen"]["mcp_name"] is None

    answers = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    by_id = {}
    for answer in answers:
        by_id.setdefault(answer.get("id"), []).append(answer)
    assert by_id[1] == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
    # The SSE answer: the acknowledgment, then the completion.
    assert any(a.get("method") == "notifications/subscriptions/acknowledged" for a in by_id[None])
    assert by_id[2] == [{"jsonrpc": "2.0", "id": 2, "result": {"resultType": "complete"}}]
    # The server's JSON-RPC error, relayed with its code and data.
    (refused,) = by_id[3]
    assert refused["error"]["code"] == -32022
    assert refused["error"]["data"] == {"supported": ["2025-11-25"]}


# ── A streamed answer is relayed as it arrives ───────────────────────────────

_ACK = '{"jsonrpc":"2.0","method":"notifications/subscriptions/acknowledged","params":{"notifications":{}}}'
# The wait for the first relayed event spans node's start-up, the bridge's
# load and the HTTP round trip, which take seconds on a loaded xdist worker:
# the pass condition is the ordering (first event read while the server still
# holds the last one), the bound only ends a test whose bridge buffers.
_FIRST_EVENT_SECONDS = 45
_HOLD_SECONDS = 60


class _StreamingServer(BaseHTTPRequestHandler):
    """Streams an SSE answer whose events ``events`` says, without a length.

    ``hold`` is waited for before the last event: a bridge that buffered the
    stream would never relay the first event, and the test would time out.
    The server's bound (``_HOLD_SECONDS``) outlasts the test's wait for the
    first event (``_FIRST_EVENT_SECONDS``), so a buffering bridge cannot slip
    the first event in before the test gives up.
    """

    events: list[str] = []
    hold: threading.Event | None = None

    def log_message(self, *_args):
        return

    def do_POST(self):
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()  # HTTP/1.0: the body ends when the connection closes
        events = [event.replace("{id}", str(message["id"])) for event in _StreamingServer.events]
        for index, event in enumerate(events):
            if index == len(events) - 1 and _StreamingServer.hold is not None:
                _StreamingServer.hold.wait(timeout=_HOLD_SECONDS)
            self.wfile.write(f"event: message\ndata: {event}\n\n".encode())
            self.wfile.flush()


def _run_streaming(events: list[str], *, hold: threading.Event | None = None) -> tuple[list[dict], str]:
    node = _node()
    _StreamingServer.events = events
    _StreamingServer.hold = hold
    with _serving(_StreamingServer) as server:
        process = subprocess.Popen(  # noqa: S603 - node runs the bridge shipped in the repository
            [node, str(BRIDGE)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "XTM_MCP_URL": f"http://127.0.0.1:{server.server_port}/mcp/xtm"},
        )
        request = {"jsonrpc": "2.0", "id": 7, "method": "subscriptions/listen", "params": {"_meta": META}}
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        answers = []
        if hold is not None:
            # The first event must arrive while the server still holds the
            # last one: a bridge that waits for the end of the stream would
            # relay nothing here.
            first: list[str] = []
            relayed = threading.Event()

            def read_first() -> None:
                first.append(process.stdout.readline())
                relayed.set()

            reader = threading.Thread(target=read_first, daemon=True)
            reader.start()
            # hold is still clear here, so the server has not sent the last
            # event: a line read now was relayed before the stream ended.
            arrived_early = relayed.wait(timeout=_FIRST_EVENT_SECONDS) and bool(first[0])
            hold.set()
            reader.join(timeout=15)
            assert arrived_early, "the first streamed event was not relayed before the stream ended"
            answers.append(json.loads(first[0]))
        process.stdin.close()
        out, err = process.communicate(timeout=20)
    answers += [json.loads(line) for line in out.splitlines() if line.strip()]
    return answers, err


def test_a_streamed_event_is_relayed_before_the_stream_ends():
    final = '{"jsonrpc":"2.0","id":{id},"result":{"resultType":"complete"}}'
    answers, _ = _run_streaming([_ACK, final], hold=threading.Event())
    assert answers[0]["method"] == "notifications/subscriptions/acknowledged"
    assert answers[1:] == [{"jsonrpc": "2.0", "id": 7, "result": {"resultType": "complete"}}]


def test_an_event_that_is_not_json_is_skipped_not_the_stream():
    final = '{"jsonrpc":"2.0","id":{id},"result":{"resultType":"complete"}}'
    answers, err = _run_streaming([_ACK, "{not json", final])
    assert [a.get("method") for a in answers] == ["notifications/subscriptions/acknowledged", None]
    assert answers[1] == {"jsonrpc": "2.0", "id": 7, "result": {"resultType": "complete"}}
    assert "not valid JSON" in err


def test_a_stream_that_never_answers_is_a_transport_error():
    answers, _ = _run_streaming([_ACK])
    assert answers[0]["method"] == "notifications/subscriptions/acknowledged"
    (failure,) = answers[1:]
    assert failure["id"] == 7
    assert failure["error"]["code"] == -32000
    assert "without answering" in failure["error"]["message"]
