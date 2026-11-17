# SPDX-License-Identifier: Apache-2.0

"""Content-Security-Policy for the HTML documents the platform serves.

The platform answers two kinds of HTML documents: the SPA (the branded
``index.html``, served for every SPA route) and a handful of server-rendered
pages (the API reference, the MCP OAuth consent page, the Teams tab stub, the
Office add-in task pane under ``/office``). Everything else is JSON, a stream,
a file or an image. A CSP on those documents is defence in depth for the
output guards: when an injection slips past them, the
browser still refuses to run a script or load a resource the page never needed.

**Modes** (``CONTENT_SECURITY_POLICY_MODE``, :func:`normalize_csp_mode`):

* ``report-only`` (default) - ``Content-Security-Policy-Report-Only``: the
  browser blocks nothing and reports what the policy would have blocked to
  :data:`CSP_REPORT_PATH` (``services/csp_reports.py``). It cannot break a
  deployment, which is why it is the default.
* ``enforce`` - the same policy as ``Content-Security-Policy``.
* ``off`` - no policy header.

``Referrer-Policy: strict-origin-when-cross-origin`` (the browser default,
spelled out) rides on every document once the policy is enforced; report-only
and off send none, so one a proxy or CDN adds only when a response carries none
keeps applying. ``X-Frame-Options`` is deliberately never sent: it cannot
express the embedders a deployment declares, and ``frame-ancestors`` does its
job in ``enforce`` mode.

**One policy, per-page additions.** :data:`BASE_DIRECTIVES` is what the SPA
needs. A server-rendered page that loads from another origin, or that others
must be able to frame, declares it with :func:`register_page_policy` next to
its template. Inline scripts are allowed by the SHA-256 hash of their text
(:func:`inline_script_hashes`), computed from the page's own template - never
by ``'unsafe-inline'``, and never from a response body, which would allow an
injected script along with the page's own. A page CI cannot exercise in its
real host (a Microsoft host), or whose third-party bundle no test drives (the
API reference), stays report-only in every mode (``always_report_only``).

**Only documents.** The header is attached to ``text/html`` responses that are
not downloads (``Content-Disposition: attachment``), and never replaces a
policy a handler set itself.

**Paths.** The middleware keys on :func:`~app.core.base_path.route_path` and
writes the report endpoint through :func:`~app.core.base_path.with_base_path`,
so it sits inside ``BasePathMiddleware``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from app.core.base_path import route_path, with_base_path

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

CSP_MODES: tuple[str, ...] = ("report-only", "enforce", "off")
CSP_REPORT_PATH = "/api/v1/csp-report"
REFERRER_POLICY = "strict-origin-when-cross-origin"
ENFORCE_HEADER = "Content-Security-Policy"
REPORT_ONLY_HEADER = "Content-Security-Policy-Report-Only"

BASE_DIRECTIVES: Mapping[str, tuple[str, ...]] = {
    "default-src": ("'self'",),
    "script-src": ("'self'",),
    # React 19 ``<style precedence>`` (the branding colours), Mermaid's
    # ``<style>`` inside every diagram SVG and the diff viewer's emotion
    # sheets are inline style elements.
    "style-src": ("'self'", "'unsafe-inline'"),
    # Chat Markdown shows any image an agent links to, integration pickers
    # show provider avatars, previews and generated images are blob:/data:.
    # ``http:`` only matters on a plain-HTTP deployment: an HTTPS page
    # upgrades or blocks such an image as mixed content before this applies.
    "img-src": ("'self'", "data:", "blob:", "https:", "http:"),
    "font-src": ("'self'", "data:"),
    # The two IPC origins are XTM One Desktop's (Tauri v2): its bridge fetches
    # them before falling back to postMessage, so without them every desktop
    # page would report its native calls. The WebSocket origin is appended per
    # request (:func:`websocket_sources`).
    "connect-src": ("'self'", "ipc:", "http://ipc.localhost"),
    # Text-to-speech plays a blob: audio.
    "media-src": ("'self'", "blob:"),
    # The diff viewer computes in a blob: worker (main-thread fallback).
    "worker-src": ("'self'", "blob:"),
    # A deck's ``video`` block plays a YouTube (privacy-enhanced mode) or Vimeo
    # link in the provider's own player; no other origin is framed
    # (``presentation_spec.VIDEO_EMBED_ORIGINS``).
    "frame-src": ("'self'", "https://www.youtube-nocookie.com", "https://player.vimeo.com"),
    "object-src": ("'none'",),
    "base-uri": ("'self'",),
    # Every SPA form is submitted by script; SSO sign-in is a navigation to a
    # backend redirect, not a form post. A page whose form posts to another
    # origin (the MCP OAuth consent page, to ``BASE_URL``) widens it for that
    # one response (:func:`add_response_sources`). Browsers check every
    # redirect the submission's navigation follows too, so a form whose answer
    # must take the browser elsewhere answers with a page that navigates there
    # rather than a redirect (the consent page's sign-in).
    "form-action": ("'self'",),
}
"""The SPA's policy (the default for every HTML document without a page
policy)."""

GOOGLE_FONTS_SOURCES: Mapping[str, tuple[str, ...]] = {
    "style-src": ("https://fonts.googleapis.com",),
    "font-src": ("https://fonts.gstatic.com",),
}
"""What a page that loads its typefaces from Google Fonts needs (the SPA
self-hosts its fonts; the consent page, the API reference and the Office
add-in do not)."""

FrameAncestors = Literal["platform", "self", "any"]
"""Who may frame a page: ``platform`` - the platform itself, the origins
``CONTENT_SECURITY_POLICY_FRAME_ANCESTORS`` declares and the registered
platforms; ``self`` - only the platform; ``any`` - no ``frame-ancestors``
directive at all."""


@dataclass(frozen=True)
class PagePolicy:
    """What one server-rendered page needs beyond :data:`BASE_DIRECTIVES`."""

    sources: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Extra sources per directive, appended to the base ones."""
    script_hashes: Callable[[], Iterable[str]] | None = None
    """The ``'sha256-...'`` sources of the page's inline scripts."""
    frame_ancestors: FrameAncestors = "platform"
    always_report_only: bool = False
    omit: tuple[str, ...] = ()
    """Base directives this page must not carry at all."""


