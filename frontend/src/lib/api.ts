// SPDX-License-Identifier: Apache-2.0

/** API client for XTM One backend. */

import { stripBasePath, withBasePath } from "@/lib/base-path";
import { isPublicDashboardPath } from "@/lib/public-dashboard";
import {
  DESKTOP_FEATURE_SESSION,
  desktopSessionAtBoot,
  desktopSupports,
  loadDesktopSession,
  storeDesktopSession,
} from "@/lib/desktop";
import { errorDetailMessage } from "@/lib/error-detail";
import { isPublicDeckPath } from "@/lib/public-deck";
import { isPublicVideoPath } from "@/lib/public-video";
import { APP_LOCKED_EVENT, appLockCode, isAppLockEnded } from "@/lib/app-lock";
import { parseRetryAfterSeconds } from "@/lib/retry-after";
import {
  SESSION_RETURN_PATH_KEY,
  sessionEndReason,
  sessionEndedLoginPath,
  type SessionEndReason,
} from "@/lib/session";
import type { DesktopSession } from "@/types";

/** `/api/v1` under the URL path prefix the platform is served from. */
export const API_BASE = withBasePath("/api/v1");

/**
 * Thrown when the backend server is unreachable — either a direct network error
 * (TypeError from fetch) or a gateway / proxy error (HTTP 502, 503, 504).
 *
 * The ErrorBoundary uses this to show the amber "server unavailable" card
 * instead of the generic red "something went wrong" card.
 */
export class BackendUnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BackendUnavailableError";
  }
}

/** Start of the 403 the Copilot-mode licence wall (`LicenseGateMiddleware`,
 *  `backend/app/main.py`) answers on every `/api` call without a licence. */
const LICENCE_WALL_DETAIL_PREFIX = "Enterprise license required";

/**
 * True for the Copilot-mode licence wall: `throwForErrorResponse` turns that
 * 403 into a {@link BackendUnavailableError} (retryable, no `status`), so a
 * caller that hides an optional licensed layer on 403 must check this too.
 */
export function isLicenceWallError(error: unknown): boolean {
  return error instanceof BackendUnavailableError && error.message.startsWith(LICENCE_WALL_DETAIL_PREFIX);
}

const TOKEN_KEY = "copilot_token";
const ADMIN_TOKEN_KEY = "copilot_admin_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

function storedSession(): DesktopSession {
  return { token: getToken(), admin_token: localStorage.getItem(ADMIN_TOKEN_KEY) };
}

function writeStoredSession(session: DesktopSession) {
  for (const [key, value] of [
    [TOKEN_KEY, session.token],
    [ADMIN_TOKEN_KEY, session.admin_token],
  ] as const) {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  }
}

/**
 * Every change of the session goes through here: inside XTM One Desktop the
 * shell keeps the session as it now stands, since the webview's own
 * storage can roll back.
 */
function changeSession(change: () => void) {
  change();
  if (desktopSupports(DESKTOP_FEATURE_SESSION)) storeDesktopSession(storedSession());
}

/**
 * At launch inside XTM One Desktop, before anything reads the token: the
 * session the shell kept wins over the page's storage. A shell that
 * keeps none yet takes the page's. Never rejects.
 */
export async function restoreDesktopSession(): Promise<void> {
  if (!desktopSupports(DESKTOP_FEATURE_SESSION)) return;
  try {
    const boot = desktopSessionAtBoot(await loadDesktopSession(), storedSession());
    if (boot.kind === "adopt") writeStoredSession(boot.session);
    else if (boot.kind === "hand_over") storeDesktopSession(boot.session);
  } catch {
    // Storage disabled: the page goes on with what it has.
  }
}

export function setToken(token: string) {
  changeSession(() => localStorage.setItem(TOKEN_KEY, token));
}

function clearToken() {
  changeSession(() => localStorage.removeItem(TOKEN_KEY));
}

// --- Impersonation token management ---

/** Whether the current session is an admin impersonating another user. */
export function isImpersonating(): boolean {
  return !!localStorage.getItem(ADMIN_TOKEN_KEY);
}

