# SPDX-License-Identifier: Apache-2.0

"""Serving the platform under a URL path prefix.

A deployment that puts the platform behind a reverse proxy at
``https://intranet.example.com/xtm-one/`` sets ``BASE_PATH=/xtm-one``. Two
proxy configurations exist in the wild and this module makes both work:

* the proxy **keeps** the prefix (nginx ``proxy_pass http://xtm-one:4000;``
  with no trailing slash, Traefik without ``StripPrefix``): the app receives
  ``GET /xtm-one/api/v1/agents``;
* the proxy **strips** it (nginx ``proxy_pass http://xtm-one:4000/;``,
  Traefik ``StripPrefix``): the app receives ``GET /api/v1/agents``.

The ASGI spec has one shape for a mounted application: ``scope["path"]`` is
the full request path INCLUDING the prefix, and ``scope["root_path"]`` is the
prefix. Starlette routes on ``path`` minus ``root_path``, so
:class:`BasePathMiddleware` - the OUTERMOST layer - normalises every request
to that shape, re-adding the prefix a stripping proxy removed. Everything
below it then sees one consistent request whatever the proxy did:
``request.url`` is the public URL, ``request.base_url`` carries the prefix,
the ``/office`` static mount resolves, and the SPA catch-all's ``full_path``
is the route path.

**Which shape a request took** is decided by one rule, in this order:

1. an ``X-Forwarded-Prefix`` header naming the prefix says the proxy stripped
   it (Traefik's ``StripPrefix`` sends it by itself; nginx needs one
   ``proxy_set_header`` line) - the request is re-prefixed whatever its path;
2. otherwise a path under the prefix was forwarded, any other path was
   stripped - or is a direct hit on the container (a liveness probe, the
   export's loopback render), which is the stripped shape by construction.

Rule 2 cannot tell ``/tools/123`` stripped from the public ``/tools/tools/123``
apart from the forwarded ``/tools/123`` when ``BASE_PATH`` is itself the
address of a platform page (``/tools``, ``/agents``, ``/chat``, ...). That is
what ``BASE_PATH_MODE`` (``config.py``) is for: ``auto`` refuses such a prefix
at startup (:func:`spa_route_problem`), ``forwarded`` declares the proxy keeps
the prefix so rule 2 is exact, ``stripped`` declares the proxy removes it and
tags every request with the header so rule 1 always decides. The mode changes
what the platform ACCEPTS at startup; the request rule is the same for all.

A middleware that keys on the request path (public CORS, licence gate,
concurrency exemptions) must read :func:`route_path`, never
``scope["path"]`` - the latter now carries the prefix.

Pure-stdlib on purpose: ``core/config.py`` imports :func:`normalize_base_path`
at settings-construction time and the frontend build has a mirror of the
same normalisation in ``frontend/src/lib/base-path.ts``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

# A prefix segment is made of RFC 3986 *unreserved* characters only. Anything
# else is refused rather than encoded: the middleware rebuilds ``raw_path`` as
# ASCII, the browser reports ``window.location.pathname`` percent-ENCODED (so
# the SPA's prefix comparison would never match a character that needs
# encoding), and the value is written into an HTML attribute, a ``<meta>``
# and a ``<script>`` literal - one character set that is safe in every one of
# those contexts is worth more than a second escaping rule per sink.
_SEGMENT_RE = re.compile(r"[A-Za-z0-9._~-]+")

# First path segments the platform itself owns at the root. A base path
# starting with one of these would make the request shape ambiguous: with
# ``BASE_PATH=/api`` a stripped ``GET /api/v1/agents`` is indistinguishable
# from an un-stripped ``GET /api/api/v1/agents`` and one of the two proxy
# configurations routes wrong. Refused at startup (``config.py``).
# ``tests/test_base_path.py`` derives the namespaces the application really
# serves at the root - router prefixes, mounts, top-level routes, the files
# the SPA build copies from ``frontend/public`` - and fails when one is
# missing here; extend this set when a new root namespace is added.
RESERVED_FIRST_SEGMENTS: frozenset[str] = frozenset(
    {
        # Machine-facing namespaces.
        "api",
        "v1",
        "mcp",
        "xtm",
        "a2a",
        ".well-known",
        "office",
        "metrics",
        # The SPA build: Vite's output directory, the document itself and the
        # files served by name from ``frontend/public``.
        "assets",
        "index.html",
        "email-assets",
        "logos",
        "apple-touch-icon.png",
        "copilot-icon-192.png",
        "copilot-icon-512.png",
        "copilot-icon-maskable-512.png",
        "favicon-16x16.png",
        "favicon-32x32.png",
        "favicon.ico",
        "filigran-black.svg",
        "filigran-copilot-light.svg",
        "filigran-copilot.svg",
        "filigran-white.svg",
        "robots.txt",
        "safari-pinned-tab.svg",
        "site.webmanifest",
        "sitemap.xml",
        "sw.js",
        "xtm-one-emblem.svg",
        "xtm-one-emblem-light.svg",
        "xtm-one-icon-192.png",
        "xtm-one-icon-512.png",
        "xtm-one-icon-maskable-512.png",
        "xtm-one.webmanifest",
        "xtp-one-icon.svg",
        "xtp-one-light.svg",
        "xtp-one.svg",
    }
)

# First path segments of the SPA's own routes (``frontend/src/App.tsx``).
# Under a STRIPPING proxy a prefix equal to one of these is ambiguous: the
# public ``/tools/tools/123`` arrives as ``/tools/123``, which rule 2 above
# reads as the forwarded ``/tools/123``. Refused in ``BASE_PATH_MODE=auto``;
# accepted once the operator declares the proxy shape. Pinned against the
# router by ``tests/test_base_path.py``; extend it when a top-level page is
# added.
SPA_FIRST_SEGMENTS: frozenset[str] = frozenset(
    {
        "agent-studio",
        "agents",
        "agents-hub",
        "ai-adoption",
        "briefing",
        "catalog",
        "chat",
        "connections",
        "ctem",
        "dashboard",
        "dashboards",
        "decide",
        "desktop",
        "evaluations",
        "files",
        "fleets",
        "flows",
        "forgot-password",
        "health",
        "inbox",
        "integrations",
        "intents",
        "investigation-packs",
        "knowledge",
        "login",
        "logs",
        "mcp-servers",
        "oauth",
        "objects",
        "observability",
        "operations",
        "presentations",
        "profile",
        "prompts",
        "public",
        "register",
        "reports",
        "reset-password",
        "search",
        "settings",
        "share",
        "skills",
        "tasks",
        "templates",
        "tools",
        "tools-and-integrations",
        "users-and-groups",
        "v",
        "videos",
        "work",
    }
)

BASE_PATH_MODES: tuple[str, ...] = ("auto", "forwarded", "stripped")
"""What the operator declares about the reverse proxy (``BASE_PATH_MODE``).

