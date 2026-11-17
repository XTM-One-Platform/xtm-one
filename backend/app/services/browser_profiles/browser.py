# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The profile browser: headless Chromium at a chosen pixel ratio, held to a URL policy.

One launch recipe for every browser that opens a browser profile - a sign-in,
a session check, a keep-alive refresh, an agent's signed-in ``browser`` session
(``agent_sessions.py``) and a Video Studio recording:

* **Native high-resolution frames.** ``--force-device-scale-factor`` makes
  the compositor paint at the pixel ratio, so the CDP screencast delivers
  device pixels (2400x1350 for a 1600x900 viewport at 1.5x) - without it, the
  screencast is the CSS size whatever the context's scale (measured on
  Chromium 153). The context's ``device_scale_factor`` matches it, so media
  queries and ``devicePixelRatio`` agree with what is painted.
* **The URL policy.** Every navigation target is checked with
  ``web_fetch.validate_outbound_url_async`` - public addresses only, the agent
  browser tool's rule - unless the host is one the profile declares AND the
  ``browser_private_targets`` security option is on, in which case the
  operator-network policy applies (private ranges admitted; loopback,
  link-local and cloud metadata still refused). Every redirect hop of every
  request goes through the same check (``browser_tool.RedirectGuard`` with
  this policy), and a route filter holds the first URL of every other request
  (subresource, XHR, frame, worker) to it: non-http(s) schemes and literal
  internal addresses are refused at once, a host name is resolved and checked
  like a navigation target, once per host for the life of the context.
* **The session.** A profile's saved storage state - the cookies of every
  site the context visited (the identity provider's and the application's),
  local storage and IndexedDB, plus the application origin's
  ``sessionStorage`` under ``session_storage`` (Playwright keeps none) - is
  loaded into the context, and read back when the caller saves it. A probe
  opens a second context of the same browser (:meth:`ProfileBrowser.fresh_page`),
  with a saved state or none, under the same policy. A recording opens its
  page with its origin's storage cleared but the session's keys
  (:func:`open_cleared`): cookies are never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.core.security_options import security_option
from app.core.utils import format_exception_message
from app.services.web_fetch import validate_outbound_url_async

logger = logging.getLogger(__name__)

NAVIGATION_TIMEOUT_MS = 45_000
ACTION_TIMEOUT_MS = 15_000
#: The longest side of the frame a profile browser paints, in device pixels: the largest recording viewport (2560)
#: at the largest pixel ratio (2). A recording zoomed in paints the same frame at a higher ratio (``spec.zoomed_window``).
MAX_FRAME_SIDE = 5120

#: Launch flags of the profile browser, the pixel ratio flag added per launch.
PROFILE_CHROMIUM_ARGS: tuple[str, ...] = (
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--hide-scrollbars",
    "--force-color-profile=srgb",
    "--disable-features=Translate,MediaRouter,OptimizationHints",
    "--no-first-run",
    "--mute-audio",
)


class TargetRefusedError(ValueError):
    """A URL the profile browser's policy refuses; ``str()`` is safe to show."""


@dataclass(frozen=True)
class TargetPolicy:
    """Which private hosts a profile's browser may reach (none unless the option is on)."""

    private_hosts: frozenset[str] = field(default_factory=frozenset)

    def admits_private(self, host: str | None) -> bool:
        return bool(host) and host.lower().rstrip(".") in self.private_hosts


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlparse(url).hostname
    except ValueError:
        return None
    return host.lower().rstrip(".") if host else None


def normalise_hosts(values: list[str] | None) -> list[str]:
    """Host names as a profile stores them: lowercase, no scheme, port or path, deduplicated."""
    hosts: list[str] = []
    for value in values or []:
        text = str(value or "").strip().lower()
        if not text:
            continue
        if "://" in text:
            text = urlparse(text).hostname or ""
        text = text.split("/", 1)[0].split(":", 1)[0].rstrip(".")
        if text and text not in hosts and len(text) <= 253:
            hosts.append(text)
    return hosts[:50]


def private_targets_enabled() -> bool:
    return bool(security_option("browser_private_targets"))