/** Save the admin token and swap in the impersonation token. */
export function startImpersonation(impersonationToken: string) {
  changeSession(() => {
    const adminToken = getToken();
    if (adminToken) {
      localStorage.setItem(ADMIN_TOKEN_KEY, adminToken);
    }
    localStorage.setItem(TOKEN_KEY, impersonationToken);
  });
}

/** Restore the original admin token, ending the impersonation session. */
export function stopImpersonation() {
  changeSession(() => {
    const adminToken = localStorage.getItem(ADMIN_TOKEN_KEY);
    if (adminToken) {
      localStorage.setItem(TOKEN_KEY, adminToken);
    }
    localStorage.removeItem(ADMIN_TOKEN_KEY);
  });
}

/** Clear all tokens including impersonation backup (used on full logout). */
export function clearAllTokens() {
  changeSession(() => writeStoredSession({ token: null, admin_token: null }));
}

/** One-shot latch: the requests and streams refused at once sign out once. */
let sessionEndIssued = false;

/**
 * The session ended: idle too long, or past its absolute lifetime.
 * Sign out - an impersonation ends with the administrator's session it was
 * started from - and open the sign-in page saying why, remembering the page to
 * come back to. No identity provider is opened on our own: with SSO, the person
 * chooses to sign in again, so an unattended browser is not signed back in.
 */
export function endSession(reason: SessionEndReason): void {
  if (sessionEndIssued) return;
  sessionEndIssued = true;
  try {
    const { search, hash } = window.location;
    sessionStorage.setItem(SESSION_RETURN_PATH_KEY, `${stripBasePath(window.location.pathname)}${search}${hash}`);
  } catch {
    // Storage disabled: the person lands on the home page after signing in.
  }
  clearAllTokens();
  window.location.href = withBasePath(sessionEndedLoginPath(reason));
}

/** Fetch wrapper that translates direct network errors (backend completely
 *  unreachable, no proxy) into BackendUnavailableError. */
async function doFetch(path: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(`${API_BASE}${path}`, init);
  } catch (err) {
    throw new BackendUnavailableError(
      err instanceof Error ? err.message : "Failed to connect to server",
    );
  }
}

/** Stable prefix of the Terms-of-Use 403 `detail`, mirroring
 *  `TERMS_OF_USE_REQUIRED_DETAIL` in `backend/app/api/deps.py`. It is the only
 *  thing that distinguishes this 403 from any other; changing the backend
 *  wording means changing this constant in the same commit. */
const TERMS_OF_USE_REQUIRED_PREFIX = "Terms of Use acceptance required";

/** One-shot latch for the Terms-of-Use reload below. */
let termsReloadIssued = false;

/** Shared non-2xx handling for every API helper (request/postForm/download)
 *  so gateway outages, the license gate, and session expiry behave
 *  identically across the app. Always throws. */
