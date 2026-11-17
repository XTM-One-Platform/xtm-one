# SPDX-License-Identifier: Apache-2.0

"""Content-Security-Policy violation reports.

Browsers POST what the platform's policy (``app/core/csp.py``) blocked - or,
in report-only mode, would have blocked - to ``/api/v1/csp-report``, in the
``report-uri`` format (``application/csp-report``, one violation per request)
and in the Reporting API format (``application/reports+json``, a batch). The
reports are what makes the switch to ``enforce`` a decision taken on data, so
they are counted and logged - but never stored, and never logged raw:

* **Scrubbed.** Every URL field loses its query string, fragment and user
  info before anything reads it: a signed file link and the export's render
  page carry their credential in the query. A blocked resource is reduced to
  its origin (its scheme for ``data:`` / ``blob:`` / an extension, the CSP
  keyword for ``inline`` / ``eval``), a document to its path with identifiers
  collapsed, a source file to its origin and path - percent-encoded wherever a
  character could read as another field of the log line. Script samples,
  referrers and the policy text are never read.
* **Aggregated.** One line the first time a violation (disposition,
  directive, blocked resource, document) is seen by the process, one summary
  line per window with the counts - never one line per report.
* **Bounded.** A body cap and a per-IP rate limit (the route), a cap on the
  reports read from one request, a cap on the distinct violations tracked
  per window.
* **Counted across replicas.** Each UTC day's violations are added to one
  Redis hash, ``csp:violations:<YYYYMMDD>``: a field per directive (the names
  of :data:`KNOWN_DIRECTIVES`, ``other`` for anything else) and
  ``extensions``, kept 8 days. The logs above hold one replica's share each;
  the security audit reads the sum (:func:`recent_violations`) to say what
  enforcing the policy would block. Fail open: without Redis a report is still
  logged and counted by its process.

Violations whose script or blocked resource belongs to a browser extension
are not the platform's: they are counted apart and never logged one by one.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote, urlsplit

from app.core.metrics import get_or_create_counter
from app.core.redis import redis_client
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

MAX_REPORT_BODY_BYTES = 64 * 1024
MAX_REPORTS_PER_REQUEST = 32
REPORT_CONTENT_TYPES = frozenset({"application/csp-report", "application/reports+json", "application/json"})
CSP_REPORT_RATE_LIMIT = 120
"""Report requests one client may send per :data:`CSP_REPORT_RATE_WINDOW_SECONDS`
(``report-uri`` sends one request per violation)."""
CSP_REPORT_RATE_WINDOW_SECONDS = 60
AGGREGATION_WINDOW_SECONDS = 300
MAX_TRACKED_VIOLATIONS = 200
VIOLATION_COUNTER_PREFIX = "csp:violations:"
VIOLATION_COUNTER_DAYS_KEPT = 8
RECENT_VIOLATION_DAYS = 7
"""The period the security audit reads: the current UTC day and the six before."""
OTHER_FIELD = "other"
EXTENSIONS_FIELD = "extensions"
_MAX_REMEMBERED_SIGNATURES = 1024
_MAX_FIELD_CHARS = 2048
_MAX_LOGGED_CHARS = 200

KNOWN_DIRECTIVES = frozenset(
    {
        "default-src",
        "script-src",
        "script-src-elem",
        "script-src-attr",
        "style-src",
        "style-src-elem",
        "style-src-attr",
        "img-src",
        "font-src",
        "connect-src",
        "media-src",
        "worker-src",
        "frame-src",
        "child-src",
        "object-src",
        "manifest-src",
        "base-uri",
        "form-action",
        "frame-ancestors",
        "require-trusted-types-for",
        "trusted-types",
    }
)
_BLOCKED_KEYWORDS = frozenset({"inline", "eval", "wasm-eval", "self", "trusted-types-policy", "trusted-types-sink"})
_SCHEME_KEYWORDS = {"data": "data:", "blob": "blob:"}
_NETWORK_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_EXTENSION_SCHEMES = frozenset(
    {
        "chrome-extension",
        "moz-extension",
        "safari-extension",
        "safari-web-extension",
        "ms-browser-extension",
        "edge-extension",
    }
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
# A host that may reach a log line: a plain name or an IP literal, nothing a
# reader could mistake for another ``key=value`` field.
_LOGGABLE_HOST_RE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*|[0-9a-f:.]+")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_TOKEN_RE = re.compile(r"(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{20,}")
# What a path segment keeps as is on a log line: the RFC 3986 path characters,
# the browser's own percent-escapes included, minus ``=``. Anything else - a
# space or an ``=`` that would fake another ``key=value`` field, a quote, an
# invisible or bidirectional character - is percent-encoded.
_LOG_SAFE_SEGMENT_CHARS = "-._~!$&'()*+,;:@%"

csp_violation_reports = get_or_create_counter(
    "csp_violation_reports_total",
    "Content-Security-Policy violations reported by browsers",
    ["directive", "disposition", "origin"],
)
"""Per directive, per disposition (``report`` / ``enforce``), per origin
(``page`` / ``extension``): the signal that says whether ``enforce`` would
block anything the platform's own pages do."""


