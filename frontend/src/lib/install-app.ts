// SPDX-License-Identifier: Apache-2.0

/**
 * How this device can install the platform as an app (the mobile companion).
 *
 * Chromium browsers (Android, desktop Chrome and Edge) fire
 * `beforeinstallprompt` when the web manifest qualifies: the app offers its own
 * Install button and the browser shows its dialog. Safari on iPhone and iPad
 * fires nothing and installs only from its Share menu ("Add to Home Screen"),
 * so the app explains that instead. An app already running installed (display
 * mode `standalone`) offers nothing.
 *
 * Pure module: `install-app.test.ts` runs it on `node --test`. The browser glue
 * (the event, the service worker) is `pwa.ts`.
 */

export type InstallMethod =
  /** Running as the installed app. */
  | "installed"
  /** The browser handed us its install prompt: an Install button works. */
  | "prompt"
  /** Safari on iOS / iPadOS: install from the Share menu. */
  | "ios"
  /** Nothing to offer here (a browser that cannot install, or not yet). */
  | "unavailable";

export interface InstallContext {
  /** `display-mode: standalone` matches, or iOS `navigator.standalone`. */
  standalone: boolean;
  /** A `beforeinstallprompt` event is held. */
  hasPrompt: boolean;
  userAgent: string;
  /** iPadOS reports a Mac user agent: its touch points tell it apart. */
  maxTouchPoints: number;
}

/** Safari on an iPhone, an iPod or an iPad (not another browser on iOS:
 *  Chrome and Firefox there cannot add to the Home Screen with a manifest). */
export function isIosSafari(userAgent: string, maxTouchPoints: number): boolean {
  const ios = /iPhone|iPad|iPod/.test(userAgent) || (/Macintosh/.test(userAgent) && maxTouchPoints > 1);
  if (!ios) return false;
  return /Safari\//.test(userAgent) && !/CriOS|FxiOS|EdgiOS|OPiOS/.test(userAgent);
}

export function installMethod(context: InstallContext): InstallMethod {
  if (context.standalone) return "installed";
  if (context.hasPrompt) return "prompt";
  if (isIosSafari(context.userAgent, context.maxTouchPoints)) return "ios";
  return "unavailable";
}

/** How long a dismissed install banner stays away. */
export const INSTALL_BANNER_SNOOZE_MS = 30 * 24 * 60 * 60 * 1000;

/** Whether the banner was dismissed recently enough to stay hidden. */
export function installBannerSnoozed(dismissedAt: number | null, now: number): boolean {
  return dismissedAt !== null && Number.isFinite(dismissedAt) && now - dismissedAt < INSTALL_BANNER_SNOOZE_MS;
}

/** A readable name for this device: "Android · Chrome", "iPhone · Safari". */
export function deviceName(userAgent: string): string {
  const device = /iPhone/.test(userAgent)
    ? "iPhone"
    : /iPad/.test(userAgent)
      ? "iPad"
      : /Android/.test(userAgent)
        ? "Android"
        : "Phone";
  const browser = /EdgA|Edg\//.test(userAgent)
    ? "Edge"
    : /SamsungBrowser/.test(userAgent)
      ? "Samsung Internet"
      : /Firefox|FxiOS/.test(userAgent)
        ? "Firefox"
        : /Chrome|CriOS/.test(userAgent)
          ? "Chrome"
          : /Safari/.test(userAgent)
            ? "Safari"
            : "Browser";
  return `${device} · ${browser}`;
}

export function devicePlatform(userAgent: string): "android" | "ios" | "other" {
  if (/iPhone|iPad|iPod/.test(userAgent)) return "ios";
  return /Android/.test(userAgent) ? "android" : "other";
}
