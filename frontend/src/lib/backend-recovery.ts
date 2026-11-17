// SPDX-License-Identifier: Apache-2.0

/**
 * Waiting for the backend to come back after an outage (a restart, a deploy):
 * the liveness route `/api/health` is asked every few seconds, and the caller
 * is told once it answers. It needs no session and is never held by the
 * startup gate or the licence wall (`backend/app/main.py`).
 */

import { useEffect, useRef } from "react";
import { withBasePath } from "@/lib/base-path";

/** How often the liveness route is asked while the backend is unreachable (ms). */
export const BACKEND_RECOVERY_POLL_MS = 3_000;

/** The liveness route under the URL path prefix the platform is served from. */
const BACKEND_HEALTH_PATH = withBasePath("/api/health");

/** Whether the backend answers its liveness route now. Never rejects. */
async function backendAnswers(): Promise<boolean> {
  try {
    const response = await fetch(BACKEND_HEALTH_PATH, { cache: "no-store" });
    return response.ok;
  } catch {
    return false;
  }
}

/**
 * While *enabled*, ask the liveness route every {@link BACKEND_RECOVERY_POLL_MS}
 * and call *onBack* once, the first time it answers.
 */
export function useBackendRecovery(enabled: boolean, onBack: () => void): void {
  const onBackRef = useRef(onBack);
  onBackRef.current = onBack;

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      const back = await backendAnswers();
      if (stopped) return;
      if (back) {
        stopped = true;
        onBackRef.current();
        return;
      }
      timer = setTimeout(poll, BACKEND_RECOVERY_POLL_MS);
    };
    timer = setTimeout(poll, BACKEND_RECOVERY_POLL_MS);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [enabled]);
}
