/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * XTM One Desktop bridge.
 *
 * The native Desktop app hosts THIS web app in a webview and injects a small
 * read-only object before any script runs:
 *
 *   window.__XTM_ONE_DESKTOP__ = { version, platform, arch, runner_id,
 *                                  runner_name, server_url, features,
 *                                  open_extensions }
 *
 * (`features` / `open_extensions`: what the shell can do on the
 * machine - open or show a local path, ask for attention - so a web app newer
 * than the installed shell only offers what that shell will honour.)
 *
 * That object is the whole contract between the shell and the page. It tells
 * the page two things: that it is running inside the Desktop app (so the
 * "get the app" nudges make no sense and the sidebar footer shows the machine
 * instead), and WHICH enrolled runner the app embeds - the id a chat turn sends
 * as `desktop_runner_id`, which pins that machine as the default target of the
 * agent's `computer_use` tools and turns held shell commands into inline
 * approval cards.
 *
 * Everything here degrades to "not the desktop" when the global is absent or
 * malformed: the web app must never depend on the shell being there. The
 * machine's live connection state is read from the platform itself
 * (`useRunner(runner_id)`), never from the shell.
 *
 * Pure helpers only; the hooks live in `@/hooks/useDesktop.ts`.
 */
import type {
  DesktopBridge,
  DesktopBrowserLoginProvider,
  DesktopBrowserLoginStarted,
  DesktopSession,
  RunnerApprovalRequest,
  RunnerPlatform,
} from "@/types";

/**
 * The slice of Tauri's global API (`withGlobalTauri`) the page uses. The shell
 * grants the instance's origin a handful of commands and the event channel at
 * runtime; anything else is refused by the shell's ACL, so this surface is
 * exactly what the web app may rely on.
 */
interface TauriGlobal {
  core: { invoke: <T>(command: string, args?: Record<string, unknown>) => Promise<T> };
  event: {
    listen: <T>(event: string, handler: (event: { payload: T }) => void) => Promise<() => void>;
  };
}

declare global {
  interface Window {
    __XTM_ONE_DESKTOP__?: unknown;
    __TAURI__?: TauriGlobal;
  }
}

/** Invoke one of the shell commands granted to this page; rejects outside the Desktop app. */
function desktopInvoke<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  const tauri = typeof window !== "undefined" ? window.__TAURI__ : undefined;
  if (!tauri) return Promise.reject(new Error("Not running inside XTM One Desktop"));
  return tauri.core.invoke<T>(command, args);
}

/** Subscribe to a shell event; resolves to the unlisten function (a no-op outside the app). */
export function desktopListen<T>(event: string, handler: (payload: T) => void): Promise<() => void> {
  const tauri = typeof window !== "undefined" ? window.__TAURI__ : undefined;
  if (!tauri) return Promise.resolve(() => {});
  return tauri.event.listen<T>(event, (e) => handler(e.payload));
}

/**
 * Single sign-on from the Desktop app runs in the SYSTEM browser:
 * identity providers refuse or degrade embedded webviews, and the webview
 * shares no session with the person's real browser. The shell opens the
 * instance's SSO route in the OS browser and the session comes back through
 * this event once the person completes (and confirms) the sign-in there.
 */
export const DESKTOP_BROWSER_LOGIN_EVENT = "desktop-browser-login";

export function beginDesktopBrowserLogin(provider: DesktopBrowserLoginProvider): Promise<DesktopBrowserLoginStarted> {
  return desktopInvoke<DesktopBrowserLoginStarted>("desktop_begin_browser_login", { provider });
}

export function cancelDesktopBrowserLogin(loginId: string): Promise<void> {
  return desktopInvoke<void>("desktop_cancel_browser_login", { loginId });
}

/** Product name of the native client, as every human-facing surface spells it. */
export const DESKTOP_PRODUCT_NAME = "XTM One Desktop";

/** Route of the download + machines page (the sidebar footer and the user menu link here). */
export const DESKTOP_ROUTE = "/desktop";

/**
 * Whether the runner subsystem - and with it XTM One Desktop - is on
 * (`RUNNERS_ENABLED`, read from `/auth/config`). `undefined` while that config
 * has not answered: a deployment with runners off must never show a Desktop
 * entry for a moment. Once it settled, a payload without the flag (a backend
 * that predates it) or a request that failed reads as on, the server default
 * (the degrade-to-defaults policy documented on `useAuthConfig`).
 */