def target_policy(*, start_url: str | None, login_url: str | None, allowed_hosts: list[str] | None) -> TargetPolicy:
    """The policy of one profile (or of a recording without one)."""
    if not private_targets_enabled():
        return TargetPolicy()
    hosts = set(normalise_hosts(allowed_hosts))
    for url in (start_url, login_url):
        host = _host_of(url)
        if host:
            hosts.add(host)
    return TargetPolicy(frozenset(hosts))


async def check_target(url: str, policy: TargetPolicy) -> str:
    """The URL as the policy admits it, or :class:`TargetRefusedError`."""
    host = _host_of(url)
    try:
        return await validate_outbound_url_async(url, allow_private=policy.admits_private(host))
    except ValueError as exc:
        hint = ""
        if not policy.private_hosts:
            hint = (
                " Browser profiles reach public addresses only; an administrator can allow an application "
                "on a private network (Settings > Access and security > Browser profiles on private networks), "
                "for the hosts the profile declares."
            )
        raise TargetRefusedError(f"{format_exception_message(exc)}.{hint}") from None


def _blocked_by_route(url: str, policy: TargetPolicy) -> bool:
    """The route filter's cheap check: schemes, and literal internal addresses."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return True
    scheme = (parsed.scheme or "").lower()
    if scheme in ("data", "blob", "about"):
        return False
    if scheme not in ("http", "https"):
        return True
    host = (parsed.hostname or "").lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # a name: RouteFilter resolves it
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_global:
        return False
    return not (policy.admits_private(host) and ip.is_private and not (ip.is_loopback or ip.is_link_local))


def _named_host(url: str) -> str | None:
    """The host of an http(s) URL when it is a name, ``None`` for a literal address or any other URL."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if (parsed.scheme or "").lower() not in ("http", "https") or not host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    return None


class RouteFilter:
    """The route filter of one browser context.

    Playwright runs it for the first URL of every request; the redirect guard
    holds the hops after it. Schemes and literal addresses are decided at once
    (:func:`_blocked_by_route`); a host name is resolved and checked by
    :func:`check_target`, the navigation check, once per host for the life of
    the context - concurrent requests to a host share the one resolution. A
    check that fails for any reason refuses the host (fails closed).
    """

    def __init__(self, policy: TargetPolicy) -> None:
        self.policy = policy
        self._hosts: dict[str, asyncio.Future[bool]] = {}

    async def admits(self, url: str) -> bool:
        if _blocked_by_route(url, self.policy):
            return False
        host = _named_host(url)
        if host is None:
            return True
        verdict = self._hosts.get(host)
        if verdict is None:
            verdict = self._hosts[host] = asyncio.ensure_future(self._check(url))
        return await asyncio.shield(verdict)

    async def _check(self, url: str) -> bool:
        try:
            await check_target(url, self.policy)
        except Exception as exc:
            logger.debug("Profile browser request refused by the URL policy: %s", format_exception_message(exc))
            return False
        return True


#: The key of a saved state holding ``sessionStorage`` by origin (Playwright's state has none).
SESSION_STORAGE_KEY = "session_storage"

_READ_SESSION_STORAGE_JS = """
() => {
  const items = {};
  for (let i = 0; i < sessionStorage.length; i++) {
    const key = sessionStorage.key(i);
    items[key] = sessionStorage.getItem(key);
  }
  return items;
}
"""

#: The page's ``localStorage``, read on the page itself: Playwright's state leaves out an origin it read empty or
#: could not read (a frame mid-navigation), and an application keeping its bearer token there then reopens signed out.
_READ_LOCAL_STORAGE_JS = """
() => {
  const items = {};
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key !== null) items[key] = localStorage.getItem(key) ?? '';
  }
  return items;
}
"""


def with_local_storage(state: dict, origin: str, items: dict[str, str]) -> dict:
    """*state* whose *origin* entry holds every key of *items* (read on the page) it lacks; *state* unchanged when
    it held them all. The values Playwright read win: the page read only fills what its read left out."""
    origin = origin_of(origin)
    if not origin or not items:
        return state
    origins = [o for o in state.get("origins") or [] if isinstance(o, dict)]
    entry = next((o for o in origins if origin_of(str(o.get("origin") or "")) == origin), None)
    held = {str(i.get("name")) for i in (entry or {}).get("localStorage") or [] if isinstance(i, dict)}
    missing = [{"name": str(k), "value": str(v)} for k, v in items.items() if str(k) not in held]
    if not missing:
        return state
    if entry is None:
        return {**state, "origins": [*origins, {"origin": origin, "localStorage": missing}]}
    merged = {**entry, "localStorage": [*(entry.get("localStorage") or []), *missing]}
    return {**state, "origins": [merged if o is entry else o for o in origins]}


