// SPDX-License-Identifier: Apache-2.0

/**
 * The installable web app: the service worker and the browser's install prompt.
 *
 * `startPwa()` runs once at boot (`main.tsx`), before React mounts, because a
 * Chromium browser fires `beforeinstallprompt` as soon as the page qualifies,
 * often before any component that would listen is on screen. The event is held
 * here and handed to `useInstallApp()` (`hooks/useInstallApp.ts`).
 *
 * The service worker (`public/sw.js`) caches nothing: it only answers an
 * offline page when a navigation fails, which is also what makes the app
 * installable on browsers that still ask for one. It is registered under the
 * platform's base path, and only in a secure context (HTTPS or localhost),
 * where browsers allow service workers at all.
 */
import { withBasePath } from "@/lib/base-path";

/** The event Chromium fires when the app can be installed (not in the DOM typings). */
export interface BeforeInstallPromptEvent extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

let heldPrompt: BeforeInstallPromptEvent | null = null;
const listeners = new Set<() => void>();
let started = false;

function notify() {
  for (const listener of listeners) listener();
}

/** The held install prompt, or `null`. */
export function installPrompt(): BeforeInstallPromptEvent | null {
  return heldPrompt;
}

/** Called when the held prompt changes; returns the unsubscribe. */
export function subscribeInstallPrompt(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Show the browser's install dialog. Resolves with whether the person installed. */
export async function promptInstall(): Promise<boolean> {
  const event = heldPrompt;
  if (!event) return false;
  // A prompt can be shown once: whatever the answer, it is spent.
  heldPrompt = null;
  notify();
  await event.prompt();
  const { outcome } = await event.userChoice;
  return outcome === "accepted";
}

/** Whether the page runs as the installed app. */
export function isStandalone(): boolean {
  if (typeof window === "undefined") return false;
  const iosStandalone = (navigator as Navigator & { standalone?: boolean }).standalone === true;
  return iosStandalone || window.matchMedia?.("(display-mode: standalone)").matches === true;
}

export function startPwa(): void {
  if (started || typeof window === "undefined") return;
  started = true;
  window.addEventListener("beforeinstallprompt", (event) => {
    // Keep the browser's mini-infobar away: the companion offers its own button.
    event.preventDefault();
    heldPrompt = event as BeforeInstallPromptEvent;
    notify();
  });
  window.addEventListener("appinstalled", () => {
    heldPrompt = null;
    notify();
  });
  if ("serviceWorker" in navigator && window.isSecureContext) {
    void navigator.serviceWorker
      .register(withBasePath("/sw.js"), { scope: withBasePath("/") })
      .catch(() => {
        // The app works without it; only the offline page and, on some
        // browsers, the install offer are lost.
      });
  }
}