export function runnersEnabledFrom(
  config: { runners_enabled?: boolean } | undefined,
  configPending: boolean,
): boolean | undefined {
  if (configPending) return undefined;
  return config?.runners_enabled ?? true;
}

/** The admin Objects browser's type filter, read against the runners flag. */
interface AdminObjectsTypeFilter {
  /** Whether the Type pill offers `Machines`. */
  offerRunners: boolean;
  /** The types the list is filtered on (none: every configuration type). */
  objectTypes: string[];
  /** A list that names machines waiting for the flag: not requested yet. */
  waitForRunners: boolean;
}

/**
 * The admin Objects browser's type filter against `runnersEnabledFrom`.
 * `Machines` stays offered until runners are known to be off, so a
 * `?type=runner` link keeps its filter while `/auth/config` loads, and a
 * `runner` filter is dropped once they are: the backend drops the arm from
 * its union and ignores a type it does not offer. For the same reason a list
 * that names machines is not requested before the flag is known.
 */
export function adminObjectsTypeFilter(requestedTypes: readonly string[], runners: boolean | undefined): AdminObjectsTypeFilter {
  const offerRunners = runners !== false;
  const objectTypes = requestedTypes.filter((type) => offerRunners || type !== "runner");
  return { offerRunners, objectTypes, waitForRunners: objectTypes.includes("runner") && runners === undefined };
}

/**
 * Whether the admin Objects browser shows its skeleton instead of a list:
 * while the list loads with nothing to show, and for as long as a list that
 * names machines waits for the flag, whatever rows the query still holds -
 * those are the previous filter's (placeholder data), never machines.
 */
export function adminObjectsLoading(
  filter: AdminObjectsTypeFilter,
  list: { isLoading: boolean; data?: unknown },
): boolean {
  return filter.waitForRunners || (list.isLoading && list.data === undefined);
}

/** Key of the per-conversation "act on this machine" preference in sessionStorage. */
const LOCAL_EXECUTION_KEY_PREFIX = "xtm-desktop-local-execution:";

const PLATFORMS: readonly RunnerPlatform[] = ["windows", "macos", "linux"];

/** A runner id as the platform mints them; anything else is treated as "not enrolled". */
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

/** A list of short tokens from the bridge (a feature, an extension); anything else is dropped. */
function asTokenList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && /^[a-z0-9_]{1,32}$/.test(item));
}

/** Parse the injected global into a typed bridge, or `null` when absent / malformed. */
export function readDesktopBridge(raw: unknown = typeof window !== "undefined" ? window.__XTM_ONE_DESKTOP__ : undefined): DesktopBridge | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const platform = asString(obj.platform);
  const version = asString(obj.version);
  const serverUrl = asString(obj.server_url);
  if (!version || !serverUrl || !platform || !PLATFORMS.includes(platform as RunnerPlatform)) return null;
  const runnerId = asString(obj.runner_id);
  return {
    version,
    platform: platform as RunnerPlatform,
    arch: asString(obj.arch) ?? "x64",
    // A malformed id would only ever earn a 400 from `/runners/{id}`; read it
    // as "not enrolled" instead, which every surface already renders.
    runner_id: runnerId && UUID_RE.test(runnerId) ? runnerId : null,
    runner_name: asString(obj.runner_name),
    server_url: serverUrl,
    // Absent on an older shell: nothing beyond the original
    // bridge is offered then.
    features: asTokenList(obj.features),
    open_extensions: asTokenList(obj.open_extensions),
  };
}

/** The bridge, read once per page load (the shell injects it before any script). */
let cached: DesktopBridge | null | undefined;
export function getDesktopBridge(): DesktopBridge | null {
  if (cached === undefined) cached = readDesktopBridge();
  return cached;
}

/** Bridge feature: the shell opens / shows local paths. */
export const DESKTOP_FEATURE_LOCAL_PATHS = "local_paths";
/** Bridge feature: the shell asks for the person's attention. */
export const DESKTOP_FEATURE_ATTENTION = "attention";
/** Bridge feature: the shell keeps the web session. */
export const DESKTOP_FEATURE_SESSION = "session";

/** Whether the shell this page runs in announced `feature` (never on the web). */
export function desktopSupports(feature: string, bridge: DesktopBridge | null = getDesktopBridge()): boolean {
  return !!bridge && bridge.features.includes(feature);
}

