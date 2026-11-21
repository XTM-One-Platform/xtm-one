// SPDX-License-Identifier: Apache-2.0

import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import type { NotificationFeed } from "@/types";

const NOTIFICATIONS_KEY = ["notifications"] as const;

/** The person's notifications as the server presents them (`GET /notifications`):
 *  each with the page it opens and whether it is unread. The bell, the
 *  companion's Inbox and a phone push read the same feed. Kept OUTSIDE the
 *  `["activity"]` namespace: the activity stream invalidates that one on every
 *  event; the bell stays fresh through this poll. */
export function useNotifications(enabled = true) {
  return useQuery<NotificationFeed>({
    queryKey: NOTIFICATIONS_KEY,
    queryFn: () => api.get("/notifications"),
    refetchInterval: REFETCH_INTERVAL_STANDARD,
    enabled,
  });
}

/** Mark every notification read (on every device: read state is on the server). */
export function useMarkNotificationsRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<NotificationFeed>("/notifications/read", {}),
    onSuccess: (feed) => qc.setQueryData(NOTIFICATIONS_KEY, feed),
  });
}

/** Mark one notification read. */
export function useDismissNotification() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post(`/notifications/${encodeURIComponent(id)}/dismiss`),
    onMutate: (id) => {
      qc.setQueryData<NotificationFeed>(NOTIFICATIONS_KEY, (feed) =>
        feed
          ? {
              ...feed,
              // An entry its recipient opens or dismisses is read (#901), except a
              // decision still waiting, read only by its answer (#991).
              items: feed.items.map((item) => (item.id === id && !item.awaiting_answer ? { ...item, unread: false } : item)),
              unread_count: feed.items.filter((item) => item.unread && (item.id !== id || item.awaiting_answer)).length,
            }
          : feed,
      );
    },
    onSettled: () => qc.invalidateQueries({ queryKey: NOTIFICATIONS_KEY }),
  });
}

/** The marker this browser kept before read state moved to the server. */
const LEGACY_READ_KEY = "copilot_notif_read";
const LEGACY_DISMISSED_KEY = "copilot_notif_dismissed";

/** Hand this browser's old read marker to the server once, so nobody's bell
 *  refills on the upgrade; then forget it. The later marker wins server-side. */
export function useImportLegacyReadState() {
  const qc = useQueryClient();
  useEffect(() => {
    let readAt: number | null;
    let dismissed: string[];
    try {
      const raw = localStorage.getItem(LEGACY_READ_KEY);
      readAt = raw ? Number.parseInt(raw, 10) : null;
      const rawDismissed = JSON.parse(localStorage.getItem(LEGACY_DISMISSED_KEY) || "[]");
      dismissed = Array.isArray(rawDismissed) ? rawDismissed.filter((id): id is string => typeof id === "string").slice(-500) : [];
    } catch {
      return;
    }
    if (!readAt && dismissed.length === 0) return;
    const body = {
      ...(readAt && Number.isFinite(readAt) ? { read_at: new Date(readAt).toISOString() } : {}),
      dismissed_ids: dismissed.filter((id) => /^[0-9a-fA-F-]{1,64}$/.test(id)),
    };
    void api
      .post<NotificationFeed>("/notifications/read", body)
      .then((feed) => {
        qc.setQueryData(NOTIFICATIONS_KEY, feed);
        try {
          localStorage.removeItem(LEGACY_READ_KEY);
          localStorage.removeItem(LEGACY_DISMISSED_KEY);
        } catch {
          // Kept: the next load tries again, the later marker still wins.
        }
      })
      .catch(() => {});
  }, [qc]);
}
