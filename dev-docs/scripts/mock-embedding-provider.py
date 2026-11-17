#!/usr/bin/env python3
"""Lightweight mock server for embedding-provider validation flows.

Purpose:
- Simulate providers that may not expose /models
- Expose /embeddings or /v1/embeddings with configurable responses
- No real model loading or inference

Examples:
  python3 mock-embedding-provider.py --port 7996
  python3 mock-embedding-provider.py --port 7996 --models-mode missing --embeddings-mode unprocessable
  python3 mock-embedding-provider.py --port 7996 --prefix /v1 --require-auth --api-key test123
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        length = 0
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def make_handler(prefix: str, models_mode: str, embeddings_mode: str, require_auth: bool, api_key: str):
    base = prefix.rstrip("/")

    class MockHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args):
            # Keep output concise and deterministic for local tests.
            return

        def _auth_ok(self) -> bool:
            if not require_auth:
                return True
            auth = self.headers.get("Authorization", "")
            expected = f"Bearer {api_key}"
            return auth == expected

        def _check_auth(self) -> bool:
            if self._auth_ok():
                return True
            _json_response(
                self,
                401,
                {
                    "error": {
                        "message": "Unauthorized",
                        "type": "authentication_error",
                    }
                },
            )
            return False

        def do_GET(self):
            if self.path == "/health":
                _json_response(self, 200, {"status": "ok"})
                return

            models_paths = ["/models"]
            if base:
                models_paths.append(f"{base}/models")

            if self.path in models_paths:
                if not self._check_auth():
                    return

                if models_mode == "ok":
                    _json_response(
                        self,
                        200,
                        {
                            "object": "list",
                            "data": [
                                {
                                    "id": "mock-embedding-model",
                                    "object": "model",
                                }
                            ],
                        },
                    )
                    return
                if models_mode == "unauthorized":
                    _json_response(self, 401, {"error": {"message": "Unauthorized"}})
                    return
                # missing
                _json_response(self, 404, {"error": {"message": "Not Found"}})
                return

            _json_response(self, 404, {"error": {"message": "Not Found"}})

        def do_POST(self):
            embeddings_paths = ["/embeddings", "/embedding"]
            if base:
                embeddings_paths.extend([f"{base}/embeddings", f"{base}/embedding"])

            if self.path in embeddings_paths:
                if not self._check_auth():
                    return

                data = _read_json_body(self)
                model = data.get("model", "mock-embedding-model")
                user_input = data.get("input", "")

                if embeddings_mode == "missing":
                    _json_response(self, 404, {"error": {"message": "Not Found"}})
                    return
                if embeddings_mode == "unauthorized":
                    _json_response(self, 401, {"error": {"message": "Unauthorized"}})
                    return
                if embeddings_mode == "unprocessable":
                    _json_response(
                        self,
                        422,
                        {
                            "error": {
                                "message": "Invalid payload for embeddings",
                                "type": "invalid_request_error",
                            }
                        },
                    )
                    return

                # ok
                vector = [0.01, 0.02, 0.03, 0.04]
                _json_response(
                    self,
                    200,
                    {
                        "object": "list",
                        "data": [
                            {
                                "object": "embedding",
                                "index": 0,
                                "embedding": vector,
                            }
                        ],
                        "model": model,
                        "usage": {
                            "prompt_tokens": len(str(user_input)),
                            "total_tokens": len(str(user_input)),
                        },
                    },
                )
                return

            _json_response(self, 404, {"error": {"message": "Not Found"}})

    return MockHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mock embedding provider server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7996, help="Bind port (default: 7996)")
    parser.add_argument("--prefix", default="/v1", help="Optional API prefix, e.g. /v1 or empty string")
    parser.add_argument(
        "--models-mode",
        choices=["ok", "missing", "unauthorized"],
        default="missing",
        help="Behavior for /models endpoints",
    )
    parser.add_argument(
        "--embeddings-mode",
        choices=["ok", "unprocessable", "unauthorized", "missing"],
        default="ok",
        help="Behavior for /embeddings endpoints",
    )
    parser.add_argument(
        "--require-auth",
        action="store_true",
        help="Require Authorization: Bearer <api-key>",
    )
    parser.add_argument("--api-key", default="mock-token", help="Token used when --require-auth is set")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    handler = make_handler(
        prefix=args.prefix,
        models_mode=args.models_mode,
        embeddings_mode=args.embeddings_mode,
        require_auth=args.require_auth,
        api_key=args.api_key,
    )

    server = ThreadingHTTPServer((args.host, args.port), handler)

    prefix_info = args.prefix if args.prefix else "(no prefix)"
    print("[mock-provider] started")
    print(f"[mock-provider] listen: http://{args.host}:{args.port}")
    print(f"[mock-provider] prefix: {prefix_info}")
    print(f"[mock-provider] /models mode: {args.models_mode}")
    print(f"[mock-provider] /embeddings mode: {args.embeddings_mode}")
    if args.require_auth:
        print("[mock-provider] auth required: Bearer <api-key set>")
    else:
        print("[mock-provider] auth required: no")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[mock-provider] stopped")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