/**
 * Whether a path from a machine-activity card can be acted on HERE: inside
 * the Desktop app, on the machine the card's command ran on (`runnerId`), by
 * a shell that supports local paths. A path quoted in a reply names no
 * machine: `runnerId` `undefined` means "the machine the app runs on".
 */
export function canActOnLocalPath(runnerId?: string | null, bridge: DesktopBridge | null = getDesktopBridge()): boolean {
  if (!bridge || !desktopSupports(DESKTOP_FEATURE_LOCAL_PATHS, bridge)) return false;
  return runnerId === undefined || (!!bridge.runner_id && runnerId === bridge.runner_id);
}

/**
 * Why the shell refused a local-path action - the tokens `desktop_open_local_path`
 * / `desktop_reveal_local_path` answer with (`local_paths.rs` + the gate in
 * `gui.rs`). Anything else reads as `failed`.
 */
export type LocalPathFailure =
  | "invalid_path"
  | "remote_path"
  | "not_found"
  | "not_openable"
  | "unfocused"
  | "too_fast"
  | "failed";

const LOCAL_PATH_FAILURES: readonly LocalPathFailure[] = [
  "invalid_path",
  "remote_path",
  "not_found",
  "not_openable",
  "unfocused",
  "too_fast",
  "failed",
];

/** The refusal token of a rejected local-path call. */
export function localPathFailure(error: unknown): LocalPathFailure {
  const token = typeof error === "string" ? error : error instanceof Error ? error.message : "";
  return (LOCAL_PATH_FAILURES as readonly string[]).includes(token) ? (token as LocalPathFailure) : "failed";
}

/** Open a local item with its default application (passive content; a folder in the file manager). */
export function openLocalPath(path: string): Promise<void> {
  return desktopInvoke<void>("desktop_open_local_path", { path });
}

/** Show a local item selected in the OS file manager. */
export function revealLocalPath(path: string): Promise<void> {
  return desktopInvoke<void>("desktop_reveal_local_path", { path });
}

/**
 * A turn finished while the person was looking elsewhere: flash the taskbar
 * entry / bounce the Dock icon. Best effort, and silent outside the app or on
 * a shell without the feature.
 */
export function requestDesktopAttention(): void {
  if (!desktopSupports(DESKTOP_FEATURE_ATTENTION)) return;
  desktopInvoke<void>("desktop_request_attention").catch(() => {
    /* attention is a courtesy, never an error the person has to see */
  });
}

/**
 * The web session inside XTM One Desktop. The page's storage is the
 * webview's, which can roll back: on Windows one torn write makes it drop
 * every later write at each launch, and the page would boot with a session it
 * signed out of, or one long expired. So the shell keeps the session as the
 * page last set it (`storeDesktopSession`, on every change), and at launch
 * that copy wins over the page's storage (`desktopSessionAtBoot`). The
 * platform still judges the session, as in a browser.
 */

/** What the shell answered for the session it keeps: `null` when it keeps none, `undefined` when unreadable. */
export function readDesktopSession(raw: unknown): DesktopSession | null | undefined {
  if (raw === null) return null;
  if (!raw || typeof raw !== "object") return undefined;
  const { token, admin_token: adminToken } = raw as Record<string, unknown>;
  const asToken = (value: unknown) => (typeof value === "string" && value ? value : null);
  if ((token != null && !asToken(token)) || (adminToken != null && !asToken(adminToken))) return undefined;
  // An administrator's token only ever sits under the session it impersonates.
  if (adminToken != null && token == null) return undefined;
  return { token: asToken(token), admin_token: asToken(adminToken) };
}

/** What the page does with its storage at launch, given the session the shell kept. */
export type DesktopSessionBoot =
  /** The shell's copy differs from the page's storage: the storage takes it. */
  | { kind: "adopt"; session: DesktopSession }
  /** The shell keeps none yet (it predates the feature): it takes the page's. */
  | { kind: "hand_over"; session: DesktopSession }
  | { kind: "keep" };

/**
 * `kept`: the session the shell keeps, `null` when it keeps none, `undefined`
 * when it could not be asked (the page then keeps its storage as it is).
 * `stored`: the session in the page's storage.
 */
export function desktopSessionAtBoot(kept: DesktopSession | null | undefined, stored: DesktopSession): DesktopSessionBoot {
  if (kept === undefined) return { kind: "keep" };
  if (kept === null) return stored.token ? { kind: "hand_over", session: stored } : { kind: "keep" };
  if (kept.token === stored.token && kept.admin_token === stored.admin_token) return { kind: "keep" };
  return { kind: "adopt", session: kept };
}