def local_storage_keys(state: dict | None, origin: str) -> list[str]:
    """The ``localStorage`` key names *state* holds for *origin* (an origin or a URL of it; names only), sorted."""
    origin = origin_of(origin)
    names: set[str] = set()
    for o in (state or {}).get("origins") or []:
        if isinstance(o, dict) and origin_of(str(o.get("origin") or "")) == origin:
            names.update(str(i.get("name")) for i in o.get("localStorage") or [] if isinstance(i, dict))
    return sorted(names)


def persistence_note(state: dict | None, start_url: str, *, page_keys: list[str] | None = None) -> str:
    """What a saved *state* keeps of the application's session, for a session that does not reopen signed in:
    ``session not persisted (no cookie of app.example; held on the page but not saved: token)``. *page_keys* are
    the keys the page held when it rendered signed in, named when the state lost them; a state that kept them all
    (a session in ``localStorage`` alone, without any cookie) is said saved. Names only, never a value."""
    host = (urlparse(start_url or "").hostname or "").lower()
    domains = [
        str(c.get("domain") or "").lstrip(".").lower()
        for c in (state or {}).get("cookies") or []
        if isinstance(c, dict)
    ]
    cookies = [d for d in domains if host and d and (d == host or host.endswith("." + d))]
    kept = local_storage_keys(state, origin_of(start_url))
    parts = [f"{len(cookies)} cookie(s) of {host}" if cookies else f"no cookie of {host}"]
    parts.append(f"localStorage keys: {', '.join(kept[:10])}" if kept else "no localStorage key")
    lost = sorted(set(page_keys or []) - set(kept))
    if lost:
        parts.append(f"held on the page but not saved: {', '.join(lost[:10])}")
    elif kept:
        # A session kept in localStorage alone (a token key) is persisted: no cookie is needed.
        return f"session saved ({'; '.join(parts)}), yet it does not reopen signed in"
    return f"session not persisted ({'; '.join(parts)})"


def origin_of(url: str | None) -> str:
    """``scheme://host[:port]`` as ``location.origin`` spells it (default ports dropped)."""
    try:
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return ""
    if not parsed.scheme or not host:
        return ""
    default = {"http": 80, "https": 443}.get(parsed.scheme.lower())
    netloc = host if port in (None, default) else f"{host}:{port}"
    return f"{parsed.scheme.lower()}://{netloc}"


def split_storage_state(state: dict | None) -> tuple[dict | None, dict[str, dict[str, str]]]:
    """Playwright's part of a saved state, and its ``sessionStorage`` by origin."""
    if not state:
        return None, {}
    playwright_state = {k: v for k, v in state.items() if k != SESSION_STORAGE_KEY}
    raw = state.get(SESSION_STORAGE_KEY)
    session: dict[str, dict[str, str]] = {}
    if isinstance(raw, dict):
        for origin, items in raw.items():
            if isinstance(items, dict):
                session[str(origin)] = {str(k): str(v) for k, v in items.items()}
    return playwright_state, session


#: Headless Chromium has no authenticator, so a WebAuthn ceremony
#: (``navigator.credentials.get``) never completes and an identity provider that
#: defaults to a passkey / FIDO key sits forever on its bridge page. This init
#: script makes the call reject at once (as a real browser without a key would,
#: ``NotAllowedError``) and counts the attempts, so the sign-in sees the method
#: is unusable and backs out to a code method instead of waiting. It is a
#: web standard, not a vendor hook: it applies to every profile browser context.
_BLOCK_WEBAUTHN_JS = """
(() => {
  try {
    const state = (window.__xtmWebauthn = window.__xtmWebauthn || { attempts: 0 });
    const creds = navigator.credentials;
    if (!creds || typeof creds.get !== 'function') return;
    const fail = function () {
      state.attempts += 1;
      return Promise.reject(new DOMException('No authenticator is available in this browser.', 'NotAllowedError'));
    };
    try { Object.defineProperty(creds, 'get', { configurable: true, writable: true, value: fail }); }
    catch (e) { creds.get = fail; }
  } catch (e) {}
})();
"""