``auto`` (default): infer the shape of every request (header first, then
path) and refuse at startup a prefix the inference cannot resolve.
``forwarded``: the proxy keeps the prefix - a request under it is the proxy's,
any other is a direct hit; such a prefix may also be a platform page.
``stripped``: the proxy removes the prefix and sends ``X-Forwarded-Prefix``
(Traefik's ``StripPrefix`` does; nginx needs ``proxy_set_header``) - the
header decides, so such a prefix may also be a platform page.
"""

FORWARDED_PREFIX_HEADER = b"x-forwarded-prefix"

BASE_PATH_META_NAME = "base-path"
"""``<meta name>`` the branded ``index.html`` carries the prefix in.

Read once by ``frontend/src/lib/base-path.ts`` before the SPA's first API
call - the prefix cannot come from an API response, because the SPA needs it
to address the API in the first place.
"""


def normalize_base_path(raw: str | None) -> str:
    """Canonical form of a configured base path.

    ``""`` (the root) for blank / ``/`` input; otherwise one leading slash, no
    trailing slash, no duplicate slashes: ``"xtm-one/"`` -> ``"/xtm-one"``,
    ``"//a//b/"`` -> ``"/a/b"``. Raises ``ValueError`` on whitespace, a query
    string or fragment, a ``..`` segment, or a character outside the RFC 3986
    unreserved set (ASCII letters, digits, ``-`` ``_`` ``.`` ``~``) - a prefix
    is a plain path, not a URL and not a percent-encoded one.
    """
    value = (raw or "").strip()
    if not value or value == "/":
        return ""
    if any(ch.isspace() for ch in value):
        raise ValueError("BASE_PATH must not contain whitespace")
    if "?" in value or "#" in value:
        raise ValueError("BASE_PATH is a path prefix and cannot carry a query string or fragment")
    if "://" in value:
        raise ValueError("BASE_PATH is a path prefix (e.g. /xtm-one), not a URL - the URL belongs in BASE_URL")
    segments = [segment for segment in value.split("/") if segment]
    if not segments:
        return ""
    if any(segment in (".", "..") for segment in segments):
        raise ValueError("BASE_PATH must not contain '.' or '..' segments")
    if any(not _SEGMENT_RE.fullmatch(segment) for segment in segments):
        raise ValueError(
            "BASE_PATH may only contain ASCII letters, digits and '-', '_', '.', '~' "
            "between slashes (no percent-encoding, no non-ASCII characters)"
        )
    return "/" + "/".join(segments)


def reserved_segment_problem(base_path: str) -> str | None:
    """Why ``base_path`` cannot be served, or ``None`` when it can."""
    if not base_path:
        return None
    first = base_path.split("/", 2)[1]
    if first in RESERVED_FIRST_SEGMENTS:
        return (
            f"BASE_PATH {base_path!r} starts with '/{first}', a namespace the platform serves "
            "itself; a request could not be told apart from an API call. Pick another prefix."
        )
    return None


def normalize_base_path_mode(raw: str | None) -> str:
    """Canonical ``BASE_PATH_MODE``: one of :data:`BASE_PATH_MODES`, ``auto`` for blank."""
    value = (raw or "").strip().lower()
    if not value:
        return "auto"
    if value not in BASE_PATH_MODES:
        raise ValueError(
            f"BASE_PATH_MODE must be one of {', '.join(BASE_PATH_MODES)}, got {raw!r}"
        )
    return value


def spa_route_problem(base_path: str, mode: str) -> str | None:
    """Why ``base_path`` cannot be served in ``mode``, or ``None`` when it can.

    Only ``auto`` refuses: a prefix whose first segment is a platform page is
    ambiguous under a stripping proxy, and the inference has no way to know
    which proxy is in front. The declared modes resolve the ambiguity.
    """
    if not base_path or mode != "auto":
        return None
    first = base_path.split("/", 2)[1]
    if first in SPA_FIRST_SEGMENTS:
        return (
            f"BASE_PATH {base_path!r} is also the address of a platform page (/{first}): "
            "behind a proxy that strips the prefix, a request for that page could not be "
            "told apart from a request under the prefix. Declare the proxy with "
            "BASE_PATH_MODE=forwarded (it keeps the prefix) or BASE_PATH_MODE=stripped (it "
            "removes it and sends X-Forwarded-Prefix, as Traefik's StripPrefix does), or "
            "pick another prefix."
        )
    return None


def forwarded_prefix(scope: Scope) -> str | None:
    """The prefix an ``X-Forwarded-Prefix`` header says the proxy stripped, canonical.

    ``None`` when the header is absent or is not a plain path. Only its
    equality with the configured prefix is ever consulted, so a foreign or
    forged value changes nothing.
    """
    for name, value in scope.get("headers") or ():
        if name.lower() == FORWARDED_PREFIX_HEADER:
            try:
                return normalize_base_path(value.decode("latin-1"))
            except ValueError:
                return None
    return None


def route_path(scope: Scope) -> str:
    """The request path relative to the application root.

    ``scope["path"]`` minus ``scope["root_path"]`` when the former starts with
    the latter - the same reading Starlette routes on, spelled out here so a
    middleware can key on ``/api/...`` without depending on a private helper.
    A request that does not carry the root path (no base path configured, or
    a scope the middleware never saw) is returned unchanged.
    """
    path: str = scope.get("path", "")
    root: str = scope.get("root_path", "") or ""
    if not root or not path.startswith(root):
        return path
    if path == root:
        # Starlette's own reading is ``""`` here (which is why the bare prefix
        # matches no route and is redirected to the slash form); a middleware
        # keying on ``startswith("/api")`` wants a path, so this is ``/``.
        return "/"
    if path[len(root)] == "/":
        return path[len(root) :]
    return path


def with_base_path(path: str, base_path: str | None = None) -> str:
    """``path`` prefixed with the configured base path.

    For the root-relative paths the backend hands to a browser: redirects,
    the head links written into ``index.html``, the web manifest, the Scalar
    reference's ``openapi_url``. Absolute URLs (``scheme://``, ``//host``) and
    paths that are not root-relative are returned untouched, so a value that
    is sometimes a public URL and sometimes a path can be passed as is.
    ``base_path`` defaults to the configured setting.
    """
    if base_path is None:
        from app.core.config import settings

        base_path = settings.base_path
    if not base_path or not path.startswith("/") or path.startswith("//"):
        return path
    return f"{base_path}{path}"


class BasePathMiddleware:
    """Normalise every HTTP / WebSocket scope to the ASGI mounted-app shape.

    See the module docstring. Register it LAST (``add_middleware`` builds the
    stack inside-out) so it runs before any middleware that reads the path.
    A blank base path makes it a pass-through.
    """

    def __init__(self, app: ASGIApp, *, base_path: str) -> None:
        self.app = app
        self.base_path = normalize_base_path(base_path)

    def was_stripped(self, scope: Scope, path: str) -> bool:
        """Whether ``path`` reached the app without its prefix (module docstring, rules 1-2)."""
        if forwarded_prefix(scope) == self.base_path:
            return True
        prefix = self.base_path
        return not (path == prefix or path.startswith(prefix + "/"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.base_path and scope["type"] in ("http", "websocket"):
            path: str = scope.get("path", "") or "/"
            prefix = self.base_path
            if self.was_stripped(scope, path):
                # The proxy stripped the prefix (or the app is addressed
                # directly on its own port): put it back so the scope has the
                # one shape everything downstream reads.
                scope["path"] = prefix + path
                raw_path = scope.get("raw_path")
                if isinstance(raw_path, bytes):
                    scope["raw_path"] = prefix.encode("ascii") + raw_path
            scope["root_path"] = prefix
        await self.app(scope, receive, send)
