// SPDX-License-Identifier: Apache-2.0

import { useCallback, useSyncExternalStore } from "react";

import { installMethod, type InstallMethod } from "@/lib/install-app";
import { installPrompt, isStandalone, promptInstall, subscribeInstallPrompt } from "@/lib/pwa";

/** How this device can install the app, and the Install action when the
 *  browser offers one (`lib/pwa.ts`, `lib/install-app.ts`). */
export function useInstallApp(): { method: InstallMethod; install: () => Promise<boolean> } {
  const hasPrompt = useSyncExternalStore(subscribeInstallPrompt, () => installPrompt() !== null, () => false);
  const method = installMethod({
    standalone: isStandalone(),
    hasPrompt,
    userAgent: typeof navigator === "undefined" ? "" : navigator.userAgent,
    maxTouchPoints: typeof navigator === "undefined" ? 0 : navigator.maxTouchPoints ?? 0,
  });
  const install = useCallback(() => promptInstall(), []);
  return { method, install };
}