def _session_storage_script(session: dict[str, dict[str, str]]) -> str:
    """An init script putting back each origin's ``sessionStorage`` keys the page does not hold yet."""
    return (
        "(() => { const all = " + json.dumps(session) + "; const items = all[location.origin];"
        " if (!items) return; try { for (const [k, v] of Object.entries(items))"
        " if (sessionStorage.getItem(k) === null) sessionStorage.setItem(k, v); } catch (e) {} })();"
    )


#: The IndexedDB databases of the page's origin, named so a new document deletes them before it opens one.
_DATABASES_JS = """async () => {
  try { return indexedDB.databases ? (await indexedDB.databases()).map((d) => d.name).filter(Boolean) : []; }
  catch (e) { return null; }
}"""
#: Clears, at the start of a document of ``plan.origin`` and before its scripts run, its ``localStorage`` and
#: ``sessionStorage`` but the keys of ``plan.keep``, and deletes its IndexedDB databases (``plan.databases``, or all
#: it lists when ``null``); what it removed is kept as ``window.__xtmCleared``.
_CLEAR_AT_START_JS = """(() => {
  const plan = __PLAN__;
  if (window.top !== window || location.origin !== plan.origin) return;
  const kept = new Set(plan.keep);
  const said = { local: [], session: [], databases: [], kept: [] };
  const clear = (store, gone) => {
    try {
      const held = Array.from({ length: store.length }, (_, i) => store.key(i)).filter((key) => key !== null);
      for (const key of held) {
        if (kept.has(key)) { if (!said.kept.includes(key)) said.kept.push(key); continue; }
        store.removeItem(key);
        gone.push(key);
      }
    } catch (e) {}
  };
  if (plan.local) clear(localStorage, said.local);
  if (plan.session) clear(sessionStorage, said.session);
  const drop = (name) => { try { indexedDB.deleteDatabase(name); said.databases.push(name); } catch (e) {} };
  if (plan.indexed_db && Array.isArray(plan.databases)) plan.databases.forEach(drop);
  else if (plan.indexed_db && indexedDB.databases) indexedDB.databases().then((all) => all.forEach((d) => d.name && drop(d.name)), () => {});
  Object.defineProperty(window, '__xtmCleared', { value: said, configurable: true });
})()"""
_CLEARED_JS = "() => window.__xtmCleared || null"

#: Writes, at the start of a top document of ``plan.origin`` and before its scripts run, the ``localStorage`` keys of
#: ``plan.values``: an application's own preference (its theme) read by its pre-paint script as if the user had set it.
_SEED_AT_START_JS = """(() => {
  const plan = __PLAN__;
  if (window.top !== window || location.origin !== plan.origin) return;
  for (const [key, value] of Object.entries(plan.values)) {
    try { localStorage.setItem(key, value); } catch (e) {}
  }
})()"""


def seed_storage_script(origin: str, values: dict[str, str]) -> str:
    """The init script writing *values* into *origin*'s ``localStorage`` at each of its documents' start.

    Registered before any page state reset's (:func:`open_cleared`, which runs after it), so a reset keeping these
    keys leaves them as seeded and one that does not removes them."""
    return _SEED_AT_START_JS.replace("__PLAN__", json.dumps({"origin": origin, "values": dict(values)}))


@dataclass(frozen=True)
class StorageReset:
    """What :func:`open_cleared` clears of the page's origin: never a cookie, never a key of :attr:`keep`."""

    local_storage: bool = True
    session_storage: bool = True
    indexed_db: bool = False
    keep: tuple[str, ...] = ()