@dataclass(frozen=True)
class Violation:
    """One reported violation, already scrubbed."""

    directive: str
    blocked: str
    document: str
    disposition: str
    source: str
    extension: bool


def _clean(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROL_CHARS_RE.sub("", value[:_MAX_FIELD_CHARS]).strip()


def _split(text: str):
    try:
        parts = urlsplit(text)
        return parts, parts.hostname, parts.port
    except ValueError:
        return None, None, None


def _origin(scheme: str, hostname: str, port: int | None) -> str | None:
    if not _LOGGABLE_HOST_RE.fullmatch(hostname):
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{scheme}://{host}{f':{port}' if port else ''}"


def _collapsed_path(path: str) -> str:
    return "/".join(_collapse(segment) for segment in (path or "/").split("/"))


def _scheme(text: str) -> str:
    parts, _, _ = _split(text)
    return parts.scheme.lower() if parts is not None else ""


def scrub_blocked(value: object) -> str:
    """The blocked resource as it may be logged: an origin, a scheme or a CSP keyword."""
    text = _clean(value)
    if not text:
        return "(none)"
    lowered = text.lower()
    if lowered in _BLOCKED_KEYWORDS:
        return lowered
    if lowered in _SCHEME_KEYWORDS:
        return _SCHEME_KEYWORDS[lowered]
    parts, hostname, port = _split(text)
    if parts is None:
        return "(invalid)"
    scheme = parts.scheme.lower()
    if scheme in _NETWORK_SCHEMES and hostname:
        return (_origin(scheme, hostname, port) or "(invalid)")[:_MAX_LOGGED_CHARS]
    if scheme:
        return f"{scheme}:"
    return "(relative)"


def _collapse(segment: str) -> str:
    if segment.isdigit() or _UUID_RE.fullmatch(segment) or _TOKEN_RE.fullmatch(segment):
        return ":id"
    return quote(segment[:64], safe=_LOG_SAFE_SEGMENT_CHARS)


def scrub_document(value: object) -> str:
    """The document's path, identifiers collapsed (``/presentations/:id/speaker``)."""
    text = _clean(value)
    if not text:
        return "(none)"
    parts, _, _ = _split(text)
    if parts is None:
        return "(invalid)"
    scheme = parts.scheme.lower()
    if scheme and scheme not in _NETWORK_SCHEMES:
        return f"{scheme}:"
    return _collapsed_path(parts.path)[:_MAX_LOGGED_CHARS] or "/"


def scrub_source(value: object) -> str:
    """The script that caused the violation: origin and path (identifiers
    collapsed, like the document's - an inline violation names the document
    itself), never the query."""
    text = _clean(value)
    if not text:
        return ""
    parts, hostname, port = _split(text)
    if parts is None:
        return ""
    scheme = parts.scheme.lower()
    if scheme in _NETWORK_SCHEMES and hostname:
        origin = _origin(scheme, hostname, port)
        if origin is None:
            return "(invalid)"
        return f"{origin}{_collapsed_path(parts.path)}"[:_MAX_LOGGED_CHARS]
    return f"{scheme}:" if scheme else ""


def _directive(*candidates: object) -> str:
    for candidate in candidates:
        text = _clean(candidate).lower()
        if text:
            # ``violated-directive`` may carry the directive's sources as well.
            name = text.split(None, 1)[0]
            return name if name in KNOWN_DIRECTIVES else "other"
    return "unknown"


def _disposition(value: object) -> str:
    text = _clean(value).lower()
    return text if text in ("report", "enforce") else "unknown"


def _violation(
    *, effective: object, violated: object, blocked: object, document: object, disposition: object, source: object
) -> Violation:
    extension = _scheme(_clean(blocked)) in _EXTENSION_SCHEMES or _scheme(_clean(source)) in _EXTENSION_SCHEMES
    return Violation(
        directive=_directive(effective, violated),
        blocked=scrub_blocked(blocked),
        document=scrub_document(document),
        disposition=_disposition(disposition),
        source=scrub_source(source),
        extension=extension,
    )


def parse_reports(payload: object) -> list[Violation]:
    """Every violation a report body carries, in either format, capped."""
    if isinstance(payload, Mapping):
        report = payload.get("csp-report")
        if not isinstance(report, Mapping):
            return []
        return [
            _violation(
                effective=report.get("effective-directive"),
                violated=report.get("violated-directive"),
                blocked=report.get("blocked-uri"),
                document=report.get("document-uri"),
                disposition=report.get("disposition"),
                source=report.get("source-file"),
            )
        ]
    if not isinstance(payload, list):
        return []
    violations: list[Violation] = []
    for item in payload:
        if len(violations) >= MAX_REPORTS_PER_REQUEST:
            break
        if not isinstance(item, Mapping) or item.get("type") != "csp-violation":
            continue
        body = item.get("body")
        if not isinstance(body, Mapping):
            continue
        violations.append(
            _violation(
                effective=body.get("effectiveDirective"),
                violated=body.get("violatedDirective"),
                blocked=body.get("blockedURL"),
                document=body.get("documentURL") or item.get("url"),
                disposition=body.get("disposition"),
                source=body.get("sourceFile"),
            )
        )
    return violations


_Signature = tuple[str, str, str, str]


class ViolationLog:
    """Deduplicated, windowed logging of violations (one process's share)."""

    def __init__(
        self,
        *,
        window_seconds: float = AGGREGATION_WINDOW_SECONDS,
        max_tracked: int = MAX_TRACKED_VIOLATIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window_seconds = window_seconds
        self.max_tracked = max_tracked
        self._clock = clock
        self._lock = threading.Lock()
        self._window_start = clock()
        self._counts: dict[_Signature, int] = {}
        self._untracked = 0
        self._extension = 0
        self._seen: OrderedDict[_Signature, None] = OrderedDict()

    def record(self, violations: Iterable[Violation]) -> None:
        first_seen: list[Violation] = []
        with self._lock:
            summary = self._drain_if_due()
            for violation in violations:
                if violation.extension:
                    self._extension += 1
                    continue
                signature = (violation.disposition, violation.directive, violation.blocked, violation.document)
                if signature in self._counts:
                    self._counts[signature] += 1
                    continue
                if len(self._counts) >= self.max_tracked:
                    self._untracked += 1
                    continue
                self._counts[signature] = 1
                if signature in self._seen:
                    self._seen.move_to_end(signature)
                    continue
                self._seen[signature] = None
                if len(self._seen) > _MAX_REMEMBERED_SIGNATURES:
                    self._seen.popitem(last=False)
                first_seen.append(violation)
        if summary is not None:
            self._log_summary(*summary)
        for violation in first_seen:
            logger.warning(
                "CSP violation first seen: directive=%s blocked=%s document=%s disposition=%s%s",
                violation.directive,
                violation.blocked,
                violation.document,
                violation.disposition,
                f" source={violation.source}" if violation.source else "",
            )

    def flush(self) -> None:
        """Log the current window's summary now: at shutdown (the lifespan in
        ``main.py``) and in tests. Otherwise a window is summarised by the first
        report that arrives after it closed."""
        with self._lock:
            summary = self._drain()
        if summary is not None:
            self._log_summary(*summary)

    def _drain_if_due(self):
        if self._clock() - self._window_start < self.window_seconds:
            return None
        return self._drain()

    def _drain(self):
        counts, untracked, extension = self._counts, self._untracked, self._extension
        elapsed = self._clock() - self._window_start
        self._counts, self._untracked, self._extension = {}, 0, 0
        self._window_start = self._clock()
        if not counts and not untracked and not extension:
            return None
        return counts, untracked, extension, elapsed

    @staticmethod
    def _log_summary(counts: dict[_Signature, int], untracked: int, extension: int, elapsed: float) -> None:
        total = sum(counts.values()) + untracked
        top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:10]
        detail = "; ".join(
            f"{directive} blocked {blocked} on {document} ({disposition}) x{count}"
            for (disposition, directive, blocked, document), count in top
        )
        logger.info(
            "CSP violation summary: %d violations (%d distinct) in the last %ds, "
            "%d from browser extensions, %d not tracked%s",
            total,
            len(counts),
            round(elapsed),
            extension,
            untracked,
            f"; top: {detail}" if detail else "",
        )


violation_log = ViolationLog()


def _directive_label(directive: str) -> str:
    return directive if directive in KNOWN_DIRECTIVES or directive == "unknown" else "other"


async def record_reports(violations: list[Violation]) -> None:
    """Count and log the violations of one report request."""
    for violation in violations:
        csp_violation_reports.labels(
            directive=_directive_label(violation.directive),
            disposition=violation.disposition,
            origin="extension" if violation.extension else "page",
        ).inc()
    if violations:
        violation_log.record(violations)
        await count_violations(violations)


# -- counters shared by every replica ----------------------------------------

_COUNTER_FIELDS = KNOWN_DIRECTIVES | {OTHER_FIELD, EXTENSIONS_FIELD}
_COUNTER_TTL_SECONDS = VIOLATION_COUNTER_DAYS_KEPT * 24 * 60 * 60
_COUNTER_TIMEOUT_SECONDS = 2.0
_counter_failure_logged = False


def _utc_today() -> date:
    return datetime.now(UTC).date()


def violation_counter_key(day: date) -> str:
    """The Redis hash holding the violations reported on the UTC day ``day``."""
    return f"{VIOLATION_COUNTER_PREFIX}{day:%Y%m%d}"


def _counter_field(violation: Violation) -> str:
    if violation.extension:
        return EXTENSIONS_FIELD
    directive = violation.directive.lower()
    return directive if directive in KNOWN_DIRECTIVES else OTHER_FIELD


def _log_counter_failure(action: str, exc: Exception) -> None:
    global _counter_failure_logged
    log = logger.debug if _counter_failure_logged else logger.warning
    _counter_failure_logged = True
    log(
        "CSP violations could not be %s Redis: %s. Reports are still logged, but the security audit "
        "cannot say what enforcing the policy would block while Redis does not answer.",
        action,
        format_exception_message(exc),
    )


async def count_violations(violations: Iterable[Violation]) -> None:
    """Add one request's violations to today's shared counters, in one round trip.

    Never raises, and never holds the report longer than
    :data:`_COUNTER_TIMEOUT_SECONDS`: a Redis failure costs the shared counts,
    never the report or its logging.
    """
    fields = Counter(_counter_field(violation) for violation in violations)
    if not fields:
        return
    key = violation_counter_key(_utc_today())
    try:
        redis = await redis_client()
        pipe = redis.pipeline()
        for name, count in fields.items():
            pipe.hincrby(key, name, count)
        pipe.expire(key, _COUNTER_TTL_SECONDS)
        await asyncio.wait_for(pipe.execute(), timeout=_COUNTER_TIMEOUT_SECONDS)
    except Exception as exc:
        _log_counter_failure("counted in", exc)


@dataclass(frozen=True)
class RecentViolations:
    """What browsers reported over the last :attr:`days` UTC days, every replica included."""

    days: int
    known: bool
    """``False`` when Redis could not be read: the counts are unknown, not zero."""
    by_directive: Mapping[str, int] = field(default_factory=dict)
    """Violations of the platform's pages per directive, largest first."""
    extensions: int = 0
    """Violations caused by browser extensions, counted apart."""

    @property
    def total(self) -> int:
        return sum(self.by_directive.values())


async def recent_violations(days: int = RECENT_VIOLATION_DAYS) -> RecentViolations:
    """The counters of the current UTC day and the ``days - 1`` before it, summed in one round trip."""
    if not 1 <= days <= VIOLATION_COUNTER_DAYS_KEPT:
        raise ValueError(f"the violation counters keep {VIOLATION_COUNTER_DAYS_KEPT} days, got days={days}")
    today = _utc_today()
    try:
        redis = await redis_client()
        pipe = redis.pipeline(transaction=False)
        for offset in range(days):
            pipe.hgetall(violation_counter_key(today - timedelta(days=offset)))
        rows = await asyncio.wait_for(pipe.execute(), timeout=_COUNTER_TIMEOUT_SECONDS)
    except Exception as exc:
        _log_counter_failure("read from", exc)
        return RecentViolations(days=days, known=False)
    totals: Counter[str] = Counter()
    for row in rows:
        for name, value in (row or {}).items():
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            if count > 0:
                totals[name if name in _COUNTER_FIELDS else OTHER_FIELD] += count
    extensions = totals.pop(EXTENSIONS_FIELD, 0)
    by_directive = dict(sorted(totals.items(), key=lambda item: (-item[1], item[0])))
    return RecentViolations(days=days, known=True, by_directive=by_directive, extensions=extensions)
