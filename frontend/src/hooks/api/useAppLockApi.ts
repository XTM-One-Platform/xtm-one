// SPDX-License-Identifier: Apache-2.0

import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useCompanionDevice } from "@/hooks/useCompanionDevice";
import { api } from "@/lib/api";
import type { AppLockStatus } from "@/types";

/** The companion's PIN lock (`lib/app-lock.ts`, `backend/app/services/app_lock.py`). */
export const APP_LOCK_KEY = ["app-lock"] as const;

/**
 * The lock's state for this session. Read on purpose only: never polled.
 *
 * On a phone the first read also records the session as the companion's and
 * locks it (`POST /app-lock/session`): every time the app opens it asks the
 * PIN. Later reads only look (`GET /app-lock`).
 */
export function useAppLockStatus() {
  const phone = useCompanionDevice();
  const qc = useQueryClient();
  return useQuery<AppLockStatus>({
    queryKey: APP_LOCK_KEY,
    queryFn: () => (phone && !opened.has(qc) ? openCompanionSession(qc) : api.get("/app-lock")),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
}

/** The query clients (one per page load) that already opened the companion's session. */
const opened = new WeakSet<QueryClient>();

async function openCompanionSession(qc: QueryClient): Promise<AppLockStatus> {
  const status = await api.post<AppLockStatus>("/app-lock/session");
  opened.add(qc);
  return status;
}

/** Lock this session now; `keepalive` lets the request leave as the app is hidden. */
export function lockCompanionSession(): Promise<AppLockStatus> {
  return api.post<AppLockStatus>("/app-lock/lock", undefined, { keepalive: true });
}

function useLockMutation<TVars>(mutationFn: (vars: TVars) => Promise<AppLockStatus>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn,
    // The lock screens and the profile card say what went wrong themselves.
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (status) => {
      // What was refused while locked (423) is read again, not shown as an error.
      if (!status.locked && !status.setup_required) {
        void qc.resetQueries({ predicate: (query) => query.state.status === "error" });
      }
      qc.setQueryData(APP_LOCK_KEY, status);
    },
  });
}

export function useUnlockApp() {
  return useLockMutation((pin: string) => api.post<AppLockStatus>("/app-lock/unlock", { pin }));
}

/** Choose a PIN (it turns the lock on) or change it; from a phone the current one is needed. */
export function useSetAppPin() {
  return useLockMutation(({ pin, currentPin }: { pin: string; currentPin?: string }) =>
    api.put<AppLockStatus>("/app-lock/pin", { pin, ...(currentPin ? { current_pin: currentPin } : {}) }),
  );
}

export function useToggleAppLock() {
  return useLockMutation(({ enabled, currentPin }: { enabled: boolean; currentPin?: string }) =>
    api.patch<AppLockStatus>("/app-lock", { enabled, ...(currentPin ? { current_pin: currentPin } : {}) }),
  );
}