_PAGE_POLICIES: dict[str, PagePolicy] = {}
_PAGE_PREFIX_POLICIES: list[tuple[str, PagePolicy]] = []


def register_page_policy(path: str, policy: PagePolicy, *, include_subpaths: bool = False) -> None:
    """Declare what the document served at the route path ``path`` needs.

    ``include_subpaths`` covers every path under ``path`` too (a static mount).
    A later registration for the same path replaces the earlier one.
    """
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError(f"a page policy is registered on a route path such as /api/reference, got {path!r}")
    if not include_subpaths:
        _PAGE_POLICIES[path] = policy
        return
    prefix = path.rstrip("/") or "/"
    _PAGE_PREFIX_POLICIES[:] = [item for item in _PAGE_PREFIX_POLICIES if item[0] != prefix]
    _PAGE_PREFIX_POLICIES.append((prefix, policy))
    _PAGE_PREFIX_POLICIES.sort(key=lambda item: len(item[0]), reverse=True)


def page_policy_for(path: str) -> PagePolicy | None:
    """The policy registered for the route path ``path``; ``None`` for the SPA."""
    policy = _PAGE_POLICIES.get(path)
    if policy is not None:
        return policy
    for prefix, candidate in _PAGE_PREFIX_POLICIES:
        if path == prefix or path.startswith(prefix + "/"):
            return candidate
    return None


# -- settings ----------------------------------------------------------------


