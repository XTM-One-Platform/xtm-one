// SPDX-License-Identifier: Apache-2.0

/**
 * The URL path prefix the platform is served under.
 *
 * `""` at the root - every deployment until now - or `"/xtm-one"` when a
 * reverse proxy mounts the platform at `https://intranet.example.com/xtm-one/`.
 * The backend writes it into the branded `index.html` as
 * `<meta name="base-path" content="/xtm-one">`, and it is read ONCE here,
 * because the SPA needs it before its first request: the API client cannot
 * learn where the API is from the API.
 *
 * Three kinds of URL leave this app, and each has one rule:
 *
 * - **Router navigation** (`<Link to>`, `navigate()`, `useLocation()`) knows
 *   nothing of the prefix: `BrowserRouter` carries it as `basename`, so route
 *   paths stay root-relative everywhere in the app.
 * - **Requests and browser sinks** - `fetch`, `WebSocket`, `EventSource`,
 *   `window.location`, `window.open`, a raw `<a href>` / `<img src>` - take a
 *   root-relative path through {@link withBasePath} at the sink.
 * - **Static asset paths held as data** (`PROVIDER_LOGOS`, a preset's `logo`,
 *   a source type's `icon`) are prefixed where the string is DEFINED, so every
 *   consumer renders them as is. Never prefix such a value a second time: the
 *   helper is deliberately not idempotent (a guard that skipped inputs already
 *   starting with the prefix would silently leave a legitimate `/tools/...`
 *   route unprefixed on a deployment whose prefix happens to be `/tools`).
 *
 * The pure functions are exported for `yarn test:unit` (Node, no DOM) and
 * mirror `normalize_base_path` / `with_base_path` in
 * `backend/app/core/base_path.py`.
 */

/** `<meta name>` the backend writes the prefix in; mirrors `BASE_PATH_META_NAME`.
 */
const BASE_PATH_META_NAME = "base-path";

/**
 * A prefix segment is made of RFC 3986 unreserved characters only - mirrors
 * `_SEGMENT_RE` in the backend. The browser reports `window.location.pathname`
 * percent-encoded, so a prefix holding a character that needs encoding could
 * never be compared against it.
 */
const SEGMENT_RE = /^[A-Za-z0-9._~-]+$/;

/**
 * Canonical form of a prefix: `""` for blank or `/`, otherwise one leading
 * slash, no trailing slash, no duplicate slashes. Anything that is not a plain
 * path (whitespace, a query string, a URL, a percent-encoded or non-ASCII
 * character) reads as the root rather than throwing - a malformed meta tag
 * must never keep the app from booting.
 */
export function normalizeBasePath(raw: string | null | undefined): string {
  const value = (raw ?? "").trim();
  if (!value || value === "/") return "";
  if (/\s/.test(value) || value.includes("?") || value.includes("#") || value.includes("://")) {
    return "";
  }
  const segments = value.split("/").filter(Boolean);
  if (
    segments.length === 0 ||
    segments.some((segment) => segment === "." || segment === ".." || !SEGMENT_RE.test(segment))
  ) {
    return "";
  }
  return `/${segments.join("/")}`;
}

/**
 * `path` under `basePath`. Only a root-relative path (`/api/v1/...`, `/`) is
 * prefixed; an absolute URL (`https://`, `//host`), a relative path, a
 * fragment or `data:` URL is returned untouched, so a value that is sometimes a
 * URL and sometimes a path (a branding logo) can be passed as is.
 */
export function joinBasePath(basePath: string, path: string): string {
  if (!basePath || !path.startsWith("/") || path.startsWith("//")) return path;
  return `${basePath}${path}`;
}

/**
 * `pathname` with `basePath` removed when it is under it: the inverse of
 * {@link joinBasePath}, for reading a full browser pathname (`window.location`,
 * an absolute link pasted into a conversation) back into a route path.
 * A pathname not under the prefix is returned unchanged.
 */
export function stripBasePathFrom(basePath: string, pathname: string): string {
  if (!basePath) return pathname;
  if (pathname === basePath) return "/";
  if (pathname.startsWith(`${basePath}/`)) return pathname.slice(basePath.length);
  return pathname;
}

function readBasePath(): string {
  if (typeof document === "undefined") return "";
  const meta = document.querySelector(`meta[name="${BASE_PATH_META_NAME}"]`);
  return normalizeBasePath(meta?.getAttribute("content"));
}

/** The prefix this page was served under: `""` at the root, else `/xtm-one`. */
export const BASE_PATH: string = readBasePath();

/** {@link joinBasePath} against the page's own prefix. */
export function withBasePath(path: string): string {
  return joinBasePath(BASE_PATH, path);
}

/** {@link stripBasePathFrom} against the page's own prefix. */
export function stripBasePath(pathname: string): string {
  return stripBasePathFrom(BASE_PATH, pathname);
}

/**
 * Absolute URL of a platform route on this origin, for a link handed to
 * someone else (a share dialog, the clipboard): origin + prefix + path.
 */
export function absoluteUrl(path: string): string {
  return `${window.location.origin}${withBasePath(path)}`;
}

/**
 * The platform's own base as this page sees it - origin + prefix, no trailing
 * slash - for a copy-paste URL (a webhook, an MCP endpoint, the widget
 * snippet) when the configured `BASE_URL` is not available. `window.location.origin`
 * alone would drop the prefix.
 */
export function platformOrigin(): string {
  return `${window.location.origin}${BASE_PATH}`;
}
