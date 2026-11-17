# SPDX-License-Identifier: Apache-2.0

"""Browser-sent Content-Security-Policy violation reports.

Unauthenticated by nature - the browser sends them, from any page carrying
the policy - so everything here is bounded before it is read: a per-client
rate limit, the accepted content types, the body size (checked on the bytes
actually read, never on the declared length alone). Parsing, scrubbing and
logging live in ``services/csp_reports.py``.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.core.http_body import read_bounded_body
from app.core.rate_limit import enforce_rate_limit
from app.services.csp_reports import (
    CSP_REPORT_RATE_LIMIT,
    CSP_REPORT_RATE_WINDOW_SECONDS,
    MAX_REPORT_BODY_BYTES,
    REPORT_CONTENT_TYPES,
    parse_reports,
    record_reports,
)

router = APIRouter()


@router.post("", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
async def receive_csp_report(request: Request) -> Response:
    # Quiet: during a Redis outage the counters say so once per process.
    await enforce_rate_limit(
        request,
        "csp_report",
        limit=CSP_REPORT_RATE_LIMIT,
        window_seconds=CSP_REPORT_RATE_WINDOW_SECONDS,
        quiet=True,
    )
    media_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media_type not in REPORT_CONTENT_TYPES:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Not a CSP report")
    body = await read_bounded_body(request, MAX_REPORT_BODY_BYTES)
    if body is None:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Report too large")
    try:
        payload = json.loads(body)
    # Deep nesting, well under the size cap, exhausts the parser's recursion.
    except (ValueError, RecursionError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed report") from None
    await record_reports(parse_reports(payload))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