def normalize_csp_mode(raw: str | None) -> str:
    """Canonical ``CONTENT_SECURITY_POLICY_MODE``; blank reads as ``report-only``."""
    value = (raw or "").strip().lower().replace("_", "-")
    if not value:
        return "report-only"
    if value not in CSP_MODES:
        raise ValueError(f"CONTENT_SECURITY_POLICY_MODE must be one of {', '.join(CSP_MODES)}, got {raw!r}")
    return value


_SCHEME_SOURCE_RE = re.compile(r"[a-z][a-z0-9+.-]*:")
_HOST_SOURCE_RE = re.compile(
    r"(?:(?P<scheme>[a-z][a-z0-9+.-]*)://)?"
    r"(?P<host>\*|(?:\*\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*)"
    r"(?P<port>:(?:[0-9]{1,5}|\*))?"
    r"/?"
)
# A document with one of these schemes framing the platform is never a
# legitimate embedder.
_REFUSED_ANCESTOR_SCHEMES = frozenset({"javascript:", "data:", "blob:", "filesystem:"})


def normalize_frame_ancestor_sources(raw: object) -> list[str]:
    """Parse ``CONTENT_SECURITY_POLICY_FRAME_ANCESTORS`` into CSP source expressions.

    Comma- or space-separated origins (``https://portal.example.com``,
    ``https://*.example.com``, ``https:``, ``*``). A malformed entry raises
    rather than being skipped: the setting widens who may frame the platform,
    and an operator must not be told everything is fine while the embedder
    they meant to allow is silently absent - or while a stray ``;`` rewrote
    the policy.
    """
    if raw is None or raw == "":
        return []
    items = [raw] if isinstance(raw, str) else list(raw)  # type: ignore[call-overload]
    tokens = [token for item in items for token in re.split(r"[,\s]+", str(item)) if token]
    parsed: list[str] = []
    for token in tokens:
        lowered = token.lower()
        if lowered in ("'self'", "*"):
            parsed.append(lowered)
            continue
        if _SCHEME_SOURCE_RE.fullmatch(lowered):
            if lowered in _REFUSED_ANCESTOR_SCHEMES:
                raise ValueError(f"CONTENT_SECURITY_POLICY_FRAME_ANCESTORS cannot allow {token!r} documents")
            parsed.append(lowered)
            continue
        if "[" in lowered:
            # CSP host sources have no IPv6 form: a browser drops the entry.
            raise ValueError(
                f"CONTENT_SECURITY_POLICY_FRAME_ANCESTORS entry {token!r}: an IPv6 address cannot be named in a "
                "Content-Security-Policy; use a host name"
            )
        match = _HOST_SOURCE_RE.fullmatch(lowered)
        if match is None or (match["scheme"] and f"{match['scheme']}:" in _REFUSED_ANCESTOR_SCHEMES):
            raise ValueError(
                f"CONTENT_SECURITY_POLICY_FRAME_ANCESTORS entry {token!r} is not an origin such as "
                "https://portal.example.com, https://*.example.com or https:"
            )
        scheme = f"{match['scheme']}://" if match["scheme"] else ""
        parsed.append(f"{scheme}{match['host']}{match['port'] or ''}")
    return list(dict.fromkeys(parsed))


def permissive_frame_ancestor_sources(sources: Iterable[str]) -> list[str]:
    """The sources that let every site, or every site of a scheme, frame the
    platform: ``*``, a bare scheme such as ``https:``, a wildcard host such as
    ``https://*``."""
    permissive: list[str] = []
    for source in sources:
        host = _HOST_SOURCE_RE.fullmatch(source)
        if source == "*" or _SCHEME_SOURCE_RE.fullmatch(source) or (host is not None and host["host"] == "*"):
            permissive.append(source)
    return permissive