async function throwForErrorResponse(response: Response, token: string | null): Promise<never> {
  // Gateway / proxy errors when backend is down (unambiguous).
  if (response.status === 502 || response.status === 504) {
    throw new BackendUnavailableError(
      `Server unavailable (HTTP ${response.status})`,
    );
  }

  // 503 is ambiguous: a proxy says it with no body, but the AI toggle says it
  // with `detail: {code: "ai_provisioning_busy", ...}` when another replica is
  // seeding. Collapsing both into BackendUnavailableError made that message
  // unreachable *and* had TanStack retry a deliberate answer as an outage
  // (/code-review, 2026-08-28). A structured body wins; anything else is still
  // treated as the gateway being down.
  if (response.status === 503) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    if (detail && typeof detail === "object" && typeof detail.code === "string") {
      throw Object.assign(
        new Error(
          typeof detail.message === "string" ? detail.message : detail.code,
        ),
        { status: 503, detail },
      );
    }
    throw new BackendUnavailableError("Server unavailable (HTTP 503)");
  }

  // License gate may transiently block during startup or cache refresh.
  // Treat as a retryable server error so TanStack Query retries and the
  // ErrorBoundary shows "server unavailable" instead of a hard crash.
  if (response.status === 403) {
    const body = await response.json().catch(() => ({ detail: "Forbidden" }));
    if (typeof body.detail === "string" && body.detail.includes(LICENCE_WALL_DETAIL_PREFIX)) {
      throw new BackendUnavailableError(body.detail);
    }
    // Terms of Use gate. On a normal boot this never fires: `/auth/me`
    // is exempt server-side, so `TermsGate` renders the consent screen before
    // any other query mounts. It only fires for a tab that was already open
    // when a new ToU version was published — reload once so the app re-boots
    // into the gate instead of showing an error boundary. The module-level
    // latch makes the reload strictly one-shot: several in-flight queries all
    // 403 at once, and a reload that somehow lands back here must not loop.
    if (typeof body.detail === "string" && body.detail.startsWith(TERMS_OF_USE_REQUIRED_PREFIX)) {
      if (!termsReloadIssued) {
        termsReloadIssued = true;
        window.location.reload();
      }
      throw new Error(body.detail);
    }
    // Structured, same as the generic branch below. Refusals carry
    // ``detail: {code, message}`` — ``new Error(object)`` prints
    // "[object Object]" and drops the code callers are meant to branch on,
    // which is what surfaced the AI-disabled refusal as a bare "Error" toast.
    const forbiddenMessage =
      typeof body.detail === "string"
        ? body.detail
        : (body.detail && typeof body.detail.message === "string"
            ? body.detail.message
            : "Forbidden");
    throw Object.assign(new Error(forbiddenMessage), {
      status: 403,
      detail: body.detail,
    });
  }

  // The companion is locked behind its PIN (or must choose one first): the
  // lock gate shows its screen; nothing else of the app is shown meanwhile.
  if (response.status === 423) {
    const body = await response.json().catch(() => null);
    const code = appLockCode(body?.detail);
    if (code) window.dispatchEvent(new CustomEvent(APP_LOCKED_EVENT, { detail: { code } }));
    throw Object.assign(new Error("Locked"), { status: 423, detail: body?.detail });
  }

  if (response.status === 401) {
    // Only redirect to login if the user had a token (session expired).
    // If there's no token we're likely on the login page itself — just
    // propagate the API error so the form can display it.
    if (token) {
      // A public deck, video or dashboard page is read without an account:
      // the dead session left in this browser is dropped, its visitor stays.
      const page = stripBasePath(window.location.pathname);
      // Every branch below keeps `status: 401` on the error, so a caller can
      // tell a session that ended from a transport failure and stop retrying
      // (the slide editor's autosave).
      if (isPublicDeckPath(page) || isPublicVideoPath(page) || isPublicDashboardPath(page)) {
        clearToken();
        throw Object.assign(new Error("Unauthorized"), { status: 401 });
      }
      const body = await response.json().catch(() => null);
      // Too many wrong PINs on the companion ended this session.
      const ended = isAppLockEnded(body?.detail) ? "pin" : sessionEndReason(body?.detail);
      if (ended) {
        endSession(ended);
        throw Object.assign(
          new Error(typeof body?.detail?.message === "string" ? body.detail.message : "Session expired"),
          { status: 401 },
        );
      }
      // If impersonating and the impersonation token expired, restore the
      // admin token instead of forcing a full logout.
      if (isImpersonating()) {
        stopImpersonation();
        window.location.href = withBasePath("/");
        throw Object.assign(new Error("Impersonation session expired"), { status: 401 });
      }
      clearToken();
      window.location.href = withBasePath("/login");
      throw Object.assign(new Error("Unauthorized"), { status: 401 });
    }
    const error = await response.json().catch(() => ({ detail: "Invalid credentials" }));
    throw new Error(error.detail || "Invalid credentials");
  }

  const error = await response.json().catch(() => ({ detail: "Request failed" }));
  // Carry the structured payload: callers key off `status` (409 optimistic
  // lock) and `detail` (422 node-addressed issues) — a bare string message
  // loses both, and `new Error(object)` prints "[object Object]".
  const message = errorDetailMessage(error.detail, response.status);
  // `Retry-After` (a 429 in particular) so a caller can back off as asked.
  const retryAfter = parseRetryAfterSeconds(response.headers.get("Retry-After"));
  throw Object.assign(new Error(message), {
    status: response.status,
    detail: error.detail,
    ...(retryAfter !== undefined ? { retryAfter } : {}),
  });
}