async def open_cleared(rb: ProfileBrowser, url: str, reset: StorageReset, *, reload: bool = False) -> dict:
    """Open *url* in *rb*'s page - or, *reload*, the page again at the address it is on - its origin's storage cleared
    at the new document's start: before any of its scripts reads it, after what the page left on unload, and
    after the session's ``sessionStorage`` was put back (:func:`_session_storage_script`, which a cleared
    ``sessionStorage`` then no longer puts back on the pages after). Cookies are never touched.

    ``{origin, local_storage, session_storage, indexed_db, kept}``: the origin cleared, the keys and databases removed,
    the kept keys it held; ``unread: true`` when the new document could not say (another origin, a frame)."""
    page = rb.page
    origin = origin_of(page.url if reload else url)
    databases = None
    if reset.indexed_db and origin_of(page.url) == origin:
        with contextlib.suppress(Exception):  # a page navigating says nothing: the new document lists them itself
            listed = await page.evaluate(_DATABASES_JS)
            databases = [str(name) for name in listed] if isinstance(listed, list) else None
    plan = {
        "origin": origin,
        "local": reset.local_storage,
        "session": reset.session_storage,
        "indexed_db": reset.indexed_db,
        "keep": list(reset.keep),
        "databases": databases,
    }
    armed = await rb.context.add_init_script(_CLEAR_AT_START_JS.replace("__PLAN__", json.dumps(plan)))
    try:
        if reload:
            await page.reload(wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        else:
            await rb.goto(url)
    finally:
        with contextlib.suppress(Exception):  # the context may be closing
            await armed.dispose()
    if reset.session_storage and rb.session_restore is not None:
        with contextlib.suppress(Exception):
            await rb.session_restore.dispose()
        rb.session_restore = None
    said = None
    with contextlib.suppress(Exception):  # navigating on: read as unknown
        said = await page.evaluate(_CLEARED_JS)
    if not isinstance(said, dict):
        return {"origin": origin, "local_storage": [], "session_storage": [], "indexed_db": [], "kept": [], "unread": True}
    return {
        "origin": origin,
        "local_storage": [str(key) for key in said.get("local") or []],
        "session_storage": [str(key) for key in said.get("session") or []],
        "indexed_db": [str(name) for name in said.get("databases") or []],
        "kept": [str(key) for key in said.get("kept") or []],
    }


@dataclass
class ProfileBrowser:
    pw: Any
    browser: Any
    context: Any
    page: Any
    policy: TargetPolicy
    redirects: Any
    #: What a second context of this browser is opened with (viewport, scale, locale...).
    context_options: dict = field(default_factory=dict)
    #: Per-browser memo of the probes (the start URL rendered without a session).
    memo: dict = field(default_factory=dict)
    #: The init script putting the session's ``sessionStorage`` back (:func:`_session_storage_script`), disposed once a
    #: page state reset cleared it (:func:`open_cleared`); ``None``: none.
    session_restore: Any = None

    async def goto(self, url: str, *, page: Any = None, wait_until: str = "domcontentloaded") -> None:
        safe = await check_target(url, self.policy)
        await (page or self.page).goto(safe, wait_until=wait_until, timeout=NAVIGATION_TIMEOUT_MS)

    async def storage_state(self, *, session_storage_of: str | None = None) -> dict:
        """The context's state, with *session_storage_of*'s ``sessionStorage`` when the page is on that origin."""
        try:
            state = await self.context.storage_state(indexed_db=True)
        except TypeError:  # an older Playwright
            state = await self.context.storage_state()
        if session_storage_of and origin_of(self.page.url) == session_storage_of:
            items = None
            with contextlib.suppress(Exception):  # a page navigating keeps what it had
                items = await self.page.evaluate(_READ_SESSION_STORAGE_JS)
            if items:
                state[SESSION_STORAGE_KEY] = {session_storage_of: {str(k): str(v) for k, v in items.items()}}
            local = None
            with contextlib.suppress(Exception):  # a page navigating keeps what Playwright read
                local = await self.page.evaluate(_READ_LOCAL_STORAGE_JS)
            if isinstance(local, dict) and local:
                state = with_local_storage(state, session_storage_of, {str(k): str(v) for k, v in local.items()})
        return state

    async def page_local_storage_keys(self) -> list[str]:
        """The ``localStorage`` key names of the page's own origin (names only); ``[]`` when it cannot be read."""
        with contextlib.suppress(Exception):  # navigating: nothing known
            local = await self.page.evaluate(_READ_LOCAL_STORAGE_JS)
            if isinstance(local, dict):
                return sorted(str(k) for k in local)
        return []

    @asynccontextmanager
    async def fresh_page(self, storage_state: dict | None = None) -> AsyncIterator[Any]:
        """A page in a new context of this browser, holding *storage_state* (or nothing), under the same policy."""
        context, _restore = await _new_context(self.browser, self.policy, storage_state=storage_state, **self.context_options)
        try:
            page = await context.new_page()
            page.set_default_timeout(ACTION_TIMEOUT_MS)
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
            yield page
        finally:
            with contextlib.suppress(Exception):  # the browser may be closing
                await context.close()


def browser_options(options: dict | None) -> dict:
    """A profile's ``browser_options`` with the defaults filled in."""
    opts = dict(options or {})
    return {
        "color_scheme": opts.get("color_scheme") if opts.get("color_scheme") in ("light", "dark") else None,
        "locale": str(opts.get("locale") or "en-US")[:20],
        "timezone": str(opts.get("timezone") or "UTC")[:60],
    }


async def _new_context(
    browser: Any,
    policy: TargetPolicy,
    *,
    storage_state: dict | None,
    viewport: tuple[int, int],
    ratio: float,
    color_scheme: str | None,
    locale: str,
    timezone: str,
    user_agent: str,
) -> tuple[Any, Any]:
    """A context of *browser* holding *storage_state*, its requests held to *policy* by the route filter, and the init
    script putting its ``sessionStorage`` back (``None`` when it holds none)."""
    playwright_state, session_storage = split_storage_state(storage_state)
    context = await browser.new_context(
        viewport={"width": int(viewport[0]), "height": int(viewport[1])},
        device_scale_factor=ratio,
        storage_state=playwright_state or None,
        color_scheme=color_scheme,  # type: ignore[arg-type]
        locale=locale,
        timezone_id=timezone,
        user_agent=user_agent,
        accept_downloads=False,
    )
    await context.add_init_script(_BLOCK_WEBAUTHN_JS)
    restore = await context.add_init_script(_session_storage_script(session_storage)) if session_storage else None

    route_filter = RouteFilter(policy)

    async def _route(route: Any) -> None:
        if not await route_filter.admits(route.request.url):
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    await context.route("**/*", _route)
    return context, restore


@asynccontextmanager
async def profile_browser(
    *,
    policy: TargetPolicy,
    viewport: tuple[int, int] = (1600, 900),
    pixel_ratio: float = 1.5,
    storage_state: dict | None = None,
    color_scheme: str | None = None,
    locale: str = "en-US",
    timezone: str = "UTC",
    extra_args: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> AsyncIterator[ProfileBrowser]:
    """A launched profile browser and its one page, closed on exit whatever happened; *extra_args* join its launch flags
    (a recording's GL backend, ``capture_rate.gl_backend``) and *env*, when given, is its environment
    (``capture_rate.launch_env``)."""
    from playwright.async_api import async_playwright

    from app.services.browser_tool import RedirectGuard

    async def _validate(url: str) -> str:
        return await check_target(url, policy)

    pw = await async_playwright().start()
    browser = None
    redirects = RedirectGuard(validator=_validate)
    try:
        ratio = max(1.0, min(float(pixel_ratio), MAX_FRAME_SIDE / max(int(viewport[0]), int(viewport[1]), 1)))
        browser = await pw.chromium.launch(
            headless=True, args=[*PROFILE_CHROMIUM_ARGS, *extra_args, f"--force-device-scale-factor={ratio:g}"], env=env
        )
        await redirects.attach(browser)
        # A real Chrome user agent: identity providers refuse "HeadlessChrome".
        version = browser.version
        options = {
            "viewport": (int(viewport[0]), int(viewport[1])),
            "ratio": ratio,
            "color_scheme": color_scheme,
            "locale": locale,
            "timezone": timezone,
            "user_agent": (
                f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"
            ),
        }
        context, restore = await _new_context(browser, policy, storage_state=storage_state, **options)
        page = await context.new_page()
        await redirects.watch_main_frame(context, page)
        page.set_default_timeout(ACTION_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        yield ProfileBrowser(
            pw=pw,
            browser=browser,
            context=context,
            page=page,
            policy=policy,
            redirects=redirects,
            context_options=options,
            session_restore=restore,
        )
    finally:
        redirects.close()
        for close in (getattr(browser, "close", None), getattr(pw, "stop", None)):
            if close is None:
                continue
            try:
                await close()
            except Exception as exc:
                logger.debug("Profile browser cleanup failed: %s", format_exception_message(exc))