def log_content_security_policy_configuration() -> None:
    """Say at startup when ``CONTENT_SECURITY_POLICY_FRAME_ANCESTORS`` lets any site frame the platform.

    Accepted, not refused - a deployment may really be embedded everywhere -
    but never silently: once the policy is enforced, every such site may show
    the platform's pages in an iframe, the clickjacking ``frame-ancestors``
    exists to stop. Nothing to say while the policy is off.
    """
    from app.core.config import settings

    if settings.content_security_policy_mode == "off":
        return
    permissive = permissive_frame_ancestor_sources(settings.content_security_policy_frame_ancestors)
    if permissive:
        logger.warning(
            "CONTENT_SECURITY_POLICY_FRAME_ANCESTORS allows %s: once the Content-Security-Policy is "
            "enforced, any such site may show the platform's pages in an iframe, which is what "
            "frame-ancestors is there to prevent. List the sites that embed the platform instead, "
            "such as https://intranet.example.com or https://*.example.com.",
            ", ".join(permissive),
        )


# -- inline scripts ----------------------------------------------------------

# ``type`` values the browser executes (and therefore checks against
# ``script-src``); any other type is a data block the policy never sees.
_EXECUTED_SCRIPT_TYPES = frozenset(
    {
        "",
        "module",
        "importmap",
        "speculationrules",
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
        "text/ecmascript",
        "application/ecmascript",
        "text/jscript",
    }
)


class _InlineScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self._current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attributes = {name.lower(): (value or "") for name, value in attrs}
        script_type = attributes.get("type", "").split(";", 1)[0].strip().lower()
        executed = "src" not in attributes and script_type in _EXECUTED_SCRIPT_TYPES
        self._current = [] if executed else None

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "script":
            return
        if self._current:
            self.scripts.append("".join(self._current))
        self._current = None