/** How long the launch waits for the shell's copy before going on with the page's storage. */
const DESKTOP_SESSION_LOAD_TIMEOUT_MS = 2_000;

/** The session the shell keeps (see `readDesktopSession`); never rejects. */
export function loadDesktopSession(timeoutMs: number = DESKTOP_SESSION_LOAD_TIMEOUT_MS): Promise<DesktopSession | null | undefined> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(undefined), timeoutMs);
    desktopInvoke<unknown>("desktop_load_session")
      .then(readDesktopSession, () => undefined)
      .then((kept) => {
        clearTimeout(timer);
        resolve(kept);
      });
  });
}

/** Hand the session as it now stands to the shell. Best effort: the shell forgets a copy it could not update. */
export function storeDesktopSession(session: DesktopSession): void {
  desktopInvoke<void>("desktop_store_session", { session }).catch(() => {
    /* the page's own storage still holds the session */
  });
}

/**
 * Detect the visitor's OS for the download page's primary button. Coarse on
 * purpose - the three families the runner ships for, `null` when unsure (a
 * phone, an exotic UA), in which case the page shows every platform equally.
 */
export function detectVisitorPlatform(userAgent: string = typeof navigator !== "undefined" ? navigator.userAgent : ""): RunnerPlatform | null {
  const ua = userAgent.toLowerCase();
  if (/iphone|ipad|android/.test(ua)) return null;
  if (ua.includes("windows")) return "windows";
  if (ua.includes("mac os") || ua.includes("macintosh")) return "macos";
  if (ua.includes("linux") || ua.includes("x11")) return "linux";
  return null;
}

/** Whether the visitor's CPU looks like arm64 (Windows on ARM, arm64 Linux) - best effort. */
export function detectVisitorArm64(userAgent: string = typeof navigator !== "undefined" ? navigator.userAgent : ""): boolean {
  const ua = userAgent.toLowerCase();
  return /\barm64\b|\baarch64\b/.test(ua);
}

/** How long the agent's tool waits for an inline decision when the frame does not say
 *  (mirrors `DESKTOP_APPROVAL_WAIT_SECONDS` in `builtin_tools/computer_use.py`). */
const DEFAULT_APPROVAL_WAIT_SECONDS = 90;

/** Parse a `runner_approval_required` tool-progress frame into a typed request, or `null` when malformed. */
export function readRunnerApprovalRequest(frame: Record<string, unknown>): RunnerApprovalRequest | null {
  const commandId = frame.command_id;
  const runnerId = frame.runner_id;
  if (typeof commandId !== "string" || !commandId || typeof runnerId !== "string" || !runnerId) return null;
  const expires =
    typeof frame.expires_in_seconds === "number" && Number.isFinite(frame.expires_in_seconds)
      ? Math.max(0, frame.expires_in_seconds)
      : DEFAULT_APPROVAL_WAIT_SECONDS;
  return {
    command_id: commandId,
    runner_id: runnerId,
    runner_name: typeof frame.runner_name === "string" ? frame.runner_name : "",
    kind: typeof frame.kind === "string" ? frame.kind : "run_shell",
    summary: typeof frame.summary === "string" ? frame.summary : "",
    expires_in_seconds: expires,
    received_at_ms: Date.now(),
  };
}

/**
 * Per-conversation switch for "the agent may act on this machine". Default ON
 * inside the Desktop app - that is what the app is for - and remembered per
 * conversation for the session, so turning it off for a sensitive thread
 * sticks while you work in it.
 */
export function readLocalExecutionPreference(conversationId: string | null): boolean {
  if (!conversationId || typeof sessionStorage === "undefined") return true;
  try {
    const stored = sessionStorage.getItem(LOCAL_EXECUTION_KEY_PREFIX + conversationId);
    return stored === null ? true : stored === "1";
  } catch {
    return true;
  }
}

export function writeLocalExecutionPreference(conversationId: string | null, enabled: boolean): void {
  if (!conversationId || typeof sessionStorage === "undefined") return;
  try {
    sessionStorage.setItem(LOCAL_EXECUTION_KEY_PREFIX + conversationId, enabled ? "1" : "0");
  } catch {
    /* storage full or blocked - the switch still works for this render */
  }
}
