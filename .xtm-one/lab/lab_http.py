"""One raw HTTP request to a lab service, from inside the lab (``[check.lab-http]``).

Run with ``sandbox_run_command(check="lab-http", service_id=<the lab>, args=["--url", "http://127.0.0.1:<port>/path"])``:
the command runs in the lab's workspace container, next to the lab's services on the pod's ``localhost``, where a
check container has no route and the browser tool hides a redirect (an ``opaqueredirect`` answer has no status and
no ``Location``). The answer is printed as it came, redirects never followed (gap lab-http-probe-inside-lab):

    --url URL           the address, on the lab's localhost (any http or https URL the lab container reaches)
    --method NAME       GET by default
    --header 'K: V'     a request header, repeatable (a Cookie, an Authorization)
    --data TEXT         a request body (sent as is)
    --max-body N        body characters printed (default 4000)

Prints one JSON object: status, reason, headers (every header, repeated ones joined), the body's first characters
and its length. Exit 0 on any HTTP answer (a 3xx, 4xx or 5xx included: the answer is the point), 2 when nothing
answers. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand every 3xx back as it came: the status and its Location are what a redirect probe reads."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _headers(message) -> dict[str, str]:  # type: ignore[no-untyped-def]
    merged: dict[str, str] = {}
    for name, value in message.items():
        merged[name] = f"{merged[name]}, {value}" if name in merged else value
    return merged


def probe(url: str, method: str, headers: list[str], data: str | None, max_body: int, timeout: float) -> dict:
    """The answer of one request, redirects not followed; an HTTP error status is an answer, not a failure."""
    request = urllib.request.Request(url, data=data.encode() if data is not None else None, method=method.upper())
    for header in headers:
        name, _, value = header.partition(":")
        if not name.strip() or not _:
            raise ValueError(f"--header {header!r} is not 'Name: value'")
        request.add_header(name.strip(), value.strip())
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        response = opener.open(request, timeout=timeout)  # noqa: S310 - the lab's own services
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        status = response.status if hasattr(response, "status") else response.code
        reason = getattr(response, "reason", "") or ""
        answer_headers = _headers(response.headers)
    body = raw.decode("utf-8", "replace")
    return {
        "url": url,
        "method": method.upper(),
        "status": status,
        "reason": str(reason),
        "headers": answer_headers,
        "body_length": len(raw),
        "body": body[:max_body],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--method", default="GET")
    parser.add_argument("--header", action="append", default=[])
    parser.add_argument("--data", default=None)
    parser.add_argument("--max-body", type=int, default=4000)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        answer = probe(args.url, args.method, args.header, args.data, args.max_body, args.timeout)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"nothing answered at {args.url}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(answer, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