def script_hash_source(text: str) -> str:
    """The ``'sha256-...'`` source that allows one inline script with this text."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def inline_script_hashes(html: str) -> tuple[str, ...]:
    """Hash sources for every inline script of ``html``, in document order.

    For a TRUSTED template only (a build artifact, a page the backend renders
    from constants and escaped values) - hashing a body that may carry
    injected markup would allow the injection too. Newlines are normalised
    the way the HTML tokenizer does before the browser hashes the text.
    """
    collector = _InlineScriptCollector()
    collector.feed(html.replace("\r\n", "\n").replace("\r", "\n"))
    collector.close()
    return tuple(dict.fromkeys(script_hash_source(text) for text in collector.scripts))


# -- per-request sources -----------------------------------------------------

# No IPv6 literal: CSP host sources cannot express one, and a browser drops
# (and logs) such a source.
_HOST_RE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*(?::[0-9]{1,5})?")


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _url_host(url: str | None) -> str | None:
    try:
        parts = urlsplit((url or "").strip())
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not hostname:
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{host}:{port}" if port else host


def websocket_sources(scope: Scope) -> tuple[str, ...]:
    """``wss://`` / ``ws://`` sources for the host(s) the page may be served on.

    ``'self'`` covers a same-origin WebSocket in current browsers but not in
    older Safari, so the origin is also named: the ``Host`` the request came
    in on (and ``X-Forwarded-Host`` behind a trusted proxy), plus the public
    origins the platform is configured with. Anything that is not a plain
    ``host[:port]`` is dropped - a header value must never reach the policy.
    """
    from app.core.config import settings
    from app.core.security_options import security_option

    candidates = [_header(scope, b"host")]
    if security_option("trust_proxy_headers"):
        forwarded = _header(scope, b"x-forwarded-host")
        if forwarded:
            candidates.append(forwarded.split(",", 1)[0])
    candidates.extend(_url_host(url) for url in (settings.base_url, settings.frontend_url))
    hosts: list[str] = []
    for candidate in candidates:
        host = (candidate or "").strip().lower()
        if host and _HOST_RE.fullmatch(host) and host not in hosts:
            hosts.append(host)
    return tuple(f"{scheme}://{host}" for host in hosts for scheme in ("wss", "ws"))


# What an origin provided at run time may look like once it reaches the
# header: a scheme, a plain host (never an IPv6 literal, which CSP host
# sources cannot express), an optional port.
_ORIGIN_SOURCE_RE = re.compile(r"https?://[a-z0-9-]+(?:\.[a-z0-9-]+)*(?::[0-9]{1,5})?")
_ORIGIN_HOSTNAME_RE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*")

RESPONSE_SOURCES_STATE = "csp_response_sources"
"""The request-state key :func:`add_response_sources` writes under."""


def origin_source(url: object) -> str | None:
    """``scheme://host[:port]`` of an ``http(s)`` URL with a plain host, else ``None``.

    For a URL that reaches a response header: anything else is dropped.
    """
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not hostname or not _ORIGIN_HOSTNAME_RE.fullmatch(hostname):
        return None
    return f"{scheme}://{hostname}{f':{port}' if port else ''}"


def add_response_sources(request: Request, directive: str, *sources: str | None) -> None:
    """Allow ``sources`` under ``directive`` in the policy of this one response.

    For an origin known only when the response is built, such as the
    ``BASE_URL`` a consent form posts to. Only origin sources
    (:func:`origin_source`) are kept, and only a directive the page's policy
    carries is widened. Never the reason a page fails: without an ASGI scope
    it does nothing.
    """
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return
    bucket = scope.setdefault("state", {}).setdefault(RESPONSE_SOURCES_STATE, {}).setdefault(directive, [])
    for source in sources:
        if isinstance(source, str) and _ORIGIN_SOURCE_RE.fullmatch(source) and source not in bucket:
            bucket.append(source)


def frame_ancestor_sources(
    page: PagePolicy | None, *, embedders: Callable[[], Iterable[str]] | None = None
) -> tuple[str, ...] | None:
    """The ``frame-ancestors`` sources for ``page``; ``None`` omits the directive.

    ``platform`` pages may be framed by the platform itself, the origins
    ``CONTENT_SECURITY_POLICY_FRAME_ANCESTORS`` declares and the origins
    ``embedders`` returns at run time (the registered platforms,
    ``services/registered_platform_origins.py``) - the latter re-checked
    here, since they come from the database.
    """
    from app.core.config import settings

    choice = page.frame_ancestors if page is not None else "platform"
    if choice == "any":
        return None
    if choice == "self":
        return ("'self'",)
    dynamic = [
        origin for origin in (embedders() if embedders is not None else ()) if _ORIGIN_SOURCE_RE.fullmatch(origin)
    ]
    return tuple(dict.fromkeys(("'self'", *settings.content_security_policy_frame_ancestors, *dynamic)))


def csp_report_endpoint() -> str:
    """The report endpoint as the browser addresses it, under ``BASE_PATH``."""
    return with_base_path(CSP_REPORT_PATH)


def build_policy(
    scope: Scope,
    *,
    page: PagePolicy | None,
    spa_script_hashes: Callable[[], Iterable[str]] | None = None,
    embedders: Callable[[], Iterable[str]] | None = None,
) -> str:
    """The policy for the document ``scope`` asks for (``page`` = its registration)."""
    directives = {name: list(sources) for name, sources in BASE_DIRECTIVES.items()}
    hashes = spa_script_hashes
    if page is not None:
        for name in page.omit:
            directives.pop(name, None)
        for name, extra in page.sources.items():
            directives.setdefault(name, list(BASE_DIRECTIVES["default-src"])).extend(extra)
        hashes = page.script_hashes
    directives["script-src"].extend(hashes() if hashes is not None else ())
    directives["connect-src"].extend(websocket_sources(scope))
    state = scope.get("state")
    response_sources = state.get(RESPONSE_SOURCES_STATE) if isinstance(state, dict) else None
    for name, extra in (response_sources or {}).items():
        if name in directives:
            directives[name].extend(source for source in extra if _ORIGIN_SOURCE_RE.fullmatch(source))
    parts = [f"{name} {' '.join(dict.fromkeys(sources))}" for name, sources in directives.items()]
    ancestors = frame_ancestor_sources(page, embedders=embedders)
    if ancestors is not None:
        parts.append(f"frame-ancestors {' '.join(ancestors)}")
    # ``report-uri`` alone: every browser resolves it against the document's
    # URL. Naming ``report-to`` makes Chromium ignore it and deliver only to an
    # HTTPS ``Reporting-Endpoints`` URL, which only ``BASE_URL`` could make
    # absolute - and that setting may name another host.
    parts.append(f"report-uri {csp_report_endpoint()}")
    return "; ".join(parts)


def policy_header(
    scope: Scope,
    *,
    spa_script_hashes: Callable[[], Iterable[str]] | None = None,
    embedders: Callable[[], Iterable[str]] | None = None,
) -> tuple[str, str] | None:
    """``(header name, policy)`` for the document ``scope`` asks for; ``None`` when off."""
    from app.core.config import settings

    mode = settings.content_security_policy_mode
    if mode == "off":
        return None
    page = page_policy_for(route_path(scope))
    report_only = mode == "report-only" or (page is not None and page.always_report_only)
    value = build_policy(scope, page=page, spa_script_hashes=spa_script_hashes, embedders=embedders)
    return (REPORT_ONLY_HEADER if report_only else ENFORCE_HEADER), value


# -- middleware --------------------------------------------------------------

_POLICY_HEADER_NAMES = frozenset({ENFORCE_HEADER.lower().encode(), REPORT_ONLY_HEADER.lower().encode()})


def _is_document(headers: list[tuple[bytes, bytes]]) -> bool:
    content_type = disposition = b""
    for name, value in headers:
        lowered = name.lower()
        if lowered == b"content-type":
            content_type = value
        elif lowered == b"content-disposition":
            disposition = value
    if content_type.split(b";", 1)[0].strip().lower() != b"text/html":
        return False
    return not disposition.strip().lower().startswith(b"attachment")


class ContentSecurityPolicyMiddleware:
    """Attach the policy - and ``Referrer-Policy`` once it is enforced - to every HTML document.

    ``spa_script_hashes`` returns the hash sources of the inline scripts of
    the document served where no page policy is registered (the branded
    ``index.html``); ``embedders`` the origins allowed to frame the platform
    at run time. Both are called on every document and must not block. Raw
    ASGI, like the other middlewares here, and never the reason a page fails:
    a policy that cannot be built is logged and the response goes out
    without it.
    """

    _warned = False

    def __init__(
        self,
        app: ASGIApp,
        *,
        spa_script_hashes: Callable[[], Iterable[str]] | None = None,
        embedders: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self.app = app
        self.spa_script_hashes = spa_script_hashes
        self.embedders = embedders

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Created here so a handler's :func:`add_response_sources` lands in
        # the dict this middleware reads, whatever copies of the scope the
        # layers below make.
        if not isinstance(scope.get("state"), dict):
            scope["state"] = {}

        async def send_with_policy(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = self._with_headers(scope, message)
            await send(message)

        await self.app(scope, receive, send_with_policy)

    def _with_headers(self, scope: Scope, message: Message) -> Message:
        from app.core.config import settings

        headers: list[tuple[bytes, bytes]] = list(message.get("headers") or [])
        if not _is_document(headers):
            return message
        try:
            names = {name.lower() for name, _ in headers}
            if settings.content_security_policy_mode == "enforce" and b"referrer-policy" not in names:
                headers.append((b"referrer-policy", REFERRER_POLICY.encode("latin-1")))
            header = (
                None
                if names & _POLICY_HEADER_NAMES
                else policy_header(scope, spa_script_hashes=self.spa_script_hashes, embedders=self.embedders)
            )
            if header is not None:
                headers.append((header[0].lower().encode("latin-1"), header[1].encode("latin-1")))
        except Exception:
            if not ContentSecurityPolicyMiddleware._warned:
                ContentSecurityPolicyMiddleware._warned = True
                logger.warning("Content-Security-Policy header could not be built", exc_info=True)
            return message
        return {**message, "headers": headers}
