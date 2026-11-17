"""A Splunk management API stand-in on the relay e2e's on-prem network.

HTTPS on 8089 with a certificate signed by the e2e's internal CA, which only
the relay trusts. Every GET answers the entry list the Splunk provider reads
(``/services/server/info``, ``/services/apps/local``), and only when the
request carries a credential: a 401 otherwise, so a passing check also proves
the integration's token travelled through the relay. Stdlib only.
"""

from __future__ import annotations

import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVER_INFO = {
    "entry": [
        {
            "name": "server-info",
            "content": {"serverName": "onprem-splunk", "version": "9.2.0", "guid": "e2e"},
        }
    ]
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not self.headers.get("Authorization"):
            self._answer(401, {"messages": [{"type": "ERROR", "text": "Unauthorized"}]})
            return
        if self.path.startswith("/services/apps/local"):
            self._answer(200, {"entry": []})
            return
        self._answer(200, SERVER_INFO)

    def _answer(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/certs/server.pem", "/certs/server-key.pem")
    server = ThreadingHTTPServer(("0.0.0.0", 8089), Handler)  # noqa: S104 - container port
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print("onprem-splunk listening on :8089", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