/**
 * Was this a 404, as opposed to a transport failure, a 5xx or a 4xx that means
 * something else?
 *
 * Lives here because `throwForErrorResponse` above is what attaches `status`,
 * and a caller reaching into that shape by hand would silently start matching
 * nothing if the property were ever renamed.
 *
 * The reason to ask: the app runs with `throwOnError: true` (main.tsx) so that
 * no failed query hides behind a skeleton. For an endpoint whose 404 is an
 * ordinary answer rather than a fault — "this integration is not an outbound
 * registration", "this capability is off for this deployment" — that default
 * turns a normal state into an ErrorBoundary crash. Such a query opts out with
 * `throwOnError: (e) => !isNotFoundError(e)`, which keeps 5xx and network
 * failures throwing; a blanket `throwOnError: false` would render a broken API
 * as a confidently empty page instead.
 */
export function isNotFoundError(error: unknown): boolean {
  return (error as { status?: number } | null)?.status === 404;
}

/**
 * Query options for endpoints whose 404 is an ordinary answer (see
 * `isNotFoundError` above): no retry, and only non-404 failures reach the
 * ErrorBoundary. Spread into the query: `...notFoundIsOrdinary`.
 */
export const notFoundIsOrdinary = {
  retry: false,
  throwOnError: (error: Error, query?: QueryOnScreen) =>
    !isNotFoundError(error) && !isOutageOverDataOnScreen(error, query),
} as const;

/** What a query's `throwOnError` reads of the query: whether it has data on screen. */
export interface QueryOnScreen {
  state: { data: unknown };
}

/**
 * An unreachable backend (a restart, a deploy) met by a read whose data is
 * already on screen: never thrown to an error boundary, the page stays and
 * catches up once the backend answers (#10910).
 */
export function isOutageOverDataOnScreen(error: unknown, query: QueryOnScreen | undefined): boolean {
  if (query?.state.data === undefined) return false;
  if (error instanceof BackendUnavailableError || error instanceof TypeError) return true;
  return error instanceof Error && /fetch|network|ECONNREFUSED/i.test(error.message);
}

/**
 * Query options for endpoints whose 404 **and 403** are ordinary answers.
 *
 * A gated surface answers 403 both when the deployment does not offer it and
 * when the viewer may not manage it — supported states, not broken ones, and
 * tolerating only 404 made each replace the page with the ErrorBoundary. It
 * arrives on licensed deployments too: `useChatSendEnabled()` reports `true`
 * while the config is still resolving.
 */
export const unavailableIsOrdinary = {
  retry: false,
  throwOnError: (error: Error, query?: QueryOnScreen) => {
    const status = (error as { status?: number } | null)?.status;
    return status !== 404 && status !== 403 && !isOutageOverDataOnScreen(error, query);
  },
} as const;

/**
 * Pull the filename out of a `Content-Disposition` header value.
 *
 * Prefers the RFC 5987 `filename*=UTF-8''…` form and falls back to the plain
 * quoted `filename="…"`, so a server that sends both (or a percent-encoding
 * that fails to decode) still yields a usable name. Returns `null` when the
 * header is absent or carries neither form — callers supply their own
 * fallback name in that case.
 *
 * Exported because `api.download()` is not the only caller: an endpoint whose
 * error semantics differ from `throwForErrorResponse` still fetches by hand,
 * and must not re-implement this regex pair.
 */
export function filenameFromContentDisposition(
  disposition: string | null,
): string | null {
  const header = disposition ?? "";
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  const quoted = /filename="([^"]+)"/i.exec(header);
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1]);
    } catch {
      return quoted?.[1] ?? null;
    }
  }
  return quoted?.[1] ?? null;
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const response = await doFetch(path, { ...options, headers });
  if (!response.ok) {
    await throwForErrorResponse(response, token);
  }

  if (response.status === 204) return undefined as T;
  return response.json();
}

/**
 * `request()` with an EXPLICIT bearer instead of the stored session - for the
 * one flow that holds a credential it must not store: the XTM One Desktop
 * browser handoff carries the SSO callback's token in memory only and
 * hands it over from a tab that stays signed out. Same error semantics as
 * `request()`; the stored token (if any) is neither read nor touched.
 */
async function requestAs<T>(
  path: string,
  token: string,
  options: RequestInit = {},
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
    Authorization: `Bearer ${token}`,
  };
  const response = await doFetch(path, { ...options, headers });
  if (!response.ok) {
    // `null`, not the bearer: the 401 branch of the shared handler treats a
    // token as THE stored session and clears it with a redirect to /login -
    // which would sign the browser out of an unrelated session it already
    // held. A refused memory-only bearer is just an error for the caller.
    await throwForErrorResponse(response, null);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

/** Multipart request shared by `api.postForm` / `api.putForm`.
 *  Deliberately sets NO `Content-Type` — the browser must add the multipart
 *  boundary itself — but keeps `request()`'s auth + error semantics. */
async function formRequest<T>(
  method: "POST" | "PUT",
  path: string,
  formData: FormData,
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;

  const response = await doFetch(path, { method, headers, body: formData });
  if (!response.ok) {
    await throwForErrorResponse(response, token);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  // `signal` aborts the request (the wizard assistant's Stop); `keepalive`
  // lets it outlive the page (the companion relocking as it is hidden).
  post: <T>(path: string, body?: unknown, init?: { signal?: AbortSignal; keepalive?: boolean }) =>
    request<T>(path, {
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
      signal: init?.signal,
      keepalive: init?.keepalive,
    }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body: body ? JSON.stringify(body) : undefined }),
  // `keepalive` lets the request outlive the page (a save as the tab closes);
  // the browser caps such a body at 64 KiB, the caller checks.
  patch: <T>(path: string, body?: unknown, init?: { keepalive?: boolean }) =>
    request<T>(path, { method: "PATCH", body: body ? JSON.stringify(body) : undefined, keepalive: init?.keepalive }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
  // POST as an explicit bearer the caller holds in memory (never the stored
  // session) - the Desktop browser sign-in handoff; see `requestAs`.
  postAs: <T>(path: string, token: string, body?: unknown) =>
    requestAs<T>(path, token, { method: "POST", body: body ? JSON.stringify(body) : undefined }),
  // Multipart upload — no Content-Type header (the browser sets the boundary),
  // otherwise identical error semantics to request().
  postForm: <T>(path: string, formData: FormData) => formRequest<T>("POST", path, formData),
  // Same, for endpoints that replace an existing binary in place (PUT).
  putForm: <T>(path: string, formData: FormData) => formRequest<T>("PUT", path, formData),
  // Binary download — returns the Blob plus the server-provided filename
  // (Content-Disposition), otherwise identical error semantics to request().
  download: async (path: string): Promise<{ blob: Blob; filename: string | null }> => {
    const token = getToken();
    const headers: Record<string, string> = {};
    if (token) headers.Authorization = `Bearer ${token}`;

    const response = await doFetch(path, { method: "GET", headers });
    if (!response.ok) {
      await throwForErrorResponse(response, token);
    }
    const filename = filenameFromContentDisposition(
      response.headers.get("content-disposition"),
    );
    return { blob: await response.blob(), filename };
  },
  // Binary download from a POST — for endpoints whose result is a file but
  // that are not idempotent (the MCP package builder mints an API key, so it
  // cannot be a GET). Returns the response headers alongside the blob because
  // such endpoints report out-of-band facts there; everything else — base URL,
  // auth header, BackendUnavailableError, the 401/403 handling in
  // `throwForErrorResponse` — is inherited rather than re-implemented, which
  // is what a hand-rolled `fetch` in a hook silently loses.
  postDownload: async (
    path: string,
    body?: unknown,
  ): Promise<{ blob: Blob; filename: string | null; headers: Headers }> => {
    const token = getToken();
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (token) headers.Authorization = `Bearer ${token}`;

    const response = await doFetch(path, {
      method: "POST",
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      await throwForErrorResponse(response, token);
    }
    return {
      blob: await response.blob(),
      filename: filenameFromContentDisposition(
        response.headers.get("content-disposition"),
      ),
      headers: response.headers,
    };
  },
};
